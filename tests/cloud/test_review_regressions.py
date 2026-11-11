"""
Author: Trịnh Thiên Quang
Kiểm thử các lỗi cấu hình, metric, verifier và benchmark đã phát hiện khi review.
"""

import json
from unittest.mock import Mock

import pytest

from backend.shared.runtime import emit_platform_fault
from scripts.deployment_checks import validate_live_evidence, verify_callers
from scripts.benchmark_live import _derive_history_url, benchmark_live_endpoint
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
    explicit_role = build_configuration(
        template, {**env, "PARAM_ChatbotRoleName": "explicit-chatbot-role"}
    )
    assert (
        first["Parameters"]["ConfigurationVersion"]
        != second["Parameters"]["ConfigurationVersion"]
    )
    assert second["Parameters"]["GeminiSecretArn"] == "new-secret"
    assert second["Parameters"]["ChatbotRoleName"] == "chatbot"
    assert explicit_role["Parameters"]["ChatbotRoleName"] == "explicit-chatbot-role"
    assert first == build_configuration(template, env)
    with pytest.raises(ValueError, match="GeminiSecretArn"):
        build_configuration(template, {})


def test_platform_metrics_distinguish_physical_functions_and_ignore_latest(
    monkeypatch, capsys
):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "sandbox-chatbot")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_VERSION", "2")
    from types import SimpleNamespace

    first = emit_platform_fault(
        "chatbot",
        "AccessDenied",
        context=SimpleNamespace(
            invoked_function_arn="arn:aws:lambda:region:account:function:sandbox-chatbot:live"
        ),
    )
    assert capsys.readouterr().out.strip() != ""
    for qualifier in ("2", "live-test", "$LATEST"):
        emit_platform_fault(
            "chatbot",
            "AccessDenied",
            context=SimpleNamespace(
                invoked_function_arn=f"arn:aws:lambda:region:account:function:sandbox-chatbot:{qualifier}"
            ),
        )
        assert capsys.readouterr().out == ""
    emit_platform_fault("chatbot", "AccessDenied")
    assert capsys.readouterr().out == ""

    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "production-chatbot")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST")
    second = emit_platform_fault("chatbot", "AccessDenied")
    assert capsys.readouterr().out == ""
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
    managed_params = {key: value for key, value in params.items() if key != "ActionStateMachineArn"}
    verify_callers(session, managed_params, aliases, workflow_arn="managed-workflow")
    workflow.describe_state_machine.assert_called_with(stateMachineArn="managed-workflow")
    # Chặn cả trường hợp trỏ hàm gốc lẫn trỏ nhầm sang alias khác có cùng tiền tố ':live-canary'
    for bad_uri in (
        aliases["chatbot"].removesuffix(":live"),
        aliases["chatbot"] + "-canary",
    ):
        api.get_integrations.return_value["Items"][0]["IntegrationUri"] = bad_uri
        with pytest.raises(ValueError, match="Gateway"):
            verify_callers(session, params, aliases)
    # Chấp nhận định dạng URI đầy đủ của API Gateway (/functions/<alias_arn>/invocations)
    api.get_integrations.return_value["Items"][0]["IntegrationUri"] = (
        f"arn:aws:apigateway:ap-southeast-1:lambda:path/2015-03-31/functions/{aliases['chatbot']}/invocations"
    )
    assert len(verify_callers(session, params, aliases)["gateway_routes"]) == 6
    # Chặn ApiGatewaySourceArn dùng wildcard '*' làm mất định danh API
    with pytest.raises(ValueError, match="concrete API ID"):
        verify_callers(
            session,
            {
                **params,
                "ApiGatewaySourceArn": "arn:aws:execute-api:ap-southeast-1:123456789012:*/*/*",
            },
            aliases,
        )
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
        "scripts.benchmark_live.urllib.request.urlopen",
        Mock(return_value=reply({"action_id": "a"}, 202)),
    )
    monkeypatch.setattr("scripts.benchmark_live.time.sleep", Mock())
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
    monkeypatch.setattr("scripts.benchmark_live.urllib.request.urlopen", network)
    monkeypatch.setattr("scripts.benchmark_live.time.sleep", Mock())
    result = benchmark_live_endpoint(
        "https://example/chat", jwt_token="test", iterations=1
    )
    assert result["completed"] == result["failed"] == 1
    assert result["succeeded"] == result["timed_out"] == 0
    assert "cursor=next" in network.call_args.args[0].full_url
    assert (
        _derive_history_url("https://example/api/chat/")
        == "https://example/api/history"
    )


def test_benchmark_excludes_unaccepted_responses_and_tracks_warm_by_iteration(
    monkeypatch,
):
    now = [0.0]
    responses = iter(
        [
            # Iteration 1: HTTP 200 chat_reply sau 500 ms -> bị từ chối, không được tính vào accept_ms
            (0.500, reply({"type": "chat_reply", "message": "Xin chào"}, 200)),
            # Iteration 2: HTTP 202 sau 20 ms, hoàn tất sau thêm 30 ms
            (0.020, reply({"action_id": "act_2"}, 202)),
            (0.030, reply({"items": [{"action_id": "act_2", "status": "SUCCESS"}]})),
            # Iteration 3: HTTP 202 sau 10 ms, hoàn tất sau thêm 25 ms
            (0.010, reply({"action_id": "act_3"}, 202)),
            (0.025, reply({"items": [{"action_id": "act_3", "status": "SUCCESS"}]})),
        ]
    )

    def _fake_urlopen(_req, timeout=20):
        delta, resp = next(responses)
        now[0] += delta
        return resp

    monkeypatch.setattr("scripts.benchmark_live.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("scripts.benchmark_live.time.perf_counter", lambda: now[0])
    monkeypatch.setattr("scripts.benchmark_live.time.sleep", Mock())
    result = benchmark_live_endpoint(
        "https://example/chat", jwt_token="test", iterations=3, poll_timeout_s=5
    )
    assert result["accepted"] == 2
    assert result["failed"] == 1
    assert "accept_ms" not in result["samples"][0]
    assert result["accept_initial_ms"] is None
    # Cả iteration 2 (20 ms) và iteration 3 (10 ms) đều có iteration > 1 -> trung bình 15.0 ms
    assert result["accept_warm_avg_ms"] == 15.0
