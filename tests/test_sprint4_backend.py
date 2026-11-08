"""
Author: Trịnh Thiên Quang
Kiểm thử toàn diện Sprint 4: phân quyền AccessDenied 5 Lambda, tối ưu Cold-start/Warm-cache,
chế độ lỗi Gemini & Formatter fallback, Connector, History, chống xác nhận lặp và CI/CD Canary/Rollback.
"""

import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
from unittest.mock import Mock
import zipfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from moto import mock_aws
import pytest

from backend.functions.chatbot import cloud_handler as chat
from backend.functions.connector import index as connector
from backend.functions.dispatcher import cloud_handler as dispatch
from backend.functions.dispatcher.services.lambda_service import LambdaService
from backend.functions.formatter import cloud_handler as formatter
from backend.functions.history import index as history
from backend.shared import appsync, gemini
from backend.shared.cloud_errors import BackendError
from backend.shared.runtime import (
    cached_boto_client,
    cached_boto_session,
    cached_dynamodb_table,
    clear_runtime_caches,
    is_access_denied,
    platform_fault_counts,
    runtime_cache_stats,
)
from backend.shared.tool_registry import (
    clear_registry_cache,
    llm_tools,
    registry,
    registry_cache_stats,
    validate_call,
)
from backend.shared.workflow_store import Store, table
from scripts.benchmark_coldstart import (
    benchmark_cloud_lambdas,
    benchmark_live_endpoint,
    benchmark_local_dispatcher,
    benchmark_subprocess_coldstart,
    benchmark_tool_registry,
)
from scripts.package_sprint2 import package
from scripts.verify_canary_rollback import (
    CanaryAliasSimulator,
    simulate_canary_and_rollback,
    verify_cdk_canary_template,
    verify_pipeline_template,
)

ROOT = Path(__file__).resolve().parents[1]


def deny_error(operation: str = "GetItem", code: str = "AccessDeniedException") -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": f"User: arn:aws:sts::123456789012:assumed-role/private-role is not authorized to perform: {operation}",
            }
        },
        operation,
    )


def api_event(path: str, payload=None, *, owner: str = "alice", method: str = "POST", query=None) -> dict:
    return {
        "rawPath": path,
        "body": json.dumps(payload) if payload is not None else "",
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": {"sub": owner}}},
        },
    }


@pytest.fixture
def sprint4_cloud(monkeypatch):
    env_vars = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-1",
        "AWS_REGION": "ap-southeast-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "SATIBOT_MODE": "cloud",
        "APPSYNC_GRAPHQL_URL": "https://satibot.appsync-api.ap-southeast-1.amazonaws.com/graphql",
        "ACTION_STATE_MACHINE_ARN": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:satibot-actions",
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)

    clear_runtime_caches()
    clear_registry_cache()
    gemini._SECRET_CACHE.clear()

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ap-southeast-1")
        for env_name, tbl_name, pk, sk in (
            ("SESSION_STATE_TABLE", "s4-sessions", "user_id", "session_id"),
            ("PENDING_ACTION_TABLE", "s4-pending", "action_id", None),
            ("AWS_CONNECTIONS_TABLE", "s4-connections", "user_id", None),
            ("ACTION_HISTORY_TABLE", "s4-history", "user_id", "created_at"),
        ):
            monkeypatch.setenv(env_name, tbl_name)
            schema = [{"AttributeName": pk, "KeyType": "HASH"}]
            if sk:
                schema.append({"AttributeName": sk, "KeyType": "RANGE"})
            ddb.create_table(
                TableName=tbl_name,
                KeySchema=schema,
                AttributeDefinitions=[{"AttributeName": item["AttributeName"], "AttributeType": "S"} for item in schema],
                BillingMode="PAY_PER_REQUEST",
            )
        secret = boto3.client("secretsmanager", region_name="ap-southeast-1").create_secret(
            Name="s4/gemini",
            SecretString='{"GEMINI_API_KEY":"s4-secret-key"}',
        )
        monkeypatch.setenv("GEMINI_SECRET_ARN", secret["ARN"])
        table("AWS_CONNECTIONS_TABLE").put_item(
            Item={
                "user_id": "alice",
                "status": "CONNECTED",
                "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole",
                "account_id": "123456789012",
                "external_id": "alice-server-external-id-0000000000",
                "verified_at": "2026-11-08T01:00:00.000000Z",
            }
        )
        yield Store()

    clear_runtime_caches()
    clear_registry_cache()
    gemini._SECRET_CACHE.clear()


