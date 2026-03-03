<p align="center">
  <h1 align="center">agent-mon</h1>
  <p align="center">
    <strong>AI-powered system monitoring that thinks before it alerts.</strong>
  </p>
  <p align="center">
    Built on the <a href="https://docs.anthropic.com/en/docs/agent-sdk">Anthropic Agent SDK</a>
    &middot; Powered by Claude
    &middot; Runs as a systemd daemon
  </p>
</p>

<br>

```
  Scheduler        Claude               Tools                  You
  ─────────       ───────        ─────────────────         ─────────
      |               |                |                       |
      |──── tick ────>|                |                       |
      |               |── get_cpu ────>|                       |
      |               |── get_mem ────>|                       |
      |               |── get_disk ───>|                       |
      |               |── containers ->|                       |
      |               |                |                       |
      |               |  (correlates signals,                  |
      |               |   reasons about root cause)            |
      |               |                |                       |
      |               |── send_alert ─────────────────────────>| [WARNING] nginx: 92% CPU
      |               |── restart ────>| nginx restarted       |
      |               |── re-check ──>| nginx healthy          |
      |               |── send_alert ─────────────────────────>| [INFO] nginx recovered
      |               |                |                       |
      |<── summary ───|                |                       |
      |                                                        |
      | ~~~~ sleeps check_interval ~~~~                        |
```

---

## Why agent-mon?

Traditional monitors fire when metric X crosses threshold Y. That's it.

agent-mon uses Claude to **correlate signals across your entire system** and
reason about what's actually happening:

- High CPU + high disk I/O + specific process = "your backup cron is running, not an incident"
- Container restarting + memory spike + OOM in dmesg = "memory leak in the app, not infra"
- 50 failed SSH attempts from one IP + new listening port = "possible breach, escalate now"

It collects, analyzes, alerts, and auto-remediates — every 5 minutes, unattended,
for less than a dollar a day.

---

## Features

**Monitoring** &mdash; 8 built-in tools covering the full stack

