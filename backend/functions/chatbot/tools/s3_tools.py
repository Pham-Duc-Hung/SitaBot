"""Tool Schema trung lập nhà cung cấp LLM cho các thao tác Amazon S3."""

import re
from copy import deepcopy


DEFAULT_AWS_REGION = "us-east-2"
MAX_PRESIGNED_URL_EXPIRATION_SECONDS = 604800

S3_LIFECYCLE_STORAGE_CLASSES = (
    "STANDARD_IA",
    "ONEZONE_IA",
    "INTELLIGENT_TIERING",
    "GLACIER_IR",
    "GLACIER",
    "DEEP_ARCHIVE",
)


def _region_property():
    return {
        "type": "string",
        "minLength": 1,
        "default": DEFAULT_AWS_REGION,
        "description": (
            "AWS Region của bucket, ví dụ us-east-2. "
            "Nếu người dùng không nói rõ thì dùng us-east-2."
        ),
    }


def _bucket_name_property():
    return {
        "type": "string",
        "minLength": 3,
        "maxLength": 63,
        "pattern": r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        "description": (
            "Tên S3 bucket chính xác. Không tự đoán hoặc tự thay đổi tên "
            "mà người dùng cung cấp."
        ),
    }


def _object_schema(properties, required):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _tool(
    *,
    name,
    description,
    action,
    requires_confirmation,
    risk_level,
    mutates_aws,
    input_schema,
    confirmation_mode="none",
):
    return {
        "name": name,
        "description": description,
        "service": "s3",
        "action": action,
        "requires_confirmation": requires_confirmation,
        "confirmation_mode": confirmation_mode,
        "risk_level": risk_level,
        "mutates_aws": mutates_aws,
        "input_schema": input_schema,
    }


S3_TOOL_SCHEMAS = (
    _tool(
        name="s3_create_bucket",
        description=(
            "Tạo một S3 bucket private tại region được yêu cầu và bật đầy "
            "đủ S3 Block Public Access. Chỉ dùng khi người dùng muốn tạo "
            "bucket mới."
        ),
        action="create_bucket",
        requires_confirmation=True,
        confirmation_mode="standard",
        risk_level="medium",
        mutates_aws=True,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "region": _region_property(),
            },
            ["bucket_name"],
        ),
    ),
    _tool(
        name="s3_scan_public_access",
        description=(
            "Quét các S3 bucket trong tài khoản để phát hiện Block Public "
            "Access chưa đầy đủ, bucket policy public hoặc public ACL. "
            "Đây là thao tác chỉ đọc."
        ),
        action="scan_public_access",
        requires_confirmation=False,
        risk_level="low",
        mutates_aws=False,
        input_schema=_object_schema(
            {"region": _region_property()},
            [],
        ),
    ),
    _tool(
        name="s3_block_public_access",
        description=(
            "Bật toàn bộ S3 Block Public Access cho một bucket đã tồn tại. "
            "Dùng sau khi người dùng xác nhận muốn khắc phục rủi ro public."
        ),
        action="block_public_access",
        requires_confirmation=True,
        confirmation_mode="standard",
        risk_level="medium",
        mutates_aws=True,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "region": _region_property(),
            },
            ["bucket_name"],
        ),
    ),
    _tool(
        name="s3_inspect_bucket_for_delete",
        description=(
            "Kiểm tra object, versions và delete markers trước khi xóa "
            "bucket. Luôn gọi tool này trước s3_delete_bucket_safe."
        ),
        action="inspect_bucket_for_delete",
        requires_confirmation=False,
        risk_level="low",
        mutates_aws=False,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "region": _region_property(),
            },
            ["bucket_name"],
        ),
    ),
    _tool(
        name="s3_delete_bucket_safe",
        description=(
            "Xóa vĩnh viễn toàn bộ object versions, delete markers và bucket. "
            "Chỉ gọi sau bước kiểm tra và sau khi người dùng nhập đúng câu "
            "DELETE <bucket-name>. Không tự tạo câu xác nhận thay người dùng."
        ),
        action="delete_bucket_safe",
        requires_confirmation=True,
        confirmation_mode="explicit_phrase",
        risk_level="high",
        mutates_aws=True,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "confirmation": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Câu xác nhận nguyên văn do người dùng nhập, bắt buộc "
                        "có dạng DELETE <bucket-name>."
                    ),
                },
                "expected_deletable_item_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Giá trị deletable_item_count nhận từ tool "
                        "s3_inspect_bucket_for_delete gần nhất."
                    ),
                },
                "region": _region_property(),
            },
            [
                "bucket_name",
                "confirmation",
                "expected_deletable_item_count",
            ],
        ),
    ),
    _tool(
        name="s3_put_lifecycle_rule",
        description=(
            "Thêm hoặc cập nhật một S3 Lifecycle Rule để tự động chuyển "
            "object sang storage class tiết kiệm chi phí. Các rule khác "
            "trong bucket được giữ nguyên."
        ),
        action="put_lifecycle_rule",
        requires_confirmation=True,
        confirmation_mode="standard",
        risk_level="medium",
        mutates_aws=True,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "rule_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": (
                        "ID ổn định của rule. Nếu cập nhật, dùng lại đúng ID."
                    ),
                },
                "prefix": {
                    "type": "string",
                    "maxLength": 1024,
                    "default": "",
                    "description": (
                        "Chỉ áp dụng cho object key có prefix này. Chuỗi rỗng "
                        "nghĩa là toàn bộ bucket."
                    ),
                },
                "transition_days": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Số ngày kể từ khi tạo object trước khi chuyển tầng. "
                        "STANDARD_IA và ONEZONE_IA cần ít nhất 30 ngày."
                    ),
                },
                "storage_class": {
                    "type": "string",
                    "enum": list(S3_LIFECYCLE_STORAGE_CLASSES),
                    "description": "Storage class đích của Lifecycle Rule.",
                },
                "region": _region_property(),
            },
            ["bucket_name", "transition_days", "storage_class"],
        ),
    ),
    _tool(
        name="s3_create_presigned_download",
        description=(
            "Tạo URL tải tạm thời cho một object S3 đang tồn tại mà không "
            "công khai bucket. URL có thời hạn tối đa 7 ngày."
        ),
        action="create_presigned_download",
        requires_confirmation=True,
        confirmation_mode="standard",
        risk_level="medium",
        mutates_aws=False,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "object_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1024,
                    "description": (
                        "Object key đầy đủ và phân biệt chữ hoa, chữ thường."
                    ),
                },
                "expires_in": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PRESIGNED_URL_EXPIRATION_SECONDS,
                    "default": 900,
                    "description": "Thời hạn URL tính bằng giây.",
                },
                "region": _region_property(),
            },
            ["bucket_name", "object_key"],
        ),
    ),
    _tool(
        name="s3_get_storage_analytics",
        description=(
            "Thống kê số object, versions, tổng dung lượng, storage class "
            "và chi phí lưu trữ ước tính của một bucket hoặc prefix. "
            "Đây là thao tác chỉ đọc."
        ),
        action="get_storage_analytics",
        requires_confirmation=False,
        risk_level="low",
        mutates_aws=False,
        input_schema=_object_schema(
            {
                "bucket_name": _bucket_name_property(),
                "prefix": {
                    "type": "string",
                    "maxLength": 1024,
                    "default": "",
                    "description": (
                        "Chỉ thống kê object key có prefix này. Chuỗi rỗng "
                        "nghĩa là toàn bộ bucket."
                    ),
                },
                "region": _region_property(),
            },
            ["bucket_name"],
        ),
    ),
)


