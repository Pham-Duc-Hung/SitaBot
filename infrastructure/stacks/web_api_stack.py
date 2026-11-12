"""Authenticated HTTP routes between the website and the live backend aliases."""

from aws_cdk import Aws, CfnOutput, CfnParameter, Fn, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_lambda as lambda_
from constructs import Construct


class WebApiStack(Stack):
    """Bind the existing Cognito pool and backend aliases without duplicating them."""

    ROUTES = {
        "chatbot": (("POST", "/api/chat"), ("POST", "/api/confirm")),
        "history": (("GET", "/api/history"),),
        "connector": (
            ("GET", "/api/connections"),
            ("POST", "/api/connections/external-id"),
            ("POST", "/api/connections/verify"),
        ),
    }

    def __init__(self, scope: Construct, construct_id: str, grant_backend_invoke: bool = True, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "ap-southeast-1":
            raise ValueError("The website API must run in ap-southeast-1")

        pool_id = CfnParameter(
            self, "UserPoolId", type="String",
            allowed_pattern=r"ap-southeast-1_[A-Za-z0-9]+",
        )
        client_id = CfnParameter(self, "UserPoolClientId", type="String", min_length=1)
        alias_arns = {
            name: CfnParameter(
                self, f"{name.title()}LiveAliasArn", type="String",
                allowed_pattern=r"arn:aws:lambda:ap-southeast-1:\d{12}:function:[A-Za-z0-9_-]+:live",
            )
            for name in self.ROUTES
        }

        api = apigwv2.CfnApi(self, "HttpApi", name="SatiBotWebApi", protocol_type="HTTP")
        apigwv2.CfnStage(self, "DefaultStage", api_id=api.ref, stage_name="$default", auto_deploy=True)
        authorizer = apigwv2.CfnAuthorizer(
            self,
            "CognitoJwtAuthorizer",
            api_id=api.ref,
            name="SatiBotCognitoJwt",
            authorizer_type="JWT",
            identity_source=["$request.header.Authorization"],
            jwt_configuration=apigwv2.CfnAuthorizer.JWTConfigurationProperty(
                audience=[client_id.value_as_string],
                issuer=Fn.join("", ["https://cognito-idp.", Aws.REGION, ".amazonaws.com/", pool_id.value_as_string]),
            ),
        )

        source_arn = Fn.join("", ["arn:", Aws.PARTITION, ":execute-api:", Aws.REGION,
                                  ":", Aws.ACCOUNT_ID, ":", api.ref, "/*"])

        health = lambda_.Function(
            self, "HealthFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.lambda_handler",
            code=lambda_.Code.from_inline(
                "import json\nimport os\n"
                "def lambda_handler(event, context):\n"
                "    return {'statusCode': 200, 'headers': {'Content-Type': 'application/json', "
                "'Cache-Control': 'no-store'}, 'body': json.dumps({'status': 'API_READY', "
                "'region': os.environ['AWS_REGION']})}\n"
            ),
        )
        health_integration = apigwv2.CfnIntegration(
            self, "HealthIntegration", api_id=api.ref, integration_type="AWS_PROXY",
            integration_uri=Fn.join("", ["arn:", Aws.PARTITION, ":apigateway:", Aws.REGION,
                                           ":lambda:path/2015-03-31/functions/", health.function_arn, "/invocations"]),
            payload_format_version="2.0",
        )
        lambda_.CfnPermission(
            self, "HealthApiInvokePermission", action="lambda:InvokeFunction",
            function_name=health.function_arn, principal="apigateway.amazonaws.com",
            source_arn=source_arn,
        )
        apigwv2.CfnRoute(
            self, "GetHealthRoute", api_id=api.ref, route_key="GET /api/health",
            authorization_type="NONE",
            target=Fn.join("", ["integrations/", health_integration.ref]),
        )

        for name, routes in self.ROUTES.items():
            alias_arn = alias_arns[name].value_as_string
            integration = apigwv2.CfnIntegration(
                self,
                f"{name.title()}Integration",
                api_id=api.ref,
                integration_type="AWS_PROXY",
                integration_uri=Fn.join("", ["arn:", Aws.PARTITION, ":apigateway:", Aws.REGION,
                                               ":lambda:path/2015-03-31/functions/", alias_arn, "/invocations"]),
                payload_format_version="2.0",
            )
            if grant_backend_invoke:
                lambda_.CfnPermission(
                    self, f"{name.title()}ApiInvokePermission",
                    action="lambda:InvokeFunction", function_name=alias_arn,
                    principal="apigateway.amazonaws.com", source_arn=source_arn,
                )
            for method, path in routes:
                apigwv2.CfnRoute(
                    self,
                    f"{name.title()}{method}{path.replace('/', ' ').title().replace(' ', '')}Route",
                    api_id=api.ref,
                    route_key=f"{method} {path}",
                    authorization_type="JWT",
                    authorizer_id=authorizer.ref,
                    target=Fn.join("", ["integrations/", integration.ref]),
                )

        CfnOutput(self, "ApiEndpointUrl", value=api.attr_api_endpoint)
        CfnOutput(self, "ApiGatewaySourceArn", value=source_arn)
        CfnOutput(self, "ApiId", value=api.ref)
        CfnOutput(self, "ApiOriginDomain", value=Fn.join("", [api.ref, ".execute-api.", Aws.REGION, ".amazonaws.com"]))
