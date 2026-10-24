import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


PUBLIC_ACL_GROUP_URIS = {
    "http://acs.amazonaws.com/groups/global/AllUsers",
    "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
}

PUBLIC_ACCESS_BLOCK_KEYS = (
    "BlockPublicAcls",
    "IgnorePublicAcls",
    "BlockPublicPolicy",
    "RestrictPublicBuckets",
)

SUPPORTED_LIFECYCLE_STORAGE_CLASSES = {
    "STANDARD_IA",
    "ONEZONE_IA",
    "INTELLIGENT_TIERING",
    "GLACIER_IR",
    "GLACIER",
    "DEEP_ARCHIVE",
}

LIFECYCLE_MINIMUM_TRANSITION_DAYS = {
    "STANDARD_IA": 30,
    "ONEZONE_IA": 30,
}


def _create_s3_client(credentials, region):
    """
    Tạo S3 client.

    Khi chạy unit test hoặc chạy bằng IAM Role của Lambda,
    credentials có thể là None và Boto3 tự tìm credential.

    Khi Dispatcher đã AssumeRole, credentials chứa bộ khóa tạm thời
    do AWS STS trả về.
    """
    client_options = {
        "region_name": region,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        ),
    }

    if credentials:
        client_options.update(
            {
                "aws_access_key_id": credentials["AccessKeyId"],
                "aws_secret_access_key": credentials["SecretAccessKey"],
                "aws_session_token": credentials["SessionToken"],
            }
        )

    return boto3.client("s3", **client_options)


def _apply_public_access_block(client, bucket_name):
    """
    Chặn toàn bộ hình thức cấp quyền public cho bucket.
    """
    client.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )


def _create_bucket(client, bucket_name, region):
    """
    Tạo bucket và bật toàn bộ thiết lập Block Public Access.
    """
    create_options = {
        "Bucket": bucket_name,
    }

    # us-east-1 là trường hợp đặc biệt:
    # AWS không cho truyền LocationConstraint="us-east-1".
    if region != "us-east-1":
        create_options["CreateBucketConfiguration"] = {
            "LocationConstraint": region,
        }

    client.create_bucket(**create_options)

    _apply_public_access_block(client, bucket_name)

    return {
        "ok": True,
        "service": "s3",
        "action": "create_bucket",
        "data": {
            "bucket_name": bucket_name,
            "region": region,
            "public_access_blocked": True,
        },
    }


def _block_public_access(client, bucket_name):
    """
    Bật Block Public Access cho một bucket đã tồn tại.
    """
    _apply_public_access_block(client, bucket_name)

    return {
        "ok": True,
        "service": "s3",
        "action": "block_public_access",
        "data": {
            "bucket_name": bucket_name,
            "public_access_blocked": True,
        },
    }


def _append_scan_error(scan_errors, bucket_name, check, error):
    """Ghi nhận lỗi của một phép kiểm tra mà không dừng toàn bộ lần quét."""
    error_details = error.response.get("Error", {})
    scan_errors.append(
        {
            "bucket_name": bucket_name,
            "check": check,
            "code": error_details.get("Code", "AwsClientError"),
            "message": error_details.get(
                "Message",
                "AWS từ chối phép kiểm tra bucket.",
            ),
        }
    )


