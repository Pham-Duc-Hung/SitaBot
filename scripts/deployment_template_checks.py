"""
Author: Trịnh Thiên Quang
Kiểm tra template Lambda canary, alarm và cấu hình pipeline.
"""

import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_sprint2 import FUNCTIONS

HTTP_FUNCTIONS = {"chatbot", "connector", "history"}
DEFAULT_TEMPLATE = ROOT / "dist" / "cdk.out" / "SatiBotBackendSprint2.template.json"
DEFAULT_PIPELINE = ROOT / "ci" / "pipeline.template.json"
CODEPIPELINE_PARAMETER_OVERRIDES_MAX_BYTES = 1000


def verify_cdk_canary_template(template_path: Path = DEFAULT_TEMPLATE) -> dict:
    """Xác thực template CDK sinh đủ Version gắn cấu hình, Alias 'live', Alarm Metric Math và CodeDeploy Canary."""
    if not template_path.exists():
        raise FileNotFoundError(
            f"Chưa có template CDK tại {template_path}; hãy chạy cdk synth trước."
        )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    resources = template.get("Resources", {})
    parameters = template.get("Parameters", {})
    conditions = template.get("Conditions", {})
    outputs = template.get("Outputs", {})

    for required_param in (
        "CodeDeployRoleArn",
        "CanaryDeploymentConfig",
        "ConfigurationVersion",
        "ApiGatewaySourceArn",
    ):
        if required_param not in parameters:
            raise AssertionError(f"Thiếu tham số {required_param} trong stack CDK.")
    if (
        parameters["CanaryDeploymentConfig"].get("Default")
        != "CodeDeployDefault.LambdaCanary10Percent5Minutes"
    ):
        raise AssertionError(
            "Chiến lược mặc định phải là CodeDeployDefault.LambdaCanary10Percent5Minutes."
        )

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
        for suffix in (
            "InvokeArn",
            "LiveAliasArn",
            "FunctionArn",
            "CanaryAlarmName",
            "DeploymentGroupName",
        ):
            if f"{title}{suffix}" not in outputs:
                raise AssertionError(f"Thiếu output {title}{suffix}.")
        if (
            outputs[f"{title}InvokeArn"]["Value"]
            != outputs[f"{title}LiveAliasArn"]["Value"]
        ):
            raise AssertionError(
                f"{title}InvokeArn bắt buộc phải trỏ tới alias 'live', không được trỏ $LATEST."
            )

    # Kiểm tra từng AWS::Lambda::Version bắt buộc phụ thuộc ConfigurationVersion và giữ lại version cũ để rollback
    for ver_id, ver_res in versions.items():
        if ver_res.get("DeletionPolicy") != "Retain":
            raise AssertionError(
                f"Version {ver_id} phải đặt DeletionPolicy=Retain để bảo toàn bản ổn định khi rollback."
            )
        desc = json.dumps(ver_res.get("Properties", {}).get("Description", ""))
        if "ConfigurationVersion" not in desc:
            raise AssertionError(
                f"Version {ver_id} chưa gắn ConfigurationVersion vào Description để tạo version mới khi đổi cấu hình."
            )

    # Kiểm tra quyền invoke của API Gateway chỉ gắn vào Alias 'live', tuyệt đối không gắn vào $LATEST
    alias_ids = set(aliases)
    for perm_id, perm_res in permissions.items():
        fn_target = perm_res.get("Properties", {}).get("FunctionName", {})
        if not isinstance(fn_target, dict) or fn_target.get("Ref") not in alias_ids:
            raise AssertionError(
                f"Permission {perm_id} không trỏ vào AWS::Lambda::Alias 'live': {fn_target}"
            )

    # Kiểm tra đủ 5 CloudWatch Alarm dùng Metric Math theo dõi cả AWS/Lambda Errors (:live) và SatiBot/Backend PlatformFault
    for alarm_id, alarm_res in alarms.items():
        metrics = alarm_res.get("Properties", {}).get("Metrics", [])
        serialized = json.dumps(metrics)
        if (
            '"AWS/Lambda"' not in serialized
            or '"SatiBot/Backend"' not in serialized
            or '"PlatformFault"' not in serialized
        ):
            raise AssertionError(
                f"Alarm {alarm_id} thiếu kết hợp AWS/Lambda Errors và SatiBot/Backend PlatformFault."
            )
        if ":live" not in serialized or "FILL(m1, 0) + FILL(m2, 0)" not in serialized:
            raise AssertionError(
                f"Alarm {alarm_id} chưa lọc theo ':live' hoặc thiếu biểu thức Metric Math 'FILL(m1, 0) + FILL(m2, 0)'."
            )

    for alias_id, alias_res in aliases.items():
        props = alias_res.get("Properties", {})
        if props.get("Name") != "live":
            raise AssertionError(f"Alias {alias_id} không đặt tên 'live'.")
        update_policy = alias_res.get("UpdatePolicy", {}).get(
            "CodeDeployLambdaAliasUpdate", {}
        )
        if update_policy.get("ApplicationName") != {"Ref": app_logical_id}:
            raise AssertionError(f"Alias {alias_id} chưa gắn CodeDeploy Application.")
        dg_ref = update_policy.get("DeploymentGroupName", {}).get("Ref")
        if dg_ref not in cd_groups:
            raise AssertionError(
                f"Alias {alias_id} chưa trỏ tới DeploymentGroup hợp lệ."
            )

    alarm_ids = set(alarms)
    for dg_id, dg_res in cd_groups.items():
        props = dg_res.get("Properties", {})
        style = props.get("DeploymentStyle", {})
        if style != {
            "DeploymentOption": "WITH_TRAFFIC_CONTROL",
            "DeploymentType": "BLUE_GREEN",
        }:
            raise AssertionError(
                f"DeploymentGroup {dg_id} thiếu cấu hình WITH_TRAFFIC_CONTROL/BLUE_GREEN."
            )
        if props.get("DeploymentConfigName") != {"Ref": "CanaryDeploymentConfig"}:
            raise AssertionError(
                f"DeploymentGroup {dg_id} chưa tham chiếu CanaryDeploymentConfig."
            )

        # Bắt buộc kiểm tra chặt AlarmConfiguration: không được phép rỗng kể cả khi chưa truyền Alarm ngoài
        alarm_cfg = props.get("AlarmConfiguration")
        if not isinstance(alarm_cfg, dict) or "Fn::If" not in alarm_cfg:
            raise AssertionError(f"DeploymentGroup {dg_id} thiếu AlarmConfiguration.")
        if_branches = alarm_cfg["Fn::If"]
        if len(if_branches) != 3:
            raise AssertionError(f"AlarmConfiguration của {dg_id} sai cấu trúc Fn::If.")
        for branch in (if_branches[1], if_branches[2]):
            if not isinstance(branch, dict) or branch.get("Enabled") is not True:
                raise AssertionError(
                    f"AlarmConfiguration của {dg_id} phải bật Enabled=True ở mọi nhánh."
                )
            branch_alarms = branch.get("Alarms", [])
            if (
                not branch_alarms
                or branch_alarms[0].get("Name", {}).get("Ref") not in alarm_ids
            ):
                raise AssertionError(
                    f"DeploymentGroup {dg_id} thiếu CloudWatch Alarm mặc định tích hợp sẵn."
                )
        if len(if_branches[1].get("Alarms", [])) != 2:
            raise AssertionError(
                f"Nhánh có Alarm ngoài của {dg_id} phải gộp cả Alarm mặc định và Alarm từ MonitoringStack."
            )

        rollback = props.get("AutoRollbackConfiguration", {})
        if rollback.get("Enabled") is not True:
            raise AssertionError(
                f"DeploymentGroup {dg_id} chưa bật AutoRollbackConfiguration."
            )
        events = set(rollback.get("Events", []))
        if events != {
            "DEPLOYMENT_FAILURE",
            "DEPLOYMENT_STOP_ON_REQUEST",
            "DEPLOYMENT_STOP_ON_ALARM",
        }:
            raise AssertionError(
                f"DeploymentGroup {dg_id} phải bật đủ 3 sự kiện rollback mặc định, nhận được: {events}"
            )

    return {
        "stack_template": str(template_path.relative_to(ROOT))
        if template_path.is_relative_to(ROOT)
        else str(template_path),
        "functions": len(lambdas),
        "versions": len(versions),
        "live_aliases": len(aliases),
        "alarms": len(alarms),
        "deployment_groups": len(cd_groups),
        "default_strategy": parameters["CanaryDeploymentConfig"]["Default"],
    }


