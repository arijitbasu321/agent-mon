"""Agent loop, scheduler, circuit breaker, and degraded mode."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
import time

import aiohttp

from agent_mon.config import Config
from agent_mon.hooks import bash_denylist_guard, docker_remediation_guard
from agent_mon.memory import MemoryStore
from agent_mon.prompt import build_system_prompt
from agent_mon.tools import create_monitoring_tools
from agent_mon.tools.alerts import AlertManager

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
# Degraded mode (subprocess-based, no psutil)
# ---------------------------------------------------------------------------

async def degraded_check(config: Config) -> list[tuple[str, str]]:
    """Minimal subprocess-based health check -- no LLM required.

    Returns list of (severity, message) tuples for all fired alerts.
    """
    alerts: list[tuple[str, str]] = []

    # Disk check via df
    try:
        result = subprocess.run(
            ["df", "-h"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    usage_str = parts[4].rstrip("%")
                    try:
                        usage = int(usage_str)
                        if usage > 95:
                            alerts.append(
                                ("critical", f"Disk {parts[5]} at {usage}%")
                            )
                    except ValueError:
                        pass
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Memory check via free
    try:
        result = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                if line.startswith("Mem:"):
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            total = int(parts[1])
                            used = int(parts[2])
                            if total > 0:
                                pct = (used / total) * 100
                                if pct > 95:
                                    alerts.append(
                                        ("critical", f"Memory at {pct:.1f}%")
                                    )
                        except ValueError:
                            pass
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Load average check via uptime
    try:
        result = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            output = result.stdout
            if "load average:" in output:
                load_part = output.split("load average:")[1].strip()
                load_values = load_part.split(",")
                if load_values:
                    try:
                        load_1m = float(load_values[0].strip())
                        # Get CPU count
                        cpu_count = os.cpu_count() or 1
                        if load_1m / cpu_count > 2.0:
                            alerts.append(
                                ("critical",
                                 f"Load average {load_1m} ({load_1m/cpu_count:.1f} per CPU)")
                            )
                    except ValueError:
                        pass
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Meta-alert about degraded mode
    alerts.append((
        "critical",
        "agent-mon degraded: Anthropic API unreachable, "
        "running subprocess-based critical checks only",
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
        self.alert_manager = AlertManager(config)
        self.memory_store: MemoryStore | None = None

        if config.memory.enabled:
            self.memory_store = MemoryStore(config.memory)

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        self.http_session = aiohttp.ClientSession()
        self.alert_manager.http_session = self.http_session

        # Initialize memory store
        if self.memory_store is not None:
            try:
                self.memory_store.initialize()
            except Exception:
                logger.exception("Failed to initialize memory store")
                self.memory_store = None

        try:
            tasks = [self._run_scheduler()]
            if self.config.heartbeat.enabled:
                tasks.append(self._run_heartbeat_loop())
            await asyncio.gather(*tasks)
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
        # Check circuit breaker
        if not self.circuit_breaker.should_attempt_api_call():
            logger.warning("Circuit breaker OPEN -- running degraded check")
            alerts = await degraded_check(self.config)
            for severity, message in alerts:
                await self.alert_manager.send_alert(severity, message, message)
            return

        try:
            # Query memory for past context
            memory_context = ""
            if self.memory_store is not None:
                try:
                    memory_context = self.memory_store.query(
                        "recent system health issues"
                    )
                except Exception:
                    logger.exception("Failed to query memory")

            # Build system prompt with memory context
            system_prompt = build_system_prompt(
                self.config, memory_context=memory_context
            )

            # Create tools
            tools = create_monitoring_tools(
                self.config, self.alert_manager, self.memory_store
            )

            # Build SDK hooks
            hooks = self._build_sdk_hooks()

            # Run agent via Claude Agent SDK
            from claude_agent_sdk import ClaudeSDKClient

            client = ClaudeSDKClient(
                model=self.config.model,
                max_turns=self.config.max_turns,
                system_prompt=system_prompt,
                tools=tools,
                hooks=hooks,
            )

            await client.run("Run a full system health check.")

            self.circuit_breaker.record_success()

        except Exception as exc:
            logger.error("Check cycle API call failed: %s", exc)
            self.circuit_breaker.record_failure()
            if self.circuit_breaker.state == CircuitBreaker.OPEN:
                logger.warning(
                    "Circuit breaker just opened -- running degraded check"
                )
                alerts = await degraded_check(self.config)
                for severity, message in alerts:
                    await self.alert_manager.send_alert(
                        severity, message, message
                    )

    def _build_sdk_hooks(self) -> dict:
        """Build the PreToolUse hooks dict for the SDK."""
        config = self.config

        def bash_hook(tool_name: str, tool_input: dict):
            return bash_denylist_guard(tool_name, tool_input, config=config)

        def docker_hook(tool_name: str, tool_input: dict):
            return docker_remediation_guard(
                tool_name, tool_input, config=config
            )

        return {
            "bash": bash_hook,
            "docker": docker_hook,
        }

    async def _send_heartbeat(self):
        """Send a heartbeat email via Resend."""
        hostname = socket.gethostname()
        resend_key = os.environ.get("RESEND_API_KEY", "")

        # Collect basic metrics via subprocess
        body_parts = [f"Heartbeat from agent-mon@{hostname}\n"]

        try:
            result = subprocess.run(
                ["uptime"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                body_parts.append(f"Uptime: {result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = subprocess.run(
                ["free", "-m"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                body_parts.append(f"Memory:\n{result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = subprocess.run(
                ["df", "-h"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                body_parts.append(f"Disk:\n{result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

        body = "\n\n".join(body_parts)

        email_cfg = self.config.alerts.email
        from_addr = email_cfg.from_addr or "agent-mon@localhost"
        to_addrs = email_cfg.to or []

        if not to_addrs or not self.http_session:
            logger.warning("Heartbeat: no recipients or no HTTP session")
            return

        try:
            await self.http_session.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}"},
                json={
                    "from": from_addr,
                    "to": to_addrs,
                    "subject": f"[HEARTBEAT] agent-mon@{hostname}",
                    "text": body,
                },
            )
            logger.info("Heartbeat email sent")
        except Exception:
            logger.exception("Failed to send heartbeat email")

    async def _run_heartbeat_loop(self):
        """Run the heartbeat timer loop independently of the scheduler."""
        while not self.shutdown_event.is_set():
            try:
                await self._send_heartbeat()
            except Exception:
                logger.exception("Heartbeat failed")

            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=self.config.heartbeat.interval,
                )
                break
            except asyncio.TimeoutError:
                continue

    async def _cleanup(self):
        if self.http_session:
            await self.http_session.close()
        logger.info("Shutdown complete")
