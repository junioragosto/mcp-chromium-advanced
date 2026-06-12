from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from chromium_advanced.chromium_profile_lib import write_profile_occupancy
from chromium_advanced.session_manager import SessionManager

DEFAULT_AUTOMATION_HEARTBEAT_TIMEOUT_SECONDS = 180


@dataclass
class ManagedAutomationRun:
    session_manager: SessionManager
    session_id: str
    profile_name: str
    engine_name: str
    owner_label: str
    started_at: float
    heartbeat_details: Optional[Dict] = None
    heartbeat_timeout_seconds: int = DEFAULT_AUTOMATION_HEARTBEAT_TIMEOUT_SECONDS

    def session(self):
        return self.session_manager.resolve_session(self.session_id)

    def heartbeat(self, details: Optional[Dict] = None) -> Dict:
        merged_details = {
            "source": "automation_runner",
            "pid": os.getpid(),
            "heartbeat_at": round(time.time(), 3),
        }
        if isinstance(self.heartbeat_details, dict):
            merged_details.update(self.heartbeat_details)
        if isinstance(details, dict):
            merged_details.update(details)
        self.heartbeat_details = merged_details
        return write_profile_occupancy(
            self.profile_name,
            scene_type="automation",
            state="active",
            owner_label=self.owner_label or "automation",
            engine_name=self.engine_name,
            session_id=self.session_id,
            details=merged_details,
            event_source="automation_runner",
            owner_pid=os.getpid(),
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            )

    def close(self) -> Dict:
        return self.session_manager.close_session(self.session_id)

    def reclaim(self, reason: str = "automation_reclaim") -> Dict:
        return self.session_manager.reclaim_profile(self.profile_name, reason=reason)

    def __enter__(self) -> "ManagedAutomationRun":
        self.heartbeat()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class AutomationRunner:
    def __init__(self, config_path: str = ""):
        self.session_manager = SessionManager(config_path=config_path or None)

    def acquire(
        self,
        profile_name: str,
        engine_name: str,
        owner_label: str = "",
        reuse_existing: bool = False,
        heartbeat_details: Optional[Dict] = None,
        heartbeat_timeout_seconds: int = DEFAULT_AUTOMATION_HEARTBEAT_TIMEOUT_SECONDS,
        runtime_options: Optional[Dict] = None,
    ) -> ManagedAutomationRun:
        session_info = self.session_manager.start_session(
            profile_name=profile_name,
            reuse_existing=bool(reuse_existing),
            engine_name=engine_name,
            scene_type="automation",
            owner_label=owner_label or "automation",
            runtime_options=dict(runtime_options or {}),
        )
        return ManagedAutomationRun(
            session_manager=self.session_manager,
            session_id=str(session_info.get("session_id", "")),
            profile_name=str(session_info.get("profile_name", "")),
            engine_name=str(session_info.get("engine_name", "")),
            owner_label=str(owner_label or "automation"),
            started_at=time.time(),
            heartbeat_details=dict(heartbeat_details or {}),
            heartbeat_timeout_seconds=max(10, int(heartbeat_timeout_seconds or DEFAULT_AUTOMATION_HEARTBEAT_TIMEOUT_SECONDS)),
        )

    def run(
        self,
        profile_name: str,
        engine_name: str,
        owner_label: str,
        callback: Callable[[ManagedAutomationRun], Dict],
        reuse_existing: bool = False,
        heartbeat_details: Optional[Dict] = None,
        heartbeat_timeout_seconds: int = DEFAULT_AUTOMATION_HEARTBEAT_TIMEOUT_SECONDS,
        runtime_options: Optional[Dict] = None,
    ) -> Dict:
        run = self.acquire(
            profile_name=profile_name,
            engine_name=engine_name,
            owner_label=owner_label,
            reuse_existing=bool(reuse_existing),
            heartbeat_details=heartbeat_details,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            runtime_options=runtime_options,
        )
        try:
            run.heartbeat()
            result = callback(run)
            return {
                "ok": True,
                "session_id": run.session_id,
                "profile_name": run.profile_name,
                "engine_name": run.engine_name,
                "owner_label": run.owner_label,
                "result": result if isinstance(result, dict) else {"value": result},
            }
        finally:
            run.close()
