import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from chromium_advanced.chromium_profile_lib import (
    clear_profile_occupancy,
    get_occupancy_registry_path,
    list_profile_occupancy_entries,
    load_profile_occupancy_registry,
    write_profile_occupancy,
)


class ProfileOccupancyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="chromium-advanced-occupancy-state-")
        self.state_dir_patch = mock.patch.dict(os.environ, {"CHROMIUM_PROFILE_MANAGER_STATE_DIR": self.state_dir}, clear=False)
        self.state_dir_patch.start()

    def tearDown(self):
        for name in ("Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 6"):
            clear_profile_occupancy(name)
        self.state_dir_patch.stop()
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def test_parallel_profile_occupancy_writes_keep_all_profiles(self):
        for name in ("Profile 1", "Profile 3", "Profile 4"):
            clear_profile_occupancy(name)

        def worker(profile_name: str, owner_label: str):
            write_profile_occupancy(
                profile_name,
                scene_type="automation",
                state="active",
                owner_label=owner_label,
                engine_name="playwright_cli",
                session_id=f"session-{profile_name.replace(' ', '-').lower()}",
                details={"source": "parallel_test"},
                owner_pid=12345,
                heartbeat_timeout_seconds=180,
                reclaimable=True,
            )

        threads = []
        for profile_name, owner_label in (
            ("Profile 1", "parallel-p1"),
            ("Profile 3", "parallel-p3"),
            ("Profile 4", "parallel-p4"),
        ):
            thread = threading.Thread(target=worker, args=(profile_name, owner_label))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

        payload = load_profile_occupancy_registry()
        entries = list_profile_occupancy_entries()
        self.assertIn("profiles", payload)
        self.assertEqual(set(entries.keys()) & {"Profile 1", "Profile 3", "Profile 4"}, {"Profile 1", "Profile 3", "Profile 4"})
        self.assertEqual(entries["Profile 1"]["owner_label"], "parallel-p1")
        self.assertEqual(entries["Profile 3"]["owner_label"], "parallel-p3")
        self.assertEqual(entries["Profile 4"]["owner_label"], "parallel-p4")
        self.assertTrue(get_occupancy_registry_path().endswith("profile_occupancy_registry.json"))
