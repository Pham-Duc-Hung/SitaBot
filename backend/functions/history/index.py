"""Author: Trịnh Thiên Quang. User-scoped, paginated history API.

Table contract: partition user_id, sort created_at (both String).
"""
import base64
import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from backend.shared.http_api import response, user_id


def lambda_handler(event, context):
    try:
        owner = user_id(event)
    except PermissionError:
        return response(401, {"error_code": "Unauthorized"})
    try:
        params = event.get("queryStringParameters") or {}
        limit = int(params.get("limit", "20"))
        if not 1 <= limit <= 100:
            raise ValueError("limit")
        query = {"KeyConditionExpression": Key("user_id").eq(owner), "Limit": limit,
                 "ScanIndexForward": False}
        if params.get("cursor"):
            cursor = json.loads(base64.b64decode(params["cursor"], altchars=b"-_", validate=True))
            if not isinstance(cursor, dict) or set(cursor) != {"user_id", "created_at"} or cursor["user_id"] != owner or not isinstance(cursor["created_at"], str):
                raise ValueError("cursor")
            query["ExclusiveStartKey"] = cursor
        table = boto3.resource("dynamodb").Table(os.environ["ACTION_HISTORY_TABLE"])
        result = table.query(**query)
        cursor = result.get("LastEvaluatedKey")
        # Explicit public fields keep stored credentials/internal metadata off HTTP.
        fields = {"action_id", "created_at", "service", "tool_name", "status", "message", "resource_id", "session_id"}
        items = [{k: v for k, v in item.items() if k in fields} for item in result.get("Items", [])]
        return response(200, {"items": items, "cursor": base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode() if cursor else None})
    except (ValueError, TypeError, UnicodeError):
        return response(400, {"error_code": "InvalidQuery"})
    except KeyError:
        return response(503, {"error_code": "ConfigurationError"})
    except ClientError:
        return response(502, {"error_code": "HistoryUnavailable"})
