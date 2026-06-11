import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chromium_advanced import mcp_daemon


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


class McpDaemonAuthTests(unittest.TestCase):
    def create_client(self, api_token: str):
        with mock.patch.object(mcp_daemon, "WorkerManager", _FakeWorkerManager):
            app = mcp_daemon.create_daemon_app(
                config_path="dummy.json",
                host="127.0.0.1",
                port=28888,
                path="/mcp",
                transport="streamable-http",
                log_level="info",
                worker_port=28889,
                api_token=api_token,
                idle_timeout_seconds=60,
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

    def test_daemon_status_allows_request_when_token_not_configured(self):
        client = self.create_client("")
        response = client.get("/_daemon/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("worker_running", response.json())


if __name__ == "__main__":
    unittest.main()
