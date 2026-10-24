from unittest.mock import ANY, Mock, patch

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
        config=ANY,
    )
    client_config = mock_boto3_client.call_args.kwargs["config"]
    assert client_config.signature_version == "s3v4"
    assert client_config.s3 == {"addressing_style": "virtual"}

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
        config=ANY,
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
        config=ANY,
    )

    assert result["ok"] is True




@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_create_presigned_download_url(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    mock_s3_client.generate_presigned_url.return_value = (
        "https://example.com/signed-download-url"
    )

    params = {
        "action": "create_presigned_download",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "object_key": "documents/report.pdf",
        "expires_in": 900,
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-2",
        config=ANY,
    )

    mock_s3_client.generate_presigned_url.assert_called_once_with(
        ClientMethod="get_object",
        Params={
            "Bucket": "sitabot-hung-test-a1b2c3",
            "Key": "documents/report.pdf",
        },
        ExpiresIn=900,
    )

    assert result == {
        "ok": True,
        "service": "s3",
        "action": "create_presigned_download",
        "data": {
            "bucket_name": "sitabot-hung-test-a1b2c3",
            "object_key": "documents/report.pdf",
            "expires_in": 900,
            "url": "https://example.com/signed-download-url",
        },
    }




@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_presigned_download_requires_object_key(mock_boto3_client):
    params = {
        "action": "create_presigned_download",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "expires_in": 900,
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "create_presigned_download",
        "error": {
            "code": "MissingObjectKey",
            "message": "Thiếu tham số object_key.",
        },
    }

    # Thiếu dữ liệu thì không được tạo AWS client
    mock_boto3_client.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_presigned_download_rejects_invalid_expiration(
    mock_boto3_client,
):
    params = {
        "action": "create_presigned_download",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "object_key": "documents/report.pdf",
        "expires_in": 0,
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "create_presigned_download",
        "error": {
            "code": "InvalidExpiration",
            "message": "expires_in phải là số nguyên dương.",
        },
    }

    mock_boto3_client.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_handles_presigned_download_aws_error(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    mock_s3_client.generate_presigned_url.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "AccessDenied",
                "Message": "Access denied to the requested object.",
            }
        },
        operation_name="GetObject",
    )

    params = {
        "action": "create_presigned_download",
        "bucket_name": "sitabot-hung-test-a1b2c3",
        "object_key": "private/report.pdf",
        "expires_in": 900,
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "create_presigned_download",
        "error": {
            "code": "AccessDenied",
            "message": "Access denied to the requested object.",
        },
    }