# ==============================================================================
# 1. KIỂM THỬ PHÂN QUYỀN (ACCESS DENIED) & CÁCH LY CHÉO TRÊN CẢ 5 LAMBDA
# ==============================================================================


def test_chatbot_secretsmanager_access_denied_returns_403_without_arn_leak(sprint4_cloud, monkeypatch):
    sm_mock = Mock()
    sm_mock.get_secret_value.side_effect = deny_error("secretsmanager:GetSecretValue")
    monkeypatch.setattr(gemini.boto3, "client", Mock(return_value=sm_mock))

    res = chat.handle(api_event("/chat", {"message": "Liệt kê hàm Lambda"}), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 403
    assert body["error_code"] == "GeminiSecretAccessDenied"
    assert "private-role" not in res["body"]


def test_chatbot_dynamodb_and_stepfunctions_access_denied_return_403(sprint4_cloud, monkeypatch):
    real_resource = boto3.resource
    # 1. Thiếu quyền đọc SessionState
    denied_table = Mock()
    denied_table.get_item.side_effect = deny_error("dynamodb:GetItem")
    denied_res = Mock()
    denied_res.Table.return_value = denied_table
    monkeypatch.setattr("backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res))

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
    res_sfn = chat.handle(api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}), None)
    assert res_sfn["statusCode"] == 403
    assert json.loads(res_sfn["body"])["error_code"] == "AccessDenied"


def test_dispatcher_dynamodb_and_sts_access_denied_never_invoke_target_service(sprint4_cloud, monkeypatch):
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
    sts_denied.assume_role.side_effect = deny_error("sts:AssumeRole", code="AccessDenied")
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
    monkeypatch.setattr("backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res))
    clear_runtime_caches()

    out_store = dispatch.handle({"user_id": "alice", "action_id": "act_other"}, None)
    assert out_store["error_code"] == "AccessDenied"
    exec_spy.assert_not_called()


def test_customer_role_missing_permission_propagates_through_dispatcher_formatter_and_history(sprint4_cloud, monkeypatch):
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
    svc.client.update_function_configuration.side_effect = deny_error("lambda:UpdateFunctionConfiguration")

    monkeypatch.setattr(
        dispatch,
        "_execute",
        lambda item, _creds: svc.update_function_resources(**item["parameters"]),
    )
    monkeypatch.setattr(formatter.gemini, "complete", Mock(return_value={"content": "Role đích thiếu quyền cập nhật cấu hình Lambda."}))
    monkeypatch.setattr(formatter.appsync, "publish", Mock())

    dispatched = dispatch.handle({"user_id": "alice", "action_id": action["action_id"]}, None)
    assert dispatched["result"]["error_code"] == "AccessDeniedException"
    assert dispatched["result"]["required_permission"] == "lambda:UpdateFunctionConfiguration"

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
        api_event("/history", owner="bob", method="GET", query={"created_at": action["created_at"]}),
        None,
    )
    assert bob_res["statusCode"] == 404


