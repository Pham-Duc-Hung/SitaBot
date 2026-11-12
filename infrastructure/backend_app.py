"""
Author: Trịnh Thiên Quang
Khai báo năm Lambda backend, Version gắn cấu hình, Alias 'live', CloudWatch Alarm Metric Math
và CodeDeploy Canary/Rollback bằng AWS CDK.
"""

import hashlib
import os
from pathlib import Path
import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_codedeploy as codedeploy,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_stepfunctions as sfn,
)

FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")
HTTP_FUNCTIONS = frozenset({"chatbot", "connector", "history"})
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
    "SatiBotBackend",
    env=cdk.Environment(region=os.environ.get("PRIMARY_REGION", "ap-southeast-1")),
)
root = Path(__file__).resolve().parents[1]

connections = cdk.CfnParameter(stack, "ConnectionsTableName", type="String")
history = cdk.CfnParameter(stack, "HistoryTableName", type="String")
sessions = cdk.CfnParameter(stack, "SessionStateTableName", type="String")
pending = cdk.CfnParameter(stack, "PendingActionTableName", type="String")
secret = cdk.CfnParameter(
    stack,
    "GeminiSecretArn",
    type="String",
    default="",
    allowed_pattern=r"^$|^arn:aws[a-zA-Z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$",
    constraint_description="Provide a Secrets Manager ARN, never a plaintext API key.",
)
workflow_role = iam.Role(stack, "ActionWorkflowRole", assumed_by=iam.ServicePrincipal("states.amazonaws.com"))
workflow = sfn.CfnStateMachine(
    stack, "ActionWorkflow", state_machine_type="STANDARD",
    role_arn=workflow_role.role_arn, definition={},
)
appsync_url = cdk.CfnParameter(stack, "AppSyncGraphqlUrl", type="String", default="")
appsync_arn = cdk.CfnParameter(stack, "AppSyncApiArn", type="String", default="")
api_gateway_source_arn = cdk.CfnParameter(
    stack, "ApiGatewaySourceArn", type="String", default=""
)
config_version = cdk.CfnParameter(
    stack, "ConfigurationVersion", type="String", default="v1"
)
grant_integration = cdk.CfnParameter(
    stack,
    "GrantIntegrationPermissions",
    type="String",
    default="false",
    allowed_values=["true", "false"],
)
role_names = {
    name: cdk.CfnParameter(
        stack,
        f"{name.title()}RoleName",
        type="String",
        default="",
        allowed_pattern=r"^$|[\w+=,.@-]{1,64}$",
    )
    for name in ("chatbot", "formatter")
}
role_arns = {
    name: cdk.CfnParameter(stack, f"{name.title()}RoleArn", type="String").value_as_string
    for name in FUNCTIONS
}
cdk.CfnRule(
    stack,
    "IntegrationRoleNamesRequired",
    assertions=[
        cdk.CfnRuleAssertion(
            assert_=cdk.Fn.condition_not(cdk.Fn.condition_equals(role_names["chatbot"].value_as_string, "")),
            assert_description="Provide ChatbotRoleName when granting Step Functions start permission.",
        ),
        cdk.CfnRuleAssertion(
            assert_=cdk.Fn.condition_or(
                cdk.Fn.condition_equals(grant_integration.value_as_string, "false"),
                cdk.Fn.condition_equals(appsync_arn.value_as_string, ""),
                cdk.Fn.condition_not(
                    cdk.Fn.condition_equals(role_names["formatter"].value_as_string, "")
                ),
            ),
            assert_description="Provide FormatterRoleName when granting AppSync publish permission.",
        ),
        cdk.CfnRuleAssertion(
            assert_=cdk.Fn.condition_or(
                cdk.Fn.condition_equals(secret.value_as_string, ""),
                cdk.Fn.condition_and(
                    cdk.Fn.condition_not(
                        cdk.Fn.condition_equals(role_names["chatbot"].value_as_string, "")
                    ),
                    cdk.Fn.condition_not(
                        cdk.Fn.condition_equals(role_names["formatter"].value_as_string, "")
                    ),
                ),
            ),
            assert_description="Provide ChatbotRoleName and FormatterRoleName when granting Gemini secret read permission.",
        ),
    ],
)

codedeploy_role_arn = cdk.CfnParameter(stack, "CodeDeployRoleArn", type="String")
canary_config = cdk.CfnParameter(
    stack,
    "CanaryDeploymentConfig",
    type="String",
    default="CodeDeployDefault.LambdaCanary10Percent5Minutes",
    allowed_values=ALLOWED_DEPLOYMENT_CONFIGS,
)

