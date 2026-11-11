"""
Author: Trịnh Thiên Quang
Kiểm thử template và cơ chế canary/rollback.
"""

from copy import deepcopy
import json
import os
import subprocess
import sys
import pytest
from scripts.package_backend import FUNCTIONS, package
from scripts.verify_canary_rollback import (
    CanaryAliasSimulator,
    simulate_canary_and_rollback,
    verify_cdk_canary_template,
    verify_pipeline_template,
)
from .helpers import ROOT


@pytest.fixture(scope="module")
def synthed_backend_template(tmp_path_factory):
    dist_backend = ROOT / "dist" / "backend"
    if not all((dist_backend / f"{name}.zip").exists() for name in FUNCTIONS):
        package(dist_backend, with_dependencies=False)
    cdk_out = tmp_path_factory.mktemp("cdk_synth")
    subprocess.run(
        [sys.executable, str(ROOT / "infrastructure" / "backend_app.py")],
        env={
            **dict(os.environ),
            "CDK_OUTDIR": str(cdk_out),
            "PRIMARY_REGION": "ap-southeast-1",
        },
        check=True,
    )
    return cdk_out / "SatiBotBackend.template.json"


def test_cdk_canary_stack_pipeline_and_alarm_rollback_simulation(
    synthed_backend_template,
):
    cdk_summary = verify_cdk_canary_template(synthed_backend_template)
    assert cdk_summary["functions"] == 5
    assert cdk_summary["versions"] == 5
    assert cdk_summary["live_aliases"] == 5
    assert cdk_summary["deployment_groups"] == 5
    assert (
        cdk_summary["default_strategy"]
        == "CodeDeployDefault.LambdaCanary10Percent5Minutes"
    )

    pipeline_summary = verify_pipeline_template(ROOT / "ci" / "pipeline.template.json")
    assert pipeline_summary["stages"] == [
        "Source",
        "BuildTestSynth",
        "ManualApproval",
        "DeployBackend",
    ]
    assert pipeline_summary["deploy_actions"] == [
        "PublishCdkAssets",
        "CreateReplaceChangeSet",
        "ExecuteChangeSet",
    ]

    sim_summary = simulate_canary_and_rollback()
    assert sim_summary["canary_promotion_v1_to_v2"]["deployment_status"] == "SUCCEEDED"
    assert sim_summary["alarm_rollback_v2_to_v3"]["deployment_status"] == "ROLLED_BACK"
    assert (
        sim_summary["alarm_rollback_v2_to_v3"]["rollback_trigger"]
        == "DEPLOYMENT_STOP_ON_ALARM"
    )

    # Kiểm tra chi tiết trọng số lưu lượng 90%/10% trong giai đoạn Canary trước khi rollback
    sim = CanaryAliasSimulator(function_name="SatiBotBackend-dispatcherFunction")
    rollback_case = sim.deploy_canary(
        "2", simulated_error_rate=0.25, error_threshold=0.05
    )
    assert rollback_case["timeline"][0]["weights"] == {"1": 0.9, "2": 0.1}
    assert rollback_case["final_weights"] == {"1": 1.0}


