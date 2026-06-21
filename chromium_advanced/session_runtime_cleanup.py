from __future__ import annotations

import os
from typing import Dict

import psutil


def terminate_runtime_processes(
    *,
    runtime_root: str,
    launch_pid: int = 0,
    normalize_fs_path,
) -> None:
    runtime_root = str(runtime_root or "").strip()
    launch_pid = int(launch_pid or 0)
    if not runtime_root and launch_pid <= 0:
        return
    try:
        runtime_norm = normalize_fs_path(runtime_root)
    except Exception:
        runtime_norm = ""

    current_pid = os.getpid()
    targets: Dict[int, psutil.Process] = {}
    if launch_pid > 0:
        try:
            root_proc = psutil.Process(launch_pid)
            targets[root_proc.pid] = root_proc
            for child in root_proc.children(recursive=True):
                targets[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == current_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            command_line = " ".join(str(item) for item in cmdline)
            if not command_line:
                continue
            if runtime_norm and runtime_norm not in os.path.normcase(command_line):
                continue
            targets[proc.pid] = proc
            for child in proc.children(recursive=True):
                targets[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue

    if not targets:
        return
    alive = list(targets.values())
    for _ in range(2):
        processes = [proc for proc in alive if proc.is_running()]
        if not processes:
            return
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(processes, timeout=3)
        if not alive:
            return
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)
