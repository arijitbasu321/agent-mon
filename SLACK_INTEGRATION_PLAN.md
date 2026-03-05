# Slack Integration Plan for agent-mon

## Level 1: Slack Alerts

Send monitoring alerts to a Slack channel (alongside or instead of email).

### Prerequisites

- A Slack workspace you control
- Permission to create a Slack App (or workspace admin access)

### Slack App Setup

1. Go to https://api.slack.com/apps and click "Create New App" > "From scratch"
2. Name it `agent-mon`, select your workspace
3. Go to **Incoming Webhooks** > toggle ON
4. Click "Add New Webhook to Workspace", select the target channel
5. Copy the webhook URL (looks like `https://hooks.slack.com/services/T.../B.../xxx`)
6. Store it as `SLACK_WEBHOOK_URL` in your `.env` file on the server

### Code Changes

#### 1. Config: `agent_mon/config.py`

Add a `SlackConfig` dataclass:

```python
@dataclass
class SlackConfig:
    enabled: bool = False
    webhook_url_env: str = "SLACK_WEBHOOK_URL"  # env var name, not the actual URL
    channel: str = ""                            # override channel (optional)
    min_severity: str = "warning"
    dedup_window_minutes: int = 15
```

Add `slack: SlackConfig` field to `AlertsConfig`. Add `_parse_slack()` to `Config`.

#### 2. Alert Dispatch: `agent_mon/tools/alerts.py`

Add a Slack dispatch path in `AlertManager.send_alert()`, after the email block:

```python
# 3. Slack webhook
slack_config = self.config.alerts.slack
if (
    slack_config.enabled
    and self.http_session is not None
    and SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(slack_config.min_severity, 1)
):
    if self._should_send_slack(original_title):
        payload = {
            "text": f"*[{severity.upper()}]* `{self.hostname}`: {title}\n{message}"
        }
        if slack_config.channel:
            payload["channel"] = slack_config.channel
        try:
            resp = await self.http_session.post(
                self._get_slack_webhook_url(),
                json=payload,
            )
            if resp.status < 300:
                results.append("slack: sent")
            else:
                results.append(f"slack: failed (HTTP {resp.status})")
        except (aiohttp.ClientError, Exception) as exc:
            results.append(f"slack: failed ({exc})")
    else:
        results.append("slack: deduplicated")
```

Add `_slack_dedup` dict and `_should_send_slack()` (same pattern as email dedup).
Add `_get_slack_webhook_url()` that reads from `os.environ`.

#### 3. Config YAML

```yaml
alerts:
  log_file: /opt/agent-mon/agent-mon-alerts.log
  email:
    enabled: true
    # ... existing email config ...
  slack:
    enabled: true
    min_severity: warning
    dedup_window_minutes: 15
```

#### 4. Env Validation

Add `SLACK_WEBHOOK_URL` check in `validate_env()` when `alerts.slack.enabled` is true.

#### 5. Message Formatting

Use Slack's mrkdwn format for better readability:
- Bold severity: `*[CRITICAL]*`
- Code for hostname: `` `server01` ``
- Separate title and body with newline
- Optional: use Block Kit JSON for richer layouts (colored sidebar, fields)

#### 6. Tests

- `TestSlackAlertDispatch`: mock `aiohttp.ClientSession.post`, verify payload shape
- `TestSlackDedup`: verify dedup window works
- `TestSlackConfig`: parsing, validation, missing env var
- `TestSlackSecretSanitization`: secrets redacted before Slack post

### Files Changed

| File | Change |
|------|--------|
| `agent_mon/config.py` | Add `SlackConfig`, parse, validate |
| `agent_mon/tools/alerts.py` | Add Slack dispatch + dedup |
| `tests/tools/test_alerts.py` | Slack alert tests |
| `tests/test_config.py` | Slack config parsing tests |

### New Dependencies

None. Uses existing `aiohttp` for the webhook POST.

### Deployment

