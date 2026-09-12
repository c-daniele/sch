// websocket-stream-channel.js — client side of the sch-remote-ui-tunnel
// bridge over `InvokeAgentRuntimeWithWebSocketStream` (design D1/D3, post-D9
// pivot from the interactive shell channel — see design.md "Nota di
// revisione"). Replaces shell-channel.js entirely.
//
// URL:      wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded-arn>/ws[?qualifier=<endpoint>]
// Auth:     SigV4 (same recipe as before: sign a fake HTTPS GET with the
//           same host, reuse the resulting headers on the WS upgrade).
//           Confirmed against the OFFICIAL bedrock_agentcore Python SDK
//           source (AgentCoreRuntimeClient._build_websocket_url /
//           _sigv4_sign), not reverse-engineered.
// Session:  runtimeSessionId travels as header
//           X-Amzn-Bedrock-AgentCore-Runtime-Session-Id (unsigned, added
//           after signing, matching the SDK's own generate_ws_connection).
// Contract: no server-imposed message envelope — text or binary, freely
//           chosen by the application. This client always sends: (1) one
//           JSON text message declaring {tunnel_id, mode, port|argv|workspace}, then
//           (2) tunnel_framing.js protocol lines as subsequent text
//           messages (shared wire format with the Python side —
//           image/app/tunnel_framing.py / image/app/main.py's
//           _TunnelSession).
// Limits:   64KB per WebSocket frame, 60min max connection duration ("
//           Streaming maximum duration"), 250 frames/s per connection, 200
//           TPS per-agent-per-account for new connections (a rate, not a
//           concurrency cap) — see design.md Context for the full quote
//           from bedrock-agentcore-limits.html. No documented concurrent-
//           connection ceiling for this operation (unlike the interactive
//           shell channel's "10 concurrent shell sessions per runtime").
//
// Reconnection: the SAME `tunnel_id` (generated once per logical
// ResilientStream, NOT per physical connection) is sent on every
// (re)connect, so the shim's `_TunnelSession` registry reattaches to the
// existing target/process instead of restarting it. CRITICAL: this file's
// caller (transport.js) MUST construct exactly one FramedPeer per
// WebSocketChannel and reuse it across reconnects — a fresh peer on
// reconnect would desync from the server's offset tracking and every
// message would look like a gap requiring a resend from byte 0 that the
// server's retain buffer can no longer satisfy (found and fixed during
// this session's own testing — see the "Cronologia" style note in
// design.md if this ever needs re-explaining).

import { randomUUID } from 'node:crypto';
import WebSocket from 'ws';
import { SignatureV4 } from '@smithy/signature-v4';
import { HttpRequest } from '@smithy/protocol-http';
import { Sha256 } from '@aws-crypto/sha256-js';
import { fromNodeProviderChain } from '@aws-sdk/credential-providers';

const WS_PING_INTERVAL_MS = 30_000;
const WS_PONG_TIMEOUT_MS = 60_000;
const RECONNECT_BASE_DELAY_S = 1;
const RECONNECT_MAX_DELAY_S = 15;
const RECONNECT_BUDGET_MS = 900_000; // 15 minutes total (client-side policy choice)
const RECONNECT_COOLDOWN_EVERY = 5;
const RECONNECT_COOLDOWN_MS = 30_000;
const GRACEFUL_CLOSE_TIMEOUT_MS = 7_000;
const DEFAULT_PROACTIVE_RECONNECT_MS = 55 * 60 * 1000; // T-5min of the documented 60min max
// Test/debug override (used by bin/verify-acp-editor.sh to force frequent
// physical reconnects and prove the framing recovers mid-transfer). Not an
// operator-facing knob.
const PROACTIVE_RECONNECT_MS = Number.parseInt(process.env.SCH_TUNNEL_PROACTIVE_RECONNECT_MS ?? '', 10)
  || DEFAULT_PROACTIVE_RECONNECT_MS;

let _credentials = null;
function defaultCredentials() {
  if (!_credentials) _credentials = fromNodeProviderChain();
  return _credentials;
}

export function dataPlaneHost(region) {
  return `bedrock-agentcore.${region}.amazonaws.com`;
}

