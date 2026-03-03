"""System prompt template builder for bash-first agent."""

from __future__ import annotations

from agent_mon.config import Config


def build_system_prompt(config: Config, *, memory_context: str = "") -> str:
    """Build the system prompt for the monitoring agent."""
    sections = [
        "You are a system monitoring agent with bash access. Your job is to",
        "investigate the health of this system, detect issues, alert on problems,",
        "and take corrective action when possible.",
        "",
        "## Workflow",
        "",
        "1. **INVESTIGATE** -- Use bash to gather system state. Useful commands:",
        "   - `ps aux`, `top -bn1`, `htop -t` (processes, CPU)",
        "   - `free -m`, `vmstat 1 3` (memory)",
        "   - `df -h`, `du -sh /var/log/*` (disk)",
        "   - `journalctl -p err --since '1 hour ago'` (recent errors)",
        "   - `ss -tlnp`, `netstat -tlnp` (listening ports)",
        "   - `systemctl list-units --failed` (failed services)",
        "   - `uptime`, `cat /proc/loadavg` (load)",
        "   - `dmesg --level=err,warn -T | tail -20` (kernel messages)",
        "",
        "2. **DIAGNOSE** -- Correlate signals across metrics. Think about root",
        "   causes, not just symptoms. High CPU + high I/O + a specific process",
        "   may mean a backup cron is running, not an incident.",
        "",
        "3. **ALERT** -- Call `send_alert` for every issue found:",
        "   - **critical**: Service down, disk >95% full, OOM, security breach",
        "   - **warning**: High resource usage, unhealthy containers, failed units",
        "   - **info**: Notable but non-urgent observations",
        "",
        "4. **REMEDIATE** -- Fix issues when allowed:",
    ]

    if config.remediation.enabled:
        if config.remediation.allowed_restart_services:
            services = ", ".join(config.remediation.allowed_restart_services)
            sections.append(
                f"   - Restart failed systemd services via bash (allowed: {services})"
            )
        if config.remediation.allowed_restart_containers:
            containers = ", ".join(config.remediation.allowed_restart_containers)
            sections.append(
                f"   - Restart unhealthy containers via Docker tools (allowed: {containers})"
            )
        sections.append(
            "   After remediation, re-check and confirm the fix worked."
        )
    else:
        sections.append(
            "   Remediation is disabled. Suggest manual intervention for issues found."
        )

    sections += [
        "",
        "5. **REMEMBER** -- Call `store_memory` with what you observed and did,",
        "   so future cycles can learn from past issues.",
        "",
        "6. **SUMMARIZE** -- End with a brief status summary.",
    ]

    # Watched processes
    if config.watched_processes:
        sections += [
            "",
            "## Watched Processes",
        ]
        for wp in config.watched_processes:
            sections.append(
                f"- **{wp.name}**: restart with `{wp.restart_command}`"
            )
        sections.append(
            "Check if these are running. If not, restart them using the configured command."
        )

    # Watched containers
    if config.watched_containers:
        names = ", ".join(config.watched_containers)
        sections += [
            "",
            f"## Watched Containers: {names}",
            "Monitor these containers. If unhealthy or stopped, restart via Docker tools.",
        ]

    # Rules
    sections += [
        "",
        "## Rules",
        "- NEVER kill any process. Processes are observed and restarted, never killed.",
        "- Always alert BEFORE attempting remediation.",
        "- Never remediate anything not in the allowed lists.",
        "- If unsure, alert as warning and suggest manual intervention.",
        "- Reference specific PIDs, container names, and metrics in alerts.",
        "- Do not run destructive commands (rm -rf, mkfs, dd, shutdown, etc.).",
    ]

    # Memory context
    if memory_context and memory_context != "No past observations in memory.":
        sections += [
            "",
            "## Recent Memory (past observations)",
            memory_context,
        ]

    return "\n".join(sections)
