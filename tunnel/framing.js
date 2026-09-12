// framing.js — local-side mirror of image/scripts/sch-tunnel-helper.py's
// FramedPeer/RetainBuffer (sch-remote-ui-tunnel, design D2). MUST stay
// wire-compatible with the Python implementation: same frame format, same
// semantics. See the Python file's module docstring for the full protocol
// rationale (why a custom ack/resend layer on top of an already-a-byte-
// stream channel: the AgentCore CommandShell channel's own replay buffer
// can silently drop bytes across a reconnect — confirmed empirically, see
// design.md D1/D2).
//
// Wire format (one frame per line, newline-terminated, ASCII-safe):
//   D <offset> <base64>   -- data chunk starting at absolute byte <offset>
//                             in this frame's SENDER's outbound stream
//   A <offset>            -- cumulative ack: sender has durably delivered
//                             everything up to (not including) <offset> of
//                             the PEER's outbound stream to its target
//   R <offset>            -- resend request: please re-send starting at
//                             <offset> of YOUR outbound stream
//   H                     -- heartbeat
//   C                     -- close

// 45000 raw bytes -> ~60KB base64/frame, under AgentCore's 64KB per-WebSocket-
// frame quota (bedrock-agentcore-limits.html "WebSocket frame size 64 KB").
// LARGER frames are deliberately preferred: they minimize the frame COUNT, and
// the binding constraint for bulk transfer is the 250-frames/sec-per-connection
// rate limit (exceeding it makes AgentCore close the connection -> 1006
// reconnect storm, found in live verification). The shim additionally paces
// sends under that rate (main.py `_rate_limit`), and each frame is sent as its
// own WebSocket message (transport.js `_sendFramedBytes` / main.py
// `_iter_ws_frames`).
export const MAX_CHUNK = 45000;
export const RETAIN_BYTES = 262144; // 256KB client-side resend window
export const HEARTBEAT_INTERVAL_MS = 20_000;

export class CloseRequested extends Error {}

/** Tail buffer of recently-sent bytes, indexed by absolute stream offset. */
export class RetainBuffer {
  constructor() {
    this.startOffset = 0;
    this.buf = Buffer.alloc(0);
  }

  get endOffset() {
    return this.startOffset + this.buf.length;
  }

  append(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const overflow = this.buf.length - RETAIN_BYTES;
    if (overflow > 0) {
      this.buf = this.buf.subarray(overflow);
      this.startOffset += overflow;
    }
  }

  ack(offset) {
    if (offset > this.startOffset) {
      const drop = Math.min(offset - this.startOffset, this.buf.length);
      this.buf = this.buf.subarray(drop);
      this.startOffset += drop;
    }
  }

  /** Bytes from `offset` to end, or null if `offset` is out of the retained window. */
  sliceFrom(offset) {
    if (offset < this.startOffset || offset > this.endOffset) return null;
    return this.buf.subarray(offset - this.startOffset);
  }
}

/**
 * One side of the framed protocol. `onData(buf)` delivers de-framed
 * application bytes; `onClose()` is called when the peer sends a close
 * frame (by convention, throws CloseRequested — matches the Python side's
 * on_close contract, letting the caller unwind via a normal exception
 * rather than a special return value).
 */
export class FramedPeer {
  constructor(onData, onClose) {
    this.onData = onData;
    this.onClose = onClose;
    this.outOffset = 0;
    this.outRetain = new RetainBuffer();
    this.inOffset = 0;
    this._lastAckedOffset = 0;
    this._partial = Buffer.alloc(0);
    this.lastTx = Date.now();
    this.lastRx = Date.now();
    // True from the moment a frame beyond inOffset arrives (bytes are missing
    // in between) until inOffset advances again. While set, maybeHeartbeat()
    // repeats the resend request: a single `R` can itself be lost by the same
    // outage that lost the data, and a sender paused on its send window has
    // nothing further to send that would provoke another gap detection --
    // the transfer would stall until the orphan timeout.
    this.gapPending = false;
  }

  // --- outbound (target -> peer) -----------------------------------------
  sendData(chunk) {
    const frames = [];
    for (let i = 0; i < chunk.length; i += MAX_CHUNK) {
      const piece = chunk.subarray(i, i + MAX_CHUNK);
      frames.push(`D ${this.outOffset} ${piece.toString('base64')}\n`);
      this.outRetain.append(piece);
      this.outOffset += piece.length;
    }
    this.lastTx = Date.now();
    return Buffer.from(frames.join(''), 'ascii');
  }

  _sendDataRaw(startOffset, piece) {
    const frames = [];
    let off = startOffset;
    for (let i = 0; i < piece.length; i += MAX_CHUNK) {
      const sub = piece.subarray(i, i + MAX_CHUNK);
      frames.push(`D ${off} ${sub.toString('base64')}\n`);
      off += sub.length;
    }
    this.lastTx = Date.now();
    return Buffer.from(frames.join(''), 'ascii');
  }

