// Tests for the DECISIONAL OpenCode permission event handler
// (add-telegram-interaction, task 5.2). Run with:
//   node --test image/app/test_sch_telegram_plugin.mjs
//
// Covers: approve/deny replies through the injected client, fail-safe timeout,
// observational fallback without the inbound channel, full no-op without
// Telegram config, and the dual-control native marker on permission.replied.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const PLUGIN_PATH = new URL(
  "../opencode-templates/plugin/sch-telegram.js",
  import.meta.url,
).href;

let importSeq = 0;

async function loadPlugin(env, client) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sch-plugin-test-"));
  const approval = path.join(tmp, "approval");
  const spool = path.join(tmp, "spool");
  const activity = path.join(tmp, "activity");
  const presence = path.join(tmp, "presence.json");
  const marker = path.join(tmp, "telegram-enabled");
  const saved = { ...process.env };
  delete process.env.SCH_TELEGRAM_BOT_TOKEN;
  delete process.env.SCH_TELEGRAM_CHAT_ID;
  delete process.env.SCH_TELEGRAM_COMMANDS_TABLE;
  Object.assign(process.env, {
    SCH_TELEGRAM_SPOOL_DIR: spool,
    SCH_APPROVAL_DIR: approval,
    SCH_ACTIVITY_DIR: activity,
    SCH_COMMAND_SHELL_PRESENCE_FILE: presence,
    SCH_TELEGRAM_ENABLED_MARKER: marker,
    SCH_ACTIVITY_THROTTLE_MS: "0",
    ...env,
  });
  if (env.SCH_TELEGRAM_BOT_TOKEN && env.SCH_TELEGRAM_CHAT_ID) {
    fs.writeFileSync(marker, JSON.stringify({
      interaction_enabled: Boolean(env.SCH_TELEGRAM_COMMANDS_TABLE),
    }));
  }
  try {
    // Fresh module instance per test (the factory reads env at call time,
    // but a unique query string guards against module-level caching).
    const { SchTelegramPlugin } = await import(`${PLUGIN_PATH}?v=${importSeq++}`);
    const hooks = await SchTelegramPlugin({ client });
    return { hooks, approval, spool, activity, presence, tmp };
  } finally {
    process.env = saved;
  }
}

const configured = {
  SCH_TELEGRAM_BOT_TOKEN: "tok",
  SCH_TELEGRAM_CHAT_ID: "-1",
  SCH_TELEGRAM_COMMANDS_TABLE: "commands",
  SCH_APPROVAL_TIMEOUT_S: "5",
};

function spoolEvents(spool) {
  if (!fs.existsSync(spool)) return [];
  return fs
    .readdirSync(spool)
    .sort()
    .map((name) => JSON.parse(fs.readFileSync(path.join(spool, name), "utf-8")));
}

function depositWhenRequested(approval, outcome) {
  const requests = path.join(approval, "requests");
  const interval = setInterval(() => {
    if (!fs.existsSync(requests)) return;
    const files = fs.readdirSync(requests).filter((n) => n.endsWith(".json"));
    if (!files.length) return;
    const rid = files[0].replace(/\.json$/, "");
    const decisions = path.join(approval, "decisions");
    fs.mkdirSync(decisions, { recursive: true });
    fs.writeFileSync(
      path.join(decisions, `${rid}.json`),
      JSON.stringify({ id: rid, outcome, source: "telegram" }),
    );
    clearInterval(interval);
  }, 50);
  return interval;
}

function activityRecords(activity) {
  if (!fs.existsSync(activity)) return {};
  const records = {};
  for (const name of fs.readdirSync(activity).sort()) {
    records[name.replace(/\.json$/, "")] = JSON.parse(
      fs.readFileSync(path.join(activity, name), "utf-8"),
    );
  }
  return records;
}

function recordingClient() {
  const replies = [];
  return {
    replies,
    client: {
      permission: {
        reply: async (input) => replies.push(input),
      },
    },
  };
}

async function permissionAsked(hooks, properties = {}) {
  await hooks.event({
    event: {
      type: "permission.asked",
      properties: {
        id: "permission-1",
        sessionID: "session-1",
        permission: "bash",
        patterns: ["ls *"],
        metadata: { command: "ls -la" },
        ...properties,
      },
    },
  });
}

test("without Telegram config only the activity hooks are registered", async () => {
  const { hooks, spool, activity } = await loadPlugin({});
  assert.equal(hooks["permission.ask"], undefined);
  assert.ok(hooks.event);
  assert.ok(hooks["tool.execute.before"]);
  await hooks.event({
    event: {
      type: "message.part.updated",
      properties: { part: { type: "text", sessionID: "ses1", text: "hi" } },
    },
  });
  // Activity marker written, but nothing ever reaches the Telegram spool.
  assert.equal(activityRecords(activity).ses1.state, "busy");
  assert.equal(fs.existsSync(spool), false);
});

test("configured plugin does not expose the dead permission.ask hook", async () => {
  const { hooks } = await loadPlugin(configured);
  assert.equal(hooks["permission.ask"], undefined);
});

test("activity markers: busy on part update / tool, idle on session.idle", async () => {
  const { hooks, activity } = await loadPlugin(configured);
  await hooks.event({
    event: {
      type: "message.part.updated",
      properties: { part: { type: "text", sessionID: "sesA", text: "a" } },
    },
  });
  assert.equal(activityRecords(activity).sesA.state, "busy");
  await hooks["tool.execute.after"]({ tool: "bash", sessionID: "sesA" });
  assert.equal(activityRecords(activity).sesA.state, "busy");
  await hooks.event({
    event: { type: "session.idle", properties: { sessionID: "sesA" } },
  });
  assert.equal(activityRecords(activity).sesA.state, "idle");
});

