"""Least-privilege Lambda execution roles for the Sprint 2 backend."""

from aws_cdk import Aws, CfnOutput, Stack, aws_dynamodb as dynamodb, aws_iam as iam
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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.roles: dict[str, iam.Role] = {}
        tables = data_stack.tables

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
                          "dynamodb:GetItem", "dynamodb:UpdateItem")
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
                "log-group:/aws/lambda/SatiBotBackendSprint2-*:*"
            ],
        ))
