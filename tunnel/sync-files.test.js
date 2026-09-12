import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { applyAtomicDelete, applyAtomicPut, isSafeRelativePath, scanFiles } from './sync-files.js';

const root = mkdtempSync(join(tmpdir(), 'sch-sync-files-'));
try {
  assert(isSafeRelativePath('src/file.txt'));
  for (const path of ['', '../escape', '/absolute', 'a\\b', 'a/../b']) assert(!isSafeRelativePath(path));
  await assert.rejects(applyAtomicPut(root, { p: '../escape', m: 0, x: false }, Buffer.from('bad'), 1), /unsafe/);
  writeFileSync(join(root, 'safe.txt'), 'old');
  await applyAtomicPut(root, { p: 'safe.txt', m: 1_000, x: false }, Buffer.from('new'), 2);
  assert.equal(readFileSync(join(root, 'safe.txt'), 'utf8'), 'new');
  mkdirSync(join(root, 'dir'));
  await assert.rejects(applyAtomicDelete(root, 'dir'), /non-regular/);
  writeFileSync(join(root, 'outside.txt'), 'outside');
  symlinkSync(join(root, 'outside.txt'), join(root, 'link.txt'));
  await assert.rejects(applyAtomicPut(root, { p: 'link.txt', m: 1_000, x: false }, Buffer.from('bad'), 3), /non-regular/);
  const outside = mkdtempSync(join(tmpdir(), 'sch-sync-outside-'));
  symlinkSync(outside, join(root, 'redirect'));
  await assert.rejects(applyAtomicPut(root, { p: 'redirect/escape.txt', m: 1_000, x: false }, Buffer.from('bad'), 4), /escapes root/);
  rmSync(outside, { recursive: true, force: true });
  const manifest = await scanFiles(root, { ignore: () => false, maxBytes: 1024 });
  assert(!manifest.has('link.txt'));
  console.log('sync-files.test.js: ALL PASS');
} finally {
  rmSync(root, { recursive: true, force: true });
}
