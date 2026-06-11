import asyncio
import unittest
from unittest import mock

from chromium_advanced import mcp_server


class McpCloseStatusContractTests(unittest.TestCase):
    def test_close_profile_session_returns_post_close_server_status(self):
        fake_manager = mock.Mock()
        fake_manager.close_session.return_value = {
            "session_id": "session-1",
            "profile_name": "Profile 1",
            "engine_name": "playwright_cli",
            "runtime_mode": "live_root",
            "closed": True,
            "active_session_ids_before": ["session-1"],
            "active_session_ids_after": [],
        }
        fake_manager.get_server_status.return_value = {
            "state": "idle",
            "active_session_count": 0,
            "message": "server is idle",
        }

        async def _run():
            with mock.patch("chromium_advanced.mcp_server.SessionManager", return_value=fake_manager):
                server = mcp_server.build_server()
                tools = await server.list_tools()
                close_tool = next(tool for tool in tools if tool.name == "close_profile_session")
                return close_tool.fn(session_id="session-1")

        result = asyncio.run(_run())
        self.assertTrue(result["closed"])
        self.assertIn("server_status_after_close", result)
        self.assertEqual(result["server_status_after_close"]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
