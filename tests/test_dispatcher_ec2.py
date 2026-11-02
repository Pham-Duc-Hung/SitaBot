"""
Unit tests for Dispatcher EC2 Service (Step 1 & Step 2).
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)
"""

from datetime import datetime, timezone
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from botocore.exceptions import ClientError

# Ensure modules in backend/functions/dispatcher can be imported
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
dispatcher_path = project_root / "backend" / "functions" / "dispatcher"

for path in [str(dispatcher_path), str(project_root)]:
    if path not in sys.path:
        sys.path.insert(0, path)

from services.ec2_service import (
    ALLOWED_INSTANCE_TYPES,
    DEFAULT_AMIS,
    DEFAULT_REGION,
    SUPPORTED_ACTIONS,
    EC2_TOOL_SCHEMAS,
    _build_response,
    _extract_name_tag,
    _format_datetime,
    execute,
    get_ec2_client,
    get_cloudwatch_client,
)


# --- Pytest Fixtures ---

@pytest.fixture
def mock_credentials():
    """Sample STS temporary credentials."""
    return {
        "AccessKeyId": "ASIA_MOCK_ACCESS_KEY",
        "SecretAccessKey": "MOCK_SECRET_KEY",
        "SessionToken": "MOCK_SESSION_TOKEN",
        "Region": "ap-southeast-2",
    }


@pytest.fixture
def mock_boto_ec2():
    """Mock boto3.client for ec2 service."""
    with patch("services.ec2_service.boto3.client") as mock:
        mock_client = MagicMock()
        mock.return_value = mock_client
        yield mock


@pytest.fixture
def sample_ec2_instances_raw():
    """Sample raw EC2 describe_instances API response."""
    return {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-01111111111111111",
                        "InstanceType": "t3.micro",
                        "State": {"Name": "running"},
                        "PublicIpAddress": "54.79.10.20",
                        "PrivateIpAddress": "172.31.1.10",
                        "LaunchTime": datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc),
                        "VpcId": "vpc-0aaaa1111",
                        "SubnetId": "subnet-0bbbb1111",
                        "KeyName": "satibot-key",
                        "Tags": [
                            {"Key": "Environment", "Value": "Production"},
                            {"Key": "Name", "Value": "Web-Production"},
                        ],
                        "SecurityGroups": [
                            {"GroupId": "sg-0123456789abcdef0", "GroupName": "web-traffic-sg"},
                            {"GroupId": "sg-0123456789abcdef1", "GroupName": "ssh-access-sg"},
                        ],
                    },
                    {
                        "InstanceId": "i-02222222222222222",
                        "InstanceType": "t2.micro",
                        "State": {"Name": "stopped"},
                        # No PublicIpAddress, no Name tag
                        "PrivateIpAddress": "172.31.2.20",
                        "LaunchTime": datetime(2026, 10, 20, 8, 30, 0, tzinfo=timezone.utc),
                        "VpcId": "vpc-0aaaa1111",
                        "SubnetId": "subnet-0bbbb2222",
                        "KeyName": "dev-key",
                        "Tags": [{"Key": "Team", "Value": "DevOps"}],
                        "SecurityGroups": [
                            {"GroupId": "sg-0123456789abcdef2", "GroupName": "internal-sg"}
                        ],
                    },
                ]
            }
        ]
    }


# --- Tests for Configuration Constants & Helpers ---

def test_constants_configuration():
    """Verify fundamental configuration constants are correctly defined."""
    assert DEFAULT_REGION == "ap-southeast-2"
    assert "t2.micro" in ALLOWED_INSTANCE_TYPES
    assert "t3.micro" in ALLOWED_INSTANCE_TYPES
    assert "t3.small" in ALLOWED_INSTANCE_TYPES

    assert "ubuntu" in DEFAULT_AMIS
    assert DEFAULT_AMIS["ubuntu"].startswith("ami-")
    assert "amazon-linux" in DEFAULT_AMIS
    assert DEFAULT_AMIS["amazon-linux"].startswith("ami-")


def test_build_response_structure():
    """Verify _build_response produces standardized dictionary format."""
    res = _build_response(
        success=True,
        action="list_instances",
        data=[{"id": "i-123"}],
        message="Thành công",
        error=None,
    )
    assert res == {
        "success": True,
        "action": "list_instances",
        "data": [{"id": "i-123"}],
        "message": "Thành công",
        "error": None,
    }

    res_default = _build_response(success=False, action="get_instance")
    assert res_default["success"] is False
    assert res_default["action"] == "get_instance"
    assert res_default["data"] is None
    assert res_default["message"] == ""
    assert res_default["error"] is None


