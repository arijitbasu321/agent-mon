# agent-mon: System Monitoring Agent — Architecture & Design

## Overview

**agent-mon** is a system monitoring agent powered by the Anthropic Agent SDK. It
uses Claude as the reasoning engine to periodically collect system metrics, detect
anomalies, alert on all incidents, and auto-remediate specific known issues (e.g.
restart a crashed container or kill a runaway process).

Unlike traditional threshold-based monitors, agent-mon uses an LLM to correlate
signals across metrics (e.g. high CPU + high I/O + specific process = diagnosis)
and decide on remediation actions contextually.

---

## High-Level Architecture

```
                        ┌──────────────┐
                        │   systemd    │
                        │  (manages    │
                        │   process)   │
                        └──────┬───────┘
                               │ starts/stops/restarts
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                        agent-mon                            │
│                                                             │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │           │    │              │    │  Alert Dispatch   │  │
│  │  Scheduler│───▶│  Agent Loop  │───▶│  - stdout        │  │
│  │  (daemon) │    │  (Claude)    │    │  - JSON log      │  │
│  │           │    │              │    │  - Email (Resend) │  │
│  └───────────┘    └──────┬───────┘    └──────────────────┘  │
│                          │                                  │
│                          │ calls tools                      │
│                   ┌──────┴──────┐                           │
│                   │             │                           │
│                   ▼             ▼                           │
│  ┌────────────────────┐ ┌────────────────────────────┐      │
│  │  SDK MCP Server    │ │  Docker MCP Server         │      │
│  │  (in-process)      │ │  (external, stdio)         │      │
│  │                    │ │                            │      │
│  │  -- Monitoring --  │ │  list_containers           │      │
│  │  cpu_info          │ │  inspect_container         │      │
│  │  mem_info          │ │  container_logs            │      │
│  │  disk_info         │ │  start/stop/restart        │      │
│  │  io_info           │ │  container_stats           │      │
│  │  proc_list         │ │  list_images               │      │
│  │  security_check    │ └────────────────────────────┘      │
│  │  system_issues     │                                     │
│  │                    │                                     │
│  │  -- Remediation -- │                                     │
│  │  kill_process      │                                     │
│  │  restart_service   │                                     │
│  │                    │                                     │
│  │  -- Alerting --    │                                     │
│  │  send_alert        │                                     │
│  │  get_alert_history │                                     │
│  └────────────────────┘                                     │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    Config (YAML)                      │   │
│  │  thresholds, alert channels (Resend), remediation     │   │
│  │  rules, check interval, model selection               │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Component Design

### 1. Custom MCP Tools (Monitoring)

All monitoring tools are defined as Python functions using the `@tool` decorator
and served via an in-process SDK MCP server. Each returns structured text that
Claude can reason over.

#### 1a. `get_cpu_info`

Collects CPU metrics using `psutil`.

```
Returns:
  - Per-core usage percentages
  - Overall CPU usage (1s sample)
  - Load averages (1m, 5m, 15m)
  - Top 5 CPU-consuming processes (pid, name, cpu%)
```

#### 1b. `get_memory_info`

Collects memory and swap usage.

```
Returns:
  - Total / used / available / percent for RAM
  - Total / used / percent for swap
  - Top 5 memory-consuming processes (pid, name, rss)
```

#### 1c. `get_disk_info`

Collects disk partition usage.

```
Returns:
  - Per-partition: mountpoint, total, used, free, percent
  - Flags partitions above 85% usage
```

#### 1d. `get_io_info`

Collects disk and network I/O counters.

```
Returns:
  - Disk I/O: read_bytes, write_bytes, read_count, write_count per disk
  - Network I/O: bytes_sent, bytes_recv, packets_sent, packets_recv per NIC
  - Errors and drops per NIC
```

#### 1e. `get_process_list`

Lists running processes with resource usage.

```
Input:  sort_by ("cpu" | "memory"), limit (int, default 20)
Returns:
  - List of processes: pid, name, user, cpu%, memory%, status, create_time
  - Highlights zombie/defunct processes
