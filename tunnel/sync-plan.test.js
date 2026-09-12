import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  conflictName, isSemanticallyEmpty, makeBaseline, parseBaseline, planBootstrap, planThreeWay, stripExempt,
  writeBaseline,
} from './sync-plan.js';

const file = (h, s = 1) => ({ h, s, x: false, m: 0 });
const manifest = (entries) => new Map(entries);

// Bootstrap matrix: bootstrap-only SCH Git metadata is semantically empty;
// all conflicting plans are rejected before any mutation is returned.
assert(isSemanticallyEmpty(manifest([['.git/HEAD', file('git')], ['.sch-initialized', file('state')]])));
assert(isSemanticallyEmpty(manifest([['.mcp.json', file('seeded')]])));
assert.equal(isSemanticallyEmpty(manifest([['foo/.mcp.json', file('user')]])), false);
assert.deepEqual([...stripExempt(manifest([
  ['.mcp.json', file('seeded')], ['.sch-initialized', file('state')], ['foo/.mcp.json', file('user')],
])).keys()], ['foo/.mcp.json']);
assert.equal(planBootstrap(manifest([['a', file('a')]]), manifest(), 'abort').operations[0].side, 'remote');
assert.equal(planBootstrap(manifest(), manifest([['a', file('a')]]), 'abort').operations[0].side, 'local');
assert.equal(planBootstrap(manifest([['a', file('same')]]), manifest([['a', file('same')]])).converged, true);
const divergent = planBootstrap(manifest([['a', file('local')]]), manifest([['a', file('remote')]]));
assert.equal(divergent.operations.length, 0);
assert.equal(divergent.conflicts.length, 1);
assert.equal(planBootstrap(manifest([['a', file('local')]]), manifest([['a', file('remote')]]), 'union').converged, false);
assert.equal(planBootstrap(manifest([['a', file('local')]]), manifest([['a', file('remote')]]), 'local-wins').operations.length, 1);
const union = planBootstrap(manifest([['local-only', file('local')]]), manifest([['remote-only', file('remote')]]), 'union');
assert.equal(union.converged, true);
assert.deepEqual(union.operations.map((o) => [o.side, o.path]), [['remote', 'local-only'], ['local', 'remote-only']]);

// Provisioning artifacts at the repository root are invisible to planning.
// The seeded remote MCP config survives while user content is pushed, but a
// nested MCP config remains normal syncable content.
const seededBootstrap = planBootstrap(
  manifest([['project.txt', file('project')]]),
  manifest([['.mcp.json', file('seeded')]]),
);
assert.equal(seededBootstrap.converged, true);
assert.deepEqual(seededBootstrap.operations.map((o) => [o.kind, o.side, o.path]), [['put', 'remote', 'project.txt']]);
const nestedBootstrap = planBootstrap(
  manifest([['foo/.mcp.json', file('user')]]),
  manifest(),
);
assert.deepEqual(nestedBootstrap.operations.map((o) => [o.side, o.path]), [['remote', 'foo/.mcp.json']]);

// Three-way: clocks are deliberately absent from comparisons. One-sided
// edits and deletions propagate; divergent edits follow the selected policy.
const base = manifest([['a', file('base')], ['gone', file('gone')]]);
assert.deepEqual(planThreeWay(base, manifest([['a', file('local')]]), manifest([['a', file('base')], ['gone', file('gone')]])).operations.map((o) => [o.kind, o.side, o.path]), [['put', 'remote', 'a'], ['del', 'remote', 'gone']]);
assert.deepEqual(planThreeWay(base, manifest([['a', file('base')], ['gone', file('gone')]]), manifest([['a', file('remote')]])).operations.map((o) => [o.kind, o.side, o.path]), [['put', 'local', 'a'], ['del', 'local', 'gone']]);
const conflict = planThreeWay(base, manifest([['a', file('local')]]), manifest([['a', file('remote')]]));
assert.equal(conflict.converged, false);
const kept = planThreeWay(base, manifest([['a', file('local')]]), manifest([['a', file('remote')]]), 'keep-both');
assert.equal(kept.converged, true);
assert.match(kept.conflicts[0].preserved.path, /^a\.sch-conflict-local-local/);
assert.equal(conflictName('a', file('123456789'), new Set(['a', 'a.sch-conflict-local-12345678'])), 'a.sch-conflict-local-12345678-2');
const seededThreeWay = planThreeWay(
  manifest([['project.txt', file('project')]]),
  manifest([['project.txt', file('project')]]),
  manifest([['project.txt', file('project')], ['.mcp.json', file('seeded')]]),
);
assert.equal(seededThreeWay.converged, true);
assert.deepEqual(seededThreeWay.operations, []);

// A detached task leaves its remote worktree authoritative while the laptop
// is offline. The next preflight pulls remote-only output, while a later
// local edit becomes an explicit conflict instead of an implicit overwrite.
const taskBase = manifest([['result.txt', file('input')]]);
assert.deepEqual(
  planThreeWay(taskBase, manifest([['result.txt', file('input')]]), manifest([['result.txt', file('task-output')]])).operations.map((o) => [o.side, o.path]),
  [['local', 'result.txt']],
);
assert.equal(
  planThreeWay(taskBase, manifest([['result.txt', file('local-edit')]]), manifest([['result.txt', file('task-output')]])).converged,
  false,
);

// Invalid, mismatched and incompatible baselines are rejected before they can
// authorize a deletion; atomic writing preserves a complete JSON document.
const root = mkdtempSync(join(tmpdir(), 'sch-baseline-'));
const path = join(root, 'baseline.json');
const baseline = makeBaseline('owner/myws', 'fingerprint', manifest([['a', file('a')]]));
await writeBaseline(path, baseline);
assert.equal(parseBaseline(readFileSync(path, 'utf8'), 'owner/myws', 'fingerprint').get('a').h, 'a');
assert.equal(parseBaseline(readFileSync(path, 'utf8'), 'other', 'fingerprint'), null);
assert.equal(parseBaseline('{bad', 'owner/myws', 'fingerprint'), null);
rmSync(root, { recursive: true, force: true });
console.log('sync-plan.test.js: ALL PASS');
