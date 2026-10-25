"""
Author: Trịnh Thiên Quang
Bộ điều phối chu trình suy luận tự trị (ReAct Agent Loop) cho trợ lý SatiBot.
"""

import json
import logging
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
    ) -> None:
        self.call_llm_fn = call_llm_fn
        self.dispatch_fn = dispatch_fn
        self.tools = tools
        self.raw_schemas = raw_schemas
        self.max_iterations = max_iterations

    def _is_tool_requiring_confirmation(self, tool_name: str) -> bool:
        """Kiểm tra công cụ có thuộc nhóm cần người dùng phê duyệt hay không."""
        for schema in self.raw_schemas:
            if schema.get("name") == tool_name:
                return schema.get("requires_confirmation", True)
        return True

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
        })

        # Nạp trợ lý và kết quả quan sát tool vào messages
        assistant_entry = raw_msg if raw_msg else {
            "role": "assistant",
            "content": "",
            "tool_calls": [first_call],
        }
        messages.append(assistant_entry)
        messages.append({
            "role": "tool",
            "tool_call_id": first_call.get("id", "call_confirmed"),
            "name": tool_name,
            "content": json.dumps(obs_data, ensure_ascii=False),
        })

        # Tiếp tục vòng lặp ReAct tự trị
        return self._execute_loop(
            messages=messages,
            steps_log=steps_log,
            start_iteration=start_iteration,
            system_prompt=system_prompt,
            context=context,
        )

    def _execute_loop(
        self,
        messages: List[Dict[str, Any]],
        steps_log: List[Dict[str, Any]],
        start_iteration: int,
        system_prompt: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Vòng lặp cốt lõi: Gửi LLM -> Thực thi -> Đọc Observation -> Nghĩ tiếp."""
        region = context.get("region", "us-east-1")
        credentials = context.get("credentials")

        for iteration in range(start_iteration, self.max_iterations):
            llm_resp = self.call_llm_fn(messages=messages, tools=self.tools if self.tools else None)

            content = llm_resp.get("content")
            tool_calls = llm_resp.get("tool_calls")
            raw_msg = llm_resp.get("raw_message")

            # Nếu Agent không gọi thêm công cụ nào nữa -> Đã hoàn thành câu trả lời
            if not tool_calls:
                return {
                    "status": "SUCCESS",
                    "type": "agent_reply",
                    "message": content or "Đã hoàn thành toàn bộ quy trình tự động hóa cho bạn.",
                    "steps": steps_log,
                    "data": {},
                }

            first_call = tool_calls[0]
            func_info = first_call.get("function", {})
            tool_name = func_info.get("name")
            raw_args = func_info.get("arguments", "{}")

            try:
                parsed_params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                parsed_params = {}

            # Kiểm tra xem công cụ có làm thay đổi hạ tầng hay không
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
                        "messages": messages,
                        "pending_tool_call": first_call,
                        "raw_message": raw_msg,
                        "steps_log": steps_log,
                        "parameters": parsed_params,
                        "iteration": iteration,
                        "system_prompt": system_prompt,
                    },
                }

            # Công cụ đọc / chẩn đoán an toàn: Tự động thực thi ngay trên AWS
            try:
                exec_res = self.dispatch_fn(
                    service="lambda",
                    action=tool_name,
                    parameters=parsed_params,
                    confirmed=True,
                    region=region,
                    credentials=credentials,
                )
            except Exception as exc:
                logger.exception("Ngoại lệ khi thực thi công cụ %s", tool_name)
                exec_res = {
                    "status": "ERROR",
                    "message": f"Lỗi ngoại lệ khi thực thi công cụ '{tool_name}': {str(exc)}",
                    "data": {"error": str(exc)},
                }

            obs_data = exec_res.get("data", exec_res)
            obs_summary = exec_res.get("message", f"Hoàn thành {tool_name}")

            steps_log.append({
                "step": len(steps_log) + 1,
                "thought": content or f"Tự động thực hiện công cụ '{tool_name}'.",
                "action": tool_name,
                "parameters": parsed_params,
                "observation": obs_summary,
            })

            # Nạp lại kết quả quan sát (Observation) vào ngữ cảnh của Agent
            assistant_entry = raw_msg if raw_msg else {
                "role": "assistant",
                "content": content or "",
                "tool_calls": [first_call],
            }
            messages.append(assistant_entry)
            messages.append({
                "role": "tool",
                "tool_call_id": first_call.get("id", f"call_{iteration}"),
                "name": tool_name,
                "content": json.dumps(obs_data, ensure_ascii=False),
            })

        # Khi chạm giới hạn số vòng lặp tối đa
        return {
            "status": "SUCCESS",
            "type": "agent_reply",
            "message": "Đã hoàn thành toàn bộ các bước trong chu trình tự động hóa hạ tầng.",
            "steps": steps_log,
            "data": {},
        }
