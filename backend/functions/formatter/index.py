"""Author: Trịnh Thiên Quang. Formatter entrypoint: Sprint 3 cloud flow and local compatibility response.
"""
import os
from backend.shared.runtime import redact_sensitive


def lambda_handler(event, context):
    if os.environ.get("SATIBOT_MODE") == "cloud" or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        from backend.functions.formatter.cloud_handler import handle
        return handle(event, context)
    if not isinstance(event, dict) or not isinstance(event.get("result"), dict):
        return {"status": "ERROR", "error_code": "InvalidPayload"}
    result = event["result"]
    return {"status": result.get("status", "ERROR"), "type": "action_result",
            "action_id": event.get("action_id"), "service": event.get("service"),
            "tool_name": event.get("tool_name"),
            "message": result.get("message") or "Đã nhận kết quả xử lý tác vụ.",
            "data": redact_sensitive(result.get("data", {}))}
