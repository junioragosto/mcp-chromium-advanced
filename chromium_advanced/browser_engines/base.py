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

    def get_capabilities(self) -> Dict:
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

    def get_interaction_context(self) -> Dict:
        ...

    def snapshot(
        self,
        target: str = "",
        by: str = "css",
        depth: int | None = None,
        boxes: bool = False,
        filename: str = "",
    ) -> Dict:
        ...

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
    ) -> Dict:
        ...

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        ...

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        ...

    def click_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        double_click: bool = False,
    ) -> Dict:
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
        ...

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

    def verify_text(self, text: str) -> Dict:
        ...

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        ...

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        ...

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        ...

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        ...

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        ...

    def diagnose_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 10,
    ) -> Dict:
        ...

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        ...

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        ...

    def clear_highlights(self) -> Dict:
        ...

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        ...

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        ...

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        ...

    def screenshot(self, filename: str = "") -> Dict:
        ...

    def close(self) -> None:
        ...


class BrowserEngine(Protocol):
    engine_name: str

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        ...
