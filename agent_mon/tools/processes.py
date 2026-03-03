"""get_process_list monitoring tool."""

from __future__ import annotations

import psutil


def get_process_list(sort_by: str = "cpu", limit: int = 20) -> str:
    """List running processes sorted by CPU or memory usage."""
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "username", "cpu_percent", "memory_percent", "status", "create_time"]
    ):
        try:
            info = proc.info
            if info and info.get("pid") is not None:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    sort_key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
    procs.sort(key=lambda p: p.get(sort_key, 0) or 0, reverse=True)
    procs = procs[:limit]

    lines = [f"Top {len(procs)} processes (sorted by {sort_by}):"]
    for p in procs:
        status = p.get("status", "unknown")
        zombie_flag = " [ZOMBIE]" if status == "zombie" else ""
        defunct_flag = " [DEFUNCT]" if "defunct" in (p.get("name", "") or "").lower() else ""
        lines.append(
            f"  PID {p['pid']} ({p['name']}) "
            f"user={p.get('username', '?')} "
            f"cpu={p.get('cpu_percent', 0):.1f}% "
            f"mem={p.get('memory_percent', 0):.1f}% "
            f"status={status}{zombie_flag}{defunct_flag}"
        )

    return "\n".join(lines)
