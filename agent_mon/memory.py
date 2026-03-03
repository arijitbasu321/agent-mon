"""ChromaDB vector memory for persisting observations across cycles."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_mon.config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryStore:
    """Persistent vector memory backed by ChromaDB."""

    def __init__(self, config: MemoryConfig):
        self.config = config
        self._client = None
        self._collection = None

    def initialize(self) -> None:
        """Create the persistence directory and ChromaDB collection."""
        import chromadb

        path = Path(self.config.path)
        path.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(path=str(path))
        self._collection = self._client.get_or_create_collection(
            name=self.config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "Memory store initialized: %s (%d entries)",
            self.config.path,
            self._collection.count(),
        )

    def store(
        self,
        observation: str,
        action: str,
        outcome: str,
        *,
        cycle_id: str | None = None,
    ) -> str:
        """Persist an observation/action/outcome tuple. Returns entry ID."""
        if self._collection is None:
            raise RuntimeError("MemoryStore not initialized -- call initialize() first")

        entry_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        document = f"{observation} | Action: {action} | Outcome: {outcome}"

        self._collection.add(
            ids=[entry_id],
            documents=[document],
            metadatas=[{
                "observation": observation,
                "action": action,
                "outcome": outcome,
                "timestamp": timestamp,
                "cycle_id": cycle_id or "",
            }],
        )

        logger.debug("Stored memory entry %s", entry_id)
        return entry_id

    def query(self, query_text: str, n_results: int | None = None) -> str:
        """Semantic search over past observations. Returns formatted text."""
        if self._collection is None:
            raise RuntimeError("MemoryStore not initialized -- call initialize() first")

        n = n_results or self.config.max_results

        if self._collection.count() == 0:
            return "No past observations in memory."

        # Don't request more results than exist
        n = min(n, self._collection.count())

        results = self._collection.query(
            query_texts=[query_text],
            n_results=n,
        )

        if not results["documents"] or not results["documents"][0]:
            return "No relevant past observations found."

        entries = []
        for doc, metadata in zip(
            results["documents"][0], results["metadatas"][0]
        ):
            ts = metadata.get("timestamp", "unknown")
            entries.append(f"[{ts}] {doc}")

        return "\n".join(entries)
