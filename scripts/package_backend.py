"""
Author: Trịnh Thiên Quang
Đóng gói backend và tạo ZIP Chatbot tương thích công cụ triển khai cũ.
"""

from pathlib import Path
import shutil
import zipfile

try:
    from scripts.package_sprint2 import package as package_all
except ModuleNotFoundError:
    from package_sprint2 import package as package_all

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ZIP = PROJECT_ROOT / "satibot_backend.zip"


def package():
    output = package_all(with_dependencies=True)
    shutil.copy2(output / "chatbot.zip", OUTPUT_ZIP)
    with zipfile.ZipFile(OUTPUT_ZIP, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.py", "from backend.functions.chatbot.index import lambda_handler\n")
    print(OUTPUT_ZIP)


if __name__ == "__main__":
    package()
