/**
 * SCH Telegram plugin for OpenCode 2 — seeded by init-workspace.sh.
 *
 * Milestones (add-telegram-notifications): seeded by init-workspace.sh into
 * the global OpenCode config plugin dir ($XDG_CONFIG_HOME/opencode/plugin/).
 * Same contract as the Claude hook template (design D3/D4): milestone events
 * are written as JSON files into the local spool dir consumed by the shim's
 * notifier thread — the plugin NEVER talks to the network and is a complete
 * no-op when the shim's local notifier marker is absent.
 *
 * OpenCode 2 plugin API (TASK-7 port; V1 plugin functions do not load in 2.x):
 * a default export `{ id, setup(ctx) }`. `setup` registers everything through
 * the context — `ctx.event.subscribe()` (public event stream, replaces the V1
 * `event` hook), `ctx.tool.hook("execute.before"/"execute.after")` and
 * `ctx.permission.hook("evaluate")` — and returns the cleanup function. The
 * plugin is a plain object (no `@opencode/plugin` import: it is a single seeded
 * file without node_modules; `Plugin.define` is a typed identity helper).
 *
 * Remote approval (add-telegram-interaction, task 3.4): the `evaluate`
 * permission hook runs after the configured rules and BEFORE the native ask is
 * published. When inbound interaction is configured and no operator is attached
 * locally, an `ask` decision is registered in the local approval broker
 * (requests/<id>.json), the permission-request milestone is emitted with the
 * request id (the shim attaches the Approve/Deny inline keyboard), and the hook
 * waits for decisions/<id>.json with a bounded timeout. approve/deny -> the
 * evaluation's `effect` becomes allow/deny (no native prompt); timeout -> the
 * effect stays `ask` (the native ask flow takes over, never auto-approval) and
 * the timeout decision is deposited atomically so a late remote decision finds
 * the slot taken.
 *
 * Dual-control (task 3.5): a later `permission.replied` event for a session
 * whose remote wait ended in timeout (or was closed by the broker as "resolved
 * elsewhere" on re-attach) writes native/<id>.json — the shim updates the
 * Telegram message as "resolved elsewhere".
 *
 * Interactive keep-alive (add-interactive-busy-keepalive): the plugin also
 * maintains per-session activity markers in SCH_ACTIVITY_DIR so the shim's
 * busy-watcher can advertise HealthyBusy on /ping while ANY opencode process
 * (TUI, `opencode serve`, headless) has a turn in flight — otherwise AgentCore
 * idle-reaps the microVM mid-turn (~15 min without invocations). This layer is
 * ALWAYS active (independent of the Telegram opt-in gate): marker files are
 * local-disk only and never touch the network.
 */

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const ACTIVITY_DIR = process.env.SCH_ACTIVITY_DIR || "/tmp/sch-activity";
// Streaming emits deltas at high frequency: rate-limit the busy writes per
// session. Idle/error transitions always write immediately.
const ACTIVITY_THROTTLE_MS = parseInt(process.env.SCH_ACTIVITY_THROTTLE_MS || "5000", 10);
const ENABLED_MARKER = process.env.SCH_TELEGRAM_ENABLED_MARKER || "/tmp/sch-telegram-enabled";
const PRESENCE_SNAPSHOT = process.env.SCH_COMMAND_SHELL_PRESENCE_FILE || "/tmp/sch-command-shell-presence.json";
const SPOOL_DIR = process.env.SCH_TELEGRAM_SPOOL_DIR || "/tmp/sch-telegram-spool";
const APPROVAL_DIR = process.env.SCH_APPROVAL_DIR || "/tmp/sch-approval";
// OQ-I1: remote wait default 10 minutes (design D4).
const APPROVAL_TIMEOUT_S = parseInt(process.env.SCH_APPROVAL_TIMEOUT_S || "600", 10);
const DECISION_POLL_MS = 500;
const EXECUTION_MODE = process.env.SCH_EXECUTION_MODE === "headless" ? "headless" : "interactive";

// Event types that mean "a turn is in flight" / "the turn ended" on the 2.x
// public stream. Both the 2.x session.* names and the message.* names still
// emitted for parts are listed; unknown types are ignored.
const BUSY_EVENTS = new Set([
  "session.execution.started", "session.step.started", "session.step.streamed",
  "session.text.started", "session.text.delta", "session.reasoning.started",
  "session.reasoning.delta", "session.tool.called", "session.tool.progress",
  "message.updated", "message.part.updated", "message.part.delta",
]);
const IDLE_EVENTS = new Set([
  "session.idle", "session.error",
  "session.execution.succeeded", "session.execution.failed", "session.execution.interrupted",
]);

