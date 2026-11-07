"""
Author: Trịnh Thiên Quang
Kiểm thử luồng ChatOps Sprint 3 với DynamoDB và AWS giả lập.
Bao gồm xác nhận, chống thực thi lặp, Gemini, lịch sử và AppSync.
"""

import io
import json
import time
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from backend.functions.chatbot import cloud_handler as chat
from backend.functions.dispatcher import cloud_handler as dispatch
from backend.functions.formatter import cloud_handler as formatter
from backend.functions.history.index import lambda_handler as history_handler
from backend.shared import appsync, gemini
from backend.shared.cloud_errors import BackendError
from backend.shared.tool_registry import llm_tools, registry, validate_call
from backend.shared.workflow_store import Store, table


@pytest.fixture
def cloud(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                       "AWS_DEFAULT_REGION": "ap-southeast-1", "AWS_EC2_METADATA_DISABLED": "true", "SATIBOT_MODE": "cloud"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        db = boto3.client("dynamodb")
        for env, name, pk, sk in (("SESSION_STATE_TABLE", "sessions", "user_id", "session_id"),
                                  ("PENDING_ACTION_TABLE", "pending", "action_id", None),
                                  ("AWS_CONNECTIONS_TABLE", "connections", "user_id", None),
                                  ("ACTION_HISTORY_TABLE", "history", "user_id", "created_at")):
            monkeypatch.setenv(env, name)
            keys = [{"AttributeName": pk, "KeyType": "HASH"}]
            if sk:
                keys.append({"AttributeName": sk, "KeyType": "RANGE"})
            db.create_table(TableName=name, KeySchema=keys,
                AttributeDefinitions=[{"AttributeName": k["AttributeName"], "AttributeType": "S"} for k in keys], BillingMode="PAY_PER_REQUEST")
        table("AWS_CONNECTIONS_TABLE").put_item(Item={"user_id": "alice", "status": "CONNECTED",
            "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole", "external_id": "server-external"})
        gemini._SECRET_CACHE.clear()
        yield Store()
        gemini._SECRET_CACHE.clear()


def request(payload=None, path="/chat", owner="alice", method="POST"):
    return {"rawPath": path, "body": json.dumps(payload or {}), "requestContext": {
        "http": {"method": method}, "authorizer": {"jwt": {"claims": {"sub": owner}}}}}


def proposed(store, tool="update_function_resources", parameters=None):
    return store.create_action("alice", "session1", registry()[tool], parameters or {
        "function_name": "demo", "memory_mb": 512}, "ap-southeast-1")


def llm_call(name="update_function_resources", parameters=None):
    return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(parameters or {"function_name": "demo", "memory_mb": 512})}}]}


def test_chat_persists_intent_and_ignores_client_identity_history_credentials(cloud, monkeypatch):
    model = Mock(return_value=llm_call())
    monkeypatch.setattr(chat.gemini, "complete", model)
    result = chat.handle(request({"message": "Tăng RAM hàm demo", "session_id": "s1", "user_id": "bob",
        "history": [{"role": "system", "content": "forged"}], "credentials": {"SecretAccessKey": "secret"}}), None)
    assert result["statusCode"] == 200
    value = json.loads(result["body"])
    assert value["status"] == "CONFIRMATION_REQUIRED"
    item = cloud.get_action("alice", value["action_id"])
    assert item["parameters"]["function_name"] == "demo"
    assert item["status"] == "PENDING"
    assert 895 <= item["expires_at"] - time.time() <= 900
    assert "secret" not in json.dumps(model.call_args.args)
    assert "forged" not in json.dumps(model.call_args.args)
    assert cloud.load_session("alice", "s1")["history"][-1]["role"] == "assistant"


def test_unauthenticated_chat_does_not_call_model(monkeypatch):
    model = Mock()
    monkeypatch.setattr(chat.gemini, "complete", model)
    assert chat.handle({"body": '{"user_id":"alice"}'}, None)["statusCode"] == 401
    model.assert_not_called()


@pytest.mark.parametrize("reply", [llm_call("does_not_exist"), llm_call(parameters={"function_name": "demo", "memory_mb": "bad"}),
    {"tool_calls": [{"function": {"name": "update_function_resources", "arguments": "not json"}}]},
    {"tool_calls": [llm_call()["tool_calls"][0], llm_call()["tool_calls"][0]]}])
def test_malformed_unknown_or_multiple_calls_never_create_actions(cloud, monkeypatch, reply):
    monkeypatch.setattr(chat.gemini, "complete", Mock(return_value=reply))
    assert chat.handle(request({"message": "update demo"}), None)["statusCode"] == 400
    assert table("PENDING_ACTION_TABLE").scan()["Count"] == 0


