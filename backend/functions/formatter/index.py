"""Author: Trịnh Thiên Quang. Internal Formatter contract for Sprint 2.

Gemini, ActionHistory writes and AppSync publication belong to Sprint 3.
"""
from backend.shared.runtime import redact_sensitive


def lambda_handler(event, context):
    if not isinstance(event, dict) or not isinstance(event.get("result"), dict):
        return {"status": "ERROR", "error_code": "InvalidPayload"}
    result = event["result"]
    return {"status": result.get("status", "ERROR"), "type": "action_result",
            "action_id": event.get("action_id"), "service": event.get("service"),
            "tool_name": event.get("tool_name"),
            "message": result.get("message") or "Đã nhận kết quả xử lý tác vụ.",
            "data": redact_sensitive(result.get("data", {}))}
