from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time
from typing import Dict, List

from PyQt5.QtCore import QTimer, Qt

from chromium_advanced.browser_engines.constants import DEFAULT_BROWSER_ENGINE
from chromium_advanced.browser_engines.factory import normalize_browser_engine_name
from chromium_advanced.chromium_profile_lib import (
    get_hidden_subprocess_kwargs,
    get_runtime_launch_cwd,
    save_app_config,
)
from chromium_advanced.gui.gui_runtime import (
    fetch_json,
    find_project_mcp_processes,
    get_frozen_companion_executable,
    terminate_project_mcp_processes,
)
from chromium_advanced.gui.gui_state import (
    build_control_auth_headers,
    build_control_ping_url,
    build_control_status_url,
    build_mcp_process_arguments as build_mcp_process_arguments_helper,
    build_mcp_startup_failure_plan,
    build_mcp_startup_plan,
    build_mcp_status_view_model,
    build_mcp_stop_plan,
    get_mcp_trace_path as get_mcp_trace_path_helper,
    normalize_mcp_path,
    query_mcp_status_snapshot,
    resolve_mcp_connect_host_port,
)


def _safe_attr(window, name: str, default):
    try:
        return object.__getattribute__(window, name)
    except Exception:
        return default


def _fetch_json(window, *args, **kwargs):
    override = _safe_attr(window, "fetch_json", None)
    if callable(override):
        return override(*args, **kwargs)
    from chromium_advanced import chromium_manage_gui as gui_module

    return gui_module.fetch_json(*args, **kwargs)


def _terminate_project_mcp_processes(window, *, exclude_pid: int):
    override = _safe_attr(window, "terminate_project_mcp_processes", None)
    if callable(override):
        return override(exclude_pid=exclude_pid)
    return terminate_project_mcp_processes(exclude_pid=exclude_pid)


def _find_project_mcp_processes(window, *, exclude_pid: int):
    override = _safe_attr(window, "find_project_mcp_processes", None)
    if callable(override):
        return override(exclude_pid=exclude_pid)
    return find_project_mcp_processes(exclude_pid=exclude_pid)


def get_mcp_endpoint(window) -> str:
    settings = window.config.get("mcp", {})
    host = str(settings.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    port = int(settings.get("port", 28888))
    return f"http://{host}:{port}{normalize_mcp_path(settings.get('path', '/mcp'))}"


def get_mcp_worker_endpoint(window) -> str:
    settings = window.config.get("mcp", {})
    port = int(settings.get("worker_port", 28889))
    return f"http://127.0.0.1:{port}{normalize_mcp_path(settings.get('path', '/mcp'))}"


def get_mcp_trace_path() -> str:
    return get_mcp_trace_path_helper()


def get_mcp_status_url(window) -> str:
    return build_control_ping_url(window.config.get("control", {}), window.config.get("mcp", {}))


def get_mcp_auth_headers(window) -> Dict[str, str]:
    settings = window.config.get("control", {}) if isinstance(window.config, dict) else {}
    return build_control_auth_headers(settings)


def get_mcp_admin_auth_headers(window) -> Dict[str, str]:
    settings = window.config.get("control", {}) if isinstance(window.config, dict) else {}
    return build_control_auth_headers(settings)


def get_mcp_connect_host_port(window):
    return resolve_mcp_connect_host_port(window.config.get("mcp", {}))


def is_mcp_expected_enabled(window) -> bool:
    return bool(window.config.get("mcp", {}).get("enabled", False))


def query_mcp_status(
    window,
    *,
    force: bool = False,
    expected_pid: int = 0,
    expected_instance_id: str = "",
) -> Dict:
    now_ts = time.monotonic()
    result = query_mcp_status_snapshot(
        force=force,
        now_ts=now_ts,
        cache=window.mcp_status_cache,
        last_query_at=window.mcp_status_last_query_at,
        last_ok_at=window.mcp_status_last_ok_at,
        consecutive_failures=window.mcp_status_consecutive_failures,
        cache_ttl_seconds=_safe_attr(window, "MCP_STATUS_CACHE_TTL_SECONDS", 3.0),
        recent_health_grace_seconds=_safe_attr(window, "MCP_RECENT_HEALTH_GRACE_SECONDS", 30.0),
        fetch_status=lambda: _fetch_json(
            window,
            get_mcp_status_url(window),
            timeout=_safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6),
            headers=get_mcp_auth_headers(window),
        ),
        expected_pid=expected_pid,
        expected_instance_id=expected_instance_id,
    )
    window.mcp_status_cache = result["cache"]
    window.mcp_status_last_query_at = float(result["last_query_at"])
    window.mcp_status_last_ok_at = float(result["last_ok_at"])
    window.mcp_status_consecutive_failures = int(result["consecutive_failures"])
    return result["status"]


