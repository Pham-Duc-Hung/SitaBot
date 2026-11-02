"""Consume Hung's frontend build contract without editing frontend sources."""
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
frontend = root / "frontend"
target = root / "dist" / "frontend"
if (frontend / "package.json").exists():
    if not (frontend / "package-lock.json").exists():
        raise SystemExit("React frontend requires package-lock.json for npm ci")
    subprocess.run(["npm", "ci"], cwd=frontend, check=True)
    subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
    source = frontend / "dist"
    if not source.is_dir():
        raise SystemExit("Frontend build contract requires frontend/dist")
else:
    # Existing frontend is static; React migration is owned by Hung.
    source = frontend
    print("Static frontend detected; copying current assets (React build pending).")
shutil.copytree(source, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("node_modules", ".env*", ".git"))
