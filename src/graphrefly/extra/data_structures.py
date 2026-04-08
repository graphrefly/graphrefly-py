"""Reactive data structures — roadmap §3.2 (KV map, log, index, list, pub/sub).

All mutation methods use two-phase protocol: ``DIRTY`` then ``DATA`` inside
:func:`~graphrefly.core.protocol.batch`, matching the TypeScript implementations.

Snapshot equality uses a monotonic *version* counter so downstream nodes can
emit ``RESOLVED`` instead of ``DATA`` when the visible collection is unchanged
(e.g. prune that evicts nothing, LRU reorder with no visible change).
"""

from __future__ import annotations

import contextlib
import threading
from bisect import bisect_left
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from graphrefly.core.node import Node

# --- Versioned snapshot -------------------------------------------------------


class Versioned(NamedTuple):
    """Immutable snapshot paired with a monotonic version for ``equals``.

    When the backing node has V0 versioning (GRAPHREFLY-SPEC §7), ``v0``
    carries the node's identity (``id``) and version counter for
    diff-friendly observation and cross-snapshot dedup (roadmap §6.0b).
    """

    version: int
    value: Any
    v0: dict[str, Any] | None = None


def _versioned_equals(a: Any, b: Any) -> bool:
    """``NodeOptions.equals`` comparing only the ``version`` field."""
    if not isinstance(a, Versioned) or not isinstance(b, Versioned):
        return a is b
    return a.version == b.version


def _v0_from_node(n: Any) -> dict[str, Any] | None:
    """Extract V0 identity from a node's versioning info, if available."""
    v = getattr(n, "v", None)
    if v is None:
        return None
    return {"id": v.id, "version": v.version}


def _bump(
    current: Versioned | None, next_value: Any, v0: dict[str, Any] | None = None
) -> Versioned:
    """Increment version. ``v0`` is captured before the backing node's DATA
    emission, so ``v0.version`` is one behind the node's post-emission value.
    This is intentional — ``v0`` records the node's version at snapshot
    construction time.
    """
    v = current.version if isinstance(current, Versioned) else 0
    return Versioned(version=v + 1, value=next_value, v0=v0)


# --- helpers ------------------------------------------------------------------


def _keepalive_derived(n: Any) -> None:
    """Subscribe so derived nodes stay wired to deps (``get()`` updates without a user sink)."""
    n.subscribe(lambda _msgs: None)


def _mono_now() -> int:
    return monotonic_ns()


def _push_two_phase(node: Any, snapshot: Any) -> None:
    """Emit DIRTY then DATA inside a batch — two-phase protocol."""
    with batch():
        node.down([(MessageType.DIRTY,)])
        node.down([(MessageType.DATA, snapshot)])


# --- reactive_map (KV + TTL + LRU eviction) -----------------------------------


class _MutableMapStore:
    """Internal mutable state for :class:`ReactiveMapBundle` — not a node."""

    __slots__ = ("entries", "lru")

    def __init__(self) -> None:
        self.entries: dict[Any, tuple[Any, int | None]] = {}
        self.lru: list[Any] = []


