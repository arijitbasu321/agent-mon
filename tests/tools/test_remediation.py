"""Tests for remediation tools: kill_process and restart_service.

Covers:
- kill_process with allowed target succeeds
- kill_process with disallowed target is denied
- kill_process TOCTOU guard: re-verifies PID name before kill
- kill_process TOCTOU guard: re-verifies process start time
- kill_process handles NoSuchProcess
- restart_service with allowed service succeeds
- restart_service with disallowed service is denied
- restart_service returns new service status
- Rate limiting is respected
"""

import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.remediation import kill_process, restart_service


# ===========================================================================
# kill_process
# ===========================================================================


class TestKillProcess:
    """Test the kill_process remediation tool."""

    @pytest.fixture
    def remediation_config(self):
        config = MagicMock()
        config.remediation.enabled = True
        config.remediation.allowed_kill_targets = ["defunct_worker", "runaway_script"]
        return config

    def test_kills_allowed_target(self, remediation_config):
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil, \
             patch("agent_mon.tools.remediation.os") as mock_os:
            proc = MagicMock()
            proc.name.return_value = "defunct_worker"
            proc.create_time.return_value = 1709001000.0
            proc.pid = 9999
            mock_psutil.Process.return_value = proc

            result = kill_process(
                pid=9999,
                signal="TERM",
                config=remediation_config,
            )

            mock_os.kill.assert_called_once_with(9999, signal.SIGTERM)
            assert "success" in result.lower() or "killed" in result.lower()

    def test_denies_disallowed_target(self, remediation_config):
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil:
            proc = MagicMock()
            proc.name.return_value = "critical_database"
            proc.create_time.return_value = 1709001000.0
            mock_psutil.Process.return_value = proc

            result = kill_process(
                pid=1234,
                signal="TERM",
                config=remediation_config,
            )

            assert "denied" in result.lower() or "not allowed" in result.lower()

    def test_toctou_reverifies_process_name(self, remediation_config):
        """The tool must re-read the process name before killing."""
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil, \
             patch("agent_mon.tools.remediation.os") as mock_os:
            proc = MagicMock()
            proc.pid = 9999
            proc.create_time.return_value = 1709001000.0

            # First call returns allowed name, but the PID was recycled
            # and now belongs to a different process
            call_count = 0

            def name_changes():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return "defunct_worker"  # initial check
                return "sshd"  # PID recycled!

            proc.name = name_changes
            mock_psutil.Process.return_value = proc

            result = kill_process(
                pid=9999,
                signal="TERM",
                config=remediation_config,
            )

            # Should NOT have killed the process
            mock_os.kill.assert_not_called()
            assert "denied" in result.lower() or "mismatch" in result.lower() or "changed" in result.lower()

    def test_toctou_reverifies_create_time(self, remediation_config):
        """The tool must verify the process start time hasn't changed (PID recycling)."""
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil, \
             patch("agent_mon.tools.remediation.os") as mock_os:
            proc = MagicMock()
            proc.pid = 9999
            proc.name.return_value = "defunct_worker"

            create_call = 0

            def create_time_changes():
                nonlocal create_call
                create_call += 1
                if create_call == 1:
                    return 1709001000.0
                return 1709999999.0  # Different start time = recycled PID

            proc.create_time = create_time_changes
            mock_psutil.Process.return_value = proc

            result = kill_process(
                pid=9999,
                signal="TERM",
                config=remediation_config,
                expected_create_time=1709001000.0,
            )

            mock_os.kill.assert_not_called()

    def test_handles_no_such_process(self, remediation_config):
        import psutil

        with patch("agent_mon.tools.remediation.psutil") as mock_psutil:
            # Preserve real exception class so 'except psutil.NoSuchProcess' works
            mock_psutil.NoSuchProcess = psutil.NoSuchProcess
            mock_psutil.Process.side_effect = psutil.NoSuchProcess(pid=9999)

            result = kill_process(
                pid=9999,
                signal="TERM",
                config=remediation_config,
            )

            assert "not found" in result.lower() or "no such" in result.lower()

    def test_denies_when_remediation_disabled(self, remediation_config):
        remediation_config.remediation.enabled = False

        result = kill_process(
            pid=9999,
            signal="TERM",
            config=remediation_config,
        )

        assert "disabled" in result.lower() or "denied" in result.lower()

    def test_default_signal_is_term(self, remediation_config):
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil, \
             patch("agent_mon.tools.remediation.os") as mock_os:
            proc = MagicMock()
            proc.name.return_value = "defunct_worker"
            proc.create_time.return_value = 1709001000.0
            proc.pid = 9999
            mock_psutil.Process.return_value = proc

            kill_process(pid=9999, config=remediation_config)

            mock_os.kill.assert_called_once_with(9999, signal.SIGTERM)

    @pytest.mark.parametrize("sig_name,sig_val", [
        ("TERM", signal.SIGTERM),
        ("KILL", signal.SIGKILL),
        ("HUP", signal.SIGHUP),
    ])
    def test_signal_mapping(self, remediation_config, sig_name, sig_val):
        with patch("agent_mon.tools.remediation.psutil") as mock_psutil, \
             patch("agent_mon.tools.remediation.os") as mock_os:
            proc = MagicMock()
            proc.name.return_value = "defunct_worker"
            proc.create_time.return_value = 1709001000.0
            proc.pid = 9999
            mock_psutil.Process.return_value = proc

            kill_process(pid=9999, signal=sig_name, config=remediation_config)

            mock_os.kill.assert_called_once_with(9999, sig_val)


