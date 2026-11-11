"""
Author: Trịnh Thiên Quang
Chạy benchmark import/init, warm cache và API; không thay thế cold start AWS.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.benchmark_cache import (
    benchmark_local_dispatcher,
    benchmark_tool_registry,
    benchmark_cloud_lambdas,
)
from scripts.benchmark_live import benchmark_live_endpoint
from scripts.package_backend import FUNCTIONS

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


def benchmark_subprocess_coldstart(
    artifact_dir: Path | None = None, warm_runs: int = 5
) -> dict:
    """Đo import/init và lần gọi kiểm tra input đầu tiên trong Python -I."""
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
                [
                    sys.executable,
                    "-I",
                    "-c",
                    SUBPROCESS_PROBE,
                    extracted,
                    name,
                    str(warm_runs),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            wall_ms = (time.perf_counter() - t_wall) * 1000
            metrics = json.loads(proc.stdout.strip().splitlines()[-1])
            metrics["process_wall_ms"] = round(wall_ms, 2)
            results[name] = metrics
    return results


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


def run_all_benchmarks(
    iterations: int = 8,
    live_url: str = "",
    jwt_token: str = "",
    history_url: str = "",
) -> dict:
    summary = {
        "packages": measure_package_sizes(),
        "subprocess_coldstart": benchmark_subprocess_coldstart(
            warm_runs=min(5, iterations)
        ),
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
    print("BENCHMARK IMPORT/INIT, WARM CACHE VÀ KÍCH THƯỚC GÓI LAMBDA")
    print("=" * 84)
    if report["packages"]:
        print("\n1. Kích thước & SHA-256 tất định của 5 gói ZIP (dist/backend):")
        for name, meta in report["packages"].items():
            print(
                f"   - {name:11s}: {meta['zip_kb']:7.2f} KB nén | {meta['uncompressed_kb']:7.2f} KB giải nén "
                f"| {meta['file_count']:3d} files | sha256:{meta['sha256_prefix']}"
            )
    if report["subprocess_coldstart"]:
        print(
            "\n2. Import/init và lần gọi kiểm tra input trong Python -I (không phải cold start AWS):"
        )
        for name, m in report["subprocess_coldstart"].items():
            print(
                f"   - {name:11s}: Import+Init {m['import_init_ms']:6.2f} ms + First {m['first_invoke_ms']:5.2f} ms "
                f"= Import+first-call {m['total_cold_entry_ms']:6.2f} ms | Warm TB {m['warm_invoke_avg_ms']:6.4f} ms"
            )
    svc = report["local_lambda_service"]
    print(
        f"\n3. Dispatcher LambdaService Cache: Cold-cache {svc['cold_ms']:.2f} ms -> Warm TB {svc['warm_avg_ms']:.2f} ms (Giảm {svc['improvement_pct']:.1f}%)"
    )
    reg = report["tool_registry"]
    print(
        f"4. Tool Registry + Draft7Validator: Cold-cache {reg['cold_ms']:.2f} ms -> Warm TB {reg['warm_avg_ms']:.3f} ms (Giảm {reg['improvement_pct']:.1f}%)"
    )
    print(
        "\n5. Luồng nghiệp vụ 5 Cloud Lambda trong Moto (Cold-cache vs Warm-cache, gồm Chatbot -> StepFunctions):"
    )
    for name, item in report["cloud_lambdas_moto_cache"].items():
        print(
            f"   - {name:11s}: Cold-cache {item['cold_ms']:7.2f} ms -> Warm TB {item['warm_avg_ms']:6.2f} ms "
            f"(Min {item['warm_min_ms']:6.2f} ms | Giảm {item['improvement_pct']:5.1f}%)"
        )
    if "live_api" in report:
        live = report["live_api"]
        print(
            f"\n6. Live samples: submitted={live['submitted']}, completed={live['completed']}, failed={live['failed']}, timed_out={live['timed_out']}"
        )
        print(
            f"Live Endpoint ({live['endpoint']}): Accept lần đầu {live['accept_initial_ms']} ms "
            f"-> Warm TB {live['accept_warm_avg_ms']} ms"
        )
        if "workflow_e2e_avg_ms" in live:
            print(
                f"   - Hoàn tất toàn trình qua Workflow (/history): TB {live['workflow_e2e_avg_ms']:.2f} ms"
            )
    print("=" * 84)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Đo import/init, warm cache và độ trễ API SatiBot."
    )
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
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if "live_api" in data and (
        data["live_api"]["failed"] or data["live_api"]["timed_out"]
    ):
        raise SystemExit(1)
