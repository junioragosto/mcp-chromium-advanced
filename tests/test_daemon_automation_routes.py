import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chromium_advanced import mcp_daemon
from chromium_advanced.browser_engines.base import BrowserSessionSummary


class _FakeAutomationBrowserSession:
    def __init__(self):
        self.current_url = "about:blank"
        self.title = ""

    def get_summary(self):
        return BrowserSessionSummary(current_url=self.current_url, title=self.title, alive=True)

    def get_capabilities(self):
        return {"engine_name": "playwright_cli"}

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = ""):
        self.current_url = str(url or "")
        if "github.com" in self.current_url:
            self.title = "GitHub"
        return {"tab_id": "tab-000", "url": self.current_url, "title": self.title}

    def run_script(self, script: str, tab_id: str = ""):
        script_text = str(script or "")
        if "meta[name=\"user-login\"]" in script_text or "meta[name='user-login']" in script_text:
            return {"tab_id": "tab-000", "url": self.current_url, "title": self.title, "result": "junioragosto"}
        return {"tab_id": "tab-000", "url": self.current_url, "title": self.title, "result": None}


class _SlowAutomationBrowserSession(_FakeAutomationBrowserSession):
    def get_summary(self):
        time.sleep(0.4)
        return super().get_summary()


class _FakeWorkerManager:
    def __init__(self, *args, **kwargs):
        self.status = {"worker_running": False, "active_browser_sessions": 0}

    def get_status(self):
        return dict(self.status)

    def ensure_worker_running(self):
        return {"ok": True}

    def stop_worker(self, reason):
        return {"ok": True, "reason": reason}

    def shutdown(self):
        return None

    def is_worker_running(self):
        return False

    def begin_proxy_request(self):
        return None

    def end_proxy_request(self):
        return None

    def mark_proxy_activity(self):
        return None


class _FakeSessionRecord:
    def __init__(self, session_id: str, profile_name: str, engine_name: str):
        self.session_id = session_id
        self.profile_name = profile_name
        self.engine_name = engine_name
        self.browser_session = _FakeAutomationBrowserSession()


class _FakeSessionManager:
    def __init__(self):
        self.sessions = {}
        self.next_id = 1

    def reconcile_stale_profile_occupancy(self):
        return []

    def reap_expired_profile_occupancy(self):
        return []

    def list_profiles(self):
        return [{"profile_name": "Profile 1"}]

    def list_recent_occupancy_events(self, limit=50):
        return []

    def get_profile_status(self, profile_name: str):
        return {"profile_name": profile_name, "busy_state": "idle", "active_session": False}

    def refresh_profile_lease(self, *args, **kwargs):
        return {"ok": True}

    def start_session(
        self,
        profile_name: str,
        reuse_existing: bool = False,
        engine_name: str = "",
        scene_type: str = "automation",
        owner_label: str = "",
        runtime_options=None,
    ):
        session_id = f"session-{self.next_id:04d}"
        self.next_id += 1
        resolved_engine = str(engine_name or "playwright_cli")
        self.sessions[session_id] = _FakeSessionRecord(session_id, profile_name, resolved_engine)
        return {
            "session_id": session_id,
            "profile_name": profile_name,
            "engine_name": resolved_engine,
            "runtime_mode": "live_root",
            "runtime_root": "",
            "mirror_generated_at": "",
            "reused": False,
        }

    def resolve_session(self, session_id: str, scene_type: str = "automation", owner_label: str = "", refresh_lease: bool = True):
        session = self.sessions.get(str(session_id or ""))
        if not session:
            raise ValueError(f"session not found: {session_id}")
        return session.browser_session

    def close_session(self, session_id: str):
        session = self.sessions.pop(str(session_id or ""), None)
        if not session:
            return {
                "session_id": str(session_id or ""),
                "closed": False,
                "message": "session not found",
                "active_session_ids_before": [],
                "active_session_ids_after": [],
            }
        return {
            "session_id": session.session_id,
            "profile_name": session.profile_name,
            "engine_name": session.engine_name,
            "runtime_mode": "live_root",
            "closed": True,
            "active_session_ids_before": [session.session_id],
            "active_session_ids_after": [],
        }

    def reclaim_profile(self, profile_name: str, reason: str = ""):
        return {
            "profile_name": profile_name,
            "reason": reason,
            "terminated_process_count": 0,
            "cleared": True,
            "occupancy_before": {},
        }

    def list_sessions(self):
        return [
            {
                "session_id": session.session_id,
                "profile_name": session.profile_name,
                "engine_name": session.engine_name,
            }
            for session in self.sessions.values()
        ]

    def get_runtime_status_snapshot(self):
        return {
            "state": "idle",
            "busy": False,
            "accepting_new_sessions": True,
            "active_session_count": len(self.sessions),
            "active_sessions": [
                {
                    "session_id": session.session_id,
                    "profile_name": session.profile_name,
                    "engine_name": session.engine_name,
                    "created_at": 0.0,
                    "last_used_at": 0.0,
                    "runtime_mode": "live_root",
                    "runtime_root": "",
                    "mirror_generated_at": "",
                }
                for session in self.sessions.values()
            ],
            "live_session_count": len(self.sessions),
            "isolated_session_count": 0,
            "message": "runtime snapshot",
        }


