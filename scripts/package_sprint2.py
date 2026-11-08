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
SCHEMA_DEPENDENT_FUNCTIONS = frozenset({"chatbot", "dispatcher"})


def _is_clean_dep(path: Path) -> bool:
    if not path.is_file():
        return False
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
        return False
    return True


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
        dependency_files = sorted(p for p in Path(temporary.name).rglob("*") if _is_clean_dep(p))
    for name in FUNCTIONS:
        # Chatbot's current PoC invokes Dispatcher in process; retain that dependency.
        sources = [ROOT / "backend" / "shared", ROOT / "backend" / "functions" / name]
        if name == "chatbot":
            sources.append(ROOT / "backend" / "functions" / "dispatcher")
        if name == "dispatcher":
            sources.append(ROOT / "backend" / "functions" / "chatbot" / "tools")
        needs_schema_deps = name in SCHEMA_DEPENDENT_FUNCTIONS
        target = output / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
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
                    rel = path.relative_to(ROOT).as_posix()
                    if not needs_schema_deps and rel == "backend/shared/tool_registry.py":
                        continue
                    archive.write(path, rel)
            if needs_schema_deps:
                for path in dependency_files:
                    archive.write(path, path.relative_to(temporary.name).as_posix())
    temporary.cleanup()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-dependencies", action="store_true")
    args = parser.parse_args()
    print(package(with_dependencies=args.with_dependencies))
