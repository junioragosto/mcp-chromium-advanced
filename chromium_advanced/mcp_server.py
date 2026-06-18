import argparse
import atexit
import ctypes
import json
import os
import sys
import tempfile
import traceback
import time
import threading
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastmcp import FastMCP

from chromium_advanced.chromium_profile_lib import now_text
from chromium_advanced.session_manager import SessionManager


MCP_INSTRUCTIONS = (
    "Use this server when a task needs a real Chromium profile with persistent login state. "
    "Start a profile session by profile_name, then use the returned session_id for browser actions."
)

DEFAULT_TIMEOUT_SECONDS = 20
ERROR_ALREADY_EXISTS = 183
MCP_TRACE_LIMIT = 500
MCP_TRACE_FILE_MAX_BYTES = 5 * 1024 * 1024
MCP_TRACE_FILE_ROTATIONS = 3
MCP_TRACE_ROTATE_CHECK_EVERY = 20
MCP_TRACE_SUCCESS_SAMPLE_RATE = 0.2


_mcp_tool_traces: list[dict[str, Any]] = []
_trace_file_lock = threading.Lock()
_trace_file_handle = None
_trace_file_path = ""
_trace_file_write_count = 0


def _env_flag(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "") or "").strip().lower()
    if not value:
        return bool(default)
    return value in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        value = str(os.environ.get(name, "") or "").strip()
        if not value:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


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


def _safe_len(value: Any) -> int:
    def _measure(item: Any, depth: int = 0) -> int:
        if item is None:
            return 0
        if isinstance(item, (bool, int, float)):
            return len(str(item))
        if isinstance(item, str):
            return len(item)
        if depth >= 2:
            return len(str(type(item).__name__))
        if isinstance(item, dict):
            total = 0
            for index, (key, val) in enumerate(item.items()):
                if index >= 24:
                    total += 64
                    break
                total += len(str(key)) + _measure(val, depth + 1)
            return total
        if isinstance(item, (list, tuple, set)):
            total = 0
            for index, entry in enumerate(item):
                if index >= 24:
                    total += 64
                    break
                total += _measure(entry, depth + 1)
            return total
        return len(str(item))

    try:
        return int(_measure(value))
    except Exception:
        return 0


def _resolve_trace_path() -> str:
    trace_path = os.environ.get("CHROMIUM_ADVANCED_MCP_TRACE_PATH")
    if not trace_path:
        trace_path = str(Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "chromium-advanced-mcp-trace.jsonl")
    return str(trace_path)


def _should_use_persistent_trace_handle(trace_path: str) -> bool:
    explicit_path = str(os.environ.get("CHROMIUM_ADVANCED_MCP_TRACE_PATH", "") or "").strip()
    if explicit_path:
        return False
    normalized = str(trace_path or "").lower()
    return normalized.endswith("chromium-advanced-mcp-trace.jsonl")


def _close_trace_file_handle() -> None:
    global _trace_file_handle, _trace_file_path
    with _trace_file_lock:
        handle = _trace_file_handle
        _trace_file_handle = None
        _trace_file_path = ""
    if handle is not None:
        try:
            handle.flush()
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


atexit.register(_close_trace_file_handle)


def _get_trace_file_handle(trace_path: str):
    global _trace_file_handle, _trace_file_path
    normalized_path = str(trace_path or "")
    with _trace_file_lock:
        if _trace_file_handle is not None and _trace_file_path == normalized_path:
            return _trace_file_handle
        old_handle = _trace_file_handle
        _trace_file_handle = None
        _trace_file_path = ""
        if old_handle is not None:
            try:
                old_handle.flush()
            except Exception:
                pass
            try:
                old_handle.close()
            except Exception:
                pass
        Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
        _trace_file_handle = open(normalized_path, "a", encoding="utf-8")
        _trace_file_path = normalized_path
        return _trace_file_handle


