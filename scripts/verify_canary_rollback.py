"""
Author: Trịnh Thiên Quang
Kiểm chứng cấu hình CDK CodeDeploy Canary/Rollback, CodePipeline 4 giai đoạn
và mô phỏng chuyển dịch lưu lượng Canary cùng tự động Rollback khi CloudWatch Alarm kích hoạt.
"""

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")
DEFAULT_TEMPLATE = ROOT / "dist" / "cdk.out" / "SatiBotBackendSprint2.template.json"
DEFAULT_PIPELINE = ROOT / "ci" / "pipeline.template.json"


def verify_cdk_canary_template(template_path: Path = DEFAULT_TEMPLATE) -> dict:
    """Xác thực template CDK đã sinh đủ 5 Lambda, Version, Alias 'live' và CodeDeploy Canary/Rollback."""
    if not template_path.exists():
        raise FileNotFoundError(f"Chưa có template CDK tại {template_path}; hãy chạy cdk synth trước.")
    template = json.loads(template_path.read_text(encoding="utf-8"))
    resources = template.get("Resources", {})
    parameters = template.get("Parameters", {})
    conditions = template.get("Conditions", {})
    outputs = template.get("Outputs", {})

    if "CodeDeployRoleArn" not in parameters or "CanaryDeploymentConfig" not in parameters:
        raise AssertionError("Thiếu tham số CodeDeployRoleArn hoặc CanaryDeploymentConfig.")
    if parameters["CanaryDeploymentConfig"].get("Default") != "CodeDeployDefault.LambdaCanary10Percent5Minutes":
        raise AssertionError("Chiến lược mặc định phải là CodeDeployDefault.LambdaCanary10Percent5Minutes.")

    by_type: dict[str, dict[str, dict]] = {}
    for logical_id, body in resources.items():
        by_type.setdefault(body.get("Type", ""), {})[logical_id] = body

    lambdas = by_type.get("AWS::Lambda::Function", {})
    versions = by_type.get("AWS::Lambda::Version", {})
    aliases = by_type.get("AWS::Lambda::Alias", {})
    cd_apps = by_type.get("AWS::CodeDeploy::Application", {})
    cd_groups = by_type.get("AWS::CodeDeploy::DeploymentGroup", {})

    if len(lambdas) != 5 or len(versions) != 5 or len(aliases) != 5 or len(cd_groups) != 5 or len(cd_apps) != 1:
        raise AssertionError(
            f"Số lượng tài nguyên không khớp: functions={len(lambdas)}, versions={len(versions)}, "
            f"aliases={len(aliases)}, codedeploy_apps={len(cd_apps)}, deployment_groups={len(cd_groups)}"
        )

    app_logical_id = next(iter(cd_apps))
    if cd_apps[app_logical_id]["Properties"].get("ComputePlatform") != "Lambda":
        raise AssertionError("CodeDeploy Application phải dùng ComputePlatform=Lambda.")

    for name in FUNCTIONS:
        title = name.title()
        if f"{title}AlarmName" not in parameters:
            raise AssertionError(f"Thiếu tham số {title}AlarmName.")
        if f"Has{title}Alarm" not in conditions:
            raise AssertionError(f"Thiếu điều kiện Has{title}Alarm.")
        for suffix in ("FunctionArn", "LiveAliasArn", "DeploymentGroupName"):
            if f"{title}{suffix}" not in outputs:
                raise AssertionError(f"Thiếu output {title}{suffix}.")

    for alias_id, alias_res in aliases.items():
        props = alias_res.get("Properties", {})
        if props.get("Name") != "live":
            raise AssertionError(f"Alias {alias_id} không đặt tên 'live'.")
        update_policy = alias_res.get("UpdatePolicy", {}).get("CodeDeployLambdaAliasUpdate", {})
        if update_policy.get("ApplicationName") != {"Ref": app_logical_id}:
            raise AssertionError(f"Alias {alias_id} chưa gắn CodeDeploy Application.")
        dg_ref = update_policy.get("DeploymentGroupName", {}).get("Ref")
        if dg_ref not in cd_groups:
            raise AssertionError(f"Alias {alias_id} chưa trỏ tới DeploymentGroup hợp lệ.")

    for dg_id, dg_res in cd_groups.items():
        props = dg_res.get("Properties", {})
        style = props.get("DeploymentStyle", {})
        if style != {"DeploymentOption": "WITH_TRAFFIC_CONTROL", "DeploymentType": "BLUE_GREEN"}:
            raise AssertionError(f"DeploymentGroup {dg_id} thiếu cấu hình WITH_TRAFFIC_CONTROL/BLUE_GREEN.")
        if props.get("DeploymentConfigName") != {"Ref": "CanaryDeploymentConfig"}:
            raise AssertionError(f"DeploymentGroup {dg_id} chưa tham chiếu CanaryDeploymentConfig.")
        rollback = props.get("AutoRollbackConfiguration", {}).get("Fn::If")
        if not rollback or len(rollback) != 3:
            raise AssertionError(f"DeploymentGroup {dg_id} thiếu Fn::If cho AutoRollbackConfiguration.")
        with_alarm_events = set(rollback[1].get("Events", []))
        without_alarm_events = set(rollback[2].get("Events", []))
        if with_alarm_events != {"DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_REQUEST", "DEPLOYMENT_STOP_ON_ALARM"}:
            raise AssertionError(f"Sự kiện rollback có alarm của {dg_id} không khớp.")
        if without_alarm_events != {"DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_REQUEST"}:
            raise AssertionError(f"Sự kiện rollback mặc định của {dg_id} không khớp.")

    return {
        "stack_template": str(template_path.relative_to(ROOT)) if template_path.is_relative_to(ROOT) else str(template_path),
        "functions": len(lambdas),
        "versions": len(versions),
        "live_aliases": len(aliases),
        "deployment_groups": len(cd_groups),
        "default_strategy": parameters["CanaryDeploymentConfig"]["Default"],
    }