@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_block_public_access_for_existing_bucket(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    params = {
        "action": "block_public_access",
        "bucket_name": "sitabot-hung-existing-bucket",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    mock_boto3_client.assert_called_once_with(
        "s3",
        region_name="us-east-2",
        config=ANY,
    )

    mock_s3_client.put_public_access_block.assert_called_once_with(
        Bucket="sitabot-hung-existing-bucket",
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
        "action": "block_public_access",
        "data": {
            "bucket_name": "sitabot-hung-existing-bucket",
            "public_access_blocked": True,
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_handles_block_public_access_denied(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    mock_s3_client.put_public_access_block.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "AccessDenied",
                "Message": "User is not authorized to block public access.",
            }
        },
        operation_name="PutPublicAccessBlock",
    )

    params = {
        "action": "block_public_access",
        "bucket_name": "sitabot-hung-existing-bucket",
        "region": "us-east-2",
    }

    result = execute(credentials=None, params=params)

    assert result == {
        "ok": False,
        "service": "s3",
        "action": "block_public_access",
        "error": {
            "code": "AccessDenied",
            "message": (
                "User is not authorized to block public access."
            ),
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_scan_public_access_finds_risky_buckets(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client

    mock_s3_client.list_buckets.return_value = {
        "Buckets": [
            {"Name": "sitabot-safe-bucket"},
            {"Name": "sitabot-risky-bucket"},
        ]
    }
    mock_s3_client.get_public_access_block.side_effect = [
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        },
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            }
        },
    ]
    mock_s3_client.get_bucket_policy_status.side_effect = [
        {"PolicyStatus": {"IsPublic": False}},
        {"PolicyStatus": {"IsPublic": True}},
    ]
    mock_s3_client.get_bucket_acl.side_effect = [
        {"Grants": []},
        {"Grants": []},
    ]

    result = execute(
        credentials=None,
        params={
            "action": "scan_public_access",
            "region": "us-east-2",
        },
    )

    assert result == {
        "ok": True,
        "service": "s3",
        "action": "scan_public_access",
        "data": {
            "scanned_count": 2,
            "risky_count": 1,
            "risky_buckets": [
                {
                    "bucket_name": "sitabot-risky-bucket",
                    "reasons": [
                        "PUBLIC_ACCESS_BLOCK_INCOMPLETE",
                        "PUBLIC_BUCKET_POLICY",
                    ],
                }
            ],
            "scan_errors": [],
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_scan_public_access_detects_missing_block_and_public_acl(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    mock_s3_client.list_buckets.return_value = {
        "Buckets": [{"Name": "sitabot-legacy-bucket"}]
    }
    mock_s3_client.get_public_access_block.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "NoSuchPublicAccessBlockConfiguration",
                "Message": "The public access block configuration was not found.",
            }
        },
        operation_name="GetPublicAccessBlock",
    )
    mock_s3_client.get_bucket_policy_status.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "NoSuchBucketPolicy",
                "Message": "The bucket policy does not exist.",
            }
        },
        operation_name="GetBucketPolicyStatus",
    )
    mock_s3_client.get_bucket_acl.return_value = {
        "Grants": [
            {
                "Grantee": {
                    "Type": "Group",
                    "URI": (
                        "http://acs.amazonaws.com/groups/global/AllUsers"
                    ),
                },
                "Permission": "READ",
            }
        ]
    }

    result = execute(
        credentials=None,
        params={"action": "scan_public_access"},
    )

    assert result["ok"] is True
    assert result["data"]["risky_buckets"] == [
        {
            "bucket_name": "sitabot-legacy-bucket",
            "reasons": [
                "PUBLIC_ACCESS_BLOCK_INCOMPLETE",
                "PUBLIC_BUCKET_ACL",
            ],
        }
    ]
    assert result["data"]["scan_errors"] == []


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_scan_public_access_records_per_bucket_access_denied(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    mock_s3_client.list_buckets.return_value = {
        "Buckets": [{"Name": "restricted-bucket"}]
    }
    mock_s3_client.get_public_access_block.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "AccessDenied",
                "Message": "Not allowed to inspect this bucket.",
            }
        },
        operation_name="GetPublicAccessBlock",
    )
    mock_s3_client.get_bucket_policy_status.return_value = {
        "PolicyStatus": {"IsPublic": False}
    }
    mock_s3_client.get_bucket_acl.return_value = {"Grants": []}

    result = execute(
        credentials=None,
        params={"action": "scan_public_access"},
    )

    assert result["ok"] is True
    assert result["data"]["scanned_count"] == 1
    assert result["data"]["risky_count"] == 0
    assert result["data"]["scan_errors"] == [
        {
            "bucket_name": "restricted-bucket",
            "check": "PUBLIC_ACCESS_BLOCK",
            "code": "AccessDenied",
            "message": "Not allowed to inspect this bucket.",
        }
    ]


