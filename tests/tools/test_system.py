"""Tests for the get_system_issues monitoring tool.

Covers:
- Uptime
- Kernel OOM killer events
- Systemd failed units
- Pending package updates
- NTP sync status
- Handles missing/unavailable subsystems
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.system import get_system_issues


class TestGetSystemIssues:
    """Test system issues collection."""

    @pytest.fixture
    def mock_deps(self):
        with patch("agent_mon.tools.system.psutil") as mock_psutil, \
             patch("agent_mon.tools.system.subprocess") as mock_subprocess:

            # Preserve real exception class so 'except subprocess.TimeoutExpired' works
            mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired

            # Uptime via psutil.boot_time
            mock_psutil.boot_time.return_value = 1709000000.0

            # subprocess.run for various checks
            def run_handler(cmd, *args, **kwargs):
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""

                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

                if "systemctl" in cmd_str and "failed" in cmd_str:
                    result.stdout = ""  # no failed units
                elif "dmesg" in cmd_str and "oom" in cmd_str.lower():
                    result.stdout = ""  # no OOM events
                elif "timedatectl" in cmd_str:
                    result.stdout = "System clock synchronized: yes"
                elif "apt" in cmd_str or "dnf" in cmd_str:
                    result.stdout = ""  # no updates

                return result

            mock_subprocess.run = run_handler

            yield {
                "psutil": mock_psutil,
                "subprocess": mock_subprocess,
            }

    def test_returns_string(self, mock_deps):
        result = get_system_issues()
        assert isinstance(result, str)

    def test_includes_uptime(self, mock_deps):
        result = get_system_issues()
        result_lower = result.lower()
        assert "uptime" in result_lower or "boot" in result_lower

    def test_reports_oom_events(self, mock_deps):
        with patch("agent_mon.tools.system.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            def run_handler(cmd, *args, **kwargs):
                result = MagicMock()
                result.returncode = 0
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "dmesg" in cmd_str:
                    result.stdout = (
                        "[ 1234.567] Out of memory: Killed process 5678 "
                        "(java-app) total-vm:8192kB\n"
                    )
                else:
                    result.stdout = ""
                return result

            mock_sub.run = run_handler
            mock_deps["psutil"].boot_time.return_value = 1709000000.0

            result = get_system_issues()
            result_lower = result.lower()
            assert "oom" in result_lower or "out of memory" in result_lower or "java-app" in result

    def test_reports_failed_systemd_units(self, mock_deps):
        with patch("agent_mon.tools.system.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            def run_handler(cmd, *args, **kwargs):
                result = MagicMock()
                result.returncode = 0
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "systemctl" in cmd_str and "failed" in cmd_str:
                    result.stdout = "  nginx.service    loaded failed failed\n"
                else:
                    result.stdout = ""
                return result

            mock_sub.run = run_handler
            mock_deps["psutil"].boot_time.return_value = 1709000000.0

            result = get_system_issues()
            assert "nginx" in result

    def test_reports_ntp_not_synced(self, mock_deps):
        with patch("agent_mon.tools.system.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            def run_handler(cmd, *args, **kwargs):
                result = MagicMock()
                result.returncode = 0
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "timedatectl" in cmd_str:
                    result.stdout = "System clock synchronized: no"
                else:
                    result.stdout = ""
                return result

            mock_sub.run = run_handler
            mock_deps["psutil"].boot_time.return_value = 1709000000.0

            result = get_system_issues()
            result_lower = result.lower()
            assert "ntp" in result_lower or "sync" in result_lower or "clock" in result_lower

    def test_handles_systemctl_not_available(self, mock_deps):
        with patch("agent_mon.tools.system.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            def run_handler(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "systemctl" in cmd_str:
                    raise FileNotFoundError("systemctl not found")
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                return result

            mock_sub.run = run_handler
            mock_deps["psutil"].boot_time.return_value = 1709000000.0

            result = get_system_issues()
            assert isinstance(result, str)

    def test_handles_dmesg_permission_error(self, mock_deps):
        with patch("agent_mon.tools.system.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired

            def run_handler(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
                if "dmesg" in cmd_str:
                    raise PermissionError("dmesg requires root")
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                return result

            mock_sub.run = run_handler
            mock_deps["psutil"].boot_time.return_value = 1709000000.0

            result = get_system_issues()
            assert isinstance(result, str)
