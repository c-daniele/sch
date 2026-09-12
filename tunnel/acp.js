#!/usr/bin/env node
// acp.js — `sch acp` adapter (sch-remote-ui-tunnel design D6; extended by
// sch-acp-editor-integration design D2/D3). Invoked by bin/sch's cmd_acp.
// Opens a ResilientStream targeting the workspace's ACP agent process in
// the microVM (`opencode acp` for harness=opencode, `claude-agent-acp` for
// harness=claude; no other harness has an ACP agent) and MEDIATES this
// process's stdin/stdout to it at the
// JSON-RPC message level (no longer a verbatim byte pipe):
//
//   - per-field path translation between the local mirror and
//     /mnt/workspace/repo in both directions (acp-path-map.js, spec
//     acp-file-locality "Path translation in ACP messages");
//   - client capability neutralization: `clientCapabilities.fs.*` and
//     `clientCapabilities.terminal` are forced to false in the initialize
//     forwarded to the agent (design D3 — the agent must operate ONLY on
//     the remote filesystem/shell, one source of truth);
//   - defense in depth: agent-issued `fs/*` / `terminal/*` requests are
//     answered with a JSON-RPC error and NEVER forwarded to the editor.
//
// The mediation preserves message ids, relative order and every non-path
// field: messages whose method carries no mapped field are forwarded as
// the ORIGINAL line (byte-identical), and rewritten messages are
// re-serialized compactly. Order is preserved by strictly synchronous
// per-line processing in both directions.
//
// Stream discipline (spec remote-ui-tunnel "No diagnostic noise on
// stdout"): stdout carries ONLY JSON-RPC lines for the editor. Every
// diagnostic goes to stderr. Never mix the two.

import { WebSocketStreamTransport } from './transport.js';
import { AcpPathMap } from './acp-path-map.js';
import { startAcpSync } from './sync.js';

// add-pi-harness (design D11): harnesses for which an ACP agent exists inside
// the microVM — `opencode` natively, `claude` through the pinned official
// adapter. Anything else (today: `pi`) has no ACP entry point at all.
const HARNESSES_WITH_ACP_AGENT = ['opencode', 'claude'];

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--region') out.region = argv[++i];
    else if (a === '--runtime-arn') out.runtimeArn = argv[++i];
    else if (a === '--session-id') out.sessionId = argv[++i];
    else if (a === '--workspace') out.workspace = argv[++i];
    else if (a === '--harness') out.harness = argv[++i];
    else if (a === '--storage') out.storage = argv[++i];
    else if (a === '--session-epoch') out.sessionEpoch = Number.parseInt(argv[++i], 10);
    else if (a === '--mirror') out.mirror = argv[++i];
  }
  const missing = ['region', 'runtimeArn', 'sessionId', 'workspace', 'harness', 'storage'].filter((k) => !out[k]);
  if (missing.length > 0) {
    throw new Error(
      `missing required args: ${missing.join(', ')} ` +
        '(usage: acp.js --region <r> --runtime-arn <arn> --session-id <sid> --workspace <ws> --harness <opencode|claude> [--mirror <dir>])',
    );
  }
  // add-pi-harness (design D11): the ACP bridge only accepts harnesses that
  // HAVE an ACP agent in the microVM. `pi` is a valid SCH harness but has
  // neither a native `acp` subcommand nor an adapter, so it is refused here
  // too — the CLI already refuses it earlier, this is the second line of
  // defense for a directly-spawned bridge (editors invoke acp.js).
  if (!HARNESSES_WITH_ACP_AGENT.includes(out.harness)) {
    throw new Error(
      `--harness must be one of ${HARNESSES_WITH_ACP_AGENT.join(', ')} ` +
        `(no ACP agent exists for '${out.harness}'), got '${out.harness}'`,
    );
  }
  if (!['s3', 'session'].includes(out.storage)) {
    throw new Error(`--storage must be 's3' or 'session', got '${out.storage}'`);
  }
  return out;
}

