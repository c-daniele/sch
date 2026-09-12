#!/usr/bin/env node
// fake-zed.js — minimal ACP client replicating the contract Zed exercises
// against an agent server (sch-acp-editor-integration, design D5; task 6.1).
// Used by bin/verify-acp-editor.sh (and runnable by hand) to drive
// `sch acp <ws>` — or any ACP agent command — WITHOUT the Zed GUI:
//
//   initialize            advertising fs + terminal client capabilities
//                         (like Zed does), so the sch bridge's capability
//                         neutralization is actually exercised;
//   session/new           with the local project cwd (the mirror dir);
//   session/prompt        text prompt, optionally with a resource_link
//                         mention (--mention <abs file>) like Zed's @file;
//   session/update        notifications collected; permission requests
//                         auto-granted (picks the first allow-ish option).
//
// It records every agent->client REQUEST — through the sch bridge there
// must be NO fs/* or terminal/* (design D3) — and every tool_call
// location path. A JSON report goes to stdout as the LAST line; asserts
// are evaluated by the caller and/or via the built-in flags:
//
//   --assert-no-client-fs          exit 5 if any fs/* or terminal/* request
//                                  reached this client
//   --assert-locations-under <p>   exit 6 if a tool_call location falls
//                                  outside prefix <p> (path locality)
//
// Usage:
//   node test/fake-zed.js --cmd "<agent command>" --cwd <project-dir>
//        [--prompt "<text>"] [--mention <abs-file>] [--timeout <s>]
//        [--between "<shell cmd>"] [--settle <s>] [--prompt2 "<text>"]
//        [--linger <s>]
//        [--assert-no-client-fs] [--assert-locations-under <prefix>]
//
//   --between/--settle/--prompt2: after the first prompt turn, run the
//     shell command (e.g. write a file into the mirror like an operator
//     save), wait <settle> seconds (default 5) for the sync to propagate,
//     then send the second prompt. Enables the "salvataggio locale
//     visibile a un prompt successivo" and LWW-conflict scenarios without
//     a GUI.
//   --linger: keep the session (and thus the sync channel) open N seconds
//     after the last turn before exiting, so in-flight file events settle.
//
// Exit codes: 0 ok; 2 usage; 3 timeout; 4 protocol failure; 5/6 asserts.
// Diagnostics on stderr; stdout carries ONLY the final JSON report line.

import { spawn, execSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { resolve } from 'node:path';

function parseArgs(argv) {
  const out = {
    timeout: 300, prompt: null, mention: null, asserts: {}, linger: 0, between: null, settle: 5, prompt2: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--cmd') out.cmd = argv[++i];
    else if (a === '--cwd') out.cwd = argv[++i];
    else if (a === '--prompt') out.prompt = argv[++i];
    else if (a === '--mention') out.mention = argv[++i];
    else if (a === '--timeout') out.timeout = parseInt(argv[++i], 10);
    else if (a === '--between') out.between = argv[++i];
    else if (a === '--settle') out.settle = parseInt(argv[++i], 10);
    else if (a === '--prompt2') out.prompt2 = argv[++i];
    else if (a === '--linger') out.linger = parseInt(argv[++i], 10);
    else if (a === '--assert-no-client-fs') out.asserts.noClientFs = true;
    else if (a === '--assert-locations-under') out.asserts.locationsUnder = argv[++i];
    else {
      process.stderr.write(`fake-zed: unknown arg '${a}'\n`);
      process.exit(2);
    }
  }
  if (!out.cmd || !out.cwd) {
    process.stderr.write('usage: fake-zed.js --cmd "<agent command>" --cwd <dir> [--prompt <text>] [--mention <abs file>] [--between <sh>] [--settle <s>] [--prompt2 <text>] [--linger <s>] [--timeout <s>] [--assert-no-client-fs] [--assert-locations-under <prefix>]\n');
    process.exit(2);
  }
  return out;
}

const opts = parseArgs(process.argv.slice(2));
const report = {
  ok: false,
  initialize: null,
  sessionId: null,
  stopReason: null,
  stopReason2: null,
  clientRequests: [],   // every agent->client request method
  fsCalls: [],          // fs/* and terminal/* (must stay empty through sch)
  locations: [],        // every tool_call/tool_call_update location path
  diffPaths: [],        // every diff content path
  agentText: '',        // concatenated agent_message_chunk text (turn 1)
  agentText2: '',       // idem, turn 2 (--prompt2)
  updates: 0,
  permissionsGranted: 0,
  errors: [],
};
let textSink = 'agentText';

const child = spawn('/bin/sh', ['-c', opts.cmd], { stdio: ['pipe', 'pipe', 'pipe'], detached: true });
child.stderr.on('data', (b) => process.stderr.write(`[agent-stderr] ${b}`));
child.on('exit', (code, sig) => {
  if (!done) {
    report.errors.push(`agent exited early (code=${code} sig=${sig})`);
    finish(4);
  }
});

let nextId = 0;
const pending = new Map();
let done = false;

function send(msg) {
  child.stdin.write(`${JSON.stringify(msg)}\n`);
}
function request(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, method });
    send({ jsonrpc: '2.0', id, method, params });
  });
}

