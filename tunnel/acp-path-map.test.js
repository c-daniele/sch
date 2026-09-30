// acp-path-map.test.js — unit tests for the per-field ACP v1 path
// translation (sch-acp-editor-integration task 3.1; spec acp-file-locality
// "Path translation in ACP messages"). Fixtures mirror REAL messages
// captured from `opencode acp` 1.18.3 and `claude-agent-acp` 0.59.0 during
// the OQ-ADAPTER-FS probe (see design.md Open Questions); `opencode acp`
// 2.0.18 still speaks ACP protocolVersion 1 with the same method names
// (initialize handshake re-verified for TASK-7).
import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AcpPathMap, translatePath, translateUri } from './acp-path-map.js';

const LOCAL = '/Users/op/.config/sch/mirrors/myws/repo';
const REMOTE = '/mnt/workspace/repo';
const map = new AcpPathMap({ localRoot: LOCAL });

// Test 1: translatePath boundary behavior
{
  assert.equal(translatePath(`${LOCAL}/src/a.ts`, [LOCAL], REMOTE), `${REMOTE}/src/a.ts`);
  assert.equal(translatePath(LOCAL, [LOCAL], REMOTE), REMOTE); // exact root
  assert.equal(translatePath(`${LOCAL}x/evil`, [LOCAL], REMOTE), null); // no boundary bleed
  assert.equal(translatePath('/somewhere/else', [LOCAL], REMOTE), null);
  assert.equal(translateUri(`file://${LOCAL}/src/a.ts`, [LOCAL], REMOTE), `file://${REMOTE}/src/a.ts`);
  assert.equal(translateUri('https://example.com/x', [LOCAL], REMOTE), null);
  console.log('Test 1 (path/uri boundary rules): PASS');
}

// Test 2: session/new cwd -> remote (spec scenario "Session cwd made remote")
{
  const msg = map.toAgent({
    jsonrpc: '2.0', id: 1, method: 'session/new', params: { cwd: LOCAL, mcpServers: [] },
  });
  assert.equal(msg.params.cwd, REMOTE);
  assert.deepEqual(msg.params.mcpServers, []); // untouched sibling field
  console.log('Test 2 (session/new cwd toAgent): PASS');
}

// Test 2b: duplicate separators in a temporary directory still match the
// mirror root (the verifier may construct paths from TMPDIR with a trailing /).
{
  const duplicate = `${LOCAL}//src/a.ts`;
  assert.equal(translatePath(duplicate, [LOCAL], REMOTE), `${REMOTE}/src/a.ts`);
  console.log('Test 2b (duplicate separator normalization): PASS');
}

// Test 3: session/prompt resource_link + embedded resource -> remote
// (spec acp-editor-integration scenario "Mention di un file del progetto nel prompt")
{
  const msg = map.toAgent({
    jsonrpc: '2.0', id: 2, method: 'session/prompt',
    params: {
      sessionId: 's1',
      prompt: [
        { type: 'text', text: `look at ${LOCAL}/readme.md` }, // free text: NOT rewritten (per-field, not blind)
        { type: 'resource_link', uri: `file://${LOCAL}/src/main.py`, name: 'main.py' },
        { type: 'resource', resource: { uri: `${LOCAL}/notes.txt`, text: 'hi' } },
      ],
    },
  });
  assert.equal(msg.params.prompt[0].text, `look at ${LOCAL}/readme.md`);
  assert.equal(msg.params.prompt[1].uri, `file://${REMOTE}/src/main.py`);
  assert.equal(msg.params.prompt[2].resource.uri, `${REMOTE}/notes.txt`);
  console.log('Test 3 (prompt content blocks toAgent, free text untouched): PASS');
}

// Test 4: tool_call_update locations + diff -> local (spec scenario "Path dei
// tool_call resi locali"). Shape from the real claude-agent-acp probe capture.
{
  const msg = map.toEditor({
    jsonrpc: '2.0', method: 'session/update',
    params: {
      sessionId: 's1',
      update: {
        sessionUpdate: 'tool_call_update',
        toolCallId: 'toolu_01',
        title: `Write ${REMOTE}/probe-agent.txt`, // human title: untouched (documented)
        status: 'completed',
        locations: [{ path: `${REMOTE}/probe-agent.txt` }],
        content: [
          { type: 'diff', path: `${REMOTE}/probe-agent.txt`, oldText: null, newText: 'from-agent' },
          { type: 'content', content: { type: 'resource_link', uri: `file://${REMOTE}/probe-agent.txt` } },
        ],
        rawInput: { file_path: `${REMOTE}/probe-agent.txt`, content: 'from-agent' },
      },
    },
  });
  const u = msg.params.update;
  assert.equal(u.locations[0].path, `${LOCAL}/probe-agent.txt`);
  assert.equal(u.content[0].path, `${LOCAL}/probe-agent.txt`);
  assert.equal(u.content[0].newText, 'from-agent'); // non-path diff fields intact
  assert.equal(u.content[1].content.uri, `file://${LOCAL}/probe-agent.txt`);
  // rawInput is adapter-internal (unmapped by design) -> flagged as suspicious, not rewritten
  assert.equal(u.rawInput.file_path, `${REMOTE}/probe-agent.txt`);
  const sus = map.findSuspicious(msg, 'toEditor');
  assert.ok(sus.some((s) => s.field.includes('rawInput.file_path')), `expected rawInput flagged, got ${JSON.stringify(sus)}`);
  // the title mentions a remote path MID-string: not a path field, not
  // flagged (the sweep is prefix-based by design — "euristica prefisso" —
  // to avoid noisy logs on every human sentence mentioning a path)
  assert.ok(!sus.some((s) => s.field.endsWith('.title')));
  console.log('Test 4 (tool_call_update toEditor + suspicious sweep): PASS');
}

