/**
 * SCH Telegram plugin for OpenCode.
 *
 * Milestones (add-telegram-notifications): seeded by init-workspace.sh into
 * the global OpenCode config plugin dir ($XDG_CONFIG_HOME/opencode/plugin/).
 * Same contract as the Claude hook template (design D3/D4): milestone events
 * are written as JSON files into the local spool dir consumed by the shim's
 * notifier thread — the plugin NEVER talks to the network and is a complete
 * no-op when the shim's local notifier marker is absent.
 *
 * Remote approval (add-telegram-interaction, task 3.4): OpenCode 1.18 emits
 * `permission.asked` through the generic event hook. Its declared
 * `permission.ask` plugin hook is not invoked by the active permission engine.
 * When inbound interaction is configured, this plugin registers the request in the local
 * approval broker (requests/<id>.json), emits the permission-request
 * milestone with the request id (the shim attaches the Approve/Deny inline
 * keyboard), then waits for decisions/<id>.json with a bounded timeout.
 * approve/deny -> reply to OpenCode's native pending request through its API;
 * timeout/fallback -> no reply (the native ask flow stays fully in charge,
 * never auto-approval) and the timeout decision is deposited atomically so
 * a late remote decision finds the slot taken.
 *
 * Dual-control (task 3.5): a later `permission.replied` event for a request
 * that ended in timeout writes native/<id>.json — the shim updates the
 * Telegram message as "resolved elsewhere".
 *
 * Interactive keep-alive (add-interactive-busy-keepalive): the plugin also
 * maintains per-session activity markers in SCH_ACTIVITY_DIR so the shim's
 * busy-watcher can advertise HealthyBusy on /ping while ANY opencode process
 * (run TUI, `opencode web`, headless) has a turn in flight — otherwise
 * AgentCore idle-reaps the microVM mid-turn (~15 min without invocations).
 * This layer is ALWAYS active (independent of the Telegram opt-in gate):
 * marker files are local-disk only and never touch the network.
 */

