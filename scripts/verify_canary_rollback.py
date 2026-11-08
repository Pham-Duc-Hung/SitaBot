"""
Author: Trịnh Thiên Quang
Kiểm tra template, mô phỏng offline và xác minh triển khai AWS khi được yêu cầu.
"""

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path

try:
    from scripts.deployment_checks import validate_live_evidence, verify_callers
    from scripts.deployment_template_checks import (
        FUNCTIONS,
        DEFAULT_TEMPLATE,
        DEFAULT_PIPELINE,
        verify_cdk_canary_template,
        verify_pipeline_template,
    )
except ModuleNotFoundError:
    from deployment_checks import validate_live_evidence, verify_callers
    from deployment_template_checks import (
        FUNCTIONS,
        DEFAULT_TEMPLATE,
        DEFAULT_PIPELINE,
        verify_cdk_canary_template,
        verify_pipeline_template,
    )


@dataclass
class CanaryAliasSimulator:
    """Mô phỏng máy trạng thái nội bộ của Canary10Percent5Minutes + Alarm Rollback (kiểm tra logic ngoại tuyến)."""

    function_name: str
    active_version: str = "1"
    routing_weights: dict[str, float] = field(default_factory=lambda: {"1": 1.0})
    alarm_state: str = "OK"
    events: list[dict] = field(default_factory=list)

    def deploy_canary(
        self,
        candidate_version: str,
        *,
        simulated_error_rate: float,
        error_threshold: float = 0.05,
    ) -> dict:
        previous = self.active_version
        self.routing_weights = {previous: 0.9, candidate_version: 0.1}
        self.events.append(
            {
                "phase": "CANARY_10_PERCENT",
                "weights": dict(self.routing_weights),
                "candidate_version": candidate_version,
            }
        )

        if simulated_error_rate >= error_threshold:
            self.alarm_state = "ALARM"
            self.routing_weights = {previous: 1.0}
            self.active_version = previous
            self.events.append(
                {
                    "phase": "AUTO_ROLLBACK",
                    "trigger": "DEPLOYMENT_STOP_ON_ALARM",
                    "restored_version": previous,
                    "weights": dict(self.routing_weights),
                }
            )
            self.alarm_state = "OK"
            return {
                "deployment_status": "ROLLED_BACK",
                "rollback_trigger": "DEPLOYMENT_STOP_ON_ALARM",
                "active_version": self.active_version,
                "final_weights": dict(self.routing_weights),
                "timeline": list(self.events),
            }

        self.alarm_state = "OK"
        self.active_version = candidate_version
        self.routing_weights = {candidate_version: 1.0}
        self.events.append(
            {
                "phase": "PROMOTED_100_PERCENT",
                "active_version": candidate_version,
                "weights": dict(self.routing_weights),
            }
        )
        return {
            "deployment_status": "SUCCEEDED",
            "rollback_trigger": None,
            "active_version": self.active_version,
            "final_weights": dict(self.routing_weights),
            "timeline": list(self.events),
        }


def simulate_canary_and_rollback() -> dict:
    """Kiểm tra logic máy trạng thái ngoại tuyến (v1 -> v2 thành công; v2 -> v3 gặp lỗi tự rollback về v2)."""
    sim = CanaryAliasSimulator(function_name="SatiBotBackendSprint2-chatbotFunction")
    healthy = sim.deploy_canary("2", simulated_error_rate=0.0)
    if healthy["deployment_status"] != "SUCCEEDED" or healthy["active_version"] != "2":
        raise AssertionError("Mô phỏng Canary bình thường không chuyển sang version 2.")

    sim.events.clear()
    faulty = sim.deploy_canary("3", simulated_error_rate=0.18, error_threshold=0.05)
    if (
        faulty["deployment_status"] != "ROLLED_BACK"
        or faulty["active_version"] != "2"
        or faulty["final_weights"] != {"2": 1.0}
    ):
        raise AssertionError(
            "Mô phỏng Canary gặp lỗi không tự động rollback về version 2."
        )

    return {
        "mode": "offline_state_machine_check",
        "canary_promotion_v1_to_v2": healthy,
        "alarm_rollback_v2_to_v3": faulty,
    }


