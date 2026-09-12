// transport.js — abstract byte-stream transport contract (sch-remote-ui-
// tunnel, design D3) and its implementation over
// `InvokeAgentRuntimeWithWebSocketStream` (post-D9 pivot — see design.md
// "Nota di revisione"). Adapters (acp.js, attach.js) depend ONLY on
// ResilientStream — an ordered, reliable Duplex byte stream with {up,
// down, closed(reason, exitInfo)} events — never on tunnel_id/WebSocket
// details. This same contract survived a full transport swap already
// (canale shell -> InvokeAgentRuntimeWithWebSocketStream) without any
// adapter change, which is the whole point of D3.
//
// target = { region, runtimeArn, sessionId, remote }
//   remote = { kind: "tcp", port } | { kind: "exec", argv }

import { Duplex } from 'node:stream';
import { FramedPeer, CloseRequested } from './framing.js';
import { WebSocketChannel } from './websocket-stream-channel.js';

const ACK_CHECK_INTERVAL_MS = 5000;
// Send-side flow-control window (local mirror of main.py's
// _TUNNEL_SEND_WINDOW, sch-remote-ui-tunnel v20 fix — same reasoning, other
// direction): never let un-acked outbound bytes exceed this, or the retain
// buffer (256KB) trims bytes a post-reconnect resend still needs and the
// offset tracking desyncs permanently. Became load-bearing client-side with
// the first bulk local->remote transfer (`sch acp` mirror push,
// sch-acp-editor-integration — found in live verification: multi-MB pushes
// vanished across forced reconnects).
const SEND_WINDOW = 128 * 1024; // < framing RETAIN_BYTES (256KB)

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * A ResilientStream is a Node Duplex: write() sends application bytes to
 * the remote target, readable 'data' events deliver bytes coming back from
 * it. Also emits 'up', 'down' (attemptNum, reason), and 'closed' (reason,
 * exitInfo).
 *
 * IMPORTANT: constructs exactly ONE FramedPeer for its whole lifetime and
 * never recreates it on reconnect — the peer's offset/retain-buffer state
 * MUST survive a physical WebSocket reconnect (the shim's `_TunnelSession`
 * on the other end does the same). Getting this wrong (e.g. a fresh peer
 * per physical connection) desyncs the offset tracking and was caught by
 * this session's own end-to-end testing — see websocket-stream-channel.js
 * file header.
 */
class ResilientStream extends Duplex {
  constructor(channel) {
    super();
    this._channel = channel;
    this._peer = new FramedPeer(
      (buf) => this.push(buf),
      () => {
        throw new CloseRequested();
      },
    );
    this._closed = false;

    channel.onData = (text) => this._onChannelData(text);
    channel.onUp = () => {
      // A *re*connection (never the first connect, whose byte stream stays
      // untouched): ask the remote to resend anything after the last byte we
      // hold. Frames handed to a socket that died silently never enter the
      // shim's pending_out buffer, so nothing else would replay them; when
      // nothing is missing the remote's retained tail is empty and this is a
      // no-op. Spec remote-ui-tunnel R10.
      if (this._everUp && !this._closed) this._sendFramedBytes(this._peer.requestResend());
      this._everUp = true;
      this.emit('up');
    };
    this._everUp = false;
    channel.onDown = (attempt, reason) => this.emit('down', attempt, reason);
    channel.onClosed = (reason) => this._onChannelClosed(reason);

    this._pendingExitInfo = null;
    this._ackTimer = setInterval(() => this._sendMaybeHeartbeat(), ACK_CHECK_INTERVAL_MS);
  }

  _onChannelData(text) {
    // Lines are either framing.js protocol frames (D/A/R/H/C) or a plain
    // JSON control message (task 6.5: {"type":"exit","code":N} — sent by
    // the shim right before the framed close, when the remote process
    // exited on its own). Distinguish by first char: '{' never starts a
    // valid framing.js frame type, so this is unambiguous and needs no
    // change to framing.js itself.
    for (const line of text.split('\n')) {
      if (!line) continue;
      if (line[0] === '{') this._handleControlLine(line);
      else this._feedFramingLine(`${line}\n`);
    }
  }

  _handleControlLine(line) {
    try {
      const msg = JSON.parse(line);
      if (msg.type === 'exit') this._pendingExitInfo = { exitCode: msg.code, signal: null };
    } catch {
      /* malformed control line — ignore, not fatal */
    }
  }

  _feedFramingLine(line) {
    let reply;
    try {
      reply = this._peer.feed(Buffer.from(line, 'ascii'));
    } catch (err) {
      if (err instanceof CloseRequested) {
        this._finish('remote requested close', this._pendingExitInfo);
        return;
      }
      throw err;
    }
    if (reply && reply.length > 0) this._sendFramedBytes(reply);
  }