  resendFrom(offset) {
    const piece = this.outRetain.sliceFrom(offset);
    if (piece === null) return null;
    return this._sendDataRaw(offset, piece);
  }

  sendAck() {
    this.lastTx = Date.now();
    return Buffer.from(`A ${this.inOffset}\n`, 'ascii');
  }

  /** Ask the peer to resend everything after the last byte we hold. Idempotent
   * on the receiving side (resendFrom replays the retained tail, an empty tail
   * when nothing is missing) and duplicates are dropped by _handleData, so it
   * is safe to repeat and safe to send speculatively after a reconnect. */
  requestResend() {
    this.lastTx = Date.now();
    return Buffer.from(`R ${this.inOffset}\n`, 'ascii');
  }

  sendHeartbeat() {
    this.lastTx = Date.now();
    return Buffer.from('H\n', 'ascii');
  }

  sendClose() {
    this.lastTx = Date.now();
    return Buffer.from('C\n', 'ascii');
  }

  maybeHeartbeat() {
    if (this.gapPending) return this.requestResend();
    if (Date.now() - this.lastTx >= HEARTBEAT_INTERVAL_MS) return this.sendHeartbeat();
    return Buffer.alloc(0);
  }

  // --- inbound (peer -> target) ------------------------------------------
  /** Consume raw bytes read from the transport; returns bytes to write back
   * (acks/resend requests generated while parsing, plus a trailing
   * cumulative ack whenever inOffset advanced). Throws CloseRequested if a
   * close frame was received (propagates from onClose, mirroring the
   * Python side's contract). */
  feed(raw) {
    this.lastRx = Date.now();
    this._partial = Buffer.concat([this._partial, raw]);
    const chunks = [];
    let nl;
    // eslint-disable-next-line no-cond-assign
    while ((nl = this._partial.indexOf(0x0a)) !== -1) {
      const line = this._partial.subarray(0, nl);
      this._partial = this._partial.subarray(nl + 1);
      chunks.push(this._handleLine(line));
    }
    if (this.inOffset > this._lastAckedOffset) {
      chunks.push(this.sendAck());
      this._lastAckedOffset = this.inOffset;
    }
    return Buffer.concat(chunks);
  }

  _handleLine(line) {
    let text;
    try {
      text = line.toString('ascii');
    } catch {
      return Buffer.alloc(0);
    }
    if (!text) return Buffer.alloc(0);
    const kind = text[0];
    // Phase 1: parse only. Any parse failure here means a corrupted/
    // malformed frame — discard it and resync on the next newline. This
    // must NOT catch exceptions from phase 2 (dispatch) below, notably
    // CloseRequested, which is meant to propagate to the caller.
    let offset = null;
    let payload = null;
    try {
      if (kind === 'D') {
        const firstSpace = text.indexOf(' ');
        const secondSpace = text.indexOf(' ', firstSpace + 1);
        offset = parseInt(text.slice(firstSpace + 1, secondSpace), 10);
        payload = Buffer.from(text.slice(secondSpace + 1), 'base64');
        if (Number.isNaN(offset)) return Buffer.alloc(0);
      } else if (kind === 'A' || kind === 'R') {
        offset = parseInt(text.slice(2), 10);
        if (Number.isNaN(offset)) return Buffer.alloc(0);
      } else if (kind === 'H' || kind === 'C') {
        // no fields to parse
      } else {
        return Buffer.alloc(0); // unknown frame type — resync tolerant: ignore
      }
    } catch {
      return Buffer.alloc(0); // malformed — discard this one frame, resync on next \n
    }

    // Phase 2: dispatch (may legitimately throw CloseRequested).
    if (kind === 'D') return this._handleData(offset, payload);
    if (kind === 'A') {
      this.outRetain.ack(offset);
      return Buffer.alloc(0);
    }
    if (kind === 'R') return this.resendFrom(offset) || Buffer.alloc(0);
    if (kind === 'H') return Buffer.alloc(0); // liveness only; lastRx already updated by feed()
    if (kind === 'C') {
      this.onClose();
      return Buffer.alloc(0);
    }
    return Buffer.alloc(0);
  }

  _handleData(offset, payload) {
    if (offset < this.inOffset) {
      // Fully/partially-duplicate bytes from a replay; deliver only the new tail.
      const skip = this.inOffset - offset;
      if (skip < payload.length) {
        this.onData(payload.subarray(skip));
        this.inOffset += payload.length - skip;
        this.gapPending = false;
      }
      return Buffer.alloc(0);
    }
    if (offset > this.inOffset) {
      // Gap: ask the sender to resend from what we actually have, and keep
      // asking on every heartbeat tick until the bytes arrive.
      this.gapPending = true;
      return this.requestResend();
    }
    this.onData(payload);
    this.inOffset += payload.length;
    this.gapPending = false;
    return Buffer.alloc(0);
  }

  orphaned(timeoutMs) {
    return Date.now() - this.lastRx >= timeoutMs;
  }
}
