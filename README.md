<p align="center">
  <h1 align="center">agent-mon</h1>
  <p align="center">
    <strong>AI-powered system monitoring that thinks before it alerts.</strong>
  </p>
  <p align="center">
    <a href="https://docs.anthropic.com/en/docs/agent-sdk">Anthropic Agent SDK</a>
    &middot; Powered by Claude
    &middot; Runs as a systemd daemon
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/claude-sonnet_4.6-blueviolet?logo=anthropic&logoColor=white" alt="Claude Sonnet 4.6">
    <img src="https://img.shields.io/badge/systemd-service-gray?logo=linux&logoColor=white" alt="systemd service">
    <img src="https://img.shields.io/badge/docker-optional-2496ED?logo=docker&logoColor=white" alt="Docker optional">
    <img src="https://img.shields.io/badge/license-TBD-lightgrey" alt="License">
  </p>
</p>

<br>

```
  Scheduler        Claude               Tools                  You
  ---------       -------        -----------------         ---------
      |               |                |                       |
      |---- tick ---->|                |                       |
      |               |-- bash ------->| ps aux, df -h, free   |
      |               |-- bash ------->| journalctl, ss -tlnp  |
      |               |                |                       |
      |               |  (correlates signals,                  |
      |               |   checks memory for past issues)       |
      |               |                |                       |
      |               |-- send_alert --------------------------->| [WARNING] nginx: 92% CPU
      |               |-- bash ------->| systemctl restart nginx |
      |               |-- bash ------->| nginx healthy          |
      |               |-- store_memory>| remembers for next time|
      |               |-- send_alert --------------------------->| [INFO] nginx recovered
      |               |                |                       |
      |<-- summary ---|                |                       |
      |                                                        |
      | ~~~~ sleeps check_interval ~~~~                        |
```

---

## Why agent-mon?

Traditional monitors fire when metric X crosses threshold Y. That's it.

agent-mon uses Claude to **investigate your system with bash**, correlate signals,
and reason about what's actually happening:

- High CPU + high disk I/O + specific process = "your backup cron is running, not an incident"
- Container restarting + memory spike + OOM in dmesg = "memory leak in the app, not infra"
- 50 failed SSH attempts from one IP + new listening port = "possible breach, escalate now"

It investigates, analyzes, alerts, remediates, and **remembers** -- every cycle,
unattended. Past observations are stored in a vector database so the agent learns
from previous cycles.

---

## Features

### Bash-first monitoring

The agent uses bash to investigate system health. No custom monitoring tools --
it runs the same commands you would: `ps`, `top`, `df`, `free`, `journalctl`,
`ss`, `systemctl`, `dmesg`, and more.

### Vector memory (ChromaDB)

Observations are persisted across cycles using ChromaDB. The agent queries past
observations at the start of each cycle, enabling it to:
- Recognize recurring patterns
- Avoid redundant alerts
- Track the history of remediation actions

### Alerting -- log file + email, zero spam

| Channel | Behavior |
|---------|----------|
| Log file | Every alert, plain text, append-only |
| Email via [Resend](https://resend.com) | Warning + critical only, deduplicated per title per 15 min |

### Auto-Remediation -- config-gated, rate-limited

| Action | Guard |
|--------|-------|
| Restart systemd service | Config allow-list via bash |
| Restart container | Config allow-list + PreToolUse hook + 3/hr rate limit |

### Safety

| Layer | Mechanism | Bypassable by LLM? |
|-------|-----------|---------------------|
| Bash deny-list | PreToolUse hook, case-insensitive substring match | No |
| Docker MCP gate | PreToolUse hook checks container name | No |
| Rate limiting | In-memory counter per target per hour | No |
| max_turns cap | Limits tool calls per cycle (default 25) | No |
| No process killing | Processes are observed and restarted, never killed | N/A |

### Resilience -- never goes dark

- **Circuit breaker**: If the Anthropic API fails 3 times in a row, agent-mon
  switches to degraded mode (subprocess-based critical checks for disk/memory/load)
  and sends a meta-alert.
- **Graceful shutdown**: SIGTERM/SIGINT handlers finish the in-flight check
  cycle, close connections, and exit cleanly.
- **Auto-recovery**: When the API comes back, full AI monitoring resumes.

---

## Quick Start

### Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- Docker (optional, for container monitoring)
- A [Resend API key](https://resend.com/) (optional, for email alerts and heartbeat)

### 1. Clone and configure

```bash
git clone https://github.com/yourorg/agent-mon.git
cd agent-mon
```

Edit `config.yaml` with your watched processes, containers, alert settings,
and bash deny-list.

Set your API keys:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export RESEND_API_KEY=re_...          # optional, for email alerts
```

### 2. Run the installer

```bash
sudo ./install.sh
```

### 3. Start the service

```bash
sudo systemctl start agent-mon
```

### Other run modes

```bash
# One-shot: run a single check and exit
agent-mon --once --config config.yaml

# Daemon: run continuously without systemd
agent-mon --config config.yaml

# Interactive: ask the agent questions about your system
agent-mon --interactive --config config.yaml
```

---

## Configuration

All configuration lives in a single `config.yaml` file. See [config.yaml](config.yaml)
for the full annotated example.

Key sections:

| Section | Purpose |
|---------|---------|
| `check_interval` | Seconds between monitoring cycles (min: 30) |
| `model` | Claude model to use |
| `max_turns` | Max agent tool calls per cycle |
| `heartbeat` | Periodic health email |
| `watched_processes` | Processes to monitor and auto-restart |
| `watched_containers` | Docker containers to watch |
| `alerts` | Log file path + email config |
| `remediation` | Allow-lists for auto-remediation |
| `bash.deny_list` | Commands the agent cannot run |
| `memory` | ChromaDB vector memory settings |

### Environment Variables

```bash
ANTHROPIC_API_KEY=sk-ant-...       # Required
RESEND_API_KEY=re_...              # Required if email alerts or heartbeat are enabled
```

---

## Architecture

```
+---------------------------------------------------------+
|                       agent-mon                         |
|                                                         |
|  Scheduler --> Agent Loop (Claude) --> Alert Dispatch   |
|                     |                   - log file      |
|                     | calls tools       - email (Resend)|
|                +----+-----+                             |
|                |    |     |                              |
|           Bash  Docker  Memory (ChromaDB)               |
|           tool  MCP     store/query                     |
|                                                         |
|  +---------------------------------------------------+ |
|  |  Config (YAML): alerts, remediation, bash deny-   | |
|  |  list, watched processes/containers, memory        | |
|  +---------------------------------------------------+ |
+---------------------------------------------------------+
```

---

## Project Structure

```
agent-mon/
├── pyproject.toml                    # Package config + dependencies
├── config.yaml                       # Default configuration
├── install.sh                        # Installer script
├── agent-mon.service                 # Systemd unit file
├── DESIGN.md                         # Full architecture document
├── agent_mon/
│   ├── __init__.py
│   ├── cli.py                        # Entry point, argument parsing
│   ├── agent.py                      # Agent loop, scheduler, circuit breaker
│   ├── config.py                     # Config loading & validation
│   ├── hooks.py                      # PreToolUse guards: bash deny-list, Docker
│   ├── memory.py                     # ChromaDB vector memory
│   ├── prompt.py                     # System prompt template builder
│   └── tools/
│       ├── __init__.py               # Tool registration
│       └── alerts.py                 # send_alert, get_alert_history
└── tests/
```

---

## License

TBD
