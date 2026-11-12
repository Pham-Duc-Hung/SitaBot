"""
Author: Trịnh Thiên Quang
Đóng gói, tổng hợp CDK, kiểm chứng canary/rollback và triển khai SatiBot theo thứ tự phụ thuộc.
Chỉ chạy sau khi đã chọn đúng AWS profile/account và bootstrap CDK. Script không tự tạo Gemini secret.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_backend import FUNCTIONS
from scripts.write_deploy_config import build_configuration


def run(*args, env):
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def outputs(path, stack):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    values = data.get(stack)
    if not isinstance(values, dict):
        raise RuntimeError(f"Missing CDK outputs for {stack}")
    return values


def integration_outputs(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "SatiBotIntegration" in data:
        return outputs(path, "SatiBotIntegration")
    result = {}
    api_stack = "SatiBotWebApi" if "SatiBotWebApi" in data else "CoreApiStack"
    sync_stack = "SatiBotAppSync" if "SatiBotAppSync" in data else "AppSyncStack"
    for stack, keys in {
        "AuthStack": ("UserPoolId", "UserPoolClientId"),
        api_stack: ("ApiEndpointUrl", "ApiGatewaySourceArn"),
        sync_stack: ("AppSyncApiArn", "AppSyncGraphqlUrl"),
    }.items():
        values = outputs(path, stack)
        for key in keys:
            try:
                result[key] = required(values, key)
            except RuntimeError as exc:
                raise RuntimeError(f"{stack}: {exc}") from None
    if api_stack == "CoreApiStack":
        result["FrontendOrigin"] = required(data[api_stack], "FrontendOrigin")
    return result


def record_outputs(stack, values):
    path = ROOT / "dist" / "release-outputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing[stack] = values
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def required(values, key):
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Missing stack output {key}")
    return value


def validate_integration_outputs(values, account, region):
    """Reject integration outputs for another account or region before deploying."""
    prefixes = {
        "AppSyncApiArn": f"arn:aws:appsync:{region}:{account}:apis/",
        "ApiGatewaySourceArn": f"arn:aws:execute-api:{region}:{account}:",
    }
    for key, prefix in prefixes.items():
        value = required(values, key)
        if not value.startswith(prefix) or len(value) <= len(prefix):
            raise ValueError(f"{key} must belong to the selected account and region")
    endpoint = urlsplit(required(values, "AppSyncGraphqlUrl"))
    if (endpoint.scheme != "https" or not endpoint.hostname
            or not re.fullmatch(rf"[a-z0-9-]+\.appsync-api\.{re.escape(region)}\.amazonaws\.com", endpoint.hostname)
            or endpoint.path != "/graphql" or endpoint.username or endpoint.password
            or endpoint.port or endpoint.query or endpoint.fragment):
        raise ValueError("AppSyncGraphqlUrl must be the regional AWS AppSync GraphQL endpoint")


def deploy(app, stack, params, output_file, env, *, cdk_out=None, context=None):
    cmd = [
        "cdk",
        "deploy",
        "--app",
        app,
        stack,
        "--require-approval",
        "never",
        "--exclusively",
        "--outputs-file",
        str(output_file),
    ]
    if cdk_out:
        cmd.extend(["--output", str(cdk_out)])
    if context:
        for key, value in context.items():
            cmd.extend(["--context", f"{key}={value}"])
    for key, value in params.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing deployment parameter: {key}")
        cmd.extend(["--parameters", f"{key}={value}"])
    run(*cmd, env=env)
    values = outputs(output_file, stack)
    record_outputs(stack, values)
    return values


def existing_backend(client):
    """Reuse deployed aliases; never reset a functioning release to bootstrap."""
    try:
        stack = client.describe_stacks(StackName="SatiBotBackend")["Stacks"][0]
    except ClientError as exc:
        error = exc.response["Error"]
        if error["Code"] == "ValidationError" and "does not exist" in error.get("Message", ""):
            try:
                client.describe_stacks(StackName="SatiBotBackendSprint2")
            except ClientError as legacy_error:
                detail = legacy_error.response["Error"]
                if detail["Code"] == "ValidationError" and "does not exist" in detail.get("Message", ""):
                    return None
                raise
            raise RuntimeError("Legacy SatiBotBackendSprint2 exists. Review stack migration before creating SatiBotBackend.")
        raise
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
        raise RuntimeError(f"Resolve backend stack status before deployment: {stack['StackStatus']}")
    return stack


def main():
    parser = argparse.ArgumentParser(description="Package, validate and deploy the SatiBot backend release.")
    parser.add_argument("--profile", help="AWS CLI profile used by SDK and CDK")
    parser.add_argument("--gemini-secret-arn", required=True)
    parser.add_argument(
        "--expected-account-id",
        required=True,
        help="12-digit AWS account that will receive resources",
    )
    parser.add_argument("--codedeploy-role-arn",
                        help="Override the CodeDeploy role; defaults to the IamStack output")
    parser.add_argument(
        "--canary-config",
        default="CodeDeployDefault.LambdaCanary10Percent5Minutes",
        help="CodeDeploy deployment configuration for SatiBotBackend",
    )
    parser.add_argument("--region", default="ap-southeast-1")
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--bootstrap-only", action="store_true",
                       help="Deploy backend aliases before the integration owner creates their stack")
    phase.add_argument("--integration-outputs", type=Path,
                       help="CDK outputs JSON from the integration stack managed by teammates")
    phase.add_argument("--deploy-integrations", action="store_true",
                       help="Deploy Auth/API/AppSync using backend aliases and activate the release")
    parser.add_argument("--frontend-origin",
                        help="Exact frontend origin for Gateway and Lambda CORS")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required to perform AWS deployments",
    )
    args = parser.parse_args()
    if args.region != "ap-southeast-1":
        parser.error("Deployment currently targets ap-southeast-1")
    if not re.fullmatch(r"\d{12}", args.expected_account_id):
        parser.error("--expected-account-id must contain exactly 12 digits")
    if not args.execute:
        parser.error("Add --execute after reviewing the target AWS profile/account")
    integration = None
    if args.integration_outputs:
        integration = integration_outputs(args.integration_outputs)

    if (args.deploy_integrations or args.integration_outputs) and not args.frontend_origin:
        parser.error("Activation requires --frontend-origin")
    if args.frontend_origin and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:[0-9]+)?", args.frontend_origin):
        parser.error("--frontend-origin must be an exact HTTP(S) origin")

    bin_dir = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PRIMARY_REGION": args.region,
        "AWS_DEFAULT_REGION": args.region,
        "LLM_SECRET_ARN": args.gemini_secret_arn,
    }
    if args.profile:
        env["AWS_PROFILE"] = args.profile
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != args.expected_account_id:
        raise RuntimeError(
            f"AWS account mismatch: expected {args.expected_account_id}, active {actual_account}"
        )
    if args.codedeploy_role_arn and not re.fullmatch(rf"arn:aws:iam::{actual_account}:role/[\w+=,.@/-]+", args.codedeploy_role_arn):
        parser.error("CodeDeploy role ARN must belong to the selected account")
    if not re.fullmatch(
        rf"arn:aws[a-zA-Z-]*:secretsmanager:{re.escape(args.region)}:{actual_account}:secret:.+",
        args.gemini_secret_arn,
    ):
        parser.error("Gemini secret ARN must be in the selected account and region")
    if integration is not None:
        validate_integration_outputs(integration, actual_account, args.region)
        if integration.get("FrontendOrigin", args.frontend_origin) != args.frontend_origin:
            raise ValueError("FrontendOrigin must match CoreApiStack")

    foundation_app = f'"{sys.executable}" infrastructure/app.py'
    backend_app = f'"{sys.executable}" infrastructure/backend_app.py'
    web_api_app = f'"{sys.executable}" -m infrastructure.web_api_app'
    sync_app = f'"{sys.executable}" -m infrastructure.appsync_app'

    run(sys.executable, "scripts/package_backend.py", "--with-dependencies", env=env)
    run(sys.executable, "scripts/verify_backend_artifacts.py", env=env)
    run("cdk", "synth", "--app", foundation_app, "--output", "dist/foundation.out", env=env)
    run("cdk", "synth", "--app", backend_app, "--output", "dist/cdk.out", env=env)
    run(sys.executable, "scripts/verify_canary_rollback.py", env=env)
    run(sys.executable, "scripts/build_frontend_ci.py", env=env)

    backend_template = json.loads(
        (ROOT / "dist/cdk.out/SatiBotBackend.template.json").read_text(encoding="utf-8")
    )

    allowed = backend_template["Parameters"]["CanaryDeploymentConfig"]["AllowedValues"]
    if args.canary_config not in allowed:
        parser.error(f"Unsupported canary configuration: {args.canary_config}")
    previous = existing_backend(session.client("cloudformation"))
    if args.integration_outputs and previous is None:
        raise RuntimeError("Bootstrap SatiBotBackend first, then deploy the integration stack and rerun with its outputs")
    activated = previous is not None and any(
        p["ParameterKey"] == "GrantIntegrationPermissions" and p.get("ParameterValue") == "true"
        for p in previous.get("Parameters", [])
    )

    with tempfile.TemporaryDirectory(prefix="satibot-release-") as temporary:
        output_file = Path(temporary) / "outputs.json"
        run(
            "cdk",
            "deploy",
            "--app",
            foundation_app,
            "--output",
            "dist/foundation.out",
            "DataStack",
            "IamStack",
            "--require-approval",
            "never",
            "--outputs-file",
            str(output_file),
            env=env,
        )
        data = outputs(output_file, "DataStack")
        record_outputs("DataStack", data)
        roles = outputs(output_file, "IamStack")
        record_outputs("IamStack", roles)
        aliases = {
            item["OutputKey"]: item["OutputValue"]
            for item in (previous or {}).get("Outputs", [])
        }
        backend_params = {
            "ConnectionsTableName": required(data, "AwsConnectionsTableName"),
            "HistoryTableName": required(data, "ActionHistoryTableName"),
            "SessionStateTableName": required(data, "SessionStateTableName"),
            "PendingActionTableName": required(data, "PendingActionTableName"),
            "GeminiSecretArn": args.gemini_secret_arn,
            "CodeDeployRoleArn": args.codedeploy_role_arn or required(roles, "CodeDeployRoleArn"),
            "CanaryDeploymentConfig": args.canary_config,
            **{
                f"{name.title()}RoleArn": required(roles, f"{name.title()}RoleArn")
                for name in FUNCTIONS
            },
        }
        if previous is None:
            bootstrap_cfg = build_configuration(
                backend_template,
                {
                    **{f"PARAM_{k}": v for k, v in backend_params.items()},
                    "PARAM_GrantIntegrationPermissions": "false",
                },
                require_integrations=False,
            )["Parameters"]
            aliases = deploy(
                backend_app,
                "SatiBotBackend",
                {k: v for k, v in bootstrap_cfg.items() if v != ""},
                output_file,
                env,
                cdk_out="dist/cdk.out",
            )

        if args.bootstrap_only:
            record_outputs("SatiBotBackend", aliases)
            print(json.dumps({"SatiBotBackend": aliases}, indent=2))
            return
        if args.deploy_integrations:
            auth = deploy(foundation_app, "AuthStack", {
                "FrontendOrigin": args.frontend_origin,
            }, output_file, env, cdk_out="dist/foundation.out")
            deploy(web_api_app, "SatiBotWebApi", {
                **{f"{name.title()}LiveAliasArn": required(aliases, f"{name.title()}LiveAliasArn")
                   for name in ("chatbot", "connector", "history")},
                "UserPoolId": required(auth, "UserPoolId"),
                "UserPoolClientId": required(auth, "UserPoolClientId"),
            }, output_file, env, cdk_out="dist/web-api-cdk.out")
            deploy(sync_app, "SatiBotAppSync", {
                "UserPoolId": required(auth, "UserPoolId"),
            }, output_file, env, cdk_out="dist/appsync-cdk.out")
            integration = integration_outputs(ROOT / "dist" / "release-outputs.json")
            validate_integration_outputs(integration, actual_account, args.region)
        release_input = {
            **backend_params,
            # Bootstrap has no functioning workflow; do not canary traffic back to it.
            "CanaryDeploymentConfig": args.canary_config if activated else "CodeDeployDefault.LambdaAllAtOnce",
            "AppSyncGraphqlUrl": required(integration, "AppSyncGraphqlUrl"),
            "AppSyncApiArn": required(integration, "AppSyncApiArn"),
            "ApiGatewaySourceArn": required(integration, "ApiGatewaySourceArn"),
            "GrantIntegrationPermissions": "true",
            "FrontendOrigin": args.frontend_origin,
        }
        release_cfg = build_configuration(
            backend_template,
            {f"PARAM_{k}": v for k, v in release_input.items()},
            require_integrations=True,
        )["Parameters"]
        deploy(
            backend_app,
            "SatiBotBackend",
            {k: v for k, v in release_cfg.items() if v != ""},
            output_file,
            env,
            cdk_out="dist/cdk.out",
        )
        if args.deploy_integrations:
            deploy(web_api_app, "SatiBotWebApi", {
                **{f"{name.title()}LiveAliasArn": required(aliases, f"{name.title()}LiveAliasArn")
                   for name in ("chatbot", "connector", "history")},
                "UserPoolId": required(integration, "UserPoolId"),
                "UserPoolClientId": required(integration, "UserPoolClientId"),
            }, output_file, env, cdk_out="dist/web-api-cdk.out",
                context={"backendOwnsInvoke": "true"})
        print(
            json.dumps(
                {key: integration[key] for key in ("UserPoolId", "UserPoolClientId", "ApiEndpointUrl", "AppSyncGraphqlUrl") if key in integration},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
