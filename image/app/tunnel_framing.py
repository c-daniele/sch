"""tunnel_framing.py — application-level reliability layer for
sch-remote-ui-tunnel (design D2). Shared by the shim's `@app.websocket`
handler (image/app/main.py) and mirrored in tunnel/framing.js on the local
bridge — the two MUST stay wire-compatible.

Originally written for a PTY-backed byte stream (see git history / design.md
"Cronologia" for the retired canale-shell approach); the wire format is
unchanged by the pivot to InvokeAgentRuntimeWithWebSocketStream — only the
transport carrying these frames changed (discrete WebSocket text messages
instead of lines on a raw-mode PTY). Kept as offset-tagged, ack'd,
resend-on-request rather than trusting the transport, because
InvokeAgentRuntimeWithWebSocketStream is not documented to guarantee
delivery across a reconnect (same reasoning as the retired canale-shell
approach, whose replay buffer was confirmed to drop bytes on reconnect).

Wire format (one frame per line, newline-terminated, ASCII-safe — a single
WebSocket message MAY carry one or more concatenated frames; the parser is
robust either way):
    D <offset> <base64>    -- data chunk starting at absolute byte <offset>
                              in this frame's SENDER's outbound stream
    A <offset>             -- cumulative ack: sender has durably delivered
                              everything up to (not including) <offset> of
                              the PEER's outbound stream to its target
    R <offset>             -- resend request: please re-send starting at
                              <offset> of YOUR outbound stream
    H                      -- heartbeat
    C                      -- close
"""

from __future__ import annotations

import base64
import time

MAX_CHUNK = 45000  # ~60KB base64/frame (< 64KB WS quota); large frames minimize
                   # frame COUNT to stay under the 250 frames/sec/connection limit
RETAIN_BYTES = 262144  # 256KB
HEARTBEAT_INTERVAL_S = 20


class CloseRequested(Exception):
    pass


class RetainBuffer:
    """Tail buffer of recently-sent bytes, indexed by absolute stream offset,
    so a resend request can be honored as long as it falls within the
    retained window."""

    def __init__(self) -> None:
        self.start_offset = 0
        self.buf = bytearray()

    @property
    def end_offset(self) -> int:
        return self.start_offset + len(self.buf)

    def append(self, data: bytes) -> None:
        self.buf.extend(data)
        overflow = len(self.buf) - RETAIN_BYTES
        if overflow > 0:
            del self.buf[:overflow]
            self.start_offset += overflow

    def ack(self, offset: int) -> None:
        if offset > self.start_offset:
            drop = min(offset - self.start_offset, len(self.buf))
            del self.buf[:drop]
            self.start_offset += drop

    def slice_from(self, offset: int) -> bytes | None:
        if offset < self.start_offset or offset > self.end_offset:
            return None
        return bytes(self.buf[offset - self.start_offset :])


class FramedPeer:
    """One side of the framed protocol. See tunnel/framing.js for the JS
    mirror used by the local bridge — keep both in sync on any wire-format
    change."""

    def __init__(self, on_data, on_close) -> None:
        self._on_data = on_data  # callback(bytes) -> deliver to target
        self._on_close = on_close  # callback() -> peer requested close (raises CloseRequested)
        self.out_offset = 0
        self.out_retain = RetainBuffer()
        self.in_offset = 0
        self._last_acked_offset = 0
        self._partial = b""
        self.last_tx = time.monotonic()
        self.last_rx = time.monotonic()

    # --- outbound (target -> peer) ---------------------------------------
    def send_data(self, chunk: bytes) -> bytes:
        out = bytearray()
        for i in range(0, len(chunk), MAX_CHUNK):
            piece = chunk[i : i + MAX_CHUNK]
            frame = f"D {self.out_offset} {base64.b64encode(piece).decode('ascii')}\n"
            out += frame.encode("ascii")
            self.out_retain.append(piece)
            self.out_offset += len(piece)
        self.last_tx = time.monotonic()
        return bytes(out)

    def resend_from(self, offset: int) -> bytes | None:
        piece = self.out_retain.slice_from(offset)
        if piece is None:
            return None
        return self.send_data_raw(offset, piece)

    def send_data_raw(self, start_offset: int, piece: bytes) -> bytes:
        out = bytearray()
        off = start_offset
        for i in range(0, len(piece), MAX_CHUNK):
            sub = piece[i : i + MAX_CHUNK]
            out += f"D {off} {base64.b64encode(sub).decode('ascii')}\n".encode("ascii")
            off += len(sub)
        self.last_tx = time.monotonic()
        return bytes(out)

    def send_ack(self) -> bytes:
        self.last_tx = time.monotonic()
        return f"A {self.in_offset}\n".encode("ascii")

    def send_heartbeat(self) -> bytes:
        self.last_tx = time.monotonic()
        return b"H\n"

    def send_close(self) -> bytes:
        self.last_tx = time.monotonic()
        return b"C\n"

    def maybe_heartbeat(self) -> bytes:
        if time.monotonic() - self.last_tx >= HEARTBEAT_INTERVAL_S:
            return self.send_heartbeat()
        return b""

    def orphaned(self, timeout_s: float) -> bool:
        return time.monotonic() - self.last_rx >= timeout_s

    # --- inbound (peer -> target) -----------------------------------------
    def feed(self, raw: bytes) -> bytes:
        self.last_rx = time.monotonic()
        self._partial += raw
        out = bytearray()
        while b"\n" in self._partial:
            line, self._partial = self._partial.split(b"\n", 1)
            out += self._handle_line(line)
        if self.in_offset > self._last_acked_offset:
            out += self.send_ack()
            self._last_acked_offset = self.in_offset
        return bytes(out)

    def _handle_line(self, line: bytes) -> bytes:
        try:
            text = line.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            return b""
        if not text:
            return b""
        kind = text[0]
        try:
            if kind == "D":
                _, off_s, b64 = text.split(" ", 2)
                offset = int(off_s)
                payload = base64.b64decode(b64)
            elif kind in ("A", "R"):
                _, off_s = text.split(" ", 1)
                offset = int(off_s)
                payload = None
            elif kind in ("H", "C"):
                offset = None
                payload = None
            else:
                return b""
        except Exception:  # noqa: BLE001 — malformed frame, discard & resync on next \n
            return b""

        if kind == "D":
            if offset < self.in_offset:
                skip = self.in_offset - offset
                if skip < len(payload):
                    self._on_data(payload[skip:])
                    self.in_offset += len(payload) - skip
                return b""
            if offset > self.in_offset:
                return f"R {self.in_offset}\n".encode("ascii")
            self._on_data(payload)
            self.in_offset += len(payload)
            return b""
        if kind == "A":
            self.out_retain.ack(offset)
            return b""
        if kind == "R":
            resent = self.resend_from(offset)
            return resent or b""
        if kind == "H":
            return b""
        if kind == "C":
            self._on_close()
            return b""
        return b""
