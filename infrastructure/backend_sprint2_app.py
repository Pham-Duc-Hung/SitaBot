"""Quang's isolated backend assembly. Imports roles supplied by Minh.

Does not mutate the shared app, IAM/data/auth/DNS/frontend stacks.
"""
import os
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import aws_iam as iam, aws_lambda as lambda_

app = cdk.App()
stack = cdk.Stack(app, "SatiBotBackendSprint2", env=cdk.Environment(
    region=os.environ.get("PRIMARY_REGION", "ap-southeast-1")))
root = Path(__file__).resolve().parents[1]
connections = cdk.CfnParameter(stack, "ConnectionsTableName", type="String")
history = cdk.CfnParameter(stack, "HistoryTableName", type="String")
for name in ("chatbot", "dispatcher", "formatter", "connector", "history"):
    role_arn = cdk.CfnParameter(stack, f"{name.title()}RoleArn", type="String")
    role = iam.Role.from_role_arn(stack, f"{name}Role", role_arn.value_as_string, mutable=False)
    environment = {}
    if name == "connector":
        environment["AWS_CONNECTIONS_TABLE"] = connections.value_as_string
    if name == "history":
        environment["ACTION_HISTORY_TABLE"] = history.value_as_string
    function = lambda_.Function(stack, f"{name}Function", runtime=lambda_.Runtime.PYTHON_3_12,
        handler=f"backend.functions.{name}.index.lambda_handler", role=role,
        code=lambda_.Code.from_asset(str(root / "dist" / "backend" / f"{name}.zip")),
        timeout=cdk.Duration.seconds(60 if name in {"chatbot", "dispatcher"} else 30),
        memory_size=256, environment=environment)
    cdk.CfnOutput(stack, f"{name.title()}FunctionArn", value=function.function_arn)
app.synth()
