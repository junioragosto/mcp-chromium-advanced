import inspect
import os
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
                with mock.patch.object(lib, "keepalive_google", side_effect=Exception("invalid session id")):
                    with self.assertRaises(lib.KeepAliveStoppedError):
                        lib.run_profile_keepalive(config, "Profile 4")

        fake_driver.quit.assert_called_once()

    def test_gui_startup_does_not_immediately_run_scheduler(self):
        source = inspect.getsource(ChromiumManagerWindow.__init__)
        self.assertNotIn("QTimer.singleShot(0, self.on_scheduler_timer)", source)


if __name__ == "__main__":
    unittest.main()
