"""
Author: Trịnh Thiên Quang
Quản lý cấu hình runtime và che thông tin nhạy cảm của Lambda backend.
"""

import hashlib
import json
import os
from typing import Any, Callable, Dict, Optional
from botocore.exceptions import ClientError


DEFAULT_REGION = "us-east-1"
ACCESS_DENIED_CODES = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
    "AccessDeniedByOrganizations",
})
_MAX_CACHE_ENTRIES = 64
_CLIENT_CACHE: Dict[tuple, Any] = {}
_TABLE_CACHE: Dict[tuple, Any] = {}
_SESSION_CACHE: Dict[tuple, Any] = {}
_CACHE_STATS: Dict[str, int] = {"hits": 0, "misses": 0}
_SENSITIVE_KEYS = {
    "authorization",
    "credentials",
    "secret_access_key",
    "aws_access_key_id",
    "accesskeyid",
    "external_id",
    "externalid",
    "password",
    "privatekey",
    "keymaterial",
    "environment",
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


def is_access_denied(exc: BaseException) -> bool:
    """Identify IAM/STS/resource policy permission denials without exposing raw AWS errors."""
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in ACCESS_DENIED_CODES


def _env_cache_scope(explicit_region: Optional[str] = None) -> tuple:
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    creds = (
        {"AccessKeyId": access_key, "SessionToken": os.environ.get("AWS_SESSION_TOKEN", "")}
        if access_key
        else None
    )
    return (get_aws_region(explicit_region), credential_fingerprint(creds))


def _store_bounded(cache: Dict[tuple, Any], key: tuple, value: Any) -> Any:
    if len(cache) >= _MAX_CACHE_ENTRIES:
        oldest = next(iter(cache))
        cache.pop(oldest, None)
    cache[key] = value
    return value


def cached_boto_client(
    client_factory: Callable[..., Any],
    service_name: str,
    *,
    region_name: Optional[str] = None,
    config: Any = None,
    config_tag: str = "default",
) -> Any:
    """Reuse Boto3 clients across warm Lambda invocations while respecting factory monkeypatches."""
    key = (client_factory, service_name, _env_cache_scope(region_name), config_tag)
    if key in _CLIENT_CACHE:
        _CACHE_STATS["hits"] += 1
        return _CLIENT_CACHE[key]
    _CACHE_STATS["misses"] += 1
    kwargs: Dict[str, Any] = {}
    if region_name is not None:
        kwargs["region_name"] = region_name
    if config is not None:
        kwargs["config"] = config
    return _store_bounded(_CLIENT_CACHE, key, client_factory(service_name, **kwargs))


def cached_dynamodb_table(resource_factory: Callable[..., Any], table_name: str) -> Any:
    """Reuse DynamoDB Table objects across warm Lambda invocations."""
    key = (resource_factory, table_name, _env_cache_scope())
    if key in _TABLE_CACHE:
        _CACHE_STATS["hits"] += 1
        return _TABLE_CACHE[key]
    _CACHE_STATS["misses"] += 1
    table_obj = resource_factory("dynamodb").Table(table_name)
    return _store_bounded(_TABLE_CACHE, key, table_obj)


def cached_boto_session(session_factory: Callable[..., Any]) -> Any:
    """Reuse Boto3 Session instances for warm SigV4 signing."""
    key = (session_factory, _env_cache_scope())
    if key in _SESSION_CACHE:
        _CACHE_STATS["hits"] += 1
        return _SESSION_CACHE[key]
    _CACHE_STATS["misses"] += 1
    return _store_bounded(_SESSION_CACHE, key, session_factory())


def clear_runtime_caches() -> None:
    """Reset warm-invocation client, table, and session caches."""
    _CLIENT_CACHE.clear()
    _TABLE_CACHE.clear()
    _SESSION_CACHE.clear()
    _CACHE_STATS["hits"] = 0
    _CACHE_STATS["misses"] = 0


def runtime_cache_stats() -> Dict[str, int]:
    """Return warm-invocation cache hit/miss counters and active entry counts."""
    return {
        "hits": _CACHE_STATS["hits"],
        "misses": _CACHE_STATS["misses"],
        "clients": len(_CLIENT_CACHE),
        "tables": len(_TABLE_CACHE),
        "sessions": len(_SESSION_CACHE),
    }
