"""Tests for PreToolUse hooks: bash deny-list guard and Docker remediation guard.

Covers:
- Bash deny-list blocks dangerous commands (case-insensitive substring match)
- Bash deny-list allows safe commands
- Docker remediation guard checks container allow-list
- Docker remediation guard enforces rate limits
- RateLimiter class (H2)
"""

import time
from unittest.mock import MagicMock

import pytest

from agent_mon.hooks import (
    RateLimiter,
    bash_denylist_guard,
    docker_remediation_guard,
    reset_rate_limits,
)


# ===========================================================================
# Bash deny-list guard
# ===========================================================================


class TestBashDenylistGuard:
    """Test the bash deny-list PreToolUse hook."""

    @pytest.fixture
    def config(self):
        config = MagicMock()
        config.bash.deny_list = [
            "rm -rf /",
            "rm -rf /*",
            "rm -r /",
            "shutdown -h",
            "shutdown -r",
            "shutdown -P",
            "shutdown now",
            "reboot",
            "poweroff",
            "halt -p",
            "halt -f",
            "mkfs",
            "dd if=",
            ":(){ :|:& };:",
            "fdisk",
            "chmod -R 777 /",
        ]
        return config

    @pytest.mark.parametrize(
        "command",
        [
            "ps aux",
            "top -bn1",
            "df -h",
            "free -m",
            "journalctl -p err --since '1 hour ago'",
            "ss -tlnp",
            "systemctl list-units --failed",
            "uptime",
            "cat /proc/loadavg",
            "systemctl restart nginx",
            "docker ps",
            "rm /tmp/test.log",
            "cat /etc/passwd",
            "grep halt_on_error config.yaml",
        ],
    )
    def test_allows_safe_commands(self, config, command):
        result = bash_denylist_guard(
            "Bash", {"command": command}, config=config
        )
        assert result.decision == "allow"

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "rm -rf /*",
            "rm -r /",
            "sudo rm -rf /",
            "shutdown -h now",
            "shutdown -r now",
            "shutdown -P",
            "shutdown now",
            "reboot",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "fdisk /dev/sda",
            "chmod -R 777 /",
            "halt -p",
            "halt -f",
        ],
    )
    def test_blocks_dangerous_commands(self, config, command):
        result = bash_denylist_guard(
            "Bash", {"command": command}, config=config
        )
        assert result.decision == "deny"
        assert "deny-list" in result.reason

    def test_case_insensitive_matching(self, config):
        result = bash_denylist_guard(
            "Bash", {"command": "SHUTDOWN -h now"}, config=config
        )
        assert result.decision == "deny"

    def test_cat_etc_passwd_allowed(self, config):
        """Regression: cat /etc/passwd should not be blocked."""
        result = bash_denylist_guard(
            "Bash", {"command": "cat /etc/passwd"}, config=config
        )
        assert result.decision == "allow"

    def test_grep_halt_on_error_allowed(self, config):
        """Regression: grep halt_on_error should not be blocked."""
        result = bash_denylist_guard(
            "Bash", {"command": "grep halt_on_error config.yaml"}, config=config
        )
        assert result.decision == "allow"

    def test_allows_empty_command(self, config):
        result = bash_denylist_guard(
            "Bash", {"command": ""}, config=config
        )
        assert result.decision == "allow"

    def test_allows_no_command_key(self, config):
        result = bash_denylist_guard(
            "Bash", {}, config=config
        )
        assert result.decision == "allow"

    def test_empty_deny_list_allows_all(self):
        config = MagicMock()
        config.bash.deny_list = []
        result = bash_denylist_guard(
            "Bash", {"command": "rm -rf /"}, config=config
        )
        assert result.decision == "allow"


# ===========================================================================
# RateLimiter (H2)
# ===========================================================================


