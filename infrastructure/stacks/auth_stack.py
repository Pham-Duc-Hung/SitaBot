"""
AuthStack: AWS Cognito User Pool, App Client, Groups, and Post-Confirmation Trigger.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)

Chức năng:
  - Khai báo AWS Cognito User Pool phục vụ xác thực người dùng (đăng nhập bằng Email).
  - Cấu hình Password Policy tuân thủ tiêu chuẩn an ninh (tối thiểu 8 ký tự, hoa, thường, số, ký tự đặc biệt).
  - Khai báo User Pool App Client cho Web React SPA (không dùng client secret, hỗ trợ USER_PASSWORD_AUTH và USER_SRP_AUTH).
  - Khai báo 2 User Groups: 'admin' (precedence 1) và 'user' (precedence 10).
  - Tích hợp Lambda Trigger Post-Confirmation trỏ vào module auth_handler để tự động tạo bản ghi người dùng trong DynamoDB.
  - Xuất các CloudFormation Outputs (UserPoolId, UserPoolArn, UserPoolClientId) phục vụ CoreApiStack và Web Frontend.
"""

import os
from pathlib import Path
from typing import Optional

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .data_stack import DataStack
from .iam_stack import IamStack


class AuthStack(Stack):
    """
    Quản lý hạ tầng xác thực danh tính người dùng và phân quyền RBAC qua AWS Cognito.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_stack: Optional[DataStack] = None,
        iam_stack: Optional[IamStack] = None,
        admin_emails: Optional[str] = None,
        post_confirmation_code: Optional[lambda_.Code] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        root = Path(__file__).resolve().parents[2]
        zip_path = root / "dist" / "backend" / "auth_handler.zip"

        # 1. Lambda Post-Confirmation Execution Role
        if iam_stack and "auth_post_confirmation" in getattr(iam_stack, "roles", {}):
            post_confirmation_role = iam_stack.roles["auth_post_confirmation"]
        else:
            post_confirmation_role = iam.Role(
                self,
                "AuthPostConfirmationRole",
                role_name="SatiBot-AuthPostConfirmation-Role",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                description="Execution role for Cognito Post-Confirmation Lambda trigger",
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    )
                ],
            )
            # Cấp quyền DynamoDB trên bảng Users
            if data_stack and hasattr(data_stack, "users"):
                data_stack.users.grant_read_write_data(post_confirmation_role)
            else:
                post_confirmation_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                        resources=[
                            f"arn:aws:dynamodb:{self.region}:{self.account}:table/SatiBot-Users"
                        ],
                    )
                )

        # 2. Lambda Function: Post-Confirmation Trigger
        users_table_name = (
            data_stack.users.table_name
            if data_stack and hasattr(data_stack, "users")
            else "SatiBot-Users"
        )

        if post_confirmation_code is None and not zip_path.is_file():
            raise FileNotFoundError(
                "Missing dist/backend/auth_handler.zip. Run python scripts/package_backend.py --with-dependencies."
            )
        lambda_code = post_confirmation_code or lambda_.Code.from_asset(str(zip_path))

        self.post_confirmation_fn = lambda_.Function(
            self,
            "AuthPostConfirmationFunction",
            function_name="SatiBot-AuthPostConfirmation",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="backend.functions.auth_handler.index.lambda_handler",
            role=post_confirmation_role,
            code=lambda_code,
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "USERS_TABLE_NAME": users_table_name,
                "ADMIN_EMAILS": admin_emails or os.environ.get("ADMIN_EMAILS", ""),
            },
            description="Triggered after Cognito ConfirmSignUp to provision user profile in DynamoDB",
        )

        # 3. Cognito User Pool
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="SatiBot-UserPool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            sign_in_case_sensitive=False,
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            custom_attributes={
                "role": cognito.StringAttribute(min_len=1, max_len=20, mutable=True),
            },
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(7),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            lambda_triggers=cognito.UserPoolTriggers(
                post_confirmation=self.post_confirmation_fn,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # 4. User Pool Client (for React SPA Frontend)
        web_origin = cdk.CfnParameter(
            self, "FrontendOrigin", type="String",
            allowed_pattern=r"https://[A-Za-z0-9.-]+(:[0-9]+)?",
        ).value_as_string
        callback = cdk.Fn.join("", [web_origin, "/"])
        self.user_pool_client = self.user_pool.add_client(
            "WebUserPoolClient",
            user_pool_client_name="SatiBot-WebClient",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[callback], logout_urls=[callback],
            ),
            write_attributes=cognito.ClientAttributes().with_standard_attributes(
                email=True, fullname=True,
            ),
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            prevent_user_existence_errors=True,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
        )

        domain_prefix = cdk.Fn.sub("satibot-${AWS::AccountId}-${AWS::Region}")
        self.user_pool.add_domain(
            "HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,
        )
        CfnOutput(self, "HostedDomain", value=cdk.Fn.join("", [domain_prefix, ".auth.", self.region, ".amazoncognito.com"]))
        CfnOutput(self, "CallbackUrl", value=callback)

        # 5. User Groups: admin & user
        self.admin_group = cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admin",
            precedence=1,
            description="Administrator group with elevated privileges",
        )

        self.user_group = cognito.CfnUserPoolGroup(
            self,
            "UserGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="user",
            precedence=10,
            description="Standard user group with personal privileges",
        )

        # 6. CloudFormation Outputs
        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="ID của Cognito User Pool",
            export_name="SatiBot-UserPoolId",
        )
        CfnOutput(
            self,
            "UserPoolArn",
            value=self.user_pool.user_pool_arn,
            description="ARN của Cognito User Pool",
            export_name="SatiBot-UserPoolArn",
        )
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="Client ID của Cognito Web App Client (SPA)",
            export_name="SatiBot-UserPoolClientId",
        )
