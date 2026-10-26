"""Cấu hình runtime và tiện ích an toàn dùng chung cho Lambda backend."""

import hashlib
import json
import os
from typing import Any, Dict, Optional


DEFAULT_REGION = "us-east-1"
_SENSITIVE_KEYS = {
    "authorization",
    "aws_secret_access_key",
    "aws_session_token",
    "secretaccesskey",
    "sessiontoken",
    "llm_api_key",
    "api_key",
    "token",
}


def get_aws_region(explicit_region: Optional[str] = None) -> str:
    """Resolve region consistently for local PoC and deployed Lambda runtimes."""
    return (
        (explicit_region or "").strip()
        or os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        or DEFAULT_REGION
    )


def get_default_lambda_role_arn() -> Optional[str]:
    """Return the opt-in PoC execution role without embedding an AWS account in code."""
    value = os.environ.get("DEFAULT_LAMBDA_ROLE_ARN", "").strip()
    return value or None


def redact_sensitive(value: Any) -> Any:
    """Recursively redact secrets before structured request data reaches logs."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if str(key).lower() in _SENSITIVE_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value


def safe_json_for_log(value: Any) -> str:
    """Serialize redacted structured data for logs."""
    return json.dumps(redact_sensitive(value), ensure_ascii=False, default=str)


def credential_fingerprint(credentials: Optional[Dict[str, Any]]) -> str:
    """Build a non-reversible cache identity without retaining secrets in cache keys."""
    if not credentials:
        return "default"
    identity = credentials.get("AccessKeyId") or credentials.get("RoleArn") or "default"
    discriminator = (
        credentials.get("SessionToken", "")
        if credentials.get("AccessKeyId")
        else credentials.get("ExternalId", "")
    )
    digest = hashlib.sha256(f"{identity}:{discriminator}".encode("utf-8")).hexdigest()[:20]
    return f"credential:{digest}"
