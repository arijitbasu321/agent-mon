"""Tests for system prompt template builder.

Covers:
- Prompt includes all required protocol steps (COLLECT, ANALYZE, ALERT, etc.)
- Prompt includes threshold values from config
- Prompt includes remediation policy and allowed targets
- Prompt includes rules about alert-before-remediate
"""

import pytest

from agent_mon.config import Config
from agent_mon.prompt import build_system_prompt


class TestSystemPromptContent:
    """Test that the system prompt includes required sections."""

    @pytest.fixture
    def prompt(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        return build_system_prompt(config)

    def test_includes_collect_step(self, prompt):
        assert "COLLECT" in prompt

    def test_includes_analyze_step(self, prompt):
        assert "ANALYZE" in prompt

    def test_includes_alert_step(self, prompt):
        assert "ALERT" in prompt

    def test_includes_remediate_step(self, prompt):
        assert "REMEDIATE" in prompt

    def test_includes_summarize_step(self, prompt):
        assert "SUMMARIZE" in prompt

    def test_includes_severity_levels(self, prompt):
        assert "critical" in prompt.lower()
        assert "warning" in prompt.lower()
        assert "info" in prompt.lower()


class TestSystemPromptThresholds:
    """Test that threshold values are injected into the prompt."""

    @pytest.fixture
    def prompt(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        return build_system_prompt(config)

    def test_includes_cpu_thresholds(self, prompt):
        assert "80" in prompt  # cpu_warning
        assert "95" in prompt  # cpu_critical

    def test_includes_disk_threshold(self, prompt):
        assert "85" in prompt  # disk_warning

    def test_includes_memory_thresholds(self, prompt):
        # memory_warning=80, memory_critical=95
        assert "80" in prompt
        assert "95" in prompt


class TestSystemPromptRemediation:
    """Test remediation policy is reflected in the prompt."""

    def test_includes_allowed_containers(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "nginx" in prompt
        assert "redis" in prompt
        assert "postgres" in prompt

    def test_includes_allowed_services(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "docker" in prompt

    def test_includes_allowed_kill_targets(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "defunct_worker" in prompt

    def test_includes_alert_before_remediate_rule(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        # Should mention alerting before remediation
        assert "alert" in prompt.lower() and "before" in prompt.lower()

    def test_remediation_disabled_reflected_in_prompt(
        self, minimal_config_yaml_file
    ):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_system_prompt(config)
        # When remediation is disabled, the prompt should not list allowed targets
        # or should explicitly say remediation is not available
        assert "nginx" not in prompt or "disabled" in prompt.lower()


class TestSystemPromptCustomThresholds:
    """Test that custom threshold values propagate into the prompt."""

    def test_custom_thresholds(self, sample_config_dict, tmp_path):
        import yaml

        sample_config_dict["thresholds"]["cpu_warning"] = 70
        sample_config_dict["thresholds"]["disk_warning"] = 90
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config_dict))

        config = Config.from_file(config_path)
        prompt = build_system_prompt(config)
        assert "70" in prompt
        assert "90" in prompt
