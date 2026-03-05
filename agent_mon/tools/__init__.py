"""MCP server setup and tool registration.

Creates tool sets for orchestrator and investigator agents.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from agent_mon.config import Config
from agent_mon.memory import MemoryStore
from agent_mon.tools.alerts import AlertManager, sanitize_secrets


def create_orchestrator_tools(
    config: Config,
    alert_manager: AlertManager,
    memory_store: MemoryStore | None = None,
    investigate_fn: Callable[[str], Awaitable[str]] | None = None,
) -> list[dict]:
    """Create the tool set for the orchestrator agent.

    Orchestrator gets: send_alert, get_alert_history, store_memory, investigate_issue.
    It does NOT get query_memory (that's for investigators).
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

    # --- Memory store tool (H1: sanitize secrets before storing) ---
    if memory_store is not None and config.memory.enabled:
        def store_memory(observation: str, action: str, outcome: str) -> str:
            """Store an observation/action/outcome in persistent memory.

            Args:
                observation: What was observed (e.g., 'High CPU on nginx').
                action: What action was taken (e.g., 'Restarted nginx container').
                outcome: What happened after the action (e.g., 'CPU dropped to 15%').
            """
            observation = sanitize_secrets(observation)
            action = sanitize_secrets(action)
            outcome = sanitize_secrets(outcome)
            entry_id = memory_store.store(observation, action, outcome)
            return f"Memory stored (id: {entry_id})"

        tools.append({"function": store_memory, "name": "store_memory"})

    # --- Investigate tool (C1: now async) ---
    if investigate_fn is not None:
        async def investigate_issue(description: str) -> str:
            """Dispatch a sub-agent to deeply investigate a specific issue.

            Args:
                description: Description of the issue to investigate.
            """
            return await investigate_fn(description)

        tools.append({"function": investigate_issue, "name": "investigate_issue"})

    return tools


def create_investigator_tools(
    config: Config,
    memory_store: MemoryStore | None = None,
) -> list[dict]:
    """Create the tool set for an investigator sub-agent.

    Investigator gets: query_memory.
    It does NOT get send_alert or store_memory (orchestrator handles those).
    """
    tools = []

    if memory_store is not None and config.memory.enabled:
        def query_memory(query: str, n_results: int = 5) -> str:
            """Search past observations by semantic similarity.

            Args:
                query: Search query describing what to look for.
                n_results: Maximum number of results to return (default: 5).
            """
            return memory_store.query(query, n_results)

        tools.append({"function": query_memory, "name": "query_memory"})

    return tools


def create_monitoring_tools(
    config: Config,
    alert_manager: AlertManager,
    memory_store: MemoryStore | None = None,
) -> list[dict]:
    """Backward-compatible factory for interactive mode.

    Returns the full orchestrator tool set (without investigate_issue).
    """
    return create_orchestrator_tools(
        config, alert_manager, memory_store, investigate_fn=None,
    )
