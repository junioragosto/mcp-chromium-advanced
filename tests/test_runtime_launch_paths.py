import unittest
from unittest import mock

from chromium_advanced.chromium_profile_lib import get_runtime_launch_cwd


class RuntimeLaunchPathTests(unittest.TestCase):
    def test_source_runtime_launch_cwd_uses_project_root(self):
        with mock.patch("chromium_advanced.chromium_profile_lib.get_project_root", return_value="E:\\repo"):
            with mock.patch("chromium_advanced.chromium_profile_lib.sys", frozen=False, executable="C:\\Python\\python.exe"):
                self.assertEqual(get_runtime_launch_cwd("C:\\Desktop\\ChromiumMcpDaemon.exe"), "E:\\repo")

    def test_frozen_runtime_launch_cwd_prefers_executable_parent(self):
        with mock.patch("chromium_advanced.chromium_profile_lib.os.path.isfile", return_value=True):
            with mock.patch("chromium_advanced.chromium_profile_lib.sys", frozen=True, executable="C:\\Desktop\\ChromiumProfileManager.exe"):
                self.assertEqual(
                    get_runtime_launch_cwd("C:\\Desktop\\ChromiumMcpDaemon\\ChromiumMcpDaemon.exe"),
                    "C:\\Desktop\\ChromiumMcpDaemon",
                )

    def test_frozen_runtime_launch_cwd_falls_back_to_gui_executable_parent(self):
        with mock.patch("chromium_advanced.chromium_profile_lib.os.path.isfile", return_value=False):
            with mock.patch("chromium_advanced.chromium_profile_lib.os.path.isdir", return_value=False):
                with mock.patch("chromium_advanced.chromium_profile_lib.sys", frozen=True, executable="C:\\Desktop\\ChromiumProfileManager.exe"):
                    self.assertEqual(get_runtime_launch_cwd(""), "C:\\Desktop")


if __name__ == "__main__":
    unittest.main()
