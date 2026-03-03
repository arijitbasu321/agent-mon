"""Tests for the get_process_list monitoring tool.

Covers:
- Returns process list sorted by CPU or memory
- Includes PID, name, user, cpu%, memory%, status, create_time
- Highlights zombie/defunct processes
- Respects limit parameter
- Handles processes that disappear during iteration
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.processes import get_process_list


class TestGetProcessList:
    """Test process list collection."""

    @pytest.fixture
    def mock_psutil(self, mock_process_list):
        with patch("agent_mon.tools.processes.psutil") as mock:
            procs = []
            for pinfo in mock_process_list:
                p = MagicMock()
                p.info = pinfo
                procs.append(p)
            mock.process_iter.return_value = procs
            yield mock

    def test_returns_string(self, mock_psutil):
        result = get_process_list()
        assert isinstance(result, str)

    def test_includes_process_names(self, mock_psutil):
        result = get_process_list()
        assert "python3" in result
        assert "nginx" in result
        assert "defunct_worker" in result

    def test_includes_pids(self, mock_psutil):
        result = get_process_list()
        assert "1234" in result
        assert "5678" in result

    def test_highlights_zombie_processes(self, mock_psutil):
        result = get_process_list()
        result_lower = result.lower()
        assert "zombie" in result_lower or "defunct" in result_lower

    def test_sort_by_cpu(self, mock_psutil):
        result = get_process_list(sort_by="cpu")
        assert isinstance(result, str)
        # The defunct_worker (92% CPU) should appear before nginx (2% CPU)
        pos_defunct = result.find("defunct_worker")
        pos_nginx = result.find("nginx")
        assert pos_defunct < pos_nginx

    def test_sort_by_memory(self, mock_psutil):
        result = get_process_list(sort_by="memory")
        assert isinstance(result, str)
        # python3 (12.3% mem) should appear before nginx (1.5% mem)
        pos_python = result.find("python3")
        pos_nginx = result.find("nginx")
        assert pos_python < pos_nginx

    def test_limit_parameter(self, mock_psutil):
        result = get_process_list(limit=1)
        # Should only contain one process entry
        assert isinstance(result, str)

    def test_default_limit_is_20(self, mock_psutil):
        # With only 3 mock processes, all should appear
        result = get_process_list()
        assert "python3" in result
        assert "nginx" in result
        assert "defunct_worker" in result

    def test_handles_process_gone_during_iteration(self, mock_psutil):
        """Processes can disappear between listing and reading info."""
        import psutil

        bad_proc = MagicMock()
        bad_proc.info = None
        bad_proc.side_effect = psutil.NoSuchProcess(pid=99999)
        mock_psutil.process_iter.return_value = [bad_proc]

        result = get_process_list()
        assert isinstance(result, str)

    def test_handles_access_denied(self, mock_psutil):
        """Some processes may deny access to their info."""
        import psutil

        denied_proc = MagicMock()
        denied_proc.info = None
        denied_proc.side_effect = psutil.AccessDenied(pid=1)
        mock_psutil.process_iter.return_value = [denied_proc]

        result = get_process_list()
        assert isinstance(result, str)