def query_mcp_ping(window) -> Dict:
    try:
        payload = _fetch_json(
            window,
            get_mcp_status_url(window),
            timeout=_safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6),
            headers=get_mcp_auth_headers(window),
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _build_control_url(window, suffix: str) -> str:
    base = build_control_status_url(window.config.get("control", {}), window.config.get("mcp", {}))
    root = base[: -len("/status")] if base.endswith("/status") else base.rstrip("/")
    return root + suffix


def get_control_profiles_url(window) -> str:
    return _build_control_url(window, "/profiles?include_runtime_snapshot=false")


def get_control_events_url(window, limit: int = 80) -> str:
    bounded_limit = max(1, int(limit or 80))
    return _build_control_url(window, f"/events?limit={bounded_limit}")


def get_control_profile_url(window, profile_name: str, include_runtime_snapshot: bool = False) -> str:
    from urllib.parse import quote

    encoded_profile_name = quote(str(profile_name or "").strip(), safe="")
    query_text = "true" if include_runtime_snapshot else "false"
    return _build_control_url(window, f"/profiles/{encoded_profile_name}?include_runtime_snapshot={query_text}")


def get_control_keepalive_url(window) -> str:
    return _build_control_url(window, "/keepalive")


def get_control_keepalive_run_url(window) -> str:
    return get_control_keepalive_url(window).rstrip("/") + "/run"


def get_control_keepalive_stop_url(window) -> str:
    return get_control_keepalive_url(window).rstrip("/") + "/stop"


def get_control_profile_launch_url(window, profile_name: str) -> str:
    from urllib.parse import quote

    encoded_profile_name = quote(str(profile_name or "").strip(), safe="")
    return _build_control_url(window, f"/profiles/{encoded_profile_name}/launch")


def get_control_profile_close_url(window, profile_name: str) -> str:
    from urllib.parse import quote

    encoded_profile_name = quote(str(profile_name or "").strip(), safe="")
    return _build_control_url(window, f"/profiles/{encoded_profile_name}/close")


def get_control_plugins_url(window) -> str:
    return _build_control_url(window, "/plugins")


def get_control_plugin_url(window, plugin_id: str) -> str:
    from urllib.parse import quote

    encoded_plugin_id = quote(str(plugin_id or "").strip(), safe="")
    return _build_control_url(window, f"/plugins/{encoded_plugin_id}")


