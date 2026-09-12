// mirror-reconnect.test.js — reconnection/loss-recovery test for the fs
// sync (sch-acp-editor-integration task 4.6; spec acp-file-locality
// scenario "Reconnection mid-sync").
//
// Reuses the framing test harness idea: the LOCAL side runs the REAL
// production stack (ResilientStream + FramedPeer from transport.js/
// framing.js — exactly what `sch acp` uses), wired to an in-memory fake
// channel that DROPS a burst of wire messages mid-transfer (what a
// physical WebSocket drop does to in-flight frames: the shim's
// _TunnelSession keeps peer state, un-acked bytes are recovered via the
// offset/ack/resend framing on the next attach). The REMOTE side is the
// real Python worker glued through a mirroring FramedPeer with the same
// send-window flow control as the shim (main.py reader_loop).
//
// Asserts: hydration of a multi-chunk file completes across the loss with
// NO missing, truncated or duplicated content, and live sync still works
// afterwards.
//
// The burst also drops the local side's first resend requests (`R`), and the
// remote pauses on its send window right after the loss, so recovery cannot
// depend on a later out-of-order frame provoking a fresh request: the local
// peer must repeat the request on its heartbeat tick (FramedPeer.gapPending,
// transport.js ACK_CHECK_INTERVAL_MS). Before that, this test passed or
// timed out depending on how the worker's stdout happened to be chunked.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import {
  mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import { FramedPeer, CloseRequested } from './framing.js';
import { ResilientStream } from './transport.js';
import { MirrorSync } from './mirror.js';

const WORKER = join(dirname(fileURLToPath(import.meta.url)), '..', 'image', 'app', 'fs_sync_worker.py');
const SEND_WINDOW = 128 * 1024; // mirrors main.py _TUNNEL_SEND_WINDOW

const remoteRoot = mkdtempSync(join(tmpdir(), 'sch-recon-remote-'));
const localRoot = mkdtempSync(join(tmpdir(), 'sch-recon-local-'));

// A payload large enough to span MANY framing chunks (45000B each), so a
// drop can land mid-transfer with more frames following (gap detection).
const PAYLOAD = Buffer.alloc(700_000);
for (let i = 0; i < PAYLOAD.length; i += 4) PAYLOAD.writeUInt32LE((i * 2654435761) >>> 0, i);
writeFileSync(join(remoteRoot, 'big.dat'), PAYLOAD);
writeFileSync(join(remoteRoot, 'small.txt'), 'small file');
const sha = (b) => createHash('sha1').update(b).digest('hex');

// --- fake lossy wire ------------------------------------------------------
let dropRemoteToLocal = 0; // messages still to drop in the r->l direction
let dropLocalToRemote = 0;
let droppedR2L = 0;
let seenR2L = 0;
let armed = true; // drop trigger armed (fires once, mid-first-big-transfer)

const child = spawn('python3', [WORKER, '--root', remoteRoot], { stdio: ['pipe', 'pipe', 'pipe'] });
child.stderr.on('data', (b) => {
  if (process.env.MIRROR_TEST_DEBUG) process.stderr.write(`[worker] ${b}`);
});

// Remote glue: FramedPeer B speaks framing on the wire, plain bytes to the
// python worker's stdio.
const localChannel = {
  onData: () => {}, onUp: () => {}, onDown: () => {}, onClosed: () => {},
  send: (text) => { // local -> remote
    if (dropLocalToRemote > 0) {
      dropLocalToRemote -= 1;
      return;
    }
    setImmediate(() => {
      let reply;
      try {
        reply = peerB.feed(Buffer.from(text, 'ascii'));
      } catch (err) {
        if (err instanceof CloseRequested) return;
        throw err;
      }
      if (reply && reply.length > 0) sendRemoteFrames(reply);
    });
  },
  close: () => {},
};

function sendRemoteFrames(buf) { // remote -> local, one framing line per message
  const text = buf.toString('ascii');
  let start = 0;
  while (start < text.length) {
    const nl = text.indexOf('\n', start);
    const line = nl === -1 ? text.slice(start) : text.slice(start, nl + 1);
    start = nl === -1 ? text.length : nl + 1;
    seenR2L += 1;
    // Arm the fault: after 6 wire messages (mid-big.dat: ~16 D-frames), drop
    // a burst in BOTH directions — the effect of a physical connection loss.
    if (armed && seenR2L === 6) {
      armed = false;
      dropRemoteToLocal = 4;
      dropLocalToRemote = 2;
    }
    if (dropRemoteToLocal > 0) {
      dropRemoteToLocal -= 1;
      droppedR2L += 1;
      continue;
    }
    const l = line;
    setImmediate(() => localChannel.onData(l));
  }
}

const peerB = new FramedPeer(
  (bytes) => child.stdin.write(bytes), // de-framed app bytes -> worker stdin
  () => { throw new CloseRequested(); },
);

// Shim-equivalent send-side flow control: pause worker stdout while the
// retain buffer holds >= SEND_WINDOW un-acked bytes (keeps every un-acked
// byte resendable — see main.py reader_loop comment).
child.stdout.on('data', (chunk) => {
  sendRemoteFrames(peerB.sendData(chunk));
  if (peerB.outRetain.buf.length >= SEND_WINDOW) {
    child.stdout.pause();
    const t = setInterval(() => {
      if (peerB.outRetain.buf.length < SEND_WINDOW) {
        clearInterval(t);
        child.stdout.resume();
      }
    }, 10);
  }
});

// --- run ------------------------------------------------------------------
const stream = new ResilientStream(localChannel);
const warnings = [];
const sync = new MirrorSync({ stream, root: localRoot, hydrationTimeoutMs: 60_000 });
sync.on('warning', (w) => warnings.push(w));

try {
  await sync.start();

  // fault must have actually fired mid-transfer
  assert.ok(droppedR2L >= 4, `expected dropped frames, got ${droppedR2L}`);

  // Test 1: hydration converged byte-exact despite the loss
  const got = readFileSync(join(localRoot, 'big.dat'));
  assert.equal(got.length, PAYLOAD.length, 'no truncation, no duplication');
  assert.equal(sha(got), sha(PAYLOAD), 'byte-exact content after resend recovery');
  assert.equal(readFileSync(join(localRoot, 'small.txt'), 'utf8'), 'small file');
  console.log(`Test 1 (hydration converges across mid-transfer loss, ${droppedR2L} frames dropped): PASS`);

  // Test 2: the channel is still healthy — live sync keeps working
  writeFileSync(join(remoteRoot, 'post-recovery.txt'), 'after the blip');
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline && !existsSync(join(localRoot, 'post-recovery.txt'))) await sleep(100);
  assert.equal(readFileSync(join(localRoot, 'post-recovery.txt'), 'utf8'), 'after the blip');
  console.log('Test 2 (live sync functional after recovery): PASS');

  console.log('mirror-reconnect.test.js: ALL PASS');
} finally {
  sync.close();
  child.kill('SIGTERM');
  for (const dir of [remoteRoot, localRoot]) rmSync(dir, { recursive: true, force: true });
}
process.exit(0);
