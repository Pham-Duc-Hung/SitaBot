"""
Author: Trịnh Thiên Quang
Kiểm thử tính liên tục ngữ cảnh tài nguyên và cập nhật mã nguồn theo hội thoại.
"""
import json
from unittest.mock import Mock, patch

from backend.functions.chatbot.agent_executor import AgentExecutor
from backend.functions.chatbot.index import lambda_handler, format_tools_for_llm
from backend.functions.dispatcher.services.lambda_service import LAMBDA_TOOL_SCHEMAS


def tool(name, **arguments):
    return {"content": None, "tool_calls": [{
        "id": "call_" + name, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def executor(llm, dispatch):
    return AgentExecutor(llm, dispatch, format_tools_for_llm(LAMBDA_TOOL_SCHEMAS), LAMBDA_TOOL_SCHEMAS, max_iterations=4)


def test_create_then_edit_after_history_window_preserves_resource_and_current_source():
    original = "def lambda_handler(event, context):\n    return 'old shop'\n"
    updated = original.replace("old shop", "blue shop")
    llm = Mock(side_effect=[
        tool("create_lambda_function", function_name="my-shop", code_content=original),
        {"content": "Website ready", "tool_calls": None},
        tool("get_lambda_source_code", function_name="my-shop"),
        tool("update_lambda_code", function_name="my-shop", code_content=updated),
    ])
    dispatch = Mock(side_effect=[
        {"status": "SUCCESS", "data": {"function_name": "my-shop"}, "message": "created"},
        {"status": "SUCCESS", "data": {"function_name": "my-shop", "main_code": original, "files": []}},
    ])
    with patch.dict("os.environ", {"LLM_API_KEY": "test-key"}), \
         patch("backend.functions.chatbot.index.call_llm_api", llm), \
         patch("backend.functions.chatbot.index._dispatch_action", dispatch):
        first = lambda_handler({"message": "Tạo website bán hàng"}, None)
        completed = lambda_handler({"confirmed": True, "tool_name": first["tool_name"],
            "parameters": first["parameters"], "agent_state": first["agent_state"]}, None)
        assert completed["workspace"]["active_function"] == "my-shop"
        # The original creation message is gone; resource memory must survive.
        edited = lambda_handler({"message": "Đổi màu giao diện thành xanh", "workspace": completed["workspace"],
            "history": [{"role": "assistant", "content": "earlier discussion"}] * 10}, None)
    assert edited["tool_name"] == "update_lambda_code"
    assert edited["parameters"]["function_name"] == "my-shop"
    assert edited["parameters"]["code_content"] == updated
    assert dispatch.call_count == 2  # update still waits for approval
    assert "my-shop" in edited["agent_state"]["messages"][0]["content"]


def test_update_without_source_or_with_identical_source_never_requests_approval():
    source = "def lambda_handler(e,c): return 'shop'"
    for read_source in (False, True):
        responses = []
        if read_source:
            responses.append(tool("get_lambda_source_code", function_name="my-shop"))
        responses += [tool("update_lambda_code", function_name="my-shop", code_content=source),
                      {"error_code": "TEST_STOP", "content": "stop"}]
        llm = Mock(side_effect=responses)
        dispatch = Mock(return_value={"status": "SUCCESS", "data": {
            "function_name": "my-shop", "main_code": source, "files": []}})
        result = executor(llm, dispatch).run("Sửa giao diện", "system")
        assert result["status"] == "ERROR"
        assert any(s["action"] == "update_lambda_code" for s in result["steps"])
        assert dispatch.call_count == int(read_source)


def test_source_longer_than_old_limit_is_not_silently_truncated():
    code = "#" + "x" * 15000 + "\ndef lambda_handler(e,c): return 'tail'"
    llm = Mock(side_effect=[tool("get_lambda_source_code", function_name="my-shop"),
        tool("update_lambda_code", function_name="my-shop", code_content=code.replace("tail", "updated"))])
    dispatch = Mock(return_value={"status": "SUCCESS", "data": {
        "function_name": "my-shop", "main_code": code, "files": []}})
    result = executor(llm, dispatch).run("Sửa website", "system")
    observations = [m for m in result["agent_state"]["messages"] if m.get("name") == "get_lambda_source_code"]
    assert json.loads(observations[0]["content"])["main_code"] == code
    assert result["tool_name"] == "update_lambda_code"


def test_followup_recovery_uses_latest_goal_and_does_not_redeploy_old_code():
    llm = Mock(return_value={"content": "Need details", "tool_calls": None})
    agent = executor(llm, Mock())
    with patch.object(agent, "_recover_create_call_from_text", return_value=None) as recover:
        agent.run("Sửa website thành shop giày", "system", history=[
            {"role": "user", "content": "Tạo website bán điện thoại"}])
    assert recover.call_args_list[0].args[1] == "Sửa website thành shop giày"
    assert agent._recover_create_call_from_text("No new code", "Sửa lại", messages=[
        {"role": "assistant", "content": "```python\ndef lambda_handler(e,c): return 'old'\n```"}]) is None
