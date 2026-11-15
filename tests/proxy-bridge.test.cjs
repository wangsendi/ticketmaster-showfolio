"use strict";
const assert = require("node:assert/strict");
const net = require("node:net");
const { once } = require("node:events");
const { test } = require("node:test");
const { createBridge } = require("../src/proxy-bridge.cjs");

async function read(socket, length) {
  while (true) {
    const data = socket.read(length);
    if (data) return data;
    if (socket.destroyed || socket.readableEnded) throw new Error("Socket closed");
    await once(socket, "readable");
  }
}

async function fixture(t, rejectAuth = false, options = {}) {
  const seen = { users: [], hosts: [], forwarded: [], errors: [] };
  const sockets = new Set();
  const upstream = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("error", () => {});
    (async () => {
      assert.deepEqual(await read(socket, 3), Buffer.from([5, 1, 2]));
      // Deliberately fragment a reply to exercise stream framing.
      socket.write(Buffer.from([5]));
      setImmediate(() => socket.write(Buffer.from([2])));
      const auth = await read(socket, 2);
      assert.equal(auth[0], 1);
      seen.users.push((await read(socket, auth[1])).toString());
      const passwordLength = (await read(socket, 1))[0];
      assert.equal((await read(socket, passwordLength)).toString(), "shared-secret");
      socket.write(Buffer.from([1, rejectAuth ? 1 : 0]));
      if (rejectAuth) return;
      const request = await read(socket, 5);
      assert.deepEqual(request.subarray(0, 4), Buffer.from([5, 1, 0, 3]));
      seen.hosts.push((await read(socket, request[4])).toString());
      assert.equal((await read(socket, 2)).readUInt16BE(), options.port || 443);
      socket.write(Buffer.from([5, 0, 0, 1, 127, 0, 0, 1, 0, 80]));
      socket.on("data", (data) => { seen.forwarded.push(data.toString()); socket.write(data); });
    })().catch((error) => { seen.errors.push(error); socket.destroy(); });
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  const bridge = createBridge({ socksHost: "127.0.0.1", socksPort: upstream.address().port,
                                username: "node-route", password: "shared-secret", maxAttempts: 1, allowHttp:!!options.allowHttp });
  bridge.listen(0, "127.0.0.1");
  await once(bridge, "listening");
  t.after(async () => {
    await bridge.shutdown();
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => upstream.close(resolve));
    assert.deepEqual(seen.errors, []);
  });
  return { bridge, seen };
}

test("CONNECT authenticates to the selected node, uses remote DNS and forwards application data", { timeout: 5000 }, async (t) => {
  const { bridge, seen } = await fixture(t);
  for (let i = 0; i < 2; i++) {
    const client = net.createConnection({ host: "127.0.0.1", port: bridge.address().port });
    await once(client, "connect");
    client.write("CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\nhello");
    const header = "HTTP/1.1 200 Connection established\r\n\r\n";
    assert.equal((await read(client, Buffer.byteLength(header))).toString(), header);
    assert.equal((await read(client, 5)).toString(), "hello");
    client.destroy();
  }
  assert.deepEqual(seen.users, ["node-route", "node-route"]);
  assert.deepEqual(seen.hosts, ["example.invalid", "example.invalid"]);
  assert.deepEqual(seen.forwarded, ["hello", "hello"]);
  assert.equal(bridge.stats.forwardedTunnels, 2);
});

test("SOCKS auth failure returns 502 without forwarding or retrying", { timeout: 5000 }, async (t) => {
  const { bridge, seen } = await fixture(t, true);
  const client = net.createConnection({ host: "127.0.0.1", port: bridge.address().port });
  await once(client, "connect");
  client.write("CONNECT example.invalid:443 HTTP/1.1\r\n\r\naccount-data");
  const reply = "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n";
  assert.equal((await read(client, Buffer.byteLength(reply))).toString(), reply);
  client.destroy();
  assert.deepEqual(seen.forwarded, []);
  assert.deepEqual(seen.hosts, []);
  assert.equal(bridge.stats.upstreamConnectAttempts, 1);
});

test("invalid requests cannot turn the bridge into a direct connection", { timeout: 5000 }, async (t) => {
  const { bridge, seen } = await fixture(t);
  const client = net.createConnection({ host: "127.0.0.1", port: bridge.address().port });
  await once(client, "connect");
  client.write("GET http://example.invalid/ HTTP/1.1\r\n\r\n");
  const reply = "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n";
  assert.equal((await read(client, Buffer.byteLength(reply))).toString(), reply);
  client.destroy();
  assert.deepEqual(seen.users, []);
  assert.equal(bridge.stats.upstreamConnectAttempts, 0);
});


test("optional HTTP support forwards origin-form requests through SOCKS without proxy credentials", { timeout: 5000 }, async (t) => {
  const {bridge,seen} = await fixture(t,false,{port:8080,allowHttp:true});
  const client = net.createConnection({host:"127.0.0.1",port:bridge.address().port});
  await once(client,"connect");
  client.write("POST http://example.invalid:8080/path?q=1 HTTP/1.1\r\nHost: wrong.invalid\r\nProxy-Authorization: Basic secret\r\nProxy-Connection: keep-alive\r\nContent-Length: 4\r\n\r\nbody");
  const expected="POST /path?q=1 HTTP/1.1\r\nHost: example.invalid:8080\r\nConnection: close\r\nContent-Length: 4\r\n\r\nbody";
  assert.equal((await read(client,Buffer.byteLength(expected))).toString(),expected);
  client.destroy();
  assert.deepEqual(seen.hosts,["example.invalid"]);
  assert.equal(bridge.stats.forwardedTunnels,1);
});