def test_formatter_secret_appsync_and_dynamodb_permission_denials(sprint4_cloud, monkeypatch):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_fmt",
        registry()["list_lambda_functions"],
        {"max_items": 5},
        "ap-southeast-1",
    )
    envelope = dispatch.envelope(action, {"status": "SUCCESS", "message": "Tìm thấy 2 hàm.", "data": {"count": 2}})

    # 1. Formatter mất quyền đọc Secrets Manager -> tự động dùng fallback, vẫn ghi lịch sử
    sm_denied = Mock()
    sm_denied.get_secret_value.side_effect = deny_error("secretsmanager:GetSecretValue")
    monkeypatch.setattr(gemini.boto3, "client", Mock(return_value=sm_denied))

    # 2. Đồng thời AppSync trả HTTP 403 AccessDenied -> publication_status = FAILED nhưng không làm mất lịch sử
    monkeypatch.setattr(
        appsync.urllib.request,
        "urlopen",
        Mock(side_effect=urllib.error.HTTPError(
            "https://satibot.appsync-api.ap-southeast-1.amazonaws.com/graphql",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"message":"AccessDeniedException"}'),
        )),
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
    monkeypatch.setattr("backend.shared.workflow_store.boto3.resource", Mock(return_value=denied_res))
    clear_runtime_caches()

    envelope_2 = {**envelope, "action_id": "act_2", "created_at": "2026-11-08T09:00:00.000000Z"}
    out_denied = formatter.handle(envelope_2, None)
    assert out_denied["error_code"] == "AccessDenied"
    assert "private-role" not in json.dumps(out_denied)


def test_connector_and_history_dynamodb_access_denied_and_transport_errors(sprint4_cloud, monkeypatch):
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

    assert history.lambda_handler(api_event("/history", method="GET"), None)["statusCode"] == 502
    assert connector.lambda_handler(api_event("/connections", method="GET"), None)["statusCode"] == 502


# ==============================================================================
# 2. TỐI ƯU COLD-START, WARM CACHE & ĐÓNG GÓI GỌN NHẸ THEO LAMBDA
# ==============================================================================


def test_runtime_warm_cache_hits_region_isolation_and_secret_redaction(monkeypatch):
    clear_runtime_caches()
    client_factory = Mock(side_effect=lambda svc, **kw: {"service": svc, "kw": kw, "token": object()})
    table_obj = Mock()
    res_obj = Mock()
    res_obj.Table.return_value = table_obj
    res_factory = Mock(return_value=res_obj)
    session_factory = Mock(side_effect=lambda: object())

    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIASECRETKEY123456")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "very-secret-session-token")

    c1 = cached_boto_client(client_factory, "sts")
    c2 = cached_boto_client(client_factory, "sts")
    assert c1 is c2
    assert client_factory.call_count == 1

    # Đổi region tạo client riêng biệt
    c_east = cached_boto_client(client_factory, "sts", region_name="us-east-1")
    assert c_east is not c1
    assert client_factory.call_count == 2

    t1 = cached_dynamodb_table(res_factory, "SessionState")
    t2 = cached_dynamodb_table(res_factory, "SessionState")
    assert t1 is t2
    assert res_factory.call_count == 1

    s1 = cached_boto_session(session_factory)
    s2 = cached_boto_session(session_factory)
    assert s1 is s2
    assert session_factory.call_count == 1

    stats = runtime_cache_stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 4
    assert stats["clients"] == 2
    assert stats["tables"] == 1
    assert stats["sessions"] == 1

    # Đảm bảo khóa cache không lưu chuỗi AWS_ACCESS_KEY_ID hay SessionToken thô
    from backend.shared import runtime as rt_mod
    serialized_keys = repr(list(rt_mod._CLIENT_CACHE.keys()))
    assert "ASIASECRETKEY123456" not in serialized_keys
    assert "very-secret-session-token" not in serialized_keys
    assert is_access_denied(ValueError("not client error")) is False


