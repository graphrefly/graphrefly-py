"""Cascading cache with tiered storage and eviction policies (roadmap §3.1c)."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state

if TYPE_CHECKING:
    from graphrefly.core.node import Node

V = TypeVar("V")

__all__ = [
    "CacheTier",
    "CascadingCache",
    "EvictionPolicy",
    "TieredStorage",
    "cascading_cache",
    "lru",
    "tiered_storage",
]


@runtime_checkable
class EvictionPolicy(Protocol):
    """Eviction policy used by :class:`CascadingCache`."""

    def insert(self, key: str) -> None:
        """Record an insertion for the given key."""
        ...

    def touch(self, key: str) -> None:
        """Record an access (get/set) for the given key."""
        ...

    def delete(self, key: str) -> None:
        """Remove a key from eviction tracking."""
        ...

    def evict(self, count: int) -> list[str]:
        """Return up to *count* keys to evict."""
        ...

    def size(self) -> int:
        """Number of tracked keys."""
        ...


class _LruPolicy:
    """Least-recently-used eviction policy."""

    __slots__ = ("_order",)

    def __init__(self) -> None:
        self._order: OrderedDict[str, None] = OrderedDict()

    def insert(self, key: str) -> None:
        if key in self._order:
            self.touch(key)
            return
        self._order[key] = None
        self._order.move_to_end(key)

    def touch(self, key: str) -> None:
        if key in self._order:
            self._order.move_to_end(key)

    def delete(self, key: str) -> None:
        self._order.pop(key, None)

    def evict(self, count: int) -> list[str]:
        victims: list[str] = []
        for _ in range(count):
            if not self._order:
                break
            key, _ = self._order.popitem(last=False)
            victims.append(key)
        return victims

    def size(self) -> int:
        return len(self._order)


def lru() -> EvictionPolicy:
    """Factory returning an LRU eviction policy.

    Example:
        ```python
        from graphrefly.extra.cascading_cache import lru
        policy = lru()
        policy.on_insert("a")
        policy.on_insert("b")
        assert policy.evict() == "a"
        ```
    """
    return _LruPolicy()


@runtime_checkable
class CacheTier(Protocol):
    """A single tier in a cascading cache.

    Only :meth:`load` is required. ``save`` and ``clear`` are optional —
    tiers without them are treated as read-only.
    """

    def load(self, key: str) -> Any | None:
        """Load a value by key, or ``None`` if not present."""
        ...


class _MemoryTier:
    """Simple dict-backed CacheTier for internal use."""

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def load(self, key: str) -> Any | None:
        return self._store.get(key)

    def save(self, key: str, value: Any) -> None:
        self._store[key] = value

    def clear(self, key: str) -> None:
        self._store.pop(key, None)


def _tier_has_save(tier: CacheTier) -> bool:
    return callable(getattr(tier, "save", None))


def _tier_has_clear(tier: CacheTier) -> bool:
    return callable(getattr(tier, "clear", None))


class CascadingCache(Generic[V]):  # noqa: UP046
    """Multi-tier cache where each entry is a ``state()`` node.

    Tiers are checked in order; a miss at tier *i* tries tier *i+1*.
    On hit at tier *i*, the value is back-filled into tiers 0..i-1.

    Args:
        tiers: Ordered list of :class:`CacheTier` instances.
        max_size: Maximum entries (0 = unlimited).
        eviction: Optional :class:`EvictionPolicy` for size-bounded caches.
        write_through: If ``True``, :meth:`save` writes to all tiers.
    """

    __slots__ = ("_entries", "_eviction", "_max_size", "_tiers", "_write_through")

    def __init__(
        self,
        tiers: list[CacheTier],
        *,
        max_size: int = 0,
        eviction: EvictionPolicy | None = None,
        write_through: bool = False,
    ) -> None:
        self._tiers = list(tiers)
        self._max_size = max_size
        self._eviction = (eviction if eviction is not None else lru()) if max_size > 0 else None
        self._write_through = write_through
        self._entries: dict[str, Node[Any]] = {}

    def _promote(self, key: str, value: V, hit_tier: int) -> None:
        for i in range(hit_tier):
            tier = self._tiers[i]
            if _tier_has_save(tier):
                tier.save(key, value)  # type: ignore[union-attr]

    def _cascade(self, key: str, nd: Node[Any]) -> None:
        for tier_index, tier in enumerate(self._tiers):
            try:
                result = tier.load(key)
            except Exception:  # noqa: BLE001
                continue
            if result is not None:
                nd.down([(MessageType.DATA, result)])
                self._promote(key, result, tier_index)
                return

    def _evict_if_needed(self) -> None:
        if self._max_size <= 0 or self._eviction is None:
            return
        while self._eviction.size() >= self._max_size:
            victims = self._eviction.evict(1)
            if not victims:
                break
            for victim in victims:
                nd = self._entries.get(victim)
                if nd is not None:
                    value = nd.get()
                    if value is not None:
                        # Demote to deepest tier with save before evicting
                        for i in range(len(self._tiers) - 1, -1, -1):
                            if _tier_has_save(self._tiers[i]):
                                self._tiers[i].save(victim, value)  # type: ignore[union-attr]
                                for j in range(i):
                                    if _tier_has_clear(self._tiers[j]):
                                        self._tiers[j].clear(victim)  # type: ignore[union-attr]
                                break
                    nd.down([(MessageType.TEARDOWN,)])
                    del self._entries[victim]

    def load(self, key: str) -> Node[V | None]:
        """Load a value by key, returning a ``state()`` node.

        On cache miss across all tiers, the node holds ``None``.
        """
        if key in self._entries:
            if self._eviction is not None:
                self._eviction.touch(key)
            return self._entries[key]

        if (
            self._eviction is not None
            and self._max_size > 0
            and self._eviction.size() >= self._max_size
        ):
            self._evict_if_needed()

        nd: Node[Any] = state(None)
        self._entries[key] = nd
        if self._eviction is not None:
            self._eviction.insert(key)
        self._cascade(key, nd)
        return nd

    def save(self, key: str, value: V) -> None:
        """Save a value under the given key."""
        if self._write_through:
            for tier in self._tiers:
                if _tier_has_save(tier):
                    tier.save(key, value)  # type: ignore[union-attr]
        elif self._tiers and _tier_has_save(self._tiers[0]):
            self._tiers[0].save(key, value)  # type: ignore[union-attr]

        if key in self._entries:
            self._entries[key].down([(MessageType.DATA, value)])
            if self._eviction is not None:
                self._eviction.touch(key)
        else:
            if (
            self._eviction is not None
            and self._max_size > 0
            and self._eviction.size() >= self._max_size
        ):
                self._evict_if_needed()
            nd: Node[Any] = state(value)
            self._entries[key] = nd
            if self._eviction is not None:
                self._eviction.insert(key)

    def invalidate(self, key: str) -> None:
        """Re-cascade tiers into the existing cache node (subscribers see the update)."""
        if key not in self._entries:
            return
        self._cascade(key, self._entries[key])

    def delete(self, key: str) -> None:
        """Completely remove the entry from the cache and all tiers."""
        if key in self._entries:
            self._entries[key].down([(MessageType.TEARDOWN,)])
            del self._entries[key]
            if self._eviction is not None:
                self._eviction.delete(key)
        for tier in self._tiers:
            if _tier_has_clear(tier):
                tier.clear(key)  # type: ignore[union-attr]

    def has(self, key: str) -> bool:
        """Check if a key is in the in-memory entries."""
        return key in self._entries

    @property
    def size(self) -> int:
        """Number of in-memory entries."""
        return len(self._entries)


def cascading_cache(
    tiers: list[CacheTier],
    *,
    max_size: int = 0,
    eviction: EvictionPolicy | None = None,
    write_through: bool = False,
) -> CascadingCache[Any]:
    """Factory for a :class:`CascadingCache`.

    Args:
        tiers: Ordered list of :class:`CacheTier` instances (fastest first).
        max_size: Maximum number of entries (0 = unlimited).
        eviction: Optional :class:`EvictionPolicy` for bounded caches.
        write_through: If ``True``, writes go to all tiers.

    Returns:
        A :class:`CascadingCache` instance.

    Example:
        ```python
        from graphrefly.extra.cascading_cache import cascading_cache, lru
        cache = cascading_cache([], max_size=2, eviction=lru())
        cache.save("a", 1)
        cache.save("b", 2)
        assert cache.load("a").get() == 1
        ```
    """
    return CascadingCache(tiers, max_size=max_size, eviction=eviction, write_through=write_through)


class _CheckpointTier:
    """Wraps a CheckpointAdapter as a CacheTier."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def load(self, key: str) -> Any | None:
        return self._adapter.load(key)

    def save(self, key: str, value: Any) -> None:
        self._adapter.save(key, value)

    def clear(self, key: str) -> None:
        if hasattr(self._adapter, "clear"):
            self._adapter.clear(key)


