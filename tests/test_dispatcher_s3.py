from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from backend.functions.dispatcher.services.s3_service import execute


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_create_bucket_safely(mock_boto3_client):
    # Giả lập S3 client để test không gọi AWS thật
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    params = {
        "action": "create_bucket",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    # Kiểm tra chương trình tạo đúng S3 client
    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-2",
    )

    # Region khác us-east-1 phải có LocationConstraint
    mock_s3_client.create_bucket.assert_called_once_with(
        Bucket="sitabot-hung-test-a1b2c3",
        CreateBucketConfiguration={
            "LocationConstraint": "us-east-2",
        },
    )

    # Bucket phải được chặn toàn bộ public access
    mock_s3_client.put_public_access_block.assert_called_once_with(
        Bucket="sitabot-hung-test-a1b2c3",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    assert result == {
        "ok": True,
        "service": "s3",
        "action": "create_bucket",
        "data": {
            "bucket_name": "sitabot-hung-test-a1b2c3",
            "region": "us-east-2",
            "public_access_blocked": True,
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_create_bucket_requires_bucket_name(mock_boto3_client):
    params = {
        "action": "create_bucket",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "create_bucket",
        "error": {
            "code": "MissingBucketName",
            "message": "Thiếu tham số bucket_name.",
        },
    }

    # Dữ liệu chưa hợp lệ thì tuyệt đối không gọi AWS
    mock_boto3_client.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_rejects_unsupported_action(mock_boto3_client):
    params = {
        "action": "delete_everything",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "delete_everything",
        "error": {
            "code": "UnsupportedAction",
            "message": "Hành động S3 không được hỗ trợ: delete_everything",
        },
    }

    # Action lạ hoặc nguy hiểm không được phép gọi AWS
    mock_boto3_client.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_handles_bucket_already_exists_error(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    mock_s3_client.create_bucket.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "BucketAlreadyExists",
                "Message": "The requested bucket name is not available.",
            }
        },
        operation_name="CreateBucket",
    )

    params = {
        "action": "create_bucket",
        "bucket_name": "bucket-name-already-used",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "create_bucket",
        "error": {
            "code": "BucketAlreadyExists",
            "message": "The requested bucket name is not available.",
        },
    }

    # Tạo bucket thất bại thì không được cấu hình bucket tiếp
    mock_s3_client.put_public_access_block.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_create_bucket_in_us_east_1_without_location_constraint(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    params = {
        "action": "create_bucket",
        "bucket_name": "sitabot-hung-us-east-1-test",
        "region": "us-east-1",
    }

    result = execute(credentials=None, params=params)

    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-1",
    )

    # us-east-1 không được truyền CreateBucketConfiguration
    mock_s3_client.create_bucket.assert_called_once_with(
        Bucket="sitabot-hung-us-east-1-test",
    )

    assert result["ok"] is True
    assert result["data"]["region"] == "us-east-1"


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_uses_temporary_sts_credentials(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    credentials = {
        "AccessKeyId": "temporary-access-key",
        "SecretAccessKey": "temporary-secret-key",
        "SessionToken": "temporary-session-token",
    }

    params = {
        "action": "create_bucket",
        "bucket_name": "sitabot-hung-sts-test",
        "region": "us-east-2",
    }

    result = execute(
        credentials=credentials,
        params=params,
    )

    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-2",
        aws_access_key_id="temporary-access-key",
        aws_secret_access_key="temporary-secret-key",
        aws_session_token="temporary-session-token",
    )

    assert result["ok"] is True

