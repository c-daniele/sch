// mirror.test.js — offline tests for the local mirror sync engine
// (sch-acp-editor-integration tasks 4.2/4.5). The peer is the REAL remote
// worker (image/app/fs_sync_worker.py) spawned over a stdio pipe — the
// same cross-implementation wire-compat discipline as framing.test.js, so
// a protocol drift between mirror.js and the Python side fails HERE, not
// in live verification.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, utimesSync, unlinkSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Duplex } from 'node:stream';
import { setTimeout as sleep } from 'node:timers/promises';
import { DEFAULT_IGNORE, MirrorSync, mirrorOptionsFromEnv, isIgnored } from './mirror.js';

const WORKER = join(dirname(fileURLToPath(import.meta.url)), '..', 'image', 'app', 'fs_sync_worker.py');

function tdir(tag) {
  return mkdtempSync(join(tmpdir(), `sch-mirror-${tag}-`));
}

/** Spawn the real Python worker rooted at `remoteRoot`; returns
 * {duplex, child} where duplex is what MirrorSync sees as its stream. */
function spawnWorker(remoteRoot) {
  const child = spawn('python3', [WORKER, '--root', remoteRoot], { stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.on('data', (b) => {
    if (process.env.MIRROR_TEST_DEBUG) process.stderr.write(`[worker] ${b}`);
  });
  const duplex = Duplex.from({ readable: child.stdout, writable: child.stdin });
  duplex.on('error', () => {}); // harness artifact: child kill -> premature close
  return { duplex, child };
}

async function withSession(remoteRoot, localRoot, opts, fn) {
  const { duplex, child } = spawnWorker(remoteRoot);
  const warnings = [];
  const sync = new MirrorSync({
    stream: duplex, root: localRoot, hydrationTimeoutMs: 30_000, ...opts,
  });
  sync.on('warning', (w) => {
    warnings.push(w);
    if (process.env.MIRROR_TEST_DEBUG) process.stderr.write(`[warn] ${w}\n`);
  });
  try {
    await sync.start();
    await fn(sync, warnings);
  } finally {
    sync.close();
    child.kill('SIGTERM');
    await sleep(100);
  }
}

/** Wait until `cond()` is true (poll), or fail after `ms`. */
async function until(cond, ms, what) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (cond()) return;
    await sleep(100);
  }
  assert.fail(`timed out waiting for: ${what}`);
}

// Test 0: env parsing + ignore helper parity with the Python side
{
  const o = mirrorOptionsFromEnv({
    SCH_MIRROR_IGNORE: '.git, node_modules ,dist',
    SCH_MIRROR_MAX_FILE_MB: '2',
    SCH_MIRROR_INCLUDE_GIT_SNAPSHOT: '1',
  });
  assert.deepEqual(o.ignore, ['.git', 'node_modules', 'dist']);
  assert.equal(o.maxBytes, 2 * 1024 * 1024);
  assert.equal(o.includeGitSnapshot, true);
  assert.deepEqual(mirrorOptionsFromEnv({}), {});
  assert.deepEqual(DEFAULT_IGNORE, [
    '.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache',
    '.ruff_cache', '.mypy_cache', '.cache', 'dist', 'build', 'coverage',
    'htmlcov', '.codebase-memory', 'dummy_data',
  ]);
  assert.ok(isIgnored('a/node_modules/b.js', ['node_modules']));
  assert.ok(isIgnored('lambda/api_handler/.venv/bin/python', DEFAULT_IGNORE));
  assert.ok(isIgnored('frontend/dist/assets/app.js', DEFAULT_IGNORE));
  assert.ok(!isIgnored('agentcore/app/.env', DEFAULT_IGNORE));
  assert.ok(!isIgnored('src/main.js', ['node_modules']));
  console.log('Test 0 (env options + ignore parity): PASS');
}

