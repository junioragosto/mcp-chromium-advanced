from __future__ import annotations

import json
import os
import re
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List

from chromium_advanced.chromium_profile_lib import get_bundled_playwright_mcp_dir


def _hidden_kwargs() -> Dict[str, Any]:
    if os.name != "nt":
        return {}
    kwargs: Dict[str, Any] = {}
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


class OfficialPlaywrightMcpBridge:
    def __init__(self, *, node_executable: str, chromium_binary: str, user_data_dir: str, profile_name: str, config: Dict[str, Any] | None = None) -> None:
        self.node_executable = str(node_executable or "").strip()
        self.chromium_binary = str(chromium_binary or "").strip()
        self.user_data_dir = str(user_data_dir or "").strip()
        self.profile_name = str(profile_name or "").strip()
        self.runtime_dir = os.path.abspath(get_bundled_playwright_mcp_dir(config))
        self.script_path = os.path.join(self.runtime_dir, "bridge.mjs")
        if not self.node_executable or not os.path.isfile(self.node_executable):
            raise FileNotFoundError(f"official_playwright_mcp bundled node not found: {self.node_executable}")
        if not self.runtime_dir or not os.path.isdir(self.runtime_dir):
            raise FileNotFoundError(f"official_playwright_mcp runtime directory not found: {self.runtime_dir}")
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"official_playwright_mcp bridge entrypoint not found: {self.script_path}")
        self._state: Dict[str, Any] = {"tabs": [], "active_tab_id": "", "url": "", "title": "", "alive": True}
        self._last_highlight_target: Dict[str, str] = {}
        self._payload_path = os.path.join(tempfile.gettempdir(), f"official-playwright-mcp-bootstrap-{uuid.uuid4().hex}.json")
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._response_queue: "queue.Queue[object]" = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._start_process()

    def _start_process(self) -> None:
        with open(self._payload_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "browser": {
                        "chromiumExecutable": self.chromium_binary,
                        "userDataDir": self.user_data_dir,
                        "profileName": self.profile_name,
                        "headless": False,
                    },
                },
                fh,
                ensure_ascii=False,
            )
        self._process = subprocess.Popen(
            [self.node_executable, self.script_path, self._payload_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.runtime_dir,
            **_hidden_kwargs(),
        )
        self._stdout_thread = threading.Thread(target=self._stdout_reader_loop, name="official-playwright-mcp-stdout", daemon=True)
        self._stdout_thread.start()
        ready = self._read_response(timeout_seconds=120)
        if not ready.get("ok") or ready.get("event") != "ready":
            self._terminate_process()
            raise RuntimeError(str(ready.get("error", "") or "official_playwright_mcp bridge failed to initialize"))
        try:
            self.list_tabs()
        except Exception:
            pass

    def _stdout_reader_loop(self) -> None:
        process = self._process
        if not process or not process.stdout:
            self._response_queue.put(RuntimeError("official_playwright_mcp bridge is not running"))
            return
        try:
            for raw_line in process.stdout:
                self._response_queue.put(raw_line)
        except BaseException as exc:  # pragma: no cover
            self._response_queue.put(exc)
        finally:
            self._response_queue.put(None)

    def _read_response(self, *, timeout_seconds: int) -> Dict[str, Any]:
        if not self._process:
            raise RuntimeError("official_playwright_mcp bridge is not running")
        try:
            item = self._response_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            self._terminate_process()
            raise TimeoutError("official_playwright_mcp bridge response timed out")
        if isinstance(item, BaseException):
            raise RuntimeError(str(item))
        if item is None:
            raw = ""
        else:
            raw = str(item or "").strip()
        if not raw:
            stderr = ""
            if self._process.stderr:
                try:
                    stderr = self._process.stderr.read()
                except Exception:
                    stderr = ""
            self._terminate_process()
            raise RuntimeError(f"official_playwright_mcp bridge returned empty response. stderr={stderr[:4000]}")
        try:
            return json.loads(raw)
        except Exception as exc:
            self._terminate_process()
            raise RuntimeError(f"official_playwright_mcp bridge returned invalid json: {raw[:4000]}") from exc

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        try:
            os.remove(self._payload_path)
        except OSError:
            pass
        self._stdout_thread = None

    def _run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if not self._process or not self._process.stdin:
                raise RuntimeError("official_playwright_mcp bridge is not running")
            self._process.stdin.write(json.dumps({"action": payload}, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
            parsed = self._read_response(timeout_seconds=120)
            if parsed.get("ok") is False:
                raise RuntimeError(str(parsed.get("error", "") or "official_playwright_mcp bridge action failed"))
            state = parsed.get("state")
            if isinstance(state, dict):
                self._state = dict(state)
            return parsed

    def _summary_from_state(self) -> Dict[str, Any]:
        active_tab_id = str(self._state.get("active_tab_id", "") or "")
        state_url = str(self._state.get("url", "") or "")
        state_title = str(self._state.get("title", "") or "")
        tabs = self._state.get("tabs", [])
        if isinstance(tabs, list):
            for item in tabs:
                if not isinstance(item, dict):
                    continue
                item_tab_id = str(item.get("tab_id", item.get("id", "")) or "")
                if active_tab_id and item_tab_id != active_tab_id:
                    continue
                if not state_url:
                    state_url = str(item.get("url", "") or "")
                if not state_title:
                    state_title = str(item.get("title", "") or "")
                if state_url and state_title:
                    break
        return {
            "url": state_url,
            "title": state_title,
            "alive": bool(self._state.get("alive", True)),
            "tab_id": active_tab_id,
        }

    def get_capabilities(self) -> Dict:
        return {
            "engine_name": "official_playwright_mcp",
            "supports_snapshot": True,
            "supports_snapshot_refs": True,
            "supports_target_actions": True,
            "supports_selector_actions": True,
            "supports_highlight": True,
            "supports_coordinates": True,
            "supports_gesture_actions": True,
            "supports_post_action_context": True,
            "supports_tabs": True,
            "supports_console_messages": True,
            "supports_page_errors": True,
            "supports_network_requests": True,
            "official_backend": True,
            "runtime_mode": "isolated_runtime",
        }

    def _target_args(self, target: str, *, element: str = "") -> Dict[str, Any]:
        payload = {"target": str(target or "").strip()}
        if str(element or "").strip():
            payload["element"] = str(element).strip()
        return payload

    def _extract_text_block(self, raw: Dict[str, Any]) -> str:
        tool_result = raw.get("tool_result", {})
        text_parts = tool_result.get("textParts", []) if isinstance(tool_result, dict) else []
        if isinstance(text_parts, list):
            return "\n".join(str(item or "") for item in text_parts).strip()
        return ""

    def _extract_result_section(self, raw: Dict[str, Any]) -> str:
        text = self._extract_text_block(raw)
        if not text:
            return ""
        marker = "### Result"
        if marker not in text:
            return text
        result = text.split(marker, 1)[1].strip()
        next_marker = result.find("### ")
        if next_marker >= 0:
            result = result[:next_marker].strip()
        return result.strip()

    def _extract_heading_block(self, raw: Dict[str, Any], heading: str) -> str:
        text = self._extract_text_block(raw)
        if not text:
            return ""
        pattern = rf"###\s+{re.escape(str(heading or '').strip())}\s*([\s\S]*?)(?:\n### |\Z)"
        match = re.search(pattern, text)
        if match:
            return str(match.group(1) or "").strip()
        return ""

    def _run_code(self, code: str) -> Dict[str, Any]:
        return self._run({"name": "browser_run_code_unsafe", "arguments": {"code": str(code or "")}})

    def list_tabs(self) -> Dict:
        payload = self._run({"name": "browser_tabs", "arguments": {"action": "list"}})
        return {"tabs": list((payload.get("state") or {}).get("tabs", []) or []), "active_tab_id": str((payload.get("state") or {}).get("active_tab_id", "") or ""), **self._summary_from_state()}

    def open_tab(self, *, url: str = "", activate: bool = True, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        args = {"action": "new"}
        if str(url or "").strip():
            args["url"] = str(url).strip()
        payload = self._run({"name": "browser_tabs", "arguments": args})
        return {"opened": True, "activated": bool(activate), "tabs": list((payload.get("state") or {}).get("tabs", []) or []), **self._summary_from_state()}

    def activate_tab(self, *, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> Dict:
        if int(index) < 0:
            tabs = self.list_tabs().get("tabs", [])
            for idx, item in enumerate(tabs):
                if tab_id and str(item.get("tab_id", "") or "") == str(tab_id):
                    index = idx
                    break
                if title_contains and title_contains in str(item.get("title", "") or ""):
                    index = idx
                    break
                if url_contains and url_contains in str(item.get("url", "") or ""):
                    index = idx
                    break
        if int(index) < 0:
            raise RuntimeError("official_playwright_mcp activate_tab requires a resolvable tab index")
        self._run({"name": "browser_tabs", "arguments": {"action": "select", "index": int(index)}})
        return {"activated": True, "index": int(index), **self._summary_from_state()}

    def close_tab(self, *, tab_id: str = "", index: int = -1) -> Dict:
        if int(index) < 0 and tab_id:
            tabs = self.list_tabs().get("tabs", [])
            for idx, item in enumerate(tabs):
                if str(item.get("tab_id", "") or "") == str(tab_id):
                    index = idx
                    break
        args: Dict[str, Any] = {"action": "close"}
        if int(index) >= 0:
            args["index"] = int(index)
        self._run({"name": "browser_tabs", "arguments": args})
        return {"closed": True, **self._summary_from_state()}

    def resize(self, *, width: int, height: int) -> Dict:
        self._run({"name": "browser_resize", "arguments": {"width": int(width), "height": int(height)}})
        return {"resized": True, "width": int(width), "height": int(height), **self._summary_from_state()}

    def navigate(self, *, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        self._run({"name": "browser_navigate", "arguments": {"url": str(url or "")}})
        return {"navigated": True, **self._summary_from_state()}

    def get_current_url(self, *, tab_id: str = "") -> Dict:
        payload = self.run_script(
            script="""() => ({
              url: location.href,
              title: document.title,
              readyState: document.readyState
            })""",
            tab_id=tab_id,
        )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        summary = self._summary_from_state()
        return {
            **summary,
            **result,
            "url": str(result.get("url", "") or summary.get("url", "") or ""),
            "title": str(result.get("title", "") or summary.get("title", "") or ""),
        }

    def get_page_text(self, *, tab_id: str = "") -> Dict:
        payload = self.run_script(script="() => document.body ? document.body.innerText : ''", tab_id=tab_id)
        return {"text": str(payload.get("result", "") or ""), **self._summary_from_state()}

    def get_page_html(self, *, tab_id: str = "") -> Dict:
        payload = self.run_script(script="() => document.documentElement ? document.documentElement.outerHTML : ''", tab_id=tab_id)
        return {"html": str(payload.get("result", "") or ""), **self._summary_from_state()}

    def get_interaction_context(self, *, tab_id: str = "") -> Dict:
        payload = self.run_script(
            script="""() => ({
              url: location.href,
              title: document.title,
              activeTag: document.activeElement ? document.activeElement.tagName : '',
              activeText: document.activeElement ? (document.activeElement.innerText || document.activeElement.value || '') : '',
            })""",
            tab_id=tab_id,
        )
        result = payload.get("result")
        return {"context": result if isinstance(result, dict) else {"value": result}, **self._summary_from_state()}

    def snapshot(self, *, target: str = "", by: str = "css", depth: int | None = None, boxes: bool = False, filename: str = "", tab_id: str = "") -> Dict:
        args: Dict[str, Any] = {}
        if str(target or "").strip():
            args["target"] = str(target).strip()
        if depth is not None:
            args["depth"] = int(depth)
        if boxes:
            args["boxes"] = True
        if str(filename or "").strip():
            args["filename"] = str(filename).strip()
        payload = self._run({"name": "browser_snapshot", "arguments": args})
        return {"snapshot": str(payload.get("snapshot", "") or ""), "snapshot_structured": dict(payload.get("snapshot_structured", {}) or {}), **self._summary_from_state()}

    def click(self, *, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        payload = self._run({"name": "browser_click", "arguments": self._target_args(selector, element="element")})
        return {"clicked": True, "selector": str(selector or ""), "raw": dict(payload.get("tool_result", {}) or {}), **self._summary_from_state()}

    def type_text(self, *, selector: str, text: str, by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        args = self._target_args(selector, element="element")
        args["text"] = str(text or "")
        if submit:
            args["submit"] = True
        self._run({"name": "browser_type", "arguments": args})
        return {"typed": True, "selector": str(selector or ""), "value": str(text or ""), **self._summary_from_state()}

    def press_key(self, *, key: str, count: int = 1, selector: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        for _ in range(max(1, int(count or 1))):
            self._run({"name": "browser_press_key", "arguments": {"key": str(key or "")}})
        return {"pressed": True, "key": str(key or ""), "count": max(1, int(count or 1)), **self._summary_from_state()}

    def run_script(self, *, script: str, tab_id: str = "") -> Dict:
        payload = self._run({"name": "browser_evaluate", "arguments": {"function": str(script or "")}})
        return {"result": payload.get("result"), "raw": dict(payload.get("tool_result", {}) or {}), **self._summary_from_state()}

    def run_script_batch(self, *, scripts: List[str], tab_id: str = "", stop_on_error: bool = True) -> Dict:
        results: List[Dict[str, Any]] = []
        ok_count = 0
        for index, script in enumerate(list(scripts or [])):
            try:
                item = self.run_script(script=str(script or ""), tab_id=tab_id)
                results.append({"ok": True, "index": index, "result": item.get("result")})
                ok_count += 1
            except Exception as exc:
                results.append({"ok": False, "index": index, "error": str(exc)})
                if stop_on_error:
                    break
        error_count = len([item for item in results if not item.get("ok")])
        return {
            "results": results,
            "ok_count": ok_count,
            "error_count": error_count,
            "all_ok": error_count == 0,
            "first_error": next((item.get("error", "") for item in results if not item.get("ok")), ""),
            **self._summary_from_state(),
        }

    def select_option(self, *, selector: str, values: List[str], by: str = "css", timeout_seconds: int = 20) -> Dict:
        args = self._target_args(selector, element="element")
        args["values"] = list(values or [])
        self._run({"name": "browser_select_option", "arguments": args})
        return {"selected": True, "selector": str(selector or ""), "values": list(values or []), **self._summary_from_state()}

    def handle_dialog(self, *, accept: bool = True, prompt_text: str = "", tab_id: str = "") -> Dict:
        self._run({"name": "browser_handle_dialog", "arguments": {"accept": bool(accept), "promptText": str(prompt_text or "")}})
        return {"handled": True, "accept": bool(accept), **self._summary_from_state()}

    def file_upload(self, *, target: str, files: List[str], by: str = "css", element: str = "", timeout_seconds: int = 20) -> Dict:
        self._run({"name": "browser_file_upload", "arguments": {"paths": list(files or [])}})
        return {"uploaded": True, "target": str(target or ""), "files": list(files or []), **self._summary_from_state()}

    def navigate_back(self, *, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        self._run({"name": "browser_navigate_back", "arguments": {}})
        return {"navigated_back": True, **self._summary_from_state()}

    def navigate_forward(self, *, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        self._run({"name": "browser_navigate_forward", "arguments": {}})
        return {"navigated_forward": True, **self._summary_from_state()}

    def screenshot(self, *, filename: str = "", tab_id: str = "") -> Dict:
        target_file = str(filename or "").strip() or os.path.join(tempfile.gettempdir(), "official-playwright-mcp-session.png")
        payload = self._run({"name": "browser_take_screenshot", "arguments": {"filename": target_file}})
        return {"path": str(payload.get("path", "") or target_file), **self._summary_from_state()}

    def hover(self, *, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        self._run({"name": "browser_hover", "arguments": self._target_args(selector, element="element")})
        return {"hovered": True, "selector": str(selector or ""), **self._summary_from_state()}

    def click_target(self, *, target: str, element: str = "", by: str = "css", timeout_seconds: int = 20, double_click: bool = False) -> Dict:
        args = self._target_args(target, element=element or "element")
        if double_click:
            args["doubleClick"] = True
        self._run({"name": "browser_click", "arguments": args})
        return {"clicked": True, "target": str(target or ""), "by": str(by or "css"), "double_click": bool(double_click), **self._summary_from_state()}

    def type_target(self, *, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        args = self._target_args(target, element=element or "element")
        args["text"] = str(text or "")
        if submit:
            args["submit"] = True
        self._run({"name": "browser_type", "arguments": args})
        return {"typed": True, "target": str(target or ""), "by": str(by or "css"), "value": str(text or ""), **self._summary_from_state()}

    def type_target_and_verify(self, *, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        payload = self.type_target(target=target, text=text, element=element, by=by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds)
        verify = self.verify_target_value(target=target, expected_value=text, element=element, by=by)
        return {**payload, "verified": bool(verify.get("verified", False)), "matched": bool(verify.get("matched", False))}

    def wait_for(self, *, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        if condition == "hidden":
            self._run({"name": "browser_wait_for", "arguments": {"textGone": str(selector or "")}})
        elif condition == "timeout":
            self._run({"name": "browser_wait_for", "arguments": {"time": max(0, int(timeout_seconds or 0))}})
        else:
            self._run({"name": "browser_wait_for", "arguments": {"text": str(selector or "")}})
        return {"waited": True, "selector": str(selector or ""), "condition": str(condition or "visible"), **self._summary_from_state()}

    def wait_for_text(self, *, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        self._run({"name": "browser_wait_for", "arguments": {"text": str(text or "")}})
        return {"waited": True, "text": str(text or ""), "condition": "visible", **self._summary_from_state()}

    def wait_for_text_gone(self, *, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        self._run({"name": "browser_wait_for", "arguments": {"textGone": str(text or "")}})
        return {"waited": True, "text": str(text or ""), "condition": "hidden", **self._summary_from_state()}

    def wait_for_text_change(self, *, text: str = "", previous_text: str = "", timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        deadline = time.time() + max(1, int(timeout_seconds or 20))
        expected = str(text or "").strip()
        previous = str(previous_text or "").strip()
        last_text = ""
        while time.time() < deadline:
            page = self.get_page_text(tab_id=tab_id)
            last_text = str(page.get("text", "") or "")
            changed = bool(last_text != previous) if previous else True
            matched = bool(expected in last_text) if expected else changed
            if changed and matched:
                return {
                    "changed": True,
                    "matched": matched,
                    "verified": matched,
                    "expected_text": expected,
                    "previous_text": previous,
                    "current_text_excerpt": last_text[:1000],
                    **self._summary_from_state(),
                }
            time.sleep(0.35)
        return {
            "changed": False,
            "matched": False,
            "verified": False,
            "expected_text": expected,
            "previous_text": previous,
            "current_text_excerpt": last_text[:1000],
            **self._summary_from_state(),
        }

    def wait_for_page_stable(self, *, timeout_seconds: int = 20, stable_cycles: int = 2, poll_interval_ms: int = 500, tab_id: str = "") -> Dict:
        deadline = time.time() + max(1, int(timeout_seconds or 20))
        stable_needed = max(1, int(stable_cycles or 2))
        poll_seconds = max(0.05, int(poll_interval_ms or 500) / 1000.0)
        previous_state = None
        stable_count = 0
        last_state = {}
        while time.time() < deadline:
            payload = self.run_script(
                script="""() => ({
                  href: location.href,
                  title: document.title,
                  readyState: document.readyState,
                  bodyTextLength: document.body ? String(document.body.innerText || '').length : 0,
                  childCount: document.body ? document.body.querySelectorAll('*').length : 0,
                  pendingImages: Array.from(document.images || []).filter((img) => !img.complete).length,
                  busyHint: !!document.querySelector('dialog,[role="dialog"],[aria-busy="true"],[data-loading="true"],[data-testid*="loading"]')
                })""",
                tab_id=tab_id,
            )
            current_state = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            last_state = dict(current_state or {})
            ready_state = str(current_state.get("readyState", "") or "").strip().lower()
            busy_hint = bool(current_state.get("busyHint", False))
            pending_images = int(current_state.get("pendingImages", 0) or 0)
            is_stable_match = (
                isinstance(previous_state, dict)
                and ready_state == "complete"
                and not busy_hint
                and pending_images <= 1
                and str(current_state.get("href", "") or "") == str(previous_state.get("href", "") or "")
                and str(current_state.get("title", "") or "") == str(previous_state.get("title", "") or "")
                and abs(int(current_state.get("bodyTextLength", 0) or 0) - int(previous_state.get("bodyTextLength", 0) or 0)) <= 32
                and abs(int(current_state.get("childCount", 0) or 0) - int(previous_state.get("childCount", 0) or 0)) <= 8
            )
            if is_stable_match:
                stable_count += 1
                if stable_count >= stable_needed:
                    return {
                        "stable": True,
                        "stable_cycles": stable_count,
                        "state": last_state,
                        **self._summary_from_state(),
                    }
            else:
                stable_count = 1 if ready_state == "complete" and not busy_hint and pending_images <= 1 else 0
            previous_state = current_state
            time.sleep(poll_seconds)
        return {
            "stable": False,
            "stable_cycles": stable_count,
            "state": last_state,
            **self._summary_from_state(),
        }

    def drag_target(self, *, source_target: str, dest_target: str, source_element: str = "", dest_element: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        self._run(
            {
                "name": "browser_drag",
                "arguments": {
                    "startTarget": str(source_target or "").strip(),
                    "endTarget": str(dest_target or "").strip(),
                    **({"startElement": str(source_element).strip()} if str(source_element or "").strip() else {}),
                    **({"endElement": str(dest_element).strip()} if str(dest_element or "").strip() else {}),
                },
            }
        )
        return {"dragged": True, "source_target": str(source_target or ""), "dest_target": str(dest_target or ""), **self._summary_from_state()}

    def get_console_messages(self, *, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        raw = self._run({"name": "browser_console_messages", "arguments": {"level": str(level or "info")}})
        text = self._extract_result_section(raw)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"messages": lines[: max(1, int(limit or 100))], "level": str(level or "info"), **self._summary_from_state()}

    def get_page_errors(self, *, tab_id: str = "", limit: int = 100) -> Dict:
        payload = self.get_console_messages(tab_id=tab_id, limit=limit, level="error")
        return {"errors": list(payload.get("messages", []) or []), **self._summary_from_state()}

    def get_network_requests(self, *, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        raw = self._run({"name": "browser_network_requests", "arguments": {"static": True}})
        text = self._extract_result_section(raw)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        requests = []
        for line in lines:
            if not re.match(r"^\d+\.", line):
                continue
            match = re.match(r"^(?P<index>\d+)\.\s+(?P<method>[A-Z]+)?\s*(?P<url>\S+)?(?:\s+\[(?P<status>[^\]]+)\])?(?P<tail>.*)$", line)
            if match:
                status_text = str(match.group("status") or "").strip()
                status_code = None
                ok = None
                if status_text:
                    number_match = re.search(r"\b(\d{3})\b", status_text)
                    if number_match:
                        status_code = int(number_match.group(1))
                        ok = status_code < 400
                item = {
                    "index": int(match.group("index")),
                    "method": str(match.group("method") or "").strip(),
                    "url": str(match.group("url") or "").strip(),
                    "status": status_code,
                    "ok": ok,
                    "summary": line,
                    "tail": str(match.group("tail") or "").strip(),
                }
            else:
                item = {"summary": line}
            requests.append(item)
        if failed_only:
            requests = [
                item
                for item in requests
                if item.get("ok") is False or "failed" in str(item.get("summary", "")).lower()
            ]
        return {"requests": requests[: max(1, int(limit or 100))], **self._summary_from_state()}

    def get_network_request_detail(self, *, index: int, part: str = "") -> Dict:
        args: Dict[str, Any] = {"index": int(index)}
        if str(part or "").strip():
            args["part"] = str(part).strip()
        raw = self._run({"name": "browser_network_request", "arguments": args})
        return {"index": int(index), "part": str(part or ""), "detail": self._extract_result_section(raw), **self._summary_from_state()}

    def verify_target_visible(self, *, target: str, element: str = "", by: str = "css") -> Dict:
        details = self.describe_target(target=target, element=element, by=by, include_box=True)
        visible = bool(details.get("visible", False))
        return {**details, "verified": visible, "matched": visible, "target": str(target or ""), "by": str(by or "css")}

    def verify_target_value(self, *, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        result = self.run_script(
            script=f"""() => {{
              const el = document.querySelector({json.dumps(str(target or ""))});
              const value = el ? String((el.value ?? el.textContent ?? '')).trim() : '';
              return {{ value, exists: !!el }};
            }}"""
        )
        value_payload = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
        actual = str(value_payload.get("value", "") or "")
        matched = bool(value_payload.get("exists")) and actual == str(expected_value or "")
        return {"verified": matched, "matched": matched, "target": str(target or ""), "expected_value": str(expected_value or ""), "actual_value": actual, "by": str(by or "css"), **self._summary_from_state()}

    def verify_active_element(self, *, target: str = "", by: str = "css", element: str = "") -> Dict:
        result = self.run_script(
            script="""() => {
              const el = document.activeElement;
              return {
                tagName: el ? el.tagName : '',
                id: el ? (el.id || '') : '',
                className: el ? (el.className || '') : '',
                text: el ? String(el.innerText || el.value || '').trim() : '',
              };
            }"""
        )
        payload = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
        target_text = str(target or "").strip().lower()
        haystack = " ".join(str(payload.get(key, "") or "") for key in ("tagName", "id", "className", "text")).lower()
        matched = not target_text or target_text in haystack
        return {"verified": matched, "matched": matched, "active_element": payload, "target": str(target or ""), "by": str(by or "css"), **self._summary_from_state()}

    def describe_target(self, *, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        payload = self._run(
            {
                "name": "browser_evaluate",
                "arguments": {
                    **self._target_args(target, element=element or "element"),
                    "function": """(element) => {
                        const rect = element.getBoundingClientRect();
                        return {
                          found: true,
                          tag_name: String(element.tagName || '').toLowerCase(),
                          text: String(element.innerText || element.textContent || '').trim(),
                          value: String(element.value || '').trim(),
                          id: String(element.id || ''),
                          class_name: String(element.className || ''),
                          role: String(element.getAttribute('role') || ''),
                          aria_label: String(element.getAttribute('aria-label') || ''),
                          visible: !!(rect.width || rect.height),
                          enabled: !element.disabled,
                          box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                        };
                    }""",
                },
            }
        )
        result = payload.get("result")
        payload = result if isinstance(result, dict) else {}
        if not payload.get("found"):
            raise RuntimeError(f'target not found: "{target}"')
        if not include_box:
            payload.pop("box", None)
        payload.update(self._summary_from_state())
        payload["target"] = str(target or "")
        payload["by"] = str(by or "css")
        return payload

    def diagnose_target(self, *, target: str, element: str = "", by: str = "css", text_filter: str = "", limit: int = 10) -> Dict:
        try:
            details = self.describe_target(target=target, element=element, by=by, include_box=True)
            locator = ""
            try:
                locator = str(self.generate_locator(target=target, element=element).get("locator", "") or "")
            except Exception:
                locator = ""
            return {
                **details,
                "status": "resolved",
                "message": "target resolved successfully",
                "text_filter": str(text_filter or ""),
                "details": details,
                "locator": locator,
            }
        except Exception as exc:
            return {
                **self._summary_from_state(),
                "status": "resolve_failed",
                "message": str(exc),
                "target": str(target or ""),
                "by": str(by or "css"),
                "text_filter": str(text_filter or ""),
            }

    def verify_dialog(self, *, accessible_name: str = "", text: str = "") -> Dict:
        result = self.run_script(
            script="""() => {
              const nodes = Array.from(document.querySelectorAll('dialog,[role="dialog"],[role="alertdialog"]'));
              return nodes.map((el) => ({
                text: String(el.innerText || el.textContent || '').trim(),
                ariaLabel: String(el.getAttribute('aria-label') || ''),
              }));
            }"""
        )
        dialogs = result.get("result", []) if isinstance(result.get("result"), list) else []
        name_text = str(accessible_name or "").strip().lower()
        body_text = str(text or "").strip().lower()
        matched = any((not name_text or name_text in str(item.get("ariaLabel", "")).lower()) and (not body_text or body_text in str(item.get("text", "")).lower()) for item in dialogs if isinstance(item, dict))
        return {"verified": matched, "matched": matched, "dialogs": dialogs, "expected_accessible_name": str(accessible_name or ""), "expected_text": str(text or ""), **self._summary_from_state()}

    def verify_element(self, *, role: str, accessible_name: str) -> Dict:
        script = f"""() => {{
          const nodes = Array.from(document.querySelectorAll('[role],button,a,input,textarea,select'));
          return nodes.some((el) => {{
            const role = String(el.getAttribute('role') || '').toLowerCase();
            const text = String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
            return role === {json.dumps(str(role or "").strip().lower())} && text.includes({json.dumps(str(accessible_name or "").strip().lower())});
          }});
        }}"""
        result = self.run_script(script=script)
        matched = bool(result.get("result"))
        return {"verified": matched, "matched": matched, "expected_role": str(role or ""), "expected_accessible_name": str(accessible_name or ""), **self._summary_from_state()}

    def generate_locator(self, *, target: str, element: str = "") -> Dict:
        try:
            raw = self._run({"name": "browser_generate_locator", "arguments": self._target_args(target, element=element or "element")})
            locator = self._extract_result_section(raw)
            if locator:
                return {"locator": locator, "target": str(target or ""), "generated_by": "official_tool", **self._summary_from_state()}
        except Exception:
            pass
        return {
            "locator": str(target or "").strip(),
            "target": str(target or ""),
            "generated_by": "selector_passthrough",
            **self._summary_from_state(),
        }

    def watch_page_state(
        self,
        *,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        if str(text or "").strip():
            result = self.wait_for_text_change(text=text, previous_text=previous_text, timeout_seconds=timeout_seconds, tab_id=tab_id)
            result.setdefault("watch_completed", bool(result.get("matched", False)))
            result.setdefault("watch_reason", "text_changed_and_matched" if result.get("matched") else "timeout")
            return result
        return self.wait_for_page_stable(
            timeout_seconds=timeout_seconds,
            stable_cycles=stable_cycles,
            poll_interval_ms=poll_interval_ms,
            tab_id=tab_id,
        )

    def watch_target_state(
        self,
        *,
        target: str,
        text: str = "",
        previous_text: str = "",
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        deadline = time.time() + max(1, int(timeout_seconds or 20))
        stable_needed = max(1, int(stable_cycles or 2))
        poll_seconds = max(0.05, int(poll_interval_ms or 500) / 1000.0)
        previous_value = str(previous_text or "")
        stable_count = 0
        last_payload: Dict[str, Any] = {}
        while time.time() < deadline:
            result = self.run_script(
                script=f"""() => {{
                  const el = document.querySelector({json.dumps(str(target or ""))});
                  if (!el) return {{ found: false, text: '', value: '' }};
                  return {{
                    found: true,
                    text: String(el.innerText || el.textContent || '').trim(),
                    value: String(el.value || '').trim()
                  }};
                }}""",
                tab_id=tab_id,
            )
            payload = result.get("result") if isinstance(result.get("result"), dict) else {}
            last_payload = dict(payload or {})
            current_value = str(payload.get("value") or payload.get("text") or "")
            changed = bool(current_value != previous_value) if previous_value else bool(current_value)
            matched = bool(str(text or "").strip() in current_value) if str(text or "").strip() else changed
            if payload.get("found") and matched:
                stable_count += 1
                if stable_count >= stable_needed:
                    return {
                        "found": True,
                        "changed": changed,
                        "matched": matched,
                        "verified": matched,
                        "target": str(target or ""),
                        "value": current_value,
                        "state": last_payload,
                        **self._summary_from_state(),
                    }
            else:
                stable_count = 0
            time.sleep(poll_seconds)
        return {
            "found": bool(last_payload.get("found")),
            "changed": False,
            "matched": False,
            "verified": False,
            "target": str(target or ""),
            "state": last_payload,
            **self._summary_from_state(),
        }

    def highlight_target(self, *, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        css_text = str(style or "").strip() or "outline: 2px solid #ff5a36; box-shadow: 0 0 0 2px rgba(255,90,54,0.25);"
        css_js = json.dumps(css_text, ensure_ascii=False)
        self.run_script(
            script=f"""() => {{
              const key = '__chromiumAdvancedHighlight';
              const prev = window[key];
              if (prev) {{
                const last = document.querySelector(prev.selector);
                if (last) {{
                  last.style.outline = prev.outline || '';
                  last.style.boxShadow = prev.boxShadow || '';
                }}
              }}
              const el = document.querySelector({json.dumps(str(target or ""))});
              if (!el) return {{ highlighted: false, reason: 'not_found' }};
              window[key] = {{
                selector: {json.dumps(str(target or ""))},
                outline: el.style.outline || '',
                boxShadow: el.style.boxShadow || ''
              }};
              el.style.cssText += '; ' + {css_js};
              return {{ highlighted: true }};
            }}"""
        )
        self._last_highlight_target = {"target": str(target or ""), "element": str(element or "element")}
        return {"highlighted": True, "target": str(target or ""), "by": str(by or "css"), **self._summary_from_state()}

    def clear_highlights(self) -> Dict:
        self.run_script(
            script="""() => {
              const key = '__chromiumAdvancedHighlight';
              const prev = window[key];
              if (!prev || !prev.selector) return { cleared: true, hadHighlight: false };
              const el = document.querySelector(prev.selector);
              if (el) {
                el.style.outline = prev.outline || '';
                el.style.boxShadow = prev.boxShadow || '';
              }
              delete window[key];
              return { cleared: true, hadHighlight: true };
            }"""
        )
        self._last_highlight_target = {}
        return {"cleared": True, **self._summary_from_state()}

    def mouse_move_xy(self, *, x: float, y: float) -> Dict:
        self._run_code(
            f"""async (page) => {{
              await page.mouse.move({float(x)}, {float(y)});
              return {{ moved: true, x: {float(x)}, y: {float(y)} }};
            }}"""
        )
        return {"moved": True, "x": float(x), "y": float(y), **self._summary_from_state()}

    def mouse_click_xy(self, *, x: float, y: float, button: str = "left", click_count: int = 1, delay_ms: int = 0) -> Dict:
        self._run_code(
            f"""async (page) => {{
              await page.mouse.click({float(x)}, {float(y)}, {{
                button: {json.dumps(str(button or "left"))},
                clickCount: {max(1, int(click_count or 1))},
                delay: {max(0, int(delay_ms or 0))}
              }});
              return {{ clicked: true }};
            }}"""
        )
        return {
            "clicked": True,
            "x": float(x),
            "y": float(y),
            "button": str(button or "left"),
            "click_count": max(1, int(click_count or 1)),
            **self._summary_from_state(),
        }

    def mouse_drag_xy(self, *, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        self._run_code(
            f"""async (page) => {{
              await page.mouse.move({float(start_x)}, {float(start_y)});
              await page.mouse.down();
              await page.mouse.move({float(end_x)}, {float(end_y)}, {{ steps: 16 }});
              await page.mouse.up();
              return {{ dragged: true }};
            }}"""
        )
        return {
            "dragged": True,
            "start": {"x": float(start_x), "y": float(start_y)},
            "end": {"x": float(end_x), "y": float(end_y)},
            **self._summary_from_state(),
        }

    def mouse_gesture_path(
        self,
        *,
        points: List[Dict[str, Any]],
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        normalized_points = [
            {"x": float(item.get("x", 0.0)), "y": float(item.get("y", 0.0))}
            for item in list(points or [])
            if isinstance(item, dict)
        ]
        if len(normalized_points) < 2:
            raise ValueError("mouse_gesture_path requires at least two points")
        self._run_code(
            f"""async (page) => {{
              const points = {json.dumps(normalized_points, ensure_ascii=False)};
              const stepsPerSegment = {max(1, int(steps_per_segment or 18))};
              const holdBefore = {max(0, int(hold_before_ms or 0))};
              const segmentDelay = {max(0, int(segment_delay_ms or 0))};
              await page.mouse.move(points[0].x, points[0].y);
              if (holdBefore) await page.waitForTimeout(holdBefore);
              await page.mouse.down();
              for (let i = 1; i < points.length; i++) {{
                await page.mouse.move(points[i].x, points[i].y, {{ steps: stepsPerSegment }});
                if (segmentDelay) await page.waitForTimeout(segmentDelay);
              }}
              await page.mouse.up();
              return {{ points: points.length }};
            }}"""
        )
        return {"gestured": True, "point_count": len(normalized_points), "points": normalized_points, **self._summary_from_state()}

    def close(self) -> None:
        try:
            if self._process and self._process.stdin:
                self._process.stdin.write(json.dumps({"command": "close"}) + "\n")
                self._process.stdin.flush()
                self._read_response(timeout_seconds=30)
        except Exception:
            pass
        finally:
            self._terminate_process()
