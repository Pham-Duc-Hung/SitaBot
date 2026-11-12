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
        "AuthStack": {
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

    if not api_enabled:
        with pytest.raises(RuntimeError, match="routing"):
            publisher.publish_site(session, "primary")
        s3_client.put_object.assert_not_called()
        return
    publisher.publish_site(session, "primary", "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql")

    config = next(call.kwargs for call in s3_client.put_object.call_args_list
                  if call.kwargs["Key"] == "site-config.json")
    assert json.loads(config["Body"]) == {
        "api_enabled": api_enabled,
        "tools_loaded": 2,
        "appsync": {"graphql_url": "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql"},
        "auth": {
            "domain": "example.auth.ap-southeast-1.amazoncognito.com",
            "client_id": "example-client",
            "redirect_uri": "https://example.cloudfront.net/",
        },
    }
    assert config["CacheControl"] == "no-cache"
    cloudfront_client.get_distribution_config.assert_called_once_with(Id="preview-distribution")


def test_primary_site_config_publishes_only_valid_appsync_endpoint(monkeypatch, tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<title>Preview</title>", encoding="utf-8")
    monkeypatch.setattr(publisher, "site_files", lambda site: [(page, "index.html")])
    monkeypatch.setattr(publisher, "stack_outputs", lambda _session, _region, stack_name: {
        "PrimaryBucketName": "preview-bucket",
        "PrimaryDistributionId": "preview-distribution",
        "PrimaryDistributionDomain": "example.cloudfront.net",
    })
    monkeypatch.setattr(publisher, "preview_auth_config", lambda *_: {"client_id": "client"})
    s3_client = Mock()
    cloudfront_client = Mock()
    cloudfront_client.get_distribution_config.return_value = {"DistributionConfig": {"CacheBehaviors": {"Items": [{"PathPattern": "api/*"}]}}}
    cloudfront_client.create_invalidation.return_value = {"Invalidation": {"Id": "one"}}
    session = Mock()
    session.client.side_effect = lambda service, **_: {"s3": s3_client, "cloudfront": cloudfront_client}[service]
    endpoint = "https://abc123.appsync-api.ap-southeast-1.amazonaws.com/graphql"

    publisher.publish_site(session, "primary", endpoint)
    config = next(call.kwargs for call in s3_client.put_object.call_args_list
                  if call.kwargs["Key"] == "site-config.json")
    assert json.loads(config["Body"])["appsync"] == {"graphql_url": endpoint}
    with pytest.raises(ValueError, match="AppSync URL"):
        publisher.publish_site(session, "primary", "https://other.example/graphql")

def test_fallback_upload_invalidates_the_single_primary_distribution(monkeypatch, tmp_path):
    page = tmp_path / "error.html"
    page.write_text("<h1>Unavailable</h1>")
    monkeypatch.setattr(publisher, "site_files", lambda site: [(page, "index.html")])
    def outputs(session, region, stack):
        if stack == "SatiBotWebErrorPage":
            assert region == "us-east-1"
            return {"ErrorPageBucketName": "error-bucket"}
        assert stack == "SatiBotWebPrimary"
        return {"PrimaryDistributionId": "shared", "PrimaryDistributionDomain": "shared.cloudfront.net"}
    monkeypatch.setattr(publisher, "stack_outputs", outputs)
    storage, cdn = Mock(), Mock()
    cdn.create_invalidation.return_value = {"Invalidation": {"Id": "one"}}
    session = Mock()
    session.client.side_effect = lambda service, **kwargs: storage if service == "s3" else cdn
    publisher.publish_site(session, "error")
    assert storage.put_object.call_args.kwargs["Bucket"] == "error-bucket"
    assert storage.put_object.call_args.kwargs["CacheControl"] == "no-store"
    assert cdn.create_invalidation.call_args.kwargs["DistributionId"] == "shared"
