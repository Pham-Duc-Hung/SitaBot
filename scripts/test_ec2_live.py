"""
Live Test Script for EC2 Service on AWS.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET)

Huong dan chay:
  python scripts/test_ec2_live.py
"""

import json
import os
import sys
from pathlib import Path

# Add backend to sys.path
project_root = Path(__file__).resolve().parent.parent
dispatcher_path = project_root / "backend" / "functions" / "dispatcher"
for p in [str(dispatcher_path), str(project_root)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.ec2_service import execute, DEFAULT_REGION


def print_result(title: str, res: dict):
    print("\n" + "=" * 50)
    print(f"👉 KẾT QUẢ: {title}")
    print("=" * 50)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    print("=" * 50 + "\n")


def get_credentials_from_user():
    print("""
===========================================================
🔐 NHẬP THÔNG TIN AWS CREDENTIALS ĐỂ TEST
   (Nếu dùng AWS Academy, lấy ở AWS Details -> AWS CLI)
===========================================================
""")
    # Ưu tiên đọc từ biến môi trường nếu có
    env_key = os.environ.get("AWS_ACCESS_KEY_ID")
    env_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    env_token = os.environ.get("AWS_SESSION_TOKEN")
    env_region = os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)

    if env_key and env_secret:
        print("💡 Phát hiện credentials từ biến môi trường của máy!")
        use_env = input("Bạn có muốn sử dụng credentials môi trường này không? (Y/n): ").strip().lower()
        if use_env in ["", "y", "yes"]:
            return {
                "AccessKeyId": env_key,
                "SecretAccessKey": env_secret,
                "SessionToken": env_token,
                "Region": env_region
            }

    access_key = input("1. Nhập AWS Access Key ID (vd: ASIA... hoặc AKIA...): ").strip()
    secret_key = input("2. Nhập AWS Secret Access Key: ").strip()
    session_token = input("3. Nhập AWS Session Token (Enter bỏ qua nếu dùng IAM User cá nhân): ").strip()
    region = input(f"4. Nhập AWS Region (Mặc định: {DEFAULT_REGION}): ").strip() or DEFAULT_REGION

    return {
        "AccessKeyId": access_key,
        "SecretAccessKey": secret_key,
        "SessionToken": session_token if session_token else None,
        "Region": region
    }


def main():
    creds = get_credentials_from_user()

    while True:
        print("""
-----------------------------------------------------------
CHỌN THAO TÁC EC2 ĐỂ TEST TRỰC TIẾP TRÊN AWS:
  1. Liệt kê tất cả máy chủ EC2 (list_instances)
  2. Liệt kê máy chủ theo trạng thái (list_instances với filter)
  3. Xem chi tiết máy chủ theo ID (get_instance)
  4. Khởi tạo máy ảo chuẩn FinOps (create_instance - t3.micro)
  5. Điều khiển nguồn máy ảo (manage_power: start / stop / reboot)
  6. Giám sát hiệu năng CPU & Đề xuất FinOps (get_metrics)
  7. Xóa máy ảo an toàn có bảo vệ & xác nhận (terminate_instance)
  0. Thoát
-----------------------------------------------------------
""")
        choice = input("Lựa chọn của bạn (0-7): ").strip()

        if choice == "1":
            print("\n⏳ Đang gọi ec2.describe_instances() trên AWS...")
            res = execute(creds, {"action": "list_instances"})
            print_result("Liệt kê máy chủ EC2", res)

        elif choice == "2":
            state = input("Nhập trạng thái cần lọc (running / stopped / pending): ").strip().lower()
            print(f"\n⏳ Đang lọc máy chủ trạng thái '{state}'...")
            res = execute(creds, {"action": "list_instances", "state": state})
            print_result(f"Lọc máy chủ trạng thái {state}", res)

        elif choice == "3":
            inst_id = input("Nhập Instance ID cần xem (vd: i-0123456789abcdef0): ").strip()
            print(f"\n⏳ Đang tra cứu chi tiết máy {inst_id}...")
            res = execute(creds, {"action": "get_instance", "instance_id": inst_id})
            print_result("Chi tiết máy chủ EC2", res)

        elif choice == "4":
            name = input("Nhập tên máy chủ muốn tạo (tag Name): ").strip()
            os_type = input("Chọn OS (ubuntu / amazon-linux) [Mặc định: ubuntu]: ").strip() or "ubuntu"
            inst_type = input("Chọn cấu hình (t3.micro / t2.micro) [Mặc định: t3.micro]: ").strip() or "t3.micro"
            print(f"\n⏳ Đang khởi tạo máy ảo '{name}'...")
            res = execute(creds, {
                "action": "create_instance",
                "instance_name": name,
                "os_type": os_type,
                "instance_type": inst_type
            })
            print_result("Khởi tạo máy chủ EC2", res)

        elif choice == "5":
            inst_id = input("Nhập Instance ID cần điều khiển: ").strip()
            op = input("Chọn thao tác nguồn (start / stop / reboot): ").strip().lower()
            print(f"\n⏳ Đang thực hiện thao tác '{op}' trên máy {inst_id}...")
            res = execute(creds, {
                "action": "manage_power",
                "instance_id": inst_id,
                "operation": op
            })
            print_result(f"Điều khiển nguồn: {op}", res)

        elif choice == "6":
            inst_id = input("Nhập Instance ID cần xem số liệu CPU & FinOps: ").strip()
            hours_str = input("Nhập số giờ muốn thống kê (mặc định 24): ").strip()
            hours = int(hours_str) if hours_str.isdigit() and int(hours_str) > 0 else 24
            print(f"\n⏳ Đang truy vấn CloudWatch CPU metrics cho máy {inst_id} ({hours} giờ)...")
            res = execute(creds, {
                "action": "get_metrics",
                "instance_id": inst_id,
                "period_hours": hours
            })
            print_result("Giám sát CPU & Khuyến nghị FinOps", res)

        elif choice == "7":
            inst_id = input("Nhập Instance ID muốn xóa vĩnh viễn: ").strip()
            confirm_input = input("⚠️ Bạn có chắc chắn muốn xóa không? Nhập 'yes' để xác nhận: ").strip().lower()
            is_confirmed = confirm_input in ["yes", "y", "true"]
            print(f"\n⏳ Đang kiểm tra và thực hiện xóa máy {inst_id} (confirm={is_confirmed})...")
            res = execute(creds, {
                "action": "terminate_instance",
                "instance_id": inst_id,
                "confirm": is_confirmed
            })
            print_result("Xóa máy chủ EC2", res)

        elif choice == "0":
            print("Tạm biệt!")
            break
        else:
            print("Lựa chọn không hợp lệ, vui lòng chọn lại.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nĐã hủy thao tác.")
