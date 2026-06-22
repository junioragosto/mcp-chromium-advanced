from __future__ import annotations

import json
import shutil
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import psutil

from chromium_advanced.browser_capability_kernel import enrich_capability_payload
from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary
from chromium_advanced.browser_engines import playwright_cli_tabs_pages
from chromium_advanced.chromium_profile_lib import (
    detect_fingerprint_extension_dir,
    get_chromium_restore_prompt_suppression_args,
    get_profile_directory_path,
    get_profile_user_data_root,
    get_hidden_subprocess_kwargs,
    now_text,
    resolve_chromium_binary,
    sanitize_chromium_launch_args,
)
from chromium_advanced.mcp_runtime_config import resolve_mcp_headless, resolve_mcp_start_minimized


TAB_LINE_PATTERN = re.compile(r"^- (\d+): (?:(\(current\)) )?\[(.*)\]\((.*)\)$")
CONSOLE_LINE_PATTERN = re.compile(r"^\[(?P<level>[A-Z]+)\]\s+(?P<text>.*?)(?:\s+@\s+(?P<url>.*?):(?P<line>\d+))?$")
REQUEST_LINE_PATTERN = re.compile(
    r"^(?P<index>\d+)\.\s+\[(?P<method>[A-Z]+)\]\s+(?P<url>\S+)(?:\s+=>\s+\[(?P<status>\d+)\](?:\s+.*)?)?\s*$"
)
SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
PLAYWRIGHT_CLI_AUTOMATION_BLINK_FEATURE = "AutomationControlled"
PLAYWRIGHT_CLI_BLINK_SENTINEL_ARG = "--no-first-run=--disable-blink-features-sentinel"
PLAYWRIGHT_CLI_DEFAULT_TIMEOUT_SECONDS = 20
PLAYWRIGHT_CLI_DIAGNOSTIC_TIMEOUT_SECONDS = 8


def _safe_log(text: str) -> None:
    message = str(text or "")
    data = (message + "\n").encode("utf-8", errors="replace")
    stream = getattr(sys, "stdout", None)
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except Exception:
            pass
    try:
        print(message, flush=True)
    except Exception:
        pass
PLAYWRIGHT_CLI_RAW_RESULT_LIMIT = 20000
PLAYWRIGHT_CLI_TEMP_PREFIX = "chromium-advanced-playwright-cli-"
PLAYWRIGHT_CLI_TEMP_RETENTION = 8
PLAYWRIGHT_CLI_TEMP_MAX_AGE_SECONDS = 24 * 60 * 60


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or ""))
    return cleaned.strip("-") or "profile"


def _parse_json_loose(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def _parse_nested_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[:1] in {'{', '[', '"'}:
        try:
            return json.loads(stripped)
        except Exception:
            return value
    return value


def _truncate_debug_text(value: Any, limit: int = PLAYWRIGHT_CLI_RAW_RESULT_LIMIT) -> str:
    text = str(value or "")
    bounded = max(1000, int(limit))
    if len(text) <= bounded:
        return text
    return text[:bounded] + f"\n...[truncated {len(text) - bounded} chars]"


def _selector_to_target(selector: str, by: str = "css") -> str:
    normalized_by = str(by or "css").strip().lower()
    if normalized_by == "css":
        return selector
    if normalized_by == "xpath":
        return f"xpath={selector}"
    if normalized_by == "id":
        return f"#{selector}"
    if normalized_by == "name":
        return f"[name={json.dumps(selector)}]"
    if normalized_by == "tag":
        return selector
    if normalized_by == "class":
        return f".{selector}"
    if normalized_by in {"link_text", "partial_link_text"}:
        return f"text={selector}"
    raise ValueError(f"unsupported selector type: {by}")


def _tab_id_for_index(index: int) -> str:
    return f"tab-{int(index):03d}"


def _normalize_playwright_cli_launch_args(args: List[str]) -> List[str]:
    normalized: List[str] = []
    has_disable_blink_features = False
    for item in args:
        text = str(item or "").strip()
        if not text:
            continue
        if text.startswith("--disable-blink-features"):
            name, separator, value = text.partition("=")
            features = [part.strip() for part in value.split(",") if part.strip()]
            features = [part for part in features if part != PLAYWRIGHT_CLI_AUTOMATION_BLINK_FEATURE]
            if features:
                normalized.append(f"{name}{separator}{','.join(features)}")
                has_disable_blink_features = True
            continue
        normalized.append(text)
    if not has_disable_blink_features:
        normalized.append(PLAYWRIGHT_CLI_BLINK_SENTINEL_ARG)
    return normalized


def _command_line_text(proc: psutil.Process) -> str:
    try:
        cmdline = proc.cmdline()
    except Exception:
        return ""
    if isinstance(cmdline, list):
        return " ".join(str(item) for item in cmdline)
    return str(cmdline or "")


def _hostname(value: str) -> str:
    try:
        return str(urlparse(str(value or "")).hostname or "").lower()
    except Exception:
        return ""


def _normalize_url_for_compare(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.rstrip("/")
    hostname = str(parsed.hostname or "").lower()
    scheme = str(parsed.scheme or "").lower()
    if not hostname or not scheme:
        return raw.rstrip("/")
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = str(parsed.path or "/").strip() or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = str(parsed.query or "").strip()
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def _classify_console_message(level: str, text: str, url: str = "") -> Dict[str, str]:
    lowered = f"{level} {text} {url}".lower()
    category = "runtime"
    severity = "info"
    if str(level or "").lower() == "error":
        severity = "error"
    elif str(level or "").lower() in {"warning", "warn"}:
        severity = "warning"
    if any(token in lowered for token in ("font", "woff", "stylesheet")):
        category = "asset"
    elif any(token in lowered for token in ("csp", "content security policy")):
        category = "security_policy"
    elif any(token in lowered for token in ("cors", "cross-origin")):
        category = "cross_origin"
    elif any(token in lowered for token in ("intercom", "hubspot", "analytics", "gtag", "doubleclick", "ads")):
        category = "third_party"
    elif any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "signin", "login")):
        category = "auth"
    return {"category": category, "severity": severity}


