"""send_alert and get_alert_history tools."""

from __future__ import annotations

import json
import logging
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp

from agent_mon.config import Config

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "info": "\033[36m",      # cyan
    "warning": "\033[33m",   # yellow
    "critical": "\033[31m",  # red
}
RESET = "\033[0m"

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


class AlertManager:
    """Manages alert dispatch: stdout, JSON Lines log, email via Resend."""

    def __init__(
        self,
        config: Config,
        *,
        max_bytes: int | None = None,
    ):
        self.config = config
        self.http_session: aiohttp.ClientSession | None = None
        self.hostname = socket.gethostname()

        # Email dedup tracking: title -> last_sent_timestamp
        self._email_dedup: dict[str, float] = {}

        # Set up log handler
        log_path = Path(config.alerts.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if max_bytes is not None:
            mb = max_bytes
        else:
            mb = config.alerts.log_max_size_mb * 1024 * 1024

        self.log_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=mb,
            backupCount=config.alerts.log_max_files,
        )

    async def send_alert(
        self, severity: str, title: str, message: str
    ) -> str:
        """Dispatch alert to all configured channels."""
        results = []

        # 1. stdout
        if self.config.alerts.stdout:
            color = SEVERITY_COLORS.get(severity, "")
            print(
                f"{color}[{severity.upper()}] {title}: {message}{RESET}",
                file=sys.stderr,
            )
            results.append("stdout: sent")

        # 2. JSON Lines log
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "severity": severity,
            "title": title,
            "message": message,
            "hostname": self.hostname,
        }
        line = json.dumps(record)
        log_record = logging.LogRecord(
            name="agent_mon.alerts",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=line,
            args=(),
            exc_info=None,
        )
        self.log_handler.emit(log_record)
        results.append("log: written")

        # 3. Email via Resend
        email_config = self.config.alerts.email
        if (
            email_config.enabled
            and self.http_session is not None
            and SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(email_config.min_severity, 1)
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
        import os
        return os.environ.get("RESEND_API_KEY", "")

    def get_alert_history(self, last_n: int = 20) -> str:
        """Return recent alerts from the JSON Lines log file."""
        log_path = Path(self.config.alerts.log_file)
        if not log_path.exists():
            return "No alert history (log file does not exist)"

        text = log_path.read_text()
        lines = [l for l in text.strip().split("\n") if l.strip()]

        if not lines:
            return "No alert history (log file is empty)"

        recent = lines[-last_n:]
        entries = []
        for line in recent:
            try:
                record = json.loads(line)
                entries.append(
                    f"[{record.get('timestamp', '?')}] "
                    f"[{record.get('severity', '?').upper()}] "
                    f"{record.get('title', '?')}: {record.get('message', '')}"
                )
            except json.JSONDecodeError:
                continue

        return "\n".join(entries) if entries else "No alert history"
