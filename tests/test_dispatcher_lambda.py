"""
Author: Trịnh Thiên Quang
Kiểm thử bộ điều phối Dispatcher cho service Lambda.
"""

import json
import unittest
from unittest.mock import patch, MagicMock

import sys
import os

# Thêm path tới dispatcher
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend", "functions", "dispatcher")))

from index import lambda_handler


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

    def test_api_gateway_invoke_missing_service(self):
        event = {"body": json.dumps({"action": "list_lambda_functions"})}
        res = lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 400)
        body = json.loads(res["body"])
        self.assertEqual(body["status"], "ERROR")

    @patch("services.lambda_service.execute")
    def test_dispatch_to_lambda_service_success(self, mock_execute):
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

