"""Tests for send_alert, get_alert_history, and secret sanitization.

Covers:
- send_alert appends plain text line to log file
- send_alert sends email for warning/critical via Resend
- send_alert skips email for info severity
- send_alert deduplicates emails within window (M1: on original title)
- send_alert handles Resend API errors gracefully (L6: reads error body)
- send_alert sanitizes secrets before logging and emailing
- send_alert rotates log files (H4)
- send_alert sends Slack webhook for warning/critical
- send_alert deduplicates Slack messages within window
- send_alert handles Slack webhook errors gracefully
- get_alert_history returns recent alerts from log (H3: efficient read)
- sanitize_secrets replaces known secret patterns (H1: expanded)
- _email_dedup pruning (L2)
- _slack_dedup pruning
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_mon.tools.alerts import AlertManager, sanitize_secrets


# ===========================================================================
# AlertManager initialization
# ===========================================================================


class TestAlertManagerInit:
    """Test AlertManager setup."""

    def test_creates_with_config(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        assert manager.config is config


# ===========================================================================
# send_alert
# ===========================================================================


class TestSendAlert:
    """Test the send_alert tool."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        manager.http_session.post = AsyncMock(return_value=response)
        return manager

    async def test_appends_line_to_log(self, alert_manager, alerts_log_file):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")

        content = alerts_log_file.read_text()
        assert "High CPU" in content
        assert "CPU at 92%" in content
        assert "[WARNING]" in content

    async def test_log_line_includes_timestamp(self, alert_manager, alerts_log_file):
        await alert_manager.send_alert("critical", "Disk full", "/data at 98%")

        content = alerts_log_file.read_text()
        # Should have ISO 8601 timestamp
        assert "T" in content and "Z" in content

    async def test_sends_email_for_warning(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        alert_manager.http_session.post.assert_awaited()

    async def test_sends_email_for_critical(self, alert_manager):
        await alert_manager.send_alert("critical", "Disk full", "/data at 98%")
        alert_manager.http_session.post.assert_awaited()

    async def test_skips_email_for_info(self, alert_manager):
        await alert_manager.send_alert("info", "System check", "All OK")
        alert_manager.http_session.post.assert_not_awaited()

    async def test_email_includes_severity_in_subject(self, alert_manager):
        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        # First post call is email (Resend), second is Slack
        call_kwargs = alert_manager.http_session.post.call_args_list[0]
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "CRITICAL" in body["subject"]

    async def test_email_includes_hostname_in_subject(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")

        call_kwargs = alert_manager.http_session.post.call_args_list[0]
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "agent-mon@" in body["subject"]

    async def test_email_sends_to_configured_recipients(self, alert_manager):
        await alert_manager.send_alert("warning", "Test", "test message")

        call_kwargs = alert_manager.http_session.post.call_args_list[0]
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "ops@example.com" in body["to"]


class TestSendAlertDedup:
    """Test email deduplication within the configured window."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        # Disable Slack to isolate email dedup behavior
        config.alerts.slack.enabled = False
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        manager.http_session.post = AsyncMock(return_value=response)
        return manager

    async def test_dedup_suppresses_duplicate_email(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High CPU", "CPU at 93%")

        assert alert_manager.http_session.post.await_count == 1

    async def test_dedup_allows_different_titles(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High Memory", "Mem at 88%")

        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_allows_after_window_expires(self, alert_manager):
        alert_manager.config.alerts.email.dedup_window_minutes = 0

        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High CPU", "CPU at 93%")

        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_still_logs_suppressed_alerts(
        self, alert_manager, alerts_log_file
    ):
        await alert_manager.send_alert("warning", "High CPU", "v1")
        await alert_manager.send_alert("warning", "High CPU", "v2")

        content = alerts_log_file.read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) == 2

    async def test_dedup_on_original_title_not_sanitized(self, alert_manager):
        """M1: different secrets should produce different dedup keys."""
        await alert_manager.send_alert(
            "warning",
            "Found key sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaa",
            "Leaked",
        )
        await alert_manager.send_alert(
            "warning",
            "Found key sk-ant-api03-bbbbbbbbbbbbbbbbbbbbbb",
            "Leaked",
        )
        # Both should send because original titles differ
        assert alert_manager.http_session.post.await_count == 2


class TestSendAlertErrorHandling:
    """Test graceful handling of email delivery failures."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        return manager

    async def test_handles_resend_api_error(self, alert_manager, alerts_log_file):
        response = AsyncMock()
        response.status = 500
        response.text = AsyncMock(return_value='{"error": "internal"}')
        alert_manager.http_session.post = AsyncMock(return_value=response)

        result = await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        content = alerts_log_file.read_text()
        assert "Disk full" in content
        # L6: error body included
        assert "500" in result

    async def test_handles_network_error(self, alert_manager, alerts_log_file):
        import aiohttp

        alert_manager.http_session.post = AsyncMock(
            side_effect=aiohttp.ClientError("network error")
        )

        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        content = alerts_log_file.read_text()
        assert "Disk full" in content

    async def test_handles_no_http_session(self, alert_manager, alerts_log_file):
        alert_manager.http_session = None

        await alert_manager.send_alert("warning", "Test", "msg")

        content = alerts_log_file.read_text()
        assert "Test" in content


# ===========================================================================
# get_alert_history (H3: efficient read)
# ===========================================================================


class TestGetAlertHistory:
    """Test alert history retrieval from log file."""

    @pytest.fixture
    def populated_log(self, alerts_log_file):
        with open(alerts_log_file, "w") as f:
            f.write("[2026-03-03T12:00:00Z] [WARNING] High CPU: CPU at 88%\n")
            f.write("[2026-03-03T12:05:00Z] [CRITICAL] Disk full: / at 97%\n")
            f.write("[2026-03-03T12:10:00Z] [INFO] Check complete: All clear\n")
        return alerts_log_file

    @pytest.fixture
    def alert_manager(self, config_yaml_file, populated_log):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(populated_log)
        return AlertManager(config)

    def test_returns_recent_alerts(self, alert_manager):
        history = alert_manager.get_alert_history(last_n=20)
        assert isinstance(history, str)
        assert "High CPU" in history
        assert "Disk full" in history

    def test_respects_last_n_limit(self, alert_manager):
        history = alert_manager.get_alert_history(last_n=1)
        assert "Check complete" in history
        assert "High CPU" not in history

    def test_handles_empty_log(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)

        history = manager.get_alert_history()
        assert isinstance(history, str)

    def test_handles_missing_log_file(self, config_yaml_file, tmp_path):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(tmp_path / "nonexistent.log")
        manager = AlertManager(config)

        history = manager.get_alert_history()
        assert isinstance(history, str)

    def test_default_last_n_is_20(self, alert_manager):
        history = alert_manager.get_alert_history()
        assert "High CPU" in history
        assert "Disk full" in history
        assert "Check complete" in history


# ===========================================================================
# Log rotation (H4)
# ===========================================================================


class TestLogRotation:
    """Test size-based log rotation."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        return AlertManager(config)

    def test_does_not_rotate_small_log(self, alert_manager, alerts_log_file):
        alerts_log_file.write_text("small content\n")
        alert_manager._rotate_log_if_needed()
        assert alerts_log_file.exists()
        assert not alerts_log_file.with_suffix(".1").exists()

    def test_rotates_large_log(self, alert_manager, alerts_log_file):
        # Write more than _MAX_LOG_SIZE
        with patch("agent_mon.tools.alerts._MAX_LOG_SIZE", 10):
            alerts_log_file.write_text("x" * 20)
            alert_manager._rotate_log_if_needed()

        assert alerts_log_file.with_suffix(".1").exists()
        assert not alerts_log_file.exists()

    def test_chains_rotations(self, alert_manager, alerts_log_file):
        with patch("agent_mon.tools.alerts._MAX_LOG_SIZE", 10):
            # First rotation
            alerts_log_file.write_text("first" * 10)
            alert_manager._rotate_log_if_needed()
            assert alerts_log_file.with_suffix(".1").exists()

            # Write new content and rotate again
            alerts_log_file.write_text("second" * 10)
            alert_manager._rotate_log_if_needed()
            assert alerts_log_file.with_suffix(".1").exists()
            assert alerts_log_file.with_suffix(".2").exists()


# ===========================================================================
# Secret sanitizer (H1: expanded patterns)
# ===========================================================================


class TestSecretSanitizer:
    """Test the sanitize_secrets function."""

    def test_redacts_anthropic_key(self):
        text = "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        assert "[REDACTED]" in sanitize_secrets(text)
        assert "sk-ant-" not in sanitize_secrets(text)

    def test_redacts_aws_access_key(self):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        assert "[REDACTED]" in sanitize_secrets(text)
        assert "AKIA" not in sanitize_secrets(text)

    def test_redacts_github_pat(self):
        for prefix in ["ghp_", "gho_", "ghs_"]:
            text = f"Token: {prefix}{'a' * 36}"
            result = sanitize_secrets(text)
            assert "[REDACTED]" in result
            assert prefix not in result

    def test_redacts_gitlab_pat(self):
        text = "Token: glpat-abcdefghijklmnopqrstuvwxyz"
        assert "[REDACTED]" in sanitize_secrets(text)
        assert "glpat-" not in sanitize_secrets(text)

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result
        assert "eyJ" not in result

    def test_redacts_password_assignment(self):
        text = "password=supersecret123"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result
        assert "supersecret123" not in result

    def test_redacts_secret_assignment(self):
        text = "secret=my_top_secret_value"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result

    def test_redacts_api_key_assignment(self):
        text = "api_key=sk-proj-abc123def456"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result

    def test_redacts_resend_key(self):
        text = "RESEND_API_KEY=re_abcdefghijklmnopqrstuvwxyz"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result
        assert "re_abcdef" not in result

    def test_redacts_slack_tokens(self):
        for prefix in ["xoxb-", "xoxp-"]:
            text = f"Token: {prefix}{'1234567890-' * 3}abcdef"
            result = sanitize_secrets(text)
            assert "[REDACTED]" in result

    def test_redacts_openai_style_key(self):
        text = "Key: sk-proj-abcdefghijklmnopqrstuvwxyz"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result

    def test_preserves_normal_text(self):
        text = "CPU at 92%, disk at 85%, nginx is running"
        assert sanitize_secrets(text) == text

    def test_multiple_secrets_in_one_string(self):
        text = (
            "Found sk-ant-api03-abcdefghijklmnopqrstuvwxyz "
            "and AKIAIOSFODNN7EXAMPLE in env"
        )
        result = sanitize_secrets(text)
        assert result.count("[REDACTED]") == 2
        assert "sk-ant-" not in result
        assert "AKIA" not in result

    # H1: expanded pattern tests
    def test_redacts_database_url(self):
        text = "DATABASE_URL=postgres://user:pass@host:5432/db"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result
        assert "postgres://" not in result

    def test_redacts_auth_token(self):
        text = "auth_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result

    def test_redacts_client_secret(self):
        text = "client_secret=super_secret_value_123"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result

    def test_redacts_db_password(self):
        text = "db_password=hunter2"
        result = sanitize_secrets(text)
        assert "[REDACTED]" in result


class TestSendAlertSecretSanitization:
    """Test that send_alert sanitizes secrets."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        manager.http_session.post = AsyncMock(return_value=response)
        return manager

    async def test_secrets_redacted_in_log(self, alert_manager, alerts_log_file):
        await alert_manager.send_alert(
            "warning",
            "Found key sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
            "Leaked in config file",
        )
        content = alerts_log_file.read_text()
        assert "sk-ant-" not in content
        assert "[REDACTED]" in content

    async def test_secrets_redacted_in_email(self, alert_manager):
        await alert_manager.send_alert(
            "warning",
            "Found API key",
            "Key AKIAIOSFODNN7EXAMPLE was exposed",
        )
        # First post call is email (Resend)
        call_kwargs = alert_manager.http_session.post.call_args_list[0]
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "AKIA" not in body["text"]
        assert "[REDACTED]" in body["text"]


# ===========================================================================
# Dedup pruning (L2)
# ===========================================================================


class TestDedupPruning:
    """Test that _email_dedup dict is pruned."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        config.alerts.email.dedup_window_minutes = 1
        manager = AlertManager(config)
        return manager

    def test_expired_entries_are_pruned(self, alert_manager):
        # Manually add an old entry
        alert_manager._email_dedup["old_title"] = time.time() - 120  # 2 min ago
        alert_manager._email_dedup["new_title"] = time.time()

        # Trigger pruning via _should_send_email
        alert_manager._should_send_email("test_title")

        # Old entry should be pruned
        assert "old_title" not in alert_manager._email_dedup
        # New entry should remain
        assert "new_title" in alert_manager._email_dedup


# ===========================================================================
# Slack webhook dispatch
# ===========================================================================


class TestSlackAlertDispatch:
    """Test Slack webhook alert delivery."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        manager.http_session.post = AsyncMock(return_value=response)
        return manager

    async def test_sends_slack_for_warning(self, alert_manager):
        result = await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        assert "slack: sent" in result
        # Should have posted to both Resend and Slack
        assert alert_manager.http_session.post.await_count == 2

    async def test_sends_slack_for_critical(self, alert_manager):
        result = await alert_manager.send_alert("critical", "Disk full", "/data at 98%")
        assert "slack: sent" in result

    async def test_skips_slack_for_info(self, alert_manager):
        result = await alert_manager.send_alert("info", "System check", "All OK")
        assert "slack" not in result

    async def test_slack_payload_contains_severity_and_title(self, alert_manager):
        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")
        # Second post call is Slack (first is email)
        slack_call = alert_manager.http_session.post.call_args_list[1]
        body = slack_call.kwargs.get("json") or slack_call[1].get("json")
        assert "CRITICAL" in body["text"]
        assert "Disk full" in body["text"]

    async def test_slack_disabled_does_not_post(self, alert_manager):
        alert_manager.config.alerts.slack.enabled = False
        result = await alert_manager.send_alert("critical", "Disk full", "/ at 98%")
        assert "slack" not in result
        # Only email post, not slack
        assert alert_manager.http_session.post.await_count == 1

    async def test_slack_no_http_session(self, alert_manager):
        alert_manager.http_session = None
        result = await alert_manager.send_alert("warning", "Test", "msg")
        assert "slack" not in result

    async def test_slack_secrets_redacted(self, alert_manager):
        await alert_manager.send_alert(
            "warning",
            "Found key",
            "Key sk-ant-api03-abcdefghijklmnopqrstuvwxyz was exposed",
        )
        # Second post call is Slack
        slack_call = alert_manager.http_session.post.call_args_list[1]
        body = slack_call.kwargs.get("json") or slack_call[1].get("json")
        assert "sk-ant-" not in body["text"]
        assert "[REDACTED]" in body["text"]


class TestSlackAlertDedup:
    """Test Slack webhook deduplication."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        # Disable email to isolate Slack behavior
        config.alerts.email.enabled = False
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        response = AsyncMock()
        response.status = 200
        manager.http_session.post = AsyncMock(return_value=response)
        return manager

    async def test_dedup_suppresses_duplicate_slack(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High CPU", "CPU at 93%")
        assert alert_manager.http_session.post.await_count == 1

    async def test_dedup_allows_different_titles(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High Memory", "Mem at 88%")
        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_allows_after_window_expires(self, alert_manager):
        alert_manager.config.alerts.slack.dedup_window_minutes = 0
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High CPU", "CPU at 93%")
        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_on_original_title_not_sanitized(self, alert_manager):
        await alert_manager.send_alert(
            "warning",
            "Found key sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaa",
            "Leaked",
        )
        await alert_manager.send_alert(
            "warning",
            "Found key sk-ant-api03-bbbbbbbbbbbbbbbbbbbbbb",
            "Leaked",
        )
        assert alert_manager.http_session.post.await_count == 2


class TestSlackAlertErrorHandling:
    """Test graceful handling of Slack webhook failures."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        config.alerts.email.enabled = False
        manager = AlertManager(config)
        manager.http_session = AsyncMock()
        return manager

    async def test_handles_slack_api_error(self, alert_manager):
        response = AsyncMock()
        response.status = 500
        response.text = AsyncMock(return_value="server_error")
        alert_manager.http_session.post = AsyncMock(return_value=response)

        result = await alert_manager.send_alert("critical", "Disk full", "/ at 98%")
        assert "slack: failed (HTTP 500" in result

    async def test_handles_slack_network_error(self, alert_manager):
        import aiohttp

        alert_manager.http_session.post = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        result = await alert_manager.send_alert("critical", "Disk full", "/ at 98%")
        assert "slack: failed" in result


class TestSlackDedupPruning:
    """Test that _slack_dedup dict is pruned."""

    @pytest.fixture
    def alert_manager(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        config.alerts.slack.dedup_window_minutes = 1
        manager = AlertManager(config)
        return manager

    def test_expired_entries_are_pruned(self, alert_manager):
        alert_manager._slack_dedup["old_title"] = time.time() - 120
        alert_manager._slack_dedup["new_title"] = time.time()

        alert_manager._should_send_slack("test_title")

        assert "old_title" not in alert_manager._slack_dedup
        assert "new_title" in alert_manager._slack_dedup
