"""The preview login must use a public PKCE-capable browser client."""

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infrastructure.stacks.web_auth_stack import WebAuthStack


def test_preview_user_pool_and_browser_client():
    stack = WebAuthStack(
        App(), "AuthTest", web_origin="https://example.cloudfront.net",
        domain_prefix="satibot-test-example",
        env=Environment(region="ap-southeast-1"),
    )
    template = Template.from_stack(stack)
    pool = next(iter(template.find_resources("AWS::Cognito::UserPool").values()))
    assert pool["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["DeletionPolicy"] == "Retain"
    client = next(iter(template.find_resources("AWS::Cognito::UserPoolClient").values()))["Properties"]
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["CallbackURLs"] == ["https://example.cloudfront.net/"]
    assert "implicit" not in client["AllowedOAuthFlows"]
