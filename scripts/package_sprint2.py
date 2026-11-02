"""Build five isolated Lambda archives without bundling .env or local artifacts."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("chatbot", "dispatcher", "formatter", "connector", "history")


def package(output=None):
    output = Path(output or ROOT / "dist" / "backend")
    output.mkdir(parents=True, exist_ok=True)
    for name in FUNCTIONS:
        # Chatbot's current PoC invokes Dispatcher in process; retain that dependency.
        sources = [ROOT / "backend" / "shared", ROOT / "backend" / "functions" / name]
        if name == "chatbot":
            sources.append(ROOT / "backend" / "functions" / "dispatcher")
        target = output / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            packages = {"backend", "backend/shared", "backend/functions"}
            packages.update(f"backend/functions/{source.name}" for source in sources if source.name != "shared")
            for directory in sorted(packages):
                archive.writestr(f"{directory}/__init__.py", "")
            for source in sources:
                for path in sorted(source.rglob("*.py")):
                    archive.write(path, path.relative_to(ROOT).as_posix())
    return output


if __name__ == "__main__":
    print(package())
