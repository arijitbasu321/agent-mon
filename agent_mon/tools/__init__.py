"""SDK MCP server setup and tool registration.

Creates MCP tool servers for orchestrator and investigator agents
using the Claude Agent SDK's @tool decorator and create_sdk_mcp_server().
"""

from __future__ import annotations

from claude_agent_sdk import tool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from agent_mon.config import Config
from agent_mon.memory import MemoryStore
from agent_mon.tools.alerts import AlertManager, sanitize_secrets


def _text_result(text: str) -> dict:
    """Helper to build an MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def create_orchestrator_tools(
    config: Config,
    alert_manager: AlertManager,
    memory_store: MemoryStore | None = None,
) -> McpSdkServerConfig:
    """Create the MCP server for the monitoring agent.

    Tools: send_alert, get_alert_history, store_memory, query_memory.
    """
    sdk_tools = []

    # --- Alert tools ---
    @tool(
        "send_alert",
        "Send an alert with the given severity, title, and message.",
        {"severity": str, "title": str, "message": str},
    )
    async def send_alert(args):
        result = await alert_manager.send_alert(
            args["severity"], args["title"], args["message"],
        )
        return _text_result(result)

    sdk_tools.append(send_alert)

    @tool(
        "get_alert_history",
        "Get recent alert history from the log file.",
        {"last_n": int},
    )
    async def get_alert_history(args):
        result = alert_manager.get_alert_history(args.get("last_n", 20))
        return _text_result(result)

    sdk_tools.append(get_alert_history)

    # --- Memory store tool (H1: sanitize secrets before storing) ---
    if memory_store is not None and config.memory.enabled:
        @tool(
            "store_memory",
            "Store an observation/action/outcome in persistent memory.",
            {"observation": str, "action": str, "outcome": str},
        )
        async def store_memory(args):
            observation = sanitize_secrets(args["observation"])
            action = sanitize_secrets(args["action"])
            outcome = sanitize_secrets(args["outcome"])
            entry_id = memory_store.store(observation, action, outcome)
            return _text_result(f"Memory stored (id: {entry_id})")

        sdk_tools.append(store_memory)

    # --- Query memory tool ---
    if memory_store is not None and config.memory.enabled:
        @tool(
            "query_memory",
            "Search past observations by semantic similarity.",
            {"query": str, "n_results": int},
        )
        async def query_memory(args):
            result = memory_store.query(
                args["query"], args.get("n_results", 5),
            )
            return _text_result(result)

        sdk_tools.append(query_memory)

    return create_sdk_mcp_server(
        name="agent-mon", tools=sdk_tools,
    )


def create_monitoring_tools(
    config: Config,
    alert_manager: AlertManager,
    memory_store: MemoryStore | None = None,
) -> McpSdkServerConfig:
    """Backward-compatible factory for interactive mode.

    Returns the full orchestrator tool set (without investigate_issue).
    """
    return create_orchestrator_tools(
        config, alert_manager, memory_store,
    )
