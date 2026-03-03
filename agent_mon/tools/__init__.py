"""MCP server setup and tool registration.

Creates an in-process MCP server with alert and memory tools.
"""

from __future__ import annotations

from agent_mon.config import Config
from agent_mon.memory import MemoryStore
from agent_mon.tools.alerts import AlertManager


def create_monitoring_tools(
    config: Config,
    alert_manager: AlertManager,
    memory_store: MemoryStore | None = None,
) -> list[dict]:
    """Create the list of tool definitions for the agent.

    Returns a list of tool dicts compatible with the Claude Agent SDK.
    The actual tool handlers are closures that capture the alert_manager
    and memory_store instances.
    """
    tools = []

    # --- Alert tools ---
    async def send_alert(severity: str, title: str, message: str) -> str:
        """Send an alert with the given severity, title, and message.

        Args:
            severity: One of 'info', 'warning', or 'critical'.
            title: Short alert title.
            message: Detailed alert message.
        """
        return await alert_manager.send_alert(severity, title, message)

    def get_alert_history(last_n: int = 20) -> str:
        """Get recent alert history from the log file.

        Args:
            last_n: Number of recent alerts to return (default: 20).
        """
        return alert_manager.get_alert_history(last_n)

    tools.append({"function": send_alert, "name": "send_alert"})
    tools.append({"function": get_alert_history, "name": "get_alert_history"})

    # --- Memory tools ---
    if memory_store is not None and config.memory.enabled:
        def store_memory(observation: str, action: str, outcome: str) -> str:
            """Store an observation/action/outcome in persistent memory.

            Args:
                observation: What was observed (e.g., 'High CPU on nginx').
                action: What action was taken (e.g., 'Restarted nginx container').
                outcome: What happened after the action (e.g., 'CPU dropped to 15%').
            """
            entry_id = memory_store.store(observation, action, outcome)
            return f"Memory stored (id: {entry_id})"

        def query_memory(query: str, n_results: int = 5) -> str:
            """Search past observations by semantic similarity.

            Args:
                query: Search query describing what to look for.
                n_results: Maximum number of results to return (default: 5).
            """
            return memory_store.query(query, n_results)

        tools.append({"function": store_memory, "name": "store_memory"})
        tools.append({"function": query_memory, "name": "query_memory"})

    return tools
