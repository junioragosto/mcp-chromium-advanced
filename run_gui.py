import os
import tempfile
import traceback


def _append_bootstrap_log(message: str) -> None:
    try:
        appdata = str(os.environ.get("APPDATA", "") or "").strip()
        if appdata:
            log_dir = os.path.join(appdata, "ChromiumProfileManager", "workstates")
        else:
            log_dir = os.path.join(tempfile.gettempdir(), "ChromiumProfileManager")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "bootstrap_startup.log"), "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{str(message).rstrip()}\n")
    except Exception:
        pass


try:
    from chromium_advanced.chromium_manage_gui import main
except Exception as exc:
    _append_bootstrap_log(f"import chromium_manage_gui failed: {exc!r}")
    _append_bootstrap_log(traceback.format_exc())
    raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _append_bootstrap_log(f"main failed: {exc!r}")
        _append_bootstrap_log(traceback.format_exc())
        raise
