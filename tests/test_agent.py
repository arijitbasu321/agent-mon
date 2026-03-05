"""Tests for the agent loop, scheduler, circuit breaker, and daemon lifecycle.

Covers:
- CircuitBreaker state machine (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
- AgentDaemon scheduler loop
- Graceful shutdown via SIGTERM/SIGINT
- Degraded mode fallback (subprocess-based, no psutil)
- Check cycle error handling
- Heartbeat loop
- Orchestrator/Investigator architecture
- Pre-cycle memory injection
"""

import asyncio
import signal
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_mon.agent import AgentDaemon, CircuitBreaker, degraded_check


# ===========================================================================
# CircuitBreaker tests
# ===========================================================================


class TestCircuitBreakerInit:
    """Test circuit breaker initialization."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreaker.CLOSED

    def test_initial_consecutive_failures_is_zero(self):
        cb = CircuitBreaker()
        assert cb.consecutive_failures == 0

    def test_custom_thresholds(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=600)
        assert cb.failure_threshold == 5
        assert cb.recovery_timeout == 600

    def test_default_thresholds(self):
        cb = CircuitBreaker()
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 300


class TestCircuitBreakerClosed:
    """Test behavior in CLOSED state."""

    def test_should_attempt_api_call_when_closed(self):
        cb = CircuitBreaker()
        assert cb.should_attempt_api_call() is True

    def test_stays_closed_after_success(self):
        cb = CircuitBreaker()
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.consecutive_failures == 0

    def test_stays_closed_after_fewer_failures_than_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.consecutive_failures == 1
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.consecutive_failures == 2

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert cb.state == CircuitBreaker.CLOSED


class TestCircuitBreakerOpen:
    """Test transition to and behavior in OPEN state."""

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_should_not_attempt_api_call_when_open(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert cb.should_attempt_api_call() is False

    def test_records_last_failure_time(self):
        cb = CircuitBreaker(failure_threshold=1)
        before = time.monotonic()
        cb.record_failure()
        after = time.monotonic()
        assert before <= cb.last_failure_time <= after

    def test_additional_failures_stay_open(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.consecutive_failures == 3


class TestCircuitBreakerHalfOpen:
    """Test transition to HALF_OPEN and recovery."""

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        result = cb.should_attempt_api_call()
        assert result is True
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_stays_open_before_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999)
        cb.record_failure()
        assert cb.should_attempt_api_call() is False
        assert cb.state == CircuitBreaker.OPEN

    def test_success_in_half_open_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        cb.should_attempt_api_call()  # transitions to HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.consecutive_failures == 0

    def test_failure_in_half_open_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        cb.should_attempt_api_call()  # transitions to HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN


class TestCircuitBreakerFullCycle:
    """Test complete lifecycle: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""

    def test_full_recovery_cycle(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0)

        assert cb.state == CircuitBreaker.CLOSED
        assert cb.should_attempt_api_call() is True

        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.should_attempt_api_call() is True  # timeout=0 -> HALF_OPEN

        assert cb.state == CircuitBreaker.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.consecutive_failures == 0
        assert cb.should_attempt_api_call() is True


# ===========================================================================
# AgentDaemon tests
# ===========================================================================


