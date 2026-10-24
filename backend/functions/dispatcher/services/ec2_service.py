"""
Amazon EC2 Service Handler for SatiBot ChatOps.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)
"""

from datetime import datetime
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
    'list_instances',
    'get_instance',
    'manage_power',
    'terminate_instance',
    'get_metrics',
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
    """Placeholder for scenario 4: Safely terminate instance."""
    return _build_response(
        success=True,
        action="terminate_instance",
        data={},
        message="Hành động 'terminate_instance' đang trong quá trình phát triển."
    )


def _handle_get_metrics(credentials: dict, params: dict) -> dict:
    """Placeholder for scenario 5: Get CloudWatch CPU metrics and FinOps suggestions."""
    return _build_response(
        success=True,
        action="get_metrics",
        data={},
        message="Hành động 'get_metrics' đang trong quá trình phát triển."
    )


ACTION_HANDLERS = {
    'create_instance': _handle_create_instance,
    'list_instances': _handle_list_instances,
    'get_instance': _handle_get_instance,
    'manage_power': _handle_manage_power,
    'terminate_instance': _handle_terminate_instance,
}


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
        if action == 'get_metrics':
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
