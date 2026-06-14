import argparse
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
from typing import Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtCore import QProcess, QTimer, Qt, QSize
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
    QVBoxLayout,
    QWidget,
)

from chromium_advanced.chromium_profile_lib import (
    APP_NAME,
    LEGACY_CHATGPT_PROMPT,
    KeepAliveStopController,
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
    get_chromium_process_map_by_profile,
    get_chromium_processes_for_profile,
    get_hidden_subprocess_kwargs,
    get_keepalive_site_icon_path,
    get_keepalive_site_ids,
    get_keepalive_site_label,
    get_profile_directory_path,
    get_profile_user_data_root,
    read_recent_jsonl_events,
    is_legacy_default_mirror_root,
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
    save_app_config,
    save_keepalive_plugin_source,
    migrate_keepalive_site_id_references,
    sync_profiles_with_user_data,
    terminate_chromium_processes,
    update_profile_launch_time,
    warm_keepalive_site_icon_cache,
    write_json_atomic,
    run_keepalive_job,
)
from chromium_advanced.browser_engines.constants import BROWSER_ENGINE_OPTIONS, DEFAULT_BROWSER_ENGINE
from chromium_advanced.browser_engines.factory import normalize_browser_engine_name
from chromium_advanced.gui.gui_dialogs import KeepalivePluginCreateDialog, ProfileEditDialog
from chromium_advanced.gui.gui_runtime import (
    acquire_single_instance_guard,
    can_connect,
    describe_keepalive_source,
    fetch_json,
    find_project_mcp_processes,
    format_datetime_for_ui,
    get_app_icon,
    get_frozen_companion_executable,
    is_system_auto_start_enabled,
    parse_schedule_time,
    qtime_to_string,
    release_single_instance_guard,
    schedule_time_to_datetime,
    set_system_auto_start_enabled,
    should_trigger_keepalive_schedule,
    show_single_instance_message,
    terminate_project_mcp_processes,
)
from chromium_advanced.gui.gui_state import (
    build_mcp_auth_headers,
    build_mcp_auth_warning_state,
    build_bottom_stats_text,
    build_mcp_endpoint,
    build_mcp_process_arguments as build_mcp_process_arguments_helper,
    build_mcp_status_url,
    build_mcp_status_view_model,
    build_keepalive_schedule_view_model,
    query_mcp_status_snapshot,
    build_external_profile_process_map,
    build_external_process_refresh_plan,
    build_external_process_transition_messages,
    build_occupancy_tab_payload,
    build_profile_table_row_payload,
    build_profile_table_view_model,
    build_profile_row_action_state,
    build_profile_site_badge_state,
    build_profile_site_selector_payloads,
    build_profile_runtime_state_text,
    build_profile_status_display,
    build_selected_profile_status_text,
    build_keepalive_plugin_table_rows,
    build_keepalive_plugin_selection_view_model,
    build_keepalive_plugin_table_view_model,
    build_keepalive_worker_message_plan,
    build_mcp_startup_failure_plan,
    build_mcp_startup_plan,
    build_mcp_stop_plan,
    collect_stale_manual_occupancy_profiles,
    get_mcp_trace_path as get_mcp_trace_path_helper,
    get_profile_status_color_hex,
    load_profile_occupancy_cache as load_profile_occupancy_cache_helper,
    normalize_mcp_path,
    resolve_mcp_connect_host_port,
    format_occupancy_entry_summary,
    format_occupancy_event_text,
    format_scene_type_label,
    serialize_external_profile_process_map,
)
from chromium_advanced.gui.gui_widgets import (
    FocusWheelSpinBox,
    FocusWheelTimeEdit,
    create_centered_checkbox_widget,
    create_profile_action_buttons_widget,
)
from chromium_advanced.gui.gui_workers import KeepAliveWorker
from chromium_advanced.i18n import load_language_options, load_translations
from chromium_advanced.mcp_runtime_config import resolve_mcp_admin_token, resolve_mcp_api_token
from chromium_advanced.occupancy_registry import (
    clear_profile_occupancy,
    get_occupancy_events_path,
    list_profile_occupancy_entries,
    occupancy_entry_is_expired,
    write_profile_occupancy,
)


