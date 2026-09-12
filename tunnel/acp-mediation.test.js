// acp-mediation.test.js — unit tests for the per-message ACP mediation in
// acp.js (sch-acp-editor-integration tasks 3.2/3.3/3.4; spec
// acp-file-locality scenarios "Path translation" and "Neutralization of
// client-fs capabilities"; spec remote-ui-tunnel scenario "Integrity of
// mediated messages"). Runs fully offline: the mediator is pure/synchronous.
import assert from 'node:assert/strict';
import { AcpMediator, LineSplitter } from './acp.js';
import { AcpPathMap } from './acp-path-map.js';

const LOCAL = '/Users/op/.config/sch/mirrors/myws/repo';
const REMOTE = '/mnt/workspace/repo';

function mediator(logs = []) {
  return new AcpMediator({
    pathMap: new AcpPathMap({ localRoot: LOCAL }),
    log: (m) => logs.push(m),
  });
}

// Test 1: capability neutralization on initialize (fs AND terminal), other
// fields preserved (spec scenario "Capability fs azzerate verso l'agente")
{
  const logs = [];
  const m = mediator(logs);
  const line = JSON.stringify({
    jsonrpc: '2.0', id: 0, method: 'initialize',
    params: {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: true, writeTextFile: true }, terminal: true },
      clientInfo: { name: 'Zed', version: '0.201.5' },
    },
  });
  const { forward, reply } = m.editorLine(line);
  assert.equal(reply.length, 0);
  assert.equal(forward.length, 1);
  const fwd = JSON.parse(forward[0]);
  assert.deepEqual(fwd.params.clientCapabilities.fs, { readTextFile: false, writeTextFile: false });
  assert.equal(fwd.params.clientCapabilities.terminal, false);
  assert.equal(fwd.id, 0);
  assert.equal(fwd.params.protocolVersion, 1);
  assert.deepEqual(fwd.params.clientInfo, { name: 'Zed', version: '0.201.5' }); // untouched
  assert.ok(logs.some((l) => l.includes('neutralized client capabilities')));
  console.log('Test 1 (initialize caps neutralization): PASS');
}

// Test 2: initialize with NO clientCapabilities still gets explicit false caps
{
  const m = mediator();
  const { forward } = m.editorLine(JSON.stringify({ jsonrpc: '2.0', id: 0, method: 'initialize', params: { protocolVersion: 1 } }));
  const fwd = JSON.parse(forward[0]);
  assert.deepEqual(fwd.params.clientCapabilities.fs, { readTextFile: false, writeTextFile: false });
  assert.equal(fwd.params.clientCapabilities.terminal, false);
  console.log('Test 2 (caps forced even when absent): PASS');
}

// Test 3: fs/* rejection — request answered toward the AGENT with a JSON-RPC
// error carrying the same id, NOT forwarded to the editor (spec scenario
// "Agent fs request rejected")
{
  const logs = [];
  const m = mediator(logs);
  const { forward, reply } = m.agentLine(JSON.stringify({
    jsonrpc: '2.0', id: 42, method: 'fs/write_text_file',
    params: { sessionId: 's1', path: `${REMOTE}/a.txt`, content: 'x' },
  }));
  assert.equal(forward.length, 0); // never reaches the editor
  assert.equal(reply.length, 1);
  const err = JSON.parse(reply[0]);
  assert.equal(err.id, 42);
  assert.equal(err.error.code, -32601);
  assert.match(err.error.message, /fs\/write_text_file/);
  assert.ok(logs.some((l) => l.includes("rejected agent request 'fs/write_text_file'")));
  console.log('Test 3 (fs/* rejection): PASS');
}

// Test 4: terminal/* rejection (design D3 extension)
{
  const m = mediator();
  const { forward, reply } = m.agentLine(JSON.stringify({
    jsonrpc: '2.0', id: 7, method: 'terminal/create',
    params: { sessionId: 's1', command: 'npm', args: ['test'], cwd: REMOTE },
  }));
  assert.equal(forward.length, 0);
  assert.equal(JSON.parse(reply[0]).error.code, -32601);
  console.log('Test 4 (terminal/* rejection): PASS');
}

// Test 5: path translation editor->agent on session/new; sibling fields kept
{
  const m = mediator();
  const { forward } = m.editorLine(JSON.stringify({
    jsonrpc: '2.0', id: 1, method: 'session/new',
    params: { cwd: LOCAL, mcpServers: [{ name: 'ctx', command: '/usr/local/bin/ctx', args: [] }] },
  }));
  const fwd = JSON.parse(forward[0]);
  assert.equal(fwd.params.cwd, REMOTE);
  assert.equal(fwd.params.mcpServers[0].command, '/usr/local/bin/ctx'); // NOT under mirror -> untouched
  console.log('Test 5 (session/new translation + field preservation): PASS');
}