def _configure_bucket_inventory(
    mock_s3_client,
    object_pages,
    version_pages,
):
    objects_paginator = Mock()
    objects_paginator.paginate.return_value = object_pages
    versions_paginator = Mock()
    versions_paginator.paginate.return_value = version_pages

    def get_paginator(operation_name):
        if operation_name == "list_objects_v2":
            return objects_paginator
        if operation_name == "list_object_versions":
            return versions_paginator
        raise AssertionError(f"Unexpected paginator: {operation_name}")

    mock_s3_client.get_paginator.side_effect = get_paginator


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_inspect_bucket_for_delete_counts_all_items(mock_boto3_client):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    _configure_bucket_inventory(
        mock_s3_client,
        object_pages=[
            {
                "Contents": [
                    {"Key": "current.txt"},
                    {"Key": "unversioned.txt"},
                ]
            }
        ],
        version_pages=[
            {
                "Versions": [
                    {"Key": "current.txt", "VersionId": "v2"},
                    {"Key": "current.txt", "VersionId": "v1"},
                ],
                "DeleteMarkers": [
                    {"Key": "deleted.txt", "VersionId": "d1"}
                ],
            }
        ],
    )

    result = execute(
        credentials=None,
        params={
            "action": "inspect_bucket_for_delete",
            "bucket_name": "sitabot-delete-test",
            "region": "us-east-2",
        },
    )

    assert result == {
        "ok": True,
        "service": "s3",
        "action": "inspect_bucket_for_delete",
        "data": {
            "bucket_name": "sitabot-delete-test",
            "object_count": 2,
            "version_count": 2,
            "delete_marker_count": 1,
            "deletable_item_count": 4,
            "is_empty": False,
            "required_confirmation": "DELETE sitabot-delete-test",
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_delete_bucket_safe_requires_exact_confirmation(
    mock_boto3_client,
):
    result = execute(
        credentials=None,
        params={
            "action": "delete_bucket_safe",
            "bucket_name": "important-data",
            "confirmation": "yes",
            "expected_deletable_item_count": 0,
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ConfirmationRequired"
    assert "DELETE important-data" in result["error"]["message"]
    mock_boto3_client.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_delete_bucket_safe_stops_when_contents_changed(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    _configure_bucket_inventory(
        mock_s3_client,
        object_pages=[{"Contents": [{"Key": "new-file.txt"}]}],
        version_pages=[{}],
    )

    result = execute(
        credentials=None,
        params={
            "action": "delete_bucket_safe",
            "bucket_name": "sitabot-delete-test",
            "confirmation": "DELETE sitabot-delete-test",
            "expected_deletable_item_count": 0,
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "BucketContentsChanged"
    assert result["data"]["expected_deletable_item_count"] == 0
    assert result["data"]["current_deletable_item_count"] == 1
    mock_s3_client.delete_objects.assert_not_called()
    mock_s3_client.delete_bucket.assert_not_called()


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_delete_bucket_safe_removes_versions_and_bucket(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    mock_s3_client.delete_objects.return_value = {}
    _configure_bucket_inventory(
        mock_s3_client,
        object_pages=[
            {
                "Contents": [
                    {"Key": "current.txt"},
                    {"Key": "unversioned.txt"},
                ]
            }
        ],
        version_pages=[
            {
                "Versions": [
                    {"Key": "current.txt", "VersionId": "v2"},
                    {"Key": "current.txt", "VersionId": "v1"},
                ],
                "DeleteMarkers": [
                    {"Key": "deleted.txt", "VersionId": "d1"}
                ],
            }
        ],
    )

    result = execute(
        credentials=None,
        params={
            "action": "delete_bucket_safe",
            "bucket_name": "sitabot-delete-test",
            "confirmation": "DELETE sitabot-delete-test",
            "expected_deletable_item_count": 4,
        },
    )

    mock_s3_client.delete_objects.assert_called_once_with(
        Bucket="sitabot-delete-test",
        Delete={
            "Objects": [
                {"Key": "current.txt", "VersionId": "v2"},
                {"Key": "current.txt", "VersionId": "v1"},
                {"Key": "deleted.txt", "VersionId": "d1"},
                {"Key": "unversioned.txt"},
            ],
            "Quiet": True,
        },
    )
    mock_s3_client.delete_bucket.assert_called_once_with(
        Bucket="sitabot-delete-test"
    )
    assert result == {
        "ok": True,
        "service": "s3",
        "action": "delete_bucket_safe",
        "data": {
            "bucket_name": "sitabot-delete-test",
            "deleted_item_count": 4,
            "bucket_deleted": True,
        },
    }


@patch("backend.functions.dispatcher.services.s3_service.boto3.client")
def test_delete_bucket_safe_does_not_delete_bucket_after_object_error(
    mock_boto3_client,
):
    mock_s3_client = Mock()
    mock_boto3_client.return_value = mock_s3_client
    mock_s3_client.delete_objects.return_value = {
        "Errors": [
            {
                "Key": "protected.txt",
                "Code": "AccessDenied",
                "Message": "Access denied.",
            }
        ]
    }
    _configure_bucket_inventory(
        mock_s3_client,
        object_pages=[{"Contents": [{"Key": "protected.txt"}]}],
        version_pages=[{}],
    )

    result = execute(
        credentials=None,
        params={
            "action": "delete_bucket_safe",
            "bucket_name": "sitabot-delete-test",
            "confirmation": "DELETE sitabot-delete-test",
            "expected_deletable_item_count": 1,
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ObjectDeleteFailed"
    mock_s3_client.delete_bucket.assert_not_called()
