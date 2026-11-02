"""HTTP contracts for Quang's Connector and History Lambdas.

Identity comes exclusively from a Gateway authorizer, never from request JSON.
"""
import base64
import json
from decimal import Decimal


def response(code, data):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(data, ensure_ascii=False, default=lambda x: float(x) if isinstance(x, Decimal) else str(x))}


def user_id(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    claims = authorizer.get("jwt", {}).get("claims", {}) or authorizer.get("claims", {})
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise PermissionError("Missing verified Cognito subject")
    return subject


def body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True).decode("utf-8")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("Body must be a JSON object")
    return value