def test_extract_name_tag_and_format_datetime():
    """Verify helper functions _extract_name_tag and _format_datetime."""
    tags_with_name = [{"Key": "Env", "Value": "Dev"}, {"Key": "Name", "Value": "AppServer"}]
    assert _extract_name_tag(tags_with_name) == "AppServer"
    assert _extract_name_tag([{"Key": "Owner", "Value": "Huy"}]) == ""
    assert _extract_name_tag([]) == ""
    assert _extract_name_tag(None) == ""

    dt = datetime(2026, 10, 22, 12, 30, 0, tzinfo=timezone.utc)
    assert _format_datetime(dt) == dt.isoformat()
    assert _format_datetime(None) == ""
    assert _format_datetime("already_string") == "already_string"


# --- Tests for get_ec2_client ---

def test_get_ec2_client_with_full_credentials(mock_credentials, mock_boto_ec2):
    """Verify get_ec2_client forwards credentials and region accurately."""
    client = get_ec2_client(mock_credentials)
    assert client is not None

    mock_boto_ec2.assert_called_once_with(
        "ec2",
        aws_access_key_id="ASIA_MOCK_ACCESS_KEY",
        aws_secret_access_key="MOCK_SECRET_KEY",
        aws_session_token="MOCK_SESSION_TOKEN",
        region_name="ap-southeast-2",
    )


def test_get_ec2_client_fallback_to_default_region(mock_boto_ec2):
    """Verify get_ec2_client uses DEFAULT_REGION when Region is not provided."""
    creds_no_region = {
        "AccessKeyId": "ASIA_KEY",
        "SecretAccessKey": "SECRET_KEY",
        "SessionToken": "TOKEN",
    }
    get_ec2_client(creds_no_region)

    mock_boto_ec2.assert_called_once_with(
        "ec2",
        aws_access_key_id="ASIA_KEY",
        aws_secret_access_key="SECRET_KEY",
        aws_session_token="TOKEN",
        region_name="ap-southeast-2",
    )


def test_get_ec2_client_with_empty_or_none_credentials(mock_boto_ec2):
    """Verify get_ec2_client gracefully handles None or empty dict."""
    get_ec2_client(None)
    mock_boto_ec2.assert_called_with(
        "ec2",
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        region_name="ap-southeast-2",
    )


# --- Tests for execute Entrypoint Validation ---

@pytest.mark.parametrize("invalid_params", [
    None,
    {},
    [],
    "action=create_instance",
    {"instance_type": "t3.micro"},  # Missing action
])
def test_execute_missing_or_invalid_action(mock_credentials, invalid_params):
    """Verify execute rejects requests with missing or invalid action parameter."""
    res = execute(mock_credentials, invalid_params)
    assert res["success"] is False
    assert res["action"] == ""
    assert res["data"] is None
    assert "action" in res["error"].lower()
    assert "thiếu tham số" in res["message"].lower()


def test_execute_unsupported_action(mock_credentials):
    """Verify execute rejects unsupported actions."""
    res = execute(mock_credentials, {"action": "delete_whole_vpc"})
    assert res["success"] is False
    assert res["action"] == "delete_whole_vpc"
    assert res["data"] is None
    assert "Unsupported action" in res["error"]
    assert "không được hỗ trợ" in res["message"]


# --- Tests for Error Handling (ClientError & Generic Exception) ---

def test_execute_catches_client_error_unauthorized(mock_credentials):
    """Verify execute catches ClientError (UnauthorizedOperation / AccessDenied) safely."""
    error_response = {
        "Error": {
            "Code": "UnauthorizedOperation",
            "Message": "You are not authorized to perform this operation.",
        }
    }
    client_err = ClientError(error_response, "RunInstances")

    with patch("services.ec2_service.get_ec2_client", side_effect=client_err):
        res = execute(mock_credentials, {"action": "create_instance"})

        assert res["success"] is False
        assert res["action"] == "create_instance"
        assert res["data"] is None
        assert "UnauthorizedOperation" in res["error"]
        assert "You are not authorized" in res["error"]
        assert "Lỗi AWS EC2 (UnauthorizedOperation)" in res["message"]


def test_execute_catches_client_error_auth_failure(mock_credentials):
    """Verify execute catches ClientError (AuthFailure) when credentials expire."""
    error_response = {
        "Error": {
            "Code": "AuthFailure",
            "Message": "AWS was not able to validate the provided access credentials.",
        }
    }
    client_err = ClientError(error_response, "DescribeInstances")

    with patch("services.ec2_service.get_ec2_client", side_effect=client_err):
        res = execute(mock_credentials, {"action": "list_instances"})

        assert res["success"] is False
        assert res["action"] == "list_instances"
        assert res["data"] is None
        assert "AuthFailure" in res["error"]
        assert "Lỗi AWS EC2 (AuthFailure)" in res["message"]


