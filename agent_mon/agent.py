"""Agent loop, scheduler, circuit breaker, and degraded mode."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

import aiohttp
import psutil

from agent_mon.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.last_failure_time: float | None = None

    def record_success(self):
        self.consecutive_failures = 0
        self.state = self.CLOSED

    def record_failure(self):
        self.consecutive_failures += 1
        self.last_failure_time = time.monotonic()
        if self.consecutive_failures >= self.failure_threshold:
            self.state = self.OPEN
            logger.error(
                "Circuit breaker OPEN: %d consecutive API failures, "
                "switching to degraded mode",
                self.consecutive_failures,
            )

    def should_attempt_api_call(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = self.HALF_OPEN
                return True
            return False
        # HALF_OPEN
        return True


# ---------------------------------------------------------------------------
# Degraded mode
# ---------------------------------------------------------------------------

async def degraded_check(
    config: Config, http_session: aiohttp.ClientSession
) -> list[tuple[str, str]]:
    """Minimal Python-only health check — no LLM required.

    Returns list of (severity, message) tuples for all fired alerts.
    """
    alerts: list[tuple[str, str]] = []

    # Disk critical
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            if usage.percent > config.thresholds.disk_critical:
                alerts.append(
                    ("critical", f"Disk {part.mountpoint} at {usage.percent}%")
                )
        except (PermissionError, OSError):
            pass

    # Memory critical
    mem = psutil.virtual_memory()
    if mem.percent > config.thresholds.memory_critical:
        alerts.append(("critical", f"Memory at {mem.percent}%"))

    # CPU critical
    cpu = psutil.cpu_percent(interval=2)
    if cpu > config.thresholds.cpu_critical:
        alerts.append(("critical", f"CPU at {cpu}%"))

    # Meta-alert about degraded mode
    alerts.append((
        "critical",
        "agent-mon degraded: Anthropic API unreachable, "
        "running Python-only critical checks",
    ))

    return alerts


# ---------------------------------------------------------------------------
# Agent Daemon
# ---------------------------------------------------------------------------

class AgentDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.http_session: aiohttp.ClientSession | None = None
        self.check_in_progress = False
        self.circuit_breaker = CircuitBreaker()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        self.http_session = aiohttp.ClientSession()
        try:
            await self._run_scheduler()
        finally:
            await self._cleanup()

    def _request_shutdown(self):
        logger.info("Shutdown requested, finishing current cycle...")
        self.shutdown_event.set()

    async def _run_scheduler(self):
        while not self.shutdown_event.is_set():
            self.check_in_progress = True
            try:
                await self._run_check_cycle()
            except Exception:
                logger.exception("Check cycle failed")
            finally:
                self.check_in_progress = False

            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=self.config.check_interval,
                )
                break
            except asyncio.TimeoutError:
                continue

    async def _run_check_cycle(self):
        """Run a single monitoring check cycle."""
        # Placeholder — will be implemented with SDK integration
        pass

    async def _cleanup(self):
        if self.http_session:
            await self.http_session.close()
        logger.info("Shutdown complete")
