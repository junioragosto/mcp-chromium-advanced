from __future__ import annotations

from typing import Dict

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession
from chromium_advanced.chromium_profile_lib import (
    get_profile_directory_path,
    get_profile_user_data_root,
    resolve_chromium_binary,
    resolve_official_playwright_mcp_runtime,
)


class OfficialPlaywrightMcpEngine(BrowserEngine):
    engine_name = "official_playwright_mcp"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        paths = config.get("paths", {})
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        if not chromium_binary:
            raise FileNotFoundError(
                f"chromium browser not found: {paths.get('chromium_dir', '')}"
            )

        user_data_root = get_profile_user_data_root(config, profile_name)
        profile_directory = get_profile_directory_path(config, profile_name)
        runtime = resolve_official_playwright_mcp_runtime(config)
        if not runtime.get("ready"):
            raise RuntimeError(
                "official_playwright_mcp runtime is not bundled yet. "
                "Bundle Node.js and @playwright/mcp into resources/runtime before enabling this engine."
            )

        raise RuntimeError(
            "official_playwright_mcp is wired into engine selection but still disabled for live persistent-profile sessions. "
            "The current official MCP runtime ownership model conflicts with this project's per-profile live-root governance. "
            f"validated_paths=node={runtime.get('node_executable','')} entrypoint={runtime.get('entrypoint','')} "
            f"chromium={chromium_binary} user_data_root={user_data_root} profile_dir={profile_directory}"
        )
