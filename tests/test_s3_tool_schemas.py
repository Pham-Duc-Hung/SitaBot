import json
import re

import pytest

from backend.functions.chatbot.tools.s3_tools import (
    MAX_PRESIGNED_URL_EXPIRATION_SECONDS,
    S3_LIFECYCLE_STORAGE_CLASSES,
    S3_TOOL_SCHEMAS,
    build_s3_dispatch_request,
    get_s3_tool_schema,
)
from backend.functions.dispatcher.services.s3_service import (
    SUPPORTED_LIFECYCLE_STORAGE_CLASSES,
)


EXPECTED_ACTIONS = {
    "create_bucket",
    "scan_public_access",
    "block_public_access",
    "inspect_bucket_for_delete",
    "delete_bucket_safe",
    "put_lifecycle_rule",
    "create_presigned_download",
    "get_storage_analytics",
}

READ_ONLY_TOOLS = {
    "s3_scan_public_access",
    "s3_inspect_bucket_for_delete",
    "s3_get_storage_analytics",
}


def test_s3_tool_schemas_cover_every_supported_chat_action():
    names = [tool["name"] for tool in S3_TOOL_SCHEMAS]
    actions = [tool["action"] for tool in S3_TOOL_SCHEMAS]

    assert len(S3_TOOL_SCHEMAS) == 8
    assert len(names) == len(set(names))
    assert len(actions) == len(set(actions))
    assert set(actions) == EXPECTED_ACTIONS


def test_every_s3_tool_has_safe_provider_neutral_metadata():
    for tool in S3_TOOL_SCHEMAS:
        assert re.fullmatch(r"[a-z0-9_]+", tool["name"])
        assert tool["description"].strip()
        assert tool["service"] == "s3"
        assert tool["risk_level"] in {"low", "medium", "high"}
        assert tool["confirmation_mode"] in {
            "none",
            "standard",
            "explicit_phrase",
        }

        input_schema = tool["input_schema"]
        assert input_schema["type"] == "object"
        assert input_schema["additionalProperties"] is False
        assert set(input_schema["required"]).issubset(
            input_schema["properties"]
        )

        if tool["mutates_aws"]:
            assert tool["requires_confirmation"] is True

    # Toàn bộ cấu trúc phải truyền được qua JSON API/DynamoDB.
    json.dumps(S3_TOOL_SCHEMAS, ensure_ascii=False)


def test_read_only_s3_tools_do_not_require_confirmation():
    tools_by_name = {
        tool["name"]: tool
        for tool in S3_TOOL_SCHEMAS
    }

    for tool_name in READ_ONLY_TOOLS:
        tool = tools_by_name[tool_name]
        assert tool["mutates_aws"] is False
        assert tool["requires_confirmation"] is False
        assert tool["risk_level"] == "low"


def test_delete_tool_requires_every_safety_guard():
    tool = get_s3_tool_schema("s3_delete_bucket_safe")
    schema = tool["input_schema"]

    assert tool["risk_level"] == "high"
    assert tool["requires_confirmation"] is True
    assert tool["confirmation_mode"] == "explicit_phrase"
    assert set(schema["required"]) == {
        "bucket_name",
        "confirmation",
        "expected_deletable_item_count",
    }
    assert schema["properties"]["expected_deletable_item_count"] == {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Giá trị deletable_item_count nhận từ tool "
            "s3_inspect_bucket_for_delete gần nhất."
        ),
    }


def test_presigned_tool_uses_the_same_expiration_limit_as_service():
    tool = get_s3_tool_schema("s3_create_presigned_download")
    expires_schema = tool["input_schema"]["properties"]["expires_in"]

    assert expires_schema["minimum"] == 1
    assert expires_schema["maximum"] == 604800
    assert expires_schema["maximum"] == (
        MAX_PRESIGNED_URL_EXPIRATION_SECONDS
    )


def test_lifecycle_tool_storage_classes_match_s3_service():
    tool = get_s3_tool_schema("s3_put_lifecycle_rule")
    storage_class_schema = (
        tool["input_schema"]["properties"]["storage_class"]
    )

    assert set(S3_LIFECYCLE_STORAGE_CLASSES) == (
        SUPPORTED_LIFECYCLE_STORAGE_CLASSES
    )
    assert set(storage_class_schema["enum"]) == (
        SUPPORTED_LIFECYCLE_STORAGE_CLASSES
    )


def test_build_s3_dispatch_request_maps_tool_without_mutating_arguments():
    arguments = {
        "bucket_name": "sitabot-schema-test",
        "region": "us-east-2",
    }

    request = build_s3_dispatch_request(
        "s3_get_storage_analytics",
        arguments,
    )

    assert "action" not in arguments
    assert request == {
        "service": "s3",
        "params": {
            "bucket_name": "sitabot-schema-test",
            "region": "us-east-2",
            "action": "get_storage_analytics",
        },
        "requires_confirmation": False,
        "confirmation_mode": "none",
        "risk_level": "low",
    }


def test_get_s3_tool_schema_returns_an_independent_copy():
    first_schema = get_s3_tool_schema("s3_create_bucket")
    first_schema["input_schema"]["properties"].clear()

    second_schema = get_s3_tool_schema("s3_create_bucket")

    assert "bucket_name" in second_schema["input_schema"]["properties"]


def test_unknown_s3_tool_is_rejected():
    with pytest.raises(KeyError, match="S3 tool không được hỗ trợ"):
        get_s3_tool_schema("s3_delete_everything")


def test_dispatch_request_requires_dictionary_arguments():
    with pytest.raises(TypeError, match="phải là dictionary"):
        build_s3_dispatch_request("s3_create_bucket", "not-a-dictionary")


def test_dispatch_request_rejects_an_action_in_llm_arguments():
    with pytest.raises(ValueError, match="tham số không được hỗ trợ: action"):
        build_s3_dispatch_request(
            "s3_get_storage_analytics",
            {
                "bucket_name": "sitabot-schema-test",
                "action": "delete_bucket_safe",
            },
        )


def test_dispatch_request_rejects_missing_required_arguments():
    with pytest.raises(ValueError, match="thiếu tham số bắt buộc: object_key"):
        build_s3_dispatch_request(
            "s3_create_presigned_download",
            {"bucket_name": "sitabot-schema-test"},
        )


def test_dispatch_request_enforces_schema_limits():
    with pytest.raises(ValueError, match="expires_in lớn hơn"):
        build_s3_dispatch_request(
            "s3_create_presigned_download",
            {
                "bucket_name": "sitabot-schema-test",
                "object_key": "document.pdf",
                "expires_in": 604801,
            },
        )

    with pytest.raises(ValueError, match="storage_class không thuộc"):
        build_s3_dispatch_request(
            "s3_put_lifecycle_rule",
            {
                "bucket_name": "sitabot-schema-test",
                "transition_days": 30,
                "storage_class": "STANDARD",
            },
        )