def test_tool_registry_validator_cache_and_auto_invalidation(monkeypatch):
    clear_registry_cache()
    tools_first = llm_tools()
    assert registry_cache_stats()["misses"] == 1
    assert registry_cache_stats()["hits"] == 0

    validate_call("list_lambda_functions", {"max_items": 10})
    tools_second = llm_tools()
    assert tools_first == tools_second
    assert registry_cache_stats()["misses"] == 1
    assert registry_cache_stats()["hits"] == 2

    # 1. Sửa object trả về từ registry() hoặc llm_tools() không làm hỏng bản cache nội bộ
    mutated_reg = registry()
    mutated_reg["list_lambda_functions"]["parameters"]["properties"]["forged"] = {"type": "string"}
    mutated_llm = llm_tools()
    mutated_llm[0]["function"]["name"] = "tampered_name"
    assert "forged" not in registry()["list_lambda_functions"]["parameters"]["properties"]
    assert llm_tools()[0]["function"]["name"] != "tampered_name"

    # 2. Sửa nội dung schema tại chỗ (giữ nguyên id và độ dài list) vẫn tự động làm mới validator
    from backend.functions.dispatcher.services import lambda_service
    inplace_list = deepcopy(list(lambda_service.LAMBDA_TOOL_SCHEMAS))
    monkeypatch.setattr(lambda_service, "LAMBDA_TOOL_SCHEMAS", inplace_list)
    clear_registry_cache()
    registry()
    assert registry_cache_stats()["misses"] == 1
    inplace_list[0]["description"] = "Updated description in-place"
    assert registry()["list_lambda_functions"]["description"] == "Updated description in-place"
    assert registry_cache_stats()["misses"] == 2


def test_runtime_cache_distinguishes_different_botocore_configs_on_default_tag():
    clear_runtime_caches()
    factory = Mock(side_effect=lambda svc, **kw: {"svc": svc, "cfg": kw.get("config"), "obj": object()})
    fast = cached_boto_client(factory, "sts", config=Config(connect_timeout=2, read_timeout=3))
    slow = cached_boto_client(factory, "sts", config=Config(connect_timeout=10, read_timeout=25))
    fast_again = cached_boto_client(factory, "sts", config=Config(connect_timeout=2, read_timeout=3))
    assert fast is not slow
    assert fast is fast_again
    assert factory.call_count == 2


def test_slim_per_lambda_packaging_and_coldstart_benchmark_suite(tmp_path):
    package(tmp_path, with_dependencies=False)
    for slim_name in ("formatter", "connector", "history"):
        with zipfile.ZipFile(tmp_path / f"{slim_name}.zip") as zf:
            names = set(zf.namelist())
            assert f"backend/functions/{slim_name}/index.py" in names
            assert "backend/shared/tool_registry.py" not in names
    for rich_name in ("chatbot", "dispatcher"):
        with zipfile.ZipFile(tmp_path / f"{rich_name}.zip") as zf:
            assert "backend/shared/tool_registry.py" in set(zf.namelist())

    local_metrics = benchmark_local_dispatcher(iterations=3)
    reg_metrics = benchmark_tool_registry(iterations=4)
    cloud_metrics = benchmark_cloud_lambdas(iterations=3)
    assert local_metrics["cold_ms"] > 0 and local_metrics["warm_avg_ms"] >= 0
    assert reg_metrics["cache_stats"]["hits"] >= 3
    assert set(cloud_metrics) == {"chatbot", "dispatcher", "formatter", "connector", "history"}


# ==============================================================================
# 3. KIỂM THỬ CHẾ ĐỘ LỖI GEMINI, CONNECTOR, HISTORY & CHỐNG XÁC NHẬN LẶP
# ==============================================================================


def test_gemini_error_modes_non_retryable_timeout_and_malformed_payload(sprint4_cloud, monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", Mock())

    # 1. Lỗi HTTP 401 không thử lại vô ích
    http_401 = Mock(side_effect=urllib.error.HTTPError(gemini.ENDPOINT, 401, "Unauthorized", {}, io.BytesIO(b"bad")))
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
    bad_reply.read.return_value = b'{"choices":[{"message":{"content":["not-a-string"]}}]}'
    monkeypatch.setattr(gemini.urllib.request, "urlopen", Mock(return_value=bad_reply))
    with pytest.raises(BackendError) as err_invalid:
        gemini.complete([{"role": "user", "content": "hi"}])
    assert err_invalid.value.code == "InvalidGeminiResponse"


def test_formatter_rejects_unexpected_tool_calls_from_gemini_and_uses_fallback(sprint4_cloud, monkeypatch):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_inj",
        registry()["list_lambda_functions"],
        {"max_items": 5},
        "ap-southeast-1",
    )
    envelope = dispatch.envelope(
        action,
        {"status": "SUCCESS", "message": "Bản ghi gốc an toàn.", "data": {"functions": ["fn-a"]}},
    )
    # Giả lập prompt injection khiến Gemini cố trả về tool_calls trong bước Formatter
    monkeypatch.setattr(
        formatter.gemini,
        "complete",
        Mock(return_value={"content": "Gọi xóa hàm", "tool_calls": [{"function": {"name": "set_emergency_kill_switch"}}]}),
    )
    monkeypatch.setattr(formatter.appsync, "publish", Mock())

    out = formatter.handle(envelope, None)
    assert out["formatter_fallback"] is True
    assert out["message"] == "Bản ghi gốc an toàn."
    assert out["status"] == "SUCCESS"


