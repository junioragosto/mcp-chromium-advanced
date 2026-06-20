from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Optional


HTML_PREVIEW_LIMIT = 12000
ANTI_BOT_TEXT_LIMIT = 12000
ANTI_BOT_TITLE_URL_LIMIT = 2000
ANTI_BOT_STRONG_MARKERS = (
    "challenge-platform",
    "cf-challenge",
    "cf-browser-verification",
    "turnstile",
    "g-recaptcha",
    "hcaptcha",
    "verify you are human",
    "checking your browser before accessing",
    "ddos protection by cloudflare",
    "recaptcha - bot challenge!",
    "正在进行安全验证",
    "请稍候",
)
ANTI_BOT_WEAK_MARKERS = (
    "cloudflare",
    "attention required",
    "security check",
    "bot challenge",
    "human verification",
    "captcha",
)

STATUS_KEYWORDS = (
    "queued",
    "running",
    "in progress",
    "complete",
    "completed",
    "done",
    "success",
    "failed",
    "failure",
    "error",
    "pending",
    "processing",
)

SEARCH_KEYWORDS = ("search", "find", "filter", "query", "keyword")
FILTER_KEYWORDS = ("filter", "sort", "status", "type", "label", "category", "newest", "latest")
PRIMARY_ACTION_KEYWORDS = ("save", "submit", "apply", "confirm", "continue", "next", "open", "run", "create")