```

#### 1f. Docker Monitoring — via Docker MCP Server (external)

Docker monitoring is handled by the **official Docker MCP server** (`mcp/docker`)
running as an external stdio-based MCP server. This provides all Docker tools
out of the box:

```
Tools provided by Docker MCP:
  - list_containers    — list all containers with status, health, ports
  - inspect_container  — detailed container metadata
  - container_logs     — tail container logs (useful for diagnosing crashes)
  - container_stats    — live CPU/memory/network stats per container
  - start_container    — start a stopped container
  - stop_container     — stop a running container
  - restart_container  — restart a container (used for remediation)
  - list_images        — list available images

Docker remediation (restart) also goes through this MCP server, gated by
the allow-list in config (enforced in a PreToolUse hook, see Safety section).
```

Configuration in the SDK:
```python
mcp_servers={
    "monitoring": monitoring_server,        # in-process SDK MCP
    "docker": {                             # external Docker MCP
        "command": "docker",
        "args": ["run", "-i", "--rm",
                 "-v", "/var/run/docker.sock:/var/run/docker.sock",
                 "mcp/docker"],
    },
}
```

#### 1g. `get_security_info`

Basic security posture checks.

```
Returns:
  - Failed SSH login attempts (last 50 from /var/log/auth.log)
  - Listening ports and their owning processes
  - Users currently logged in
  - Recent sudo commands
  - Files with world-writable permissions in key directories
```

#### 1h. `get_system_issues`

Checks for common system-level problems.

```
Returns:
  - Uptime
  - Kernel OOM killer events (from dmesg/syslog)
  - Systemd failed units (if systemd is present)
  - Pending package updates (security-critical)
  - NTP sync status
```

### 2. Custom MCP Tools (Remediation)

These are the tools Claude can call to fix detected issues. They are gated by
configuration — the agent can only remediate actions listed in the config file.

#### 2a. `kill_process`

```
Input:  pid (int), signal (str, default "TERM")
Action: Sends signal to process
Returns: Success/failure message
Guard:  Config must list this PID's process name in allowed_kill_targets
```

#### 2b. `restart_container` — via Docker MCP

Container restart is handled by the Docker MCP server's `restart_container` tool.
Access is gated by a **PreToolUse hook** that checks the container name against
`config.remediation.allowed_restart_containers` before allowing execution.

```
Hook logic (PreToolUse on mcp__docker__restart_container):
  1. Extract container name from tool input
  2. Check against allowed_restart_containers in config
  3. Check rate limit (max_restart_attempts per hour)
  4. Return "allow" or "deny" with reason
```

#### 2c. `restart_service`

```
Input:  service_name (str)
Action: systemctl restart <service>
Returns: New service status
Guard:  Config must list service in allowed_restart_services
```

### 3. Custom MCP Tools (Alerting)

#### 3a. `send_alert`

```
Input:  severity ("info" | "warning" | "critical"), title (str), message (str)
Action: Dispatches alert to ALL configured channels:
        1. stdout (always) — colored by severity
        2. JSON log file (always) — appended to alerts.json
        3. Email via Resend (if configured) — for warning and critical only
Returns: Confirmation of delivery per channel
```

**Email via Resend**:
- Uses the Resend REST API (`https://api.resend.com/emails`)
- API key provided via `RESEND_API_KEY` environment variable
- Email is sent via `aiohttp` POST (no extra SDK dependency)
- Subject line includes severity and hostname: `[CRITICAL] agent-mon@prod-1: Disk full`
- Body is plain text with full alert details
- Only `warning` and `critical` alerts trigger email (configurable)
- Rate limit: max 1 email per unique alert title per 15 minutes (dedup)

```python
# Resend API call (inside send_alert tool)
async with aiohttp.ClientSession() as session:
    await session.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {resend_api_key}"},
        json={
            "from": config.alerts.email_from,      # e.g. "agent-mon@yourdomain.com"
            "to": config.alerts.email_to,           # e.g. ["ops@company.com"]
            "subject": f"[{severity.upper()}] agent-mon@{hostname}: {title}",
            "text": message,
        },
    )
```

