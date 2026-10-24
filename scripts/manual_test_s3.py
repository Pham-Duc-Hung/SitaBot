import os
import uuid
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen
from xml.etree import ElementTree

import boto3
from botocore.exceptions import ClientError

from backend.functions.dispatcher.services.s3_service import execute


REGION = "us-east-2"
BUCKET_PREFIX = "sitabot-hung-test-"
OBJECT_KEY = "demo/hello.txt"
OBJECT_CONTENT = b"Hello from SitaBot S3 integration test!"


def require_safe_environment():
    """
    Ngăn script chạy nhầm bằng profile khác hoặc chạy ngoài ý muốn.
    """
    if os.getenv("RUN_AWS_INTEGRATION") != "1":
        raise RuntimeError(
            "Integration test is disabled. "
            "Set RUN_AWS_INTEGRATION=1 before running."
        )

    profile = os.getenv("AWS_PROFILE")

    if profile != "sitabot-dev":
        raise RuntimeError(
            "AWS_PROFILE phải là sitabot-dev."
        )


def require_success(result):
    """
    Dừng test nếu s3_service trả về lỗi.
    """
    if not result.get("ok"):
        raise RuntimeError(
            f"S3 action failed: {result.get('error')}"
        )

    return result["data"]


def cleanup(s3_client, bucket_name):
    """
    Cố gắng xóa object và bucket dù test thành công hay thất bại.
    """
    print("Cleaning up test resources...")

    try:
        s3_client.delete_object(
            Bucket=bucket_name,
            Key=OBJECT_KEY,
        )
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")

        if error_code not in {"NoSuchBucket", "NoSuchKey", "404"}:
            print(f"Warning: could not delete object: {error_code}")

    try:
        s3_client.delete_bucket(
            Bucket=bucket_name,
        )
        print(f"Deleted bucket: {bucket_name}")
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")

        if error_code not in {"NoSuchBucket", "404"}:
            print(
                "IMPORTANT: bucket cleanup failed. "
                f"Delete it manually: {bucket_name}. "
                f"Error: {error_code}"
            )


