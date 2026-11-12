"""
Author: Trịnh Thiên Quang
Quản lý cấu hình runtime và che thông tin nhạy cảm của Lambda backend.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from typing import Any, Callable, Dict, Optional
from botocore.exceptions import ClientError


DEFAULT_REGION = "us-east-1"
EMF_NAMESPACE = "SatiBot/Backend"
EMF_METRIC_NAME = "PlatformFault"
ACCESS_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AuthorizationError",
        "AccessDeniedByOrganizations",
    }
)
PLATFORM_FAULT_CODES = frozenset(
    {
        "AccessDenied",
        "ActionStoreUnavailable",
        "AppSyncAccessDenied",
        "AppSyncNotConfigured",
        "BackendNotConfigured",
        "BackendUnavailable",
        "ConfigurationError",
        "GeminiNotConfigured",
        "GeminiSecretAccessDenied",
        "GeminiSecretUnavailable",
        "HistoryUnavailable",
        "InvalidToolConfiguration",
        "WorkflowNotConfigured",
    }
)
_MAX_CACHE_ENTRIES = 64
_CLIENT_CACHE: Dict[tuple, Any] = {}
_TABLE_CACHE: Dict[tuple, Any] = {}
_SESSION_CACHE: Dict[tuple, Any] = {}
_CACHE_STATS: Dict[str, int] = {"hits": 0, "misses": 0}
_FAULT_COUNTS: Dict[str, int] = {}
_emf_logger = logging.getLogger("satibot.emf")
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
            key: "<redacted>"
            if str(key).lower() in _SENSITIVE_KEYS
            else redact_sensitive(item)
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
    digest = hashlib.sha256(f"{identity}:{discriminator}".encode("utf-8")).hexdigest()[
        :20
    ]
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
        {
            "AccessKeyId": access_key,
            "SessionToken": os.environ.get("AWS_SESSION_TOKEN", ""),
        }
        if access_key
        else None
    )
    return (get_aws_region(explicit_region), credential_fingerprint(creds))


def _config_signature(config: Any) -> Any:
    if config is None:
        return None
    attrs = (
        "connect_timeout",
        "read_timeout",
        "region_name",
        "max_pool_connections",
        "signature_version",
        "user_agent_extra",
        "retries",
        "s3",
        "tcp_keepalive",
    )
    if any(hasattr(config, attr) for attr in attrs):
        extracted = {attr: getattr(config, attr, None) for attr in attrs}
        return json.dumps(extracted, sort_keys=True, default=str)
    return ("custom", id(config), repr(config))


def _store_bounded(cache: Dict[tuple, Any], key: tuple, value: Any) -> Any:
    if key not in cache and len(cache) >= _MAX_CACHE_ENTRIES:
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
    """Reuse Boto3 clients across warm Lambda invocations while respecting config and monkeypatches."""
    key = (
        client_factory,
        service_name,
        _env_cache_scope(region_name),
        config_tag,
        _config_signature(config),
    )
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


def emit_platform_fault(
    function_name: str, error_code: str, *, context=None
) -> Dict[str, Any]:
    """Emit CloudWatch Embedded Metric Format (EMF) signal for platform/deployment faults."""
    fn = str(function_name or "unknown")[:64]
    code = str(error_code or "PlatformFault")[:128]
    version = os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "").strip()
    _FAULT_COUNTS[f"{fn}:{code}"] = _FAULT_COUNTS.get(f"{fn}:{code}", 0) + 1
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    "Dimensions": [["FunctionName"]],
                    "Metrics": [{"Name": EMF_METRIC_NAME, "Unit": "Count"}],
                }
            ],
        },
        "Function": fn,
        "FunctionName": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", fn),
        "FunctionVersion": version or "local",
        "error_code": code,
        EMF_METRIC_NAME: 1,
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    invoked_arn = getattr(context, "invoked_function_arn", "")
    # Only requests through the monitored alias may contribute to its rollback alarm.
    if (
        os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        and version != "$LATEST"
        and isinstance(invoked_arn, str)
        and invoked_arn.endswith(":live")
    ):
        print(line, file=sys.stdout, flush=True)
    else:
        _emf_logger.debug(line)
    return payload


def clear_runtime_caches() -> None:
    """Reset warm-invocation client, table, session caches, and platform fault counters."""
    _CLIENT_CACHE.clear()
    _TABLE_CACHE.clear()
    _SESSION_CACHE.clear()
    _FAULT_COUNTS.clear()
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
        "platform_faults": sum(_FAULT_COUNTS.values()),
    }


def platform_fault_counts() -> Dict[str, int]:
    """Return recorded platform fault counts keyed by '<function>:<error_code>'."""
    return dict(_FAULT_COUNTS)


def sanitize_model_data(value):
    """Remove signed links from model input without changing the public result."""
    value = redact_sensitive(value)
    if isinstance(value, dict):
        return {key: sanitize_model_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_model_data(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r'https?://[^\s<>"\\]+',
            lambda match: "[signed URL withheld]" if re.search(
                r"x-amz-(signature|credential|security-token)=|[?&]signature=",
                match.group(), re.IGNORECASE,
            ) else match.group(),
            value,
        )
    return value
