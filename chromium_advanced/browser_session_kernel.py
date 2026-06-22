from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from chromium_advanced.browser_engines.base import BrowserSession, BrowserSessionSummary
from chromium_advanced.browser_capability_kernel import enrich_capability_payload
from chromium_advanced.browser_session_kernel_diagnostics import ManagedSessionDiagnosticsMixin
from chromium_advanced.browser_session_kernel_watchers import fallback_watch_page_state


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
SNAPSHOT_LINE_REF_PATTERN = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
HTML_PREVIEW_LIMIT = 12000
RECENT_ACTION_LIMIT = 40
SNAPSHOT_REF_CACHE_LIMIT = 5000
RESOLUTION_CACHE_LIMIT = 40

CAPABILITY_MATRIX = {
    "snapshot": "supports_snapshot",
    "snapshot_refs": "supports_snapshot_refs",
    "target_actions": "supports_target_actions",
    "selector_actions": "supports_selector_actions",
    "highlight": "supports_highlight",
    "coordinates": "supports_coordinates",
    "gesture_actions": "supports_gesture_actions",
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
    gesture_actions: bool
    post_action_context: bool
    tabs: bool
    console_messages: bool
    page_errors: bool
    network_requests: bool

    @classmethod
    def from_legacy(cls, raw: Dict[str, Any]) -> "RuntimeCapabilities":
        engine_name = str(raw.get("engine_name", "") or "unknown")
        runtime_profile = {
            "patchright": "primary",
            "selenium_uc": "stealth",
            "playwright_cli": "lightweight",
            "official_playwright_mcp": "official",
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
            gesture_actions=bool(raw.get("supports_gesture_actions", raw.get("supports_coordinates"))),
            post_action_context=bool(raw.get("supports_post_action_context")),
            tabs=bool(raw.get("supports_tabs")),
            console_messages=bool(raw.get("supports_console_messages")),
            page_errors=bool(raw.get("supports_page_errors")),
            network_requests=bool(raw.get("supports_network_requests")),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        payload = {
            "engine_name": self.engine_name,
            "runtime_profile": self.runtime_profile,
            "capability_version": 3,
            "supports_snapshot": self.snapshot,
            "supports_snapshot_refs": self.snapshot_refs,
            "supports_target_actions": self.target_actions,
            "supports_selector_actions": self.selector_actions,
            "supports_highlight": self.highlight,
            "supports_coordinates": self.coordinates,
            "supports_gesture_actions": self.gesture_actions,
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
                "gesture_actions": {
                    "supported": self.gesture_actions,
                    "actions": ["mouse_move_xy", "mouse_click_xy", "mouse_drag_xy", "mouse_gesture_path"] if self.gesture_actions else [],
                },
                "highlight": {"supported": self.highlight},
            },
            "strategy": {
                "preferred_mode": self.runtime_profile,
                "best_for": {
                    "primary": ["default MCP work", "structured extraction", "deep diagnostics"],
                    "stealth": ["anti-detection browsing", "challenge tolerance", "gesture-style pages"],
                    "lightweight": ["lower-overhead flows", "compatibility tasks", "bounded CLI diagnostics"],
                    "balanced": ["general browser automation"],
                }.get(self.runtime_profile, ["general browser automation"]),
            },
        }
        return enrich_capability_payload(payload)


class ManagedBrowserSession(ManagedSessionDiagnosticsMixin, BrowserSession):
    def __init__(self, raw_session: BrowserSession):
        self._raw = raw_session
        self._capabilities = RuntimeCapabilities.from_legacy(self._safe_raw_capabilities())
        self.engine_name = self._capabilities.engine_name
        if callable(getattr(self._raw, "get_interaction_context", None)):
            self._capabilities.post_action_context = True
        self._snapshot_ref_map: Dict[str, Dict[str, Any]] = {}
        self._last_snapshot_text = ""
        self._next_snapshot_ref = 1
        self._recent_actions: list[Dict[str, Any]] = []
        self._resolution_cache: Dict[str, Dict[str, Any]] = {}
        self._last_structured_page: Dict[str, Any] = {}
        self._last_interaction_hints: Dict[str, Any] = {}

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

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
        resolution_trace = payload.get("resolution_trace", {})
        resolution_mode = ""
        resolution_stage = ""
        resolution_scoped = False
        if isinstance(resolution_trace, dict):
            resolution_mode = str(resolution_trace.get("source", "") or "")
            resolution_stage = str(resolution_trace.get("stage", "") or "")
            resolution_scoped = bool(resolution_trace.get("scoped", False))
        trace = {
            "timestamp": round(time.time(), 3),
            "action_name": str(action_name or ""),
            "ok": payload.get("ok") is not False,
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "used_fallback": bool((payload.get("action_meta") or {}).get("used_fallback")),
            "duration_ms": self._safe_int(payload.get("duration_ms", 0), default=0),
            "tab_id": str(payload.get("tab_id", "") or ""),
            "url": str(payload.get("url", "") or ""),
            "title": str(payload.get("title", "") or ""),
            "target": str(payload.get("target", "") or payload.get("selector", "") or ""),
            "error_code": str(payload.get("error_code", "") or ""),
            "error_type": str(payload.get("error_type", "") or ""),
            "recoverable": bool(payload.get("recoverable", False)),
            "resolution_mode": resolution_mode,
            "resolution_stage": resolution_stage,
            "resolution_scoped": resolution_scoped,
        }
        self._recent_actions.append(trace)
        overflow = len(self._recent_actions) - RECENT_ACTION_LIMIT
        if overflow > 0:
            del self._recent_actions[:overflow]
        if action_name in {
            "navigate",
            "open_tab",
            "activate_tab",
            "close_tab",
            "click",
            "click_target",
            "type_text",
            "type_target",
            "type_target_and_verify",
            "select_option",
            "drag_target",
            "handle_dialog",
            "navigate_back",
            "navigate_forward",
        }:
            self._clear_resolution_cache()

    def _recent_actions_payload(self, limit: int = 10) -> list[Dict[str, Any]]:
        bounded = max(1, int(limit))
        return [dict(item) for item in self._recent_actions[-bounded:]]

    def get_action_trace(self, limit: int = 20) -> Dict[str, Any]:
        bounded = max(1, min(RECENT_ACTION_LIMIT, int(limit)))
        recent = self._recent_actions_payload(limit=bounded)
        durations = [self._safe_int(item.get("duration_ms", 0), 0) for item in self._recent_actions]
        failures = [dict(item) for item in self._recent_actions if not item.get("ok")]
        slowest = sorted(
            (dict(item) for item in self._recent_actions),
            key=lambda item: self._safe_int(item.get("duration_ms", 0), 0),
            reverse=True,
        )[: min(10, bounded)]
        fallback_count = len([item for item in self._recent_actions if item.get("used_fallback")])
        return {
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "trace_limit": RECENT_ACTION_LIMIT,
            "recent_action_count": len(self._recent_actions),
            "recent_failure_count": len(failures),
            "fallback_action_count": fallback_count,
            "average_action_duration_ms": round(sum(durations) / max(1, len(durations))) if durations else 0,
            "max_action_duration_ms": max(durations) if durations else 0,
            "slowest_actions": slowest,
            "recent_failures": failures[-bounded:],
            "recent_actions": recent,
        }

    def _recent_actions_excluding_current(self, action_name: str, limit: int = 10) -> list[Dict[str, Any]]:
        items = [dict(item) for item in self._recent_actions]
        if items and str(items[-1].get("action_name", "") or "") == str(action_name or ""):
            items = items[:-1]
        bounded = max(1, int(limit))
        return items[-bounded:]

    def _recent_target_hints(self, limit: int = 6) -> list[str]:
        hints: list[str] = []
        for item in reversed(self._recent_actions[-max(1, int(limit) * 2) :]):
            target = str(item.get("target", "") or "").strip().lower()
            if not target:
                continue
            target = re.sub(r"[^a-z0-9:_\-\s>#.]+", " ", target)
            for token in re.split(r"[\s>#.]+", target):
                token = token.strip()
                if len(token) < 3:
                    continue
                if token not in hints:
                    hints.append(token)
                if len(hints) >= max(1, int(limit)):
                    return hints
        return hints

    def _clear_resolution_cache(self) -> None:
        self._resolution_cache.clear()

    def _remember_structured_context(self, structured_page: Any = None, interaction_hints: Any = None) -> None:
        if isinstance(structured_page, dict) and structured_page:
            self._last_structured_page = dict(structured_page)
        if isinstance(interaction_hints, dict) and interaction_hints:
            self._last_interaction_hints = dict(interaction_hints)

    def _recent_structured_context(self) -> Dict[str, Any]:
        structured_page = dict(self._last_structured_page) if isinstance(self._last_structured_page, dict) else {}
        interaction_hints = dict(self._last_interaction_hints) if isinstance(self._last_interaction_hints, dict) else {}
        toolbar_labels = [
            str(item).strip().lower()
            for item in interaction_hints.get("toolbar_control_labels", [])
            if str(item or "").strip()
        ] if isinstance(interaction_hints.get("toolbar_control_labels", []), list) else []
        filter_labels = [
            str(item).strip().lower()
            for item in interaction_hints.get("filter_control_labels", [])
            if str(item or "").strip()
        ] if isinstance(interaction_hints.get("filter_control_labels", []), list) else []
        search_labels = [
            str(item).strip().lower()
            for item in interaction_hints.get("search_control_labels", [])
            if str(item or "").strip()
        ] if isinstance(interaction_hints.get("search_control_labels", []), list) else []
        status_labels = [
            str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip().lower()
            for item in structured_page.get("status_surfaces", [])
            if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
        ] if isinstance(structured_page.get("status_surfaces", []), list) else []
        collection_labels: list[str] = []
        if isinstance(interaction_hints.get("collection_summaries", []), list):
            for item in interaction_hints.get("collection_summaries", [])[:6]:
                if not isinstance(item, dict):
                    continue
                for label in item.get("labels_preview", [])[:4]:
                    normalized = str(label or "").strip().lower()
                    if normalized and normalized not in collection_labels:
                        collection_labels.append(normalized)
        return {
            "primary_collection_kind": str(interaction_hints.get("primary_collection_kind", "") or structured_page.get("primary_collection_kind", "") or "").strip().lower(),
            "interaction_region": str(interaction_hints.get("interaction_region", "") or structured_page.get("interaction_region", "") or "").strip().lower(),
            "has_modal": bool(interaction_hints.get("has_modal", False)),
            "toolbar_labels": toolbar_labels[:8],
            "filter_labels": filter_labels[:8],
            "search_labels": search_labels[:8],
            "status_labels": status_labels[:8],
            "collection_labels": collection_labels[:8],
        }

    def _resolution_cache_key(
        self,
        *,
        action_name: str,
        target: str,
        by: str,
        text_filter: str,
        limit: int,
        include_boxes: bool,
        tab_id: str,
    ) -> str:
        return json.dumps(
            {
                "action_name": str(action_name or ""),
                "target": str(target or ""),
                "by": str(by or "css"),
                "text_filter": str(text_filter or ""),
                "limit": int(limit or 0),
                "include_boxes": bool(include_boxes),
                "tab_id": str(tab_id or ""),
                "engine_name": self._capabilities.engine_name,
                "recent_target_hints": self._recent_target_hints(limit=6),
                "structured_context": self._recent_structured_context(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _cached_resolution(
        self,
        *,
        action_name: str,
        target: str,
        by: str,
        text_filter: str,
        limit: int,
        include_boxes: bool,
        tab_id: str,
    ) -> Dict[str, Any] | None:
        key = self._resolution_cache_key(
            action_name=action_name,
            target=target,
            by=by,
            text_filter=text_filter,
            limit=limit,
            include_boxes=include_boxes,
            tab_id=tab_id,
        )
        cached = self._resolution_cache.get(key)
        if not isinstance(cached, dict):
            return None
        payload = dict(cached)
        trace = dict(payload.get("trace", {})) if isinstance(payload.get("trace", {}), dict) else {}
        trace["source"] = f"{trace.get('source', '')}+cache" if trace.get("source") else "resolution_cache"
        trace["stage"] = f"{trace.get('stage', '')}+cache" if trace.get("stage") else "cache_hit"
        payload["trace"] = trace
        return payload

    def _store_resolution_cache(
        self,
        *,
        action_name: str,
        target: str,
        by: str,
        text_filter: str,
        limit: int,
        include_boxes: bool,
        tab_id: str,
        payload: Dict[str, Any],
    ) -> None:
        if not isinstance(payload, dict):
            return
        key = self._resolution_cache_key(
            action_name=action_name,
            target=target,
            by=by,
            text_filter=text_filter,
            limit=limit,
            include_boxes=include_boxes,
            tab_id=tab_id,
        )
        stored = {
            "entry": dict(payload.get("entry", {})) if isinstance(payload.get("entry", {}), dict) else payload.get("entry"),
            "trace": dict(payload.get("trace", {})) if isinstance(payload.get("trace", {}), dict) else {},
            "scope": dict(payload.get("scope", {})) if isinstance(payload.get("scope", {}), dict) else {},
            "resolved": dict(payload.get("resolved", {})) if isinstance(payload.get("resolved", {}), dict) else payload.get("resolved"),
            "candidates": [dict(item) for item in payload.get("candidates", []) if isinstance(item, dict)]
            if isinstance(payload.get("candidates", []), list)
            else [],
        }
        self._resolution_cache[key] = stored
        overflow = len(self._resolution_cache) - RESOLUTION_CACHE_LIMIT
        if overflow > 0:
            for old_key in list(self._resolution_cache.keys())[:overflow]:
                self._resolution_cache.pop(old_key, None)

    def _build_resolution_scope(self, action_name: str, target: str = "", by: str = "css", text_filter: str = "") -> Dict[str, Any]:
        filter_terms: list[str] = []
        for source in (text_filter, target if by in {"link_text", "partial_link_text"} else ""):
            normalized = re.sub(r"\s+", " ", str(source or "").strip().lower())
            if not normalized:
                continue
            for token in normalized.split(" "):
                token = token.strip()
                if len(token) >= 2 and token not in filter_terms:
                    filter_terms.append(token)
        combined = " ".join(filter_terms)
        recent_hints = self._recent_target_hints(limit=6)
        transient_terms = ("menu", "dropdown", "popup", "dialog", "sheet", "overlay", "sort", "filter", "newest", "latest", "option")
        prefer_overlay = any(term in combined for term in transient_terms)
        prefer_dialog = any(term in combined for term in ("dialog", "modal", "confirm", "sheet", "popup"))
        prefer_expanded = any(term in combined for term in ("sort", "filter", "menu", "dropdown", "option", "newest", "latest"))
        context = self._recent_structured_context()
        if not (prefer_overlay or prefer_dialog or prefer_expanded):
            recent_text = " ".join(recent_hints)
            prefer_overlay = any(term in recent_text for term in ("menu", "dropdown", "popup", "sort", "filter"))
            prefer_dialog = any(term in recent_text for term in ("dialog", "modal", "sheet"))
            prefer_expanded = any(term in recent_text for term in ("expanded", "sort", "filter", "menu"))
        if not prefer_overlay and bool(context.get("has_modal")):
            prefer_overlay = True
        if not prefer_dialog and bool(context.get("has_modal")):
            prefer_dialog = True
        if not prefer_expanded:
            prefer_expanded = bool(context.get("filter_labels") or context.get("toolbar_labels"))
        return {
            "action_name": str(action_name or ""),
            "target": str(target or ""),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "filter_terms": filter_terms,
            "recent_target_hints": recent_hints,
            "prefer_overlay": prefer_overlay,
            "prefer_dialog": prefer_dialog,
            "prefer_expanded": prefer_expanded,
            "explicit_target": bool(str(target or "").strip()),
            "structured_context": context,
        }

    def _normalize_page_drift(self, payload: Any) -> Dict[str, Any]:
        drift = payload if isinstance(payload, dict) else {}
        return {
            "tab_id": str(drift.get("tab_id", "") or ""),
            "drifted": bool(drift.get("drifted", False)),
            "expected_url": str(drift.get("expected_url", "") or ""),
            "current_url": str(drift.get("current_url", "") or ""),
            "expected_title": str(drift.get("expected_title", "") or ""),
            "current_title": str(drift.get("current_title", "") or ""),
        }

    def _extract_page_drift(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return self._normalize_page_drift({})
        if isinstance(payload.get("page_drift"), dict):
            return self._normalize_page_drift(payload.get("page_drift"))
        page = payload.get("page", {})
        if isinstance(page, dict) and isinstance(page.get("page_drift"), dict):
            return self._normalize_page_drift(page.get("page_drift"))
        return self._normalize_page_drift({})

    def _classify_failure(self, error_code: str, alive: bool = True, page_drift: Optional[Dict[str, Any]] = None) -> str:
        normalized = str(error_code or "").strip()
        drift = self._normalize_page_drift(page_drift)
        if alive and drift.get("drifted"):
            return "page_drift"
        if not alive or normalized == "session_not_alive":
            return "session_lost"
        if normalized == "page_not_ready":
            return "page_not_ready"
        if normalized in {"target_not_found", "target_not_interactable"}:
            return "target_resolution"
        if normalized == "timeout":
            return "page_synchronization"
        if normalized == "action_not_supported_by_runtime":
            return "capability_gap"
        return "runtime_failure"

    def _recovery_actions_for_failure(self, error_code: str, alive: bool = True, page_drift: Optional[Dict[str, Any]] = None) -> list[str]:
        classification = self._classify_failure(error_code, alive=alive, page_drift=page_drift)
        if classification == "page_drift":
            return ["reactivate_expected_tab", "reopen_expected_url", "retry_on_sticky_tab", "diagnose_page"]
        if classification == "session_lost":
            return ["recreate_session", "reopen_target_page"]
        if classification == "page_not_ready":
            return ["wait_for_page_stable", "wait_for_text_change", "refresh_page_state", "diagnose_page"]
        if classification == "target_resolution":
            return ["refresh_candidates_or_snapshot", "retry_with_scoped_targeting", "diagnose_page"]
        if classification == "page_synchronization":
            return ["wait_and_retry", "refresh_page_state", "diagnose_page"]
        if classification == "capability_gap":
            return ["switch_engine_for_capability", "switch_to_managed_fallback", "use_supported_action_path", "diagnose_page"]
        return ["diagnose_page", "retry_action", "recreate_session_if_repeatable"]

    def _encode_js_string(self, value: str) -> str:
        return json.dumps(str(value or ""))

    def _selector_target_for_cli(self, selector: str, by: str = "css") -> str:
        normalized_by = str(by or "css").strip().lower()
        if normalized_by == "css":
            return str(selector or "")
        if normalized_by == "xpath":
            return f"xpath={selector}"
        if normalized_by == "id":
            return f"#{selector}"
        if normalized_by == "name":
            return f"[name={json.dumps(str(selector or ''))}]"
        if normalized_by == "tag":
            return str(selector or "")
        if normalized_by == "class":
            return f".{selector}"
        if normalized_by in {"link_text", "partial_link_text"}:
            return f"text={selector}"
        raise ValueError(f"unsupported selector type: {by}")

    def _dom_runtime_helpers_js(self) -> str:
        return """
        const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
        const pickSingle = value => value ? [value] : [];
        const overlayPattern = /(menu|dropdown|popup|dialog|sheet|overlay|popover|tooltip|listbox)/i;
        const customTagPreview = nodes => nodes.map(node => (node.tagName || '').toLowerCase()).filter(tag => tag.includes('-')).slice(0, 8);
        const elementAncestry = el => {
          const items = [];
          let current = el;
          while (current && current.nodeType === Node.ELEMENT_NODE) {
            items.push(current);
            current = current.parentElement || ((current.getRootNode() instanceof ShadowRoot) ? current.getRootNode().host : null);
          }
          return items;
        };
        const textPreview = value => {
          const normalized = normalize(value);
          return normalized.length > 180 ? normalized.slice(0, 177) + '...' : normalized;
        };
        const inferAccessibleName = el => {
          const ariaLabel = normalize(el.getAttribute('aria-label') || '');
          if (ariaLabel) return ariaLabel;
          const labelledBy = normalize(el.getAttribute('aria-labelledby') || '');
          if (labelledBy) {
            const labelText = labelledBy
              .split(/\\s+/)
              .map(id => document.getElementById(id))
              .filter(Boolean)
              .map(node => normalize(node.innerText || node.textContent || ''))
              .filter(Boolean)
              .join(' ');
            if (labelText) return labelText;
          }
          if (el.labels && el.labels.length) {
            const labelText = Array.from(el.labels)
              .map(node => normalize(node.innerText || node.textContent || ''))
              .filter(Boolean)
              .join(' ');
            if (labelText) return labelText;
          }
          const placeholder = normalize(el.getAttribute('placeholder') || '');
          if (placeholder) return placeholder;
          const titleText = normalize(el.getAttribute('title') || '');
          if (titleText) return titleText;
          return textPreview(el.innerText || el.textContent || '');
        };
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
          const ancestry = elementAncestry(el);
          const dialogAncestors = ancestry.filter(node => normalize(node.getAttribute && node.getAttribute('role')) === 'dialog' || node.tagName === 'DIALOG' || normalize(node.getAttribute && node.getAttribute('aria-modal')) === 'true');
          const overlayAncestors = ancestry.filter(node => {
            const role = normalize(node.getAttribute && node.getAttribute('role'));
            const classes = normalize(node.getAttribute && node.getAttribute('class'));
            const identifier = normalize(node.getAttribute && node.getAttribute('id'));
            return overlayPattern.test([role, classes, identifier, node.tagName || ''].join(' '));
          });
          const role = normalize(el.getAttribute('role') || '');
          const inputType = normalize(el.getAttribute('type') || '');
          const accessibleName = inferAccessibleName(el);
          const ancestryPath = ancestry.map(node => (node.tagName || '').toLowerCase()).filter(Boolean).slice(0, 10).join(' > ');
          const controlType = role || inputType || (el.tagName || '').toLowerCase();
          const popupRole = normalize(el.getAttribute('aria-haspopup') || '');
          const selected = ('selected' in el) ? !!el.selected : normalize(el.getAttribute('aria-selected') || '') === 'true';
          const expanded = normalize(el.getAttribute('aria-expanded') || '') === 'true';
          const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
          const scopeTags = [];
          if (dialogAncestors.length) scopeTags.push('dialog');
          if (overlayAncestors.length) scopeTags.push('overlay');
          if (expanded) scopeTags.push('expanded');
          if (selected) scopeTags.push('selected');
          if (popupRole) scopeTags.push('popup');
          if ((el.tagName || '').includes('-') || customTagPreview(ancestry).length) scopeTags.push('custom-element');
          return {
            tag_name: (el.tagName || '').toLowerCase(),
            text: normalize(el.innerText || el.textContent || ''),
            text_preview: textPreview(el.innerText || el.textContent || ''),
            value: 'value' in el ? String(el.value || '') : '',
            visible,
            enabled: !disabled,
            checked: 'checked' in el ? !!el.checked : null,
            selected,
            expanded,
            disabled,
            id: normalize(el.id || ''),
            name: normalize(el.getAttribute('name') || ''),
            class: normalize(el.getAttribute('class') || ''),
            placeholder: normalize(el.getAttribute('placeholder') || ''),
            title_attr: normalize(el.getAttribute('title') || ''),
            aria_label: normalize(el.getAttribute('aria-label') || ''),
            aria_expanded: normalize(el.getAttribute('aria-expanded') || ''),
            aria_haspopup: popupRole,
            role,
            input_type: inputType,
            control_type: controlType,
            accessible_name: accessibleName,
            href: normalize(el.getAttribute('href') || ''),
            outer_html: el.outerHTML || '',
            selector: buildSelectorWithinRoot(el, el.getRootNode()),
            deep_selector: buildDeepSelector(el),
            ancestry_path: ancestryPath,
            custom_element_ancestry: customTagPreview(ancestry),
            dialog_ancestry: dialogAncestors.map(node => (node.tagName || '').toLowerCase()).slice(0, 6),
            overlay_ancestry: overlayAncestors.map(node => (node.tagName || '').toLowerCase()).slice(0, 6),
            scope_tags: scopeTags,
            in_dialog: dialogAncestors.length > 0,
            in_overlay: overlayAncestors.length > 0,
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
        dest_selector: str = "",
        dest_by: str = "",
        values: list[str] | None = None,
    ) -> str:
        escaped_selector = self._encode_js_string(selector)
        escaped_by = self._encode_js_string(by)
        escaped_action = self._encode_js_string(action)
        escaped_text = self._encode_js_string(text)
        escaped_dest_selector = self._encode_js_string(dest_selector)
        escaped_dest_by = self._encode_js_string(dest_by)
        encoded_values = json.dumps([str(item) for item in (values or [])], ensure_ascii=False)
        return f"""
        {self._dom_runtime_helpers_js()}
        const selector = {escaped_selector};
        const by = {escaped_by}.toLowerCase();
        const action = {escaped_action};
        const text = {escaped_text};
        const destSelector = {escaped_dest_selector};
        const destBy = {escaped_dest_by}.toLowerCase();
        const values = {encoded_values};
        const clearFirst = {str(bool(clear_first)).lower()};
        const submit = {str(bool(submit)).lower()};
        const el = queryAllDeep(selector, by)[0] || null;
        if (!el) {{
          return {{ ok: false, error: `Target not found: ${{selector}}`, error_type: 'ValueError' }};
        }}
        const dispatchMouse = (node, type) => {{
          node.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, composed: true }}));
        }};
        if (action === 'click') {{
          el.scrollIntoView({{ block: 'center', inline: 'center' }});
          if (typeof el.click === 'function') el.click();
          else el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, composed: true }}));
          return {{ ok: true, clicked: true, target: selector, by, details: describeElement(el) }};
        }}
        if (action === 'hover') {{
          el.scrollIntoView({{ block: 'center', inline: 'center' }});
          dispatchMouse(el, 'mouseover');
          dispatchMouse(el, 'mouseenter');
          dispatchMouse(el, 'mousemove');
          return {{ ok: true, hovered: true, target: selector, by, details: describeElement(el) }};
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
        if (action === 'select_option') {{
          if (!('options' in el) || typeof el.selectedOptions === 'undefined') {{
            return {{ ok: false, error: `Target is not a selectable control: ${{selector}}`, error_type: 'ValueError' }};
          }}
          const requested = Array.isArray(values) ? values.map(item => String(item || '')) : [];
          const matched = [];
          for (const option of Array.from(el.options || [])) {{
            const optionValue = String(option.value || '');
            const optionLabel = String(option.label || option.textContent || '').trim();
            const shouldSelect = requested.includes(optionValue) || requested.includes(optionLabel);
            option.selected = shouldSelect;
            if (shouldSelect) matched.push(optionValue || optionLabel);
          }}
          el.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
          el.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
          return {{ ok: true, selected: true, target: selector, by, values: matched, details: describeElement(el) }};
        }}
        if (action === 'drag') {{
          const dest = queryAllDeep(destSelector, destBy)[0] || null;
          if (!dest) {{
            return {{ ok: false, error: `Target not found: ${{destSelector}}`, error_type: 'ValueError' }};
          }}
          el.scrollIntoView({{ block: 'center', inline: 'center' }});
          dest.scrollIntoView({{ block: 'center', inline: 'center' }});
          dispatchMouse(el, 'mousedown');
          dispatchMouse(dest, 'mousemove');
          dispatchMouse(dest, 'mouseup');
          el.dispatchEvent(new DragEvent('dragstart', {{ bubbles: true, cancelable: true, composed: true }}));
          dest.dispatchEvent(new DragEvent('dragenter', {{ bubbles: true, cancelable: true, composed: true }}));
          dest.dispatchEvent(new DragEvent('dragover', {{ bubbles: true, cancelable: true, composed: true }}));
          dest.dispatchEvent(new DragEvent('drop', {{ bubbles: true, cancelable: true, composed: true }}));
          dest.dispatchEvent(new DragEvent('dragend', {{ bubbles: true, cancelable: true, composed: true }}));
          return {{ ok: true, dragged: true, source_target: selector, dest_target: destSelector, by, details: describeElement(dest) }};
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
        dest_selector: str = "",
        dest_by: str = "",
        values: list[str] | None = None,
    ) -> Dict[str, Any]:
        raw = self._run_script_result(
            self._managed_target_action_script(
                selector,
                by,
                "click"
                if action_name == "click_target"
                else "hover"
                if action_name == "hover"
                else "select_option"
                if action_name == "select_option"
                else "drag"
                if action_name == "drag_target"
                else "type",
                text=text,
                clear_first=clear_first,
                submit=submit,
                dest_selector=dest_selector,
                dest_by=dest_by,
                values=values,
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

    def _semantic_select_option_fallback(self, selector: str, by: str, values: list[str]) -> Dict[str, Any]:
        normalized_selector = str(selector or "").strip()
        normalized_by = str(by or "css")
        normalized_values = [str(item or "").strip() for item in (values or []) if str(item or "").strip()]
        if not normalized_selector:
            raise ValueError("selector is required")
        if not normalized_values:
            raise ValueError("at least one value is required")

        trigger_details = self._fallback_describe_target(normalized_selector, by=normalized_by, include_box=True)
        trigger_role = str(trigger_details.get("role", "") or "").lower()
        trigger_control_type = str(trigger_details.get("control_type", "") or "").lower()
        expanded = str(trigger_details.get("aria_expanded", "") or "").lower() == "true" or bool(trigger_details.get("expanded"))
        opens_popup = bool(str(trigger_details.get("aria_haspopup", "") or "").strip()) or trigger_role in {"combobox", "button"} or trigger_control_type in {"combobox", "button"}

        if opens_popup and not expanded:
            click_result = self.click(normalized_selector, by=normalized_by)
            if isinstance(click_result, dict) and click_result.get("ok") is False:
                raise RuntimeError(str(click_result.get("error", "") or f'failed to open option surface for "{normalized_selector}"'))

        chosen_candidates: list[Dict[str, Any]] = []
        clicked_targets: list[str] = []
        allowed_roles = {"option", "menuitem", "listbox", "tab"}
        allowed_control_types = {"option", "menuitem", "listbox", "tab", "button"}
        for requested in normalized_values:
            candidates_payload = self.list_candidates(text_filter=requested, limit=12, include_boxes=True)
            candidates = candidates_payload.get("candidates", []) if isinstance(candidates_payload, dict) else []
            if not isinstance(candidates, list):
                candidates = []
            ranked = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                role = str(candidate.get("role", "") or "").lower()
                control_type = str(candidate.get("control_type", "") or "").lower()
                text_blob = " ".join(
                    [
                        str(candidate.get("text", "") or ""),
                        str(candidate.get("aria_label", "") or ""),
                        str(candidate.get("accessible_name", "") or ""),
                    ]
                ).lower()
                semantic_bonus = 0
                if role in allowed_roles:
                    semantic_bonus += 120
                if control_type in allowed_control_types:
                    semantic_bonus += 70
                if candidate.get("in_overlay") or candidate.get("in_dialog"):
                    semantic_bonus += 40
                if requested.lower() == text_blob.strip():
                    semantic_bonus += 80
                elif requested.lower() in text_blob:
                    semantic_bonus += 35
                ranked.append((int(candidate.get("match_score", 0) or 0) + semantic_bonus, candidate))
            ranked.sort(key=lambda item: -item[0])
            if not ranked:
                raise ValueError(f'No option candidate found for "{requested}"')
            selected_candidate = dict(ranked[0][1])
            target_ref = str(selected_candidate.get("ref", "") or selected_candidate.get("target", "") or "").strip()
            target_by = str(selected_candidate.get("by", "") or "css")
            target_selector = str(selected_candidate.get("selector", "") or "").strip()
            if not target_ref and target_selector:
                target_ref = target_selector
            if not target_ref:
                raise ValueError(f'No actionable target found for option "{requested}"')
            click_result = self.click_target(target_ref, by=target_by)
            if isinstance(click_result, dict) and click_result.get("ok") is False:
                raise RuntimeError(str(click_result.get("error", "") or f'Failed to click option "{requested}"'))
            clicked_targets.append(target_ref)
            chosen_candidates.append(selected_candidate)

        current = self._raw.get_current_url()
        final_candidate = chosen_candidates[-1] if chosen_candidates else {}
        return {
            **current,
            "selected": True,
            "matched": True,
            "selection_mode": "semantic_option_surface",
            "selector": normalized_selector,
            "by": normalized_by,
            "requested_values": normalized_values,
            "matched_values": [
                str(item.get("text", "") or item.get("aria_label", "") or item.get("accessible_name", "") or "").strip()
                for item in chosen_candidates
            ],
            "clicked_targets": clicked_targets,
            "trigger_details": trigger_details,
            "selected_candidate": final_candidate,
        }

    def _select_option_with_fallback(self, selector: str, by: str, values: list[str]) -> Dict[str, Any]:
        try:
            return self._execute_managed_target_action(
                "select_option",
                selector,
                by,
                values=values,
            )
        except Exception as exc:
            message = str(exc or "").lower()
            if any(token in message for token in ("not a selectable control", "unsupported managed action", "target not found")):
                return self._semantic_select_option_fallback(selector, by, values)
            raise

    def _fallback_wait_for_text(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict[str, Any]:
        deadline = time.time() + max(1, int(timeout_seconds))
        last_text = ""
        while time.time() < deadline:
            page = self._raw.get_page_text(tab_id=tab_id) if tab_id else self._raw.get_page_text()
            last_text = str(page.get("text", "") or "")
            if str(text or "") in last_text:
                current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
                return {**current, "found": True, "text": str(text or "")}
            time.sleep(0.2)
        raise TimeoutError(f'Timed out waiting for text: "{text}"')

    def _fallback_wait_for_text_gone(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict[str, Any]:
        deadline = time.time() + max(1, int(timeout_seconds))
        while time.time() < deadline:
            page = self._raw.get_page_text(tab_id=tab_id) if tab_id else self._raw.get_page_text()
            page_text = str(page.get("text", "") or "")
            if str(text or "") not in page_text:
                current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
                return {**current, "gone": True, "text": str(text or "")}
            time.sleep(0.2)
        raise TimeoutError(f'Timed out waiting for text to disappear: "{text}"')

    def _fallback_wait_for_timeout(self, timeout_ms: int = 0, tab_id: str = "") -> Dict[str, Any]:
        delay = max(0, int(timeout_ms))
        time.sleep(delay / 1000.0)
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        return {**current, "waited": True, "timeout_ms": delay}

    def _fallback_wait_for_text_change(
        self,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        tab_id: str = "",
    ) -> Dict[str, Any]:
        expected_text = str(text or "")
        previous = str(previous_text or "")
        deadline = time.time() + max(1, int(timeout_seconds))
        last_text = ""
        while time.time() < deadline:
            payload = self._raw.get_page_text(tab_id=tab_id) if tab_id else self._raw.get_page_text()
            current_text = str(payload.get("text", "") or "")
            last_text = current_text
            if expected_text:
                changed = expected_text in current_text and expected_text not in previous
            elif previous:
                changed = current_text != previous
            else:
                changed = bool(current_text.strip())
            if changed:
                return {
                    **(payload if isinstance(payload, dict) else self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()),
                    "changed": True,
                    "match_type": "text_changed",
                    "text": current_text,
                    "previous_text": previous,
                    "observed_text": expected_text,
                }
            time.sleep(0.3)
        raise TimeoutError(f"Timed out waiting for page text change. observed_text={expected_text!r} previous_text_len={len(previous)} last_text_len={len(last_text)}")

    def _fallback_wait_for_page_stable(
        self,
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict[str, Any]:
        deadline = time.time() + max(1, int(timeout_seconds))
        interval = max(50, int(poll_interval_ms)) / 1000.0
        required_cycles = max(1, int(stable_cycles))
        stable_count = 0
        last_signature = ""
        last_payload: Dict[str, Any] = {}
        while time.time() < deadline:
            page = self._raw.get_page_text(tab_id=tab_id) if tab_id else self._raw.get_page_text()
            html = self._raw.get_page_html(tab_id=tab_id) if tab_id else self._raw.get_page_html()
            text_value = str(page.get("text", "") or "")
            html_value = str(html.get("html", "") or "")
            signature_source = f"{str(page.get('url', '') or '')}\n{str(page.get('title', '') or '')}\n{text_value[:4000]}\n{len(html_value)}"
            signature = hashlib.sha1(signature_source.encode("utf-8", errors="ignore")).hexdigest()
            if signature == last_signature:
                stable_count += 1
            else:
                stable_count = 1
                last_signature = signature
            last_payload = {
                **(page if isinstance(page, dict) else {}),
                "stable": stable_count >= required_cycles,
                "stable_cycles": stable_count,
                "required_stable_cycles": required_cycles,
                "poll_interval_ms": max(50, int(poll_interval_ms)),
                "text_length": len(text_value),
                "html_length": len(html_value),
                "page_signature": signature,
            }
            if stable_count >= required_cycles:
                return last_payload
            time.sleep(interval)
        raise TimeoutError(f"Timed out waiting for page stability. stable_cycles={stable_count} required_stable_cycles={required_cycles}")

    def _fallback_navigate_history(self, direction: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict[str, Any]:
        del wait_for_ready, timeout_seconds
        before = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        script = "history.back();" if str(direction) == "back" else "history.forward();"
        self._raw.run_script(script, tab_id=tab_id)
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        current["navigated"] = str(direction)
        current["history_changed"] = (
            str(current.get("url", "") or "") != str(before.get("url", "") or "")
            or str(current.get("title", "") or "") != str(before.get("title", "") or "")
        )
        return current

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

    def _build_resolution_trace(
        self,
        action_name: str,
        *,
        source: str,
        stage: str,
        target: str,
        by: str,
        text_filter: str,
        candidate_count: int,
        matched: bool,
        scope: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "action_name": str(action_name or ""),
            "source": str(source or ""),
            "stage": str(stage or ""),
            "target": str(target or ""),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "candidate_count": int(candidate_count or 0),
            "matched": bool(matched),
            "scoped": bool(scope.get("prefer_overlay") or scope.get("prefer_dialog") or scope.get("prefer_expanded")),
            "scope_preferences": {
                "prefer_overlay": bool(scope.get("prefer_overlay")),
                "prefer_dialog": bool(scope.get("prefer_dialog")),
                "prefer_expanded": bool(scope.get("prefer_expanded")),
                "recent_target_hints": list(scope.get("recent_target_hints", []))[:6],
                "interaction_region": str((scope.get("structured_context", {}) or {}).get("interaction_region", "") or ""),
                "primary_collection_kind": str((scope.get("structured_context", {}) or {}).get("primary_collection_kind", "") or ""),
            },
        }

    def _query_candidate_entries(self, selector: str, by: str, limit: int = 25, tab_id: str = "") -> list[Dict[str, Any]]:
        raw = self._run_script_result(
            self._generic_target_script(selector, by, "list", limit=max(1, int(limit))),
            tab_id=tab_id,
        )
        return raw if isinstance(raw, list) else []

    def _rank_entries(self, entries: list[Dict[str, Any]], text_filter: str, scope: Dict[str, Any]) -> list[tuple[int, int, Dict[str, Any]]]:
        ranked_entries: list[tuple[int, int, Dict[str, Any]]] = []
        lowered_filter = str(text_filter or "").strip().lower()
        context = scope.get("structured_context", {}) if isinstance(scope.get("structured_context"), dict) else {}
        context_tokens: list[str] = []
        for key in ("toolbar_labels", "filter_labels", "search_labels", "status_labels", "collection_labels"):
            values = context.get(key, [])
            if not isinstance(values, list):
                continue
            for value in values:
                normalized = str(value or "").strip().lower()
                if normalized and normalized not in context_tokens:
                    context_tokens.append(normalized)
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            merged = " ".join(
                [
                    str(entry.get("text", "") or ""),
                    str(entry.get("text_preview", "") or ""),
                    str(entry.get("aria_label", "") or ""),
                    str(entry.get("accessible_name", "") or ""),
                    str(entry.get("role", "") or ""),
                    str(entry.get("name", "") or ""),
                    str(entry.get("value", "") or ""),
                    str(entry.get("ancestry_path", "") or ""),
                    str(entry.get("control_type", "") or ""),
                ]
            ).strip()
            if lowered_filter and lowered_filter not in merged.lower():
                continue
            relevance = self._candidate_relevance_details(entry, text_filter=lowered_filter)
            scope_details = self._candidate_scope_details(entry, scope)
            context_score = 0
            context_reasons: list[str] = []
            merged_lower = merged.lower()
            if str(context.get("primary_collection_kind", "") or ""):
                kind = str(context.get("primary_collection_kind", "") or "")
                role = str(entry.get("role", "") or "").lower()
                control_type = str(entry.get("control_type", "") or "").lower()
                if kind == "message_list" and any(token in role or token in control_type for token in ("row", "listitem", "article", "link")):
                    context_score += 40
                    context_reasons.append("primary_collection:message_list")
                elif kind == "comment_threads" and any(token in role or token in control_type for token in ("article", "listitem", "comment", "button")):
                    context_score += 40
                    context_reasons.append("primary_collection:comment_threads")
                elif kind == "repository_list" and any(token in role or token in control_type for token in ("link", "row", "listitem")):
                    context_score += 40
                    context_reasons.append("primary_collection:repository_list")
                elif kind == "result_list" and any(token in role or token in control_type for token in ("link", "button", "row", "listitem", "menuitem")):
                    context_score += 40
                    context_reasons.append("primary_collection:result_list")
            for token in context_tokens[:8]:
                if token and token in merged_lower:
                    context_score += 12
                    context_reasons.append(f"context_label:{token}")
                    break
            if str(context.get("interaction_region", "") or "") == "overlay" and bool(entry.get("in_overlay")):
                context_score += 35
                context_reasons.append("interaction_region:overlay")
            if str(context.get("interaction_region", "") or "") == "dialog" and bool(entry.get("in_dialog")):
                context_score += 35
                context_reasons.append("interaction_region:dialog")
            score = int(relevance.get("score", 0) or 0) + int(scope_details.get("score", 0) or 0) + context_score
            enriched = dict(entry)
            enriched["match_reason"] = {
                "score": score,
                "relevance_reasons": list(relevance.get("reasons", []))[:12],
                "scope_reasons": [*list(scope_details.get("reasons", []))[:12], *context_reasons[:8]],
                "matched_fields": list(relevance.get("matched_fields", []))[:8],
            }
            enriched["ranking_reason"] = "; ".join(
                [
                    *[str(item) for item in list(relevance.get("reasons", []))[:4]],
                    *[str(item) for item in list(scope_details.get("reasons", []))[:4]],
                    *[str(item) for item in context_reasons[:4]],
                ]
            )
            ranked_entries.append((score, index, enriched))
        ranked_entries.sort(key=lambda item: (-item[0], item[1]))
        return ranked_entries

    def _resolve_target_pipeline(
        self,
        action_name: str,
        *,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> Dict[str, Any]:
        normalized_target = str(target or "").strip()
        normalized_by = str(by or "css")
        normalized_filter = str(text_filter or "")
        cached = self._cached_resolution(
            action_name=action_name,
            target=normalized_target,
            by=normalized_by,
            text_filter=normalized_filter,
            limit=limit,
            include_boxes=include_boxes,
            tab_id=tab_id,
        )
        if cached is not None:
            return cached
        scope = self._build_resolution_scope(action_name, normalized_target, normalized_by, normalized_filter)
        if SNAPSHOT_REF_PATTERN.match(normalized_target):
            resolved = self._resolve_snapshot_ref(normalized_target)
            trace = self._build_resolution_trace(
                action_name,
                source="snapshot_cache",
                stage="snapshot_ref",
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                candidate_count=1,
                matched=True,
                scope=scope,
            )
            result = {"entry": None, "trace": trace, "scope": scope, "resolved": resolved, "candidates": []}
            self._store_resolution_cache(
                action_name=action_name,
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                limit=limit,
                include_boxes=include_boxes,
                tab_id=tab_id,
                payload=result,
            )
            return result

        if self._capabilities.engine_name == "playwright_cli" and normalized_target:
            trace = self._build_resolution_trace(
                action_name,
                source="cli_selector",
                stage="direct_selector",
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                candidate_count=1,
                matched=True,
                scope=scope,
            )
            result = {
                "entry": None,
                "trace": trace,
                "scope": scope,
                "resolved": {"selector": normalized_target, "by": normalized_by},
                "candidates": [],
            }
            self._store_resolution_cache(
                action_name=action_name,
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                limit=limit,
                include_boxes=include_boxes,
                tab_id=tab_id,
                payload=result,
            )
            return result

        if self._capabilities.engine_name == "playwright_cli" and not normalized_target:
            snapshot_result = self._raw.snapshot(tab_id=tab_id)
            snapshot_text = str(snapshot_result.get("snapshot", "") or "")
            snapshot_candidates = self._parse_snapshot_candidates(snapshot_text, limit=max(1, int(limit) * 2))
            lowered_filter = normalized_filter.strip().lower()
            if lowered_filter:
                snapshot_candidates = [
                    item
                    for item in snapshot_candidates
                    if lowered_filter in " ".join(
                        [str(item.get("text", "") or ""), str(item.get("aria_label", "") or ""), str(item.get("tag_name", "") or "")]
                    ).lower()
                ]
            trace = self._build_resolution_trace(
                action_name,
                source="snapshot_text",
                stage="snapshot_scan",
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                candidate_count=len(snapshot_candidates[: max(1, int(limit))]),
                matched=bool(snapshot_candidates),
                scope=scope,
            )
            if snapshot_candidates:
                result = {
                    "entry": dict(snapshot_candidates[0]),
                    "trace": trace,
                    "scope": scope,
                    "resolved": None,
                    "candidates": snapshot_candidates[: max(1, int(limit))],
                }
                self._store_resolution_cache(
                    action_name=action_name,
                    target=normalized_target,
                    by=normalized_by,
                    text_filter=normalized_filter,
                    limit=limit,
                    include_boxes=include_boxes,
                    tab_id=tab_id,
                    payload=result,
                )
                return result

        dom_selector = normalized_target or "a,button,input,textarea,select,summary,[role],[aria-label],[title],[placeholder]"
        dom_by = normalized_by if normalized_target else "css"
        dom_entries = self._query_candidate_entries(dom_selector, dom_by, limit=max(25, int(limit) * 4), tab_id=tab_id)
        ranking_filter = normalized_filter or (normalized_target if normalized_by in {"link_text", "partial_link_text"} else "")
        ranked_entries = self._rank_entries(dom_entries, ranking_filter, scope)
        if ranked_entries:
            candidates = []
            for score, _, entry in ranked_entries[: max(1, int(limit))]:
                candidate = dict(entry)
                candidate["match_score"] = score
                if not include_boxes:
                    candidate.pop("box", None)
                candidates.append(candidate)
            trace = self._build_resolution_trace(
                action_name,
                source="dom_fallback",
                stage="ranked_dom_query",
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                candidate_count=len(candidates),
                matched=True,
                scope=scope,
            )
            result = {"entry": dict(candidates[0]), "trace": trace, "scope": scope, "resolved": None, "candidates": candidates}
            self._store_resolution_cache(
                action_name=action_name,
                target=normalized_target,
                by=normalized_by,
                text_filter=normalized_filter,
                limit=limit,
                include_boxes=include_boxes,
                tab_id=tab_id,
                payload=result,
            )
            return result

        trace = self._build_resolution_trace(
            action_name,
            source="dom_fallback",
            stage="no_match",
            target=normalized_target,
            by=normalized_by,
            text_filter=normalized_filter,
            candidate_count=0,
            matched=False,
            scope=scope,
        )
        result = {"entry": None, "trace": trace, "scope": scope, "resolved": None, "candidates": []}
        self._store_resolution_cache(
            action_name=action_name,
            target=normalized_target,
            by=normalized_by,
            text_filter=normalized_filter,
            limit=limit,
            include_boxes=include_boxes,
            tab_id=tab_id,
            payload=result,
        )
        return result

    def _fallback_candidates(self, target: str = "", by: str = "css", text_filter: str = "", limit: int = 25, include_boxes: bool = True, tab_id: str = "") -> Dict[str, Any]:
        resolution = self._resolve_target_pipeline(
            "list_candidates",
            target=target,
            by=by,
            text_filter=text_filter,
            limit=limit,
            include_boxes=include_boxes,
            tab_id=tab_id,
        )
        ranked_entries = [
            (int(candidate.get("match_score", 0) or 0), index, candidate)
            for index, candidate in enumerate(resolution.get("candidates", []))
            if isinstance(candidate, dict)
        ]
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
            "resolution_trace": resolution.get("trace", {}),
            "count": len(candidates),
            "candidates": candidates,
        }

    def _normalize_native_candidates(
        self,
        result: Dict[str, Any],
        *,
        target: str,
        by: str,
        text_filter: str,
        limit: int,
        include_boxes: bool,
        tab_id: str,
    ) -> Dict[str, Any]:
        raw_candidates = result.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        scope = self._build_resolution_scope("list_candidates", target, by, text_filter)
        ranking_filter = str(text_filter or "") or (str(target or "") if str(by or "css") in {"link_text", "partial_link_text"} else "")
        ranked_entries = self._rank_entries(raw_candidates, ranking_filter, scope)
        normalized_candidates: list[Dict[str, Any]] = []
        for score, _, entry in ranked_entries[: max(1, int(limit))]:
            candidate = dict(entry)
            candidate.setdefault("source", "native_engine")
            candidate["match_score"] = score
            deep_selector = str(candidate.get("deep_selector", "") or "").strip()
            candidate.setdefault("by", "deep_css" if deep_selector else "css")
            selector_value = str(candidate.get("selector", "") or "").strip()
            if selector_value:
                candidate.setdefault("target", selector_value)
            if not include_boxes:
                candidate.pop("box", None)
            ref_value = str(candidate.get("ref", "") or "").strip()
            if not ref_value:
                ref_value = f"e{self._next_snapshot_ref}"
                self._next_snapshot_ref += 1
                candidate["ref"] = ref_value
            normalized_candidates.append(candidate)
        self._record_snapshot_refs(normalized_candidates)
        trace = self._build_resolution_trace(
            "list_candidates",
            source="native_engine",
            stage="native_candidates",
            target=str(target or ""),
            by=str(by or "css"),
            text_filter=str(text_filter or ""),
            candidate_count=len(normalized_candidates),
            matched=bool(normalized_candidates),
            scope=scope,
        )
        current = self._raw.get_current_url(tab_id=tab_id) if tab_id else self._raw.get_current_url()
        payload = {
            **current,
            **{k: v for k, v in result.items() if k != "candidates"},
            "target": str(target or ""),
            "text_filter": str(text_filter or ""),
            "resolution_trace": trace,
            "count": len(normalized_candidates),
            "candidates": normalized_candidates,
        }
        payload.setdefault(
            "target_summary",
            {
                "target": str(target or "").strip(),
                "by": str(by or "css"),
                "text_filter": str(text_filter or ""),
                "top_candidate_text": str(
                    (normalized_candidates[0] or {}).get("text", "")
                    or (normalized_candidates[0] or {}).get("aria_label", "")
                    or ""
                )
                if normalized_candidates
                else "",
            },
        )
        return payload

    def _fallback_describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict[str, Any]:
        resolution = self._resolve_target_pipeline(
            "describe_target",
            target=target,
            by=by,
            text_filter="",
            limit=10,
            include_boxes=include_box,
        )
        resolved_target = resolution.get("resolved") if isinstance(resolution.get("resolved"), dict) else None
        if resolved_target and resolved_target.get("by") == "snapshot_ref":
            entry = dict(resolution.get("entry") or {})
        elif resolved_target and self._capabilities.engine_name == "playwright_cli":
            target_expr = self._selector_target_for_cli(resolved_target["selector"], resolved_target["by"])
            if hasattr(self._raw, "_describe_target_via_eval"):
                entry = getattr(self._raw, "_describe_target_via_eval")(target_expr)
            elif hasattr(self._raw, "_eval_on_target"):
                entry = getattr(
                    self._raw,
                    "_eval_on_target",
                )(
                    target_expr,
                    "(element) => { const rect = element.getBoundingClientRect(); return {tag_name: (element.tagName || '').toLowerCase(), text: String(element.innerText || element.textContent || '').trim(), text_preview: String(element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 180), value: 'value' in element ? String(element.value || '') : '', visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length), enabled: !element.disabled && element.getAttribute('aria-disabled') !== 'true', id: String(element.id || '').trim(), name: String(element.getAttribute('name') || '').trim(), class: String(element.getAttribute('class') || '').trim(), placeholder: String(element.getAttribute('placeholder') || '').trim(), title_attr: String(element.getAttribute('title') || '').trim(), aria_label: String(element.getAttribute('aria-label') || '').trim(), accessible_name: String(element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.getAttribute('title') || element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim(), aria_expanded: String(element.getAttribute('aria-expanded') || '').trim(), aria_haspopup: String(element.getAttribute('aria-haspopup') || '').trim(), role: String(element.getAttribute('role') || '').trim(), input_type: String(element.getAttribute('type') || '').trim(), control_type: String(element.getAttribute('role') || element.getAttribute('type') || element.tagName || '').toLowerCase(), href: String(element.getAttribute('href') || '').trim(), ancestry_path: (function() { const tags = []; let current = element; while (current && current.nodeType === Node.ELEMENT_NODE && tags.length < 10) { tags.push((current.tagName || '').toLowerCase()); current = current.parentElement || ((current.getRootNode() instanceof ShadowRoot) ? current.getRootNode().host : null); } return tags.join(' > '); })(), custom_element_ancestry: (function() { const tags = []; let current = element; while (current && current.nodeType === Node.ELEMENT_NODE && tags.length < 8) { const tag = (current.tagName || '').toLowerCase(); if (tag.includes('-')) tags.push(tag); current = current.parentElement || ((current.getRootNode() instanceof ShadowRoot) ? current.getRootNode().host : null); } return tags; })(), dialog_ancestry: [], overlay_ancestry: [], scope_tags: [], in_dialog: false, in_overlay: false, selector: '', deep_selector: '', box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height} }; }",
                )
            else:
                compact_script = " ".join(
                    self._cli_simple_query_script(resolved_target["selector"], resolved_target["by"], include_box=include_box).strip().splitlines()
                )
                entry = getattr(self._raw, "_eval_json")(f"() => {{ {compact_script} }}")
        else:
            entry = resolution.get("entry")
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"Target not found: {target}")
        result = {
            **self._raw.get_current_url(),
            "target": str(target or "").strip(),
            "resolution_trace": resolution.get("trace", {}),
            "visible": bool(entry.get("visible")),
            "enabled": bool(entry.get("enabled", True)),
            **entry,
        }
        if not include_box:
            result.pop("box", None)
        return result

    def _fallback_wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict[str, Any]:
        if self._capabilities.engine_name == "playwright_cli":
            deadline = time.time() + max(1, int(timeout_seconds))
            last_entry: Dict[str, Any] | None = None
            scope = self._build_resolution_scope("wait_for", selector, by, "")
            last_trace = self._build_resolution_trace(
                "wait_for",
                source="cli_selector",
                stage="direct_wait",
                target=selector,
                by=by,
                text_filter="",
                candidate_count=0,
                matched=False,
                scope=scope,
            )
            target_expr = self._selector_target_for_cli(selector, by)
            while time.time() < deadline:
                if hasattr(self._raw, "_eval_on_target"):
                    entry = getattr(
                        self._raw,
                        "_eval_on_target",
                    )(
                        target_expr,
                        "(element) => { const rect = element.getBoundingClientRect(); const visible = !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length); return { found: true, tag_name: (element.tagName || '').toLowerCase(), text: String(element.innerText || element.textContent || '').trim(), visible, enabled: !element.disabled && element.getAttribute('aria-disabled') !== 'true', box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height} }; }",
                    )
                else:
                    script = " ".join(self._cli_wait_query_script(selector, by).strip().splitlines())
                    entry = getattr(self._raw, "_eval_json")(f"() => {{ {script} }}")
                if isinstance(entry, dict) and entry.get("found"):
                    last_entry = entry
                    last_trace = self._build_resolution_trace(
                        "wait_for",
                        source="cli_selector",
                        stage="direct_wait",
                        target=selector,
                        by=by,
                        text_filter="",
                        candidate_count=1,
                        matched=True,
                        scope=scope,
                    )
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
                "resolution_trace": last_trace,
            }
        deadline = time.time() + max(1, int(timeout_seconds))
        last_entry: Dict[str, Any] | None = None
        last_trace: Dict[str, Any] = {}
        while time.time() < deadline:
            resolution = self._resolve_target_pipeline(
                "wait_for",
                target=selector,
                by=by,
                text_filter="",
                limit=5,
                include_boxes=False,
            )
            entry = resolution.get("entry")
            last_trace = resolution.get("trace", {}) if isinstance(resolution.get("trace"), dict) else {}
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
            "resolution_trace": last_trace,
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
        resolution = self._resolve_target_pipeline(
            "diagnose_target",
            target=target,
            by=by,
            text_filter=text_filter,
            limit=limit,
            include_boxes=True,
        )
        diagnosis = {
            **self._raw.get_current_url(),
            "target": str(target or "").strip(),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "is_snapshot_ref": bool(SNAPSHOT_REF_PATTERN.match(str(target or "").strip())),
            "resolution_trace": resolution.get("trace", {}),
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

    def execute_native_action(self, action_name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        executor = getattr(self._raw, "execute_native_action", None)
        if not callable(executor):
            raise ValueError(f"native action is not supported by engine: {self.engine_name}")
        result = executor(str(action_name or "").strip(), dict(args or {}))
        if isinstance(result, dict):
            result.setdefault("engine_name", self.engine_name)
        return result

    def list_tabs(self) -> Dict:
        return self._dispatch("list_tabs", lambda: self._raw.list_tabs())

    def open_tab(self, url: str = "", activate: bool = True, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        result = self._dispatch(
            "open_tab",
            lambda: self._raw.open_tab(url=url, activate=activate, wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds),
        )
        if isinstance(result, dict):
            result.setdefault("opened", True)
            result.setdefault("activated", bool(activate))
            tab = result.get("tab")
            if isinstance(tab, dict):
                result.setdefault("active_tab_id", str(tab.get("tab_id", "") or ""))
                result.setdefault("tab_id", str(result.get("tab_id", "") or tab.get("tab_id", "") or ""))
            if isinstance(result.get("tabs"), list):
                result.setdefault("tab_count", len(result.get("tabs", [])))
        return result

    def activate_tab(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = "") -> Dict:
        result = self._dispatch(
            "activate_tab",
            lambda: self._raw.activate_tab(tab_id=tab_id, index=index, title_contains=title_contains, url_contains=url_contains),
        )
        if isinstance(result, dict):
            result.setdefault("activated", True)
            tab = result.get("tab")
            if isinstance(tab, dict):
                result.setdefault("active_tab_id", str(tab.get("tab_id", "") or ""))
                result.setdefault("tab_id", str(result.get("tab_id", "") or tab.get("tab_id", "") or ""))
            if isinstance(result.get("tabs"), list):
                result.setdefault("tab_count", len(result.get("tabs", [])))
        return result

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        result = self._dispatch("close_tab", lambda: self._raw.close_tab(tab_id=tab_id, index=index))
        if isinstance(result, dict):
            result.setdefault("closed", True)
            closed_tab = result.get("closed_tab")
            if isinstance(closed_tab, dict):
                result.setdefault("closed_tab_id", str(closed_tab.get("tab_id", "") or ""))
            if isinstance(result.get("tabs"), list):
                result.setdefault("tab_count", len(result.get("tabs", [])))
        return result

    def resize(self, width: int, height: int) -> Dict:
        result = self._dispatch("resize", lambda: self._raw.resize(width=int(width), height=int(height)))
        if isinstance(result, dict):
            result.setdefault("resized", True)
            result.setdefault("width", int(width))
            result.setdefault("height", int(height))
        return result

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
            self._remember_structured_context(
                result["interaction_context"].get("structured_page", {}),
                result["interaction_context"].get("interaction_hints", {}),
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
            refs = [item.get("ref", "") for item in self._parse_snapshot_candidates(snapshot_text, limit=SNAPSHOT_REF_CACHE_LIMIT)]
            result["ref_count"] = len(refs)
            result["refs"] = refs
        return result

    def list_candidates(self, target: str = "", by: str = "css", text_filter: str = "", limit: int = 25, include_boxes: bool = True, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "list_candidates",
            lambda: self._raw.list_candidates(target=target, by=by, text_filter=text_filter, limit=limit, include_boxes=include_boxes, tab_id=tab_id),
            fallback=lambda: self._fallback_candidates(target=target, by=by, text_filter=text_filter, limit=limit, include_boxes=include_boxes, tab_id=tab_id),
        )
        if isinstance(result, dict):
            candidates = result.get("candidates", [])
            if isinstance(candidates, list):
                has_native_trace = isinstance(result.get("resolution_trace"), dict)
                needs_normalization = any(
                    not isinstance(item, dict) or "match_score" not in item or "by" not in item or "ref" not in item
                    for item in candidates
                )
                if candidates and (needs_normalization or not has_native_trace):
                    result = self._normalize_native_candidates(
                        result,
                        target=target,
                        by=by,
                        text_filter=text_filter,
                        limit=limit,
                        include_boxes=include_boxes,
                        tab_id=tab_id,
                    )
                else:
                    result.setdefault("count", len(candidates))
                    result.setdefault("target_summary", {
                        "target": str(target or "").strip(),
                        "by": str(by or "css"),
                        "text_filter": str(text_filter or ""),
                        "top_candidate_text": str((candidates[0] or {}).get("text", "") or (candidates[0] or {}).get("aria_label", "") or "") if candidates else "",
                    })
        return result

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        result = self._dispatch(
            "wait_for",
            lambda: self._raw.wait_for(selector, by, timeout_seconds, condition),
            fallback=lambda: self._fallback_wait_for(selector, by=by, timeout_seconds=timeout_seconds, condition=condition),
        )
        if isinstance(result, dict) and result.get("found"):
            result.setdefault("condition", str(condition or "visible"))
            result.setdefault("by", str(by or "css"))
            result.setdefault("matched", True)
            result.setdefault("verified", True)
            result.setdefault("selector", str(selector or "").strip())
        return result

    def wait_for_text(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "wait_for_text",
            lambda: self._raw.wait_for_text(text, timeout_seconds=timeout_seconds, tab_id=tab_id),
            fallback=lambda: self._fallback_wait_for_text(text, timeout_seconds=timeout_seconds, tab_id=tab_id),
        )
        if isinstance(result, dict) and result.get("found"):
            result.setdefault("match_type", "text_visible")
            result.setdefault("matched", True)
            result.setdefault("verified", True)
            result.setdefault("expected_text", str(text or ""))
        return result

    def wait_for_text_gone(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "wait_for_text_gone",
            lambda: self._raw.wait_for_text_gone(text, timeout_seconds=timeout_seconds, tab_id=tab_id),
            fallback=lambda: self._fallback_wait_for_text_gone(text, timeout_seconds=timeout_seconds, tab_id=tab_id),
        )
        if isinstance(result, dict) and result.get("gone"):
            result.setdefault("match_type", "text_gone")
            result.setdefault("matched", True)
            result.setdefault("verified", True)
            result.setdefault("expected_text", str(text or ""))
        return result

    def wait_for_text_change(self, text: str = "", previous_text: str = "", timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        start = time.time()
        try:
            result = self._fallback_wait_for_text_change(
                text=text,
                previous_text=previous_text,
                timeout_seconds=timeout_seconds,
                tab_id=tab_id,
            )
            return self._normalize_result("wait_for_text_change", result, used_fallback=True, duration_ms=int((time.time() - start) * 1000))
        except Exception as exc:
            return self._normalize_failure("wait_for_text_change", exc, used_fallback=True, duration_ms=int((time.time() - start) * 1000))

    def wait_for_page_stable(self, timeout_seconds: int = 20, stable_cycles: int = 2, poll_interval_ms: int = 500, tab_id: str = "") -> Dict:
        start = time.time()
        try:
            result = self._fallback_wait_for_page_stable(
                timeout_seconds=timeout_seconds,
                stable_cycles=stable_cycles,
                poll_interval_ms=poll_interval_ms,
                tab_id=tab_id,
            )
            return self._normalize_result("wait_for_page_stable", result, used_fallback=True, duration_ms=int((time.time() - start) * 1000))
        except Exception as exc:
            return self._normalize_failure("wait_for_page_stable", exc, used_fallback=True, duration_ms=int((time.time() - start) * 1000))

    def wait_for_timeout(self, timeout_ms: int = 0, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "wait_for_timeout",
            lambda: self._raw.wait_for_timeout(timeout_ms=timeout_ms, tab_id=tab_id),
            fallback=lambda: self._fallback_wait_for_timeout(timeout_ms=timeout_ms, tab_id=tab_id),
        )
        if isinstance(result, dict):
            result.setdefault("waited", True)
            result.setdefault("timeout_ms", max(0, int(timeout_ms)))
        return result

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._dispatch("click", lambda: self._raw.click(selector, by, timeout_seconds))

    def hover(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        result = self._dispatch(
            "hover",
            lambda: self._raw.hover(selector, by, timeout_seconds),
            fallback=lambda: self._execute_managed_target_action("hover", selector, by),
        )
        if isinstance(result, dict) and result.get("hovered"):
            result.setdefault("by", str(by or "css"))
        return result

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
        result = self._dispatch(
            "type_target_and_verify",
            lambda: self._raw.type_target_and_verify(resolved_target, text, element=element, by=resolved_by, clear_first=clear_first, submit=submit, timeout_seconds=timeout_seconds),
            fallback=managed_fallback,
        )
        if isinstance(result, dict) and result.get("typed"):
            result.setdefault("verified", bool(result.get("verified", False)))
            result.setdefault("submitted", bool(submit))
            result.setdefault("by", str(resolved_by or by or "css"))
            result.setdefault("target", str(resolved_target or target or "").strip())
            result.setdefault("requested_target", str(target or "").strip())
            result.setdefault("value", str(text or ""))
        return result

    def press_key(self, key: str, count: int = 1, selector: str = "", by: str = "css", timeout_seconds: int = 20) -> Dict:
        return self._dispatch(
            "press_key",
            lambda: self._raw.press_key(key, count=count, selector=selector, by=by, timeout_seconds=timeout_seconds),
        )

    def select_option(self, selector: str, values: list[str] | None = None, by: str = "css", timeout_seconds: int = 20) -> Dict:
        normalized_values = [str(item) for item in (values or []) if str(item or "")]
        result = self._dispatch(
            "select_option",
            lambda: self._raw.select_option(selector, values=normalized_values, by=by, timeout_seconds=timeout_seconds),
            fallback=lambda: self._select_option_with_fallback(selector, by, normalized_values),
        )
        if isinstance(result, dict) and result.get("selected"):
            result.setdefault("by", str(by or "css"))
            result.setdefault("selector", str(selector or "").strip())
            result.setdefault("requested_values", normalized_values)
            result.setdefault("matched", True)
        return result

    def handle_dialog(self, accept: bool = True, prompt_text: str = "", tab_id: str = "") -> Dict:
        result = self._dispatch(
            "handle_dialog",
            lambda: self._raw.handle_dialog(accept=accept, prompt_text=prompt_text, tab_id=tab_id),
        )
        if isinstance(result, dict):
            result.setdefault("handled", True)
            result.setdefault("accepted", bool(accept))
            result.setdefault("dismissed", not bool(accept))
            result.setdefault("prompt_text", str(prompt_text or ""))
        return result

    def file_upload(
        self,
        target: str,
        files: list[str] | None = None,
        by: str = "css",
        element: str = "",
        timeout_seconds: int = 20,
    ) -> Dict:
        normalized_files = [str(item).strip() for item in (files or []) if str(item or "").strip()]
        result = self._dispatch(
            "file_upload",
            lambda: self._raw.file_upload(
                target=target,
                files=normalized_files,
                by=by,
                element=element,
                timeout_seconds=timeout_seconds,
            ),
        )
        if isinstance(result, dict):
            result.setdefault("uploaded", bool(normalized_files))
            result.setdefault("file_count", len(normalized_files))
            result.setdefault("target", str(target or "").strip())
            result.setdefault("by", str(by or "css"))
            result.setdefault("files", list(normalized_files))
        return result

    def navigate_back(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "navigate_back",
            lambda: self._raw.navigate_back(wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id),
            fallback=lambda: self._fallback_navigate_history("back", wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id),
        )
        if isinstance(result, dict):
            result.setdefault("navigated", "back")
            result.setdefault("history_changed", bool(str(result.get("url", "") or "") != "about:blank"))
        return result

    def navigate_forward(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        result = self._dispatch(
            "navigate_forward",
            lambda: self._raw.navigate_forward(wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id),
            fallback=lambda: self._fallback_navigate_history("forward", wait_for_ready=wait_for_ready, timeout_seconds=timeout_seconds, tab_id=tab_id),
        )
        if isinstance(result, dict):
            result.setdefault("navigated", "forward")
            result.setdefault("history_changed", bool(str(result.get("url", "") or "") != "about:blank"))
        return result

    def drag_target(
        self,
        source_target: str,
        dest_target: str,
        source_element: str = "",
        dest_element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        result = self._dispatch(
            "drag_target",
            lambda: self._raw.drag_target(
                source_target,
                dest_target,
                source_element=source_element,
                dest_element=dest_element,
                by=by,
                timeout_seconds=timeout_seconds,
            ),
            fallback=lambda: self._execute_managed_target_action(
                "drag_target",
                source_target,
                by,
                dest_selector=dest_target,
                dest_by=by,
            ),
        )
        if isinstance(result, dict) and result.get("dragged"):
            result.setdefault("by", str(by or "css"))
        return result

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        result = self._dispatch("run_script", lambda: self._raw.run_script(script, tab_id=tab_id))
        if isinstance(result, dict):
            script_result = result.get("result")
            result.setdefault("script_result_type", type(script_result).__name__ if script_result is not None else "NoneType")
            current_state = str(result.get("script_result_state", "") or "").strip().lower()
            if not current_state:
                current_state = "value" if script_result is not None else "null"
                result["script_result_state"] = current_state
            if current_state == "null":
                result.setdefault(
                    "diagnostic_hint",
                    "run_script returned null; the page may still be rendering, the queried node may not exist yet, the script may not have returned a value, or the page returned an empty structured result.",
                )
            elif current_state == "stringified":
                result.setdefault(
                    "diagnostic_hint",
                    "run_script returned a non-JSON-serializable value and the runtime stringified it.",
                )
        return result

    def run_script_batch(self, scripts: list[str], tab_id: str = "", stop_on_error: bool = True) -> Dict:
        if not isinstance(scripts, list) or not scripts:
            raise ValueError("scripts is required")
        items = []
        normalized_tab_id = str(tab_id or "")
        stop = bool(stop_on_error)
        for index, script in enumerate(scripts):
            script_text = str(script or "")
            item = {
                "index": index,
                "script": script_text,
            }
            try:
                item_result = self.run_script(script_text, tab_id=normalized_tab_id)
                item["result"] = item_result
                item["ok"] = item_result.get("ok") is not False if isinstance(item_result, dict) else True
                if isinstance(item_result, dict) and item["ok"] is False:
                    if item_result.get("error"):
                        item["error"] = str(item_result.get("error", "") or "")
                    if item_result.get("error_type"):
                        item["error_type"] = str(item_result.get("error_type", "") or "")
                    if stop:
                        raise RuntimeError(item["error"] or "run_script_batch item failed")
            except Exception as exc:
                item["ok"] = False
                item["error_type"] = type(exc).__name__
                item["error"] = str(exc)
                if stop:
                    items.append(item)
                    error_items = [entry for entry in items if not bool(entry.get("ok", False))]
                    return {
                        "count": len(items),
                        "stop_on_error": stop,
                        "ok_count": len(items) - len(error_items),
                        "error_count": len(error_items),
                        "all_ok": not error_items,
                        "first_error": error_items[0] if error_items else None,
                        "items": items,
                    }
            items.append(item)
        error_items = [item for item in items if not bool(item.get("ok", False))]
        return {
            "count": len(items),
            "stop_on_error": stop,
            "ok_count": len(items) - len(error_items),
            "error_count": len(error_items),
            "all_ok": not error_items,
            "first_error": error_items[0] if error_items else None,
            "items": items,
        }

    def _target_watch_signature(self, details: Dict[str, Any]) -> str:
        important = {
            "tag_name": str(details.get("tag_name", "") or ""),
            "role": str(details.get("role", "") or ""),
            "text": str(details.get("text", "") or ""),
            "text_preview": str(details.get("text_preview", "") or ""),
            "value": str(details.get("value", "") or ""),
            "aria_label": str(details.get("aria_label", "") or ""),
            "accessible_name": str(details.get("accessible_name", "") or ""),
            "placeholder": str(details.get("placeholder", "") or ""),
            "title_attr": str(details.get("title_attr", "") or ""),
            "visible": bool(details.get("visible")),
            "enabled": bool(details.get("enabled", True)),
            "aria_expanded": str(details.get("aria_expanded", "") or ""),
            "aria_haspopup": str(details.get("aria_haspopup", "") or ""),
            "control_type": str(details.get("control_type", "") or ""),
        }
        source = json.dumps(important, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()

    def watch_target_state(
        self,
        target: str,
        text: str = "",
        previous_text: str = "",
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        start = time.time()
        normalized_target = str(target or "").strip()
        normalized_text = str(text or "")
        normalized_previous = str(previous_text or "")
        normalized_element = str(element or "")
        normalized_by = str(by or "css")
        interval = max(50, int(poll_interval_ms)) / 1000.0
        required_cycles = max(1, int(stable_cycles))
        deadline = time.time() + max(1, int(timeout_seconds))
        try:
            initial = self.describe_target(
                normalized_target,
                element=normalized_element,
                by=normalized_by,
                include_box=False,
            )
            initial_text = str(initial.get("text", "") or initial.get("text_preview", "") or "")
            baseline_text = normalized_previous or initial_text
            last_signature = self._target_watch_signature(initial)
            stable_count = 1
            final_details = dict(initial)
            last_observed_text = initial_text
            text_changed = False
            matched_target_text = False
            while time.time() < deadline:
                current = self.describe_target(
                    normalized_target,
                    element=normalized_element,
                    by=normalized_by,
                    include_box=False,
                )
                current_text = str(current.get("text", "") or current.get("text_preview", "") or "")
                current_signature = self._target_watch_signature(current)
                last_observed_text = current_text
                changed_from_previous = current_text != baseline_text if baseline_text else bool(current_text.strip())
                contains_target = bool(normalized_text and normalized_text in current_text)
                text_changed = changed_from_previous or text_changed
                matched_target_text = contains_target or matched_target_text
                if current_signature == last_signature:
                    stable_count += 1
                else:
                    stable_count = 1
                    last_signature = current_signature
                final_details = dict(current)
                if (not normalized_text or contains_target) and changed_from_previous and stable_count >= required_cycles:
                    break
                if normalized_text and contains_target and stable_count >= required_cycles:
                    text_changed = True
                    matched_target_text = True
                    break
                time.sleep(interval)
            else:
                raise TimeoutError(
                    f"Timed out waiting for target state change. target={normalized_target!r} by={normalized_by!r} "
                    f"previous_text_len={len(baseline_text)} last_text_len={len(last_observed_text)}"
                )

            result = {
                **final_details,
                "watch_completed": True,
                "watch_reason": "target_changed_and_stable",
                "target": normalized_target,
                "element": normalized_element,
                "by": normalized_by,
                "initial_text": initial_text,
                "previous_text": baseline_text,
                "final_text": last_observed_text,
                "text_changed": bool(text_changed),
                "target_text": normalized_text,
                "text_contains_target": bool(matched_target_text),
                "stable": stable_count >= required_cycles,
                "stable_cycles": stable_count,
                "required_stable_cycles": required_cycles,
                "poll_interval_ms": max(50, int(poll_interval_ms)),
                "target_signature": last_signature,
                "details": final_details,
                "text_diff": {
                    "changed": last_observed_text != baseline_text,
                    "previous_length": len(baseline_text),
                    "final_length": len(last_observed_text),
                },
            }
            return self._normalize_result("watch_target_state", result, used_fallback=True, duration_ms=int((time.time() - start) * 1000))
        except Exception as exc:
            return self._normalize_failure("watch_target_state", exc, used_fallback=True, duration_ms=int((time.time() - start) * 1000))

    def watch_page_state(
        self,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        def _raw_watch_page_state():
            if not callable(getattr(self._raw, "watch_page_state", None)):
                raise NotImplementedError("watch_page_state is not supported by raw runtime")
            return self._raw.watch_page_state(
                text=text,
                previous_text=previous_text,
                timeout_seconds=timeout_seconds,
                stable_cycles=stable_cycles,
                poll_interval_ms=poll_interval_ms,
                tab_id=tab_id,
            )

        result = self._dispatch(
            "watch_page_state",
            _raw_watch_page_state,
            fallback=lambda: self._fallback_watch_page_state(
                text=text,
                previous_text=previous_text,
                timeout_seconds=timeout_seconds,
                stable_cycles=stable_cycles,
                poll_interval_ms=poll_interval_ms,
                tab_id=tab_id,
            ),
        )
        if isinstance(result, dict):
            result.setdefault("watch_completed", bool(result.get("matched", result.get("verified", False))))
            result.setdefault(
                "watch_reason",
                "text_changed_and_stable" if str(text or "").strip() else "page_stable_after_change",
            )
        return result

    def _fallback_watch_page_state(
        self,
        text: str = "",
        previous_text: str = "",
        timeout_seconds: int = 20,
        stable_cycles: int = 2,
        poll_interval_ms: int = 500,
        tab_id: str = "",
    ) -> Dict:
        return fallback_watch_page_state(
            raw_session=self._raw,
            normalize_result=self._normalize_result,
            normalize_failure=self._normalize_failure,
            text=text,
            previous_text=previous_text,
            timeout_seconds=timeout_seconds,
            stable_cycles=stable_cycles,
            poll_interval_ms=poll_interval_ms,
            tab_id=tab_id,
        )

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        return self._dispatch("get_console_messages", lambda: self._raw.get_console_messages(tab_id=tab_id, limit=limit, level=level))

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        return self._dispatch("get_page_errors", lambda: self._raw.get_page_errors(tab_id=tab_id, limit=limit))

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        return self._dispatch(
            "get_network_requests",
            lambda: self._raw.get_network_requests(tab_id=tab_id, limit=limit, failed_only=failed_only),
        )

    def get_network_request(self, index: int, tab_id: str = "") -> Dict:
        normalized_index = max(1, int(index))
        payload = self.get_network_requests(tab_id=tab_id, limit=max(200, normalized_index), failed_only=False)
        requests = payload.get("requests", [])
        if not isinstance(requests, list):
            requests = []
        if normalized_index > len(requests):
            raise IndexError(f"network request index out of range: {normalized_index}")
        request = requests[normalized_index - 1]
        return {
            "tab_id": str(payload.get("tab_id", "") or tab_id or ""),
            "url": str(payload.get("url", "") or ""),
            "title": str(payload.get("title", "") or ""),
            "index": normalized_index,
            "request": dict(request) if isinstance(request, dict) else {"value": request},
            "available_count": len(requests),
        }

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        return self._dispatch("clear_debug_buffers", lambda: self._raw.clear_debug_buffers(tab_id=tab_id))

    def diagnose_page(self, tab_id: str = "") -> Dict:
        if self._capabilities.engine_name == "playwright_cli":
            result = self._dispatch(
                "diagnose_page",
                lambda: self._fallback_diagnose_page(tab_id=tab_id),
            )
        else:
            result = self._dispatch("diagnose_page", lambda: self._raw.diagnose_page(tab_id=tab_id))
        augmented = self._augment_diagnosis_payload("diagnose_page", result)
        self._remember_structured_context(
            augmented.get("structured_page", {}),
            augmented.get("interaction_context", {}).get("interaction_hints", {}) if isinstance(augmented.get("interaction_context", {}), dict) else {},
        )
        return augmented

    def verify_text(self, text: str) -> Dict:
        result = self._dispatch("verify_text", lambda: self._raw.verify_text(text))
        if isinstance(result, dict):
            matched = bool(result.get("verified", result.get("found", False)))
            result.setdefault("verified", matched)
            result.setdefault("matched", matched)
            result.setdefault("expected_text", str(text or ""))
        return result

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        result = self._dispatch("verify_dialog", lambda: self._raw.verify_dialog(accessible_name=accessible_name, text=text))
        if isinstance(result, dict):
            matched = bool(result.get("verified", result.get("found", False)))
            result.setdefault("verified", matched)
            result.setdefault("matched", matched)
            result.setdefault("expected_accessible_name", str(accessible_name or ""))
            result.setdefault("expected_text", str(text or ""))
        return result

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        return self._dispatch(
            "verify_active_element",
            lambda: self._raw.verify_active_element(target=target, by=by, element=element),
            fallback=lambda: self._fallback_verify_active_element(target=target, by=by, element=element),
        )

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        result = self._dispatch(
            "verify_target_value",
            lambda: self._raw.verify_target_value(target=target, expected_value=expected_value, element=element, by=by),
            fallback=lambda: self._fallback_verify_target_value(target=target, expected_value=expected_value, element=element, by=by),
        )
        if isinstance(result, dict):
            matched = bool(result.get("verified", result.get("matched", False)))
            result.setdefault("verified", matched)
            result.setdefault("matched", matched)
            result.setdefault("expected_value", str(expected_value or ""))
            result.setdefault("target", str(target or "").strip())
            result.setdefault("by", str(by or "css"))
        return result

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        result = self._dispatch(
            "verify_target_visible",
            lambda: self._raw.verify_target_visible(target=target, element=element, by=by),
            fallback=lambda: self._fallback_verify_target_visible(target=target, element=element, by=by),
        )
        if isinstance(result, dict):
            matched = bool(result.get("verified", result.get("visible", False)))
            result.setdefault("verified", matched)
            result.setdefault("matched", matched)
            result.setdefault("target", str(target or "").strip())
            result.setdefault("by", str(by or "css"))
        return result

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        result = self._dispatch(
            "describe_target",
            lambda: self._raw.describe_target(target=target, element=element, by=by, include_box=include_box),
            fallback=lambda: self._fallback_describe_target(target=target, element=element, by=by, include_box=include_box),
        )
        if isinstance(result, dict):
            result.setdefault("target_summary", {
                "target": str(target or "").strip(),
                "by": str(by or "css"),
                "tag_name": str(result.get("tag_name", "") or ""),
                "role": str(result.get("role", "") or ""),
                "visible": bool(result.get("visible", False)),
                "enabled": bool(result.get("enabled", True)),
                "label": str(result.get("accessible_name", "") or result.get("aria_label", "") or result.get("text_preview", "") or result.get("text", "") or ""),
            })
        return result

    def diagnose_target(self, target: str, element: str = "", by: str = "css", text_filter: str = "", limit: int = 10) -> Dict:
        result = self._dispatch(
            "diagnose_target",
            lambda: self._raw.diagnose_target(target=target, element=element, by=by, text_filter=text_filter, limit=limit),
            fallback=lambda: self._fallback_diagnose_target(target=target, element=element, by=by, text_filter=text_filter, limit=limit),
        )
        return self._augment_diagnosis_payload("diagnose_target", result, target=target, by=by, text_filter=text_filter)

    def generate_locator(self, target: str, element: str = "") -> Dict:
        return self._dispatch(
            "generate_locator",
            lambda: self._raw.generate_locator(target=target, element=element),
            fallback=lambda: {
                "locator": str(target or "").strip(),
                "target": str(target or "").strip(),
                "element": str(element or "").strip(),
                "generated_by": "fallback_passthrough",
            },
        )

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        result = self._dispatch("verify_element", lambda: self._raw.verify_element(role=role, accessible_name=accessible_name))
        if isinstance(result, dict):
            matched = bool(result.get("verified", result.get("found", False)))
            result.setdefault("verified", matched)
            result.setdefault("matched", matched)
            result.setdefault("expected_role", str(role or ""))
            result.setdefault("expected_accessible_name", str(accessible_name or ""))
        return result

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

    def mouse_gesture_path(
        self,
        points: list[dict[str, Any]],
        *,
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        return self._dispatch(
            "mouse_gesture_path",
            lambda: self._raw.mouse_gesture_path(
                points,
                steps_per_segment=steps_per_segment,
                hold_before_ms=hold_before_ms,
                segment_delay_ms=segment_delay_ms,
            ),
        )

    def detect_gesture_grid(
        self,
        container_target: str = "",
        by: str = "css",
        tab_id: str = "",
        min_nodes: int = 4,
    ) -> Dict:
        normalized_target = str(container_target or "").strip()
        normalized_by = str(by or "css")
        min_nodes = max(4, int(min_nodes))
        target_expr = json.dumps(normalized_target, ensure_ascii=False)
        by_expr = json.dumps(normalized_by, ensure_ascii=False)
        script = f"""
const target = {target_expr};
const by = {by_expr};
const minNodes = {int(min_nodes)};

function resolveRootTarget(targetValue, byValue) {{
  if (!targetValue) return null;
  try {{
    if (byValue === 'css') return document.querySelector(targetValue);
    if (byValue === 'xpath') return document.evaluate(targetValue, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (byValue === 'id') return document.getElementById(targetValue);
    if (byValue === 'name') return document.querySelector(`[name="${{String(targetValue).replace(/"/g, '\\"')}}"]`);
    if (byValue === 'class') return document.querySelector(`.${{String(targetValue).trim().split(/\\s+/).join('.')}}`);
    if (byValue === 'tag') return document.querySelector(String(targetValue));
  }} catch (error) {{
    return null;
  }}
  return null;
}}

function textOf(node) {{
  return String(node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
}}

function asBox(rect) {{
  return {{
    x: Number(rect.left.toFixed(2)),
    y: Number(rect.top.toFixed(2)),
    width: Number(rect.width.toFixed(2)),
    height: Number(rect.height.toFixed(2)),
  }};
}}

function centerForRect(rect) {{
  return {{
    x: Number((rect.left + rect.width / 2).toFixed(2)),
    y: Number((rect.top + rect.height / 2).toFixed(2)),
  }};
}}

const root = resolveRootTarget(target, by) || document;
const strongSelector = '.node, [data-index], [data-node], [data-point], [data-key]';
const fallbackSelector = '[role="button"], button, [aria-label]';

function collectCandidates(selectorText) {{
  const items = [];
  for (const element of root.querySelectorAll(selectorText)) {{
    if (!(element instanceof HTMLElement)) continue;
    const rect = element.getBoundingClientRect();
    if (rect.width < 18 || rect.height < 18) continue;
    const label = String(
      element.getAttribute('data-index') ||
      element.getAttribute('data-id') ||
      element.getAttribute('data-node') ||
      element.getAttribute('data-point') ||
      element.getAttribute('data-key') ||
      element.getAttribute('aria-label') ||
      textOf(element) ||
      ''
    ).trim();
    const match = label.match(/\\d+/);
    items.push({{
      label,
      numeric: match ? Number(match[0]) : 0,
      active:
        element.classList.contains('active') ||
        element.classList.contains('selected') ||
        element.classList.contains('current') ||
        element.getAttribute('aria-pressed') === 'true' ||
        element.getAttribute('aria-selected') === 'true',
      center: centerForRect(rect),
      box: asBox(rect),
      classes: String(element.className || ''),
    }});
  }}
  return items;
}}

let nodes = collectCandidates(strongSelector);
if (nodes.length < minNodes) {{
  const seen = new Set(nodes.map((item) => `${{item.center.x}}:${{item.center.y}}:${{item.label}}`));
  for (const item of collectCandidates(fallbackSelector)) {{
    const key = `${{item.center.x}}:${{item.center.y}}:${{item.label}}`;
    if (!seen.has(key)) {{
      nodes.push(item);
      seen.add(key);
    }}
  }}
}}
if (nodes.length < minNodes) {{
  return {{
    detected: false,
    requested_target: target,
    requested_by: by,
    reason: target ? 'no_candidate_nodes_inside_target' : 'no_candidate_gesture_grid_found',
  }};
}}
const numbered = nodes.filter((item) => item.numeric > 0);
const numberingMode = numbered.length >= Math.ceil(nodes.length / 2) ? 'numeric_label' : 'visual_order';
const ordered = [...nodes].sort((a, b) => {{
  if (numberingMode === 'numeric_label') {{
    if (a.numeric !== b.numeric) return a.numeric - b.numeric;
  }}
  if (a.center.y !== b.center.y) return a.center.y - b.center.y;
  return a.center.x - b.center.x;
}});
const rows = new Set(ordered.map((item) => Math.round(item.center.y / 40))).size;
const cols = new Set(ordered.map((item) => Math.round(item.center.x / 40))).size;
return {{
  detected: true,
  requested_target: target,
  requested_by: by,
  numbering_mode: numberingMode,
  node_count: ordered.length,
  rows,
  cols,
  nodes: ordered.map((item, index) => ({{
    index: index + 1,
    label: item.label,
    numeric: item.numeric,
    active: item.active,
    center: item.center,
    box: item.box,
    classes: item.classes,
  }})),
}};
"""
        result = self.run_script(script, tab_id=tab_id)
        payload = result.get("result") if isinstance(result, dict) else None
        if isinstance(payload, dict):
            payload.setdefault("requested_target", normalized_target)
            payload.setdefault("requested_by", normalized_by)
            return payload
        return {
            "detected": False,
            "requested_target": normalized_target,
            "requested_by": normalized_by,
            "error": "gesture_grid_detection_failed",
            "raw_result": result,
        }

    def unlock_gesture_pattern(
        self,
        pattern: list[Any],
        *,
        container_target: str = "",
        by: str = "css",
        tab_id: str = "",
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        normalized_pattern = [str(item).strip() for item in list(pattern or []) if str(item).strip()]
        if len(normalized_pattern) < 2:
            raise ValueError("pattern must contain at least two node ids")
        grid = self.detect_gesture_grid(container_target=container_target, by=by, tab_id=tab_id)
        if not bool(grid.get("detected")):
            return {
                "unlocked": False,
                "gesture_performed": False,
                "pattern": list(normalized_pattern),
                "grid": grid,
                "error": "gesture_grid_not_detected",
            }
        mapping: Dict[str, Dict[str, Any]] = {}
        for item in list(grid.get("nodes", []) or []):
            for key in (
                str(item.get("index", "") or "").strip(),
                str(item.get("numeric", "") or "").strip(),
                str(item.get("label", "") or "").strip(),
            ):
                if key and key not in mapping:
                    mapping[key] = item
        missing = [item for item in normalized_pattern if item not in mapping]
        if missing:
            return {
                "unlocked": False,
                "gesture_performed": False,
                "pattern": list(normalized_pattern),
                "grid": grid,
                "error": "gesture_nodes_missing",
                "missing_nodes": missing,
            }
        points = []
        resolved_nodes = []
        for key in normalized_pattern:
            item = mapping[key]
            center = item.get("center", {}) if isinstance(item, dict) else {}
            points.append({"x": float(center.get("x", 0.0)), "y": float(center.get("y", 0.0))})
            resolved_nodes.append(
                {
                    "requested": key,
                    "index": int(item.get("index", 0) or 0),
                    "numeric": int(item.get("numeric", 0) or 0),
                    "label": str(item.get("label", "") or ""),
                    "center": dict(center) if isinstance(center, dict) else {},
                }
            )
        action = self.mouse_gesture_path(
            points,
            steps_per_segment=int(steps_per_segment),
            hold_before_ms=int(hold_before_ms),
            segment_delay_ms=int(segment_delay_ms),
        )
        return {
            "unlocked": bool(action.get("gesture_performed", False)),
            "gesture_performed": bool(action.get("gesture_performed", False)),
            "pattern": list(normalized_pattern),
            "resolved_nodes": resolved_nodes,
            "point_count": len(points),
            "grid": grid,
            **(dict(action) if isinstance(action, dict) else {"action_result": action}),
        }

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        return self._dispatch("screenshot", lambda: self._raw.screenshot(filename=filename, tab_id=tab_id))

    def close(self) -> None:
        self._raw.close()
