"""
Author: Trịnh Thiên Quang
Đọc khóa từ Secrets Manager và gọi Gemini với thời gian chờ hữu hạn.
Kiểm tra phản hồi và thử lại các lỗi kết nối tạm thời.
"""

import json
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.config import Config
from backend.shared.cloud_errors import BackendError
from backend.shared.runtime import redact_sensitive
from backend.shared.http_api import reject_constant

_SECRET_CACHE = {}
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def get_api_key():
    secret_id = os.environ.get("GEMINI_SECRET_ARN")
    if not secret_id:
        raise BackendError("GeminiNotConfigured", "Chưa cấu hình Gemini secret.", 503)
    cached = _SECRET_CACHE.get(secret_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        raw = boto3.client("secretsmanager", config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 2})).get_secret_value(SecretId=secret_id)["SecretString"]
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        key = (value.get("api_key") or value.get("GEMINI_API_KEY")) if isinstance(value, dict) else value
        if not isinstance(key, str) or not key.strip():
            raise ValueError("empty key")
    except Exception:
        raise BackendError("GeminiSecretUnavailable", "Không đọc được khóa Gemini.", 503) from None
    _SECRET_CACHE[secret_id] = (time.monotonic() + 300, key)
    return key


def complete(messages, tools=None):
    key = get_api_key()
    payload = {"model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
               "messages": redact_sensitive(messages), "temperature": 0.1,
               "max_tokens": 2048}
    if tools:
        payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
    for attempt in range(3):
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=6) as reply:
                data = json.loads(reply.read(262145), parse_constant=reject_constant)
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or (message.get("content") is not None and not isinstance(message["content"], str)):
                raise ValueError("Invalid message")
            return message
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise BackendError("GeminiUnavailable", "Gemini tạm thời không khả dụng.", 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise BackendError("GeminiUnavailable", "Kết nối Gemini bị gián đoạn.", 502) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackendError("InvalidGeminiResponse", "Gemini trả dữ liệu sai cấu trúc.", 502) from None
        time.sleep(0.25 * (2 ** attempt))
