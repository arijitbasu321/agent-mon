"""Tests for the CLI entry point and argument parsing.

Covers:
- --config flag defaults to /etc/agent-mon/config.yaml
- --once flag triggers one-shot mode
- --interactive flag triggers interactive mode
- Default mode is daemon (continuous)
- Missing config file error
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_mon.cli import parse_args, main


class TestArgParsing:
    """Test CLI argument parsing."""

    def test_config_flag(self):
        args = parse_args(["--config", "/etc/agent-mon/config.yaml"])
        assert args.config == "/etc/agent-mon/config.yaml"

    def test_config_default(self):
        args = parse_args([])
        assert args.config == "/etc/agent-mon/config.yaml"

    def test_once_flag(self):
        args = parse_args(["--once", "--config", "config.yaml"])
        assert args.once is True

    def test_interactive_flag(self):
        args = parse_args(["--interactive", "--config", "config.yaml"])
        assert args.interactive is True

    def test_default_mode_is_daemon(self):
        args = parse_args(["--config", "config.yaml"])
        assert args.once is False
        assert args.interactive is False

    def test_once_and_interactive_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse_args(["--once", "--interactive", "--config", "config.yaml"])


class TestMainEntryPoint:
    """Test the main() function orchestration."""

    @patch("agent_mon.cli.Config")
    @patch("agent_mon.cli.AgentDaemon")
    def test_main_loads_config(self, mock_daemon_cls, mock_config_cls, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("check_interval: 300\n")
        mock_config = MagicMock()
        mock_config_cls.from_file.return_value = mock_config

        mock_daemon = MagicMock()
        mock_daemon.run = AsyncMock()
        mock_daemon._run_check_cycle = AsyncMock()
        mock_daemon_cls.return_value = mock_daemon

        with patch("sys.argv", ["agent-mon", "--config", str(config_path), "--once"]):
            main()

        mock_config_cls.from_file.assert_called_once_with(str(config_path))

    @patch("agent_mon.cli.Config")
    def test_main_exits_on_missing_config(self, mock_config_cls):
        mock_config_cls.from_file.side_effect = FileNotFoundError("not found")

        with patch("sys.argv", ["agent-mon", "--config", "/no/such/file.yaml"]):
            with pytest.raises(SystemExit):
                main()

    @patch("agent_mon.cli.Config")
    def test_main_exits_on_invalid_config(self, mock_config_cls):
        from agent_mon.config import ConfigError

        mock_config_cls.from_file.side_effect = ConfigError("bad config")

        with patch("sys.argv", ["agent-mon", "--config", "bad.yaml"]):
            with pytest.raises(SystemExit):
                main()

    @patch("agent_mon.cli.Config")
    @patch("agent_mon.cli.AgentDaemon")
    def test_main_validates_env(self, mock_daemon_cls, mock_config_cls, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("check_interval: 300\n")

        mock_config = MagicMock()
        mock_config_cls.from_file.return_value = mock_config

        mock_daemon = MagicMock()
        mock_daemon.run = AsyncMock()
        mock_daemon._run_check_cycle = AsyncMock()
        mock_daemon_cls.return_value = mock_daemon

        with patch("sys.argv", ["agent-mon", "--config", str(config_path), "--once"]):
            main()

        mock_config.validate_env.assert_called_once()
