import test from "node:test";
import assert from "node:assert/strict";

import { chatRequest, confirmationPayload, pollActionResult, resolveQueuedAction } from "../frontend/chat-api.js";

const jsonResponse = (data, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => data
});

test("chat sends the Cognito token and stable backend session", async () => {
  let request;
  const result = await chatRequest({ message: "Tạo bucket" }, {
    sessionId: "ses_123",
    getIdToken: async () => "jwt-example",
    fetchImpl: async (url, options) => {
      request = { url, options };
      return jsonResponse({ status: "CONFIRMATION_REQUIRED", action_id: "act_1" });
    }
  });
  assert.equal(request.url, "/api/chat");
  assert.equal(request.options.headers.Authorization, "Bearer jwt-example");
  assert.equal(request.options.credentials, "same-origin");
  assert.deepEqual(JSON.parse(request.options.body), { message: "Tạo bucket", session_id: "ses_123" });
  assert.equal(result.action_id, "act_1");
});

test("local demo can call chat without a token, while cloud 401 is explicit", async () => {
  const calls = [];
  await chatRequest({ message: "Xin chào" }, {
    getIdToken: async () => null,
    fetchImpl: async (_url, options) => {
      calls.push(options);
      return jsonResponse({ status: "SUCCESS" });
    }
  });
  assert.equal(calls[0].headers.Authorization, undefined);
  await assert.rejects(chatRequest({ message: "Xin chào" }, {
    getIdToken: async () => null,
    fetchImpl: async () => jsonResponse({ error_code: "Unauthorized" }, 401)
  }), /cần đăng nhập/);
});

test("delete bucket requires the exact typed phrase in both approval paths", () => {
  const action = {
    action_id: "act_1", service: "s3", tool_name: "s3_delete_bucket_safe",
    confirmation_mode: "explicit_phrase", required_confirmation: "DELETE demo-bucket",
    parameters: { bucket_name: "demo-bucket", confirmation: "DELETE demo-bucket" }
  };
  assert.throws(() => confirmationPayload(action), /nhập chính xác/);
  assert.throws(() => confirmationPayload(action, "DELETE other-bucket"), /nhập chính xác/);
  assert.deepEqual(confirmationPayload(action, "DELETE demo-bucket"), {
    confirmed: true, action_id: "act_1", confirmation_text: "DELETE demo-bucket"
  });
  const local = confirmationPayload({ ...action, action_id: null }, "DELETE demo-bucket");
  assert.equal(local.parameters.confirmation, "DELETE demo-bucket");
  assert.equal(local.tool_name, "s3_delete_bucket_safe");
});

test("queued action waits for matching history record and fetches its result", async () => {
  let reads = 0;
  const urls = [];
  const result = await pollActionResult("act_target", "ses_123", {
    attempts: 3,
    wait: async () => {},
    getIdToken: async () => "jwt-example",
    fetchImpl: async (url, options) => {
      urls.push(url);
      assert.equal(options.headers.Authorization, "Bearer jwt-example");
      if (url.includes("created_at=")) {
        return jsonResponse({ item: { status: "SUCCESS", action_id: "act_target", session_id: "ses_123", message: "Bucket đã tạo", data: { bucket_name: "demo" } } });
      }
      reads += 1;
      return jsonResponse({ items: reads === 1 ? [] : [
        { action_id: "act_other", status: "SUCCESS", created_at: "2026-11-10T01:00:00Z" },
        { action_id: "act_target", status: "SUCCESS", created_at: "2026-11-10T02:00:00Z" }
      ] });
    }
  });
  assert.equal(reads, 2);
  assert.equal(result.message, "Bucket đã tạo");
  assert.match(urls[0], /session_id=ses_123/);
  assert.match(urls.at(-1), /created_at=/);
});

test("queued action without a recorded result remains pending", async () => {
  let calls = 0;
  const result = await pollActionResult("act_1", "ses_1", {
    attempts: 2,
    wait: async () => {},
    getIdToken: async () => null,
    fetchImpl: async () => {
      calls += 1;
      return jsonResponse({ items: [] });
    }
  });
  assert.equal(result, null);
  assert.equal(calls, 2);
});

test("history search follows DynamoDB cursor when filtering hides the first page", async () => {
  const urls = [];
  const result = await pollActionResult("act_target", "ses_1", {
    attempts: 1,
    getIdToken: async () => null,
    fetchImpl: async url => {
      urls.push(url);
      if (url.includes("created_at=")) return jsonResponse({ item: { status: "SUCCESS", action_id: "act_target", session_id: "ses_1" } });
      if (!url.includes("cursor=")) return jsonResponse({ items: [], cursor: "next-page" });
      return jsonResponse({ items: [
        { action_id: "act_target", status: "SUCCESS", created_at: "2026-11-10T02:00:00Z" }
      ] });
    }
  });
  assert.equal(result.action_id, "act_target");
  assert.match(urls[1], /cursor=next-page/);
});

test("accepted is not success until the matching history result is available", async () => {
  const queued = { status: "ACCEPTED", action_id: "act_1", session_id: "ses_1" };
  const pending = await resolveQueuedAction(queued, { poll: async () => null });
  assert.equal(pending.status, "PENDING");
  assert.match(pending.message, /act_1/);
  const finished = await resolveQueuedAction(queued, {
    poll: async (actionId, sessionId) => {
      assert.equal(actionId, "act_1");
      assert.equal(sessionId, "ses_1");
      return { status: "SUCCESS", message: "Đã tạo bucket" };
    }
  });
  assert.equal(finished.status, "SUCCESS");
  assert.equal(finished.message, "Đã tạo bucket");
  const unreadable = await resolveQueuedAction(queued, { poll: async () => { throw Error("offline"); } });
  assert.equal(unreadable.status, "PENDING");
  assert.match(unreadable.message, /chưa đọc được kết quả/);
});
