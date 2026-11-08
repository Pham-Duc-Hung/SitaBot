"""
Author: Trịnh Thiên Quang
Đo hiệu năng cache runtime và các luồng backend trong AWS giả lập.
"""

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock
import boto3
from moto import mock_aws
from backend.functions.chatbot import cloud_handler as chatbot_cloud
from backend.functions.connector import index as connector_lambda
from backend.functions.dispatcher import cloud_handler as dispatcher_cloud
from backend.functions.dispatcher.services.lambda_service import (
    clear_service_cache,
    execute as execute_lambda_service,
)
from backend.functions.formatter import cloud_handler as formatter_cloud
from backend.functions.history import index as history_lambda
from backend.shared import gemini
from backend.shared.runtime import clear_runtime_caches, runtime_cache_stats
from backend.shared.tool_registry import (
    clear_registry_cache,
    llm_tools,
    registry_cache_stats,
    validate_call,
)
from backend.shared.workflow_store import Store, table


def benchmark_local_dispatcher(iterations: int = 10) -> dict:
    """Đo hiệu năng khởi tạo LambdaService và tái sử dụng client cache ở chế độ cục bộ."""
    with mock_aws():
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
        clear_service_cache()
        t0 = time.perf_counter()
        execute_lambda_service(
            credentials=None,
            params={"action": "list_lambda_functions", "parameters": {}},
        )
        cold_ms = (time.perf_counter() - t0) * 1000

        warm_samples = []
        for _ in range(max(1, iterations - 1)):
            t_start = time.perf_counter()
            execute_lambda_service(
                credentials=None,
                params={"action": "list_lambda_functions", "parameters": {}},
            )
            warm_samples.append((time.perf_counter() - t_start) * 1000)

    warm_avg = statistics.mean(warm_samples)
    speedup = ((cold_ms - warm_avg) / cold_ms * 100) if cold_ms > 0 else 0.0
    return {
        "cold_ms": round(cold_ms, 3),
        "warm_avg_ms": round(warm_avg, 3),
        "warm_min_ms": round(min(warm_samples), 3),
        "warm_max_ms": round(max(warm_samples), 3),
        "improvement_pct": round(speedup, 1),
    }


def benchmark_tool_registry(iterations: int = 20) -> dict:
    """Đo hiệu năng biên dịch schema công cụ và Draft7Validator giữa Cold-cache và Warm-cache."""
    clear_registry_cache()
    t0 = time.perf_counter()
    llm_tools()
    validate_call(
        "update_function_resources", {"function_name": "demo", "memory_mb": 256}
    )
    cold_ms = (time.perf_counter() - t0) * 1000

    warm_samples = []
    for _ in range(max(1, iterations - 1)):
        t_start = time.perf_counter()
        llm_tools()
        validate_call(
            "update_function_resources", {"function_name": "demo", "memory_mb": 256}
        )
        warm_samples.append((time.perf_counter() - t_start) * 1000)

    warm_avg = statistics.mean(warm_samples)
    speedup = ((cold_ms - warm_avg) / cold_ms * 100) if cold_ms > 0 else 0.0
    return {
        "cold_ms": round(cold_ms, 3),
        "warm_avg_ms": round(warm_avg, 3),
        "improvement_pct": round(speedup, 1),
        "cache_stats": registry_cache_stats(),
    }


def _mock_http_router() -> Mock:
    gemini_tool_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "list_lambda_functions",
                                    "arguments": json.dumps({"max_items": 5}),
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode("utf-8")
    gemini_prose_body = json.dumps(
        {"choices": [{"message": {"content": "Đã kiểm tra danh sách tài nguyên AWS."}}]}
    ).encode("utf-8")
    appsync_body = json.dumps(
        {"data": {"publishActionResult": {"action_id": "act_bench"}}}
    ).encode("utf-8")

    def _urlopen(req, *_args, **_kwargs):
        url = getattr(req, "full_url", str(req))
        if "generativelanguage.googleapis.com" in url:
            req_body = (
                json.loads(req.data.decode("utf-8"))
                if getattr(req, "data", None)
                else {}
            )
            # Nếu request có danh sách tools (từ Chatbot) -> trả về tool_call để đi hết nhánh tạo action + StepFunctions!
            payload = gemini_tool_body if req_body.get("tools") else gemini_prose_body
        else:
            payload = appsync_body
        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock(return_value=False)
        handle.read = Mock(return_value=payload)
        return handle

    return Mock(side_effect=_urlopen)


