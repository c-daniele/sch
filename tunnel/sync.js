#!/usr/bin/env node
// sync.js -- standalone fs-sync v2 preflight/watch helper. Control records go
// to stdout; progress and diagnostics are deliberately stderr-only.

import { randomUUID } from 'node:crypto';
import { promises as fsp } from 'node:fs';
import { resolve } from 'node:path';
import { WebSocketStreamTransport } from './transport.js';
import {
  DEFAULT_IGNORE, DEFAULT_MAX_BYTES, MirrorSync, isIgnored, mirrorOptionsFromEnv,
} from './mirror.js';
import { applyAtomicDelete, applyAtomicPut, scanFiles } from './sync-files.js';
import {
  SYNC_EXEMPT_PATHS, isSemanticallyEmpty, makeBaseline, manifestHash, parseBaseline, planBootstrap,
  planThreeWay, stripExempt, writeBaseline,
} from './sync-plan.js';

export const REQUIRED_CAPABILITIES = ['operation-ack', 'barrier', 'post-barrier-manifest'];
const DEFAULT_PROTOCOL_TIMEOUT_MS = 60_000;

/**
 * Start the ACP-compatible fs lifecycle over an already open channel.
 *
 * ACP intentionally remains on the v1-compatible wire contract so it can
 * degrade against older runtime images. Keeping that compatibility adapter
 * here makes the channel lifecycle shared with the standalone v2 helper
 * instead of duplicating it in acp.js.
 */
export async function startAcpSync({ stream, root, env = process.env, log = () => {} }) {
  const sync = new MirrorSync({
    stream,
    root,
    ...mirrorOptionsFromEnv(env),
  });
  sync.on('warning', log);
  await sync.start();
  sync.on('error', (error) => {
    log(`WARNING file sync stopped (${error.message ?? error}); the mirror may go stale — save/reload manually or restart 'sch acp'`);
    sync.close();
  });
  return sync;
}

function fail(message) {
  throw new Error(message);
}

export function parseArgs(argv) {
  const out = { mode: 'once', bootstrap: 'abort', conflict: 'abort' };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === '--watch') out.mode = 'watch';
    else if (key === '--once') out.mode = 'once';
    else if (['--region', '--runtime-arn', '--session-id', '--workspace', '--storage', '--session-epoch', '--root', '--baseline', '--bootstrap', '--conflict'].includes(key)) out[{ '--runtime-arn': 'runtimeArn', '--session-id': 'sessionId', '--session-epoch': 'sessionEpoch' }[key] ?? key.slice(2)] = argv[++i];
    else fail(`unknown option '${key}'`);
  }
  for (const key of ['region', 'runtimeArn', 'sessionId', 'workspace', 'root', 'baseline']) if (!out[key]) fail(`missing required --${key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`);
  if (!['abort', 'local-wins', 'remote-wins', 'union'].includes(out.bootstrap)) fail(`invalid --bootstrap '${out.bootstrap}'`);
  if (!['abort', 'local-wins', 'remote-wins', 'keep-both'].includes(out.conflict)) fail(`invalid --conflict '${out.conflict}'`);
  if (!['s3', 'session'].includes(out.storage)) fail(`invalid --storage '${out.storage}'`);
  out.sessionEpoch = Number.parseInt(out.sessionEpoch ?? '0', 10);
  if (!Number.isInteger(out.sessionEpoch) || out.sessionEpoch < 0) fail('invalid --session-epoch');
  return out;
}

export class FsSyncClient {
  constructor(stream, session, log, timeoutMs = DEFAULT_PROTOCOL_TIMEOUT_MS) {
    this.stream = stream;
    this.session = session;
    this.log = log;
    this.timeoutMs = timeoutMs;
    this.partial = '';
    this.remoteManifest = new Map();
    this.pendingManifest = null;
    this.pendingPut = null;
    this.pendingChunks = [];
    this.pendingFetch = new Map();
    this.pendingAck = new Map();
    this.pendingBarrier = new Map();
    this.onLivePut = null;
    this.onLiveDel = null;
    this.onSkip = null;
    this.remoteGitFingerprint = null;
    this.remoteHello = new Promise((resolveHello, rejectHello) => {
      this.resolveHello = resolveHello;
      this.rejectHello = rejectHello;
    });
    stream.on('data', (chunk) => this._onData(chunk));
    stream.on('closed', (reason) => this._rejectAll(new Error(`fs channel closed: ${reason}`)));
    stream.on('error', (error) => this._rejectAll(error));
  }

