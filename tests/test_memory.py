"""Tests for the ChromaDB vector memory store.

Covers:
- MemoryStore initialization creates directory and collection
- store() persists entries with correct metadata
- query() returns formatted results
- query() handles empty collection
- Error when not initialized
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_mon.config import MemoryConfig
from agent_mon.memory import MemoryStore


@pytest.fixture
def memory_config(tmp_path):
    """Return a MemoryConfig pointing to a temp directory."""
    return MemoryConfig(
        enabled=True,
        path=str(tmp_path / "memory"),
        collection_name="test_memory",
        max_results=5,
    )


@pytest.fixture
def memory_store(memory_config):
    """Return an initialized MemoryStore."""
    store = MemoryStore(memory_config)
    store.initialize()
    yield store


class TestMemoryStoreInit:
    """Test MemoryStore initialization."""

    def test_initialize_creates_directory(self, memory_config):
        store = MemoryStore(memory_config)
        store.initialize()
        assert Path(memory_config.path).exists()

    def test_initialize_creates_collection(self, memory_config):
        store = MemoryStore(memory_config)
        store.initialize()
        assert store._collection is not None

    def test_not_initialized_raises_on_store(self, memory_config):
        store = MemoryStore(memory_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            store.store("obs", "act", "out")

    def test_not_initialized_raises_on_query(self, memory_config):
        store = MemoryStore(memory_config)
        with pytest.raises(RuntimeError, match="not initialized"):
            store.query("test")


class TestMemoryStoreOperations:
    """Test store and query operations."""

    def test_store_returns_id(self, memory_store):
        entry_id = memory_store.store(
            "High CPU on nginx",
            "Restarted nginx",
            "CPU dropped to 15%",
        )
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0

    def test_store_increments_count(self, memory_store):
        assert memory_store._collection.count() == 0
        memory_store.store("obs1", "act1", "out1")
        assert memory_store._collection.count() == 1
        memory_store.store("obs2", "act2", "out2")
        assert memory_store._collection.count() == 2

    def test_store_with_cycle_id(self, memory_store):
        entry_id = memory_store.store(
            "obs", "act", "out", cycle_id="cycle-123"
        )
        result = memory_store._collection.get(ids=[entry_id])
        assert result["metadatas"][0]["cycle_id"] == "cycle-123"

    def test_query_empty_collection(self, memory_store):
        result = memory_store.query("anything")
        assert "No past observations" in result

    def test_query_returns_stored_entries(self, memory_store):
        memory_store.store(
            "High CPU on nginx",
            "Restarted nginx container",
            "CPU dropped to 15%",
        )
        memory_store.store(
            "Disk /data at 92%",
            "Cleaned old logs",
            "Disk dropped to 60%",
        )

        result = memory_store.query("CPU issues")
        assert isinstance(result, str)
        # Should contain at least one entry
        assert "|" in result

    def test_query_respects_n_results(self, memory_store):
        for i in range(10):
            memory_store.store(f"obs {i}", f"act {i}", f"out {i}")

        result = memory_store.query("observations", n_results=3)
        lines = [l for l in result.strip().split("\n") if l]
        assert len(lines) <= 3

    def test_store_metadata_fields(self, memory_store):
        entry_id = memory_store.store("obs", "act", "out")
        result = memory_store._collection.get(ids=[entry_id])
        metadata = result["metadatas"][0]
        assert metadata["observation"] == "obs"
        assert metadata["action"] == "act"
        assert metadata["outcome"] == "out"
        assert "timestamp" in metadata

    def test_document_format(self, memory_store):
        entry_id = memory_store.store(
            "high cpu", "restarted", "resolved"
        )
        result = memory_store._collection.get(ids=[entry_id])
        doc = result["documents"][0]
        assert "high cpu" in doc
        assert "Action: restarted" in doc
        assert "Outcome: resolved" in doc
