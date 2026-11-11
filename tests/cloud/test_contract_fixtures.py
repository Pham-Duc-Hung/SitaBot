"""Keep Quang's request/response examples aligned with the backend contracts."""

import json

from backend.functions.chatbot import cloud_handler as chat
from backend.functions.connector import index as connector
from backend.functions.formatter.cloud_handler import PUBLIC
from backend.functions.history import index as history
from backend.shared import appsync
from backend.shared.workflow_store import table
from .helpers import ROOT, api_event


def examples():
    data = json.loads((ROOT / "tests/fixtures/backend_contracts.json").read_text(encoding="utf-8"))
    return data, {item["name"]: item for item in data["http"]}


def test_http_contract_examples_match_live_handlers(cloud_store, monkeypatch):
    _, cases = examples()
    assert {"chat_reply", "chat_confirmation", "confirm_accepted", "connection_external_id",
            "connection_verified", "connection_missing_external_id", "connection_role_denied",
            "connection_status", "history_list"} == set(cases)

    chat_case = cases["chat_reply"]
    monkeypatch.setattr(chat.gemini, "complete", lambda *_args, **_kwargs: {"content": "Chào bạn."})
    chat_response = chat.handle(api_event(chat_case["request"]["path"], chat_case["request"]["body"]), None)
    assert chat_response["statusCode"] == chat_case["response"]["statusCode"]
    assert json.loads(chat_response["body"]) == chat_case["response"]["body"]

    connection_case = cases["connection_status"]
    connection_response = connector.lambda_handler(api_event("/connections", method="GET"), None)
    assert connection_response["statusCode"] == connection_case["response"]["statusCode"]
    assert json.loads(connection_response["body"]) == connection_case["response"]["body"]

    history_case = cases["history_list"]
    table("ACTION_HISTORY_TABLE").put_item(Item={"user_id": "alice", **history_case["response"]["body"]["items"][0]})
    history_response = history.lambda_handler(api_event("/history", method="GET", query=history_case["request"]["query"]), None)
    assert history_response["statusCode"] == history_case["response"]["statusCode"]
    assert json.loads(history_response["body"]) == history_case["response"]["body"]


def test_appsync_contract_example_matches_publisher_schema():
    data, _ = examples()
    event = data["appsync"]["publisher_event"]
    schema = (ROOT / "backend/graphql/schema.graphql").read_text(encoding="utf-8")
    assert set(event) == set(PUBLIC)
    assert data["appsync"]["mutation"] in appsync.MUTATION
    assert data["appsync"]["subscription"] in schema
    assert not ({"external_id", "credentials", "SecretAccessKey"} & set(event))