def query_control_profiles(window, force: bool = False) -> Dict:
    now_ts = time.monotonic()
    if (
        not force
        and window.control_profiles_cache
        and window.control_profiles_last_query_at > 0
        and (now_ts - window.control_profiles_last_query_at) < getattr(window, "CONTROL_PROFILES_REFRESH_INTERVAL_SECONDS", 2.5)
    ):
        return window.control_profiles_cache
    try:
        payload = _fetch_json(
            window,
            get_control_profiles_url(window),
            timeout=max(1.5, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
            headers=get_mcp_auth_headers(window),
        )
        if isinstance(payload, dict):
            window.control_profiles_cache = payload
            window.control_profiles_last_query_at = now_ts
            prune = _safe_attr(window, "prune_fallback_profile_occupancy_cache", None)
            if callable(prune):
                prune()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return window.control_profiles_cache if isinstance(window.control_profiles_cache, dict) else {}


def query_control_events(window, limit: int = 80) -> Dict:
    try:
        payload = _fetch_json(
            window,
            get_control_events_url(window, limit=limit),
            timeout=max(1.5, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
            headers=get_mcp_auth_headers(window),
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def query_control_keepalive(window) -> Dict:
    now_ts = time.monotonic()
    if (
        window.control_keepalive_cache
        and window.control_keepalive_last_query_at > 0
        and (now_ts - window.control_keepalive_last_query_at) < getattr(window, "CONTROL_KEEPALIVE_REFRESH_INTERVAL_SECONDS", 10.0)
    ):
        return window.control_keepalive_cache
    try:
        payload = _fetch_json(
            window,
            get_control_keepalive_url(window),
            timeout=max(1.5, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
            headers=get_mcp_auth_headers(window),
        )
        if isinstance(payload, dict):
            window.control_keepalive_cache = payload
            window.control_keepalive_last_query_at = now_ts
            return payload
        return {}
    except Exception:
        return window.control_keepalive_cache if isinstance(window.control_keepalive_cache, dict) else {}


def invalidate_control_keepalive_cache(window) -> None:
    window.control_keepalive_cache = {}
    window.control_keepalive_last_query_at = 0.0


def query_control_keepalive_runtime(window) -> Dict:
    payload = query_control_keepalive(window)
    runtime = payload.get("runtime", {}) if isinstance(payload.get("runtime", {}), dict) else {}
    return runtime


def control_launch_profile(window, profile_name: str) -> Dict:
    return _fetch_json(
        window,
        get_control_profile_launch_url(window, profile_name),
        method="POST",
        headers=get_mcp_auth_headers(window),
        timeout=max(2.0, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
    )


def control_close_profile(window, profile_name: str) -> Dict:
    return _fetch_json(
        window,
        get_control_profile_close_url(window, profile_name),
        method="POST",
        headers=get_mcp_auth_headers(window),
        timeout=max(2.0, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
    )


def control_start_keepalive(window, selected_profiles: List[str], source: str) -> Dict:
    return _fetch_json(
        window,
        get_control_keepalive_run_url(window),
        method="POST",
        headers=get_mcp_auth_headers(window),
        json_payload={
            "selected_profiles": [str(item).strip() for item in (selected_profiles or []) if str(item).strip()],
            "source": str(source or "manual"),
        },
        timeout=max(2.0, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
    )


def control_stop_keepalive(window) -> Dict:
    return _fetch_json(
        window,
        get_control_keepalive_stop_url(window),
        method="POST",
        headers=get_mcp_auth_headers(window),
        timeout=max(2.0, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
    )


def query_control_plugins(window) -> Dict:
    try:
        payload = _fetch_json(
            window,
            get_control_plugins_url(window),
            timeout=max(1.5, _safe_attr(window, "MCP_STATUS_QUERY_TIMEOUT_SECONDS", 0.6)),
            headers=get_mcp_auth_headers(window),
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def refresh_mcp_status_ui(window):
    window.mcp_endpoint_label.setText(get_mcp_endpoint(window))
    window.mcp_worker_endpoint_label.setText(get_mcp_worker_endpoint(window))
    window.mcp_trace_path_label.setText(get_mcp_trace_path())
    window.mcp_default_engine_label.setText(
        f"{normalize_browser_engine_name(window.config.get('app', {}).get('browser_engine', DEFAULT_BROWSER_ENGINE))}"
        f" / {str(window.config.get('app', {}).get('concurrency_mode', 'per_profile_live') or 'per_profile_live')}"
    )
    daemon_status = query_mcp_status(window)
    view_model = build_mcp_status_view_model(
        daemon_status,
        is_mcp_expected_enabled(window),
        window.mcp_startup_in_progress,
        window.tr,
    )
    window.mcp_status_label.setText(view_model["label"])
    window.mcp_status_detail_label.setText(view_model["detail"])
    window.refresh_bottom_stats()


def maybe_refresh_profiles_in_background(window, *, force: bool = False) -> None:
    if window.gui_bootstrap_in_progress or window.is_ui_interaction_busy():
        return
    now_ts = time.monotonic()
    if (
        not force
        and window.background_profiles_refresh_last_at > 0
        and (now_ts - window.background_profiles_refresh_last_at) < _safe_attr(window, "BACKGROUND_PROFILES_REFRESH_INTERVAL_SECONDS", 20.0)
    ):
        return
    payload = query_control_profiles(window, force=force)
    window.background_profiles_refresh_last_at = now_ts
    if not isinstance(payload, dict) or not payload:
        return
    keepalive_runtime = None
    cached_context = getattr(window, "current_ui_refresh_context", {}) if isinstance(getattr(window, "current_ui_refresh_context", {}), dict) else {}
    if force:
        keepalive_runtime = query_control_keepalive_runtime(window)
    else:
        keepalive_runtime = cached_context.get("keepalive_runtime")
        if not isinstance(keepalive_runtime, dict):
            keepalive_runtime = window.control_keepalive_cache.get("runtime", {}) if isinstance(window.control_keepalive_cache, dict) else {}
        if not isinstance(keepalive_runtime, dict) or not keepalive_runtime:
            keepalive_runtime = query_control_keepalive_runtime(window)
    window.current_ui_refresh_context = {
        "control_profiles_payload": payload,
        "keepalive_runtime": keepalive_runtime,
    }
    window.refresh_external_profile_process_state()
    window.request_ui_refresh(table=True, selected_status=True, bottom_stats=True, occupancy_tab=True)


def apply_initial_mcp_state(window):
    if window.mcp_startup_applied:
        return
    window.mcp_startup_applied = True
    if not bool(window.config.get("mcp", {}).get("enabled", False)):
        return
    if window.mcp_bootstrap_prelaunched:
        window.mcp_startup_in_progress = True
        window.mcp_startup_token += 1
        window.mcp_startup_deadline = datetime.datetime.now() + datetime.timedelta(
            milliseconds=_safe_attr(window, "MCP_HEALTHCHECK_START_TIMEOUT_MS", 30000)
        )
        window.append_mcp_log("Detected prelaunched daemon bootstrap; waiting for readiness", prefix="MCP")
        QTimer.singleShot(0, lambda token=window.mcp_startup_token: check_mcp_health_after_start(window, token))
        return
    try:
        start_mcp_service(window)
    except Exception as exc:
        window.append_mcp_log(window.trf("log_mcp_error", error=exc), prefix="MCP-ERR")
        finish_mcp_startup_failure(window)


def finish_mcp_startup_failure(window):
    window.mcp_startup_in_progress = False
    window.mcp_startup_deadline = None
    window.mcp_stop_requested = True
    cleanup_mcp_process_residue(window)
    window.mcp_launch_pid = 0
    window.mcp_launch_instance_id = ""
    window.mcp_owned_process = False
    window.mcp_status_cache = {}
    window.invalidate_control_profiles_cache()
    window.request_ui_refresh(mcp_status=True)


def check_mcp_health_after_start(window, startup_token: int):
    if startup_token != window.mcp_startup_token or not window.mcp_startup_in_progress:
        return
    try:
        query_override = _safe_attr(window, "query_mcp_status", None)
        if callable(query_override) and getattr(query_override, "__self__", None) is not window:
            status = query_override(
                force=True,
                expected_pid=window.mcp_launch_pid,
                expected_instance_id=window.mcp_launch_instance_id,
            )
        else:
            status = query_mcp_status(
                window,
                force=True,
                expected_pid=window.mcp_launch_pid,
                expected_instance_id=window.mcp_launch_instance_id,
            )
        if status:
            if not window.mcp_launch_instance_id:
                window.mcp_launch_instance_id = str(status.get("daemon_instance_id", "") or "").strip()
            window.mcp_owned_process = True
            window.mcp_startup_in_progress = False
            window.mcp_startup_deadline = None
            window.request_ui_refresh(mcp_status=True)
            return
    except Exception:
        pass
    deadline = window.mcp_startup_deadline
    if isinstance(deadline, datetime.datetime) and datetime.datetime.now() >= deadline:
        window.append_mcp_log(window.tr("log_mcp_watchdog_port_down"), prefix="MCP-ERR")
        finish_mcp_startup_failure(window)
        return
    QTimer.singleShot(
        _safe_attr(window, "MCP_HEALTHCHECK_POLL_INTERVAL_MS", 400),
        lambda token=startup_token: check_mcp_health_after_start(window, token),
    )


def build_mcp_process_arguments(window) -> List[str]:
    settings = window.config.get("mcp", {})
    return build_mcp_process_arguments_helper(
        settings,
        window.config.get("control", {}),
        window.config_path,
        _safe_attr(window, "MCP_TRANSPORT_OPTIONS", ["streamable-http", "http", "sse"]),
        bool(getattr(sys, "frozen", False)),
    )


def start_mcp_service(window):
    window.save_mcp_settings()
    if window.mcp_startup_in_progress:
        window.request_ui_refresh(mcp_status=True)
        return
    if query_mcp_status(window, force=True):
        window.mcp_owned_process = True
        window.request_ui_refresh(mcp_status=True)
        return

    terminated_pids = _terminate_project_mcp_processes(window, exclude_pid=os.getpid())
    plan = build_mcp_startup_plan(
        terminated_pids=terminated_pids,
        startup_timeout_ms=_safe_attr(window, "MCP_HEALTHCHECK_START_TIMEOUT_MS", 30000),
        tr=window.tr,
        trf=window.trf,
        now_dt=datetime.datetime.now(),
    )
    if str(plan.get("cleanup_log", "")).strip():
        window.append_mcp_log(str(plan["cleanup_log"]), prefix=str(plan.get("log_prefix", "MCP")))

    window.mcp_restart_pending = bool(plan.get("set_restart_pending", False))
    window.mcp_stop_requested = bool(plan.get("set_stop_requested", False))
    window.mcp_owned_process = bool(plan.get("set_owned_process", True))
    window.mcp_startup_in_progress = bool(plan.get("set_startup_in_progress", True))
    window.mcp_startup_token += 1
    window.mcp_launch_instance_id = ""
    if bool(plan.get("reset_consecutive_failures")):
        window.mcp_status_consecutive_failures = 0
    window.mcp_startup_deadline = plan.get("startup_deadline")
    window.append_mcp_log(str(plan.get("prepare_log", "")))
    program = sys.executable
    if getattr(sys, "frozen", False):
        program = get_frozen_companion_executable("ChromiumMcpDaemon")
    command = [program, *build_mcp_process_arguments(window)]
    try:
        process = subprocess.Popen(
            command,
            cwd=get_runtime_launch_cwd(program),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **get_hidden_subprocess_kwargs(),
        )
        window.mcp_launch_pid = int(process.pid or 0)
    except Exception as exc:
        failure_plan = build_mcp_startup_failure_plan(error=exc, trf=window.trf)
        window.mcp_startup_in_progress = bool(failure_plan.get("startup_in_progress", False))
        window.mcp_startup_deadline = failure_plan.get("startup_deadline")
        window.mcp_owned_process = bool(failure_plan.get("owned_process", False))
        window.append_mcp_log(
            str(failure_plan.get("error_log", "")),
            prefix=str(failure_plan.get("error_prefix", "MCP-ERR")),
        )
        if bool(failure_plan.get("request_status_refresh", True)):
            window.request_ui_refresh(mcp_status=True)
        return
    window.request_ui_refresh(mcp_status=True)
    QTimer.singleShot(0, lambda token=window.mcp_startup_token: check_mcp_health_after_start(window, token))


def stop_mcp_service(window, update_checkbox: bool = True):
    plan = build_mcp_stop_plan(update_checkbox=update_checkbox, tr=window.tr)
    window.mcp_startup_in_progress = bool(plan.get("startup_in_progress", False))
    window.mcp_startup_deadline = plan.get("startup_deadline")
    if bool(plan.get("increment_startup_token", True)):
        window.mcp_startup_token += 1
    if bool(plan.get("reset_consecutive_failures", True)):
        window.mcp_status_consecutive_failures = 0
    window.mcp_restart_pending = bool(plan.get("restart_pending", False))
    window.mcp_stop_requested = bool(plan.get("stop_requested", True))
    window.append_mcp_log(str(plan.get("prepare_log", "")))
    if bool(plan.get("cleanup_residue", True)):
        cleanup_mcp_process_residue(window)
    window.mcp_launch_pid = int(plan.get("launch_pid", 0) or 0)
    window.mcp_launch_instance_id = ""
    if bool(plan.get("update_checkbox", False)):
        window.mcp_service_checkbox.blockSignals(True)
        window.mcp_service_checkbox.setChecked(False)
        window.mcp_service_checkbox.blockSignals(False)
    window.mcp_owned_process = bool(plan.get("owned_process", False))
    if bool(plan.get("clear_status_cache", True)):
        window.mcp_status_cache = {}
    window.invalidate_control_profiles_cache()
    if bool(plan.get("request_status_refresh", True)):
        window.request_ui_refresh(mcp_status=True)


def restart_mcp_service(window):
    if not is_mcp_expected_enabled(window):
        window.mcp_service_checkbox.setChecked(True)
        return
    stop_mcp_service(window, update_checkbox=False)
    window.mcp_restart_pending = False
    QTimer.singleShot(800, lambda: start_mcp_service(window))


def on_mcp_service_checkbox_changed(window, state):
    enabled = state == Qt.Checked
    if enabled:
        start_mcp_service(window)
    else:
        window.config.setdefault("mcp", {})
        window.config["mcp"]["enabled"] = False
        window.config = save_app_config(window.config, window.config_path)
        stop_mcp_service(window, update_checkbox=False)


def on_mcp_process_output(window):
    if window.mcp_process is None:
        return
    text = bytes(window.mcp_process.readAllStandardOutput()).decode(errors="replace")
    if text.strip():
        window.append_mcp_log(text.rstrip())


def on_mcp_process_state_changed(window, _state):
    window.request_ui_refresh(mcp_status=True)


def cleanup_mcp_process_residue(window):
    override = _safe_attr(window, "cleanup_mcp_process_residue", None)
    if callable(override) and getattr(override, "__self__", None) is not window:
        return override()
    terminated_pids = _terminate_project_mcp_processes(window, exclude_pid=os.getpid())
    if terminated_pids:
        try:
            message = window.trf("log_mcp_cleanup_stale", pid_text=", ".join(str(pid) for pid in terminated_pids))
        except Exception:
            message = f"cleaned stale daemon pids: {', '.join(str(pid) for pid in terminated_pids)}"
        window.append_mcp_log(message, prefix="MCP")


def on_mcp_watchdog_timer(window):
    if not is_mcp_expected_enabled(window) or window.mcp_startup_in_progress:
        return
    ping_status = query_mcp_ping(window)
    if ping_status:
        had_failures = window.mcp_status_consecutive_failures > 0
        window.mcp_status_consecutive_failures = 0
        if had_failures:
            window.request_ui_refresh(mcp_status=True)
        maybe_refresh_profiles_in_background(window, force=False)
        return
    window.mcp_status_consecutive_failures += 1
    if window.mcp_status_consecutive_failures >= 3:
        if _find_project_mcp_processes(window, exclude_pid=os.getpid()):
            return
        window.append_mcp_log(window.tr("log_mcp_watchdog_not_running"), prefix="MCP-WARN")
        start_mcp_service(window)
        return
    window.request_ui_refresh(mcp_status=True)
