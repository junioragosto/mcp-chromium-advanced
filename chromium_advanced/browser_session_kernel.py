from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from chromium_advanced.browser_engines.base import BrowserSession, BrowserSessionSummary


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
SNAPSHOT_LINE_REF_PATTERN = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
HTML_PREVIEW_LIMIT = 12000
RECENT_ACTION_LIMIT = 40

CAPABILITY_MATRIX = {
    "snapshot": "supports_snapshot",
    "snapshot_refs": "supports_snapshot_refs",
    "target_actions": "supports_target_actions",
    "selector_actions": "supports_selector_actions",
    "highlight": "supports_highlight",
    "coordinates": "supports_coordinates",
    "post_action_context": "supports_post_action_context",
    "tabs": "supports_tabs",
    "console_messages": "supports_console_messages",
    "page_errors": "supports_page_errors",
    "network_requests": "supports_network_requests",
}


@dataclass
class RuntimeCapabilities:
    engine_name: str
    runtime_profile: str
    snapshot: bool
    snapshot_refs: bool
    target_actions: bool
    selector_actions: bool
    highlight: bool
    coordinates: bool
    post_action_context: bool
    tabs: bool
    console_messages: bool
    page_errors: bool
    network_requests: bool

    @classmethod
    def from_legacy(cls, raw: Dict[str, Any]) -> "RuntimeCapabilities":
        engine_name = str(raw.get("engine_name", "") or "unknown")
        runtime_profile = {
            "playwright_cli": "fast",
            "patchright": "diagnostic",
            "selenium_uc": "compatible",
        }.get(engine_name, "balanced")
        return cls(
            engine_name=engine_name,
            runtime_profile=runtime_profile,
            snapshot=bool(raw.get("supports_snapshot")),
            snapshot_refs=bool(raw.get("supports_snapshot_refs")),
            target_actions=bool(raw.get("supports_target_actions")),
            selector_actions=bool(raw.get("supports_selector_actions")),
            highlight=bool(raw.get("supports_highlight")),
            coordinates=bool(raw.get("supports_coordinates")),
            post_action_context=bool(raw.get("supports_post_action_context")),
            tabs=bool(raw.get("supports_tabs")),
            console_messages=bool(raw.get("supports_console_messages")),
            page_errors=bool(raw.get("supports_page_errors")),
            network_requests=bool(raw.get("supports_network_requests")),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "engine_name": self.engine_name,
            "runtime_profile": self.runtime_profile,
            "capability_version": 2,
            "supports_snapshot": self.snapshot,
            "supports_snapshot_refs": self.snapshot_refs,
            "supports_target_actions": self.target_actions,
            "supports_selector_actions": self.selector_actions,
            "supports_highlight": self.highlight,
            "supports_coordinates": self.coordinates,
            "supports_post_action_context": self.post_action_context,
            "supports_tabs": self.tabs,
            "supports_console_messages": self.console_messages,
            "supports_page_errors": self.page_errors,
            "supports_network_requests": self.network_requests,
            "capabilities": {
                "snapshot": {
                    "supported": self.snapshot,
                    "structured": self.snapshot,
                    "refs": self.snapshot_refs,
                },
                "target_actions": {
                    "supported": self.target_actions,
                    "snapshot_refs": self.snapshot_refs,
                },
                "selector_actions": {"supported": self.selector_actions},
                "diagnostics": {
                    "console_messages": self.console_messages,
                    "page_errors": self.page_errors,
                    "network_requests": self.network_requests,
                    "post_action_context": self.post_action_context,
                },
                "tabs": {"supported": self.tabs},
                "coordinates": {"supported": self.coordinates},
                "highlight": {"supported": self.highlight},
            },
            "strategy": {
                "preferred_mode": self.runtime_profile,
                "best_for": {
                    "fast": ["high-frequency actions", "low-token navigation"],
                    "diagnostic": ["structured snapshots", "deep diagnostics"],
                    "compatible": ["broad environment fallback", "legacy resilience"],
                    "balanced": ["general browser automation"],
                }.get(self.runtime_profile, ["general browser automation"]),
            },
        }


