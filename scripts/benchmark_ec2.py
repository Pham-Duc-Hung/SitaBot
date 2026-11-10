"""
Benchmark Latency & Performance for Amazon EC2 Service.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)

Mục đích:
  Đo đạc thời gian phản hồi (Latency Benchmark) của module Amazon EC2 Service trên AWS Region Sydney (ap-southeast-2):
  - Cold Start: Lần gọi đầu tiên (khởi tạo TCP/TLS handshake, xác thực AWS Signature V4 và nạp Boto3 Client).
  - Warm Start: Các lần gọi tiếp theo (tái sử dụng HTTPS keep-alive connection pool).
  - Thử nghiệm trên các tác vụ cốt lõi: list_instances, audit_security_group (SecOps), get_metrics (CloudWatch).
  - Tự động xuất bảng số liệu định dạng Terminal, Markdown và LaTeX phục vụ Báo cáo & Slide bảo vệ cuối kỳ.

Hướng dẫn chạy:
  python scripts/benchmark_ec2.py
"""

import json
import os
import statistics
import sys
import time
from pathlib import Path

# Thêm đường dẫn project vào sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER_PATH = PROJECT_ROOT / "backend" / "functions" / "dispatcher"
for p in [str(PROJECT_ROOT), str(DISPATCHER_PATH)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.ec2_service import execute, DEFAULT_REGION


def get_credentials():
    """Lấy credentials từ biến môi trường hoặc người dùng nhập vào."""
    print("=" * 70)
    print("🚀 CHƯƠNG TRÌNH ĐO ĐẠC HIỆU NĂNG & ĐỘ TRỄ (LATENCY BENCHMARK) EC2 SERVICE")
    print("   Tác giả: Nguyễn Đức Huy (Nhóm 10 - Cloud Computing - UET/VNU)")
    print("=" * 70)

    env_key = os.environ.get("AWS_ACCESS_KEY_ID")
    env_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    env_token = os.environ.get("AWS_SESSION_TOKEN")
    env_region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)

    if env_key and env_secret:
        masked_key = env_key[:4] + "..." + env_key[-4:] if len(env_key) > 8 else env_key
        print(f"💡 Phát hiện credentials từ biến môi trường: {masked_key} | Region: {env_region}")
        use_env = input("Bạn có muốn sử dụng credentials môi trường này không? [Y/n]: ").strip().lower()
        if use_env in ["", "y", "yes"]:
            return {
                "AccessKeyId": env_key,
                "SecretAccessKey": env_secret,
                "SessionToken": env_token,
                "Region": env_region,
            }

    print("\n🔐 Vui lòng nhập thông tin xác thực AWS để chạy benchmark:")
    acc_key = input("1. Nhập AWS Access Key ID (vd: AKIA...): ").strip()
    sec_key = input("2. Nhập AWS Secret Access Key: ").strip()
    token = input("3. Nhập AWS Session Token (Enter bỏ qua nếu dùng IAM User): ").strip() or None
    region = input(f"4. Nhập AWS Region [Mặc định: {DEFAULT_REGION}]: ").strip() or DEFAULT_REGION

    return {
        "AccessKeyId": acc_key,
        "SecretAccessKey": sec_key,
        "SessionToken": token,
        "Region": region,
    }


def benchmark_single_action(creds: dict, action_name: str, params: dict, iterations: int = 5):
    """
    Đo đạc độ trễ cho một tác vụ EC2 cụ thể.
    Trả về dict chứa: cold_ms, warm_latencies, warm_avg_ms, min_ms, max_ms, speedup_percent.
    """
    print(f"\n⚡ Đang đo đạc tác vụ: '{action_name}' ({iterations} lần lặp)...")
    latencies = []

    for i in range(1, iterations + 1):
        t0 = time.perf_counter()
        res = execute(creds, params)
        t_elapsed = (time.perf_counter() - t0) * 1000  # mili-giây
        latencies.append(t_elapsed)

        tag = "Cold Start" if i == 1 else f"Warm #{i-1}"
        status = "Thành công" if res.get("success") else f"Thất bại ({res.get('error')})"
        print(f"   [{tag:>10}] Lần {i:2d}: {t_elapsed:7.2f} ms | Kết quả: {status}")

        # Khoảng nghỉ ngắn giữa các request tránh throttling
        if i < iterations:
            time.sleep(0.3)

    cold_ms = latencies[0]
    warm_list = latencies[1:] if len(latencies) > 1 else [latencies[0]]
    warm_avg = statistics.mean(warm_list)
    min_ms = min(warm_list)
    max_ms = max(warm_list)
    speedup = ((cold_ms - warm_avg) / cold_ms * 100) if cold_ms > 0 else 0.0

    return {
        "action": action_name,
        "cold_ms": round(cold_ms, 2),
        "warm_avg_ms": round(warm_avg, 2),
        "min_ms": round(min_ms, 2),
        "max_ms": round(max_ms, 2),
        "speedup_percent": round(speedup, 1),
    }


