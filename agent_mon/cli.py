"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from agent_mon.agent import AgentDaemon
from agent_mon.config import Config, ConfigError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent-mon",
        description="AI-powered system monitoring agent",
    )
    parser.add_argument(
        "--config",
        default="/etc/agent-mon/config.yaml",
        help="Path to config.yaml (default: /etc/agent-mon/config.yaml)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run a single check cycle and exit",
    )
    mode.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Interactive mode: ask the agent questions",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        stream=sys.stderr,
    )

    try:
        config = Config.from_file(args.config)
    except (FileNotFoundError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        config.validate_env()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Add file handler for real-time activity logging
    try:
        from pathlib import Path
        log_path = Path(config.alerts.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.alerts.log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        print(f"Warning: could not open log file: {exc}", file=sys.stderr)

    daemon = AgentDaemon(config)

    if args.once:
        # H7: use run_once() which initializes properly
        asyncio.run(daemon.run_once())
    elif args.interactive:
        # M8: explicit error instead of silent no-op
        print("Error: --interactive mode is not yet implemented", file=sys.stderr)
        sys.exit(1)
    else:
        asyncio.run(daemon.run())
