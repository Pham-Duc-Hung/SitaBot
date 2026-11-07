"""Authenticated workflow task: load server-owned intent, claim, assume, execute once."""
import json

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from backend.shared.cloud_errors import BackendError, failure
from backend.shared.runtime import redact_sensitive
from backend.shared.tool_registry import validate_call
from backend.shared.workflow_store import Store, json_safe, connection_identity


def _execute(item, credentials):
    from backend.functions.dispatcher import index
    service = item["service"]
    parameters = item["parameters"]
    if service in {"ec2", "s3"}:
        # These teammate modules use flat parameters, unlike Lambda and IAM.
        params = {**parameters, "action": item["action"], "confirmed": True, "region": item["region"]}
    else:
        params = {"action": item["action"], "parameters": parameters, "confirmed": True, "region": item["region"]}
    module = {"ec2": index.ec2_service, "s3": index.s3_service, "lambda": index.lambda_service, "iam": index.iam_service}[service]
    return module.execute(credentials, params)


def normalize(raw):
    if not isinstance(raw, dict):
        return {"status": "ERROR", "error_code": "InvalidServiceResponse", "message": "Service trả kết quả sai cấu trúc.", "data": {}}
    status = raw.get("status") or ("SUCCESS" if raw.get("success", raw.get("ok", False)) else "ERROR")
    result = redact_sensitive({"status": status, "message": raw.get("message") or ("Đã thực thi yêu cầu AWS." if status == "SUCCESS" else "AWS chưa hoàn tất yêu cầu."),
                              "data": raw.get("data", {}), "error_code": raw.get("error_code") or raw.get("error")})
    result["message"] = str(result["message"])[:8000]
    error = result.get("error_code")
    if isinstance(error, dict):
        error = error.get("code") or error.get("Code") or "ServiceError"
    result["error_code"] = str(error)[:128] if error else None
    result = json_safe(result)
    if len(json.dumps(result, default=str).encode()) > 96000:
        result["data"] = {"truncated": True, "message": "Kết quả quá lớn; tra cứu tài nguyên để xem chi tiết."}
    return result


def envelope(item, result):
    return {key: item[key] for key in ("user_id", "session_id", "action_id", "created_at", "service", "tool_name")} | {"result": result}


def handle(event, context):
    if not isinstance(event, dict) or "requestContext" in event or "body" in event:
        return failure(BackendError("InternalOnly", "Dispatcher chỉ nhận tác vụ nội bộ.", 403))
    owner, action_id = event.get("user_id"), event.get("action_id")
    if not isinstance(owner, str) or not owner or not isinstance(action_id, str) or not action_id:
        return failure(BackendError("InvalidPayload", "Thiếu user_id/action_id nội bộ."))
    store = Store()
    try:
        item = store.get_action(owner, action_id)
        if item["status"] == "COMPLETED":
            return envelope(item, item["result"])
        tool = validate_call(item["tool_name"], item["parameters"])
        if item["service"] != tool["service"] or item["action"] != tool["action"] or item["requires_confirmation"] != tool["requires_confirmation"] or item.get("confirmation_mode") != tool.get("confirmation_mode", "standard"):
            raise BackendError("InvalidStoredAction", "Schema tác vụ đã thay đổi; hãy tạo yêu cầu mới.", 409)
        store.claim(item)
    except BackendError as exc:
        return failure(exc)
    except (ClientError, BotoCoreError):
        return failure(BackendError("ActionStoreUnavailable", "Không đối soát được tác vụ; chưa thực thi.", 503))
    try:
        connection = store.connection(owner)
        if connection_identity(connection) != item["connection_id"]:
            raise BackendError("ConnectionChanged", "Kết nối AWS đã thay đổi; hãy tạo và duyệt yêu cầu mới.", 409)
        credentials = boto3.client("sts", config=Config(retries={"max_attempts": 2})).assume_role(
            RoleArn=connection["role_arn"], ExternalId=connection["external_id"],
            RoleSessionName="satibot-" + action_id[-32:], DurationSeconds=900)["Credentials"]
        credentials = {**credentials, "Region": item["region"]}
    except BackendError as exc:
        result = failure(exc)
    except (ClientError, BotoCoreError):
        result = failure(BackendError("AwsConnectionUnavailable", "Không thể lấy quyền AWS; chưa thực thi tác vụ.", 502))
    else:
        # A transport failure can happen after mutation. Never retry a service here.
        try:
            result = normalize(_execute(item, credentials))
        except Exception:
            result = {"status": "ERROR", "error_code": "ExecutionOutcomeUnknown",
                      "message": "Không xác định được kết quả cuối; kiểm tra tài nguyên trước khi tạo tác vụ mới.", "data": {}}
    # A failed completion write leaves EXECUTING, deliberately preventing replay.
    store.finish(item, result)
    return envelope(item, result)
