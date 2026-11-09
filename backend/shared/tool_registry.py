"""
Author: Trịnh Thiên Quang
Tổng hợp schema công cụ của các dịch vụ AWS.
Kiểm tra tham số tool call trước khi điều phối thực thi.
"""

import hashlib
import importlib
import json
from copy import deepcopy

from jsonschema import Draft7Validator
from backend.shared.cloud_errors import BackendError

SOURCES = (
    ("lambda", "backend.functions.dispatcher.services.lambda_service", "LAMBDA_TOOL_SCHEMAS"),
    ("ec2", "backend.functions.dispatcher.services.ec2_service", "EC2_TOOL_SCHEMAS"),
    ("s3", "backend.functions.dispatcher.services.s3_service", "S3_TOOL_SCHEMAS"),
    ("iam", "backend.functions.dispatcher.services.iam_service", "IAM_TOOL_SCHEMAS"),
)

_CACHE_SIGNATURE = None
_REGISTRY_CACHE = None
_VALIDATOR_CACHE = {}
_LLM_TOOLS_CACHE = None
_REGISTRY_STATS = {"hits": 0, "misses": 0}


def _ensure_cache():
    global _CACHE_SIGNATURE, _REGISTRY_CACHE, _VALIDATOR_CACHE, _LLM_TOOLS_CACHE
    loaded = []
    for service, module, constant in SOURCES:
        raw_items = getattr(importlib.import_module(module), constant, ())
        loaded.append((service, raw_items))
    signature = hashlib.sha256(
        json.dumps(loaded, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if _REGISTRY_CACHE is not None and _CACHE_SIGNATURE == signature:
        _REGISTRY_STATS["hits"] += 1
        return _REGISTRY_CACHE
    _REGISTRY_STATS["misses"] += 1
    result = {}
    validators = {}
    for service, raw_items in loaded:
        for raw in raw_items:
            tool = deepcopy(raw)
            tool["service"] = service
            tool["action"] = tool.get("action", tool["name"])
            tool["parameters"] = tool.get("parameters", tool.get("input_schema", {}))
            tool["parameters"].setdefault("additionalProperties", False)
            # Missing confirmation metadata fails closed.
            tool["requires_confirmation"] = tool.get("requires_confirmation", True)
            if tool["name"] in result:
                raise BackendError("InvalidToolConfiguration", "Trùng tên công cụ.", 503)
            result[tool["name"]] = tool
            validators[tool["name"]] = Draft7Validator(tool["parameters"])
    _CACHE_SIGNATURE = signature
    _REGISTRY_CACHE = result
    _VALIDATOR_CACHE = validators
    _LLM_TOOLS_CACHE = tuple(
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in result.values()
    )
    return _REGISTRY_CACHE


def clear_registry_cache():
    global _CACHE_SIGNATURE, _REGISTRY_CACHE, _VALIDATOR_CACHE, _LLM_TOOLS_CACHE
    _CACHE_SIGNATURE = None
    _REGISTRY_CACHE = None
    _VALIDATOR_CACHE = {}
    _LLM_TOOLS_CACHE = None
    _REGISTRY_STATS["hits"] = 0
    _REGISTRY_STATS["misses"] = 0


def registry_cache_stats():
    return {
        "hits": _REGISTRY_STATS["hits"],
        "misses": _REGISTRY_STATS["misses"],
        "tool_count": len(_REGISTRY_CACHE or {}),
    }


def registry():
    return deepcopy(_ensure_cache())


def validate_call(name, parameters):
    tools = _ensure_cache()
    tool = tools.get(name) if isinstance(name, str) else None
    if not tool:
        raise BackendError("UnsupportedTool", "Công cụ chưa có schema được đăng ký.")
    validator = _VALIDATOR_CACHE[name]
    if not isinstance(parameters, dict) or not validator.is_valid(parameters):
        raise BackendError("InvalidParameters", "Tham số công cụ không hợp lệ; hãy bổ sung hoặc sửa yêu cầu.")
    return deepcopy(tool)


def llm_tools():
    _ensure_cache()
    return deepcopy(list(_LLM_TOOLS_CACHE))
