"""SatiBot system tables for the single-AWS-connection-per-user model."""

from aws_cdk import CfnOutput, RemovalPolicy, Stack, aws_dynamodb as dynamodb
from constructs import Construct


class DataStack(Stack):
    """Own the five application tables and expose constructs to the IAM stack."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.users = self._table(
            "Users", "user_id", point_in_time_recovery=True, deletion_protection=True
        )
        self.aws_connections = self._table(
            "AwsConnections", "user_id", point_in_time_recovery=True,
            deletion_protection=True,
        )
        self.session_state = self._table(
            "SessionState", "user_id", sort_key="session_id",
            ttl_attribute="expires_at",
        )
        self.pending_action = self._table(
            "PendingAction", "action_id", ttl_attribute="expires_at",
        )
        self.action_history = self._table(
            "ActionHistory", "user_id", sort_key="created_at",
            point_in_time_recovery=True, deletion_protection=True,
        )

        for name, table in self.tables.items():
            CfnOutput(self, f"{name}TableName", value=table.table_name)
            CfnOutput(self, f"{name}TableArn", value=table.table_arn)

    @property
    def tables(self):
        return {
            "Users": self.users,
            "AwsConnections": self.aws_connections,
            "SessionState": self.session_state,
            "PendingAction": self.pending_action,
            "ActionHistory": self.action_history,
        }

    def _table(
        self,
        name: str,
        partition_key: str,
        *,
        sort_key: str | None = None,
        ttl_attribute: str | None = None,
        point_in_time_recovery: bool = False,
        deletion_protection: bool = False,
    ) -> dynamodb.Table:
        props = {
            "table_name": f"SatiBot-{name}",
            "partition_key": dynamodb.Attribute(
                name=partition_key, type=dynamodb.AttributeType.STRING
            ),
            "billing_mode": dynamodb.BillingMode.PAY_PER_REQUEST,
            "point_in_time_recovery": point_in_time_recovery,
            "deletion_protection": deletion_protection,
            "removal_policy": RemovalPolicy.RETAIN,
        }
        if sort_key:
            props["sort_key"] = dynamodb.Attribute(
                name=sort_key, type=dynamodb.AttributeType.STRING
            )
        if ttl_attribute:
            props["time_to_live_attribute"] = ttl_attribute
        return dynamodb.Table(self, f"{name}Table", **props)
