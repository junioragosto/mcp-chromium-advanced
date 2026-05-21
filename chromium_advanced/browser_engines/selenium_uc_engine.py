from __future__ import annotations

import json
import os
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

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        locator = (selector_kind_to_by(by), selector)
        element = wait_for_element(self.driver, locator, int(timeout_seconds), condition)
        return {
            **self.get_current_url(),
            "found": True,
            "tag_name": element.tag_name,
            "text": (element.text or "").strip(),
        }

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        locator = (selector_kind_to_by(by), selector)
        robust_click(self.driver, locator, int(timeout_seconds))
        return {**self.get_current_url(), "clicked": True}

    def type_text(
        self,
        selector: str,
        text: str,
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        locator = (selector_kind_to_by(by), selector)
        robust_type_text(self.driver, locator, text, bool(clear_first), bool(submit), int(timeout_seconds))
        return {**self.get_current_url(), "typed": True, "submitted": bool(submit)}

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
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

    def run_script(self, script: str) -> Dict:
        result = self.driver.execute_script(script)
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {**self.get_current_url(), "result": serialized}

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
