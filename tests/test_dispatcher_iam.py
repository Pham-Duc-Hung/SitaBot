"""Offline PR tests: boto3 clients are replaced before any AWS request."""
import json
from copy import deepcopy
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from backend.functions.dispatcher.services import iam_service as iam
from backend.functions.dispatcher.index import lambda_handler

ARN = "arn:aws:iam::123456789012:policy/demo"
PROFILE_ARN = "arn:aws:iam::123456789012:instance-profile/demo"
PERMISSION = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::demo/docs/file"}]}
TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


@pytest.fixture
def clients(monkeypatch):
    client, ec2 = MagicMock(), MagicMock()
    client.get_role.return_value = {"Role": {"RoleName": "demo", "Path": "/"}}
    client.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": ARN}]}
    client.list_role_policies.return_value = {"PolicyNames": ["inline"]}
    client.list_instance_profiles_for_role.return_value = {"InstanceProfiles": [
        {"InstanceProfileName": "demo", "Arn": PROFILE_ARN}]}
    client.get_instance_profile.return_value = {"InstanceProfile": {"Roles": []}}
    client.get_policy.return_value = {"Policy": {"Arn": ARN, "DefaultVersionId": "v2"}}
    client.get_policy_version.return_value = {"PolicyVersion": {"Document": PERMISSION}}
    client.list_entities_for_policy.return_value = {"PolicyRoles": [{"RoleName": "demo"}], "PolicyUsers": [], "PolicyGroups": []}
    def entities(**kwargs):
        if kwargs["PolicyUsageFilter"] == "PermissionsBoundary":
            return {"PolicyRoles": [], "PolicyUsers": [], "PolicyGroups": []}
        return client.list_entities_for_policy.return_value
    client.list_entities_for_policy.side_effect = entities
    client.list_policy_versions.return_value = {"Versions": [
        {"VersionId": "v1", "IsDefaultVersion": False}, {"VersionId": "v2", "IsDefaultVersion": True}]}
    ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}, {"RegionName": "ap-southeast-1"}]}
    ec2.describe_iam_instance_profile_associations.return_value = {"IamInstanceProfileAssociations": []}
    factory = MagicMock(side_effect=lambda service, **kw: client if service == "iam" else ec2)
    monkeypatch.setattr(iam.boto3, "client", factory)
    return client, ec2, factory


def run(action, confirmed=False, **p):
    return iam.execute(None, {"action": action, "parameters": p, "confirmed": confirmed})


def approved_delete(action, **parameters):
    preview = run("preview_" + action, **parameters)
    assert preview["status"] == "SUCCESS"
    return run(action, True, **parameters,
               preview_fingerprint=preview["data"]["preview_fingerprint"])


def writes(client):
    prefixes = ("create_", "update_", "put_", "delete_", "attach_", "detach_", "remove_")
    return [c for c in client.mock_calls if c[0].startswith(prefixes)]


@pytest.mark.parametrize("params", [None, [], {"parameters": []}, {"action": "list_roles", "confirmed": "true"}, {"action": "list_roles", "confirmed": 1}])
def test_invalid_contract_never_constructs_client(clients, params):
    assert iam.execute(None, params)["status"] == "ERROR"
    clients[2].assert_not_called()


@pytest.mark.parametrize("action", ["create_user", "delete_user", "create_group", "add_user_to_group", "unknown"])
def test_outside_scope_no_client(clients, action):
    result = run(action, True)
    assert result["error_code"] == "UnsupportedTool"
    if "user" in action or "group" in action:
        assert "Console" in result["message"]
    clients[2].assert_not_called()