  send(message) {
    this.stream.write(`${JSON.stringify(message)}\n`);
  }

  async waitFor(promise, phase) {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(new Error(`timed out waiting for remote fs-sync ${phase} after ${this.timeoutMs / 1000}s`));
      }, this.timeoutMs);
    });
    try {
      return await Promise.race([promise, timeout]);
    } finally {
      clearTimeout(timer);
    }
  }

  _rejectAll(error) {
    this.rejectHello(error);
    if (this.pendingManifest) this.pendingManifest.reject(error);
    for (const resolve of this.pendingFetch.values()) resolve.reject(error);
    for (const resolve of this.pendingAck.values()) resolve.reject(error);
    for (const resolve of this.pendingBarrier.values()) resolve.reject(error);
  }

  _onData(chunk) {
    this.partial += chunk.toString('utf8');
    const lines = this.partial.split('\n');
    this.partial = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try { this._handle(JSON.parse(line)); } catch (error) { this._rejectAll(error); }
    }
  }

  _handle(message) {
    if (message.type === 'hello') {
      const capabilities = new Set(message.capabilities ?? []);
      if (message.v !== 2 || !REQUIRED_CAPABILITIES.every((capability) => capabilities.has(capability))) {
        this.rejectHello(new Error('remote fs-sync runtime does not support required barrier capabilities'));
      } else this.resolveHello(message);
      return;
    }
    if (message.type === 'error') return this._rejectAll(new Error(message.error ?? 'remote fs-sync error'));
    if (message.type === 'manifest') {
      for (const item of message.files ?? []) this.remoteManifest.set(item.p, item);
      if (message.last && this.pendingManifest) {
        this.remoteGitFingerprint = message.gitFingerprint ?? null;
        this.pendingManifest.resolve(this.remoteManifest);
      }
      return;
    }
    if (message.type === 'skip') {
      if (this.onSkip) this.onSkip(message.p, message.s ?? 0, message.reason ?? 'excluded');
      return;
    }
    if (message.type === 'applied' && this.pendingAck.has(message.id)) {
      const pending = this.pendingAck.get(message.id);
      this.pendingAck.delete(message.id);
      if (message.ok) pending.resolve(message); else pending.reject(new Error(message.error ?? `remote failed ${message.p}`));
      return;
    }
    if (message.type === 'barrier-ack' && this.pendingBarrier.has(message.id)) {
      const pending = this.pendingBarrier.get(message.id);
      this.pendingBarrier.delete(message.id);
      if (message.ok) pending.resolve(message); else pending.reject(new Error(message.error ?? 'barrier failed'));
      return;
    }
    if (message.type === 'file' && (message.op === 'put' || message.op === 'del')) {
      if (message.op === 'del') {
        const pending = this.pendingFetch.get(message.p);
        if (pending) { this.pendingFetch.delete(message.p); pending.resolve(null); }
      } else { this.pendingPut = message; this.pendingChunks = []; }
      return;
    }
    if (message.type === 'data' && this.pendingPut?.p === message.p) {
      this.pendingChunks.push(Buffer.from(message.b64 ?? '', 'base64'));
      if (message.last) {
        const header = this.pendingPut;
        const pending = this.pendingFetch.get(header.p);
        this.pendingPut = null;
        const content = Buffer.concat(this.pendingChunks);
        if (pending) {
          this.pendingFetch.delete(header.p);
          pending.resolve({ header, content });
        } else if (this.onLivePut) {
          void this.onLivePut(header, content);
        }
      }
    }
  }

  async start(ignore, maxBytes, expectedRoot = null) {
    this.send({ v: 2, type: 'hello', role: 'local', root: 'local', session: this.session, capabilities: REQUIRED_CAPABILITIES });
    const hello = await this.waitFor(this.remoteHello, 'hello');
    if (expectedRoot && hello.root !== expectedRoot) {
      throw new Error(`remote fs-sync root mismatch: expected '${expectedRoot}', got '${hello.root ?? 'unknown'}'`);
    }
    return this.requestManifest(ignore, maxBytes);
  }

  requestManifest(ignore, maxBytes, git = false) {
    let resolveManifest;
    let rejectManifest;
    const manifestPromise = new Promise((resolvePromise, rejectPromise) => {
      resolveManifest = resolvePromise;
      rejectManifest = rejectPromise;
    });
    this.pendingManifest = { resolve: resolveManifest, reject: rejectManifest };
    this.remoteManifest.clear();
    this.send({ type: 'manifest-request', git, ignore, maxBytes });
    return this.waitFor(manifestPromise, 'manifest');
  }

  fetch(path) {
    const pending = new Promise((resolveFetch, rejectFetch) => {
      this.pendingFetch.set(path, { resolve: resolveFetch, reject: rejectFetch });
      this.send({ type: 'fetch', p: path });
    });
    return this.waitFor(pending, `file '${path}'`);
  }

  mutate(operation, sequence) {
    const id = `${this.session}:${sequence}`;
    const pending = new Promise((resolveAck, rejectAck) => {
      this.pendingAck.set(id, { resolve: resolveAck, reject: rejectAck });
      if (operation.kind === 'del') this.send({ type: 'file', id, op: 'del', p: operation.path, m: Date.now() });
      else operation.send(id);
    });
    return this.waitFor(pending, `acknowledgement for '${operation.path}'`);
  }

  barrier(sequence) {
    const id = `${this.session}:${sequence}`;
    const pending = new Promise((resolveBarrier, rejectBarrier) => {
      this.pendingBarrier.set(id, { resolve: resolveBarrier, reject: rejectBarrier });
      this.send({ v: 2, type: 'barrier', id });
    });
    return this.waitFor(pending, 'barrier');
  }
}

