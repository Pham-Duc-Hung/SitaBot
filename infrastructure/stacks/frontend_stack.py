"""Website assets, edge protection, and an independent maintenance page."""

from pathlib import Path
import re

from aws_cdk import CfnOutput, CfnParameter, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct


def _private_site_bucket(scope: Construct, name: str) -> s3.Bucket:
    return s3.Bucket(
        scope,
        name,
        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        encryption=s3.BucketEncryption.S3_MANAGED,
        enforce_ssl=True,
        object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        removal_policy=RemovalPolicy.RETAIN,
    )


def _certificate(scope: Construct, domain: str | None, arn: str | None):
    if bool(domain) != bool(arn):
        raise ValueError("A CloudFront domain and its us-east-1 ACM certificate ARN must be configured together")
    if arn and not re.fullmatch(r"arn:aws[a-zA-Z-]*:acm:us-east-1:\d{12}:certificate/[0-9a-fA-F-]+", arn):
        raise ValueError("CloudFront requires an ACM certificate ARN from us-east-1")
    return acm.Certificate.from_certificate_arn(scope, "SiteCertificate", arn) if arn else None


class WebWafStack(Stack):
    """CloudFront-scope WAF; this stack must be deployed in us-east-1."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "us-east-1":
            raise ValueError("CloudFront WAF must be deployed in us-east-1")

        def visibility(metric: str) -> dict:
            return {
                "cloudWatchMetricsEnabled": True,
                "metricName": metric,
                "sampledRequestsEnabled": True,
            }

        acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            name="SatiBotWebAcl",
            scope="CLOUDFRONT",
            default_action={"allow": {}},
            visibility_config=visibility("SatiBotWebAcl"),
            rules=[
                {
                    "name": "AWSCommonThreats",
                    "priority": 0,
                    "overrideAction": {"none": {}},
                    "statement": {
                        "managedRuleGroupStatement": {
                            "vendorName": "AWS",
                            "name": "AWSManagedRulesCommonRuleSet",
                        }
                    },
                    "visibilityConfig": visibility("SatiBotCommonThreats"),
                },
                {
                    "name": "AWSSqliProtection",
                    "priority": 1,
                    "overrideAction": {"none": {}},
                    "statement": {
                        "managedRuleGroupStatement": {
                            "vendorName": "AWS",
                            "name": "AWSManagedRulesSQLiRuleSet",
                        }
                    },
                    "visibilityConfig": visibility("SatiBotSqliProtection"),
                },
                {
                    "name": "PerIpRateLimit",
                    "priority": 2,
                    "action": {"block": {}},
                    "statement": {
                        "rateBasedStatement": {
                            "aggregateKeyType": "IP",
                            "limit": 100,
                            "evaluationWindowSec": 300,
                        }
                    },
                    "visibilityConfig": visibility("SatiBotRateLimit"),
                },
            ],
        )
        CfnOutput(self, "WebAclArn", value=acl.attr_arn)


class PrimaryWebStack(Stack):
    """Private S3 website with a WAF-protected CloudFront distribution."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        site_dir: Path,
        api_origin_domain: str | None = None,
        domain_name: str | None = None,
        certificate_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "ap-southeast-1":
            raise ValueError("The primary website bucket must be in ap-southeast-1")
        if not (site_dir / "index.html").is_file():
            raise FileNotFoundError(f"Build the frontend first: {site_dir / 'index.html'}")

        web_acl_arn = CfnParameter(
            self,
            "WebAclArn",
            type="String",
            description="ARN exported by SatiBotWebWaf in us-east-1",
            allowed_pattern=r"arn:aws:wafv2:us-east-1:\d{12}:global/webacl/.+",
        )
        if api_origin_domain and not re.fullmatch(r"[A-Za-z0-9.-]+", api_origin_domain):
            raise ValueError("API origin must be a hostname without scheme or path")
        bucket = _private_site_bucket(self, "PrimarySiteBucket")
        certificate = _certificate(self, domain_name, certificate_arn)
        api_behaviors = {}
        if api_origin_domain:
            api_behaviors["api/*"] = cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    api_origin_domain,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            )
        distribution = cloudfront.Distribution(
            self,
            "PrimaryDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            additional_behaviors=api_behaviors,
            web_acl_id=web_acl_arn.value_as_string,
            certificate=certificate,
            domain_names=[domain_name] if domain_name else None,
        )
        CfnOutput(self, "PrimaryBucketName", value=bucket.bucket_name)
        CfnOutput(self, "PrimaryDistributionId", value=distribution.distribution_id)
        CfnOutput(self, "PrimaryDistributionDomain", value=distribution.distribution_domain_name)


class ErrorPageStack(Stack):
    """Standalone static page in us-east-1; no backend or application data."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        error_html: Path,
        domain_name: str | None = None,
        certificate_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != "us-east-1":
            raise ValueError("The independent error page must be in us-east-1")
        if not error_html.is_file():
            raise FileNotFoundError(f"Missing fallback page: {error_html}")
        bucket = _private_site_bucket(self, "ErrorPageBucket")
        certificate = _certificate(self, domain_name, certificate_arn)
        distribution = cloudfront.Distribution(
            self,
            "ErrorPageDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            certificate=certificate,
            domain_names=[domain_name] if domain_name else None,
        )
        CfnOutput(self, "ErrorPageBucketName", value=bucket.bucket_name)
        CfnOutput(self, "ErrorPageDistributionId", value=distribution.distribution_id)
        CfnOutput(self, "ErrorPageDistributionDomain", value=distribution.distribution_domain_name)
