from __future__ import annotations

from typing import Dict


def list_tabs(session) -> Dict:
    tabs = session._safe_tabs_summary()
    active_tab_id = ""
    for item in tabs:
        if item.get("active"):
            active_tab_id = str(item.get("tab_id", "") or "")
            break
    return {**session.get_current_url(), "active_tab_id": active_tab_id, "count": len(tabs), "tabs": tabs}


def open_tab(
    session,
    *,
    url: str = "",
    activate: bool = True,
    wait_for_ready: bool = True,
    timeout_seconds: int = 20,
) -> Dict:
    page = session.context.new_page()
    session._attach_page(page)
    if str(url or "").strip():
        wait_until = "domcontentloaded" if wait_for_ready else "commit"
        page.goto(str(url).strip(), wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
    if activate:
        session.page = page
        try:
            page.bring_to_front()
        except Exception:
            pass
    return {
        **session.get_current_url(tab_id=session._get_tab_id(page)),
        "opened": True,
        "activated": bool(activate),
        "tab": session._tab_entry(page, session._live_pages().index(page)),
        "tabs": session._safe_tabs_summary(),
    }


def activate_tab(
    session,
    *,
    tab_id: str = "",
    index: int = -1,
    title_contains: str = "",
    url_contains: str = "",
) -> Dict:
    page = session._resolve_page(tab_id=tab_id, index=index, title_contains=title_contains, url_contains=url_contains)
    session.page = page
    try:
        page.bring_to_front()
    except Exception:
        pass
    return {
        **session.get_current_url(tab_id=session._get_tab_id(page)),
        "activated": True,
        "tab": session._tab_entry(page, session._live_pages().index(page)),
        "tabs": session._safe_tabs_summary(),
    }


def close_tab(session, *, tab_id: str = "", index: int = -1) -> Dict:
    page = session._resolve_page(tab_id=tab_id, index=index)
    closing_tab = session._tab_entry(page, session._live_pages().index(page))
    page.close()
    remaining_pages = session._live_pages()
    if remaining_pages:
        if session.page == page:
            session.page = remaining_pages[0]
    else:
        session.page = session.context.new_page()
        session._attach_page(session.page)
    return {
        **session.get_current_url(),
        "closed": True,
        "closed_tab": closing_tab,
        "tabs": session._safe_tabs_summary(),
    }


def resize(session, *, width: int, height: int) -> Dict:
    page = session._resolve_page()
    target_width = max(320, int(width))
    target_height = max(240, int(height))
    try:
        session.page.set_viewport_size({"width": target_width, "height": target_height})
    except Exception:
        page.set_viewport_size({"width": target_width, "height": target_height})
    return {
        **session.get_current_url(tab_id=session._get_tab_id(page)),
        "resized": True,
        "width": target_width,
        "height": target_height,
        "tabs": session._safe_tabs_summary(),
    }


def navigate(
    session,
    *,
    url: str,
    wait_for_ready: bool = True,
    timeout_seconds: int = 20,
    tab_id: str = "",
) -> Dict:
    page = session._resolve_page(tab_id=tab_id)
    wait_until = "domcontentloaded" if wait_for_ready else "commit"
    page.goto(url, wait_until=wait_until, timeout=int(timeout_seconds) * 1000)
    return {**session.get_current_url(tab_id=session._get_tab_id(page))}


def get_current_url(session, *, tab_id: str = "") -> Dict:
    page = session._resolve_page(tab_id=tab_id)
    return {"tab_id": session._get_tab_id(page), "url": page.url, "title": page.title()}


def get_page_text(session, *, tab_id: str = "") -> Dict:
    page = session._resolve_page(tab_id=tab_id)
    text = page.locator("body").inner_text(timeout=15000).strip()
    return {**session.get_current_url(tab_id=session._get_tab_id(page)), "text": text}


def get_page_html(session, *, tab_id: str = "") -> Dict:
    page = session._resolve_page(tab_id=tab_id)
    return {**session.get_current_url(tab_id=session._get_tab_id(page)), "html": page.content()}
