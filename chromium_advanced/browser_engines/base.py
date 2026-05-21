from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Protocol


@dataclass
class BrowserSessionSummary:
    current_url: str = ""
    title: str = ""
    alive: bool = True


class BrowserSession(Protocol):
    def get_summary(self) -> BrowserSessionSummary:
        ...

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        ...

    def get_current_url(self) -> Dict:
        ...

    def get_page_text(self) -> Dict:
        ...

    def get_page_html(self) -> Dict:
        ...

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10) -> Dict:
        ...

    def get_active_element(self) -> Dict:
        ...

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        ...

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        ...

    def type_text(
        self,
        selector: str,
        text: str,
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        ...

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        ...

    def run_script(self, script: str) -> Dict:
        ...

    def screenshot(self, filename: str = "") -> Dict:
        ...

    def close(self) -> None:
        ...


class BrowserEngine(Protocol):
    engine_name: str

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        ...
