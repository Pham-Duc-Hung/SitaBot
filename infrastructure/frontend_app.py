"""Isolated CDK app for the Sprint 2 website, WAF, and fallback page."""

from pathlib import Path

import aws_cdk as cdk

from infrastructure.stacks.frontend_stack import ErrorPageStack, PrimaryWebStack, WebWafStack


root = Path(__file__).resolve().parents[1]
app = cdk.App()
primary_domain = app.node.try_get_context("primaryDomain")
error_domain = app.node.try_get_context("errorDomain")
if primary_domain and primary_domain == error_domain:
    raise ValueError("Primary and error CloudFront distributions need different hostnames")

WebWafStack(
    app,
    "SatiBotWebWaf",
    env=cdk.Environment(region="us-east-1"),
)
PrimaryWebStack(
    app,
    "SatiBotWebPrimary",
    site_dir=root / "dist" / "frontend",
    api_origin_domain=app.node.try_get_context("apiOriginDomain"),
    domain_name=primary_domain,
    certificate_arn=app.node.try_get_context("primaryCertificateArn"),
    env=cdk.Environment(region="ap-southeast-1"),
)
ErrorPageStack(
    app,
    "SatiBotWebErrorPage",
    error_html=root / "frontend" / "error.html",
    domain_name=error_domain,
    certificate_arn=app.node.try_get_context("errorCertificateArn"),
    env=cdk.Environment(region="us-east-1"),
)

app.synth()
