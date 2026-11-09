"""
Author: Trịnh Thiên Quang
Tạo năm gói ZIP Lambda riêng, tất định theo nội dung (reproducible SHA-256)
và chỉ đóng kèm thư viện runtime cần thiết cho từng hàm.
"""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")
SCHEMA_DEPENDENT_FUNCTIONS = frozenset({"chatbot", "dispatcher"})
FIXED_ZIP_DATETIME = (2026, 1, 1, 0, 0, 0)
VOLATILE_DIST_INFO_FILES = frozenset({"INSTALLER", "REQUESTED", "RECORD", "direct_url.json"})


def _is_clean_dep(path: Path) -> bool:
    if not path.is_file():
        return False
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
        return False
    if any(part.endswith(".dist-info") for part in path.parts) and path.name in VOLATILE_DIST_INFO_FILES:
        return False
    return True


def _write_deterministic_entry(archive: zipfile.ZipFile, arcname: str, payload: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=FIXED_ZIP_DATETIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100000 | mode) << 16
    archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def package(output=None, with_dependencies=False):
    output = Path(output or ROOT / "dist" / "backend")
    output.mkdir(parents=True, exist_ok=True)
    dependency_files = []
    temporary = tempfile.TemporaryDirectory(prefix="satibot-lambda-deps-")
    try:
        if with_dependencies:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-compile",
                    "--platform",
                    "manylinux2014_x86_64",
                    "--python-version",
                    "3.12",
                    "--implementation",
                    "cp",
                    "--abi",
                    "cp312",
                    "--only-binary=:all:",
                    "--target",
                    temporary.name,
                    "-r",
                    str(ROOT / "backend" / "requirements.txt"),
                ],
                check=True,
            )
            dependency_files = sorted(
                (p.relative_to(temporary.name).as_posix(), p)
                for p in Path(temporary.name).rglob("*")
                if _is_clean_dep(p)
            )
        for name in FUNCTIONS:
            # Chatbot's current PoC invokes Dispatcher in process; retain that dependency.
            sources = [ROOT / "backend" / "shared", ROOT / "backend" / "functions" / name]
            if name == "chatbot":
                sources.append(ROOT / "backend" / "functions" / "dispatcher")
            if name == "dispatcher":
                sources.append(ROOT / "backend" / "functions" / "chatbot" / "tools")
            needs_schema_deps = name in SCHEMA_DEPENDENT_FUNCTIONS

            entries: dict[str, tuple[bytes, int]] = {}
            packages = {"backend", "backend/shared", "backend/functions"}
            for source in sources:
                directory = source.relative_to(ROOT)
                while str(directory) != ".":
                    packages.add(directory.as_posix())
                    directory = directory.parent
            for directory in sorted(packages):
                if not (ROOT / directory / "__init__.py").exists():
                    entries[f"{directory}/__init__.py"] = (b"", 0o644)
            for source in sources:
                for path in sorted(source.rglob("*.py")):
                    rel = path.relative_to(ROOT).as_posix()
                    if not needs_schema_deps and rel == "backend/shared/tool_registry.py":
                        continue
                    entries[rel] = (path.read_bytes(), 0o644)
            if needs_schema_deps:
                for rel, path in dependency_files:
                    mode = 0o755 if path.suffix == ".so" else 0o644
                    entries[rel] = (path.read_bytes(), mode)

            target = output / f"{name}.zip"
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for arcname in sorted(entries):
                    data, mode = entries[arcname]
                    _write_deterministic_entry(archive, arcname, data, mode=mode)
    finally:
        temporary.cleanup()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-dependencies", action="store_true")
    args = parser.parse_args()
    print(package(with_dependencies=args.with_dependencies))