const readJson = (file) => {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf-8"));
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  }
};

const writeJsonAtomic = (file, payload) => {
  const tmp = `${file}.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`;
  fs.writeFileSync(tmp, JSON.stringify(payload));
  fs.renameSync(tmp, file);
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Event payload accessor: 2.x events carry `data`, the 1.x-shaped ones
// `properties`. Only the fields SCH reads are extracted.
const eventData = (event) => event?.data || event?.properties || {};

const sessionFromEvent = (event) => {
  const d = eventData(event);
  return (
    d.sessionID || d.part?.sessionID || d.info?.sessionID || d.message?.sessionID ||
    d.session?.id || null
  );
};

const textFromEvent = (event) => {
  const d = eventData(event);
  if (typeof d.text === "string") return d.text;
  if (d.part?.type === "text" && typeof d.part.text === "string") return d.part.text;
  return null;
};

// Latest assistant text of a session through the typed client API (the
// streaming shape is a fallback, not the contract).
const lastAssistantTextFromContext = async (ctx, sessionID) => {
  try {
    const result = await ctx.session.context({ sessionID });
    const messages = Array.isArray(result) ? result : result?.data;
    if (!Array.isArray(messages)) return "";
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i];
      if (message?.type !== "assistant" || !Array.isArray(message.content)) continue;
      const text = message.content
        .filter((c) => c?.type === "text" && typeof c.text === "string")
        .map((c) => c.text)
        .join("\n")
        .trim();
      if (text) return text;
    }
  } catch {
    /* best-effort by design */
  }
  return "";
};

