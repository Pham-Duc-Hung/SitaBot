from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts.manual_test_s3 import wait_for_lifecycle_rule


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
