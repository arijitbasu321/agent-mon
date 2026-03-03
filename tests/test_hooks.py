"""Tests for PreToolUse hooks: tool allowlist guard and Docker remediation guard.

Covers:
- Catch-all allowlist hook allows/denies correct tools
- Docker remediation guard checks container allow-list
- Docker remediation guard enforces rate limits
- Docker stop_container is also gated
"""

import time

import pytest

from agent_mon.hooks import (
    ALLOWED_TOOLS,
    docker_remediation_guard,
    tool_allowlist_guard,
)


# ===========================================================================
# Tool allowlist guard
# ===========================================================================


class TestToolAllowlistGuard:
    """Test the catch-all PreToolUse hook."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__monitoring__get_cpu_info",
            "mcp__monitoring__get_memory_info",
            "mcp__monitoring__get_disk_info",
            "mcp__monitoring__get_io_info",
            "mcp__monitoring__get_process_list",
            "mcp__monitoring__get_security_info",
            "mcp__monitoring__get_system_issues",
            "mcp__monitoring__send_alert",
            "mcp__monitoring__get_alert_history",
            "mcp__monitoring__kill_process",
            "mcp__monitoring__restart_service",
            "mcp__docker__list_containers",
            "mcp__docker__inspect_container",
            "mcp__docker__container_logs",
            "mcp__docker__container_stats",
            "mcp__docker__restart_container",
            "mcp__docker__start_container",
            "mcp__docker__stop_container",
            "mcp__docker__list_images",
        ],
    )
    def test_allows_all_listed_tools(self, tool_name):
        result = tool_allowlist_guard(tool_name, {})
        assert result.decision == "allow"

    @pytest.mark.parametrize(
        "tool_name",
        [
            "Bash",
            "Write",
            "Edit",
            "Read",
            "Glob",
            "Grep",
            "mcp__monitoring__unknown_tool",
            "mcp__docker__remove_container",
            "mcp__docker__pull_image",
            "mcp__unknown__something",
            "",
        ],
    )
    def test_denies_unlisted_tools(self, tool_name):
        result = tool_allowlist_guard(tool_name, {})
        assert result.decision == "deny"
        assert tool_name in result.reason

    def test_allowed_tools_set_has_expected_count(self):
        """Sanity check: should have 19 tools (11 monitoring + 8 Docker)."""
        assert len(ALLOWED_TOOLS) == 19

    def test_no_bash_in_allowed_tools(self):
        assert "Bash" not in ALLOWED_TOOLS

    def test_no_write_in_allowed_tools(self):
        assert "Write" not in ALLOWED_TOOLS

    def test_no_edit_in_allowed_tools(self):
        assert "Edit" not in ALLOWED_TOOLS


# ===========================================================================
# Docker remediation guard
# ===========================================================================


class TestDockerRemediationGuard:
    """Test the Docker restart/stop gating hook."""

    @pytest.fixture
    def remediation_config(self):
        """Return a mock config with allowed containers and rate limit."""
        from unittest.mock import MagicMock

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


class TestDockerRemediationRateLimit:
    """Test rate limiting on Docker remediation actions."""

    @pytest.fixture
    def remediation_config(self):
        from unittest.mock import MagicMock

        config = MagicMock()
        config.remediation.enabled = True
        config.remediation.allowed_restart_containers = ["nginx"]
        config.remediation.max_restart_attempts = 2
        return config

    @pytest.fixture(autouse=True)
    def reset_rate_limiter(self):
        """Reset the rate limiter state between tests."""
        from agent_mon.hooks import reset_rate_limits

        reset_rate_limits()
        yield
        reset_rate_limits()

    def test_allows_up_to_max_attempts(self, remediation_config):
        # First attempt
        r1 = docker_remediation_guard(
            "mcp__docker__restart_container",
            {"container": "nginx"},
            config=remediation_config,
        )
        assert r1.decision == "allow"

        # Second attempt
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

        # This should be denied
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