def test_execute_catches_generic_exception(mock_credentials):
    """Verify execute catches unexpected Exception without crashing Lambda."""
    with patch("services.ec2_service.get_ec2_client", side_effect=RuntimeError("Connection timeout")):
        res = execute(mock_credentials, {"action": "get_instance", "instance_id": "i-123"})

        assert res["success"] is False
        assert res["action"] == "get_instance"
        assert res["data"] is None
        assert "Connection timeout" in res["error"]
        assert "lỗi nội bộ" in res["message"].lower()


# --- Tests for Scenario 2: list_instances ---

def test_list_instances_no_filter(mock_credentials, mock_boto_ec2, sample_ec2_instances_raw):
    """Verify list_instances without filters returns all instances with concise attributes."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = sample_ec2_instances_raw

    res = execute(mock_credentials, {"action": "list_instances"})

    mock_client.describe_instances.assert_called_once_with()
    assert res["success"] is True
    assert res["action"] == "list_instances"
    assert res["error"] is None
    assert len(res["data"]) == 2
    assert "Tìm thấy 2 máy chủ EC2" in res["message"]

    inst1 = res["data"][0]
    assert inst1["instance_id"] == "i-01111111111111111"
    assert inst1["name"] == "Web-Production"
    assert inst1["state"] == "running"
    assert inst1["instance_type"] == "t3.micro"
    assert inst1["public_ip"] == "54.79.10.20"
    assert inst1["private_ip"] == "172.31.1.10"
    assert "2026-10-22" in inst1["launch_time"]

    inst2 = res["data"][1]
    assert inst2["instance_id"] == "i-02222222222222222"
    assert inst2["name"] == ""  # No Name tag
    assert inst2["state"] == "stopped"
    assert inst2["public_ip"] == ""  # No public IP


def test_list_instances_with_state_filter_string(mock_credentials, mock_boto_ec2, sample_ec2_instances_raw):
    """Verify list_instances with state string parameter sets appropriate filter."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = sample_ec2_instances_raw

    res = execute(mock_credentials, {"action": "list_instances", "state": "running"})

    mock_client.describe_instances.assert_called_once_with(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    assert res["success"] is True
    assert "trạng thái: running" in res["message"]


def test_list_instances_with_state_filter_list(mock_credentials, mock_boto_ec2, sample_ec2_instances_raw):
    """Verify list_instances with state_filter list parameter."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = sample_ec2_instances_raw

    res = execute(mock_credentials, {"action": "list_instances", "state_filter": ["running", "stopped"]})

    mock_client.describe_instances.assert_called_once_with(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
    )
    assert res["success"] is True


def test_list_instances_empty(mock_credentials, mock_boto_ec2):
    """Verify list_instances when no instances exist."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}

    res = execute(mock_credentials, {"action": "list_instances"})

    assert res["success"] is True
    assert res["data"] == []
    assert "Tìm thấy 0 máy chủ EC2" in res["message"]


# --- Tests for Scenario 2: get_instance ---

def test_get_instance_success(mock_credentials, mock_boto_ec2, sample_ec2_instances_raw):
    """Verify get_instance returns detailed instance parameters."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = sample_ec2_instances_raw

    res = execute(
        mock_credentials,
        {"action": "get_instance", "instance_id": "i-01111111111111111"}
    )

    mock_client.describe_instances.assert_called_once_with(
        InstanceIds=["i-01111111111111111"]
    )
    assert res["success"] is True
    assert res["action"] == "get_instance"
    assert res["error"] is None

    data = res["data"]
    assert data["instance_id"] == "i-01111111111111111"
    assert data["name"] == "Web-Production"
    assert data["state"] == "running"
    assert data["instance_type"] == "t3.micro"
    assert data["vpc_id"] == "vpc-0aaaa1111"
    assert data["subnet_id"] == "subnet-0bbbb1111"
    assert data["key_name"] == "satibot-key"
    assert data["public_ip"] == "54.79.10.20"
    assert data["private_ip"] == "172.31.1.10"
    assert len(data["security_groups"]) == 2
    assert data["security_groups"][0]["group_id"] == "sg-0123456789abcdef0"
    assert data["security_groups"][0]["group_name"] == "web-traffic-sg"
    assert "2026-10-22" in data["launch_time"]


@pytest.mark.parametrize("missing_id_param", [
    {"action": "get_instance"},
    {"action": "get_instance", "instance_id": ""},
    {"action": "get_instance", "instance_id": "   "},
    {"action": "get_instance", "instance_id": None},
])
def test_get_instance_missing_instance_id(mock_credentials, missing_id_param):
    """Verify get_instance fails with clear error when instance_id is omitted or empty."""
    res = execute(mock_credentials, missing_id_param)

    assert res["success"] is False
    assert res["action"] == "get_instance"
    assert res["data"] is None
    assert "Missing or invalid 'instance_id'" in res["error"]
    assert "Vui lòng cung cấp 'instance_id'" in res["message"]


def test_get_instance_not_found_client_error(mock_credentials, mock_boto_ec2):
    """Verify get_instance handles InvalidInstanceID.NotFound from AWS gracefully."""
    mock_client = mock_boto_ec2.return_value
    error_response = {
        "Error": {
            "Code": "InvalidInstanceID.NotFound",
            "Message": "The instance ID 'i-nonexistent' does not exist",
        }
    }
    mock_client.describe_instances.side_effect = ClientError(error_response, "DescribeInstances")

    res = execute(
        mock_credentials,
        {"action": "get_instance", "instance_id": "i-nonexistent"}
    )

    assert res["success"] is False
    assert res["action"] == "get_instance"
    assert res["data"] is None
    assert "InvalidInstanceID.NotFound" in res["error"]
    assert "Không tìm thấy máy chủ với ID 'i-nonexistent' trên AWS" in res["message"]


def test_get_instance_empty_reservations_handled(mock_credentials, mock_boto_ec2):
    """Verify get_instance handles case when API returns empty Reservations without raising error."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}

    res = execute(
        mock_credentials,
        {"action": "get_instance", "instance_id": "i-empty123"}
    )

    assert res["success"] is False
    assert res["action"] == "get_instance"
    assert res["data"] is None
    assert "InstanceNotFound: i-empty123" in res["error"]
    assert "Không tìm thấy máy chủ với ID 'i-empty123'" in res["message"]


# --- Tests for Scenario 1: create_instance ---

def test_create_instance_default_success(mock_credentials, mock_boto_ec2):
    """Verify create_instance with default settings creates instance with correct tags and pending state."""
    mock_client = mock_boto_ec2.return_value
    # No existing instance with this name
    mock_client.describe_instances.return_value = {"Reservations": []}
    mock_client.run_instances.return_value = {
        "Instances": [
            {
                "InstanceId": "i-0123456789created",
                "State": {"Name": "pending"},
            }
        ]
    }

    params = {"action": "create_instance", "instance_name": "MyWebServer"}
    res = execute(mock_credentials, params)

    # Verify duplicate name check
    mock_client.describe_instances.assert_called_once_with(
        Filters=[{"Name": "tag:Name", "Values": ["MyWebServer"]}]
    )

    # Verify run_instances arguments
    mock_client.run_instances.assert_called_once()
    run_args = mock_client.run_instances.call_args[1]
    assert run_args["ImageId"] == DEFAULT_AMIS["ubuntu"]
    assert run_args["InstanceType"] == "t3.micro"
    assert run_args["MinCount"] == 1
    assert run_args["MaxCount"] == 1

    tags = run_args["TagSpecifications"][0]["Tags"]
    assert {"Key": "Name", "Value": "MyWebServer"} in tags
    assert {"Key": "Project", "Value": "SatiBot"} in tags
    assert {"Key": "AutoStop", "Value": "True"} in tags

    # Verify response
    assert res["success"] is True
    assert res["action"] == "create_instance"
    assert res["error"] is None
    assert res["data"]["instance_id"] == "i-0123456789created"
    assert res["data"]["instance_name"] == "MyWebServer"
    assert res["data"]["state"] == "pending"
    assert res["data"]["instance_type"] == "t3.micro"
    assert res["data"]["image_id"] == DEFAULT_AMIS["ubuntu"]
    assert "i-0123456789created" in res["message"]


def test_create_instance_with_amazon_linux_and_custom_options(mock_credentials, mock_boto_ec2):
    """Verify create_instance with Amazon Linux, key_name, and security_groups."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}
    mock_client.run_instances.return_value = {
        "Instances": [{"InstanceId": "i-0amznlinux", "State": {"Name": "pending"}}]
    }

    params = {
        "action": "create_instance",
        "instance_name": "AppNode",
        "os_type": "amazon-linux",
        "instance_type": "t2.micro",
        "key_name": "satibot-keypair",
        "security_group_ids": ["sg-01234"],
        "subnet_id": "subnet-01234",
    }
    res = execute(mock_credentials, params)

    assert res["success"] is True
    run_args = mock_client.run_instances.call_args[1]
    assert run_args["ImageId"] == DEFAULT_AMIS["amazon-linux"]
    assert run_args["InstanceType"] == "t2.micro"
    assert run_args["KeyName"] == "satibot-keypair"
    assert run_args["SecurityGroupIds"] == ["sg-01234"]
    assert run_args["SubnetId"] == "subnet-01234"


def test_create_instance_duplicate_name_blocked(mock_credentials, mock_boto_ec2):
    """Verify create_instance is blocked when an active instance with identical name already exists."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-existing111",
                        "State": {"Name": "running"},
                    }
                ]
            }
        ]
    }

    params = {"action": "create_instance", "instance_name": "ExistingServer", "force": False}
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "create_instance"
    assert res["error"] == "DuplicateInstanceName"
    assert "i-existing111" in res["data"]["existing_instances"]
    assert "force=True" in res["message"]
    mock_client.run_instances.assert_not_called()


