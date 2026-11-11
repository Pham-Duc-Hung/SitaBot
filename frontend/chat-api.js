/** Browser contract for the cloud Chatbot and History APIs. */

async function authHeaders(getIdToken) {
  const headers = { Accept: "application/json", "Content-Type": "application/json" };
  const token = await getIdToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function browserIdToken() {
  const auth = globalThis.SatiBotAuth;
  return typeof auth?.getIdToken === "function" ? auth.getIdToken() : null;
}

async function readJson(response) {
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.message || (response.status === 401
      ? "Bạn cần đăng nhập để sử dụng API ChatOps."
      : `Lỗi máy chủ: HTTP ${response.status}`));
    error.status = response.status;
    error.code = data?.error_code;
    throw error;
  }
  if (!data || typeof data !== "object") {
    throw new Error(`Server trả HTTP ${response.status} không đúng chuẩn JSON.`);
  }
  return data;
}

export async function chatRequest(payload, {
  sessionId,
  signal,
  fetchImpl = fetch,
  getIdToken = browserIdToken
} = {}) {
  const body = sessionId ? { ...payload, session_id: sessionId } : payload;
  return readJson(await fetchImpl("/api/chat", {
    method: "POST",
    headers: await authHeaders(getIdToken),
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify(body),
    signal
  }));
}

export function confirmationPayload(action, typedPhrase = "") {
  if (action.confirmation_mode === "explicit_phrase") {
    const expected = action.required_confirmation;
    if (!expected || typedPhrase !== expected) {
      throw new Error(`Hãy nhập chính xác câu xác nhận: ${expected || "DELETE <bucket-name>"}`);
    }
    if (action.action_id && action.parameters?.confirmation !== expected) {
      throw new Error("Câu xác nhận trong tác vụ chờ không khớp; hãy tạo yêu cầu xóa mới.");
    }
  }
  if (action.action_id) {
    return {
      confirmed: true,
      action_id: action.action_id,
      ...(action.confirmation_mode === "explicit_phrase" ? { confirmation_text: typedPhrase } : {})
    };
  }
  return {
    confirmed: true,
    service: action.service,
    tool_name: action.tool_name,
    parameters: {
      ...action.parameters,
      ...(action.confirmation_mode === "explicit_phrase" ? { confirmation: typedPhrase } : {})
    },
    agent_state: action.agent_state,
    model: action.model
  };
}

export async function pollActionResult(actionId, sessionId, {
  fetchImpl = fetch,
  getIdToken = browserIdToken,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)),
  attempts = 15,
  intervalMs = 2000
} = {}) {
  if (!actionId || !sessionId) return null;
  const token = await getIdToken();
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const options = { headers, credentials: "same-origin", cache: "no-store" };
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    let cursor = null;
    // DynamoDB applies FilterExpression after Limit; the target may be on a later page.
    for (let pageIndex = 0; pageIndex < 5; pageIndex += 1) {
      const query = new URLSearchParams({ session_id: sessionId, limit: "100" });
      if (cursor) query.set("cursor", cursor);
      const page = await readJson(await fetchImpl(`/api/history?${query}`, options));
      const match = page.items?.find(item => item.action_id === actionId);
      if (match && !["PENDING", "EXECUTING", "ACCEPTED"].includes(match.status)) {
        const detail = await readJson(await fetchImpl(
          `/api/history?${new URLSearchParams({ created_at: match.created_at })}`,
          options
        ));
        return detail.item;
      }
      cursor = page.cursor;
      if (!cursor) break;
    }
    if (attempt + 1 < attempts) await wait(intervalMs);
  }
  return null;
}

export async function resolveQueuedAction(result, { poll = pollActionResult } = {}) {
  if (result.status !== "ACCEPTED") return result;
  const actionLabel = result.action_id ? ` (mã ${result.action_id})` : "";
  const pending = {
    ...result,
    status: "PENDING",
    message: `AWS đã nhận tác vụ${actionLabel} và đang xử lý; chưa có kết quả cuối cùng.`
  };
  if (!result.action_id || !result.session_id) return pending;
  try {
    const completed = await poll(result.action_id, result.session_id);
    return completed || {
      ...pending,
      message: `Tác vụ${actionLabel} vẫn đang xử lý. Kiểm tra lịch sử trước khi gửi lại yêu cầu.`
    };
  } catch {
    return {
      ...pending,
      message: `Đã gửi tác vụ${actionLabel}, nhưng chưa đọc được kết quả từ lịch sử. Kiểm tra lịch sử trước khi gửi lại yêu cầu.`
    };
  }
}