def _resolve_override_string(value):
    """Resolve literal intrinsics; reject values whose deployed size is unknown."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        spec = value["Fn::Join"]
        if not isinstance(spec, list) or len(spec) != 2:
            raise AssertionError("Invalid ParameterOverrides Fn::Join.")
        delimiter, parts = spec
        if isinstance(delimiter, str) and isinstance(parts, list):
            return delimiter.join(_resolve_override_string(part) for part in parts)
    if isinstance(value, dict) and set(value) == {"Fn::Sub"}:
        spec = value["Fn::Sub"]
        if not isinstance(spec, str) and (not isinstance(spec, list) or len(spec) != 2):
            raise AssertionError("Invalid ParameterOverrides Fn::Sub.")
        template, variables = (spec, {}) if isinstance(spec, str) else spec
        if isinstance(template, str) and isinstance(variables, dict):

            def replace(match):
                name = match.group(1)
                if name.startswith("!"):
                    return "${" + name[1:] + "}"
                if name not in variables:
                    raise AssertionError(
                        "Cannot verify ParameterOverrides size with unresolved substitutions; "
                        "use TemplateConfiguration."
                    )
                return _resolve_override_string(variables[name])

            return re.sub(r"\$\{([^}]+)\}", replace, template)
    raise AssertionError(
        "Cannot verify ParameterOverrides size for unresolved intrinsics; "
        "use TemplateConfiguration."
    )


def verify_pipeline_template(pipeline_path: Path = DEFAULT_PIPELINE) -> dict:
    """Xác thực CodePipeline 4 giai đoạn, TemplateConfiguration và giới hạn ParameterOverrides < 1 KB."""
    template = json.loads(pipeline_path.read_text(encoding="utf-8"))
    resources = template.get("Resources", {})
    pipeline = resources["Pipeline"]["Properties"]
    stages = pipeline["Stages"]
    stage_names = [stage["Name"] for stage in stages]
    expected = ["Source", "BuildTestSynth", "ManualApproval", "DeployBackend"]
    if stage_names != expected:
        raise AssertionError(
            f"Thứ tự stage pipeline {stage_names} khác thiết kế {expected}."
        )

    build_env = {
        item["Name"]: item["Value"]
        for item in resources["Build"]["Properties"]["Environment"][
            "EnvironmentVariables"
        ]
    }
    if (
        build_env.get("PRIMARY_REGION") != "ap-southeast-1"
        or build_env.get("SECONDARY_REGION") != "us-east-1"
    ):
        raise AssertionError(
            "CodeBuild thiếu biến môi trường PRIMARY_REGION=ap-southeast-1 / SECONDARY_REGION=us-east-1."
        )

    approval_action = stages[2]["Actions"][0]
    if approval_action["ActionTypeId"] != {
        "Category": "Approval",
        "Owner": "AWS",
        "Provider": "Manual",
        "Version": "1",
    }:
        raise AssertionError(
            "Stage ManualApproval phải sử dụng AWS Manual Approval provider."
        )

    deploy_actions = stages[3]["Actions"]
    deploy_modes = [
        (
            act["Name"],
            act.get("RunOrder"),
            act["ActionTypeId"]["Provider"],
            act["Configuration"].get("ActionMode"),
        )
        for act in deploy_actions
    ]
    if deploy_modes != [
        ("PublishCdkAssets", 1, "CodeBuild", None),
        ("CreateReplaceChangeSet", 2, "CloudFormation", "CHANGE_SET_REPLACE"),
        ("ExecuteChangeSet", 3, "CloudFormation", "CHANGE_SET_EXECUTE"),
    ]:
        raise AssertionError(
            f"Chuỗi hành động DeployBackend không đúng chuẩn CDK/CloudFormation: {deploy_modes}"
        )

    changeset_cfg = deploy_actions[1]["Configuration"]
    if (
        changeset_cfg.get("TemplateConfiguration")
        != "DeployConfig::template-configuration.json"
    ):
        raise AssertionError(
            "CreateReplaceChangeSet phải dùng TemplateConfiguration từ artifact DeployConfig."
        )

    raw_overrides = changeset_cfg.get("ParameterOverrides")
    if raw_overrides is None:
        overrides_bytes = 0
    else:
        resolved_overrides = _resolve_override_string(raw_overrides)
        overrides_bytes = len(resolved_overrides.encode("utf-8"))
        if overrides_bytes >= CODEPIPELINE_PARAMETER_OVERRIDES_MAX_BYTES:
            raise AssertionError(
                f"ParameterOverrides vượt giới hạn 1000 byte của AWS CodePipeline ({overrides_bytes} bytes)."
            )
        try:
            parsed_overrides = json.loads(
                resolved_overrides,
                parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)),
            )
        except ValueError as exc:
            raise AssertionError(
                f"ParameterOverrides không phải JSON hợp lệ: {exc}."
            ) from None
        if not isinstance(parsed_overrides, dict):
            raise AssertionError(
                "ParameterOverrides phải là JSON object ánh xạ tên tham số sang giá trị."
            )

    publish_spec = resources["PublishAssets"]["Properties"]["Source"]["BuildSpec"]
    if publish_spec != "deployment/publish-buildspec.yml":
        raise AssertionError("PublishAssets must use the staged publish buildspec.")
    env = resources["PublishAssets"]["Properties"]["Environment"][
        "EnvironmentVariables"
    ]
    if not any(item["Name"] == "PARAM_GeminiSecretArn" for item in env):
        raise AssertionError(
            "GeminiSecretArn must participate in the deployment configuration fingerprint."
        )
    no_echo_params = {
        name
        for name, spec in template.get("Parameters", {}).items()
        if spec.get("NoEcho") is True
    }
    for item in env:
        if item.get("Type", "PLAINTEXT") == "PLAINTEXT" and isinstance(
            item.get("Value"), dict
        ):
            ref_name = item["Value"].get("Ref")
            if ref_name in no_echo_params:
                raise AssertionError(
                    f"NoEcho parameter '{ref_name}' must not be exposed via PLAINTEXT CodeBuild environment variables."
                )

    return {
        "pipeline_template": str(pipeline_path.relative_to(ROOT))
        if pipeline_path.is_relative_to(ROOT)
        else str(pipeline_path),
        "stages": stage_names,
        "deploy_actions": [act["Name"] for act in deploy_actions],
        "parameter_overrides_bytes": overrides_bytes,
        "regions": build_env,
    }
