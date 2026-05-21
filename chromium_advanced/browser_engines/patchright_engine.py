from __future__ import annotations

import json
import os
import tempfile
from typing import Dict

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary


def _load_patchright():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Patchright is not installed. Install it with: pip install patchright") from exc
    return sync_playwright


def _selector_to_locator(page, selector: str, by: str):
    normalized = str(by or "css").strip().lower()
    if normalized == "css":
        return page.locator(selector)
    if normalized == "xpath":
        return page.locator(f"xpath={selector}")
    if normalized == "id":
        return page.locator(f"#{selector}")
    if normalized == "name":
        return page.locator(f"[name={json.dumps(selector)}]")
    if normalized == "tag":
        return page.locator(selector)
    if normalized == "class":
        return page.locator(f".{selector}")
    if normalized == "link_text":
        return page.locator(f"a:has-text({json.dumps(selector)})")
    if normalized == "partial_link_text":
        return page.locator(f"a:has-text({json.dumps(selector)})")
    raise ValueError(f"unsupported selector type: {by}")


def _describe_locator(locator) -> Dict:
    return locator.evaluate(
        """
        el => ({
          tag_name: (el.tagName || '').toLowerCase(),
          text: (el.innerText || el.textContent || '').trim(),
          id: el.id || '',
          name: el.getAttribute('name') || '',
          class: el.getAttribute('class') || '',
          aria_label: el.getAttribute('aria-label') || '',
          role: el.getAttribute('role') || '',
          value: 'value' in el ? (el.value || '') : '',
          href: el.getAttribute('href') || '',
          outer_html: el.outerHTML || ''
        })
        """
    )


class PatchrightBrowserSession(BrowserSession):
    def __init__(self, playwright_ctx, browser_context, page):
        self._playwright_ctx = playwright_ctx
        self.context = browser_context
        self.page = page

    def get_summary(self) -> BrowserSessionSummary:
        try:
            return BrowserSessionSummary(current_url=self.page.url or "", title=self.page.title() or "", alive=not self.page.is_closed())
        except Exception:
            return BrowserSessionSummary(alive=False)

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        wait_until = "load" if wait_for_ready else "domcontentloaded"
        self.page.goto(url, wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
        return self.get_current_url()

    def get_current_url(self) -> Dict:
        return {"url": self.page.url, "title": self.page.title()}

    def get_page_text(self) -> Dict:
        text = self.page.locator("body").inner_text(timeout=15000).strip()
        return {**self.get_current_url(), "text": text}

    def get_page_html(self) -> Dict:
        return {**self.get_current_url(), "html": self.page.content()}

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10) -> Dict:
        locator = _selector_to_locator(self.page, selector, by)
        count = locator.count()
        elements = []
        for index in range(min(max(1, int(limit)), count)):
            try:
                elements.append(_describe_locator(locator.nth(index)))
            except Exception:
                continue
        return {**self.get_current_url(), "count": count, "elements": elements}

    def get_active_element(self) -> Dict:
        element = self.page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return {};
              return {
                tag_name: (el.tagName || '').toLowerCase(),
                text: (el.innerText || el.textContent || '').trim(),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                class: el.getAttribute('class') || '',
                aria_label: el.getAttribute('aria-label') || '',
                role: el.getAttribute('role') || '',
                value: 'value' in el ? (el.value || '') : '',
                href: el.getAttribute('href') || '',
                outer_html: el.outerHTML || ''
              };
            }
            """
        )
        return {**self.get_current_url(), "element": element}

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        locator = _selector_to_locator(self.page, selector, by).first
        target_state = {"present": "attached", "visible": "visible", "clickable": "visible"}.get(condition, "visible")
        locator.wait_for(state=target_state, timeout=int(timeout_seconds) * 1000)
        item = _describe_locator(locator)
        return {**self.get_current_url(), "found": True, "tag_name": item.get("tag_name", ""), "text": item.get("text", "")}

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        locator = _selector_to_locator(self.page, selector, by).first
        locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
        locator.click(timeout=int(timeout_seconds) * 1000)
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
        locator = _selector_to_locator(self.page, selector, by).first
        locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
        if clear_first:
            locator.fill("", timeout=int(timeout_seconds) * 1000)
        locator.type(text, timeout=int(timeout_seconds) * 1000)
        if submit:
            locator.press("Enter", timeout=int(timeout_seconds) * 1000)
        return {**self.get_current_url(), "typed": True, "submitted": bool(submit)}

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        repeat = max(1, int(count))
        if str(selector or "").strip():
            locator = _selector_to_locator(self.page, selector, by).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            locator.focus(timeout=int(timeout_seconds) * 1000)
        for _ in range(repeat):
            self.page.keyboard.press(str(key), timeout=int(timeout_seconds) * 1000)
        return {**self.get_current_url(), "pressed": True, "key": key, "count": repeat}

    def run_script(self, script: str) -> Dict:
        result = self.page.evaluate(f"() => {{ {script} }}")
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {**self.get_current_url(), "result": serialized}

    def screenshot(self, filename: str = "") -> Dict:
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "chromium-advanced-patchright-session.png")
        self.page.screenshot(path=output_path, full_page=True)
        return {**self.get_current_url(), "path": output_path}

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._playwright_ctx.stop()


class PatchrightEngine(BrowserEngine):
    engine_name = "patchright"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        from chromium_advanced.chromium_profile_lib import resolve_chromium_binary

        sync_playwright = _load_patchright()
        paths = config.get("paths", {})
        launch_settings = config.get("launch", {})
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        user_data_root = os.path.abspath(os.path.expanduser(paths.get("user_data_root", "")))
        if not chromium_binary or not os.path.exists(chromium_binary):
            raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
        if not os.path.isdir(user_data_root):
            raise FileNotFoundError(f"UserData root not found: {user_data_root}")

        # Keep Patchright on the smallest validated argument set first.
        # Its Chromium startup behavior differs from Selenium/uc, so not every
        # shared launch flag should be forwarded blindly.
        args = [f"--profile-directory={profile_name}"]
        if launch_settings.get("start_maximized", True):
            args.append("--start-maximized")
        window_size = str(launch_settings.get("window_size", "")).strip()
        if window_size:
            args.append(f"--window-size={window_size}")
        if launch_settings.get("no_first_run", True):
            args.append("--no-first-run")
        if launch_settings.get("no_default_browser_check", True):
            args.append("--no-default-browser-check")
        extra_args = launch_settings.get("extra_args", [])
        if isinstance(extra_args, list):
            args.extend([item for item in extra_args if item])

        playwright_ctx = sync_playwright().start()
        browser_context = playwright_ctx.chromium.launch_persistent_context(
            user_data_dir=user_data_root,
            executable_path=chromium_binary,
            headless=False,
            args=args,
            no_viewport=True,
        )
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        extensions_page = bool(launch_settings.get("open_extensions_page", False))
        check_url = str(launch_settings.get("check_url", "")).strip()
        if extensions_page:
            page.goto("chrome://extensions", wait_until="domcontentloaded", timeout=45000)
        if check_url:
            page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
        return PatchrightBrowserSession(playwright_ctx, browser_context, page)
