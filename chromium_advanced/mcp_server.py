import argparse
import os
from typing import Literal, Optional

from fastmcp import FastMCP

from chromium_advanced.chromium_profile_lib import now_text
from chromium_advanced.session_manager import SessionManager


MCP_INSTRUCTIONS = (
    "Use this server when a task needs a real Chromium profile with persistent login state. "
    "Start a profile session by profile_name, then use the returned session_id for browser actions."
)

DEFAULT_TIMEOUT_SECONDS = 20


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
        result = session_manager.start_session(profile_name=profile_name, reuse_existing=reuse_existing, engine_name=engine)
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
    server = build_server(config_path=config_path)
    transport = str(args.transport or "stdio").strip() or "stdio"
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


if __name__ == "__main__":
    main()
