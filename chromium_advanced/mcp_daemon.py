import argparse
import asyncio
import multiprocessing
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlunsplit

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
    now_text,
)


HEALTHCHECK_TIMEOUT_SECONDS = 0.5
WORKER_START_TIMEOUT_SECONDS = 15.0
WATCHDOG_INTERVAL_SECONDS = 2.0


def normalize_path(path: str) -> str:
    text = str(path or "/mcp").strip() or "/mcp"
    if not text.startswith("/"):
        text = "/" + text
    return text


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
    if getattr(sys, "frozen", False):
        extension = ".exe" if platform.system() == "Windows" else ""
        worker_executable = os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)),
            f"ChromiumMcpWorker{extension}",
        )
        return [
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

    return [
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


class WorkerManager:
    def __init__(
        self,
        config_path: str,
        transport: str,
        public_host: str,
        public_port: int,
        public_path: str,
        worker_host: str,
        worker_port: int,
        log_level: str,
        idle_timeout_seconds: int,
    ):
        self.config_path = config_path
        self.transport = transport
        self.public_host = public_host
        self.public_port = int(public_port)
        self.public_path = normalize_path(public_path)
        self.worker_host = worker_host
        self.worker_port = int(worker_port)
        self.log_level = log_level
        self.idle_timeout_seconds = int(idle_timeout_seconds)

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
            command = build_worker_command(
                transport=self.transport,
                host=self.worker_host,
                port=self.worker_port,
                path=self.public_path,
                log_level=self.log_level,
                config_path=self.config_path,
            )
            print(f"[{now_text()}] [MCP-DAEMON] starting worker: {' '.join(command)}", flush=True)
            self._process = subprocess.Popen(
                command,
                cwd=get_project_root(),
                **get_hidden_subprocess_kwargs(),
            )
            self._last_start_at = time.time()
            self._last_request_at = self._last_start_at
            self._last_activity_at = self._last_start_at

        deadline = time.time() + WORKER_START_TIMEOUT_SECONDS
        while time.time() < deadline:
            with self._lock:
                self._cleanup_dead_process_locked()
                if self._is_worker_healthy_locked():
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
            worker_pid = self._process.pid if self._process is not None else None
            worker_running = self._process is not None and self._process.poll() is None
            worker_listening = can_connect(self.worker_host, self.worker_port)
            public_endpoint = f"http://{self.public_host}:{self.public_port}{self.public_path}"
            worker_endpoint = f"http://{self.worker_host}:{self.worker_port}{self.public_path}"
            now_ts = time.time()
            idle_seconds = 0
            if self._last_activity_at > 0:
                idle_seconds = max(0, int(now_ts - self._last_activity_at))
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
                "idle_timeout_seconds": self.idle_timeout_seconds,
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
        if exit_code != 0 and not self._last_error:
            self._last_error = f"worker exited unexpectedly with code {exit_code}"
        print(
            f"[{now_text()}] [MCP-DAEMON] worker exited: code={exit_code}",
            flush=True,
        )
        self._process = None
        self._last_stop_at = time.time()
        if not self._last_stop_reason:
            self._last_stop_reason = "unexpected_exit"

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
        print(
            f"[{now_text()}] [MCP-DAEMON] stopping worker: reason={self._last_stop_reason}",
            flush=True,
        )
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
        self._last_exit_code = process.poll()
        self._process = None
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
                if self._process is None:
                    continue
                if self._last_activity_at <= 0:
                    continue
                if (time.time() - self._last_activity_at) < self.idle_timeout_seconds:
                    continue
                self._stop_worker_locked("idle_timeout")


def create_daemon_app(
    config_path: str,
    host: str,
    port: int,
    path: str,
    transport: str,
    log_level: str,
    worker_port: int,
    idle_timeout_seconds: int,
) -> FastAPI:
    app = FastAPI(title="Chromium Advanced MCP Daemon")
    public_path = normalize_path(path)
    worker_manager = WorkerManager(
        config_path=config_path,
        transport=transport,
        public_host=host,
        public_port=port,
        public_path=public_path,
        worker_host="127.0.0.1",
        worker_port=worker_port,
        log_level=log_level,
        idle_timeout_seconds=idle_timeout_seconds,
    )

    @app.on_event("shutdown")
    def _shutdown_worker_manager() -> None:
        worker_manager.shutdown()

    @app.get("/")
    def root() -> Dict:
        status = worker_manager.get_status()
        return {
            "name": "chromium-advanced-mcp-daemon",
            "status": status,
        }

    @app.get("/health")
    def health() -> Dict:
        return {"ok": True, "time": now_text(), "status": worker_manager.get_status()}

    @app.get("/_daemon/status")
    def daemon_status() -> Dict:
        return worker_manager.get_status()

    @app.post("/_daemon/worker/start")
    def daemon_worker_start() -> Dict:
        try:
            return worker_manager.ensure_worker_running()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/_daemon/worker/stop")
    def daemon_worker_stop() -> Dict:
        return worker_manager.stop_worker("api_stop")

    async def proxy_to_worker(request: Request, tail: str = ""):
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
        print(
            (
                f"[{now_text()}] [MCP-DAEMON] proxy request: {request.method} {target_label} "
                f"from {client_host}:{client_port} session_id={'yes' if session_id else 'no'} "
                f"user_agent={user_agent or '-'} accept={accept or '-'}"
            ),
            flush=True,
        )

        # Prevent generic localhost probes from waking the worker up. In
        # streamable-http mode, session streams and session deletion require a
        # session ID, while session creation starts with POST.
        if transport == "streamable-http" and request.method.upper() != "POST" and not session_id:
            detail = {
                "error": "Missing session ID",
                "message": "This request must include an MCP session ID header.",
            }
            status_code = 400 if request.method.upper() in {"GET", "HEAD", "DELETE"} else 405
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
            print(
                f"[{now_text()}] [MCP-DAEMON] proxy request complete: {request.method} {target_label} from {client_host}:{client_port}",
                flush=True,
            )
            await backend_response.aclose()
            await client.aclose()

        if request.method.upper() == "HEAD":
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
    parser.add_argument("--config-path", default=os.environ.get("CHROMIUM_MCP_CONFIG_PATH", "").strip())
    args = parser.parse_args()

    config_path = args.config_path or os.environ.get("CHROMIUM_MCP_CONFIG_PATH", "").strip() or get_default_config_path()
    if not config_path:
        raise SystemExit("config path is required")

    app = create_daemon_app(
        config_path=config_path,
        host=str(args.host or "127.0.0.1").strip() or "127.0.0.1",
        port=int(args.port or 28888),
        path=str(args.path or "/mcp").strip() or "/mcp",
        transport=str(args.transport or "streamable-http").strip() or "streamable-http",
        log_level=str(args.log_level or "info").strip() or "info",
        worker_port=int(args.worker_port or 28889),
        idle_timeout_seconds=int(args.idle_timeout_seconds or 60),
    )
    uvicorn.run(app, host=str(args.host or "127.0.0.1"), port=int(args.port or 28888), log_level=str(args.log_level or "info"))


if __name__ == "__main__":
    main()
