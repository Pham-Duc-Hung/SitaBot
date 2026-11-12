"""AppSync must isolate subscriptions by Cognito identity and chat session."""

import runpy

from aws_cdk.assertions import Template


def test_appsync_api_and_resolvers_use_separate_publisher_and_browser_auth():
    stack = runpy.run_module("infrastructure.appsync_app")["stack"]
    template = Template.from_stack(stack)
    api = next(iter(template.find_resources("AWS::AppSync::GraphQLApi").values()))["Properties"]
    assert api["AuthenticationType"] == "AMAZON_COGNITO_USER_POOLS"
    assert api["AdditionalAuthenticationProviders"] == [{"AuthenticationType": "AWS_IAM"}]
    assert api["UserPoolConfig"]["UserPoolId"] == {"Ref": "UserPoolId"}
    assert "API_KEY" not in str(api)

    schema = next(iter(template.find_resources("AWS::AppSync::GraphQLSchema").values()))
    assert "publishActionResult(event: ActionResultInput!): ActionResult @aws_iam" in schema["Properties"]["Definition"]
    assert "onActionResult(session_id: ID!): ActionResult" in schema["Properties"]["Definition"]
    assert "@aws_cognito_user_pools" in schema["Properties"]["Definition"]

    sources = template.find_resources("AWS::AppSync::DataSource")
    assert len(sources) == 1
    assert next(iter(sources.values()))["Properties"]["Type"] == "NONE"
    resolvers = {value["Properties"]["FieldName"]: value["Properties"]
                 for value in template.find_resources("AWS::AppSync::Resolver").values()}
    assert set(resolvers) == {"publishActionResult", "onActionResult"}
    assert all(value["Runtime"] == {"Name": "APPSYNC_JS", "RuntimeVersion": "1.0.0"}
               for value in resolvers.values())
    filter_code = resolvers["onActionResult"]["Code"]
    assert "ctx.identity.sub" in filter_code
    assert "ctx.args.session_id" in filter_code
    assert "setSubscriptionFilter" in filter_code
