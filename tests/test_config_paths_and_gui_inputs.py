import os
import unittest
from unittest import mock

from PyQt5.QtWidgets import QApplication, QSpinBox, QTimeEdit

from chromium_advanced.chromium_manage_gui import ChromiumManagerWindow, FocusWheelSpinBox, FocusWheelTimeEdit
from chromium_advanced.chromium_profile_lib import (
    _google_results_ready,
    format_keepalive_site_status,
    normalize_config,
    resolve_mcp_start_minimized,
)


class ConfigPathMigrationTests(unittest.TestCase):
    def test_mcp_start_minimized_defaults_to_enabled(self):
        normalized = normalize_config({})
        self.assertFalse(normalized["mcp"]["headless"])
        self.assertTrue(normalized["mcp"]["start_minimized"])

    def test_mcp_headless_is_explicit_and_disables_minimized_window(self):
        normalized = normalize_config({"mcp": {"headless": True, "start_minimized": True}})
        self.assertTrue(normalized["mcp"]["headless"])
        self.assertFalse(resolve_mcp_start_minimized(normalized))

    def test_legacy_default_mirror_root_migrates_next_to_user_data_root(self):
        legacy_workspace_root = r"C:\Users\Administrator\.chromium-profile-manager"
        user_data_root = r"D:\softs\chromium\UserData\134.0.6998.177"
        with mock.patch(
            "chromium_advanced.chromium_profile_lib.get_default_workspace_root",
            return_value=legacy_workspace_root,
        ):
            normalized = normalize_config(
                {
                    "paths": {
                        "user_data_root": user_data_root,
                        "mirror_user_data_root": os.path.join(legacy_workspace_root, "temp_user_data"),
                    }
                }
            )

        self.assertEqual(
            normalized["paths"]["mirror_user_data_root"],
            r"D:\softs\chromium\UserData\temp_user_data",
        )

    def test_gui_mcp_trace_path_default_does_not_crash(self):
        window = ChromiumManagerWindow.__new__(ChromiumManagerWindow)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(window.get_mcp_trace_path().endswith("chromium-advanced-mcp-trace.jsonl"))

    def test_gui_mcp_trace_path_honors_environment_override(self):
        window = ChromiumManagerWindow.__new__(ChromiumManagerWindow)
        with mock.patch.dict(os.environ, {"CHROMIUM_ADVANCED_MCP_TRACE_PATH": r"D:\trace\mcp.jsonl"}):
            self.assertEqual(window.get_mcp_trace_path(), r"D:\trace\mcp.jsonl")


class FocusWheelInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_spinbox_ignores_wheel_when_not_focused(self):
        widget = FocusWheelSpinBox()
        event = mock.Mock()

        with mock.patch.object(widget, "hasFocus", return_value=False):
            with mock.patch.object(QSpinBox, "wheelEvent") as super_wheel:
                widget.wheelEvent(event)

        super_wheel.assert_not_called()
        event.ignore.assert_called_once()


class KeepalivePresentationTests(unittest.TestCase):
    def test_keepalive_site_status_formats_translated_summary(self):
        text = format_keepalive_site_status(
            "chatgpt",
            {"status": "signed_out", "message": "ChatGPT is not signed in for this profile."},
            lambda key, fallback="": {
                "site_name_chatgpt": "ChatGPT",
                "keepalive_site_status_signed_out": "已掉线",
            }.get(key, fallback or key),
        )
        self.assertEqual(text, "ChatGPT: 已掉线 - ChatGPT is not signed in for this profile.")

    def test_google_results_ready_accepts_search_results_url(self):
        driver = mock.Mock()
        driver.current_url = "https://www.google.com/search?q=profile+keepalive&hl=en"
        driver.title = "profile keepalive - Google Search"
        driver.find_elements.return_value = []
        self.assertTrue(_google_results_ready(driver, "profile keepalive"))

    def test_spinbox_forwards_wheel_when_focused(self):
        widget = FocusWheelSpinBox()
        event = mock.Mock()

        with mock.patch.object(widget, "hasFocus", return_value=True):
            with mock.patch.object(QSpinBox, "wheelEvent") as super_wheel:
                widget.wheelEvent(event)

        super_wheel.assert_called_once_with(event)
        event.ignore.assert_not_called()

    def test_timeedit_ignores_wheel_when_not_focused(self):
        widget = FocusWheelTimeEdit()
        event = mock.Mock()

        with mock.patch.object(widget, "hasFocus", return_value=False):
            with mock.patch.object(QTimeEdit, "wheelEvent") as super_wheel:
                widget.wheelEvent(event)

        super_wheel.assert_not_called()
        event.ignore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