@dataclass(frozen=True, slots=True)
class ReactiveMapBundle:
    """Key–value store with optional per-key TTL and LRU eviction at capacity.

    Attributes:
        entries: A :func:`~graphrefly.core.sugar.state` node whose value is a
            :class:`~types.MappingProxyType` (immutable) of non-expired keys.

    Notes:
        TTL deadlines use :func:`~graphrefly.core.clock.monotonic_ns`
        (nanoseconds). Expired keys are evicted eagerly on ``get``/``has``
        and lazily on mutations via :meth:`prune_expired`. LRU order is
        refreshed on ``set``, ``get``, and ``has``.

    Example:
        ```python
        from graphrefly.extra import reactive_map
        m = reactive_map(max_size=10)
        m.set("a", 1)
        assert m.get("a") == 1        # synchronous key lookup
        assert m.has("a") is True
        assert m.size == 1
        ```
    """

    _store: _MutableMapStore
    entries: Node[MappingProxyType[Any, Any]]
    _default_ttl: float | None
    _max_size: int | None

    def _push_snapshot(self) -> None:
        """Build visible MappingProxyType from ``_store.entries``, filter expired, emit."""
        now = _mono_now()
        out: dict[Any, Any] = {}
        for k, (v, exp) in self._store.entries.items():
            if exp is not None and exp <= now:
                continue
            out[k] = v
        _push_two_phase(self.entries, MappingProxyType(out))

    def _prune_expired_internal(self) -> bool:
        """Remove expired keys from ``_store``; return whether anything changed."""
        now = _mono_now()
        expired = [
            k for k, (_, exp) in self._store.entries.items() if exp is not None and exp <= now
        ]
        if not expired:
            return False
        for k in expired:
            del self._store.entries[k]
            with contextlib.suppress(ValueError):
                self._store.lru.remove(k)
        return True

    def _evict_lru(self) -> None:
        """Evict oldest from ``_store.lru`` while over ``_max_size``."""
        if self._max_size is None:
            return
        while len(self._store.entries) > self._max_size and self._store.lru:
            victim = self._store.lru.pop(0)
            self._store.entries.pop(victim, None)

    def _touch_lru(self, key: Any) -> None:
        """Move *key* to end of ``_store.lru``."""
        with contextlib.suppress(ValueError):
            self._store.lru.remove(key)
        self._store.lru.append(key)

    def set(self, key: Any, value: Any, *, ttl: float | None = None) -> None:
        eff = self._default_ttl if ttl is None else ttl
        exp: int | None = None
        if eff is not None:
            if eff <= 0:
                msg = "ttl must be > 0"
                raise ValueError(msg)
            exp = _mono_now() + int(eff * 1_000_000_000)

        self._prune_expired_internal()
        self._store.entries[key] = (value, exp)
        self._touch_lru(key)
        self._evict_lru()
        self._push_snapshot()

    def delete(self, key: Any) -> None:
        if key not in self._store.entries:
            return
        del self._store.entries[key]
        with contextlib.suppress(ValueError):
            self._store.lru.remove(key)
        self._push_snapshot()

    def clear(self) -> None:
        if not self._store.entries:
            return
        self._store.entries.clear()
        self._store.lru.clear()
        self._push_snapshot()

    def get(self, key: Any, default: Any = None) -> Any:
        """Synchronous key lookup with LRU refresh and TTL eviction."""
        entry = self._store.entries.get(key)
        if entry is None:
            return default
        value, exp = entry
        if exp is not None and exp <= _mono_now():
            del self._store.entries[key]
            with contextlib.suppress(ValueError):
                self._store.lru.remove(key)
            self._push_snapshot()
            return default
        self._touch_lru(key)
        return value

    def has(self, key: Any) -> bool:
        """Check if key exists with LRU refresh and TTL eviction."""
        entry = self._store.entries.get(key)
        if entry is None:
            return False
        _, exp = entry
        if exp is not None and exp <= _mono_now():
            del self._store.entries[key]
            with contextlib.suppress(ValueError):
                self._store.lru.remove(key)
            self._push_snapshot()
            return False
        self._touch_lru(key)
        return True

    @property
    def size(self) -> int:
        """Number of non-expired entries (matches TS ``ReactiveMapBundle.size``)."""
        self._prune_expired_internal()
        return len(self._store.entries)

    def prune_expired(self) -> None:
        """Drop expired keys (monotonic clock) and emit if anything changed."""
        if not self._prune_expired_internal():
            return
        self._push_snapshot()


def reactive_map(
    *,
    default_ttl: float | None = None,
    max_size: int | None = None,
    name: str | None = None,
) -> ReactiveMapBundle:
    """Creates a reactive key–value map with optional TTL and LRU eviction.

    Args:
        default_ttl: If set, seconds until expiry when :meth:`ReactiveMapBundle.set` omits
            ``ttl`` (``None`` = no default expiry).
        max_size: If set, maximum number of entries; evicts LRU when exceeded (must be >= 1).
        name: Optional registry name for ``describe()`` / debugging.

    Returns:
        A :class:`ReactiveMapBundle` with imperative ``set`` / ``delete`` / ``clear`` /
        ``prune_expired`` and an ``entries`` node exposing the live snapshot.

    Example:
        ```python
        from graphrefly.extra import reactive_map
        m = reactive_map(default_ttl=60.0, max_size=100)
        m.set("x", 1)
        assert m.entries.get()["x"] == 1
        ```
    """

    if max_size is not None and max_size < 1:
        msg = "max_size must be >= 1 when set"
        raise ValueError(msg)

    store = _MutableMapStore()
    entries_node = state(
        MappingProxyType({}),
        describe_kind="state",
        equals=lambda a, b: a is b,
        name=name,
    )
    bundle = ReactiveMapBundle(
        _store=store,
        entries=entries_node,
        _default_ttl=default_ttl,
        _max_size=max_size,
    )
    return bundle