let buf = '';
child.stdout.on('data', (chunk) => {
  buf += chunk.toString('utf8');
  let nl;
  while ((nl = buf.indexOf('\n')) !== -1) {
    const line = buf.slice(0, nl);
    buf = buf.slice(nl + 1);
    if (!line.trim()) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      report.errors.push(`non-JSON line on agent stdout: ${line.slice(0, 160)}`);
      continue;
    }
    handle(msg);
  }
});

function collectContentBlock(block) {
  if (!block || typeof block !== 'object') return;
  if (block.type === 'text' && typeof block.text === 'string') report[textSink] += block.text;
}

function collectToolCallLike(u) {
  for (const loc of u?.locations ?? []) {
    if (loc && typeof loc.path === 'string') report.locations.push(loc.path);
  }
  for (const item of u?.content ?? []) {
    if (item && item.type === 'diff' && typeof item.path === 'string') report.diffPaths.push(item.path);
  }
}

function handle(msg) {
  // responses to our requests
  if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined) && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(`${p.method}: JSON-RPC error ${msg.error.code}: ${msg.error.message}`));
    else p.resolve(msg.result);
    return;
  }
  // agent -> client requests
  if (msg.method && msg.id !== undefined) {
    report.clientRequests.push(msg.method);
    if (msg.method.startsWith('fs/') || msg.method.startsWith('terminal/')) {
      report.fsCalls.push({ method: msg.method, params: msg.params });
      process.stderr.write(`fake-zed: !!! agent asked the CLIENT for ${msg.method} (should be blocked by the sch bridge)\n`);
      send({ jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: 'fake-zed: client fs/terminal not served in this test' } });
      return;
    }
    if (msg.method === 'session/request_permission') {
      collectToolCallLike(msg.params?.toolCall);
      const options = msg.params?.options ?? [];
      const allow = options.find((o) => /allow/i.test(o.kind ?? '')) ?? options[0];
      report.permissionsGranted += 1;
      process.stderr.write(`fake-zed: permission '${msg.params?.toolCall?.title ?? '?'}' -> granting '${allow?.name ?? allow?.optionId}'\n`);
      send({ jsonrpc: '2.0', id: msg.id, result: { outcome: { outcome: 'selected', optionId: allow?.optionId } } });
      return;
    }
    send({ jsonrpc: '2.0', id: msg.id, result: null }); // benign default
    return;
  }
  // notifications
  if (msg.method === 'session/update') {
    report.updates += 1;
    const u = msg.params?.update;
    if (!u) return;
    if (u.sessionUpdate === 'tool_call' || u.sessionUpdate === 'tool_call_update') collectToolCallLike(u);
    else if (u.sessionUpdate === 'agent_message_chunk') collectContentBlock(u.content);
  }
}

const killTimer = setTimeout(() => {
  report.errors.push(`timeout after ${opts.timeout}s`);
  finish(3);
}, opts.timeout * 1000);

