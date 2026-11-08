"""
Author: Trịnh Thiên Quang
Kiểm chứng hợp đồng CDK CodeDeploy Canary/Rollback, Alarm Metric Math, giới hạn kích thước
CodePipeline ParameterOverrides/TemplateConfiguration và hỗ trợ lấy bằng chứng thực tế trên AWS Sandbox.
"""

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")
HTTP_FUNCTIONS = {"chatbot", "connector", "history"}
DEFAULT_TEMPLATE = ROOT / "dist" / "cdk.out" / "SatiBotBackendSprint2.template.json"
DEFAULT_PIPELINE = ROOT / "ci" / "pipeline.template.json"
CODEPIPELINE_PARAMETER_OVERRIDES_MAX_BYTES = 1000


def verify_cdk_canary_template(template_path: Path = DEFAULT_TEMPLATE) -> dict:
    """Xác thực template CDK sinh đủ Version gắn cấu hình, Alias 'live', Alarm Metric Math và CodeDeploy Canary."""
    if not template_path.exists():
        raise FileNotFoundError(f"Chưa có template CDK tại {template_path}; hãy chạy cdk synth trước.")
    template = json.loads(template_path.read_text(encoding="utf-8"))
    resources = template.get("Resources", {})
    parameters = template.get("Parameters", {})
    conditions = template.get("Conditions", {})
    outputs = template.get("Outputs", {})

    for required_param in ("CodeDeployRoleArn", "CanaryDeploymentConfig", "ConfigurationVersion", "ApiGatewaySourceArn"):
        if required_param not in parameters:
            raise AssertionError(f"Thiếu tham số {required_param} trong stack CDK.")
    if parameters["CanaryDeploymentConfig"].get("Default") != "CodeDeployDefault.LambdaCanary10Percent5Minutes":
        raise AssertionError("Chiến lược mặc định phải là CodeDeployDefault.LambdaCanary10Percent5Minutes.")

    by_type: dict[str, dict[str, dict]] = {}
    for logical_id, body in resources.items():
        by_type.setdefault(body.get("Type", ""), {})[logical_id] = body

    lambdas = by_type.get("AWS::Lambda::Function", {})
    versions = by_type.get("AWS::Lambda::Version", {})
    aliases = by_type.get("AWS::Lambda::Alias", {})
    permissions = by_type.get("AWS::Lambda::Permission", {})
    alarms = by_type.get("AWS::CloudWatch::Alarm", {})
    cd_apps = by_type.get("AWS::CodeDeploy::Application", {})
    cd_groups = by_type.get("AWS::CodeDeploy::DeploymentGroup", {})

    if (
        len(lambdas) != 5
        or len(versions) != 5
        or len(aliases) != 5
        or len(alarms) != 5
        or len(cd_groups) != 5
        or len(cd_apps) != 1
        or len(permissions) != len(HTTP_FUNCTIONS)
    ):
        raise AssertionError(
            f"Số lượng tài nguyên không khớp: functions={len(lambdas)}, versions={len(versions)}, "
            f"aliases={len(aliases)}, alarms={len(alarms)}, permissions={len(permissions)}, "
            f"codedeploy_apps={len(cd_apps)}, deployment_groups={len(cd_groups)}"
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
        for suffix in ("InvokeArn", "LiveAliasArn", "FunctionArn", "CanaryAlarmName", "DeploymentGroupName"):
            if f"{title}{suffix}" not in outputs:
                raise AssertionError(f"Thiếu output {title}{suffix}.")
        if outputs[f"{title}InvokeArn"]["Value"] != outputs[f"{title}LiveAliasArn"]["Value"]:
            raise AssertionError(f"{title}InvokeArn bắt buộc phải trỏ tới alias 'live', không được trỏ $LATEST.")

    # Kiểm tra từng AWS::Lambda::Version bắt buộc phụ thuộc ConfigurationVersion và giữ lại version cũ để rollback
    for ver_id, ver_res in versions.items():
        if ver_res.get("DeletionPolicy") != "Retain":
            raise AssertionError(f"Version {ver_id} phải đặt DeletionPolicy=Retain để bảo toàn bản ổn định khi rollback.")
        desc = json.dumps(ver_res.get("Properties", {}).get("Description", ""))
        if "ConfigurationVersion" not in desc:
            raise AssertionError(f"Version {ver_id} chưa gắn ConfigurationVersion vào Description để tạo version mới khi đổi cấu hình.")

    # Kiểm tra quyền invoke của API Gateway chỉ gắn vào Alias 'live', tuyệt đối không gắn vào $LATEST
    alias_ids = set(aliases)
    for perm_id, perm_res in permissions.items():
        fn_target = perm_res.get("Properties", {}).get("FunctionName", {})
        if not isinstance(fn_target, dict) or fn_target.get("Ref") not in alias_ids:
            raise AssertionError(f"Permission {perm_id} không trỏ vào AWS::Lambda::Alias 'live': {fn_target}")

    # Kiểm tra đủ 5 CloudWatch Alarm dùng Metric Math theo dõi cả AWS/Lambda Errors (:live) và SatiBot/Backend PlatformFault
    for alarm_id, alarm_res in alarms.items():
        metrics = alarm_res.get("Properties", {}).get("Metrics", [])
        serialized = json.dumps(metrics)
        if '"AWS/Lambda"' not in serialized or '"SatiBot/Backend"' not in serialized or '"PlatformFault"' not in serialized:
            raise AssertionError(f"Alarm {alarm_id} thiếu kết hợp AWS/Lambda Errors và SatiBot/Backend PlatformFault.")
        if ":live" not in serialized or "m1 + FILL(m2, 0)" not in serialized:
            raise AssertionError(f"Alarm {alarm_id} chưa lọc theo ':live' hoặc thiếu biểu thức Metric Math 'm1 + FILL(m2, 0)'.")

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

    alarm_ids = set(alarms)
    for dg_id, dg_res in cd_groups.items():
        props = dg_res.get("Properties", {})
        style = props.get("DeploymentStyle", {})
        if style != {"DeploymentOption": "WITH_TRAFFIC_CONTROL", "DeploymentType": "BLUE_GREEN"}:
            raise AssertionError(f"DeploymentGroup {dg_id} thiếu cấu hình WITH_TRAFFIC_CONTROL/BLUE_GREEN.")
        if props.get("DeploymentConfigName") != {"Ref": "CanaryDeploymentConfig"}:
            raise AssertionError(f"DeploymentGroup {dg_id} chưa tham chiếu CanaryDeploymentConfig.")

        # Bắt buộc kiểm tra chặt AlarmConfiguration: không được phép rỗng kể cả khi chưa truyền Alarm ngoài
        alarm_cfg = props.get("AlarmConfiguration")
        if not isinstance(alarm_cfg, dict) or "Fn::If" not in alarm_cfg:
            raise AssertionError(f"DeploymentGroup {dg_id} thiếu AlarmConfiguration.")
        if_branches = alarm_cfg["Fn::If"]
        if len(if_branches) != 3:
            raise AssertionError(f"AlarmConfiguration của {dg_id} sai cấu trúc Fn::If.")
        for branch in (if_branches[1], if_branches[2]):
            if not isinstance(branch, dict) or branch.get("Enabled") is not True:
                raise AssertionError(f"AlarmConfiguration của {dg_id} phải bật Enabled=True ở mọi nhánh.")
            branch_alarms = branch.get("Alarms", [])
            if not branch_alarms or branch_alarms[0].get("Name", {}).get("Ref") not in alarm_ids:
                raise AssertionError(f"DeploymentGroup {dg_id} thiếu CloudWatch Alarm mặc định tích hợp sẵn.")
        if len(if_branches[1].get("Alarms", [])) != 2:
            raise AssertionError(f"Nhánh có Alarm ngoài của {dg_id} phải gộp cả Alarm mặc định và Alarm từ MonitoringStack.")

        rollback = props.get("AutoRollbackConfiguration", {})
        if rollback.get("Enabled") is not True:
            raise AssertionError(f"DeploymentGroup {dg_id} chưa bật AutoRollbackConfiguration.")
        events = set(rollback.get("Events", []))
        if events != {"DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_REQUEST", "DEPLOYMENT_STOP_ON_ALARM"}:
            raise AssertionError(f"DeploymentGroup {dg_id} phải bật đủ 3 sự kiện rollback mặc định, nhận được: {events}")

    return {
        "stack_template": str(template_path.relative_to(ROOT)) if template_path.is_relative_to(ROOT) else str(template_path),
        "functions": len(lambdas),
        "versions": len(versions),
        "live_aliases": len(aliases),
        "alarms": len(alarms),
        "deployment_groups": len(cd_groups),
        "default_strategy": parameters["CanaryDeploymentConfig"]["Default"],
    }


def verify_pipeline_template(pipeline_path: Path = DEFAULT_PIPELINE) -> dict:
    """Xác thực CodePipeline 4 giai đoạn, TemplateConfiguration và giới hạn ParameterOverrides < 1 KB."""
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

    changeset_cfg = deploy_actions[1]["Configuration"]
    if changeset_cfg.get("TemplateConfiguration") != "DeployConfig::template-configuration.json":
        raise AssertionError("CreateReplaceChangeSet phải dùng TemplateConfiguration từ artifact DeployConfig.")

    raw_overrides = changeset_cfg.get("ParameterOverrides", {}).get("Fn::Sub", "")
    simulated_secret_arn = "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:satibot/production/gemini-api-key-AbCdEf"
    rendered_overrides = raw_overrides.replace("${GeminiSecretArn}", simulated_secret_arn)
    overrides_bytes = len(rendered_overrides.encode("utf-8"))
    if overrides_bytes >= CODEPIPELINE_PARAMETER_OVERRIDES_MAX_BYTES:
        raise AssertionError(
            f"ParameterOverrides vượt giới hạn 1000 byte của AWS CodePipeline ({overrides_bytes} bytes)."
        )

    publish_spec = resources["PublishAssets"]["Properties"]["Source"]["BuildSpec"]
    if "ConfigurationVersion" not in publish_spec or "template-configuration.json" not in publish_spec:
        raise AssertionError("PublishAssets phải sinh template-configuration.json kèm băm ConfigurationVersion.")

    return {
        "pipeline_template": str(pipeline_path.relative_to(ROOT)) if pipeline_path.is_relative_to(ROOT) else str(pipeline_path),
        "stages": stage_names,
        "deploy_actions": [act["Name"] for act in deploy_actions],
        "parameter_overrides_bytes": overrides_bytes,
        "regions": build_env,
    }


@dataclass
class CanaryAliasSimulator:
    """Mô phỏng máy trạng thái nội bộ của Canary10Percent5Minutes + Alarm Rollback (kiểm tra logic ngoại tuyến)."""

    function_name: str
    active_version: str = "1"
    routing_weights: dict[str, float] = field(default_factory=lambda: {"1": 1.0})
    alarm_state: str = "OK"
    events: list[dict] = field(default_factory=list)

    def deploy_canary(self, candidate_version: str, *, simulated_error_rate: float, error_threshold: float = 0.05) -> dict:
        previous = self.active_version
        self.routing_weights = {previous: 0.9, candidate_version: 0.1}
        self.events.append({
            "phase": "CANARY_10_PERCENT",
            "weights": dict(self.routing_weights),
            "candidate_version": candidate_version,
        })

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
    """Kiểm tra logic máy trạng thái ngoại tuyến (v1 -> v2 thành công; v2 -> v3 gặp lỗi tự rollback về v2)."""
    sim = CanaryAliasSimulator(function_name="SatiBotBackendSprint2-chatbotFunction")
    healthy = sim.deploy_canary("2", simulated_error_rate=0.0)
    if healthy["deployment_status"] != "SUCCEEDED" or healthy["active_version"] != "2":
        raise AssertionError("Mô phỏng Canary bình thường không chuyển sang version 2.")

    sim.events.clear()
    faulty = sim.deploy_canary("3", simulated_error_rate=0.18, error_threshold=0.05)
    if faulty["deployment_status"] != "ROLLED_BACK" or faulty["active_version"] != "2" or faulty["final_weights"] != {"2": 1.0}:
        raise AssertionError("Mô phỏng Canary gặp lỗi không tự động rollback về version 2.")

    return {
        "mode": "offline_state_machine_check",
        "canary_promotion_v1_to_v2": healthy,
        "alarm_rollback_v2_to_v3": faulty,
    }


def verify_live_aws_sandbox(stack_name: str, region: str = "ap-southeast-1") -> dict:
    """Truy vấn trực tiếp AWS CloudFormation, Lambda, CloudWatch và CodeDeploy để thu thập bằng chứng thật."""
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    cd = boto3.client("codedeploy", region_name=region)

    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    if not stacks:
        raise RuntimeError(f"Không tìm thấy stack {stack_name} tại {region}.")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs", [])}
    app_name = outputs["CodeDeployApplicationName"]

    evidence = {"stack_name": stack_name, "region": region, "codedeploy_application": app_name, "functions": {}}
    for name in FUNCTIONS:
        title = name.title()
        live_arn = outputs[f"{title}LiveAliasArn"]
        fn_name = live_arn.split(":function:")[1].split(":")[0]
        alarm_name = outputs[f"{title}CanaryAlarmName"]
        dg_name = outputs[f"{title}DeploymentGroupName"]

        alias_cfg = lam.get_alias(FunctionName=fn_name, Name="live")
        alarm_list = cw.describe_alarms(AlarmNames=[alarm_name]).get("MetricAlarms", [])
        deployments = cd.list_deployments(applicationName=app_name, deploymentGroupName=dg_name, maxResults=5).get("deployments", [])
        latest_deployment = cd.get_deployment(deploymentId=deployments[0])["deploymentInfo"] if deployments else None
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
                "autoRollbackConfiguration": latest_deployment.get("autoRollbackConfiguration"),
            }
            if latest_deployment
            else None,
            "live_alias_smoke_response": invoke_payload,
        }
    return evidence


