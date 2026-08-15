"""Build Jarvis.exe + JarvisService.exe with PyInstaller (all on D drive).

    python build_app.py            build both exes into dist/Jarvis/
    python build_app.py service    build only JarvisService.exe (fast)

Output layout (one folder, shared runtime):
    dist/Jarvis/
        Jarvis.exe          the assistant (windowed, tray)
        JarvisService.exe   watchdog (no window)
        core/ config/ memory/ assets/ ...   data files
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIST = BASE / "dist" / "Jarvis"
ICON = BASE / "assets" / "jarvis.ico"

# data folders shipped next to the exe (read at runtime via BASE_DIR)
DATA_DIRS = ["config", "assets", "core", "memory"]

COMMON = [
    "--noconfirm", "--clean",
    "--distpath", str(BASE / "dist"),
    "--workpath", str(BASE / "build"),
    "--icon", str(ICON),
    # heavy libs Jarvis imports lazily — PyInstaller must still bundle them
    "--collect-all", "openwakeword",
    "--hidden-import", "onnxruntime",
]


def _run(args: list[str]) -> None:
    print(">>", " ".join(args[:6]), "...")
    subprocess.run([sys.executable, "-m", "PyInstaller", *args], check=True,
                   cwd=str(BASE))


def build_service() -> None:
    _run(["--name", "JarvisService", "--noconsole",
          "--distpath", str(DIST), "--workpath", str(BASE / "build"),
          "--noconfirm", "--clean", "--onefile",
          "--icon", str(ICON), "service.py"])


def build_app() -> None:
    _run([*COMMON, "--name", "Jarvis", "--noconsole", "main.py"])
    # one-folder app: move service exe + data dirs in next to Jarvis.exe
    for d in DATA_DIRS:
        src, dst = BASE / d, DIST / d
        if src.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = BASE / ".env"          # OmniRoute endpoint + key live here
    if env.exists():
        shutil.copy2(env, DIST / ".env")
    sc = BASE / "SpeechCore" / "Build" / "SpeechCore.exe"
    if sc.exists():              # C++ audio engine rides along if built
        (DIST / "SpeechCore" / "Build").mkdir(parents=True, exist_ok=True)
        shutil.copy2(sc, DIST / "SpeechCore" / "Build" / "SpeechCore.exe")
    print(f"App built: {DIST}")


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    if only == "service":
        build_service()
    else:
        build_app()
        build_service()
        print("\nDone. Test: dist/Jarvis/Jarvis.exe  |  autostart: JarvisService.exe install")