| Tool | What it checks |
|------|---------------|
| `get_cpu_info` | Per-core usage, load averages, top CPU consumers |
| `get_memory_info` | RAM/swap usage, top memory consumers |
| `get_disk_info` | Partition usage, flags >85% full |
| `get_io_info` | Disk + network I/O, errors, packet drops |
| `get_process_list` | Running processes, zombies, resource hogs |
| `get_security_info` | Failed SSH logins, open ports, sudo commands, world-writable files |
| `get_system_issues` | OOM events, failed systemd units, NTP drift, pending updates |
| Docker MCP | Container status, health, logs, stats (via [mcp/docker](https://hub.docker.com/r/mcp/docker)) |

**Alerting** &mdash; three channels, zero spam

| Channel | Behavior |
|---------|----------|
| stdout / journald | Every alert, colored by severity |
| JSON Lines log | Append-only `.jsonl` with automatic rotation (10MB x 5 files) |
| Email via [Resend](https://resend.com) | Warning + critical only, deduplicated per title per 15 min |

**Auto-remediation** &mdash; config-gated, rate-limited, audited

| Action | Guard |
|--------|-------|
| Restart container | Config allow-list + PreToolUse hook + 3/hr rate limit |
| Restart systemd service | Config allow-list + 3/hr rate limit |
| Kill runaway process | Config allow-list + PID/name verification at execution time |

The agent always alerts **before** attempting remediation, then re-checks
afterward to confirm the fix worked.

**Resilience** &mdash; never goes dark

- **Circuit breaker**: If the Anthropic API fails 3 times, switches to degraded
  mode (Python-only critical checks for disk/memory/CPU) and sends a meta-alert
- **Graceful shutdown**: SIGTERM/SIGINT handlers finish the in-flight check
  cycle, close connections, and flush logs before exit
- **Auto-recovery**: When the API comes back, resumes full AI monitoring and
  sends a recovery notification

---

## Quick Start

### Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- Docker (optional, for container monitoring)
- A [Resend API key](https://resend.com/) (optional, for email alerts)

### Install

```bash
git clone https://github.com/yourorg/agent-mon.git
cd agent-mon
pip install .
```

### Configure

```bash
cp config.yaml my-config.yaml
# Edit thresholds, alert channels, and remediation allow-lists
```

Set your API keys:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export RESEND_API_KEY=re_...          # optional
```

### Run

```bash
# One-shot: run a single check and exit
agent-mon --once --config my-config.yaml

# Daemon: run continuously at the configured interval
agent-mon --config my-config.yaml

# Interactive: ask the agent questions about your system
agent-mon --interactive --config my-config.yaml
```

### Deploy as a systemd service

```bash
# Create service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin agent-mon
sudo usermod -aG docker agent-mon

# Set up directories and config
sudo mkdir -p /etc/agent-mon /var/log/agent-mon
sudo chown agent-mon:agent-mon /var/log/agent-mon
sudo cp my-config.yaml /etc/agent-mon/config.yaml

# Set up secrets
sudo tee /etc/agent-mon/env > /dev/null << 'EOF'
ANTHROPIC_API_KEY=sk-ant-...
RESEND_API_KEY=re_...
EOF
sudo chmod 600 /etc/agent-mon/env

# Install and start
sudo cp agent-mon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-mon
```

```bash
# Check status
sudo systemctl status agent-mon

# Tail logs
sudo journalctl -u agent-mon -f

# View alert history
cat /var/log/agent-mon/alerts.jsonl | jq .
```

---

## Configuration

```yaml
check_interval: 300                   # seconds between checks (default: 5 min)
model: haiku                          # haiku | sonnet | opus
max_turns: 25                         # max agent tool calls per cycle

thresholds:
  cpu_warning: 80                     # percent
  cpu_critical: 95
  memory_warning: 80
  memory_critical: 95
  disk_warning: 85
  disk_critical: 95
  swap_warning: 50

alerts:
  stdout: true
  log_file: /var/log/agent-mon/alerts.jsonl
  log_max_size_mb: 10                 # rotate after this size
  log_max_files: 5                    # keep this many rotated files
  email:
    enabled: true
    from: "agent-mon@yourdomain.com"
    to: ["ops@company.com"]
    min_severity: warning             # only email for warning + critical
    dedup_window_minutes: 15          # suppress duplicate emails

docker:
  enabled: true                       # set false if no Docker on this host

remediation:
  enabled: true
  allowed_restart_containers:         # only these containers can be restarted
    - nginx
    - redis
    - postgres
  allowed_restart_services:           # only these systemd services
    - nginx
    - docker
  allowed_kill_targets:               # only these process names can be killed
    - defunct_worker
  max_restart_attempts: 3             # per container/service per hour
```

---

## How It Works

Each check cycle follows this protocol:

```
1. COLLECT   Call all monitoring tools to gather system state
2. ANALYZE   Correlate signals across metrics — what's actually happening?
3. ALERT     Fire send_alert for every issue, with severity + context
4. REMEDIATE Take action on allowed targets (alert first, act second)
5. VERIFY    Re-check after remediation to confirm the fix
6. SUMMARIZE Return a brief status report
```

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                       agent-mon                          │
│                                                          │
│  Scheduler ──> Agent Loop (Claude) ──> Alert Dispatch    │
│                     |                   - stdout         │
│                     | calls tools       - .jsonl log     │
│                ┌────┴─────┐             - email (Resend) │
│                |          |                              │
│          SDK MCP Server   Docker MCP Server              │
│          (in-process)     (external, stdio)              │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Config (YAML): thresholds, alerts, remediation    │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

Two MCP servers, one agent:

- **SDK MCP server** (in-process) — system monitoring tools, remediation tools,
  and alerting tools, all defined as Python functions with `@tool`
- **Docker MCP server** (external) — the official
  [mcp/docker](https://hub.docker.com/r/mcp/docker) image, communicating over
  stdio. Provides container listing, inspection, logs, stats, and restart.

---

## Safety

agent-mon is designed to run unattended. Every remediation action is gated by
multiple independent safety layers:

```
   LLM decides to restart a container
                  |
                  v
   [1] allowed_tools list ── tool not listed? ── BLOCKED
                  |
                  v
   [2] PreToolUse hook ── container not in allow-list? ── DENIED
                  |
                  v
   [3] Rate limiter ── >3 restarts/hr for this container? ── DENIED
                  |
                  v
   Action executes
```

| Layer | Mechanism | Bypassable by LLM? |
|-------|-----------|---------------------|
| Tool allowlist | `PreToolUse` catch-all hook on `.*` | No |
| Remediation allow-list | Python function checks config before executing | No |
| Docker MCP gate | `PreToolUse` hook checks container name | No |
| Rate limiting | In-memory counter per target per hour | No |
| PID verification | Re-reads `/proc/{pid}/comm` + start time before `kill` | No |
| Permission mode | `acceptEdits` (not `bypassPermissions`) | No |
| No shell access | Bash, Write, Edit tools are not available | No |
| Audit log | Every action logged to `.jsonl` with timestamp | N/A |

---

## Cost

| Interval | Checks/day | Est. daily cost (Haiku) |
|----------|-----------|------------------------|
| 5 min | 288 | $0.30 - $0.85 |
| 1 min | 1,440 | $1.50 - $4.30 |

Each check uses ~2-4K input tokens and ~500-1K output tokens. The default
model is Haiku for cost efficiency. Switch to Sonnet or Opus in config for
deeper analysis when needed.

---

## Project Structure

```
agent-mon/
├── pyproject.toml                    # Package config + dependencies
├── config.yaml                       # Default configuration
├── agent-mon.service                 # Systemd unit file
├── DESIGN.md                         # Full architecture document
├── agent_mon/
│   ├── __init__.py
│   ├── cli.py                        # Entry point, argument parsing
│   ├── agent.py                      # Agent loop, scheduler, circuit breaker
│   ├── config.py                     # Config loading & validation
│   ├── tools/
│   │   ├── __init__.py               # MCP server setup, tool registration
│   │   ├── cpu.py                    # get_cpu_info
│   │   ├── memory.py                 # get_memory_info
│   │   ├── disk.py                   # get_disk_info
│   │   ├── io.py                     # get_io_info
│   │   ├── processes.py              # get_process_list
│   │   ├── security.py               # get_security_info
│   │   ├── system.py                 # get_system_issues
│   │   ├── remediation.py            # kill_process, restart_service
│   │   └── alerts.py                 # send_alert, get_alert_history
│   └── prompt.py                     # System prompt template builder
└── README.md
```

---

## Roadmap

**v0.1** (current) &mdash; single-host monitoring, email alerts, auto-remediation

Planned improvements:

- Health check endpoint / heartbeat file for external monitoring
- Flapping detection to suppress noisy oscillating alerts
- Prometheus metrics export (`/metrics`)
- First-run baseline to reduce false positives on deploy
- Resend API error handling with exponential backoff
- Config validation with fail-fast at startup
- Alert severity routing (critical to PagerDuty, warning to email)
- Stronger systemd hardening (target security score <4.0)

**Future**

- Multi-host monitoring via SSH MCP
- Webhook alert channel (Slack, Teams, Discord, PagerDuty)
- Metrics history with SQLite + trend analysis
- Web dashboard
- Dry-run / audit mode
- Token budget enforcement
- LLM-powered runbook suggestions in alerts

---

## License

TBD
