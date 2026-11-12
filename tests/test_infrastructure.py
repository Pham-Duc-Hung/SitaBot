"""Template-level tests for the Sprint 2 infrastructure stacks."""

import json
import zipfile
import pytest
import subprocess
from pathlib import Path

from aws_cdk import App, Environment, Stack, aws_lambda as lambda_
from aws_cdk.assertions import Template
from aws_cdk import aws_cognito as cognito

from infrastructure.stacks.appsync_stack import AppSyncStack
from infrastructure.stacks.data_stack import DataStack
from infrastructure.stacks.iam_stack import IamStack
from infrastructure.stacks.auth_stack import AuthStack
from infrastructure.stacks.core_api_stack import CoreApiStack
from infrastructure.stacks.dns_failover_stack import DnsFailoverStack


TABLE_CONFIG = {
    "SatiBot-Users": ("user_id", None, False, True),
    "SatiBot-AwsConnections": ("user_id", None, False, True),
    "SatiBot-SessionState": ("user_id", "session_id", True, False),
    "SatiBot-PendingAction": ("action_id", None, True, False),
    "SatiBot-ActionHistory": ("user_id", "created_at", False, True),
}


def _synth_appsync():
    stack = AppSyncStack(
        App(), "AppSyncStack", env=Environment(region="ap-southeast-1")
    )
    return Template.from_stack(stack)


def test_appsync_auth_modes():
    template = _synth_appsync()
    template.resource_count_is("AWS::AppSync::ApiKey", 0)
    template.has_resource_properties("AWS::AppSync::GraphQLApi", {
        "AuthenticationType": "AMAZON_COGNITO_USER_POOLS",
        "UserPoolConfig": {
            "UserPoolId": {"Ref": "UserPoolId"},
            "AwsRegion": "ap-southeast-1",
            "DefaultAction": "ALLOW",
        },
        "AdditionalAuthenticationProviders": [{"AuthenticationType": "AWS_IAM"}],
    })
    parameter = template.to_json()["Parameters"]["UserPoolId"]
    assert "Default" not in parameter
    assert parameter["Type"] == "String"


def test_appsync_resolver_bindings():
    template = _synth_appsync()
    graphql = Path(__file__).resolve().parents[1] / "backend" / "graphql"
    template.resource_count_is("AWS::AppSync::Resolver", 2)
    sources = template.find_resources("AWS::AppSync::DataSource")
    assert len(sources) == 1
    source_id, source = next(iter(sources.items()))
    assert source["Properties"]["Type"] == "NONE"
    api_id = source["Properties"]["ApiId"]
    for type_name, field, filename in (
        ("Mutation", "publishActionResult", "publish.js"),
        ("Subscription", "onActionResult", "subscribe.js"),
    ):
        template.has_resource_properties("AWS::AppSync::Resolver", {
            "ApiId": api_id,
            "TypeName": type_name,
            "FieldName": field,
            "DataSourceName": source["Properties"]["Name"],
            "Runtime": {"Name": "APPSYNC_JS", "RuntimeVersion": "1.0.0"},
            "Code": (graphql / filename).read_text(encoding="utf-8"),
        })
    template.has_resource_properties("AWS::AppSync::GraphQLSchema", {
        "ApiId": api_id,
        "Definition": (graphql / "schema.graphql").read_bytes().decode("utf-8"),
    })
    schema_id = next(iter(template.find_resources("AWS::AppSync::GraphQLSchema")))
    for resolver in template.find_resources("AWS::AppSync::Resolver").values():
        assert {source_id, schema_id} <= set(resolver["DependsOn"])


def test_appsync_backend_outputs():
    template = _synth_appsync()
    api_id = next(iter(template.find_resources("AWS::AppSync::GraphQLApi")))
    outputs = template.to_json()["Outputs"]
    assert outputs["AppSyncApiArn"]["Value"] == {"Fn::GetAtt": [api_id, "Arn"]}
    assert outputs["AppSyncGraphqlUrl"]["Value"] == {
        "Fn::GetAtt": [api_id, "GraphQLUrl"]
    }


def test_appsync_accepts_cognito_construct_without_pool_id_parameter():
    app = App()
    auth = Stack(app, "Auth", env=Environment(region="ap-southeast-1"))
    pool = cognito.UserPool(auth, "Users")
    stack = AppSyncStack(
        app, "AppSync", user_pool=pool,
        env=Environment(region="ap-southeast-1"),
    )
    template = Template.from_stack(stack).to_json()
    assert "UserPoolId" not in template.get("Parameters", {})
    api = next(
        resource for resource in template["Resources"].values()
        if resource["Type"] == "AWS::AppSync::GraphQLApi"
    )
    assert "Fn::ImportValue" in api["Properties"]["UserPoolConfig"]["UserPoolId"]