class TestRateLimiter:
    """Test the RateLimiter class."""

    def test_allows_first_attempt(self):
        limiter = RateLimiter()
        allowed, reason = limiter.check_and_record("nginx", 3)
        assert allowed is True
        assert reason == ""

    def test_allows_up_to_max(self):
        limiter = RateLimiter()
        for _ in range(3):
            allowed, _ = limiter.check_and_record("nginx", 3)
            assert allowed is True

    def test_denies_after_max(self):
        limiter = RateLimiter()
        for _ in range(3):
            limiter.check_and_record("nginx", 3)
        allowed, reason = limiter.check_and_record("nginx", 3)
        assert allowed is False
        assert "rate limit" in reason.lower()

    def test_per_container_tracking(self):
        limiter = RateLimiter()
        for _ in range(3):
            limiter.check_and_record("nginx", 3)
        # nginx exhausted, but redis should be fine
        allowed, _ = limiter.check_and_record("redis", 3)
        assert allowed is True

    def test_reset_clears_all(self):
        limiter = RateLimiter()
        for _ in range(3):
            limiter.check_and_record("nginx", 3)
        limiter.reset()
        allowed, _ = limiter.check_and_record("nginx", 3)
        assert allowed is True


# ===========================================================================
# Docker remediation guard
# ===========================================================================


class TestDockerRemediationGuard:
    """Test the Docker restart/stop gating hook."""

    @pytest.fixture
    def remediation_config(self):
        config = MagicMock()
        config.remediation.enabled = True
        config.remediation.allowed_restart_containers = ["nginx", "redis", "postgres"]
        config.remediation.max_restart_attempts = 3
        return config

    def test_allows_restart_of_allowed_container(self, remediation_config):
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert result.decision == "allow"

    def test_denies_restart_of_disallowed_container(self, remediation_config):
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "production-db"},
            config=remediation_config,
        )
        assert result.decision == "deny"
        assert "production-db" in result.reason

    def test_allows_stop_of_allowed_container(self, remediation_config):
        result = docker_remediation_guard(
            "mcp__docker__stop_container",
            {"container": "redis"},
            config=remediation_config,
        )
        assert result.decision == "allow"

    def test_denies_stop_of_disallowed_container(self, remediation_config):
        result = docker_remediation_guard(
            "mcp__docker__stop_container",
            {"container": "vault"},
            config=remediation_config,
        )
        assert result.decision == "deny"

    def test_case_sensitive_container_names(self, remediation_config):
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "Nginx"},
            config=remediation_config,
        )
        assert result.decision == "deny"

    def test_denies_when_remediation_disabled(self, remediation_config):
        remediation_config.remediation.enabled = False
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert result.decision == "deny"

    def test_uses_provided_rate_limiter(self, remediation_config):
        """H2: accepts an explicit RateLimiter instance."""
        limiter = RateLimiter()
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
            rate_limiter=limiter,
        )
        assert result.decision == "allow"


class TestDockerRemediationRateLimit:
    """Test rate limiting on Docker remediation actions."""

    @pytest.fixture
    def remediation_config(self):
        config = MagicMock()
        config.remediation.enabled = True
        config.remediation.allowed_restart_containers = ["nginx"]
        config.remediation.max_restart_attempts = 2
        return config

    @pytest.fixture(autouse=True)
    def reset_rate_limiter(self):
        reset_rate_limits()
        yield
        reset_rate_limits()

    def test_allows_up_to_max_attempts(self, remediation_config):
        r1 = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert r1.decision == "allow"

        r2 = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert r2.decision == "allow"

    def test_denies_after_max_attempts_exceeded(self, remediation_config):
        for _ in range(remediation_config.remediation.max_restart_attempts):
            docker_remediation_guard(
                "mcp__docker__restart_container",
                {"container": "nginx"},
                config=remediation_config,
            )

        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert result.decision == "deny"
        assert "rate limit" in result.reason.lower()

    def test_rate_limit_is_per_container(self, remediation_config):
        remediation_config.remediation.allowed_restart_containers = [
            "nginx",
            "redis",
        ]

        for _ in range(remediation_config.remediation.max_restart_attempts):
            docker_remediation_guard(
                "mcp__docker__restart_container",
                {"container": "nginx"},
                config=remediation_config,
            )

        # nginx is rate-limited, but redis should still be allowed
        result = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "redis"},
            config=remediation_config,
        )
        assert result.decision == "allow"