// Test 6: path translation agent->editor on session/update tool_call
{
  const m = mediator();
  const { forward, reply } = m.agentLine(JSON.stringify({
    jsonrpc: '2.0', method: 'session/update',
    params: {
      sessionId: 's1',
      update: {
        sessionUpdate: 'tool_call_update', toolCallId: 't1', status: 'completed',
        locations: [{ path: `${REMOTE}/src/f.ts` }],
        content: [{ type: 'diff', path: `${REMOTE}/src/f.ts`, oldText: 'a', newText: 'b' }],
      },
    },
  }));
  assert.equal(reply.length, 0);
  const fwd = JSON.parse(forward[0]);
  assert.equal(fwd.params.update.locations[0].path, `${LOCAL}/src/f.ts`);
  assert.equal(fwd.params.update.content[0].path, `${LOCAL}/src/f.ts`);
  assert.equal(fwd.params.update.content[0].newText, 'b');
  console.log('Test 6 (session/update translation): PASS');
}

// Test 7: messages without mapped fields pass through BYTE-IDENTICAL
// (spec remote-ui-tunnel scenario "Integrity of mediated messages")
{
  const m = mediator();
  const line = '{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn","_meta":{"weird":[1,2,{"x":null}]}}}';
  const { forward } = m.agentLine(line);
  assert.equal(forward[0], line); // exact same bytes, no re-serialization drift
  const line2 = '{"jsonrpc":"2.0","id":4,"method":"session/cancel","params":{"sessionId":"s1"}}';
  const r2 = m.editorLine(line2);
  assert.equal(r2.forward[0], line2);
  console.log('Test 7 (byte-identical passthrough): PASS');
}

// Test 8: id + relative order preserved across a realistic interleaving
{
  const m = mediator();
  const inputs = [
    { jsonrpc: '2.0', id: 10, method: 'session/prompt', params: { sessionId: 's1', prompt: [{ type: 'text', text: 'hi' }] } },
    { jsonrpc: '2.0', id: 11, method: 'session/set_mode', params: { sessionId: 's1', modeId: 'auto' } },
    { jsonrpc: '2.0', method: 'session/cancel', params: { sessionId: 's1' } },
  ];
  const out = [];
  for (const msg of inputs) out.push(...m.editorLine(JSON.stringify(msg)).forward);
  assert.equal(out.length, 3);
  assert.equal(JSON.parse(out[0]).id, 10);
  assert.equal(JSON.parse(out[1]).id, 11);
  assert.equal(JSON.parse(out[2]).method, 'session/cancel');
  console.log('Test 8 (order + id preservation): PASS');
}

// Test 9: non-JSON lines forwarded verbatim with a warning (defensive)
{
  const logs = [];
  const m = mediator(logs);
  const { forward } = m.agentLine('not json at all');
  assert.equal(forward[0], 'not json at all');
  assert.ok(logs.some((l) => l.includes('non-JSON line from agent')));
  console.log('Test 9 (non-JSON passthrough): PASS');
}

// Test 10: chat-style (pathMap=null) — no translation, but neutralization
// and rejection STILL active (design D3 unconditional)
{
  const logs = [];
  const m = new AcpMediator({ pathMap: null, log: (l) => logs.push(l) });
  const init = m.editorLine(JSON.stringify({
    jsonrpc: '2.0', id: 0, method: 'initialize',
    params: { protocolVersion: 1, clientCapabilities: { fs: { readTextFile: true, writeTextFile: true } } },
  }));
  assert.deepEqual(JSON.parse(init.forward[0]).params.clientCapabilities.fs, { readTextFile: false, writeTextFile: false });
  const sn = m.editorLine(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'session/new', params: { cwd: LOCAL, mcpServers: [] } }));
  assert.equal(JSON.parse(sn.forward[0]).params.cwd, LOCAL); // untranslated in chat-style
  const rej = m.agentLine(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'fs/read_text_file', params: { path: '/x' } }));
  assert.equal(rej.forward.length, 0);
  assert.equal(JSON.parse(rej.reply[0]).error.code, -32601);
  console.log('Test 10 (chat-style: no translation, D3 still enforced): PASS');
}

// Test 11: suspicious un-translated remote path logged, message unaltered
// (spec scenario "Unknown field with suspicious remote path")
{
  const logs = [];
  const m = mediator(logs);
  const line = JSON.stringify({
    jsonrpc: '2.0', method: 'session/update',
    params: { sessionId: 's1', update: { sessionUpdate: 'plan', _meta: { scratch: `${REMOTE}/tmp/x` }, entries: [] } },
  });
  const { forward } = m.agentLine(line);
  assert.equal(forward[0], JSON.stringify(JSON.parse(line))); // structurally unchanged
  assert.ok(logs.some((l) => l.includes('un-translated remote path') && l.includes('_meta.scratch')));
  console.log('Test 11 (suspicious log-and-passthrough): PASS');
}

// Test 12: LineSplitter reassembles chunks split mid-message and holds partials
{
  const ls = new LineSplitter();
  assert.deepEqual(ls.feed(Buffer.from('{"a":')), []);
  assert.deepEqual(ls.feed(Buffer.from('1}\n{"b":2}\n{"c"')), ['{"a":1}', '{"b":2}']);
  assert.deepEqual(ls.feed(Buffer.from(':3}\n')), ['{"c":3}']);
  console.log('Test 12 (line splitting across chunks): PASS');
}

console.log('acp-mediation.test.js: ALL PASS');
