"""Union-find registry for per-subgraph write locking (roadmap 0.4).

Contract (aligned with callbag-recharge-py and GRAPHREFLY-SPEC §6.1):

- ``get()`` does not take the subgraph write lock; it uses a per-node ``threading.Lock``
  (``NodeImpl._cache_lock``) so the cached value is read/written with proper synchronization
  under free-threaded Python (nogil), independent of the component ``RLock``.
- Mutations that change node state or topology (``down``, recompute, subscribe /
  unsubscribe) run under the subgraph RLock for the node's union component.
- ``union_nodes`` merges components when nodes are linked by dependency edges so one
  logical subgraph serializes writes on one lock.
- ``defer_set`` / ``defer_down`` queue cross-subgraph work until the current write lock
  is released, avoiding deadlocks.

Each thread has its own defer queue (TLS), matching :func:`graphrefly.core.protocol.batch`
isolation per thread.
"""

from __future__ import annotations

import threading
import weakref
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


class _LockBox:
    """Mutable holder for a component RLock; redirect ``.lock`` on union."""

    __slots__ = ("lock",)

    def __init__(self) -> None:
        self.lock = threading.RLock()


_MAX_LOCK_RETRIES: int = 100


class _SubgraphRegistry:
    __slots__ = ("_children", "_meta_lock", "_parent", "_rank", "_boxes", "_refs")

    def __init__(self) -> None:
        self._meta_lock = threading.RLock()
        self._parent: dict[int, int] = {}
        self._rank: dict[int, int] = {}
        self._boxes: dict[int, _LockBox] = {}
        self._refs: dict[int, weakref.ref[object]] = {}
        self._children: dict[int, set[int]] = {}

    def _on_gc(self, node_id: int, ref_obj: weakref.ref[object]) -> None:
        with self._meta_lock:
            if self._refs.get(node_id) is not ref_obj:
                return
            self._refs.pop(node_id, None)
            parent = self._parent.get(node_id)
            if parent is None:
                return

            direct_children = list(self._children.get(node_id, ()))

            if parent == node_id:
                if direct_children:
                    new_root = direct_children[0]
                    self._parent[new_root] = new_root
                    new_root_kids = self._children.setdefault(new_root, set())
                    for child in direct_children[1:]:
                        self._parent[child] = new_root
                        new_root_kids.add(child)
                        kids = self._children.get(child)
                        if kids is not None:
                            kids.discard(node_id)

                    box = self._boxes.get(node_id)
                    if box is not None:
                        self._boxes[new_root] = box
                    self._rank[new_root] = self._rank.get(new_root, self._rank.get(node_id, 0))
            else:
                parent_kids = self._children.get(parent)
                if parent_kids is not None:
                    parent_kids.discard(node_id)
                for child in direct_children:
                    self._parent[child] = parent
                    if parent_kids is not None:
                        parent_kids.add(child)

            self._children.pop(node_id, None)
            self._parent.pop(node_id, None)
            self._rank.pop(node_id, None)
            self._boxes.pop(node_id, None)

    def _find_locked(self, node_id: int) -> int:
        parent = self._parent.get(node_id)
        if parent is None:
            return node_id
        if parent != node_id:
            root = self._find_locked(parent)
            if root != parent:
                old_parent = parent
                self._parent[node_id] = root
                # Maintain _children reverse map during path compression.
                old_kids = self._children.get(old_parent)
                if old_kids is not None:
                    old_kids.discard(node_id)
                self._children.setdefault(root, set()).add(node_id)
            return root
        return node_id

    def _ensure_locked(self, node: object) -> int:
        node_id = id(node)
        existing_ref = self._refs.get(node_id)
        existing_obj = existing_ref() if existing_ref is not None else None

        if node_id not in self._parent or existing_obj is None:
            self._parent[node_id] = node_id
            self._rank[node_id] = 0
            self._boxes[node_id] = _LockBox()
            self._children[node_id] = set()
            self._refs[node_id] = weakref.ref(node, lambda _ref: self._on_gc(node_id, _ref))
        return node_id

    def ensure_node(self, node: object) -> None:
        with self._meta_lock:
            self._ensure_locked(node)

    def union(self, node_a: object, node_b: object) -> None:
        with self._meta_lock:
            id_a = self._ensure_locked(node_a)
            id_b = self._ensure_locked(node_b)

            root_a = self._find_locked(id_a)
            root_b = self._find_locked(id_b)
            if root_a == root_b:
                return

            rank_a = self._rank.get(root_a, 0)
            rank_b = self._rank.get(root_b, 0)
            if rank_a < rank_b:
                root_a, root_b = root_b, root_a
            self._parent[root_b] = root_a
            self._children.setdefault(root_a, set()).add(root_b)
            if rank_a == rank_b:
                self._rank[root_a] = rank_a + 1

            canonical_lock = self._boxes[root_a].lock
            box_b = self._boxes.get(root_b)
            if box_b is not None:
                box_b.lock = canonical_lock

    @contextmanager
    def lock_for(self, node: object) -> Generator[None]:
        for _attempt in range(_MAX_LOCK_RETRIES):
            with self._meta_lock:
                node_id = self._ensure_locked(node)
                root = self._find_locked(node_id)
                box = self._boxes.get(root)
                if box is None:
                    box = _LockBox()
                    self._boxes[root] = box
                lock = box.lock

            lock.acquire()
            valid = False
            try:
                with self._meta_lock:
                    current_root = self._find_locked(node_id)
                    current_box = self._boxes.get(current_root)
                    if current_box is None:
                        current_box = _LockBox()
                        self._boxes[current_root] = current_box
                    valid = current_box.lock is lock
                if valid:
                    yield
                    return
            finally:
                lock.release()
        raise RuntimeError(
            f"subgraph lock acquisition failed after {_MAX_LOCK_RETRIES} retries "
            "(continuous union activity?)"
        )


