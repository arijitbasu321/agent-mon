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
class EmailConfig:
    enabled: bool = False
    from_addr: str = ""
    to: list[str] = field(default_factory=list)
    min_severity: str = "warning"
    dedup_window_minutes: int = 15


@dataclass
class AlertsConfig:
    log_file: str = "/var/log/agent-mon.log"
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass
class HeartbeatConfig:
    enabled: bool = False
    interval: int = 3600


@dataclass
class WatchedProcessConfig:
    name: str
    restart_command: str


@dataclass
class RemediationConfig:
    enabled: bool = False
    allowed_restart_containers: list[str] = field(default_factory=list)
    allowed_restart_services: list[str] = field(default_factory=list)
    max_restart_attempts: int = 3


@dataclass
class DockerConfig:
    enabled: bool = False


@dataclass
class BashConfig:
    deny_list: list[str] = field(default_factory=list)


@dataclass
class MemoryConfig:
    enabled: bool = True
    path: str = "/var/lib/agent-mon/memory"
    collection_name: str = "agent_mon_memory"
    max_results: int = 5
    max_entries: int = 10000


@dataclass
class Config:
    check_interval: int
    model: str
    max_turns: int
    alerts: AlertsConfig
    remediation: RemediationConfig
    bash: BashConfig
    memory: MemoryConfig
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    watched_processes: list[WatchedProcessConfig] = field(default_factory=list)
    watched_containers: list[str] = field(default_factory=list)
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

        check_interval = raw["check_interval"]
        if not isinstance(check_interval, (int, float)) or check_interval < 30:
            raise ConfigError("check_interval must be >= 30 seconds")

        max_turns = raw.get("max_turns", 100)
        if not isinstance(max_turns, int) or max_turns <= 0:
            raise ConfigError("max_turns must be a positive integer")

        # --- alerts ---
        alerts = cls._parse_alerts(raw.get("alerts", {}))

        # --- heartbeat ---
        heartbeat = cls._parse_heartbeat(raw.get("heartbeat", {}))

        # --- watched processes ---
        watched_processes = cls._parse_watched_processes(
            raw.get("watched_processes", [])
        )

        # --- watched containers ---
        watched_containers = raw.get("watched_containers", [])

        # --- remediation ---
        remediation = cls._parse_remediation(
            raw.get("remediation", {}), watched_containers
        )

        # --- docker ---
        docker_raw = raw.get("docker", {})
        docker = DockerConfig(enabled=docker_raw.get("enabled", False))

        # --- bash ---
        bash = cls._parse_bash(raw.get("bash", {}))

        # --- memory ---
        memory = cls._parse_memory(raw.get("memory", {}))

        return cls(
            check_interval=int(check_interval),
            model=raw["model"],
            max_turns=max_turns,
            alerts=alerts,
            heartbeat=heartbeat,
            watched_processes=watched_processes,
            watched_containers=watched_containers,
            remediation=remediation,
            docker=docker,
            bash=bash,
            memory=memory,
        )

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
            log_file=raw.get("log_file", "/var/log/agent-mon.log"),
            email=email,
        )

    @classmethod
    def _parse_heartbeat(cls, raw: dict) -> HeartbeatConfig:
        if not raw:
            return HeartbeatConfig()

        enabled = raw.get("enabled", False)
        interval = raw.get("interval", 3600)

        if isinstance(interval, (int, float)) and interval < 60:
            raise ConfigError("heartbeat interval must be >= 60 seconds")

        return HeartbeatConfig(
            enabled=enabled,
            interval=int(interval),
        )

    @classmethod
    def _parse_watched_processes(
        cls, raw: list,
    ) -> list[WatchedProcessConfig]:
        if not raw:
            return []

        result = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ConfigError("Each watched_process must be a mapping")
            if "name" not in entry:
                raise ConfigError(
                    "Each watched_process must have a 'name' field"
                )
            if "restart_command" not in entry:
                raise ConfigError(
                    "Each watched_process must have a 'restart_command' field"
                )
            result.append(WatchedProcessConfig(
                name=entry["name"],
                restart_command=entry["restart_command"],
            ))
        return result

    @classmethod
    def _parse_remediation(
        cls, raw: dict, watched_containers: list[str],
    ) -> RemediationConfig:
        if not raw:
            return RemediationConfig()

        enabled = raw.get("enabled", False)

        # Auto-merge watched_containers into allowed_restart_containers
        explicit_containers = raw.get("allowed_restart_containers", [])
        merged_containers = list(dict.fromkeys(
            explicit_containers + watched_containers
        ))

        config = RemediationConfig(
            enabled=enabled,
            allowed_restart_containers=merged_containers,
            allowed_restart_services=raw.get("allowed_restart_services", []),
            max_restart_attempts=raw.get("max_restart_attempts", 3),
        )

        if config.max_restart_attempts <= 0:
            raise ConfigError("max_restart_attempts must be positive")

        if enabled:
            has_targets = (
                config.allowed_restart_containers
                or config.allowed_restart_services
            )
            if not has_targets:
                raise ConfigError(
                    "remediation is enabled but all allow-lists are empty"
                )

        return config

    @classmethod
    def _parse_bash(cls, raw: dict) -> BashConfig:
        if not raw:
            return BashConfig()

        return BashConfig(
            deny_list=raw.get("deny_list", []),
        )

    @classmethod
    def _parse_memory(cls, raw: dict) -> MemoryConfig:
        if not raw:
            return MemoryConfig()

        return MemoryConfig(
            enabled=raw.get("enabled", True),
            path=raw.get("path", "/var/lib/agent-mon/memory"),
            collection_name=raw.get("collection_name", "agent_mon_memory"),
            max_results=raw.get("max_results", 5),
            max_entries=raw.get("max_entries", 10000),
        )

    # ------------------------------------------------------------------
    # Environment validation
    # ------------------------------------------------------------------

    def validate_env(self) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ConfigError("ANTHROPIC_API_KEY environment variable is not set")

        needs_resend = (
            self.alerts.email.enabled or self.heartbeat.enabled
        )
        if needs_resend and not os.environ.get("RESEND_API_KEY"):
            raise ConfigError(
                "RESEND_API_KEY environment variable is not set "
                "(required when email alerts or heartbeat are enabled)"
            )
