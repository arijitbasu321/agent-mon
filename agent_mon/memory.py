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
        entry_type: str = "observation",
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
                "entry_type": entry_type,
            }],
        )

        logger.debug("Stored memory entry %s", entry_id)

        # M5: evict old entries if over limit
        self._evict_if_needed()

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

    def get_last_cycle_summary(self) -> str:
        """Return the most recent cycle summary, or empty string if none."""
        if self._collection is None:
            raise RuntimeError("MemoryStore not initialized -- call initialize() first")

        try:
            if self._collection.count() == 0:
                return ""

            results = self._collection.get(
                where={"entry_type": "cycle_summary"},
            )

            if not results["documents"]:
                return ""

            # Find the most recent by timestamp
            best_doc = None
            best_ts = ""
            for doc, metadata in zip(results["documents"], results["metadatas"]):
                ts = metadata.get("timestamp", "")
                if ts > best_ts:
                    best_ts = ts
                    best_doc = doc

            return best_doc or ""
        except Exception:
            logger.debug("Failed to get last cycle summary", exc_info=True)
            return ""

    def query_by_services(self, service_names: list[str]) -> str:
        """Query memory for observations related to specific services."""
        if self._collection is None:
            raise RuntimeError("MemoryStore not initialized -- call initialize() first")

        if not service_names:
            return ""

        try:
            query_text = " ".join(service_names)
            return self.query(query_text)
        except Exception:
            logger.debug("Failed to query by services", exc_info=True)
            return ""

    # M5: evict oldest entries when over max_entries limit
    def _evict_if_needed(self) -> None:
        """Remove oldest entries if collection exceeds max_entries."""
        if self._collection is None:
            return

        count = self._collection.count()
        if count <= self.config.max_entries:
            return

        try:
            all_entries = self._collection.get()
            if not all_entries["ids"]:
                return

            # Sort by timestamp (oldest first)
            pairs = list(zip(all_entries["ids"], all_entries["metadatas"]))
            pairs.sort(key=lambda p: p[1].get("timestamp", ""))

            excess = count - self.config.max_entries
            ids_to_delete = [p[0] for p in pairs[:excess]]
            if ids_to_delete:
                self._collection.delete(ids=ids_to_delete)
                logger.info("Evicted %d old memory entries", len(ids_to_delete))
        except Exception:
            logger.debug("Failed to evict old entries", exc_info=True)
