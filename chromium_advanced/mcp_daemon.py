import argparse
import asyncio
import ctypes
from contextlib import asynccontextmanager
import multiprocessing
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Dict, Optional
from urllib.parse import unquote, urlunsplit

import httpx
import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

from chromium_advanced.chromium_profile_lib import (
    get_default_config_path,
    get_hidden_subprocess_kwargs,
    get_project_root,
    get_runtime_launch_cwd,
    now_text,
)
from chromium_advanced.mcp_runtime_config import resolve_mcp_admin_token, resolve_mcp_api_token
from chromium_advanced.occupancy_registry import list_profile_occupancy_entries
from chromium_advanced.session_manager import SessionManager


HEALTHCHECK_TIMEOUT_SECONDS = 0.5
WORKER_START_TIMEOUT_SECONDS = 15.0
WATCHDOG_INTERVAL_SECONDS = 2.0
DAEMON_WARMUP_SECONDS = 2.0
ERROR_ALREADY_EXISTS = 183
WORKER_SESSION_STARTED_PATTERN = re.compile(
    r"\[MCP-WORKER\]\s+session\s+started:.*session_id=(?P<session_id>\S+)"
)
WORKER_SESSION_REUSED_PATTERN = re.compile(
    r"\[MCP-WORKER\]\s+session\s+reused:.*session_id=(?P<session_id>\S+)"
)
WORKER_SESSION_CLOSED_PATTERN = re.compile(
    r"\[MCP-WORKER\]\s+session\s+closed:.*session_id=(?P<session_id>\S+)"
)


def safe_print(text: str) -> None:
    message = str(text or "")
    data = (message + "\n").encode("utf-8", errors="replace")
    stream = getattr(sys, "stdout", None)
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except Exception:
            pass
    try:
        print(message, flush=True)
    except Exception:
        pass


def _as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    message = str(exc or "").strip() or type(exc).__name__
    lowered = message.lower()
    if "not found" in lowered or "missing" in lowered:
        return HTTPException(status_code=404, detail=message)
    if (
        "already held" in lowered
        or "already running" in lowered
        or "start blocked" in lowered
        or "already in use" in lowered
        or "occupied" in lowered
        or "reusable session" in lowered
        or "no longer alive" in lowered
    ):
        return HTTPException(status_code=409, detail=message)
    if "required" in lowered or "invalid" in lowered or "unsupported" in lowered:
        return HTTPException(status_code=400, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=message)
    if "not found" in lowered:
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=500, detail=message)


def _extract_action_level_error(result: object) -> tuple[bool, str, str]:
    if not isinstance(result, dict):
        return False, "", ""
    if result.get("ok") is not False:
        return False, "", ""
    return (
        True,
        str(result.get("error", "") or "").strip(),
        str(result.get("error_type", "") or "").strip(),
    )


def normalize_path(path: str) -> str:
    text = str(path or "/mcp").strip() or "/mcp"
    if not text.startswith("/"):
        text = "/" + text
    return text


