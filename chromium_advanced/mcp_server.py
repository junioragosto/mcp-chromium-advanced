import argparse
import os
import tempfile
from typing import Literal, Optional

from fastmcp import FastMCP
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from chromium_advanced.session_manager import SessionManager


MCP_INSTRUCTIONS = (
    "Use this server when a task needs a real Chromium profile with persistent login state. "
    "Start a profile session by profile_name, then use the returned session_id for browser actions."
)

DEFAULT_TIMEOUT_SECONDS = 20


def selector_kind_to_by(by: str):
    normalized = str(by or "css").strip().lower()
    mapping = {
        "css": By.CSS_SELECTOR,
        "xpath": By.XPATH,
        "id": By.ID,
        "name": By.NAME,
        "tag": By.TAG_NAME,
        "class": By.CLASS_NAME,
        "link_text": By.LINK_TEXT,
        "partial_link_text": By.PARTIAL_LINK_TEXT,
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported selector type: {by}")
    return mapping[normalized]


def wait_until_ready(driver, timeout_seconds: int) -> None:
    WebDriverWait(driver, timeout_seconds).until(
        lambda current: current.execute_script("return document.readyState") == "complete"
    )


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
    def can_start_profile_session(profile_name: str) -> dict:
        """Check whether a new session is allowed right now for this profile."""
        return session_manager.can_start_session(profile_name)

    @server.tool
    def list_sessions() -> dict:
        """List active profile-backed browser sessions."""
        return {"sessions": session_manager.list_sessions()}

    @server.tool
    def start_profile_session(profile_name: str, reuse_existing: bool = False) -> dict:
        """Start or reuse a real logged-in browser session for the specified profile."""
        return session_manager.start_session(profile_name=profile_name, reuse_existing=reuse_existing)

    @server.tool
    def close_profile_session(session_id: str) -> dict:
        """Close an active browser session."""
        return session_manager.close_session(session_id)

    @server.tool
    def navigate(session_id: str, url: str, wait_for_ready: bool = True, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
        """Navigate the session to a URL."""
        driver = session_manager.resolve_driver(session_id)
        driver.get(url)
        if wait_for_ready:
            wait_until_ready(driver, int(timeout_seconds))
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
        }

    @server.tool
    def get_current_url(session_id: str) -> dict:
        """Get the session's current URL and title."""
        driver = session_manager.resolve_driver(session_id)
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
        }

    @server.tool
    def get_page_text(session_id: str) -> dict:
        """Extract visible page text from the current document body."""
        driver = session_manager.resolve_driver(session_id)
        body = driver.find_element(By.TAG_NAME, "body")
        text = (body.text or "").strip()
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
            "text": text,
        }

    @server.tool
    def get_page_html(session_id: str) -> dict:
        """Return the current page HTML source."""
        driver = session_manager.resolve_driver(session_id)
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
            "html": driver.page_source,
        }

    @server.tool
    def wait_for(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        condition: Literal["present", "visible", "clickable"] = "visible",
    ) -> dict:
        """Wait for an element to reach a desired state."""
        driver = session_manager.resolve_driver(session_id)
        locator = (selector_kind_to_by(by), selector)

        if condition == "present":
            element = WebDriverWait(driver, int(timeout_seconds)).until(
                EC.presence_of_element_located(locator)
            )
        elif condition == "clickable":
            element = WebDriverWait(driver, int(timeout_seconds)).until(
                EC.element_to_be_clickable(locator)
            )
        else:
            element = WebDriverWait(driver, int(timeout_seconds)).until(
                EC.visibility_of_element_located(locator)
            )

        return {
            "session_id": session_id,
            "found": True,
            "tag_name": element.tag_name,
            "text": (element.text or "").strip(),
        }

    @server.tool
    def click(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """Wait for and click an element."""
        driver = session_manager.resolve_driver(session_id)
        locator = (selector_kind_to_by(by), selector)
        element = WebDriverWait(driver, int(timeout_seconds)).until(EC.element_to_be_clickable(locator))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        element.click()
        return {
            "session_id": session_id,
            "clicked": True,
            "url": driver.current_url,
            "title": driver.title,
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
        driver = session_manager.resolve_driver(session_id)
        locator = (selector_kind_to_by(by), selector)
        element = WebDriverWait(driver, int(timeout_seconds)).until(EC.visibility_of_element_located(locator))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        if clear_first:
            try:
                element.clear()
            except Exception:
                pass
        element.send_keys(text)
        if submit:
            element.submit()
        return {
            "session_id": session_id,
            "typed": True,
            "submitted": bool(submit),
            "url": driver.current_url,
            "title": driver.title,
        }

    @server.tool
    def screenshot(session_id: str, filename: str = "") -> dict:
        """Save a screenshot to disk and return the file path."""
        driver = session_manager.resolve_driver(session_id)
        output_path = str(filename or "").strip()
        if not output_path:
            safe_name = session_id.replace(os.sep, "_")
            output_path = os.path.join(tempfile.gettempdir(), f"{safe_name}.png")
        driver.save_screenshot(output_path)
        return {
            "session_id": session_id,
            "path": output_path,
            "url": driver.current_url,
            "title": driver.title,
        }

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
