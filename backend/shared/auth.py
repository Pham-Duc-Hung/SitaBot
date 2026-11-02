"""
Module xác thực và phân quyền (Authentication & Authorization) dùng chung cho SatiBot.
Hỗ trợ xác thực AWS Cognito JWT Token, trích xuất claims từ API Gateway Authorizer,
và phân quyền theo vai trò (Role-Based Access Control - RBAC).
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("satibot.auth")
logger.setLevel(logging.INFO)

# Thứ bậc phân quyền (Role Hierarchy)
ROLE_HIERARCHY = {
    "admin": 100,
    "user": 10,
    "guest": 0,
}

# Tên bảng DynamoDB quản lý thông tin User
USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "SatiBot-Users")


def _base64url_decode(input_str: str) -> bytes:
    """
    Giải mã chuỗi Base64URL tiêu chuẩn của JWT, tự động bù padding '=' nếu thiếu.
    """
    rem = len(input_str) % 4
    if rem > 0:
        input_str += "=" * (4 - rem)
    return base64.urlsafe_b64decode(input_str.encode("utf-8"))


def verify_token(token: str) -> Dict[str, Any]:
    """
    Xác thực JWT token và trả về thông tin user (user_id, email, role, groups).
    
    Hỗ trợ:
      1. Mock Token cho môi trường Dev/Testing (tiền tố 'mock-')
      2. JWT Token thực tế từ AWS Cognito (giải mã Payload, kiểm tra exp, claims)

    :param token: Chuỗi JWT token hoặc Bearer token
    :return: Dict chứa:
             - is_valid (bool): Token có hợp lệ không
             - user_id (str): Định danh người dùng (Cognito 'sub')
             - email (str): Email người dùng
             - role (str): Vai trò ('admin' hoặc 'user')
             - groups (list): Danh sách Cognito User Groups
             - claims (dict): Toàn bộ claims đã giải mã
             - error (str | None): Thông báo lỗi nếu không hợp lệ
    """
    if not token or not isinstance(token, str):
        return {
            "is_valid": False,
            "user_id": None,
            "email": None,
            "role": "guest",
            "groups": [],
            "claims": {},
            "error": "Token rỗng hoặc không đúng định dạng chuỗi.",
        }

    # Bỏ tiền tố 'Bearer ' nếu có
    token_str = token.strip()
    if token_str.lower().startswith("bearer "):
        token_str = token_str[7:].strip()

    # 1. Hỗ trợ Mock Token trong môi trường Local Dev / Testing
    if token_str.startswith("mock-"):
        return _handle_mock_token(token_str)

    # 2. Xử lý JWT Token chuẩn
    parts = token_str.split(".")
    if len(parts) != 3:
        return {
            "is_valid": False,
            "user_id": None,
            "email": None,
            "role": "guest",
            "groups": [],
            "claims": {},
            "error": "JWT token không hợp lệ (phải gồm 3 phần header.payload.signature).",
        }

    try:
        payload_bytes = _base64url_decode(parts[1])
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        logger.warning(f"Không thể giải mã payload JWT: {exc}")
        return {
            "is_valid": False,
            "user_id": None,
            "email": None,
            "role": "guest",
            "groups": [],
            "claims": {},
            "error": f"Lỗi giải mã payload JWT: {str(exc)}",
        }

    # Kiểm tra thời hạn token (Claim 'exp')
    now = int(time.time())
    exp = payload.get("exp")
    if exp and isinstance(exp, (int, float)) and now > exp:
        return {
            "is_valid": False,
            "user_id": payload.get("sub"),
            "email": payload.get("email"),
            "role": "guest",
            "groups": [],
            "claims": payload,
            "error": "Token đã hết hạn (Token expired).",
        }

    # Trích xuất thông tin người dùng từ claims của Cognito
    user_id = payload.get("sub") or payload.get("username") or payload.get("cognito:username")
    email = payload.get("email") or ""
    
    # Xác định Cognito Groups và Role
    groups = payload.get("cognito:groups", [])
    if isinstance(groups, str):
        groups = [groups]

    custom_role = payload.get("custom:role")
    if "admin" in groups or custom_role == "admin":
        role = "admin"
    else:
        role = "user"

    return {
        "is_valid": True,
        "user_id": user_id,
        "email": email,
        "role": role,
        "groups": groups,
        "claims": payload,
        "error": None,
    }


def _handle_mock_token(token: str) -> Dict[str, Any]:
    """Xử lý các mock token phục vụ unit test và dev cục bộ."""
    if token == "mock-admin":
        return {
            "is_valid": True,
            "user_id": "usr_mock_admin_001",
            "email": "admin@satibot.internal",
            "role": "admin",
            "groups": ["admin"],
            "claims": {"sub": "usr_mock_admin_001", "email": "admin@satibot.internal", "cognito:groups": ["admin"]},
            "error": None,
        }
    elif token == "mock-user":
        return {
            "is_valid": True,
            "user_id": "usr_mock_user_002",
            "email": "user@satibot.internal",
            "role": "user",
            "groups": ["user"],
            "claims": {"sub": "usr_mock_user_002", "email": "user@satibot.internal", "cognito:groups": ["user"]},
            "error": None,
        }
    elif token.startswith("mock-token:"):
        # Định dạng mở rộng: mock-token:<user_id>:<role>:<email>
        parts = token.split(":")
        uid = parts[1] if len(parts) > 1 else "mock_id"
        role = parts[2] if len(parts) > 2 else "user"
        mail = parts[3] if len(parts) > 3 else f"{uid}@satibot.internal"
        return {
            "is_valid": True,
            "user_id": uid,
            "email": mail,
            "role": role,
            "groups": [role],
            "claims": {"sub": uid, "email": mail, "custom:role": role},
            "error": None,
        }

    return {
        "is_valid": False,
        "user_id": None,
        "email": None,
        "role": "guest",
        "groups": [],
        "claims": {},
        "error": f"Mock token không hợp lệ: '{token}'",
    }


def extract_user_from_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trích xuất thông tin người dùng từ Lambda Event.
    
    Ưu tiên theo thứ tự:
      1. Claims đã được API Gateway Cognito Authorizer xác thực (event.requestContext.authorizer.claims)
      2. Header 'Authorization' chứa Bearer JWT token
      3. Thông tin user truyền trực tiếp trong body (nếu môi trường test/local)
    """
    if not isinstance(event, dict):
        return {
            "is_authenticated": False,
            "user_id": "anonymous",
            "email": "",
            "role": "guest",
            "groups": [],
        }

    # 1. Kiểm tra API Gateway Cognito Authorizer Claims
    req_context = event.get("requestContext") or {}
    authorizer = req_context.get("authorizer") or {}
    claims = authorizer.get("claims") or {}

    if claims and isinstance(claims, dict):
        user_id = claims.get("sub") or claims.get("cognito:username")
        email = claims.get("email", "")
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = [groups]

        custom_role = claims.get("custom:role")
        role = "admin" if ("admin" in groups or custom_role == "admin") else "user"

        return {
            "is_authenticated": True,
            "user_id": user_id,
            "email": email,
            "role": role,
            "groups": groups,
            "claims": claims,
        }

    # 2. Kiểm tra Header Authorization
    headers = event.get("headers") or {}
    auth_header = (
        headers.get("Authorization")
        or headers.get("authorization")
        or headers.get("AUTHORIZATION")
    )
    if auth_header:
        token_info = verify_token(auth_header)
        if token_info["is_valid"]:
            return {
                "is_authenticated": True,
                "user_id": token_info["user_id"],
                "email": token_info["email"],
                "role": token_info["role"],
                "groups": token_info["groups"],
                "claims": token_info["claims"],
            }

    # 3. Fallback cho người dùng ẩn danh (Anonymous / Unauthenticated)
    return {
        "is_authenticated": False,
        "user_id": "anonymous",
        "email": "",
        "role": "guest",
        "groups": [],
        "claims": {},
    }


