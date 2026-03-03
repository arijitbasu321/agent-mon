"""get_system_issues monitoring tool."""

from __future__ import annotations

import subprocess
import time

import psutil


def get_system_issues() -> str:
    """Check for common system-level problems."""
    lines = []

    # Uptime
    boot = psutil.boot_time()
    uptime_secs = time.time() - boot
    days = int(uptime_secs // 86400)
    hours = int((uptime_secs % 86400) // 3600)
    lines.append(f"Uptime: {days}d {hours}h (boot: {time.ctime(boot)})")

    # OOM killer events
    lines.append("OOM killer events:")
    try:
        result = subprocess.run(
            ["dmesg", "--level=err,warn"],
            capture_output=True, text=True, timeout=10,
        )
        oom_lines = [l for l in result.stdout.splitlines() if "oom" in l.lower() or "Out of memory" in l]
        for line in oom_lines[-10:]:
            lines.append(f"  {line.strip()}")
        if not oom_lines:
            lines.append("  None found")
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        lines.append("  dmesg not available")

    # Failed systemd units
    lines.append("Failed systemd units:")
    try:
        result = subprocess.run(
            ["systemctl", "--failed", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        failed = result.stdout.strip()
        if failed:
            for line in failed.splitlines():
                lines.append(f"  {line.strip()}")
        else:
            lines.append("  None")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        lines.append("  systemctl not available")

    # NTP sync
    lines.append("NTP sync status:")
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
        if "yes" in output.lower():
            lines.append("  Clock synchronized: yes")
        else:
            lines.append("  Clock NOT synchronized")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        lines.append("  timedatectl not available")

    return "\n".join(lines)