class ManagedBrowserSession(BrowserSession):
    def __init__(self, raw_session: BrowserSession):
        self._raw = raw_session
        self._capabilities = RuntimeCapabilities.from_legacy(self._safe_raw_capabilities())
        if callable(getattr(self._raw, "get_interaction_context", None)):
            self._capabilities.post_action_context = True
        self._snapshot_ref_map: Dict[str, Dict[str, Any]] = {}
        self._last_snapshot_text = ""
        self._next_snapshot_ref = 1
        self._recent_actions: list[Dict[str, Any]] = []

    def _safe_raw_capabilities(self) -> Dict[str, Any]:
        try:
            raw = self._raw.get_capabilities()
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _action_meta(self, action_name: str, used_fallback: bool = False) -> Dict[str, Any]:
        return {
            "action_name": action_name,
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "used_fallback": bool(used_fallback),
        }

    def _record_action_trace(self, action_name: str, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        trace = {
            "timestamp": round(time.time(), 3),
            "action_name": str(action_name or ""),
            "ok": payload.get("ok") is not False,
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "used_fallback": bool((payload.get("action_meta") or {}).get("used_fallback")),
            "tab_id": str(payload.get("tab_id", "") or ""),
            "url": str(payload.get("url", "") or ""),
            "title": str(payload.get("title", "") or ""),
            "target": str(payload.get("target", "") or payload.get("selector", "") or ""),
            "error_code": str(payload.get("error_code", "") or ""),
            "error_type": str(payload.get("error_type", "") or ""),
            "recoverable": bool(payload.get("recoverable", False)),
        }
        self._recent_actions.append(trace)
        overflow = len(self._recent_actions) - RECENT_ACTION_LIMIT
        if overflow > 0:
            del self._recent_actions[:overflow]

    def _recent_actions_payload(self, limit: int = 10) -> list[Dict[str, Any]]:
        bounded = max(1, int(limit))
        return [dict(item) for item in self._recent_actions[-bounded:]]

    def _recent_actions_excluding_current(self, action_name: str, limit: int = 10) -> list[Dict[str, Any]]:
        items = [dict(item) for item in self._recent_actions]
        if items and str(items[-1].get("action_name", "") or "") == str(action_name or ""):
            items = items[:-1]
        bounded = max(1, int(limit))
        return items[-bounded:]

    def _build_session_health_snapshot(self) -> Dict[str, Any]:
        try:
            summary = self._raw.get_summary()
            alive = bool(summary.alive)
            current_url = str(summary.current_url or "")
            title = str(summary.title or "")
        except Exception as exc:
            return {
                "alive": False,
                "current_url": "",
                "title": "",
                "engine_name": self._capabilities.engine_name,
                "runtime_profile": self._capabilities.runtime_profile,
                "recent_action_count": len(self._recent_actions),
                "recent_failure_count": len([item for item in self._recent_actions if not item.get("ok")]),
                "last_action_name": str(self._recent_actions[-1].get("action_name", "") or "") if self._recent_actions else "",
                "recovery_hint": "recreate_session",
                "summary_error": str(exc),
            }
        last_failure = next((item for item in reversed(self._recent_actions) if not item.get("ok")), {})
        recovery_hint = "none" if alive else "recreate_session"
        if alive and last_failure:
            error_code = str(last_failure.get("error_code", "") or "")
            if error_code == "timeout":
                recovery_hint = "retry_or_diagnose_page"
            elif error_code in {"target_not_found", "target_not_interactable"}:
                recovery_hint = "refresh_candidates_or_snapshot"
            else:
                recovery_hint = "diagnose_page"
        return {
            "alive": alive,
            "current_url": current_url,
            "title": title,
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "recent_action_count": len(self._recent_actions),
            "recent_failure_count": len([item for item in self._recent_actions if not item.get("ok")]),
            "last_action_name": str(self._recent_actions[-1].get("action_name", "") or "") if self._recent_actions else "",
            "last_failure": dict(last_failure) if last_failure else {},
            "recovery_hint": recovery_hint,
        }

    def _supports_managed_post_action_context(self) -> bool:
        return bool(self._capabilities.post_action_context)

    def _compact_context_element(self, element: Any) -> Dict[str, Any]:
        if not isinstance(element, dict):
            return {}
        allowed = ("tag_name", "text", "id", "name", "class", "aria_label", "role", "value", "href")
        return {key: element.get(key) for key in allowed if key in element and element.get(key) not in {None, ""}}

    def _fallback_interaction_context(self, action_name: str = "inspect", tab_id: str = "") -> Dict[str, Any]:
        normalized_tab_id = str(tab_id or "").strip()
        page: Dict[str, Any] = {}
        tabs: list[Dict[str, Any]] = []
        active_element: Dict[str, Any] = {}
        try:
            page = self._raw.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self._raw.get_current_url()
        except Exception:
            page = {}
        try:
            tabs = list(self._raw.list_tabs().get("tabs", []))
        except Exception:
            tabs = []
        try:
            active_raw = self._raw.get_active_element(tab_id=normalized_tab_id) if normalized_tab_id else self._raw.get_active_element()
            active_element = self._compact_context_element(active_raw.get("element", {}))
        except Exception as exc:
            active_element = {"error": str(exc)}
        active_tab_id = normalized_tab_id or str(page.get("tab_id", "") or "")
        return {
            "action_name": str(action_name or "inspect"),
            "page": page,
            "tabs": tabs,
            "active_tab_id": active_tab_id,
            "active_element": active_element,
            "modal_state": {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []},
            "snapshot": {"unsupported": True, "message": "Structured post-action snapshot is not available in this runtime path."},
            "recent_actions": self._recent_actions_payload(limit=8),
            "session_health": self._build_session_health_snapshot(),
        }

    def _normalize_interaction_context(self, payload: Any, action_name: str = "inspect", tab_id: str = "") -> Dict[str, Any]:
        context = payload if isinstance(payload, dict) else {}
        page = context.get("page", {})
        if not isinstance(page, dict):
            page = {}
        tabs = context.get("tabs", [])
        if not isinstance(tabs, list):
            tabs = []
        modal_state = context.get("modal_state", {})
        if not isinstance(modal_state, dict):
            modal_state = {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []}
        snapshot = context.get("snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        active_element = self._compact_context_element(context.get("active_element", {}))
        if not active_element and isinstance(context.get("active_element"), dict) and context.get("active_element", {}).get("error"):
            active_element = {"error": str(context.get("active_element", {}).get("error"))}
        normalized_page = page or {}
        if not normalized_page:
            try:
                normalized_page = self._raw.get_current_url(tab_id=str(tab_id or "").strip()) if str(tab_id or "").strip() else self._raw.get_current_url()
            except Exception:
                normalized_page = {}
        active_tab_id = str(context.get("active_tab_id", "") or normalized_page.get("tab_id", "") or str(tab_id or "").strip())
        return {
            "action_name": str(context.get("action_name", "") or action_name or "inspect"),
            "page": normalized_page,
            "tabs": tabs,
            "active_tab_id": active_tab_id,
            "active_element": active_element,
            "modal_state": {
                "visible": bool(modal_state.get("visible", False)),
                "count": int(modal_state.get("count", 0) or 0),
                "primary_dialog": modal_state.get("primary_dialog", {}) if isinstance(modal_state.get("primary_dialog", {}), dict) else {},
                "dialogs": modal_state.get("dialogs", []) if isinstance(modal_state.get("dialogs", []), list) else [],
            },
            "snapshot": snapshot or {"unsupported": True, "message": "Structured post-action snapshot is not available in this runtime path."},
            "recent_actions": context.get("recent_actions", self._recent_actions_payload(limit=8))
            if isinstance(context.get("recent_actions", None), list)
            else self._recent_actions_payload(limit=8),
            "session_health": context.get("session_health", self._build_session_health_snapshot())
            if isinstance(context.get("session_health", None), dict)
            else self._build_session_health_snapshot(),
        }

    def _build_post_action_context(self, action_name: str, tab_id: str = "") -> Dict[str, Any]:
        normalized_tab_id = str(tab_id or "").strip()
        if callable(getattr(self._raw, "get_interaction_context", None)):
            try:
                payload = self._raw.get_interaction_context(tab_id=normalized_tab_id) if normalized_tab_id else self._raw.get_interaction_context()
                if isinstance(payload, dict):
                    context = payload.get("interaction_context", payload)
                    normalized = self._normalize_interaction_context(context, action_name=action_name, tab_id=normalized_tab_id)
                    normalized["action_name"] = str(action_name or "inspect")
                    return normalized
            except Exception:
                pass
        return self._fallback_interaction_context(action_name=action_name, tab_id=normalized_tab_id)

    def _should_attach_post_action_context(self, action_name: str) -> bool:
        return action_name in {
            "navigate",
            "open_tab",
            "activate_tab",
            "close_tab",
            "click",
            "click_target",
            "type_text",
            "type_target",
            "type_target_and_verify",
            "press_key",
            "mouse_move_xy",
            "mouse_click_xy",
            "mouse_drag_xy",
        }

    def _attach_post_action_context(self, action_name: str, payload: Dict[str, Any], *, failure: bool = False) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not self._supports_managed_post_action_context():
            return payload
        if "post_action_context" in payload and isinstance(payload.get("post_action_context"), dict) and payload.get("post_action_context"):
            payload["post_action_context"] = self._normalize_interaction_context(
                payload.get("post_action_context", {}),
                action_name=str(payload.get("post_action_context", {}).get("action_name", "") or (f"{action_name}_failed" if failure else action_name)),
                tab_id=str(payload.get("tab_id", "") or ""),
            )
            return payload
        if not self._should_attach_post_action_context(action_name):
            return payload
        if payload.get("ok") is False or failure:
            context_action = f"{action_name}_failed"
        else:
            context_action = action_name
        payload["post_action_context"] = self._build_post_action_context(context_action, tab_id=str(payload.get("tab_id", "") or ""))
        return payload

    def _infer_error_code(self, error_type: str, error_text: str) -> str:
        lowered = str(error_text or "").lower()
        normalized_type = str(error_type or "").strip()
        if normalized_type == "NotImplementedError":
            return "action_not_supported_by_runtime"
        if "not implemented" in lowered or "unsupported" in lowered:
            return "action_not_supported_by_runtime"
        if "not found" in lowered or "no such element" in lowered:
            return "target_not_found"
        if "not visible" in lowered or "not interactable" in lowered or "intercept" in lowered:
            return "target_not_interactable"
        if "timeout" in lowered:
            return "timeout"
        if "session" in lowered and "alive" in lowered:
            return "session_not_alive"
        return "runtime_action_failed"

    def _normalize_failure(self, action_name: str, error: Exception, used_fallback: bool = False) -> Dict[str, Any]:
        error_text = str(error)
        error_type = type(error).__name__
        payload = {
            "ok": False,
            "error": error_text,
            "error_type": error_type,
            "error_code": self._infer_error_code(error_type, error_text),
            "recoverable": error_type in {"NotImplementedError", "ValueError", "TimeoutError"},
            "action_meta": self._action_meta(action_name, used_fallback=used_fallback),
        }
        try:
            payload.update(self._raw.get_current_url())
        except Exception:
            pass
        normalized = self._attach_post_action_context(action_name, payload, failure=True)
        self._record_action_trace(action_name, normalized)
        if isinstance(normalized.get("post_action_context"), dict):
            normalized["post_action_context"]["recent_actions"] = self._recent_actions_payload(limit=8)
            normalized["post_action_context"]["session_health"] = self._build_session_health_snapshot()
        return normalized

    def _normalize_result(self, action_name: str, result: Any, used_fallback: bool = False) -> Dict[str, Any]:
        if not isinstance(result, dict):
            result = {"result": result}
        normalized = dict(result)
        if normalized.get("ok") is False:
            error_text = str(normalized.get("error", "") or "")
            error_type = str(normalized.get("error_type", "") or "")
            normalized.setdefault("error_code", self._infer_error_code(error_type, error_text))
            normalized.setdefault("recoverable", normalized["error_code"] != "runtime_action_failed")
        if action_name == "get_page_html":
            normalized = self._normalize_html_payload(normalized)
        normalized["action_meta"] = self._action_meta(action_name, used_fallback=used_fallback)
        normalized = self._attach_post_action_context(action_name, normalized, failure=normalized.get("ok") is False)
        self._record_action_trace(action_name, normalized)
        if isinstance(normalized.get("post_action_context"), dict):
            normalized["post_action_context"]["recent_actions"] = self._recent_actions_payload(limit=8)
            normalized["post_action_context"]["session_health"] = self._build_session_health_snapshot()
        return normalized

    def _augment_diagnosis_payload(self, action_name: str, payload: Dict[str, Any], *, target: str = "", by: str = "css", text_filter: str = "") -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        interaction_context = payload.get("interaction_context")
        if isinstance(interaction_context, dict):
            payload["interaction_context"] = self._normalize_interaction_context(
                interaction_context,
                action_name=action_name,
                tab_id=str(payload.get("tab_id", "") or ""),
            )
        elif action_name == "diagnose_target":
            payload["interaction_context"] = self._build_post_action_context("diagnose_target", tab_id=str(payload.get("tab_id", "") or ""))
        elif action_name == "diagnose_page":
            payload["interaction_context"] = self._build_post_action_context("diagnose_page", tab_id=str(payload.get("tab_id", "") or ""))
        payload["recent_actions"] = self._recent_actions_excluding_current(action_name, limit=12)
        payload["recent_failures"] = [item for item in self._recent_actions_excluding_current(action_name, limit=12) if not item.get("ok")]
        payload["managed_diagnostics"] = {
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "recent_action_count": len(self._recent_actions),
            "recent_failure_count": len([item for item in self._recent_actions if not item.get("ok")]),
            "target": str(target or ""),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
        }
        payload["session_health"] = self._build_session_health_snapshot()
        return payload

    def _normalize_html_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        html = payload.get("html")
        if not isinstance(html, str):
            return payload
        compact = str(html)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", compact, re.IGNORECASE | re.DOTALL)
        custom_tags = set(re.findall(r"<([a-z][a-z0-9:_-]*-[a-z0-9:_-]*)\b", compact, re.IGNORECASE))
        summary = {
            "length": len(compact),
            "line_count": compact.count("\n") + 1,
            "script_count": len(re.findall(r"<script\\b", compact, re.IGNORECASE)),
            "style_count": len(re.findall(r"<style\\b", compact, re.IGNORECASE)),
            "form_control_count": len(re.findall(r"<(?:input|button|textarea|select)\\b", compact, re.IGNORECASE)),
            "link_count": len(re.findall(r"<a\\b", compact, re.IGNORECASE)),
            "custom_element_count": len(custom_tags),
            "custom_element_preview": sorted(custom_tags)[:20],
            "title": re.sub(r"\\s+", " ", title_match.group(1)).strip() if title_match else str(payload.get("title", "") or ""),
        }
        payload["html_summary"] = summary
        if len(compact) <= HTML_PREVIEW_LIMIT:
            payload["html_length"] = len(compact)
            payload["html_truncated"] = False
            return payload
        preview = compact[:HTML_PREVIEW_LIMIT] + "\n...[truncated by managed runtime]"
        payload["html"] = preview
        payload["html_preview"] = preview
        payload["html_length"] = len(compact)
        payload["html_truncated"] = True
        payload["html_preview_limit"] = HTML_PREVIEW_LIMIT
        return payload

    def _candidate_relevance_score(self, entry: Dict[str, Any], text_filter: str = "") -> int:
        if not isinstance(entry, dict):
            return -1
        score = 0
        if entry.get("visible"):
            score += 30
        if entry.get("enabled", True):
            score += 10
        tag_name = str(entry.get("tag_name", "") or "").lower()
        role = str(entry.get("role", "") or "").lower()
        if tag_name in {"button", "a", "input", "textarea", "select", "summary"}:
            score += 12
        if role in {"button", "link", "textbox", "option", "menuitem", "tab", "combobox", "listbox"}:
            score += 10
        if str(entry.get("aria_haspopup", "") or "").strip():
            score += 16
        if str(entry.get("aria_expanded", "") or "").strip().lower() == "true":
            score += 18
        if role in {"menuitem", "option"}:
            score += 24
        classes = str(entry.get("class", "") or "").lower()
        if any(token in classes for token in ("menu", "dropdown", "popup", "dialog", "sheet", "overlay")):
            score += 10
        if role in {"menuitem", "option", "listbox"} and (
            str(entry.get("aria_haspopup", "") or "").strip()
            or str(entry.get("aria_expanded", "") or "").strip().lower() == "true"
            or any(token in classes for token in ("menu", "dropdown", "popup", "dialog", "sheet", "overlay"))
        ):
            score += 140
        filter_text = str(text_filter or "").strip().lower()
        if not filter_text:
            return score
        haystacks = {
            "text": str(entry.get("text", "") or "").strip(),
            "aria_label": str(entry.get("aria_label", "") or "").strip(),
            "name": str(entry.get("name", "") or "").strip(),
            "id": str(entry.get("id", "") or "").strip(),
            "value": str(entry.get("value", "") or "").strip(),
            "role": role,
            "class": str(entry.get("class", "") or "").strip(),
        }
        for key, raw_value in haystacks.items():
            value = raw_value.lower()
            if not value:
                continue
            if value == filter_text:
                score += 220 if key in {"text", "aria_label"} else 180
            elif value.startswith(filter_text):
                score += 140 if key in {"text", "aria_label"} else 100
            elif filter_text in value:
                score += 90 if key in {"text", "aria_label"} else 60
        return score

    def _is_unsupported_result(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("ok") is False and self._infer_error_code(result.get("error_type", ""), result.get("error", "")) == "action_not_supported_by_runtime":
            return True
        snapshot = result.get("snapshot")
        if isinstance(snapshot, dict) and snapshot.get("unsupported"):
            return True
        return False

    def _dispatch(
        self,
        action_name: str,
        raw_call: Callable[[], Any],
        fallback: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        try:
            raw_result = raw_call()
            if fallback and self._is_unsupported_result(raw_result):
                return self._normalize_result(action_name, fallback(), used_fallback=True)
            return self._normalize_result(action_name, raw_result, used_fallback=False)
        except NotImplementedError:
            if fallback:
                try:
                    return self._normalize_result(action_name, fallback(), used_fallback=True)
                except Exception as exc:
                    return self._normalize_failure(action_name, exc, used_fallback=True)
            raise
        except Exception as exc:
            if fallback and self._infer_error_code(type(exc).__name__, str(exc)) == "action_not_supported_by_runtime":
                try:
                    return self._normalize_result(action_name, fallback(), used_fallback=True)
                except Exception as fallback_exc:
                    return self._normalize_failure(action_name, fallback_exc, used_fallback=True)
            return self._normalize_failure(action_name, exc, used_fallback=False)

    def _run_script_result(self, script: str, tab_id: str = "") -> Any:
        if self._capabilities.engine_name == "playwright_cli" and hasattr(self._raw, "_eval_json"):
            compact_script = " ".join(str(script or "").strip().splitlines())
            func_text = f"() => {{ {compact_script} }}"
            return getattr(self._raw, "_eval_json")(func_text, tab_id=tab_id)
        result = self._raw.run_script(script, tab_id=tab_id)
        if isinstance(result, dict):
            return result.get("result")
        return result

    def _encode_js_string(self, value: str) -> str:
        return json.dumps(str(value or ""))

    def _dom_runtime_helpers_js(self) -> str:
        return """
        const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
        const pickSingle = value => value ? [value] : [];
        const getSearchRoots = () => {
          const roots = [document];
          const stack = [document.documentElement].filter(Boolean);
          while (stack.length) {
            const node = stack.pop();
            if (!node || node.nodeType !== Node.ELEMENT_NODE) continue;
            if (node.shadowRoot) {
              roots.push(node.shadowRoot);
              const shadowChildren = Array.from(node.shadowRoot.children || []);
              for (let i = shadowChildren.length - 1; i >= 0; i -= 1) stack.push(shadowChildren[i]);
            }
            const children = Array.from(node.children || []);
            for (let i = children.length - 1; i >= 0; i -= 1) stack.push(children[i]);
          }
          return roots;
        };
        const dedupeElements = items => {
          const seen = new Set();
          const result = [];
          for (const item of items || []) {
            if (!item || item.nodeType !== Node.ELEMENT_NODE || seen.has(item)) continue;
            seen.add(item);
            result.push(item);
          }
          return result;
        };
        const buildSelectorWithinRoot = (el, root) => {
          if (!el || el.nodeType !== Node.ELEMENT_NODE) return '';
          const segments = [];
          let current = el;
          while (current && current.nodeType === Node.ELEMENT_NODE) {
            let part = (current.tagName || '').toLowerCase();
            if (!part) break;
            if (current.id) {
              part += '#' + CSS.escape(current.id);
              segments.unshift(part);
              break;
            }
            const classList = Array.from(current.classList || []).slice(0, 2);
            if (classList.length) part += classList.map(name => '.' + CSS.escape(name)).join('');
            const parent = current.parentElement;
            const siblingsSource = parent ? Array.from(parent.children || []) : Array.from((root && root.children) || []);
            const sameTag = siblingsSource.filter(child => (child.tagName || '').toLowerCase() === (current.tagName || '').toLowerCase());
            if (sameTag.length > 1) part += ':nth-of-type(' + (sameTag.indexOf(current) + 1) + ')';
            segments.unshift(part);
            if (!parent) break;
            if (root === document && parent === document.documentElement) {
              segments.unshift((parent.tagName || '').toLowerCase());
              break;
            }
            current = parent;
            if (current === root) break;
          }
          return segments.join(' > ');
        };
        const buildDeepSelector = el => {
          if (!el || el.nodeType !== Node.ELEMENT_NODE) return '';
          const segments = [];
          let current = el;
          while (current && current.nodeType === Node.ELEMENT_NODE) {
            const root = current.getRootNode();
            segments.unshift(buildSelectorWithinRoot(current, root));
            if (root instanceof ShadowRoot && root.host) {
              current = root.host;
              continue;
            }
            break;
          }
          return segments.filter(Boolean).join(' >>> ');
        };
        const resolveDeepSelector = selector => {
          const parts = String(selector || '').split(/\\s*>>>\\s*/).map(part => part.trim()).filter(Boolean);
          if (!parts.length) return null;
          let root = document;
          let current = null;
          for (let i = 0; i < parts.length; i += 1) {
            current = root.querySelector(parts[i]);
            if (!current) return null;
            if (i < parts.length - 1) {
              root = current.shadowRoot;
              if (!root) return null;
            }
          }
          return current;
        };
        const queryAllDeep = (selector, by) => {
          const roots = getSearchRoots();
          if (by === 'deep_css') return pickSingle(resolveDeepSelector(selector));
          if (by === 'css') return dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll(selector))));
          if (by === 'id') return dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll('#' + CSS.escape(selector)))));
          if (by === 'name') return dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll('[name=\"' + CSS.escape(selector) + '\"]'))));
          if (by === 'tag') return dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll(selector))));
          if (by === 'class') return dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll('.' + CSS.escape(selector)))));
          if (by === 'xpath') {
            const nodes = [];
            const result = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < result.snapshotLength; i += 1) nodes.push(result.snapshotItem(i));
            return dedupeElements(nodes);
          }
          if (by === 'link_text' || by === 'partial_link_text') {
            const anchors = dedupeElements(roots.flatMap(root => Array.from(root.querySelectorAll('a'))));
            return anchors.filter(el => {
              const text = normalize(el.innerText || el.textContent || '');
              return by === 'link_text' ? text === selector : text.includes(selector);
            });
          }
          return [];
        };
        const describeElement = el => {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          const visible = !!style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
          return {
            tag_name: (el.tagName || '').toLowerCase(),
            text: normalize(el.innerText || el.textContent || ''),
            value: 'value' in el ? String(el.value || '') : '',
            visible,
            enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
            checked: 'checked' in el ? !!el.checked : null,
            id: normalize(el.id || ''),
            name: normalize(el.getAttribute('name') || ''),
            class: normalize(el.getAttribute('class') || ''),
            aria_label: normalize(el.getAttribute('aria-label') || ''),
            aria_expanded: normalize(el.getAttribute('aria-expanded') || ''),
            aria_haspopup: normalize(el.getAttribute('aria-haspopup') || ''),
            role: normalize(el.getAttribute('role') || ''),
            href: normalize(el.getAttribute('href') || ''),
            outer_html: el.outerHTML || '',
            selector: buildSelectorWithinRoot(el, el.getRootNode()),
            deep_selector: buildDeepSelector(el),
            box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
          };
        };
        """

    def _selector_query_js(self, selector: str, by: str) -> str:
        escaped_selector = self._encode_js_string(selector)
        escaped_by = self._encode_js_string(by)
        return f"""
        {self._dom_runtime_helpers_js()}
        const selector = {escaped_selector};
        const by = {escaped_by}.toLowerCase();
        const queryAll = () => queryAllDeep(selector, by);
        """

    def _generic_target_script(self, selector: str, by: str, mode: str, limit: int = 25) -> str:
        query_js = self._selector_query_js(selector, by)
        return f"""
        {query_js}
        const nodes = queryAll();
        if ({self._encode_js_string(mode)} === 'describe') {{
          const el = nodes[0];
          return el ? describeElement(el) : null;
        }}
        return nodes.slice(0, {max(1, int(limit))}).map(describeElement);
        """

    def _cli_simple_query_script(self, selector: str, by: str, include_box: bool = True) -> str:
        escaped_selector = self._encode_js_string(selector)
        escaped_by = self._encode_js_string(by)
        box_code = (
            "const rect = el.getBoundingClientRect();"
            "const box = { x: rect.x, y: rect.y, width: rect.width, height: rect.height };"
        ) if include_box else "const box = null;"
        return f"""
        {self._dom_runtime_helpers_js()}
        const selector = {escaped_selector};
        const by = {escaped_by}.toLowerCase();
        const el = queryAllDeep(selector, by)[0] || null;
        if (!el) return null;
        const style = window.getComputedStyle(el);
        {box_code}
        const visible = !!style && style.visibility !== 'hidden' && style.display !== 'none' && (!box || (box.width > 0 && box.height > 0));
        return {{
          tag_name: (el.tagName || '').toLowerCase(),
          text: normalize(el.innerText || el.textContent || ''),
          value: 'value' in el ? String(el.value || '') : '',
          visible,
          enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
          id: normalize(el.id || ''),
          name: normalize(el.getAttribute('name') || ''),
          class: normalize(el.getAttribute('class') || ''),
          aria_label: normalize(el.getAttribute('aria-label') || ''),
          aria_expanded: normalize(el.getAttribute('aria-expanded') || ''),
          aria_haspopup: normalize(el.getAttribute('aria-haspopup') || ''),
          role: normalize(el.getAttribute('role') || ''),
          href: normalize(el.getAttribute('href') || ''),
          selector: buildSelectorWithinRoot(el, el.getRootNode()),
          deep_selector: buildDeepSelector(el),
          box
        }};
        """

    def _cli_wait_query_script(self, selector: str, by: str) -> str:
        escaped_selector = self._encode_js_string(selector)
        escaped_by = self._encode_js_string(by)
        return f"""
        {self._dom_runtime_helpers_js()}
        const selector = {escaped_selector};
        const by = {escaped_by}.toLowerCase();
        const el = queryAllDeep(selector, by)[0] || null;
        if (!el) return {{ found: false }};
        const details = describeElement(el);
        return {{ found: true, ...details }};
        """

    def _managed_target_action_script(
        self,
        selector: str,
        by: str,
        action: str,
        text: str = "",
        clear_first: bool = True,
        submit: bool = False,
    ) -> str:
        escaped_selector = self._encode_js_string(selector)
        escaped_by = self._encode_js_string(by)
        escaped_action = self._encode_js_string(action)
        escaped_text = self._encode_js_string(text)
        return f"""
        {self._dom_runtime_helpers_js()}
        const selector = {escaped_selector};
        const by = {escaped_by}.toLowerCase();
        const action = {escaped_action};
        const text = {escaped_text};
        const clearFirst = {str(bool(clear_first)).lower()};
        const submit = {str(bool(submit)).lower()};
        const el = queryAllDeep(selector, by)[0] || null;
        if (!el) {{
          return {{ ok: false, error: `Target not found: ${{selector}}`, error_type: 'ValueError' }};
        }}
        if (action === 'click') {{
          el.scrollIntoView({{ block: 'center', inline: 'center' }});
          if (typeof el.click === 'function') el.click();
          else el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, composed: true }}));
          return {{ ok: true, clicked: true, target: selector, by, details: describeElement(el) }};
        }}
        if (action === 'type') {{
          el.scrollIntoView({{ block: 'center', inline: 'center' }});
          if (typeof el.focus === 'function') el.focus();
          const nextValue = clearFirst ? text : String(('value' in el ? el.value : '') || '') + text;
          if ('value' in el) el.value = nextValue;
          else el.textContent = nextValue;
          el.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
          el.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
          if (submit) {{
            el.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true, composed: true }}));
            el.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Enter', bubbles: true, composed: true }}));
            if (el.form && typeof el.form.requestSubmit === 'function') el.form.requestSubmit();
          }}
          return {{ ok: true, typed: true, target: selector, by, value: 'value' in el ? String(el.value || '') : String(el.textContent || ''), details: describeElement(el) }};
        }}
        return {{ ok: false, error: `Unsupported managed action: ${{action}}`, error_type: 'NotImplementedError' }};
        """

    def _resolve_snapshot_ref(self, target: str) -> Dict[str, str]:
        resolved = self._snapshot_ref_map.get(str(target or "").strip())
        if not resolved:
            raise ValueError(f"Snapshot ref not found: {target}")
        return resolved

    def _record_snapshot_refs(self, candidates: list[Dict[str, Any]]) -> None:
        for candidate in candidates:
            ref = str(candidate.get("ref", "") or "").strip()
            selector = str(candidate.get("selector", "") or "").strip()
            deep_selector = str(candidate.get("deep_selector", "") or "").strip()
            if not ref:
                continue
            if deep_selector:
                self._snapshot_ref_map[ref] = {"selector": deep_selector, "by": "deep_css"}
            elif selector:
                self._snapshot_ref_map[ref] = {"selector": selector, "by": "css"}

    def _execute_managed_target_action(
        self,
        action_name: str,
        selector: str,
        by: str,
        *,
        text: str = "",
        clear_first: bool = True,
        submit: bool = False,
    ) -> Dict[str, Any]:
        raw = self._run_script_result(
            self._managed_target_action_script(
                selector,
                by,
                "click" if action_name == "click_target" else "type",
                text=text,
                clear_first=clear_first,
                submit=submit,
            )
        )
        if not isinstance(raw, dict):
            raise RuntimeError(f"Managed target action returned invalid payload for {action_name}")
        if raw.get("ok") is False:
            error_type = str(raw.get("error_type", "") or "RuntimeError")
            error_text = str(raw.get("error", "") or f"Managed target action failed: {action_name}")
            if error_type == "NotImplementedError":
                raise NotImplementedError(error_text)
            raise ValueError(error_text) if error_type == "ValueError" else RuntimeError(error_text)
        details = raw.get("details", {}) if isinstance(raw.get("details"), dict) else {}
        result = {
            **self._raw.get_current_url(),
            **{k: v for k, v in raw.items() if k not in {"ok", "details"}},
            **details,
        }
        if action_name == "type_target_and_verify":
            result["verified"] = True
        return result

    def _parse_snapshot_candidates(self, snapshot_text: str, limit: int = 25) -> list[Dict[str, Any]]:
        candidates: list[Dict[str, Any]] = []
        for raw_line in str(snapshot_text or "").splitlines():
            match = SNAPSHOT_LINE_REF_PATTERN.search(raw_line)
            if not match:
                continue
            ref = match.group(1)
            before_ref = raw_line[: match.start()].strip()
            before_ref = before_ref.lstrip("-").strip()
            tag_name = before_ref.split(" ", 1)[0].strip().lower() if before_ref else "node"
            quoted = re.findall(r'"([^"]+)"', before_ref)
            label = quoted[-1] if quoted else before_ref[len(tag_name) :].strip()
            candidate = {
                "source": "snapshot_text",
                "target": ref,
                "ref": ref,
                "by": "snapshot_ref",
                "visible": True,
                "enabled": True,
                "tag_name": tag_name,
                "text": label,
                "aria_label": label,
                "role": tag_name,
                "selector": ref,
            }
            candidates.append(candidate)
            self._snapshot_ref_map[ref] = {"selector": ref, "by": "snapshot_ref"}
            if len(candidates) >= max(1, int(limit)):
                break
        return candidates

    def _fallback_candidates(self, target: str = "", by: str = "css", text_filter: str = "", limit: int = 25, include_boxes: bool = True, tab_id: str = "") -> Dict[str, Any]:
        if self._capabilities.engine_name == "playwright_cli" and not str(target or "").strip():
            try:
                snapshot_result = self._raw.snapshot(tab_id=tab_id)
                snapshot_text = str(snapshot_result.get("snapshot", "") or "")
                snapshot_candidates = self._parse_snapshot_candidates(snapshot_text, limit=max(1, int(limit) * 2))
                lowered_filter = str(text_filter or "").strip().lower()
                if lowered_filter:
                    snapshot_candidates = [
                        item
                        for item in snapshot_candidates
                        if lowered_filter in " ".join(
                            [str(item.get("text", "") or ""), str(item.get("aria_label", "") or ""), str(item.get("tag_name", "") or "")]
                        ).lower()
                    ]
                current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
                return {
                    **current,
                    "target": "",
                    "text_filter": str(text_filter or ""),
                    "count": len(snapshot_candidates[: max(1, int(limit))]),
                    "candidates": snapshot_candidates[: max(1, int(limit))],
                }
            except Exception:
                pass
        selector = target or "a,button,input,textarea,select,summary,[role],[aria-label],[title]"
        mode = "list"
        raw = self._run_script_result(self._generic_target_script(selector, by if target else "css", mode, limit=max(25, int(limit) * 4)), tab_id=tab_id)
        entries = raw if isinstance(raw, list) else []
        lowered_filter = str(text_filter or "").strip().lower()
        ranked_entries = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            merged = " ".join(
                [
                    str(entry.get("text", "") or ""),
                    str(entry.get("aria_label", "") or ""),
                    str(entry.get("role", "") or ""),
                    str(entry.get("name", "") or ""),
                    str(entry.get("value", "") or ""),
                ]
            ).strip()
            if lowered_filter and lowered_filter not in merged.lower():
                continue
            ranked_entries.append((self._candidate_relevance_score(entry, text_filter=lowered_filter), index, entry))
        ranked_entries.sort(key=lambda item: (-item[0], item[1]))
        candidates = []
        for score, _, entry in ranked_entries:
            ref = f"e{self._next_snapshot_ref}"
            self._next_snapshot_ref += 1
            deep_selector = str(entry.get("deep_selector", "") or "").strip()
            candidate = {
                "source": "dom_fallback",
                "target": ref,
                "ref": ref,
                "by": "deep_css" if deep_selector else "css",
                "visible": bool(entry.get("visible")),
                "enabled": bool(entry.get("enabled", True)),
                "match_score": score,
                **entry,
            }
            if not include_boxes:
                candidate.pop("box", None)
            candidates.append(candidate)
            if len(candidates) >= max(1, int(limit)):
                break
        self._record_snapshot_refs(candidates)
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        return {
            **current,
            "target": str(target or ""),
            "text_filter": str(text_filter or ""),
            "count": len(candidates),
            "candidates": candidates,
        }

    def _fallback_describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict[str, Any]:
        resolved_target = self._resolve_snapshot_ref(target) if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()) else {"selector": target, "by": by}
        if self._capabilities.engine_name == "playwright_cli" and resolved_target.get("by") != "snapshot_ref" and hasattr(self._raw, "_eval_json"):
            compact_script = " ".join(
                self._cli_simple_query_script(resolved_target["selector"], resolved_target["by"], include_box=include_box).strip().splitlines()
            )
            entry = getattr(
                self._raw,
                "_eval_json",
            )(f"() => {{ {compact_script} }}")
        else:
            entry = self._run_script_result(
                self._generic_target_script(resolved_target["selector"], resolved_target["by"], "describe", limit=1)
            )
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"Target not found: {target}")
        result = {
            **self._raw.get_current_url(),
            "target": str(target or "").strip(),
            "visible": bool(entry.get("visible")),
            "enabled": bool(entry.get("enabled", True)),
            **entry,
        }
        if not include_box:
            result.pop("box", None)
        return result

    def _fallback_wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict[str, Any]:
        if self._capabilities.engine_name == "playwright_cli" and hasattr(self._raw, "_eval_json"):
            script = " ".join(self._cli_wait_query_script(selector, by).strip().splitlines())
            deadline = time.time() + max(1, int(timeout_seconds))
            last_entry: Dict[str, Any] | None = None
            while time.time() < deadline:
                entry = getattr(self._raw, "_eval_json")(f"() => {{ {script} }}")
                if isinstance(entry, dict) and entry.get("found"):
                    last_entry = entry
                    if condition == "present":
                        break
                    if condition == "visible" and entry.get("visible"):
                        break
                    if condition == "clickable" and entry.get("visible") and entry.get("enabled", True):
                        break
                time.sleep(0.2)
            else:
                raise TimeoutError(f"Timed out waiting for selector: {selector}")
            return {
                **self._raw.get_current_url(),
                "found": True,
                "tag_name": str(last_entry.get("tag_name", "") or ""),
                "text": str(last_entry.get("text", "") or ""),
                "condition": str(condition or "visible"),
            }
        deadline = time.time() + max(1, int(timeout_seconds))
        last_entry: Dict[str, Any] | None = None
        while time.time() < deadline:
            entry = self._run_script_result(self._generic_target_script(selector, by, "describe", limit=1))
            if isinstance(entry, dict) and entry:
                last_entry = entry
                if condition == "present":
                    break
                if condition == "visible" and entry.get("visible"):
                    break
                if condition == "clickable" and entry.get("visible") and entry.get("enabled", True):
                    break
            time.sleep(0.2)
        else:
            raise TimeoutError(f"Timed out waiting for selector: {selector}")
        return {
            **self._raw.get_current_url(),
            "found": True,
            "tag_name": str(last_entry.get("tag_name", "") or ""),
            "text": str(last_entry.get("text", "") or ""),
            "condition": str(condition or "visible"),
        }

    def _fallback_snapshot(self, target: str = "", by: str = "css", depth: int | None = None, boxes: bool = False, filename: str = "", tab_id: str = "") -> Dict[str, Any]:
        del filename
        candidates_payload = self._fallback_candidates(target=target, by=by, limit=30, include_boxes=boxes, tab_id=tab_id)
        candidates = candidates_payload.get("candidates", [])
        lines = ["- page [ref=e0]:"]
        for item in candidates:
            text_bits = [
                str(item.get("tag_name", "") or "node"),
                str(item.get("role", "") or ""),
                str(item.get("aria_label", "") or "") or str(item.get("text", "") or ""),
            ]
            summary = " ".join(bit for bit in text_bits if bit).strip()
            lines.append(f"  - {summary} [ref={item.get('ref', '')}]")
        snapshot_text = "\n".join(lines)
        self._last_snapshot_text = snapshot_text
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        return {
            **current,
            "target": str(target or ""),
            "depth": depth,
            "boxes": bool(boxes),
            "ref_count": len(candidates),
            "refs": [item.get("ref", "") for item in candidates],
            "snapshot": snapshot_text,
            "snapshot_type": "dom_fallback",
        }

    def _fallback_verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict[str, Any]:
        del element
        details = self._fallback_describe_target(target, by=by, include_box=True)
        if not details.get("visible"):
            raise ValueError(f'Target not visible: "{target}"')
        return {
            **self._raw.get_current_url(),
            "verified": True,
            "target": str(target or "").strip(),
            "visible": True,
            "tag_name": details.get("tag_name", ""),
            "text": details.get("text", ""),
        }

    def _fallback_verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict[str, Any]:
        del element
        details = self._fallback_describe_target(target, by=by, include_box=False)
        actual_value = str(details.get("value", "") or "")
        if actual_value != str(expected_value):
            raise ValueError(
                f'Value mismatch for target "{target}": expected "{expected_value}", got "{actual_value}"'
            )
        return {
            **self._raw.get_current_url(),
            "verified": True,
            "target": str(target or "").strip(),
            "expected_value": str(expected_value),
            "actual_value": actual_value,
        }

    def _fallback_inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict[str, Any]:
        raw = self._run_script_result(
            self._generic_target_script(selector, by, "list", limit=max(1, int(limit))),
            tab_id=tab_id,
        )
        elements = raw if isinstance(raw, list) else []
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        return {
            **current,
            "count": len(elements),
            "elements": elements[: max(1, int(limit))],
        }

    def _fallback_verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict[str, Any]:
        del element
        result = self._run_script_result(
            """
            const el = document.activeElement;
            if (!el) return null;
            return {
              tag_name: (el.tagName || '').toLowerCase(),
              text: String(el.innerText || el.textContent || '').trim(),
              id: String(el.id || '').trim(),
              name: String(el.getAttribute('name') || '').trim(),
              class: String(el.getAttribute('class') || '').trim(),
              aria_label: String(el.getAttribute('aria-label') || '').trim(),
              role: String(el.getAttribute('role') || '').trim(),
              value: 'value' in el ? String(el.value || '') : '',
              href: String(el.getAttribute('href') || '').trim()
            };
            """
        )
        if not isinstance(result, dict) or not result:
            raise ValueError("No active element found")
        if str(target or "").strip():
            details = self._fallback_describe_target(target, by=by, include_box=False)
            comparable = ("id", "name", "href", "text", "aria_label", "tag_name")
            if not any(str(result.get(key, "") or "") == str(details.get(key, "") or "") for key in comparable):
                raise ValueError(f'Active element did not match target: "{target}"')
        return {**self._raw.get_current_url(), "verified": True, "element": result, "target": str(target or "").strip()}

    def _fallback_diagnose_target(self, target: str, element: str = "", by: str = "css", text_filter: str = "", limit: int = 10) -> Dict[str, Any]:
        del element
        diagnosis = {
            **self._raw.get_current_url(),
            "target": str(target or "").strip(),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "is_snapshot_ref": bool(SNAPSHOT_REF_PATTERN.match(str(target or "").strip())),
        }
        try:
            diagnosis["status"] = "resolved"
            diagnosis["message"] = "target resolved successfully"
            diagnosis["details"] = self._fallback_describe_target(target, by=by, include_box=True)
        except Exception as exc:
            diagnosis["status"] = "resolve_failed"
            diagnosis["message"] = str(exc)
            diagnosis["selector_matches"] = self._fallback_inspect_elements(
                selector=target,
                by=by,
                limit=max(1, int(limit)),
            )
            diagnosis["page_candidates"] = self._fallback_candidates(
                text_filter=text_filter or target,
                limit=max(1, int(limit)),
                include_boxes=True,
            ).get("candidates", [])
        try:
            diagnosis["interaction_context"] = self._raw.get_interaction_context().get("interaction_context", {})
        except Exception:
            diagnosis["interaction_context"] = {}
        return diagnosis

    def get_summary(self) -> BrowserSessionSummary:
        return self._raw.get_summary()

    def get_capabilities(self) -> Dict:
        return self._capabilities.to_public_dict()

    def list_tabs(self) -> Dict:
        return self._dispatch("list_tabs", lambda: self._raw.list_tabs())

    def open_tab(self, url: str = "", activate: bool = True, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        return self._dispatch(
            "open_tab",
            lambda: self._raw.open_tab(url=url, activate=activate, wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds),
        )

    def activate_tab(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> Dict:
        return self._dispatch(
            "activate_tab",
            lambda: self._raw.activate_tab(tab_id=tab_id, index=index, title_contains=title_contains, url_contains=url_contains),
        )

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        return self._dispatch("close_tab", lambda: self._raw.close_tab(tab_id=tab_id, index=index))

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        return self._dispatch(
            "navigate",
            lambda: self._raw.navigate(url, wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id),
        )

    def get_current_url(self, tab_id: str = "") -> Dict:
        return self._dispatch("get_current_url", lambda: self._raw.get_current_url(tab_id=tab_id))

    def get_page_text(self, tab_id: str = "") -> Dict:
        return self._dispatch("get_page_text", lambda: self._raw.get_page_text(tab_id=tab_id))

    def get_page_html(self, tab_id: str = "") -> Dict:
        return self._dispatch("get_page_html", lambda: self._raw.get_page_html(tab_id=tab_id))

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        return self._dispatch(
            "inspect_elements",
            lambda: self._raw.inspect_elements(selector, by, limit, tab_id),
            fallback=lambda: self._fallback_inspect_elements(selector, by=by, limit=limit, tab_id=tab_id),
        )

    def get_active_element(self, tab_id: str = "") -> Dict:
        return self._dispatch("get_active_element", lambda: self._raw.get_active_element(tab_id=tab_id))

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        result = self._dispatch("get_interaction_context", lambda: self._raw.get_interaction_context(tab_id=tab_id))
        interaction_context = result.get("interaction_context")
        if isinstance(interaction_context, dict):
            result["interaction_context"] = self._normalize_interaction_context(
                interaction_context,
                action_name="inspect",
                tab_id=str(result.get("tab_id", "") or tab_id or ""),
            )
        return result

    def snapshot(self, target: str = "", by: str = "css", depth: int | None = None, boxes: bool = False, filename: str = "", tab_id: str = "") -> Dict:
        if self._capabilities.engine_name != "playwright_cli" and not self._capabilities.snapshot_refs:
            return self._normalize_result(
                "snapshot",
                self._fallback_snapshot(target=target, by=by, depth=depth, boxes=boxes, filename=filename, tab_id=tab_id),
                used_fallback=True,
            )
        result = self._dispatch(
            "snapshot",
            lambda: self._raw.snapshot(target=target, by=by, depth=depth, boxes=boxes, filename=filename, tab_id=tab_id),
            fallback=lambda: self._fallback_snapshot(target=target, by=by, depth=depth, boxes=boxes, filename=filename, tab_id=tab_id),
        )
        if self._capabilities.engine_name == "playwright_cli":
            snapshot_text = str(result.get("snapshot", "") or "")
            refs = [item.get("ref", "") for item in self._parse_snapshot_candidates(snapshot_text, limit=200)]
            result["ref_count"] = len(refs)
            result["refs"] = refs
        return result

    def list_candidates(self, target: str = "", by: str = "css", text_filter: str = "", limit: int = 25, include_boxes: bool = True, tab_id: str = "") -> Dict:
        return self._dispatch(
            "list_candidates",
            lambda: self._raw.list_candidates(target=target, by=by, text_filter=text_filter, limit=limit, include_boxes=include_boxes, tab_id=tab_id),
            fallback=lambda: self._fallback_candidates(target=target, by=by, text_filter=text_filter, limit=limit, include_boxes=include_boxes, tab_id=tab_id),
        )

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        return self._dispatch(
            "wait_for",
            lambda: self._raw.wait_for(selector, by, timeout_seconds, condition),
            fallback=lambda: self._fallback_wait_for(selector, by=by, timeout_seconds=timeout_seconds, condition=condition),
        )

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._dispatch("click", lambda: self._raw.click(selector, by, timeout_seconds))

    def click_target(self, target: str, element: str = "", by: str = "css", timeout_seconds: int = 20, double_click: bool = False) -> Dict:
        resolved_target = target
        resolved_by = by
        managed_fallback = None
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()) and not self._capabilities.snapshot_refs:
            cached = self._resolve_snapshot_ref(target)
            if cached.get("by") != "snapshot_ref":
                resolved_target = cached["selector"]
                resolved_by = cached["by"]
                if resolved_by == "deep_css":
                    managed_fallback = lambda: self._execute_managed_target_action("click_target", resolved_target, resolved_by)
        return self._dispatch(
            "click_target",
            lambda: self._raw.click_target(resolved_target, element=element, by=resolved_by, timeout_seconds=timeout_seconds, double_click=double_click),
            fallback=managed_fallback,
        )

    def type_text(self, selector: str, text: str, by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        return self._dispatch(
            "type_text",
            lambda: self._raw.type_text(selector, text, by, clear_first, submit, timeout_seconds),
        )

    def type_target(self, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        resolved_target = target
        resolved_by = by
        managed_fallback = None
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()) and not self._capabilities.snapshot_refs:
            cached = self._resolve_snapshot_ref(target)
            if cached.get("by") != "snapshot_ref":
                resolved_target = cached["selector"]
                resolved_by = cached["by"]
                if resolved_by == "deep_css":
                    managed_fallback = lambda: self._execute_managed_target_action(
                        "type_target",
                        resolved_target,
                        resolved_by,
                        text=text,
                        clear_first=clear_first,
                        submit=submit,
                    )
        return self._dispatch(
            "type_target",
            lambda: self._raw.type_target(resolved_target, text, element=element, by=resolved_by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds),
            fallback=managed_fallback,
        )

    def type_target_and_verify(self, target: str, text: str, element: str = "", by: str = "css", clear_first: bool = True, submit: bool = False, timeout_seconds: int = 20) -> Dict:
        resolved_target = target
        resolved_by = by
        managed_fallback = None
        if SNAPSHOT_REF_PATTERN.match(str(target or "").strip()) and not self._capabilities.snapshot_refs:
            cached = self._resolve_snapshot_ref(target)
            if cached.get("by") != "snapshot_ref":
                resolved_target = cached["selector"]
                resolved_by = cached["by"]
                if resolved_by == "deep_css":
                    managed_fallback = lambda: self._execute_managed_target_action(
                        "type_target_and_verify",
                        resolved_target,
                        resolved_by,
                        text=text,
                        clear_first=clear_first,
                        submit=submit,
                    )
        return self._dispatch(
            "type_target_and_verify",
            lambda: self._raw.type_target_and_verify(resolved_target, text, element=element, by=resolved_by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds),
            fallback=managed_fallback,
        )

    def press_key(self, key: str, count: int = 1, selector: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._dispatch(
            "press_key",
            lambda: self._raw.press_key(key, count=count, selector=selector, by=by, timeout_seconds=timeout_seconds),
        )

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        return self._dispatch("run_script", lambda: self._raw.run_script(script, tab_id=tab_id))

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        return self._dispatch("get_console_messages", lambda: self._raw.get_console_messages(tab_id=tab_id, limit=limit, level=level))

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        return self._dispatch("get_page_errors", lambda: self._raw.get_page_errors(tab_id=tab_id, limit=limit))

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        return self._dispatch(
            "get_network_requests",
            lambda: self._raw.get_network_requests(tab_id=tab_id, limit=limit, failed_only=failed_only),
        )

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        return self._dispatch("clear_debug_buffers", lambda: self._raw.clear_debug_buffers(tab_id=tab_id))

    def diagnose_page(self, tab_id: str = "") -> Dict:
        result = self._dispatch("diagnose_page", lambda: self._raw.diagnose_page(tab_id=tab_id))
        return self._augment_diagnosis_payload("diagnose_page", result)

    def verify_text(self, text: str) -> Dict:
        return self._dispatch("verify_text", lambda: self._raw.verify_text(text))

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        return self._dispatch("verify_dialog", lambda: self._raw.verify_dialog(accessible_name=accessible_name, text=text))

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        return self._dispatch(
            "verify_active_element",
            lambda: self._raw.verify_active_element(target=target, by=by, element=element),
            fallback=lambda: self._fallback_verify_active_element(target=target, by=by, element=element),
        )

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        return self._dispatch(
            "verify_target_value",
            lambda: self._raw.verify_target_value(target=target, expected_value=expected_value, element=element, by=by),
            fallback=lambda: self._fallback_verify_target_value(target=target, expected_value=expected_value, element=element, by=by),
        )

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        return self._dispatch(
            "verify_target_visible",
            lambda: self._raw.verify_target_visible(target=target, element=element, by=by),
            fallback=lambda: self._fallback_verify_target_visible(target=target, element=element, by=by),
        )

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        return self._dispatch(
            "describe_target",
            lambda: self._raw.describe_target(target=target, element=element, by=by, include_box=include_box),
            fallback=lambda: self._fallback_describe_target(target=target, element=element, by=by, include_box=include_box),
        )

    def diagnose_target(self, target: str, element: str = "", by: str = "css", text_filter: str = "", limit: int = 10) -> Dict:
        result = self._dispatch(
            "diagnose_target",
            lambda: self._raw.diagnose_target(target=target, element=element, by=by, text_filter=text_filter, limit=limit),
            fallback=lambda: self._fallback_diagnose_target(target=target, element=element, by=by, text_filter=text_filter, limit=limit),
        )
        return self._augment_diagnosis_payload("diagnose_target", result, target=target, by=by, text_filter=text_filter)

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        return self._dispatch("verify_element", lambda: self._raw.verify_element(role=role, accessible_name=accessible_name))

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        return self._dispatch("highlight_target", lambda: self._raw.highlight_target(target=target, element=element, by=by, style=style))

    def clear_highlights(self) -> Dict:
        return self._dispatch("clear_highlights", lambda: self._raw.clear_highlights())

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        return self._dispatch("mouse_move_xy", lambda: self._raw.mouse_move_xy(x, y))

    def mouse_click_xy(self, x: float, y: float, button: str = "left", click_count: int = 1, delay_ms: int = 0) -> Dict:
        return self._dispatch(
            "mouse_click_xy",
            lambda: self._raw.mouse_click_xy(x, y, button=button, click_count=click_count, delay_ms=delay_ms),
        )

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        return self._dispatch("mouse_drag_xy", lambda: self._raw.mouse_drag_xy(start_x, start_y, end_x, end_y))

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        return self._dispatch("screenshot", lambda: self._raw.screenshot(filename=filename, tab_id=tab_id))

    def close(self) -> None:
        self._raw.close()
