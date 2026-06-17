from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol


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

    def list_tabs(self) -> Dict:
        ...

    def open_tab(
        self,
        url: str = "",
        activate: bool = True,
        wait_for_ready: bool = True,
        timeout_seconds: int = 20,
    ) -> Dict:
        ...

    def activate_tab(
        self,
        tab_id: str = "",
        index: int = -1,
        title_contains: str = "",
        url_contains: str = "",
    ) -> Dict:
        ...

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        ...

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        ...

    def get_current_url(self, tab_id: str = "") -> Dict:
        ...

    def get_page_text(self, tab_id: str = "") -> Dict:
        ...

    def get_page_html(self, tab_id: str = "") -> Dict:
        ...

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        ...

    def get_active_element(self, tab_id: str = "") -> Dict:
        ...

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        ...

    def snapshot(
        self,
        target: str = "",
        by: str = "css",
        depth: int | None = None,
        boxes: bool = False,
        filename: str = "",
        tab_id: str = "",
    ) -> Dict:
        ...

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> Dict:
        ...

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        ...

    def wait_for_text(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        ...

    def wait_for_text_gone(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        ...

    def wait_for_text_change(
        self,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        tab_id: str = "",
    ) -> Dict:
        ...

    def wait_for_page_stable(
        self,
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        ...

    def wait_for_timeout(self, timeout_ms: int = 0, tab_id: str = "") -> Dict:
        ...

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        ...

    def hover(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
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

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        ...

    def watch_page_state(
        self,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        ...

    def watch_target_state(
        self,
        target: str,
        text: str = "",
        previous_text: str = "",
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        ...

    def select_option(
        self,
        selector: str,
        values: list[str] | None = None,
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        ...

    def navigate_back(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        ...

    def navigate_forward(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        ...

    def drag_target(
        self,
        source_target: str,
        dest_target: str,
        source_element: str = "",
        dest_element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        ...

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        ...

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        ...

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        ...

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        ...

    def diagnose_page(self, tab_id: str = "") -> Dict:
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

    def mouse_gesture_path(
        self,
        points: list[dict[str, Any]],
        *,
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        ...

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        ...

    def close(self) -> None:
        ...


class BrowserEngine(Protocol):
    engine_name: str

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        ...