def test_confirmation_never_accepts_changed_client_parameters(cloud, monkeypatch):
    item = proposed(cloud)
    started = Mock(return_value={"status": "ACCEPTED"})
    monkeypatch.setattr(chat, "start_workflow", started)
    result = chat.handle(request({"confirmed": True, "action_id": item["action_id"],
        "parameters": {"function_name": "victim"}, "service": "iam", "credentials": {"SecretAccessKey": "forged"}}, "/confirm"), None)
    assert result["statusCode"] == 202
    assert started.call_args.args[0]["parameters"]["function_name"] == "demo"
    assert cloud.get_action("alice", item["action_id"])["approved"] is True


def test_cross_user_confirmation_hidden(cloud):
    item = proposed(cloud)
    result = chat.handle(request({"confirmed": True, "action_id": item["action_id"]}, "/confirm", "bob"), None)
    assert result["statusCode"] == 404
    assert not cloud.get_action("alice", item["action_id"]).get("approved")


def test_expired_action_rejected_even_before_ttl_deletion(cloud):
    item = proposed(cloud)
    table("PENDING_ACTION_TABLE").update_item(Key={"action_id": item["action_id"]},
        UpdateExpression="SET expires_at = :t", ExpressionAttributeValues={":t": int(time.time()) - 1})
    with pytest.raises(BackendError, match="hết hạn"):
        cloud.approve("alice", item["action_id"])


def test_session_optimistic_lock_prevents_lost_turns(cloud):
    initial = cloud.load_session("alice", "same")
    cloud.save_session("alice", "same", initial, [{"role": "user", "content": "first"}])
    with pytest.raises(BackendError) as error:
        cloud.save_session("alice", "same", initial, [{"role": "user", "content": "second"}])
    assert error.value.code == "SessionConflict"
    assert cloud.load_session("alice", "same")["history"][0]["content"] == "first"


def test_action_claim_requires_approval_and_allows_one_worker(cloud):
    item = proposed(cloud)
    with pytest.raises(BackendError):
        cloud.claim(item)
    item = cloud.approve("alice", item["action_id"])
    cloud.claim(item)
    with pytest.raises(BackendError):
        cloud.claim(item)


