"""Tests for config loading, parsing, and validation.

Covers:
- Loading a valid YAML config file
- Default value population for optional fields
- New v2 fields: bash, memory
- Removed v1 fields: thresholds, stdout, log rotation
- Validation errors for missing required fields
- Validation errors for invalid values
- Environment variable requirements
"""

import pytest
import yaml

from agent_mon.config import Config, ConfigError


class TestConfigLoading:
    """Test basic config file loading."""

    def test_load_valid_config(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.check_interval == 300
        assert config.model == "claude-sonnet-4-6"
        assert config.max_turns == 100

    def test_load_minimal_config(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.check_interval == 300
        assert config.remediation.enabled is False

    def test_load_nonexistent_file_raises(self, tmp_path):
        with pytest.raises((ConfigError, FileNotFoundError)):
            Config.from_file(tmp_path / "nonexistent.yaml")

    def test_load_invalid_yaml_raises(self, tmp_path):
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{{not valid yaml: [}")
        with pytest.raises((ConfigError, yaml.YAMLError)):
            Config.from_file(bad_file)

    def test_load_empty_file_raises(self, tmp_path):
        empty_file = tmp_path / "empty.yaml"
        empty_file.write_text("")
        with pytest.raises(ConfigError):
            Config.from_file(empty_file)


class TestConfigAlerts:
    """Test alert configuration."""

    def test_alert_log_file(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.alerts.log_file == "/var/log/agent-mon.log"

    def test_email_config(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.alerts.email.enabled is True
        assert config.alerts.email.from_addr == "agent-mon@example.com"
        assert "ops@example.com" in config.alerts.email.to
        assert config.alerts.email.min_severity == "warning"
        assert config.alerts.email.dedup_window_minutes == 15

    def test_invalid_min_severity(self, sample_config_dict, tmp_path):
        sample_config_dict["alerts"]["email"]["min_severity"] = "panic"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="severity"):
            Config.from_file(config_path)

    def test_email_disabled_by_default_when_not_specified(self, minimal_config_dict, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(minimal_config_dict))
        config = Config.from_file(config_path)
        assert config.alerts.email.enabled is False

    def test_default_log_file(self, minimal_config_dict, tmp_path):
        del minimal_config_dict["alerts"]["log_file"]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(minimal_config_dict))
        config = Config.from_file(config_path)
        assert config.alerts.log_file == "/var/log/agent-mon.log"


class TestConfigRemediation:
    """Test remediation policy configuration."""

    def test_remediation_enabled(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.remediation.enabled is True
        # watched_containers auto-merged into allowed_restart_containers
        assert "nginx" in config.remediation.allowed_restart_containers
        assert "redis" in config.remediation.allowed_restart_containers
        assert "postgres" in config.remediation.allowed_restart_containers

    def test_allowed_restart_services(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert "nginx" in config.remediation.allowed_restart_services
        assert "docker" in config.remediation.allowed_restart_services

    def test_max_restart_attempts(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.remediation.max_restart_attempts == 3

    def test_remediation_disabled(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.remediation.enabled is False

    def test_remediation_enabled_with_empty_lists_raises(
        self, sample_config_dict, tmp_path
    ):
        sample_config_dict["remediation"]["allowed_restart_services"] = []
        sample_config_dict["watched_containers"] = []
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="remediation.*empty"):
            Config.from_file(config_path)


class TestConfigHeartbeat:
    """Test heartbeat configuration."""

    def test_heartbeat_defaults(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.heartbeat.enabled is False
        assert config.heartbeat.interval == 3600

    def test_heartbeat_enabled(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.heartbeat.enabled is True
        assert config.heartbeat.interval == 3600

    def test_heartbeat_interval_below_minimum(self, sample_config_dict, tmp_path):
        sample_config_dict["heartbeat"]["interval"] = 30
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="heartbeat interval"):
            Config.from_file(config_path)


class TestConfigBash:
    """Test bash deny-list configuration."""

    def test_bash_deny_list(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert "rm -rf /" in config.bash.deny_list
        assert "shutdown -h" in config.bash.deny_list
        assert "reboot" in config.bash.deny_list

    def test_bash_deny_list_default_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.bash.deny_list == []


class TestConfigMemory:
    """Test memory configuration."""

    def test_memory_config(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.memory.enabled is True
        assert config.memory.path == "/tmp/test-agent-mon-memory"
        assert config.memory.collection_name == "test_memory"
        assert config.memory.max_results == 5

    def test_memory_defaults(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.memory.enabled is True
        assert config.memory.path == "/var/lib/agent-mon/memory"
        assert config.memory.collection_name == "agent_mon_memory"
        assert config.memory.max_results == 5


class TestConfigWatchedProcesses:
    """Test watched processes configuration."""

    def test_watched_processes_parsed(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert len(config.watched_processes) == 2
        assert config.watched_processes[0].name == "my-api-server"
        assert config.watched_processes[0].restart_command == "systemctl restart my-api-server"

    def test_watched_processes_default_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.watched_processes == []

    def test_watched_process_missing_name_raises(self, sample_config_dict, tmp_path):
        sample_config_dict["watched_processes"] = [
            {"restart_command": "systemctl restart foo"}
        ]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="name"):
            Config.from_file(config_path)


class TestConfigWatchedContainers:
    """Test watched containers configuration."""

    def test_watched_containers_parsed(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.watched_containers == ["nginx", "redis", "postgres"]

    def test_watched_containers_merged_into_remediation(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        for c in config.watched_containers:
            assert c in config.remediation.allowed_restart_containers

    def test_watched_containers_default_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.watched_containers == []


class TestConfigValidation:
    """Test validation rules that should reject bad configs at startup."""

    def test_missing_check_interval(self, sample_config_dict, tmp_path):
        del sample_config_dict["check_interval"]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="check_interval"):
            Config.from_file(config_path)

    def test_check_interval_below_minimum(self, sample_config_dict, tmp_path):
        sample_config_dict["check_interval"] = 10  # below 30s minimum
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="30"):
            Config.from_file(config_path)

    def test_check_interval_at_minimum(self, sample_config_dict, tmp_path):
        sample_config_dict["check_interval"] = 30
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        config = Config.from_file(config_path)
        assert config.check_interval == 30

    def test_missing_model(self, sample_config_dict, tmp_path):
        del sample_config_dict["model"]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="model"):
            Config.from_file(config_path)

    def test_max_restart_attempts_non_positive(self, sample_config_dict, tmp_path):
        sample_config_dict["remediation"]["max_restart_attempts"] = 0
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError):
            Config.from_file(config_path)

    def test_max_turns_non_positive(self, sample_config_dict, tmp_path):
        sample_config_dict["max_turns"] = 0
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError):
            Config.from_file(config_path)


class TestConfigEnvironment:
    """Test environment variable checks."""

    def test_validate_env_with_keys(self, config_yaml_file, env_with_api_keys):
        config = Config.from_file(config_yaml_file)
        # Should not raise
        config.validate_env()

    def test_validate_env_missing_anthropic_key(
        self, config_yaml_file, env_without_api_keys
    ):
        config = Config.from_file(config_yaml_file)
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
            config.validate_env()

    def test_validate_env_missing_resend_key_when_email_enabled(
        self, config_yaml_file, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        config = Config.from_file(config_yaml_file)
        with pytest.raises(ConfigError, match="RESEND_API_KEY"):
            config.validate_env()

    def test_validate_env_resend_key_not_required_when_email_disabled(
        self, minimal_config_yaml_file, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        config = Config.from_file(minimal_config_yaml_file)
        # Should not raise -- email is not enabled and heartbeat is not enabled
        config.validate_env()
