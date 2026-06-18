import hashlib
import importlib.util
import inspect
import os
import signal
import random
import re
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import quote_plus

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from chromium_advanced.mcp_runtime_config import resolve_mcp_headless, resolve_mcp_start_minimized


def _lib():
    from chromium_advanced import chromium_profile_lib as lib

    return lib


def _builtin_keepalive_site_actions() -> Dict[str, Callable]:
    actions = getattr(_lib(), "BUILTIN_KEEPALIVE_SITE_ACTIONS", None)
    if isinstance(actions, dict):
        return actions
    return BUILTIN_KEEPALIVE_SITE_ACTIONS


def _run_profile_keepalive_func():
    func = getattr(_lib(), "run_profile_keepalive", None)
    if callable(func) and getattr(func, "__module__", "") != __name__:
        return func
    return run_profile_keepalive


class KeepAliveLoginRequiredError(RuntimeError):
    def __init__(self, site_name: str, message: str):
        super().__init__(message)
        self.site_name = site_name


class KeepAliveSoftFailureError(RuntimeError):
    def __init__(self, site_name: str, message: str):
        super().__init__(message)
        self.site_name = site_name


def cleanup_keepalive_profile_processes(
    config: Dict,
    profile_name: str,
    before_pids: Optional[Sequence[int]] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> int:
    before = {int(pid) for pid in (before_pids or []) if str(pid).isdigit()}
    matches = []
    for item in _lib().get_chromium_processes_for_profile(config, profile_name):
        try:
            pid = int(item.get("pid") or 0)
        except Exception:
            pid = 0
        if pid and pid not in before:
            matches.append(item)
    return _lib().terminate_chromium_processes(matches, logger=logger)


def _get_driver_service_pid(driver) -> int:
    try:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        pid = int(getattr(process, "pid", 0) or 0)
        if pid > 0:
            return pid
    except Exception:
        pass
    return 0


def terminate_driver_service_process(driver) -> int:
    pid = _get_driver_service_pid(driver)
    if pid <= 0:
        return 0
    try:
        proc = _lib().psutil.Process(pid)
    except Exception:
        return 0

    killed = 0
    try:
        for child in proc.children(recursive=True):
            try:
                child.kill()
                killed += 1
            except Exception:
                pass
    except Exception:
        pass

    try:
        proc.kill()
        killed += 1
    except Exception:
        pass
    return killed


def safe_quit_driver(driver) -> int:
    terminated = 0
    if driver is None:
        return terminated
    try:
        driver.quit()
    except Exception:
        pass
    try:
        terminated += terminate_driver_service_process(driver)
    except Exception:
        pass
    return terminated


def is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = (
        "invalid session id",
        "chrome not reachable",
        "disconnected",
        "target window already closed",
        "no such window",
        "web view not found",
        "session deleted",
        "connection refused",
    )
    return any(marker in text for marker in markers)


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
                safe_quit_driver(driver)
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


def normalize_keepalive_locator_by(value: str) -> str:
    normalized = str(value or "css").strip().lower()
    mapping = {
        "css": By.CSS_SELECTOR,
        "xpath": By.XPATH,
        "id": By.ID,
        "name": By.NAME,
        "class": By.CLASS_NAME,
        "tag": By.TAG_NAME,
        "link_text": By.LINK_TEXT,
        "partial_link_text": By.PARTIAL_LINK_TEXT,
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported locator type: {value}")
    return mapping[normalized]


class KeepaliveResultFactory:
    def success(self, message: str, **extra) -> Dict:
        return {"status": "success", "message": str(message or "success"), **extra}

    def signed_out(self, message: str, **extra) -> Dict:
        return {"status": "signed_out", "message": str(message or "signed out"), "signed_in": False, **extra}

    def attention(self, message: str, **extra) -> Dict:
        return {"status": "attention", "message": str(message or "attention"), **extra}

    def failed(self, message: str, **extra) -> Dict:
        return {"status": "failed", "message": str(message or "failed"), **extra}

    def skipped(self, message: str, **extra) -> Dict:
        return {"status": "skipped", "message": str(message or "skipped"), **extra}


class KeepaliveBrowserApi:
    def __init__(self, driver, settings: Dict, stop_controller: Optional[KeepAliveStopController] = None):
        self.driver = driver
        self.settings = settings
        self.stop_controller = stop_controller

    def _timeout(self, timeout: Optional[int] = None) -> int:
        value = timeout if timeout is not None else self.settings.get("page_timeout_seconds", 45)
        return max(1, int(value))

    def _check(self) -> None:
        if self.stop_controller:
            self.stop_controller.check_or_raise()

    def wait_ready(self, timeout: Optional[int] = None) -> None:
        self._check()
        WebDriverWait(self.driver, self._timeout(timeout)).until(
            lambda current: current.execute_script("return document.readyState") == "complete"
        )
        self._check()

    def goto(self, url: str, wait_ready: bool = True, timeout: Optional[int] = None) -> str:
        self._check()
        self.driver.get(str(url))
        if wait_ready:
            self.wait_ready(timeout=timeout)
        return self.current_url()

    def sleep(self, seconds: int) -> None:
        interruptible_sleep(int(seconds), self.stop_controller)

    def current_url(self) -> str:
        return str(getattr(self.driver, "current_url", "") or "")

    def title(self) -> str:
        return str(getattr(self.driver, "title", "") or "")

    def execute(self, script: str, *args):
        self._check()
        return self.driver.execute_script(script, *args)

    def find(self, selector: str, by: str = "css", timeout: Optional[int] = None):
        self._check()
        locator_by = normalize_keepalive_locator_by(by)
        if timeout is None or int(timeout) <= 0:
            return self.driver.find_element(locator_by, selector)
        return WebDriverWait(self.driver, self._timeout(timeout)).until(
            lambda current: current.find_element(locator_by, selector)
        )

    def find_all(self, selector: str, by: str = "css") -> List:
        self._check()
        return self.driver.find_elements(normalize_keepalive_locator_by(by), selector)

    def exists(self, selector: str, by: str = "css", timeout: int = 0) -> bool:
        try:
            element = self.find(selector, by=by, timeout=timeout)
            return bool(element)
        except Exception:
            return False

    def click(self, selector: str, by: str = "css", timeout: Optional[int] = None):
        element = self.find(selector, by=by, timeout=timeout)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)
        self._check()
        return element

    def fill(self, selector: str, text: str, by: str = "css", timeout: Optional[int] = None, clear: bool = True):
        element = self.find(selector, by=by, timeout=timeout)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].focus();", element)
        if clear:
            try:
                element.clear()
            except Exception:
                pass
            try:
                element.send_keys(Keys.CONTROL, "a")
                element.send_keys(Keys.DELETE)
            except Exception:
                pass
        element.send_keys(str(text or ""))
        self._check()
        return element

    def press(self, keys, selector: str = "", by: str = "css", timeout: Optional[int] = None):
        element = self.find(selector, by=by, timeout=timeout) if selector else None
        target = element or self.driver.switch_to.active_element
        if isinstance(keys, (list, tuple)):
            target.send_keys(*keys)
        else:
            target.send_keys(keys)
        self._check()
        return target

    def text(self, selector: str = "", by: str = "css", timeout: Optional[int] = None) -> str:
        if not selector:
            return str(self.driver.execute_script("return document.body ? (document.body.innerText || '') : '';") or "")
        return str((self.find(selector, by=by, timeout=timeout).text or "").strip())

    def html(self, selector: str = "", by: str = "css", timeout: Optional[int] = None) -> str:
        if not selector:
            return str(self.driver.execute_script("return document.documentElement.outerHTML || '';") or "")
        return str(self.find(selector, by=by, timeout=timeout).get_attribute("outerHTML") or "")


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
    if raw_prompt and raw_prompt != _lib().LEGACY_CHATGPT_PROMPT:
        return raw_prompt
    return random.choice(_lib().DEFAULT_CHATGPT_PROMPTS)


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


