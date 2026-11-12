"""Deploy the website HTTP API after Cognito and backend live aliases exist."""

import aws_cdk as cdk

from infrastructure.stacks.web_api_stack import WebApiStack


app = cdk.App()
WebApiStack(
    app, "SatiBotWebApi",
    grant_backend_invoke=app.node.try_get_context("backendOwnsInvoke") != "true",
    env=cdk.Environment(region="ap-southeast-1"),
)
app.synth()