class _SlowStartSessionManager(_FakeSessionManager):
    def start_session(
        self,
        profile_name: str,
        reuse_existing: bool = False,
        engine_name: str = "",
        scene_type: str = "automation",
        owner_label: str = "",
        runtime_options=None,
    ):
        time.sleep(0.5)
        return super().start_session(
            profile_name,
            reuse_existing=reuse_existing,
            engine_name=engine_name,
            scene_type=scene_type,
            owner_label=owner_label,
            runtime_options=runtime_options,
        )


class _SlowResolveSessionManager(_FakeSessionManager):
    def resolve_session(self, session_id: str, scene_type: str = "automation", owner_label: str = "", refresh_lease: bool = True):
        time.sleep(0.5)
        return super().resolve_session(
            session_id,
            scene_type=scene_type,
            owner_label=owner_label,
            refresh_lease=refresh_lease,
        )


class DaemonAutomationRouteTests(unittest.TestCase):
    def create_client(self):
        fake_manager = _FakeSessionManager()
        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager), mock.patch.object(
            mcp_daemon, "SessionManager", return_value=fake_manager
        ):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token="secret-token",
                admin_token="admin-token",
                idle_timeout_seconds=60,
                worker_policy="sticky",
            )
        return TestClient(app)

    def test_daemon_automation_happy_path(self):
        client = self.create_client()
        headers = {"Authorization": "Bearer secret-token"}

        acquire = client.post(
            "/_daemon/automation/acquire",
            headers=headers,
            json={
                "profile_name": "Profile 1",
                "engine": "playwright_cli",
                "owner_label": "route-test",
                "runtime_options": {"start_minimized": True},
            },
        )
        self.assertEqual(acquire.status_code, 200)
        acquire_payload = acquire.json()
        session_id = acquire_payload["session_id"]

        get_summary = client.post(
            "/_daemon/automation/action",
            headers=headers,
            json={"session_id": session_id, "owner_label": "route-test", "action": "get_summary", "args": {}},
        )
        self.assertEqual(get_summary.status_code, 200)
        self.assertTrue(get_summary.json()["result"]["value"]["alive"])

        navigate = client.post(
            "/_daemon/automation/action",
            headers=headers,
            json={
                "session_id": session_id,
                "owner_label": "route-test",
                "action": "navigate",
                "args": {"url": "https://github.com/", "wait_for_ready": True, "timeout_seconds": 45},
            },
        )
        self.assertEqual(navigate.status_code, 200)
        self.assertEqual(navigate.json()["result"]["url"], "https://github.com/")
        self.assertEqual(navigate.json()["result"]["title"], "GitHub")

        script = client.post(
            "/_daemon/automation/action",
            headers=headers,
            json={
                "session_id": session_id,
                "owner_label": "route-test",
                "action": "run_script",
                "args": {
                    "script": "() => { const m=document.querySelector('meta[name=\"user-login\"]'); return m ? m.getAttribute('content') : ''; }"
                },
            },
        )
        self.assertEqual(script.status_code, 200)
        self.assertEqual(script.json()["result"]["result"], "junioragosto")

        release = client.post(
            "/_daemon/automation/release",
            headers=headers,
            json={"session_id": session_id, "profile_name": "Profile 1"},
        )
        self.assertEqual(release.status_code, 200)
        self.assertTrue(release.json()["closed"])

    def test_daemon_status_uses_runtime_snapshot(self):
        client = self.create_client()
        headers = {"Authorization": "Bearer secret-token"}

        response = client.get("/_daemon/status", headers=headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("server_status", payload)
        self.assertEqual(payload["server_status"]["state"], "idle")

    def test_daemon_status_remains_responsive_while_action_runs(self):
        fake_manager = _FakeSessionManager()
        record = _FakeSessionRecord("session-slow", "Profile 1", "playwright_cli")
        record.browser_session = _SlowAutomationBrowserSession()
        fake_manager.sessions["session-slow"] = record

        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager), mock.patch.object(
            mcp_daemon, "SessionManager", return_value=fake_manager
        ):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token="secret-token",
                admin_token="admin-token",
                idle_timeout_seconds=60,
                worker_policy="sticky",
            )
        client = TestClient(app)
        headers = {"Authorization": "Bearer secret-token"}
        holder = {}

        def run_action():
            holder["response"] = client.post(
                "/_daemon/automation/action",
                headers=headers,
                json={"session_id": "session-slow", "owner_label": "route-test", "action": "get_summary", "args": {}},
            )

        thread = threading.Thread(target=run_action)
        thread.start()
        time.sleep(0.05)
        status = client.get("/_daemon/status", headers=headers)
        thread.join(timeout=2)

        self.assertEqual(status.status_code, 200)
        self.assertIn("response", holder)
        self.assertEqual(holder["response"].status_code, 200)

    def test_daemon_status_remains_responsive_while_action_resolves_session(self):
        fake_manager = _SlowResolveSessionManager()
        record = _FakeSessionRecord("session-slow-resolve", "Profile 1", "playwright_cli")
        fake_manager.sessions["session-slow-resolve"] = record

        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager), mock.patch.object(
            mcp_daemon, "SessionManager", return_value=fake_manager
        ):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token="secret-token",
                admin_token="admin-token",
                idle_timeout_seconds=60,
                worker_policy="sticky",
            )
        client = TestClient(app)
        headers = {"Authorization": "Bearer secret-token"}
        holder = {}

        def run_action():
            holder["response"] = client.post(
                "/_daemon/automation/action",
                headers=headers,
                json={"session_id": "session-slow-resolve", "owner_label": "route-test", "action": "get_summary", "args": {}},
            )

        thread = threading.Thread(target=run_action)
        thread.start()
        time.sleep(0.05)
        status = client.get("/_daemon/status", headers=headers)
        thread.join(timeout=2)

        self.assertEqual(status.status_code, 200)
        self.assertIn("response", holder)
        self.assertEqual(holder["response"].status_code, 200)

    def test_daemon_status_remains_responsive_while_acquire_runs(self):
        fake_manager = _SlowStartSessionManager()

        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager), mock.patch.object(
            mcp_daemon, "SessionManager", return_value=fake_manager
        ):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token="secret-token",
                admin_token="admin-token",
                idle_timeout_seconds=60,
                worker_policy="sticky",
            )
        client = TestClient(app)
        headers = {"Authorization": "Bearer secret-token"}
        holder = {}

        def run_acquire():
            holder["response"] = client.post(
                "/_daemon/automation/acquire",
                headers=headers,
                json={"profile_name": "Profile 1", "engine": "playwright_cli", "owner_label": "route-test"},
            )

        thread = threading.Thread(target=run_acquire)
        thread.start()
        time.sleep(0.05)
        status = client.get("/_daemon/status", headers=headers)
        thread.join(timeout=2)

        self.assertEqual(status.status_code, 200)
        self.assertIn("response", holder)
        self.assertEqual(holder["response"].status_code, 200)


if __name__ == "__main__":
    unittest.main()
