/** Cognito-authenticated AppSync GraphQL subscription for one chat session. */

const SUBSCRIPTION = `subscription OnActionResult($sessionId: ID!) {
  onActionResult(session_id: $sessionId) {
    user_id session_id action_id created_at service tool_name status message data
  }
}`;

export function realtimeEndpoint(graphqlUrl) {
  const url = new URL(graphqlUrl);
  if (url.protocol !== "https:" || url.pathname !== "/graphql" || url.search || url.hash ||
      !/^[a-z0-9-]+\.appsync-api\.[a-z0-9-]+\.amazonaws\.com$/.test(url.hostname)) {
    throw new Error("AppSync GraphQL endpoint không hợp lệ.");
  }
  const host = url.hostname;
  url.protocol = "wss:";
  url.hostname = host.replace(".appsync-api.", ".appsync-realtime-api.");
  return { url, host };
}

function encodeHeader(value) {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function normalizeActionResult(value) {
  if (!value || typeof value !== "object" || !value.action_id || !value.session_id ||
      !["SUCCESS", "ERROR", "CONFIRMATION_REQUIRED", "USER_ACTION_REQUIRED"].includes(value.status)) {
    return null;
  }
  let data = value.data;
  if (typeof data === "string") {
    try { data = JSON.parse(data); } catch { data = null; }
  }
  return { ...value, data: data && typeof data === "object" ? data : null };
}

/** Upsert a result in local chat history; one action_id has one assistant message. */
export function mergeActionResult(session, record, appendMissing = false) {
  let message = session.messages.find(item => item.actionId === record.action_id);
  if (!message && !appendMissing) return null;
  if (message && message.status === record.status && message.content === record.message &&
      (!record.data || message.data)) return null;
  if (message) {
    Object.assign(message, { content: record.message, data: record.data || message.data, status: record.status });
  } else {
    message = { role: "assistant", content: record.message, data: record.data || null,
      actionId: record.action_id, status: record.status, createdAt: Date.parse(record.created_at) || Date.now() };
    session.messages.push(message);
  }
  session.updatedAt = Date.now();
  return message;
}

export function createAppSyncSubscription({
  graphqlUrl, getIdToken, onResult = () => {}, onState = () => {},
  WebSocketImpl = WebSocket, setTimer = setTimeout, clearTimer = clearTimeout,
  random = Math.random
}) {
  const endpoint = realtimeEndpoint(graphqlUrl);
  let socket = null;
  let sessionId = null;
  let subscriptionId = null;
  let generation = 0;
  let retry = 0;
  let retryTimer = null;
  let heartbeatTimer = null;
  let phase = "disconnected";
  let connectionTimeoutMs = 300000;
  const seen = new Map();
  const waiters = new Map();

  const emitState = next => {
    if (phase !== next) { phase = next; onState(next); }
  };
  const clearTimers = () => {
    if (retryTimer !== null) clearTimer(retryTimer);
    if (heartbeatTimer !== null) clearTimer(heartbeatTimer);
    retryTimer = heartbeatTimer = null;
  };
  const closeSocket = () => {
    if (heartbeatTimer !== null) clearTimer(heartbeatTimer);
    heartbeatTimer = null;
    if (!socket) return;
    const old = socket;
    socket = null;
    if (old.readyState === 1 && subscriptionId) {
      old.send(JSON.stringify({ type: "stop", id: subscriptionId }));
    }
    old.close();
  };
  const scheduleRetry = () => {
    if (!sessionId || retryTimer !== null) return;
    if (heartbeatTimer !== null) clearTimer(heartbeatTimer);
    heartbeatTimer = null;
    emitState("reconnecting");
    const delay = Math.min(30000, 1000 * 2 ** Math.min(retry++, 5));
    retryTimer = setTimer(() => {
      retryTimer = null;
      void connect();
    }, Math.round(delay * (0.5 + random() * 0.5)));
  };
  const armHeartbeat = timeoutMs => {
    if (heartbeatTimer !== null) clearTimer(heartbeatTimer);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return;
    heartbeatTimer = setTimer(() => { closeSocket(); scheduleRetry(); }, timeoutMs);
  };

  async function connect() {
    if (!sessionId || socket) return;
    const current = ++generation;
    emitState(retry ? "reconnecting" : "connecting");
    let token;
    try { token = await getIdToken(); } catch { token = null; }
    if (current !== generation || !sessionId) return;
    if (!token) { emitState("unauthenticated"); return; }
    const auth = { host: endpoint.host, Authorization: token };
    let ws;
    try {
      ws = new WebSocketImpl(endpoint.url.href, ["graphql-ws", `header-${encodeHeader(auth)}`]);
    } catch {
      scheduleRetry();
      return;
    }
    socket = ws;
    const activeSession = sessionId;
    const id = globalThis.crypto?.randomUUID?.() || `sub_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    subscriptionId = id;
    ws.onopen = () => {
      if (ws !== socket) return;
      ws.send(JSON.stringify({ type: "connection_init" }));
    };
    ws.onmessage = message => {
      if (ws !== socket) return;
      let frame;
      try { frame = JSON.parse(message.data); } catch { return; }
      if (frame.type === "connection_ack") {
        connectionTimeoutMs = frame.payload?.connectionTimeoutMs || 300000;
        armHeartbeat(connectionTimeoutMs);
        ws.send(JSON.stringify({
          id, type: "start",
          payload: {
            data: JSON.stringify({ query: SUBSCRIPTION, variables: { sessionId: activeSession } }),
            extensions: { authorization: auth }
          }
        }));
      } else if (frame.type === "start_ack" && frame.id === id) {
        retry = 0;
        emitState("connected");
      } else if (frame.type === "ka") {
        armHeartbeat(connectionTimeoutMs);
      } else if (frame.type === "data" && frame.id === id) {
        const result = normalizeActionResult(frame.payload?.data?.onActionResult);
        if (!result || result.session_id !== activeSession) return;
        if (seen.has(result.action_id)) return;
        seen.set(result.action_id, result);
        if (seen.size > 100) seen.delete(seen.keys().next().value);
        const waiting = waiters.get(result.action_id);
        if (waiting) {
          waiters.delete(result.action_id);
          waiting(result);
        }
        onResult(result);
      } else if (frame.type === "error" || frame.type === "connection_error") {
        closeSocket();
        scheduleRetry();
      }
    };
    ws.onerror = () => { if (ws === socket) { closeSocket(); scheduleRetry(); } };
    ws.onclose = () => { if (ws === socket) { socket = null; scheduleRetry(); } };
  }

  function start(nextSessionId) {
    if (nextSessionId === sessionId && socket) return;
    generation++;
    clearTimers();
    closeSocket();
    for (const done of waiters.values()) done(null);
    waiters.clear();
    seen.clear();
    sessionId = nextSessionId || null;
    retry = 0;
    if (sessionId) void connect();
    else emitState("disconnected");
  }

  function waitForResult(actionId, timeoutMs = 30000) {
    if (seen.has(actionId)) return Promise.resolve(seen.get(actionId));
    if (phase !== "connected") return Promise.resolve(null);
    return new Promise(resolve => {
      const timer = setTimer(() => {
        waiters.delete(actionId);
        resolve(null);
      }, timeoutMs);
      waiters.set(actionId, result => { clearTimer(timer); resolve(result); });
    });
  }

  function stop() {
    generation++;
    sessionId = null;
    clearTimers();
    closeSocket();
    for (const done of waiters.values()) done(null);
    waiters.clear();
    seen.clear();
    emitState("disconnected");
  }

  return { start, stop, waitForResult, get status() { return phase; } };
}