export const SchTelegramPlugin = async ({ client } = {}) => {
  const fs = await import("node:fs");
  const path = await import("node:path");

  // --- Activity markers (add-interactive-busy-keepalive, always on) -------
  const ACTIVITY_DIR = process.env.SCH_ACTIVITY_DIR || "/tmp/sch-activity";
  // Streaming emits message.part.updated at high frequency: rate-limit the
  // busy writes per session. Idle/error transitions always write immediately.
  const ACTIVITY_THROTTLE_MS = parseInt(
    process.env.SCH_ACTIVITY_THROTTLE_MS || "5000", 10,
  );
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

  const sessionFromEvent = (event) => {
    const p = event?.properties || {};
    return (
      p.part?.sessionID || p.info?.sessionID || p.message?.sessionID ||
      p.sessionID || null
    );
  };

  const trackActivity = (event) => {
    const type = event?.type;
    if (type === "session.idle") {
      writeActivity(sessionFromEvent(event), "idle");
    } else if (type === "session.error") {
      // Aborted turn: release promptly instead of waiting for staleness.
      writeActivity(sessionFromEvent(event), "idle");
    } else if (
      type === "message.updated" || type === "message.part.updated"
    ) {
      writeActivity(sessionFromEvent(event), "busy");
    }
  };

  const ENABLED_MARKER = process.env.SCH_TELEGRAM_ENABLED_MARKER || "/tmp/sch-telegram-enabled";
  const PRESENCE_SNAPSHOT = process.env.SCH_COMMAND_SHELL_PRESENCE_FILE || "/tmp/sch-command-shell-presence.json";
  const EXECUTION_MODE = process.env.SCH_EXECUTION_MODE === "headless" ? "headless" : "interactive";

  if (!fs.existsSync(ENABLED_MARKER)) {
    // Telegram opt-in gate: without config only the (network-free) activity
    // marker hooks are registered.
    return {
      event: async ({ event }) => {
        try {
          trackActivity(event);
        } catch {
          /* best-effort by design */
        }
      },
      "tool.execute.before": async (input) => {
        try {
          writeActivity(input?.sessionID, "busy");
        } catch {
          /* best-effort by design */
        }
      },
      "tool.execute.after": async (input) => {
        try {
          writeActivity(input?.sessionID, "busy");
        } catch {
          /* best-effort by design */
        }
      },
    };
  }

  const crypto = await import("node:crypto");
  const SPOOL_DIR = process.env.SCH_TELEGRAM_SPOOL_DIR || "/tmp/sch-telegram-spool";
  const APPROVAL_DIR = process.env.SCH_APPROVAL_DIR || "/tmp/sch-approval";
  // OQ-I1: remote wait default 10 minutes (design D4).
  const APPROVAL_TIMEOUT_S = parseInt(process.env.SCH_APPROVAL_TIMEOUT_S || "600", 10);
  const DECISION_POLL_MS = 500;

  const readJson = (file) => {
    try {
      const data = JSON.parse(fs.readFileSync(file, "utf-8"));
      return data && typeof data === "object" ? data : null;
    } catch {
      return null;
    }
  };

  const markerMetadata = readJson(ENABLED_MARKER) || {};
  // The marker exposes capability, not Telegram configuration or credentials.
  const INTERACTION_ENABLED = markerMetadata.interaction_enabled === true;

  // sessionID -> latest assistant text part observed (the message parts
  // stream carries the full accumulated text, so the last update wins).
  const lastAssistantText = new Map();
  // opencode permissionID -> broker request id (dual-control bookkeeping).
  const permissionRequests = new Map();
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

  const writeJsonAtomic = (file, payload) => {
    const tmp = `${file}.tmp-${process.pid}-${crypto.randomBytes(4).toString("hex")}`;
    fs.writeFileSync(tmp, JSON.stringify(payload));
    fs.renameSync(tmp, file);
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

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const replyPermission = async (request, response) => {
    if (!client || !request?.id || !request?.sessionID) return false;
    if (client.permission?.reply) {
      await client.permission.reply({ requestID: request.id, reply: response });
      return true;
    }
    if (client.postSessionIdPermissionsPermissionId) {
      await client.postSessionIdPermissionsPermissionId({
        path: { id: request.sessionID, permissionID: request.id },
        body: { response },
      });
      return true;
    }
    return false;
  };

  const handlePermissionAsked = async (request) => {
    if (EXECUTION_MODE === "headless" || locallyAttached()) return;
    const tool = request?.permission || request?.title || request?.type || "tool";
    const patterns = Array.isArray(request?.patterns)
      ? request.patterns
      : Array.isArray(request?.pattern)
        ? request.pattern
        : request?.pattern ? [request.pattern] : [];
    const detail = patterns.join(" · ") || String(request?.metadata?.command || "");
    if (!INTERACTION_ENABLED) {
      spool({ type: "permission-request", payload: { tool, detail } });
      return;
    }
    const rid = String(request?.id || "");
    if (!rid) return;
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
    permissionRequests.set(rid, rid);
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
      if (!decision) return;
    }
    if (decision.outcome === "approve") await replyPermission(request, "once");
    else if (decision.outcome === "deny") await replyPermission(request, "reject");
  };

  const todoSummary = (todos) => {
    const icons = { completed: "\u2714", in_progress: "\u25B8", pending: "\u00B7" };
    return (Array.isArray(todos) ? todos : [])
      .map((t) => `${icons[t?.status] || "\u00B7"} ${t?.content || ""}`)
      .join("\n");
  };

  return {
    event: async ({ event }) => {
      try {
        trackActivity(event);
        if (event?.type === "message.part.updated") {
          const part = event.properties?.part;
          if (part?.type === "text" && part.sessionID && typeof part.text === "string") {
            lastAssistantText.set(part.sessionID, part.text);
          }
        } else if (event?.type === "session.idle") {
          const sessionID = event.properties?.sessionID;
          spool({
            type: "turn-end",
            payload: { text: lastAssistantText.get(sessionID) || "" },
          });
        } else if (event?.type === "permission.asked") {
          await handlePermissionAsked(event.properties || {});
        } else if (
          event?.type === "permission.replied" ||
          event?.type === "permission.updated"
        ) {
          // Dual-control (task 3.5): the operator resolved the ask natively
          // (TUI). Relevant only for requests whose remote wait timed out —
          // a remote decision already answered the ask itself.
          const permissionID =
            event.properties?.requestID || event.properties?.permissionID || event.properties?.id;
          const rid = permissionID ? permissionRequests.get(permissionID) : null;
          if (rid) {
            const decision = readJson(path.join(APPROVAL_DIR, "decisions", `${rid}.json`));
            if (decision?.outcome === "timeout") {
              const dir = path.join(APPROVAL_DIR, "native");
              fs.mkdirSync(dir, { recursive: true });
              writeJsonAtomic(path.join(dir, `${rid}.json`), {
                id: rid,
                outcome: String(event.properties?.reply || event.properties?.response || ""),
                ts: Date.now() / 1000,
              });
            }
            permissionRequests.delete(permissionID);
          }
        }
      } catch {
        /* best-effort by design */
      }
    },
    "tool.execute.before": async (input, output) => {
      try {
        writeActivity(input?.sessionID, "busy");
        if (input?.tool === "todowrite") {
          const summary = todoSummary(output?.args?.todos);
          if (summary) spool({ type: "todo", payload: { summary } });
        }
      } catch {
        /* best-effort by design */
      }
    },
    "tool.execute.after": async (input) => {
      try {
        writeActivity(input?.sessionID, "busy");
      } catch {
        /* best-effort by design */
      }
    },
  };
};