def test_appsync_subscription_filter_isolates_users_and_sessions():
    """Run the existing JS resolver with mocked AppSync utilities, without AWS."""
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .* from '@aws-appsync\/utils';\r?\n/m, '')
  .replace(/export function /g, 'function ');
let filter;
const context = vm.createContext({
  extensions: { setSubscriptionFilter(value) { filter = JSON.parse(JSON.stringify(value)); } },
  util: {
    unauthorized() { throw new Error('Unauthorized'); },
    transform: { toSubscriptionFilter(value) { return value; } }
  }
});
vm.runInContext(source, context);
for (const [user, session] of [['alice', 'shared'], ['bob', 'shared'], ['alice', 'other']]) {
  const event = { identity: { sub: user }, args: { session_id: session } };
  context.request(event);
  context.response(event);
  assert.deepEqual(filter, { user_id: { eq: user }, session_id: { eq: session } });
}
assert.throws(() => context.request({ args: { session_id: 'shared' } }), /Unauthorized/);
assert.throws(() => context.request({ identity: {}, args: {} }), /Unauthorized/);
"""
    resolver = Path(__file__).resolve().parents[1] / "backend/graphql/subscribe.js"
    subprocess.run(
        ["node", "-e", script, str(resolver)],
        check=True, capture_output=True, text=True,
    )


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

    lambda_roles = [role for role in roles.values()
                    if role["Properties"]["RoleName"].endswith("-ExecutionRole")]
    assert len(lambda_roles) == 6
    for role in lambda_roles:
        statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert any(
            statement.get("Principal", {}).get("Service") == "lambda.amazonaws.com"
            and "sts:AssumeRole" in statement.get("Action", [])
            for statement in statements
        )


def test_iam_stack_provides_codedeploy_lambda_service_role():
    _, iam_template = _synth_stacks()
    roles = iam_template.find_resources("AWS::IAM::Role")
    codedeploy = next(role["Properties"] for role in roles.values()
                      if role["Properties"]["RoleName"] == "SatiBot-CodeDeploy-LambdaRole")
    assert ":iam::aws:policy/service-role/AWSCodeDeployRoleForLambda" in json.dumps(codedeploy)
    assert "codedeploy.amazonaws.com" in json.dumps(codedeploy["AssumeRolePolicyDocument"])


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


def test_auth_stack_defines_cognito_user_pool_client_and_groups(auth_code):
    """Verify AuthStack creates UserPool, SPA client without secret, and admin/user groups."""
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(app, "IamStack", data_stack=data_stack)
    auth_stack = AuthStack(app, "AuthStack", data_stack=data_stack, iam_stack=iam_stack, post_confirmation_code=auth_code)
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


def test_core_api_stack_defines_rest_api_cognito_authorizer_and_routes(auth_code):
    """Verify CoreApiStack defines RestApi, Cognito Authorizer, 6 business routes and /health mock."""
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(app, "IamStack", data_stack=data_stack)
    auth_stack = AuthStack(app, "AuthStack", data_stack=data_stack, iam_stack=iam_stack, post_confirmation_code=auth_code)
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


def test_dns_failover_stack_defines_hosted_zone_acm_and_failover_records(auth_code):
    """Verify DnsFailoverStack creates HostedZone, Wildcard ACM cert, /health HealthCheck, and Failover routing."""
    app = App()
    data_stack = DataStack(app, "DataStack")
    iam_stack = IamStack(app, "IamStack", data_stack=data_stack)
    auth_stack = AuthStack(app, "AuthStack", data_stack=data_stack, iam_stack=iam_stack, post_confirmation_code=auth_code)
    core_api_stack = CoreApiStack(app, "CoreApiStack", auth_stack=auth_stack)
    dns_stack = DnsFailoverStack(app, "DnsFailoverStack", core_api_stack=core_api_stack)
    template = Template.from_stack(dns_stack)

    # 1. Route 53 Public Hosted Zone
    zones = template.find_resources("AWS::Route53::HostedZone")
    assert len(zones) == 1
    assert list(zones.values())[0]["Properties"]["Name"] == "satibot.id.vn."

    # 2. ACM Wildcard Certificate
    certs = template.find_resources("AWS::CertificateManager::Certificate")
    assert len(certs) == 1
    cert_props = list(certs.values())[0]["Properties"]
    assert cert_props["DomainName"] == "satibot.id.vn"
    assert "*.satibot.id.vn" in cert_props.get("SubjectAlternativeNames", [])

    # 3. Route 53 Health Check (probes /health)
    health_checks = template.find_resources("AWS::Route53::HealthCheck")
    assert len(health_checks) == 1
    hc_config = list(health_checks.values())[0]["Properties"]["HealthCheckConfig"]
    assert hc_config["Type"] == "HTTPS"
    assert hc_config["ResourcePath"] == "/health"
    assert hc_config["Port"] == 443
    assert hc_config["RequestInterval"] == 10
    assert hc_config["FailureThreshold"] == 3

    # 4. Custom Domain Name for API Gateway
    domains = template.find_resources("AWS::ApiGateway::DomainName")
    assert len(domains) == 1
    assert list(domains.values())[0]["Properties"]["DomainName"] == "api.satibot.id.vn"

    # 5. Failover Record Sets (Primary & Secondary)
    record_sets = template.find_resources("AWS::Route53::RecordSet")
    assert len(record_sets) >= 2
    failover_types = {r["Properties"].get("Failover") for r in record_sets.values()}
    assert "PRIMARY" in failover_types
    assert "SECONDARY" in failover_types


@pytest.fixture
def auth_code(tmp_path_factory):
    from scripts.package_backend import source_entries, _write_deterministic_entry
    artifact = tmp_path_factory.mktemp("auth-code") / "auth_handler.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name, (payload, mode) in sorted(source_entries("auth_handler").items()):
            _write_deterministic_entry(archive, name, payload, mode)
    return lambda_.Code.from_asset(str(artifact))


def test_real_auth_appsync_and_api_contract(auth_code):
    app = App()
    env = Environment(region="ap-southeast-1")
    data = DataStack(app, "Data", env=env)
    roles = IamStack(app, "Roles", data_stack=data, env=env)
    auth = AuthStack(app, "Auth", data_stack=data, iam_stack=roles,
                     post_confirmation_code=auth_code, env=env)
    sync = AppSyncStack(app, "Sync", user_pool=auth.user_pool, env=env)
    api = CoreApiStack(app, "Api", auth_stack=auth, env=env)
    assembly = app.synth()
    auth_template = assembly.get_stack_by_name(auth.stack_name).template
    clients = [r["Properties"] for r in auth_template["Resources"].values()
               if r["Type"] == "AWS::Cognito::UserPoolClient"]
    assert set(clients[0]["WriteAttributes"]) == {"email", "name"}
    sync_template = assembly.get_stack_by_name(sync.stack_name).template
    assert "UserPoolId" not in sync_template.get("Parameters", {})
    api_template = assembly.get_stack_by_name(api.stack_name).template
    resources = api_template["Resources"]
    paths = {}
    def path_for(resource_id):
        resource = resources[resource_id]["Properties"]
        parent = resource["ParentId"]
        prefix = path_for(parent["Ref"]) if "Ref" in parent else ""
        return prefix + "/" + resource["PathPart"]
    for key, resource in resources.items():
        if resource["Type"] == "AWS::ApiGateway::Resource":
            paths[key] = path_for(key)
    expected = {("POST", "/chat"): "ChatbotLiveAliasArn",
                ("POST", "/confirm"): "ChatbotLiveAliasArn",
                ("GET", "/connections"): "ConnectorLiveAliasArn",
                ("POST", "/connections/external-id"): "ConnectorLiveAliasArn",
                ("POST", "/connections/verify"): "ConnectorLiveAliasArn",
                ("GET", "/history"): "HistoryLiveAliasArn"}
    found = {}
    for resource in resources.values():
        if resource["Type"] != "AWS::ApiGateway::Method":
            continue
        props = resource["Properties"]
        resource_id = props["ResourceId"].get("Ref")
        if resource_id not in paths:
            continue
        route = (props["HttpMethod"], paths[resource_id])
        if route in expected:
            assert props["AuthorizationType"] == "COGNITO_USER_POOLS"
            assert expected[route] in json.dumps(props["Integration"]["Uri"])
            found[route] = expected[route]
    assert found == expected
    assert "/prod/" in json.dumps(api_template["Outputs"]["ApiGatewaySourceArn"])
    assert not Template.from_stack(api).find_resources("AWS::Lambda::Permission")
    for response in Template.from_stack(api).find_resources("AWS::ApiGateway::GatewayResponse").values():
        assert "FrontendOrigin" in json.dumps(response["Properties"]["ResponseParameters"])


def test_demo_auth_has_hosted_login_and_restricted_browser_attributes(tmp_path):
    from aws_cdk import App, Environment, aws_lambda as lambda_
    from aws_cdk.assertions import Template
    from infrastructure.stacks.auth_stack import AuthStack
    app = App(outdir=str(tmp_path / "auth"))
    stack = AuthStack(app, "DemoAuth", env=Environment(region="ap-southeast-1"),
                      post_confirmation_code=lambda_.Code.from_inline("def handler(event, context): return event"))
    template = Template.from_stack(stack)
    client = next(iter(template.find_resources("AWS::Cognito::UserPoolClient").values()))["Properties"]
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["GenerateSecret"] is False
    assert "custom:role" not in client["WriteAttributes"]
    assert client["CallbackURLs"] == client["LogoutURLs"]
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 1)
    for name in ("HostedDomain", "CallbackUrl", "UserPoolId", "UserPoolClientId"):
        template.has_output(name, {})
