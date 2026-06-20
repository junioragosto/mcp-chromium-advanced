import json
import time
from pathlib import Path

import psutil


def find_processes() -> list[dict]:
    rows = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            name = str(proc.info.get("name") or "")
            exe = str(proc.info.get("exe") or "")
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(token in name for token in ["ChromiumProfileManager", "ChromiumMcpDaemon", "ChromiumMcpWorker"]):
            rows.append(
                {
                    "pid": proc.pid,
                    "name": name,
                    "exe": exe,
                    "cmdline": cmdline,
                    "process": proc,
                }
            )
    return rows


def sample_cpu(processes: list[dict], seconds: float = 3.0) -> list[dict]:
    live = []
    for row in processes:
        proc = row["process"]
        try:
            proc.cpu_percent(interval=None)
            live.append(row)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(seconds)
    rows = []
    for row in live:
        proc = row["process"]
        try:
            cpu = proc.cpu_percent(interval=None)
            mem = proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rows.append(
            {
                "pid": row["pid"],
                "name": row["name"],
                "exe": row["exe"],
                "cpu_percent": cpu,
                "rss_bytes": mem,
            }
        )
    return rows


def main() -> None:
    processes = find_processes()
    sampled = sample_cpu(processes)
    payload = {
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_window_seconds": 3.0,
        "process_count": len(sampled),
        "processes": sampled,
        "total_cpu_percent": round(sum(float(item.get("cpu_percent", 0.0) or 0.0) for item in sampled), 3),
        "total_rss_bytes": int(sum(int(item.get("rss_bytes", 0) or 0) for item in sampled)),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
