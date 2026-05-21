import argparse
import ctypes
import os
import traceback
from typing import Literal, Optional

from fastmcp import FastMCP

from chromium_advanced.chromium_profile_lib import now_text
from chromium_advanced.session_manager import SessionManager


MCP_INSTRUCTIONS = (
    "Use this server when a task needs a real Chromium profile with persistent login state. "
    "Start a profile session by profile_name, then use the returned session_id for browser actions."
)

DEFAULT_TIMEOUT_SECONDS = 20
ERROR_ALREADY_EXISTS = 183


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

    @server.tool
    def list_profiles() -> dict:
        """List configured Chromium profiles and whether each currently has an active MCP session."""
        return {"profiles": session_manager.list_profiles()}

    @server.tool
    def get_server_status() -> dict:
        """Return whether the browser service is idle, starting, or occupied."""
        return session_manager.get_server_status()

    @server.tool
    def get_profile_status(profile_name: str) -> dict:
        """Get one profile's current session occupancy and metadata."""
        return session_manager.get_profile_status(profile_name)

    @server.tool
    def can_start_profile_session(profile_name: str, engine: str = "") -> dict:
        """Check whether a new session is allowed right now for this profile."""
        return session_manager.can_start_session(profile_name, engine_name=engine)

    @server.tool
    def list_sessions() -> dict:
        """List active profile-backed browser sessions."""
        return {"sessions": session_manager.list_sessions()}

    @server.tool
    def start_profile_session(profile_name: str, reuse_existing: bool = False, engine: str = "") -> dict:
        """Start or reuse a real logged-in browser session for the specified profile."""
        print(
            (
                f"[{now_text()}] [MCP-WORKER] start session request: "
                f"profile={profile_name} reuse_existing={reuse_existing} engine={engine or '-'}"
            ),
            flush=True,
        )
        try:
            result = session_manager.start_session(
                profile_name=profile_name,
                reuse_existing=reuse_existing,
                engine_name=engine,
            )
        except Exception as exc:
            print(
                (
                    f"[{now_text()}] [MCP-WORKER] start session failed: "
                    f"profile={profile_name} engine={engine or '-'} error={exc}"
                ),
                flush=True,
            )
            print(traceback.format_exc(), flush=True)
            raise
        print(
            (
                f"[{now_text()}] [MCP-WORKER] session "
                f"{'reused' if result.get('reused') else 'started'}: "
                f"profile={result.get('profile_name', '')} "
                f"engine={result.get('engine_name', '')} "
                f"session_id={result.get('session_id', '')}"
            ),
            flush=True,
        )
        return result

    @server.tool
    def close_profile_session(session_id: str) -> dict:
        """Close an active browser session."""
        result = session_manager.close_session(session_id)
        print(
            (
                f"[{now_text()}] [MCP-WORKER] session "
                f"{'closed' if result.get('closed') else 'close-missed'}: "
                f"profile={result.get('profile_name', '')} "
                f"engine={result.get('engine_name', '')} "
                f"session_id={result.get('session_id', session_id)}"
            ),
            flush=True,
        )
        return result

    @server.tool
    def navigate(session_id: str, url: str, wait_for_ready: bool = True, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
        """Navigate the session to a URL."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.navigate(url, wait_for_ready, int(timeout_seconds))}

    @server.tool
    def get_current_url(session_id: str) -> dict:
        """Get the session's current URL and title."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_current_url()}

    @server.tool
    def get_session_capabilities(session_id: str) -> dict:
        """Return the feature capabilities exposed by the current browser session."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_capabilities()}

    @server.tool
    def get_page_text(session_id: str) -> dict:
        """Extract visible page text from the current document body."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_page_text()}

    @server.tool
    def get_page_html(session_id: str) -> dict:
        """Return the current page HTML source."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_page_html()}

    @server.tool
    def browser_snapshot(
        session_id: str,
        target: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        depth: int = 0,
        boxes: bool = False,
        filename: str = "",
    ) -> dict:
        """Capture an AI-friendly accessibility snapshot of the page or a target subtree."""
        browser_session = session_manager.resolve_session(session_id)
        resolved_depth = int(depth) if int(depth) > 0 else None
        return {
            "session_id": session_id,
            **browser_session.snapshot(target=target, by=by, depth=resolved_depth, boxes=bool(boxes), filename=filename),
        }

    @server.tool
    def browser_list_candidates(
        session_id: str,
        target: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
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
            ),
        }

    @server.tool
    def inspect_elements(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        limit: int = 10,
    ) -> dict:
        """Inspect matching elements to debug dynamic pages and refine selectors."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.inspect_elements(selector, by, int(limit))}

    @server.tool
    def get_active_element(session_id: str) -> dict:
        """Describe the currently focused element."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_active_element()}

    @server.tool
    def browser_get_interaction_context(session_id: str) -> dict:
        """Return the current page, focus, modal, tab, and snapshot context for agent reasoning."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.get_interaction_context()}

    @server.tool
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

    @server.tool
    def click(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Wait for and click an element."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.click(selector, by, int(timeout_seconds))}

    @server.tool
    def click_target(
        session_id: str,
        target: str,
        element: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        double_click: bool = False,
    ) -> dict:
        """Click a snapshot ref target, or fall back to a direct selector target when needed."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.click_target(
                target=target,
                element=element,
                by=by,
                timeout_seconds=int(timeout_seconds),
                double_click=bool(double_click),
            ),
        }

    @server.tool
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
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.type_text(selector, text, by, bool(clear_first), bool(submit), int(timeout_seconds)),
        }

    @server.tool
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

    @server.tool
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

    @server.tool
    def press_key(
        session_id: str,
        key: str,
        count: int = 1,
        selector: str = "",
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Press a key on the active element or an optional target element."""
        browser_session = session_manager.resolve_session(session_id)
        return {
            "session_id": session_id,
            **browser_session.press_key(key, int(count), selector, by, int(timeout_seconds)),
        }

    @server.tool
    def run_script(session_id: str, script: str) -> dict:
        """Run JavaScript in the current page and return a JSON-serializable result when possible."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.run_script(script)}

    @server.tool
    def browser_verify_text(session_id: str, text: str) -> dict:
        """Verify that text is visible on the current page."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.verify_text(text)}

    @server.tool
    def browser_verify_dialog(session_id: str, accessible_name: str = "", text: str = "") -> dict:
        """Verify that a visible dialog/modal is open, optionally matching accessible name or text."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.verify_dialog(accessible_name=accessible_name, text=text)}

    @server.tool
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

    @server.tool
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

    @server.tool
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

    @server.tool
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

    @server.tool
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

    @server.tool
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

    @server.tool
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

    @server.tool
    def browser_clear_highlights(session_id: str) -> dict:
        """Clear highlight overlays created earlier in the session."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.clear_highlights()}

    @server.tool
    def browser_mouse_move_xy(session_id: str, x: float, y: float) -> dict:
        """Move the mouse to viewport coordinates."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.mouse_move_xy(float(x), float(y))}

    @server.tool
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

    @server.tool
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

    @server.tool
    def screenshot(session_id: str, filename: str = "") -> dict:
        """Save a screenshot to disk and return the file path."""
        browser_session = session_manager.resolve_session(session_id)
        return {"session_id": session_id, **browser_session.screenshot(filename)}

    @server.tool
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
