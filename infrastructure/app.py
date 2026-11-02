import os

import aws_cdk as cdk
from stacks.auth_stack import AuthStack
from stacks.core_api_stack import CoreApiStack
from stacks.ai_stack import AiStack
from stacks.data_stack import DataStack
from stacks.iam_stack import IamStack
from stacks.monitoring_stack import MonitoringStack

app = cdk.App()

AuthStack(app, "AuthStack")
CoreApiStack(app, "CoreApiStack")
AiStack(app, "AiStack")
data_stack = DataStack(
    app,
    "DataStack",
    env=cdk.Environment(region="ap-southeast-1"),
)
IamStack(
    app,
    "IamStack",
    data_stack=data_stack,
    llm_secret_arn=os.environ.get("LLM_SECRET_ARN"),
    env=cdk.Environment(region="ap-southeast-1"),
)
MonitoringStack(app, "MonitoringStack")

app.synth()