def check_permission(
    user_id: str,
    required_role: str,
    user_role: Optional[str] = None,
) -> bool:
    """
    Kiểm tra quyền của người dùng theo mô hình RBAC phân cấp.
    
    Quy tắc:
      - 'admin' (level 100) có toàn quyền trên các tài nguyên yêu cầu 'admin', 'user', 'guest'.
      - 'user' (level 10) có quyền trên tài nguyên yêu cầu 'user' hoặc 'guest'.
      - 'guest' (level 0) chỉ có quyền trên tài nguyên công khai 'guest'.

    :param user_id: Định danh người dùng
    :param required_role: Vai trò tối thiểu yêu cầu ('admin', 'user', 'guest')
    :param user_role: Vai trò hiện tại của user (nếu đã có từ claims; nếu None sẽ tra cứu DynamoDB)
    :return: True nếu đủ quyền, False nếu không đủ quyền
    """
    if not required_role:
        return True

    req_level = ROLE_HIERARCHY.get(required_role.lower())
    if req_level is None:
        logger.warning(f"Vai trò yêu cầu không hợp lệ: '{required_role}'")
        return False

    # Nếu chưa truyền user_role, tiến hành tra cứu từ DynamoDB
    current_role = user_role
    if not current_role and user_id and user_id != "anonymous":
        current_role = _get_user_role_from_db(user_id)

    if not current_role:
        current_role = "guest"

    curr_level = ROLE_HIERARCHY.get(current_role.lower(), 0)
    return curr_level >= req_level


def _get_user_role_from_db(user_id: str) -> str:
    """
    Tra cứu vai trò của user từ DynamoDB bảng Users.
    Tự động fallback về 'user' nếu gặp lỗi kết nối hoặc môi trường test.
    """
    try:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(USERS_TABLE_NAME)
        resp = table.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if item and "role" in item:
            return str(item["role"])
    except Exception as exc:
        logger.debug(f"Không thể tra cứu role từ DynamoDB cho user {user_id}: {exc}")

    return "user"
