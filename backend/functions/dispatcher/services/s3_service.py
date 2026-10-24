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

    try:
        client = _create_s3_client(credentials, region)

        if action == "scan_public_access":
            return _scan_public_access(client)

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