def test_create_instance_duplicate_name_with_force_flag(mock_credentials, mock_boto_ec2):
    """Verify create_instance proceeds when force=True even if duplicate active instance exists."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-existing111",
                        "State": {"Name": "running"},
                    }
                ]
            }
        ]
    }
    mock_client.run_instances.return_value = {
        "Instances": [{"InstanceId": "i-newforced222", "State": {"Name": "pending"}}]
    }

    params = {"action": "create_instance", "instance_name": "ExistingServer", "force": True}
    res = execute(mock_credentials, params)

    assert res["success"] is True
    assert res["data"]["instance_id"] == "i-newforced222"
    mock_client.run_instances.assert_called_once()


def test_create_instance_duplicate_terminated_ignored(mock_credentials, mock_boto_ec2):
    """Verify terminated instances with identical name do NOT block creation."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-dead999",
                        "State": {"Name": "terminated"},
                    }
                ]
            }
        ]
    }
    mock_client.run_instances.return_value = {
        "Instances": [{"InstanceId": "i-newfresh333", "State": {"Name": "pending"}}]
    }

    params = {"action": "create_instance", "instance_name": "OldTerminatedServer"}
    res = execute(mock_credentials, params)

    assert res["success"] is True
    assert res["data"]["instance_id"] == "i-newfresh333"
    mock_client.run_instances.assert_called_once()


