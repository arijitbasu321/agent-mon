"""send_alert and get_alert_history tools."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from pathlib import Path

import aiohttp

from agent_mon.config import Config

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


class AlertManager:
    """Manages alert dispatch: plain text log file + email via Resend."""

    def __init__(self, config: Config):
        self.config = config
        self.http_session: aiohttp.ClientSession | None = None
        self.hostname = socket.gethostname()

        # Email dedup tracking: title -> last_sent_timestamp
        self._email_dedup: dict[str, float] = {}

        # Ensure log directory exists
        log_path = Path(config.alerts.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    async def send_alert(
        self, severity: str, title: str, message: str
    ) -> str:
        """Dispatch alert to log file and optionally email."""
        results = []

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
            if self._should_send_email(title):
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
                        results.append(f"email: failed (HTTP {resp.status})")
                except (aiohttp.ClientError, Exception) as exc:
                    logger.warning("Email send failed: %s", exc)
                    results.append(f"email: failed ({exc})")
            else:
                results.append("email: deduplicated")

        return "; ".join(results)

    def _should_send_email(self, title: str) -> bool:
        """Check dedup window for this alert title."""
        now = time.time()
        window = self.config.alerts.email.dedup_window_minutes * 60
        last_sent = self._email_dedup.get(title, 0)
        if now - last_sent < window:
            return False
        self._email_dedup[title] = now
        return True

    @staticmethod
    def _get_resend_key() -> str:
        return os.environ.get("RESEND_API_KEY", "")

    def get_alert_history(self, last_n: int = 20) -> str:
        """Return recent alerts from the log file."""
        log_path = Path(self.config.alerts.log_file)
        if not log_path.exists():
            return "No alert history (log file does not exist)"

        text = log_path.read_text()
        lines = [line for line in text.strip().split("\n") if line.strip()]

        if not lines:
            return "No alert history (log file is empty)"

        recent = lines[-last_n:]
        return "\n".join(recent)
