"""
Module tiện ích thao tác với Amazon DynamoDB dùng chung cho SatiBot.
Author: Nguyễn Đức Huy (Nhóm 10 - SatiBot)
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("satibot.db")
logger.setLevel(logging.INFO)

_DYNAMODB_RESOURCE = None


def get_dynamodb_resource():
    """Khởi tạo và tái sử dụng singleton resource DynamoDB."""
    global _DYNAMODB_RESOURCE
    if _DYNAMODB_RESOURCE is None:
        try:
            import boto3
            region = os.environ.get("AWS_REGION", os.environ.get("DEFAULT_REGION", "ap-southeast-2"))
            _DYNAMODB_RESOURCE = boto3.resource("dynamodb", region_name=region)
        except Exception as exc:
            logger.warning(f"Không thể khởi tạo Boto3 DynamoDB resource: {exc}")
            return None
    return _DYNAMODB_RESOURCE


def get_db_item(table_name: str, key: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Lấy một bản ghi từ bảng DynamoDB theo khóa chính.

    :param table_name: Tên bảng DynamoDB
    :param key: Dict chứa khóa chính (ví dụ: {'user_id': 'usr_123'})
    :return: Dict chứa Item hoặc None nếu không tìm thấy/lỗi
    """
    if not table_name or not key:
        return None

    dynamo = get_dynamodb_resource()
    if dynamo is None:
        logger.warning(f"DynamoDB resource chưa sẵn sàng khi truy vấn bảng {table_name}")
        return None

    try:
        table = dynamo.Table(table_name)
        response = table.get_item(Key=key)
        return response.get("Item")
    except Exception as exc:
        logger.error(f"Lỗi khi get_db_item trên bảng {table_name} với key={key}: {exc}")
        return None


def put_db_item(
    table_name: str,
    item: Dict[str, Any],
    condition_expression: Optional[str] = None,
) -> bool:
    """
    Ghi một bản ghi vào bảng DynamoDB.

    :param table_name: Tên bảng DynamoDB
    :param item: Dữ liệu bản ghi cần lưu
    :param condition_expression: Biểu thức điều kiện (ví dụ: 'attribute_not_exists(user_id)')
    :return: True nếu ghi thành công, False nếu thất bại hoặc vi phạm điều kiện
    """
    if not table_name or not item:
        return False

    dynamo = get_dynamodb_resource()
    if dynamo is None:
        logger.warning(f"DynamoDB resource chưa sẵn sàng khi ghi vào bảng {table_name}")
        return False

    try:
        table = dynamo.Table(table_name)
        kwargs: Dict[str, Any] = {"Item": item}
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        table.put_item(**kwargs)
        return True
    except Exception as exc:
        # Nếu vi phạm điều kiện (ConditionalCheckFailedException), ghi log debug
        if "ConditionalCheckFailedException" in str(exc):
            logger.debug(f"Bản ghi đã tồn tại hoặc vi phạm điều kiện trên bảng {table_name}: {exc}")
        else:
            logger.error(f"Lỗi khi put_db_item trên bảng {table_name}: {exc}")
        return False
