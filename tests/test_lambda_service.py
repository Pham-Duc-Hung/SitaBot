"""
Author: Trịnh Thiên Quang
Kiểm thử các quy tắc an toàn cơ bản của LambdaService.
"""
import os
import sys
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

# Thêm path tới services của dispatcher trong backend
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "backend", "functions", "dispatcher", "services"))
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

from lambda_service import LambdaService


class LambdaServiceSafetyTests(unittest.TestCase):
    def setUp(self):
        # Skip __init__: no boto3 client or AWS credentials are created.
        self.service = object.__new__(LambdaService)

    def test_unknown_tool_is_rejected(self):
        result = self.service.execute_tool("not_a_real_tool", {})
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "UnsupportedTool")

    def test_non_dict_parameters_are_rejected(self):
        result = self.service.execute_tool("list_lambda_functions", ["bad"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")

    def test_destructive_tool_requires_confirmation(self):
        self.service.update_function_resources = Mock(
            return_value={"status": "SUCCESS"}
        )
        result = self.service.execute_tool(
            "update_function_resources",
            {"function_name": "fake-function", "memory_mb": 256},
        )
        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(result["error_code"], "ConfirmationRequired")
        self.service.update_function_resources.assert_not_called()

    def test_confirmed_tool_dispatches(self):
        self.service.update_function_resources = Mock(
            return_value={"status": "SUCCESS", "action": "update_function_resources"}
        )
        result = self.service.execute_tool(
            "update_function_resources",
            {"function_name": "fake-function", "memory_mb": 256},
            confirmed=True,
        )
        self.assertEqual(result["status"], "SUCCESS")
        self.service.update_function_resources.assert_called_once_with(
            function_name="fake-function", memory_mb=256, timeout_seconds=None
        )

    def test_access_denied_is_classified(self):
        error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "ListFunctions",
        )
        result = self.service._handle_boto_error("list_lambda_functions", error)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "AccessDeniedException")
        self.assertIn("không đủ quyền", result["message"])

    def test_missing_resource_is_classified(self):
        error = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetFunction",
        )
        result = self.service._handle_boto_error("get_function_config_detail", error)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "ResourceNotFoundException")
        self.assertIn("Không tìm thấy", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

