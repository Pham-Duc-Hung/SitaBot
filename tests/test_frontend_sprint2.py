"""Check the deployable website and fallback infrastructure contract."""

import json

from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infrastructure.stacks.frontend_stack import ErrorPageStack, PrimaryWebStack, WebWafStack


def test_cloudfront_waf_is_in_virginia_with_managed_and_rate_rules():
    app = App()
    stack = WebWafStack(app, "WebWafTest", env=Environment(region="us-east-1"))
    acl = next(iter(Template.from_stack(stack).find_resources("AWS::WAFv2::WebACL").values()))["Properties"]

    assert acl["Scope"] == "CLOUDFRONT"
    managed = [rule for rule in acl["Rules"] if "ManagedRuleGroupStatement" in rule["Statement"]]
    assert {rule["Statement"]["ManagedRuleGroupStatement"]["Name"] for rule in managed} == {
        "AWSManagedRulesCommonRuleSet",
        "AWSManagedRulesSQLiRuleSet",
    }
    rate = next(rule for rule in acl["Rules"] if "RateBasedStatement" in rule["Statement"])
    assert rate["Action"] == {"Block": {}}
    assert rate["Statement"]["RateBasedStatement"]["AggregateKeyType"] == "IP"
    assert rate["Statement"]["RateBasedStatement"]["Limit"] == 100
    assert rate["Statement"]["RateBasedStatement"]["EvaluationWindowSec"] == 300


def test_primary_site_is_private_and_forwards_api_without_caching(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>SatiBot</title>", encoding="utf-8")
    app = App()
    stack = PrimaryWebStack(
        app, "PrimaryWebTest", site_dir=tmp_path, api_origin_domain="api.example.com",
        env=Environment(region="ap-southeast-1"),
    )
    template = Template.from_stack(stack)
    bucket = next(iter(template.find_resources("AWS::S3::Bucket").values()))["Properties"]
    assert bucket["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert policies
    policy_json = json.dumps(policies)
    assert "cloudfront.amazonaws.com" in policy_json
    assert "AWS:SourceArn" in policy_json
    distribution = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert distribution["WebACLId"] == {"Ref": "WebAclArn"}
    api = next(behavior for behavior in distribution["CacheBehaviors"] if behavior["PathPattern"] == "api/*")
    assert api["AllowedMethods"] == ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    assert api["CachePolicyId"]
    assert api["OriginRequestPolicyId"]
    assert any(origin.get("OriginAccessControlId") for origin in distribution["Origins"])
    assert not template.find_resources("AWS::Lambda::Function")
    assert not template.find_resources("AWS::Lambda::LayerVersion")


def test_preview_site_can_launch_before_api_gateway_exists(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Preview</title>", encoding="utf-8")
    app = App()
    stack = PrimaryWebStack(app, "PreviewWebTest", site_dir=tmp_path, env=Environment(region="ap-southeast-1"))
    distribution = next(iter(Template.from_stack(stack).find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]
    assert "CacheBehaviors" not in distribution
    assert distribution["WebACLId"] == {"Ref": "WebAclArn"}


def test_error_page_is_independent_of_backend(tmp_path):
    page = tmp_path / "error.html"
    page.write_text("<!doctype html><h1>Service unavailable</h1>", encoding="utf-8")
    app = App()
    stack = ErrorPageStack(app, "ErrorPageTest", error_html=page, env=Environment(region="us-east-1"))
    template = Template.from_stack(stack)
    distribution = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))["Properties"]["DistributionConfig"]

    assert distribution["DefaultRootObject"] == "index.html"
    assert any(origin.get("OriginAccessControlId") for origin in distribution["Origins"])
    assert "CacheBehaviors" not in distribution
    assert not template.find_resources("AWS::DynamoDB::Table")
    assert not template.find_resources("AWS::ApiGatewayV2::Api")
    assert not template.find_resources("AWS::Lambda::Function")
    assert not template.find_resources("AWS::Lambda::LayerVersion")