@pytest.mark.parametrize("action,p", [
    ("create_role", {"role_name": "demo", "trust_policy": TRUST}),
    ("update_assume_role_policy", {"role_name": "demo", "trust_policy": TRUST}),
    ("create_policy", {"policy_name": "demo", "policy_document": PERMISSION}),
    ("update_policy", {"policy_arn": ARN, "policy_document": PERMISSION}),
    ("attach_role_policy", {"role_name": "demo", "policy_arn": ARN}),
    ("detach_role_policy", {"role_name": "demo", "policy_arn": ARN}),
    ("put_role_policy", {"role_name": "demo", "policy_name": "inline", "policy_document": PERMISSION}),
    ("delete_role_policy", {"role_name": "demo", "policy_name": "inline"}),
])
def test_every_non_delete_write_requires_confirmation_and_dispatches(clients, action, p):
    assert run(action, **p)["status"] == "CONFIRMATION_REQUIRED"
    if action != "attach_role_policy":
        clients[2].assert_not_called()
    assert not writes(clients[0])
    method = "create_policy_version" if action == "update_policy" else action
    getattr(clients[0], method).return_value = {}
    assert run(action, True, **p)["status"] == "SUCCESS"
    assert len(writes(clients[0])) == 1
    if action == "update_policy":
        assert clients[0].create_policy_version.call_args.kwargs["SetAsDefault"] is True


@pytest.mark.parametrize("doc", ["{", {}, {"Version": "2012-10-17", "Statement": []},
    {**PERMISSION, "Statement": [{**PERMISSION["Statement"][0], "Principal": "*"}]},
    {**PERMISSION, "Statement": [{"Effect": "Allow", "Action": "*"}]},
    {**PERMISSION, "Statement": [{**PERMISSION["Statement"][0], "Resource": "${ARN}"}]}])
def test_bad_policy_rejected_before_client(clients, doc):
    assert run("create_policy", True, policy_name="demo", policy_document=doc)["error_code"] == "InvalidParameters"
    clients[2].assert_not_called()


def test_wildcard_warns_but_confirmed_write_permitted(clients):
    doc = deepcopy(PERMISSION)
    doc["Statement"][0].update(Action="*", Resource="*")
    preview = run("create_policy", policy_name="demo", policy_document=doc)
    assert preview["warnings"] and preview["status"] == "CONFIRMATION_REQUIRED"
    clients[0].create_policy.return_value = {}
    result = run("create_policy", True, policy_name="demo", policy_document=doc)
    assert result["warnings"] and result["status"] == "SUCCESS"


def test_role_trust_template_and_unsupported_principal(clients):
    doc = deepcopy(iam.POLICY_TEMPLATES["role_trust"])
    doc["Statement"][0]["Principal"]["AWS"] = "arn:aws:iam::123456789012:role/caller"
    doc["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"] = "example-external-id"
    assert run("validate_policy", policy_type="trust", policy_document=doc)["status"] == "SUCCESS"
    doc["Statement"][0]["Principal"] = {"Federated": "anything"}
    assert run("validate_policy", policy_type="trust", policy_document=doc)["status"] == "ERROR"
    clients[2].assert_not_called()


def test_role_preview_paginated_and_cancel_is_read_only(clients):
    c = clients[0]
    c.list_attached_role_policies.side_effect = [
        {"AttachedPolicies": [{"PolicyArn": ARN}], "IsTruncated": True, "Marker": "next"},
        {"AttachedPolicies": [{"PolicyArn": ARN + "2"}]}]
    result = run("delete_role", role_name="demo")
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert len(result["data"]["preview"]["managed_policies"]) == 2
    assert "ngoài IAM" in result["warnings"][0]
    assert not writes(c)
    assert c.list_attached_role_policies.call_args.kwargs["Marker"] == "next"


def test_missing_pagination_marker_does_not_write(clients):
    clients[0].list_role_policies.return_value = {"PolicyNames": [], "IsTruncated": True}
    assert run("delete_role", True, role_name="demo")["status"] == "ERROR"
    assert not writes(clients[0])


def test_changed_preview_requires_new_confirmation(clients):
    assert run("delete_role", True, role_name="demo", preview_fingerprint="old")["status"] == "CONFIRMATION_REQUIRED"
    assert not writes(clients[0])


