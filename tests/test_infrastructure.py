"""Template-level tests for the Sprint 2 infrastructure stacks."""

import json

from aws_cdk import App
from aws_cdk.assertions import Template

from infrastructure.stacks.data_stack import DataStack
from infrastructure.stacks.iam_stack import IamStack
from infrastructure.stacks.auth_stack import AuthStack
from infrastructure.stacks.core_api_stack import CoreApiStack


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
    # Trust policies also contain sts:AssumeRole; inspect permission policies only.
    roles = iam_template.find_resources("AWS::IAM::Role")
    policies = iam_template.find_resources("AWS::IAM::Policy")
    allowed_roles = set()
    assume_statements = []
    for policy in policies.values():
        properties = policy["Properties"]
        for statement in properties["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "sts:AssumeRole" in actions:
                assume_statements.append(statement)
                assert statement["Effect"] == "Allow"
                assert "satibot/SatiBotAccessRole" in json.dumps(statement["Resource"])
                allowed_roles.update(role["Ref"] for role in properties["Roles"])
    expected_roles = {
        logical_id for logical_id, role in roles.items()
        if role["Properties"]["RoleName"] in {
            "SatiBot-Dispatcher-ExecutionRole", "SatiBot-Connector-ExecutionRole"
        }
    }
    assert len(assume_statements) == 2
    assert len(expected_roles) == 2
    assert allowed_roles == expected_roles


def test_llm_secret_read_permission_is_added_only_when_secret_arn_is_configured():
    _, no_secret_template = _synth_stacks()
    _, secret_template = _synth_stacks(
        secret_arn="arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:satibot/llm-key-AbCdEf"
    )

    assert "secretsmanager:GetSecretValue" not in json.dumps(no_secret_template.to_json())
    configured = json.dumps(secret_template.to_json())
    assert "secretsmanager:GetSecretValue" in configured
    assert "satibot/llm-key-AbCdEf" in configured


def test_auth_stack_defines_cognito_user_pool_client_and_groups():
    """Verify AuthStack creates UserPool, SPA client without secret, and admin/user groups."""
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(app, "IamStack", data_stack=data_stack)
    auth_stack = AuthStack(app, "AuthStack", data_stack=data_stack, iam_stack=iam_stack)
    template = Template.from_stack(auth_stack)

    # 1. User Pool
    user_pools = template.find_resources("AWS::Cognito::UserPool")
    assert len(user_pools) == 1
    up_props = list(user_pools.values())[0]["Properties"]
    assert up_props["UserPoolName"] == "SatiBot-UserPool"
    assert "email" in up_props.get("AutoVerifiedAttributes", [])

    # 2. User Pool Client (SPA React)
    clients = template.find_resources("AWS::Cognito::UserPoolClient")
    assert len(clients) == 1
    client_props = list(clients.values())[0]["Properties"]
    assert client_props["ClientName"] == "SatiBot-WebClient"
    assert client_props.get("GenerateSecret", False) is False

    # 3. User Groups (admin & user)
    groups = template.find_resources("AWS::Cognito::UserPoolGroup")
    assert len(groups) == 2
    group_names = {g["Properties"]["GroupName"] for g in groups.values()}
    assert group_names == {"admin", "user"}

    # 4. Post-Confirmation Lambda Trigger
    lambdas = template.find_resources("AWS::Lambda::Function")
    assert len(lambdas) >= 1
    auth_lambda = [l for l in lambdas.values() if "AuthPostConfirmation" in l["Properties"].get("FunctionName", "")][0]
    assert auth_lambda["Properties"]["Runtime"] == "python3.12"


def test_core_api_stack_defines_rest_api_cognito_authorizer_and_routes():
    """Verify CoreApiStack defines RestApi, Cognito Authorizer, 6 business routes and /health mock."""
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(app, "IamStack", data_stack=data_stack)
    auth_stack = AuthStack(app, "AuthStack", data_stack=data_stack, iam_stack=iam_stack)
    core_api_stack = CoreApiStack(app, "CoreApiStack", auth_stack=auth_stack)
    template = Template.from_stack(core_api_stack)

    # 1. RestApi
    apis = template.find_resources("AWS::ApiGateway::RestApi")
    assert len(apis) == 1
    assert list(apis.values())[0]["Properties"]["Name"] == "SatiBot-CoreApi"

    # 2. Cognito Authorizer
    authorizers = template.find_resources("AWS::ApiGateway::Authorizer")
    assert len(authorizers) == 1
    auth_props = list(authorizers.values())[0]["Properties"]
    assert auth_props["Type"] == "COGNITO_USER_POOLS"

    # 3. Resources & Methods (chat, confirm, connections, external-id, verify, history, health)
    resources = template.find_resources("AWS::ApiGateway::Resource")
    path_parts = {r["Properties"]["PathPart"] for r in resources.values()}
    assert {"chat", "confirm", "connections", "external-id", "verify", "history", "health"}.issubset(path_parts)

    methods = template.find_resources("AWS::ApiGateway::Method")
    http_methods = {f"{m['Properties']['HttpMethod']}" for m in methods.values()}
    assert "POST" in http_methods
    assert "GET" in http_methods

