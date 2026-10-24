"""
Author: Trịnh Thiên Quang
Tiếp nhận câu lệnh chat, gọi LLM trích xuất Function Call và xử lý luồng xác nhận.
"""

import json
import logging
import os
import time
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# System prompt định hướng hành vi cho SatiBot
SYSTEM_PROMPT = """Bạn là SatiBot - Trợ lý ChatOps thông minh hỗ trợ quản trị và giám sát hạ tầng AWS bằng tiếng Việt.
Nhiệm vụ của bạn:
1. Lắng nghe yêu cầu bằng ngôn ngữ tự nhiên tiếng Việt từ người dùng.
2. Xác định chính xác ý định và lựa chọn công cụ (Function Calling) phù hợp nhất từ danh sách công cụ được cung cấp.
3. Trích xuất đầy đủ và chính xác các tham số kỹ thuật cần thiết cho công cụ.
4. Đối với các câu hỏi chào hỏi, tư vấn hoặc hướng dẫn chung không cần thao tác tài nguyên, hãy phản hồi bằng tiếng Việt lịch sự, ngắn gọn và hữu ích.
5. Luôn tôn trọng quy tắc an toàn: Không tự ý bịa đặt tham số hay công cụ không tồn tại."""


def _get_lambda_tool_schemas() -> List[Dict[str, Any]]:
    """Tải schemas công cụ của Lambda Service."""
    try:
        from services.lambda_service import LAMBDA_TOOL_SCHEMAS
        return LAMBDA_TOOL_SCHEMAS
    except ImportError:
        try:
            from backend.functions.dispatcher.services.lambda_service import LAMBDA_TOOL_SCHEMAS
            return LAMBDA_TOOL_SCHEMAS
        except ImportError:
            return []


def format_tools_for_llm(raw_schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chuyển đổi tool schemas nội bộ sang định dạng chuẩn Function Calling của LLM."""
    formatted_tools = []
    for schema in raw_schemas:
        formatted_tools.append({
            "type": "function",
            "function": {
                "name": schema.get("name"),
                "description": schema.get("description"),
                "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
            }
        })
    return formatted_tools


def _is_tool_requiring_confirmation(tool_name: str, raw_schemas: List[Dict[str, Any]]) -> bool:
    """Kiểm tra công cụ có thuộc nhóm tác vụ thay đổi tài nguyên cần xác nhận hay không."""
    for schema in raw_schemas:
        if schema.get("name") == tool_name:
            return schema.get("requires_confirmation", False)
    return False


def call_llm_api(
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Gọi API mô hình ngôn ngữ lớn (OpenAI-compatible / Gemini / REST endpoint).
    Nếu không có API Key trong môi trường, chuyển sang chế độ giả lập (Mock/Heuristic)
    để hỗ trợ kiểm thử cục bộ và chạy unit test.
    """
    api_key = api_key or os.environ.get("LLM_API_KEY")
    api_url = api_url or os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
    model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")

    # Nếu không có API Key cấu hình, dùng bộ phân tích mẫu cho môi trường test
    if not api_key:
        return _mock_llm_fallback(messages, tools)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]["message"]
            return {
                "content": choice.get("content"),
                "tool_calls": choice.get("tool_calls"),
            }
    except Exception as exc:
        logger.error("Lỗi khi gọi API LLM: %s", str(exc))
        return {
            "content": f"Đã xảy ra lỗi khi kết nối với mô hình AI: {str(exc)}",
            "tool_calls": None,
        }


