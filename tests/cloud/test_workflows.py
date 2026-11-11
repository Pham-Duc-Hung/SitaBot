"""
Author: Trịnh Thiên Quang
Kiểm thử Gemini, kết nối AWS, lịch sử và xác nhận tác vụ.
"""

import base64
import io
import json
import urllib.error
from unittest.mock import Mock
from botocore.exceptions import ClientError
import pytest
from backend.functions.chatbot import cloud_handler as chat
from backend.functions.connector import index as connector
from backend.functions.dispatcher import cloud_handler as dispatch
from backend.functions.formatter import cloud_handler as formatter
from backend.functions.history import index as history
from backend.shared import gemini
from backend.shared.cloud_errors import BackendError
from backend.shared.tool_registry import registry
from backend.shared.workflow_store import table
from .helpers import api_event


def test_gemini_error_modes_non_retryable_timeout_and_malformed_payload(
    cloud_store, monkeypatch
):
    monkeypatch.setattr(gemini.time, "sleep", Mock())

    # 1. Lỗi HTTP 401 không thử lại vô ích
    http_401 = Mock(
        side_effect=urllib.error.HTTPError(
            gemini.ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(b"bad")
        )
    )
    monkeypatch.setattr(gemini.urllib.request, "urlopen", http_401)
    with pytest.raises(BackendError) as err_401:
        gemini.complete([{"role": "user", "content": "hi"}])
    assert err_401.value.code == "GeminiUnavailable"
    assert http_401.call_count == 1

    # 2. Lỗi mạng TimeoutError thử đủ 3 lần trước khi dừng
    timeout_mock = Mock(side_effect=TimeoutError("timed out"))
    monkeypatch.setattr(gemini.urllib.request, "urlopen", timeout_mock)
    with pytest.raises(BackendError) as err_timeout:
        gemini.complete([{"role": "user", "content": "hi"}])
    assert err_timeout.value.code == "GeminiUnavailable"
    assert timeout_mock.call_count == 3

    # 3. Dữ liệu trả về sai cấu trúc (thiếu choices hoặc content sai kiểu)
    bad_reply = Mock()
    bad_reply.__enter__ = Mock(return_value=bad_reply)
    bad_reply.__exit__ = Mock(return_value=False)
    bad_reply.read.return_value = (
        b'{"choices":[{"message":{"content":["not-a-string"]}}]}'
    )
    monkeypatch.setattr(gemini.urllib.request, "urlopen", Mock(return_value=bad_reply))
    with pytest.raises(BackendError) as err_invalid:
        gemini.complete([{"role": "user", "content": "hi"}])
    assert err_invalid.value.code == "InvalidGeminiResponse"


def test_formatter_rejects_unexpected_tool_calls_from_gemini_and_uses_fallback(
    cloud_store, monkeypatch
):
    action = cloud_store.create_action(
        "alice",
        "ses_inj",
        registry()["list_lambda_functions"],
        {"max_items": 5},
        "ap-southeast-1",
    )
    envelope = dispatch.envelope(
        action,
        {
            "status": "SUCCESS",
            "message": "Bản ghi gốc an toàn.",
            "data": {"functions": ["fn-a"]},
        },
    )
    # Giả lập prompt injection khiến Gemini cố trả về tool_calls trong bước Formatter
    monkeypatch.setattr(
        formatter.gemini,
        "complete",
        Mock(
            return_value={
                "content": "Gọi xóa hàm",
                "tool_calls": [{"function": {"name": "set_emergency_kill_switch"}}],
            }
        ),
    )
    monkeypatch.setattr(formatter.appsync, "publish", Mock())

    out = formatter.handle(envelope, None)
    assert out["formatter_fallback"] is True
    assert out["message"] == "Bản ghi gốc an toàn."
    assert out["status"] == "SUCCESS"


def test_connector_status_projection_onboarding_order_and_validation(cloud_store):
    # GET /connections chỉ trả trường công khai, tuyệt đối không trả external_id
    get_res = connector.lambda_handler(
        api_event("/api/connections", owner="alice", method="GET"), None
    )
    assert get_res["statusCode"] == 200
    data = json.loads(get_res["body"])
    assert data["status"] == "CONNECTED"
    assert data["account_id"] == "123456789012"
    assert "external_id" not in data

    # Người dùng mới chưa gọi /external-id mà gọi /verify -> 409 ExternalIdRequired
    new_user_verify = connector.lambda_handler(
        api_event(
            "/connections/verify",
            {"role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole"},
            owner="charlie",
        ),
        None,
    )
    assert new_user_verify["statusCode"] == 409
    assert json.loads(new_user_verify["body"])["error_code"] == "ExternalIdRequired"

    # ARN không hợp lệ -> 400; sai method -> 405; sai đường dẫn -> 404
    bad_arn = connector.lambda_handler(
        api_event("/connections/verify", {"role_arn": "not-an-arn"}), None
    )
    assert bad_arn["statusCode"] == 400
    assert (
        connector.lambda_handler(api_event("/connections/verify", method="GET"), None)[
            "statusCode"
        ]
        == 405
    )
    assert (
        connector.lambda_handler(
            api_event("/connections/unknown", method="POST"), None
        )["statusCode"]
        == 404
    )


