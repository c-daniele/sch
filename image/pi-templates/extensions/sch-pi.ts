/**
 * SCH extension for Pi (add-pi-harness, design D6/D7).
 *
 * seeded by init-workspace.sh into the global Pi extensions dir
 * ($PI_CODING_AGENT_DIR/extensions/), auto-discovered by Pi at startup.
 * One artifact, two functions — the Pi counterpart of the opencode plugin
 * (sch-telegram.js) and the claude hooks (telegram-hook.py):
 *
 *  1. Telegram milestones (add-telegram-notifications): end-of-turn events are
 *     written as JSON files into the local spool dir consumed by the shim's
 *     notifier thread. NEVER touches the network, never blocks the turn, and
 *     is a complete no-op when the shim's local notifier marker is absent.
 *     Coverage on Pi (OQ-PI-EVENTS, resolved empirically against 0.84.2):
 *     end-of-turn is the guaranteed minimum. Pi has no todo tool and no
 *     "waiting for input" hook, so those milestone categories degrade
 *     silently (existing contract, spec: telegram-notifications).
 *
 *  2. Busy keep-alive (add-interactive-busy-keepalive): per-session activity
 *     markers under SCH_ACTIVITY_DIR, same file contract as the opencode
 *     plugin, so the shim's busy-watcher can advertise HealthyBusy while an
 *     interactive Pi turn is in flight instead of letting AgentCore idle-reap
 *     the microVM mid-turn. ALWAYS active (independent of the Telegram opt-in):
 *     marker files are local-disk only and never touch the network.
 *
 * Never-fail contract: every handler swallows its own errors. A broken
 * integration must degrade to lifecycle events only, never break a turn.
 *
 * Syntax note: this file is loaded through jiti (no compile step) and is also
 * import-checked at image build time by Node's native type stripping, so it
 * must stay within erasable TypeScript (type annotations only — no enums, no
 * parameter properties, no namespaces).
 */

import * as fs from "node:fs";
import * as path from "node:path";

// --- Configuration (all optional; absent config = degraded, never broken) ----

const SPOOL_DIR = process.env.SCH_TELEGRAM_SPOOL_DIR || "/tmp/sch-telegram-spool";
const ENABLED_MARKER = process.env.SCH_TELEGRAM_ENABLED_MARKER || "/tmp/sch-telegram-enabled";
const EXECUTION_MODE = process.env.SCH_EXECUTION_MODE === "headless" ? "headless" : "interactive";
const ACTIVITY_DIR = process.env.SCH_ACTIVITY_DIR || "/tmp/sch-activity";
// Streaming emits message_update at high frequency: rate-limit the busy writes
// per session. Idle transitions always write immediately.
const ACTIVITY_THROTTLE_MS = parseIntOr(process.env.SCH_ACTIVITY_THROTTLE_MS, 5000);

function parseIntOr(raw: string | undefined, fallback: number): number {
  const value = parseInt(String(raw ?? ""), 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Local notifier available? Gates the milestone spool without credentials. */
function notificationsConfigured(): boolean {
  return fs.existsSync(ENABLED_MARKER);
}

// --- Local file protocol helpers (best-effort by design) ---------------------

export default function (pi: any) {
  let spoolSeq = 0;
  const lastBusyWrite = new Map<string, number>();
  // Accumulated assistant text of the current agent run: turn_end fires once
  // per LLM turn (tool-only turns carry no text), so the LAST non-empty text
  // is the conclusive assistant message published on agent_settled — one
  // end-of-turn milestone per prompt, matching opencode's session.idle.
  let pendingTurnText = "";
  let currentSessionId: string | null = null;

  const spool = (event: Record<string, unknown>): boolean => {
    // Never-fail contract: a broken milestone must not degrade the turn.
    try {
      fs.mkdirSync(SPOOL_DIR, { recursive: true });
      event.ts = Date.now() / 1000;
      event.source = "pi";
      event.execution_mode = EXECUTION_MODE;
      const name = `${Date.now()}${String(spoolSeq++).padStart(4, "0")}-${process.pid}.json`;
      const tmp = path.join(SPOOL_DIR, `${name}.tmp`);
      // tmp-then-rename so the notifier's *.json poll never sees a partial file.
      fs.writeFileSync(tmp, JSON.stringify(event));
      fs.renameSync(tmp, path.join(SPOOL_DIR, name));
      return true;
    } catch {
      return false;
    }
  };

  const writeActivity = (state: string) => {
    try {
      const sid = String(currentSessionId || `pid-${process.pid}`).replace(
        /[^A-Za-z0-9._-]/g,
        "_",
      );
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
      fs.writeFileSync(
        tmp,
        JSON.stringify({
          sessionID: sid,
          state,
          ts: Date.now() / 1000,
          pid: process.pid,
        }),
      );
      fs.renameSync(tmp, file);
    } catch {
      /* best-effort by design */
    }
  };

  const sessionIdOf = (ctx: any): string | null => {
    try {
      const sm = ctx?.sessionManager;
      if (sm && typeof sm.getSessionId === "function") {
        const sid = sm.getSessionId();
        if (sid) return String(sid);
      }
      if (sm && typeof sm.getSessionFile === "function") {
        const file = sm.getSessionFile();
        if (file) return path.basename(String(file)).replace(/\.jsonl$/, "");
      }
    } catch {
      /* fall through to the pid-based id */
    }
    return null;
  };

  // --- 2. Busy keep-alive (always on) ---------------------------------------

  pi.on("session_start", async (_event: any, ctx: any) => {
    try {
      currentSessionId = sessionIdOf(ctx);
    } catch {
      /* best-effort by design */
    }
  });

  pi.on("turn_start", async (_event: any, _ctx: any) => {
    writeActivity("busy");
  });

  pi.on("message_update", async (_event: any, _ctx: any) => {
    writeActivity("busy");
  });

  pi.on("tool_execution_start", async (_event: any, _ctx: any) => {
    writeActivity("busy");
  });

  pi.on("tool_execution_end", async (_event: any, _ctx: any) => {
    writeActivity("busy");
  });

  pi.on("session_shutdown", async (_event: any, _ctx: any) => {
    writeActivity("idle");
  });

  // --- 1. Milestones: end-of-turn ------------------------------------------

  pi.on("turn_end", async (event: any, _ctx: any) => {
    try {
      const content = event?.message?.content;
      if (Array.isArray(content)) {
        const text = content
          .filter((part: any) => part?.type === "text" && typeof part.text === "string")
          .map((part: any) => part.text)
          .join("");
        if (text.trim()) pendingTurnText = text;
      }
    } catch {
      /* best-effort by design */
    }
  });

  pi.on("agent_settled", async (_event: any, _ctx: any) => {
    // Pi will not continue automatically from here (no retry, no compaction
    // retry, no queued follow-up): this is the end of the operator-visible
    // turn. Release the keep-alive first, then publish the milestone.
    writeActivity("idle");
    try {
      if (notificationsConfigured()) {
        spool({ type: "turn-end", payload: { text: pendingTurnText } });
      }
      pendingTurnText = "";
    } catch {
      /* best-effort by design */
    }
  });
}
