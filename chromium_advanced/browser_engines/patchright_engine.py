from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from queue import Queue
from typing import Any, Dict

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary
from chromium_advanced.chromium_profile_lib import (
    get_chromium_restore_prompt_suppression_args,
    get_profile_user_data_root,
    now_text,
)
from chromium_advanced.mcp_runtime_config import resolve_mcp_headless, resolve_mcp_start_minimized


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
SNAPSHOT_REF_EXTRACT_PATTERN = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
DEBUG_EVENT_LIMIT = 400
SEARCH_KEYWORDS = ("search", "find", "filter", "query", "keyword")
FILTER_KEYWORDS = ("filter", "sort", "status", "type", "label", "category", "newest", "latest")
PRIMARY_ACTION_KEYWORDS = ("save", "submit", "apply", "confirm", "continue", "next", "open", "run", "create")


def _safe_log(text: str) -> None:
    message = str(text or "")
    data = (message + "\n").encode("utf-8", errors="replace")
    stream = getattr(sys, "stdout", None)
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except Exception:
            pass
    try:
        print(message, flush=True)
    except Exception:
        pass


def _load_patchright():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Patchright is not installed. Install it with: pip install patchright") from exc
    return sync_playwright


def _selector_to_locator(page, selector: str, by: str):
    normalized = str(by or "css").strip().lower()
    if normalized == "css":
        return page.locator(selector)
    if normalized == "xpath":
        return page.locator(f"xpath={selector}")
    if normalized == "id":
        return page.locator(f"#{selector}")
    if normalized == "name":
        return page.locator(f"[name={json.dumps(selector)}]")
    if normalized == "tag":
        return page.locator(selector)
    if normalized == "class":
        return page.locator(f".{selector}")
    if normalized == "link_text":
        return page.locator(f"a:has-text({json.dumps(selector)})")
    if normalized == "partial_link_text":
        return page.locator(f"a:has-text({json.dumps(selector)})")
    raise ValueError(f"unsupported selector type: {by}")


def _raw_target_to_locator(page, target: str):
    lowered = str(target or "").strip().lower()
    if lowered.startswith(("css=", "xpath=", "text=", "aria=")):
        return page.locator(str(target).strip())
    return None


def _is_snapshot_ref(value: str) -> bool:
    return bool(SNAPSHOT_REF_PATTERN.match(str(value or "").strip()))


def _extract_snapshot_refs(snapshot_text: str) -> list[str]:
    refs: list[str] = []
    seen = set()
    for match in SNAPSHOT_REF_EXTRACT_PATTERN.finditer(snapshot_text or ""):
        ref = match.group(1)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _describe_locator(locator) -> Dict:
    return locator.evaluate(
        """
        el => ({
          tag_name: (el.tagName || '').toLowerCase(),
          text: (el.innerText || el.textContent || '').trim(),
          text_preview: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 180),
          id: el.id || '',
          name: el.getAttribute('name') || '',
          class: el.getAttribute('class') || '',
          aria_label: el.getAttribute('aria-label') || '',
          aria_expanded: el.getAttribute('aria-expanded') || '',
          aria_haspopup: el.getAttribute('aria-haspopup') || '',
          role: el.getAttribute('role') || '',
          value: 'value' in el ? (el.value || '') : '',
          href: el.getAttribute('href') || '',
          placeholder: el.getAttribute('placeholder') || '',
          title_attr: el.getAttribute('title') || '',
          input_type: el.getAttribute('type') || '',
          accessible_name: String(
            el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') ||
            el.getAttribute('title') ||
            el.innerText ||
            el.textContent ||
            ''
          ).replace(/\s+/g, ' ').trim(),
          control_type: String(el.getAttribute('role') || el.getAttribute('type') || el.tagName || '').toLowerCase(),
          outer_html: el.outerHTML || ''
        })
        """
    )


def _safe_bounding_box(locator) -> Dict:
    try:
        box = locator.bounding_box()
    except Exception:
        box = None
    if not box:
        return {}
    return {
        "x": box.get("x"),
        "y": box.get("y"),
        "width": box.get("width"),
        "height": box.get("height"),
    }


def _normalize_candidate_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _is_interactive_candidate(details: Dict) -> bool:
    tag_name = str(details.get("tag_name", "") or "").lower()
    role = str(details.get("role", "") or "").lower()
    return tag_name in {"a", "button", "input", "textarea", "select", "summary", "option"} or role in {
        "button",
        "link",
        "textbox",
        "combobox",
        "dialog",
        "menuitem",
        "checkbox",
        "radio",
        "switch",
        "tab",
    }


def _should_skip_large_container(details: Dict, lowered_filter: str) -> bool:
    if not lowered_filter:
        return False
    if _is_interactive_candidate(details):
        return False
    if str(details.get("aria_label", "") or "").strip():
        return False
    if str(details.get("href", "") or "").strip():
        return False
    tag_name = str(details.get("tag_name", "") or "").lower()
    text = str(details.get("text", "") or "")
    return tag_name in {"div", "main", "section", "article", "body"} and len(text) > 500


def _candidate_sort_key(item: Dict) -> tuple:
    interactive = _is_interactive_candidate(item)
    has_aria = bool(str(item.get("aria_label", "") or "").strip())
    has_role = bool(str(item.get("role", "") or "").strip())
    has_href = bool(str(item.get("href", "") or "").strip())
    visible = bool(item.get("visible"))
    enabled = bool(item.get("enabled"))
    accessible_name = str(item.get("accessible_name", "") or "").strip()
    text_preview = str(item.get("text_preview", "") or "").strip()
    role = str(item.get("role", "") or "").strip().lower()
    control_type = str(item.get("control_type", "") or "").strip().lower()
    aria_expanded = str(item.get("aria_expanded", "") or "").strip().lower()
    aria_haspopup = str(item.get("aria_haspopup", "") or "").strip().lower()
    tag_name = str(item.get("tag_name", "") or "").strip().lower()
    in_overlay = bool(item.get("in_overlay")) or "overlay" in " ".join(str(x or "") for x in (item.get("scope_tags") or [] if isinstance(item.get("scope_tags"), list) else []))
    in_dialog = bool(item.get("in_dialog"))
    popup_like = role in {"menuitem", "option", "tab"} or control_type in {"menuitem", "option", "tab"} or aria_haspopup in {"menu", "listbox", "dialog", "grid", "tree"}
    expanded = aria_expanded == "true"
    has_text_signal = bool(accessible_name or text_preview)
    text_len = len(str(item.get("text", "") or ""))
    return (
        0 if visible else 1,
        0 if enabled else 1,
        0 if popup_like else 1,
        0 if expanded else 1,
        0 if in_overlay else 1,
        0 if in_dialog else 1,
        0 if interactive else 1,
        0 if has_text_signal else 1,
        0 if has_aria else 1,
        0 if has_role else 1,
        0 if has_href else 1,
        0 if tag_name in {"button", "a", "input", "textarea", "select", "option"} else 1,
        text_len,
    )


def _semantic_candidate_score(item: Dict) -> int:
    if not isinstance(item, dict):
        return 0
    score = 0
    tag_name = str(item.get("tag_name", "") or "").strip().lower()
    role = str(item.get("role", "") or "").strip().lower()
    label = " ".join(
        [
            str(item.get("accessible_name", "") or ""),
            str(item.get("aria_label", "") or ""),
            str(item.get("text_preview", "") or ""),
            str(item.get("text", "") or ""),
            str(item.get("placeholder", "") or ""),
        ]
    ).strip().lower()
    if any(token in label for token in PRIMARY_ACTION_KEYWORDS):
        score += 120
    if any(token in label for token in SEARCH_KEYWORDS):
        score += 85
    if any(token in label for token in FILTER_KEYWORDS):
        score += 95
    if role in {"menuitem", "option", "tab"}:
        score += 70
    if tag_name in {"button", "input", "select", "textarea", "a"}:
        score += 25
    if bool(item.get("in_overlay")):
        score += 55
    if bool(item.get("in_dialog")):
        score += 35
    if str(item.get("aria_expanded", "") or "").strip().lower() == "true":
        score += 40
    if str(item.get("aria_haspopup", "") or "").strip():
        score += 30
    return score


