import unittest
import inspect

from chromium_advanced.browser_engines.base import BrowserSessionSummary
from chromium_advanced.browser_session_kernel import ManagedBrowserSession


class FakeRawSession:
    def __init__(self, engine_name="playwright_cli"):
        self.engine_name = engine_name
        self.clicked_targets = []
        self.typed_targets = []
        self.managed_actions = []
        self.run_script_calls = 0
        self.visible_after = 1
        self.raise_click = None
        self.deep_candidate = False
        self.large_html = False
        self.candidate_entries = None
        self.modal_visible = False
        self.page_text_override = None
        self.diagnose_page_calls = 0
        self.summary_alive = True
        self.current_url = "https://example.com"
        self.current_title = "Example"
        self.page_drift_override = None

    def get_summary(self):
        return BrowserSessionSummary(current_url=self.current_url, title=self.current_title, alive=self.summary_alive)

    def get_capabilities(self):
        return {
            "engine_name": self.engine_name,
            "supports_snapshot": self.engine_name == "patchright",
            "supports_snapshot_refs": self.engine_name == "patchright",
            "supports_target_actions": True,
            "supports_selector_actions": True,
            "supports_highlight": False,
            "supports_coordinates": self.engine_name != "playwright_cli",
            "supports_post_action_context": self.engine_name == "patchright",
            "supports_tabs": True,
            "supports_console_messages": True,
            "supports_page_errors": True,
            "supports_network_requests": True,
        }

    def get_current_url(self, tab_id=""):
        payload = {"tab_id": tab_id or "tab-001", "url": self.current_url, "title": self.current_title}
        if isinstance(self.page_drift_override, dict):
            payload["page_drift"] = dict(self.page_drift_override)
        return payload

    def list_tabs(self):
        return {"tabs": [{"tab_id": "tab-001", "index": 0, "active": True, "url": "https://example.com", "title": "Example"}], "count": 1}

    def open_tab(self, url="", activate=True, wait_for_ready=True, timeout_seconds=20):
        return {"opened": True, "tab": {"tab_id": "tab-002", "index": 1, "active": bool(activate), "url": url, "title": "New"}}

    def activate_tab(self, tab_id="", index=-1, title_contains="", url_contains=""):
        return {"activated": True, "tab": {"tab_id": tab_id or "tab-001", "index": 0}}

    def close_tab(self, tab_id="", index=-1):
        return {"closed": True, "closed_tab": {"tab_id": tab_id or "tab-001"}}

    def navigate(self, url, wait_for_ready=True, timeout_seconds=20, tab_id=""):
        return {"tab_id": tab_id or "tab-001", "url": url, "title": "Example"}

    def get_page_text(self, tab_id=""):
        if isinstance(self.page_text_override, str):
            return {"tab_id": tab_id or "tab-001", "url": self.current_url, "title": self.current_title, "text": self.page_text_override}
        return {"tab_id": tab_id or "tab-001", "url": self.current_url, "title": self.current_title, "text": "Hello"}

    def get_page_html(self, tab_id=""):
        if self.large_html:
            custom = "<app-shell><yt-chip-cloud-chip-renderer>Newest</yt-chip-cloud-chip-renderer></app-shell>"
            html = "<html><head><title>Example</title></head><body>" + (custom * 400) + "</body></html>"
            return {"tab_id": tab_id or "tab-001", "url": self.current_url, "title": self.current_title, "html": html}
        return {"tab_id": tab_id or "tab-001", "url": self.current_url, "title": self.current_title, "html": "<html></html>"}

    def inspect_elements(self, selector, by="css", limit=10, tab_id=""):
        raise NotImplementedError("inspect_elements is not implemented")

    def get_active_element(self, tab_id=""):
        return {"element": {"tag_name": "input", "id": "name", "value": "hello", "role": "textbox"}}

    def get_interaction_context(self, tab_id=""):
        modal_state = {
            "visible": self.modal_visible,
            "count": 1 if self.modal_visible else 0,
            "primary_dialog": {"tag_name": "div", "role": "dialog", "text": "Newest Apply"} if self.modal_visible else {},
            "dialogs": [{"tag_name": "div", "role": "dialog", "text": "Newest Apply"}] if self.modal_visible else [],
        }
        return {
            "interaction_context": {
                "action_name": "inspect",
                "page": self.get_current_url(tab_id=tab_id),
                "tabs": self.list_tabs().get("tabs", []),
                "active_tab_id": tab_id or "tab-001",
                "active_element": {"tag_name": "input", "id": "name", "value": "hello", "role": "textbox"},
                "modal_state": modal_state,
                "snapshot": {"unsupported": True, "message": "fake runtime"},
            }
        }

    def snapshot(self, target="", by="css", depth=None, boxes=False, filename="", tab_id=""):
        return {"snapshot": {"unsupported": True, "message": "not supported"}}

    def list_candidates(self, target="", by="css", text_filter="", limit=25, include_boxes=True, tab_id=""):
        raise NotImplementedError("list_candidates is not implemented")

    def wait_for(self, selector, by="css", timeout_seconds=20, condition="visible"):
        raise NotImplementedError("wait_for is not implemented")

    def click(self, selector, by="css", timeout_seconds=20):
        if self.raise_click:
            raise self.raise_click
        self.clicked_targets.append((selector, by))
        return {"clicked": True}

    def click_target(self, target, element="", by="css", timeout_seconds=20, double_click=False):
        if by == "deep_css":
            raise NotImplementedError("deep_css target actions are not implemented")
        self.clicked_targets.append((target, by))
        return {"clicked": True, "target": target}

    def type_text(self, selector, text, by="css", clear_first=True, submit=False, timeout_seconds=20):
        self.typed_targets.append((selector, text, by))
        return {"typed": True}

    def type_target(self, target, text, element="", by="css", clear_first=True, submit=False, timeout_seconds=20):
        if by == "deep_css":
            raise NotImplementedError("deep_css target actions are not implemented")
        self.typed_targets.append((target, text, by))
        return {"typed": True, "target": target}

    def type_target_and_verify(self, target, text, element="", by="css", clear_first=True, submit=False, timeout_seconds=20):
        if by == "deep_css":
            raise NotImplementedError("deep_css target actions are not implemented")
        return {"typed": True, "verified": True}

    def press_key(self, key, count=1, selector="", by="css", timeout_seconds=20):
        return {"pressed": True, "key": key, "count": count}

    def run_script(self, script, tab_id=""):
        self.run_script_calls += 1
        if 'const action = "click"' in script:
            self.managed_actions.append(("click", script))
            return {
                "result": {
                    "ok": True,
                    "clicked": True,
                    "target": "app-shell#root >>> button.save",
                    "by": "deep_css",
                    "details": {
                        "tag_name": "button",
                        "text": "Save",
                        "value": "",
                        "visible": True,
                        "enabled": True,
                        "id": "save",
                        "name": "",
                        "class": "save",
                        "aria_label": "Save",
                        "role": "button",
                        "href": "",
                        "selector": "button.save",
                        "deep_selector": "app-shell#root >>> button.save",
                    },
                }
            }
        if 'const action = "type"' in script:
            self.managed_actions.append(("type", script))
            return {
                "result": {
                    "ok": True,
                    "typed": True,
                    "target": "app-shell#root >>> input.name",
                    "by": "deep_css",
                    "value": "Alice",
                    "details": {
                        "tag_name": "input",
                        "text": "",
                        "value": "Alice",
                        "visible": True,
                        "enabled": True,
                        "id": "name",
                        "name": "name",
                        "class": "name",
                        "aria_label": "Name",
                        "role": "textbox",
                        "href": "",
                        "selector": "input.name",
                        "deep_selector": "app-shell#root >>> input.name",
                    },
                }
            }
        if "\"describe\" === 'describe'" in script:
            visible = self.run_script_calls >= self.visible_after
            return {
                "result": {
                    "tag_name": "input",
                    "text": "",
                    "text_preview": "",
                    "value": "hello",
                    "visible": visible,
                    "enabled": True,
                    "id": "name",
                    "name": "name",
                    "class": "",
                    "placeholder": "Name",
                    "title_attr": "Name field",
                    "aria_label": "Name",
                    "accessible_name": "Name",
                    "role": "textbox",
                    "input_type": "text",
                    "control_type": "textbox",
                    "href": "",
                    "ancestry_path": "input > form",
                    "custom_element_ancestry": ["app-shell"],
                    "dialog_ancestry": [],
                    "overlay_ancestry": [],
                    "scope_tags": ["custom-element"],
                    "in_dialog": False,
                    "in_overlay": False,
                    "outer_html": "<input id='name' value='hello' />",
                    "selector": "#name",
                    "box": {"x": 1, "y": 2, "width": 100, "height": 20},
                }
            }
        if "nodes.slice(0" in script:
            if isinstance(self.candidate_entries, list):
                return {"result": self.candidate_entries}
            deep_selector = "app-shell#root >>> button.save" if self.deep_candidate else ""
            return {
                "result": [
                    {
                        "tag_name": "button",
                        "text": "Save",
                        "text_preview": "Save",
                        "value": "",
                        "visible": True,
                        "enabled": True,
                        "id": "save",
                        "name": "",
                        "class": "btn primary",
                        "placeholder": "",
                        "title_attr": "",
                        "aria_label": "Save",
                        "accessible_name": "Save",
                        "role": "button",
                        "input_type": "",
                        "control_type": "button",
                        "href": "",
                        "ancestry_path": "button > app-shell",
                        "custom_element_ancestry": ["app-shell"],
                        "dialog_ancestry": [],
                        "overlay_ancestry": [],
                        "scope_tags": ["custom-element"],
                        "in_dialog": False,
                        "in_overlay": False,
                        "outer_html": "<button id='save'>Save</button>",
                        "selector": "#save",
                        "deep_selector": deep_selector,
                        "box": {"x": 1, "y": 2, "width": 100, "height": 20},
                    }
                ]
            }
        if "visibleDialogs" in script:
            return {
                "result": {
                    "visible": self.modal_visible,
                    "count": 1 if self.modal_visible else 0,
                    "primary_dialog": {"tag_name": "div", "role": "dialog", "text": "Newest Apply"} if self.modal_visible else {},
                    "dialogs": [{"tag_name": "div", "role": "dialog", "text": "Newest Apply"}] if self.modal_visible else [],
                }
            }
        if "document.activeElement" in script:
            return {"result": {"tag_name": "input", "id": "name", "name": "name", "text": "", "aria_label": "Name", "role": "textbox", "value": "hello", "href": ""}}
        return {"result": None}

    def _describe_target_via_eval(self, target):
        del target
        self.run_script_calls += 1
        visible = self.run_script_calls >= self.visible_after
        return {
            "tag_name": "input",
            "text": "",
            "text_preview": "",
            "value": "hello",
            "visible": visible,
            "enabled": True,
            "id": "name",
            "name": "name",
            "class": "",
            "placeholder": "Name",
            "title_attr": "Name field",
            "aria_label": "Name",
            "accessible_name": "Name",
            "role": "textbox",
            "input_type": "text",
            "control_type": "textbox",
            "href": "",
            "ancestry_path": "input > form",
            "custom_element_ancestry": ["app-shell"],
            "dialog_ancestry": [],
            "overlay_ancestry": [],
            "scope_tags": ["custom-element"],
            "in_dialog": False,
            "in_overlay": False,
            "selector": "#name",
            "deep_selector": "",
            "box": {"x": 1, "y": 2, "width": 100, "height": 20},
        }

    def _eval_on_target(self, target, func_text):
        del target, func_text
        details = self._describe_target_via_eval("")
        return {"found": True, **details}

    def get_console_messages(self, tab_id="", limit=100, level=""):
        return {"count": 0, "messages": []}

    def get_page_errors(self, tab_id="", limit=100):
        return {"count": 0, "errors": []}

    def get_network_requests(self, tab_id="", limit=100, failed_only=False):
        return {"count": 0, "requests": []}

    def clear_debug_buffers(self, tab_id=""):
        return {"cleared": True}

    def diagnose_page(self, tab_id=""):
        self.diagnose_page_calls += 1
        return {"diagnosis": {}, "interaction_context": self.get_interaction_context(tab_id=tab_id).get("interaction_context", {})}

    def verify_text(self, text):
        return {"verified": True, "text": text}

    def verify_dialog(self, accessible_name="", text=""):
        return {"verified": True}

    def verify_active_element(self, target="", by="css", element=""):
        raise NotImplementedError("verify_active_element is not implemented")

    def verify_target_value(self, target, expected_value, element="", by="css"):
        raise NotImplementedError("verify_target_value is not implemented")

    def verify_target_visible(self, target, element="", by="css"):
        raise NotImplementedError("verify_target_visible is not implemented")

    def describe_target(self, target, element="", by="css", include_box=True):
        raise NotImplementedError("describe_target is not implemented")

    def diagnose_target(self, target, element="", by="css", text_filter="", limit=10):
        raise NotImplementedError("diagnose_target is not implemented")

    def verify_element(self, role, accessible_name):
        return {"verified": True, "role": role, "accessible_name": accessible_name}

    def highlight_target(self, target, element="", by="css", style=""):
        raise NotImplementedError("highlight_target is not implemented")

    def clear_highlights(self):
        raise NotImplementedError("clear_highlights is not implemented")

    def mouse_move_xy(self, x, y):
        return {"moved": True}

    def mouse_click_xy(self, x, y, button="left", click_count=1, delay_ms=0):
        return {"clicked": True}

    def mouse_drag_xy(self, start_x, start_y, end_x, end_y):
        return {"dragged": True}

    def screenshot(self, filename="", tab_id=""):
        return {"path": filename or "shot.png"}

    def close(self):
        return None


