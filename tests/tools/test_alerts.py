"""Tests for send_alert and get_alert_history tools.

Covers:
- send_alert appends plain text line to log file
- send_alert sends email for warning/critical via Resend
- send_alert skips email for info severity
- send_alert deduplicates emails within window
- send_alert handles Resend API errors gracefully
- get_alert_history returns recent alerts from log
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_mon.tools.alerts import AlertManager


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

        call_kwargs = alert_manager.http_session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "CRITICAL" in body["subject"]

    async def test_email_includes_hostname_in_subject(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")

        call_kwargs = alert_manager.http_session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "agent-mon@" in body["subject"]

    async def test_email_sends_to_configured_recipients(self, alert_manager):
        await alert_manager.send_alert("warning", "Test", "test message")

        call_kwargs = alert_manager.http_session.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "ops@example.com" in body["to"]


class TestSendAlertDedup:
    """Test email deduplication within the configured window."""

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
        alert_manager.http_session.post = AsyncMock(return_value=response)

        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        content = alerts_log_file.read_text()
        assert "Disk full" in content

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
# get_alert_history
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
