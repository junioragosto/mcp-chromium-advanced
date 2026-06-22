from __future__ import annotations

from typing import Any, Callable, Dict

from chromium_advanced.browser_action_orchestrator import BrowserActionOrchestrator


class ActionPipeline:
    def __init__(self, browser_session):
        self.browser_session = browser_session
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "navigate": self._navigate,
            "get_page_text": self._get_page_text,
            "get_current_url": self._get_current_url,
            "get_page_html": self._get_page_html,
            "list_tabs": self._list_tabs,
            "list_candidates": self._list_candidates,
            "open_tab": self._open_tab,
            "activate_tab": self._activate_tab,
            "close_tab": self._close_tab,
            "resize": self._resize,
            "click": self._click,
            "click_target": self._click_target,
            "type_text": self._type_text,
            "type_target": self._type_target,
            "type_target_and_verify": self._type_target_and_verify,
            "press_key": self._press_key,
            "run_script": self._run_script,
            "run_script_batch": self._run_script_batch,
            "watch_page_state": self._watch_page_state,
            "watch_target_state": self._watch_target_state,
            "wait_for": self._wait_for,
            "wait_for_text": self._wait_for_text,
            "wait_for_text_gone": self._wait_for_text_gone,
            "wait_for_text_change": self._wait_for_text_change,
            "wait_for_page_stable": self._wait_for_page_stable,
            "wait_for_timeout": self._wait_for_timeout,
            "inspect_elements": self._inspect_elements,
            "get_active_element": self._get_active_element,
            "get_interaction_context": self._get_interaction_context,
            "describe_target": self._describe_target,
            "diagnose_target": self._diagnose_target,
            "diagnose_page": self._diagnose_page,
            "verify_target_visible": self._verify_target_visible,
            "verify_target_value": self._verify_target_value,
            "verify_active_element": self._verify_active_element,
            "hover": self._hover,
            "highlight_target": self._highlight_target,
            "clear_highlights": self._clear_highlights,
            "mouse_move_xy": self._mouse_move_xy,
            "mouse_click_xy": self._mouse_click_xy,
            "mouse_drag_xy": self._mouse_drag_xy,
            "mouse_gesture_path": self._mouse_gesture_path,
            "select_option": self._select_option,
            "handle_dialog": self._handle_dialog,
            "file_upload": self._file_upload,
            "navigate_back": self._navigate_back,
            "navigate_forward": self._navigate_forward,
            "drag_target": self._drag_target,
            "get_console_messages": self._get_console_messages,
            "get_page_errors": self._get_page_errors,
            "get_network_requests": self._get_network_requests,
            "clear_debug_buffers": self._clear_debug_buffers,
            "screenshot": self._screenshot,
            "get_summary": self._get_summary,
            "get_capabilities": self._get_capabilities,
            "snapshot": self._snapshot,
            "get_action_trace": self._get_action_trace,
            "verify_text": self._verify_text,
            "verify_dialog": self._verify_dialog,
            "verify_element": self._verify_element,
        }
        self._orchestrator = BrowserActionOrchestrator(
            browser_session=self.browser_session,
            legacy_execute=self._execute_legacy,
            legacy_supports=self._supports_legacy,
        )

    def supports(self, action_name: str) -> bool:
        return self._orchestrator.supports(action_name)

    def execute(self, action_name: str, args: Dict[str, Any] | None = None):
        return self._orchestrator.execute(action_name, args)

    def _supports_legacy(self, action_name: str) -> bool:
        return str(action_name or "").strip() in self._handlers

    def _execute_legacy(self, action_name: str, args: Dict[str, Any] | None = None):
        normalized = str(action_name or "").strip()
        if normalized not in self._handlers:
            raise ValueError(f"unsupported automation action: {normalized}")
        return self._handlers[normalized](dict(args or {}))

    def _navigate(self, args: Dict[str, Any]):
        return self.browser_session.navigate(
            str(args.get("url", "") or ""),
            bool(args.get("wait_for_ready", True)),
            int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _get_page_text(self, args: Dict[str, Any]):
        return self.browser_session.get_page_text(tab_id=str(args.get("tab_id", "") or ""))

    def _get_current_url(self, args: Dict[str, Any]):
        return self.browser_session.get_current_url(tab_id=str(args.get("tab_id", "") or ""))

    def _get_page_html(self, args: Dict[str, Any]):
        return self.browser_session.get_page_html(tab_id=str(args.get("tab_id", "") or ""))

    def _list_tabs(self, args: Dict[str, Any]):
        return self.browser_session.list_tabs()

    def _list_candidates(self, args: Dict[str, Any]):
        return self.browser_session.list_candidates(
            target=str(args.get("target", "") or ""),
            by=str(args.get("by", "css") or "css"),
            text_filter=str(args.get("text_filter", "") or ""),
            limit=int(args.get("limit", 25) or 25),
            include_boxes=bool(args.get("include_boxes", True)),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _open_tab(self, args: Dict[str, Any]):
        return self.browser_session.open_tab(
            url=str(args.get("url", "") or ""),
            activate=bool(args.get("activate", True)),
            wait_for_ready=bool(args.get("wait_for_ready", True)),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _activate_tab(self, args: Dict[str, Any]):
        return self.browser_session.activate_tab(
            tab_id=str(args.get("tab_id", "") or ""),
            index=int(args.get("index", -1) or -1),
            title_contains=str(args.get("title_contains", "") or ""),
            url_contains=str(args.get("url_contains", "") or ""),
        )

    def _close_tab(self, args: Dict[str, Any]):
        return self.browser_session.close_tab(
            tab_id=str(args.get("tab_id", "") or ""),
            index=int(args.get("index", -1) or -1),
        )

    def _resize(self, args: Dict[str, Any]):
        return self.browser_session.resize(
            int(args.get("width", 0) or 0),
            int(args.get("height", 0) or 0),
        )

    def _click(self, args: Dict[str, Any]):
        return self.browser_session.click(
            str(args.get("selector", "") or ""),
            str(args.get("by", "css") or "css"),
            int(args.get("timeout_seconds", 20) or 20),
        )

    def _click_target(self, args: Dict[str, Any]):
        return self.browser_session.click_target(
            str(args.get("target", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            double_click=bool(args.get("double_click", False)),
        )

    def _type_text(self, args: Dict[str, Any]):
        return self.browser_session.type_text(
            str(args.get("selector", "") or ""),
            str(args.get("text", "") or ""),
            str(args.get("by", "css") or "css"),
            bool(args.get("clear_first", True)),
            bool(args.get("submit", False)),
            int(args.get("timeout_seconds", 20) or 20),
        )

    def _type_target(self, args: Dict[str, Any]):
        return self.browser_session.type_target(
            str(args.get("target", "") or ""),
            str(args.get("text", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            clear_first=bool(args.get("clear_first", True)),
            submit=bool(args.get("submit", False)),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _type_target_and_verify(self, args: Dict[str, Any]):
        return self.browser_session.type_target_and_verify(
            str(args.get("target", "") or ""),
            str(args.get("text", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            clear_first=bool(args.get("clear_first", True)),
            submit=bool(args.get("submit", False)),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _press_key(self, args: Dict[str, Any]):
        return self.browser_session.press_key(
            str(args.get("key", "") or ""),
            int(args.get("count", 1) or 1),
            str(args.get("selector", "") or ""),
            str(args.get("by", "css") or "css"),
            int(args.get("timeout_seconds", 20) or 20),
        )

    def _run_script(self, args: Dict[str, Any]):
        return self.browser_session.run_script(
            str(args.get("script", "") or ""),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _run_script_batch(self, args: Dict[str, Any]):
        return self.browser_session.run_script_batch(
            scripts=list(args.get("scripts", []) or []),
            tab_id=str(args.get("tab_id", "") or ""),
            stop_on_error=bool(args.get("stop_on_error", True)),
        )

    def _watch_page_state(self, args: Dict[str, Any]):
        return self.browser_session.watch_page_state(
            text=str(args.get("text", "") or ""),
            previous_text=str(args.get("previous_text", "") or ""),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            stable_cycles=int(args.get("stable_cycles", 2) or 2),
            poll_interval_ms=int(args.get("poll_interval_ms", 500) or 500),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _watch_target_state(self, args: Dict[str, Any]):
        return self.browser_session.watch_target_state(
            target=str(args.get("target", "") or ""),
            text=str(args.get("text", "") or ""),
            previous_text=str(args.get("previous_text", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            stable_cycles=int(args.get("stable_cycles", 2) or 2),
            poll_interval_ms=int(args.get("poll_interval_ms", 500) or 500),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _wait_for(self, args: Dict[str, Any]):
        return self.browser_session.wait_for(
            str(args.get("selector", "") or ""),
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            condition=str(args.get("condition", "visible") or "visible"),
        )

    def _wait_for_text(self, args: Dict[str, Any]):
        return self.browser_session.wait_for_text(
            str(args.get("text", "") or ""),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _wait_for_text_gone(self, args: Dict[str, Any]):
        return self.browser_session.wait_for_text_gone(
            str(args.get("text", "") or ""),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _wait_for_text_change(self, args: Dict[str, Any]):
        return self.browser_session.wait_for_text_change(
            text=str(args.get("text", "") or ""),
            previous_text=str(args.get("previous_text", "") or ""),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _wait_for_page_stable(self, args: Dict[str, Any]):
        return self.browser_session.wait_for_page_stable(
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            stable_cycles=int(args.get("stable_cycles", 2) or 2),
            poll_interval_ms=int(args.get("poll_interval_ms", 500) or 500),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _wait_for_timeout(self, args: Dict[str, Any]):
        return self.browser_session.wait_for_timeout(
            timeout_ms=int(args.get("timeout_ms", 0) or 0),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _inspect_elements(self, args: Dict[str, Any]):
        return self.browser_session.inspect_elements(
            str(args.get("selector", "") or ""),
            by=str(args.get("by", "css") or "css"),
            limit=int(args.get("limit", 10) or 10),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _get_active_element(self, args: Dict[str, Any]):
        return self.browser_session.get_active_element(tab_id=str(args.get("tab_id", "") or ""))

    def _get_interaction_context(self, args: Dict[str, Any]):
        return self.browser_session.get_interaction_context(tab_id=str(args.get("tab_id", "") or ""))

    def _describe_target(self, args: Dict[str, Any]):
        return self.browser_session.describe_target(
            str(args.get("target", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            include_box=bool(args.get("include_box", True)),
        )

    def _diagnose_target(self, args: Dict[str, Any]):
        return self.browser_session.diagnose_target(
            str(args.get("target", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            text_filter=str(args.get("text_filter", "") or ""),
            limit=int(args.get("limit", 10) or 10),
        )

    def _diagnose_page(self, args: Dict[str, Any]):
        return self.browser_session.diagnose_page(tab_id=str(args.get("tab_id", "") or ""))

    def _verify_target_visible(self, args: Dict[str, Any]):
        return self.browser_session.verify_target_visible(
            str(args.get("target", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
        )

    def _verify_target_value(self, args: Dict[str, Any]):
        return self.browser_session.verify_target_value(
            str(args.get("target", "") or ""),
            str(args.get("expected_value", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
        )

    def _verify_active_element(self, args: Dict[str, Any]):
        return self.browser_session.verify_active_element(
            str(args.get("target", "") or ""),
            by=str(args.get("by", "css") or "css"),
            element=str(args.get("element", "") or ""),
        )

    def _hover(self, args: Dict[str, Any]):
        return self.browser_session.hover(
            str(args.get("selector", "") or ""),
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _highlight_target(self, args: Dict[str, Any]):
        return self.browser_session.highlight_target(
            str(args.get("target", "") or ""),
            element=str(args.get("element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            style=str(args.get("style", "") or ""),
        )

    def _clear_highlights(self, args: Dict[str, Any]):
        return self.browser_session.clear_highlights()

    def _mouse_move_xy(self, args: Dict[str, Any]):
        return self.browser_session.mouse_move_xy(
            float(args.get("x", 0) or 0),
            float(args.get("y", 0) or 0),
        )

    def _mouse_click_xy(self, args: Dict[str, Any]):
        return self.browser_session.mouse_click_xy(
            float(args.get("x", 0) or 0),
            float(args.get("y", 0) or 0),
            button=str(args.get("button", "left") or "left"),
            click_count=int(args.get("click_count", 1) or 1),
            delay_ms=int(args.get("delay_ms", 0) or 0),
        )

    def _mouse_drag_xy(self, args: Dict[str, Any]):
        return self.browser_session.mouse_drag_xy(
            float(args.get("start_x", 0) or 0),
            float(args.get("start_y", 0) or 0),
            float(args.get("end_x", 0) or 0),
            float(args.get("end_y", 0) or 0),
        )

    def _mouse_gesture_path(self, args: Dict[str, Any]):
        points = args.get("points") or []
        if not isinstance(points, list):
            points = []
        normalized = []
        for item in points:
            if isinstance(item, dict):
                normalized.append(
                    {
                        "x": float(item.get("x", 0) or 0),
                        "y": float(item.get("y", 0) or 0),
                    }
                )
        return self.browser_session.mouse_gesture_path(
            normalized,
            button=str(args.get("button", "left") or "left"),
            step_delay_ms=int(args.get("step_delay_ms", 30) or 30),
        )

    def _select_option(self, args: Dict[str, Any]):
        values = args.get("values")
        if isinstance(values, list):
            normalized_values = [str(item) for item in values if str(item or "")]
        else:
            single = str(args.get("value", "") or "").strip()
            normalized_values = [single] if single else []
        return self.browser_session.select_option(
            str(args.get("selector", "") or ""),
            values=normalized_values,
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _handle_dialog(self, args: Dict[str, Any]):
        return self.browser_session.handle_dialog(
            accept=bool(args.get("accept", True)),
            prompt_text=str(args.get("prompt_text", "") or ""),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _file_upload(self, args: Dict[str, Any]):
        values = args.get("files")
        if isinstance(values, list):
            normalized_files = [str(item).strip() for item in values if str(item or "").strip()]
        else:
            single = str(args.get("file", "") or "").strip()
            normalized_files = [single] if single else []
        return self.browser_session.file_upload(
            target=str(args.get("target", "") or ""),
            files=normalized_files,
            by=str(args.get("by", "css") or "css"),
            element=str(args.get("element", "") or ""),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _navigate_back(self, args: Dict[str, Any]):
        return self.browser_session.navigate_back(
            wait_for_ready=bool(args.get("wait_for_ready", True)),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _navigate_forward(self, args: Dict[str, Any]):
        return self.browser_session.navigate_forward(
            wait_for_ready=bool(args.get("wait_for_ready", True)),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _drag_target(self, args: Dict[str, Any]):
        return self.browser_session.drag_target(
            str(args.get("source_target", "") or ""),
            str(args.get("dest_target", "") or ""),
            source_element=str(args.get("source_element", "") or ""),
            dest_element=str(args.get("dest_element", "") or ""),
            by=str(args.get("by", "css") or "css"),
            timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
        )

    def _get_console_messages(self, args: Dict[str, Any]):
        return self.browser_session.get_console_messages(
            tab_id=str(args.get("tab_id", "") or ""),
            limit=int(args.get("limit", 100) or 100),
            level=str(args.get("level", "") or ""),
        )

    def _get_page_errors(self, args: Dict[str, Any]):
        return self.browser_session.get_page_errors(
            tab_id=str(args.get("tab_id", "") or ""),
            limit=int(args.get("limit", 100) or 100),
        )

    def _get_network_requests(self, args: Dict[str, Any]):
        return self.browser_session.get_network_requests(
            tab_id=str(args.get("tab_id", "") or ""),
            limit=int(args.get("limit", 100) or 100),
            failed_only=bool(args.get("failed_only", False)),
        )

    def _clear_debug_buffers(self, args: Dict[str, Any]):
        return self.browser_session.clear_debug_buffers(
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _screenshot(self, args: Dict[str, Any]):
        return self.browser_session.screenshot(
            str(args.get("filename", "") or ""),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _get_summary(self, args: Dict[str, Any]):
        return self.browser_session.get_summary()

    def _get_capabilities(self, args: Dict[str, Any]):
        return self.browser_session.get_capabilities()

    def _verify_text(self, args: Dict[str, Any]):
        return self.browser_session.verify_text(
            str(args.get("text", "") or ""),
        )

    def _verify_dialog(self, args: Dict[str, Any]):
        return self.browser_session.verify_dialog(
            accessible_name=str(args.get("accessible_name", "") or ""),
            text=str(args.get("text", "") or ""),
        )

    def _verify_element(self, args: Dict[str, Any]):
        return self.browser_session.verify_element(
            role=str(args.get("role", "") or ""),
            accessible_name=str(args.get("accessible_name", "") or ""),
        )

    def _snapshot(self, args: Dict[str, Any]):
        depth_value = args.get("depth", 0)
        resolved_depth = int(depth_value or 0) or None
        return self.browser_session.snapshot(
            target=str(args.get("target", "") or ""),
            by=str(args.get("by", "css") or "css"),
            depth=resolved_depth,
            boxes=bool(args.get("boxes", False)),
            filename=str(args.get("filename", "") or ""),
            tab_id=str(args.get("tab_id", "") or ""),
        )

    def _get_action_trace(self, args: Dict[str, Any]):
        return self.browser_session.get_action_trace(limit=int(args.get("limit", 20) or 20))
