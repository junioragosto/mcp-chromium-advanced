import argparse
import ctypes
from ctypes import wintypes
import datetime
import json
import multiprocessing
import os
import platform
import psutil
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtCore import QLockFile, QProcess, QThread, QTimer, Qt, QTime, QSize, pyqtSignal
from PyQt5.QtGui import QColor, QGuiApplication, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSystemTrayIcon,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from chromium_advanced.chromium_profile_lib import (
    APP_NAME,
    LEGACY_CHATGPT_PROMPT,
    KeepAliveStopController,
    clear_profile_occupancy,
    build_keepalive_plugin_template,
    build_profile_detail_text,
    delete_keepalive_plugin_source,
    format_keepalive_site_status,
    get_keepalive_plugin_records,
    get_keepalive_plugin_root,
    get_keepalive_plugin_source_text,
    normalize_keepalive_site_result_for_display,
    detect_default_language,
    ensure_profile_directory,
    ensure_profile_bookmarks_initialized,
    get_default_config_path,
    get_default_split_user_data_profiles_root,
    get_chromium_processes_for_profile,
    get_hidden_subprocess_kwargs,
    get_keepalive_site_icon_path,
    get_keepalive_site_ids,
    get_keepalive_site_label,
    get_occupancy_events_path,
    get_profile_directory_path,
    get_profile_user_data_root,
    occupancy_entry_is_expired,
    read_recent_jsonl_events,
    is_legacy_default_mirror_root,
    list_profile_occupancy_entries,
    load_json_file,
    find_running_chromium_processes,
    get_project_root,
    get_runtime_launch_cwd,
    get_state_storage_dir,
    launch_profile,
    load_app_config,
    next_profile_name,
    now_text,
    normalize_language_code,
    profile_sort_key,
    resolve_mcp_api_token,
    resolve_mcp_admin_token,
    save_app_config,
    save_keepalive_plugin_source,
    migrate_keepalive_site_id_references,
    sync_profiles_with_user_data,
    terminate_chromium_processes,
    update_profile_launch_time,
    warm_keepalive_site_icon_cache,
    write_profile_occupancy,
    write_json_atomic,
    run_keepalive_job,
)
from chromium_advanced.browser_engines.constants import BROWSER_ENGINE_OPTIONS, DEFAULT_BROWSER_ENGINE
from chromium_advanced.browser_engines.factory import normalize_browser_engine_name
from chromium_advanced.i18n import load_language_options, load_translations


SYSTEM_TYPE = platform.system()
WINDOWS_RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
SCHEDULER_POLL_MS = 15000
PACKAGED_APP_ICON_PATH = os.path.join("resources", "chromium_profile_manager.ico")
LOG_MAX_BLOCKS = 5000
LOG_FLUSH_INTERVAL_MS = 200
CONFIG_MTIME_EPSILON = 0.0001
MCP_PROCESS_STOP_TIMEOUT_MS = 3000
MCP_WATCHDOG_INTERVAL_MS = 5000
MCP_HEALTHCHECK_START_TIMEOUT_MS = 15000
MCP_HEALTHCHECK_POLL_INTERVAL_MS = 250
MCP_STATUS_QUERY_TIMEOUT_SECONDS = 0.6
MCP_STATUS_CACHE_TTL_SECONDS = 1.0
MCP_RECENT_HEALTH_GRACE_SECONDS = 30.0
MCP_WATCHDOG_RESTART_FAILURES = 6
OCCUPANCY_EVENTS_POLL_MS = 2000
MCP_TRANSPORT_OPTIONS = ["streamable-http", "http", "sse"]
MCP_LOG_LEVEL_OPTIONS = ["debug", "info", "warning", "error"]
MCP_WORKER_POLICY_OPTIONS = ["lazy", "sticky", "always_on"]
CONCURRENCY_MODE_OPTIONS = ["per_profile_live", "block"]
LANGUAGE_OPTIONS = load_language_options()
I18N = load_translations()
WINDOW_STATE_SAVE_DELAY_MS = 400
ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX_NAME = "Local\\ChromiumProfileManagerGuiSingleton"
MCP_DAEMON_EXE_NAME = "ChromiumMcpDaemon.exe"
MCP_WORKER_EXE_NAME = "ChromiumMcpWorker.exe"


def _write_gui_profile_occupancy(profile_name: str, scene_type: str, state: str, owner_label: str = "", engine_name: str = "", session_id: str = "") -> None:
    write_profile_occupancy(
        profile_name,
        scene_type=scene_type,
        state=state,
        owner_label=owner_label,
        engine_name=engine_name,
        session_id=session_id,
        details={"source": "gui"},
        event_source="gui",
    )


def _clear_gui_profile_occupancy(profile_name: str) -> None:
    clear_profile_occupancy(
        profile_name,
        event_state="released",
        details={"source": "gui", "cleared": True},
        event_source="gui",
    )


class FocusWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


class FocusWheelTimeEdit(QTimeEdit):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


def get_resource_path(*parts) -> str:
    base_dir = get_project_root()
    return os.path.join(base_dir, *parts)


def show_single_instance_message() -> None:
    if getattr(sys, "stderr", None):
        try:
            sys.stderr.write("Chromium Profile Manager is already running.\n")
        except Exception:
            pass


def acquire_single_instance_guard():
    if SYSTEM_TYPE == "Windows":
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError()
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
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

    gone, alive = psutil.wait_procs(processes, timeout=timeout_seconds)
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


def can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((str(host or "127.0.0.1"), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_json(url: str, timeout: float = 1.5, headers: Optional[Dict[str, str]] = None) -> Dict:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items() if k and v is not None})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}


