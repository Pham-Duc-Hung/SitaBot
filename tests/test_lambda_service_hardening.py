"""
Author: Trịnh Thiên Quang
Kiểm thử ràng buộc kiểu dữ liệu, tham số và CORS cho LambdaService.
"""
import os
import sys
import unittest
from unittest.mock import Mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "backend", "functions", "dispatcher", "services"))
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

from lambda_service import LambdaService, LAMBDA_TOOL_SCHEMAS


class LambdaServiceHardeningTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(LambdaService)
        self.service.region_name = "us-east-1"
        self.service.client = Mock()

    def test_update_rejects_memory_boolean_before_aws_call(self):
        result = self.service.update_function_resources("demo", memory_mb=True)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.get_function_configuration.assert_not_called()
        self.service.client.update_function_configuration.assert_not_called()

    def test_update_rejects_timeout_float_before_aws_call(self):
        result = self.service.update_function_resources("demo", timeout_seconds=2.5)
        self.assertEqual(result["status"], "ERROR")
        self.service.client.get_function_configuration.assert_not_called()

    def test_update_rejects_out_of_range_values_before_aws_call(self):
        result = self.service.update_function_resources("demo", memory_mb=10241)
        self.assertEqual(result["status"], "ERROR")
        self.service.client.get_function_configuration.assert_not_called()

    def test_preview_rejects_boolean_before_aws_call(self):
        result = self.service.preview_config_update("demo", new_memory_mb=False)
        self.assertEqual(result["status"], "ERROR")
        self.service.client.get_function_configuration.assert_not_called()

    def test_kill_switch_rejects_zero_restore_concurrency(self):
        result = self.service.set_emergency_kill_switch(
            "demo", enable_kill_switch=False, restore_concurrency=0
        )
        self.assertEqual(result["status"], "ERROR")
        self.service.client.delete_function_concurrency.assert_not_called()
        self.service.client.put_function_concurrency.assert_not_called()

    def test_kill_switch_rejects_non_boolean_flag(self):
        result = self.service.set_emergency_kill_switch("demo", enable_kill_switch="false")
        self.assertEqual(result["status"], "ERROR")
        self.service.client.put_function_concurrency.assert_not_called()

    def test_public_url_requires_explicit_origins(self):
        result = self.service.create_public_function_url("demo")
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.create_function_url_config.assert_not_called()

    def test_public_url_rejects_wildcard_origin(self):
        result = self.service.create_public_function_url("demo", ["*"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.create_function_url_config.assert_not_called()

    def test_schema_declares_cors_allow_origins(self):
        schema = next(
            (s for s in LAMBDA_TOOL_SCHEMAS if s["name"] == "create_public_function_url"),
            None,
        )
        self.assertIsNotNone(schema, "Schema cho create_public_function_url phải tồn tại.")
        params = schema.get("parameters", {})
        properties = params.get("properties", {})
        self.assertIn("cors_allow_origins", properties)
        self.assertEqual(properties["cors_allow_origins"]["type"], "array")
        self.assertEqual(properties["cors_allow_origins"]["items"]["type"], "string")
        self.assertIn("cors_allow_origins", params.get("required", []))

    def test_execute_tool_dispatches_valid_origins_to_function(self):
        origins = ["https://app.example.com", "https://api.example.com"]
        self.service.create_public_function_url = Mock(
            return_value={"status": "SUCCESS", "action": "create_public_function_url"}
        )
        result = self.service.execute_tool(
            "create_public_function_url",
            {"function_name": "demo", "cors_allow_origins": origins},
            confirmed=True,
        )
        self.assertEqual(result["status"], "SUCCESS")
        self.service.create_public_function_url.assert_called_once_with(
            function_name="demo",
            cors_allow_origins=origins,
        )

    def test_execute_tool_rejects_missing_origin(self):
        result = self.service.execute_tool(
            "create_public_function_url",
            {"function_name": "demo"},
            confirmed=True,
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.create_function_url_config.assert_not_called()

    def test_execute_tool_rejects_wildcard_origin(self):
        result = self.service.execute_tool(
            "create_public_function_url",
            {"function_name": "demo", "cors_allow_origins": ["*"]},
            confirmed=True,
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.create_function_url_config.assert_not_called()

    def test_invalid_origin_cases_do_not_call_aws_api(self):
        invalid_cases = [
            None,
            [],
            ["*"],
            ["https://example.com", "*"],
            ["   "],
            [123],
            "https://example.com",
            {"origin": "https://example.com"},
        ]
        for invalid_origin in invalid_cases:
            with self.subTest(origin=invalid_origin):
                result = self.service.create_public_function_url(
                    "demo", cors_allow_origins=invalid_origin
                )
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["error_code"], "InvalidParameters")
                self.service.client.create_function_url_config.assert_not_called()
                self.service.client.add_permission.assert_not_called()

    def test_dispatch_rejects_unknown_parameter(self):
        result = self.service.execute_tool(
            "list_lambda_functions", {"max_items": 5, "unexpected": True}
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.get_paginator.assert_not_called()

    def test_dispatch_rejects_boolean_for_integer_parameter(self):
        result = self.service.execute_tool("list_lambda_functions", {"max_items": True})
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.get_paginator.assert_not_called()

    def test_dispatch_rejects_blank_function_name(self):
        result = self.service.execute_tool(
            "get_function_config_detail", {"function_name": "   "}
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.get_function_configuration.assert_not_called()

    def test_update_resources_requires_confirmation(self):
        result = self.service.execute_tool(
            "update_function_resources",
            {"function_name": "demo", "memory_mb": 512},
            confirmed=False,
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.service.client.get_function_configuration.assert_not_called()
        self.service.client.update_function_configuration.assert_not_called()

    def test_invoke_requires_confirmation(self):
        result = self.service.execute_tool(
            "invoke_lambda_function",
            {"function_name": "demo"},
            confirmed=False,
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.service.client.invoke.assert_not_called()

    def test_public_url_requires_confirmation(self):
        result = self.service.execute_tool(
            "create_public_function_url",
            {
                "function_name": "demo",
                "cors_allow_origins": ["https://example.com"],
            },
            confirmed=False,
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.service.client.create_function_url_config.assert_not_called()

    def test_kill_switch_requires_confirmation(self):
        result = self.service.execute_tool(
            "set_emergency_kill_switch",
            {"function_name": "demo", "enable_kill_switch": True},
            confirmed=False,
        )

        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        self.service.client.put_function_concurrency.assert_not_called()
        self.service.client.delete_function_concurrency.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

