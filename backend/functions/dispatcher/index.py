"""
Author: Trịnh Thiên Quang
Điều phối các yêu cầu từ API Gateway hoặc Step Functions đến service tương ứng.
"""

import json
import logging
from typing import Any, Dict

try:
    from backend.shared.runtime import get_aws_region, safe_json_for_log
except ImportError:
    from runtime import get_aws_region, safe_json_for_log

# Import các service con (hỗ trợ cả chạy cục bộ lẫn đóng gói Lambda)
try:
    from services import lambda_service
    from services import ec2_service
    from services import s3_service
    from services import iam_service
except ImportError:
    from backend.functions.dispatcher.services import lambda_service
    from backend.functions.dispatcher.services import ec2_service
    from backend.functions.dispatcher.services import s3_service
    from backend.functions.dispatcher.services import iam_service

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


def _http_status_for_result(result: Dict[str, Any]) -> int:
    """Map the shared service contract to a meaningful HTTP status."""
    if result.get("status") == "NOT_IMPLEMENTED":
        return 501
    if result.get("status") == "CONFIRMATION_REQUIRED":
        return 200
    if result.get("status") != "ERROR":
        return 200
    code = result.get("error_code")
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
        return 403
    if code == "ResourceNotFoundException":
        return 404
    if code in {"ResourceConflictException", "PreconditionFailedException"}:
        return 409
    if code == "TooManyRequestsException":
        return 429
    if code in {
        "InvalidJSON", "InvalidPayload", "InvalidParameters", "UnsupportedService",
        "UnsupportedTool", "MissingService", "MissingAction",
    }:
        return 400
    return 500


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Điểm tiếp nhận chính của Dispatcher Lambda:
    - Hỗ trợ cả direct invocation (từ Step Functions / SQS) lẫn API Gateway Proxy event.
    """
    logger.info("Dispatcher received event: %s", safe_json_for_log(event))

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

    if not isinstance(parameters, dict):
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR", "message": "Trường 'parameters' phải là JSON object.",
            "error_code": "InvalidParameters",
        })
    if credentials is not None and not isinstance(credentials, dict):
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR", "message": "Trường 'credentials' phải là JSON object hoặc null.",
            "error_code": "InvalidParameters",
        })
    if not isinstance(confirmed, bool):
        return _format_result(is_api_gateway, 400, {
            "status": "ERROR", "message": "Trường 'confirmed' phải là boolean.",
            "error_code": "InvalidParameters",
        })

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
        "region": get_aws_region(payload.get("region")),
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

    # Đảm bảo kết quả luôn là dict kể cả khi service con chưa được triển khai hoàn chỉnh
    if not isinstance(result, dict):
        result = {
            "status": "NOT_IMPLEMENTED",
            "message": f"Dịch vụ '{service_name}' đang được phát triển bởi thành viên phụ trách.",
            "error_code": "ServiceUnderDevelopment"
        }

    # 4. Trả về kết quả
    return _format_result(is_api_gateway, _http_status_for_result(result), result)
