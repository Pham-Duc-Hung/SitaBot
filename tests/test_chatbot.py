"""
Author: Trịnh Thiên Quang
Kiểm thử Chatbot Lambda và cơ chế xử lý phản hồi Function Calling.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Thêm đường dẫn backend
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.functions.chatbot.index import lambda_handler, format_tools_for_llm, call_llm_api, SYSTEM_PROMPT


class TestChatbotFunctionCalling(unittest.TestCase):
    def setUp(self):
        # Tách biệt unit test khỏi internet và biến môi trường LLM_API_KEY thực
        self.env_patcher = patch.dict(os.environ, {"LLM_API_KEY": ""}, clear=False)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_missing_message_returns_error(self):
        res = lambda_handler({}, None)
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("Thiếu nội dung", res["message"])

    def test_format_tools_for_llm_structure(self):
        mock_schemas = [
            {
                "name": "dummy_tool",
                "description": "Tool mẫu",
                "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}},
            }
        ]
        formatted = format_tools_for_llm(mock_schemas)
        self.assertEqual(len(formatted), 1)
        self.assertEqual(formatted[0]["type"], "function")
        self.assertEqual(formatted[0]["function"]["name"], "dummy_tool")

    def test_chat_greeting_reply(self):
        event = {"message": "Xin chào SatiBot"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "chat_reply")
        self.assertIn("SatiBot", res["message"])

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_selected_model_is_forwarded_to_llm(self, mock_llm):
        mock_llm.return_value = {"content": "Xin chào", "tool_calls": None}

        res = lambda_handler({"message": "hi", "model": "gemini-2.5-flash"}, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(mock_llm.call_args.kwargs["model"], "gemini-2.5-flash")

    def test_rejects_unsupported_model(self):
        res = lambda_handler({"message": "hi", "model": "unknown-model"}, None)

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "InvalidParameters")

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_gemini_31_flash_lite_is_supported(self, mock_llm):
        mock_llm.return_value = {"content": "OK", "tool_calls": None}

        res = lambda_handler({"message": "hi", "model": "gemini-3.1-flash-lite"}, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(mock_llm.call_args.kwargs["model"], "gemini-3.1-flash-lite")

    @patch("backend.functions.chatbot.index._dispatch_action")
    def test_list_lambda_intent_action_executed(self, mock_dispatch):
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "Đã tìm thấy 2 hàm Lambda",
            "data": {"count": 2}
        }
        event = {"message": "Liệt kê danh sách hàm lambda"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        self.assertEqual(res["tool_name"], "list_lambda_functions")
        mock_dispatch.assert_called_once()

    def test_invoke_lambda_intent_requires_confirmation(self):
        event = {"message": "Chạy thử hàm test-lambda với dữ liệu giá 100 và thuế 0.1"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["type"], "action_confirmation")
        self.assertEqual(res["tool_name"], "invoke_lambda_function")
        self.assertIn("action_id", res)

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_delete_lambda_intent_returns_real_confirmation_action(self, mock_llm):
        mock_llm.return_value = {
            "content": "Lambda này sẽ bị xóa vĩnh viễn.",
            "tool_calls": [{
                "id": "call_delete_1",
                "type": "function",
                "function": {
                    "name": "delete_lambda_function",
                    "arguments": json.dumps({"function_name": "sati-store-web"}),
                },
            }],
        }
        event = {"message": "Xóa hộ hàm lambda sati-store-web"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["type"], "action_confirmation")
        self.assertEqual(res["tool_name"], "delete_lambda_function")
        self.assertEqual(res["parameters"]["function_name"], "sati-store-web")

    def test_system_prompt_does_not_require_repeated_self_introduction(self):
        self.assertIn("Không tự giới thiệu ở đầu mỗi câu trả lời", SYSTEM_PROMPT)
        self.assertNotIn('Khi giới thiệu bản thân hoặc chào hỏi, luôn xưng:', SYSTEM_PROMPT)

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_external_llm_function_call_dispatch(self, mock_llm):
        mock_llm.return_value = {
            "content": None,
            "tool_calls": [{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "update_function_resources",
                    "arguments": json.dumps({"function_name": "my-func", "memory_mb": 512}),
                }
            }]
        }
        event = {"message": "Nâng RAM cho hàm my-func lên 512 MB"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["tool_name"], "update_function_resources")
        self.assertEqual(res["parameters"]["memory_mb"], 512)

    @patch("backend.functions.chatbot.index._dispatch_action")
    def test_confirmed_action_execution(self, mock_dispatch):
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "Thao tác đã được thực thi thành công",
            "data": {"function_name": "test-lambda"}
        }
        event = {
            "confirmed": True,
            "service": "lambda",
            "tool_name": "invoke_lambda_function",
            "parameters": {"function_name": "test-lambda"}
        }
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        mock_dispatch.assert_called_once()

    @patch("backend.functions.chatbot.index._dispatch_action")
    def test_recoverable_conflict_returns_clickable_resolution_contract(self, mock_dispatch):
        mock_dispatch.return_value = {
            "status": "USER_ACTION_REQUIRED",
            "message": "Tên Lambda đã được sử dụng. Hãy chọn cách xử lý:",
            "data": {
                "resolution_kind": "recoverable_conflict",
                "resolution_options": [
                    {"id": "update_existing", "label": "Cập nhật hàm hiện có", "changes_aws": True},
                    {"id": "cancel", "label": "Hủy thao tác", "changes_aws": False},
                ],
            },
        }
        event = {
            "confirmed": True,
            "service": "lambda",
            "tool_name": "create_lambda_function",
            "parameters": {
                "function_name": "sati-store-web",
                "code_content": "def lambda_handler(event, context): return {}",
            },
        }
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "USER_ACTION_REQUIRED")
        self.assertEqual(res["type"], "resolution_options")
        self.assertEqual(res["data"]["resolution_kind"], "recoverable_conflict")
        self.assertTrue(res["data"]["resolution_options"][0]["changes_aws"])

    def test_api_gateway_event_wrapper(self):
        event = {"body": json.dumps({"message": "Hello"})}
        res = lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "SUCCESS")

    @patch("backend.functions.chatbot.index.time.sleep")
    @patch("backend.functions.chatbot.index.urllib.request.urlopen")
    def test_llm_connection_error_retries_before_success(self, mock_urlopen, mock_sleep):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok", "tool_calls": None}}]
        }).encode()
        response.__enter__.return_value = response
        mock_urlopen.side_effect = [TimeoutError("temporary"), response]

        result = call_llm_api(
            messages=[{"role": "user", "content": "hello"}],
            api_key="test-key",
            api_url="https://example.test/chat",
        )

        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["llm_attempts"], 2)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