def test_delete_role_removes_only_direct_links_in_order(clients):
    result = approved_delete("delete_role", role_name="demo")
    assert result["status"] == "SUCCESS"
    assert writes(clients[0]) == [
        call.detach_role_policy(RoleName="demo", PolicyArn=ARN),
        call.delete_role_policy(RoleName="demo", PolicyName="inline"),
        call.remove_role_from_instance_profile(RoleName="demo", InstanceProfileName="demo"),
        call.delete_role(RoleName="demo")]


def test_explicit_optional_profile_deletion_checks_all_enabled_regions(clients):
    assert approved_delete("delete_role", role_name="demo", delete_instance_profiles=["demo"])["status"] == "SUCCESS"
    clients[0].delete_instance_profile.assert_called_once_with(InstanceProfileName="demo")
    assert clients[1].describe_iam_instance_profile_associations.call_count == 4


def test_in_use_profile_stops_before_mutations(clients):
    clients[1].describe_iam_instance_profile_associations.return_value = {"IamInstanceProfileAssociations": [
        {"State": "associated", "IamInstanceProfile": {"Arn": PROFILE_ARN}}]}
    assert approved_delete("delete_role", role_name="demo", delete_instance_profiles=["demo"])["status"] == "ERROR"
    assert not writes(clients[0])


def test_profile_still_nonempty_does_not_delete_profile_or_role(clients):
    clients[0].get_instance_profile.return_value = {"InstanceProfile": {"Roles": [{"RoleName": "other"}]}}
    result = approved_delete("delete_role", role_name="demo", delete_instance_profiles=["demo"])
    assert result["status"] == "ERROR" and result["data"]["completed_steps"]
    clients[0].delete_instance_profile.assert_not_called()
    clients[0].delete_role.assert_not_called()


@pytest.mark.parametrize("code", ["AccessDenied", "DeleteConflict", "NoSuchEntity", "Throttling", "ServiceFailure"])
def test_failure_reports_partial_progress_without_claiming_rollback(clients, code):
    clients[0].delete_role_policy.side_effect = ClientError({"Error": {"Code": code, "Message": "AWS failure"}}, "DeleteRolePolicy")
    result = approved_delete("delete_role", role_name="demo")
    assert result["error_code"] == code
    assert len(result["data"]["completed_steps"]) == 1
    assert result["data"]["failed_step"]["operation"] == "delete_role_policy"
    assert result["data"]["recovery_suggestion"]
    clients[0].delete_role.assert_not_called()


def test_policy_delete_only_nondefault_versions_and_role_attachments(clients):
    assert approved_delete("delete_policy", policy_arn=ARN)["status"] == "SUCCESS"
    assert writes(clients[0]) == [call.detach_role_policy(RoleName="demo", PolicyArn=ARN),
        call.delete_policy_version(PolicyArn=ARN, VersionId="v1"), call.delete_policy(PolicyArn=ARN)]


@pytest.mark.parametrize("dependency", ["PolicyUsers", "PolicyGroups", "boundary"])
def test_out_of_scope_policy_dependencies_never_detached(clients, dependency):
    c = clients[0]
    if dependency == "boundary":
        c.list_entities_for_policy.side_effect = None
        c.list_entities_for_policy.return_value = {"PolicyRoles": [{"RoleName": "boundary-role"}]}
    else:
        c.list_entities_for_policy.return_value[dependency] = [{"Name": "outside"}]
    assert approved_delete("delete_policy", policy_arn=ARN)["error_code"] == "DependenciesOutsideScope"
    assert not writes(c)


def test_aws_managed_policy_cannot_be_deleted(clients):
    assert run("delete_policy", True, policy_arn="arn:aws:iam::aws:policy/ReadOnlyAccess")["status"] == "ERROR"
    clients[2].assert_not_called()


