"""send_alert and get_alert_history tools."""

from __future__ import annotations

import logging
import os
import re
import socket
import time
from collections import deque
from pathlib import Path

import aiohttp

from agent_mon.config import Config

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# Log rotation settings (H4)
_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_LOG_BACKUPS = 3

# ---------------------------------------------------------------------------
# Secret sanitizer (H1: expanded patterns)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # Anthropic API keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    # AWS access keys
    re.compile(r"AKIA[A-Z0-9]{16}"),
    # GitHub PATs
    re.compile(r"gh[pos]_[A-Za-z0-9_]{20,}"),
    # GitLab PATs
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    # Bearer tokens (JWT-style)
    re.compile(r"Bearer\s+eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # Resend keys
    re.compile(r"re_[A-Za-z0-9_]{20,}"),
    # Slack tokens
    re.compile(r"xox[bp]-[A-Za-z0-9-]{20,}"),
    # Generic sk- style keys (OpenAI, etc.)
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # Generic key/secret/token assignments (H1: expanded to cover more patterns)
    re.compile(
        r"(password|secret|api_key|auth_token|access_token|refresh_token"
        r"|client_secret|db_password|private_key"
        r"|DATABASE_URL|MONGO_URI|REDIS_URL)\s*=\s*\S+",
        re.IGNORECASE,
    ),
]


def sanitize_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class AlertManager:
    """Manages alert dispatch: plain text log file + email via Resend + Slack."""

    def __init__(self, config: Config):
        self.config = config
        self.http_session: aiohttp.ClientSession | None = None
        self.hostname = socket.gethostname()

        # Email dedup tracking: title -> last_sent_timestamp
        self._email_dedup: dict[str, float] = {}
        # Slack dedup tracking: title -> last_sent_timestamp
        self._slack_dedup: dict[str, float] = {}

        # Ensure log directory exists
        log_path = Path(config.alerts.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    async def send_alert(
        self, severity: str, title: str, message: str
    ) -> str:
        """Dispatch alert to log file and optionally email."""
        # M1: dedup on original title before sanitization
        original_title = title

        # Sanitize secrets before any output
        title = sanitize_secrets(title)
        message = sanitize_secrets(message)

        results = []

        # H4: rotate log if needed
        self._rotate_log_if_needed()

        # 1. Append to plain text log file
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_line = f"[{timestamp}] [{severity.upper()}] {title}: {message}\n"

        try:
            with open(self.config.alerts.log_file, "a") as f:
                f.write(log_line)
            results.append("log: written")
        except OSError as exc:
            logger.warning("Failed to write alert log: %s", exc)
            results.append(f"log: failed ({exc})")

        # 2. Email via Resend
        email_config = self.config.alerts.email
        if (
            email_config.enabled
            and self.http_session is not None
            and SEVERITY_RANK.get(severity, 0)
            >= SEVERITY_RANK.get(email_config.min_severity, 1)
        ):
            # M1: dedup on original title
            if self._should_send_email(original_title):
                try:
                    resp = await self.http_session.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {self._get_resend_key()}"
                        },
                        json={
                            "from": email_config.from_addr,
                            "to": email_config.to,
                            "subject": (
                                f"[{severity.upper()}] "
                                f"agent-mon@{self.hostname}: {title}"
                            ),
                            "text": message,
                        },
                    )
                    if resp.status < 300:
                        results.append("email: sent")
                    else:
                        # L6: read error body for diagnostics
                        try:
                            error_body = await resp.text()
                        except Exception:
                            error_body = ""
                        results.append(
                            f"email: failed (HTTP {resp.status}: {error_body})"
                        )
                except (aiohttp.ClientError, Exception) as exc:
                    logger.warning("Email send failed: %s", exc)
                    results.append(f"email: failed ({exc})")
            else:
                results.append("email: deduplicated")

        # 3. Slack webhook
        slack_config = self.config.alerts.slack
        if (
            slack_config.enabled
            and self.http_session is not None
            and SEVERITY_RANK.get(severity, 0)
            >= SEVERITY_RANK.get(slack_config.min_severity, 1)
        ):
            if self._should_send_slack(original_title):
                try:
                    resp = await self.http_session.post(
                        self._get_slack_webhook_url(),
                        json={
                            "text": (
                                f"*[{severity.upper()}]* "
                                f"`agent-mon@{self.hostname}`: {title}\n"
                                f"{message}"
                            ),
                        },
                    )
                    if resp.status < 300:
                        results.append("slack: sent")
                    else:
                        try:
                            error_body = await resp.text()
                        except Exception:
                            error_body = ""
                        results.append(
                            f"slack: failed (HTTP {resp.status}: {error_body})"
                        )
                except (aiohttp.ClientError, Exception) as exc:
                    logger.warning("Slack send failed: %s", exc)
                    results.append(f"slack: failed ({exc})")
            else:
                results.append("slack: deduplicated")

        return "; ".join(results)

    def _should_send_email(self, title: str) -> bool:
        """Check dedup window for this alert title."""
        now = time.time()
        window = self.config.alerts.email.dedup_window_minutes * 60

        # L2: prune expired entries to prevent unbounded growth
        self._email_dedup = {
            t: ts for t, ts in self._email_dedup.items()
            if now - ts < window
        }

        last_sent = self._email_dedup.get(title, 0)
        if now - last_sent < window:
            return False
        self._email_dedup[title] = now
        return True

    def _should_send_slack(self, title: str) -> bool:
        """Check dedup window for this Slack alert title."""
        now = time.time()
        window = self.config.alerts.slack.dedup_window_minutes * 60

        self._slack_dedup = {
            t: ts for t, ts in self._slack_dedup.items()
            if now - ts < window
        }

        last_sent = self._slack_dedup.get(title, 0)
        if now - last_sent < window:
            return False
        self._slack_dedup[title] = now
        return True

    @staticmethod
    def _get_resend_key() -> str:
        return os.environ.get("RESEND_API_KEY", "")

    @staticmethod
    def _get_slack_webhook_url() -> str:
        return os.environ.get("SLACK_WEBHOOK_URL", "")

    # H3: efficient tail-read instead of loading entire file
    def get_alert_history(self, last_n: int = 20) -> str:
        """Return recent alerts from the log file."""
        log_path = Path(self.config.alerts.log_file)
        if not log_path.exists():
            return "No alert history (log file does not exist)"

        try:
            with open(log_path) as f:
                recent = deque(f, maxlen=last_n)
            lines = [line.strip() for line in recent if line.strip()]
        except OSError as exc:
            return f"Failed to read alert history: {exc}"

        if not lines:
            return "No alert history (log file is empty)"

        return "\n".join(lines)

    # H4: size-based log rotation
    def _rotate_log_if_needed(self) -> None:
        """Rotate the alert log file if it exceeds the size limit."""
        log_path = Path(self.config.alerts.log_file)
        try:
            if not log_path.exists():
                return
            if log_path.stat().st_size <= _MAX_LOG_SIZE:
                return
            # Rotate existing backups
            for i in range(_MAX_LOG_BACKUPS - 1, 0, -1):
                src = log_path.with_suffix(f".{i}")
                dst = log_path.with_suffix(f".{i + 1}")
                if src.exists():
                    src.rename(dst)
            # Move current log to .1
            log_path.rename(log_path.with_suffix(".1"))
        except OSError:
            pass
