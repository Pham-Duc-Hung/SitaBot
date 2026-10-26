from unittest.mock import patch

from backend.shared.runtime import get_aws_region, redact_sensitive, safe_json_for_log
from backend.functions.chatbot.index import _http_status_for_backend_result
from backend.functions.dispatcher.index import _http_status_for_result, lambda_handler
from backend.functions.dispatcher.services.lambda_service import execute


def test_redact_sensitive_nested_credentials():
    event = {
        "credentials": {
            "AccessKeyId": "AKIA-DEMO",
            "SecretAccessKey": "secret-value",
            "SessionToken": "session-value",
        },
        "headers": {"Authorization": "Bearer private"},
    }
    redacted = redact_sensitive(event)
    assert redacted["credentials"]["AccessKeyId"] == "AKIA-DEMO"
    assert redacted["credentials"]["SecretAccessKey"] == "<redacted>"
    assert redacted["credentials"]["SessionToken"] == "<redacted>"
    serialized = safe_json_for_log(event)
    assert "secret-value" not in serialized
    assert "session-value" not in serialized
    assert "Bearer private" not in serialized


def test_region_resolution_prefers_explicit_then_environment():
    with patch.dict("os.environ", {"AWS_REGION": "ap-southeast-1"}, clear=False):
        assert get_aws_region("eu-west-1") == "eu-west-1"
        assert get_aws_region() == "ap-southeast-1"


def test_dispatcher_validates_shared_service_contract():
    result = lambda_handler({"service": "lambda", "action": "list_lambda_functions", "parameters": []}, None)
    assert result["status"] == "ERROR"
    assert result["error_code"] == "InvalidParameters"


def test_lambda_execute_rejects_invalid_contract_before_aws_client_creation():
    result = execute(None, {"action": "list_lambda_functions", "parameters": [], "confirmed": True})
    assert result["status"] == "ERROR"
    assert result["error_code"] == "InvalidParameters"


def test_http_error_mapping():
    assert _http_status_for_result({"status": "ERROR", "error_code": "AccessDeniedException"}) == 403
    assert _http_status_for_result({"status": "ERROR", "error_code": "ResourceNotFoundException"}) == 404
    assert _http_status_for_result({"status": "ERROR", "error_code": "TooManyRequestsException"}) == 429
    assert _http_status_for_result({"status": "SUCCESS"}) == 200
    assert _http_status_for_backend_result({"status": "ERROR", "error_code": "AccessDeniedException"}) == 403
    assert _http_status_for_backend_result({"status": "ERROR", "error_code": "PythonSyntaxError"}) == 400
