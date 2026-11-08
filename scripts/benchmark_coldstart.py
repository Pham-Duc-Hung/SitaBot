"""
Author: Trịnh Thiên Quang
Đo đạc hiệu năng tiến trình Cold-start thực tế trên gói ZIP cô lập (python -I),
tái sử dụng Warm-cache trong tiến trình (Moto) và tùy chọn đo toàn trình có xác thực JWT.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISPATCHER_PATH = PROJECT_ROOT / "backend" / "functions" / "dispatcher"
for p in (str(PROJECT_ROOT), str(DISPATCHER_PATH)):
    if p not in sys.path:
        sys.path.insert(0, p)

import boto3
from moto import mock_aws

from backend.functions.chatbot import cloud_handler as chatbot_cloud
from backend.functions.connector import index as connector_lambda
from backend.functions.dispatcher import cloud_handler as dispatcher_cloud
from backend.functions.dispatcher.services.lambda_service import clear_service_cache, execute as execute_lambda_service
from backend.functions.formatter import cloud_handler as formatter_cloud
from backend.functions.history import index as history_lambda
from backend.shared import gemini
from backend.shared.runtime import clear_runtime_caches, runtime_cache_stats
from backend.shared.tool_registry import clear_registry_cache, llm_tools, registry_cache_stats, validate_call
from backend.shared.workflow_store import Store, table

FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")

SUBPROCESS_PROBE = """
import importlib, json, os, sys, time
root = sys.argv[1]
name = sys.argv[2]
warm_runs = int(sys.argv[3])
sys.path.insert(0, root)
os.environ["SATIBOT_MODE"] = "cloud"
os.environ["AWS_DEFAULT_REGION"] = "ap-southeast-1"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

t0 = time.perf_counter()
module = importlib.import_module(f"backend.functions.{name}.index")
if name in {"chatbot", "dispatcher"}:
    from backend.shared.tool_registry import llm_tools
    llm_tools()
import_ms = (time.perf_counter() - t0) * 1000

t1 = time.perf_counter()
module.lambda_handler({}, None)
first_handler_ms = (time.perf_counter() - t1) * 1000

warm = []
for _ in range(max(1, warm_runs)):
    tw = time.perf_counter()
    module.lambda_handler({}, None)
    warm.append((time.perf_counter() - tw) * 1000)

