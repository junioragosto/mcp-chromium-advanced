from __future__ import annotations

from typing import Any, Callable, Dict

from chromium_advanced.browser_capability_kernel import preferred_execution_path, supports_native_action


class BrowserActionOrchestrator:
    def __init__(self, browser_session, legacy_execute: Callable[[str, Dict[str, Any]], Any], legacy_supports: Callable[[str], bool]):
        self.browser_session = browser_session
        self._legacy_execute = legacy_execute
        self._legacy_supports = legacy_supports

    def supports(self, action_name: str) -> bool:
        normalized = str(action_name or "").strip()
        if self._legacy_supports(normalized):
            return True
        capabilities = self._get_capabilities()
        if supports_native_action(capabilities, normalized) and callable(getattr(self.browser_session, "execute_native_action", None)):
            return True
        return False

    def execute(self, action_name: str, args: Dict[str, Any] | None = None):
        normalized = str(action_name or "").strip()
        payload = dict(args or {})
        capabilities = self._get_capabilities()
        execution_path = preferred_execution_path(capabilities, normalized)
        native_supported = supports_native_action(capabilities, normalized)
        native_executor = getattr(self.browser_session, "execute_native_action", None)
        if execution_path == "native" and native_supported and callable(native_executor):
            result = native_executor(normalized, payload)
            return self._attach_trace(result, normalized, "native", "native_engine")
        if self._legacy_supports(normalized):
            result = self._legacy_execute(normalized, payload)
            dispatch_mode = "legacy_with_native_fallback" if native_supported else "legacy_standard"
            return self._attach_trace(result, normalized, "standard", dispatch_mode)
        if native_supported and callable(native_executor):
            result = native_executor(normalized, payload)
            return self._attach_trace(result, normalized, "native", "native_engine")
        raise ValueError(f"unsupported automation action: {normalized}")

    def _get_capabilities(self) -> Dict[str, Any]:
        try:
            raw = self.browser_session.get_capabilities()
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _attach_trace(self, result: Any, action_name: str, execution_path: str, dispatch_mode: str):
        if not isinstance(result, dict):
            return result
        result.setdefault(
            "action_pipeline",
            {
                "action_name": action_name,
                "pipeline_version": 2,
                "engine_name": getattr(self.browser_session, "engine_name", ""),
                "execution_path": execution_path,
                "dispatch_mode": dispatch_mode,
            },
        )
        return result
