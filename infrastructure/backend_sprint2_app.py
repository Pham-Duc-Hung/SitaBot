"""
Author: Trịnh Thiên Quang
Khai báo năm Lambda backend và cấu hình tích hợp bằng AWS CDK.
Sử dụng role và bảng dữ liệu do hạ tầng chung cung cấp.
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
sessions = cdk.CfnParameter(stack, "SessionStateTableName", type="String")
pending = cdk.CfnParameter(stack, "PendingActionTableName", type="String")
secret = cdk.CfnParameter(stack, "GeminiSecretArn", type="String", default="")
workflow = cdk.CfnParameter(stack, "ActionStateMachineArn", type="String", default="")
appsync_url = cdk.CfnParameter(stack, "AppSyncGraphqlUrl", type="String", default="")
role_arns = {
    name: cdk.CfnParameter(stack, f"{name.title()}RoleArn", type="String").value_as_string
    for name in ("chatbot", "dispatcher", "formatter", "connector", "history")
}
for name in ("chatbot", "dispatcher", "formatter", "connector", "history"):
    role = iam.Role.from_role_arn(stack, f"{name}Role", role_arns[name], mutable=False)
    environment = {"SATIBOT_MODE": "cloud"}
    if name == "connector":
        environment["AWS_CONNECTIONS_TABLE"] = connections.value_as_string
        environment["SATIBOT_CONNECTOR_ROLE_ARN"] = role_arns["connector"]
        environment["SATIBOT_DISPATCHER_ROLE_ARN"] = role_arns["dispatcher"]
    if name == "chatbot":
        environment.update(SESSION_STATE_TABLE=sessions.value_as_string, PENDING_ACTION_TABLE=pending.value_as_string,
                           GEMINI_SECRET_ARN=secret.value_as_string, ACTION_STATE_MACHINE_ARN=workflow.value_as_string)
    if name == "dispatcher":
        environment.update(PENDING_ACTION_TABLE=pending.value_as_string, AWS_CONNECTIONS_TABLE=connections.value_as_string)
    if name == "formatter":
        environment.update(ACTION_HISTORY_TABLE=history.value_as_string, GEMINI_SECRET_ARN=secret.value_as_string,
                           APPSYNC_GRAPHQL_URL=appsync_url.value_as_string)
    if name == "history":
        environment["ACTION_HISTORY_TABLE"] = history.value_as_string
    function = lambda_.Function(stack, f"{name}Function", runtime=lambda_.Runtime.PYTHON_3_12,
        handler=f"backend.functions.{name}.index.lambda_handler", role=role,
        code=lambda_.Code.from_asset(str(root / "dist" / "backend" / f"{name}.zip")),
        timeout=cdk.Duration.seconds(60 if name in {"chatbot", "dispatcher"} else 30),
        memory_size=256, environment=environment)
    cdk.CfnOutput(stack, f"{name.title()}FunctionArn", value=function.function_arn)
app.synth()
