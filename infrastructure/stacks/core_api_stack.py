"""
CoreApiStack: Amazon API Gateway REST API, Cognito JWT Authorizer, CORS, and Routes.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)

Chức năng:
  - Khai báo API Gateway REST API ('SatiBot-CoreApi') với cấu hình CORS toàn diện cho Web React Frontend.
  - Cấu hình Cognito UserPools Authorizer bảo vệ các endpoints nhạy cảm bằng JWT Token.
  - Cấp quyền 'grant_invoke' từ API Gateway tới 3 Lambda HTTP nghiệp vụ (chatbot, connector, history).
  - Ghép chính xác 6 Routes nghiệp vụ tới alias 'live':
      1. POST /chat                     -> chatbot:live
      2. POST /confirm                  -> chatbot:live
      3. GET  /connections              -> connector:live
      4. POST /connections/external-id  -> connector:live
      5. POST /connections/verify       -> connector:live
      6. GET  /history                  -> history:live
  - Cung cấp Endpoint công khai GET /health sử dụng Mock Integration (HTTP 200) phục vụ Route 53 Health Check & Failover.
  - Xuất các CloudFormation Outputs (ApiUrl, ApiId, ApiStage, HealthEndpointUrl).
"""

from typing import Dict, Optional

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Stack,
    aws_apigateway as apigateway,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .auth_stack import AuthStack


