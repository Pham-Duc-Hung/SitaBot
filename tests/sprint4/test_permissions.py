"""
Author: Trịnh Thiên Quang
Kiểm thử phân quyền và cách ly người dùng của các Lambda.
"""

import io
import json
import urllib.error
from unittest.mock import Mock
import boto3
from botocore.exceptions import BotoCoreError
import pytest
from backend.functions.chatbot import cloud_handler as chat
from backend.functions.connector import index as connector
from backend.functions.dispatcher import cloud_handler as dispatch
from backend.functions.dispatcher.services.lambda_service import LambdaService
from backend.functions.formatter import cloud_handler as formatter
from backend.functions.history import index as history
from backend.shared import appsync, gemini
from backend.shared.cloud_errors import BackendError
from backend.shared.runtime import clear_runtime_caches
from backend.shared.tool_registry import registry
from .helpers import deny_error, api_event


def test_chatbot_secretsmanager_access_denied_returns_403_without_arn_leak(
    sprint4_cloud, monkeypatch
):
    sm_mock = Mock()
    sm_mock.get_secret_value.side_effect = deny_error("secretsmanager:GetSecretValue")
    monkeypatch.setattr(gemini.boto3, "client", Mock(return_value=sm_mock))

    res = chat.handle(api_event("/chat", {"message": "Liệt kê hàm Lambda"}), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 403
    assert body["error_code"] == "GeminiSecretAccessDenied"
    assert "private-role" not in res["body"]


def test_chatbot_dynamodb_and_stepfunctions_access_denied_return_403(
    sprint4_cloud, monkeypatch
):
    real_resource = boto3.resource
    # 1. Thiếu quyền đọc SessionState
    denied_table = Mock()
    denied_table.get_item.side_effect = deny_error("dynamodb:GetItem")
    denied_res = Mock()
    denied_res.Table.return_value = denied_table
    monkeypatch.setattr(
        "backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res)
    )

    res_db = chat.handle(api_event("/chat", {"message": "Kiểm tra EC2"}), None)
    assert res_db["statusCode"] == 403
    assert json.loads(res_db["body"])["error_code"] == "AccessDenied"
    assert "private-role" not in res_db["body"]

    # 2. Khôi phục DynamoDB, kiểm tra thiếu quyền states:StartExecution khi confirm
    monkeypatch.setattr("backend.shared.workflow_store.boto3.resource", real_resource)
    clear_runtime_caches()
    sfn_mock = Mock()
    sfn_mock.start_execution.side_effect = deny_error("states:StartExecution")
    monkeypatch.setattr(chat.boto3, "client", Mock(return_value=sfn_mock))

    action = sprint4_cloud.create_action(
        "alice",
        "ses_1",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    res_sfn = chat.handle(
        api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}),
        None,
    )
    assert res_sfn["statusCode"] == 403
    assert json.loads(res_sfn["body"])["error_code"] == "AccessDenied"


def test_dispatcher_dynamodb_and_sts_access_denied_never_invoke_target_service(
    sprint4_cloud, monkeypatch
):
    exec_spy = Mock()
    monkeypatch.setattr(dispatch, "_execute", exec_spy)

    action = sprint4_cloud.create_action(
        "alice",
        "ses_disp",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 512},
        "ap-southeast-1",
    )
    sprint4_cloud.approve("alice", action["action_id"])

    # 1. STS AssumeRole bị từ chối bởi Trust Policy của tài khoản khách hàng
    sts_denied = Mock()
    sts_denied.assume_role.side_effect = deny_error(
        "sts:AssumeRole", code="AccessDenied"
    )
    monkeypatch.setattr(dispatch.boto3, "client", Mock(return_value=sts_denied))

    out = dispatch.handle({"user_id": "alice", "action_id": action["action_id"]}, None)
    assert out["result"]["status"] == "ERROR"
    assert out["result"]["error_code"] == "AccessDenied"
    assert "private-role" not in json.dumps(out)
    exec_spy.assert_not_called()

    # 2. Dispatcher bị từ chối đọc bảng PendingAction ngay từ đầu
    denied_tbl = Mock()
    denied_tbl.get_item.side_effect = deny_error("dynamodb:GetItem")
    denied_res = Mock()
    denied_res.Table.return_value = denied_tbl
    monkeypatch.setattr(
        "backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res)
    )
    clear_runtime_caches()

    out_store = dispatch.handle({"user_id": "alice", "action_id": "act_other"}, None)
    assert out_store["error_code"] == "AccessDenied"
    exec_spy.assert_not_called()


