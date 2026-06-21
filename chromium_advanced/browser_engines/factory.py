from __future__ import annotations

from typing import Dict

from chromium_advanced.browser_engines.constants import DEFAULT_BROWSER_ENGINE


def normalize_browser_engine_name(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"patchright", "playwright_patchright"}:
        return "patchright"
    if text in {"selenium_uc", "selenium-uc", "seleniumuc", "uc"}:
        return "selenium_uc"
    if text in {"playwright_cli", "playwright-cli", "playwrightcli"}:
        return "playwright_cli"
    if text in {"official_playwright_mcp", "official-playwright-mcp", "playwright_mcp", "playwright-mcp"}:
        return "official_playwright_mcp"
    return DEFAULT_BROWSER_ENGINE


def create_browser_engine(engine_name: str) -> object:
    normalized = normalize_browser_engine_name(engine_name)
    if normalized == "patchright":
        from chromium_advanced.browser_engines.patchright_engine import PatchrightEngine

        return PatchrightEngine()
    if normalized == "playwright_cli":
        from chromium_advanced.browser_engines.playwright_cli_engine import PlaywrightCliEngine

        return PlaywrightCliEngine()
    if normalized == "official_playwright_mcp":
        from chromium_advanced.browser_engines.official_playwright_mcp_engine import OfficialPlaywrightMcpEngine

        return OfficialPlaywrightMcpEngine()
    from chromium_advanced.browser_engines.selenium_uc_engine import SeleniumUCEngine

    return SeleniumUCEngine()


def resolve_browser_engine_name(config: Dict, explicit_engine_name: str = "") -> str:
    if str(explicit_engine_name or "").strip():
        return normalize_browser_engine_name(explicit_engine_name)
    app = config.get("app", {}) if isinstance(config, dict) else {}
    return normalize_browser_engine_name(app.get("browser_engine", DEFAULT_BROWSER_ENGINE))