// Test 1: first hydration — empty mirror receives the full worktree
// INCLUDING the one-time .git snapshot (spec scenario "Prima idratazione
// del mirror"); atomicity leftovers absent.
await (async () => {
  const remote = tdir('remote1');
  const local = tdir('local1');
  mkdirSync(join(remote, 'src'), { recursive: true });
  writeFileSync(join(remote, 'src', 'app.py'), 'print("hi")\n');
  writeFileSync(join(remote, 'README.md'), '# proj\n');
  mkdirSync(join(remote, '.git', 'refs'), { recursive: true });
  writeFileSync(join(remote, '.git', 'HEAD'), 'ref: refs/heads/main\n');
  const big = Buffer.alloc(120_000, 0x41); // multi-chunk transfer (3 data chunks)
  writeFileSync(join(remote, 'big.bin'), big);

  await withSession(remote, local, {}, async () => {
    assert.equal(readFileSync(join(local, 'src', 'app.py'), 'utf8'), 'print("hi")\n');
    assert.equal(readFileSync(join(local, 'README.md'), 'utf8'), '# proj\n');
    assert.equal(readFileSync(join(local, '.git', 'HEAD'), 'utf8'), 'ref: refs/heads/main\n');
    assert.ok(readFileSync(join(local, 'big.bin')).equals(big));
    const leftovers = readdirSync(local).filter((n) => n.startsWith('.sch-sync-'));
    assert.deepEqual(leftovers, []);
  });
  console.log('Test 1 (first hydration incl. .git snapshot): PASS');
})();

// Test 2: delta hydration — second session transfers only the changed file
// (spec scenario "Riapertura con idratazione a delta"); local-only file is
// pushed to the remote; remote deletions do NOT delete locally (union).
await (async () => {
  const remote = tdir('remote2');
  const local = tdir('local2');
  writeFileSync(join(remote, 'stable.txt'), 'same');
  writeFileSync(join(remote, 'changed.txt'), 'v1');

  await withSession(remote, local, {}, async () => {});

  // between sessions: remote changes one file; local adds one
  await sleep(1100); // ensure a distinguishable mtime for LWW ordering
  writeFileSync(join(remote, 'changed.txt'), 'v2-remote');
  writeFileSync(join(local, 'local-new.txt'), 'from laptop');

  await withSession(remote, local, {}, async () => {
    assert.equal(readFileSync(join(local, 'changed.txt'), 'utf8'), 'v2-remote');
    assert.equal(readFileSync(join(local, 'stable.txt'), 'utf8'), 'same');
    await until(() => existsSync(join(remote, 'local-new.txt')), 5000, 'local-only file pushed');
    assert.equal(readFileSync(join(remote, 'local-new.txt'), 'utf8'), 'from laptop');
  });
  console.log('Test 2 (delta hydration both directions): PASS');
})();

// Test 3: live remote->local — a file created remotely during the session
// appears in the mirror (spec scenario "Agent change visible in
// the editor")
await (async () => {
  const remote = tdir('remote3');
  const local = tdir('local3');
  writeFileSync(join(remote, 'seed.txt'), 'seed');
  await withSession(remote, local, {}, async () => {
    writeFileSync(join(remote, 'agent-made.txt'), 'hello from agent');
    await until(() => existsSync(join(local, 'agent-made.txt')), 8000, 'remote create propagates');
    assert.equal(readFileSync(join(local, 'agent-made.txt'), 'utf8'), 'hello from agent');
    // remote deletion propagates live
    unlinkSync(join(remote, 'agent-made.txt'));
    await until(() => !existsSync(join(local, 'agent-made.txt')), 8000, 'remote delete propagates');
  });
  console.log('Test 3 (live remote->local create+delete): PASS');
})();

// Test 4: live local->remote — operator save propagates (spec scenario
// "Salvataggio locale visibile all'agente")
await (async () => {
  const remote = tdir('remote4');
  const local = tdir('local4');
  writeFileSync(join(remote, 'seed.txt'), 'seed');
  await withSession(remote, local, {}, async () => {
    writeFileSync(join(local, 'edited.txt'), 'operator save v1');
    await until(() => existsSync(join(remote, 'edited.txt')), 8000, 'local create propagates');
    assert.equal(readFileSync(join(remote, 'edited.txt'), 'utf8'), 'operator save v1');
    writeFileSync(join(local, 'edited.txt'), 'operator save v2');
    await until(
      () => existsSync(join(remote, 'edited.txt')) && readFileSync(join(remote, 'edited.txt'), 'utf8') === 'operator save v2',
      8000, 'local edit propagates',
    );
  });
  console.log('Test 4 (live local->remote save): PASS');
})();

