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
┌─────────────────────────────────────────────────────────────┐
│                        agent-mon                            │
│                                                             │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │           │    │              │    │                  │  │
│  │  Scheduler│───▶│  Agent Loop  │───▶│  Alert Dispatch  │  │
│  │  (daemon) │    │  (Claude)    │    │  (stdout/file/   │  │
│  │           │    │              │    │   webhook)       │  │
│  └───────────┘    └──────┬───────┘    └──────────────────┘  │
│                          │                                  │
│                          │ calls tools                      │
│                          ▼                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              MCP Tool Server (in-process)            │   │
│  │                                                      │   │
│  │  ┌──────────┐ ┌──────────┐ ┌───────────┐            │   │
│  │  │ cpu_info │ │ mem_info │ │ disk_info │            │   │
│  │  └──────────┘ └──────────┘ └───────────┘            │   │
│  │  ┌──────────┐ ┌──────────┐ ┌───────────┐            │   │
│  │  │  io_info │ │proc_list │ │docker_list│            │   │
│  │  └──────────┘ └──────────┘ └───────────┘            │   │
│  │  ┌───────────────┐ ┌────────────────┐                │   │
│  │  │ security_check│ │ system_issues  │                │   │
│  │  └───────────────┘ └────────────────┘                │   │
│  │                                                      │   │
│  │  ── Remediation Tools ──                             │   │
│  │  ┌──────────────┐ ┌─────────────────┐                │   │
│  │  │ kill_process │ │restart_container│                │   │
│  │  └──────────────┘ └─────────────────┘                │   │
│  │  ┌───────────────┐                                   │   │
│  │  │restart_service│                                   │   │
│  │  └───────────────┘                                   │   │
│  │                                                      │   │
│  │  ── Alert Tools ──                                   │   │
│  │  ┌──────────┐ ┌──────────────┐                       │   │
│  │  │send_alert│ │get_alert_log │                       │   │
│  │  └──────────┘ └──────────────┘                       │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    Config (YAML)                      │   │
│  │  thresholds, alert channels, remediation rules,       │   │
│  │  check interval, model selection                      │   │
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

#### 1f. `get_docker_containers`

Lists Docker containers and their status. Uses the `docker` Python SDK.

```
Returns:
  - Per container: id, name, image, status, health, ports, restart_count
  - Flags containers in "exited" or "unhealthy" state
  - Container resource stats (cpu%, mem%) for running containers
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

#### 2b. `restart_container`

```
Input:  container_id_or_name (str)
Action: docker restart <container>
Returns: New container status after restart
Guard:  Config must list container name in allowed_restart_containers
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
Action: Dispatches alert to configured channels:
        - stdout (always)
        - JSON log file (always)
        - Webhook URL (if configured)
Returns: Confirmation of delivery
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
    mcp_servers={"monitoring": monitoring_server},
    allowed_tools=[
        # Monitoring
        "mcp__monitoring__get_cpu_info",
        "mcp__monitoring__get_memory_info",
        "mcp__monitoring__get_disk_info",
        "mcp__monitoring__get_io_info",
        "mcp__monitoring__get_process_list",
        "mcp__monitoring__get_docker_containers",
        "mcp__monitoring__get_security_info",
        "mcp__monitoring__get_system_issues",
        # Alerting
        "mcp__monitoring__send_alert",
        "mcp__monitoring__get_alert_history",
        # Remediation
        "mcp__monitoring__kill_process",
        "mcp__monitoring__restart_container",
        "mcp__monitoring__restart_service",
    ],
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
  log_file: /var/log/agent-mon/alerts.json
  webhook_url: null                          # Optional: Slack/PagerDuty webhook
  stdout: true

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

---

## File Structure

```
agent-mon/
├── pyproject.toml
├── config.yaml                     # Default configuration
├── agent_mon/
│   ├── __init__.py
│   ├── cli.py                      # Entry point & argument parsing
│   ├── agent.py                    # Agent loop, scheduler, SDK integration
│   ├── config.py                   # Config loading & validation
│   ├── tools/
│   │   ├── __init__.py             # Creates the MCP server, registers all tools
│   │   ├── cpu.py                  # get_cpu_info
│   │   ├── memory.py               # get_memory_info
│   │   ├── disk.py                 # get_disk_info
│   │   ├── io.py                   # get_io_info
│   │   ├── processes.py            # get_process_list
│   │   ├── docker.py               # get_docker_containers
│   │   ├── security.py             # get_security_info
│   │   ├── system.py               # get_system_issues
│   │   ├── remediation.py          # kill_process, restart_container, restart_service
│   │   └── alerts.py               # send_alert, get_alert_history
│   └── prompt.py                   # System prompt template builder
└── README.md
```

---

## Execution Modes

### 1. Daemon Mode (default)

Runs continuously, executing checks at `check_interval`.

```bash
agent-mon --config config.yaml
```

### 2. One-Shot Mode

Runs a single check and exits. Useful for cron jobs or CI.

```bash
agent-mon --once --config config.yaml
```

### 3. Interactive Mode

Opens an interactive session where you can ask the agent questions about
system health or request specific checks.

```bash
agent-mon --interactive --config config.yaml
```

---

## Safety & Guards

1. **Remediation allow-lists**: Tools refuse to act on targets not in config.
   The LLM cannot bypass this — the Python tool function checks the config
   before executing.

2. **Rate limiting**: Remediation tools track restart counts per hour. If
   `max_restart_attempts` is exceeded, the tool refuses and alerts instead.

3. **max_turns cap**: Each check cycle is limited to prevent runaway token usage.

4. **Model selection**: Uses `haiku` by default for cost efficiency. Can be
   switched to `sonnet` or `opus` for deeper analysis on demand.

5. **No destructive built-in tools**: The agent has NO access to `Bash`, `Write`,
   `Edit`, or other file-manipulation tools. It can only use the custom MCP tools
   defined above.

6. **Audit log**: Every alert and remediation action is logged to the JSON log
   file with timestamps, for post-incident review.

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

## Future Extensions (Not in v0.1)

- **Multi-host**: Agent queries remote hosts via SSH MCP tools
- **Metrics history**: Store metrics in SQLite for trend analysis
- **Dashboard**: Simple web UI showing current status and alert history
- **Escalation**: If remediation fails, page on-call via PagerDuty
- **Custom checks**: User-defined check scripts loaded as additional tools