  _onChannelClosed(reason) {
    this._finish(reason?.message ?? String(reason ?? 'channel closed'), null);
  }

  _finish(reason, exitInfo) {
    if (this._closed) return;
    this._closed = true;
    clearInterval(this._ackTimer);
    this.push(null); // end the readable side
    this.emit('closed', reason, exitInfo);
  }

  _sendMaybeHeartbeat() {
    if (this._closed) return;
    const hb = this._peer.maybeHeartbeat();
    if (hb.length > 0) this._sendFramedBytes(hb);
  }

  _sendFramedBytes(buf) {
    // Send ONE framing line per WebSocket message. Several concatenated
    // frames (from a large write) can exceed AgentCore's 64KB per-frame
    // limit, and an oversized frame is silently dropped end-to-end (found in
    // live verification — large transfers stalled; see websocket-stream-
    // channel.js / image/app/main.py _iter_ws_frames). Each individual frame
    // is <64KB by construction (framing.js MAX_CHUNK base64 ~= 60KB). The
    // receiving FramedPeer.feed() reassembles regardless of message
    // boundaries, so this is purely a send-side chunking concern.
    const text = buf.toString('ascii');
    let start = 0;
    while (start < text.length) {
      const nl = text.indexOf('\n', start);
      if (nl === -1) {
        this._channel.send(text.slice(start));
        break;
      }
      this._channel.send(text.slice(start, nl + 1));
      start = nl + 1;
    }
  }

  // --- Duplex implementation ----------------------------------------------
  _write(chunk, _encoding, callback) {
    if (this._closed) {
      callback(new Error('stream is closed'));
      return;
    }
    // Apply the send window BEFORE framing this chunk: wait (async, so the
    // event loop keeps consuming incoming acks) until the un-acked backlog
    // drains below the window. Writers that respect Duplex backpressure
    // (write() returning false + 'drain') are throttled cleanly;
    // fire-and-forget writers are still protected because the wait happens
    // here, inside the pending-callback write slot.
    const doSend = () => {
      try {
        this._sendFramedBytes(this._peer.sendData(chunk));
        callback();
      } catch (err) {
        callback(err);
      }
    };
    if (this._peer.outRetain.buf.length < SEND_WINDOW) {
      doSend();
      return;
    }
    const waitDrain = async () => {
      while (!this._closed && this._peer.outRetain.buf.length >= SEND_WINDOW) {
        await sleepMs(20);
      }
      if (this._closed) {
        callback(new Error('stream is closed'));
        return;
      }
      doSend();
    };
    waitDrain();
  }

  // eslint-disable-next-line class-methods-use-this
  _read() {
    /* data is pushed as it arrives (see _onChannelData); nothing to pull */
  }

  _destroy(err, callback) {
    this.closeStream();
    callback(err);
  }

  /** Graceful application-level close: notify the remote end, then tear
   * down the underlying channel. */
  closeStream() {
    if (this._closed) return Promise.resolve(false);
    this._closed = true;
    clearInterval(this._ackTimer);
    const closeFrame = this._peer.sendClose();
    if (this._channel.closeAfterRemoteAck) {
      return this._channel.closeAfterRemoteAck(closeFrame.toString('ascii'));
    }
    try {
      this._sendFramedBytes(closeFrame);
    } catch {
      /* best effort */
    }
    this._channel.close();
    return Promise.resolve(true);
  }
}

export class WebSocketStreamTransport {
  constructor({ region, runtimeArn, sessionId, endpointName }) {
    this.region = region;
    this.runtimeArn = runtimeArn;
    this.sessionId = sessionId;
    this.endpointName = endpointName;
  }

  /** target.remote = {kind:"tcp", port} | {kind:"exec", argv} | {kind:"fs", workspace, storage} */
  async open(target) {
    const remote = target.remote;
    const channel = new WebSocketChannel({
      region: this.region,
      runtimeArn: this.runtimeArn,
      sessionId: this.sessionId,
      endpointName: this.endpointName,
      mode: remote.kind,
      port: remote.kind === 'tcp' ? remote.port : undefined,
      argv: remote.kind === 'exec' ? remote.argv : undefined,
      workspace: remote.workspace,
      storage: remote.storage,
      sessionEpoch: remote.sessionEpoch,
    });
    // Wire the ResilientStream's callbacks onto the channel BEFORE opening
    // it — otherwise the very first 'up' event (fired synchronously inside
    // open()) would hit the channel's no-op defaults and be lost.
    const stream = new ResilientStream(channel);
    await channel.open();
    return stream;
  }
}

export { ResilientStream };