# --- reactive_log (append-only + tail view) -----------------------------------


@dataclass(frozen=True, slots=True)
class ReactiveLogBundle:
    """Append-only log of values stored as an immutable tuple.

    Attributes:
        entries: Node whose value is a ``tuple`` of all log entries.

    Example:
        ```python
        from graphrefly.extra import reactive_log
        lg = reactive_log()
        lg.append("event1")
        assert lg.entries.get() == ("event1",)
        ```
    """

    _state: Node[tuple[Any, ...]]
    entries: Node[tuple[Any, ...]]
    _max_size: int | None

    def _trim(self, t: tuple[Any, ...]) -> tuple[Any, ...]:
        """Trim from head if bounded and over capacity."""
        if self._max_size is not None and len(t) > self._max_size:
            return t[len(t) - self._max_size :]
        return t

    def append(self, value: Any) -> None:
        cur = self._state.get()
        t: tuple[Any, ...] = cur if isinstance(cur, tuple) else ()
        t = self._trim((*t, value))
        _push_two_phase(self._state, t)

    def append_many(self, values: Sequence[Any]) -> None:
        """Extend log with all *values*, trim once, emit one snapshot."""
        if not values:
            return
        cur = self._state.get()
        t: tuple[Any, ...] = cur if isinstance(cur, tuple) else ()
        t = self._trim((*t, *values))
        _push_two_phase(self._state, t)

    def trim_head(self, n: int) -> None:
        """Remove first *n* entries from the log and emit a snapshot.

        Args:
            n: Number of entries to remove from the head (must be >= 0).
        """
        if n < 0:
            msg = "n must be >= 0"
            raise ValueError(msg)
        if n == 0:
            return
        cur = self._state.get()
        t: tuple[Any, ...] = cur if isinstance(cur, tuple) else ()
        if n >= len(t):
            _push_two_phase(self._state, ())
        else:
            _push_two_phase(self._state, t[n:])

    def clear(self) -> None:
        cur = self._state.get()
        if isinstance(cur, tuple) and len(cur) == 0:
            return
        _push_two_phase(self._state, ())

    def tail(self, n: int) -> Node[tuple[Any, ...]]:
        """Last ``n`` entries (or fewer if shorter); updates when the log changes."""

        if n < 0:
            msg = "n must be >= 0"
            raise ValueError(msg)

        def _tail(deps: list[Any], _a: Any) -> tuple[Any, ...]:
            raw = deps[0]
            t: tuple[Any, ...] = raw if isinstance(raw, tuple) else ()
            return t[-n:] if n else ()

        raw = self._state.get()
        t0: tuple[Any, ...] = raw if isinstance(raw, tuple) else ()
        init_tail = t0[-n:] if n else ()
        out = derived([self._state], _tail, initial=init_tail)
        _keepalive_derived(out)
        return out


def reactive_log(
    initial: Sequence[Any] | None = None,
    *,
    max_size: int | None = None,
    name: str | None = None,
) -> ReactiveLogBundle:
    """Creates an append-only reactive log (tuple snapshot).

    Args:
        initial: Optional seed sequence; copied to a tuple.
        max_size: If set, maximum number of entries; oldest entries are trimmed
            from the head when the buffer exceeds this size (must be >= 1).
        name: Optional registry name for ``describe()`` / debugging.

    Returns:
        A :class:`ReactiveLogBundle` with ``append`` / ``append_many`` /
        ``trim_head`` / ``clear`` and :meth:`~ReactiveLogBundle.tail`.

    Example:
        ```python
        from graphrefly.extra import reactive_log
        lg = reactive_log([1, 2])
        lg.append(3)
        assert lg.entries.get() == (1, 2, 3)
        ```
    """
    if max_size is not None and max_size < 1:
        msg = "max_size must be >= 1"
        raise ValueError(msg)

    init = tuple(initial) if initial is not None else ()
    # Trim initial if it exceeds max_size
    if max_size is not None and len(init) > max_size:
        init = init[len(init) - max_size :]
    inner = state(
        init,
        describe_kind="state",
        equals=lambda a, b: a is b,
        name=name,
    )
    return ReactiveLogBundle(_state=inner, entries=inner, _max_size=max_size)


