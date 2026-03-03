"""get_cpu_info monitoring tool."""

from __future__ import annotations

import psutil


def get_cpu_info() -> str:
    """Collect CPU metrics: per-core usage, overall, load averages, top processes."""
    lines = []

    # Overall and per-core
    overall = psutil.cpu_percent(interval=1)
    per_core = psutil.cpu_percent(interval=0, percpu=True)
    lines.append(f"Overall CPU usage: {overall}%")
    lines.append(f"CPU cores: {psutil.cpu_count()}")
    for i, pct in enumerate(per_core):
        lines.append(f"  Core {i}: {pct}%")

    # Load averages
    load1, load5, load15 = psutil.getloadavg()
    lines.append(f"Load averages: 1m={load1}, 5m={load5}, 15m={load15}")

    # Top 5 CPU-consuming processes
    lines.append("Top CPU processes:")
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent"]):
        try:
            info = proc.info
            if info and info.get("cpu_percent") is not None:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    procs.sort(key=lambda p: p.get("cpu_percent", 0), reverse=True)
    for p in procs[:5]:
        lines.append(f"  PID {p['pid']} ({p['name']}): {p['cpu_percent']}%")

    return "\n".join(lines)