// Test 5: session/request_permission toolCall (real claude-agent-acp shape)
{
  const msg = map.toEditor({
    jsonrpc: '2.0', id: 7, method: 'session/request_permission',
    params: {
      sessionId: 's1',
      options: [{ optionId: 'allow', name: 'Allow', kind: 'allow_once' }],
      toolCall: {
        toolCallId: 'toolu_02',
        locations: [{ path: `${REMOTE}/src/x.ts`, line: 12 }],
        content: [{ type: 'diff', path: `${REMOTE}/src/x.ts`, oldText: 'a', newText: 'b' }],
      },
    },
  });
  assert.equal(msg.params.toolCall.locations[0].path, `${LOCAL}/src/x.ts`);
  assert.equal(msg.params.toolCall.locations[0].line, 12);
  assert.equal(msg.params.toolCall.content[0].path, `${LOCAL}/src/x.ts`);
  assert.equal(msg.params.options[0].optionId, 'allow');
  console.log('Test 5 (request_permission toolCall toEditor): PASS');
}

// Test 6: agent_message_chunk resource_link -> local
{
  const msg = map.toEditor({
    jsonrpc: '2.0', method: 'session/update',
    params: {
      sessionId: 's1',
      update: {
        sessionUpdate: 'agent_message_chunk',
        content: { type: 'resource_link', uri: `file://${REMOTE}/doc.md`, name: 'doc.md' },
      },
    },
  });
  assert.equal(msg.params.update.content.uri, `file://${LOCAL}/doc.md`);
  console.log('Test 6 (message chunk content block toEditor): PASS');
}

// Test 7: fs/* and terminal/create param mapping (table completeness — the
// mediator rejects these, but the table must still translate them)
{
  const r = map.toEditor({ jsonrpc: '2.0', id: 9, method: 'fs/read_text_file', params: { sessionId: 's1', path: `${REMOTE}/a.txt` } });
  assert.equal(r.params.path, `${LOCAL}/a.txt`);
  const w = map.toEditor({ jsonrpc: '2.0', id: 10, method: 'fs/write_text_file', params: { sessionId: 's1', path: `${REMOTE}/b.txt`, content: 'x' } });
  assert.equal(w.params.path, `${LOCAL}/b.txt`);
  const t = map.toEditor({ jsonrpc: '2.0', id: 11, method: 'terminal/create', params: { sessionId: 's1', command: 'ls', cwd: REMOTE } });
  assert.equal(t.params.cwd, LOCAL);
  console.log('Test 7 (fs/* + terminal/create param table): PASS');
}

// Test 8: unknown fields pass through untouched; suspicious remote path in
// unmapped field is reported but NOT altered (spec scenario "Unknown
// field with suspicious remote path")
{
  const original = {
    jsonrpc: '2.0', method: 'session/update',
    params: {
      sessionId: 's1',
      update: {
        sessionUpdate: 'plan',
        entries: [{ content: `refactor ${REMOTE}/src/big.ts`, priority: 'high', status: 'pending' }],
        _meta: { adapterExtension: { scratchFile: `${REMOTE}/.tmp/scratch.json` } },
      },
    },
  };
  const copy = JSON.parse(JSON.stringify(original));
  const out = map.toEditor(copy);
  assert.deepEqual(out, original); // nothing rewritten in unmapped kinds
  const sus = map.findSuspicious(out, 'toEditor');
  assert.ok(sus.some((s) => s.field.includes('_meta.adapterExtension.scratchFile')));
  // plan entry content mentions the path MID-string: not flagged (prefix heuristic)
  assert.ok(!sus.some((s) => s.field.includes('entries[0].content')));
  console.log('Test 8 (unmapped fields passthrough + suspicious report): PASS');
}

// Test 9: toEditor leaves local paths alone / toAgent leaves remote paths
// alone (idempotence across double application)
{
  const msg = map.toEditor({
    jsonrpc: '2.0', method: 'session/update',
    params: { sessionId: 's1', update: { sessionUpdate: 'tool_call', toolCallId: 't', locations: [{ path: `${LOCAL}/already.ts` }] } },
  });
  assert.equal(msg.params.update.locations[0].path, `${LOCAL}/already.ts`);
  console.log('Test 9 (idempotence on already-translated paths): PASS');
}

// Test 10: macOS canonicalization — a mirror under a symlinked root (e.g.
// /var -> /private/var) accepts BOTH forms toAgent and emits the as-given
// form toEditor (design note from the OQ-ADAPTER-FS probe)
{
  const base = mkdtempSync(join(tmpdir(), 'sch-pm-'));   // on macOS: /var/folders/...
  const canonical = realpathSync(base);                   // /private/var/folders/...
  const m2 = new AcpPathMap({ localRoot: base });
  if (canonical !== base) {
    const viaCanonical = m2.toAgent({ jsonrpc: '2.0', id: 1, method: 'session/new', params: { cwd: `${canonical}/sub` } });
    assert.equal(viaCanonical.params.cwd, `${REMOTE}/sub`);
  }
  const viaGiven = m2.toAgent({ jsonrpc: '2.0', id: 2, method: 'session/new', params: { cwd: `${base}/sub` } });
  assert.equal(viaGiven.params.cwd, `${REMOTE}/sub`);
  const back = m2.toEditor({
    jsonrpc: '2.0', method: 'session/update',
    params: { sessionId: 's', update: { sessionUpdate: 'tool_call', toolCallId: 't', locations: [{ path: `${REMOTE}/sub/f.txt` }] } },
  });
  assert.equal(back.params.update.locations[0].path, `${base}/sub/f.txt`);
  console.log('Test 10 (canonical + as-given local roots): PASS');
}

console.log('acp-path-map.test.js: ALL PASS');