test("session.error also marks the session idle", async () => {
  const { hooks, activity } = await loadPlugin({});
  await hooks.event({
    event: {
      type: "message.updated",
      properties: { info: { sessionID: "sesE" } },
    },
  });
  assert.equal(activityRecords(activity).sesE.state, "busy");
  await hooks.event({
    event: { type: "session.error", properties: { sessionID: "sesE" } },
  });
  assert.equal(activityRecords(activity).sesE.state, "idle");
});

test("busy writes are throttled per session", async () => {
  const { hooks, activity } = await loadPlugin({
    SCH_ACTIVITY_THROTTLE_MS: "60000",
  });
  await hooks.event({
    event: {
      type: "message.part.updated",
      properties: { part: { type: "text", sessionID: "sesT", text: "1" } },
    },
  });
  const first = activityRecords(activity).sesT.ts;
  await hooks.event({
    event: {
      type: "message.part.updated",
      properties: { part: { type: "text", sessionID: "sesT", text: "2" } },
    },
  });
  // Second write suppressed by the throttle window.
  assert.equal(activityRecords(activity).sesT.ts, first);
  // Idle bypasses the throttle.
  await hooks.event({
    event: { type: "session.idle", properties: { sessionID: "sesT" } },
  });
  assert.equal(activityRecords(activity).sesT.state, "idle");
});

test("observational without the inbound channel", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval, spool } = await loadPlugin({
    SCH_TELEGRAM_BOT_TOKEN: "tok",
    SCH_TELEGRAM_CHAT_ID: "-1",
  }, client);
  await permissionAsked(hooks, {
    id: "p1",
    permission: "bash",
    patterns: ["ls *"],
  });
  assert.deepEqual(replies, []);
  assert.equal(fs.existsSync(approval), false);
  const events = spoolEvents(spool);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "permission-request");
  assert.equal(events[0].payload.request_id, undefined);
  assert.equal(events[0].payload.tool, "bash");
  assert.equal(events[0].payload.detail, "ls *");
});

test("attached session creates no approval broker or spool event", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval, spool, presence } = await loadPlugin(configured, client);
  fs.writeFileSync(presence, JSON.stringify({
    version: 1,
    state: "attached",
    expires_at: 4102444800,
    leases: [{
      shellId: "shell-1",
      attachmentId: "client-1",
      attached_at: 1,
      expires_at: 4102444800,
    }],
  }));
  await permissionAsked(hooks, { id: "p-attached" });
  assert.deepEqual(replies, []);
  assert.equal(fs.existsSync(approval), false);
  assert.equal(fs.existsSync(spool), false);
});

test("approve decision replies once through the current client API", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval, spool } = await loadPlugin(configured, client);
  const timer = depositWhenRequested(approval, "approve");
  await permissionAsked(hooks, { id: "p2", patterns: ["ls *"] });
  clearInterval(timer);
  assert.deepEqual(replies, [{ requestID: "p2", reply: "once" }]);
  const events = spoolEvents(spool);
  assert.equal(events[0].type, "permission-request");
  assert.equal(events[0].payload.request_id, "p2");
  assert.equal(events[0].payload.detail, "ls *");
});

test("deny decision replies reject through the current client API", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval } = await loadPlugin(configured, client);
  const timer = depositWhenRequested(approval, "deny");
  await permissionAsked(hooks, { id: "p3", patterns: ["rm -rf *"] });
  clearInterval(timer);
  assert.deepEqual(replies, [{ requestID: "p3", reply: "reject" }]);
});

test("timeout does not reply and claims the decision slot", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval } = await loadPlugin({
    ...configured,
    SCH_TELEGRAM_COMMANDS_TABLE: "commands",
    SCH_APPROVAL_TIMEOUT_S: "0",
  }, client);
  await permissionAsked(hooks, { id: "p4" });
  assert.deepEqual(replies, []);
  const decisions = fs.readdirSync(path.join(approval, "decisions"));
  assert.equal(decisions.length, 1);
  const decision = JSON.parse(
    fs.readFileSync(path.join(approval, "decisions", decisions[0]), "utf-8"),
  );
  assert.equal(decision.outcome, "timeout");
});

test("pre-resolved timeout decision does not reply", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval } = await loadPlugin(configured, client);
  const timer = depositWhenRequested(approval, "timeout");
  await permissionAsked(hooks, { id: "p5" });
  clearInterval(timer);
  assert.deepEqual(replies, []);
});

test("presence fallback returns to native without waiting for timeout", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval } = await loadPlugin({
    ...configured,
    SCH_APPROVAL_TIMEOUT_S: "30",
  }, client);
  const timer = depositWhenRequested(approval, "fallback");
  const started = Date.now();
  await permissionAsked(hooks, { id: "p-fallback" });
  clearInterval(timer);
  assert.ok(Date.now() - started < 5000);
  assert.deepEqual(replies, []);
});

test("dual-control uses requestID/reply to write the native marker", async () => {
  const { client, replies } = recordingClient();
  const { hooks, approval } = await loadPlugin({
    ...configured,
    SCH_APPROVAL_TIMEOUT_S: "0",
  }, client);
  await permissionAsked(hooks, { id: "perm-9" });
  assert.deepEqual(replies, []);
  // The remote wait timed out; the operator then replies from the TUI.
  await hooks.event({
    event: {
      type: "permission.replied",
      properties: { requestID: "perm-9", reply: "once" },
    },
  });
  const nativeDir = path.join(approval, "native");
  const markers = fs.readdirSync(nativeDir);
  assert.equal(markers.length, 1);
  const marker = JSON.parse(
    fs.readFileSync(path.join(nativeDir, markers[0]), "utf-8"),
  );
  assert.equal(marker.outcome, "once");
});
