"""Tests for the agent loop, scheduler, circuit breaker, and daemon lifecycle.

Covers:
- CircuitBreaker state machine (CLOSED -> OPEN -> HALF_OPEN -> CLOSED)
- AgentDaemon scheduler loop
- Graceful shutdown via SIGTERM/SIGINT
- Degraded mode fallback when API is unavailable
- Check cycle error handling
"""

import asyncio
import signal
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_mon.agent import AgentDaemon, CircuitBreaker


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
        # With recovery_timeout=0, should immediately transition
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

        # CLOSED: normal operation
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.should_attempt_api_call() is True

        # Two failures -> OPEN
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.should_attempt_api_call() is True  # timeout=0 -> HALF_OPEN

        # HALF_OPEN: try one call
        assert cb.state == CircuitBreaker.HALF_OPEN

        # Success -> CLOSED
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
        """Scheduler should exit promptly when shutdown is requested."""
        daemon_with_config._run_check_cycle = AsyncMock()
        # Request shutdown before scheduler starts
        daemon_with_config.shutdown_event.set()

        # Should exit without running any cycles
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
        """If a check cycle raises, the scheduler should continue."""
        call_count = 0

        async def failing_check():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("API error")
            # On second call, request shutdown to stop the loop
            daemon_with_config.shutdown_event.set()

        daemon_with_config._run_check_cycle = failing_check
        daemon_with_config.config.check_interval = 0  # no wait between cycles

        await daemon_with_config._run_scheduler()
        assert call_count == 2  # Ran twice — survived the first failure

    async def test_check_in_progress_flag(self, daemon_with_config):
        """check_in_progress should be True during cycle and False after."""
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


class TestDegradedMode:
    """Test Python-only degraded check when API is unavailable."""

    async def test_degraded_check_fires_on_critical_disk(
        self, config_yaml_file, mock_aiohttp_session
    ):
        from agent_mon.agent import degraded_check
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        mock_part = MagicMock()
        mock_part.mountpoint = "/"
        mock_usage = MagicMock()
        mock_usage.percent = 97.0  # above 95% critical threshold

        with patch("agent_mon.agent.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = [mock_part]
            mock_psutil.disk_usage.return_value = mock_usage
            mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_psutil.cpu_percent.return_value = 30.0

            alerts = await degraded_check(config, mock_aiohttp_session)

        # Should have at least a disk alert and the degraded-mode meta-alert
        assert any("97.0%" in str(a) for a in alerts)

    async def test_degraded_check_fires_on_critical_memory(
        self, config_yaml_file, mock_aiohttp_session
    ):
        from agent_mon.agent import degraded_check
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = []
            mock_psutil.virtual_memory.return_value = MagicMock(percent=98.0)
            mock_psutil.cpu_percent.return_value = 30.0

            alerts = await degraded_check(config, mock_aiohttp_session)

        assert any("98.0%" in str(a) for a in alerts)

    async def test_degraded_check_fires_on_critical_cpu(
        self, config_yaml_file, mock_aiohttp_session
    ):
        from agent_mon.agent import degraded_check
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = []
            mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_psutil.cpu_percent.return_value = 99.0

            alerts = await degraded_check(config, mock_aiohttp_session)

        assert any("99.0%" in str(a) for a in alerts)

    async def test_degraded_check_always_sends_meta_alert(
        self, config_yaml_file, mock_aiohttp_session
    ):
        from agent_mon.agent import degraded_check
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = []
            mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_psutil.cpu_percent.return_value = 30.0

            alerts = await degraded_check(config, mock_aiohttp_session)

        # Even with no threshold violations, the degraded-mode meta-alert fires
        assert any("degraded" in str(a).lower() for a in alerts)

    async def test_degraded_check_no_false_positive_below_thresholds(
        self, config_yaml_file, mock_aiohttp_session
    ):
        from agent_mon.agent import degraded_check
        from agent_mon.config import Config

        config = Config.from_file(config_yaml_file)

        with patch("agent_mon.agent.psutil") as mock_psutil:
            mock_psutil.disk_partitions.return_value = []
            mock_psutil.virtual_memory.return_value = MagicMock(percent=50.0)
            mock_psutil.cpu_percent.return_value = 30.0

            alerts = await degraded_check(config, mock_aiohttp_session)

        # Only the meta-alert, no threshold alerts
        threshold_alerts = [a for a in alerts if "degraded" not in str(a).lower()]
        assert len(threshold_alerts) == 0
