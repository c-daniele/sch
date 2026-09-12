import assert from 'node:assert/strict';
import { FramedPeer, CloseRequested } from './framing.js';

// Test 1: basic round trip, no loss
{
  const receivedB = [];
  const a = new FramedPeer(() => {}, () => {});
  const b = new FramedPeer((buf) => receivedB.push(buf), () => {});
  let wire = a.sendData(Buffer.from('hello '));
  wire = Buffer.concat([wire, a.sendData(Buffer.from('world'))]);
  const reply = b.feed(wire);
  assert.equal(Buffer.concat(receivedB).toString(), 'hello world');
  assert.equal(reply.subarray(0, 2).toString('ascii'), 'A ');
  a.feed(reply);
  assert.equal(a.outRetain.startOffset, a.outOffset);
  console.log('Test 1 (basic roundtrip + ack trim): PASS');
}

// Test 2: duplicate/overlap frames (simulating a replay)
{
  const receivedC = [];
  const c = new FramedPeer((buf) => receivedC.push(buf), () => {});
  const d = new FramedPeer(() => {}, () => {});
  const frame1 = d.sendData(Buffer.from('ABCDE'));
  const frame2 = d.sendData(Buffer.from('FGHIJ'));
  c.feed(frame1);
  c.feed(frame1); // duplicate
  c.feed(frame2);
  assert.equal(Buffer.concat(receivedC).toString(), 'ABCDEFGHIJ');
  console.log('Test 2 (duplicate/replay dedup): PASS');
}

// Test 3: gap detection + resend request + successful resend
{
  const receivedE = [];
  const e = new FramedPeer((buf) => receivedE.push(buf), () => {});
  const f = new FramedPeer(() => {}, () => {});
  const frame1 = f.sendData(Buffer.from('AAAAA'));
  const frame2 = f.sendData(Buffer.from('BBBBB'));
  const frame3 = f.sendData(Buffer.from('CCCCC'));
  void frame2; // simulated dropped in transit
  e.feed(frame1);
  const reply3 = e.feed(frame3);
  assert.equal(reply3.subarray(0, 2).toString('ascii'), 'R ');
  const resent = f.feed(reply3);
  assert.equal(resent.subarray(0, 2).toString('ascii'), 'D ');
  e.feed(resent);
  assert.equal(Buffer.concat(receivedE).toString(), 'AAAAABBBBBCCCCC');
  console.log('Test 3 (gap detection + resend): PASS');
}

// Test 4: heartbeat/close frames
{
  let closed = false;
  const h = new FramedPeer(
    () => {},
    () => {
      throw new CloseRequested();
    },
  );
  h.feed(Buffer.from('H\n', 'ascii'));
  assert.equal(closed, false);
  assert.throws(() => h.feed(Buffer.from('C\n', 'ascii')), CloseRequested);
  console.log('Test 4 (heartbeat/close): PASS');
}

// Test 5: malformed line resync tolerance
{
  const recv = [];
  const i = new FramedPeer((buf) => recv.push(buf), () => {});
  i.feed(Buffer.from('GARBAGE NOT A FRAME\n', 'ascii'));
  i.feed(Buffer.from('D 0 aGVsbG8=\n', 'ascii')); // base64('hello') at offset 0
  assert.equal(Buffer.concat(recv).toString(), 'hello');
  console.log('Test 5 (malformed-line resync): PASS');
}

// Test 6: cross-implementation wire compatibility with the Python helper —
// a frame built by the JS encoder must be byte-identical in shape to what
// the Python FramedPeer would produce for the same input (same offsets,
// same base64 alphabet, same line terminator).
{
  const j = new FramedPeer(() => {}, () => {});
  const wire = j.sendData(Buffer.from('hello'));
  assert.equal(wire.toString('ascii'), 'D 0 aGVsbG8=\n');
  console.log('Test 6 (wire format matches Python encoder): PASS');
}

// Test 7: a lost resend request is repeated on the heartbeat tick until the gap
// closes (the same outage that drops data frames drops the `R` too; a sender
// paused on its send window would otherwise never provoke a second request).
{
  const recv = [];
  const rx = new FramedPeer((buf) => recv.push(buf), () => {});
  const tx = new FramedPeer(() => {}, () => {});
  const frame1 = tx.sendData(Buffer.from('11111'));
  const frame2 = tx.sendData(Buffer.from('22222')); // lost in transit
  const frame3 = tx.sendData(Buffer.from('33333'));
  void frame2;
  rx.feed(frame1);
  assert.equal(rx.gapPending, false);
  assert.equal(rx.maybeHeartbeat().length, 0, 'no gap: heartbeat tick is quiet inside the interval');
  const firstRequest = rx.feed(frame3); // arrives out of order -> R 5 (lost as well)
  assert.equal(firstRequest.toString('ascii'), 'R 5\n');
  assert.equal(rx.gapPending, true);
  const repeated = rx.maybeHeartbeat(); // next tick repeats the request instead of a heartbeat
  assert.equal(repeated.toString('ascii'), 'R 5\n');
  const resent = tx.feed(repeated);
  assert.equal(resent.subarray(0, 2).toString('ascii'), 'D ');
  rx.feed(resent);
  assert.equal(Buffer.concat(recv).toString(), '111112222233333');
  assert.equal(rx.gapPending, false, 'gap closed: no further requests');
  assert.equal(rx.maybeHeartbeat().length, 0);
  // Idempotent on the sender: a request for bytes it already delivered
  // replays them, and the receiver drops the duplicates.
  const replay = tx.feed(Buffer.from('R 5\n', 'ascii'));
  rx.feed(replay);
  assert.equal(Buffer.concat(recv).toString(), '111112222233333');
  // A request at the very end of the retained window is an empty no-op.
  assert.equal(tx.feed(Buffer.from('R 15\n', 'ascii')).length, 0);
  console.log('Test 7 (resend request repeated until the gap closes): PASS');
}

console.log('ALL TESTS PASSED');
