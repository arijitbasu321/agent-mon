"""Tests for the get_memory_info monitoring tool.

Covers:
- Returns RAM usage (total, used, available, percent)
- Returns swap usage (total, used, percent)
- Returns top 5 memory-consuming processes
- Handles no swap configured
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.memory import get_memory_info


class TestGetMemoryInfo:
    """Test memory info collection."""

    @pytest.fixture
    def mock_psutil(self, mock_virtual_memory, mock_swap_memory):
        with patch("agent_mon.tools.memory.psutil") as mock:
            mock.virtual_memory.return_value = mock_virtual_memory
            mock.swap_memory.return_value = mock_swap_memory

            proc1 = MagicMock()
            proc1.info = {
                "pid": 100,
                "name": "java-app",
                "memory_percent": 25.0,
                "memory_info": MagicMock(rss=4 * 1024 * 1024 * 1024),
            }
            proc2 = MagicMock()
            proc2.info = {
                "pid": 200,
                "name": "postgres",
                "memory_percent": 15.0,
                "memory_info": MagicMock(rss=2 * 1024 * 1024 * 1024),
            }
            mock.process_iter.return_value = [proc1, proc2]

            yield mock

    def test_returns_string(self, mock_psutil):
        result = get_memory_info()
        assert isinstance(result, str)

    def test_includes_ram_percent(self, mock_psutil):
        result = get_memory_info()
        assert "75.0" in result or "75" in result

    def test_includes_swap_percent(self, mock_psutil):
        result = get_memory_info()
        assert "12.5" in result or "12" in result

    def test_includes_total_ram(self, mock_psutil):
        result = get_memory_info()
        # 16 GB in some human-readable format
        assert "16" in result or "17179869184" in result

    def test_includes_top_memory_processes(self, mock_psutil):
        result = get_memory_info()
        assert "java-app" in result
        assert "postgres" in result

    def test_handles_no_swap(self, mock_psutil):
        no_swap = MagicMock()
        no_swap.total = 0
        no_swap.used = 0
        no_swap.percent = 0.0
        mock_psutil.swap_memory.return_value = no_swap

        result = get_memory_info()
        assert isinstance(result, str)

    def test_handles_high_memory_usage(self, mock_psutil):
        high_mem = MagicMock()
        high_mem.total = 16 * 1024 * 1024 * 1024
        high_mem.used = 15 * 1024 * 1024 * 1024
        high_mem.available = 1 * 1024 * 1024 * 1024
        high_mem.percent = 94.0
        mock_psutil.virtual_memory.return_value = high_mem

        result = get_memory_info()
        assert "94" in result