def test_connector_status_projection_onboarding_order_and_validation(sprint4_cloud):
    # GET /connections chỉ trả trường công khai, tuyệt đối không trả external_id
    get_res = connector.lambda_handler(api_event("/api/connections", owner="alice", method="GET"), None)
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
    bad_arn = connector.lambda_handler(api_event("/connections/verify", {"role_arn": "not-an-arn"}), None)
    assert bad_arn["statusCode"] == 400
    assert connector.lambda_handler(api_event("/connections/verify", method="GET"), None)["statusCode"] == 405
    assert connector.lambda_handler(api_event("/connections/unknown", method="POST"), None)["statusCode"] == 404


def test_history_query_validation_filters_and_method_guard(sprint4_cloud):
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
    assert history.lambda_handler(api_event("/history", method="POST"), None)["statusCode"] == 405

    # Các truy vấn sai biên: limit ngoài [1..100], from > to, timestamp sai định dạng, cursor giả mạo
    for bad_query in (
        {"limit": "0"},
        {"limit": "101"},
        {"from": "2026-11-08T06:00:00Z", "to": "2026-11-08T05:00:00Z"},
        {"created_at": "2026/11/08 05:00:00"},
        {"cursor": base64.urlsafe_b64encode(b'{"user_id":"bob","created_at":"2026-11-08T05:00:00Z"}').decode()},
    ):
        res = history.lambda_handler(api_event("/history", method="GET", query=bad_query), None)
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


def test_duplicate_confirmations_idempotent_and_completed_short_circuit(sprint4_cloud, monkeypatch):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_dup",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 512},
        "ap-southeast-1",
    )
    sfn_mock = Mock()
    sfn_mock.start_execution.side_effect = [
        {"executionArn": "arn:aws:states:ap-southeast-1:123456789012:execution:satibot:1"},
        ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "already running"}}, "StartExecution"),
    ]
    monkeypatch.setattr(chat.boto3, "client", Mock(return_value=sfn_mock))

    # Gửi xác nhận lặp 2 lần liên tiếp -> cả hai đều trả 202 ACCEPTED an toàn, không tạo execution trùng
    first = chat.handle(api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}), None)
    second = chat.handle(api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}), None)
    assert first["statusCode"] == 202
    assert second["statusCode"] == 202
    assert sfn_mock.start_execution.call_count == 2

    # Khi tác vụ đã COMPLETED, xác nhận lặp trả ngay kết quả đã lưu (200) mà không gọi Step Functions nữa
    claimed = sprint4_cloud.get_action("alice", action["action_id"])
    sprint4_cloud.claim(claimed)
    sprint4_cloud.finish(claimed, {"status": "SUCCESS", "message": "Đã cập nhật RAM 512MB.", "data": {"memory_mb": 512}})

    third = chat.handle(api_event("/confirm", {"confirmed": True, "action_id": action["action_id"]}), None)
    assert third["statusCode"] == 200
    assert json.loads(third["body"])["data"]["memory_mb"] == 512
    assert sfn_mock.start_execution.call_count == 2


