from __future__ import annotations

import time
from typing import Dict, List, Optional


def session_record_from_occupancy_entry(
    *,
    profile_name: str,
    entry: Dict,
    session_record_cls,
    resource_lease_session_cls,
):
    if not isinstance(entry, dict):
        return None
    session_id = str(entry.get("session_id", "") or "").strip()
    state = str(entry.get("state", "") or "").strip().lower()
    scene_type = str(entry.get("scene_type", "") or "").strip().lower()
    if not session_id or state != "active" or scene_type not in {"mcp", "automation"}:
        return None
    details = entry.get("details", {})
    if not isinstance(details, dict):
        details = {}
    runtime_mode = str(details.get("runtime_mode", "") or "").strip() or "live_root"
    now_ts = time.time()
    return session_record_cls(
        session_id=session_id,
        profile_name=str(profile_name or "").strip(),
        engine_name=str(entry.get("engine_name", "") or "").strip(),
        created_at=0.0,
        last_used_at=float(entry.get("last_heartbeat_at", 0.0) or 0.0) or now_ts,
        browser_session=resource_lease_session_cls(profile_name),
        runtime_mode=runtime_mode,
        runtime_root=str(details.get("runtime_root", "") or "").strip(),
        mirror_generated_at=str(details.get("mirror_generated_at", "") or "").strip(),
        cleanup_runtime_on_close=False,
        scene_type=scene_type,
        owner_label=str(entry.get("owner_label", "") or "").strip(),
        task_scope=str(details.get("task_scope", "") or "").strip(),
        reuse_scope=str(details.get("reuse_scope", "") or "").strip() or "session",
        launch_pid=int(entry.get("owner_pid", 0) or 0),
        cached_alive=True,
    )


def build_active_session_view(
    *,
    live_sessions: List,
    occupancy_map: Optional[Dict[str, Dict]],
    session_record_cls,
    resource_lease_session_cls,
) -> List:
    view = list(live_sessions or [])
    existing_ids = {session.session_id for session in view if str(getattr(session, "session_id", "") or "").strip()}
    if not isinstance(occupancy_map, dict):
        return view
    for profile_name, entry in occupancy_map.items():
        derived = session_record_from_occupancy_entry(
            profile_name=profile_name,
            entry=entry,
            session_record_cls=session_record_cls,
            resource_lease_session_cls=resource_lease_session_cls,
        )
        if derived is None or derived.session_id in existing_ids:
            continue
        view.append(derived)
        existing_ids.add(derived.session_id)
    return view


def sessions_for_profile_view(
    *,
    profile_name: str,
    live_sessions: List,
    occupancy_map: Optional[Dict[str, Dict]],
    session_record_cls,
    resource_lease_session_cls,
) -> List:
    normalized = str(profile_name or "").strip()
    return [
        session
        for session in build_active_session_view(
            live_sessions=live_sessions,
            occupancy_map=occupancy_map,
            session_record_cls=session_record_cls,
            resource_lease_session_cls=resource_lease_session_cls,
        )
        if str(getattr(session, "profile_name", "") or "").strip() == normalized
    ]
