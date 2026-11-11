"""
Author: Trịnh Thiên Quang
Kiểm tra năm gói backend và Post Confirmation bằng Python 3.12.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_backend import AUTH_FUNCTION, FUNCTIONS, SCHEMA_DEPENDENT_FUNCTIONS as SCHEMA_FUNCTIONS, source_entries

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
module = importlib.import_module(f"backend.functions.{name}.index")
event = {"triggerSource": "PostConfirmation_ConfirmForgotPassword"} if name == "auth_handler" else {}
result = module.lambda_handler(event, None)
assert isinstance(result, dict)
if name == "auth_handler":
    assert result == event
elif name in {"chatbot", "connector", "history"}:
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
    for name in (*FUNCTIONS, AUTH_FUNCTION):
        with tempfile.TemporaryDirectory(prefix="satibot-artifact-") as target:
            with zipfile.ZipFile(ROOT / "dist" / "backend" / f"{name}.zip") as archive:
                paths = archive.namelist()
                if len(paths) != len(set(paths)):
                    raise RuntimeError(f"Duplicate files in {name}.zip")
                if name in SCHEMA_FUNCTIONS and "jsonschema/__init__.py" not in paths:
                    raise RuntimeError("Build with --with-dependencies before verification")
                if name not in SCHEMA_FUNCTIONS and "jsonschema/__init__.py" in paths:
                    raise RuntimeError(f"Unused schema dependency bundled into slim package {name}.zip")
                if any(p.startswith(("tests/", "moto/")) or "__pycache__" in p or ".env" in p for p in paths):
                    raise RuntimeError(f"Development-only files in {name}.zip")
                expected = source_entries(name)
                if {path for path in paths if path.startswith("backend/")} != set(expected):
                    raise RuntimeError(f"Missing or unused backend source in {name}.zip; rebuild artifacts")
                for path, (payload, _mode) in expected.items():
                    if archive.read(path) != payload:
                        raise RuntimeError(f"Stale source in {name}.zip: {path}")
                archive.extractall(target)
            subprocess.run([sys.executable, "-I", "-c", SMOKE, target, name], check=True)
            print(f"{name}: cloud handler and vendored dependencies OK")


if __name__ == "__main__":
    verify()