export function buildWsUrl(region, runtimeArn, endpointName) {
  const host = dataPlaneHost(region);
  const encodedArn = encodeURIComponent(runtimeArn);
  const url = new URL(`wss://${host}/runtimes/${encodedArn}/ws`);
  if (endpointName) url.searchParams.set('qualifier', endpointName);
  return url;
}

async function signUpgradeHeaders(region, url, sessionId) {
  const query = {};
  url.searchParams.forEach((v, k) => {
    query[k] = v;
  });
  const req = new HttpRequest({
    method: 'GET',
    protocol: 'https:',
    hostname: url.hostname,
    path: url.pathname,
    query,
    headers: { host: url.hostname },
  });
  const signer = new SignatureV4({
    service: 'bedrock-agentcore',
    region,
    credentials: defaultCredentials(),
    sha256: Sha256,
  });
  const signed = await signer.sign(req);
  const headers = { ...signed.headers };
  if (sessionId) headers['X-Amzn-Bedrock-AgentCore-Runtime-Session-Id'] = sessionId;
  return headers;
}

function jitter(seconds) {
  return seconds * (0.75 + Math.random() * 0.5);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function startKeepalive(ws, onDead) {
  let pongTimer = null;
  let stopped = false;
  ws.on('pong', () => {
    if (pongTimer !== null) clearTimeout(pongTimer);
    pongTimer = null;
  });
  const pingTimer = setInterval(() => {
    if (stopped || ws.readyState !== ws.OPEN) return;
    ws.ping();
    pongTimer = setTimeout(() => {
      if (!stopped) onDead();
    }, WS_PONG_TIMEOUT_MS);
  }, WS_PING_INTERVAL_MS);
  return () => {
    stopped = true;
    clearInterval(pingTimer);
    if (pongTimer !== null) clearTimeout(pongTimer);
    ws.removeAllListeners('pong');
  };
}

/** Open one physical WebSocket connection and send the initial control
 * message. Confirmation is the successful WS upgrade itself — NOT a server
 * greeting: the remote byte target is client-speaks-first (opencode serve
 * speaks HTTP, ACP agents speak JSON-RPC — both stay silent until they
 * receive the tunneled request), and the adapter only starts piping client
 * bytes AFTER open() resolves, so waiting for a first server message here
 * deadlocks (discovered in live verification, OQ-WS-LIVE — see design.md
 * "Nota di revisione"/Risks). Any rejection the shim emits ({"type":"error"}
 * + close) is handled on the persistent channel instead. The JSON error is
 * authoritative because AgentCore may also use 1011 for a transient physical
 * connection shutdown. */
function connectOnce({ region, runtimeArn, sessionId, tunnelId, mode, port, argv, workspace, storage, sessionEpoch, endpointName, timeoutMs = 10_000 }) {
  return new Promise((resolve, reject) => {
    const url = buildWsUrl(region, runtimeArn, endpointName);
    signUpgradeHeaders(region, url, sessionId)
      .then((headers) => {
        const ws = new WebSocket(url.toString(), { headers });
        const pendingMessages = [];
        const onEarlyMessage = (raw) => pendingMessages.push(raw);
        ws.on('message', onEarlyMessage);
        let settled = false;
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          ws.terminate();
          reject(new Error(`Timed out waiting for WebSocket upgrade (${timeoutMs / 1000}s)`));
        }, timeoutMs);

        const finish = (err, result) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          ws.removeListener('error', onError);
          ws.removeListener('close', onClose);
          if (err) {
            ws.removeListener('message', onEarlyMessage);
            reject(err);
          }
          else resolve(result);
        };
        const onError = (err) => finish(err);
        const onClose = (code, reasonBuf) => {
          const reason = reasonBuf ? reasonBuf.toString() : '';
          finish(new Error(`WebSocket closed before upgrade (code ${code}${reason ? `: ${reason}` : ''})`));
        };

        ws.on('open', () => {
          const first = { tunnel_id: tunnelId, mode };
          if (storage) first.storage = storage;
          if (Number.isInteger(sessionEpoch)) first.session_epoch = sessionEpoch;
          if (workspace) first.workspace = workspace;
          if (mode === 'tcp') first.port = port;
          else if (mode === 'exec') first.argv = argv;
          try {
            ws.send(JSON.stringify(first));
          } catch (err) {
            finish(err);
            return;
          }
          finish(null, { ws, pendingMessages, onEarlyMessage });
        });
        ws.on('error', onError);
        ws.on('close', onClose);
      })
      .catch(reject);
  });
}

