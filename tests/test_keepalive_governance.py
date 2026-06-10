import inspect
import os
import tempfile
import time
import unittest
from unittest import mock

from chromium_advanced import chromium_profile_lib as lib
from chromium_advanced.chromium_manage_gui import ChromiumManagerWindow


class FakeProc:
    def __init__(self, pid, exe, cmdline):
        self.info = {"pid": pid, "name": "chrome.exe", "exe": exe, "cmdline": cmdline}
        self.pid = pid
        self.killed = False

    def children(self, recursive=True):
        return []

    def kill(self):
        self.killed = True


class KeepaliveGovernanceTests(unittest.TestCase):
    def make_config(self):
        return lib.normalize_config(
            {
                "paths": {
                    "chromium_dir": r"C:\Chromium\chrome.exe",
                    "chromedriver_path": r"D:\drivers\chromedriver.exe",
                    "user_data_root": r"C:\Chromium\UserData",
                },
                "profiles": [
                    {
                        "profile_name": "Profile 4",
                        "keepalive_enabled": True,
                        "keepalive_sites": {"google": True},
                    }
                ],
            }
        )

    def test_profile_process_match_uses_user_data_and_profile(self):
        config = self.make_config()
        chrome = config["paths"]["chromium_dir"]
        procs = [
            FakeProc(10, chrome, [chrome, r"--user-data-dir=C:\Chromium\UserData", "--profile-directory=Profile 4"]),
            FakeProc(11, chrome, [chrome, r"--user-data-dir=C:\Chromium\UserData", "--profile-directory=Profile 2"]),
            FakeProc(12, r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["chrome.exe"]),
        ]

        with mock.patch.object(lib.psutil, "process_iter", return_value=procs):
            matches = lib.get_chromium_processes_for_profile(config, "Profile 4")

        self.assertEqual([item["pid"] for item in matches], [10])

    def test_cleanup_only_terminates_new_profile_processes(self):
        config = self.make_config()
        chrome = config["paths"]["chromium_dir"]
        old_proc = FakeProc(10, chrome, [chrome, r"--user-data-dir=C:\Chromium\UserData", "--profile-directory=Profile 4"])
        new_proc = FakeProc(20, chrome, [chrome, r"--user-data-dir=C:\Chromium\UserData", "--profile-directory=Profile 4"])

        with mock.patch.object(lib.psutil, "process_iter", return_value=[old_proc, new_proc]):
            with mock.patch.object(lib.psutil, "Process", side_effect=lambda pid: {10: old_proc, 20: new_proc}[pid]):
                terminated = lib.cleanup_keepalive_profile_processes(config, "Profile 4", before_pids=[10])

        self.assertEqual(terminated, 1)
        self.assertFalse(old_proc.killed)
        self.assertTrue(new_proc.killed)

    def test_browser_closed_error_stops_profile_keepalive(self):
        config = self.make_config()
        fake_driver = mock.Mock()

        with mock.patch.object(lib, "create_driver_for_profile", return_value=fake_driver):
            with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                with mock.patch.dict(
                    lib.BUILTIN_KEEPALIVE_SITE_ACTIONS,
                    {"google": mock.Mock(side_effect=Exception("invalid session id"))},
                ):
                    result = lib.run_profile_keepalive(config, "Profile 4")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["details"]["google"]["status"], "attention")
        fake_driver.quit.assert_called_once()

    def test_manual_single_profile_keepalive_ignores_other_chromium_processes(self):
        config = self.make_config()
        fake_driver = mock.Mock()
        with mock.patch.object(lib, "load_app_config", return_value=config):
            with mock.patch.object(lib, "save_app_config", side_effect=lambda cfg, path: cfg):
                with mock.patch.object(lib, "find_running_chromium_processes", return_value=[{"pid": 999, "name": "chrome.exe"}]):
                    with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                        with mock.patch.object(lib, "create_driver_for_profile", return_value=fake_driver):
                            with mock.patch.object(lib.SingleRunLock, "try_acquire", return_value=True):
                                with mock.patch.object(lib.SingleRunLock, "release", return_value=None):
                                    with mock.patch.dict(
                                        lib.BUILTIN_KEEPALIVE_SITE_ACTIONS,
                                        {"google": mock.Mock(return_value={"status": "success", "message": "ok"})},
                                    ):
                                        result = lib.run_keepalive_job(
                                            config_path="dummy.json",
                                            selected_profiles=["Profile 4"],
                                            source="manual:profile:Profile 4",
                                        )
        self.assertEqual(result["status"], "success")
        fake_driver.quit.assert_called_once()

    def test_manual_single_profile_keepalive_still_skips_when_target_profile_running(self):
        config = self.make_config()
        with mock.patch.object(lib, "load_app_config", return_value=config):
            with mock.patch.object(lib, "save_app_config", side_effect=lambda cfg, path: cfg):
                with mock.patch.object(lib, "find_running_chromium_processes", return_value=[{"pid": 999, "name": "chrome.exe"}]):
                    with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[{"pid": 1234}]):
                        with mock.patch.object(lib.SingleRunLock, "try_acquire", return_value=True):
                            with mock.patch.object(lib.SingleRunLock, "release", return_value=None):
                                result = lib.run_keepalive_job(
                                    config_path="dummy.json",
                                    selected_profiles=["Profile 4"],
                                    source="manual:profile:Profile 4",
                                )
        self.assertEqual(result["status"], "skipped")
        self.assertIn("Profile 4 chromium already running", result["message"])

    def test_gui_startup_does_not_immediately_run_scheduler(self):
        source = inspect.getsource(ChromiumManagerWindow.__init__)
        self.assertNotIn("QTimer.singleShot(0, self.on_scheduler_timer)", source)

    def test_scheduler_does_not_mark_today_before_worker_finishes(self):
        source = inspect.getsource(ChromiumManagerWindow.on_scheduler_timer)
        self.assertNotIn('keepalive["last_scheduled_run_date"] = today_text', source)

    def test_internal_schedule_marks_today_only_after_non_skipped_summary(self):
        source = inspect.getsource(ChromiumManagerWindow.on_keepalive_worker_message)
        self.assertIn('payload or {}).get("source", "")).startswith("internal-schedule")', source)
        self.assertIn('not in {"skipped", "stopped"}', source)

    def test_external_keepalive_plugin_is_discovered_and_executed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_path = os.path.join(temp_dir, "youtube_studio.py")
            with open(plugin_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "def get_plugin():",
                            "    return {",
                            "        'site_id': 'youtube_studio',",
                            "        'display_name': 'YouTube Studio',",
                            "        'home_url': 'https://studio.youtube.com/',",
                            "    }",
                            "",
                            "def keepalive(context):",
                            "    context['log']('checked')",
                            "    return {'status': 'success', 'message': 'plugin ok'}",
                        ]
                    )
                )
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]
            config["profiles"][0]["keepalive_sites"] = {"youtube_studio": True}
            fake_driver = mock.Mock()

            with mock.patch.object(lib, "create_driver_for_profile", return_value=fake_driver):
                with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                    result = lib.run_profile_keepalive(config, "Profile 4")

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["details"]["youtube_studio"]["status"], "success")
            self.assertIn("youtube_studio", lib.get_keepalive_site_registry(config))

    def test_class_based_keepalive_plugin_is_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_path = os.path.join(temp_dir, "youtube_site.py")
            with open(plugin_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "class KeepalivePlugin:",
                            "    metadata = {'site_id': 'youtube_site', 'display_name': 'YouTube Site'}",
                            "",
                            "    def keepalive(self, context):",
                            "        browser = context['browser']",
                            "        results = context['results']",
                            "        context['log'](browser.current_url())",
                            "        return results.success('class plugin ok')",
                        ]
                    )
                )
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]
            config["profiles"][0]["keepalive_sites"] = {"youtube_site": True}
            fake_driver = mock.Mock()
            fake_driver.current_url = "https://example.com/"

            with mock.patch.object(lib, "create_driver_for_profile", return_value=fake_driver):
                with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                    result = lib.run_profile_keepalive(config, "Profile 4")

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["details"]["youtube_site"]["status"], "success")

    def test_plugin_signed_out_result_disables_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_path = os.path.join(temp_dir, "signed_out_site.py")
            with open(plugin_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "def get_plugin():",
                            "    return {'site_id': 'signed_out_site', 'display_name': 'Signed Out Site'}",
                            "",
                            "def keepalive(context):",
                            "    return {'status': 'signed_out', 'message': 'login required'}",
                        ]
                    )
                )
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]
            config["profiles"][0]["keepalive_sites"] = {"signed_out_site": True}

            with mock.patch.object(lib, "create_driver_for_profile", return_value=mock.Mock()):
                with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                    result = lib.run_profile_keepalive(config, "Profile 4")

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["details"]["signed_out_site"]["status"], "signed_out")
            self.assertEqual(result["details"]["signed_out_site"]["signed_in"], False)
            self.assertEqual(result["disabled_sites"], ["signed_out_site"])

    def test_plugin_unexpected_exception_is_failed_not_attention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_path = os.path.join(temp_dir, "broken_site.py")
            with open(plugin_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "def get_plugin():",
                            "    return {'site_id': 'broken_site', 'display_name': 'Broken Site'}",
                            "",
                            "def keepalive(context):",
                            "    raise RuntimeError('plugin bug')",
                        ]
                    )
                )
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]
            config["profiles"][0]["keepalive_sites"] = {"broken_site": True}

            with mock.patch.object(lib, "create_driver_for_profile", return_value=mock.Mock()):
                with mock.patch.object(lib, "get_chromium_processes_for_profile", return_value=[]):
                    result = lib.run_profile_keepalive(config, "Profile 4")

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["details"]["broken_site"]["status"], "failed")
            self.assertIn("plugin bug", result["details"]["broken_site"]["message"])

    def test_broken_plugin_source_is_still_listed_for_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_path = os.path.join(temp_dir, "broken_file.py")
            with open(plugin_path, "w", encoding="utf-8") as handle:
                handle.write("def get_plugin(:\n    pass\n")
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]

            registry = lib.get_keepalive_site_registry(config)
            records = lib.get_keepalive_plugin_records(config)

            self.assertIn("broken_file", registry)
            self.assertTrue(registry["broken_file"]["load_error"])
            self.assertTrue(any(item["site_id"] == "broken_file" for item in records))

    def test_plugin_metadata_discovery_is_cached_until_file_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            counter_path = os.path.join(temp_dir, "counter.txt")
            plugin_path = os.path.join(temp_dir, "cached_site.py")

            def write_plugin(label: str):
                with open(plugin_path, "w", encoding="utf-8") as handle:
                    handle.write(
                        "\n".join(
                            [
                                f"counter_path = {counter_path!r}",
                                "try:",
                                "    with open(counter_path, 'r', encoding='utf-8') as handle:",
                                "        count = int(handle.read() or '0')",
                                "except FileNotFoundError:",
                                "    count = 0",
                                "with open(counter_path, 'w', encoding='utf-8') as handle:",
                                "    handle.write(str(count + 1))",
                                "",
                                "def get_plugin():",
                                f"    return {{'site_id': 'cached_site', 'display_name': {label!r}}}",
                            ]
                        )
                    )

            write_plugin("Cached Site")
            config = self.make_config()
            config["keepalive"]["plugin_dirs"] = [temp_dir]

            first = lib.discover_external_keepalive_site_metadata(config)
            second = lib.discover_external_keepalive_site_metadata(config)
            self.assertEqual(first["cached_site"]["display_name"], "Cached Site")
            self.assertEqual(second["cached_site"]["display_name"], "Cached Site")
            with open(counter_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "1")

            time.sleep(0.01)
            write_plugin("Updated Site")
            updated = lib.discover_external_keepalive_site_metadata(config)
            self.assertEqual(updated["cached_site"]["display_name"], "Updated Site")
            with open(counter_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "2")

    def test_plugin_template_uses_class_contract(self):
        source = lib.build_keepalive_plugin_template("youtube_studio", "YouTube Studio", "https://studio.youtube.com/")
        self.assertIn("class KeepalivePlugin:", source)
        self.assertIn('browser = context["browser"]', source)
        self.assertIn('results = context["results"]', source)

    def test_builtin_plugin_source_falls_back_when_inspect_unavailable(self):
        with mock.patch.object(lib.inspect, "getsource", side_effect=OSError("could not get source code")):
            source = lib.get_keepalive_plugin_source_text("google")
        self.assertIn("class KeepalivePlugin:", source)
        self.assertIn('"site_id": "google"', source)


if __name__ == "__main__":
    unittest.main()
