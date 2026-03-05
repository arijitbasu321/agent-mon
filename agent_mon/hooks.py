"""PreToolUse hooks: bash deny-list guard and Docker remediation guard.

Hooks are implemented both as plain functions (for unit testing) and as
SDK-compatible async hook callbacks via build_sdk_hooks().
"""

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
# Rate limiter (H2: instance-owned, not global mutable state)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Per-container restart rate limiter."""

    def __init__(self):
        self._restart_history: dict[str, list[float]] = defaultdict(list)

    def check_and_record(self, container: str, max_attempts: int) -> tuple[bool, str]:
        """Check if a restart is allowed, and record it if so.

        Returns (allowed, reason) tuple.
        """
        now = time.monotonic()
        hour_ago = now - 3600
        # Prune old entries
        self._restart_history[container] = [
            t for t in self._restart_history[container] if t > hour_ago
        ]

        if len(self._restart_history[container]) >= max_attempts:
            return False, (
                f"Rate limit exceeded: {container} has been restarted "
                f"{len(self._restart_history[container])} times in the last hour "
                f"(max: {max_attempts})"
            )

        self._restart_history[container].append(now)
        return True, ""

    def reset(self) -> None:
        """Reset all rate limit state."""
        self._restart_history.clear()


# Module-level default for backward compat / tests
_default_rate_limiter = RateLimiter()


def reset_rate_limits() -> None:
    """Reset rate limit state. Used in tests."""
    _default_rate_limiter.reset()


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

def docker_remediation_guard(
    tool_name: str,
    tool_input: dict,
    *,
    config: Config,
    rate_limiter: RateLimiter | None = None,
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

    # Rate limiting -- use provided limiter or module-level default
    limiter = rate_limiter or _default_rate_limiter
    allowed, reason = limiter.check_and_record(
        container, config.remediation.max_restart_attempts,
    )
    if not allowed:
        return HookResult(decision="deny", reason=reason)

    return HookResult(decision="allow")


# ---------------------------------------------------------------------------
# SDK hook builder
# ---------------------------------------------------------------------------

def build_sdk_hooks(config: Config, rate_limiter: RateLimiter | None = None):
    """Build SDK-compatible PreToolUse hooks dict.

    Returns a dict suitable for ClaudeAgentOptions.hooks:
        {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[...])]}
    """
    from claude_agent_sdk.types import HookMatcher

    limiter = rate_limiter or _default_rate_limiter

    async def _bash_hook(hook_input, session_id, context):
        tool_input = hook_input.get("tool_input", {})
        result = bash_denylist_guard("Bash", tool_input, config=config)
        if result.decision == "deny":
            return {"decision": "block", "reason": result.reason}
        return {}

    async def _docker_hook(hook_input, session_id, context):
        tool_input = hook_input.get("tool_input", {})
        tool_name = hook_input.get("tool_name", "")
        result = docker_remediation_guard(
            tool_name, tool_input, config=config, rate_limiter=limiter,
        )
        if result.decision == "deny":
            return {"decision": "block", "reason": result.reason}
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[_bash_hook]),
            HookMatcher(matcher="docker", hooks=[_docker_hook]),
        ],
    }
