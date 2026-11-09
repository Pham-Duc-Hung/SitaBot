"""
Author: Trịnh Thiên Quang
Tạo ExternalId và kiểm tra kết nối tài khoản AWS qua STS.
Lưu và trả trạng thái kết nối của từng người dùng.
"""

import os
import re
import secrets

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from backend.shared.runtime import (
    cached_boto_client,
    cached_dynamodb_table,
    emit_platform_fault,
    is_access_denied,
)
from backend.shared.workflow_store import utc_now
from backend.shared.http_api import body, response, user_id

_STS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"})


def _trust_setup(external_id):
    """Describe the exact roles allowed to assume a customer role."""
    connector_arn = os.environ.get("SATIBOT_CONNECTOR_ROLE_ARN", "").strip()
    dispatcher_arn = os.environ.get("SATIBOT_DISPATCHER_ROLE_ARN", "").strip()
    if not connector_arn or not dispatcher_arn:
        return {}
    return {
        "role_path": "satibot/",
        "role_name": "SatiBotAccessRole",
        "trust_policy": {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": [connector_arn, dispatcher_arn]},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
            }],
        },
    }


def lambda_handler(event, context):
    try:
        owner = user_id(event)
    except (PermissionError, TypeError, AttributeError):
        return response(401, {"error_code": "Unauthorized"})
    stage = "dynamodb"
    try:
        payload = body(event)
        path = (event.get("rawPath") or event.get("path", "")).removeprefix("/api")
        method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
        if path == "/connections" and method == "GET":
            table = cached_dynamodb_table(boto3.resource, os.environ["AWS_CONNECTIONS_TABLE"])
            item = table.get_item(Key={"user_id": owner}, ConsistentRead=True).get("Item", {})
            return response(200, {k: v for k, v in item.items() if k in {"status", "role_arn", "account_id", "verified_at"}})
        if path not in {"/connections/external-id", "/connections/verify"}:
            return response(404, {"error_code": "NotFound"})
        if method != "POST":
            return response(405, {"error_code": "MethodNotAllowed"})
        table = cached_dynamodb_table(boto3.resource, os.environ["AWS_CONNECTIONS_TABLE"])
        if path.endswith("/external-id"):
            # Conditional creation makes repeated onboarding requests stable.
            external_id = secrets.token_urlsafe(32)
            try:
                table.put_item(Item={"user_id": owner, "external_id": external_id, "status": "PENDING"},
                               ConditionExpression="attribute_not_exists(user_id)")
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                external_id = table.get_item(Key={"user_id": owner}, ConsistentRead=True)["Item"]["external_id"]
            return response(200, {"external_id": external_id, **_trust_setup(external_id)})
        role_arn = payload.get("role_arn", "")
        if not isinstance(role_arn, str) or not re.fullmatch(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]+", role_arn):
            raise ValueError("role_arn is invalid")
        connection = table.get_item(Key={"user_id": owner}, ConsistentRead=True).get("Item")
        if not connection:
            return response(409, {"error_code": "ExternalIdRequired"})
        # Never accept ExternalId or credentials supplied by the client.
        stage = "customer_sts"
        cached_boto_client(boto3.client, "sts", config=_STS_CONFIG, config_tag="connector-sts").assume_role(
            RoleArn=role_arn, ExternalId=connection["external_id"],
            RoleSessionName="satibot-connection-check", DurationSeconds=900)
        stage = "dynamodb"
        table.update_item(Key={"user_id": owner},
                          UpdateExpression="SET role_arn = :role, #s = :status, account_id = :account, verified_at = :verified",
                          ConditionExpression="external_id = :external",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":role": role_arn, ":status": "CONNECTED",
                                                     ":external": connection["external_id"], ":account": role_arn.split(":")[4], ":verified": utc_now()})
        return response(200, {"status": "CONNECTED", "role_arn": role_arn})
    except (ValueError, TypeError, UnicodeError):
        return response(400, {"error_code": "InvalidRequest"})
    except KeyError:
        emit_platform_fault("connector", "ConfigurationError", context=context)
        return response(503, {"error_code": "ConfigurationError"})
    except ClientError as exc:
        if stage == "dynamodb":
            emit_platform_fault("connector", "AccessDenied" if is_access_denied(exc) else "ConnectionStoreUnavailable", context=context)
        return response(403 if is_access_denied(exc) else 502,
                        {"error_code": "ConnectionFailed"})
    except BotoCoreError:
        if stage == "dynamodb":
            emit_platform_fault("connector", "ConnectionUnavailable", context=context)
        return response(502, {"error_code": "ConnectionUnavailable"})
