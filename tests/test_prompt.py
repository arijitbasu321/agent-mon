"""Tests for system prompt template builder.

Covers:
- Prompt includes 6-step workflow (INVESTIGATE, DIAGNOSE, ALERT, REMEDIATE, REMEMBER, SUMMARIZE)
- Prompt suggests bash commands
- Prompt includes remediation policy and allowed targets
- Prompt includes rules
- Prompt includes watched processes and containers
- Prompt includes memory context when provided
- No thresholds in prompt (agent uses judgment)
"""

import pytest

from agent_mon.config import Config
from agent_mon.prompt import build_system_prompt


class TestSystemPromptWorkflow:
    """Test that the system prompt includes the 6-step workflow."""

    @pytest.fixture
    def prompt(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        return build_system_prompt(config)

    def test_includes_investigate_step(self, prompt):
        assert "INVESTIGATE" in prompt

    def test_includes_diagnose_step(self, prompt):
        assert "DIAGNOSE" in prompt

    def test_includes_alert_step(self, prompt):
        assert "ALERT" in prompt

    def test_includes_remediate_step(self, prompt):
        assert "REMEDIATE" in prompt

    def test_includes_remember_step(self, prompt):
        assert "REMEMBER" in prompt

    def test_includes_summarize_step(self, prompt):
        assert "SUMMARIZE" in prompt

    def test_includes_severity_levels(self, prompt):
        assert "critical" in prompt.lower()
        assert "warning" in prompt.lower()
        assert "info" in prompt.lower()


class TestSystemPromptBashCommands:
    """Test that bash commands are suggested in the prompt."""

    @pytest.fixture
    def prompt(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        return build_system_prompt(config)

    def test_suggests_ps(self, prompt):
        assert "ps" in prompt

    def test_suggests_df(self, prompt):
        assert "df" in prompt

    def test_suggests_free(self, prompt):
        assert "free" in prompt

    def test_suggests_journalctl(self, prompt):
        assert "journalctl" in prompt

    def test_suggests_systemctl(self, prompt):
        assert "systemctl" in prompt


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

    def test_includes_alert_before_remediate_rule(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "alert" in prompt.lower() and "before" in prompt.lower()

    def test_remediation_disabled_reflected_in_prompt(
        self, minimal_config_yaml_file
    ):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_system_prompt(config)
        assert "disabled" in prompt.lower()

    def test_never_kill_rule(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "never kill" in prompt.lower()


class TestSystemPromptWatchedProcesses:
    """Test watched processes appear in prompt."""

    def test_includes_watched_processes(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "my-api-server" in prompt
        assert "background-worker" in prompt

    def test_no_watched_processes_section_when_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_system_prompt(config)
        assert "Watched Processes" not in prompt


class TestSystemPromptWatchedContainers:
    """Test watched containers appear in prompt."""

    def test_includes_watched_containers(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert "Watched Containers" in prompt

    def test_no_watched_containers_section_when_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_system_prompt(config)
        assert "Watched Containers" not in prompt


class TestSystemPromptMemoryContext:
    """Test that memory context is injected into the prompt."""

    def test_includes_memory_context(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        memory_context = "[2026-03-01T12:00:00Z] High CPU on nginx | Action: Restarted | Outcome: Resolved"
        prompt = build_system_prompt(config, memory_context=memory_context)
        assert "Recent Memory" in prompt
        assert "High CPU on nginx" in prompt

    def test_no_memory_section_when_empty(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config, memory_context="")
        assert "Recent Memory" not in prompt

    def test_no_memory_section_when_no_observations(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(
            config, memory_context="No past observations in memory."
        )
        assert "Recent Memory" not in prompt
