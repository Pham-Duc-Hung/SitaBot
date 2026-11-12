"""AppSync action results shared by Formatter and the Cognito web client."""

from pathlib import Path

from aws_cdk import CfnOutput, CfnParameter, Stack
from aws_cdk import aws_appsync as appsync, aws_cognito as cognito
from constructs import Construct


class AppSyncStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        user_pool: cognito.IUserPool | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if user_pool is None:
            pool_id = CfnParameter(
                self,
                "UserPoolId",
                type="String",
                allowed_pattern=r"[\w-]+_[0-9A-Za-z]+",
                description="Cognito User Pool used by the SatiBot frontend.",
            )
            user_pool = cognito.UserPool.from_user_pool_id(
                self, "ExistingUserPool", pool_id.value_as_string
            )

        graphql = Path(__file__).resolve().parents[2] / "backend" / "graphql"
        self.api = appsync.GraphqlApi(
            self,
            "ActionResultsApi",
            name="SatiBotActionResults",
            definition=appsync.Definition.from_file(str(graphql / "schema.graphql")),
            authorization_config=appsync.AuthorizationConfig(
                default_authorization=appsync.AuthorizationMode(
                    authorization_type=appsync.AuthorizationType.USER_POOL,
                    user_pool_config=appsync.UserPoolConfig(user_pool=user_pool),
                ),
                additional_authorization_modes=[
                    appsync.AuthorizationMode(
                        authorization_type=appsync.AuthorizationType.IAM
                    )
                ],
            ),
        )
        results = self.api.add_none_data_source("ActionResults")
        results.create_resolver(
            "PublishActionResult",
            type_name="Mutation",
            field_name="publishActionResult",
            runtime=appsync.FunctionRuntime.JS_1_0_0,
            code=appsync.Code.from_inline(
                (graphql / "publish.js").read_text(encoding="utf-8")
            ),
        )
        results.create_resolver(
            "OnActionResult",
            type_name="Subscription",
            field_name="onActionResult",
            runtime=appsync.FunctionRuntime.JS_1_0_0,
            code=appsync.Code.from_inline(
                (graphql / "subscribe.js").read_text(encoding="utf-8")
            ),
        )

        CfnOutput(self, "AppSyncApiArn", value=self.api.arn)
        CfnOutput(self, "AppSyncGraphqlUrl", value=self.api.graphql_url)
