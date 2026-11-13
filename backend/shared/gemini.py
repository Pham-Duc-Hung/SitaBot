"""
Author: Trịnh Thiên Quang
Đọc khóa từ Secrets Manager và gọi Gemini với thời gian chờ hữu hạn.
Kiểm tra phản hồi và thử lại các lỗi kết nối tạm thời.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config
from backend.shared.cloud_errors import BackendError
from backend.shared.runtime import cached_boto_client, is_access_denied, sanitize_model_data
from backend.shared.http_api import reject_constant

_SECRET_CACHE = {}
_SM_CONFIG = Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 2})
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
SUPPORTED_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash")
REQUEST_TIMEOUT_SECONDS = 8
_LOGGER = logging.getLogger(__name__)


def get_api_key():
    secret_id = os.environ.get("GEMINI_SECRET_ARN")
    if not secret_id:
        raise BackendError("GeminiNotConfigured", "Chưa cấu hình Gemini secret.", 503)
    cached = _SECRET_CACHE.get(secret_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        client = cached_boto_client(boto3.client, "secretsmanager", config=_SM_CONFIG, config_tag="gemini-sm")
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        key = (value.get("api_key") or value.get("GEMINI_API_KEY")) if isinstance(value, dict) else value
        if not isinstance(key, str) or not key.strip():
            raise ValueError("empty key")
    except Exception as exc:
        if is_access_denied(exc):
            raise BackendError("GeminiSecretAccessDenied", "Lambda không đủ quyền đọc khóa Gemini từ Secrets Manager.", 403) from None
        raise BackendError("GeminiSecretUnavailable", "Không đọc được khóa Gemini.", 503) from None
    _SECRET_CACHE[secret_id] = (time.monotonic() + 300, key)
    return key


def complete(messages, tools=None, model=None):
    key = get_api_key()
    selected_model = model or os.environ.get("GEMINI_MODEL", SUPPORTED_MODELS[0])
    if selected_model not in SUPPORTED_MODELS:
        raise BackendError("InvalidModel", "Model Gemini không được hỗ trợ.", 400)
    payload = {"model": selected_model,
               "messages": sanitize_model_data(messages),
               "max_tokens": 2048}
    if tools:
        payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
    for attempt in range(3):
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
                data = json.loads(reply.read(262145), parse_constant=reject_constant)
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or (message.get("content") is not None and not isinstance(message["content"], str)):
                raise ValueError("Invalid message")
            return message
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise BackendError("GeminiUnavailable", "Gemini tạm thời không khả dụng.", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Never log the request or exception text: either may contain the API key.
            _LOGGER.warning("Gemini connection attempt %s/3 failed (%s)", attempt + 1, type(exc).__name__)
            if attempt == 2:
                raise BackendError("GeminiUnavailable", "Kết nối Gemini bị gián đoạn.", 502) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackendError("InvalidGeminiResponse", "Gemini trả dữ liệu sai cấu trúc.", 502) from None
        time.sleep(0.25 * (2 ** attempt))
