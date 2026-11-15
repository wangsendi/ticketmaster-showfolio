'use strict';

// 纯HTTP CONNECT桥。仅在上游SOCKS握手阶段重连，转发应用数据后绝不重连。
const net = require('node:net');

function failure(code, retryable = false) {
  return Object.assign(new Error(code), { code, retryable });
}

function readExact(socket, size) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      socket.off('readable', read);
      socket.off('error', fail);
      socket.off('end', closed);
      socket.off('close', closed);
    };
    const fail = (error) => { cleanup(); reject(error); };
    const closed = () => fail(failure('socks_closed', true));
    const read = () => {
      const data = socket.read(size);
      if (data !== null) {
        cleanup();
        if (data.length === size) resolve(data);
        else reject(failure('socks_short_reply', true));
      }
      else if (socket.destroyed || socket.readableEnded) closed();
    };
    socket.on('readable', read);
    socket.once('error', fail);
    socket.once('end', closed);
    socket.once('close', closed);
    read();
  });
}

async function connectSocks(config, host, port, stats, track) {
  const user = Buffer.from(config.username, 'utf8');
  const password = Buffer.from(config.password, 'utf8');
  const destination = Buffer.from(host, 'utf8');
  if (!destination.length || destination.length > 255) throw failure('invalid_destination');
  for (let attempt = 0; attempt < config.maxAttempts; attempt++) {
    stats.upstreamConnectAttempts++;
    const upstream = net.createConnection({ host: config.socksHost, port: config.socksPort });
    track(upstream);
    // 保留错误监听，避免读阶段切换的间隙出现未处理事件；readExact仍会接收并拒绝错误。
    upstream.on('error', () => {});
    upstream.setTimeout(config.handshakeTimeoutMs, () => upstream.destroy(failure('socks_timeout', true)));
    try {
      await new Promise((resolve, reject) => {
        const connected = () => { upstream.off('error', reject); resolve(); };
        upstream.once('error', reject);
        upstream.once('connect', connected);
      });
      upstream.write(Buffer.from([5, 1, 2]));
      const greeting = await readExact(upstream, 2);
      if (greeting[0] !== 5 || greeting[1] !== 2) throw failure('socks_auth_method');
      upstream.write(Buffer.concat([Buffer.from([1, user.length]), user,
                                    Buffer.from([password.length]), password]));
      const auth = await readExact(upstream, 2);
      if (auth[0] !== 1 || auth[1] !== 0) throw failure('socks_auth_failed');
      const request = Buffer.alloc(7 + destination.length);
      request.set([5, 1, 0, 3, destination.length]);
      destination.copy(request, 5);
      request.writeUInt16BE(port, 5 + destination.length);
      upstream.write(request);
      const reply = await readExact(upstream, 4);
      if (reply[0] !== 5 || reply[2] !== 0) throw failure('socks_bad_reply');
      if (reply[1] !== 0) throw failure('socks_connect_refused', true);
      if (reply[3] === 1) await readExact(upstream, 6);
      else if (reply[3] === 4) await readExact(upstream, 18);
      else if (reply[3] === 3) {
        const length = (await readExact(upstream, 1))[0];
        await readExact(upstream, length + 2);
      } else throw failure('socks_bad_address');
      upstream.setTimeout(0);
      return upstream;
    } catch (error) {
      upstream.destroy();
      const connectionError = ['ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EPIPE'].includes(error.code);
      if ((!error.retryable && !connectionError) || attempt + 1 >= config.maxAttempts) throw error;
      stats.connectRetries++;
    }
  }
  throw failure('socks_connect_failed');
}

function readConnectHead(socket) {
  return new Promise((resolve, reject) => {
    let buffered = Buffer.alloc(0);
    const cleanup = () => {
      socket.off('data', data);
      socket.off('error', fail);
      socket.off('end', closed);
      socket.off('close', closed);
    };
    const fail = (error) => { cleanup(); reject(error); };
    const closed = () => fail(failure('client_closed'));
    const data = (chunk) => {
      buffered = Buffer.concat([buffered, chunk]);
      const end = buffered.indexOf('\r\n\r\n');
      if (end < 0 && buffered.length <= 16384) return;
      if (end < 0 || end > 16384) return fail(failure('connect_head_too_large'));
      socket.pause();
      cleanup();
      resolve({ header: buffered.subarray(0, end).toString('latin1'),
                leftover: buffered.subarray(end + 4) });
    };
    socket.on('data', data);
    socket.once('error', fail);
    socket.once('end', closed);
    socket.once('close', closed);
  });
}