class ManagedBrowserSessionTests(unittest.TestCase):
    def test_capabilities_expose_runtime_profile(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="playwright_cli"))
        caps = session.get_capabilities()
        self.assertEqual(caps["runtime_profile"], "fast")
        self.assertEqual(caps["capability_version"], 2)
        self.assertIn("capabilities", caps)
        self.assertTrue(caps["supports_post_action_context"])

    def test_list_candidates_falls_back_to_dom_script(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="selenium_uc"))
        result = session.list_candidates(text_filter="save", limit=5)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["action_meta"]["used_fallback"])
        self.assertEqual(result["candidates"][0]["selector"], "#save")
        self.assertEqual(result["resolution_trace"]["source"], "dom_fallback")
        self.assertEqual(result["resolution_trace"]["stage"], "ranked_dom_query")

    def test_snapshot_ref_click_translates_to_selector_on_runtime_without_refs(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        session = ManagedBrowserSession(raw)
        snapshot = session.snapshot()
        ref = snapshot["refs"][0]
        result = session.click_target(ref)
        self.assertTrue(result["clicked"])
        self.assertEqual(raw.clicked_targets[-1], ("#save", "css"))

    def test_list_candidates_preserves_deep_selector_and_click_uses_managed_action(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        raw.deep_candidate = True
        session = ManagedBrowserSession(raw)
        result = session.list_candidates(text_filter="save", limit=5)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["by"], "deep_css")
        self.assertEqual(session._snapshot_ref_map[candidate["ref"]]["by"], "deep_css")
        clicked = session.click_target(candidate["ref"])
        self.assertTrue(clicked["clicked"])
        self.assertEqual(len(raw.clicked_targets), 0)
        self.assertEqual(raw.managed_actions[-1][0], "click")

    def test_snapshot_ref_type_uses_managed_action_for_deep_selector(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        session = ManagedBrowserSession(raw)
        session._snapshot_ref_map["e9"] = {"selector": "app-shell#root >>> input.name", "by": "deep_css"}
        result = session.type_target_and_verify("e9", "Alice")
        self.assertTrue(result["typed"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["value"], "Alice")
        self.assertEqual(len(raw.typed_targets), 0)
        self.assertEqual(raw.managed_actions[-1][0], "type")

    def test_parse_snapshot_candidates_can_cache_high_ref_ids(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="playwright_cli"))
        snapshot_text = "- button \"Late target\" [ref=e1074]\n- button \"Apply\" [ref=e1081]"
        parsed = session._parse_snapshot_candidates(snapshot_text, limit=5000)
        self.assertEqual(parsed[0]["ref"], "e1074")
        self.assertEqual(session._snapshot_ref_map["e1081"]["by"], "snapshot_ref")

    def test_wait_for_falls_back_and_observes_visibility(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.visible_after = 2
        session = ManagedBrowserSession(raw)
        result = session.wait_for("#name", by="css", timeout_seconds=2, condition="visible")
        self.assertTrue(result["found"])
        self.assertTrue(result["action_meta"]["used_fallback"])

    def test_runtime_exception_is_normalized(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.raise_click = RuntimeError("browser crashed")
        session = ManagedBrowserSession(raw)
        result = session.click("#boom")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "runtime_action_failed")
        self.assertEqual(result["action_meta"]["engine_name"], "playwright_cli")
        self.assertEqual(result["post_action_context"]["action_name"], "click_failed")

    def test_action_result_gets_managed_post_action_context(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="playwright_cli"))
        result = session.click("#save")
        self.assertTrue(result["clicked"])
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(result["post_action_context"]["action_name"], "click")
        self.assertEqual(result["post_action_context"]["active_tab_id"], "tab-001")
        self.assertEqual(result["post_action_context"]["active_element"]["id"], "name")
        self.assertGreaterEqual(len(result["post_action_context"]["recent_actions"]), 1)
        self.assertIn("session_health", result["post_action_context"])
        self.assertTrue(result["post_action_context"]["session_health"]["alive"])
        self.assertEqual(result["post_action_context"]["session_health"]["recovery_hint"], "none")
        self.assertEqual(result["post_action_context"]["session_health"]["failure_classification"], "healthy")

    def test_get_page_html_is_truncated_and_summarized_for_large_pages(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.large_html = True
        session = ManagedBrowserSession(raw)
        result = session.get_page_html()
        self.assertTrue(result["html_truncated"])
        self.assertGreater(result["html_length"], len(result["html"]))
        self.assertIn("html_summary", result)
        self.assertGreaterEqual(result["html_summary"]["custom_element_count"], 1)

    def test_list_candidates_prioritizes_better_text_match(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        raw.candidate_entries = [
            {
                "tag_name": "button",
                "text": "Sort by",
                "value": "",
                "visible": True,
                "enabled": True,
                "id": "sort",
                "name": "",
                "class": "menu-trigger",
                "aria_label": "Sort by",
                "role": "button",
                "href": "",
                "outer_html": "<button>Sort by</button>",
                "selector": "#sort",
                "box": {"x": 1, "y": 2, "width": 100, "height": 20},
            },
            {
                "tag_name": "button",
                "text": "Newest",
                "value": "",
                "visible": True,
                "enabled": True,
                "id": "newest",
                "name": "",
                "class": "menu-item",
                "aria_label": "Newest",
                "role": "menuitem",
                "href": "",
                "outer_html": "<button>Newest</button>",
                "selector": "#newest",
                "box": {"x": 1, "y": 2, "width": 100, "height": 20},
            },
        ]
        session = ManagedBrowserSession(raw)
        result = session.list_candidates(text_filter="newest", limit=5)
        self.assertEqual(result["candidates"][0]["text"], "Newest")
        self.assertGreater(result["candidates"][0]["match_score"], 0)

    def test_diagnose_page_includes_recent_actions(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="playwright_cli"))
        session.click("#save")
        session.type_text("#name", "Alice", by="css")
        result = session.diagnose_page()
        self.assertIn("recent_actions", result)
        self.assertGreaterEqual(len(result["recent_actions"]), 2)
        self.assertEqual(result["recent_actions"][-1]["action_name"], "type_text")
        self.assertIn("managed_diagnostics", result)
        self.assertIn("session_health", result)
        self.assertTrue(result["session_health"]["alive"])
        self.assertEqual(result["session_health"]["recovery_hint"], "none")
        self.assertIn("session_health", result["interaction_context"])
        self.assertEqual(result["diagnosis"]["mode"], "managed_fast_path")

    def test_action_trace_summary_exposes_slow_and_failure_counts(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        session = ManagedBrowserSession(raw)
        session.click("#save")
        raw.raise_click = RuntimeError("missing target")
        session.click("#missing")
        result = session.get_action_trace(limit=10)
        self.assertEqual(result["engine_name"], "playwright_cli")
        self.assertEqual(result["recent_action_count"], 2)
        self.assertEqual(result["recent_failure_count"], 1)
        self.assertEqual(result["recent_failures"][-1]["action_name"], "click")
        self.assertIn("slowest_actions", result)
        self.assertEqual(result["recent_actions"][-1]["ok"], False)

    def test_playwright_cli_diagnose_page_uses_fast_managed_path(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        session = ManagedBrowserSession(raw)
        result = session.diagnose_page()
        self.assertEqual(raw.diagnose_page_calls, 0)
        self.assertEqual(result["diagnosis"]["mode"], "managed_fast_path")
        self.assertIn("console", result)
        self.assertIn("page_errors", result)
        self.assertIn("network", result)

    def test_diagnose_target_includes_managed_metadata(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="selenium_uc"))
        session.click("#save")
        result = session.diagnose_target("#save", by="css", text_filter="save", limit=5)
        self.assertIn("managed_diagnostics", result)
        self.assertEqual(result["managed_diagnostics"]["target"], "#save")
        self.assertEqual(result["managed_diagnostics"]["by"], "css")
        self.assertIn("recent_actions", result)
        self.assertIn("session_health", result)
        self.assertTrue(result["session_health"]["alive"])
        self.assertIn("resolution_trace", result)

    def test_interaction_context_exposes_session_health(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.modal_visible = True
        session = ManagedBrowserSession(raw)
        result = session.get_interaction_context()
        context = result["interaction_context"]
        self.assertIn("session_health", context)
        self.assertTrue(context["session_health"]["alive"])
        self.assertEqual(context["session_health"]["engine_name"], "playwright_cli")
        self.assertEqual(context["session_health"]["runtime_profile"], "fast")
        self.assertTrue(context["modal_state"]["visible"])
        self.assertEqual(context["modal_state"]["count"], 1)

    def test_interaction_context_refreshes_modal_state_from_managed_probe_for_playwright_cli(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.modal_visible = True

        def stale_raw_context(tab_id=""):
            return {
                "interaction_context": {
                    "action_name": "inspect",
                    "page": raw.get_current_url(tab_id=tab_id),
                    "tabs": raw.list_tabs().get("tabs", []),
                    "active_tab_id": tab_id or "tab-001",
                    "active_element": {"tag_name": "tp-yt-paper-radio-button", "role": "radio", "text": "Most relevant"},
                    "modal_state": {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []},
                    "snapshot": {"unsupported": True, "message": "fake runtime"},
                }
            }

        raw.get_interaction_context = stale_raw_context
        session = ManagedBrowserSession(raw)
        result = session.get_interaction_context()
        context = result["interaction_context"]
        self.assertTrue(context["modal_state"]["visible"])
        self.assertEqual(context["modal_state"]["count"], 1)

    def test_fallback_modal_state_script_defines_style_in_visible_dialog_mapping(self):
        source = inspect.getsource(ManagedBrowserSession._fallback_modal_state)
        self.assertIn("const style = window.getComputedStyle(el);", source)

    def test_session_health_suggests_refresh_for_target_failures(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.raise_click = RuntimeError("element is not visible")
        session = ManagedBrowserSession(raw)
        result = session.click("#missing")
        health = result["post_action_context"]["session_health"]
        self.assertEqual(result["error_code"], "target_not_interactable")
        self.assertEqual(health["recent_failure_count"], 1)
        self.assertEqual(health["recovery_hint"], "refresh_candidates_or_snapshot")
        self.assertEqual(health["failure_classification"], "target_resolution")
        self.assertIn("retry_with_scoped_targeting", health["recovery_actions"])

    def test_session_health_classifies_page_drift(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.current_url = "https://panel.awoocd.online/vault"
        raw.current_title = "Resource Panel"
        raw.page_drift_override = {
            "tab_id": "tab-001",
            "drifted": True,
            "expected_url": "https://studio.youtube.com/channel/UC123/comments/inbox",
            "current_url": raw.current_url,
            "expected_title": "Community - YouTube Studio",
            "current_title": raw.current_title,
        }
        session = ManagedBrowserSession(raw)
        context = session.get_interaction_context()["interaction_context"]
        health = context["session_health"]
        self.assertTrue(health["alive"])
        self.assertEqual(health["failure_classification"], "page_drift")
        self.assertEqual(health["recovery_hint"], "reactivate_expected_tab")
        self.assertIn("retry_on_sticky_tab", health["recovery_actions"])
        self.assertTrue(health["page_drift"]["drifted"])
        self.assertEqual(health["page_drift"]["expected_title"], "Community - YouTube Studio")

    def test_diagnose_page_surfaces_drift_governance(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.current_url = "https://panel.awoocd.online/vault"
        raw.current_title = "Resource Panel"
        raw.page_drift_override = {
            "tab_id": "tab-001",
            "drifted": True,
            "expected_url": "https://studio.youtube.com/channel/UC123/comments/inbox",
            "current_url": raw.current_url,
            "expected_title": "Community - YouTube Studio",
            "current_title": raw.current_title,
        }
        session = ManagedBrowserSession(raw)
        result = session.diagnose_page()
        self.assertEqual(result["session_health"]["failure_classification"], "page_drift")
        self.assertEqual(result["interaction_context"]["session_health"]["recovery_hint"], "reactivate_expected_tab")
        self.assertTrue(result["session_health"]["page_drift"]["drifted"])

    def test_list_candidates_boosts_transient_popup_controls(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        raw.candidate_entries = [
            {
                "tag_name": "button",
                "text": "Newest",
                "text_preview": "Newest",
                "value": "",
                "visible": True,
                "enabled": True,
                "id": "newest-button",
                "name": "",
                "class": "btn",
                "placeholder": "",
                "title_attr": "",
                "aria_label": "Newest",
                "accessible_name": "Newest",
                "aria_expanded": "",
                "aria_haspopup": "",
                "role": "button",
                "input_type": "",
                "control_type": "button",
                "href": "",
                "ancestry_path": "button > div",
                "custom_element_ancestry": [],
                "dialog_ancestry": [],
                "overlay_ancestry": [],
                "scope_tags": [],
                "in_dialog": False,
                "in_overlay": False,
                "outer_html": "<button>Newest</button>",
                "selector": "#newest-button",
                "box": {"x": 1, "y": 2, "width": 100, "height": 20},
            },
            {
                "tag_name": "div",
                "text": "Newest first",
                "text_preview": "Newest first",
                "value": "",
                "visible": True,
                "enabled": True,
                "id": "newest-item",
                "name": "",
                "class": "dropdown-menu overlay-item",
                "placeholder": "",
                "title_attr": "",
                "aria_label": "Newest first",
                "accessible_name": "Newest first",
                "aria_expanded": "true",
                "aria_haspopup": "menu",
                "role": "menuitem",
                "input_type": "",
                "control_type": "menuitem",
                "href": "",
                "ancestry_path": "div > div > app-shell",
                "custom_element_ancestry": ["app-shell"],
                "dialog_ancestry": [],
                "overlay_ancestry": ["div"],
                "scope_tags": ["overlay", "expanded"],
                "in_dialog": False,
                "in_overlay": True,
                "outer_html": "<div role='menuitem'>Newest first</div>",
                "selector": "#newest-item",
                "box": {"x": 1, "y": 2, "width": 100, "height": 20},
            },
        ]
        session = ManagedBrowserSession(raw)
        result = session.list_candidates(text_filter="newest", limit=5)
        self.assertEqual(result["candidates"][0]["id"], "newest-item")
        self.assertEqual(result["candidates"][0]["role"], "menuitem")
        self.assertGreater(result["candidates"][0]["match_score"], result["candidates"][1]["match_score"])
        self.assertTrue(result["resolution_trace"]["scoped"])

    def test_describe_target_returns_richer_structured_fields(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        raw.candidate_entries = [
            {
                "tag_name": "input",
                "text": "",
                "text_preview": "",
                "value": "hello",
                "visible": True,
                "enabled": True,
                "id": "name",
                "name": "name",
                "class": "",
                "placeholder": "Name",
                "title_attr": "Name field",
                "aria_label": "Name",
                "accessible_name": "Name",
                "role": "textbox",
                "input_type": "text",
                "control_type": "textbox",
                "href": "",
                "ancestry_path": "input > app-shell",
                "custom_element_ancestry": ["app-shell"],
                "dialog_ancestry": [],
                "overlay_ancestry": [],
                "scope_tags": ["custom-element"],
                "in_dialog": False,
                "in_overlay": False,
                "outer_html": "<input id='name' value='hello' />",
                "selector": "#name",
                "box": {"x": 1, "y": 2, "width": 100, "height": 20},
            }
        ]
        session = ManagedBrowserSession(raw)
        result = session.describe_target("#name", by="css")
        self.assertEqual(result["accessible_name"], "Name")
        self.assertEqual(result["placeholder"], "Name")
        self.assertEqual(result["control_type"], "textbox")
        self.assertIn("app-shell", result["custom_element_ancestry"])
        self.assertEqual(result["resolution_trace"]["source"], "dom_fallback")

    def test_diagnose_page_extracts_structured_comment_threads(self):
        raw = FakeRawSession(engine_name="playwright_cli")
        raw.page_text_override = "\n".join(
            [
                "Community",
                "Comments",
                "View: Newest",
                "@alice  • 2 hours ago",
                "First comment",
                "Video A",
                "0 replies",
                "@bob  • 1 day ago",
                "Second comment",
                "Video B",
                "1 reply",
            ]
        )
        session = ManagedBrowserSession(raw)
        result = session.diagnose_page()
        structured = result["structured_page"]
        self.assertEqual(structured["comment_thread_count"], 2)
        self.assertEqual(structured["comment_threads"][0]["author"], "@alice")
        self.assertEqual(structured["comment_threads"][0]["video_title"], "Video A")
        self.assertIn("Community", structured["headings"])


if __name__ == "__main__":
    unittest.main()
