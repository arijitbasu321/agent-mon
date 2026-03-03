"""get_security_info monitoring tool."""

from __future__ import annotations

import os
import subprocess

import psutil


AUTH_LOG = "/var/log/auth.log"


def get_security_info() -> str:
    """Basic security posture checks."""
    lines = []

    # Failed SSH login attempts
    lines.append("Failed SSH logins (last 50):")
    if os.path.exists(AUTH_LOG):
        try:
            with open(AUTH_LOG) as f:
                content = f.read()
            failed = [l for l in content.splitlines() if "Failed password" in l]
            for entry in failed[-50:]:
                lines.append(f"  {entry.strip()}")
            if not failed:
                lines.append("  None found")
        except PermissionError:
            lines.append("  Cannot read auth.log (permission denied)")
    else:
        lines.append("  auth.log not found")

    # Listening ports
    lines.append("Listening ports:")
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN":
                proc_name = "unknown"
                if conn.pid:
                    try:
                        proc_name = psutil.Process(conn.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                lines.append(
                    f"  {conn.laddr.ip}:{conn.laddr.port} ({proc_name})"
                )
    except (psutil.AccessDenied, OSError):
        lines.append("  Cannot enumerate ports (permission denied)")

    # Logged-in users
    lines.append("Logged-in users:")
    for user in psutil.users():
        lines.append(
            f"  {user.name} on {user.terminal} from {user.host}"
        )

    # Recent sudo commands
    lines.append("Recent sudo commands:")
    try:
        result = subprocess.run(
            ["grep", "sudo", "/var/log/auth.log"],
            capture_output=True, text=True, timeout=10,
        )
        sudo_lines = result.stdout.strip().splitlines()[-10:]
        for line in sudo_lines:
            lines.append(f"  {line.strip()}")
        if not sudo_lines:
            lines.append("  None found")
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        lines.append("  Not available")

    return "\n".join(lines)
