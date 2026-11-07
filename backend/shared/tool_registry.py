"""
Author: Trịnh Thiên Quang
Tổng hợp schema công cụ của các dịch vụ AWS.
Kiểm tra tham số tool call trước khi điều phối thực thi.
"""

import importlib
from copy import deepcopy

from jsonschema import Draft7Validator
from backend.shared.cloud_errors import BackendError

SOURCES = (
    ("lambda", "backend.functions.dispatcher.services.lambda_service", "LAMBDA_TOOL_SCHEMAS"),
    ("ec2", "backend.functions.dispatcher.services.ec2_service", "EC2_TOOL_SCHEMAS"),
    ("s3", "backend.functions.chatbot.tools.s3_tools", "S3_TOOL_SCHEMAS"),
    ("iam", "backend.functions.dispatcher.services.iam_service", "IAM_TOOL_SCHEMAS"),
)


def registry():
    result = {}
    for service, module, constant in SOURCES:
        for raw in getattr(importlib.import_module(module), constant, ()):
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
    return result


def validate_call(name, parameters):
    tool = registry().get(name) if isinstance(name, str) else None
    if not tool:
        raise BackendError("UnsupportedTool", "Công cụ chưa có schema được đăng ký.")
    if not isinstance(parameters, dict) or not Draft7Validator(tool["parameters"]).is_valid(parameters):
        raise BackendError("InvalidParameters", "Tham số công cụ không hợp lệ; hãy bổ sung hoặc sửa yêu cầu.")
    return tool


def llm_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]}} for t in registry().values()]
