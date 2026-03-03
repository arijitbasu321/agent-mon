"""Shared fixtures for agent-mon tests."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Sample config dict matching config.yaml from DESIGN.md
# ---------------------------------------------------------------------------

SAMPLE_CONFIG_DICT = {
    "check_interval": 300,
    "model": "claude-sonnet-4-6",
    "max_turns": 25,
    "thresholds": {
        "cpu_warning": 80,
        "cpu_critical": 95,
        "memory_warning": 80,
        "memory_critical": 95,
        "disk_warning": 85,
        "disk_critical": 95,
        "swap_warning": 50,
    },
    "alerts": {
        "stdout": True,
        "log_file": "/var/log/agent-mon/alerts.jsonl",
        "log_max_size_mb": 10,
        "log_max_files": 5,
        "email": {
            "enabled": True,
            "from": "agent-mon@example.com",
            "to": ["ops@example.com"],
            "min_severity": "warning",
            "dedup_window_minutes": 15,
        },
    },
    "docker": {
        "enabled": True,
    },
    "remediation": {
        "enabled": True,
        "allowed_restart_containers": ["nginx", "redis", "postgres"],
        "allowed_restart_services": ["nginx", "docker"],
        "allowed_kill_targets": ["defunct_worker"],
        "max_restart_attempts": 3,
    },
}

MINIMAL_CONFIG_DICT = {
    "check_interval": 300,
    "model": "claude-sonnet-4-6",
    "max_turns": 25,
    "thresholds": {
        "cpu_warning": 80,
        "cpu_critical": 95,
        "memory_warning": 80,
        "memory_critical": 95,
        "disk_warning": 85,
        "disk_critical": 95,
        "swap_warning": 50,
    },
    "alerts": {
        "stdout": True,
        "log_file": "/tmp/test-alerts.jsonl",
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
    """Return path to a temp alerts.jsonl file."""
    return alerts_log_dir / "alerts.jsonl"


@pytest.fixture
def env_with_api_keys(monkeypatch):
    """Set required environment variables for API keys."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_456")


@pytest.fixture
def env_without_api_keys(monkeypatch):
    """Ensure API key environment variables are not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)


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


# ---------------------------------------------------------------------------
# psutil mock data
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cpu_times_percent():
    """Mock psutil.cpu_times_percent return value."""
    mock = MagicMock()
    mock.user = 25.0
    mock.system = 10.0
    mock.idle = 60.0
    mock.iowait = 5.0
    return mock


@pytest.fixture
def mock_cpu_percent_per_core():
    """Mock psutil.cpu_percent(percpu=True) return value."""
    return [30.0, 45.0, 20.0, 55.0]


@pytest.fixture
def mock_virtual_memory():
    """Mock psutil.virtual_memory return value."""
    mock = MagicMock()
    mock.total = 16 * 1024 * 1024 * 1024  # 16 GB
    mock.used = 12 * 1024 * 1024 * 1024   # 12 GB
    mock.available = 4 * 1024 * 1024 * 1024  # 4 GB
    mock.percent = 75.0
    return mock


@pytest.fixture
def mock_swap_memory():
    """Mock psutil.swap_memory return value."""
    mock = MagicMock()
    mock.total = 8 * 1024 * 1024 * 1024  # 8 GB
    mock.used = 1 * 1024 * 1024 * 1024   # 1 GB
    mock.percent = 12.5
    return mock


@pytest.fixture
def mock_disk_partitions():
    """Mock psutil.disk_partitions return value."""
    root = MagicMock()
    root.mountpoint = "/"
    root.device = "/dev/sda1"
    root.fstype = "ext4"

    data = MagicMock()
    data.mountpoint = "/data"
    data.device = "/dev/sdb1"
    data.fstype = "ext4"

    return [root, data]


@pytest.fixture
def mock_disk_usage_normal():
    """Mock psutil.disk_usage for a normal partition."""
    mock = MagicMock()
    mock.total = 100 * 1024 * 1024 * 1024  # 100 GB
    mock.used = 60 * 1024 * 1024 * 1024    # 60 GB
    mock.free = 40 * 1024 * 1024 * 1024    # 40 GB
    mock.percent = 60.0
    return mock


@pytest.fixture
def mock_disk_usage_critical():
    """Mock psutil.disk_usage for a critically full partition."""
    mock = MagicMock()
    mock.total = 100 * 1024 * 1024 * 1024  # 100 GB
    mock.used = 97 * 1024 * 1024 * 1024    # 97 GB
    mock.free = 3 * 1024 * 1024 * 1024     # 3 GB
    mock.percent = 97.0
    return mock


@pytest.fixture
def mock_process_list():
    """Return a list of mock process info dicts."""
    return [
        {
            "pid": 1234,
            "name": "python3",
            "username": "app",
            "cpu_percent": 45.0,
            "memory_percent": 12.3,
            "status": "running",
            "create_time": 1709000000.0,
        },
        {
            "pid": 5678,
            "name": "nginx",
            "username": "www-data",
            "cpu_percent": 2.0,
            "memory_percent": 1.5,
            "status": "sleeping",
            "create_time": 1709000100.0,
        },
        {
            "pid": 9999,
            "name": "defunct_worker",
            "username": "app",
            "cpu_percent": 92.0,
            "memory_percent": 0.1,
            "status": "zombie",
            "create_time": 1709001000.0,
        },
    ]
