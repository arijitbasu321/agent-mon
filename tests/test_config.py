"""Tests for config loading, parsing, and validation.

Covers:
- Loading a valid YAML config file
- Default value population for optional fields
- Attribute access on the Config object
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
        assert config.max_turns == 25

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


class TestConfigThresholds:
    """Test threshold values from config."""

    def test_all_thresholds_present(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.thresholds.cpu_warning == 80
        assert config.thresholds.cpu_critical == 95
        assert config.thresholds.memory_warning == 80
        assert config.thresholds.memory_critical == 95
        assert config.thresholds.disk_warning == 85
        assert config.thresholds.disk_critical == 95
        assert config.thresholds.swap_warning == 50

    def test_warning_must_be_less_than_critical(self, sample_config_dict, tmp_path):
        sample_config_dict["thresholds"]["cpu_warning"] = 96
        sample_config_dict["thresholds"]["cpu_critical"] = 95
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="warning.*critical"):
            Config.from_file(config_path)

    def test_threshold_out_of_range(self, sample_config_dict, tmp_path):
        sample_config_dict["thresholds"]["cpu_warning"] = 150
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError):
            Config.from_file(config_path)

    def test_threshold_negative_value(self, sample_config_dict, tmp_path):
        sample_config_dict["thresholds"]["cpu_warning"] = -10
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError):
            Config.from_file(config_path)


class TestConfigAlerts:
    """Test alert configuration."""

    def test_alert_channels(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.alerts.stdout is True
        assert config.alerts.log_file == "/var/log/agent-mon/alerts.jsonl"

    def test_email_config(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.alerts.email.enabled is True
        assert config.alerts.email.from_addr == "agent-mon@example.com"
        assert "ops@example.com" in config.alerts.email.to
        assert config.alerts.email.min_severity == "warning"
        assert config.alerts.email.dedup_window_minutes == 15

    def test_log_rotation_defaults(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.alerts.log_max_size_mb == 10
        assert config.alerts.log_max_files == 5

    def test_log_rotation_custom_values(self, sample_config_dict, tmp_path):
        sample_config_dict["alerts"]["log_max_size_mb"] = 50
        sample_config_dict["alerts"]["log_max_files"] = 10
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        config = Config.from_file(config_path)
        assert config.alerts.log_max_size_mb == 50
        assert config.alerts.log_max_files == 10

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


class TestConfigRemediation:
    """Test remediation policy configuration."""

    def test_remediation_enabled(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.remediation.enabled is True
        assert "nginx" in config.remediation.allowed_restart_containers
        assert "redis" in config.remediation.allowed_restart_containers
        assert "postgres" in config.remediation.allowed_restart_containers

    def test_allowed_restart_services(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert "nginx" in config.remediation.allowed_restart_services
        assert "docker" in config.remediation.allowed_restart_services

    def test_allowed_kill_targets(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert "defunct_worker" in config.remediation.allowed_kill_targets

    def test_max_restart_attempts(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        assert config.remediation.max_restart_attempts == 3

    def test_remediation_disabled(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        assert config.remediation.enabled is False

    def test_remediation_enabled_with_empty_lists_raises(
        self, sample_config_dict, tmp_path
    ):
        sample_config_dict["remediation"]["allowed_restart_containers"] = []
        sample_config_dict["remediation"]["allowed_restart_services"] = []
        sample_config_dict["remediation"]["allowed_kill_targets"] = []
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="remediation.*empty"):
            Config.from_file(config_path)


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

    def test_missing_thresholds(self, sample_config_dict, tmp_path):
        del sample_config_dict["thresholds"]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))
        with pytest.raises(ConfigError, match="thresholds"):
            Config.from_file(config_path)

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
        # Should not raise -- email is not enabled
        config.validate_env()