1. Add `SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...` to `/opt/agent-mon/.env`
2. Add `slack:` block to `/opt/agent-mon/config.yaml`
3. `systemctl restart agent-mon`

---

## Level 2: Interactive Slack Bot

Let users ask the agent questions, trigger investigations, and receive threaded responses in Slack.

### Prerequisites

Everything from Level 1, plus:

- The Slack App needs additional scopes and Socket Mode or an HTTP endpoint

### Slack App Setup (Extended)

1. In your existing `agent-mon` Slack App:
2. Go to **OAuth & Permissions** > add Bot Token Scopes:
   - `chat:write` -- post messages
   - `app_mentions:read` -- respond to @agent-mon mentions
   - `channels:history` -- read channel messages (for context)
   - `im:history`, `im:read`, `im:write` -- DM support
3. Go to **Socket Mode** > enable it (no public URL needed, connects via WebSocket)
   - Create an App-Level Token with `connections:write` scope
   - Save this as `SLACK_APP_TOKEN` in `.env`
4. Go to **Event Subscriptions** > subscribe to:
   - `app_mention` (someone @mentions the bot in a channel)
   - `message.im` (someone DMs the bot)
5. Install the app to your workspace
6. Save the Bot Token (`xoxb-...`) as `SLACK_BOT_TOKEN` in `.env`

### Architecture

```
Slack (events via Socket Mode)
    |
    v
SlackBot (agent_mon/slack_bot.py)
    |
    +--> parse mention/DM
    +--> create one-shot agent cycle (like run_once)
    +--> post response back to Slack thread
    |
    v
AgentDaemon._run_check_cycle() or new _run_query()
```

The daemon loop and the Slack bot run concurrently:
- Daemon loop: periodic monitoring (existing behavior)
- Slack bot: on-demand queries from users

### Code Changes

#### 1. New Dependency: `slack-bolt`

```toml
# pyproject.toml
dependencies = [
    # ... existing ...
    "slack-bolt>=1.18.0",
]
```

`slack-bolt` is Slack's official async bot framework. It handles Socket Mode, event parsing, retries, and ack/respond patterns.

#### 2. New Module: `agent_mon/slack_bot.py`

```python
"""Slack bot integration for interactive agent queries."""

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

class AgentSlackBot:
    def __init__(self, config: Config, daemon: AgentDaemon):
        self.config = config
        self.daemon = daemon
        self.app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

        # Register event handlers
        self.app.event("app_mention")(self.handle_mention)
        self.app.event("message")(self.handle_dm)

    async def handle_mention(self, event, say):
        """Handle @agent-mon mentions in channels."""
        user_query = self._extract_query(event["text"])
        thread_ts = event.get("thread_ts", event["ts"])

        # Acknowledge quickly
        await say("Investigating...", thread_ts=thread_ts)

        # Run a one-shot agent query
        result = await self.daemon.run_query(user_query)

        # Post result as threaded reply
        await say(result, thread_ts=thread_ts)

    async def handle_dm(self, event, say):
        """Handle direct messages to the bot."""
        # Skip bot's own messages
        if event.get("bot_id"):
            return
        result = await self.daemon.run_query(event["text"])
        await say(result)

    async def start(self):
        handler = AsyncSocketModeHandler(self.app, os.environ["SLACK_APP_TOKEN"])
        await handler.start_async()
```

#### 3. New Method: `AgentDaemon.run_query()`

Add a method that runs a single agent cycle with a user-provided question instead of the monitoring prompt:

```python
async def run_query(self, question: str) -> str:
    """Run a one-shot agent query from an external trigger (e.g. Slack)."""
    await self._initialize()

    prompt = f"""You are a system monitoring assistant. A user has asked:

{question}

Use your monitoring tools to investigate and provide a concise answer.
Previous cycle context:
{self.memory_store.get_last_cycle_summary() if self.config.memory.enabled else 'N/A'}
"""
    # Run with orchestrator tools, collect text response
    response_text = await self._run_agent_query(prompt)
    return response_text
```

