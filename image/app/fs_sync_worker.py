"""fs_sync_worker.py — remote end of the `sch acp` file-locality sync
(sch-acp-editor-integration, design D4; capability acp-file-locality,
requirement "Continuous bidirectional sync without loss").

Spawned by the shim's tunnel handler when a client opens a tunnel with
``mode:"fs"`` (image/app/main.py `_open_tunnel_target`), as a subprocess
speaking this protocol on stdio — exactly like a ``mode:"exec"`` target,
except the argv is FIXED server-side (never client-controlled). Rooted at
the active workspace worktree. The local peer is tunnel/mirror.js on the operator's
laptop. Reliability (offset/ack/resend across physical reconnects) is
provided by the byte layer underneath (tunnel_framing.py / framing.js): a
reconnect of the same logical tunnel session resumes the byte stream with
no loss or duplication, so THIS protocol never needs its own retransmit.

PROTOCOL (fs-sync v1 and v2) — newline-delimited JSON, one message per line.
This comment is the protocol's canonical definition, shared with
tunnel/mirror.js (keep both in sync on any change).

Roles: "remote" (this worker, authoritative worktree) and "local" (the
mirror). Message types:

   {"v":1,"type":"hello","role":"remote"|"local","root":"<abs path>"}
       fs-sync v1 handshake. It retains the original best-effort transfer
       semantics for legacy ACP clients.

   {"v":2,"type":"hello","role":"remote"|"local","root":"<abs path>",
    "session":"<opaque id>","capabilities":["operation-ack","barrier",
    "post-barrier-manifest"]}
       fs-sync v2 handshake. The session id identifies one logical sync
       attempt. Both peers advertise capabilities; a client requiring v2
       MUST fail closed when the remote hello lacks all three capabilities.
       A v1 hello selects the separately-supported v1 path, rather than
       silently treating a legacy peer as a v2 peer.

  {"type":"manifest-request","git":bool,"ignore":[...],"maxBytes":N}
      local -> remote. Asks for the full manifest. git:true includes
      .git/** entries (one-time snapshot on first hydration — .git is
      NEVER watched live by either side afterwards); ignore/maxBytes
      propagate the local configuration (SCH_MIRROR_IGNORE /
      SCH_MIRROR_MAX_FILE_MB) so both sides apply the same policy.

  {"type":"manifest","files":[{"p","s","m","h","x"}...],"last":bool}
      remote -> local, batched (last:true on the final batch). Entry
      fields: p = '/'-separated path relative to root, s = size bytes,
      m = mtime in ms, h = 16-hex-char truncated sha1 of content,
      x = executable bit.

  {"type":"fetch","p":"rel/path"}
      local -> remote. Asks for one file's content (hydration pull). The
      remote answers with a file/put + data sequence (below). A fetch for
      a path that vanished meanwhile is answered with {"type":"file",
      "op":"del",...} so the puller does not wait forever.

   {"type":"file","op":"put","p","s","m","h","x"} then
  {"type":"data","p","seq":0..N,"b64":"...","last":bool}
      File transfer, either direction (hydration or live). Content is
      chunked (CHUNK_RAW bytes per data line, base64) so every line stays
      well under the 64KB WebSocket frame budget after framing. Transfers
      are NOT interleaved: a sender finishes one file's data before the
      next file/put. Application is atomic and idempotent: the receiver
      writes to a temp file in the same directory, sets mtime=m and the
      exec bit, then renames over the target; re-applying the same put
      converges to the same disk state. Zero-byte files send a single
      {"type":"data","seq":0,"b64":"","last":true}.

   {"type":"file","op":"del","p","m"}
      Deletion event (live watch, either direction), applied with
       missing-file tolerated (idempotent).

   v2 adds a monotonic operation id to mutating file messages:
   {"type":"file","id":"<session>:<sequence>","op":"put"|"del",...}
       The worker answers only after the mutation is applied with
       {"type":"applied","id":"...","ok":true,"op":"put"|"del","p":"..."}.
       An invalid transfer yields the same shape with ok:false and error. A
       replayed id returns its original acknowledgement without reapplying.

   {"v":2,"type":"barrier","id":"<session>:<sequence>"}
       v2 only. The worker processes this in stream order, after all prior
       operations. It replies with {"type":"barrier-ack","id":"...",
       "manifestHash":"<sha256>","fileCount":N}, where the hash is a
       SHA-256 of the canonical sorted post-apply manifest. Clients can
       request a manifest and calculate the same identity before committing
       a local baseline.

  {"type":"skip","p","s","reason":"size"|"symlink"|"ignored"}
      Advisory: sender excluded a file from sync (threshold/ignore/type).
      The receiver logs it (the local side surfaces it on stderr — spec
      scenario "File oltre soglia escluso").

  {"type":"bye"}
      Graceful shutdown announcement (best effort).

Hydration flow: local sends manifest-request -> remote sends manifest
batches -> local diffs against its own scan and (a) sends fetch for
remote-only / remote-newer files, (b) pushes put+data for local-only /
local-newer files. Ties on equal hash are no-ops. Hydration never DELETES
on either side (without a stored baseline, "deleted here" is
indistinguishable from "created there while disconnected"); deletions
propagate only as live del events within a session. Deliberate trade-off,
documented in docs/remote-access.md (Editor integration).

Live flow: this worker polls the worktree with an adaptive interval
(SCAN_INTERVAL_ACTIVE_S while file/tunnel activity was seen recently,
SCAN_INTERVAL_IDLE_S otherwise), diffs (size, mtime) against its last
snapshot (hashing only changed files), and pushes put/del events. Applied
incoming changes are recorded so the next scan does not echo them back
(hash-based echo suppression). Conflicts are last-writer-wins by event
mtime; the overwritten side is reported with an explicit warning by the
LOCAL peer (which owns operator-facing stderr).

Every diagnostic goes to stderr (the tunnel exec plumbing keeps it out of
the data stream).
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import queue
import stat as stat_mod
import sys
import threading
import time
from pathlib import Path

PROTO_VERSION = 2
LEGACY_PROTO_VERSION = 1
CAPABILITIES = ("operation-ack", "barrier", "post-barrier-manifest")
LEGACY_CAPABILITY = "legacy-v1"
CHUNK_RAW = 40000            # bytes per data line (b64 ~53KB < 64KB frame budget)
SCAN_INTERVAL_ACTIVE_S = 1.0
SCAN_INTERVAL_IDLE_S = 5.0
ACTIVE_WINDOW_S = 30.0       # how long after the last activity we stay "active"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
ENOSPC_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 2.0, 4.0)
DEFAULT_IGNORE = [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    "dist",
    "build",
    "coverage",
    "htmlcov",
    ".codebase-memory",
    "dummy_data",
]


def _log(msg: str) -> None:
    print(f"fs-sync: {msg}", file=sys.stderr, flush=True)


def hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def is_ignored(rel: str, ignore: list[str]) -> bool:
    """An ignore entry matches the whole relative path, a path prefix, or
    any single path segment (so "node_modules" matches at any depth)."""
    parts = rel.split("/")
    for entry in ignore:
        if not entry:
            continue
        if rel == entry or rel.startswith(entry + "/"):
            return True
        if entry in parts:
            return True
    return False


class FsSyncWorker:
    def __init__(self, root: Path, out=sys.stdout):
        self.root = root
        self.out = out
        self.out_lock = threading.Lock()
        self.ignore = list(DEFAULT_IGNORE)
        self.max_bytes = DEFAULT_MAX_BYTES
        # rel path -> (size, mtime_ms, hash16, exec) of the last state this
        # side considers synced (built by scans + applied incoming events).
        self.state: dict[str, tuple[int, int, str, bool]] = {}
        # rel path -> hash16 applied from an incoming event; consulted by
        # the scanner for echo suppression.
        self.applied: dict[str, str] = {}
        self.skip_warned: set[str] = set()
        self.last_activity = time.monotonic()
        self.incoming: "queue.Queue[dict|None]" = queue.Queue()
        self.pending_put: dict | None = None  # header of the file being received
        self.pending_chunks: list[bytes] = []
        self.pending_next_seq = 0
        self.pending_bytes = 0
        self.ignore_pending_data = False
        self.peer_version = LEGACY_PROTO_VERSION
        self.session_id: str | None = None
        # The reliable byte stream can replay after reconnect. v2 additionally
        # records operation results so a replay cannot apply a mutation twice.
        self.completed_ops: dict[str, dict] = {}
        self.closed = False

    # --- outbound ---------------------------------------------------------
    def send(self, msg: dict) -> None:
        with self.out_lock:
            self.out.write(json.dumps(msg, separators=(",", ":")) + "\n")
            self.out.flush()
        self.last_activity = time.monotonic()

    def send_file(self, rel: str) -> None:
        """Emit put + data chunks for one file (skips with advisory when it
        no longer qualifies)."""
        path = self.root / rel
        try:
            st = path.lstat()
        except FileNotFoundError:
            self.send({"type": "file", "op": "del", "p": rel, "m": int(time.time() * 1000)})
            return
        if stat_mod.S_ISLNK(st.st_mode):
            self.send({"type": "skip", "p": rel, "s": 0, "reason": "symlink"})
            return
        if st.st_size > self.max_bytes:
            self.send({"type": "skip", "p": rel, "s": st.st_size, "reason": "size"})
            return
        h = hash_file(path)
        mtime_ms = int(st.st_mtime * 1000)
        is_exec = bool(st.st_mode & 0o100)
        try:
            content = path.read_bytes()
        except OSError as exc:
            _log(f"cannot read {rel}: {exc}")
            return
        self.send({
            "type": "file", "op": "put", "p": rel, "s": len(content),
            "m": mtime_ms, "h": h, "x": is_exec,
        })
        seq = 0
        off = 0
        while True:
            chunk = content[off:off + CHUNK_RAW]
            off += len(chunk)
            self.send({
                "type": "data", "p": rel, "seq": seq,
                "b64": base64.b64encode(chunk).decode("ascii"),
                "last": off >= len(content),
            })
            seq += 1
            if off >= len(content):
                break
        self.state[rel] = (len(content), mtime_ms, h, is_exec)

    def _manifest_identity(self) -> tuple[str, int]:
        entries = self.scan(include_git=False)
        canonical = [
            {"p": p, "s": value[0], "h": value[2], "x": value[3]}
            for p, value in sorted(entries.items())
        ]
        encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), len(canonical)

    def _git_fingerprint(self) -> str | None:
        """Lightweight metadata identity for an advisory live-sync warning."""
        git = self.root / ".git"
        if not git.is_dir():
            return None
        digest = hashlib.sha256()
        files = [git / "HEAD", git / "packed-refs"]
        refs = git / "refs"
        if refs.is_dir():
            files.extend(sorted(path for path in refs.rglob("*") if path.is_file()))
        found = False
        for path in files:
            try:
                if not path.is_file():
                    continue
                digest.update(str(path.relative_to(git)).encode("utf-8"))
                digest.update(path.read_bytes())
                found = True
            except OSError:
                continue
        return digest.hexdigest() if found else None

    def _remember_result(self, op_id: str, result: dict) -> None:
        self.completed_ops[op_id] = result
        # Bound memory for long-running live sessions while keeping enough
        # recent history for reconnect replay.
        if len(self.completed_ops) > 4096:
            self.completed_ops.pop(next(iter(self.completed_ops)))

    def _ack_operation(self, header: dict, ok: bool, error: str | None = None) -> None:
        op_id = header.get("id")
        if self.peer_version < PROTO_VERSION or not isinstance(op_id, str) or not op_id:
            return
        result = {"type": "applied", "id": op_id, "ok": ok, "op": header.get("op"), "p": header.get("p")}
        if error:
            result["error"] = error
        self._remember_result(op_id, result)
        self.send(result)

    def _reject_pending_put(self, error: str) -> None:
        if self.pending_put is not None:
            self._ack_operation(self.pending_put, False, error)
        self.pending_put = None
        self.pending_chunks = []
        self.pending_next_seq = 0
        self.pending_bytes = 0
        self.ignore_pending_data = False

    # --- scanning ---------------------------------------------------------
    def scan(self, include_git: bool = False) -> dict[str, tuple[int, int, str, bool]]:
        """Walk the root, returning rel -> (size, mtime_ms, hash, exec) for
        every syncable file. Reuses cached hashes when (size, mtime) are
        unchanged from self.state."""
        result: dict[str, tuple[int, int, str, bool]] = {}
        if not self.root.is_dir():
            return result
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            # prune ignored dirs (always prune .git unless snapshotting)
            kept = []
            for d in dirnames:
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if rel == ".git" or (rel.split("/")[-1] == ".git"):
                    if include_git and rel == ".git":
                        kept.append(d)
                    continue
                if is_ignored(rel, self.ignore):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for name in filenames:
                if name.startswith(".sch-sync-"):
                    continue  # our own (or the peer's) apply temp files — never sync
                rel = f"{rel_dir}/{name}" if rel_dir else name
                in_git = rel == ".git" or rel.startswith(".git/")
                if in_git and not include_git:
                    continue
                if not in_git and is_ignored(rel, self.ignore):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.lstat()
                except FileNotFoundError:
                    continue
                if stat_mod.S_ISLNK(st.st_mode) or not stat_mod.S_ISREG(st.st_mode):
                    continue
                if st.st_size > self.max_bytes:
                    if rel not in self.skip_warned:
                        self.skip_warned.add(rel)
                        self.send({"type": "skip", "p": rel, "s": st.st_size, "reason": "size"})
                    continue
                mtime_ms = int(st.st_mtime * 1000)
                is_exec = bool(st.st_mode & 0o100)
                prev = self.state.get(rel)
                if prev is not None and prev[0] == st.st_size and prev[1] == mtime_ms:
                    result[rel] = (st.st_size, mtime_ms, prev[2], is_exec)
                else:
                    try:
                        result[rel] = (st.st_size, mtime_ms, hash_file(path), is_exec)
                    except OSError:
                        continue
        return result

    def scan_and_emit(self) -> None:
        """One live-scan pass: diff against self.state, push events."""
        current = self.scan(include_git=False)
        # .git entries in state (from the one-time snapshot) must not be
        # diffed against a scan that excludes them.
        baseline = {p: v for p, v in self.state.items() if not (p == ".git" or p.startswith(".git/"))}
        for rel, (size, mtime_ms, h, is_exec) in current.items():
            prev = baseline.get(rel)
            if prev is not None and prev[2] == h:
                if self.applied.get(rel) == h:
                    del self.applied[rel]  # echo settled — keep the map tidy
                if prev[3] != is_exec:  # pure mode change: re-send header via put
                    self.send_file(rel)
                continue
            if self.applied.get(rel) == h:
                # echo of a change WE applied from the peer — record, skip
                self.state[rel] = (size, mtime_ms, h, is_exec)
                del self.applied[rel]
                continue
            self.send_file(rel)
        for rel in list(baseline.keys()):
            if rel not in current:
                if self.applied.get(rel) == "<del>":
                    del self.applied[rel]
                    self.state.pop(rel, None)
                    continue
                self.send({"type": "file", "op": "del", "p": rel, "m": int(time.time() * 1000)})
                self.state.pop(rel, None)

    # --- inbound ----------------------------------------------------------
    def handle(self, msg: dict) -> None:
        t = msg.get("type")
        self.last_activity = time.monotonic()
        if t == "hello":
            version = msg.get("v", LEGACY_PROTO_VERSION)
            if version not in (LEGACY_PROTO_VERSION, PROTO_VERSION):
                self.send({"type": "error", "error": "unsupported-protocol", "supported": [1, 2]})
                return
            self.peer_version = version
            if version == PROTO_VERSION:
                session = msg.get("session")
                capabilities = set(msg.get("capabilities") or [])
                if not isinstance(session, str) or not session or not set(CAPABILITIES).issubset(capabilities):
                    self.send({"type": "error", "error": "missing-required-capabilities", "required": list(CAPABILITIES)})
                    return
                self.session_id = session
            return
        if t == "manifest-request":
            if isinstance(msg.get("ignore"), list):
                self.ignore = [str(e) for e in msg["ignore"]]
            if isinstance(msg.get("maxBytes"), int) and msg["maxBytes"] > 0:
                self.max_bytes = msg["maxBytes"]
            include_git = bool(msg.get("git"))
            entries = self.scan(include_git=include_git)
            self.state.update(entries)
            files = [
                {"p": p, "s": v[0], "m": v[1], "h": v[2], "x": v[3]}
                for p, v in sorted(entries.items())
            ]
            batch = 500
            if not files:
                self.send({"type": "manifest", "files": [], "last": True, "gitFingerprint": self._git_fingerprint()})
            for i in range(0, len(files), batch):
                self.send({
                    "type": "manifest",
                    "files": files[i:i + batch],
                    "last": i + batch >= len(files),
                    "gitFingerprint": self._git_fingerprint() if i + batch >= len(files) else None,
                })
            return
        if t == "fetch":
            rel = msg.get("p")
            if isinstance(rel, str) and self._safe_rel(rel):
                self.send_file(rel)
            return
        if t == "file":
            op = msg.get("op")
            rel = msg.get("p")
            if not isinstance(rel, str) or not self._safe_rel(rel):
                _log(f"rejected unsafe path from peer: {rel!r}")
                self._ack_operation(msg, False, "unsafe-path")
                return
            op_id = msg.get("id")
            if self.peer_version == PROTO_VERSION:
                if not isinstance(op_id, str) or not op_id.startswith(f"{self.session_id}:"):
                    self._ack_operation(msg, False, "invalid-operation-id")
                    return
                previous = self.completed_ops.get(op_id)
                if previous is not None:
                    self.send(previous)
                    self.ignore_pending_data = op == "put"
                    return
            if op == "del":
                try:
                    self._apply_del(rel)
                    self._ack_operation(msg, True)
                except OSError as exc:
                    self._ack_operation(msg, False, str(exc))
            elif op == "put":
                if self.pending_put is not None:
                    self._ack_operation(msg, False, "transfer-already-in-progress")
                    return
                self.pending_put = msg
                self.pending_chunks = []
                self.pending_next_seq = 0
                self.pending_bytes = 0
            else:
                self._ack_operation(msg, False, "unsupported-file-operation")
            return
        if t == "data":
            if self.ignore_pending_data:
                if msg.get("last"):
                    self.ignore_pending_data = False
                return
            if self.pending_put is None or msg.get("p") != self.pending_put.get("p"):
                _log(f"orphan data chunk for {msg.get('p')!r} — dropped")
                return
            if self.peer_version == PROTO_VERSION and msg.get("seq") != self.pending_next_seq:
                self._reject_pending_put("invalid-chunk-sequence")
                return
            try:
                chunk = base64.b64decode(msg.get("b64") or "", validate=True)
            except Exception:  # noqa: BLE001
                _log(f"undecodable data chunk for {msg.get('p')!r} — transfer aborted")
                self._reject_pending_put("invalid-base64")
                return
            self.pending_chunks.append(chunk)
            self.pending_next_seq += 1
            self.pending_bytes += len(chunk)
            if msg.get("last"):
                header = self.pending_put
                content = b"".join(self.pending_chunks)
                expected_size = header.get("s")
                expected_hash = header.get("h")
                if self.peer_version == PROTO_VERSION and (
                    not isinstance(expected_size, int)
                    or expected_size != self.pending_bytes
                    or not isinstance(expected_hash, str)
                    or expected_hash != hashlib.sha1(content).hexdigest()[:16]
                ):
                    self._reject_pending_put("content-verification-failed")
                    return
                self.pending_put = None
                self.pending_chunks = []
                self.pending_next_seq = 0
                self.pending_bytes = 0
                try:
                    applied = self._apply_put(header, content)
                    self._ack_operation(header, applied, None if applied else "conflict-not-applied")
                except OSError as exc:
                    self._ack_operation(header, False, f"apply {header.get('p')}: {exc}")
            return
        if t == "barrier":
            barrier_id = msg.get("id")
            if self.peer_version != PROTO_VERSION or not isinstance(barrier_id, str) or not barrier_id.startswith(f"{self.session_id}:"):
                self.send({"type": "error", "error": "barrier-not-supported"})
                return
            if self.pending_put is not None:
                self.send({"type": "barrier-ack", "id": barrier_id, "ok": False, "error": "transfer-in-progress"})
                return
            manifest_hash, file_count = self._manifest_identity()
            self.send({"type": "barrier-ack", "id": barrier_id, "ok": True, "manifestHash": manifest_hash, "fileCount": file_count})
            return
        if t == "skip":
            _log(f"peer skipped {msg.get('p')} (reason={msg.get('reason')}, size={msg.get('s')})")
            return
        if t == "bye":
            self.closed = True
            return
        _log(f"unknown message type {t!r} — ignored")

    def _safe_rel(self, rel: str) -> bool:
        if not rel or rel.startswith("/") or rel.startswith("~") or "\\" in rel:
            return False
        if ".." in rel.split("/"):
            return False
        try:
            root = self.root.resolve()
            target = (self.root / rel).resolve(strict=False)
            target.relative_to(root)
            return True
        except (OSError, ValueError):
            return False

    def _apply_put(self, header: dict, content: bytes) -> bool:
        rel = header["p"]
        h = header.get("h") or ""
        mtime_ms = int(header.get("m") or 0)
        is_exec = bool(header.get("x"))
        # Legacy v1 used LWW: if OUR copy changed after the incoming event's
        # mtime and differs, the newer side wins. The remote side stays
        # quiet on stderr policy decisions except a log line; the LOCAL
        # peer owns the operator-facing warning.
        path = self.root / rel
        if self.peer_version == LEGACY_PROTO_VERSION:
            try:
                st = path.lstat()
                ours_mtime = int(st.st_mtime * 1000)
                if ours_mtime > mtime_ms:
                    ours_hash = hash_file(path)
                    if ours_hash != h:
                        _log(f"LWW: kept newer local copy of {rel} (ours {ours_mtime} > incoming {mtime_ms}); re-announcing ours")
                        self.send_file(rel)
                        return False
            except FileNotFoundError:
                pass
        tmp = path.parent / f".sch-sync-{os.getpid()}-{threading.get_ident()}.tmp"
        for delay in (*ENOSPC_RETRY_DELAYS_S, None):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(content)
                if is_exec:
                    os.chmod(tmp, os.stat(tmp).st_mode | 0o111)
                os.utime(tmp, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
                os.replace(tmp, path)
                break
            except OSError as exc:
                # A failed atomic apply must not leave a temp file that becomes
                # a permanent artifact in the session workspace. The managed
                # session filesystem can transiently report ENOSPC during a
                # burst of small-file creates despite free capacity.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                if exc.errno != errno.ENOSPC or delay is None:
                    raise
                _log(f"transient ENOSPC applying {rel}; retrying in {delay}s")
                time.sleep(delay)
        self.state[rel] = (len(content), mtime_ms, h, is_exec)
        self.applied[rel] = h
        return True

    def _apply_del(self, rel: str) -> None:
        path = self.root / rel
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.state.pop(rel, None)
        self.applied[rel] = "<del>"

    # --- main loop ---------------------------------------------------------
    def run(self, stdin=sys.stdin) -> None:
        self.send({
            "v": PROTO_VERSION,
            "type": "hello",
            "role": "remote",
            "root": str(self.root),
            "capabilities": [*CAPABILITIES, LEGACY_CAPABILITY],
        })

        def reader() -> None:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.incoming.put(json.loads(line))
                except json.JSONDecodeError:
                    _log(f"non-JSON line from peer ignored: {line[:120]!r}")
            self.incoming.put(None)

        threading.Thread(target=reader, daemon=True).start()
        last_scan = 0.0
        while not self.closed:
            active = (time.monotonic() - self.last_activity) < ACTIVE_WINDOW_S
            interval = SCAN_INTERVAL_ACTIVE_S if active else SCAN_INTERVAL_IDLE_S
            timeout = max(0.05, interval - (time.monotonic() - last_scan))
            try:
                msg = self.incoming.get(timeout=timeout)
                if msg is None:
                    break
                self.handle(msg)
                continue  # drain queued messages before the next scan
            except queue.Empty:
                pass
            if time.monotonic() - last_scan >= interval:
                try:
                    self.scan_and_emit()
                except Exception as exc:  # noqa: BLE001
                    _log(f"scan error: {exc}")
                last_scan = time.monotonic()
        _log("worker exiting")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("SCH_WORKSPACE_ROOT", "/mnt/workspace") + "/repo")
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    FsSyncWorker(root).run()


if __name__ == "__main__":
    main()