def test_history_query_validation_filters_and_method_guard(cloud_store):
    table("ACTION_HISTORY_TABLE").put_item(
        Item={
            "user_id": "alice",
            "created_at": "2026-11-08T05:00:00.000000Z",
            "action_id": "act_h1",
            "service": "lambda",
            "status": "SUCCESS",
            "session_id": "ses_h",
            "message": "OK",
        }
    )
    # POST lên /history ở cloud mode bị chặn 405
    assert (
        history.lambda_handler(api_event("/history", method="POST"), None)["statusCode"]
        == 405
    )

    # Các truy vấn sai biên: limit ngoài [1..100], from > to, timestamp sai định dạng, cursor giả mạo
    for bad_query in (
        {"limit": "0"},
        {"limit": "101"},
        {"from": "2026-11-08T06:00:00Z", "to": "2026-11-08T05:00:00Z"},
        {"created_at": "2026/11/08 05:00:00"},
        {
            "cursor": base64.urlsafe_b64encode(
                b'{"user_id":"bob","created_at":"2026-11-08T05:00:00Z"}'
            ).decode()
        },
    ):
        res = history.lambda_handler(
            api_event("/history", method="GET", query=bad_query), None
        )
        assert res["statusCode"] == 400

    # Lọc kết hợp service + status + session_id + khoảng thời gian hợp lệ
    ok_res = history.lambda_handler(
        api_event(
            "/history",
            method="GET",
            query={
                "service": "lambda",
                "status": "SUCCESS",
                "session_id": "ses_h",
                "from": "2026-11-08T04:00:00Z",
                "to": "2026-11-08T06:00:00Z",
            },
        ),
        None,
    )
    assert ok_res["statusCode"] == 200
    assert len(json.loads(ok_res["body"])["items"]) == 1


def test_duplicate_confirmations_idempotent_and_completed_short_circuit(
    cloud_store, monkeypatch
):
    action = cloud_store.create_action(
        "alice",
        "ses_dup",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 512},
        "ap-southeast-1",
    )
    sfn_mock = Mock()
    sfn_mock.start_execution.side_effect = [
        {
            "executionArn": "arn:aws:states:ap-southeast-1:123456789012:execution:satibot:1"
        },
        ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "already running"}},
            "StartExecution",
        ),
    ]
    monkeypatch.setattr(chat.boto3, "client", Mock(return_value=sfn_mock))

    # Gửi xác nhận lặp 2 lần liên tiếp -> cả hai đều trả 202 ACCEPTED an toàn, không tạo execution trùng
    first = chat.handle(
        api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}),
        None,
    )
    second = chat.handle(
        api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}),
        None,
    )
    assert first["statusCode"] == 202
    assert second["statusCode"] == 202
    assert sfn_mock.start_execution.call_count == 2

    # Khi tác vụ đã COMPLETED, xác nhận lặp trả ngay kết quả đã lưu (200) mà không gọi Step Functions nữa
    claimed = cloud_store.get_action("alice", action["action_id"])
    cloud_store.claim(claimed)
    cloud_store.finish(
        claimed,
        {
            "status": "SUCCESS",
            "message": "Đã cập nhật RAM 512MB.",
            "data": {"memory_mb": 512},
        },
    )

    third = chat.handle(
        api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}),
        None,
    )
    assert third["statusCode"] == 200
    assert json.loads(third["body"])["data"]["memory_mb"] == 512
    assert sfn_mock.start_execution.call_count == 2


def test_stored_action_schema_drift_and_formatter_history_key_collision(
    cloud_store, monkeypatch
):
    action = cloud_store.create_action(
        "alice",
        "ses_drift",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    cloud_store.approve("alice", action["action_id"])
    # Giả lập bản ghi trong DB bị lệch action so với schema hiện hành
    table("PENDING_ACTION_TABLE").update_item(
        Key={"action_id": action["action_id"]},
        UpdateExpression="SET #a = :tampered",
        ExpressionAttributeNames={"#a": "action"},
        ExpressionAttributeValues={":tampered": "delete_everything"},
    )
    exec_spy = Mock()
    monkeypatch.setattr(dispatch, "_execute", exec_spy)
    drift_out = dispatch.handle(
        {"user_id": "alice", "action_id": action["action_id"]}, None
    )
    assert drift_out["error_code"] == "InvalidStoredAction"
    exec_spy.assert_not_called()

    # Giả lập xung đột khóa lịch sử (cùng user_id + created_at nhưng khác action_id)
    monkeypatch.setattr(
        formatter.gemini, "complete", Mock(return_value={"content": "OK"})
    )
    monkeypatch.setattr(formatter.appsync, "publish", Mock())
    first_env = dispatch.envelope(
        action, {"status": "SUCCESS", "message": "OK", "data": {}}
    )
    assert formatter.handle(first_env, None)["status"] == "SUCCESS"
    colliding_env = {**first_env, "action_id": "act_different_collision"}
    with pytest.raises(BackendError) as collision_err:
        formatter.handle(colliding_env, None)
    assert collision_err.value.code == "HistoryConflict"
