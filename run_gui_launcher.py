import ctypes
import os
import subprocess
import sys


APP_EXE_NAME = "ChromiumProfileManager.exe"
APP_DIR_NAME = "ChromiumProfileManager"


def _show_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, str(message), "Chromium Profile Manager", 0x10)
    except Exception:
        pass


def _resolve_target_executable() -> str:
    launcher_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidate = os.path.join(launcher_dir, APP_DIR_NAME, APP_EXE_NAME)
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(candidate)


def main() -> int:
    try:
        target = _resolve_target_executable()
    except Exception as exc:
        _show_error(f"Failed to locate GUI executable:\n{exc}")
        return 1

    try:
        args = [target, *sys.argv[1:]]
        if "--exit-existing-instance" in sys.argv[1:]:
            completed = subprocess.run(args, cwd=os.path.dirname(target))
            return int(completed.returncode or 0)
        subprocess.Popen(args, cwd=os.path.dirname(target))
        return 0
    except Exception as exc:
        _show_error(f"Failed to launch GUI executable:\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
