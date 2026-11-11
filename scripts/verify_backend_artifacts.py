"""
Author: Trịnh Thiên Quang
Kiểm tra cloud handler và thư viện trong năm gói ZIP bằng Python 3.12.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_sprint2 import FUNCTIONS, SCHEMA_DEPENDENT_FUNCTIONS as SCHEMA_FUNCTIONS

SMOKE = '''
import importlib, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
os.environ["SATIBOT_MODE"] = "cloud"
name = sys.argv[2]
if name in {"chatbot", "dispatcher"}:
    import jsonschema
    assert Path(jsonschema.__file__).is_relative_to(root)
    import typing_extensions
    assert Path(typing_extensions.__file__).is_relative_to(root)
module = importlib.import_module(f"backend.functions.{name}.index")
result = module.lambda_handler({}, None)
assert isinstance(result, dict)
if name in {"chatbot", "connector", "history"}:
    assert result["statusCode"] == 401
else:
    assert result["error_code"] in {"InvalidPayload", "InternalOnly"}
if name in {"chatbot", "dispatcher"}:
    from backend.shared.tool_registry import registry
    assert "s3_create_bucket" in registry()
'''


def verify():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run artifact verification with Python 3.12, matching Lambda.")
    for name in FUNCTIONS:
        with tempfile.TemporaryDirectory(prefix="satibot-artifact-") as target:
            with zipfile.ZipFile(ROOT / "dist" / "backend" / f"{name}.zip") as archive:
                paths = archive.namelist()
                if len(paths) != len(set(paths)):
                    raise RuntimeError(f"Duplicate files in {name}.zip")
                if name in SCHEMA_FUNCTIONS and "jsonschema/__init__.py" not in paths:
                    raise RuntimeError("Build with --with-dependencies before verification")
                if name in SCHEMA_FUNCTIONS and "typing_extensions.py" not in paths:
                    raise RuntimeError("Python 3.12 needs vendored typing_extensions")
                if name not in SCHEMA_FUNCTIONS and "jsonschema/__init__.py" in paths:
                    raise RuntimeError(f"Unused schema dependency bundled into slim package {name}.zip")
                if any(p.startswith(("tests/", "moto/")) or "__pycache__" in p or ".env" in p for p in paths):
                    raise RuntimeError(f"Development-only files in {name}.zip")
                archive.extractall(target)
            subprocess.run([sys.executable, "-I", "-c", SMOKE, target, name], check=True)
            print(f"{name}: cloud handler and vendored dependencies OK")


if __name__ == "__main__":
    verify()
