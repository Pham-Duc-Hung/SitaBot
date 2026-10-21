import boto3
from botocore.exceptions import ClientError


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

    if not bucket_name:
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

