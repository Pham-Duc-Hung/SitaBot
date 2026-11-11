"""
Author: Trịnh Thiên Quang
Dữ liệu và request dùng chung cho kiểm thử các luồng đám mây.
"""

import json
from botocore.exceptions import ClientError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def deny_error(
    operation: str = "GetItem", code: str = "AccessDeniedException"
) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": f"User: arn:aws:sts::123456789012:assumed-role/private-role is not authorized to perform: {operation}",
            }
        },
        operation,
    )


def api_event(
    path: str, payload=None, *, owner: str = "alice", method: str = "POST", query=None
) -> dict:
    return {
        "rawPath": path,
        "body": json.dumps(payload) if payload is not None else "",
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": {"sub": owner}}},
        },
    }
