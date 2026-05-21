from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Dict

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

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")


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


class SeleniumBrowserSession(BrowserSession):
    def __init__(self, driver):
        self.driver = driver

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
        }

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

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        self.driver.get(url)
        if wait_for_ready:
            wait_until_ready(self.driver, int(timeout_seconds))
        return self.get_current_url()

    def get_current_url(self) -> Dict:
        return {"url": self.driver.current_url, "title": self.driver.title}

    def get_page_text(self) -> Dict:
        body = self.driver.find_element(By.TAG_NAME, "body")
        return {**self.get_current_url(), "text": (body.text or "").strip()}

    def get_page_html(self) -> Dict:
        return {**self.get_current_url(), "html": self.driver.page_source}

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10) -> Dict:
        locator_by = selector_kind_to_by(by)
        elements = self.driver.find_elements(locator_by, selector)
        inspected = []
        for element in elements[: max(1, int(limit))]:
            try:
                inspected.append(describe_element(self.driver, element))
            except WebDriverException:
                continue
        return {**self.get_current_url(), "count": len(elements), "elements": inspected}

    def get_active_element(self) -> Dict:
        element = self.driver.switch_to.active_element
        return {**self.get_current_url(), "element": describe_element(self.driver, element)}

    def get_interaction_context(self) -> Dict:
        return {
            **self.get_current_url(),
            "interaction_context": {
                "action_name": "inspect",
                "page": self.get_current_url(),
                "tabs": [],
                "active_element": self.get_active_element().get("element", {}),
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
    ) -> Dict:
        raise NotImplementedError("Structured browser_snapshot is currently supported only by the patchright engine.")

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
    ) -> Dict:
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
        return {**self.get_current_url(), "target": "", "text_filter": str(text_filter or ""), "count": len(candidates), "candidates": candidates}

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
            return {**self.get_current_url(), "typed": True, "submitted": bool(submit)}
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
            return {
                **self.get_current_url(),
                **self.type_text(
                    target,
                    text,
                    by=by,
                    clear_first=clear_first,
                    submit=submit,
                    timeout_seconds=timeout_seconds,
                ),
                "target": str(target or "").strip(),
            }
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

    def run_script(self, script: str) -> Dict:
        result = self.driver.execute_script(script)
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {**self.get_current_url(), "result": serialized}

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
        ActionChains(self.driver).move_by_offset(float(x), float(y)).perform()
        return {**self.get_current_url(), "moved": True, "x": float(x), "y": float(y)}

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        actions = ActionChains(self.driver).move_by_offset(float(x), float(y))
        clicks = max(1, int(click_count))
        button_name = str(button or "left").strip().lower()
        for _ in range(clicks):
            if button_name == "right":
                actions.context_click()
            else:
                actions.click()
        actions.perform()
        return {
            **self.get_current_url(),
            "clicked": True,
            "x": float(x),
            "y": float(y),
            "button": button_name,
            "click_count": clicks,
        }

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        ActionChains(self.driver).move_by_offset(float(start_x), float(start_y)).click_and_hold().move_by_offset(
            float(end_x) - float(start_x),
            float(end_y) - float(start_y),
        ).release().perform()
        return {
            **self.get_current_url(),
            "dragged": True,
            "start_x": float(start_x),
            "start_y": float(start_y),
            "end_x": float(end_x),
            "end_y": float(end_y),
        }

    def screenshot(self, filename: str = "") -> Dict:
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "chromium-advanced-selenium-session.png")
        self.driver.save_screenshot(output_path)
        return {**self.get_current_url(), "path": output_path}

    def close(self) -> None:
        self.driver.quit()


class SeleniumUCEngine(BrowserEngine):
    engine_name = "selenium_uc"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        from chromium_advanced.chromium_profile_lib import create_driver_for_profile

        driver = create_driver_for_profile(config, profile_name)
        return SeleniumBrowserSession(driver)
