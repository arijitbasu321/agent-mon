# agent-mon v2: Bash-First Agent with Memory

## Overview

**agent-mon** is a system monitoring agent powered by the Anthropic Agent SDK. It
gives Claude **bash access** and **vector memory**, letting the LLM decide how to
investigate system health and learn from past cycles. Custom code is only written
for things the agent can't do itself: alerting, safety guardrails, memory
persistence, and the daemon loop.

---

## Architecture

```
systemd -> agent-mon daemon
  |-- Scheduler (configurable interval)
  |-- Agent Loop (Claude Agent SDK)
  |     |-- Bash tool (with deny-list hook)
  |     |-- Docker MCP (external, container management)
  |     |-- send_alert / get_alert_history (in-process)
  |     |-- store_memory / query_memory (in-process, ChromaDB)
  |     `-- System prompt (what to monitor + past context from memory)
  |-- Circuit Breaker (fallback to degraded mode if API fails)
  |-- Heartbeat (periodic email)
  `-- Config (YAML)
```

**Tool set (6 custom + bash + docker):**
- `Bash` -- via SDK, with deny-list PreToolUse hook
- Docker tools -- via external MCP server (list, inspect, logs, stats, start/stop/restart)
- `send_alert(severity, title, message)` -- email via Resend + append to log file
- `get_alert_history(last_n)` -- read recent alerts from log file
- `store_memory(observation, action, outcome)` -- persist to ChromaDB
- `query_memory(query, n_results)` -- semantic search over past observations

---

## Component Design

### 1. Bash Tool

The agent has direct bash access via the SDK's built-in Bash tool. A PreToolUse
hook (`bash_denylist_guard`) blocks dangerous commands using case-insensitive
substring matching against a configurable deny-list.

### 2. Memory System (ChromaDB)

**`MemoryStore` class in `agent_mon/memory.py`:**
- `PersistentClient` -- in-process, persists to disk, no server needed
- One collection: `agent_mon_memory` (cosine similarity)
- Default embedding: `all-MiniLM-L6-v2` (ships with ChromaDB, runs locally)
- Document format: `"{observation} | Action: {action} | Outcome: {outcome}"`
- Metadata: observation, action, outcome, timestamp (ISO 8601), cycle_id
- `store(observation, action, outcome)` -- returns entry ID
- `query(query_text, n_results)` -- returns formatted text of relevant past entries
- `initialize()` -- called once at daemon startup, creates dir + collection
- At cycle start: query memory for "recent system health issues" and inject into prompt

### 3. Alert Tools

#### `send_alert`
```
Input:  severity ("info" | "warning" | "critical"), title (str), message (str)
Action: Dispatches alert to:
        1. Plain text log file (always) -- appended to alerts log
        2. Email via Resend (if configured) -- for warning and critical only
Returns: Confirmation of delivery per channel
```

#### `get_alert_history`
```
Input:  last_n (int, default 20)
Returns: Recent alerts from the log file for context
```

### 4. Docker Tools (External MCP)

Docker monitoring and remediation is handled by the official Docker MCP server
(`mcp/docker`) running as an external stdio-based MCP server.

### 5. Safety Hooks

1. **Bash deny-list** -- substring match (case-insensitive) against configurable
   deny_list. Blocks dangerous commands before execution.
2. **Docker remediation guard** -- allow-list + per-container rate limiting
   (max N restarts/hour).
3. **max_turns cap** -- limits agent tool calls per cycle (default 25).

---

## Prompt Design

6-step workflow:
1. **INVESTIGATE** -- use bash (ps, top, df, free, journalctl, ss, systemctl, etc.)
2. **DIAGNOSE** -- correlate signals, think about root causes
3. **ALERT** -- call send_alert for every issue (critical/warning/info)
4. **REMEDIATE** -- fix issues via bash (systemctl restart) or Docker tools
5. **REMEMBER** -- call store_memory with what was found and done
6. **SUMMARIZE** -- brief status summary

Dynamic sections: watched processes/containers, remediation allow-lists, memory
context from past cycles. No thresholds -- the agent uses judgment.

---

## Agent Loop (`_run_check_cycle`)

1. Query memory for relevant past context
2. Build system prompt with memory context injected
3. Create tool list (alerts + memory tools)
4. Check circuit breaker -- if OPEN, run degraded_check instead
5. Run agent via `ClaudeSDKClient` with prompt "Run a full system health check."
6. On success: `circuit_breaker.record_success()`
7. On failure: `circuit_breaker.record_failure()`, run degraded_check if circuit just opened

---

## Degraded Mode

When circuit breaker is OPEN (API unreachable):
- Runs `df -h` -- alerts if any partition >95%
- Runs `free -m` -- alerts if memory >95%
- Runs `uptime` -- alerts if load average per CPU >2.0
- Sends meta-alert: "agent-mon degraded: Anthropic API unreachable"
- Uses subprocess (not psutil) -- consistent with bash-first philosophy

---

## Circuit Breaker

```python
class CircuitBreaker:
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # API failed, degraded mode
    HALF_OPEN = "half_open"  # Testing if API is back

    failure_threshold = 3   # consecutive failures to open
    recovery_timeout = 300  # seconds before retrying
```

---

## Configuration

```yaml
check_interval: 300
model: claude-sonnet-4-6
max_turns: 25

heartbeat:
  enabled: true
  interval: 3600

watched_processes:
  - name: my-api-server
    restart_command: "systemctl restart my-api-server"

watched_containers:
  - nginx
  - redis

alerts:
  log_file: /var/log/agent-mon.log
  email:
    enabled: true
    from: "agent-mon@yourdomain.com"
    to: ["ops@company.com"]
    min_severity: warning
    dedup_window_minutes: 15

docker:
  enabled: true

remediation:
  enabled: true
  allowed_restart_containers: [nginx, redis]
  allowed_restart_services: [nginx, docker]
  max_restart_attempts: 3

bash:
  deny_list:
    - "rm -rf /"
    - "shutdown"
    - "reboot"
    - "mkfs"
    # ... (see config.yaml for full list)

memory:
  enabled: true
  path: /var/lib/agent-mon/memory
  collection_name: agent_mon_memory
  max_results: 5
```

---

## File Structure

```
agent-mon/
├── pyproject.toml
├── config.yaml                       # Default configuration
├── install.sh                        # Installer script
├── agent-mon.service                 # Systemd unit file
├── DESIGN.md                         # This file
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
    ├── conftest.py
    ├── test_agent.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_hooks.py
    ├── test_memory.py
    ├── test_prompt.py
    └── tools/
        └── test_alerts.py
```

---

## Dependencies

```
claude-agent-sdk>=0.1.0    # Agent SDK
chromadb>=1.0.0            # Vector memory (in-process, no server)
aiohttp>=3.9.0             # Async HTTP for Resend API
pyyaml>=6.0                # Config file parsing
```

No psutil -- bash-first philosophy. No Docker Python SDK -- external MCP server.
