"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import asyncio
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

    daemon = AgentDaemon(config)

    if args.once:
        asyncio.run(daemon._run_check_cycle())
    elif args.interactive:
        # Placeholder for interactive mode
        pass
    else:
        asyncio.run(daemon.run())