def test_customer_role_missing_permission_propagates_through_dispatcher_formatter_and_history(
    sprint4_cloud, monkeypatch
):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_perm",
        registry()["update_function_resources"],
        {"function_name": "prod-fn", "memory_mb": 512},
        "ap-southeast-1",
    )
    sprint4_cloud.approve("alice", action["action_id"])

    # Giả lập LambdaService trong tài khoản khách hàng thiếu quyền lambda:UpdateFunctionConfiguration
    svc = object.__new__(LambdaService)
    svc.region_name = "ap-southeast-1"
    svc.client = Mock()
    svc.client.get_function_configuration.return_value = {
        "MemorySize": 256,
        "Timeout": 15,
        "Runtime": "python3.12",
        "Handler": "index.handler",
        "State": "Active",
        "LastUpdateStatus": "Successful",
    }
    svc.client.update_function_configuration.side_effect = deny_error(
        "lambda:UpdateFunctionConfiguration"
    )

    monkeypatch.setattr(
        dispatch,
        "_execute",
        lambda item, _creds: svc.update_function_resources(**item["parameters"]),
    )
    monkeypatch.setattr(
        formatter.gemini,
        "complete",
        Mock(
            return_value={"content": "Role đích thiếu quyền cập nhật cấu hình Lambda."}
        ),
    )
    monkeypatch.setattr(formatter.appsync, "publish", Mock())

    dispatched = dispatch.handle(
        {"user_id": "alice", "action_id": action["action_id"]}, None
    )
    assert dispatched["result"]["error_code"] == "AccessDeniedException"
    assert (
        dispatched["result"]["required_permission"]
        == "lambda:UpdateFunctionConfiguration"
    )

    formatted = formatter.handle(dispatched, None)
    assert formatted["status"] == "ERROR"
    assert formatted["error_code"] == "AccessDeniedException"
    assert formatted["required_permission"] == "lambda:UpdateFunctionConfiguration"

    # Kiểm tra API History trả đúng required_permission cho chủ sở hữu và chặn người dùng khác
    list_res = history.lambda_handler(api_event("/history", method="GET"), None)
    items = json.loads(list_res["body"])["items"]
    assert len(items) == 1
    assert items[0]["required_permission"] == "lambda:UpdateFunctionConfiguration"

    bob_res = history.lambda_handler(
        api_event(
            "/history",
            owner="bob",
            method="GET",
            query={"created_at": action["created_at"]},
        ),
        None,
    )
    assert bob_res["statusCode"] == 404


def test_formatter_secret_appsync_and_dynamodb_permission_denials(
    sprint4_cloud, monkeypatch
):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_fmt",
        registry()["list_lambda_functions"],
        {"max_items": 5},
        "ap-southeast-1",
    )
    envelope = dispatch.envelope(
        action,
        {"status": "SUCCESS", "message": "Tìm thấy 2 hàm.", "data": {"count": 2}},
    )

    # 1. Formatter mất quyền đọc Secrets Manager -> tự động dùng fallback, vẫn ghi lịch sử
    sm_denied = Mock()
    sm_denied.get_secret_value.side_effect = deny_error("secretsmanager:GetSecretValue")
    monkeypatch.setattr(gemini.boto3, "client", Mock(return_value=sm_denied))

    # 2. Đồng thời AppSync trả HTTP 403 AccessDenied -> publication_status = FAILED nhưng không làm mất lịch sử
    monkeypatch.setattr(
        appsync.urllib.request,
        "urlopen",
        Mock(
            side_effect=urllib.error.HTTPError(
                "https://satibot.appsync-api.ap-southeast-1.amazonaws.com/graphql",
                403,
                "Forbidden",
                {},
                io.BytesIO(b'{"message":"AccessDeniedException"}'),
            )
        ),
    )

    out = formatter.handle(envelope, None)
    assert out["formatter_fallback"] is True
    assert out["publication_status"] == "FAILED"
    assert out["message"] == "Tìm thấy 2 hàm."

    # 3. Kiểm tra trực tiếp appsync.publish ném đúng AppSyncAccessDenied (403)
    with pytest.raises(BackendError) as exc_info:
        appsync.publish({"action_id": "act_1", "data": {}})
    assert exc_info.value.code == "AppSyncAccessDenied"
    assert exc_info.value.status == 403

    # 4. Formatter mất quyền dynamodb:PutItem trên ActionHistory -> trả lỗi AccessDenied an toàn
    denied_tbl = Mock()
    denied_tbl.put_item.side_effect = deny_error("dynamodb:PutItem")
    denied_res = Mock()
    denied_res.Table.return_value = denied_tbl
    monkeypatch.setattr(
        "backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res)
    )
    clear_runtime_caches()

    envelope_2 = {
        **envelope,
        "action_id": "act_2",
        "created_at": "2026-11-08T09:00:00.000000Z",
    }
    out_denied = formatter.handle(envelope_2, None)
    assert out_denied["error_code"] == "AccessDenied"
    assert "private-role" not in json.dumps(out_denied)


def test_connector_and_history_dynamodb_access_denied_and_transport_errors(
    sprint4_cloud, monkeypatch
):
    denied_tbl = Mock()
    denied_tbl.get_item.side_effect = deny_error("dynamodb:GetItem")
    denied_tbl.query.side_effect = deny_error("dynamodb:Query")
    denied_res = Mock()
    denied_res.Table.return_value = denied_tbl

    monkeypatch.setattr(connector.boto3, "resource", Mock(return_value=denied_res))
    monkeypatch.setattr(history.boto3, "resource", Mock(return_value=denied_res))
    clear_runtime_caches()

    conn_res = connector.lambda_handler(api_event("/connections", method="GET"), None)
    assert conn_res["statusCode"] == 403
    assert json.loads(conn_res["body"])["error_code"] == "ConnectionFailed"
    assert "private-role" not in conn_res["body"]

    hist_res = history.lambda_handler(api_event("/history", method="GET"), None)
    assert hist_res["statusCode"] == 403
    assert json.loads(hist_res["body"])["error_code"] == "AccessDenied"

    # BotoCoreError trên History & Connector
    broken_tbl = Mock()
    broken_tbl.query.side_effect = BotoCoreError()
    broken_tbl.get_item.side_effect = BotoCoreError()
    broken_res = Mock()
    broken_res.Table.return_value = broken_tbl
    monkeypatch.setattr(connector.boto3, "resource", Mock(return_value=broken_res))
    monkeypatch.setattr(history.boto3, "resource", Mock(return_value=broken_res))
    clear_runtime_caches()

    assert (
        history.lambda_handler(api_event("/history", method="GET"), None)["statusCode"]
        == 502
    )
    assert (
        connector.lambda_handler(api_event("/connections", method="GET"), None)[
            "statusCode"
        ]
        == 502
    )
