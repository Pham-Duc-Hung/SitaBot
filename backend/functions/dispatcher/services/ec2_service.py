"""
Amazon EC2 Service Handler for SatiBot ChatOps.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)
"""

from datetime import datetime, timedelta, timezone
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Configuration Constants ---
DEFAULT_REGION = 'ap-southeast-2'

ALLOWED_INSTANCE_TYPES = ['t2.micro', 't3.micro', 't3.small']

# Default AMIs for supported operating systems in ap-southeast-2 (Sydney)
DEFAULT_AMIS = {
    'ubuntu': 'ami-0af9110f0ec0820e2',        # Ubuntu Server 22.04 LTS (HVM, SSD) in ap-southeast-2
    'amazon-linux': 'ami-0da639e071ea706d8',  # Amazon Linux 2023 AMI (x86_64) in ap-southeast-2
}

SUPPORTED_ACTIONS = {
    'create_instance',
    'create_ec2_instance',
    'list_instances',
    'list_ec2_instances',
    'get_instance',
    'get_ec2_instance',
    'get_ec2_instance_detail',
    'manage_power',
    'manage_ec2_power',
    'terminate_instance',
    'terminate_ec2_instance',
    'get_metrics',
    'get_ec2_metrics',
}


def _build_response(
    success: bool,
    action: str,
    data=None,
    message: str = "",
    error: str = None
) -> dict:
    """
    Standardize response dictionary format across all operations.

    :param success: Boolean indicating success or failure.
    :param action: Name of the action executed.
    :param data: Payload returned (dict or list).
    :param message: Human-readable message (Vietnamese preferred for bot responses).
    :param error: Error description if failed, else None.
    :return: Standardized response dictionary.
    """
    return {
        "success": success,
        "action": action,
        "data": data,
        "message": message,
        "error": error
    }


def _extract_name_tag(tags: list) -> str:
    """Extract 'Name' tag from an EC2 instance's tags list."""
    if not tags or not isinstance(tags, list):
        return ""
    for tag in tags:
        if isinstance(tag, dict) and tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _format_datetime(dt) -> str:
    """Convert datetime object to ISO 8601 string, or return as string/empty."""
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def get_ec2_client(credentials: dict):
    """
    Initialize a Boto3 EC2 client using STS temporary credentials.

    :param credentials: Dict containing AccessKeyId, SecretAccessKey, SessionToken, and optional Region.
    :return: Boto3 EC2 client instance.
    """
    creds = credentials or {}
    return boto3.client(
        'ec2',
        aws_access_key_id=creds.get('AccessKeyId'),
        aws_secret_access_key=creds.get('SecretAccessKey'),
        aws_session_token=creds.get('SessionToken'),
        region_name=creds.get('Region', DEFAULT_REGION)
    )


def get_cloudwatch_client(credentials: dict):
    """
    Initialize a Boto3 CloudWatch client using STS temporary credentials.

    :param credentials: Dict containing AccessKeyId, SecretAccessKey, SessionToken, and optional Region.
    :return: Boto3 CloudWatch client instance.
    """
    creds = credentials or {}
    return boto3.client(
        'cloudwatch',
        aws_access_key_id=creds.get('AccessKeyId'),
        aws_secret_access_key=creds.get('SecretAccessKey'),
        aws_session_token=creds.get('SessionToken'),
        region_name=creds.get('Region', DEFAULT_REGION)
    )


# --- Action Handlers ---

