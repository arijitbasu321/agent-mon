"""Tests for the get_io_info monitoring tool.

Covers:
- Returns disk I/O counters (read/write bytes and counts per disk)
- Returns network I/O counters (bytes, packets, errors, drops per NIC)
- Handles systems with no disks or no network interfaces
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.io import get_io_info


class TestGetIoInfo:
    """Test I/O info collection."""

    @pytest.fixture
    def mock_psutil(self):
        with patch("agent_mon.tools.io.psutil") as mock:
            # Disk I/O
            sda = MagicMock()
            sda.read_bytes = 1024 * 1024 * 500   # 500 MB
            sda.write_bytes = 1024 * 1024 * 200   # 200 MB
            sda.read_count = 10000
            sda.write_count = 5000
            mock.disk_io_counters.return_value = {"sda": sda}

            # Network I/O
            eth0 = MagicMock()
            eth0.bytes_sent = 1024 * 1024 * 100
            eth0.bytes_recv = 1024 * 1024 * 300
            eth0.packets_sent = 50000
            eth0.packets_recv = 80000
            eth0.errin = 0
            eth0.errout = 0
            eth0.dropin = 2
            eth0.dropout = 0
            mock.net_io_counters.return_value = {"eth0": eth0}

            yield mock

    def test_returns_string(self, mock_psutil):
        result = get_io_info()
        assert isinstance(result, str)

    def test_includes_disk_io(self, mock_psutil):
        result = get_io_info()
        assert "sda" in result

    def test_includes_network_io(self, mock_psutil):
        result = get_io_info()
        assert "eth0" in result

    def test_includes_network_errors(self, mock_psutil):
        result = get_io_info()
        # dropin=2, should be reported
        assert "drop" in result.lower() or "2" in result

    def test_handles_no_disk_io(self, mock_psutil):
        mock_psutil.disk_io_counters.return_value = {}
        result = get_io_info()
        assert isinstance(result, str)

    def test_handles_no_network(self, mock_psutil):
        mock_psutil.net_io_counters.return_value = {}
        result = get_io_info()
        assert isinstance(result, str)

    def test_handles_none_disk_io(self, mock_psutil):
        """On some systems, disk_io_counters returns None."""
        mock_psutil.disk_io_counters.return_value = None
        result = get_io_info()
        assert isinstance(result, str)

    def test_multiple_nics(self, mock_psutil):
        eth0 = MagicMock()
        eth0.bytes_sent = 100
        eth0.bytes_recv = 200
        eth0.packets_sent = 10
        eth0.packets_recv = 20
        eth0.errin = 0
        eth0.errout = 0
        eth0.dropin = 0
        eth0.dropout = 0

        lo = MagicMock()
        lo.bytes_sent = 50
        lo.bytes_recv = 50
        lo.packets_sent = 5
        lo.packets_recv = 5
        lo.errin = 0
        lo.errout = 0
        lo.dropin = 0
        lo.dropout = 0

        mock_psutil.net_io_counters.return_value = {"eth0": eth0, "lo": lo}

        result = get_io_info()
        assert "eth0" in result
        assert "lo" in result
