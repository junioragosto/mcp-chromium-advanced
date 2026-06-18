import argparse
import asyncio
import ctypes
import inspect
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
    append_jsonl_event,
    build_keepalive_plugin_template,
    delete_keepalive_plugin_source,
    ensure_profile_bookmarks_initialized,
    get_default_config_path,
    get_keepalive_plugin_records,
    get_keepalive_plugin_source_text,
    get_hidden_subprocess_kwargs,
    inspect_keepalive_plugin_source,
    get_project_root,
    get_runtime_launch_cwd,
    get_chromium_processes_for_profile,
    launch_profile,
    now_text,
    normalize_config,
    load_app_config,
    read_recent_jsonl_events,
    save_app_config,
    save_keepalive_plugin_source,
    terminate_chromium_processes,
    update_profile_launch_time,
    run_keepalive_job,
)
from chromium_advanced.mcp_runtime_config import resolve_control_api_token, resolve_mcp_api_token
from chromium_advanced.occupancy_registry import list_profile_occupancy_entries
from chromium_advanced.session_manager import SessionManager
from chromium_advanced.engine_strategy import resolve_engine_strategy
from chromium_advanced.action_pipeline import ActionPipeline


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
CONTROL_LOG_FILE_NAME = "control_runtime.jsonl"


class KeepaliveJobManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_controller = None
        self._runtime = {
            "running": False,
            "source": "",
            "selected_profiles": [],
            "started_at": "",
            "finished_at": "",
            "last_summary": {},
            "last_error": "",
            "current_profile_name": "",
            "stop_requested": False,
        }

    def get_status(self) -> Dict:
        with self._lock:
            return dict(self._runtime)

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._runtime.get("running", False))

    def start(self, *, selected_profiles: Optional[list[str]] = None, source: str = "manual") -> Dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("keepalive job already running")
            from chromium_advanced.keepalive_runtime import KeepAliveStopController

            self._stop_controller = KeepAliveStopController()
            self._runtime = {
                "running": True,
                "source": str(source or "manual"),
                "selected_profiles": [str(item).strip() for item in (selected_profiles or []) if str(item).strip()],
                "started_at": now_text(),
                "finished_at": "",
                "last_summary": {},
                "last_error": "",
                "current_profile_name": "",
                "stop_requested": False,
            }
            thread = threading.Thread(
                target=self._run_job,
                args=(list(self._runtime["selected_profiles"]), self._runtime["source"], self._stop_controller),
                name="chromium-keepalive-job",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self.get_status()

    def request_stop(self) -> Dict:
        with self._lock:
            if self._stop_controller is not None:
                self._runtime["stop_requested"] = True
                self._stop_controller.request_stop()
            return dict(self._runtime)

    def _run_job(self, selected_profiles: list[str], source: str, stop_controller) -> None:
        def _progress(kind: str, payload: Dict) -> None:
            if str(kind or "").strip().lower() != "profile_start":
                return
            with self._lock:
                self._runtime["current_profile_name"] = str((payload or {}).get("profile_name", "") or "")

        try:
            summary = run_keepalive_job(
                config_path=self.config_path,
                selected_profiles=selected_profiles,
                logger=None,
                source=source,
                stop_controller=stop_controller,
                progress_callback=_progress,
            )
            with self._lock:
                self._runtime["last_summary"] = dict(summary or {})
                self._runtime["finished_at"] = str(summary.get("finished_at", "") or now_text())
                self._runtime["current_profile_name"] = ""
                self._runtime["running"] = False
                self._runtime["last_error"] = ""
        except Exception as exc:
            with self._lock:
                self._runtime["last_error"] = str(exc)
                self._runtime["finished_at"] = now_text()
                self._runtime["current_profile_name"] = ""
                self._runtime["running"] = False
        finally:
            with self._lock:
                self._thread = None
                self._stop_controller = None


class ManualProfileRuntimeManager:
    def __init__(self, session_manager: SessionManager, config_path: str):
        self.session_manager = session_manager
        self.config_path = config_path

    def launch(self, profile_name: str) -> Dict:
        config = normalize_config(load_app_config(self.config_path))
        normalized_name = str(profile_name or "").strip()
        if not normalized_name:
            raise ValueError("profile_name is required")
        ensure_profile_bookmarks_initialized(config, normalized_name)
        result = launch_profile(normalized_name, config)
        config = update_profile_launch_time(config, normalized_name)
        save_app_config(config, self.config_path)
        time.sleep(0.5)
        processes = get_chromium_processes_for_profile(config, normalized_name)
        owner_pid = 0
        if processes:
            try:
                owner_pid = int(processes[0].get("pid", 0) or 0)
            except Exception:
                owner_pid = 0
        self.session_manager._register_profile_occupancy(
            normalized_name,
            scene_type="manual",
            state="active",
            owner_label="GUI launch",
            engine_name="direct_launch",
            details={"source": "control_launch"},
            owner_pid=owner_pid,
        )
        return {
            "profile_name": normalized_name,
            "returncode": int(getattr(result, "returncode", 0) or 0),
            "stdout": str(getattr(result, "stdout", "") or ""),
            "stderr": str(getattr(result, "stderr", "") or ""),
            "process_count": len(processes),
            "owner_pid": owner_pid,
        }

    def close(self, profile_name: str) -> Dict:
        config = normalize_config(load_app_config(self.config_path))
        normalized_name = str(profile_name or "").strip()
        if not normalized_name:
            raise ValueError("profile_name is required")
        processes = get_chromium_processes_for_profile(config, normalized_name)
        terminated = terminate_chromium_processes(processes, logger=None) if processes else 0
        self.session_manager._clear_profile_occupancy(normalized_name, event_state="released")
        return {
            "profile_name": normalized_name,
            "terminated_process_count": int(terminated or 0),
        }


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


def get_control_log_path(config_path: str) -> str:
    root = os.path.dirname(os.path.abspath(os.path.expanduser(str(config_path or "").strip() or get_default_config_path())))
    return os.path.join(root, CONTROL_LOG_FILE_NAME)


def _load_strategy_config(session_manager, config_path: str) -> Dict:
    loader = getattr(session_manager, "_load_config", None)
    if callable(loader):
        return normalize_config(loader())
    try:
        return normalize_config(load_app_config(config_path))
    except Exception:
        return normalize_config({})


def _call_with_optional_reconcile(target, method_name: str, *, reconcile_occupancy: bool, **kwargs):
    method = getattr(target, method_name)
    try:
        signature = inspect.signature(method)
        if "reconcile_occupancy" not in signature.parameters:
            return method(**kwargs)
    except (TypeError, ValueError):
        pass
    try:
        return method(**kwargs, reconcile_occupancy=reconcile_occupancy)
    except TypeError as exc:
        if "reconcile_occupancy" not in str(exc):
            raise
        return method(**kwargs)


def _call_with_optional_runtime_flags(target, method_name: str, **kwargs):
    method = getattr(target, method_name)
    try:
        return method(**kwargs)
    except TypeError as exc:
        filtered = dict(kwargs)
        changed = False
        for key in ("include_external_processes", "include_mirror_status"):
            if key in filtered:
                filtered.pop(key, None)
                changed = True
        if not changed:
            raise
        try:
            return method(**filtered)
        except TypeError:
            raise exc


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
        worker_executable = ""
        for candidate in candidates:
            if os.path.exists(candidate):
                worker_executable = candidate
                break
        if not worker_executable:
            raise FileNotFoundError(
                "ChromiumMcpWorker companion executable not found. Checked: "
                + ", ".join(candidates)
            )
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
        self._worker_listening_cache = False
        self._worker_listening_cache_at = 0.0
        self._worker_listening_cache_ttl_seconds = 2.0
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
            occupancy_entries = list_profile_occupancy_entries(tolerate_lock_timeout=True)
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

    def get_status(self, include_session_details: bool = True) -> Dict:
        with self._lock:
            self._cleanup_dead_process_locked()
            self._reconcile_active_browser_session_ids_locked()
            worker_pid = self._process.pid if self._process is not None else None
            worker_running = self._process is not None and self._process.poll() is None
            worker_listening = self._get_worker_listening_cached_locked()
            public_endpoint = f"http://{self.public_host}:{self.public_port}{self.public_path}"
            worker_endpoint = f"http://{self.worker_host}:{self.worker_port}{self.public_path}"
            now_ts = time.time()
            idle_seconds = 0
            if self._last_activity_at > 0:
                idle_seconds = max(0, int(now_ts - self._last_activity_at))
            daemon_session_ids: list[str] = []
            if include_session_details:
                daemon_sessions = _call_with_optional_reconcile(
                    self.session_manager,
                    "list_sessions",
                    reconcile_occupancy=False,
                )
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
            self._worker_listening_cache = False
            self._worker_listening_cache_at = time.time()
            return False
        listening = can_connect(self.worker_host, self.worker_port)
        self._worker_listening_cache = bool(listening)
        self._worker_listening_cache_at = time.time()
        return listening

    def _get_worker_listening_cached_locked(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            self._worker_listening_cache = False
            self._worker_listening_cache_at = time.time()
            return False
        now_ts = time.time()
        if (now_ts - self._worker_listening_cache_at) <= self._worker_listening_cache_ttl_seconds:
            return bool(self._worker_listening_cache)
        listening = can_connect(self.worker_host, self.worker_port, timeout=0.05)
        self._worker_listening_cache = bool(listening)
        self._worker_listening_cache_at = now_ts
        return listening

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
    control_token: str,
    idle_timeout_seconds: int,
    worker_policy: str,
    warmup_seconds: float = 0.0,
) -> FastAPI:
    public_path = normalize_path(path)
    daemon_pid = os.getpid()
    daemon_instance_id = f"{daemon_pid}-{uuid.uuid4().hex[:8]}"
    daemon_started_at = time.time()
    housekeeping_lock = threading.Lock()
    housekeeping_last_run_at = 0.0
    housekeeping_interval_seconds = 30.0
    runtime_status_cache: Dict[str, object] = {}
    runtime_status_cache_at = 0.0
    runtime_status_cache_ttl_seconds = 2.0

    def maybe_run_housekeeping(force: bool = False) -> None:
        nonlocal housekeeping_last_run_at
        now_ts = time.time()
        if not force and (now_ts - housekeeping_last_run_at) < housekeeping_interval_seconds:
            return
        with housekeeping_lock:
            now_ts = time.time()
            if not force and (now_ts - housekeeping_last_run_at) < housekeeping_interval_seconds:
                return
            session_manager.reconcile_stale_profile_occupancy()
            session_manager.reap_expired_profile_occupancy()
            housekeeping_last_run_at = now_ts

    def get_runtime_status_cached(*, include_external_processes: bool = True, include_mirror_status: bool = True) -> Dict:
        nonlocal runtime_status_cache, runtime_status_cache_at
        now_ts = time.time()
        cache_key = f"{int(bool(include_external_processes))}:{int(bool(include_mirror_status))}"
        if (
            runtime_status_cache
            and runtime_status_cache.get("cache_key") == cache_key
            and (now_ts - runtime_status_cache_at) <= runtime_status_cache_ttl_seconds
        ):
            cached_payload = runtime_status_cache.get("payload", {})
            return dict(cached_payload) if isinstance(cached_payload, dict) else {}
        payload = _call_with_optional_runtime_flags(
            session_manager,
            "get_runtime_status_snapshot",
            include_external_processes=include_external_processes,
            include_mirror_status=include_mirror_status,
        )
        runtime_status_cache = {"cache_key": cache_key, "payload": dict(payload)}
        runtime_status_cache_at = now_ts
        return payload
    session_manager = SessionManager(config_path=config_path)
    manual_profile_manager = ManualProfileRuntimeManager(session_manager, config_path)
    keepalive_manager = KeepaliveJobManager(config_path)
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

    _require_mcp_auth = bool(api_token)
    _control_token = str(control_token or "").strip()
    _require_control_auth = bool(_control_token)

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
        return False

    def _is_control_path(path_text: str) -> bool:
        path_text = str(path_text or "").strip()
        return path_text == "/_control" or path_text.startswith("/_control/")

    def _is_mcp_scoped_path(path_text: str) -> bool:
        path_text = str(path_text or "").strip()
        if _is_control_path(path_text):
            return False
        if path_text in {"/", "/health"}:
            return False
        if path_text.startswith("/_daemon/"):
            return True
        if path_text == public_path or path_text.startswith(f"{public_path.rstrip('/')}/"):
            return True
        return False

    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request_path = str(request.url.path or "").strip()
            auth_header = (request.headers.get("authorization") or "").strip()
            token_value = ""
            if _is_control_path(request_path):
                if _require_control_auth:
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
                    if not secrets.compare_digest(token_value, _control_token):
                        return JSONResponse(
                            {"error": "Authentication required", "detail": "Invalid control API token"},
                            status_code=401,
                        )
            elif _is_mcp_scoped_path(request_path) and _require_mcp_auth:
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
                mcp_valid = secrets.compare_digest(token_value, api_token)
                control_valid = bool(_control_token) and secrets.compare_digest(token_value, _control_token)
                if not (mcp_valid or control_valid):
                    return JSONResponse(
                        {"error": "Authentication required", "detail": "Invalid API token"},
                        status_code=401,
                    )
            if (
                not _daemon_ready()
                and request_path not in {"/", "/health", "/_daemon/status", "/_control/status", "/_control/ping"}
                and not request_path.startswith("/_daemon/status")
                and not request_path.startswith("/_control/status")
                and not request_path.startswith("/_control/ping")
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
            try:
                keepalive_manager.request_stop()
            except Exception:
                pass
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
            maybe_run_housekeeping(force=False)
            status = worker_manager.get_status()
            status["daemon_pid"] = daemon_pid
            status["daemon_instance_id"] = daemon_instance_id
            status["daemon_ready"] = _daemon_ready()
            status["warmup_remaining_ms"] = max(0, int((effective_warmup_seconds - (time.time() - daemon_started_at)) * 1000))
            status["server_status"] = get_runtime_status_cached(
                include_external_processes=False,
                include_mirror_status=False,
            )
            status["status_build_ms"] = int((time.perf_counter() - started_at) * 1000)
            return status
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/status")
    def control_status() -> Dict:
        try:
            maybe_run_housekeeping(force=False)
            return {
                "ok": True,
                "surface": "control",
                "daemon_pid": daemon_pid,
                "daemon_instance_id": daemon_instance_id,
                "daemon_ready": _daemon_ready(),
                "warmup_remaining_ms": max(
                    0,
                    int((effective_warmup_seconds - (time.time() - daemon_started_at)) * 1000),
                ),
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/ping")
    def control_ping() -> Dict:
        return {
            "ok": True,
            "surface": "control",
            "daemon_pid": daemon_pid,
            "daemon_instance_id": daemon_instance_id,
            "daemon_ready": _daemon_ready(),
            "time": now_text(),
        }

    @app.get("/_control/dashboard")
    def control_dashboard() -> Dict:
        started_at = time.perf_counter()
        try:
            maybe_run_housekeeping(force=False)
            profiles = _call_with_optional_reconcile(
                session_manager,
                "list_profiles",
                reconcile_occupancy=False,
                include_external_processes=False,
                include_mirror_validation=False,
            )
            sessions = _call_with_optional_reconcile(
                session_manager,
                "list_sessions",
                reconcile_occupancy=False,
            )
            server_status = get_runtime_status_cached(
                include_external_processes=False,
                include_mirror_status=False,
            )
            busy_profiles = [item for item in profiles if str(item.get("busy_state", "idle")) != "idle"]
            return {
                "ok": True,
                "surface": "control",
                "daemon_pid": daemon_pid,
                "daemon_instance_id": daemon_instance_id,
                "daemon_ready": _daemon_ready(),
                "profile_count": len(profiles),
                "busy_profile_count": len(busy_profiles),
                "active_session_count": len(sessions),
                "profiles": profiles,
                "sessions": sessions,
                "server_status": server_status,
                "events": session_manager.list_recent_occupancy_events(limit=50),
                "status_build_ms": int((time.perf_counter() - started_at) * 1000),
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/profiles")
    def control_profiles(include_runtime_snapshot: bool = False) -> Dict:
        started_at = time.perf_counter()
        try:
            maybe_run_housekeeping(force=False)
            include_heavy_profile_details = bool(include_runtime_snapshot)
            payload = {
                "ok": True,
                "surface": "control",
                "profiles": _call_with_optional_reconcile(
                    session_manager,
                    "list_profiles",
                    reconcile_occupancy=include_heavy_profile_details,
                    include_external_processes=include_heavy_profile_details,
                    include_mirror_validation=include_heavy_profile_details,
                ),
                "status_build_ms": int((time.perf_counter() - started_at) * 1000),
            }
            if bool(include_runtime_snapshot):
                payload["server_status"] = get_runtime_status_cached()
            return payload
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/profiles/{profile_name}")
    def control_profile_status(profile_name: str, include_runtime_snapshot: bool = False) -> Dict:
        try:
            maybe_run_housekeeping(force=False)
            include_heavy_profile_details = bool(include_runtime_snapshot)
            profile_payload = session_manager.get_profile_status_with_options(
                profile_name,
                include_external_processes=include_heavy_profile_details,
                include_mirror_validation=include_heavy_profile_details,
            )
            payload = {
                "ok": True,
                "surface": "control",
                "profile": profile_payload,
            }
            if bool(include_runtime_snapshot):
                payload["server_status"] = get_runtime_status_cached()
            return payload
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/sessions")
    def control_sessions() -> Dict:
        try:
            maybe_run_housekeeping(force=False)
            return {
                "ok": True,
                "surface": "control",
                "sessions": session_manager.list_sessions(reconcile_occupancy=False),
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/events")
    def control_events(limit: int = 100) -> Dict:
        bounded_limit = max(1, min(1000, int(limit or 100)))
        try:
            maybe_run_housekeeping(force=False)
            return {
                "ok": True,
                "surface": "control",
                "events": session_manager.list_recent_occupancy_events(limit=bounded_limit),
                "limit": bounded_limit,
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/keepalive")
    def control_keepalive_status() -> Dict:
        config = normalize_config(load_app_config(config_path))
        keepalive = dict(config.get("keepalive", {}) if isinstance(config.get("keepalive", {}), dict) else {})
        profiles = list(config.get("profiles", []) if isinstance(config.get("profiles", []), list) else [])
        enabled_profiles = []
        for item in profiles:
            if not isinstance(item, dict):
                continue
            profile_name = str(item.get("profile_name", "") or "").strip()
            if profile_name and bool(item.get("keepalive_enabled", False)):
                enabled_profiles.append(
                    {
                        "profile_name": profile_name,
                        "sites": dict(item.get("keepalive_sites", {}) if isinstance(item.get("keepalive_sites", {}), dict) else {}),
                        "last_keepalive_at": str(item.get("last_keepalive_at", "") or ""),
                        "last_keepalive_status": str(item.get("last_keepalive_status", "") or ""),
                        "last_keepalive_message": str(item.get("last_keepalive_message", "") or ""),
                    }
                )
        return {
            "ok": True,
            "surface": "control",
            "keepalive": keepalive,
            "enabled_profiles": enabled_profiles,
            "runtime": keepalive_manager.get_status(),
        }

    @app.post("/_control/profiles/{profile_name}/launch")
    async def control_launch_profile(profile_name: str) -> Dict:
        try:
            result = await asyncio.to_thread(manual_profile_manager.launch, unquote(profile_name))
            return {"ok": True, "surface": "control", "result": result}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_control/profiles/{profile_name}/close")
    async def control_close_profile(profile_name: str) -> Dict:
        try:
            result = await asyncio.to_thread(manual_profile_manager.close, unquote(profile_name))
            return {"ok": True, "surface": "control", "result": result}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_control/keepalive/run")
    async def control_keepalive_run(request: Request) -> Dict:
        try:
            body = await request.json()
        except Exception:
            body = {}
        payload = body if isinstance(body, dict) else {}
        selected_profiles = payload.get("selected_profiles", [])
        if not isinstance(selected_profiles, list):
            raise HTTPException(status_code=400, detail="selected_profiles must be a list")
        source = str(payload.get("source", "") or "manual").strip() or "manual"
        try:
            result = await asyncio.to_thread(
                keepalive_manager.start,
                selected_profiles=[str(item).strip() for item in selected_profiles if str(item).strip()],
                source=source,
            )
            return {"ok": True, "surface": "control", "runtime": result}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_control/keepalive/stop")
    def control_keepalive_stop() -> Dict:
        try:
            return {"ok": True, "surface": "control", "runtime": keepalive_manager.request_stop()}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.post("/_control/service/worker/start")
    def control_worker_start() -> Dict:
        try:
            return {"ok": True, "surface": "control", "result": worker_manager.ensure_worker_running()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/_control/service/worker/stop")
    def control_worker_stop() -> Dict:
        try:
            return {"ok": True, "surface": "control", "result": worker_manager.stop_worker("control_api_stop")}
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_control/logs")
    def control_logs(limit: int = 200) -> Dict:
        bounded_limit = max(1, min(1000, int(limit or 200)))
        log_path = get_control_log_path(config_path)
        items = read_recent_jsonl_events(log_path, limit=bounded_limit)
        return {
            "ok": True,
            "surface": "control",
            "log_path": log_path,
            "limit": bounded_limit,
            "items": items,
        }

    @app.get("/_control/log-settings")
    def control_log_settings() -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config

        config = normalize_config(load_app_config(config_path))
        logging_settings = dict(config.get("logging", {}) if isinstance(config.get("logging", {}), dict) else {})
        if "level" not in logging_settings:
            logging_settings["level"] = "info"
        if "retention_days" not in logging_settings:
            logging_settings["retention_days"] = 7
        return {
            "ok": True,
            "surface": "control",
            "logging": logging_settings,
        }

    @app.put("/_control/log-settings")
    async def control_update_log_settings(request: Request) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config, save_app_config

        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        config = normalize_config(load_app_config(config_path))
        config.setdefault("logging", {})
        level = str(payload.get("level", config["logging"].get("level", "info"))).strip().lower() or "info"
        if level not in {"debug", "info", "warning", "error"}:
            raise HTTPException(status_code=400, detail="unsupported log level")
        retention_days = max(1, min(365, int(payload.get("retention_days", config["logging"].get("retention_days", 7)) or 7)))
        config["logging"]["level"] = level
        config["logging"]["retention_days"] = retention_days
        config = save_app_config(config, config_path)
        append_jsonl_event(
            get_control_log_path(config_path),
            {
                "time": now_text(),
                "source": "control",
                "level": "info",
                "event": "log_settings_updated",
                "message": "control log settings updated",
                "detail": {"level": level, "retention_days": retention_days},
            },
        )
        return {
            "ok": True,
            "surface": "control",
            "logging": dict(config.get("logging", {})),
        }

    @app.get("/_control/plugins")
    def control_plugins() -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config

        config = normalize_config(load_app_config(config_path))
        profile_plugin_map = dict(config.get("profile_plugins", {}) if isinstance(config.get("profile_plugins", {}), dict) else {})
        return {
            "ok": True,
            "surface": "control",
            "plugins": get_keepalive_plugin_records(config),
            "profile_plugin_map": profile_plugin_map,
        }

    @app.post("/_control/plugins/preview")
    async def control_preview_plugin(request: Request) -> Dict:
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        site_id = str(payload.get("site_id", "") or "").strip()
        source_text = str(payload.get("source_text", "") or "")
        if not site_id:
            raise HTTPException(status_code=400, detail="site_id is required")
        metadata = inspect_keepalive_plugin_source(site_id, source_text)
        return {
            "ok": True,
            "surface": "control",
            "metadata": metadata,
        }

    @app.post("/_control/plugins")
    async def control_create_plugin(request: Request) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config

        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        config = normalize_config(load_app_config(config_path))
        site_id = str(payload.get("site_id", "") or "").strip()
        if not site_id:
            raise HTTPException(status_code=400, detail="site_id is required")
        source_text = str(payload.get("source_text", "") or "")
        if not source_text.strip():
            source_text = build_keepalive_plugin_template(
                site_id,
                display_name=str(payload.get("display_name", "") or ""),
                home_url=str(payload.get("home_url", "") or ""),
            )
        save_result = save_keepalive_plugin_source(site_id, source_text, config)
        return {
            "ok": True,
            "surface": "control",
            "plugin": save_result,
        }

    @app.put("/_control/plugins/{plugin_id}")
    async def control_update_plugin(plugin_id: str, request: Request) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config, save_app_config, migrate_keepalive_site_id_references

        normalized_plugin_id = str(unquote(plugin_id) or "").strip()
        if not normalized_plugin_id:
            raise HTTPException(status_code=400, detail="plugin_id is required")
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        source_text = str(payload.get("source_text", "") or "")
        if not source_text.strip():
            raise HTTPException(status_code=400, detail="source_text is required")
        config = normalize_config(load_app_config(config_path))
        save_result = save_keepalive_plugin_source(normalized_plugin_id, source_text, config)
        previous_site_id = str(save_result.get("previous_site_id", "") or normalized_plugin_id)
        current_site_id = str(save_result.get("site_id", "") or previous_site_id)
        if previous_site_id and current_site_id and previous_site_id != current_site_id:
            config, _ = migrate_keepalive_site_id_references(config, previous_site_id, current_site_id)
            config = save_app_config(config, config_path)
        return {
            "ok": True,
            "surface": "control",
            "plugin": save_result,
        }

    @app.delete("/_control/plugins/{plugin_id}")
    def control_delete_plugin(plugin_id: str) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config

        normalized_plugin_id = str(unquote(plugin_id) or "").strip()
        if not normalized_plugin_id:
            raise HTTPException(status_code=400, detail="plugin_id is required")
        config = normalize_config(load_app_config(config_path))
        path = delete_keepalive_plugin_source(normalized_plugin_id, config)
        return {
            "ok": True,
            "surface": "control",
            "plugin_id": normalized_plugin_id,
            "path": path,
        }

    @app.get("/_control/profiles/{profile_name}/plugins")
    def control_profile_plugins(profile_name: str) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config

        config = normalize_config(load_app_config(config_path))
        normalized_profile_name = str(unquote(profile_name) or "").strip()
        profile_plugin_map = dict(config.get("profile_plugins", {}) if isinstance(config.get("profile_plugins", {}), dict) else {})
        selected = profile_plugin_map.get(normalized_profile_name, [])
        if not isinstance(selected, list):
            selected = []
        return {
            "ok": True,
            "surface": "control",
            "profile_name": normalized_profile_name,
            "plugin_ids": [str(item).strip() for item in selected if str(item).strip()],
        }

    @app.put("/_control/profiles/{profile_name}/plugins")
    async def control_update_profile_plugins(profile_name: str, request: Request) -> Dict:
        from chromium_advanced.chromium_profile_lib import load_app_config, normalize_config, save_app_config

        normalized_profile_name = str(unquote(profile_name) or "").strip()
        if not normalized_profile_name:
            raise HTTPException(status_code=400, detail="profile_name is required")
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
        plugin_ids = payload.get("plugin_ids", [])
        if not isinstance(plugin_ids, list):
            raise HTTPException(status_code=400, detail="plugin_ids must be a list")
        normalized_plugin_ids = []
        for item in plugin_ids:
            value = str(item or "").strip()
            if value and value not in normalized_plugin_ids:
                normalized_plugin_ids.append(value)
        config = normalize_config(load_app_config(config_path))
        config.setdefault("profile_plugins", {})
        config["profile_plugins"][normalized_profile_name] = normalized_plugin_ids
        config = save_app_config(config, config_path)
        append_jsonl_event(
            get_control_log_path(config_path),
            {
                "time": now_text(),
                "source": "control",
                "level": "info",
                "event": "profile_plugins_updated",
                "message": "profile plugin associations updated",
                "detail": {"profile_name": normalized_profile_name, "plugin_ids": normalized_plugin_ids},
            },
        )
        return {
            "ok": True,
            "surface": "control",
            "profile_name": normalized_profile_name,
            "plugin_ids": normalized_plugin_ids,
        }

    @app.get("/_daemon/profiles")
    def daemon_profiles() -> Dict:
        started_at = time.perf_counter()
        try:
            maybe_run_housekeeping(force=False)
            return {
                "daemon_pid": daemon_pid,
                "daemon_instance_id": daemon_instance_id,
                "daemon_ready": _daemon_ready(),
                "profiles": session_manager.list_profiles(reconcile_occupancy=False),
                "events": session_manager.list_recent_occupancy_events(limit=50),
                "status_build_ms": int((time.perf_counter() - started_at) * 1000),
            }
        except Exception as exc:
            raise _as_http_error(exc) from exc

    @app.get("/_daemon/profiles/{profile_name}")
    def daemon_profile_status(profile_name: str) -> Dict:
        maybe_run_housekeeping(force=False)
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
            maybe_run_housekeeping(force=True)
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
        requested_engine_name = str((body or {}).get("engine", "") or "").strip()
        owner_label = str((body or {}).get("owner_label", "") or "automation").strip() or "automation"
        reuse_existing = bool((body or {}).get("reuse_existing", True))
        runtime_options = dict((body or {}).get("runtime_options") or {})
        if "task_scope" in body and not runtime_options.get("task_scope"):
            runtime_options["task_scope"] = str((body or {}).get("task_scope", "") or "").strip()
        heartbeat_timeout_seconds = int((body or {}).get("heartbeat_timeout_seconds", 180) or 180)
        runtime_options.setdefault("heartbeat_timeout_seconds", heartbeat_timeout_seconds)
        try:
            config = _load_strategy_config(session_manager, config_path)
            strategy = resolve_engine_strategy(
                config,
                explicit_engine_name=requested_engine_name,
                action_name="acquire",
                runtime_options=runtime_options,
            )
            result = await asyncio.to_thread(
                session_manager.start_session,
                profile_name=profile_name,
                reuse_existing=reuse_existing,
                engine_name=strategy.resolved_engine_name,
                scene_type="automation",
                owner_label=owner_label,
                runtime_options=runtime_options,
            )
            result["engine_strategy"] = strategy.to_dict()
            await asyncio.to_thread(
                session_manager.refresh_profile_lease,
                profile_name,
                scene_type="automation",
                owner_label=owner_label,
                engine_name=result.get("engine_name", ""),
                session_id=result.get("session_id", ""),
                owner_pid=os.getpid(),
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                details={
                    "source": "daemon_automation_acquire",
                    "runtime_options": runtime_options,
                    "engine_strategy": strategy.to_dict(),
                },
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
        raw_args = (body or {}).get("args")
        raw_params = (body or {}).get("params")
        if isinstance(raw_args, dict):
            args = dict(raw_args)
        elif isinstance(raw_params, dict):
            args = dict(raw_params)
        else:
            args = {}
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
            pipeline = ActionPipeline(browser_session)
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
                    "action_pipeline": {
                        "action_name": action,
                        "pipeline_version": 1,
                        "engine_name": getattr(browser_session, "engine_name", ""),
                    },
                }
            direct_dispatch = {
                "list_candidates": lambda: browser_session.list_candidates(
                    target=str(args.get("target", "") or ""),
                    by=str(args.get("by", "css") or "css"),
                    text_filter=str(args.get("text_filter", "") or ""),
                    limit=int(args.get("limit", 25) or 25),
                    include_boxes=bool(args.get("include_boxes", True)),
                    tab_id=str(args.get("tab_id", "") or ""),
                ),
                "get_page_errors": lambda: browser_session.get_page_errors(
                    tab_id=str(args.get("tab_id", "") or ""),
                    limit=int(args.get("limit", 100) or 100),
                ),
                "clear_debug_buffers": lambda: browser_session.clear_debug_buffers(
                    tab_id=str(args.get("tab_id", "") or ""),
                ),
                "verify_text": lambda: browser_session.verify_text(
                    str(args.get("text", "") or ""),
                ),
                "verify_dialog": lambda: browser_session.verify_dialog(
                    accessible_name=str(args.get("accessible_name", "") or ""),
                    text=str(args.get("text", "") or ""),
                ),
                "verify_element": lambda: browser_session.verify_element(
                    role=str(args.get("role", "") or ""),
                    accessible_name=str(args.get("accessible_name", "") or ""),
                ),
            }
            if pipeline.supports(action):
                result = pipeline.execute(action, args)
                if isinstance(result, dict):
                    result.setdefault(
                        "action_pipeline",
                        {
                            "action_name": action,
                            "pipeline_version": 1,
                            "engine_name": getattr(browser_session, "engine_name", ""),
                        },
                    )
                return result
            if action in direct_dispatch:
                result = direct_dispatch[action]()
                if isinstance(result, dict):
                    result.setdefault(
                        "action_pipeline",
                        {
                            "action_name": action,
                            "pipeline_version": 1,
                            "engine_name": getattr(browser_session, "engine_name", ""),
                            "dispatch_mode": "daemon_direct_fallback",
                        },
                    )
                return result
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
    parser.add_argument("--control-token", default=os.environ.get("CHROMIUM_CONTROL_API_TOKEN", "").strip())
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
    if not args.control_token:
        configured_control_token = str(config.get("control", {}).get("api_token", "")).strip()
        if not configured_control_token:
            configured_control_token = resolve_control_api_token(config)
            config.setdefault("control", {})
            config["control"]["api_token"] = configured_control_token
            config = save_app_config(config, config_path)
        args.control_token = configured_control_token
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
            control_token=str(args.control_token or "").strip(),
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
