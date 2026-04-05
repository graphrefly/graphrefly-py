"""Tests for cascading_cache: lru, CascadingCache, tiered_storage."""

from __future__ import annotations

from typing import Any

from graphrefly.core.protocol import MessageType
from graphrefly.extra.cascading_cache import (
    CascadingCache,
    TieredStorage,
    cascading_cache,
    lru,
    tiered_storage,
)
from graphrefly.extra.checkpoint import MemoryCheckpointAdapter


class DictTier:
    """Simple dict-backed CacheTier for testing."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def load(self, key: str) -> Any | None:
        return self.store.get(key)

    def save(self, key: str, value: Any) -> None:
        self.store[key] = value

    def clear(self, key: str) -> None:
        self.store.pop(key, None)


def test_lru_basic_eviction() -> None:
    policy = lru()
    policy.insert("a")
    policy.insert("b")
    policy.insert("c")
    assert policy.size() == 3
    victims = policy.evict(1)
    assert victims == ["a"]
    assert policy.size() == 2


def test_lru_access_moves_to_end() -> None:
    policy = lru()
    policy.insert("a")
    policy.insert("b")
    policy.touch("a")
    assert policy.evict(1) == ["b"]
    assert policy.evict(1) == ["a"]


def test_lru_remove() -> None:
    policy = lru()
    policy.insert("a")
    policy.insert("b")
    policy.delete("a")
    assert policy.evict(1) == ["b"]
    assert policy.evict(1) == []


def test_lru_insert_idempotent() -> None:
    policy = lru()
    policy.insert("a")
    policy.insert("b")
    policy.insert("a")  # re-insert touches
    assert policy.evict(1) == ["b"]  # b is now LRU


def test_lru_evict_batch() -> None:
    policy = lru()
    policy.insert("a")
    policy.insert("b")
    policy.insert("c")
    victims = policy.evict(2)
    assert victims == ["a", "b"]
    assert policy.size() == 1


def test_cascading_cache_save_load() -> None:
    cc = cascading_cache([])
    cc.save("k1", 42)
    n = cc.load("k1")
    assert n.get() == 42


def test_cascading_cache_miss_returns_none() -> None:
    cc = cascading_cache([])
    n = cc.load("missing")
    assert n.get() is None


def test_cascading_cache_has_and_size() -> None:
    cc = cascading_cache([])
    assert cc.size == 0
    assert not cc.has("k")
    cc.save("k", 1)
    assert cc.has("k")
    assert cc.size == 1


def test_cascading_cache_same_node_dedup() -> None:
    t = DictTier()
    t.store["k"] = 1
    cc = cascading_cache([t])
    n1 = cc.load("k")
    n2 = cc.load("k")
    assert n1 is n2


def test_cascading_cache_invalidate() -> None:
    t = DictTier()
    cc = cascading_cache([t])
    cc.save("k", 10)
    assert t.store["k"] == 10
    # invalidate re-cascades from tiers; tier still has value so node is updated
    cc.invalidate("k")
    assert cc.load("k").get() == 10
    assert t.store["k"] == 10


def test_cascading_cache_delete() -> None:
    t = DictTier()
    cc = cascading_cache([t])
    cc.save("k", 10)

    msgs: list[Any] = []
    cc.load("k").subscribe(msgs.append)

    cc.delete("k")
    assert not cc.has("k")
    assert "k" not in t.store
    assert any(m[0] is MessageType.TEARDOWN for batch in msgs for m in batch)


def test_cascading_cache_max_size_with_lru() -> None:
    cc = cascading_cache([], max_size=2, eviction=lru())
    cc.save("a", 1)
    cc.save("b", 2)
    assert cc.size == 2
    cc.save("c", 3)
    assert cc.size == 2
    assert not cc.has("a")  # evicted
    assert cc.has("b")
    assert cc.has("c")


def test_cascading_cache_eviction_demotes_to_cold() -> None:
    hot = DictTier()
    cold = DictTier()
    cc = cascading_cache([hot, cold], max_size=1)
    cc.save("a", 10)
    cc.save("b", 20)  # evicts "a", demotes to cold
    assert cold.store.get("a") == 10


def test_cascading_cache_tier_cascade() -> None:
    t1 = DictTier()
    t2 = DictTier()
    t2.store["deep"] = {"data": 1}
    cc = cascading_cache([t1, t2])
    n = cc.load("deep")
    assert n.get() == {"data": 1}
    # Back-filled into t1
    assert t1.store.get("deep") == {"data": 1}


def test_cascading_cache_skips_throwing_tiers() -> None:
    class BrokenTier:
        def load(self, key: str) -> Any:
            raise RuntimeError("boom")

    good = DictTier()
    good.store["k"] = 42
    cc = cascading_cache([BrokenTier(), good])
    assert cc.load("k").get() == 42


def test_cascading_cache_write_through() -> None:
    t1 = DictTier()
    t2 = DictTier()
    cc = cascading_cache([t1, t2], write_through=True)
    cc.save("k", "val")
    assert t1.store["k"] == "val"
    assert t2.store["k"] == "val"


def test_cascading_cache_write_first_only() -> None:
    t1 = DictTier()
    t2 = DictTier()
    cc = cascading_cache([t1, t2], write_through=False)
    cc.save("k", "val")
    assert t1.store["k"] == "val"
    assert "k" not in t2.store


def test_cascading_cache_reactive_update() -> None:
    cc = cascading_cache([])
    cc.save("k", 1)
    n = cc.load("k")
    assert n.get() == 1
    cc.save("k", 2)
    assert n.get() == 2


def test_cascading_cache_save_creates_new_node() -> None:
    t = DictTier()
    cc = cascading_cache([t])
    cc.save("new", 99)
    assert cc.has("new")
    assert cc.load("new").get() == 99


def test_tiered_storage_with_checkpoint_adapters() -> None:
    mem1 = MemoryCheckpointAdapter()
    mem2 = MemoryCheckpointAdapter()
    ts = tiered_storage([mem1, mem2])
    ts.save("k", {"data": 42})
    # Write-through: both adapters have it
    assert mem1.load("k") == {"data": 42}
    assert mem2.load("k") == {"data": 42}
    n = ts.load("k")
    assert n.get() == {"data": 42}


def test_tiered_storage_cascade_load() -> None:
    mem1 = MemoryCheckpointAdapter()
    mem2 = MemoryCheckpointAdapter()
    mem2.save("deep", {"val": 99})
    ts = tiered_storage([mem1, mem2])
    n = ts.load("deep")
    assert n.get() == {"val": 99}
    # Back-filled
    assert mem1.load("deep") == {"val": 99}


def test_tiered_storage_with_eviction() -> None:
    mem = MemoryCheckpointAdapter()
    ts = tiered_storage([mem], max_size=2, eviction=lru())
    ts.save("a", 1)
    ts.save("b", 2)
    ts.save("c", 3)
    assert ts.size == 2
    assert not ts.has("a")


def test_tiered_storage_has_cache_property() -> None:
    ts = tiered_storage([MemoryCheckpointAdapter()])
    assert ts.cache is not None
    assert isinstance(ts.cache, CascadingCache)


def test_tiered_storage_is_instance() -> None:
    ts = tiered_storage([MemoryCheckpointAdapter()])
    assert isinstance(ts, TieredStorage)


def test_cascading_cache_is_instance() -> None:
    cc = cascading_cache([])
    assert isinstance(cc, CascadingCache)
