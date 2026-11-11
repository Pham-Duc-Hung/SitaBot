import test from "node:test";
import assert from "node:assert/strict";

import { createAppSyncSubscription, mergeActionResult, normalizeActionResult, realtimeEndpoint } from "../frontend/appsync.js";
import { findActionResult, listSessionResults } from "../frontend/chat-api.js";

const graphqlUrl = "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql";
const event = {
  user_id: "user-1", session_id: "ses_one", action_id: "act_one",
  created_at: "2026-11-12T01:00:00Z", service: "s3", tool_name: "s3_create_bucket",
  status: "SUCCESS", message: "Bucket đã tạo", data: '{"bucket_name":"example"}'
};

class FakeSocket {
  static instances = [];
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 0;
    this.sent = [];
    FakeSocket.instances.push(this);
  }
  send(value) { this.sent.push(JSON.parse(value)); }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
  close() { this.readyState = 3; this.onclose?.(); }
}

function testChannel() {
  FakeSocket.instances = [];
  const scheduled = new Map();
  let nextTimer = 1;
  const results = [];
  const states = [];
  const channel = createAppSyncSubscription({
    graphqlUrl, getIdToken: async () => "sample.jwt.token", WebSocketImpl: FakeSocket,
    onResult: result => results.push(result), onState: state => states.push(state),
    setTimer: fn => { const id = nextTimer++; scheduled.set(id, fn); return id; },
    clearTimer: id => scheduled.delete(id), random: () => 0
  });
  const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
  async function connect() {
    await flush();
    const socket = FakeSocket.instances.at(-1);
    socket.open();
    assert.deepEqual(socket.sent[0], { type: "connection_init" });
    socket.receive({ type: "connection_ack", payload: { connectionTimeoutMs: 300000 } });
    const start = socket.sent[1];
    socket.receive({ type: "start_ack", id: start.id });
    return { socket, start };
  }
  return { channel, results, states, scheduled, connect, flush };
}

test("AppSync endpoint accepts only primary-region AWS GraphQL URLs", () => {
  const endpoint = realtimeEndpoint(graphqlUrl);
  assert.equal(endpoint.url.href, "wss://abc123.appsync-realtime-api.ap-southeast-1.amazonaws.com/graphql");
  for (const bad of ["http://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql",
    "https://evil.example/graphql", graphqlUrl + "?token=one", graphqlUrl + "/extra"]) {
    assert.throws(() => realtimeEndpoint(bad), /không hợp lệ/);
  }
});

test("Cognito WebSocket subscribes to one session, decodes result and ignores duplicates or other sessions", async () => {
  const { channel, results, states, connect } = testChannel();
  channel.start("ses_one");
  const { socket, start } = await connect();
  assert.deepEqual(socket.protocols.slice(0, 1), ["graphql-ws"]);
  const header = JSON.parse(Buffer.from(socket.protocols[1].slice(7), "base64url").toString());
  assert.equal(header.host, "abc123.appsync-api.ap-southeast-1.amazonaws.com");
  assert.equal(header.Authorization, "sample.jwt.token");
  assert.equal(start.payload.extensions.authorization.Authorization, "sample.jwt.token");
  assert.deepEqual(JSON.parse(start.payload.data).variables, { sessionId: "ses_one" });
  assert.match(JSON.parse(start.payload.data).query, /onActionResult\(session_id: \$sessionId\)/);
  assert.equal(channel.status, "connected");
  const waiting = channel.waitForResult("act_one");
  socket.receive({ type: "data", id: start.id, payload: { data: { onActionResult: { ...event, session_id: "other" } } } });
  socket.receive({ type: "data", id: start.id, payload: { data: { onActionResult: event } } });
  socket.receive({ type: "data", id: start.id, payload: { data: { onActionResult: event } } });
  assert.equal((await waiting).data.bucket_name, "example");
  assert.equal(results.length, 1);
  assert.deepEqual(states, ["connecting", "connected"]);
  channel.stop();
  assert.equal(socket.sent.at(-1).type, "stop");
});

test("disconnection reconnects and pending action can be delivered after reconnect", async () => {
  const { channel, results, states, scheduled, connect, flush } = testChannel();
  channel.start("ses_one");
  const first = await connect();
  const waiting = channel.waitForResult("act_one");
  first.socket.close();
  assert.equal(channel.status, "reconnecting");
  const retry = [...scheduled.values()].at(-1);
  retry();
  await flush();
  const second = await connect();
  second.socket.receive({ type: "data", id: second.start.id, payload: { data: { onActionResult: event } } });
  assert.equal((await waiting).message, "Bucket đã tạo");
  assert.equal(results.length, 1);
  assert.ok(states.includes("reconnecting"));
  channel.stop();
});

test("missing keep-alive closes stale socket and starts a fresh subscription", async () => {
  const { channel, scheduled, connect, flush } = testChannel();
  channel.start("ses_one");
  const first = await connect();
  const heartbeat = [...scheduled.values()][0];
  heartbeat();
  assert.equal(first.socket.readyState, 3);
  assert.equal(channel.status, "reconnecting");
  [...scheduled.values()][0]();
  await flush();
  const second = await connect();
  assert.notEqual(second.start.id, first.start.id);
  channel.stop();
});

test("switching sessions closes old subscription and drops old events", async () => {
  const { channel, results, connect } = testChannel();
  channel.start("ses_one");
  const first = await connect();
  channel.start("ses_two");
  assert.equal(first.socket.readyState, 3);
  const second = await connect();
  first.socket.receive({ type: "data", id: first.start.id, payload: { data: { onActionResult: event } } });
  second.socket.receive({ type: "data", id: second.start.id, payload: { data: { onActionResult: event } } });
  assert.equal(results.length, 0);
  channel.stop();
});

test("missing authentication never opens a socket", async () => {
  FakeSocket.instances = [];
  const channel = createAppSyncSubscription({ graphqlUrl, getIdToken: async () => null, WebSocketImpl: FakeSocket });
  channel.start("ses_one");
  await Promise.resolve();
  assert.equal(channel.status, "unauthenticated");
  assert.equal(FakeSocket.instances.length, 0);
  channel.stop();
});

test("history reconciles only matching action and session", async () => {
  const fetchImpl = async url => ({ ok: true, status: 200, json: async () => {
    if (url.includes("created_at=")) return { item: { ...event, session_id: "other" } };
    return { items: [event], cursor: null };
  } });
  assert.equal(await findActionResult("act_one", "ses_one", { fetchImpl, getIdToken: async () => null }), null);
  const records = await listSessionResults("ses_one", { fetchImpl, getIdToken: async () => null });
  assert.equal(records.length, 1);
  assert.equal(normalizeActionResult(event).data.bucket_name, "example");
});

test("late event replaces pending chat history without adding a duplicate", () => {
  const session = { messages: [{ role: "assistant", actionId: "act_one", status: "PENDING",
    content: "AWS đang xử lý", data: null }] };
  const result = normalizeActionResult(event);
  assert.equal(mergeActionResult(session, result).content, "Bucket đã tạo");
  assert.equal(session.messages[0].data.bucket_name, "example");
  assert.equal(session.messages[0].status, "SUCCESS");
  assert.equal(mergeActionResult(session, result), null);
  assert.equal(session.messages.length, 1);
  assert.equal(mergeActionResult(session, { ...result, action_id: "other" }), null);
  assert.equal(mergeActionResult(session, { ...result, action_id: "other" }, true).actionId, "other");
});
