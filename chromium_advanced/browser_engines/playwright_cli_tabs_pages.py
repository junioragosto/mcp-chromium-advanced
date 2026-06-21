from __future__ import annotations

from typing import Any, Dict


def list_tabs(session) -> Dict:
    tabs = session._refresh_tabs()
    active_tab = next((tab for tab in tabs if tab.get("active")), {})
    active_tab_id = str(active_tab.get("tab_id", "") or session._preferred_tab_id())
    return {
        **session.get_current_url(tab_id=active_tab_id),
        "active_tab_id": str(active_tab.get("tab_id", "")),
        "count": len(tabs),
        "tabs": tabs,
    }


def open_tab(
    session,
    *,
    url: str = "",
    activate: bool = True,
    wait_for_ready: bool = True,
    timeout_seconds: int = 20,
) -> Dict:
    del wait_for_ready
    original_tabs = session._refresh_tabs()
    original_active = next((tab for tab in original_tabs if tab.get("active")), {})
    original_index = int(original_active.get("index", 0)) if original_active else 0
    session._run_cli(["tab-new", "--json"])
    tabs_after_open = session._refresh_tabs()
    if not tabs_after_open:
        raise RuntimeError("No tabs found after opening a new tab.")
    new_tab = max(tabs_after_open, key=lambda item: int(item.get("index", -1)))
    new_index = int(new_tab.get("index", 0))
    session._select_index(new_index)
    session._sticky_tab_id = session._tab_id_for_index(new_index) if hasattr(session, "_tab_id_for_index") else f"tab-{new_index:03d}"
    if str(url or "").strip():
        target_url = str(url).strip()
        try:
            session._run_cli(["goto", target_url, "--json"], timeout_seconds=timeout_seconds)
        except TimeoutError:
            if not session._recover_navigation_timeout(target_url=target_url, tab_id=session._sticky_tab_id, action_name="open_tab"):
                raise
        tabs_after_open = session._refresh_tabs()
        new_tab = next((item for item in tabs_after_open if int(item.get("index", -1)) == new_index), new_tab)
    if not activate:
        session._select_index(original_index)
        session._sticky_tab_id = session._tab_id_for_index(original_index) if hasattr(session, "_tab_id_for_index") else f"tab-{original_index:03d}"
    tabs_final = session._refresh_tabs()
    current_tab_id = str(new_tab.get("tab_id", "") or (session._tab_id_for_index(new_index if activate else original_index) if hasattr(session, "_tab_id_for_index") else ""))
    return {
        **session._current_page_payload(tab_id=current_tab_id, commit_expected=True, action_name="open_tab"),
        "opened": True,
        "activated": bool(activate),
        "tab": new_tab,
        "tabs": tabs_final,
    }


def activate_tab(
    session,
    *,
    tab_id: str = "",
    index: int = -1,
    title_contains: str = "",
    url_contains: str = "",
) -> Dict:
    resolved_index = session._ensure_tab_selected(
        tab_id=tab_id,
        index=index,
        title_contains=title_contains,
        url_contains=url_contains,
    )
    tabs = session._refresh_tabs()
    active_tab = next((tab for tab in tabs if int(tab.get("index", -1)) == resolved_index), {})
    session._sticky_tab_id = str(active_tab.get("tab_id", "") or session._tab_id_for_index(resolved_index))
    return {
        **session._current_page_payload(tab_id=str(active_tab.get("tab_id", "")), commit_expected=True, action_name="activate_tab"),
        "activated": True,
        "tab": active_tab,
        "tabs": tabs,
    }


def close_tab(session, *, tab_id: str = "", index: int = -1) -> Dict:
    resolved_index = session._resolve_index(tab_id=tab_id, index=index)
    tabs_before = session._refresh_tabs()
    closed_tab = next((tab for tab in tabs_before if int(tab.get("index", -1)) == resolved_index), {})
    session._select_index(resolved_index)
    session._run_cli(["tab-close", str(resolved_index), "--json"])
    tabs_after = session._refresh_tabs()
    remaining_active = next((tab for tab in tabs_after if tab.get("active")), tabs_after[0] if tabs_after else {})
    session._sticky_tab_id = str(remaining_active.get("tab_id", "") or "")
    return {
        **session._current_page_payload(tab_id=session._sticky_tab_id, commit_expected=True, action_name="close_tab"),
        "closed": True,
        "closed_tab": closed_tab,
        "tabs": tabs_after,
    }


def resize(session, *, width: int, height: int) -> Dict:
    target_width = max(320, int(width))
    target_height = max(240, int(height))
    session._run_cli(["resize", str(target_width), str(target_height), "--json"])
    tabs = session._refresh_tabs()
    return {
        **session._current_page_payload(tab_id=session._preferred_tab_id(), commit_expected=True, action_name="resize"),
        "resized": True,
        "width": target_width,
        "height": target_height,
        "tabs": tabs,
    }


def navigate(
    session,
    *,
    url: str,
    wait_for_ready: bool = True,
    timeout_seconds: int = 20,
    tab_id: str = "",
) -> Dict:
    del wait_for_ready
    effective_tab_id = session._preferred_tab_id(tab_id=tab_id)
    if effective_tab_id:
        session._ensure_tab_selected(tab_id=effective_tab_id)
    target_url = str(url).strip()
    try:
        session._run_cli(["goto", target_url, "--json"], timeout_seconds=timeout_seconds)
    except TimeoutError:
        recovered = session._recover_navigation_timeout(target_url=target_url, tab_id=effective_tab_id, action_name="navigate")
        if recovered:
            return recovered
        raise
    return session._current_page_payload(tab_id=effective_tab_id, commit_expected=True, action_name="navigate")


def get_current_url(session, *, tab_id: str = "") -> Dict:
    return session._current_page_payload(tab_id=tab_id)


def get_page_text(session, *, tab_id: str = "") -> Dict:
    effective_tab_id = session._preferred_tab_id(tab_id=tab_id)
    try:
        result = session._page_text_via_dom_chunks(tab_id=effective_tab_id)
    except Exception:
        result = str(session._eval_json("() => document.body ? document.body.innerText : ''", tab_id=effective_tab_id) or "")
    return {**session.get_current_url(tab_id=effective_tab_id), "text": str(result or "")}


def get_page_html(session, *, tab_id: str = "") -> Dict:
    effective_tab_id = session._preferred_tab_id(tab_id=tab_id)
    result = session._eval_json("() => document.documentElement ? document.documentElement.outerHTML : ''", tab_id=effective_tab_id)
    return {**session.get_current_url(tab_id=effective_tab_id), "html": str(result or "")}
