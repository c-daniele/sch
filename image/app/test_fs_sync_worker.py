"""test_fs_sync_worker.py — offline unit tests for the fs-sync remote
worker (sch-acp-editor-integration task 4.5). Plain-assert style, zero
test framework (consistent with the repo's tunnel/*.test.js scripts):

    python3 image/app/test_fs_sync_worker.py

Covers: scan/manifest, ignore semantics, atomic+idempotent apply, mtime/
exec preservation, LWW re-announce, path traversal rejection, size
threshold notices, echo suppression of applied events.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from fs_sync_worker import DEFAULT_IGNORE, FsSyncWorker, is_ignored  # noqa: E402


class Capture(io.StringIO):
    """stdout stand-in collecting parsed JSON messages."""

    def messages(self):
        return [json.loads(line) for line in self.getvalue().splitlines() if line.strip()]

    def clear(self):
        self.truncate(0)
        self.seek(0)


def make_worker(root: Path) -> tuple[FsSyncWorker, Capture]:
    out = Capture()
    return FsSyncWorker(root, out=out), out


def put_file(w: FsSyncWorker, rel: str, content: bytes, mtime_ms: int, is_exec=False):
    h = __import__("hashlib").sha1(content).hexdigest()[:16]
    w.handle({"type": "file", "op": "put", "p": rel, "s": len(content), "m": mtime_ms, "h": h, "x": is_exec})
    w.handle({"type": "data", "p": rel, "seq": 0, "b64": base64.b64encode(content).decode(), "last": True})
    return h


def v2_hello(w: FsSyncWorker, session="session-1"):
    w.handle({
        "v": 2,
        "type": "hello",
        "role": "local",
        "root": "/local",
        "session": session,
        "capabilities": ["operation-ack", "barrier", "post-barrier-manifest"],
    })
    return session


# Test 1: is_ignored semantics (whole path, prefix, any segment)
assert is_ignored(".git", [".git"])
assert is_ignored(".git/config", [".git"])
assert is_ignored("a/node_modules/x.js", ["node_modules"])
assert not is_ignored("src/gitlog.txt", [".git"])
assert not is_ignored("srcx/file", ["src"])
assert is_ignored("src/file", ["src"])
assert is_ignored("lambda/api_handler/.venv/bin/python", DEFAULT_IGNORE)
assert is_ignored("frontend/dist/assets/app.js", DEFAULT_IGNORE)
assert not is_ignored("agentcore/app/.env", DEFAULT_IGNORE)
print("Test 1 (is_ignored semantics): PASS")

# Test 2: manifest-request -> batched manifest with hashes; .git only when git:true
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_bytes(b"alpha")
    (root / "b.txt").write_bytes(b"beta")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main")
    w, out = make_worker(root)
    w.handle({"type": "manifest-request", "git": False, "ignore": [".git"], "maxBytes": 1024 * 1024})
    msgs = [m for m in out.messages() if m["type"] == "manifest"]
    assert msgs and msgs[-1]["last"] is True
    paths = {f["p"] for m in msgs for f in m["files"]}
    assert paths == {"src/a.txt", "b.txt"}, paths
    out.clear()
    w2, out2 = make_worker(root)
    w2.handle({"type": "manifest-request", "git": True, "ignore": [".git"], "maxBytes": 1024 * 1024})
    paths2 = {f["p"] for m in out2.messages() if m["type"] == "manifest" for f in m["files"]}
    assert ".git/HEAD" in paths2, paths2  # one-time snapshot includes .git
    print("Test 2 (manifest incl/excl .git snapshot): PASS")

# Test 3: apply put — atomic (no temp leftovers), mtime + exec preserved, idempotent
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    mtime = int(time.time() * 1000) - 5000
    put_file(w, "bin/run.sh", b"#!/bin/sh\necho hi\n", mtime, is_exec=True)
    target = root / "bin" / "run.sh"
    assert target.read_bytes() == b"#!/bin/sh\necho hi\n"
    st = target.stat()
    assert int(st.st_mtime * 1000) == mtime, (int(st.st_mtime * 1000), mtime)
    assert st.st_mode & 0o100
    assert not [p for p in (root / "bin").iterdir() if p.name.startswith(".sch-sync-")]
    put_file(w, "bin/run.sh", b"#!/bin/sh\necho hi\n", mtime, is_exec=True)  # re-apply
    assert target.read_bytes() == b"#!/bin/sh\necho hi\n"
print("Test 3 (atomic idempotent apply + mtime/exec): PASS")

# Test 3b: failed atomic apply leaves no temp artifact behind.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, _ = make_worker(root)
    target = root / "dir" / "file.txt"
    target.parent.mkdir()
    with patch("fs_sync_worker.ENOSPC_RETRY_DELAYS_S", (0, 0)), patch(
        "fs_sync_worker.os.replace", side_effect=OSError(28, "No space left on device")
    ):
        try:
            w._apply_put({"p": "dir/file.txt", "h": "hash", "m": 0, "x": False}, b"content")
        except OSError as exc:
            assert exc.errno == 28
        else:
            assert False, "failed atomic replace must propagate"
    assert not list(target.parent.glob(".sch-sync-*.tmp"))
    print("Test 3b (failed atomic apply cleans temp): PASS")

# Test 3c: apply errors report the affected path to the sync client.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    v2_hello(w)
    content = b"content"
    with patch.object(w, "_apply_put", side_effect=OSError(28, "No space left on device")):
        w.handle({
            "type": "file", "id": "session-1:1", "op": "put", "p": "dir/file.txt",
            "s": len(content), "m": int(time.time() * 1000),
            "h": __import__("hashlib").sha1(content).hexdigest()[:16], "x": False,
        })
        w.handle({"type": "data", "p": "dir/file.txt", "seq": 0,
                  "b64": base64.b64encode(content).decode(), "last": True})
    failed = [m for m in out.messages() if m["type"] == "applied" and not m["ok"]]
    assert failed and "apply dir/file.txt" in failed[-1]["error"], failed
    print("Test 3c (apply error identifies path): PASS")

# Test 3d: transient ENOSPC retries the atomic replace and completes.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, _ = make_worker(root)
    original_replace = os.replace
    attempts = 0

    def transient_enospc(source, destination):
        nonlocal_attempts[0] += 1
        if nonlocal_attempts[0] < 3:
            raise OSError(28, "No space left on device")
        return original_replace(source, destination)

    nonlocal_attempts = [attempts]
    with patch("fs_sync_worker.ENOSPC_RETRY_DELAYS_S", (0, 0)), patch("fs_sync_worker.os.replace", side_effect=transient_enospc):
        assert w._apply_put({"p": "dir/file.txt", "h": "hash", "m": 0, "x": False}, b"content")
    assert (root / "dir" / "file.txt").read_bytes() == b"content"
    assert nonlocal_attempts[0] == 3
    print("Test 3d (transient ENOSPC retries): PASS")

# Test 3e: transient ENOSPC while creating a new parent directory retries too.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, _ = make_worker(root)
    original_mkdir = Path.mkdir
    attempts = [0]

    def transient_enospc_mkdir(self, *args, **kwargs):
        if self == root / "new" / "nested":
            attempts[0] += 1
            if attempts[0] < 3:
                raise OSError(28, "No space left on device")
        return original_mkdir(self, *args, **kwargs)

    with patch("fs_sync_worker.ENOSPC_RETRY_DELAYS_S", (0, 0)), patch.object(Path, "mkdir", transient_enospc_mkdir):
        assert w._apply_put({"p": "new/nested/file.txt", "h": "hash", "m": 0, "x": False}, b"content")
    assert (root / "new" / "nested" / "file.txt").read_bytes() == b"content"
    assert attempts[0] >= 3
    print("Test 3e (transient ENOSPC retries parent creation): PASS")

# Test 4: LWW — incoming put OLDER than a differing local copy is refused and
# the local copy is re-announced
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    local = root / "f.txt"
    local.write_bytes(b"local newer content")
    now_ms = int(local.stat().st_mtime * 1000)
    out.clear()
    put_file(w, "f.txt", b"stale remote content", now_ms - 60_000)
    assert local.read_bytes() == b"local newer content"  # kept
    reann = [m for m in out.messages() if m["type"] == "file" and m.get("op") == "put" and m["p"] == "f.txt"]
    assert reann, out.messages()  # our copy re-announced toward the peer
    print("Test 4 (LWW keeps newer local + re-announce): PASS")

# Test 5: newer incoming wins over older local copy
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    local = root / "f.txt"
    local.write_bytes(b"old local")
    old = (int(time.time()) - 3600)
    os.utime(local, (old, old))
    put_file(w, "f.txt", b"fresh remote", int(time.time() * 1000))
    assert local.read_bytes() == b"fresh remote"
    print("Test 5 (LWW newer incoming applies): PASS")

# Test 6: unsafe paths rejected (traversal / absolute)
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    w.handle({"type": "file", "op": "put", "p": "../evil.txt", "s": 1, "m": 0, "h": "x", "x": False})
    w.handle({"type": "data", "p": "../evil.txt", "seq": 0, "b64": base64.b64encode(b"!").decode(), "last": True})
    w.handle({"type": "file", "op": "del", "p": "/etc/passwd"})
    w.handle({"type": "file", "op": "put", "p": "dir\\evil.txt", "s": 1, "m": 0, "h": "x", "x": False})
    assert not (root.parent / "evil.txt").exists()
    print("Test 6 (unsafe path rejection): PASS")

# Test 7: scan_and_emit — new/changed file pushed, deletion emitted, applied
# events echo-suppressed
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    (root / "watched.txt").write_bytes(b"v1")
    w.scan_and_emit()
    sent = out.messages()
    assert any(m["type"] == "file" and m.get("op") == "put" and m["p"] == "watched.txt" for m in sent)
    assert any(m["type"] == "data" and m["p"] == "watched.txt" for m in sent)
    out.clear()
    w.scan_and_emit()  # steady state: nothing new
    assert not [m for m in out.messages() if m["type"] == "file"], out.messages()
    # applied incoming event must NOT echo back on the next scan
    h = put_file(w, "incoming.txt", b"from peer", int(time.time() * 1000))
    out.clear()
    w.scan_and_emit()
    assert not [m for m in out.messages() if m.get("p") == "incoming.txt"], out.messages()
    assert w.applied.get("incoming.txt") is None  # consumed
    # deletion propagates
    (root / "watched.txt").unlink()
    out.clear()
    w.scan_and_emit()
    dels = [m for m in out.messages() if m["type"] == "file" and m.get("op") == "del"]
    assert [m["p"] for m in dels] == ["watched.txt"], out.messages()
    print("Test 7 (scan diff + echo suppression + del): PASS")

# Test 8: per-file size threshold -> skip notice, once
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    w.max_bytes = 10
    (root / "big.bin").write_bytes(b"x" * 100)
    (root / "ok.txt").write_bytes(b"y")
    w.scan_and_emit()
    msgs = out.messages()
    assert any(m["type"] == "skip" and m["p"] == "big.bin" and m["reason"] == "size" for m in msgs)
    assert any(m.get("p") == "ok.txt" and m["type"] == "file" for m in msgs)
    out.clear()
    w.scan_and_emit()
    assert not [m for m in out.messages() if m["type"] == "skip"], "skip must be warned once"
    print("Test 8 (size threshold skip-once): PASS")

# Test 9: zero-byte file round-trips (single empty data chunk)
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    (root / "empty.txt").write_bytes(b"")
    w.send_file("empty.txt")
    msgs = out.messages()
    datas = [m for m in msgs if m["type"] == "data"]
    assert len(datas) == 1 and datas[0]["last"] is True and datas[0]["b64"] == ""
    w2root = Path(td) / "peer"
    w2root.mkdir()
    w2, _ = make_worker(w2root)
    hdr = [m for m in msgs if m["type"] == "file"][0]
    w2.handle(hdr)
    w2.handle(datas[0])
    assert (w2root / "empty.txt").read_bytes() == b""
    print("Test 9 (zero-byte file): PASS")

# Test 10: v2 applies identified operations once, acknowledges only after
# application, and exposes a verifiable post-barrier manifest identity.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    session = v2_hello(w)
    content = b"verified"
    h = __import__("hashlib").sha1(content).hexdigest()[:16]
    header = {"type": "file", "id": f"{session}:1", "op": "put", "p": "v2.txt", "s": len(content), "m": 1, "h": h, "x": False}
    w.handle(header)
    w.handle({"type": "data", "p": "v2.txt", "seq": 0, "b64": base64.b64encode(content).decode(), "last": True})
    ack = [m for m in out.messages() if m["type"] == "applied"][-1]
    assert ack == {"type": "applied", "id": f"{session}:1", "ok": True, "op": "put", "p": "v2.txt"}
    out.clear()
    w.handle(header)  # replay after reconnect: acknowledgement, no second apply
    replay = out.messages()
    assert replay == [ack], replay
    out.clear()
    w.handle({"type": "barrier", "id": f"{session}:2"})
    barrier = out.messages()
    assert barrier[0]["type"] == "barrier-ack" and barrier[0]["ok"] is True
    expected_hash, expected_count = w._manifest_identity()
    assert barrier[0]["manifestHash"] == expected_hash and barrier[0]["fileCount"] == expected_count
    print("Test 10 (v2 ack replay + verified barrier): PASS")

# Test 11: a failed apply is acknowledged and cannot advance a barrier as a
# successful mutation. Deletion remains idempotent and acknowledged.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    session = v2_hello(w)
    bad = {"type": "file", "id": f"{session}:1", "op": "put", "p": "bad.txt", "s": 2, "m": 1, "h": "not-a-hash", "x": False}
    w.handle(bad)
    w.handle({"type": "data", "p": "bad.txt", "seq": 0, "b64": base64.b64encode(b"no").decode(), "last": True})
    assert out.messages()[-1]["ok"] is False
    assert not (root / "bad.txt").exists()
    out.clear()
    delete = {"type": "file", "id": f"{session}:2", "op": "del", "p": "missing.txt", "m": 1}
    w.handle(delete)
    assert out.messages() == [{"type": "applied", "id": f"{session}:2", "ok": True, "op": "del", "p": "missing.txt"}]
    print("Test 11 (v2 apply failure + idempotent delete): PASS")

# Test 12: a disconnect during a transfer does not apply a partial file or
# produce a successful acknowledgement that could let a caller advance state.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    w, out = make_worker(root)
    session = v2_hello(w)
    w.handle({"type": "file", "id": f"{session}:1", "op": "put", "p": "partial.txt", "s": 4, "m": 1, "h": "0000000000000000", "x": False})
    w.handle({"type": "data", "p": "partial.txt", "seq": 0, "b64": base64.b64encode(b"pa").decode(), "last": False})
    w.handle({"type": "bye"})
    assert w.closed and not (root / "partial.txt").exists()
    assert not [m for m in out.messages() if m["type"] == "applied"]
    print("Test 12 (disconnect leaves partial transfer unapplied): PASS")

print("test_fs_sync_worker.py: ALL PASS")
