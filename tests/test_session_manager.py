import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from chromium_advanced.chromium_profile_lib import (
    clear_profile_occupancy,
    get_lock_path,
    get_mirror_lock_path,
    normalize_config,
    read_lockfile_payload,
    run_keepalive_job,
    write_profile_occupancy,
)
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


class FlakyBrowserSession(FakeBrowserSession):
    def __init__(self, failures_before_success=0, always_raise=False):
        super().__init__()
        self.failures_before_success = int(failures_before_success or 0)
        self.always_raise = bool(always_raise)
        self.call_count = 0

    def get_summary(self):
        self.call_count += 1
        if self.always_raise or self.call_count <= self.failures_before_success:
            raise RuntimeError("temporary summary probe failure")
        return super().get_summary()


class CountingBrowserSession(FakeBrowserSession):
    def __init__(self):
        super().__init__()
        self.call_count = 0

    def get_summary(self):
        self.call_count += 1
        return super().get_summary()


class FakeEngine:
    def __init__(self):
        self.created = []

    def create_session(self, config, profile_name):
        self.created.append((config, profile_name))
        return FakeBrowserSession()


class SessionManagerPerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="session-manager-mirror-")
        self.state_dir = tempfile.mkdtemp(prefix="session-manager-state-")
        self.state_dir_patch = mock.patch.dict(os.environ, {"CHROMIUM_PROFILE_MANAGER_STATE_DIR": self.state_dir}, clear=False)
        self.state_dir_patch.start()
        for profile_name in ("Profile 4", "Profile 6", "Profile 7", "Profile 8", "Profile 9"):
            clear_profile_occupancy(profile_name)
        for lock_path in (get_lock_path(), get_mirror_lock_path()):
            try:
                if lock_path and os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass
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
        for profile_name in ("Profile 4", "Profile 6", "Profile 7", "Profile 8", "Profile 9"):
            clear_profile_occupancy(profile_name)
        for lock_path in (get_lock_path(), get_mirror_lock_path()):
            try:
                if lock_path and os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass
        self.state_dir_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        shutil.rmtree(self.state_dir, ignore_errors=True)

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

    def test_close_session_returns_before_after_session_snapshots(self):
        manager = SessionManager()
        browser_session = FakeBrowserSession()
        record = SessionRecord(
            session_id="session-1",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=mock.Mock(),
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        result = manager.close_session("session-1")

        self.assertTrue(result["closed"])
        self.assertEqual(result["active_session_ids_before"], ["session-1"])
        self.assertEqual(result["active_session_ids_after"], [])

    def test_reclaim_profile_closes_live_session_and_clears_in_memory_state(self):
        manager = SessionManager()
        browser_session = FakeBrowserSession()
        profile_lock = mock.Mock()
        record = SessionRecord(
            session_id="session-reclaim-live",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=profile_lock,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)
        write_profile_occupancy(
            "Profile 4",
            scene_type="automation",
            state="active",
            owner_label="automation session-reclaim-live",
            engine_name="playwright_cli",
            session_id=record.session_id,
            details={"runtime_mode": "live_root"},
            event_source="test",
            owner_pid=os.getpid(),
            heartbeat_timeout_seconds=180,
            reclaimable=True,
        )

        with mock.patch.object(manager, "_load_config", return_value=self.config):
            with mock.patch("chromium_advanced.session_manager.get_chromium_processes_for_profile", return_value=[]):
                result = manager.reclaim_profile("Profile 4", reason="test_reclaim")

        self.assertEqual(1, result["closed_session_count"])
        self.assertEqual(["session-reclaim-live"], result["closed_session_ids"])
        self.assertNotIn("session-reclaim-live", manager._sessions_by_id)
        self.assertEqual([], manager._session_ids_by_profile.get("Profile 4", []))
        self.assertEqual(1, browser_session.close_count)
        profile_lock.release.assert_called_once()
        self.assertEqual({}, manager.get_profile_occupancy("Profile 4"))

    def test_start_session_rolls_back_created_session_when_active_occupancy_write_fails(self):
        engine = FakeEngine()
        manager = SessionManager()
        register_calls = {"count": 0}

        def fail_on_active(*args, **kwargs):
            register_calls["count"] += 1
            if register_calls["count"] >= 2:
                raise PermissionError("registry replace failed")
            return None

        with mock.patch.object(manager, "_load_config", return_value=self.config):
            with mock.patch("chromium_advanced.session_manager.create_browser_engine", return_value=engine):
                with mock.patch.object(manager, "_register_profile_occupancy", side_effect=fail_on_active):
                    with self.assertRaises(PermissionError):
                        manager.start_session("Profile 4", engine_name="playwright_cli", scene_type="automation", owner_label="rollback-test")

        self.assertEqual(manager.list_sessions(), [])
        self.assertEqual(manager.get_profile_status("Profile 4").get("active_session_count"), 0)

    def test_reconcile_stale_mcp_occupancy_clears_dead_pid_and_lock(self):
        manager = SessionManager()
        profile_root = os.path.join(self.temp_dir, "profiles", "UserDataProfile6")
        os.makedirs(profile_root, exist_ok=True)
        lock_path = os.path.join(profile_root, ".profile_runtime.lock")
        now_ts = time.time()
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(f'{{"pid": 70496, "time": "2026-06-12 01:38:34", "updated_at_ts": {now_ts}}}')

        stale_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 6"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
            }
        )

        write_profile_occupancy(
            "Profile 6",
            scene_type="mcp",
            state="active",
            owner_label="mcp session-dead",
            engine_name="playwright_cli",
            session_id="session-dead",
            details={"runtime_mode": "live_root"},
            event_source="test",
            owner_pid=70496,
            reclaimable=False,
        )

        with mock.patch.object(manager, "_load_config", return_value=stale_config):
            with mock.patch("chromium_advanced.session_manager.is_process_alive", return_value=False):
                results = manager.reconcile_stale_profile_occupancy()
                occupancy = manager.get_profile_occupancy("Profile 6")

        matching = [item for item in results if item.get("profile_name") == "Profile 6"]
        self.assertEqual(1, len(matching))
        self.assertEqual("Profile 6", matching[0]["profile_name"])
        self.assertEqual({}, occupancy)
        self.assertFalse(os.path.exists(lock_path))

    def test_reconcile_stale_profile_lock_without_registry(self):
        manager = SessionManager()
        profile_root = os.path.join(self.temp_dir, "profiles", "UserDataProfile7")
        os.makedirs(profile_root, exist_ok=True)
        lock_path = os.path.join(profile_root, ".profile_runtime.lock")
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write('{"pid": 77777, "time": "2026-06-12 01:38:34", "updated_at_ts": 1781199514.849062}')

        stale_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 7"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
            }
        )

        with mock.patch.object(manager, "_load_config", return_value=stale_config):
            with mock.patch("chromium_advanced.session_manager.is_process_alive", return_value=False):
                results = manager.reconcile_stale_profile_occupancy()

        self.assertEqual(1, len(results))
        self.assertEqual("Profile 7", results[0]["profile_name"])
        self.assertEqual("stale_profile_lock", results[0]["reason"])
        self.assertFalse(os.path.exists(lock_path))

    def test_reconcile_does_not_reclaim_live_automation_session_even_if_owner_pid_looks_dead(self):
        manager = SessionManager()
        profile_root = os.path.join(self.temp_dir, "profiles", "UserDataProfile8")
        os.makedirs(profile_root, exist_ok=True)
        lock_path = os.path.join(profile_root, ".profile_runtime.lock")
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write('{"pid": 88888, "time": "2026-06-12 01:38:34", "updated_at_ts": 1781199514.849062}')

        stale_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 8"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
            }
        )

        browser_session = FakeBrowserSession()
        profile_lock = mock.Mock()
        record = SessionRecord(
            session_id="session-live-automation",
            profile_name="Profile 8",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=profile_lock,
            launch_pid=0,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        write_profile_occupancy(
            "Profile 8",
            scene_type="automation",
            state="active",
            owner_label="automation",
            engine_name="playwright_cli",
            session_id="session-live-automation",
            details={"runtime_mode": "live_root"},
            event_source="test",
            owner_pid=99999,
            heartbeat_timeout_seconds=180,
            reclaimable=True,
        )

        with mock.patch.object(manager, "_load_config", return_value=stale_config):
            with mock.patch("chromium_advanced.session_manager.is_process_alive", return_value=False):
                results = manager.reconcile_stale_profile_occupancy()
                occupancy = manager.get_profile_occupancy("Profile 8")

        matching = [item for item in results if item.get("profile_name") == "Profile 8"]
        self.assertEqual([], matching)
        self.assertEqual("session-live-automation", occupancy.get("session_id"))
        self.assertTrue(os.path.exists(lock_path))

    def test_keepalive_skips_profile_when_session_manager_blocks_start(self):
        keepalive_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [
                    {
                        "profile_name": "Profile 4",
                        "keepalive_enabled": True,
                        "keepalive_sites": {"google": True},
                    }
                ],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
                "keepalive": {
                    "headless": False,
                    "page_timeout_seconds": 10,
                    "between_profiles_seconds": 0,
                    "settle_seconds": 0,
                    "site_dwell_seconds": 0,
                    "plugin_dirs": [],
                },
            }
        )
        config_path = os.path.join(self.temp_dir, "keepalive.json")
        import json

        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(keepalive_config, handle, ensure_ascii=False, indent=2)

        blocked_preflight = {
            "allowed": False,
            "reason": "profile already has a reusable session",
        }
        with mock.patch("chromium_advanced.session_manager.SessionManager.can_start_session", return_value=blocked_preflight):
            summary = run_keepalive_job(config_path=config_path, selected_profiles=["Profile 4"], source="manual:test")

        self.assertEqual("skipped", summary["status"])
        self.assertEqual(1, len(summary["profile_results"]))
        result = summary["profile_results"][0]
        self.assertEqual("Profile 4", result["profile_name"])
        self.assertEqual("skipped", result["status"])
        self.assertIn("reusable session", result["message"])
        self.assertEqual(blocked_preflight, result["details"]["preflight"])

    def test_status_paths_do_not_probe_browser_summary(self):
        manager = SessionManager()
        browser_session = CountingBrowserSession()
        record = SessionRecord(
            session_id="session-status-cache",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=mock.Mock(),
            cached_current_url="https://example.com/",
            cached_title="Example",
            cached_alive=True,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        with mock.patch.object(manager, "_load_config", return_value=self.config):
            sessions = manager.list_sessions()
            status = manager.get_server_status()

        self.assertEqual(0, browser_session.call_count)
        self.assertEqual("https://example.com/", sessions[0]["current_url"])
        self.assertEqual("Example", sessions[0]["title"])
        self.assertEqual(1, status["active_session_count"])

    def test_can_start_session_uses_cached_reuse_state_without_browser_probe(self):
        manager = SessionManager()
        browser_session = CountingBrowserSession()
        record = SessionRecord(
            session_id="session-reuse-cache",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=mock.Mock(),
            cached_current_url="https://example.com/",
            cached_title="Example",
            cached_alive=True,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        with mock.patch.object(manager, "_load_config", return_value=self.config):
            preflight = manager.can_start_session("Profile 4", engine_name="playwright_cli")

        self.assertEqual(0, browser_session.call_count)
        self.assertFalse(preflight["allowed"])
        self.assertTrue(preflight["reusable"])
        self.assertEqual("session-reuse-cache", preflight["reusable_session_id"])

    def test_stale_starting_profile_is_pruned_from_status(self):
        manager = SessionManager()
        with manager._lock:
            manager._starting_profiles["Profile 4"] = time.time() - (SessionManager.STARTING_PROFILE_STALE_SECONDS + 5)
        with mock.patch.object(manager, "_load_config", return_value=self.config):
            status = manager.get_server_status()
        self.assertEqual("idle", status["state"])
        self.assertEqual([], status.get("starting_profiles", []))

    def test_non_starting_profile_is_reconciled_out_of_starting_snapshot(self):
        manager = SessionManager()
        with manager._lock:
            manager._starting_profiles["Profile 4"] = time.time()
        with mock.patch.object(manager, "_load_config", return_value=self.config):
            status = manager.get_server_status()
        self.assertEqual("idle", status["state"])
        self.assertEqual([], status.get("starting_profiles", []))

    def test_starting_profiles_fail_safe_clears_orphaned_in_memory_starting_state(self):
        manager = SessionManager()
        with manager._lock:
            manager._starting_profiles["Profile 3"] = time.time()
            manager._starting_profiles["Profile 4"] = time.time()
        empty_registry = {"profiles": {}, "updated_at": ""}
        with mock.patch.object(manager, "_load_config", return_value=self.config), mock.patch.object(
            manager, "_load_occupancy_registry", return_value=empty_registry
        ):
            status = manager.get_runtime_status_snapshot()
        self.assertEqual("idle", status["state"])
        self.assertEqual([], status.get("starting_profiles", []))

    def test_reconcile_reclaims_automation_starting_entry_when_owner_pid_dead_and_lock_missing(self):
        manager = SessionManager()
        dead_pid = 999999
        write_profile_occupancy(
            "Profile 4",
            scene_type="automation",
            state="starting",
            owner_label="stale-start",
            engine_name="playwright_cli",
            session_id="",
            details={"source": "test"},
            owner_pid=dead_pid,
            reclaimable=False,
        )

        with mock.patch.object(manager, "_load_config", return_value=self.config), mock.patch(
            "chromium_advanced.session_manager.is_process_alive", return_value=False
        ):
            results = manager.reconcile_stale_profile_occupancy()

        self.assertTrue(any(item.get("profile_name") == "Profile 4" for item in results))
        self.assertEqual({}, manager.get_profile_occupancy("Profile 4"))

    def test_get_session_touches_profile_lock(self):
        manager = SessionManager()
        profile_root = os.path.join(self.temp_dir, "profiles", "UserDataProfile9")
        os.makedirs(profile_root, exist_ok=True)
        lock_path = os.path.join(profile_root, ".profile_runtime.lock")

        stale_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 9"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
            }
        )

        from chromium_advanced.chromium_profile_lib import SingleRunLock

        profile_lock = SingleRunLock(lock_path)
        self.assertTrue(profile_lock.try_acquire())
        before_payload = read_lockfile_payload(lock_path)
        time.sleep(0.02)

        browser_session = FakeBrowserSession()
        record = SessionRecord(
            session_id="session-touch-lock",
            profile_name="Profile 9",
            engine_name="playwright_cli",
            created_at=1.0,
            last_used_at=1.0,
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=profile_lock,
            launch_pid=0,
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        write_profile_occupancy(
            "Profile 9",
            scene_type="automation",
            state="active",
            owner_label="automation",
            engine_name="playwright_cli",
            session_id="session-touch-lock",
            details={"runtime_mode": "live_root"},
            event_source="test",
            owner_pid=os.getpid(),
            heartbeat_timeout_seconds=180,
            reclaimable=True,
        )

        with mock.patch.object(manager, "_load_config", return_value=stale_config):
            manager.get_session("session-touch-lock", scene_type="automation", owner_label="automation")

        after_payload = read_lockfile_payload(lock_path)
        self.assertGreater(float(after_payload.get("updated_at_ts", 0.0) or 0.0), float(before_payload.get("updated_at_ts", 0.0) or 0.0))
        profile_lock.release()

    def test_reconcile_does_not_reclaim_mcp_occupancy_only_because_live_map_is_empty(self):
        manager = SessionManager()
        profile_root = os.path.join(self.temp_dir, "profiles", "UserDataProfile4")
        os.makedirs(profile_root, exist_ok=True)
        lock_path = os.path.join(profile_root, ".profile_runtime.lock")
        now_ts = time.time()
        with open(lock_path, "w", encoding="utf-8") as handle:
            handle.write(f'{{"pid": 77701, "time": "2026-06-12 04:38:00", "updated_at_ts": {now_ts}}}')

        stale_config = normalize_config(
            {
                "paths": {
                    "user_data_profiles_root": os.path.join(self.temp_dir, "profiles"),
                    "mirror_user_data_root": self.mirror_root,
                },
                "profiles": [{"profile_name": "Profile 4"}],
                "app": {"browser_engine": "playwright_cli", "concurrency_mode": "per_profile_live"},
            }
        )

        write_profile_occupancy(
            "Profile 4",
            scene_type="mcp",
            state="active",
            owner_label="mcp session-ephemeral",
            engine_name="playwright_cli",
            session_id="session-ephemeral",
            details={"runtime_mode": "live_root"},
            event_source="test",
            owner_pid=os.getpid(),
            reclaimable=False,
        )

        with mock.patch.object(manager, "_load_config", return_value=stale_config):
            with mock.patch("chromium_advanced.session_manager.is_process_alive", return_value=True):
                results = manager.reconcile_stale_profile_occupancy()
                occupancy = manager.get_profile_occupancy("Profile 4")

        self.assertEqual([], [item for item in results if item.get("profile_name") == "Profile 4"])
        self.assertEqual("session-ephemeral", occupancy.get("session_id"))

    def test_get_session_tolerates_transient_alive_probe_failures(self):
        manager = SessionManager()
        browser_session = FlakyBrowserSession(failures_before_success=2)
        record = SessionRecord(
            session_id="session-flaky",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=time.time(),
            last_used_at=time.time(),
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=mock.Mock(),
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        session = manager.get_session(record.session_id)
        self.assertIs(session, record)
        session = manager.get_session(record.session_id)
        self.assertIs(session, record)
        session = manager.get_session(record.session_id)
        self.assertIs(session, record)
        self.assertEqual(0, record.alive_probe_failures)

    def test_get_session_removes_session_after_repeated_alive_probe_failures(self):
        manager = SessionManager()
        browser_session = FlakyBrowserSession(always_raise=True)
        record = SessionRecord(
            session_id="session-dead-after-retries",
            profile_name="Profile 4",
            engine_name="playwright_cli",
            created_at=time.time() - (SessionManager.SESSION_ALIVE_PROBE_GRACE_SECONDS + 1),
            last_used_at=time.time(),
            browser_session=browser_session,
            runtime_mode="live_root",
            runtime_root="",
            mirror_generated_at="",
            cleanup_runtime_on_close=True,
            profile_lock=mock.Mock(),
        )
        manager._sessions_by_id[record.session_id] = record
        manager._session_ids_by_profile.setdefault(record.profile_name, []).append(record.session_id)

        manager.get_session(record.session_id)
        manager.get_session(record.session_id)
        with self.assertRaises(RuntimeError):
            manager.get_session(record.session_id)

        self.assertNotIn(record.session_id, manager._sessions_by_id)
