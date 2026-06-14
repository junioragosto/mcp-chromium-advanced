import copy
import os
import psutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from chromium_advanced.browser_session_kernel import ManagedBrowserSession
from chromium_advanced.browser_engines.factory import create_browser_engine, resolve_browser_engine_name
from chromium_advanced.chromium_profile_lib import (
    build_runtime_config_overrides,
    clear_stale_lockfile,
    cleanup_keepalive_profile_processes,
    ensure_profile_bookmarks_initialized,
    find_running_chromium_processes,
    get_chromium_processes_for_profile,
    get_lock_path,
    get_mirror_lock_path,
    get_profile_runtime_lock_path,
    is_process_alive,
    load_app_config,
    normalize_config,
    normalize_fs_path,
    now_text,
    read_recent_jsonl_events,
    SingleRunLock,
)
from chromium_advanced.mirror_manager import MirrorManager
from chromium_advanced.occupancy_registry import (
    clear_profile_occupancy,
    get_occupancy_events_path,
    list_profile_occupancy_entries,
    load_profile_occupancy_registry,
    occupancy_entry_is_expired,
    write_profile_occupancy,
)


@dataclass
class SessionRecord:
    session_id: str
    profile_name: str
    engine_name: str
    created_at: float
    last_used_at: float
    browser_session: object
    runtime_mode: str
    runtime_root: str
    mirror_generated_at: str
    cleanup_runtime_on_close: bool
    profile_lock: object = None
    launch_pid: int = 0
    alive_probe_failures: int = 0
    last_alive_probe_at: float = 0.0
    cached_current_url: str = ""
    cached_title: str = ""
    cached_alive: bool = True

    def refresh_cached_summary(self) -> None:
        try:
            summary = self.browser_session.get_summary()
            self.cached_current_url = getattr(summary, "current_url", "") or ""
            self.cached_title = getattr(summary, "title", "") or ""
            self.cached_alive = bool(getattr(summary, "alive", True))
        except Exception:
            self.cached_alive = True

    def to_summary(self, refresh: bool = True) -> Dict:
        if refresh:
            self.refresh_cached_summary()
        return {
            "session_id": self.session_id,
            "profile_name": self.profile_name,
            "engine_name": self.engine_name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "current_url": self.cached_current_url,
            "title": self.cached_title,
            "alive": self.cached_alive,
            "runtime_mode": self.runtime_mode,
            "runtime_root": self.runtime_root,
            "mirror_generated_at": self.mirror_generated_at,
        }


