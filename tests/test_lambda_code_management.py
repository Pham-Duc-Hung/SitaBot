"""
Author: Trịnh Thiên Quang
Kiểm thử tính năng tự động tạo, đọc và cập nhật mã nguồn Lambda của AI Agent.
"""

import io
import unittest
import zipfile
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError
from backend.functions.dispatcher.services.lambda_service import LambdaService


class TestLambdaCodeManagement(unittest.TestCase):
    def setUp(self):
        self.service = LambdaService(region_name="us-east-1")
        self.service.client = MagicMock()

    def test_get_lambda_source_code_success(self):
        # Tạo mock zip data
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("lambda_function.py", "def lambda_handler(event, context):\n    return 'OK'\n")
        fake_zip = buf.getvalue()

        self.service.client.get_function.return_value = {
            "Configuration": {"Runtime": "python3.12", "Handler": "lambda_function.lambda_handler"},
            "Code": {"Location": "https://s3.amazonaws.com/test-presigned-url"},
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_zip
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = self.service.get_lambda_source_code("test-lambda")

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["data"]["function_name"], "test-lambda")
        self.assertIn("def lambda_handler", res["data"]["main_code"])

    def test_create_lambda_function_syntax_error(self):
        # Code Python có lỗi cú pháp
        bad_code = "def lambda_handler(event, context)\n return 123"
        res = self.service.create_lambda_function("bad-func", bad_code)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "PythonSyntaxError")
        self.service.client.create_function.assert_not_called()

    def test_create_lambda_function_does_not_guess_execution_role(self):
        good_code = "def lambda_handler(event, context):\n    return {'statusCode': 200}"
        res = self.service.create_lambda_function("safe-func", good_code)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "InvalidParameters")
        self.service.client.create_function.assert_not_called()

    def test_create_lambda_function_success(self):
        good_code = "def lambda_handler(event, context):\n    return {'statusCode': 200}"
        self.service.client.create_function.return_value = {
            "FunctionName": "new-calc",
            "FunctionArn": "arn:aws:lambda:us-east-1:12345:function:new-calc",
            "Runtime": "python3.12",
            "Handler": "lambda_function.lambda_handler",
            "MemorySize": 256,
            "Timeout": 20,
            "CodeSize": 150,
        }

        res = self.service.create_lambda_function(
            function_name="new-calc",
            code_content=good_code,
            memory_mb=256,
            timeout_seconds=20,
            role_arn="arn:aws:iam::12345:role/sample-role",
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["data"]["function_name"], "new-calc")
        self.assertEqual(res["data"]["memory_mb"], 256)
        self.service.client.create_function.assert_called_once()

    def test_create_lambda_function_is_idempotent_when_function_exists(self):
        self.service.client.create_function.side_effect = ClientError(
            {"Error": {"Code": "ResourceConflictException", "Message": "already exists"}},
            "CreateFunction",
        )
        self.service.client.get_function_configuration.return_value = {
            "FunctionName": "resilient-api", "Runtime": "python3.12", "MemorySize": 512, "Timeout": 30
        }
        code = "def lambda_handler(event, context):\n    return {}"
        res = self.service.create_lambda_function(
            "resilient-api", code, role_arn="arn:aws:iam::12345:role/sample-role"
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["data"]["already_existed"])

    def test_update_lambda_code_success(self):
        new_code = "def lambda_handler(event, context):\n    return {'status': 'updated'}"
        self.service.client.update_function_code.return_value = {
            "FunctionName": "test-lambda",
            "FunctionArn": "arn:aws:lambda:us-east-1:12345:function:test-lambda",
            "Runtime": "python3.12",
            "LastModified": "2026-10-25T16:00:00.000+0000",
            "CodeSize": 200,
        }

        res = self.service.update_lambda_code("test-lambda", new_code)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["data"]["function_name"], "test-lambda")
        self.service.client.update_function_code.assert_called_once()

    def test_update_lambda_code_rejects_zip_slip_filename(self):
        code = "def lambda_handler(event, context):\n    return {}"
        res = self.service.update_lambda_code("test-lambda", code, filename="../../lambda_function.py")
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "InvalidParameters")
        self.service.client.update_function_code.assert_not_called()

    def test_configure_resilient_service_publishes_release_and_controls(self):
        waiter = MagicMock()
        self.service.client.get_waiter.return_value = waiter
        self.service.client.publish_version.return_value = {"Version": "7"}
        self.service.client.update_alias.return_value = {
            "Name": "live", "AliasArn": "arn:aws:lambda:us-east-1:12345:function:demo:live"
        }

        res = self.service.configure_resilient_service(
            "demo", alias_name="live", reserved_concurrency=3,
            maximum_retry_attempts=2, maximum_event_age_seconds=3600,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["data"]["published_version"], "7")
        waiter.wait.assert_called_once()
        self.service.client.publish_version.assert_called_once()
        self.service.client.update_alias.assert_called_once()
        self.service.client.put_function_event_invoke_config.assert_called_once()
        self.service.client.put_function_concurrency.assert_called_once()

    def test_configure_resilient_service_rejects_invalid_retry_count(self):
        res = self.service.configure_resilient_service("demo", maximum_retry_attempts=3)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "InvalidParameters")
        self.service.client.publish_version.assert_not_called()

    def test_configure_resilient_service_degrades_gracefully_on_concurrency_quota(self):
        waiter = MagicMock()
        self.service.client.get_waiter.return_value = waiter
        self.service.client.publish_version.return_value = {"Version": "8"}
        self.service.client.update_alias.return_value = {"Name": "live"}
        self.service.client.put_function_concurrency.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameterValueException", "Message": "UnreservedConcurrentExecution below minimum 10"}},
            "PutFunctionConcurrency",
        )
        res = self.service.configure_resilient_service("demo")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["data"]["warnings"])
        self.assertNotIn("reserved_concurrency", res["data"]["completed_controls"])

    def test_execute_tool_requires_confirmation_for_create_and_update(self):
        # Thử gọi create_lambda_function mà chưa confirm
        res_create = self.service.execute_tool(
            "create_lambda_function",
            {"function_name": "calc", "code_content": "def lambda_handler(e, c): pass"},
            confirmed=False,
        )
        self.assertEqual(res_create["status"], "CONFIRMATION_REQUIRED")

        # Thử gọi update_lambda_code mà chưa confirm
        res_update = self.service.execute_tool(
            "update_lambda_code",
            {"function_name": "calc", "code_content": "def lambda_handler(e, c): pass"},
            confirmed=False,
        )
        self.assertEqual(res_update["status"], "CONFIRMATION_REQUIRED")

        # Gọi get_lambda_source_code không cần confirm
        self.service.get_lambda_source_code = MagicMock(return_value={"status": "SUCCESS"})
        res_get = self.service.execute_tool(
            "get_lambda_source_code",
            {"function_name": "calc"},
            confirmed=False,
        )
        self.assertEqual(res_get["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
