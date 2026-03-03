"""Tests for the agent loop, scheduler, circuit breaker, and daemon lifecycle.

Covers:
- CircuitBreaker state machine (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
- AgentDaemon scheduler loop
- Graceful shutdown via SIGTERM/SIGINT
- Degraded mode fallback (subprocess-based, no psutil)
- Check cycle error handling
- Heartbeat loop
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