def verify_pipeline_template(pipeline_path: Path = DEFAULT_PIPELINE) -> dict:
    """Xác thực CodePipeline gồm đủ 4 stage: Source -> BuildTestSynth -> ManualApproval -> DeployBackend."""
    template = json.loads(pipeline_path.read_text(encoding="utf-8"))
    resources = template.get("Resources", {})
    pipeline = resources["Pipeline"]["Properties"]
    stages = pipeline["Stages"]
    stage_names = [stage["Name"] for stage in stages]
    expected = ["Source", "BuildTestSynth", "ManualApproval", "DeployBackend"]
    if stage_names != expected:
        raise AssertionError(f"Thứ tự stage pipeline {stage_names} khác thiết kế {expected}.")

    build_env = {
        item["Name"]: item["Value"]
        for item in resources["Build"]["Properties"]["Environment"]["EnvironmentVariables"]
    }
    if build_env.get("PRIMARY_REGION") != "ap-southeast-1" or build_env.get("SECONDARY_REGION") != "us-east-1":
        raise AssertionError("CodeBuild thiếu biến môi trường PRIMARY_REGION=ap-southeast-1 / SECONDARY_REGION=us-east-1.")

    approval_action = stages[2]["Actions"][0]
    if approval_action["ActionTypeId"] != {
        "Category": "Approval",
        "Owner": "AWS",
        "Provider": "Manual",
        "Version": "1",
    }:
        raise AssertionError("Stage ManualApproval phải sử dụng AWS Manual Approval provider.")

    deploy_actions = stages[3]["Actions"]
    deploy_modes = [
        (act["Name"], act.get("RunOrder"), act["ActionTypeId"]["Provider"], act["Configuration"].get("ActionMode"))
        for act in deploy_actions
    ]
    if deploy_modes != [
        ("PublishCdkAssets", 1, "CodeBuild", None),
        ("CreateReplaceChangeSet", 2, "CloudFormation", "CHANGE_SET_REPLACE"),
        ("ExecuteChangeSet", 3, "CloudFormation", "CHANGE_SET_EXECUTE"),
    ]:
        raise AssertionError(f"Chuỗi hành động DeployBackend không đúng chuẩn CDK/CloudFormation: {deploy_modes}")

    return {
        "pipeline_template": str(pipeline_path.relative_to(ROOT)) if pipeline_path.is_relative_to(ROOT) else str(pipeline_path),
        "stages": stage_names,
        "deploy_actions": [act["Name"] for act in deploy_actions],
        "regions": build_env,
    }