function finish(code) {
  if (done) return;
  done = true;
  clearTimeout(killTimer);
  // built-in asserts
  let rc = code;
  if (rc === 0 && opts.asserts.noClientFs && report.fsCalls.length > 0) {
    process.stderr.write(`fake-zed: ASSERT FAILED: ${report.fsCalls.length} client fs/terminal call(s) observed\n`);
    rc = 5;
  }
  if (rc === 0 && opts.asserts.locationsUnder) {
    const locationsUnder = realpathSync(opts.asserts.locationsUnder);
    const bad = report.locations.concat(report.diffPaths)
      .filter((p) => {
        let canonical = resolve(p);
        try { canonical = realpathSync(canonical); } catch { /* absent paths cannot be canonicalized */ }
        return !(canonical === locationsUnder || canonical.startsWith(`${locationsUnder}/`));
      });
    if (bad.length > 0) {
      process.stderr.write(`fake-zed: ASSERT FAILED: ${bad.length} location(s) outside '${locationsUnder}': ${bad.slice(0, 3).join(', ')}\n`);
      rc = 6;
    }
  }
  report.ok = rc === 0;
  process.stdout.write(`${JSON.stringify(report)}\n`);
  try {
    child.stdin.end();
  } catch { /* gone */ }
  setTimeout(() => {
    // Kill the WHOLE process group (sh -> sch -> node acp.js): killing just
    // the sh wrapper leaves the bridge running detached, still syncing
    // (observed in live verification — late events polluted later checks).
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch {
      child.kill('SIGTERM');
    }
    setTimeout(() => {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch { /* gone */ }
      process.exit(rc);
    }, 1500);
  }, 500);
}

(async () => {
  try {
    const init = await request('initialize', {
      protocolVersion: 1,
      clientInfo: { name: 'fake-zed', version: '1.0' },
      // Advertise capabilities exactly like Zed: the sch bridge MUST
      // neutralize these before the agent sees them (design D3).
      clientCapabilities: {
        fs: { readTextFile: true, writeTextFile: true },
        terminal: true,
      },
    });
    report.initialize = {
      protocolVersion: init?.protocolVersion,
      agent: init?.agentInfo?.name ?? null,
    };
    process.stderr.write(`fake-zed: initialize OK (protocolVersion=${init?.protocolVersion}, agent=${init?.agentInfo?.name ?? '?'})\n`);

    const sess = await request('session/new', { cwd: opts.cwd, mcpServers: [] });
    report.sessionId = sess?.sessionId ?? null;
    process.stderr.write(`fake-zed: session/new OK (${report.sessionId})\n`);

    if (opts.prompt) {
      const prompt = [{ type: 'text', text: opts.prompt }];
      if (opts.mention) {
        prompt.push({
          type: 'resource_link',
          uri: `file://${opts.mention}`,
          name: opts.mention.split('/').pop(),
        });
      }
      const res = await request('session/prompt', { sessionId: report.sessionId, prompt });
      report.stopReason = res?.stopReason ?? null;
      process.stderr.write(`fake-zed: prompt turn done (stopReason=${report.stopReason})\n`);
    }

    if (opts.between) {
      process.stderr.write(`fake-zed: running between-script: ${opts.between}\n`);
      try {
        execSync(opts.between, { stdio: ['ignore', 'inherit', 'inherit'] });
      } catch (err) {
        report.errors.push(`between-script failed: ${err.message ?? err}`);
        finish(4);
        return;
      }
      process.stderr.write(`fake-zed: settling ${opts.settle}s for sync propagation...\n`);
      await new Promise((r) => setTimeout(r, opts.settle * 1000));
    }

    if (opts.prompt2) {
      textSink = 'agentText2';
      const res2 = await request('session/prompt', {
        sessionId: report.sessionId,
        prompt: [{ type: 'text', text: opts.prompt2 }],
      });
      report.stopReason2 = res2?.stopReason ?? null;
      process.stderr.write(`fake-zed: second prompt turn done (stopReason=${report.stopReason2})\n`);
    }

    if (opts.linger > 0) {
      process.stderr.write(`fake-zed: lingering ${opts.linger}s (sync settling)...\n`);
      await new Promise((r) => setTimeout(r, opts.linger * 1000));
    }
    finish(0);
  } catch (err) {
    report.errors.push(err.message ?? String(err));
    process.stderr.write(`fake-zed: FAILED: ${err.message ?? err}\n`);
    finish(4);
  }
})();