def _scan_public_access(client):
    """
    Quét các bucket trong tài khoản và phát hiện cấu hình có nguy cơ public.

    Mỗi bucket được kiểm tra ở ba lớp:
    - S3 Block Public Access.
    - Trạng thái public của bucket policy.
    - ACL cấp quyền cho AllUsers hoặc AuthenticatedUsers.

    Nếu không đủ quyền kiểm tra một bucket, lỗi được đưa vào scan_errors để
    các bucket còn lại vẫn tiếp tục được quét.
    """
    bucket_response = client.list_buckets()
    bucket_names = [
        bucket.get("Name")
        for bucket in bucket_response.get("Buckets", [])
        if bucket.get("Name")
    ]

    risky_buckets = []
    scan_errors = []

    for bucket_name in bucket_names:
        reasons = []

        try:
            block_response = client.get_public_access_block(
                Bucket=bucket_name,
            )
            block_configuration = block_response.get(
                "PublicAccessBlockConfiguration",
                {},
            )
            if not all(
                block_configuration.get(key) is True
                for key in PUBLIC_ACCESS_BLOCK_KEYS
            ):
                reasons.append("PUBLIC_ACCESS_BLOCK_INCOMPLETE")
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code in {
                "NoSuchPublicAccessBlockConfiguration",
                "NoSuchPublicAccessBlock",
            }:
                reasons.append("PUBLIC_ACCESS_BLOCK_INCOMPLETE")
            else:
                _append_scan_error(
                    scan_errors,
                    bucket_name,
                    "PUBLIC_ACCESS_BLOCK",
                    error,
                )

        try:
            policy_response = client.get_bucket_policy_status(
                Bucket=bucket_name,
            )
            if policy_response.get("PolicyStatus", {}).get("IsPublic") is True:
                reasons.append("PUBLIC_BUCKET_POLICY")
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code not in {
                "NoSuchBucketPolicy",
                "NoSuchPolicy",
            }:
                _append_scan_error(
                    scan_errors,
                    bucket_name,
                    "BUCKET_POLICY",
                    error,
                )

        try:
            acl_response = client.get_bucket_acl(Bucket=bucket_name)
            has_public_acl = any(
                grant.get("Grantee", {}).get("Type") == "Group"
                and grant.get("Grantee", {}).get("URI")
                in PUBLIC_ACL_GROUP_URIS
                for grant in acl_response.get("Grants", [])
            )
            if has_public_acl:
                reasons.append("PUBLIC_BUCKET_ACL")
        except ClientError as error:
            _append_scan_error(
                scan_errors,
                bucket_name,
                "BUCKET_ACL",
                error,
            )

        if reasons:
            risky_buckets.append(
                {
                    "bucket_name": bucket_name,
                    "reasons": reasons,
                }
            )

    return {
        "ok": True,
        "service": "s3",
        "action": "scan_public_access",
        "data": {
            "scanned_count": len(bucket_names),
            "risky_count": len(risky_buckets),
            "risky_buckets": risky_buckets,
            "scan_errors": scan_errors,
        },
    }


def _collect_bucket_delete_inventory(client, bucket_name):
    """
    Thu thập các mục phải xóa trước khi có thể xóa bucket.

    S3 versioning cần xử lý cả object versions và delete markers. Danh sách
    object hiện tại được đọc riêng để không bỏ sót object không có version.
    """
    current_keys = []
    objects_paginator = client.get_paginator("list_objects_v2")

    for page in objects_paginator.paginate(Bucket=bucket_name):
        current_keys.extend(
            item["Key"]
            for item in page.get("Contents", [])
            if item.get("Key")
        )

    versions = []
    delete_markers = []
    versions_paginator = client.get_paginator("list_object_versions")

    for page in versions_paginator.paginate(Bucket=bucket_name):
        versions.extend(
            {
                "Key": item["Key"],
                "VersionId": item["VersionId"],
            }
            for item in page.get("Versions", [])
            if item.get("Key") and item.get("VersionId") is not None
        )
        delete_markers.extend(
            {
                "Key": item["Key"],
                "VersionId": item["VersionId"],
            }
            for item in page.get("DeleteMarkers", [])
            if item.get("Key") and item.get("VersionId") is not None
        )

    versioned_keys = {
        item["Key"]
        for item in versions + delete_markers
    }
    unversioned_objects = [
        {"Key": key}
        for key in current_keys
        if key not in versioned_keys
    ]
    delete_candidates = versions + delete_markers + unversioned_objects

    return {
        "object_count": len(current_keys),
        "version_count": len(versions),
        "delete_marker_count": len(delete_markers),
        "deletable_item_count": len(delete_candidates),
        "delete_candidates": delete_candidates,
    }


def _inspect_bucket_for_delete(client, bucket_name):
    """Trả về ảnh chụp nội dung bucket trước khi người dùng xác nhận xóa."""
    inventory = _collect_bucket_delete_inventory(client, bucket_name)

    return {
        "ok": True,
        "service": "s3",
        "action": "inspect_bucket_for_delete",
        "data": {
            "bucket_name": bucket_name,
            "object_count": inventory["object_count"],
            "version_count": inventory["version_count"],
            "delete_marker_count": inventory["delete_marker_count"],
            "deletable_item_count": inventory["deletable_item_count"],
            "is_empty": inventory["deletable_item_count"] == 0,
            "required_confirmation": f"DELETE {bucket_name}",
        },
    }


