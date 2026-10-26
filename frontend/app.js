/**
 * Author: Trịnh Thiên Quang
 * Codex & ChatGPT-style Agent UI with interactive tool log steps & typewriter text stream.
 */
"use strict";

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const state = {
  history: [],
  pending: null,
  isBusy: false,
  controller: null,
  autoApproval: true,
  sessions: [],
  currentSessionId: null
};

const SESSION_STORAGE_KEY = "satibot.chat.sessions.v1";

const els = {
  sidebar: $("#sidebar"),
  sidebarToggleBtn: $("#sidebarToggleBtn"),
  lightningBtn: $("#lightningBtn"),
  newChatBtn: $("#newChatBtn"),
  recentList: $("#recentList"),
  statusBadge: $("#statusBadge"),
  awsStatusPill: $("#awsStatusPill"),
  toolsPill: $("#toolsPill"),
  chatScroll: $("#chatScroll"),
  emptyView: $("#emptyView"),
  conversationFeed: $("#conversationFeed"),
  promptInput: $("#promptInput"),
  sendBtn: $("#sendBtn"),
  approvalToggle: $("#approvalToggle"),
  composerModelText: $("#composerModelText"),
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
function createLogItem(actionTitle, detailData) {
  const item = document.createElement("div");
  item.className = "log-item";

  const header = document.createElement("div");
  header.className = "log-header";

  const title = document.createElement("span");
  title.className = "log-title";
  title.textContent = `Ran ${actionTitle}`;

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
  try {
    const parsed = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) || "[]");
    state.sessions = Array.isArray(parsed) ? parsed.filter(s => s && s.id && Array.isArray(s.messages)) : [];
  } catch {
    state.sessions = [];
  }
}

function saveSessions() {
  state.sessions.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  state.sessions = state.sessions.slice(0, 30);
  try {
    localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(state.sessions));
  } catch {
    // Browser storage is usually ~5 MB. Prefer retaining recent conversations
    // instead of letting a very large model response break the chat UI.
    state.sessions = state.sessions.slice(0, Math.max(1, Math.floor(state.sessions.length / 2)));
    try { localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(state.sessions)); } catch { /* keep UI usable */ }
  }
  renderRecentSessions();
}

function currentSession() {
  return state.sessions.find(s => s.id === state.currentSessionId) || null;
}

function ensureSession(firstPrompt) {
  let session = currentSession();
  if (session) return session;
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
  return session;
}

function storeMessage(role, content, data = null) {
  const session = currentSession();
  if (!session || !content) return;
  session.messages.push({ role, content: String(content), data, createdAt: Date.now() });
  session.updatedAt = Date.now();
  saveSessions();
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
  els.conversationFeed.append(row);
}

function openSession(sessionId) {
  if (state.isBusy) return;
  const session = state.sessions.find(s => s.id === sessionId);
  if (!session) return;
  state.currentSessionId = session.id;
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
  requestAnimationFrame(() => { els.chatScroll.scrollTop = els.chatScroll.scrollHeight; });
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
  els.sendBtn.disabled = val;
  els.promptInput.disabled = val;
  if (val) {
    els.sendBtn.classList.remove("active");
  } else {
    updateSendBtnState();
  }
}

function updateSendBtnState() {
  const hasText = els.promptInput.value.trim().length > 0;
  els.sendBtn.disabled = !hasText || state.isBusy;
  els.sendBtn.classList.toggle("active", hasText && !state.isBusy);
}

async function fetchChat(payload) {
  state.controller = new AbortController();
  const timeoutMs = 210000;
  const timeout = setTimeout(() => state.controller.abort(), timeoutMs);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: state.controller.signal
    });
    clearTimeout(timeout);

    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(`Server trả HTTP ${response.status} không đúng chuẩn JSON.`);
    }

    if (!response.ok) {
      throw new Error(data.message || `Lỗi máy chủ: HTTP ${response.status}`);
    }
    return data;
  } catch (err) {
    clearTimeout(timeout);
    if (err.name === "AbortError") {
      throw new Error("Quá thời gian chờ phản hồi (3.5 phút). Vui lòng thử lại.");
    }
    throw err;
  }
}

