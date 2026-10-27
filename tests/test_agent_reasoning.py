"""
Author: Trịnh Thiên Quang
Kiểm thử chu trình ReAct Agent Loop và điều phối công cụ đa bước.
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CHATBOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "backend", "functions", "chatbot"))
if CHATBOT_DIR not in sys.path:
    sys.path.insert(0, CHATBOT_DIR)

from agent_executor import AgentExecutor


class TestAgentReasoningLoop(unittest.TestCase):
    def setUp(self):
        self.raw_schemas = [
            {
                "name": "list_lambda_functions",
                "requires_confirmation": False,
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_function_config_detail",
                "requires_confirmation": False,
                "parameters": {
                    "type": "object",
                    "properties": {"function_name": {"type": "string"}},
                    "required": ["function_name"],
                },
            },
            {
                "name": "diagnose_lambda_health",
                "requires_confirmation": False,
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "update_function_resources",
                "requires_confirmation": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function_name": {"type": "string"},
                        "memory_mb": {"type": "integer"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["function_name"],
                },
            },
            {
                "name": "create_lambda_function",
                "requires_confirmation": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function_name": {"type": "string"},
                        "code_content": {"type": "string"},
                        "description": {"type": "string"},
                        "runtime": {"type": "string"},
                        "memory_mb": {"type": "integer"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["function_name", "code_content"],
                },
            },
            {
                "name": "configure_resilient_service",
                "requires_confirmation": True,
                "parameters": {
                    "type": "object",
                    "properties": {"function_name": {"type": "string"}},
                    "required": ["function_name"],
                },
            },
            {
                "name": "create_public_function_url",
                "requires_confirmation": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function_name": {"type": "string"},
                        "cors_allow_origins": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["function_name", "cors_allow_origins"],
                },
            },
        ]
        self.tools = [{"type": "function", "function": {"name": s["name"]}} for s in self.raw_schemas]
        self.mock_llm = Mock()
        self.mock_dispatch = Mock()
        self.executor = AgentExecutor(
            call_llm_fn=self.mock_llm,
            dispatch_fn=self.mock_dispatch,
            tools=self.tools,
            raw_schemas=self.raw_schemas,
            max_iterations=4,
        )

    def test_agent_smalltalk_without_tools(self):
        """Kiểm thử khi người dùng hỏi chào hỏi, Agent phản hồi trực tiếp không qua tool."""
        self.mock_llm.return_value = {
            "content": "Xin chào! Tôi là SatiBot - AI Agent hỗ trợ quản trị hạ tầng AWS.",
            "tool_calls": None,
        }

        res = self.executor.run(user_message="Xin chào bạn", system_prompt="Prompt")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "agent_reply")
        self.assertIn("Xin chào", res["message"])
        self.assertEqual(len(res["steps"]), 0)
        self.mock_dispatch.assert_not_called()

    def test_agent_autonomous_investigation_loop(self):
        """Kiểm thử Agent tự động tra cứu dữ liệu (Action) và đọc kết quả (Observation) để trả lời."""
        # Vòng 1: Agent quyết định gọi get_function_config_detail
        call_1_resp = {
            "content": "Tôi đang tiến hành kiểm tra cấu hình hàm test-lambda...",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_function_config_detail",
                    "arguments": '{"function_name": "test-lambda"}',
                },
            }],
            "raw_message": {"role": "assistant", "content": "Checking..."},
        }
        # Vòng 2: Sau khi đọc dữ liệu AWS, Agent phân tích và đưa ra đánh giá
        call_2_resp = {
            "content": "Hàm test-lambda đang cấu hình RAM 128MB và Timeout 3s. Mức này quá thấp và rất dễ bị lỗi.",
            "tool_calls": None,
        }
        self.mock_llm.side_effect = [call_1_resp, call_2_resp]

        self.mock_dispatch.return_value = {
            "status": "SUCCESS",
            "data": {"function_name": "test-lambda", "memory_mb": 128, "timeout_seconds": 3},
            "message": "Chi tiết cấu hình hàm test-lambda",
        }

        res = self.executor.run(
            user_message="Hàm test-lambda có bị chậm không?",
            system_prompt="Prompt",
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "agent_reply")
        self.assertIn("RAM 128MB", res["message"])
        self.assertEqual(len(res["steps"]), 1)
        self.assertEqual(res["steps"][0]["action"], "get_function_config_detail")

    def test_agent_pauses_for_confirmation_on_modifications(self):
        """Kiểm thử khi Agent quyết định hành động sửa đổi tài nguyên, phải dừng lại xin xác nhận."""
        self.mock_llm.return_value = {
            "content": "Tôi đề xuất nâng RAM hàm test-lambda lên 512MB để tối ưu hiệu năng.",
            "tool_calls": [{
                "id": "call_update",
                "type": "function",
                "function": {
                    "name": "update_function_resources",
                    "arguments": '{"function_name": "test-lambda", "memory_mb": 512, "timeout_seconds": 30}',
                },
            }],
        }

        res = self.executor.run(
            user_message="Tối ưu hàm test-lambda giúp tôi",
            system_prompt="Prompt",
        )

        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["tool_name"], "update_function_resources")
        self.assertEqual(res["parameters"]["memory_mb"], 512)
        self.assertIn("act_", res["action_id"])
        self.mock_dispatch.assert_not_called()

    def test_agent_executes_multiple_read_only_tools_in_one_turn(self):
        self.mock_llm.side_effect = [
            {
                "content": "Kiểm tra song song hai nguồn dữ liệu.",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "list_lambda_functions", "arguments": "{}"}},
                    {"id": "call_2", "type": "function", "function": {"name": "diagnose_lambda_health", "arguments": "{}"}},
                ],
            },
            {"content": "Đã tổng hợp xong kết quả.", "tool_calls": None},
        ]
        self.mock_dispatch.side_effect = [
            {"status": "SUCCESS", "message": "Có 2 hàm", "data": {"count": 2}},
            {"status": "SUCCESS", "message": "Hệ thống khỏe", "data": {"health_score": 95}},
        ]

        result = self.executor.run("Kiểm tra toàn bộ Lambda", "Prompt")

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(self.mock_dispatch.call_count, 2)
        self.assertEqual([step["action"] for step in result["steps"]], ["list_lambda_functions", "diagnose_lambda_health"])

    def test_invalid_tool_call_is_returned_to_model_instead_of_executed(self):
        self.mock_llm.side_effect = [
            {"content": "", "tool_calls": [{"id": "bad", "type": "function", "function": {"name": "delete_everything", "arguments": "{}"}}]},
            {"content": "Tôi không có công cụ đó.", "tool_calls": None},
        ]

        result = self.executor.run("Xóa hết", "Prompt")

        self.assertEqual(result["status"], "SUCCESS")
        self.mock_dispatch.assert_not_called()
        self.assertEqual(result["steps"][0]["action"], "delete_everything")

    def test_iteration_limit_is_not_reported_as_success(self):
        self.executor.max_iterations = 1
        self.mock_llm.return_value = {
            "content": "Đang kiểm tra",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "list_lambda_functions", "arguments": "{}"}}],
        }
        self.mock_dispatch.return_value = {"status": "SUCCESS", "message": "OK", "data": {}}

        result = self.executor.run("Kiểm tra", "Prompt")

        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["type"], "agent_limit_reached")

    def test_operational_request_cannot_finish_without_tool_evidence(self):
        self.mock_llm.side_effect = [
            {
                "content": "Tôi đang truy cập AWS. Hãy bấm xác nhận để tôi làm tiếp.",
                "tool_calls": None,
            },
            {
                "content": "Bạn muốn tôi kiểm tra hay thay đổi Lambda nào?",
                "tool_calls": None,
            },
        ]

        result = self.executor.run("Truy cập vào AWS của tôi và làm đi", "Prompt")

        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertEqual(result["type"], "agent_needs_input")
        self.assertEqual(result["steps"], [])
        self.mock_dispatch.assert_not_called()

    def test_operational_request_retries_then_executes_real_tool(self):
        self.mock_llm.side_effect = [
            {"content": "Tôi sẽ kiểm tra ngay.", "tool_calls": None},
            {
                "content": "",
                "tool_calls": [{
                    "id": "call_retry",
                    "type": "function",
                    "function": {"name": "list_lambda_functions", "arguments": "{}"},
                }],
            },
            {"content": "Đã kiểm tra AWS dựa trên dữ liệu thực tế.", "tool_calls": None},
        ]
        self.mock_dispatch.return_value = {
            "status": "SUCCESS", "message": "Đã tìm thấy 2 hàm", "data": {"count": 2}
        }

        result = self.executor.run("Liệt kê Lambda trong tài khoản AWS", "Prompt")

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(result["steps"]), 1)
        self.mock_dispatch.assert_called_once()

    def test_resume_continues_resilient_deployment_after_approval(self):
        self.mock_llm.side_effect = [
            {
                "content": "Tạo service trước.",
                "tool_calls": [{
                    "id": "create_1", "type": "function",
                    "function": {"name": "create_lambda_function", "arguments": '{"function_name":"resilient-api","code_content":"def lambda_handler(e,c): return {}"}'},
                }],
            },
            {
                "content": "Tiếp tục cấu hình resilience.",
                "tool_calls": [{
                    "id": "harden_1", "type": "function",
                    "function": {"name": "configure_resilient_service", "arguments": '{"function_name":"resilient-api"}'},
                }],
            },
        ]

        first = self.executor.run("Triển khai Lambda resilient", "Prompt")
        resumed = self.executor.resume(
            first["agent_state"],
            {"status": "SUCCESS", "message": "Đã tạo resilient-api", "data": {"function_name": "resilient-api"}},
        )

        self.assertEqual(first["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(resumed["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(resumed["tool_name"], "configure_resilient_service")
        self.assertEqual(len(resumed["steps"]), 1)

    def test_invalid_tool_arguments_are_self_corrected_before_approval(self):
        self.mock_llm.side_effect = [
            {
                "content": "Cấu hình resilience.",
                "tool_calls": [{
                    "id": "bad_args", "type": "function",
                    "function": {"name": "configure_resilient_service", "arguments": "{}"},
                }],
            },
            {
                "content": "Đã sửa arguments.",
                "tool_calls": [{
                    "id": "fixed_args", "type": "function",
                    "function": {"name": "configure_resilient_service", "arguments": '{"function_name":"resilient-api"}'},
                }],
            },
        ]

        result = self.executor.run("Cấu hình Lambda resilient", "Prompt")

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(result["parameters"]["function_name"], "resilient-api")
        self.assertEqual(result["steps"][0]["action"], "configure_resilient_service")
        self.mock_dispatch.assert_not_called()

    def test_plain_text_pseudo_tool_call_is_recovered(self):
        self.mock_llm.return_value = {
            "content": """Đang tạo service.\n```python\ndef lambda_handler(event, context):\n    return {\"statusCode\": 200}\n```\n[Tool Call: create_lambda_function(function_name='resilient-webhook-receiver', code_content='...')]""",
            "tool_calls": None,
        }

        result = self.executor.run(
            "Triển khai Lambda resilient-webhook-receiver trên AWS", "Prompt"
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(result["tool_name"], "create_lambda_function")
        self.assertEqual(result["parameters"]["function_name"], "resilient-webhook-receiver")
        self.assertTrue(result["recovered_tool_call"])

    def test_hallucinated_confirmation_prose_is_intercepted_and_retried_with_real_tool(self):
        self.mock_llm.side_effect = [
            {
                "content": "Dạ, tôi cần bạn bấm vào nút Xác nhận (Confirm/Approve) trên hộp thoại yêu cầu gọi công cụ mà hệ thống đang hiển thị.",
                "tool_calls": None,
            },
            {
                "content": "",
                "tool_calls": [{
                    "id": "call_store",
                    "type": "function",
                    "function": {
                        "name": "create_lambda_function",
                        "arguments": json.dumps({
                            "function_name": "custom-service",
                            "code_content": "def lambda_handler(e, c): return {'statusCode': 200}",
                        }),
                    },
                }],
            },
        ]

        result = self.executor.run(
            "Triển khai dịch vụ của tôi lên AWS", "Prompt"
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(result["tool_name"], "create_lambda_function")
        self.assertEqual(result["parameters"]["function_name"], "custom-service")
        self.assertIn("lambda_handler", result["parameters"]["code_content"])

    def test_create_public_function_url_auto_injects_cors(self):
        tool_call = {
            "id": "call_url",
            "type": "function",
            "function": {
                "name": "create_public_function_url",
                "arguments": json.dumps({"function_name": "web-store-demo"}),
            },
        }
        tool_name, params, err = self.executor._parse_tool_call(tool_call)
        self.assertIsNone(err)
        self.assertEqual(params["cors_allow_origins"], ["http://127.0.0.1:8080"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