export default {
  id: "sch-telegram",

  async setup(ctx = {}) {
    // --- Activity markers (add-interactive-busy-keepalive, always on) -------
    const lastBusyWrite = new Map(); // sessionID -> epoch ms of last busy write

    const writeActivity = (sessionID, state) => {
      // Never-fail contract: a broken marker must not degrade the turn.
      try {
        const sid = String(sessionID || "unknown").replace(/[^A-Za-z0-9._-]/g, "_");
        if (state === "busy") {
          const last = lastBusyWrite.get(sid) || 0;
          if (Date.now() - last < ACTIVITY_THROTTLE_MS) return;
          lastBusyWrite.set(sid, Date.now());
        } else {
          lastBusyWrite.delete(sid);
        }
        fs.mkdirSync(ACTIVITY_DIR, { recursive: true });
        const file = path.join(ACTIVITY_DIR, `${sid}.json`);
        const tmp = `${file}.tmp-${process.pid}`;
        // tmp-then-rename so the shim's poll never sees a partial file.
        fs.writeFileSync(tmp, JSON.stringify({
          sessionID: sid,
          state,
          ts: Date.now() / 1000,
          pid: process.pid,
        }));
        fs.renameSync(tmp, file);
      } catch {
        /* best-effort by design */
      }
    };

    const trackActivity = (event) => {
      const type = event?.type;
      if (IDLE_EVENTS.has(type)) {
        writeActivity(sessionFromEvent(event), "idle");
      } else if (BUSY_EVENTS.has(type)) {
        writeActivity(sessionFromEvent(event), "busy");
      }
    };

    const telegramEnabled = fs.existsSync(ENABLED_MARKER);
    const markerMetadata = telegramEnabled ? readJson(ENABLED_MARKER) || {} : {};
    // The marker exposes capability, not Telegram configuration or credentials.
    const INTERACTION_ENABLED = markerMetadata.interaction_enabled === true;

    // sessionID -> latest assistant text observed on the stream (the parts
    // stream carries the full accumulated text, so the last update wins).
    const lastAssistantText = new Map();
    // sessionID -> broker request ids whose remote wait timed out and that a
    // native reply may still resolve (dual-control bookkeeping).
    const timedOutRequests = new Map();
    let spoolSeq = 0;

    const spool = (event) => {
      // Never-fail contract: a broken milestone must not degrade the turn.
      try {
        fs.mkdirSync(SPOOL_DIR, { recursive: true });
        event.ts = Date.now() / 1000;
        event.source = "opencode";
        event.execution_mode = EXECUTION_MODE;
        const name = `${Date.now()}${String(spoolSeq++).padStart(4, "0")}-${process.pid}.json`;
        const tmp = path.join(SPOOL_DIR, `${name}.tmp`);
        // tmp-then-rename so the notifier's *.json poll never sees a partial file.
        fs.writeFileSync(tmp, JSON.stringify(event));
        fs.renameSync(tmp, path.join(SPOOL_DIR, name));
      } catch {
        /* best-effort by design */
      }
    };

    // --- Approval broker protocol (mirror of image/app/telegram_interaction) ---

    const locallyAttached = () => {
      if (EXECUTION_MODE === "headless") return false;
      const snapshot = readJson(PRESENCE_SNAPSHOT);
      const expiresAt = snapshot?.expires_at;
      const leases = snapshot?.leases;
      if (
        snapshot?.version !== 1 || snapshot?.state !== "attached" ||
        !Array.isArray(leases) || leases.length === 0 ||
        typeof expiresAt !== "number" || !Number.isFinite(expiresAt)
      ) return false;
      const idPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
      const expiries = [];
      for (const lease of leases) {
        if (
          !lease || typeof lease !== "object" ||
          typeof lease.shellId !== "string" || !idPattern.test(lease.shellId) ||
          typeof lease.attachmentId !== "string" || !idPattern.test(lease.attachmentId) ||
          typeof lease.attached_at !== "number" || !Number.isFinite(lease.attached_at) ||
          typeof lease.expires_at !== "number" || !Number.isFinite(lease.expires_at) ||
          lease.attached_at < 0 || lease.expires_at <= lease.attached_at
        ) return false;
        expiries.push(lease.expires_at);
      }
      return expiresAt === Math.max(...expiries) &&
        expiries.some((expiry) => expiry > Date.now() / 1000);
    };

    // Atomically claim the single decision slot (hard link create-or-fail);
    // returns false when the request was already resolved.
    const depositDecision = (rid, outcome, source) => {
      const dir = path.join(APPROVAL_DIR, "decisions");
      fs.mkdirSync(dir, { recursive: true });
      const final = path.join(dir, `${rid}.json`);
      const tmp = `${final}.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`;
      writeJsonAtomic(tmp, {
        id: rid, outcome, source, decided_ts: Date.now() / 1000,
      });
      try {
        fs.linkSync(tmp, final);
        return true;
      } catch {
        return false;
      } finally {
        try { fs.unlinkSync(tmp); } catch { /* already gone */ }
      }
    };

    // `evaluate` hook (2.x): `evaluation` is the mutable PermissionEvaluation
    // {sessionID, action, resources, metadata, effect: allow|ask|deny, message}.
    // Only `ask` decisions are brokered; `allow` passes untouched.
    const handlePermissionEvaluate = async (evaluation) => {
      if (!evaluation || evaluation.effect !== "ask") return;
      if (EXECUTION_MODE === "headless" || locallyAttached()) return;
      const tool = evaluation.action || "tool";
      const resources = Array.isArray(evaluation.resources) ? evaluation.resources : [];
      const detail = resources.join(" \u00B7 ") || String(evaluation.metadata?.command || "");
      if (!INTERACTION_ENABLED) {
        spool({ type: "permission-request", payload: { tool, detail } });
        return;
      }
      const rid = `per_${crypto.randomBytes(8).toString("hex")}`;
      const sessionID = String(evaluation.sessionID || "");
      const requestsDir = path.join(APPROVAL_DIR, "requests");
      fs.mkdirSync(requestsDir, { recursive: true });
      writeJsonAtomic(path.join(requestsDir, `${rid}.json`), {
        id: rid,
        tool,
        detail,
        timeout_s: APPROVAL_TIMEOUT_S,
        source: "opencode",
        execution_mode: EXECUTION_MODE,
        created_ts: Date.now() / 1000,
      });
      spool({
        type: "permission-request",
        payload: { request_id: rid, tool, detail, timeout_s: APPROVAL_TIMEOUT_S },
      });
      const decisionPath = path.join(APPROVAL_DIR, "decisions", `${rid}.json`);
      const deadline = Date.now() + APPROVAL_TIMEOUT_S * 1000;
      let decision = null;
      while (Date.now() < deadline) {
        decision = readJson(decisionPath);
        if (decision) break;
        await sleep(DECISION_POLL_MS);
      }
      if (!decision) {
        if (!depositDecision(rid, "timeout", "plugin-timeout")) {
          decision = readJson(decisionPath);
        }
      }
      if (!decision || decision.outcome === "timeout" || decision.outcome === "fallback") {
        // The native ask flow takes over (remote timeout, or a client
        // re-attached and the broker closed the request as "resolved
        // elsewhere"): the prompt is published unchanged, and a later native
        // reply is recorded for dual-control.
        if (sessionID) {
          const pending = timedOutRequests.get(sessionID) || [];
          pending.push(rid);
          timedOutRequests.set(sessionID, pending);
        }
        return;
      }
      if (decision.outcome === "approve") {
        evaluation.effect = "allow";
        evaluation.message = "approved remotely via Telegram";
      } else if (decision.outcome === "deny") {
        evaluation.effect = "deny";
        evaluation.message = "denied remotely via Telegram";
      }
    };

    const handlePermissionReplied = (event) => {
      // Dual-control (task 3.5): the operator resolved the ask natively
      // (TUI). Relevant only for requests whose remote wait timed out —
      // a remote decision already answered the ask itself.
      const d = eventData(event);
      const sessionID = d.sessionID || d.request?.sessionID || null;
      if (!sessionID) return;
      const pending = timedOutRequests.get(sessionID);
      if (!pending || pending.length === 0) return;
      const rid = pending.shift();
      if (pending.length === 0) timedOutRequests.delete(sessionID);
      const decision = readJson(path.join(APPROVAL_DIR, "decisions", `${rid}.json`));
      if (decision?.outcome !== "timeout" && decision?.outcome !== "fallback") return;
      const dir = path.join(APPROVAL_DIR, "native");
      fs.mkdirSync(dir, { recursive: true });
      writeJsonAtomic(path.join(dir, `${rid}.json`), {
        id: rid,
        outcome: String(d.reply || d.response || d.decision || ""),
        ts: Date.now() / 1000,
      });
    };

    const todoSummary = (todos) => {
      const icons = { completed: "\u2714", in_progress: "\u25B8", pending: "\u00B7" };
      return (Array.isArray(todos) ? todos : [])
        .map((t) => `${icons[t?.status] || "\u00B7"} ${t?.content || ""}`)
        .join("\n");
    };

    // --- Registrations ----------------------------------------------------

    const onEvent = async (event) => {
      try {
        trackActivity(event);
        if (!telegramEnabled) return;
        const type = event?.type;
        const sessionID = sessionFromEvent(event);
        const text = textFromEvent(event);
        if (sessionID && text !== null && !IDLE_EVENTS.has(type)) {
          lastAssistantText.set(sessionID, text);
        } else if (type === "session.idle") {
          let final = lastAssistantText.get(sessionID) || "";
          if (!final && ctx.session?.context && sessionID) {
            final = await lastAssistantTextFromContext(ctx, sessionID);
          }
          lastAssistantText.delete(sessionID);
          spool({ type: "turn-end", payload: { text: final } });
        } else if (type === "permission.replied") {
          handlePermissionReplied(event);
        }
      } catch {
        /* best-effort by design */
      }
    };

    const onToolBefore = async (event) => {
      try {
        writeActivity(event?.sessionID, "busy");
        if (telegramEnabled && event?.tool === "todowrite") {
          const summary = todoSummary(event?.input?.todos ?? event?.args?.todos);
          if (summary) spool({ type: "todo", payload: { summary } });
        }
      } catch {
        /* best-effort by design */
      }
    };

    const onToolAfter = async (event) => {
      try {
        writeActivity(event?.sessionID, "busy");
      } catch {
        /* best-effort by design */
      }
    };

    if (ctx.tool?.hook) {
      await ctx.tool.hook("execute.before", onToolBefore);
      await ctx.tool.hook("execute.after", onToolAfter);
    }
    if (telegramEnabled && ctx.permission?.hook) {
      await ctx.permission.hook("evaluate", async (evaluation) => {
        try {
          await handlePermissionEvaluate(evaluation);
        } catch {
          /* best-effort by design: the native ask flow stays in charge */
        }
      });
    }

    const controller = new AbortController();
    if (ctx.event?.subscribe) {
      void (async () => {
        try {
          for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
            await onEvent(event);
          }
        } catch {
          /* stream closed (unload) or unavailable: markers degrade to tool hooks */
        }
      })();
    }

    return () => controller.abort();
  },
};
