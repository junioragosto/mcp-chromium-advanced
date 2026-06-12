import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chromium_advanced import mcp_daemon


class _FakeWorkerManager:
    def __init__(self, *args, **kwargs):
        self.status = {"worker_running": False, "active_browser_sessions": 0, "worker_policy": kwargs.get("worker_policy", "sticky")}

    def get_status(self):
        return dict(self.status)

    def ensure_worker_running(self):
        return {"ok": True}

    def stop_worker(self, reason):
        payload = dict(self.status)
        payload["ok"] = True
        payload["reason"] = reason
        payload["last_stop_reason"] = reason
        return payload

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


class _FakeProcess:
    pid = 12345

    def poll(self):
        return None


class _FakeSessionManager:
    def reconcile_stale_profile_occupancy(self):
        return []

    def reap_expired_profile_occupancy(self):
        return []

    def list_profiles(self):
        return []

    def list_recent_occupancy_events(self, limit=50):
        return []

    def get_runtime_status_snapshot(self):
        return {
            "state": "idle",
            "busy": False,
            "accepting_new_sessions": True,
            "active_session_count": 0,
            "active_sessions": [],
            "live_session_count": 0,
            "isolated_session_count": 0,
            "message": "runtime snapshot",
        }

    def list_sessions(self):
        return []


class McpDaemonAuthTests(unittest.TestCase):
    def create_client(self, api_token: str, admin_token: str = "", worker_policy: str = "sticky"):
        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager), mock.patch.object(
            mcp_daemon, "SessionManager", return_value=_FakeSessionManager()
        ):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token=api_token,
                admin_token=admin_token,
                idle_timeout_seconds=60,
                worker_policy=worker_policy,
            )
        return TestClient(app)

    def test_daemon_status_rejects_missing_token_when_configured(self):
        client = self.create_client("secret-token")
        response = client.get("/_daemon/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Missing Authorization header")

    def test_daemon_status_rejects_invalid_bearer_token(self):
        client = self.create_client("secret-token")
        response = client.get("/_daemon/status", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid API token")

    def test_daemon_status_accepts_valid_bearer_token(self):
        client = self.create_client("secret-token")
        response = client.get("/_daemon/status", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("worker_running", response.json())

    def test_management_endpoint_requires_admin_token(self):
        client = self.create_client("secret-token", admin_token="admin-token")
        response = client.post("/_daemon/worker/stop", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Management endpoints require the admin token")

    def test_management_endpoint_accepts_admin_token(self):
        client = self.create_client("secret-token", admin_token="admin-token")
        response = client.post("/_daemon/worker/stop", headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["last_stop_reason"], "api_stop")

    def test_management_endpoint_is_disabled_without_admin_token(self):
        client = self.create_client("secret-token", admin_token="")
        response = client.post("/_daemon/worker/stop", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin token is not configured for management endpoints")

    def test_status_payload_includes_worker_policy(self):
        client = self.create_client("secret-token", admin_token="admin-token", worker_policy="always_on")
        response = client.get("/_daemon/status", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker_policy"], "always_on")

    def test_daemon_status_allows_request_when_token_not_configured(self):
        client = self.create_client("")
        response = client.get("/_daemon/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("worker_running", response.json())

    def test_worker_status_prunes_stale_worker_session_tracking_without_registry_entry(self):
        manager = mcp_daemon.WorkerManager(
            session_manager=_FakeSessionManager(),
            config_path="dummy.json",
            transport="streamable-http",
            public_host="127.0.0.1",
            public_port=28888,
            public_path="/mcp",
            worker_host="127.0.0.1",
            worker_port=28889,
            log_level="info",
            idle_timeout_seconds=60,
            worker_policy="sticky",
        )
        manager._watchdog_stop.set()
        manager._process = _FakeProcess()
        manager._active_browser_session_ids.add("session-stale")
        with mock.patch.object(mcp_daemon, "can_connect", return_value=True), mock.patch.object(
            mcp_daemon, "list_profile_occupancy_entries", return_value={}
        ):
            status = manager.get_status()
        self.assertEqual(status["active_browser_session_count"], 0)
        self.assertEqual(status["worker_browser_session_ids"], [])

    def test_worker_status_keeps_live_worker_session_tracking_when_registry_entry_exists(self):
        manager = mcp_daemon.WorkerManager(
            session_manager=_FakeSessionManager(),
            config_path="dummy.json",
            transport="streamable-http",
            public_host="127.0.0.1",
            public_port=28888,
            public_path="/mcp",
            worker_host="127.0.0.1",
            worker_port=28889,
            log_level="info",
            idle_timeout_seconds=60,
            worker_policy="sticky",
        )
        manager._watchdog_stop.set()
        manager._process = _FakeProcess()
        manager._active_browser_session_ids.add("session-live")
        with mock.patch.object(mcp_daemon, "can_connect", return_value=True), mock.patch.object(
            mcp_daemon,
            "list_profile_occupancy_entries",
            return_value={"Profile 4": {"session_id": "session-live"}},
        ):
            status = manager.get_status()
        self.assertEqual(status["active_browser_session_count"], 1)
        self.assertEqual(status["worker_browser_session_ids"], ["session-live"])


if __name__ == "__main__":
    unittest.main()