def _mock_llm_fallback(
    messages: List[Dict[str, str]], tools: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Bộ phân tích dự phòng khi chạy kiểm thử hoặc chưa gắn API Key."""
    user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_msg = m.get("content", "").lower()
            break

    # 1. Ý định liệt kê hàm lambda
    if any(k in user_msg for k in ["liệt kê", "danh sách", "list"]) and "lambda" in user_msg:
        return {
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "list_lambda_functions",
                    "arguments": json.dumps({"max_items": 50}),
                }
            }]
        }

    # 2. Ý định chạy thử nghiệm hàm (invoke)
    if any(k in user_msg for k in ["chạy thử", "invoke", "test", "thực thi"]) and "hàm" in user_msg:
        # Trích xuất tên hàm
        words = user_msg.split()
        func_name = "test-lambda"
        for i, w in enumerate(words):
            if w in ["hàm", "function"] and i + 1 < len(words):
                func_name = words[i + 1].strip(",. ")
                break

        return {
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "invoke_lambda_function",
                    "arguments": json.dumps({"function_name": func_name, "payload": {"price": 100, "tax": 0.1}}),
                }
            }]
        }

    # 3. Ý định đổi RAM / Timeout (update config)
    if any(k in user_msg for k in ["tăng", "giảm", "đổi", "nâng", "ram", "timeout"]) and "hàm" in user_msg:
        return {
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "update_function_resources",
                    "arguments": json.dumps({"function_name": "test-lambda", "memory_mb": 512, "timeout_seconds": 30}),
                }
            }]
        }

    # 4. Ý định khóa khẩn cấp (kill-switch)
    if any(k in user_msg for k in ["khóa", "tạm dừng", "kill", "ngắt", "dừng"]) and "hàm" in user_msg:
        return {
            "content": None,
            "tool_calls": [{
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "set_emergency_kill_switch",
                    "arguments": json.dumps({"function_name": "test-lambda", "enable_kill_switch": True}),
                }
            }]
        }

    # Trả về câu chào / hội thoại thông thường
    return {
        "content": "Xin chào! Tôi là SatiBot. Tôi có thể hỗ trợ bạn kiểm tra danh sách, chạy thử nghiệm, hoặc cấu hình các hàm AWS Lambda.",
        "tool_calls": None,
    }


def _build_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Tạo response chuẩn HTTP."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Điểm tiếp nhận chính của Chatbot Lambda:
    - Xử lý câu chat người dùng và trích xuất Function Calling.
    - Phân loại thao tác: nếu an toàn -> gửi thực thi, nếu rủi ro -> yêu cầu xác nhận.
    """
    logger.info("Chatbot received event: %s", json.dumps(event, default=str))

    # 1. Trích xuất body
    payload = event
    is_api_gateway = False

    if isinstance(event, dict) and "body" in event:
        is_api_gateway = True
        raw_body = event.get("body")
        if isinstance(raw_body, str):
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                err_resp = {"status": "ERROR", "message": "Body không phải JSON hợp lệ."}
                return _build_response(400, err_resp) if is_api_gateway else err_resp
        elif isinstance(raw_body, dict):
            payload = raw_body

    if not isinstance(payload, dict):
        err_resp = {"status": "ERROR", "message": "Payload phải là JSON object."}
        return _build_response(400, err_resp) if is_api_gateway else err_resp

    user_message = payload.get("message", "").strip()
    history = payload.get("history", [])

    if not user_message:
        err_resp = {"status": "ERROR", "message": "Thiếu nội dung câu lệnh 'message'."}
        return _build_response(400, err_resp) if is_api_gateway else err_resp

    # 2. Chuẩn bị ngữ cảnh hội thoại & danh sách công cụ
    raw_schemas = _get_lambda_tool_schemas()
    llm_tools = format_tools_for_llm(raw_schemas)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        if isinstance(h, dict) and "role" in h and "content" in h:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    # 3. Gọi LLM trích xuất ý định (Function Calling)
    llm_resp = call_llm_api(messages=messages, tools=llm_tools if llm_tools else None)

    content = llm_resp.get("content")
    tool_calls = llm_resp.get("tool_calls")

    # Nếu LLM phản hồi hội thoại thông thường (không gọi tool)
    if not tool_calls:
        resp_data = {
            "status": "SUCCESS",
            "type": "chat_reply",
            "message": content or "Đã ghi nhận yêu cầu của bạn.",
            "data": {},
        }
        return _build_response(200, resp_data) if is_api_gateway else resp_data

    # 4. Xử lý phản hồi Function Calling từ LLM
    first_call = tool_calls[0]
    func_info = first_call.get("function", {})
    tool_name = func_info.get("name")
    raw_args = func_info.get("arguments", "{}")

    try:
        parsed_params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        parsed_params = {}

    needs_confirmation = _is_tool_requiring_confirmation(tool_name, raw_schemas)

    # 5. Phân luồng: Yêu cầu xác nhận hoặc Thực thi ngay
    if needs_confirmation:
        action_id = f"act_{uuid.uuid4().hex[:12]}"
        resp_data = {
            "status": "CONFIRMATION_REQUIRED",
            "type": "action_confirmation",
            "action_id": action_id,
            "service": "lambda",
            "tool_name": tool_name,
            "parameters": parsed_params,
            "message": (
                f"Thao tác '{tool_name}' làm thay đổi cấu hình hoặc kích hoạt tài nguyên. "
                "Bạn có chắc chắn muốn thực hiện không? Vui lòng gửi xác nhận để tiếp tục."
            ),
        }
    else:
        # Tác vụ chỉ đọc (an toàn): sẵn sàng chuyển sang Dispatcher
        resp_data = {
            "status": "SUCCESS",
            "type": "action_ready",
            "service": "lambda",
            "tool_name": tool_name,
            "parameters": parsed_params,
            "message": f"Đã nhận diện ý định gọi công cụ '{tool_name}'. Đang chuyển tiếp điều phối thực thi.",
        }

    return _build_response(200, resp_data) if is_api_gateway else resp_data
