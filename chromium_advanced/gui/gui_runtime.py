import ctypes
from ctypes import wintypes
import datetime
import json
import os
import platform
import psutil
import socket
import subprocess
import sys
import urllib.request
from typing import Dict, List, Optional

from PyQt5.QtCore import QLockFile, QTime
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QLocalSocket

from chromium_advanced.chromium_profile_lib import APP_NAME, get_packaged_app_root, get_project_root, get_state_storage_dir


SYSTEM_TYPE = platform.system()
WINDOWS_RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
PACKAGED_APP_ICON_PATH = os.path.join("resources", "chromium_profile_manager.ico")
SINGLE_INSTANCE_MUTEX_NAME = "Local\\ChromiumProfileManagerGuiSingleton"
SINGLE_INSTANCE_SERVER_NAME = "ChromiumProfileManagerGuiSingletonServer"
MCP_DAEMON_EXE_NAME = "ChromiumMcpDaemon.exe"
MCP_WORKER_EXE_NAME = "ChromiumMcpWorker.exe"


def get_resource_path(*parts) -> str:
    candidates = []
    packaged_root = get_packaged_app_root()
    if packaged_root:
        candidates.append(packaged_root)
    project_root = get_project_root()
    if project_root and project_root not in candidates:
        candidates.append(project_root)
    for base_dir in candidates:
        candidate = os.path.join(base_dir, *parts)
        if os.path.exists(candidate):
            return candidate
    if candidates:
        return os.path.join(candidates[0], *parts)
    return os.path.join(get_project_root(), *parts)


def show_single_instance_message() -> None:
    if getattr(sys, "stderr", None):
        try:
            sys.stderr.write("Chromium Profile Manager is already running.\n")
        except Exception:
            pass


def notify_existing_instance(timeout_ms: int = 1200) -> bool:
    return send_existing_instance_command("show\n", timeout_ms=timeout_ms)


def request_existing_instance_exit(timeout_ms: int = 1200) -> bool:
    return send_existing_instance_command("exit\n", timeout_ms=timeout_ms)


def send_existing_instance_command(command: str, timeout_ms: int = 1200) -> bool:
    try:
        socket_client = QLocalSocket()
        socket_client.connectToServer(SINGLE_INSTANCE_SERVER_NAME)
        if not socket_client.waitForConnected(timeout_ms):
            return False
        socket_client.write(str(command or "").encode("utf-8", errors="replace"))
        socket_client.flush()
        socket_client.waitForBytesWritten(timeout_ms)
        socket_client.disconnectFromServer()
        return True
    except Exception:
        return False


def acquire_single_instance_guard():
    if SYSTEM_TYPE == "Windows":
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError()
        last_error = kernel32.GetLastError()
        if last_error == 183:
            kernel32.CloseHandle(handle)
            return None
        return ("win32-mutex", handle)

    lock_path = os.path.join(get_state_storage_dir(), "gui.lock")
    gui_lock = QLockFile(lock_path)
    gui_lock.setStaleLockTime(0)
    if not gui_lock.tryLock(100):
        return None
    return ("qt-lock", gui_lock)


def release_single_instance_guard(guard) -> None:
    if not guard:
        return
    guard_type, guard_handle = guard
    if guard_type == "win32-mutex":
        try:
            ctypes.windll.kernel32.CloseHandle(guard_handle)
        except Exception:
            pass
        return
    if guard_type == "qt-lock":
        try:
            guard_handle.unlock()
        except Exception:
            pass


