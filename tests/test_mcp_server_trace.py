import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chromium_advanced import mcp_server


class McpServerTraceTests(unittest.TestCase):
    def test_append_mcp_trace_rotates_large_trace_file(self):
        temp_dir = tempfile.mkdtemp()
        try:
            trace_path = Path(temp_dir) / "trace.jsonl"
            trace_path.write_text("x" * 128, encoding="utf-8")
            with mock.patch.object(mcp_server, "MCP_TRACE_FILE_MAX_BYTES", 64):
                with mock.patch.dict(os.environ, {"CHROMIUM_ADVANCED_MCP_TRACE_PATH": str(trace_path)}):
                    mcp_server._append_mcp_trace({"tool_name": "click", "ok": True})
            self.assertTrue(trace_path.exists())
            self.assertTrue((Path(temp_dir) / "trace.jsonl.1").exists())
            self.assertIn('"tool_name": "click"', trace_path.read_text(encoding="utf-8"))
        finally:
            mcp_server._mcp_tool_traces.clear()
            for item in Path(temp_dir).glob("*"):
                item.unlink(missing_ok=True)
            os.rmdir(temp_dir)


if __name__ == "__main__":
    unittest.main()
