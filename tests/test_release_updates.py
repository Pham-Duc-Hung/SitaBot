from unittest.mock import Mock
import pytest
from botocore.exceptions import ClientError
from scripts.deploy_release import existing_backend, validate_integration_outputs
from scripts.write_deploy_config import RELEASE_REQUIRED, build_configuration


def test_existing_release_preserves_aliases_and_activation_state():
    stack = {"StackStatus": "UPDATE_COMPLETE", "Outputs": [{"OutputKey": "ChatbotLiveAliasArn", "OutputValue": "alias"}],
             "Parameters": [{"ParameterKey": "GrantIntegrationPermissions", "ParameterValue": "true"}]}
    client = Mock()
    client.describe_stacks.return_value = {"Stacks": [stack]}
    assert existing_backend(client) == stack


@pytest.mark.parametrize("status", ["UPDATE_IN_PROGRESS", "ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED"])
def test_unhealthy_stack_blocks_release(status):
    client = Mock()
    client.describe_stacks.return_value = {"Stacks": [{"StackStatus": status}]}
    with pytest.raises(RuntimeError, match=status):
        existing_backend(client)


def test_only_missing_stack_can_bootstrap():
    client = Mock()
    client.describe_stacks.side_effect = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks")
    assert existing_backend(client) is None
    client.describe_stacks.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeStacks")
    with pytest.raises(ClientError):
        existing_backend(client)


def test_legacy_stack_requires_migration_instead_of_duplicate_backend():
    client = Mock()
    client.describe_stacks.side_effect = [
        ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}}, "DescribeStacks"),
        {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]},
    ]
    with pytest.raises(RuntimeError, match="migration"):
        existing_backend(client)
    assert client.describe_stacks.call_args.kwargs["StackName"] == "SatiBotBackendSprint2"


@pytest.mark.parametrize("already_active", [False, True])
def test_release_bootstraps_only_new_stack(monkeypatch, tmp_path, already_active):
    import json
    import sys
    from scripts import deploy_release as release
    root = tmp_path / "dist/cdk.out"
    root.mkdir(parents=True)
    (root / "SatiBotBackend.template.json").write_text(json.dumps({"Parameters": {
        "CanaryDeploymentConfig": {"AllowedValues": ["CodeDeployDefault.LambdaCanary10Percent5Minutes"]}}}))
    aliases = {f"{n.title()}LiveAliasArn": f"arn:{n}:live" for n in release.FUNCTIONS}
    previous = {"StackStatus": "UPDATE_COMPLETE", "Outputs": [
        {"OutputKey": k, "OutputValue": v} for k, v in aliases.items()],
        "Parameters": [{"ParameterKey": "GrantIntegrationPermissions", "ParameterValue": "true"}]}
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "existing_backend", lambda client: previous if already_active else None)
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(release.boto3, "Session", Mock(return_value=session))
    monkeypatch.setattr(release, "run", Mock())
    foundation = {
        **{k: k for k in ["AwsConnectionsTableName", "ActionHistoryTableName", "SessionStateTableName", "PendingActionTableName"]},
        **{f"{n.title()}RoleArn": "role" for n in release.FUNCTIONS},
    }
    integration = {
        "AppSyncGraphqlUrl": "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql",
        "AppSyncApiArn": "arn:aws:appsync:ap-southeast-1:123456789012:apis/abc123",
        "ApiGatewaySourceArn": "arn:aws:execute-api:ap-southeast-1:123456789012:api123/*/*",
    }
    monkeypatch.setattr(release, "outputs", lambda path, stack: integration if stack == "SatiBotIntegration" else foundation)
    monkeypatch.setattr(release, "build_configuration", lambda template, values, **kw: {
        "Parameters": {k.removeprefix("PARAM_"): v for k, v in values.items()}})
    deploy = Mock(return_value=aliases)
    monkeypatch.setattr(release, "deploy", deploy)
    integration_file = tmp_path / "integration.json"
    integration_file.write_text(json.dumps({"SatiBotIntegration": integration}))
    phase = ["--integration-outputs", str(integration_file)] if already_active else ["--bootstrap-only"]
    monkeypatch.setattr(sys, "argv", ["deploy_release", "--expected-account-id", "123456789012", "--gemini-secret-arn", "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:gemini", "--codedeploy-role-arn", "arn:aws:iam::123456789012:role/CodeDeploy", *phase, "--execute"])
    release.main()
    calls = deploy.call_args_list
    assert [c.args[1] for c in calls] == ["SatiBotBackend"]
    assert calls[-1].args[2]["GrantIntegrationPermissions"] == ("true" if already_active else "false")
    if already_active:
        assert calls[-1].args[2]["CanaryDeploymentConfig"] == "CodeDeployDefault.LambdaCanary10Percent5Minutes"


def test_release_rejects_missing_integration_parameters():
    template = {
        "Parameters": {
            name: {"Default": ""}
            for name in (*RELEASE_REQUIRED, "GrantIntegrationPermissions", "ConfigurationVersion")
        }
    }
    with pytest.raises(ValueError, match="Missing release parameters"):
        build_configuration(template, {}, require_integrations=True)
    env = {f"PARAM_{name}": "configured" for name in RELEASE_REQUIRED}
    with pytest.raises(ValueError, match="GrantIntegrationPermissions"):
        build_configuration(template, env, require_integrations=True)
    bootstrap = build_configuration(
        template,
        {**env, "PARAM_GrantIntegrationPermissions": "false"},
        require_integrations=False,
    )
    env["PARAM_GrantIntegrationPermissions"] = "true"
    result = build_configuration(template, env, require_integrations=True)
    assert result["Parameters"]["AppSyncApiArn"] == "configured"
    assert (
        bootstrap["Parameters"]["ConfigurationVersion"]
        != result["Parameters"]["ConfigurationVersion"]
    )


def test_release_checks_external_outputs_before_any_aws_change(monkeypatch, tmp_path):
    import json
    import sys
    from scripts import deploy_release as release

    integration_file = tmp_path / "integration.json"
    integration_file.write_text(json.dumps({"SatiBotIntegration": {"AppSyncGraphqlUrl": "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql"}}))
    run = Mock()
    session = Mock()
    session.return_value.client.return_value.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release.boto3, "Session", session)
    monkeypatch.setattr(sys, "argv", [
        "deploy_release", "--expected-account-id", "123456789012",
        "--gemini-secret-arn", "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:gemini",
        "--codedeploy-role-arn", "arn:aws:iam::123456789012:role/CodeDeploy",
        "--integration-outputs", str(integration_file), "--execute",
    ])
    with pytest.raises(RuntimeError, match="AppSyncApiArn"):
        release.main()
    run.assert_not_called()
    session.assert_called_once()


def test_release_rejects_integration_outputs_from_another_account_or_region():
    valid = {
        "AppSyncGraphqlUrl": "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql",
        "AppSyncApiArn": "arn:aws:appsync:ap-southeast-1:123456789012:apis/abc123",
        "ApiGatewaySourceArn": "arn:aws:execute-api:ap-southeast-1:123456789012:api123/*/*",
    }
    validate_integration_outputs(valid, "123456789012", "ap-southeast-1")
    for key in valid:
        wrong = dict(valid)
        wrong[key] = wrong[key].replace("123456789012", "999999999999").replace("ap-southeast-1", "us-east-1")
        with pytest.raises(ValueError, match=key):
            validate_integration_outputs(wrong, "123456789012", "ap-southeast-1")