def test_create_instance_cost_guardrail_blocked(mock_credentials, mock_boto_ec2):
    """Verify FinOps Cost Guardrail blocks large instances (e.g. c5.4xlarge)."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}

    params = {
        "action": "create_instance",
        "instance_name": "ExpensiveServer",
        "instance_type": "c5.4xlarge",
    }
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "create_instance"
    assert res["error"] == "CostGuardrailViolation"
    assert "FinOps" in res["message"]
    assert res["data"]["requested_type"] == "c5.4xlarge"
    assert res["data"]["allowed_types"] == ALLOWED_INSTANCE_TYPES
    mock_client.run_instances.assert_not_called()


def test_create_instance_cost_guardrail_override(mock_credentials, mock_boto_ec2):
    """Verify FinOps Cost Guardrail can be overridden with confirm_cost=True."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}
    mock_client.run_instances.return_value = {
        "Instances": [{"InstanceId": "i-expensiveApproved", "State": {"Name": "pending"}}]
    }

    params = {
        "action": "create_instance",
        "instance_name": "HeavyApprovedServer",
        "instance_type": "c5.4xlarge",
        "confirm_cost": True,
    }
    res = execute(mock_credentials, params)

    assert res["success"] is True
    assert res["data"]["instance_id"] == "i-expensiveApproved"
    assert res["data"]["instance_type"] == "c5.4xlarge"
    mock_client.run_instances.assert_called_once()


@pytest.mark.parametrize("invalid_name_param", [
    {"action": "create_instance"},
    {"action": "create_instance", "instance_name": ""},
    {"action": "create_instance", "instance_name": "   "},
    {"action": "create_instance", "instance_name": None},
])
def test_create_instance_missing_name(mock_credentials, mock_boto_ec2, invalid_name_param):
    """Verify create_instance fails when instance_name is missing or whitespace."""
    mock_client = mock_boto_ec2.return_value
    res = execute(mock_credentials, invalid_name_param)

    assert res["success"] is False
    assert res["action"] == "create_instance"
    assert "Missing or invalid 'instance_name'" in res["error"]
    assert "instance_name" in res["message"]
    mock_client.describe_instances.assert_not_called()
    mock_client.run_instances.assert_not_called()


# --- Tests for Scenario 3: manage_power ---

def test_manage_power_start_success(mock_credentials, mock_boto_ec2):
    """Verify start operation succeeds when instance is stopped."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-stop123", "State": {"Name": "stopped"}}
                ]
            }
        ]
    }
    mock_client.start_instances.return_value = {
        "StartingInstances": [
            {
                "InstanceId": "i-stop123",
                "PreviousState": {"Name": "stopped"},
                "CurrentState": {"Name": "pending"},
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-stop123", "operation": "start"}
    res = execute(mock_credentials, params)

    mock_client.describe_instances.assert_called_once_with(InstanceIds=["i-stop123"])
    mock_client.start_instances.assert_called_once_with(InstanceIds=["i-stop123"])
    assert res["success"] is True
    assert res["action"] == "manage_power"
    assert res["data"]["operation"] == "start"
    assert res["data"]["previous_state"] == "stopped"
    assert res["data"]["current_state"] == "pending"
    assert "bật máy chủ" in res["message"]


def test_manage_power_start_rejected_when_running(mock_credentials, mock_boto_ec2):
    """Verify start operation is rejected when instance is already running."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-run123", "State": {"Name": "running"}}
                ]
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-run123", "operation": "start"}
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "manage_power"
    assert "InvalidStateTransition" in res["error"]
    assert "đang ở trạng thái 'running'" in res["message"]
    mock_client.start_instances.assert_not_called()


