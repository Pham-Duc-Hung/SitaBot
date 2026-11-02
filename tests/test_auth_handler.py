"""
Unit tests cho Cognito Post Confirmation Lambda Trigger (auth_handler).
Kiểm thử tự động khởi tạo User Profile trong DynamoDB, phân quyền ban đầu (User/Admin),
tính Idempotency (chống ghi đè) và đảm bảo luôn trả về event theo chuẩn Cognito.
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

from unittest.mock import MagicMock, patch

from backend.functions.auth_handler.index import (
    handle_post_confirmation,
    lambda_handler,
)


def _make_cognito_event(
    trigger_source: str = "PostConfirmation_ConfirmSignUp",
    sub: str = "cognito-sub-1234-abcd",
    email: str = "duchuy@uet.vnu.edu.vn",
    name: str = "Nguyen Duc Huy",
    custom_role: str = None,
    user_pool_id: str = "ap-southeast-2_TestPool",
) -> dict:
    """Tạo mock event chuẩn từ AWS Cognito Post-Confirmation Trigger."""
    user_attrs = {
        "sub": sub,
        "email": email,
        "email_verified": "true",
        "name": name,
    }
    if custom_role:
        user_attrs["custom:role"] = custom_role

    return {
        "version": "1",
        "region": "ap-southeast-2",
        "userPoolId": user_pool_id,
        "userName": sub,
        "triggerSource": trigger_source,
        "request": {
            "userAttributes": user_attrs,
        },
        "response": {},
    }


# ==============================================================================
# 1. KIỂM THỬ KHỞI TẠO USER PROFILE MỚI
# ==============================================================================

@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_successful_user_creation(mock_get_db, mock_put_db):
    """Kiểm tra tạo hồ sơ người dùng mới bình thường với role mặc định là 'user'."""
    mock_get_db.return_value = None  # User chưa có trong DB
    mock_put_db.return_value = True

    event = _make_cognito_event(email="student@uet.vnu.edu.vn")
    res = lambda_handler(event, None)

    # Bắt buộc Cognito phải nhận lại event
    assert res == event

    # Xác nhận put_db_item được gọi
    assert mock_put_db.called
    saved_item = mock_put_db.call_args[1]["item"]
    assert saved_item["user_id"] == "cognito-sub-1234-abcd"
    assert saved_item["email"] == "student@uet.vnu.edu.vn"
    assert saved_item["role"] == "user"
    assert saved_item["status"] == "ACTIVE"
    assert saved_item["email_verified"] is True
    assert "created_at" in saved_item
    assert "updated_at" in saved_item


# ==============================================================================
# 2. KIỂM THỬ PHÂN QUYỀN ADMIN (WHITELIST EMAIL & CUSTOM:ROLE)
# ==============================================================================

@patch("backend.functions.auth_handler.index.ADMIN_EMAILS", ["admin@satibot.com", "duchuy@uet.vnu.edu.vn"])
@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_admin_assignment_via_admin_emails(mock_get_db, mock_put_db):
    """Kiểm tra người dùng có email trong whitelist ADMIN_EMAILS tự động được cấp role 'admin'."""
    mock_get_db.return_value = None
    mock_put_db.return_value = True

    event = _make_cognito_event(email="duchuy@uet.vnu.edu.vn")
    res = lambda_handler(event, None)

    assert res == event
    saved_item = mock_put_db.call_args[1]["item"]
    assert saved_item["role"] == "admin"


@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_admin_assignment_via_custom_role(mock_get_db, mock_put_db):
    """Kiểm tra gán role 'admin' khi Cognito user có custom:role='admin'."""
    mock_get_db.return_value = None
    mock_put_db.return_value = True

    event = _make_cognito_event(email="another@example.com", custom_role="admin")
    res = lambda_handler(event, None)

    assert res == event
    saved_item = mock_put_db.call_args[1]["item"]
    assert saved_item["role"] == "admin"


# ==============================================================================
# 3. KIỂM THỬ TÍNH IDEMPOTENCY & BỎ QUA TRIGGER KHÔNG LIÊN QUAN
# ==============================================================================

@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_idempotent_existing_user(mock_get_db, mock_put_db):
    """Nếu user đã tồn tại trong DB, không được ghi đè để bảo vệ role hiện tại."""
    mock_get_db.return_value = {
        "user_id": "cognito-sub-1234-abcd",
        "email": "student@uet.vnu.edu.vn",
        "role": "admin",  # Đã được thăng hạng trước đó
    }

    event = _make_cognito_event()
    res = lambda_handler(event, None)

    assert res == event
    # Không được gọi put_db_item
    assert not mock_put_db.called


@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_ignore_forgot_password_trigger(mock_get_db, mock_put_db):
    """Bỏ qua sự kiện PostConfirmation_ConfirmForgotPassword (chỉ đổi mật khẩu, không tạo user mới)."""
    event = _make_cognito_event(trigger_source="PostConfirmation_ConfirmForgotPassword")
    res = lambda_handler(event, None)

    assert res == event
    assert not mock_get_db.called
    assert not mock_put_db.called


# ==============================================================================
# 4. KIỂM THỬ XỬ LÝ LỖI AN TOÀN (FAULT TOLERANCE)
# ==============================================================================

@patch("backend.functions.auth_handler.index.put_db_item")
@patch("backend.functions.auth_handler.index.get_db_item")
def test_post_confirmation_missing_user_id(mock_get_db, mock_put_db):
    """Sự kiện thiếu cả 'sub' và 'userName' được xử lý an toàn không crash."""
    event = {
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "request": {"userAttributes": {"email": "test@test.com"}},
    }
    res = lambda_handler(event, None)
    assert res == event
    assert not mock_put_db.called


@patch("backend.functions.auth_handler.index.handle_post_confirmation")
def test_lambda_handler_catches_all_exceptions(mock_handle):
    """Dù có ngoại lệ bất ngờ xảy ra, Lambda vẫn bắt lỗi và trả về event cho Cognito."""
    mock_handle.side_effect = RuntimeError("Bất ngờ mất kết nối mạng hoặc lỗi cú pháp")

    event = {"triggerSource": "PostConfirmation_ConfirmSignUp"}
    res = lambda_handler(event, None)

    # Vẫn trả về event an toàn
    assert res == event
