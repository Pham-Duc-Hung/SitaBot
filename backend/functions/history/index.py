"""Author: Trịnh Thiên Quang. Owner-scoped history query/detail APIs."""
import base64
import json
import os
import re

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from backend.shared.http_api import response, user_id

SUMMARY = {"action_id", "created_at", "service", "tool_name", "status", "message", "resource_id", "session_id", "publication_status", "formatter_fallback", "error_code"}


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
        raise ValueError("Invalid UTC timestamp")
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="microseconds").replace("+00:00", "Z")


def lambda_handler(event, context):
    try:
        owner = user_id(event)
    except (PermissionError, TypeError, AttributeError):
        return response(401, {"error_code": "Unauthorized"})
    try:
        if os.environ.get("SATIBOT_MODE") == "cloud" or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
            if method != "GET":
                return response(405, {"error_code": "MethodNotAllowed"})
        params = event.get("queryStringParameters") or {}
        table = boto3.resource("dynamodb").Table(os.environ["ACTION_HISTORY_TABLE"])
        if params.get("created_at"):
            created_at = timestamp(params["created_at"])
            item = table.get_item(Key={"user_id": owner, "created_at": created_at}, ConsistentRead=True).get("Item")
            if not item:
                return response(404, {"error_code": "HistoryNotFound"})
            from backend.shared.runtime import redact_sensitive
            return response(200, {"item": redact_sensitive({k: v for k, v in item.items() if k in SUMMARY | {"data"}})})
        limit = int(params.get("limit", "20"))
        if not 1 <= limit <= 100:
            raise ValueError("limit")
        condition = Key("user_id").eq(owner)
        if params.get("from") and params.get("to"):
            start, end = timestamp(params["from"]), timestamp(params["to"])
            if start > end:
                raise ValueError("range")
            condition &= Key("created_at").between(start, end)
        elif params.get("from"):
            condition &= Key("created_at").gte(timestamp(params["from"]))
        elif params.get("to"):
            condition &= Key("created_at").lte(timestamp(params["to"]))
        query = {"KeyConditionExpression": condition, "Limit": limit, "ScanIndexForward": False}
        filters = []
        for name in ("service", "status", "session_id"):
            if params.get(name):
                if not isinstance(params[name], str) or len(params[name]) > 128:
                    raise ValueError("filter")
                filters.append(Attr(name).eq(params[name]))
        if filters:
            expr = filters[0]
            for item in filters[1:]:
                expr &= item
            query["FilterExpression"] = expr
        if params.get("cursor"):
            if len(params["cursor"]) > 2048:
                raise ValueError("cursor")
            cursor = json.loads(base64.b64decode(params["cursor"], altchars=b"-_", validate=True))
            if not isinstance(cursor, dict) or set(cursor) != {"user_id", "created_at"} or cursor["user_id"] != owner or not isinstance(cursor["created_at"], str):
                raise ValueError("cursor")
            query["ExclusiveStartKey"] = cursor
        result = table.query(**query)
        cursor = result.get("LastEvaluatedKey")
        items = [{k: v for k, v in item.items() if k in SUMMARY} for item in result.get("Items", [])]
        return response(200, {"items": items, "cursor": base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode() if cursor else None})
    except (ValueError, TypeError, UnicodeError):
        return response(400, {"error_code": "InvalidQuery"})
    except KeyError:
        return response(503, {"error_code": "ConfigurationError"})
    except ClientError:
        return response(502, {"error_code": "HistoryUnavailable"})