def test_verifier_rejects_missing_alarms_missing_config_version_or_oversized_overrides(
    synthed_backend_template, tmp_path
):
    base_tpl = json.loads(synthed_backend_template.read_text(encoding="utf-8"))

    # 1. Xóa AlarmConfiguration khỏi DeploymentGroup -> verifier bắt buộc báo lỗi
    no_alarm_tpl = deepcopy(base_tpl)
    for res in no_alarm_tpl["Resources"].values():
        if res.get("Type") == "AWS::CodeDeploy::DeploymentGroup":
            res["Properties"].pop("AlarmConfiguration", None)
    tampered_alarm_file = tmp_path / "no_alarm.template.json"
    tampered_alarm_file.write_text(json.dumps(no_alarm_tpl), encoding="utf-8")
    with pytest.raises(AssertionError, match="AlarmConfiguration"):
        verify_cdk_canary_template(tampered_alarm_file)

    # 2. Xóa ConfigurationVersion khỏi AWS::Lambda::Version Description -> verifier bắt buộc báo lỗi
    no_cfg_ver_tpl = deepcopy(base_tpl)
    for res in no_cfg_ver_tpl["Resources"].values():
        if res.get("Type") == "AWS::Lambda::Version":
            res["Properties"]["Description"] = "static-description"
    tampered_ver_file = tmp_path / "no_cfg_ver.template.json"
    tampered_ver_file.write_text(json.dumps(no_cfg_ver_tpl), encoding="utf-8")
    with pytest.raises(AssertionError, match="ConfigurationVersion"):
        verify_cdk_canary_template(tampered_ver_file)

    # 3. ParameterOverrides vượt 1000 byte (cả dạng Fn::Sub lẫn chuỗi thuần) hoặc lộ NoEcho qua PLAINTEXT -> verifier bắt buộc báo lỗi
    base_pipeline = json.loads(
        (ROOT / "ci" / "pipeline.template.json").read_text(encoding="utf-8")
    )
    for idx, overrides in enumerate(
        (
            {"Fn::Join": ["", ['{"Padding":"', "x" * 1050, '"}']]},
            {"Fn::Sub": ['{"Padding":"${P}${P}"}', {"P": "x" * 600}]},
            {"Fn::Sub": "{" + '"Padding":"' + ("x" * 1050) + '"}'},
            "{" + '"Padding":"' + ("x" * 1050) + '"}',
        )
    ):
        bad_pipeline = deepcopy(base_pipeline)
        bad_pipeline["Resources"]["Pipeline"]["Properties"]["Stages"][3]["Actions"][1][
            "Configuration"
        ]["ParameterOverrides"] = overrides
        bad_pipeline_file = tmp_path / f"bad_pipeline_{idx}.json"
        bad_pipeline_file.write_text(json.dumps(bad_pipeline), encoding="utf-8")
        with pytest.raises(AssertionError, match="1000 byte"):
            verify_pipeline_template(bad_pipeline_file)

    leaked_noecho = deepcopy(base_pipeline)
    leaked_noecho["Parameters"]["GeminiSecretArn"]["NoEcho"] = True
    leaked_file = tmp_path / "leaked_noecho.json"
    leaked_file.write_text(json.dumps(leaked_noecho), encoding="utf-8")
    with pytest.raises(AssertionError, match="NoEcho"):
        verify_pipeline_template(leaked_file)


@pytest.mark.parametrize(
    "overrides",
    [
        {"Ref": "DynamicValue"},
        {"Fn::Sub": "${DynamicValue}"},
        {"Fn::Join": ["", [{"Ref": "DynamicValue"}]]},
        {"Fn::Join": []},
        {"Fn::Sub": []},
        "not-json",
        "[]",
        '{"X":',
        "",
        '{"X": NaN}',
    ],
)
def test_pipeline_rejects_unverifiable_or_invalid_json_overrides(tmp_path, overrides):
    pipeline = json.loads((ROOT / "ci/pipeline.template.json").read_text())
    pipeline["Resources"]["Pipeline"]["Properties"]["Stages"][3]["Actions"][1][
        "Configuration"
    ]["ParameterOverrides"] = overrides
    path = tmp_path / "pipeline.json"
    path.write_text(json.dumps(pipeline))
    with pytest.raises(AssertionError, match="ParameterOverrides"):
        verify_pipeline_template(path)


def test_canary_alarm_allows_one_delayed_breaching_minute(synthed_backend_template):
    template = json.loads(synthed_backend_template.read_text())
    alarms = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    ]
    assert len(alarms) == len(FUNCTIONS)
    assert all(
        alarm["EvaluationPeriods"] == 3 and alarm["DatapointsToAlarm"] == 1
        for alarm in alarms
    )
    assert not template["Parameters"]["GeminiSecretArn"].get("NoEcho", False)

    lambdas = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    ]
    assert all(
        fn["Environment"]["Variables"].get("SATIBOT_CONFIG_VERSION")
        == {"Ref": "ConfigurationVersion"}
        for fn in lambdas
    )
    assert "GeminiSecretReadSupplementaryPolicy" in template["Resources"]