def iter_project_mcp_processes() -> List[psutil.Process]:
    results: List[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
        try:
            name = str(proc.info.get("name") or "")
            cmdline_items = [str(item) for item in (proc.info.get("cmdline") or [])]
            cmdline_text = " ".join(cmdline_items)
            exe_path = str(proc.info.get("exe") or "")
            if name in {MCP_DAEMON_EXE_NAME, MCP_WORKER_EXE_NAME}:
                results.append(proc)
                continue
            if name.lower() in {"python.exe", "pythonw.exe"} and (
                "chromium_advanced.mcp_daemon" in cmdline_text
                or "chromium_advanced.mcp_server" in cmdline_text
                or "--run-mcp-daemon" in cmdline_text
                or "--run-mcp-worker" in cmdline_text
            ):
                results.append(proc)
                continue
            if exe_path and os.path.basename(exe_path) in {MCP_DAEMON_EXE_NAME, MCP_WORKER_EXE_NAME}:
                results.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return results


def terminate_project_mcp_processes(exclude_pid: Optional[int] = None, timeout_seconds: float = 3.0) -> List[int]:
    terminated: List[int] = []
    processes = [proc for proc in iter_project_mcp_processes() if proc.pid != exclude_pid]
    if not processes:
        return terminated

    for proc in processes:
        try:
            proc.terminate()
            terminated.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _, alive = psutil.wait_procs(processes, timeout=timeout_seconds)
    if alive:
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        psutil.wait_procs(alive, timeout=timeout_seconds)
    return terminated


def find_project_mcp_processes(exclude_pid: Optional[int] = None) -> List[psutil.Process]:
    return [proc for proc in iter_project_mcp_processes() if proc.pid != exclude_pid]


def get_app_icon() -> QIcon:
    candidates = [
        get_resource_path(PACKAGED_APP_ICON_PATH),
        get_resource_path("resources", "read_gui_icon.png"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return QIcon()


def describe_keepalive_source(
    source: str,
    selected_profiles: Optional[List[str]] = None,
    translate=None,
) -> str:
    selected_profiles = [item for item in (selected_profiles or []) if item]
    tr = translate or (lambda key, fallback="": fallback or key)
    if source.startswith("internal-schedule"):
        return tr("keepalive_source_scheduled")
    if source.startswith("manual:profile:"):
        profile_name = source.split("manual:profile:", 1)[1].strip()
        return (
            tr("keepalive_source_manual_profile_with_name").format(profile_name=profile_name)
            if profile_name
            else tr("keepalive_source_manual_profile")
        )
    if source == "manual:selected":
        if len(selected_profiles) == 1:
            return tr("keepalive_source_manual_profile_with_name").format(profile_name=selected_profiles[0])
        return tr("keepalive_source_manual_selected")
    if source == "manual:all":
        return tr("keepalive_source_manual_all")
    return source or tr("keepalive_source_default")


def parse_schedule_time(value: str) -> QTime:
    parsed = QTime.fromString(value, "HH:mm")
    if parsed.isValid():
        return parsed
    return QTime(9, 0)


def qtime_to_string(value: QTime) -> str:
    return value.toString("HH:mm")


def schedule_time_to_datetime(value: str, base_dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    base_dt = base_dt or datetime.datetime.now()
    qtime = parse_schedule_time(value)
    return base_dt.replace(hour=qtime.hour(), minute=qtime.minute(), second=0, microsecond=0)


def format_datetime_for_ui(value: datetime.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def should_trigger_keepalive_schedule(
    now_dt: datetime.datetime,
    schedule_dt: datetime.datetime,
    last_scheduled_date: str,
    trigger_window_seconds: int = 90,
) -> bool:
    today_text = now_dt.strftime("%Y-%m-%d")
    if str(last_scheduled_date or "").strip() == today_text:
        return False
    delta_seconds = (now_dt - schedule_dt).total_seconds()
    if delta_seconds < 0:
        return False
    return delta_seconds <= max(1, int(trigger_window_seconds))


def can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((str(host or "127.0.0.1"), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_json(
    url: str,
    timeout: float = 1.5,
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
    json_payload: Optional[Dict] = None,
) -> Dict:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items() if k and v is not None})
    body = None
    normalized_method = str(method or "GET").strip().upper() or "GET"
    if json_payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, headers=request_headers, data=body, method=normalized_method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}


def get_frozen_companion_executable(stem: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(sys.executable))
    parent_dir = os.path.dirname(base_dir)
    extension = ".exe" if SYSTEM_TYPE == "Windows" else ""
    packaged_app_root = get_packaged_app_root()
    candidates = [
        os.path.join(base_dir, f"{stem}{extension}"),
        os.path.join(base_dir, stem, f"{stem}{extension}"),
        os.path.join(base_dir, stem, stem, f"{stem}{extension}"),
        os.path.join(parent_dir, f"{stem}{extension}"),
        os.path.join(parent_dir, stem, f"{stem}{extension}"),
        os.path.join(parent_dir, stem, stem, f"{stem}{extension}"),
    ]
    if packaged_app_root:
        candidates.extend(
            [
                os.path.join(packaged_app_root, f"{stem}{extension}"),
                os.path.join(packaged_app_root, "bin", f"{stem}{extension}"),
                os.path.join(packaged_app_root, stem, f"{stem}{extension}"),
            ]
        )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"frozen companion executable not found for {stem}: {', '.join(candidates)}"
    )


def get_startup_command() -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([os.path.abspath(sys.executable), "--start-minimized"])

    python_executable = sys.executable
    if SYSTEM_TYPE == "Windows" and python_executable.lower().endswith("python.exe"):
        pythonw_executable = python_executable[:-10] + "pythonw.exe"
        if os.path.exists(pythonw_executable):
            python_executable = pythonw_executable

    return subprocess.list2cmdline([python_executable, os.path.abspath(__file__), "--start-minimized"])


def _extract_startup_command_executable(command: str) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        parts = subprocess.list2cmdline([])  # no-op to keep subprocess imported consistently
        del parts
    except Exception:
        pass
    try:
        import shlex

        if SYSTEM_TYPE == "Windows":
            # Windows Run command values are typically quoted command lines.
            tokens = shlex.split(text, posix=False)
        else:
            tokens = shlex.split(text)
    except Exception:
        tokens = []
    if not tokens:
        return ""
    return os.path.abspath(str(tokens[0]).strip().strip('"'))


def _normalize_startup_command(command: str) -> str:
    executable = _extract_startup_command_executable(command)
    if not executable:
        return ""
    return subprocess.list2cmdline([executable, "--start-minimized"])


def is_system_auto_start_enabled() -> bool:
    if SYSTEM_TYPE != "Windows":
        return False

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            current_command = _normalize_startup_command(get_startup_command())
            existing_command = _normalize_startup_command(str(value or ""))
            existing_executable = _extract_startup_command_executable(str(value or ""))
            if not existing_command or not existing_executable or not os.path.exists(existing_executable):
                return False
            return existing_command == current_command
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_system_auto_start_enabled(enabled: bool) -> None:
    if SYSTEM_TYPE != "Windows":
        raise NotImplementedError("This feature currently supports Windows startup only.")

    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_REG_PATH) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