#### 4. CLI: `agent_mon/cli.py`

Add `--slack` mode:

```python
mode.add_argument(
    "--slack",
    action="store_true",
    default=False,
    help="Run with Slack bot integration (daemon + interactive)",
)
```

In `main()`:

```python
elif args.slack:
    async def run_with_slack():
        bot = AgentSlackBot(config, daemon)
        await asyncio.gather(
            daemon.run(),        # monitoring loop
            bot.start(),         # slack listener
        )
    asyncio.run(run_with_slack())
```

#### 5. Config: `agent_mon/config.py`

Add Slack bot config:

```python
@dataclass
class SlackBotConfig:
    enabled: bool = False
    allowed_user_ids: list[str] = field(default_factory=list)  # empty = allow all
    allowed_commands: list[str] = field(default_factory=lambda: [
        "status", "investigate", "query", "alerts"
    ])
    response_max_length: int = 3000  # Slack message limit ~4000 chars
```

#### 6. Permission Model

Control who can do what from Slack:

| Action | Default | Config Key |
|--------|---------|------------|
| Ask status questions | All users | `allowed_user_ids` (empty = all) |
| Trigger investigation | All users | `allowed_user_ids` |
| Trigger remediation | Explicit allowlist only | `remediation_user_ids` |
| View alert history | All users | -- |

Check `event["user"]` against the allowlist before executing.

#### 7. Command Patterns

Support structured commands via mentions:

- `@agent-mon status` -- current system health summary
- `@agent-mon investigate <service>` -- run investigation on a specific service
- `@agent-mon alerts` -- show recent alert history
- `@agent-mon <free text>` -- ask the agent anything

#### 8. Response Formatting

- Use Slack Block Kit for structured responses
- Wrap code/logs in triple backtick blocks
- Truncate long responses with "Full details logged to file"
- Use thread replies for multi-step investigations

#### 9. Tests

- `tests/test_slack_bot.py`:
  - `TestSlackBotEventHandling`: mock Slack events, verify `run_query` called
  - `TestSlackBotPermissions`: verify user allowlist enforcement
  - `TestSlackBotResponseFormatting`: verify truncation, mrkdwn
  - `TestSlackBotCommandParsing`: verify command extraction from mention text
- Mock `slack-bolt` app and `AsyncSocketModeHandler`

### Files Changed

| File | Change |
|------|--------|
| `pyproject.toml` | Add `slack-bolt` dependency |
| `agent_mon/config.py` | Add `SlackBotConfig`, parse, validate |
| `agent_mon/slack_bot.py` | New module: bot event handlers |
| `agent_mon/agent.py` | Add `run_query()` method |
| `agent_mon/cli.py` | Add `--slack` mode |
| `tests/test_slack_bot.py` | Bot tests |
| `tests/test_config.py` | SlackBot config parsing tests |
| `tests/test_agent.py` | `run_query()` tests |

### New Dependencies

| Package | Purpose |
|---------|---------|
| `slack-bolt>=1.18.0` | Slack bot framework (async, Socket Mode) |

### Deployment

1. Complete Slack App setup (scopes, Socket Mode, event subscriptions)
2. Add to `/opt/agent-mon/.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```
3. Add `slack_bot:` block to `/opt/agent-mon/config.yaml`
4. Update systemd `ExecStart` to use `--slack` flag
5. `pip install slack-bolt` (or `uv sync` after pyproject.toml update)
6. `systemctl restart agent-mon`

---

## Implementation Order

1. **L1 first**: Slack webhook alerts (~100 lines of code, no new deps)
2. **Test L1 on server**: verify alerts land in Slack
3. **L2 second**: Interactive bot (new module, new dep, new CLI mode)
4. **Test L2 locally**: mock Slack events in tests
5. **Deploy L2**: Slack App setup + server config + systemd update
