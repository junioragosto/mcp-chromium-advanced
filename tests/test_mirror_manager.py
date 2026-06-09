import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from chromium_advanced.chromium_profile_lib import normalize_config
from chromium_advanced.mirror_manager import MirrorManager


class MirrorManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="mirror-manager-test-")
        self.user_data_root = os.path.join(self.temp_dir, "user-data")
        self.mirror_root = os.path.join(self.temp_dir, "temp_user_data")
        os.makedirs(self.user_data_root, exist_ok=True)
        os.makedirs(os.path.join(self.user_data_root, "Default"), exist_ok=True)
        os.makedirs(os.path.join(self.user_data_root, "Profile"), exist_ok=True)
        os.makedirs(os.path.join(self.user_data_root, "Profile 4", "Network"), exist_ok=True)
        os.makedirs(os.path.join(self.user_data_root, "Profile 4", "Cache"), exist_ok=True)
        os.makedirs(os.path.join(self.user_data_root, "Profile 7"), exist_ok=True)
        Path(os.path.join(self.user_data_root, "Local State")).write_text("local-state", encoding="utf-8")
        Path(os.path.join(self.user_data_root, "Default", "Preferences")).write_text("{}", encoding="utf-8")
        Path(os.path.join(self.user_data_root, "Profile 4", "Preferences")).write_text("{}", encoding="utf-8")
        Path(os.path.join(self.user_data_root, "Profile 4", "Network", "Cookies")).write_text("cookie-data", encoding="utf-8")
        Path(os.path.join(self.user_data_root, "Profile 4", "Cache", "temp.bin")).write_text("cache", encoding="utf-8")
        Path(os.path.join(self.user_data_root, "Profile 7", "Preferences")).write_text("other", encoding="utf-8")
        self.config = normalize_config(
            {
                "paths": {
                    "user_data_root": self.user_data_root,
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 4"}],
                "mirror": {"enabled": True},
            }
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_refresh_and_materialize_runtime(self):
        manager = MirrorManager(self.config)
        summary = manager.refresh_snapshots(profile_names=["Profile 4"])
        self.assertEqual(summary["status"], "success")
        self.assertTrue(os.path.exists(manager.root_snapshot_path()))
        self.assertTrue(os.path.exists(manager.profile_snapshot_path("Profile 4")))

        with zipfile.ZipFile(manager.root_snapshot_path(), "r") as archive:
            names = set(archive.namelist())
        self.assertIn("Local State", names)
        self.assertIn("Default/Preferences", names)
        self.assertNotIn("Profile 4/Preferences", names)
        self.assertNotIn("Profile 7/Preferences", names)

        with zipfile.ZipFile(manager.profile_snapshot_path("Profile 4"), "r") as archive:
            names = set(archive.namelist())
        self.assertIn("Preferences", names)
        self.assertIn("Network/Cookies", names)
        self.assertNotIn("Cache/temp.bin", names)

        runtime = manager.materialize_runtime("Profile 4")
        self.assertTrue(os.path.isdir(runtime.runtime_root))
        self.assertTrue(os.path.exists(os.path.join(runtime.runtime_root, "Local State")))
        self.assertTrue(os.path.exists(os.path.join(runtime.runtime_root, "Default", "Preferences")))
        self.assertTrue(os.path.exists(os.path.join(runtime.runtime_profile_dir, "Preferences")))
        self.assertTrue(os.path.exists(os.path.join(runtime.runtime_profile_dir, "Network", "Cookies")))
        self.assertFalse(os.path.exists(os.path.join(runtime.runtime_profile_dir, "Cache", "temp.bin")))

        manager.cleanup_runtime(runtime.runtime_root)
        self.assertFalse(os.path.exists(runtime.runtime_root))

