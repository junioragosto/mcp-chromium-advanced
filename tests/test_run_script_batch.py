import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from chromium_advanced import mcp_daemon, mcp_server


class _FakeBrowserSession:
    def run_script(self, script: str, tab_id: str = "") -> dict:
        if script == "throw":
            return {
                "ok": False,
                "error": "boom",
                "error_type": "RuntimeError",
                "tab_id": tab_id or "tab-000",
                "url": "about:blank",
                "title": "",
            }
        return {
            "tab_id": tab_id or "tab-000",
            "url": "about:blank",
            "title": "",
            "result": script,
        }


class _FakeSessionManager:
    def resolve_session(self, session_id: str, **kwargs):
        return _FakeBrowserSession()


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


class RunScriptBatchTests(unittest.TestCase):
    def test_mcp_server_marks_action_level_failure_when_stop_on_error_false(self):
        fake_manager = _FakeSessionManager()
        async def _run():
            with mock.patch.object(mcp_server, "SessionManager", return_value=fake_manager):
                server = mcp_server.build_server(config_path="dummy.json")
                tools = await server.list_tools()
                tool = next(item for item in tools if item.name == "run_script_batch")
                return tool.fn(
                    session_id="session-1",
                    scripts=["one", "throw", "three"],
                    tab_id="tab-a",
                    stop_on_error=False,
                )

        result = asyncio.run(_run())

        self.assertEqual(3, result["count"])
        self.assertEqual([0, 1, 2], [item["index"] for item in result["items"]])
        self.assertTrue(result["items"][0]["ok"])
        self.assertFalse(result["items"][1]["ok"])
        self.assertEqual("boom", result["items"][1]["error"])
        self.assertTrue(result["items"][2]["ok"])

    def test_daemon_marks_action_level_failure_when_stop_on_error_false(self):
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
        client = TestClient(app)

        response = client.post(
            "/_daemon/automation/action",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "session_id": "session-1",
                "owner_label": "test",
                "action": "run_script_batch",
                "args": {
                    "scripts": ["one", "throw", "three"],
                    "stop_on_error": False,
                    "tab_id": "tab-a",
                },
            },
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        items = payload["result"]["items"]
        self.assertEqual([0, 1, 2], [item["index"] for item in items])
        self.assertTrue(items[0]["ok"])
        self.assertFalse(items[1]["ok"])
        self.assertEqual("boom", items[1]["error"])
        self.assertTrue(items[2]["ok"])


if __name__ == "__main__":
    unittest.main()
