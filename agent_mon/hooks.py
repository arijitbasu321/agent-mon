"""PreToolUse hooks: tool allowlist guard and Docker remediation guard."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from agent_mon.config import Config


@dataclass
class HookResult:
    decision: str  # "allow" or "deny"
    reason: str = ""


# ---------------------------------------------------------------------------
# Allowed tools set
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = {
    # System monitoring (in-process)
    "mcp__monitoring__get_cpu_info",
    "mcp__monitoring__get_memory_info",
    "mcp__monitoring__get_disk_info",
    "mcp__monitoring__get_io_info",
    "mcp__monitoring__get_process_list",
    "mcp__monitoring__get_security_info",
    "mcp__monitoring__get_system_issues",
    # Alerting (in-process)
    "mcp__monitoring__send_alert",
    "mcp__monitoring__get_alert_history",
    # Remediation — process/service (in-process)
    "mcp__monitoring__kill_process",
    "mcp__monitoring__restart_service",
    # Docker (external MCP)
    "mcp__docker__list_containers",
    "mcp__docker__inspect_container",
    "mcp__docker__container_logs",
    "mcp__docker__container_stats",
    "mcp__docker__restart_container",
    "mcp__docker__start_container",
    "mcp__docker__stop_container",
    "mcp__docker__list_images",
}


# ---------------------------------------------------------------------------
# Tool allowlist guard (catch-all)
# ---------------------------------------------------------------------------

def tool_allowlist_guard(tool_name: str, tool_input: dict) -> HookResult:
    if tool_name not in ALLOWED_TOOLS:
        return HookResult(
            decision="deny",
            reason=f"Tool {tool_name} is not permitted",
        )
    return HookResult(decision="allow")


# ---------------------------------------------------------------------------
# Docker remediation guard with rate limiting
# ---------------------------------------------------------------------------

# Per-container restart timestamps: container_name -> list of timestamps
_restart_history: dict[str, list[float]] = defaultdict(list)


def reset_rate_limits() -> None:
    """Reset rate limit state. Used in tests."""
    _restart_history.clear()


def docker_remediation_guard(
    tool_name: str,
    tool_input: dict,
    *,
    config: Config,
) -> HookResult:
    if not config.remediation.enabled:
        return HookResult(
            decision="deny",
            reason="Remediation is disabled",
        )

    container = tool_input.get("container", "")
    if container not in config.remediation.allowed_restart_containers:
        return HookResult(
            decision="deny",
            reason=f"Container '{container}' is not in the allowed restart list",
        )

    # Rate limiting
    now = time.monotonic()
    hour_ago = now - 3600
    # Prune old entries
    _restart_history[container] = [
        t for t in _restart_history[container] if t > hour_ago
    ]

    if len(_restart_history[container]) >= config.remediation.max_restart_attempts:
        return HookResult(
            decision="deny",
            reason=(
                f"Rate limit exceeded: {container} has been restarted "
                f"{len(_restart_history[container])} times in the last hour "
                f"(max: {config.remediation.max_restart_attempts})"
            ),
        )

    _restart_history[container].append(now)
    return HookResult(decision="allow")
