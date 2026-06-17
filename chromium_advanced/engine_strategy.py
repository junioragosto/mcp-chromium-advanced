from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from chromium_advanced.browser_engines.factory import resolve_browser_engine_name


@dataclass(frozen=True)
class EngineStrategyDecision:
    requested_engine_name: str
    resolved_engine_name: str
    reason: str
    runtime_profile: str
    used_strategy: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_engine_name": self.requested_engine_name,
            "resolved_engine_name": self.resolved_engine_name,
            "reason": self.reason,
            "runtime_profile": self.runtime_profile,
            "used_strategy": self.used_strategy,
        }


def _runtime_profile_for_engine(engine_name: str) -> str:
    normalized = str(engine_name or "").strip().lower()
    if normalized == "patchright":
        return "primary"
    if normalized == "selenium_uc":
        return "stealth"
    if normalized == "playwright_cli":
        return "lightweight"
    return "balanced"


def resolve_engine_strategy(
    config: Dict[str, Any],
    *,
    explicit_engine_name: str = "",
    action_name: str = "",
    runtime_options: Dict[str, Any] | None = None,
    page_hints: Dict[str, Any] | None = None,
) -> EngineStrategyDecision:
    requested = str(explicit_engine_name or "").strip()
    runtime_options = dict(runtime_options or {})
    page_hints = dict(page_hints or {})

    if requested:
        resolved = resolve_browser_engine_name(config, explicit_engine_name=requested)
        return EngineStrategyDecision(
            requested_engine_name=requested,
            resolved_engine_name=resolved,
            reason="explicit_engine_override",
            runtime_profile=_runtime_profile_for_engine(resolved),
            used_strategy=False,
        )

    gesture_required = bool(runtime_options.get("gesture_required")) or bool(page_hints.get("gesture_required"))
    challenge_sensitive = bool(runtime_options.get("challenge_sensitive")) or bool(page_hints.get("challenge_sensitive"))
    stealth_required = bool(runtime_options.get("stealth_required")) or bool(page_hints.get("stealth_required"))
    prefer_lightweight = bool(runtime_options.get("prefer_lightweight")) or bool(page_hints.get("prefer_lightweight"))
    action_name = str(action_name or "").strip().lower()

    if gesture_required or challenge_sensitive or stealth_required:
        resolved = "selenium_uc"
        reason = "strategy_stealth_or_gesture_path"
    elif prefer_lightweight and action_name in {
        "",
        "get_current_url",
        "get_page_text",
        "get_page_html",
        "list_tabs",
        "get_console_messages",
        "get_network_requests",
        "screenshot",
    }:
        resolved = "playwright_cli"
        reason = "strategy_lightweight_diagnostic_path"
    else:
        resolved = resolve_browser_engine_name(config, explicit_engine_name="")
        reason = "strategy_default_primary_path"

    return EngineStrategyDecision(
        requested_engine_name="",
        resolved_engine_name=resolved,
        reason=reason,
        runtime_profile=_runtime_profile_for_engine(resolved),
        used_strategy=True,
    )