def is_chatgpt_authenticated(driver) -> bool:
    try:
        current_url = str(driver.current_url or "").lower()
    except Exception:
        current_url = ""
    if "auth" in current_url or "login" in current_url:
        return False

    try:
        authenticated = bool(
            driver.execute_script(
                """
                const href = String(location.href || '').toLowerCase();
                if (href.includes('/auth') || href.includes('/login')) return false;
                const loginText = Array.from(document.querySelectorAll('a, button'))
                  .map((node) => (node.innerText || node.textContent || '').trim().toLowerCase())
                  .filter(Boolean)
                  .slice(0, 80);
                if (loginText.some((text) => text === 'log in' || text === 'sign up' || text === '鐧诲綍' || text === '娉ㄥ唽')) {
                  return false;
                }
                const hasConversation = !!document.querySelector("a[href^='/c/'], a[href*='/c/']");
                const hasAccountMenu = !!document.querySelector(
                  "[data-testid*='profile'], [data-testid*='account'], button[aria-label*='profile' i], button[aria-label*='account' i]"
                );
                const hasComposer = !!document.querySelector(
                  "#prompt-textarea[contenteditable='true'], textarea#prompt-textarea, [contenteditable='true'][data-lexical-editor='true']"
                );
                const hasAppChrome = !!document.querySelector("nav, aside, main");
                return hasConversation || hasAccountMenu || (hasComposer && hasAppChrome && !href.includes('logged-out'));
                """
            )
        )
    except Exception:
        authenticated = False
    return authenticated


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
            _lib().log_message(logger, f"ChatGPT conversation selected: {title}")
            return title

        last_error = "no existing conversation found in sidebar"
        time.sleep(0.5)

    raise RuntimeError(last_error or "failed to open existing ChatGPT conversation")