class TestAgentDaemonInit:
    """Test daemon initialization."""

    def test_daemon_creates_with_config(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        assert daemon.config is config
        assert daemon.shutdown_event.is_set() is False
        assert daemon.http_session is None
        assert daemon.check_in_progress is False

    def test_daemon_has_rate_limiter(self, config_yaml_file):
        """H2: daemon owns its own RateLimiter instance."""
        from agent_mon.config import Config
        from agent_mon.hooks import RateLimiter

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        assert isinstance(daemon.rate_limiter, RateLimiter)


class TestAgentDaemonShutdown:
    """Test graceful shutdown behavior."""

    @pytest.fixture
    def daemon_with_config(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        return AgentDaemon(config)

    def test_request_shutdown_sets_event(self, daemon_with_config):
        daemon_with_config._request_shutdown()
        assert daemon_with_config.shutdown_event.is_set() is True

    async def test_cleanup_closes_http_session(self, daemon_with_config):
        mock_session = AsyncMock()
        daemon_with_config.http_session = mock_session
        await daemon_with_config._cleanup()
        mock_session.close.assert_awaited_once()

    async def test_cleanup_handles_no_session(self, daemon_with_config):
        daemon_with_config.http_session = None
        # Should not raise
        await daemon_with_config._cleanup()

    async def test_scheduler_exits_on_shutdown_event(self, daemon_with_config):
        daemon_with_config._run_check_cycle = AsyncMock()
        daemon_with_config.shutdown_event.set()

        await daemon_with_config._run_scheduler()
        daemon_with_config._run_check_cycle.assert_not_awaited()


class TestAgentDaemonCheckCycle:
    """Test check cycle execution."""

    @pytest.fixture
    def daemon_with_config(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        return daemon

    async def test_check_cycle_exception_does_not_crash_scheduler(
        self, daemon_with_config
    ):
        call_count = 0

        async def failing_check():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("API error")
            daemon_with_config.shutdown_event.set()

        daemon_with_config._run_check_cycle = failing_check
        daemon_with_config.config.check_interval = 0

        await daemon_with_config._run_scheduler()
        assert call_count == 2

    async def test_check_in_progress_flag(self, daemon_with_config):
        was_in_progress = None

        async def capture_flag():
            nonlocal was_in_progress
            was_in_progress = daemon_with_config.check_in_progress
            daemon_with_config.shutdown_event.set()

        daemon_with_config._run_check_cycle = capture_flag
        daemon_with_config.config.check_interval = 0

        await daemon_with_config._run_scheduler()
        assert was_in_progress is True
        assert daemon_with_config.check_in_progress is False


class TestAgentDaemonInitialize:
    """Test the _initialize method (H7)."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        return AgentDaemon(config)

    async def test_initialize_creates_http_session(self, daemon):
        await daemon._initialize()
        assert daemon.http_session is not None
        await daemon._cleanup()

    async def test_initialize_sets_alert_manager_session(self, daemon):
        await daemon._initialize()
        assert daemon.alert_manager.http_session is daemon.http_session
        await daemon._cleanup()


class TestRunOnce:
    """Test the run_once method (H7)."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        return AgentDaemon(config)

    async def test_run_once_initializes_and_cleans_up(self, daemon):
        daemon._run_check_cycle = AsyncMock()
        await daemon.run_once()
        daemon._run_check_cycle.assert_awaited_once()
        # Session should be closed after cleanup
        assert daemon.http_session is None or daemon.http_session.closed


class TestHeartbeatLoop:
    """Test the heartbeat loop."""

    @pytest.fixture
    def daemon_with_heartbeat(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        config.heartbeat.enabled = True
        config.heartbeat.interval = 1
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        return daemon

    async def test_heartbeat_exits_on_shutdown(self, daemon_with_heartbeat, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
        send_count = 0

        async def counting_send():
            nonlocal send_count
            send_count += 1
            daemon_with_heartbeat.shutdown_event.set()

        daemon_with_heartbeat._send_heartbeat = counting_send

        await daemon_with_heartbeat._run_heartbeat_loop()
        assert send_count == 1


class TestHeartbeatEmptyKey:
    """Test H5: heartbeat skips when RESEND_API_KEY is empty."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        return daemon

    async def test_skips_when_resend_key_empty(self, daemon, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "")
        await daemon._send_heartbeat()
        # Should not call http_session.post
        daemon.http_session.post.assert_not_awaited()

    async def test_skips_when_resend_key_missing(self, daemon, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        await daemon._send_heartbeat()
        daemon.http_session.post.assert_not_awaited()


class TestDegradedMode:
    """Test subprocess-based degraded check when API is unavailable."""

    async def test_degraded_check_always_sends_meta_alert(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.subprocess") as mock_subprocess:
            # Mock all subprocess.run calls to return empty/safe output
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_subprocess.run.return_value = mock_result
            mock_subprocess.TimeoutExpired = TimeoutError

            alerts = await degraded_check(config)

        assert any("degraded" in str(a).lower() for a in alerts)

    async def test_degraded_check_detects_disk_critical(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.subprocess") as mock_subprocess:
            mock_subprocess.TimeoutExpired = TimeoutError

            def mock_run(cmd, **kwargs):
                result = MagicMock()
                result.returncode = 0
                if cmd[0] == "df":
                    result.stdout = (
                        "Filesystem      Size  Used Avail Use% Mounted on\n"
                        "/dev/sda1       100G   97G    3G  97% /\n"
                    )
                elif cmd[0] == "free":
                    result.stdout = (
                        "              total        used        free\n"
                        "Mem:          16000        8000        8000\n"
                    )
                elif cmd[0] == "uptime":
                    result.stdout = " 12:00:00 up 10 days, load average: 0.5, 0.3, 0.2"
                else:
                    result.stdout = ""
                return result

            mock_subprocess.run.side_effect = mock_run

            alerts = await degraded_check(config)

        # Should have a disk alert
        assert any("97%" in str(a) for a in alerts)


class TestCircuitBreakerH6:
    """Test H6: PermissionError/MemoryError don't trip the circuit breaker."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        return daemon

    async def test_permission_error_does_not_trip_breaker(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(side_effect=PermissionError("no access"))
            MockClient.return_value = mock_instance

            with pytest.raises(PermissionError):
                await daemon._run_check_cycle()

        assert daemon.circuit_breaker.state == CircuitBreaker.CLOSED
        assert daemon.circuit_breaker.consecutive_failures == 0

    async def test_api_error_trips_breaker(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(side_effect=ConnectionError("API down"))
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

        assert daemon.circuit_breaker.consecutive_failures == 1


# ===========================================================================
# Orchestrator / Investigator architecture tests
# ===========================================================================


class TestOrchestratorFlow:
    """Test that _run_check_cycle creates orchestrator with correct setup."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        return daemon

    async def test_cycle_creates_client_with_orchestrator_prompt(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            # Check the system_prompt contains orchestrator identity
            call_kwargs = MockClient.call_args
            prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
            assert "orchestrator" in prompt.lower()

    async def test_cycle_passes_investigate_issue_tool(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            call_kwargs = MockClient.call_args
            tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools", [])
            tool_names = [t["name"] for t in tools]
            assert "investigate_issue" in tool_names

    async def test_cycle_passes_send_alert_and_store_memory(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            call_kwargs = MockClient.call_args
            tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools", [])
            tool_names = [t["name"] for t in tools]
            assert "send_alert" in tool_names
            assert "store_memory" in tool_names

    async def test_cycle_does_not_pass_query_memory(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            call_kwargs = MockClient.call_args
            tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools", [])
            tool_names = [t["name"] for t in tools]
            assert "query_memory" not in tool_names


class TestInvestigatorDispatch:
    """Test _run_investigator creates sub-agent with correct setup."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        return daemon

    async def test_investigator_creates_sub_agent(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="Investigation complete")
            MockClient.return_value = mock_instance

            result = await daemon._run_investigator("nginx is down")

            MockClient.assert_called_once()
            assert "Investigation complete" in result

    async def test_investigator_passes_query_memory_tool(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="done")
            MockClient.return_value = mock_instance

            await daemon._run_investigator("nginx is down")

            call_kwargs = MockClient.call_args
            tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools", [])
            tool_names = [t["name"] for t in tools]
            assert "query_memory" in tool_names

    async def test_investigator_does_not_pass_send_alert(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="done")
            MockClient.return_value = mock_instance

            await daemon._run_investigator("disk full")

            call_kwargs = MockClient.call_args
            tools = call_kwargs.kwargs.get("tools") or call_kwargs[1].get("tools", [])
            tool_names = [t["name"] for t in tools]
            assert "send_alert" not in tool_names
            assert "store_memory" not in tool_names

    async def test_investigator_includes_issue_in_prompt(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="done")
            MockClient.return_value = mock_instance

            await daemon._run_investigator("redis memory spike")

            call_kwargs = MockClient.call_args
            prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
            assert "redis memory spike" in prompt

    async def test_investigator_max_turns_capped_at_30(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="done")
            MockClient.return_value = mock_instance

            await daemon._run_investigator("test issue")

            call_kwargs = MockClient.call_args
            max_turns = call_kwargs.kwargs.get("max_turns") or call_kwargs[1].get("max_turns")
            assert max_turns <= 30

    async def test_investigator_gets_same_hooks(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock(return_value="done")
            MockClient.return_value = mock_instance

            await daemon._run_investigator("test issue")

            call_kwargs = MockClient.call_args
            hooks = call_kwargs.kwargs.get("hooks") or call_kwargs[1].get("hooks", {})
            assert "bash" in hooks
            assert "docker" in hooks

    async def test_investigator_error_returns_error_string(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            MockClient.side_effect = RuntimeError("API down")

            result = await daemon._run_investigator("test issue")

            assert "failed" in result.lower()

    async def test_investigator_timeout_returns_timeout_string(self, daemon):
        """M4: test wall-clock timeout on investigator."""
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()

            async def slow_run(*args, **kwargs):
                await asyncio.sleep(999)

            mock_instance.run = slow_run
            MockClient.return_value = mock_instance

            with patch("agent_mon.agent._INVESTIGATOR_TIMEOUT", 0.01):
                result = await daemon._run_investigator("test issue")

            assert "timed out" in result.lower()


class TestPreCycleMemory:
    """Test that _run_check_cycle queries memory before running."""

    @pytest.fixture
    def daemon(self, config_yaml_file):
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)
        daemon = AgentDaemon(config)
        daemon.http_session = AsyncMock()
        # Set up mock memory store
        daemon.memory_store = MagicMock()
        daemon.memory_store.get_last_cycle_summary = MagicMock(
            return_value="Last cycle: all healthy"
        )
        daemon.memory_store.query_by_services = MagicMock(
            return_value="nginx was restarted yesterday"
        )
        return daemon

    async def test_queries_last_cycle_summary(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            daemon.memory_store.get_last_cycle_summary.assert_called_once()

    async def test_queries_watched_service_context(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            daemon.memory_store.query_by_services.assert_called_once()
            # Check service names include watched processes + containers
            call_args = daemon.memory_store.query_by_services.call_args
            service_names = call_args[0][0]
            assert "my-api-server" in service_names
            assert "nginx" in service_names
            assert "redis" in service_names

    async def test_injects_both_into_orchestrator_prompt(self, daemon):
        with patch("claude_agent_sdk.ClaudeSDKClient") as MockClient:
            mock_instance = MagicMock()
            mock_instance.run = AsyncMock()
            MockClient.return_value = mock_instance

            await daemon._run_check_cycle()

            call_kwargs = MockClient.call_args
            prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs[1].get("system_prompt", "")
            assert "Last cycle: all healthy" in prompt
            assert "nginx was restarted yesterday" in prompt
