"""Least-privilege Lambda execution roles for the Sprint 2 backend."""

import re

from aws_cdk import Aws, CfnOutput, Stack, Token, aws_dynamodb as dynamodb, aws_iam as iam
from constructs import Construct

from .data_stack import DataStack


class IamStack(Stack):
    """Create one execution role per Lambda and grant access to explicit tables."""

    ROLE_NAMES = (
        "chatbot",
        "dispatcher",
        "formatter",
        "connector",
        "history",
        "auth_post_confirmation",
    )

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_stack: DataStack,
        llm_secret_arn: str | None = None,
        state_machine_arn: str | None = None,
        dispatcher_function_arn: str | None = None,
        formatter_function_arn: str | None = None,
        appsync_api_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.roles: dict[str, iam.Role] = {}
        tables = data_stack.tables

        # Callers can wire CDK tokens directly or supply deployed ARNs through
        # matching `cdk -c` context keys without changing the shared app entrypoint.
        state_machine_arn = state_machine_arn or self.node.try_get_context("state_machine_arn")
        dispatcher_function_arn = dispatcher_function_arn or self.node.try_get_context("dispatcher_function_arn")
        formatter_function_arn = formatter_function_arn or self.node.try_get_context("formatter_function_arn")
        appsync_api_arn = appsync_api_arn or self.node.try_get_context("appsync_api_arn")
        for value, pattern in (
            (state_machine_arn, r"arn:[\w-]+:states:[\w-]+:\d{12}:stateMachine:[\w-]+"),
            (dispatcher_function_arn, r"arn:[\w-]+:lambda:[\w-]+:\d{12}:function:[\w-]+(?::[\w-]+)?"),
            (formatter_function_arn, r"arn:[\w-]+:lambda:[\w-]+:\d{12}:function:[\w-]+(?::[\w-]+)?"),
            (appsync_api_arn, r"arn:[\w-]+:appsync:[\w-]+:\d{12}:apis/[A-Za-z0-9_-]+"),
        ):
            if value and (not isinstance(value, str) or
                          (not Token.is_unresolved(value) and not re.fullmatch(pattern, value))):
                raise ValueError("Provide a concrete resource ARN or a CDK ARN token; wildcards are not supported.")
        if bool(dispatcher_function_arn) != bool(formatter_function_arn):
            raise ValueError("Configure both Dispatcher and Formatter function ARNs for the workflow role.")

        for name in self.ROLE_NAMES:
            role = iam.Role(
                self,
                f"{name}ExecutionRole",
                role_name=f"SatiBot-{self._role_label(name)}-ExecutionRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                description=f"SatiBot {name} Lambda execution role",
            )
            self.roles[name] = role
            self._grant_logs(role)

        self._grant_table(self.roles["chatbot"], tables["SessionState"],
                          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                          "dynamodb:Query")
        self._grant_table(self.roles["chatbot"], tables["PendingAction"],
                          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem")
        self._grant_table(self.roles["chatbot"], tables["AwsConnections"],
                          "dynamodb:GetItem")

        self._grant_table(self.roles["dispatcher"], tables["PendingAction"],
                          "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem")
        self._grant_table(self.roles["dispatcher"], tables["AwsConnections"],
                          "dynamodb:GetItem")
        self.roles["dispatcher"].add_to_policy(self._assume_customer_role_policy())

        self._grant_table(self.roles["formatter"], tables["ActionHistory"],
                          "dynamodb:PutItem", "dynamodb:UpdateItem")

        self._grant_table(self.roles["connector"], tables["AwsConnections"],
                          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem")
        self.roles["connector"].add_to_policy(self._assume_customer_role_policy())

        self._grant_table(self.roles["history"], tables["ActionHistory"],
                          "dynamodb:Query", "dynamodb:GetItem")

        self._grant_table(self.roles["auth_post_confirmation"], tables["Users"],
                          "dynamodb:GetItem", "dynamodb:PutItem")

        if llm_secret_arn:
            secret_policy = iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[llm_secret_arn],
            )
            self.roles["chatbot"].add_to_policy(secret_policy)
            self.roles["formatter"].add_to_policy(secret_policy)

        for name, role in self.roles.items():
            CfnOutput(self, f"{self._role_label(name)}RoleArn", value=role.role_arn)

        self.codedeploy_role = iam.Role(
            self,
            "CodeDeployLambdaRole",
            role_name="SatiBot-CodeDeploy-LambdaRole",
            assumed_by=iam.ServicePrincipal("codedeploy.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSCodeDeployRoleForLambda"
            )],
        )
        CfnOutput(self, "CodeDeployRoleArn", value=self.codedeploy_role.role_arn)

        if state_machine_arn:
            self.roles["chatbot"].add_to_policy(iam.PolicyStatement(
                actions=["states:StartExecution"], resources=[state_machine_arn]))
        if appsync_api_arn:
            self.roles["formatter"].add_to_policy(iam.PolicyStatement(
                actions=["appsync:GraphQL"],
                resources=[appsync_api_arn + "/types/Mutation/fields/publishActionResult"]))
        self.workflow_role = None
        if dispatcher_function_arn and formatter_function_arn:
            self.workflow_role = iam.Role(
                self, "ActionWorkflowExecutionRole",
                assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
                description="SatiBot action workflow invokes Dispatcher then Formatter",
            )
            self.workflow_role.add_to_policy(iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[dispatcher_function_arn, formatter_function_arn]))
            CfnOutput(self, "ActionWorkflowRoleArn", value=self.workflow_role.role_arn)

    @staticmethod
    def _role_label(name: str) -> str:
        return "".join(part.capitalize() for part in name.split("_"))

    @staticmethod
    def _grant_table(role: iam.Role, table: dynamodb.ITable, *actions: str) -> None:
        role.add_to_policy(iam.PolicyStatement(actions=list(actions), resources=[table.table_arn]))

    def _assume_customer_role_policy(self) -> iam.PolicyStatement:
        target_arn = (
            f"arn:{Aws.PARTITION}:iam::*:role/satibot/SatiBotAccessRole"
        )
        return iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[target_arn],
        )

    def _grant_logs(self, role: iam.Role) -> None:
        # CreateLogGroup does not support resource-level permissions. Writes to
        # streams are restricted to this backend stack's Lambda name prefix.
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogGroup"], resources=["*"]
        ))
        role.add_to_policy(iam.PolicyStatement(
            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:{Aws.PARTITION}:logs:{self.region}:{self.account}:"
                "log-group:/aws/lambda/SatiBotBackend-*:*",
                f"arn:{Aws.PARTITION}:logs:{self.region}:{self.account}:log-group:/aws/lambda/SatiBot-AuthPostConfirmation:*"
            ],
        ))