has_api_gw_source = cdk.CfnCondition(
    stack,
    "HasApiGatewaySourceArn",
    expression=cdk.Fn.condition_not(
        cdk.Fn.condition_equals(api_gateway_source_arn.value_as_string, "")
    ),
)
should_grant_appsync = cdk.CfnCondition(
    stack,
    "ShouldGrantFormatterAppSync",
    expression=cdk.Fn.condition_and(
        cdk.Fn.condition_equals(grant_integration.value_as_string, "true"),
        cdk.Fn.condition_not(cdk.Fn.condition_equals(appsync_arn.value_as_string, "")),
    ),
)
should_grant_secret = cdk.CfnCondition(
    stack,
    "ShouldGrantGeminiSecretRead",
    expression=cdk.Fn.condition_and(
        cdk.Fn.condition_equals(grant_integration.value_as_string, "true"),
        cdk.Fn.condition_not(cdk.Fn.condition_equals(secret.value_as_string, "")),
    ),
)

codedeploy_role = iam.Role.from_role_arn(
    stack,
    "BackendCodeDeployRole",
    codedeploy_role_arn.value_as_string,
    mutable=False,
)
codedeploy_app = codedeploy.LambdaApplication(stack, "BackendCodeDeployApplication")
cdk.CfnOutput(stack, "CodeDeployApplicationName", value=codedeploy_app.application_name)