def acquire_single_instance_guard(name: str):
    if platform.system() != "Windows":
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, str(name))
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance_guard(handle) -> None:
    if not handle or platform.system() != "Windows":
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def can_connect(host: str, port: int, timeout: float = HEALTHCHECK_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def build_worker_command(
    transport: str,
    host: str,
    port: int,
    path: str,
    log_level: str,
    config_path: str,
) -> list[str]:
    worker_args: list[str]
    if getattr(sys, "frozen", False):
        extension = ".exe" if platform.system() == "Windows" else ""
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
        parent_dir = os.path.dirname(base_dir)
        candidates = [
            os.path.join(base_dir, f"ChromiumMcpWorker{extension}"),
            os.path.join(base_dir, "ChromiumMcpWorker", f"ChromiumMcpWorker{extension}"),
            os.path.join(base_dir, "ChromiumMcpWorker", "ChromiumMcpWorker", f"ChromiumMcpWorker{extension}"),
            os.path.join(parent_dir, f"ChromiumMcpWorker{extension}"),
            os.path.join(parent_dir, "ChromiumMcpWorker", f"ChromiumMcpWorker{extension}"),
            os.path.join(parent_dir, "ChromiumMcpWorker", "ChromiumMcpWorker", f"ChromiumMcpWorker{extension}"),
        ]
        worker_executable = candidates[0]
        for candidate in candidates:
            if os.path.exists(candidate):
                worker_executable = candidate
                break
        worker_args = [
            worker_executable,
            "--transport",
            transport,
            "--host",
            host,
            "--port",
            str(int(port)),
            "--path",
            path,
            "--log-level",
            log_level,
            "--config-path",
            config_path,
        ]
        return worker_args

    worker_args = [
        sys.executable,
        "-m",
        "chromium_advanced.mcp_server",
        "--transport",
        transport,
        "--host",
        host,
        "--port",
        str(int(port)),
        "--path",
        path,
        "--log-level",
        log_level,
        "--config-path",
        config_path,
    ]
    return worker_args


class WorkerManager:
    def __init__(
        self,
        session_manager: SessionManager,
        config_path: str,
        transport: str,
        public_host: str,
        public_port: int,
        public_path: str,
        worker_host: str,
        worker_port: int,
        log_level: str,
        idle_timeout_seconds: int,
        worker_policy: str,
    ):
        self.session_manager = session_manager
        self.config_path = config_path
        self.transport = transport
        self.public_host = public_host
        self.public_port = int(public_port)
        self.public_path = normalize_path(public_path)
        self.worker_host = worker_host
        self.worker_port = int(worker_port)
        self.log_level = log_level
        self.idle_timeout_seconds = int(idle_timeout_seconds)
        self.worker_policy = str(worker_policy or "sticky").strip().lower() or "sticky"

        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._active_proxy_requests = 0
        self._last_request_at = 0.0
        self._last_activity_at = 0.0
        self._last_start_at = 0.0
        self._last_stop_at = 0.0
        self._last_error = ""
        self._last_exit_code: Optional[int] = None
        self._last_stop_reason = ""
        self._worker_ready_once = False
        self._active_browser_session_ids: set[str] = set()
        self._worker_log_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def shutdown(self) -> None:
        self._watchdog_stop.set()
        self.stop_worker("daemon_shutdown")

    def begin_proxy_request(self) -> None:
        with self._lock:
            self._active_proxy_requests += 1
            now_ts = time.time()
            self._last_request_at = now_ts
            self._last_activity_at = now_ts

    def end_proxy_request(self) -> None:
        with self._lock:
            if self._active_proxy_requests > 0:
                self._active_proxy_requests -= 1
            self._last_activity_at = time.time()

    def mark_proxy_activity(self) -> None:
        with self._lock:
            self._last_activity_at = time.time()

    def _reconcile_active_browser_session_ids_locked(self) -> None:
        try:
            occupancy_entries = list_profile_occupancy_entries()
        except Exception:
            return
        live_session_ids = {
            str(entry.get("session_id", "") or "").strip()
            for entry in occupancy_entries.values()
            if isinstance(entry, dict) and str(entry.get("session_id", "") or "").strip()
        }
        if not live_session_ids:
            self._active_browser_session_ids.clear()
            return
        self._active_browser_session_ids.intersection_update(live_session_ids)

    def _handle_worker_log_line(self, line: str) -> None:
        text = str(line or "").rstrip()
        if not text:
            return
        safe_print(text)
        started_match = WORKER_SESSION_STARTED_PATTERN.search(text)
        if started_match:
            session_id = str(started_match.group("session_id") or "").strip()
            if session_id:
                with self._lock:
                    self._active_browser_session_ids.add(session_id)
            return
        reused_match = WORKER_SESSION_REUSED_PATTERN.search(text)
        if reused_match:
            session_id = str(reused_match.group("session_id") or "").strip()
            if session_id:
                with self._lock:
                    self._active_browser_session_ids.add(session_id)
            return
        closed_match = WORKER_SESSION_CLOSED_PATTERN.search(text)
        if closed_match:
            session_id = str(closed_match.group("session_id") or "").strip()
            if session_id:
                with self._lock:
                    self._active_browser_session_ids.discard(session_id)

    def _start_worker_log_thread(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return

        def _pump() -> None:
            try:
                for raw_line in process.stdout:
                    self._handle_worker_log_line(raw_line)
            except Exception as exc:
                safe_print(f"[{now_text()}] [MCP-DAEMON] worker log pump failed: {exc}")
            finally:
                try:
                    process.stdout.close()
                except Exception:
                    pass

        self._worker_log_thread = threading.Thread(target=_pump, daemon=True)
        self._worker_log_thread.start()

    def ensure_worker_running(self) -> Dict:
        with self._lock:
            self._cleanup_dead_process_locked()
            if self._is_worker_healthy_locked():
                now_ts = time.time()
                self._last_request_at = now_ts
                self._last_activity_at = now_ts
                return self.get_status()

            self._last_error = ""
            self._last_exit_code = None
            self._last_stop_reason = ""
            self._worker_ready_once = False
            command = build_worker_command(
                transport=self.transport,
                host=self.worker_host,
                port=self.worker_port,
                path=self.public_path,
                log_level=self.log_level,
                config_path=self.config_path,
            )
            safe_print(f"[{now_text()}] [MCP-DAEMON] starting worker: {' '.join(command)}")
            self._process = subprocess.Popen(
                command,
                cwd=get_runtime_launch_cwd(command[0] if command else ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **get_hidden_subprocess_kwargs(),
            )
            self._active_browser_session_ids.clear()
            self._start_worker_log_thread(self._process)
            self._last_start_at = time.time()
            self._last_request_at = self._last_start_at
            self._last_activity_at = self._last_start_at

        deadline = time.time() + WORKER_START_TIMEOUT_SECONDS
        while time.time() < deadline:
            with self._lock:
                self._cleanup_dead_process_locked()
                if self._is_worker_healthy_locked():
                    self._worker_ready_once = True
                    return self.get_status()
                if self._process is None:
                    break
            time.sleep(0.25)

        self.stop_worker("startup_timeout")
        message = self._last_error or "worker did not become ready in time"
        raise RuntimeError(message)

    def stop_worker(self, reason: str) -> Dict:
        with self._lock:
            self._stop_worker_locked(reason)
            return self.get_status()

    def get_status(self) -> Dict:
        with self._lock:
            self._cleanup_dead_process_locked()
            self._reconcile_active_browser_session_ids_locked()
            worker_pid = self._process.pid if self._process is not None else None
            worker_running = self._process is not None and self._process.poll() is None
            worker_listening = can_connect(self.worker_host, self.worker_port)
            public_endpoint = f"http://{self.public_host}:{self.public_port}{self.public_path}"
            worker_endpoint = f"http://{self.worker_host}:{self.worker_port}{self.public_path}"
            now_ts = time.time()
            idle_seconds = 0
            if self._last_activity_at > 0:
                idle_seconds = max(0, int(now_ts - self._last_activity_at))
            daemon_sessions = self.session_manager.list_sessions()
            daemon_session_ids = sorted(
                str(item.get("session_id", "") or "").strip()
                for item in daemon_sessions
                if str(item.get("session_id", "") or "").strip()
            )
            worker_session_ids = sorted(self._active_browser_session_ids)
            combined_session_ids = sorted(set(worker_session_ids + daemon_session_ids))
            return {
                "daemon_state": "running",
                "public_endpoint": public_endpoint,
                "worker_endpoint": worker_endpoint,
                "transport": self.transport,
                "worker_state": "running" if worker_running and worker_listening else "stopped",
                "worker_pid": worker_pid,
                "worker_listening": worker_listening,
                "worker_port": self.worker_port,
                "active_proxy_requests": self._active_proxy_requests,
                "active_browser_session_count": len(combined_session_ids),
                "active_browser_session_ids": combined_session_ids,
                "worker_browser_session_count": len(worker_session_ids),
                "worker_browser_session_ids": worker_session_ids,
                "daemon_browser_session_count": len(daemon_session_ids),
                "daemon_browser_session_ids": daemon_session_ids,
                "idle_timeout_seconds": self.idle_timeout_seconds,
                "worker_policy": self.worker_policy,
                "idle_seconds": idle_seconds,
                "last_request_at": self._last_request_at,
                "last_activity_at": self._last_activity_at,
                "last_start_at": self._last_start_at,
                "last_stop_at": self._last_stop_at,
                "last_stop_reason": self._last_stop_reason,
                "last_exit_code": self._last_exit_code,
                "last_error": self._last_error,
            }

    def _cleanup_dead_process_locked(self) -> None:
        if self._process is None:
            return
        exit_code = self._process.poll()
        if exit_code is None:
            return
        self._last_exit_code = exit_code
        exited_during_active_request = self._active_proxy_requests > 0
        exited_before_ready = not self._worker_ready_once
        classify_as_unexpected = exited_during_active_request or exited_before_ready
        if classify_as_unexpected and exit_code != 0 and not self._last_error:
            self._last_error = f"worker exited unexpectedly with code {exit_code}"
        safe_print(f"[{now_text()}] [MCP-DAEMON] worker exited: code={exit_code}")
        self._process = None
        self._last_stop_at = time.time()
        if not self._last_stop_reason:
            self._last_stop_reason = "unexpected_exit" if classify_as_unexpected else "self_terminated"
        if not classify_as_unexpected:
            self._last_error = ""

    def _is_worker_healthy_locked(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            return False
        return can_connect(self.worker_host, self.worker_port)

    def _stop_worker_locked(self, reason: str) -> None:
        self._last_stop_reason = str(reason or "manual")
        process = self._process
        if process is None:
            self._last_stop_at = time.time()
            return
        safe_print(f"[{now_text()}] [MCP-DAEMON] stopping worker: reason={self._last_stop_reason}")
        self._terminate_process_tree(process.pid)
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        raw_exit_code = process.poll()
        # Managed shutdowns are daemon-orchestrated lifecycle events, not
        # worker crashes. Avoid surfacing a synthetic non-zero exit code from a
        # forced terminate/kill as if it were an unexpected worker failure.
        if self._last_stop_reason in {"api_stop", "idle_timeout", "daemon_shutdown"}:
            self._last_exit_code = None
        else:
            self._last_exit_code = raw_exit_code
        self._process = None
        self._worker_ready_once = False
        self._active_browser_session_ids.clear()
        self._worker_log_thread = None
        self._last_stop_at = time.time()

    def _terminate_process_tree(self, root_pid: int) -> None:
        try:
            root = psutil.Process(root_pid)
        except Exception:
            return

        children = root.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except Exception:
                pass
        try:
            _, alive = psutil.wait_procs(children, timeout=3)
        except Exception:
            alive = children
        for child in alive:
            try:
                child.kill()
            except Exception:
                pass

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL_SECONDS):
            with self._lock:
                self._cleanup_dead_process_locked()
                self._reconcile_active_browser_session_ids_locked()
                if self._process is None:
                    continue
                if self.worker_policy == "always_on":
                    continue
                if self._active_browser_session_ids:
                    continue
                last_idle_basis_at = self._last_request_at or self._last_start_at or self._last_activity_at
                if last_idle_basis_at <= 0:
                    continue
                effective_timeout = self.idle_timeout_seconds
                if self.worker_policy == "sticky":
                    effective_timeout = max(self.idle_timeout_seconds, 300)
                if (time.time() - last_idle_basis_at) < effective_timeout:
                    continue
                self._stop_worker_locked("idle_timeout")

    def is_worker_running(self) -> bool:
        with self._lock:
            self._cleanup_dead_process_locked()
            return self._is_worker_healthy_locked()


def create_daemon_app(
    config_path: str,
    host: str,
    port: int,
    path: str,
    transport: str,
    log_level: str,
    worker_port: int,
    api_token: str,
    admin_token: str,
    idle_timeout_seconds: int,
    worker_policy: str,
    warmup_seconds: float = 0.0,
) -> FastAPI:
    public_path = normalize_path(path)
    daemon_pid = os.getpid()
    daemon_instance_id = f"{daemon_pid}-{uuid.uuid4().hex[:8]}"
    daemon_started_at = time.time()
    session_manager = SessionManager(config_path=config_path)
    worker_manager = WorkerManager(
        session_manager=session_manager,
        config_path=config_path,
        transport=transport,
        public_host=host,
        public_port=port,
        public_path=public_path,
        worker_host="127.0.0.1",
        worker_port=worker_port,
        log_level=log_level,
        idle_timeout_seconds=idle_timeout_seconds,
        worker_policy=worker_policy,
    )
    # -- Auth middleware --------------------------------------------------------
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    _require_auth = bool(api_token)
    _admin_token = str(admin_token or "").strip()
    _require_admin_auth = bool(_admin_token)

    effective_warmup_seconds = max(0.0, float(warmup_seconds or 0.0))

    def _daemon_ready() -> bool:
        return (time.time() - daemon_started_at) >= effective_warmup_seconds

    def _daemon_warmup_payload() -> Dict:
        remaining_seconds = max(0.0, effective_warmup_seconds - (time.time() - daemon_started_at))
        return {
            "error": "Daemon is warming up",
            "detail": "Retry the request after daemon startup finishes.",
            "daemon_pid": daemon_pid,
            "daemon_instance_id": daemon_instance_id,
            "daemon_ready": False,
            "warmup_remaining_ms": int(remaining_seconds * 1000),
        }

    def _is_management_path(path_text: str) -> bool:
        path_text = str(path_text or "").strip()
        if not path_text.startswith("/_daemon/"):
            return False
        if path_text.startswith("/_daemon/automation/"):
            return False
        if path_text.startswith("/_daemon/status"):
            return False
        if path_text.startswith("/_daemon/profiles") and "/reclaim" not in path_text:
            return False
        return True

    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth_header = (request.headers.get("authorization") or "").strip()
            token_value = ""
            if _require_auth:
                if not auth_header:
                    return JSONResponse(
                        {"error": "Authentication required", "detail": "Missing Authorization header"},
                        status_code=401,
                    )
                parts = auth_header.split()
                if len(parts) != 2 or parts[0].lower() != "bearer":
                    return JSONResponse(
                        {"error": "Authentication required", "detail": "Authorization header must use Bearer scheme"},
                        status_code=401,
                    )
                token_value = parts[1]
                if not secrets.compare_digest(token_value, api_token) and not (_require_admin_auth and secrets.compare_digest(token_value, _admin_token)):
                    return JSONResponse(
                        {"error": "Authentication required", "detail": "Invalid API token"},
                        status_code=401,
                    )
            if _is_management_path(request.url.path):
                if not _require_admin_auth:
                    return JSONResponse(
                        {"error": "Management API disabled", "detail": "Admin token is not configured for management endpoints"},
                        status_code=403,
                    )
                if not token_value or not secrets.compare_digest(token_value, _admin_token):
                    return JSONResponse(
                        {"error": "Admin authentication required", "detail": "Management endpoints require the admin token"},
                        status_code=403,
                    )
            request_path = str(request.url.path or "").strip()
            if (
                not _daemon_ready()
                and request_path not in {"/", "/health", "/_daemon/status"}
                and not request_path.startswith("/_daemon/status")
            ):
                remaining_seconds = max(0.0, effective_warmup_seconds - (time.time() - daemon_started_at))
                response = JSONResponse(_daemon_warmup_payload(), status_code=503)
                response.headers["Retry-After"] = str(max(1, int(round(max(remaining_seconds, 1.0)))))
                return response
            return await call_next(request)


    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            worker_manager.shutdown()

    app = FastAPI(title="Chromium Advanced MCP Daemon", lifespan=lifespan)

    app.add_middleware(_AuthMiddleware)
    @app.get("/")
    def root() -> Dict:
        status = worker_manager.get_status()
        return {
            "name": "chromium-advanced-mcp-daemon",
            "daemon_pid": daemon_pid,
            "daemon_instance_id": daemon_instance_id,
            "daemon_ready": _daemon_ready(),
            "status": status,
        }

    @app.get("/health")
    def health() -> Dict:
        return {
            "ok": True,
            "time": now_text(),
            "daemon_pid": daemon_pid,
            "daemon_instance_id": daemon_instance_id,
            "daemon_ready": _daemon_ready(),
            "status": worker_manager.get_status(),
        }

    @app.get("/_daemon/status")
    def daemon_status() -> Dict:
        started_at = time.perf_counter()
        try:
            session_manager.reconcile_stale_profile_occupancy()
            session_manager.reap_expired_profile_occupancy()
            status = worker_manager.get_status()
            status["daemon_pid"] = daemon_pid
            status["daemon_instance_id"] = daemon_instance_id
            status["daemon_ready"] = _daemon_ready()
            status["warmup_remaining_ms"] = max(0, int((effective_warmup_seconds - (time.time() - daemon_started_at)) * 1000))
            status["server_status"] = session_manager.get_runtime_status_snapshot()
            status["status_build_ms"] = int((time.perf_counter() - started_at) * 1000)
            return status
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_daemon/profiles")
    def daemon_profiles() -> Dict:
        started_at = time.perf_counter()
        try:
            session_manager.reconcile_stale_profile_occupancy()
            session_manager.reap_expired_profile_occupancy()
            return {
                "daemon_pid": daemon_pid,
                "daemon_instance_id": daemon_instance_id,
                "daemon_ready": _daemon_ready(),
                "profiles": session_manager.list_profiles(),
                "events": session_manager.list_recent_occupancy_events(limit=50),
                "status_build_ms": int((time.perf_counter() - started_at) * 1000),
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_daemon/profiles/{profile_name}")
    def daemon_profile_status(profile_name: str) -> Dict:
        session_manager.reconcile_stale_profile_occupancy()
        session_manager.reap_expired_profile_occupancy()
        try:
            return session_manager.get_profile_status(unquote(profile_name))
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/profiles/{profile_name}/reclaim")
    async def daemon_profile_reclaim(profile_name: str, request: Request) -> Dict:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = str((body or {}).get("reason", "") or "daemon_api_reclaim")
        try:
            decoded_profile_name = unquote(profile_name)
            await asyncio.to_thread(session_manager.get_profile_status, decoded_profile_name)
            return await asyncio.to_thread(session_manager.reclaim_profile, decoded_profile_name, reason=reason)
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/reap-expired")
    def daemon_reap_expired() -> Dict:
        try:
            results = session_manager.reap_expired_profile_occupancy()
            return {"reclaimed": results, "count": len(results)}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/automation/acquire")
    async def daemon_automation_acquire(request: Request) -> Dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        profile_name = str((body or {}).get("profile_name", "") or "").strip()
        engine_name = str((body or {}).get("engine", "") or "").strip()
        owner_label = str((body or {}).get("owner_label", "") or "automation").strip() or "automation"
        reuse_existing = bool((body or {}).get("reuse_existing", False))
        runtime_options = dict((body or {}).get("runtime_options") or {})
        heartbeat_timeout_seconds = int((body or {}).get("heartbeat_timeout_seconds", 180) or 180)
        runtime_options.setdefault("heartbeat_timeout_seconds", heartbeat_timeout_seconds)
        try:
            result = await asyncio.to_thread(
                session_manager.start_session,
                profile_name=profile_name,
                reuse_existing=reuse_existing,
                engine_name=engine_name,
                scene_type="automation",
                owner_label=owner_label,
                runtime_options=runtime_options,
            )
            await asyncio.to_thread(
                session_manager.refresh_profile_lease,
                profile_name,
                scene_type="automation",
                owner_label=owner_label,
                engine_name=result.get("engine_name", ""),
                session_id=result.get("session_id", ""),
                owner_pid=os.getpid(),
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                details={"source": "daemon_automation_acquire", "runtime_options": runtime_options},
                reclaimable=True,
            )
            return result
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/automation/heartbeat")
    async def daemon_automation_heartbeat(request: Request) -> Dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        profile_name = str((body or {}).get("profile_name", "") or "").strip()
        owner_label = str((body or {}).get("owner_label", "") or "").strip()
        engine_name = str((body or {}).get("engine_name", "") or "").strip()
        session_id = str((body or {}).get("session_id", "") or "").strip()
        heartbeat_timeout_seconds = int((body or {}).get("heartbeat_timeout_seconds", 180) or 180)
        details = dict((body or {}).get("details") or {})
        try:
            return await asyncio.to_thread(
                session_manager.refresh_profile_lease,
                profile_name,
                scene_type="automation",
                owner_label=owner_label,
                engine_name=engine_name,
                session_id=session_id,
                owner_pid=int((body or {}).get("owner_pid", 0) or 0),
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                details=details,
                reclaimable=True,
            )
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/automation/release")
    async def daemon_automation_release(request: Request) -> Dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        session_id = str((body or {}).get("session_id", "") or "").strip()
        profile_name = str((body or {}).get("profile_name", "") or "").strip()
        try:
            if session_id:
                result = await asyncio.to_thread(session_manager.close_session, session_id)
                if result.get("closed"):
                    return result
            if profile_name:
                return await asyncio.to_thread(
                    session_manager.reclaim_profile,
                    profile_name,
                    reason="daemon_api_automation_release",
                )
            raise HTTPException(status_code=400, detail="session_id or profile_name is required")
        except HTTPException:
            raise
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_daemon/automation/action")
    async def daemon_automation_action(request: Request) -> Dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        session_id = str((body or {}).get("session_id", "") or "").strip()
        action = str((body or {}).get("action", "") or "").strip()
        owner_label = str((body or {}).get("owner_label", "") or "automation").strip() or "automation"
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        if not action:
            raise HTTPException(status_code=400, detail="action is required")
        args = dict((body or {}).get("args") or {})
        try:
            browser_session = await asyncio.to_thread(
                session_manager.resolve_session,
                session_id,
                scene_type="automation",
                owner_label=owner_label,
                refresh_lease=True,
            )
        except Exception as exc:
            raise _as_http_error(exc) from exc

        def _run_action() -> Dict:
            if action == "navigate":
                return browser_session.navigate(
                    str(args.get("url", "") or ""),
                    bool(args.get("wait_for_ready", True)),
                    int(args.get("timeout_seconds", 20) or 20),
                    tab_id=str(args.get("tab_id", "") or ""),
                )
            if action == "get_page_text":
                return browser_session.get_page_text(tab_id=str(args.get("tab_id", "") or ""))
            if action == "get_current_url":
                return browser_session.get_current_url(tab_id=str(args.get("tab_id", "") or ""))
            if action == "get_page_html":
                return browser_session.get_page_html(tab_id=str(args.get("tab_id", "") or ""))
            if action == "list_tabs":
                return browser_session.list_tabs()
            if action == "open_tab":
                return browser_session.open_tab(
                    url=str(args.get("url", "") or ""),
                    activate=bool(args.get("activate", True)),
                    wait_for_ready=bool(args.get("wait_for_ready", True)),
                    timeout_seconds=int(args.get("timeout_seconds", 20) or 20),
                )
            if action == "activate_tab":
                return browser_session.activate_tab(
                    tab_id=str(args.get("tab_id", "") or ""),
                    index=int(args.get("index", -1) or -1),
                    title_contains=str(args.get("title_contains", "") or ""),
                    url_contains=str(args.get("url_contains", "") or ""),
                )
            if action == "close_tab":
                return browser_session.close_tab(
                    tab_id=str(args.get("tab_id", "") or ""),
                    index=int(args.get("index", -1) or -1),
                )
            if action == "click":
                return browser_session.click(
                    str(args.get("selector", "") or ""),
                    str(args.get("by", "css") or "css"),
                    int(args.get("timeout_seconds", 20) or 20),
                )
            if action == "type_text":
                return browser_session.type_text(
                    str(args.get("selector", "") or ""),
                    str(args.get("text", "") or ""),
                    str(args.get("by", "css") or "css"),
                    bool(args.get("clear_first", True)),
                    bool(args.get("submit", False)),
                    int(args.get("timeout_seconds", 20) or 20),
                )
            if action == "press_key":
                return browser_session.press_key(
                    str(args.get("key", "") or ""),
                    int(args.get("count", 1) or 1),
                    str(args.get("selector", "") or ""),
                    str(args.get("by", "css") or "css"),
                    int(args.get("timeout_seconds", 20) or 20),
                )
            if action == "run_script":
                return browser_session.run_script(
                    str(args.get("script", "") or ""),
                    tab_id=str(args.get("tab_id", "") or ""),
                )
            if action == "run_script_batch":
                scripts = args.get("scripts") or []
                if not isinstance(scripts, list) or not scripts:
                    raise HTTPException(status_code=400, detail="scripts is required for run_script_batch")
                tab_id = str(args.get("tab_id", "") or "")
                stop_on_error = bool(args.get("stop_on_error", True))
                batch_results = []
                for index, script in enumerate(scripts):
                    script_text = str(script or "")
                    item = {
                        "index": index,
                        "script": script_text,
                    }
                    try:
                        item_result = browser_session.run_script(script_text, tab_id=tab_id)
                        item["result"] = item_result
                        failed, message, error_type = _extract_action_level_error(item_result)
                        item["ok"] = not failed
                        if failed:
                            if message:
                                item["error"] = message
                            if error_type:
                                item["error_type"] = error_type
                            if stop_on_error:
                                raise RuntimeError(message or "run_script_batch item failed")
                    except Exception as exc:
                        item["ok"] = False
                        item["error_type"] = type(exc).__name__
                        item["error"] = str(exc)
                        if stop_on_error:
                            raise
                    batch_results.append(item)
                return {
                    "count": len(batch_results),
                    "stop_on_error": stop_on_error,
                    "items": batch_results,
                }
            if action == "get_console_messages":
                return browser_session.get_console_messages(
                    tab_id=str(args.get("tab_id", "") or ""),
                    limit=int(args.get("limit", 100) or 100),
                    level=str(args.get("level", "") or ""),
                )
            if action == "get_network_requests":
                return browser_session.get_network_requests(
                    tab_id=str(args.get("tab_id", "") or ""),
                    limit=int(args.get("limit", 100) or 100),
                    failed_only=bool(args.get("failed_only", False)),
                )
            if action == "screenshot":
                return browser_session.screenshot(
                    str(args.get("filename", "") or ""),
                    tab_id=str(args.get("tab_id", "") or ""),
                )
            if action == "get_summary":
                return browser_session.get_summary()
            if action == "get_capabilities":
                return browser_session.get_capabilities()
            if action == "snapshot":
                return browser_session.snapshot(
                    target=str(args.get("target", "") or ""),
                    by=str(args.get("by", "css") or "css"),
                    depth=(int(args.get("depth", 0) or 0) or None),
                    boxes=bool(args.get("boxes", False)),
                    filename=str(args.get("filename", "") or ""),
                    tab_id=str(args.get("tab_id", "") or ""),
                )
            raise HTTPException(status_code=400, detail=f"unsupported automation action: {action}")

        try:
            result = await asyncio.to_thread(_run_action)
        except HTTPException:
            raise
        except Exception as exc:
            raise _as_http_error(exc) from exc
        return {
            "ok": True,
            "session_id": session_id,
            "action": action,
            "result": result if isinstance(result, dict) else {"value": result},
        }

    @app.post("/_daemon/worker/start")
    def daemon_worker_start() -> Dict:
        try:
            return worker_manager.ensure_worker_running()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/_daemon/worker/stop")
    def daemon_worker_stop() -> Dict:
        try:
            return worker_manager.stop_worker("api_stop")
        except Exception as exc:
            raise _as_http_error(exc) from exc

    async def proxy_to_worker(request: Request, tail: str = ""):
        method_upper = request.method.upper()
        client_host = getattr(request.client, "host", "") or "-"
        client_port = getattr(request.client, "port", "") or "-"
        target_label = public_path if not tail else f"{public_path.rstrip('/')}/{tail.lstrip('/')}"
        session_id = (
            request.headers.get("mcp-session-id")
            or request.headers.get("Mcp-Session-Id")
            or request.headers.get("x-mcp-session-id")
            or ""
        ).strip()
        user_agent = (request.headers.get("user-agent") or "").strip()
        accept = (request.headers.get("accept") or "").strip()
        safe_print(
            (
                f"[{now_text()}] [MCP-DAEMON] proxy request: {request.method} {target_label} "
                f"from {client_host}:{client_port} session_id={'yes' if session_id else 'no'} "
                f"user_agent={user_agent or '-'} accept={accept or '-'}"
            )
        )

        # Prevent generic localhost probes from waking the worker up. In
        # streamable-http mode, session streams and session deletion require a
        # session ID, while session creation starts with POST.
        if transport == "streamable-http" and method_upper != "POST" and not session_id:
            detail = {
                "error": "Missing session ID",
                "message": "This request must include an MCP session ID header.",
            }
            status_code = 400 if method_upper in {"GET", "HEAD", "DELETE"} else 405
            return JSONResponse(detail, status_code=status_code)

        # Once the worker has been reclaimed, lingering streamable-http GET or
        # DELETE requests for an old MCP session should not wake it back up.
        # The client must create a fresh session via POST when it has new work.
        if transport == "streamable-http" and method_upper != "POST" and not worker_manager.is_worker_running():
            detail = {
                "error": "MCP session is not active",
                "message": "The worker is not running. Create a new MCP session with POST before retrying this request.",
            }
            status_code = 404 if method_upper in {"GET", "DELETE", "HEAD"} else 405
            return JSONResponse(detail, status_code=status_code)

        try:
            await asyncio.to_thread(worker_manager.ensure_worker_running)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

        query_string = request.url.query
        target_path = public_path
        if tail:
            target_path = f"{public_path.rstrip('/')}/{tail.lstrip('/')}"
        backend_url = urlunsplit(("http", f"127.0.0.1:{worker_port}", target_path, query_string, ""))
        body = await request.body()
        request_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }

        client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        worker_manager.begin_proxy_request()
        try:
            backend_request = client.build_request(
                request.method,
                backend_url,
                headers=request_headers,
                content=body,
            )
            backend_response = await client.send(backend_request, stream=True)
        except Exception as exc:
            worker_manager.end_proxy_request()
            await client.aclose()
            return JSONResponse({"error": f"worker proxy failed: {exc}"}, status_code=502)

        response_headers = {
            key: value
            for key, value in backend_response.headers.items()
            if key.lower() not in {"content-length", "connection"}
        }

        async def close_stream() -> None:
            worker_manager.end_proxy_request()
            safe_print(
                f"[{now_text()}] [MCP-DAEMON] proxy request complete: {request.method} {target_label} from {client_host}:{client_port}"
            )
            await backend_response.aclose()
            await client.aclose()

        if method_upper == "HEAD":
            data = await backend_response.aread()
            worker_manager.mark_proxy_activity()
            await close_stream()
            return PlainTextResponse(
                content=data.decode(errors="replace"),
                status_code=backend_response.status_code,
                headers=response_headers,
            )

        async def iter_response_body():
            async for chunk in backend_response.aiter_raw():
                worker_manager.mark_proxy_activity()
                yield chunk

        return StreamingResponse(
            iter_response_body(),
            status_code=backend_response.status_code,
            headers=response_headers,
            background=BackgroundTask(close_stream),
        )

    app.add_api_route(public_path, proxy_to_worker, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    app.add_api_route(
        f"{public_path.rstrip('/')}/{{tail:path}}",
        proxy_to_worker,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    return app


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Chromium Advanced MCP Daemon")
    parser.add_argument("--host", default=os.environ.get("CHROMIUM_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("CHROMIUM_MCP_PORT", "28888"))
    parser.add_argument("--path", default=os.environ.get("CHROMIUM_MCP_PATH", "/mcp"))
    parser.add_argument("--transport", default=os.environ.get("CHROMIUM_MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--log-level", default=os.environ.get("CHROMIUM_MCP_LOG_LEVEL", "info"))
    parser.add_argument("--worker-port", default=os.environ.get("CHROMIUM_MCP_WORKER_PORT", "28889"))
    parser.add_argument("--idle-timeout-seconds", default=os.environ.get("CHROMIUM_MCP_IDLE_TIMEOUT_SECONDS", "60"))
    parser.add_argument("--api-token", default=os.environ.get("CHROMIUM_MCP_API_TOKEN", "").strip())
    parser.add_argument("--admin-token", default=os.environ.get("CHROMIUM_MCP_ADMIN_TOKEN", "").strip())
    parser.add_argument("--worker-policy", default=os.environ.get("CHROMIUM_MCP_WORKER_POLICY", "").strip())
    parser.add_argument("--config-path", default=os.environ.get("CHROMIUM_MCP_CONFIG_PATH", "").strip())
    args = parser.parse_args()

    config_path = args.config_path or os.environ.get("CHROMIUM_MCP_CONFIG_PATH", "").strip() or get_default_config_path()
    if not config_path:
        raise SystemExit("config path is required")
    from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config, save_app_config
    config = normalize_config(load_app_config(config_path))
    if not args.api_token:
        configured_token = str(config.get("mcp", {}).get("api_token", "")).strip()
        if not configured_token:
            configured_token = resolve_mcp_api_token(config)
            config.setdefault("mcp", {})
            config["mcp"]["api_token"] = configured_token
            config = save_app_config(config, config_path)
        args.api_token = configured_token
    if not args.admin_token:
        configured_admin_token = str(config.get("mcp", {}).get("admin_token", "")).strip()
        if not configured_admin_token:
            configured_admin_token = resolve_mcp_admin_token(config)
            config.setdefault("mcp", {})
            config["mcp"]["admin_token"] = configured_admin_token
            config = save_app_config(config, config_path)
        args.admin_token = configured_admin_token
    if not args.worker_policy:
        args.worker_policy = str(config.get("mcp", {}).get("worker_policy", "sticky")).strip() or "sticky"
    
    guard = acquire_single_instance_guard(f"Local\\ChromiumMcpDaemon-{int(args.port or 28888)}")
    if guard is None:
        raise SystemExit(f"MCP daemon already running on configured port {int(args.port or 28888)}")

    try:
        app = create_daemon_app(
            config_path=config_path,
            host=str(args.host or "127.0.0.1").strip() or "127.0.0.1",
            port=int(args.port or 28888),
            path=str(args.path or "/mcp").strip() or "/mcp",
            transport=str(args.transport or "streamable-http").strip() or "streamable-http",
            log_level=str(args.log_level or "info").strip() or "info",
            worker_port=int(args.worker_port or 28889),
            idle_timeout_seconds=int(args.idle_timeout_seconds or 60),
            api_token=str(args.api_token or "").strip(),
            admin_token=str(args.admin_token or "").strip(),
            worker_policy=str(args.worker_policy or "sticky").strip() or "sticky",
            warmup_seconds=DAEMON_WARMUP_SECONDS,
        )
        uvicorn.run(
            app,
            host=str(args.host or "127.0.0.1"),
            port=int(args.port or 28888),
            log_level=str(args.log_level or "info"),
            log_config=None,
        )
    finally:
        release_single_instance_guard(guard)


if __name__ == "__main__":
    main()
