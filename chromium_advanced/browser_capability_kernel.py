from __future__ import annotations

from typing import Any, Dict, Iterable, List

from chromium_advanced.browser_action_registry import DEFAULT_NATIVE_ACTIONS, STANDARD_ACTIONS


def _bool(value: Any) -> bool:
    return bool(value)


def _listify(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def enrich_capability_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(raw or {})
    engine_name = str(payload.get("engine_name", "") or "unknown")
    capability_version = max(3, int(payload.get("capability_version", 2) or 2))
    standard_actions = _listify(payload.get("standard_actions") or STANDARD_ACTIONS)
    native_actions = _listify(payload.get("native_actions") or DEFAULT_NATIVE_ACTIONS.get(engine_name, set()))

    preferred_paths = dict(payload.get("preferred_paths") or {})
    for action_name in native_actions:
        preferred_paths.setdefault(action_name, "native")
    for action_name in standard_actions:
        preferred_paths.setdefault(action_name, "standard")

    payload["capability_version"] = capability_version
    payload["standard_actions"] = standard_actions
    payload["native_actions"] = native_actions
    payload["preferred_paths"] = preferred_paths
    payload["capability_kernel"] = {
        "enabled": True,
        "version": 1,
        "engine_name": engine_name,
        "supports_native_actions": bool(native_actions),
        "native_action_count": len(native_actions),
        "standard_action_count": len(standard_actions),
    }
    capability_groups = dict(payload.get("capabilities") or {})
    capability_groups.setdefault(
        "execution",
        {
            "standard_actions": standard_actions,
            "native_actions": native_actions,
            "preferred_paths": preferred_paths,
        },
    )
    capability_groups.setdefault(
        "native_execution",
        {
            "supported": bool(native_actions),
            "actions": native_actions,
        },
    )
    payload["capabilities"] = capability_groups
    return payload


def supports_native_action(capabilities: Dict[str, Any], action_name: str) -> bool:
    action = str(action_name or "").strip()
    native_actions = capabilities.get("native_actions") or []
    return action in {str(item or "").strip() for item in native_actions}


def preferred_execution_path(capabilities: Dict[str, Any], action_name: str) -> str:
    action = str(action_name or "").strip()
    preferred_paths = dict(capabilities.get("preferred_paths") or {})
    resolved = str(preferred_paths.get(action, "") or "").strip().lower()
    return resolved or "standard"

