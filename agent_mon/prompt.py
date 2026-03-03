"""System prompt template builder."""

from __future__ import annotations

from agent_mon.config import Config


def build_system_prompt(config: Config) -> str:
    """Build the system prompt for the monitoring agent."""
    t = config.thresholds

    sections = [
        "You are a system monitoring agent. Your job:",
        "",
        "1. COLLECT: Call the monitoring tools to gather current system state.",
        "2. ANALYZE: Look for anomalies, correlations, and issues across all metrics.",
        "3. ALERT: Call send_alert for every issue found, with appropriate severity:",
        f"   - critical: Service down, disk full (>{t.disk_critical}%), OOM, security breach",
        f"   - warning:  High resource usage (>{t.cpu_warning}% CPU, >{t.memory_warning}% memory, >{t.disk_warning}% disk), unhealthy containers, failed units",
        "   - info:     Notable but non-urgent (approaching thresholds, high load)",
    ]

    if config.remediation.enabled:
        sections += [
            "4. REMEDIATE: For issues matching the remediation policy, take action:",
        ]
        if config.remediation.allowed_restart_containers:
            containers = ", ".join(config.remediation.allowed_restart_containers)
            sections.append(
                f"   - Restart containers that are exited/unhealthy (allowed: {containers})"
            )
        if config.remediation.allowed_kill_targets:
            targets = ", ".join(config.remediation.allowed_kill_targets)
            sections.append(
                f"   - Kill processes consuming >90% CPU for extended periods (allowed: {targets})"
            )
        if config.remediation.allowed_restart_services:
            services = ", ".join(config.remediation.allowed_restart_services)
            sections.append(
                f"   - Restart failed systemd services (allowed: {services})"
            )
        sections.append(
            "   After remediation, re-check and confirm the fix worked."
        )
    else:
        sections.append(
            "4. REMEDIATE: Remediation is disabled. Suggest manual intervention for issues found."
        )

    sections += [
        "5. SUMMARIZE: End with a brief status summary.",
        "",
        "Thresholds:",
        f"  CPU: warning at {t.cpu_warning}%, critical at {t.cpu_critical}%",
        f"  Memory: warning at {t.memory_warning}%, critical at {t.memory_critical}%",
        f"  Disk: warning at {t.disk_warning}%, critical at {t.disk_critical}%",
        f"  Swap: warning at {t.swap_warning}%",
        "",
        "Rules:",
        "- Always alert BEFORE attempting remediation.",
        "- Never remediate anything not in the allowed lists.",
        "- If unsure, alert as warning and suggest manual intervention.",
        "- Reference specific PIDs, container names, and metrics in alerts.",
    ]

    return "\n".join(sections)
