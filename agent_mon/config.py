"""Config loading, validation, and access."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ThresholdsConfig:
    cpu_warning: int
    cpu_critical: int
    memory_warning: int
    memory_critical: int
    disk_warning: int
    disk_critical: int
    swap_warning: int


@dataclass
class EmailConfig:
    enabled: bool = False
    from_addr: str = ""
    to: list[str] = field(default_factory=list)
    min_severity: str = "warning"
    dedup_window_minutes: int = 15


@dataclass
class AlertsConfig:
    stdout: bool = True
    log_file: str = "/var/log/agent-mon/alerts.jsonl"
    log_max_size_mb: int = 10
    log_max_files: int = 5
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass
class RemediationConfig:
    enabled: bool = False
    allowed_restart_containers: list[str] = field(default_factory=list)
    allowed_restart_services: list[str] = field(default_factory=list)
    allowed_kill_targets: list[str] = field(default_factory=list)
    max_restart_attempts: int = 3


@dataclass
class DockerConfig:
    enabled: bool = False


@dataclass
class Config:
    check_interval: int
    model: str
    max_turns: int
    thresholds: ThresholdsConfig
    alerts: AlertsConfig
    remediation: RemediationConfig
    docker: DockerConfig = field(default_factory=DockerConfig)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        text = path.read_text()
        if not text or not text.strip():
            raise ConfigError("Config file is empty")

        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise ConfigError("Config file must be a YAML mapping")

        return cls._parse(raw)

    @classmethod
    def _parse(cls, raw: dict) -> Config:
        # --- required top-level fields ---
        if "check_interval" not in raw:
            raise ConfigError("Missing required field: check_interval")
        if "model" not in raw:
            raise ConfigError("Missing required field: model")
        if "thresholds" not in raw:
            raise ConfigError("Missing required field: thresholds")

        check_interval = raw["check_interval"]
        if not isinstance(check_interval, (int, float)) or check_interval < 30:
            raise ConfigError("check_interval must be >= 30 seconds")

        max_turns = raw.get("max_turns", 25)
        if not isinstance(max_turns, int) or max_turns <= 0:
            raise ConfigError("max_turns must be a positive integer")

        # --- thresholds ---
        thresholds = cls._parse_thresholds(raw["thresholds"])

        # --- alerts ---
        alerts = cls._parse_alerts(raw.get("alerts", {}))

        # --- remediation ---
        remediation = cls._parse_remediation(raw.get("remediation", {}))

        # --- docker ---
        docker_raw = raw.get("docker", {})
        docker = DockerConfig(enabled=docker_raw.get("enabled", False))

        return cls(
            check_interval=int(check_interval),
            model=raw["model"],
            max_turns=max_turns,
            thresholds=thresholds,
            alerts=alerts,
            remediation=remediation,
            docker=docker,
        )

    @classmethod
    def _parse_thresholds(cls, raw: dict) -> ThresholdsConfig:
        required = [
            "cpu_warning", "cpu_critical",
            "memory_warning", "memory_critical",
            "disk_warning", "disk_critical",
            "swap_warning",
        ]
        for key in required:
            if key not in raw:
                raise ConfigError(f"Missing threshold: {key}")

        for key, val in raw.items():
            if not isinstance(val, (int, float)):
                raise ConfigError(f"Threshold {key} must be numeric")
            if val < 0 or val > 100:
                raise ConfigError(f"Threshold {key} must be 0-100, got {val}")

        # warning < critical checks
        for metric in ("cpu", "memory", "disk"):
            w = raw[f"{metric}_warning"]
            c = raw[f"{metric}_critical"]
            if w >= c:
                raise ConfigError(
                    f"{metric}_warning ({w}) must be less than "
                    f"{metric}_critical ({c})"
                )

        return ThresholdsConfig(**{k: raw[k] for k in required})

    @classmethod
    def _parse_alerts(cls, raw: dict) -> AlertsConfig:
        if not raw:
            return AlertsConfig()

        email_raw = raw.get("email", {})
        email = EmailConfig(
            enabled=email_raw.get("enabled", False),
            from_addr=email_raw.get("from", ""),
            to=email_raw.get("to", []),
            min_severity=email_raw.get("min_severity", "warning"),
            dedup_window_minutes=email_raw.get("dedup_window_minutes", 15),
        )

        valid_severities = {"info", "warning", "critical"}
        if email.min_severity not in valid_severities:
            raise ConfigError(
                f"Invalid min_severity '{email.min_severity}', "
                f"must be one of {valid_severities}"
            )

        return AlertsConfig(
            stdout=raw.get("stdout", True),
            log_file=raw.get("log_file", "/var/log/agent-mon/alerts.jsonl"),
            log_max_size_mb=raw.get("log_max_size_mb", 10),
            log_max_files=raw.get("log_max_files", 5),
            email=email,
        )

    @classmethod
    def _parse_remediation(cls, raw: dict) -> RemediationConfig:
        if not raw:
            return RemediationConfig()

        enabled = raw.get("enabled", False)
        config = RemediationConfig(
            enabled=enabled,
            allowed_restart_containers=raw.get("allowed_restart_containers", []),
            allowed_restart_services=raw.get("allowed_restart_services", []),
            allowed_kill_targets=raw.get("allowed_kill_targets", []),
            max_restart_attempts=raw.get("max_restart_attempts", 3),
        )

        if config.max_restart_attempts <= 0:
            raise ConfigError("max_restart_attempts must be positive")

        if enabled:
            has_targets = (
                config.allowed_restart_containers
                or config.allowed_restart_services
                or config.allowed_kill_targets
            )
            if not has_targets:
                raise ConfigError(
                    "remediation is enabled but all allow-lists are empty"
                )

        return config

    # ------------------------------------------------------------------
    # Environment validation
    # ------------------------------------------------------------------

    def validate_env(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigError("ANTHROPIC_API_KEY environment variable is not set")

        if self.alerts.email.enabled and not os.environ.get("RESEND_API_KEY"):
            raise ConfigError(
                "RESEND_API_KEY environment variable is not set "
                "(required when email alerts are enabled)"
            )