def get_frozen_companion_executable(stem: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(sys.executable))
    extension = ".exe" if SYSTEM_TYPE == "Windows" else ""
    candidates = [
        os.path.join(base_dir, f"{stem}{extension}"),
        os.path.join(base_dir, stem, f"{stem}{extension}"),
        os.path.join(base_dir, stem, stem, f"{stem}{extension}"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def get_startup_command() -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([os.path.abspath(sys.executable), "--start-minimized"])

    python_executable = sys.executable
    if SYSTEM_TYPE == "Windows" and python_executable.lower().endswith("python.exe"):
        pythonw_executable = python_executable[:-10] + "pythonw.exe"
        if os.path.exists(pythonw_executable):
            python_executable = pythonw_executable

    return subprocess.list2cmdline([python_executable, os.path.abspath(__file__), "--start-minimized"])


def is_system_auto_start_enabled() -> bool:
    if SYSTEM_TYPE != "Windows":
        return False

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_REG_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(str(value).strip())
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


class ProfileEditDialog(QDialog):
    def __init__(self, profile: Dict, config: Optional[Dict] = None, parent=None, translator=None):
        super().__init__(parent)
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.config = config or {}
        self.setWindowTitle(self.translate("profile_dialog_title"))
        self.resize(560, 320)
        layout = QFormLayout(self)

        self.profile_name_edit = QLineEdit(profile.get("profile_name", ""))
        self.profile_name_edit.setReadOnly(True)
        self.account_edit = QLineEdit(profile.get("account", ""))
        self.keepalive_enabled = QCheckBox(self.translate("profile_dialog_keepalive"))
        self.keepalive_enabled.setChecked(profile.get("keepalive_enabled", False))
        self.site_flags = dict(profile.get("keepalive_sites", {}) or {})
        self.notes_edit = QTextEdit(profile.get("notes", ""))
        self.notes_edit.setAcceptRichText(False)
        self.notes_edit.setPlaceholderText(self.translate("profile_dialog_notes_placeholder"))
        self.notes_edit.setMaximumHeight(90)
        self.site_box = QWidget()
        self.site_box_layout = QHBoxLayout(self.site_box)
        self.site_box_layout.setContentsMargins(0, 0, 0, 0)
        self.site_box_layout.setSpacing(8)
        self.site_checkboxes = {}

        for site_name in get_keepalive_site_ids(self.config):
            label = self.translate(f"site_name_{site_name}", get_keepalive_site_label(site_name, self.config))
            checkbox = QCheckBox(label)
            icon_path = get_keepalive_site_icon_path(site_name, self.config, fetch=False)
            if icon_path:
                checkbox.setIcon(QIcon(icon_path))
                checkbox.setIconSize(QSize(16, 16))
            checkbox.setChecked(bool(self.site_flags.get(site_name, False)))
            self.site_checkboxes[site_name] = checkbox
            self.site_box_layout.addWidget(checkbox)
        self.site_box_layout.addStretch()

        layout.addRow("Profile", self.profile_name_edit)
        layout.addRow("Account", self.account_edit)
        layout.addRow("", self.keepalive_enabled)
        layout.addRow(self.translate("profile_dialog_sites"), self.site_box)
        layout.addRow(self.translate("profile_dialog_notes"), self.notes_edit)

        button_row = QHBoxLayout()
        button_row.addStretch()
        save_button = QPushButton(self.translate("common_save"))
        cancel_button = QPushButton(self.translate("common_cancel"))
        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addRow(button_row)

    def get_data(self) -> Dict:
        return {
            "profile_name": self.profile_name_edit.text().strip(),
            "account": self.account_edit.text().strip(),
            "keepalive_enabled": self.keepalive_enabled.isChecked(),
            "keepalive_sites": {site_name: checkbox.isChecked() for site_name, checkbox in self.site_checkboxes.items()},
            "notes": self.notes_edit.toPlainText().strip(),
        }


class KeepalivePluginCreateDialog(QDialog):
    def __init__(self, parent=None, translator=None):
        super().__init__(parent)
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.setWindowTitle(self.translate("plugin_dialog_title"))
        self.resize(420, 180)
        layout = QFormLayout(self)

        self.site_id_edit = QLineEdit()
        self.display_name_edit = QLineEdit()
        self.home_url_edit = QLineEdit()
        self.site_id_edit.setPlaceholderText(self.translate("plugin_dialog_site_id_placeholder"))
        self.display_name_edit.setPlaceholderText(self.translate("plugin_dialog_display_name_placeholder"))
        self.home_url_edit.setPlaceholderText(self.translate("plugin_dialog_home_url_placeholder"))

        layout.addRow(self.translate("plugin_table_site_id"), self.site_id_edit)
        layout.addRow(self.translate("plugin_table_display_name"), self.display_name_edit)
        layout.addRow(self.translate("plugin_detail_home_url"), self.home_url_edit)

        button_row = QHBoxLayout()
        button_row.addStretch()
        save_button = QPushButton(self.translate("common_save"))
        cancel_button = QPushButton(self.translate("common_cancel"))
        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addRow(button_row)

    def get_data(self) -> Dict:
        return {
            "site_id": self.site_id_edit.text().strip(),
            "display_name": self.display_name_edit.text().strip(),
            "home_url": self.home_url_edit.text().strip(),
        }


class KeepAliveWorker(QThread):
    log_signal = pyqtSignal(str, str)
    payload_signal = pyqtSignal(str, object)

    def __init__(
        self,
        config_path: str,
        selected_profiles: Optional[List[str]],
        source: str,
        parent=None,
        translator=None,
    ):
        super().__init__(parent)
        self.config_path = config_path
        self.selected_profiles = selected_profiles or []
        self.source = source
        self.translate = translator or (lambda key, fallback="": fallback or key)
        self.task_prefix = describe_keepalive_source(source, self.selected_profiles, self.translate)
        self.stop_controller = KeepAliveStopController()

    def request_stop(self):
        self.stop_controller.request_stop()

    def run(self):
        try:
            summary = run_keepalive_job(
                config_path=self.config_path,
                selected_profiles=self.selected_profiles,
                logger=lambda message: self.log_signal.emit(self.task_prefix, message),
                source=self.source,
                stop_controller=self.stop_controller,
                progress_callback=lambda kind, payload: self.payload_signal.emit(f"__{kind.upper()}__", payload),
            )
            self.payload_signal.emit("__SUMMARY__", summary)
        except Exception as exc:
            self.log_signal.emit(self.task_prefix, self.translate("keepalive_thread_error").format(error=exc))
            self.payload_signal.emit("__ERROR__", {"message": str(exc)})


class ChromiumManagerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config_path = get_default_config_path()
        self.config = load_app_config(self.config_path)
        self.current_language = normalize_language_code(
            self.config.get("app", {}).get("language", detect_default_language())
        )
        self.config_mtime = self.get_config_mtime()
        self.selected_profile_name = ""
        self.keepalive_worker: Optional[KeepAliveWorker] = None
        self.keepalive_target_profiles: List[str] = []
        self.keepalive_running_profile_name = ""
        self.keepalive_log_prefix = ""
        self.keepalive_stop_requested = False
        self.external_profile_process_signature = ""
        self.external_profile_process_map: Dict[str, List[int]] = {}
        self.profile_occupancy_cache: Dict[str, Dict] = {}
        self.force_exit_requested = False
        self.tray_message_shown = False
        self.scheduler_notice_key = ""
        self.pending_log_lines: List[str] = []
        self.pending_mcp_log_lines: List[str] = []
        self.mcp_process: Optional[QProcess] = None
        self.mcp_owned_process = False
        self.mcp_startup_applied = False
        self.mcp_status_cache: Dict = {}
        self.mcp_restart_pending = False
        self.mcp_stop_requested = False
        self.mcp_startup_in_progress = False
        self.mcp_startup_token = 0
        self.mcp_startup_deadline: Optional[datetime.datetime] = None
        self.mcp_launch_pid = 0
        self.mcp_status_last_query_at = 0.0
        self.mcp_status_last_ok_at = 0.0
        self.mcp_status_consecutive_failures = 0
        self.window_state_dirty = False
        self.occupancy_event_file_offset = 0

        self.ensure_mcp_api_token_persisted()
        self.setWindowTitle(self.tr("window_title"))
        bounds = self.config.get("app", {}).get("window_bounds", {})
        initial_width = max(720, int(bounds.get("width", 860) or 860))
        initial_height = max(560, int(bounds.get("height", 680) or 680))
        self.resize(initial_width, initial_height)
        self.init_ui()
        self.restore_window_bounds()
        self.fit_window_to_screen()
        self.retranslate_ui()
        self.setup_tray_icon()
        self.refresh_app_auto_start_checkbox()
        self.refresh_close_to_tray_checkbox()
        self.refresh_all()
        occupancy_events_path = get_occupancy_events_path()
        if os.path.exists(occupancy_events_path):
            try:
                self.occupancy_event_file_offset = os.path.getsize(occupancy_events_path)
            except OSError:
                self.occupancy_event_file_offset = 0
        self.warm_keepalive_icon_cache_async()
        QTimer.singleShot(0, self.apply_initial_mcp_state)

        self.scheduler_timer = QTimer(self)
        self.scheduler_timer.timeout.connect(self.on_scheduler_timer)
        self.scheduler_timer.start(SCHEDULER_POLL_MS)

        self.log_flush_timer = QTimer(self)
        self.log_flush_timer.timeout.connect(self.flush_log_buffer)
        self.log_flush_timer.start(LOG_FLUSH_INTERVAL_MS)

        self.mcp_watchdog_timer = QTimer(self)
        self.mcp_watchdog_timer.timeout.connect(self.on_mcp_watchdog_timer)
        self.mcp_watchdog_timer.start(MCP_WATCHDOG_INTERVAL_MS)

        self.occupancy_events_timer = QTimer(self)
        self.occupancy_events_timer.timeout.connect(self.on_occupancy_events_timer)
        self.occupancy_events_timer.start(OCCUPANCY_EVENTS_POLL_MS)

        self.window_state_timer = QTimer(self)
        self.window_state_timer.setSingleShot(True)
        self.window_state_timer.timeout.connect(self.persist_window_bounds)

    def ensure_mcp_api_token_persisted(self):
        self.config.setdefault("mcp", {})
        token = str(self.config["mcp"].get("api_token", "")).strip()
        admin_token = str(self.config["mcp"].get("admin_token", "")).strip()
        changed = False
        if not token:
            token = resolve_mcp_api_token(self.config)
            self.config["mcp"]["api_token"] = token
            changed = True
        if not admin_token:
            admin_token = resolve_mcp_admin_token(self.config)
            self.config["mcp"]["admin_token"] = admin_token
            changed = True
        if changed:
            self.config = save_app_config(self.config, self.config_path)

    def _current_screen_available_geometry(self):
        screen = self.screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def fit_window_to_screen(self):
        available = self._current_screen_available_geometry()
        if available is None:
            return
        max_width = max(720, min(available.width() - 40, 1100))
        max_height = max(560, min(available.height() - 40, 820))
        desired_width = min(self.width(), max_width)
        desired_height = min(self.height(), max_height)
        if desired_width <= 0 or desired_height <= 0:
            desired_width = max_width
            desired_height = max_height
        self.resize(desired_width, desired_height)

        x = self.x()
        y = self.y()
        if x < available.left():
            x = available.left()
        if y < available.top():
            y = available.top()
        if x + self.width() > available.right():
            x = max(available.left(), available.right() - self.width())
        if y + self.height() > available.bottom():
            y = max(available.top(), available.bottom() - self.height())
        self.move(x, y)

    def restore_window_bounds(self):
        bounds = self.config.get("app", {}).get("window_bounds", {})
        if not isinstance(bounds, dict):
            return
        width = max(720, int(bounds.get("width", self.width()) or self.width()))
        height = max(560, int(bounds.get("height", self.height()) or self.height()))
        x = int(bounds.get("x", -1) or -1)
        y = int(bounds.get("y", -1) or -1)
        self.resize(width, height)
        if x >= 0 and y >= 0:
            self.move(x, y)

    def schedule_window_bounds_save(self):
        if self.isMinimized() or self.isFullScreen() or self.isMaximized():
            return
        self.window_state_dirty = True
        self.window_state_timer.start(WINDOW_STATE_SAVE_DELAY_MS)

    def persist_window_bounds(self):
        if not self.window_state_dirty:
            return
        if self.isMinimized() or self.isFullScreen() or self.isMaximized():
            return
        self.window_state_dirty = False
        self.config.setdefault("app", {})
        self.config["app"]["window_bounds"] = {
            "x": int(self.x()),
            "y": int(self.y()),
            "width": int(self.width()),
            "height": int(self.height()),
        }
        self.config = save_app_config(self.config, self.config_path)

    def tr(self, key: str, fallback: str = "") -> str:
        lang = self.current_language if getattr(self, "current_language", "") in I18N else "en"
        if key in I18N.get(lang, {}):
            return I18N[lang][key]
        return I18N["en"].get(key, fallback or key)

    def trf(self, key: str, **kwargs) -> str:
        return self.tr(key).format(**kwargs)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        toolbar = QHBoxLayout()

        self.btn_add = QPushButton()
        self.btn_add.clicked.connect(self.add_profile)
        toolbar.addWidget(self.btn_add)

        self.btn_edit = QPushButton()
        self.btn_edit.clicked.connect(self.edit_selected_profile)
        toolbar.addWidget(self.btn_edit)

        self.btn_remove = QPushButton()
        self.btn_remove.clicked.connect(self.remove_selected_profile)
        toolbar.addWidget(self.btn_remove)

        self.btn_remove_with_dir = QPushButton()
        self.btn_remove_with_dir.clicked.connect(self.remove_selected_profile_with_directory)
        toolbar.addWidget(self.btn_remove_with_dir)

        self.btn_sync = QPushButton()
        self.btn_sync.clicked.connect(self.sync_profiles)
        toolbar.addWidget(self.btn_sync)

        self.btn_launch_selected = QPushButton()
        self.btn_launch_selected.clicked.connect(self.launch_selected_profile)
        toolbar.addWidget(self.btn_launch_selected)

        self.btn_reclaim_selected = QPushButton()
        self.btn_reclaim_selected.clicked.connect(self.reclaim_selected_profile)
        toolbar.addWidget(self.btn_reclaim_selected)

        self.btn_keepalive_selected = QPushButton()
        self.btn_keepalive_selected.clicked.connect(self.run_keepalive_for_selected)
        toolbar.addWidget(self.btn_keepalive_selected)

        self.btn_keepalive_all = QPushButton()
        self.btn_keepalive_all.clicked.connect(self.run_keepalive_for_all)
        toolbar.addWidget(self.btn_keepalive_all)

        self.btn_open_config_dir = QPushButton()
        self.btn_open_config_dir.clicked.connect(self.open_config_dir)
        toolbar.addWidget(self.btn_open_config_dir)

        self.app_auto_start_checkbox = QCheckBox()
        self.app_auto_start_checkbox.stateChanged.connect(self.on_app_auto_start_changed)
        toolbar.addWidget(self.app_auto_start_checkbox)

        self.close_to_tray_checkbox = QCheckBox()
        self.close_to_tray_checkbox.stateChanged.connect(self.on_close_to_tray_changed)
        toolbar.addWidget(self.close_to_tray_checkbox)

        self.mcp_service_checkbox = QCheckBox()
        self.mcp_service_checkbox.stateChanged.connect(self.on_mcp_service_checkbox_changed)
        toolbar.addWidget(self.mcp_service_checkbox)

        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(10)
        splitter.setChildrenCollapsible(False)
        main_layout.addWidget(splitter)

        self.table = QTableWidget()
        self.table.setMinimumHeight(0)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Profile", "Account", "Status", "Logged In", "Keepalive", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.cellDoubleClicked.connect(self.on_table_double_clicked)
        self.table.itemSelectionChanged.connect(self.on_table_selection_changed)
        splitter.addWidget(self.table)

        lower_widget = QWidget()
        lower_widget.setMinimumHeight(0)
        lower_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lower_layout = QVBoxLayout(lower_widget)
        lower_layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        lower_layout.addWidget(self.tabs)

        self.keepalive_tab = QScrollArea()
        self.keepalive_tab.setWidgetResizable(True)
        self.keepalive_content = QWidget()
        self.keepalive_content.setMinimumHeight(0)
        self.keepalive_layout = QVBoxLayout(self.keepalive_content)
        self.keepalive_layout.setContentsMargins(12, 12, 12, 12)
        self.keepalive_layout.setSpacing(10)
        self.keepalive_tab.setWidget(self.keepalive_content)
        self.tabs.addTab(self.keepalive_tab, "")

        self.plugin_tab = QWidget()
        self.plugin_layout = QVBoxLayout(self.plugin_tab)
        self.plugin_layout.setContentsMargins(12, 12, 12, 12)
        self.plugin_layout.setSpacing(10)
        self.tabs.addTab(self.plugin_tab, "")

        self.log_tab = QWidget()
        self.log_layout = QVBoxLayout(self.log_tab)
        self.log_layout.setContentsMargins(12, 12, 12, 12)
        self.log_layout.setSpacing(10)
        self.tabs.addTab(self.log_tab, "")

        self.occupancy_tab = QWidget()
        self.occupancy_layout = QVBoxLayout(self.occupancy_tab)
        self.occupancy_layout.setContentsMargins(12, 12, 12, 12)
        self.occupancy_layout.setSpacing(10)
        self.tabs.addTab(self.occupancy_tab, "")

        self.mcp_log_tab = QWidget()
        self.mcp_log_layout = QVBoxLayout(self.mcp_log_tab)
        self.mcp_log_layout.setContentsMargins(12, 12, 12, 12)
        self.mcp_log_layout.setSpacing(10)
        self.tabs.addTab(self.mcp_log_tab, "")

        self.config_tab = QScrollArea()
        self.config_tab.setWidgetResizable(True)
        self.config_content = QWidget()
        self.config_content.setMinimumHeight(0)
        self.config_layout = QVBoxLayout(self.config_content)
        self.config_layout.setContentsMargins(12, 12, 12, 12)
        self.config_layout.setSpacing(10)
        self.config_tab.setWidget(self.config_content)
        self.tabs.addTab(self.config_tab, "")

        self.build_keepalive_tab()
        self.build_plugin_tab()
        self.build_log_tab()
        self.build_occupancy_tab()
        self.build_mcp_log_tab()
        self.build_config_tab()
        splitter.addWidget(lower_widget)
        splitter.setSizes([360, 360])

        self.bottom_status_layout = QHBoxLayout()
        self.bottom_status_label = QLabel()
        self.bottom_status_label.setStyleSheet("color: #666; font-style: italic;")
        self.bottom_status_layout.addWidget(self.bottom_status_label, 1)

        self.bottom_stats_label = QLabel()
        self.bottom_stats_label.setStyleSheet("font-weight: bold; color: #333; margin-right: 10px;")
        self.bottom_status_layout.addWidget(self.bottom_stats_label, 0)
        main_layout.addLayout(self.bottom_status_layout)

    def build_keepalive_tab(self):
        self.selected_group = QGroupBox()
        selected_layout = QVBoxLayout(self.selected_group)
        self.selected_profile_status = QPlainTextEdit()
        self.selected_profile_status.setReadOnly(True)
        self.selected_profile_status.setMaximumBlockCount(200)
        selected_layout.addWidget(self.selected_profile_status)
        self.keepalive_layout.addWidget(self.selected_group)

        self.settings_group = QGroupBox()
        self.settings_layout = QFormLayout(self.settings_group)

        self.site_scope_hint = QLabel()
        self.site_scope_hint.setWordWrap(True)

        self.keepalive_headless = QCheckBox()
        self.keepalive_timeout = FocusWheelSpinBox()
        self.keepalive_timeout.setRange(10, 180)
        self.keepalive_timeout.setSuffix(self.tr("unit_seconds"))
        self.keepalive_between_profiles = FocusWheelSpinBox()
        self.keepalive_between_profiles.setRange(0, 120)
        self.keepalive_between_profiles.setSuffix(self.tr("unit_seconds"))
        self.keepalive_settle = FocusWheelSpinBox()
        self.keepalive_settle.setRange(0, 30)
        self.keepalive_settle.setSuffix(self.tr("unit_seconds"))
        self.keepalive_site_dwell = FocusWheelSpinBox()
        self.keepalive_site_dwell.setRange(0, 60)
        self.keepalive_site_dwell.setSuffix(self.tr("unit_seconds"))
        self.chatgpt_prompt = QLineEdit()
        self.chatgpt_conversation_hint = QLineEdit()
        self.google_query = QLineEdit()
        self.keepalive_plugin_dirs = QLineEdit()
        self.keepalive_plugin_dirs.setPlaceholderText(self.tr("keepalive_plugin_dirs_placeholder"))
        self.keepalive_plugin_dirs_browse = QPushButton()
        self.keepalive_plugin_dirs_browse.clicked.connect(self.pick_keepalive_plugin_dir)
        self.keepalive_plugin_dirs_row = QWidget()
        keepalive_plugin_dirs_layout = QHBoxLayout(self.keepalive_plugin_dirs_row)
        keepalive_plugin_dirs_layout.setContentsMargins(0, 0, 0, 0)
        keepalive_plugin_dirs_layout.addWidget(self.keepalive_plugin_dirs)
        keepalive_plugin_dirs_layout.addWidget(self.keepalive_plugin_dirs_browse)
        self.schedule_time = FocusWheelTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setToolTip(self.tr("schedule_time_tooltip"))
        self.chatgpt_conversation_hint.setPlaceholderText(self.tr("chatgpt_hint_placeholder"))

        self.settings_layout.addRow(self.tr("site_label"), self.site_scope_hint)
        self.settings_layout.addRow(self.tr("headless"), self.keepalive_headless)
        self.settings_layout.addRow(self.tr("page_timeout"), self.keepalive_timeout)
        self.settings_layout.addRow(self.tr("between_profiles"), self.keepalive_between_profiles)
        self.settings_layout.addRow(self.tr("settle"), self.keepalive_settle)
        self.settings_layout.addRow(self.tr("site_dwell"), self.keepalive_site_dwell)
        self.settings_layout.addRow(self.tr("chatgpt_prompt"), self.chatgpt_prompt)
        self.settings_layout.addRow(self.tr("chatgpt_hint"), self.chatgpt_conversation_hint)
        self.settings_layout.addRow(self.tr("google_query"), self.google_query)
        self.settings_layout.addRow(self.tr("keepalive_plugin_dirs"), self.keepalive_plugin_dirs_row)
        self.settings_layout.addRow(self.tr("schedule_time"), self.schedule_time)

        keepalive_button_row = QHBoxLayout()
        self.btn_save_keepalive = QPushButton()
        self.btn_save_keepalive.clicked.connect(self.save_keepalive_settings)
        self.btn_refresh_task = QPushButton()
        self.btn_refresh_task.clicked.connect(self.refresh_scheduler_status)
        keepalive_button_row.addWidget(self.btn_save_keepalive)
        keepalive_button_row.addWidget(self.btn_refresh_task)
        keepalive_button_row.addStretch()

        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.addWidget(self.settings_group)
        wrapper_layout.addLayout(keepalive_button_row)
        self.keepalive_layout.addWidget(wrapper)

        self.summary_group = QGroupBox()
        self.summary_layout = QFormLayout(self.summary_group)
        self.global_last_run = QLabel("-")
        self.global_last_status = QLabel("-")
        self.global_last_message = QLabel("-")
        self.global_task_status = QLabel("-")
        self.global_task_next_run = QLabel("-")
        self.global_task_last_result = QLabel("-")
        self.global_last_message.setWordWrap(True)
        self.global_task_status.setWordWrap(True)
        self.summary_layout.addRow(self.tr("last_run"), self.global_last_run)
        self.summary_layout.addRow(self.tr("last_status"), self.global_last_status)
        self.summary_layout.addRow(self.tr("last_message"), self.global_last_message)
        self.summary_layout.addRow(self.tr("task_status"), self.global_task_status)
        self.summary_layout.addRow(self.tr("next_run"), self.global_task_next_run)
        self.summary_layout.addRow(self.tr("today_result"), self.global_task_last_result)
        self.keepalive_layout.addWidget(self.summary_group)

    def build_plugin_tab(self):
        toolbar = QHBoxLayout()
        self.btn_plugin_reload = QPushButton()
        self.btn_plugin_new = QPushButton()
        self.btn_plugin_save = QPushButton()
        self.btn_plugin_delete = QPushButton()
        self.btn_plugin_open_dir = QPushButton()
        self.btn_plugin_reload.clicked.connect(self.refresh_keepalive_plugin_table)
        self.btn_plugin_new.clicked.connect(self.create_keepalive_plugin)
        self.btn_plugin_save.clicked.connect(self.save_current_keepalive_plugin)
        self.btn_plugin_delete.clicked.connect(self.delete_current_keepalive_plugin)
        self.btn_plugin_open_dir.clicked.connect(self.open_keepalive_plugin_dir)
        toolbar.addWidget(self.btn_plugin_reload)
        toolbar.addWidget(self.btn_plugin_new)
        toolbar.addWidget(self.btn_plugin_save)
        toolbar.addWidget(self.btn_plugin_delete)
        toolbar.addWidget(self.btn_plugin_open_dir)
        toolbar.addStretch()
        self.plugin_layout.addLayout(toolbar)

        self.plugin_splitter = QSplitter(Qt.Horizontal)
        self.plugin_table = QTableWidget(0, 4)
        self.plugin_table.setHorizontalHeaderLabels(
            [
                self.tr("plugin_table_site_id"),
                self.tr("plugin_table_display_name"),
                self.tr("plugin_table_type"),
                self.tr("plugin_table_source"),
            ]
        )
        self.plugin_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.plugin_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.plugin_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.plugin_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.plugin_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.plugin_table.setSelectionMode(QTableWidget.SingleSelection)
        self.plugin_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.plugin_table.itemSelectionChanged.connect(self.on_keepalive_plugin_selection_changed)
        self.plugin_splitter.addWidget(self.plugin_table)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)

        self.plugin_detail_group = QGroupBox()
        self.plugin_detail_layout = QFormLayout(self.plugin_detail_group)
        self.plugin_detail_site_id = QLabel("-")
        self.plugin_detail_display_name = QLabel("-")
        self.plugin_detail_type = QLabel("-")
        self.plugin_detail_source = QLabel("-")
        self.plugin_detail_source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.plugin_detail_home_url = QLabel("-")
        self.plugin_detail_home_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.plugin_detail_icon_url = QLabel("-")
        self.plugin_detail_icon_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.plugin_detail_layout.addRow(self.tr("plugin_table_site_id"), self.plugin_detail_site_id)
        self.plugin_detail_layout.addRow(self.tr("plugin_table_display_name"), self.plugin_detail_display_name)
        self.plugin_detail_layout.addRow(self.tr("plugin_table_type"), self.plugin_detail_type)
        self.plugin_detail_layout.addRow(self.tr("plugin_table_source"), self.plugin_detail_source)
        self.plugin_detail_layout.addRow(self.tr("plugin_detail_home_url"), self.plugin_detail_home_url)
        self.plugin_detail_layout.addRow(self.tr("plugin_detail_icon_url"), self.plugin_detail_icon_url)
        editor_layout.addWidget(self.plugin_detail_group)

        self.plugin_source_hint = QLabel()
        self.plugin_source_hint.setWordWrap(True)
        editor_layout.addWidget(self.plugin_source_hint)

        self.plugin_source_editor = QPlainTextEdit()
        self.plugin_source_editor.setPlaceholderText(self.tr("plugin_editor_placeholder"))
        editor_layout.addWidget(self.plugin_source_editor, 1)

        self.plugin_source_status = QLabel("-")
        self.plugin_source_status.setWordWrap(True)
        editor_layout.addWidget(self.plugin_source_status)

        self.plugin_splitter.addWidget(editor_panel)
        self.plugin_splitter.setSizes([360, 720])
        self.plugin_layout.addWidget(self.plugin_splitter, 1)

        self.keepalive_plugin_records = []
        self.selected_plugin_site_id = ""
        self.btn_plugin_save.setEnabled(False)
        self.btn_plugin_delete.setEnabled(False)
        self.plugin_source_editor.setReadOnly(True)
        self.refresh_keepalive_plugin_table()

    def build_log_tab(self):
        log_toolbar = QHBoxLayout()
        self.btn_clear_logs = QPushButton()
        self.btn_clear_logs.clicked.connect(self.clear_logs)
        log_toolbar.addWidget(self.btn_clear_logs)
        log_toolbar.addStretch()
        self.log_layout.addLayout(log_toolbar)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log_layout.addWidget(self.log_output, 1)

    def build_occupancy_tab(self):
        toolbar = QHBoxLayout()
        self.btn_refresh_occupancy = QPushButton()
        self.btn_refresh_occupancy.clicked.connect(self.refresh_occupancy_tab)
        self.btn_clear_occupancy_view = QPushButton()
        self.btn_clear_occupancy_view.clicked.connect(lambda: self.occupancy_output.clear())
        toolbar.addWidget(self.btn_refresh_occupancy)
        toolbar.addWidget(self.btn_clear_occupancy_view)
        toolbar.addStretch()
        self.occupancy_layout.addLayout(toolbar)

        self.occupancy_summary = QLabel("-")
        self.occupancy_summary.setWordWrap(True)
        self.occupancy_layout.addWidget(self.occupancy_summary)

        self.occupancy_output = QPlainTextEdit()
        self.occupancy_output.setReadOnly(True)
        self.occupancy_output.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.occupancy_layout.addWidget(self.occupancy_output, 1)

    def build_mcp_log_tab(self):
        self.mcp_status_group = QGroupBox()
        self.mcp_status_layout = QFormLayout(self.mcp_status_group)
        self.mcp_status_label = QLabel()
        self.mcp_endpoint_label = QLabel("-")
        self.mcp_endpoint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mcp_worker_endpoint_label = QLabel("-")
        self.mcp_worker_endpoint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mcp_default_engine_label = QLabel("-")
        self.mcp_trace_path_label = QLabel("-")
        self.mcp_trace_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.mcp_status_detail_label = QLabel()
        self.mcp_status_detail_label.setWordWrap(True)
        self.mcp_status_layout.addRow(self.tr("mcp_state"), self.mcp_status_label)
        self.mcp_status_layout.addRow(self.tr("mcp_endpoint"), self.mcp_endpoint_label)
        self.mcp_status_layout.addRow(self.tr("mcp_worker"), self.mcp_worker_endpoint_label)
        self.mcp_status_layout.addRow(self.tr("mcp_default_engine"), self.mcp_default_engine_label)
        self.mcp_status_layout.addRow(self.tr("mcp_trace_path"), self.mcp_trace_path_label)
        self.mcp_status_layout.addRow(self.tr("mcp_detail"), self.mcp_status_detail_label)
        self.mcp_log_layout.addWidget(self.mcp_status_group)

        toolbar = QHBoxLayout()
        self.btn_clear_mcp_logs = QPushButton()
        self.btn_clear_mcp_logs.clicked.connect(self.clear_mcp_logs)
        toolbar.addWidget(self.btn_clear_mcp_logs)
        toolbar.addStretch()
        self.mcp_log_layout.addLayout(toolbar)

        self.mcp_log_output = QPlainTextEdit()
        self.mcp_log_output.setReadOnly(True)
        self.mcp_log_output.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.mcp_log_layout.addWidget(self.mcp_log_output, 1)

    def get_config_mtime(self) -> float:
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return 0.0

    def load_config_from_disk(self) -> Dict:
        self.config = load_app_config(self.config_path)
        self.config_mtime = self.get_config_mtime()
        return self.config

    def reload_config_if_changed(self, force: bool = False) -> bool:
        current_mtime = self.get_config_mtime()
        if not force and abs(current_mtime - self.config_mtime) < CONFIG_MTIME_EPSILON:
            return False
        self.load_config_from_disk()
        return True

    def is_ui_interaction_busy(self) -> bool:
        return QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None

    def exec_modal_dialog(self, dialog: QDialog) -> int:
        scheduler_was_active = self.scheduler_timer.isActive() if hasattr(self, "scheduler_timer") else False
        if scheduler_was_active:
            self.scheduler_timer.stop()
        try:
            return dialog.exec_()
        finally:
            if scheduler_was_active:
                self.scheduler_timer.start(SCHEDULER_POLL_MS)
                QTimer.singleShot(0, self.refresh_scheduler_status)

    def build_config_tab(self):
        self.form_group = QGroupBox()
        self.form_layout = QFormLayout(self.form_group)
        self.path_editors = {}
        self.path_browse_buttons = {}

        fields = [
            ("chromium_dir", self.tr("path_chromium"), "dir"),
            ("chromedriver_path", self.tr("path_driver"), "any"),
            ("user_data_profiles_root", self.tr("path_user_data"), "dir"),
            ("mirror_user_data_root", self.tr("path_mirror_user_data"), "dir"),
            ("bookmarks_template_path", self.tr("path_bookmarks"), "file"),
            ("fingerprint_zip_path", self.tr("path_fingerprint"), "file"),
        ]

        for key, label, mode in fields:
            line_edit = QLineEdit()
            browse_button = QPushButton()
            browse_button.clicked.connect(lambda _, name=key, kind=mode: self.pick_path(name, kind))
            row = QHBoxLayout()
            row.addWidget(line_edit)
            row.addWidget(browse_button)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self.form_layout.addRow(label, wrapper)
            self.path_editors[key] = line_edit
            self.path_browse_buttons[key] = browse_button

        self.language_combo = QComboBox()
        for code, label in LANGUAGE_OPTIONS:
            self.language_combo.addItem(label, code)
        self.language_combo.currentIndexChanged.connect(self.on_language_changed)
        self.form_layout.addRow(self.tr("language"), self.language_combo)

        self.browser_engine_combo = QComboBox()
        for engine_name in BROWSER_ENGINE_OPTIONS:
            self.browser_engine_combo.addItem(engine_name, engine_name)
        self.browser_engine_combo.currentIndexChanged.connect(self.on_browser_engine_changed)
        self.form_layout.addRow(self.tr("browser_engine"), self.browser_engine_combo)

        self.concurrency_mode_combo = QComboBox()
        for mode_name in CONCURRENCY_MODE_OPTIONS:
            self.concurrency_mode_combo.addItem(mode_name, mode_name)
        self.concurrency_mode_combo.currentIndexChanged.connect(self.on_concurrency_mode_changed)
        self.form_layout.addRow(self.tr("concurrency_mode"), self.concurrency_mode_combo)
        self.config_layout.addWidget(self.form_group)

        self.mcp_group = QGroupBox()
        self.mcp_layout = QFormLayout(self.mcp_group)
        self.mcp_transport_combo = QComboBox()
        self.mcp_transport_combo.addItems(MCP_TRANSPORT_OPTIONS)
        self.mcp_host_edit = QLineEdit()
        self.mcp_host_edit.textChanged.connect(lambda: self._refresh_api_token_warning(self.mcp_api_token_edit.text().strip()))
        self.mcp_port_spin = FocusWheelSpinBox()
        self.mcp_port_spin.setRange(1, 65535)
        self.mcp_worker_port_spin = FocusWheelSpinBox()
        self.mcp_worker_port_spin.setRange(1, 65535)
        self.mcp_path_edit = QLineEdit()
        self.mcp_idle_timeout_spin = FocusWheelSpinBox()
        self.mcp_idle_timeout_spin.setRange(10, 86400)
        self.mcp_worker_policy_combo = QComboBox()
        self.mcp_worker_policy_combo.addItems(MCP_WORKER_POLICY_OPTIONS)
        self.mcp_start_minimized_checkbox = QCheckBox()
        self.mcp_log_level_combo = QComboBox()
        self.mcp_log_level_combo.addItems(MCP_LOG_LEVEL_OPTIONS)
        self.mcp_layout.addRow("Transport", self.mcp_transport_combo)
        self.mcp_layout.addRow("Host", self.mcp_host_edit)
        self.mcp_layout.addRow("Daemon Port", self.mcp_port_spin)
        self.mcp_layout.addRow("Worker Port", self.mcp_worker_port_spin)
        self.mcp_layout.addRow("Path", self.mcp_path_edit)
        self.mcp_layout.addRow("Idle Timeout(s)", self.mcp_idle_timeout_spin)
        self.mcp_layout.addRow("Worker Policy", self.mcp_worker_policy_combo)
        self.mcp_layout.addRow(self.tr("mcp_start_minimized"), self.mcp_start_minimized_checkbox)
        self.mcp_layout.addRow(self.tr("mcp_log_level"), self.mcp_log_level_combo)

        self.mcp_api_token_label = QLabel()
        self.mcp_api_token_edit = QLineEdit()
        self.mcp_api_token_edit.setReadOnly(True)
        self.mcp_api_token_edit.setEchoMode(QLineEdit.Normal)
        # selectable-by-mouse is the default for read-only QLineEdit
        self.mcp_api_token_edit.setToolTip(self.tr("mcp_api_token_tooltip"))
        self.btn_regenerate_api_token = QPushButton()
        self.btn_regenerate_api_token.clicked.connect(self.regenerate_api_token)
        self.mcp_admin_token_label = QLabel("Admin Token")
        self.mcp_admin_token_edit = QLineEdit()
        self.mcp_admin_token_edit.setReadOnly(True)
        self.mcp_admin_token_edit.setEchoMode(QLineEdit.Normal)
        self.mcp_auth_warning_label = QLabel()
        self.mcp_auth_warning_label.setStyleSheet("color: #e67e22; font-weight: bold;")
        self.mcp_auth_warning_label.setWordWrap(True)
        self.mcp_auth_warning_label.hide()
        token_row = QHBoxLayout()
        token_row.addWidget(self.mcp_api_token_edit, 1)
        token_row.addWidget(self.btn_regenerate_api_token)
        self.mcp_layout.addRow(self.mcp_api_token_label, token_row)
        self.mcp_layout.addRow(self.mcp_admin_token_label, self.mcp_admin_token_edit)
        self.mcp_layout.addRow("", self.mcp_auth_warning_label)
        self.config_layout.addWidget(self.mcp_group)

        config_button_row = QHBoxLayout()
        self.btn_save_paths = QPushButton()
        self.btn_save_paths.clicked.connect(self.save_path_settings)
        self.btn_save_mcp = QPushButton()
        self.btn_save_mcp.clicked.connect(self.save_mcp_settings)
        self.btn_restart_mcp = QPushButton()
        self.btn_restart_mcp.clicked.connect(self.restart_mcp_service)
        self.btn_reload = QPushButton()
        self.btn_reload.clicked.connect(self.reload_config_from_disk)
        self.config_path_label = QLabel(self.config_path)
        config_button_row.addWidget(self.btn_save_paths)
        config_button_row.addWidget(self.btn_save_mcp)
        config_button_row.addWidget(self.btn_restart_mcp)
        config_button_row.addWidget(self.btn_reload)
        config_button_row.addStretch()
        config_button_row.addWidget(self.config_path_label)
        self.config_layout.addLayout(config_button_row)
        self.config_layout.addStretch()

    def append_log(self, message: str, prefix: str = "GUI"):
        lines = str(message).splitlines() or [""]
        for line in lines:
            self.pending_log_lines.append(f"[{now_text()}] [{prefix}] {line}")
        last_line = str(lines[-1]).strip() if lines else ""
        if last_line:
            self.set_bottom_status(last_line, prefix=prefix)

    def append_mcp_log(self, message: str, prefix: str = "MCP"):
        lines = str(message).splitlines() or [""]
        for line in lines:
            self.pending_mcp_log_lines.append(f"[{now_text()}] [{prefix}] {line}")
        last_line = str(lines[-1]).strip() if lines else ""
        if last_line:
            self.set_bottom_status(last_line, prefix=prefix)

    def set_bottom_status(self, message: str, prefix: str = ""):
        text = str(message or "").strip() or self.tr("bottom_ready")
        if prefix:
            text = f"[{prefix}] {text}"
        self.bottom_status_label.setText(text)

    def refresh_bottom_stats(self):
        profile_count = len(self.config.get("profiles", []))
        mcp_state_text = self.tr("bottom_mcp_stopped")
        active_session_count = len([item for item in self.profile_occupancy_cache.values() if isinstance(item, dict) and item.get("state") not in {"released", "idle", "start_failed"}])

        if self.mcp_startup_in_progress:
            mcp_state_text = "starting"
        elif self.mcp_status_cache:
            mcp_state_text = "running"

        mcp_status_text = self.mcp_status_label.text() if hasattr(self, "mcp_status_label") else mcp_state_text
        self.bottom_stats_label.setText(
            f"Profiles: {profile_count} | MCP: {mcp_status_text or mcp_state_text} | Sessions: {active_session_count}"
        )

    def flush_log_buffer(self):
        if self.pending_log_lines:
            scrollbar = self.log_output.verticalScrollBar()
            scroll_at_bottom = scrollbar.value() >= (scrollbar.maximum() - 200)
            self.log_output.appendPlainText("\n".join(self.pending_log_lines))
            self.pending_log_lines = []
            if scroll_at_bottom:
                scrollbar.setValue(scrollbar.maximum())

        if self.pending_mcp_log_lines:
            scrollbar = self.mcp_log_output.verticalScrollBar()
            scroll_at_bottom = scrollbar.value() >= (scrollbar.maximum() - 200)
            self.mcp_log_output.appendPlainText("\n".join(self.pending_mcp_log_lines))
            self.pending_mcp_log_lines = []
            if scroll_at_bottom:
                scrollbar.setValue(scrollbar.maximum())

    def clear_logs(self):
        self.pending_log_lines = []
        self.log_output.clear()

    def clear_mcp_logs(self):
        self.pending_mcp_log_lines = []
        self.mcp_log_output.clear()

    def on_keepalive_worker_log(self, prefix: str, message: str):
        self.append_log(message, prefix=prefix)

    def on_keepalive_worker_message(self, kind: str, payload: Dict):
        if kind == "__PROFILE_START__":
            self.keepalive_running_profile_name = str((payload or {}).get("profile_name", "")).strip()
            acquired = bool((payload or {}).get("lock_acquired", True))
            if self.keepalive_running_profile_name and acquired:
                _write_gui_profile_occupancy(
                    self.keepalive_running_profile_name,
                    scene_type="keepalive",
                    state="active",
                    owner_label=self.keepalive_log_prefix or "Keepalive",
                    engine_name=normalize_browser_engine_name(
                        self.config.get("app", {}).get("browser_engine", DEFAULT_BROWSER_ENGINE)
                    ),
                )
                self.profile_occupancy_cache = self.load_profile_occupancy_cache()
            self.refresh_table()
            return

        if kind == "__SUMMARY__":
            self.append_log(
                self.trf("log_keepalive_finished", status=payload.get("status"), message=payload.get("message")),
                prefix=self.keepalive_log_prefix or self.tr("keepalive_source_default"),
            )
            if str((payload or {}).get("source", "")).startswith("internal-schedule") and str(
                (payload or {}).get("status", "")
            ) not in {"skipped", "stopped"}:
                self.config = load_app_config(self.config_path)
                self.config["keepalive"]["last_scheduled_run_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
                self.config = save_app_config(self.config, self.config_path)
            self.keepalive_worker = None
            if self.keepalive_running_profile_name:
                _clear_gui_profile_occupancy(self.keepalive_running_profile_name)
            self.keepalive_target_profiles = []
            self.keepalive_running_profile_name = ""
            self.keepalive_log_prefix = ""
            self.keepalive_stop_requested = False
            self.set_keepalive_buttons_enabled(True)
            self.profile_occupancy_cache = self.load_profile_occupancy_cache()
            self.reload_config_from_disk()
            return

        if kind == "__ERROR__":
            self.append_log(
                self.trf("log_keepalive_failed", message=payload.get("message", "")),
                prefix=self.keepalive_log_prefix or self.tr("keepalive_source_default"),
            )
            self.keepalive_worker = None
            if self.keepalive_running_profile_name:
                _clear_gui_profile_occupancy(self.keepalive_running_profile_name)
            self.keepalive_target_profiles = []
            self.keepalive_running_profile_name = ""
            self.keepalive_log_prefix = ""
            self.keepalive_stop_requested = False
            self.set_keepalive_buttons_enabled(True)
            self.profile_occupancy_cache = self.load_profile_occupancy_cache()
            self.reload_config_from_disk()

    def set_keepalive_buttons_enabled(self, enabled: bool):
        self.btn_add.setEnabled(enabled)
        self.btn_edit.setEnabled(enabled)
        self.btn_remove.setEnabled(enabled)
        self.btn_remove_with_dir.setEnabled(enabled)
        self.btn_reclaim_selected.setEnabled(enabled)
        self.btn_keepalive_selected.setEnabled(enabled)
        self.btn_keepalive_all.setEnabled(enabled)
        self.btn_save_keepalive.setEnabled(enabled)
        self.btn_refresh_task.setEnabled(enabled)
        self.btn_plugin_reload.setEnabled(enabled)
        self.btn_plugin_new.setEnabled(enabled)
        self.btn_plugin_open_dir.setEnabled(enabled)
        record = self.get_selected_keepalive_plugin_record() if hasattr(self, "plugin_table") else None
        editable_plugin_selected = bool(record) and not bool((record or {}).get("builtin"))
        self.btn_plugin_save.setEnabled(enabled and editable_plugin_selected)
        self.btn_plugin_delete.setEnabled(enabled and editable_plugin_selected)
        if hasattr(self, "plugin_source_editor"):
            self.plugin_source_editor.setReadOnly((not enabled) or (not editable_plugin_selected))
        self.refresh_table()

    def build_external_profile_process_signature(self) -> str:
        process_map = self.build_external_profile_process_map()
        return self.serialize_external_profile_process_map(process_map)

    def build_external_profile_process_map(self) -> Dict[str, List[int]]:
        entries: Dict[str, List[int]] = {}
        for profile in self.config.get("profiles", []):
            profile_name = str(profile.get("profile_name", "") or "").strip()
            if not profile_name:
                continue
            pids = sorted(
                int(item.get("pid") or 0)
                for item in get_chromium_processes_for_profile(self.config, profile_name)
                if int(item.get("pid") or 0) > 0
            )
            entries[profile_name] = pids
        return entries

    def serialize_external_profile_process_map(self, process_map: Dict[str, List[int]]) -> str:
        return "|".join(
            f"{profile_name}:{','.join(str(pid) for pid in process_map.get(profile_name, []))}"
            for profile_name in sorted(process_map.keys())
        )

    def refresh_external_profile_process_state(self) -> None:
        process_map = self.build_external_profile_process_map()
        signature = self.serialize_external_profile_process_map(process_map)
        if signature == self.external_profile_process_signature:
            self.reconcile_manual_occupancy(process_map)
            return
        previous_map = dict(self.external_profile_process_map)
        self.external_profile_process_signature = signature
        self.external_profile_process_map = process_map
        if previous_map:
            self.log_external_profile_process_transitions(previous_map, process_map)
        self.reconcile_manual_occupancy(process_map)
        self.refresh_table()
        self.update_selected_profile_status()

    def log_external_profile_process_transitions(
        self,
        previous_map: Dict[str, List[int]],
        current_map: Dict[str, List[int]],
    ) -> None:
        for profile_name in sorted(set(previous_map.keys()) | set(current_map.keys())):
            before = sorted(int(pid) for pid in previous_map.get(profile_name, []) if int(pid) > 0)
            after = sorted(int(pid) for pid in current_map.get(profile_name, []) if int(pid) > 0)
            if before == after:
                continue
            if not before and after:
                self.append_log(
                    self.tr("log_profile_runtime_detected_started", "{profile_name} external Chromium detected: {pid_text}").format(
                        profile_name=profile_name,
                        pid_text=", ".join(str(pid) for pid in after),
                    )
                )
                continue
            if before and not after:
                self.append_log(
                    self.tr("log_profile_runtime_detected_stopped", "{profile_name} external Chromium fully exited.").format(
                        profile_name=profile_name,
                    )
                )
                continue
            self.append_log(
                self.tr("log_profile_runtime_detected_changed", "{profile_name} external Chromium changed: {pid_text}").format(
                    profile_name=profile_name,
                    pid_text=", ".join(str(pid) for pid in after),
                )
            )

    def build_profile_runtime_state_text(self, profile_name: str) -> str:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            return self.tr("runtime_state_unknown", "Unknown")
        occupancy = self.profile_occupancy_cache.get(profile_name, {})
        if occupancy:
            scene_type = str(occupancy.get("scene_type", "") or "").strip()
            owner_label = str(occupancy.get("owner_label", "") or "").strip()
            state = str(occupancy.get("state", "") or "").strip() or "active"
            if scene_type and state not in {"released", "start_failed"}:
                suffix = f" · {owner_label}" if owner_label else ""
                return f"{scene_type}:{state}{suffix}"
        if self.is_profile_keepalive_running(profile_name):
            return self.tr("runtime_state_keepalive", "Keepalive running")
        pids = self.external_profile_process_map.get(profile_name, [])
        if pids:
            return self.tr("runtime_state_external_running", "External Chromium running: {pid_text}").format(
                pid_text=", ".join(str(pid) for pid in pids)
            )
        return self.tr("runtime_state_idle", "Idle")

    def resolve_keepalive_target_profiles(self, selected_profiles: List[str]) -> List[str]:
        selected = [str(item or "").strip() for item in (selected_profiles or []) if str(item or "").strip()]
        if selected:
            return selected
        return [
            str(item.get("profile_name", "")).strip()
            for item in self.config.get("profiles", [])
            if str(item.get("profile_name", "")).strip() and bool(item.get("keepalive_enabled", False))
        ]

    def refresh_all(self):
        self.load_config_from_disk()
        self.current_language = normalize_language_code(
            self.config.get("app", {}).get("language", detect_default_language())
        )
        self.external_profile_process_map = self.build_external_profile_process_map()
        self.external_profile_process_signature = self.serialize_external_profile_process_map(
            self.external_profile_process_map
        )
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.load_app_settings_to_ui()
        self.retranslate_ui()
        self.refresh_keepalive_plugin_table()
        self.refresh_table()
        self.refresh_occupancy_tab()
        self.load_keepalive_settings_to_ui()
        self.load_path_settings_to_ui()
        self.load_mcp_settings_to_ui()
        self.refresh_app_auto_start_checkbox()
        self.refresh_close_to_tray_checkbox()
        self.refresh_scheduler_status()
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.update_selected_profile_status()
        self.refresh_bottom_stats()

    def reload_config_from_disk(self):
        self.refresh_all()
        self.append_log(self.tr("log_config_reloaded"))

    def load_profile_occupancy_cache(self) -> Dict[str, Dict]:
        from chromium_advanced.session_manager import SessionManager

        manager = SessionManager(config_path=self.config_path)
        try:
            return manager.list_profile_occupancy()
        except TimeoutError:
            try:
                self.append_log("Profile occupancy registry is temporarily busy; using empty occupancy cache.", prefix="GUI")
            except Exception:
                pass
            return {}

    def format_scene_type_label(self, scene_type: str) -> str:
        scene_type = str(scene_type or "").strip().lower()
        mapping = {
            "mcp": self.tr("occupancy_scene_mcp", "MCP"),
            "manual": self.tr("occupancy_scene_manual", "MANUAL"),
            "keepalive": self.tr("occupancy_scene_keepalive", "KEEPALIVE"),
            "automation": self.tr("occupancy_scene_automation", "SCRIPT"),
            "in_use": self.tr("occupancy_scene_in_use", "IN USE"),
            "unknown": self.tr("occupancy_scene_unknown", "IN USE"),
        }
        return mapping.get(scene_type, scene_type.upper() if scene_type else self.tr("occupancy_scene_unknown", "IN USE"))

    def format_occupancy_entry_summary(self, profile_name: str, occupancy: Dict) -> str:
        if not isinstance(occupancy, dict) or not occupancy:
            return f"{profile_name}: {self.tr('runtime_state_idle', 'Idle')}"
        scene_label = self.format_scene_type_label(occupancy.get("scene_type", ""))
        state = str(occupancy.get("state", "") or "active")
        owner_label = str(occupancy.get("owner_label", "") or "").strip()
        owner_pid = int(occupancy.get("owner_pid", 0) or 0)
        lease_expires_at = float(occupancy.get("lease_expires_at", 0.0) or 0.0)
        expired = occupancy_entry_is_expired(occupancy)
        lease_text = "-"
        if lease_expires_at > 0:
            lease_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease_expires_at))
        return (
            f"{profile_name}: {scene_label}/{state}"
            f" | owner={owner_label or '-'}"
            f" | pid={owner_pid or '-'}"
            f" | lease_until={lease_text}"
            f" | expired={'yes' if expired else 'no'}"
        )

    def reconcile_manual_occupancy(self, process_map: Optional[Dict[str, List[int]]] = None) -> None:
        process_map = process_map if isinstance(process_map, dict) else self.external_profile_process_map
        changed = False
        for profile_name, occupancy in list(self.profile_occupancy_cache.items()):
            if not isinstance(occupancy, dict):
                continue
            scene_type = str(occupancy.get("scene_type", "") or "").strip()
            state = str(occupancy.get("state", "") or "").strip()
            if scene_type != "manual" or state in {"released", "start_failed"}:
                continue
            if process_map.get(profile_name):
                continue
            _clear_gui_profile_occupancy(profile_name)
            changed = True
            self.append_log(
                self.tr(
                    "log_profile_manual_occupancy_released",
                    "{profile_name} manual occupancy was cleared because no matching Chromium process remained.",
                ).format(profile_name=profile_name)
            )
        if changed:
            self.profile_occupancy_cache = self.load_profile_occupancy_cache()

    def format_occupancy_event_text(self, payload: Dict) -> str:
        profile_name = str(payload.get("profile_name", "") or "-")
        scene_type = str(payload.get("scene_type", "") or "unknown")
        state = str(payload.get("state", "") or "active")
        owner_label = str(payload.get("owner_label", "") or "").strip()
        engine_name = str(payload.get("engine_name", "") or "").strip()
        parts = [f"{profile_name} {scene_type}:{state}"]
        if owner_label:
            parts.append(owner_label)
        if engine_name:
            parts.append(f"engine={engine_name}")
        return " | ".join(parts)

    def on_occupancy_events_timer(self):
        path = get_occupancy_events_path()
        try:
            if not os.path.exists(path):
                self.occupancy_event_file_offset = 0
                return
            file_size = os.path.getsize(path)
            if file_size < self.occupancy_event_file_offset:
                self.occupancy_event_file_offset = 0
            with open(path, "r", encoding="utf-8") as handle:
                handle.seek(self.occupancy_event_file_offset)
                lines = handle.readlines()
                self.occupancy_event_file_offset = handle.tell()
        except Exception:
            return

        if not lines:
            return

        emitted = False
        for raw_line in lines[-20:]:
            raw_line = str(raw_line or "").strip()
            if not raw_line:
                continue
            try:
                payload = json.loads(raw_line)
            except Exception:
                continue
            self.append_log(self.format_occupancy_event_text(payload), prefix="OCC")
            emitted = True
        if emitted:
            self.profile_occupancy_cache = self.load_profile_occupancy_cache()
            self.refresh_table()
            self.update_selected_profile_status()
            self.refresh_bottom_stats()
            self.refresh_occupancy_tab()

    def refresh_occupancy_tab(self):
        entries = self.load_profile_occupancy_cache()
        recent_events = read_recent_jsonl_events(get_occupancy_events_path(), limit=80)
        active_lines = []
        for profile_name in sorted(entries.keys(), key=profile_sort_key):
            active_lines.append(self.format_occupancy_entry_summary(profile_name, entries.get(profile_name, {})))
        if not active_lines:
            active_lines.append(self.tr("runtime_state_idle", "Idle"))
        self.occupancy_summary.setText(
            self.tr(
                "occupancy_summary_template",
                "Active occupancies: {active_count} | Recent events: {event_count}",
            ).format(active_count=len(entries), event_count=len(recent_events))
        )
        event_lines = [self.format_occupancy_event_text(item) for item in recent_events[-40:]]
        payload = ["[ACTIVE]", *active_lines, "", "[EVENTS]", *event_lines]
        self.occupancy_output.setPlainText("\n".join(payload))

    def reclaim_selected_profile(self):
        profile = self.get_selected_profile()
        if not profile:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return
        profile_name = str(profile.get("profile_name", "") or "").strip()
        if not profile_name:
            return
        from chromium_advanced.session_manager import SessionManager

        manager = SessionManager(config_path=self.config_path)
        try:
            result = manager.reclaim_profile(profile_name, reason="gui_manual_reclaim")
        except Exception as exc:
            QMessageBox.warning(self, self.tr("running_title"), str(exc))
            return
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.refresh_external_profile_process_state()
        self.refresh_occupancy_tab()
        self.append_log(
            self.tr(
                "log_profile_reclaimed",
                "{profile_name} reclaimed: terminated={terminated_process_count}",
            ).format(
                profile_name=profile_name,
                terminated_process_count=result.get("terminated_process_count", 0),
            )
        )

    def get_profile_status_display(self, profile_name: str) -> Dict[str, str]:
        occupancy = self.profile_occupancy_cache.get(profile_name, {})
        if occupancy:
            scene_type = str(occupancy.get("scene_type", "") or "").strip() or "in_use"
            state = str(occupancy.get("state", "") or "").strip() or "active"
            owner_label = str(occupancy.get("owner_label", "") or "").strip()
            label = self.format_scene_type_label(scene_type)
            if state not in {"active", "running"}:
                label = f"{label}/{state}"
            tooltip = owner_label or f"{scene_type} ({state})"
            if occupancy.get("engine_name"):
                tooltip = f"{tooltip}\nengine={occupancy.get('engine_name')}"
            if occupancy.get("session_id"):
                tooltip = f"{tooltip}\nsession={occupancy.get('session_id')}"
            return {"label": label, "tooltip": tooltip}
        if self.is_profile_keepalive_running(profile_name):
            return {"label": "KEEPALIVE", "tooltip": "keepalive running"}
        pids = self.external_profile_process_map.get(profile_name, [])
        if pids:
            return {"label": "MANUAL", "tooltip": f"external chromium pid={', '.join(str(pid) for pid in pids)}"}
        return {"label": self.tr("runtime_state_idle", "Idle"), "tooltip": self.tr("runtime_state_idle", "Idle")}

    def sync_profiles(self):
        self.config = sync_profiles_with_user_data(self.config)
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(self.tr("log_profiles_synced"))

    def get_selected_row(self) -> int:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return -1
        return indexes[0].row()

    def get_selected_profile(self) -> Optional[Dict]:
        row = self.get_selected_row()
        if row < 0 or row >= len(self.config.get("profiles", [])):
            return None
        return self.config["profiles"][row]

    def on_table_double_clicked(self, row: int, column: int):
        if column in (0, 1):
            self.edit_selected_profile()

    def on_table_selection_changed(self):
        profile = self.get_selected_profile()
        self.selected_profile_name = profile.get("profile_name", "") if profile else ""
        self.update_selected_profile_status()

    def update_selected_profile_status(self):
        profile = self.get_selected_profile()
        if not profile and self.selected_profile_name:
            for item in self.config.get("profiles", []):
                if item.get("profile_name") == self.selected_profile_name:
                    profile = item
                    break
        if not profile:
            self.selected_profile_status.setPlainText(self.tr("status_no_profile_selected"))
            return
        base_text = build_profile_detail_text(profile, self.tr)
        runtime_text = self.build_profile_runtime_state_text(profile.get("profile_name", ""))
        occupancy = self.profile_occupancy_cache.get(profile.get("profile_name", ""), {})
        occupancy_text = "-"
        if occupancy:
            owner_pid = int(occupancy.get("owner_pid", 0) or 0)
            lease_expires_at = float(occupancy.get("lease_expires_at", 0.0) or 0.0)
            lease_text = "-"
            if lease_expires_at > 0:
                lease_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease_expires_at))
            occupancy_text = (
                f"{occupancy.get('scene_type', '-')}/{occupancy.get('state', '-')}"
                f" ({occupancy.get('owner_label', '-')})"
                f" pid={owner_pid or '-'}"
                f" lease={lease_text}"
            )
        self.selected_profile_status.setPlainText(
            f"{base_text}\n"
            f"{self.tr('detail_runtime_state', 'Runtime State')}: {runtime_text}\n"
            f"{self.tr('table_status', 'Status')}: {occupancy_text}"
        )

    def is_profile_keepalive_running(self, profile_name: str) -> bool:
        profile_name = str(profile_name or "").strip()
        if not profile_name or self.keepalive_worker is None:
            return False
        if self.keepalive_running_profile_name == profile_name:
            return True
        return len(self.keepalive_target_profiles) == 1 and self.keepalive_target_profiles[0] == profile_name

    def is_single_profile_keepalive_active(self) -> bool:
        return self.keepalive_worker is not None and len(self.keepalive_target_profiles) == 1

    def is_profile_keepalive_ui_locked(self, profile_name: str) -> bool:
        profile_name = str(profile_name or "").strip()
        if not profile_name or self.keepalive_worker is None:
            return False
        if self.is_profile_keepalive_running(profile_name):
            return True
        if not self.keepalive_target_profiles:
            return False
        return profile_name in self.keepalive_target_profiles

    def status_color_for_profile(self, profile: Dict) -> Optional[QColor]:
        status = profile.get("last_keepalive_status", "never")
        if status == "success":
            return QColor("#1e7d34")
        if status == "partial":
            return QColor("#b26a00")
        if status == "failed":
            return QColor("#c62828")
        if status == "stopped":
            return QColor("#616161")
        return None

    def create_profile_site_selector(self, profile: Dict) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        site_flags = profile.get("keepalive_sites", {}) or {}
        last_details = profile.get("last_keepalive_details", {}) or {}
        enabled_site_names = [
            site_name for site_name in get_keepalive_site_ids(self.config) if bool(site_flags.get(site_name, False))
        ]
        if not enabled_site_names:
            empty_label = QLabel("-")
            empty_label.setStyleSheet("color: #757575;")
            layout.addWidget(empty_label)
            layout.addStretch()
            return wrapper

        for site_name in enabled_site_names:
            base_label = self.tr(f"site_name_{site_name}", get_keepalive_site_label(site_name, self.config))
            raw_info = last_details.get(site_name, {}) if isinstance(last_details, dict) else {}
            info = normalize_keepalive_site_result_for_display(raw_info)
            site_status = str((info or {}).get("status", "") or "").strip().lower()
            suffix_map = {
                "signed_out": self.tr("keepalive_site_badge_signed_out"),
                "attention": self.tr("keepalive_site_badge_attention"),
                "failed": self.tr("keepalive_site_badge_failed"),
                "success": self.tr("keepalive_site_badge_success"),
            }
            icon_path = get_keepalive_site_icon_path(site_name, self.config, fetch=False)
            checkbox_text = ""
            if not icon_path:
                checkbox_text = base_label
                if site_status in suffix_map:
                    checkbox_text = f"{base_label} {suffix_map[site_status]}"
            checkbox = QCheckBox(checkbox_text)
            if icon_path:
                checkbox.setIcon(QIcon(icon_path))
                checkbox.setIconSize(QSize(16, 16))
            checkbox.setChecked(bool(site_flags.get(site_name, False)))
            checkbox.setEnabled(self.keepalive_worker is None)
            checkbox.setToolTip(
                format_keepalive_site_status(site_name, info, self.tr) if info else self.tr("site_checkbox_tooltip")
            )
            style_map = {
                "signed_out": "QCheckBox { border: 1px solid #c62828; background: #fdecea; border-radius: 4px; padding: 2px 4px; }",
                "attention": "QCheckBox { border: 1px solid #b26a00; background: #fff4db; border-radius: 4px; padding: 2px 4px; }",
                "failed": "QCheckBox { border: 1px solid #8e0000; background: #fbe9e7; border-radius: 4px; padding: 2px 4px; }",
                "success": "QCheckBox { border: 1px solid #1e7d34; background: #e9f6ec; border-radius: 4px; padding: 2px 4px; }",
            }
            checkbox.setStyleSheet(style_map.get(site_status, "QCheckBox { border: 1px solid #d0d0d0; border-radius: 4px; padding: 2px 4px; }"))
            checkbox.stateChanged.connect(
                lambda state, name=profile.get("profile_name", ""), site=site_name: self.set_profile_keepalive_site_enabled(
                    name, site, state == Qt.Checked
                )
            )
            layout.addWidget(checkbox)
        layout.addStretch()
        return wrapper

    def warm_keepalive_icon_cache_async(self):
        try:
            config_snapshot = json.loads(json.dumps(self.config, ensure_ascii=False))
        except Exception:
            config_snapshot = {}

        def _worker():
            warm_keepalive_site_icon_cache(config_snapshot)

        threading.Thread(target=_worker, name="keepalive-icon-cache", daemon=True).start()
        QTimer.singleShot(4000, self.refresh_table)

    def refresh_keepalive_plugin_table(self):
        self.keepalive_plugin_records = get_keepalive_plugin_records(self.config)
        self.plugin_table.blockSignals(True)
        self.plugin_table.setRowCount(len(self.keepalive_plugin_records))
        for row, record in enumerate(self.keepalive_plugin_records):
            source_text = record.get("source") or self.tr("plugin_type_system")
            values = [
                record.get("site_id", ""),
                record.get("display_name", ""),
                self.tr("plugin_type_system") if record.get("builtin") else self.tr("plugin_type_external"),
                source_text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if column != 1:
                    item.setTextAlignment(Qt.AlignCenter if column == 2 else Qt.AlignLeft | Qt.AlignVCenter)
                self.plugin_table.setItem(row, column, item)
        self.plugin_table.blockSignals(False)

        if self.keepalive_plugin_records:
            selected_row = 0
            if self.selected_plugin_site_id:
                for index, record in enumerate(self.keepalive_plugin_records):
                    if record.get("site_id") == self.selected_plugin_site_id:
                        selected_row = index
                        break
            self.plugin_table.selectRow(selected_row)
            self.on_keepalive_plugin_selection_changed()
        else:
            self.selected_plugin_site_id = ""
            self.plugin_source_editor.setPlainText("")
            self.plugin_source_editor.setReadOnly(True)
            self.btn_plugin_save.setEnabled(False)
            self.btn_plugin_delete.setEnabled(False)
            self.plugin_detail_site_id.setText("-")
            self.plugin_detail_display_name.setText("-")
            self.plugin_detail_type.setText("-")
            self.plugin_detail_source.setText("-")
            self.plugin_detail_home_url.setText("-")
            self.plugin_detail_icon_url.setText("-")
            self.plugin_source_status.setText(self.tr("plugin_status_empty"))

    def get_selected_keepalive_plugin_record(self) -> Optional[Dict]:
        selected_items = self.plugin_table.selectedItems()
        if not selected_items:
            return None
        row = selected_items[0].row()
        if row < 0 or row >= len(self.keepalive_plugin_records):
            return None
        return self.keepalive_plugin_records[row]

    def on_keepalive_plugin_selection_changed(self):
        record = self.get_selected_keepalive_plugin_record()
        if not record:
            return
        self.selected_plugin_site_id = str(record.get("site_id", "") or "").strip()
        self.plugin_detail_site_id.setText(self.selected_plugin_site_id or "-")
        self.plugin_detail_display_name.setText(str(record.get("display_name", "") or "-"))
        self.plugin_detail_type.setText(self.tr("plugin_type_system") if record.get("builtin") else self.tr("plugin_type_external"))
        self.plugin_detail_source.setText(str(record.get("source", "") or self.tr("plugin_type_system")))
        self.plugin_detail_home_url.setText(str(record.get("home_url", "") or "-"))
        self.plugin_detail_icon_url.setText(str(record.get("icon_url", "") or "-"))
        editable = not bool(record.get("builtin"))
        allow_edit = editable and self.keepalive_worker is None
        self.plugin_source_editor.setReadOnly(not allow_edit)
        self.btn_plugin_save.setEnabled(allow_edit)
        self.btn_plugin_delete.setEnabled(allow_edit)
        self.plugin_source_status.setText(self.tr("plugin_status_loading"))
        try:
            source_text = get_keepalive_plugin_source_text(self.selected_plugin_site_id, self.config)
            self.plugin_source_editor.setPlainText(source_text)
            base_status = self.tr("plugin_status_readonly") if not editable else self.tr("plugin_status_editable")
            load_error = str(record.get("load_error", "") or "").strip()
            self.plugin_source_status.setText(
                self.trf("plugin_status_loaded_with_error", status=base_status, error=load_error)
                if load_error
                else base_status
            )
        except Exception as exc:
            self.plugin_source_editor.setPlainText("")
            self.plugin_source_status.setText(self.trf("plugin_status_load_failed", error=exc))

    def create_keepalive_plugin(self):
        dialog = KeepalivePluginCreateDialog(self, self.tr)
        if self.exec_modal_dialog(dialog) != QDialog.Accepted:
            return
        payload = dialog.get_data()
        site_id = payload.get("site_id", "")
        if not site_id:
            QMessageBox.warning(self, self.tr("error_generic_title"), self.tr("plugin_error_site_id_required"))
            return
        try:
            source_text = build_keepalive_plugin_template(
                site_id,
                display_name=payload.get("display_name", ""),
                home_url=payload.get("home_url", ""),
            )
            save_result = save_keepalive_plugin_source(site_id, source_text, self.config)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_generic_title"), self.trf("plugin_error_create_failed", error=exc))
            return
        self.selected_plugin_site_id = str(save_result.get("site_id", "") or site_id)
        self.refresh_keepalive_plugin_table()
        self.warm_keepalive_icon_cache_async()
        self.refresh_table()
        self.append_log(
            self.trf(
                "plugin_log_created",
                site_id=str(save_result.get("site_id", "") or site_id),
                path=save_result.get("path", ""),
            )
        )

    def save_current_keepalive_plugin(self):
        record = self.get_selected_keepalive_plugin_record()
        if not record or record.get("builtin"):
            return
        try:
            save_result = save_keepalive_plugin_source(record.get("site_id", ""), self.plugin_source_editor.toPlainText(), self.config)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_generic_title"), self.trf("plugin_error_save_failed", error=exc))
            return
        previous_site_id = str(save_result.get("previous_site_id", "") or record.get("site_id", "") or "")
        current_site_id = str(save_result.get("site_id", "") or previous_site_id)
        if previous_site_id and current_site_id and previous_site_id != current_site_id:
            self.config, _ = migrate_keepalive_site_id_references(self.config, previous_site_id, current_site_id)
            self.config = save_app_config(self.config, self.config_path)
        self.append_log(self.trf("plugin_log_saved", site_id=current_site_id, path=save_result.get("path", "")))
        self.selected_plugin_site_id = current_site_id
        self.refresh_keepalive_plugin_table()
        self.warm_keepalive_icon_cache_async()
        self.refresh_table()

    def delete_current_keepalive_plugin(self):
        record = self.get_selected_keepalive_plugin_record()
        if not record or record.get("builtin"):
            return
        site_id = str(record.get("site_id", "") or "")
        answer = QMessageBox.question(
            self,
            self.tr("plugin_delete_title"),
            self.trf("plugin_delete_message", site_id=site_id),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            path = delete_keepalive_plugin_source(site_id, self.config)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_generic_title"), self.trf("plugin_error_delete_failed", error=exc))
            return
        self.selected_plugin_site_id = ""
        self.refresh_keepalive_plugin_table()
        self.warm_keepalive_icon_cache_async()
        self.refresh_table()
        self.append_log(self.trf("plugin_log_deleted", site_id=site_id, path=path))

    def open_keepalive_plugin_dir(self):
        plugin_root = get_keepalive_plugin_root()
        try:
            os.startfile(plugin_root)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_open_failed_title"), self.trf("plugin_error_open_dir", error=exc))

    def refresh_table(self):
        profiles = sorted(self.config.get("profiles", []), key=lambda item: profile_sort_key(item.get("profile_name", "")))
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, profile in enumerate(profiles):
                self.table.insertRow(row)
                profile_item = QTableWidgetItem(profile.get("profile_name", ""))
                account_text = profile.get("account", "") or "-"
                account_item = QTableWidgetItem(account_text)
                status_payload = self.get_profile_status_display(profile.get("profile_name", ""))
                status_item = QTableWidgetItem(status_payload.get("label", ""))
                color = self.status_color_for_profile(profile)
                if color:
                    profile_item.setForeground(color)
                    account_item.setForeground(color)
                    status_item.setForeground(color)
                tooltip = profile.get("last_keepalive_message", "") or self.tr("status_keepalive_never")
                profile_item.setToolTip(tooltip)
                account_item.setToolTip(tooltip)
                status_item.setToolTip(status_payload.get("tooltip", tooltip))
                self.table.setItem(row, 0, profile_item)
                self.table.setItem(row, 1, account_item)
                self.table.setItem(row, 2, status_item)
                self.table.setCellWidget(row, 3, self.create_profile_site_selector(profile))

                keepalive_checkbox = QCheckBox()
                profile_name = profile.get("profile_name", "")
                row_locked = self.is_profile_keepalive_ui_locked(profile_name)
                is_running = self.is_profile_keepalive_running(profile_name)

                keepalive_checkbox.setChecked(bool(profile.get("keepalive_enabled", False)))
                keepalive_checkbox.setEnabled(self.keepalive_worker is None)
                keepalive_checkbox.stateChanged.connect(
                    lambda state, name=profile_name: self.set_profile_keepalive_enabled(
                        name, state == Qt.Checked
                    )
                )
                keepalive_wrapper = QWidget()
                keepalive_layout = QHBoxLayout(keepalive_wrapper)
                keepalive_layout.setContentsMargins(4, 2, 4, 2)
                keepalive_layout.addWidget(keepalive_checkbox)
                keepalive_layout.setAlignment(Qt.AlignCenter)
                self.table.setCellWidget(row, 4, keepalive_wrapper)

                launch_button = QPushButton(self.tr("action_launch"))
                external_running = bool(get_chromium_processes_for_profile(self.config, profile_name))
                launch_button.setText(self.tr("action_close", "Close") if external_running else self.tr("action_launch"))
                launch_button.setEnabled(not row_locked)
                if row_locked:
                    launch_button.setToolTip(self.tr("info_keepalive_already_running"))
                elif external_running:
                    launch_button.setToolTip(self.tr("profile_close_tooltip", "Close this profile and kill its Chromium processes."))
                else:
                    launch_button.setToolTip("")
                launch_button.clicked.connect(lambda _, name=profile_name: self.toggle_profile_launch_by_name(name))
                keepalive_button = QPushButton(self.tr("action_stop") if is_running else self.tr("action_keepalive"))
                if is_running:
                    keepalive_button.setStyleSheet("background-color: #f8d7da; color: #b00020;")
                else:
                    keepalive_button.setStyleSheet("")
                    if self.keepalive_worker is not None:
                        keepalive_button.setEnabled(False)
                        keepalive_button.setToolTip(self.tr("info_keepalive_already_running"))
                keepalive_button.clicked.connect(lambda _, name=profile_name: self.run_keepalive_for_profile(name))
                button_wrapper = QWidget()
                button_layout = QHBoxLayout(button_wrapper)
                button_layout.setContentsMargins(4, 2, 4, 2)
                button_layout.setSpacing(4)
                button_layout.addWidget(launch_button)
                button_layout.addWidget(keepalive_button)
                button_layout.setAlignment(Qt.AlignCenter)
                self.table.setCellWidget(row, 5, button_wrapper)

            if profiles:
                row_to_select = 0
                if self.selected_profile_name:
                    for index, item in enumerate(profiles):
                        if item.get("profile_name") == self.selected_profile_name:
                            row_to_select = index
                            break
                self.table.selectRow(row_to_select)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

    def add_profile(self):
        user_data_root = self.config["paths"].get("user_data_profiles_root", "")
        if not user_data_root:
            QMessageBox.warning(self, self.tr("warn_missing_path_title"), self.tr("warn_missing_user_data_root"))
            return

        profile_name = next_profile_name(self.config)
        new_profile = {
            "profile_name": profile_name,
            "user_data_dir_name": "",
            "account": "",
            "keepalive_enabled": False,
            "keepalive_sites": {},
            "notes": "",
        }
        dialog = ProfileEditDialog(new_profile, self.config, self, self.tr)
        if self.exec_modal_dialog(dialog) != QDialog.Accepted:
            return

        new_profile.update(dialog.get_data())
        try:
            ensure_profile_directory(self.config, profile_name)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_create_failed_title"), self.trf("error_create_profile_dir", error=exc))
            return

        self.config["profiles"].append(new_profile)
        self.config = save_app_config(self.config, self.config_path)
        try:
            if ensure_profile_bookmarks_initialized(self.config, profile_name):
                self.append_log(self.trf("log_profile_bookmarks_initialized", profile_name=profile_name))
        except Exception as exc:
            self.append_log(self.trf("log_profile_bookmarks_init_failed", profile_name=profile_name, error=exc))
        self.selected_profile_name = profile_name
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(self.trf("log_profile_created", profile_name=profile_name))

    def edit_selected_profile(self):
        profile = self.get_selected_profile()
        if not profile:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return

        dialog = ProfileEditDialog(profile, self.config, self, self.tr)
        if self.exec_modal_dialog(dialog) != QDialog.Accepted:
            return

        updated = dialog.get_data()
        for item in self.config["profiles"]:
            if item.get("profile_name") == profile.get("profile_name"):
                item.update(updated)
                break
        self.config = save_app_config(self.config, self.config_path)
        self.selected_profile_name = updated["profile_name"]
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(self.trf("log_profile_updated", profile_name=updated["profile_name"]))

    def remove_selected_profile(self):
        profile = self.get_selected_profile()
        if not profile:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return

        profile_dir = get_profile_directory_path(self.config, profile.get("profile_name", ""))
        if os.path.isdir(profile_dir):
            QMessageBox.information(self, self.tr("cannot_remove_entry_title"), self.tr("cannot_remove_entry_message"))
            return

        answer = QMessageBox.question(
            self,
            self.tr("confirm_remove_title"),
            self.trf("confirm_remove_message", profile_name=profile.get("profile_name")),
        )
        if answer != QMessageBox.Yes:
            return

        self.config["profiles"] = [
            item
            for item in self.config.get("profiles", [])
            if item.get("profile_name") != profile.get("profile_name")
        ]
        self.selected_profile_name = ""
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(self.trf("log_entry_removed", profile_name=profile.get("profile_name")))

    def remove_selected_profile_with_directory(self):
        profile = self.get_selected_profile()
        if not profile:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return

        profile_name = profile.get("profile_name", "")
        if not profile_name:
            QMessageBox.warning(self, self.tr("error_remove_failed_title"), self.tr("error_invalid_profile_name"))
            return

        if self.keepalive_worker is not None:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_keepalive_running_delete"))
            return

        running_processes = get_chromium_processes_for_profile(self.config, profile_name)
        if running_processes:
            QMessageBox.information(self, self.tr("close_chromium_title"), self.tr("close_chromium_before_delete"))
            return

        user_data_root = os.path.abspath(get_profile_user_data_root(self.config, profile_name).strip())
        if not user_data_root:
            QMessageBox.warning(self, self.tr("warn_missing_path_title"), self.tr("warn_missing_user_data_root"))
            return

        profile_dir = os.path.abspath(get_profile_directory_path(self.config, profile_name))
        try:
            if os.path.commonpath([profile_dir, user_data_root]) != user_data_root:
                raise ValueError("profile directory escaped user data root")
        except ValueError:
            QMessageBox.critical(self, self.tr("error_remove_failed_title"), self.trf("error_illegal_profile_dir", profile_dir=profile_dir))
            return

        answer = QMessageBox.question(
            self,
            self.tr("confirm_delete_profile_title"),
            self.trf("confirm_delete_profile_message", profile_name=profile_name),
        )
        if answer != QMessageBox.Yes:
            return

        if os.path.isdir(profile_dir):
            try:
                shutil.rmtree(profile_dir)
            except Exception as exc:
                QMessageBox.critical(self, self.tr("error_remove_failed_title"), self.trf("error_delete_profile_dir", error=exc))
                self.append_log(self.trf("log_delete_profile_dir_failed", profile_name=profile_name, error=exc))
                return

        self.config["profiles"] = [
            item
            for item in self.config.get("profiles", [])
            if item.get("profile_name") != profile_name
        ]
        self.selected_profile_name = ""
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(self.trf("log_profile_deleted_full", profile_name=profile_name))

    def launch_profile_by_name(self, profile_name: str):
        if not profile_name:
            return
        engine_name = normalize_browser_engine_name(
            self.config.get("app", {}).get("browser_engine", DEFAULT_BROWSER_ENGINE)
        )
        try:
            if ensure_profile_bookmarks_initialized(self.config, profile_name):
                self.append_log(self.trf("log_profile_bookmarks_synced", profile_name=profile_name))
        except Exception as exc:
            self.append_log(self.trf("log_profile_bookmarks_sync_failed", profile_name=profile_name, error=exc))

        try:
            result = launch_profile(profile_name, self.config)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_launch_failed_title"), str(exc))
            self.append_log(self.trf("log_profile_launch_failed", profile_name=profile_name, error=exc))
            return

        _write_gui_profile_occupancy(
            profile_name,
            scene_type="manual",
            state="active",
            owner_label="GUI launch",
            engine_name=engine_name,
        )

        self.config = update_profile_launch_time(self.config, profile_name)
        self.config = save_app_config(self.config, self.config_path)
        self.selected_profile_name = profile_name
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.refresh_table()
        self.update_selected_profile_status()
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        details = stdout or stderr or f"returncode={result.returncode}"
        details = f"engine={engine_name}; {details}"
        self.append_log(self.trf("log_profile_launched", profile_name=profile_name, details=details))
        if result.returncode != 0:
            QMessageBox.warning(self, self.tr("warn_launch_return_title"), details)

    def close_profile_by_name(self, profile_name: str):
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            return
        processes = get_chromium_processes_for_profile(self.config, profile_name)
        if not processes:
            self.refresh_external_profile_process_state()
            return
        terminated = terminate_chromium_processes(processes, logger=None)
        self.selected_profile_name = profile_name
        _clear_gui_profile_occupancy(profile_name)
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.refresh_external_profile_process_state()
        details = self.tr("profile_close_none", "no matching Chromium process found")
        if terminated:
            details = self.tr("profile_close_killed_count", "terminated {count} Chromium process(es)").format(count=terminated)
        self.append_log(
            self.tr("log_profile_closed", "{profile_name} closed: {details}").format(
                profile_name=profile_name,
                details=details,
            )
        )

    def toggle_profile_launch_by_name(self, profile_name: str):
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            return
        if get_chromium_processes_for_profile(self.config, profile_name):
            self.close_profile_by_name(profile_name)
            return
        self.launch_profile_by_name(profile_name)

    def launch_selected_profile(self):
        profile = self.get_selected_profile()
        if not profile:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return
        self.toggle_profile_launch_by_name(profile.get("profile_name", ""))

    def get_selected_profile_names(self) -> List[str]:
        profile = self.get_selected_profile()
        if not profile:
            return []
        return [profile.get("profile_name", "")]

    def run_keepalive_for_profile(self, profile_name: str):
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_invalid_profile"))
            return
        if self.is_profile_keepalive_running(profile_name):
            self.request_keepalive_stop(profile_name)
            return
        self.selected_profile_name = profile_name
        self.start_keepalive_worker([profile_name], f"manual:profile:{profile_name}")

    def request_keepalive_stop(self, profile_name: str = ""):
        if self.keepalive_worker is None:
            return
        if self.keepalive_stop_requested:
            return
        self.keepalive_stop_requested = True
        self.append_log(
            self.trf("log_keepalive_stop_requested", profile_suffix=(f": {profile_name}" if profile_name else "")),
            prefix=self.keepalive_log_prefix or self.tr("keepalive_source_default"),
        )
        self.keepalive_worker.request_stop()
        self.refresh_table()

    def save_keepalive_settings(self):
        keepalive = self.config["keepalive"]
        keepalive["headless"] = self.keepalive_headless.isChecked()
        keepalive["page_timeout_seconds"] = self.keepalive_timeout.value()
        keepalive["between_profiles_seconds"] = self.keepalive_between_profiles.value()
        keepalive["settle_seconds"] = self.keepalive_settle.value()
        keepalive["site_dwell_seconds"] = self.keepalive_site_dwell.value()
        keepalive["chatgpt_prompt"] = self.chatgpt_prompt.text().strip()
        keepalive["chatgpt_conversation_hint"] = self.chatgpt_conversation_hint.text().strip()
        keepalive["google_query"] = self.google_query.text().strip()
        keepalive["plugin_dirs"] = [item.strip() for item in self.keepalive_plugin_dirs.text().split(";") if item.strip()]
        keepalive["schedule_time"] = qtime_to_string(self.schedule_time.time())
        self.config = save_app_config(self.config, self.config_path)
        self.warm_keepalive_icon_cache_async()
        self.refresh_keepalive_plugin_table()
        self.refresh_table()
        self.append_log(self.tr("log_keepalive_settings_saved"))
        self.refresh_scheduler_status()

    def set_profile_keepalive_enabled(self, profile_name: str, enabled: bool):
        changed = False
        for item in self.config.get("profiles", []):
            if item.get("profile_name") != profile_name:
                continue
            if bool(item.get("keepalive_enabled", False)) == bool(enabled):
                return
            item["keepalive_enabled"] = bool(enabled)
            changed = True
            break

        if not changed:
            return

        self.config = save_app_config(self.config, self.config_path)
        self.selected_profile_name = profile_name
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(
            self.trf(
                "log_profile_keepalive_toggled",
                profile_name=profile_name,
                state=self.tr("state_enabled") if enabled else self.tr("state_disabled"),
            )
        )

    def set_profile_keepalive_site_enabled(self, profile_name: str, site_name: str, enabled: bool):
        changed = False
        for item in self.config.get("profiles", []):
            if item.get("profile_name") != profile_name:
                continue
            site_flags = dict(item.get("keepalive_sites", {}) or {})
            if bool(site_flags.get(site_name, False)) == bool(enabled):
                return
            if enabled:
                site_flags[site_name] = True
            else:
                site_flags.pop(site_name, None)
            item["keepalive_sites"] = site_flags
            changed = True
            break

        if not changed:
            return

        self.config = save_app_config(self.config, self.config_path)
        self.selected_profile_name = profile_name
        self.refresh_table()
        self.update_selected_profile_status()
        self.append_log(
            self.trf(
                "log_profile_keepalive_site_toggled",
                profile_name=profile_name,
                site_name=self.tr(f"site_name_{site_name}"),
                state=self.tr("state_checked") if enabled else self.tr("state_unchecked"),
            )
        )

    def load_keepalive_settings_to_ui(self):
        keepalive = self.config["keepalive"]
        self.keepalive_headless.setChecked(bool(keepalive.get("headless", False)))
        self.keepalive_timeout.setValue(int(keepalive.get("page_timeout_seconds", 45)))
        self.keepalive_between_profiles.setValue(int(keepalive.get("between_profiles_seconds", 5)))
        self.keepalive_settle.setValue(int(keepalive.get("settle_seconds", 3)))
        self.keepalive_site_dwell.setValue(int(keepalive.get("site_dwell_seconds", 6)))
        chatgpt_prompt = str(keepalive.get("chatgpt_prompt", ""))
        if chatgpt_prompt == LEGACY_CHATGPT_PROMPT:
            chatgpt_prompt = ""
        self.chatgpt_prompt.setText(chatgpt_prompt)
        self.chatgpt_conversation_hint.setText(str(keepalive.get("chatgpt_conversation_hint", "")))
        self.google_query.setText(str(keepalive.get("google_query", "")))
        plugin_dirs = keepalive.get("plugin_dirs", [])
        if isinstance(plugin_dirs, list):
            self.keepalive_plugin_dirs.setText("; ".join(str(item) for item in plugin_dirs if str(item or "").strip()))
        else:
            self.keepalive_plugin_dirs.setText("")
        self.schedule_time.setTime(parse_schedule_time(str(keepalive.get("schedule_time", "09:00"))))

        self.global_last_run.setText(keepalive.get("last_run_at", "") or "-")
        self.global_last_status.setText(keepalive.get("last_run_status", "") or "-")
        self.global_last_message.setText(keepalive.get("last_run_message", "") or "-")

    def load_path_settings_to_ui(self):
        paths = self.config.get("paths", {})
        for key, line_edit in self.path_editors.items():
            line_edit.setText(str(paths.get(key, "")))

    def load_app_settings_to_ui(self):
        app_settings = self.config.get("app", {})
        language = normalize_language_code(app_settings.get("language", detect_default_language()))
        self.current_language = language
        if hasattr(self, "language_combo"):
            self.language_combo.blockSignals(True)
            for index in range(self.language_combo.count()):
                if self.language_combo.itemData(index) == language:
                    self.language_combo.setCurrentIndex(index)
                    break
            self.language_combo.blockSignals(False)
        if hasattr(self, "browser_engine_combo"):
            current_engine = normalize_browser_engine_name(app_settings.get("browser_engine", DEFAULT_BROWSER_ENGINE))
            self.browser_engine_combo.blockSignals(True)
            for index in range(self.browser_engine_combo.count()):
                if self.browser_engine_combo.itemData(index) == current_engine:
                    self.browser_engine_combo.setCurrentIndex(index)
                    break
            self.browser_engine_combo.blockSignals(False)
        if hasattr(self, "concurrency_mode_combo"):
            current_mode = str(app_settings.get("concurrency_mode", "per_profile_live") or "per_profile_live").strip().lower()
            if current_mode not in CONCURRENCY_MODE_OPTIONS:
                current_mode = "per_profile_live"
            self.concurrency_mode_combo.blockSignals(True)
            for index in range(self.concurrency_mode_combo.count()):
                if self.concurrency_mode_combo.itemData(index) == current_mode:
                    self.concurrency_mode_combo.setCurrentIndex(index)
                    break
            self.concurrency_mode_combo.blockSignals(False)

    def on_language_changed(self):
        if not hasattr(self, "language_combo"):
            return
        language = normalize_language_code(self.language_combo.currentData() or detect_default_language())
        if language == self.current_language:
            return
        self.current_language = language
        self.config.setdefault("app", {})
        self.config["app"]["language"] = language
        self.config = save_app_config(self.config, self.config_path)
        self.retranslate_ui()

    def on_browser_engine_changed(self):
        if not hasattr(self, "browser_engine_combo"):
            return
        engine_name = normalize_browser_engine_name(
            self.browser_engine_combo.currentData() or DEFAULT_BROWSER_ENGINE
        )
        current_engine = normalize_browser_engine_name(
            self.config.get("app", {}).get("browser_engine", DEFAULT_BROWSER_ENGINE)
        )
        if engine_name == current_engine:
            return
        self.config.setdefault("app", {})
        self.config["app"]["browser_engine"] = engine_name
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_mcp_status_ui()
        self.append_log(f"Browser engine saved: {engine_name}")

    def on_concurrency_mode_changed(self):
        if not hasattr(self, "concurrency_mode_combo"):
            return
        mode_name = str(self.concurrency_mode_combo.currentData() or "per_profile_live").strip().lower()
        if mode_name not in CONCURRENCY_MODE_OPTIONS:
            mode_name = "per_profile_live"
        current_mode = str(self.config.get("app", {}).get("concurrency_mode", "per_profile_live") or "per_profile_live").strip().lower()
        if mode_name == current_mode:
            return
        self.config.setdefault("app", {})
        self.config["app"]["concurrency_mode"] = mode_name
        self.config = save_app_config(self.config, self.config_path)
        self.append_log(f"Concurrency mode saved: {mode_name}")
        self.refresh_mcp_status_ui()

    def retranslate_ui(self):
        self.setWindowTitle(self.tr("window_title"))
        self.btn_add.setText(self.tr("toolbar_add"))
        self.btn_edit.setText(self.tr("toolbar_edit"))
        self.btn_remove.setText(self.tr("toolbar_remove"))
        self.btn_remove_with_dir.setText(self.tr("toolbar_remove_full"))
        self.btn_sync.setText(self.tr("toolbar_sync"))
        self.btn_launch_selected.setText(self.tr("toolbar_launch"))
        self.btn_reclaim_selected.setText(self.tr("toolbar_reclaim"))
        self.btn_keepalive_selected.setText(self.tr("toolbar_keepalive_selected"))
        self.btn_keepalive_all.setText(self.tr("toolbar_keepalive_all"))
        self.btn_open_config_dir.setText(self.tr("toolbar_open_config"))
        self.app_auto_start_checkbox.setText(self.tr("toolbar_autostart"))
        self.close_to_tray_checkbox.setText(self.tr("toolbar_tray"))
        self.mcp_service_checkbox.setText(self.tr("toolbar_mcp"))
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("table_profile"),
                self.tr("table_account"),
                self.tr("table_status", "Status"),
                self.tr("table_logged_in"),
                self.tr("table_keepalive"),
                self.tr("table_actions"),
            ]
        )
        self.tabs.setTabText(self.tabs.indexOf(self.keepalive_tab), self.tr("tab_keepalive"))
        self.tabs.setTabText(self.tabs.indexOf(self.plugin_tab), self.tr("tab_plugins"))
        self.tabs.setTabText(self.tabs.indexOf(self.log_tab), self.tr("tab_logs"))
        self.tabs.setTabText(self.tabs.indexOf(self.occupancy_tab), self.tr("tab_occupancy"))
        self.tabs.setTabText(self.tabs.indexOf(self.mcp_log_tab), self.tr("tab_mcp_logs"))
        self.tabs.setTabText(self.tabs.indexOf(self.config_tab), self.tr("tab_config"))
        self.selected_group.setTitle(self.tr("group_selected"))
        self.settings_group.setTitle(self.tr("group_keepalive"))
        self.summary_group.setTitle(self.tr("group_summary"))
        self.mcp_status_group.setTitle(self.tr("group_mcp_status"))
        self.form_group.setTitle(self.tr("group_paths"))
        self.mcp_group.setTitle(self.tr("group_mcp_config"))
        self.bottom_status_label.setText(self.tr("bottom_ready"))
        self.refresh_bottom_stats()

        self.btn_save_keepalive.setText(self.tr("save_keepalive"))
        self.btn_refresh_task.setText(self.tr("refresh_status"))
        self.btn_refresh_occupancy.setText(self.tr("refresh_status"))
        self.btn_clear_occupancy_view.setText(self.tr("clear_logs"))
        self.btn_plugin_reload.setText(self.tr("plugin_action_reload"))
        self.btn_plugin_new.setText(self.tr("plugin_action_new"))
        self.btn_plugin_save.setText(self.tr("plugin_action_save"))
        self.btn_plugin_delete.setText(self.tr("plugin_action_delete"))
        self.btn_plugin_open_dir.setText(self.tr("plugin_action_open_dir"))
        self.btn_clear_logs.setText(self.tr("clear_logs"))
        self.btn_clear_mcp_logs.setText(self.tr("clear_mcp_logs"))
        self.btn_save_paths.setText(self.tr("save_paths"))
        self.btn_save_mcp.setText(self.tr("save_mcp"))
        self.btn_restart_mcp.setText(self.tr("restart_mcp"))
        self.btn_reload.setText(self.tr("reload_disk"))
        self.keepalive_headless.setText(self.tr("headless"))
        self.mcp_status_detail_label.setText(self.tr("mcp_detail_idle"))
        self.site_scope_hint.setText(self.tr("site_scope_hint"))
        self.chatgpt_conversation_hint.setPlaceholderText(self.tr("chatgpt_hint_placeholder"))
        self.keepalive_plugin_dirs.setPlaceholderText(self.tr("keepalive_plugin_dirs_placeholder"))
        self.keepalive_plugin_dirs_browse.setText(self.tr("browse"))
        self.schedule_time.setToolTip(self.tr("schedule_time_tooltip"))
        self.keepalive_timeout.setSuffix(self.tr("unit_seconds"))
        self.keepalive_between_profiles.setSuffix(self.tr("unit_seconds"))
        self.keepalive_settle.setSuffix(self.tr("unit_seconds"))
        self.keepalive_site_dwell.setSuffix(self.tr("unit_seconds"))

        path_keys = [
            ("chromium_dir", "path_chromium"),
            ("chromedriver_path", "path_driver"),
            ("user_data_profiles_root", "path_user_data"),
            ("mirror_user_data_root", "path_mirror_user_data"),
            ("bookmarks_template_path", "path_bookmarks"),
            ("fingerprint_zip_path", "path_fingerprint"),
        ]
        for key, title_key in path_keys:
            self.form_layout.labelForField(self.path_editors[key].parentWidget()).setText(self.tr(title_key))
            self.path_browse_buttons[key].setText(self.tr("browse"))
        self.form_layout.labelForField(self.language_combo).setText(self.tr("language"))
        self.form_layout.labelForField(self.browser_engine_combo).setText(self.tr("browser_engine"))
        self.form_layout.labelForField(self.concurrency_mode_combo).setText(self.tr("concurrency_mode"))

        self.settings_layout.labelForField(self.keepalive_headless).setText(self.tr("headless"))
        self.settings_layout.labelForField(self.site_scope_hint).setText(self.tr("site_label"))
        self.settings_layout.labelForField(self.keepalive_timeout).setText(self.tr("page_timeout"))
        self.settings_layout.labelForField(self.keepalive_between_profiles).setText(self.tr("between_profiles"))
        self.settings_layout.labelForField(self.keepalive_settle).setText(self.tr("settle"))
        self.settings_layout.labelForField(self.keepalive_site_dwell).setText(self.tr("site_dwell"))
        self.settings_layout.labelForField(self.chatgpt_prompt).setText(self.tr("chatgpt_prompt"))
        self.settings_layout.labelForField(self.chatgpt_conversation_hint).setText(self.tr("chatgpt_hint"))
        self.settings_layout.labelForField(self.google_query).setText(self.tr("google_query"))
        self.settings_layout.labelForField(self.keepalive_plugin_dirs_row).setText(self.tr("keepalive_plugin_dirs"))
        self.settings_layout.labelForField(self.schedule_time).setText(self.tr("schedule_time"))
        self.plugin_detail_group.setTitle(self.tr("plugin_group_detail"))
        self.plugin_source_hint.setText(self.tr("plugin_source_hint"))
        self.plugin_source_editor.setPlaceholderText(self.tr("plugin_editor_placeholder"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_site_id).setText(self.tr("plugin_table_site_id"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_display_name).setText(self.tr("plugin_table_display_name"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_type).setText(self.tr("plugin_table_type"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_source).setText(self.tr("plugin_table_source"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_home_url).setText(self.tr("plugin_detail_home_url"))
        self.plugin_detail_layout.labelForField(self.plugin_detail_icon_url).setText(self.tr("plugin_detail_icon_url"))
        self.plugin_table.setHorizontalHeaderLabels(
            [
                self.tr("plugin_table_site_id"),
                self.tr("plugin_table_display_name"),
                self.tr("plugin_table_type"),
                self.tr("plugin_table_source"),
            ]
        )

        self.summary_layout.labelForField(self.global_last_run).setText(self.tr("last_run"))
        self.summary_layout.labelForField(self.global_last_status).setText(self.tr("last_status"))
        self.summary_layout.labelForField(self.global_last_message).setText(self.tr("last_message"))
        self.summary_layout.labelForField(self.global_task_status).setText(self.tr("task_status"))
        self.summary_layout.labelForField(self.global_task_next_run).setText(self.tr("next_run"))
        self.summary_layout.labelForField(self.global_task_last_result).setText(self.tr("today_result"))

        self.mcp_status_layout.labelForField(self.mcp_status_label).setText(self.tr("mcp_state"))
        self.mcp_status_layout.labelForField(self.mcp_endpoint_label).setText(self.tr("mcp_endpoint"))
        self.mcp_status_layout.labelForField(self.mcp_worker_endpoint_label).setText(self.tr("mcp_worker"))
        self.mcp_status_layout.labelForField(self.mcp_default_engine_label).setText(self.tr("mcp_default_engine"))
        self.mcp_status_layout.labelForField(self.mcp_trace_path_label).setText(self.tr("mcp_trace_path"))
        self.mcp_status_layout.labelForField(self.mcp_status_detail_label).setText(self.tr("mcp_detail"))

        self.mcp_api_token_label.setText(self.tr("mcp_api_token"))
        self.mcp_api_token_edit.setToolTip(self.tr("mcp_api_token_tooltip"))
        self.btn_regenerate_api_token.setText(self.tr("mcp_regenerate_token"))
        self._refresh_api_token_warning(self.mcp_api_token_edit.text().strip())
        self.mcp_start_minimized_checkbox.setText(self.tr("mcp_start_minimized_hint"))
        self.mcp_layout.labelForField(self.mcp_start_minimized_checkbox).setText(self.tr("mcp_start_minimized"))
        self.mcp_layout.labelForField(self.mcp_log_level_combo).setText(self.tr("mcp_log_level"))
        if hasattr(self, "tray_action_show") and self.tray_action_show:
            self.tray_action_show.setText(self.tr("tray_show"))
        if hasattr(self, "tray_action_hide") and self.tray_action_hide:
            self.tray_action_hide.setText(self.tr("tray_hide"))
        if hasattr(self, "tray_action_exit") and self.tray_action_exit:
            self.tray_action_exit.setText(self.tr("tray_exit"))
        self.refresh_table()
        self.update_selected_profile_status()
        self.refresh_scheduler_status()

    def load_mcp_settings_to_ui(self):
        settings = self.config.get("mcp", {})
        transport = str(settings.get("transport", MCP_TRANSPORT_OPTIONS[0]))
        if transport not in MCP_TRANSPORT_OPTIONS:
            transport = MCP_TRANSPORT_OPTIONS[0]
        self.mcp_transport_combo.setCurrentText(transport)
        self.mcp_host_edit.setText(str(settings.get("host", "127.0.0.1")))
        self.mcp_port_spin.setValue(int(settings.get("port", 28888)))
        self.mcp_worker_port_spin.setValue(int(settings.get("worker_port", 28889)))
        self.mcp_path_edit.setText(str(settings.get("path", "/mcp")))
        self.mcp_idle_timeout_spin.setValue(int(settings.get("idle_timeout_seconds", 60)))
        worker_policy = str(settings.get("worker_policy", "sticky") or "sticky")
        if worker_policy not in MCP_WORKER_POLICY_OPTIONS:
            worker_policy = "sticky"
        self.mcp_worker_policy_combo.setCurrentText(worker_policy)
        self.mcp_start_minimized_checkbox.setChecked(bool(settings.get("start_minimized", True)))
        log_level = str(settings.get("log_level", "info"))
        if log_level not in MCP_LOG_LEVEL_OPTIONS:
            log_level = "info"
        self.mcp_log_level_combo.setCurrentText(log_level)
        self.mcp_service_checkbox.blockSignals(True)

        api_token = str(settings.get("api_token", ""))
        self.mcp_api_token_edit.setText(api_token)
        self.mcp_admin_token_edit.setText(str(settings.get("admin_token", "")))
        self._refresh_api_token_warning(api_token)
        self.mcp_service_checkbox.setChecked(bool(settings.get("enabled", False)))
        self.mcp_service_checkbox.blockSignals(False)
        self.refresh_mcp_status_ui()

    def save_path_settings(self):
        for key, line_edit in self.path_editors.items():
            self.config["paths"][key] = line_edit.text().strip()
        user_data_root = str(self.config["paths"].get("user_data_profiles_root", "")).strip()
        if not user_data_root:
            user_data_root = get_default_split_user_data_profiles_root(str(self.config["paths"].get("user_data_root", "")).strip())
            self.config["paths"]["user_data_profiles_root"] = user_data_root
            if "user_data_profiles_root" in self.path_editors:
                self.path_editors["user_data_profiles_root"].setText(user_data_root)
        mirror_user_data_root = str(self.config["paths"].get("mirror_user_data_root", "")).strip()
        resolved_default_mirror_root = os.path.join(user_data_root, "mirror_disk")
        if not mirror_user_data_root or is_legacy_default_mirror_root(mirror_user_data_root):
            resolved_mirror_root = resolved_default_mirror_root
            self.config["paths"]["mirror_user_data_root"] = resolved_mirror_root
            if "mirror_user_data_root" in self.path_editors:
                self.path_editors["mirror_user_data_root"].setText(resolved_mirror_root)
        self.config.setdefault("app", {})
        self.config["app"]["browser_engine"] = normalize_browser_engine_name(
            self.browser_engine_combo.currentData() or DEFAULT_BROWSER_ENGINE
        )
        self.config["app"]["concurrency_mode"] = str(self.concurrency_mode_combo.currentData() or "per_profile_live").strip().lower()
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_table()
        self.append_log(self.tr("log_base_config_saved"))


    def regenerate_api_token(self):
        import secrets
        new_token = secrets.token_hex(24)
        new_admin_token = secrets.token_hex(24)
        self.config.setdefault("mcp", {})
        self.config["mcp"]["api_token"] = new_token
        self.config["mcp"]["admin_token"] = new_admin_token
        self.config = save_app_config(self.config, self.config_path)
        self.mcp_api_token_edit.setText(new_token)
        self.mcp_admin_token_edit.setText(new_admin_token)
        self._refresh_api_token_warning(new_token)
        self.append_log(self.tr("log_api_token_regenerated"))

    def _refresh_api_token_warning(self, api_token=""):
        host = (self.mcp_host_edit.text().strip() or "127.0.0.1").lower()
        is_local = host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")
        if not (api_token or "").strip():
            self.mcp_auth_warning_label.setText(self.tr("mcp_auth_warning_no_token"))
            self.mcp_auth_warning_label.show()
        elif not is_local:
            self.mcp_auth_warning_label.setText(self.tr("mcp_auth_warning_remote"))
            self.mcp_auth_warning_label.show()
        else:
            self.mcp_auth_warning_label.hide()

    def save_mcp_settings(self):
        self.config.setdefault("mcp", {})
        self.config["mcp"]["transport"] = self.mcp_transport_combo.currentText().strip() or MCP_TRANSPORT_OPTIONS[0]
        self.config["mcp"]["host"] = self.mcp_host_edit.text().strip() or "127.0.0.1"
        path_text = self.mcp_path_edit.text().strip() or "/mcp"
        if not path_text.startswith("/"):
            path_text = "/" + path_text
        self.config["mcp"]["path"] = path_text
        self.config["mcp"]["port"] = self.mcp_port_spin.value()
        self.config["mcp"]["worker_port"] = self.mcp_worker_port_spin.value()
        self.config["mcp"]["idle_timeout_seconds"] = self.mcp_idle_timeout_spin.value()
        self.config["mcp"]["worker_policy"] = self.mcp_worker_policy_combo.currentText().strip() or "sticky"
        self.config["mcp"]["start_minimized"] = self.mcp_start_minimized_checkbox.isChecked()
        self.config["mcp"]["log_level"] = self.mcp_log_level_combo.currentText().strip() or "info"
        self.config["mcp"]["enabled"] = self.mcp_service_checkbox.isChecked()
        self.config = save_app_config(self.config, self.config_path)
        self.append_log(self.tr("log_mcp_config_saved"))
        self.refresh_mcp_status_ui()

    def pick_path(self, key: str, mode: str):
        current = self.path_editors[key].text().strip()
        if mode == "dir":
            selected = QFileDialog.getExistingDirectory(self, self.tr("file_dialog_select_dir"), current or os.path.expanduser("~"))
        else:
            selected, _ = QFileDialog.getOpenFileName(self, self.tr("file_dialog_select_file"), current or os.path.expanduser("~"))
        if selected:
            self.path_editors[key].setText(selected)

    def pick_keepalive_plugin_dir(self):
        current_text = self.keepalive_plugin_dirs.text().strip()
        existing = [item.strip() for item in current_text.split(";") if item.strip()]
        start_dir = existing[-1] if existing and os.path.isdir(existing[-1]) else os.path.expanduser("~")
        selected = QFileDialog.getExistingDirectory(self, self.tr("file_dialog_select_dir"), start_dir)
        if not selected:
            return
        normalized_existing = {os.path.normcase(os.path.abspath(item)) for item in existing}
        if os.path.normcase(os.path.abspath(selected)) not in normalized_existing:
            existing.append(selected)
        self.keepalive_plugin_dirs.setText("; ".join(existing))

    def open_config_dir(self):
        config_dir = get_state_storage_dir()
        try:
            os.startfile(config_dir)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_open_failed_title"), self.trf("error_open_config_dir", error=exc))

    def get_mcp_endpoint(self) -> str:
        settings = self.config.get("mcp", {})
        host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        port = int(settings.get("port", 28888))
        path = str(settings.get("path", "/mcp")).strip() or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{host}:{port}{path}"

    def get_mcp_worker_endpoint(self) -> str:
        settings = self.config.get("mcp", {})
        path = str(settings.get("path", "/mcp")).strip() or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        worker_port = int(settings.get("worker_port", 28889))
        return f"http://127.0.0.1:{worker_port}{path}"

    def get_mcp_trace_path(self) -> str:
        return os.environ.get("CHROMIUM_ADVANCED_MCP_TRACE_PATH") or os.path.join(
            tempfile.gettempdir(),
            "chromium-advanced-mcp-trace.jsonl",
        )

    def get_mcp_status_url(self) -> str:
        settings = self.config.get("mcp", {})
        host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(settings.get("port", 28888))
        return f"http://{host}:{port}/_daemon/status"

    def get_mcp_auth_headers(self) -> Dict[str, str]:
        settings = self.config.get("mcp", {}) if isinstance(self.config, dict) else {}
        api_token = str(settings.get("api_token", "")).strip() if isinstance(settings, dict) else ""
        if not api_token:
            return {}
        return {"Authorization": f"Bearer {api_token}"}

    def get_mcp_admin_auth_headers(self) -> Dict[str, str]:
        settings = self.config.get("mcp", {}) if isinstance(self.config, dict) else {}
        admin_token = str(settings.get("admin_token", "")).strip() if isinstance(settings, dict) else ""
        if not admin_token:
            return self.get_mcp_auth_headers()
        return {"Authorization": f"Bearer {admin_token}"}

    def get_mcp_connect_host_port(self):
        settings = self.config.get("mcp", {})
        host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        port = int(settings.get("port", 28888))
        return host, port

    def is_mcp_expected_enabled(self) -> bool:
        return bool(self.config.get("mcp", {}).get("enabled", False))

    def query_mcp_status(
        self,
        force: bool = False,
        expected_pid: int = 0,
        expected_instance_id: str = "",
    ) -> Dict:
        now_ts = time.monotonic()
        if (
            not force
            and self.mcp_status_cache
            and self.mcp_status_last_query_at > 0
            and (now_ts - self.mcp_status_last_query_at) < MCP_STATUS_CACHE_TTL_SECONDS
        ):
            return self.mcp_status_cache
        try:
            status = fetch_json(
                self.get_mcp_status_url(),
                timeout=MCP_STATUS_QUERY_TIMEOUT_SECONDS,
                headers=self.get_mcp_auth_headers(),
            )
            if expected_pid:
                status_pid = int(status.get("daemon_pid", 0) or 0) if isinstance(status, dict) else 0
                if status_pid != int(expected_pid):
                    raise RuntimeError(f"daemon pid mismatch: expected {expected_pid}, got {status_pid or '-'}")
            if expected_instance_id:
                status_instance_id = str(status.get("daemon_instance_id", "") or "").strip() if isinstance(status, dict) else ""
                if status_instance_id != str(expected_instance_id).strip():
                    raise RuntimeError(
                        f"daemon instance mismatch: expected {expected_instance_id}, got {status_instance_id or '-'}"
                    )
            if isinstance(status, dict) and status and not bool(status.get("daemon_ready", True)):
                raise RuntimeError("daemon is warming up")
            self.mcp_status_last_query_at = now_ts
            if isinstance(status, dict) and status:
                self.mcp_status_cache = status
                self.mcp_status_last_ok_at = now_ts
                self.mcp_status_consecutive_failures = 0
                return status
        except Exception:
            self.mcp_status_last_query_at = now_ts
            self.mcp_status_consecutive_failures += 1
        if self.mcp_status_cache and self.mcp_status_last_ok_at > 0:
            if (now_ts - self.mcp_status_last_ok_at) < MCP_RECENT_HEALTH_GRACE_SECONDS:
                return self.mcp_status_cache
        self.mcp_status_cache = {}
        return {}

    def refresh_mcp_status_ui(self):
        self.mcp_endpoint_label.setText(self.get_mcp_endpoint())
        self.mcp_worker_endpoint_label.setText(self.get_mcp_worker_endpoint())
        self.mcp_trace_path_label.setText(self.get_mcp_trace_path())
        self.mcp_default_engine_label.setText(
            f"{normalize_browser_engine_name(self.config.get('app', {}).get('browser_engine', DEFAULT_BROWSER_ENGINE))}"
            f" / {str(self.config.get('app', {}).get('concurrency_mode', 'per_profile_live') or 'per_profile_live')}"
        )

        daemon_status = self.query_mcp_status()
        if daemon_status and self.is_mcp_expected_enabled():
            worker_state = str(daemon_status.get("worker_state", "stopped"))
            worker_pid = daemon_status.get("worker_pid")
            active_requests = int(daemon_status.get("active_proxy_requests", 0) or 0)
            idle_seconds = int(daemon_status.get("idle_seconds", 0) or 0)
            idle_timeout = int(daemon_status.get("idle_timeout_seconds", 0) or 0)
            worker_policy = str(daemon_status.get("worker_policy", "sticky") or "sticky")
            status_build_ms = int(daemon_status.get("status_build_ms", 0) or 0)
            external_scan_ms = int(((daemon_status.get("server_status") or {}) if isinstance(daemon_status.get("server_status"), dict) else {}).get("external_scan_ms", 0) or 0)
            if worker_state == "running":
                self.mcp_status_label.setText(self.tr("mcp_state_running"))
                self.mcp_status_detail_label.setText(
                    self.trf(
                        "mcp_status_detail_running",
                        worker_pid=(worker_pid or "-"),
                        active_requests=active_requests,
                        idle_seconds=idle_seconds,
                        idle_timeout=idle_timeout,
                    )
                    + f" policy={worker_policy}, status={status_build_ms}ms, scan={external_scan_ms}ms"
                )
            else:
                self.mcp_status_label.setText(self.tr("mcp_state_guarding"))
                self.mcp_status_detail_label.setText(
                    self.trf("mcp_status_detail_guarding", reason=(daemon_status.get("last_stop_reason") or "-"))
                    + f" policy={worker_policy}, status={status_build_ms}ms, scan={external_scan_ms}ms"
                )
            self.refresh_bottom_stats()
            return

        if daemon_status:
            self.mcp_status_label.setText(self.tr("mcp_state_stopped"))
            self.mcp_status_detail_label.setText(self.tr("mcp_status_detail_stopped"))
            self.refresh_bottom_stats()
            return

        if self.mcp_startup_in_progress:
            self.mcp_status_label.setText(self.tr("mcp_state_starting"))
            self.mcp_status_detail_label.setText(self.tr("mcp_status_detail_starting"))
            self.refresh_bottom_stats()
            return

        if not self.is_mcp_expected_enabled():
            self.mcp_status_label.setText(self.tr("mcp_state_not_started"))
            self.mcp_status_detail_label.setText(self.tr("mcp_status_detail_not_started"))
            self.refresh_bottom_stats()
            return

        self.mcp_status_label.setText(self.tr("mcp_state_waiting"))
        self.mcp_status_detail_label.setText(self.tr("mcp_status_detail_waiting"))
        self.refresh_bottom_stats()

    def apply_initial_mcp_state(self):
        if self.mcp_startup_applied:
            return
        self.mcp_startup_applied = True
        if bool(self.config.get("mcp", {}).get("enabled", False)):
            self.start_mcp_service()

    def ensure_mcp_process(self):
        if self.mcp_process is not None:
            return
        self.mcp_process = QProcess(self)
        self.mcp_process.setProcessChannelMode(QProcess.MergedChannels)
        self.mcp_process.readyReadStandardOutput.connect(self.on_mcp_process_output)
        self.mcp_process.stateChanged.connect(self.on_mcp_process_state_changed)
        self.mcp_process.finished.connect(self.on_mcp_process_finished)
        self.mcp_process.errorOccurred.connect(self.on_mcp_process_error)

    def finish_mcp_startup_failure(self):
        self.mcp_startup_in_progress = False
        self.mcp_startup_deadline = None
        self.mcp_stop_requested = True
        self.cleanup_mcp_process_residue()
        self.mcp_launch_pid = 0
        self.mcp_owned_process = False
        self.mcp_status_cache = {}
        self.refresh_mcp_status_ui()

    def check_mcp_health_after_start(self, startup_token: int):
        if startup_token != self.mcp_startup_token or not self.mcp_startup_in_progress:
            return
        try:
            status = self.query_mcp_status(force=True, expected_pid=self.mcp_launch_pid)
            if status:
                self.mcp_startup_in_progress = False
                self.mcp_startup_deadline = None
                self.refresh_mcp_status_ui()
                return
        except Exception:
            pass

        deadline = self.mcp_startup_deadline or datetime.datetime.now()
        if datetime.datetime.now() >= deadline:
            self.append_mcp_log(self.tr("log_mcp_watchdog_port_down"), prefix="MCP-ERR")
            self.finish_mcp_startup_failure()
            return

        QTimer.singleShot(
            MCP_HEALTHCHECK_POLL_INTERVAL_MS,
            lambda token=startup_token: self.check_mcp_health_after_start(token),
        )

    def build_mcp_process_arguments(self) -> List[str]:
        settings = self.config.get("mcp", {})
        if getattr(sys, "frozen", False):
            args = [
                "--transport",
                str(settings.get("transport", MCP_TRANSPORT_OPTIONS[0])),
                "--host",
                str(settings.get("host", "127.0.0.1")),
                "--port",
                str(int(settings.get("port", 28888))),
                "--worker-port",
                str(int(settings.get("worker_port", 28889))),
                "--path",
                str(settings.get("path", "/mcp")),
                "--log-level",
                str(settings.get("log_level", "info")),
                "--idle-timeout-seconds",
                str(int(settings.get("idle_timeout_seconds", 60))),
                "--worker-policy",
                str(settings.get("worker_policy", "sticky") or "sticky"),
                "--config-path",
                self.config_path,
            ]

            api_token = str(settings.get("api_token", "")).strip()
            if api_token:
                args.append("--api-token")
                args.append(api_token)
            admin_token = str(settings.get("admin_token", "")).strip()
            if admin_token:
                args.append("--admin-token")
                args.append(admin_token)
            return args

        args = [
            "-m",
            "chromium_advanced.mcp_daemon",
            "--transport",
            str(settings.get("transport", MCP_TRANSPORT_OPTIONS[0])),
            "--host",
            str(settings.get("host", "127.0.0.1")),
            "--port",
            str(int(settings.get("port", 28888))),
            "--worker-port",
            str(int(settings.get("worker_port", 28889))),
            "--path",
            str(settings.get("path", "/mcp")),
            "--log-level",
            str(settings.get("log_level", "info")),
            "--idle-timeout-seconds",
            str(int(settings.get("idle_timeout_seconds", 60))),
            "--worker-policy",
            str(settings.get("worker_policy", "sticky") or "sticky"),
            "--config-path",
            self.config_path,
        ]

        api_token = str(settings.get("api_token", "")).strip()
        if api_token:
            args.append("--api-token")
            args.append(api_token)
        admin_token = str(settings.get("admin_token", "")).strip()
        if admin_token:
            args.append("--admin-token")
            args.append(admin_token)
        return args

    def start_mcp_service(self):
        self.save_mcp_settings()
        if self.mcp_startup_in_progress:
            self.refresh_mcp_status_ui()
            return
        if self.query_mcp_status(force=True):
            self.mcp_owned_process = True
            self.refresh_mcp_status_ui()
            return

        terminated_pids = terminate_project_mcp_processes(exclude_pid=os.getpid())
        if terminated_pids:
            self.append_mcp_log(
                self.trf("log_mcp_cleanup_stale", pid_text=", ".join(str(pid) for pid in terminated_pids)),
                prefix="MCP",
            )

        self.mcp_restart_pending = False
        self.mcp_stop_requested = False
        self.mcp_owned_process = True
        self.mcp_startup_in_progress = True
        self.mcp_startup_token += 1
        self.mcp_status_consecutive_failures = 0
        self.mcp_startup_deadline = datetime.datetime.now() + datetime.timedelta(
            milliseconds=max(0, int(MCP_HEALTHCHECK_START_TIMEOUT_MS))
        )
        self.append_mcp_log(self.tr("log_mcp_prepare_start"))
        program = sys.executable
        if getattr(sys, "frozen", False):
            program = get_frozen_companion_executable("ChromiumMcpDaemon")
        command = [program, *self.build_mcp_process_arguments()]
        try:
            process = subprocess.Popen(
                command,
                cwd=get_runtime_launch_cwd(program),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **get_hidden_subprocess_kwargs(),
            )
            self.mcp_launch_pid = int(process.pid or 0)
        except Exception as exc:
            self.mcp_startup_in_progress = False
            self.mcp_startup_deadline = None
            self.mcp_owned_process = False
            self.append_mcp_log(self.trf("log_mcp_error", error=exc), prefix="MCP-ERR")
            self.refresh_mcp_status_ui()
            return
        self.refresh_mcp_status_ui()
        QTimer.singleShot(0, lambda token=self.mcp_startup_token: self.check_mcp_health_after_start(token))

    def stop_mcp_service(self, update_checkbox: bool = True):
        self.mcp_startup_in_progress = False
        self.mcp_startup_deadline = None
        self.mcp_startup_token += 1
        self.mcp_status_consecutive_failures = 0
        self.mcp_restart_pending = False
        self.mcp_stop_requested = True
        self.append_mcp_log(self.tr("log_mcp_prepare_stop"))
        self.cleanup_mcp_process_residue()
        self.mcp_launch_pid = 0
        if update_checkbox:
            self.mcp_service_checkbox.blockSignals(True)
            self.mcp_service_checkbox.setChecked(False)
            self.mcp_service_checkbox.blockSignals(False)
        self.mcp_owned_process = False
        self.mcp_status_cache = {}
        self.refresh_mcp_status_ui()

    def restart_mcp_service(self):
        if not self.is_mcp_expected_enabled():
            self.mcp_service_checkbox.setChecked(True)
            return
        self.stop_mcp_service(update_checkbox=False)
        self.mcp_restart_pending = False
        QTimer.singleShot(800, self.start_mcp_service)

    def on_mcp_service_checkbox_changed(self, state):
        enabled = (state == Qt.Checked)
        if enabled:
            self.start_mcp_service()
        else:
            self.config.setdefault("mcp", {})
            self.config["mcp"]["enabled"] = False
            self.config = save_app_config(self.config, self.config_path)
            self.stop_mcp_service(update_checkbox=False)

    def on_mcp_process_output(self):
        if self.mcp_process is None:
            return
        text = bytes(self.mcp_process.readAllStandardOutput()).decode(errors="replace")
        if text.strip():
            self.append_mcp_log(text.rstrip())

    def on_mcp_process_state_changed(self, _state):
        self.refresh_mcp_status_ui()

    def on_mcp_process_finished(self, exit_code: int, exit_status):
        return

    def on_mcp_process_error(self, error):
        return

    def cleanup_mcp_process_residue(self):
        terminated_pids = terminate_project_mcp_processes(exclude_pid=os.getpid())
        if terminated_pids:
            self.append_mcp_log(
                self.trf("log_mcp_cleanup_stale", pid_text=", ".join(str(pid) for pid in terminated_pids)),
                prefix="MCP",
            )

    def on_mcp_watchdog_timer(self):
        self.refresh_external_profile_process_state()
        self.refresh_mcp_status_ui()
        if not self.is_mcp_expected_enabled():
            return
        if self.mcp_startup_in_progress:
            return
        daemon_status = self.query_mcp_status(force=True)
        if daemon_status:
            return
        if self.mcp_status_consecutive_failures >= 3:
            if find_project_mcp_processes(exclude_pid=os.getpid()):
                return
            self.append_mcp_log(self.tr("log_mcp_watchdog_not_running"), prefix="MCP-WARN")
            self.start_mcp_service()

    def setup_tray_icon(self):
        self.tray_icon = None
        self.tray_menu = None
        self.tray_action_show = None
        self.tray_action_hide = None
        self.tray_action_exit = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = self.windowIcon()
        if tray_icon.isNull():
            tray_icon = get_app_icon()

        self.tray_icon = QSystemTrayIcon(tray_icon, self)
        self.tray_icon.setToolTip(APP_NAME)

        self.tray_menu = QMenu(self)
        self.tray_action_show = self.tray_menu.addAction(self.tr("tray_show"))
        self.tray_action_show.triggered.connect(self.show_from_tray)
        self.tray_action_hide = self.tray_menu.addAction(self.tr("tray_hide"))
        self.tray_action_hide.triggered.connect(self.hide_to_tray)
        self.tray_menu.addSeparator()
        self.tray_action_exit = self.tray_menu.addAction(self.tr("tray_exit"))
        self.tray_action_exit.triggered.connect(self.request_exit)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()
        self.update_tray_actions()

    def is_tray_available(self) -> bool:
        return self.tray_icon is not None and QSystemTrayIcon.isSystemTrayAvailable()

    def update_tray_actions(self):
        if not self.is_tray_available():
            return
        is_visible = self.isVisible() and not self.isMinimized()
        self.tray_action_show.setEnabled(not is_visible)
        self.tray_action_hide.setEnabled(is_visible)

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.update_tray_actions()

    def hide_to_tray(self):
        if not self.is_tray_available():
            return False
        self.hide()
        self.update_tray_actions()
        if not self.tray_message_shown:
            self.tray_icon.showMessage(
                APP_NAME,
                self.tr("tray_hidden_message"),
                QSystemTrayIcon.Information,
                3000,
            )
            self.tray_message_shown = True
        return True

    def on_tray_icon_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide_to_tray()
            else:
                self.show_from_tray()

    def should_minimize_to_tray_on_close(self) -> bool:
        app_config = self.config.get("app", {})
        return bool(app_config.get("minimize_to_tray_on_close", True)) and self.is_tray_available()

    def refresh_close_to_tray_checkbox(self):
        tray_available = self.is_tray_available()
        self.close_to_tray_checkbox.setEnabled(tray_available)
        self.close_to_tray_checkbox.blockSignals(True)
        self.close_to_tray_checkbox.setChecked(bool(self.config.get("app", {}).get("minimize_to_tray_on_close", True)))
        self.close_to_tray_checkbox.blockSignals(False)

    def on_close_to_tray_changed(self, state):
        self.config.setdefault("app", {})
        self.config["app"]["minimize_to_tray_on_close"] = (state == Qt.Checked)
        self.config = save_app_config(self.config, self.config_path)
        self.refresh_close_to_tray_checkbox()

    def refresh_app_auto_start_checkbox(self):
        is_windows = (SYSTEM_TYPE == "Windows")
        self.app_auto_start_checkbox.setEnabled(is_windows)
        self.app_auto_start_checkbox.blockSignals(True)
        self.app_auto_start_checkbox.setChecked(is_system_auto_start_enabled())
        self.app_auto_start_checkbox.blockSignals(False)

    def on_app_auto_start_changed(self, state):
        enabled = (state == Qt.Checked)
        try:
            set_system_auto_start_enabled(enabled)
        except NotImplementedError as exc:
            QMessageBox.information(self, self.tr("info_title"), str(exc))
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_generic_title"), self.trf("error_set_autostart_failed", error=exc))
        self.refresh_app_auto_start_checkbox()

    def request_exit(self):
        self.force_exit_requested = True
        self.close()

    def showEvent(self, event):
        super().showEvent(event)
        self.update_tray_actions()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.update_tray_actions()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.schedule_window_bounds_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_window_bounds_save()

    def closeEvent(self, event):
        if not self.force_exit_requested and self.should_minimize_to_tray_on_close():
            event.ignore()
            self.hide_to_tray()
            return

        if self.keepalive_worker is not None:
            self.force_exit_requested = False
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_keepalive_running_exit"))
            event.ignore()
            return

        self.force_exit_requested = False
        self.persist_window_bounds()
        self.stop_mcp_service(update_checkbox=False)
        if self.tray_icon:
            self.tray_icon.hide()
        event.accept()

    def get_enabled_keepalive_profiles(self) -> List[Dict]:
        return [item for item in self.config.get("profiles", []) if item.get("keepalive_enabled", False)]

    def refresh_scheduler_status(self):
        previous_status = self.config.get("keepalive", {}).get("last_run_status")
        previous_run_at = self.config.get("keepalive", {}).get("last_run_at")
        self.reload_config_if_changed()
        keepalive = self.config["keepalive"]
        self.global_last_run.setText(keepalive.get("last_run_at", "") or "-")
        self.global_last_status.setText(keepalive.get("last_run_status", "") or "-")
        self.global_last_message.setText(keepalive.get("last_run_message", "") or "-")

        if previous_status != keepalive.get("last_run_status") or previous_run_at != keepalive.get("last_run_at"):
            self.refresh_table()
            self.update_selected_profile_status()

        enabled_profiles = self.get_enabled_keepalive_profiles()
        if not enabled_profiles:
            self.global_task_status.setText(self.tr("schedule_status_disabled"))
            self.global_task_next_run.setText("-")
            self.global_task_last_result.setText(self.tr("schedule_result_enable_profiles"))
            return

        now_dt = datetime.datetime.now()
        schedule_dt = schedule_time_to_datetime(keepalive.get("schedule_time", "09:00"), now_dt)
        today_text = now_dt.strftime("%Y-%m-%d")
        last_scheduled_date = str(keepalive.get("last_scheduled_run_date", "")).strip()

        if self.keepalive_worker is not None and getattr(self.keepalive_worker, "source", "").startswith("internal-schedule"):
            self.global_task_status.setText(self.tr("schedule_status_running"))
            self.global_task_next_run.setText("-")
            self.global_task_last_result.setText(self.trf("schedule_result_triggered_today", today_text=today_text))
            return

        if last_scheduled_date == today_text:
            self.global_task_status.setText(self.tr("schedule_status_done_today"))
            self.global_task_next_run.setText(format_datetime_for_ui(schedule_dt + datetime.timedelta(days=1)))
            self.global_task_last_result.setText(self.trf("schedule_result_triggered_today", today_text=today_text))
            return

        if now_dt < schedule_dt:
            self.global_task_status.setText(self.tr("schedule_status_waiting"))
            self.global_task_next_run.setText(format_datetime_for_ui(schedule_dt))
            self.global_task_last_result.setText(self.tr("schedule_result_not_triggered_today"))
            return

        self.global_task_status.setText(self.tr("schedule_status_due"))
        self.global_task_next_run.setText(self.tr("schedule_next_run_when_ready"))
        self.global_task_last_result.setText(self.tr("schedule_result_not_triggered_today"))

    def on_scheduler_timer(self):
        if self.is_ui_interaction_busy():
            return

        self.refresh_scheduler_status()

        if self.keepalive_worker is not None:
            return

        enabled_profiles = self.get_enabled_keepalive_profiles()
        if not enabled_profiles:
            return

        keepalive = self.config["keepalive"]
        now_dt = datetime.datetime.now()
        schedule_dt = schedule_time_to_datetime(keepalive.get("schedule_time", "09:00"), now_dt)
        today_text = now_dt.strftime("%Y-%m-%d")
        last_scheduled_date = str(keepalive.get("last_scheduled_run_date", "")).strip()
        if last_scheduled_date == today_text or now_dt < schedule_dt:
            return

        self.start_keepalive_worker([], "internal-schedule", persist_ui_settings=False)

    def run_keepalive_for_selected(self):
        selected_profiles = self.get_selected_profile_names()
        if not selected_profiles:
            QMessageBox.information(self, self.tr("info_title"), self.tr("info_select_profile_first"))
            return
        self.start_keepalive_worker(selected_profiles, "manual:selected")

    def run_keepalive_for_all(self):
        self.start_keepalive_worker([], "manual:all")

    def start_keepalive_worker(self, selected_profiles: List[str], source: str, persist_ui_settings: bool = True):
        if self.keepalive_worker is not None:
            if not source.startswith("internal-schedule"):
                QMessageBox.information(self, self.tr("running_title"), self.tr("info_keepalive_already_running"))
            return

        if persist_ui_settings:
            self.save_keepalive_settings()
        engine_name = normalize_browser_engine_name(
            self.config.get("app", {}).get("browser_engine", DEFAULT_BROWSER_ENGINE)
        )
        self.keepalive_log_prefix = describe_keepalive_source(source, [item for item in selected_profiles if item])
        self.append_log(f"{self.tr('log_keepalive_started')} (engine={engine_name})", prefix=self.keepalive_log_prefix)
        self.keepalive_target_profiles = self.resolve_keepalive_target_profiles(selected_profiles)
        self.keepalive_running_profile_name = ""
        self.keepalive_stop_requested = False
        self.set_keepalive_buttons_enabled(False)
        self.keepalive_worker = KeepAliveWorker(self.config_path, selected_profiles, source, self, self.tr)
        self.keepalive_worker.log_signal.connect(self.on_keepalive_worker_log)
        self.keepalive_worker.payload_signal.connect(self.on_keepalive_worker_message)
        self.keepalive_worker.start()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Chromium profile manager")
    parser.add_argument("--start-minimized", action="store_true", help="Start minimized to tray when available.")
    parser.add_argument("--run-mcp-daemon", action="store_true", help="Run the MCP daemon instead of the GUI.")
    parser.add_argument("--run-mcp-worker", action="store_true", help="Run the MCP worker instead of the GUI.")
    args, remaining = parser.parse_known_args()

    if args.run_mcp_daemon:
        from chromium_advanced.mcp_daemon import main as daemon_main

        sys.argv = [sys.argv[0], *remaining]
        daemon_main()
        return

    if args.run_mcp_worker:
        from chromium_advanced.mcp_server import main as worker_main

        sys.argv = [sys.argv[0], *remaining]
        worker_main()
        return

    single_instance_guard = acquire_single_instance_guard()
    if not single_instance_guard:
        show_single_instance_message()
        raise SystemExit(0)

    app = QApplication(sys.argv)
    app._single_instance_guard = single_instance_guard
    app.aboutToQuit.connect(lambda: release_single_instance_guard(getattr(app, "_single_instance_guard", None)))
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = ChromiumManagerWindow()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    if args.start_minimized:
        if window.is_tray_available():
            QTimer.singleShot(0, window.hide_to_tray)
        else:
            QTimer.singleShot(0, window.showMinimized)
    raise SystemExit(app.exec_())


if __name__ == "__main__":
    main()
