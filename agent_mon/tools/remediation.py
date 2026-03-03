"""Remediation tools: kill_process, restart_service."""

from __future__ import annotations

import os
import re
import signal as signal_mod
import subprocess

import psutil

from agent_mon.config import Config

SIGNAL_MAP = {
    "TERM": signal_mod.SIGTERM,
    "KILL": signal_mod.SIGKILL,
    "HUP": signal_mod.SIGHUP,
    "INT": signal_mod.SIGINT,
    "USR1": signal_mod.SIGUSR1,
    "USR2": signal_mod.SIGUSR2,
}

# Characters that are never valid in a service name
_DANGEROUS_CHARS = re.compile(r"[;|&$`\\\"'\n\r/]")


def kill_process(
    pid: int,
    signal: str = "TERM",
    *,
    config: Config,
    expected_create_time: float | None = None,
) -> str:
    """Send a signal to a process, with allow-list and TOCTOU guards."""
    if not config.remediation.enabled:
        return "Denied: remediation is disabled"

    # Resolve signal
    sig = SIGNAL_MAP.get(signal.upper())
    if sig is None:
        return f"Denied: unknown signal '{signal}'"

    # Look up the process
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return f"Process {pid} not found (no such process)"

    # First read: check if target is allowed
    proc_name = proc.name()
    if proc_name not in config.remediation.allowed_kill_targets:
        return f"Denied: process '{proc_name}' (PID {pid}) is not in allowed kill targets"

    create_time = proc.create_time()

    # TOCTOU guard: re-verify name
    current_name = proc.name()
    if current_name != proc_name:
        return (
            f"Denied: process name changed from '{proc_name}' to "
            f"'{current_name}' (PID may have been recycled)"
        )

    # TOCTOU guard: verify create_time hasn't changed (PID recycling)
    current_create_time = proc.create_time()
    if expected_create_time is not None and current_create_time != expected_create_time:
        return (
            f"Denied: process create_time mismatch for PID {pid} "
            f"(expected {expected_create_time}, got {current_create_time})"
        )

    os.kill(pid, sig)
    return f"Successfully sent {signal} to PID {pid} ({proc_name})"


def restart_service(
    service_name: str,
    *,
    config: Config,
) -> str:
    """Restart a systemd service, with allow-list guard."""
    if not config.remediation.enabled:
        return "Denied: remediation is disabled"

    # Input validation: reject dangerous characters
    if _DANGEROUS_CHARS.search(service_name):
        return f"Denied: invalid service name '{service_name}'"

    if service_name not in config.remediation.allowed_restart_services:
        return f"Denied: service '{service_name}' is not in allowed restart list"

    # Restart
    result = subprocess.run(
        ["systemctl", "restart", service_name],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode != 0:
        return (
            f"Failed to restart {service_name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    # Get new status
    status = subprocess.run(
        ["systemctl", "is-active", service_name],
        capture_output=True, text=True, timeout=10,
    )

    return f"Restarted {service_name}. Status: {status.stdout.strip()}"
