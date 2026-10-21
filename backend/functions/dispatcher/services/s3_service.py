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

    client.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

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

    if action != "create_bucket":
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

    try:
        client = _create_s3_client(credentials, region)
        return _create_bucket(client, bucket_name, region)

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