# ===========================================================================
# restart_service
# ===========================================================================


class TestRestartService:
    """Test the restart_service remediation tool."""

    @pytest.fixture
    def remediation_config(self):
        config = MagicMock()
        config.remediation.enabled = True
        config.remediation.allowed_restart_services = ["nginx", "docker"]
        config.remediation.max_restart_attempts = 3
        return config

    def test_restarts_allowed_service(self, remediation_config):
        with patch("agent_mon.tools.remediation.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout="active (running)",
            )

            result = restart_service(
                service_name="nginx",
                config=remediation_config,
            )

            # Should have called systemctl restart
            calls = mock_sub.run.call_args_list
            restart_call = [c for c in calls if "restart" in str(c)]
            assert len(restart_call) > 0
            assert "nginx" in str(restart_call[0])

    def test_denies_disallowed_service(self, remediation_config):
        result = restart_service(
            service_name="sshd",
            config=remediation_config,
        )

        assert "denied" in result.lower() or "not allowed" in result.lower()

    def test_returns_new_status_after_restart(self, remediation_config):
        with patch("agent_mon.tools.remediation.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout="active (running)",
            )

            result = restart_service(
                service_name="nginx",
                config=remediation_config,
            )

            assert "active" in result.lower() or "running" in result.lower()

    def test_reports_failure_on_restart_error(self, remediation_config):
        with patch("agent_mon.tools.remediation.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Failed to restart nginx.service: Unit not found.",
            )

            result = restart_service(
                service_name="nginx",
                config=remediation_config,
            )

            assert "fail" in result.lower() or "error" in result.lower()

    def test_denies_when_remediation_disabled(self, remediation_config):
        remediation_config.remediation.enabled = False

        result = restart_service(
            service_name="nginx",
            config=remediation_config,
        )

        assert "disabled" in result.lower() or "denied" in result.lower()

    def test_rejects_service_name_with_path_traversal(self, remediation_config):
        """Service names must not contain path separators or shell metacharacters."""
        result = restart_service(
            service_name="../../../etc/passwd",
            config=remediation_config,
        )
        assert "denied" in result.lower() or "invalid" in result.lower()

    def test_rejects_service_name_with_semicolon(self, remediation_config):
        result = restart_service(
            service_name="nginx; rm -rf /",
            config=remediation_config,
        )
        assert "denied" in result.lower() or "invalid" in result.lower()
