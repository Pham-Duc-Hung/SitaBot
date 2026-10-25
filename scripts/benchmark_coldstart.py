"""
Author: Trịnh Thiên Quang
Đo đạc và đánh giá hiệu năng cold start và tái sử dụng context (Warm start).
"""

import json
import os
import statistics
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DISPATCHER_PATH = os.path.join(PROJECT_ROOT, "backend", "functions", "dispatcher")
for p in (PROJECT_ROOT, DISPATCHER_PATH):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.lambda_service import execute, clear_service_cache


API_ENDPOINT = "https://1726520kwg.execute-api.us-east-1.amazonaws.com/chat"


def benchmark_local_dispatcher(iterations: int = 10):
    """Đo đạc hiệu năng Dispatcher cục bộ giữa Cold Start và Warm Start."""
    print("=" * 70)
    print("1. ĐO ĐẠC HIỆU NĂNG DISPATCHER BACKEND CỤC BỘ (LOCAL BENCHMARK)")
    print("=" * 70)

    # 1. Cold Start: Xóa cache trước khi chạy lần 1
    clear_service_cache()
    t0 = time.perf_counter()
    res1 = execute(credentials=None, params={"action": "list_lambda_functions", "parameters": {}})
    t_cold = (time.perf_counter() - t0) * 1000

    print(f"[*] Lần 1 (Cold Start - Khởi tạo Boto3 Session & Cache): {t_cold:.2f} ms")

    # 2. Warm Starts: Chạy tiếp các lần sau với Cache tái sử dụng
    warm_latencies = []
    for i in range(2, iterations + 1):
        t_start = time.perf_counter()
        execute(credentials=None, params={"action": "list_lambda_functions", "parameters": {}})
        t_elapsed = (time.perf_counter() - t_start) * 1000
        warm_latencies.append(t_elapsed)
        print(f"[*] Lần {i:2d} (Warm Start - Cache Hit): {t_elapsed:.2f} ms")

    avg_warm = statistics.mean(warm_latencies)
    speedup = ((t_cold - avg_warm) / t_cold) * 100 if t_cold > 0 else 0

    print("-" * 70)
    print(f"-> Thời gian Cold Start:       {t_cold:.2f} ms")
    print(f"-> Thời gian Warm Start TB:    {avg_warm:.2f} ms (Min: {min(warm_latencies):.2f} ms, Max: {max(warm_latencies):.2f} ms)")
    print(f"-> Tốc độ cải thiện đạt:       {speedup:.1f}%")
    print("=" * 70 + "\n")
    return t_cold, avg_warm, warm_latencies


def benchmark_cloud_api(iterations: int = 10):
    """Đo đạc độ trễ toàn trình (End-to-End Latency) qua API Gateway và AWS Lambda thật."""
    print("=" * 70)
    print("2. ĐO ĐẠC ĐỘ TRỄ TOÀN TRÌNH CLOUD (API GATEWAY -> LAMBDA BACKEND)")
    print(f"Endpoint: {API_ENDPOINT}")
    print("=" * 70)

    payload = json.dumps({"message": "Kiểm tra xem tài khoản có những hàm lambda nào đang chạy"}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    latencies = []
    for i in range(1, iterations + 1):
        req = urllib.request.Request(API_ENDPOINT, data=payload, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
                t_elapsed = (time.perf_counter() - t0) * 1000
                latencies.append(t_elapsed)
                tag = "Cold/Initial" if i == 1 else "Warm Call"
                print(f"[*] Request {i:2d} [{tag}]: {t_elapsed:.2f} ms")
        except Exception as e:
            print(f"[!] Request {i:2d} lỗi: {e}")

        # Nghỉ ngắn giữa các request
        time.sleep(0.5)

    if latencies:
        first_call = latencies[0]
        subsequent = latencies[1:] if len(latencies) > 1 else latencies
        avg_sub = statistics.mean(subsequent)
        print("-" * 70)
        print(f"-> Request đầu tiên (Gồm khởi động & TLS Handshake): {first_call:.2f} ms")
        print(f"-> Độ trễ các request tiếp theo TB:                 {avg_sub:.2f} ms")
        print("=" * 70)


if __name__ == "__main__":
    benchmark_local_dispatcher(iterations=10)
    benchmark_cloud_api(iterations=5)

