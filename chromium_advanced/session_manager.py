import os
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
    get_lock_path,
    load_app_config,
    normalize_config,
    now_text,
)


@dataclass
class SessionRecord:
    session_id: str
    profile_name: str
    engine_name: str
    created_at: float
    last_used_at: float
    browser_session: object

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
        }


class SessionManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self._lock = threading.RLock()
        self._sessions_by_id: Dict[str, SessionRecord] = {}
        self._session_id_by_profile: Dict[str, str] = {}
        self._starting_profile_name = ""
        self._starting_started_at = 0.0

    def _load_config(self) -> Dict:
        return load_app_config(self.config_path)

    def _get_external_busy_details(self) -> Dict:
        config = self._load_config()
        running_processes = find_running_chromium_processes(config)
        keepalive_lock_active = os.path.exists(get_lock_path())
        return {
            "running_processes": running_processes,
            "keepalive_lock_active": keepalive_lock_active,
        }

    def _get_profile_names(self) -> List[str]:
        config = normalize_config(self._load_config())
        return [item.get("profile_name", "") for item in config.get("profiles", []) if item.get("profile_name")]

    def list_profiles(self) -> List[Dict]:
        config = normalize_config(self._load_config())
        busy_status = self.get_server_status()
        with self._lock:
            active_sessions = {
                session.profile_name: session
                for session in self._sessions_by_id.values()
                if self._is_session_alive(session.browser_session)
            }
        results: List[Dict] = []
        for item in config.get("profiles", []):
            profile_name = item.get("profile_name", "")
            if not profile_name:
                continue
            active_session = active_sessions.get(profile_name)
            active_summary = active_session.to_summary() if active_session else {}
            results.append(
                {
                    "profile_name": profile_name,
                    "account": item.get("account", ""),
                    "notes": item.get("notes", ""),
                    "keepalive_enabled": bool(item.get("keepalive_enabled", False)),
                    "last_launch_at": item.get("last_launch_at", ""),
                    "last_keepalive_at": item.get("last_keepalive_at", ""),
                    "last_keepalive_status": item.get("last_keepalive_status", ""),
                    "active_session": bool(active_session),
                    "session_id": active_summary.get("session_id", ""),
                    "current_url": active_summary.get("current_url", ""),
                    "title": active_summary.get("title", ""),
                    "created_at": active_summary.get("created_at", 0),
                    "last_used_at": active_summary.get("last_used_at", 0),
                    "busy_owner_profile_name": busy_status.get("owner_profile_name", ""),
                    "busy_state": busy_status.get("state", "idle"),
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
            dead_session_ids = [
                sid
                for sid, session in self._sessions_by_id.items()
                if not self._is_session_alive(session.browser_session)
            ]
            for session_id in dead_session_ids:
                self._remove_session_locked(session_id, close_session=False)

            return [session.to_summary() for session in self._sessions_by_id.values()]

    def get_server_status(self) -> Dict:
        with self._lock:
            dead_session_ids = [
                sid
                for sid, session in self._sessions_by_id.items()
                if not self._is_session_alive(session.browser_session)
            ]
            for session_id in dead_session_ids:
                self._remove_session_locked(session_id, close_session=False)

            active_sessions = [session.to_summary() for session in self._sessions_by_id.values()]
            external_busy = self._get_external_busy_details()
            running_processes = external_busy.get("running_processes", [])
            keepalive_lock_active = bool(external_busy.get("keepalive_lock_active"))
            if self._starting_profile_name:
                return {
                    "state": "starting",
                    "busy": True,
                    "owner_profile_name": self._starting_profile_name,
                    "owner_session_id": "",
                    "started_at": self._starting_started_at,
                    "active_session_count": len(active_sessions),
                    "active_sessions": active_sessions,
                    "external_running_process_count": len(running_processes),
                    "external_running_processes": running_processes,
                    "keepalive_lock_active": keepalive_lock_active,
                    "message": f"profile is starting: {self._starting_profile_name}",
                }

            if active_sessions:
                owner = active_sessions[0]
                return {
                    "state": "occupied",
                    "busy": True,
                    "owner_profile_name": owner.get("profile_name", ""),
                    "owner_session_id": owner.get("session_id", ""),
                    "started_at": owner.get("created_at", 0),
                    "active_session_count": len(active_sessions),
                    "active_sessions": active_sessions,
                    "external_running_process_count": len(running_processes),
                    "external_running_processes": running_processes,
                    "keepalive_lock_active": keepalive_lock_active,
                    "message": f"profile is in use: {owner.get('profile_name', '')}",
                }

            if keepalive_lock_active:
                return {
                    "state": "keepalive_running",
                    "busy": True,
                    "owner_profile_name": "",
                    "owner_session_id": "",
                    "started_at": 0,
                    "active_session_count": 0,
                    "active_sessions": [],
                    "external_running_process_count": len(running_processes),
                    "external_running_processes": running_processes,
                    "keepalive_lock_active": True,
                    "message": "keepalive job is running",
                }

            if running_processes:
                return {
                    "state": "external_chromium_running",
                    "busy": True,
                    "owner_profile_name": "",
                    "owner_session_id": "",
                    "started_at": 0,
                    "active_session_count": 0,
                    "active_sessions": [],
                    "external_running_process_count": len(running_processes),
                    "external_running_processes": running_processes,
                    "keepalive_lock_active": False,
                    "message": f"chromium is already running ({len(running_processes)} process(es))",
                }

            return {
                "state": "idle",
                "busy": False,
                "default_engine_name": resolve_browser_engine_name(self._load_config()),
                "owner_profile_name": "",
                "owner_session_id": "",
                "started_at": 0,
                "active_session_count": 0,
                "active_sessions": [],
                "external_running_process_count": 0,
                "external_running_processes": [],
                "keepalive_lock_active": False,
                "message": "server is idle",
            }

    def can_start_session(self, profile_name: str, engine_name: str = "") -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")

        status = self.get_server_status()
        owner_profile_name = str(status.get("owner_profile_name", "") or "")
        owner_session_id = str(status.get("owner_session_id", "") or "")
        same_profile_owned = bool(owner_profile_name) and owner_profile_name == profile_name
        allowed = not bool(status.get("busy"))
        return {
            "allowed": bool(allowed),
            "profile_name": profile_name,
            "engine_name": resolve_browser_engine_name(self._load_config(), engine_name),
            "reusable": bool(same_profile_owned and owner_session_id),
            "reusable_session_id": owner_session_id if same_profile_owned else "",
            "status": status,
        }

    def start_session(self, profile_name: str, reuse_existing: bool = False, engine_name: str = "") -> Dict:
        profile_name = str(profile_name or "").strip()
        if not profile_name:
            raise ValueError("profile_name is required")
        config = self._load_config()
        resolved_engine_name = resolve_browser_engine_name(config, engine_name)

        with self._lock:
            if reuse_existing:
                existing_session_id = self._session_id_by_profile.get(profile_name)
                if existing_session_id:
                    session = self._sessions_by_id.get(existing_session_id)
                    if session and session.engine_name == resolved_engine_name and self._is_session_alive(session.browser_session):
                        session.last_used_at = time.time()
                        return {
                            "session_id": session.session_id,
                            "profile_name": session.profile_name,
                            "engine_name": session.engine_name,
                            "reused": True,
                        }
                    if existing_session_id:
                        self._remove_session_locked(existing_session_id, close_session=True)
            else:
                existing_session_id = self._session_id_by_profile.get(profile_name)
                if existing_session_id:
                    session = self._sessions_by_id.get(existing_session_id)
                    if session and self._is_session_alive(session.browser_session):
                        raise RuntimeError(
                            f"profile already has an active session: {profile_name} ({session.session_id})"
                        )
                    if existing_session_id:
                        self._remove_session_locked(existing_session_id, close_session=True)

            busy_status = self.get_server_status()
            if busy_status.get("busy") and busy_status.get("owner_profile_name") != profile_name:
                state = str(busy_status.get("state", "occupied"))
                if state == "external_chromium_running":
                    raise RuntimeError(busy_status.get("message", "chromium is already running"))
                if state == "keepalive_running":
                    raise RuntimeError(busy_status.get("message", "keepalive job is running"))
                raise RuntimeError(
                    "browser service is busy: "
                    f"{busy_status.get('owner_profile_name') or 'unknown'} "
                    f"({state})"
                )

            if profile_name not in self._get_profile_names():
                raise ValueError(f"profile not found: {profile_name}")

            self._starting_profile_name = profile_name
            self._starting_started_at = time.time()

        try:
            print(
                f"[{now_text()}] [SESSION] start_session begin: profile={profile_name} engine={resolved_engine_name}",
                flush=True,
            )
            ensure_profile_bookmarks_initialized(config, profile_name)
            print(
                f"[{now_text()}] [SESSION] bookmarks ready: profile={profile_name}",
                flush=True,
            )
            engine = create_browser_engine(resolved_engine_name)
            print(
                f"[{now_text()}] [SESSION] engine created: profile={profile_name} engine={resolved_engine_name}",
                flush=True,
            )
            browser_session = ManagedBrowserSession(engine.create_session(config, profile_name))
            print(
                f"[{now_text()}] [SESSION] browser session created: profile={profile_name} engine={resolved_engine_name}",
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
            )
            with self._lock:
                self._sessions_by_id[session_id] = session
                self._session_id_by_profile[profile_name] = session_id
            return {
                "session_id": session_id,
                "profile_name": profile_name,
                "engine_name": resolved_engine_name,
                "reused": False,
            }
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
                self._remove_session_locked(session_id, close_session=False)
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
                }
            self._remove_session_locked(session.session_id, close_session=True)
            return {
                "session_id": session.session_id,
                "profile_name": session.profile_name,
                "engine_name": session.engine_name,
                "closed": True,
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
        self._session_id_by_profile.pop(session.profile_name, None)
        if close_session:
            try:
                session.browser_session.close()
            except Exception:
                pass

    def _is_session_alive(self, browser_session) -> bool:
        try:
            return bool(browser_session.get_summary().alive)
        except Exception:
            return False