export class KickedError extends Error {} // kept for API parity; unused (no takeover semantics on this transport)

export function isNonRetryableCloseCode(code) {
  return code === 1002 || code === 1008;
}

/**
 * Reconnect wrapper: retries connectOnce with exponential backoff/jitter,
 * a total time budget, and a periodic cooldown — same policy shape as the
 * retired shell-channel transport (client-side choice, not an AWS-mandated
 * policy for this operation).
 */
export async function connectWithRetry(opts) {
  const { reconnect } = opts;
  if (!reconnect) return connectOnce(opts);
  const { maxRetries = 5, onAttempt } = reconnect;
  const start = Date.now();
  let attempts = 0;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      return await connectOnce(opts);
    } catch (err) {
      attempts += 1;
      const reason = err?.message ?? String(err);
      if (onAttempt) onAttempt(attempts, reason);
      if ((maxRetries > 0 && attempts >= maxRetries) || Date.now() - start > RECONNECT_BUDGET_MS) {
        throw err;
      }
      const delayS = Math.min(RECONNECT_BASE_DELAY_S * 2 ** Math.min(attempts - 1, 4), RECONNECT_MAX_DELAY_S);
      await sleep(jitter(delayS) * 1000);
      if (attempts % RECONNECT_COOLDOWN_EVERY === 0) await sleep(RECONNECT_COOLDOWN_MS);
    }
  }
}

/**
 * Long-lived logical channel: owns one `tunnel_id` for its whole lifetime
 * and transparently reconnects the underlying physical WebSocket
 * (proactively before the documented connection-duration limit, reactively
 * on error/close). Emits raw decoded text payloads via `onData(payload)`;
 * the caller (transport.js) feeds those into the SAME long-lived
 * FramedPeer instance — never create a new peer on reconnect (see file
 * header).
 */
export class WebSocketChannel {
  constructor({
    region,
    runtimeArn,
    sessionId,
    mode,
    port,
    argv,
    workspace,
    storage,
    sessionEpoch,
    endpointName,
    onData,
    onUp,
    onDown,
    onClosed,
    proactiveReconnectMs = PROACTIVE_RECONNECT_MS,
  }) {
    this.region = region;
    this.runtimeArn = runtimeArn;
    this.sessionId = sessionId;
    this.mode = mode;
    this.port = port;
    this.argv = argv;
    this.workspace = workspace;
    this.storage = storage;
    this.sessionEpoch = sessionEpoch;
    this.endpointName = endpointName;
    this.tunnelId = randomUUID();
    this.onData = onData;
    this.onUp = onUp ?? (() => {});
    this.onDown = onDown ?? (() => {});
    this.onClosed = onClosed ?? (() => {});
    this.proactiveReconnectMs = proactiveReconnectMs;
    this.ws = null;
    this._stopKeepalive = null;
    this._proactiveTimer = null;
    this._closed = false;
    this._remoteCloseResolve = null;
    this._pendingCloseFrame = null;
  }

  async open() {
    await this._connect();
  }

  async _connect() {
    const result = await connectWithRetry({
      region: this.region,
      runtimeArn: this.runtimeArn,
      sessionId: this.sessionId,
      tunnelId: this.tunnelId,
      mode: this.mode,
      port: this.port,
      argv: this.argv,
      workspace: this.workspace,
      storage: this.storage,
      sessionEpoch: this.sessionEpoch,
      endpointName: this.endpointName,
      reconnect: { onAttempt: (n, reason) => this.onDown(n, reason) },
    });
    if (this._closed) {
      result.ws.close();
      return;
    }
    this.ws = result.ws;
    this._wireSocket(result.pendingMessages, result.onEarlyMessage);
    this._armProactiveReconnect();
    this._sendPendingClose();
    this.onUp();
  }

