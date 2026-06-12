from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Optional


HTML_PREVIEW_LIMIT = 12000


class ManagedSessionDiagnosticsMixin:
    def _engine_suggestions_for_action(self, action_name: str) -> list[str]:
        normalized = str(action_name or "").strip()
        if normalized in {"mouse_move_xy", "mouse_click_xy", "mouse_drag_xy"}:
            return ["selenium_uc", "patchright"]
        return []

    def _build_session_health_snapshot(self, page_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                "failure_classification": "session_lost",
                "recovery_hint": "recreate_session",
                "recovery_actions": ["recreate_session", "reopen_target_page"],
                "summary_error": str(exc),
                "page_drift": self._normalize_page_drift({}),
            }
        drift_source = page_payload if isinstance(page_payload, dict) else {}
        if not drift_source:
            try:
                drift_source = self._raw.get_current_url()
            except Exception:
                drift_source = {}
        page_drift = self._extract_page_drift(drift_source)
        if not current_url:
            current_url = str(page_drift.get("current_url", "") or current_url)
        if not title:
            title = str(page_drift.get("current_title", "") or title)
        last_failure = next((item for item in reversed(self._recent_actions) if not item.get("ok")), {})
        recovery_hint = "none" if alive else "recreate_session"
        failure_classification = (
            self._classify_failure(str(last_failure.get("error_code", "") or ""), alive=alive, page_drift=page_drift)
            if last_failure
            else "healthy"
        )
        if alive and page_drift.get("drifted"):
            failure_classification = "page_drift"
            recovery_hint = "reactivate_expected_tab"
        elif alive and last_failure:
            error_code = str(last_failure.get("error_code", "") or "")
            if error_code == "timeout":
                recovery_hint = "retry_or_diagnose_page"
            elif error_code in {"target_not_found", "target_not_interactable"}:
                recovery_hint = "refresh_candidates_or_snapshot"
            else:
                recovery_hint = "diagnose_page"
        average_duration_ms = 0
        if self._recent_actions:
            average_duration_ms = int(
                round(sum(self._safe_int(item.get("duration_ms", 0), 0) for item in self._recent_actions) / max(1, len(self._recent_actions)))
            )
        return {
            "alive": alive,
            "current_url": current_url,
            "title": title,
            "engine_name": self._capabilities.engine_name,
            "runtime_profile": self._capabilities.runtime_profile,
            "recent_action_count": len(self._recent_actions),
            "recent_failure_count": len([item for item in self._recent_actions if not item.get("ok")]),
            "average_action_duration_ms": average_duration_ms,
            "last_action_name": str(self._recent_actions[-1].get("action_name", "") or "") if self._recent_actions else "",
            "last_failure": dict(last_failure) if last_failure else {},
            "failure_classification": failure_classification,
            "recovery_hint": recovery_hint,
            "recovery_actions": []
            if not last_failure and alive and not page_drift.get("drifted")
            else self._recovery_actions_for_failure(str(last_failure.get("error_code", "") or ""), alive=alive, page_drift=page_drift),
            "page_drift": page_drift,
        }

    def _supports_managed_post_action_context(self) -> bool:
        return bool(self._capabilities.post_action_context)

    def _compact_context_element(self, element: Any) -> Dict[str, Any]:
        if not isinstance(element, dict):
            return {}
        allowed = ("tag_name", "text", "id", "name", "class", "aria_label", "role", "value", "href", "accessible_name")
        return {key: element.get(key) for key in allowed if key in element and element.get(key) not in {None, ""}}

    def _fallback_modal_state(self, tab_id: str = "") -> Dict[str, Any]:
        try:
            result = self._run_script_result(
                """
                const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const getDeepActiveElement = () => {
                  let current = document.activeElement;
                  let guard = 0;
                  while (current && current.shadowRoot && current.shadowRoot.activeElement && guard < 20) {
                    current = current.shadowRoot.activeElement;
                    guard += 1;
                  }
                  return current;
                };
                const roots = [document];
                const stack = [document.documentElement].filter(Boolean);
                const seenRoots = new Set([document]);
                while (stack.length) {
                  const node = stack.pop();
                  if (!node || !node.children) continue;
                  if (node.shadowRoot && !seenRoots.has(node.shadowRoot)) {
                    seenRoots.add(node.shadowRoot);
                    roots.push(node.shadowRoot);
                    if (node.shadowRoot.children) {
                      for (const child of Array.from(node.shadowRoot.children)) stack.push(child);
                    }
                  }
                  for (const child of Array.from(node.children)) stack.push(child);
                }
                const candidates = [];
                const candidateKeys = new Set();
                const selectors = ['dialog', '[role="dialog"]', '[aria-modal="true"]', '[role="listbox"]', '[role="menu"]'];
                for (const root of roots) {
                  for (const selector of selectors) {
                    for (const el of Array.from(root.querySelectorAll(selector))) {
                      const key = `${selector}:${normalize(el.id || '')}:${normalize(el.getAttribute('role') || '')}:${normalize(el.innerText || el.textContent || '').slice(0, 120)}`;
                      if (!candidateKeys.has(key)) {
                        candidateKeys.add(key);
                        candidates.push(el);
                      }
                    }
                  }
                }
                const activeAncestors = [];
                let activeNode = getDeepActiveElement();
                while (activeNode && activeNode.nodeType === Node.ELEMENT_NODE) {
                  activeAncestors.push(activeNode);
                  activeNode = activeNode.parentElement || ((activeNode.getRootNode() instanceof ShadowRoot) ? activeNode.getRootNode().host : null);
                }
                for (const el of activeAncestors) {
                  const role = normalize(el.getAttribute && el.getAttribute('role'));
                  const ariaModal = normalize(el.getAttribute && el.getAttribute('aria-modal'));
                  if (role === 'dialog' || role === 'listbox' || role === 'menu' || ariaModal === 'true' || (el.tagName || '').toLowerCase() === 'dialog') {
                    const key = `active:${normalize(el.id || '')}:${role}:${normalize(el.innerText || el.textContent || '').slice(0, 120)}`;
                    if (!candidateKeys.has(key)) {
                      candidateKeys.add(key);
                      candidates.push(el);
                    }
                  }
                }
                const rankCandidate = item => {
                  let score = 0;
                  if (item.containsActive) score += 300;
                  if (item.role === 'dialog') score += 240;
                  else if (item.role === 'listbox') score += 180;
                  else if (item.role === 'menu') score += 80;
                  if (item.position === 'fixed') score += 90;
                  else if (item.position === 'absolute') score += 50;
                  const zIndex = Number.parseInt(item.z_index || '0', 10);
                  if (Number.isFinite(zIndex)) score += Math.max(0, Math.min(zIndex, 3000)) / 20;
                  if ((item.tag_name || '') === 'tp-yt-paper-dialog' || /dialog/.test(item.class || '')) score += 100;
                  if (/listbox|selection-group|paper-listbox|overlay|popup|dropdown|menu-popup/.test(`${item.class} ${item.id}`)) score += 80;
                  if (/navigation|drawer|sidebar/.test(`${item.class} ${item.id}`)) score -= 220;
                  if (item.box && item.box.width >= 240 && item.box.x <= 8) score -= 120;
                  if (!item.containsActive && item.role === 'menu' && !/overlay|popup|dropdown/.test(`${item.class} ${item.id}`)) score -= 90;
                  return score;
                };
                const visibleDialogs = candidates
                  .filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  })
                  .map(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return {
                      tag_name: (el.tagName || '').toLowerCase(),
                      role: normalize(el.getAttribute('role') || ''),
                      text: normalize(el.innerText || el.textContent || '').slice(0, 240),
                      aria_label: normalize(el.getAttribute('aria-label') || ''),
                      id: normalize(el.id || ''),
                      class: normalize(el.getAttribute('class') || ''),
                      z_index: style.zIndex || '',
                      position: style.position || '',
                      containsActive: !!(getDeepActiveElement() && el.contains(getDeepActiveElement())),
                      box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                    };
                  })
                  .map(item => ({ ...item, score: rankCandidate(item) }))
                  .filter(item => item.score >= 0)
                  .sort((a, b) => (b.score - a.score) || ((b.box?.width || 0) * (b.box?.height || 0) - (a.box?.width || 0) * (a.box?.height || 0)));
                return {
                  visible: visibleDialogs.length > 0,
                  count: visibleDialogs.length,
                  primary_dialog: visibleDialogs[0] || {},
                  dialogs: visibleDialogs.slice(0, 8),
                };
                """,
                tab_id=tab_id,
            )
            if isinstance(result, dict):
                return {
                    "visible": bool(result.get("visible", False)),
                    "count": int(result.get("count", 0) or 0),
                    "primary_dialog": result.get("primary_dialog", {}) if isinstance(result.get("primary_dialog"), dict) else {},
                    "dialogs": result.get("dialogs", []) if isinstance(result.get("dialogs"), list) else [],
                }
        except Exception:
            pass
        return {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []}

    def _fallback_diagnose_page(self, tab_id: str = "") -> Dict[str, Any]:
        normalized_tab_id = str(tab_id or "").strip()
        current = self._raw.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self._raw.get_current_url()
        payload: Dict[str, Any] = {
            **(current if isinstance(current, dict) else {}),
            "diagnosis": {
                "mode": "managed_fast_path",
                "engine_name": self._capabilities.engine_name,
                "runtime_profile": self._capabilities.runtime_profile,
            },
            "interaction_context": self._fallback_interaction_context(action_name="diagnose_page", tab_id=normalized_tab_id),
        }
        try:
            payload["console"] = self._raw.get_console_messages(tab_id=normalized_tab_id, limit=40)
        except Exception:
            payload["console"] = {"count": 0, "messages": []}
        try:
            payload["page_errors"] = self._raw.get_page_errors(tab_id=normalized_tab_id, limit=40)
        except Exception:
            payload["page_errors"] = {"count": 0, "errors": []}
        try:
            payload["network"] = self._raw.get_network_requests(tab_id=normalized_tab_id, limit=40, failed_only=True)
        except Exception:
            payload["network"] = {"count": 0, "requests": []}
        return payload

    def _extract_structured_page_data(self, text: str, snapshot_text: str = "") -> Dict[str, Any]:
        lines = [line.strip() for line in str(text or "").splitlines()]
        cleaned = [line for line in lines if line]
        headings = [line for line in cleaned if len(line) <= 80 and not line.startswith("@")][:20]
        comments: list[Dict[str, Any]] = []
        index = 0
        while index < len(cleaned):
            line = cleaned[index]
            match = re.match(r"^(@\S+)\s+•\s+(.+)$", line)
            if not match:
                index += 1
                continue
            author = match.group(1).strip()
            age = match.group(2).strip()
            comment_lines: list[str] = []
            video_title = ""
            reply_count = ""
            lookahead = index + 1
            while lookahead < len(cleaned):
                current = cleaned[lookahead]
                if re.match(r"^@\S+\s+•\s+.+$", current):
                    break
                if current.lower().startswith("reply"):
                    lookahead += 1
                    continue
                if re.match(r"^\d+\s+repl(?:y|ies)$", current.lower()):
                    reply_count = current
                    lookahead += 1
                    continue
                if not comment_lines:
                    comment_lines.append(current)
                elif not video_title:
                    video_title = current
                else:
                    break
                lookahead += 1
            comments.append(
                {
                    "author": author,
                    "age": age,
                    "comment": " ".join(comment_lines).strip(),
                    "video_title": video_title,
                    "reply_count": reply_count,
                }
            )
            index = lookahead
        snapshot_refs = re.findall(r"\[ref=((?:f\d+)?e\d+)\]", str(snapshot_text or ""))
        return {
            "headings": headings,
            "comment_threads": comments[:12],
            "comment_thread_count": len(comments),
            "snapshot_ref_count": len(snapshot_refs),
        }

    def _fallback_interaction_context(self, action_name: str = "inspect", tab_id: str = "") -> Dict[str, Any]:
        normalized_tab_id = str(tab_id or "").strip()
        page: Dict[str, Any] = {}
        tabs: list[Dict[str, Any]] = []
        active_element: Dict[str, Any] = {}
        modal_state = {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []}
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
        modal_state = self._fallback_modal_state(tab_id=normalized_tab_id)
        active_tab_id = normalized_tab_id or str(page.get("tab_id", "") or "")
        return {
            "action_name": str(action_name or "inspect"),
            "page": page,
            "tabs": tabs,
            "active_tab_id": active_tab_id,
            "active_element": active_element,
            "modal_state": modal_state,
            "snapshot": {"unsupported": True, "message": "Structured post-action snapshot is not available in this runtime path."},
            "recent_actions": self._recent_actions_payload(limit=8),
            "session_health": self._build_session_health_snapshot(page_payload=page),
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
                normalized_page = (
                    self._raw.get_current_url(tab_id=str(tab_id or "").strip())
                    if str(tab_id or "").strip()
                    else self._raw.get_current_url()
                )
            except Exception:
                normalized_page = {}
        if self._capabilities.engine_name == "playwright_cli" and not bool(modal_state.get("visible", False)):
            refreshed_modal_state = self._fallback_modal_state(tab_id=str(tab_id or "").strip())
            if bool(refreshed_modal_state.get("visible", False)):
                modal_state = refreshed_modal_state
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
            "session_health": context.get("session_health", self._build_session_health_snapshot(page_payload=normalized_page))
            if isinstance(context.get("session_health", None), dict)
            else self._build_session_health_snapshot(page_payload=normalized_page),
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
        context_action = f"{action_name}_failed" if payload.get("ok") is False or failure else action_name
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

    def _normalize_failure(
        self,
        action_name: str,
        error: Exception,
        used_fallback: bool = False,
        duration_ms: int = 0,
    ) -> Dict[str, Any]:
        error_text = str(error)
        error_type = type(error).__name__
        payload = {
            "ok": False,
            "error": error_text,
            "error_type": error_type,
            "error_code": self._infer_error_code(error_type, error_text),
            "recoverable": error_type in {"NotImplementedError", "ValueError", "TimeoutError"},
            "duration_ms": int(duration_ms or 0),
            "action_meta": self._action_meta(action_name, used_fallback=used_fallback),
        }
        if payload["error_code"] == "action_not_supported_by_runtime":
            payload["engine_suggestions"] = self._engine_suggestions_for_action(action_name)
        try:
            payload.update(self._raw.get_current_url())
        except Exception:
            pass
        normalized = self._attach_post_action_context(action_name, payload, failure=True)
        self._record_action_trace(action_name, normalized)
        if isinstance(normalized.get("post_action_context"), dict):
            normalized["post_action_context"]["recent_actions"] = self._recent_actions_payload(limit=8)
            normalized["post_action_context"]["session_health"] = self._build_session_health_snapshot(
                page_payload=normalized.get("post_action_context", {}).get("page", {})
            )
        return normalized

    def _normalize_result(self, action_name: str, result: Any, used_fallback: bool = False, duration_ms: int = 0) -> Dict[str, Any]:
        if not isinstance(result, dict):
            result = {"result": result}
        normalized = dict(result)
        normalized["duration_ms"] = int(duration_ms or 0)
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
            normalized["post_action_context"]["session_health"] = self._build_session_health_snapshot(
                page_payload=normalized.get("post_action_context", {}).get("page", {})
            )
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
            "average_action_duration_ms": self._build_session_health_snapshot(page_payload=payload).get("average_action_duration_ms", 0),
            "target": str(target or ""),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
        }
        try:
            page_text_payload = self._raw.get_page_text(tab_id=str(payload.get("tab_id", "") or ""))
            page_text = str(page_text_payload.get("text", "") or "")
        except Exception:
            page_text = ""
        snapshot_text = ""
        snapshot_payload = payload.get("snapshot")
        if isinstance(snapshot_payload, str):
            snapshot_text = snapshot_payload
        elif isinstance(snapshot_payload, dict):
            snapshot_text = str(snapshot_payload.get("snapshot", "") or "")
        payload["structured_page"] = self._extract_structured_page_data(page_text, snapshot_text=snapshot_text)
        payload["session_health"] = self._build_session_health_snapshot(page_payload=payload)
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
        if entry.get("in_overlay"):
            score += 18
        if entry.get("in_dialog"):
            score += 14
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
            "text_preview": str(entry.get("text_preview", "") or "").strip(),
            "aria_label": str(entry.get("aria_label", "") or "").strip(),
            "accessible_name": str(entry.get("accessible_name", "") or "").strip(),
            "name": str(entry.get("name", "") or "").strip(),
            "id": str(entry.get("id", "") or "").strip(),
            "value": str(entry.get("value", "") or "").strip(),
            "role": role,
            "class": str(entry.get("class", "") or "").strip(),
            "placeholder": str(entry.get("placeholder", "") or "").strip(),
            "title_attr": str(entry.get("title_attr", "") or "").strip(),
            "control_type": str(entry.get("control_type", "") or "").strip(),
            "ancestry_path": str(entry.get("ancestry_path", "") or "").strip(),
        }
        for key, raw_value in haystacks.items():
            value = raw_value.lower()
            if not value:
                continue
            if value == filter_text:
                score += 220 if key in {"text", "aria_label", "accessible_name"} else 180
            elif value.startswith(filter_text):
                score += 140 if key in {"text", "aria_label", "accessible_name"} else 100
            elif filter_text in value:
                score += 90 if key in {"text", "aria_label", "accessible_name", "text_preview"} else 60
        return score

    def _candidate_scope_boost(self, entry: Dict[str, Any], scope: Dict[str, Any]) -> int:
        if not isinstance(entry, dict) or not isinstance(scope, dict):
            return 0
        score = 0
        role = str(entry.get("role", "") or "").lower()
        if scope.get("prefer_overlay") and (entry.get("in_overlay") or role in {"menuitem", "option", "listbox"}):
            score += 120
        if scope.get("prefer_dialog") and entry.get("in_dialog"):
            score += 90
        if scope.get("prefer_expanded") and str(entry.get("aria_expanded", "") or "").lower() == "true":
            score += 70
        hints = " ".join(
            [
                str(entry.get("text", "") or ""),
                str(entry.get("aria_label", "") or ""),
                str(entry.get("accessible_name", "") or ""),
                str(entry.get("class", "") or ""),
                str(entry.get("ancestry_path", "") or ""),
                " ".join(entry.get("custom_element_ancestry", []) if isinstance(entry.get("custom_element_ancestry"), list) else []),
            ]
        ).lower()
        for token in scope.get("recent_target_hints", []):
            if token and token in hints:
                score += 18
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
        started = time.perf_counter()
        try:
            raw_result = raw_call()
            if fallback and self._is_unsupported_result(raw_result):
                duration_ms = int(round((time.perf_counter() - started) * 1000))
                return self._normalize_result(action_name, fallback(), used_fallback=True, duration_ms=duration_ms)
            duration_ms = int(round((time.perf_counter() - started) * 1000))
            return self._normalize_result(action_name, raw_result, used_fallback=False, duration_ms=duration_ms)
        except NotImplementedError:
            if fallback:
                try:
                    duration_ms = int(round((time.perf_counter() - started) * 1000))
                    return self._normalize_result(action_name, fallback(), used_fallback=True, duration_ms=duration_ms)
                except Exception as exc:
                    duration_ms = int(round((time.perf_counter() - started) * 1000))
                    return self._normalize_failure(action_name, exc, used_fallback=True, duration_ms=duration_ms)
            duration_ms = int(round((time.perf_counter() - started) * 1000))
            return self._normalize_failure(
                action_name,
                NotImplementedError(f"{action_name} is not supported by {self._capabilities.engine_name}"),
                used_fallback=False,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            if fallback and self._infer_error_code(type(exc).__name__, str(exc)) == "action_not_supported_by_runtime":
                try:
                    duration_ms = int(round((time.perf_counter() - started) * 1000))
                    return self._normalize_result(action_name, fallback(), used_fallback=True, duration_ms=duration_ms)
                except Exception as fallback_exc:
                    duration_ms = int(round((time.perf_counter() - started) * 1000))
                    return self._normalize_failure(action_name, fallback_exc, used_fallback=True, duration_ms=duration_ms)
            duration_ms = int(round((time.perf_counter() - started) * 1000))
            return self._normalize_failure(action_name, exc, used_fallback=False, duration_ms=duration_ms)

    def _run_script_result(self, script: str, tab_id: str = "") -> Any:
        if self._capabilities.engine_name == "playwright_cli" and hasattr(self._raw, "_eval_json"):
            compact_script = " ".join(str(script or "").strip().splitlines())
            func_text = f"() => {{ {compact_script} }}"
            return getattr(self._raw, "_eval_json")(func_text, tab_id=tab_id)
        result = self._raw.run_script(script, tab_id=tab_id)
        if isinstance(result, dict):
            return result.get("result")
        return result