async function localOperation(root, client, operation, sequence) {
  if (operation.side === 'local') {
    if (operation.kind === 'del') await applyAtomicDelete(root, operation.path);
    else {
      const fetched = await client.fetch(operation.path);
      if (!fetched) return;
      await applyAtomicPut(root, fetched.header, fetched.content, sequence);
    }
    return sequence;
  }
  if (operation.kind === 'del') await client.mutate(operation, sequence);
  else {
    const content = await fsp.readFile(resolve(root, operation.path));
    await client.mutate({ ...operation, send: (id) => {
      client.send({ type: 'file', id, op: 'put', p: operation.path, s: content.length, m: operation.value.m, h: operation.value.h, x: operation.value.x });
      for (let offset = 0, chunk = 0; offset < content.length || (content.length === 0 && chunk === 0); chunk += 1) {
        const bytes = content.subarray(offset, offset + 40000);
        offset += bytes.length;
        client.send({ type: 'data', p: operation.path, seq: chunk, b64: bytes.toString('base64'), last: offset >= content.length });
      }
    } }, sequence);
  }
  return sequence;
}

function changed(left, right) {
  return !left || !right || left.h !== right.h || left.s !== right.s || !!left.x !== !!right.x;
}

async function gitFingerprint(root, maxBytes) {
  const manifest = await scanFiles(root, {
    ignore: () => false,
    maxBytes,
    includeGit: true,
  });
  const entries = new Map([...manifest].filter(([path]) => path === '.git' || path.startsWith('.git/')));
  return entries.size ? manifestHash(entries) : null;
}

