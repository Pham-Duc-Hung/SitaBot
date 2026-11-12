"""Deploy sign-in for the independent website preview."""

import aws_cdk as cdk

from infrastructure.stacks.web_auth_stack import WebAuthStack


app = cdk.App()
origin = app.node.try_get_context("webOrigin")
prefix = app.node.try_get_context("authDomainPrefix")
if not origin or not prefix:
    raise ValueError("Supply -c webOrigin=https://... -c authDomainPrefix=...")
WebAuthStack(app, "SatiBotWebAuth", web_origin=origin, domain_prefix=prefix,
             env=cdk.Environment(region="ap-southeast-1"))
app.synth()
