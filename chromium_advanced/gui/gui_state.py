import datetime
import os
import tempfile
import time
from typing import Callable, Dict, List, Optional


TranslateFunc = Callable[[str, str], str]


def build_external_profile_process_map(config: Dict, raw_map: Dict[str, List[Dict]]) -> Dict[str, List[int]]:
    entries: Dict[str, List[int]] = {}
    for profile in config.get("profiles", []):
        profile_name = str(profile.get("profile_name", "") or "").strip()
        if not profile_name:
            continue
        entries[profile_name] = [
            int(item.get("pid") or 0)
            for item in raw_map.get(profile_name, [])
            if int(item.get("pid") or 0) > 0
        ]
    return entries


def serialize_external_profile_process_map(process_map: Dict[str, List[int]]) -> str:
    return "|".join(
        f"{profile_name}:{','.join(str(pid) for pid in process_map.get(profile_name, []))}"
        for profile_name in sorted(process_map.keys())
    )


def format_scene_type_label(scene_type: str, tr: TranslateFunc) -> str:
    scene_type = str(scene_type or "").strip().lower()
    mapping = {
        "mcp": tr("occupancy_scene_mcp", "MCP"),
        "manual": tr("occupancy_scene_manual", "MANUAL"),
        "keepalive": tr("occupancy_scene_keepalive", "KEEPALIVE"),
        "automation": tr("occupancy_scene_automation", "SCRIPT"),
        "in_use": tr("occupancy_scene_in_use", "IN USE"),
        "unknown": tr("occupancy_scene_unknown", "IN USE"),
    }
    return mapping.get(scene_type, scene_type.upper() if scene_type else tr("occupancy_scene_unknown", "IN USE"))


def build_profile_runtime_state_text(
    profile_name: str,
    occupancy_cache: Dict[str, Dict],
    external_profile_process_map: Dict[str, List[int]],
    is_profile_keepalive_running: Callable[[str], bool],
    tr: TranslateFunc,
    control_profile: Optional[Dict] = None,
) -> str:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return tr("runtime_state_unknown", "Unknown")
    control_profile = control_profile if isinstance(control_profile, dict) else {}
    control_busy_state = str(control_profile.get("busy_state", "") or "").strip().lower()
    control_scene_type = str(control_profile.get("occupancy_scene_type", "") or "").strip().lower()
    control_state = str(control_profile.get("occupancy_state", "") or "").strip().lower()
    control_owner_label = str(control_profile.get("occupancy_owner_label", "") or "").strip()
    control_owner_source = str(control_profile.get("busy_owner_source", "") or "").strip().lower()
    control_busy_owner_label = str(control_profile.get("busy_owner_label", "") or "").strip()
    control_occupancy = control_profile.get("occupancy", {}) if isinstance(control_profile.get("occupancy", {}), dict) else {}
    occupancy = control_occupancy if control_occupancy else {}
    fallback_occupancy = occupancy_cache.get(profile_name, {}) if isinstance(occupancy_cache.get(profile_name, {}), dict) else {}
    if occupancy:
        scene_type = str(occupancy.get("scene_type", "") or "").strip()
        owner_label = str(occupancy.get("owner_label", "") or "").strip()
        state = str(occupancy.get("state", "") or "").strip() or "active"
        if scene_type and state not in {"released", "start_failed"}:
            suffix = f" | {owner_label}" if owner_label else ""
            return f"{scene_type}:{state}{suffix}"
    if control_busy_state and control_busy_state not in {"idle", "released"}:
        scene_type = control_scene_type or control_owner_source or control_busy_state or "in_use"
        state = control_state or "active"
        owner_text = control_owner_label or control_busy_owner_label
        suffix = f" | {owner_text}" if owner_text else ""
        return f"{scene_type}:{state}{suffix}"
    if fallback_occupancy:
        scene_type = str(fallback_occupancy.get("scene_type", "") or "").strip()
        owner_label = str(fallback_occupancy.get("owner_label", "") or "").strip()
        state = str(fallback_occupancy.get("state", "") or "").strip() or "active"
        if scene_type and state not in {"released", "start_failed"}:
            suffix = f" | {owner_label}" if owner_label else ""
            return f"{scene_type}:{state}{suffix}"
    if is_profile_keepalive_running(profile_name):
        return tr("runtime_state_keepalive", "Keepalive running")
    control_external_process_count = int(control_profile.get("external_process_count", 0) or 0)
    if control_external_process_count > 0:
        return tr("runtime_state_external_running", "External Chromium running: {pid_text}").format(
            pid_text=str(control_external_process_count)
        )
    pids = external_profile_process_map.get(profile_name, [])
    if pids:
        return tr("runtime_state_external_running", "External Chromium running: {pid_text}").format(
            pid_text=", ".join(str(pid) for pid in pids)
        )
    return tr("runtime_state_idle", "Idle")


def format_occupancy_entry_summary(profile_name: str, occupancy: Dict, tr: TranslateFunc, is_expired: bool) -> str:
    if not isinstance(occupancy, dict) or not occupancy:
        return f"{profile_name}: {tr('runtime_state_idle', 'Idle')}"
    scene_label = format_scene_type_label(occupancy.get("scene_type", ""), tr)
    state = str(occupancy.get("state", "") or "active")
    owner_label = str(occupancy.get("owner_label", "") or "").strip()
    owner_pid = int(occupancy.get("owner_pid", 0) or 0)
    lease_expires_at = float(occupancy.get("lease_expires_at", 0.0) or 0.0)
    lease_text = "-"
    if lease_expires_at > 0:
        lease_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease_expires_at))
    return (
        f"{profile_name}: {scene_label}/{state}"
        f" | owner={owner_label or '-'}"
        f" | pid={owner_pid or '-'}"
        f" | lease_until={lease_text}"
        f" | expired={'yes' if is_expired else 'no'}"
    )