frontend_origin = cdk.CfnParameter(
    stack, "FrontendOrigin", type="String", default="",
    allowed_pattern=r"^$|https?://[A-Za-z0-9.-]+(:[0-9]+)?",
)
live_aliases = {}
for name in FUNCTIONS:
    title = name.title()
    alarm_name = cdk.CfnParameter(stack, f"{title}AlarmName", type="String", default="")
    has_external_alarm = cdk.CfnCondition(
        stack,
        f"Has{title}Alarm",
        expression=cdk.Fn.condition_not(
            cdk.Fn.condition_equals(alarm_name.value_as_string, "")
        ),
    )

    role = iam.Role.from_role_arn(
        stack, f"{name}Role", role_arns[name], mutable=False
    )
    # A changed configuration marker must alter Lambda configuration as well as
    # the Version description; Lambda rejects a duplicate version otherwise.
    environment = {
        "SATIBOT_MODE": "cloud",
        "SATIBOT_CONFIGURATION_VERSION": config_version.value_as_string,
        "SATIBOT_CONFIG_VERSION": config_version.value_as_string,
    }
    if name in HTTP_FUNCTIONS:
        environment["FRONTEND_ORIGIN"] = frontend_origin.value_as_string
    if name == "connector":
        environment["AWS_CONNECTIONS_TABLE"] = connections.value_as_string
        environment["SATIBOT_CONNECTOR_ROLE_ARN"] = role_arns["connector"]
        environment["SATIBOT_DISPATCHER_ROLE_ARN"] = role_arns["dispatcher"]
    if name == "chatbot":
        environment.update(
            AWS_CONNECTIONS_TABLE=connections.value_as_string,
            SESSION_STATE_TABLE=sessions.value_as_string,
            PENDING_ACTION_TABLE=pending.value_as_string,
            GEMINI_SECRET_ARN=secret.value_as_string,
            ACTION_STATE_MACHINE_ARN=workflow.attr_arn,
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

    zip_path = root / "dist" / "backend" / f"{name}.zip"
    asset_digest = (
        hashlib.sha256(zip_path.read_bytes()).hexdigest()[:16]
        if zip_path.exists()
        else "unbuilt"
    )

    function = lambda_.Function(
        stack,
        f"{name}Function",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler=f"backend.functions.{name}.index.lambda_handler",
        role=role,
        code=lambda_.Code.from_asset(str(zip_path)),
        timeout=cdk.Duration.seconds(60 if name in {"chatbot", "dispatcher"} else 30),
        memory_size=256,
        environment=environment,
        current_version_options=lambda_.VersionOptions(
            removal_policy=cdk.RemovalPolicy.RETAIN,
            description=cdk.Fn.sub(
                f"satibot-{name}-cfg:${{ConfigurationVersion}}-code:{asset_digest}"
            ),
        ),
    )
    live_alias = lambda_.Alias(
        stack,
        f"{name}LiveAlias",
        alias_name="live",
        version=function.current_version,
    )
    live_aliases[name] = live_alias

    if name in HTTP_FUNCTIONS:
        api_perm = lambda_.CfnPermission(
            stack,
            f"{name}LiveApiGatewayPermission",
            action="lambda:InvokeFunction",
            function_name=live_alias.function_arn,
            principal="apigateway.amazonaws.com",
            source_arn=api_gateway_source_arn.value_as_string,
        )
        api_perm.cfn_options.condition = has_api_gw_source

    # Built-in Canary health alarm watching both Lambda invocation Errors on ':live'
    # and structured SatiBot/Backend PlatformFault EMF metrics.
    canary_alarm = cloudwatch.CfnAlarm(
        stack,
        f"{name}CanaryHealthAlarm",
        alarm_description=f"Canary rollback alarm for {name} (:live Errors + PlatformFaults)",
        comparison_operator="GreaterThanOrEqualToThreshold",
        # Allow delayed metric ingestion while reacting to one breaching minute.
        evaluation_periods=3,
        datapoints_to_alarm=1,
        threshold=1,
        treat_missing_data="notBreaching",
        metrics=[
            cloudwatch.CfnAlarm.MetricDataQueryProperty(
                id="m1",
                return_data=False,
                metric_stat=cloudwatch.CfnAlarm.MetricStatProperty(
                    metric=cloudwatch.CfnAlarm.MetricProperty(
                        namespace="AWS/Lambda",
                        metric_name="Errors",
                        dimensions=[
                            cloudwatch.CfnAlarm.DimensionProperty(
                                name="FunctionName", value=function.function_name
                            ),
                            cloudwatch.CfnAlarm.DimensionProperty(
                                name="Resource",
                                value=cdk.Fn.sub(
                                    "${Fn}:live", {"Fn": function.function_name}
                                ),
                            ),
                        ],
                    ),
                    period=60,
                    stat="Sum",
                ),
            ),
            cloudwatch.CfnAlarm.MetricDataQueryProperty(
                id="m2",
                return_data=False,
                metric_stat=cloudwatch.CfnAlarm.MetricStatProperty(
                    metric=cloudwatch.CfnAlarm.MetricProperty(
                        namespace="SatiBot/Backend",
                        metric_name="PlatformFault",
                        dimensions=[
                            cloudwatch.CfnAlarm.DimensionProperty(
                                name="FunctionName", value=function.function_name
                            ),
                        ],
                    ),
                    period=60,
                    stat="Sum",
                ),
            ),
            cloudwatch.CfnAlarm.MetricDataQueryProperty(
                id="e1",
                expression="FILL(m1, 0) + FILL(m2, 0)",
                label=f"{name} Canary Health Faults",
                return_data=True,
            ),
        ],
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
            has_external_alarm.logical_id,
            {
                "Enabled": True,
                "Alarms": [
                    {"Name": canary_alarm.ref},
                    {"Name": alarm_name.value_as_string},
                ],
            },
            {
                "Enabled": True,
                "Alarms": [
                    {"Name": canary_alarm.ref},
                ],
            },
        ),
    )
    cfn_dg.add_property_override(
        "AutoRollbackConfiguration",
        {
            "Enabled": True,
            "Events": [
                "DEPLOYMENT_FAILURE",
                "DEPLOYMENT_STOP_ON_REQUEST",
                "DEPLOYMENT_STOP_ON_ALARM",
            ],
        },
    )

    cdk.CfnOutput(
        stack,
        f"{title}InvokeArn",
        value=live_alias.function_arn,
        description=f"Caller invoke ARN for {name} (:live alias protected by CodeDeploy Canary)",
    )
    cdk.CfnOutput(stack, f"{title}LiveAliasArn", value=live_alias.function_arn)
    cdk.CfnOutput(
        stack,
        f"{title}FunctionArn",
        value=function.function_arn,
        description=f"Unqualified function ARN for {name}; callers must bind {title}LiveAliasArn instead",
    )
    cdk.CfnOutput(stack, f"{title}CanaryAlarmName", value=canary_alarm.ref)
    cdk.CfnOutput(
        stack,
        f"{title}DeploymentGroupName",
        value=deployment_group.deployment_group_name,
    )