#### 3b. `get_alert_history`

```
Input:  last_n (int, default 20)
Returns: Recent alerts from the JSON log for context
```

---

## Agent Design

### System Prompt

The agent receives a system prompt that establishes its role and rules:

```
You are a system monitoring agent. Your job:

1. COLLECT: Call the monitoring tools to gather current system state.
2. ANALYZE: Look for anomalies, correlations, and issues across all metrics.
3. ALERT: Call send_alert for every issue found, with appropriate severity:
   - critical: Service down, disk full (>95%), OOM, security breach
   - warning:  High resource usage (>80%), unhealthy containers, failed units
   - info:     Notable but non-urgent (approaching thresholds, high load)
4. REMEDIATE: For issues matching the remediation policy, take action:
   - Restart containers that are exited/unhealthy (if in allowed list)
   - Kill processes consuming >90% CPU for extended periods (if in allowed list)
   - Restart failed systemd services (if in allowed list)
   After remediation, re-check and confirm the fix worked.
5. SUMMARIZE: End with a brief status summary.

Rules:
- Always alert BEFORE attempting remediation.
- Never remediate anything not in the allowed lists.
- If unsure, alert as warning and suggest manual intervention.
- Reference specific PIDs, container names, and metrics in alerts.
```

### Agent Loop Flow

```
  ┌─────────────────┐
  │  Scheduler tick  │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Send prompt to │     "Run a full system health check."
  │  Claude via SDK  │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ Claude calls     │     get_cpu_info, get_memory_info, get_disk_info,
  │ monitoring tools │     get_io_info, get_process_list, get_docker_containers,
  │                  │     get_security_info, get_system_issues
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ Claude analyzes  │     Correlates signals, identifies issues
  │ all results      │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ Claude calls     │     send_alert(severity, title, details)
  │ send_alert for   │     for each issue found
  │ each issue       │
  └────────┬────────┘
           │
           ▼
  ┌────────────────────┐
  │ If remediable:     │   restart_container("nginx")
  │ Claude calls       │   kill_process(12345)
  │ remediation tool   │
  └────────┬───────────┘
           │
           ▼
  ┌─────────────────┐
  │ Claude re-checks │     Calls monitoring tool again to verify fix
  │ after remediation│
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │ Claude returns   │     "System check complete. 2 issues found..."
  │ summary          │
  └─────────────────┘
```

### SDK Configuration

```python
options = ClaudeAgentOptions(
    model="haiku",                    # Cost-efficient for routine checks
    max_turns=15,                     # Cap per check cycle
    permission_mode="bypassPermissions",  # Unattended operation
    system_prompt=SYSTEM_PROMPT,
    mcp_servers={
        # In-process: system monitoring + alerting + remediation
        "monitoring": monitoring_server,
        # External: Docker MCP server (containers, images, logs)
        "docker": {
            "command": "docker",
            "args": ["run", "-i", "--rm",
                     "-v", "/var/run/docker.sock:/var/run/docker.sock",
                     "mcp/docker"],
        },
    },
    allowed_tools=[
        # System monitoring (in-process)
        "mcp__monitoring__get_cpu_info",
        "mcp__monitoring__get_memory_info",
        "mcp__monitoring__get_disk_info",
        "mcp__monitoring__get_io_info",
        "mcp__monitoring__get_process_list",
        "mcp__monitoring__get_security_info",
        "mcp__monitoring__get_system_issues",
        # Alerting (in-process)
        "mcp__monitoring__send_alert",
        "mcp__monitoring__get_alert_history",
        # Remediation — process/service (in-process)
        "mcp__monitoring__kill_process",
        "mcp__monitoring__restart_service",
        # Docker (external MCP)
        "mcp__docker__list_containers",
        "mcp__docker__inspect_container",
        "mcp__docker__container_logs",
        "mcp__docker__container_stats",
        "mcp__docker__restart_container",
        "mcp__docker__start_container",
        "mcp__docker__stop_container",
        "mcp__docker__list_images",
    ],
    hooks={
        # Gate Docker remediation via allow-list
        "PreToolUse": [
            HookMatcher(
                matcher="mcp__docker__restart_container|mcp__docker__stop_container",
                hooks=[docker_remediation_guard],
            )
        ],
    },
)
```

