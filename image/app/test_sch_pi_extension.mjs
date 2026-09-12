// Offline tests for the SCH Pi extension (add-pi-harness, task 3.6).
// Run with: node --test image/app/test_sch_pi_extension.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const EXTENSION_PATH = new URL(
  "../pi-templates/extensions/sch-pi.ts",
  import.meta.url,
).href;

let importSeq = 0;
const TELEGRAM_ENV = [
  "SCH_TELEGRAM_BOT_TOKEN",
  "SCH_TELEGRAM_CHAT_ID",
  "SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED",
];

function applyEnv(env) {
  for (const name of TELEGRAM_ENV) delete process.env[name];
  Object.assign(process.env, env);
}

async function loadExtension(env = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "sch-pi-ext-test-"));
  const spool = path.join(tmp, "spool");
  const activity = path.join(tmp, "activity");
  const marker = path.join(tmp, "telegram-enabled");
  const saved = { ...process.env };
  const loadEnv = {
    SCH_TELEGRAM_SPOOL_DIR: spool,
    SCH_ACTIVITY_DIR: activity,
    SCH_TELEGRAM_ENABLED_MARKER: marker,
    SCH_ACTIVITY_THROTTLE_MS: "1",
    ...env,
  };
  if (
    (env.SCH_TELEGRAM_BOT_TOKEN && env.SCH_TELEGRAM_CHAT_ID) ||
    env.SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED === "1"
  ) {
    fs.writeFileSync(marker, "{}");
  }
  applyEnv(loadEnv);
  try {
    const mod = await import(`${EXTENSION_PATH}?v=${importSeq++}`);
    const handlers = {};
    mod.default({
      on(event, handler) {
        handlers[event] = handler;
      },
    });
    const run = async (event, ...args) => {
      const outer = { ...process.env };
      applyEnv(loadEnv);
      try {
        return await handlers[event](...args);
      } finally {
        process.env = outer;
      }
    };
    return { handlers, run, spool, activity };
  } finally {
    process.env = saved;
  }
}

function spoolEvents(spool) {
  if (!fs.existsSync(spool)) return [];
  return fs.readdirSync(spool)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => JSON.parse(fs.readFileSync(path.join(spool, name), "utf-8")));
}

function activityRecords(activity) {
  if (!fs.existsSync(activity)) return {};
  return Object.fromEntries(
    fs.readdirSync(activity)
      .filter((name) => name.endsWith(".json"))
      .map((name) => [
        name.replace(/\.json$/, ""),
        JSON.parse(fs.readFileSync(path.join(activity, name), "utf-8")),
      ]),
  );
}

const CONFIGURED = {
  SCH_TELEGRAM_BOT_TOKEN: "tok",
  SCH_TELEGRAM_CHAT_ID: "-1",
};
const CTX = { sessionManager: { getSessionId: () => "ses1" } };

test("extension never registers a decision hook", async () => {
  const { handlers } = await loadExtension({
    ...CONFIGURED,
    SCH_TELEGRAM_COMMANDS_TABLE: "commands",
  });
  assert.equal(handlers.tool_call, undefined);
});

test("end-of-turn publishes the conclusive assistant text once per settle", async () => {
  const { run, spool } = await loadExtension(CONFIGURED);
  await run("turn_end", {
    message: { role: "assistant", content: [{ type: "toolCall" }] },
  }, CTX);
  await run("turn_end", {
    message: { role: "assistant", content: [{ type: "text", text: "done, all green" }] },
  }, CTX);
  await run("agent_settled", {}, CTX);

  const events = spoolEvents(spool);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "turn-end");
  assert.equal(events[0].source, "pi");
  assert.equal(events[0].payload.text, "done, all green");
});

test("the accumulated text is reset between prompts", async () => {
  const { run, spool } = await loadExtension(CONFIGURED);
  await run("turn_end", {
    message: { content: [{ type: "text", text: "first" }] },
  }, CTX);
  await run("agent_settled", {}, CTX);
  await run("turn_end", { message: { content: [{ type: "toolCall" }] } }, CTX);
  await run("agent_settled", {}, CTX);

  const events = spoolEvents(spool);
  assert.deepEqual(events.map((event) => event.payload.text), ["first", ""]);
});

test("login-shell capability flag enables milestones without Telegram secrets", async () => {
  const { run, spool } = await loadExtension({
    SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED: "1",
  });
  await run("turn_end", {
    message: { content: [{ type: "text", text: "bridged" }] },
  }, CTX);
  await run("agent_settled", {}, CTX);
  assert.equal(spoolEvents(spool)[0].payload.text, "bridged");
});

test("without Telegram config no milestone reaches the spool", async () => {
  const { run, spool } = await loadExtension();
  await run("turn_end", {
    message: { content: [{ type: "text", text: "quiet" }] },
  }, CTX);
  await run("agent_settled", {}, CTX);
  assert.deepEqual(spoolEvents(spool), []);
});

test("activity markers are busy during a turn and idle when settled", async () => {
  const { run, activity } = await loadExtension();
  await run("session_start", {}, CTX);
  await run("turn_start", {}, CTX);
  assert.equal(activityRecords(activity).ses1.state, "busy");
  await run("agent_settled", {}, CTX);
  assert.equal(activityRecords(activity).ses1.state, "idle");
});

test("activity markers survive a session manager without an id", async () => {
  const { run, activity } = await loadExtension();
  await run("session_start", {}, {});
  await run("turn_start", {}, {});
  const records = activityRecords(activity);
  const ids = Object.keys(records);
  assert.equal(ids.length, 1);
  assert.match(ids[0], /^pid-\d+$/);
  assert.equal(records[ids[0]].state, "busy");
});

test("session shutdown releases the keep-alive", async () => {
  const { run, activity } = await loadExtension();
  await run("session_start", {}, CTX);
  await run("turn_start", {}, CTX);
  await run("session_shutdown", {}, CTX);
  assert.equal(activityRecords(activity).ses1.state, "idle");
});
