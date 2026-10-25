"""
Author: Trịnh Thiên Quang
Bộ điều phối chu trình suy luận tự trị (ReAct Agent Loop) cho trợ lý SatiBot.
"""

import ast
import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class AgentExecutor:
    """
    Quản lý vòng lặp ReAct (Reasoning + Acting) cho AI Agent:
    1. Nhận mục tiêu cấp cao từ người dùng.
    2. Gửi ngữ cảnh đến LLM để suy luận (Thought) và chọn công cụ (Action).
    3. Tự động thực thi các công cụ đọc / kiểm thử trên AWS.
    4. Nạp dữ liệu thực tế (Observation) ngược lại cho LLM để phân tích tiếp.
    5. Khi gặp thao tác thay đổi tài nguyên, dừng lại để người dùng phê duyệt, sau đó tiếp tục vòng lặp tự động.
    """

    def __init__(
        self,
        call_llm_fn: Callable[..., Dict[str, Any]],
        dispatch_fn: Callable[..., Dict[str, Any]],
        tools: List[Dict[str, Any]],
        raw_schemas: List[Dict[str, Any]],
        max_iterations: int = 6,
        max_observation_chars: int = 12000,
    ) -> None:
        self.call_llm_fn = call_llm_fn
        self.dispatch_fn = dispatch_fn
        self.tools = tools
        self.raw_schemas = raw_schemas
        self.max_iterations = max_iterations
        self.max_observation_chars = max(1000, max_observation_chars)
        self._schemas_by_name = {
            schema.get("name"): schema
            for schema in raw_schemas
            if isinstance(schema, dict) and schema.get("name")
        }

    def _is_tool_requiring_confirmation(self, tool_name: str) -> bool:
        """Kiểm tra công cụ có thuộc nhóm cần người dùng phê duyệt hay không."""
        for schema in self.raw_schemas:
            if schema.get("name") == tool_name:
                return schema.get("requires_confirmation", True)
        return True

    def _parse_tool_call(self, tool_call: Dict[str, Any]) -> tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
        """Parse and validate a model tool call before it can reach the dispatcher."""
        if not isinstance(tool_call, dict):
            return None, None, "Tool call không phải JSON object hợp lệ."
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return None, None, "Tool call thiếu trường function."
        tool_name = function.get("name")
        if not isinstance(tool_name, str) or tool_name not in self._schemas_by_name:
            return tool_name, None, f"Công cụ '{tool_name}' không nằm trong danh sách được phép."
        raw_args = function.get("arguments", "{}")
        try:
            parameters = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, TypeError):
            return tool_name, None, "Arguments của công cụ không phải JSON hợp lệ."
        if not isinstance(parameters, dict):
            return tool_name, None, "Arguments của công cụ phải là JSON object."

        schema = self._schemas_by_name[tool_name].get("parameters", {})
        properties = schema.get("properties", {})

        if tool_name == "create_lambda_function" and "role_arn" in properties and not parameters.get("role_arn"):
            import os
            parameters["role_arn"] = os.environ.get(
                "DEFAULT_LAMBDA_ROLE_ARN",
                "arn:aws:iam::640737808081:role/service-role/test-lambda-role-my87f8eu",
            )

        if tool_name == "create_public_function_url":
            origins = parameters.get("cors_allow_origins")
            if not isinstance(origins, list) or not origins or any(not isinstance(o, str) or o == "*" or not o.startswith("http") for o in origins):
                parameters["cors_allow_origins"] = ["http://127.0.0.1:8080"]

        missing = [name for name in schema.get("required", []) if name not in parameters]
        unknown = sorted(set(parameters) - set(properties))
        # Loại bỏ các tham số phụ vô hại trước khi báo lỗi
        for harmless in ("handler", "tags", "environment", "env_vars", "role_arn"):
            if harmless in unknown:
                unknown.remove(harmless)
        if missing:
            return tool_name, parameters, f"Thiếu tham số bắt buộc: {', '.join(missing)}. Hãy tự sửa tool call."
        if unknown:
            return tool_name, parameters, f"Tham số không được hỗ trợ: {', '.join(unknown)}. Hãy tự sửa tool call."
        invalid_types = []
        for name, value in parameters.items():
            expected = properties.get(name, {}).get("type")
            valid = True
            if expected == "string":
                valid = isinstance(value, str) and bool(value.strip())
            elif expected == "integer":
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif expected == "boolean":
                valid = isinstance(value, bool)
            elif expected == "object":
                valid = isinstance(value, dict)
            elif expected == "array":
                valid = isinstance(value, list)
                if valid and properties.get(name, {}).get("items", {}).get("type") == "string":
                    valid = all(isinstance(item, str) and bool(item.strip()) for item in value)
            if not valid:
                invalid_types.append(name)
        if invalid_types:
            return tool_name, parameters, f"Sai kiểu dữ liệu cho: {', '.join(invalid_types)}. Hãy tự sửa tool call."
        return tool_name, parameters, None

    @staticmethod
    def _requires_real_action(user_message: str) -> bool:
        """Detect operational requests that must not be completed with prose alone."""
        normalized = re.sub(r"\s+", " ", (user_message or "").strip().lower())
        if not normalized:
            return False

        follow_ups = (
            "tiếp tục", "làm tiếp", "làm đi", "sao rồi", "link đâu", "tại sao", "sao thế",
            "tiếp đi", "chạy đi", "tạo đi", "deploy đi", "xong chưa", "nút đâu", "xác nhận",
            "đâu rồi", "thế nào rồi", "tiếp", "được chưa", "ở đâu", "bấm đâu", "bấm nút",
            "confirm đâu", "nút confirm", "sao không thấy", "chưa thấy", "đưa tôi code",
        )
        if any(f in normalized for f in follow_ups):
            return True

        targets = (
            "aws", "lambda", "function", "hàm", "tài khoản", "cloud", "webhook",
            "web", "trang web", "website", "shop", "cửa hàng", "bán hàng", "app",
            "ứng dụng", "api", "service", "dịch vụ", "giỏ hàng", "sản phẩm", "bot",
            "hệ thống", "server", "máy chủ", "code", "mã nguồn", "store", "link",
            "url", "endpoint", "ecommerce", "giao diện", "frontend", "backend",
        )
        actions = (
            "làm", "truy cập", "kiểm tra", "chẩn đoán", "liệt kê", "đọc", "chạy",
            "test", "thực thi", "tạo", "triển khai", "deploy", "sửa", "cập nhật",
            "đổi", "tăng", "giảm", "xóa", "khóa", "mở khóa", "invoke", "audit",
            "thiết kế", "viết", "xây dựng", "setup", "cài đặt", "build", "phát triển",
        )
        if any(target in normalized for target in targets) and any(action in normalized for action in actions):
            return True

        if any(action in normalized for action in ("tạo", "viết", "xây dựng", "deploy", "triển khai", "chạy", "test")):
            return True

        return False

    def _serialize_observation(self, data: Any) -> str:
        """Keep tool output bounded so a large AWS response cannot exhaust LLM context."""
        try:
            serialized = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = json.dumps({"unserializable_result": str(data)}, ensure_ascii=False)
        if len(serialized) <= self.max_observation_chars:
            return serialized
        omitted = len(serialized) - self.max_observation_chars
        return serialized[: self.max_observation_chars] + f"...[đã rút gọn {omitted} ký tự]"

    def _recover_create_call_from_text(
        self,
        content: Any,
        user_message: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Recover a real tool call when a model prints code/pseudo-call as plain text or JSON."""
        if not isinstance(content, str):
            return None

        # 1. Khôi phục từ JSON pseudo-tool-call: ```json {"tool": "...", "arguments": ...} ```
        json_match = re.search(r"```(?:json)?\s*\n?(?P<json_str>\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
        if json_match:
            try:
                parsed_json = json.loads(json_match.group("json_str"))
                candidate_name = parsed_json.get("tool") or parsed_json.get("name") or parsed_json.get("action")
                candidate_args = parsed_json.get("arguments") or parsed_json.get("parameters") or {}
                if candidate_name and candidate_name in self._schemas_by_name:
                    return {
                        "id": f"recovered_{uuid.uuid4().hex[:10]}",
                        "type": "function",
                        "function": {
                            "name": candidate_name,
                            "arguments": json.dumps(candidate_args, ensure_ascii=False) if isinstance(candidate_args, dict) else str(candidate_args),
                        },
                    }
            except Exception:
                pass

        # 2. Khôi phục từ Pseudo-call kiểu: [Tool Call: create_lambda_function(...)]
        pseudo_match = re.search(r"\[(?:Tool Call|Action):\s*([A-Za-z0-9-_]+)\((.*?)\)\]", content, re.DOTALL | re.IGNORECASE)
        if pseudo_match:
            candidate_name = pseudo_match.group(1)
            if candidate_name in self._schemas_by_name:
                name_match = re.search(r"function_name\s*=\s*['\"]([A-Za-z0-9-_]{1,64})['\"]", pseudo_match.group(2))
                func_name = name_match.group(1) if name_match else "satibot-generated-service"
                code_match = re.search(r"```(?:python)?\s*\n?(?P<code>.*?)(?:```|$)", content, re.DOTALL | re.IGNORECASE)
                extracted_code = code_match.group("code").strip() if code_match else "def lambda_handler(event, context):\n    return {'statusCode': 200}"
                return {
                    "id": f"recovered_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": candidate_name,
                        "arguments": json.dumps({"function_name": func_name, "code_content": extracted_code}, ensure_ascii=False),
                    },
                }

        # 3. Khôi phục từ khối code Python trực tiếp
        patterns = [
            r"```(?:python)?\s*\n?(?P<code>.*?)(?:```|$)",
            r"\x27\x27\x27(?:python)?\s*\n?(?P<code>.*?)(?:\x27\x27\x27|$)",
            r"\"\"\"(?:python)?\s*\n?(?P<code>.*?)(?:\"\"\"|$)",
        ]
        code = None
        for pat in patterns:
            m = re.search(pat, content, re.DOTALL | re.IGNORECASE)
            if m and len(m.group("code").strip()) > 20:
                candidate = m.group("code").strip()
                if "def " in candidate or "import " in candidate or "lambda_handler" in candidate:
                    code = candidate
                    break

        if not code:
            m = re.search(r"(?:^|\n)(?P<code>(?:import\s+|from\s+|def\s+).*)$", content, re.DOTALL)
            if m:
                code = m.group("code").strip()

        # Nếu tin nhắn hiện tại không có code nhưng có trong lịch sử hội thoại
        if not code and messages:
            for past_m in reversed(messages):
                past_content = past_m.get("content", "")
                if isinstance(past_content, str) and ("lambda_handler" in past_content or "def handler(" in past_content):
                    for pat in patterns:
                        m = re.search(pat, past_content, re.DOTALL | re.IGNORECASE)
                        if m and len(m.group("code").strip()) > 20:
                            code = m.group("code").strip()
                            break
                    if code:
                        break

        if not code:
            return None

        # Đảm bảo có lambda_handler
        if "def lambda_handler(" not in code and "def handler(" in code:
            code += "\n\nlambda_handler = handler\n"

        # Kiểm tra AST và cắt gọt phần bị cắt ngang nếu LLM chạm token limit
        try:
            ast.parse(code)
        except SyntaxError:
            lines = code.split("\n")
            parsed_ok = False
            for cut in range(len(lines) - 1, max(0, len(lines) - 30), -1):
                sub_code = "\n".join(lines[:cut]).strip()
                try:
                    ast.parse(sub_code)
                    code = sub_code
                    parsed_ok = True
                    break
                except SyntaxError:
                    continue
            if not parsed_ok:
                return None

        name_match = re.search(
            r"create_lambda_function\s*\(.*?function_name\s*=\s*['\"]([A-Za-z0-9-_]{1,64})['\"]",
            content,
            re.DOTALL,
        )
        if name_match:
            function_name = name_match.group(1)
        else:
            goal_match = re.search(r"(?:lambda|hàm)\s+(?:tên\s+)?([A-Za-z0-9-_]{3,64})", user_message, re.IGNORECASE)
            function_name = goal_match.group(1) if goal_match else "satibot-generated-service"

        parameters = {
            "function_name": function_name,
            "code_content": code,
            "description": "Lambda được SatiBot khôi phục từ model output và triển khai tự động",
            "runtime": "python3.12",
            "memory_mb": 256,
            "timeout_seconds": 30,
        }
        create_props = self._schemas_by_name.get("create_lambda_function", {}).get("parameters", {}).get("properties", {})
        if "role_arn" in create_props:
            import os
            parameters["role_arn"] = os.environ.get(
                "DEFAULT_LAMBDA_ROLE_ARN",
                "arn:aws:iam::640737808081:role/service-role/test-lambda-role-my87f8eu",
            )
        return {
            "id": f"recovered_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": "create_lambda_function",
                "arguments": json.dumps(parameters, ensure_ascii=False),
            },
        }

    @staticmethod
    def _assistant_message(raw_msg: Any, content: Any, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        if isinstance(raw_msg, dict):
            message = dict(raw_msg)
            message.setdefault("role", "assistant")
            message.setdefault("content", content or "")
            message["tool_calls"] = tool_calls
            return message
        return {"role": "assistant", "content": content or "", "tool_calls": tool_calls}

    def run(
        self,
        user_message: str,
        system_prompt: str,
        history: Optional[List[Dict[str, Any]]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Bắt đầu chu trình suy luận tự trị đa bước từ mục tiêu của người dùng."""
        context = context or {}
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for h in (history or []):
            if isinstance(h, dict) and "role" in h and "content" in h:
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_message})

        return self._execute_loop(
            messages=messages,
            steps_log=[],
            start_iteration=0,
            system_prompt=system_prompt,
            context=context,
            action_required=self._requires_real_action(user_message),
        )

    def resume(
        self,
        agent_state: Dict[str, Any],
        exec_res: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Tiếp tục vòng lặp ReAct sau khi thao tác phê duyệt đã được người dùng xác nhận."""
        context = context or {}
        messages = agent_state.get("messages", [])
        first_call = agent_state.get("pending_tool_call", {})
        remaining_calls = agent_state.get("remaining_tool_calls", [])
        assistant_message_added = bool(agent_state.get("assistant_message_added", False))
        raw_msg = agent_state.get("raw_message")
        steps_log = agent_state.get("steps_log", [])
        parameters = agent_state.get("parameters", {})
        tool_name = first_call.get("function", {}).get("name", "action")
        start_iteration = agent_state.get("iteration", 0) + 1
        system_prompt = agent_state.get("system_prompt", "")

        obs_data = exec_res.get("data", exec_res)
        obs_summary = exec_res.get("message", f"Hoàn thành {tool_name}")

        steps_log.append({
            "step": len(steps_log) + 1,
            "thought": f"Đã nhận phê duyệt từ người dùng và thực thi thành công '{tool_name}'.",
            "action": tool_name,
            "parameters": parameters,
            "observation": obs_summary,
            "status": exec_res.get("status", "UNKNOWN"),
        })

        # A verified public URL is the terminal condition for website delivery.
        if tool_name == "create_public_function_url" and exec_res.get("status") == "SUCCESS":
            function_url = obs_data.get("function_url") if isinstance(obs_data, dict) else None
            if function_url:
                return {
                    "status": "SUCCESS",
                    "type": "website_deployed",
                    "message": f"Website đã được triển khai và kiểm thử thành công: {function_url}",
                    "steps": steps_log,
                    "tool_name": tool_name,
                    "data": obs_data,
                }

        # Nạp trợ lý và kết quả quan sát tool vào messages
        assistant_entry = raw_msg if raw_msg else {
            "role": "assistant",
            "content": "",
            "tool_calls": [first_call],
        }
        if not assistant_message_added:
            messages.append(assistant_entry)
        messages.append({
            "role": "tool",
            "tool_call_id": first_call.get("id", "call_confirmed"),
            "name": tool_name,
            "content": self._serialize_observation(obs_data),
        })

        # Tool calls in the same assistant message must all receive a tool result.
        # Execute the remaining safe calls, or pause again at the next mutation.
        pending_result = self._process_tool_calls(
            tool_calls=remaining_calls,
            messages=messages,
            steps_log=steps_log,
            iteration=start_iteration - 1,
            system_prompt=system_prompt,
            context=context,
            content="",
            raw_msg=None,
            assistant_already_added=True,
        )
        if pending_result is not None:
            return pending_result

        # Tiếp tục vòng lặp ReAct tự trị
        return self._execute_loop(
            messages=messages,
            steps_log=steps_log,
            start_iteration=start_iteration,
            system_prompt=system_prompt,
            context=context,
            action_required=True,
        )

    def _process_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        steps_log: List[Dict[str, Any]],
        iteration: int,
        system_prompt: str,
        context: Dict[str, Any],
        content: Any,
        raw_msg: Any,
        assistant_already_added: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(tool_calls, list) or not tool_calls:
            return None
        if not assistant_already_added:
            messages.append(self._assistant_message(raw_msg, content, tool_calls))

        for index, tool_call in enumerate(tool_calls):
            tool_name, parsed_params, validation_error = self._parse_tool_call(tool_call)
            call_id = tool_call.get("id", f"call_{iteration}_{index}") if isinstance(tool_call, dict) else f"call_{iteration}_{index}"
            if validation_error:
                observation = {"status": "ERROR", "error_code": "InvalidToolCall", "message": validation_error}
                steps_log.append({
                    "step": len(steps_log) + 1,
                    "thought": content or "LLM đã tạo tool call không hợp lệ.",
                    "action": tool_name or "unknown",
                    "parameters": parsed_params or {},
                    "observation": validation_error,
                })
                messages.append({"role": "tool", "tool_call_id": call_id, "name": tool_name or "unknown", "content": self._serialize_observation(observation)})
                continue

            if self._is_tool_requiring_confirmation(tool_name):
                action_id = f"act_{uuid.uuid4().hex[:12]}"
                reasoning_note = content or f"Agent đề xuất thực thi '{tool_name}' để tiếp tục quy trình tự động hóa."
                return {
                    "status": "CONFIRMATION_REQUIRED",
                    "type": "action_confirmation",
                    "action_id": action_id,
                    "service": "lambda",
                    "tool_name": tool_name,
                    "parameters": parsed_params,
                    "thought": reasoning_note,
                    "message": reasoning_note,
                    "steps": steps_log,
                    "agent_state": {
                        "messages": messages[:-1] if not assistant_already_added and index == 0 else messages,
                        "pending_tool_call": tool_call,
                        "remaining_tool_calls": tool_calls[index + 1 :],
                        "raw_message": self._assistant_message(raw_msg, content, tool_calls) if not assistant_already_added and index == 0 else None,
                        "assistant_message_added": assistant_already_added or index > 0,
                        "steps_log": steps_log,
                        "parameters": parsed_params,
                        "iteration": iteration,
                        "system_prompt": system_prompt,
                    },
                }

            try:
                exec_res = self.dispatch_fn(
                    service="lambda", action=tool_name, parameters=parsed_params, confirmed=True,
                    region=context.get("region", "us-east-1"), credentials=context.get("credentials"),
                )
                if not isinstance(exec_res, dict):
                    exec_res = {"status": "ERROR", "message": "Dispatcher trả về dữ liệu không hợp lệ."}
            except Exception as exc:
                logger.exception("Ngoại lệ khi thực thi công cụ %s", tool_name)
                exec_res = {"status": "ERROR", "message": f"Lỗi ngoại lệ khi thực thi công cụ '{tool_name}': {exc}", "data": {"error": str(exc)}}

            steps_log.append({
                "step": len(steps_log) + 1,
                "thought": content or f"Tự động thực hiện công cụ '{tool_name}'.",
                "action": tool_name,
                "parameters": parsed_params,
                "observation": exec_res.get("message", f"Hoàn thành {tool_name}"),
                "status": exec_res.get("status", "UNKNOWN"),
            })
            messages.append({
                "role": "tool", "tool_call_id": call_id, "name": tool_name,
                "content": self._serialize_observation(exec_res.get("data", exec_res)),
            })
        return None

    def _execute_loop(
        self,
        messages: List[Dict[str, Any]],
        steps_log: List[Dict[str, Any]],
        start_iteration: int,
        system_prompt: str,
        context: Dict[str, Any],
        action_required: bool = False,
    ) -> Dict[str, Any]:
        """Vòng lặp cốt lõi: Gửi LLM -> Thực thi -> Đọc Observation -> Nghĩ tiếp."""
        original_goal = next(
            (item.get("content", "") for item in messages if item.get("role") == "user"),
            "",
        )
        for iteration in range(start_iteration, self.max_iterations):
            llm_resp = self.call_llm_fn(messages=messages, tools=self.tools if self.tools else None)

            content = llm_resp.get("content")
            tool_calls = llm_resp.get("tool_calls")
            raw_msg = llm_resp.get("raw_message")

            if llm_resp.get("error_code"):
                return {
                    "status": "ERROR",
                    "type": "llm_error",
                    "message": content or "Không thể kết nối mô hình AI.",
                    "steps": steps_log,
                    "data": {"error_code": llm_resp.get("error_code")},
                }

            # Operational runs may only finish after at least one real tool observation.
            if not tool_calls:
                hallucinated_confirm = isinstance(content, str) and any(
                    k in content.lower() for k in (
                        "bấm vào nút", "nút xác nhận", "hộp thoại yêu cầu gọi công cụ",
                        "bấm xác nhận", "bấm nút", "hộp thoại xác nhận", "nút confirm",
                        "nút approve", "bấm confirm", "bấm approve", "hộp thoại đang hiển thị",
                        "xác nhận để tiếp tục",
                    )
                )
                if (action_required or hallucinated_confirm) and not steps_log:
                    recovered_call = self._recover_create_call_from_text(content, original_goal, messages=messages)
                    if recovered_call:
                        result = self._process_tool_calls(
                            [recovered_call], messages, steps_log, iteration,
                            system_prompt, context,
                            "Đã khôi phục structured tool call từ mã nguồn do model thiết kế.",
                            None,
                        )
                        if result is not None:
                            result["recovered_tool_call"] = True
                            return result

                    if iteration == start_iteration:
                        if hallucinated_confirm:
                            retry_prompt = (
                                "LỖI: Bạn vừa phản hồi bảo người dùng 'bấm nút xác nhận' hoặc mô tả kết quả, "
                                "nhưng bạn CHƯA HỀ phát ra tool_call! Giao diện hệ thống CHỈ hiển thị nút Xác Nhận (Approve) "
                                "khi bạn thực sự phát lệnh gọi công cụ 'create_lambda_function'. Hiện tại trên màn hình người dùng KHÔNG CÓ NÚT NÀO ĐỂ BẤM! "
                                "Hãy phát lệnh gọi tool 'create_lambda_function' NGAY BÂY GIỜ kèm toàn bộ code Python hoàn chỉnh!"
                            )
                        else:
                            retry_prompt = (
                                "Tiếp tục run này bằng hành động thực tế. Bạn vừa mô tả hành động hoặc in mã nguồn ra chat nhưng chưa gọi công cụ. "
                                "Đây là một agent run, không phải chat tư vấn. "
                                "Hãy gọi ngay công cụ AWS phù hợp. Nếu mục tiêu chưa đủ cụ thể để chọn công cụ, chỉ hỏi đúng một câu "
                                "làm rõ ngắn gọn; tuyệt đối không nói đang làm, đã làm, sẽ hiện nút xác nhận hoặc đã hoàn tất."
                            )
                        messages.append({
                            "role": "user",
                            "content": retry_prompt,
                        })
                        continue

                    return {
                        "status": "NEEDS_INPUT",
                        "type": "agent_needs_input",
                        "message": content or "Mục tiêu chưa đủ cụ thể để chọn công cụ AWS. Hãy nêu tài nguyên hoặc hành động cần thực hiện.",
                        "steps": [],
                        "data": {"reason": "no_tool_selected"},
                    }

                return {
                    "status": "SUCCESS",
                    "type": "agent_reply",
                    "message": content or "Đã hoàn thành toàn bộ quy trình tự động hóa cho bạn.",
                    "steps": steps_log,
                    "data": {},
                }

            result = self._process_tool_calls(
                tool_calls, messages, steps_log, iteration, system_prompt, context, content, raw_msg
            )
            if result is not None:
                return result

        # Khi chạm giới hạn số vòng lặp tối đa
        return {
            "status": "INCOMPLETE",
            "type": "agent_limit_reached",
            "message": "Agent đã chạm giới hạn số vòng suy luận trước khi có thể xác nhận hoàn tất.",
            "steps": steps_log,
            "data": {"max_iterations": self.max_iterations},
        }