# --- reactive_index (primary key + secondary sort key) ----------------------


@dataclass(frozen=True, slots=True)
class _IndexRow[K]:
    primary: K
    secondary: Any
    value: Any


def _row_key(row: _IndexRow[Any]) -> tuple[Any, Any]:
    return (row.secondary, row.primary)


@dataclass(frozen=True, slots=True)
class ReactiveIndexBundle[K]:
    """Dual-key index: unique primary key with rows sorted by ``(secondary, primary)``.

    Attributes:
        by_primary: Derived node mapping ``primary -> value``.
        ordered: Derived node with all rows as a sorted tuple.

    Example:
        ```python
        from graphrefly.extra import reactive_index
        idx = reactive_index()
        idx.upsert("alice", score=90, value={"name": "Alice"})
        assert "alice" in idx.by_primary.get()
        ```
    """

    _state: Node[tuple[_IndexRow[K], ...]]
    by_primary: Node[dict[K, Any]]
    ordered: Node[tuple[_IndexRow[K], ...]]

    def upsert(self, primary: K, secondary: Any, value: Any) -> None:
        cur = self._state.get()
        prev: tuple[_IndexRow[K], ...] = cur if isinstance(cur, tuple) else ()
        rows = [r for r in prev if r.primary != primary]
        row = _IndexRow(primary=primary, secondary=secondary, value=value)
        keys = [_row_key(r) for r in rows]
        pos = bisect_left(keys, _row_key(row))
        rows.insert(pos, row)
        _push_two_phase(self._state, tuple(rows))

    def delete(self, primary: K) -> None:
        cur = self._state.get()
        prev: tuple[_IndexRow[K], ...] = cur if isinstance(cur, tuple) else ()
        rows = [r for r in prev if r.primary != primary]
        if len(rows) == len(prev):
            return
        _push_two_phase(self._state, tuple(rows))

    def clear(self) -> None:
        cur = self._state.get()
        if isinstance(cur, tuple) and len(cur) == 0:
            return
        _push_two_phase(self._state, ())


def reactive_index(*, name: str | None = None) -> ReactiveIndexBundle[Any]:
    """Creates a dual-key index: unique primary key, rows sorted by ``(secondary, primary)``.

    Args:
        name: Optional registry name for ``describe()`` / debugging.

    Returns:
        A :class:`ReactiveIndexBundle` with ``upsert`` / ``delete`` / ``clear`` and
        ``by_primary`` / ``ordered`` derived nodes.
    """
    empty: tuple[_IndexRow[Any], ...] = ()
    inner = state(
        empty,
        describe_kind="state",
        equals=lambda a, b: a is b,
        name=name,
    )

    def _ordered(deps: list[Any], _a: Any) -> tuple[_IndexRow[Any], ...]:
        raw = deps[0]
        return raw if isinstance(raw, tuple) else ()

    def _by_p(deps: list[Any], _a: Any) -> Any:
        raw = deps[0]
        rows = raw if isinstance(raw, tuple) else ()
        return MappingProxyType({r.primary: r.value for r in rows})

    by_p = derived([inner], _by_p, initial=MappingProxyType({}))
    ordered_node = derived([inner], _ordered, initial=())
    _keepalive_derived(by_p)
    _keepalive_derived(ordered_node)
    return ReactiveIndexBundle(_state=inner, by_primary=by_p, ordered=ordered_node)


# --- reactive_list -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReactiveListBundle:
    """Positional list backed by an immutable tuple snapshot.

    Attributes:
        items: Node whose value is the current item ``tuple``.

    Example:
        ```python
        from graphrefly.extra import reactive_list
        lst = reactive_list([1, 2])
        lst.append(3)
        assert lst.items.get() == (1, 2, 3)
        ```
    """

    _state: Node[tuple[Any, ...]]
    items: Node[tuple[Any, ...]]

    def _cur_tuple(self) -> tuple[Any, ...]:
        raw = self._state.get()
        return raw if isinstance(raw, tuple) else ()

    def append(self, value: Any) -> None:
        t = self._cur_tuple()
        _push_two_phase(self._state, (*t, value))

    def insert(self, index: int, value: Any) -> None:
        t = self._cur_tuple()
        if index < 0 or index > len(t):
            msg = "index out of range"
            raise IndexError(msg)
        _push_two_phase(self._state, (*t[:index], value, *t[index:]))

    def pop(self, index: int = -1) -> Any:
        t = self._cur_tuple()
        if not t:
            msg = "pop from empty list"
            raise IndexError(msg)
        i = index if index >= 0 else len(t) + index
        if i < 0 or i >= len(t):
            msg = "index out of range"
            raise IndexError(msg)
        v = t[i]
        _push_two_phase(self._state, (*t[:i], *t[i + 1 :]))
        return v

    def clear(self) -> None:
        if not self._cur_tuple():
            return
        _push_two_phase(self._state, ())


