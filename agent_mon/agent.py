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
from agent_mon.hooks import RateLimiter, build_sdk_hooks
from agent_mon.memory import MemoryStore
from agent_mon.prompt import build_orchestrator_prompt, build_investigator_prompt
from agent_mon.tools import create_orchestrator_tools, create_investigator_tools
from agent_mon.tools.alerts import AlertManager

logger = logging.getLogger(__name__)

# Wall-clock timeout for investigator sub-agents (M4)
_INVESTIGATOR_TIMEOUT = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Non-blocking subprocess helper (C2/C3)
# ---------------------------------------------------------------------------

async def _async_subprocess(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """Run subprocess in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
    )


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
# Degraded mode (subprocess-based, no psutil) -- C2: non-blocking
# ---------------------------------------------------------------------------

async def degraded_check(config: Config) -> list[tuple[str, str]]:
    """Minimal subprocess-based health check -- no LLM required.

    Returns list of (severity, message) tuples for all fired alerts.
    """
    alerts: list[tuple[str, str]] = []

    # Disk check via df
    try:
        result = await _async_subprocess(["df", "-h"])
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
        result = await _async_subprocess(["free", "-m"])
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
        result = await _async_subprocess(["uptime"])
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
        self.rate_limiter = RateLimiter()  # H2: instance-owned rate limiter

        if config.memory.enabled:
            self.memory_store = MemoryStore(config.memory)

    # H7: extracted initialization for reuse in run() and run_once()
    async def _initialize(self):
        """Initialize HTTP session, memory store, and other resources."""
        self.http_session = aiohttp.ClientSession()
        self.alert_manager.http_session = self.http_session

        if self.memory_store is not None:
            try:
                self.memory_store.initialize()
            except Exception:
                logger.exception("Failed to initialize memory store")
                self.memory_store = None

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        await self._initialize()

        try:
            tasks = [self._run_scheduler()]
            if self.config.heartbeat.enabled:
                tasks.append(self._run_heartbeat_loop())
            await asyncio.gather(*tasks)
        finally:
            await self._cleanup()

    # H7: proper one-shot mode with full initialization
    async def run_once(self):
        """Run a single check cycle with proper initialization and cleanup."""
        await self._initialize()
        try:
            await self._run_check_cycle()
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
            # Pre-cycle memory queries
            last_cycle_summary = ""
            watched_context = ""
            if self.memory_store is not None:
                try:
                    last_cycle_summary = self.memory_store.get_last_cycle_summary()
                except Exception:
                    logger.exception("Failed to get last cycle summary")

                try:
                    service_names = [
                        wp.name for wp in self.config.watched_processes
                    ] + list(self.config.watched_containers)
                    if service_names:
                        watched_context = self.memory_store.query_by_services(
                            service_names
                        )
                except Exception:
                    logger.exception("Failed to query watched services")

            # Build orchestrator prompt
            system_prompt = build_orchestrator_prompt(
                self.config,
                last_cycle_summary=last_cycle_summary,
                watched_context=watched_context,
            )

            # C1: async investigate_fn closure
            async def investigate_fn(description: str) -> str:
                return await self._run_investigator(description)

            # Create orchestrator tools
            tools = create_orchestrator_tools(
                self.config,
                self.alert_manager,
                self.memory_store,
                investigate_fn=investigate_fn,
            )

            # Build SDK hooks
            hooks = build_sdk_hooks(self.config, self.rate_limiter)

            # Run orchestrator via Claude Agent SDK
            from claude_agent_sdk import query
            from claude_agent_sdk.types import ClaudeAgentOptions

            options = ClaudeAgentOptions(
                model=self.config.model,
                max_turns=self.config.max_turns,
                system_prompt=system_prompt,
                mcp_servers={"agent-mon": tools},
                hooks=hooks,
                permission_mode="bypassPermissions",
            )

            async for _msg in query(
                prompt="Run a full system health check.",
                options=options,
            ):
                pass  # consume the async iterator

            self.circuit_breaker.record_success()

        except (PermissionError, MemoryError):
            # H6: local errors should not trip the circuit breaker
            raise
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

    # C1: async investigator + M4: wall-clock timeout
    async def _run_investigator(self, issue_description: str) -> str:
        """Run an investigator sub-agent for a specific issue.

        Returns the investigation result text.
        """
        try:
            from claude_agent_sdk import query
            from claude_agent_sdk.types import ClaudeAgentOptions

            system_prompt = build_investigator_prompt(
                self.config, issue_description
            )

            tools = create_investigator_tools(
                self.config, self.memory_store
            )

            hooks = build_sdk_hooks(self.config, self.rate_limiter)

            max_turns = min(30, self.config.max_turns)

            options = ClaudeAgentOptions(
                model=self.config.model,
                max_turns=max_turns,
                system_prompt=system_prompt,
                mcp_servers={"agent-mon-investigator": tools},
                hooks=hooks,
                permission_mode="bypassPermissions",
            )

            async def _run():
                last_text = ""
                async for msg in query(
                    prompt=f"Investigate this issue: {issue_description}",
                    options=options,
                ):
                    # Capture the last result message text
                    if hasattr(msg, "type") and msg.get("type") == "result":
                        last_text = str(msg)
                return last_text

            result = await asyncio.wait_for(
                _run(),
                timeout=_INVESTIGATOR_TIMEOUT,
            )

            return result or "Investigation complete (no output)."

        except asyncio.TimeoutError:
            logger.error("Investigator timed out for: %s", issue_description)
            return f"Investigation timed out after {_INVESTIGATOR_TIMEOUT}s."
        except Exception as exc:
            logger.error("Investigator failed: %s", exc)
            return f"Investigation failed: {exc}"

    # C3: non-blocking subprocess + H5: check resend key
    async def _send_heartbeat(self):
        """Send a heartbeat email via Resend."""
        hostname = socket.gethostname()
        resend_key = os.environ.get("RESEND_API_KEY", "")

        # H5: don't send with empty bearer token
        if not resend_key:
            logger.warning("Heartbeat: RESEND_API_KEY is empty, skipping")
            return

        # Collect basic metrics via subprocess (non-blocking)
        body_parts = [f"Heartbeat from agent-mon@{hostname}\n"]

        try:
            result = await _async_subprocess(["uptime"])
            if result.returncode == 0:
                body_parts.append(f"Uptime: {result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = await _async_subprocess(["free", "-m"])
            if result.returncode == 0:
                body_parts.append(f"Memory:\n{result.stdout.strip()}")
        except (subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = await _async_subprocess(["df", "-h"])
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
