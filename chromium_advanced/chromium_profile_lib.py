import copy
import datetime
import glob
import json
import locale
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import quote_plus

import psutil
import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from chromium_advanced.browser_engines.constants import DEFAULT_BROWSER_ENGINE
from chromium_advanced.browser_engines.factory import normalize_browser_engine_name


APP_NAME = "ChromiumProfileManager"
CONFIG_FILENAME = "chromium_profiles.json"
LOCK_FILENAME = "chromium_keepalive.lock"
MIRROR_LOCK_FILENAME = "chromium_mirroring.lock"
WINDOWS_TEXT_ENCODING = locale.getpreferredencoding(False) or "utf-8"
BOOKMARK_BAR_FOLDER_NAMES = {"书签栏", "Bookmarks Bar", "Bookmarks bar", "Bookmarks Toolbar"}
PROFILE_MARKER_URL = "https://www.google.com/generate_204"
LEGACY_CHATGPT_PROMPT = "Reply with one word: alive"
SYSTEM_NAME = platform.system()
KEEPALIVE_SITE_ORDER = ("chatgpt", "gmail", "google", "github")
DEFAULT_CHATGPT_PROMPTS = [
    "I just got back. Reply with a short greeting.",
    "I am checking in briefly. Reply with one short sentence.",
    "I reopened this conversation. A quick reply is enough.",
]
BOOKMARK_ROOT_BAR_NAME = "Bookmarks bar"
BOOKMARK_ROOT_OTHER_NAME = "Other bookmarks"
BOOKMARK_ROOT_MOBILE_NAME = "Mobile bookmarks"


class BookmarkTemplateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"type": "folder", "name": "__root__", "children": []}
        self.folder_stack = [self.root]
        self.pending_folder = None
        self.capture_kind = ""
        self.capture_attrs: Dict[str, str] = {}
        self.capture_text: List[str] = []
        self.dl_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        attr_map = {str(key).lower(): str(value) for key, value in attrs if key}

        if tag == "a":
            self._flush_pending_folder(push=False)
            self.capture_kind = "url"
            self.capture_attrs = attr_map
            self.capture_text = []
            return

        if tag == "h3":
            self._flush_pending_folder(push=False)
            self.capture_kind = "folder"
            self.capture_attrs = attr_map
            self.capture_text = []
            return

        if tag == "dl":
            self.dl_depth += 1
            self._flush_pending_folder(push=True)

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        if tag == "a" and self.capture_kind == "url":
            name = "".join(self.capture_text).strip()
            url = self.capture_attrs.get("href", "").strip()
            if name and url:
                self.folder_stack[-1]["children"].append(
                    {
                        "type": "url",
                        "name": name,
                        "url": url,
                        "add_date": self.capture_attrs.get("add_date", "").strip(),
                    }
                )
            self.capture_kind = ""
            self.capture_attrs = {}
            self.capture_text = []
            return

        if tag == "h3" and self.capture_kind == "folder":
            name = "".join(self.capture_text).strip()
            if name:
                self.pending_folder = {
                    "type": "folder",
                    "name": name,
                    "children": [],
                    "add_date": self.capture_attrs.get("add_date", "").strip(),
                    "last_modified": self.capture_attrs.get("last_modified", "").strip(),
                    "personal_toolbar_folder": self.capture_attrs.get("personal_toolbar_folder", "").strip(),
                }
            self.capture_kind = ""
            self.capture_attrs = {}
            self.capture_text = []
            return

        if tag == "dl":
            self._flush_pending_folder(push=False)
            if self.dl_depth > 1 and len(self.folder_stack) > 1:
                self.folder_stack.pop()
            if self.dl_depth > 0:
                self.dl_depth -= 1

    def handle_data(self, data: str):
        if self.capture_kind:
            self.capture_text.append(data)

    def close(self):
        super().close()
        self._flush_pending_folder(push=False)

    def _flush_pending_folder(self, push: bool):
        if not self.pending_folder:
            return
        folder = self.pending_folder
        self.pending_folder = None
        self.folder_stack[-1]["children"].append(folder)
        if push:
            self.folder_stack.append(folder)


def get_hidden_subprocess_kwargs() -> Dict:
    if os.name != "nt":
        return {}

    kwargs: Dict = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs

def get_user_home_dir() -> str:
    return os.path.expanduser("~")


def get_platform_config_root() -> str:
    if SYSTEM_NAME == "Windows":
        return os.environ.get("APPDATA") or get_user_home_dir()
    if SYSTEM_NAME == "Darwin":
        return os.path.join(get_user_home_dir(), "Library", "Application Support")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(get_user_home_dir(), ".config")


def get_default_workspace_root() -> str:
    return os.path.join(get_user_home_dir(), ".chromium-profile-manager")


def get_builtin_resource_path(*parts: str) -> str:
    return os.path.join(get_project_root(), "resources", *parts)


def ensure_default_bookmarks_template(workspace_root: str) -> str:
    target_path = os.path.join(workspace_root, "bookmarks_template.html")
    source_path = get_builtin_resource_path("bookmarks_template.html")
    if os.path.exists(target_path) or not os.path.exists(source_path):
        return target_path
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copyfile(source_path, target_path)
    return target_path


def get_default_mirror_user_data_root(user_data_root: str = "") -> str:
    user_data_root_text = str(user_data_root or "").strip()
    if user_data_root_text:
        expanded_user_data_root = os.path.abspath(os.path.expanduser(user_data_root_text))
        return os.path.join(os.path.dirname(expanded_user_data_root), "temp_user_data")
    return os.path.join(get_default_workspace_root(), "temp_user_data")


def is_legacy_default_mirror_root(mirror_user_data_root: str) -> bool:
    mirror_root_text = str(mirror_user_data_root or "").strip()
    if not mirror_root_text:
        return False
    legacy_default = os.path.abspath(os.path.expanduser(os.path.join(get_default_workspace_root(), "temp_user_data")))
    candidate = os.path.abspath(os.path.expanduser(mirror_root_text))
    return candidate == legacy_default


def get_default_path_config() -> Dict[str, str]:
    workspace_root = get_default_workspace_root()
    driver_name = "chromedriver.exe" if SYSTEM_NAME == "Windows" else "chromedriver"
    bookmarks_template_path = ensure_default_bookmarks_template(workspace_root)

    # Ship platform-specific hints instead of machine-specific real paths.
    if SYSTEM_NAME == "Windows":
        chromium_hint = os.path.join(workspace_root, "chromium", "chrome.exe")
    elif SYSTEM_NAME == "Darwin":
        chromium_hint = "/Applications/Chromium.app/Contents/MacOS/Chromium"
    else:
        chromium_hint = shutil.which("chromium") or shutil.which("chromium-browser") or "/usr/bin/chromium"

    return {
        "chromium_dir": chromium_hint,
        "chromedriver_path": os.path.join(workspace_root, "drivers", driver_name),
        "user_data_root": os.path.join(workspace_root, "user-data"),
        "mirror_user_data_root": get_default_mirror_user_data_root(os.path.join(workspace_root, "user-data")),
        "bookmarks_template_path": bookmarks_template_path,
        "fingerprint_zip_path": os.path.join(workspace_root, "extensions", "fingerprint-extension.zip"),
    }


def detect_default_language() -> str:
    candidates = [
        locale.getdefaultlocale()[0] if locale.getdefaultlocale() else "",
        locale.getlocale()[0] if locale.getlocale() else "",
        os.environ.get("LANG", ""),
    ]
    for candidate in candidates:
        text = str(candidate or "").lower()
        if text.startswith("zh"):
            return "zh"
        if text.startswith("ja"):
            return "ja"
    return "en"


