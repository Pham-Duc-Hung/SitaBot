"""
Author: Trịnh Thiên Quang
Ký IAM SigV4 và gửi kết quả tác vụ tới AppSync GraphQL.
"""

import json
import os
import urllib.request
from urllib.parse import urlsplit

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from backend.shared.cloud_errors import BackendError

MUTATION = '''mutation PublishActionResult($event: ActionResultInput!) {
  publishActionResult(event: $event) {
    user_id session_id action_id created_at service tool_name status message data
  }
}'''


def publish(event):
    endpoint = os.environ.get("APPSYNC_GRAPHQL_URL", "")
    host = urlsplit(endpoint)
    # Restrict SigV4 credential use to the AWS AppSync endpoint family.
    if host.scheme != "https" or not host.hostname or not host.hostname.endswith(".amazonaws.com") or ".appsync-api." not in host.hostname or host.path != "/graphql":
        raise BackendError("AppSyncNotConfigured", "Chưa cấu hình endpoint AppSync hợp lệ.", 503)
    data = json.dumps({"query": MUTATION, "variables": {"event": {**event, "data": json.dumps(event.get("data", {}), ensure_ascii=False, default=str)}}}, ensure_ascii=False).encode()
    credentials = boto3.Session().get_credentials()
    if not credentials:
        raise BackendError("AppSyncUnavailable", "Thiếu IAM credentials cho AppSync.", 503)
    region = host.hostname.split(".appsync-api.", 1)[1].split(".")[0]
    request = AWSRequest(method="POST", url=endpoint, data=data, headers={"Content-Type": "application/json"})
    SigV4Auth(credentials.get_frozen_credentials(), "appsync", region).add_auth(request)
    with urllib.request.urlopen(urllib.request.Request(endpoint, data=data, headers=dict(request.headers), method="POST"), timeout=5) as reply:
        result = json.loads(reply.read(131073))
    if result.get("errors") or not result.get("data", {}).get("publishActionResult"):
        raise BackendError("AppSyncUnavailable", "Không phát được kết quả AppSync.", 502)