def format_occupancy_event_text(payload: Dict) -> str:
    profile_name = str(payload.get("profile_name", "") or "-")
    scene_type = str(payload.get("scene_type", "") or "unknown")
    state = str(payload.get("state", "") or "active")
    owner_label = str(payload.get("owner_label", "") or "").strip()
    engine_name = str(payload.get("engine_name", "") or "").strip()
    parts = [f"{profile_name} {scene_type}:{state}"]
    if owner_label:
        parts.append(owner_label)
    if engine_name:
        parts.append(f"engine={engine_name}")
    return " | ".join(parts)


def build_profile_status_display(
    profile_name: str,
    occupancy_cache: Dict[str, Dict],
    external_profile_process_map: Dict[str, List[int]],
    is_profile_keepalive_running: Callable[[str], bool],
    tr: TranslateFunc,
    control_profile: Optional[Dict] = None,
) -> Dict[str, str]:
    control_profile = control_profile if isinstance(control_profile, dict) else {}
    control_busy_state = str(control_profile.get("busy_state", "") or "").strip().lower()
    control_scene_type = str(control_profile.get("occupancy_scene_type", "") or "").strip().lower()
    control_state = str(control_profile.get("occupancy_state", "") or "").strip().lower()
    control_owner_label = str(control_profile.get("occupancy_owner_label", "") or "").strip()
    control_owner_source = str(control_profile.get("busy_owner_source", "") or "").strip().lower()
    control_busy_owner_label = str(control_profile.get("busy_owner_label", "") or "").strip()
    control_occupancy = control_profile.get("occupancy", {}) if isinstance(control_profile.get("occupancy", {}), dict) else {}
    occupancy = control_occupancy if control_occupancy else {}
    fallback_occupancy = occupancy_cache.get(profile_name, {}) if isinstance(occupancy_cache.get(profile_name, {}), dict) else {}
    if occupancy:
        scene_type = str(occupancy.get("scene_type", "") or "").strip() or "in_use"
        state = str(occupancy.get("state", "") or "").strip() or "active"
        owner_label = str(occupancy.get("owner_label", "") or "").strip()
        label = format_scene_type_label(scene_type, tr)
        if state not in {"active", "running"}:
            label = f"{label}/{state}"
        tooltip = owner_label or f"{scene_type} ({state})"
        if occupancy.get("engine_name"):
            tooltip = f"{tooltip}\nengine={occupancy.get('engine_name')}"
        if occupancy.get("session_id"):
            tooltip = f"{tooltip}\nsession={occupancy.get('session_id')}"
        return {"label": label, "tooltip": tooltip}
    if control_busy_state and control_busy_state not in {"idle", "released"}:
        scene_type = control_scene_type or control_owner_source or control_busy_state or "in_use"
        state = control_state or "active"
        label = format_scene_type_label(scene_type, tr)
        if state not in {"active", "running"}:
            label = f"{label}/{state}"
        tooltip = control_owner_label or control_busy_owner_label or f"{scene_type} ({state})"
        return {"label": label, "tooltip": tooltip}
    if fallback_occupancy:
        scene_type = str(fallback_occupancy.get("scene_type", "") or "").strip() or "in_use"
        state = str(fallback_occupancy.get("state", "") or "").strip() or "active"
        owner_label = str(fallback_occupancy.get("owner_label", "") or "").strip()
        label = format_scene_type_label(scene_type, tr)
        if state not in {"active", "running"}:
            label = f"{label}/{state}"
        tooltip = owner_label or f"{scene_type} ({state})"
        if fallback_occupancy.get("engine_name"):
            tooltip = f"{tooltip}\nengine={fallback_occupancy.get('engine_name')}"
        if fallback_occupancy.get("session_id"):
            tooltip = f"{tooltip}\nsession={fallback_occupancy.get('session_id')}"
        return {"label": label, "tooltip": tooltip}
    if is_profile_keepalive_running(profile_name):
        return {"label": "KEEPALIVE", "tooltip": "keepalive running"}
    pids = external_profile_process_map.get(profile_name, [])
    if pids:
        return {"label": "MANUAL", "tooltip": f"external chromium pid={', '.join(str(pid) for pid in pids)}"}
    idle_text = tr("runtime_state_idle", "Idle")
    return {"label": idle_text, "tooltip": idle_text}


def build_selected_profile_status_text(
    profile: Optional[Dict],
    selected_profile_name: str,
    profiles: List[Dict],
    build_profile_detail_text: Callable[[Dict, TranslateFunc], str],
    occupancy_cache: Dict[str, Dict],
    external_profile_process_map: Dict[str, List[int]],
    is_profile_keepalive_running: Callable[[str], bool],
    tr: TranslateFunc,
    control_profile: Optional[Dict] = None,
) -> str:
    resolved_profile = profile
    if not resolved_profile and selected_profile_name:
        for item in profiles:
            if item.get("profile_name") == selected_profile_name:
                resolved_profile = item
                break
    if not resolved_profile:
        return tr("status_no_profile_selected", "No profile selected.")

    base_text = build_profile_detail_text(resolved_profile, tr)
    profile_name = resolved_profile.get("profile_name", "")
    runtime_text = build_profile_runtime_state_text(
        profile_name,
        occupancy_cache,
        external_profile_process_map,
        is_profile_keepalive_running,
        tr,
        control_profile,
    )
    control_profile = control_profile if isinstance(control_profile, dict) else {}
    control_occupancy = control_profile.get("occupancy", {}) if isinstance(control_profile.get("occupancy", {}), dict) else {}
    occupancy = control_occupancy if control_occupancy else {}
    if not occupancy:
        cached = occupancy_cache.get(profile_name, {})
        if isinstance(cached, dict):
            occupancy = cached
    occupancy_text = "-"
    if occupancy:
        owner_pid = int(occupancy.get("owner_pid", 0) or 0)
        lease_expires_at = float(occupancy.get("lease_expires_at", 0.0) or 0.0)
        lease_text = "-"
        if lease_expires_at > 0:
            lease_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease_expires_at))
        occupancy_text = (
            f"{occupancy.get('scene_type', '-')}/{occupancy.get('state', '-')}"
            f" ({occupancy.get('owner_label', '-')})"
            f" pid={owner_pid or '-'}"
            f" lease={lease_text}"
        )
    return (
        f"{base_text}\n"
        f"{tr('detail_runtime_state', 'Runtime State')}: {runtime_text}\n"
        f"{tr('table_status', 'Status')}: {occupancy_text}"
    )