  _wireSocket(pendingMessages = [], onEarlyMessage = null) {
    const ws = this.ws;
    this._openedAt = Date.now();
    this._stopKeepalive = startKeepalive(ws, () => {
      try {
        ws.terminate();
      } catch {
        /* already dead */
      }
    });
    const onMessage = (raw) => {
      const text = typeof raw === 'string' ? raw : raw.toString('utf8');
      // A control message the shim sends to REJECT the tunnel (bad initial
      // message, wrong harness for tcp, target-open failure). It is
      // deterministic given the same (tunnel_id, mode, port/argv), so
      // reconnecting would loop forever — surface it as a fatal close
      // instead. Detected here (not only via the WS close code, which the
      // AgentCore proxy may not preserve end-to-end) BEFORE the bytes reach
      // the framing layer. A framing frame never starts with '{', and
      // {"type":"exit"} (remote exit code, task 6.5) is passed through
      // untouched for transport.js to handle.
      if (text.charCodeAt(0) === 0x7b /* { */) {
        let msg = null;
        try { msg = JSON.parse(text.split('\n')[0]); } catch { /* not a control line */ }
        if (msg && msg.type === 'error') {
          this._closed = true;
          this.onClosed(new Error(`tunnel rejected by remote: ${msg.message || 'error'}`));
          try { ws.close(); } catch { /* best effort */ }
          return;
        }
        if (msg && msg.type === 'closed') {
          if (this._remoteCloseResolve) this._remoteCloseResolve();
          return;
        }
      }
      this.onData(text);
    };
    ws.on('message', onMessage);
    if (onEarlyMessage) ws.removeListener('message', onEarlyMessage);
    for (const raw of pendingMessages) onMessage(raw);
    ws.on('close', (code, reasonBuf) => {
      if (process.env.SCH_TUNNEL_DEBUG) {
        process.stderr.write(`[ws close] code=${code} reason=${JSON.stringify(reasonBuf ? reasonBuf.toString() : '')} lifetime=${Date.now() - (this._openedAt || Date.now())}ms\n`);
      }
      if (this._stopKeepalive) this._stopKeepalive();
      if (this._closed) return;
      // A server JSON error above is the authoritative application rejection.
      // AgentCore also emits 1011 when ending an otherwise healthy physical
      // connection, so reconnect it with the same logical tunnel_id.
      if (isNonRetryableCloseCode(code)) {
        this._closed = true;
        this.onClosed(new Error(`tunnel rejected by remote (close code ${code})`));
        return;
      }
      this._reconnect();
    });
    ws.on('error', () => {
      // 'close' follows every 'error' for ws — reconnect handled there.
    });
  }

  _armProactiveReconnect() {
    if (this._proactiveTimer) clearTimeout(this._proactiveTimer);
    this._proactiveTimer = setTimeout(() => {
      if (!this._closed) this._reconnect();
    }, this.proactiveReconnectMs);
  }

  async _reconnect() {
    if (this._closed) return;
    this.onDown(0, 'reconnecting');
    try {
      await this._connect();
    } catch (err) {
      this._closed = true;
      this.onClosed(err);
    }
  }

  send(text) {
    if (this.ws && this.ws.readyState === this.ws.OPEN) this.ws.send(text);
  }

  close() {
    this._closed = true;
    if (this._proactiveTimer) clearTimeout(this._proactiveTimer);
    if (this._stopKeepalive) this._stopKeepalive();
    if (this.ws) this.ws.close();
  }

  _sendPendingClose() {
    if (this._pendingCloseFrame && this.ws && this.ws.readyState === this.ws.OPEN) {
      this.ws.send(this._pendingCloseFrame);
    }
  }

  closeAfterRemoteAck(closeFrame, timeoutMs = GRACEFUL_CLOSE_TIMEOUT_MS) {
    if (this._proactiveTimer) clearTimeout(this._proactiveTimer);
    this._pendingCloseFrame = closeFrame;
    this._sendPendingClose();
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), timeoutMs);
      this._remoteCloseResolve = () => {
        clearTimeout(timer);
        resolve(true);
      };
    }).finally(() => {
      this._closed = true;
      this._remoteCloseResolve = null;
      this._pendingCloseFrame = null;
      if (this._stopKeepalive) this._stopKeepalive();
      if (this.ws) this.ws.close();
    });
  }
}
