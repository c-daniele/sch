import net from 'node:net';

const DEFAULT_MAX_CHANNELS = 32;

export function maxChannelsFromEnv(env = process.env) {
  const value = Number.parseInt(env.SCH_TUNNEL_MAX_CHANNELS ?? '', 10);
  return Number.isInteger(value) && value > 0 ? value : DEFAULT_MAX_CHANNELS;
}

export async function openTcpBridge({
  transport,
  remote,
  maxChannels = maxChannelsFromEnv(),
  label = 'tunnel',
  writeError = (message) => process.stderr.write(message),
}) {
  const connections = new Set();
  const opening = new Set();
  let shuttingDown = false;

  const server = net.createServer((socket) => {
    if (shuttingDown || connections.size >= maxChannels) {
      writeError(`sch: ${label} channel cap reached (${maxChannels}); rejecting new local connection\n`);
      socket.destroy();
      return;
    }

    const connection = { socket, stream: null, closeStarted: false };
    connections.add(connection); // Reserve capacity before the async open.
    socket.pause();

    const release = () => connections.delete(connection);
    const closeStream = () => {
      if (!connection.stream || connection.closeStarted) return null;
      connection.closeStarted = true;
      return connection.stream.closeStream();
    };
    socket.once('close', () => {
      release();
      void closeStream();
    });
    socket.once('error', () => { void closeStream(); });

    const open = Promise.resolve()
      .then(() => transport.open({ remote }))
      .then(async (stream) => {
        connection.stream = stream;
        if (shuttingDown || socket.destroyed) {
          release();
          await closeStream();
          return;
        }

        const closeSocket = () => {
          release();
          socket.destroy();
        };
        stream.once('closed', () => {
          connection.closeStarted = true;
          closeSocket();
        });
        stream.once('error', closeSocket);
        socket.pipe(stream);
        stream.pipe(socket);
        socket.resume();
      })
      .catch((err) => {
        release();
        writeError(`sch: failed to open ${label} channel: ${err.message ?? err}\n`);
        socket.destroy();
      })
      .finally(() => opening.delete(open));
    opening.add(open);
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  let shutdownPromise = null;
  const shutdown = () => {
    if (shutdownPromise) return shutdownPromise;
    shuttingDown = true;
    shutdownPromise = (async () => {
      const serverClosed = new Promise((resolve) => server.close(resolve));
      const closes = [];
      for (const connection of connections) {
        connection.socket.destroy();
        const close = closeStreamFor(connection);
        if (close) closes.push(close);
      }
      await Promise.allSettled(closes);
      await Promise.allSettled([...opening]);
      await serverClosed;
      connections.clear();
    })();
    return shutdownPromise;
  };

  function closeStreamFor(connection) {
    if (!connection.stream || connection.closeStarted) return null;
    connection.closeStarted = true;
    return connection.stream.closeStream();
  }

  return { localPort: server.address().port, shutdown };
}
