"""
Author: Trịnh Thiên Quang
Kiểm thử bộ điều phối Dispatcher cho service Lambda.
"""

import json
import unittest
from unittest.mock import patch, MagicMock

import sys
import os

# Thêm project root vào path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.functions.dispatcher import index
from backend.functions.dispatcher.index import lambda_handler


class TestDispatcherLambda(unittest.TestCase):
    def test_direct_invoke_missing_service(self):
        event = {"action": "list_lambda_functions"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "MissingService")

    def test_direct_invoke_missing_action(self):
        event = {"service": "lambda"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "MissingAction")

    def test_direct_invoke_unsupported_service(self):
        event = {"service": "unknown_svc", "action": "do_something"}
        res = lambda_handler(event, None)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["error_code"], "UnsupportedService")

    def test_direct_invoke_service_under_development(self):
        with patch.object(index.iam_service, "execute") as mock_iam:
            mock_iam.return_value = {"status": "NOT_IMPLEMENTED", "error_code": "ServiceUnderDevelopment"}
            event = {"service": "iam", "action": "describe_instances"}
            res = lambda_handler(event, None)
            self.assertEqual(res["status"], "NOT_IMPLEMENTED")
            self.assertEqual(res["error_code"], "ServiceUnderDevelopment")

    def test_api_gateway_invoke_missing_service(self):
        event = {"body": json.dumps({"action": "list_lambda_functions"})}
        res = lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 400)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "ERROR")

    def test_dispatch_to_lambda_service_success(self):
        with patch.object(index.lambda_service, "execute") as mock_execute:
            mock_execute.return_value = {
                "status": "SUCCESS",
                "service": "lambda",
                "action": "list_lambda_functions",
                "message": "OK",
                "data": {"count": 1}
            }
            event = {
                "service": "lambda",
                "action": "list_lambda_functions",
                "parameters": {"max_items": 10},
                "confirmed": True
            }
            res = lambda_handler(event, None)
            self.assertEqual(res["status"], "SUCCESS")
            mock_execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()

