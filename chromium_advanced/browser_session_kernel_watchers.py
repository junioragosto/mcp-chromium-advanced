from __future__ import annotations

import hashlib
import time
from typing import Any, Dict


def fallback_watch_page_state(
    *,
    raw_session,
    normalize_result,
    normalize_failure,
    text: str = "",
    previous_text: str = "",
    timeout_seconds: int = 20,
    stable_cycles: int = 2,
    poll_interval_ms: int = 500,
    tab_id: str = "",
) -> Dict:
    start = time.time()
    try:
        initial_page = raw_session.get_page_text(tab_id=tab_id) if tab_id else raw_session.get_page_text()
        initial_text = str(initial_page.get("text", "") or "")
        baseline_text = str(previous_text or initial_text or "")
        interval = max(50, int(poll_interval_ms)) / 1000.0
        required_cycles = max(1, int(stable_cycles))
        deadline = time.time() + max(1, int(timeout_seconds))
        expected_text = str(text or "")
        stable_count = 0
        last_signature = ""
        final_payload: Dict[str, Any] = {}
        final_text = baseline_text
        matched = False
        text_changed = False
        while time.time() < deadline:
            page = raw_session.get_page_text(tab_id=tab_id) if tab_id else raw_session.get_page_text()
            html = raw_session.get_page_html(tab_id=tab_id) if tab_id else raw_session.get_page_html()
            current_text = str(page.get("text", "") or "")
            html_value = str(html.get("html", "") or "")
            signature_source = (
                f"{str(page.get('url', '') or '')}\n"
                f"{str(page.get('title', '') or '')}\n"
                f"{current_text[:4000]}\n"
                f"{len(html_value)}"
            )
            signature = hashlib.sha1(signature_source.encode("utf-8", errors="ignore")).hexdigest()
            if signature == last_signature:
                stable_count += 1
            else:
                stable_count = 1
                last_signature = signature
            final_text = current_text
            contains_target = bool(expected_text and expected_text in current_text)
            changed_from_previous = current_text != baseline_text if baseline_text else bool(current_text.strip())
            matched = contains_target if expected_text else changed_from_previous
            text_changed = changed_from_previous
            final_payload = {
                **(page if isinstance(page, dict) else {}),
                "stable": stable_count >= required_cycles,
                "stable_cycles": stable_count,
                "required_stable_cycles": required_cycles,
                "poll_interval_ms": max(50, int(poll_interval_ms)),
                "text_length": len(current_text),
                "html_length": len(html_value),
                "page_signature": signature,
            }
            if stable_count >= required_cycles and (contains_target if expected_text else changed_from_previous):
                break
            time.sleep(interval)
        else:
            raise TimeoutError(
                f"Timed out waiting for combined page change/stability. expected_text={expected_text!r} previous_text_len={len(baseline_text)}"
            )
        result = {
            **final_payload,
            "watch_completed": True,
            "watch_reason": "text_changed_and_stable" if expected_text else "page_stable_after_change",
            "initial_text": initial_text,
            "previous_text": baseline_text,
            "final_text": final_text,
            "text_changed": text_changed,
            "target_text": expected_text,
            "text_contains_target": bool(expected_text and expected_text in final_text),
            "matched": matched,
            "verified": matched,
            "text_diff": {
                "changed": final_text != baseline_text,
                "previous_length": len(baseline_text),
                "final_length": len(final_text),
            },
        }
        return normalize_result("watch_page_state", result, used_fallback=True, duration_ms=int((time.time() - start) * 1000))
    except Exception as exc:
        return normalize_failure("watch_page_state", exc, used_fallback=True, duration_ms=int((time.time() - start) * 1000))
