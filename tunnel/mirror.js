// mirror.js — local end of the `sch acp` file-locality sync
// (sch-acp-editor-integration, design D4; capability acp-file-locality).
// Peer of image/app/fs_sync_worker.py, which hosts the CANONICAL protocol
// definition (fs-sync v1/v2, newline-delimited JSON over the reliable framed
// byte stream) — keep the two in sync on any protocol change.
//
// Responsibilities (spec acp-file-locality):
//   - "Local per-workspace mirror of the worktree": full hydration on first
//     open (including the one-time .git snapshot), delta hydration via
//     manifest diff on later opens; per-file size threshold and ignore
//     list with stderr warnings;
//   - "Continuous bidirectional sync without loss": near-real-time apply of
//     remote events (atomic temp+rename), local fs.watch for the
//     local->remote direction, last-writer-wins on conflicts with an
//     explicit warning identifying the file and the overwritten side;
//   - reconnection safety comes for free from the byte layer (framing.js
//     offset/ack/resend across the SAME logical tunnel session) — this
//     protocol has no retransmit of its own.
//
// Hydration never deletes on either side (no stored baseline: "deleted
// here" and "created there while disconnected" are indistinguishable);
// deletions propagate only as live del events within a session. Deliberate
// trade-off, documented in docs/remote-access.md (Editor integration).

import {
  promises as fsp, watch, mkdirSync, existsSync,
} from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { EventEmitter } from 'node:events';
import { applyAtomicDelete, applyAtomicPut, hashBuffer, scanFiles } from './sync-files.js';

export const PROTO_VERSION = 1;
const LEGACY_CAPABILITY = 'legacy-v1';
export const CHUNK_RAW = 40000;
export const DEFAULT_MAX_BYTES = 10 * 1024 * 1024;
export const DEFAULT_IGNORE = [
  '.git',
  'node_modules',
  '.venv',
  'venv',
  '__pycache__',
  '.pytest_cache',
  '.ruff_cache',
  '.mypy_cache',
  '.cache',
  'dist',
  'build',
  'coverage',
  'htmlcov',
  '.codebase-memory',
  'dummy_data',
];
const WATCH_DEBOUNCE_MS = 150;
const HYDRATION_TIMEOUT_MS = 10 * 60 * 1000; // repo-sized first hydration can be minutes (docs/remote-access.md)

export function mirrorOptionsFromEnv(env = process.env) {
  const opts = {};
  if (env.SCH_MIRROR_IGNORE !== undefined) {
    opts.ignore = env.SCH_MIRROR_IGNORE.split(',').map((s) => s.trim()).filter(Boolean);
  }
  if (env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT !== undefined) {
    opts.includeGitSnapshot = env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT === '1';
  }
  const mb = Number.parseFloat(env.SCH_MIRROR_MAX_FILE_MB ?? '');
  if (Number.isFinite(mb) && mb > 0) opts.maxBytes = Math.floor(mb * 1024 * 1024);
  return opts;
}

export function isIgnored(rel, ignore) {
  const parts = rel.split('/');
  for (const entry of ignore) {
    if (!entry) continue;
    if (rel === entry || rel.startsWith(`${entry}/`)) return true;
    if (parts.includes(entry)) return true;
  }
  return false;
}

/**
 * MirrorSync — drives one fs-sync session over an already-open Duplex
 * (the fs-mode ResilientStream). Events:
 *   'ready'   -> hydration complete (mirror usable by the editor)
 *   'warning' -> operator-facing diagnostic string (caller -> stderr)
 *   'error'   -> fatal error (caller decides: degrade to chat-style)
 */
export class MirrorSync extends EventEmitter {
  constructor({
    stream, root, ignore = DEFAULT_IGNORE, maxBytes = DEFAULT_MAX_BYTES,
    hydrationTimeoutMs = HYDRATION_TIMEOUT_MS,
  }) {
    super();
    this.stream = stream;
    this.root = root;
    this.ignore = ignore;
    this.maxBytes = maxBytes;
    this.hydrationTimeoutMs = hydrationTimeoutMs;
    /** rel -> {s, m, h, x} last-synced state */
    this.state = new Map();
    /** rel -> hash applied from remote (watcher echo suppression); '<del>' for deletions */
    this.applied = new Map();
    this.skipWarned = new Set();
    this._partial = '';
    this._pendingPut = null;
    this._pendingChunks = [];
    this._remoteManifest = new Map();
    this._manifestDone = false;
    this._pendingFetches = new Set();
    this._helloSeen = false;
    this._readyEmitted = false;
    this._watcher = null;
    this._debounce = new Map();
    this._sendQueue = Promise.resolve(); // serializes local->remote transfers
    this._recvQueue = Promise.resolve(); // serializes incoming message handling (protocol order)
    this._tmpSeq = 0;
    this._closed = false;
  }

