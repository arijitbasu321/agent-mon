"""System prompt builders for orchestrator and investigator agents."""

from __future__ import annotations

from agent_mon.config import Config


def build_orchestrator_prompt(
    config: Config,
    *,
    last_cycle_summary: str = "",
    watched_context: str = "",
) -> str:
    """Build the system prompt for the monitoring orchestrator agent."""
    sections = [
        "You are a system monitoring agent. Your job is to keep this",
        "system healthy by scanning, investigating, and reporting issues.",
        "",
        "## Goal",
        "",
        "1. Use bash to scan system health: processes, memory, disk, load,",
        "   failed services, journal errors, container status, network.",
        "2. For each issue found, investigate it directly using bash.",
        "   Check logs, correlate metrics, diagnose root causes.",
        "3. After investigating all issues, send ONE consolidated `send_alert`",
        "   covering everything found and done this cycle.",
        "4. Store a cycle summary via `store_memory` so the next cycle has context.",
        "",
        "## Efficiency",
        "- Be concise. Run parallel bash commands when possible.",
        "- Focus on actionable findings, not exhaustive enumeration.",
        "- Aim to complete the cycle in under 15 tool calls.",
    ]

    # Remediation policy
    sections += [
        "",
        "## Remediation Policy",
    ]
    if config.remediation.enabled:
        if config.remediation.allowed_restart_services:
            services = ", ".join(config.remediation.allowed_restart_services)
            sections.append(
                f"- Allowed to restart systemd services: {services}"
            )
        if config.remediation.allowed_restart_containers:
            containers = ", ".join(config.remediation.allowed_restart_containers)
            sections.append(
                f"- Allowed to restart containers: {containers}"
            )
        sections.append(
            f"- Max restart attempts per target: {config.remediation.max_restart_attempts}"
        )
    else:
        sections.append(
            "Remediation is disabled. Report issues but do not attempt fixes."
        )

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

    # Watched containers
    if config.watched_containers:
        names = ", ".join(config.watched_containers)
        sections += [
            "",
            f"## Watched Containers: {names}",
            "Check these containers are running and healthy. Investigate any that are not.",
        ]

    # Rules
    sections += [
        "",
        "## Rules",
        "- NEVER leak secrets, API keys, or passwords in alerts or memory.",
        "- NEVER kill any process.",
        "- NEVER run destructive commands (rm -rf, mkfs, dd, etc.).",
        "- Never remediate anything not in the allowed lists.",
        "- Reference specific PIDs, container names, and metrics in alerts.",
    ]

    # Last cycle summary
    if last_cycle_summary:
        sections += [
            "",
            "## Last Cycle Summary",
            last_cycle_summary,
        ]

    # Watched service context from memory
    if watched_context and watched_context != "No past observations in memory.":
        sections += [
            "",
            "## Recent Memory (watched services)",
            watched_context,
        ]

    return "\n".join(sections)


def build_investigator_prompt(config: Config, issue_description: str) -> str:
    """Build the system prompt for an investigator sub-agent."""
    sections = [
        "You are investigating a specific system issue. Your job is to deeply",
        "investigate, diagnose, and if possible remediate this issue.",
        "",
        f"## Issue to Investigate",
        f"{issue_description}",
        "",
        "## Instructions",
        "",
        "1. Use bash freely to investigate: check logs (`journalctl`, `docker logs`,",
        "   application logs), correlate metrics (CPU, memory, disk, network),",
        "   inspect processes and containers.",
        "2. Use `query_memory` to check for similar past issues and what resolved them.",
        "3. Diagnose the root cause, not just the symptom.",
        "4. If remediation is allowed for this target, fix it and verify the fix worked.",
        "5. Report back a concise summary: what you found, what you did, and the outcome.",
    ]

    # Remediation policy
    sections += [
        "",
        "## Remediation Policy",
    ]
    if config.remediation.enabled:
        if config.remediation.allowed_restart_services:
            services = ", ".join(config.remediation.allowed_restart_services)
            sections.append(
                f"- Allowed to restart systemd services: {services}"
            )
        if config.remediation.allowed_restart_containers:
            containers = ", ".join(config.remediation.allowed_restart_containers)
            sections.append(
                f"- Allowed to restart containers: {containers}"
            )
        sections.append(
            f"- Max restart attempts per target: {config.remediation.max_restart_attempts}"
        )
    else:
        sections.append(
            "Remediation is disabled. Diagnose and report but do not attempt fixes."
        )

    # Rules
    sections += [
        "",
        "## Rules",
        "- NEVER leak secrets, API keys, or passwords in your report.",
        "- NEVER kill any process.",
        "- NEVER run destructive commands (rm -rf, mkfs, dd, etc.).",
        "- Never remediate anything not in the allowed lists.",
    ]

    return "\n".join(sections)


def build_system_prompt(config: Config, *, memory_context: str = "") -> str:
    """Backward-compatible wrapper for interactive mode.

    Delegates to build_orchestrator_prompt with memory_context as watched_context.
    """
    return build_orchestrator_prompt(
        config,
        watched_context=memory_context,
    )