class CoreApiStack(Stack):
    """
    Quản lý hạ tầng API Gateway, phân luồng truy cập và kiểm soát an ninh định danh người dùng.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        auth_stack: Optional[AuthStack] = None,
        user_pool: Optional[cognito.IUserPool] = None,
        live_aliases: Optional[Dict[str, lambda_.IFunction]] = None,
        chatbot_alias: Optional[lambda_.IFunction] = None,
        connector_alias: Optional[lambda_.IFunction] = None,
        history_alias: Optional[lambda_.IFunction] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 1. Resolve Cognito User Pool
        target_user_pool = (
            (auth_stack and getattr(auth_stack, "user_pool", None))
            or user_pool
            or cognito.UserPool.from_user_pool_id(
                self,
                "ImportedAuthUserPool",
                user_pool_id=cdk.Fn.import_value("SatiBot-UserPoolId"),
            )
        )

        # 2. Resolve Lambda Functions / Live Aliases
        chatbot_fn = (
            (live_aliases and live_aliases.get("chatbot"))
            or chatbot_alias
            or self._import_fallback_fn("chatbot")
        )
        connector_fn = (
            (live_aliases and live_aliases.get("connector"))
            or connector_alias
            or self._import_fallback_fn("connector")
        )
        history_fn = (
            (live_aliases and live_aliases.get("history"))
            or history_alias
            or self._import_fallback_fn("history")
        )

        frontend_origin = cdk.CfnParameter(
            self, "FrontendOrigin", type="String",
            allowed_pattern=r"https?://[A-Za-z0-9.-]+(:[0-9]+)?",
            description="Exact frontend origin, including scheme and optional port.",
        ).value_as_string

        # 3. Create API Gateway REST API with full CORS support
        self.api = apigateway.RestApi(
            self,
            "SatiBotRestApi",
            rest_api_name="SatiBot-CoreApi",
            description="API Gateway chính của hệ thống SatiBot ChatOps, định tuyến Lambda backend",
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                metrics_enabled=True,
                logging_level=apigateway.MethodLoggingLevel.INFO,
                data_trace_enabled=False,
            ),
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=[frontend_origin],
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=[
                    "Content-Type",
                    "Authorization",
                    "X-Amz-Date",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                    "X-Amz-User-Agent",
                    "x-session-id",
                    "x-action-id",
                ],
                allow_credentials=False,
                max_age=Duration.hours(1),
            ),
            endpoint_types=[apigateway.EndpointType.REGIONAL],
        )

        # 4. Cognito UserPools JWT Authorizer
        self.authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "SatiBotCognitoAuthorizer",
            cognito_user_pools=[target_user_pool],
            authorizer_name="SatiBot-CognitoAuthorizer",
            identity_source=apigateway.IdentitySource.header("Authorization"),
            results_cache_ttl=Duration.minutes(5),
        )

        # Lambda Integrations (Proxy Mode)
        chatbot_integration = self._proxy_integration(chatbot_fn)
        connector_integration = self._proxy_integration(connector_fn)
        history_integration = self._proxy_integration(history_fn)

        # 5. Define 6 Business Routes to :live aliases
        # Từng method sử dụng Cognito User Pool Authorizer để bảo vệ endpoint

        # Route 1: POST /chat (Chatbot tương tác hội thoại tự nhiên)
        chat_resource = self.api.root.add_resource("chat")
        chat_resource.add_method(
            "POST",
            chatbot_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # Route 2: POST /confirm (Xác nhận các hành động ghi hạ tầng EC2/S3/Lambda)
        confirm_resource = self.api.root.add_resource("confirm")
        confirm_resource.add_method(
            "POST",
            chatbot_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # Route 3: GET /connections (Lấy thông tin kết nối AWS của user)
        connections_resource = self.api.root.add_resource("connections")
        connections_resource.add_method(
            "GET",
            connector_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # Route 4: POST /connections/external-id (Tạo ExternalId ngẫu nhiên để gắn Role ARN)
        ext_id_resource = connections_resource.add_resource("external-id")
        ext_id_resource.add_method(
            "POST",
            connector_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # Route 5: POST /connections/verify (Xác minh quyền AssumeRole tài khoản AWS của user)
        verify_resource = connections_resource.add_resource("verify")
        verify_resource.add_method(
            "POST",
            connector_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # Route 6: GET /history (Truy vấn lịch sử các lệnh ChatOps trong quá khứ)
        history_resource = self.api.root.add_resource("history")
        history_resource.add_method(
            "GET",
            history_integration,
            authorizer=self.authorizer,
            authorization_type=apigateway.AuthorizationType.COGNITO,
        )

        # 6. Route 7: GET /health (Mock Integration HTTP 200 không tốn chi phí Lambda)
        # Endpoint công khai phục vụ kiểm tra sức khỏe hệ thống và Route 53 DNS Failover
        health_resource = self.api.root.add_resource("health")
        mock_health_integration = apigateway.MockIntegration(
            integration_responses=[
                apigateway.IntegrationResponse(
                    status_code="200",
                    response_templates={
                        "application/json": (
                            '{"status": "healthy", "service": "SatiBot Core API", '
                            '"region": "' + Aws.REGION + '", '
                            '"timestamp": "$context.requestTime"}'
                        )
                    },
                    response_parameters={
                        "method.response.header.Access-Control-Allow-Origin": cdk.Fn.join("", ["'", frontend_origin, "'"]),
                    },
                )
            ],
            passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
            request_templates={"application/json": '{"statusCode": 200}'},
        )
        health_resource.add_method(
            "GET",
            mock_health_integration,
            authorization_type=apigateway.AuthorizationType.NONE,
            method_responses=[
                apigateway.MethodResponse(
                    status_code="200",
                    response_parameters={
                        "method.response.header.Access-Control-Allow-Origin": True,
                    },
                )
            ],
        )

        for name, response_type in (
            ("ClientErrors", apigateway.ResponseType.DEFAULT_4_XX),
            ("ServerErrors", apigateway.ResponseType.DEFAULT_5_XX),
        ):
            self.api.add_gateway_response(
                name, type=response_type,
                response_headers={
                    "Access-Control-Allow-Origin": cdk.Fn.join("", ["'", frontend_origin, "'"]),
                },
            )
        origin_output = CfnOutput(self, "FrontendOriginOutput", value=frontend_origin)
        origin_output.override_logical_id("FrontendOrigin")
        CfnOutput(self, "ApiGatewaySourceArn", value=self.api.arn_for_execute_api(stage="prod"))

        # 7. CloudFormation Outputs
        self.api_url = self.api.url
        CfnOutput(
            self,
            "ApiEndpointUrl",
            value=self.api.url,
            description="URL gốc của API Gateway (Stage prod)",
            export_name="SatiBot-ApiEndpointUrl",
        )
        CfnOutput(
            self,
            "RestApiId",
            value=self.api.rest_api_id,
            description="ID của API Gateway REST API",
            export_name="SatiBot-RestApiId",
        )
        CfnOutput(
            self,
            "HealthCheckUrl",
            value=f"{self.api.url}health",
            description="URL kiểm tra sức khỏe /health phục vụ Route 53 DNS Failover",
            export_name="SatiBot-HealthCheckUrl",
        )

    @staticmethod
    def _proxy_integration(function: lambda_.IFunction) -> apigateway.Integration:
        # SatiBotBackend owns the alias invoke permissions after API output handoff.
        return apigateway.Integration(
            type=apigateway.IntegrationType.AWS_PROXY,
            integration_http_method="POST",
            uri=cdk.Fn.sub(
                "arn:${AWS::Partition}:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${FunctionArn}/invocations",
                {"FunctionArn": function.function_arn},
            ),
        )

    def _import_fallback_fn(self, name: str) -> lambda_.IFunction:
        """
        Nhập tham chiếu Lambda Function theo ARN chuẩn để hỗ trợ standalone synthesis
        khi chưa liên kết trực tiếp với stack triển khai Lambda backend.
        """
        return lambda_.Function.from_function_attributes(
            self,
            f"Imported{name.title()}LiveAlias",
            function_arn=cdk.CfnParameter(
                self, f"{name.title()}LiveAliasArn", type="String",
                allowed_pattern=r"arn:aws:lambda:ap-southeast-1:\d{12}:function:[A-Za-z0-9_-]+:live",
            ).value_as_string,
            same_environment=True,
            skip_permissions=True,
        )