def test_chatbot_can_read_connections_in_deployed_environment(synthed_backend_template):
    template = json.loads(synthed_backend_template.read_text())
    chatbot = next(r["Properties"] for r in template["Resources"].values()
                   if r["Type"] == "AWS::Lambda::Function"
                   and r["Properties"]["Handler"] == "backend.functions.chatbot.index.lambda_handler")
    assert chatbot["Environment"]["Variables"]["AWS_CONNECTIONS_TABLE"] == {"Ref": "ConnectionsTableName"}

def test_managed_workflow_wiring_and_error_routes(synthed_backend_template):
    from scripts.deployment_template_checks import verify_action_workflow
    template = json.loads(synthed_backend_template.read_text())
    verify_action_workflow(template)
    machine_id, machine = next((key, value) for key, value in template["Resources"].items()
                              if value["Type"] == "AWS::StepFunctions::StateMachine")
    states = machine["Properties"]["Definition"]["States"]
    assert "ActionStateMachineArn" not in template["Parameters"]
    chatbot = next(value for key, value in template["Resources"].items()
                   if value["Type"] == "AWS::Lambda::Function" and key.startswith("chatbot"))
    assert chatbot["Properties"]["Environment"]["Variables"]["ACTION_STATE_MACHINE_ARN"] == {"Fn::GetAtt": [machine_id, "Arn"]}
    assert states["Dispatcher"]["Parameters"]["Payload"] == {"user_id.$": "$.user_id", "action_id.$": "$.action_id"}
    assert states["HasResultEnvelope"]["Default"] == "DispatcherRejected"
    assert states["DispatcherRejected"]["Type"] == "Fail"
    assert states["HasResultEnvelope"]["Choices"][1]["Next"] == "DispatcherError"
    assert states["DispatcherError"]["ErrorPath"] == "$.error_code"
    assert states["Formatter"]["ResultPath"] == "$.formatted"
    assert states["RepublishFormatter"]["Parameters"] == states["Formatter"]["Parameters"]
    assert states["RepublishFormatter"]["Next"] == "CheckResult"
    assert states["CheckResult"]["Default"] == "ActionFailed"
    assert states["Completed"]["OutputPath"] == "$.formatted.Payload"
    assert {"Variable": "$.formatted.Payload.publication_status", "StringEquals": "PUBLISHED"} in states["CheckResult"]["Choices"][0]["And"]
    assert {"Not": {"Variable": "$.result.status", "StringEquals": "ERROR"}} in states["CheckResult"]["Choices"][0]["And"]
    pipeline = json.loads((ROOT / "ci/pipeline.template.json").read_text())
    assert "ActionStateMachineArn" not in pipeline["Parameters"]


@pytest.mark.parametrize("fault", ["dispatcher_retry", "wrong_alias", "formatter_retries", "publish_loop", "timeout", "wildcard_permission"])
def test_workflow_verifier_rejects_unsafe_definitions(synthed_backend_template, fault):
    from scripts.deployment_template_checks import verify_action_workflow
    template = json.loads(synthed_backend_template.read_text())
    machine = next(value for value in template["Resources"].values() if value["Type"] == "AWS::StepFunctions::StateMachine")
    states = machine["Properties"]["Definition"]["States"]
    if fault == "dispatcher_retry":
        states["Dispatcher"]["Retry"] = [{"ErrorEquals": ["States.ALL"]}]
    elif fault == "wrong_alias":
        states["Formatter"]["Parameters"]["FunctionName"] = "formatter:$LATEST"
    elif fault == "formatter_retries":
        states["Formatter"]["Retry"][0]["MaxAttempts"] = 3
    elif fault == "publish_loop":
        states["RepublishFormatter"]["Next"] = "CheckPublication"
    elif fault == "timeout":
        machine["Properties"]["Definition"]["TimeoutSeconds"] = 600
    else:
        role_id = machine["Properties"]["RoleArn"]["Fn::GetAtt"][0]
        for resource in template["Resources"].values():
            if resource["Type"] == "AWS::IAM::Policy" and {"Ref": role_id} in resource["Properties"]["Roles"]:
                resource["Properties"]["PolicyDocument"]["Statement"][0]["Resource"] = "*"
    with pytest.raises(AssertionError):
        verify_action_workflow(template)
