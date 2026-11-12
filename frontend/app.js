/**
 * Author: Trịnh Thiên Quang
 * Codex & ChatGPT-style Agent UI with interactive tool log steps & typewriter text stream.
 */
import { chatStorageKey } from "./auth.js";
import { chatRequest, confirmationPayload, findActionResult, listSessionResults, pollActionResult, resolveQueuedAction } from "./chat-api.js";
import { createAppSyncSubscription, mergeActionResult } from "./appsync.js";

"use strict";

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const VALID_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash"];
const initialModel = localStorage.getItem("satibot.model");

const state = {
  history: [],
  pending: null,
  isBusy: false,
  apiAvailable: true,
  controller: null,
  autoApproval: true,
  selectedModel: VALID_MODELS.includes(initialModel) ? initialModel : "gemini-3.5-flash-lite",
  sessions: [],
  currentSessionId: null,
  realtime: null
};

let sessionStorageKey = null;
let sessionOwner = null;

const els = {
  sidebar: $("#sidebar"),
  sidebarToggleBtn: $("#sidebarToggleBtn"),
  lightningBtn: $("#lightningBtn"),
  newChatBtn: $("#newChatBtn"),
  recentList: $("#recentList"),
  statusBadge: $("#statusBadge"),
  realtimeStatus: $("#realtimeStatus"),
  awsStatusPill: $("#awsStatusPill"),
  toolsPill: $("#toolsPill"),
  chatScroll: $("#chatScroll"),
  emptyView: $("#emptyView"),
  conversationFeed: $("#conversationFeed"),
  promptInput: $("#promptInput"),
  sendBtn: $("#sendBtn"),
  approvalToggle: $("#approvalToggle"),
  modelPickerContainer: $("#modelPickerContainer"),
  modelPickerBtn: $("#modelPickerBtn"),
  currentModelLabel: $("#currentModelLabel"),
  modelDropdownMenu: $("#modelDropdownMenu"),
  topModelText: $("#topModelText")
};

function str(v) {
  return v == null ? "" : String(v);
}

function cleanUrl(raw) {
  if (!raw) return "";
  let u = String(raw).trim();
  u = u.replace(/[,\)]+$/, "");
  return u;
}