_S3_TOOL_BY_NAME = {
    tool["name"]: tool
    for tool in S3_TOOL_SCHEMAS
}


def get_s3_tool_schema(tool_name):
    """Lấy bản sao schema theo tên để caller không sửa dữ liệu dùng chung."""
    try:
        return deepcopy(_S3_TOOL_BY_NAME[tool_name])
    except KeyError as error:
        raise KeyError(f"S3 tool không được hỗ trợ: {tool_name}") from error


def _validate_tool_arguments(tool, arguments):
    """Kiểm tra phần JSON Schema cơ bản trước khi gửi tới Dispatcher."""
    schema = tool["input_schema"]
    properties = schema["properties"]
    unexpected_fields = sorted(set(arguments) - set(properties))

    if unexpected_fields:
        raise ValueError(
            "S3 tool chứa tham số không được hỗ trợ: "
            + ", ".join(unexpected_fields)
        )

    missing_fields = [
        field
        for field in schema["required"]
        if field not in arguments
    ]

    if missing_fields:
        raise ValueError(
            "S3 tool thiếu tham số bắt buộc: "
            + ", ".join(missing_fields)
        )

    for field, value in arguments.items():
        field_schema = properties[field]
        field_type = field_schema.get("type")

        if field_type == "string" and not isinstance(value, str):
            raise ValueError(f"{field} phải là chuỗi.")

        if field_type == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ValueError(f"{field} phải là số nguyên.")

        if isinstance(value, str):
            if len(value) < field_schema.get("minLength", 0):
                raise ValueError(f"{field} quá ngắn.")
            if len(value) > field_schema.get("maxLength", len(value)):
                raise ValueError(f"{field} quá dài.")
            pattern = field_schema.get("pattern")
            if pattern and re.fullmatch(pattern, value) is None:
                raise ValueError(f"{field} không đúng định dạng.")

        if isinstance(value, int) and not isinstance(value, bool):
            if value < field_schema.get("minimum", value):
                raise ValueError(f"{field} nhỏ hơn giá trị cho phép.")
            if value > field_schema.get("maximum", value):
                raise ValueError(f"{field} lớn hơn giá trị cho phép.")

        allowed_values = field_schema.get("enum")
        if allowed_values is not None and value not in allowed_values:
            raise ValueError(f"{field} không thuộc danh sách được hỗ trợ.")


def build_s3_dispatch_request(tool_name, arguments):
    """Chuyển một S3 tool call thành request trung lập cho Dispatcher."""
    if not isinstance(arguments, dict):
        raise TypeError("arguments của S3 tool call phải là dictionary.")

    tool = get_s3_tool_schema(tool_name)
    _validate_tool_arguments(tool, arguments)
    params = dict(arguments)

    # Action chỉ lấy từ mapping nội bộ, không lấy từ nội dung LLM sinh ra.
    params["action"] = tool["action"]

    return {
        "service": tool["service"],
        "params": params,
        "requires_confirmation": tool["requires_confirmation"],
        "confirmation_mode": tool["confirmation_mode"],
        "risk_level": tool["risk_level"],
    }
