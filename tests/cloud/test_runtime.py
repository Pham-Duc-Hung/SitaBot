"""
Author: Trịnh Thiên Quang
Kiểm thử cache, schema công cụ và giới hạn thời gian thực thi.
"""

from copy import deepcopy
from unittest.mock import Mock
import boto3
from botocore.config import Config
from backend.functions.chatbot import cloud_handler as chat
from backend.functions.dispatcher import cloud_handler as dispatch
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
from .helpers import deny_error, api_event


def test_runtime_warm_cache_hits_region_isolation_and_secret_redaction(monkeypatch):
    clear_runtime_caches()
    client_factory = Mock(
        side_effect=lambda svc, **kw: {"service": svc, "kw": kw, "token": object()}
    )
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
    mutated_reg["list_lambda_functions"]["parameters"]["properties"]["forged"] = {
        "type": "string"
    }
    mutated_llm = llm_tools()
    mutated_llm[0]["function"]["name"] = "tampered_name"
    assert (
        "forged" not in registry()["list_lambda_functions"]["parameters"]["properties"]
    )
    assert llm_tools()[0]["function"]["name"] != "tampered_name"

    # 2. Sửa nội dung schema tại chỗ (giữ nguyên id và độ dài list) vẫn tự động làm mới validator
    from backend.functions.dispatcher.services import lambda_service

    inplace_list = deepcopy(list(lambda_service.LAMBDA_TOOL_SCHEMAS))
    monkeypatch.setattr(lambda_service, "LAMBDA_TOOL_SCHEMAS", inplace_list)
    clear_registry_cache()
    registry()
    assert registry_cache_stats()["misses"] == 1
    inplace_list[0]["description"] = "Updated description in-place"
    assert (
        registry()["list_lambda_functions"]["description"]
        == "Updated description in-place"
    )
    assert registry_cache_stats()["misses"] == 2


def test_runtime_cache_distinguishes_different_botocore_configs_on_default_tag():
    clear_runtime_caches()
    factory = Mock(
        side_effect=lambda svc, **kw: {
            "svc": svc,
            "cfg": kw.get("config"),
            "obj": object(),
        }
    )
    fast = cached_boto_client(
        factory, "sts", config=Config(connect_timeout=2, read_timeout=3)
    )
    slow = cached_boto_client(
        factory, "sts", config=Config(connect_timeout=10, read_timeout=25)
    )
    fast_again = cached_boto_client(
        factory, "sts", config=Config(connect_timeout=2, read_timeout=3)
    )
    assert fast is not slow
    assert fast is fast_again
    assert factory.call_count == 2


def test_dispatcher_sts_timeout_budget_and_platform_fault_scoping(
    cloud_store, monkeypatch
):
    real_client = boto3.client
    # 1. Timeout STS phải nhỏ hơn nhiều so với ngưỡng 60s của Dispatcher
    assert dispatch._STS_CONFIG.connect_timeout <= 5
    assert dispatch._STS_CONFIG.read_timeout <= 10

    # 2. Lỗi đầu vào người dùng hoặc lỗi STS từ phía tài khoản khách hàng KHÔNG phát PlatformFault
    clear_runtime_caches()
    assert chat.handle(api_event("/chat", {"message": ""}), None)["statusCode"] == 400
    assert platform_fault_counts() == {}

    action = cloud_store.create_action(
        "alice",
        "ses_budget",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    cloud_store.approve("alice", action["action_id"])
    sts_customer_denied = Mock()
    sts_customer_denied.assume_role.side_effect = deny_error(
        "sts:AssumeRole", code="AccessDenied"
    )
    monkeypatch.setattr(
        dispatch.boto3, "client", Mock(return_value=sts_customer_denied)
    )
    res_cust = dispatch.handle(
        {"user_id": "alice", "action_id": action["action_id"]}, None
    )
    assert res_cust["result"]["error_code"] == "AccessDenied"
    assert platform_fault_counts() == {}

    # 3. Khi thời gian còn lại của Lambda dưới 5 giây trước lúc gọi dịch vụ đích, dừng an toàn và ghi trạng thái COMPLETED
    monkeypatch.setattr(dispatch.boto3, "client", real_client)
    clear_runtime_caches()
    action2 = cloud_store.create_action(
        "alice",
        "ses_budget_2",
        registry()["update_function_resources"],
        {"function_name": "demo", "memory_mb": 256},
        "ap-southeast-1",
    )
    cloud_store.approve("alice", action2["action_id"])
    low_budget_ctx = Mock()
    low_budget_ctx.get_remaining_time_in_millis.return_value = 2500
    exec_spy = Mock()
    monkeypatch.setattr(dispatch, "_execute", exec_spy)
    res_budget = dispatch.handle(
        {"user_id": "alice", "action_id": action2["action_id"]}, low_budget_ctx
    )
    assert res_budget["result"]["error_code"] == "DispatcherTimeoutBudgetExceeded"
    assert (
        cloud_store.get_action("alice", action2["action_id"])["status"] == "COMPLETED"
    )
    exec_spy.assert_not_called()
