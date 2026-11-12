from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from scripts.manual_test_s3 import (
    require_expected_identity,
    require_safe_environment,
    wait_for_lifecycle_rule,
)


def test_safe_environment_requires_profile_and_expected_account(monkeypatch):
    monkeypatch.setenv("RUN_AWS_INTEGRATION", "1")
    monkeypatch.setenv("AWS_PROFILE", "example-profile")
    monkeypatch.setenv("AWS_EXPECTED_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv(
        "AWS_EXPECTED_PRINCIPAL_ARN", "arn:aws:iam::123456789012:user/ExampleUser"
    )
    assert require_safe_environment() == (
        "example-profile",
        "123456789012",
        "arn:aws:iam::123456789012:user/ExampleUser",
        "sitabot-s3-test-",
    )

    monkeypatch.setenv("AWS_TEST_BUCKET_PREFIX", "sitabot-example-test-")
    assert require_safe_environment()[-1] == "sitabot-example-test-"
    monkeypatch.setenv("AWS_TEST_BUCKET_PREFIX", "production-")
    with pytest.raises(RuntimeError, match="AWS_TEST_BUCKET_PREFIX"):
        require_safe_environment()
    monkeypatch.delenv("AWS_TEST_BUCKET_PREFIX")

    monkeypatch.delenv("AWS_EXPECTED_ACCOUNT_ID")
    with pytest.raises(RuntimeError, match="AWS_EXPECTED_ACCOUNT_ID"):
        require_safe_environment()

    monkeypatch.setenv("AWS_EXPECTED_ACCOUNT_ID", "123456789012")
    monkeypatch.delenv("AWS_EXPECTED_PRINCIPAL_ARN")
    with pytest.raises(RuntimeError, match="AWS_EXPECTED_PRINCIPAL_ARN"):
        require_safe_environment()


def test_expected_identity_rejects_wrong_account_or_user():
    expected_account = "123456789012"
    expected_arn = f"arn:aws:iam::{expected_account}:user/ExampleUser"
    require_expected_identity(
        {
            "Account": expected_account,
            "Arn": expected_arn,
        },
        expected_account,
        expected_arn,
    )

    for account, user in [("999999999999", "ExampleUser"), (expected_account, "OtherUser")]:
        with pytest.raises(RuntimeError, match="No S3 resource was created"):
            require_expected_identity(
                {"Account": account, "Arn": f"arn:aws:iam::{account}:user/{user}"},
                expected_account,
                expected_arn,
            )


def test_wait_for_lifecycle_rule_retries_until_visible():
    rule = {"ID": "archive-test", "Transitions": [{"Days": 30}]}
    client = Mock()
    client.get_bucket_lifecycle_configuration.side_effect = [
        ClientError(
            {
                "Error": {
                    "Code": "NoSuchLifecycleConfiguration",
                    "Message": "The lifecycle configuration does not exist.",
                }
            },
            "GetBucketLifecycleConfiguration",
        ),
        {"Rules": [rule]},
    ]

    with patch("scripts.manual_test_s3.time.sleep") as sleep:
        result = wait_for_lifecycle_rule(client, "test-bucket", "archive-test")

    assert result == rule
    assert client.get_bucket_lifecycle_configuration.call_count == 2
    sleep.assert_called_once()
