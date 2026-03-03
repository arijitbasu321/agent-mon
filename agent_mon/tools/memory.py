"""get_memory_info monitoring tool."""

from __future__ import annotations

import psutil


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_memory_info() -> str:
    """Collect memory and swap usage, plus top memory consumers."""
    lines = []

    mem = psutil.virtual_memory()
    lines.append(f"RAM: {mem.percent}% used")
    lines.append(f"  Total: {_fmt_bytes(mem.total)}")
    lines.append(f"  Used: {_fmt_bytes(mem.used)}")
    lines.append(f"  Available: {_fmt_bytes(mem.available)}")

    swap = psutil.swap_memory()
    if swap.total > 0:
        lines.append(f"Swap: {swap.percent}% used")
        lines.append(f"  Total: {_fmt_bytes(swap.total)}")
        lines.append(f"  Used: {_fmt_bytes(swap.used)}")
    else:
        lines.append("Swap: not configured")

    lines.append("Top memory processes:")
    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_percent", "memory_info"]):
        try:
            info = proc.info
            if info and info.get("memory_percent") is not None:
                procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    procs.sort(key=lambda p: p.get("memory_percent", 0), reverse=True)
    for p in procs[:5]:
        rss = p.get("memory_info")
        rss_str = f" (RSS: {_fmt_bytes(rss.rss)})" if rss else ""
        lines.append(
            f"  PID {p['pid']} ({p['name']}): {p['memory_percent']:.1f}%{rss_str}"
        )

    return "\n".join(lines)