class ManagedSessionDiagnosticsMixin:
    def _deferred_anti_bot_snapshot(self) -> Dict[str, Any]:
        return {
            "detected": False,
            "confidence": "deferred",
            "deferred": True,
            "strong_markers": [],
            "weak_markers": [],
            "structured_signals": {},
            "page_signals": {},
            "text_error": "",
            "html_error": "",
        }

    def _next_step_suggestions_for_context(
        self,
        *,
        action_name: str,
        session_health: Dict[str, Any] | None = None,
        structured_page: Dict[str, Any] | None = None,
        interaction_hints: Dict[str, Any] | None = None,
        resolution_trace: Dict[str, Any] | None = None,
    ) -> list[str]:
        health = session_health if isinstance(session_health, dict) else {}
        page = structured_page if isinstance(structured_page, dict) else {}
        hints = interaction_hints if isinstance(interaction_hints, dict) else {}
        trace = resolution_trace if isinstance(resolution_trace, dict) else {}
        suggestions: list[str] = []
        recovery_hint = str(health.get("recovery_hint", "") or "")
        if recovery_hint == "reactivate_expected_tab":
            suggestions.append("reactivate_expected_tab_or_reopen_expected_url")
        elif recovery_hint == "wait_for_page_stable":
            suggestions.append("wait_for_page_stable_then_retry")
        elif recovery_hint == "refresh_candidates_or_snapshot":
            suggestions.append("refresh_candidates_or_snapshot_then_retry")
        elif recovery_hint == "retry_or_diagnose_page":
            suggestions.append("retry_once_then_diagnose_page")
        elif recovery_hint == "diagnose_page":
            suggestions.append("diagnose_page_before_retry")
        elif recovery_hint == "recreate_session":
            suggestions.append("recreate_session")

        if bool(hints.get("has_modal", False)):
            suggestions.append("prefer_modal_or_dialog_scoped_controls")
        if str(hints.get("interaction_region", "") or "") in {"overlay", "dialog"}:
            suggestions.append("prefer_hot_region_controls_over_global_page_scan")
        if trace.get("matched") is False:
            suggestions.append("broaden_target_or_use_text_filter")
        if str(trace.get("stage", "") or "") == "ranked_dom_query":
            suggestions.append("inspect_top_ranked_candidates_before_custom_script")
        if int(page.get("search_control_count", 0) or 0) > 0:
            suggestions.append("prefer_search_or_filter_controls_when_narrowing_scope")
        if str(page.get("primary_collection_kind", "") or "") in {"comment_threads", "message_list", "result_list", "repository_list"}:
            suggestions.append("prefer_collection_scoped_reads_before_full_page_probe")
        if int(page.get("status_candidate_count", 0) or 0) > 0 and action_name in {
            "watch_page_state",
            "watch_target_state",
            "wait_for_page_stable",
            "diagnose_page",
        }:
            suggestions.append("prefer_status_surfaces_before_raw_script_polling")
        if int(page.get("primary_action_count", 0) or 0) > 0 and action_name in {"diagnose_page", "diagnose_target"}:
            suggestions.append("prefer_primary_action_controls_for_next_step")

        deduped: list[str] = []
        for item in suggestions:
            if item and item not in deduped:
                deduped.append(item)
        return deduped[:6]

    def _candidate_text_fields(self, entry: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(entry, dict):
            return {}
        return {
            "text": str(entry.get("text", "") or "").strip(),
            "text_preview": str(entry.get("text_preview", "") or "").strip(),
            "aria_label": str(entry.get("aria_label", "") or "").strip(),
            "accessible_name": str(entry.get("accessible_name", "") or "").strip(),
            "name": str(entry.get("name", "") or "").strip(),
            "id": str(entry.get("id", "") or "").strip(),
            "value": str(entry.get("value", "") or "").strip(),
            "role": str(entry.get("role", "") or "").strip(),
            "class": str(entry.get("class", "") or "").strip(),
            "placeholder": str(entry.get("placeholder", "") or "").strip(),
            "title_attr": str(entry.get("title_attr", "") or "").strip(),
            "control_type": str(entry.get("control_type", "") or "").strip(),
            "ancestry_path": str(entry.get("ancestry_path", "") or "").strip(),
        }

    def _candidate_relevance_details(self, entry: Dict[str, Any], text_filter: str = "") -> Dict[str, Any]:
        if not isinstance(entry, dict):
            return {"score": -1, "reasons": ["invalid_candidate"], "matched_fields": []}
        score = 0
        reasons: list[str] = []
        matched_fields: list[str] = []
        if entry.get("visible"):
            score += 30
            reasons.append("visible")
        if entry.get("enabled", True):
            score += 10
            reasons.append("enabled")
        tag_name = str(entry.get("tag_name", "") or "").lower()
        role = str(entry.get("role", "") or "").lower()
        control_type = str(entry.get("control_type", "") or "").lower()
        if tag_name in {"button", "a", "input", "textarea", "select", "summary"}:
            score += 12
            reasons.append(f"interactive_tag:{tag_name}")
        if role in {"button", "link", "textbox", "option", "menuitem", "tab", "combobox", "listbox"}:
            score += 10
            reasons.append(f"interactive_role:{role}")
        if control_type in {"button", "textbox", "menuitem", "option", "combobox", "listbox"}:
            score += 8
            reasons.append(f"control_type:{control_type}")
        if str(entry.get("aria_haspopup", "") or "").strip():
            score += 16
            reasons.append("has_popup")
        if str(entry.get("aria_expanded", "") or "").strip().lower() == "true":
            score += 18
            reasons.append("expanded")
        if role in {"menuitem", "option"}:
            score += 24
            reasons.append(f"transient_choice_role:{role}")
        if entry.get("in_overlay"):
            score += 18
            reasons.append("inside_overlay")
        if entry.get("in_dialog"):
            score += 14
            reasons.append("inside_dialog")
        if bool(entry.get("selected")):
            score += 8
            reasons.append("selected")
        if bool(entry.get("checked")):
            score += 8
            reasons.append("checked")
        classes = str(entry.get("class", "") or "").lower()
        if any(token in classes for token in ("menu", "dropdown", "popup", "dialog", "sheet", "overlay")):
            score += 10
            reasons.append("overlay_class_affinity")
        if role in {"menuitem", "option", "listbox"} and (
            str(entry.get("aria_haspopup", "") or "").strip()
            or str(entry.get("aria_expanded", "") or "").strip().lower() == "true"
            or any(token in classes for token in ("menu", "dropdown", "popup", "dialog", "sheet", "overlay"))
        ):
            score += 140
            reasons.append("transient_control_priority")
        filter_text = str(text_filter or "").strip().lower()
        if not filter_text:
            return {"score": score, "reasons": reasons, "matched_fields": matched_fields}
        haystacks = self._candidate_text_fields(entry)
        for key, raw_value in haystacks.items():
            value = raw_value.lower()
            if not value:
                continue
            bonus = 0
            if value == filter_text:
                bonus = 220 if key in {"text", "aria_label", "accessible_name"} else 180
                reasons.append(f"exact_match:{key}")
            elif value.startswith(filter_text):
                bonus = 140 if key in {"text", "aria_label", "accessible_name"} else 100
                reasons.append(f"prefix_match:{key}")
            elif filter_text in value:
                bonus = 90 if key in {"text", "aria_label", "accessible_name", "text_preview"} else 60
                reasons.append(f"contains_match:{key}")
            if bonus:
                score += bonus
                if key not in matched_fields:
                    matched_fields.append(key)
        return {"score": score, "reasons": reasons, "matched_fields": matched_fields}

    def _build_anti_bot_detection(self, page_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        page = page_payload if isinstance(page_payload, dict) else {}
        tab_id = str(page.get("tab_id", "") or "")
        title = str(page.get("title", "") or "")
        url = str(page.get("url", "") or "")
        text_excerpt = ""
        html_excerpt = ""
        text_error = ""
        html_error = ""
        try:
            text_payload = self._raw.get_page_text(tab_id=tab_id) if tab_id else self._raw.get_page_text()
            if isinstance(text_payload, dict):
                text_excerpt = str(text_payload.get("text", "") or "")[:ANTI_BOT_TEXT_LIMIT]
        except Exception as exc:
            text_error = str(exc)
        try:
            html_payload = self._raw.get_page_html(tab_id=tab_id) if tab_id else self._raw.get_page_html()
            if isinstance(html_payload, dict):
                html_excerpt = str(html_payload.get("html", "") or "")[:ANTI_BOT_TEXT_LIMIT]
        except Exception as exc:
            html_error = str(exc)

        title_url_haystack = "\n".join([title[:ANTI_BOT_TITLE_URL_LIMIT], url[:ANTI_BOT_TITLE_URL_LIMIT]]).lower()
        text_haystack = text_excerpt.lower()
        html_haystack = html_excerpt.lower()
        strong_hits = [
            marker
            for marker in ANTI_BOT_STRONG_MARKERS
            if marker.lower() in title_url_haystack or marker.lower() in text_haystack or marker.lower() in html_haystack
        ]
        weak_hits = [
            marker
            for marker in ANTI_BOT_WEAK_MARKERS
            if marker.lower() in title_url_haystack or marker.lower() in text_haystack or marker.lower() in html_haystack
        ]

        structured_signals: Dict[str, Any] = {}
        try:
            structured = self._run_script_result(
                """
                return {
                  recaptcha: document.querySelectorAll('.g-recaptcha, iframe[src*="recaptcha"], iframe[title*="reCAPTCHA"]').length,
                  hcaptcha: document.querySelectorAll('iframe[src*="hcaptcha"], [data-hcaptcha-response], [name="h-captcha-response"]').length,
                  turnstile: document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [name="cf-turnstile-response"]').length,
                  challengeForms: document.querySelectorAll('form[id*="challenge"], form[action*="challenge"]').length,
                  challengeInputs: document.querySelectorAll('input[name*="captcha"], input[name*="challenge"], input[name*="cf"]').length,
                };
                """,
                tab_id=tab_id,
            )
            if isinstance(structured, dict):
                structured_signals = dict(structured)
        except Exception:
            structured_signals = {}

        structured_positive = any(int(structured_signals.get(key, 0) or 0) > 0 for key in structured_signals.keys())
        likely_challenge = bool(strong_hits) or structured_positive
        confidence = "high" if likely_challenge else ("low" if weak_hits else "none")

        if not likely_challenge and "cloudflare" in [item.lower() for item in weak_hits]:
            if ("/search?" in url or "/detail/" in url) and (
                "magnet:?" in text_haystack or "磁力链接" in text_excerpt or "资源详情" in text_excerpt
            ):
                weak_hits = [item for item in weak_hits if item.lower() != "cloudflare"]
                confidence = "none" if not weak_hits else "low"

        return {
            "detected": bool(likely_challenge),
            "confidence": confidence,
            "strong_markers": strong_hits,
            "weak_markers": weak_hits,
            "structured_signals": structured_signals,
            "page_signals": {
                "url": url,
                "title": title,
                "has_text": bool(text_excerpt),
                "has_html": bool(html_excerpt),
            },
            "text_error": text_error,
            "html_error": html_error,
        }

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
            if error_code == "page_not_ready":
                recovery_hint = "wait_for_page_stable"
            elif error_code == "timeout":
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
        try:
            payload["snapshot"] = self._fallback_snapshot(tab_id=normalized_tab_id)
        except Exception:
            payload["snapshot"] = {"unsupported": True, "message": "snapshot unavailable during diagnose_page"}
        payload["anti_bot"] = self._build_anti_bot_detection(current if isinstance(current, dict) else {})
        return payload

    def _extract_structured_page_data(self, text: str, snapshot_text: str = "") -> Dict[str, Any]:
        lines = [line.strip() for line in str(text or "").splitlines()]
        cleaned = [line for line in lines if line]
        headings = [line for line in cleaned if len(line) <= 80 and not line.startswith("@")] [:20]
        interactive_controls: list[Dict[str, Any]] = []
        form_controls: list[Dict[str, Any]] = []
        custom_elements: list[str] = []
        links: list[Dict[str, Any]] = []
        buttons: list[Dict[str, Any]] = []
        options: list[Dict[str, Any]] = []
        tabs: list[Dict[str, Any]] = []
        status_candidates: list[str] = []
        search_controls: list[Dict[str, Any]] = []
        filter_controls: list[Dict[str, Any]] = []
        toolbar_controls: list[Dict[str, Any]] = []
        navigation_controls: list[Dict[str, Any]] = []
        primary_actions: list[Dict[str, Any]] = []
        interactive_labels_preview: list[str] = []
        region_summaries: dict[str, Dict[str, Any]] = {}
        region_counts = {"dialog": 0, "menu": 0, "listbox": 0, "tab": 0}
        scope_counts = {"overlay": 0, "dialog": 0, "expanded": 0}
        role_counts: Dict[str, int] = {}
        control_type_counts = {
            "button": 0,
            "link": 0,
            "input_like": 0,
            "option_like": 0,
            "tab": 0,
        }
        list_signal_count = 0
        table_signal_count = 0
        status_surfaces: list[Dict[str, Any]] = []
        for raw_line in str(snapshot_text or "").splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            ref_match = re.search(r"\[ref=((?:f\d+)?e\d+)\]", line)
            ref = ref_match.group(1) if ref_match else ""
            summary = re.sub(r"\s*\[ref=((?:f\d+)?e\d+)\]\s*$", "", line)
            summary = summary.lstrip("-").strip()
            normalized_summary = re.sub(r"\s+", " ", summary)
            quoted = re.findall(r'"([^"]+)"', normalized_summary)
            label = quoted[-1].strip() if quoted else ""
            tag_match = re.match(r"([a-z][a-z0-9:_-]*)", normalized_summary.lower())
            tag_name = tag_match.group(1) if tag_match else ""
            if not label and tag_name:
                remainder = re.sub(rf"^{re.escape(tag_name)}(?:\s+{re.escape(tag_name)})?\s*", "", normalized_summary, flags=re.IGNORECASE)
                label = remainder.strip()
            lowered_summary = normalized_summary.lower()
            scope_hint = "overlay" if any(token in lowered_summary for token in ("menu", "dropdown", "popup", "dialog", "sheet", "listbox")) else ""
            if "dialog" in lowered_summary:
                region_counts["dialog"] += 1
            if "menuitem" in lowered_summary or re.search(r"\bmenu\b", lowered_summary):
                region_counts["menu"] += 1
            if "listbox" in lowered_summary:
                region_counts["listbox"] += 1
            if re.search(r"\btab\b", lowered_summary):
                region_counts["tab"] += 1
            if tag_name:
                role_counts[tag_name] = int(role_counts.get(tag_name, 0) or 0) + 1
            if any(token in lowered_summary for token in ("list ", "listitem", "feed", "thread", "row", "collection")):
                list_signal_count += 1
            if any(token in lowered_summary for token in ("table", "grid", "columnheader", "rowheader", "cell")):
                table_signal_count += 1
            if scope_hint:
                scope_counts["overlay"] += 1
            if any(token in lowered_summary for token in ("dialog", "modal", "sheet")):
                scope_counts["dialog"] += 1
            if "expanded" in lowered_summary:
                scope_counts["expanded"] += 1
            summary_status_source = label or normalized_summary
            if summary_status_source and len(summary_status_source) <= 160:
                lowered_status = summary_status_source.lower()
                if any(token in lowered_status for token in STATUS_KEYWORDS):
                    candidate_text = summary_status_source.strip()
                    if candidate_text and candidate_text not in status_candidates:
                        status_candidates.append(candidate_text)
                    if len(status_surfaces) < 12:
                        status_surfaces.append(
                            {
                                "ref": ref,
                                "tag_name": tag_name,
                                "label": label or candidate_text,
                                "summary": summary,
                                "is_custom_element": bool(tag_name and "-" in tag_name),
                                "scope_hint": scope_hint,
                            }
                        )
            if tag_name and "-" in tag_name and tag_name not in custom_elements:
                custom_elements.append(tag_name)
            if tag_name in {"button", "link", "textbox", "input", "select", "option", "menuitem", "checkbox", "radio", "tab"}:
                control = {
                    "ref": ref,
                    "tag_name": tag_name,
                    "label": label,
                    "summary": summary,
                    "is_custom_element": bool(tag_name and "-" in tag_name),
                    "scope_hint": scope_hint,
                }
                interactive_controls.append(control)
                if label and label not in interactive_labels_preview:
                    interactive_labels_preview.append(label)
                if tag_name in {"textbox", "input", "select", "checkbox", "radio"}:
                    form_controls.append(dict(control))
                    control_type_counts["input_like"] += 1
                if tag_name == "button":
                    buttons.append(dict(control))
                    control_type_counts["button"] += 1
                if tag_name == "link":
                    links.append(dict(control))
                    control_type_counts["link"] += 1
                if tag_name in {"option", "menuitem"}:
                    options.append(dict(control))
                    control_type_counts["option_like"] += 1
                if tag_name == "tab":
                    tabs.append(dict(control))
                    control_type_counts["tab"] += 1
                lowered_label = label.lower() if label else ""
                if (
                    lowered_label and any(token in lowered_label for token in SEARCH_KEYWORDS)
                ) or (
                    tag_name in {"textbox", "input"} and any(token in lowered_summary for token in SEARCH_KEYWORDS)
                ):
                    search_controls.append(dict(control))
                if (
                    lowered_label and any(token in lowered_label for token in FILTER_KEYWORDS)
                ) or (
                    any(token in lowered_summary for token in FILTER_KEYWORDS)
                    and tag_name in {"button", "menuitem", "option", "select", "tab"}
                ):
                    filter_controls.append(dict(control))
                if tag_name in {"link", "tab"}:
                    navigation_controls.append(dict(control))
                if tag_name in {"button", "textbox", "input", "select", "tab"} and any(
                    token in lowered_summary for token in ("toolbar", "filter", "search", "sort", "view")
                ):
                    toolbar_controls.append(dict(control))
                if lowered_label and any(token in lowered_label for token in PRIMARY_ACTION_KEYWORDS):
                    primary_actions.append(dict(control))
                if label and len(label) <= 120:
                    if any(token in lowered_label for token in STATUS_KEYWORDS) or any(
                        token in lowered_summary for token in STATUS_KEYWORDS
                    ):
                        if label not in status_candidates:
                            status_candidates.append(label)
                        status_surfaces.append(dict(control))
            if scope_hint or tag_name in {"dialog", "menu", "menuitem", "listbox", "option", "tab", "toolbar", "form"}:
                region_key = scope_hint or tag_name or "generic"
                region_summary = region_summaries.setdefault(
                    region_key,
                    {
                        "region_kind": region_key,
                        "control_count": 0,
                        "labels_preview": [],
                        "refs": [],
                        "control_roles": {},
                    },
                )
                region_summary["control_count"] += 1
                if label and label not in region_summary["labels_preview"] and len(region_summary["labels_preview"]) < 8:
                    region_summary["labels_preview"].append(label)
                if ref and ref not in region_summary["refs"] and len(region_summary["refs"]) < 8:
                    region_summary["refs"].append(ref)
                if tag_name:
                    region_summary["control_roles"][tag_name] = int(region_summary["control_roles"].get(tag_name, 0) or 0) + 1
        comments: list[Dict[str, Any]] = []
        index = 0
        while index < len(cleaned):
            line = cleaned[index]
            match = re.match(r"^(@\S+)\s+.{2,4}\s+(.+)$", line)
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
                if re.match(r"^@\S+\s+.{2,4}\s+.+$", current):
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
        message_items: list[Dict[str, Any]] = []
        result_items: list[Dict[str, Any]] = []
        repository_items: list[Dict[str, Any]] = []
        for idx, line in enumerate(cleaned):
            lowered = line.lower()
            if "@" in line and ("hour" in lowered or "day" in lowered or "minute" in lowered):
                continue
            if re.search(r"\b(inbox|unread|drafts|sent|starred)\b", lowered):
                continue
            if re.search(r"\bfrom[:\s]|subject[:\s]", lowered):
                message_items.append({"label": line, "line_index": idx})
            if "/" in line and re.search(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", line.strip()):
                repository_items.append({"label": line.strip(), "line_index": idx})
            if re.search(r"\b(result|issue|pull request|repository|video|comment)\b", lowered):
                result_items.append({"label": line, "line_index": idx})
        snapshot_refs = re.findall(r"\[ref=((?:f\d+)?e\d+)\]", str(snapshot_text or ""))
        primary_region = ""
        primary_region_count = 0
        for region_name, region_count in {**region_counts, **scope_counts}.items():
            if region_count > primary_region_count:
                primary_region = region_name
                primary_region_count = region_count
        collection_summaries: list[Dict[str, Any]] = []
        if comments:
            collection_summaries.append(
                {
                    "kind": "comment_threads",
                    "count": len(comments),
                    "labels_preview": [item.get("author", "") for item in comments[:4] if str(item.get("author", "") or "")],
                }
            )
        if message_items:
            collection_summaries.append(
                {
                    "kind": "message_list",
                    "count": len(message_items),
                    "labels_preview": [item.get("label", "") for item in message_items[:4] if str(item.get("label", "") or "")],
                }
            )
        if repository_items:
            collection_summaries.append(
                {
                    "kind": "repository_list",
                    "count": len(repository_items),
                    "labels_preview": [item.get("label", "") for item in repository_items[:4] if str(item.get("label", "") or "")],
                }
            )
        if result_items:
            collection_summaries.append(
                {
                    "kind": "result_list",
                    "count": len(result_items),
                    "labels_preview": [item.get("label", "") for item in result_items[:4] if str(item.get("label", "") or "")],
                }
            )
        collection_priority = {
            "comment_threads": 4,
            "message_list": 3,
            "repository_list": 2,
            "result_list": 1,
        }
        collection_summaries = sorted(
            collection_summaries,
            key=lambda item: (
                -int(collection_priority.get(str(item.get("kind", "") or ""), 0) or 0),
                -int(item.get("count", 0) or 0),
                str(item.get("kind", "") or ""),
            ),
        )[:6]
        primary_collection_kind = str(collection_summaries[0].get("kind", "") or "") if collection_summaries else ""
        return {
            "headings": headings,
            "interactive_controls": interactive_controls[:20],
            "interactive_control_count": len(interactive_controls),
            "interactive_labels_preview": interactive_labels_preview[:20],
            "form_controls": form_controls[:20],
            "form_control_count": len(form_controls),
            "button_controls": buttons[:20],
            "button_count": len(buttons),
            "link_controls": links[:20],
            "link_count": len(links),
            "option_controls": options[:20],
            "option_count": len(options),
            "tab_controls": tabs[:20],
            "search_controls": search_controls[:12],
            "search_control_count": len(search_controls),
            "filter_controls": filter_controls[:12],
            "filter_control_count": len(filter_controls),
            "toolbar_controls": toolbar_controls[:12],
            "toolbar_control_count": len(toolbar_controls),
            "navigation_controls": navigation_controls[:12],
            "navigation_control_count": len(navigation_controls),
            "primary_actions": primary_actions[:12],
            "primary_action_count": len(primary_actions),
            "custom_element_preview": custom_elements[:20],
            "custom_element_count": len(custom_elements),
            "comment_threads": comments[:12],
            "comment_thread_count": len(comments),
            "message_items": message_items[:12],
            "message_item_count": len(message_items),
            "repository_items": repository_items[:12],
            "repository_item_count": len(repository_items),
            "result_items": result_items[:12],
            "result_item_count": len(result_items),
            "snapshot_ref_count": len(snapshot_refs),
            "dialog_count": int(region_counts["dialog"]),
            "menu_count": int(region_counts["menu"]),
            "listbox_count": int(region_counts["listbox"]),
            "tab_count": int(region_counts["tab"]),
            "overlay_control_count": int(scope_counts["overlay"]),
            "list_signal_count": int(list_signal_count),
            "table_signal_count": int(table_signal_count),
            "collection_signals": {
                "list_signal_count": int(list_signal_count),
                "table_signal_count": int(table_signal_count),
                "comment_thread_count": len(comments),
                "message_item_count": len(message_items),
                "repository_item_count": len(repository_items),
                "result_item_count": len(result_items),
            },
            "collection_summaries": collection_summaries,
            "primary_collection_kind": primary_collection_kind,
            "region_summaries": sorted(
                [
                    {
                        **summary,
                        "control_roles": dict(
                            sorted(summary.get("control_roles", {}).items(), key=lambda item: (-int(item[1]), str(item[0])))
                        ),
                    }
                    for summary in region_summaries.values()
                ],
                key=lambda item: (-int(item.get("control_count", 0) or 0), str(item.get("region_kind", "") or "")),
            )[:8],
            "status_candidates": status_candidates[:12],
            "status_candidate_count": len(status_candidates),
            "status_surfaces": status_surfaces[:12],
            "status_surface_count": len(status_surfaces),
            "role_counts": dict(sorted(role_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))),
            "control_type_counts": dict(control_type_counts),
            "interaction_region": primary_region,
            "interaction_region_summary": {
                "primary_region": primary_region,
                "dialog_count": int(region_counts["dialog"]),
                "menu_count": int(region_counts["menu"]),
                "listbox_count": int(region_counts["listbox"]),
                "tab_count": int(region_counts["tab"]),
                "overlay_control_count": int(scope_counts["overlay"]),
                "form_control_count": len(form_controls),
                "button_count": len(buttons),
                "link_count": len(links),
                "option_count": len(options),
                "list_signal_count": int(list_signal_count),
                "table_signal_count": int(table_signal_count),
            },
        }

    def _extract_structured_region_data(self, details: Dict[str, Any], subtree_candidates: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        base = details if isinstance(details, dict) else {}
        candidates = [item for item in (subtree_candidates or []) if isinstance(item, dict)]
        links = [item for item in candidates if str(item.get("tag_name", "") or "").lower() == "link"]
        buttons = [item for item in candidates if str(item.get("tag_name", "") or "").lower() == "button"]
        options = [
            item
            for item in candidates
            if str(item.get("tag_name", "") or "").lower() in {"option", "menuitem"}
        ]
        input_like = [
            item
            for item in candidates
            if str(item.get("tag_name", "") or "").lower() in {"textbox", "input", "select", "checkbox", "radio"}
        ]
        texts = []
        role_counts: Dict[str, int] = {}
        interactive_controls: list[Dict[str, Any]] = []
        primary_actions: list[Dict[str, Any]] = []
        search_like_controls: list[Dict[str, Any]] = []
        status_controls: list[Dict[str, Any]] = []
        visible_controls: list[Dict[str, Any]] = []
        overlay_controls: list[Dict[str, Any]] = []
        dialog_controls: list[Dict[str, Any]] = []
        for item in candidates:
            tag_name = str(item.get("tag_name", "") or "").lower()
            if tag_name:
                role_counts[tag_name] = int(role_counts.get(tag_name, 0) or 0) + 1
            label = str(item.get("text", "") or item.get("aria_label", "") or item.get("accessible_name", "") or "").strip()
            if label:
                texts.append(label)
            if tag_name in {"button", "link", "textbox", "input", "select", "option", "menuitem", "checkbox", "radio", "tab"}:
                control = {
                    "target": str(item.get("target", "") or item.get("ref", "") or ""),
                    "ref": str(item.get("ref", "") or ""),
                    "tag_name": tag_name,
                    "label": label,
                    "visible": bool(item.get("visible", False)),
                    "enabled": bool(item.get("enabled", True)),
                }
                interactive_controls.append(control)
                if control["visible"] and len(visible_controls) < 12:
                    visible_controls.append(dict(control))
                if bool(item.get("in_overlay")) and len(overlay_controls) < 8:
                    overlay_controls.append(dict(control))
                if bool(item.get("in_dialog")) and len(dialog_controls) < 8:
                    dialog_controls.append(dict(control))
                lowered_label = label.lower()
                if lowered_label and any(token in lowered_label for token in PRIMARY_ACTION_KEYWORDS):
                    primary_actions.append(dict(control))
                if lowered_label and any(token in lowered_label for token in SEARCH_KEYWORDS + FILTER_KEYWORDS):
                    search_like_controls.append(dict(control))
                if lowered_label and any(token in lowered_label for token in STATUS_KEYWORDS):
                    status_controls.append(dict(control))
        status_candidates = []
        for value in texts:
            lowered = value.lower()
            if any(token in lowered for token in STATUS_KEYWORDS):
                if value not in status_candidates:
                    status_candidates.append(value)
        region_kind = "generic"
        role = str(base.get("role", "") or "").lower()
        if role in {"dialog", "menu", "listbox", "tablist"}:
            region_kind = role
        elif options:
            region_kind = "option_group"
        elif input_like:
            region_kind = "form"
        control_density = 0.0
        if candidates:
            control_density = round(float(len(interactive_controls)) / float(len(candidates)), 3)
        return {
            "target": str(base.get("target", "") or ""),
            "tag_name": str(base.get("tag_name", "") or ""),
            "role": str(base.get("role", "") or ""),
            "visible": bool(base.get("visible", False)),
            "enabled": bool(base.get("enabled", True)),
            "region_kind": region_kind,
            "candidate_count": len(candidates),
            "button_count": len(buttons),
            "link_count": len(links),
            "option_count": len(options),
            "input_like_count": len(input_like),
            "interactive_density": control_density,
            "status_candidates": status_candidates[:12],
            "labels_preview": texts[:20],
            "role_counts": dict(sorted(role_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))),
            "interactive_controls": interactive_controls[:12],
            "visible_controls": visible_controls[:12],
            "overlay_controls": overlay_controls[:8],
            "dialog_controls": dialog_controls[:8],
            "primary_actions": primary_actions[:8],
            "search_like_controls": search_like_controls[:8],
            "status_controls": status_controls[:8],
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
        if not isinstance(page, dict):
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
            "anti_bot": self._build_anti_bot_detection(page),
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
        if not isinstance(normalized_page, dict):
            normalized_page = {}
        if self._capabilities.engine_name == "playwright_cli" and not bool(modal_state.get("visible", False)):
            refreshed_modal_state = self._fallback_modal_state(tab_id=str(tab_id or "").strip())
            if bool(refreshed_modal_state.get("visible", False)):
                modal_state = refreshed_modal_state
        active_tab_id = str(context.get("active_tab_id", "") or normalized_page.get("tab_id", "") or str(tab_id or "").strip())
        structured_page = context.get("structured_page", {})
        if not isinstance(structured_page, dict):
            structured_page = {}
        if not structured_page:
            snapshot_text = ""
            if isinstance(snapshot, dict):
                snapshot_text = str(snapshot.get("snapshot", "") or "")
            if not snapshot_text and callable(getattr(self._raw, "snapshot", None)):
                try:
                    snapshot_payload = (
                        self._raw.snapshot(tab_id=str(tab_id or "").strip())
                        if str(tab_id or "").strip()
                        else self._raw.snapshot()
                    )
                    if isinstance(snapshot_payload, dict):
                        snapshot = snapshot_payload
                        snapshot_text = str(snapshot_payload.get("snapshot", "") or "")
                except Exception:
                    snapshot_text = ""
            page_text = str(context.get("page_text_preview", "") or "")
            if not page_text:
                try:
                    page_text_payload = (
                        self._raw.get_page_text(tab_id=str(tab_id or "").strip())
                        if str(tab_id or "").strip()
                        else self._raw.get_page_text()
                    )
                    if isinstance(page_text_payload, dict):
                        page_text = str(page_text_payload.get("text", "") or "")
                except Exception:
                    page_text = ""
            structured_page = self._extract_structured_page_data(page_text, snapshot_text=snapshot_text)
        interaction_hints = context.get("interaction_hints", {})
        if not isinstance(interaction_hints, dict):
            interaction_hints = {}
        if not interaction_hints:
            interaction_hints = self._build_interaction_hints(structured_page, active_element, modal_state)
        session_health = context.get("session_health", self._build_session_health_snapshot(page_payload=normalized_page))
        if not isinstance(session_health, dict):
            session_health = self._build_session_health_snapshot(page_payload=normalized_page)
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
            "anti_bot": context.get("anti_bot", self._build_anti_bot_detection(normalized_page))
            if isinstance(context.get("anti_bot", None), dict)
            else self._build_anti_bot_detection(normalized_page),
            "snapshot": snapshot or {"unsupported": True, "message": "Structured post-action snapshot is not available in this runtime path."},
            "structured_page": structured_page,
            "interaction_hints": interaction_hints,
            "next_steps": self._next_step_suggestions_for_context(
                action_name=str(context.get("action_name", "") or action_name or "inspect"),
                session_health=session_health,
                structured_page=structured_page,
                interaction_hints=interaction_hints,
                resolution_trace=context.get("resolution_trace", {}) if isinstance(context.get("resolution_trace", {}), dict) else {},
            ),
            "recent_actions": context.get("recent_actions", self._recent_actions_payload(limit=8))
            if isinstance(context.get("recent_actions", None), list)
            else self._recent_actions_payload(limit=8),
            "session_health": session_health,
        }

    def _build_interaction_hints(self, structured_page: Dict[str, Any], active_element: Dict[str, Any], modal_state: Dict[str, Any]) -> Dict[str, Any]:
        primary_actions = structured_page.get("primary_actions", []) if isinstance(structured_page, dict) else []
        search_controls = structured_page.get("search_controls", []) if isinstance(structured_page, dict) else []
        filter_controls = structured_page.get("filter_controls", []) if isinstance(structured_page, dict) else []
        toolbar_controls = structured_page.get("toolbar_controls", []) if isinstance(structured_page, dict) else []
        navigation_controls = structured_page.get("navigation_controls", []) if isinstance(structured_page, dict) else []
        region_summaries = structured_page.get("region_summaries", []) if isinstance(structured_page, dict) else []
        collection_summaries = structured_page.get("collection_summaries", []) if isinstance(structured_page, dict) else []
        active_label = str(
            active_element.get("accessible_name", "")
            or active_element.get("aria_label", "")
            or active_element.get("text", "")
            or active_element.get("value", "")
            or ""
        ).strip()
        current_region = structured_page.get("interaction_region_summary", {}) if isinstance(structured_page, dict) else {}
        return {
            "has_modal": bool(modal_state.get("visible", False)),
            "primary_action_labels": [
                str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
                for item in primary_actions[:6]
                if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
            ],
            "search_control_labels": [
                str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
                for item in search_controls[:4]
                if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
            ],
            "filter_control_labels": [
                str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
                for item in filter_controls[:4]
                if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
            ],
            "navigation_control_labels": [
                str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
                for item in navigation_controls[:4]
                if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
            ],
            "toolbar_control_labels": [
                str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
                for item in toolbar_controls[:4]
                if isinstance(item, dict) and str(item.get("label", "") or item.get("text", "") or item.get("accessible_name", "") or "").strip()
            ],
            "active_element_label": active_label,
            "active_element_role": str(active_element.get("role", "") or active_element.get("control_type", "") or "").strip(),
            "interaction_region": str(structured_page.get("interaction_region", "") or ""),
            "primary_collection_kind": str(structured_page.get("primary_collection_kind", "") or ""),
            "collection_summaries": [
                {
                    "kind": str(item.get("kind", "") or ""),
                    "count": int(item.get("count", 0) or 0),
                    "labels_preview": list(item.get("labels_preview", []))[:4],
                }
                for item in collection_summaries[:4]
                if isinstance(item, dict)
            ],
            "interaction_region_controls": {
                "button_count": int(current_region.get("button_count", 0) or 0),
                "form_control_count": int(current_region.get("form_control_count", 0) or 0),
                "option_count": int(current_region.get("option_count", 0) or 0),
                "overlay_control_count": int(current_region.get("overlay_control_count", 0) or 0),
            },
            "top_regions": [
                {
                    "region_kind": str(item.get("region_kind", "") or ""),
                    "control_count": int(item.get("control_count", 0) or 0),
                    "labels_preview": list(item.get("labels_preview", []))[:4],
                }
                for item in region_summaries[:4]
                if isinstance(item, dict)
            ],
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
            "wait_for_text_change",
            "wait_for_page_stable",
            "watch_page_state",
            "watch_target_state",
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

    def _minimal_post_action_context(
        self,
        action_name: str,
        tab_id: str = "",
        *,
        include_anti_bot: bool = False,
        include_session_health: bool = True,
    ) -> Dict[str, Any]:
        normalized_tab_id = str(tab_id or "").strip()
        try:
            page = (
                self._raw.get_current_url(tab_id=normalized_tab_id)
                if normalized_tab_id
                else self._raw.get_current_url()
            )
        except Exception:
            page = {}
        if not isinstance(page, dict):
            page = {}
        try:
            tabs = list(self._raw.list_tabs().get("tabs", []))
        except Exception:
            tabs = []
        active_tab_id = str(page.get("tab_id", "") or normalized_tab_id or "")
        page_text = ""
        snapshot_text = ""
        try:
            text_payload = (
                self._raw.get_page_text(tab_id=normalized_tab_id)
                if normalized_tab_id
                else self._raw.get_page_text()
            )
            if isinstance(text_payload, dict):
                page_text = str(text_payload.get("text", "") or "")
        except Exception:
            page_text = ""
        snapshot_payload = {"unsupported": True, "message": "post-action context minimized for fast path."}
        if self._capabilities.engine_name == "patchright" and callable(getattr(self._raw, "get_interaction_context", None)):
            try:
                raw_context_payload = (
                    self._raw.get_interaction_context(tab_id=normalized_tab_id)
                    if normalized_tab_id
                    else self._raw.get_interaction_context()
                )
                raw_context = raw_context_payload.get("interaction_context", raw_context_payload) if isinstance(raw_context_payload, dict) else {}
                snapshot_candidate = raw_context.get("snapshot", {}) if isinstance(raw_context, dict) else {}
                if isinstance(snapshot_candidate, dict):
                    snapshot_payload = snapshot_candidate
                    snapshot_text = str(snapshot_candidate.get("snapshot", "") or "")
            except Exception:
                snapshot_payload = {"unsupported": True, "message": "post-action context minimized for fast path."}
        structured_page = self._extract_structured_page_data(page_text, snapshot_text=snapshot_text)
        modal_state = {"visible": False, "count": 0, "primary_dialog": {}, "dialogs": []}
        interaction_hints = self._build_interaction_hints(structured_page, {}, modal_state)
        session_health = (
            self._build_session_health_snapshot(page_payload=page)
            if include_session_health
            else {
                "alive": True,
                "current_url": str(page.get("url", "") or ""),
                "title": str(page.get("title", "") or ""),
                "engine_name": self._capabilities.engine_name,
                "runtime_profile": self._capabilities.runtime_profile,
                "recent_action_count": len(self._recent_actions),
                "recent_failure_count": len([item for item in self._recent_actions if not item.get("ok")]),
                "last_action_name": str(self._recent_actions[-1].get("action_name", "") or "") if self._recent_actions else "",
                "failure_classification": "healthy",
                "recovery_hint": "none",
                "recovery_actions": [],
                "page_drift": self._normalize_page_drift({}),
            }
        )
        return {
            "action_name": str(action_name or "inspect"),
            "page": page,
            "tabs": tabs,
            "active_tab_id": active_tab_id,
            "active_element": {},
            "modal_state": modal_state,
            "anti_bot": self._build_anti_bot_detection(page) if include_anti_bot else self._deferred_anti_bot_snapshot(),
            "snapshot": snapshot_payload,
            "structured_page": structured_page,
            "interaction_hints": interaction_hints,
            "next_steps": self._next_step_suggestions_for_context(
                action_name=str(action_name or "inspect"),
                session_health=session_health,
                structured_page=structured_page,
                interaction_hints=interaction_hints,
                resolution_trace={},
            ),
            "recent_actions": self._recent_actions_payload(limit=4),
            "session_health": session_health,
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
        payload["post_action_context"] = (
            self._build_post_action_context(context_action, tab_id=str(payload.get("tab_id", "") or ""))
            if payload.get("ok") is False or failure
            else self._minimal_post_action_context(
                context_action,
                tab_id=str(payload.get("tab_id", "") or ""),
                include_anti_bot=False,
                include_session_health=True,
            )
        )
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
        if "page stability" in lowered or "page text change" in lowered or "still be rendering" in lowered:
            return "page_not_ready"
        if "timeout" in lowered:
            if "page stability" in lowered or "text change" in lowered:
                return "page_not_ready"
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
            normalized["post_action_context"]["next_steps"] = self._next_step_suggestions_for_context(
                action_name=str(normalized.get("post_action_context", {}).get("action_name", "") or action_name),
                session_health=normalized["post_action_context"].get("session_health", {}),
                structured_page=normalized["post_action_context"].get("structured_page", {}),
                interaction_hints=normalized["post_action_context"].get("interaction_hints", {}),
                resolution_trace=normalized.get("resolution_trace", {}) if isinstance(normalized.get("resolution_trace", {}), dict) else {},
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
            self._remember_structured_context(
                normalized["post_action_context"].get("structured_page", {}),
                normalized["post_action_context"].get("interaction_hints", {}),
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
        if "anti_bot" not in payload or not isinstance(payload.get("anti_bot"), dict):
            page_source = payload.get("page", {})
            if not isinstance(page_source, dict):
                page_source = payload.get("interaction_context", {}).get("page", {}) if isinstance(payload.get("interaction_context"), dict) else {}
            payload["anti_bot"] = self._build_anti_bot_detection(page_source if isinstance(page_source, dict) else {})
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
        if action_name == "diagnose_target":
            details = payload.get("details", {})
            subtree_candidates = payload.get("subtree_candidates", [])
            payload["structured_region"] = self._extract_structured_region_data(
                details if isinstance(details, dict) else {},
                subtree_candidates if isinstance(subtree_candidates, list) else [],
            )
        payload["session_health"] = self._build_session_health_snapshot(page_payload=payload)
        payload["next_steps"] = self._next_step_suggestions_for_context(
            action_name=action_name,
            session_health=payload.get("session_health", {}),
            structured_page=payload.get("structured_page", {}),
            interaction_hints=payload.get("interaction_context", {}).get("interaction_hints", {})
            if isinstance(payload.get("interaction_context", {}), dict)
            else {},
            resolution_trace=payload.get("resolution_trace", {}) if isinstance(payload.get("resolution_trace", {}), dict) else {},
        )
        self._remember_structured_context(
            payload.get("structured_page", {}),
            payload.get("interaction_context", {}).get("interaction_hints", {}) if isinstance(payload.get("interaction_context", {}), dict) else {},
        )
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
        return int(self._candidate_relevance_details(entry, text_filter=text_filter).get("score", -1) or -1)

    def _candidate_scope_boost(self, entry: Dict[str, Any], scope: Dict[str, Any]) -> int:
        return int(self._candidate_scope_details(entry, scope).get("score", 0) or 0)

    def _candidate_scope_details(self, entry: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(entry, dict) or not isinstance(scope, dict):
            return {"score": 0, "reasons": []}
        score = 0
        reasons: list[str] = []
        role = str(entry.get("role", "") or "").lower()
        if scope.get("prefer_overlay") and (entry.get("in_overlay") or role in {"menuitem", "option", "listbox"}):
            score += 120
            reasons.append("prefer_overlay")
        if scope.get("prefer_dialog") and entry.get("in_dialog"):
            score += 90
            reasons.append("prefer_dialog")
        if scope.get("prefer_expanded") and str(entry.get("aria_expanded", "") or "").lower() == "true":
            score += 70
            reasons.append("prefer_expanded")
        if scope.get("prefer_overlay") and any(token in role for token in ("menuitem", "option")):
            score += 60
            reasons.append("overlay_choice_role")
        if scope.get("prefer_expanded") and any(
            token in " ".join(
                [
                    str(entry.get("class", "") or ""),
                    str(entry.get("aria_label", "") or ""),
                    str(entry.get("accessible_name", "") or ""),
                    str(entry.get("text", "") or ""),
                ]
            ).lower()
            for token in ("newest", "latest", "sort", "filter", "apply")
        ):
            score += 40
            reasons.append("expanded_keyword_affinity")
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
                score += 26
                reasons.append(f"recent_hint:{token}")
        return {"score": score, "reasons": reasons}

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
