"""get_disk_info monitoring tool."""

from __future__ import annotations

import psutil


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_disk_info() -> str:
    """Collect disk partition usage info."""
    lines = []

    partitions = psutil.disk_partitions()
    if not partitions:
        return "No disk partitions found."

    for part in partitions:
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            lines.append(f"{part.mountpoint} ({part.device}): access denied")
            continue

        flag = ""
        if usage.percent >= 85:
            flag = " [WARNING: HIGH USAGE]"
        if usage.percent >= 95:
            flag = " [CRITICAL: NEARLY FULL]"

        lines.append(
            f"{part.mountpoint} ({part.device}, {part.fstype}): "
            f"{usage.percent}% used "
            f"({_fmt_bytes(usage.used)} / {_fmt_bytes(usage.total)}, "
            f"{_fmt_bytes(usage.free)} free){flag}"
        )

    return "\n".join(lines)