def _handle_create_instance(client, params: dict) -> dict:
    """
    Scenario 1: Create an EC2 instance with strict 4-step guardrails:
      - Step 1.1: Name check & idempotency (detect duplicate tag:Name)
      - Step 1.2: Cost Guardrail (FinOps whitelist)
      - Step 1.3: Run instances with default tags (Name, Project=SatiBot, AutoStop=True)
      - Step 1.4: Immediate response with pending state (non-blocking)
    """
    # Step 1.1: Name check & Idempotency
    instance_name = params.get("instance_name") or params.get("name")
    if not instance_name or not isinstance(instance_name, str) or not instance_name.strip():
        return _build_response(
            success=False,
            action="create_instance",
            data=None,
            message="Vui lòng cung cấp 'instance_name' để khởi tạo máy chủ.",
            error="Missing or invalid 'instance_name' parameter"
        )
    instance_name = instance_name.strip()

    # Check for existing active instances with the same tag:Name
    existing_check = client.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]}
        ]
    )
    existing_ids = []
    for reservation in existing_check.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            state_name = inst.get("State", {}).get("Name", "")
            if state_name != "terminated":
                existing_ids.append(inst.get("InstanceId"))

    force = params.get("force", False) is True
    if existing_ids and not force:
        return _build_response(
            success=False,
            action="create_instance",
            data={"existing_instances": existing_ids},
            message=(
                f"Đã tồn tại {len(existing_ids)} máy chủ mang tên '{instance_name}' "
                f"ở trạng thái hoạt động ({', '.join(existing_ids)}). "
                "Vui lòng chọn tên khác hoặc truyền 'force=True' để xác nhận tiếp tục tạo."
            ),
            error="DuplicateInstanceName"
        )

    # Step 1.2: Cost Guardrail (FinOps Whitelist)
    instance_type = params.get("instance_type", "t3.micro")
    if not instance_type or not isinstance(instance_type, str):
        instance_type = "t3.micro"
    instance_type = instance_type.strip()

    allow_large_instance = (
        params.get("confirm_cost") is True or
        params.get("override_guardrail") is True
    )
    if instance_type not in ALLOWED_INSTANCE_TYPES and not allow_large_instance:
        return _build_response(
            success=False,
            action="create_instance",
            data={
                "requested_type": instance_type,
                "allowed_types": ALLOWED_INSTANCE_TYPES,
            },
            message=(
                f"Cảnh báo FinOps: Loại máy chủ '{instance_type}' vượt ngoài danh mục "
                f"tiết kiệm chi phí được phép ({', '.join(ALLOWED_INSTANCE_TYPES)}). "
                "Vui lòng chọn loại máy phù hợp hoặc cung cấp cờ 'confirm_cost=True' để xác nhận dự toán chi phí."
            ),
            error="CostGuardrailViolation"
        )

    # Step 1.3: Determine AMI & launch instance
    image_id = params.get("image_id")
    if not image_id or not isinstance(image_id, str) or not image_id.strip():
        os_type = str(params.get("os_type", "ubuntu")).lower().strip()
        if "amazon" in os_type or "amzn" in os_type:
            image_id = DEFAULT_AMIS["amazon-linux"]
        else:
            image_id = DEFAULT_AMIS["ubuntu"]
    else:
        image_id = image_id.strip()

    tags = [
        {"Key": "Name", "Value": instance_name},
        {"Key": "Project", "Value": "SatiBot"},
        {"Key": "AutoStop", "Value": "True"},
    ]
    extra_tags = params.get("tags", [])
    if isinstance(extra_tags, list):
        reserved_keys = {"Name", "Project", "AutoStop"}
        for t in extra_tags:
            if isinstance(t, dict) and t.get("Key") not in reserved_keys:
                tags.append(t)

    run_kwargs = {
        "ImageId": image_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": tags
            }
        ]
    }

    key_name = params.get("key_name") or params.get("KeyName")
    if key_name and isinstance(key_name, str):
        run_kwargs["KeyName"] = key_name.strip()

    security_group_ids = params.get("security_group_ids") or params.get("SecurityGroupIds")
    if security_group_ids:
        if isinstance(security_group_ids, str):
            run_kwargs["SecurityGroupIds"] = [security_group_ids.strip()]
        elif isinstance(security_group_ids, list):
            run_kwargs["SecurityGroupIds"] = [str(sg).strip() for sg in security_group_ids]

    subnet_id = params.get("subnet_id") or params.get("SubnetId")
    if subnet_id and isinstance(subnet_id, str):
        run_kwargs["SubnetId"] = subnet_id.strip()

    run_response = client.run_instances(**run_kwargs)

    # Step 1.4: Immediate, non-blocking response
    created_instances = run_response.get("Instances", [])
    created = created_instances[0] if created_instances else {}
    created_id = created.get("InstanceId", "")
    state = created.get("State", {}).get("Name", "pending")

    return _build_response(
        success=True,
        action="create_instance",
        data={
            "instance_id": created_id,
            "instance_name": instance_name,
            "state": state,
            "instance_type": instance_type,
            "image_id": image_id,
        },
        message=f"Đã gửi yêu cầu khởi tạo máy chủ '{instance_name}' ({created_id}) thành công với trạng thái '{state}'."
    )