  // --- wire helpers -------------------------------------------------------
  _send(msg) {
    this.stream.write(`${JSON.stringify(msg)}\n`);
  }

  _warn(msg) {
    this.emit('warning', msg);
  }

  // --- lifecycle ----------------------------------------------------------
  /** Resolves when hydration is complete (also emits 'ready'). */
  start() {
    mkdirSync(this.root, { recursive: true });
    const wantGit = !existsSync(join(this.root, '.git'));
    this._send({ v: PROTO_VERSION, type: 'hello', role: 'local', root: this.root });
    this._send({
      type: 'manifest-request', git: wantGit, ignore: this.ignore, maxBytes: this.maxBytes,
    });

    this.stream.on('data', (buf) => this._onData(buf));
    this.stream.on('closed', (reason) => {
      if (!this._closed) this.emit('error', new Error(`fs channel closed: ${reason}`));
    });

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`hydration did not complete within ${this.hydrationTimeoutMs / 1000}s`));
      }, this.hydrationTimeoutMs);
      this.once('ready', () => {
        clearTimeout(timer);
        resolve();
      });
      this.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  close() {
    this._closed = true;
    if (this._watcher) {
      this._watcher.close();
      this._watcher = null;
    }
    for (const t of this._debounce.values()) clearTimeout(t);
    this._debounce.clear();
    try {
      this._send({ type: 'bye' });
    } catch {
      /* stream may be gone */
    }
  }

  // --- inbound ------------------------------------------------------------
  _onData(buf) {
    this._partial += buf.toString('utf8');
    const lines = this._partial.split('\n');
    this._partial = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        this._warn(`fs-sync: non-JSON line from remote ignored: ${line.slice(0, 120)}`);
        continue;
      }
      // Strictly in-order handling: put headers and their data chunks (and
      // interleavings between different files) must apply in protocol order.
      this._recvQueue = this._recvQueue
        .then(() => this._handle(msg))
        .catch((err) => this._warn(`fs-sync: apply error: ${err.message ?? err}`));
    }
  }

  async _handle(msg) {
    switch (msg.type) {
      case 'hello':
        if (msg.v === 1) {
          this._helloSeen = true;
          return;
        }
        // ACP deliberately remains a v1 client. A v2 worker must explicitly
        // advertise that it accepts v1; otherwise continuing would interpret
        // a capability mismatch as a working mirror.
        if (msg.v === 2 && (msg.capabilities ?? []).includes(LEGACY_CAPABILITY)) {
          this._helloSeen = true;
          this._warn('fs-sync: remote negotiated v2; ACP is using explicit legacy v1 compatibility');
          return;
        }
        this.emit('error', new Error(`fs-sync protocol mismatch: remote v${msg.v ?? '?'} does not support ACP legacy v1`));
        return;
      case 'manifest': {
        for (const f of msg.files ?? []) this._remoteManifest.set(f.p, f);
        if (msg.last) {
          this._manifestDone = true;
          await this._hydrate();
        }
        return;
      }
      case 'file': {
        if (!this._safeRel(msg.p)) {
          this._warn(`fs-sync: rejected unsafe path from remote: ${msg.p}`);
          return;
        }
        if (msg.op === 'del') {
          await this._applyDel(msg.p);
          this._settleFetch(msg.p);
        } else if (msg.op === 'put') {
          this._pendingPut = msg;
          this._pendingChunks = [];
        }
        return;
      }
      case 'data': {
        if (!this._pendingPut || msg.p !== this._pendingPut.p) {
          this._warn(`fs-sync: orphan data chunk for ${msg.p} — dropped`);
          return;
        }
        this._pendingChunks.push(Buffer.from(msg.b64 ?? '', 'base64'));
        if (msg.last) {
          const header = this._pendingPut;
          const content = Buffer.concat(this._pendingChunks);
          this._pendingPut = null;
          this._pendingChunks = [];
          await this._applyPut(header, content);
          this._settleFetch(header.p);
        }
        return;
      }
      case 'skip': {
        const rel = msg.p ?? '?';
        this._warn(`fs-sync: remote excluded '${rel}' from sync (reason=${msg.reason}, size=${msg.s ?? '?'})`);
        this._settleFetch(rel);
        return;
      }
      case 'bye':
        return;
      default:
        this._warn(`fs-sync: unknown message type '${msg.type}' ignored`);
    }
  }

  _settleFetch(rel) {
    if (this._pendingFetches.delete(rel)) this._maybeReady();
  }

  _maybeReady() {
    if (!this._readyEmitted && this._manifestDone && this._pendingFetches.size === 0) {
      this._readyEmitted = true;
      this._startWatcher();
      this.emit('ready');
    }
  }

  // --- hydration ----------------------------------------------------------
  async _scanLocal() {
    /** rel -> {s, m, h, x} — .git always excluded from the LOCAL scan (the
     * snapshot is receive-only; local .git edits never propagate) */
    return scanFiles(this.root, {
      ignore: (rel) => isIgnored(rel, this.ignore),
      maxBytes: this.maxBytes,
      onSkip: (rel, size, reason) => {
        if (reason === 'size' && !this.skipWarned.has(rel)) {
          this.skipWarned.add(rel);
          this._warn(`fs-sync: '${rel}' exceeds the per-file threshold (${size} bytes > ${this.maxBytes}) — excluded from sync`);
        }
      },
    });
  }

  async _hydrate() {
    const local = await this._scanLocal();
    const pushes = [];
    for (const [rel, remote] of this._remoteManifest) {
      const mine = local.get(rel);
      const isGit = rel === '.git' || rel.startsWith('.git/');
      if (mine === undefined) {
        if (isGit || !existsSync(join(this.root, ...rel.split('/')))) {
          this._pendingFetches.add(rel);
        }
      } else if (mine.h !== remote.h) {
        if (mine.m > remote.m) {
          pushes.push(rel); // local newer -> local wins (LWW)
        } else {
          this._pendingFetches.add(rel); // remote newer or tie -> remote wins
        }
      } else {
        this.state.set(rel, remote);
      }
    }
    for (const [rel, mine] of local) {
      if (!this._remoteManifest.has(rel)) pushes.push(rel);
      void mine;
    }
    for (const rel of this._pendingFetches) this._send({ type: 'fetch', p: rel });
    for (const rel of pushes) this._queueSendFile(rel);
    this._maybeReady();
  }

  // --- applying remote events ----------------------------------------------
  _safeRel(rel) {
    if (typeof rel !== 'string' || rel.length === 0) return false;
    if (rel.startsWith('/') || rel.startsWith('~')) return false;
    return !rel.split('/').includes('..');
  }

  async _applyPut(header, content) {
    const rel = header.p;
    const abs = join(this.root, ...rel.split('/'));
    const isGit = rel === '.git' || rel.startsWith('.git/');
    // Live LWW (spec scenario "Concurrent conflict on the same file"):
    // if the local copy is NEWER than the incoming event and differs, the
    // local side wins; announce ours back. Otherwise incoming wins — and
    // when it overwrites a local edit the remote never saw, say so.
    if (!isGit) {
      try {
        const st = await fsp.lstat(abs);
        const localM = Math.floor(st.mtimeMs);
        if (localM > header.m) {
          const ours = hashBuffer(await fsp.readFile(abs));
          if (ours !== header.h) {
            this._warn(`fs-sync: CONFLICT on '${rel}': keeping the newer LOCAL copy (last-writer-wins); the remote change was overwritten`);
            this._queueSendFile(rel);
            return;
          }
        } else {
          const prev = this.state.get(rel);
          if (prev) {
            const ours = hashBuffer(await fsp.readFile(abs));
            if (ours !== prev.h && ours !== header.h) {
              this._warn(`fs-sync: CONFLICT on '${rel}': the REMOTE change is newer (last-writer-wins); your unsynced local edit was overwritten`);
            }
          }
        }
      } catch {
        /* no local copy — plain create */
      }
    }
    this._tmpSeq += 1;
    await applyAtomicPut(this.root, header, content, this._tmpSeq);
    this.state.set(rel, {
      s: content.length, m: header.m, h: header.h, x: !!header.x,
    });
    this.applied.set(rel, header.h);
  }

  async _applyDel(rel) {
    await applyAtomicDelete(this.root, rel);
    this.state.delete(rel);
    this.applied.set(rel, '<del>');
  }

  // --- local -> remote ------------------------------------------------------
  _queueSendFile(rel) {
    this._sendQueue = this._sendQueue
      .then(() => this._sendFile(rel))
      .catch((err) => this._warn(`fs-sync: failed to send '${rel}': ${err.message ?? err}`));
    return this._sendQueue;
  }

  async _sendFile(rel) {
    const abs = join(this.root, ...rel.split('/'));
    let st;
    try {
      st = await fsp.lstat(abs);
    } catch {
      this._send({ type: 'file', op: 'del', p: rel, m: Date.now() });
      this.state.delete(rel);
      return;
    }
    if (st.isSymbolicLink()) {
      this._send({ type: 'skip', p: rel, s: 0, reason: 'symlink' });
      return;
    }
    if (st.size > this.maxBytes) {
      if (!this.skipWarned.has(rel)) {
        this.skipWarned.add(rel);
        this._warn(`fs-sync: '${rel}' exceeds the per-file threshold (${st.size} bytes > ${this.maxBytes}) — excluded from sync`);
      }
      this._send({ type: 'skip', p: rel, s: st.size, reason: 'size' });
      return;
    }
    const content = await fsp.readFile(abs);
    const h = hashBuffer(content);
    const m = Math.floor(st.mtimeMs);
    const x = (st.mode & 0o100) !== 0;
    this._send({
      type: 'file', op: 'put', p: rel, s: content.length, m, h, x,
    });
    let seq = 0;
    let off = 0;
    do {
      const chunk = content.subarray(off, off + CHUNK_RAW);
      off += chunk.length;
      this._send({
        type: 'data', p: rel, seq, b64: chunk.toString('base64'), last: off >= content.length,
      });
      seq += 1;
    } while (off < content.length);
    this.state.set(rel, { s: content.length, m, h, x });
  }

  // --- watcher ---------------------------------------------------------------
  _startWatcher() {
    try {
      this._watcher = watch(this.root, { recursive: true }, (_event, filename) => {
        if (!filename) return;
        const rel = filename.split(sep).join('/');
        if (rel === '.git' || rel.startsWith('.git/')) return;
        if (rel.split('/').pop().startsWith('.sch-sync-')) return; // our own temp files
        if (isIgnored(rel, this.ignore)) return;
        const prev = this._debounce.get(rel);
        if (prev) clearTimeout(prev);
        this._debounce.set(rel, setTimeout(() => {
          this._debounce.delete(rel);
          this._onLocalChange(rel).catch((err) => this._warn(`fs-sync: watch error on '${rel}': ${err.message ?? err}`));
        }, WATCH_DEBOUNCE_MS));
      });
    } catch (err) {
      this._warn(`fs-sync: cannot start local watcher (${err.message ?? err}); local edits will NOT propagate`);
    }
  }

  async _onLocalChange(rel) {
    if (this._closed) return;
    const abs = join(this.root, ...rel.split('/'));
    let st = null;
    try {
      st = await fsp.lstat(abs);
    } catch {
      /* deleted */
    }
    if (st === null) {
      if (this.applied.get(rel) === '<del>') {
        this.applied.delete(rel); // echo of a remote deletion we applied
        return;
      }
      if (this.state.has(rel)) {
        this._send({ type: 'file', op: 'del', p: rel, m: Date.now() });
        this.state.delete(rel);
      }
      return;
    }
    if (st.isDirectory() || st.isSymbolicLink()) return;
    if (st.size > this.maxBytes) {
      if (!this.skipWarned.has(rel)) {
        this.skipWarned.add(rel);
        this._warn(`fs-sync: '${rel}' exceeds the per-file threshold (${st.size} bytes > ${this.maxBytes}) — excluded from sync`);
      }
      return;
    }
    const content = await fsp.readFile(abs);
    const h = hashBuffer(content);
    if (this.applied.get(rel) === h) {
      this.applied.delete(rel); // echo of a remote change we just applied
      return;
    }
    const prev = this.state.get(rel);
    if (prev && prev.h === h) return; // no content change (e.g. touch)
    await this._queueSendFile(rel);
  }
}