export async function runSync(options, { log = (message) => process.stderr.write(`sch: ${message}\n`), transport: suppliedTransport } = {}) {
  const root = resolve(options.root);
  const config = mirrorOptionsFromEnv(process.env);
  const ignore = config.ignore ?? DEFAULT_IGNORE;
  const maxBytes = config.maxBytes ?? DEFAULT_MAX_BYTES;
  const includeGitSnapshot = config.includeGitSnapshot ?? false;
  const configuredTimeout = Number.parseInt(process.env.SCH_SYNC_PROTOCOL_TIMEOUT_MS ?? '', 10);
  const protocolTimeoutMs = configuredTimeout > 0 ? configuredTimeout : DEFAULT_PROTOCOL_TIMEOUT_MS;
  const transport = suppliedTransport ?? new WebSocketStreamTransport({ region: options.region, runtimeArn: options.runtimeArn, sessionId: options.sessionId });
  const stream = await transport.open({
    remote: {
      kind: 'fs', workspace: options.workspace, storage: options.storage,
      sessionEpoch: options.sessionEpoch,
    },
  });
  const client = new FsSyncClient(stream, randomUUID(), log, protocolTimeoutMs);
  // The watch loop rescans the local root every 500ms; without deduplication
  // every ignored directory would be re-logged on every tick, flooding the
  // operator's terminal for the lifetime of the helper.
  const loggedSkips = new Set();
  const logSkip = (path, size, reason) => {
    if (loggedSkips.has(path)) return;
    loggedSkips.add(path);
    log(`excluded '${path}' (${reason}, ${size} bytes)`);
  };
  client.onSkip = logSkip;
  let closed = false;
  let watcher = null;
  try {
    log(`sync preflight started for '${options.workspace}'`);
    const expectedRemoteRoot = suppliedTransport ? null : (
      options.storage === 's3' ? '/home/sch/workspace/repo' : '/mnt/workspace/repo'
    );
    let remote = stripExempt(await client.start(ignore, maxBytes, expectedRemoteRoot));
    const local = stripExempt(await scanFiles(root, { ignore: (path) => isIgnored(path, ignore), maxBytes, onSkip: logSkip }));
    const fingerprint = resolve(root);
    let baseline = null;
    try {
      const parsed = parseBaseline(await fsp.readFile(options.baseline, 'utf8'), options.workspace, fingerprint);
      baseline = parsed ? stripExempt(parsed) : null;
    } catch { /* no prior baseline */ }
    let plannedLocal = local;
    if (includeGitSnapshot && !baseline && isSemanticallyEmpty(local) && !isSemanticallyEmpty(remote)) {
      // Git metadata is opt-in because its object database can be much larger
      // than the source tree. It is never live-synced.
      remote = stripExempt(await client.requestManifest(ignore, maxBytes, true));
    } else if (includeGitSnapshot && !baseline && !isSemanticallyEmpty(local) && isSemanticallyEmpty(remote)) {
      plannedLocal = stripExempt(await scanFiles(root, {
        ignore: (path) => isIgnored(path, ignore), maxBytes, includeGit: true,
        onSkip: logSkip,
      }));
    }
    const plan = baseline ? planThreeWay(baseline, local, remote, options.conflict) : planBootstrap(plannedLocal, remote, options.bootstrap);
    if (!plan.converged) fail(`sync conflict: ${plan.conflicts.map((conflict) => conflict.path).join(', ')}; select an explicit policy`);
    let sequence = 0;
    for (const operation of plan.operations) sequence = await localOperation(root, client, operation, sequence + 1);
    const barrier = await client.barrier(sequence + 1);
    let current = stripExempt(await scanFiles(root, { ignore: (path) => isIgnored(path, ignore), maxBytes }));
    const currentGitFingerprint = includeGitSnapshot ? await gitFingerprint(root, maxBytes) : null;
    if (currentGitFingerprint && client.remoteGitFingerprint && currentGitFingerprint !== client.remoteGitFingerprint) {
      log('WARNING Git metadata differs between local and remote; .git is not live-synced');
    }
    await writeBaseline(options.baseline, makeBaseline(options.workspace, fingerprint, current, currentGitFingerprint));
    const summary = { operations: plan.operations.length, conflicts: plan.conflicts, barrier };
    if (options.mode !== 'watch') {
      const released = await stream.closeStream();
      if (released === false) log('WARNING remote did not confirm fs-sync lease release; retry may wait for orphan cleanup');
      return summary;
    }

    // Remote live events have no operation id because the worker originates
    // them. Apply atomically, then update the local snapshot to avoid echoing
    // the change back through the polling loop.
    let serial = Promise.resolve();
    client.onLivePut = (header, content) => {
      if (SYNC_EXEMPT_PATHS.has(header.p)) return serial;
      serial = serial.then(async () => {
        await applyAtomicPut(root, header, content, Date.now());
        current.set(header.p, { s: content.length, m: header.m, h: header.h, x: !!header.x });
      });
      return serial;
    };
    client.onLiveDel = (path) => {
      if (SYNC_EXEMPT_PATHS.has(path)) return serial;
      serial = serial.then(async () => {
        await applyAtomicDelete(root, path);
        current.delete(path);
      });
      return serial;
    };
    // Handle worker-originated deletion events that do not belong to fetch.
    const originalHandle = client._handle.bind(client);
    client._handle = (message) => {
      if (message.type === 'file' && message.op === 'del' && !client.pendingFetch.has(message.p) && client.onLiveDel) {
        void client.onLiveDel(message.p);
        return;
      }
      originalHandle(message);
    };

    let liveSequence = 10_000;
    watcher = setInterval(() => {
      serial = serial.then(async () => {
        const next = stripExempt(await scanFiles(root, { ignore: (path) => isIgnored(path, ignore), maxBytes, onSkip: logSkip }));
        for (const [path, value] of next) {
          if (changed(current.get(path), value)) {
            liveSequence = await localOperation(root, client, { kind: 'put', side: 'remote', path, value }, liveSequence + 1);
          }
        }
        for (const path of current.keys()) {
          if (!next.has(path)) liveSequence = await localOperation(root, client, { kind: 'del', side: 'remote', path }, liveSequence + 1);
        }
        current = next;
      }).catch((error) => log(`live sync stopped: ${error.message ?? error}`));
    }, 500);

    return {
      summary,
      async close() {
        if (closed) return summary;
        closed = true;
        clearInterval(watcher);
        await serial;
        const finalBarrier = await client.barrier(liveSequence + 1);
        const finalLocal = stripExempt(await scanFiles(root, { ignore: (path) => isIgnored(path, ignore), maxBytes }));
        await writeBaseline(options.baseline, makeBaseline(
          options.workspace,
          fingerprint,
          finalLocal,
          includeGitSnapshot ? await gitFingerprint(root, maxBytes) : null,
        ));
        const released = await stream.closeStream();
        if (released === false) log('WARNING remote did not confirm fs-sync lease release; retry may wait for orphan cleanup');
        return { ...summary, barrier: finalBarrier };
      },
    };
  } catch (error) {
    if (watcher) clearInterval(watcher);
    stream.closeStream();
    throw error;
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  // The Python supervisor normally keeps stdout open until shutdown finishes.
  // If an embedding editor closes the control pipe early, final summary output
  // must not turn an otherwise successful sync shutdown into a Node crash.
  process.stdout.on('error', (error) => {
    if (error.code !== 'EPIPE') throw error;
  });
  try {
    const result = await runSync(options);
    if (options.mode === 'once') {
      process.stdout.write(`${JSON.stringify({ type: 'ready', mode: options.mode, summary: result })}\n`);
      process.stdout.write(`${JSON.stringify({ type: 'summary', summary: result })}\n`);
      return;
    }
    process.stdout.write(`${JSON.stringify({ type: 'ready', mode: options.mode, summary: result.summary })}\n`);
    const shutdown = async () => {
      try {
        const summary = await result.close();
        process.stdout.write(`${JSON.stringify({ type: 'summary', summary })}\n`);
      } catch (error) {
        process.stdout.write(`${JSON.stringify({ type: 'error', message: error.message ?? String(error) })}\n`);
        process.stderr.write(`sch: sync shutdown failed: ${error.message ?? error}\n`);
        process.exitCode = 1;
      }
    };
    process.once('SIGINT', shutdown);
    process.once('SIGTERM', shutdown);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ type: 'error', message: error.message ?? String(error) })}\n`);
    process.stderr.write(`sch: sync failed: ${error.message ?? error}\n`);
    process.exitCode = 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) void main();