def reactive_list(
    initial: Sequence[Any] | None = None,
    *,
    name: str | None = None,
) -> ReactiveListBundle:
    """Creates a reactive list backed by an immutable tuple snapshot.

    Args:
        initial: Optional initial sequence.
        name: Optional registry name for ``describe()`` / debugging.

    Returns:
        A :class:`ReactiveListBundle` with ``append`` / ``insert`` / ``pop`` / ``clear``.
    """
    init = tuple(initial) if initial is not None else ()
    inner = state(
        init,
        describe_kind="state",
        equals=lambda a, b: a is b,
        name=name,
    )
    return ReactiveListBundle(_state=inner, items=inner)


# --- pubsub (lazy topic nodes) ------------------------------------------------


class PubSubHub:
    """Lazy per-topic source node registry for pub/sub messaging.

    Topics are created on first access as independent manual source nodes.
    Use :meth:`publish` to push values via the two-phase ``DIRTY`` then ``DATA``
    protocol. Thread-safe.

    Example:
        ```python
        from graphrefly.extra import pubsub
        from graphrefly.extra.sources import for_each
        hub = pubsub()
        log = []
        unsub = for_each(hub.topic("events"), log.append)
        hub.publish("events", "hello")
        unsub()
        assert log == ["hello"]
        ```
    """

    __slots__ = ("_lock", "_topics")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._topics: dict[str, Node[Any]] = {}

    def topic(self, name: str) -> Node[Any]:
        with self._lock:
            if name not in self._topics:
                self._topics[name] = state(None, describe_kind="state")
            return self._topics[name]

    def publish(self, name: str, value: Any) -> None:
        t = self.topic(name)
        _push_two_phase(t, value)

    def remove_topic(self, name: str) -> bool:
        """Tear down and remove a topic node by name.

        Returns ``True`` if the topic existed and was removed, ``False`` otherwise.
        """
        with self._lock:
            n = self._topics.get(name)
            if n is None:
                return False
            n.down([(MessageType.TEARDOWN,)])
            del self._topics[name]
            return True


def pubsub() -> PubSubHub:
    """Create a new :class:`PubSubHub` with an empty topic registry.

    Returns:
        A fresh :class:`PubSubHub` instance.

    Example:
        ```python
        from graphrefly.extra import pubsub
        hub = pubsub()
        hub.publish("topic", 1)
        assert hub.topic("topic").get() == 1
        ```
    """
    return PubSubHub()


# --- scan / slice helpers on reactive_log (optional graph nodes) -------------


def log_slice(
    log: ReactiveLogBundle,
    start: int,
    stop: int | None = None,
) -> Node[tuple[Any, ...]]:
    """Derived view of a slice of the log, same semantics as ``tuple[start:stop]`` (stop exclusive).

    Args:
        log: A :class:`ReactiveLogBundle`.
        start: Start index (must be >= 0).
        stop: End index (exclusive); if ``None``, slice to the end.

    Returns:
        A derived node emitting the sliced tuple; stays updated while the log changes.
    """

    if start < 0:
        msg = "start must be >= 0"
        raise ValueError(msg)

    def _slice(deps: list[Any], _a: Any) -> tuple[Any, ...]:
        raw = deps[0]
        t: tuple[Any, ...] = raw if isinstance(raw, tuple) else ()
        if stop is None:
            return t[start:]
        return t[start:stop]

    raw = log._state.get()
    t0: tuple[Any, ...] = raw if isinstance(raw, tuple) else ()
    init = t0[start:stop] if stop is not None else t0[start:]
    out = derived([log._state], _slice, initial=init)
    _keepalive_derived(out)
    return out


__all__ = [
    "PubSubHub",
    "ReactiveIndexBundle",
    "ReactiveListBundle",
    "ReactiveLogBundle",
    "ReactiveMapBundle",
    "log_slice",
    "pubsub",
    "reactive_index",
    "reactive_list",
    "reactive_log",
    "reactive_map",
]