def build_bottom_stats_text(
    config: Dict,
    occupancy_cache: Dict[str, Dict],
    mcp_startup_in_progress: bool,
    mcp_status_cache,
    mcp_status_label_text: str,
    tr: TranslateFunc,
    control_profiles_payload: Optional[Dict] = None,
) -> str:
    profile_count = len(config.get("profiles", []))
    mcp_state_text = tr("bottom_mcp_stopped", "stopped")
    control_profiles_payload = control_profiles_payload if isinstance(control_profiles_payload, dict) else {}
    control_profiles = control_profiles_payload.get("profiles", []) if isinstance(control_profiles_payload.get("profiles", []), list) else []
    if control_profiles:
        active_session_count = len(
            [
                item
                for item in control_profiles
                if isinstance(item, dict)
                and isinstance(item.get("occupancy", {}), dict)
                and item.get("occupancy", {})
                and str(item.get("occupancy", {}).get("state", "") or "").strip() not in {"released", "idle", "start_failed"}
            ]
        )
    else:
        active_session_count = len(
            [
                item
                for item in occupancy_cache.values()
                if isinstance(item, dict) and item.get("state") not in {"released", "idle", "start_failed"}
            ]
        )
    if mcp_startup_in_progress:
        mcp_state_text = "starting"
    elif mcp_status_cache:
        mcp_state_text = "running"
    resolved_mcp_text = mcp_status_label_text or mcp_state_text
    return f"Profiles: {profile_count} | MCP: {resolved_mcp_text} | Sessions: {active_session_count}"


def build_occupancy_tab_payload(
    entries: Dict[str, Dict],
    recent_events: List[Dict],
    tr: TranslateFunc,
    profile_sort_key: Callable[[str], object],
    is_expired: Callable[[Dict], bool],
) -> Dict[str, str]:
    active_lines = []
    for profile_name in sorted(entries.keys(), key=profile_sort_key):
        active_lines.append(
            format_occupancy_entry_summary(profile_name, entries.get(profile_name, {}), tr, is_expired(entries.get(profile_name, {})))
        )
    if not active_lines:
        active_lines.append(tr("runtime_state_idle", "Idle"))
    summary_text = tr(
        "occupancy_summary_template",
        "Active occupancies: {active_count} | Recent events: {event_count}",
    ).format(active_count=len(entries), event_count=len(recent_events))
    event_lines = [format_occupancy_event_text(item) for item in recent_events[-40:]]
    payload = ["[ACTIVE]", *active_lines, "", "[EVENTS]", *event_lines]
    return {"summary_text": summary_text, "body_text": "\n".join(payload)}


def build_external_process_transition_messages(
    previous_map: Dict[str, List[int]],
    current_map: Dict[str, List[int]],
    tr: TranslateFunc,
) -> List[str]:
    messages: List[str] = []
    for profile_name in sorted(set(previous_map.keys()) | set(current_map.keys())):
        before = sorted(int(pid) for pid in previous_map.get(profile_name, []) if int(pid) > 0)
        after = sorted(int(pid) for pid in current_map.get(profile_name, []) if int(pid) > 0)
        if before == after:
            continue
        if not before and after:
            messages.append(
                tr("log_profile_runtime_detected_started", "{profile_name} external Chromium detected: {pid_text}").format(
                    profile_name=profile_name,
                    pid_text=", ".join(str(pid) for pid in after),
                )
            )
            continue
        if before and not after:
            messages.append(
                tr("log_profile_runtime_detected_stopped", "{profile_name} external Chromium fully exited.").format(
                    profile_name=profile_name,
                )
            )
            continue
        messages.append(
            tr("log_profile_runtime_detected_changed", "{profile_name} external Chromium changed: {pid_text}").format(
                profile_name=profile_name,
                pid_text=", ".join(str(pid) for pid in after),
            )
        )
    return messages


def load_profile_occupancy_cache(
    config_path: str,
    list_profile_occupancy: Callable[[str], Dict[str, Dict]],
    on_error: Optional[Callable[[str], None]] = None,
) -> Dict[str, Dict]:
    try:
        return list_profile_occupancy(config_path)
    except (TimeoutError, PermissionError, OSError):
        if on_error is not None:
            try:
                on_error("Profile occupancy registry is temporarily busy; using empty occupancy cache.")
            except Exception:
                pass
        return {}