def main():
    require_safe_environment()

    # Tên ngẫu nhiên vì tên S3 bucket phải duy nhất trên toàn AWS.
    random_suffix = uuid.uuid4().hex[:12]
    bucket_name = f"{BUCKET_PREFIX}{random_suffix}"

    session = boto3.Session()
    sts_client = session.client("sts", region_name=REGION)
    s3_client = session.client("s3", region_name=REGION)

    identity = sts_client.get_caller_identity()
    identity_name = identity["Arn"].rsplit("/", 1)[-1]

    if identity_name != "PhamHung":
        raise RuntimeError(
            f"Unexpected AWS identity: {identity_name}"
        )

    print(f"AWS identity: {identity_name}")
    print(f"Test bucket: {bucket_name}")

    try:
        print("1. Creating a safe bucket...")

        create_result = execute(
            credentials=None,
            params={
                "action": "create_bucket",
                "bucket_name": bucket_name,
                "region": REGION,
            },
        )

        require_success(create_result)

        print("2. Verifying Block Public Access...")

        public_access = s3_client.get_public_access_block(
            Bucket=bucket_name,
        )

        configuration = public_access[
            "PublicAccessBlockConfiguration"
        ]

        expected_configuration = {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }

        if configuration != expected_configuration:
            raise RuntimeError(
                f"Unexpected public access configuration: "
                f"{configuration}"
            )

        print("2.1. Scanning account buckets for public access risks...")

        scan_result = execute(
            credentials=None,
            params={
                "action": "scan_public_access",
                "region": REGION,
            },
        )

        scan_data = require_success(scan_result)
        risky_bucket_names = {
            item["bucket_name"]
            for item in scan_data["risky_buckets"]
        }

        if bucket_name in risky_bucket_names:
            raise RuntimeError(
                "The newly created safe bucket was incorrectly marked risky."
            )

        scan_error_bucket_names = {
            item["bucket_name"]
            for item in scan_data["scan_errors"]
        }

        if bucket_name in scan_error_bucket_names:
            raise RuntimeError(
                "The public access scan could not inspect the test bucket."
            )

        print("3. Calling block_public_access again...")

        block_result = execute(
            credentials=None,
            params={
                "action": "block_public_access",
                "bucket_name": bucket_name,
                "region": REGION,
            },
        )

        require_success(block_result)

        print("4. Uploading a small demo object...")

        s3_client.put_object(
            Bucket=bucket_name,
            Key=OBJECT_KEY,
            Body=OBJECT_CONTENT,
            ContentType="text/plain",
        )
        print("4.1. Downloading directly with Boto3...")

        direct_response = s3_client.get_object(
            Bucket=bucket_name,
            Key=OBJECT_KEY,
        )

        direct_content = direct_response["Body"].read()

        if direct_content != OBJECT_CONTENT:
            raise RuntimeError(
                "Direct Boto3 download returned incorrect content."
            )
        print("5. Creating a presigned download URL...")

        presigned_result = execute(
            credentials=None,
            params={
                "action": "create_presigned_download",
                "bucket_name": bucket_name,
                "object_key": OBJECT_KEY,
                "expires_in": 900,
                "region": REGION,
            },
        )

        presigned_data = require_success(presigned_result)

        # Không in URL ra terminal vì URL chứa chữ ký tạm thời.
        print("6. Downloading the object through the signed URL...")

        presigned_url = presigned_data["url"]
        original_host = urlparse(presigned_url).hostname

        # Chỉ in hostname, không in toàn bộ URL chứa chữ ký.
        print(f"Presigned URL host: {original_host}")

        try:
            with urlopen(presigned_url, timeout=30) as response:
                downloaded_content = response.read()

        except HTTPError as error:
            error_body = error.read()
            error_code = error.reason
            error_message = ""
            error_region = ""

            try:
                root = ElementTree.fromstring(error_body)
                error_code = root.findtext("Code") or error_code
                error_message = root.findtext("Message") or ""
                error_region = root.findtext("Region") or ""
            except ElementTree.ParseError:
                pass

            final_host = urlparse(error.url).hostname

            raise RuntimeError(
                "Presigned download failed: "
                f"HTTP={error.code}, "
                f"code={error_code}, "
                f"message={error_message}, "
                f"region={error_region}, "
                f"original_host={original_host}, "
                f"final_host={final_host}"
            ) from error

        if downloaded_content != OBJECT_CONTENT:
            raise RuntimeError(
                "Downloaded content does not match uploaded content."
            )

        print("7. Applying and verifying an S3 Lifecycle Rule...")

        lifecycle_rule_id = "sitabot-test-standard-ia-transition"
        lifecycle_result = execute(
            credentials=None,
            params={
                "action": "put_lifecycle_rule",
                "bucket_name": bucket_name,
                "rule_id": lifecycle_rule_id,
                "prefix": "archive/",
                "transition_days": 30,
                "storage_class": "STANDARD_IA",
                "region": REGION,
            },
        )
        require_success(lifecycle_result)

        lifecycle_configuration = (
            s3_client.get_bucket_lifecycle_configuration(
                Bucket=bucket_name,
            )
        )
        lifecycle_rules = lifecycle_configuration.get("Rules", [])
        matching_rules = [
            rule
            for rule in lifecycle_rules
            if rule.get("ID") == lifecycle_rule_id
        ]

        if len(matching_rules) != 1:
            raise RuntimeError(
                "The Lifecycle Rule was not stored on the test bucket."
            )

        transition = matching_rules[0].get("Transitions", [{}])[0]
        if (
            transition.get("Days") != 30
            or transition.get("StorageClass") != "STANDARD_IA"
        ):
            raise RuntimeError(
                f"Unexpected Lifecycle transition: {transition}"
            )

        print("8. Inspecting the bucket before safe deletion...")

        inspect_result = execute(
            credentials=None,
            params={
                "action": "inspect_bucket_for_delete",
                "bucket_name": bucket_name,
                "region": REGION,
            },
        )
        inspect_data = require_success(inspect_result)

        if inspect_data["object_count"] != 1:
            raise RuntimeError(
                "Safe-delete inspection did not find the demo object."
            )

        rejected_delete = execute(
            credentials=None,
            params={
                "action": "delete_bucket_safe",
                "bucket_name": bucket_name,
                "confirmation": "yes",
                "expected_deletable_item_count": (
                    inspect_data["deletable_item_count"]
                ),
                "region": REGION,
            },
        )

        if rejected_delete.get("error", {}).get("code") != (
            "ConfirmationRequired"
        ):
            raise RuntimeError(
                "Safe deletion accepted an invalid confirmation."
            )

        print("9. Deleting the test bucket with explicit confirmation...")

        delete_result = execute(
            credentials=None,
            params={
                "action": "delete_bucket_safe",
                "bucket_name": bucket_name,
                "confirmation": inspect_data["required_confirmation"],
                "expected_deletable_item_count": (
                    inspect_data["deletable_item_count"]
                ),
                "region": REGION,
            },
        )
        delete_data = require_success(delete_result)

        if not delete_data["bucket_deleted"]:
            raise RuntimeError("Safe deletion did not delete the test bucket.")

        print("Integration test passed successfully.")

    finally:
        cleanup(s3_client, bucket_name)


if __name__ == "__main__":
    main()