def test_list_read_policy_and_inline_document(clients):
    c = clients[0]
    c.list_roles.return_value = {"Roles": []}
    c.list_policies.return_value = {"Policies": []}
    c.get_policy_version.return_value = {"PolicyVersion": {"Document": PERMISSION}}
    c.get_role_policy.return_value = {"PolicyDocument": json.dumps(PERMISSION)}
    for action, p in [("list_roles", {}), ("list_policies", {}), ("get_role", {"role_name": "demo"}),
                      ("get_policy", {"policy_arn": ARN}), ("get_role_policy", {"role_name": "demo", "policy_name": "inline"}),
                      ("list_role_policies", {"role_name": "demo"}), ("list_attached_role_policies", {"role_name": "demo"})]:
        assert run(action, **p)["status"] == "SUCCESS"
    assert not writes(c)


def test_temporary_credentials_and_safe_aws_error(clients):
    creds = {"AccessKeyId": "example-key", "SecretAccessKey": "example-secret", "SessionToken": "example-token"}
    clients[0].list_roles.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "example-secret example-token"}}, "ListRoles")
    result = iam.execute(creds, {"action": "list_roles"})
    assert "example-secret" not in json.dumps(result) and "example-token" not in json.dumps(result)
    assert clients[2].call_args.kwargs["aws_session_token"] == "example-token"


def test_dispatcher_direct_and_http_contract(clients):
    clients[0].list_roles.return_value = {"Roles": []}
    event = {"service": "iam", "action": "list_roles", "parameters": {}}
    assert lambda_handler(event, None)["status"] == "SUCCESS"
    response = lambda_handler({"body": json.dumps(event)}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["service"] == "iam"


@pytest.mark.parametrize("action,p", [
    ("delete_role", {"role_name": "demo"}),
    ("delete_policy", {"policy_arn": ARN}),
])
def test_preview_read_access_denied_never_mutates(clients, action, p):
    method = clients[0].get_role if action == "delete_role" else clients[0].get_policy
    method.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "Cannot inspect"}}, "Get")
    result = run(action, True, **p)
    assert result["error_code"] == "AccessDenied"
    assert not writes(clients[0])


def test_policy_preview_is_read_only(clients):
    result = run("delete_policy", policy_arn=ARN)
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert result["data"]["preview"]["versions"]
    assert not writes(clients[0])


def test_profile_check_without_ec2_permissions_never_mutates(clients):
    clients[1].describe_regions.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "Missing EC2 permissions"}}, "DescribeRegions")
    assert approved_delete("delete_role", role_name="demo", delete_instance_profiles=["demo"])["error_code"] == "AccessDenied"
    assert not writes(clients[0])


def test_service_linked_role_uses_separate_workflow(clients):
    clients[0].get_role.return_value["Role"]["Path"] = "/aws-service-role/service/"
    assert run("delete_role", True, role_name="demo")["status"] == "ERROR"
    assert not writes(clients[0])


def test_version_limit_keeps_existing_versions(clients):
    clients[0].create_policy_version.side_effect = ClientError({"Error": {"Code": "LimitExceeded", "Message": "Version limit"}}, "CreatePolicyVersion")
    result = run("update_policy", True, policy_arn=ARN, policy_document=PERMISSION)
    assert result["error_code"] == "LimitExceeded"
    clients[0].delete_policy_version.assert_not_called()


def test_attach_custom_broad_policy_is_inspected_before_confirmation(clients):
    doc = deepcopy(PERMISSION)
    doc["Statement"][0].update(Action="*", Resource="*")
    clients[0].get_policy_version.return_value["PolicyVersion"]["Document"] = doc
    result = run("attach_role_policy", role_name="demo", policy_arn=ARN)
    assert result["status"] == "CONFIRMATION_REQUIRED" and result["warnings"]
    assert not writes(clients[0])