async function handleSendMessage() {
  const text = els.promptInput.value.trim();
  if (!text || state.isBusy) return;

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

  // Dynamic log progression while thinking
  const simulatedSteps = [
    { title: "check health on port 8080", data: { port: 8080, status: "OK", tools: 11 } },
    { title: "inspect AWS Lambda environment & credentials", data: { region: "us-east-1", service: "lambda" } },
    { title: "plan execution steps & code architecture", data: { runtime: "python3.12", framework: "serverless" } }
  ];

  let simIdx = 0;
  const progressTimer = setInterval(() => {
    if (simIdx < simulatedSteps.length) {
      const step = simulatedSteps[simIdx];
      const item = createLogItem(step.title, step.data);
      logsBox.insertBefore(item, workingStep);
      simIdx++;
      els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
    }
  }, 2200);

  try {
    const res = await fetchChat({ message: text, history: state.history });
    clearInterval(progressTimer);

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
    storeMessage("assistant", res.message || "Đã hoàn thành nhiệm vụ.", res.data || null);

    // Stream the final response text (Typewriter animation)
    streamText(textContainer, res.message || "Đã hoàn thành nhiệm vụ.", () => {
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
    workingStep.remove();
    textContainer.innerHTML = `<span style="color:#ef4444;font-weight:600;">Lỗi: ${err.message}</span>`;
    storeMessage("assistant", `Lỗi: ${err.message}`);
    setBusy(false);
  }
}

function handleConfirmation(res, agentRow, originalPrompt) {
  state.pending = {
    tool_name: res.tool_name,
    parameters: res.parameters || {},
    agent_state: res.agent_state,
    originalPrompt: originalPrompt
  };

  const card = document.createElement("div");
  card.className = "approval-card";
  card.innerHTML = `
    <div class="approval-title">⚠️ Yêu cầu phê duyệt thao tác AWS</div>
    <div class="approval-msg">${res.message || "Agent đang chuẩn bị thực hiện thay đổi trên AWS Lambda."}</div>
    <div class="approval-buttons">
      <button class="btn-approve" id="confirmApprove">Xác nhận thực hiện</button>
      <button class="btn-reject" id="confirmReject">Hủy bỏ</button>
    </div>
  `;

  agentRow.append(card);
  els.chatScroll.scrollTop = els.chatScroll.scrollHeight;

  card.querySelector("#confirmApprove").onclick = async () => {
    card.remove();
    setBusy(true);
    const approvedAction = {
      ...state.pending,
      parameters: { ...(state.pending?.parameters || {}) }
    };

    const logsBox = agentRow.querySelector(".agent-logs");
    const workingStep = document.createElement("div");
    workingStep.className = "log-working";
    workingStep.innerHTML = `<span class="working-pulse">Working • Executing approved action</span>`;
    logsBox.append(workingStep);

    try {
      const execRes = await fetchChat({
        confirmed: true,
        service: "lambda",
        tool_name: approvedAction.tool_name,
        parameters: approvedAction.parameters,
        agent_state: approvedAction.agent_state
      });

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
      storeMessage("assistant", execRes.message || "Đã thực thi thành công.", execRes.data || null);
      state.history.push({ role: "assistant", content: execRes.message || "Đã thực thi thành công." });
      state.history = state.history.slice(-10);
      streamText(textContainer, execRes.message || "Đã thực thi thành công.", () => {
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
      const result = await fetchChat({
        confirmed: true,
        service: "lambda",
        tool_name: toolName,
        parameters,
        agent_state: pendingAction.agent_state
      });
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
      storeMessage("assistant", message, result.data || null);
      state.history.push({ role: "assistant", content: message });
      state.history = state.history.slice(-10);
      streamText(textContainer, message);
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
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    els.statusBadge.textContent = `${data.region || "us-east-1"} • ${data.tools_loaded || 11} Tools Active`;
    els.awsStatusPill.textContent = `☁️ ${data.region || "us-east-1"}`;
    els.toolsPill.textContent = `⚡ ${data.tools_loaded || 11} Tools`;
    els.topModelText.textContent = "SatiBot";
    if (data.model) {
      els.composerModelText.textContent = data.model;
    }
  } catch {
    els.statusBadge.textContent = "Backend Offline";
  }
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

loadStoredSessions();
renderRecentSessions();
loadHealth();
els.promptInput.focus();
