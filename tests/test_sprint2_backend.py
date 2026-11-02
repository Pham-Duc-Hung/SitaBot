"""Quang's Sprint 2 API isolation and deployable package regression tests."""
import base64
import json
import zipfile
from unittest.mock import Mock

from botocore.exceptions import ClientError
from backend.functions.connector import index as connector
from backend.functions.history import index as history
from backend.functions.formatter import index as formatter
from scripts.package_sprint2 import package


def event(path="/connections/external-id", payload=None):
    return {"rawPath": path, "body": json.dumps(payload or {}),
            "requestContext": {"http": {"method": "POST"},
                               "authorizer": {"jwt": {"claims": {"sub": "alice"}}}}}


def setup_table(monkeypatch, module, env):
    table = Mock()
    resource = Mock()
    resource.Table.return_value = table
    monkeypatch.setenv(env, "test-table")
    monkeypatch.setattr(module.boto3, "resource", Mock(return_value=resource))
    return table


def test_unauthenticated_requests_do_not_access_aws():
    for module in (connector, history):
        assert module.lambda_handler({"body": '{"user_id":"alice"}'}, None)["statusCode"] == 401


def test_external_id_creation_is_user_scoped(monkeypatch):
    table = setup_table(monkeypatch, connector, "AWS_CONNECTIONS_TABLE")
    result = connector.lambda_handler(event(payload={"user_id": "bob", "external_id": "forged"}), None)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["user_id"] == "alice"
    assert len(item["external_id"]) >= 32
    assert json.loads(result["body"])["external_id"] == item["external_id"]


def test_external_id_is_stable(monkeypatch):
    table = setup_table(monkeypatch, connector, "AWS_CONNECTIONS_TABLE")
    table.put_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    table.get_item.return_value = {"Item": {"external_id": "saved"}}
    assert json.loads(connector.lambda_handler(event(), None)["body"])["external_id"] == "saved"


def test_verify_uses_stored_external_id_and_never_returns_credentials(monkeypatch):
    table = setup_table(monkeypatch, connector, "AWS_CONNECTIONS_TABLE")
    table.get_item.return_value = {"Item": {"external_id": "server-id"}}
    sts = Mock()
    sts.assume_role.return_value = {"Credentials": {"SecretAccessKey": "secret"}}
    monkeypatch.setattr(connector.boto3, "client", Mock(return_value=sts))
    result = connector.lambda_handler(event("/connections/verify", {
        "role_arn": "arn:aws:iam::123456789012:role/demo", "external_id": "forged"}), None)
    assert result["statusCode"] == 200
    assert sts.assume_role.call_args.kwargs["ExternalId"] == "server-id"
    assert "secret" not in result["body"]

    # Supports /api/ prefix as well
    api_result = connector.lambda_handler(event("/api/connections/verify", {
        "role_arn": "arn:aws:iam::123456789012:role/demo"}), None)
    assert api_result["statusCode"] == 200


def test_sts_denial_does_not_mark_connected(monkeypatch):
    table = setup_table(monkeypatch, connector, "AWS_CONNECTIONS_TABLE")
    table.get_item.return_value = {"Item": {"external_id": "server-id"}}
    sts = Mock()
    sts.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "private"}}, "AssumeRole")
    monkeypatch.setattr(connector.boto3, "client", Mock(return_value=sts))
    result = connector.lambda_handler(event("/connections/verify", {"role_arn": "arn:aws:iam::123456789012:role/demo"}), None)
    assert result["statusCode"] == 403
    table.update_item.assert_not_called()
    assert "private" not in result["body"]


def test_history_rejects_other_users_cursor(monkeypatch):
    table = setup_table(monkeypatch, history, "ACTION_HISTORY_TABLE")
    request = event()
    cursor = base64.urlsafe_b64encode(json.dumps({"user_id": "bob", "created_at": "today"}).encode()).decode()
    request["queryStringParameters"] = {"cursor": cursor}
    assert history.lambda_handler(request, None)["statusCode"] == 400
    table.query.assert_not_called()


def test_history_pagination_and_public_projection(monkeypatch):
    table = setup_table(monkeypatch, history, "ACTION_HISTORY_TABLE")
    key = {"user_id": "alice", "created_at": "2026-10-31"}
    table.query.return_value = {"Items": [{**key, "status": "SUCCESS", "credentials": "private"}], "LastEvaluatedKey": key}
    result = json.loads(history.lambda_handler(event(), None)["body"])
    assert "private" not in json.dumps(result)
    assert json.loads(base64.urlsafe_b64decode(result["cursor"])) == key
    request = event()
    request["queryStringParameters"] = {"cursor": result["cursor"], "limit": "5"}
    assert history.lambda_handler(request, None)["statusCode"] == 200
    assert table.query.call_args.kwargs["ExclusiveStartKey"] == key
    assert table.query.call_args.kwargs["Limit"] == 5


def test_formatter_redacts_credentials():
    result = formatter.lambda_handler({"result": {"status": "SUCCESS", "data": {"SecretAccessKey": "private"}}}, None)
    assert result["data"]["SecretAccessKey"] == "<redacted>"


def test_five_packages_contain_handlers_and_no_secrets(tmp_path):
    package(tmp_path)
    for name in ("chatbot", "dispatcher", "formatter", "connector", "history"):
        with zipfile.ZipFile(tmp_path / f"{name}.zip") as archive:
            files = archive.namelist()
            assert f"backend/functions/{name}/index.py" in files
            assert "backend/shared/runtime.py" in files
            assert "backend/__init__.py" in files
            assert f"backend/functions/{name}/__init__.py" in files
            assert not any(".env" in path or "__pycache__" in path for path in files)
