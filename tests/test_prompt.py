"""Tests for orchestrator and investigator prompt builders.

Covers:
- Orchestrator prompt: identity, investigate_issue, consolidated alert,
  secrets warning, remediation policy, watched processes/containers,
  last cycle summary injection, watched context injection
- Investigator prompt: identity, issue description, query_memory,
  remediation policy, secrets warning, report instruction
- Backward-compatible build_system_prompt wrapper
"""

import pytest

from agent_mon.config import Config
from agent_mon.prompt import (
    build_orchestrator_prompt,
    build_investigator_prompt,
    build_system_prompt,
)


class TestOrchestratorPrompt:
    """Test the orchestrator system prompt."""

    @pytest.fixture
    def config(self, config_yaml_file):
        return Config.from_file(config_yaml_file)

    @pytest.fixture
    def prompt(self, config):
        return build_orchestrator_prompt(config)

    def test_includes_orchestrator_identity(self, prompt):
        assert "orchestrator" in prompt.lower()

    def test_includes_investigate_issue_reference(self, prompt):
        assert "investigate_issue" in prompt

    def test_includes_consolidated_alert_instruction(self, prompt):
        assert "send_alert" in prompt

    def test_includes_store_memory_instruction(self, prompt):
        assert "store_memory" in prompt

    def test_includes_secrets_warning(self, prompt):
        assert "secret" in prompt.lower()

    def test_includes_never_kill_rule(self, prompt):
        assert "never kill" in prompt.lower()

    def test_includes_no_destructive_commands_rule(self, prompt):
        assert "destructive" in prompt.lower()

    def test_includes_remediation_policy(self, config):
        prompt = build_orchestrator_prompt(config)
        assert "Remediation Policy" in prompt

    def test_includes_allowed_services(self, config):
        prompt = build_orchestrator_prompt(config)
        assert "nginx" in prompt

    def test_includes_allowed_containers(self, config):
        prompt = build_orchestrator_prompt(config)
        assert "redis" in prompt
        assert "postgres" in prompt

    def test_includes_watched_processes(self, config):
        prompt = build_orchestrator_prompt(config)
        assert "my-api-server" in prompt
        assert "background-worker" in prompt

    def test_includes_watched_containers_section(self, config):
        prompt = build_orchestrator_prompt(config)
        assert "Watched Containers" in prompt

    def test_injects_last_cycle_summary(self, config):
        prompt = build_orchestrator_prompt(
            config,
            last_cycle_summary="All systems healthy, no issues found.",
        )
        assert "Last Cycle Summary" in prompt
        assert "All systems healthy" in prompt

    def test_no_last_cycle_section_when_empty(self, config):
        prompt = build_orchestrator_prompt(config, last_cycle_summary="")
        assert "Last Cycle Summary" not in prompt

    def test_injects_watched_context(self, config):
        prompt = build_orchestrator_prompt(
            config,
            watched_context="[2026-03-01] nginx high CPU | Action: restarted",
        )
        assert "Recent Memory" in prompt
        assert "nginx high CPU" in prompt

    def test_no_memory_section_when_empty(self, config):
        prompt = build_orchestrator_prompt(config, watched_context="")
        assert "Recent Memory" not in prompt

    def test_no_memory_section_when_no_observations(self, config):
        prompt = build_orchestrator_prompt(
            config, watched_context="No past observations in memory."
        )
        assert "Recent Memory" not in prompt

    def test_remediation_disabled(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_orchestrator_prompt(config)
        assert "disabled" in prompt.lower()

    def test_no_watched_processes_when_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_orchestrator_prompt(config)
        assert "Watched Processes" not in prompt

    def test_no_watched_containers_when_empty(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_orchestrator_prompt(config)
        assert "Watched Containers" not in prompt


class TestInvestigatorPrompt:
    """Test the investigator system prompt."""

    @pytest.fixture
    def config(self, config_yaml_file):
        return Config.from_file(config_yaml_file)

    @pytest.fixture
    def prompt(self, config):
        return build_investigator_prompt(config, "nginx container is down")

    def test_includes_investigator_identity(self, prompt):
        assert "investigating" in prompt.lower()

    def test_includes_issue_description(self, prompt):
        assert "nginx container is down" in prompt

    def test_includes_query_memory_instruction(self, prompt):
        assert "query_memory" in prompt

    def test_includes_remediation_policy(self, prompt):
        assert "Remediation Policy" in prompt

    def test_includes_secrets_warning(self, prompt):
        assert "secret" in prompt.lower()

    def test_includes_never_kill_rule(self, prompt):
        assert "never kill" in prompt.lower()

    def test_includes_report_instruction(self, prompt):
        assert "report" in prompt.lower()

    def test_includes_docker_logs_suggestion(self, prompt):
        assert "docker logs" in prompt

    def test_remediation_disabled(self, minimal_config_yaml_file):
        config = Config.from_file(minimal_config_yaml_file)
        prompt = build_investigator_prompt(config, "disk full")
        assert "disabled" in prompt.lower()

    def test_includes_allowed_services(self, config):
        prompt = build_investigator_prompt(config, "service down")
        assert "nginx" in prompt

    def test_includes_allowed_containers(self, config):
        prompt = build_investigator_prompt(config, "container issue")
        assert "redis" in prompt
        assert "postgres" in prompt


class TestBuildSystemPromptBackwardsCompat:
    """Test that build_system_prompt still works as a wrapper."""

    def test_returns_string(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(config)
        assert isinstance(prompt, str)
        assert "orchestrator" in prompt.lower()

    def test_passes_memory_context(self, config_yaml_file):
        config = Config.from_file(config_yaml_file)
        prompt = build_system_prompt(
            config,
            memory_context="[2026-03-01] High CPU on nginx",
        )
        assert "High CPU on nginx" in prompt