def _handle_list_instances(client, params: dict) -> dict:
    """
    Scenario 2a: List EC2 instances with optional state filtering.
    Extracts concise summary: ID, Name, State, Type, PublicIP, PrivateIP, LaunchTime.
    """
    describe_kwargs = {}
    state_filter = params.get('state') or params.get('state_filter')
    if state_filter:
        if isinstance(state_filter, str):
            state_values = [state_filter.strip()]
        elif isinstance(state_filter, (list, tuple, set)):
            state_values = [str(s).strip() for s in state_filter]
        else:
            state_values = [str(state_filter).strip()]

        describe_kwargs["Filters"] = [
            {"Name": "instance-state-name", "Values": state_values}
        ]

    response = client.describe_instances(**describe_kwargs)

    instances_list = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            instances_list.append({
                "instance_id": instance.get("InstanceId", ""),
                "name": _extract_name_tag(instance.get("Tags", [])),
                "state": instance.get("State", {}).get("Name", ""),
                "instance_type": instance.get("InstanceType", ""),
                "public_ip": instance.get("PublicIpAddress", ""),
                "private_ip": instance.get("PrivateIpAddress", ""),
                "launch_time": _format_datetime(instance.get("LaunchTime")),
            })

    if state_filter:
        msg = f"Tìm thấy {len(instances_list)} máy chủ EC2 (trạng thái: {', '.join(describe_kwargs['Filters'][0]['Values'])})."
    else:
        msg = f"Tìm thấy {len(instances_list)} máy chủ EC2."

    return _build_response(
        success=True,
        action="list_instances",
        data=instances_list,
        message=msg
    )


def _handle_get_instance(client, params: dict) -> dict:
    """
    Scenario 2b: Get detailed information for a specific EC2 instance by ID.
    Requires 'instance_id' in params.
    """
    instance_id = params.get("instance_id")
    if not instance_id or not isinstance(instance_id, str) or not instance_id.strip():
        return _build_response(
            success=False,
            action="get_instance",
            data=None,
            message="Vui lòng cung cấp 'instance_id' để tra cứu thông tin máy chủ.",
            error="Missing or invalid 'instance_id' parameter"
        )

    instance_id = instance_id.strip()
    response = client.describe_instances(InstanceIds=[instance_id])

    instances = []
    for res in response.get("Reservations", []):
        instances.extend(res.get("Instances", []))

    if not instances:
        return _build_response(
            success=False,
            action="get_instance",
            data=None,
            message=f"Không tìm thấy máy chủ với ID '{instance_id}'.",
            error=f"InstanceNotFound: {instance_id}"
        )

    inst = instances[0]
    security_groups = [
        {
            "group_id": sg.get("GroupId", ""),
            "group_name": sg.get("GroupName", ""),
        }
        for sg in inst.get("SecurityGroups", [])
    ]

    instance_data = {
        "instance_id": inst.get("InstanceId", instance_id),
        "name": _extract_name_tag(inst.get("Tags", [])),
        "state": inst.get("State", {}).get("Name", ""),
        "instance_type": inst.get("InstanceType", ""),
        "vpc_id": inst.get("VpcId", ""),
        "subnet_id": inst.get("SubnetId", ""),
        "security_groups": security_groups,
        "key_name": inst.get("KeyName", ""),
        "public_ip": inst.get("PublicIpAddress", ""),
        "private_ip": inst.get("PrivateIpAddress", ""),
        "launch_time": _format_datetime(inst.get("LaunchTime")),
    }

    return _build_response(
        success=True,
        action="get_instance",
        data=instance_data,
        message=f"Lấy thông tin chi tiết máy chủ '{instance_id}' thành công."
    )


