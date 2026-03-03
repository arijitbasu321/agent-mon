"""Tests for the get_cpu_info monitoring tool.

Covers:
- Returns per-core usage percentages
- Returns overall CPU usage
- Returns load averages (1m, 5m, 15m)
- Returns top 5 CPU-consuming processes
- Handles psutil edge cases (single core, high load)
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.cpu import get_cpu_info


class TestGetCpuInfo:
    """Test CPU info collection."""

    @pytest.fixture
    def mock_psutil(self):
        with patch("agent_mon.tools.cpu.psutil") as mock:
            # Per-core usage
            mock.cpu_percent.return_value = 35.0
            mock.cpu_percent.side_effect = None

            # cpu_count
            mock.cpu_count.return_value = 4

            # Load averages
            mock.getloadavg.return_value = (1.5, 2.0, 1.8)

            # Per-core percentages (when percpu=True)
            def cpu_percent_handler(interval=None, percpu=False):
                if percpu:
                    return [30.0, 45.0, 20.0, 55.0]
                return 35.0

            mock.cpu_percent = cpu_percent_handler

            # Top processes
            proc1 = MagicMock()
            proc1.info = {
                "pid": 100,
                "name": "python3",
                "cpu_percent": 45.0,
            }
            proc2 = MagicMock()
            proc2.info = {
                "pid": 200,
                "name": "nginx",
                "cpu_percent": 12.0,
            }
            mock.process_iter.return_value = [proc1, proc2]

            yield mock

    def test_returns_string(self, mock_psutil):
        result = get_cpu_info()
        assert isinstance(result, str)

    def test_includes_overall_usage(self, mock_psutil):
        result = get_cpu_info()
        assert "35.0" in result or "35" in result

    def test_includes_per_core_usage(self, mock_psutil):
        result = get_cpu_info()
        assert "30.0" in result or "core" in result.lower()

    def test_includes_load_averages(self, mock_psutil):
        result = get_cpu_info()
        assert "1.5" in result
        assert "2.0" in result
        assert "1.8" in result

    def test_includes_top_processes(self, mock_psutil):
        result = get_cpu_info()
        assert "python3" in result
        assert "nginx" in result

    def test_handles_single_core(self, mock_psutil):
        mock_psutil.cpu_count.return_value = 1

        def cpu_percent_handler(interval=None, percpu=False):
            if percpu:
                return [50.0]
            return 50.0

        mock_psutil.cpu_percent = cpu_percent_handler
        result = get_cpu_info()
        assert isinstance(result, str)

    def test_handles_process_access_error(self, mock_psutil):
        """If a process disappears during iteration, it should be skipped."""
        import psutil

        bad_proc = MagicMock()
        bad_proc.info = None
        bad_proc.side_effect = psutil.NoSuchProcess(999)
        mock_psutil.process_iter.return_value = [bad_proc]

        # Should not raise
        result = get_cpu_info()
        assert isinstance(result, str)