def print_summary_tables(results: list, region: str):
    """In bảng tổng kết dạng ASCII, Markdown và mã LaTeX phục vụ Báo cáo & Slide."""
    print("\n" + "=" * 80)
    print(f"📊 BẢNG TỔNG HỢP HIỆU NĂNG AMAZON EC2 SERVICE TRÊN AWS SYDNEY ({region})")
    print("=" * 80)
    print(f"{'Tác vụ (Action)':<25} | {'Cold Start (ms)':<15} | {'Warm Avg (ms)':<15} | {'Min (ms)':<10} | {'Speedup':<10}")
    print("-" * 80)
    for r in results:
        print(f"{r['action']:<25} | {r['cold_ms']:<15.2f} | {r['warm_avg_ms']:<15.2f} | {r['min_ms']:<10.2f} | +{r['speedup_percent']}%")
    print("=" * 80)

    # In bảng Markdown
    print("\n📋 DẠNG BẢNG MARKDOWN (Dùng để dán vào Slide hoặc Tài liệu):")
    print("```markdown")
    print(f"| Tác vụ (Action) | Cold Start (ms) | Warm Start TB (ms) | Min (ms) | Tốc độ cải thiện |")
    print(f"| :--- | :---: | :---: | :---: | :---: |")
    for r in results:
        print(f"| `{r['action']}` | {r['cold_ms']} ms | {r['warm_avg_ms']} ms | {r['min_ms']} ms | **+{r['speedup_percent']}%** |")
    print("```")

    # In bảng LaTeX
    print("\n📄 DẠNG BẢNG LATEX (Dùng để dán vào file report3.tex / report4.tex):")
    print("```latex")
    print(r"\begin{table}[htbp]")
    print(r"\centering")
    print(r"\caption{Kết quả đo đạc độ trễ phản hồi module Amazon EC2 trên Region " + region + r"}")
    print(r"\label{tab:ec2_latency_benchmark}")
    print(r"\begin{tabular}{|l|c|c|c|c|}")
    print(r"\hline")
    print(r"\textbf{Tác vụ Boto3 EC2} & \textbf{Cold Start (ms)} & \textbf{Warm TB (ms)} & \textbf{Min (ms)} & \textbf{Cải thiện} \\ \hline")
    for r in results:
        print(f"{r['action']} & {r['cold_ms']} & {r['warm_avg_ms']} & {r['min_ms']} & +{r['speedup_percent']}\\% \\\\ \\hline")
    print(r"\end{tabular}")
    print(r"\end{table}")
    print("```\n")


def main():
    creds = get_credentials()

    # Danh sách các action cần benchmark
    test_cases = [
        ("list_instances", {"action": "list_instances"}),
        ("audit_security_group", {"action": "audit_security_group"}),
    ]

    # Kiểm tra xem có instance nào đang chạy không để đo thêm get_instance & get_metrics
    print("\n🔍 Đang tìm máy ảo trên tài khoản để kiểm tra chi tiết...")
    list_res = execute(creds, {"action": "list_instances"})
    instances = list_res.get("data", [])
    if instances and isinstance(instances, list):
        target_id = instances[0].get("instance_id")
        print(f"💡 Tìm thấy máy ảo: {target_id} ({instances[0].get('name', 'No-Name')})")
        test_cases.append(("get_instance", {"action": "get_instance", "instance_id": target_id}))
        test_cases.append(("get_metrics", {"action": "get_metrics", "instance_id": target_id, "period_hours": 1}))

    # Thực hiện đo đạc
    benchmark_results = []
    for action_name, params in test_cases:
        res = benchmark_single_action(creds, action_name, params, iterations=5)
        benchmark_results.append(res)

    # In bảng tổng kết
    print_summary_tables(benchmark_results, creds.get("Region", DEFAULT_REGION))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Đã dừng chương trình đo đạc.")
