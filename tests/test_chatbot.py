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

from backend.functions.chatbot.index import lambda_handler, format_tools_for_llm


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

    def test_api_gateway_event_wrapper(self):
        event = {"body": json.dumps({"message": "Hello"})}
        res = lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