export function argvForHarness(harness) {
  // design D6 (sch-remote-ui-tunnel): opencode -> native `opencode acp`
  // (goes through harness-wrapper.sh via PATH, inheriting the readiness
  // gate + ENV bridge for free); claude -> the official adapter binary.
  // Unreachable for other harnesses: parseArgs rejects them above.
  if (!HARNESSES_WITH_ACP_AGENT.includes(harness)) {
    throw new Error(`no ACP agent exists for harness '${harness}'`);
  }
  return harness === 'claude' ? ['claude-agent-acp'] : ['opencode', 'acp'];
}

/** Incremental newline splitter: feed(chunk) -> array of complete lines
 * (without trailing \n). Carries partial data across feeds. */
export class LineSplitter {
  constructor() {
    this._partial = '';
  }

  feed(chunk) {
    this._partial += chunk.toString('utf8');
    const lines = this._partial.split('\n');
    this._partial = lines.pop();
    return lines;
  }
}

/**
 * Per-message ACP mediation (tasks 3.2/3.3). Pure/synchronous: unit-testable
 * without any transport. Both handlers take one complete JSON-RPC line and
 * return { forward: [lines], reply: [lines] }:
 *   - editorLine: forward -> agent, reply -> editor (unused today)
 *   - agentLine:  forward -> editor, reply -> agent (fs/terminal rejections)
 * Lines returned are WITHOUT trailing newline; the caller appends it.
 */
export class AcpMediator {
  /**
   * @param {object} opts
   * @param {AcpPathMap|null} opts.pathMap  null -> no path translation
   *        (chat-style degraded session); capability neutralization and
   *        fs/terminal rejection stay active regardless (design D3 is not
   *        conditioned on the fs channel).
   * @param {(msg: string) => void} [opts.log] stderr diagnostics sink
   */
  constructor({ pathMap = null, log = () => {} } = {}) {
    this.pathMap = pathMap;
    this.log = log;
  }

  _parse(line) {
    if (!line || !line.trim()) return null;
    try {
      const msg = JSON.parse(line);
      return msg && typeof msg === 'object' ? msg : null;
    } catch {
      return null;
    }
  }

  _warnSuspicious(msg, direction) {
    if (!this.pathMap) return;
    for (const s of this.pathMap.findSuspicious(msg, direction)) {
      this.log(`acp bridge: un-translated ${direction === 'toEditor' ? 'remote' : 'local'} path in unmapped field '${s.field}': ${s.value} (message forwarded unchanged)`);
    }
  }

  /** editor -> agent */
  editorLine(line) {
    const msg = this._parse(line);
    if (msg === null) {
      if (line.trim()) this.log('acp bridge: non-JSON line from editor forwarded verbatim');
      return { forward: [line], reply: [] };
    }
    if (msg.method === 'initialize' && msg.params && typeof msg.params === 'object') {
      // Capability neutralization (design D3; spec acp-file-locality
      // "Neutralization of client-fs capabilities"). Applied in EVERY
      // mode, including chat-style degradation.
      const caps = typeof msg.params.clientCapabilities === 'object' && msg.params.clientCapabilities !== null
        ? msg.params.clientCapabilities
        : {};
      const offeredFs = JSON.stringify(caps.fs ?? null);
      const offeredTerm = JSON.stringify(caps.terminal ?? null);
      caps.fs = { readTextFile: false, writeTextFile: false };
      caps.terminal = false;
      msg.params.clientCapabilities = caps;
      this.log(`acp bridge: neutralized client capabilities toward agent (editor offered fs=${offeredFs}, terminal=${offeredTerm})`);
      return { forward: [JSON.stringify(msg)], reply: [] };
    }
    if (this.pathMap && msg.method && msg.params
        && ['session/new', 'session/load', 'session/prompt'].includes(msg.method)) {
      this.pathMap.toAgent(msg);
      this._warnSuspicious(msg, 'toAgent');
      return { forward: [JSON.stringify(msg)], reply: [] };
    }
    this._warnSuspicious(msg, 'toAgent');
    return { forward: [line], reply: [] }; // untouched -> byte-identical
  }

