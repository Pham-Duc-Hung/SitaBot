"""
Mock-based business tests cho lambda_service.py (không gọi AWS thật)
Phụ trách: Trịnh Thiên Quang
"""
import io
import os
import sys
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "backend", "functions", "dispatcher", "services"))
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

from lambda_service import LambdaService


class LambdaServiceBusinessTests(unittest.TestCase):
    def setUp(self):
        # Avoid creating a boto3 session or contacting AWS.
        self.service = object.__new__(LambdaService)
        self.service.region_name = "us-east-1"
        self.service.client = Mock()

    def test_list_functions_formats_results(self):
        paginator = Mock()
        paginator.paginate.return_value = [{
            "Functions": [{
                "FunctionName": "demo-function",
                "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:demo-function",
                "Runtime": "python3.12",
                "MemorySize": 256,
                "Timeout": 10,
                "Handler": "app.handler",
                "CodeSize": 1024,
                "Description": "demo",
            }]
        }]
        self.service.client.get_paginator.return_value = paginator

        result = self.service.list_lambda_functions(max_items=5)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["region"], "us-east-1")
        self.assertEqual(result["data"]["functions"][0]["function_name"], "demo-function")
        paginator.paginate.assert_called_once_with(PaginationConfig={"MaxItems": 5})

    def test_list_functions_handles_access_denied(self):
        self.service.client.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "ListFunctions",
        )

        result = self.service.list_lambda_functions()

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "AccessDeniedException")

    def test_get_function_detail_includes_config_and_optional_url(self):
        self.service.client.get_function_configuration.return_value = {
            "FunctionName": "demo-function",
            "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:demo-function",
            "Runtime": "python3.12",
            "Role": "arn:aws:iam::123456789012:role/demo",
            "Handler": "app.handler",
            "MemorySize": 512,
            "Timeout": 20,
            "EphemeralStorage": {"Size": 1024},
            "State": "Active",
            "LastUpdateStatus": "Successful",
        }
        self.service.client.get_function_concurrency.return_value = {
            "ReservedConcurrentExecutions": 10
        }
        self.service.client.get_function_url_config.return_value = {
            "FunctionUrl": "https://example.lambda-url.us-east-1.on.aws/"
        }

        result = self.service.get_function_config_detail("demo-function")

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["memory_mb"], 512)
        self.assertEqual(result["data"]["timeout_seconds"], 20)
        self.assertEqual(result["data"]["reserved_concurrency"], 10)
        self.assertFalse(result["data"]["is_paused_kill_switch"])
        self.assertEqual(
            result["data"]["function_url"],
            "https://example.lambda-url.us-east-1.on.aws/",
        )

    def test_get_function_detail_tolerates_missing_optional_url(self):
        self.service.client.get_function_configuration.return_value = {
            "FunctionName": "demo-function",
            "Runtime": "python3.12",
            "MemorySize": 128,
            "Timeout": 3,
        }
        self.service.client.get_function_concurrency.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no concurrency config"}},
            "GetFunctionConcurrency",
        )
        self.service.client.get_function_url_config.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no URL"}},
            "GetFunctionUrlConfig",
        )

        result = self.service.get_function_config_detail("demo-function")

        self.assertEqual(result["status"], "SUCCESS")
        self.assertIsNone(result["data"]["reserved_concurrency"])
        self.assertIsNone(result["data"]["function_url"])
        self.assertFalse(result["data"]["is_paused_kill_switch"])

    def test_preview_rejects_memory_out_of_range_without_updating(self):
        self.service.client.get_function_configuration.return_value = {
            "MemorySize": 128,
            "Timeout": 3,
        }

        result = self.service.preview_config_update("demo-function", new_memory_mb=64)

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.update_function_configuration.assert_not_called()

    def test_preview_rejects_timeout_out_of_range(self):
        self.service.client.get_function_configuration.return_value = {
            "MemorySize": 128,
            "Timeout": 3,
        }

        result = self.service.preview_config_update("demo-function", new_timeout_seconds=901)

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error_code"], "InvalidParameters")
        self.service.client.update_function_configuration.assert_not_called()

    def test_update_resources_calls_client_with_requested_values(self):
        self.service.client.get_function_configuration.return_value = {
            "MemorySize": 128,
            "Timeout": 3,
        }
        self.service.client.update_function_configuration.return_value = {
            "MemorySize": 256,
            "Timeout": 10,
            "LastUpdateStatus": "InProgress",
            "LastModified": "2026-10-22T00:00:00.000+0000",
        }

        result = self.service.update_function_resources(
            "demo-function", memory_mb=256, timeout_seconds=10
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.service.client.update_function_configuration.assert_called_once_with(
            FunctionName="demo-function", MemorySize=256, Timeout=10
        )

    def test_invoke_parses_json_response(self):
        self.service.client.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": None,
            "ExecutedVersion": "$LATEST",
            "Payload": io.BytesIO(b'{"ok": true, "value": 42}'),
        }

        result = self.service.invoke_lambda_function("demo-function", {"input": 1})

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["response_payload"], {"ok": True, "value": 42})
        self.service.client.invoke.assert_called_once()

    def test_invoke_marks_function_runtime_error(self):
        self.service.client.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "ExecutedVersion": "$LATEST",
            "Payload": io.BytesIO(b'{"errorMessage": "failed"}'),
        }

        result = self.service.invoke_lambda_function("demo-function", {})

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["data"]["function_error"], "Unhandled")


if __name__ == "__main__":
    unittest.main(verbosity=2)