def _classify_network_request(url: str, status: Optional[int], page_url: str = "") -> Dict[str, str]:
    host = _hostname(url)
    page_host = _hostname(page_url)
    path = str(urlparse(str(url or "")).path or "").lower()
    category = "network"
    severity = "info"
    if page_host and host and host != page_host and not host.endswith(f".{page_host}"):
        category = "third_party"
    if any(path.endswith(ext) for ext in (".woff", ".woff2", ".ttf", ".otf", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        category = "asset"
    if any(token in path for token in (".m3u8", ".m4s", ".ts", "/videoplayback")):
        category = "media"
    if isinstance(status, int):
        if status in {401, 403}:
            category = "auth"
            severity = "error"
        elif status >= 500:
            severity = "error"
        elif status >= 400:
            severity = "warning"
    return {"category": category, "severity": severity}


def _is_process_using_path(path: str) -> bool:
    if not path:
        return False
    needle = os.path.normcase(os.path.abspath(path))
    for proc in psutil.process_iter(["pid", "name"]):
        command_line = _command_line_text(proc)
        if command_line and needle in os.path.normcase(command_line):
            return True
    return False


def cleanup_stale_playwright_cli_temp_dirs(
    *,
    temp_root: str = "",
    retention: int = PLAYWRIGHT_CLI_TEMP_RETENTION,
    max_age_seconds: int = PLAYWRIGHT_CLI_TEMP_MAX_AGE_SECONDS,
) -> List[str]:
    root = Path(temp_root or tempfile.gettempdir())
    if not root.exists():
        return []
    candidates = [item for item in root.iterdir() if item.is_dir() and item.name.startswith(PLAYWRIGHT_CLI_TEMP_PREFIX)]
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    now_ts = time.time()
    removed: List[str] = []
    for index, directory in enumerate(candidates):
        try:
            stat = directory.stat()
        except OSError:
            continue
        too_old = (now_ts - stat.st_mtime) > max(60, int(max_age_seconds))
        beyond_retention = index >= max(0, int(retention))
        empty = not any(directory.iterdir())
        if not (empty or too_old or beyond_retention):
            continue
        full_path = str(directory)
        if _is_process_using_path(full_path):
            continue
        shutil.rmtree(full_path, ignore_errors=True)
        if not directory.exists():
            removed.append(full_path)
    return removed


class PlaywrightCliBrowserSession(BrowserSession):
    engine_name = "playwright_cli"

    def __init__(
        self,
        *,
        cli_path: str,
        cli_command: List[str],
        session_name: str,
        config_path: str,
        output_root: str,
        user_data_root: str,
        profile_name: str,
    ):
        self.cli_path = cli_path
        self.cli_command = list(cli_command or [cli_path])
        self.session_name = session_name
        self.config_path = config_path
        self.output_root = output_root
        self.user_data_root = user_data_root
        self.profile_name = profile_name
        self._last_tabs: List[Dict[str, Any]] = []
        self._console_offsets: Dict[str, int] = {"": 0}
        self._request_offsets: Dict[str, int] = {"": 0}
        self._sticky_tab_id: str = ""
        self._expected_page_by_tab: Dict[str, Dict[str, str]] = {}
        self._last_observed_page_by_tab: Dict[str, Dict[str, str]] = {}
        self._command_lock = threading.RLock()

    def _remember_page(self, tab_id: str, url: str = "", title: str = "") -> None:
        self._commit_expected_page(tab_id=tab_id, url=url, title=title)

    def _commit_expected_page(self, tab_id: str, url: str = "", title: str = "") -> None:
        normalized_tab_id = str(tab_id or "").strip()
        if not normalized_tab_id:
            return
        self._sticky_tab_id = normalized_tab_id
        page = {
            "url": str(url or "").strip(),
            "title": str(title or "").strip(),
        }
        self._expected_page_by_tab[normalized_tab_id] = dict(page)
        self._last_observed_page_by_tab[normalized_tab_id] = dict(page)

    def _observe_page(self, tab_id: str, url: str = "", title: str = "") -> None:
        normalized_tab_id = str(tab_id or "").strip()
        if not normalized_tab_id:
            return
        self._sticky_tab_id = normalized_tab_id
        self._last_observed_page_by_tab[normalized_tab_id] = {
            "url": str(url or "").strip(),
            "title": str(title or "").strip(),
        }

    def _page_memory(self, tab_id: str) -> Dict[str, str]:
        return dict(self._expected_page_by_tab.get(str(tab_id or "").strip(), {}))

    def _last_observed_page(self, tab_id: str) -> Dict[str, str]:
        return dict(self._last_observed_page_by_tab.get(str(tab_id or "").strip(), {}))

    def _build_page_drift(self, tab_id: str, current_url: str, current_title: str) -> Dict[str, Any]:
        memory = self._page_memory(tab_id)
        expected_url = str(memory.get("url", "") or "")
        expected_title = str(memory.get("title", "") or "")
        return {
            "tab_id": str(tab_id or "").strip(),
            "drifted": bool(expected_url and str(current_url or "").strip() and expected_url != str(current_url or "").strip()),
            "expected_url": expected_url,
            "current_url": str(current_url or "").strip(),
            "expected_title": expected_title,
            "current_title": str(current_title or "").strip(),
        }

    def _attach_page_drift(self, payload: Dict[str, Any], tab_id: str, url: str, title: str) -> Dict[str, Any]:
        if isinstance(payload, dict):
            payload["page_drift"] = self._build_page_drift(tab_id=tab_id, current_url=url, current_title=title)
        return payload

    def _should_promote_observed_page(self, *, action_name: str, tab_id: str, url: str, title: str) -> bool:
        normalized_action = str(action_name or "").strip().lower()
        normalized_tab_id = str(tab_id or "").strip()
        current_url = str(url or "").strip()
        current_title = str(title or "").strip()
        if not normalized_tab_id or not current_url:
            return False
        if normalized_action in {"navigate", "open_tab", "activate_tab", "close_tab"}:
            return True
        if normalized_action not in {"click", "click_target", "type_text", "type_target", "type_target_and_verify", "press_key"}:
            return False
        expected = self._page_memory(normalized_tab_id)
        expected_url = str(expected.get("url", "") or "")
        expected_title = str(expected.get("title", "") or "")
        if not expected_url:
            return True
        if expected_url == current_url:
            return False
        expected_base = expected_url.split("?", 1)[0]
        current_base = current_url.split("?", 1)[0]
        if expected_base and expected_base == current_base:
            return True
        if expected_title and current_title and expected_title == current_title:
            return True
        return False

    def _current_page_payload(self, tab_id: str = "", *, commit_expected: bool = False, action_name: str = "") -> Dict[str, Any]:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        result = self._eval_json("() => ({url: location.href, title: document.title})", tab_id=effective_tab_id)
        if not isinstance(result, dict):
            result = {}
        active_index = self._resolve_index(tab_id=effective_tab_id) if self._refresh_tabs() else 0
        resolved_tab_id = _tab_id_for_index(active_index)
        url = str(result.get("url", "") or "")
        title = str(result.get("title", "") or "")
        if commit_expected or self._should_promote_observed_page(action_name=action_name, tab_id=resolved_tab_id, url=url, title=title):
            self._commit_expected_page(resolved_tab_id, url=url, title=title)
        else:
            self._observe_page(resolved_tab_id, url=url, title=title)
        payload = {
            "tab_id": resolved_tab_id,
            "url": url,
            "title": title,
        }
        return self._attach_page_drift(payload, tab_id=resolved_tab_id, url=url, title=title)

    def _navigation_target_reached(self, target_url: str, current_url: str) -> bool:
        expected_raw = str(target_url or "").strip()
        current_raw = str(current_url or "").strip()
        if not expected_raw or not current_raw:
            return False
        if _normalize_url_for_compare(expected_raw) == _normalize_url_for_compare(current_raw):
            return True
        try:
            expected = urlparse(expected_raw)
            current = urlparse(current_raw)
        except Exception:
            return False
        if _hostname(expected_raw) != _hostname(current_raw):
            return False
        expected_path = str(expected.path or "/").strip() or "/"
        current_path = str(current.path or "/").strip() or "/"
        expected_path = expected_path.rstrip("/") or "/"
        current_path = current_path.rstrip("/") or "/"
        if expected_path != "/" and expected_path != current_path:
            return False
        expected_query = str(expected.query or "").strip()
        current_query = str(current.query or "").strip()
        if expected_query and expected_query != current_query:
            return False
        return True

    def _recover_navigation_timeout(self, *, target_url: str, tab_id: str = "", action_name: str = "navigate") -> Optional[Dict[str, Any]]:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        try:
            payload = self._current_page_payload(tab_id=effective_tab_id, commit_expected=False, action_name=action_name)
            observed_url = str(payload.get("url", "") or "")
            observed_title = str(payload.get("title", "") or "")
            resolved_tab_id = str(payload.get("tab_id", "") or effective_tab_id)
            if self._navigation_target_reached(target_url, observed_url):
                self._commit_expected_page(resolved_tab_id, url=observed_url, title=observed_title)
                return self._attach_page_drift(
                    {
                        "tab_id": resolved_tab_id,
                        "url": observed_url,
                        "title": observed_title,
                    },
                    tab_id=resolved_tab_id,
                    url=observed_url,
                    title=observed_title,
                )
        except Exception:
            pass
        try:
            tabs = self._refresh_tabs()
            for tab in tabs:
                candidate_url = str(tab.get("url", "") or "")
                if self._navigation_target_reached(target_url, candidate_url):
                    resolved_tab_id = str(tab.get("tab_id", "") or effective_tab_id)
                    observed_title = str(tab.get("title", "") or "")
                    self._commit_expected_page(resolved_tab_id, url=candidate_url, title=observed_title)
                    return self._attach_page_drift(
                        {
                            "tab_id": resolved_tab_id,
                            "url": candidate_url,
                            "title": observed_title,
                        },
                        tab_id=resolved_tab_id,
                        url=candidate_url,
                        title=observed_title,
                    )
        except Exception:
            pass
        return None

    def _parse_cli_json(self, stdout: str) -> Any:
        return _parse_json_loose(stdout)

    def _normalize_cli_error(self, result: Dict[str, Any]) -> RuntimeError:
        parsed = result.get("parsed", {})
        if isinstance(parsed, dict) and parsed.get("isError"):
            message = str(parsed.get("error") or "playwright-cli action failed").strip()
            return RuntimeError(message)
        stderr = str(result.get("stderr") or "").strip()
        stdout = str(result.get("stdout") or "").strip()
        returncode = int(result.get("returncode", 0) or 0)
        message = stderr or stdout or f"playwright-cli exited with {returncode}"
        return RuntimeError(message)

    def _run_cli(
        self,
        args: List[str],
        *,
        expect_process_success: bool = True,
        expect_action_success: bool = True,
        timeout_seconds: int = PLAYWRIGHT_CLI_DEFAULT_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        command = [*self.cli_command, f"-s={self.session_name}", *args]
        with self._command_lock:
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    cwd=self.output_root,
                    timeout=max(1, int(timeout_seconds)),
                    **get_hidden_subprocess_kwargs(),
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"playwright-cli command timed out after {max(1, int(timeout_seconds))}s: {' '.join(command)}"
                ) from exc
            result = {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "parsed": self._parse_cli_json(completed.stdout),
            }
            if expect_process_success and completed.returncode != 0:
                raise self._normalize_cli_error(result)

            if expect_action_success and isinstance(result["parsed"], dict) and result["parsed"].get("isError"):
                raise self._normalize_cli_error(result)

            return result

    def _action_error_payload(
        self,
        action_name: str,
        error: Exception,
        *,
        target: str = "",
        selector: str = "",
        by: str = "css",
        text_filter: str = "",
    ) -> Dict:
        payload = {
            "ok": False,
            "action_name": action_name,
            "error": str(error),
            "error_type": type(error).__name__,
            "target": str(target or "").strip(),
            "selector": str(selector or "").strip(),
            "by": str(by or "css"),
            "text_filter": str(text_filter or "").strip(),
        }
        try:
            payload.update(self.get_current_url())
        except Exception:
            payload.update({"url": "", "title": ""})
        return payload

    def _extract_result(self, payload: Dict[str, Any]) -> Any:
        parsed = payload.get("parsed", {})
        if isinstance(parsed, dict) and "result" in parsed:
            return _parse_nested_result(parsed.get("result"))
        if isinstance(parsed, dict) and "snapshot" in parsed:
            return parsed.get("snapshot")
        return parsed

    def _refresh_tabs(self) -> List[Dict[str, Any]]:
        payload = self._run_cli(["tab-list", "--json"])
        result = self._extract_result(payload)
        tabs: List[Dict[str, Any]] = []
        if isinstance(result, str):
            for raw_line in result.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                match = TAB_LINE_PATTERN.match(line)
                if not match:
                    continue
                index = int(match.group(1))
                active = bool(match.group(2))
                title = str(match.group(3) or "")
                url = str(match.group(4) or "")
                tabs.append(
                    {
                        "tab_id": _tab_id_for_index(index),
                        "index": index,
                        "title": title,
                        "url": url,
                        "active": active,
                        "alive": True,
                    }
                )
        self._last_tabs = tabs
        if tabs and self._sticky_tab_id and not any(str(tab.get("tab_id", "")) == self._sticky_tab_id for tab in tabs):
            active_tab = next((tab for tab in tabs if tab.get("active")), tabs[0])
            self._sticky_tab_id = str(active_tab.get("tab_id", "") or "")
        return tabs

    def _preferred_tab_id(self, tab_id: str = "") -> str:
        explicit = str(tab_id or "").strip()
        if explicit:
            return explicit
        if self._sticky_tab_id:
            tabs = self._last_tabs or self._refresh_tabs()
            if any(str(tab.get("tab_id", "")) == self._sticky_tab_id for tab in tabs):
                return self._sticky_tab_id
        tabs = self._last_tabs or self._refresh_tabs()
        active_tab = next((tab for tab in tabs if tab.get("active")), tabs[0] if tabs else {})
        resolved = str(active_tab.get("tab_id", "") or "")
        if resolved:
            self._sticky_tab_id = resolved
        return resolved

    def _resolve_index(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> int:
        tabs = self._refresh_tabs()
        if not tabs:
            raise RuntimeError("No live tabs are available in the current session.")
        normalized_tab_id = str(tab_id or "").strip()
        if normalized_tab_id:
            for tab in tabs:
                if tab.get("tab_id") == normalized_tab_id:
                    return int(tab.get("index", 0))
            raise ValueError(f"Tab not found: {normalized_tab_id}")
        if int(index) >= 0:
            for tab in tabs:
                if int(tab.get("index", -1)) == int(index):
                    return int(index)
            raise ValueError(f"Tab index out of range: {index}")
        title_filter = str(title_contains or "").strip().lower()
        if title_filter:
            for tab in tabs:
                if title_filter in str(tab.get("title", "") or "").lower():
                    return int(tab.get("index", 0))
        url_filter = str(url_contains or "").strip().lower()
        if url_filter:
            for tab in tabs:
                if url_filter in str(tab.get("url", "") or "").lower():
                    return int(tab.get("index", 0))
        for tab in tabs:
            if tab.get("active"):
                return int(tab.get("index", 0))
        return int(tabs[0].get("index", 0))

    def _select_index(self, index: int) -> None:
        self._run_cli(["tab-select", str(int(index)), "--json"])

    def _ensure_tab_selected(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> int:
        explicit_tab_id = str(tab_id or "").strip()
        has_explicit_target = bool(explicit_tab_id) or int(index) >= 0 or bool(str(title_contains or "").strip()) or bool(str(url_contains or "").strip())
        resolved_tab_id = explicit_tab_id if explicit_tab_id else ("" if has_explicit_target else self._preferred_tab_id())
        resolved_index = self._resolve_index(tab_id=resolved_tab_id, index=index, title_contains=title_contains, url_contains=url_contains)
        self._select_index(resolved_index)
        self._sticky_tab_id = _tab_id_for_index(resolved_index)
        return resolved_index

    def _build_eval_function(self, script: str) -> str:
        stripped = str(script or "").strip()
        if not stripped:
            raise ValueError("script is required")
        if "=>" in stripped or stripped.startswith("function") or stripped.startswith("async "):
            return stripped
        if stripped.startswith("return ") or "\n" in stripped or ";" in stripped:
            return f"() => {{ {stripped} }}"
        return f"() => ({stripped})"

    def _build_safe_eval_function(self, script: str) -> str:
        inner = self._build_eval_function(script)
        return (
            "() => {"
            " const __fn = (" + inner + ");"
            " const __safe = (value, depth = 0) => {"
            "   if (value === null || value === undefined) return value ?? null;"
            "   if (depth > 4) return String(value);"
            "   const t = typeof value;"
            "   if (t === 'string' || t === 'number' || t === 'boolean') return value;"
            "   if (t === 'bigint') return String(value);"
            "   if (t === 'function' || t === 'symbol') return String(value);"
            "   if (Array.isArray(value)) return value.slice(0, 200).map(item => __safe(item, depth + 1));"
            "   if (value instanceof Date) return value.toISOString();"
            "   if (value instanceof Error) return { name: value.name || 'Error', message: value.message || String(value) };"
            "   if (value && value.nodeType) {"
            "     return {"
            "       tag_name: String(value.tagName || '').toLowerCase(),"
            "       id: String(value.id || ''),"
            "       class: String(value.className || ''),"
            "       text: String(value.innerText || value.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 1000)"
            "     };"
            "   }"
            "   if (t === 'object') {"
            "     const out = {};"
            "     for (const [k, v] of Object.entries(value).slice(0, 200)) out[String(k)] = __safe(v, depth + 1);"
            "     return out;"
            "   }"
            "   return String(value);"
            " };"
            " const __result = __fn();"
            " return __safe(__result);"
            " }"
        )

    def _read_target_value(self, target: str) -> str:
        details = self._describe_target_via_eval(target)
        return str(details.get("value", "") or "")

    def _eval_json(self, func_text: str, tab_id: str = "") -> Any:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        if effective_tab_id:
            self._ensure_tab_selected(tab_id=effective_tab_id)
        payload = self._run_cli(["eval", func_text, "--json"])
        return self._extract_result(payload)

    def _page_text_via_dom_chunks(self, tab_id: str = "") -> str:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        result = self._eval_json(
            "() => {"
            " const root = document.body || document.documentElement;"
            " if (!root) return '';"
            " const parts = [];"
            " const push = (text) => {"
            "   const normalized = String(text || '').replace(/\\s+/g, ' ').trim();"
            "   if (!normalized) return;"
            "   if (parts.includes(normalized)) return;"
            "   parts.push(normalized);"
            " };"
            " const selectors = ["
            "   '[role=\"main\"] *',"
            "   'main *',"
            "   'ytcp-comment-thread',"
            "   '[data-legacy-thread-id]',"
            "   'tr',"
            "   '[role=\"row\"]',"
            "   'article',"
            "   'section'"
            " ];"
            " for (const selector of selectors) {"
            "   const nodes = Array.from(document.querySelectorAll(selector));"
            "   for (const node of nodes.slice(0, 400)) push(node.innerText || node.textContent || '');"
            "   if (parts.length >= 200) break;"
            " }"
            " if (!parts.length) push(root.innerText || root.textContent || '');"
            " return parts.slice(0, 200).join('\\n');"
            " }",
            tab_id=effective_tab_id,
        )
        return str(result or "")

    def _describe_target_via_eval(self, target: str) -> Dict[str, Any]:
        payload = self._run_cli(
            [
                "eval",
                "(element) => { const rect = element.getBoundingClientRect(); return {tag_name: (element.tagName || '').toLowerCase(), text: (element.innerText || element.textContent || '').trim(), value: 'value' in element ? (element.value || '') : '', visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length), enabled: !element.disabled, checked: 'checked' in element ? !!element.checked : null, selected: 'value' in element ? (element.value || '') : '', id: element.id || '', name: element.getAttribute('name') || '', class: element.getAttribute('class') || '', aria_label: element.getAttribute('aria-label') || '', role: element.getAttribute('role') || '', href: element.getAttribute('href') || '', outer_html: element.outerHTML || '', box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height} }; }",
                target,
                "--json",
            ]
        )
        result = self._extract_result(payload)
        return result if isinstance(result, dict) else {"raw": result}

    def _eval_on_target(self, target: str, func_text: str) -> Any:
        payload = self._run_cli(["eval", func_text, target, "--json"])
        return self._extract_result(payload)

    def _can_use_fast_dom_path(self, target: str, by: str = "css") -> bool:
        normalized_by = str(by or "css").strip().lower()
        if normalized_by not in {"css", "id", "name", "tag", "class"}:
            return False
        normalized_target = str(target or "").strip()
        return bool(normalized_target) and not SNAPSHOT_REF_PATTERN.match(normalized_target)

    def _fast_dom_click(self, target: str, *, double_click: bool = False) -> Dict[str, Any]:
        result = self._eval_json(
            (
                "() => { const element=document.querySelector(%SELECTOR%);"
                " if(!element) return {ok:false,reason:'target_not_found'};"
                " const visible=!!(element.offsetWidth||element.offsetHeight||element.getClientRects().length);"
                " const disabled=!!element.disabled||element.getAttribute('aria-disabled')==='true';"
                " if(!visible) return {ok:false,reason:'target_not_visible'};"
                " if(disabled) return {ok:false,reason:'target_disabled'};"
                " element.scrollIntoView({block:'center',inline:'center'});"
                " const dbl=%DOUBLE_CLICK%;"
                " if(typeof element.click==='function'&&!dbl){element.click();}"
                " else {element.dispatchEvent(new MouseEvent(dbl?'dblclick':'click',{bubbles:true,cancelable:true,view:window}));}"
                " return {ok:true,tag_name:(element.tagName||'').toLowerCase(),id:element.id||'',text:(element.innerText||element.textContent||'').trim().slice(0,200)}; }"
            ).replace("%SELECTOR%", json.dumps(str(target))).replace("%DOUBLE_CLICK%", "true" if double_click else "false"),
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "fast_dom_click_failed"
            raise RuntimeError(str(reason or "fast_dom_click_failed"))
        return result

    def _fast_dom_fill(self, target: str, text: str, *, submit: bool = False) -> Dict[str, Any]:
        result = self._eval_json(
            (
                "() => { const element=document.querySelector(%SELECTOR%);"
                " if(!element) return {ok:false,reason:'target_not_found'};"
                " const value=%TEXT%;"
                " const visible=!!(element.offsetWidth||element.offsetHeight||element.getClientRects().length);"
                " const disabled=!!element.disabled||element.getAttribute('aria-disabled')==='true';"
                " if(!visible) return {ok:false,reason:'target_not_visible'};"
                " if(disabled||element.readOnly) return {ok:false,reason:'target_disabled'};"
                " element.scrollIntoView({block:'center',inline:'center'}); element.focus();"
                " if('value' in element){element.value=value;} else {element.textContent=value;}"
                " element.dispatchEvent(new Event('input',{bubbles:true}));"
                " element.dispatchEvent(new Event('change',{bubbles:true}));"
                " if(%SUBMIT%){const form=element.form||element.closest('form');"
                " if(form&&typeof form.requestSubmit==='function') form.requestSubmit();"
                " else element.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));}"
                " return {ok:true,value:'value' in element?String(element.value||''):String(element.textContent||'')}; }"
            ).replace("%SELECTOR%", json.dumps(str(target))).replace("%TEXT%", json.dumps(str(text))).replace("%SUBMIT%", "true" if submit else "false"),
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("reason") if isinstance(result, dict) else "fast_dom_fill_failed"
            raise RuntimeError(str(reason or "fast_dom_fill_failed"))
        return result

    def _global_console_offset(self, tab_id: str = "") -> int:
        return int(self._console_offsets.get(str(tab_id or "").strip(), 0))

    def _global_request_offset(self, tab_id: str = "") -> int:
        return int(self._request_offsets.get(str(tab_id or "").strip(), 0))

    def get_summary(self) -> BrowserSessionSummary:
        try:
            result = self._current_page_payload()
            if isinstance(result, dict):
                return BrowserSessionSummary(
                    current_url=str(result.get("url", "") or ""),
                    title=str(result.get("title", "") or ""),
                    alive=True,
                )
        except Exception:
            pass
        try:
            tabs = self._refresh_tabs()
            if tabs:
                active_tab = next((tab for tab in tabs if tab.get("active")), tabs[0])
                return BrowserSessionSummary(
                    current_url=str(active_tab.get("url", "") or ""),
                    title=str(active_tab.get("title", "") or ""),
                    alive=True,
                )
        except Exception:
            pass
        if self._last_tabs:
            active_tab = next((tab for tab in self._last_tabs if tab.get("active")), self._last_tabs[0])
            return BrowserSessionSummary(
                current_url=str(active_tab.get("url", "") or ""),
                title=str(active_tab.get("title", "") or ""),
                alive=True,
            )
        return BrowserSessionSummary(alive=False)

    def get_capabilities(self) -> Dict:
        return enrich_capability_payload({
            "engine_name": self.engine_name,
            "supports_snapshot": True,
            "supports_snapshot_refs": False,
            "supports_target_actions": True,
            "supports_selector_actions": True,
            "supports_highlight": False,
            "supports_coordinates": False,
            "supports_gesture_actions": False,
            "supports_post_action_context": False,
            "supports_tabs": True,
            "supports_console_messages": True,
            "supports_page_errors": True,
            "supports_network_requests": True,
        })

    def list_tabs(self) -> Dict:
        return playwright_cli_tabs_pages.list_tabs(self)

    def open_tab(
        self,
        url: str = "",
        activate: bool = True,
        wait_for_ready: bool = True,
        timeout_seconds: int = 20,
    ) -> Dict:
        return playwright_cli_tabs_pages.open_tab(
            self,
            url=url,
            activate=activate,
            wait_for_ready=wait_for_ready,
            timeout_seconds=timeout_seconds,
        )

    def activate_tab(
        self,
        tab_id: str = "",
        index: int = -1,
        title_contains: str = "",
        url_contains: str = "",
    ) -> Dict:
        return playwright_cli_tabs_pages.activate_tab(
            self,
            tab_id=tab_id,
            index=index,
            title_contains=title_contains,
            url_contains=url_contains,
        )

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        return playwright_cli_tabs_pages.close_tab(self, tab_id=tab_id, index=index)

    def resize(self, width: int, height: int) -> Dict:
        return playwright_cli_tabs_pages.resize(self, width=width, height=height)

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return playwright_cli_tabs_pages.navigate(
            self,
            url=url,
            wait_for_ready=wait_for_ready,
            timeout_seconds=timeout_seconds,
            tab_id=tab_id,
        )

    def get_current_url(self, tab_id: str = "") -> Dict:
        return playwright_cli_tabs_pages.get_current_url(self, tab_id=tab_id)

    def get_page_text(self, tab_id: str = "") -> Dict:
        return playwright_cli_tabs_pages.get_page_text(self, tab_id=tab_id)

    def get_page_html(self, tab_id: str = "") -> Dict:
        return playwright_cli_tabs_pages.get_page_html(self, tab_id=tab_id)

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        raise NotImplementedError("inspect_elements is not implemented for playwright_cli in v1.")

    def get_active_element(self, tab_id: str = "") -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        result = self._eval_json(
            "() => { const el = document.activeElement; if (!el) return {}; return {tag_name: (el.tagName || '').toLowerCase(), text: (el.innerText || el.textContent || '').trim(), id: el.id || '', class: el.className || '', value: 'value' in el ? (el.value || '') : ''}; }",
            tab_id=effective_tab_id,
        )
        return {**self.get_current_url(tab_id=effective_tab_id), "element": result if isinstance(result, dict) else {}}

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        return {
            **self.get_current_url(tab_id=effective_tab_id),
            "interaction_context": {
                "tabs": self._refresh_tabs(),
                "active_element": self.get_active_element(tab_id=effective_tab_id).get("element", {}),
            },
        }

    def snapshot(
        self,
        target: str = "",
        by: str = "css",
        depth: int | None = None,
        boxes: bool = False,
        filename: str = "",
        tab_id: str = "",
    ) -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        if effective_tab_id:
            self._ensure_tab_selected(tab_id=effective_tab_id)
        args = ["snapshot"]
        if str(target or "").strip():
            args.append(target if SNAPSHOT_REF_PATTERN.match(str(target).strip()) else _selector_to_target(str(target).strip(), by))
        if str(filename or "").strip():
            args.append(f"--filename={str(filename).strip()}")
        if depth is not None and int(depth) > 0:
            args.append(f"--depth={int(depth)}")
        if boxes:
            args.append("--boxes")
        args.append("--json")
        payload = self._run_cli(args)
        parsed = payload.get("parsed", {})
        snapshot_text = ""
        if isinstance(parsed, dict):
            snapshot_text = str(parsed.get("snapshot", "") or "")
        elif parsed is not None:
            snapshot_text = str(parsed or "")
        if not snapshot_text.strip():
            time.sleep(0.25)
            payload = self._run_cli(args)
            parsed = payload.get("parsed", {})
        return {**self.get_current_url(tab_id=effective_tab_id), **(parsed if isinstance(parsed, dict) else {"snapshot": parsed})}

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> Dict:
        raise NotImplementedError("list_candidates is not implemented for playwright_cli in v1.")

    def execute_native_action(self, action_name: str, args: Dict[str, Any] | None = None) -> Dict:
        payload = dict(args or {})
        normalized = str(action_name or "").strip()
        if normalized == "get_current_url":
            return self.get_current_url(tab_id=str(payload.get("tab_id", "") or ""))
        if normalized == "get_page_text":
            return self.get_page_text(tab_id=str(payload.get("tab_id", "") or ""))
        if normalized == "get_page_html":
            return self.get_page_html(tab_id=str(payload.get("tab_id", "") or ""))
        if normalized == "get_interaction_context":
            return self.get_interaction_context(tab_id=str(payload.get("tab_id", "") or ""))
        if normalized == "snapshot":
            depth_value = payload.get("depth", None)
            return self.snapshot(
                target=str(payload.get("target", "") or ""),
                by=str(payload.get("by", "css") or "css"),
                depth=(int(depth_value) if depth_value not in (None, "", 0, "0") else None),
                boxes=bool(payload.get("boxes", False)),
                filename=str(payload.get("filename", "") or ""),
                tab_id=str(payload.get("tab_id", "") or ""),
            )
        raise ValueError(f"unsupported native action: {normalized}")

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        raise NotImplementedError("wait_for is not implemented for playwright_cli in v1.")

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        del timeout_seconds
        target = _selector_to_target(selector, by)
        effective_tab_id = self._preferred_tab_id()
        try:
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            result: Dict[str, Any]
            action_path = "cli"
            if self._can_use_fast_dom_path(target, by=by):
                try:
                    result = {"ok": True, "fast_dom": self._fast_dom_click(target), "action_path": "fast_dom"}
                    action_path = "fast_dom"
                except Exception:
                    payload = self._run_cli(["click", target, "--json"])
                    parsed = payload.get("parsed", {})
                    result = parsed if isinstance(parsed, dict) else {"result": parsed}
            else:
                payload = self._run_cli(["click", target, "--json"])
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
            return {
                **self._current_page_payload(tab_id=effective_tab_id, action_name="click"),
                **result,
                "clicked": True,
                "action_path": action_path,
            }
        except Exception as exc:
            return self._action_error_payload("click", exc, selector=selector, by=by, text_filter=selector)

    def click_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        double_click: bool = False,
    ) -> Dict:
        del element, timeout_seconds
        resolved_target = str(target or "").strip()
        if not SNAPSHOT_REF_PATTERN.match(resolved_target):
            resolved_target = _selector_to_target(resolved_target, by)
        command_name = "dblclick" if double_click else "click"
        effective_tab_id = self._preferred_tab_id()
        try:
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            action_path = "cli"
            if self._can_use_fast_dom_path(resolved_target, by=by):
                try:
                    result = {"ok": True, "fast_dom": self._fast_dom_click(resolved_target, double_click=double_click), "action_path": "fast_dom"}
                    action_path = "fast_dom"
                except Exception:
                    payload = self._run_cli([command_name, resolved_target, "--json"])
                    parsed = payload.get("parsed", {})
                    result = parsed if isinstance(parsed, dict) else {"result": parsed}
            else:
                payload = self._run_cli([command_name, resolved_target, "--json"])
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
            return {
                **self._current_page_payload(tab_id=effective_tab_id, action_name="click_target"),
                **result,
                "clicked": True,
                "action_path": action_path,
            }
        except Exception as exc:
            return self._action_error_payload("click_target", exc, target=target, by=by, text_filter=target)

    def type_text(
        self,
        selector: str,
        text: str,
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        del clear_first, timeout_seconds
        target = _selector_to_target(selector, by)
        effective_tab_id = self._preferred_tab_id()
        args = ["fill", target, str(text), "--json"]
        if submit:
            args.insert(3, "--submit")
        try:
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            action_path = "cli"
            if self._can_use_fast_dom_path(target, by=by):
                try:
                    fast_result = self._fast_dom_fill(target, str(text), submit=submit)
                    result = {"ok": True, "fast_dom": fast_result, "action_path": "fast_dom"}
                    actual_value = str(fast_result.get("value", "") or "")
                    action_path = "fast_dom"
                except Exception:
                    payload = self._run_cli(args)
                    parsed = payload.get("parsed", {})
                    result = parsed if isinstance(parsed, dict) else {"result": parsed}
                    actual_value = self._read_target_value(target)
            else:
                payload = self._run_cli(args)
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
                actual_value = self._read_target_value(target)
            return {
                **self._current_page_payload(tab_id=effective_tab_id, action_name="type_text"),
                **result,
                "typed": True,
                "submitted": bool(submit),
                "actual_value": actual_value,
                "value_matches": actual_value == str(text),
                "action_path": action_path,
            }
        except Exception as exc:
            try:
                time.sleep(0.2)
                if effective_tab_id:
                    self._ensure_tab_selected(tab_id=effective_tab_id)
                payload = self._run_cli(args)
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
                actual_value = self._read_target_value(target)
                return {
                    **self._current_page_payload(tab_id=effective_tab_id, action_name="type_text"),
                    **result,
                    "typed": True,
                    "submitted": bool(submit),
                    "actual_value": actual_value,
                    "value_matches": actual_value == str(text),
                    "action_path": "cli_retry",
                }
            except Exception:
                return self._action_error_payload("type_text", exc, selector=selector, by=by, text_filter=text)

    def type_target(
        self,
        target: str,
        text: str,
        element: str = "",
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        del element, clear_first, timeout_seconds
        resolved_target = str(target or "").strip()
        if not SNAPSHOT_REF_PATTERN.match(resolved_target):
            resolved_target = _selector_to_target(resolved_target, by)
        effective_tab_id = self._preferred_tab_id()
        args = ["fill", resolved_target, str(text), "--json"]
        if submit:
            args.insert(3, "--submit")
        try:
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            action_path = "cli"
            if self._can_use_fast_dom_path(resolved_target, by=by):
                try:
                    fast_result = self._fast_dom_fill(resolved_target, str(text), submit=submit)
                    result = {"ok": True, "fast_dom": fast_result, "action_path": "fast_dom"}
                    actual_value = str(fast_result.get("value", "") or "")
                    action_path = "fast_dom"
                except Exception:
                    payload = self._run_cli(args)
                    parsed = payload.get("parsed", {})
                    result = parsed if isinstance(parsed, dict) else {"result": parsed}
                    actual_value = self._read_target_value(resolved_target)
            else:
                payload = self._run_cli(args)
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
                actual_value = self._read_target_value(resolved_target)
            return {
                **self._current_page_payload(tab_id=effective_tab_id, action_name="type_target"),
                **result,
                "typed": True,
                "submitted": bool(submit),
                "actual_value": actual_value,
                "value_matches": actual_value == str(text),
                "action_path": action_path,
            }
        except Exception as exc:
            try:
                time.sleep(0.2)
                if effective_tab_id:
                    self._ensure_tab_selected(tab_id=effective_tab_id)
                payload = self._run_cli(args)
                parsed = payload.get("parsed", {})
                result = parsed if isinstance(parsed, dict) else {"result": parsed}
                actual_value = self._read_target_value(resolved_target)
                return {
                    **self._current_page_payload(tab_id=effective_tab_id, action_name="type_target"),
                    **result,
                    "typed": True,
                    "submitted": bool(submit),
                    "actual_value": actual_value,
                    "value_matches": actual_value == str(text),
                    "action_path": "cli_retry",
                }
            except Exception:
                return self._action_error_payload("type_target", exc, target=target, by=by, text_filter=text)

    def type_target_and_verify(
        self,
        target: str,
        text: str,
        element: str = "",
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        type_result = self.type_target(
            target=target,
            text=text,
            element=element,
            by=by,
            clear_first=clear_first,
            submit=submit,
            timeout_seconds=timeout_seconds,
        )
        if not type_result.get("typed"):
            return type_result
        verify_result = self.verify_target_value(target=target, expected_value=text, element=element, by=by)
        return {
            **self._current_page_payload(action_name="type_target_and_verify"),
            "typed": True,
            "verified": bool(verify_result.get("verified")),
            "submitted": bool(submit),
            "target": str(target or "").strip(),
            "type_result": type_result,
            "verify_result": verify_result,
        }

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        del timeout_seconds
        try:
            if str(selector or "").strip():
                click_result = self.click(selector, by=by)
                if not click_result.get("clicked"):
                    return click_result
            repeat = max(1, int(count))
            for _ in range(repeat):
                self._run_cli(["press", str(key), "--json"])
            return {
                **self._current_page_payload(tab_id=self._preferred_tab_id(), action_name="press_key"),
                "pressed": True,
                "key": key,
                "count": repeat,
            }
        except Exception as exc:
            return self._action_error_payload("press_key", exc, selector=selector, by=by, text_filter=key)

    def handle_dialog(self, accept: bool = True, prompt_text: str = "", tab_id: str = "") -> Dict:
        try:
            effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            if bool(accept):
                args = ["dialog-accept"]
                if str(prompt_text or ""):
                    args.append(str(prompt_text or ""))
            else:
                args = ["dialog-dismiss"]
            self._run_cli([*args, "--json"])
            return {
                **self.get_current_url(tab_id=effective_tab_id),
                "handled": True,
                "accepted": bool(accept),
                "dismissed": not bool(accept),
                "prompt_text": str(prompt_text or ""),
            }
        except Exception as exc:
            return self._action_error_payload("handle_dialog", exc, text_filter="dialog")

    def file_upload(
        self,
        target: str,
        files: list[str] | None = None,
        by: str = "css",
        element: str = "",
        timeout_seconds: int = 20,
    ) -> Dict:
        del element, timeout_seconds
        try:
            normalized_files = [str(item).strip() for item in (files or []) if str(item or "").strip()]
            if not normalized_files:
                raise ValueError("files is required")
            effective_tab_id = self._preferred_tab_id()
            if effective_tab_id:
                self._ensure_tab_selected(tab_id=effective_tab_id)
            resolved_target = str(target or "").strip()
            if SNAPSHOT_REF_PATTERN.match(resolved_target):
                raise NotImplementedError("file_upload does not support snapshot refs for playwright_cli.")
            selector_target = _selector_to_target(resolved_target, by)
            payload = self._eval_on_target(
                selector_target,
                f"(element) => {{ if (!element || String(element.tagName || '').toLowerCase() !== 'input' || String(element.getAttribute('type') || '').toLowerCase() !== 'file') throw new Error('target is not a file input'); return true; }}"
            )
            if payload is not True:
                raise ValueError("target is not a file input")
            self.click(resolved_target, by=by)
            self._run_cli(["upload", *normalized_files, "--json"])
            return {
                **self.get_current_url(),
                "uploaded": True,
                "file_count": len(normalized_files),
                "target": str(target or "").strip(),
                "by": str(by or "css"),
                "files": list(normalized_files),
            }
        except Exception as exc:
            return self._action_error_payload("file_upload", exc, target=target, by=by, text_filter=target)

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        try:
            result = self._eval_json(self._build_safe_eval_function(script), tab_id=effective_tab_id)
        except Exception:
            result = self._eval_json(self._build_eval_function(script), tab_id=effective_tab_id)
        payload = {
            **self.get_current_url(tab_id=effective_tab_id),
            "result": result,
            "script_result_state": "value" if result is not None else "null",
            "script_result_type": type(result).__name__ if result is not None else "NoneType",
        }
        if result is None:
            payload["diagnostic_hint"] = "run_script returned null."
        return payload

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        if effective_tab_id:
            self._ensure_tab_selected(tab_id=effective_tab_id)
        page = self.get_current_url(tab_id=effective_tab_id)
        resolved_tab_id = str(page.get("tab_id", "") or effective_tab_id)
        payload = self._run_cli(["console", "--json"], timeout_seconds=PLAYWRIGHT_CLI_DIAGNOSTIC_TIMEOUT_SECONDS)
        raw_result = self._extract_result(payload)
        messages: List[Dict[str, Any]] = []
        if isinstance(raw_result, str):
            for line in raw_result.splitlines():
                line = line.strip()
                if not line or line.startswith("Total messages:"):
                    continue
                match = CONSOLE_LINE_PATTERN.match(line)
                if not match:
                    continue
                message_level = str(match.group("level") or "").lower()
                message = {
                    "timestamp": 0,
                    "tab_id": resolved_tab_id,
                    "type": message_level,
                    "text": str(match.group("text") or "").strip(),
                    "location": {
                        "url": str(match.group("url") or ""),
                        "line_number": int(match.group("line")) if match.group("line") else None,
                        "column_number": None,
                    },
                }
                message.update(_classify_console_message(message_level, message["text"], str(match.group("url") or "")))
                messages.append(message)
        offset = self._global_console_offset(tab_id=effective_tab_id)
        messages = messages[offset:]
        normalized_level = str(level or "").strip().lower()
        if normalized_level:
            messages = [item for item in messages if item.get("type") == normalized_level]
        messages = messages[-max(1, int(limit)) :]
        noise_count = len([item for item in messages if item.get("category") in {"asset", "third_party", "security_policy", "cross_origin"}])
        error_count = len([item for item in messages if item.get("severity") == "error"])
        return {
            **page,
            "tab_id": resolved_tab_id,
            "level": normalized_level,
            "count": len(messages),
            "summary": {
                "error_count": error_count,
                "noise_count": noise_count,
                "signal_count": max(0, len(messages) - noise_count),
            },
            "messages": messages,
            "raw_result": _truncate_debug_text(raw_result) if isinstance(raw_result, str) else "",
        }

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        console_messages = self.get_console_messages(tab_id=effective_tab_id, limit=max(1, int(limit) * 4)).get("messages", [])
        errors = []
        for item in console_messages:
            if str(item.get("type", "") or "").lower() not in {"error"}:
                continue
            location = item.get("location", {}) if isinstance(item.get("location"), dict) else {}
            errors.append(
                {
                    "timestamp": item.get("timestamp", 0),
                    "tab_id": item.get("tab_id", ""),
                    "message": str(item.get("text", "") or ""),
                    "url": str(location.get("url", "") or ""),
                    "line_number": location.get("line_number"),
                    "column_number": location.get("column_number"),
                }
            )
        errors = errors[-max(1, int(limit)) :]
        page = self.get_current_url(tab_id=effective_tab_id)
        return {
            **page,
            "tab_id": str(page.get("tab_id", "") or effective_tab_id),
            "count": len(errors),
            "errors": errors,
        }

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        if effective_tab_id:
            self._ensure_tab_selected(tab_id=effective_tab_id)
        page = self.get_current_url(tab_id=effective_tab_id)
        resolved_tab_id = str(page.get("tab_id", "") or effective_tab_id)
        payload = self._run_cli(["requests", "--json"], timeout_seconds=PLAYWRIGHT_CLI_DIAGNOSTIC_TIMEOUT_SECONDS)
        raw_result = self._extract_result(payload)
        requests: List[Dict[str, Any]] = []
        if isinstance(raw_result, str):
            for line in raw_result.splitlines():
                line = line.strip()
                if not line or line.startswith("Note:"):
                    continue
                match = REQUEST_LINE_PATTERN.match(line)
                if not match:
                    continue
                status_value = int(match.group("status")) if match.group("status") else None
                request_item = {
                    "index": int(match.group("index")),
                    "tab_id": resolved_tab_id,
                    "event": "response" if status_value is not None else "request",
                    "method": str(match.group("method") or ""),
                    "url": str(match.group("url") or ""),
                    "status": status_value,
                    "ok": status_value is None or status_value < 400,
                    "failure": "",
                }
                request_item.update(_classify_network_request(request_item["url"], status_value, str(page.get("url", "") or "")))
                requests.append(request_item)
        offset = self._global_request_offset(tab_id=effective_tab_id)
        requests = requests[offset:]
        if bool(failed_only):
            requests = [item for item in requests if item.get("ok") is False]
        requests = requests[-max(1, int(limit)) :]
        noise_count = len([item for item in requests if item.get("category") in {"asset", "third_party", "media"}])
        error_count = len([item for item in requests if item.get("severity") == "error"])
        warning_count = len([item for item in requests if item.get("severity") == "warning"])
        return {
            **page,
            "tab_id": resolved_tab_id,
            "failed_only": bool(failed_only),
            "count": len(requests),
            "summary": {
                "error_count": error_count,
                "warning_count": warning_count,
                "noise_count": noise_count,
                "signal_count": max(0, len(requests) - noise_count),
            },
            "requests": requests,
            "raw_result": _truncate_debug_text(raw_result) if isinstance(raw_result, str) else "",
        }

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        normalized_tab_id = self._preferred_tab_id(tab_id=tab_id)
        console_payload = self._run_cli(["console", "--json"])
        raw_console = self._extract_result(console_payload)
        console_count = 0
        if isinstance(raw_console, str):
            console_count = sum(
                1 for line in raw_console.splitlines() if line.strip().startswith("[") and not line.strip().startswith("[Screenshot")
            )
        request_payload = self._run_cli(["requests", "--json"])
        raw_requests = self._extract_result(request_payload)
        request_count = 0
        if isinstance(raw_requests, str):
            request_count = sum(1 for line in raw_requests.splitlines() if REQUEST_LINE_PATTERN.match(line.strip()))
        self._console_offsets[normalized_tab_id] = console_count
        self._request_offsets[normalized_tab_id] = request_count
        if not normalized_tab_id:
            active_tab_id = self.get_current_url().get("tab_id", "")
            if active_tab_id:
                self._console_offsets[active_tab_id] = console_count
                self._request_offsets[active_tab_id] = request_count
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "cleared": True,
            "tab_id": normalized_tab_id,
        }

    def diagnose_page(self, tab_id: str = "") -> Dict:
        effective_tab_id = self._preferred_tab_id(tab_id=tab_id)
        resolved = self.get_current_url(tab_id=effective_tab_id)
        diagnostic_errors: List[Dict[str, str]] = []
        console_messages: List[Dict[str, Any]] = []
        all_requests: List[Dict[str, Any]] = []
        try:
            console_messages = self.get_console_messages(tab_id=effective_tab_id, limit=20).get("messages", [])
        except Exception as exc:
            diagnostic_errors.append({"source": "console", "error_type": type(exc).__name__, "error": str(exc)})
        page_errors = []
        for item in console_messages:
            if str(item.get("type", "") or "").lower() != "error":
                continue
            location = item.get("location", {}) if isinstance(item.get("location"), dict) else {}
            page_errors.append(
                {
                    "timestamp": item.get("timestamp", 0),
                    "tab_id": item.get("tab_id", ""),
                    "message": str(item.get("text", "") or ""),
                    "url": str(location.get("url", "") or ""),
                    "line_number": location.get("line_number"),
                    "column_number": location.get("column_number"),
                }
            )
        try:
            all_requests = self.get_network_requests(tab_id=effective_tab_id, limit=60, failed_only=False).get("requests", [])
        except Exception as exc:
            diagnostic_errors.append({"source": "network", "error_type": type(exc).__name__, "error": str(exc)})
        failed_requests = [item for item in all_requests if item.get("ok") is False]
        final_page = self.get_current_url(tab_id=effective_tab_id)
        bad_responses = [
            item
            for item in all_requests
            if isinstance(item.get("status"), int) and int(item.get("status")) >= 400
        ]
        return {
            **final_page,
            "tab_id": resolved.get("tab_id", ""),
            "diagnosis": {
                "page_drift": {
                    "started": resolved,
                    "ended": final_page,
                    "drifted": str(resolved.get("url", "") or "") != str(final_page.get("url", "") or ""),
                },
                "interaction_context": self.get_interaction_context(tab_id=effective_tab_id).get("interaction_context", {}),
                "console_messages": console_messages,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "bad_responses": bad_responses[-20:],
                "diagnostic_errors": diagnostic_errors,
                "noise_summary": {
                    "console_noise_count": len([item for item in console_messages if item.get("category") in {"asset", "third_party", "security_policy", "cross_origin"}]),
                    "network_noise_count": len([item for item in all_requests if item.get("category") in {"asset", "third_party", "media"}]),
                    "auth_issue_count": len([item for item in all_requests if item.get("category") == "auth"]),
                },
            },
        }

    def verify_text(self, text: str) -> Dict:
        try:
            page_text = self.get_page_text().get("text", "")
            if str(text) not in str(page_text):
                raise ValueError(f'Text not visible: "{text}"')
            return {**self.get_current_url(), "verified": True, "text": str(text)}
        except Exception as exc:
            return self._action_error_payload("verify_text", exc, text_filter=str(text))

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        try:
            result = self._eval_json(
                "() => { const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim(); const isVisible = el => { const style = window.getComputedStyle(el); if (!style || style.visibility === 'hidden' || style.display === 'none') return false; const rect = el.getBoundingClientRect(); return rect.width > 0 && rect.height > 0; }; const selectors = ['dialog[open]', '[role=\"dialog\"]', 'details-dialog', '.Overlay--modal', '.Popover-message']; const dialogs = []; for (const selector of selectors) { for (const el of document.querySelectorAll(selector)) { if (!isVisible(el)) continue; dialogs.push({ tag_name: (el.tagName || '').toLowerCase(), role: normalize(el.getAttribute('role') || ''), aria_label: normalize(el.getAttribute('aria-label') || ''), text: normalize(el.innerText || el.textContent || '') }); } } return dialogs; }"
            )
            dialogs = result if isinstance(result, list) else []
            expected_name = str(accessible_name or "").strip().lower()
            expected_text = str(text or "").strip().lower()
            matched = []
            for dialog in dialogs:
                dialog_name = str(dialog.get("aria_label", "") or "").strip().lower()
                dialog_text = str(dialog.get("text", "") or "").strip().lower()
                if expected_name and expected_name not in dialog_name:
                    continue
                if expected_text and expected_text not in dialog_text:
                    continue
                matched.append(dialog)
            if not matched:
                raise ValueError("Dialog not visible or did not match the expected name/text.")
            return {**self.get_current_url(), "verified": True, "count": len(matched), "dialog": matched[0], "dialogs": matched}
        except Exception as exc:
            return self._action_error_payload("verify_dialog", exc, text_filter=str(accessible_name or text or "dialog"))

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        del element
        try:
            active = self.get_active_element().get("element", {})
            if str(target or "").strip():
                resolved_target = str(target or "").strip()
                if SNAPSHOT_REF_PATTERN.match(resolved_target):
                    raise NotImplementedError("verify_active_element does not support snapshot refs for playwright_cli.")
                selector_target = _selector_to_target(resolved_target, by)
                is_active = bool(
                    self._eval_on_target(selector_target, "(element) => element === document.activeElement")
                )
                if not is_active:
                    raise ValueError(f'Active element did not match target: "{target}"')
                return {**self.get_current_url(), "verified": True, "target": str(target), "element": self._describe_target_via_eval(selector_target)}
            if not active:
                raise ValueError("No active element found.")
            return {**self.get_current_url(), "verified": True, "element": active}
        except Exception as exc:
            return self._action_error_payload("verify_active_element", exc, target=target, by=by, text_filter=target or "active element")

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        del element
        try:
            resolved_target = str(target or "").strip()
            if not SNAPSHOT_REF_PATTERN.match(resolved_target):
                resolved_target = _selector_to_target(resolved_target, by)
            details = self._describe_target_via_eval(resolved_target)
            actual_value = str(details.get("value", "") or "")
            if actual_value != str(expected_value):
                raise ValueError(
                    f'Value mismatch for target "{target}": expected "{expected_value}", got "{actual_value}"'
                )
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "expected_value": str(expected_value),
                "actual_value": actual_value,
            }
        except Exception as exc:
            return self._action_error_payload("verify_target_value", exc, target=target, by=by, text_filter=expected_value)

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        del element
        try:
            resolved_target = str(target or "").strip()
            if not SNAPSHOT_REF_PATTERN.match(resolved_target):
                resolved_target = _selector_to_target(resolved_target, by)
            details = self._describe_target_via_eval(resolved_target)
            visible = bool(details.get("visible"))
            if not visible:
                raise ValueError(f'Target not visible: "{target}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "visible": True,
                "tag_name": details.get("tag_name", ""),
                "text": details.get("text", ""),
            }
        except Exception as exc:
            return self._action_error_payload("verify_target_visible", exc, target=target, by=by, text_filter=target)

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        del element
        resolved_target = str(target or "").strip()
        result = {
            **self.get_current_url(),
            "target": resolved_target,
        }
        if SNAPSHOT_REF_PATTERN.match(resolved_target):
            result.update(
                {
                    "visible": False,
                    "enabled": False,
                    "message": "snapshot ref diagnostics are not supported for playwright_cli in v1.",
                }
            )
            return result
        selector_target = _selector_to_target(resolved_target, by)
        details = self._describe_target_via_eval(selector_target)
        result.update(
            {
                "visible": bool(details.get("visible")),
                "enabled": bool(details.get("enabled", True)),
                "tag_name": details.get("tag_name", ""),
                "text": details.get("text", ""),
                "id": details.get("id", ""),
                "name": details.get("name", ""),
                "class": details.get("class", ""),
                "aria_label": details.get("aria_label", ""),
                "role": details.get("role", ""),
                "value": details.get("value", ""),
                "href": details.get("href", ""),
                "outer_html": details.get("outer_html", ""),
            }
        )
        if include_box:
            result["box"] = details.get("box", {})
        return result

    def diagnose_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 10,
    ) -> Dict:
        del element, limit
        resolved_target = str(target or "").strip()
        diagnosis = {
            **self.get_current_url(),
            "target": resolved_target,
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "is_snapshot_ref": bool(SNAPSHOT_REF_PATTERN.match(resolved_target)),
        }
        if SNAPSHOT_REF_PATTERN.match(resolved_target):
            diagnosis.update(
                {
                    "status": "unsupported_snapshot_ref",
                    "message": "Snapshot ref diagnostics are not supported for playwright_cli in v1.",
                    "interaction_context": self.get_interaction_context().get("interaction_context", {}),
                }
            )
            return diagnosis
        try:
            diagnosis["details"] = self.describe_target(resolved_target, by=by, include_box=True)
            diagnosis["status"] = "resolved"
            diagnosis["message"] = "target resolved successfully"
        except Exception as exc:
            diagnosis["status"] = "resolve_failed"
            diagnosis["message"] = str(exc)
        diagnosis["interaction_context"] = self.get_interaction_context().get("interaction_context", {})
        return diagnosis

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        try:
            result = self._eval_json(
                f"""() => {{
                    const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
                    const expectedRole = {json.dumps(str(role or ""))};
                    const expectedName = {json.dumps(str(accessible_name or ""))};
                    const getName = el => normalize(el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('title') || '');
                    const selectorsByRole = {{
                        button: 'button, input[type=\"button\"], input[type=\"submit\"], input[type=\"reset\"], [role=\"button\"]',
                        link: 'a[href], [role=\"link\"]',
                        textbox: 'textarea, input[type=\"text\"], input[type=\"email\"], input[type=\"search\"], input[type=\"url\"], input[type=\"tel\"], input[type=\"password\"], input[type=\"number\"], [role=\"textbox\"]',
                        checkbox: 'input[type=\"checkbox\"], [role=\"checkbox\"]',
                        radio: 'input[type=\"radio\"], [role=\"radio\"]',
                        combobox: 'select, [role=\"combobox\"]',
                        dialog: 'dialog[open], [role=\"dialog\"]'
                    }};
                    const selector = selectorsByRole[expectedRole] || `[role="${{expectedRole}}"]`;
                    const nodes = Array.from(document.querySelectorAll(selector));
                    const matches = nodes.filter(el => getName(el) === expectedName && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    return matches.map(el => {{
                        const rect = el.getBoundingClientRect();
                        return {{ box: {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height }} }};
                    }});
                }}"""
            )
            matches = result if isinstance(result, list) else []
            if not matches:
                raise ValueError(f'Element not visible: role="{role}" accessible_name="{accessible_name}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "role": str(role),
                "accessible_name": str(accessible_name),
                "count": len(matches),
                "box": matches[0].get("box", {}),
            }
        except Exception as exc:
            return self._action_error_payload("verify_element", exc, text_filter=str(accessible_name or role))

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        raise NotImplementedError("highlight_target is not implemented for playwright_cli in v1.")

    def clear_highlights(self) -> Dict:
        raise NotImplementedError("clear_highlights is not implemented for playwright_cli in v1.")

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        raise NotImplementedError("mouse_move_xy is not implemented for playwright_cli in v1.")

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        raise NotImplementedError("mouse_click_xy is not implemented for playwright_cli in v1.")

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        raise NotImplementedError("mouse_drag_xy is not implemented for playwright_cli in v1.")

    def mouse_gesture_path(
        self,
        points: list[dict[str, object]],
        *,
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        raise NotImplementedError("mouse_gesture_path is not implemented for playwright_cli in v1.")

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        if str(tab_id or "").strip():
            self._ensure_tab_selected(tab_id=tab_id)
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(self.output_root, "page.png")
        payload = self._run_cli(["screenshot", f"--filename={output_path}", "--json"])
        result = payload.get("parsed", {})
        return {**self.get_current_url(tab_id=tab_id), **(result if isinstance(result, dict) else {"result": result}), "path": output_path}

    def close(self) -> None:
        try:
            self._run_cli(["close", "--json"], expect_process_success=True, expect_action_success=False)
        finally:
            self._terminate_owned_processes()
            try:
                shutil.rmtree(self.output_root, ignore_errors=True)
            except Exception:
                pass

    def _terminate_owned_processes(self) -> None:
        needles = [
            str(self.session_name or "").strip(),
            os.path.abspath(os.path.expanduser(str(self.config_path or ""))),
            os.path.abspath(os.path.expanduser(str(self.user_data_root or ""))),
        ]
        needles = [item for item in needles if item]
        if not needles:
            return

        current_pid = os.getpid()
        targets: Dict[int, psutil.Process] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.pid == current_pid:
                continue
            command_line = _command_line_text(proc)
            if not command_line:
                continue
            command_line_cmp = os.path.normcase(command_line)
            if any(os.path.normcase(needle) in command_line_cmp for needle in needles):
                targets[proc.pid] = proc
                try:
                    for child in proc.children(recursive=True):
                        targets[child.pid] = child
                except Exception:
                    pass

        if not targets:
            return
        processes = list(targets.values())
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(processes, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=3)


class PlaywrightCliEngine(BrowserEngine):
    engine_name = "playwright_cli"

    @staticmethod
    def _resolve_cli_launch_command() -> tuple[str, List[str]]:
        direct_cli = shutil.which("playwright-cli")
        cmd_cli = shutil.which("playwright-cli.cmd") or shutil.which("playwright-cli.CMD")
        if direct_cli:
            direct_path = Path(direct_cli)
            try:
                if direct_path.is_file():
                    node_exe = direct_path.parent / "node.exe"
                    cli_js = direct_path.parent / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
                    if node_exe.exists() and cli_js.exists():
                        return str(direct_path), [str(node_exe), str(cli_js)]
            except Exception:
                pass
            return str(direct_path), [str(direct_path)]
        if cmd_cli:
            cmd_path = Path(cmd_cli)
            node_exe = cmd_path.parent / "node.exe"
            cli_js = cmd_path.parent / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
            if node_exe.exists() and cli_js.exists():
                return str(cmd_path), [str(node_exe), str(cli_js)]
            return str(cmd_path), [str(cmd_path)]
        return "", []

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        cli_path, cli_command = self._resolve_cli_launch_command()
        if not cli_path:
            raise RuntimeError("playwright-cli was not found on PATH. Install it with: npm install -g @playwright/cli")
        cleanup_stale_playwright_cli_temp_dirs()

        paths = config.get("paths", {})
        launch_settings = config.get("launch", {})
        headless = resolve_mcp_headless(config)
        start_minimized = resolve_mcp_start_minimized(config)
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        user_data_root = get_profile_user_data_root(config, profile_name)
        profile_directory = get_profile_directory_path(config, profile_name)
        if not chromium_binary or not os.path.exists(chromium_binary):
            raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
        if not os.path.isdir(user_data_root):
            raise FileNotFoundError(f"Profile UserData root not found: {user_data_root}")
        if not os.path.isdir(profile_directory):
            raise FileNotFoundError(f"Profile directory not found: {profile_directory}")

        output_root = tempfile.mkdtemp(prefix=f"chromium-advanced-playwright-cli-{_slugify(profile_name)}-")
        session_name = f"playwright-cli-{_slugify(profile_name)}-{uuid.uuid4().hex[:8]}"
        config_path = os.path.join(output_root, "cli.config.json")
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            f"--profile-directory={profile_name}",
        ]
        launch_args.extend(get_chromium_restore_prompt_suppression_args())
        if start_minimized:
            launch_args.append("--start-minimized")
        elif bool(launch_settings.get("start_maximized", True)):
            launch_args.append("--start-maximized")
        window_size = str(launch_settings.get("window_size", "") or "").strip()
        if window_size:
            launch_args.append(f"--window-size={window_size}")
        extension_dir = ""
        if bool(launch_settings.get("load_fingerprint_extension", True)):
            extension_dir = detect_fingerprint_extension_dir(paths.get("fingerprint_zip_path", ""))
            if extension_dir:
                launch_args.append(f"--load-extension={extension_dir}")
        extra_args = launch_settings.get("extra_args", [])
        if isinstance(extra_args, list):
            launch_args.extend(sanitize_chromium_launch_args([str(item).strip() for item in extra_args if str(item).strip()]))
        launch_args = _normalize_playwright_cli_launch_args(launch_args)
        cli_config = {
            "browser": {
                "browserName": "chromium",
                "userDataDir": user_data_root,
                "launchOptions": {
                    "executablePath": chromium_binary,
                    "headless": bool(headless),
                    "args": launch_args,
                },
            }
        }
        try:
            with open(config_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(cli_config, handle, ensure_ascii=False, indent=2)

            _safe_log(
                f"[{now_text()}] [PLAYWRIGHT-CLI] create_session begin: "
                f"profile={profile_name} cli={cli_path} command={' '.join(cli_command)} chromium={chromium_binary} "
                f"user_data_root={user_data_root}"
            )
            command = [
                *cli_command,
                f"-s={session_name}",
                "--config",
                config_path,
                "open",
                "about:blank",
                "--persistent",
                "--json",
            ]
            if not bool(headless):
                command.append("--headed")
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                cwd=output_root,
                **get_hidden_subprocess_kwargs(),
            )
            result = {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "parsed": _parse_json_loose(completed.stdout),
            }
            if completed.returncode != 0:
                message = str(completed.stderr or completed.stdout or f"playwright-cli exited with {completed.returncode}").strip()
                raise RuntimeError(message)
            parsed = result["parsed"]
            nested_result = parsed.get("result") if isinstance(parsed, dict) else None
            if isinstance(nested_result, dict) and nested_result.get("isError"):
                raise RuntimeError(str(nested_result.get("error", "playwright-cli open failed")))

            session = PlaywrightCliBrowserSession(
                cli_path=cli_path,
                cli_command=cli_command,
                session_name=session_name,
                config_path=config_path,
                output_root=output_root,
                user_data_root=user_data_root,
                profile_name=profile_name,
            )
            _safe_log(
                f"[{now_text()}] [PLAYWRIGHT-CLI] create_session ready: profile={profile_name} session={session_name}"
            )
            return session
        except Exception:
            shutil.rmtree(output_root, ignore_errors=True)
            raise
