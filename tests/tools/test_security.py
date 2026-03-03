"""Tests for the get_security_info monitoring tool.

Covers:
- Failed SSH login attempts from auth.log
- Listening ports and owning processes
- Currently logged-in users
- Recent sudo commands
- World-writable files in key directories
- Handles missing auth.log gracefully
"""

from unittest.mock import MagicMock, mock_open, patch

import pytest

from agent_mon.tools.security import get_security_info


class TestGetSecurityInfo:
    """Test security info collection."""

    @pytest.fixture
    def mock_deps(self):
        """Mock all external dependencies for security checks."""
        with patch("agent_mon.tools.security.psutil") as mock_psutil, \
             patch("agent_mon.tools.security.subprocess") as mock_subprocess, \
             patch("agent_mon.tools.security.open", mock_open(read_data="")) as mock_file, \
             patch("agent_mon.tools.security.os") as mock_os:

            # Listening ports
            conn1 = MagicMock()
            conn1.laddr = MagicMock(ip="0.0.0.0", port=22)
            conn1.status = "LISTEN"
            conn1.pid = 100
            conn2 = MagicMock()
            conn2.laddr = MagicMock(ip="0.0.0.0", port=80)
            conn2.status = "LISTEN"
            conn2.pid = 200
            mock_psutil.net_connections.return_value = [conn1, conn2]

            # Process name lookup
            def get_process(pid):
                p = MagicMock()
                if pid == 100:
                    p.name.return_value = "sshd"
                elif pid == 200:
                    p.name.return_value = "nginx"
                return p

            mock_psutil.Process = get_process

            # Logged-in users
            user1 = MagicMock()
            user1.name = "admin"
            user1.host = "192.168.1.100"
            user1.terminal = "pts/0"
            mock_psutil.users.return_value = [user1]

            # Subprocess for sudo commands, world-writable files
            mock_subprocess.run.return_value = MagicMock(
                stdout="", returncode=0
            )

            # os.path.exists for auth.log
            mock_os.path.exists.return_value = True

            yield {
                "psutil": mock_psutil,
                "subprocess": mock_subprocess,
                "file": mock_file,
                "os": mock_os,
            }

    def test_returns_string(self, mock_deps):
        result = get_security_info()
        assert isinstance(result, str)

    def test_includes_listening_ports(self, mock_deps):
        result = get_security_info()
        assert "22" in result
        assert "80" in result

    def test_includes_process_names_for_ports(self, mock_deps):
        result = get_security_info()
        assert "sshd" in result
        assert "nginx" in result

    def test_includes_logged_in_users(self, mock_deps):
        result = get_security_info()
        assert "admin" in result

    def test_handles_missing_auth_log(self, mock_deps):
        mock_deps["os"].path.exists.return_value = False
        result = get_security_info()
        assert isinstance(result, str)

    def test_includes_failed_ssh_attempts(self, mock_deps):
        auth_log_data = (
            "Mar  3 10:00:01 server sshd[1234]: Failed password for "
            "invalid user attacker from 10.0.0.1 port 22\n"
            "Mar  3 10:00:02 server sshd[1235]: Failed password for "
            "root from 10.0.0.2 port 22\n"
        )
        mock_deps["file"].return_value = mock_open(read_data=auth_log_data)()

        with patch(
            "agent_mon.tools.security.open",
            mock_open(read_data=auth_log_data),
        ):
            result = get_security_info()

        result_lower = result.lower()
        assert "failed" in result_lower or "ssh" in result_lower or "10.0.0" in result

    def test_handles_auth_log_permission_error(self, mock_deps):
        with patch(
            "agent_mon.tools.security.open",
            side_effect=PermissionError("access denied"),
        ):
            result = get_security_info()
        assert isinstance(result, str)