@dataclass
class CanaryAliasSimulator:
    """Mô phỏng hành vi AWS CodeDeploy Canary10Percent5Minutes + CloudWatch Alarm Rollback trên alias 'live'."""

    function_name: str
    active_version: str = "1"
    routing_weights: dict[str, float] = field(default_factory=lambda: {"1": 1.0})
    alarm_state: str = "OK"
    events: list[dict] = field(default_factory=list)

    def deploy_canary(self, candidate_version: str, *, simulated_error_rate: float, error_threshold: float = 0.05) -> dict:
        previous = self.active_version
        # Bước 1: Chuyển 10% lưu lượng sang phiên bản mới trong cửa sổ Canary 5 phút.
        self.routing_weights = {previous: 0.9, candidate_version: 0.1}
        self.events.append({
            "phase": "CANARY_10_PERCENT",
            "weights": dict(self.routing_weights),
            "candidate_version": candidate_version,
        })

        # Bước 2: CloudWatch Alarm đánh giá tỷ lệ lỗi trên lưu lượng Canary.
        if simulated_error_rate >= error_threshold:
            self.alarm_state = "ALARM"
            self.routing_weights = {previous: 1.0}
            self.active_version = previous
            self.events.append({
                "phase": "AUTO_ROLLBACK",
                "trigger": "DEPLOYMENT_STOP_ON_ALARM",
                "restored_version": previous,
                "weights": dict(self.routing_weights),
            })
            self.alarm_state = "OK"
            return {
                "deployment_status": "ROLLED_BACK",
                "rollback_trigger": "DEPLOYMENT_STOP_ON_ALARM",
                "active_version": self.active_version,
                "final_weights": dict(self.routing_weights),
                "timeline": list(self.events),
            }

        # Bước 3: Không có lỗi -> chuyển 100% lưu lượng sang phiên bản mới.
        self.alarm_state = "OK"
        self.active_version = candidate_version
        self.routing_weights = {candidate_version: 1.0}
        self.events.append({
            "phase": "PROMOTED_100_PERCENT",
            "active_version": candidate_version,
            "weights": dict(self.routing_weights),
        })
        return {
            "deployment_status": "SUCCEEDED",
            "rollback_trigger": None,
            "active_version": self.active_version,
            "final_weights": dict(self.routing_weights),
            "timeline": list(self.events),
        }


def simulate_canary_and_rollback() -> dict:
    """Thực thi kịch bản Canary thành công (v1 -> v2) và kịch bản lỗi kích hoạt Auto-Rollback (v2 -> v3 -> v2)."""
    sim = CanaryAliasSimulator(function_name="SatiBotBackendSprint2-chatbotFunction")
    healthy = sim.deploy_canary("2", simulated_error_rate=0.0)
    if healthy["deployment_status"] != "SUCCEEDED" or healthy["active_version"] != "2":
        raise AssertionError("Mô phỏng Canary bình thường không chuyển sang version 2.")

    sim.events.clear()
    faulty = sim.deploy_canary("3", simulated_error_rate=0.18, error_threshold=0.05)
    if faulty["deployment_status"] != "ROLLED_BACK" or faulty["active_version"] != "2" or faulty["final_weights"] != {"2": 1.0}:
        raise AssertionError("Mô phỏng Canary gặp lỗi không tự động rollback về version 2.")

    return {
        "canary_promotion_v1_to_v2": healthy,
        "alarm_rollback_v2_to_v3": faulty,
    }


def run_verification(template_path: Path = DEFAULT_TEMPLATE, pipeline_path: Path = DEFAULT_PIPELINE) -> dict:
    return {
        "cdk": verify_cdk_canary_template(template_path),
        "pipeline": verify_pipeline_template(pipeline_path),
        "simulation": simulate_canary_and_rollback(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kiểm chứng CDK Canary/Rollback và CodePipeline SatiBot.")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--pipeline", default=str(DEFAULT_PIPELINE))
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    summary = run_verification(Path(args.template), Path(args.pipeline))
    print(
        f"CDK Canary OK ({summary['cdk']['functions']} functions, {summary['cdk']['live_aliases']} 'live' aliases, "
        f"{summary['cdk']['deployment_groups']} CodeDeploy groups: {summary['cdk']['default_strategy']})"
    )
    print(f"Pipeline OK (stages: {' -> '.join(summary['pipeline']['stages'])})")
    print("Simulation OK (v1->v2 SUCCEEDED; v2->v3 ALARM -> ROLLED_BACK to v2)")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
