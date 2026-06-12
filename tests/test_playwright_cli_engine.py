import unittest
import json
import os
import re
import shutil
import subprocess
import tempfile
from types import MethodType
from unittest import mock

from chromium_advanced.browser_engines.playwright_cli_engine import (
    PLAYWRIGHT_CLI_BLINK_SENTINEL_ARG,
    PLAYWRIGHT_CLI_TEMP_PREFIX,
    PlaywrightCliEngine,
    PlaywrightCliBrowserSession,
    cleanup_stale_playwright_cli_temp_dirs,
    _normalize_playwright_cli_launch_args,
)
from chromium_advanced.chromium_profile_lib import get_chromium_restore_prompt_suppression_args


class FakePlaywrightCliSession(PlaywrightCliBrowserSession):
    def __init__(self):
        super().__init__(
            cli_path="playwright-cli",
            cli_command=["playwright-cli"],
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
        self.commands = []
        self.goto_timeout_targets = set()

    def _run_cli(self, args, expect_process_success=True, expect_action_success=True, **kwargs):
        del expect_process_success, expect_action_success
        command = list(args)
        self.commands.append(command)
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
        if command[:1] == ["goto"]:
            target_url = command[1]
            active = next(tab for tab in self.tabs if tab["active"])
            active["url"] = target_url
            page = self.page_by_tab[active["tab_id"]]
            page["url"] = target_url
            if target_url in self.goto_timeout_targets:
                raise TimeoutError(f"playwright-cli command timed out after 20s: goto {target_url}")
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
            if "element.scrollIntoView" in func_text and "MouseEvent" in func_text:
                selector_match = re.search(r"document\.querySelector\((.*?)\)", func_text)
                target = ""
                if selector_match:
                    try:
                        target = json.loads(selector_match.group(1))
                    except Exception:
                        target = ""
                if not target and len(command) > 2:
                    target = command[2]
                self.clicked_targets.append(target)
                replacement = self.next_click_page_by_tab.pop(active["tab_id"], None)
                if isinstance(replacement, dict):
                    self.page_by_tab[active["tab_id"]].update(replacement)
                return {"parsed": {"result": {"ok": True, "tag_name": "button", "id": target.strip("#"), "text": "clicked"}}}
            if "element.scrollIntoView" in func_text and "Event('input'" in func_text:
                match = re.search(r"const value\s*=\s*(.*?);", func_text)
                value = ""
                if match:
                    try:
                        value = json.loads(match.group(1))
                    except Exception:
                        value = ""
                return {"parsed": {"result": {"ok": True, "value": value}}, "stdout": "", "stderr": "", "returncode": 0}
            return {"parsed": {"result": None}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:2] == ["console", "--json"]:
            return {"parsed": {"result": "[INFO] ok @ https://example.com:1"}, "stdout": "", "stderr": "", "returncode": 0}
        if command[:2] == ["requests", "--json"]:
            return {"parsed": {"result": "1. [GET] https://example.com => [200]"}, "stdout": "", "stderr": "", "returncode": 0}
        raise AssertionError(f"Unexpected command: {command}")


class PlaywrightCliEngineTests(unittest.TestCase):
    def _create_session_with_mocked_open(self, *, headless, start_minimized=True):
        temp_dir = tempfile.mkdtemp()
        chrome_path = os.path.join(temp_dir, "chrome.exe")
        profile_root = os.path.join(temp_dir, "UserData")
        cli_root = os.path.join(temp_dir, "cli-bin")
        os.makedirs(os.path.join(profile_root, "Profile 1"), exist_ok=True)
        os.makedirs(os.path.join(cli_root, "node_modules", "@playwright", "cli"), exist_ok=True)
        with open(chrome_path, "w", encoding="utf-8"):
            pass
        node_path = os.path.join(cli_root, "node.exe")
        cli_path = os.path.join(cli_root, "playwright-cli")
        cli_cmd_path = os.path.join(cli_root, "playwright-cli.cmd")
        cli_js_path = os.path.join(cli_root, "node_modules", "@playwright", "cli", "playwright-cli.js")
        for path in (node_path, cli_path, cli_cmd_path, cli_js_path):
            with open(path, "w", encoding="utf-8"):
                pass
        config = {
            "paths": {"chromium_dir": chrome_path, "user_data_root": profile_root},
            "mcp": {"headless": bool(headless), "start_minimized": bool(start_minimized)},
            "launch": {
                "start_maximized": False,
                "load_fingerprint_extension": False,
                "extra_args": [],
            },
        }
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        run_patch = mock.patch(
            "chromium_advanced.browser_engines.playwright_cli_engine.subprocess.run",
            return_value=completed,
        )
        which_patch = mock.patch(
            "chromium_advanced.browser_engines.playwright_cli_engine.shutil.which",
            side_effect=lambda name: {
                "playwright-cli": cli_path,
                "playwright-cli.cmd": cli_cmd_path,
                "playwright-cli.CMD": cli_cmd_path,
            }.get(name),
        )
        rmtree_patch = mock.patch("chromium_advanced.browser_engines.playwright_cli_engine.shutil.rmtree")
        with run_patch as run_mock, which_patch, rmtree_patch:
            session = PlaywrightCliEngine().create_session(config, "Profile 1")
        shutil.rmtree(temp_dir, ignore_errors=True)
        config_path = run_mock.call_args.args[0][run_mock.call_args.args[0].index("--config") + 1]
        with open(config_path, "r", encoding="utf-8") as handle:
            cli_config = json.load(handle)
        return session, run_mock.call_args.args[0], cli_config

    def test_cli_prefers_node_js_chain_over_cmd_launcher(self):
        session, command, _cli_config = self._create_session_with_mocked_open(headless=False)
        self.assertTrue(command[0].lower().endswith("node.exe"))
        self.assertTrue(command[1].lower().endswith("playwright-cli.js"))
        self.assertEqual(session.cli_command[:2], command[:2])

    def test_launch_args_block_upstream_automation_controlled_injection(self):
        args = _normalize_playwright_cli_launch_args([
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",
        ])
        self.assertIn(PLAYWRIGHT_CLI_BLINK_SENTINEL_ARG, args)
        self.assertFalse(any("AutomationControlled" in item for item in args))
        self.assertFalse(any(item.startswith("--disable-blink-features") for item in args))

    def test_launch_args_preserve_existing_non_automation_blink_features(self):
        args = _normalize_playwright_cli_launch_args([
            "--disable-blink-features=IdleDetection,AutomationControlled",
        ])
        self.assertEqual(args, ["--disable-blink-features=IdleDetection"])

    def test_headless_open_does_not_force_headed_cli_window(self):
        session, command, _cli_config = self._create_session_with_mocked_open(headless=True)
        self.assertNotIn("--headed", command)
        self.assertEqual(session.engine_name, "playwright_cli")

    def test_headed_open_keeps_explicit_headed_mode(self):
        _session, command, _cli_config = self._create_session_with_mocked_open(headless=False)
        self.assertIn("--headed", command)

    def test_mcp_start_minimized_uses_minimized_chromium_window(self):
        _session, _command, cli_config = self._create_session_with_mocked_open(headless=False, start_minimized=True)
        args = cli_config["browser"]["launchOptions"]["args"]
        self.assertIn("--start-minimized", args)
        self.assertNotIn("--start-maximized", args)
        for item in get_chromium_restore_prompt_suppression_args():
            self.assertIn(item, args)

    def test_mcp_can_opt_out_of_minimized_window(self):
        _session, _command, cli_config = self._create_session_with_mocked_open(headless=False, start_minimized=False)
        args = cli_config["browser"]["launchOptions"]["args"]
        self.assertNotIn("--start-minimized", args)

    def test_close_attempts_owned_process_cleanup(self):
        session = FakePlaywrightCliSession()
        with mock.patch.object(session, "_run_cli", return_value={"parsed": {}}) as run_cli:
            with mock.patch.object(session, "_terminate_owned_processes") as cleanup:
                with mock.patch("chromium_advanced.browser_engines.playwright_cli_engine.shutil.rmtree"):
                    session.close()
        run_cli.assert_called_once()
        cleanup.assert_called_once()

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
        self.assertEqual(result["action_path"], "fast_dom")
        self.assertFalse(any(command[:1] == ["click"] for command in session.commands))

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

    def test_activate_tab_by_index_ignores_sticky_tab_bias(self):
        session = FakePlaywrightCliSession()
        session._sticky_tab_id = "tab-001"
        result = session.activate_tab(index=0)
        self.assertTrue(result["activated"])
        self.assertEqual(result["tab"]["index"], 0)
        self.assertEqual(result["tab_id"], "tab-000")
        self.assertEqual(session._sticky_tab_id, "tab-000")

    def test_diagnose_page_fetches_network_once(self):
        session = FakePlaywrightCliSession()
        result = session.diagnose_page()
        request_calls = [command for command in session.commands if command[:2] == ["requests", "--json"]]
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(result["diagnosis"]["failed_requests"], [])
        self.assertEqual(result["diagnosis"]["diagnostic_errors"], [])

    def test_console_and_network_reuse_single_page_payload(self):
        session = FakePlaywrightCliSession()
        session.get_console_messages(limit=10)
        session.get_network_requests(limit=10)
        eval_page_calls = [
            command
            for command in session.commands
            if command[:1] == ["eval"] and "location.href" in str(command[1])
        ]
        self.assertLessEqual(len(eval_page_calls), 4)

    def test_type_text_uses_fast_dom_path(self):
        session = FakePlaywrightCliSession()
        result = session.type_text("#name", "Alice")
        self.assertTrue(result["typed"])
        self.assertEqual(result["action_path"], "fast_dom")
        self.assertEqual(result["actual_value"], "Alice")
        self.assertFalse(any(command[:1] == ["fill"] for command in session.commands))

    def test_network_and_console_are_classified(self):
        session = FakePlaywrightCliSession()
        console = session.get_console_messages()
        network = session.get_network_requests()
        self.assertEqual(console["messages"][0]["category"], "runtime")
        self.assertIn("summary", console)
        self.assertEqual(network["requests"][0]["category"], "third_party")
        self.assertIn("summary", network)

    def test_get_summary_falls_back_to_tab_list_when_page_probe_temporarily_fails(self):
        session = FakePlaywrightCliSession()
        original_eval_json = session._eval_json

        def flaky_eval(self, func_text, tab_id=""):
            if "location.href" in str(func_text):
                raise RuntimeError("Target page, context or browser has been closed")
            return original_eval_json(func_text, tab_id=tab_id)

        session._eval_json = MethodType(flaky_eval, session)
        summary = session.get_summary()
        self.assertTrue(summary.alive)
        self.assertEqual(summary.current_url, "https://panel.awoocd.online/vault")
        self.assertEqual(summary.title, "Other")

    def test_navigate_recovers_when_cli_goto_times_out_after_page_commit(self):
        session = FakePlaywrightCliSession()
        session.tabs[0]["active"] = True
        session.tabs[1]["active"] = False
        session.goto_timeout_targets.add("https://github.com/")
        session.page_by_tab["tab-000"]["title"] = "GitHub"
        result = session.navigate("https://github.com/", timeout_seconds=20, tab_id="tab-000")
        self.assertEqual(result["url"], "https://github.com/")
        self.assertEqual(result["title"], "GitHub")
        self.assertFalse(result["page_drift"]["drifted"])

    def test_navigate_timeout_still_raises_when_page_never_reaches_target(self):
        session = FakePlaywrightCliSession()
        session.tabs[0]["active"] = True
        session.tabs[1]["active"] = False

        def timeout_without_commit(self, args, expect_process_success=True, expect_action_success=True, **kwargs):
            if list(args)[:1] == ["goto"]:
                raise TimeoutError("playwright-cli command timed out after 20s: goto https://github.com/")
            return FakePlaywrightCliSession._run_cli(self, args, expect_process_success=expect_process_success, expect_action_success=expect_action_success, **kwargs)

        session._run_cli = MethodType(timeout_without_commit, session)
        with self.assertRaises(TimeoutError):
            session.navigate("https://github.com/", timeout_seconds=20, tab_id="tab-000")

    def test_cleanup_stale_playwright_cli_temp_dirs_removes_empty_dirs(self):
        temp_root = tempfile.mkdtemp()
        try:
            stale_dir = os.path.join(temp_root, PLAYWRIGHT_CLI_TEMP_PREFIX + "profile-1-old")
            keep_dir = os.path.join(temp_root, "other-dir")
            os.makedirs(stale_dir, exist_ok=True)
            os.makedirs(keep_dir, exist_ok=True)
            removed = cleanup_stale_playwright_cli_temp_dirs(temp_root=temp_root, retention=0, max_age_seconds=60)
            self.assertIn(stale_dir, removed)
            self.assertFalse(os.path.exists(stale_dir))
            self.assertTrue(os.path.exists(keep_dir))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
