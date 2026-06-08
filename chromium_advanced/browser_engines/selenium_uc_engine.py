from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    MoveTargetOutOfBoundsException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
DEBUG_EVENT_LIMIT = 400


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
        const normalize = input => String(input ?? '');
        const previousValue = 'value' in el ? normalize(el.value) : normalize(el.textContent);
        if (el.isContentEditable) {
            el.focus();
            el.innerHTML = '';
            el.textContent = value;
        } else if ('value' in el) {
            const proto =
                el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : el.tagName === 'INPUT'
                      ? HTMLInputElement.prototype
                      : Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
            if (descriptor && descriptor.set) {
                descriptor.set.call(el, value);
            } else {
                el.value = value;
            }
            el.focus();
        }
        const tracker = el._valueTracker;
        if (tracker && tracker.setValue) {
            tracker.setValue(previousValue);
        }
        try {
            el.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
        } catch (error) {
            // Older Chromium builds may not expose InputEvent as a constructor.
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        text,
    )


def read_element_input_value(driver, element) -> str:
    try:
        value = driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return '';
            if (el.isContentEditable) {
                return String(el.innerText || el.textContent || '');
            }
            if ('value' in el) {
                return String(el.value || '');
            }
            return String(el.textContent || '');
            """,
            element,
        )
        return str(value or "")
    except JavascriptException:
        try:
            return str(element.get_attribute("value") or "")
        except WebDriverException:
            return ""


def normalize_input_value_for_compare(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def input_value_matches_expected(actual_value: str, expected_text: str) -> bool:
    actual_text = str(actual_value or "")
    expected = str(expected_text or "")
    if actual_text == expected:
        return True
    return normalize_input_value_for_compare(actual_text) == normalize_input_value_for_compare(expected)


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
            current_value = read_element_input_value(driver, element)
            if not input_value_matches_expected(current_value, text):
                raise ValueError(
                    f'Input value did not settle after {input_mode}: expected "{text}", got "{current_value}"'
                )
            if submit:
                try:
                    element.submit()
                except WebDriverException:
                    element.send_keys(Keys.ENTER)
            return
        except (StaleElementReferenceException, WebDriverException, JavascriptException) as exc:
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


def element_box(driver, element) -> dict:
    try:
        rect = driver.execute_script(
            """
            const r = arguments[0].getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
            """,
            element,
        )
        return rect or {}
    except WebDriverException:
        return {}


def get_viewport_metrics(driver) -> dict:
    metrics = driver.execute_script(
        """
        return {
          width: Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0),
          height: Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0),
          device_pixel_ratio: window.devicePixelRatio || 1
        };
        """
    )
    return metrics or {"width": 0, "height": 0, "device_pixel_ratio": 1}


def ensure_viewport_point(metrics: dict, x: float, y: float) -> None:
    width = float(metrics.get("width", 0) or 0)
    height = float(metrics.get("height", 0) or 0)
    if x < 0 or y < 0 or x > width or y > height:
        raise MoveTargetOutOfBoundsException(
            f"Viewport point is out of bounds: ({x}, {y}) not within 0..{width}, 0..{height}"
        )


def dispatch_cdp_mouse(driver, event_type: str, x: float, y: float, *, button: str = "left", click_count: int = 1) -> None:
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": str(event_type),
            "x": float(x),
            "y": float(y),
            "button": str(button),
            "buttons": 1 if str(button) == "left" else 2 if str(button) == "right" else 4,
            "clickCount": max(1, int(click_count)),
        },
    )


class SeleniumBrowserSession(BrowserSession):
    def __init__(self, driver):
        self.driver = driver
        self._console_messages: list[Dict[str, Any]] = []
        self._page_errors: list[Dict[str, Any]] = []
        self._network_requests: list[Dict[str, Any]] = []

    def _append_limited(self, bucket: list[Dict[str, Any]], payload: Dict[str, Any]) -> None:
        bucket.append(payload)
        overflow = len(bucket) - DEBUG_EVENT_LIMIT
        if overflow > 0:
            del bucket[:overflow]

    def _make_timestamp(self) -> float:
        return round(time.time(), 3)

    def _list_window_handles(self) -> list[str]:
        try:
            return list(self.driver.window_handles)
        except Exception:
            return []

    def _current_tab_id(self) -> str:
        try:
            return str(self.driver.current_window_handle)
        except Exception:
            return ""

    def _tab_entry(self, handle: str, index: int) -> Dict[str, Any]:
        original = self._current_tab_id()
        title = ""
        url = ""
        alive = True
        try:
            self.driver.switch_to.window(handle)
            title = str(self.driver.title or "")
            url = str(self.driver.current_url or "")
        except Exception:
            alive = False
        finally:
            if original and handle != original:
                try:
                    self.driver.switch_to.window(original)
                except Exception:
                    pass
        return {
            "tab_id": str(handle),
            "index": int(index),
            "url": url,
            "title": title,
            "active": str(handle) == original,
            "alive": bool(alive),
        }

    def _resolve_handle(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> str:
        handles = self._list_window_handles()
        if not handles:
            raise RuntimeError("No live tabs are available in the current session.")
        normalized_tab_id = str(tab_id or "").strip()
        if normalized_tab_id:
            if normalized_tab_id in handles:
                return normalized_tab_id
            raise ValueError(f"Tab not found: {normalized_tab_id}")
        if int(index) >= 0:
            if int(index) >= len(handles):
                raise ValueError(f"Tab index out of range: {index}")
            return handles[int(index)]
        title_filter = str(title_contains or "").strip().lower()
        url_filter = str(url_contains or "").strip().lower()
        if title_filter or url_filter:
            original = self._current_tab_id()
            try:
                for handle in handles:
                    try:
                        self.driver.switch_to.window(handle)
                        current_title = str(self.driver.title or "").lower()
                        current_url = str(self.driver.current_url or "").lower()
                        if title_filter and title_filter in current_title:
                            return handle
                        if url_filter and url_filter in current_url:
                            return handle
                    except Exception:
                        continue
            finally:
                if original:
                    try:
                        self.driver.switch_to.window(original)
                    except Exception:
                        pass
        return self._current_tab_id() or handles[0]

    def _activate_handle(self, handle: str) -> str:
        self.driver.switch_to.window(handle)
        return handle

    def _poll_debug_logs(self) -> None:
        current_tab_id = self._current_tab_id()
        try:
            browser_logs = self.driver.get_log("browser")
        except Exception:
            browser_logs = []
        for entry in browser_logs or []:
            level = str(entry.get("level", "") or "").lower()
            message = str(entry.get("message", "") or "")
            payload = {
                "timestamp": self._make_timestamp(),
                "tab_id": current_tab_id,
                "type": level,
                "text": message,
                "location": {},
            }
            self._append_limited(self._console_messages, payload)
            if level in {"severe", "error"}:
                self._append_limited(
                    self._page_errors,
                    {
                        "timestamp": payload["timestamp"],
                        "tab_id": current_tab_id,
                        "message": message,
                    },
                )

        try:
            perf_logs = self.driver.get_log("performance")
        except Exception:
            perf_logs = []
        for entry in perf_logs or []:
            try:
                message = json.loads(str(entry.get("message", "") or "")).get("message", {})
            except Exception:
                continue
            method = str(message.get("method", "") or "")
            params = message.get("params", {}) or {}
            if method == "Network.requestWillBeSent":
                request = params.get("request", {}) or {}
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": current_tab_id,
                    "request_id": str(params.get("requestId", "") or ""),
                    "event": "request",
                    "method": str(request.get("method", "") or ""),
                    "url": str(request.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": False,
                    "status": None,
                    "ok": None,
                    "failure": "",
                }
                self._append_limited(self._network_requests, payload)
            elif method == "Network.responseReceived":
                response = params.get("response", {}) or {}
                status = response.get("status")
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": current_tab_id,
                    "request_id": str(params.get("requestId", "") or ""),
                    "event": "response",
                    "method": "",
                    "url": str(response.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": False,
                    "status": int(status) if isinstance(status, (int, float)) else None,
                    "ok": bool(response.get("status", 0) and int(response.get("status", 0)) < 400) if isinstance(response.get("status"), (int, float)) else None,
                    "failure": "",
                }
                self._append_limited(self._network_requests, payload)
            elif method == "Network.loadingFailed":
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": current_tab_id,
                    "request_id": str(params.get("requestId", "") or ""),
                    "event": "requestfailed",
                    "method": "",
                    "url": str(params.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": False,
                    "status": None,
                    "ok": False,
                    "failure": str(params.get("errorText", "") or ""),
                }
                self._append_limited(self._network_requests, payload)

    def get_summary(self) -> BrowserSessionSummary:
        try:
            current_url = str(getattr(self.driver, "current_url", "") or "")
            title = str(getattr(self.driver, "title", "") or "")
            return BrowserSessionSummary(current_url=current_url, title=title, alive=True)
        except Exception:
            return BrowserSessionSummary(alive=False)

    def get_capabilities(self) -> Dict:
        return {
            "engine_name": "selenium_uc",
            "supports_snapshot": False,
            "supports_snapshot_refs": False,
            "supports_target_actions": True,
            "supports_selector_actions": True,
            "supports_highlight": False,
            "supports_coordinates": True,
            "supports_post_action_context": False,
            "supports_tabs": True,
            "supports_console_messages": True,
            "supports_page_errors": True,
            "supports_network_requests": True,
        }

    def list_tabs(self) -> Dict:
        tabs = [self._tab_entry(handle, index) for index, handle in enumerate(self._list_window_handles())]
        return {**self.get_current_url(), "active_tab_id": self._current_tab_id(), "count": len(tabs), "tabs": tabs}

    def open_tab(
        self,
        url: str = "",
        activate: bool = True,
        wait_for_ready: bool = True,
        timeout_seconds: int = 20,
    ) -> Dict:
        original = self._current_tab_id()
        self.driver.execute_script("window.open(arguments[0] || 'about:blank', '_blank');", str(url or "").strip() or "about:blank")
        handles = self._list_window_handles()
        new_handle = handles[-1]
        if activate:
            self._activate_handle(new_handle)
        elif original:
            self._activate_handle(original)
        if activate and wait_for_ready:
            wait_until_ready(self.driver, int(timeout_seconds))
        return {
            **self.get_current_url(tab_id=new_handle if activate else original),
            "opened": True,
            "activated": bool(activate),
            "tab": self._tab_entry(new_handle, handles.index(new_handle)),
            "tabs": self.list_tabs().get("tabs", []),
        }

    def activate_tab(
        self,
        tab_id: str = "",
        index: int = -1,
        title_contains: str = "",
        url_contains: str = "",
    ) -> Dict:
        handle = self._resolve_handle(tab_id=tab_id, index=index, title_contains=title_contains, url_contains=url_contains)
        self._activate_handle(handle)
        return {
            **self.get_current_url(tab_id=handle),
            "activated": True,
            "tab": self._tab_entry(handle, self._list_window_handles().index(handle)),
            "tabs": self.list_tabs().get("tabs", []),
        }

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        handle = self._resolve_handle(tab_id=tab_id, index=index)
        tabs_before = self._list_window_handles()
        closed_tab = self._tab_entry(handle, tabs_before.index(handle))
        if len(tabs_before) <= 1:
            self.driver.execute_script("window.open('about:blank', '_blank');")
            tabs_before = self._list_window_handles()
        self._activate_handle(handle)
        self.driver.close()
        remaining = self._list_window_handles()
        if remaining:
            self._activate_handle(remaining[0])
        return {**self.get_current_url(), "closed": True, "closed_tab": closed_tab, "tabs": self.list_tabs().get("tabs", [])}

    def _error_payload(
        self,
        action_name: str,
        error: Exception,
        *,
        target: str = "",
        selector: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 8,
    ) -> Dict:
        payload = {
            **self.get_current_url(),
            "ok": False,
            "action_name": action_name,
            "error": str(error),
            "error_type": type(error).__name__,
            "interaction_context": self.get_interaction_context().get("interaction_context", {}),
        }
        diagnose_target = str(target or selector or "").strip()
        if diagnose_target:
            try:
                payload["diagnosis"] = self.diagnose_target(
                    target=diagnose_target,
                    by=by,
                    text_filter=text_filter or diagnose_target,
                    limit=limit,
                )
            except Exception as diag_exc:
                payload["diagnosis_error"] = str(diag_exc)
        elif text_filter:
            try:
                payload["page_candidates"] = self.list_candidates(
                    text_filter=text_filter,
                    limit=limit,
                    include_boxes=True,
                ).get("candidates", [])
            except Exception as candidate_exc:
                payload["page_candidates_error"] = str(candidate_exc)
        return payload

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        self.driver.get(url)
        if wait_for_ready:
            wait_until_ready(self.driver, int(timeout_seconds))
        return self.get_current_url(tab_id=handle)

    def get_current_url(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id) if tab_id else self._current_tab_id()
        if handle and handle != self._current_tab_id():
            original = self._current_tab_id()
            try:
                self._activate_handle(handle)
                return {"tab_id": handle, "url": self.driver.current_url, "title": self.driver.title}
            finally:
                if original and original != handle:
                    try:
                        self._activate_handle(original)
                    except Exception:
                        pass
        return {"tab_id": handle, "url": self.driver.current_url, "title": self.driver.title}

    def get_page_text(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        body = self.driver.find_element(By.TAG_NAME, "body")
        return {**self.get_current_url(tab_id=handle), "text": (body.text or "").strip()}

    def get_page_html(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        return {**self.get_current_url(tab_id=handle), "html": self.driver.page_source}

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        locator_by = selector_kind_to_by(by)
        elements = self.driver.find_elements(locator_by, selector)
        inspected = []
        for element in elements[: max(1, int(limit))]:
            try:
                inspected.append(describe_element(self.driver, element))
            except WebDriverException:
                continue
        return {**self.get_current_url(tab_id=handle), "count": len(elements), "elements": inspected}

    def get_active_element(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        element = self.driver.switch_to.active_element
        return {**self.get_current_url(tab_id=handle), "element": describe_element(self.driver, element)}

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        return {
            **self.get_current_url(tab_id=handle),
            "interaction_context": {
                "action_name": "inspect",
                "page": self.get_current_url(tab_id=handle),
                "tabs": self.list_tabs().get("tabs", []),
                "active_tab_id": handle,
                "active_element": self.get_active_element(tab_id=handle).get("element", {}),
                "modal_state": {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []},
                "snapshot": {"error": "Structured snapshot context is currently supported only by the patchright engine."},
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
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        target_text = str(target or "").strip()
        response = {
            **self.get_current_url(tab_id=handle),
            "target": target_text,
            "by": str(by or "css"),
            "depth": int(depth) if depth is not None else None,
            "boxes": bool(boxes),
            "filename": str(filename or "").strip(),
            "snapshot": {
                "unsupported": True,
                "engine_name": "selenium_uc",
                "message": "Structured browser_snapshot and snapshot refs are currently supported only by the patchright engine.",
                "recommended_tools": [
                    "inspect_elements",
                    "browser_list_candidates",
                    "browser_describe_target",
                    "screenshot",
                ],
            },
        }
        if target_text and not SNAPSHOT_REF_PATTERN.match(target_text):
            try:
                response["target_details"] = self.describe_target(target_text, by=by, include_box=bool(boxes))
            except Exception as exc:
                response["target_details_error"] = str(exc)
        return response

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        if str(target or "").strip():
            raise NotImplementedError("Region candidate enumeration is currently supported only by the patchright engine.")
        lowered = str(text_filter or "").strip().lower()
        elements = self.driver.find_elements(By.CSS_SELECTOR, "a,button,input,textarea,select,[role],[aria-label]")
        candidates = []
        for element in elements:
            if len(candidates) >= max(1, int(limit)):
                break
            try:
                details = describe_element(self.driver, element)
                merged_text = " ".join(
                    [
                        str(details.get("text", "") or ""),
                        str(details.get("aria_label", "") or ""),
                        str(details.get("role", "") or ""),
                    ]
                ).strip()
                if lowered and lowered not in merged_text.lower():
                    continue
                entry = {
                    "ref": "",
                    "visible": element.is_displayed(),
                    "enabled": element.is_enabled(),
                    **details,
                }
                if include_boxes:
                    entry["box"] = element_box(self.driver, element)
                candidates.append(entry)
            except WebDriverException:
                continue
        return {
            **self.get_current_url(tab_id=handle),
            "target": "",
            "text_filter": str(text_filter or ""),
            "count": len(candidates),
            "candidates": candidates,
        }

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        try:
            locator = (selector_kind_to_by(by), selector)
            element = wait_for_element(self.driver, locator, int(timeout_seconds), condition)
            return {
                **self.get_current_url(),
                "found": True,
                "tag_name": element.tag_name,
                "text": (element.text or "").strip(),
            }
        except Exception as exc:
            return self._error_payload("wait_for", exc, selector=selector, by=by, text_filter=selector)

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        try:
            locator = (selector_kind_to_by(by), selector)
            robust_click(self.driver, locator, int(timeout_seconds))
            return {**self.get_current_url(), "clicked": True}
        except Exception as exc:
            return self._error_payload("click", exc, selector=selector, by=by, text_filter=selector)

    def click_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        double_click: bool = False,
    ) -> Dict:
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()):
            return self._error_payload(
                "click_target",
                NotImplementedError("Snapshot ref targets are currently supported only by the patchright engine."),
                target=target,
                by=by,
                text_filter=target,
            )
        try:
            if double_click:
                locator = (selector_kind_to_by(by), target)
                element_handle = wait_for_element(self.driver, locator, int(timeout_seconds), "present")
                ActionChains(self.driver).move_to_element(element_handle).double_click().perform()
                return {**self.get_current_url(), "clicked": True, "double_click": True, "target": str(target or "").strip()}
            return {**self.get_current_url(), **self.click(target, by=by, timeout_seconds=timeout_seconds), "target": str(target or "").strip()}
        except Exception as exc:
            return self._error_payload("click_target", exc, target=target, by=by, text_filter=target)

    def type_text(
        self,
        selector: str,
        text: str,
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            locator = (selector_kind_to_by(by), selector)
            robust_type_text(self.driver, locator, text, bool(clear_first), bool(submit), int(timeout_seconds))
            element = wait_for_element(self.driver, locator, int(timeout_seconds), "present")
            actual_value = read_element_input_value(self.driver, element)
            return {
                **self.get_current_url(),
                "typed": True,
                "submitted": bool(submit),
                "actual_value": actual_value,
                "value_matches": input_value_matches_expected(actual_value, text),
            }
        except Exception as exc:
            return self._error_payload("type_text", exc, selector=selector, by=by, text_filter=selector)

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
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()):
            return self._error_payload(
                "type_target",
                NotImplementedError("Snapshot ref targets are currently supported only by the patchright engine."),
                target=target,
                by=by,
                text_filter=target,
            )
        try:
            result = self.type_text(
                target,
                text,
                by=by,
                clear_first=clear_first,
                submit=submit,
                timeout_seconds=timeout_seconds,
            )
            return {**self.get_current_url(), **result, "target": str(target or "").strip()}
        except Exception as exc:
            return self._error_payload("type_target", exc, target=target, by=by, text_filter=target)

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
        typed = self.type_target(
            target=target,
            text=text,
            element=element,
            by=by,
            clear_first=clear_first,
            submit=submit,
            timeout_seconds=timeout_seconds,
        )
        if not typed.get("typed"):
            return typed
        verified = self.verify_target_value(target=target, expected_value=text, element=element, by=by)
        return {
            **self.get_current_url(),
            "typed": True,
            "verified": bool(verified.get("verified")),
            "submitted": bool(submit),
            "target": str(target or "").strip(),
            "type_result": typed,
            "verify_result": verified,
        }

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            target = self.driver.switch_to.active_element
            if str(selector or "").strip():
                locator = (selector_kind_to_by(by), selector)
                target = wait_for_element(self.driver, locator, int(timeout_seconds), "present")
                try:
                    scroll_into_view(self.driver, target)
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
            return {**self.get_current_url(), "pressed": True, "key": key, "count": repeat}
        except Exception as exc:
            return self._error_payload("press_key", exc, selector=selector, by=by, text_filter=selector or key)

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        result = self.driver.execute_script(script)
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {**self.get_current_url(tab_id=handle), "result": serialized}

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        self._poll_debug_logs()
        normalized_tab_id = str(tab_id or "").strip()
        normalized_level = str(level or "").strip().lower()
        messages = list(self._console_messages)
        if normalized_tab_id:
            messages = [item for item in messages if str(item.get("tab_id", "") or "") == normalized_tab_id]
        if normalized_level:
            messages = [item for item in messages if str(item.get("type", "") or "").lower() == normalized_level]
        messages = messages[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "level": normalized_level,
            "count": len(messages),
            "messages": messages,
        }

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        self._poll_debug_logs()
        normalized_tab_id = str(tab_id or "").strip()
        errors = list(self._page_errors)
        if normalized_tab_id:
            errors = [item for item in errors if str(item.get("tab_id", "") or "") == normalized_tab_id]
        errors = errors[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "count": len(errors),
            "errors": errors,
        }

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        self._poll_debug_logs()
        normalized_tab_id = str(tab_id or "").strip()
        requests = list(self._network_requests)
        if normalized_tab_id:
            requests = [item for item in requests if str(item.get("tab_id", "") or "") == normalized_tab_id]
        if bool(failed_only):
            requests = [item for item in requests if item.get("event") == "requestfailed" or item.get("ok") is False]
        requests = requests[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "failed_only": bool(failed_only),
            "count": len(requests),
            "requests": requests,
        }

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        normalized_tab_id = str(tab_id or "").strip()
        if normalized_tab_id:
            self._console_messages = [item for item in self._console_messages if str(item.get("tab_id", "") or "") != normalized_tab_id]
            self._page_errors = [item for item in self._page_errors if str(item.get("tab_id", "") or "") != normalized_tab_id]
            self._network_requests = [item for item in self._network_requests if str(item.get("tab_id", "") or "") != normalized_tab_id]
        else:
            self._console_messages.clear()
            self._page_errors.clear()
            self._network_requests.clear()
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "cleared": True,
            "tab_id": normalized_tab_id,
        }

    def diagnose_page(self, tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        console_messages = self.get_console_messages(tab_id=handle, limit=20).get("messages", [])
        page_errors = self.get_page_errors(tab_id=handle, limit=20).get("errors", [])
        failed_requests = self.get_network_requests(tab_id=handle, limit=30, failed_only=True).get("requests", [])
        all_requests = self.get_network_requests(tab_id=handle, limit=30, failed_only=False).get("requests", [])
        bad_responses = [
            item
            for item in all_requests
            if item.get("event") == "response" and isinstance(item.get("status"), int) and int(item.get("status")) >= 400
        ]
        return {
            **self.get_current_url(tab_id=handle),
            "tab_id": handle,
            "diagnosis": {
                "interaction_context": self.get_interaction_context(tab_id=handle).get("interaction_context", {}),
                "console_messages": console_messages,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "bad_responses": bad_responses[-20:],
            },
        }

    def verify_text(self, text: str) -> Dict:
        try:
            body_text = (self.driver.find_element(By.TAG_NAME, "body").text or "").strip()
            if str(text) not in body_text:
                raise ValueError(f'Text not visible: "{text}"')
            return {**self.get_current_url(), "verified": True, "text": str(text)}
        except Exception as exc:
            return self._error_payload("verify_text", exc, text_filter=str(text))

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        try:
            script = """
        const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
        const isVisible = el => {
          const style = window.getComputedStyle(el);
          if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };
        const selectors = ['dialog[open]', '[role="dialog"]', 'details-dialog', '.Overlay--modal', '.Popover-message'];
        const result = [];
        for (const selector of selectors) {
          for (const el of document.querySelectorAll(selector)) {
            if (!isVisible(el)) continue;
            result.push({
              tag_name: (el.tagName || '').toLowerCase(),
              role: normalize(el.getAttribute('role') || ''),
              aria_label: normalize(el.getAttribute('aria-label') || ''),
              text: normalize(el.innerText || el.textContent || '')
            });
          }
        }
        return result;
        """
            dialogs = self.driver.execute_script(script) or []
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
            return self._error_payload("verify_dialog", exc, text_filter=str(accessible_name or text or "dialog"))

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        try:
            active = self.driver.switch_to.active_element
            if str(target or "").strip():
                locator = (selector_kind_to_by(by), target)
                expected = wait_for_element(self.driver, locator, 10, "present")
                same = self.driver.execute_script("return arguments[0] === document.activeElement;", expected)
                if not same:
                    raise ValueError(f'Active element did not match target: "{target}"')
                return {**self.get_current_url(), "verified": True, "target": str(target), "element": describe_element(self.driver, expected)}
            return {**self.get_current_url(), "verified": True, "element": describe_element(self.driver, active)}
        except Exception as exc:
            return self._error_payload("verify_active_element", exc, target=target, by=by, text_filter=target or "active element")

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        try:
            if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()):
                raise NotImplementedError("Snapshot ref targets are currently supported only by the patchright engine.")
            locator = (selector_kind_to_by(by), target)
            element_handle = wait_for_element(self.driver, locator, 10, "present")
            actual_value = element_handle.get_attribute("value") or ""
            if str(actual_value) != str(expected_value):
                raise ValueError(f'Value mismatch for target "{target}": expected "{expected_value}", got "{actual_value}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "expected_value": str(expected_value),
                "actual_value": str(actual_value),
            }
        except Exception as exc:
            return self._error_payload("verify_target_value", exc, target=target, by=by, text_filter=expected_value)

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        try:
            if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()):
                raise NotImplementedError("Snapshot ref targets are currently supported only by the patchright engine.")
            locator = (selector_kind_to_by(by), target)
            element_handle = wait_for_element(self.driver, locator, 10, "present")
            if not element_handle.is_displayed():
                raise ValueError(f'Target not visible: "{target}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "visible": True,
                "tag_name": element_handle.tag_name,
                "text": (element_handle.text or "").strip(),
            }
        except Exception as exc:
            return self._error_payload("verify_target_visible", exc, target=target, by=by, text_filter=target)

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()):
            raise NotImplementedError("Snapshot ref targets are currently supported only by the patchright engine.")
        locator = (selector_kind_to_by(by), target)
        element_handle = wait_for_element(self.driver, locator, 10, "present")
        result = {
            **self.get_current_url(),
            "target": str(target or "").strip(),
            "visible": element_handle.is_displayed(),
            "enabled": element_handle.is_enabled(),
            **describe_element(self.driver, element_handle),
        }
        if include_box:
            result["box"] = element_box(self.driver, element_handle)
        return result

    def diagnose_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 10,
    ) -> Dict:
        resolved_target = str(target or "").strip()
        diagnosis = {
            **self.get_current_url(),
            "target": resolved_target,
            "element": str(element or "").strip(),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "is_snapshot_ref": bool(SNAPSHOT_REF_PATTERN.match(resolved_target)),
        }
        if SNAPSHOT_REF_PATTERN.match(resolved_target):
            diagnosis.update(
                {
                    "status": "unsupported_snapshot_ref",
                    "message": "Snapshot ref diagnostics are currently supported only by the patchright engine.",
                    "interaction_context": self.get_interaction_context().get("interaction_context", {}),
                }
            )
            return diagnosis
        try:
            diagnosis["status"] = "resolved"
            diagnosis["message"] = "target resolved successfully"
            diagnosis["details"] = self.describe_target(resolved_target, by=by, include_box=True)
        except Exception as exc:
            diagnosis["status"] = "resolve_failed"
            diagnosis["message"] = str(exc)
            try:
                diagnosis["selector_matches"] = self.inspect_elements(
                    selector=resolved_target,
                    by=by,
                    limit=max(1, int(limit)),
                )
            except Exception as inspect_exc:
                diagnosis["selector_matches_error"] = str(inspect_exc)
            try:
                diagnosis["page_candidates"] = self.list_candidates(
                    text_filter=text_filter or resolved_target,
                    limit=max(1, int(limit)),
                    include_boxes=True,
                ).get("candidates", [])
            except Exception as candidate_exc:
                diagnosis["page_candidates_error"] = str(candidate_exc)
        diagnosis["interaction_context"] = self.get_interaction_context().get("interaction_context", {})
        return diagnosis

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        try:
            xpath = (
                "//*[@role=%s and normalize-space(@aria-label)=%s]"
                % (json.dumps(str(role)), json.dumps(str(accessible_name)))
            )
            elements = self.driver.find_elements(By.XPATH, xpath)
            visible_elements = [el for el in elements if el.is_displayed()]
            if not visible_elements:
                raise ValueError(f'Element not visible: role="{role}" accessible_name="{accessible_name}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "role": str(role),
                "accessible_name": str(accessible_name),
                "count": len(visible_elements),
                "box": element_box(self.driver, visible_elements[0]),
            }
        except Exception as exc:
            return self._error_payload("verify_element", exc, text_filter=str(accessible_name or role))

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        raise NotImplementedError("Persistent highlight is currently supported only by the patchright engine.")

    def clear_highlights(self) -> Dict:
        raise NotImplementedError("Persistent highlight is currently supported only by the patchright engine.")

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        viewport = get_viewport_metrics(self.driver)
        ensure_viewport_point(viewport, float(x), float(y))
        try:
            dispatch_cdp_mouse(self.driver, "mouseMoved", float(x), float(y))
        except Exception:
            mouse = PointerInput("mouse", "default")
            actions = ActionBuilder(self.driver, mouse=mouse)
            actions.pointer_action.move_to_location(int(float(x)), int(float(y)))
            actions.perform()
        return {**self.get_current_url(), "moved": True, "x": float(x), "y": float(y), "viewport": viewport}

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        clicks = max(1, int(click_count))
        button_name = str(button or "left").strip().lower()
        viewport = get_viewport_metrics(self.driver)
        ensure_viewport_point(viewport, float(x), float(y))
        try:
            dispatch_cdp_mouse(self.driver, "mouseMoved", float(x), float(y), button=button_name, click_count=clicks)
            for _ in range(clicks):
                dispatch_cdp_mouse(self.driver, "mousePressed", float(x), float(y), button=button_name, click_count=clicks)
                if int(delay_ms) > 0:
                    time.sleep(max(0, int(delay_ms)) / 1000.0)
                dispatch_cdp_mouse(self.driver, "mouseReleased", float(x), float(y), button=button_name, click_count=clicks)
        except Exception:
            mouse = PointerInput("mouse", "default")
            actions = ActionBuilder(self.driver, mouse=mouse)
            actions.pointer_action.move_to_location(int(float(x)), int(float(y)))
            if button_name == "right":
                actions.pointer_action.context_click()
            else:
                for _ in range(clicks):
                    actions.pointer_action.click()
            actions.perform()
        return {
            **self.get_current_url(),
            "clicked": True,
            "x": float(x),
            "y": float(y),
            "button": button_name,
            "click_count": clicks,
            "viewport": viewport,
        }

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        viewport = get_viewport_metrics(self.driver)
        ensure_viewport_point(viewport, float(start_x), float(start_y))
        ensure_viewport_point(viewport, float(end_x), float(end_y))
        try:
            dispatch_cdp_mouse(self.driver, "mouseMoved", float(start_x), float(start_y))
            dispatch_cdp_mouse(self.driver, "mousePressed", float(start_x), float(start_y))
            dispatch_cdp_mouse(self.driver, "mouseMoved", float(end_x), float(end_y))
            dispatch_cdp_mouse(self.driver, "mouseReleased", float(end_x), float(end_y))
        except Exception:
            mouse = PointerInput("mouse", "default")
            actions = ActionBuilder(self.driver, mouse=mouse)
            actions.pointer_action.move_to_location(int(float(start_x)), int(float(start_y)))
            actions.pointer_action.pointer_down()
            actions.pointer_action.move_to_location(int(float(end_x)), int(float(end_y)))
            actions.pointer_action.release()
            actions.perform()
        return {
            **self.get_current_url(),
            "dragged": True,
            "start_x": float(start_x),
            "start_y": float(start_y),
            "end_x": float(end_x),
            "end_y": float(end_y),
            "viewport": viewport,
        }

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        handle = self._resolve_handle(tab_id=tab_id)
        self._activate_handle(handle)
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "chromium-advanced-selenium-session.png")
        self.driver.save_screenshot(output_path)
        return {**self.get_current_url(tab_id=handle), "path": output_path}

    def close(self) -> None:
        self.driver.quit()


class SeleniumUCEngine(BrowserEngine):
    engine_name = "selenium_uc"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        from chromium_advanced.chromium_profile_lib import create_driver_for_profile

        driver = create_driver_for_profile(config, profile_name)
        return SeleniumBrowserSession(driver)
