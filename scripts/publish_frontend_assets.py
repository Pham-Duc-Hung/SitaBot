"""Upload the built website and fallback page without CDK's Lambda custom resource."""

import argparse
import json
import mimetypes
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.shared.tool_registry import registry
SITES = {
    "primary": ("ap-southeast-1", "SatiBotWebPrimary", "Primary"),
    "error": ("us-east-1", "SatiBotWebErrorPage", "ErrorPage"),
}


def stack_outputs(session, region, stack_name):
    response = session.client("cloudformation", region_name=region).describe_stacks(
        StackName=stack_name
    )
    stack = response["Stacks"][0]
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise RuntimeError(f"Stack {stack_name} is not ready: {stack['StackStatus']}")
    return {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}


def site_files(site):
    if site == "primary":
        source = ROOT / "dist" / "frontend"
        if not (source / "index.html").is_file():
            raise FileNotFoundError("Build dist/frontend before publishing the primary site")
        return [(path, path.relative_to(source).as_posix()) for path in source.rglob("*") if path.is_file()]

    source = ROOT / "frontend" / "error.html"
    if not source.is_file():
        raise FileNotFoundError(f"Missing fallback page: {source}")
    return [(source, "index.html")]


def preview_auth_config(session, distribution_domain):
    outputs = stack_outputs(session, "ap-southeast-1", "AuthStack")
    if outputs.get("CallbackUrl") != f"https://{distribution_domain}/":
        raise RuntimeError("Cognito callback does not match the primary CloudFront distribution")
    if not all(outputs.get(key) for key in ("HostedDomain", "UserPoolClientId")):
        raise RuntimeError("Cognito stack is missing browser client outputs")
    return {
        "domain": outputs["HostedDomain"],
        "client_id": outputs["UserPoolClientId"],
        "redirect_uri": outputs["CallbackUrl"],
    }


def publish_site(session, site, appsync_graphql_url=""):
    region, stack_name, output_prefix = SITES[site]
    files = site_files(site)
    outputs = stack_outputs(session, region, stack_name)
    bucket = outputs[f"{output_prefix}BucketName"]
    primary = outputs if site == "primary" else stack_outputs(session, "ap-southeast-1", "SatiBotWebPrimary")
    distribution_id = primary["PrimaryDistributionId"]
    s3_client = session.client("s3", region_name=region)

    if site == "primary":
        distribution = session.client("cloudfront", region_name="us-east-1").get_distribution_config(
            Id=distribution_id
        )["DistributionConfig"]
        behaviors = distribution.get("CacheBehaviors", {}).get("Items", [])
        api_enabled = any(item.get("PathPattern") == "api/*" for item in behaviors)
        if not api_enabled:
            raise RuntimeError("CloudFront api/* routing is required before publishing the demo")
        if not appsync_graphql_url:
            sync = stack_outputs(session, "ap-southeast-1", "SatiBotAppSync")
            appsync_graphql_url = sync["AppSyncGraphqlUrl"]
        if appsync_graphql_url:
            endpoint = urlsplit(appsync_graphql_url)
            if (endpoint.scheme != "https" or endpoint.path != "/graphql" or
                    endpoint.query or endpoint.fragment or not re.fullmatch(
                        r"[a-z0-9-]+\.appsync-api\.ap-southeast-1\.amazonaws\.com", endpoint.hostname or "")):
                raise ValueError("AppSync URL must be the primary-region AWS GraphQL endpoint")
        config = {"api_enabled": api_enabled, "tools_loaded": len(registry())}
        if appsync_graphql_url:
            config["appsync"] = {"graphql_url": appsync_graphql_url}
        auth = preview_auth_config(session, outputs[f"{output_prefix}DistributionDomain"])
        if not auth:
            raise RuntimeError("Cognito configuration is required before publishing the demo")
        config["auth"] = auth

    for source, key in files:
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=source.read_bytes(),
            ContentType=content_type,
            CacheControl=(
                "public, max-age=31536000, immutable"
                if key.startswith("assets/")
                else "no-store"
            ),
        )

    if site == "primary":
        s3_client.put_object(
            Bucket=bucket,
            Key="site-config.json",
            Body=json.dumps(config).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-cache",
        )
        print(f"primary API routing enabled: {api_enabled}")

    invalidation = session.client("cloudfront", region_name="us-east-1").create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"satibot-{site}-{uuid.uuid4().hex}",
        },
    )
    print(
        f"{site}: uploaded {len(files)} files to {bucket}; "
        f"CloudFront invalidation {invalidation['Invalidation']['Id']}"
    )
    print(f"{site} uses shared URL: https://{primary['PrimaryDistributionDomain']}/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="AWS CLI profile")
    parser.add_argument("--expected-account", required=True, help="12-digit AWS account ID")
    parser.add_argument("--site", choices=(*SITES, "both"), default="both")
    parser.add_argument("--appsync-graphql-url", default="", help="AppSync GraphQL HTTPS endpoint, after the API is deployed")
    args = parser.parse_args()

    if len(args.expected_account) != 12 or not args.expected_account.isascii() or not args.expected_account.isdigit():
        parser.error("--expected-account must be a 12-digit AWS account ID")
    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != args.expected_account:
        raise RuntimeError(
            f"AWS account mismatch: expected {args.expected_account}, got {actual_account}. "
            "No files were published."
        )

    for site in ("error", "primary") if args.site == "both" else (args.site,):
        publish_site(session, site, args.appsync_graphql_url)


if __name__ == "__main__":
    main()
