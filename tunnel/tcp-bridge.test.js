import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import net from 'node:net';
import { PassThrough } from 'node:stream';
import { setTimeout as sleep } from 'node:timers/promises';
import { maxChannelsFromEnv, openTcpBridge } from './tcp-bridge.js';
import { runWeb } from './web.js';

function fakeStream() {
  const stream = new PassThrough();
  stream.closeCalls = 0;
  stream.closeStream = async () => {
    stream.closeCalls += 1;
    stream.end();
    stream.emit('closed');
  };
  return stream;
}

function connect(port) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1');
    socket.once('connect', () => resolve(socket));
    socket.once('error', reject);
  });
}

assert.equal(maxChannelsFromEnv({ SCH_TUNNEL_MAX_CHANNELS: '7' }), 7);
assert.equal(maxChannelsFromEnv({ SCH_TUNNEL_MAX_CHANNELS: 'invalid' }), 32);
assert.equal(maxChannelsFromEnv({ SCH_TUNNEL_MAX_CHANNELS: '0' }), 32);

async function until(predicate, description) {
  for (let i = 0; i < 100; i += 1) {
    if (predicate()) return;
    await sleep(10);
  }
  assert.fail(`timed out waiting for ${description}`);
}

// Multiple local connections get independent transport channels and shutdown
// closes every stream and listener.
{
  const streams = [];
  const transport = { open: async () => streams.push(fakeStream()) && streams.at(-1) };
  const bridge = await openTcpBridge({ transport, remote: { kind: 'tcp', port: 4096 } });
  const clients = await Promise.all([connect(bridge.localPort), connect(bridge.localPort)]);
  await until(() => streams.length === 2, 'two channels');
  assert.notEqual(streams[0], streams[1]);
  await bridge.shutdown();
  assert.ok(streams.every((stream) => stream.closeCalls === 1));
  await assert.rejects(connect(bridge.localPort));
  clients.forEach((client) => client.destroy());
}

// Pending asynchronous opens reserve cap capacity, preventing a connection
// burst from exceeding SCH_TUNNEL_MAX_CHANNELS.
{
  let releaseOpen;
  let opens = 0;
  const stream = fakeStream();
  const warnings = [];
  const transport = {
    open: () => {
      opens += 1;
      return new Promise((resolve) => { releaseOpen = () => resolve(stream); });
    },
  };
  const bridge = await openTcpBridge({
    transport,
    remote: { kind: 'tcp', port: 4096 },
    maxChannels: 1,
    writeError: (message) => warnings.push(message),
  });
  const first = await connect(bridge.localPort);
  const second = await connect(bridge.localPort);
  await until(() => warnings.length === 1, 'cap warning');
  assert.equal(opens, 1);
  assert.match(warnings[0], /channel cap reached \(1\)/);
  releaseOpen();
  await bridge.shutdown();
  first.destroy();
  second.destroy();
}

// The headless adapter emits exactly one parseable ready line, accepts
// multiple clients, and drains channels after SIGTERM.
{
  const signals = new EventEmitter();
  const lines = [];
  const streams = [];
  const transport = { open: async () => streams.push(fakeStream()) && streams.at(-1) };
  const argv = [
    '--region', 'eu-west-1', '--runtime-arn', 'arn:test', '--session-id', 'session',
    '--workspace', 'workspace', '--storage', 'session', '--remote-port', '4096',
  ];
  const running = runWeb(argv, {
    signalEmitter: signals,
    writeError: (message) => lines.push(message),
    transportFactory: () => transport,
  });
  await until(() => lines.length === 1, 'ready line');
  const ready = JSON.parse(lines[0]);
  assert.deepEqual(Object.keys(ready).sort(), ['host', 'port', 'type', 'url']);
  assert.equal(ready.type, 'ready');
  assert.equal(ready.host, '127.0.0.1');
  assert.equal(ready.url, `http://127.0.0.1:${ready.port}`);
  const clients = await Promise.all([connect(ready.port), connect(ready.port)]);
  await until(() => streams.length === 2, 'web channels');
  signals.emit('SIGTERM');
  await running;
  assert.ok(streams.every((stream) => stream.closeCalls === 1));
  clients.forEach((client) => client.destroy());
}

console.log('tcp-bridge.test.js: ALL PASS');