---

## Configuration File (`config.yaml`)

```yaml
# How often to run a full check (seconds)
check_interval: 300

# Model to use for analysis
model: haiku

# Maximum agent turns per check cycle
max_turns: 15

# Thresholds (inform the system prompt)
thresholds:
  cpu_warning: 80
  cpu_critical: 95
  memory_warning: 80
  memory_critical: 95
  disk_warning: 85
  disk_critical: 95
  swap_warning: 50

# Alert channels
alerts:
  stdout: true
  log_file: /var/log/agent-mon/alerts.json

  # Email alerts via Resend
  email:
    enabled: true
    from: "agent-mon@yourdomain.com"
    to:
      - "ops@company.com"
    min_severity: warning           # Only email for warning and critical
    dedup_window_minutes: 15        # Suppress duplicate emails within this window
  # RESEND_API_KEY must be set as environment variable

# Docker MCP server
docker:
  enabled: true
  # The Docker MCP server runs as an external stdio process

# Remediation policy — only these targets can be auto-remediated
remediation:
  enabled: true
  allowed_restart_containers:
    - nginx
    - redis
    - postgres
  allowed_restart_services:
    - nginx
    - docker
  allowed_kill_targets:           # Process names that can be killed
    - defunct_worker
  max_restart_attempts: 3         # Per container/service per hour
```

### Environment Variables

```bash
ANTHROPIC_API_KEY=sk-ant-...       # Required: Claude API key
RESEND_API_KEY=re_...              # Required for email alerts
```

---

## File Structure

```
agent-mon/
├── pyproject.toml
├── config.yaml                       # Default configuration
├── agent-mon.service                  # Systemd unit file
├── agent_mon/
│   ├── __init__.py
│   ├── cli.py                        # Entry point & argument parsing
│   ├── agent.py                      # Agent loop, scheduler, SDK integration
│   ├── config.py                     # Config loading & validation
│   ├── tools/
│   │   ├── __init__.py               # Creates SDK MCP server, registers all tools
│   │   ├── cpu.py                    # get_cpu_info
│   │   ├── memory.py                 # get_memory_info
│   │   ├── disk.py                   # get_disk_info
│   │   ├── io.py                     # get_io_info
│   │   ├── processes.py              # get_process_list
│   │   ├── security.py               # get_security_info
│   │   ├── system.py                 # get_system_issues
│   │   ├── remediation.py            # kill_process, restart_service
│   │   └── alerts.py                 # send_alert (stdout + log + Resend email),
│   │                                 # get_alert_history
│   └── prompt.py                     # System prompt template builder
└── README.md
```

Note: Docker monitoring and container remediation are handled by the external
Docker MCP server (`mcp/docker`), so no `docker.py` tool file is needed.

---

## Systemd Service

agent-mon runs as a systemd service. This provides automatic start on boot,
restart on crash, structured logging via journald, and standard service
management (`systemctl start/stop/restart/status`).

### Unit File (`agent-mon.service`)

```ini
[Unit]
Description=agent-mon - AI-powered system monitoring agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/agent-mon --config /etc/agent-mon/config.yaml
Restart=on-failure
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=300

# Environment
EnvironmentFile=/etc/agent-mon/env
# Contains:
#   ANTHROPIC_API_KEY=sk-ant-...
#   RESEND_API_KEY=re_...

# Security hardening
User=agent-mon
Group=agent-mon
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/agent-mon
PrivateTmp=true

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=agent-mon

[Install]
WantedBy=multi-user.target
```

### Installation

