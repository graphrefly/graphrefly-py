"""Reactive data structures — roadmap §3.2 (KV map, log, index, list, pub/sub).

All mutation methods use two-phase protocol: ``DIRTY`` then ``DATA`` inside
:func:`~graphrefly.core.protocol.batch`, matching the TypeScript implementations.

Snapshot equality uses a monotonic *version* counter so downstream nodes can
emit ``RESOLVED`` instead of ``DATA`` when the visible collection is unchanged
(e.g. prune that evicts nothing, LRU reorder with no visible change).
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_left
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from graphrefly.core.node import Node

# --- Versioned snapshot -------------------------------------------------------


class Versioned(NamedTuple):
    """Immutable snapshot paired with a monotonic version for ``equals``."""

    version: int
    value: Any


def _versioned_equals(a: Any, b: Any) -> bool:
    """``NodeOptions.equals`` comparing only the ``version`` field."""
    if not isinstance(a, Versioned) or not isinstance(b, Versioned):
        return a is b
    return a.version == b.version


def _bump(current: Versioned | None, next_value: Any) -> Versioned:
    v = current.version if isinstance(current, Versioned) else 0
    return Versioned(version=v + 1, value=next_value)


# --- helpers ------------------------------------------------------------------


def _keepalive_derived(n: Any) -> None:
    """Subscribe so derived nodes stay wired to deps (``get()`` updates without a user sink)."""
    n.subscribe(lambda _msgs: None)


def _mono_now() -> float:
    return time.monotonic()


def _push_two_phase(node: Any, snapshot: Any) -> None:
    """Emit DIRTY then DATA inside a batch — two-phase protocol."""
    with batch():
        node.down([(MessageType.DIRTY,)])
        node.down([(MessageType.DATA, snapshot)])


# --- reactive_map (KV + TTL + LRU eviction) -----------------------------------


@dataclass(frozen=True, slots=True)
class _MapState:
    """Internal snapshot: values with optional monotonic expiry; LRU order (oldest first)."""

    entries: dict[Any, tuple[Any, float | None]]
    lru: tuple[Any, ...]

    @staticmethod
    def empty() -> _MapState:
        return _MapState(entries={}, lru=())


def _visible_map(ms: _MapState) -> MappingProxyType[Any, Any]:
    now = _mono_now()
    out: dict[Any, Any] = {}
    for k, (v, exp) in ms.entries.items():
        if exp is not None and exp <= now:
            continue
        out[k] = v
    return MappingProxyType(out)


def _purge_stale(ms: _MapState) -> _MapState:
    now = _mono_now()
    if not ms.entries:
        return ms
    alive: dict[Any, tuple[Any, float | None]] = {}
    for k, (v, exp) in ms.entries.items():
        if exp is not None and exp <= now:
            continue
        alive[k] = (v, exp)
    lru = tuple(k for k in ms.lru if k in alive)
    return _MapState(entries=alive, lru=lru)


def _touch_lru(order: tuple[Any, ...], key: Any) -> tuple[Any, ...]:
    without = tuple(k for k in order if k != key)
    return (*without, key)


def _evict_lru(
    entries: dict[Any, tuple[Any, float | None]],
    lru: tuple[Any, ...],
    max_size: int,
) -> tuple[dict[Any, tuple[Any, float | None]], tuple[Any, ...]]:
    while len(entries) > max_size and lru:
        victim = lru[0]
        lru = tuple(lru[1:])
        entries.pop(victim, None)
    return entries, lru


@dataclass(frozen=True, slots=True)
class ReactiveMapBundle:
    """Key–value store with optional per-key TTL and LRU eviction at capacity.

    Attributes:
        data: A :func:`~graphrefly.core.sugar.state` node whose value is a
            :class:`~types.MappingProxyType` (immutable) of non-expired keys.
            Uses versioned snapshots for efficient ``RESOLVED`` deduplication.

    Notes:
        TTL deadlines use :func:`time.monotonic` (seconds). Expired keys remain in the
        internal store until the next mutation or :meth:`prune`. LRU order is updated on
        ``set``, not on dict reads from ``data``.
    """

    _state: Node[_MapState]
    data: Node[Versioned]
    _default_ttl: float | None
    _max_size: int | None
    _version: list[int]

    def _sync_data(self) -> None:
        raw = self._state.get()
        snap = _visible_map(raw if raw is not None else _MapState.empty())
        cur = self.data.get()
        next_snap = _bump(cur if isinstance(cur, Versioned) else Versioned(0, snap), snap)
        self._version[0] = next_snap.version
        _push_two_phase(self.data, next_snap)

    def set(self, key: Any, value: Any, *, ttl: float | None = None) -> None:
        eff = self._default_ttl if ttl is None else ttl
        exp: float | None = None
        if eff is not None:
            if eff <= 0:
                msg = "ttl must be > 0"
                raise ValueError(msg)
            exp = _mono_now() + eff

        raw = self._state.get()
        cur = _purge_stale(raw if raw is not None else _MapState.empty())
        ent = dict(cur.entries)
        lru = _touch_lru(cur.lru, key)
        ent[key] = (value, exp)
        if self._max_size is not None:
            ent, lru = _evict_lru(ent, lru, self._max_size)
        _push_two_phase(self._state, _MapState(entries=ent, lru=lru))
        self._sync_data()

    def delete(self, key: Any) -> None:
        raw = self._state.get()
        cur = _purge_stale(raw if raw is not None else _MapState.empty())
        if key not in cur.entries:
            return
        ent = dict(cur.entries)
        del ent[key]
        lru = tuple(k for k in cur.lru if k != key)
        _push_two_phase(self._state, _MapState(entries=ent, lru=lru))
        self._sync_data()

    def clear(self) -> None:
        _push_two_phase(self._state, _MapState.empty())
        self._sync_data()

    def prune(self) -> None:
        """Drop expired keys (monotonic clock) and emit."""
        raw = self._state.get()
        _push_two_phase(
            self._state,
            _purge_stale(raw if raw is not None else _MapState.empty()),
        )
        self._sync_data()


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
        ``prune`` and a ``data`` node exposing the live snapshot.

    Examples:
        >>> from graphrefly.extra import reactive_map
        >>> m = reactive_map(default_ttl=60.0, max_size=100)
        >>> m.set("x", 1)
        >>> m.data.get().value["x"]
        1
    """

    if max_size is not None and max_size < 1:
        msg = "max_size must be >= 1 when set"
        raise ValueError(msg)

    inner = state(_MapState.empty(), describe_kind="state", name=name)
    init_snap = Versioned(version=0, value=MappingProxyType({}))
    data = state(init_snap, describe_kind="state", equals=_versioned_equals)
    bundle = ReactiveMapBundle(
        _state=inner,
        data=data,
        _default_ttl=default_ttl,
        _max_size=max_size,
        _version=[0],
    )
    return bundle