def test_write_parameters_match_real_botocore_api_model(monkeypatch):
    from botocore.session import get_session
    from botocore.stub import Stubber
    client = get_session().create_client("iam", region_name="us-east-1",
        aws_access_key_id="unit-test", aws_secret_access_key="unit-test")
    monkeypatch.setattr(iam.boto3, "client", lambda *args, **kwargs: client)
    with Stubber(client) as stub:
        stub.add_response("create_policy", {}, {"PolicyName": "demo", "PolicyDocument": json.dumps(PERMISSION)})
        stub.add_response("create_policy_version", {}, {"PolicyArn": ARN, "PolicyDocument": json.dumps(PERMISSION), "SetAsDefault": True})
        stub.add_response("update_assume_role_policy", {}, {"RoleName": "demo", "PolicyDocument": json.dumps(TRUST)})
        assert run("create_policy", True, policy_name="demo", policy_document=PERMISSION)["status"] == "SUCCESS"
        assert run("update_policy", True, policy_arn=ARN, policy_document=PERMISSION)["status"] == "SUCCESS"
        assert run("update_assume_role_policy", True, role_name="demo", trust_policy=TRUST)["status"] == "SUCCESS"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("action,parameters", [
    ("delete_role", {"role_name": "demo"}),
    ("delete_policy", {"policy_arn": ARN}),
])
@pytest.mark.parametrize("fingerprint", [None, "", "old", 123])
def test_confirmed_delete_requires_matching_server_preview(clients, action, parameters, fingerprint):
    result = run(action, True, **parameters, preview_fingerprint=fingerprint)
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert result["data"]["preview_fingerprint"]
    assert result["data"]["preview"]
    assert not writes(clients[0])


@pytest.mark.parametrize("action,parameters", [
    ("delete_role", {"role_name": "demo"}),
    ("delete_policy", {"policy_arn": ARN}),
])
def test_changed_dependencies_block_previously_approved_delete(clients, action, parameters):
    preview = run("preview_" + action, **parameters)
    if action == "delete_role":
        clients[0].list_role_policies.return_value = {"PolicyNames": ["new-inline"]}
    else:
        clients[0].list_policy_versions.return_value["Versions"].append(
            {"VersionId": "v3", "IsDefaultVersion": False})
    result = run(action, True, **parameters,
                 preview_fingerprint=preview["data"]["preview_fingerprint"])
    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert not writes(clients[0])


def test_cloud_registry_exposes_all_iam_actions_with_confirmation_metadata():
    from backend.shared.tool_registry import registry, llm_tools, validate_call
    from jsonschema import Draft7Validator

    registered = {tool["action"]: tool for tool in registry().values() if tool["service"] == "iam"}
    assert set(registered) == iam.READ_ACTIONS | iam.WRITE_ACTIONS
    for action, tool in registered.items():
        Draft7Validator.check_schema(tool["parameters"])
        assert tool["name"] == "iam_" + action
        assert tool["requires_confirmation"] == (action in iam.WRITE_ACTIONS)
        assert tool["parameters"]["additionalProperties"] is False
        assert not {"confirmed", "credentials", "preview_fingerprint"} & set(tool["parameters"]["properties"])
    assert any(tool["function"]["name"] == "iam_delete_role" for tool in llm_tools())
    assert validate_call("iam_create_role", {"role_name": "demo", "trust_policy": TRUST})["action"] == "create_role"


def test_unchanged_role_links_in_different_order_keep_approval_valid(clients):
    clients[0].list_role_policies.return_value = {"PolicyNames": ["a", "b"]}
    preview = run("preview_delete_role", role_name="demo")
    clients[0].list_role_policies.return_value = {"PolicyNames": ["b", "a"]}
    result = run("delete_role", True, role_name="demo",
                 preview_fingerprint=preview["data"]["preview_fingerprint"])
    assert result["status"] == "SUCCESS"
    clients[0].delete_role.assert_called_once()


