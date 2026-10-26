"""
Author: Trịnh Thiên Quang
Kiểm thử tối ưu hóa cold start và phân quyền an toàn cho Lambda Backend.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", "backend", "functions", "dispatcher", "services"))
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

from lambda_service import (
    LambdaService,
    get_cached_lambda_service,
    clear_service_cache,
    execute,
    _compute_cache_key,
)


class TestOptimizationAndColdStart(unittest.TestCase):
    def setUp(self):
        clear_service_cache()

    def tearDown(self):
        clear_service_cache()

    def test_cache_key_computation(self):
        key_default = _compute_cache_key(None, "us-east-1")
        self.assertEqual(key_default, "default:us-east-1")

        key_ak = _compute_cache_key({"AccessKeyId": "AKIA123"}, "us-west-2")
        self.assertTrue(key_ak.startswith("credential:"))
        self.assertTrue(key_ak.endswith(":us-west-2"))
        self.assertNotIn("AKIA123", key_ak)

        key_role = _compute_cache_key({"RoleArn": "arn:aws:iam::123:role/test", "ExternalId": "ext1"}, "us-east-1")
        self.assertTrue(key_role.startswith("credential:"))
        self.assertTrue(key_role.endswith(":us-east-1"))
        self.assertNotIn("ext1", key_role)

    @patch("lambda_service.LambdaService")
    def test_global_context_caching_reuses_instance(self, mock_service_cls):
        mock_instance = Mock()
        mock_service_cls.return_value = mock_instance

        # Lần 1: Khởi tạo (Cold start)
        svc1 = get_cached_lambda_service(region="us-east-1")
        # Lần 2: Tái sử dụng (Warm start)
        svc2 = get_cached_lambda_service(region="us-east-1")

        self.assertIs(svc1, svc2)
        # Chỉ khởi tạo đúng 1 lần duy nhất
        mock_service_cls.assert_called_once_with(region_name="us-east-1")

    @patch("lambda_service.LambdaService")
    def test_different_regions_create_separate_instances(self, mock_service_cls):
        mock_instance_east = Mock()
        mock_instance_west = Mock()
        mock_service_cls.side_effect = [mock_instance_east, mock_instance_west]

        svc_east = get_cached_lambda_service(region="us-east-1")
        svc_west = get_cached_lambda_service(region="us-west-2")

        self.assertIsNot(svc_east, svc_west)
        self.assertEqual(mock_service_cls.call_count, 2)

    @patch("lambda_service.LambdaService")
    def test_clear_service_cache_resets_instances(self, mock_service_cls):
        svc1 = get_cached_lambda_service(region="us-east-1")
        clear_service_cache()
        svc2 = get_cached_lambda_service(region="us-east-1")

        self.assertEqual(mock_service_cls.call_count, 2)

    @patch("lambda_service.LambdaService")
    def test_enable_cache_false_bypasses_cache(self, mock_service_cls):
        svc1 = get_cached_lambda_service(region="us-east-1", enable_cache=False)
        svc2 = get_cached_lambda_service(region="us-east-1", enable_cache=False)

        self.assertEqual(mock_service_cls.call_count, 2)


class TestPermissionHardening(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(LambdaService)
        self.service.region_name = "us-east-1"
        self.service.client = Mock()

    def _make_access_denied_error(self, operation: str = "Lambda"):
        return ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": f"User is not authorized to perform {operation}",
                }
            },
            operation,
        )

    def test_list_functions_permission_denied(self):
        self.service.client.get_paginator.side_effect = self._make_access_denied_error("ListFunctions")
        res = self.service.list_lambda_functions()

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:ListFunctions")
        self.assertIn("không đủ quyền", res["message"])

    def test_update_resources_permission_denied(self):
        self.service.client.get_function_configuration.return_value = {
            "MemorySize": 128,
            "Timeout": 10,
            "Runtime": "python3.12",
            "Handler": "index.handler",
            "State": "Active",
            "LastUpdateStatus": "Successful",
        }
        self.service.client.update_function_configuration.side_effect = self._make_access_denied_error("UpdateFunctionConfiguration")
        res = self.service.update_function_resources("my-func", memory_mb=256)

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:UpdateFunctionConfiguration")
        self.assertIn("không đủ quyền", res["message"])

    def test_invoke_permission_denied(self):
        self.service.client.invoke.side_effect = self._make_access_denied_error("InvokeFunction")
        res = self.service.invoke_lambda_function("my-func", payload={})

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:InvokeFunction")

    def test_get_config_detail_permission_denied(self):
        self.service.client.get_function_configuration.side_effect = self._make_access_denied_error("GetFunctionConfiguration")
        res = self.service.get_function_config_detail("my-func")

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:GetFunctionConfiguration")

    def test_create_public_url_permission_denied(self):
        self.service.client.get_function_url_config.side_effect = self._make_access_denied_error("GetFunctionUrlConfig")
        res = self.service.create_public_function_url("my-func", cors_allow_origins=["https://example.com"])

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:CreateFunctionUrlConfig")

    def test_kill_switch_permission_denied(self):
        self.service.client.put_function_concurrency.side_effect = self._make_access_denied_error("PutFunctionConcurrency")
        res = self.service.set_emergency_kill_switch("my-func", enable_kill_switch=True)

        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "AccessDeniedException")
        self.assertEqual(res["required_permission"], "lambda:PutFunctionConcurrency")


if __name__ == "__main__":
    unittest.main(verbosity=2)