def verify_live_aws_sandbox(
    stack_name: str, region: str = "ap-southeast-1", *, collect_only: bool = False
) -> dict:
    """Truy vấn trực tiếp AWS CloudFormation, Lambda, CloudWatch và CodeDeploy để thu thập bằng chứng thật."""
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    cd = boto3.client("codedeploy", region_name=region)

    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    if not stacks:
        raise RuntimeError(f"Không tìm thấy stack {stack_name} tại {region}.")
    outputs = {
        item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs", [])
    }
    app_name = outputs["CodeDeployApplicationName"]

    parameters = {
        item["ParameterKey"]: item.get("ParameterValue", "")
        for item in stacks[0].get("Parameters", [])
    }
    evidence = {
        "stack_name": stack_name,
        "region": region,
        "codedeploy_application": app_name,
        "functions": {},
    }
    for name in FUNCTIONS:
        title = name.title()
        live_arn = outputs[f"{title}LiveAliasArn"]
        fn_name = live_arn.split(":function:")[1].split(":")[0]
        alarm_name = outputs[f"{title}CanaryAlarmName"]
        dg_name = outputs[f"{title}DeploymentGroupName"]

        alias_cfg = lam.get_alias(FunctionName=fn_name, Name="live")
        alarm_list = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
        deployments = cd.list_deployments(
            applicationName=app_name, deploymentGroupName=dg_name, maxResults=5
        ).get("deployments", [])
        latest_deployment = (
            cd.get_deployment(deploymentId=deployments[0])["deploymentInfo"]
            if deployments
            else None
        )
        target = {}
        rollback_status = None
        if latest_deployment:
            deployment_id = latest_deployment["deploymentId"]
            targets = cd.list_deployment_targets(deploymentId=deployment_id).get(
                "targetIds", []
            )
            if targets:
                data = cd.get_deployment_target(
                    deploymentId=deployment_id, targetId=targets[0]
                )
                target = (
                    data.get("deploymentTarget", {})
                    .get("lambdaTarget", {})
                    .get("lambdaFunctionInfo", {})
                )
            rollback_id = (latest_deployment.get("rollbackInfo") or {}).get(
                "rollbackDeploymentId"
            )
            if rollback_id:
                rollback_status = cd.get_deployment(deploymentId=rollback_id)[
                    "deploymentInfo"
                ].get("status")
        invoke_res = lam.invoke(FunctionName=live_arn, Payload=b"{}")
        invoke_payload = json.loads(invoke_res["Payload"].read().decode("utf-8"))

        evidence["functions"][name] = {
            "live_alias_arn": live_arn,
            "active_version": alias_cfg.get("FunctionVersion"),
            "routing_config": alias_cfg.get("RoutingConfig", {}),
            "alarm_name": alarm_name,
            "alarm_state": alarm_list[0].get("StateValue") if alarm_list else "MISSING",
            "deployment_group": dg_name,
            "latest_deployment": {
                "deploymentId": latest_deployment.get("deploymentId"),
                "status": latest_deployment.get("status"),
                "rollbackInfo": latest_deployment.get("rollbackInfo"),
                "autoRollbackConfiguration": latest_deployment.get(
                    "autoRollbackConfiguration"
                ),
            }
            if latest_deployment
            else None,
            "live_alias_smoke_response": invoke_payload,
            "invoke_function_error": invoke_res.get("FunctionError"),
            "deployment_target": target,
            "rollback_status": rollback_status,
        }
    if not collect_only:
        validate_live_evidence(evidence)
        session = boto3.Session(region_name=region)
        evidence["caller_wiring"] = verify_callers(
            session,
            parameters,
            {
                name: item["live_alias_arn"]
                for name, item in evidence["functions"].items()
            },
        )
    evidence["validated"] = not collect_only
    return evidence


def run_verification(
    template_path: Path = DEFAULT_TEMPLATE,
    pipeline_path: Path = DEFAULT_PIPELINE,
    live_stack: str = "",
    region: str = "ap-southeast-1",
    collect_only: bool = False,
) -> dict:
    result = {
        "cdk": verify_cdk_canary_template(template_path),
        "pipeline": verify_pipeline_template(pipeline_path),
        "simulation": simulate_canary_and_rollback(),
    }
    if live_stack:
        result["live_aws_evidence"] = verify_live_aws_sandbox(
            live_stack, region=region, collect_only=collect_only
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Kiểm chứng CDK Canary/Rollback và CodePipeline SatiBot."
    )
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--pipeline", default=str(DEFAULT_PIPELINE))
    parser.add_argument("--live-stack", default="")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Collect AWS data without declaring validation success",
    )
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    summary = run_verification(
        Path(args.template),
        Path(args.pipeline),
        live_stack=args.live_stack.strip(),
        region=args.region.strip(),
        collect_only=args.collect_only,
    )
    print(
        f"CDK template validation passed ({summary['cdk']['functions']} functions, {summary['cdk']['live_aliases']} 'live' aliases, "
        f"{summary['cdk']['alarms']} Metric-Math alarms, {summary['cdk']['deployment_groups']} CodeDeploy groups)"
    )
    print(
        f"Pipeline template validation passed (stages: {' -> '.join(summary['pipeline']['stages'])}; "
        f"ParameterOverrides={summary['pipeline']['parameter_overrides_bytes']}B < 1000B)"
    )
    print(
        "Offline State-Machine Check OK (v1->v2 SUCCEEDED; v2->v3 ALARM -> ROLLED_BACK; "
        "use --live-stack <StackName> on AWS sandbox to capture real deploymentId/rollback evidence)"
    )
    if "live_aws_evidence" in summary:
        status = (
            "validation passed"
            if summary["live_aws_evidence"]["validated"]
            else "evidence collected (not validated)"
        )
        print(f"Live AWS {status} (stack={summary['live_aws_evidence']['stack_name']})")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
