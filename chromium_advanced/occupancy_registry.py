import os
import threading
import time
from typing import Dict, Optional

from chromium_advanced.chromium_profile_lib import (
    SingleRunLock,
    append_jsonl_event,
    get_state_storage_dir,
    load_json_file,
    now_text,
    write_json_atomic,
)


_OCCUPANCY_REGISTRY_LOCK = threading.RLock()


def get_occupancy_registry_path() -> str:
    return os.path.join(get_state_storage_dir(), "profile_occupancy_registry.json")


def get_occupancy_registry_lock_path() -> str:
    return os.path.join(get_state_storage_dir(), "profile_occupancy_registry.lock")


def get_occupancy_events_path() -> str:
    return os.path.join(get_state_storage_dir(), "profile_occupancy_events.jsonl")


def _load_profile_occupancy_registry_unlocked() -> Dict:
    loaded = load_json_file(get_occupancy_registry_path(), default={})
    if not isinstance(loaded, dict):
        return {"profiles": {}, "updated_at": ""}
    loaded.setdefault("profiles", {})
    loaded.setdefault("updated_at", "")
    return loaded


def _acquire_occupancy_registry_file_lock(timeout_seconds: float = 5.0, poll_interval_seconds: float = 0.05):
    lock = SingleRunLock(get_occupancy_registry_lock_path(), stale_seconds=60)
    deadline = time.time() + max(0.1, float(timeout_seconds or 0.1))
    while True:
        if lock.try_acquire():
            return lock
        if time.time() >= deadline:
            raise TimeoutError("timed out acquiring occupancy registry lock")
        time.sleep(max(0.01, float(poll_interval_seconds or 0.01)))


def load_profile_occupancy_registry(
    *,
    tolerate_lock_timeout: bool = False,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
) -> Dict:
    with _OCCUPANCY_REGISTRY_LOCK:
        try:
            registry_lock = _acquire_occupancy_registry_file_lock(
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        except TimeoutError:
            if not tolerate_lock_timeout:
                raise
            return _load_profile_occupancy_registry_unlocked()
        try:
            return _load_profile_occupancy_registry_unlocked()
        finally:
            registry_lock.release()


def list_profile_occupancy_entries(*, tolerate_lock_timeout: bool = False) -> Dict[str, Dict]:
    payload = load_profile_occupancy_registry(tolerate_lock_timeout=tolerate_lock_timeout)
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    return {str(name): dict(value) for name, value in profiles.items() if isinstance(value, dict)}


def write_profile_occupancy(
    profile_name: str,
    *,
    scene_type: str,
    state: str,
    owner_label: str = "",
    engine_name: str = "",
    session_id: str = "",
    details: Optional[Dict] = None,
    event_source: str = "",
    owner_pid: int = 0,
    heartbeat_timeout_seconds: int = 0,
    lease_expires_at: float = 0.0,
    last_heartbeat_at: float = 0.0,
    reclaimable: bool = False,
) -> Dict:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return {}
    now_ts = time.time()
    heartbeat_timeout_seconds = max(0, int(heartbeat_timeout_seconds or 0))
    if heartbeat_timeout_seconds > 0 and float(lease_expires_at or 0) <= 0:
        lease_expires_at = now_ts + heartbeat_timeout_seconds
    if float(last_heartbeat_at or 0) <= 0:
        last_heartbeat_at = now_ts
    with _OCCUPANCY_REGISTRY_LOCK:
        registry_lock = _acquire_occupancy_registry_file_lock()
        try:
            payload = _load_profile_occupancy_registry_unlocked()
            profiles = payload.setdefault("profiles", {})
            entry = {
                "profile_name": profile_name,
                "scene_type": str(scene_type or "").strip() or "unknown",
                "state": str(state or "").strip() or "active",
                "owner_label": str(owner_label or "").strip(),
                "engine_name": str(engine_name or "").strip(),
                "session_id": str(session_id or "").strip(),
                "updated_at": now_text(),
                "details": dict(details or {}),
                "owner_pid": int(owner_pid or 0),
                "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                "lease_expires_at": float(lease_expires_at or 0.0),
                "last_heartbeat_at": float(last_heartbeat_at or 0.0),
                "reclaimable": bool(reclaimable),
            }
            profiles[profile_name] = entry
            payload["updated_at"] = now_text()
            write_json_atomic(get_occupancy_registry_path(), payload)
        finally:
            registry_lock.release()
    append_jsonl_event(
        get_occupancy_events_path(),
        {
            "timestamp": round(now_ts, 3),
            "profile_name": profile_name,
            "scene_type": entry["scene_type"],
            "state": entry["state"],
            "owner_label": entry["owner_label"],
            "engine_name": entry["engine_name"],
            "session_id": entry["session_id"],
            "event_source": str(event_source or "").strip(),
            "details": entry["details"],
            "owner_pid": entry["owner_pid"],
            "heartbeat_timeout_seconds": entry["heartbeat_timeout_seconds"],
            "lease_expires_at": entry["lease_expires_at"],
            "last_heartbeat_at": entry["last_heartbeat_at"],
            "reclaimable": entry["reclaimable"],
        },
    )
    return entry


def clear_profile_occupancy(
    profile_name: str,
    *,
    session_id: str = "",
    event_state: str = "released",
    details: Optional[Dict] = None,
    event_source: str = "",
) -> Dict:
    profile_name = str(profile_name or "").strip()
    if not profile_name:
        return {}
    with _OCCUPANCY_REGISTRY_LOCK:
        registry_lock = _acquire_occupancy_registry_file_lock()
        try:
            payload = _load_profile_occupancy_registry_unlocked()
            profiles = payload.setdefault("profiles", {})
            previous = profiles.pop(profile_name, None)
            if not isinstance(previous, dict):
                return {}
            payload["updated_at"] = now_text()
            write_json_atomic(get_occupancy_registry_path(), payload)
        finally:
            registry_lock.release()
    if not isinstance(previous, dict):
        return {}
    append_jsonl_event(
        get_occupancy_events_path(),
        {
            "timestamp": round(time.time(), 3),
            "profile_name": profile_name,
            "scene_type": str((previous or {}).get("scene_type", "") or "unknown"),
            "state": str(event_state or "").strip() or "released",
            "owner_label": str((previous or {}).get("owner_label", "") or ""),
            "engine_name": str((previous or {}).get("engine_name", "") or ""),
            "session_id": str(session_id or (previous or {}).get("session_id", "") or ""),
            "event_source": str(event_source or "").strip(),
            "details": dict(details or {"cleared": True}),
            "owner_pid": int((previous or {}).get("owner_pid", 0) or 0),
            "heartbeat_timeout_seconds": int((previous or {}).get("heartbeat_timeout_seconds", 0) or 0),
            "lease_expires_at": float((previous or {}).get("lease_expires_at", 0.0) or 0.0),
            "last_heartbeat_at": float((previous or {}).get("last_heartbeat_at", 0.0) or 0.0),
            "reclaimable": bool((previous or {}).get("reclaimable", False)),
        },
    )
    return previous if isinstance(previous, dict) else {}


def occupancy_entry_is_expired(entry: Dict, now_ts: Optional[float] = None) -> bool:
    if not isinstance(entry, dict):
        return False
    now_ts = float(now_ts if now_ts is not None else time.time())
    lease_expires_at = float(entry.get("lease_expires_at", 0.0) or 0.0)
    if lease_expires_at > 0 and now_ts >= lease_expires_at:
        return True
    return False