_REGISTRY = _SubgraphRegistry()


def ensure_registered(node: object) -> None:
    """Register *node* in the subgraph registry (weak-ref; auto cleanup on GC)."""
    _REGISTRY.ensure_node(node)


def union_nodes(a: object, b: object) -> None:
    """Merge the subgraph components containing *a* and *b* (same write lock)."""
    _REGISTRY.union(a, b)


@contextmanager
def acquire_subgraph_write_lock(node: object) -> Generator[None]:
    """Acquire the write lock for the component containing *node*."""
    with _REGISTRY.lock_for(node):
        yield


# --- defer_set / defer_down (TLS queue, flushed when defer-aware lock exits) ---

_deferred_tls = threading.local()


def _get_deferred_depth() -> int:
    return getattr(_deferred_tls, "depth", 0)


def _inc_deferred_depth() -> None:
    _deferred_tls.depth = getattr(_deferred_tls, "depth", 0) + 1


def _dec_deferred_depth() -> int:
    current = getattr(_deferred_tls, "depth", 0)
    if current <= 0:
        raise RuntimeError("deferred depth underflow: lock/defer bookkeeping out of balance")
    depth = current - 1
    _deferred_tls.depth = depth
    return depth


def _get_deferred_queue() -> list[Callable[[], None]]:
    q: list[Callable[[], None]] | None = getattr(_deferred_tls, "queue", None)
    if q is None:
        q = []
        _deferred_tls.queue = q
    return q


@contextmanager
def acquire_subgraph_write_lock_with_defer(node: object) -> Generator[None]:
    """Acquire the subgraph write lock; flush deferred cross-subgraph work on exit."""
    _inc_deferred_depth()
    try:
        with _REGISTRY.lock_for(node):
            yield
    finally:
        if _dec_deferred_depth() == 0:
            queue = _get_deferred_queue()
            errors: list[Exception] = []
            first_base: BaseException | None = None
            while queue:
                pending = queue[:]
                queue.clear()
                for fn in pending:
                    try:
                        fn()
                    except Exception as e:
                        errors.append(e)
                    except BaseException as e:
                        if first_base is None:
                            first_base = e
            if first_base is not None:
                raise first_base
            if len(errors) == 1:
                raise errors[0]
            if len(errors) > 1:
                raise ExceptionGroup("deferred subgraph work", errors)


def defer_set(target: Any, value: Any) -> None:
    """Schedule ``target.set(value)`` after the current defer-aware lock exits.

    If not inside :func:`acquire_subgraph_write_lock_with_defer`, runs immediately.
    *target* must provide a ``set`` method (e.g. future state sugar).
    """
    if _get_deferred_depth() > 0:
        _get_deferred_queue().append(lambda: target.set(value))
    else:
        target.set(value)


def defer_down(node: Any, messages: Any) -> None:
    """Schedule ``node.down(messages)`` after the current defer-aware lock exits.

    If not inside :func:`acquire_subgraph_write_lock_with_defer`, runs immediately.
    """
    if _get_deferred_depth() > 0:
        _get_deferred_queue().append(lambda: node.down(messages))
    else:
        node.down(messages)


__all__ = [
    "acquire_subgraph_write_lock",
    "acquire_subgraph_write_lock_with_defer",
    "defer_down",
    "defer_set",
    "ensure_registered",
    "union_nodes",
]