function extractPublicUrl(text, data) {
  if (data && data.function_url && typeof data.function_url === "string") {
    return cleanUrl(data.function_url);
  }
  if (!text) return null;
  const match = text.match(/https:\/\/[A-Za-z0-9\-._~:/?#@!$&'*+,;=]+/);
  if (match) {
    const candidate = cleanUrl(match[0]);
    if (candidate.includes("lambda-url") || candidate.includes(".on.aws")) {
      return candidate;
    }
  }
  return null;
}

function appendS3DownloadLink(container, data) {
  if (!data || typeof data.url !== "string" || !data.object_key) return;
  try {
    const url = new URL(data.url);
    if (url.protocol !== "https:" || !url.hostname.endsWith(".amazonaws.com")) return;
    const link = document.createElement("a");
    link.className = "restored-url-link";
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `Tải tệp S3: ${data.object_key} (liên kết có thời hạn) ↗`;
    container.append(link);
  } catch {
    // Ignore malformed URLs returned by a backend or restored session.
  }
}

function formatBody(text) {
  if (!text) return "";
  // Models occasionally flatten a Markdown list into one long line. Restore the
  // structural breaks before escaping/rendering it.
  const normalized = String(text)
    .replace(/\r\n?/g, "\n")
    .replace(/\s+(?=(?:\d+\.)\s+[A-ZÀ-Ỹ*`])/g, "\n\n")
    .replace(/([:.])\s+-\s+(?=[A-ZÀ-Ỹ*`])/g, "$1\n- ")
    .replace(/\.\s+-\s+(?=[A-ZÀ-Ỹ*`])/g, ".\n- ");

  let safe = normalized
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  const inline = value => {
    let result = value;

    // Markdown links: [text](url)
    result = result.replace(/\[([^\]]+)\]\((https?:\/\/[^\s\)]+)\)/g, (m, txt, url) => {
      const clean = cleanUrl(url);
      return `<a href="${clean}" target="_blank" rel="noopener noreferrer">${txt} ↗</a>`;
    });

    // Raw URLs
    result = result.replace(/(^|[^"'>])(https?:\/\/[A-Za-z0-9\-._~:/?#@!$&'*+,;=]+)/g, (m, prefix, url) => {
      const clean = cleanUrl(url);
      return `${prefix}<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean} ↗</a>`;
    });

    result = result.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    result = result.replace(/`([^`]+)`/g, '<code>$1</code>');
    return result;
  };

  const lines = safe.split("\n");
  const html = [];
  let listType = null;
  const closeList = () => {
    if (listType) html.push(`</${listType}>`);
    listType = null;
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      closeList();
      continue;
    }
    const ordered = line.match(/^(\d+)\.\s+(.+)$/);
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (ordered || bullet) {
      const wanted = ordered ? "ol" : "ul";
      if (listType !== wanted) {
        closeList();
        html.push(`<${wanted}>`);
        listType = wanted;
      }
      const content = ordered ? ordered[2] : bullet[1];
      const valueAttr = ordered ? ` value="${ordered[1]}"` : "";
      html.push(`<li${valueAttr}>${inline(content)}</li>`);
      continue;
    }
    closeList();
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length + 2;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
    } else {
      html.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return html.join("");
}

/**
 * Smooth typewriter streaming animation for assistant responses.
 */
function streamText(targetEl, text, onDone) {
  if (!text) {
    if (onDone) onDone();
    return;
  }

  const tokens = text.match(/\S+|\s+/g) || [text];
  let i = 0;
  let streamedText = "";
  targetEl.innerHTML = "";

  const textSpan = document.createElement("div");
  textSpan.className = "streaming-body";
  const cursor = document.createElement("span");
  cursor.className = "streaming-cursor";
  targetEl.append(textSpan);

  function renderStream() {
    // Use the exact same formatter during animation and after completion. This
    // prevents the old plain-text phase followed by a sudden layout jump.
    textSpan.innerHTML = formatBody(streamedText);
    const tails = textSpan.querySelectorAll("li:last-child, p:last-child, h3:last-child, h4:last-child, h5:last-child");
    const tail = tails.length ? tails[tails.length - 1] : textSpan;
    tail.append(cursor);
  }

  let finished = false;
  function finish() {
    if (finished) return;
    finished = true;
    clearInterval(timer);
    cursor.remove();
    targetEl.innerHTML = formatBody(text);
    if (onDone) onDone();
  }

  targetEl.style.cursor = "pointer";
  targetEl.title = "Click để hiện ngay toàn bộ nội dung";
  targetEl.onclick = finish;

  const timer = setInterval(() => {
    if (i < tokens.length) {
      const chunk = tokens.slice(i, i + 2).join("");
      streamedText += chunk;
      i += 2;
      renderStream();
      els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
    } else {
      finish();
    }
  }, 16);
}

/**
 * Creates an interactive collapsible log item (Image 2).
 */
function createLogItem(actionTitle, detailData, prefix = "Ran ") {
  const item = document.createElement("div");
  item.className = "log-item";

  const header = document.createElement("div");
  header.className = "log-header";

  const title = document.createElement("span");
  title.className = "log-title";
  title.textContent = `${prefix}${actionTitle}`;

  const chevron = document.createElement("span");
  chevron.className = "log-chevron";
  chevron.textContent = "›";

  header.append(title, chevron);

  const detail = document.createElement("div");
  detail.className = "log-detail hidden";
  detail.textContent = typeof detailData === "string" ? detailData : JSON.stringify(detailData, null, 2);

  header.onclick = () => {
    const isClosed = detail.classList.contains("hidden");
    if (isClosed) {
      detail.classList.remove("hidden");
      item.classList.add("open");
    } else {
      detail.classList.add("hidden");
      item.classList.remove("open");
    }
  };

  item.append(header, detail);
  return item;
}

function loadStoredSessions() {
  if (!sessionStorageKey) { state.sessions = []; return; }
  try {
    const parsed = JSON.parse(localStorage.getItem(sessionStorageKey) || "[]");
    state.sessions = Array.isArray(parsed) ? parsed.filter(s => s && s.id && Array.isArray(s.messages)) : [];
  } catch {
    state.sessions = [];
  }
}

function saveSessions() {
  if (!sessionStorageKey) return;
  state.sessions.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  state.sessions = state.sessions.slice(0, 30);
  try {
    localStorage.setItem(sessionStorageKey, JSON.stringify(state.sessions));
  } catch {
    // Browser storage is usually ~5 MB. Prefer retaining recent conversations
    // instead of letting a very large model response break the chat UI.
    state.sessions = state.sessions.slice(0, Math.max(1, Math.floor(state.sessions.length / 2)));
    try { localStorage.setItem(sessionStorageKey, JSON.stringify(state.sessions)); } catch { /* keep UI usable */ }
  }
  renderRecentSessions();
}

function currentSession() {
  return state.sessions.find(s => s.id === state.currentSessionId) || null;
}

function ensureSession(firstPrompt) {
  let session = currentSession();
  if (session) {
    state.realtime?.start(session.id);
    return session;
  }
  const id = globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : `chat_${Date.now()}_${Math.random().toString(16).slice(2)}`;
  session = {
    id,
    title: firstPrompt.length > 42 ? `${firstPrompt.slice(0, 39)}…` : firstPrompt,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: []
  };
  state.sessions.unshift(session);
  state.currentSessionId = id;
  saveSessions();
  state.realtime?.start(id);
  return session;
}

function storeMessage(role, content, data = null, actionId = null, status = null) {
  const session = currentSession();
  if (!session || !content) return;
  const existing = actionId && session.messages.find(message => message.actionId === actionId);
  if (existing) {
    if (!(existing.status !== "PENDING" && status === "PENDING")) {
      Object.assign(existing, { content: String(content), data, status });
    }
  } else {
    session.messages.push({ role, content: String(content), data, actionId, status, createdAt: Date.now() });
  }
  session.updatedAt = Date.now();
  saveSessions();
  return existing || session.messages.at(-1);
}

function appendStoredMessage(message) {
  if (message.role === "user") {
    const row = document.createElement("div");
    row.className = "user-row";
    const bubble = document.createElement("div");
    bubble.className = "user-bubble";
    bubble.textContent = message.content;
    row.append(bubble);
    els.conversationFeed.append(row);
    return;
  }
  const row = document.createElement("div");
  row.className = "agent-row";
  if (message.actionId) row.dataset.actionId = message.actionId;
  const header = document.createElement("div");
  header.className = "agent-header";
  header.innerHTML = '<div class="agent-avatar">S</div><span>SatiBot AWS Agent</span>';
  const body = document.createElement("div");
  body.className = "agent-text";
  body.innerHTML = formatBody(message.content);
  row.append(header, body);
  const publicUrl = extractPublicUrl(message.content, message.data);
  if (publicUrl) {
    const link = document.createElement("a");
    link.className = "restored-url-link";
    link.href = publicUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `Mở website ↗  ${publicUrl}`;
    row.append(link);
  }
  appendS3DownloadLink(row, message.data);
  els.conversationFeed.append(row);
}

function openSession(sessionId) {
  if (state.isBusy) return;
  const session = state.sessions.find(s => s.id === sessionId);
  if (!session) return;
  state.currentSessionId = session.id;
  state.realtime?.start(session.id);
  state.pending = null;
  state.history = session.messages
    .filter(m => m.role === "user" || m.role === "assistant")
    .map(m => ({ role: m.role, content: m.content }))
    .slice(-10);
  els.conversationFeed.innerHTML = "";
  session.messages.forEach(appendStoredMessage);
  els.emptyView.classList.toggle("hidden", session.messages.length > 0);
  els.conversationFeed.classList.toggle("hidden", session.messages.length === 0);
  renderRecentSessions();
  void syncSessionHistory(session.id);
  requestAnimationFrame(() => { els.chatScroll.scrollTop = els.chatScroll.scrollHeight; });
}

function updateActionResult(record, appendMissing = false) {
  const session = state.sessions.find(item => item.id === record.session_id);
  if (!session || !record.action_id || !record.message) return;
  const message = mergeActionResult(session, record, appendMissing && !state.isBusy);
  if (!message) return;
  saveSessions();
  if (state.currentSessionId !== session.id) return;
  const row = [...els.conversationFeed.querySelectorAll(".agent-row")]
    .find(item => item.dataset.actionId === record.action_id);
  if (row) {
    const body = row.querySelector(".agent-text");
    if (body) body.innerHTML = formatBody(message.content);
    row.querySelectorAll(".restored-url-link").forEach(link => link.remove());
    appendS3DownloadLink(row, message.data);
  } else {
    appendStoredMessage(message);
  }
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
}

async function syncSessionHistory(sessionId) {
  const owner = sessionOwner;
  try {
    const records = await listSessionResults(sessionId);
    if (sessionOwner !== owner || state.currentSessionId !== sessionId) return;
    for (const record of records.reverse()) {
      const existing = currentSession()?.messages.find(message => message.actionId === record.action_id);
      if (existing?.status === "PENDING") {
        let detail = null;
        try { detail = await findActionResult(record.action_id, sessionId); } catch { /* use summary */ }
        if (sessionOwner !== owner || state.currentSessionId !== sessionId) return;
        updateActionResult(detail || record);
      } else if (!existing) {
        let detail = null;
        try { detail = await findActionResult(record.action_id, sessionId); } catch { /* use summary */ }
        if (sessionOwner !== owner || state.currentSessionId !== sessionId) return;
        updateActionResult(detail || record, true);
      }
    }
  } catch {
    // History is optional for the local demo and retried on the next connection.
  }
}

async function waitForActionResult(actionId, sessionId) {
  if (state.realtime?.status === "connected") {
    const event = await state.realtime.waitForResult(actionId);
    if (event) {
      try { return await findActionResult(actionId, sessionId) || event; } catch { return event; }
    }
    // A lost event can still be recovered from the authoritative history.
    return findActionResult(actionId, sessionId);
  }
  return pollActionResult(actionId, sessionId);
}

async function resolveAction(result) {
  return resolveQueuedAction(result, { poll: waitForActionResult });
}

function setRealtimeState(phase) {
  if (!els.realtimeStatus) return;
  els.realtimeStatus.textContent = {
    connected: "Kết quả: trực tiếp",
    connecting: "Kết quả: đang kết nối",
    reconnecting: "Kết quả: đang kết nối lại",
    unauthenticated: "Kết quả: cần đăng nhập",
    disconnected: "Kết quả: lịch sử"
  }[phase] || "Kết quả: lịch sử";
  if (phase === "connected" && state.currentSessionId) void syncSessionHistory(state.currentSessionId);
}

function renderRecentSessions() {
  els.recentList.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "empty-recent";
    empty.textContent = "Chưa có đoạn chat nào gần đây";
    els.recentList.append(empty);
    return;
  }
  state.sessions.forEach(session => {
    const item = document.createElement("button");
    item.className = "recent-item";
    item.classList.toggle("active", session.id === state.currentSessionId);
    item.textContent = session.title || "Đoạn chat không tiêu đề";
    item.title = session.title || "Đoạn chat không tiêu đề";
    item.onclick = () => openSession(session.id);
    els.recentList.append(item);
  });
}

function setBusy(val) {
  state.isBusy = val;
  els.sendBtn.disabled = val || !state.apiAvailable;
  els.promptInput.disabled = val || !state.apiAvailable;
  if (els.modelPickerBtn) els.modelPickerBtn.disabled = val;
  if (val) {
    els.sendBtn.classList.remove("active");
    toggleModelMenu(false);
  } else {
    updateSendBtnState();
  }
}

function updateSendBtnState() {
  const hasText = els.promptInput.value.trim().length > 0;
  els.sendBtn.disabled = !hasText || state.isBusy || !state.apiAvailable;
  els.sendBtn.classList.toggle("active", hasText && !state.isBusy && state.apiAvailable);
}

async function fetchChat(payload) {
  state.controller = new AbortController();
  const timeoutMs = 210000;
  const timeout = setTimeout(() => state.controller.abort(), timeoutMs);

  try {
    const data = await chatRequest(
      { ...payload, workspace: currentSession()?.workspace || {} },
      { sessionId: currentSession()?.id, signal: state.controller.signal }
    );

    if (data.workspace && currentSession()) {
      currentSession().workspace = data.workspace;
      saveSessions();
    }
    return data;
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("Quá thời gian chờ phản hồi (3.5 phút). Vui lòng thử lại.");
    }
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

async function handleSendMessage() {
  const text = els.promptInput.value.trim();
  if (!text || state.isBusy || !state.apiAvailable) return;

  ensureSession(text);
  storeMessage("user", text);

  // Clear input
  els.promptInput.value = "";
  els.promptInput.style.height = "auto";
  setBusy(true);

  // Switch from empty view to conversation feed
  els.emptyView.classList.add("hidden");
  els.conversationFeed.classList.remove("hidden");

  // 1. Append User Message Bubble
  const userRow = document.createElement("div");
  userRow.className = "user-row";
  const userBubble = document.createElement("div");
  userBubble.className = "user-bubble";
  userBubble.textContent = text;
  userRow.append(userBubble);
  els.conversationFeed.append(userRow);

  // 2. Append Agent Response Row with Logs Container (Image 2)
  const agentRow = document.createElement("div");
  agentRow.className = "agent-row";

  const agentHeader = document.createElement("div");
  agentHeader.className = "agent-header";
  agentHeader.innerHTML = `
    <div class="agent-avatar">S</div>
    <span>SatiBot AWS Agent</span>
  `;

  const logsBox = document.createElement("div");
  logsBox.className = "agent-logs";

  // Initial working step
  const workingStep = document.createElement("div");
  workingStep.className = "log-working";
  workingStep.innerHTML = `<span class="working-pulse">Working</span>`;
  logsBox.append(workingStep);

  const textContainer = document.createElement("div");
  textContainer.className = "agent-text";

  agentRow.append(agentHeader, logsBox, textContainer);
  els.conversationFeed.append(agentRow);
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;

  // Honest live telemetry: this is the real in-flight operation. Tool steps are
  // rendered only when the backend confirms that they actually ran.
  const requestStartedAt = performance.now();
  const requestLog = createLogItem(
    "Agent is processing your request (0s)",
    { model: state.selectedModel, status: "request_in_flight" },
    ""
  );
  logsBox.insertBefore(requestLog, workingStep);
  const progressTimer = setInterval(() => {
    const elapsedSeconds = Math.floor((performance.now() - requestStartedAt) / 1000);
    const title = requestLog.querySelector(".log-title");
    if (title) title.textContent = `Agent is processing your request (${elapsedSeconds}s)`;
  }, 1000);

  try {
    const owner = sessionOwner;
    const res = await resolveAction(await fetchChat({
      message: text, history: state.history, model: state.selectedModel
    }));
    clearInterval(progressTimer);
    if (sessionOwner !== owner) return;

    const elapsedMs = Math.round(performance.now() - requestStartedAt);
    const requestTitle = requestLog.querySelector(".log-title");
    const requestDetail = requestLog.querySelector(".log-detail");
    if (requestTitle) requestTitle.textContent = `Response ready in ${(elapsedMs / 1000).toFixed(1)}s`;
    if (requestDetail) requestDetail.textContent = JSON.stringify({
      model: state.selectedModel,
      elapsed_ms: elapsedMs,
      status: res.status || "SUCCESS"
    }, null, 2);

    // If real steps returned from backend, replace or append them
    if (res.steps && res.steps.length > 0) {
      res.steps.forEach(s => {
        const item = createLogItem(s.action, s.parameters || s.observation || {});
        logsBox.insertBefore(item, workingStep);
      });
    }

    // Stop working spinner
    workingStep.remove();

    // Check if confirmation is required
    if (res.status === "CONFIRMATION_REQUIRED") {
      storeMessage("assistant", res.message || "SatiBot đang chờ bạn phê duyệt thao tác AWS.");
      state.history.push(
        { role: "user", content: text },
        { role: "assistant", content: res.message || "SatiBot đang chờ bạn phê duyệt thao tác AWS." }
      );
      state.history = state.history.slice(-10);
      handleConfirmation(res, agentRow, text);
      return;
    }

    state.history.push({ role: "user", content: text }, { role: "assistant", content: res.message || "" });
    state.history = state.history.slice(-10);
    if (res.action_id) agentRow.dataset.actionId = res.action_id;
    const saved = storeMessage("assistant", res.message || (res.status === "SUCCESS" ? "Đã hoàn thành nhiệm vụ." : "Chưa có kết quả cuối cùng."), res.data || null,
      res.action_id || null, res.status === "PENDING" ? "PENDING" : res.status);

    // Stream the final response text (Typewriter animation)
    streamText(textContainer, saved?.content || res.message || "Chưa có kết quả cuối cùng.", () => {
      appendS3DownloadLink(agentRow, saved?.data || res.data);
      // Check for public URL
      const publicUrl = extractPublicUrl(res.message, res.data);
      if (publicUrl) {
        const urlCard = document.createElement("div");
        urlCard.className = "public-url-card";
        urlCard.innerHTML = `
          <div class="url-meta">
            <span class="url-label">WEBSITE ĐÃ TRIỂN KHAI THÀNH CÔNG</span>
            <a href="${publicUrl}" target="_blank" class="url-address">${publicUrl}</a>
          </div>
          <a href="${publicUrl}" target="_blank" rel="noopener noreferrer" class="url-btn">
            Mở Website ↗
          </a>
        `;
        agentRow.append(urlCard);
        els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
      }
    });

    setBusy(false);
  } catch (err) {
    clearInterval(progressTimer);
    const elapsedMs = Math.round(performance.now() - requestStartedAt);
    const requestTitle = requestLog.querySelector(".log-title");
    if (requestTitle) requestTitle.textContent = `Request failed after ${(elapsedMs / 1000).toFixed(1)}s`;
    workingStep.remove();
    textContainer.innerHTML = `<span style="color:#ef4444;font-weight:600;">Lỗi: ${err.message}</span>`;
    storeMessage("assistant", `Lỗi: ${err.message}`);
    setBusy(false);
  }
}

function handleConfirmation(res, agentRow, originalPrompt) {
  // Async IAM results carry the new confirmation action inside data.
  const confirmation = res.data?.preview ? res.data : res;
  state.pending = {
    // The local demo also emits an action_id, but only the cloud API persists it.
    action_id: (res.session_id || confirmation.session_id) ?
      (confirmation.action_id || res.action_id) : undefined,
    service: confirmation.service || res.service || "lambda",
    tool_name: confirmation.tool_name || res.tool_name,
    parameters: confirmation.parameters || res.parameters || {},
    confirmation_mode: confirmation.confirmation_mode || res.confirmation_mode ||
      (confirmation.tool_name === "s3_delete_bucket_safe" ? "explicit_phrase" : "standard"),
    required_confirmation: confirmation.required_confirmation || res.required_confirmation ||
      (confirmation.tool_name === "s3_delete_bucket_safe" ? `DELETE ${confirmation.parameters?.bucket_name || ""}` : null),
    agent_state: res.agent_state,
    model: state.selectedModel,
    originalPrompt: originalPrompt
  };

  const card = document.createElement("div");
  card.className = "approval-card";
  card.innerHTML = `
    <div class="approval-title">⚠️ Yêu cầu phê duyệt thao tác AWS</div>
    <div class="approval-msg"></div>
    <div class="approval-buttons">
      <button class="btn-approve" id="confirmApprove">Xác nhận thực hiện</button>
      <button class="btn-reject" id="confirmReject">Hủy bỏ</button>
    </div>
  `;
  card.querySelector(".approval-msg").textContent = res.message || "Xem tác động trước khi xác nhận thay đổi AWS.";
  if (confirmation.preview) {
    const details = document.createElement("pre");
    details.style.whiteSpace = "pre-wrap";
    details.style.overflowWrap = "anywhere";
    details.textContent = JSON.stringify({
      parameters: confirmation.parameters,
      preview: confirmation.preview,
      warnings: confirmation.warnings || res.warnings || []
    }, null, 2);
    card.querySelector(".approval-buttons").before(details);
  }
  let phraseInput = null;
  const approveButton = card.querySelector("#confirmApprove");
  if (state.pending.confirmation_mode === "explicit_phrase") {
    const label = document.createElement("label");
    label.className = "approval-phrase";
    label.textContent = `Nhập chính xác ${state.pending.required_confirmation} để xác nhận xóa: `;
    phraseInput = document.createElement("input");
    phraseInput.type = "text";
    phraseInput.autocomplete = "off";
    phraseInput.spellcheck = false;
    phraseInput.setAttribute("aria-label", "Câu xác nhận xóa bucket");
    phraseInput.addEventListener("input", () => {
      approveButton.disabled = phraseInput.value !== state.pending?.required_confirmation;
    });
    approveButton.disabled = true;
    label.append(phraseInput);
    card.querySelector(".approval-buttons").before(label);
  }

  agentRow.append(card);
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;

  approveButton.onclick = async () => {
    const approvedAction = {
      ...state.pending,
      parameters: { ...(state.pending?.parameters || {}) }
    };
    let payload;
    try {
      payload = confirmationPayload(approvedAction, phraseInput?.value || "");
    } catch (error) {
      card.querySelector(".approval-msg").textContent = error.message;
      return;
    }
    card.remove();
    setBusy(true);

    const logsBox = agentRow.querySelector(".agent-logs");
    const workingStep = document.createElement("div");
    workingStep.className = "log-working";
    workingStep.innerHTML = `<span class="working-pulse">Working • Executing approved action</span>`;
    logsBox.append(workingStep);

    try {
      const owner = sessionOwner;
      const execRes = await resolveAction(await fetchChat(payload));
      if (sessionOwner !== owner) return;

      workingStep.remove();

      if (execRes.steps && execRes.steps.length > 0) {
        execRes.steps.forEach(s => {
          const item = createLogItem(s.action, s.parameters || s.observation || {});
          logsBox.append(item);
        });
      }

      if (execRes.status === "CONFIRMATION_REQUIRED") {
        state.pending = null;
        storeMessage("assistant", execRes.message || "SatiBot đang chờ bạn phê duyệt bước tiếp theo.");
        handleConfirmation(execRes, agentRow, originalPrompt);
        return;
      }

      if (execRes.status === "USER_ACTION_REQUIRED") {
        state.pending = approvedAction;
        storeMessage("assistant", execRes.message || "Cần bạn chọn cách xử lý.", execRes.data || null);
        handleResolutionOptions(execRes, agentRow, originalPrompt, approvedAction);
        setBusy(false);
        return;
      }

      state.pending = null;

      const textContainer = agentRow.querySelector(".agent-text");
      const finalMessage = execRes.message || (execRes.status === "SUCCESS" ? "Đã thực thi thành công." : "Chưa có kết quả cuối cùng.");
      if (execRes.action_id) agentRow.dataset.actionId = execRes.action_id;
      const saved = storeMessage("assistant", finalMessage, execRes.data || null,
        execRes.action_id || null, execRes.status === "PENDING" ? "PENDING" : execRes.status);
      state.history.push({ role: "assistant", content: finalMessage });
      state.history = state.history.slice(-10);
      streamText(textContainer, saved?.content || finalMessage, () => {
        appendS3DownloadLink(agentRow, saved?.data || execRes.data);
        const publicUrl = extractPublicUrl(execRes.message, execRes.data);
        if (publicUrl) {
          const urlCard = document.createElement("div");
          urlCard.className = "public-url-card";
          urlCard.innerHTML = `
            <div class="url-meta">
              <span class="url-label">WEBSITE ĐÃ TRIỂN KHAI THÀNH CÔNG</span>
              <a href="${publicUrl}" target="_blank" class="url-address">${publicUrl}</a>
            </div>
            <a href="${publicUrl}" target="_blank" rel="noopener noreferrer" class="url-btn">
              Mở Website ↗
            </a>
          `;
          agentRow.append(urlCard);
          els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
        }
      });

      setBusy(false);
    } catch (err) {
      workingStep.remove();
      state.pending = null;
      const textContainer = agentRow.querySelector(".agent-text");
      textContainer.innerHTML = `<span style="color:#ef4444;font-weight:600;">Lỗi thực thi: ${err.message}</span>`;
      storeMessage("assistant", `Lỗi thực thi: ${err.message}`);
      setBusy(false);
    }
  };

  card.querySelector("#confirmReject").onclick = () => {
    card.remove();
    state.pending = null;
    const textContainer = agentRow.querySelector(".agent-text");
    textContainer.textContent = "Bạn đã từ chối thao tác. Không có thay đổi nào được ghi lên AWS.";
    storeMessage("assistant", "Bạn đã từ chối thao tác. Không có thay đổi nào được ghi lên AWS.");
    setBusy(false);
  };
}

function handleResolutionOptions(res, agentRow, originalPrompt, pendingAction) {
  const options = Array.isArray(res.data?.resolution_options) ? res.data.resolution_options : [];
  const textContainer = agentRow.querySelector(".agent-text");
  textContainer.textContent = res.message || "Yêu cầu cần thêm lựa chọn để tiếp tục.";

  const card = document.createElement("div");
  card.className = "resolution-card";

  const title = document.createElement("div");
  title.className = "resolution-title";
  title.textContent = "Chọn cách xử lý";
  card.append(title);

  const optionList = document.createElement("div");
  optionList.className = "resolution-options";
  card.append(optionList);

  const finishWithoutChange = message => {
    card.remove();
    state.pending = null;
    textContainer.textContent = message;
    storeMessage("assistant", message);
    setBusy(false);
  };

  const executeChoice = async option => {
    if (option.id === "cancel") {
      finishWithoutChange("Đã hủy yêu cầu. Không có thay đổi nào được thực hiện trên AWS.");
      return;
    }
    if (option.id === "use_existing") {
      finishWithoutChange(`Đã giữ nguyên Lambda '${res.data?.function_name || "hiện có"}'. Không có thay đổi nào được thực hiện.`);
      return;
    }

    let toolName;
    let parameters;
    if (option.id === "update_existing") {
      toolName = "update_lambda_code";
      parameters = {
        function_name: res.data?.function_name || pendingAction.parameters.function_name,
        code_content: pendingAction.parameters.code_content,
        filename: "lambda_function.py"
      };
    } else if (option.id === "create_with_new_name") {
      toolName = "create_lambda_function";
      parameters = {
        ...pendingAction.parameters,
        function_name: option.suggested_function_name
      };
    } else {
      return;
    }

    optionList.querySelectorAll("button").forEach(button => { button.disabled = true; });
    setBusy(true);
    const logsBox = agentRow.querySelector(".agent-logs");
    const workingStep = document.createElement("div");
    workingStep.className = "log-working";
    workingStep.innerHTML = `<span class="working-pulse">Working • Applying selected resolution</span>`;
    logsBox.append(workingStep);

    try {
      const owner = sessionOwner;
      const result = await resolveAction(await fetchChat({
        confirmed: true,
        service: "lambda",
        tool_name: toolName,
        parameters,
        agent_state: pendingAction.agent_state,
        model: pendingAction.model || state.selectedModel
      }));
      if (sessionOwner !== owner) return;
      workingStep.remove();
      card.remove();

      if (result.status === "USER_ACTION_REQUIRED") {
        state.pending = { ...pendingAction, tool_name: toolName, parameters };
        handleResolutionOptions(result, agentRow, originalPrompt, state.pending);
        setBusy(false);
        return;
      }
      if (result.status === "CONFIRMATION_REQUIRED") {
        state.pending = null;
        handleConfirmation(result, agentRow, originalPrompt);
        setBusy(false);
        return;
      }

      state.pending = null;
      const message = result.message || "Đã xử lý lựa chọn thành công.";
      if (result.action_id) agentRow.dataset.actionId = result.action_id;
      const saved = storeMessage("assistant", message, result.data || null,
        result.action_id || null, result.status === "PENDING" ? "PENDING" : result.status);
      state.history.push({ role: "assistant", content: message });
      state.history = state.history.slice(-10);
      streamText(textContainer, saved?.content || message);
      setBusy(false);
    } catch (err) {
      workingStep.remove();
      optionList.querySelectorAll("button").forEach(button => { button.disabled = false; });
      textContainer.textContent = `Không thể thực hiện lựa chọn: ${err.message}`;
      setBusy(false);
    }
  };

  options.forEach(option => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `resolution-option ${option.changes_aws ? "changes-aws" : "no-change"}`;

    const label = document.createElement("span");
    label.className = "resolution-option-label";
    label.textContent = option.label;
    const description = document.createElement("span");
    description.className = "resolution-option-description";
    description.textContent = option.description || "";
    button.append(label, description);
    button.onclick = () => executeChoice(option);
    optionList.append(button);
  });

  agentRow.append(card);
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
}

function resetChat() {
  state.realtime?.stop();
  state.history = [];
  state.pending = null;
  state.currentSessionId = null;
  els.conversationFeed.innerHTML = "";
  els.conversationFeed.classList.add("hidden");
  els.emptyView.classList.remove("hidden");
  els.promptInput.value = "";
  els.promptInput.style.height = "auto";
  setBusy(false);
  renderRecentSessions();
  els.promptInput.focus();
}

async function loadHealth() {
  let configuredToolCount = null;
  try {
    const configResponse = await fetch("/site-config.json", { cache: "no-store" });
    if (configResponse.ok) {
      const config = await configResponse.json();
      if (Number.isSafeInteger(config.tools_loaded) && config.tools_loaded >= 0) {
        configuredToolCount = config.tools_loaded;
      }
      if (config.api_enabled === false) {
        state.apiAvailable = false;
        els.statusBadge.textContent = "Bản thử giao diện • Chưa nối API";
        els.toolsPill.textContent = "⚡ Preview";
        els.promptInput.placeholder = "Bản thử giao diện: chờ kết nối API Gateway để sử dụng chat";
        els.promptInput.title = "Website đã triển khai; API ChatOps chưa được kết nối.";
        els.promptInput.disabled = true;
        updateSendBtnState();
        return;
      }
      if (typeof config.appsync?.graphql_url === "string") {
        try {
          state.realtime = createAppSyncSubscription({
            graphqlUrl: config.appsync.graphql_url,
            getIdToken: () => globalThis.SatiBotAuth?.getIdToken?.(),
            onResult: record => updateActionResult(record, true),
            onState: setRealtimeState
          });
          if (state.currentSessionId) state.realtime.start(state.currentSessionId);
        } catch {
          setRealtimeState("disconnected");
        }
      }
    }
  } catch {
    // Local demo does not publish site-config.json.
  }
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const toolCount = Number.isSafeInteger(data.tools_loaded) && data.tools_loaded >= 0
      ? data.tools_loaded : configuredToolCount;
    els.statusBadge.textContent = data.status === "API_READY"
      ? `${data.region || "ap-southeast-1"} • API Gateway online`
      : `${data.region || "ap-southeast-1"} • ${toolCount ?? "—"} công cụ`;
    els.awsStatusPill.textContent = `☁️ ${data.region || "ap-southeast-1"}`;
    els.toolsPill.textContent = `⚡ ${toolCount ?? "—"} công cụ`;
    els.topModelText.textContent = "SatiBot";
    if (data.model && !localStorage.getItem("satibot.model") && VALID_MODELS.includes(data.model)) {
      setSelectedModel(data.model);
    }
  } catch {
    els.statusBadge.textContent = "Backend Offline";
  }
}

function setSelectedModel(model) {
  if (!model || !VALID_MODELS.includes(model)) return;
  state.selectedModel = model;
  localStorage.setItem("satibot.model", model);
  if (els.currentModelLabel) {
    els.currentModelLabel.textContent = model;
  }
  if (els.modelDropdownMenu) {
    els.modelDropdownMenu.querySelectorAll(".model-option").forEach(opt => {
      const isSelected = opt.getAttribute("data-value") === model;
      opt.classList.toggle("selected", isSelected);
      opt.setAttribute("aria-selected", isSelected ? "true" : "false");
    });
  }
}

function toggleModelMenu(forceOpen) {
  if (!els.modelDropdownMenu || !els.modelPickerBtn) return;
  const willOpen = typeof forceOpen === "boolean"
    ? forceOpen
    : !els.modelDropdownMenu.classList.contains("open");
  els.modelDropdownMenu.classList.toggle("open", willOpen);
  els.modelPickerBtn.classList.toggle("open", willOpen);
  els.modelPickerBtn.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

// Event Listeners
els.promptInput.addEventListener("input", () => {
  els.promptInput.style.height = "auto";
  els.promptInput.style.height = Math.min(els.promptInput.scrollHeight, 140) + "px";
  updateSendBtnState();
});

els.promptInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!els.sendBtn.disabled) {
      handleSendMessage();
    }
  }
});

