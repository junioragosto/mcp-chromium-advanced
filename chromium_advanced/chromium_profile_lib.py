import copy
import datetime
import glob
import hashlib
import importlib.util
import inspect
import json
import locale
import os
import platform
import random
import re
import shutil
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import traceback
import uuid
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

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
from chromium_advanced.version import get_app_version


APP_NAME = "ChromiumProfileManager"
APP_DISPLAY_NAME = "Chromium Profile Manager"
APP_VERSION = get_app_version()
CONFIG_FILENAME = "chromium_profiles.json"
LOCK_FILENAME = "chromium_keepalive.lock"
MIRROR_LOCK_FILENAME = "chromium_mirroring.lock"
_JSON_ATOMIC_WRITE_LOCK = threading.RLock()
WINDOWS_TEXT_ENCODING = locale.getpreferredencoding(False) or "utf-8"
BOOKMARK_BAR_FOLDER_NAMES = {"书签栏", "Bookmarks Bar", "Bookmarks bar", "Bookmarks Toolbar"}
PROFILE_MARKER_URL = "https://www.google.com/generate_204"
LEGACY_CHATGPT_PROMPT = "Reply with one word: alive"
SYSTEM_NAME = platform.system()
KEEPALIVE_SITE_ORDER = ("chatgpt", "gmail", "google", "github")
CHROMIUM_SUPPRESS_RESTORE_PROMPT_ARGS = (
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
)
SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS = {
    "BrowserMetrics",
    "Crashpad",
    "DeferredBrowserMetrics",
    "GraphiteDawnCache",
    "GrShaderCache",
    "ShaderCache",
    "component_crx_cache",
    "mirror_disk",
}
SPLIT_USER_DATA_ROOT_EXCLUDE_FILES = {
    "lockfile",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    ".profile_runtime.lock",
}
SPLIT_PROFILE_CACHE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
}
SPLIT_PROFILE_EXCLUDE_FILES = {
    "LOCK",
    "LOG",
    "LOG.old",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    ".profile_runtime.lock",
}
PROFILE_RUNTIME_LOCK_FILENAME = ".profile_runtime.lock"
BUILTIN_KEEPALIVE_SITE_METADATA = {
    "chatgpt": {
        "site_id": "chatgpt",
        "display_name": "ChatGPT",
        "home_url": "https://chatgpt.com/",
        "icon_url": "https://chatgpt.com/favicon.ico",
        "builtin": True,
    },
    "gmail": {
        "site_id": "gmail",
        "display_name": "Gmail",
        "home_url": "https://mail.google.com/",
        "icon_url": "https://mail.google.com/favicon.ico",
        "builtin": True,
    },
    "google": {
        "site_id": "google",
        "display_name": "Google",
        "home_url": "https://www.google.com/",
        "icon_url": "https://www.google.com/favicon.ico",
        "builtin": True,
    },
    "github": {
        "site_id": "github",
        "display_name": "GitHub",
        "home_url": "https://github.com/",
        "icon_url": "https://github.com/favicon.ico",
        "builtin": True,
    },
}
_KEEPALIVE_PLUGIN_METADATA_CACHE = {
    "signature": None,
    "metadata": {},
}

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


def get_chromium_restore_prompt_suppression_args() -> List[str]:
    return list(CHROMIUM_SUPPRESS_RESTORE_PROMPT_ARGS)

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


def get_default_split_user_data_profiles_root(user_data_root: str = "") -> str:
    user_data_root_text = str(user_data_root or "").strip()
    if user_data_root_text:
        expanded_user_data_root = os.path.abspath(os.path.expanduser(user_data_root_text))
        return os.path.join(os.path.dirname(expanded_user_data_root), "UserDataSplited")
    return os.path.join(get_default_workspace_root(), "UserDataSplited")