def dismiss_google_consent_if_needed(driver) -> None:
    button_texts = [
        "I agree",
        "Accept all",
        "Reject all",
        "Accept",
        "鎺ュ彈鍏ㄩ儴",
        "鍚屾剰",
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
        _lib().log_message(logger, f"Google results ready; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    _lib().log_message(logger, f"Google search ok: {query}")
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
                _lib().log_message(logger, f"Gmail first message opened; staying {dwell_seconds}s")
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
            _lib().log_message(logger, "Gmail inbox opened and first message previewed.")
            return {"status": "success", "message": f"opened inbox, previewed first email, stayed {dwell_seconds}s"}
        except Exception:
            pass

    if dwell_seconds > 0:
        _lib().log_message(logger, f"Gmail inbox loaded; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    _lib().log_message(logger, "Gmail inbox loaded.")
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
    if "auth" in current_url or "login" in current_url or not is_chatgpt_authenticated(driver):
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
            _lib().log_message(logger, f"ChatGPT signed in but no reusable conversation found; staying {dwell_seconds}s")
            interruptible_sleep(dwell_seconds, stop_controller)
        _lib().log_message(logger, "ChatGPT composer is available without reusable sidebar conversation.")
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
        _lib().log_message(logger, f"ChatGPT reply observed; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    _lib().log_message(logger, "ChatGPT prompt sent in existing conversation and reply observed.")
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
        _lib().log_message(logger, f"GitHub pulls page loaded; staying {dwell_seconds}s")
        interruptible_sleep(dwell_seconds, stop_controller)
    _lib().log_message(logger, "GitHub pulls page loaded.")
    return {"status": "success", "message": f"pull requests page loaded and stayed {dwell_seconds}s"}


BUILTIN_KEEPALIVE_SITE_ACTIONS = {
    "chatgpt": keepalive_chatgpt,
    "gmail": keepalive_gmail,
    "google": keepalive_google,
    "github": keepalive_github,
}


def run_external_keepalive_plugin(
    site_id: str,
    metadata: Dict,
    driver,
    settings: Dict,
    profile_entry: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    source = str(metadata.get("source", "") or "").strip()
    if not source or not os.path.exists(source):
        raise RuntimeError(f"keepalive plugin source not found for {site_id}: {source}")
    module_name = str(metadata.get("module_name", "") or "").strip() or (
        f"chromium_advanced_user_keepalive_{hashlib.sha1(source.encode('utf-8')).hexdigest()[:12]}"
    )
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load keepalive plugin spec for {site_id}: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    action = None
    class_name = str(metadata.get("class_name", "") or "").strip()
    if class_name:
        plugin_class = getattr(module, class_name, None)
        if inspect.isclass(plugin_class):
            action = getattr(plugin_class(), str(metadata.get("function_name", "keepalive") or "keepalive"), None)
    if action is None:
        action = getattr(module, str(metadata.get("function_name", "keepalive") or "keepalive"), None)
    if not callable(action):
        raise RuntimeError(f"keepalive plugin {site_id} does not expose callable keepalive(context)")
    browser = KeepaliveBrowserApi(driver, settings, stop_controller=stop_controller)
    results = KeepaliveResultFactory()
    context = {
        "site_id": site_id,
        "metadata": metadata,
        "driver": driver,
        "browser": browser,
        "results": results,
        "settings": settings,
        "profile": profile_entry,
        "logger": logger,
        "stop_controller": stop_controller,
        "log": lambda message: _lib().log_message(logger, f"{site_id}: {message}"),
    }
    result = action(context)
    if not isinstance(result, dict):
        raise RuntimeError(f"keepalive plugin {site_id} returned non-dict result")
    return result


def run_keepalive_site_action(
    site_id: str,
    metadata: Dict,
    driver,
    settings: Dict,
    profile_entry: Dict,
    logger: Optional[Callable[[str], None]],
    stop_controller: Optional[KeepAliveStopController] = None,
) -> Dict:
    normalized = _lib().normalize_site_id(site_id)
    actions = _builtin_keepalive_site_actions()
    if normalized in actions:
        return actions[normalized](driver, settings, logger, stop_controller)
    return run_external_keepalive_plugin(normalized, metadata, driver, settings, profile_entry, logger, stop_controller)


def create_driver_for_profile(config: Dict, profile_name: str):
    paths = config["paths"]
    chromium_binary = _lib().resolve_chromium_binary(paths.get("chromium_dir", ""))
    chromedriver_binary = _lib().resolve_chromedriver_path(paths.get("chromedriver_path", ""))
    user_data_root = _lib().get_profile_user_data_root(config, profile_name)

    if not chromium_binary or not os.path.exists(chromium_binary):
        raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
    if not chromedriver_binary or not os.path.exists(chromedriver_binary):
        raise FileNotFoundError(f"chromedriver not found: {chromedriver_binary or paths.get('chromedriver_path', '')}")
    if not os.path.isdir(user_data_root):
        raise FileNotFoundError(f"Profile UserData root not found: {user_data_root}")

    options = uc.ChromeOptions()
    options.binary_location = chromium_binary
    options.add_argument(f"--user-data-dir={user_data_root}")
    options.add_argument(f"--profile-directory={profile_name}")
    for item in _lib().get_chromium_restore_prompt_suppression_args():
        options.add_argument(item)
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

    extension_dir = _lib().detect_fingerprint_extension_dir(paths.get("fingerprint_zip_path", ""))
    if extension_dir:
        options.add_argument(f"--load-extension={extension_dir}")

    kwargs = {
        "driver_executable_path": chromedriver_binary,
        "options": options,
        "use_subprocess": True,
    }
    version_main = _lib().detect_chromium_major_version(paths.get("chromium_dir", ""))
    if version_main:
        kwargs["version_main"] = version_main

    driver = uc.Chrome(**kwargs)
    try:
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
    progress_callback: Optional[Callable[[str, Dict], None]] = None,
) -> Dict:
    settings = config["keepalive"]
    profile_entry = next(
        (item for item in config.get("profiles", []) if item.get("profile_name") == profile_name),
        {},
    )
    site_registry = _lib().get_keepalive_site_registry(config)
    enabled_sites = _lib().normalize_keepalive_site_flags(
        profile_entry.get("keepalive_sites", {}),
        default=False,
        site_ids=_lib().get_keepalive_site_ids(config),
    )
    driver = None
    site_results: Dict[str, Dict[str, str]] = {}
    failed_sites = []
    disabled_sites = []
    soft_sites = []
    profile_lock = _lib().SingleRunLock(_lib().get_profile_runtime_lock_path(config, profile_name))
    before_pids = [
        int(item.get("pid") or 0)
        for item in _lib().get_chromium_processes_for_profile(config, profile_name)
        if int(item.get("pid") or 0) > 0
    ]

    try:
        if before_pids:
            return {
                "profile_name": profile_name,
                "status": "skipped",
                "message": f"{profile_name} chromium already running",
                "details": {},
                "disabled_sites": [],
            }
        try:
            lock_path = _lib().get_profile_runtime_lock_path(config, profile_name)
            if lock_path:
                _lib().clear_stale_lockfile(lock_path, stale_seconds=1)
        except Exception:
            pass
        if not profile_lock.try_acquire():
            return {
                "profile_name": profile_name,
                "status": "skipped",
                "message": f"{profile_name} runtime lock is already held",
                "details": {},
                "disabled_sites": [],
            }
        if stop_controller:
            stop_controller.check_or_raise()
        if progress_callback:
            progress_callback("profile_start", {"profile_name": profile_name, "lock_acquired": True})
        driver_factory = getattr(_lib(), "create_driver_for_profile", None)
        if callable(driver_factory) and getattr(driver_factory, "__module__", "") != __name__:
            driver = driver_factory(config, profile_name)
        else:
            driver = create_driver_for_profile(config, profile_name)
        if stop_controller:
            stop_controller.bind_driver(driver)
            stop_controller.check_or_raise()
        interruptible_sleep(int(settings["settle_seconds"]), stop_controller)

        actions = [
            (site_id, site_registry.get(site_id, {}))
            for site_id in _lib().get_keepalive_site_ids(config)
            if enabled_sites.get(site_id)
        ]

        if not actions:
            return {
                "profile_name": profile_name,
                "status": "skipped",
                "message": "no keepalive sites checked for this profile",
                "details": {},
                "disabled_sites": [],
            }

        for site_name, metadata in actions:
            if stop_controller:
                stop_controller.check_or_raise()
            try:
                _lib().log_message(logger, f"{profile_name}: start {site_name}")
                action_func = getattr(_lib(), "run_keepalive_site_action", None)
                if not callable(action_func) or getattr(action_func, "__module__", "") == __name__:
                    action_func = run_keepalive_site_action
                result = _lib().normalize_keepalive_action_result(
                    site_name,
                    action_func(site_name, metadata, driver, settings, profile_entry, logger, stop_controller),
                )
                site_results[site_name] = result
                if result["status"] == "signed_out":
                    disabled_sites.append(site_name)
                    soft_sites.append(site_name)
                    _lib().log_message(logger, f"{profile_name}: {site_name} signed out; unchecked for next run")
                elif result["status"] == "attention":
                    soft_sites.append(site_name)
                    _lib().log_message(logger, f"{profile_name}: {site_name} attention: {result['message']}")
                elif result["status"] == "failed":
                    failed_sites.append(site_name)
                    _lib().log_message(logger, f"{profile_name}: {site_name} failed: {result['message']}")
                elif result["status"] == "skipped":
                    soft_sites.append(site_name)
                    _lib().log_message(logger, f"{profile_name}: {site_name} skipped: {result['message']}")
                if stop_controller:
                    stop_controller.check_or_raise()
            except KeepAliveStoppedError:
                raise
            except KeepAliveLoginRequiredError as exc:
                disabled_sites.append(site_name)
                soft_sites.append(site_name)
                site_results[site_name] = {"status": "signed_out", "message": str(exc), "signed_in": False}
                _lib().log_message(logger, f"{profile_name}: {site_name} signed out; unchecked for next run")
            except KeepAliveSoftFailureError as exc:
                soft_sites.append(site_name)
                site_results[site_name] = {"status": "attention", "message": str(exc), "signed_in": True}
                _lib().log_message(logger, f"{profile_name}: {site_name} attention: {exc}")
            except Exception as exc:
                if stop_controller and stop_controller.should_stop():
                    raise KeepAliveStoppedError("keepalive stopped by user") from exc
                if is_browser_closed_error(exc):
                    soft_sites.append(site_name)
                    site_results[site_name] = {"status": "attention", "message": str(exc), "signed_in": None}
                    _lib().log_message(logger, f"{profile_name}: {site_name} browser state needs recheck: {exc}")
                else:
                    failed_sites.append(site_name)
                    site_results[site_name] = {"status": "failed", "message": str(exc), "signed_in": None}
                    _lib().log_message(logger, f"{profile_name}: {site_name} failed: {exc}")

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
                safe_quit_driver(driver)
            except Exception:
                pass
        cleanup_keepalive_profile_processes(config, profile_name, before_pids=before_pids, logger=logger)
        try:
            _lib().cleanup_profile_user_data_root(_lib().get_profile_user_data_root(config, profile_name))
        except Exception:
            pass
        profile_lock.release()


def run_keepalive_job(
    config_path: Optional[str] = None,
    selected_profiles: Optional[Sequence[str]] = None,
    logger: Optional[Callable[[str], None]] = None,
    source: str = "manual",
    stop_controller: Optional[KeepAliveStopController] = None,
    progress_callback: Optional[Callable[[str, Dict], None]] = None,
) -> Dict:
    from chromium_advanced.mirror_manager import MirrorManager
    from chromium_advanced.session_manager import SessionManager

    path = config_path or _lib().get_default_config_path()
    config = _lib().load_app_config(path)
    session_manager = SessionManager(config_path=path, config_override=config)
    lock = _lib().SingleRunLock(_lib().get_lock_path())
    mirror_lock_path = _lib().get_mirror_lock_path()

    if not lock.try_acquire():
        summary = {
            "status": "skipped",
            "message": "keepalive job already running",
            "profile_results": [],
            "started_at": _lib().now_text(),
            "finished_at": _lib().now_text(),
            "source": source,
        }
        _lib().log_message(logger, summary["message"])
        return summary

    try:
        started_at = _lib().now_text()
        keepalive = config["keepalive"]
        keepalive["last_run_at"] = started_at
        keepalive["last_run_finished_at"] = ""
        keepalive["last_run_status"] = "running"
        keepalive["last_run_message"] = "keepalive job started"
        keepalive["last_run_source"] = source
        _lib().save_app_config(config, path)

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
            finished_at = _lib().now_text()
            keepalive["last_run_finished_at"] = finished_at
            keepalive["last_run_status"] = "skipped"
            keepalive["last_run_message"] = "no profiles selected for keepalive"
            keepalive["last_run_profile_count"] = 0
            keepalive["last_run_details"] = []
            _lib().save_app_config(config, path)
            summary = {
                "status": "skipped",
                "message": "no profiles selected for keepalive",
                "profile_results": [],
                "started_at": started_at,
                "finished_at": finished_at,
                "source": source,
            }
            _lib().log_message(logger, summary["message"])
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
                _lib().log_message(logger, f"keepalive start: {profile_name}")
                profile_running_processes = _lib().get_chromium_processes_for_profile(config, profile_name)
                profile_lock_path = _lib().get_profile_runtime_lock_path(config, profile_name)
                profile_lock_active = bool(profile_lock_path and os.path.exists(profile_lock_path))
                preflight = session_manager.can_start_session(
                    profile_name,
                    engine_name=str(config.get("app", {}).get("browser_engine", "") or ""),
                )
                starting_profiles = preflight.get("status", {}).get("starting_profiles", [])
                starting_profile_names = {
                    str(item.get("profile_name", "") or "").strip()
                    for item in starting_profiles
                    if isinstance(item, dict)
                }
                block_reason = ""
                if profile_running_processes:
                    block_reason = f"{profile_name} chromium already running"
                elif profile_lock_active:
                    block_reason = f"{profile_name} runtime lock is already held"
                elif profile_name in starting_profile_names:
                    block_reason = f"profile is starting: {profile_name}"
                elif int(preflight.get("active_profile_session_count", 0) or 0) > 0:
                    block_reason = str(preflight.get("reason", "") or "profile is already in use by another MCP session")
                elif int(preflight.get("external_profile_process_count", 0) or 0) > 0:
                    block_reason = str(preflight.get("reason", "") or "profile chromium is already running")
                elif not bool(preflight.get("allowed")) and "reusable session" in str(preflight.get("reason", "") or "").lower():
                    block_reason = str(preflight.get("reason", "") or "profile already has a reusable session")
                elif not bool(preflight.get("allowed")) and (
                    bool(preflight.get("profile_lock_active"))
                    or int(preflight.get("active_profile_session_count", 0) or 0) > 0
                    or int(preflight.get("external_profile_process_count", 0) or 0) > 0
                ):
                    block_reason = str(preflight.get("reason", "") or "profile is unavailable")
                if block_reason:
                    result = {
                        "profile_name": profile_name,
                        "status": "skipped",
                        "message": block_reason,
                        "details": {
                            "preflight": preflight,
                            "profile_lock_active": profile_lock_active,
                            "external_profile_process_count": len(profile_running_processes),
                        },
                        "disabled_sites": [],
                    }
                    _lib().log_message(logger, f"{profile_name}: skipped before keepalive start: {result['message']}")
                    for profile in config["profiles"]:
                        if profile.get("profile_name") != profile_name:
                            continue
                        profile["last_keepalive_at"] = _lib().now_text()
                        profile["last_keepalive_status"] = result["status"]
                        profile["last_keepalive_message"] = result["message"]
                        profile["last_keepalive_details"] = result.get("details", {})
                        break
                    profile_results.append(result)
                    any_skipped = True
                    _lib().save_app_config(config, path)
                    if index < len(target_profiles) - 1:
                        interruptible_sleep(int(config["keepalive"]["between_profiles_seconds"]), stop_controller)
                    continue

                try:
                    result = _run_profile_keepalive_func()(
                        config,
                        profile_name,
                        logger=logger,
                        stop_controller=stop_controller,
                        progress_callback=progress_callback,
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
                        profile["last_keepalive_at"] = _lib().now_text()
                        profile["last_keepalive_status"] = result["status"]
                        profile["last_keepalive_message"] = result["message"]
                        profile["last_keepalive_details"] = result.get("details", {})
                        break
                    profile_results.append(result)
                    _lib().save_app_config(config, path)
                    raise
                except Exception as exc:
                    result = {
                        "profile_name": profile_name,
                        "status": "failed",
                        "message": str(exc),
                        "details": {"exception": {"status": "failed", "message": traceback.format_exc(limit=5)}},
                    }
                    _lib().log_message(logger, f"{profile_name}: fatal keepalive error: {exc}")

                for profile in config["profiles"]:
                    if profile.get("profile_name") != profile_name:
                        continue
                    profile_sites = _lib().normalize_keepalive_site_flags(
                        profile.get("keepalive_sites", {}),
                        default=False,
                        site_ids=_lib().get_keepalive_site_ids(config),
                    )
                    for site_name in result.get("disabled_sites", []):
                        profile_sites[site_name] = False
                    profile["keepalive_sites"] = profile_sites
                    profile["last_keepalive_at"] = _lib().now_text()
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
                _lib().save_app_config(config, path)

                if index < len(target_profiles) - 1:
                    interruptible_sleep(int(config["keepalive"]["between_profiles_seconds"]), stop_controller)
        except KeepAliveStoppedError as exc:
            finished_at = _lib().now_text()
            final_status = "stopped"
            final_message = str(exc)
            keepalive["last_run_finished_at"] = finished_at
            keepalive["last_run_status"] = final_status
            keepalive["last_run_message"] = final_message
            keepalive["last_run_profile_count"] = len(profile_results)
            keepalive["last_run_details"] = profile_results
            _lib().save_app_config(config, path)

            summary = {
                "status": final_status,
                "message": final_message,
                "profile_results": profile_results,
                "started_at": started_at,
                "finished_at": finished_at,
                "source": source,
            }
            _lib().log_message(logger, f"keepalive finished: {final_status}")
            return summary

        finished_at = _lib().now_text()
        if any_failed:
            final_status = "failed"
            final_message = "at least one profile failed"
        elif any_partial:
            final_status = "partial"
            final_message = "at least one profile partially failed"
        elif profile_results and all(item.get("status") == "skipped" for item in profile_results):
            final_status = "skipped"
            if len(profile_results) == 1 and profile_results[0].get("message"):
                final_message = str(profile_results[0].get("message"))
            else:
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
        _lib().save_app_config(config, path)

        mirror_summary = None
        mirror_settings = config.get("mirror", {})
        should_run_mirror = bool(mirror_settings.get("enabled", False)) and any(
            str(item.get("status", "") or "").strip().lower() in {"success", "partial", "failed"}
            for item in profile_results
        )
        if should_run_mirror:
            config["mirror"]["last_run_at"] = _lib().now_text()
            config["mirror"]["last_run_finished_at"] = ""
            config["mirror"]["last_run_status"] = "running"
            config["mirror"]["last_run_message"] = "mirror snapshot job started"
            _lib().save_app_config(config, path)
            try:
                _lib().write_json_atomic(mirror_lock_path, {"started_at": _lib().now_text(), "source": source})
            except Exception:
                pass
            try:
                mirror_manager = MirrorManager(config)
                mirror_summary = mirror_manager.refresh_snapshots(
                    profile_names=[item.get("profile_name", "") for item in target_profiles if item.get("profile_name")],
                    logger=logger,
                )
                config = _lib().load_app_config(path)
                config["mirror"]["last_run_finished_at"] = mirror_summary.get("finished_at", _lib().now_text())
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
                        profile["last_mirror_at"] = mirror_summary.get("finished_at", _lib().now_text())
                        profile["last_mirror_status"] = profile_result.get("status", "success")
                        profile["last_mirror_message"] = profile_result.get(
                            "message",
                            mirror_summary.get("message", "mirror snapshots updated"),
                        )
                        break
                _lib().save_app_config(config, path)
            except Exception as exc:
                config = _lib().load_app_config(path)
                config["mirror"]["last_run_finished_at"] = _lib().now_text()
                config["mirror"]["last_run_status"] = "failed"
                config["mirror"]["last_run_message"] = str(exc)
                _lib().save_app_config(config, path)
                _lib().log_message(logger, f"mirror finished: failed ({exc})")
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
        _lib().log_message(logger, f"keepalive finished: {final_status}")
        return summary
    finally:
        lock.release()