print(json.dumps({
    "import_init_ms": round(import_ms, 3),
    "first_invoke_ms": round(first_handler_ms, 3),
    "total_cold_entry_ms": round(import_ms + first_handler_ms, 3),
    "warm_invoke_avg_ms": round(sum(warm) / len(warm), 4),
}))
"""


def measure_package_sizes(artifact_dir: Path | None = None) -> dict:
    """Thống kê kích thước nén, giải nén và mã băm SHA-256 của 5 gói ZIP Lambda."""
    target_dir = artifact_dir or (PROJECT_ROOT / "dist" / "backend")
    report = {}
    for name in FUNCTIONS:
        zip_path = target_dir / f"{name}.zip"
        if not zip_path.exists():
            continue
        raw_bytes = zip_path.read_bytes()
        with zipfile.ZipFile(zip_path) as archive:
            uncompressed = sum(info.file_size for info in archive.infolist())
            file_count = len(archive.infolist())
        report[name] = {
            "zip_kb": round(len(raw_bytes) / 1024, 2),
            "uncompressed_kb": round(uncompressed / 1024, 2),
            "file_count": file_count,
            "sha256_prefix": hashlib.sha256(raw_bytes).hexdigest()[:16],
        }
    return report


def benchmark_subprocess_coldstart(artifact_dir: Path | None = None, warm_runs: int = 5) -> dict:
    """Đo Cold-start tiến trình thật (khởi động Python -I, nạp thư viện từ gói ZIP giải nén + lần gọi đầu)."""
    target_dir = artifact_dir or (PROJECT_ROOT / "dist" / "backend")
    results = {}
    for name in FUNCTIONS:
        zip_path = target_dir / f"{name}.zip"
        if not zip_path.exists():
            continue
        with tempfile.TemporaryDirectory(prefix=f"satibot-cold-{name}-") as extracted:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extracted)
            t_wall = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "-I", "-c", SUBPROCESS_PROBE, extracted, name, str(warm_runs)],
                check=True,
                capture_output=True,
                text=True,
            )
            wall_ms = (time.perf_counter() - t_wall) * 1000
            metrics = json.loads(proc.stdout.strip().splitlines()[-1])
            metrics["process_wall_ms"] = round(wall_ms, 2)
            results[name] = metrics
    return results


def benchmark_local_dispatcher(iterations: int = 10) -> dict:
    """Đo hiệu năng khởi tạo LambdaService và tái sử dụng client cache ở chế độ cục bộ."""
    with mock_aws():
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
        clear_service_cache()
        t0 = time.perf_counter()
        execute_lambda_service(credentials=None, params={"action": "list_lambda_functions", "parameters": {}})
        cold_ms = (time.perf_counter() - t0) * 1000

        warm_samples = []
        for _ in range(max(1, iterations - 1)):
            t_start = time.perf_counter()
            execute_lambda_service(credentials=None, params={"action": "list_lambda_functions", "parameters": {}})
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
    validate_call("update_function_resources", {"function_name": "demo", "memory_mb": 256})
    cold_ms = (time.perf_counter() - t0) * 1000

    warm_samples = []
    for _ in range(max(1, iterations - 1)):
        t_start = time.perf_counter()
        llm_tools()
        validate_call("update_function_resources", {"function_name": "demo", "memory_mb": 256})
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
    gemini_tool_body = json.dumps({
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "list_lambda_functions",
                        "arguments": json.dumps({"max_items": 5}),
                    }
                }]
            }
        }]
    }).encode("utf-8")
    gemini_prose_body = json.dumps(
        {"choices": [{"message": {"content": "Đã kiểm tra danh sách tài nguyên AWS."}}]}
    ).encode("utf-8")
    appsync_body = json.dumps(
        {"data": {"publishActionResult": {"action_id": "act_bench"}}}
    ).encode("utf-8")

    def _urlopen(req, *_args, **_kwargs):
        url = getattr(req, "full_url", str(req))
        if "generativelanguage.googleapis.com" in url:
            req_body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else {}
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
        os.environ.update({
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
        })
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
                    AttributeDefinitions=[{"AttributeName": k["AttributeName"], "AttributeType": "S"} for k in keys],
                    BillingMode="PAY_PER_REQUEST",
                )
            sm = boto3.client("secretsmanager", region_name="ap-southeast-1")
            secret = sm.create_secret(Name="satibot/gemini", SecretString='{"api_key":"bench-gemini-key"}')
            os.environ["GEMINI_SECRET_ARN"] = secret["ARN"]

            iam_client = boto3.client("iam", region_name="ap-southeast-1")
            sfn_role = iam_client.create_role(
                RoleName="bench-sfn-role",
                AssumeRolePolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                }),
            )["Role"]["Arn"]
            sfn = boto3.client("stepfunctions", region_name="ap-southeast-1")
            sm_arn = sfn.create_state_machine(
                name="bench-actions",
                definition=json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Pass", "End": True}}}),
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

            def _auth_event(path: str, method: str = "POST", payload: dict | None = None, query: dict | None = None):
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
                    "improvement_pct": round(((cold_ms - warm_avg) / cold_ms * 100) if cold_ms > 0 else 0.0, 1),
                    "runtime_cache": runtime_cache_stats(),
                }

            def _invoke_chatbot(idx: int):
                res = chatbot_cloud.handle(
                    _auth_event("/chat", "POST", {"message": f"Liệt kê Lambda lần {idx}", "session_id": f"ses_{idx}"}),
                    None,
                )
                assert res["statusCode"] == 202

            _run_bench("chatbot", _invoke_chatbot)

            store = Store()
            tools_map = validate_call("list_ec2_instances", {"state_filter": "all"})

            def _invoke_dispatcher(idx: int):
                action = store.create_action("bench-user", f"ses_d_{idx}", tools_map, {"state_filter": "all"}, "ap-southeast-1")
                resp = dispatcher_cloud.handle({"user_id": "bench-user", "action_id": action["action_id"]}, None)
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
                        "result": {"status": "SUCCESS", "message": "OK", "data": {"count": idx}},
                    },
                    None,
                )
                assert resp["publication_status"] == "PUBLISHED"
                assert resp["formatter_fallback"] is False

            _run_bench("formatter", _invoke_formatter)

            _run_bench(
                "connector",
                lambda _idx: connector_lambda.lambda_handler(
                    _auth_event("/connections/verify", "POST", {"role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole"}),
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


def _derive_history_url(chat_url: str) -> str:
    parsed = urllib.parse.urlsplit(chat_url)
    path = parsed.path
    if path.endswith("/chat"):
        path = path[:-5] + "/history"
    else:
        path = path.rstrip("/") + "/history"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def benchmark_live_endpoint(
    endpoint_url: str,
    *,
    jwt_token: str = "",
    history_url: str = "",
    iterations: int = 5,
    poll_timeout_s: float = 25.0,
) -> dict:
    """Đo độ trễ tiếp nhận (/chat) có xác thực Cognito JWT và độ trễ hoàn tất workflow bất đồng bộ qua /history."""
    token = (jwt_token or os.environ.get("SATIBOT_BENCHMARK_JWT", "")).strip()
    if not token:
        raise ValueError(
            "Benchmark live đối với endpoint có Cognito JWT Authorizer bắt buộc truyền --jwt-token hoặc biến SATIBOT_BENCHMARK_JWT."
        )
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    resolved_history = (history_url or os.environ.get("SATIBOT_BENCHMARK_HISTORY_URL", "")).strip() or _derive_history_url(endpoint_url)
    accept_latencies = []
    workflow_latencies = []

    for i in range(iterations):
        payload = json.dumps({"message": f"Kiểm tra danh sách hàm Lambda lần {i + 1}"}).encode("utf-8")
        req = urllib.request.Request(endpoint_url, data=payload, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status_code = resp.status
                body_data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Live /chat trả HTTP {exc.code}; kiểm tra lại Cognito JWT hoặc quyền API Gateway.") from None

        accept_ms = (time.perf_counter() - t0) * 1000
        accept_latencies.append(accept_ms)

        if status_code == 202 and body_data.get("action_id"):
            action_id = body_data["action_id"]
            deadline = time.perf_counter() + poll_timeout_s
            while time.perf_counter() < deadline:
                h_req = urllib.request.Request(f"{resolved_history}?limit=10", headers={"Authorization": f"Bearer {token}"}, method="GET")
                with urllib.request.urlopen(h_req, timeout=10) as h_resp:
                    h_body = json.loads(h_resp.read().decode("utf-8") or "{}")
                if any(item.get("action_id") == action_id for item in h_body.get("items", [])):
                    workflow_latencies.append((time.perf_counter() - t0) * 1000)
                    break
                time.sleep(0.4)
        time.sleep(0.3)

    first = accept_latencies[0]
    warm = statistics.mean(accept_latencies[1:]) if len(accept_latencies) > 1 else first
    result = {
        "endpoint": endpoint_url,
        "accept_initial_ms": round(first, 2),
        "accept_warm_avg_ms": round(warm, 2),
    }
    if workflow_latencies:
        result["workflow_e2e_initial_ms"] = round(workflow_latencies[0], 2)
        result["workflow_e2e_avg_ms"] = round(statistics.mean(workflow_latencies), 2)
    return result


def run_all_benchmarks(
    iterations: int = 8,
    live_url: str = "",
    jwt_token: str = "",
    history_url: str = "",
) -> dict:
    summary = {
        "packages": measure_package_sizes(),
        "subprocess_coldstart": benchmark_subprocess_coldstart(warm_runs=min(5, iterations)),
        "local_lambda_service": benchmark_local_dispatcher(iterations=iterations),
        "tool_registry": benchmark_tool_registry(iterations=iterations * 2),
        "cloud_lambdas_moto_cache": benchmark_cloud_lambdas(iterations=iterations),
    }
    target_url = (live_url or os.environ.get("SATIBOT_BENCHMARK_URL", "")).strip()
    if target_url:
        summary["live_api"] = benchmark_live_endpoint(
            target_url,
            jwt_token=jwt_token,
            history_url=history_url,
            iterations=min(5, iterations),
        )
    return summary


def _print_report(report: dict) -> None:
    print("=" * 84)
    print("BÁO CÁO ĐO ĐẠC COLD-START TIẾN TRÌNH, WARM-CACHE & KÍCH THƯỚC GÓI 5 LAMBDA (SPRINT 4)")
    print("=" * 84)
    if report["packages"]:
        print("\n1. Kích thước & SHA-256 tất định của 5 gói ZIP (dist/backend):")
        for name, meta in report["packages"].items():
            print(
                f"   - {name:11s}: {meta['zip_kb']:7.2f} KB nén | {meta['uncompressed_kb']:7.2f} KB giải nén "
                f"| {meta['file_count']:3d} files | sha256:{meta['sha256_prefix']}"
            )
    if report["subprocess_coldstart"]:
        print("\n2. Cold-Start tiến trình cô lập thực tế trên gói ZIP (python3.12 -I import + first invoke):")
        for name, m in report["subprocess_coldstart"].items():
            print(
                f"   - {name:11s}: Import+Init {m['import_init_ms']:6.2f} ms + First {m['first_invoke_ms']:5.2f} ms "
                f"= Cold {m['total_cold_entry_ms']:6.2f} ms | Warm TB {m['warm_invoke_avg_ms']:6.4f} ms"
            )
    svc = report["local_lambda_service"]
    print(f"\n3. Dispatcher LambdaService Cache: Cold-cache {svc['cold_ms']:.2f} ms -> Warm TB {svc['warm_avg_ms']:.2f} ms (Giảm {svc['improvement_pct']:.1f}%)")
    reg = report["tool_registry"]
    print(f"4. Tool Registry + Draft7Validator: Cold-cache {reg['cold_ms']:.2f} ms -> Warm TB {reg['warm_avg_ms']:.3f} ms (Giảm {reg['improvement_pct']:.1f}%)")
    print("\n5. Luồng nghiệp vụ 5 Cloud Lambda trong Moto (Cold-cache vs Warm-cache, gồm Chatbot -> StepFunctions):")
    for name, item in report["cloud_lambdas_moto_cache"].items():
        print(
            f"   - {name:11s}: Cold-cache {item['cold_ms']:7.2f} ms -> Warm TB {item['warm_avg_ms']:6.2f} ms "
            f"(Min {item['warm_min_ms']:6.2f} ms | Giảm {item['improvement_pct']:5.1f}%)"
        )
    if "live_api" in report:
        live = report["live_api"]
        print(
            f"\n6. Live Endpoint ({live['endpoint']}): Accept lần đầu {live['accept_initial_ms']:.2f} ms "
            f"-> Warm TB {live['accept_warm_avg_ms']:.2f} ms"
        )
        if "workflow_e2e_avg_ms" in live:
            print(f"   - Hoàn tất toàn trình qua Workflow (/history): TB {live['workflow_e2e_avg_ms']:.2f} ms")
    print("=" * 84)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark Cold-start tiến trình & Warm-cache cho Backend SatiBot.")
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--live-url", default="")
    parser.add_argument("--jwt-token", default="")
    parser.add_argument("--history-url", default="")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    data = run_all_benchmarks(
        iterations=args.iterations,
        live_url=args.live_url,
        jwt_token=args.jwt_token,
        history_url=args.history_url,
    )
    _print_report(data)
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
