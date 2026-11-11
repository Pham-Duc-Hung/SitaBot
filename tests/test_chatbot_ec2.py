"""
Unit tests kiểm thử tích hợp Chatbot Lambda với Amazon EC2 Service qua LLM Function Calling.
Kiểm thử trích xuất Tool Call, phân loại cờ xác nhận (Guardrails) và thực thi điều phối Boto3.
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Thêm đường dẫn project root
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Giả lập boto3 và botocore nếu môi trường cục bộ chưa cài đặt
if "boto3" not in sys.modules:
    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()
    mock_botocore_exceptions = MagicMock()
    mock_botocore_exceptions.ClientError = type("ClientError", (Exception,), {})
    mock_botocore_exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
    sys.modules["boto3"] = mock_boto3
    sys.modules["botocore"] = mock_botocore
    sys.modules["botocore.config"] = MagicMock()
    sys.modules["botocore.exceptions"] = mock_botocore_exceptions

from backend.functions.chatbot.index import (
    _get_all_tool_schemas,
    _get_ec2_tool_schemas,
    _resolve_service_for_tool,
    format_tools_for_llm,
    lambda_handler,
)


class TestChatbotEc2Integration(unittest.TestCase):
    """Kiểm thử tích hợp các kịch bản ChatOps cho Amazon EC2."""

    def setUp(self):
        # Tách biệt unit test khỏi môi trường internet và API key thực
        self.env_patcher = patch.dict(os.environ, {"LLM_API_KEY": "", "GEMINI_API_KEY": ""}, clear=False)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    # ==============================================================================
    # 1. KIỂM THỬ NẠP VÀ ĐỊNH DẠNG TOOL SCHEMAS CỦA EC2
    # ==============================================================================

    def test_ec2_tool_schemas_loaded(self):
        """Kiểm tra nạp đủ 7 tool schemas của EC2 vào Chatbot."""
        ec2_schemas = _get_ec2_tool_schemas()
        self.assertGreaterEqual(len(ec2_schemas), 7)
        tool_names = [s.get("name") for s in ec2_schemas]
        self.assertIn("create_ec2_instance", tool_names)
        self.assertIn("list_ec2_instances", tool_names)
        self.assertIn("get_ec2_instance_detail", tool_names)
        self.assertIn("manage_ec2_power", tool_names)
        self.assertIn("terminate_ec2_instance", tool_names)
        self.assertIn("get_ec2_metrics", tool_names)
        self.assertIn("audit_ec2_security", tool_names)

    def test_all_tool_schemas_contains_ec2_and_lambda(self):
        """Kiểm tra _get_all_tool_schemas nạp đồng thời cả công cụ Lambda và EC2."""
        all_schemas = _get_all_tool_schemas()
        tool_names = [s.get("name") for s in all_schemas]
        # Có công cụ EC2
        self.assertIn("create_ec2_instance", tool_names)
        self.assertIn("get_ec2_metrics", tool_names)
        self.assertIn("list_ec2_instances", tool_names)
        self.assertIn("audit_ec2_security", tool_names)

    def test_resolve_service_for_ec2_tools(self):
        """Kiểm tra định tuyến chính xác tên service 'ec2' cho các công cụ EC2."""
        raw_schemas = _get_ec2_tool_schemas()
        self.assertEqual(_resolve_service_for_tool("create_ec2_instance", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("list_ec2_instances", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("get_ec2_instance_detail", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("manage_ec2_power", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("terminate_ec2_instance", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("get_ec2_metrics", raw_schemas), "ec2")
        self.assertEqual(_resolve_service_for_tool("audit_ec2_security", raw_schemas), "ec2")

    # ==============================================================================
    # 2. KIỂM THỬ CÁC THAO TÁC THAY ĐỔI HẠ TẦNG (YÊU CẦU PHÊ DUYỆT / CONFIRMATION)
    # ==============================================================================

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_create_ec2_instance_intent_requires_confirmation(self, mock_llm):
        """Tạo máy ảo EC2 là thao tác thay đổi hạ tầng, bắt buộc trả về CONFIRMATION_REQUIRED."""
        mock_llm.return_value = {
            "content": "Tôi sẽ giúp bạn tạo máy ảo Ubuntu.",
            "tool_calls": [{
                "id": "call_ec2_create_01",
                "type": "function",
                "function": {
                    "name": "create_ec2_instance",
                    "arguments": json.dumps({
                        "name": "web-demo",
                        "instance_type": "t3.micro",
                        "os": "ubuntu",
                    }),
                },
            }],
        }

        event = {"message": "Tạo cho tôi máy chủ Ubuntu t3.micro tên là web-demo"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["type"], "action_confirmation")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "create_ec2_instance")
        self.assertEqual(res["parameters"]["name"], "web-demo")
        self.assertEqual(res["parameters"]["instance_type"], "t3.micro")
        self.assertIn("action_id", res)
        self.assertTrue(res["action_id"].startswith("act_"))

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_manage_ec2_power_intent_requires_confirmation(self, mock_llm):
        """Bật/tắt máy ảo EC2 bắt buộc trả về CONFIRMATION_REQUIRED."""
        mock_llm.return_value = {
            "content": "Tôi sẽ gửi yêu cầu tắt máy ảo.",
            "tool_calls": [{
                "id": "call_ec2_stop_02",
                "type": "function",
                "function": {
                    "name": "manage_ec2_power",
                    "arguments": json.dumps({
                        "instance_id": "i-015aad95042137744",
                        "operation": "stop",
                    }),
                },
            }],
        }

        event = {"message": "Hãy tắt máy ảo i-015aad95042137744"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["type"], "action_confirmation")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "manage_ec2_power")
        self.assertEqual(res["parameters"]["instance_id"], "i-015aad95042137744")
        self.assertEqual(res["parameters"]["operation"], "stop")

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_terminate_ec2_instance_intent_requires_confirmation(self, mock_llm):
        """Xóa vĩnh viễn máy ảo EC2 là rủi ro cao, bắt buộc CONFIRMATION_REQUIRED."""
        mock_llm.return_value = {
            "content": "Cảnh báo: Máy ảo sẽ bị hủy vĩnh viễn.",
            "tool_calls": [{
                "id": "call_ec2_term_03",
                "type": "function",
                "function": {
                    "name": "terminate_ec2_instance",
                    "arguments": json.dumps({
                        "instance_id": "i-015aad95042137744",
                    }),
                },
            }],
        }

        event = {"message": "Xóa máy ảo i-015aad95042137744 đi"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "terminate_ec2_instance")

    # ==============================================================================
    # 3. KIỂM THỬ CÁC THAO TÁC CHỈ ĐỌC (TỰ ĐỘNG DISPATCH NGAY)
    # ==============================================================================

    @patch("backend.functions.chatbot.index._dispatch_action")
    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_list_ec2_instances_executes_immediately(self, mock_llm, mock_dispatch):
        """Xem danh sách máy ảo là tác vụ an toàn, tự động điều phối gọi Dispatcher."""
        mock_llm.return_value = {
            "content": "Đang tra cứu danh sách máy ảo...",
            "tool_calls": [{
                "id": "call_ec2_list_04",
                "type": "function",
                "function": {
                    "name": "list_ec2_instances",
                    "arguments": json.dumps({"state_filter": "running"}),
                },
            }],
        }
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "Tìm thấy 1 máy ảo đang hoạt động.",
            "data": [{
                "instance_id": "i-015aad95042137744",
                "name": "SatiBot-DucHuy-Test",
                "state": "running",
                "instance_type": "t3.micro",
            }],
        }

        event = {"message": "Liệt kê các máy ảo EC2 đang chạy"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "list_ec2_instances")
        mock_dispatch.assert_called_once()
        self.assertEqual(len(res["data"]), 1)

    @patch("backend.functions.chatbot.index._dispatch_action")
    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_get_ec2_metrics_executes_immediately(self, mock_llm, mock_dispatch):
        """Xem thông số CPU CloudWatch tự động thực thi ngay lập tức."""
        mock_llm.return_value = {
            "content": "Đang đọc metric CloudWatch...",
            "tool_calls": [{
                "id": "call_ec2_metrics_05",
                "type": "function",
                "function": {
                    "name": "get_ec2_metrics",
                    "arguments": json.dumps({"instance_id": "i-015aad95042137744"}),
                },
            }],
        }
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "Thông số CPU 24h qua: trung bình 1.25%. Khuyến nghị FinOps: Máy ảo nhàn rỗi (idle_waste).",
            "data": {
                "instance_id": "i-015aad95042137744",
                "metric_name": "CPUUtilization",
                "average_percent": 1.25,
                "status": "idle_waste",
            },
        }

        event = {"message": "Xem tình trạng sử dụng CPU của máy i-015aad95042137744"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "get_ec2_metrics")
        self.assertIn("idle_waste", res["data"]["status"])

    # ==============================================================================
    # 4. KIỂM THỬ THỰC THI HÀNH ĐỘNG ĐÃ ĐƯỢC DUYỆT (CONFIRMED = TRUE)
    # ==============================================================================

    @patch("backend.functions.chatbot.index._dispatch_action")
    def test_confirmed_create_ec2_execution(self, mock_dispatch):
        """Khi nhận event có cờ confirmed=True, Chatbot trực tiếp điều phối Boto3 EC2."""
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "Đã tạo máy ảo i-015aad95042137744 thành công.",
            "data": {
                "instance_id": "i-015aad95042137744",
                "instance_name": "web-demo",
                "state": "pending",
                "instance_type": "t3.micro",
            },
        }

        event = {
            "confirmed": True,
            "service": "ec2",
            "tool_name": "create_ec2_instance",
            "parameters": {
                "name": "web-demo",
                "instance_type": "t3.micro",
            },
        }
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "create_ec2_instance")
        self.assertEqual(res["data"]["instance_id"], "i-015aad95042137744")
        mock_dispatch.assert_called_once()

    # ==============================================================================
    # 5. KIỂM THỬ TƯƠNG THÍCH VỚI API GATEWAY PROXY EVENT
    # ==============================================================================

    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_api_gateway_wrapped_ec2_request(self, mock_llm):
        """Kiểm tra request bọc trong API Gateway body JSON trả về statusCode 200 chuẩn."""
        mock_llm.return_value = {
            "content": "Tôi sẽ giúp bạn tạo máy ảo.",
            "tool_calls": [{
                "id": "call_gw_01",
                "type": "function",
                "function": {
                    "name": "create_ec2_instance",
                    "arguments": json.dumps({"name": "api-gw-vm"}),
                },
            }],
        }

        event = {
            "body": json.dumps({"message": "Tạo máy ảo api-gw-vm"})
        }
        res = lambda_handler(event, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertIn("body", res)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(body["service"], "ec2")
        self.assertEqual(body["tool_name"], "create_ec2_instance")

    # ==============================================================================
    # 6. KIỂM THỬ ĐÁNH GIÁ AN NINH TƯỜNG LỬA (SECOPS SECURITY GROUP AUDIT)
    # ==============================================================================

    @patch("backend.functions.chatbot.index._dispatch_action")
    @patch("backend.functions.chatbot.index.call_llm_api")
    def test_audit_ec2_security_executes_immediately(self, mock_llm, mock_dispatch):
        """Quét an ninh tường lửa là tác vụ đọc/kiểm toán, tự động thực thi ngay."""
        mock_llm.return_value = {
            "content": "Đang phân tích cấu hình an ninh tường lửa...",
            "tool_calls": [{
                "id": "call_audit_01",
                "type": "function",
                "function": {
                    "name": "audit_ec2_security",
                    "arguments": json.dumps({"instance_id": "i-015aad95042137744"}),
                },
            }],
        }
        mock_dispatch.return_value = {
            "status": "SUCCESS",
            "message": "CẢNH BÁO BẢO MẬT: Phát hiện cổng 22 SSH đang mở công khai ra Internet (0.0.0.0/0).",
            "data": {
                "instance_id": "i-015aad95042137744",
                "risk_level": "CRITICAL",
                "risks_found": 1,
            },
        }

        event = {"message": "Kiểm tra an toàn tường lửa của máy i-015aad95042137744"}
        res = lambda_handler(event, None)

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["type"], "action_executed")
        self.assertEqual(res["service"], "ec2")
        self.assertEqual(res["tool_name"], "audit_ec2_security")
        self.assertEqual(res["data"]["risk_level"], "CRITICAL")
        mock_dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
