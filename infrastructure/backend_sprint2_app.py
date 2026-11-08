"""
Author: Trịnh Thiên Quang
Khai báo năm Lambda backend, Version, Alias 'live' và CodeDeploy Canary/Rollback bằng AWS CDK.
Tích hợp role thực thi, bảng dữ liệu và CloudWatch Alarm tùy chọn từ hạ tầng chung.
"""

import os
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import (
    aws_codedeploy as codedeploy,
    aws_iam as iam,
    aws_lambda as lambda_,
)

FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")
ALLOWED_DEPLOYMENT_CONFIGS = [
    "CodeDeployDefault.LambdaCanary10Percent5Minutes",
    "CodeDeployDefault.LambdaCanary10Percent10Minutes",
    "CodeDeployDefault.LambdaCanary10Percent15Minutes",
    "CodeDeployDefault.LambdaLinear10PercentEvery1Minute",
    "CodeDeployDefault.LambdaAllAtOnce",
]

app = cdk.App()
stack = cdk.Stack(
    app,
    "SatiBotBackendSprint2",
    env=cdk.Environment(region=os.environ.get("PRIMARY_REGION", "ap-southeast-1")),
)
root = Path(__file__).resolve().parents[1]

connections = cdk.CfnParameter(stack, "ConnectionsTableName", type="String")
history = cdk.CfnParameter(stack, "HistoryTableName", type="String")
sessions = cdk.CfnParameter(stack, "SessionStateTableName", type="String")
pending = cdk.CfnParameter(stack, "PendingActionTableName", type="String")
secret = cdk.CfnParameter(stack, "GeminiSecretArn", type="String", default="", no_echo=True)
workflow = cdk.CfnParameter(stack, "ActionStateMachineArn", type="String", default="")
appsync_url = cdk.CfnParameter(stack, "AppSyncGraphqlUrl", type="String", default="")
codedeploy_role_arn = cdk.CfnParameter(stack, "CodeDeployRoleArn", type="String")
canary_config = cdk.CfnParameter(
    stack,
    "CanaryDeploymentConfig",
    type="String",
    default="CodeDeployDefault.LambdaCanary10Percent5Minutes",
    allowed_values=ALLOWED_DEPLOYMENT_CONFIGS,
)

codedeploy_role = iam.Role.from_role_arn(
    stack,
    "BackendCodeDeployRole",
    codedeploy_role_arn.value_as_string,
    mutable=False,
)
codedeploy_app = codedeploy.LambdaApplication(stack, "BackendCodeDeployApplication")
cdk.CfnOutput(stack, "CodeDeployApplicationName", value=codedeploy_app.application_name)

for name in FUNCTIONS:
    title = name.title()
    role_arn = cdk.CfnParameter(stack, f"{title}RoleArn", type="String")
    alarm_name = cdk.CfnParameter(stack, f"{title}AlarmName", type="String", default="")
    has_alarm = cdk.CfnCondition(
        stack,
        f"Has{title}Alarm",
        expression=cdk.Fn.condition_not(cdk.Fn.condition_equals(alarm_name.value_as_string, "")),
    )

    role = iam.Role.from_role_arn(stack, f"{name}Role", role_arn.value_as_string, mutable=False)
    environment = {"SATIBOT_MODE": "cloud"}
    if name == "connector":
        environment["AWS_CONNECTIONS_TABLE"] = connections.value_as_string
    if name == "chatbot":
        environment.update(
            SESSION_STATE_TABLE=sessions.value_as_string,
            PENDING_ACTION_TABLE=pending.value_as_string,
            GEMINI_SECRET_ARN=secret.value_as_string,
            ACTION_STATE_MACHINE_ARN=workflow.value_as_string,
        )
    if name == "dispatcher":
        environment.update(
            PENDING_ACTION_TABLE=pending.value_as_string,
            AWS_CONNECTIONS_TABLE=connections.value_as_string,
        )
    if name == "formatter":
        environment.update(
            ACTION_HISTORY_TABLE=history.value_as_string,
            GEMINI_SECRET_ARN=secret.value_as_string,
            APPSYNC_GRAPHQL_URL=appsync_url.value_as_string,
        )
    if name == "history":
        environment["ACTION_HISTORY_TABLE"] = history.value_as_string

    function = lambda_.Function(
        stack,
        f"{name}Function",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler=f"backend.functions.{name}.index.lambda_handler",
        role=role,
        code=lambda_.Code.from_asset(str(root / "dist" / "backend" / f"{name}.zip")),
        timeout=cdk.Duration.seconds(60 if name in {"chatbot", "dispatcher"} else 30),
        memory_size=256,
        environment=environment,
    )
    live_alias = lambda_.Alias(
        stack,
        f"{name}LiveAlias",
        alias_name="live",
        version=function.current_version,
    )
    deployment_group = codedeploy.LambdaDeploymentGroup(
        stack,
        f"{name}DeploymentGroup",
        application=codedeploy_app,
        alias=live_alias,
        role=codedeploy_role,
        deployment_config=codedeploy.LambdaDeploymentConfig.CANARY_10_PERCENT_5_MINUTES,
    )
    cfn_dg = deployment_group.node.default_child
    cfn_dg.deployment_config_name = canary_config.value_as_string
    cfn_dg.add_property_override(
        "AlarmConfiguration",
        cdk.Fn.condition_if(
            has_alarm.logical_id,
            {"Enabled": True, "Alarms": [{"Name": alarm_name.value_as_string}]},
            cdk.Aws.NO_VALUE,
        ),
    )
    cfn_dg.add_property_override(
        "AutoRollbackConfiguration",
        cdk.Fn.condition_if(
            has_alarm.logical_id,
            {
                "Enabled": True,
                "Events": [
                    "DEPLOYMENT_FAILURE",
                    "DEPLOYMENT_STOP_ON_REQUEST",
                    "DEPLOYMENT_STOP_ON_ALARM",
                ],
            },
            {
                "Enabled": True,
                "Events": [
                    "DEPLOYMENT_FAILURE",
                    "DEPLOYMENT_STOP_ON_REQUEST",
                ],
            },
        ),
    )

    cdk.CfnOutput(stack, f"{title}FunctionArn", value=function.function_arn)
    cdk.CfnOutput(stack, f"{title}LiveAliasArn", value=live_alias.function_arn)
    cdk.CfnOutput(stack, f"{title}DeploymentGroupName", value=deployment_group.deployment_group_name)

app.synth()
