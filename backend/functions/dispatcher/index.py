"""
SatiBot - Dispatcher Lambda Handler (index.py)
Phụ trách: Trịnh Thiên Quang (Nhóm 10 - INT3319 1)
Sprint 2: Tiếp nhận request từ API Gateway hoặc Step Functions State Machine,
phân loại dịch vụ (service), ủy quyền và điều phối đến các module dịch vụ tương ứng.
"""

import json
import logging
from typing import Any, Dict

# Import các service con (được phát triển bởi các thành viên trong nhóm)
from services import lambda_service
from services import ec2_service
from services import s3_service
from services import iam_service

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _build_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Tạo response chuẩn tương thích HTTP API Gateway."""
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


def _format_result(is_api_gateway: bool, http_status: int, result: Dict[str, Any]) -> Dict[str, Any]:
    """Trả về kết quả phù hợp cho API Gateway (có statusCode, headers, body JSON) hoặc trả trực tiếp cho Step Functions."""
    if is_api_gateway:
        return _build_response(http_status, result)
    return result


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Điểm tiếp nhận chính của Dispatcher Lambda:
    - Hỗ trợ cả direct invocation (từ Step Functions / SQS) lẫn API Gateway Proxy event.
    """
    logger.info("Dispatcher received event: %s", json.dumps(event, default=str))

    # 1. Trích xuất payload từ event
    payload = event
    is_api_gateway = False

    if isinstance(event, dict) and "body" in event:
        is_api_gateway = True
        raw_body = event.get("body")
        if isinstance(raw_body, str):
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                return _format_result(is_api_gateway, 400, {
                    "status": "ERROR",
                    "message": "Nội dung body trong request không phải JSON hợp lệ.",
                    "error_code": "InvalidJSON"
                })
        elif isinstance(raw_body, dict):
            payload = raw_body

    if not isinstance(payload, dict):
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR",
            "message": "Request payload phải là đối tượng JSON.",
            "error_code": "InvalidPayload"
        })

    # 2. Đọc các trường điều phối nghiệp vụ
    service_name = payload.get("service")
    action = payload.get("action") or payload.get("tool_name")
    parameters = payload.get("parameters", {})
    credentials = payload.get("credentials")
    confirmed = payload.get("confirmed", False)

    if not service_name:
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR",
            "message": "Thiếu trường 'service' (ví dụ: 'lambda', 'ec2', 's3', 'iam').",
            "error_code": "MissingService"
        })

    if not action:
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR",
            "message": "Thiếu trường 'action' hoặc 'tool_name'.",
            "error_code": "MissingAction"
        })

    # Gói params truyền cho service execution
    service_params = {
        "action": action,
        "parameters": parameters,
        "confirmed": confirmed,
        "region": payload.get("region", "us-east-1"),
    }

    # 3. Phân luồng điều phối đến từng dịch vụ
    result = None
    try:
        if service_name == "lambda":
            result = lambda_service.execute(credentials, service_params)
        elif service_name == "ec2":
            result = ec2_service.execute(credentials, service_params)
        elif service_name == "s3":
            result = s3_service.execute(credentials, service_params)
        elif service_name == "iam":
            result = iam_service.execute(credentials, service_params)
        else:
            result = {
                "status": "ERROR",
                "message": f"Dịch vụ '{service_name}' chưa được hỗ trợ. Các dịch vụ hợp lệ: lambda, ec2, s3, iam.",
                "error_code": "UnsupportedService"
            }
    except Exception as exc:
        logger.exception("Lỗi trong quá trình thực thi service %s", service_name)
        result = {
            "status": "ERROR",
            "message": f"Lỗi hệ thống khi điều phối dịch vụ '{service_name}': {str(exc)}",
            "error_code": "DispatcherExecutionError"
        }

    # 4. Trả về kết quả
    http_status = 200
    if result.get("status") == "ERROR":
        http_status = 400 if result.get("error_code") in ("InvalidParameters", "UnsupportedService", "MissingService", "MissingAction") else 500
    elif result.get("status") == "CONFIRMATION_REQUIRED":
        http_status = 200  # Vẫn là 200 OK nhưng body báo CONFIRMATION_REQUIRED để FE hỏi người dùng

    return _format_result(is_api_gateway, http_status, result)