def collect_stale_manual_occupancy_profiles(
    occupancy_cache: Dict[str, Dict],
    process_map: Dict[str, List[int]],
) -> List[str]:
    stale_profiles: List[str] = []
    for profile_name, occupancy in list(occupancy_cache.items()):
        if not isinstance(occupancy, dict):
            continue
        scene_type = str(occupancy.get("scene_type", "") or "").strip()
        state = str(occupancy.get("state", "") or "").strip()
        if scene_type != "manual" or state in {"released", "start_failed"}:
            continue
        if process_map.get(profile_name):
            continue
        stale_profiles.append(profile_name)
    return stale_profiles


def get_profile_status_color_hex(profile: Dict) -> Optional[str]:
    status = str(profile.get("last_keepalive_status", "never") or "never").strip().lower()
    if status == "success":
        return "#1e7d34"
    if status == "partial":
        return "#b26a00"
    if status == "failed":
        return "#c62828"
    if status == "stopped":
        return "#616161"
    return None


def build_profile_row_action_state(
    profile_name: str,
    external_profile_process_map: Dict[str, List[int]],
    is_profile_keepalive_running: Callable[[str], bool],
    is_profile_keepalive_ui_locked: Callable[[str], bool],
    keepalive_worker_present: bool,
    tr: TranslateFunc,
    control_profile: Optional[Dict] = None,
) -> Dict[str, object]:
    profile_name = str(profile_name or "").strip()
    row_locked = bool(is_profile_keepalive_ui_locked(profile_name))
    is_running = bool(is_profile_keepalive_running(profile_name))
    control_profile = control_profile if isinstance(control_profile, dict) else {}
    control_external_running = bool(int(control_profile.get("external_process_count", 0) or 0))
    control_profile_lock_active = bool(control_profile.get("profile_lock_active", False))
    external_running = control_external_running or control_profile_lock_active or bool(external_profile_process_map.get(profile_name, []))

    launch_text = tr("action_close", "Close") if external_running else tr("action_launch", "Launch")
    launch_tooltip = ""
    if row_locked:
        launch_tooltip = tr("info_keepalive_already_running", "A keepalive task is already running. Please wait.")
    elif external_running:
        launch_tooltip = tr("profile_close_tooltip", "Close this profile and kill its Chromium processes.")

    keepalive_text = tr("action_stop", "Stop") if is_running else tr("action_keepalive", "Keepalive")
    keepalive_enabled = True
    keepalive_tooltip = ""
    keepalive_style = ""
    if is_running:
        keepalive_style = "background-color: #f8d7da; color: #b00020;"
    elif keepalive_worker_present:
        keepalive_enabled = False
        keepalive_tooltip = tr("info_keepalive_already_running", "A keepalive task is already running. Please wait.")

    return {
        "row_locked": row_locked,
        "is_running": is_running,
        "external_running": external_running,
        "launch_text": launch_text,
        "launch_enabled": not row_locked,
        "launch_tooltip": launch_tooltip,
        "keepalive_text": keepalive_text,
        "keepalive_enabled": keepalive_enabled,
        "keepalive_tooltip": keepalive_tooltip,
        "keepalive_style": keepalive_style,
    }


def build_profile_site_badge_state(
    site_name: str,
    site_enabled: bool,
    raw_info: Dict,
    base_label: str,
    icon_path: str,
    site_checkbox_tooltip: str,
    format_keepalive_site_status: Callable[[str, Dict, TranslateFunc], str],
    normalize_keepalive_site_result_for_display: Callable[[Dict], Dict],
    tr: TranslateFunc,
) -> Dict[str, object]:
    info = normalize_keepalive_site_result_for_display(raw_info)
    site_status = str((info or {}).get("status", "") or "").strip().lower()
    suffix_map = {
        "signed_out": tr("keepalive_site_badge_signed_out", "[Signed Out]"),
        "attention": tr("keepalive_site_badge_attention", "[Attention]"),
        "failed": tr("keepalive_site_badge_failed", "[Failed]"),
        "success": tr("keepalive_site_badge_success", "[OK]"),
    }
    checkbox_text = ""
    if not icon_path:
        checkbox_text = base_label
        if site_status in suffix_map:
            checkbox_text = f"{base_label} {suffix_map[site_status]}"
    style_map = {
        "signed_out": "QCheckBox { border: 1px solid #c62828; background: #fdecea; border-radius: 4px; padding: 2px 4px; }",
        "attention": "QCheckBox { border: 1px solid #b26a00; background: #fff4db; border-radius: 4px; padding: 2px 4px; }",
        "failed": "QCheckBox { border: 1px solid #8e0000; background: #fbe9e7; border-radius: 4px; padding: 2px 4px; }",
        "success": "QCheckBox { border: 1px solid #1e7d34; background: #e9f6ec; border-radius: 4px; padding: 2px 4px; }",
    }
    return {
        "site_name": site_name,
        "checked": bool(site_enabled),
        "icon_path": icon_path,
        "text": checkbox_text,
        "tooltip": format_keepalive_site_status(site_name, info, tr) if info else site_checkbox_tooltip,
        "style": style_map.get(site_status, "QCheckBox { border: 1px solid #d0d0d0; border-radius: 4px; padding: 2px 4px; }"),
        "status": site_status,
        "label": base_label,
    }


def normalize_mcp_path(path_value: object, default_path: str = "/mcp") -> str:
    path_text = str(path_value or default_path).strip() or default_path
    if not path_text.startswith("/"):
        path_text = "/" + path_text
    return path_text


def build_mcp_endpoint(settings: Dict, worker: bool = False) -> str:
    if worker:
        port = int(settings.get("worker_port", 28889))
        return f"http://127.0.0.1:{port}{normalize_mcp_path(settings.get('path', '/mcp'))}"
    host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(settings.get("port", 28888))
    return f"http://{host}:{port}{normalize_mcp_path(settings.get('path', '/mcp'))}"