def test_manage_power_stop_success(mock_credentials, mock_boto_ec2):
    """Verify stop operation succeeds when instance is running."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-run123", "State": {"Name": "running"}}
                ]
            }
        ]
    }
    mock_client.stop_instances.return_value = {
        "StoppingInstances": [
            {
                "InstanceId": "i-run123",
                "PreviousState": {"Name": "running"},
                "CurrentState": {"Name": "stopping"},
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-run123", "operation": "stop"}
    res = execute(mock_credentials, params)

    mock_client.describe_instances.assert_called_once_with(InstanceIds=["i-run123"])
    mock_client.stop_instances.assert_called_once_with(InstanceIds=["i-run123"])
    assert res["success"] is True
    assert res["action"] == "manage_power"
    assert res["data"]["operation"] == "stop"
    assert res["data"]["previous_state"] == "running"
    assert res["data"]["current_state"] == "stopping"
    assert "tắt máy chủ" in res["message"]


def test_manage_power_stop_rejected_when_stopped(mock_credentials, mock_boto_ec2):
    """Verify stop operation is rejected when instance is already stopped."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-stop123", "State": {"Name": "stopped"}}
                ]
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-stop123", "operation": "stop"}
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "manage_power"
    assert "InvalidStateTransition" in res["error"]
    assert "đang ở trạng thái 'stopped'" in res["message"]
    mock_client.stop_instances.assert_not_called()


def test_manage_power_reboot_success(mock_credentials, mock_boto_ec2):
    """Verify reboot operation succeeds when instance is running."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-run123", "State": {"Name": "running"}}
                ]
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-run123", "operation": "reboot"}
    res = execute(mock_credentials, params)

    mock_client.describe_instances.assert_called_once_with(InstanceIds=["i-run123"])
    mock_client.reboot_instances.assert_called_once_with(InstanceIds=["i-run123"])
    assert res["success"] is True
    assert res["action"] == "manage_power"
    assert res["data"]["operation"] == "reboot"
    assert res["data"]["current_state"] == "rebooting"
    assert "khởi động lại" in res["message"]


def test_manage_power_reboot_rejected_when_stopped(mock_credentials, mock_boto_ec2):
    """Verify reboot operation is rejected when instance is stopped."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-stop123", "State": {"Name": "stopped"}}
                ]
            }
        ]
    }

    params = {"action": "manage_power", "instance_id": "i-stop123", "operation": "reboot"}
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "manage_power"
    assert "InvalidStateTransition" in res["error"]
    assert "đang ở trạng thái 'stopped'" in res["message"]
    mock_client.reboot_instances.assert_not_called()


def test_manage_power_invalid_operation(mock_credentials, mock_boto_ec2):
    """Verify manage_power rejects unsupported operations (e.g. hibernate, suspend)."""
    mock_client = mock_boto_ec2.return_value
    params = {"action": "manage_power", "instance_id": "i-123", "operation": "hibernate"}
    res = execute(mock_credentials, params)

    assert res["success"] is False
    assert res["action"] == "manage_power"
    assert "InvalidOperation" in res["error"]
    assert "không hợp lệ" in res["message"]
    mock_client.describe_instances.assert_not_called()


@pytest.mark.parametrize("missing_param", [
    {"action": "manage_power", "operation": "start"},  # Missing instance_id
    {"action": "manage_power", "instance_id": "i-123"},  # Missing operation
    {"action": "manage_power", "instance_id": "", "operation": "start"},  # Empty instance_id
    {"action": "manage_power", "instance_id": "i-123", "operation": ""},  # Empty operation
])
def test_manage_power_missing_required_params(mock_credentials, mock_boto_ec2, missing_param):
    """Verify manage_power rejects requests missing either instance_id or operation."""
    mock_client = mock_boto_ec2.return_value
    res = execute(mock_credentials, missing_param)

    assert res["success"] is False
    assert res["action"] == "manage_power"
    assert res["error"] is not None
    mock_client.describe_instances.assert_not_called()


# --- Tests for Scenario 4: terminate_instance ---