  /** agent -> editor */
  agentLine(line) {
    const msg = this._parse(line);
    if (msg === null) {
      if (line.trim()) this.log('acp bridge: non-JSON line from agent forwarded verbatim');
      return { forward: [line], reply: [] };
    }
    // Defense in depth (design D3): the agent MUST NOT use the client's
    // fs/terminal after the neutralized initialize; reject instead of
    // forwarding so the editor never touches its local files on the
    // agent's behalf.
    if (typeof msg.method === 'string' && (msg.method.startsWith('fs/') || msg.method.startsWith('terminal/'))) {
      this.log(`acp bridge: rejected agent request '${msg.method}' (client fs/terminal disabled by sch, design D3)`);
      if (msg.id !== undefined && msg.id !== null) {
        return {
          forward: [],
          reply: [JSON.stringify({
            jsonrpc: '2.0',
            id: msg.id,
            error: { code: -32601, message: `sch acp bridge: '${msg.method}' is not available (client fs/terminal capabilities are disabled; the agent operates on the remote workspace only)` },
          })],
        };
      }
      return { forward: [], reply: [] }; // notification form: drop
    }
    if (this.pathMap && msg.method && ['session/update', 'session/request_permission'].includes(msg.method)) {
      this.pathMap.toEditor(msg);
      this._warnSuspicious(msg, 'toEditor');
      return { forward: [JSON.stringify(msg)], reply: [] };
    }
    this._warnSuspicious(msg, 'toEditor');
    return { forward: [line], reply: [] }; // untouched -> byte-identical
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const transport = new WebSocketStreamTransport({
    region: opts.region,
    runtimeArn: opts.runtimeArn,
    sessionId: opts.sessionId,
  });

  process.stderr.write(
    `sch: opening ACP tunnel for workspace '${opts.workspace}' (harness=${opts.harness})...\n`,
  );

  let stream;
  try {
    stream = await transport.open({
      remote: {
        kind: 'exec', argv: argvForHarness(opts.harness),
        workspace: opts.workspace, storage: opts.storage,
        sessionEpoch: opts.sessionEpoch,
      },
    });
  } catch (err) {
    process.stderr.write(`sch: failed to open ACP tunnel: ${err.message ?? err}\n`);
    process.exitCode = 1;
    return;
  }

  process.stderr.write('sch: ACP tunnel open; mediating JSON-RPC stdio\n');

  const log = (m) => process.stderr.write(`sch: ${m}\n`);
  let pathMap = null;
  if (opts.mirror) {
    try {
      const remoteRoot = opts.storage === 's3' ? '/home/sch/workspace/repo' : '/mnt/workspace/repo';
      pathMap = new AcpPathMap({ localRoot: opts.mirror, remoteRoot });
      log(`path translation active: ${opts.mirror} <-> ${remoteRoot}`);
    } catch (err) {
      log(`WARNING cannot initialize path map (${err.message ?? err}); continuing chat-style without path translation`);
    }
  } else {
    log('no --mirror given: chat-style session (no path translation)');
  }
  const mediator = new AcpMediator({ pathMap, log });

  // --- file-locality sync channel (design D4, task 4.4) -------------------
  // A SECOND tunnel channel (own tunnel_id, mode:"fs") next to the exec
  // channel: JSON-RPC and sync never share a stream (spec acp-file-locality
  // "Canale di sync dedicato con degradazione esplicita"). The editor's
  // stdin is gated until hydration completes ("Prima idratazione del
  // mirror": the worktree is materialized before the session is declared
  // ready) or until we degrade.
  let mirrorSync = null;
  let fsStream = null;
  if (pathMap) {
    try {
      fsStream = await transport.open({
        remote: {
          kind: 'fs', workspace: opts.workspace, storage: opts.storage,
          sessionEpoch: opts.sessionEpoch,
        },
      });
      fsStream.on('error', (err) => log(`fs channel stream error: ${err.message ?? err}`));
      fsStream.on('down', (attempt, reason) => log(`fs channel connection interrupted (attempt ${attempt}: ${reason}) — reconnecting...`));
      fsStream.on('up', () => log('fs channel connection (re)established'));
       log('hydrating local mirror from the remote worktree...');
       mirrorSync = await startAcpSync({
         stream: fsStream,
         root: opts.mirror,
         log,
       });
       log(`mirror ready: ${opts.mirror}`);
    } catch (err) {
      // v21 shim rejects mode:"fs" (or hydration failed): chat-style
      // degradation, explicit and non-fatal (spec scenario "Degradation
      // on a shim without fs mode"). Path translation stays ACTIVE: the agent
      // still needs remote paths (claude-agent-acp validates session/new
      // cwd against the microVM filesystem — see design OQ-CWD-REMOTO).
      log(`WARNING file-locality sync unavailable (${err.message ?? err})`);
      log('WARNING continuing in chat-style mode: no mirror sync — editor file features (jump-to-file, inline diff) will not reflect the remote worktree. Requires runtime image >= v22 with tunnel mode "fs".');
      try {
        if (mirrorSync) mirrorSync.close();
        if (fsStream) fsStream.closeStream();
      } catch { /* best effort */ }
      mirrorSync = null;
      fsStream = null;
    }
  }

  let shuttingDown = false;
  const shutdown = (reason) => {
    if (shuttingDown) return;
    shuttingDown = true;
    process.stderr.write(`sch: closing ACP tunnel (${reason})\n`);
    if (mirrorSync) mirrorSync.close();
    if (fsStream) fsStream.closeStream();
    stream.closeStream();
    // Hard-exit fallback: if the exec channel's 'closed' event never fires
    // (e.g. a physical connection mid-reconnect when the close was
    // requested), do not linger as a detached process applying late sync
    // events — found in live verification (task 6.2).
    setTimeout(() => {
      process.stderr.write('sch: forcing exit (close did not complete in 3s)\n');
      process.exit(0);
    }, 3000).unref();
  };

  const agentLines = new LineSplitter();
  stream.on('data', (buf) => {
    for (const line of agentLines.feed(buf)) {
      const { forward, reply } = mediator.agentLine(line);
      for (const f of forward) process.stdout.write(`${f}\n`);
      for (const r of reply) stream.write(`${r}\n`);
    }
  });
  stream.on('down', (attempt, reason) => {
    process.stderr.write(`sch: tunnel connection interrupted (attempt ${attempt}: ${reason}) — reconnecting...\n`);
  });
  stream.on('up', () => {
    process.stderr.write('sch: tunnel connection (re)established\n');
  });
  stream.on('closed', (reason, exitInfo) => {
    process.stderr.write(`sch: tunnel closed: ${reason}\n`);
    // Best-effort teardown of the sync channel so the shim can reap the
    // fs worker session immediately (instead of waiting for the orphan
    // sweep).
    try {
      if (mirrorSync) mirrorSync.close();
      if (fsStream) fsStream.closeStream();
    } catch { /* exiting anyway */ }
    // Propagate the remote process's real exit code when the channel told
    // us one (sch-remote-ui-tunnel task 6.5); otherwise 0 for a normal
    // local-initiated close, 1 for anything else.
    if (exitInfo && typeof exitInfo.exitCode === 'number') process.exit(exitInfo.exitCode);
    else process.exit(shuttingDown ? 0 : 1);
  });

  const editorLines = new LineSplitter();
  process.stdin.on('data', (buf) => {
    for (const line of editorLines.feed(buf)) {
      const { forward, reply } = mediator.editorLine(line);
      for (const f of forward) stream.write(`${f}\n`);
      for (const r of reply) process.stdout.write(`${r}\n`);
    }
  });
  process.stdin.on('end', () => shutdown('local stdin closed'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

// Guard so tests can `import { AcpMediator, LineSplitter } from './acp.js'`
// without side effects.
import { pathToFileURL } from 'node:url';
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    process.stderr.write(`sch: fatal: ${err.stack ?? err}\n`);
    process.exitCode = 1;
  });
}
