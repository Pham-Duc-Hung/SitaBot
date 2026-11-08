"""
Author: Trịnh Thiên Quang
Kiểm thử các lỗi cấu hình, metric, verifier và benchmark đã phát hiện khi review.
"""

import json
from unittest.mock import Mock

import pytest

from backend.shared.runtime import emit_platform_fault
from scripts.deployment_checks import validate_live_evidence, verify_callers
from scripts.live_benchmark import benchmark_live_endpoint
from scripts.write_deploy_config import build_configuration


def test_config_fingerprint_tracks_secret_and_derives_role_name():
    template = {
        "Parameters": {
            "GeminiSecretArn": {"Type": "String"},
            "ChatbotRoleName": {"Default": ""},
            "ChatbotRoleArn": {"Type": "String"},
            "ConfigurationVersion": {"Default": "v1"},
        }
    }
    env = {
        "PARAM_GeminiSecretArn": "old-secret",
        "PARAM_ChatbotRoleArn": "arn:aws:iam::123456789012:role/service-role/chatbot",
    }
    first = build_configuration(template, env)
    second = build_configuration(
        template, {**env, "PARAM_GeminiSecretArn": "new-secret"}
    )
    assert (
        first["Parameters"]["ConfigurationVersion"]
        != second["Parameters"]["ConfigurationVersion"]
    )
    assert second["Parameters"]["GeminiSecretArn"] == "new-secret"
    assert second["Parameters"]["ChatbotRoleName"] == "chatbot"
    assert first == build_configuration(template, env)
    with pytest.raises(ValueError, match="GeminiSecretArn"):
        build_configuration(template, {})


def test_platform_metrics_distinguish_physical_functions(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "sandbox-chatbot")
    first = emit_platform_fault("chatbot", "AccessDenied")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "production-chatbot")
    second = emit_platform_fault("chatbot", "AccessDenied")
    assert first["FunctionName"] != second["FunctionName"]
    assert first["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["FunctionName"]]


def healthy_evidence():
    return {
        "functions": {
            "chatbot": {
                "alarm_state": "OK",
                "invoke_function_error": None,
                "live_alias_smoke_response": {"statusCode": 401},
                "latest_deployment": {"status": "Succeeded"},
                "active_version": "2",
                "routing_config": {},
                "live_alias_arn": "arn:aws:lambda:ap-southeast-1:123456789012:function:chatbot:live",
                "deployment_target": {
                    "functionName": "chatbot",
                    "functionAlias": "live",
                    "currentVersion": "1",
                    "targetVersion": "2",
                },
            }
        }
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"alarm_state": "MISSING"},
        {"invoke_function_error": "Unhandled"},
        {"latest_deployment": None},
        {"latest_deployment": {"status": "Failed"}},
        {"active_version": "1"},
        {"live_alias_smoke_response": {"statusCode": 500}},
        {"routing_config": {"AdditionalVersionWeights": {"3": 0.1}}},
    ],
)
def test_live_validation_rejects_unhealthy_or_incomplete_evidence(changes):
    evidence = healthy_evidence()
    evidence["functions"]["chatbot"].update(changes)
    with pytest.raises(ValueError):
        validate_live_evidence(evidence)


def test_live_validation_checks_successful_rollback_version():
    evidence = healthy_evidence()
    validate_live_evidence(evidence)
    item = evidence["functions"]["chatbot"]
    item.update(
        latest_deployment={"status": "Failed"},
        rollback_status="Succeeded",
        active_version="1",
    )
    validate_live_evidence(evidence)
    item["active_version"] = "2"
    with pytest.raises(ValueError, match="version"):
        validate_live_evidence(evidence)


def test_verifier_checks_gateway_and_workflow_aliases():
    aliases = {
        n: f"arn:aws:lambda:ap-southeast-1:123456789012:function:{n}:live"
        for n in ("chatbot", "dispatcher", "formatter", "connector", "history")
    }
    api = Mock()
    functions = {
        "POST /chat": "chatbot",
        "POST /confirm": "chatbot",
        "GET /history": "history",
        "GET /connections": "connector",
        "POST /connections/external-id": "connector",
        "POST /connections/verify": "connector",
    }
    api.get_integrations.return_value = {
        "Items": [
            {"IntegrationId": n, "IntegrationUri": arn} for n, arn in aliases.items()
        ]
    }
    api.get_routes.return_value = {
        "Items": [
            {"RouteKey": r, "Target": "integrations/" + n} for r, n in functions.items()
        ]
    }
    workflow = Mock()
    workflow.describe_state_machine.return_value = {
        "definition": json.dumps(
            {
                "States": {
                    n: {
                        "Type": "Task",
                        "Resource": "arn:aws:states:::lambda:invoke",
                        "Parameters": {"FunctionName": aliases[n]},
                    }
                    for n in ("dispatcher", "formatter")
                }
            }
        )
    }
    session = Mock()
    session.client.side_effect = lambda n: {
        "apigatewayv2": api,
        "stepfunctions": workflow,
    }[n]
    params = {
        "ApiGatewaySourceArn": "arn:aws:execute-api:ap-southeast-1:123456789012:api/*",
        "ActionStateMachineArn": "workflow",
    }
    assert len(verify_callers(session, params, aliases)["gateway_routes"]) == 6
    api.get_integrations.return_value["Items"][0]["IntegrationUri"] = aliases[
        "chatbot"
    ].removesuffix(":live")
    with pytest.raises(ValueError, match="Gateway"):
        verify_callers(session, params, aliases)
    api.get_integrations.return_value["Items"][0]["IntegrationUri"] = aliases["chatbot"]
    definition = json.loads(workflow.describe_state_machine.return_value["definition"])
    definition["States"]["formatter"]["Parameters"]["FunctionName"] = (
        "formatter:$LATEST"
    )
    workflow.describe_state_machine.return_value["definition"] = json.dumps(definition)
    with pytest.raises(ValueError, match="Workflow"):
        verify_callers(session, params, aliases)


def reply(data, status=200):
    result = Mock()
    result.status = status
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = json.dumps(data).encode()
    return result


def test_benchmark_records_timeouts_instead_of_silently_dropping_samples(monkeypatch):
    monkeypatch.setattr(
        "scripts.live_benchmark.urllib.request.urlopen",
        Mock(return_value=reply({"action_id": "a"}, 202)),
    )
    monkeypatch.setattr("scripts.live_benchmark.time.sleep", Mock())
    result = benchmark_live_endpoint(
        "https://example/chat", jwt_token="test", iterations=2, poll_timeout_s=0
    )
    assert result["submitted"] == result["timed_out"] == 2
    assert result["completed"] == 0
    assert "workflow_e2e_avg_ms" not in result


def test_benchmark_finds_action_after_first_page_and_reports_failed_status(monkeypatch):
    network = Mock(
        side_effect=[
            reply({"action_id": "target"}, 202),
            reply({"items": [], "cursor": "next"}),
            reply({"items": [{"action_id": "target", "status": "ERROR"}]}),
        ]
    )
    monkeypatch.setattr("scripts.live_benchmark.urllib.request.urlopen", network)
    monkeypatch.setattr("scripts.live_benchmark.time.sleep", Mock())
    result = benchmark_live_endpoint(
        "https://example/chat", jwt_token="test", iterations=1
    )
    assert result["completed"] == result["failed"] == 1
    assert result["succeeded"] == result["timed_out"] == 0
    assert "cursor=next" in network.call_args.args[0].full_url
