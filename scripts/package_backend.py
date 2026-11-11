"""
Author: Trịnh Thiên Quang
Tạo năm gói ZIP backend và một gói Post Confirmation riêng, tất định theo nội dung
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
AUTH_FUNCTION = "auth_handler"
SCHEMA_DEPENDENT_FUNCTIONS = frozenset({"chatbot", "dispatcher"})
FIXED_ZIP_DATETIME = (2026, 1, 1, 0, 0, 0)
VOLATILE_DIST_INFO_FILES = frozenset({"INSTALLER", "REQUESTED", "RECORD", "direct_url.json"})
SHARED_MODULES = {
    "chatbot": ("runtime", "http_api", "cloud_errors", "workflow_store", "gemini", "tool_registry"),
    "dispatcher": ("runtime", "cloud_errors", "workflow_store", "tool_registry"),
    "formatter": ("runtime", "http_api", "cloud_errors", "workflow_store", "gemini", "appsync"),
    "connector": ("runtime", "http_api", "cloud_errors", "workflow_store"),
    "history": ("runtime", "http_api"),
    "auth_handler": ("db",),
}


def source_entries(name):
    """Exact cloud-only source manifest, also checked by artifact verification."""
    sources = [ROOT / "backend/shared" / f"{module}.py" for module in SHARED_MODULES[name]]
    function = ROOT / "backend/functions" / name
    sources.append(function / "index.py")
    if name in {"chatbot", "dispatcher", "formatter"}:
        sources.append(function / "cloud_handler.py")
    if name in SCHEMA_DEPENDENT_FUNCTIONS:
        sources.extend(sorted((ROOT / "backend/functions/dispatcher/services").glob("*.py")))
    entries = {path.relative_to(ROOT).as_posix(): (path.read_bytes(), 0o644) for path in sources}
    for path in sources:
        for directory in path.relative_to(ROOT).parents:
            if directory != Path("."):
                init = directory / "__init__.py"
                entries.setdefault(init.as_posix(), ((ROOT / init).read_bytes() if (ROOT / init).exists() else b"", 0o644))
    return entries


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
        for name in (*FUNCTIONS, AUTH_FUNCTION):
            entries = source_entries(name)
            if name in SCHEMA_DEPENDENT_FUNCTIONS:
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
