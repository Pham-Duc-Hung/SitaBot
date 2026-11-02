"""
AWS Lambda Trigger: Cognito Post Confirmation.
Được kích hoạt tự động sau khi người dùng xác nhận đăng ký tài khoản (Confirm Sign Up).
Chức năng: Tự động khởi tạo hồ sơ người dùng trong DynamoDB bảng Users (Role, Email, Trạng thái).
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

import datetime
import logging
import os
from typing import Any, Dict

try:
    from backend.shared.db import get_db_item, put_db_item
except ImportError:
    try:
        from shared.db import get_db_item, put_db_item
    except ImportError:
        from db import get_db_item, put_db_item

logger = logging.getLogger("satibot.auth_handler")
logger.setLevel(logging.INFO)

USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "SatiBot-Users")
ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", "").split(",")
    if email.strip()
]


def _get_current_utc_iso() -> str:
    """Trả về thời gian UTC theo chuẩn ISO 8601."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def handle_post_confirmation(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Xử lý logic nghiệp vụ khi có sự kiện Post-Confirmation từ AWS Cognito.
    """
    trigger_source = event.get("triggerSource", "")
    logger.info(f"Tiếp nhận sự kiện Cognito Trigger: {trigger_source}")

    # Chỉ xử lý sự kiện người dùng xác nhận đăng ký tài khoản mới
    if trigger_source != "PostConfirmation_ConfirmSignUp":
        logger.info(f"Bỏ qua triggerSource '{trigger_source}', không cần tạo bản ghi user mới.")
        return event

    request_data = event.get("request", {})
    user_attributes = request_data.get("userAttributes", {})

    # Trích xuất thông tin người dùng từ User Attributes của Cognito
    user_id = user_attributes.get("sub") or event.get("userName")
    email = user_attributes.get("email", "").strip().lower()
    name = user_attributes.get("name", "")
    email_verified_raw = user_attributes.get("email_verified", "false")
    email_verified = str(email_verified_raw).lower() in ("true", "1")
    user_pool_id = event.get("userPoolId", "")

    if not user_id:
        logger.warning("Không tìm thấy user_id hoặc sub trong sự kiện Cognito, hủy tạo bản ghi.")
        return event

    # Xác định vai trò (Role): Nếu nằm trong whitelist ADMIN_EMAILS hoặc có custom:role='admin'
    custom_role = user_attributes.get("custom:role", "").strip().lower()
    if email in ADMIN_EMAILS or custom_role == "admin":
        assigned_role = "admin"
    else:
        assigned_role = "user"

    now_iso = _get_current_utc_iso()

    # Kiểm tra Idempotency: Nếu user đã tồn tại, tránh ghi đè làm mất role đã cập nhật trước đó
    existing_user = get_db_item(USERS_TABLE_NAME, {"user_id": user_id})
    if existing_user:
        logger.info(f"User {user_id} ({email}) đã tồn tại trong DynamoDB, bỏ qua ghi đè.")
        return event

    user_record = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "role": assigned_role,
        "status": "ACTIVE",
        "email_verified": email_verified,
        "user_pool_id": user_pool_id,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    # Ghi vào DynamoDB với điều kiện user_id chưa tồn tại (Atomic Check)
    success = put_db_item(
        table_name=USERS_TABLE_NAME,
        item=user_record,
        condition_expression="attribute_not_exists(user_id)",
    )

    if success:
        logger.info(f"Đã tạo thành công bản ghi User profile: {user_id} - Role: {assigned_role} - Email: {email}")
    else:
        logger.warning(f"Không thể ghi bản ghi User profile cho user {user_id} vào bảng {USERS_TABLE_NAME}")

    return event


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Entrypoint tiếp nhận sự kiện từ AWS Cognito Post Confirmation Trigger.
    QUY TẮC BẮT BUỘC CỦA COGNITO: Phải luôn trả về chính `event` để không làm gián đoạn luồng đăng ký.
    """
    try:
        return handle_post_confirmation(event)
    except Exception as exc:
        logger.error(f"Lỗi không mong muốn trong lambda_handler auth_handler: {exc}", exc_info=True)
        # Luôn trả về event để Cognito không chặn hoàn tất đăng ký của người dùng
        return event
