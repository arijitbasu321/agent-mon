"""Tests for send_alert and get_alert_history tools.

Covers:
- send_alert writes to stdout
- send_alert appends JSON Line to log file
- send_alert sends email for warning/critical via Resend
- send_alert skips email for info severity
- send_alert deduplicates emails within window
- send_alert handles Resend API errors gracefully
- get_alert_history returns recent alerts from log
- Log rotation: files rotate at configured size
- JSON Lines format: one valid JSON object per line
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

    def test_initializes_log_handler(self, config_yaml_file, alerts_log_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.alerts.log_file = str(alerts_log_file)
        manager = AlertManager(config)
        assert manager.log_handler is not None


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

    async def test_writes_to_stdout(self, alert_manager, capsys):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        captured = capsys.readouterr()
        assert "High CPU" in captured.out or "High CPU" in captured.err

    async def test_appends_json_line_to_log(self, alert_manager, alerts_log_file):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")

        content = alerts_log_file.read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) >= 1

        record = json.loads(lines[0])
        assert record["severity"] == "warning"
        assert record["title"] == "High CPU"
        assert record["message"] == "CPU at 92%"
        assert "timestamp" in record

    async def test_json_line_is_valid_json(self, alert_manager, alerts_log_file):
        await alert_manager.send_alert("critical", "Disk full", "/data at 98%")
        await alert_manager.send_alert("warning", "High mem", "Memory at 88%")

        content = alerts_log_file.read_text()
        for line in content.strip().split("\n"):
            if line:
                # Should not raise
                parsed = json.loads(line)
                assert "severity" in parsed
                assert "title" in parsed

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

        # Should only send one email (the first)
        assert alert_manager.http_session.post.await_count == 1

    async def test_dedup_allows_different_titles(self, alert_manager):
        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High Memory", "Mem at 88%")

        # Different titles = different alerts = both sent
        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_allows_after_window_expires(self, alert_manager):
        # Set the dedup window to 0 so it "expires" immediately
        alert_manager.config.alerts.email.dedup_window_minutes = 0

        await alert_manager.send_alert("warning", "High CPU", "CPU at 92%")
        await alert_manager.send_alert("warning", "High CPU", "CPU at 93%")

        # Both should have been sent
        assert alert_manager.http_session.post.await_count == 2

    async def test_dedup_still_logs_suppressed_alerts(
        self, alert_manager, alerts_log_file
    ):
        """Even when email is deduplicated, the alert should still be logged."""
        await alert_manager.send_alert("warning", "High CPU", "v1")
        await alert_manager.send_alert("warning", "High CPU", "v2")

        content = alerts_log_file.read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) == 2  # Both logged even though email was deduped


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
        """If Resend returns an error, alert should still be logged."""
        response = AsyncMock()
        response.status = 500
        alert_manager.http_session.post = AsyncMock(return_value=response)

        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        # Alert should still be in the log file
        content = alerts_log_file.read_text()
        assert "Disk full" in content

    async def test_handles_network_error(self, alert_manager, alerts_log_file):
        """If the network is down, alert should still be logged."""
        import aiohttp

        alert_manager.http_session.post = AsyncMock(
            side_effect=aiohttp.ClientError("network error")
        )

        await alert_manager.send_alert("critical", "Disk full", "/ at 98%")

        content = alerts_log_file.read_text()
        assert "Disk full" in content

    async def test_handles_no_http_session(self, alert_manager, alerts_log_file):
        """If http_session is None (email not configured), should still log."""
        alert_manager.http_session = None

        await alert_manager.send_alert("warning", "Test", "msg")

        content = alerts_log_file.read_text()
        assert "Test" in content


# ===========================================================================
# get_alert_history
# ===========================================================================


class TestGetAlertHistory:
    """Test alert history retrieval from JSON Lines log."""

    @pytest.fixture
    def populated_log(self, alerts_log_file):
        """Create a log file with some test alerts."""
        alerts = [
            {"timestamp": "2026-03-03T12:00:00Z", "severity": "warning",
             "title": "High CPU", "message": "CPU at 88%"},
            {"timestamp": "2026-03-03T12:05:00Z", "severity": "critical",
             "title": "Disk full", "message": "/ at 97%"},
            {"timestamp": "2026-03-03T12:10:00Z", "severity": "info",
             "title": "Check complete", "message": "All clear"},
        ]
        with open(alerts_log_file, "w") as f:
            for alert in alerts:
                f.write(json.dumps(alert) + "\n")
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
        # Should only contain the most recent alert
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
        config.alerts.log_file = str(tmp_path / "nonexistent.jsonl")
        manager = AlertManager(config)

        history = manager.get_alert_history()
        assert isinstance(history, str)

    def test_default_last_n_is_20(self, alert_manager):
        # With 3 alerts in the log, all should be returned
        history = alert_manager.get_alert_history()
        assert "High CPU" in history
        assert "Disk full" in history
        assert "Check complete" in history


# ===========================================================================
# Log rotation
# ===========================================================================


class TestLogRotation:
    """Test that the alert log rotates at the configured size."""

    @pytest.fixture
    def small_rotation_manager(self, config_yaml_file, alerts_log_dir):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        log_file = alerts_log_dir / "alerts.jsonl"
        config.alerts.log_file = str(log_file)
        config.alerts.log_max_size_mb = 0  # Will use bytes directly
        # For testing, we'll set a very small max
        manager = AlertManager(config, max_bytes=500)  # 500 bytes
        manager.http_session = None  # No email
        return manager, log_file, alerts_log_dir

    async def test_rotates_after_exceeding_max_size(self, small_rotation_manager):
        manager, log_file, log_dir = small_rotation_manager

        # Write enough alerts to exceed 500 bytes
        for i in range(20):
            await manager.send_alert(
                "info", f"Test alert {i}", f"This is test message number {i}"
            )

        # Should have created at least one rotated file
        rotated = list(log_dir.glob("alerts.jsonl.*"))
        assert len(rotated) >= 1

    async def test_all_log_content_is_valid_jsonl(self, small_rotation_manager):
        manager, log_file, log_dir = small_rotation_manager

        for i in range(10):
            await manager.send_alert("info", f"Alert {i}", f"Message {i}")

        # Check the main log file
        if log_file.exists():
            for line in log_file.read_text().strip().split("\n"):
                if line:
                    json.loads(line)  # Should not raise

        # Check rotated files
        for rotated in log_dir.glob("alerts.jsonl.*"):
            for line in rotated.read_text().strip().split("\n"):
                if line:
                    json.loads(line)  # Should not raise
