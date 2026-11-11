"""Deploy the SatiBot action-result GraphQL subscription in the primary region."""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_appsync as appsync


ROOT = Path(__file__).resolve().parents[1]
GRAPHQL = ROOT / "backend" / "graphql"


app = cdk.App()
stack = cdk.Stack(app, "SatiBotAppSync", env=cdk.Environment(region="ap-southeast-1"))
user_pool_id = cdk.CfnParameter(
    stack, "UserPoolId", type="String",
    allowed_pattern=r"ap-southeast-1_[A-Za-z0-9]+",
)

api = appsync.CfnGraphQLApi(
    stack, "ActionResultsApi", name="SatiBotActionResults",
    authentication_type="AMAZON_COGNITO_USER_POOLS",
    user_pool_config=appsync.CfnGraphQLApi.UserPoolConfigProperty(
        user_pool_id=user_pool_id.value_as_string,
        aws_region="ap-southeast-1",
        default_action="ALLOW",
    ),
    additional_authentication_providers=[
        appsync.CfnGraphQLApi.AdditionalAuthenticationProviderProperty(
            authentication_type="AWS_IAM",
        ),
    ],
    xray_enabled=True,
)

schema = appsync.CfnGraphQLSchema(
    stack, "ActionResultsSchema", api_id=api.attr_api_id,
    definition=(GRAPHQL / "schema.graphql").read_text(encoding="utf-8"),
)
source = appsync.CfnDataSource(
    stack, "NoneDataSource", api_id=api.attr_api_id,
    name="ActionResultsNone", type="NONE",
)
for field, file_name, type_name in (
    ("publishActionResult", "publish.js", "Mutation"),
    ("onActionResult", "subscribe.js", "Subscription"),
):
    resolver = appsync.CfnResolver(
        stack, f"{field}Resolver", api_id=api.attr_api_id,
        type_name=type_name, field_name=field,
        data_source_name=source.name,
        kind="UNIT",
        runtime=appsync.CfnResolver.AppSyncRuntimeProperty(
            name="APPSYNC_JS", runtime_version="1.0.0",
        ),
        code=(GRAPHQL / file_name).read_text(encoding="utf-8"),
    )
    resolver.add_dependency(schema)
    resolver.add_dependency(source)

cdk.CfnOutput(stack, "AppSyncGraphqlUrl", value=api.attr_graph_ql_url)
cdk.CfnOutput(stack, "AppSyncRealtimeUrl", value=api.attr_realtime_url)
cdk.CfnOutput(stack, "AppSyncApiArn", value=api.attr_arn)
cdk.CfnOutput(stack, "AppSyncApiId", value=api.attr_api_id)
app.synth()