@pytest.mark.parametrize("parameters", [
    {}, {"role_name": "demo", "confirmed": True},
    {"role_name": "demo", "preview_fingerprint": "forged"},
    {"role_name": "demo", "credentials": {}},
    {"role_name": 123}, {"role_name": "demo", "unexpected": "value"},
])
def test_cloud_schema_rejects_missing_arguments_and_client_approval_metadata(parameters):
    from backend.shared.cloud_errors import BackendError
    from backend.shared.tool_registry import validate_call
    with pytest.raises(BackendError) as error:
        validate_call("iam_delete_role", parameters)
    assert error.value.code == "InvalidParameters"


WORKFLOW_ARNS = {
    "state_machine_arn": "arn:aws:states:ap-southeast-1:123456789012:stateMachine:test-actions",
    "dispatcher_function_arn": "arn:aws:lambda:ap-southeast-1:123456789012:function:test-dispatcher",
    "formatter_function_arn": "arn:aws:lambda:ap-southeast-1:123456789012:function:test-formatter",
    "appsync_api_arn": "arn:aws:appsync:ap-southeast-1:123456789012:apis/testapi",
}


def iam_template(context=None, **kwargs):
    from aws_cdk import App
    from aws_cdk.assertions import Template
    from infrastructure.stacks.data_stack import DataStack
    from infrastructure.stacks.iam_stack import IamStack
    app = App(context=context)
    data = DataStack(app, "Data")
    stack = IamStack(app, "Iam", data_stack=data, **kwargs)
    return Template.from_stack(stack)


@pytest.mark.parametrize("use_context", [False, True])
def test_workflow_permissions_use_correct_roles_and_exact_resources(use_context):
    template = iam_template(context=WORKFLOW_ARNS) if use_context else iam_template(**WORKFLOW_ARNS)
    roles = template.find_resources("AWS::IAM::Role")
    policies = template.find_resources("AWS::IAM::Policy")
    expected = {
        "states:StartExecution": ("SatiBot-Chatbot-ExecutionRole", [WORKFLOW_ARNS["state_machine_arn"]]),
        "appsync:GraphQL": ("SatiBot-Formatter-ExecutionRole", [
            WORKFLOW_ARNS["appsync_api_arn"] + "/types/Mutation/fields/publishActionResult"]),
        "lambda:InvokeFunction": (None, [WORKFLOW_ARNS["dispatcher_function_arn"], WORKFLOW_ARNS["formatter_function_arn"]]),
    }
    seen = set()
    for policy in policies.values():
        properties = policy["Properties"]
        for statement in properties["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            for action in set(actions) & expected.keys():
                role_name, resources = expected[action]
                actual = statement["Resource"]
                assert sorted([actual] if isinstance(actual, str) else actual) == sorted(resources)
                assert statement["Effect"] == "Allow"
                assert len(properties["Roles"]) == 1
                role = roles[properties["Roles"][0]["Ref"]]["Properties"]
                if role_name:
                    assert role["RoleName"] == role_name
                else:
                    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "states.amazonaws.com"}
                assert action not in seen
                seen.add(action)
    assert seen == set(expected)


def test_unconfigured_workflow_does_not_grant_placeholder_or_wildcard_permissions():
    rendered = json.dumps(iam_template().to_json())
    assert "states:StartExecution" not in rendered
    assert "appsync:GraphQL" not in rendered
    assert "lambda:InvokeFunction" not in rendered


@pytest.mark.parametrize("config", [
    {"state_machine_arn": "*"},
    {"appsync_api_arn": WORKFLOW_ARNS["appsync_api_arn"] + "/*"},
    {"dispatcher_function_arn": WORKFLOW_ARNS["dispatcher_function_arn"]},
    {"dispatcher_function_arn": "*", "formatter_function_arn": WORKFLOW_ARNS["formatter_function_arn"]},
])
def test_workflow_configuration_rejects_overbroad_or_incomplete_resource_grants(config):
    with pytest.raises(ValueError):
        iam_template(**config)