def _append_mcp_trace(trace: dict[str, Any]) -> None:
    global _trace_file_write_count
    _mcp_tool_traces.append(trace)
    overflow = len(_mcp_tool_traces) - MCP_TRACE_LIMIT
    if overflow > 0:
        del _mcp_tool_traces[:overflow]
    trace_path = _resolve_trace_path()
    try:
        should_persist = True
        explicit_path = str(os.environ.get("CHROMIUM_ADVANCED_MCP_TRACE_PATH", "") or "").strip()
        if bool(trace.get("ok", True)) and not explicit_path:
            sample_rate = max(0.0, min(1.0, _env_float("CHROMIUM_ADVANCED_MCP_TRACE_SUCCESS_SAMPLE_RATE", MCP_TRACE_SUCCESS_SAMPLE_RATE)))
            timestamp = float(trace.get("timestamp", 0.0) or 0.0)
            bucket = abs(hash((str(trace.get("tool_name", "")), str(trace.get("session_id", "")), int(timestamp * 10)))) % 1000
            should_persist = bucket < int(sample_rate * 1000)
        if not should_persist:
            return
        use_persistent_handle = _should_use_persistent_trace_handle(trace_path)
        if use_persistent_handle:
            with _trace_file_lock:
                _trace_file_write_count += 1
                should_rotate = (_trace_file_write_count % MCP_TRACE_ROTATE_CHECK_EVERY) == 0
            if should_rotate:
                _close_trace_file_handle()
                _rotate_trace_file(Path(trace_path))
            handle = _get_trace_file_handle(trace_path)
            with _trace_file_lock:
                handle.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
                if _env_flag("CHROMIUM_ADVANCED_MCP_TRACE_FLUSH_EVERY_WRITE", default=False):
                    handle.flush()
        else:
            Path(trace_path).parent.mkdir(parents=True, exist_ok=True)
            _rotate_trace_file(Path(trace_path))
            with open(trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _rotate_trace_file(trace_path: Path) -> None:
    try:
        if not trace_path.exists() or trace_path.stat().st_size < MCP_TRACE_FILE_MAX_BYTES:
            return
        for index in range(MCP_TRACE_FILE_ROTATIONS - 1, 0, -1):
            source = trace_path.with_name(f"{trace_path.name}.{index}")
            target = trace_path.with_name(f"{trace_path.name}.{index + 1}")
            if source.exists():
                if index + 1 > MCP_TRACE_FILE_ROTATIONS:
                    source.unlink(missing_ok=True)
                else:
                    source.replace(target)
        trace_path.replace(trace_path.with_name(f"{trace_path.name}.1"))
    except Exception:
        pass


def _trace_mcp_tool(tool_name: str, func: Callable[[], dict], *, session_id: str = "") -> dict:
    started = time.perf_counter()
    trace: dict[str, Any] = {
        "timestamp": round(time.time(), 3),
        "tool_name": str(tool_name or ""),
        "session_id": str(session_id or ""),
        "ok": True,
        "duration_ms": 0,
        "result_size": 0,
        "error_type": "",
        "error": "",
    }
    try:
        result = func()
        trace["result_size"] = _safe_len(result)
        if isinstance(result, dict):
            before_ids = result.get("active_session_ids_before")
            after_ids = result.get("active_session_ids_after")
            if isinstance(before_ids, list):
                trace["active_session_ids_before"] = [str(item) for item in before_ids]
            if isinstance(after_ids, list):
                trace["active_session_ids_after"] = [str(item) for item in after_ids]
        return result
    except Exception as exc:
        trace["ok"] = False
        trace["error_type"] = type(exc).__name__
        trace["error"] = str(exc)[:1000]
        raise
    finally:
        trace["duration_ms"] = round((time.perf_counter() - started) * 1000)
        _append_mcp_trace(trace)
        trace_stdout_enabled = _env_flag("CHROMIUM_ADVANCED_MCP_TRACE_STDOUT", default=False)
        if trace_stdout_enabled or not trace["ok"]:
            _safe_log(
                f"[{now_text()}] [MCP-TRACE] tool={trace['tool_name']} "
                f"session={trace['session_id'] or '-'} ok={trace['ok']} "
                f"duration_ms={trace['duration_ms']} result_size={trace['result_size']} "
                f"error={trace['error_type'] or '-'}"
            )


def _extract_action_level_error(result: object) -> tuple[bool, str, str]:
    if not isinstance(result, dict):
        return False, "", ""
    if result.get("ok") is not False:
        return False, "", ""
    return (
        True,
        str(result.get("error", "") or "").strip(),
        str(result.get("error_type", "") or "").strip(),
    )


def acquire_single_instance_guard(name: str):
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, str(name))
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance_guard(handle) -> None:
    if not handle or os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def build_server(config_path: Optional[str] = None) -> FastMCP:
    session_manager = SessionManager(config_path=config_path)
    server = FastMCP(name="chromium-advanced", instructions=MCP_INSTRUCTIONS)

    local_read_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    browser_read_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    session_start_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    local_lifecycle_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    trusted_browser_action_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    browser_script_annotations = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    browser_overlay_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }

    @server.tool(annotations=local_read_annotations)
    def list_profiles() -> dict:
        """List configured Chromium profiles and whether each currently has an active MCP session."""
        return {"profiles": session_manager.list_profiles()}

    @server.tool(annotations=local_read_annotations)
    def get_server_status() -> dict:
        """Return the current service status snapshot and whether new profile sessions are still accepted."""
        return session_manager.get_server_status()

    @server.tool(annotations=local_read_annotations)
    def get_profile_status(profile_name: str) -> dict:
        """Get one profile's current session occupancy and metadata."""
        return session_manager.get_profile_status(profile_name)

    @server.tool(annotations=local_read_annotations)
    def can_start_profile_session(profile_name: str, engine: str = "") -> dict:
        """Check whether a new session is allowed right now for this profile."""
        return session_manager.can_start_session(profile_name, engine_name=engine)

    @server.tool(annotations=local_read_annotations)
    def list_sessions() -> dict:
        """List active profile-backed browser sessions."""
        return {"sessions": session_manager.list_sessions()}

    @server.tool(annotations=local_read_annotations)
    def list_profile_occupancy_events(limit: int = 100) -> dict:
        """List recent shared profile occupancy events."""
        return {"events": session_manager.list_recent_occupancy_events(limit=limit)}

    @server.tool(annotations=local_lifecycle_annotations)
    def reclaim_profile(profile_name: str, reason: str = "mcp_reclaim") -> dict:
        """Force-clear a stale profile occupancy/lock state when recovery is required."""
        return session_manager.reclaim_profile(profile_name, reason=reason)

    @server.tool(annotations=session_start_annotations)
    def start_profile_session(profile_name: str, reuse_existing: bool = False, engine: str = "") -> dict:
        """Start or reuse a real logged-in browser session for the specified profile."""
        def _call() -> dict:
            _safe_log(
                f"[{now_text()}] [MCP-WORKER] start session request: "
                f"profile={profile_name} reuse_existing={reuse_existing} engine={engine or '-'}"
            )
            try:
                result = session_manager.start_session(
                    profile_name=profile_name,
                    reuse_existing=reuse_existing,
                    engine_name=engine,
                )
            except Exception as exc:
                _safe_log(
                    f"[{now_text()}] [MCP-WORKER] start session failed: "
                    f"profile={profile_name} engine={engine or '-'} error={exc}"
                )
                _safe_log(traceback.format_exc())
                raise
            _safe_log(
                f"[{now_text()}] [MCP-WORKER] session "
                f"{'reused' if result.get('reused') else 'started'}: "
                f"profile={result.get('profile_name', '')} "
                f"engine={result.get('engine_name', '')} "
                f"mode={result.get('runtime_mode', '') or '-'} "
                f"session_id={result.get('session_id', '')}"
            )
            return result

        return _trace_mcp_tool("start_profile_session", _call)

    @server.tool(annotations=local_lifecycle_annotations)
    def close_profile_session(session_id: str) -> dict:
        """Close an active browser session."""
        def _call() -> dict:
            result = session_manager.close_session(session_id)
            post_status = session_manager.get_server_status()
            _safe_log(
                f"[{now_text()}] [MCP-WORKER] session "
                f"{'closed' if result.get('closed') else 'close-missed'}: "
                f"profile={result.get('profile_name', '')} "
                f"engine={result.get('engine_name', '')} "
                f"session_id={result.get('session_id', session_id)}"
            )
            return {
                **result,
                "server_status_after_close": post_status,
            }

        return _trace_mcp_tool("close_profile_session", _call, session_id=session_id)

    @server.tool(annotations=trusted_browser_action_annotations)
    def navigate(
        session_id: str,
        url: str,
        wait_for_ready: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Navigate the session to a URL."""
        return _trace_mcp_tool(
            "navigate",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).navigate(url, wait_for_ready, int(timeout_seconds), tab_id=tab_id),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_read_annotations)
    def get_current_url(session_id: str, tab_id: str = "") -> dict:
        """Get the session's current URL and title."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_current_url(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def browser_list_tabs(session_id: str) -> dict:
        """List known tabs for the current browser session and indicate which tab is active."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.list_tabs()}

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_tabs(
        session_id: str,
        action: str = "list",
        index: int = -1,
        url: str = "",
    ) -> dict:
        """Official-style tab manager: list, new, select, or close a browser tab."""
        browser_session = session_manager.resolve_session(session_id)
        normalized_action = str(action or "list").strip().lower()
        if normalized_action in {"", "list"}:
            return {"session_id": session_id, **browser_session.list_tabs(), "action": "list"}
        if normalized_action in {"new", "open", "create"}:
            return {
                "session_id": session_id,
                **browser_session.open_tab(url=str(url or "").strip(), activate=True, wait_for_ready=True, timeout_seconds=DEFAULT_TIMEOUT_SECONDS),
                "action": "new",
            }
        if normalized_action in {"select", "activate"}:
            return {
                "session_id": session_id,
                **browser_session.activate_tab(index=int(index)),
                "action": "select",
            }
        if normalized_action == "close":
            return {
                "session_id": session_id,
                **browser_session.close_tab(index=int(index)),
                "action": "close",
            }
        raise ValueError(f"Unsupported browser_tabs action: {normalized_action}")

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_open_tab(
        session_id: str,
        url: str = "",
        activate: bool = True,
        wait_for_ready: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Open a new tab, optionally navigate it to a URL, and optionally activate it."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.open_tab(
                url=url,
                activate=bool(activate),
                wait_for_ready=bool(wait_for_ready),
                timeout_seconds=int(timeout_seconds),
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_activate_tab(
        session_id: str,
        tab_id: str = "",
        index: int = -1,
        title_contains: str = "",
        url_contains: str = "",
    ) -> dict:
        """Activate an existing tab by explicit tab_id, index, or partial title/URL match."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.activate_tab(
                tab_id=tab_id,
                index=int(index),
                title_contains=title_contains,
                url_contains=url_contains,
            ),
        }

    @server.tool(annotations=local_lifecycle_annotations)
    def browser_close_tab(session_id: str, tab_id: str = "", index: int = -1) -> dict:
        """Close a tab by explicit tab_id or index and keep the session alive on a remaining tab."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.close_tab(tab_id=tab_id, index=int(index))}

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_resize(session_id: str, width: int, height: int) -> dict:
        """Official-style browser window resize."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.resize(width=int(width), height=int(height))}

    @server.tool(annotations=local_lifecycle_annotations)
    def browser_close(session_id: str) -> dict:
        """Official-style alias for closing the current browser session."""
        return close_profile_session(session_id)

    @server.tool(annotations=local_read_annotations)
    def get_session_capabilities(session_id: str) -> dict:
        """Return the feature capabilities exposed by the current browser session."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_capabilities()}

    @server.tool(annotations=browser_read_annotations)
    def get_page_text(session_id: str, tab_id: str = "") -> dict:
        """Extract visible page text from the current document body."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_page_text(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def get_page_html(session_id: str, tab_id: str = "") -> dict:
        """Return the current page HTML source."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_page_html(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def browser_snapshot(
        session_id: str,
        target: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        depth: int = 0,
        boxes: bool = False,
        filename: str = "",
        tab_id: str = "",
    ) -> dict:
        """Capture an AI-friendly accessibility snapshot of the page or a target subtree."""
        browser_session = session_manager.resolve_session(session_id)
        resolved_depth = int(depth) if int(depth) > 0 else None
        return {
            "session_id": session_id,
            **browser_session.snapshot(
                target=target,
                by=by,
                depth=resolved_depth,
                boxes=bool(boxes),
                filename=filename,
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_list_candidates(
        session_id: str,
        target: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> dict:
        """List actionable candidate elements from the page or a target subtree."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.list_candidates(
                target=target,
                by=by,
                text_filter=text_filter,
                limit=int(limit),
                include_boxes=bool(include_boxes),
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def inspect_elements(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        limit: int = 10,
        tab_id: str = "",
    ) -> dict:
        """Inspect matching elements to debug dynamic pages and refine selectors."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.inspect_elements(selector, by, int(limit), tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def get_active_element(session_id: str, tab_id: str = "") -> dict:
        """Describe the currently focused element."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_active_element(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def browser_get_interaction_context(session_id: str, tab_id: str = "") -> dict:
        """Return the current page, focus, modal, tab, and snapshot context for agent reasoning."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_interaction_context(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def wait_for(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        condition: Literal["present", "visible", "clickable"] = "visible",
    ) -> dict:
        """Wait for an element to reach a desired state."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.wait_for(selector, by, int(timeout_seconds), condition)}

    @server.tool(annotations=browser_read_annotations)
    def wait_for_text(
        session_id: str,
        text: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Wait until visible page text contains the requested text."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.wait_for_text(text, int(timeout_seconds), tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def wait_for_text_gone(
        session_id: str,
        text: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Wait until the requested visible text disappears from the page."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.wait_for_text_gone(text, int(timeout_seconds), tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def wait_for_text_change(
        session_id: str,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Wait until page text changes from a previous snapshot, or until a target text newly appears."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.wait_for_text_change(
                text=text,
                previous_text=previous_text,
                timeout_seconds=int(timeout_seconds),
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def wait_for_page_stable(
        session_id: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> dict:
        """Wait until page url/title/text/html-length stop changing for a short polling window."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.wait_for_page_stable(
                timeout_seconds=int(timeout_seconds),
                stable_cycles=int(stable_cycles),
                poll_interval_ms=int(poll_interval_ms),
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def wait_for_timeout(session_id: str, timeout_ms: int = 0, tab_id: str = "") -> dict:
        """Pause the active browser session for a bounded amount of time."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.wait_for_timeout(int(timeout_ms), tab_id=tab_id)}

    @server.tool(annotations=trusted_browser_action_annotations)
    def click(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Wait for and click an element."""
        return _trace_mcp_tool(
            "click",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).click(selector, by, int(timeout_seconds)),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def hover(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Move the pointer over an element and surface post-action context."""
        return _trace_mcp_tool(
            "hover",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).hover(selector, by, int(timeout_seconds)),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def click_target(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        double_click: bool = False,
    ) -> dict:
        """Click a snapshot ref target, or fall back to a direct selector target when needed."""
        return _trace_mcp_tool(
            "click_target",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).click_target(
                    target=target,
                    element=element,
                    by=by,
                    timeout_seconds=int(timeout_seconds),
                    double_click=bool(double_click),
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def type_text(
        session_id: str,
        selector: str,
        text: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Type into an input, textarea, or editable field."""
        return _trace_mcp_tool(
            "type_text",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).type_text(
                    selector,
                    text,
                    by,
                    bool(clear_first),
                    bool(submit),
                    int(timeout_seconds),
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def type_target(
        session_id: str,
        target: str,
        text: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Type into a snapshot ref target, or fall back to a direct selector target when needed."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.type_target(
                target=target,
                text=text,
                element=element,
                by=by,
                clear_first=bool(clear_first),
                submit=bool(submit),
                timeout_seconds=int(timeout_seconds),
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def type_target_and_verify(
        session_id: str,
        target: str,
        text: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Type into a target and immediately verify that the resulting value matches the requested text."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.type_target_and_verify(
                target=target,
                text=text,
                element=element,
                by=by,
                clear_first=bool(clear_first),
                submit=bool(submit),
                timeout_seconds=int(timeout_seconds),
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def press_key(
        session_id: str,
        key: str,
        count: int = 1,
        selector: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Press a key on the active element or an optional target element."""
        return _trace_mcp_tool(
            "press_key",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).press_key(key, int(count), selector, by, int(timeout_seconds)),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def select_option(
        session_id: str,
        selector: str,
        value: str = "",
        values: list[str] | None = None,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Select one or more option values or labels on a select control."""
        normalized_values = [str(item) for item in (values or []) if str(item or "")]
        if not normalized_values and str(value or "").strip():
            normalized_values = [str(value).strip()]
        return _trace_mcp_tool(
            "select_option",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).select_option(
                    selector,
                    values=normalized_values,
                    by=by,
                    timeout_seconds=int(timeout_seconds),
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def navigate_back(
        session_id: str,
        wait_for_ready: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Navigate back in browser history."""
        return _trace_mcp_tool(
            "navigate_back",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).navigate_back(
                    wait_for_ready=bool(wait_for_ready),
                    timeout_seconds=int(timeout_seconds),
                    tab_id=tab_id,
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=trusted_browser_action_annotations)
    def navigate_forward(
        session_id: str,
        wait_for_ready: bool = True,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        tab_id: str = "",
    ) -> dict:
        """Navigate forward in browser history."""
        return _trace_mcp_tool(
            "navigate_forward",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).navigate_forward(
                    wait_for_ready=bool(wait_for_ready),
                    timeout_seconds=int(timeout_seconds),
                    tab_id=tab_id,
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_script_annotations)
    def run_script(session_id: str, script: str, tab_id: str = "") -> dict:
        """Run arbitrary JavaScript in the current page. This is a high-trust action and is intentionally not read-only."""
        return _trace_mcp_tool(
            "run_script",
            lambda: {"session_id": session_id, **session_manager.resolve_session(session_id).run_script(script, tab_id=tab_id)},
            session_id=session_id,
        )

    @server.tool(annotations=browser_read_annotations)
    def watch_page_state(
        session_id: str,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> dict:
        """Wait for text change and page stabilization, then return the initial/final state summary and diff hints."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.watch_page_state(
                text=text,
                previous_text=previous_text,
                timeout_seconds=int(timeout_seconds),
                stable_cycles=int(stable_cycles),
                poll_interval_ms=int(poll_interval_ms),
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def watch_target_state(
        session_id: str,
        target: str,
        text: str = "",
        previous_text: str = "",
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> dict:
        """Watch a target-local text/value/state region until it changes and then stabilizes."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.watch_target_state(
                target=target,
                text=text,
                previous_text=previous_text,
                element=element,
                by=by,
                timeout_seconds=int(timeout_seconds),
                stable_cycles=int(stable_cycles),
                poll_interval_ms=int(poll_interval_ms),
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=browser_script_annotations)
    def run_script_batch(
        session_id: str,
        scripts: list[str],
        tab_id: str = "",
        stop_on_error: bool = True,
    ) -> dict:
        """Run multiple arbitrary JavaScript snippets in one logical call. This remains a high-trust non-read-only surface."""
        if not isinstance(scripts, list) or not scripts:
            raise ValueError("scripts is required")

        def _call() -> dict:
            browser_session = session_manager.resolve_session(session_id)
            items = []
            for index, script in enumerate(scripts):
                script_text = str(script or "")
                item = {
                    "index": index,
                    "script": script_text,
                }
                try:
                    item_result = browser_session.run_script(script_text, tab_id=tab_id)
                    item["result"] = item_result
                    failed, message, error_type = _extract_action_level_error(item_result)
                    item["ok"] = not failed
                    if failed:
                        if message:
                            item["error"] = message
                        if error_type:
                            item["error_type"] = error_type
                        if stop_on_error:
                            raise RuntimeError(message or "run_script_batch item failed")
                except Exception as exc:
                    item["ok"] = False
                    item["error_type"] = type(exc).__name__
                    item["error"] = str(exc)
                    if stop_on_error:
                        raise
                items.append(item)
            return {
                "session_id": session_id,
                "count": len(items),
                "stop_on_error": bool(stop_on_error),
                "items": items,
            }

        return _trace_mcp_tool("run_script_batch", _call, session_id=session_id)

    @server.tool(annotations=browser_read_annotations)
    def browser_get_console_messages(
        session_id: str,
        tab_id: str = "",
        limit: int = 100,
        level: str = "",
    ) -> dict:
        """Return recent console messages, optionally filtered by tab and message level."""
        return _trace_mcp_tool(
            "browser_get_console_messages",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).get_console_messages(tab_id=tab_id, limit=int(limit), level=level),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_read_annotations)
    def browser_get_page_errors(session_id: str, tab_id: str = "", limit: int = 100) -> dict:
        """Return recent uncaught page errors and severe browser log entries."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_page_errors(tab_id=tab_id, limit=int(limit))}

    @server.tool(annotations=browser_read_annotations)
    def browser_get_network_requests(
        session_id: str,
        tab_id: str = "",
        limit: int = 100,
        failed_only: bool = False,
    ) -> dict:
        """Return recent observed network requests and responses, optionally filtering to failures."""
        return _trace_mcp_tool(
            "browser_get_network_requests",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).get_network_requests(tab_id=tab_id, limit=int(limit), failed_only=bool(failed_only)),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_overlay_annotations)
    def browser_clear_debug_buffers(session_id: str, tab_id: str = "") -> dict:
        """Clear cached console, page error, and network request buffers for the session or one tab."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.clear_debug_buffers(tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def browser_diagnose_page(session_id: str, tab_id: str = "") -> dict:
        """Return a high-signal diagnostic bundle for a page, including recent console, error, and network failures."""
        return _trace_mcp_tool(
            "browser_diagnose_page",
            lambda: {"session_id": session_id, **session_manager.resolve_session(session_id).diagnose_page(tab_id=tab_id)},
            session_id=session_id,
        )

    @server.tool(annotations=local_read_annotations)
    def browser_get_action_trace(session_id: str, limit: int = 20) -> dict:
        """Return recent managed browser action traces and slow/failure summaries for this session."""
        return _trace_mcp_tool(
            "browser_get_action_trace",
            lambda: {"session_id": session_id, **session_manager.resolve_session(session_id).get_action_trace(limit=int(limit))},
            session_id=session_id,
        )

    @server.tool(annotations=local_read_annotations)
    def get_mcp_tool_trace(limit: int = 50) -> dict:
        """Return recent MCP tool-level timing traces recorded by this worker process."""
        bounded = max(1, min(200, int(limit)))
        items = [dict(item) for item in _mcp_tool_traces[-bounded:]]
        slow = sorted(items, key=lambda item: int(item.get("duration_ms", 0) or 0), reverse=True)[:10]
        failures = [item for item in items if not item.get("ok")]
        return {
            "count": len(items),
            "trace_limit": MCP_TRACE_LIMIT,
            "slowest": slow,
            "failures": failures[-20:],
            "traces": items,
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_text(session_id: str, text: str) -> dict:
        """Verify that text is visible on the current page."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.verify_text(text)}

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_dialog(session_id: str, accessible_name: str = "", text: str = "") -> dict:
        """Verify that a visible dialog/modal is open, optionally matching accessible name or text."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.verify_dialog(accessible_name=accessible_name, text=text)}

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_active_element(
        session_id: str,
        target: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        element: str = "",
    ) -> dict:
        """Verify the currently focused element, or verify that focus is on a specific target."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.verify_active_element(target=target, by=by, element=element),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_target_value(
        session_id: str,
        target: str,
        expected_value: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
    ) -> dict:
        """Verify the current value of an input-like target."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.verify_target_value(
                target=target,
                expected_value=expected_value,
                element=element,
                by=by,
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_describe_target(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        include_box: bool = True,
    ) -> dict:
        """Describe a target ref or selector, including visibility and optional bounding box."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.describe_target(target=target, element=element, by=by, include_box=bool(include_box)),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_diagnose_target(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        text_filter: str = "",
        limit: int = 10,
    ) -> dict:
        """Diagnose why a target may not be interactable, including related candidates and current page context."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.diagnose_target(
                target=target,
                element=element,
                by=by,
                text_filter=text_filter,
                limit=int(limit),
            ),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_element(
        session_id: str,
        role: str,
        accessible_name: str,
    ) -> dict:
        """Verify that an element with the given role and accessible name is visible."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.verify_element(role=role, accessible_name=accessible_name),
        }

    @server.tool(annotations=browser_read_annotations)
    def browser_verify_target_visible(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
    ) -> dict:
        """Verify that a target ref or selector is visible."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.verify_target_visible(target=target, element=element, by=by),
        }

    @server.tool(annotations=browser_overlay_annotations)
    def browser_highlight_target(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        style: str = "",
    ) -> dict:
        """Show a persistent highlight overlay for a target ref or selector."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.highlight_target(target=target, element=element, by=by, style=style),
        }

    @server.tool(annotations=browser_overlay_annotations)
    def browser_clear_highlights(session_id: str) -> dict:
        """Clear highlight overlays created earlier in the session."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.clear_highlights()}

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_mouse_move_xy(session_id: str, x: float, y: float) -> dict:
        """Move the mouse to viewport coordinates."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.mouse_move_xy(float(x), float(y))}

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_mouse_click_xy(
        session_id: str,
        x: float,
        y: float,
        button: Literal["left", "right", "middle"] = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> dict:
        """Click at viewport coordinates as a vision-style fallback."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.mouse_click_xy(
                float(x),
                float(y),
                button=str(button),
                click_count=int(click_count),
                delay_ms=int(delay_ms),
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_mouse_drag_xy(
        session_id: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> dict:
        """Drag the mouse from one viewport coordinate to another."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.mouse_drag_xy(float(start_x), float(start_y), float(end_x), float(end_y)),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_mouse_gesture_path(
        session_id: str,
        points: list[dict[str, float]],
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> dict:
        """Perform one continuous mouse gesture across multiple viewport points."""
        browser_session = session_manager.resolve_session(session_id)
        normalized_points = []
        for item in list(points or []):
            if not isinstance(item, dict):
                raise ValueError("points must be a list of {x, y} objects")
            if "x" not in item or "y" not in item:
                raise ValueError("each gesture point must include x and y")
            normalized_points.append({"x": float(item["x"]), "y": float(item["y"])})
        return {
            "session_id": session_id,
            **browser_session.mouse_gesture_path(
                normalized_points,
                steps_per_segment=int(steps_per_segment),
                hold_before_ms=int(hold_before_ms),
                segment_delay_ms=int(segment_delay_ms),
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def drag_target(
        session_id: str,
        source_target: str,
        dest_target: str,
        source_element: str = "",
        dest_element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Drag one target element onto another target element."""
        return _trace_mcp_tool(
            "drag_target",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).drag_target(
                    source_target,
                    dest_target,
                    source_element=source_element,
                    dest_element=dest_element,
                    by=by,
                    timeout_seconds=int(timeout_seconds),
                ),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_read_annotations)
    def browser_network_request(session_id: str, index: int, tab_id: str = "") -> dict:
        """Official-style single network request detail lookup by 1-based index."""
        return _trace_mcp_tool(
            "browser_network_request",
            lambda: {
                "session_id": session_id,
                **session_manager.resolve_session(session_id).get_network_request(index=int(index), tab_id=tab_id),
            },
            session_id=session_id,
        )

    @server.tool(annotations=browser_read_annotations)
    def screenshot(session_id: str, filename: str = "", tab_id: str = "") -> dict:
        """Save a screenshot to disk and return the file path."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.screenshot(filename, tab_id=tab_id)}

    @server.tool(annotations=browser_read_annotations)
    def browser_take_screenshot(session_id: str, filename: str = "", tab_id: str = "") -> dict:
        """Official-style alias for saving a screenshot to disk."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.screenshot(filename, tab_id=tab_id)}

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_handle_dialog(
        session_id: str,
        accept: bool = True,
        prompt_text: str = "",
        tab_id: str = "",
    ) -> dict:
        """Accept or dismiss a blocking browser dialog."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.handle_dialog(
                accept=bool(accept),
                prompt_text=prompt_text,
                tab_id=tab_id,
            ),
        }

    @server.tool(annotations=trusted_browser_action_annotations)
    def browser_file_upload(
        session_id: str,
        target: str,
        files: list[str] | None = None,
        file: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        element: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Upload one or multiple files through a file input target."""
        normalized_files = [str(item) for item in (files or []) if str(item or "")]
        if not normalized_files and str(file or "").strip():
            normalized_files = [str(file).strip()]
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.file_upload(
                target=target,
                files=normalized_files,
                by=by,
                element=element,
                timeout_seconds=int(timeout_seconds),
            ),
        }

    @server.tool(annotations=local_lifecycle_annotations)
    def close_all_sessions() -> dict:
        """Close all active sessions created by this MCP server process."""
        return session_manager.close_all()

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Chromium Advanced MCP Server")
    parser.add_argument(
        "--transport",
        default=os.environ.get("CHROMIUM_MCP_TRANSPORT", "stdio"),
        help="FastMCP transport, default: stdio",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CHROMIUM_MCP_HOST", "").strip(),
        help="Optional host override for HTTP transports",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("CHROMIUM_MCP_PORT", "").strip(),
        help="Optional port override for HTTP transports",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("CHROMIUM_MCP_PATH", "").strip(),
        help="Optional path override for HTTP transports",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CHROMIUM_MCP_LOG_LEVEL", "").strip(),
        help="Optional log level override for HTTP transports",
    )
    parser.add_argument(
        "--config-path",
        default=os.environ.get("CHROMIUM_MCP_CONFIG_PATH", "").strip(),
        help="Optional explicit config path",
    )
    args = parser.parse_args()

    config_path = args.config_path or None
    transport = str(args.transport or "stdio").strip() or "stdio"
    guard_name = ""
    if transport != "stdio" and args.port:
        guard_name = f"Local\\ChromiumMcpWorker-{int(args.port)}"
    guard = acquire_single_instance_guard(guard_name) if guard_name else None
    if guard_name and guard is None:
        raise SystemExit(f"MCP worker already running on configured port {int(args.port)}")

    try:
        server = build_server(config_path=config_path)
        if transport == "stdio":
            server.run(transport)
            return

        run_kwargs = {}
        if args.host:
            run_kwargs["host"] = args.host
        if args.port:
            run_kwargs["port"] = int(args.port)
        if args.path:
            run_kwargs["path"] = args.path
        if args.log_level:
            run_kwargs["log_level"] = args.log_level
        server.run(transport, **run_kwargs)
    finally:
        release_single_instance_guard(guard)


if __name__ == "__main__":
    main()