def build_mcp_status_url(settings: Dict) -> str:
    host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(settings.get("port", 28888))
    return f"http://{host}:{port}/_daemon/status"


def build_control_status_url(control_settings: Dict, mcp_settings: Dict) -> str:
    host = str(control_settings.get("host", "")).strip() or str(mcp_settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(mcp_settings.get("port", 28888))
    path = str(control_settings.get("path", "/_control")).strip() or "/_control"
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{host}:{port}{path.rstrip('/')}/status"


def build_control_ping_url(control_settings: Dict, mcp_settings: Dict) -> str:
    base = build_control_status_url(control_settings, mcp_settings)
    if base.endswith("/status"):
        return base[: -len("/status")] + "/ping"
    return base.rstrip("/") + "/ping"


def resolve_mcp_connect_host_port(settings: Dict) -> tuple[str, int]:
    host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(settings.get("port", 28888))
    return host, port


def build_mcp_auth_headers(settings: Dict, admin: bool = False) -> Dict[str, str]:
    api_token = str(settings.get("api_token", "")).strip()
    if not api_token:
        return {}
    return {"Authorization": f"Bearer {api_token}"}


def build_control_auth_headers(settings: Dict) -> Dict[str, str]:
    token = str(settings.get("api_token", "")).strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def get_mcp_trace_path() -> str:
    return os.environ.get("CHROMIUM_ADVANCED_MCP_TRACE_PATH") or os.path.join(
        tempfile.gettempdir(),
        "chromium-advanced-mcp-trace.jsonl",
    )


def build_mcp_auth_warning_state(host: str, api_token: str, tr: TranslateFunc) -> Dict[str, str]:
    normalized_host = str(host or "127.0.0.1").strip().lower() or "127.0.0.1"
    is_local = normalized_host in {"127.0.0.1", "::1", "localhost"} or normalized_host.startswith("127.")
    token_text = str(api_token or "").strip()
    if not token_text:
        return {"visible": True, "text": tr("mcp_auth_warning_no_token", "Missing API token")}
    if not is_local:
        return {"visible": True, "text": tr("mcp_auth_warning_remote", "Remote binding requires API token")}
    return {"visible": False, "text": ""}


def build_mcp_process_arguments(
    settings: Dict,
    control_settings: Dict,
    config_path: str,
    transport_options: List[str],
    frozen: bool,
) -> List[str]:
    transport = str(settings.get("transport", transport_options[0] if transport_options else "streamable-http"))
    args: List[str] = []
    if not frozen:
        args.extend(["-m", "chromium_advanced.mcp_daemon"])
    args.extend(
        [
            "--transport",
            transport,
            "--host",
            str(settings.get("host", "127.0.0.1")),
            "--port",
            str(int(settings.get("port", 28888))),
            "--worker-port",
            str(int(settings.get("worker_port", 28889))),
            "--path",
            normalize_mcp_path(settings.get("path", "/mcp")),
            "--log-level",
            str(settings.get("log_level", "info")),
            "--idle-timeout-seconds",
            str(int(settings.get("idle_timeout_seconds", 60))),
            "--worker-policy",
            str(settings.get("worker_policy", "sticky") or "sticky"),
            "--config-path",
            config_path,
        ]
    )
    api_token = str(settings.get("api_token", "")).strip()
    if api_token:
        args.extend(["--api-token", api_token])
    control_token = str(control_settings.get("api_token", "")).strip()
    if control_token:
        args.extend(["--control-token", control_token])
    return args


def build_mcp_status_view_model(
    daemon_status: Dict,
    expected_enabled: bool,
    startup_in_progress: bool,
    tr: TranslateFunc,
) -> Dict[str, str]:
    daemon_running = bool(daemon_status)
    if daemon_running:
        worker_state = str(daemon_status.get("worker_state", "stopped"))
        worker_pid = daemon_status.get("worker_pid")
        active_requests = int(daemon_status.get("active_proxy_requests", 0) or 0)
        idle_seconds = int(daemon_status.get("idle_seconds", 0) or 0)
        idle_timeout = int(daemon_status.get("idle_timeout_seconds", 0) or 0)
        worker_policy = str(daemon_status.get("worker_policy", "sticky") or "sticky")
        status_build_ms = int(daemon_status.get("status_build_ms", 0) or 0)
        server_status = daemon_status.get("server_status") if isinstance(daemon_status.get("server_status"), dict) else {}
        external_scan_ms = int((server_status or {}).get("external_scan_ms", 0) or 0)
        daemon_pid = int(daemon_status.get("daemon_pid", 0) or 0)
        suffix = f" policy={worker_policy}, status={status_build_ms}ms, scan={external_scan_ms}ms, daemon={daemon_pid or '-'}"
        if worker_state == "running":
            return {
                "label": tr("mcp_state_running", "Running"),
                "detail": tr(
                    "mcp_status_detail_running",
                    "worker={worker_pid}, requests={active_requests}, idle={idle_seconds}s/{idle_timeout}s",
                ).format(
                    worker_pid=(worker_pid or "-"),
                    active_requests=active_requests,
                    idle_seconds=idle_seconds,
                    idle_timeout=idle_timeout,
                )
                + suffix,
            }
        return {
            "label": tr("mcp_state_guarding", "Guarding") if expected_enabled else tr("mcp_state_running", "Running"),
            "detail": tr(
                "mcp_status_detail_guarding",
                "worker stopped: {reason}",
            ).format(reason=(daemon_status.get("last_stop_reason") or "-"))
            + suffix,
        }
    if daemon_status:
        return {
            "label": tr("mcp_state_stopped", "Stopped"),
            "detail": tr("mcp_status_detail_stopped", "Stopped"),
        }
    if startup_in_progress:
        return {
            "label": tr("mcp_state_starting", "Starting"),
            "detail": tr("mcp_status_detail_starting", "Starting"),
        }
    if not expected_enabled:
        return {
            "label": tr("mcp_state_not_started", "Not started"),
            "detail": tr("mcp_status_detail_not_started", "Not started"),
        }
    return {
        "label": tr("mcp_state_waiting", "Waiting"),
        "detail": tr("mcp_status_detail_waiting", "Waiting for daemon"),
    }


def query_mcp_status_snapshot(
    *,
    force: bool,
    now_ts: float,
    cache: Dict,
    last_query_at: float,
    last_ok_at: float,
    consecutive_failures: int,
    cache_ttl_seconds: float,
    recent_health_grace_seconds: float,
    fetch_status: Callable[[], Dict],
    expected_pid: int = 0,
    expected_instance_id: str = "",
) -> Dict[str, object]:
    if (
        not force
        and cache
        and last_query_at > 0
        and (now_ts - last_query_at) < cache_ttl_seconds
    ):
        return {
            "status": cache,
            "cache": cache,
            "last_query_at": last_query_at,
            "last_ok_at": last_ok_at,
            "consecutive_failures": consecutive_failures,
        }
    try:
        status = fetch_status()
        if expected_pid:
            status_pid = int(status.get("daemon_pid", 0) or 0) if isinstance(status, dict) else 0
            if status_pid != int(expected_pid):
                raise RuntimeError(f"daemon pid mismatch: expected {expected_pid}, got {status_pid or '-'}")
        if expected_instance_id:
            status_instance_id = str(status.get("daemon_instance_id", "") or "").strip() if isinstance(status, dict) else ""
            if status_instance_id != str(expected_instance_id).strip():
                raise RuntimeError(
                    f"daemon instance mismatch: expected {expected_instance_id}, got {status_instance_id or '-'}"
                )
        if isinstance(status, dict) and status and not bool(status.get("daemon_ready", True)):
            raise RuntimeError("daemon is warming up")
        if isinstance(status, dict) and status:
            return {
                "status": status,
                "cache": status,
                "last_query_at": now_ts,
                "last_ok_at": now_ts,
                "consecutive_failures": 0,
            }
    except Exception:
        pass
    next_failures = consecutive_failures + 1
    if cache and last_ok_at > 0 and (now_ts - last_ok_at) < recent_health_grace_seconds:
        return {
            "status": cache,
            "cache": cache,
            "last_query_at": now_ts,
            "last_ok_at": last_ok_at,
            "consecutive_failures": next_failures,
        }
    return {
        "status": {},
        "cache": {},
        "last_query_at": now_ts,
        "last_ok_at": last_ok_at,
        "consecutive_failures": next_failures,
    }


def build_keepalive_schedule_view_model(
    *,
    enabled_profile_count: int,
    now_dt,
    schedule_dt,
    last_scheduled_date: str,
    keepalive_running: bool,
    triggered_today_text: str,
    format_datetime: Callable[[object], str],
    should_trigger_schedule: Callable[[object, object, str], bool],
    tr: TranslateFunc,
) -> Dict[str, str]:
    if enabled_profile_count <= 0:
        return {
            "status": tr("schedule_status_disabled", "Disabled"),
            "next_run": "-",
            "last_result": tr("schedule_result_enable_profiles", "Enable profiles first"),
        }
    today_text = now_dt.strftime("%Y-%m-%d")
    if keepalive_running:
        return {
            "status": tr("schedule_status_running", "Running"),
            "next_run": "-",
            "last_result": tr("schedule_result_triggered_today", "Triggered today: {today_text}").format(
                today_text=triggered_today_text or today_text
            ),
        }
    if last_scheduled_date == today_text:
        return {
            "status": tr("schedule_status_done_today", "Done today"),
            "next_run": format_datetime(schedule_dt + datetime.timedelta(days=1)),
            "last_result": tr("schedule_result_triggered_today", "Triggered today: {today_text}").format(
                today_text=triggered_today_text or today_text
            ),
        }
    if now_dt < schedule_dt:
        return {
            "status": tr("schedule_status_waiting", "Waiting"),
            "next_run": format_datetime(schedule_dt),
            "last_result": tr("schedule_result_not_triggered_today", "Not triggered today"),
        }
    if should_trigger_schedule(now_dt, schedule_dt, last_scheduled_date):
        return {
            "status": tr("schedule_status_due", "Due"),
            "next_run": tr("schedule_next_run_when_ready", "When current run finishes"),
            "last_result": tr("schedule_result_not_triggered_today", "Not triggered today"),
        }
    return {
        "status": tr("schedule_status_waiting", "Waiting"),
        "next_run": format_datetime(schedule_dt + datetime.timedelta(days=1)),
        "last_result": tr("schedule_result_not_triggered_today", "Not triggered today"),
    }


def build_external_process_refresh_plan(
    *,
    previous_map: Dict[str, List[int]],
    current_map: Dict[str, List[int]],
    occupancy_cache: Dict[str, Dict],
    tr: TranslateFunc,
) -> Dict[str, object]:
    previous_signature = serialize_external_profile_process_map(previous_map)
    current_signature = serialize_external_profile_process_map(current_map)
    signature_changed = previous_signature != current_signature
    stale_profiles = collect_stale_manual_occupancy_profiles(occupancy_cache, current_map)
    transition_messages: List[str] = []
    if signature_changed and previous_map:
        transition_messages = build_external_process_transition_messages(previous_map, current_map, tr)
    return {
        "signature": current_signature,
        "signature_changed": signature_changed,
        "stale_profiles": stale_profiles,
        "transition_messages": transition_messages,
        "needs_ui_refresh": bool(signature_changed or stale_profiles),
    }


def build_profile_table_row_payload(
    profile: Dict,
    *,
    status_payload: Dict[str, str],
    keepalive_running_globally: bool,
    action_state: Dict[str, object],
    tr: TranslateFunc,
) -> Dict[str, object]:
    profile_name = str(profile.get("profile_name", "") or "")
    account_text = str(profile.get("account", "") or "").strip() or "-"
    keepalive_enabled = bool(profile.get("keepalive_enabled", False))
    tooltip = str(profile.get("last_keepalive_message", "") or "").strip() or tr("status_keepalive_never", "Keepalive never ran")
    return {
        "profile_name": profile_name,
        "account_text": account_text,
        "status_text": str(status_payload.get("label", "") or ""),
        "status_tooltip": str(status_payload.get("tooltip", "") or tooltip),
        "row_tooltip": tooltip,
        "keepalive_checked": keepalive_enabled,
        "keepalive_enabled": not keepalive_running_globally,
        "launch_text": str(action_state.get("launch_text", "") or ""),
        "launch_enabled": bool(action_state.get("launch_enabled", False)),
        "launch_tooltip": str(action_state.get("launch_tooltip", "") or ""),
        "keepalive_button_text": str(action_state.get("keepalive_text", "") or ""),
        "keepalive_button_enabled": bool(action_state.get("keepalive_enabled", False)),
        "keepalive_button_tooltip": str(action_state.get("keepalive_tooltip", "") or ""),
        "keepalive_button_style": str(action_state.get("keepalive_style", "") or ""),
    }


def build_profile_table_view_model(
    profiles: List[Dict],
    *,
    selected_profile_name: str,
    get_status_payload: Callable[[Dict], Dict[str, str]],
    build_action_state: Callable[[Dict], Dict[str, object]],
    keepalive_running_globally: bool,
    tr: TranslateFunc,
) -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    for profile in profiles:
        profile_name = str(profile.get("profile_name", "") or "").strip()
        status_payload = get_status_payload(profile)
        action_state = build_action_state(profile)
        row_payload = build_profile_table_row_payload(
            profile,
            status_payload=status_payload,
            keepalive_running_globally=keepalive_running_globally,
            action_state=action_state,
            tr=tr,
        )
        rows.append(
            {
                "profile_name": profile_name,
                "row_payload": row_payload,
            }
        )
    selected_row = resolve_selected_row_index(
        profiles,
        selected_profile_name,
        id_key="profile_name",
        default_index=0,
    )
    return {
        "rows": rows,
        "selected_row": selected_row,
        "has_profiles": bool(profiles),
    }


def build_profile_site_selector_payloads(
    *,
    profile: Dict,
    keepalive_site_ids: List[str],
    keepalive_running_globally: bool,
    get_site_label: Callable[[str], str],
    get_site_icon_path: Callable[[str], str],
    site_checkbox_tooltip: str,
    format_keepalive_site_status: Callable[[str, Dict, TranslateFunc], str],
    normalize_keepalive_site_result_for_display: Callable[[Dict], Dict],
    tr: TranslateFunc,
) -> List[Dict[str, object]]:
    site_flags = profile.get("keepalive_sites", {}) or {}
    last_details = profile.get("last_keepalive_details", {}) or {}
    enabled_site_names = [site_name for site_name in keepalive_site_ids if bool(site_flags.get(site_name, False))]
    payloads: List[Dict[str, object]] = []
    for site_name in enabled_site_names:
        base_label = get_site_label(site_name)
        raw_info = last_details.get(site_name, {}) if isinstance(last_details, dict) else {}
        icon_path = get_site_icon_path(site_name)
        badge_state = build_profile_site_badge_state(
            site_name,
            bool(site_flags.get(site_name, False)),
            raw_info,
            base_label,
            icon_path,
            site_checkbox_tooltip,
            format_keepalive_site_status,
            normalize_keepalive_site_result_for_display,
            tr,
        )
        badge_state["enabled"] = not keepalive_running_globally
        payloads.append(badge_state)
    return payloads


def build_keepalive_worker_message_plan(
    *,
    kind: str,
    payload: Dict,
    keepalive_log_prefix: str,
    default_source_label: str,
    engine_name: str,
    now_date_text: str,
    trf: Callable[..., str],
) -> Dict[str, object]:
    profile_name = str((payload or {}).get("profile_name", "")).strip()
    acquired = bool((payload or {}).get("lock_acquired", True))
    plan: Dict[str, object] = {
        "kind": kind,
        "write_occupancy": False,
        "clear_occupancy": False,
        "reload_config": False,
        "load_occupancy_cache": False,
        "reset_keepalive_runtime": False,
        "enable_keepalive_buttons": False,
        "request_ui_refresh": False,
        "mark_scheduled_date": False,
        "scheduled_date": now_date_text,
        "summary_log": "",
        "error_log": "",
        "log_prefix": keepalive_log_prefix or default_source_label,
        "profile_name": profile_name,
        "occupancy_payload": {
            "profile_name": profile_name,
            "scene_type": "keepalive",
            "state": "active",
            "owner_label": keepalive_log_prefix or "Keepalive",
            "engine_name": engine_name,
        },
    }
    if kind == "__PROFILE_START__":
        plan["request_ui_refresh"] = True
        if profile_name and acquired:
            plan["write_occupancy"] = True
            plan["load_occupancy_cache"] = True
        return plan
    if kind == "__SUMMARY__":
        plan["summary_log"] = trf("log_keepalive_finished", status=payload.get("status"), message=payload.get("message"))
        plan["clear_occupancy"] = bool(profile_name)
        plan["reset_keepalive_runtime"] = True
        plan["enable_keepalive_buttons"] = True
        plan["load_occupancy_cache"] = True
        plan["reload_config"] = True
        if str((payload or {}).get("source", "")).startswith("internal-schedule") and str((payload or {}).get("status", "")) not in {"skipped", "stopped"}:
            plan["mark_scheduled_date"] = True
        return plan
    if kind == "__ERROR__":
        plan["error_log"] = trf("log_keepalive_failed", message=payload.get("message", ""))
        plan["clear_occupancy"] = bool(profile_name)
        plan["reset_keepalive_runtime"] = True
        plan["enable_keepalive_buttons"] = True
        plan["load_occupancy_cache"] = True
        plan["reload_config"] = True
        return plan
    return plan


def build_keepalive_plugin_table_rows(records: List[Dict], tr: TranslateFunc) -> List[List[str]]:
    rows: List[List[str]] = []
    for record in records:
        source_text = record.get("source") or tr("plugin_type_system", "System")
        rows.append(
            [
                str(record.get("site_id", "") or ""),
                str(record.get("display_name", "") or ""),
                tr("plugin_type_system", "System") if record.get("builtin") else tr("plugin_type_external", "External"),
                str(source_text or ""),
            ]
        )
    return rows


def build_keepalive_plugin_selection_view_model(record: Optional[Dict], *, keepalive_worker_present: bool, tr: TranslateFunc) -> Dict[str, object]:
    if not isinstance(record, dict) or not record:
        return {
            "selected_plugin_site_id": "",
            "detail_site_id": "-",
            "detail_display_name": "-",
            "detail_type": "-",
            "detail_source": "-",
            "detail_home_url": "-",
            "detail_icon_url": "-",
            "editable": False,
            "allow_edit": False,
            "base_status": tr("plugin_status_empty", "No plugin selected"),
        }
    editable = not bool(record.get("builtin"))
    allow_edit = editable and not keepalive_worker_present
    return {
        "selected_plugin_site_id": str(record.get("site_id", "") or "").strip(),
        "detail_site_id": str(record.get("site_id", "") or "-"),
        "detail_display_name": str(record.get("display_name", "") or "-"),
        "detail_type": tr("plugin_type_system", "System") if record.get("builtin") else tr("plugin_type_external", "External"),
        "detail_source": str(record.get("source", "") or tr("plugin_type_system", "System")),
        "detail_home_url": str(record.get("home_url", "") or "-"),
        "detail_icon_url": str(record.get("icon_url", "") or "-"),
        "editable": editable,
        "allow_edit": allow_edit,
        "base_status": tr("plugin_status_readonly", "Readonly") if not editable else tr("plugin_status_editable", "Editable"),
    }


def resolve_selected_row_index(records: List[Dict], selected_id: str, *, id_key: str, default_index: int = 0) -> int:
    if not records:
        return -1
    selected_id = str(selected_id or "").strip()
    if not selected_id:
        return max(0, int(default_index))
    for index, record in enumerate(records):
        if str(record.get(id_key, "") or "").strip() == selected_id:
            return index
    return max(0, int(default_index))


def build_keepalive_plugin_table_view_model(
    records: List[Dict],
    selected_site_id: str,
    tr: TranslateFunc,
) -> Dict[str, object]:
    rows = build_keepalive_plugin_table_rows(records, tr)
    has_records = bool(records)
    selected_row = resolve_selected_row_index(records, selected_site_id, id_key="site_id", default_index=0) if has_records else -1
    empty_selection = build_keepalive_plugin_selection_view_model(None, keepalive_worker_present=False, tr=tr)
    return {
        "rows": rows,
        "has_records": has_records,
        "selected_row": selected_row,
        "empty_selection": empty_selection,
    }


def build_mcp_startup_plan(
    *,
    terminated_pids: List[int],
    startup_timeout_ms: int,
    tr: TranslateFunc,
    trf: Callable[..., str],
    now_dt,
) -> Dict[str, object]:
    return {
        "cleanup_log": trf("log_mcp_cleanup_stale", pid_text=", ".join(str(pid) for pid in terminated_pids)) if terminated_pids else "",
        "log_prefix": "MCP",
        "error_prefix": "MCP-ERR",
        "prepare_log": tr("log_mcp_prepare_start", "Preparing MCP start"),
        "startup_deadline": now_dt + datetime.timedelta(milliseconds=max(0, int(startup_timeout_ms))),
        "set_restart_pending": False,
        "set_stop_requested": False,
        "set_owned_process": True,
        "set_startup_in_progress": True,
        "reset_consecutive_failures": True,
    }


def build_mcp_startup_failure_plan(*, error, trf: Callable[..., str]) -> Dict[str, object]:
    return {
        "startup_in_progress": False,
        "startup_deadline": None,
        "owned_process": False,
        "error_log": trf("log_mcp_error", error=error),
        "error_prefix": "MCP-ERR",
        "request_status_refresh": True,
    }


def build_mcp_stop_plan(*, update_checkbox: bool, tr: TranslateFunc) -> Dict[str, object]:
    return {
        "startup_in_progress": False,
        "startup_deadline": None,
        "increment_startup_token": True,
        "reset_consecutive_failures": True,
        "restart_pending": False,
        "stop_requested": True,
        "prepare_log": tr("log_mcp_prepare_stop", "Preparing MCP stop"),
        "cleanup_residue": True,
        "launch_pid": 0,
        "update_checkbox": bool(update_checkbox),
        "owned_process": False,
        "clear_status_cache": True,
        "request_status_refresh": True,
    }