SYSTEM_TYPE = platform.system()
SCHEDULER_POLL_MS = 15000
LOG_MAX_BLOCKS = 5000
LOG_FLUSH_INTERVAL_MS = 350
CONFIG_MTIME_EPSILON = 0.0001
MCP_PROCESS_STOP_TIMEOUT_MS = 3000
MCP_WATCHDOG_INTERVAL_MS = 7000
MCP_HEALTHCHECK_START_TIMEOUT_MS = 15000
MCP_HEALTHCHECK_POLL_INTERVAL_MS = 250
MCP_STATUS_QUERY_TIMEOUT_SECONDS = 0.6
MCP_STATUS_CACHE_TTL_SECONDS = 1.0
MCP_RECENT_HEALTH_GRACE_SECONDS = 30.0
MCP_WATCHDOG_RESTART_FAILURES = 6
OCCUPANCY_EVENTS_POLL_MS = 4000
SCHEDULE_TRIGGER_WINDOW_SECONDS = 90
UI_REFRESH_DEBOUNCE_MS = 75
MCP_TRANSPORT_OPTIONS = ["streamable-http", "http", "sse"]
MCP_LOG_LEVEL_OPTIONS = ["debug", "info", "warning", "error"]
MCP_WORKER_POLICY_OPTIONS = ["lazy", "sticky", "always_on"]
CONCURRENCY_MODE_OPTIONS = ["per_profile_live", "block"]
LANGUAGE_OPTIONS = load_language_options()
I18N = load_translations()
WINDOW_STATE_SAVE_DELAY_MS = 400


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
        self.pending_ui_refresh_flags: Dict[str, bool] = {}
        self.keepalive_icon_refresh_scheduled = False

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

        self.ui_refresh_timer = QTimer(self)
        self.ui_refresh_timer.setSingleShot(True)
        self.ui_refresh_timer.timeout.connect(self.flush_pending_ui_refresh)

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
        mcp_status_text = self.mcp_status_label.text() if hasattr(self, "mcp_status_label") else ""
        self.bottom_stats_label.setText(
            build_bottom_stats_text(
                self.config,
                self.profile_occupancy_cache,
                self.mcp_startup_in_progress,
                self.mcp_status_cache,
                mcp_status_text,
                self.tr,
            )
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

    def request_ui_refresh(
        self,
        *,
        table: bool = False,
        selected_status: bool = False,
        bottom_stats: bool = False,
        occupancy_tab: bool = False,
        scheduler_status: bool = False,
        mcp_status: bool = False,
        delay_ms: int = UI_REFRESH_DEBOUNCE_MS,
    ) -> None:
        if not hasattr(self, "ui_refresh_timer") or self.ui_refresh_timer is None:
            if mcp_status:
                self.refresh_mcp_status_ui()
            if scheduler_status:
                self.refresh_scheduler_status()
            if table:
                self.refresh_table()
            if selected_status:
                self.update_selected_profile_status()
            if bottom_stats:
                self.refresh_bottom_stats()
            if occupancy_tab:
                self.refresh_occupancy_tab()
            return
        if table:
            self.pending_ui_refresh_flags["table"] = True
        if selected_status:
            self.pending_ui_refresh_flags["selected_status"] = True
        if bottom_stats:
            self.pending_ui_refresh_flags["bottom_stats"] = True
        if occupancy_tab:
            self.pending_ui_refresh_flags["occupancy_tab"] = True
        if scheduler_status:
            self.pending_ui_refresh_flags["scheduler_status"] = True
        if mcp_status:
            self.pending_ui_refresh_flags["mcp_status"] = True
        if not self.ui_refresh_timer.isActive():
            self.ui_refresh_timer.start(max(0, int(delay_ms)))

    def schedule_table_refresh(self, delay_ms: int = UI_REFRESH_DEBOUNCE_MS) -> None:
        if not hasattr(self, "ui_refresh_timer") or self.ui_refresh_timer is None:
            self.refresh_table()
            return
        self.request_ui_refresh(table=True, delay_ms=delay_ms)

    def flush_pending_ui_refresh(self) -> None:
        flags = dict(self.pending_ui_refresh_flags)
        self.pending_ui_refresh_flags = {}
        if not flags:
            return
        if flags.get("mcp_status"):
            self.refresh_mcp_status_ui()
            flags["bottom_stats"] = False
        if flags.get("scheduler_status"):
            self.refresh_scheduler_status()
        if flags.get("table"):
            self.refresh_table()
        if flags.get("selected_status"):
            self.update_selected_profile_status()
        if flags.get("bottom_stats"):
            self.refresh_bottom_stats()
        if flags.get("occupancy_tab"):
            self.refresh_occupancy_tab()

    def clear_logs(self):
        self.pending_log_lines = []
        self.log_output.clear()

    def clear_mcp_logs(self):
        self.pending_mcp_log_lines = []
        self.mcp_log_output.clear()

    def on_keepalive_worker_log(self, prefix: str, message: str):
        self.append_log(message, prefix=prefix)

    def on_keepalive_worker_message(self, kind: str, payload: Dict):
        plan = build_keepalive_worker_message_plan(
            kind=kind,
            payload=payload or {},
            keepalive_log_prefix=self.keepalive_log_prefix,
            default_source_label=self.tr("keepalive_source_default"),
            engine_name=normalize_browser_engine_name(
                self.config.get("app", {}).get("browser_engine", DEFAULT_BROWSER_ENGINE)
            ),
            now_date_text=datetime.datetime.now().strftime("%Y-%m-%d"),
            trf=self.trf,
        )
        if kind == "__PROFILE_START__":
            self.keepalive_running_profile_name = str(plan.get("profile_name", "") or "")
        if str(plan.get("summary_log", "")).strip():
            self.append_log(str(plan["summary_log"]), prefix=str(plan["log_prefix"]))
        if str(plan.get("error_log", "")).strip():
            self.append_log(str(plan["error_log"]), prefix=str(plan["log_prefix"]))
        if bool(plan.get("write_occupancy")):
            occupancy_payload = dict(plan.get("occupancy_payload", {}) or {})
            _write_gui_profile_occupancy(
                str(occupancy_payload.get("profile_name", "") or ""),
                scene_type=str(occupancy_payload.get("scene_type", "keepalive") or "keepalive"),
                state=str(occupancy_payload.get("state", "active") or "active"),
                owner_label=str(occupancy_payload.get("owner_label", "") or ""),
                engine_name=str(occupancy_payload.get("engine_name", "") or ""),
            )
        if bool(plan.get("mark_scheduled_date")):
            self.config = load_app_config(self.config_path)
            self.config["keepalive"]["last_scheduled_run_date"] = str(plan.get("scheduled_date", ""))
            self.config = save_app_config(self.config, self.config_path)
        if bool(plan.get("clear_occupancy")) and self.keepalive_running_profile_name:
            _clear_gui_profile_occupancy(self.keepalive_running_profile_name)
        if bool(plan.get("reset_keepalive_runtime")):
            self.keepalive_worker = None
            self.keepalive_target_profiles = []
            self.keepalive_running_profile_name = ""
            self.keepalive_log_prefix = ""
            self.keepalive_stop_requested = False
        if bool(plan.get("enable_keepalive_buttons")):
            self.set_keepalive_buttons_enabled(True)
        if bool(plan.get("load_occupancy_cache")):
            self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        if bool(plan.get("reload_config")):
            self.reload_config_from_disk()
            return
        if bool(plan.get("request_ui_refresh")):
            self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, occupancy_tab=True)

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
        self.request_ui_refresh(table=True)

    def build_external_profile_process_signature(self) -> str:
        process_map = self.build_external_profile_process_map()
        return self.serialize_external_profile_process_map(process_map)

    def build_external_profile_process_map(self) -> Dict[str, List[int]]:
        raw_map = get_chromium_process_map_by_profile(self.config)
        return build_external_profile_process_map(self.config, raw_map)

    def serialize_external_profile_process_map(self, process_map: Dict[str, List[int]]) -> str:
        return serialize_external_profile_process_map(process_map)

    def refresh_external_profile_process_state(self) -> None:
        process_map = self.build_external_profile_process_map()
        previous_map = dict(self.external_profile_process_map)
        plan = build_external_process_refresh_plan(
            previous_map=previous_map,
            current_map=process_map,
            occupancy_cache=self.profile_occupancy_cache,
            tr=self.tr,
        )
        if not bool(plan["signature_changed"]):
            self.reconcile_manual_occupancy(process_map)
            return
        self.external_profile_process_signature = str(plan["signature"])
        self.external_profile_process_map = process_map
        for message in plan["transition_messages"]:
            self.append_log(message)
        self.reconcile_manual_occupancy(process_map)
        if bool(plan["needs_ui_refresh"]):
            self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, occupancy_tab=True)

    def build_profile_runtime_state_text(self, profile_name: str) -> str:
        return build_profile_runtime_state_text(
            profile_name,
            self.profile_occupancy_cache,
            self.external_profile_process_map,
            self.is_profile_keepalive_running,
            self.tr,
        )

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
        self.refresh_occupancy_tab()
        self.load_keepalive_settings_to_ui()
        self.load_path_settings_to_ui()
        self.load_mcp_settings_to_ui()
        self.refresh_app_auto_start_checkbox()
        self.refresh_close_to_tray_checkbox()
        self.refresh_scheduler_status()
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()
        self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, delay_ms=0)

    def reload_config_from_disk(self):
        self.refresh_all()
        self.append_log(self.tr("log_config_reloaded"))

    def load_profile_occupancy_cache(self) -> Dict[str, Dict]:
        from chromium_advanced.session_manager import SessionManager

        def _list_profile_occupancy(_config_path: str) -> Dict[str, Dict]:
            manager = SessionManager(config_path=_config_path)
            return manager.list_profile_occupancy(tolerate_lock_timeout=True)

        return load_profile_occupancy_cache_helper(
            self.config_path,
            _list_profile_occupancy,
            lambda message: self.append_log(message, prefix="GUI"),
        )

    def format_scene_type_label(self, scene_type: str) -> str:
        return format_scene_type_label(scene_type, self.tr)

    def format_occupancy_entry_summary(self, profile_name: str, occupancy: Dict) -> str:
        return format_occupancy_entry_summary(
            profile_name,
            occupancy,
            self.tr,
            occupancy_entry_is_expired(occupancy),
        )

    def reconcile_manual_occupancy(self, process_map: Optional[Dict[str, List[int]]] = None) -> None:
        process_map = process_map if isinstance(process_map, dict) else self.external_profile_process_map
        stale_profiles = collect_stale_manual_occupancy_profiles(self.profile_occupancy_cache, process_map)
        if not stale_profiles:
            return
        for profile_name in stale_profiles:
            _clear_gui_profile_occupancy(profile_name)
            self.append_log(
                self.tr(
                    "log_profile_manual_occupancy_released",
                    "{profile_name} manual occupancy was cleared because no matching Chromium process remained.",
                ).format(profile_name=profile_name)
            )
        self.profile_occupancy_cache = self.load_profile_occupancy_cache()

    def format_occupancy_event_text(self, payload: Dict) -> str:
        return format_occupancy_event_text(payload)

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
            self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, occupancy_tab=True)

    def refresh_occupancy_tab(self):
        entries = self.load_profile_occupancy_cache()
        recent_events = read_recent_jsonl_events(get_occupancy_events_path(), limit=80)
        payload = build_occupancy_tab_payload(
            entries,
            recent_events,
            self.tr,
            profile_sort_key,
            occupancy_entry_is_expired,
        )
        self.occupancy_summary.setText(payload["summary_text"])
        self.occupancy_output.setPlainText(payload["body_text"])

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
        return build_profile_status_display(
            profile_name,
            self.profile_occupancy_cache,
            self.external_profile_process_map,
            self.is_profile_keepalive_running,
            self.tr,
        )

    def sync_profiles(self):
        self.config = sync_profiles_with_user_data(self.config)
        self.config = save_app_config(self.config, self.config_path)
        self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, delay_ms=0)
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
        self.selected_profile_status.setPlainText(
            build_selected_profile_status_text(
                profile,
                self.selected_profile_name,
                self.config.get("profiles", []),
                build_profile_detail_text,
                self.profile_occupancy_cache,
                self.external_profile_process_map,
                self.is_profile_keepalive_running,
                self.tr,
            )
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
        color_hex = get_profile_status_color_hex(profile)
        return QColor(color_hex) if color_hex else None

    def create_profile_site_selector(self, profile: Dict) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        payloads = build_profile_site_selector_payloads(
            profile=profile,
            keepalive_site_ids=get_keepalive_site_ids(self.config),
            keepalive_running_globally=self.keepalive_worker is not None,
            get_site_label=lambda site_name: self.tr(f"site_name_{site_name}", get_keepalive_site_label(site_name, self.config)),
            get_site_icon_path=lambda site_name: get_keepalive_site_icon_path(site_name, self.config, fetch=False),
            site_checkbox_tooltip=self.tr("site_checkbox_tooltip"),
            format_keepalive_site_status=format_keepalive_site_status,
            normalize_keepalive_site_result_for_display=normalize_keepalive_site_result_for_display,
            tr=self.tr,
        )
        if not payloads:
            empty_label = QLabel("-")
            empty_label.setStyleSheet("color: #757575;")
            layout.addWidget(empty_label)
            layout.addStretch()
            return wrapper

        for badge_state in payloads:
            site_name = str(badge_state["site_name"])
            icon_path = str(badge_state["icon_path"])
            checkbox = QCheckBox(str(badge_state["text"]))
            if icon_path:
                checkbox.setIcon(QIcon(icon_path))
                checkbox.setIconSize(QSize(16, 16))
            checkbox.setChecked(bool(badge_state["checked"]))
            checkbox.setEnabled(bool(badge_state["enabled"]))
            checkbox.setToolTip(str(badge_state["tooltip"]))
            checkbox.setStyleSheet(str(badge_state["style"]))
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
        if not self.keepalive_icon_refresh_scheduled:
            self.keepalive_icon_refresh_scheduled = True
            QTimer.singleShot(4000, self._on_keepalive_icon_cache_refreshed)

    def _on_keepalive_icon_cache_refreshed(self):
        self.keepalive_icon_refresh_scheduled = False
        self.schedule_table_refresh()

    def refresh_keepalive_plugin_table(self):
        self.keepalive_plugin_records = get_keepalive_plugin_records(self.config)
        view_model = build_keepalive_plugin_table_view_model(
            self.keepalive_plugin_records,
            self.selected_plugin_site_id,
            self.tr,
        )
        rows = view_model["rows"]
        self.plugin_table.blockSignals(True)
        self.plugin_table.setRowCount(len(self.keepalive_plugin_records))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if column != 1:
                    item.setTextAlignment(Qt.AlignCenter if column == 2 else Qt.AlignLeft | Qt.AlignVCenter)
                self.plugin_table.setItem(row, column, item)
        self.plugin_table.blockSignals(False)

        if bool(view_model["has_records"]):
            self.plugin_table.selectRow(int(view_model["selected_row"]))
            self.on_keepalive_plugin_selection_changed()
        else:
            empty_selection = dict(view_model["empty_selection"])
            self.selected_plugin_site_id = str(empty_selection.get("selected_plugin_site_id", "") or "")
            self.plugin_source_editor.setPlainText("")
            self.plugin_source_editor.setReadOnly(True)
            self.btn_plugin_save.setEnabled(False)
            self.btn_plugin_delete.setEnabled(False)
            self.plugin_detail_site_id.setText(str(empty_selection.get("detail_site_id", "-")))
            self.plugin_detail_display_name.setText(str(empty_selection.get("detail_display_name", "-")))
            self.plugin_detail_type.setText(str(empty_selection.get("detail_type", "-")))
            self.plugin_detail_source.setText(str(empty_selection.get("detail_source", "-")))
            self.plugin_detail_home_url.setText(str(empty_selection.get("detail_home_url", "-")))
            self.plugin_detail_icon_url.setText(str(empty_selection.get("detail_icon_url", "-")))
            self.plugin_source_status.setText(str(empty_selection.get("base_status", self.tr("plugin_status_empty"))))

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
        view_model = build_keepalive_plugin_selection_view_model(
            record,
            keepalive_worker_present=self.keepalive_worker is not None,
            tr=self.tr,
        )
        self.selected_plugin_site_id = str(view_model.get("selected_plugin_site_id", "") or "")
        if not record:
            self.plugin_source_editor.setPlainText("")
            self.plugin_source_editor.setReadOnly(True)
            self.btn_plugin_save.setEnabled(False)
            self.btn_plugin_delete.setEnabled(False)
            self.plugin_detail_site_id.setText(str(view_model["detail_site_id"]))
            self.plugin_detail_display_name.setText(str(view_model["detail_display_name"]))
            self.plugin_detail_type.setText(str(view_model["detail_type"]))
            self.plugin_detail_source.setText(str(view_model["detail_source"]))
            self.plugin_detail_home_url.setText(str(view_model["detail_home_url"]))
            self.plugin_detail_icon_url.setText(str(view_model["detail_icon_url"]))
            self.plugin_source_status.setText(str(view_model["base_status"]))
            return
        self.plugin_detail_site_id.setText(str(view_model["detail_site_id"]))
        self.plugin_detail_display_name.setText(str(view_model["detail_display_name"]))
        self.plugin_detail_type.setText(str(view_model["detail_type"]))
        self.plugin_detail_source.setText(str(view_model["detail_source"]))
        self.plugin_detail_home_url.setText(str(view_model["detail_home_url"]))
        self.plugin_detail_icon_url.setText(str(view_model["detail_icon_url"]))
        self.plugin_source_editor.setReadOnly(not bool(view_model["allow_edit"]))
        self.btn_plugin_save.setEnabled(bool(view_model["allow_edit"]))
        self.btn_plugin_delete.setEnabled(bool(view_model["allow_edit"]))
        self.plugin_source_status.setText(self.tr("plugin_status_loading"))
        try:
            source_text = get_keepalive_plugin_source_text(self.selected_plugin_site_id, self.config)
            self.plugin_source_editor.setPlainText(source_text)
            load_error = str(record.get("load_error", "") or "").strip()
            self.plugin_source_status.setText(
                self.trf("plugin_status_loaded_with_error", status=view_model["base_status"], error=load_error)
                if load_error
                else str(view_model["base_status"])
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()

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
        self.schedule_table_refresh()
        self.append_log(self.trf("plugin_log_deleted", site_id=site_id, path=path))

    def open_keepalive_plugin_dir(self):
        plugin_root = get_keepalive_plugin_root()
        try:
            os.startfile(plugin_root)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("error_open_failed_title"), self.trf("plugin_error_open_dir", error=exc))

    def refresh_table(self):
        profiles = sorted(self.config.get("profiles", []), key=lambda item: profile_sort_key(item.get("profile_name", "")))
        view_model = build_profile_table_view_model(
            profiles,
            selected_profile_name=self.selected_profile_name,
            get_status_payload=self.get_profile_status_display,
            build_action_state=lambda profile_name: build_profile_row_action_state(
                profile_name,
                self.external_profile_process_map,
                self.is_profile_keepalive_running,
                self.is_profile_keepalive_ui_locked,
                self.keepalive_worker is not None,
                self.tr,
            ),
            keepalive_running_globally=self.keepalive_worker is not None,
            tr=self.tr,
        )
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for row, row_entry in enumerate(view_model["rows"]):
                profile = profiles[row]
                row_payload = dict(row_entry["row_payload"])
                profile_name = str(row_entry["profile_name"])
                self.table.insertRow(row)
                profile_item = QTableWidgetItem(str(row_payload["profile_name"]))
                account_item = QTableWidgetItem(str(row_payload["account_text"]))
                status_item = QTableWidgetItem(str(row_payload["status_text"]))
                color = self.status_color_for_profile(profile)
                if color:
                    profile_item.setForeground(color)
                    account_item.setForeground(color)
                    status_item.setForeground(color)
                tooltip = str(row_payload["row_tooltip"])
                profile_item.setToolTip(tooltip)
                account_item.setToolTip(tooltip)
                status_item.setToolTip(str(row_payload["status_tooltip"]))
                self.table.setItem(row, 0, profile_item)
                self.table.setItem(row, 1, account_item)
                self.table.setItem(row, 2, status_item)
                self.table.setCellWidget(row, 3, self.create_profile_site_selector(profile))

                keepalive_wrapper, _keepalive_checkbox = create_centered_checkbox_widget(
                    checked=bool(row_payload["keepalive_checked"]),
                    enabled=bool(row_payload["keepalive_enabled"]),
                    on_state_changed=lambda state, name=profile_name: self.set_profile_keepalive_enabled(
                        name, state == Qt.Checked
                    ),
                )
                self.table.setCellWidget(row, 4, keepalive_wrapper)

                button_wrapper, _launch_button, _keepalive_button = create_profile_action_buttons_widget(
                    launch_text=str(row_payload["launch_text"]),
                    launch_enabled=bool(row_payload["launch_enabled"]),
                    launch_tooltip=str(row_payload["launch_tooltip"]),
                    on_launch_clicked=lambda _=False, name=profile_name: self.toggle_profile_launch_by_name(name),
                    keepalive_text=str(row_payload["keepalive_button_text"]),
                    keepalive_enabled=bool(row_payload["keepalive_button_enabled"]),
                    keepalive_tooltip=str(row_payload["keepalive_button_tooltip"]),
                    keepalive_style=str(row_payload["keepalive_button_style"]),
                    on_keepalive_clicked=lambda _=False, name=profile_name: self.run_keepalive_for_profile(name),
                )
                self.table.setCellWidget(row, 5, button_wrapper)

            if bool(view_model["has_profiles"]):
                self.table.selectRow(int(view_model["selected_row"]))
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()

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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        self.schedule_table_refresh()
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
        warning_state = build_mcp_auth_warning_state(
            self.mcp_host_edit.text().strip() or "127.0.0.1",
            api_token,
            self.tr,
        )
        self.mcp_auth_warning_label.setText(warning_state["text"])
        self.mcp_auth_warning_label.setVisible(bool(warning_state["visible"]))

    def save_mcp_settings(self):
        self.config.setdefault("mcp", {})
        self.config["mcp"]["transport"] = self.mcp_transport_combo.currentText().strip() or MCP_TRANSPORT_OPTIONS[0]
        self.config["mcp"]["host"] = self.mcp_host_edit.text().strip() or "127.0.0.1"
        self.config["mcp"]["path"] = normalize_mcp_path(self.mcp_path_edit.text().strip() or "/mcp")
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
        return build_mcp_endpoint(self.config.get("mcp", {}), worker=False)

    def get_mcp_worker_endpoint(self) -> str:
        return build_mcp_endpoint(self.config.get("mcp", {}), worker=True)

    def get_mcp_trace_path(self) -> str:
        return get_mcp_trace_path_helper()

    def get_mcp_status_url(self) -> str:
        return build_mcp_status_url(self.config.get("mcp", {}))

    def get_mcp_auth_headers(self) -> Dict[str, str]:
        settings = self.config.get("mcp", {}) if isinstance(self.config, dict) else {}
        return build_mcp_auth_headers(settings, admin=False)

    def get_mcp_admin_auth_headers(self) -> Dict[str, str]:
        settings = self.config.get("mcp", {}) if isinstance(self.config, dict) else {}
        return build_mcp_auth_headers(settings, admin=True)

    def get_mcp_connect_host_port(self):
        return resolve_mcp_connect_host_port(self.config.get("mcp", {}))

    def is_mcp_expected_enabled(self) -> bool:
        return bool(self.config.get("mcp", {}).get("enabled", False))

    def query_mcp_status(
        self,
        force: bool = False,
        expected_pid: int = 0,
        expected_instance_id: str = "",
    ) -> Dict:
        now_ts = time.monotonic()
        result = query_mcp_status_snapshot(
            force=force,
            now_ts=now_ts,
            cache=self.mcp_status_cache,
            last_query_at=self.mcp_status_last_query_at,
            last_ok_at=self.mcp_status_last_ok_at,
            consecutive_failures=self.mcp_status_consecutive_failures,
            cache_ttl_seconds=MCP_STATUS_CACHE_TTL_SECONDS,
            recent_health_grace_seconds=MCP_RECENT_HEALTH_GRACE_SECONDS,
            fetch_status=lambda: fetch_json(
                self.get_mcp_status_url(),
                timeout=MCP_STATUS_QUERY_TIMEOUT_SECONDS,
                headers=self.get_mcp_auth_headers(),
            ),
            expected_pid=expected_pid,
            expected_instance_id=expected_instance_id,
        )
        self.mcp_status_cache = result["cache"]
        self.mcp_status_last_query_at = float(result["last_query_at"])
        self.mcp_status_last_ok_at = float(result["last_ok_at"])
        self.mcp_status_consecutive_failures = int(result["consecutive_failures"])
        return result["status"]

    def refresh_mcp_status_ui(self):
        self.mcp_endpoint_label.setText(self.get_mcp_endpoint())
        self.mcp_worker_endpoint_label.setText(self.get_mcp_worker_endpoint())
        self.mcp_trace_path_label.setText(self.get_mcp_trace_path())
        self.mcp_default_engine_label.setText(
            f"{normalize_browser_engine_name(self.config.get('app', {}).get('browser_engine', DEFAULT_BROWSER_ENGINE))}"
            f" / {str(self.config.get('app', {}).get('concurrency_mode', 'per_profile_live') or 'per_profile_live')}"
        )

        daemon_status = self.query_mcp_status()
        view_model = build_mcp_status_view_model(
            daemon_status,
            self.is_mcp_expected_enabled(),
            self.mcp_startup_in_progress,
            self.tr,
        )
        self.mcp_status_label.setText(view_model["label"])
        self.mcp_status_detail_label.setText(view_model["detail"])
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
        self.request_ui_refresh(mcp_status=True)

    def check_mcp_health_after_start(self, startup_token: int):
        if startup_token != self.mcp_startup_token or not self.mcp_startup_in_progress:
            return
        try:
            status = self.query_mcp_status(force=True, expected_pid=self.mcp_launch_pid)
            if status:
                self.mcp_startup_in_progress = False
                self.mcp_startup_deadline = None
                self.request_ui_refresh(mcp_status=True)
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
        return build_mcp_process_arguments_helper(
            settings,
            self.config_path,
            MCP_TRANSPORT_OPTIONS,
            bool(getattr(sys, "frozen", False)),
        )

    def start_mcp_service(self):
        self.save_mcp_settings()
        if self.mcp_startup_in_progress:
            self.request_ui_refresh(mcp_status=True)
            return
        if self.query_mcp_status(force=True):
            self.mcp_owned_process = True
            self.request_ui_refresh(mcp_status=True)
            return

        terminated_pids = terminate_project_mcp_processes(exclude_pid=os.getpid())
        plan = build_mcp_startup_plan(
            terminated_pids=terminated_pids,
            startup_timeout_ms=MCP_HEALTHCHECK_START_TIMEOUT_MS,
            tr=self.tr,
            trf=self.trf,
            now_dt=datetime.datetime.now(),
        )
        if str(plan.get("cleanup_log", "")).strip():
            self.append_mcp_log(str(plan["cleanup_log"]), prefix=str(plan.get("log_prefix", "MCP")))

        self.mcp_restart_pending = bool(plan.get("set_restart_pending", False))
        self.mcp_stop_requested = bool(plan.get("set_stop_requested", False))
        self.mcp_owned_process = bool(plan.get("set_owned_process", True))
        self.mcp_startup_in_progress = bool(plan.get("set_startup_in_progress", True))
        self.mcp_startup_token += 1
        if bool(plan.get("reset_consecutive_failures")):
            self.mcp_status_consecutive_failures = 0
        self.mcp_startup_deadline = plan.get("startup_deadline")
        self.append_mcp_log(str(plan.get("prepare_log", "")))
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
            failure_plan = build_mcp_startup_failure_plan(error=exc, trf=self.trf)
            self.mcp_startup_in_progress = bool(failure_plan.get("startup_in_progress", False))
            self.mcp_startup_deadline = failure_plan.get("startup_deadline")
            self.mcp_owned_process = bool(failure_plan.get("owned_process", False))
            self.append_mcp_log(
                str(failure_plan.get("error_log", "")),
                prefix=str(failure_plan.get("error_prefix", "MCP-ERR")),
            )
            if bool(failure_plan.get("request_status_refresh", True)):
                self.request_ui_refresh(mcp_status=True)
            return
        self.request_ui_refresh(mcp_status=True)
        QTimer.singleShot(0, lambda token=self.mcp_startup_token: self.check_mcp_health_after_start(token))

    def stop_mcp_service(self, update_checkbox: bool = True):
        plan = build_mcp_stop_plan(update_checkbox=update_checkbox, tr=self.tr)
        self.mcp_startup_in_progress = bool(plan.get("startup_in_progress", False))
        self.mcp_startup_deadline = plan.get("startup_deadline")
        if bool(plan.get("increment_startup_token", True)):
            self.mcp_startup_token += 1
        if bool(plan.get("reset_consecutive_failures", True)):
            self.mcp_status_consecutive_failures = 0
        self.mcp_restart_pending = bool(plan.get("restart_pending", False))
        self.mcp_stop_requested = bool(plan.get("stop_requested", True))
        self.append_mcp_log(str(plan.get("prepare_log", "")))
        if bool(plan.get("cleanup_residue", True)):
            self.cleanup_mcp_process_residue()
        self.mcp_launch_pid = int(plan.get("launch_pid", 0) or 0)
        if bool(plan.get("update_checkbox", False)):
            self.mcp_service_checkbox.blockSignals(True)
            self.mcp_service_checkbox.setChecked(False)
            self.mcp_service_checkbox.blockSignals(False)
        self.mcp_owned_process = bool(plan.get("owned_process", False))
        if bool(plan.get("clear_status_cache", True)):
            self.mcp_status_cache = {}
        if bool(plan.get("request_status_refresh", True)):
            self.request_ui_refresh(mcp_status=True)

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
        self.request_ui_refresh(mcp_status=True)

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
        self.request_ui_refresh(mcp_status=True)
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
            self.request_ui_refresh(table=True, selected_status=True, bottom_stats=True)

        enabled_profiles = self.get_enabled_keepalive_profiles()
        now_dt = datetime.datetime.now()
        schedule_dt = schedule_time_to_datetime(keepalive.get("schedule_time", "09:00"), now_dt)
        today_text = now_dt.strftime("%Y-%m-%d")
        last_scheduled_date = str(keepalive.get("last_scheduled_run_date", "")).strip()
        view_model = build_keepalive_schedule_view_model(
            enabled_profile_count=len(enabled_profiles),
            now_dt=now_dt,
            schedule_dt=schedule_dt,
            last_scheduled_date=last_scheduled_date,
            keepalive_running=bool(
                self.keepalive_worker is not None and getattr(self.keepalive_worker, "source", "").startswith("internal-schedule")
            ),
            triggered_today_text=today_text,
            format_datetime=format_datetime_for_ui,
            should_trigger_schedule=should_trigger_keepalive_schedule,
            tr=self.tr,
        )
        self.global_task_status.setText(view_model["status"])
        self.global_task_next_run.setText(view_model["next_run"])
        self.global_task_last_result.setText(view_model["last_result"])

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
        last_scheduled_date = str(keepalive.get("last_scheduled_run_date", "")).strip()
        if not should_trigger_keepalive_schedule(now_dt, schedule_dt, last_scheduled_date):
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
