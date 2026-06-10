import os
import unittest
from unittest import mock

from PyQt5.QtWidgets import QApplication, QCheckBox, QSpinBox, QTimeEdit

from chromium_advanced.chromium_manage_gui import (
    ChromiumManagerWindow,
    FocusWheelSpinBox,
    FocusWheelTimeEdit,
    ProfileEditDialog,
)
from chromium_advanced.chromium_profile_lib import (
    _google_results_ready,
    format_keepalive_site_status,
    get_keepalive_site_ids,
    get_keepalive_site_registry,
    migrate_keepalive_site_id_references,
    normalize_config,
    normalize_keepalive_site_result_for_display,
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
        legacy_workspace_root = r"C:\Users\Example\.chromium-profile-manager"
        user_data_root = r"C:\Chromium\UserData\134.0.6998.177"
        with mock.patch(
            "chromium_advanced.chromium_profile_lib.get_default_workspace_root",
            return_value=legacy_workspace_root,
        ):
            with mock.patch(
                "chromium_advanced.chromium_profile_lib.ensure_default_bookmarks_template",
                return_value=r"C:\Users\Example\.chromium-profile-manager\bookmarks_template.html",
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
            r"C:\Chromium\UserData\temp_user_data",
        )

    def test_gui_mcp_trace_path_default_does_not_crash(self):
        window = ChromiumManagerWindow.__new__(ChromiumManagerWindow)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(window.get_mcp_trace_path().endswith("chromium-advanced-mcp-trace.jsonl"))

    def test_gui_mcp_trace_path_honors_environment_override(self):
        window = ChromiumManagerWindow.__new__(ChromiumManagerWindow)
        with mock.patch.dict(os.environ, {"CHROMIUM_ADVANCED_MCP_TRACE_PATH": r"C:\Trace\mcp.jsonl"}):
            self.assertEqual(window.get_mcp_trace_path(), r"C:\Trace\mcp.jsonl")


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


class KeepaliveGuiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        window = ChromiumManagerWindow()
        self.addCleanup(window.close)
        return window

    def test_profile_edit_dialog_shows_keepalive_site_checkboxes(self):
        config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "account": "demo@example.com",
                        "keepalive_enabled": True,
                        "keepalive_sites": {"google": True, "github": False},
                    }
                ]
            }
        )
        dialog = ProfileEditDialog(config["profiles"][0], config=config)
        self.addCleanup(dialog.close)

        expected_sites = get_keepalive_site_ids(config)
        self.assertEqual(set(dialog.site_checkboxes.keys()), set(expected_sites))
        self.assertGreater(len(dialog.site_checkboxes), 0)
        self.assertTrue(dialog.site_checkboxes["google"].isChecked())
        self.assertFalse(dialog.site_checkboxes["github"].isChecked())

    def test_profile_site_selector_hides_text_when_icon_exists(self):
        window = self.make_window()
        window.config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "keepalive_sites": {"google": True},
                        "last_keepalive_details": {"google": {"status": "success", "message": "ok"}},
                    }
                ]
            }
        )
        profile = window.config["profiles"][0]
        with mock.patch(
            "chromium_advanced.chromium_manage_gui.get_keepalive_site_icon_path",
            side_effect=lambda site_name, config=None, fetch=False: f"C:/icons/{site_name}.png",
        ):
            widget = window.create_profile_site_selector(profile)
        checkboxes = widget.findChildren(QCheckBox)
        self.assertGreater(len(checkboxes), 0)
        self.assertTrue(all(checkbox.text() == "" for checkbox in checkboxes))

    def test_profile_site_selector_shows_only_enabled_sites(self):
        window = self.make_window()
        window.config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "keepalive_sites": {"google": True, "github": False, "gmail": True},
                        "last_keepalive_details": {"google": {"status": "success", "message": "ok"}},
                    }
                ]
            }
        )
        profile = window.config["profiles"][0]
        widget = window.create_profile_site_selector(profile)
        checkboxes = widget.findChildren(QCheckBox)
        self.assertEqual(len(checkboxes), 2)
        self.assertTrue(all(checkbox.isChecked() for checkbox in checkboxes))

    def test_disabling_profile_site_removes_false_key(self):
        window = self.make_window()
        window.config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "keepalive_sites": {"google": True, "github": True},
                    }
                ]
            }
        )
        with mock.patch(
            "chromium_advanced.chromium_manage_gui.save_app_config",
            side_effect=lambda cfg, path: cfg,
        ):
            window.set_profile_keepalive_site_enabled("Profile 1", "github", False)
        self.assertNotIn("github", window.config["profiles"][0]["keepalive_sites"])

    def test_keepalive_buttons_disable_editing_controls_and_plugin_editor(self):
        window = self.make_window()
        window.plugin_source_editor.setPlainText("demo")
        window.refresh_keepalive_plugin_table()
        window.plugin_table.selectRow(0)
        window.on_keepalive_plugin_selection_changed()

        window.set_keepalive_buttons_enabled(False)

        self.assertFalse(window.btn_add.isEnabled())
        self.assertFalse(window.btn_edit.isEnabled())
        self.assertFalse(window.btn_remove.isEnabled())
        self.assertFalse(window.btn_remove_with_dir.isEnabled())
        self.assertFalse(window.btn_plugin_reload.isEnabled())
        self.assertFalse(window.btn_plugin_new.isEnabled())
        self.assertFalse(window.btn_plugin_open_dir.isEnabled())
        self.assertFalse(window.btn_plugin_save.isEnabled())
        self.assertFalse(window.btn_plugin_delete.isEnabled())
        self.assertTrue(window.plugin_source_editor.isReadOnly())

    def test_unknown_false_site_is_not_exposed_in_registry(self):
        config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "keepalive_sites": {"google": True, "stop": False},
                    }
                ]
            }
        )
        registry = get_keepalive_site_registry(config)
        self.assertNotIn("stop", registry)

    def test_keepalive_site_id_migration_moves_profile_references(self):
        config = normalize_config(
            {
                "profiles": [
                    {
                        "profile_name": "Profile 1",
                        "keepalive_sites": {"infinicloud": True},
                        "last_keepalive_details": {"infinicloud": {"status": "success", "message": "ok"}},
                    }
                ]
            }
        )
        updated, changed = migrate_keepalive_site_id_references(config, "infinicloud", "teracloud_browser")
        self.assertTrue(changed)
        self.assertNotIn("infinicloud", updated["profiles"][0]["keepalive_sites"])
        self.assertTrue(updated["profiles"][0]["keepalive_sites"]["teracloud_browser"])
        self.assertIn("teracloud_browser", updated["profiles"][0]["last_keepalive_details"])


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

    def test_browser_closed_keepalive_failure_displays_as_attention(self):
        info = normalize_keepalive_site_result_for_display(
            {"status": "failed", "message": "invalid session id: session deleted as the browser has closed"}
        )
        self.assertEqual(info["status"], "attention")

        text = format_keepalive_site_status(
            "google",
            {"status": "failed", "message": "invalid session id: session deleted"},
            lambda key, fallback="": {
                "site_name_google": "Google",
                "keepalive_site_status_attention": "需检查",
            }.get(key, fallback or key),
        )
        self.assertTrue(text.startswith("Google: 需检查"))

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
