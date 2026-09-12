#!/usr/bin/env node
// attach.js — `sch attach` adapter (sch-remote-ui-tunnel, design D5).
// Invoked by bin/sch's cmd_attach. Opens a local TCP listener; for each
// accepted local connection, opens a dedicated ResilientStream (target
// {kind:"tcp", port} against the remote `opencode serve`, supervised by the
// shim's serve-ensure action) and pipes bytes bidirectionally. Once the
// listener is ready, spawns the local `opencode attach <url>` TUI as a
// child process with inherited stdio (direct terminal access), and exits
// with its exit code once it quits.
//
// No channel-count cap tied to a shared budget (design D9 resolved by the
// pivot to InvokeAgentRuntimeWithWebSocketStream — no shared "10 concurrent
// shells" ceiling with `sch shell`/`sch open`/`sch run`): SCH_TUNNEL_MAX_CHANNELS
// below is a soft safety net against a runaway client, not an architectural
// necessity.

import { execFileSync, spawn } from 'node:child_process';
import { WebSocketStreamTransport } from './transport.js';
import { openTcpBridge } from './tcp-bridge.js';

function captureTerminalState() {
  if (!process.stdin.isTTY) return null;
  try {
    return execFileSync('stty', ['-g'], { encoding: 'utf8', stdio: ['inherit', 'pipe', 'ignore'] }).trim();
  } catch {
    return null;
  }
}

function restoreTerminal(state) {
  if (!process.stdin.isTTY) return;
  if (state) {
    try {
      execFileSync('stty', [state], { stdio: ['inherit', 'ignore', 'ignore'] });
    } catch { /* best-effort cleanup after the TUI exits */ }
  }
  if (process.stdout.isTTY) {
    process.stdout.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?1015l\x1b[?2004l\x1b[?25h');
  }
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--region') out.region = argv[++i];
    else if (a === '--runtime-arn') out.runtimeArn = argv[++i];
    else if (a === '--session-id') out.sessionId = argv[++i];
    else if (a === '--workspace') out.workspace = argv[++i];
    else if (a === '--storage') out.storage = argv[++i];
    else if (a === '--session-epoch') out.sessionEpoch = Number.parseInt(argv[++i], 10);
    else if (a === '--remote-port') out.remotePort = parseInt(argv[++i], 10);
    else if (a === '--opencode-bin') out.opencodeBin = argv[++i];
    else if (a === '--extra-attach-arg') (out.extraAttachArgs ??= []).push(argv[++i]);
  }
  const missing = ['region', 'runtimeArn', 'sessionId', 'workspace', 'storage', 'remotePort'].filter((k) => !out[k]);
  if (missing.length > 0) {
    throw new Error(
      `missing required args: ${missing.join(', ')} ` +
        '(usage: attach.js --region <r> --runtime-arn <arn> --session-id <sid> --workspace <ws> --remote-port <p>)',
    );
  }
  out.opencodeBin = out.opencodeBin || 'opencode';
  out.extraAttachArgs = out.extraAttachArgs || [];
  return out;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const terminalState = captureTerminalState();
  const transport = new WebSocketStreamTransport({
    region: opts.region,
    runtimeArn: opts.runtimeArn,
    sessionId: opts.sessionId,
  });

  const bridge = await openTcpBridge({
    transport,
    label: 'attach',
    remote: {
      kind: 'tcp', port: opts.remotePort,
      workspace: opts.workspace, storage: opts.storage,
      sessionEpoch: opts.sessionEpoch,
    },
  });
  const { localPort } = bridge;
  process.stderr.write(`sch: attach bridge listening on 127.0.0.1:${localPort} -> remote port ${opts.remotePort}\n`);

  const attachArgv = [opts.opencodeBin, 'attach', `http://127.0.0.1:${localPort}`, ...opts.extraAttachArgs];
  process.stderr.write(`sch: launching local TUI: ${attachArgv.join(' ')}\n`);
  const child = spawn(attachArgv[0], attachArgv.slice(1), { stdio: 'inherit' });

  await new Promise((resolve) => {
    child.on('exit', async (code, signal) => {
      await bridge.shutdown();
      restoreTerminal(terminalState);
      process.exitCode = signal ? 1 : code ?? 0;
      resolve();
    });
    process.on('SIGINT', () => child.kill('SIGINT'));
    process.on('SIGTERM', () => child.kill('SIGTERM'));
  });
}

main().catch((err) => {
  process.stderr.write(`sch: fatal: ${err.stack ?? err}\n`);
  process.exitCode = 1;
});