class SessionManager:
    MCP_STALE_RECLAIM_GRACE_SECONDS = 15.0
    SESSION_ALIVE_PROBE_GRACE_SECONDS = 20.0
    SESSION_ALIVE_MAX_CONSECUTIVE_FAILURES = 3
    EXTERNAL_BUSY_CACHE_TTL_SECONDS = 1.5
    STARTING_PROFILE_STALE_SECONDS = 120.0

    def __init__(self, config_path: Optional[str] = None, config_override: Optional[Dict] = None):
        self.config_path = config_path
        self._config_override = copy.deepcopy(config_override) if isinstance(config_override, dict) else None
        self._lock = threading.RLock()
        self._sessions_by_id: Dict[str, SessionRecord] = {}
        self._session_ids_by_profile: Dict[str, List[str]] = {}
        self._starting_profiles: Dict[str, float] = {}
        self._external_busy_cache: Dict = {}
        self._external_busy_cache_at = 0.0

    def _load_config(self) -> Dict:
        if isinstance(self._config_override, dict):
            return copy.deepcopy(self._config_override)
        return load_app_config(self.config_path)

    def _load_occupancy_registry(self) -> Dict:
        return load_profile_occupancy_registry(tolerate_lock_timeout=True)

    def _get_starting_profiles_snapshot(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._starting_profiles)

    def _prune_stale_starting_profiles_locked(self) -> None:
        now_ts = time.time()
        stale_names = [
            name
            for name, started_at in self._starting_profiles.items()
            if (now_ts - float(started_at or 0.0)) > self.STARTING_PROFILE_STALE_SECONDS
        ]
        for name in stale_names:
            self._starting_profiles.pop(name, None)

    def _reconcile_starting_profiles_locked(self) -> None:
        self._prune_stale_starting_profiles_locked()
        if not self._starting_profiles:
            return
        registry = self._load_occupancy_registry()
        profiles = registry.get("profiles", {}) if isinstance(registry, dict) else {}
        if not isinstance(profiles, dict):
            profiles = {}
        active_profiles = {session.profile_name for session in self._sessions_by_id.values()}
        for profile_name in list(self._starting_profiles.keys()):
            if profile_name in active_profiles:
                continue
            occupancy = profiles.get(profile_name, {})
            occupancy_state = str(occupancy.get("state", "") or "").strip().lower() if isinstance(occupancy, dict) else ""
            if occupancy_state == "starting":
                continue
            self._starting_profiles.pop(profile_name, None)

    def _visible_starting_profiles_locked(self) -> Dict[str, float]:
        self._reconcile_starting_profiles_locked()
        if not self._starting_profiles:
            return {}
        registry = self._load_occupancy_registry()
        profiles = registry.get("profiles", {}) if isinstance(registry, dict) else {}
        if not isinstance(profiles, dict):
            profiles = {}
        active_profiles = {session.profile_name for session in self._sessions_by_id.values()}
        visible: Dict[str, float] = {}
        for profile_name, started_at in self._starting_profiles.items():
            occupancy = profiles.get(profile_name, {})
            occupancy_state = str(occupancy.get("state", "") or "").strip().lower() if isinstance(occupancy, dict) else ""
            if profile_name in active_profiles or occupancy_state == "starting":
                visible[profile_name] = started_at
        return visible

    def _filter_starting_profiles_fail_safe(
        self,
        config: Dict,
        starting_profiles: Dict[str, float],
        *,
        active_session_count: int,
        active_profile_names: Optional[set[str]] = None,
    ) -> Dict[str, float]:
        visible = dict(starting_profiles or {})
        if not visible or active_session_count > 0:
            return visible
        active_profile_names = {str(name).strip() for name in (active_profile_names or set()) if str(name).strip()}
        registry = self._load_occupancy_registry()
        profiles = registry.get("profiles", {}) if isinstance(registry, dict) else {}
        if not isinstance(profiles, dict):
            profiles = {}
        filtered: Dict[str, float] = {}
        for name, started_at in visible.items():
            normalized_name = str(name).strip()
            if not normalized_name:
                continue
            if normalized_name in active_profile_names:
                filtered[normalized_name] = started_at
                continue
            entry = profiles.get(normalized_name, {})
            entry_state = str(entry.get("state", "") or "").strip().lower() if isinstance(entry, dict) else ""
            if entry_state != "starting":
                continue
            profile_lock_path = get_profile_runtime_lock_path(config, normalized_name)
            if profile_lock_path and os.path.exists(profile_lock_path):
                filtered[normalized_name] = started_at
        return filtered

    def _register_profile_occupancy(
        self,
        profile_name: str,
        *,
        scene_type: str,
        state: str,
        owner_label: str = "",
        engine_name: str = "",
        session_id: str = "",
        details: Optional[Dict] = None,
        owner_pid: int = 0,
        heartbeat_timeout_seconds: int = 0,
        lease_expires_at: float = 0.0,
        last_heartbeat_at: float = 0.0,
        reclaimable: bool = False,
    ) -> None:
        self._run_occupancy_write_with_retry(
            lambda: write_profile_occupancy(
                profile_name,
                scene_type=scene_type,
                state=state,
                owner_label=owner_label,
                engine_name=engine_name,
                session_id=session_id,
                details=details,
                event_source="session_manager",
                owner_pid=owner_pid,
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=last_heartbeat_at,
                reclaimable=reclaimable,
            )
        )

    def _clear_profile_occupancy(self, profile_name: str, *, session_id: str = "", event_state: str = "released") -> None:
        self._run_occupancy_write_with_retry(
            lambda: clear_profile_occupancy(
                profile_name,
                session_id=session_id,
                event_state=event_state,
                details={"cleared": True},
                event_source="session_manager",
            )
        )

    def _run_occupancy_write_with_retry(self, func, *, attempts: int = 5, sleep_seconds: float = 0.1):
        attempts = max(1, int(attempts or 1))
        last_error = None
        for _ in range(attempts):
            try:
                return func()
            except TimeoutError as exc:
                last_error = exc
                time.sleep(max(0.01, float(sleep_seconds or 0.01)))
        if last_error is not None:
            raise last_error
        return func()

    def get_profile_occupancy(self, profile_name: str) -> Dict:
        profile_name = str(profile_name or "").strip()
        registry = self._load_occupancy_registry()
        profiles = registry.get("profiles", {})
        if not isinstance(profiles, dict):
            return {}
        entry = profiles.get(profile_name, {})
        return entry if isinstance(entry, dict) else {}

    def list_profile_occupancy(self, *, tolerate_lock_timeout: bool = False) -> Dict[str, Dict]:
        self.reconcile_stale_profile_occupancy()
        return list_profile_occupancy_entries(tolerate_lock_timeout=True if not tolerate_lock_timeout else tolerate_lock_timeout)

    def list_recent_occupancy_events(self, limit: int = 100) -> List[Dict]:
        return read_recent_jsonl_events(get_occupancy_events_path(), limit=limit)

    def refresh_profile_lease(
        self,
        profile_name: str,
        *,
        scene_type: str = "",
        owner_label: str = "",
        engine_name: str = "",
        session_id: str = "",
        owner_pid: int = 0,
        heartbeat_timeout_seconds: int = 0,
        details: Optional[Dict] = None,
        reclaimable: Optional[bool] = None,
    ) -> Dict:
        existing = self.get_profile_occupancy(profile_name)
        if not existing:
            raise ValueError(f"profile occupancy not found: {profile_name}")
        current_details = dict(existing.get("details", {}) or {})
        if isinstance(details, dict):
            current_details.update(details)
        timeout_seconds = int(
            heartbeat_timeout_seconds
            or existing.get("heartbeat_timeout_seconds", 0)
            or current_details.get("heartbeat_timeout_seconds", 0)
            or 0
        )
        owner_pid_value = int(owner_pid or existing.get("owner_pid", 0) or 0)
        reclaimable_value = bool(existing.get("reclaimable", False) if reclaimable is None else reclaimable)
        return write_profile_occupancy(
            profile_name,
            scene_type=scene_type or str(existing.get("scene_type", "") or "unknown"),
            state="active",
            owner_label=owner_label or str(existing.get("owner_label", "") or ""),
            engine_name=engine_name or str(existing.get("engine_name", "") or ""),
            session_id=session_id or str(existing.get("session_id", "") or ""),
            details=current_details,
            event_source="session_manager_heartbeat",
            owner_pid=owner_pid_value,
            heartbeat_timeout_seconds=timeout_seconds,
            last_heartbeat_at=time.time(),
            reclaimable=reclaimable_value,
        )

    def reclaim_profile(self, profile_name: str, reason: str = "manual_reclaim") -> Dict:
        config = normalize_config(self._load_config())
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")
        occupancy = self.get_profile_occupancy(profile_name)
        removed_sessions: List[SessionRecord] = []
        with self._lock:
            self._starting_profiles.pop(profile_name, None)
            for session in self._sessions_for_profile_locked(profile_name):
                removed = self._remove_session_locked(session.session_id)
                if removed is not None:
                    removed_sessions.append(removed)
        for session in removed_sessions:
            self._finalize_removed_session(session, close_session=True)
        profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
        external_processes = get_chromium_processes_for_profile(config, profile_name)
        terminated_count = 0
        if external_processes:
            terminated_count = terminate_count = 0
            try:
                from chromium_advanced.chromium_profile_lib import terminate_chromium_processes
                terminate_count = terminate_chromium_processes(external_processes, logger=None)
            except Exception:
                terminate_count = 0
            terminated_count = int(terminate_count or 0)
        if profile_lock_path:
            clear_stale_lockfile(profile_lock_path, stale_seconds=1)
            if os.path.exists(profile_lock_path):
                try:
                    os.remove(profile_lock_path)
                except OSError:
                    pass
        cleared = {}
        if occupancy:
            cleared = clear_profile_occupancy(
                profile_name,
                session_id=str(occupancy.get("session_id", "") or ""),
                event_state="reclaimed",
                details={"reason": reason, "terminated_process_count": terminated_count},
                event_source="session_manager_reclaim",
            )
        return {
            "profile_name": profile_name,
            "reason": reason,
            "terminated_process_count": terminated_count,
            "lock_path": profile_lock_path,
            "cleared": bool(cleared),
            "occupancy_before": occupancy,
            "closed_session_count": len(removed_sessions),
            "closed_session_ids": [session.session_id for session in removed_sessions],
        }

    def reconcile_stale_profile_occupancy(self) -> List[Dict]:
        config = normalize_config(self._load_config())
        results: List[Dict] = []
        registry_entries = list_profile_occupancy_entries(tolerate_lock_timeout=True)
        profile_names = self._get_profile_names()
        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            live_session_ids = set(self._sessions_by_id.keys())
            live_profiles = {session.profile_name for session in self._sessions_by_id.values()}
        for profile_name, entry in registry_entries.items():
            if not isinstance(entry, dict):
                continue
            scene_type = str(entry.get("scene_type", "") or "").strip().lower()
            owner_pid = int(entry.get("owner_pid", 0) or 0)
            session_id = str(entry.get("session_id", "") or "").strip()
            if session_id and session_id in live_session_ids:
                continue
            if profile_name in live_profiles:
                continue
            profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
            lock_payload = {}
            if profile_lock_path:
                clear_stale_lockfile(profile_lock_path, stale_seconds=1)
                if os.path.exists(profile_lock_path):
                    try:
                        from chromium_advanced.chromium_profile_lib import read_lockfile_payload

                        lock_payload = read_lockfile_payload(profile_lock_path)
                    except Exception:
                        lock_payload = {}
            lock_pid = int(lock_payload.get("pid", 0) or 0)
            owner_pid_dead = owner_pid > 0 and not is_process_alive(owner_pid)
            lock_pid_dead = lock_pid > 0 and not is_process_alive(lock_pid)
            lock_missing = not bool(profile_lock_path and os.path.exists(profile_lock_path))
            if scene_type == "mcp":
                if not (owner_pid_dead or lock_pid_dead):
                    continue
                results.append(self.reclaim_profile(profile_name, reason="stale_mcp_occupancy"))
                continue
            if scene_type == "automation":
                if not (owner_pid_dead and (lock_missing or lock_pid <= 0 or lock_pid_dead)):
                    continue
                results.append(self.reclaim_profile(profile_name, reason="stale_profile_occupancy"))
                continue
            if owner_pid_dead or lock_pid_dead:
                results.append(self.reclaim_profile(profile_name, reason="stale_profile_occupancy"))
        for profile_name in profile_names:
            if profile_name in registry_entries:
                continue
            profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
            if not profile_lock_path or not os.path.exists(profile_lock_path):
                continue
            if clear_stale_lockfile(profile_lock_path, stale_seconds=1):
                results.append(
                    {
                        "profile_name": profile_name,
                        "reason": "stale_profile_lock",
                        "terminated_process_count": 0,
                        "lock_path": profile_lock_path,
                        "cleared": True,
                        "occupancy_before": {},
                    }
                )
        return results

    def reap_expired_profile_occupancy(self) -> List[Dict]:
        config = normalize_config(self._load_config())
        now_ts = time.time()
        results: List[Dict] = []
        for profile_name, entry in self.list_profile_occupancy().items():
            if not isinstance(entry, dict):
                continue
            if not occupancy_entry_is_expired(entry, now_ts=now_ts):
                continue
            scene_type = str(entry.get("scene_type", "") or "")
            if scene_type == "mcp":
                continue
            profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
            if profile_lock_path:
                clear_stale_lockfile(profile_lock_path, stale_seconds=1)
            results.append(self.reclaim_profile(profile_name, reason="lease_expired"))
        return results

    def _purge_dead_sessions_locked(self, *, probe_browser: bool = True) -> None:
        dead_session_ids = [
            sid
            for sid, session in self._sessions_by_id.items()
            if not self._is_session_alive(session, probe_browser=probe_browser)
        ]
        removed_sessions: List[SessionRecord] = []
        for session_id in dead_session_ids:
            session = self._remove_session_locked(session_id)
            if session is not None:
                removed_sessions.append(session)
        for session in removed_sessions:
            self._finalize_removed_session(session, close_session=True)

    def _mirror_manager(self, config: Dict) -> MirrorManager:
        return MirrorManager(config)

    def _sessions_for_profile_locked(self, profile_name: str) -> List[SessionRecord]:
        session_ids = list(self._session_ids_by_profile.get(profile_name, []))
        results: List[SessionRecord] = []
        for session_id in session_ids:
            session = self._sessions_by_id.get(session_id)
            if session:
                results.append(session)
        results.sort(key=lambda item: item.last_used_at, reverse=True)
        return results

    def _reuse_candidate_locked(self, profile_name: str, engine_name: str, *, probe_browser: bool = True) -> Optional[SessionRecord]:
        for session in self._sessions_for_profile_locked(profile_name):
            if session.engine_name == engine_name and self._is_session_alive(session, probe_browser=probe_browser):
                return session
        return None

    def _active_sessions_locked(self) -> List[SessionRecord]:
        return list(self._sessions_by_id.values())

    def _live_sessions_locked(self) -> List[SessionRecord]:
        return [session for session in self._sessions_by_id.values() if session.runtime_mode == "live_root"]

    def _isolated_sessions_locked(self) -> List[SessionRecord]:
        return []

    def _get_external_busy_details(self, config: Optional[Dict] = None) -> Dict:
        config = normalize_config(config or self._load_config())
        now_ts = time.time()
        with self._lock:
            if self._external_busy_cache and (now_ts - self._external_busy_cache_at) < self.EXTERNAL_BUSY_CACHE_TTL_SECONDS:
                return dict(self._external_busy_cache)
        scan_started = time.perf_counter()
        running_processes = find_running_chromium_processes(config)
        keepalive_lock_active = os.path.exists(get_lock_path())
        mirror_lock_active = os.path.exists(get_mirror_lock_path())
        details = {
            "running_processes": running_processes,
            "keepalive_lock_active": keepalive_lock_active,
            "mirror_lock_active": mirror_lock_active,
            "external_scan_ms": int((time.perf_counter() - scan_started) * 1000),
        }
        with self._lock:
            self._external_busy_cache = dict(details)
            self._external_busy_cache_at = now_ts
        return details

    @staticmethod
    def _group_running_processes_by_profile(running_processes: Optional[List[Dict]]) -> Dict[str, List[Dict]]:
        grouped: Dict[str, List[Dict]] = {}
        for item in running_processes or []:
            if not isinstance(item, dict):
                continue
            profile_name = str(item.get("profile_name", "") or "").strip()
            if not profile_name:
                continue
            grouped.setdefault(profile_name, []).append(item)
        return grouped

    @staticmethod
    def _compact_running_process_payload(running_processes: Optional[List[Dict]], limit: int = 8) -> Dict:
        items = list(running_processes or [])
        sample_limit = max(0, int(limit or 0))
        sample = items[:sample_limit]
        return {
            "external_running_process_count": len(items),
            "external_running_processes": sample,
            "external_running_processes_truncated": len(items) > len(sample),
        }

    def _get_profile_names(self) -> List[str]:
        config = normalize_config(self._load_config())
        return [item.get("profile_name", "") for item in config.get("profiles", []) if item.get("profile_name")]

    def _build_mirror_status(self, config: Dict) -> Dict:
        mirror_settings = config.get("mirror", {})
        manager = self._mirror_manager(config)
        manifest = manager.load_manifest()
        profile_entries = manifest.get("profiles", {}) if isinstance(manifest, dict) else {}
        available_profiles = [
            name
            for name, entry in profile_entries.items()
            if isinstance(entry, dict) and entry.get("status") == "success"
        ]
        return {
            "enabled": bool(mirror_settings.get("enabled", False)),
            "last_run_at": str(mirror_settings.get("last_run_at", "") or ""),
            "last_run_finished_at": str(mirror_settings.get("last_run_finished_at", "") or ""),
            "last_run_status": str(mirror_settings.get("last_run_status", "never") or "never"),
            "last_run_message": str(mirror_settings.get("last_run_message", "") or ""),
            "last_run_profile_count": int(mirror_settings.get("last_run_profile_count", 0) or 0),
            "disk_root": manager.disk_root(),
            "runtime_root": manager.runtime_root(),
            "manifest_generated_at": str(manifest.get("generated_at", "") or ""),
            "available_profile_count": len(available_profiles),
            "available_profiles": available_profiles,
        }

    def list_profiles(self) -> List[Dict]:
        config = normalize_config(self._load_config())
        manager = self._mirror_manager(config)
        occupancy_map = self.list_profile_occupancy()
        external_busy = self._get_external_busy_details(config)
        running_by_profile = self._group_running_processes_by_profile(external_busy.get("running_processes", []))
        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            profile_sessions = {
                item.get("profile_name", ""): self._sessions_for_profile_locked(item.get("profile_name", ""))
                for item in config.get("profiles", [])
                if item.get("profile_name")
            }
        results: List[Dict] = []
        for item in config.get("profiles", []):
            profile_name = item.get("profile_name", "")
            if not profile_name:
                continue
            sessions = profile_sessions.get(profile_name, [])
            active_summary = sessions[0].to_summary(refresh=False) if sessions else {}
            live_session_count = sum(1 for session in sessions if session.runtime_mode == "live_root")
            isolated_session_count = 0
            mirror_validation = manager.validate_profile_snapshot(profile_name)
            occupancy = occupancy_map.get(profile_name, {})
            profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
            profile_lock_active = bool(profile_lock_path and os.path.exists(profile_lock_path))
            profile_processes = running_by_profile.get(profile_name, [])
            if sessions:
                busy_state = "active_sessions"
            elif occupancy:
                busy_state = str(occupancy.get("scene_type", "") or occupancy.get("state", "") or "occupied")
            elif profile_lock_active:
                busy_state = "profile_lock_active"
            elif profile_processes:
                busy_state = "external_chromium_running"
            else:
                busy_state = "idle"
            results.append(
                {
                    "profile_name": profile_name,
                    "account": item.get("account", ""),
                    "notes": item.get("notes", ""),
                    "keepalive_enabled": bool(item.get("keepalive_enabled", False)),
                    "last_launch_at": item.get("last_launch_at", ""),
                    "last_keepalive_at": item.get("last_keepalive_at", ""),
                    "last_keepalive_status": item.get("last_keepalive_status", ""),
                    "last_mirror_at": item.get("last_mirror_at", ""),
                    "last_mirror_status": item.get("last_mirror_status", ""),
                    "last_mirror_message": item.get("last_mirror_message", ""),
                    "active_session": bool(sessions),
                    "active_session_count": len(sessions),
                    "live_session_count": live_session_count,
                    "isolated_session_count": isolated_session_count,
                    "session_id": active_summary.get("session_id", ""),
                    "current_url": active_summary.get("current_url", ""),
                    "title": active_summary.get("title", ""),
                    "created_at": active_summary.get("created_at", 0),
                    "last_used_at": active_summary.get("last_used_at", 0),
                    "busy_owner_profile_name": "",
                    "busy_state": busy_state,
                    "occupancy": occupancy,
                    "occupancy_state": str(occupancy.get("state", "") or ""),
                    "occupancy_scene_type": str(occupancy.get("scene_type", "") or ""),
                    "occupancy_owner_label": str(occupancy.get("owner_label", "") or ""),
                    "mirror_available": bool(mirror_validation.get("available")),
                    "mirror_generated_at": mirror_validation.get("generated_at", ""),
                    "mirror_root_available": bool(mirror_validation.get("root_available")),
                    "mirror_profile_available": bool(mirror_validation.get("profile_available")),
                }
            )
        return results

    def get_profile_status(self, profile_name: str) -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")

        for item in self.list_profiles():
            if item.get("profile_name") == profile_name:
                return item
        raise ValueError(f"profile not found: {profile_name}")

    def list_sessions(self) -> List[Dict]:
        self.reconcile_stale_profile_occupancy()
        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            return [session.to_summary(refresh=False) for session in self._sessions_by_id.values()]

    def get_runtime_status_snapshot(self) -> Dict:
        config = normalize_config(self._load_config())
        concurrency_mode = str(config.get("app", {}).get("concurrency_mode", "per_profile_live") or "per_profile_live")
        default_engine_name = resolve_browser_engine_name(config)
        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            visible_starting_profiles = self._visible_starting_profiles_locked()
            active_sessions = [
                {
                    "session_id": session.session_id,
                    "profile_name": session.profile_name,
                    "engine_name": session.engine_name,
                    "created_at": session.created_at,
                    "last_used_at": session.last_used_at,
                    "runtime_mode": session.runtime_mode,
                    "runtime_root": session.runtime_root,
                    "mirror_generated_at": session.mirror_generated_at,
                }
                for session in self._sessions_by_id.values()
            ]
            live_session_count = sum(1 for session in self._sessions_by_id.values() if session.runtime_mode == "live_root")
            active_profile_names = {str(session.profile_name).strip() for session in self._sessions_by_id.values() if str(session.profile_name).strip()}
        external_busy = self._get_external_busy_details(config)
        running_processes = external_busy.get("running_processes", [])
        running_process_payload = self._compact_running_process_payload(running_processes)
        keepalive_lock_active = bool(external_busy.get("keepalive_lock_active"))
        mirror_lock_active = bool(external_busy.get("mirror_lock_active"))
        external_scan_ms = int(external_busy.get("external_scan_ms", 0) or 0)
        mirror_status = self._build_mirror_status(config)
        state = "idle"
        busy = False
        message = "runtime snapshot"
        starting_profiles = self._filter_starting_profiles_fail_safe(
            config,
            dict(visible_starting_profiles),
            active_session_count=len(active_sessions),
            active_profile_names=active_profile_names,
        )
        if starting_profiles:
            first_profile_name = next(iter(starting_profiles.keys()))
            state = "starting"
            busy = True
            if len(starting_profiles) == 1:
                message = f"profile is starting: {first_profile_name}"
            else:
                message = f"{len(starting_profiles)} profiles are starting"
        elif active_sessions:
            state = "active_sessions"
            message = f"{len(active_sessions)} active per-profile session(s)"
        elif mirror_lock_active:
            state = "mirroring"
            message = "mirror snapshot job is running in background"
        elif keepalive_lock_active:
            state = "keepalive_running"
            message = "keepalive scheduler is running; per-profile locks still apply"
        elif running_processes:
            state = "external_chromium_running"
            message = f"chromium is running on {len(running_processes)} process(es); profile-level gating applies"
        return {
            "default_engine_name": default_engine_name,
            "concurrency_mode": concurrency_mode,
            "mirror_status": mirror_status,
            "active_session_count": len(active_sessions),
            "active_sessions": active_sessions,
            "live_session_count": live_session_count,
            "isolated_session_count": 0,
            **running_process_payload,
            "external_scan_ms": external_scan_ms,
            "keepalive_lock_active": keepalive_lock_active,
            "mirror_lock_active": mirror_lock_active,
            "owner_profile_name": next(iter(starting_profiles.keys())) if starting_profiles else "",
            "owner_session_id": "",
            "started_at": min(starting_profiles.values()) if starting_profiles else 0,
            "state": state,
            "busy": busy,
            "accepting_new_sessions": True,
            "message": message,
            "starting_profiles": [
                {"profile_name": name, "started_at": started_at}
                for name, started_at in sorted(starting_profiles.items(), key=lambda item: item[1])
            ],
        }

    def get_server_status(self) -> Dict:
        config = normalize_config(self._load_config())
        self.reconcile_stale_profile_occupancy()
        self.reap_expired_profile_occupancy()
        concurrency_mode = str(config.get("app", {}).get("concurrency_mode", "per_profile_live") or "per_profile_live")
        default_engine_name = resolve_browser_engine_name(config)
        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            visible_starting_profiles = self._visible_starting_profiles_locked()
            active_sessions = [session.to_summary(refresh=False) for session in self._sessions_by_id.values()]
            live_sessions = [session.to_summary(refresh=False) for session in self._live_sessions_locked()]
            isolated_sessions = [session.to_summary(refresh=False) for session in self._isolated_sessions_locked()]
            active_profile_names = {str(session.profile_name).strip() for session in self._sessions_by_id.values() if str(session.profile_name).strip()}

        external_busy = self._get_external_busy_details(config)
        running_processes = external_busy.get("running_processes", [])
        running_process_payload = self._compact_running_process_payload(running_processes)
        keepalive_lock_active = bool(external_busy.get("keepalive_lock_active"))
        mirror_lock_active = bool(external_busy.get("mirror_lock_active"))
        external_scan_ms = int(external_busy.get("external_scan_ms", 0) or 0)
        mirror_status = self._build_mirror_status(config)

        base = {
            "default_engine_name": default_engine_name,
            "concurrency_mode": concurrency_mode,
            "mirror_status": mirror_status,
            "active_session_count": len(active_sessions),
            "active_sessions": active_sessions,
            "live_session_count": len(live_sessions),
            "isolated_session_count": len(isolated_sessions),
            **running_process_payload,
            "external_scan_ms": external_scan_ms,
            "keepalive_lock_active": keepalive_lock_active,
            "mirror_lock_active": mirror_lock_active,
            "owner_profile_name": "",
            "owner_session_id": "",
            "started_at": 0,
        }

        starting_profiles = self._filter_starting_profiles_fail_safe(
            config,
            dict(visible_starting_profiles),
            active_session_count=len(active_sessions),
            active_profile_names=active_profile_names,
        )
        if starting_profiles:
            first_profile_name = next(iter(starting_profiles.keys()))
            return {
                **base,
                "state": "starting",
                "busy": True,
                "accepting_new_sessions": True,
                "owner_profile_name": first_profile_name,
                "started_at": min(starting_profiles.values()),
                "message": f"{len(starting_profiles)} profile(s) are starting" if len(starting_profiles) > 1 else f"profile is starting: {first_profile_name}",
                "starting_profiles": [
                    {"profile_name": name, "started_at": started_at}
                    for name, started_at in sorted(starting_profiles.items(), key=lambda item: item[1])
                ],
            }

        if mirror_lock_active:
            return {
                **base,
                "state": "mirroring",
                "busy": False,
                "accepting_new_sessions": True,
                "message": "mirror snapshot job is running in background",
            }

        if keepalive_lock_active:
            return {
                **base,
                "state": "keepalive_running",
                "busy": False,
                "accepting_new_sessions": True,
                "message": "keepalive scheduler is running; per-profile locks still apply",
            }

        if live_sessions:
            return {
                **base,
                "state": "active_sessions",
                "busy": False,
                "accepting_new_sessions": True,
                "message": f"{len(live_sessions)} active per-profile session(s)",
            }

        if isolated_sessions:
            return {
                **base,
                "state": "isolated_runtime_active",
                "busy": False,
                "accepting_new_sessions": True,
                "message": f"{len(isolated_sessions)} isolated runtime session(s) active",
            }

        if running_processes:
            return {
                **base,
                "state": "external_chromium_running",
                "busy": False,
                "accepting_new_sessions": True,
                "message": f"chromium is running on {len(running_processes)} process(es); profile-level gating applies",
            }

        return {
            **base,
            "state": "idle",
            "busy": False,
            "accepting_new_sessions": True,
            "message": "server is idle",
        }

    def can_start_session(self, profile_name: str, engine_name: str = "") -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")

        config = normalize_config(self._load_config())
        self.reconcile_stale_profile_occupancy()
        self.reap_expired_profile_occupancy()
        resolved_engine_name = resolve_browser_engine_name(config, engine_name)
        if profile_name not in self._get_profile_names():
            raise ValueError(f"profile not found: {profile_name}")

        status = self.get_server_status()
        external_busy = self._get_external_busy_details(config)
        running_by_profile = self._group_running_processes_by_profile(external_busy.get("running_processes", []))
        profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
        profile_lock_active = bool(profile_lock_path and os.path.exists(profile_lock_path))
        profile_processes = running_by_profile.get(profile_name, [])

        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            profile_sessions = self._sessions_for_profile_locked(profile_name)
            reusable_session = self._reuse_candidate_locked(profile_name, resolved_engine_name, probe_browser=False)
        same_profile_parallel_supported = False
        start_mode = "live_root"
        allowed = False
        reason = ""

        starting_profiles = status.get("starting_profiles", [])
        starting_profile_names = {
            str(item.get("profile_name", "") or "").strip()
            for item in starting_profiles
            if isinstance(item, dict)
        }
        if profile_name in starting_profile_names:
            reason = f"profile is starting: {profile_name}"
        elif reusable_session:
            reason = "profile already has a reusable session"
        elif profile_sessions:
            reason = "profile is already in use by another MCP session"
        elif profile_lock_active:
            reason = "profile runtime lock is already held"
        elif profile_processes:
            reason = "profile chromium is already running"
        else:
            allowed = True
            reason = "profile is available"

        return {
            "allowed": bool(allowed),
            "profile_name": profile_name,
            "engine_name": resolved_engine_name,
            "reusable": bool(reusable_session is not None),
            "reusable_session_id": reusable_session.session_id if reusable_session else "",
            "status": status,
            "start_mode": start_mode,
            "reason": reason,
            "same_profile_parallel_supported": same_profile_parallel_supported,
            "active_profile_session_count": len(profile_sessions),
            "profile_lock_active": profile_lock_active,
            "external_profile_process_count": len(profile_processes),
            "mirror_available": False,
            "mirror_generated_at": "",
        }

    def start_session(
        self,
        profile_name: str,
        reuse_existing: bool = False,
        engine_name: str = "",
        scene_type: str = "mcp",
        owner_label: str = "",
        runtime_options: Optional[Dict] = None,
    ) -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")

        config = normalize_config(self._load_config())
        self.reconcile_stale_profile_occupancy()
        resolved_engine_name = resolve_browser_engine_name(config, engine_name)
        preflight = self.can_start_session(profile_name, engine_name=resolved_engine_name)

        with self._lock:
            self._purge_dead_sessions_locked(probe_browser=False)
            reusable_session = self._reuse_candidate_locked(profile_name, resolved_engine_name, probe_browser=False)
            if reuse_existing and reusable_session:
                reusable_session.last_used_at = time.time()
                return {
                    "session_id": reusable_session.session_id,
                    "profile_name": reusable_session.profile_name,
                    "engine_name": reusable_session.engine_name,
                    "runtime_mode": reusable_session.runtime_mode,
                    "runtime_root": reusable_session.runtime_root,
                    "mirror_generated_at": reusable_session.mirror_generated_at,
                    "reused": True,
                }
            if not preflight.get("allowed"):
                raise RuntimeError(str(preflight.get("reason", "") or "browser session start blocked"))
            self._starting_profiles[profile_name] = time.time()
            effective_scene_type = str(scene_type or "mcp").strip() or "mcp"
            effective_owner_label = str(owner_label or "").strip()
            self._register_profile_occupancy(
                profile_name,
                scene_type=effective_scene_type,
                state="starting",
                owner_label=effective_owner_label or f"{effective_scene_type} session starting",
                engine_name=resolved_engine_name,
                session_id="",
                details={"source": "SessionManager.start_session"},
                owner_pid=os.getpid(),
                reclaimable=False,
            )

        runtime_root = ""
        mirror_generated_at = ""
        runtime_mode = str(preflight.get("start_mode", "live_root") or "live_root")
        cleanup_runtime_on_close = False
        profile_lock = SingleRunLock(get_profile_runtime_lock_path(config, profile_name))
        session: Optional[SessionRecord] = None
        try:
            if not profile_lock.try_acquire():
                raise RuntimeError(f"profile runtime lock is already held: {profile_name}")
            print(
                f"[{now_text()}] [SESSION] start_session begin: profile={profile_name} engine={resolved_engine_name} mode={runtime_mode}",
                flush=True,
            )
            config_for_launch = copy.deepcopy(config)
            if isinstance(runtime_options, dict) and runtime_options:
                config_for_launch = build_runtime_config_overrides(
                    config_for_launch,
                    headless=runtime_options.get("headless"),
                    start_minimized=runtime_options.get("start_minimized"),
                    mute_audio=runtime_options.get("mute_audio"),
                    incognito=runtime_options.get("incognito"),
                    window_size=str(runtime_options.get("window_size", "") or "").strip(),
                    extra_args=runtime_options.get("extra_args") if isinstance(runtime_options.get("extra_args"), list) else [],
                    engine_name=resolved_engine_name,
                )
            ensure_profile_bookmarks_initialized(config_for_launch, profile_name)
            print(
                f"[{now_text()}] [SESSION] bookmarks ready: profile={profile_name}",
                flush=True,
            )

            engine = create_browser_engine(resolved_engine_name)
            print(
                f"[{now_text()}] [SESSION] engine created: profile={profile_name} engine={resolved_engine_name}",
                flush=True,
            )
            browser_session = ManagedBrowserSession(engine.create_session(config_for_launch, profile_name))
            print(
                f"[{now_text()}] [SESSION] browser session created: profile={profile_name} engine={resolved_engine_name} mode={runtime_mode}",
                flush=True,
            )
            session_id = f"session-{uuid.uuid4().hex[:12]}"
            now = time.time()
            session = SessionRecord(
                session_id=session_id,
                profile_name=profile_name,
                engine_name=resolved_engine_name,
                created_at=now,
                last_used_at=now,
                browser_session=browser_session,
                runtime_mode=runtime_mode,
                runtime_root=runtime_root,
                mirror_generated_at=mirror_generated_at,
                cleanup_runtime_on_close=cleanup_runtime_on_close,
                profile_lock=profile_lock,
                launch_pid=int(getattr(browser_session, "pid", 0) or 0),
            )
            with self._lock:
                self._sessions_by_id[session_id] = session
                self._session_ids_by_profile.setdefault(profile_name, []).append(session_id)
            self._register_profile_occupancy(
                profile_name,
                scene_type=effective_scene_type,
                state="active",
                owner_label=effective_owner_label or f"{effective_scene_type} {session_id}",
                engine_name=resolved_engine_name,
                session_id=session_id,
                details={"runtime_mode": runtime_mode},
                owner_pid=os.getpid(),
                reclaimable=False,
                heartbeat_timeout_seconds=int((runtime_options or {}).get("heartbeat_timeout_seconds", 0) or 0),
            )
            return {
                "session_id": session_id,
                "profile_name": profile_name,
                "engine_name": resolved_engine_name,
                "runtime_mode": runtime_mode,
                "runtime_root": runtime_root,
                "mirror_generated_at": mirror_generated_at,
                "reused": False,
            }
        except Exception:
            removed_session = None
            if session is not None:
                with self._lock:
                    removed_session = self._remove_session_locked(session.session_id)
            if removed_session is not None:
                self._finalize_removed_session(removed_session, close_session=True)
            else:
                profile_lock.release()
                self._clear_profile_occupancy(profile_name, event_state="start_failed")
            raise
        finally:
            with self._lock:
                self._starting_profiles.pop(profile_name, None)

    def get_session(
        self,
        session_id: str,
        scene_type: str = "mcp",
        owner_label: str = "",
        refresh_lease: bool = True,
    ) -> SessionRecord:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        stale_session: Optional[SessionRecord] = None
        with self._lock:
            session = self._sessions_by_id.get(session_id)
            if not session:
                raise ValueError(f"session not found: {session_id}")
            if not self._is_session_alive(session):
                stale_session = self._remove_session_locked(session_id)
                session = None
            else:
                session.last_used_at = time.time()
        if stale_session is not None:
            self._finalize_removed_session(stale_session, close_session=True)
            raise RuntimeError(f"session is no longer alive: {session_id}")
        if session is None:
            raise RuntimeError(f"session is no longer alive: {session_id}")
        session.refresh_cached_summary()
        if session.profile_lock is not None:
            try:
                session.profile_lock.touch()
            except Exception:
                pass
        if refresh_lease:
            try:
                effective_scene_type = str(scene_type or "mcp").strip() or "mcp"
                effective_owner_label = str(owner_label or "").strip() or f"{effective_scene_type} {session.session_id}"
                self.refresh_profile_lease(
                    session.profile_name,
                    scene_type=effective_scene_type,
                    owner_label=effective_owner_label,
                    engine_name=session.engine_name,
                    session_id=session.session_id,
                    owner_pid=os.getpid(),
                    details={"runtime_mode": session.runtime_mode, "last_used_at": session.last_used_at},
                    reclaimable=(effective_scene_type != "mcp"),
                )
            except Exception:
                pass
        return session

    def close_session(self, session_id: str) -> Dict:
        removed_session: Optional[SessionRecord] = None
        with self._lock:
            session = self._sessions_by_id.get(str(session_id or "").strip())
            if not session:
                return {
                    "session_id": str(session_id or "").strip(),
                    "closed": False,
                    "message": "session not found",
                    "active_session_ids_before": list(self._sessions_by_id.keys()),
                    "active_session_ids_after": list(self._sessions_by_id.keys()),
                }
            before_ids = list(self._sessions_by_id.keys())
            removed_session = self._remove_session_locked(session.session_id)
            self._starting_profiles.pop(session.profile_name, None)
            after_ids = list(self._sessions_by_id.keys())
        if removed_session is None:
            return {
                "session_id": str(session_id or "").strip(),
                "closed": False,
                "message": "session not found",
                "active_session_ids_before": before_ids,
                "active_session_ids_after": after_ids,
            }
        self._finalize_removed_session(removed_session, close_session=True)
        return {
            "session_id": removed_session.session_id,
            "profile_name": removed_session.profile_name,
            "engine_name": removed_session.engine_name,
            "runtime_mode": removed_session.runtime_mode,
            "closed": True,
            "active_session_ids_before": before_ids,
            "active_session_ids_after": after_ids,
        }

    def close_all(self) -> Dict:
        with self._lock:
            session_ids = list(self._sessions_by_id.keys())
        results = [self.close_session(session_id) for session_id in session_ids]
        return {"closed_count": sum(1 for item in results if item.get("closed")), "results": results}

    def resolve_session(
        self,
        session_id: str,
        scene_type: str = "mcp",
        owner_label: str = "",
        refresh_lease: bool = True,
    ):
        return self.get_session(
            session_id,
            scene_type=scene_type,
            owner_label=owner_label,
            refresh_lease=refresh_lease,
        ).browser_session

    def _remove_session_locked(self, session_id: str) -> Optional[SessionRecord]:
        session = self._sessions_by_id.pop(session_id, None)
        if not session:
            return None
        existing_ids = [sid for sid in self._session_ids_by_profile.get(session.profile_name, []) if sid != session_id]
        if existing_ids:
            self._session_ids_by_profile[session.profile_name] = existing_ids
        else:
            self._session_ids_by_profile.pop(session.profile_name, None)
        return session

    def _finalize_removed_session(self, session: SessionRecord, *, close_session: bool) -> None:
        with self._lock:
            self._starting_profiles.pop(session.profile_name, None)
        if close_session:
            try:
                session.browser_session.close()
            except Exception:
                pass
        try:
            config = normalize_config(self._load_config())
            before_pids = [int(session.launch_pid)] if int(session.launch_pid or 0) > 0 else []
            cleanup_keepalive_profile_processes(config, session.profile_name, before_pids=before_pids, logger=None)
        except Exception:
            pass
        self._clear_profile_occupancy(session.profile_name, session_id=session.session_id)
        if session.profile_lock is not None:
            try:
                session.profile_lock.release()
            except Exception:
                pass

    def _terminate_runtime_processes(self, runtime_root: str) -> None:
        runtime_root = str(runtime_root or "").strip()
        if not runtime_root:
            return
        try:
            runtime_norm = normalize_fs_path(runtime_root)
        except Exception:
            return

        current_pid = os.getpid()
        targets: Dict[int, psutil.Process] = {}
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.pid == current_pid:
                    continue
                cmdline = proc.info.get("cmdline") or []
                command_line = " ".join(str(item) for item in cmdline)
                if not command_line:
                    continue
                if runtime_norm not in os.path.normcase(command_line):
                    continue
                targets[proc.pid] = proc
                for child in proc.children(recursive=True):
                    targets[child.pid] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue

        if not targets:
            return
        processes = list(targets.values())
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(processes, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=3)

    def _is_session_alive(self, session_or_browser_session, *, probe_browser: bool = True) -> bool:
        browser_session = session_or_browser_session
        session: Optional[SessionRecord] = None
        if isinstance(session_or_browser_session, SessionRecord):
            session = session_or_browser_session
            browser_session = session.browser_session
        if not probe_browser:
            if session is None:
                return True
            launch_pid = int(getattr(session, "launch_pid", 0) or 0)
            if launch_pid > 0 and not is_process_alive(launch_pid):
                return False
            return bool(getattr(session, "cached_alive", True))
        now_ts = time.time()
        try:
            alive = bool(browser_session.get_summary().alive)
            if session is not None:
                session.last_alive_probe_at = now_ts
                if alive:
                    session.alive_probe_failures = 0
                session.cached_alive = alive
            return alive
        except Exception:
            if session is None:
                return False
            session.last_alive_probe_at = now_ts
            session.alive_probe_failures = int(session.alive_probe_failures or 0) + 1
            session.cached_alive = False
            if (now_ts - float(session.created_at or 0.0)) < self.SESSION_ALIVE_PROBE_GRACE_SECONDS:
                return True
            if session.alive_probe_failures < self.SESSION_ALIVE_MAX_CONSECUTIVE_FAILURES:
                return True
            return False
