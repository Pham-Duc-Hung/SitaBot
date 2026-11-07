"""Template-level tests for the Sprint 2 infrastructure stacks."""

import json

from aws_cdk import App
from aws_cdk.assertions import Template

from infrastructure.stacks.data_stack import DataStack
from infrastructure.stacks.iam_stack import IamStack


TABLE_CONFIG = {
    "SatiBot-Users": ("user_id", None, False, True),
    "SatiBot-AwsConnections": ("user_id", None, False, True),
    "SatiBot-SessionState": ("user_id", "session_id", True, False),
    "SatiBot-PendingAction": ("action_id", None, True, False),
    "SatiBot-ActionHistory": ("user_id", "created_at", False, True),
}


def _synth_stacks(*, secret_arn=None):
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(
        app,
        "IamStack",
        data_stack=data_stack,
        llm_secret_arn=secret_arn,
    )
    return Template.from_stack(data_stack), Template.from_stack(iam_stack)


def test_data_stack_defines_five_retained_on_demand_tables_with_expected_keys_and_ttl():
    data_template, _ = _synth_stacks()
    resources = data_template.find_resources("AWS::DynamoDB::Table")

    assert len(resources) == 5
    by_name = {resource["Properties"]["TableName"]: resource for resource in resources.values()}

    assert set(by_name) == set(TABLE_CONFIG)
    for table_name, (partition_key, sort_key, has_ttl, _) in TABLE_CONFIG.items():
        resource = by_name[table_name]
        properties = resource["Properties"]
        assert properties["BillingMode"] == "PAY_PER_REQUEST"
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"

        keys = {key["KeyType"]: key["AttributeName"] for key in properties["KeySchema"]}
        assert keys["HASH"] == partition_key
        if sort_key:
            assert keys["RANGE"] == sort_key
        else:
            assert "RANGE" not in keys

        ttl = properties.get("TimeToLiveSpecification")
        if has_ttl:
            assert ttl == {"AttributeName": "expires_at", "Enabled": True}
        else:
            assert ttl is None


def test_only_long_lived_user_data_tables_enable_pitr_and_deletion_protection():
    data_template, _ = _synth_stacks()
    resources = data_template.find_resources("AWS::DynamoDB::Table")

    for resource in resources.values():
        properties = resource["Properties"]
        table_name = properties["TableName"]
        _, _, _, protected = TABLE_CONFIG[table_name]
        assert properties.get("DeletionProtectionEnabled", False) is protected
        recovery = properties.get("PointInTimeRecoverySpecification", {})
        assert recovery.get("PointInTimeRecoveryEnabled", False) is protected


def test_iam_stack_creates_six_lambda_roles_with_lambda_trust():
    _, iam_template = _synth_stacks()
    roles = iam_template.find_resources("AWS::IAM::Role")

    assert len(roles) == 6
    for role in roles.values():
        statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert any(
            statement.get("Principal", {}).get("Service") == "lambda.amazonaws.com"
            and "sts:AssumeRole" in statement.get("Action", [])
            for statement in statements
        )


def test_iam_data_permissions_are_scoped_to_table_arns():
    _, iam_template = _synth_stacks()
    policies = iam_template.find_resources("AWS::IAM::Policy")

    dynamodb_statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if any(action.startswith("dynamodb:") for action in statement.get("Action", []))
    ]
    assert dynamodb_statements
    assert all(statement.get("Resource") not in ("*", ["*"]) for statement in dynamodb_statements)


def test_dispatcher_and_connector_can_assume_customer_role_pattern():
    _, iam_template = _synth_stacks()
    policies = iam_template.find_resources("AWS::IAM::Policy")
    assume_role_policies = [
        policy
        for policy in policies.values()
        if any(
            "sts:AssumeRole" in statement.get("Action", [])
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        )
    ]

    assert len(assume_role_policies) == 2
    for policy in assume_role_policies:
        assert "satibot/SatiBotAccessRole" in json.dumps(
            policy["Properties"]["PolicyDocument"]
        )


def test_llm_secret_read_permission_is_added_only_when_secret_arn_is_configured():
    _, no_secret_template = _synth_stacks()
    _, secret_template = _synth_stacks(
        secret_arn="arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:satibot/llm-key-AbCdEf"
    )

    assert "secretsmanager:GetSecretValue" not in json.dumps(no_secret_template.to_json())
    configured = json.dumps(secret_template.to_json())
    assert "secretsmanager:GetSecretValue" in configured
    assert "satibot/llm-key-AbCdEf" in configured
