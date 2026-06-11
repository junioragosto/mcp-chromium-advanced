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
    ensure_profile_bookmarks_initialized,
    find_running_chromium_processes,
    get_chromium_processes_for_profile,
    get_lock_path,
    get_mirror_lock_path,
    get_profile_runtime_lock_path,
    load_app_config,
    normalize_config,
    normalize_fs_path,
    now_text,
    SingleRunLock,
)
from chromium_advanced.mirror_manager import MirrorManager


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

    def to_summary(self) -> Dict:
        summary = self.browser_session.get_summary()
        return {
            "session_id": self.session_id,
            "profile_name": self.profile_name,
            "engine_name": self.engine_name,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "current_url": summary.current_url,
            "title": summary.title,
            "alive": summary.alive,
            "runtime_mode": self.runtime_mode,
            "runtime_root": self.runtime_root,
            "mirror_generated_at": self.mirror_generated_at,
        }


class SessionManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self._lock = threading.RLock()
        self._sessions_by_id: Dict[str, SessionRecord] = {}
        self._session_ids_by_profile: Dict[str, List[str]] = {}
        self._starting_profile_name = ""
        self._starting_started_at = 0.0

    def _load_config(self) -> Dict:
        return load_app_config(self.config_path)

    def _purge_dead_sessions_locked(self) -> None:
        dead_session_ids = [
            sid
            for sid, session in self._sessions_by_id.items()
            if not self._is_session_alive(session.browser_session)
        ]
        for session_id in dead_session_ids:
            self._remove_session_locked(session_id, close_session=True)

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

    def _reuse_candidate_locked(self, profile_name: str, engine_name: str) -> Optional[SessionRecord]:
        for session in self._sessions_for_profile_locked(profile_name):
            if session.engine_name == engine_name and self._is_session_alive(session.browser_session):
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
        running_processes = find_running_chromium_processes(config)
        keepalive_lock_active = os.path.exists(get_lock_path())
        mirror_lock_active = os.path.exists(get_mirror_lock_path())
        return {
            "running_processes": running_processes,
            "keepalive_lock_active": keepalive_lock_active,
            "mirror_lock_active": mirror_lock_active,
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
        busy_status = self.get_server_status()
        manager = self._mirror_manager(config)
        with self._lock:
            self._purge_dead_sessions_locked()
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
            active_summary = sessions[0].to_summary() if sessions else {}
            live_session_count = sum(1 for session in sessions if session.runtime_mode == "live_root")
            isolated_session_count = 0
            mirror_validation = manager.validate_profile_snapshot(profile_name)
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
                    "busy_owner_profile_name": busy_status.get("owner_profile_name", ""),
                    "busy_state": busy_status.get("state", "idle"),
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
        with self._lock:
            self._purge_dead_sessions_locked()
            return [session.to_summary() for session in self._sessions_by_id.values()]

    def get_server_status(self) -> Dict:
        config = normalize_config(self._load_config())
        concurrency_mode = str(config.get("app", {}).get("concurrency_mode", "per_profile_live") or "per_profile_live")
        default_engine_name = resolve_browser_engine_name(config)
        with self._lock:
            self._purge_dead_sessions_locked()
            active_sessions = [session.to_summary() for session in self._sessions_by_id.values()]
            live_sessions = [session.to_summary() for session in self._live_sessions_locked()]
            isolated_sessions = [session.to_summary() for session in self._isolated_sessions_locked()]

        external_busy = self._get_external_busy_details(config)
        running_processes = external_busy.get("running_processes", [])
        keepalive_lock_active = bool(external_busy.get("keepalive_lock_active"))
        mirror_lock_active = bool(external_busy.get("mirror_lock_active"))
        mirror_status = self._build_mirror_status(config)

        base = {
            "default_engine_name": default_engine_name,
            "concurrency_mode": concurrency_mode,
            "mirror_status": mirror_status,
            "active_session_count": len(active_sessions),
            "active_sessions": active_sessions,
            "live_session_count": len(live_sessions),
            "isolated_session_count": len(isolated_sessions),
            "external_running_process_count": len(running_processes),
            "external_running_processes": running_processes,
            "keepalive_lock_active": keepalive_lock_active,
            "mirror_lock_active": mirror_lock_active,
            "owner_profile_name": "",
            "owner_session_id": "",
            "started_at": 0,
        }

        if self._starting_profile_name:
            return {
                **base,
                "state": "starting",
                "busy": True,
                "accepting_new_sessions": False,
                "owner_profile_name": self._starting_profile_name,
                "started_at": self._starting_started_at,
                "message": f"profile is starting: {self._starting_profile_name}",
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
        resolved_engine_name = resolve_browser_engine_name(config, engine_name)
        if profile_name not in self._get_profile_names():
            raise ValueError(f"profile not found: {profile_name}")

        status = self.get_server_status()
        profile_lock_path = get_profile_runtime_lock_path(config, profile_name)
        profile_lock_active = bool(profile_lock_path and os.path.exists(profile_lock_path))
        profile_processes = get_chromium_processes_for_profile(config, profile_name)

        with self._lock:
            self._purge_dead_sessions_locked()
            profile_sessions = self._sessions_for_profile_locked(profile_name)
            reusable_session = self._reuse_candidate_locked(profile_name, resolved_engine_name)
        same_profile_parallel_supported = False
        start_mode = "live_root"
        allowed = False
        reason = ""

        if status.get("state") == "starting":
            reason = str(status.get("message", "") or "browser service is busy")
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

    def start_session(self, profile_name: str, reuse_existing: bool = False, engine_name: str = "") -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")

        config = normalize_config(self._load_config())
        resolved_engine_name = resolve_browser_engine_name(config, engine_name)
        preflight = self.can_start_session(profile_name, engine_name=resolved_engine_name)

        with self._lock:
            self._purge_dead_sessions_locked()
            reusable_session = self._reuse_candidate_locked(profile_name, resolved_engine_name)
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
            self._starting_profile_name = profile_name
            self._starting_started_at = time.time()

        runtime_root = ""
        mirror_generated_at = ""
        runtime_mode = str(preflight.get("start_mode", "live_root") or "live_root")
        cleanup_runtime_on_close = False
        profile_lock = SingleRunLock(get_profile_runtime_lock_path(config, profile_name))
        try:
            if not profile_lock.try_acquire():
                raise RuntimeError(f"profile runtime lock is already held: {profile_name}")
            print(
                f"[{now_text()}] [SESSION] start_session begin: profile={profile_name} engine={resolved_engine_name} mode={runtime_mode}",
                flush=True,
            )
            config_for_launch = copy.deepcopy(config)
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
            )
            with self._lock:
                self._sessions_by_id[session_id] = session
                self._session_ids_by_profile.setdefault(profile_name, []).append(session_id)
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
            profile_lock.release()
            raise
        finally:
            with self._lock:
                if self._starting_profile_name == profile_name:
                    self._starting_profile_name = ""
                    self._starting_started_at = 0.0

    def get_session(self, session_id: str) -> SessionRecord:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        with self._lock:
            session = self._sessions_by_id.get(session_id)
            if not session:
                raise ValueError(f"session not found: {session_id}")
            if not self._is_session_alive(session.browser_session):
                self._remove_session_locked(session_id, close_session=True)
                raise RuntimeError(f"session is no longer alive: {session_id}")
            session.last_used_at = time.time()
            return session

    def close_session(self, session_id: str) -> Dict:
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
            self._remove_session_locked(session.session_id, close_session=True)
            after_ids = list(self._sessions_by_id.keys())
            return {
                "session_id": session.session_id,
                "profile_name": session.profile_name,
                "engine_name": session.engine_name,
                "runtime_mode": session.runtime_mode,
                "closed": True,
                "active_session_ids_before": before_ids,
                "active_session_ids_after": after_ids,
            }

    def close_all(self) -> Dict:
        with self._lock:
            session_ids = list(self._sessions_by_id.keys())
        results = [self.close_session(session_id) for session_id in session_ids]
        return {"closed_count": sum(1 for item in results if item.get("closed")), "results": results}

    def resolve_session(self, session_id: str):
        return self.get_session(session_id).browser_session

    def _remove_session_locked(self, session_id: str, close_session: bool) -> None:
        session = self._sessions_by_id.pop(session_id, None)
        if not session:
            return
        existing_ids = [sid for sid in self._session_ids_by_profile.get(session.profile_name, []) if sid != session_id]
        if existing_ids:
            self._session_ids_by_profile[session.profile_name] = existing_ids
        else:
            self._session_ids_by_profile.pop(session.profile_name, None)
        if close_session:
            try:
                session.browser_session.close()
            except Exception:
                pass
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

    def _is_session_alive(self, browser_session) -> bool:
        try:
            return bool(browser_session.get_summary().alive)
        except Exception:
            return False