def normalize_language_code(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("zh"):
        return "zh"
    if text.startswith("ja") or text.startswith("jp"):
        return "ja"
    return "en"


def build_default_config() -> Dict:
    return {
        "version": 1,
        "paths": get_default_path_config(),
        "app": {
            "minimize_to_tray_on_close": True,
            "language": detect_default_language(),
            "browser_engine": DEFAULT_BROWSER_ENGINE,
            "concurrency_mode": "block",
            "window_bounds": {
                "x": -1,
                "y": -1,
                "width": 860,
                "height": 680,
            },
        },
        "mcp": {
            "enabled": True,
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 28888,
            "worker_port": 28889,
            "path": "/mcp",
            "log_level": "info",
            "idle_timeout_seconds": 60,
            "headless": False,
            "start_minimized": True,
        },
        "launch": {
            "new_window": True,
            "start_maximized": True,
            "window_size": "",
            "no_first_run": True,
            "no_default_browser_check": True,
            "disable_background_networking": True,
            "disable_default_apps": True,
            "disable_sync": True,
            "metrics_recording_only": True,
            "disable_client_side_phishing_detection": False,
            "disable_webrtc": False,
            "webrtc_ip_handling_policy": "",
            "force_webrtc_ip_handling_policy": False,
            "load_fingerprint_extension": True,
            "open_extensions_page": False,
            "check_url": "",
            "extra_args": [],
        },
        "profiles": [],
        "keepalive": {
            "enabled_sites": {
                "chatgpt": True,
                "gmail": True,
                "google": True,
                "github": False,
            },
            "schedule_time": "06:00",
            "headless": False,
            "page_timeout_seconds": 45,
            "between_profiles_seconds": 5,
            "settle_seconds": 3,
            "site_dwell_seconds": 6,
            "chatgpt_prompt": "",
            "chatgpt_conversation_hint": "",
            "google_query": "profile keepalive",
            "last_run_at": "",
            "last_run_finished_at": "",
            "last_run_status": "never",
            "last_run_message": "",
            "last_run_source": "",
            "last_run_profile_count": 0,
            "last_run_details": [],
            "last_scheduled_run_date": "",
        },
        "mirror": {
            "enabled": True,
            "disk_dir_name": "mirror_disk",
            "runtime_dir_name": "runtime",
            "cleanup_on_session_close": True,
            "max_runtime_age_hours": 24,
            "last_run_at": "",
            "last_run_finished_at": "",
            "last_run_status": "never",
            "last_run_message": "",
            "last_run_profile_count": 0,
        },
    }


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def resolve_mcp_headless(config: Dict) -> bool:
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    if isinstance(mcp, dict) and "headless" in mcp:
        return bool(mcp.get("headless"))
    env_value = str(os.environ.get("CHROMIUM_ADVANCED_MCP_HEADLESS", "") or "").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def resolve_mcp_start_minimized(config: Dict) -> bool:
    if resolve_mcp_headless(config):
        return False
    mcp = config.get("mcp", {}) if isinstance(config, dict) else {}
    if isinstance(mcp, dict) and "start_minimized" in mcp:
        return bool(mcp.get("start_minimized"))
    env_value = str(os.environ.get("CHROMIUM_ADVANCED_MCP_START_MINIMIZED", "") or "").strip().lower()
    if env_value:
        return env_value in {"1", "true", "yes", "on"}
    return True


def unique_paths(paths: Sequence[str]) -> List[str]:
    results: List[str] = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = os.path.abspath(os.path.expanduser(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def get_script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def get_project_root() -> str:
    return os.path.abspath(os.path.join(get_script_dir(), ".."))


def get_runtime_launch_cwd(executable_path: Optional[str] = None) -> str:
    candidate = str(executable_path or "").strip()
    if getattr(sys, "frozen", False):
        if candidate:
            normalized_candidate = os.path.abspath(candidate)
            if os.path.isfile(normalized_candidate):
                return os.path.dirname(normalized_candidate)
            if os.path.isdir(normalized_candidate):
                return normalized_candidate
        return os.path.dirname(os.path.abspath(sys.executable))
    return get_project_root()


def get_state_storage_dir() -> str:
    base_dir = get_platform_config_root()
    path = os.path.join(base_dir, APP_NAME, "workstates")
    os.makedirs(path, exist_ok=True)
    return path


def get_default_config_path() -> str:
    return os.path.join(get_state_storage_dir(), CONFIG_FILENAME)


def get_lock_path() -> str:
    return os.path.join(get_state_storage_dir(), LOCK_FILENAME)


def get_mirror_lock_path() -> str:
    return os.path.join(get_state_storage_dir(), MIRROR_LOCK_FILENAME)


def safe_copy(value):
    return copy.deepcopy(value)


def normalize_keepalive_site_flags(value, default: bool = False) -> Dict[str, bool]:
    flags = {site_name: bool(default) for site_name in KEEPALIVE_SITE_ORDER}
    if isinstance(value, dict):
        for site_name in KEEPALIVE_SITE_ORDER:
            if site_name in value:
                flags[site_name] = bool(value.get(site_name))
    return flags


def format_keepalive_sites_text(site_flags: Dict, translate: Optional[Callable[[str, str], str]] = None) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    labels = []
    normalized = normalize_keepalive_site_flags(site_flags, default=False)
    for site_name in KEEPALIVE_SITE_ORDER:
        if normalized.get(site_name):
            labels.append(tr(f"site_name_{site_name}", site_name.title()))
    return ", ".join(labels) if labels else "-"


def format_keepalive_site_status(
    site_name: str,
    info: Dict,
    translate: Optional[Callable[[str, str], str]] = None,
) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    payload = info if isinstance(info, dict) else {}
    status = str(payload.get("status", "unknown") or "unknown").strip().lower()
    message = str(payload.get("message", "") or "").strip()
    site_label = tr(f"site_name_{site_name}", site_name.title())
    status_label = {
        "success": tr("keepalive_site_status_success", "ok"),
        "signed_out": tr("keepalive_site_status_signed_out", "signed out"),
        "attention": tr("keepalive_site_status_attention", "attention"),
        "failed": tr("keepalive_site_status_failed", "failed"),
        "unknown": tr("keepalive_site_status_unknown", "unknown"),
    }.get(status, status)
    base = f"{site_label}: {status_label}"
    return f"{base} - {message}" if message else base


def normalize_profile_entry(entry: Dict, legacy_keepalive_sites: Optional[Dict] = None) -> Dict:
    normalized = dict(entry) if isinstance(entry, dict) else {}
    normalized["profile_name"] = str(normalized.get("profile_name", "")).strip()
    normalized["account"] = str(normalized.get("account", "")).strip()
    normalized["notes"] = str(normalized.get("notes", "")).strip()
    normalized["keepalive_enabled"] = bool(normalized.get("keepalive_enabled", False))
    if "keepalive_sites" in normalized:
        normalized["keepalive_sites"] = normalize_keepalive_site_flags(normalized.get("keepalive_sites"), default=False)
    else:
        normalized["keepalive_sites"] = normalize_keepalive_site_flags(legacy_keepalive_sites, default=False)
    normalized["last_launch_at"] = str(normalized.get("last_launch_at", "")).strip()
    normalized["last_keepalive_at"] = str(normalized.get("last_keepalive_at", "")).strip()
    normalized["last_keepalive_status"] = str(normalized.get("last_keepalive_status", "never")).strip() or "never"
    normalized["last_keepalive_message"] = str(normalized.get("last_keepalive_message", "")).strip()
    normalized["last_mirror_at"] = str(normalized.get("last_mirror_at", "")).strip()
    normalized["last_mirror_status"] = str(normalized.get("last_mirror_status", "never")).strip() or "never"
    normalized["last_mirror_message"] = str(normalized.get("last_mirror_message", "")).strip()
    details = normalized.get("last_keepalive_details", {})
    normalized["last_keepalive_details"] = details if isinstance(details, dict) else {}
    return normalized


def merge_profile_entries(existing: Dict, incoming: Dict) -> Dict:
    merged = normalize_profile_entry(existing)
    candidate = normalize_profile_entry(incoming)

    for key in (
        "account",
        "notes",
        "last_launch_at",
        "last_keepalive_at",
        "last_keepalive_message",
        "last_mirror_at",
        "last_mirror_message",
    ):
        if candidate.get(key):
            merged[key] = candidate[key]

    if candidate.get("keepalive_enabled"):
        merged["keepalive_enabled"] = True

    merged_sites = normalize_keepalive_site_flags(merged.get("keepalive_sites"), default=False)
    candidate_sites = normalize_keepalive_site_flags(candidate.get("keepalive_sites"), default=False)
    for site_name in KEEPALIVE_SITE_ORDER:
        if candidate_sites.get(site_name):
            merged_sites[site_name] = True
    merged["keepalive_sites"] = merged_sites

    if candidate.get("last_keepalive_status") and candidate.get("last_keepalive_status") != "never":
        merged["last_keepalive_status"] = candidate["last_keepalive_status"]

    if candidate.get("last_keepalive_details"):
        merged["last_keepalive_details"] = candidate["last_keepalive_details"]

    if candidate.get("last_mirror_status") and candidate.get("last_mirror_status") != "never":
        merged["last_mirror_status"] = candidate["last_mirror_status"]

    return merged


def dedupe_profile_entries(entries: List[Dict]) -> List[Dict]:
    merged_by_name: Dict[str, Dict] = {}
    ordered_names: List[str] = []

    for item in entries:
        normalized = normalize_profile_entry(item)
        profile_name = normalized.get("profile_name", "")
        if not profile_name:
            continue
        if profile_name not in merged_by_name:
            merged_by_name[profile_name] = normalized
            ordered_names.append(profile_name)
            continue
        merged_by_name[profile_name] = merge_profile_entries(merged_by_name[profile_name], normalized)

    return [merged_by_name[name] for name in ordered_names]


def profile_sort_key(profile_name: str):
    match = re.match(r"^Profile\s+(\d+)$", profile_name or "")
    if match:
        return (0, int(match.group(1)))
    return (1, profile_name or "")


def sort_profiles(entries: List[Dict]) -> List[Dict]:
    return sorted(entries, key=lambda item: profile_sort_key(item.get("profile_name", "")))


def normalize_config(config: Optional[Dict]) -> Dict:
    normalized = build_default_config()
    loaded = dict(config) if isinstance(config, dict) else {}

    loaded_paths = loaded.get("paths", {})
    if isinstance(loaded_paths, dict):
        allowed_path_keys = set(normalized["paths"].keys())
        normalized["paths"].update(
            {k: str(v).strip() for k, v in loaded_paths.items() if k in allowed_path_keys and v is not None}
        )

    loaded_app = loaded.get("app", {})
    if isinstance(loaded_app, dict):
        if "minimize_to_tray_on_close" in loaded_app:
            normalized["app"]["minimize_to_tray_on_close"] = bool(loaded_app.get("minimize_to_tray_on_close"))
        if "language" in loaded_app and loaded_app.get("language") is not None:
            normalized["app"]["language"] = str(loaded_app.get("language")).strip()
        if "browser_engine" in loaded_app and loaded_app.get("browser_engine") is not None:
            normalized["app"]["browser_engine"] = normalize_browser_engine_name(str(loaded_app.get("browser_engine")).strip())
        if "concurrency_mode" in loaded_app and loaded_app.get("concurrency_mode") is not None:
            normalized["app"]["concurrency_mode"] = str(loaded_app.get("concurrency_mode")).strip()
        window_bounds = loaded_app.get("window_bounds", {})
        if isinstance(window_bounds, dict):
            for key in ("x", "y", "width", "height"):
                if key in window_bounds:
                    try:
                        normalized["app"]["window_bounds"][key] = int(window_bounds.get(key))
                    except Exception:
                        pass

    loaded_mcp = loaded.get("mcp", {})
    if isinstance(loaded_mcp, dict):
        for key in ("transport", "host", "path", "log_level"):
            if key in loaded_mcp and loaded_mcp.get(key) is not None:
                normalized["mcp"][key] = str(loaded_mcp.get(key)).strip()
        if "enabled" in loaded_mcp:
            normalized["mcp"]["enabled"] = bool(loaded_mcp.get("enabled"))
        if "headless" in loaded_mcp:
            normalized["mcp"]["headless"] = bool(loaded_mcp.get("headless"))
        if "start_minimized" in loaded_mcp:
            normalized["mcp"]["start_minimized"] = bool(loaded_mcp.get("start_minimized"))
        if "port" in loaded_mcp:
            normalized["mcp"]["port"] = loaded_mcp.get("port")
        if "worker_port" in loaded_mcp:
            normalized["mcp"]["worker_port"] = loaded_mcp.get("worker_port")
        if "idle_timeout_seconds" in loaded_mcp:
            normalized["mcp"]["idle_timeout_seconds"] = loaded_mcp.get("idle_timeout_seconds")

    loaded_launch = loaded.get("launch", {})
    if isinstance(loaded_launch, dict):
        for key in (
            "new_window",
            "start_maximized",
            "no_first_run",
            "no_default_browser_check",
            "disable_background_networking",
            "disable_default_apps",
            "disable_sync",
            "metrics_recording_only",
            "disable_client_side_phishing_detection",
            "disable_webrtc",
            "force_webrtc_ip_handling_policy",
            "load_fingerprint_extension",
            "open_extensions_page",
        ):
            if key in loaded_launch:
                normalized["launch"][key] = bool(loaded_launch.get(key))
        for key in ("window_size", "webrtc_ip_handling_policy", "check_url"):
            if key in loaded_launch and loaded_launch.get(key) is not None:
                normalized["launch"][key] = str(loaded_launch.get(key)).strip()
        extra_args = loaded_launch.get("extra_args", [])
        if isinstance(extra_args, list):
            normalized["launch"]["extra_args"] = [str(item).strip() for item in extra_args if str(item).strip()]

    loaded_keepalive = loaded.get("keepalive", {})
    legacy_keepalive_sites = normalize_keepalive_site_flags(
        loaded_keepalive.get("enabled_sites", normalized["keepalive"]["enabled_sites"]) if isinstance(loaded_keepalive, dict) else {},
        default=False,
    )
    if isinstance(loaded_keepalive, dict):
        enabled_sites = loaded_keepalive.get("enabled_sites", {})
        if isinstance(enabled_sites, dict):
            normalized["keepalive"]["enabled_sites"].update(
                {
                    "chatgpt": bool(enabled_sites.get("chatgpt", normalized["keepalive"]["enabled_sites"]["chatgpt"])),
                    "gmail": bool(enabled_sites.get("gmail", normalized["keepalive"]["enabled_sites"]["gmail"])),
                    "google": bool(enabled_sites.get("google", normalized["keepalive"]["enabled_sites"]["google"])),
                    "github": bool(enabled_sites.get("github", normalized["keepalive"]["enabled_sites"]["github"])),
                }
            )

        for key in (
            "schedule_time",
            "headless",
            "page_timeout_seconds",
            "between_profiles_seconds",
            "settle_seconds",
            "site_dwell_seconds",
            "chatgpt_prompt",
            "chatgpt_conversation_hint",
            "google_query",
            "last_run_at",
            "last_run_finished_at",
            "last_run_status",
            "last_run_message",
            "last_run_source",
            "last_run_profile_count",
            "last_scheduled_run_date",
        ):
            if key in loaded_keepalive:
                normalized["keepalive"][key] = loaded_keepalive[key]

        details = loaded_keepalive.get("last_run_details", [])
        normalized["keepalive"]["last_run_details"] = details if isinstance(details, list) else []

    loaded_mirror = loaded.get("mirror", {})
    if isinstance(loaded_mirror, dict):
        for key in (
            "enabled",
            "cleanup_on_session_close",
            "max_runtime_age_hours",
            "last_run_at",
            "last_run_finished_at",
            "last_run_status",
            "last_run_message",
            "last_run_profile_count",
            "disk_dir_name",
            "runtime_dir_name",
        ):
            if key in loaded_mirror:
                normalized["mirror"][key] = loaded_mirror[key]

    profiles = loaded.get("profiles", [])
    if isinstance(profiles, list):
        normalized["profiles"] = dedupe_profile_entries([
            normalize_profile_entry(item, legacy_keepalive_sites)
            for item in profiles
            if isinstance(item, dict) and str(item.get("profile_name", "")).strip()
        ])

    normalized["profiles"] = sort_profiles(normalized["profiles"])
    normalized["app"]["language"] = normalize_language_code(normalized["app"].get("language", detect_default_language()))
    normalized["app"]["browser_engine"] = normalize_browser_engine_name(
        normalized["app"].get("browser_engine", DEFAULT_BROWSER_ENGINE)
    )
    concurrency_mode = str(normalized["app"].get("concurrency_mode", "block")).strip().lower()
    if concurrency_mode not in {"block", "mirror_isolated"}:
        concurrency_mode = "block"
    normalized["app"]["concurrency_mode"] = concurrency_mode
    normalized["mcp"]["enabled"] = bool(normalized["mcp"].get("enabled", False))
    normalized["mcp"]["headless"] = bool(normalized["mcp"].get("headless", False))
    normalized["mcp"]["start_minimized"] = bool(normalized["mcp"].get("start_minimized", True))
    normalized["mcp"]["transport"] = str(normalized["mcp"].get("transport", "streamable-http")).strip() or "streamable-http"
    normalized["mcp"]["host"] = str(normalized["mcp"].get("host", "127.0.0.1")).strip() or "127.0.0.1"
    normalized["mcp"]["path"] = str(normalized["mcp"].get("path", "/mcp")).strip() or "/mcp"
    if not normalized["mcp"]["path"].startswith("/"):
        normalized["mcp"]["path"] = "/" + normalized["mcp"]["path"]
    normalized["mcp"]["log_level"] = str(normalized["mcp"].get("log_level", "info")).strip() or "info"
    normalized["mcp"]["port"] = max(1, min(65535, int(normalized["mcp"].get("port", 28888))))
    normalized["mcp"]["worker_port"] = max(1, min(65535, int(normalized["mcp"].get("worker_port", 28889))))
    if normalized["mcp"]["worker_port"] == normalized["mcp"]["port"]:
        normalized["mcp"]["worker_port"] = min(65535, normalized["mcp"]["port"] + 1)
    normalized["mcp"]["idle_timeout_seconds"] = max(
        10,
        int(normalized["mcp"].get("idle_timeout_seconds", 60)),
    )
    normalized["keepalive"]["headless"] = bool(normalized["keepalive"].get("headless", False))
    normalized["keepalive"]["page_timeout_seconds"] = max(10, int(normalized["keepalive"].get("page_timeout_seconds", 45)))
    normalized["keepalive"]["between_profiles_seconds"] = max(0, int(normalized["keepalive"].get("between_profiles_seconds", 5)))
    normalized["keepalive"]["settle_seconds"] = max(0, int(normalized["keepalive"].get("settle_seconds", 3)))
    normalized["keepalive"]["site_dwell_seconds"] = max(0, int(normalized["keepalive"].get("site_dwell_seconds", 6)))
    normalized["keepalive"]["last_run_profile_count"] = max(0, int(normalized["keepalive"].get("last_run_profile_count", 0)))
    normalized["keepalive"]["last_scheduled_run_date"] = str(
        normalized["keepalive"].get("last_scheduled_run_date", "")
    ).strip()
    normalized["mirror"]["enabled"] = bool(normalized["mirror"].get("enabled", True))
    normalized["mirror"]["cleanup_on_session_close"] = bool(normalized["mirror"].get("cleanup_on_session_close", True))
    normalized["mirror"]["max_runtime_age_hours"] = max(1, int(normalized["mirror"].get("max_runtime_age_hours", 24)))
    normalized["mirror"]["disk_dir_name"] = str(normalized["mirror"].get("disk_dir_name", "mirror_disk")).strip() or "mirror_disk"
    normalized["mirror"]["runtime_dir_name"] = str(normalized["mirror"].get("runtime_dir_name", "runtime")).strip() or "runtime"
    normalized["mirror"]["last_run_at"] = str(normalized["mirror"].get("last_run_at", "")).strip()
    normalized["mirror"]["last_run_finished_at"] = str(normalized["mirror"].get("last_run_finished_at", "")).strip()
    normalized["mirror"]["last_run_status"] = str(normalized["mirror"].get("last_run_status", "never")).strip() or "never"
    normalized["mirror"]["last_run_message"] = str(normalized["mirror"].get("last_run_message", "")).strip()
    normalized["mirror"]["last_run_profile_count"] = max(0, int(normalized["mirror"].get("last_run_profile_count", 0)))
    user_data_root = str(normalized["paths"].get("user_data_root", "")).strip()
    mirror_user_data_root = str(normalized["paths"].get("mirror_user_data_root", "")).strip()
    expected_mirror_root = get_default_mirror_user_data_root(user_data_root)
    if not mirror_user_data_root or is_legacy_default_mirror_root(mirror_user_data_root):
        normalized["paths"]["mirror_user_data_root"] = expected_mirror_root
    return normalized


def write_json_atomic(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    text = json.dumps(data, indent=2, ensure_ascii=False)
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temp_path, path)


def load_app_config(config_path: Optional[str] = None) -> Dict:
    path = config_path or get_default_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
        except Exception:
            loaded = {}
    else:
        loaded = {}
    config = normalize_config(loaded)
    synced = sync_profiles_with_user_data(config)
    if loaded != synced or not os.path.exists(path):
        write_json_atomic(path, synced)
    return synced


def save_app_config(config: Dict, config_path: Optional[str] = None) -> Dict:
    path = config_path or get_default_config_path()
    normalized = normalize_config(config)
    normalized = sync_profiles_with_user_data(normalized)
    write_json_atomic(path, normalized)
    return normalized


def discover_profile_directories(user_data_root: str) -> List[str]:
    if not user_data_root or not os.path.isdir(user_data_root):
        return []

    profiles = []
    for name in os.listdir(user_data_root):
        full_path = os.path.join(user_data_root, name)
        if not os.path.isdir(full_path):
            continue
        if re.match(r"^Profile\s+\d+$", name):
            profiles.append(name)
    profiles.sort(key=profile_sort_key)
    return profiles


def sync_profiles_with_user_data(config: Dict) -> Dict:
    normalized = normalize_config(config)
    user_data_root = normalized["paths"].get("user_data_root", "")
    discovered = discover_profile_directories(user_data_root)
    existing = {item["profile_name"]: item for item in normalized["profiles"]}

    for profile_name in discovered:
        if profile_name not in existing:
            normalized["profiles"].append(
                normalize_profile_entry(
                    {
                        "profile_name": profile_name,
                        "account": "",
                        "keepalive_enabled": False,
                        "keepalive_sites": {},
                    }
                )
            )

    normalized["profiles"] = sort_profiles(normalized["profiles"])
    return normalized


def next_profile_name(config: Dict) -> str:
    highest = 0
    for item in normalize_config(config).get("profiles", []):
        match = re.match(r"^Profile\s+(\d+)$", item.get("profile_name", ""))
        if match:
            highest = max(highest, int(match.group(1)))

    user_data_root = normalize_config(config)["paths"].get("user_data_root", "")
    for profile_name in discover_profile_directories(user_data_root):
        match = re.match(r"^Profile\s+(\d+)$", profile_name)
        if match:
            highest = max(highest, int(match.group(1)))

    return f"Profile {highest + 1}"


def ensure_profile_directory(user_data_root: str, profile_name: str) -> str:
    if not user_data_root:
        raise ValueError("UserData root is empty.")
    target = os.path.join(user_data_root, profile_name)
    os.makedirs(target, exist_ok=True)
    return target


def profile_name_to_marker_name(profile_name: str) -> str:
    profile_name = str(profile_name or "").strip()
    return profile_name or "Current Profile"


def chrome_bookmark_timestamp(unix_seconds_text: str = "") -> str:
    epoch_start = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    try:
        timestamp = float(str(unix_seconds_text).strip())
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    except Exception:
        dt = datetime.datetime.now(datetime.timezone.utc)
    delta = dt - epoch_start
    return str(int(delta.total_seconds() * 1_000_000))


def load_bookmark_template_tree(template_path: str) -> Dict[str, List[Dict]]:
    if not template_path or not os.path.exists(template_path):
        raise FileNotFoundError(f"bookmark template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as handle:
        parser = BookmarkTemplateParser()
        parser.feed(handle.read())
        parser.close()

    toolbar_folder = None
    other_children: List[Dict] = []
    for child in parser.root["children"]:
        if child.get("type") == "folder":
            is_toolbar = str(child.get("personal_toolbar_folder", "")).strip().lower() == "true"
            if is_toolbar or child.get("name", "").strip() in BOOKMARK_BAR_FOLDER_NAMES:
                if toolbar_folder is None:
                    toolbar_folder = child
                    continue
        other_children.append(child)

    if toolbar_folder is None:
        return {
            "bookmark_bar": list(parser.root["children"]),
            "other": [],
        }

    return {
        "bookmark_bar": list(toolbar_folder.get("children", [])),
        "other": other_children,
    }


def build_chromium_bookmark_node(template_node: Dict, id_counter: List[int]) -> Dict:
    node_id = str(id_counter[0])
    id_counter[0] += 1
    date_added = chrome_bookmark_timestamp(template_node.get("add_date", ""))

    if template_node.get("type") == "url":
        return {
            "type": "url",
            "name": str(template_node.get("name", "")).strip(),
            "url": str(template_node.get("url", "")).strip(),
            "id": node_id,
            "guid": str(uuid.uuid4()),
            "date_added": date_added,
            "date_last_used": "0",
        }

    children = [
        build_chromium_bookmark_node(child, id_counter)
        for child in template_node.get("children", [])
        if isinstance(child, dict)
    ]
    date_modified = chrome_bookmark_timestamp(
        template_node.get("last_modified", "") or template_node.get("add_date", "")
    )
    return {
        "type": "folder",
        "name": str(template_node.get("name", "")).strip(),
        "children": children,
        "id": node_id,
        "guid": str(uuid.uuid4()),
        "date_added": date_added,
        "date_last_used": "0",
        "date_modified": date_modified,
    }


def build_bookmarks_json_from_template(template_path: str) -> Dict:
    tree = load_bookmark_template_tree(template_path)
    id_counter = [4]
    now_chrome = chrome_bookmark_timestamp()
    bookmark_bar_children = [
        build_chromium_bookmark_node(child, id_counter)
        for child in tree.get("bookmark_bar", [])
        if isinstance(child, dict)
    ]
    other_children = [
        build_chromium_bookmark_node(child, id_counter)
        for child in tree.get("other", [])
        if isinstance(child, dict)
    ]

    return {
        "checksum": "",
        "roots": {
            "bookmark_bar": {
                "children": bookmark_bar_children,
                "date_added": now_chrome,
                "date_last_used": "0",
                "date_modified": now_chrome,
                "guid": "0bc5d13f-2cba-5d74-951f-3f233fe6c908",
                "id": "1",
                "name": BOOKMARK_ROOT_BAR_NAME,
                "type": "folder",
            },
            "other": {
                "children": other_children,
                "date_added": now_chrome,
                "date_last_used": "0",
                "date_modified": now_chrome if other_children else "0",
                "guid": "82b081ec-3dd3-529c-8475-ab6c344590dd",
                "id": "2",
                "name": BOOKMARK_ROOT_OTHER_NAME,
                "type": "folder",
            },
            "synced": {
                "children": [],
                "date_added": now_chrome,
                "date_last_used": "0",
                "date_modified": "0",
                "guid": "4cf2e351-0e85-532b-bb37-df045d8f8d0f",
                "id": "3",
                "name": BOOKMARK_ROOT_MOBILE_NAME,
                "type": "folder",
            },
        },
        "version": 1,
    }


def iter_bookmark_nodes(nodes: Sequence[Dict]):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        yield node
        if node.get("type") == "folder":
            yield from iter_bookmark_nodes(node.get("children", []))


def ensure_profile_bookmark_marker(config: Dict, profile_name: str) -> bool:
    normalized = normalize_config(config)
    paths = normalized.get("paths", {})
    user_data_root = paths.get("user_data_root", "")
    if not user_data_root:
        return False

    profile_dir = ensure_profile_directory(user_data_root, profile_name)
    bookmarks_path = os.path.join(profile_dir, "Bookmarks")
    if not os.path.exists(bookmarks_path):
        return False

    with open(bookmarks_path, "r", encoding="utf-8") as handle:
        try:
            bookmarks_json = json.load(handle)
        except json.JSONDecodeError:
            return False

    roots = bookmarks_json.setdefault("roots", {})
    bookmark_bar = roots.setdefault(
        "bookmark_bar",
        {
            "children": [],
            "date_added": chrome_bookmark_timestamp(),
            "date_last_used": "0",
            "date_modified": chrome_bookmark_timestamp(),
            "guid": "0bc5d13f-2cba-5d74-951f-3f233fe6c908",
            "id": "1",
            "name": BOOKMARK_ROOT_BAR_NAME,
            "type": "folder",
        },
    )
    children = bookmark_bar.get("children", [])
    if not isinstance(children, list):
        children = []
    bookmark_bar["children"] = children

    marker_name = profile_name_to_marker_name(profile_name)
    legacy_marker_name = profile_name_to_start_id(profile_name)

    max_id = 0
    for node in iter_bookmark_nodes(roots.values()):
        node_id = node.get("id")
        try:
            max_id = max(max_id, int(str(node_id)))
        except (TypeError, ValueError):
            continue

    preferred_indexes = []
    legacy_indexes = []
    for index, child in enumerate(children):
        if not isinstance(child, dict) or child.get("type") != "url":
            continue
        name = str(child.get("name", "")).strip()
        if name == marker_name:
            preferred_indexes.append(index)
        elif name == legacy_marker_name:
            legacy_indexes.append(index)

    changed = False
    now_chrome = chrome_bookmark_timestamp()

    if preferred_indexes:
        preferred = children[preferred_indexes[0]]
        if str(preferred.get("url", "")).strip() != PROFILE_MARKER_URL:
            preferred["url"] = PROFILE_MARKER_URL
            changed = True
        for index in reversed(preferred_indexes[1:] + legacy_indexes):
            del children[index]
            changed = True
    elif legacy_indexes:
        preferred = children[legacy_indexes[0]]
        if str(preferred.get("name", "")).strip() != marker_name:
            preferred["name"] = marker_name
            changed = True
        if str(preferred.get("url", "")).strip() != PROFILE_MARKER_URL:
            preferred["url"] = PROFILE_MARKER_URL
            changed = True
        for index in reversed(legacy_indexes[1:]):
            del children[index]
            changed = True
    else:
        max_id += 1
        children.append(
            {
                "type": "url",
                "name": marker_name,
                "url": PROFILE_MARKER_URL,
                "id": str(max_id),
                "guid": str(uuid.uuid4()),
                "date_added": now_chrome,
                "date_last_used": "0",
            }
        )
        changed = True

    if changed:
        bookmark_bar["date_modified"] = now_chrome
        bookmarks_json["checksum"] = ""
        write_json_atomic(bookmarks_path, bookmarks_json)
    return changed


def ensure_profile_bookmarks_initialized(config: Dict, profile_name: str, overwrite: bool = False) -> bool:
    normalized = normalize_config(config)
    paths = normalized.get("paths", {})
    user_data_root = paths.get("user_data_root", "")
    template_path = paths.get("bookmarks_template_path", "")
    if not user_data_root or not template_path:
        return False

    profile_dir = ensure_profile_directory(user_data_root, profile_name)
    bookmarks_path = os.path.join(profile_dir, "Bookmarks")
    changed = False
    if os.path.exists(bookmarks_path) and not overwrite:
        return ensure_profile_bookmark_marker(normalized, profile_name)

    bookmarks_json = build_bookmarks_json_from_template(template_path)
    write_json_atomic(bookmarks_path, bookmarks_json)
    changed = True
    return ensure_profile_bookmark_marker(normalized, profile_name) or changed


def profile_name_to_start_id(profile_name: str) -> str:
    match = re.match(r"^Profile\s+(\d+)$", profile_name)
    if match:
        return f"p{match.group(1)}"
    return profile_name


def resolve_path_candidates(path_value: str, candidate_names: Sequence[str]) -> str:
    raw_value = str(path_value or "").strip()
    if raw_value and os.path.isfile(os.path.abspath(os.path.expanduser(raw_value))):
        return os.path.abspath(os.path.expanduser(raw_value))

    if raw_value and os.path.isdir(os.path.abspath(os.path.expanduser(raw_value))):
        base_dir = os.path.abspath(os.path.expanduser(raw_value))
        for candidate_name in candidate_names:
            candidate_path = os.path.join(base_dir, candidate_name)
            if os.path.isfile(candidate_path):
                return candidate_path

    if raw_value and os.path.sep not in raw_value and (os.path.altsep or "") not in raw_value:
        found = shutil.which(raw_value)
        if found:
            return found

    for candidate_name in candidate_names:
        found = shutil.which(candidate_name)
        if found:
            return found

    if raw_value:
        return os.path.abspath(os.path.expanduser(raw_value))
    return ""


def resolve_chromium_binary(chromium_dir: str) -> str:
    if SYSTEM_NAME == "Windows":
        candidates = ["chrome.exe", "chromium.exe"]
    elif SYSTEM_NAME == "Darwin":
        candidates = [
            "Chromium",
            "Google Chrome",
            os.path.join("Chromium.app", "Contents", "MacOS", "Chromium"),
            os.path.join("Google Chrome.app", "Contents", "MacOS", "Google Chrome"),
        ]
    else:
        candidates = ["chromium", "chromium-browser", "google-chrome", "chrome"]
    return resolve_path_candidates(chromium_dir, candidates)


def resolve_chromedriver_path(chromedriver_path: str) -> str:
    candidates = ["chromedriver.exe"] if SYSTEM_NAME == "Windows" else ["chromedriver"]
    return resolve_path_candidates(chromedriver_path, candidates)


def detect_chromium_major_version(path_value: str) -> Optional[int]:
    text = path_value or ""
    match = re.search(r"(\d{2,3})\.\d+\.\d+\.\d+", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def detect_fingerprint_extension_dir(zip_path: str) -> str:
    if not zip_path:
        return ""

    base_dir = os.path.dirname(os.path.abspath(os.path.expanduser(zip_path)))
    version_match = re.search(r"(\d+\.\d+\.\d+)", os.path.basename(zip_path))
    version_text = version_match.group(1) if version_match else ""
    candidates = unique_paths(
        [
            os.path.join(base_dir, "extensions", "my-fingerprint-2.7.2"),
            os.path.join(base_dir, "extensions", "my-fingerprint"),
            os.path.join(base_dir, "my-fingerprint-2.7.2"),
            os.path.join(base_dir, "extensions"),
        ]
    )

    for candidate in candidates:
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "manifest.json")):
            return candidate

    pattern = os.path.join(base_dir, "extensions", "*")
    for item in glob.glob(pattern):
        if not os.path.isdir(item):
            continue
        name = os.path.basename(item).lower()
        if "fingerprint" not in name:
            continue
        if version_text and version_text not in name:
            continue
        if os.path.isfile(os.path.join(item, "manifest.json")):
            return item

    return ""


def build_profile_detail_text(profile: Dict, translate: Optional[Callable[[str, str], str]] = None) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    details = profile.get("last_keepalive_details", {}) or {}
    site_parts = []
    for site_name in KEEPALIVE_SITE_ORDER:
        info = details.get(site_name, {})
        if not info:
            continue
        site_parts.append(format_keepalive_site_status(site_name, info, tr))

    return "\n".join(
        [
            f"{tr('detail_profile', 'Profile')}: {profile.get('profile_name', '')}",
            f"{tr('detail_account', 'Account')}: {profile.get('account', '') or '-'}",
            f"{tr('detail_keepalive_enabled', 'Keepalive Enabled')}: {tr('common_yes', 'Yes') if profile.get('keepalive_enabled') else tr('common_no', 'No')}",
            f"{tr('detail_keepalive_sites', 'Enabled Sites')}: {format_keepalive_sites_text(profile.get('keepalive_sites', {}), tr)}",
            f"{tr('detail_last_launch', 'Last Launch')}: {profile.get('last_launch_at', '') or '-'}",
            f"{tr('detail_last_keepalive', 'Last Keepalive')}: {profile.get('last_keepalive_at', '') or '-'}",
            f"{tr('detail_last_status', 'Last Status')}: {profile.get('last_keepalive_status', '') or '-'}",
            f"{tr('detail_last_message', 'Last Message')}: {profile.get('last_keepalive_message', '') or '-'}",
            f"{tr('detail_last_mirror', 'Last Mirror')}: {profile.get('last_mirror_at', '') or '-'}",
            f"{tr('detail_last_mirror_status', 'Mirror Status')}: {profile.get('last_mirror_status', '') or '-'}",
            f"{tr('detail_last_mirror_message', 'Mirror Message')}: {profile.get('last_mirror_message', '') or '-'}",
            f"{tr('detail_site_detail', 'Site Detail')}: {' | '.join(site_parts) if site_parts else '-'}",
            f"{tr('detail_notes', 'Notes')}: {profile.get('notes', '') or '-'}",
        ]
    )


class KeepAliveLoginRequiredError(RuntimeError):
    def __init__(self, site_name: str, message: str):
        super().__init__(message)
        self.site_name = site_name


class KeepAliveSoftFailureError(RuntimeError):
    def __init__(self, site_name: str, message: str):
        super().__init__(message)
        self.site_name = site_name


def normalize_fs_path(path_value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(path_value))))


def find_running_chromium_processes(config: Dict) -> List[Dict]:
    paths = normalize_config(config).get("paths", {})
    chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
    if not chromium_binary:
        return []
    chromium_root = os.path.dirname(chromium_binary)
    binary_norm = normalize_fs_path(chromium_binary)
    root_norm = normalize_fs_path(chromium_root)

    matches: List[Dict] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if proc.info.get("pid") == os.getpid():
                continue

            candidates = []
            if proc.info.get("exe"):
                candidates.append(proc.info["exe"])
            cmdline = proc.info.get("cmdline") or []
            if cmdline:
                candidates.append(cmdline[0])

            matched_path = ""
            for candidate in candidates:
                if not candidate:
                    continue
                candidate_norm = normalize_fs_path(candidate)
                if candidate_norm == binary_norm:
                    matched_path = candidate
                    break
                try:
                    common = os.path.commonpath([candidate_norm, root_norm])
                except ValueError:
                    common = ""
                if common == root_norm:
                    matched_path = candidate
                    break

            if not matched_path:
                continue

            matches.append(
                {
                    "pid": proc.info.get("pid"),
                    "name": proc.info.get("name", ""),
                    "path": matched_path,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if matches or os.name != "nt":
        return matches

    ps_script = r"""
$items = Get-Process chrome -ErrorAction SilentlyContinue |
    Select-Object Id, ProcessName, Path
$items | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **get_hidden_subprocess_kwargs(),
        )
        raw = (result.stdout or "").strip()
        if raw:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                loaded = [loaded]
            if isinstance(loaded, list):
                for item in loaded:
                    if not isinstance(item, dict):
                        continue
                    executable_path = str(item.get("Path", "") or "").strip()
                    if not executable_path:
                        continue
                    candidate_norm = normalize_fs_path(executable_path)
                    matched = False
                    if candidate_norm == binary_norm:
                        matched = True
                    else:
                        try:
                            common = os.path.commonpath([candidate_norm, root_norm])
                        except ValueError:
                            common = ""
                        matched = (common == root_norm)

                    if not matched:
                        continue

                    matches.append(
                        {
                            "pid": item.get("Id"),
                            "name": item.get("ProcessName", ""),
                            "path": executable_path,
                        }
                    )
    except Exception:
        pass

    return matches


class SingleRunLock:
    def __init__(self, lock_path: str, stale_seconds: int = 12 * 60 * 60):
        self.lock_path = lock_path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def try_acquire(self) -> bool:
        if os.path.exists(self.lock_path):
            try:
                age = time.time() - os.path.getmtime(self.lock_path)
                if age > self.stale_seconds:
                    os.remove(self.lock_path)
            except OSError:
                pass

        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        payload = json.dumps({"pid": os.getpid(), "time": now_text()}, ensure_ascii=False)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def __enter__(self):
        if not self.try_acquire():
            raise RuntimeError("KeepAlive job is already running.")
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.release()


def log_message(logger: Optional[Callable[[str], None]], message: str) -> None:
    if logger:
        logger(message)


class KeepAliveStoppedError(RuntimeError):
    pass


class KeepAliveStopController:
    def __init__(self):
        self._stop_event = threading.Event()
        self._driver_lock = threading.Lock()
        self._current_driver = None

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()
        driver = None
        with self._driver_lock:
            driver = self._current_driver
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def check_or_raise(self) -> None:
        if self.should_stop():
            raise KeepAliveStoppedError("keepalive stopped by user")

    def bind_driver(self, driver) -> None:
        with self._driver_lock:
            self._current_driver = driver

    def clear_driver(self, driver=None) -> None:
        with self._driver_lock:
            if driver is None or self._current_driver is driver:
                self._current_driver = None


def interruptible_sleep(seconds: int, stop_controller: Optional[KeepAliveStopController] = None) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if stop_controller:
            stop_controller.check_or_raise()
        interval = min(0.25, remaining)
        time.sleep(interval)
        remaining -= interval


def update_profile_launch_time(config: Dict, profile_name: str) -> Dict:
    normalized = normalize_config(config)
    for item in normalized["profiles"]:
        if item.get("profile_name") == profile_name:
            item["last_launch_at"] = now_text()
            break
    return normalized


def resolve_site_url(site: str) -> str:
    site_text = str(site or "").strip()
    if not site_text:
        return ""
    site_map = {
        "google": "https://www.google.com/",
        "gmail": "https://mail.google.com/",
        "chatgpt": "https://chatgpt.com/",
        "github": "https://github.com/",
    }
    if site_text in site_map:
        return site_map[site_text]
    if re.match(r"^https?://", site_text, flags=re.IGNORECASE):
        return site_text
    return f"https://{site_text}"


def build_direct_launch_command(profile_name: str, config: Dict, site: str = "") -> List[str]:
    normalized = normalize_config(config)
    paths = normalized["paths"]
    launch_settings = normalized.get("launch", {})
    chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
    user_data_root = os.path.abspath(os.path.expanduser(paths.get("user_data_root", "")))
    if not chromium_binary or not os.path.exists(chromium_binary):
        raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
    if not user_data_root:
        raise ValueError("user_data_root is required")

    command = [
        chromium_binary,
        f"--user-data-dir={user_data_root}",
        f"--profile-directory={profile_name}",
    ]

    if launch_settings.get("new_window", True):
        command.append("--new-window")
    if launch_settings.get("start_maximized", True):
        command.append("--start-maximized")
    window_size = str(launch_settings.get("window_size", "")).strip()
    if window_size:
        command.append(f"--window-size={window_size}")
    if launch_settings.get("no_first_run", True):
        command.append("--no-first-run")
    if launch_settings.get("no_default_browser_check", True):
        command.append("--no-default-browser-check")
    if launch_settings.get("disable_background_networking", True):
        command.append("--disable-background-networking")
    if launch_settings.get("disable_default_apps", True):
        command.append("--disable-default-apps")
    if launch_settings.get("disable_sync", True):
        command.append("--disable-sync")
    if launch_settings.get("metrics_recording_only", True):
        command.append("--metrics-recording-only")
    if launch_settings.get("disable_client_side_phishing_detection", False):
        command.append("--disable-client-side-phishing-detection")
    if launch_settings.get("disable_webrtc", False):
        command.append("--disable-webrtc")
    webrtc_policy = str(launch_settings.get("webrtc_ip_handling_policy", "")).strip()
    if webrtc_policy:
        command.append(f"--webrtc-ip-handling-policy={webrtc_policy}")
    if launch_settings.get("force_webrtc_ip_handling_policy", False):
        command.append("--force-webrtc-ip-handling-policy")

    if launch_settings.get("load_fingerprint_extension", True):
        extension_dir = detect_fingerprint_extension_dir(paths.get("fingerprint_zip_path", ""))
        if extension_dir:
            command.append(f"--load-extension={extension_dir}")

    if isinstance(launch_settings.get("extra_args", []), list):
        command.extend([item for item in launch_settings.get("extra_args", []) if item])

    if launch_settings.get("open_extensions_page", False):
        command.append("chrome://extensions")
    check_url = str(launch_settings.get("check_url", "")).strip()
    if check_url:
        command.append(check_url)

    target_url = resolve_site_url(site)
    if target_url:
        command.append(target_url)
    return command


def launch_profile(profile_name: str, config: Dict, site: str = "") -> subprocess.CompletedProcess:
    normalized = normalize_config(config)
    command = build_direct_launch_command(profile_name, normalized, site)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name != "nt"),
        **get_hidden_subprocess_kwargs(),
    )
    return subprocess.CompletedProcess(
        args=command,
        returncode=0,
        stdout=f"launched pid={process.pid} via built-in launcher",
        stderr="",
    )


def wait_for_any(driver, selectors: Sequence, timeout: int):
    def _locate(_):
        for by, value in selectors:
            found = driver.find_elements(by, value)
            if found:
                return found[0]
        return False

    return WebDriverWait(driver, timeout).until(_locate)


def is_interactable(element) -> bool:
    try:
        if not element.is_displayed():
            return False
        size = element.size or {}
        if size.get("width", 0) <= 0 or size.get("height", 0) <= 0:
            return False
        if not element.is_enabled():
            return False
    except Exception:
        return False
    return True


def find_first_interactable(driver, selectors: Sequence, timeout: int):
    def _locate(_):
        for by, value in selectors:
            for found in driver.find_elements(by, value):
                if is_interactable(found):
                    return found
        return False

    return WebDriverWait(driver, timeout).until(_locate)


def get_last_assistant_message_text(driver) -> str:
    messages = driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']")
    for message in reversed(messages):
        text = ""
        try:
            text = (message.text or "").strip()
        except Exception:
            text = ""
        if text:
            return text
    return ""


def wait_for_assistant_text_to_stabilize(
    driver,
    timeout_seconds: int,
    stable_seconds: int,
    stop_controller: Optional[KeepAliveStopController] = None,
) -> str:
    deadline = time.time() + max(1, int(timeout_seconds))
    stable_seconds = max(1, int(stable_seconds))
    last_text = ""
    last_change_at = time.time()

    while time.time() < deadline:
        if stop_controller:
            stop_controller.check_or_raise()
        current_text = get_last_assistant_message_text(driver)
        now_ts = time.time()
        if current_text and current_text != last_text:
            last_text = current_text
            last_change_at = now_ts
        elif current_text and (now_ts - last_change_at) >= stable_seconds:
            return current_text
        time.sleep(0.5)

    if stop_controller:
        stop_controller.check_or_raise()
    return last_text


def choose_chatgpt_prompt(settings: Dict) -> str:
    raw_prompt = str(settings.get("chatgpt_prompt", "")).strip()
    if raw_prompt and raw_prompt != LEGACY_CHATGPT_PROMPT:
        return raw_prompt
    return random.choice(DEFAULT_CHATGPT_PROMPTS)


def list_chatgpt_sidebar_conversations(driver) -> List:
    selectors = [
        "nav a[href^='/c/']",
        "aside a[href^='/c/']",
        "a[href^='/c/']",
    ]
    conversations = []
    seen = set()
    for selector in selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            found = []
        for item in found:
            try:
                href = item.get_attribute("href") or ""
                text = (item.text or "").strip()
            except Exception:
                href = ""
                text = ""
            key = (href, text)
            if key in seen:
                continue
            seen.add(key)
            if href and "/c/" in href:
                conversations.append(item)
    return conversations


def open_chatgpt_existing_conversation(
    driver,
    timeout: int,
    settings: Dict,
    logger: Optional[Callable[[str], None]] = None,
    stop_controller: Optional[KeepAliveStopController] = None,
) -> str:
    hint = str(settings.get("chatgpt_conversation_hint", "")).strip().lower()
    if "/c/" in driver.current_url:
        return "current"

    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if stop_controller:
            stop_controller.check_or_raise()
        conversations = list_chatgpt_sidebar_conversations(driver)
        chosen = None

        if hint:
            for item in conversations:
                try:
                    text = (item.text or "").strip()
                except Exception:
                    text = ""
                if text and hint in text.lower():
                    chosen = item
                    break

        if chosen is None:
            for item in conversations:
                if is_interactable(item):
                    chosen = item
                    break

        if chosen is not None:
            try:
                title = (chosen.text or "").strip() or "existing conversation"
            except Exception:
                title = "existing conversation"
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", chosen)
            except Exception:
                pass
            try:
                chosen.click()
            except Exception:
                driver.execute_script("arguments[0].click();", chosen)

            WebDriverWait(driver, timeout).until(
                lambda current: "/c/" in current.current_url
                or current.find_elements(By.CSS_SELECTOR, "#prompt-textarea")
                or current.find_elements(By.CSS_SELECTOR, "textarea#prompt-textarea")
            )
            log_message(logger, f"ChatGPT conversation selected: {title}")
            return title

        last_error = "no existing conversation found in sidebar"
        time.sleep(0.5)

    raise RuntimeError(last_error or "failed to open existing ChatGPT conversation")


def dismiss_google_consent_if_needed(driver) -> None:
    # Consent UIs vary by locale, so keep a short multilingual allowlist here.
    button_texts = [
        "I agree",
        "Accept all",
        "Reject all",
        "Accept",
        "接受全部",
        "同意",
    ]
    for text in button_texts:
        buttons = driver.find_elements(By.XPATH, f"//button[normalize-space()='{text}']")
        if buttons:
            try:
                buttons[0].click()
                time.sleep(1)
                return
            except Exception:
                continue


def build_page_debug_hint(driver, limit: int = 180) -> str:
    try:
        url = str(driver.current_url or "").strip()
    except Exception:
        url = ""
    try:
        title = str(driver.title or "").strip()
    except Exception:
        title = ""
    try:
        body_text = str(
            driver.execute_script(
                "return document.body ? (document.body.innerText || document.body.textContent || '') : '';"
            )
            or ""
        ).strip()
    except Exception:
        body_text = ""
    snippet = re.sub(r"\s+", " ", body_text)
    if len(snippet) > limit:
        snippet = snippet[:limit] + "..."
    parts = []
    if title:
        parts.append(f"title={title}")
    if url:
        parts.append(f"url={url}")
    if snippet:
        parts.append(f"body={snippet}")
    return "; ".join(parts) if parts else "no page context"


def _google_results_ready(driver, query: str) -> bool:
    query_text = str(query or "").strip().lower()
    try:
        current_url = str(driver.current_url or "").lower()
    except Exception:
        current_url = ""
    try:
        title = str(driver.title or "").lower()
    except Exception:
        title = ""

    if "/search?" in current_url and "q=" in current_url:
        return True
    if query_text and query_text in title and "google" not in title:
        return True

    selectors = [
        (By.ID, "search"),
        (By.CSS_SELECTOR, "a h3"),
        (By.CSS_SELECTOR, "div[data-snc]"),
        (By.CSS_SELECTOR, "div.g"),
        (By.CSS_SELECTOR, "div#rso"),
        (By.CSS_SELECTOR, "[role='main'] a h3"),
    ]
    for by, selector in selectors:
        try:
            if driver.find_elements(by, selector):
                return True
        except Exception:
            continue
    return False


def _open_google_search_results(driver, query: str, timeout: int) -> None:
    query = str(query or "").strip()
    attempts = []
    try:
        search_box = wait_for_any(
            driver,
            [
                (By.CSS_SELECTOR, "textarea[name='q']"),
                (By.CSS_SELECTOR, "input[name='q']"),
            ],
            timeout,
        )
        attempts.append("search-box")
    except TimeoutException as exc:
        raise RuntimeError(f"Google search box did not appear. {build_page_debug_hint(driver)}") from exc

    try:
        search_box.clear()
    except Exception:
        pass
    try:
        search_box.click()
    except Exception:
        pass

    last_exc = None
    action_attempts = [
        ("enter", lambda: (search_box.send_keys(query), search_box.send_keys(Keys.ENTER))),
        (
            "button-click",
            lambda: wait_for_any(
                driver,
                [
                    (By.CSS_SELECTOR, "button[aria-label='Google Search']"),
                    (By.CSS_SELECTOR, "input[name='btnK']"),
                ],
                max(3, min(timeout, 6)),
            ).click(),
        ),
        (
            "form-submit",
            lambda: driver.execute_script(
                """
                const q = arguments[0];
                const box = document.querySelector("textarea[name='q'], input[name='q']");
                if (!box) return false;
                box.focus();
                box.value = q;
                box.dispatchEvent(new Event('input', { bubbles: true }));
                box.dispatchEvent(new Event('change', { bubbles: true }));
                const form = box.form || box.closest('form');
                if (form && typeof form.submit === 'function') {
                    form.submit();
                    return true;
                }
                return false;
                """,
                query,
            ),
        ),
        (
            "direct-search-url",
            lambda: driver.get(f"https://www.google.com/search?q={quote_plus(query)}&hl=en"),
        ),
    ]

    for attempt_name, action in action_attempts:
        try:
            attempts.append(attempt_name)
            action()
            WebDriverWait(driver, timeout).until(lambda current: _google_results_ready(current, query))
            return
        except Exception as exc:
            last_exc = exc
            if attempt_name != "direct-search-url":
                try:
                    driver.get("https://www.google.com/ncr")
                    dismiss_google_consent_if_needed(driver)
                    search_box = wait_for_any(
                        driver,
                        [
                            (By.CSS_SELECTOR, "textarea[name='q']"),
                            (By.CSS_SELECTOR, "input[name='q']"),
                        ],
                        max(3, min(timeout, 6)),
                    )
                except Exception:
                    pass
            continue

    raise RuntimeError(
        f"Google results did not load for query '{query}'. attempts={','.join(attempts)}. {build_page_debug_hint(driver)}"
    ) from last_exc


def keepalive_google(
    driver,
    settings: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    timeout = int(settings["page_timeout_seconds"])
    driver.get("https://myaccount.google.com/")
    WebDriverWait(driver, timeout).until(lambda current: current.execute_script("return document.readyState") == "complete")

    current_url = driver.current_url.lower()
    if "accounts.google.com" in current_url or "signin" in current_url:
        raise KeepAliveLoginRequiredError("google", "Google account is not signed in for this profile.")

    driver.get("https://www.google.com/ncr")
    dismiss_google_consent_if_needed(driver)

    query = str(settings.get("google_query", "")).strip() or "profile keepalive"
    _open_google_search_results(driver, query, timeout)
    dwell_seconds = int(settings.get("site_dwell_seconds", 6))
    if dwell_seconds > 0:
        log_message(logger, f"Google results ready; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    log_message(logger, f"Google search ok: {query}")
    return {"status": "success", "message": f"searched '{query}' and stayed {dwell_seconds}s"}


def keepalive_gmail(
    driver,
    settings: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    driver.get("https://mail.google.com/")
    timeout = int(settings["page_timeout_seconds"])
    dwell_seconds = int(settings.get("site_dwell_seconds", 6))
    WebDriverWait(driver, timeout).until(lambda current: current.execute_script("return document.readyState") == "complete")

    current_url = driver.current_url.lower()
    if "accounts.google.com" in current_url or "signin" in current_url:
        raise KeepAliveLoginRequiredError("gmail", "Gmail is not signed in for this profile.")

    try:
        wait_for_any(
            driver,
            [
                (By.CSS_SELECTOR, "div[role='main']"),
                (By.CSS_SELECTOR, "table[role='grid']"),
                (By.CSS_SELECTOR, "tr.zA"),
                (By.CSS_SELECTOR, "input[placeholder*='Search mail']"),
            ],
            timeout,
        )
    except TimeoutException:
        raise RuntimeError("Gmail inbox did not load in time.")

    rows = driver.find_elements(By.CSS_SELECTOR, "tr.zA")
    if rows:
        try:
            rows[0].click()
            if dwell_seconds > 0:
                log_message(logger, f"Gmail first message opened; staying {dwell_seconds}s")
                interruptible_sleep(dwell_seconds, stop_controller)
            driver.back()
            wait_for_any(
                driver,
                [
                    (By.CSS_SELECTOR, "table[role='grid']"),
                    (By.CSS_SELECTOR, "tr.zA"),
                ],
                timeout,
            )
            interruptible_sleep(min(2, max(0, dwell_seconds)), stop_controller)
            log_message(logger, "Gmail inbox opened and first message previewed.")
            return {"status": "success", "message": f"opened inbox, previewed first email, stayed {dwell_seconds}s"}
        except Exception:
            pass

    if dwell_seconds > 0:
        log_message(logger, f"Gmail inbox loaded; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    log_message(logger, "Gmail inbox loaded.")
    return {"status": "success", "message": f"inbox loaded and stayed {dwell_seconds}s"}


def keepalive_chatgpt(
    driver,
    settings: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    driver.get("https://chat.openai.com/")
    timeout = int(settings["page_timeout_seconds"])
    dwell_seconds = int(settings.get("site_dwell_seconds", 6))
    WebDriverWait(driver, timeout).until(lambda current: current.execute_script("return document.readyState") == "complete")

    current_url = driver.current_url.lower()
    if "auth" in current_url or "login" in current_url:
        raise KeepAliveLoginRequiredError("chatgpt", "ChatGPT is not signed in for this profile.")

    composer = find_first_interactable(
        driver,
        [
            (By.CSS_SELECTOR, "#prompt-textarea[contenteditable='true']"),
            (By.CSS_SELECTOR, "div#prompt-textarea[contenteditable='true']"),
            (By.CSS_SELECTOR, "textarea#prompt-textarea"),
            (By.CSS_SELECTOR, "textarea[placeholder*='Message']"),
            (By.CSS_SELECTOR, "[contenteditable='true'][data-lexical-editor='true']"),
            (By.CSS_SELECTOR, "[contenteditable='true']"),
            (By.CSS_SELECTOR, "textarea"),
        ],
        timeout,
    )
    try:
        conversation_title = open_chatgpt_existing_conversation(
            driver,
            timeout,
            settings,
            logger=logger,
            stop_controller=stop_controller,
        )
    except RuntimeError as exc:
        if "no existing conversation found in sidebar" not in str(exc):
            raise
        if dwell_seconds > 0:
            log_message(logger, f"ChatGPT signed in but no reusable conversation found; staying {dwell_seconds}s")
            interruptible_sleep(dwell_seconds, stop_controller)
        log_message(logger, "ChatGPT composer is available without reusable sidebar conversation.")
        return {
            "status": "success",
            "message": "signed in; no reusable conversation found, composer remained available",
        }
    if stop_controller:
        stop_controller.check_or_raise()

    before_count = len(driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']"))
    prompt = choose_chatgpt_prompt(settings)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", composer)
    try:
        composer.click()
    except Exception:
        driver.execute_script("arguments[0].focus();", composer)

    try:
        composer.send_keys(Keys.CONTROL, "a")
        composer.send_keys(Keys.DELETE)
    except Exception:
        pass

    try:
        composer.send_keys(prompt)
        composer.send_keys(Keys.ENTER)
    except Exception:
        ActionChains(driver).move_to_element(composer).click(composer).send_keys(prompt).send_keys(Keys.ENTER).perform()

    WebDriverWait(driver, timeout).until(
        lambda current: len(current.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']")) > before_count
        or "/c/" in current.current_url
    )
    reply_text = wait_for_assistant_text_to_stabilize(driver, timeout, max(2, dwell_seconds), stop_controller)
    if dwell_seconds > 0:
        log_message(logger, f"ChatGPT reply observed; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    log_message(logger, "ChatGPT prompt sent in existing conversation and reply observed.")
    if reply_text:
        snippet = reply_text.replace("\n", " ").strip()
        if len(snippet) > 40:
            snippet = snippet[:40] + "..."
        return {
            "status": "success",
            "message": f"existing conversation used ({conversation_title}); reply: {snippet}",
        }
    return {
        "status": "success",
        "message": f"existing conversation used ({conversation_title}) and reply observed",
    }


def keepalive_github(
    driver,
    settings: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    driver.get("https://github.com/")
    timeout = int(settings["page_timeout_seconds"])
    dwell_seconds = int(settings.get("site_dwell_seconds", 6))
    WebDriverWait(driver, timeout).until(lambda current: current.execute_script("return document.readyState") == "complete")

    current_url = driver.current_url.lower()
    if "/login" in current_url or "/session" in current_url:
        raise KeepAliveLoginRequiredError("github", "GitHub is not signed in for this profile.")

    user_login = str(
        driver.execute_script(
            "const meta = document.querySelector('meta[name=\"user-login\"]'); return meta ? meta.content || '' : '';"
        )
        or ""
    ).strip()
    if not user_login:
        sign_in_links = driver.find_elements(By.CSS_SELECTOR, "a[href='/login'], a[data-analytics-event*='login']")
        if sign_in_links:
            raise KeepAliveLoginRequiredError("github", "GitHub is not signed in for this profile.")

    driver.get("https://github.com/pulls")
    WebDriverWait(driver, timeout).until(lambda current: current.execute_script("return document.readyState") == "complete")
    current_url = driver.current_url.lower()
    if "/login" in current_url or "/session" in current_url:
        raise KeepAliveLoginRequiredError("github", "GitHub is not signed in for this profile.")

    if dwell_seconds > 0:
        log_message(logger, f"GitHub pulls page loaded; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    log_message(logger, "GitHub pulls page loaded.")
    return {"status": "success", "message": f"pull requests page loaded and stayed {dwell_seconds}s"}


def create_driver_for_profile(config: Dict, profile_name: str):
    paths = config["paths"]
    chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
    chromedriver_binary = resolve_chromedriver_path(paths.get("chromedriver_path", ""))
    user_data_root = os.path.abspath(os.path.expanduser(paths.get("user_data_root", "")))

    if not chromium_binary or not os.path.exists(chromium_binary):
        raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
    if not chromedriver_binary or not os.path.exists(chromedriver_binary):
        raise FileNotFoundError(f"chromedriver not found: {chromedriver_binary or paths.get('chromedriver_path', '')}")
    if not os.path.isdir(user_data_root):
        raise FileNotFoundError(f"UserData root not found: {user_data_root}")

    options = uc.ChromeOptions()
    options.binary_location = chromium_binary
    options.add_argument(f"--user-data-dir={user_data_root}")
    options.add_argument(f"--profile-directory={profile_name}")
    options.add_argument("--disable-notifications")
    if resolve_mcp_start_minimized(config):
        options.add_argument("--start-minimized")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--disable-popup-blocking")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

    if resolve_mcp_headless(config):
        options.add_argument("--headless=new")

    extension_dir = detect_fingerprint_extension_dir(paths.get("fingerprint_zip_path", ""))
    if extension_dir:
        options.add_argument(f"--load-extension={extension_dir}")

    kwargs = {
        "driver_executable_path": chromedriver_binary,
        "options": options,
        "use_subprocess": True,
    }
    version_main = detect_chromium_major_version(paths.get("chromium_dir", ""))
    if version_main:
        kwargs["version_main"] = version_main

    driver = uc.Chrome(**kwargs)
    try:
        # MCP sessions should not steal the desktop unless the user opted out.
        if resolve_mcp_start_minimized(config):
            driver.minimize_window()
        else:
            driver.set_window_position(0, 0)
            driver.maximize_window()
    except Exception:
        pass
    return driver


def run_profile_keepalive(
    config: Dict,
    profile_name: str,
    logger: Optional[Callable[[str], None]] = None,
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    settings = config["keepalive"]
    profile_entry = next(
        (item for item in config.get("profiles", []) if item.get("profile_name") == profile_name),
        {},
    )
    enabled_sites = normalize_keepalive_site_flags(profile_entry.get("keepalive_sites", {}), default=False)
    driver = None
    site_results: Dict[str, Dict[str, str]] = {}
    failed_sites = []
    disabled_sites = []
    soft_sites = []

    try:
        if stop_controller:
            stop_controller.check_or_raise()
        driver = create_driver_for_profile(config, profile_name)
        if stop_controller:
            stop_controller.bind_driver(driver)
            stop_controller.check_or_raise()
        interruptible_sleep(int(settings["settle_seconds"]), stop_controller)

        actions = []
        if enabled_sites.get("chatgpt"):
            actions.append(("chatgpt", keepalive_chatgpt))
        if enabled_sites.get("gmail"):
            actions.append(("gmail", keepalive_gmail))
        if enabled_sites.get("google"):
            actions.append(("google", keepalive_google))
        if enabled_sites.get("github"):
            actions.append(("github", keepalive_github))

        if not actions:
            return {
                "profile_name": profile_name,
                "status": "skipped",
                "message": "no keepalive sites checked for this profile",
                "details": {},
                "disabled_sites": [],
            }

        for site_name, action in actions:
            if stop_controller:
                stop_controller.check_or_raise()
            try:
                log_message(logger, f"{profile_name}: start {site_name}")
                result = action(driver, settings, logger, stop_controller)
                result["signed_in"] = True
                site_results[site_name] = result
                if stop_controller:
                    stop_controller.check_or_raise()
            except KeepAliveStoppedError:
                raise
            except KeepAliveLoginRequiredError as exc:
                disabled_sites.append(site_name)
                soft_sites.append(site_name)
                site_results[site_name] = {"status": "signed_out", "message": str(exc), "signed_in": False}
                log_message(logger, f"{profile_name}: {site_name} signed out; unchecked for next run")
            except KeepAliveSoftFailureError as exc:
                soft_sites.append(site_name)
                site_results[site_name] = {"status": "attention", "message": str(exc), "signed_in": True}
                log_message(logger, f"{profile_name}: {site_name} attention: {exc}")
            except Exception as exc:
                if stop_controller and stop_controller.should_stop():
                    raise KeepAliveStoppedError("keepalive stopped by user") from exc
                failed_sites.append(site_name)
                site_results[site_name] = {"status": "failed", "message": str(exc)}
                log_message(logger, f"{profile_name}: {site_name} failed: {exc}")

        if failed_sites and len(failed_sites) == len(actions):
            summary_status = "failed"
            summary_message = "all enabled sites failed"
        elif failed_sites:
            summary_status = "partial"
            summary_message = "some sites failed"
        elif soft_sites:
            summary_status = "partial"
            summary_message = "some sites require attention"
        else:
            summary_status = "success"
            summary_message = "all enabled sites succeeded"

        return {
            "profile_name": profile_name,
            "status": summary_status,
            "message": summary_message,
            "details": site_results,
            "disabled_sites": disabled_sites,
        }
    finally:
        if stop_controller:
            stop_controller.clear_driver(driver)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def run_keepalive_job(
    config_path: Optional[str] = None,
    selected_profiles: Optional[Sequence[str]] = None,
    logger: Optional[Callable[[str], None]] = None,
    source: str = "manual",
    stop_controller: Optional[KeepAliveStopController] = None,
    progress_callback: Optional[Callable[[str, Dict], None]] = None,
) -> Dict:
    from chromium_advanced.mirror_manager import MirrorManager

    path = config_path or get_default_config_path()
    config = load_app_config(path)
    lock = SingleRunLock(get_lock_path())
    mirror_lock_path = get_mirror_lock_path()

    if not lock.try_acquire():
        summary = {
            "status": "skipped",
            "message": "keepalive job already running",
            "profile_results": [],
            "started_at": now_text(),
            "finished_at": now_text(),
            "source": source,
        }
        log_message(logger, summary["message"])
        return summary

    try:
        started_at = now_text()
        keepalive = config["keepalive"]
        keepalive["last_run_at"] = started_at
        keepalive["last_run_finished_at"] = ""
        keepalive["last_run_status"] = "running"
        keepalive["last_run_message"] = "keepalive job started"
        keepalive["last_run_source"] = source
        save_app_config(config, path)

        running_processes = find_running_chromium_processes(config)
        if running_processes:
            finished_at = now_text()
            pid_text = ", ".join(str(item.get("pid")) for item in running_processes[:6])
            message = f"chromium already running, skip keepalive (pid: {pid_text})"
            keepalive["last_run_finished_at"] = finished_at
            keepalive["last_run_status"] = "skipped"
            keepalive["last_run_message"] = message
            keepalive["last_run_profile_count"] = 0
            keepalive["last_run_details"] = []
            save_app_config(config, path)
            log_message(logger, message)
            return {
                "status": "skipped",
                "message": message,
                "profile_results": [],
                "started_at": started_at,
                "finished_at": finished_at,
                "source": source,
            }

        selected_set = {item for item in (selected_profiles or []) if item}
        target_profiles = []
        for item in config["profiles"]:
            profile_name = item.get("profile_name", "")
            if not profile_name:
                continue
            if selected_set and profile_name not in selected_set:
                continue
            if not selected_set and not item.get("keepalive_enabled", False):
                continue
            target_profiles.append(item)

        if not target_profiles:
            finished_at = now_text()
            keepalive["last_run_finished_at"] = finished_at
            keepalive["last_run_status"] = "skipped"
            keepalive["last_run_message"] = "no profiles selected for keepalive"
            keepalive["last_run_profile_count"] = 0
            keepalive["last_run_details"] = []
            save_app_config(config, path)
            summary = {
                "status": "skipped",
                "message": "no profiles selected for keepalive",
                "profile_results": [],
                "started_at": started_at,
                "finished_at": finished_at,
                "source": source,
            }
            log_message(logger, summary["message"])
            return summary

        profile_results = []
        any_failed = False
        any_partial = False
        any_skipped = False

        try:
            for index, item in enumerate(target_profiles):
                if stop_controller:
                    stop_controller.check_or_raise()

                profile_name = item["profile_name"]
                log_message(logger, f"keepalive start: {profile_name}")
                if progress_callback:
                    progress_callback("profile_start", {"profile_name": profile_name, "index": index})

                try:
                    result = run_profile_keepalive(
                        config,
                        profile_name,
                        logger=logger,
                        stop_controller=stop_controller,
                    )
                except KeepAliveStoppedError as exc:
                    result = {
                        "profile_name": profile_name,
                        "status": "stopped",
                        "message": str(exc),
                        "details": {"stop": {"status": "stopped", "message": str(exc)}},
                    }
                    for profile in config["profiles"]:
                        if profile.get("profile_name") != profile_name:
                            continue
                        profile["last_keepalive_at"] = now_text()
                        profile["last_keepalive_status"] = result["status"]
                        profile["last_keepalive_message"] = result["message"]
                        profile["last_keepalive_details"] = result.get("details", {})
                        break
                    profile_results.append(result)
                    save_app_config(config, path)
                    raise
                except Exception as exc:
                    result = {
                        "profile_name": profile_name,
                        "status": "failed",
                        "message": str(exc),
                        "details": {"exception": {"status": "failed", "message": traceback.format_exc(limit=5)}},
                    }
                    log_message(logger, f"{profile_name}: fatal keepalive error: {exc}")

                for profile in config["profiles"]:
                    if profile.get("profile_name") != profile_name:
                        continue
                    profile_sites = normalize_keepalive_site_flags(profile.get("keepalive_sites", {}), default=False)
                    for site_name in result.get("disabled_sites", []):
                        profile_sites[site_name] = False
                    profile["keepalive_sites"] = profile_sites
                    profile["last_keepalive_at"] = now_text()
                    profile["last_keepalive_status"] = result["status"]
                    profile["last_keepalive_message"] = result["message"]
                    profile["last_keepalive_details"] = result.get("details", {})
                    break

                if result["status"] == "failed":
                    any_failed = True
                elif result["status"] == "partial":
                    any_partial = True
                elif result["status"] == "skipped":
                    any_skipped = True

                profile_results.append(result)
                save_app_config(config, path)

                if index < len(target_profiles) - 1:
                    interruptible_sleep(int(config["keepalive"]["between_profiles_seconds"]), stop_controller)
        except KeepAliveStoppedError as exc:
            finished_at = now_text()
            final_status = "stopped"
            final_message = str(exc)
            keepalive["last_run_finished_at"] = finished_at
            keepalive["last_run_status"] = final_status
            keepalive["last_run_message"] = final_message
            keepalive["last_run_profile_count"] = len(profile_results)
            keepalive["last_run_details"] = profile_results
            save_app_config(config, path)

            summary = {
                "status": final_status,
                "message": final_message,
                "profile_results": profile_results,
                "started_at": started_at,
                "finished_at": finished_at,
                "source": source,
            }
            log_message(logger, f"keepalive finished: {final_status}")
            return summary

        finished_at = now_text()
        if any_failed:
            final_status = "failed"
            final_message = "at least one profile failed"
        elif any_partial:
            final_status = "partial"
            final_message = "at least one profile partially failed"
        elif profile_results and all(item.get("status") == "skipped" for item in profile_results):
            final_status = "skipped"
            final_message = "all selected profiles were skipped"
        elif any_skipped:
            final_status = "partial"
            final_message = "at least one profile was skipped"
        else:
            final_status = "success"
            final_message = "all selected profiles finished"

        keepalive["last_run_finished_at"] = finished_at
        keepalive["last_run_status"] = final_status
        keepalive["last_run_message"] = final_message
        keepalive["last_run_profile_count"] = len(profile_results)
        keepalive["last_run_details"] = profile_results
        save_app_config(config, path)

        mirror_summary = None
        mirror_settings = config.get("mirror", {})
        if bool(mirror_settings.get("enabled", False)):
            config["mirror"]["last_run_at"] = now_text()
            config["mirror"]["last_run_finished_at"] = ""
            config["mirror"]["last_run_status"] = "running"
            config["mirror"]["last_run_message"] = "mirror snapshot job started"
            save_app_config(config, path)
            try:
                write_json_atomic(mirror_lock_path, {"started_at": now_text(), "source": source})
            except Exception:
                pass
            try:
                mirror_manager = MirrorManager(config)
                mirror_summary = mirror_manager.refresh_snapshots(logger=logger)
                config = load_app_config(path)
                config["mirror"]["last_run_finished_at"] = mirror_summary.get("finished_at", now_text())
                config["mirror"]["last_run_status"] = mirror_summary.get("status", "success")
                config["mirror"]["last_run_message"] = mirror_summary.get("message", "mirror snapshots updated")
                config["mirror"]["last_run_profile_count"] = len(mirror_summary.get("profiles", []))
                for profile_result in mirror_summary.get("profiles", []):
                    profile_name = str(profile_result.get("profile_name", "")).strip()
                    if not profile_name:
                        continue
                    for profile in config.get("profiles", []):
                        if profile.get("profile_name") != profile_name:
                            continue
                        profile["last_mirror_at"] = mirror_summary.get("finished_at", now_text())
                        profile["last_mirror_status"] = profile_result.get("status", "success")
                        profile["last_mirror_message"] = profile_result.get("message", mirror_summary.get("message", "mirror snapshots updated"))
                        break
                save_app_config(config, path)
            except Exception as exc:
                config = load_app_config(path)
                config["mirror"]["last_run_finished_at"] = now_text()
                config["mirror"]["last_run_status"] = "failed"
                config["mirror"]["last_run_message"] = str(exc)
                save_app_config(config, path)
                log_message(logger, f"mirror finished: failed ({exc})")
            finally:
                try:
                    if os.path.exists(mirror_lock_path):
                        os.remove(mirror_lock_path)
                except OSError:
                    pass

        summary = {
            "status": final_status,
            "message": final_message,
            "profile_results": profile_results,
            "started_at": started_at,
            "finished_at": finished_at,
            "source": source,
        }
        if mirror_summary is not None:
            summary["mirror"] = mirror_summary
        log_message(logger, f"keepalive finished: {final_status}")
        return summary
    finally:
        lock.release()
