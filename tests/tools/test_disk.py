"""Tests for the get_disk_info monitoring tool.

Covers:
- Returns per-partition usage (mountpoint, total, used, free, percent)
- Flags partitions above 85% usage
- Handles multiple partitions
- Handles permission errors on some mountpoints
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_mon.tools.disk import get_disk_info


class TestGetDiskInfo:
    """Test disk info collection."""

    @pytest.fixture
    def mock_psutil(self, mock_disk_partitions, mock_disk_usage_normal):
        with patch("agent_mon.tools.disk.psutil") as mock:
            mock.disk_partitions.return_value = mock_disk_partitions
            mock.disk_usage.return_value = mock_disk_usage_normal
            yield mock

    def test_returns_string(self, mock_psutil):
        result = get_disk_info()
        assert isinstance(result, str)

    def test_includes_mountpoints(self, mock_psutil):
        result = get_disk_info()
        assert "/" in result
        assert "/data" in result

    def test_includes_usage_percent(self, mock_psutil):
        result = get_disk_info()
        assert "60" in result

    def test_flags_high_usage_partition(self, mock_psutil, mock_disk_usage_critical):
        """Partitions above 85% should be flagged."""

        def usage_by_mount(path):
            if path == "/":
                return mock_disk_usage_critical  # 97%
            mock = MagicMock()
            mock.percent = 40.0
            mock.total = 100 * 1024 * 1024 * 1024
            mock.used = 40 * 1024 * 1024 * 1024
            mock.free = 60 * 1024 * 1024 * 1024
            return mock

        mock_psutil.disk_usage.side_effect = usage_by_mount

        result = get_disk_info()
        assert "97" in result
        # Should contain a warning indicator for the high partition
        result_lower = result.lower()
        assert "warning" in result_lower or "!" in result or "critical" in result_lower or "high" in result_lower

    def test_handles_permission_error(self, mock_psutil):
        """Some mountpoints (like /snap) may raise PermissionError."""
        mock_psutil.disk_usage.side_effect = PermissionError("access denied")
        # Should not raise
        result = get_disk_info()
        assert isinstance(result, str)

    def test_handles_empty_partitions(self, mock_psutil):
        mock_psutil.disk_partitions.return_value = []
        result = get_disk_info()
        assert isinstance(result, str)

    def test_multiple_partitions_all_shown(self, mock_psutil):
        parts = []
        for i, mp in enumerate(["/" , "/home", "/var", "/tmp"]):
            p = MagicMock()
            p.mountpoint = mp
            p.device = f"/dev/sd{chr(97+i)}"
            p.fstype = "ext4"
            parts.append(p)
        mock_psutil.disk_partitions.return_value = parts

        result = get_disk_info()
        for mp in ["/home", "/var", "/tmp"]:
            assert mp in result
