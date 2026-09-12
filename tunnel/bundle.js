#!/usr/bin/env node
// bundle.js — git bundle transfer helper for the git-native workflow
// (add-git-native-workflow, design D2/D3/D7).
//
// Moves a single binary file (a git bundle) between the operator's laptop
// and the remote workspace's bundle STAGING directory, reusing the existing
// tunnel file-channel primitives end to end:
//   - transport: WebSocketStreamTransport (transport.js) — same resilient
//     framed byte stream used by `sch acp`/`sch attach`;
//   - remote end: the existing fs_sync_worker.py, spawned via mode:"exec"
//     with `--root <staging dir>` (NOT the repo worktree — the staging dir
//     lives under state/bundles by design, see image/app/main.py
//     BUNDLE_STAGING_DIR), so no new remote code path is needed;
//   - protocol: fs-sync v2 (FsSyncClient in sync.js) — hash-verified,
//     ack'd, chunked transfers (40KB data lines, well under the 64KB
//     WebSocket frame budget). Task 1.4 note: the transport has NO hard
//     per-file limit — DEFAULT_MAX_BYTES (10MB) is only a *policy default*
//     propagated via manifest-request, so this helper simply raises it;
//     chunking/framing already handle arbitrarily large files.
//
// Control protocol on stdout (one JSON line, same style as sync.js):
//   success: {"type":"done","bytes":N,"name":"...","hash":"..."}
//   failure: {"type":"error","message":"..."}
// Progress and diagnostics go to stderr only.

import { createHash } from 'node:crypto';
import { promises as fsp } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { WebSocketStreamTransport } from './transport.js';
import { FsSyncClient } from './sync.js';

// Effectively-unlimited per-file threshold for bundle transfers: the fs-sync
// maxBytes is advisory policy (what to EXCLUDE from a sync), not a transport
// capability — bundles must never be excluded by size.
const BUNDLE_MAX_BYTES = 64 * 1024 * 1024 * 1024; // 64GB
const CHUNK_RAW = 40000; // must match fs_sync_worker.py / sync.js data lines

function fail(message) {
  throw new Error(message);
}

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if ([
      '--region', '--runtime-arn', '--session-id', '--workspace', '--storage',
      '--session-epoch', '--staging-root', '--upload', '--download', '--name',
    ].includes(key)) {
      const map = {
        '--runtime-arn': 'runtimeArn',
        '--session-id': 'sessionId',
        '--session-epoch': 'sessionEpoch',
        '--staging-root': 'stagingRoot',
      };
      out[map[key] ?? key.slice(2)] = argv[++i];
    } else fail(`unknown option '${key}'`);
  }
  for (const key of ['region', 'runtimeArn', 'sessionId', 'workspace', 'stagingRoot', 'name']) {
    if (!out[key]) fail(`missing required --${key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`);
  }
  if (!!out.upload === !!out.download) fail('exactly one of --upload/--download is required');
  if (!['s3', 'session'].includes(out.storage)) fail(`invalid --storage '${out.storage}'`);
  if (!/^[A-Za-z0-9._-]+$/.test(out.name)) fail(`invalid --name '${out.name}'`);
  out.sessionEpoch = Number.parseInt(out.sessionEpoch ?? '0', 10);
  if (!Number.isInteger(out.sessionEpoch) || out.sessionEpoch < 0) fail('invalid --session-epoch');
  return out;
}

function hash16(buffer) {
  return createHash('sha1').update(buffer).digest('hex').slice(0, 16);
}

function humanBytes(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} bytes`;
}

async function openClient(options, log, suppliedTransport) {
  const transport = suppliedTransport ?? new WebSocketStreamTransport({
    region: options.region, runtimeArn: options.runtimeArn, sessionId: options.sessionId,
  });
  // mode:"exec" with the image's own fs_sync_worker rooted at the staging
  // dir: same subprocess plumbing the shim already supports, no fs-mode
  // lease contention with a live mirror sync on the same workspace.
  const stream = await transport.open({
    remote: {
      kind: 'exec',
      argv: ['python3', '/app/fs_sync_worker.py', '--root', options.stagingRoot],
      workspace: options.workspace,
      storage: options.storage,
      sessionEpoch: options.sessionEpoch,
    },
  });
  const client = new FsSyncClient(stream, randomUUID(), log);
  // An image without fs-sync v2 fails the hello handshake here -> the
  // caller surfaces "runtime image does not support git-native mode".
  await client.start([], BUNDLE_MAX_BYTES);
  return { stream, client };
}

export async function runBundleTransfer(options, { log = (m) => process.stderr.write(`sch: ${m}\n`), transport } = {}) {
  const { stream, client } = await openClient(options, log, transport);
  try {
    if (options.upload) {
      const content = await fsp.readFile(options.upload);
      log(`uploading bundle '${options.name}' (${humanBytes(content.length)})...`);
      const header = {
        s: content.length, m: Date.now(), h: hash16(content), x: false,
      };
      let sent = 0;
      let lastLogged = 0;
      await client.mutate({
        kind: 'put',
        path: options.name,
        send: (id) => {
          client.send({ type: 'file', id, op: 'put', p: options.name, s: header.s, m: header.m, h: header.h, x: header.x });
          for (let offset = 0, chunk = 0; offset < content.length || (content.length === 0 && chunk === 0); chunk += 1) {
            const bytes = content.subarray(offset, offset + CHUNK_RAW);
            offset += bytes.length;
            client.send({ type: 'data', p: options.name, seq: chunk, b64: bytes.toString('base64'), last: offset >= content.length });
            sent = offset;
            if (sent - lastLogged >= 8 * 1024 * 1024) {
              lastLogged = sent;
              log(`  ... ${humanBytes(sent)} / ${humanBytes(content.length)}`);
            }
          }
        },
      }, 1);
      log(`upload complete (${humanBytes(content.length)}, hash-verified by the remote)`);
      return { bytes: content.length, name: options.name, hash: header.h };
    }

    // Download: the manifest from start() already lists the staging dir.
    const entry = client.remoteManifest.get(options.name);
    if (!entry) fail(`remote bundle '${options.name}' not found in staging`);
    log(`downloading bundle '${options.name}' (${humanBytes(entry.s)})...`);
    const fetched = await client.fetch(options.name);
    if (!fetched) fail(`remote bundle '${options.name}' vanished during download`);
    const { header, content } = fetched;
    if (content.length !== header.s || hash16(content) !== header.h) {
      fail(`bundle '${options.name}' failed content verification after download`);
    }
    await fsp.writeFile(options.download, content);
    log(`download complete (${humanBytes(content.length)}, hash verified)`);
    return { bytes: content.length, name: options.name, hash: header.h };
  } finally {
    await stream.closeStream();
  }
}

async function main() {
  process.stdout.on('error', (error) => {
    if (error.code !== 'EPIPE') throw error;
  });
  try {
    const options = parseArgs(process.argv.slice(2));
    const result = await runBundleTransfer(options);
    process.stdout.write(`${JSON.stringify({ type: 'done', ...result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ type: 'error', message: error.message ?? String(error) })}\n`);
    process.stderr.write(`sch: bundle transfer failed: ${error.message ?? error}\n`);
    process.exitCode = 1;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) void main();
