import assert from 'node:assert/strict';

import { EventEmitter } from 'node:events';
import { WebSocketChannel, isNonRetryableCloseCode } from './websocket-stream-channel.js';

assert.equal(isNonRetryableCloseCode(1002), true);
assert.equal(isNonRetryableCloseCode(1008), true);
assert.equal(isNonRetryableCloseCode(1011), false);
assert.equal(isNonRetryableCloseCode(1006), false);

class FakeSocket extends EventEmitter {
  constructor() {
    super();
    this.OPEN = 1;
    this.readyState = this.OPEN;
    this.closed = false;
    this.sent = [];
  }

  close() {
    this.closed = true;
  }

  ping() {}

  send(text) {
    this.sent.push(text);
  }
}

const channel = new WebSocketChannel({
  region: 'test', runtimeArn: 'arn', sessionId: 'session', mode: 'fs', workspace: 'workspace',
});
const socket = new FakeSocket();
channel.ws = socket;
const earlyMessages = [];
const onEarlyMessage = (raw) => earlyMessages.push(raw);
socket.on('message', onEarlyMessage);
socket.emit('message', Buffer.from('early worker hello'));
let receivedEarly = null;
channel.onData = (text) => { receivedEarly = text; };
channel._wireSocket(earlyMessages, onEarlyMessage);
assert.equal(receivedEarly, 'early worker hello');
const acknowledged = channel.closeAfterRemoteAck('C\n', 1_000);
assert.deepEqual(socket.sent, ['C\n']);
socket.emit('message', Buffer.from(JSON.stringify({ type: 'closed', tunnel_id: channel.tunnelId })));
assert.equal(await acknowledged, true);
assert.equal(socket.closed, true);

const timeoutChannel = new WebSocketChannel({
  region: 'test', runtimeArn: 'arn', sessionId: 'session', mode: 'fs', workspace: 'workspace',
});
const timeoutSocket = new FakeSocket();
timeoutChannel.ws = timeoutSocket;
timeoutChannel._wireSocket();
assert.equal(await timeoutChannel.closeAfterRemoteAck('C\n', 1), false);
assert.equal(timeoutSocket.closed, true);

const reconnectChannel = new WebSocketChannel({
  region: 'test', runtimeArn: 'arn', sessionId: 'session', mode: 'fs', workspace: 'workspace',
});
const reconnectingSocket = new FakeSocket();
reconnectingSocket.readyState = 3;
reconnectChannel.ws = reconnectingSocket;
const reconnectedClose = reconnectChannel.closeAfterRemoteAck('C\n', 1_000);
assert.deepEqual(reconnectingSocket.sent, []);
const replacementSocket = new FakeSocket();
reconnectChannel.ws = replacementSocket;
reconnectChannel._wireSocket();
reconnectChannel._sendPendingClose();
assert.deepEqual(replacementSocket.sent, ['C\n']);
replacementSocket.emit('message', Buffer.from(JSON.stringify({ type: 'closed', tunnel_id: reconnectChannel.tunnelId })));
assert.equal(await reconnectedClose, true);
assert.equal(replacementSocket.closed, true);

console.log('websocket-stream-channel.test.js: ALL PASS');