def run_verification(
    template_path: Path = DEFAULT_TEMPLATE,
    pipeline_path: Path = DEFAULT_PIPELINE,
    live_stack: str = "",
    region: str = "ap-southeast-1",
) -> dict:
    result = {
        "cdk": verify_cdk_canary_template(template_path),
        "pipeline": verify_pipeline_template(pipeline_path),
        "simulation": simulate_canary_and_rollback(),
    }
    if live_stack:
        result["live_aws_evidence"] = verify_live_aws_sandbox(live_stack, region=region)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kiểm chứng CDK Canary/Rollback và CodePipeline SatiBot.")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--pipeline", default=str(DEFAULT_PIPELINE))
    parser.add_argument("--live-stack", default="")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    summary = run_verification(
        Path(args.template),
        Path(args.pipeline),
        live_stack=args.live_stack.strip(),
        region=args.region.strip(),
    )
    print(
        f"CDK Contract OK ({summary['cdk']['functions']} functions, {summary['cdk']['live_aliases']} 'live' aliases, "
        f"{summary['cdk']['alarms']} Metric-Math alarms, {summary['cdk']['deployment_groups']} CodeDeploy groups)"
    )
    print(
        f"Pipeline Contract OK (stages: {' -> '.join(summary['pipeline']['stages'])}; "
        f"ParameterOverrides={summary['pipeline']['parameter_overrides_bytes']}B < 1000B)"
    )
    print(
        "Offline State-Machine Check OK (v1->v2 SUCCEEDED; v2->v3 ALARM -> ROLLED_BACK; "
        "use --live-stack <StackName> on AWS sandbox to capture real deploymentId/rollback evidence)"
    )
    if "live_aws_evidence" in summary:
        print(f"Live AWS Sandbox Evidence OK (stack={summary['live_aws_evidence']['stack_name']})")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
