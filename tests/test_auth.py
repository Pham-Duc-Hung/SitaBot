"""
Unit tests cho module xác thực và phân quyền (Authentication & Authorization).
Kiểm thử toàn diện xác thực token JWT, Mock token, RBAC hierarchy và trích xuất claims từ API Gateway.
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

import base64
import json
import time
from unittest.mock import MagicMock, patch

from backend.shared.auth import (
    ROLE_HIERARCHY,
    _base64url_decode,
    check_permission,
    extract_user_from_event,
    verify_token,
)


def _create_mock_jwt(payload: dict, header: dict = None) -> str:
    """Tạo chuỗi JWT giả lập hợp lệ để kiểm thử."""
    hdr = header or {"alg": "RS256", "typ": "JWT"}
    hdr_b64 = base64.urlsafe_b64encode(json.dumps(hdr).encode()).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig_b64 = "MOCK_SIGNATURE_FOR_TESTING"
    return f"{hdr_b64}.{payload_b64}.{sig_b64}"


# ==============================================================================
# 1. KIỂM THỬ GIẢI MÃ BASE64URL & ĐỊNH DẠNG JWT CƠ BẢN
# ==============================================================================

def test_base64url_decode():
    """Kiểm tra giải mã Base64URL với các mức padding bù dấu '=' khác nhau."""
    raw = b"Hello SatiBot Cognito Auth"
    # Mã hóa không padding
    b64_str = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    decoded = _base64url_decode(b64_str)
    assert decoded == raw


def test_verify_token_invalid_inputs():
    """Kiểm tra token rỗng, None hoặc sai định dạng chuỗi."""
    assert verify_token("")["is_valid"] is False
    assert verify_token(None)["is_valid"] is False
    assert verify_token("not_a_valid_token")["is_valid"] is False
    assert verify_token("part1.part2")["is_valid"] is False
    assert verify_token("p1.p2.p3.p4")["is_valid"] is False


def test_verify_token_corrupted_payload():
    """Kiểm tra token có payload bị hỏng không thể parse JSON."""
    bad_token = "eyJhbGciOiJSUzI1NiJ9.bm90LWpzb24tZGF0YQ.sig"
    res = verify_token(bad_token)
    assert res["is_valid"] is False
    assert "Lỗi giải mã payload JWT" in res["error"]


# ==============================================================================
# 2. KIỂM THỬ MOCK TOKEN (DEV / TEST ENVIRONMENT)
# ==============================================================================

def test_verify_token_mock_admin():
    """Kiểm tra token mock admin."""
    res = verify_token("mock-admin")
    assert res["is_valid"] is True
    assert res["role"] == "admin"
    assert "admin" in res["groups"]
    assert res["user_id"] == "usr_mock_admin_001"
    assert res["error"] is None


def test_verify_token_mock_user():
    """Kiểm tra token mock user thông thường."""
    res = verify_token("mock-user")
    assert res["is_valid"] is True
    assert res["role"] == "user"
    assert "user" in res["groups"]
    assert res["user_id"] == "usr_mock_user_002"
    assert res["error"] is None


def test_verify_token_mock_custom_format():
    """Kiểm tra mock token định dạng mở rộng: mock-token:<user_id>:<role>:<email>."""
    token = "mock-token:huy_uet:admin:huy@vnu.edu.vn"
    res = verify_token(token)
    assert res["is_valid"] is True
    assert res["user_id"] == "huy_uet"
    assert res["role"] == "admin"
    assert res["email"] == "huy@vnu.edu.vn"


def test_verify_token_with_bearer_prefix():
    """Kiểm tra tiền tố Bearer được tự động loại bỏ."""
    res = verify_token("Bearer mock-admin")
    assert res["is_valid"] is True
    assert res["role"] == "admin"


# ==============================================================================
# 3. KIỂM THỬ JWT TOKEN CHUẨN COGNITO (PAYLOAD, CLAIMS, EXPIRATION)
# ==============================================================================

def test_verify_token_valid_jwt_user():
    """Kiểm tra JWT Cognito hợp lệ với vai trò user."""
    payload = {
        "sub": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "email": "user@example.com",
        "cognito:username": "user@example.com",
        "exp": int(time.time()) + 3600,  # Còn hạn 1 tiếng
        "cognito:groups": ["user"],
    }
    jwt_token = _create_mock_jwt(payload)
    res = verify_token(jwt_token)

    assert res["is_valid"] is True
    assert res["user_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert res["email"] == "user@example.com"
    assert res["role"] == "user"
    assert res["error"] is None


def test_verify_token_valid_jwt_admin():
    """Kiểm tra JWT Cognito hợp lệ với vai trò admin qua cognito:groups."""
    payload = {
        "sub": "admin-sub-9999",
        "email": "admin@example.com",
        "exp": int(time.time()) + 3600,
        "cognito:groups": ["admin", "developers"],
    }
    jwt_token = _create_mock_jwt(payload)
    res = verify_token(jwt_token)

    assert res["is_valid"] is True
    assert res["user_id"] == "admin-sub-9999"
    assert res["role"] == "admin"


def test_verify_token_valid_jwt_admin_custom_role():
    """Kiểm tra JWT Cognito phân quyền admin thông qua custom:role."""
    payload = {
        "sub": "dev-sub-8888",
        "email": "dev@example.com",
        "exp": int(time.time()) + 3600,
        "custom:role": "admin",
    }
    jwt_token = _create_mock_jwt(payload)
    res = verify_token(jwt_token)

    assert res["is_valid"] is True
    assert res["role"] == "admin"


def test_verify_token_expired_jwt():
    """Kiểm tra JWT đã hết hạn phải bị từ chối xác thực."""
    payload = {
        "sub": "expired-user-id",
        "email": "expired@example.com",
        "exp": int(time.time()) - 100,  # Đã hết hạn từ 100 giây trước
    }
    jwt_token = _create_mock_jwt(payload)
    res = verify_token(jwt_token)

    assert res["is_valid"] is False
    assert "Token đã hết hạn" in res["error"]
    assert res["role"] == "guest"


# ==============================================================================
# 4. KIỂM THỬ TRÍCH XUẤT NGƯỜI DÙNG TỪ LAMBDA EVENT (API GATEWAY)
# ==============================================================================

def test_extract_user_from_event_authorizer_claims():
    """Kiểm tra trích xuất thông tin user từ requestContext.authorizer.claims."""
    event = {
        "requestContext": {
            "authorizer": {
                "claims": {
                    "sub": "cognito-sub-1111",
                    "email": "duchuy@uet.vnu.edu.vn",
                    "cognito:groups": "admin",
                }
            }
        }
    }
    user = extract_user_from_event(event)
    assert user["is_authenticated"] is True
    assert user["user_id"] == "cognito-sub-1111"
    assert user["email"] == "duchuy@uet.vnu.edu.vn"
    assert user["role"] == "admin"


def test_extract_user_from_event_authorization_header():
    """Kiểm tra fallback trích xuất token từ header khi chưa có authorizer claims."""
    event = {
        "headers": {
            "Authorization": "Bearer mock-user"
        }
    }
    user = extract_user_from_event(event)
    assert user["is_authenticated"] is True
    assert user["user_id"] == "usr_mock_user_002"
    assert user["role"] == "user"


def test_extract_user_from_event_anonymous():
    """Kiểm tra trường hợp event không có xác thực (Anonymous Guest)."""
    assert extract_user_from_event({})["is_authenticated"] is False
    assert extract_user_from_event({})["user_id"] == "anonymous"
    assert extract_user_from_event({})["role"] == "guest"


# ==============================================================================
# 5. KIỂM THỬ PHÂN QUYỀN RBAC (ROLE-BASED ACCESS CONTROL)
# ==============================================================================

def test_check_permission_hierarchy():
    """Kiểm tra thứ bậc phân quyền: Admin (100) > User (10) > Guest (0)."""
    # 1. Admin có quyền truy cập tất cả
    assert check_permission("admin_id", "admin", user_role="admin") is True
    assert check_permission("admin_id", "user", user_role="admin") is True
    assert check_permission("admin_id", "guest", user_role="admin") is True

    # 2. User có quyền trên user và guest, KHÔNG có quyền trên admin
    assert check_permission("user_id", "user", user_role="user") is True
    assert check_permission("user_id", "guest", user_role="user") is True
    assert check_permission("user_id", "admin", user_role="user") is False

    # 3. Guest chỉ có quyền trên guest
    assert check_permission("guest_id", "guest", user_role="guest") is True
    assert check_permission("guest_id", "user", user_role="guest") is False
    assert check_permission("guest_id", "admin", user_role="guest") is False

    # 4. Vai trò yêu cầu không hợp lệ
    assert check_permission("admin_id", "super_god_mode", user_role="admin") is False
    assert check_permission("admin_id", "", user_role="admin") is True


def test_check_permission_with_dynamodb_lookup_success():
    """Kiểm tra tự động tra cứu vai trò từ DynamoDB khi chưa truyền user_role."""
    mock_boto3 = MagicMock()
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"user_id": "usr_db_01", "role": "admin"}}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        # Yêu cầu role 'admin', user trong DB có role 'admin' -> True
        allowed = check_permission("usr_db_01", "admin")
        assert allowed is True


def test_check_permission_with_dynamodb_lookup_fallback():
    """Kiểm tra tra cứu DynamoDB thất bại thì an toàn fallback về 'user'."""
    mock_boto3 = MagicMock()
    mock_boto3.resource.side_effect = Exception("DynamoDB connection failed")

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        # Fallback về 'user', yêu cầu 'admin' -> False
        assert check_permission("usr_fail", "admin") is False
        # Fallback về 'user', yêu cầu 'user' -> True
        assert check_permission("usr_fail", "user") is True
