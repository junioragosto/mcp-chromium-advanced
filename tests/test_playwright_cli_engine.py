import unittest

from chromium_advanced.browser_engines.playwright_cli_engine import PlaywrightCliBrowserSession


class FakePlaywrightCliSession(PlaywrightCliBrowserSession):
    def __init__(self):
        super().__init__(
            cli_path="playwright-cli",
            session_name="fake-session",
            config_path="fake.json",
            output_root=".",
            user_data_root=".",
            profile_name="Profile 1",
        )
        self.tabs = [
            {"tab_id": "tab-000", "index": 0, "title": "Community - YouTube Studio", "url": "https://studio.youtube.com/comments", "active": False, "alive": True},
            {"tab_id": "tab-001", "index": 1, "title": "Other", "url": "https://panel.awoocd.online/vault", "active": True, "alive": True},
        ]
        self.page_by_tab = {
            "tab-000": {
                "url": "https://studio.youtube.com/comments",
                "title": "Community - YouTube Studio",
                "text": "Studio comments",
                "html": "<html><title>Community - YouTube Studio</title></html>",
            },
            "tab-001": {
                "url": "https://panel.awoocd.online/vault",
                "title": "资源工作台",
                "text": "panel page",
                "html": "<html><title>资源工作台</title></html>",
            },
        }
        self.clicked_targets = []
        self.next_click_page_by_tab = {}

    def _run_cli(self, args, expect_process_success=True, expect_action_success=True):
        del expect_process_success, expect_action_success
        command = list(args)
        if command[:2] == ["tab-list", "--json"]:
            lines = []
            for tab in self.tabs:
                current = "(current) " if tab["active"] else ""
                lines.append(f"- {tab['index']}: {current}[{tab['title']}]({tab['url']})")
            return {"parsed": {"result": "\n".join(lines)}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:1] == ["tab-select"]:
            target_index = int(command[1])
            for tab in self.tabs:
                tab["active"] = int(tab["index"]) == target_index
            return {"parsed": {"ok": True}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:1] == ["click"]:
            self.clicked_targets.append(command[1])
            active = next(tab for tab in self.tabs if tab["active"])
            replacement = self.next_click_page_by_tab.pop(active["tab_id"], None)
            if isinstance(replacement, dict):
                self.page_by_tab[active["tab_id"]].update(replacement)
            return {"parsed": {"ok": True}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:1] == ["eval"]:
            active = next(tab for tab in self.tabs if tab["active"])
            page = self.page_by_tab[active["tab_id"]]
            func_text = command[1]
            if "location.href" in func_text and "document.title" in func_text:
                return {"parsed": {"result": {"url": page["url"], "title": page["title"]}}, "stdout": "", "stderr": "", "returncode": 0}
            if "document.body ? document.body.innerText" in func_text:
                return {"parsed": {"result": page["text"]}, "stdout": "", "stderr": "", "returncode": 0}
            if "document.documentElement ? document.documentElement.outerHTML" in func_text:
                return {"parsed": {"result": page["html"]}, "stdout": "", "stderr": "", "returncode": 0}
            if "document.activeElement" in func_text:
                return {"parsed": {"result": {"tag_name": "body", "text": page["text"], "id": "html-body", "class": "", "value": ""}}, "stdout": "", "stderr": "", "returncode": 0}
            return {"parsed": {"result": None}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:2] == ["console", "--json"]:
            return {"parsed": {"result": "[INFO] ok @ https://example.com:1"}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:2] == ["requests", "--json"]:
            return {"parsed": {"result": "1. [GET] https://example.com => [200]"}, "stdout": "", "stderr": "", "returncode": 0}
        raise AssertionError(f"Unexpected command: {command}")


class PlaywrightCliEngineTests(unittest.TestCase):
    def test_sticky_tab_is_used_when_no_tab_id_is_provided(self):
        session = FakePlaywrightCliSession()
        session._remember_page("tab-000", url="https://studio.youtube.com/comments", title="Community - YouTube Studio")
        result = session.get_page_text()
        self.assertEqual(result["tab_id"], "tab-000")
        self.assertEqual(result["url"], "https://studio.youtube.com/comments")
        self.assertEqual(result["text"], "Studio comments")

    def test_click_reanchors_to_sticky_tab_before_action(self):
        session = FakePlaywrightCliSession()
        session._remember_page("tab-000", url="https://studio.youtube.com/comments", title="Community - YouTube Studio")
        result = session.click("ytcp-chip#chip-1")
        self.assertTrue(result["clicked"])
        self.assertEqual(result["tab_id"], "tab-000")
        self.assertEqual(session.clicked_targets[-1], "ytcp-chip#chip-1")

    def test_page_drift_is_reported_when_same_tab_navigated_elsewhere(self):
        session = FakePlaywrightCliSession()
        session.tabs[0]["active"] = True
        session.tabs[1]["active"] = False
        session._remember_page("tab-000", url="https://studio.youtube.com/comments", title="Community - YouTube Studio")
        session.page_by_tab["tab-000"]["url"] = "https://panel.awoocd.online/vault"
        session.page_by_tab["tab-000"]["title"] = "资源工作台"
        result = session.get_current_url(tab_id="tab-000")
        self.assertTrue(result["page_drift"]["drifted"])
        self.assertEqual(result["page_drift"]["expected_url"], "https://studio.youtube.com/comments")
        self.assertEqual(result["page_drift"]["current_url"], "https://panel.awoocd.online/vault")

    def test_read_only_get_current_url_does_not_overwrite_expected_page(self):
        session = FakePlaywrightCliSession()
        session.tabs[0]["active"] = True
        session.tabs[1]["active"] = False
        session._remember_page("tab-000", url="https://studio.youtube.com/comments?sort=top", title="Community - YouTube Studio")
        session.page_by_tab["tab-000"]["url"] = "https://panel.awoocd.online/vault"
        session.page_by_tab["tab-000"]["title"] = "Panel"
        first = session.get_current_url(tab_id="tab-000")
        second = session.get_current_url(tab_id="tab-000")
        self.assertTrue(first["page_drift"]["drifted"])
        self.assertTrue(second["page_drift"]["drifted"])
        self.assertEqual(second["page_drift"]["expected_url"], "https://studio.youtube.com/comments?sort=top")

    def test_click_promotes_same_page_query_transition_to_expected(self):
        session = FakePlaywrightCliSession()
        session.tabs[0]["active"] = True
        session.tabs[1]["active"] = False
        session._remember_page("tab-000", url="https://studio.youtube.com/comments?sort=top", title="Community - YouTube Studio")
        session.next_click_page_by_tab["tab-000"] = {
            "url": "https://studio.youtube.com/comments?sort=newest",
            "title": "Community - YouTube Studio",
        }
        result = session.click("ytcp-chip#chip-1")
        self.assertTrue(result["clicked"])
        self.assertFalse(result["page_drift"]["drifted"])
        self.assertEqual(result["page_drift"]["expected_url"], "https://studio.youtube.com/comments?sort=newest")


if __name__ == "__main__":
    unittest.main()
