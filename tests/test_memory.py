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

    def test_store_with_entry_type(self, memory_store):
        entry_id = memory_store.store(
            "cycle complete", "monitored", "all clear",
            entry_type="cycle_summary",
        )
        result = memory_store._collection.get(ids=[entry_id])
        assert result["metadatas"][0]["entry_type"] == "cycle_summary"

    def test_store_default_entry_type(self, memory_store):
        entry_id = memory_store.store("obs", "act", "out")
        result = memory_store._collection.get(ids=[entry_id])
        assert result["metadatas"][0]["entry_type"] == "observation"


class TestGetLastCycleSummary:
    """Test get_last_cycle_summary() method."""

    def test_returns_empty_when_no_summaries(self, memory_store):
        result = memory_store.get_last_cycle_summary()
        assert result == ""

    def test_returns_empty_when_only_observations(self, memory_store):
        memory_store.store("obs1", "act1", "out1")
        result = memory_store.get_last_cycle_summary()
        assert result == ""

    def test_returns_most_recent_summary(self, memory_store):
        memory_store.store(
            "old summary", "monitored", "issues found",
            entry_type="cycle_summary",
        )
        memory_store.store(
            "recent summary", "monitored", "all clear",
            entry_type="cycle_summary",
        )
        result = memory_store.get_last_cycle_summary()
        assert isinstance(result, str)
        assert len(result) > 0


class TestQueryByServices:
    """Test query_by_services() method."""

    def test_returns_empty_for_empty_list(self, memory_store):
        result = memory_store.query_by_services([])
        assert result == ""

    def test_queries_with_service_names(self, memory_store):
        memory_store.store(
            "nginx high CPU", "restarted nginx", "CPU dropped",
        )
        memory_store.store(
            "redis memory spike", "cleared cache", "memory normalized",
        )
        result = memory_store.query_by_services(["nginx", "redis"])
        assert isinstance(result, str)
        assert "|" in result  # contains formatted entries

    def test_returns_no_observations_for_empty_store(self, memory_store):
        result = memory_store.query_by_services(["nginx"])
        assert "No past observations" in result