def _handle_manage_power(client, params: dict) -> dict:
    """
    Scenario 3: Manage instance power lifecycle (start/stop/reboot).
    Enforces safe state transitions before invoking Boto3 APIs:
      - 'start': only allowed when current state is 'stopped'
      - 'stop': only allowed when current state is 'running'
      - 'reboot': only allowed when current state is 'running'
    """
    instance_id = params.get("instance_id")
    if not instance_id or not isinstance(instance_id, str) or not instance_id.strip():
        return _build_response(
            success=False,
            action="manage_power",
            data=None,
            message="Vui lòng cung cấp 'instance_id' để điều khiển nguồn máy chủ.",
            error="Missing or invalid 'instance_id' parameter"
        )
    instance_id = instance_id.strip()

    operation = params.get("operation")
    if not operation or not isinstance(operation, str):
        return _build_response(
            success=False,
            action="manage_power",
            data=None,
            message="Vui lòng cung cấp thao tác nguồn 'operation' ('start', 'stop', 'reboot').",
            error="Missing or invalid 'operation' parameter"
        )
    operation = operation.lower().strip()

    valid_operations = {"start", "stop", "reboot"}
    if operation not in valid_operations:
        return _build_response(
            success=False,
            action="manage_power",
            data={"allowed_operations": list(valid_operations)},
            message=f"Thao tác nguồn '{operation}' không hợp lệ. Chỉ hỗ trợ: start, stop, reboot.",
            error=f"InvalidOperation: '{operation}'"
        )

    # Pre-check current instance state using describe_instances
    describe_resp = client.describe_instances(InstanceIds=[instance_id])
    instances = []
    for res in describe_resp.get("Reservations", []):
        instances.extend(res.get("Instances", []))

    if not instances:
        return _build_response(
            success=False,
            action="manage_power",
            data=None,
            message=f"Không tìm thấy máy chủ với ID '{instance_id}'.",
            error=f"InstanceNotFound: {instance_id}"
        )

    current_state = instances[0].get("State", {}).get("Name", "").lower()

    # Validate state transition rules
    if operation == "start":
        if current_state != "stopped":
            return _build_response(
                success=False,
                action="manage_power",
                data={"instance_id": instance_id, "current_state": current_state},
                message=f"Không thể bật máy chủ '{instance_id}' vì máy đang ở trạng thái '{current_state}' (chỉ có thể bật khi máy đang 'stopped').",
                error=f"InvalidStateTransition: Cannot start instance in '{current_state}' state"
            )
        resp = client.start_instances(InstanceIds=[instance_id])
        starting = resp.get("StartingInstances", [{}])[0] if resp.get("StartingInstances") else {}
        prev_state = starting.get("PreviousState", {}).get("Name", current_state)
        new_state = starting.get("CurrentState", {}).get("Name", "pending")
        msg = f"Đã gửi lệnh bật máy chủ '{instance_id}' thành công (trạng thái: {prev_state} -> {new_state})."

    elif operation == "stop":
        if current_state != "running":
            return _build_response(
                success=False,
                action="manage_power",
                data={"instance_id": instance_id, "current_state": current_state},
                message=f"Không thể tắt máy chủ '{instance_id}' vì máy đang ở trạng thái '{current_state}' (chỉ có thể tắt khi máy đang 'running').",
                error=f"InvalidStateTransition: Cannot stop instance in '{current_state}' state"
            )
        resp = client.stop_instances(InstanceIds=[instance_id])
        stopping = resp.get("StoppingInstances", [{}])[0] if resp.get("StoppingInstances") else {}
        prev_state = stopping.get("PreviousState", {}).get("Name", current_state)
        new_state = stopping.get("CurrentState", {}).get("Name", "stopping")
        msg = f"Đã gửi lệnh tắt máy chủ '{instance_id}' thành công (trạng thái: {prev_state} -> {new_state})."

    else:  # reboot
        if current_state != "running":
            return _build_response(
                success=False,
                action="manage_power",
                data={"instance_id": instance_id, "current_state": current_state},
                message=f"Không thể khởi động lại máy chủ '{instance_id}' vì máy đang ở trạng thái '{current_state}' (chỉ có thể khởi động lại khi máy đang 'running').",
                error=f"InvalidStateTransition: Cannot reboot instance in '{current_state}' state"
            )
        client.reboot_instances(InstanceIds=[instance_id])
        prev_state = current_state
        new_state = "rebooting"
        msg = f"Đã gửi lệnh khởi động lại (reboot) máy chủ '{instance_id}' thành công."

    return _build_response(
        success=True,
        action="manage_power",
        data={
            "instance_id": instance_id,
            "operation": operation,
            "previous_state": prev_state,
            "current_state": new_state,
        },
        message=msg
    )


