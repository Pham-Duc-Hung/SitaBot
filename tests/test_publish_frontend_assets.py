"""Verify the published preview matches the CloudFront API routing state."""

import json
from unittest.mock import Mock

import pytest

from scripts import publish_frontend_assets as publisher


@pytest.mark.parametrize("api_enabled", [False, True])
def test_primary_site_config_uses_deployed_cloudfront_behavior(monkeypatch, tmp_path, api_enabled):
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><title>Preview</title>", encoding="utf-8")
    monkeypatch.setattr(publisher, "site_files", lambda site: [(page, "index.html")])
    monkeypatch.setattr(publisher, "registry", lambda: {"example_one": {}, "example_two": {}})
    monkeypatch.setattr(publisher, "stack_outputs", lambda _session, _region, stack_name: {
        "SatiBotWebPrimary": {
            "PrimaryBucketName": "preview-bucket",
            "PrimaryDistributionId": "preview-distribution",
            "PrimaryDistributionDomain": "example.cloudfront.net",
        },
        "SatiBotWebAuth": {
            "CallbackUrl": "https://example.cloudfront.net/",
            "HostedDomain": "example.auth.ap-southeast-1.amazoncognito.com",
            "UserPoolClientId": "example-client",
        },
    }[stack_name])

    s3_client = Mock()
    cloudfront_client = Mock()
    cloudfront_client.get_distribution_config.return_value = {
        "DistributionConfig": {"CacheBehaviors": {
            "Items": [{"PathPattern": "api/*"}] if api_enabled else []
        }}
    }
    cloudfront_client.create_invalidation.return_value = {"Invalidation": {"Id": "invalidation-1"}}
    session = Mock()
    session.client.side_effect = lambda service, **_: {
        "s3": s3_client, "cloudfront": cloudfront_client
    }[service]

    publisher.publish_site(session, "primary")

    config = next(call.kwargs for call in s3_client.put_object.call_args_list
                  if call.kwargs["Key"] == "site-config.json")
    assert json.loads(config["Body"]) == {
        "api_enabled": api_enabled,
        "tools_loaded": 2,
        "auth": {
            "domain": "example.auth.ap-southeast-1.amazoncognito.com",
            "client_id": "example-client",
            "redirect_uri": "https://example.cloudfront.net/",
        },
    }
    assert config["CacheControl"] == "no-cache"
    cloudfront_client.get_distribution_config.assert_called_once_with(Id="preview-distribution")
