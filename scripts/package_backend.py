"""
Author: Trịnh Thiên Quang
Đóng gói mã nguồn Backend Lambda thành file zip sẵn sàng triển khai lên AWS.
"""

import os
import zipfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_ZIP = os.path.join(PROJECT_ROOT, "satibot_backend.zip")

FILES_TO_PACKAGE = [
    ("backend/functions/chatbot/index.py", "index.py"),
    ("backend/functions/chatbot/agent_executor.py", "agent_executor.py"),
    ("backend/functions/dispatcher/index.py", "dispatcher.py"),
    ("backend/functions/dispatcher/services/__init__.py", "services/__init__.py"),
    ("backend/functions/dispatcher/services/lambda_service.py", "services/lambda_service.py"),
    ("backend/functions/dispatcher/services/ec2_service.py", "services/ec2_service.py"),
    ("backend/functions/dispatcher/services/s3_service.py", "services/s3_service.py"),
    ("backend/functions/dispatcher/services/iam_service.py", "services/iam_service.py"),
]


def package():
    print(f"📦 Đang đóng gói {OUTPUT_ZIP}...")
    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_src, arcname in FILES_TO_PACKAGE:
            full_path = os.path.join(PROJECT_ROOT, rel_src)
            if os.path.exists(full_path):
                zf.write(full_path, arcname)
                print(f"  + {arcname} ({os.path.getsize(full_path)} bytes)")
            else:
                print(f"  ! CẢNH BÁO: Không tìm thấy {rel_src}")

    print(f"✅ Đóng gói hoàn tất! Kích thước: {os.path.getsize(OUTPUT_ZIP)} bytes\n")


if __name__ == "__main__":
    package()
