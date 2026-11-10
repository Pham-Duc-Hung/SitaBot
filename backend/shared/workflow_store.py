"""
Author: Trịnh Thiên Quang
Lưu phiên hội thoại và hành động chờ trong DynamoDB.
Kiểm tra thời hạn, xác nhận và khóa tác vụ bằng conditional write.
"""

import json
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from backend.shared.cloud_errors import BackendError
from backend.shared.runtime import cached_dynamodb_table, redact_sensitive


def pack(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, allow_nan=False), parse_float=Decimal)


def json_safe(value):
    return json.loads(json.dumps(value, default=lambda x: int(x) if isinstance(x, Decimal) and x % 1 == 0 else float(x) if isinstance(x, Decimal) else str(x)))


def table(env):
    name = os.environ.get(env)
    if not name:
        raise BackendError("BackendNotConfigured", f"Chưa cấu hình {env}.", 503)
    return cached_dynamodb_table(boto3.resource, name)


def connection_identity(connection):
    value = connection["role_arn"] + "\0" + connection["external_id"]
    return hashlib.sha256(value.encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    def load_session(self, owner, session):
        item = table("SESSION_STATE_TABLE").get_item(Key={"user_id": owner, "session_id": session}, ConsistentRead=True).get("Item", {})
        if int(item.get("expires_at", 0)) <= int(time.time()):
            return {"version": int(item.get("version", 0)), "history": []}
        return item

    def save_session(self, owner, session, previous, messages, action_id=None):
        version = int(previous.get("version", 0))
        history = []
        size = 0
        for message in reversed(messages[-20:]):
            if message.get("role") not in {"user", "assistant"} or not isinstance(message.get("content"), str):
                continue
            cleaned = {"role": message["role"], "content": message["content"][:8000]}
            size += len(json.dumps(cleaned).encode())
            if size > 48000:
                break
            history.insert(0, cleaned)
        try:
            table("SESSION_STATE_TABLE").put_item(
                Item={"user_id": owner, "session_id": session, "version": version + 1,
                      "history": history, "last_action_id": action_id, "expires_at": int(time.time()) + 86400},
                ConditionExpression="attribute_not_exists(#v) OR #v = :v",
                ExpressionAttributeNames={"#v": "version"}, ExpressionAttributeValues={":v": version})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BackendError("SessionConflict", "Phiên vừa được cập nhật; hãy gửi lại yêu cầu.", 409) from None
            raise

    def create_action(self, owner, session, tool, parameters, region, *, delete_intent=None):
        now = int(time.time())
        connection = self.connection(owner)
        item = {"action_id": "act_" + uuid.uuid4().hex, "user_id": owner, "session_id": session,
                "service": tool["service"], "tool_name": tool["name"], "action": tool["action"],
                "parameters": parameters, "region": region, "target_role_arn": connection["role_arn"],
                "connection_id": connection_identity(connection), "confirmation_mode": tool.get("confirmation_mode", "standard"),
                "requires_confirmation": tool["requires_confirmation"],
                "status": "PENDING", "created_at": utc_now(), "expires_at": now + 900}
        if delete_intent is not None:
            item["delete_intent"] = delete_intent
        if len(json.dumps(item).encode()) > 150000:
            raise BackendError("ActionTooLarge", "Tham số tác vụ vượt giới hạn.")
        table("PENDING_ACTION_TABLE").put_item(Item=pack(item), ConditionExpression="attribute_not_exists(action_id)")
        return item

    def create_iam_confirmation(self, source, result):
        """Persist one immutable approval per preview execution, including on retry."""
        if int(source["expires_at"]) <= int(time.time()):
            raise BackendError("ActionExpired", "Preview đã hết hạn; hãy tạo yêu cầu mới.", 410)
        data = result.get("data", {})
        fingerprint = data.get("preview_fingerprint")
        if not isinstance(data.get("preview"), dict) or not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise BackendError("InvalidPreview", "IAM chưa trả preview và fingerprint đầy đủ.")
        intent = source.get("delete_intent") or {
            "action": source["action"], "tool_name": source["tool_name"],
            "parameters": source["parameters"]}
        if intent["action"] not in {"delete_role", "delete_policy"}:
            raise BackendError("InvalidPreview", "Preview không thuộc tác vụ xóa IAM.")
        action_id = "act_" + uuid.uuid5(uuid.NAMESPACE_URL, source["action_id"] + ":iam-delete").hex
        item = {key: source[key] for key in (
            "user_id", "session_id", "region", "target_role_arn", "connection_id", "expires_at")}
        item.update(action_id=action_id, service="iam", action=intent["action"],
                    tool_name=intent["tool_name"], parameters=intent["parameters"],
                    preview_action_id=source["action_id"], preview=data["preview"],
                    preview_fingerprint=fingerprint, warnings=result.get("warnings", []),
                    requires_confirmation=True, confirmation_mode="standard",
                    status="PENDING", created_at=utc_now())
        if len(json.dumps(item).encode()) > 150000:
            raise BackendError("ActionTooLarge", "Preview quá lớn; chưa thể yêu cầu xác nhận.")
        try:
            table("PENDING_ACTION_TABLE").put_item(Item=pack(item), ConditionExpression="attribute_not_exists(action_id)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            item = self.get_action(source["user_id"], action_id)
            if item.get("preview_action_id") != source["action_id"] or item["connection_id"] != source["connection_id"]:
                raise BackendError("ActionConflict", "Không đối soát được preview đã lưu.", 409) from None
        return json_safe(item)

    def get_action(self, owner, action_id):
        item = table("PENDING_ACTION_TABLE").get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item")
        if not item or item.get("user_id") != owner:
            raise BackendError("ActionNotFound", "Không tìm thấy tác vụ của bạn.", 404)
        return json_safe(item)

    def approve(self, owner, action_id, confirmation_text=None):
        item = self.get_action(owner, action_id)
        if item["status"] in {"COMPLETED", "EXECUTING"}:
            return item
        if int(item["expires_at"]) <= int(time.time()):
            raise BackendError("ActionExpired", "Tác vụ đã hết hạn; hãy tạo yêu cầu mới.", 410)
        if item["service"] == "iam" and item["action"] in {"delete_role", "delete_policy"}:
            if not item.get("preview") or not item.get("preview_fingerprint"):
                raise BackendError("PreviewRequired", "Phải xem preview trước khi xác nhận xóa.", 409)
        if item.get("confirmation_mode") == "explicit_phrase":
            expected = "DELETE " + item["parameters"].get("bucket_name", "")
            if confirmation_text != expected or item["parameters"].get("confirmation") != expected:
                raise BackendError("ExplicitConfirmationRequired", "Cần nhập đúng câu xác nhận xóa bucket.")
        try:
            table("PENDING_ACTION_TABLE").update_item(Key={"action_id": action_id},
                UpdateExpression="SET approved = :yes",
                ConditionExpression="user_id = :u AND #s = :pending AND expires_at > :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":u": owner, ":pending": "PENDING", ":now": int(time.time()), ":yes": True})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BackendError("ActionConflict", "Trạng thái tác vụ vừa thay đổi.", 409) from None
            raise
        return {**item, "approved": True}

    def claim(self, item):
        if int(item["expires_at"]) <= int(time.time()):
            raise BackendError("ActionExpired", "Tác vụ đã hết hạn.", 410)
        try:
            condition = "user_id = :u AND #s = :pending AND expires_at > :now"
            values = {":u": item["user_id"], ":pending": "PENDING", ":now": int(time.time()), ":running": "EXECUTING"}
            if item.get("delete_intent") and item["service"] == "iam" and item["action"] in {"preview_delete_role", "preview_delete_policy"}:
                # Repeating this read is safe. Conditional creation below keeps
                # the first stored snapshot immutable for concurrent workers.
                condition = "user_id = :u AND (#s = :pending OR #s = :running) AND expires_at > :now"
            if item["requires_confirmation"]:
                condition += " AND approved = :yes"
                values[":yes"] = True
            if item["service"] == "iam" and item["action"] in {"delete_role", "delete_policy"}:
                if not item.get("preview") or not item.get("preview_fingerprint"):
                    raise BackendError("PreviewRequired", "Thiếu preview đã được duyệt.", 409)
                condition += " AND preview_fingerprint = :fingerprint"
                values[":fingerprint"] = item["preview_fingerprint"]
            table("PENDING_ACTION_TABLE").update_item(Key={"action_id": item["action_id"]},
                UpdateExpression="SET #s = :running", ConditionExpression=condition,
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BackendError("ActionConflict", "Tác vụ chưa được duyệt hoặc đã bắt đầu; không thực thi lại.", 409) from None
            raise

    def finish(self, item, result):
        table("PENDING_ACTION_TABLE").update_item(Key={"action_id": item["action_id"]},
            UpdateExpression="SET #s = :done, #r = :result",
            ConditionExpression="user_id = :u AND #s = :running",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues={":u": item["user_id"], ":running": "EXECUTING", ":done": "COMPLETED", ":result": pack(result)})

    def connection(self, owner):
        item = table("AWS_CONNECTIONS_TABLE").get_item(Key={"user_id": owner}, ConsistentRead=True).get("Item", {})
        if item.get("status") != "CONNECTED" or not item.get("role_arn") or not item.get("external_id"):
            raise BackendError("ConnectionRequired", "Hãy kết nối tài khoản AWS trước.", 409)
        return item