def _handle_terminate_instance(client, params: dict) -> dict:
    """
    Scenario 4: Safely terminate an EC2 instance:
      - Validates instance_id.
      - Checks current state (reject if already terminated).
      - Checks Termination Protection (DisableApiTermination attribute).
      - Requires confirmation (confirm=True / confirmed=True) before calling terminate_instances.
    """
    instance_id = params.get("instance_id") or params.get("id")
    if not instance_id or not isinstance(instance_id, str) or not instance_id.strip():
        return _build_response(
            success=False,
            action="terminate_instance",
            data=None,
            message="Vui lòng cung cấp 'instance_id' để thực hiện thao tác xóa máy chủ.",
            error="Missing or invalid 'instance_id' parameter"
        )
    instance_id = instance_id.strip()

    # Pre-check instance existence and current status
    describe_resp = client.describe_instances(InstanceIds=[instance_id])
    instances = []
    for res in describe_resp.get("Reservations", []):
        instances.extend(res.get("Instances", []))

    if not instances:
        return _build_response(
            success=False,
            action="terminate_instance",
            data=None,
            message=f"Không tìm thấy máy chủ với ID '{instance_id}'.",
            error=f"InstanceNotFound: {instance_id}"
        )

    instance = instances[0]
    current_state = instance.get("State", {}).get("Name", "").lower()
    instance_name = _extract_name_tag(instance.get("Tags", []))

    if current_state == "terminated":
        return _build_response(
            success=False,
            action="terminate_instance",
            data={"instance_id": instance_id, "current_state": "terminated"},
            message=f"Máy chủ '{instance_id}' đã ở trạng thái đã xóa ('terminated') trước đó.",
            error="InstanceAlreadyTerminated"
        )

    # Check Termination Protection
    attr_resp = client.describe_instance_attribute(
        InstanceId=instance_id,
        Attribute="disableApiTermination"
    )
    is_protected = attr_resp.get("DisableApiTermination", {}).get("Value", False)
    if is_protected:
        return _build_response(
            success=False,
            action="terminate_instance",
            data={
                "instance_id": instance_id,
                "instance_name": instance_name,
                "current_state": current_state,
                "disable_api_termination": True
            },
            message=(
                f"Không thể xóa máy chủ '{instance_id}' do đang bật tính năng bảo vệ chống xóa "
                "(Termination Protection - DisableApiTermination=True). "
                "Vui lòng tắt tính năng bảo vệ này trước khi thực hiện xóa."
            ),
            error="TerminationProtectionEnabled"
        )

    # Human-in-the-loop Guardrail: Require explicit confirmation
    is_confirmed = params.get("confirm") is True or params.get("confirmed") is True
    if not is_confirmed:
        name_str = f" ('{instance_name}')" if instance_name else ""
        warning_msg = (
            f"Cảnh báo rủi ro cao: Thao tác xóa máy chủ '{instance_id}'{name_str} "
            f"đang ở trạng thái '{current_state}' sẽ xóa vĩnh viễn máy chủ và giải phóng toàn bộ dữ liệu ổ đĩa gốc (EBS Root Volume). "
            "Hành động này KHÔNG THỂ HOÀN TÁC. Vui lòng gửi kèm 'confirm=True' để xác nhận thực hiện."
        )
        return _build_response(
            success=False,
            action="terminate_instance",
            data={
                "instance_id": instance_id,
                "instance_name": instance_name,
                "current_state": current_state,
                "confirmation_required": True
            },
            message=warning_msg,
            error="ConfirmationRequired"
        )

    # Execute termination via Boto3
    term_resp = client.terminate_instances(InstanceIds=[instance_id])
    terminating = term_resp.get("TerminatingInstances", [{}])[0] if term_resp.get("TerminatingInstances") else {}
    prev_state = terminating.get("PreviousState", {}).get("Name", current_state)
    new_state = terminating.get("CurrentState", {}).get("Name", "shutting-down")

    name_str = f" '{instance_name}'" if instance_name else ""
    return _build_response(
        success=True,
        action="terminate_instance",
        data={
            "instance_id": instance_id,
            "instance_name": instance_name,
            "previous_state": prev_state,
            "current_state": new_state,
        },
        message=f"Đã gửi yêu cầu xóa vĩnh viễn máy chủ{name_str} ({instance_id}) thành công với trạng thái '{new_state}'."
    )


