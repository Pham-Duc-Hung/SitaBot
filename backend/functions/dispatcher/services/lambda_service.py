"""
Author: Trịnh Thiên Quang
Xử lý các thao tác quản trị và kiểm thử AWS Lambda bằng Boto3.
"""

import json
import time
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def _is_strict_int(value: Any) -> bool:
    """Kiểm tra chỉ chấp nhận số nguyên, loại trừ boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_function_name(value: Any) -> bool:
    """Kiểm tra tên hàm hoặc ARN là chuỗi hợp lệ."""
    return isinstance(value, str) and bool(value.strip())


class LambdaService:
    """
    Lớp dịch vụ quản lý AWS Lambda thông qua Boto3.
    Hỗ trợ khởi tạo từ phiên mặc định hoặc thông tin xác thực tạm thời từ AWS STS AssumeRole.
    """

    def __init__(
        self,
        region_name: str = "ap-southeast-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ) -> None:
        retry_config = Config(
            region_name=region_name,
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        )

        if boto3_session is not None:
            self.session = boto3_session
        elif aws_access_key_id and aws_secret_access_key:
            self.session = boto3.Session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token,
                region_name=region_name,
            )
        else:
            self.session = boto3.Session(region_name=region_name)

        self.region_name = region_name
        self.client = self.session.client("lambda", config=retry_config)

    @classmethod
    def from_assumed_role(
        cls,
        role_arn: str,
        external_id: str,
        region_name: str = "ap-southeast-1",
        session_name: str = "SatiBotLambdaSession",
    ) -> "LambdaService":
        """Khởi tạo service thông qua STS AssumeRole của tài khoản khách hàng."""
        sts_client = boto3.client("sts", region_name=region_name)
        assume_resp = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            ExternalId=external_id,
            DurationSeconds=3600,
        )
        creds = assume_resp["Credentials"]
        return cls(
            region_name=region_name,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    @staticmethod
    def _format_response(
        status: str,
        action: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chuẩn hóa dữ liệu trả về cho Backend / Dispatcher."""
        res: Dict[str, Any] = {
            "status": status,
            "service": "lambda",
            "action": action,
            "message": message,
            "data": data or {},
        }
        if error_code:
            res["error_code"] = error_code
        return res

    def _handle_boto_error(self, action: str, exc: Exception) -> Dict[str, Any]:
        """Bắt và phân loại lỗi AWS API thành thông điệp thân thiện."""
        if isinstance(exc, ClientError):
            err_info = exc.response.get("Error", {})
            code = err_info.get("Code", "UnknownClientError")
            raw_msg = err_info.get("Message", str(exc))

            error_mappings = {
                "ResourceNotFoundException": "Không tìm thấy hàm Lambda được chỉ định. Vui lòng kiểm tra lại tên hàm.",
                "AccessDeniedException": "Tài khoản không đủ quyền thực hiện thao tác này trên AWS Lambda.",
                "InvalidParameterValueException": f"Tham số đầu vào không hợp lệ: {raw_msg}",
                "TooManyRequestsException": "Hệ thống đang bị giới hạn tần suất gọi API (Rate limit). Vui lòng thử lại sau.",
                "ResourceConflictException": f"Xung đột tài nguyên: {raw_msg}",
                "ServiceException": "Lỗi nội bộ từ phía dịch vụ AWS Lambda. Vui lòng thử lại sau.",
            }
            msg = error_mappings.get(code, f"Lỗi AWS [{code}]: {raw_msg}")
            return self._format_response("ERROR", action, msg, error_code=code)

        if isinstance(exc, BotoCoreError):
            return self._format_response(
                "ERROR", action, f"Lỗi kết nối hạ tầng AWS: {str(exc)}", error_code="BotoCoreError"
            )

        return self._format_response(
            "ERROR", action, f"Lỗi không xác định: {str(exc)}", error_code="UnhandledException"
        )

    def list_lambda_functions(self, max_items: int = 50) -> Dict[str, Any]:
        """Kịch bản 1: Liệt kê danh sách các hàm Lambda trong tài khoản."""
        action = "list_lambda_functions"
        try:
            paginator = self.client.get_paginator("list_functions")
            functions: List[Dict[str, Any]] = []

            for page in paginator.paginate(PaginationConfig={"MaxItems": max_items}):
                for fn in page.get("Functions", []):
                    functions.append({
                        "function_name": fn.get("FunctionName"),
                        "function_arn": fn.get("FunctionArn"),
                        "runtime": fn.get("Runtime", "custom"),
                        "memory_mb": fn.get("MemorySize"),
                        "timeout_seconds": fn.get("Timeout"),
                        "handler": fn.get("Handler"),
                        "last_modified": fn.get("LastModified"),
                        "code_size_bytes": fn.get("CodeSize"),
                        "description": fn.get("Description", ""),
                    })

            return self._format_response(
                status="SUCCESS",
                action=action,
                message=f"Đã tìm thấy {len(functions)} hàm Lambda tại region {self.region_name}.",
                data={"region": self.region_name, "count": len(functions), "functions": functions},
            )
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def get_function_config_detail(self, function_name: str) -> Dict[str, Any]:
        """Kịch bản 1b: Tra cứu chi tiết cấu hình và trạng thái của một hàm."""
        action = "get_function_config_detail"
        if not _valid_function_name(function_name):
            return self._format_response(
                status="ERROR", action=action,
                message="function_name phải là chuỗi ký tự hợp lệ.",
                error_code="InvalidParameters",
            )
        try:
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            func_url = None
            try:
                url_cfg = self.client.get_function_url_config(FunctionName=function_name)
                func_url = url_cfg.get("FunctionUrl")
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    pass

            reserved_concurrency = None
            try:
                concur_resp = self.client.get_function_concurrency(FunctionName=function_name)
                reserved_concurrency = concur_resp.get("ReservedConcurrentExecutions")
            except ClientError:
                pass

            data = {
                "function_name": cfg.get("FunctionName"),
                "function_arn": cfg.get("FunctionArn"),
                "runtime": cfg.get("Runtime"),
                "role_arn": cfg.get("Role"),
                "handler": cfg.get("Handler"),
                "memory_mb": cfg.get("MemorySize"),
                "timeout_seconds": cfg.get("Timeout"),
                "ephemeral_storage_mb": cfg.get("EphemeralStorage", {}).get("Size", 512),
                "state": cfg.get("State", "Active"),
                "last_update_status": cfg.get("LastUpdateStatus"),
                "last_modified": cfg.get("LastModified"),
                "reserved_concurrency": reserved_concurrency,
                "is_paused_kill_switch": reserved_concurrency == 0,
                "function_url": func_url,
            }
            msg = (
                f"Hàm '{function_name}' đang chạy runtime {data['runtime']}, "
                f"RAM {data['memory_mb']} MB, Timeout {data['timeout_seconds']} giây."
            )
            return self._format_response("SUCCESS", action, msg, data=data)
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def invoke_lambda_function(
        self, function_name: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Kịch bản 2: Kích hoạt thử nghiệm hàm Lambda với dữ liệu test JSON."""
        action = "invoke_lambda_function"
        if not _valid_function_name(function_name):
            return self._format_response(
                status="ERROR", action=action,
                message="function_name phải là chuỗi không rỗng.",
                error_code="InvalidParameters",
            )
        try:
            payload_bytes = json.dumps(payload or {}).encode("utf-8")
        except (TypeError, ValueError) as json_err:
            return self._format_response(
                status="ERROR", action=action,
                message=f"Dữ liệu payload không đúng định dạng JSON: {str(json_err)}",
                error_code="InvalidPayloadFormat",
            )

        start_time = time.perf_counter()
        try:
            response = self.client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                LogType="Tail",
                Payload=payload_bytes,
            )
            execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
            status_code = response.get("StatusCode", 200)
            function_error = response.get("FunctionError")

            raw_response_body = response["Payload"].read().decode("utf-8")
            try:
                response_data = json.loads(raw_response_body)
            except json.JSONDecodeError:
                response_data = raw_response_body

            data = {
                "function_name": function_name,
                "http_status_code": status_code,
                "execution_time_ms": execution_time_ms,
                "function_error": function_error,
                "input_payload": payload or {},
                "response_payload": response_data,
                "executed_version": response.get("ExecutedVersion", "$LATEST"),
            }

            if function_error:
                msg = f"Hàm '{function_name}' thực thi sinh lỗi ({function_error}) sau {execution_time_ms} ms."
                return self._format_response("ERROR", action, msg, data=data, error_code="LambdaExecutionError")

            msg = f"Đã chạy thử hàm '{function_name}' trong {execution_time_ms} ms (HTTP Status: {status_code})."
            return self._format_response("SUCCESS", action, msg, data=data)
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def _validate_resource_update_inputs(
        self,
        function_name: str,
        memory_mb: Optional[int],
        timeout_seconds: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        action = "update_function_resources"
        if not _valid_function_name(function_name):
            return self._format_response(
                status="ERROR", action=action,
                message="function_name phải là chuỗi không rỗng.",
                error_code="InvalidParameters",
            )
        if memory_mb is None and timeout_seconds is None:
            return self._format_response(
                status="ERROR", action=action,
                message="Cần cung cấp ít nhất memory_mb hoặc timeout_seconds để cập nhật.",
                error_code="InvalidParameters",
            )
        if memory_mb is not None:
            if not _is_strict_int(memory_mb):
                return self._format_response(
                    status="ERROR", action=action,
                    message="memory_mb phải là số nguyên (MB).",
                    error_code="InvalidParameters",
                )
            if not (128 <= memory_mb <= 10240):
                return self._format_response(
                    status="ERROR", action=action,
                    message="Dung lượng RAM (memory_mb) phải nằm trong khoảng từ 128 MB đến 10240 MB (10 GB).",
                    error_code="InvalidParameters",
                )
        if timeout_seconds is not None:
            if not _is_strict_int(timeout_seconds):
                return self._format_response(
                    status="ERROR", action=action,
                    message="timeout_seconds phải là số nguyên (giây).",
                    error_code="InvalidParameters",
                )
            if not (1 <= timeout_seconds <= 900):
                return self._format_response(
                    status="ERROR", action=action,
                    message="Thời gian timeout phải nằm trong khoảng từ 1 giây đến 900 giây (15 phút).",
                    error_code="InvalidParameters",
                )
        return None

    def preview_config_update(
        self,
        function_name: str,
        new_memory_mb: Optional[int] = None,
        new_timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Kịch bản 3a: Hiển thị bảng đối soát cấu hình cũ và mới trước khi xác nhận."""
        action = "preview_config_update"
        val_err = self._validate_resource_update_inputs(function_name, new_memory_mb, new_timeout_seconds)
        if val_err:
            val_err["action"] = action
            return val_err

        try:
            cfg = self.client.get_function_configuration(FunctionName=function_name)
            current_mem = cfg.get("MemorySize")
            current_timeout = cfg.get("Timeout")
            target_mem = new_memory_mb if new_memory_mb is not None else current_mem
            target_timeout = new_timeout_seconds if new_timeout_seconds is not None else current_timeout

            data = {
                "function_name": function_name,
                "current_configuration": {"memory_mb": current_mem, "timeout_seconds": current_timeout},
                "proposed_configuration": {"memory_mb": target_mem, "timeout_seconds": target_timeout},
                "changes_summary": {
                    "memory_changed": (new_memory_mb is not None and new_memory_mb != current_mem),
                    "timeout_changed": (new_timeout_seconds is not None and new_timeout_seconds != current_timeout),
                },
            }
            msg = (
                f"Kế hoạch điều chỉnh cho hàm '{function_name}':\n"
                f" - RAM: {current_mem} MB -> {target_mem} MB\n"
                f" - Timeout: {current_timeout}s -> {target_timeout}s\n"
                f"Vui lòng xác nhận để áp dụng thay đổi."
            )
            return self._format_response("SUCCESS", action, msg, data=data)
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def update_function_resources(
        self,
        function_name: str,
        memory_mb: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Kịch bản 3b: Cập nhật cấu hình RAM/Timeout sau khi đã được xác nhận."""
        action = "update_function_resources"
        val_err = self._validate_resource_update_inputs(function_name, memory_mb, timeout_seconds)
        if val_err:
            return val_err

        try:
            update_kwargs: Dict[str, Any] = {"FunctionName": function_name}
            if memory_mb is not None:
                update_kwargs["MemorySize"] = memory_mb
            if timeout_seconds is not None:
                update_kwargs["Timeout"] = timeout_seconds

            response = self.client.update_function_configuration(**update_kwargs)
            data = {
                "function_name": response.get("FunctionName"),
                "memory_mb": response.get("MemorySize"),
                "timeout_seconds": response.get("Timeout"),
                "last_modified": response.get("LastModified"),
                "last_update_status": response.get("LastUpdateStatus"),
            }
            msg = (
                f"Đã cập nhật cấu hình hàm '{function_name}' thành công: "
                f"RAM = {data['memory_mb']} MB, Timeout = {data['timeout_seconds']}s."
            )
            return self._format_response("SUCCESS", action, msg, data=data)
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    @staticmethod
    def _is_safe_cors_origin(origin: Any) -> bool:
        if not isinstance(origin, str):
            return False
        clean = origin.strip()
        if clean in ("", "*") or clean.startswith("*."):
            return False
        parts = urlsplit(clean)
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    def create_public_function_url(
        self,
        function_name: str,
        cors_allow_origins: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Kịch bản 4: Cấp phát nhanh liên kết HTTP công khai (Function URL)."""
        action = "create_public_function_url"
        if not _valid_function_name(function_name):
            return self._format_response(
                status="ERROR", action=action,
                message="function_name phải là chuỗi không rỗng.",
                error_code="InvalidParameters",
            )
        if (
            not isinstance(cors_allow_origins, list)
            or not cors_allow_origins
            or any(not self._is_safe_cors_origin(origin) for origin in cors_allow_origins)
        ):
            return self._format_response(
                status="ERROR", action=action,
                message="Cần cung cấp cors_allow_origins là danh sách origin cụ thể, không chấp nhận wildcard '*'.",
                error_code="InvalidParameters",
            )
        cors_config = {
            "AllowOrigins": cors_allow_origins,
            "AllowMethods": ["GET", "POST"],
            "AllowHeaders": ["content-type", "authorization"],
        }

        try:
            is_new = False
            try:
                existing = self.client.get_function_url_config(FunctionName=function_name)
                func_url = existing.get("FunctionUrl")
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                    url_resp = self.client.create_function_url_config(
                        FunctionName=function_name,
                        AuthType="NONE",
                        Cors=cors_config,
                    )
                    func_url = url_resp.get("FunctionUrl")
                    is_new = True
                else:
                    raise

            if is_new:
                statement_id = f"SatiBotPublicAccess-{int(time.time())}"
                try:
                    self.client.add_permission(
                        FunctionName=function_name,
                        StatementId=statement_id,
                        Action="lambda:InvokeFunctionUrl",
                        Principal="*",
                        FunctionUrlAuthType="NONE",
                    )
                except ClientError as perm_err:
                    if perm_err.response.get("Error", {}).get("Code") != "ResourceConflictException":
                        raise

            msg = (
                f"Đã cấp phát Function URL công khai cho hàm '{function_name}': {func_url}"
                if is_new else
                f"Hàm '{function_name}' đã có sẵn Function URL: {func_url}"
            )
            return self._format_response(
                status="SUCCESS", action=action, message=msg,
                data={"function_name": function_name, "function_url": func_url, "auth_type": "NONE", "is_newly_created": is_new},
            )
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def set_emergency_kill_switch(
        self,
        function_name: str,
        enable_kill_switch: bool = True,
        restore_concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Kịch bản 5: Tạm dừng hoạt động hàm khẩn cấp (Emergency Kill-Switch)."""
        action = "set_emergency_kill_switch"
        if not _valid_function_name(function_name):
            return self._format_response(
                status="ERROR", action=action,
                message="function_name phải là chuỗi không rỗng.",
                error_code="InvalidParameters",
            )
        if not isinstance(enable_kill_switch, bool):
            return self._format_response(
                status="ERROR", action=action,
                message="enable_kill_switch phải là True hoặc False.",
                error_code="InvalidParameters",
            )
        if (
            not enable_kill_switch
            and restore_concurrency is not None
            and (not _is_strict_int(restore_concurrency) or restore_concurrency <= 0)
        ):
            return self._format_response(
                status="ERROR", action=action,
                message="restore_concurrency phải là số nguyên dương (> 0).",
                error_code="InvalidParameters",
            )

        try:
            if enable_kill_switch:
                self.client.put_function_concurrency(
                    FunctionName=function_name,
                    ReservedConcurrentExecutions=0,
                )
                msg = (
                    f"ĐÃ KÍCH HOẠT KILL-SWITCH: Hàm '{function_name}' đã được đặt Reserved Concurrency = 0. "
                    "Mọi yêu cầu mới sẽ bị từ chối (HTTP 429 Too Many Requests)."
                )
                data = {"function_name": function_name, "is_paused": True, "reserved_concurrency": 0}
            else:
                if restore_concurrency and restore_concurrency > 0:
                    self.client.put_function_concurrency(
                        FunctionName=function_name,
                        ReservedConcurrentExecutions=restore_concurrency,
                    )
                    data = {"function_name": function_name, "is_paused": False, "reserved_concurrency": restore_concurrency}
                else:
                    self.client.delete_function_concurrency(FunctionName=function_name)
                    data = {"function_name": function_name, "is_paused": False, "reserved_concurrency": "unreserved"}

                msg = f"ĐÃ MỞ KHÓA HOẠT ĐỘNG: Hàm '{function_name}' đã được khôi phục trạng thái nhận request."

            return self._format_response("SUCCESS", action, msg, data=data)
        except Exception as exc:
            return self._handle_boto_error(action, exc)

    def execute_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        """Điều phối công cụ Lambda và chặn thao tác cần xác nhận nếu chưa confirmed."""
        if not isinstance(confirmed, bool):
            return self._format_response(
                status="ERROR", action=tool_name,
                message="confirmed phải là boolean do Backend kiểm soát.",
                error_code="InvalidConfirmation",
            )
        if not isinstance(parameters, dict):
            return self._format_response(
                status="ERROR", action=tool_name,
                message="Tham số công cụ phải là một đối tượng JSON.",
                error_code="InvalidParameters",
            )

        schema = next((item for item in LAMBDA_TOOL_SCHEMAS if item["name"] == tool_name), None)
        if schema is None:
            return self._format_response(
                status="ERROR", action=tool_name,
                message=f"Không hỗ trợ công cụ Lambda '{tool_name}'.",
                error_code="UnsupportedTool",
            )

        parameter_schema = schema.get("parameters", {})
        properties = parameter_schema.get("properties", {})
        required = parameter_schema.get("required", [])
        unknown_keys = set(parameters) - set(properties)
        missing_keys = [key for key in required if key not in parameters]
        if unknown_keys or missing_keys:
            details = []
            if unknown_keys:
                details.append(f"Tham số không hỗ trợ: {', '.join(sorted(unknown_keys))}")
            if missing_keys:
                details.append(f"Thiếu tham số bắt buộc: {', '.join(missing_keys)}")
            return self._format_response(
                status="ERROR", action=tool_name,
                message=". ".join(details) + ".",
                error_code="InvalidParameters",
            )

        type_errors = []
        for key, value in parameters.items():
            expected_type = properties.get(key, {}).get("type")
            valid = True
            if expected_type == "string":
                valid = isinstance(value, str) and bool(value.strip())
            elif expected_type == "integer":
                valid = _is_strict_int(value)
            elif expected_type == "boolean":
                valid = isinstance(value, bool)
            elif expected_type == "array":
                valid = isinstance(value, list)
                item_type = properties.get(key, {}).get("items", {}).get("type")
                if valid and item_type == "string":
                    valid = all(isinstance(item, str) and bool(item.strip()) for item in value)
            elif expected_type == "object":
                valid = isinstance(value, dict)
                if valid:
                    try:
                        json.dumps(value, allow_nan=False)
                    except (TypeError, ValueError):
                        valid = False
            if not valid:
                type_errors.append(key)
        if type_errors:
            return self._format_response(
                status="ERROR", action=tool_name,
                message=f"Kiểu dữ liệu không hợp lệ cho tham số: {', '.join(type_errors)}.",
                error_code="InvalidParameters",
            )

        if schema.get("requires_confirmation", False) and not confirmed:
            return self._format_response(
                status="CONFIRMATION_REQUIRED",
                action=tool_name,
                message="Thao tác này cần được người dùng xác nhận trước khi thực thi.",
                data={"tool_name": tool_name, "parameters": parameters, "requires_confirmation": True},
                error_code="ConfirmationRequired",
            )

        dispatch_map = {
            "list_lambda_functions": lambda p: self.list_lambda_functions(max_items=p.get("max_items", 50)),
            "get_function_config_detail": lambda p: self.get_function_config_detail(function_name=p["function_name"]),
            "invoke_lambda_function": lambda p: self.invoke_lambda_function(
                function_name=p["function_name"], payload=p.get("payload")
            ),
            "update_function_resources": lambda p: self.update_function_resources(
                function_name=p["function_name"],
                memory_mb=p.get("memory_mb"),
                timeout_seconds=p.get("timeout_seconds"),
            ),
            "create_public_function_url": lambda p: self.create_public_function_url(
                function_name=p["function_name"],
                cors_allow_origins=p.get("cors_allow_origins"),
            ),
            "set_emergency_kill_switch": lambda p: self.set_emergency_kill_switch(
                function_name=p["function_name"],
                enable_kill_switch=p.get("enable_kill_switch", True),
                restore_concurrency=p.get("restore_concurrency"),
            ),
        }
        return dispatch_map[tool_name](parameters)


LAMBDA_TOOL_SCHEMAS = [
    {
        "name": "list_lambda_functions",
        "description": "Liệt kê danh sách các hàm AWS Lambda hiện có trong tài khoản cùng cấu hình cơ bản.",
        "requires_confirmation": False,
        "parameters": {
            "type": "object",
            "properties": {
                "max_items": {"type": "integer", "description": "Số lượng hàm tối đa cần liệt kê (mặc định 50)."}
            },
        },
    },
    {
        "name": "get_function_config_detail",
        "description": "Tra cứu chi tiết cấu hình và trạng thái của một hàm AWS Lambda cụ thể.",
        "requires_confirmation": False,
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Tên hoặc ARN của hàm AWS Lambda."}
            },
            "required": ["function_name"],
        },
    },
    {
        "name": "invoke_lambda_function",
        "description": "Chạy thử nghiệm trực tiếp một hàm AWS Lambda với dữ liệu JSON.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Tên của hàm AWS Lambda cần chạy thử."},
                "payload": {"type": "object", "description": "Đối tượng JSON chứa dữ liệu đầu vào."},
            },
            "required": ["function_name"],
        },
    },
    {
        "name": "update_function_resources",
        "description": "Điều chỉnh cấu hình dung lượng bộ nhớ RAM (MB) hoặc Timeout (giây) của hàm AWS Lambda.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Tên của hàm AWS Lambda cần điều chỉnh."},
                "memory_mb": {"type": "integer", "description": "Dung lượng RAM mới (128-10240 MB)."},
                "timeout_seconds": {"type": "integer", "description": "Thời gian chờ mới (1-900 giây)."},
            },
            "required": ["function_name"],
        },
    },
    {
        "name": "create_public_function_url",
        "description": "Cấp phát một đường dẫn Endpoint HTTPS công khai (Function URL) cho hàm AWS Lambda.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Tên của hàm AWS Lambda cần tạo Function URL."},
                "cors_allow_origins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Danh sách origin cho phép CORS (ví dụ: ['https://app.example.com']).",
                },
            },
            "required": ["function_name", "cors_allow_origins"],
        },
    },
    {
        "name": "set_emergency_kill_switch",
        "description": "Tạm dừng khẩn cấp hàm AWS Lambda (đặt Concurrency = 0) hoặc mở khóa khôi phục.",
        "requires_confirmation": True,
        "parameters": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Tên của hàm AWS Lambda cần khóa/mở khóa."},
                "enable_kill_switch": {"type": "boolean", "description": "True để khóa khẩn cấp, False để mở khóa."},
                "restore_concurrency": {"type": "integer", "description": "Mức concurrency muốn khôi phục khi mở khóa."},
            },
            "required": ["function_name", "enable_kill_switch"],
        },
    },
]


# =============================================================================
# HÀM ENTRYPOINT CỦA DISPATCHER DÀNH CHO LAMBDA_SERVICE
# =============================================================================
def execute(credentials: Optional[Dict[str, Any]], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Hàm thực thi chuẩn hóa được Dispatcher Lambda gọi đến:
    - credentials: Dictionary chứa thông tin xác thực tạm thời từ STS AssumeRole (hoặc None nếu dùng default role).
    - params: Dictionary chứa:
        + 'action' hoặc 'tool_name': Tên công cụ cần gọi.
        + 'parameters': Các tham số truyền vào công cụ.
        + 'confirmed': Boolean (True nếu đã được người dùng xác nhận ở bước trước).
    """
    region = (credentials or {}).get("region_name") or params.get("region") or "us-east-1"
    
    # Khởi tạo service với credentials truyền vào
    if credentials and credentials.get("AccessKeyId"):
        service = LambdaService(
            region_name=region,
            aws_access_key_id=credentials.get("AccessKeyId"),
            aws_secret_access_key=credentials.get("SecretAccessKey"),
            aws_session_token=credentials.get("SessionToken"),
        )
    elif credentials and credentials.get("RoleArn"):
        service = LambdaService.from_assumed_role(
            role_arn=credentials["RoleArn"],
            external_id=credentials.get("ExternalId", ""),
            region_name=region,
        )
    else:
        # Dùng session môi trường hiện tại (Local PoC hoặc Lambda Execution Role)
        service = LambdaService(region_name=region)

    action = params.get("action") or params.get("tool_name")
    action_params = params.get("parameters", {})
    confirmed = params.get("confirmed", False)

    return service.execute_tool(
        tool_name=action,
        parameters=action_params,
        confirmed=confirmed,
    )


# =============================================================================
# KỊCH BẢN KIỂM THỬ ĐỘC LẬP QUA CLI (Dành cho PoC và Kiểm thử nhanh)
# =============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Công cụ kiểm thử độc lập module lambda_service (SatiBot)"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS Region cần kết nối (mặc định: us-east-1)",
    )
    parser.add_argument(
        "--func",
        default=None,
        help="Tên hàm Lambda mẫu dùng để kiểm thử các kịch bản 2, 3, 4, 5",
    )
    parser.add_argument(
        "--action",
        choices=["list", "detail", "invoke", "preview-config", "update-config", "url", "kill", "unblock"],
        default="list",
        help="Kịch bản muốn chạy thử",
    )
    parser.add_argument("--memory", type=int, default=None, help="Mức RAM (MB) mới khi test update-config")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout (s) mới khi test update-config")
    parser.add_argument(
        "--payload",
        type=str,
        default='{"price": 100, "tax": 0.1}',
        help="Chuỗi JSON payload khi test invoke",
    )

    args = parser.parse_args()
    service = LambdaService(region_name=args.region)

    print(f"=== KIỂM THỬ MODULE LAMBDA_SERVICE (Region: {args.region}) ===")
    if args.action == "list":
        res = service.list_lambda_functions()
    elif not args.func:
        print("Vui lòng truyền thêm tham số --func <tên_hàm_lambda> cho hành động này!")
        raise SystemExit(1)
    elif args.action == "detail":
        res = service.get_function_config_detail(args.func)
    elif args.action == "invoke":
        res = service.invoke_lambda_function(args.func, json.loads(args.payload))
    elif args.action == "preview-config":
        res = service.preview_config_update(args.func, args.memory, args.timeout)
    elif args.action == "update-config":
        res = service.update_function_resources(args.func, args.memory, args.timeout)
    elif args.action == "url":
        res = service.create_public_function_url(args.func, cors_allow_origins=["https://satibot.com"])
    elif args.action == "kill":
        res = service.set_emergency_kill_switch(args.func, enable_kill_switch=True)
    elif args.action == "unblock":
        res = service.set_emergency_kill_switch(args.func, enable_kill_switch=False)

    print(json.dumps(res, indent=2, ensure_ascii=False))