def benchmark_cloud_lambdas(iterations: int = 8) -> dict:
    """Đo độ trễ Cold-cache vs Warm-cache trong tiến trình (Moto) trên luồng nghiệp vụ đầy đủ của 5 Cloud Lambda."""
    saved_env = dict(os.environ)
    orig_urlopen = urllib.request.urlopen
    try:
        os.environ.update(
            {
                "AWS_ACCESS_KEY_ID": "testing",
                "AWS_SECRET_ACCESS_KEY": "testing",
                "AWS_SESSION_TOKEN": "testing",
                "AWS_DEFAULT_REGION": "ap-southeast-1",
                "AWS_REGION": "ap-southeast-1",
                "AWS_EC2_METADATA_DISABLED": "true",
                "SATIBOT_MODE": "cloud",
                "SESSION_STATE_TABLE": "bench-sessions",
                "PENDING_ACTION_TABLE": "bench-pending",
                "AWS_CONNECTIONS_TABLE": "bench-connections",
                "ACTION_HISTORY_TABLE": "bench-history",
                "APPSYNC_GRAPHQL_URL": "https://bench.appsync-api.ap-southeast-1.amazonaws.com/graphql",
            }
        )
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="ap-southeast-1")
            for name, pk, sk in (
                ("bench-sessions", "user_id", "session_id"),
                ("bench-pending", "action_id", None),
                ("bench-connections", "user_id", None),
                ("bench-history", "user_id", "created_at"),
            ):
                keys = [{"AttributeName": pk, "KeyType": "HASH"}]
                if sk:
                    keys.append({"AttributeName": sk, "KeyType": "RANGE"})
                ddb.create_table(
                    TableName=name,
                    KeySchema=keys,
                    AttributeDefinitions=[
                        {"AttributeName": k["AttributeName"], "AttributeType": "S"}
                        for k in keys
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
            sm = boto3.client("secretsmanager", region_name="ap-southeast-1")
            secret = sm.create_secret(
                Name="satibot/gemini", SecretString='{"api_key":"bench-gemini-key"}'
            )
            os.environ["GEMINI_SECRET_ARN"] = secret["ARN"]

            iam_client = boto3.client("iam", region_name="ap-southeast-1")
            sfn_role = iam_client.create_role(
                RoleName="bench-sfn-role",
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "states.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    }
                ),
            )["Role"]["Arn"]
            sfn = boto3.client("stepfunctions", region_name="ap-southeast-1")
            sm_arn = sfn.create_state_machine(
                name="bench-actions",
                definition=json.dumps(
                    {
                        "StartAt": "Done",
                        "States": {"Done": {"Type": "Pass", "End": True}},
                    }
                ),
                roleArn=sfn_role,
            )["stateMachineArn"]
            os.environ["ACTION_STATE_MACHINE_ARN"] = sm_arn

            urllib.request.urlopen = _mock_http_router()

            clear_runtime_caches()
            clear_registry_cache()
            gemini._SECRET_CACHE.clear()
            table("AWS_CONNECTIONS_TABLE").put_item(
                Item={
                    "user_id": "bench-user",
                    "status": "CONNECTED",
                    "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole",
                    "external_id": "bench-external-id-1234567890123456",
                }
            )

            def _auth_event(
                path: str,
                method: str = "POST",
                payload: dict | None = None,
                query: dict | None = None,
            ):
                return {
                    "rawPath": path,
                    "body": json.dumps(payload or {}),
                    "queryStringParameters": query or {},
                    "requestContext": {
                        "http": {"method": method},
                        "authorizer": {"jwt": {"claims": {"sub": "bench-user"}}},
                    },
                }

            results = {}

            def _run_bench(label: str, invoke_fn):
                clear_runtime_caches()
                clear_registry_cache()
                clear_service_cache()
                gemini._SECRET_CACHE.clear()
                t0 = time.perf_counter()
                invoke_fn(0)
                cold_ms = (time.perf_counter() - t0) * 1000
                warm = []
                for idx in range(1, max(2, iterations)):
                    ts = time.perf_counter()
                    invoke_fn(idx)
                    warm.append((time.perf_counter() - ts) * 1000)
                warm_avg = statistics.mean(warm)
                results[label] = {
                    "cold_ms": round(cold_ms, 3),
                    "warm_avg_ms": round(warm_avg, 3),
                    "warm_min_ms": round(min(warm), 3),
                    "improvement_pct": round(
                        ((cold_ms - warm_avg) / cold_ms * 100) if cold_ms > 0 else 0.0,
                        1,
                    ),
                    "runtime_cache": runtime_cache_stats(),
                }

            def _invoke_chatbot(idx: int):
                res = chatbot_cloud.handle(
                    _auth_event(
                        "/chat",
                        "POST",
                        {
                            "message": f"Liệt kê Lambda lần {idx}",
                            "session_id": f"ses_{idx}",
                        },
                    ),
                    None,
                )
                assert res["statusCode"] == 202

            _run_bench("chatbot", _invoke_chatbot)

            store = Store()
            tools_map = validate_call("list_ec2_instances", {"state_filter": "all"})

            def _invoke_dispatcher(idx: int):
                action = store.create_action(
                    "bench-user",
                    f"ses_d_{idx}",
                    tools_map,
                    {"state_filter": "all"},
                    "ap-southeast-1",
                )
                resp = dispatcher_cloud.handle(
                    {"user_id": "bench-user", "action_id": action["action_id"]}, None
                )
                assert resp["result"]["status"] == "SUCCESS"

            _run_bench("dispatcher", _invoke_dispatcher)

            def _invoke_formatter(idx: int):
                created = f"2026-11-08T08:00:{idx:02d}.000000Z"
                resp = formatter_cloud.handle(
                    {
                        "user_id": "bench-user",
                        "session_id": "ses_fmt",
                        "action_id": f"act_fmt_{idx}",
                        "created_at": created,
                        "service": "lambda",
                        "tool_name": "list_lambda_functions",
                        "result": {
                            "status": "SUCCESS",
                            "message": "OK",
                            "data": {"count": idx},
                        },
                    },
                    None,
                )
                assert resp["publication_status"] == "PUBLISHED"
                assert resp["formatter_fallback"] is False

            _run_bench("formatter", _invoke_formatter)

            _run_bench(
                "connector",
                lambda _idx: connector_lambda.lambda_handler(
                    _auth_event(
                        "/connections/verify",
                        "POST",
                        {
                            "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole"
                        },
                    ),
                    None,
                ),
            )

            _run_bench(
                "history",
                lambda _idx: history_lambda.lambda_handler(
                    _auth_event("/history", "GET", query={"limit": "10"}),
                    None,
                ),
            )
            return results
    finally:
        urllib.request.urlopen = orig_urlopen
        os.environ.clear()
        os.environ.update(saved_env)
        clear_runtime_caches()
        clear_registry_cache()
        clear_service_cache()
        gemini._SECRET_CACHE.clear()
