import contextlib
import http.server
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest

from chromium_advanced.browser_engines.factory import create_browser_engine
from chromium_advanced.browser_session_kernel import ManagedBrowserSession
from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config, resolve_chromium_binary


TEST_HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Managed Runtime Test</title>
    <style>
      .hidden { display: none; }
      .ready { color: green; }
    </style>
  </head>
  <body>
    <h1>Managed Runtime Test</h1>
    <label for="name">Name</label>
    <input id="name" name="name" aria-label="Name" />
    <button id="submit" aria-label="Submit">Submit</button>
    <div id="status" class="hidden" aria-label="Status"></div>
    <script>
      document.getElementById('submit').addEventListener('click', () => {
        const value = document.getElementById('name').value || 'empty';
        console.log('submit:' + value);
        setTimeout(() => {
          const status = document.getElementById('status');
          status.className = 'ready';
          status.textContent = 'Submitted: ' + value;
        }, 150);
      });
    </script>
  </body>
</html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class RuntimeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="mcp-runtime-integration-")
        cls.web_root = os.path.join(cls.temp_dir, "web")
        os.makedirs(cls.web_root, exist_ok=True)
        with open(os.path.join(cls.web_root, "index.html"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(TEST_HTML)

        class Handler(QuietHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=cls.web_root, **kwargs)

        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(Exception):
            cls.httpd.shutdown()
        with contextlib.suppress(Exception):
            cls.httpd.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _build_config(self):
        base = normalize_config(load_app_config())
        chromium_binary = resolve_chromium_binary(base.get("paths", {}).get("chromium_dir", ""))
        self.assertTrue(chromium_binary and os.path.exists(chromium_binary), "chromium binary is required for integration tests")

        config = {
            "paths": dict(base.get("paths", {})),
            "launch": dict(base.get("launch", {})),
            "profiles": [],
            "app": {"browser_engine": "playwright_cli"},
            "mcp": {"headless": True},
        }
        runtime_root = tempfile.mkdtemp(prefix="mcp-runtime-profile-")
        profile_name = "Profile 1"
        os.makedirs(os.path.join(runtime_root, profile_name), exist_ok=True)
        config["paths"]["user_data_root"] = runtime_root
        return config, runtime_root, profile_name

    def _run_runtime_flow(self, engine_name: str):
        config, runtime_root, profile_name = self._build_config()
        session = None
        try:
            engine = create_browser_engine(engine_name)
            session = ManagedBrowserSession(engine.create_session(config, profile_name))
            url = f"http://127.0.0.1:{self.port}/index.html"
            nav = session.navigate(url)
            self.assertIn("Managed Runtime Test", nav.get("title", ""))

            caps = session.get_capabilities()
            self.assertEqual(caps["capability_version"], 2)

            snapshot = session.snapshot()
            self.assertGreaterEqual(snapshot.get("ref_count", 0), 1)

            candidates = session.list_candidates(text_filter="Submit", limit=5)
            self.assertGreaterEqual(candidates.get("count", 0), 1)
            submit_ref = candidates["candidates"][0]["ref"]

            typed = session.type_text("#name", "Alice", by="css")
            self.assertTrue(typed.get("typed"))
            self.assertEqual(typed.get("post_action_context", {}).get("action_name"), "type_text")

            verified = session.verify_target_value("#name", "Alice", by="css")
            self.assertTrue(verified.get("verified"))

            clicked = session.click_target(submit_ref)
            self.assertTrue(clicked.get("clicked"))
            self.assertEqual(clicked.get("post_action_context", {}).get("action_name"), "click_target")

            waited = session.wait_for("#status.ready", by="css", timeout_seconds=5, condition="visible")
            self.assertTrue(waited.get("found"))

            page_text = session.get_page_text()
            self.assertIn("Submitted: Alice", page_text.get("text", ""))

            page_html = session.get_page_html()
            self.assertIn("html_summary", page_html)
            self.assertFalse(page_html.get("html_truncated"))

            diagnosis = session.diagnose_page()
            self.assertIn("recent_actions", diagnosis)
            self.assertGreaterEqual(len(diagnosis.get("recent_actions", [])), 3)
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()
            shutil.rmtree(runtime_root, ignore_errors=True)
            time.sleep(1.0)

    def test_playwright_cli_runtime_flow(self):
        self._run_runtime_flow("playwright_cli")


if __name__ == "__main__":
    unittest.main()