def _handle_get_metrics(credentials: dict, params: dict) -> dict:
    """
    Scenario 5: Retrieve Amazon CloudWatch CPUUtilization metrics for the instance
    over a specified period (default 24h) and provide intelligent FinOps recommendations.
      - Idle waste (< 5% avg CPU): Recommend stopping the instance.
      - Overloaded (> 80% avg CPU): Recommend upgrading instance type or auto-scaling.
      - Optimal: Operating within normal cost-effective parameters.
    """
    instance_id = params.get("instance_id") or params.get("id")
    if not instance_id or not isinstance(instance_id, str) or not instance_id.strip():
        return _build_response(
            success=False,
            action="get_metrics",
            data=None,
            message="Vui lòng cung cấp 'instance_id' để kiểm tra số liệu hiệu năng.",
            error="Missing or invalid 'instance_id' parameter"
        )
    instance_id = instance_id.strip()

    period_hours = params.get("period_hours", 24)
    if not isinstance(period_hours, int) or period_hours < 1:
        period_hours = 24
    if period_hours > 168:
        period_hours = 168

    # Query EC2 to fetch instance metadata
    ec2_client = get_ec2_client(credentials)
    describe_resp = ec2_client.describe_instances(InstanceIds=[instance_id])
    instances = []
    for res in describe_resp.get("Reservations", []):
        instances.extend(res.get("Instances", []))

    if not instances:
        return _build_response(
            success=False,
            action="get_metrics",
            data=None,
            message=f"Không tìm thấy máy chủ với ID '{instance_id}'.",
            error=f"InstanceNotFound: {instance_id}"
        )

    instance = instances[0]
    current_state = instance.get("State", {}).get("Name", "").lower()
    instance_name = _extract_name_tag(instance.get("Tags", []))
    instance_type = instance.get("InstanceType", "unknown")

    # Query CloudWatch for CPU metrics
    cw_client = get_cloudwatch_client(credentials)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=period_hours)

    metric_resp = cw_client.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=3600,
        Statistics=["Average", "Maximum"]
    )

    datapoints = metric_resp.get("Datapoints", [])

    if not datapoints:
        avg_cpu = 0.0
        max_cpu = 0.0
        rec_type = "no_data"
        suggestion = (
            f"Máy chủ '{instance_id}' (loại {instance_type}, trạng thái: {current_state}) chưa ghi nhận "
            f"số liệu CPU trong {period_hours} giờ qua. Có thể máy mới khởi tạo hoặc đang ở trạng thái tắt (stopped)."
        )
    else:
        avg_cpu = sum(d.get("Average", 0.0) for d in datapoints) / len(datapoints)
        max_cpu = max(d.get("Maximum", d.get("Average", 0.0)) for d in datapoints)

        if avg_cpu < 5.0:
            rec_type = "idle_waste"
            suggestion = (
                f"Cảnh báo lãng phí FinOps: Mức sử dụng CPU trung bình ({avg_cpu:.1f}%) "
                f"rất thấp (< 5%) trong {period_hours} giờ qua. Máy chủ đang nhàn rỗi (Idle). "
                f"Khuyến nghị: Tắt máy chủ ('manage_power' với operation='stop') khi không sử dụng để tiết kiệm chi phí."
            )
        elif avg_cpu > 80.0:
            rec_type = "overloaded"
            suggestion = (
                f"Cảnh báo hiệu năng: Mức sử dụng CPU trung bình ({avg_cpu:.1f}%) "
                f"vượt ngưỡng 80% (đỉnh đạt {max_cpu:.1f}%). Máy chủ có nguy cơ nghẽn tải. "
                f"Khuyến nghị: Cân nhắc nâng cấp cấu hình máy chủ lên loại cao hơn hoặc cấu hình Auto Scaling."
            )
        else:
            rec_type = "optimal"
            suggestion = (
                f"Hiệu năng tối ưu: Mức sử dụng CPU trung bình {avg_cpu:.1f}% "
                f"(đỉnh {max_cpu:.1f}%) trong {period_hours} giờ qua nằm trong ngưỡng vận hành an toàn và tiết kiệm."
            )

    return _build_response(
        success=True,
        action="get_metrics",
        data={
            "instance_id": instance_id,
            "instance_name": instance_name,
            "instance_type": instance_type,
            "current_state": current_state,
            "period_hours": period_hours,
            "datapoints_count": len(datapoints),
            "average_cpu": round(avg_cpu, 2),
            "max_cpu": round(max_cpu, 2),
            "recommendation_type": rec_type,
            "recommendation": suggestion,
        },
        message=suggestion
    )


