"""
Author: Trịnh Thiên Quang
Tiếp nhận câu lệnh chat, gọi LLM trích xuất Function Call và xử lý luồng xác nhận.
"""

import json
import logging
import os
import sys
import time
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_env_file():
    """Tải cấu hình từ tệp .env nếu môi trường chưa có."""
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env_file()


def _dispatch_action(
    service: str,
    action: str,
    parameters: Dict[str, Any],
    confirmed: bool = False,
    region: str = "us-east-1",
    credentials: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Kết nối và chuyển tiếp yêu cầu đến Dispatcher Lambda."""
    # Đảm bảo đường dẫn dispatcher có trong sys.path
    dispatcher_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dispatcher"))
    if dispatcher_dir not in sys.path:
        sys.path.insert(0, dispatcher_dir)

    try:
        from backend.functions.dispatcher.index import lambda_handler as dispatcher_handler
    except ImportError:
        try:
            from dispatcher import lambda_handler as dispatcher_handler
        except ImportError:
            try:
                from index import lambda_handler as dispatcher_handler
            except ImportError:
                return {
                    "status": "ERROR",
                    "message": "Không tìm thấy module Dispatcher.",
                    "error_code": "DispatcherNotFound"
                }

    event = {
        "service": service,
        "action": action,
        "parameters": parameters,
        "confirmed": confirmed,
        "region": region,
        "credentials": credentials,
    }
    return dispatcher_handler(event, None)

# System prompt định hướng tư duy Agent (ReAct) cho SatiBot
SYSTEM_PROMPT = """Bạn là SatiBot - AI Agent Tự Trị Quản Trị & Phát Triển AWS Serverless (Autonomous AWS Serverless DevOps Agent).
Phương châm tối thượng: "THAY THẾ CON NGƯỜI TỰ ĐỘNG HÓA TỪ A ĐẾN Z - HÀNH ĐỘNG THAY VÌ HƯỚNG DẪN".
Người dùng không cần kiến thức AWS chuyên sâu và KHÔNG BAO GIỜ phải tự vào AWS Console bấm thủ công. Bạn có toàn quyền truy cập thực tế vào tài khoản AWS của người dùng qua Boto3 để tự lập kế hoạch, tự viết code, tự triển khai và kiểm thử.

=== NGUYÊN TẮC CỐT LÕI (BẮT BUỘC TUÂN THỦ 100%) ===
1. BẠN CÓ QUYỀN TRUY CẬP THỰC TẾ VÀO AWS:
   - Bạn ĐANG KẾT NỐI VÀ CÓ ĐẦY ĐỦ QUYỀN TRUY CẬP HỢP LỆ VÀO TÀI KHOẢN AWS CỦA NGƯỜI DÙNG thông qua AWS SDK (Boto3) và hệ thống công cụ (tools) có sẵn.
   - TUYỆT ĐỐI CẤM NÓI: "Tôi là AI không có quyền truy cập trực tiếp", "Tôi không có quyền IAM", "Mọi thứ chỉ là lý thuyết", "Tôi chỉ đang trò chuyện trong khung chat".
   - TUYỆT ĐỐI CẤM HƯỚNG DẪN NGƯỜI DÙNG: "Hãy vào AWS Management Console bấm theo các bước sau...", "Bước 1 mở console, bước 2 tạo hàm...". Mọi thao tác đều do Agent tự động thực hiện qua công cụ!
   - Khi người dùng phản hồi "sao tôi lên AWS không thấy gì?", hãy ngay lập tức kiểm tra danh sách bằng công cụ `list_lambda_functions` hoặc chủ động tạo ngay hàm trên AWS bằng `create_lambda_function`, tuyệt đối không phân trần hay xin lỗi suông.

2. PHẠM VI CHUYÊN SÂU - BẬC THẦY SERVERLESS (AWS LAMBDA EXPERT):
   - Bạn phụ trách toàn diện dịch vụ AWS Lambda: Tạo mới, cập nhật mã nguồn, kiểm tra mã nguồn, chạy thử nghiệm (invoke), điều chỉnh tài nguyên (RAM/timeout), quản lý Function URL, chẩn đoán sức khỏe, và kill-switch.
   - Khi người dùng yêu cầu bài toán hệ thống rộng như "thiết kế hệ thống chống chịu", "xây dựng hệ thống chịu tải", "viết API", "tạo webhook":
     * KHÔNG lan man lý thuyết về các dịch vụ bên ngoài bạn không có công cụ (như SQS, DynamoDB).
     * HÃY TẬP TRUNG TỰ ĐỘNG XÂY DỰNG MỘT DỊCH VỤ LAMBDA CHỐNG CHỊU SỰ CỐ (Resilient Serverless Service) bằng Python trên AWS.
     * Tích hợp trực tiếp các mẫu thiết kế chống chịu (Resilience Design Patterns) vào mã nguồn Python của hàm:
       + Exponential Backoff & Jitter (tự động thử lại khi API ngoài chập chờn).
       + Circuit Breaker (ngắt mạch khi dịch vụ phụ thuộc sập để chống cạn kiệt tài nguyên).
       + Timeout Guard & Graceful Fallback (trả về dữ liệu dự phòng khi gặp sự cố, không để hệ thống crash).
       + Input Schema Validation & Comprehensive Logging (bắt lỗi an toàn, trả về chuẩn HTTP 200/400/500).

3. QUY TRÌNH HÀNH ĐỘNG TRỌN GÓI (ACTION FIRST):
   - Khi người dùng giao mục tiêu (tạo hàm, viết API, hệ thống chống chịu, tiếp nhận webhook...):
     a) BẮT BUỘC PHẢI PHÁT RA LỆNH GỌI CÔNG CỤ (tool_call) 'create_lambda_function' NGAY LẬP TỨC.
        Toàn bộ mã nguồn Python do bạn thiết kế PHẢI NẰM TRỌN VẸN trong tham số 'code_content' của tool_call.
        TUYỆT ĐỐI KHÔNG xuất mã nguồn dưới dạng văn bản chat (không dùng ```python hay '''python).
        Sử dụng role_arn: 'arn:aws:iam::640737808081:role/service-role/test-lambda-role-my87f8eu'.
     b) ĐỪNG CHỈ HỎI BẰNG CHỮ "bạn có đồng ý không" mà không gọi tool! Hệ thống của chúng ta đã có sẵn cơ chế chặn an toàn (Confirmation Dialog) tự động hiển thị hộp thoại duyệt và nút bấm Xác Nhận cho người dùng mỗi khi bạn phát lệnh gọi 'create_lambda_function'.
     c) Sau khi người dùng bấm Duyệt (Confirmed): Hệ thống tự động tiếp tục vòng lặp (resume):
        - Với yêu cầu chống chịu sự cố / webhook, tự động gọi 'configure_resilient_service' để publish version bất biến, gắn alias ổn định, cấu hình retry/event age và reserved concurrency.
        - Tự động gọi 'invoke_lambda_function' với payload thử nghiệm (ví dụ: các sự kiện order.created, order.cancelled, payment.failed, event cũ, event trùng lặp) để kiểm chứng hàm chạy thành công thật.
        - Tự động gọi 'create_public_function_url' để cấp đường link HTTPS công khai.
          Khi chạy web demo cục bộ và người dùng không nêu origin, dùng cors_allow_origins=['http://127.0.0.1:8080']; tuyệt đối không bỏ thiếu trường này và không dùng wildcard '*'.
        - Bàn giao kết quả trọn gói kèm endpoint HTTPS và số liệu kiểm thử thực tế.

4. AN TOÀN & BẢO MẬT:
   - Luôn tôn trọng quy trình xác nhận khi thực hiện thao tác thay đổi tài nguyên (tạo hàm, sửa code, đổi cấu hình, kill-switch).
   - Trước khi đề xuất tạo hoặc cập nhật code, hãy giải thích ngắn gọn tư duy (Thought) và cơ chế kỹ thuật bạn đã áp dụng."""


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
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Gọi API mô hình ngôn ngữ lớn (OpenAI-compatible / Gemini / REST endpoint).
    Nếu không có API Key trong môi trường, chuyển sang chế độ giả lập (Mock/Heuristic)
    để hỗ trợ kiểm thử cục bộ và chạy unit test.
    """
    api_key = api_key or os.environ.get("LLM_API_KEY")
    api_url = api_url or os.environ.get("LLM_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
    model = model or os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
    if model in ("gemini-1.5-flash", "gemini-2.5-flash", "gemini-3.5-flash", "gpt-4o-mini"):
        model = "gemini-3.5-flash-lite"

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

    max_attempts = 3
    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
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
                    "raw_message": choice,
                    "llm_attempts": attempt + 1,
                }
        except urllib.error.HTTPError as http_err:
            last_error = http_err
            try:
                err_body = http_err.read().decode("utf-8")
            except Exception:
                err_body = str(http_err)
            logger.warning(
                "LLM HTTP attempt %s/%s failed: %s - %s",
                attempt + 1, max_attempts, http_err.code, err_body,
            )
            if http_err.code not in (429, 500, 502, 503, 504) or attempt == max_attempts - 1:
                friendly_msg = (
                    "Hệ thống AI tạm thời vượt hạn mức sau nhiều lần tự thử lại."
                    if http_err.code == 429 else
                    f"Đã xảy ra lỗi khi kết nối với mô hình AI ({http_err.code}): {err_body}"
                )
                return {
                    "content": friendly_msg, "tool_calls": None,
                    "error_code": f"LLM_HTTP_{http_err.code}", "llm_attempts": attempt + 1,
                }
        except Exception as exc:
            last_error = exc
            logger.warning("LLM connection attempt %s/%s failed: %s", attempt + 1, max_attempts, exc)
            if attempt == max_attempts - 1:
                return {
                    "content": f"Không thể kết nối mô hình AI sau {max_attempts} lần thử: {exc}",
                    "tool_calls": None, "error_code": "LLM_CONNECTION_ERROR",
                    "llm_attempts": max_attempts,
                }
        time.sleep(0.75 * (2 ** attempt))

    return {
        "content": f"Không thể kết nối mô hình AI: {last_error}",
        "tool_calls": None, "error_code": "LLM_CONNECTION_ERROR", "llm_attempts": max_attempts,
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

    # Nếu là request xác nhận hành động đã được duyệt
    if payload.get("confirmed") is True and payload.get("tool_name"):
        tool_params = payload.get("parameters", {})
        if payload.get("tool_name") == "create_lambda_function" and not tool_params.get("role_arn"):
            configured_role = os.environ.get("DEFAULT_LAMBDA_ROLE_ARN")
            if configured_role:
                tool_params["role_arn"] = configured_role
        exec_res = _dispatch_action(
            service=payload.get("service", "lambda"),
            action=payload.get("tool_name"),
            parameters=tool_params,
            confirmed=True,
            region=payload.get("region", "us-east-1"),
            credentials=payload.get("credentials"),
        )

        # Nếu có agent_state, tự động tiếp tục chu trình ReAct hoàn thiện toàn bộ mục tiêu
        try:
            from agent_executor import AgentExecutor
        except ImportError:
            try:
                from backend.functions.chatbot.agent_executor import AgentExecutor
            except ImportError:
                AgentExecutor = None

        api_key = os.environ.get("LLM_API_KEY")
        agent_state = payload.get("agent_state")
        raw_schemas = _get_lambda_tool_schemas()
        llm_tools = format_tools_for_llm(raw_schemas)

        if agent_state and AgentExecutor and api_key and exec_res.get("status") == "SUCCESS":
            executor = AgentExecutor(
                call_llm_fn=call_llm_api,
                dispatch_fn=_dispatch_action,
                tools=llm_tools,
                raw_schemas=raw_schemas,
                max_iterations=6,
            )
            resumed_data = executor.resume(
                agent_state=agent_state,
                exec_res=exec_res,
                context={
                    "region": payload.get("region", "us-east-1"),
                    "credentials": payload.get("credentials"),
                },
            )
            return _build_response(200, resumed_data) if is_api_gateway else resumed_data

        resp_data = {
            "status": exec_res.get("status", "SUCCESS"),
            "type": "action_executed",
            "service": payload.get("service", "lambda"),
            "tool_name": payload.get("tool_name"),
            "message": exec_res.get("message", "Thao tác đã được thực thi trên AWS."),
            "data": exec_res.get("data", {}),
        }
        return _build_response(200, resp_data) if is_api_gateway else resp_data

    user_message = payload.get("message", "").strip()
    history = payload.get("history", [])
    if isinstance(history, list) and len(history) > 10:
        history = history[-10:]

    if not user_message:
        err_resp = {"status": "ERROR", "message": "Thiếu nội dung câu lệnh 'message'."}
        return _build_response(400, err_resp) if is_api_gateway else err_resp

    # 2. Chuẩn bị ngữ cảnh hội thoại & danh sách công cụ
    raw_schemas = _get_lambda_tool_schemas()
    llm_tools = format_tools_for_llm(raw_schemas)

    # 3. Kích hoạt chu trình suy luận tự trị Agent (ReAct Loop) khi có LLM API Key
    try:
        from agent_executor import AgentExecutor
    except ImportError:
        try:
            from backend.functions.chatbot.agent_executor import AgentExecutor
        except ImportError:
            AgentExecutor = None

    api_key = os.environ.get("LLM_API_KEY")
    if api_key and AgentExecutor:
        executor = AgentExecutor(
            call_llm_fn=call_llm_api,
            dispatch_fn=_dispatch_action,
            tools=llm_tools,
            raw_schemas=raw_schemas,
            max_iterations=4,
        )
        resp_data = executor.run(
            user_message=user_message,
            system_prompt=SYSTEM_PROMPT,
            history=history,
            context={
                "region": payload.get("region", "us-east-1"),
                "credentials": payload.get("credentials"),
            },
        )
        return _build_response(200, resp_data) if is_api_gateway else resp_data

    # Chế độ Fallback / Mock khi chạy kiểm thử đơn vị hoặc thiếu API Key
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        if isinstance(h, dict) and "role" in h and "content" in h:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

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
        # Tác vụ chỉ đọc (an toàn): Tự động điều phối gọi Dispatcher lấy dữ liệu AWS thật
        exec_res = _dispatch_action(
            service="lambda",
            action=tool_name,
            parameters=parsed_params,
            confirmed=True,
            region=payload.get("region", "us-east-1"),
            credentials=payload.get("credentials"),
        )
        resp_data = {
            "status": exec_res.get("status", "SUCCESS"),
            "type": "action_executed",
            "service": "lambda",
            "tool_name": tool_name,
            "parameters": parsed_params,
            "message": exec_res.get("message", f"Đã thực thi công cụ '{tool_name}'."),
            "data": exec_res.get("data", {}),
        }

    return _build_response(200, resp_data) if is_api_gateway else resp_data
