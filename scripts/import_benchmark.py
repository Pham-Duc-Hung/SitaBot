"""
Author: Trịnh Thiên Quang
Đo thời gian import và lần gọi kiểm tra input trong Python cô lập.
"""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import zipfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