def _delete_bucket_safe(
    client,
    bucket_name,
    expected_deletable_item_count,
):
    """
    Xóa toàn bộ version, delete marker và object rồi mới xóa bucket.

    Số mục hiện tại phải khớp ảnh chụp mà người dùng đã xác nhận. Nếu bucket
    thay đổi giữa hai bước, thao tác dừng trước khi xóa bất kỳ dữ liệu nào.
    """
    inventory = _collect_bucket_delete_inventory(client, bucket_name)
    current_item_count = inventory["deletable_item_count"]

    if current_item_count != expected_deletable_item_count:
        return {
            "ok": False,
            "service": "s3",
            "action": "delete_bucket_safe",
            "error": {
                "code": "BucketContentsChanged",
                "message": (
                    "Nội dung bucket đã thay đổi sau bước kiểm tra. "
                    "Hãy kiểm tra lại trước khi xác nhận xóa."
                ),
            },
            "data": {
                "bucket_name": bucket_name,
                "expected_deletable_item_count": (
                    expected_deletable_item_count
                ),
                "current_deletable_item_count": current_item_count,
            },
        }

    delete_candidates = inventory["delete_candidates"]

    for start in range(0, len(delete_candidates), 1000):
        batch = delete_candidates[start:start + 1000]
        response = client.delete_objects(
            Bucket=bucket_name,
            Delete={
                "Objects": batch,
                "Quiet": True,
            },
        )
        delete_errors = response.get("Errors", [])

        if delete_errors:
            return {
                "ok": False,
                "service": "s3",
                "action": "delete_bucket_safe",
                "error": {
                    "code": "ObjectDeleteFailed",
                    "message": (
                        "Không thể xóa hết nội dung bucket nên bucket chưa "
                        "được xóa."
                    ),
                    "details": delete_errors,
                },
            }

    client.delete_bucket(Bucket=bucket_name)

    return {
        "ok": True,
        "service": "s3",
        "action": "delete_bucket_safe",
        "data": {
            "bucket_name": bucket_name,
            "deleted_item_count": len(delete_candidates),
            "bucket_deleted": True,
        },
    }


def _put_lifecycle_rule(
    client,
    bucket_name,
    rule_id,
    prefix,
    transition_days,
    storage_class,
):
    """
    Thêm hoặc cập nhật một Lifecycle Rule mà không làm mất các rule khác.

    PutBucketLifecycleConfiguration thay thế toàn bộ cấu hình hiện tại, vì
    vậy phải đọc và hợp nhất các rule trước khi gửi lại cho S3.
    """
    try:
        current_configuration = client.get_bucket_lifecycle_configuration(
            Bucket=bucket_name,
        )
        existing_rules = current_configuration.get("Rules", [])
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        if error_code != "NoSuchLifecycleConfiguration":
            raise
        existing_rules = []

    new_rule = {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
        "Transitions": [
            {
                "Days": transition_days,
                "StorageClass": storage_class,
            }
        ],
    }

    merged_rules = []
    replaced_existing_rule = False

    for existing_rule in existing_rules:
        if existing_rule.get("ID") == rule_id:
            merged_rules.append(new_rule)
            replaced_existing_rule = True
        else:
            merged_rules.append(existing_rule)

    if not replaced_existing_rule:
        merged_rules.append(new_rule)

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={"Rules": merged_rules},
    )

    return {
        "ok": True,
        "service": "s3",
        "action": "put_lifecycle_rule",
        "data": {
            "bucket_name": bucket_name,
            "rule": new_rule,
            "replaced_existing_rule": replaced_existing_rule,
            "total_rule_count": len(merged_rules),
        },
    }


def _create_presigned_download(
    client,
    bucket_name,
    object_key,
    expires_in,
):
    """
    Tạo URL tải object S3 có thời hạn.

    Hàm này không làm bucket hoặc object trở thành public.
    """
    url = client.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket_name,
            "Key": object_key,
        },
        ExpiresIn=expires_in,
    )

    return {
        "ok": True,
        "service": "s3",
        "action": "create_presigned_download",
        "data": {
            "bucket_name": bucket_name,
            "object_key": object_key,
            "expires_in": expires_in,
            "url": url,
        },
    }

