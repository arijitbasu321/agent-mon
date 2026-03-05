"""Shared fixtures for agent-mon tests."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml


# ---------------------------------------------------------------------------
# Sample config dict matching new v2 schema
# ---------------------------------------------------------------------------

SAMPLE_CONFIG_DICT = {
    "check_interval": 300,
    "model": "claude-sonnet-4-6",
    "max_turns": 100,
    "heartbeat": {
        "enabled": True,
        "interval": 3600,
    },
    "watched_processes": [
        {"name": "my-api-server", "restart_command": "systemctl restart my-api-server"},
        {"name": "background-worker", "restart_command": "systemctl restart background-worker"},
    ],
    "watched_containers": ["nginx", "redis", "postgres"],
    "alerts": {
        "log_file": "/var/log/agent-mon.log",
        "email": {
            "enabled": True,
            "from": "agent-mon@example.com",
            "to": ["ops@example.com"],
            "min_severity": "warning",
            "dedup_window_minutes": 15,
        },
        "slack": {
            "enabled": True,
            "min_severity": "warning",
            "dedup_window_minutes": 15,
        },
    },
    "docker": {
        "enabled": True,
    },
    "remediation": {
        "enabled": True,
        "allowed_restart_services": ["nginx", "docker"],
        "max_restart_attempts": 3,
    },
    "bash": {
        "deny_list": [
            "rm -rf /",
            "shutdown -h",
            "shutdown -r",
            "shutdown -P",
            "shutdown now",
            "reboot",
            "mkfs",
            "dd if=",
        ],
    },
    "memory": {
        "enabled": True,
        "path": "/tmp/test-agent-mon-memory",
        "collection_name": "test_memory",
        "max_results": 5,
    },
}

MINIMAL_CONFIG_DICT = {
    "check_interval": 300,
    "model": "claude-sonnet-4-6",
    "max_turns": 100,
    "alerts": {
        "log_file": "/tmp/test-alerts.log",
    },
    "remediation": {
        "enabled": False,
    },
}


@pytest.fixture
def sample_config_dict():
    """Return a deep copy of the full sample config dict."""
    return json.loads(json.dumps(SAMPLE_CONFIG_DICT))


@pytest.fixture
def minimal_config_dict():
    """Return a deep copy of the minimal config dict."""
    return json.loads(json.dumps(MINIMAL_CONFIG_DICT))


@pytest.fixture
def config_yaml_file(sample_config_dict, tmp_path):
    """Write sample config to a temp YAML file and return its path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    return config_path


@pytest.fixture
def minimal_config_yaml_file(minimal_config_dict, tmp_path):
    """Write minimal config to a temp YAML file and return its path."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(minimal_config_dict))
    return config_path


@pytest.fixture
def alerts_log_dir(tmp_path):
    """Create and return a temp directory for alert logs."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def alerts_log_file(alerts_log_dir):
    """Return path to a temp alerts.log file."""
    return alerts_log_dir / "alerts.log"


@pytest.fixture
def env_with_api_keys(monkeypatch):
    """Set required environment variables for API keys."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_456")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T00/B00/xxx")


@pytest.fixture
def env_without_api_keys(monkeypatch):
    """Ensure API key environment variables are not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)


@pytest.fixture
def mock_aiohttp_session():
    """Return a mock aiohttp.ClientSession with async context manager support."""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.json = AsyncMock(return_value={"id": "email-123"})
    session.post = AsyncMock(return_value=response)
    session.close = AsyncMock()
    return session