def test_terminate_instance_missing_id(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance rejects requests with missing or empty instance_id."""
    mock_client = mock_boto_ec2.return_value
    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": ""})

    assert res["success"] is False
    assert res["action"] == "terminate_instance"
    assert "Missing or invalid 'instance_id'" in res["error"]
    mock_client.describe_instances.assert_not_called()


def test_terminate_instance_not_found(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance returns not found when instance does not exist."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}

    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": "i-999999"})

    assert res["success"] is False
    assert res["action"] == "terminate_instance"
    assert "InstanceNotFound" in res["error"]
    assert "Không tìm thấy" in res["message"]


def test_terminate_instance_already_terminated(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance rejects if instance is already in terminated state."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-123", "State": {"Name": "terminated"}, "Tags": []}]}]
    }

    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": "i-123"})

    assert res["success"] is False
    assert res["action"] == "terminate_instance"
    assert "InstanceAlreadyTerminated" in res["error"]
    assert "đã ở trạng thái đã xóa" in res["message"]
    mock_client.terminate_instances.assert_not_called()


def test_terminate_instance_protection_enabled(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance is blocked if Termination Protection is active."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-123", "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": "prod-app"}]}]}]
    }
    mock_client.describe_instance_attribute.return_value = {
        "DisableApiTermination": {"Value": True}
    }

    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": "i-123", "confirm": True})

    assert res["success"] is False
    assert res["action"] == "terminate_instance"
    assert "TerminationProtectionEnabled" in res["error"]
    assert "bảo vệ chống xóa" in res["message"]
    mock_client.terminate_instances.assert_not_called()


def test_terminate_instance_requires_confirmation(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance warns and requires confirm=True before destroying."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-123", "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": "web-demo"}]}]}]
    }
    mock_client.describe_instance_attribute.return_value = {
        "DisableApiTermination": {"Value": False}
    }

    # Calling without confirm=True
    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": "i-123"})

    assert res["success"] is False
    assert res["action"] == "terminate_instance"
    assert "ConfirmationRequired" in res["error"]
    assert "Cảnh báo rủi ro cao" in res["message"]
    assert res["data"]["confirmation_required"] is True
    mock_client.terminate_instances.assert_not_called()


def test_terminate_instance_success(mock_credentials, mock_boto_ec2):
    """Verify terminate_instance successfully destroys instance when confirm=True."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-123", "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": "web-demo"}]}]}]
    }
    mock_client.describe_instance_attribute.return_value = {
        "DisableApiTermination": {"Value": False}
    }
    mock_client.terminate_instances.return_value = {
        "TerminatingInstances": [{
            "InstanceId": "i-123",
            "PreviousState": {"Name": "running"},
            "CurrentState": {"Name": "shutting-down"}
        }]
    }

    res = execute(mock_credentials, {"action": "terminate_instance", "instance_id": "i-123", "confirm": True})

    assert res["success"] is True
    assert res["action"] == "terminate_instance"
    assert res["data"]["instance_id"] == "i-123"
    assert res["data"]["previous_state"] == "running"
    assert res["data"]["current_state"] == "shutting-down"
    assert "thành công" in res["message"]
    mock_client.terminate_instances.assert_called_once_with(InstanceIds=["i-123"])


def test_terminate_instance_namespaced_alias(mock_credentials, mock_boto_ec2):
    """Verify calling terminate_ec2_instance alias also succeeds."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-123", "State": {"Name": "stopped"}, "Tags": []}]}]
    }
    mock_client.describe_instance_attribute.return_value = {"DisableApiTermination": {"Value": False}}
    mock_client.terminate_instances.return_value = {
        "TerminatingInstances": [{
            "InstanceId": "i-123",
            "PreviousState": {"Name": "stopped"},
            "CurrentState": {"Name": "shutting-down"}
        }]
    }

    res = execute(mock_credentials, {"action": "terminate_ec2_instance", "instance_id": "i-123", "confirmed": True})
    assert res["success"] is True
    assert res["data"]["current_state"] == "shutting-down"


# --- Tests for Scenario 5: get_metrics & FinOps ---

def test_get_metrics_missing_id(mock_credentials, mock_boto_ec2):
    """Verify get_metrics rejects requests without instance_id."""
    res = execute(mock_credentials, {"action": "get_metrics", "instance_id": ""})

    assert res["success"] is False
    assert res["action"] == "get_metrics"
    assert "Missing or invalid 'instance_id'" in res["error"]


def test_get_metrics_not_found(mock_credentials, mock_boto_ec2):
    """Verify get_metrics returns error when instance does not exist."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {"Reservations": []}

    res = execute(mock_credentials, {"action": "get_metrics", "instance_id": "i-nonexist"})

    assert res["success"] is False
    assert res["action"] == "get_metrics"
    assert "InstanceNotFound" in res["error"]


