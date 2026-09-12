import assert from 'node:assert/strict';
import { ResilientStream } from './transport.js';

// A fake "channel" duck-typing WebSocketChannel's public surface (onData/
// onUp/onDown/onClosed settable, send(text), close()), wired to a peer fake
// so both ends of the ResilientStream/FramedPeer integration are exercised
// without any network/AWS access. Unlike the retired shell-channel test,
// there is no AgentCore byte-framing layer to wrap/unwrap any more — the
// "wire" here is just plain text, exactly like a real WebSocket text
// message.
function makeLoopbackPair() {
  const a = { onData: null, onUp: null, onDown: null, onClosed: null };
  const b = { onData: null, onUp: null, onDown: null, onClosed: null };
  a.send = (text) => setImmediate(() => b.onData(text));
  b.send = (text) => setImmediate(() => a.onData(text));
  a.close = () => {};
  b.close = () => {};
  return [a, b];
}

async function main() {
  const [chanA, chanB] = makeLoopbackPair();
  const streamA = new ResilientStream(chanA);
  const streamB = new ResilientStream(chanB);

  const receivedByB = [];
  streamB.on('data', (buf) => receivedByB.push(buf));

  await new Promise((resolve, reject) => {
    streamA.write('hello from A', (err) => (err ? reject(err) : resolve()));
  });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(Buffer.concat(receivedByB).toString(), 'hello from A');
  console.log('Integration test 1 (write -> framing -> readable data): PASS');

  const receivedByA = [];
  streamA.on('data', (buf) => receivedByA.push(buf));
  await new Promise((resolve, reject) => {
    streamB.write('reply from B', (err) => (err ? reject(err) : resolve()));
  });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(Buffer.concat(receivedByA).toString(), 'reply from B');
  console.log('Integration test 2 (bidirectional): PASS');

  // Large write spanning multiple framing.js MAX_CHUNK pieces.
  const big = Buffer.alloc(200_000, 0x61); // 200KB of 'a'
  const receivedBig = [];
  streamB.removeAllListeners('data');
  streamB.on('data', (buf) => receivedBig.push(buf));
  await new Promise((resolve, reject) => {
    streamA.write(big, (err) => (err ? reject(err) : resolve()));
  });
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(Buffer.concat(receivedBig).length, big.length);
  assert.ok(Buffer.concat(receivedBig).equals(big));
  console.log('Integration test 3 (large multi-frame write): PASS');

  // Task 6.5: an "exit" control line interleaved with the framed close must
  // surface via the 'closed' event's exitInfo.
  const { FramedPeer } = await import('./framing.js');
  const [chanC] = makeLoopbackPair();
  const streamC = new ResilientStream(chanC);
  let closedInfo = null;
  streamC.on('closed', (_reason, exitInfo) => {
    closedInfo = exitInfo;
  });
  const dummyPeer = new FramedPeer(() => {}, () => {});
  chanC.onData('{"type": "exit", "code": 7}\n');
  chanC.onData(dummyPeer.sendClose().toString('ascii'));
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(closedInfo, { exitCode: 7, signal: null });
  console.log('Integration test 4 (exit code propagation via control line): PASS');

  // Graceful shutdown must not report completion until the channel confirms
  // that the remote target and its fs-sync lease have been released.
  const sentByD = [];
  let acknowledgeClose;
  const chanD = {
    send: () => {},
    close: () => {},
    closeAfterRemoteAck: (text) => {
      sentByD.push(text);
      return new Promise((resolve) => { acknowledgeClose = resolve; });
    },
  };
  const streamD = new ResilientStream(chanD);
  let closeCompleted = false;
  const closePromise = streamD.closeStream().then(() => { closeCompleted = true; });
  assert.equal(sentByD.at(-1), 'C\n');
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(closeCompleted, false);
  acknowledgeClose(true);
  await closePromise;
  assert.equal(closeCompleted, true);
  console.log('Integration test 5 (graceful close waits for remote release): PASS');

  // Integration test 6: a reconnection (second 'up') asks the remote to resend
  // from the last byte held; the first connect sends nothing extra.
  const sentByE = [];
  const chanE = {
    onData: null, onUp: null, onDown: null, onClosed: null,
    send: (text) => sentByE.push(text),
    close: () => {},
  };
  const streamE = new ResilientStream(chanE);
  const ups = [];
  streamE.on('up', () => ups.push(Date.now()));
  chanE.onUp(); // first connect
  assert.deepEqual(sentByE, [], 'first connect: byte stream untouched');
  chanE.onData('D 0 aGVsbG8=\n'); // 'hello' -> inOffset 5
  chanE.onUp(); // physical reconnect
  assert.equal(sentByE.at(-1), 'R 5\n', 'reconnect: resend requested from the last byte held');
  assert.equal(ups.length, 2, "'up' still emitted on both connections");
  streamE.closeStream();
  console.log('Integration test 6 (reconnect requests resend from last byte held): PASS');

  streamA.closeStream();
  streamB.closeStream();
  streamC.closeStream();
  console.log('ALL INTEGRATION TESTS PASSED');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
