// sync-plan.js -- side-effect-free planning and local baseline persistence.
// The sync helper owns transport/apply; this module makes every destructive
// decision from complete manifests before an operation is sent.

import { createHash } from 'node:crypto';
import { promises as fsp } from 'node:fs';
import { dirname } from 'node:path';

export const BASELINE_VERSION = 1;
// These root-level files are workspace infrastructure, not user sync state.
// Exact matching is intentional: nested files with the same names remain
// ordinary project content. A user-authored root .mcp.json is therefore also
// left local; a pre-existing file from a Git clone is handled by provisioning.
export const SYNC_EXEMPT_PATHS = new Set(['.sch-initialized', '.mcp.json']);

function same(a, b) {
  return a === b || (!!a && !!b && a.h === b.h && a.x === b.x && a.s === b.s);
}

function entry(manifest, path) {
  return manifest.get(path) ?? null;
}

export function manifestHash(manifest) {
  const canonical = [...manifest.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([p, value]) => ({ p, s: value.s, h: value.h, x: !!value.x }));
  return createHash('sha256').update(JSON.stringify(canonical)).digest('hex');
}

export function semanticEntries(manifest) {
  return [...manifest.entries()].filter(([path]) => (
    path !== '.git' && !path.startsWith('.git/') && !SYNC_EXEMPT_PATHS.has(path)
  ));
}

export function stripExempt(manifest) {
  return new Map([...manifest].filter(([path]) => !SYNC_EXEMPT_PATHS.has(path)));
}

export function isSemanticallyEmpty(manifest) {
  return semanticEntries(manifest).length === 0;
}

function allPaths(...manifests) {
  return [...new Set(manifests.flatMap((manifest) => [...manifest.keys()]))].sort();
}

function put(side, path, value, reason) {
  return { kind: 'put', side, path, value, reason };
}

function del(side, path, reason) {
  return { kind: 'del', side, path, reason };
}

function copy(source, target, path, value, reason) {
  return value ? put(target, path, value, reason) : del(target, path, reason);
}

function collision(path, local, remote, base = null) {
  return { path, local, remote, base, reason: 'content-differs' };
}

export function planBootstrap(local, remote, policy = 'abort') {
  local = stripExempt(local);
  remote = stripExempt(remote);
  const localEmpty = isSemanticallyEmpty(local);
  const remoteEmpty = isSemanticallyEmpty(remote);
  const operations = [];
  const conflicts = [];

  if (localEmpty && remoteEmpty) return { operations, conflicts, converged: true };
  if (localEmpty) {
    for (const [path, value] of remote) operations.push(put('local', path, value, 'bootstrap-remote'));
    return { operations, conflicts, converged: true };
  }
  if (remoteEmpty) {
    for (const [path, value] of local) operations.push(put('remote', path, value, 'bootstrap-local'));
    return { operations, conflicts, converged: true };
  }

  const paths = allPaths(local, remote);
  const differences = [];
  for (const path of paths) {
    const left = entry(local, path);
    const right = entry(remote, path);
    if (same(left, right)) continue;
    differences.push({ path, left, right });
    if (left && right) conflicts.push(collision(path, left, right));
  }
  if (differences.length === 0) return { operations, conflicts, converged: true };
  if (policy === 'abort') {
    return {
      operations: [],
      conflicts: differences.map(({ path, left, right }) => collision(path, left, right)),
      converged: false,
    };
  }
  if (policy === 'union') {
    if (conflicts.length) return { operations: [], conflicts, converged: false };
    for (const { path, left, right } of differences) {
      if (left) operations.push(put('remote', path, left, 'bootstrap-union'));
      else operations.push(put('local', path, right, 'bootstrap-union'));
    }
    return { operations, conflicts, converged: true };
  }
  if (policy === 'local-wins' || policy === 'remote-wins') {
    const source = policy === 'local-wins' ? local : remote;
    const target = policy === 'local-wins' ? 'remote' : 'local';
    for (const path of paths) operations.push(copy(source, target, path, entry(source, path), `bootstrap-${policy}`));
    return { operations, conflicts, converged: true };
  }
  return { operations: [], conflicts, converged: false };
}

export function conflictName(path, local, occupied) {
  const suffix = `.sch-conflict-local-${(local?.h ?? 'deleted').slice(0, 8)}`;
  let candidate = `${path}${suffix}`;
  let index = 2;
  while (occupied.has(candidate)) candidate = `${path}${suffix}-${index++}`;
  return candidate;
}

export function planThreeWay(base, local, remote, policy = 'abort') {
  base = stripExempt(base);
  local = stripExempt(local);
  remote = stripExempt(remote);
  const operations = [];
  const conflicts = [];
  const occupied = new Set(allPaths(base, local, remote));
  for (const path of allPaths(base, local, remote)) {
    const b = entry(base, path);
    const l = entry(local, path);
    const r = entry(remote, path);
    if (same(l, r)) continue;
    if (same(l, b)) {
      operations.push(copy(remote, 'local', path, r, 'remote-change'));
      continue;
    }
    if (same(r, b)) {
      operations.push(copy(local, 'remote', path, l, 'local-change'));
      continue;
    }
    const found = collision(path, l, r, b);
    conflicts.push(found);
    if (policy === 'local-wins') operations.push(copy(local, 'remote', path, l, 'conflict-local-wins'));
    else if (policy === 'remote-wins') operations.push(copy(remote, 'local', path, r, 'conflict-remote-wins'));
    else if (policy === 'keep-both') {
      const preserved = conflictName(path, l, occupied);
      occupied.add(preserved);
      operations.push(put('local', preserved, l, 'conflict-keep-both'));
      operations.push(put('remote', preserved, l, 'conflict-keep-both'));
      found.preserved = { side: 'local', path: preserved };
    }
  }
  if (conflicts.length && policy === 'abort') return { operations: [], conflicts, converged: false };
  return { operations, conflicts, converged: true };
}

export function makeBaseline(workspace, rootFingerprint, manifest, gitFingerprint = null) {
  return {
    version: BASELINE_VERSION,
    workspace,
    rootFingerprint,
    manifest: Object.fromEntries(manifest),
    gitFingerprint,
  };
}

export function parseBaseline(raw, workspace, rootFingerprint) {
  try {
    const baseline = JSON.parse(raw);
    if (baseline.version !== BASELINE_VERSION || baseline.workspace !== workspace || baseline.rootFingerprint !== rootFingerprint) return null;
    if (!baseline.manifest || typeof baseline.manifest !== 'object' || Array.isArray(baseline.manifest)) return null;
    return new Map(Object.entries(baseline.manifest));
  } catch {
    return null;
  }
}

export async function writeBaseline(path, baseline) {
  await fsp.mkdir(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}.${Date.now()}.tmp`;
  await fsp.writeFile(temp, `${JSON.stringify(baseline)}\n`, { mode: 0o600 });
  await fsp.rename(temp, path);
}
