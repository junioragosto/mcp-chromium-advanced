import unittest

from chromium_advanced.browser_engines.base import BrowserSessionSummary
from chromium_advanced.browser_session_kernel import ManagedBrowserSession


class FakeRawSession:
    def __init__(self, engine_name="playwright_cli"):
        self.engine_name = engine_name
        self.clicked_targets = []
        self.typed_targets = []
        self.run_script_calls = 0
        self.visible_after = 1
        self.raise_click = None

    def get_summary(self):
        return BrowserSessionSummary(current_url="https://example.com", title="Example", alive=True)

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
        return {"tab_id": tab_id or "tab-001", "url": "https://example.com", "title": "Example"}

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
        return {"tab_id": tab_id or "tab-001", "url": "https://example.com", "title": "Example", "text": "Hello"}

    def get_page_html(self, tab_id=""):
        return {"tab_id": tab_id or "tab-001", "url": "https://example.com", "title": "Example", "html": "<html></html>"}

    def inspect_elements(self, selector, by="css", limit=10, tab_id=""):
        raise NotImplementedError("inspect_elements is not implemented")

    def get_active_element(self, tab_id=""):
        return {"element": {"tag_name": "input", "id": "name"}}

    def get_interaction_context(self, tab_id=""):
        return {"interaction_context": {"tabs": [], "active_element": {}}}

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
        self.clicked_targets.append((target, by))
        return {"clicked": True, "target": target}

    def type_text(self, selector, text, by="css", clear_first=True, submit=False, timeout_seconds=20):
        self.typed_targets.append((selector, text, by))
        return {"typed": True}

    def type_target(self, target, text, element="", by="css", clear_first=True, submit=False, timeout_seconds=20):
        self.typed_targets.append((target, text, by))
        return {"typed": True, "target": target}

    def type_target_and_verify(self, target, text, element="", by="css", clear_first=True, submit=False, timeout_seconds=20):
        return {"typed": True, "verified": True}

    def press_key(self, key, count=1, selector="", by="css", timeout_seconds=20):
        return {"pressed": True, "key": key, "count": count}

    def run_script(self, script, tab_id=""):
        self.run_script_calls += 1
        if "\"describe\" === 'describe'" in script:
            visible = self.run_script_calls >= self.visible_after
            return {
                "result": {
                    "tag_name": "input",
                    "text": "",
                    "value": "hello",
                    "visible": visible,
                    "enabled": True,
                    "id": "name",
                    "name": "name",
                    "class": "",
                    "aria_label": "Name",
                    "role": "textbox",
                    "href": "",
                    "outer_html": "<input id='name' value='hello' />",
                    "selector": "#name",
                    "box": {"x": 1, "y": 2, "width": 100, "height": 20},
                }
            }
        if "nodes.slice(0" in script:
            return {
                "result": [
                    {
                        "tag_name": "button",
                        "text": "Save",
                        "value": "",
                        "visible": True,
                        "enabled": True,
                        "id": "save",
                        "name": "",
                        "class": "btn primary",
                        "aria_label": "Save",
                        "role": "button",
                        "href": "",
                        "outer_html": "<button id='save'>Save</button>",
                        "selector": "#save",
                        "box": {"x": 1, "y": 2, "width": 100, "height": 20},
                    }
                ]
            }
        if "document.activeElement" in script:
            return {"result": {"tag_name": "input", "id": "name", "name": "name", "text": "", "aria_label": "Name", "role": "textbox", "value": "hello", "href": ""}}
        return {"result": None}

    def get_console_messages(self, tab_id="", limit=100, level=""):
        return {"count": 0, "messages": []}

    def get_page_errors(self, tab_id="", limit=100):
        return {"count": 0, "errors": []}

    def get_network_requests(self, tab_id="", limit=100, failed_only=False):
        return {"count": 0, "requests": []}

    def clear_debug_buffers(self, tab_id=""):
        return {"cleared": True}

    def diagnose_page(self, tab_id=""):
        return {"diagnosis": {}}

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

    def test_list_candidates_falls_back_to_dom_script(self):
        session = ManagedBrowserSession(FakeRawSession(engine_name="selenium_uc"))
        result = session.list_candidates(text_filter="save", limit=5)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["action_meta"]["used_fallback"])
        self.assertEqual(result["candidates"][0]["selector"], "#save")

    def test_snapshot_ref_click_translates_to_selector_on_runtime_without_refs(self):
        raw = FakeRawSession(engine_name="selenium_uc")
        session = ManagedBrowserSession(raw)
        snapshot = session.snapshot()
        ref = snapshot["refs"][0]
        result = session.click_target(ref)
        self.assertTrue(result["clicked"])
        self.assertEqual(raw.clicked_targets[-1], ("#save", "css"))

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


if __name__ == "__main__":
    unittest.main()
