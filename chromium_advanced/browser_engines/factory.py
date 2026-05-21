from __future__ import annotations

from typing import Dict

from chromium_advanced.browser_engines.constants import DEFAULT_BROWSER_ENGINE


def normalize_browser_engine_name(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"patchright", "playwright_patchright"}:
        return "patchright"
    return DEFAULT_BROWSER_ENGINE


def create_browser_engine(engine_name: str) -> object:
    normalized = normalize_browser_engine_name(engine_name)
    if normalized == "patchright":
        from chromium_advanced.browser_engines.patchright_engine import PatchrightEngine

        return PatchrightEngine()
    from chromium_advanced.browser_engines.selenium_uc_engine import SeleniumUCEngine

    return SeleniumUCEngine()


def resolve_browser_engine_name(config: Dict, explicit_engine_name: str = "") -> str:
    if str(explicit_engine_name or "").strip():
        return normalize_browser_engine_name(explicit_engine_name)
    app = config.get("app", {}) if isinstance(config, dict) else {}
    return normalize_browser_engine_name(app.get("browser_engine", DEFAULT_BROWSER_ENGINE))