class PatchrightBrowserSession(BrowserSession):
    def __init__(self, playwright_ctx, browser_context, page):
        self.engine_name = "patchright"
        self._playwright_ctx = playwright_ctx
        self.context = browser_context
        self.page = page
        self._last_snapshot_text = ""
        self._last_snapshot_refs: set[str] = set()
        self._tab_ids_by_page_key: dict[int, str] = {}
        self._cdp_sessions_by_page_key: dict[int, Any] = {}
        self._next_tab_number = 1
        self._console_messages: list[Dict[str, Any]] = []
        self._page_errors: list[Dict[str, Any]] = []
        self._network_requests: list[Dict[str, Any]] = []
        self._network_request_ids: dict[tuple[int, int], str] = {}
        self._next_request_number = 1
        self._attach_existing_pages()
        try:
            self.context.on("page", self._handle_new_page)
        except Exception:
            pass

    def _append_limited(self, bucket: list[Dict[str, Any]], payload: Dict[str, Any]) -> None:
        bucket.append(payload)
        overflow = len(bucket) - DEBUG_EVENT_LIMIT
        if overflow > 0:
            del bucket[:overflow]

    def _make_timestamp(self) -> float:
        return round(time.time(), 3)

    def _get_page_key(self, page) -> int:
        return id(page)

    def _get_tab_id(self, page) -> str:
        page_key = self._get_page_key(page)
        tab_id = self._tab_ids_by_page_key.get(page_key)
        if tab_id:
            return tab_id
        tab_id = f"tab-{self._next_tab_number:03d}"
        self._next_tab_number += 1
        self._tab_ids_by_page_key[page_key] = tab_id
        return tab_id

    def _attach_existing_pages(self) -> None:
        for existing_page in list(getattr(self.context, "pages", []) or []):
            self._attach_page(existing_page)

    def _handle_new_page(self, page) -> None:
        self._attach_page(page)

    def _attach_page(self, page) -> None:
        page_key = self._get_page_key(page)
        if page_key in self._tab_ids_by_page_key:
            return
        tab_id = self._get_tab_id(page)

        try:
            cdp = self.context.new_cdp_session(page)
            cdp.send("Runtime.enable")
            cdp.send("Log.enable")
            cdp.send("Network.enable")
            self._cdp_sessions_by_page_key[page_key] = cdp

            def handle_runtime_console(params) -> None:
                args = params.get("args", []) or []
                parts = []
                for item in args:
                    value = item.get("value")
                    if value is not None:
                        parts.append(str(value))
                        continue
                    description = item.get("description")
                    if description:
                        parts.append(str(description))
                stack_frames = ((params.get("stackTrace") or {}).get("callFrames") or [])
                first_frame = stack_frames[0] if stack_frames else {}
                payload = {
                    "timestamp": round(float(params.get("timestamp", 0) or 0) / 1000.0, 3) or self._make_timestamp(),
                    "tab_id": tab_id,
                    "type": str(params.get("type", "") or ""),
                    "text": " ".join(parts).strip(),
                    "location": {
                        "url": str(first_frame.get("url", "") or ""),
                        "line_number": first_frame.get("lineNumber"),
                        "column_number": first_frame.get("columnNumber"),
                    },
                }
                self._append_limited(self._console_messages, payload)

            def handle_runtime_exception(params) -> None:
                details = params.get("exceptionDetails", {}) or {}
                exception = details.get("exception", {}) or {}
                message = (
                    str(exception.get("description", "") or "")
                    or str(exception.get("value", "") or "")
                    or str(details.get("text", "") or "")
                )
                payload = {
                    "timestamp": round(float(params.get("timestamp", 0) or 0) / 1000.0, 3) or self._make_timestamp(),
                    "tab_id": tab_id,
                    "message": message.strip(),
                    "line_number": details.get("lineNumber"),
                    "column_number": details.get("columnNumber"),
                    "url": str(details.get("url", "") or ""),
                }
                self._append_limited(self._page_errors, payload)

            def handle_log_entry(params) -> None:
                entry = params.get("entry", {}) or {}
                level = str(entry.get("level", "") or "")
                text = str(entry.get("text", "") or "")
                payload = {
                    "timestamp": round(float(entry.get("timestamp", 0) or 0) / 1000.0, 3) or self._make_timestamp(),
                    "tab_id": tab_id,
                    "type": level,
                    "text": text,
                    "location": {
                        "url": str(entry.get("url", "") or ""),
                        "line_number": entry.get("lineNumber"),
                        "column_number": None,
                    },
                }
                self._append_limited(self._console_messages, payload)
                if level.lower() in {"error", "warning"}:
                    self._append_limited(
                        self._page_errors,
                        {
                            "timestamp": payload["timestamp"],
                            "tab_id": tab_id,
                            "message": text,
                            "url": payload["location"]["url"],
                            "line_number": payload["location"]["line_number"],
                            "column_number": payload["location"]["column_number"],
                        },
                    )

            def handle_request_will_be_sent(params) -> None:
                request = params.get("request", {}) or {}
                request_id = str(params.get("requestId", "") or "")
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": tab_id,
                    "request_id": request_id,
                    "event": "request",
                    "method": str(request.get("method", "") or ""),
                    "url": str(request.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": bool(params.get("documentURL")),
                    "status": None,
                    "ok": None,
                    "failure": "",
                }
                self._append_limited(self._network_requests, payload)

            def handle_response_received(params) -> None:
                response = params.get("response", {}) or {}
                status = response.get("status")
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": tab_id,
                    "request_id": str(params.get("requestId", "") or ""),
                    "event": "response",
                    "method": "",
                    "url": str(response.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": False,
                    "status": int(status) if isinstance(status, (int, float)) else None,
                    "ok": bool(status < 400) if isinstance(status, (int, float)) else None,
                    "failure": "",
                }
                self._append_limited(self._network_requests, payload)

            def handle_loading_failed(params) -> None:
                payload = {
                    "timestamp": self._make_timestamp(),
                    "tab_id": tab_id,
                    "request_id": str(params.get("requestId", "") or ""),
                    "event": "requestfailed",
                    "method": "",
                    "url": str(params.get("url", "") or ""),
                    "resource_type": str(params.get("type", "") or ""),
                    "navigation": False,
                    "status": None,
                    "ok": False,
                    "failure": str(params.get("errorText", "") or ""),
                }
                self._append_limited(self._network_requests, payload)

            cdp.on("Runtime.consoleAPICalled", handle_runtime_console)
            cdp.on("Runtime.exceptionThrown", handle_runtime_exception)
            cdp.on("Log.entryAdded", handle_log_entry)
            cdp.on("Network.requestWillBeSent", handle_request_will_be_sent)
            cdp.on("Network.responseReceived", handle_response_received)
            cdp.on("Network.loadingFailed", handle_loading_failed)
        except Exception:
            pass

    def _live_pages(self) -> list:
        pages = []
        for page in list(getattr(self.context, "pages", []) or []):
            try:
                if not page.is_closed():
                    pages.append(page)
            except Exception:
                continue
        if not pages and getattr(self, "page", None) is not None:
            try:
                if not self.page.is_closed():
                    pages.append(self.page)
            except Exception:
                pass
        return pages

    def _resolve_page(self, tab_id: str = "", index: int = -1, title_contains: str = "", url_contains: str = ""):
        pages = self._live_pages()
        if not pages:
            raise RuntimeError("No live tabs are available in the current session.")
        normalized_tab_id = str(tab_id or "").strip()
        if normalized_tab_id:
            for page in pages:
                if self._get_tab_id(page) == normalized_tab_id:
                    return page
            raise ValueError(f"Tab not found: {normalized_tab_id}")
        if int(index) >= 0:
            if int(index) >= len(pages):
                raise ValueError(f"Tab index out of range: {index}")
            return pages[int(index)]
        title_filter = str(title_contains or "").strip().lower()
        if title_filter:
            for page in pages:
                try:
                    if title_filter in str(page.title() or "").lower():
                        return page
                except Exception:
                    continue
        url_filter = str(url_contains or "").strip().lower()
        if url_filter:
            for page in pages:
                try:
                    if url_filter in str(page.url or "").lower():
                        return page
                except Exception:
                    continue
        active_page = getattr(self, "page", None)
        if active_page is not None:
            try:
                if not active_page.is_closed():
                    return active_page
            except Exception:
                pass
        return pages[0]

    def _tab_entry(self, page, index: int) -> Dict[str, Any]:
        url = ""
        title = ""
        alive = True
        try:
            url = str(page.url or "")
        except Exception:
            alive = False
        try:
            title = str(page.title() or "")
        except Exception:
            alive = False
        return {
            "tab_id": self._get_tab_id(page),
            "index": int(index),
            "url": url,
            "title": title,
            "active": page == getattr(self, "page", None),
            "alive": bool(alive),
        }

    def get_summary(self) -> BrowserSessionSummary:
        try:
            return BrowserSessionSummary(current_url=self.page.url or "", title=self.page.title() or "", alive=not self.page.is_closed())
        except Exception:
            return BrowserSessionSummary(alive=False)

    def get_capabilities(self) -> Dict:
        return {
            "engine_name": "patchright",
            "supports_snapshot": True,
            "supports_snapshot_refs": True,
            "supports_target_actions": True,
            "supports_selector_actions": True,
            "supports_highlight": True,
            "supports_coordinates": True,
            "supports_gesture_actions": True,
            "supports_post_action_context": True,
            "supports_tabs": True,
            "supports_console_messages": True,
            "supports_page_errors": True,
            "supports_network_requests": True,
        }

    def _update_snapshot_cache(self, snapshot_text: str) -> list[str]:
        refs = _extract_snapshot_refs(snapshot_text)
        self._last_snapshot_text = snapshot_text or ""
        self._last_snapshot_refs = set(refs)
        return refs

    def _resolve_target_locator(self, target: str, by: str = "css", element: str = "", page=None):
        target = str(target or "").strip()
        if not target:
            raise ValueError("target is required")
        current_page = page or self._resolve_page()
        if _is_snapshot_ref(target):
            if self._last_snapshot_refs and target not in self._last_snapshot_refs:
                raise ValueError(f"Ref {target} not found in the cached snapshot. Capture a fresh browser_snapshot first.")
            locator = current_page.locator(f"aria-ref={target}")
        else:
            locator = _raw_target_to_locator(current_page, target) or _selector_to_locator(current_page, target, by)
        locator = locator.first
        if str(element or "").strip():
            locator = locator.describe(str(element).strip())
        return locator

    def _safe_page_snapshot(self, page=None, depth: int = 4, max_chars: int = 5000, update_cache: bool = False) -> Dict:
        current_page = page or self._resolve_page()
        try:
            snapshot_text = current_page.aria_snapshot(mode="ai", depth=depth, boxes=False)
            refs = _extract_snapshot_refs(snapshot_text)
            if update_cache:
                self._update_snapshot_cache(snapshot_text)
            truncated = len(snapshot_text) > max_chars
            if truncated:
                snapshot_text = snapshot_text[:max_chars] + "\n...[truncated]"
            return {
                "depth": depth,
                "ref_count": len(refs),
                "refs": refs[:100],
                "snapshot": snapshot_text,
                "truncated": truncated,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _compact_element_details(self, details: Dict, text_limit: int = 240, html_limit: int = 600) -> Dict:
        compact = dict(details or {})
        compact["text"] = _truncate_text(compact.get("text", ""), text_limit)
        compact["outer_html"] = _truncate_text(compact.get("outer_html", ""), html_limit)
        return compact

    def _safe_tabs_summary(self) -> list[Dict]:
        tabs = []
        try:
            for index, page in enumerate(self._live_pages()):
                tabs.append(self._tab_entry(page, index))
        except Exception as exc:
            tabs.append({"error": str(exc)})
        return tabs

    def _safe_modal_state(self, page=None) -> Dict:
        current_page = page or self._resolve_page()
        try:
            payload = current_page.evaluate(
                """
                () => {
                  const normalizeText = value => String(value || '').replace(/\\s+/g, ' ').trim();
                  const isVisible = el => {
                    if (!el) {
                      return false;
                    }
                    const style = window.getComputedStyle(el);
                    if (!style || style.visibility === 'hidden' || style.display === 'none') {
                      return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const selectors = [
                    'dialog[open]',
                    '[role="dialog"]',
                    'details-dialog',
                    '.Overlay--modal',
                    '.Popover-message'
                  ];
                  const matches = [];
                  for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                      if (!isVisible(el)) {
                        continue;
                      }
                      const rect = el.getBoundingClientRect();
                      matches.push({
                        tag_name: (el.tagName || '').toLowerCase(),
                        role: normalizeText(el.getAttribute('role') || ''),
                        aria_label: normalizeText(el.getAttribute('aria-label') || ''),
                        text: normalizeText(el.innerText || el.textContent || '').slice(0, 600),
                        box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
                      });
                    }
                  }
                  return matches;
                }
                """
            )
            unique_dialogs = []
            seen = set()
            for item in payload or []:
                box = item.get("box") or {}
                key = (
                    str(item.get("tag_name", "") or ""),
                    str(item.get("role", "") or ""),
                    str(item.get("aria_label", "") or ""),
                    str(item.get("text", "") or ""),
                    round(float(box.get("x", 0) or 0), 1),
                    round(float(box.get("y", 0) or 0), 1),
                    round(float(box.get("width", 0) or 0), 1),
                    round(float(box.get("height", 0) or 0), 1),
                )
                if key in seen:
                    continue
                seen.add(key)
                unique_dialogs.append(item)
            return {
                "visible": bool(unique_dialogs),
                "count": len(unique_dialogs),
                "primary_dialog": unique_dialogs[0] if unique_dialogs else {},
                "dialogs": unique_dialogs,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _post_action_context(self, action_name: str, page=None) -> Dict:
        current_page = page or self._resolve_page()
        snapshot_payload = self._safe_page_snapshot(page=current_page, depth=4, max_chars=5000, update_cache=False)
        context = {
            "action_name": action_name,
            "page": self.get_current_url(tab_id=self._get_tab_id(current_page)),
            "tabs": self._safe_tabs_summary(),
            "active_tab_id": self._get_tab_id(current_page),
            "active_element": {},
            "modal_state": self._safe_modal_state(page=current_page),
            "snapshot": snapshot_payload,
        }
        try:
            context["active_element"] = self._compact_element_details(
                self.get_active_element(tab_id=self._get_tab_id(current_page)).get("element", {})
            )
        except Exception as exc:
            context["active_element"] = {"error": str(exc)}
        try:
            page_text_payload = self.get_page_text(tab_id=self._get_tab_id(current_page))
            context["page_text_preview"] = str(page_text_payload.get("text", "") or "").strip()[:1200]
        except Exception as exc:
            context["page_text_preview"] = ""
            context["page_text_error"] = str(exc)
        return context

    def _action_error_payload(
        self,
        action_name: str,
        error: Exception,
        *,
        target: str = "",
        selector: str = "",
        by: str = "css",
        text_filter: str = "",
        element: str = "",
        limit: int = 8,
        tab_id: str = "",
    ) -> Dict:
        current_page = None
        try:
            current_page = self._resolve_page(tab_id=tab_id)
        except Exception:
            current_page = None
        payload: Dict[str, Any] = {
            **(self.get_current_url(tab_id=tab_id) if current_page is not None else self.get_current_url()),
            "ok": False,
            "action_name": action_name,
            "error": str(error),
            "error_type": type(error).__name__,
            "post_action_context": self._post_action_context(f"{action_name}_failed", page=current_page) if current_page is not None else {},
        }
        diagnose_target = str(target or selector or "").strip()
        if diagnose_target:
            try:
                payload["diagnosis"] = self.diagnose_target(
                    target=diagnose_target,
                    element=element,
                    by=by,
                    text_filter=text_filter or diagnose_target,
                    limit=limit,
                    tab_id=tab_id,
                )
            except Exception as diag_exc:
                payload["diagnosis_error"] = str(diag_exc)
        elif text_filter:
            try:
                payload["page_candidates"] = self.list_candidates(
                    text_filter=text_filter,
                    limit=limit,
                    include_boxes=True,
                    tab_id=tab_id,
                ).get("candidates", [])
            except Exception as candidate_exc:
                payload["page_candidates_error"] = str(candidate_exc)
        return payload

    def _list_dom_candidates(self, root_locator, lowered_filter: str, limit: int, include_boxes: bool) -> list[Dict]:
        dom_locator = root_locator.locator(
            "button, summary, a, input, textarea, select, option, [role], [aria-label], [title]"
        )
        payload = dom_locator.evaluate_all(
            """
            (elements, maxItems) => {
              const normalizeText = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const isVisible = el => {
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none') {
                  return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const buildSelector = el => {
                if (!el || el.nodeType !== Node.ELEMENT_NODE) {
                  return '';
                }
                if (el.id) {
                  return `#${CSS.escape(el.id)}`;
                }
                const parts = [];
                let current = el;
                while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
                  let part = (current.tagName || '').toLowerCase();
                  const classList = Array.from(current.classList || []).slice(0, 2);
                  if (classList.length) {
                    part += classList.map(name => `.${CSS.escape(name)}`).join('');
                  }
                  const parent = current.parentElement;
                  if (parent) {
                    const sameTagSiblings = Array.from(parent.children).filter(
                      child => (child.tagName || '').toLowerCase() === (current.tagName || '').toLowerCase()
                    );
                    if (sameTagSiblings.length > 1) {
                      const index = sameTagSiblings.indexOf(current) + 1;
                      part += `:nth-of-type(${index})`;
                    }
                  }
                  parts.unshift(part);
                  current = parent;
                }
                return parts.join(' > ');
              };
              const results = [];
              for (const el of elements) {
                if (results.length >= maxItems) {
                  break;
                }
                const rect = el.getBoundingClientRect();
                const text = normalizeText(el.innerText || el.textContent || '');
                const ariaLabel = normalizeText(el.getAttribute('aria-label') || '');
                const title = normalizeText(el.getAttribute('title') || '');
                const role = normalizeText(el.getAttribute('role') || '');
                const placeholder = normalizeText(el.getAttribute('placeholder') || '');
                const name = normalizeText(el.getAttribute('name') || '');
                const titleAttr = normalizeText(el.getAttribute('title') || '');
                const ariaExpanded = normalizeText(el.getAttribute('aria-expanded') || '');
                const ariaHaspopup = normalizeText(el.getAttribute('aria-haspopup') || '');
                const inputType = normalizeText(el.getAttribute('type') || '');
                const value = 'value' in el ? normalizeText(el.value || '') : '';
                const href = normalizeText(el.getAttribute('href') || '');
                const tagName = normalizeText((el.tagName || '').toLowerCase());
                const accessibleName = normalizeText(ariaLabel || placeholder || titleAttr || text || el.textContent || '');
                const candidateText = normalizeText([text, ariaLabel, titleAttr, role, placeholder, name, value].join(' '));
                const ancestorRoles = [];
                let overlayDepth = 0;
                let dialogDepth = 0;
                let current = el.parentElement;
                while (current) {
                  const ancestorRole = normalizeText(current.getAttribute('role') || '');
                  if (ancestorRole) {
                    ancestorRoles.push(ancestorRole);
                  }
                  const className = normalizeText(current.getAttribute('class') || '');
                  if (ancestorRole === 'dialog' || className.includes('dialog') || className.includes('modal')) {
                    dialogDepth += 1;
                  }
                  if (
                    ancestorRole === 'menu' ||
                    ancestorRole === 'listbox' ||
                    className.includes('overlay') ||
                    className.includes('dropdown') ||
                    className.includes('popup') ||
                    className.includes('menu')
                  ) {
                    overlayDepth += 1;
                  }
                  current = current.parentElement;
                }
                if (!candidateText && !href && !role) {
                  continue;
                }
                results.push({
                  source: 'dom',
                  target: `css=${buildSelector(el)}`,
                  by: 'css',
                  visible: isVisible(el),
                  enabled: !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true',
                  tag_name: tagName,
                  text,
                  text_preview: text.slice(0, 180),
                  id: normalizeText(el.id || ''),
                  name,
                  class: normalizeText(el.getAttribute('class') || ''),
                  aria_label: ariaLabel,
                  title: titleAttr,
                  title_attr: titleAttr,
                  role,
                  placeholder,
                  value,
                  href,
                  aria_expanded: ariaExpanded,
                  aria_haspopup: ariaHaspopup,
                  input_type: inputType,
                  accessible_name: accessibleName,
                  control_type: normalizeText(role || inputType || tagName),
                  overlay_ancestry: ancestorRoles.filter(value => value === 'menu' || value === 'listbox' || value === 'dialog'),
                  in_overlay: overlayDepth > 0,
                  in_dialog: dialogDepth > 0,
                  scope_tags: [
                    ...(overlayDepth > 0 ? ['overlay'] : []),
                    ...(dialogDepth > 0 ? ['dialog'] : []),
                    ...(ariaExpanded === 'true' ? ['expanded'] : []),
                    ...(ariaHaspopup ? ['popup'] : []),
                  ],
                  outer_html: el.outerHTML || '',
                  box: {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height
                  }
                });
              }
              return results;
            }
            """,
            max(50, int(limit) * 4),
        )
        candidates = []
        seen_targets = set()
        for item in payload or []:
            target = str(item.get("target", "") or "").strip()
            if not target or target in seen_targets:
                continue
            searchable_text = " ".join(
                [
                    str(item.get("text", "") or ""),
                    str(item.get("aria_label", "") or ""),
                    str(item.get("title", "") or ""),
                    str(item.get("role", "") or ""),
                    str(item.get("placeholder", "") or ""),
                    str(item.get("name", "") or ""),
                ]
            ).strip()
            if lowered_filter and lowered_filter not in searchable_text.lower():
                continue
            seen_targets.add(target)
            if not include_boxes:
                item.pop("box", None)
            candidates.append(item)
            if len(candidates) >= max(1, int(limit)):
                break
        return candidates

    def list_tabs(self) -> Dict:
        tabs = self._safe_tabs_summary()
        active_tab_id = ""
        for item in tabs:
            if item.get("active"):
                active_tab_id = str(item.get("tab_id", "") or "")
                break
        return {**self.get_current_url(), "active_tab_id": active_tab_id, "count": len(tabs), "tabs": tabs}

    def open_tab(
        self,
        url: str = "",
        activate: bool = True,
        wait_for_ready: bool = True,
        timeout_seconds: int = 20,
    ) -> Dict:
        page = self.context.new_page()
        self._attach_page(page)
        if str(url or "").strip():
            wait_until = "load" if wait_for_ready else "domcontentloaded"
            page.goto(str(url).strip(), wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
        if activate:
            self.page = page
            try:
                page.bring_to_front()
            except Exception:
                pass
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "opened": True,
            "activated": bool(activate),
            "tab": self._tab_entry(page, self._live_pages().index(page)),
            "tabs": self._safe_tabs_summary(),
        }

    def activate_tab(
        self,
        tab_id: str = "",
        index: int = -1,
        title_contains: str = "",
        url_contains: str = "",
    ) -> Dict:
        page = self._resolve_page(tab_id=tab_id, index=index, title_contains=title_contains, url_contains=url_contains)
        self.page = page
        try:
            page.bring_to_front()
        except Exception:
            pass
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "activated": True,
            "tab": self._tab_entry(page, self._live_pages().index(page)),
            "tabs": self._safe_tabs_summary(),
        }

    def close_tab(self, tab_id: str = "", index: int = -1) -> Dict:
        page = self._resolve_page(tab_id=tab_id, index=index)
        closing_tab = self._tab_entry(page, self._live_pages().index(page))
        page.close()
        remaining_pages = self._live_pages()
        if remaining_pages:
            if self.page == page:
                self.page = remaining_pages[0]
        else:
            self.page = self.context.new_page()
            self._attach_page(self.page)
        return {
            **self.get_current_url(),
            "closed": True,
            "closed_tab": closing_tab,
            "tabs": self._safe_tabs_summary(),
        }

    def resize(self, width: int, height: int) -> Dict:
        page = self._resolve_page()
        target_width = max(320, int(width))
        target_height = max(240, int(height))
        try:
            self.page.set_viewport_size({"width": target_width, "height": target_height})
        except Exception:
            page.set_viewport_size({"width": target_width, "height": target_height})
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "resized": True,
            "width": target_width,
            "height": target_height,
            "tabs": self._safe_tabs_summary(),
        }

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        wait_until = "load" if wait_for_ready else "domcontentloaded"
        page.goto(url, wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "post_action_context": self._post_action_context("navigate", page=page),
        }

    def get_current_url(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        return {"tab_id": self._get_tab_id(page), "url": page.url, "title": page.title()}

    def get_page_text(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        text = page.locator("body").inner_text(timeout=15000).strip()
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "text": text}

    def get_page_html(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "html": page.content()}

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        locator = _selector_to_locator(page, selector, by)
        count = locator.count()
        elements = []
        for index in range(min(max(1, int(limit)), count)):
            try:
                elements.append(_describe_locator(locator.nth(index)))
            except Exception:
                continue
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "count": count, "elements": elements}

    def get_active_element(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        element = page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return {};
              return {
                tag_name: (el.tagName || '').toLowerCase(),
                text: (el.innerText || el.textContent || '').trim(),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                class: el.getAttribute('class') || '',
                aria_label: el.getAttribute('aria-label') || '',
                role: el.getAttribute('role') || '',
                value: 'value' in el ? (el.value || '') : '',
                href: el.getAttribute('href') || '',
                outer_html: el.outerHTML || ''
              };
            }
            """
        )
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "element": element}

    def get_interaction_context(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "interaction_context": self._post_action_context("inspect", page=page),
        }

    def snapshot(
        self,
        target: str = "",
        by: str = "css",
        depth: int | None = None,
        boxes: bool = False,
        filename: str = "",
        tab_id: str = "",
    ) -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        locator = None
        resolved_target = str(target or "").strip()
        if resolved_target:
            locator = self._resolve_target_locator(resolved_target, by=by, page=page)
            snapshot_text = locator.aria_snapshot(mode="ai", depth=depth, boxes=bool(boxes))
        else:
            snapshot_text = page.aria_snapshot(mode="ai", depth=depth, boxes=bool(boxes))
        refs = self._update_snapshot_cache(snapshot_text)
        result = {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "target": resolved_target,
            "depth": depth,
            "boxes": bool(boxes),
            "ref_count": len(refs),
            "refs": refs,
            "snapshot": snapshot_text,
        }
        output_path = str(filename or "").strip()
        if output_path:
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write(snapshot_text)
            result["path"] = output_path
        return result

    def list_candidates(
        self,
        target: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 25,
        include_boxes: bool = True,
        tab_id: str = "",
    ) -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        resolved_target = str(target or "").strip()
        locator = None
        if resolved_target:
            locator = self._resolve_target_locator(resolved_target, by=by, page=page)
            snapshot_text = locator.aria_snapshot(mode="ai", boxes=False)
        else:
            snapshot_text = page.aria_snapshot(mode="ai", boxes=False)
        refs = _extract_snapshot_refs(snapshot_text)
        lowered_filter = str(text_filter or "").strip().lower()
        candidates = []
        seen_targets = set()
        for ref in refs:
            try:
                ref_locator = page.locator(f"aria-ref={ref}").first
                details = _describe_locator(ref_locator)
                visible = bool(ref_locator.is_visible())
                enabled = bool(ref_locator.is_enabled())
                candidate_text = " ".join(
                    [
                        str(details.get("text", "") or ""),
                        str(details.get("aria_label", "") or ""),
                        str(details.get("role", "") or ""),
                    ]
                ).strip()
                if lowered_filter and lowered_filter not in candidate_text.lower():
                    continue
                if _should_skip_large_container(details, lowered_filter):
                    continue
                entry = {
                    "source": "snapshot_ref",
                    "target": ref,
                    "by": "css",
                    "ref": ref,
                    "visible": visible,
                    "enabled": enabled,
                    **details,
                }
                if include_boxes:
                    entry["box"] = _safe_bounding_box(ref_locator)
                seen_targets.add(ref)
                candidates.append(entry)
            except Exception:
                continue
        if len(candidates) < max(1, int(limit)):
            root_locator = locator or page.locator("body")
            for entry in self._list_dom_candidates(
                root_locator=root_locator,
                lowered_filter=lowered_filter,
                limit=max(1, int(limit)) * 2,
                include_boxes=include_boxes,
            ):
                target = str(entry.get("target", "") or "").strip()
                if not target or target in seen_targets:
                    continue
                seen_targets.add(target)
                candidates.append(entry)
        for entry in candidates:
            entry["match_score"] = _semantic_candidate_score(entry)
        candidates = sorted(
            candidates,
            key=lambda item: (-int(item.get("match_score", 0) or 0),) + _candidate_sort_key(item),
        )[: max(1, int(limit))]
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "target": resolved_target,
            "text_filter": str(text_filter or ""),
            "count": len(candidates),
            "candidates": candidates,
        }

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        try:
            page = self._resolve_page()
            locator = _selector_to_locator(page, selector, by).first
            target_state = {"present": "attached", "visible": "visible", "clickable": "visible"}.get(condition, "visible")
            locator.wait_for(state=target_state, timeout=int(timeout_seconds) * 1000)
            item = _describe_locator(locator)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "found": True,
                "tag_name": item.get("tag_name", ""),
                "text": item.get("text", ""),
            }
        except Exception as exc:
            return self._action_error_payload("wait_for", exc, selector=selector, by=by, text_filter=selector)

    def wait_for_text(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        try:
            page = self._resolve_page(tab_id=tab_id)
            locator = page.get_by_text(str(text), exact=False).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "found": True,
                "text": str(text or ""),
                "match_type": "text_visible",
            }
        except Exception as exc:
            return self._action_error_payload("wait_for_text", exc, text_filter=str(text or ""))

    def wait_for_text_gone(self, text: str, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        try:
            page = self._resolve_page(tab_id=tab_id)
            locator = page.get_by_text(str(text), exact=False).first
            locator.wait_for(state="detached", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "gone": True,
                "text": str(text or ""),
            }
        except Exception:
            try:
                page = self._resolve_page(tab_id=tab_id)
                locator = page.get_by_text(str(text), exact=False).first
                locator.wait_for(state="hidden", timeout=int(timeout_seconds) * 1000)
                return {
                    **self.get_current_url(tab_id=self._get_tab_id(page)),
                    "gone": True,
                    "text": str(text or ""),
                    "match_type": "text_detached",
                }
            except Exception as exc:
                return self._action_error_payload("wait_for_text_gone", exc, text_filter=str(text or ""))

    def wait_for_timeout(self, timeout_ms: int = 0, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        page.wait_for_timeout(max(0, int(timeout_ms)))
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "waited": True,
            "timeout_ms": max(0, int(timeout_ms)),
        }

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        try:
            page = self._resolve_page()
            locator = _selector_to_locator(page, selector, by).first
            locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            locator.click(timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "clicked": True,
                "post_action_context": self._post_action_context("click", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("click", exc, selector=selector, by=by, text_filter=selector)

    def hover(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        try:
            page = self._resolve_page()
            locator = _selector_to_locator(page, selector, by).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            locator.hover(timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "hovered": True,
                "selector": str(selector or "").strip(),
                "by": str(by or "css"),
                "post_action_context": self._post_action_context("hover", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("hover", exc, selector=selector, by=by, text_filter=selector)

    def click_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        double_click: bool = False,
    ) -> Dict:
        try:
            page = self._resolve_page()
            locator = self._resolve_target_locator(target, by=by, element=element, page=page)
            locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            if double_click:
                locator.dblclick(timeout=int(timeout_seconds) * 1000)
            else:
                locator.click(timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "clicked": True,
                "double_click": bool(double_click),
                "target": str(target or "").strip(),
                "post_action_context": self._post_action_context("click_target", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "click_target",
                exc,
                target=target,
                by=by,
                text_filter=target,
                element=element,
            )

    def type_text(
        self,
        selector: str,
        text: str,
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            locator = _selector_to_locator(page, selector, by).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            if clear_first:
                locator.fill("", timeout=int(timeout_seconds) * 1000)
            locator.type(text, timeout=int(timeout_seconds) * 1000)
            if submit:
                locator.press("Enter", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "typed": True,
                "submitted": bool(submit),
                "post_action_context": self._post_action_context("type_text", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("type_text", exc, selector=selector, by=by, text_filter=selector)

    def type_target(
        self,
        target: str,
        text: str,
        element: str = "",
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            locator = self._resolve_target_locator(target, by=by, element=element, page=page)
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            if clear_first:
                locator.fill("", timeout=int(timeout_seconds) * 1000)
            locator.type(text, timeout=int(timeout_seconds) * 1000)
            if submit:
                locator.press("Enter", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "typed": True,
                "submitted": bool(submit),
                "target": str(target or "").strip(),
                "post_action_context": self._post_action_context("type_target", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "type_target",
                exc,
                target=target,
                by=by,
                text_filter=target,
                element=element,
            )

    def type_target_and_verify(
        self,
        target: str,
        text: str,
        element: str = "",
        by: str = "css",
        clear_first: bool = True,
        submit: bool = False,
        timeout_seconds: int = 20,
    ) -> Dict:
        typed = self.type_target(
            target=target,
            text=text,
            element=element,
            by=by,
            clear_first=clear_first,
            submit=submit,
            timeout_seconds=timeout_seconds,
        )
        if not typed.get("typed"):
            return typed
        verified = self.verify_target_value(target=target, expected_value=text, element=element, by=by)
        return {
            **self.get_current_url(),
            "typed": True,
            "verified": bool(verified.get("verified")),
            "submitted": bool(submit),
            "target": str(target or "").strip(),
            "type_result": typed,
            "verify_result": verified,
            "post_action_context": self._post_action_context("type_target_and_verify"),
        }

    def press_key(
        self,
        key: str,
        count: int = 1,
        selector: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            repeat = max(1, int(count))
            if str(selector or "").strip():
                locator = _selector_to_locator(page, selector, by).first
                locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
                locator.focus(timeout=int(timeout_seconds) * 1000)
            for _ in range(repeat):
                page.keyboard.press(str(key))
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "pressed": True,
                "key": key,
                "count": repeat,
                "post_action_context": self._post_action_context("press_key", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("press_key", exc, selector=selector, by=by, text_filter=selector or key)

    def select_option(
        self,
        selector: str,
        values: list[str] | None = None,
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            locator = _selector_to_locator(page, selector, by).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            normalized_values = [str(item) for item in (values or []) if str(item or "")]
            result = locator.select_option(value=normalized_values, timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "selected": True,
                "selector": str(selector or "").strip(),
                "by": str(by or "css"),
                "values": list(result or normalized_values),
                "post_action_context": self._post_action_context("select_option", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("select_option", exc, selector=selector, by=by, text_filter=selector)

    def handle_dialog(self, accept: bool = True, prompt_text: str = "", tab_id: str = "") -> Dict:
        try:
            page = self._resolve_page(tab_id=tab_id)
            holder: Dict[str, Any] = {}

            def _listener(dialog) -> None:
                holder["type"] = str(getattr(dialog, "type", "")() if callable(getattr(dialog, "type", None)) else "")
                holder["message"] = str(getattr(dialog, "message", "")() if callable(getattr(dialog, "message", None)) else "")
                holder["default_value"] = str(getattr(dialog, "default_value", "")() if callable(getattr(dialog, "default_value", None)) else "")
                if bool(accept):
                    dialog.accept(str(prompt_text or "")) if str(prompt_text or "") else dialog.accept()
                else:
                    dialog.dismiss()
                holder["handled"] = True

            page.once("dialog", _listener)
            page.wait_for_timeout(300)
            if not holder.get("handled"):
                raise ValueError("No dialog was observed for the current page.")
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "handled": True,
                "accepted": bool(accept),
                "dismissed": not bool(accept),
                "prompt_text": str(prompt_text or ""),
                "dialog": {
                    "type": str(holder.get("type", "") or ""),
                    "message": str(holder.get("message", "") or ""),
                    "default_value": str(holder.get("default_value", "") or ""),
                },
                "post_action_context": self._post_action_context("handle_dialog", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("handle_dialog", exc, text_filter="dialog", tab_id=tab_id)

    def file_upload(
        self,
        target: str,
        files: list[str] | None = None,
        by: str = "css",
        element: str = "",
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
            normalized_files = [str(item).strip() for item in (files or []) if str(item or "").strip()]
            if not normalized_files:
                raise ValueError("files is required")
            locator.set_input_files(normalized_files, timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "uploaded": True,
                "file_count": len(normalized_files),
                "target": str(target or "").strip(),
                "by": str(by or "css"),
                "files": list(normalized_files),
                "post_action_context": self._post_action_context("file_upload", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("file_upload", exc, target=target, by=by, text_filter=target, element=element)

    def navigate_back(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        try:
            page = self._resolve_page(tab_id=tab_id)
            response = page.go_back(
                wait_until="domcontentloaded" if bool(wait_for_ready) else None,
                timeout=int(timeout_seconds) * 1000,
            )
            current = self.get_current_url(tab_id=self._get_tab_id(page))
            current["navigated"] = "back"
            current["history_changed"] = response is not None
            current["post_action_context"] = self._post_action_context("navigate_back", page=page)
            return current
        except Exception as exc:
            return self._action_error_payload("navigate_back", exc)

    def navigate_forward(self, wait_for_ready: bool = True, timeout_seconds: int = 20, tab_id: str = "") -> Dict:
        try:
            page = self._resolve_page(tab_id=tab_id)
            response = page.go_forward(
                wait_until="domcontentloaded" if bool(wait_for_ready) else None,
                timeout=int(timeout_seconds) * 1000,
            )
            current = self.get_current_url(tab_id=self._get_tab_id(page))
            current["navigated"] = "forward"
            current["history_changed"] = response is not None
            current["post_action_context"] = self._post_action_context("navigate_forward", page=page)
            return current
        except Exception as exc:
            return self._action_error_payload("navigate_forward", exc)

    def drag_target(
        self,
        source_target: str,
        dest_target: str,
        source_element: str = "",
        dest_element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
    ) -> Dict:
        try:
            page = self._resolve_page()
            source_locator = self._resolve_target_locator(source_target, by=by, element=source_element, page=page)
            dest_locator = self._resolve_target_locator(dest_target, by=by, element=dest_element, page=page)
            source_locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            dest_locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            source_locator.drag_to(dest_locator, timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "dragged": True,
                "source_target": str(source_target or "").strip(),
                "dest_target": str(dest_target or "").strip(),
                "by": str(by or "css"),
                "post_action_context": self._post_action_context("drag_target", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "drag_target",
                exc,
                target=source_target,
                by=by,
                text_filter=str(dest_target or source_target or ""),
            )

    def run_script(self, script: str, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        result = page.evaluate(f"() => {{ {script} }}")
        serialization_error = ""
        try:
            serialized = json.loads(json.dumps(result))
            result_state = "value" if serialized is not None else "null"
        except TypeError as exc:
            serialized = str(result)
            serialization_error = str(exc or "")
            result_state = "stringified"
        payload = {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "result": serialized,
            "script_result_state": result_state,
            "script_result_type": type(result).__name__ if result is not None else "NoneType",
        }
        if serialization_error:
            payload["serialization_error"] = serialization_error
            payload["diagnostic_hint"] = "run_script returned a non-JSON-serializable value and was stringified."
        elif result_state == "null":
            payload["diagnostic_hint"] = "run_script returned null."
        return payload

    def get_console_messages(self, tab_id: str = "", limit: int = 100, level: str = "") -> Dict:
        normalized_tab_id = str(tab_id or "").strip()
        normalized_level = str(level or "").strip().lower()
        messages = list(self._console_messages)
        if normalized_tab_id:
            messages = [item for item in messages if str(item.get("tab_id", "") or "") == normalized_tab_id]
        if normalized_level:
            messages = [item for item in messages if str(item.get("type", "") or "").lower() == normalized_level]
        messages = messages[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "level": normalized_level,
            "count": len(messages),
            "messages": messages,
        }

    def get_page_errors(self, tab_id: str = "", limit: int = 100) -> Dict:
        normalized_tab_id = str(tab_id or "").strip()
        errors = list(self._page_errors)
        if normalized_tab_id:
            errors = [item for item in errors if str(item.get("tab_id", "") or "") == normalized_tab_id]
        errors = errors[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "count": len(errors),
            "errors": errors,
        }

    def get_network_requests(self, tab_id: str = "", limit: int = 100, failed_only: bool = False) -> Dict:
        normalized_tab_id = str(tab_id or "").strip()
        requests = list(self._network_requests)
        if normalized_tab_id:
            requests = [item for item in requests if str(item.get("tab_id", "") or "") == normalized_tab_id]
        if bool(failed_only):
            requests = [item for item in requests if item.get("event") == "requestfailed" or item.get("ok") is False]
        requests = requests[-max(1, int(limit)) :]
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "tab_id": normalized_tab_id,
            "failed_only": bool(failed_only),
            "count": len(requests),
            "requests": requests,
        }

    def clear_debug_buffers(self, tab_id: str = "") -> Dict:
        normalized_tab_id = str(tab_id or "").strip()
        if normalized_tab_id:
            self._console_messages = [item for item in self._console_messages if str(item.get("tab_id", "") or "") != normalized_tab_id]
            self._page_errors = [item for item in self._page_errors if str(item.get("tab_id", "") or "") != normalized_tab_id]
            self._network_requests = [item for item in self._network_requests if str(item.get("tab_id", "") or "") != normalized_tab_id]
        else:
            self._console_messages.clear()
            self._page_errors.clear()
            self._network_requests.clear()
            self._network_request_ids.clear()
        return {
            **(self.get_current_url(tab_id=normalized_tab_id) if normalized_tab_id else self.get_current_url()),
            "cleared": True,
            "tab_id": normalized_tab_id,
        }

    def diagnose_page(self, tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        resolved_tab_id = self._get_tab_id(page)
        console_messages = self.get_console_messages(tab_id=resolved_tab_id, limit=20).get("messages", [])
        page_errors = self.get_page_errors(tab_id=resolved_tab_id, limit=20).get("errors", [])
        failed_requests = self.get_network_requests(tab_id=resolved_tab_id, limit=30, failed_only=True).get("requests", [])
        all_requests = self.get_network_requests(tab_id=resolved_tab_id, limit=30, failed_only=False).get("requests", [])
        recent_bad_responses = [
            item
            for item in all_requests
            if item.get("event") == "response" and isinstance(item.get("status"), int) and int(item.get("status")) >= 400
        ]
        return {
            **self.get_current_url(tab_id=resolved_tab_id),
            "tab_id": resolved_tab_id,
            "diagnosis": {
                "interaction_context": self._post_action_context("diagnose_page", page=page),
                "console_messages": console_messages,
                "page_errors": page_errors,
                "failed_requests": failed_requests,
                "bad_responses": recent_bad_responses[-20:],
            },
        }

    def verify_text(self, text: str) -> Dict:
        try:
            page = self._resolve_page()
            locator = page.get_by_text(str(text))
            count = locator.count()
            visible = False
            if count:
                try:
                    visible = bool(locator.first.is_visible())
                except Exception:
                    visible = False
            if not count or not visible:
                raise ValueError(f'Text not visible: "{text}"')
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "text": str(text),
                "expected_text": str(text),
                "count": count,
                "post_action_context": self._post_action_context("verify_text", page=page),
            }
        except Exception as exc:
            return self._action_error_payload("verify_text", exc, text_filter=str(text))

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        try:
            page = self._resolve_page()
            modal_state = self._safe_modal_state(page=page)
            dialogs = modal_state.get("dialogs", [])
            matched = []
            expected_name = str(accessible_name or "").strip().lower()
            expected_text = str(text or "").strip().lower()
            for dialog in dialogs:
                dialog_name = str(dialog.get("aria_label", "") or "").strip().lower()
                dialog_text = str(dialog.get("text", "") or "").strip().lower()
                if expected_name and expected_name not in dialog_name:
                    continue
                if expected_text and expected_text not in dialog_text:
                    continue
                matched.append(dialog)
            if not matched:
                raise ValueError("Dialog not visible or did not match the expected name/text.")
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "count": len(matched),
                "dialog": matched[0],
                "dialogs": matched,
                "expected_accessible_name": str(accessible_name or ""),
                "expected_text": str(text or ""),
                "post_action_context": self._post_action_context("verify_dialog", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "verify_dialog",
                exc,
                text_filter=str(accessible_name or text or "dialog"),
            )

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        try:
            page = self._resolve_page()
            active = self.get_active_element(tab_id=self._get_tab_id(page)).get("element", {})
            if str(target or "").strip():
                locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
                is_active = bool(locator.evaluate("el => el === document.activeElement"))
                if not is_active:
                    raise ValueError(f'Active element did not match target: "{target}"')
                expected = _describe_locator(locator)
                return {
                    **self.get_current_url(tab_id=self._get_tab_id(page)),
                    "verified": True,
                    "matched": True,
                    "target": str(target),
                    "element": expected,
                    "post_action_context": self._post_action_context("verify_active_element", page=page),
                }
            if not active:
                raise ValueError("No active element found.")
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "element": self._compact_element_details(active),
                "post_action_context": self._post_action_context("verify_active_element", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "verify_active_element",
                exc,
                target=target,
                by=by,
                text_filter=target or "active element",
                element=element,
            )

    def verify_target_value(self, target: str, expected_value: str, element: str = "", by: str = "css") -> Dict:
        try:
            page = self._resolve_page()
            locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
            details = _describe_locator(locator)
            actual_value = str(details.get("value", "") or "")
            if actual_value != str(expected_value):
                raise ValueError(f'Value mismatch for target "{target}": expected "{expected_value}", got "{actual_value}"')
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "target": str(target or "").strip(),
                "expected_value": str(expected_value),
                "actual_value": actual_value,
                "by": str(by or "css"),
                "post_action_context": self._post_action_context("verify_target_value", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "verify_target_value",
                exc,
                target=target,
                by=by,
                text_filter=expected_value,
                element=element,
            )

    def verify_target_visible(self, target: str, element: str = "", by: str = "css") -> Dict:
        try:
            page = self._resolve_page()
            locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
            visible = bool(locator.is_visible())
            if not visible:
                raise ValueError(f'Target not visible: "{target}"')
            details = _describe_locator(locator)
            return {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "target": str(target or "").strip(),
                "visible": True,
                "tag_name": details.get("tag_name", ""),
                "text": details.get("text", ""),
                "by": str(by or "css"),
                "post_action_context": self._post_action_context("verify_target_visible", page=page),
            }
        except Exception as exc:
            return self._action_error_payload(
                "verify_target_visible",
                exc,
                target=target,
                by=by,
                text_filter=target,
                element=element,
            )

    def describe_target(self, target: str, element: str = "", by: str = "css", include_box: bool = True) -> Dict:
        page = self._resolve_page()
        locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
        details = _describe_locator(locator)
        result = {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "target": str(target or "").strip(),
            "visible": bool(locator.is_visible()),
            "enabled": bool(locator.is_enabled()),
            **details,
        }
        if include_box:
            result["box"] = _safe_bounding_box(locator)
        return result

    def diagnose_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        text_filter: str = "",
        limit: int = 10,
        tab_id: str = "",
    ) -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        resolved_target = str(target or "").strip()
        diagnosis: Dict[str, Any] = {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "target": resolved_target,
            "element": str(element or "").strip(),
            "by": str(by or "css"),
            "text_filter": str(text_filter or ""),
            "is_snapshot_ref": _is_snapshot_ref(resolved_target),
            "snapshot_cache": {
                "cached_ref_count": len(self._last_snapshot_refs),
                "cached_refs_preview": sorted(self._last_snapshot_refs)[:50],
            },
        }
        if _is_snapshot_ref(resolved_target):
            diagnosis["snapshot_cache"]["ref_in_cache"] = resolved_target in self._last_snapshot_refs
            if self._last_snapshot_refs and resolved_target not in self._last_snapshot_refs:
                diagnosis["status"] = "stale_snapshot_ref"
                diagnosis["message"] = (
                    f'Ref "{resolved_target}" is not in the cached snapshot. '
                    "Capture a fresh browser_snapshot before retrying target actions."
                )
                diagnosis["interaction_context"] = self._post_action_context("diagnose_target")
                return diagnosis
        try:
            locator = self._resolve_target_locator(resolved_target, by=by, element=element, page=page)
            details = _describe_locator(locator)
            diagnosis.update(
                {
                    "status": "resolved",
                    "message": "target resolved successfully",
                    "visible": bool(locator.is_visible()),
                    "enabled": bool(locator.is_enabled()),
                    "resolved_target": resolved_target,
                    "details": self._compact_element_details(details, text_limit=400, html_limit=1000),
                    "box": _safe_bounding_box(locator),
                }
            )
            try:
                subtree_candidates = self.list_candidates(
                    target=resolved_target,
                    by=by,
                    text_filter=text_filter,
                    limit=max(1, int(limit)),
                    include_boxes=True,
                    tab_id=self._get_tab_id(page),
                )
                diagnosis["subtree_candidates"] = subtree_candidates.get("candidates", [])
            except Exception as exc:
                diagnosis["subtree_candidates_error"] = str(exc)
        except Exception as exc:
            diagnosis["status"] = "resolve_failed"
            diagnosis["message"] = str(exc)
            if not _is_snapshot_ref(resolved_target):
                try:
                    diagnosis["selector_matches"] = self.inspect_elements(
                        selector=resolved_target,
                        by=by,
                        limit=max(1, int(limit)),
                        tab_id=self._get_tab_id(page),
                    )
                except Exception as inspect_exc:
                    diagnosis["selector_matches_error"] = str(inspect_exc)
            try:
                diagnosis["page_candidates"] = self.list_candidates(
                    text_filter=text_filter or resolved_target,
                    limit=max(1, int(limit)),
                    include_boxes=True,
                    tab_id=self._get_tab_id(page),
                ).get("candidates", [])
            except Exception as candidate_exc:
                diagnosis["page_candidates_error"] = str(candidate_exc)
        diagnosis["interaction_context"] = self._post_action_context("diagnose_target", page=page)
        return diagnosis

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        try:
            page = self._resolve_page()
            locator = page.get_by_role(str(role), name=str(accessible_name))
            count = locator.count()
            if not count:
                raise ValueError(f'Element not found: role="{role}" accessible_name="{accessible_name}"')
            first = locator.first
            if not first.is_visible():
                raise ValueError(f'Element not visible: role="{role}" accessible_name="{accessible_name}"')
            resolved = {
                **self.get_current_url(tab_id=self._get_tab_id(page)),
                "verified": True,
                "matched": True,
                "role": str(role),
                "accessible_name": str(accessible_name),
                "expected_role": str(role),
                "expected_accessible_name": str(accessible_name),
                "count": count,
                "post_action_context": self._post_action_context("verify_element", page=page),
            }
            try:
                resolved["box"] = _safe_bounding_box(first)
            except Exception:
                pass
            return resolved
        except Exception as exc:
            return self._action_error_payload(
                "verify_element",
                exc,
                text_filter=str(accessible_name or role),
            )

    def highlight_target(self, target: str, element: str = "", by: str = "css", style: str = "") -> Dict:
        page = self._resolve_page()
        locator = self._resolve_target_locator(target=target, by=by, element=element, page=page)
        kwargs = {}
        if str(style or "").strip():
            kwargs["style"] = str(style).strip()
        locator.highlight(**kwargs)
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "highlighted": True,
            "target": str(target or "").strip(),
        }

    def clear_highlights(self) -> Dict:
        page = self._resolve_page()
        page.hide_highlight()
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "cleared": True}

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        page = self._resolve_page()
        page.mouse.move(float(x), float(y))
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "moved": True, "x": float(x), "y": float(y)}

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        page = self._resolve_page()
        page.mouse.click(
            float(x),
            float(y),
            button=str(button or "left"),
            click_count=max(1, int(click_count)),
            delay=max(0, int(delay_ms)),
        )
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "clicked": True,
            "x": float(x),
            "y": float(y),
            "button": str(button or "left"),
            "click_count": max(1, int(click_count)),
            "post_action_context": self._post_action_context("mouse_click_xy", page=page),
        }

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        page = self._resolve_page()
        page.mouse.move(float(start_x), float(start_y))
        page.mouse.down()
        page.mouse.move(float(end_x), float(end_y))
        page.mouse.up()
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "dragged": True,
            "start_x": float(start_x),
            "start_y": float(start_y),
            "end_x": float(end_x),
            "end_y": float(end_y),
            "post_action_context": self._post_action_context("mouse_drag_xy", page=page),
        }

    def mouse_gesture_path(
        self,
        points: list[dict[str, object]],
        *,
        steps_per_segment: int = 18,
        hold_before_ms: int = 0,
        segment_delay_ms: int = 0,
    ) -> Dict:
        normalized_points: list[tuple[float, float]] = []
        for item in list(points or []):
            if not isinstance(item, dict):
                raise ValueError("gesture points must be dictionaries with x/y coordinates")
            try:
                normalized_points.append((float(item["x"]), float(item["y"])))
            except Exception as exc:
                raise ValueError("gesture points must include numeric x/y coordinates") from exc
        if len(normalized_points) < 2:
            raise ValueError("gesture path requires at least two points")

        page = self._resolve_page()
        start_x, start_y = normalized_points[0]
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        if int(hold_before_ms) > 0:
            time.sleep(max(0, int(hold_before_ms)) / 1000.0)
        for x, y in normalized_points[1:]:
            page.mouse.move(float(x), float(y), steps=max(1, int(steps_per_segment)))
            if int(segment_delay_ms) > 0:
                time.sleep(max(0, int(segment_delay_ms)) / 1000.0)
        page.mouse.up()
        return {
            **self.get_current_url(tab_id=self._get_tab_id(page)),
            "gesture_performed": True,
            "point_count": len(normalized_points),
            "points": [{"x": x, "y": y} for x, y in normalized_points],
            "steps_per_segment": max(1, int(steps_per_segment)),
            "hold_before_ms": max(0, int(hold_before_ms)),
            "segment_delay_ms": max(0, int(segment_delay_ms)),
            "post_action_context": self._post_action_context("mouse_gesture_path", page=page),
        }

    def screenshot(self, filename: str = "", tab_id: str = "") -> Dict:
        page = self._resolve_page(tab_id=tab_id)
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "chromium-advanced-patchright-session.png")
        page.screenshot(path=output_path, full_page=True)
        return {**self.get_current_url(tab_id=self._get_tab_id(page)), "path": output_path}

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._playwright_ctx.stop()


class PatchrightThreadBoundSession:
    def __init__(self, session_factory):
        self.engine_name = "patchright"
        self._session_factory = session_factory
        self._thread = threading.Thread(
            target=self._thread_main,
            name="patchright-session-thread",
            daemon=True,
        )
        self._tasks = Queue()
        self._ready = threading.Event()
        self._closed = False
        self._raw_session = None
        self._startup_error = None
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _thread_main(self) -> None:
        try:
            self._raw_session = self._session_factory()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._tasks.get()
            if item is None:
                break
            method_name, args, kwargs, result_queue = item
            try:
                result = getattr(self._raw_session, method_name)(*args, **kwargs)
                result_queue.put((True, result))
            except Exception as exc:
                result_queue.put((False, exc))

    def _call(self, method_name: str, *args, **kwargs):
        if self._closed and method_name != "close":
            raise RuntimeError("patchright session is already closed")
        result_queue = Queue(maxsize=1)
        self._tasks.put((method_name, args, kwargs, result_queue))
        ok, value = result_queue.get()
        if ok:
            return value
        raise value

    def __getattr__(self, item: str):
        if item.startswith("_"):
            raise AttributeError(item)
        return lambda *args, **kwargs: self._call(item, *args, **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call("close")
        finally:
            self._closed = True
            self._tasks.put(None)
            self._thread.join(timeout=5)


class PatchrightEngine(BrowserEngine):
    engine_name = "patchright"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        from chromium_advanced.chromium_profile_lib import resolve_chromium_binary

        sync_playwright = _load_patchright()
        paths = config.get("paths", {})
        launch_settings = config.get("launch", {})
        headless = resolve_mcp_headless(config)
        start_minimized = resolve_mcp_start_minimized(config)
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        user_data_root = get_profile_user_data_root(config, profile_name)
        if not chromium_binary or not os.path.exists(chromium_binary):
            raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
        if not os.path.isdir(user_data_root):
            raise FileNotFoundError(f"Profile UserData root not found: {user_data_root}")
        _safe_log(
            f"[{now_text()}] [PATCHRIGHT] create_session begin: "
            f"profile={profile_name} chromium={chromium_binary} user_data_root={user_data_root}"
        )

        # Keep Patchright on the smallest validated argument set first.
        # Its Chromium startup behavior differs from Selenium/uc, so not every
        # shared launch flag should be forwarded blindly.
        args = [f"--profile-directory={profile_name}"]
        if start_minimized:
            args.append("--start-minimized")
        elif launch_settings.get("start_maximized", True):
            args.append("--start-maximized")
        window_size = str(launch_settings.get("window_size", "")).strip()
        if window_size:
            args.append(f"--window-size={window_size}")
        if launch_settings.get("no_first_run", True):
            args.append("--no-first-run")
        if launch_settings.get("no_default_browser_check", True):
            args.append("--no-default-browser-check")
        args.extend(get_chromium_restore_prompt_suppression_args())
        extra_args = launch_settings.get("extra_args", [])
        if isinstance(extra_args, list):
            args.extend([item for item in extra_args if item])

        def _create_raw_session() -> PatchrightBrowserSession:
            last_error = None
            playwright_ctx = None
            browser_context = None
            for attempt in range(1, 3):
                try:
                    playwright_ctx = sync_playwright().start()
                    _safe_log(
                        f"[{now_text()}] [PATCHRIGHT] playwright started: profile={profile_name} attempt={attempt}"
                    )
                    browser_context = playwright_ctx.chromium.launch_persistent_context(
                        user_data_dir=user_data_root,
                        executable_path=chromium_binary,
                        headless=bool(headless),
                        args=args,
                        no_viewport=True,
                    )
                    _safe_log(
                        f"[{now_text()}] [PATCHRIGHT] persistent context launched: profile={profile_name} attempt={attempt}"
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    _safe_log(
                        f"[{now_text()}] [PATCHRIGHT] launch attempt failed: profile={profile_name} attempt={attempt} error={exc}"
                    )
                    if playwright_ctx is not None:
                        try:
                            playwright_ctx.stop()
                        except Exception:
                            pass
                        playwright_ctx = None
                    if attempt >= 2:
                        raise
                    time.sleep(1.0)
            if browser_context is None or playwright_ctx is None:
                raise RuntimeError(f"Patchright failed to launch for profile {profile_name}: {last_error}")
            page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
            extensions_page = bool(launch_settings.get("open_extensions_page", False))
            check_url = str(launch_settings.get("check_url", "")).strip()
            if extensions_page:
                page.goto("chrome://extensions", wait_until="domcontentloaded", timeout=45000)
            if check_url:
                page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
            return PatchrightBrowserSession(playwright_ctx, browser_context, page)

        return PatchrightThreadBoundSession(_create_raw_session)
