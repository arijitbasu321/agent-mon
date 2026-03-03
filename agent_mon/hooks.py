"""PreToolUse hooks: bash deny-list guard and Docker remediation guard."""

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
# Bash deny-list guard
# ---------------------------------------------------------------------------

def bash_denylist_guard(
    tool_name: str,
    tool_input: dict,
    *,
    config: Config,
) -> HookResult:
    """Block bash commands that match any entry in the deny list.

    Uses case-insensitive substring matching.
    """
    command = tool_input.get("command", "")
    if not command:
        return HookResult(decision="allow")

    command_lower = command.lower()
    for pattern in config.bash.deny_list:
        if pattern.lower() in command_lower:
            return HookResult(
                decision="deny",
                reason=f"Command blocked by deny-list: matches '{pattern}'",
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
