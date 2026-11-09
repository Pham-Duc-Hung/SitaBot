"""
Author: Trịnh Thiên Quang
Chuẩn bị môi trường AWS giả lập cho kiểm thử Sprint 4.
"""

import boto3
from moto import mock_aws
import pytest
from backend.shared import gemini
from backend.shared.runtime import clear_runtime_caches
from backend.shared.tool_registry import clear_registry_cache
from backend.shared.workflow_store import Store, table


@pytest.fixture
def sprint4_cloud(monkeypatch):
    env_vars = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-1",
        "AWS_REGION": "ap-southeast-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "SATIBOT_MODE": "cloud",
        "APPSYNC_GRAPHQL_URL": "https://satibot.appsync-api.ap-southeast-1.amazonaws.com/graphql",
        "ACTION_STATE_MACHINE_ARN": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:satibot-actions",
    }
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)

    clear_runtime_caches()
    clear_registry_cache()
    gemini._SECRET_CACHE.clear()

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ap-southeast-1")
        for env_name, tbl_name, pk, sk in (
            ("SESSION_STATE_TABLE", "s4-sessions", "user_id", "session_id"),
            ("PENDING_ACTION_TABLE", "s4-pending", "action_id", None),
            ("AWS_CONNECTIONS_TABLE", "s4-connections", "user_id", None),
            ("ACTION_HISTORY_TABLE", "s4-history", "user_id", "created_at"),
        ):
            monkeypatch.setenv(env_name, tbl_name)
            schema = [{"AttributeName": pk, "KeyType": "HASH"}]
            if sk:
                schema.append({"AttributeName": sk, "KeyType": "RANGE"})
            ddb.create_table(
                TableName=tbl_name,
                KeySchema=schema,
                AttributeDefinitions=[
                    {"AttributeName": item["AttributeName"], "AttributeType": "S"}
                    for item in schema
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        secret = boto3.client(
            "secretsmanager", region_name="ap-southeast-1"
        ).create_secret(
            Name="s4/gemini",
            SecretString='{"GEMINI_API_KEY":"s4-secret-key"}',
        )
        monkeypatch.setenv("GEMINI_SECRET_ARN", secret["ARN"])
        table("AWS_CONNECTIONS_TABLE").put_item(
            Item={
                "user_id": "alice",
                "status": "CONNECTED",
                "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole",
                "account_id": "123456789012",
                "external_id": "alice-server-external-id-0000000000",
                "verified_at": "2026-11-08T01:00:00.000000Z",
            }
        )
        yield Store()

    clear_runtime_caches()
    clear_registry_cache()
    gemini._SECRET_CACHE.clear()
