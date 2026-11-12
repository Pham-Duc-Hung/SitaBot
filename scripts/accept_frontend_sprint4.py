"""Record live WAF and independent fallback-page acceptance without client IPs."""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3


def stack_outputs(session, stack_name, region):
    stack = session.client("cloudformation", region_name=region).describe_stacks(
        StackName=stack_name
    )["Stacks"][0]
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise RuntimeError(f"{stack_name} is not ready: {stack['StackStatus']}")
    return {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}


def fetch(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 SatiBotSprint4Acceptance/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            return {
                "status": response.status,
                "x_cache": response.headers.get("X-Cache"),
                "server": response.headers.get("Server"),
            }, response.read(65536)
    except HTTPError as error:
        return {
            "status": error.code,
            "x_cache": error.headers.get("X-Cache"),
            "server": error.headers.get("Server"),
        }, error.read(65536)


def blocked_sample(session, web_acl_arn, since):
    client = session.client("wafv2", region_name="us-east-1")
    for attempt in range(8):
        response = client.get_sampled_requests(
            WebAclArn=web_acl_arn,
            RuleMetricName="SatiBotCommonThreats",
            Scope="CLOUDFRONT",
            TimeWindow={
                "StartTime": since - timedelta(seconds=10),
                "EndTime": datetime.now(timezone.utc),
            },
            MaxItems=100,
        )
        for item in response.get("SampledRequests", []):
            if (item["Action"] == "BLOCK" and
                    "sprint4_waf_probe" in item["Request"].get("URI", "")):
                return {
                    "action": item["Action"],
                    "rule": item.get("RuleNameWithinRuleGroup"),
                    "time_utc": item["Timestamp"].astimezone(timezone.utc).isoformat(),
                }
        if attempt < 7:
            time.sleep(3)
    raise RuntimeError("WAF did not sample the blocked XSS probe")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-failover", action="store_true",
                        help="Observe fallback at the primary URL after an externally triggered origin timeout")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise RuntimeError("AWS account mismatch; acceptance test was not run")

    primary = stack_outputs(session, "SatiBotWebPrimary", "ap-southeast-1")
    fallback = stack_outputs(session, "SatiBotWebErrorPage", "us-east-1")
    waf = stack_outputs(session, "SatiBotWebWaf", "us-east-1")
    primary_domain = primary["PrimaryDistributionDomain"]
    distribution = session.client("cloudfront", region_name="us-east-1").get_distribution_config(
        Id=primary["PrimaryDistributionId"]
    )["DistributionConfig"]
    if distribution.get("WebACLId") != waf["WebAclArn"]:
        raise RuntimeError("Primary CloudFront distribution is not attached to the expected WAF")

    groups = distribution.get("OriginGroups", {}).get("Items", [])
    target = distribution["DefaultCacheBehavior"]["TargetOriginId"]
    group = next((item for item in groups if item["Id"] == target), None)
    if not group or group["FailoverCriteria"]["StatusCodes"]["Items"] != [504]:
        raise RuntimeError("The default behavior must use the timeout fallback origin group")
    if args.expect_failover:
        status, body = fetch(f"https://{primary_domain}/")
        if status["status"] != 200 or "SatiBot tạm thời không khả dụng" not in body.decode("utf-8", errors="replace"):
            raise RuntimeError("The primary URL did not return the fallback page")
        evidence = {
            "scope": "Same-URL timeout failover observation after an externally triggered fault",
            "tested_at_utc": datetime.now(timezone.utc).isoformat(),
            "url": f"https://{primary_domain}/", "http": status,
            "origin_group": group, "notice_visible": True,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Fallback page observed at the primary CloudFront URL")
        return

    started = datetime.now(timezone.utc)
    allowed, _ = fetch(f"https://{primary_domain}/")
    probe = urlencode({"sprint4_waf_probe": '<script>alert("hello")</script>'})
    blocked, _ = fetch(f"https://{primary_domain}/?{probe}")
    stored = session.client("s3", region_name="us-east-1").get_object(
        Bucket=fallback["ErrorPageBucketName"], Key="index.html",
    )
    body = stored["Body"].read(65536)
    stored["Body"].close()
    if (allowed["status"], blocked["status"]) != (200, 403):
        raise RuntimeError(f"Unexpected HTTP results: {allowed}, {blocked}")
    if "SatiBot tạm thời không khả dụng" not in body.decode("utf-8", errors="replace"):
        raise RuntimeError("Fallback page is missing its unavailable notice")
    sampled = blocked_sample(session, waf["WebAclArn"], started)
    if "CrossSiteScripting_QUERYARGUMENTS" not in (sampled["rule"] or ""):
        raise RuntimeError("The expected AWS managed XSS rule did not block the probe")

    evidence = {
        "scope": "WAF and fallback origin readiness; timeout failover requires --expect-failover",
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "primary": {
            "url": f"https://{primary_domain}/",
            "region": "ap-southeast-1",
            "waf_attached": True,
            "http": allowed,
        },
        "blocked_xss_probe": {
            "target": f"https://{primary_domain}/",
            "probe": "XSS pattern in a query argument",
            "http": blocked,
            "waf_sample": sampled,
        },
        "fallback": {
            "bucket": fallback["ErrorPageBucketName"],
            "region": "us-east-1",
            "object": "index.html",
            "notice_visible": True,
            "screenshot": "sprint4-error-page.png",
        },
        "scope_note": "Fallback content was read from private S3. Same-URL timeout failover was not exercised.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Allowed {allowed['status']}; blocked {blocked['status']} by {sampled['rule']}; "
          f"fallback origin content verified; evidence {args.output}")


if __name__ == "__main__":
    main()