class TieredStorage:
    """Reactive tiered storage backed by CheckpointAdapter instances.

    Wraps adapters as a :class:`CascadingCache` with write-through.
    """

    __slots__ = ("_cache",)

    def __init__(self, inner: CascadingCache[Any]) -> None:
        self._cache = inner

    def load(self, key: str) -> Node[Any]:
        return self._cache.load(key)

    def save(self, key: str, value: Any) -> None:
        self._cache.save(key, value)

    def invalidate(self, key: str) -> None:
        self._cache.invalidate(key)

    def delete(self, key: str) -> None:
        self._cache.delete(key)

    def has(self, key: str) -> bool:
        return self._cache.has(key)

    @property
    def size(self) -> int:
        return self._cache.size

    @property
    def cache(self) -> CascadingCache[Any]:
        """The underlying cascading cache (for advanced use)."""
        return self._cache


def tiered_storage(
    adapters: list[Any],
    *,
    max_size: int = 0,
    eviction: EvictionPolicy | None = None,
) -> TieredStorage:
    """Create a :class:`TieredStorage` from CheckpointAdapter instances.

    Each adapter is wrapped into a :class:`CacheTier`.

    Args:
        adapters: List of :class:`~graphrefly.extra.checkpoint.CheckpointAdapter` instances.
        max_size: Maximum entries (0 = unlimited).
        eviction: Optional :class:`EvictionPolicy`.

    Returns:
        A :class:`TieredStorage` instance with a ``.cache`` property.

    Example:
        ```python
        from graphrefly.extra.checkpoint import MemoryCheckpointAdapter
        from graphrefly.extra.cascading_cache import tiered_storage
        mem = MemoryCheckpointAdapter()
        ts = tiered_storage([mem])
        ts.save("k", {"data": 1})
        assert ts.load("k").get() == {"data": 1}
        assert ts.cache is not None
        ```
    """
    tiers = [_CheckpointTier(a) for a in adapters]
    inner = CascadingCache(tiers, max_size=max_size, eviction=eviction, write_through=True)
    return TieredStorage(inner)
