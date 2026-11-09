"""
Author: Trịnh Thiên Quang
Tạo năm gói ZIP Lambda riêng và đóng kèm thư viện runtime cần thiết.
"""

from pathlib import Path
import zipfile
import argparse
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")


def package(output=None, with_dependencies=False):
    output = Path(output or ROOT / "dist" / "backend")
    output.mkdir(parents=True, exist_ok=True)
    dependency_files = []
    temporary = tempfile.TemporaryDirectory(prefix="satibot-lambda-deps-")
    if with_dependencies:
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-compile", "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
                        "--implementation", "cp", "--abi", "cp312", "--only-binary=:all:",
                        "--target", temporary.name,
                        "-r", str(ROOT / "backend" / "requirements.txt")], check=True)
        dependency_files = [p for p in Path(temporary.name).rglob("*") if p.is_file()]
    for name in FUNCTIONS:
        # Chatbot's current PoC invokes Dispatcher in process; retain that dependency.
        sources = [ROOT / "backend" / "shared", ROOT / "backend" / "functions" / name]
        if name == "chatbot":
            sources.append(ROOT / "backend" / "functions" / "dispatcher")
        target = output / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            packages = {"backend", "backend/shared", "backend/functions"}
            for source in sources:
                directory = source.relative_to(ROOT)
                while str(directory) != ".":
                    packages.add(directory.as_posix())
                    directory = directory.parent
            for directory in sorted(packages):
                if not (ROOT / directory / "__init__.py").exists():
                    archive.writestr(f"{directory}/__init__.py", "")
            for source in sources:
                for path in sorted(source.rglob("*.py")):
                    archive.write(path, path.relative_to(ROOT).as_posix())
            for path in dependency_files:
                archive.write(path, path.relative_to(temporary.name).as_posix())
    temporary.cleanup()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-dependencies", action="store_true")
    args = parser.parse_args()
    print(package(with_dependencies=args.with_dependencies))