ACTION_HANDLERS = {
    'create_instance': _handle_create_instance,
    'create_ec2_instance': _handle_create_instance,
    'list_instances': _handle_list_instances,
    'list_ec2_instances': _handle_list_instances,
    'get_instance': _handle_get_instance,
    'get_ec2_instance': _handle_get_instance,
    'get_ec2_instance_detail': _handle_get_instance,
    'manage_power': _handle_manage_power,
    'manage_ec2_power': _handle_manage_power,
    'terminate_instance': _handle_terminate_instance,
    'terminate_ec2_instance': _handle_terminate_instance,
    'get_metrics': _handle_get_metrics,
    'get_ec2_metrics': _handle_get_metrics,
}

# --- Tool Schemas for LLM Function Calling (ChatOps AI Agent) ---
EC2_TOOL_SCHEMAS = [
    {
        "name": "create_ec2_instance",
        "service": "ec2",
        "description": "Khởi tạo một máy chủ ảo Amazon EC2 mới chuẩn hóa với cấu hình tiết kiệm FinOps (t2.micro/t3.micro), tự động gắn thẻ quản lý và bảo vệ trùng lặp tên.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instance_name": {
                    "type": "string",
                    "description": "Tên định danh của máy chủ ảo (gắn vào tag Name). Ví dụ: 'web-demo', 'staging-server'."
                },
                "os_type": {
                    "type": "string",
                    "enum": ["ubuntu", "amazon-linux"],
                    "description": "Hệ điều hành máy chủ: 'ubuntu' (Ubuntu 22.04 LTS) hoặc 'amazon-linux' (Amazon Linux 2023). Mặc định: 'ubuntu'."
                },
                "instance_type": {
                    "type": "string",
                    "enum": ["t2.micro", "t3.micro", "t3.small"],
                    "description": "Loại cấu hình phần cứng theo chuẩn FinOps. Mặc định: 't3.micro'."
                },
                "key_name": {
                    "type": "string",
                    "description": "Tên khóa SSH Key Pair đã có trên AWS để kết nối vào máy chủ."
                },
                "force": {
                    "type": "boolean",
                    "description": "Bỏ qua cảnh báo trùng tên và tiếp tục khởi tạo nếu đã tồn tại máy chủ cùng tên."
                }
            },
            "required": ["instance_name"]
        }
    },
    {
        "name": "list_ec2_instances",
        "service": "ec2",
        "description": "Liệt kê danh sách các máy chủ EC2 trong tài khoản AWS, hỗ trợ lọc theo trạng thái hoạt động (running, stopped, all).",
        "requires_confirmation": False,
        "parameters": {
            "type": "object",
            "properties": {
                "state_filter": {
                    "type": "string",
                    "enum": ["all", "running", "stopped", "pending", "shutting-down", "terminated"],
                    "description": "Lọc theo trạng thái máy chủ (mặc định 'all' để xem tất cả)."
                }
            }
        }
    },
    {
        "name": "get_ec2_instance_detail",
        "service": "ec2",
        "description": "Xem thông tin cấu hình chi tiết của một máy chủ EC2 cụ thể theo Instance ID (VPC, Subnet, IP công khai/nội bộ, Security Groups, thời gian chạy).",
        "requires_confirmation": False,
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Mã định danh duy nhất của máy chủ EC2 (ví dụ: 'i-015aad95042137744')."
                }
            },
            "required": ["instance_id"]
        }
    },
    {
        "name": "manage_ec2_power",
        "service": "ec2",
        "description": "Điều khiển trạng thái nguồn của máy chủ EC2 (Bật - 'start', Tắt - 'stop', hoặc Khởi động lại - 'reboot') kèm kiểm tra tiền điều kiện an toàn.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Mã định danh duy nhất của máy chủ EC2 cần điều khiển (ví dụ: 'i-015aad95042137744')."
                },
                "operation": {
                    "type": "string",
                    "enum": ["start", "stop", "reboot"],
                    "description": "Thao tác nguồn muốn thực hiện: 'start' (bật máy khi đang stopped), 'stop' (tắt máy khi đang running), 'reboot' (khởi động lại khi đang running)."
                }
            },
            "required": ["instance_id", "operation"]
        }
    },
    {
        "name": "terminate_ec2_instance",
        "service": "ec2",
        "description": "Xóa vĩnh viễn một máy chủ EC2 và giải phóng tài nguyên ổ đĩa lưu trữ. Thao tác có rủi ro cao, bắt buộc xác nhận an toàn.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Mã định danh duy nhất của máy chủ EC2 cần xóa vĩnh viễn."
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Cờ xác nhận đồng ý xóa máy chủ vĩnh viễn (bắt buộc True để thực thi)."
                }
            },
            "required": ["instance_id"]
        }
    },
    {
        "name": "get_ec2_metrics",
        "service": "ec2",
        "description": "Truy vấn số liệu giám sát CPU qua Amazon CloudWatch trong 24 giờ qua của máy chủ EC2 và đưa ra khuyến nghị tối ưu chi phí (FinOps).",
        "requires_confirmation": False,
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Mã định danh duy nhất của máy chủ EC2 cần xem báo cáo hiệu năng."
                },
                "period_hours": {
                    "type": "integer",
                    "description": "Khoảng thời gian thống kê (tính bằng giờ, mặc định 24, tối đa 168 giờ)."
                }
            },
            "required": ["instance_id"]
        }
    }
]


