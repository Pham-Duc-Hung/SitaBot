"""Author: Trịnh Thiên Quang. Sprint 2 AWS connection API.

POST /connections/external-id, POST /connections/verify.
Table contract: AwsConnections partition key user_id (String).
"""
import os
import re
import secrets

import boto3
from botocore.exceptions import ClientError
from backend.shared.http_api import body, response, user_id


def lambda_handler(event, context):
    try:
        owner = user_id(event)
    except PermissionError:
        return response(401, {"error_code": "Unauthorized"})
    try:
        payload = body(event)
        path = event.get("rawPath") or event.get("path", "")
        if path.startswith("/api/"):
            path = path[4:]
        if path not in {"/connections/external-id", "/connections/verify"}:
            return response(404, {"error_code": "NotFound"})
        method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
        if method != "POST":
            return response(405, {"error_code": "MethodNotAllowed"})
        table = boto3.resource("dynamodb").Table(os.environ["AWS_CONNECTIONS_TABLE"])
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
            return response(200, {"external_id": external_id})
        role_arn = payload.get("role_arn", "")
        if not isinstance(role_arn, str) or not re.fullmatch(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]+", role_arn):
            raise ValueError("role_arn is invalid")
        connection = table.get_item(Key={"user_id": owner}, ConsistentRead=True).get("Item")
        if not connection:
            return response(409, {"error_code": "ExternalIdRequired"})
        # Never accept ExternalId or credentials supplied by the client.
        boto3.client("sts").assume_role(RoleArn=role_arn, ExternalId=connection["external_id"],
                                       RoleSessionName="satibot-connection-check", DurationSeconds=900)
        table.update_item(Key={"user_id": owner},
                          UpdateExpression="SET role_arn = :role, #s = :status",
                          ConditionExpression="external_id = :external",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":role": role_arn, ":status": "CONNECTED",
                                                     ":external": connection["external_id"]})
        return response(200, {"status": "CONNECTED", "role_arn": role_arn})
    except (ValueError, TypeError, UnicodeError):
        return response(400, {"error_code": "InvalidRequest"})
    except KeyError:
        return response(503, {"error_code": "ConfigurationError"})
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        return response(403 if code in {"AccessDenied", "AccessDeniedException"} else 502,
                        {"error_code": "ConnectionFailed"})