def profile_name_to_user_data_dir_name(profile_name: str) -> str:
    profile_name = str(profile_name or "").strip()
    match = re.match(r"^Profile\s+(\d+)$", profile_name)
    if match:
        return f"UserDataProfile{match.group(1)}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "", profile_name)
    return f"UserData{slug or 'Profile'}"


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
        "user_data_profiles_root": get_default_split_user_data_profiles_root(os.path.join(workspace_root, "user-data")),
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
            "concurrency_mode": "per_profile_live",
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
            "worker_policy": "sticky",
            "headless": False,
            "start_minimized": False,
            "api_token": "",
        },
        "control": {
            "enabled": True,
            "host": "127.0.0.1",
            "path": "/_control",
            "api_token": "",
        },
        "logging": {
            "level": "info",
            "retention_days": 7,
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
            "plugin_dirs": [],
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
        "profile_plugins": {},
    }


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def build_runtime_config_overrides(
    config: Dict,
    *,
    headless: Optional[bool] = None,
    start_minimized: Optional[bool] = None,
    mute_audio: Optional[bool] = None,
    incognito: Optional[bool] = None,
    window_size: str = "",
    extra_args: Optional[Sequence[str]] = None,
    engine_name: str = "",
) -> Dict:
    normalized = normalize_config(config)
    runtime_config = copy.deepcopy(normalized)
    runtime_config.setdefault("mcp", {})
    runtime_config.setdefault("launch", {})
    runtime_config.setdefault("app", {})
    if headless is not None:
        runtime_config["mcp"]["headless"] = bool(headless)
    if start_minimized is not None:
        runtime_config["mcp"]["start_minimized"] = bool(start_minimized)
    if str(window_size or "").strip():
        runtime_config["launch"]["window_size"] = str(window_size).strip()
    merged_extra_args = [str(item).strip() for item in runtime_config["launch"].get("extra_args", []) if str(item).strip()]
    if incognito is not None:
        merged_extra_args = [item for item in merged_extra_args if item != "--incognito"]
        if bool(incognito):
            merged_extra_args.append("--incognito")
    if mute_audio:
        if "--mute-audio" not in merged_extra_args:
            merged_extra_args.append("--mute-audio")
    if isinstance(extra_args, Sequence) and not isinstance(extra_args, (str, bytes)):
        for item in extra_args:
            text = str(item or "").strip()
            if text and text not in merged_extra_args:
                merged_extra_args.append(text)
    runtime_config["launch"]["extra_args"] = merged_extra_args
    if str(engine_name or "").strip():
        runtime_config["app"]["browser_engine"] = normalize_browser_engine_name(str(engine_name).strip())
    return runtime_config



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
    override = str(os.environ.get("CHROMIUM_PROFILE_MANAGER_STATE_DIR", "") or "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        os.makedirs(path, exist_ok=True)
        return path
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


def is_process_alive(pid: int) -> bool:
    try:
        pid = int(pid or 0)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def read_lockfile_payload(lock_path: str) -> Dict:
    normalized = os.path.abspath(os.path.expanduser(str(lock_path or "").strip()))
    if not normalized or not os.path.exists(normalized):
        return {}
    try:
        with open(normalized, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def clear_stale_lockfile(lock_path: str, stale_seconds: int = 12 * 60 * 60) -> bool:
    normalized = os.path.abspath(os.path.expanduser(str(lock_path or "").strip()))
    if not normalized or not os.path.exists(normalized):
        return False
    try:
        payload = read_lockfile_payload(normalized)
        pid = int(payload.get("pid", 0) or 0)
        updated_at_ts = float(payload.get("updated_at_ts", 0.0) or 0.0)
        mtime = os.path.getmtime(normalized)
        age = time.time() - (updated_at_ts or mtime)
        if pid > 0 and not is_process_alive(pid):
            os.remove(normalized)
            return True
        if age > max(1, int(stale_seconds or 1)):
            os.remove(normalized)
            return True
    except OSError:
        pass
    return False


def get_keepalive_plugin_root() -> str:
    path = os.path.join(get_state_storage_dir(), "keepalive_plugins")
    os.makedirs(path, exist_ok=True)
    return path


def get_keepalive_icon_cache_dir() -> str:
    path = os.path.join(get_state_storage_dir(), "keepalive_site_icons")
    os.makedirs(path, exist_ok=True)
    return path


def safe_copy(value):
    return copy.deepcopy(value)


def normalize_site_id(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text)
    return text.strip("._-")


def _iter_keepalive_plugin_dirs(config: Optional[Dict] = None) -> List[str]:
    configured = []
    if isinstance(config, dict):
        keepalive = config.get("keepalive", {})
        if isinstance(keepalive, dict) and isinstance(keepalive.get("plugin_dirs"), list):
            configured.extend(str(item) for item in keepalive.get("plugin_dirs", []) if str(item or "").strip())
    return unique_paths([get_keepalive_plugin_root(), *configured])


def _get_keepalive_plugin_signature(config: Optional[Dict] = None) -> List:
    signature = []
    for plugin_dir in _iter_keepalive_plugin_dirs(config):
        if not os.path.isdir(plugin_dir):
            signature.append((plugin_dir, "missing"))
            continue
        files = []
        for path in sorted(glob.glob(os.path.join(plugin_dir, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            try:
                stat = os.stat(path)
                files.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
            except OSError:
                files.append((path, "unreadable"))
        signature.append((plugin_dir, tuple(files)))
    return signature


def _normalize_keepalive_plugin_payload(raw: Dict, source: str = "") -> Dict:
    payload = dict(raw) if isinstance(raw, dict) else {}
    site_id = normalize_site_id(payload.get("site_id") or payload.get("id") or payload.get("name"))
    if not site_id:
        return {}
    home_url = str(payload.get("home_url", "") or "").strip()
    icon_url = str(payload.get("icon_url", "") or "").strip()
    if not icon_url and home_url:
        parsed = urlparse(home_url)
        if parsed.scheme and parsed.netloc:
            icon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    return {
        "site_id": site_id,
        "display_name": str(payload.get("display_name") or payload.get("label") or site_id.title()).strip(),
        "home_url": home_url,
        "icon_url": icon_url,
        "builtin": bool(payload.get("builtin", False)),
        "source": source,
        "module_name": str(payload.get("module_name", "") or "").strip(),
        "function_name": str(payload.get("function_name", "keepalive") or "keepalive").strip(),
        "class_name": str(payload.get("class_name", "") or "").strip(),
        "plugin_type": str(payload.get("plugin_type", "") or "").strip(),
        "load_error": str(payload.get("load_error", "") or "").strip(),
    }


def _extract_keepalive_plugin_metadata_from_module(module, path: str, module_name: str) -> Dict:
    raw = None
    if hasattr(module, "get_plugin"):
        raw = module.get_plugin()
    elif hasattr(module, "SITE_PLUGIN"):
        raw = module.SITE_PLUGIN
    elif hasattr(module, "PLUGIN"):
        raw = module.PLUGIN
    elif hasattr(module, "KeepalivePlugin"):
        plugin_class = getattr(module, "KeepalivePlugin")
        if inspect.isclass(plugin_class):
            instance = plugin_class()
            if hasattr(instance, "get_plugin"):
                raw = instance.get_plugin()
            else:
                raw = getattr(instance, "metadata", None)
            if isinstance(raw, dict):
                raw = dict(raw)
                raw.setdefault("class_name", "KeepalivePlugin")
                raw.setdefault("function_name", "keepalive")
                raw.setdefault("plugin_type", "class")
    if not isinstance(raw, dict):
        return {}
    raw = dict(raw)
    raw["source"] = path
    raw["module_name"] = module_name
    return _normalize_keepalive_plugin_payload(raw, source=path)


def discover_external_keepalive_site_metadata(config: Optional[Dict] = None) -> Dict[str, Dict]:
    signature = _get_keepalive_plugin_signature(config)
    if _KEEPALIVE_PLUGIN_METADATA_CACHE["signature"] == signature:
        return {site_id: dict(meta) for site_id, meta in _KEEPALIVE_PLUGIN_METADATA_CACHE["metadata"].items()}

    discovered: Dict[str, Dict] = {}
    for plugin_dir in _iter_keepalive_plugin_dirs(config):
        if not os.path.isdir(plugin_dir):
            continue
        for path in sorted(glob.glob(os.path.join(plugin_dir, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            module_name = f"chromium_advanced_user_keepalive_{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                metadata = _extract_keepalive_plugin_metadata_from_module(module, path, module_name)
                if metadata:
                    discovered[metadata["site_id"]] = metadata
            except Exception as exc:
                site_id = normalize_site_id(os.path.splitext(os.path.basename(path))[0])
                if site_id:
                    discovered[site_id] = _normalize_keepalive_plugin_payload(
                        {
                            "site_id": site_id,
                            "display_name": site_id.replace("_", " ").title(),
                            "source": path,
                            "module_name": module_name,
                            "plugin_type": "external",
                            "load_error": str(exc),
                        },
                        source=path,
                    )
    _KEEPALIVE_PLUGIN_METADATA_CACHE["signature"] = signature
    _KEEPALIVE_PLUGIN_METADATA_CACHE["metadata"] = {site_id: dict(meta) for site_id, meta in discovered.items()}
    return discovered


def get_keepalive_site_registry(config: Optional[Dict] = None) -> Dict[str, Dict]:
    registry = {site_id: dict(meta) for site_id, meta in BUILTIN_KEEPALIVE_SITE_METADATA.items()}
    registry.update(discover_external_keepalive_site_metadata(config))
    if isinstance(config, dict):
        for profile in config.get("profiles", []):
            keepalive_sites = profile.get("keepalive_sites", {})
            if isinstance(keepalive_sites, dict):
                for site_id, enabled in keepalive_sites.items():
                    normalized = normalize_site_id(site_id)
                    if normalized and bool(enabled) and normalized not in registry:
                        registry[normalized] = {
                            "site_id": normalized,
                            "display_name": normalized.title(),
                            "home_url": "",
                            "icon_url": "",
                            "builtin": False,
                        }
            last_keepalive_details = profile.get("last_keepalive_details", {})
            if isinstance(last_keepalive_details, dict):
                for site_id in last_keepalive_details:
                    normalized = normalize_site_id(site_id)
                    if normalized and normalized not in registry:
                        registry[normalized] = {
                            "site_id": normalized,
                            "display_name": normalized.title(),
                            "home_url": "",
                            "icon_url": "",
                            "builtin": False,
                        }
    return registry


def get_keepalive_plugin_records(config: Optional[Dict] = None) -> List[Dict]:
    records = []
    for site_id in get_keepalive_site_ids(config):
        metadata = dict(get_keepalive_site_registry(config).get(site_id, {}))
        if not metadata:
            continue
        metadata.setdefault("site_id", site_id)
        metadata["plugin_type"] = "system" if metadata.get("builtin") else "external"
        metadata["editable"] = not metadata.get("builtin")
        if metadata.get("builtin") and not metadata.get("source"):
            action = BUILTIN_KEEPALIVE_SITE_ACTIONS.get(site_id)
            if action:
                metadata["source"] = f"builtin::{action.__name__}"
        records.append(metadata)
    return records


def get_keepalive_site_ids(config: Optional[Dict] = None) -> List[str]:
    registry = get_keepalive_site_registry(config)
    ordered = [site_id for site_id in KEEPALIVE_SITE_ORDER if site_id in registry]
    extras = sorted(site_id for site_id in registry if site_id not in ordered)
    return ordered + extras


def get_keepalive_site_label(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    return str(get_keepalive_site_registry(config).get(normalized, {}).get("display_name") or normalized.title())


def get_keepalive_plugin_root_for_site(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    registry = get_keepalive_site_registry(config)
    metadata = registry.get(normalized, {})
    source = str(metadata.get("source", "") or "").strip()
    if source:
        return source
    return os.path.join(get_keepalive_plugin_root(), f"{normalized}.py")


def get_keepalive_plugin_source_text(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    if normalized in BUILTIN_KEEPALIVE_SITE_ACTIONS:
        action = BUILTIN_KEEPALIVE_SITE_ACTIONS[normalized]
        try:
            return inspect.getsource(action).strip() + "\n"
        except (OSError, IOError, TypeError):
            return build_builtin_keepalive_plugin_reference_source(normalized)
    source_path = get_keepalive_plugin_root_for_site(normalized, config)
    if not source_path or not os.path.exists(source_path):
        raise FileNotFoundError(f"keepalive plugin source not found for {normalized}: {source_path}")
    with open(source_path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_builtin_keepalive_plugin_reference_source(site_id: str) -> str:
    normalized = normalize_site_id(site_id)
    if normalized == "google":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "google",
                    "display_name": "Google",
                    "home_url": "https://www.google.com/",
                    "icon_url": "https://www.google.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    query = str(settings.get("google_query", "")).strip() or "profile keepalive"
                    browser.goto("https://www.google.com/")
                    browser.wait_ready()

                    if browser.exists("a[href*='ServiceLogin']", by="css", timeout=0):
                        return results.signed_out("Google is not signed in for this profile.")

                    browser.fill("textarea[name='q']", query, by="css", timeout=10)
                    browser.press(Keys.ENTER)
                    browser.wait_ready()
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success(f"search results loaded for query: {query}")
            """
        )
    if normalized == "gmail":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "gmail",
                    "display_name": "Gmail",
                    "home_url": "https://mail.google.com/",
                    "icon_url": "https://mail.google.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://mail.google.com/")
                    browser.wait_ready()

                    current_url = browser.current_url().lower()
                    if "service=mail" in current_url or "accounts.google.com" in current_url:
                        return results.signed_out("Gmail is not signed in for this profile.")

                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("gmail inbox loaded")
            """
        )
    if normalized == "github":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "github",
                    "display_name": "GitHub",
                    "home_url": "https://github.com/",
                    "icon_url": "https://github.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://github.com/")
                    browser.wait_ready()

                    user_login = browser.execute(
                        "const meta = document.querySelector('meta[name=\"user-login\"]'); return meta ? meta.content || '' : '';"
                    )
                    if not str(user_login or "").strip():
                        return results.signed_out("GitHub is not signed in for this profile.")

                    browser.goto("https://github.com/pulls")
                    browser.wait_ready()
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("pull requests page loaded")
            """
        )
    if normalized == "chatgpt":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "chatgpt",
                    "display_name": "ChatGPT",
                    "home_url": "https://chatgpt.com/",
                    "icon_url": "https://chatgpt.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://chatgpt.com/")
                    browser.wait_ready()

                    current_url = browser.current_url().lower()
                    if "auth" in current_url or "login" in current_url:
                        return results.signed_out("ChatGPT is not signed in for this profile.")

                    prompt = str(settings.get("chatgpt_prompt", "")).strip() or "Reply with one word: alive"
                    browser.fill("#prompt-textarea", prompt, by="css", timeout=15)
                    browser.press(Keys.ENTER)
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("prompt sent and reply flow observed")
            """
        )
    return build_keepalive_plugin_template(normalized, normalized.replace("_", " ").title(), f"https://example.com/{normalized}")


def build_keepalive_plugin_template(site_id: str, display_name: str = "", home_url: str = "") -> str:
    normalized = normalize_site_id(site_id) or "example_site"
    label = str(display_name or normalized.replace("_", " ").title()).strip()
    home = str(home_url or f"https://example.com/{normalized}").strip()
    parsed = urlparse(home)
    icon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico" if parsed.scheme and parsed.netloc else ""
    return textwrap.dedent(
        f"""\
        class KeepalivePlugin:
            metadata = {{
                "site_id": "{normalized}",
                "display_name": "{label}",
                "home_url": "{home}",
                "icon_url": "{icon_url}",
            }}

            def get_plugin(self):
                return dict(self.metadata)

            def keepalive(self, context):
                browser = context["browser"]
                results = context["results"]
                log = context["log"]

                browser.goto("{home}")
                browser.wait_ready()

                if "login" in browser.current_url().lower():
                    return results.signed_out("{label} is not signed in for this profile.")

                log("page opened")
                return results.success("{label} page opened")
        """
    )


def inspect_keepalive_plugin_source(site_id: str, source_text: str) -> Dict:
    normalized = normalize_site_id(site_id)
    if not normalized:
        raise ValueError("plugin site_id is required")
    source_text = str(source_text or "")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8", newline="\n") as handle:
        handle.write(source_text)
        temp_path = handle.name
    module_name = f"chromium_advanced_preview_keepalive_{hashlib.sha1((normalized + source_text).encode('utf-8')).hexdigest()[:12]}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, temp_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load keepalive plugin spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = _extract_keepalive_plugin_metadata_from_module(module, temp_path, module_name)
        if not metadata:
            raise RuntimeError("keepalive plugin does not expose metadata")
        return metadata
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def save_keepalive_plugin_source(site_id: str, source_text: str, config: Optional[Dict] = None) -> Dict:
    normalized = normalize_site_id(site_id)
    if not normalized:
        raise ValueError("plugin site_id is required")
    registry = get_keepalive_site_registry(config)
    if registry.get(normalized, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{normalized}' is read-only")
    metadata = inspect_keepalive_plugin_source(normalized, source_text)
    resolved_site_id = normalize_site_id(metadata.get("site_id", "")) or normalized
    if registry.get(resolved_site_id, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{resolved_site_id}' is read-only")
    source_path = get_keepalive_plugin_root_for_site(normalized, config)
    target_path = get_keepalive_plugin_root_for_site(resolved_site_id, config)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source_text)
    source_path = os.path.normcase(os.path.abspath(source_path)) if source_path else ""
    target_path_norm = os.path.normcase(os.path.abspath(target_path))
    if source_path and source_path != target_path_norm and os.path.exists(source_path):
        try:
            os.remove(source_path)
        except OSError:
            pass
    return {
        "path": target_path,
        "site_id": resolved_site_id,
        "display_name": str(metadata.get("display_name", "") or resolved_site_id),
        "home_url": str(metadata.get("home_url", "") or ""),
        "icon_url": str(metadata.get("icon_url", "") or ""),
        "previous_site_id": normalized,
    }


def delete_keepalive_plugin_source(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    registry = get_keepalive_site_registry(config)
    if registry.get(normalized, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{normalized}' cannot be deleted")
    target_path = get_keepalive_plugin_root_for_site(normalized, config)
    if not target_path or not os.path.exists(target_path):
        raise FileNotFoundError(f"keepalive plugin source not found for {normalized}: {target_path}")
    os.remove(target_path)
    return target_path


def migrate_keepalive_site_id_references(config: Dict, old_site_id: str, new_site_id: str) -> Tuple[Dict, bool]:
    normalized_old = normalize_site_id(old_site_id)
    normalized_new = normalize_site_id(new_site_id)
    if not normalized_old or not normalized_new or normalized_old == normalized_new:
        return config, False

    payload = dict(config) if isinstance(config, dict) else {}
    changed = False

    keepalive = payload.get("keepalive", {})
    if isinstance(keepalive, dict):
        enabled_sites = keepalive.get("enabled_sites")
        if isinstance(enabled_sites, dict) and normalized_old in enabled_sites:
            old_value = bool(enabled_sites.pop(normalized_old))
            enabled_sites[normalized_new] = bool(enabled_sites.get(normalized_new, False) or old_value)
            changed = True

    profiles = payload.get("profiles", [])
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            keepalive_sites = profile.get("keepalive_sites")
            if isinstance(keepalive_sites, dict) and normalized_old in keepalive_sites:
                old_value = bool(keepalive_sites.pop(normalized_old))
                if old_value or normalized_new in keepalive_sites:
                    keepalive_sites[normalized_new] = bool(keepalive_sites.get(normalized_new, False) or old_value)
                changed = True
            last_keepalive_details = profile.get("last_keepalive_details")
            if isinstance(last_keepalive_details, dict) and normalized_old in last_keepalive_details:
                old_detail = last_keepalive_details.pop(normalized_old)
                last_keepalive_details.setdefault(normalized_new, old_detail)
                changed = True
    return payload, changed


def get_keepalive_site_icon_path(site_id: str, config: Optional[Dict] = None, fetch: bool = True) -> str:
    normalized = normalize_site_id(site_id)
    if not normalized:
        return ""
    registry = get_keepalive_site_registry(config)
    metadata = registry.get(normalized, {})
    icon_url = str(metadata.get("icon_url", "") or "").strip()
    if not icon_url:
        return ""
    cache_dir = get_keepalive_icon_cache_dir()
    extension = os.path.splitext(urlparse(icon_url).path)[1].lower()
    if extension not in {".ico", ".png", ".jpg", ".jpeg", ".webp"}:
        extension = ".ico"
    target = os.path.join(cache_dir, f"{normalized}-{hashlib.sha1(icon_url.encode('utf-8')).hexdigest()[:12]}{extension}")
    if os.path.exists(target) or not fetch:
        return target if os.path.exists(target) else ""
    try:
        request = Request(icon_url, headers={"User-Agent": f"{APP_NAME}/1.0"})
        with urlopen(request, timeout=5) as response:
            data = response.read(512 * 1024)
        if data:
            with open(target, "wb") as handle:
                handle.write(data)
            return target
    except Exception:
        return ""
    return ""


def warm_keepalive_site_icon_cache(config: Optional[Dict] = None) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for site_id in get_keepalive_site_ids(config):
        path = get_keepalive_site_icon_path(site_id, config, fetch=True)
        if path:
            results[site_id] = path
    return results


def normalize_keepalive_site_flags(value, default: bool = False, site_ids: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    ordered_site_ids = list(site_ids or [])
    flags = {site_name: bool(default) for site_name in ordered_site_ids}
    if isinstance(value, dict):
        for raw_site_name, enabled in value.items():
            site_name = normalize_site_id(raw_site_name)
            if site_name:
                flags[site_name] = bool(enabled)
    return flags


def format_keepalive_sites_text(site_flags: Dict, translate: Optional[Callable[[str, str], str]] = None) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    labels = []
    normalized = normalize_keepalive_site_flags(site_flags, default=False)
    for site_name in get_keepalive_site_ids({"profiles": [{"keepalive_sites": normalized}]}):
        if normalized.get(site_name):
            labels.append(tr(f"site_name_{site_name}", get_keepalive_site_label(site_name)))
    return ", ".join(labels) if labels else "-"


def normalize_keepalive_site_result_for_display(info: Dict) -> Dict:
    payload = dict(info) if isinstance(info, dict) else {}
    status = str(payload.get("status", "") or "").strip().lower()
    message = str(payload.get("message", "") or "")
    if status == "failed" and is_browser_closed_error(RuntimeError(message)):
        payload["status"] = "attention"
        payload.setdefault("signed_in", None)
    return payload


def normalize_keepalive_action_result(site_name: str, result: Dict) -> Dict:
    payload = dict(result) if isinstance(result, dict) else {}
    status = str(payload.get("status", "") or "").strip().lower()
    if status not in {"success", "signed_out", "attention", "failed", "skipped"}:
        status = "success" if status in {"", "ok"} else "attention"
    message = str(payload.get("message", "") or "").strip()
    if not message:
        message = f"{site_name} {status}"
    signed_in = payload.get("signed_in")
    if status == "success":
        signed_in = True if signed_in is None else bool(signed_in)
    elif status == "signed_out":
        signed_in = False
    elif status in {"attention", "failed"} and signed_in is None:
        signed_in = None
    payload.update({"status": status, "message": message, "signed_in": signed_in})
    return payload


def derive_keepalive_site_presence(profile: Dict) -> Dict[str, List[str]]:
    details = profile.get("last_keepalive_details", {}) or {}
    if not isinstance(details, dict):
        details = {}

    online_sites: List[str] = []
    signed_out_sites: List[str] = []
    attention_sites: List[str] = []
    failed_sites: List[str] = []
    skipped_sites: List[str] = []
    unknown_sites: List[str] = []

    for site_name in sorted(details.keys(), key=lambda item: str(item or "").lower()):
        site_id = str(site_name or "").strip()
        if not site_id:
            continue
        payload = normalize_keepalive_action_result(site_id, details.get(site_name, {}))
        status = str(payload.get("status", "") or "").strip().lower()
        signed_in = payload.get("signed_in")
        if signed_in is True:
            online_sites.append(site_id)
            continue
        if signed_in is False or status == "signed_out":
            signed_out_sites.append(site_id)
            continue
        if status == "attention":
            attention_sites.append(site_id)
            continue
        if status == "failed":
            failed_sites.append(site_id)
            continue
        if status == "skipped":
            skipped_sites.append(site_id)
            continue
        unknown_sites.append(site_id)

    return {
        "online_sites": online_sites,
        "signed_out_sites": signed_out_sites,
        "attention_sites": attention_sites,
        "failed_sites": failed_sites,
        "skipped_sites": skipped_sites,
        "unknown_sites": unknown_sites,
    }


def format_keepalive_site_status(
    site_name: str,
    info: Dict,
    translate: Optional[Callable[[str, str], str]] = None,
) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    payload = normalize_keepalive_site_result_for_display(info)
    status = str(payload.get("status", "unknown") or "unknown").strip().lower()
    message = str(payload.get("message", "") or "").strip()
    site_label = tr(f"site_name_{site_name}", get_keepalive_site_label(site_name))
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
    normalized["user_data_dir_name"] = str(normalized.get("user_data_dir_name", "")).strip()
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
    normalized.update(derive_keepalive_site_presence(normalized))
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
    for site_name in set(merged_sites) | set(candidate_sites):
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
    loaded_has_user_data_profiles_root = isinstance(loaded_paths, dict) and "user_data_profiles_root" in loaded_paths
    loaded_has_mirror_user_data_root = isinstance(loaded_paths, dict) and "mirror_user_data_root" in loaded_paths
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
        for key in ("transport", "host", "path", "log_level", "worker_policy"):
            if key in loaded_mcp and loaded_mcp.get(key) is not None:
                normalized["mcp"][key] = str(loaded_mcp.get(key)).strip()
        if "enabled" in loaded_mcp:
            normalized["mcp"]["enabled"] = bool(loaded_mcp.get("enabled"))
        if "headless" in loaded_mcp:
            normalized["mcp"]["headless"] = bool(loaded_mcp.get("headless"))
        if "start_minimized" in loaded_mcp:
            normalized["mcp"]["start_minimized"] = bool(loaded_mcp.get("start_minimized"))
        if "api_token" in loaded_mcp and loaded_mcp.get("api_token") is not None:
            normalized["mcp"]["api_token"] = str(loaded_mcp.get("api_token")).strip()
        if "port" in loaded_mcp:
            normalized["mcp"]["port"] = loaded_mcp.get("port")
        if "worker_port" in loaded_mcp:
            normalized["mcp"]["worker_port"] = loaded_mcp.get("worker_port")
        if "idle_timeout_seconds" in loaded_mcp:
            normalized["mcp"]["idle_timeout_seconds"] = loaded_mcp.get("idle_timeout_seconds")

    loaded_control = loaded.get("control", {})
    if isinstance(loaded_control, dict):
        for key in ("host", "path"):
            if key in loaded_control and loaded_control.get(key) is not None:
                normalized["control"][key] = str(loaded_control.get(key)).strip()
        if "enabled" in loaded_control:
            normalized["control"]["enabled"] = bool(loaded_control.get("enabled"))
        if "api_token" in loaded_control and loaded_control.get("api_token") is not None:
            normalized["control"]["api_token"] = str(loaded_control.get("api_token")).strip()

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

    loaded_logging = loaded.get("logging", {})
    if isinstance(loaded_logging, dict):
        if "level" in loaded_logging and loaded_logging.get("level") is not None:
            normalized["logging"]["level"] = str(loaded_logging.get("level")).strip()
        if "retention_days" in loaded_logging:
            normalized["logging"]["retention_days"] = loaded_logging.get("retention_days")

    loaded_keepalive = loaded.get("keepalive", {})
    legacy_keepalive_sites = normalize_keepalive_site_flags(
        loaded_keepalive.get("enabled_sites", normalized["keepalive"]["enabled_sites"]) if isinstance(loaded_keepalive, dict) else {},
        default=False,
    )
    if isinstance(loaded_keepalive, dict):
        enabled_sites = loaded_keepalive.get("enabled_sites", {})
        if isinstance(enabled_sites, dict):
            normalized["keepalive"]["enabled_sites"].update(normalize_keepalive_site_flags(enabled_sites, default=False))
        plugin_dirs = loaded_keepalive.get("plugin_dirs", [])
        if isinstance(plugin_dirs, list):
            normalized["keepalive"]["plugin_dirs"] = [str(item).strip() for item in plugin_dirs if str(item or "").strip()]

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

    loaded_profile_plugins = loaded.get("profile_plugins", {})
    if isinstance(loaded_profile_plugins, dict):
        normalized_map = {}
        for profile_name, plugin_ids in loaded_profile_plugins.items():
            normalized_profile_name = str(profile_name or "").strip()
            if not normalized_profile_name or not isinstance(plugin_ids, list):
                continue
            values = []
            for plugin_id in plugin_ids:
                value = str(plugin_id or "").strip()
                if value and value not in values:
                    values.append(value)
            normalized_map[normalized_profile_name] = values
        normalized["profile_plugins"] = normalized_map

    normalized["profiles"] = sort_profiles(normalized["profiles"])
    normalized["app"]["language"] = normalize_language_code(normalized["app"].get("language", detect_default_language()))
    normalized["app"]["browser_engine"] = normalize_browser_engine_name(
        normalized["app"].get("browser_engine", DEFAULT_BROWSER_ENGINE)
    )
    concurrency_mode = str(normalized["app"].get("concurrency_mode", "per_profile_live")).strip().lower()
    if concurrency_mode == "mirror_isolated":
        concurrency_mode = "per_profile_live"
    if concurrency_mode not in {"block", "per_profile_live"}:
        concurrency_mode = "per_profile_live"
    normalized["app"]["concurrency_mode"] = concurrency_mode
    normalized["mcp"]["enabled"] = bool(normalized["mcp"].get("enabled", False))
    normalized["mcp"]["headless"] = bool(normalized["mcp"].get("headless", False))
    normalized["mcp"]["start_minimized"] = bool(normalized["mcp"].get("start_minimized", False))
    normalized["mcp"]["api_token"] = str(normalized["mcp"].get("api_token", "")).strip()
    normalized["mcp"]["transport"] = str(normalized["mcp"].get("transport", "streamable-http")).strip() or "streamable-http"
    normalized["mcp"]["host"] = str(normalized["mcp"].get("host", "127.0.0.1")).strip() or "127.0.0.1"
    normalized["mcp"]["path"] = str(normalized["mcp"].get("path", "/mcp")).strip() or "/mcp"
    if not normalized["mcp"]["path"].startswith("/"):
        normalized["mcp"]["path"] = "/" + normalized["mcp"]["path"]
    normalized["mcp"]["log_level"] = str(normalized["mcp"].get("log_level", "info")).strip() or "info"
    worker_policy = str(normalized["mcp"].get("worker_policy", "sticky")).strip().lower() or "sticky"
    if worker_policy not in {"lazy", "sticky", "always_on"}:
        worker_policy = "sticky"
    normalized["mcp"]["worker_policy"] = worker_policy
    normalized["mcp"]["port"] = max(1, min(65535, int(normalized["mcp"].get("port", 28888))))
    normalized["mcp"]["worker_port"] = max(1, min(65535, int(normalized["mcp"].get("worker_port", 28889))))
    if normalized["mcp"]["worker_port"] == normalized["mcp"]["port"]:
        normalized["mcp"]["worker_port"] = min(65535, normalized["mcp"]["port"] + 1)
    normalized["mcp"]["idle_timeout_seconds"] = max(
        10,
        int(normalized["mcp"].get("idle_timeout_seconds", 60)),
    )
    normalized["control"]["enabled"] = bool(normalized["control"].get("enabled", True))
    normalized["control"]["api_token"] = str(normalized["control"].get("api_token", "")).strip()
    normalized["control"]["host"] = str(normalized["control"].get("host", "127.0.0.1")).strip() or "127.0.0.1"
    normalized["control"]["path"] = str(normalized["control"].get("path", "/_control")).strip() or "/_control"
    if not normalized["control"]["path"].startswith("/"):
        normalized["control"]["path"] = "/" + normalized["control"]["path"]
    normalized["logging"]["level"] = str(normalized["logging"].get("level", "info")).strip().lower() or "info"
    if normalized["logging"]["level"] not in {"debug", "info", "warning", "error"}:
        normalized["logging"]["level"] = "info"
    normalized["logging"]["retention_days"] = max(1, min(365, int(normalized["logging"].get("retention_days", 7))))
    normalized["profile_plugins"] = dict(normalized.get("profile_plugins", {}) if isinstance(normalized.get("profile_plugins", {}), dict) else {})
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
    user_data_profiles_root = str(normalized["paths"].get("user_data_profiles_root", "")).strip()
    mirror_user_data_root = str(normalized["paths"].get("mirror_user_data_root", "")).strip()
    expected_split_root = get_default_split_user_data_profiles_root(user_data_root)
    expected_mirror_root = get_default_mirror_user_data_root(user_data_root)
    if not loaded_has_user_data_profiles_root or not user_data_profiles_root:
        normalized["paths"]["user_data_profiles_root"] = expected_split_root
    if (
        not loaded_has_mirror_user_data_root
        or not mirror_user_data_root
        or is_legacy_default_mirror_root(mirror_user_data_root)
    ):
        normalized["paths"]["mirror_user_data_root"] = expected_mirror_root
    return normalized


def write_json_atomic(path: str, data: Dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    with _JSON_ATOMIC_WRITE_LOCK:
        temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            last_exc = None
            for _ in range(8):
                try:
                    os.replace(temp_path, path)
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            if last_exc is not None:
                raise last_exc
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass


def load_json_file(path: str, default=None):
    normalized = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not normalized or not os.path.exists(normalized):
        return {} if default is None else default
    try:
        with open(normalized, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if data is not None else ({} if default is None else default)
    except Exception:
        return {} if default is None else default


def append_jsonl_event(path: str, payload: Dict) -> None:
    normalized = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    os.makedirs(os.path.dirname(normalized), exist_ok=True)
    with open(normalized, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_recent_jsonl_events(path: str, limit: int = 100) -> List[Dict]:
    normalized = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not normalized or not os.path.exists(normalized):
        return []
    limit = max(1, int(limit or 1))
    try:
        with open(normalized, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return []
    results: List[Dict] = []
    for raw_line in lines[-limit:]:
        raw_line = str(raw_line or "").strip()
        if not raw_line:
            continue
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


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


def get_user_data_profiles_root(config: Dict) -> str:
    normalized = normalize_config(config)
    paths = normalized.get("paths", {})
    root = str(paths.get("user_data_profiles_root", "") or "").strip()
    if root:
        return os.path.abspath(os.path.expanduser(root))
    legacy_root = str(paths.get("user_data_root", "") or "").strip()
    if legacy_root:
        return os.path.abspath(os.path.expanduser(legacy_root))
    return ""


def get_profile_record(config: Dict, profile_name: str) -> Dict:
    normalized = normalize_config(config)
    profile_name = str(profile_name or "").strip()
    for item in normalized.get("profiles", []):
        if item.get("profile_name") == profile_name:
            return item
    return {}


def get_profile_user_data_dir_name(config: Dict, profile_name: str) -> str:
    record = get_profile_record(config, profile_name)
    configured = str(record.get("user_data_dir_name", "") or "").strip()
    if configured:
        return configured
    return profile_name_to_user_data_dir_name(profile_name)


def get_profile_user_data_root(config: Dict, profile_name: str) -> str:
    normalized = normalize_config(config)
    split_root = get_user_data_profiles_root(normalized)
    user_data_dir_name = get_profile_user_data_dir_name(normalized, profile_name)
    split_candidate = os.path.join(split_root, user_data_dir_name) if split_root else ""
    if split_candidate and os.path.isdir(split_candidate):
        return split_candidate

    legacy_root = str(normalized.get("paths", {}).get("user_data_root", "") or "").strip()
    if legacy_root:
        legacy_root = os.path.abspath(os.path.expanduser(legacy_root))
        legacy_profile_dir = os.path.join(legacy_root, str(profile_name or "").strip())
        if os.path.isdir(legacy_profile_dir):
            return legacy_root

    if split_candidate and os.path.isdir(split_root):
        return split_candidate

    if split_candidate:
        return split_candidate
    return legacy_root if legacy_root else ""


def get_profile_directory_path(config: Dict, profile_name: str) -> str:
    profile_root = get_profile_user_data_root(config, profile_name)
    if not profile_root:
        return ""
    return os.path.join(profile_root, str(profile_name or "").strip())


def discover_profiles_from_split_roots(user_data_profiles_root: str) -> List[Dict]:
    root = os.path.abspath(os.path.expanduser(str(user_data_profiles_root or "").strip()))
    if not root or not os.path.isdir(root):
        return []
    results: List[Dict] = []
    for entry_name in sorted(os.listdir(root)):
        full_path = os.path.join(root, entry_name)
        if not os.path.isdir(full_path):
            continue
        if entry_name == "mirror_disk":
            continue
        profile_names = discover_profile_directories(full_path)
        if len(profile_names) != 1:
            continue
        profile_name = profile_names[0]
        results.append(
            {
                "profile_name": profile_name,
                "user_data_dir_name": entry_name,
                "user_data_root": full_path,
                "profile_dir": os.path.join(full_path, profile_name),
            }
        )
    results.sort(key=lambda item: profile_sort_key(item.get("profile_name", "")))
    return results


def sync_profiles_with_user_data(config: Dict) -> Dict:
    normalized = normalize_config(config)
    user_data_profiles_root = get_user_data_profiles_root(normalized)
    split_profiles = discover_profiles_from_split_roots(user_data_profiles_root)
    existing = {item["profile_name"]: item for item in normalized["profiles"]}

    if split_profiles:
        for discovered_profile in split_profiles:
            profile_name = str(discovered_profile.get("profile_name", "")).strip()
            if not profile_name:
                continue
            if profile_name not in existing:
                normalized["profiles"].append(
                    normalize_profile_entry(
                        {
                            "profile_name": profile_name,
                            "user_data_dir_name": discovered_profile.get("user_data_dir_name", ""),
                            "account": "",
                            "keepalive_enabled": False,
                            "keepalive_sites": {},
                        }
                    )
                )
                continue
            existing[profile_name]["user_data_dir_name"] = (
                str(discovered_profile.get("user_data_dir_name", "")).strip()
                or existing[profile_name].get("user_data_dir_name", "")
            )
    else:
        user_data_root = normalized["paths"].get("user_data_root", "")
        discovered = discover_profile_directories(user_data_root)
        for profile_name in discovered:
            if profile_name not in existing:
                normalized["profiles"].append(
                    normalize_profile_entry(
                        {
                            "profile_name": profile_name,
                            "user_data_dir_name": profile_name_to_user_data_dir_name(profile_name),
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
    normalized = normalize_config(config)
    for item in normalized.get("profiles", []):
        match = re.match(r"^Profile\s+(\d+)$", item.get("profile_name", ""))
        if match:
            highest = max(highest, int(match.group(1)))

    for discovered in discover_profiles_from_split_roots(get_user_data_profiles_root(normalized)):
        profile_name = str(discovered.get("profile_name", "")).strip()
        match = re.match(r"^Profile\s+(\d+)$", profile_name)
        if match:
            highest = max(highest, int(match.group(1)))

    return f"Profile {highest + 1}"


def ensure_profile_root_directory(profile_root: str) -> str:
    normalized = os.path.abspath(os.path.expanduser(str(profile_root or "").strip()))
    if not normalized:
        raise ValueError("Profile UserData root is empty.")
    os.makedirs(normalized, exist_ok=True)
    return normalized


def ensure_profile_directory(config_or_root, profile_name: str, user_data_dir_name: str = "") -> str:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        raise ValueError("profile_name is required")

    if isinstance(config_or_root, dict):
        normalized = normalize_config(config_or_root)
        profile_root = get_profile_user_data_root(normalized, profile_name)
    else:
        base_root = str(config_or_root or "").strip()
        if not base_root:
            raise ValueError("UserData root is empty.")
        if user_data_dir_name:
            profile_root = os.path.join(base_root, str(user_data_dir_name).strip())
        else:
            profile_root = base_root

    profile_root = ensure_profile_root_directory(profile_root)
    target = os.path.join(profile_root, profile_name)
    os.makedirs(target, exist_ok=True)
    return target


def get_profile_runtime_lock_path(config: Dict, profile_name: str) -> str:
    profile_root = get_profile_user_data_root(config, profile_name)
    if not profile_root:
        return ""
    return os.path.join(profile_root, PROFILE_RUNTIME_LOCK_FILENAME)


def _should_skip_profile_root_dir(name: str) -> bool:
    return name in SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS or bool(re.match(r"^Profile\s+\d+$", str(name or "").strip()))


def _copy_file(src_path: str, dst_path: str) -> None:
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.copy2(src_path, dst_path)


def _copy_root_state_into_profile_root(shared_root: str, target_profile_root: str) -> None:
    shared_root = os.path.abspath(os.path.expanduser(str(shared_root or "").strip()))
    target_profile_root = ensure_profile_root_directory(target_profile_root)
    if not os.path.isdir(shared_root):
        raise FileNotFoundError(f"shared UserData root not found: {shared_root}")

    for item_name in sorted(os.listdir(shared_root)):
        source_path = os.path.join(shared_root, item_name)
        if item_name in SPLIT_USER_DATA_ROOT_EXCLUDE_FILES or _should_skip_profile_root_dir(item_name):
            continue
        destination_path = os.path.join(target_profile_root, item_name)
        if os.path.isfile(source_path):
            _copy_file(source_path, destination_path)
            continue
        if not os.path.isdir(source_path):
            continue
        for dirpath, dirnames, filenames in os.walk(source_path):
            dirnames[:] = [name for name in dirnames if name not in SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS]
            rel_dir = os.path.relpath(dirpath, shared_root)
            current_target_dir = os.path.join(target_profile_root, rel_dir)
            os.makedirs(current_target_dir, exist_ok=True)
            for filename in filenames:
                if filename in SPLIT_USER_DATA_ROOT_EXCLUDE_FILES:
                    continue
                _copy_file(os.path.join(dirpath, filename), os.path.join(current_target_dir, filename))


def cleanup_profile_user_data_root(profile_root: str) -> Dict[str, int]:
    profile_root = os.path.abspath(os.path.expanduser(str(profile_root or "").strip()))
    removed_dirs = 0
    removed_files = 0
    if not os.path.isdir(profile_root):
        return {"removed_dirs": 0, "removed_files": 0}

    for dirpath, dirnames, filenames in os.walk(profile_root, topdown=True):
        retained = []
        for dirname in list(dirnames):
            full_path = os.path.join(dirpath, dirname)
            if dirname in SPLIT_PROFILE_CACHE_DIRS or dirname in SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS:
                shutil.rmtree(full_path, ignore_errors=True)
                removed_dirs += 1
                continue
            retained.append(dirname)
        dirnames[:] = retained

        for filename in filenames:
            if filename not in SPLIT_PROFILE_EXCLUDE_FILES and filename not in SPLIT_USER_DATA_ROOT_EXCLUDE_FILES:
                continue
            try:
                os.remove(os.path.join(dirpath, filename))
                removed_files += 1
            except OSError:
                pass

    return {"removed_dirs": removed_dirs, "removed_files": removed_files}


def migrate_shared_user_data_to_split_roots(
    config: Dict,
    target_root: str = "",
    logger: Optional[Callable[[str], None]] = None,
) -> Dict:
    normalized = normalize_config(config)
    shared_root = os.path.abspath(os.path.expanduser(str(normalized.get("paths", {}).get("user_data_root", "") or "").strip()))
    if not shared_root or not os.path.isdir(shared_root):
        raise FileNotFoundError(f"shared UserData root not found: {shared_root}")

    split_root = os.path.abspath(
        os.path.expanduser(
            str(target_root or normalized.get("paths", {}).get("user_data_profiles_root", "") or "").strip()
        )
    )
    if not split_root:
        raise ValueError("target split UserData root is required")
    os.makedirs(split_root, exist_ok=True)
    os.makedirs(os.path.join(split_root, "mirror_disk"), exist_ok=True)

    discovered_profiles = discover_profile_directories(shared_root)
    migrated_profiles = []
    migrated_config = copy.deepcopy(normalized)
    existing_by_name = {item.get("profile_name", ""): item for item in migrated_config.get("profiles", []) if item.get("profile_name")}

    for profile_name in discovered_profiles:
        user_data_dir_name = (
            str(existing_by_name.get(profile_name, {}).get("user_data_dir_name", "")).strip()
            or profile_name_to_user_data_dir_name(profile_name)
        )
        target_profile_root = os.path.join(split_root, user_data_dir_name)
        target_profile_dir = os.path.join(target_profile_root, profile_name)
        if logger:
            logger(f"migrating {profile_name} -> {target_profile_root}")
        if os.path.exists(target_profile_root):
            shutil.rmtree(target_profile_root, ignore_errors=True)
        ensure_profile_root_directory(target_profile_root)
        _copy_root_state_into_profile_root(shared_root, target_profile_root)
        shutil.copytree(os.path.join(shared_root, profile_name), target_profile_dir, dirs_exist_ok=True)
        cleanup_profile_user_data_root(target_profile_root)

        record = existing_by_name.get(profile_name)
        if record is None:
            record = normalize_profile_entry({"profile_name": profile_name})
            migrated_config.setdefault("profiles", []).append(record)
            existing_by_name[profile_name] = record
        record["user_data_dir_name"] = user_data_dir_name
        migrated_profiles.append(
            {
                "profile_name": profile_name,
                "user_data_dir_name": user_data_dir_name,
                "profile_root": target_profile_root,
                "profile_dir": target_profile_dir,
            }
        )

    migrated_config.setdefault("paths", {})
    migrated_config["paths"]["user_data_profiles_root"] = split_root
    migrated_config.setdefault("app", {})
    migrated_config["app"]["concurrency_mode"] = "per_profile_live"
    migrated_config["profiles"] = sort_profiles(migrated_config.get("profiles", []))
    return {
        "config": migrated_config,
        "shared_root": shared_root,
        "split_root": split_root,
        "profiles": migrated_profiles,
    }


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
    if not get_profile_user_data_root(normalized, profile_name):
        return False

    profile_dir = ensure_profile_directory(normalized, profile_name)
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
    template_path = paths.get("bookmarks_template_path", "")
    if not get_profile_user_data_root(normalized, profile_name) or not template_path:
        return False

    profile_dir = ensure_profile_directory(normalized, profile_name)
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
    for site_name in get_keepalive_site_ids({"profiles": [profile]}):
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


def normalize_fs_path(path_value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(path_value))))


def _extract_switch_value(cmdline: Sequence[str], prefix: str) -> str:
    prefix = str(prefix or "").strip().lower()
    if not prefix:
        return ""
    for item in cmdline or []:
        text = str(item or "").strip()
        lower = text.lower()
        if lower.startswith(prefix):
            return text[len(prefix):]
    return ""


def _extract_switch_values(cmdline: Sequence[str], prefix: str) -> List[str]:
    lowered_prefix = str(prefix or "").strip().lower()
    if not lowered_prefix:
        return []
    values: List[str] = []
    for item in cmdline or []:
        text = str(item or "").strip()
        lower = text.lower()
        if lower.startswith(lowered_prefix):
            values.append(text[len(lowered_prefix):])
    return values


def _classify_chromium_process_role(cmdline: Sequence[str]) -> str:
    joined = " ".join(str(item or "") for item in (cmdline or [])).lower()
    if "--type=" not in joined:
        return "browser"
    if "--type=renderer" in joined:
        return "renderer"
    if "--type=gpu-process" in joined:
        return "gpu"
    if "--type=utility" in joined:
        if "--utility-sub-type=network.mojom.networkservice" in joined:
            return "utility_network"
        if "--utility-sub-type=storage.mojom.storage_service" in joined:
            return "utility_storage"
        if "--utility-sub-type=audio.mojom.audioservice" in joined:
            return "utility_audio"
        if "ondevicemodelservice" in joined:
            return "utility_model"
        return "utility"
    if "--type=crashpad-handler" in joined:
        return "crashpad"
    return "child"


def _is_noise_only_chromium_process(role: str, cmdline: Sequence[str]) -> bool:
    normalized_role = str(role or "").strip().lower()
    if normalized_role in {
        "gpu",
        "utility",
        "utility_audio",
        "utility_model",
        "utility_network",
        "utility_storage",
        "renderer",
        "crashpad",
        "child",
    }:
        return True
    joined = " ".join(str(item or "") for item in (cmdline or [])).lower()
    if "--headless=old" in joined or "--headless=new" in joined:
        return False
    return False


def _profile_root_map(config: Dict) -> Dict[str, str]:
    normalized = normalize_config(config)
    mapping: Dict[str, str] = {}
    for item in normalized.get("profiles", []):
        profile_name = str(item.get("profile_name", "")).strip()
        profile_root = get_profile_user_data_root(normalized, profile_name)
        if not profile_name or not profile_root:
            continue
        mapping[normalize_fs_path(profile_root)] = profile_name
    return mapping


def find_running_chromium_processes(config: Dict) -> List[Dict]:
    paths = normalize_config(config).get("paths", {})
    chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
    if not chromium_binary:
        return []
    chromium_root = os.path.dirname(chromium_binary)
    binary_norm = normalize_fs_path(chromium_binary)
    root_norm = normalize_fs_path(chromium_root)
    profile_root_map = _profile_root_map(config)

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

            profile_name = profile_root_map.get(
                normalize_fs_path(_extract_switch_value(cmdline, "--user-data-dir="))
            ) or ""
            role = _classify_chromium_process_role(cmdline)
            matches.append(
                {
                    "pid": proc.info.get("pid"),
                    "name": proc.info.get("name", ""),
                    "path": matched_path,
                    "cmdline": cmdline,
                    "profile_name": profile_name,
                    "process_role": role,
                    "noise_only": _is_noise_only_chromium_process(role, cmdline),
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
                            "cmdline": [],
                            "profile_name": "",
                            "process_role": "browser",
                            "noise_only": False,
                        }
                    )
    except Exception:
        pass

    return matches


def get_chromium_processes_for_profile(config: Dict, profile_name: str) -> List[Dict]:
    return get_chromium_process_map_by_profile(config).get(str(profile_name or "").strip(), [])


def get_chromium_process_map_by_profile(config: Dict) -> Dict[str, List[Dict]]:
    normalized = normalize_config(config)
    paths = normalized.get("paths", {})
    chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
    profiles = normalized.get("profiles", [])
    entries: Dict[str, List[Dict]] = {}
    if not profiles:
        return entries

    for item in profiles:
        profile_name = str(item.get("profile_name", "") or "").strip()
        if profile_name:
            entries[profile_name] = []

    if not chromium_binary:
        return entries

    chromium_root = os.path.dirname(chromium_binary)
    binary_norm = normalize_fs_path(chromium_binary)
    root_norm = normalize_fs_path(chromium_root)
    legacy_root = str(paths.get("user_data_root", "") or "").strip()

    profile_specs = []
    for item in profiles:
        profile_name = str(item.get("profile_name", "") or "").strip()
        profile_root = get_profile_user_data_root(normalized, profile_name)
        if not profile_name or not profile_root:
            continue
        allowed_user_data_roots = {normalize_fs_path(profile_root)}
        if legacy_root:
            allowed_user_data_roots.add(normalize_fs_path(legacy_root))
        profile_specs.append(
            {
                "profile_name": profile_name,
                "profile_name_lower": profile_name.lower(),
                "profile_arg": f"--profile-directory={profile_name}".lower(),
                "allowed_user_data_roots": allowed_user_data_roots,
            }
        )

    if not profile_specs:
        return entries

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if proc.info.get("pid") == os.getpid():
                continue

            cmdline = [str(item or "") for item in (proc.info.get("cmdline") or [])]
            candidates = []
            if proc.info.get("exe"):
                candidates.append(proc.info["exe"])
            if cmdline:
                candidates.append(cmdline[0])

            binary_matched = False
            for candidate in candidates:
                if not candidate:
                    continue
                candidate_norm = normalize_fs_path(candidate)
                if candidate_norm == binary_norm:
                    binary_matched = True
                    break
                try:
                    common = os.path.commonpath([candidate_norm, root_norm])
                except ValueError:
                    common = ""
                if common == root_norm:
                    binary_matched = True
                    break

            if not binary_matched:
                continue

            joined = " ".join(cmdline)
            joined_norm = joined.replace("/", os.sep).replace("\\", os.sep).lower()
            joined_lower = joined.lower()
            extracted_user_data = _extract_switch_value(cmdline, "--user-data-dir=")
            extracted_profile_name = _extract_switch_value(cmdline, "--profile-directory=").strip()

            for spec in profile_specs:
                allowed_user_data_roots = spec["allowed_user_data_roots"]
                if extracted_user_data:
                    if normalize_fs_path(extracted_user_data) not in allowed_user_data_roots:
                        continue
                elif not any(candidate.lower() in joined_norm for candidate in allowed_user_data_roots):
                    continue

                if extracted_profile_name:
                    if extracted_profile_name.lower() != spec["profile_name_lower"]:
                        continue
                elif spec["profile_arg"] not in joined_lower and spec["profile_name_lower"] not in joined_lower:
                    continue

                role = _classify_chromium_process_role(cmdline)
                entries[spec["profile_name"]].append(
                    {
                        "pid": proc.info.get("pid"),
                        "name": proc.info.get("name", ""),
                        "path": proc.info.get("exe", "") or (cmdline[0] if cmdline else ""),
                        "cmdline": cmdline,
                        "process_role": role,
                        "noise_only": _is_noise_only_chromium_process(role, cmdline),
                    }
                )
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    for profile_name in list(entries.keys()):
        items = sorted(entries[profile_name], key=lambda item: int(item.get("pid") or 0))
        primary_items = [item for item in items if not bool(item.get("noise_only", False))]
        entries[profile_name] = primary_items or items
    return entries


def terminate_chromium_processes(processes: Sequence[Dict], logger: Optional[Callable[[str], None]] = None) -> int:
    pids = []
    for item in processes or []:
        try:
            pid = int(item.get("pid") or 0)
        except Exception:
            pid = 0
        if pid > 0 and pid not in pids:
            pids.append(pid)

    terminated = 0
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                    terminated += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            proc.kill()
            terminated += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if terminated:
        log_message(logger, f"cleaned up {terminated} owned chromium process(es)")
    return terminated


class SingleRunLock:
    def __init__(self, lock_path: str, stale_seconds: int = 12 * 60 * 60):
        self.lock_path = lock_path
        self.stale_seconds = stale_seconds
        self.acquired = False
        self.owner_pid = int(os.getpid() or 0)

    def try_acquire(self) -> bool:
        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir:
            os.makedirs(lock_dir, exist_ok=True)
        if os.path.exists(self.lock_path):
            clear_stale_lockfile(self.lock_path, stale_seconds=self.stale_seconds)

        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        now_ts = time.time()
        payload = json.dumps(
            {
                "pid": self.owner_pid,
                "time": now_text(),
                "updated_at_ts": now_ts,
            },
            ensure_ascii=False,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        self.acquired = True
        return True

    def touch(self) -> bool:
        if not self.acquired:
            return False
        now_ts = time.time()
        payload = json.dumps(
            {
                "pid": self.owner_pid,
                "time": now_text(),
                "updated_at_ts": now_ts,
            },
            ensure_ascii=False,
        )
        try:
            with open(self.lock_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            return True
        except OSError:
            return False

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


from chromium_advanced.occupancy_registry import (  # noqa: E402
    clear_profile_occupancy,
    get_occupancy_events_path,
    get_occupancy_registry_path,
    list_profile_occupancy_entries,
    load_profile_occupancy_registry,
    occupancy_entry_is_expired,
    write_profile_occupancy,
)
from chromium_advanced.mcp_runtime_config import (  # noqa: E402
    is_mcp_localhost_listening,
    mcp_auth_required,
    resolve_mcp_api_token,
    resolve_mcp_headless,
    resolve_mcp_start_minimized,
)
from chromium_advanced.keepalive_runtime import (  # noqa: E402
    BUILTIN_KEEPALIVE_SITE_ACTIONS,
    KeepAliveLoginRequiredError,
    KeepAliveSoftFailureError,
    KeepAliveStopController,
    KeepAliveStoppedError,
    KeepaliveBrowserApi,
    KeepaliveResultFactory,
    _google_results_ready,
    _open_google_search_results,
    build_page_debug_hint,
    choose_chatgpt_prompt,
    cleanup_keepalive_profile_processes,
    create_driver_for_profile,
    dismiss_google_consent_if_needed,
    find_first_interactable,
    get_last_assistant_message_text,
    interruptible_sleep,
    is_browser_closed_error,
    is_chatgpt_authenticated,
    is_interactable,
    keepalive_chatgpt,
    keepalive_github,
    keepalive_gmail,
    keepalive_google,
    list_chatgpt_sidebar_conversations,
    normalize_keepalive_locator_by,
    open_chatgpt_existing_conversation,
    run_external_keepalive_plugin,
    run_keepalive_job,
    run_keepalive_site_action,
    run_profile_keepalive,
    wait_for_any,
    wait_for_assistant_text_to_stabilize,
)


def log_message(logger: Optional[Callable[[str], None]], message: str) -> None:
    if logger:
        logger(message)


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
    user_data_root = get_profile_user_data_root(normalized, profile_name)
    if not chromium_binary or not os.path.exists(chromium_binary):
        raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
    if not user_data_root:
        raise ValueError(f"profile UserData root is required for {profile_name}")

    command = [
        chromium_binary,
        f"--user-data-dir={user_data_root}",
        f"--profile-directory={profile_name}",
    ]
    command.extend(get_chromium_restore_prompt_suppression_args())

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


def update_profile_launch_time(config: Dict, profile_name: str) -> Dict:
    normalized = normalize_config(config)
    for item in normalized["profiles"]:
        if item.get("profile_name") == profile_name:
            item["last_launch_at"] = now_text()
            break
    return normalized








from chromium_advanced.keepalive_registry import (  # noqa: E402,F401
    build_builtin_keepalive_plugin_reference_source,
    build_keepalive_plugin_template,
    delete_keepalive_plugin_source,
    discover_external_keepalive_site_metadata,
    format_keepalive_site_status,
    format_keepalive_sites_text,
    get_keepalive_icon_cache_dir,
    get_keepalive_plugin_records,
    get_keepalive_plugin_root,
    get_keepalive_plugin_root_for_site,
    get_keepalive_plugin_source_text,
    get_keepalive_site_icon_path,
    get_keepalive_site_ids,
    get_keepalive_site_label,
    get_keepalive_site_registry,
    inspect_keepalive_plugin_source,
    migrate_keepalive_site_id_references,
    normalize_keepalive_action_result,
    normalize_keepalive_site_flags,
    normalize_keepalive_site_result_for_display,
    normalize_site_id,
    safe_copy,
    save_keepalive_plugin_source,
    warm_keepalive_site_icon_cache,
)
