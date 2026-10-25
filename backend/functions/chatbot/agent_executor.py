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
    1. Nhận mục tiêu từ người dùng.
    2. Gửi ngữ cảnh đến LLM để suy luận (Thought) và chọn công cụ (Action).
    3. Tự động thực thi các công cụ đọc dữ liệu trên AWS.
    4. Nạp dữ liệu thực tế (Observation) ngược lại cho LLM để phân tích tiếp.
    5. Khi cần sửa đổi tài nguyên, dừng lại và xin phê duyệt từ người dùng.
    """

    def __init__(
        self,
        call_llm_fn: Callable[..., Dict[str, Any]],
        dispatch_fn: Callable[..., Dict[str, Any]],
        tools: List[Dict[str, Any]],
        raw_schemas: List[Dict[str, Any]],
        max_iterations: int = 4,
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
        """Thực thi chu trình suy luận tự trị đa bước."""
        context = context or {}
        region = context.get("region", "us-east-1")
        credentials = context.get("credentials")

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for h in (history or []):
            if isinstance(h, dict) and "role" in h and "content" in h:
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_message})

        steps_log: List[Dict[str, Any]] = []

        for iteration in range(self.max_iterations):
            llm_resp = self.call_llm_fn(messages=messages, tools=self.tools if self.tools else None)

            content = llm_resp.get("content")
            tool_calls = llm_resp.get("tool_calls")
            raw_msg = llm_resp.get("raw_message")

            # Nếu Agent không gọi thêm công cụ nào nữa -> Đã hoàn thành câu trả lời
            if not tool_calls:
                return {
                    "status": "SUCCESS",
                    "type": "agent_reply",
                    "message": content or "Đã hoàn thành phân tích yêu cầu của bạn.",
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
                reasoning_note = content or f"Agent đề xuất thực thi '{tool_name}' dựa trên kết quả phân tích hệ thống."
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
                }

            # Công cụ đọc / chẩn đoán an toàn: Tự động thực thi ngay trên AWS
            exec_res = self.dispatch_fn(
                service="lambda",
                action=tool_name,
                parameters=parsed_params,
                confirmed=True,
                region=region,
                credentials=credentials,
            )

            obs_data = exec_res.get("data", exec_res)
            obs_summary = exec_res.get("message", f"Hoàn thành {tool_name}")

            steps_log.append({
                "step": iteration + 1,
                "thought": content or f"Tự động tra cứu và điều tra thông tin qua công cụ '{tool_name}'.",
                "action": tool_name,
                "parameters": parsed_params,
                "observation": obs_summary,
            })

            # Nạp lại kết quả quan sát (Observation) vào ngữ cảnh của Agent
            # Đảm bảo giữ nguyên thought_signature của LLM nếu có
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
            "message": "Đã hoàn thành các bước điều tra và chẩn đoán hạ tầng.",
            "steps": steps_log,
            "data": {},
        }