// Test 5: LWW conflict — remote event older than a differing local copy:
// local wins + explicit warning (spec scenario "Concurrent conflict on
// the same file")
await (async () => {
  const remote = tdir('remote5');
  const local = tdir('local5');
  writeFileSync(join(remote, 'clash.txt'), 'base');
  await withSession(remote, local, {}, async (sync, warnings) => {
    // Make the REMOTE copy look stale: rewrite it with an old mtime so the
    // scanner emits an event whose m is OLDER than our local save.
    writeFileSync(join(local, 'clash.txt'), 'local latest');
    const old = (Date.now() - 3_600_000) / 1000;
    writeFileSync(join(remote, 'clash.txt'), 'remote stale');
    utimesSync(join(remote, 'clash.txt'), old, old);
    await until(
      () => warnings.some((w) => w.includes("CONFLICT on 'clash.txt'") && w.includes('LOCAL')),
      10_000, 'LWW warning emitted',
    );
    assert.equal(readFileSync(join(local, 'clash.txt'), 'utf8'), 'local latest'); // kept
    await until(
      () => readFileSync(join(remote, 'clash.txt'), 'utf8') === 'local latest',
      8000, 'local winner re-announced to remote',
    );
  });
  console.log('Test 5 (LWW local-newer wins + warning): PASS');
})();

// Test 6: ignore list + per-file threshold, both directions (spec scenario
// "File oltre soglia escluso")
await (async () => {
  const remote = tdir('remote6');
  const local = tdir('local6');
  mkdirSync(join(remote, 'node_modules', 'x'), { recursive: true });
  writeFileSync(join(remote, 'node_modules', 'x', 'lib.js'), 'ignored');
  writeFileSync(join(remote, 'huge.bin'), Buffer.alloc(64 * 1024, 1));
  writeFileSync(join(remote, 'ok.txt'), 'fine');
  await withSession(remote, local, { ignore: ['.git', 'node_modules'], maxBytes: 32 * 1024 }, async (sync, warnings) => {
    assert.equal(readFileSync(join(local, 'ok.txt'), 'utf8'), 'fine');
    assert.ok(!existsSync(join(local, 'node_modules')), 'ignored dir must not sync');
    assert.ok(!existsSync(join(local, 'huge.bin')), 'oversize must not sync');
    await until(
      () => warnings.some((w) => w.includes('huge.bin') && (w.includes('excluded') || w.includes('skipped'))),
      5000, 'threshold warning surfaced',
    );
    // local side enforces the same policy outbound
    writeFileSync(join(local, 'too-big-local.bin'), Buffer.alloc(40 * 1024, 2));
    await sleep(1500);
    assert.ok(!existsSync(join(remote, 'too-big-local.bin')));
  });
  console.log('Test 6 (ignore list + threshold both ways): PASS');
})();

// Test 7: echo suppression — an applied remote change must not bounce back
// (no infinite loop); steady state exchanges no file events. Uses a wire
// tap on the duplex to count file events after quiescence.
await (async () => {
  const remote = tdir('remote7');
  const local = tdir('local7');
  writeFileSync(join(remote, 'ping.txt'), 'v1');
  const { duplex, child } = spawnWorker(remote);
  const sentFileEvents = [];
  const origWrite = duplex.write.bind(duplex);
  duplex.write = (chunk, ...rest) => {
    for (const line of chunk.toString().split('\n')) {
      if (!line.trim()) continue;
      try {
        const m = JSON.parse(line);
        if (m.type === 'file') sentFileEvents.push(m);
      } catch { /* partial */ }
    }
    return origWrite(chunk, ...rest);
  };
  const sync = new MirrorSync({ stream: duplex, root: local, hydrationTimeoutMs: 30_000 });
  sync.on('warning', () => {});
  await sync.start();
  writeFileSync(join(remote, 'ping.txt'), 'v2'); // remote change -> applied locally
  await until(() => {
    try { return readFileSync(join(local, 'ping.txt'), 'utf8') === 'v2'; } catch { return false; }
  }, 8000, 'remote change applied');
  await sleep(2500); // give any echo a chance to fire
  assert.deepEqual(sentFileEvents.filter((m) => m.p === 'ping.txt'), [], 'applied change must not echo back');
  sync.close();
  child.kill('SIGTERM');
  console.log('Test 7 (echo suppression): PASS');
})();

console.log('mirror.test.js: ALL PASS');
process.exit(0);