def execute(credentials: dict, params: dict) -> dict:
    """
    Entrypoint tiếp nhận yêu cầu từ Dispatcher Lambda.

    :param credentials: Dict chứa STS Temporary Credentials:
           {
               "AccessKeyId": str,
               "SecretAccessKey": str,
               "SessionToken": str,
               "Region": str (mặc định 'ap-southeast-2' nếu thiếu)
           }
    :param params: Dict chứa action và các tham số chi tiết từ LLM Function Calling:
           {
               "action": str,  # create_instance, list_instances, get_instance, manage_power, terminate_instance, get_metrics
               ...
           }
    :return: Dict chuẩn hóa:
           {
               "success": bool,
               "action": str,
               "data": dict or list,
               "message": str,
               "error": str or None
           }
    """
    if not isinstance(params, dict) or not params.get('action'):
        return _build_response(
            success=False,
            action="",
            data=None,
            message="Yêu cầu không hợp lệ: thiếu tham số 'action'.",
            error="Missing or invalid action parameter"
        )

    action = params.get('action')

    if action not in SUPPORTED_ACTIONS:
        return _build_response(
            success=False,
            action=action,
            data=None,
            message=f"Hành động '{action}' không được hỗ trợ.",
            error=f"Unsupported action: '{action}'"
        )

    try:
        if action in ('get_metrics', 'get_ec2_metrics'):
            return _handle_get_metrics(credentials, params)

        client = get_ec2_client(credentials)
        handler = ACTION_HANDLERS.get(action)
        return handler(client, params)

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'ClientError')
        error_msg = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"AWS ClientError during action '{action}': {error_code} - {error_msg}")

        if error_code == 'InvalidInstanceID.NotFound':
            user_msg = f"Không tìm thấy máy chủ với ID '{params.get('instance_id', '')}' trên AWS."
        else:
            user_msg = f"Lỗi AWS EC2 ({error_code}): {error_msg}"

        return _build_response(
            success=False,
            action=action,
            data=None,
            message=user_msg,
            error=f"{error_code}: {error_msg}"
        )
    except Exception as e:
        logger.exception(f"Unexpected error during action '{action}': {str(e)}")
        return _build_response(
            success=False,
            action=action,
            data=None,
            message="Đã xảy ra lỗi nội bộ khi xử lý yêu cầu EC2.",
            error=str(e)
        )