def test_stored_action_schema_drift_and_formatter_history_key_collision(sprint4_cloud, monkeypatch):
    action = sprint4_cloud.create_action(
        "alice",
        "ses_drift",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    sprint4_cloud.approve("alice", action["action_id"])
    # Giả lập bản ghi trong DB bị lệch action so với schema hiện hành
    table("PENDING_ACTION_TABLE").update_item(
        Key={"action_id": action["action_id"]},
        UpdateExpression="SET #a = :tampered",
        ExpressionAttributeNames={"#a": "action"},
        ExpressionAttributeValues={":tampered": "delete_everything"},
    )
    exec_spy = Mock()
    monkeypatch.setattr(dispatch, "_execute", exec_spy)
    drift_out = dispatch.handle({"user_id": "alice", "action_id": action["action_id"]}, None)
    assert drift_out["error_code"] == "InvalidStoredAction"
    exec_spy.assert_not_called()

    # Giả lập xung đột khóa lịch sử (cùng user_id + created_at nhưng khác action_id)
    monkeypatch.setattr(formatter.gemini, "complete", Mock(return_value={"content": "OK"}))
    monkeypatch.setattr(formatter.appsync, "publish", Mock())
    first_env = dispatch.envelope(action, {"status": "SUCCESS", "message": "OK", "data": {}})
    assert formatter.handle(first_env, None)["status"] == "SUCCESS"
    colliding_env = {**first_env, "action_id": "act_different_collision"}
    with pytest.raises(BackendError) as collision_err:
        formatter.handle(colliding_env, None)
    assert collision_err.value.code == "HistoryConflict"


# ==============================================================================
# 4. KIỂM THỬ HẠ TẦNG CDK CANARY/ROLLBACK & PIPELINE PHÊ DUYỆT THỦ CÔNG
# ==============================================================================


def test_cdk_canary_stack_pipeline_and_alarm_rollback_simulation(tmp_path):
    # Đảm bảo dist/backend có đủ 5 file zip để cdk synth độc lập trong mọi môi trường test
    dist_backend = ROOT / "dist" / "backend"
    if not all((dist_backend / f"{name}.zip").exists() for name in ("chatbot", "dispatcher", "formatter", "connector", "history")):
        package(dist_backend, with_dependencies=False)

    cdk_out = tmp_path / "cdk.out"
    subprocess.run(
        [sys.executable, str(ROOT / "infrastructure" / "backend_sprint2_app.py")],
        env={**dict(__import__("os").environ), "CDK_OUTDIR": str(cdk_out), "PRIMARY_REGION": "ap-southeast-1"},
        check=True,
    )
    template_file = cdk_out / "SatiBotBackendSprint2.template.json"
    cdk_summary = verify_cdk_canary_template(template_file)
    assert cdk_summary["functions"] == 5
    assert cdk_summary["versions"] == 5
    assert cdk_summary["live_aliases"] == 5
    assert cdk_summary["deployment_groups"] == 5
    assert cdk_summary["default_strategy"] == "CodeDeployDefault.LambdaCanary10Percent5Minutes"

    pipeline_summary = verify_pipeline_template(ROOT / "ci" / "pipeline.template.json")
    assert pipeline_summary["stages"] == ["Source", "BuildTestSynth", "ManualApproval", "DeployBackend"]
    assert pipeline_summary["deploy_actions"] == ["PublishCdkAssets", "CreateReplaceChangeSet", "ExecuteChangeSet"]

    sim_summary = simulate_canary_and_rollback()
    assert sim_summary["canary_promotion_v1_to_v2"]["deployment_status"] == "SUCCEEDED"
    assert sim_summary["alarm_rollback_v2_to_v3"]["deployment_status"] == "ROLLED_BACK"
    assert sim_summary["alarm_rollback_v2_to_v3"]["rollback_trigger"] == "DEPLOYMENT_STOP_ON_ALARM"

    # Kiểm tra chi tiết trọng số lưu lượng 90%/10% trong giai đoạn Canary trước khi rollback
    sim = CanaryAliasSimulator(function_name="SatiBotBackendSprint2-dispatcherFunction")
    rollback_case = sim.deploy_canary("2", simulated_error_rate=0.25, error_threshold=0.05)
    assert rollback_case["timeline"][0]["weights"] == {"1": 0.9, "2": 0.1}
    assert rollback_case["final_weights"] == {"1": 1.0}


def test_verifier_rejects_missing_alarms_missing_config_version_or_oversized_overrides(tmp_path):
    dist_backend = ROOT / "dist" / "backend"
    if not all((dist_backend / f"{name}.zip").exists() for name in ("chatbot", "dispatcher", "formatter", "connector", "history")):
        package(dist_backend, with_dependencies=False)

    cdk_out = tmp_path / "cdk_tamper"
    subprocess.run(
        [sys.executable, str(ROOT / "infrastructure" / "backend_sprint2_app.py")],
        env={**dict(__import__("os").environ), "CDK_OUTDIR": str(cdk_out), "PRIMARY_REGION": "ap-southeast-1"},
        check=True,
    )
    base_tpl = json.loads((cdk_out / "SatiBotBackendSprint2.template.json").read_text(encoding="utf-8"))

    # 1. Xóa AlarmConfiguration khỏi DeploymentGroup -> verifier bắt buộc báo lỗi
    no_alarm_tpl = deepcopy(base_tpl)
    for res in no_alarm_tpl["Resources"].values():
        if res.get("Type") == "AWS::CodeDeploy::DeploymentGroup":
            res["Properties"].pop("AlarmConfiguration", None)
    tampered_alarm_file = tmp_path / "no_alarm.template.json"
    tampered_alarm_file.write_text(json.dumps(no_alarm_tpl), encoding="utf-8")
    with pytest.raises(AssertionError, match="AlarmConfiguration"):
        verify_cdk_canary_template(tampered_alarm_file)

    # 2. Xóa ConfigurationVersion khỏi AWS::Lambda::Version Description -> verifier bắt buộc báo lỗi
    no_cfg_ver_tpl = deepcopy(base_tpl)
    for res in no_cfg_ver_tpl["Resources"].values():
        if res.get("Type") == "AWS::Lambda::Version":
            res["Properties"]["Description"] = "static-description"
    tampered_ver_file = tmp_path / "no_cfg_ver.template.json"
    tampered_ver_file.write_text(json.dumps(no_cfg_ver_tpl), encoding="utf-8")
    with pytest.raises(AssertionError, match="ConfigurationVersion"):
        verify_cdk_canary_template(tampered_ver_file)

    # 3. ParameterOverrides vượt 1000 byte trong pipeline -> verifier bắt buộc báo lỗi
    bad_pipeline = json.loads((ROOT / "ci" / "pipeline.template.json").read_text(encoding="utf-8"))
    bad_pipeline["Resources"]["Pipeline"]["Properties"]["Stages"][3]["Actions"][1]["Configuration"]["ParameterOverrides"] = {
        "Fn::Sub": "{" + '"Padding":"' + ("x" * 1050) + '"}'
    }
    bad_pipeline_file = tmp_path / "bad_pipeline.json"
    bad_pipeline_file.write_text(json.dumps(bad_pipeline), encoding="utf-8")
    with pytest.raises(AssertionError, match="1000 byte"):
        verify_pipeline_template(bad_pipeline_file)


def test_deterministic_zip_hashes_and_subprocess_coldstart(tmp_path):
    dir_1 = tmp_path / "build1"
    dir_2 = tmp_path / "build2"
    package(dir_1, with_dependencies=False)
    time.sleep(1.1)
    package(dir_2, with_dependencies=False)
    for name in ("chatbot", "dispatcher", "formatter", "connector", "history"):
        h1 = hashlib.sha256((dir_1 / f"{name}.zip").read_bytes()).hexdigest()
        h2 = hashlib.sha256((dir_2 / f"{name}.zip").read_bytes()).hexdigest()
        assert h1 == h2

    # Đo subprocess cold-start trên các gói đã đóng kèm dependency ở dist/backend nếu có
    target = ROOT / "dist" / "backend"
    if all((target / f"{n}.zip").exists() for n in ("chatbot", "dispatcher", "formatter", "connector", "history")):
        cold_report = benchmark_subprocess_coldstart(target, warm_runs=2)
        assert set(cold_report) == {"chatbot", "dispatcher", "formatter", "connector", "history"}
        assert all(item["import_init_ms"] > 0 for item in cold_report.values())


def test_dispatcher_sts_timeout_budget_and_platform_fault_scoping(sprint4_cloud, monkeypatch):
    real_client = boto3.client
    # 1. Timeout STS phải nhỏ hơn nhiều so với ngưỡng 60s của Dispatcher
    assert dispatch._STS_CONFIG.connect_timeout <= 5
    assert dispatch._STS_CONFIG.read_timeout <= 10

    # 2. Lỗi đầu vào người dùng hoặc lỗi STS từ phía tài khoản khách hàng KHÔNG phát PlatformFault
    clear_runtime_caches()
    assert chat.handle(api_event("/chat", {"message": ""}), None)["statusCode"] == 400
    assert platform_fault_counts() == {}

    action = sprint4_cloud.create_action(
        "alice",
        "ses_budget",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    sprint4_cloud.approve("alice", action["action_id"])
    sts_customer_denied = Mock()
    sts_customer_denied.assume_role.side_effect = deny_error("sts:AssumeRole", code="AccessDenied")
    monkeypatch.setattr(dispatch.boto3, "client", Mock(return_value=sts_customer_denied))
    res_cust = dispatch.handle({"user_id": "alice", "action_id": action["action_id"]}, None)
    assert res_cust["result"]["error_code"] == "AccessDenied"
    assert platform_fault_counts() == {}

    # 3. Khi thời gian còn lại của Lambda dưới 5 giây trước lúc gọi dịch vụ đích, dừng an toàn và ghi trạng thái COMPLETED
    monkeypatch.setattr(dispatch.boto3, "client", real_client)
    clear_runtime_caches()
    action2 = sprint4_cloud.create_action(
        "alice",
        "ses_budget_2",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    sprint4_cloud.approve("alice", action2["action_id"])
    low_budget_ctx = Mock()
    low_budget_ctx.get_remaining_time_in_millis.return_value = 2500
    exec_spy = Mock()
    monkeypatch.setattr(dispatch, "_execute", exec_spy)
    res_budget = dispatch.handle({"user_id": "alice", "action_id": action2["action_id"]}, low_budget_ctx)
    assert res_budget["result"]["error_code"] == "DispatcherTimeoutBudgetExceeded"
    assert sprint4_cloud.get_action("alice", action2["action_id"])["status"] == "COMPLETED"
    exec_spy.assert_not_called()


def test_live_benchmark_enforces_jwt_and_tracks_async_workflow_completion(monkeypatch):
    monkeypatch.delenv("SATIBOT_BENCHMARK_JWT", raising=False)
    with pytest.raises(ValueError, match="JWT"):
        benchmark_live_endpoint("https://example.execute-api.ap-southeast-1.amazonaws.com/api/chat", iterations=1)

    seen_headers = []

    def _fake_urlopen(req, timeout=20):
        seen_headers.append(dict(req.headers))
        resp = Mock()
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=False)
        if req.full_url.endswith("/chat"):
            resp.status = 202
            resp.read.return_value = b'{"status":"ACCEPTED","action_id":"act_live_1"}'
        else:
            resp.status = 200
            resp.read.return_value = b'{"items":[{"action_id":"act_live_1","status":"SUCCESS"}]}'
        return resp

    monkeypatch.setattr("scripts.benchmark_coldstart.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("scripts.benchmark_coldstart.time.sleep", lambda *_a: None)
    metrics = benchmark_live_endpoint(
        "https://example.execute-api.ap-southeast-1.amazonaws.com/api/chat",
        jwt_token="eyJ.demo.token",
        iterations=2,
    )
    assert metrics["accept_initial_ms"] >= 0
    assert "workflow_e2e_avg_ms" in metrics
    assert all(h.get("Authorization") == "Bearer eyJ.demo.token" for h in seen_headers)
