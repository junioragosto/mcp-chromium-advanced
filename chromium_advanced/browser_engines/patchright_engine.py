from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict

from chromium_advanced.browser_engines.base import BrowserEngine, BrowserSession, BrowserSessionSummary
from chromium_advanced.chromium_profile_lib import now_text


SNAPSHOT_REF_PATTERN = re.compile(r"^(?:f\d+)?e\d+$")
SNAPSHOT_REF_EXTRACT_PATTERN = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")


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
          id: el.id || '',
          name: el.getAttribute('name') || '',
          class: el.getAttribute('class') || '',
          aria_label: el.getAttribute('aria-label') || '',
          role: el.getAttribute('role') || '',
          value: 'value' in el ? (el.value || '') : '',
          href: el.getAttribute('href') || '',
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
    text_len = len(str(item.get("text", "") or ""))
    return (
        0 if visible else 1,
        0 if enabled else 1,
        0 if interactive else 1,
        0 if has_aria else 1,
        0 if has_role else 1,
        0 if has_href else 1,
        text_len,
    )


class PatchrightBrowserSession(BrowserSession):
    def __init__(self, playwright_ctx, browser_context, page):
        self._playwright_ctx = playwright_ctx
        self.context = browser_context
        self.page = page
        self._last_snapshot_text = ""
        self._last_snapshot_refs: set[str] = set()

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
            "supports_post_action_context": True,
        }

    def _update_snapshot_cache(self, snapshot_text: str) -> list[str]:
        refs = _extract_snapshot_refs(snapshot_text)
        self._last_snapshot_text = snapshot_text or ""
        self._last_snapshot_refs = set(refs)
        return refs

    def _resolve_target_locator(self, target: str, by: str = "css", element: str = ""):
        target = str(target or "").strip()
        if not target:
            raise ValueError("target is required")
        if _is_snapshot_ref(target):
            if self._last_snapshot_refs and target not in self._last_snapshot_refs:
                raise ValueError(f"Ref {target} not found in the cached snapshot. Capture a fresh browser_snapshot first.")
            locator = self.page.locator(f"aria-ref={target}")
        else:
            locator = _raw_target_to_locator(self.page, target) or _selector_to_locator(self.page, target, by)
        locator = locator.first
        if str(element or "").strip():
            locator = locator.describe(str(element).strip())
        return locator

    def _safe_page_snapshot(self, depth: int = 4, max_chars: int = 5000, update_cache: bool = False) -> Dict:
        try:
            snapshot_text = self.page.aria_snapshot(mode="ai", depth=depth, boxes=False)
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
            for index, page in enumerate(self.context.pages):
                tabs.append(
                    {
                        "index": index,
                        "url": page.url or "",
                        "title": page.title() or "",
                        "active": page == self.page,
                    }
                )
        except Exception as exc:
            tabs.append({"error": str(exc)})
        return tabs

    def _safe_modal_state(self) -> Dict:
        try:
            payload = self.page.evaluate(
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

    def _post_action_context(self, action_name: str) -> Dict:
        context = {
            "action_name": action_name,
            "page": self.get_current_url(),
            "tabs": self._safe_tabs_summary(),
            "active_element": {},
            "modal_state": self._safe_modal_state(),
            "snapshot": self._safe_page_snapshot(depth=4, max_chars=5000, update_cache=False),
        }
        try:
            context["active_element"] = self._compact_element_details(self.get_active_element().get("element", {}))
        except Exception as exc:
            context["active_element"] = {"error": str(exc)}
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
    ) -> Dict:
        payload: Dict[str, Any] = {
            **self.get_current_url(),
            "ok": False,
            "action_name": action_name,
            "error": str(error),
            "error_type": type(error).__name__,
            "post_action_context": self._post_action_context(f"{action_name}_failed"),
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
                )
            except Exception as diag_exc:
                payload["diagnosis_error"] = str(diag_exc)
        elif text_filter:
            try:
                payload["page_candidates"] = self.list_candidates(
                    text_filter=text_filter,
                    limit=limit,
                    include_boxes=True,
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
                const value = 'value' in el ? normalizeText(el.value || '') : '';
                const href = normalizeText(el.getAttribute('href') || '');
                const tagName = normalizeText((el.tagName || '').toLowerCase());
                const candidateText = normalizeText([text, ariaLabel, title, role, placeholder, name, value].join(' '));
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
                  id: normalizeText(el.id || ''),
                  name,
                  class: normalizeText(el.getAttribute('class') || ''),
                  aria_label: ariaLabel,
                  title,
                  role,
                  placeholder,
                  value,
                  href,
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

    def navigate(self, url: str, wait_for_ready: bool = True, timeout_seconds: int = 20) -> Dict:
        wait_until = "load" if wait_for_ready else "domcontentloaded"
        self.page.goto(url, wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
        return {**self.get_current_url(), "post_action_context": self._post_action_context("navigate")}

    def get_current_url(self) -> Dict:
        return {"url": self.page.url, "title": self.page.title()}

    def get_page_text(self) -> Dict:
        text = self.page.locator("body").inner_text(timeout=15000).strip()
        return {**self.get_current_url(), "text": text}

    def get_page_html(self) -> Dict:
        return {**self.get_current_url(), "html": self.page.content()}

    def inspect_elements(self, selector: str, by: str = "css", limit: int = 10) -> Dict:
        locator = _selector_to_locator(self.page, selector, by)
        count = locator.count()
        elements = []
        for index in range(min(max(1, int(limit)), count)):
            try:
                elements.append(_describe_locator(locator.nth(index)))
            except Exception:
                continue
        return {**self.get_current_url(), "count": count, "elements": elements}

    def get_active_element(self) -> Dict:
        element = self.page.evaluate(
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
        return {**self.get_current_url(), "element": element}

    def get_interaction_context(self) -> Dict:
        return {**self.get_current_url(), "interaction_context": self._post_action_context("inspect")}

    def snapshot(
        self,
        target: str = "",
        by: str = "css",
        depth: int | None = None,
        boxes: bool = False,
        filename: str = "",
    ) -> Dict:
        locator = None
        resolved_target = str(target or "").strip()
        if resolved_target:
            locator = self._resolve_target_locator(resolved_target, by=by)
            snapshot_text = locator.aria_snapshot(mode="ai", depth=depth, boxes=bool(boxes))
        else:
            snapshot_text = self.page.aria_snapshot(mode="ai", depth=depth, boxes=bool(boxes))
        refs = self._update_snapshot_cache(snapshot_text)
        result = {
            **self.get_current_url(),
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
    ) -> Dict:
        resolved_target = str(target or "").strip()
        locator = None
        if resolved_target:
            locator = self._resolve_target_locator(resolved_target, by=by)
            snapshot_text = locator.aria_snapshot(mode="ai", boxes=False)
        else:
            snapshot_text = self.page.aria_snapshot(mode="ai", boxes=False)
        refs = _extract_snapshot_refs(snapshot_text)
        lowered_filter = str(text_filter or "").strip().lower()
        candidates = []
        seen_targets = set()
        for ref in refs:
            try:
                ref_locator = self.page.locator(f"aria-ref={ref}").first
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
            root_locator = locator or self.page.locator("body")
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
        candidates = sorted(candidates, key=_candidate_sort_key)[: max(1, int(limit))]
        return {
            **self.get_current_url(),
            "target": resolved_target,
            "text_filter": str(text_filter or ""),
            "count": len(candidates),
            "candidates": candidates,
        }

    def wait_for(self, selector: str, by: str = "css", timeout_seconds: int = 20, condition: str = "visible") -> Dict:
        try:
            locator = _selector_to_locator(self.page, selector, by).first
            target_state = {"present": "attached", "visible": "visible", "clickable": "visible"}.get(condition, "visible")
            locator.wait_for(state=target_state, timeout=int(timeout_seconds) * 1000)
            item = _describe_locator(locator)
            return {**self.get_current_url(), "found": True, "tag_name": item.get("tag_name", ""), "text": item.get("text", "")}
        except Exception as exc:
            return self._action_error_payload("wait_for", exc, selector=selector, by=by, text_filter=selector)

    def click(self, selector: str, by: str = "css", timeout_seconds: int = 20) -> Dict:
        try:
            locator = _selector_to_locator(self.page, selector, by).first
            locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            locator.click(timeout=int(timeout_seconds) * 1000)
            return {**self.get_current_url(), "clicked": True, "post_action_context": self._post_action_context("click")}
        except Exception as exc:
            return self._action_error_payload("click", exc, selector=selector, by=by, text_filter=selector)

    def click_target(
        self,
        target: str,
        element: str = "",
        by: str = "css",
        timeout_seconds: int = 20,
        double_click: bool = False,
    ) -> Dict:
        try:
            locator = self._resolve_target_locator(target, by=by, element=element)
            locator.scroll_into_view_if_needed(timeout=int(timeout_seconds) * 1000)
            if double_click:
                locator.dblclick(timeout=int(timeout_seconds) * 1000)
            else:
                locator.click(timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(),
                "clicked": True,
                "double_click": bool(double_click),
                "target": str(target or "").strip(),
                "post_action_context": self._post_action_context("click_target"),
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
            locator = _selector_to_locator(self.page, selector, by).first
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            if clear_first:
                locator.fill("", timeout=int(timeout_seconds) * 1000)
            locator.type(text, timeout=int(timeout_seconds) * 1000)
            if submit:
                locator.press("Enter", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(),
                "typed": True,
                "submitted": bool(submit),
                "post_action_context": self._post_action_context("type_text"),
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
            locator = self._resolve_target_locator(target, by=by, element=element)
            locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
            if clear_first:
                locator.fill("", timeout=int(timeout_seconds) * 1000)
            locator.type(text, timeout=int(timeout_seconds) * 1000)
            if submit:
                locator.press("Enter", timeout=int(timeout_seconds) * 1000)
            return {
                **self.get_current_url(),
                "typed": True,
                "submitted": bool(submit),
                "target": str(target or "").strip(),
                "post_action_context": self._post_action_context("type_target"),
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
            repeat = max(1, int(count))
            if str(selector or "").strip():
                locator = _selector_to_locator(self.page, selector, by).first
                locator.wait_for(state="visible", timeout=int(timeout_seconds) * 1000)
                locator.focus(timeout=int(timeout_seconds) * 1000)
            for _ in range(repeat):
                self.page.keyboard.press(str(key))
            return {
                **self.get_current_url(),
                "pressed": True,
                "key": key,
                "count": repeat,
                "post_action_context": self._post_action_context("press_key"),
            }
        except Exception as exc:
            return self._action_error_payload("press_key", exc, selector=selector, by=by, text_filter=selector or key)

    def run_script(self, script: str) -> Dict:
        result = self.page.evaluate(f"() => {{ {script} }}")
        try:
            serialized = json.loads(json.dumps(result))
        except TypeError:
            serialized = str(result)
        return {**self.get_current_url(), "result": serialized}

    def verify_text(self, text: str) -> Dict:
        try:
            locator = self.page.get_by_text(str(text))
            count = locator.count()
            visible = False
            if count:
                try:
                    visible = bool(locator.first.is_visible())
                except Exception:
                    visible = False
            if not count or not visible:
                raise ValueError(f'Text not visible: "{text}"')
            return {**self.get_current_url(), "verified": True, "text": str(text), "count": count}
        except Exception as exc:
            return self._action_error_payload("verify_text", exc, text_filter=str(text))

    def verify_dialog(self, accessible_name: str = "", text: str = "") -> Dict:
        try:
            modal_state = self._safe_modal_state()
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
                **self.get_current_url(),
                "verified": True,
                "count": len(matched),
                "dialog": matched[0],
                "dialogs": matched,
            }
        except Exception as exc:
            return self._action_error_payload(
                "verify_dialog",
                exc,
                text_filter=str(accessible_name or text or "dialog"),
            )

    def verify_active_element(self, target: str = "", by: str = "css", element: str = "") -> Dict:
        try:
            active = self.get_active_element().get("element", {})
            if str(target or "").strip():
                locator = self._resolve_target_locator(target=target, by=by, element=element)
                is_active = bool(locator.evaluate("el => el === document.activeElement"))
                if not is_active:
                    raise ValueError(f'Active element did not match target: "{target}"')
                expected = _describe_locator(locator)
                return {**self.get_current_url(), "verified": True, "target": str(target), "element": expected}
            if not active:
                raise ValueError("No active element found.")
            return {**self.get_current_url(), "verified": True, "element": self._compact_element_details(active)}
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
            locator = self._resolve_target_locator(target=target, by=by, element=element)
            details = _describe_locator(locator)
            actual_value = str(details.get("value", "") or "")
            if actual_value != str(expected_value):
                raise ValueError(f'Value mismatch for target "{target}": expected "{expected_value}", got "{actual_value}"')
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "expected_value": str(expected_value),
                "actual_value": actual_value,
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
            locator = self._resolve_target_locator(target=target, by=by, element=element)
            visible = bool(locator.is_visible())
            if not visible:
                raise ValueError(f'Target not visible: "{target}"')
            details = _describe_locator(locator)
            return {
                **self.get_current_url(),
                "verified": True,
                "target": str(target or "").strip(),
                "visible": True,
                "tag_name": details.get("tag_name", ""),
                "text": details.get("text", ""),
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
        locator = self._resolve_target_locator(target=target, by=by, element=element)
        details = _describe_locator(locator)
        result = {
            **self.get_current_url(),
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
    ) -> Dict:
        resolved_target = str(target or "").strip()
        diagnosis: Dict[str, Any] = {
            **self.get_current_url(),
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
            locator = self._resolve_target_locator(resolved_target, by=by, element=element)
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
                    )
                except Exception as inspect_exc:
                    diagnosis["selector_matches_error"] = str(inspect_exc)
            try:
                diagnosis["page_candidates"] = self.list_candidates(
                    text_filter=text_filter or resolved_target,
                    limit=max(1, int(limit)),
                    include_boxes=True,
                ).get("candidates", [])
            except Exception as candidate_exc:
                diagnosis["page_candidates_error"] = str(candidate_exc)
        diagnosis["interaction_context"] = self._post_action_context("diagnose_target")
        return diagnosis

    def verify_element(self, role: str, accessible_name: str) -> Dict:
        try:
            locator = self.page.get_by_role(str(role), name=str(accessible_name))
            count = locator.count()
            if not count:
                raise ValueError(f'Element not found: role="{role}" accessible_name="{accessible_name}"')
            first = locator.first
            if not first.is_visible():
                raise ValueError(f'Element not visible: role="{role}" accessible_name="{accessible_name}"')
            resolved = {
                **self.get_current_url(),
                "verified": True,
                "role": str(role),
                "accessible_name": str(accessible_name),
                "count": count,
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
        locator = self._resolve_target_locator(target=target, by=by, element=element)
        kwargs = {}
        if str(style or "").strip():
            kwargs["style"] = str(style).strip()
        locator.highlight(**kwargs)
        return {**self.get_current_url(), "highlighted": True, "target": str(target or "").strip()}

    def clear_highlights(self) -> Dict:
        self.page.hide_highlight()
        return {**self.get_current_url(), "cleared": True}

    def mouse_move_xy(self, x: float, y: float) -> Dict:
        self.page.mouse.move(float(x), float(y))
        return {**self.get_current_url(), "moved": True, "x": float(x), "y": float(y)}

    def mouse_click_xy(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
    ) -> Dict:
        self.page.mouse.click(
            float(x),
            float(y),
            button=str(button or "left"),
            click_count=max(1, int(click_count)),
            delay=max(0, int(delay_ms)),
        )
        return {
            **self.get_current_url(),
            "clicked": True,
            "x": float(x),
            "y": float(y),
            "button": str(button or "left"),
            "click_count": max(1, int(click_count)),
            "post_action_context": self._post_action_context("mouse_click_xy"),
        }

    def mouse_drag_xy(self, start_x: float, start_y: float, end_x: float, end_y: float) -> Dict:
        self.page.mouse.move(float(start_x), float(start_y))
        self.page.mouse.down()
        self.page.mouse.move(float(end_x), float(end_y))
        self.page.mouse.up()
        return {
            **self.get_current_url(),
            "dragged": True,
            "start_x": float(start_x),
            "start_y": float(start_y),
            "end_x": float(end_x),
            "end_y": float(end_y),
            "post_action_context": self._post_action_context("mouse_drag_xy"),
        }

    def screenshot(self, filename: str = "") -> Dict:
        output_path = str(filename or "").strip()
        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), "chromium-advanced-patchright-session.png")
        self.page.screenshot(path=output_path, full_page=True)
        return {**self.get_current_url(), "path": output_path}

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._playwright_ctx.stop()


class PatchrightEngine(BrowserEngine):
    engine_name = "patchright"

    def create_session(self, config: Dict, profile_name: str) -> BrowserSession:
        from chromium_advanced.chromium_profile_lib import resolve_chromium_binary

        sync_playwright = _load_patchright()
        paths = config.get("paths", {})
        launch_settings = config.get("launch", {})
        chromium_binary = resolve_chromium_binary(paths.get("chromium_dir", ""))
        user_data_root = os.path.abspath(os.path.expanduser(paths.get("user_data_root", "")))
        if not chromium_binary or not os.path.exists(chromium_binary):
            raise FileNotFoundError(f"chromium browser not found: {chromium_binary or paths.get('chromium_dir', '')}")
        if not os.path.isdir(user_data_root):
            raise FileNotFoundError(f"UserData root not found: {user_data_root}")
        print(
            (
                f"[{now_text()}] [PATCHRIGHT] create_session begin: "
                f"profile={profile_name} chromium={chromium_binary} user_data_root={user_data_root}"
            ),
            flush=True,
        )

        # Keep Patchright on the smallest validated argument set first.
        # Its Chromium startup behavior differs from Selenium/uc, so not every
        # shared launch flag should be forwarded blindly.
        args = [f"--profile-directory={profile_name}"]
        if launch_settings.get("start_maximized", True):
            args.append("--start-maximized")
        window_size = str(launch_settings.get("window_size", "")).strip()
        if window_size:
            args.append(f"--window-size={window_size}")
        if launch_settings.get("no_first_run", True):
            args.append("--no-first-run")
        if launch_settings.get("no_default_browser_check", True):
            args.append("--no-default-browser-check")
        extra_args = launch_settings.get("extra_args", [])
        if isinstance(extra_args, list):
            args.extend([item for item in extra_args if item])

        playwright_ctx = sync_playwright().start()
        print(f"[{now_text()}] [PATCHRIGHT] playwright started: profile={profile_name}", flush=True)
        browser_context = playwright_ctx.chromium.launch_persistent_context(
            user_data_dir=user_data_root,
            executable_path=chromium_binary,
            headless=False,
            args=args,
            no_viewport=True,
        )
        print(f"[{now_text()}] [PATCHRIGHT] persistent context launched: profile={profile_name}", flush=True)
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        extensions_page = bool(launch_settings.get("open_extensions_page", False))
        check_url = str(launch_settings.get("check_url", "")).strip()
        if extensions_page:
            page.goto("chrome://extensions", wait_until="domcontentloaded", timeout=45000)
        if check_url:
            page.goto(check_url, wait_until="domcontentloaded", timeout=45000)
        return PatchrightBrowserSession(playwright_ctx, browser_context, page)