# The workflow references only Dispatcher/Formatter aliases. Chatbot may depend
# on the workflow ARN without introducing a CloudFormation dependency cycle.
workflow_role.add_to_policy(iam.PolicyStatement(
    actions=["lambda:InvokeFunction"],
    resources=[live_aliases[name].function_arn for name in ("dispatcher", "formatter")],
))
workflow.node.add_dependency(workflow_role.node.find_child("DefaultPolicy"))
invoke_resource = f"arn:{cdk.Aws.PARTITION}:states:::lambda:invoke"
formatter_task = {
    "Type": "Task", "Resource": invoke_resource,
    "Parameters": {"FunctionName": live_aliases["formatter"].function_arn, "Payload.$": "$"},
    "ResultPath": "$.formatted",
    "Retry": [{"ErrorEquals": ["States.TaskFailed", "States.Timeout"], "MaxAttempts": 2}],
    "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.workflow_error", "Next": "InvocationFailed"}],
}
workflow.definition = {
    "StartAt": "Dispatcher", "TimeoutSeconds": 300,
    "States": {
        "Dispatcher": {
            "Type": "Task", "Resource": invoke_resource,
            "Parameters": {"FunctionName": live_aliases["dispatcher"].function_arn,
                           "Payload": {"user_id.$": "$.user_id", "action_id.$": "$.action_id"}},
            "OutputPath": "$.Payload", "Next": "HasResultEnvelope",
            "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.workflow_error", "Next": "InvocationFailed"}],
        },
        "HasResultEnvelope": {
            "Type": "Choice", "Choices": [{"And": [
                {"Variable": "$.result", "IsPresent": True},
                *[{"Variable": "$." + key, "IsString": True} for key in
                  ("user_id", "session_id", "action_id", "created_at", "service", "tool_name")],
            ], "Next": "Formatter"}, {"And": [
                {"Variable": "$.error_code", "IsString": True},
                {"Variable": "$.message", "IsString": True},
            ], "Next": "DispatcherError"}], "Default": "DispatcherRejected",
        },
        "Formatter": {**formatter_task, "Next": "CheckPublication"},
        "CheckPublication": {"Type": "Choice", "Choices": [{"And": [
            {"Variable": "$.formatted.Payload.publication_status", "IsPresent": True},
            {"Variable": "$.formatted.Payload.publication_status", "StringEquals": "FAILED"}], "Next": "RepublishFormatter"}],
            "Default": "CheckResult"},
        # Keep the original Dispatcher envelope. Republish never calls Dispatcher.
        "RepublishFormatter": {**formatter_task, "Next": "CheckResult"},
        "CheckResult": {"Type": "Choice", "Choices": [{"And": [
            {"Variable": "$.formatted.Payload.publication_status", "IsPresent": True},
            {"Variable": "$.formatted.Payload.publication_status", "StringEquals": "PUBLISHED"},
            {"Variable": "$.formatted.Payload.status", "IsPresent": True},
            {"Variable": "$.result.status", "IsPresent": True},
            {"Not": {"Variable": "$.formatted.Payload.status", "StringEquals": "ERROR"}},
            {"Not": {"Variable": "$.result.status", "StringEquals": "ERROR"}},
        ], "Next": "Completed"}], "Default": "ActionFailed"},
        "Completed": {"Type": "Pass", "OutputPath": "$.formatted.Payload", "End": True},
        "DispatcherRejected": {"Type": "Fail", "Error": "DispatcherRejected", "Cause": "Dispatcher returned no valid action envelope."},
        "DispatcherError": {"Type": "Fail", "ErrorPath": "$.error_code", "CausePath": "$.message"},
        "ActionFailed": {"Type": "Fail", "Error": "ActionFailed", "Cause": "The action, formatting or AppSync publication failed. Consult action history."},
        "InvocationFailed": {"Type": "Fail", "ErrorPath": "$.workflow_error.Error", "CausePath": "$.workflow_error.Cause"},
    },
}
cdk.CfnOutput(stack, "ActionStateMachineArn", value=workflow.attr_arn)

chatbot_sfn_policy = iam.CfnPolicy(
    stack,
    "ChatbotWorkflowInvokeSupplementaryPolicy",
    policy_name="SatiBot-Chatbot-StartWorkflow",
    roles=[role_names["chatbot"].value_as_string],
    policy_document={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["states:StartExecution"],
                "Resource": workflow.attr_arn,
            }
        ],
    },
)

formatter_appsync_policy = iam.CfnPolicy(
    stack,
    "FormatterAppSyncPublishSupplementaryPolicy",
    policy_name="SatiBot-Formatter-AppSyncPublish",
    roles=[role_names["formatter"].value_as_string],
    policy_document={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["appsync:GraphQL"],
                "Resource": cdk.Fn.sub(
                    "${ApiArn}/types/Mutation/fields/publishActionResult",
                    {"ApiArn": appsync_arn.value_as_string},
                ),
            }
        ],
    },
)
formatter_appsync_policy.cfn_options.condition = should_grant_appsync

gemini_secret_policy = iam.CfnPolicy(
    stack,
    "GeminiSecretReadSupplementaryPolicy",
    policy_name="SatiBot-Backend-GeminiSecretRead",
    roles=[
        role_names["chatbot"].value_as_string,
        role_names["formatter"].value_as_string,
    ],
    policy_document={
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": secret.value_as_string,
            }
        ],
    },
)
gemini_secret_policy.cfn_options.condition = should_grant_secret

app.synth()
