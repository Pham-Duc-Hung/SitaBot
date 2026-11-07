"""Exercise the boundary from S3 tool calls to the shared Dispatcher contract."""

import json
from unittest.mock import Mock

from backend.functions.chatbot import index as chatbot
from backend.functions.chatbot.agent_executor import AgentExecutor
from backend.functions.chatbot.tools.s3_tools import S3_TOOL_SCHEMAS
from backend.functions.dispatcher import index as dispatcher


def tool_call(name, arguments):
    return {
        "id": "call_s3",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_s3_schemas_are_exposed_to_llm_with_their_input_schema():
    schemas = chatbot._get_all_tool_schemas()
    s3_schema = next(s for s in schemas if s["name"] == "s3_create_bucket")
    llm_tool = next(
        t for t in chatbot.format_tools_for_llm(schemas)
        if t["function"]["name"] == "s3_create_bucket"
    )
    assert s3_schema["service"] == "s3"
    assert s3_schema["input_schema"]["properties"]["region"]["default"] == "ap-southeast-1"
    assert llm_tool["function"]["parameters"] == s3_schema["input_schema"]
    assert "bucket_name" in llm_tool["function"]["parameters"]["required"]


def test_read_only_s3_chat_call_reaches_dispatcher_with_flat_parameters(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setattr(chatbot, "call_llm_api", lambda **_: {
        "content": None,
        "tool_calls": [tool_call("s3_get_storage_analytics", {"bucket_name": "demo-bucket"})],
    })
    execute = Mock(return_value={
        "ok": True, "action": "get_storage_analytics", "data": {"current_object_count": 2}
    })
    monkeypatch.setattr(dispatcher.s3_service, "execute", execute)

    response = chatbot.lambda_handler({"message": "Thống kê bucket demo-bucket"}, None)

    assert response["status"] == "SUCCESS"
    assert response["service"] == "s3"
    assert response["data"]["current_object_count"] == 2
    assert execute.call_args.args[1] == {
        "bucket_name": "demo-bucket",
        "action": "get_storage_analytics",
        "region": "ap-southeast-1",
    }


def test_s3_create_waits_for_confirmation_then_maps_to_native_action(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setattr(chatbot, "call_llm_api", lambda **_: {
        "content": None,
        "tool_calls": [tool_call("s3_create_bucket", {"bucket_name": "demo-bucket"})],
    })
    execute = Mock(return_value={
        "ok": True, "action": "create_bucket", "data": {"bucket_name": "demo-bucket"}
    })
    monkeypatch.setattr(dispatcher.s3_service, "execute", execute)

    pending = chatbot.lambda_handler({"message": "Tạo bucket demo-bucket"}, None)
    assert pending["status"] == "CONFIRMATION_REQUIRED"
    assert pending["service"] == "s3"
    execute.assert_not_called()

    approved = chatbot.lambda_handler({
        "confirmed": True,
        "service": pending["service"],
        "tool_name": pending["tool_name"],
        "parameters": pending["parameters"],
    }, None)
    assert approved["status"] == "SUCCESS"
    assert approved["service"] == "s3"
    assert execute.call_args.args[1]["action"] == "create_bucket"
    assert execute.call_args.args[1]["bucket_name"] == "demo-bucket"


def test_direct_dispatcher_s3_write_requires_confirmation(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(dispatcher.s3_service, "execute", execute)

    response = dispatcher.lambda_handler({
        "service": "s3",
        "action": "create_bucket",
        "parameters": {"bucket_name": "demo-bucket"},
    }, None)
    assert response["status"] == "CONFIRMATION_REQUIRED"
    execute.assert_not_called()


def test_direct_dispatcher_s3_read_defaults_to_primary_region(monkeypatch):
    execute = Mock(return_value={
        "ok": True, "action": "scan_public_access", "data": {"scanned_count": 0}
    })
    monkeypatch.setattr(dispatcher.s3_service, "execute", execute)

    response = dispatcher.lambda_handler({
        "service": "s3",
        "action": "scan_public_access",
        "parameters": {},
    }, None)

    assert response["status"] == "SUCCESS"
    assert execute.call_args.args[1]["region"] == "ap-southeast-1"


def test_s3_service_errors_reach_chatbot_as_errors(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setattr(chatbot, "call_llm_api", lambda **_: {
        "content": None,
        "tool_calls": [tool_call("s3_get_storage_analytics", {"bucket_name": "demo-bucket"})],
    })
    monkeypatch.setattr(dispatcher.s3_service, "execute", Mock(return_value={
        "ok": False,
        "action": "get_storage_analytics",
        "error": {"code": "AccessDenied", "message": "Không có quyền đọc bucket."},
    }))

    response = chatbot.lambda_handler({"message": "Thống kê bucket demo-bucket"}, None)
    assert response["status"] == "ERROR"
    assert response["message"] == "Không có quyền đọc bucket."


def test_agent_chat_path_executes_read_only_s3_tool(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    llm = Mock(side_effect=[
        {"content": None, "tool_calls": [tool_call("s3_scan_public_access", {})]},
        {"content": "Đã quét các bucket.", "tool_calls": None},
    ])
    monkeypatch.setattr(chatbot, "call_llm_api", llm)
    execute = Mock(return_value={
        "ok": True, "action": "scan_public_access", "data": {"risky_count": 0}
    })
    monkeypatch.setattr(dispatcher.s3_service, "execute", execute)

    response = chatbot.lambda_handler({"message": "Quét bucket S3"}, None)
    assert response["status"] == "SUCCESS"
    assert execute.call_args.args[1]["action"] == "scan_public_access"


def test_agent_executor_routes_s3_reads_and_confirmations_to_s3():
    dispatch = Mock(return_value={"status": "SUCCESS", "message": "Đã quét bucket", "data": {}})
    read_llm = Mock(side_effect=[
        {"content": None, "tool_calls": [tool_call("s3_scan_public_access", {})]},
        {"content": "Đã quét bucket", "tool_calls": None},
    ])
    executor = AgentExecutor(
        read_llm, dispatch, chatbot.format_tools_for_llm(S3_TOOL_SCHEMAS),
        list(S3_TOOL_SCHEMAS), max_iterations=3,
    )
    assert executor.run("Quét bucket S3", "prompt")["status"] == "SUCCESS"
    assert dispatch.call_args.kwargs["service"] == "s3"

    write_llm = Mock(return_value={
        "content": None,
        "tool_calls": [tool_call("s3_create_bucket", {"bucket_name": "demo-bucket"})],
    })
    dispatch.reset_mock()
    executor = AgentExecutor(
        write_llm, dispatch, chatbot.format_tools_for_llm(S3_TOOL_SCHEMAS),
        list(S3_TOOL_SCHEMAS), max_iterations=3,
    )
    pending = executor.run("Tạo bucket demo-bucket", "prompt")
    assert pending["status"] == "CONFIRMATION_REQUIRED"
    assert pending["service"] == "s3"
    dispatch.assert_not_called()
