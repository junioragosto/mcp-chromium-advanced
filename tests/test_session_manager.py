import os
import shutil
import tempfile
import unittest
from unittest import mock

from chromium_advanced.chromium_profile_lib import normalize_config
from chromium_advanced.mirror_manager import MirrorManager
from chromium_advanced.session_manager import SessionManager, SessionRecord


class FakeSummary:
    def __init__(self, alive=True):
        self.current_url = "about:blank"
        self.title = "Fake"
        self.alive = alive


class FakeBrowserSession:
    def __init__(self):
        self.alive = True
        self.close_count = 0

    def get_summary(self):
        return FakeSummary(alive=self.alive)

    def close(self):
        self.close_count += 1
        self.alive = False


class FakeEngine:
    def __init__(self):
        self.created = []

    def create_session(self, config, profile_name):
        self.created.append((config, profile_name))
        return FakeBrowserSession()


class SessionManagerPerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="session-manager-mirror-")
        self.user_data_root = f"{self.temp_dir}/user-data"
        self.mirror_root = f"{self.temp_dir}/temp_user_data"
        os.makedirs(f"{self.user_data_root}/Default", exist_ok=True)
        os.makedirs(f"{self.user_data_root}/Profile 4/Network", exist_ok=True)
        with open(f"{self.user_data_root}/Local State", "w", encoding="utf-8") as handle:
            handle.write("local")
        with open(f"{self.user_data_root}/Default/Preferences", "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(f"{self.user_data_root}/Profile 4/Preferences", "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(f"{self.user_data_root}/Profile 4/Network/Cookies", "w", encoding="utf-8") as handle:
            handle.write("cookie")
        self.config = normalize_config(
            {
                "paths": {
                    "user_data_root": self.user_data_root,
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 4"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "mirror_isolated"},
                "mirror": {"enabled": True, "cleanup_on_session_close": True},
            }
        )
        MirrorManager(self.config).refresh_snapshots(profile_names=["Profile 4"])

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_same_profile_parallel_session_is_blocked(self):
        engine = FakeEngine()
        manager = SessionManager()
        with mock.patch.object(manager, "_load_config", return_value=self.config):
            with mock.patch("chromium_advanced.session_manager.create_browser_engine", return_value=engine):
                first = manager.start_session("Profile 4", engine_name="playwright_cli")
                status = manager.get_server_status()
                preflight = manager.can_start_session("Profile 4", engine_name="playwright_cli")
                with self.assertRaises(RuntimeError):
                    manager.start_session("Profile 4", engine_name="playwright_cli")

        self.assertEqual(first["runtime_mode"], "live_root")
        self.assertEqual(status["state"], "active_sessions")
        self.assertFalse(status["busy"])
        self.assertFalse(preflight["allowed"])
        self.assertFalse(preflight["same_profile_parallel_supported"])
        self.assertEqual(len(manager.list_sessions()), 1)

        close_all = manager.close_all()
        self.assertEqual(close_all["closed_count"], 1)

    def test_mirror_mode_falls_back_to_live_root_when_snapshot_missing(self):
        engine = FakeEngine()
        manager = SessionManager()
        config_without_mirror = normalize_config(self.config)
        shutil.rmtree(self.mirror_root, ignore_errors=True)
        with mock.patch.object(manager, "_load_config", return_value=config_without_mirror):
            with mock.patch("chromium_advanced.session_manager.create_browser_engine", return_value=engine):
                preflight = manager.can_start_session("Profile 4", engine_name="playwright_cli")
                result = manager.start_session("Profile 4", engine_name="playwright_cli")

        self.assertTrue(preflight["allowed"])
        self.assertEqual(preflight["start_mode"], "live_root")
        self.assertEqual(result["runtime_mode"], "live_root")
        manager.close_all()

    def test_dead_session_is_closed_before_purge(self):
        manager = SessionManager()
        browser_session = FakeBrowserSession()
        browser_session.alive = False
        lock = mock.Mock()
        record = SessionRecord(
            session_id="session-dead",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=lock,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        with mock.patch.object(manager, "_load_config", return_value=self.config):
            with self.assertRaises(RuntimeError):
                manager.get_session(record.session_id)

        self.assertEqual(browser_session.close_count, 1)
        lock.release.assert_called_once()
        self.assertNotIn(record.session_id, manager._sessions_by_id)
