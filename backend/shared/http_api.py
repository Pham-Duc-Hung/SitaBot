"""
Author: Trịnh Thiên Quang
Đọc danh tính từ Gateway authorizer, kiểm tra JSON và tạo phản hồi HTTP.
"""

import base64
import json
import os
from decimal import Decimal


def response(code, data):
    headers = {"Content-Type": "application/json"}
    origin = os.environ.get("FRONTEND_ORIGIN", "")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
    return {"statusCode": code, "headers": headers,
            "body": json.dumps(data, ensure_ascii=False, default=lambda x: float(x) if isinstance(x, Decimal) else str(x))}


def user_id(event):
    context = event.get("requestContext") if isinstance(event, dict) else None
    authorizer = context.get("authorizer") if isinstance(context, dict) else None
    authorizer = authorizer if isinstance(authorizer, dict) else {}
    jwt = authorizer.get("jwt")
    claims = jwt.get("claims") if isinstance(jwt, dict) else None
    claims = claims or authorizer.get("claims")
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        raise PermissionError("Missing verified Cognito subject")
    return subject


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True).decode("utf-8")
    value = json.loads(raw, parse_constant=reject_constant) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("Body must be a JSON object")
    return value