```bash
# Install the package
pip install .

# Create service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin agent-mon
sudo usermod -aG docker agent-mon   # For Docker MCP access

# Create directories
sudo mkdir -p /etc/agent-mon /var/log/agent-mon
sudo chown agent-mon:agent-mon /var/log/agent-mon

# Copy config and environment
sudo cp config.yaml /etc/agent-mon/config.yaml
sudo cp agent-mon.env /etc/agent-mon/env
sudo chmod 600 /etc/agent-mon/env   # Protect API keys

# Install and enable service
sudo cp agent-mon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-mon
```

### Management

```bash
sudo systemctl start agent-mon       # Start
sudo systemctl stop agent-mon        # Stop
sudo systemctl restart agent-mon     # Restart
sudo systemctl status agent-mon      # Status
sudo journalctl -u agent-mon -f      # Live logs
sudo journalctl -u agent-mon --since "1 hour ago"  # Recent logs
```

### CLI Modes (outside of systemd)

The CLI also supports direct invocation for debugging and one-off checks:

```bash
# One-shot mode: run a single check and exit
agent-mon --once --config config.yaml

# Interactive mode: ask the agent questions
agent-mon --interactive --config config.yaml
```

---

## Safety & Guards

1. **Remediation allow-lists**: In-process tools (`kill_process`, `restart_service`)
   refuse to act on targets not in config. The LLM cannot bypass this — the Python
   tool function checks the config before executing.

2. **Docker MCP gating via PreToolUse hook**: Since the Docker MCP server doesn't
   know about our allow-lists, a `PreToolUse` hook intercepts
   `mcp__docker__restart_container` and `mcp__docker__stop_container` calls. The
   hook extracts the container name, checks it against
   `config.remediation.allowed_restart_containers`, and returns `deny` if not
   allowed. This runs in Python before the tool executes — the LLM cannot bypass it.

3. **Rate limiting**: Remediation tools track restart counts per hour. If
   `max_restart_attempts` is exceeded, the tool/hook refuses and alerts instead.

4. **max_turns cap**: Each check cycle is limited to prevent runaway token usage.

5. **Model selection**: Uses `haiku` by default for cost efficiency. Can be
   switched to `sonnet` or `opus` for deeper analysis on demand.

6. **No destructive built-in tools**: The agent has NO access to `Bash`, `Write`,
   `Edit`, or other file-manipulation tools. It can only use the custom MCP tools
   and the Docker MCP server.

7. **Audit log**: Every alert and remediation action is logged to the JSON log
   file with timestamps, for post-incident review.

8. **Systemd hardening**: The service runs as a dedicated `agent-mon` user with
   `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=true`, and restricted
   write paths. Only `/var/log/agent-mon` is writable.

9. **Email dedup**: Resend emails are deduplicated per alert title within a
   configurable window (default 15 min) to prevent inbox flooding.

---

## Cost Considerations

| Component | Estimate per check | Notes |
|-----------|-------------------|-------|
| Input tokens | ~2-4K | System prompt + tool results |
| Output tokens | ~500-1K | Analysis + tool calls |
| Haiku cost | ~$0.001-0.003 | Per check cycle |
| Daily (5min interval) | ~$0.30-0.85 | 288 checks/day |
| Daily (1min interval) | ~$1.50-4.30 | 1440 checks/day |

Session reuse (`resume`) can reduce input tokens on subsequent checks since
Claude retains context about the system's baseline.

---

## Dependencies

```
# pyproject.toml [project.dependencies]
claude-agent-sdk           # Agent SDK
psutil                     # CPU, memory, disk, I/O, process metrics
aiohttp                    # Async HTTP for Resend API
pyyaml                     # Config file parsing
```

No Docker Python SDK needed — Docker is handled by the external MCP server.

---

## Future Extensions (Not in v0.1)

- **Multi-host**: Agent queries remote hosts via SSH MCP tools
- **Metrics history**: Store metrics in SQLite for trend analysis
- **Dashboard**: Simple web UI showing current status and alert history
- **Slack/PagerDuty**: Additional alert channels beyond email
- **Custom checks**: User-defined check scripts loaded as additional tools
