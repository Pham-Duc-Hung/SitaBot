"""
CDK App Entrypoint for SatiBot Full-Stack Infrastructure.
Wired by: Group 10 (Cao Anh Minh, Nguyen Duc Huy, Trinh Thien Quang, Pham Duc Hung)
"""

import os
import aws_cdk as cdk
from stacks.ai_stack import AiStack
from stacks.auth_stack import AuthStack
from stacks.core_api_stack import CoreApiStack
from stacks.data_stack import DataStack
from stacks.iam_stack import IamStack
from stacks.monitoring_stack import MonitoringStack

app = cdk.App()
region = os.environ.get("PRIMARY_REGION", "ap-southeast-1")
target_env = cdk.Environment(region=region)

# 1. Hạ tầng cơ sở dữ liệu DynamoDB (DataStack)
data_stack = DataStack(
    app,
    "DataStack",
    env=target_env,
)

# 2. Hạ tầng phân quyền IAM Least Privilege (IamStack)
iam_stack = IamStack(
    app,
    "IamStack",
    data_stack=data_stack,
    llm_secret_arn=os.environ.get("LLM_SECRET_ARN") or app.node.try_get_context("llmSecretArn"),
    env=target_env,
)

# 3. Hạ tầng xác thực danh tính Cognito (AuthStack - Nguyễn Đức Huy)
auth_stack = AuthStack(
    app,
    "AuthStack",
    data_stack=data_stack,
    iam_stack=iam_stack,
    env=target_env,
)

# 4. Hạ tầng API Gateway & Định tuyến nghiệp vụ (CoreApiStack - Nguyễn Đức Huy)
core_api_stack = CoreApiStack(
    app,
    "CoreApiStack",
    auth_stack=auth_stack,
    env=target_env,
)

# 6. Các Stack bổ trợ
AiStack(app, "AiStack", env=target_env)
MonitoringStack(app, "MonitoringStack", env=target_env)


app.synth()
