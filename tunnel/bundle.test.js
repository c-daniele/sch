// bundle.test.js — offline tests for the git bundle transfer helper
// (add-git-native-workflow, tasks 2.1/2.2/2.3): upload local->staging and
// download staging->local against the real fs_sync_worker.py, including a
// realistically large (multi-MB, > DEFAULT_MAX_BYTES) binary payload.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Duplex } from 'node:stream';
import { parseArgs, runBundleTransfer } from './bundle.js';

const worker = join(dirname(fileURLToPath(import.meta.url)), '..', 'image', 'app', 'fs_sync_worker.py');

function workerTransport(root) {
  const child = spawn('python3', [worker, '--root', root], { stdio: ['pipe', 'pipe', 'pipe'] });
  child.stderr.on('data', () => {});
  const stream = Duplex.from({ readable: child.stdout, writable: child.stdin });
  stream.on('error', () => {});
  stream.closeStream = async () => { stream.destroy(); child.kill('SIGTERM'); };
  return { transport: { open: async () => stream }, child };
}

const local = mkdtempSync(join(tmpdir(), 'sch-bundle-local-'));
const staging = mkdtempSync(join(tmpdir(), 'sch-bundle-staging-'));

const base = {
  region: 'test', runtimeArn: 'test', sessionId: 'test', workspace: 'ws',
  storage: 'session', sessionEpoch: 0, stagingRoot: staging,
};

try {
  // --- parseArgs contract ---------------------------------------------------
  assert.throws(() => parseArgs(['--region', 'r']), /missing required/);
  assert.throws(() => parseArgs([
    '--region', 'r', '--runtime-arn', 'a', '--session-id', 's', '--workspace', 'w',
    '--staging-root', '/x', '--name', 'seed.bundle', '--storage', 'session',
  ]), /--upload\/--download/);
  assert.throws(() => parseArgs([
    '--region', 'r', '--runtime-arn', 'a', '--session-id', 's', '--workspace', 'w',
    '--staging-root', '/x', '--name', '../evil', '--storage', 'session', '--upload', '/f',
  ]), /invalid --name/);
  const parsed = parseArgs([
    '--region', 'r', '--runtime-arn', 'a', '--session-id', 's', '--workspace', 'w',
    '--staging-root', '/x', '--name', 'seed.bundle', '--storage', 's3',
    '--upload', '/f', '--session-epoch', '3',
  ]);
  assert.equal(parsed.stagingRoot, '/x');
  assert.equal(parsed.sessionEpoch, 3);
  console.log('Test 0 (parseArgs validation): PASS');

  // --- upload: small bundle ---------------------------------------------------
  const seedPath = join(local, 'seed.bundle');
  writeFileSync(seedPath, 'fake bundle payload');
  {
    const { transport, child } = workerTransport(staging);
    const result = await runBundleTransfer(
      { ...base, upload: seedPath, name: 'seed.bundle' },
      { log: () => {}, transport },
    );
    child.kill('SIGTERM');
    assert.equal(result.bytes, 19);
    assert.equal(readFileSync(join(staging, 'seed.bundle'), 'utf8'), 'fake bundle payload');
  }
  console.log('Test 1 (upload local -> staging): PASS');

  // --- upload: large binary bundle (> fs-sync DEFAULT_MAX_BYTES of 10MB) ------
  const bigPath = join(local, 'big.bundle');
  const bigContent = randomBytes(12 * 1024 * 1024);
  writeFileSync(bigPath, bigContent);
  {
    const { transport, child } = workerTransport(staging);
    const result = await runBundleTransfer(
      { ...base, upload: bigPath, name: 'big.bundle' },
      { log: () => {}, transport },
    );
    child.kill('SIGTERM');
    assert.equal(result.bytes, bigContent.length);
    assert.ok(readFileSync(join(staging, 'big.bundle')).equals(bigContent));
  }
  console.log('Test 2 (large 12MB binary upload, above the mirror default threshold): PASS');

  // --- download: staging -> local temp file ------------------------------------
  const deliveryContent = randomBytes(11 * 1024 * 1024);
  writeFileSync(join(staging, 'delivery.bundle'), deliveryContent);
  const downloadPath = join(local, 'delivery-downloaded.bundle');
  {
    const { transport, child } = workerTransport(staging);
    const result = await runBundleTransfer(
      { ...base, download: downloadPath, name: 'delivery.bundle' },
      { log: () => {}, transport },
    );
    child.kill('SIGTERM');
    assert.equal(result.bytes, deliveryContent.length);
    assert.ok(readFileSync(downloadPath).equals(deliveryContent));
  }
  console.log('Test 3 (large 11MB download staging -> local): PASS');

  // --- download: missing remote bundle fails cleanly ---------------------------
  {
    const { transport, child } = workerTransport(staging);
    await assert.rejects(
      runBundleTransfer(
        { ...base, download: join(local, 'nope.bundle'), name: 'nope.bundle' },
        { log: () => {}, transport },
      ),
      /not found in staging/,
    );
    child.kill('SIGTERM');
    assert.ok(!existsSync(join(local, 'nope.bundle')));
  }
  console.log('Test 4 (missing remote bundle -> clean error): PASS');

  console.log('bundle.test.js: ALL PASS');
} finally {
  rmSync(local, { recursive: true, force: true });
  rmSync(staging, { recursive: true, force: true });
}