def execute(credentials, params):
    """
    Điểm vào chính của module S3.

    Dispatcher gọi hàm này với:
    - credentials: credential tạm thời từ STS hoặc None khi test.
    - params: action và tham số mà chatbot đã phân tích.
    """
    if not isinstance(params, dict):
        return {
            "ok": False,
            "service": "s3",
            "action": None,
            "error": {
                "code": "InvalidParameters",
                "message": "params phải là một dictionary.",
            },
        }

    action = params.get("action")
    region = params.get("region", "us-east-2")

    supported_actions = {
        "create_bucket",
        "create_presigned_download",
        "block_public_access",
        "scan_public_access",
        "inspect_bucket_for_delete",
        "delete_bucket_safe",
        "put_lifecycle_rule",
    }

    if action not in supported_actions:
        return {
            "ok": False,
            "service": "s3",
            "action": action,
            "error": {
                "code": "UnsupportedAction",
                "message": f"Hành động S3 không được hỗ trợ: {action}",
            },
        }

    bucket_name = params.get("bucket_name")

    if action != "scan_public_access" and not bucket_name:
        return {
            "ok": False,
            "service": "s3",
            "action": action,
            "error": {
                "code": "MissingBucketName",
                "message": "Thiếu tham số bucket_name.",
            },
        }

    if action == "create_presigned_download":
        object_key = params.get("object_key")
        expires_in = params.get("expires_in", 900)

        if not object_key:
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "MissingObjectKey",
                    "message": "Thiếu tham số object_key.",
                },
            }

        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidExpiration",
                    "message": "expires_in phải là số nguyên dương.",
                },
            }

    if action == "delete_bucket_safe":
        required_confirmation = f"DELETE {bucket_name}"
        confirmation = params.get("confirmation")
        expected_deletable_item_count = params.get(
            "expected_deletable_item_count"
        )

        if confirmation != required_confirmation:
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "ConfirmationRequired",
                    "message": (
                        "Để xóa bucket, confirmation phải chính xác là: "
                        f"{required_confirmation}"
                    ),
                },
            }

        if (
            not isinstance(expected_deletable_item_count, int)
            or isinstance(expected_deletable_item_count, bool)
            or expected_deletable_item_count < 0
        ):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidExpectedItemCount",
                    "message": (
                        "expected_deletable_item_count phải là số nguyên "
                        "không âm lấy từ bước kiểm tra bucket."
                    ),
                },
            }

    if action == "put_lifecycle_rule":
        transition_days = params.get("transition_days")
        storage_class = params.get("storage_class")
        prefix = params.get("prefix", "")
        rule_id = params.get(
            "rule_id",
            (
                f"sitabot-transition-{storage_class.lower()}-"
                f"{transition_days}-days"
                if isinstance(storage_class, str)
                else ""
            ),
        )

        if (
            not isinstance(transition_days, int)
            or isinstance(transition_days, bool)
            or transition_days < 1
        ):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidTransitionDays",
                    "message": "transition_days phải là số nguyên dương.",
                },
            }

        if not isinstance(storage_class, str):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidStorageClass",
                    "message": "storage_class không hợp lệ.",
                },
            }

        storage_class = storage_class.upper()

        if storage_class not in SUPPORTED_LIFECYCLE_STORAGE_CLASSES:
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidStorageClass",
                    "message": (
                        "storage_class phải là một trong: "
                        + ", ".join(
                            sorted(SUPPORTED_LIFECYCLE_STORAGE_CLASSES)
                        )
                    ),
                },
            }

        minimum_days = LIFECYCLE_MINIMUM_TRANSITION_DAYS.get(
            storage_class,
            1,
        )

        if transition_days < minimum_days:
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "TransitionTooEarly",
                    "message": (
                        f"{storage_class} yêu cầu transition_days tối thiểu "
                        f"là {minimum_days}."
                    ),
                },
            }

        if not isinstance(prefix, str):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidPrefix",
                    "message": "prefix phải là chuỗi.",
                },
            }

        if (
            not isinstance(rule_id, str)
            or not rule_id.strip()
            or len(rule_id) > 255
        ):
            return {
                "ok": False,
                "service": "s3",
                "action": action,
                "error": {
                    "code": "InvalidRuleId",
                    "message": (
                        "rule_id phải là chuỗi từ 1 đến 255 ký tự."
                    ),
                },
            }

    try:
        client = _create_s3_client(credentials, region)

        if action == "scan_public_access":
            return _scan_public_access(client)

        if action == "inspect_bucket_for_delete":
            return _inspect_bucket_for_delete(client, bucket_name)

        if action == "delete_bucket_safe":
            return _delete_bucket_safe(
                client,
                bucket_name,
                expected_deletable_item_count,
            )

        if action == "put_lifecycle_rule":
            return _put_lifecycle_rule(
                client,
                bucket_name,
                rule_id,
                prefix,
                transition_days,
                storage_class,
            )

        if action == "create_bucket":
            return _create_bucket(
                client,
                bucket_name,
                region,
            )

        if action == "block_public_access":
            return _block_public_access(
                client,
                bucket_name,
            )

        return _create_presigned_download(
            client,
            bucket_name,
            object_key,
            expires_in,
        )

    except ClientError as error:
        error_details = error.response.get("Error", {})

        return {
            "ok": False,
            "service": "s3",
            "action": action,
            "error": {
                "code": error_details.get("Code", "AwsClientError"),
                "message": error_details.get(
                    "Message",
                    "AWS từ chối yêu cầu S3.",
                ),
            },
        }

    except (KeyError, TypeError, ValueError) as error:
        return {
            "ok": False,
            "service": "s3",
            "action": action,
            "error": {
                "code": "InvalidParameters",
                "message": str(error),
            },
        }