els.sendBtn.onclick = handleSendMessage;
els.newChatBtn.onclick = resetChat;

if (els.modelPickerBtn) {
  els.modelPickerBtn.addEventListener("click", e => {
    e.stopPropagation();
    if (!state.isBusy) {
      toggleModelMenu();
    }
  });
}

if (els.modelDropdownMenu) {
  els.modelDropdownMenu.querySelectorAll(".model-option").forEach(opt => {
    const choose = e => {
      e.stopPropagation();
      const val = opt.getAttribute("data-value");
      setSelectedModel(val);
      toggleModelMenu(false);
    };
    opt.addEventListener("click", choose);
    opt.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        choose(e);
      }
    });
  });
}

document.addEventListener("click", e => {
  if (els.modelPickerContainer && !els.modelPickerContainer.contains(e.target)) {
    toggleModelMenu(false);
  }
});

document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    toggleModelMenu(false);
  }
});

setSelectedModel(state.selectedModel);

function toggleSidebar() {
  els.sidebar.classList.toggle("collapsed");
}

function openSidebar() {
  els.sidebar.classList.remove("collapsed");
}

if (els.sidebarToggleBtn) {
  els.sidebarToggleBtn.onclick = toggleSidebar;
}

if (els.lightningBtn) {
  els.lightningBtn.onclick = () => {
    if (els.sidebar.classList.contains("collapsed")) {
      openSidebar();
    } else {
      toggleSidebar();
    }
  };
}

els.approvalToggle.onclick = () => {
  state.autoApproval = !state.autoApproval;
  els.approvalToggle.classList.toggle("active", state.autoApproval);
};

// Global Shortcut: ⌘K or Ctrl+K for new chat
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    resetChat();
  }
});

async function syncSessionOwner() {
  const owner = await window.SatiBotAuth?.getUserId();
  if ((owner || null) === sessionOwner) return;
  state.controller?.abort();
  state.realtime?.stop();
  sessionOwner = owner || null;
  sessionStorageKey = chatStorageKey(sessionOwner);
  resetChat();
  loadStoredSessions();
  renderRecentSessions();
}
void syncSessionOwner();
window.addEventListener("satibot-auth-changed", syncSessionOwner);
window.addEventListener("online", () => {
  state.realtime?.networkOnline();
});
window.addEventListener("offline", () => state.realtime?.networkOffline());
loadHealth();
els.promptInput.focus();
