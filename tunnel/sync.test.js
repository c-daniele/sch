import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Duplex } from 'node:stream';
import { FsSyncClient, runSync, startAcpSync } from './sync.js';

const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 50));
  }
  assert.fail(`timed out waiting for ${label}`);
};

const worker = join(dirname(fileURLToPath(import.meta.url)), '..', 'image', 'app', 'fs_sync_worker.py');
const local = mkdtempSync(join(tmpdir(), 'sch-sync-local-'));
const remote = mkdtempSync(join(tmpdir(), 'sch-sync-remote-'));
writeFileSync(join(local, 'local.txt'), 'local contents');
writeFileSync(join(remote, 'remote.txt'), 'remote contents');
const child = spawn('python3', [worker, '--root', remote], { stdio: ['pipe', 'pipe', 'pipe'] });
child.stderr.on('data', () => {});
const stream = Duplex.from({ readable: child.stdout, writable: child.stdin });
stream.closeStream = () => stream.destroy();
const transport = { open: async () => stream };

async function syncOnce(localRoot, remoteRoot, workspace, baseline, options = {}) {
  const process = spawn('python3', [worker, '--root', remoteRoot], { stdio: ['pipe', 'pipe', 'pipe'] });
  process.stderr.on('data', () => {});
  const channel = Duplex.from({ readable: process.stdout, writable: process.stdin });
  channel.on('error', () => {});
  channel.closeStream = () => channel.destroy();
  try {
    return await runSync({
      region: 'test', runtimeArn: 'test', sessionId: 'test', workspace, storage: 's3', root: localRoot,
      baseline, bootstrap: 'abort', conflict: 'abort', mode: 'once', ...options,
    }, { transport: { open: async () => channel }, log: () => {} });
  } finally {
    process.kill('SIGTERM');
  }
}

