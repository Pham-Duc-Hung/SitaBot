"""Cognito sign-in for the isolated CloudFront preview."""

import re

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct


class WebAuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, web_origin: str,
                 domain_prefix: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "ap-southeast-1":
            raise ValueError("Cognito must run with the primary backend in ap-southeast-1")
        if not re.fullmatch(r"https://[a-z0-9.-]+", web_origin):
            raise ValueError("web_origin must be an HTTPS hostname without a path")
        if not re.fullmatch(r"[a-z0-9-]{1,63}", domain_prefix):
            raise ValueError("Cognito domain prefix must contain lowercase letters, digits, or hyphens")

        callback = web_origin + "/"
        pool = cognito.UserPool(
            self, "UserPool", user_pool_name="SatiBotPreviewUsers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "BrowserClient", generate_secret=False,
            prevent_user_existence_errors=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[callback], logout_urls=[callback],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )
        pool.add_domain(
            "HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,
        )
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "HostedDomain", value=f"{domain_prefix}.auth.ap-southeast-1.amazoncognito.com")
        CfnOutput(self, "CallbackUrl", value=callback)
