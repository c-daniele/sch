#!/usr/bin/env node
import { pathToFileURL } from 'node:url';
import { WebSocketStreamTransport } from './transport.js';
import { openTcpBridge } from './tcp-bridge.js';

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--region') out.region = argv[++i];
    else if (arg === '--runtime-arn') out.runtimeArn = argv[++i];
    else if (arg === '--session-id') out.sessionId = argv[++i];
    else if (arg === '--workspace') out.workspace = argv[++i];
    else if (arg === '--storage') out.storage = argv[++i];
    else if (arg === '--session-epoch') out.sessionEpoch = Number.parseInt(argv[++i], 10);
    else if (arg === '--remote-port') out.remotePort = Number.parseInt(argv[++i], 10);
  }
  const missing = ['region', 'runtimeArn', 'sessionId', 'workspace', 'storage', 'remotePort'].filter((key) => !out[key]);
  if (missing.length > 0) {
    throw new Error(`missing required args: ${missing.join(', ')}`);
  }
  return out;
}

export async function runWeb(argv, {
  signalEmitter = process,
  writeError = (message) => process.stderr.write(message),
  transportFactory = (opts) => new WebSocketStreamTransport(opts),
} = {}) {
  const opts = parseArgs(argv);
  const transport = transportFactory(opts);
  const bridge = await openTcpBridge({
    transport,
    label: 'web',
    writeError,
    remote: {
      kind: 'tcp', port: opts.remotePort,
      workspace: opts.workspace, storage: opts.storage,
      sessionEpoch: opts.sessionEpoch,
    },
  });
  const url = `http://127.0.0.1:${bridge.localPort}`;
  writeError(`${JSON.stringify({ type: 'ready', host: '127.0.0.1', port: bridge.localPort, url })}\n`);

  let stop;
  await new Promise((resolve) => {
    stop = resolve;
    signalEmitter.once('SIGINT', stop);
    signalEmitter.once('SIGTERM', stop);
  });
  signalEmitter.off('SIGINT', stop);
  signalEmitter.off('SIGTERM', stop);
  await bridge.shutdown();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runWeb(process.argv.slice(2)).catch((err) => {
    process.stderr.write(`sch: fatal: ${err.stack ?? err}\n`);
    process.exitCode = 1;
  });
}