def test_get_metrics_no_datapoints(mock_credentials, mock_boto_ec2):
    """Verify get_metrics handles newly launched or stopped instances with no metrics."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-123",
                "InstanceType": "t3.micro",
                "State": {"Name": "stopped"},
                "Tags": [{"Key": "Name", "Value": "demo-server"}]
            }]
        }]
    }
    mock_client.get_metric_statistics.return_value = {"Datapoints": []}

    res = execute(mock_credentials, {"action": "get_metrics", "instance_id": "i-123", "period_hours": 24})

    assert res["success"] is True
    assert res["action"] == "get_metrics"
    assert res["data"]["recommendation_type"] == "no_data"
    assert res["data"]["average_cpu"] == 0.0
    assert "chưa ghi nhận" in res["message"]


def test_get_metrics_idle_waste_recommendation(mock_credentials, mock_boto_ec2):
    """Verify get_metrics detects low CPU utilization and warns idle waste."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-123",
                "InstanceType": "t3.micro",
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "idle-server"}]
            }]
        }]
    }
    # Mock CPU averaging 1.8%
    mock_client.get_metric_statistics.return_value = {
        "Datapoints": [
            {"Average": 1.2, "Maximum": 2.5, "Timestamp": datetime.now(timezone.utc)},
            {"Average": 2.4, "Maximum": 3.0, "Timestamp": datetime.now(timezone.utc)}
        ]
    }

    res = execute(mock_credentials, {"action": "get_metrics", "instance_id": "i-123"})

    assert res["success"] is True
    assert res["data"]["recommendation_type"] == "idle_waste"
    assert res["data"]["average_cpu"] < 5.0
    assert "Cảnh báo lãng phí FinOps" in res["message"]
    assert "Tắt máy chủ" in res["data"]["recommendation"]


def test_get_metrics_overloaded_recommendation(mock_credentials, mock_boto_ec2):
    """Verify get_metrics detects high CPU utilization (> 80%) and warns overload."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-123",
                "InstanceType": "t3.micro",
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "busy-server"}]
            }]
        }]
    }
    # Mock CPU averaging 89.5%
    mock_client.get_metric_statistics.return_value = {
        "Datapoints": [
            {"Average": 85.0, "Maximum": 94.0, "Timestamp": datetime.now(timezone.utc)},
            {"Average": 94.0, "Maximum": 98.0, "Timestamp": datetime.now(timezone.utc)}
        ]
    }

    res = execute(mock_credentials, {"action": "get_metrics", "instance_id": "i-123"})

    assert res["success"] is True
    assert res["data"]["recommendation_type"] == "overloaded"
    assert res["data"]["average_cpu"] > 80.0
    assert "Cảnh báo hiệu năng" in res["message"]
    assert "nâng cấp cấu hình máy chủ" in res["data"]["recommendation"]


def test_get_metrics_optimal_recommendation(mock_credentials, mock_boto_ec2):
    """Verify get_metrics detects optimal CPU utilization (5% <= CPU <= 80%)."""
    mock_client = mock_boto_ec2.return_value
    mock_client.describe_instances.return_value = {
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-123",
                "InstanceType": "t3.micro",
                "State": {"Name": "running"},
                "Tags": []
            }]
        }]
    }
    # Mock CPU averaging 45.0%
    mock_client.get_metric_statistics.return_value = {
        "Datapoints": [
            {"Average": 40.0, "Maximum": 55.0, "Timestamp": datetime.now(timezone.utc)},
            {"Average": 50.0, "Maximum": 60.0, "Timestamp": datetime.now(timezone.utc)}
        ]
    }

    res = execute(mock_credentials, {"action": "get_ec2_metrics", "instance_id": "i-123"})

    assert res["success"] is True
    assert res["data"]["recommendation_type"] == "optimal"
    assert res["data"]["average_cpu"] == 45.0
    assert "Hiệu năng tối ưu" in res["message"]


# --- Tests for EC2 Tool Schemas (AI Agent Function Calling) ---

def test_ec2_tool_schemas_structure():
    """Verify all EC2 tool schemas have required keys and correct specifications."""
    assert len(EC2_TOOL_SCHEMAS) == 6

    expected_names = {
        "create_ec2_instance",
        "list_ec2_instances",
        "get_ec2_instance_detail",
        "manage_ec2_power",
        "terminate_ec2_instance",
        "get_ec2_metrics",
    }
    actual_names = {schema["name"] for schema in EC2_TOOL_SCHEMAS}
    assert actual_names == expected_names

    for schema in EC2_TOOL_SCHEMAS:
        assert schema.get("service") == "ec2"
        assert isinstance(schema.get("description"), str) and len(schema["description"]) > 10
        assert "parameters" in schema
        assert schema["parameters"]["type"] == "object"
        assert "properties" in schema["parameters"]


def test_ec2_tool_schemas_confirmation_flags():
    """Verify high-impact actions require confirmation while queries do not."""
    confirmation_map = {s["name"]: s["requires_confirmation"] for s in EC2_TOOL_SCHEMAS}

    # Mutations must require confirmation
    assert confirmation_map["create_ec2_instance"] is True
    assert confirmation_map["manage_ec2_power"] is True
    assert confirmation_map["terminate_ec2_instance"] is True

    # Read-only queries must not block on confirmation
    assert confirmation_map["list_ec2_instances"] is False
    assert confirmation_map["get_ec2_instance_detail"] is False
    assert confirmation_map["get_ec2_metrics"] is False