def test_dispatch_executes_saved_parameters_with_sts_and_caches_result(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    execute = Mock(return_value={"status": "SUCCESS", "data": {"function_name": "demo", "SecretAccessKey": "never-leak"}})
    monkeypatch.setattr(dispatch, "_execute", execute)
    payload = {"user_id": "alice", "action_id": item["action_id"], "parameters": {"function_name": "forged"}}
    first = dispatch.handle(payload, None)
    second = dispatch.handle(payload, None)
    assert first == second
    execute.assert_called_once()
    credentials = execute.call_args.args[1]
    assert credentials["AccessKeyId"].startswith("ASIA")
    assert credentials["Region"] == "ap-southeast-1"
    assert execute.call_args.args[0]["parameters"]["function_name"] == "demo"
    assert "never-leak" not in json.dumps(first)


def test_dispatch_unapproved_never_calls_service(cloud, monkeypatch):
    item = proposed(cloud)
    execute = Mock()
    monkeypatch.setattr(dispatch, "_execute", execute)
    assert dispatch.handle({"user_id": "alice", "action_id": item["action_id"]}, None)["error_code"] == "ActionConflict"
    execute.assert_not_called()


def test_dispatch_unknown_outcome_is_not_replayed(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    execute = Mock(side_effect=TimeoutError("request may have reached AWS"))
    monkeypatch.setattr(dispatch, "_execute", execute)
    payload = {"user_id": "alice", "action_id": item["action_id"]}
    for _ in range(2):
        assert dispatch.handle(payload, None)["result"]["error_code"] == "ExecutionOutcomeUnknown"
    execute.assert_called_once()


def test_completion_write_failure_keeps_lock(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    execute = Mock(return_value={"status": "SUCCESS"})
    monkeypatch.setattr(dispatch, "_execute", execute)
    monkeypatch.setattr(Store, "finish", Mock(side_effect=RuntimeError("db down")))
    payload = {"user_id": "alice", "action_id": item["action_id"]}
    with pytest.raises(RuntimeError):
        dispatch.handle(payload, None)
    assert dispatch.handle(payload, None)["error_code"] == "ActionConflict"
    execute.assert_called_once()


def test_formatter_fallback_history_and_replay_do_not_duplicate(cloud, monkeypatch):
    item = proposed(cloud)
    event = dispatch.envelope(item, {"status": "SUCCESS", "data": {"id": "real", "SecretAccessKey": "private"}, "message": "AWS completed"})
    monkeypatch.setattr(formatter.gemini, "complete", Mock(side_effect=BackendError("GeminiUnavailable", "down")))
    publisher = Mock()
    monkeypatch.setattr(formatter.appsync, "publish", publisher)
    first = formatter.handle(event, None)
    second = formatter.handle(event, None)
    assert first["formatter_fallback"] is True
    assert first["status"] == "SUCCESS"
    assert first["publication_status"] == "PUBLISHED"
    assert second["data"]["id"] == "real"
    assert table("ACTION_HISTORY_TABLE").scan()["Count"] == 1
    publisher.assert_called_once()
    assert "private" not in json.dumps(first)


def test_publication_failure_can_retry_formatter_without_executing_aws(cloud, monkeypatch):
    item = proposed(cloud)
    monkeypatch.setattr(formatter.gemini, "complete", Mock(return_value={"content": "Đã xử lý."}))
    publish = Mock(side_effect=[RuntimeError("offline"), None])
    monkeypatch.setattr(formatter.appsync, "publish", publish)
    event = dispatch.envelope(item, {"status": "ERROR", "error_code": "AccessDenied", "data": {"id": "actual"}})
    assert formatter.handle(event, None)["publication_status"] == "FAILED"
    result = formatter.handle(event, None)
    assert result["publication_status"] == "PUBLISHED"
    assert result["status"] == "ERROR"
    assert result["error_code"] == "AccessDenied"
    assert table("ACTION_HISTORY_TABLE").scan()["Count"] == 1


def test_history_detail_is_scoped_and_filtered(cloud):
    item = proposed(cloud)
    table("ACTION_HISTORY_TABLE").put_item(Item={"user_id": "alice", "created_at": item["created_at"],
        "service": "lambda", "status": "SUCCESS", "session_id": "s1", "action_id": item["action_id"], "data": {"id": "real"}})
    event = request(path="/history", method="GET")
    event["queryStringParameters"] = {"created_at": item["created_at"]}
    assert json.loads(history_handler(event, None)["body"])["item"]["data"]["id"] == "real"
    event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = "bob"
    assert history_handler(event, None)["statusCode"] == 404
    event = request(path="/history", method="GET")
    event["queryStringParameters"] = {"service": "ec2"}
    assert json.loads(history_handler(event, None)["body"])["items"] == []


def test_stepfunctions_input_is_deterministic_and_contains_no_parameters(cloud, monkeypatch):
    monkeypatch.setenv("ACTION_STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-1:123456789012:stateMachine:actions")
    client = Mock()
    monkeypatch.setattr(chat.boto3, "client", Mock(return_value=client))
    item = proposed(cloud)
    chat.start_workflow(item)
    first = client.start_execution.call_args.kwargs
    chat.start_workflow(item)
    assert first == client.start_execution.call_args.kwargs
    assert json.loads(first["input"]) == {"user_id": "alice", "action_id": item["action_id"]}
    assert first["name"] == item["action_id"]


def test_gemini_reads_secret_and_retries_without_logging_key(cloud, monkeypatch):
    secret = boto3.client("secretsmanager").create_secret(Name="gemini", SecretString='{"api_key":"private-key"}')
    monkeypatch.setenv("GEMINI_SECRET_ARN", secret["ARN"])
    assert gemini.get_api_key() == "private-key"
    import urllib.error
    reply = Mock()
    reply.__enter__ = Mock(return_value=reply)
    reply.__exit__ = Mock(return_value=False)
    reply.read.return_value = b'{"choices":[{"message":{"content":"hello"}}]}'
    network = Mock(side_effect=[urllib.error.HTTPError(gemini.ENDPOINT, 429, "limit", {}, io.BytesIO(b"private-key")), reply])
    monkeypatch.setattr(gemini.urllib.request, "urlopen", network)
    monkeypatch.setattr(gemini.time, "sleep", Mock())
    assert gemini.complete([{"role": "user", "content": "hello", "credentials": {"SecretAccessKey": "aws-secret"}}])["content"] == "hello"
    assert network.call_count == 2
    assert b"aws-secret" not in network.call_args.args[0].data


def test_gemini_missing_secret_never_falls_back_to_mock(cloud, monkeypatch):
    monkeypatch.delenv("GEMINI_SECRET_ARN", raising=False)
    with pytest.raises(BackendError) as exc:
        gemini.complete([{"role": "user", "content": "hello"}])
    assert exc.value.code == "GeminiNotConfigured"


def test_tool_adapter_preserves_s3_action_and_validates_arguments():
    tool = validate_call("s3_create_bucket", {"bucket_name": "test-bucket"})
    assert tool["action"] == "create_bucket"
    assert tool["service"] == "s3"
    assert any(t["function"]["name"] == "s3_create_bucket" for t in llm_tools())
    with pytest.raises(BackendError):
        validate_call("s3_create_bucket", {"bucket_name": "test-bucket", "confirmed": True})


def test_flat_service_adapter_preserves_service_contract(monkeypatch):
    from backend.functions.dispatcher import index
    execute = Mock(return_value={"ok": True})
    monkeypatch.setattr(index.s3_service, "execute", execute)
    dispatch._execute({"service": "s3", "action": "create_bucket", "parameters": {"bucket_name": "test-bucket"}, "region": "ap-southeast-1"}, {"Region": "ap-southeast-1"})
    params = execute.call_args.args[1]
    assert params["bucket_name"] == "test-bucket"
    assert params["action"] == "create_bucket"
    assert "parameters" not in params


def test_appsync_signs_and_rejects_graphql_errors(cloud, monkeypatch):
    monkeypatch.setenv("APPSYNC_GRAPHQL_URL", "https://example.appsync-api.ap-southeast-1.amazonaws.com/graphql")
    reply = Mock()
    reply.__enter__ = Mock(return_value=reply)
    reply.__exit__ = Mock(return_value=False)
    reply.read.return_value = b'{"errors":[{"message":"denied"}]}'
    network = Mock(return_value=reply)
    monkeypatch.setattr(appsync.urllib.request, "urlopen", network)
    with pytest.raises(BackendError):
        appsync.publish({"action_id": "a", "data": {"id": "real"}})
    req = network.call_args.args[0]
    assert "AWS4-HMAC-SHA256" in req.headers["Authorization"]
    assert "testing" not in req.data.decode()


def test_read_only_chat_runs_separate_tasks_and_persists_context(cloud, monkeypatch):
    from backend.functions.chatbot.index import lambda_handler as chatbot_entry
    from backend.functions.dispatcher.index import lambda_handler as dispatcher_entry
    from backend.functions.formatter.index import lambda_handler as formatter_entry
    execute = Mock(return_value={"status": "SUCCESS", "data": {"functions": [{"name": "demo"}]}})
    monkeypatch.setattr(dispatch, "_execute", execute)
    monkeypatch.setattr(formatter.appsync, "publish", Mock())
    model = Mock(side_effect=[llm_call("list_lambda_functions", {"max_items": 10}),
                              {"content": "Có một hàm demo."}, {"content": "Hàm vừa xem là demo."}])
    monkeypatch.setattr(chat.gemini, "complete", model)
    invoked = []

    def run_workflow(item):
        invoked.append("workflow")
        result = dispatcher_entry({"user_id": item["user_id"], "action_id": item["action_id"]}, None)
        formatter_entry(result, None)
        return {"status": "ACCEPTED", "action_id": item["action_id"], "session_id": item["session_id"]}

    monkeypatch.setattr(chat, "start_workflow", run_workflow)
    first = chatbot_entry(request({"message": "Liệt kê lambda", "session_id": "s1"}), None)
    data = json.loads(first["body"])
    assert first["statusCode"] == 202
    assert cloud.get_action("alice", data["action_id"])["result"]["data"]["functions"][0]["name"] == "demo"
    assert invoked == ["workflow"]
    assert table("ACTION_HISTORY_TABLE").scan()["Count"] == 1
    second = chatbot_entry(request({"message": "Hàm vừa xem tên gì?", "session_id": "s1"}), None)
    assert second["statusCode"] == 200
    assert "demo" in json.dumps(model.call_args.args)
    execute.assert_called_once()


def test_expired_previous_action_does_not_break_session(cloud, monkeypatch):
    cloud.save_session("alice", "s1", {}, [{"role": "user", "content": "before"}], action_id="deleted-by-ttl")
    monkeypatch.setattr(chat.gemini, "complete", Mock(return_value={"content": "Bạn muốn làm gì tiếp?"}))
    assert chat.handle(request({"message": "xin chào", "session_id": "s1"}), None)["statusCode"] == 200


def test_connection_missing_records_failure_without_service_call(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    table("AWS_CONNECTIONS_TABLE").delete_item(Key={"user_id": "alice"})
    execute = Mock()
    monkeypatch.setattr(dispatch, "_execute", execute)
    result = dispatch.handle({"user_id": "alice", "action_id": item["action_id"]}, None)
    assert result["result"]["error_code"] == "ConnectionRequired"
    assert cloud.get_action("alice", item["action_id"])["status"] == "COMPLETED"
    execute.assert_not_called()


def test_dispatch_uses_role_and_external_id_from_server_only(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    client_factory = boto3.client
    sts = Mock(wraps=client_factory("sts"))
    monkeypatch.setattr(dispatch.boto3, "client", lambda service, **kwargs: sts if service == "sts" else client_factory(service, **kwargs))
    monkeypatch.setattr(dispatch, "_execute", Mock(return_value={"status": "SUCCESS"}))
    dispatch.handle({"user_id": "alice", "action_id": item["action_id"], "role_arn": "forged", "external_id": "forged"}, None)
    assert sts.assume_role.call_args.kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole"
    assert sts.assume_role.call_args.kwargs["ExternalId"] == "server-external"


def test_session_retention_bounds_large_context(cloud):
    messages = [{"role": "user", "content": "x" * 9000} for _ in range(30)]
    cloud.save_session("alice", "s1", {}, messages)
    stored = cloud.load_session("alice", "s1")["history"]
    assert len(stored) <= 20
    assert len(json.dumps(stored).encode()) <= 49000
    assert all(len(m["content"]) <= 8000 for m in stored)


def test_cloud_dispatcher_and_formatter_reject_public_proxy_events(cloud):
    from backend.functions.dispatcher.index import lambda_handler as dispatch_entry
    from backend.functions.formatter.index import lambda_handler as format_entry
    for entry in (dispatch_entry, format_entry):
        assert entry(request({"user_id": "alice", "confirmed": True}), None)["error_code"] == "InternalOnly"


def test_ec2_read_through_real_service_adapter_with_mocked_aws(cloud):
    item = proposed(cloud, "list_ec2_instances", {"state_filter": "all"})
    # Exercises the actual EC2 service contract rather than replacing _execute.
    result = dispatch.handle({"user_id": "alice", "action_id": item["action_id"]}, None)
    assert result["result"]["status"] == "SUCCESS"
    assert cloud.get_action("alice", item["action_id"])["status"] == "COMPLETED"


def test_history_second_boundary_normalizes_utc_precision(cloud):
    table("ACTION_HISTORY_TABLE").put_item(Item={"user_id": "alice", "created_at": "2026-11-07T10:00:00.500000Z", "status": "SUCCESS"})
    event = request(path="/history", method="GET")
    event["queryStringParameters"] = {"from": "2026-11-07T10:00:00Z", "to": "2026-11-07T10:00:01Z"}
    assert len(json.loads(history_handler(event, None)["body"])["items"]) == 1


def test_non_finite_tool_arguments_never_create_action(cloud, monkeypatch):
    reply = {"tool_calls": [{"function": {"name": "update_function_resources", "arguments": '{"function_name":"demo","memory_mb":NaN}'}}]}
    monkeypatch.setattr(chat.gemini, "complete", Mock(return_value=reply))
    assert chat.handle(request({"message": "update"}), None)["statusCode"] == 400
    assert table("PENDING_ACTION_TABLE").scan()["Count"] == 0


def test_connection_change_after_approval_never_switches_execution_target(cloud, monkeypatch):
    item = proposed(cloud)
    cloud.approve("alice", item["action_id"])
    table("AWS_CONNECTIONS_TABLE").update_item(Key={"user_id": "alice"},
        UpdateExpression="SET role_arn = :r", ExpressionAttributeValues={":r": "arn:aws:iam::999999999999:role/other"})
    execute = Mock()
    monkeypatch.setattr(dispatch, "_execute", execute)
    result = dispatch.handle({"user_id": "alice", "action_id": item["action_id"]}, None)
    assert result["result"]["error_code"] == "ConnectionChanged"
    execute.assert_not_called()


def test_s3_explicit_phrase_cannot_be_supplied_only_by_model(cloud, monkeypatch):
    item = proposed(cloud, "s3_delete_bucket_safe", {"bucket_name": "test-bucket", "confirmation": "DELETE test-bucket", "expected_deletable_item_count": 0})
    started = Mock(return_value={"status": "ACCEPTED"})
    monkeypatch.setattr(chat, "start_workflow", started)
    payload = {"confirmed": True, "action_id": item["action_id"]}
    for text in (None, "DELETE other-bucket"):
        result = chat.handle(request({**payload, "confirmation_text": text}, "/confirm"), None)
        assert result["statusCode"] == 400
        assert not cloud.get_action("alice", item["action_id"]).get("approved")
    started.assert_not_called()
    result = chat.handle(request({**payload, "confirmation_text": "DELETE test-bucket"}, "/confirm"), None)
    assert result["statusCode"] == 202
    assert cloud.get_action("alice", item["action_id"])["approved"] is True
