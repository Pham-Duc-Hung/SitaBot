"""The browser API must require Cognito JWTs and invoke live aliases."""

import json

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infrastructure.stacks.web_api_stack import WebApiStack


def test_web_api_routes_are_authenticated_and_target_live_aliases(monkeypatch):
    stack = WebApiStack(App(), "WebApiTest", env=Environment(region="ap-southeast-1"))
    template = Template.from_stack(stack)
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert {resource["Properties"]["RouteKey"] for resource in routes.values()} == {
        "GET /api/health",
        "POST /api/chat", "POST /api/confirm", "GET /api/history",
        "GET /api/connections", "POST /api/connections/external-id",
        "POST /api/connections/verify",
    }
    protected = [resource["Properties"] for resource in routes.values()
                 if resource["Properties"]["RouteKey"] != "GET /api/health"]
    assert all(resource["AuthorizationType"] == "JWT" for resource in protected)
    assert all("AuthorizerId" in resource for resource in protected)
    health = next(resource["Properties"] for resource in routes.values()
                  if resource["Properties"]["RouteKey"] == "GET /api/health")
    assert health["AuthorizationType"] == "NONE"

    authorizer = next(iter(template.find_resources("AWS::ApiGatewayV2::Authorizer").values()))["Properties"]
    assert authorizer["AuthorizerType"] == "JWT"
    assert authorizer["IdentitySource"] == ["$request.header.Authorization"]
    assert authorizer["JwtConfiguration"]["Audience"] == [{"Ref": "UserPoolClientId"}]

    integrations = template.find_resources("AWS::ApiGatewayV2::Integration")
    assert len(integrations) == 4
    assert all(resource["Properties"]["PayloadFormatVersion"] == "2.0" for resource in integrations.values())
    permissions = template.find_resources("AWS::Lambda::Permission")
    assert len(permissions) == 4
    assert all(resource["Properties"]["Principal"] == "apigateway.amazonaws.com" for resource in permissions.values())
    assert all("SourceArn" in resource["Properties"] for resource in permissions.values())

    health_code = next(iter(template.find_resources("AWS::Lambda::Function").values()))["Properties"]["Code"]["ZipFile"]
    namespace = {}
    exec(compile(health_code, "health/index.py", "exec"), namespace)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    result = namespace["lambda_handler"]({}, None)
    assert result["statusCode"] == 200
    assert result["headers"]["Cache-Control"] == "no-store"
    assert json.loads(result["body"]) == {"status": "API_READY", "region": "ap-southeast-1"}


def test_backend_owned_alias_permissions_leave_only_health_permission():
    stack = WebApiStack(App(), "WebApiBackendOwned",
                        grant_backend_invoke=False,
                        env=Environment(region="ap-southeast-1"))
    template = Template.from_stack(stack)
    permissions = template.find_resources("AWS::Lambda::Permission")
    assert len(permissions) == 1
    template.has_output("ApiGatewaySourceArn", {})
    template.has_output("ApiEndpointUrl", {})
