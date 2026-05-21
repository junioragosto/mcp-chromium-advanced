import argparse
import json
import os
import tempfile
from typing import Literal, Optional

from fastmcp import FastMCP
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    MoveTargetOutOfBoundsException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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


def wait_for_element(driver, locator, timeout_seconds: int, condition: str):
    if condition == "present":
        return WebDriverWait(driver, timeout_seconds).until(EC.presence_of_element_located(locator))
    if condition == "clickable":
        return WebDriverWait(driver, timeout_seconds).until(EC.element_to_be_clickable(locator))
    return WebDriverWait(driver, timeout_seconds).until(EC.visibility_of_element_located(locator))


def scroll_into_view(driver, element) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
        element,
    )


def robust_click(driver, locator, timeout_seconds: int) -> None:
    last_error = None
    for condition in ("clickable", "visible", "present"):
        try:
            element = wait_for_element(driver, locator, timeout_seconds, condition)
        except TimeoutException as exc:
            last_error = exc
            continue
        try:
            scroll_into_view(driver, element)
        except WebDriverException:
            pass
        for click_mode in ("native", "actions", "js"):
            try:
                if click_mode == "native":
                    element.click()
                elif click_mode == "actions":
                    ActionChains(driver).move_to_element(element).pause(0.05).click().perform()
                else:
                    driver.execute_script("arguments[0].click();", element)
                return
            except (
                ElementClickInterceptedException,
                MoveTargetOutOfBoundsException,
                StaleElementReferenceException,
                WebDriverException,
                JavascriptException,
            ) as exc:
                last_error = exc
                try:
                    element = wait_for_element(driver, locator, timeout_seconds, "present")
                except TimeoutException:
                    break
    if last_error:
        raise last_error
    raise TimeoutException("failed to click element")


def clear_element(driver, element) -> None:
    try:
        element.clear()
    except WebDriverException:
        pass
    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
    except WebDriverException:
        pass
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            if (el.isContentEditable) {
                el.innerHTML = '';
                el.textContent = '';
                return;
            }
            if ('value' in el) {
                el.value = '';
            }
            """,
            element,
        )
    except JavascriptException:
        pass


def set_element_value_js(driver, element, text: str) -> None:
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        if (el.isContentEditable) {
            el.focus();
            el.innerHTML = '';
            el.textContent = value;
        } else if ('value' in el) {
            const proto = Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
            if (descriptor && descriptor.set) {
                descriptor.set.call(el, value);
            } else {
                el.value = value;
            }
            el.focus();
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        text,
    )


def robust_type_text(driver, locator, text: str, clear_first: bool, submit: bool, timeout_seconds: int) -> None:
    last_error = None
    element = wait_for_element(driver, locator, timeout_seconds, "visible")
    try:
        scroll_into_view(driver, element)
    except WebDriverException:
        pass
    try:
        robust_click(driver, locator, timeout_seconds)
        element = wait_for_element(driver, locator, timeout_seconds, "present")
    except Exception as exc:
        last_error = exc
    if clear_first:
        clear_element(driver, element)
    for input_mode in ("send_keys", "js"):
        try:
            if input_mode == "send_keys":
                element.send_keys(text)
            else:
                set_element_value_js(driver, element, text)
            if submit:
                try:
                    element.submit()
                except WebDriverException:
                    element.send_keys(Keys.ENTER)
            return
        except (
            StaleElementReferenceException,
            WebDriverException,
            JavascriptException,
        ) as exc:
            last_error = exc
            element = wait_for_element(driver, locator, timeout_seconds, "present")
            try:
                scroll_into_view(driver, element)
            except WebDriverException:
                pass
    if last_error:
        raise last_error
    raise TimeoutException("failed to type into element")


def describe_element(driver, element) -> dict:
    return {
        "tag_name": element.tag_name,
        "text": (element.text or "").strip(),
        "id": element.get_attribute("id") or "",
        "name": element.get_attribute("name") or "",
        "class": element.get_attribute("class") or "",
        "aria_label": element.get_attribute("aria-label") or "",
        "role": element.get_attribute("role") or "",
        "value": element.get_attribute("value") or "",
        "href": element.get_attribute("href") or "",
        "outer_html": driver.execute_script("return arguments[0].outerHTML;", element),
    }


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
    def inspect_elements(
        session_id: str,
        selector: str,
        by: Literal["css", "xpath", "id", "name", "tag", "class", "link_text", "partial_link_text"] = "css",
        limit: int = 10,
    ) -> dict:
        """Inspect matching elements to debug dynamic pages and refine selectors."""
        driver = session_manager.resolve_driver(session_id)
        locator_by = selector_kind_to_by(by)
        elements = driver.find_elements(locator_by, selector)
        inspected = []
        for element in elements[: max(1, int(limit))]:
            try:
                inspected.append(describe_element(driver, element))
            except WebDriverException:
                continue
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
            "count": len(elements),
            "elements": inspected,
        }

    @server.tool
    def get_active_element(session_id: str) -> dict:
        """Describe the currently focused element."""
        driver = session_manager.resolve_driver(session_id)
        element = driver.switch_to.active_element
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
            "element": describe_element(driver, element),
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
        element = wait_for_element(driver, locator, int(timeout_seconds), condition)

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
        robust_click(driver, locator, int(timeout_seconds))
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
        robust_type_text(
            driver=driver,
            locator=locator,
            text=text,
            clear_first=bool(clear_first),
            submit=bool(submit),
            timeout_seconds=int(timeout_seconds),
        )
        return {
            "session_id": session_id,
            "typed": True,
            "submitted": bool(submit),
            "url": driver.current_url,
            "title": driver.title,
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
        driver = session_manager.resolve_driver(session_id)
        target = driver.switch_to.active_element
        if str(selector or "").strip():
            locator = (selector_kind_to_by(by), selector)
            target = wait_for_element(driver, locator, int(timeout_seconds), "present")
            try:
                scroll_into_view(driver, target)
            except WebDriverException:
                pass
            try:
                target.click()
            except WebDriverException:
                pass

        normalized_key = str(key or "").strip()
        selenium_key = getattr(Keys, normalized_key.upper(), None)
        payload = selenium_key if selenium_key is not None else normalized_key
        repeat = max(1, int(count))
        for _ in range(repeat):
            target.send_keys(payload)
        return {
            "session_id": session_id,
            "pressed": True,
            "key": key,
            "count": repeat,
            "url": driver.current_url,
            "title": driver.title,
        }

    @server.tool
    def run_script(session_id: str, script: str) -> dict:
        """Run JavaScript in the current page and return a JSON-serializable result when possible."""
        driver = session_manager.resolve_driver(session_id)
        result = driver.execute_script(script)
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {
            "session_id": session_id,
            "url": driver.current_url,
            "title": driver.title,
            "result": serialized,
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
