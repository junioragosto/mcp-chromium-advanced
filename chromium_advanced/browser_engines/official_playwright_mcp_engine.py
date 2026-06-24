from __future__ import annotations

import json
import os
import queue
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary
from chromium_advanced.browser_capability_kernel import enrich_capability_payload
from chromium_advanced.chromium_profile_lib import (
    detect_fingerprint_extension_dir,
    get_profile_directory_path,
    get_profile_user_data_root,
    resolve_chromium_binary,
    resolve_official_playwright_mcp_runtime,
    resolve_profile_extension_dirs,
)


def _json_preview(value: object, *, limit: int = 8000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    return text[:limit]


@dataclass
class _OfficialRuntimeSpec:
    node_executable: str
    chromium_binary: str
    user_data_dir: str
    profile_name: str
    extension_dirs: List[str]


class _OfficialPlaywrightMcpSessionThread:
    def __init__(self, runtime_spec: _OfficialRuntimeSpec):
        self._runtime_spec = runtime_spec
        self._thread = threading.Thread(target=self._thread_main, name="official-playwright-mcp-session", daemon=True)
        self._tasks: "queue.Queue[tuple[str, tuple, dict, queue.Queue]]" = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._startup_error: Optional[BaseException] = None
        self._driver = None
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _thread_main(self) -> None:
        try:
            from chromium_advanced.official_playwright_mcp_bridge import OfficialPlaywrightMcpBridge

            self._driver = OfficialPlaywrightMcpBridge(
                node_executable=self._runtime_spec.node_executable,
                chromium_binary=self._runtime_spec.chromium_binary,
                user_data_dir=self._runtime_spec.user_data_dir,
                profile_name=self._runtime_spec.profile_name,
                extension_dirs=self._runtime_spec.extension_dirs,
                config={},
            )
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._tasks.get()
            if item is None:
                break
            method_name, args, kwargs, result_queue = item
            try:
                result = getattr(self._driver, method_name)(*args, **kwargs)
                result_queue.put((True, result))
            except BaseException as exc:
                result_queue.put((False, exc))

    def call(self, method_name: str, *args, **kwargs):
        if self._closed and method_name != "close":
            raise RuntimeError("official_playwright_mcp session is already closed")
        result_queue: "queue.Queue[tuple[bool, object]]" = queue.Queue(maxsize=1)
        self._tasks.put((method_name, args, kwargs, result_queue))
        ok, value = result_queue.get()
        if ok:
            return value
        raise value

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.call("close")
        finally:
            self._closed = True
            self._tasks.put(None)
            self._thread.join(timeout=10)


class OfficialPlaywrightMcpBrowserSession(BrowserSession):
    engine_name = "official_playwright_mcp"

    def __init__(
        self,
        *,
        runtime_spec: _OfficialRuntimeSpec,
        runtime_mode: str,
        runtime_root: str,
        mirror_generated_at: str,
    ) -> None:
        self.runtime_spec = runtime_spec
        self.runtime_mode = str(runtime_mode or "isolated_runtime")
        self.runtime_root = str(runtime_root or "")
        self.mirror_generated_at = str(mirror_generated_at or "")
        self._worker = _OfficialPlaywrightMcpSessionThread(runtime_spec)
        self.pid = 0

    def _call(self, method_name: str, *args, **kwargs):
        return self._worker.call(method_name, *args, **kwargs)

    def get_summary(self) -> BrowserSessionSummary:
        payload = self.get_current_url()
        current_url = str(payload.get("url", "") or payload.get("href", "") or "").strip()
        title = str(payload.get("title", "") or "").strip()
        return BrowserSessionSummary(
            current_url=current_url,
            title=title,
            alive=bool(payload.get("alive", True)),
        )

    def get_capabilities(self) -> Dict:
        return enrich_capability_payload(self._call("get_capabilities"))

    def list_tabs(self) -> Dict:
        return self._call("list_tabs")

    def open_tab(self, url: str = "", activate: bool = True, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        return self._call(
            "open_tab",
            url=url,
            activate=activate,
            wait_for_ready=wait_for_ready,
            timeout_seconds=timeout_seconds,
        )

    def activate_tab(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> Dict:
        return self._call(
            "activate_tab",
            tab_id=tab_id,
            index=index,
            title_contains=title_contains,
            url_contains=url_contains,
        )

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        return self._call("close_tab", tab_id=tab_id, index=index)

    def resize(self, width: int, height: int) -> Dict:
        return self._call("resize", width=width, height=height)

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("navigate", url=url, wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def get_current_url(self, tab_id: str = "") -> Dict:
        return self._call("get_current_url", tab_id=tab_id)

    def get_page_text(self, tab_id: str = "") -> Dict:
        return self._call("get_page_text", tab_id=tab_id)

    def get_page_html(self, tab_id: str = "") -> Dict:
        return self._call("get_page_html", tab_id=tab_id)

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        page = self._call("get_page_context_bundle", selector=selector, limit=max(1, int(limit or 10)), tab_id=tab_id)
        return {
            "selector": str(selector or ""),
            "by": str(by or "css"),
            "matches": list(page.get("inspect_matches", []) or []),
            "url": str(page.get("url", "") or ""),
            "title": str(page.get("title", "") or ""),
            "tab_id": str(page.get("tab_id", "") or ""),
            "alive": bool(page.get("alive", True)),
        }

    def get_active_element(self, tab_id: str = "") -> Dict:
        return self.run_script(
            """() => {
                const el = document.activeElement;
                if (!el) return {};
                return {
                    tag_name: String(el.tagName || '').toLowerCase(),
                    id: String(el.id || ''),
                    class_name: String(el.className || ''),
                    text: String(el.innerText || el.textContent || el.value || '').trim(),
                };
            }""",
            tab_id=tab_id,
        )

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        return self._call("get_interaction_context", tab_id=tab_id)

    def snapshot(self, target: str = "", by: str = "css", depth: int | None = None, boxes: bool = False, filename: str = "", tab_id: str = "") -> Dict:
        return self._call("snapshot", target=target, by=by, depth=depth, boxes=boxes, filename=filename, tab_id=tab_id)

    def list_candidates(self, target: str = "", by: str = "css", text_filter: str = "", limit: int = 25, include_boxes: bool = True, tab_id: str = "") -> Dict:
        page = self._call(
            "get_page_context_bundle",
            text_filter=text_filter,
            limit=max(1, int(limit or 25)),
            tab_id=tab_id,
        )
        candidates = list(page.get("candidates", []) or [])
        if not include_boxes:
            for item in candidates:
                if isinstance(item, dict):
                    item.pop("box", None)
        return {
            "candidates": candidates,
            "target": str(target or ""),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "url": str(page.get("url", "") or ""),
            "title": str(page.get("title", "") or ""),
            "tab_id": str(page.get("tab_id", "") or ""),
            "alive": bool(page.get("alive", True)),
        }

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        return self._call("wait_for", selector=selector, by=by, timeout_seconds=timeout_seconds, condition=condition)

    def wait_for_text(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("wait_for_text", text=text, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def wait_for_text_gone(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("wait_for_text_gone", text=text, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def wait_for_text_change(self, text: str = "", previous_text: str = "", timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("wait_for_text_change", text=text, previous_text=previous_text, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def wait_for_page_stable(self, timeout_seconds: int = 20, stable_cycles: int = 2, poll_interval_ms: int = 500, tab_id: str = "") -> Dict:
        return self._call(
            "wait_for_page_stable",
            timeout_seconds=timeout_seconds,
            stable_cycles=stable_cycles,
            poll_interval_ms=poll_interval_ms,
            tab_id=tab_id,
        )

    def wait_for_timeout(self, timeout_ms: int = 0, tab_id: str = "") -> Dict:
        return {"waited": True, "timeout_ms": max(0, int(timeout_ms or 0)), "tab_id": str(tab_id or "")}

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._call("click", selector=selector, by=by, timeout_seconds=timeout_seconds)

    def hover(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._call("hover", selector=selector, by=by, timeout_seconds=timeout_seconds)

    def click_target(self, target: str, element: str = "", by: str = "css", timeout_seconds: int = 20, double_click: bool = False) -> Dict:
        return self._call("click_target", target=target, element=element, by=by, timeout_seconds=timeout_seconds, double_click=double_click)

    def type_text(self, selector: str, text: str, by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        return self._call(
            "type_text",
            selector=selector,
            text=text,
            by=by,
            clear_first=clear_first,
            submit=submit,
            timeout_seconds=timeout_seconds,
        )

    def type_target(self, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        return self._call("type_target", target=target, text=text, element=element, by=by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds)

    def type_target_and_verify(self, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        return self._call("type_target_and_verify", target=target, text=text, element=element, by=by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds)

    def press_key(self, key: str, count: int = 1, selector: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._call(
            "press_key",
            key=key,
            count=count,
            selector=selector,
            by=by,
            timeout_seconds=timeout_seconds,
        )

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        return self._call("run_script", script=script, tab_id=tab_id)

    def run_script_batch(self, scripts: list[str], tab_id: str = "", stop_on_error: bool = True) -> Dict:
        return self._call("run_script_batch", scripts=scripts, tab_id=tab_id, stop_on_error=stop_on_error)

    def watch_page_state(self, text: str = "", previous_text: str = "", timeout_seconds: int = 20, stable_cycles: int = 2, poll_interval_ms: int = 500, tab_id: str = "") -> Dict:
        return self._call(
            "watch_page_state",
            text=text,
            previous_text=previous_text,
            timeout_seconds=timeout_seconds,
            stable_cycles=stable_cycles,
            poll_interval_ms=poll_interval_ms,
            tab_id=tab_id,
        )

    def watch_target_state(self, target: str, text: str = "", previous_text: str = "", element: str = "", by: str = "css", timeout_seconds: int = 20, stable_cycles: int = 2, poll_interval_ms: int = 500, tab_id: str = "") -> Dict:
        return self._call(
            "watch_target_state",
            target=target,
            text=text,
            previous_text=previous_text,
            element=element,
            by=by,
            timeout_seconds=timeout_seconds,
            stable_cycles=stable_cycles,
            poll_interval_ms=poll_interval_ms,
            tab_id=tab_id,
        )

    def select_option(self, selector: str, values: list[str] | None = None, by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._call("select_option", selector=selector, values=values or [], by=by, timeout_seconds=timeout_seconds)

    def handle_dialog(self, accept: bool = True, prompt_text: str = "", tab_id: str = "") -> Dict:
        return self._call("handle_dialog", accept=accept, prompt_text=prompt_text, tab_id=tab_id)

    def file_upload(self, target: str, files: list[str] | None = None, by: str = "css", element: str = "", timeout_seconds: int = 20) -> Dict:
        return self._call("file_upload", target=target, files=files or [], by=by, element=element, timeout_seconds=timeout_seconds)

    def navigate_back(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("navigate_back", wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def navigate_forward(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._call("navigate_forward", wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id)

    def drag_target(self, source_target: str, dest_target: str, source_element: str = "", dest_element: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._call("drag_target", source_target=source_target, dest_target=dest_target, source_element=source_element, dest_element=dest_element, by=by, timeout_seconds=timeout_seconds)

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        return self._call("get_console_messages", tab_id=tab_id, limit=limit, level=level)

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        return self._call("get_page_errors", tab_id=tab_id, limit=limit)

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        return self._call("get_network_requests", tab_id=tab_id, limit=limit, failed_only=failed_only)

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        return {"cleared": False, "message": "official_playwright_mcp backend does not buffer debug streams"}

    def diagnose_page(self, tab_id: str = "") -> Dict:
        payload = self.get_current_url(tab_id=tab_id)
        payload["diagnostic_backend"] = "official_playwright_mcp"
        return payload

    def verify_text(self, text: str) -> Dict:
        page = self.get_page_text()
        body = str(page.get("text", "") or "")
        matched = str(text or "") in body
        return {"verified": matched, "matched": matched, "text": str(text or ""), "engine_name": self.engine_name}

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        return self._call("verify_dialog", accessible_name=accessible_name, text=text)

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        return self._call("verify_active_element", target=target, by=by, element=element)

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        return self._call("verify_target_value", target=target, expected_value=expected_value, element=element, by=by)

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        return self._call("verify_target_visible", target=target, element=element, by=by)

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        return self._call("describe_target", target=target, element=element, by=by, include_box=include_box)

    def diagnose_target(self, target: str, element: str = "", by: str = "css", text_filter: str = "", limit: int = 10) -> Dict:
        return self._call("diagnose_target", target=target, element=element, by=by, text_filter=text_filter, limit=limit)

    def generate_locator(self, target: str, element: str = "") -> Dict:
        return self._call("generate_locator", target=target, element=element)

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        return self._call("verify_element", role=role, accessible_name=accessible_name)

    def execute_native_action(self, action_name: str, args: Dict[str, Any] | None = None) -> Dict:
        payload = dict(args or {})
        normalized = str(action_name or "").strip()
        if normalized == "get_current_url":
            page = self._call("get_page_context_bundle", tab_id=str(payload.get("tab_id", "") or ""))
            return {
                "url": str(page.get("url", "") or ""),
                "title": str(page.get("title", "") or ""),
                "readyState": str(page.get("readyState", "") or ""),
                "tab_id": str(page.get("tab_id", "") or ""),
                "alive": bool(page.get("alive", True)),
            }
        if normalized == "get_page_text":
            page = self._call("get_page_context_bundle", tab_id=str(payload.get("tab_id", "") or ""))
            return {
                "text": str(page.get("text", "") or ""),
                "url": str(page.get("url", "") or ""),
                "title": str(page.get("title", "") or ""),
                "tab_id": str(page.get("tab_id", "") or ""),
                "alive": bool(page.get("alive", True)),
            }
        if normalized == "get_page_html":
            page = self._call("get_page_context_bundle", include_html=True, tab_id=str(payload.get("tab_id", "") or ""))
            return {
                "html": str(page.get("html", "") or ""),
                "url": str(page.get("url", "") or ""),
                "title": str(page.get("title", "") or ""),
                "tab_id": str(page.get("tab_id", "") or ""),
                "alive": bool(page.get("alive", True)),
            }
        if normalized == "get_interaction_context":
            return self._call("get_interaction_context", tab_id=str(payload.get("tab_id", "") or ""))
        if normalized == "inspect_elements":
            return self.inspect_elements(
                selector=str(payload.get("selector", "") or ""),
                by=str(payload.get("by", "css") or "css"),
                limit=int(payload.get("limit", 10) or 10),
                tab_id=str(payload.get("tab_id", "") or ""),
            )
        if normalized == "list_candidates":
            return self.list_candidates(
                target=str(payload.get("target", "") or ""),
                by=str(payload.get("by", "css") or "css"),
                text_filter=str(payload.get("text_filter", "") or ""),
                limit=int(payload.get("limit", 25) or 25),
                include_boxes=bool(payload.get("include_boxes", True)),
                tab_id=str(payload.get("tab_id", "") or ""),
            )
        if normalized == "snapshot":
            return self.snapshot(
                target=str(payload.get("target", "") or ""),
                by=str(payload.get("by", "css") or "css"),
                depth=(int(payload.get("depth", 0) or 0) or None),
                boxes=bool(payload.get("boxes", False)),
                filename=str(payload.get("filename", "") or ""),
                tab_id=str(payload.get("tab_id", "") or ""),
            )
        raise ValueError(f"unsupported native action: {normalized}")

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        return self._call("highlight_target", target=target, element=element, by=by, style=style)

    def clear_highlights(self) -> Dict:
        return self._call("clear_highlights")

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        return self._call("mouse_move_xy", x=x, y=y)

    def mouse_click_xy(self, x: float, y: float, button: str = "left", click_count: int = 1, delay_ms: int = 0) -> Dict:
        return self._call("mouse_click_xy", x=x, y=y, button=button, click_count=click_count, delay_ms=delay_ms)

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        return self._call("mouse_drag_xy", start_x=start_x, start_y=start_y, end_x=end_x, end_y=end_y)

    def mouse_gesture_path(self, points: list[dict[str, Any]], *, steps_per_segment: int = 18, hold_before_ms: int = 0, segment_delay_ms: int = 0) -> Dict:
        return self._call(
            "mouse_gesture_path",
            points=points,
            steps_per_segment=steps_per_segment,
            hold_before_ms=hold_before_ms,
            segment_delay_ms=segment_delay_ms,
        )

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        return self._call("screenshot", filename=filename, tab_id=tab_id)

    def close(self) -> None:
        self._worker.close()


class OfficialPlaywrightMcpEngine(BrowserEngine):
    engine_name = "official_playwright_mcp"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        paths = config.get("paths", {})
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        if not chromium_binary:
            raise FileNotFoundError(f"chromium browser not found: {paths.get('chromium_dir', '')}")
        runtime = resolve_official_playwright_mcp_runtime(config)
        if not runtime.get("ready"):
            raise RuntimeError(
                "official_playwright_mcp runtime is not bundled yet. "
                "Prepare bundled Node.js and @playwright/mcp runtime under resources/runtime before enabling this engine."
            )
        runtime_root = str(paths.get("__runtime_root__", "") or "").strip()
        runtime_mode = str(paths.get("__runtime_mode__", "") or "isolated_runtime").strip() or "isolated_runtime"
        mirror_generated_at = str(paths.get("__mirror_generated_at__", "") or "").strip()
        user_data_root = str(paths.get("__runtime_user_data_dir__", "") or "").strip() or get_profile_user_data_root(config, profile_name)
        profile_directory = str(paths.get("__runtime_profile_dir__", "") or "").strip() or get_profile_directory_path(config, profile_name)
        if runtime_mode != "isolated_runtime":
            raise RuntimeError(
                "official_playwright_mcp only supports isolated_runtime mode in the current product line. "
                f"received_runtime_mode={runtime_mode} user_data_root={user_data_root} profile_dir={profile_directory}"
            )
        if not os.path.isdir(user_data_root):
            raise FileNotFoundError(f"official_playwright_mcp runtime root not found: {user_data_root}")
        if not os.path.isdir(profile_directory):
            raise FileNotFoundError(f"official_playwright_mcp profile directory not found: {profile_directory}")

        runtime_spec = _OfficialRuntimeSpec(
            node_executable=str(runtime.get("node_executable", "") or "").strip(),
            chromium_binary=chromium_binary,
            user_data_dir=user_data_root,
            profile_name=profile_name,
            extension_dirs=self._resolve_extension_dirs(config, profile_name),
        )
        return OfficialPlaywrightMcpBrowserSession(
            runtime_spec=runtime_spec,
            runtime_mode=runtime_mode,
            runtime_root=runtime_root,
            mirror_generated_at=mirror_generated_at,
        )

    @staticmethod
    def _resolve_extension_dirs(config: Dict, profile_name: str) -> List[str]:
        paths = config.get("paths", {}) if isinstance(config, dict) else {}
        resolved = resolve_profile_extension_dirs(config, profile_name)
        fingerprint_enabled = bool(config.get("launch", {}).get("load_fingerprint_extension", True)) if isinstance(config, dict) else True
        if fingerprint_enabled:
            fingerprint_dir = detect_fingerprint_extension_dir(paths.get("fingerprint_zip_path", ""))
            if fingerprint_dir and fingerprint_dir not in resolved:
                resolved.insert(0, fingerprint_dir)
        return resolved