try {
  const silentStream = new Duplex({
    read() {},
    write(_chunk, _encoding, callback) { callback(); },
  });
  const silentClient = new FsSyncClient(silentStream, 'timeout-test', () => {}, 5);
  await assert.rejects(
    silentClient.start([], 1024),
    /timed out waiting for remote fs-sync hello/,
  );
  silentStream.destroy();

  // Union is required because both initial replicas contain independent data.
  const summary = await runSync({
    region: 'test', runtimeArn: 'test', sessionId: 'test', workspace: 'owner/ws', storage: 's3', root: local,
    baseline: join(local, '..', 'baseline.json'), bootstrap: 'union', conflict: 'abort', mode: 'once',
  }, { transport, log: () => {} });
  assert.equal(summary.operations, 2);
  assert.equal(readFileSync(join(local, 'remote.txt'), 'utf8'), 'remote contents');
  assert.equal(readFileSync(join(remote, 'local.txt'), 'utf8'), 'local contents');
  const baseline = JSON.parse(readFileSync(join(local, '..', 'baseline.json'), 'utf8'));
  assert.equal(baseline.workspace, 'owner/ws');
  assert.ok(baseline.manifest['local.txt'] && baseline.manifest['remote.txt']);
  // A watch helper remains active after ready, propagates local mutations,
  // then advances the baseline only after its final barrier on shutdown.
  const watchChild = spawn('python3', [worker, '--root', remote], { stdio: ['pipe', 'pipe', 'pipe'] });
  watchChild.stderr.on('data', () => {});
  const watchStream = Duplex.from({ readable: watchChild.stdout, writable: watchChild.stdin });
  watchStream.closeStream = () => watchStream.destroy();
  const watched = await runSync({
    region: 'test', runtimeArn: 'test', sessionId: 'test', workspace: 'owner/ws', storage: 's3', root: local,
    baseline: join(local, '..', 'baseline.json'), bootstrap: 'abort', conflict: 'abort', mode: 'watch',
  }, { transport: { open: async () => watchStream }, log: () => {} });
  writeFileSync(join(local, 'live.txt'), 'live contents');
  await waitFor(() => existsSync(join(remote, 'live.txt')), 'local watch push');
  unlinkSync(join(local, 'live.txt'));
  await waitFor(() => !existsSync(join(remote, 'live.txt')), 'local watch delete');
  await watched.close();
  watchChild.kill('SIGTERM');

  // ACP keeps its legacy-compatible transport, but now starts it through the
  // same sync lifecycle module as the v2 helper.
  const acpLocal = mkdtempSync(join(tmpdir(), 'sch-acp-sync-local-'));
  const acpRemote = mkdtempSync(join(tmpdir(), 'sch-acp-sync-remote-'));
  writeFileSync(join(acpRemote, 'from-acp.txt'), 'ACP compatibility');
  const acpChild = spawn('python3', [worker, '--root', acpRemote], { stdio: ['pipe', 'pipe', 'pipe'] });
  acpChild.stderr.on('data', () => {});
  const acpStream = Duplex.from({ readable: acpChild.stdout, writable: acpChild.stdin });
  // Destroying the worker after the ACP lifecycle closes its duplex stream.
  acpStream.on('error', () => {});
  acpStream.closeStream = () => acpStream.destroy();
  const acpSync = await startAcpSync({ stream: acpStream, root: acpLocal, log: () => {} });
  assert.equal(readFileSync(join(acpLocal, 'from-acp.txt'), 'utf8'), 'ACP compatibility');
  acpSync.close();
  acpChild.kill('SIGTERM');
  rmSync(acpLocal, { recursive: true, force: true });
  rmSync(acpRemote, { recursive: true, force: true });

  // Git metadata is excluded from standalone sync by default, even during
  // bootstrap. The source tree still converges normally.
  const gitLocal = mkdtempSync(join(tmpdir(), 'sch-git-local-'));
  const gitRemote = mkdtempSync(join(tmpdir(), 'sch-git-remote-'));
  mkdirSync(join(gitLocal, '.git'));
  writeFileSync(join(gitLocal, '.git', 'HEAD'), 'ref: refs/heads/main\n');
  writeFileSync(join(gitLocal, 'project.txt'), 'from local');
  await syncOnce(gitLocal, gitRemote, 'owner/git-local', join(gitLocal, '..', 'git-local.json'));
  assert.equal(readFileSync(join(gitRemote, 'project.txt'), 'utf8'), 'from local');
  assert.ok(!existsSync(join(gitRemote, '.git', 'HEAD')));

  // Workflows that require history and references can opt into the one-time
  // snapshot explicitly.
  const gitSnapshotRemote = mkdtempSync(join(tmpdir(), 'sch-git-snapshot-remote-'));
  const previousGitSnapshot = process.env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT;
  process.env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT = '1';
  try {
    await syncOnce(gitLocal, gitSnapshotRemote, 'owner/git-local-snapshot', join(gitLocal, '..', 'git-local-snapshot.json'));
  } finally {
    if (previousGitSnapshot === undefined) delete process.env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT;
    else process.env.SCH_MIRROR_INCLUDE_GIT_SNAPSHOT = previousGitSnapshot;
  }
  assert.equal(readFileSync(join(gitSnapshotRemote, '.git', 'HEAD'), 'utf8'), 'ref: refs/heads/main\n');

  const pullLocal = mkdtempSync(join(tmpdir(), 'sch-git-pull-local-'));
  const pullRemote = mkdtempSync(join(tmpdir(), 'sch-git-pull-remote-'));
  mkdirSync(join(pullRemote, '.git'));
  writeFileSync(join(pullRemote, '.git', 'HEAD'), 'ref: refs/heads/main\n');
  writeFileSync(join(pullRemote, 'project.txt'), 'from remote');
  await syncOnce(pullLocal, pullRemote, 'owner/git-remote', join(pullLocal, '..', 'git-remote.json'));
  assert.equal(readFileSync(join(pullLocal, 'project.txt'), 'utf8'), 'from remote');
  assert.ok(!existsSync(join(pullLocal, '.git', 'HEAD')));

  // A remote-only change made while the laptop was detached is imported on
  // the next three-way preflight because the local side still matches the
  // saved baseline.
  const detachedLocal = mkdtempSync(join(tmpdir(), 'sch-detached-local-'));
  const detachedRemote = mkdtempSync(join(tmpdir(), 'sch-detached-remote-'));
  const detachedBaseline = join(detachedLocal, '..', `detached-${Date.now()}.json`);
  writeFileSync(join(detachedLocal, 'result.txt'), 'before');
  await syncOnce(detachedLocal, detachedRemote, 'owner/detached', detachedBaseline);
  writeFileSync(join(detachedRemote, 'result.txt'), 'after remote task');
  await syncOnce(detachedLocal, detachedRemote, 'owner/detached', detachedBaseline);
  assert.equal(readFileSync(join(detachedLocal, 'result.txt'), 'utf8'), 'after remote task');

  // Claude provisioning seeds a root .mcp.json before the first sync. It is
  // infrastructure: default bootstrap must neither conflict nor download it,
  // and later three-way syncs must continue to leave it remote-only.
  const claudeLocal = mkdtempSync(join(tmpdir(), 'sch-claude-local-'));
  const claudeRemote = mkdtempSync(join(tmpdir(), 'sch-claude-remote-'));
  const claudeBaseline = join(claudeLocal, '..', `claude-${Date.now()}.json`);
  writeFileSync(join(claudeLocal, 'project.txt'), 'from local');
  writeFileSync(join(claudeRemote, '.mcp.json'), '{"mcpServers":{"aws-docs":{}}}\n');
  await syncOnce(claudeLocal, claudeRemote, 'owner/claude', claudeBaseline);
  assert.equal(readFileSync(join(claudeRemote, 'project.txt'), 'utf8'), 'from local');
  assert.ok(existsSync(join(claudeRemote, '.mcp.json')));
  assert.ok(!existsSync(join(claudeLocal, '.mcp.json')));
  assert.ok(!JSON.parse(readFileSync(claudeBaseline, 'utf8')).manifest['.mcp.json']);
  writeFileSync(join(claudeRemote, 'task.txt'), 'remote result');
  await syncOnce(claudeLocal, claudeRemote, 'owner/claude', claudeBaseline);
  assert.equal(readFileSync(join(claudeLocal, 'task.txt'), 'utf8'), 'remote result');
  assert.ok(existsSync(join(claudeRemote, '.mcp.json')));
  assert.ok(!existsSync(join(claudeLocal, '.mcp.json')));

  const claudeWatchChild = spawn('python3', [worker, '--root', claudeRemote], { stdio: ['pipe', 'pipe', 'pipe'] });
  claudeWatchChild.stderr.on('data', () => {});
  const claudeWatchStream = Duplex.from({ readable: claudeWatchChild.stdout, writable: claudeWatchChild.stdin });
  claudeWatchStream.on('error', () => {});
  claudeWatchStream.closeStream = () => claudeWatchStream.destroy();
  const claudeWatch = await runSync({
    region: 'test', runtimeArn: 'test', sessionId: 'test', workspace: 'owner/claude', storage: 's3', root: claudeLocal,
    baseline: claudeBaseline, bootstrap: 'abort', conflict: 'abort', mode: 'watch',
  }, { transport: { open: async () => claudeWatchStream }, log: () => {} });
  writeFileSync(join(claudeRemote, '.mcp.json'), '{"mcpServers":{"aws-api":{}}}\n');
  writeFileSync(join(claudeLocal, 'watch.txt'), 'watch active');
  await waitFor(() => existsSync(join(claudeRemote, 'watch.txt')), 'Claude watch push');
  assert.ok(!existsSync(join(claudeLocal, '.mcp.json')));
  await claudeWatch.close();
  claudeWatchChild.kill('SIGTERM');

  // Only the exact root path is exempt; nested project MCP configuration is
  // ordinary user content and syncs in both bootstrap and three-way modes.
  mkdirSync(join(claudeLocal, 'foo'));
  writeFileSync(join(claudeLocal, 'foo', '.mcp.json'), '{"user":true}\n');
  await syncOnce(claudeLocal, claudeRemote, 'owner/claude', claudeBaseline);
  assert.equal(readFileSync(join(claudeRemote, 'foo', '.mcp.json'), 'utf8'), '{"user":true}\n');
  rmSync(gitLocal, { recursive: true, force: true });
  rmSync(gitRemote, { recursive: true, force: true });
  rmSync(gitSnapshotRemote, { recursive: true, force: true });
  rmSync(pullLocal, { recursive: true, force: true });
  rmSync(pullRemote, { recursive: true, force: true });
  rmSync(detachedLocal, { recursive: true, force: true });
  rmSync(detachedRemote, { recursive: true, force: true });
  rmSync(detachedBaseline, { force: true });
  rmSync(claudeLocal, { recursive: true, force: true });
  rmSync(claudeRemote, { recursive: true, force: true });
  rmSync(claudeBaseline, { force: true });
  console.log('sync.test.js: ALL PASS');
} finally {
  child.kill('SIGTERM');
  rmSync(local, { recursive: true, force: true });
  rmSync(remote, { recursive: true, force: true });
}