function createBridge(settings) {
  const config = { maxAttempts: 2, handshakeTimeoutMs: 10000, allowHttp: false, ...settings };
  if (typeof config.allowHttp !== 'boolean' || !Number.isInteger(config.socksPort) || config.socksPort < 1 || config.socksPort > 65535 ||
      typeof config.socksHost !== 'string' || !config.socksHost ||
      ![1, 2].includes(config.maxAttempts) || !Number.isInteger(config.handshakeTimeoutMs) ||
      config.handshakeTimeoutMs < 1 || config.handshakeTimeoutMs > 30000) throw failure('invalid_proxy_config');
  for (const key of ['username', 'password']) {
    if (typeof config[key] !== 'string' || !Buffer.byteLength(config[key]) ||
        Buffer.byteLength(config[key]) > 255) throw failure('invalid_proxy_credentials');
  }
  const stats = { upstreamConnectAttempts: 0, connectRetries: 0, forwardedTunnels: 0, failedTunnels: 0 };
  const sockets = new Set();
  const track = (socket) => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  };
  const server = net.createServer((client) => {
    track(client);
    client.on('error', () => {});
    client.setTimeout(30000, () => client.destroy());
    (async () => {
      let upstream;
      try {
        const { header, leftover } = await readConnectHead(client);
        const match = header.match(/^CONNECT (?:\[([0-9a-fA-F:]+)\]|([^:\s/]+)):(\d+) HTTP\/1\.[01](?:\r\n|$)/);
        let host = match && (match[1] || match[2]);
        let port = match ? Number(match[3]) : 0;
        let originHeader = null;
        if (!match && config.allowHttp) {
          const lines = header.split('\r\n');
          const httpMatch = lines[0].match(/^([A-Z]+) (http:\/\/[^\s]+) HTTP\/1\.[01]$/);
          if (httpMatch) {
            const target = new URL(httpMatch[2]);
            if (!target.username && !target.password) {
              host = target.hostname.replace(/^\[|\]$/g, '');
              port = Number(target.port || 80);
              const headers = lines.slice(1).filter(line => !/^(host|connection|proxy-connection|proxy-authorization):/i.test(line));
              originHeader = `${httpMatch[1]} ${target.pathname}${target.search} HTTP/1.1\r\nHost: ${target.host}\r\nConnection: close\r\n${headers.length ? headers.join('\r\n') + '\r\n' : ''}\r\n`;
            }
          }
        }
        if ((!match && !originHeader) || !host || port < 1 || port > 65535) {
          client.end('HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n');
          return;
        }
        upstream = await connectSocks(config, host, port, stats, track);
        if (client.destroyed) { upstream.destroy(); return; }
        const cleanup = () => { client.destroy(); upstream.destroy(); };
        client.once('error', cleanup);
        client.once('close', () => upstream.destroy());
        upstream.once('error', cleanup);
        upstream.once('close', () => client.destroy());
        client.setTimeout(180000, cleanup);
        upstream.setTimeout(180000, cleanup);
        if (originHeader) upstream.write(originHeader);
        else client.write('HTTP/1.1 200 Connection established\r\n\r\n');
        // 提前到达的应用字节只写上游；CONNECT头始终留在桥内。
        if (leftover.length) upstream.write(leftover);
        stats.forwardedTunnels++;
        client.pipe(upstream).pipe(client);
      } catch (_) {
        if (upstream) upstream.destroy();
        stats.failedTunnels++;
        if (!client.destroyed) client.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n');
      }
    })();
  });
  server.stats = stats;
  server.shutdown = () => new Promise((resolve) => {
    for (const socket of sockets) socket.destroy();
    server.close(resolve);
  });
  return server;
}

module.exports = { createBridge };

if (require.main === module) {
  // 单行JSON经stdin输入，避免把上游凭据放进命令行；stdout只给本地地址和统计。
  const lines = require('node:readline').createInterface({ input: process.stdin });
  lines.once('line', (line) => {
    lines.close();
    try {
      const settings = JSON.parse(line);
      const server = createBridge(settings);
      server.once('error', () => { process.stderr.write('bridge_start_failed\n'); process.exit(2); });
      server.listen(settings.listenPort || 0, '127.0.0.1', () => {
        process.stdout.write(JSON.stringify({ listenURL: 'http://127.0.0.1:' + server.address().port }) + '\n');
      });
      process.once('SIGTERM', async () => {
        process.stdout.write(JSON.stringify({ stats: server.stats }) + '\n');
        await server.shutdown();
        process.exit(0);
      });
    } catch (_) {
      process.stderr.write('bridge_invalid_config\n');
      process.exit(2);
    }
  });
}
