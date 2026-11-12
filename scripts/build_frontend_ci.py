"""
Author: Trịnh Thiên Quang
Build hoặc sao chép tài sản frontend để phục vụ pipeline CI.
"""

from pathlib import Path
import os
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
frontend = root / "frontend"
target = root / "dist" / "frontend"
if (frontend / "package.json").exists():
    if not (frontend / "package-lock.json").exists():
        raise SystemExit("React frontend requires package-lock.json for npm ci")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise SystemExit("Node.js/npm is required to build the React frontend")
    subprocess.run([npm, "ci"], cwd=frontend, check=True)
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    source = frontend / "dist"
    if not source.is_dir():
        raise SystemExit("Frontend build contract requires frontend/dist")
else:
    # Keep the old static site usable in checkouts without the React package.
    source = frontend
    print("Static frontend detected; copying current assets (React build pending).")
if target.exists():
    # A previous build may contain old hashed JS/CSS files. Only clear this
    # fixed generated directory, never a caller-supplied path.
    shutil.rmtree(target)
shutil.copytree(source, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("node_modules", ".env*", ".git"))
