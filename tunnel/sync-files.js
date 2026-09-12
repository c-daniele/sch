// sync-files.js -- local filesystem primitives shared by sync lifecycles.

import { createHash } from 'node:crypto';
import { promises as fsp } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';

export function hashBuffer(buffer) {
  return createHash('sha1').update(buffer).digest('hex').slice(0, 16);
}

export function isSafeRelativePath(path) {
  return typeof path === 'string'
    && path.length > 0
    && !path.startsWith('/')
    && !path.startsWith('~')
    && !path.split('/').includes('..')
    && !path.includes('\\');
}

async function targetFor(root, path) {
  if (!isSafeRelativePath(path)) throw new Error(`unsafe sync path '${path}'`);
  const rootReal = await fsp.realpath(root);
  const target = join(rootReal, ...path.split('/'));
  await fsp.mkdir(dirname(target), { recursive: true });
  const parentReal = await fsp.realpath(dirname(target));
  if (parentReal !== rootReal && !parentReal.startsWith(`${rootReal}/`)) {
    throw new Error(`sync path escapes root through a symlink '${path}'`);
  }
  return target;
}

export async function scanFiles(root, {
  ignore, maxBytes, includeGit = false, onSkip = () => {},
}) {
  const result = new Map();
  const walk = async (dir) => {
    let entries;
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const item of entries) {
      const abs = join(dir, item.name);
      const rel = relative(root, abs).split(sep).join('/');
      if (item.name.startsWith('.sch-sync-')) continue;
      const inGit = rel === '.git' || rel.startsWith('.git/');
      if (inGit && !includeGit) continue;
      if (!inGit && ignore(rel)) {
        onSkip(rel, 0, 'ignored');
        continue;
      }
      if (item.isSymbolicLink()) {
        onSkip(rel, 0, 'symlink');
      } else if (item.isDirectory()) {
        await walk(abs);
      } else if (item.isFile()) {
        const stat = await fsp.lstat(abs);
        if (stat.size > maxBytes) {
          onSkip(rel, stat.size, 'size');
          continue;
        }
        result.set(rel, {
          s: stat.size,
          m: Math.floor(stat.mtimeMs),
          h: hashBuffer(await fsp.readFile(abs)),
          x: (stat.mode & 0o100) !== 0,
        });
      }
    }
  };
  await walk(root);
  return result;
}

export async function applyAtomicPut(root, header, content, sequence) {
  const target = await targetFor(root, header.p);
  try {
    const existing = await fsp.lstat(target);
    if (existing.isSymbolicLink() || !existing.isFile()) {
      throw new Error(`refusing to replace non-regular file '${header.p}'`);
    }
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  const temp = join(dirname(target), `.sch-sync-${process.pid}-${sequence}.tmp`);
  await fsp.writeFile(temp, content);
  if (header.x) await fsp.chmod(temp, 0o755);
  const mtime = new Date(header.m);
  await fsp.utimes(temp, mtime, mtime);
  await fsp.rename(temp, target);
}

export async function applyAtomicDelete(root, rel) {
  try {
    const target = await targetFor(root, rel);
    const existing = await fsp.lstat(target);
    if (existing.isSymbolicLink() || !existing.isFile()) {
      throw new Error(`refusing to delete non-regular file '${rel}'`);
    }
    await fsp.unlink(target);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
}