# --- reactive_log (append-only + tail view) -----------------------------------


@dataclass(frozen=True, slots=True)
class ReactiveLogBundle:
    """Append-only log of values stored as an immutable tuple.

    Attributes:
        entries: Node whose value is a :class:`Versioned` wrapping a ``tuple`` of all entries.
    """

    _state: Node[Versioned]
    entries: Node[Versioned]
    _max_size: int | None

    def _trim(self, t: tuple[Any, ...]) -> tuple[Any, ...]:
        """Trim from head if bounded and over capacity."""
        if self._max_size is not None and len(t) > self._max_size:
            return t[len(t) - self._max_size :]
        return t

    def append(self, value: Any) -> None:
        cur = self._state.get()
        t: tuple[Any, ...] = cur.value if isinstance(cur, Versioned) else (cur or ())
        t = self._trim((*t, value))
        _push_two_phase(self._state, _bump(cur, t))

    def append_many(self, values: Sequence[Any]) -> None:
        """Extend log with all *values*, trim once, emit one snapshot."""
        cur = self._state.get()
        t: tuple[Any, ...] = cur.value if isinstance(cur, Versioned) else (cur or ())
        t = self._trim((*t, *values))
        _push_two_phase(self._state, _bump(cur, t))

    def trim_head(self, n: int) -> None:
        """Remove first *n* entries from the log and emit a snapshot.

        Args:
            n: Number of entries to remove from the head (must be >= 0).
        """
        if n < 0:
            msg = "n must be >= 0"
            raise ValueError(msg)
        cur = self._state.get()
        t: tuple[Any, ...] = cur.value if isinstance(cur, Versioned) else (cur or ())
        if n >= len(t):
            _push_two_phase(self._state, _bump(cur, ()))
        else:
            _push_two_phase(self._state, _bump(cur, t[n:]))

    def clear(self) -> None:
        cur = self._state.get()
        _push_two_phase(self._state, _bump(cur, ()))

    def tail(self, n: int) -> Node[tuple[Any, ...]]:
        """Last ``n`` entries (or fewer if shorter); updates when the log changes."""

        if n < 0:
            msg = "n must be >= 0"
            raise ValueError(msg)

        def _tail(deps: list[Any], _a: Any) -> tuple[Any, ...]:
            raw = deps[0]
            t: tuple[Any, ...] = raw.value if isinstance(raw, Versioned) else (raw or ())
            return t[-n:] if n else ()

        raw = self._state.get()
        t0: tuple[Any, ...] = raw.value if isinstance(raw, Versioned) else (raw or ())
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

    Examples:
        >>> from graphrefly.extra import reactive_log
        >>> lg = reactive_log([1, 2])
        >>> lg.append(3)
        >>> lg.entries.get().value
        (1, 2, 3)
    """
    if max_size is not None and max_size < 1:
        msg = "max_size must be >= 1"
        raise ValueError(msg)

    init = tuple(initial) if initial is not None else ()
    # Trim initial if it exceeds max_size
    if max_size is not None and len(init) > max_size:
        init = init[len(init) - max_size :]
    inner = state(
        Versioned(version=0, value=init),
        describe_kind="state",
        equals=_versioned_equals,
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
    """Unique primary key; rows sorted by ``(secondary, primary)`` for ordered scans."""

    _state: Node[Versioned]
    by_primary: Node[dict[K, Any]]
    ordered: Node[Versioned]

    def upsert(self, primary: K, secondary: Any, value: Any) -> None:
        cur = self._state.get()
        prev: tuple[_IndexRow[K], ...] = cur.value if isinstance(cur, Versioned) else (cur or ())
        rows = [r for r in prev if r.primary != primary]
        row = _IndexRow(primary=primary, secondary=secondary, value=value)
        keys = [_row_key(r) for r in rows]
        pos = bisect_left(keys, _row_key(row))
        rows.insert(pos, row)
        _push_two_phase(self._state, _bump(cur, tuple(rows)))

    def delete(self, primary: K) -> None:
        cur = self._state.get()
        prev: tuple[_IndexRow[K], ...] = cur.value if isinstance(cur, Versioned) else (cur or ())
        rows = [r for r in prev if r.primary != primary]
        if len(rows) == len(prev):
            return
        _push_two_phase(self._state, _bump(cur, tuple(rows)))

    def clear(self) -> None:
        cur = self._state.get()
        _push_two_phase(self._state, _bump(cur, ()))


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
        Versioned(version=0, value=empty),
        describe_kind="state",
        equals=_versioned_equals,
        name=name,
    )

    def _ordered(deps: list[Any], _a: Any) -> tuple[_IndexRow[Any], ...]:
        raw = deps[0]
        return raw.value if isinstance(raw, Versioned) else (raw or ())

    def _by_p(deps: list[Any], _a: Any) -> Any:
        raw = deps[0]
        rows = raw.value if isinstance(raw, Versioned) else (raw or ())
        return MappingProxyType({r.primary: r.value for r in rows})

    by_p = derived([inner], _by_p, initial=MappingProxyType({}))
    ordered_node = derived([inner], _ordered, initial=())
    _keepalive_derived(by_p)
    _keepalive_derived(ordered_node)
    return ReactiveIndexBundle(_state=inner, by_primary=by_p, ordered=ordered_node)


# --- reactive_list -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReactiveListBundle:
    """Positional list as an immutable tuple snapshot (versioned)."""

    _state: Node[Versioned]
    items: Node[Versioned]

    def _cur_tuple(self) -> tuple[Any, ...]:
        raw = self._state.get()
        return raw.value if isinstance(raw, Versioned) else (raw or ())

    def append(self, value: Any) -> None:
        cur = self._state.get()
        t = self._cur_tuple()
        _push_two_phase(self._state, _bump(cur, (*t, value)))

    def insert(self, index: int, value: Any) -> None:
        cur = self._state.get()
        t = self._cur_tuple()
        if index < 0 or index > len(t):
            msg = "index out of range"
            raise IndexError(msg)
        _push_two_phase(self._state, _bump(cur, (*t[:index], value, *t[index:])))

    def pop(self, index: int = -1) -> Any:
        cur = self._state.get()
        t = self._cur_tuple()
        if not t:
            msg = "pop from empty list"
            raise IndexError(msg)
        i = index if index >= 0 else len(t) + index
        if i < 0 or i >= len(t):
            msg = "index out of range"
            raise IndexError(msg)
        v = t[i]
        _push_two_phase(self._state, _bump(cur, (*t[:i], *t[i + 1 :])))
        return v

    def clear(self) -> None:
        cur = self._state.get()
        _push_two_phase(self._state, _bump(cur, ()))


def reactive_list(
    initial: Sequence[Any] | None = None,
    *,
    name: str | None = None,
) -> ReactiveListBundle:
    """Creates a reactive list backed by an immutable tuple snapshot (versioned).

    Args:
        initial: Optional initial sequence.
        name: Optional registry name for ``describe()`` / debugging.

    Returns:
        A :class:`ReactiveListBundle` with ``append`` / ``insert`` / ``pop`` / ``clear``.
    """
    init = tuple(initial) if initial is not None else ()
    inner = state(
        Versioned(version=0, value=init),
        describe_kind="state",
        equals=_versioned_equals,
        name=name,
    )
    return ReactiveListBundle(_state=inner, items=inner)


# --- pubsub (lazy topic nodes) ------------------------------------------------


class PubSubHub:
    """Lazy per-topic :func:`~graphrefly.core.sugar.state` nodes (created on first access).

    Thread-safe topic registry. Each topic is an independent manual source node; use
    :meth:`publish` to push values with two-phase ``DIRTY`` then ``DATA``.
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
    """Returns a new :class:`PubSubHub` (empty topic registry)."""
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
        t: tuple[Any, ...] = raw.value if isinstance(raw, Versioned) else (raw or ())
        if stop is None:
            return t[start:]
        return t[start:stop]

    raw = log._state.get()
    t0: tuple[Any, ...] = raw.value if isinstance(raw, Versioned) else (raw or ())
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
    "Versioned",
    "log_slice",
    "pubsub",
    "reactive_index",
    "reactive_list",
    "reactive_log",
    "reactive_map",
]
