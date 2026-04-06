"""Checkpoint adapters and :class:`~graphrefly.graph.Graph` save/restore helpers (roadmap §3.1)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from graphrefly.core.node import Node
    from graphrefly.graph.graph import Graph

__all__ = [
    "CheckpointAdapter",
    "DictCheckpointAdapter",
    "FileCheckpointAdapter",
    "MemoryCheckpointAdapter",
    "SqliteCheckpointAdapter",
    "checkpoint_node_value",
    "restore_graph_checkpoint",
    "save_graph_checkpoint",
]

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


@runtime_checkable
class CheckpointAdapter(Protocol):
    """Key-value checkpoint persistence (§3.1c)."""

    def save(self, key: str, data: Any) -> None: ...
    def load(self, key: str) -> Any | None: ...
    def clear(self, key: str) -> None: ...


class MemoryCheckpointAdapter:
    """In-memory checkpoint adapter (process-local; useful for tests and embedding).

    Stores a deep-copy of the snapshot so mutations to the saved dict do not
    affect the stored state.

    Example:
        ```python
        from graphrefly import Graph, state
        from graphrefly.extra.checkpoint import MemoryCheckpointAdapter, save_graph_checkpoint
        g = Graph("g"); g.add("x", state(1))
        adapter = MemoryCheckpointAdapter()
        save_graph_checkpoint(g, adapter)
        assert adapter.load("g")["nodes"]["x"]["value"] == 1
        ```
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def save(self, key: str, data: Any) -> None:
        self._store[key] = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self, key: str) -> Any | None:
        raw = self._store.get(key)
        return None if raw is None else dict(raw) if isinstance(raw, dict) else raw

    def clear(self, key: str) -> None:
        self._store.pop(key, None)


class DictCheckpointAdapter:
    """Store checkpoints by key inside a caller-owned ``dict``.

    Example:
        ```python
        from graphrefly.extra.checkpoint import DictCheckpointAdapter
        store = {}
        adapter = DictCheckpointAdapter(store)
        data = {"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"}
        adapter.save("mykey", data)
        assert "mykey" in store
        ```
    """

    __slots__ = ("_storage",)

    def __init__(self, storage: dict[str, Any]) -> None:
        self._storage = storage

    def save(self, key: str, data: Any) -> None:
        self._storage[key] = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self, key: str) -> Any | None:
        raw = self._storage.get(key)
        return None if raw is None else (dict(raw) if isinstance(raw, dict) else raw)

    def clear(self, key: str) -> None:
        self._storage.pop(key, None)


def _sanitize_key(key: str) -> str:
    return _SANITIZE_RE.sub(lambda m: f"%{ord(m.group()):02x}", key) + ".json"


class FileCheckpointAdapter:
    """Persist checkpoint data as JSON files in a directory, keyed by sanitized filename.

    Writes to a temporary file in the same directory, then renames it over the
    target path to avoid partial writes.

    Args:
        directory: Destination directory path (``str`` or :class:`pathlib.Path`).

    Example:
        ```python
        import tempfile, os
        from graphrefly.extra.checkpoint import FileCheckpointAdapter
        with tempfile.TemporaryDirectory() as d:
            adapter = FileCheckpointAdapter(d)
            data = {"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"}
            adapter.save("g", data)
            assert os.path.exists(os.path.join(d, "g.json"))
        ```
    """

    __slots__ = ("_dir",)

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def _path_for(self, key: str) -> Path:
        return self._dir / _sanitize_key(key)

    def save(self, key: str, data: Any) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(key)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(
            dir=self._dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def load(self, key: str) -> Any | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        data = json.loads(text)
        return data

    def clear(self, key: str) -> None:
        path = self._path_for(key)
        with suppress(FileNotFoundError):
            path.unlink()


def _stable_snapshot_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqliteCheckpointAdapter:
    """Persist checkpoint blobs by key using :mod:`sqlite3` (stdlib, zero deps).

    Uses a key-value table. Call :meth:`close` when the adapter is no longer needed.

    Args:
        path: Path to the SQLite database file (``str`` or :class:`pathlib.Path`).

    Example:
        ```python
        import tempfile, os
        from graphrefly.extra.checkpoint import SqliteCheckpointAdapter
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        adapter = SqliteCheckpointAdapter(tmp)
        adapter.save("g", {"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"})
        assert adapter.load("g")["version"] == 1
        adapter.close()
        os.unlink(tmp)
        ```
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, path: str | Path) -> None:
        import threading

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS graphrefly_checkpoint (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, key: str, data: Any) -> None:
        payload = _stable_snapshot_json(data)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO graphrefly_checkpoint (k, v) VALUES (?, ?)",
                (key, payload),
            )
            self._conn.commit()

    def load(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM graphrefly_checkpoint WHERE k = ?", (key,)
            ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0].strip():
            return None
        return json.loads(row[0])

    def clear(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM graphrefly_checkpoint WHERE k = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection (safe to call more than once)."""
        with suppress(Exception):
            self._conn.close()


def _check_json_serializable(data: dict[str, Any]) -> None:
    """Warn when snapshot values are not JSON-serializable."""
    try:
        json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        warnings.warn(
            f"Snapshot contains non-JSON-serializable values: {exc}. "
            "This may cause errors when persisting to JSON-based adapters.",
            stacklevel=3,
        )


def save_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> None:
    """Persist a :meth:`~graphrefly.graph.Graph.snapshot` via a :class:`CheckpointAdapter`.

    Args:
        graph: The :class:`~graphrefly.graph.Graph` to snapshot.
        adapter: Any :class:`CheckpointAdapter` (memory, file, SQLite, etc.).

    Example:
        ```python
        from graphrefly import Graph, state
        from graphrefly.extra.checkpoint import MemoryCheckpointAdapter, save_graph_checkpoint
        g = Graph("g"); g.add("x", state(5))
        adapter = MemoryCheckpointAdapter()
        save_graph_checkpoint(g, adapter)
        ```
    """
    snap = graph.snapshot()
    _check_json_serializable(snap)
    adapter.save(graph.name, snap)


def restore_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> bool:
    """Load a snapshot from *adapter* and apply it to *graph*.

    Uses :meth:`~graphrefly.graph.Graph.restore` for application.

    Args:
        graph: The target :class:`~graphrefly.graph.Graph`.
        adapter: Any :class:`CheckpointAdapter` to load from.

    Returns:
        ``True`` if snapshot data existed and was applied; ``False`` if the adapter
        had no saved data.

    Example:
        ```python
        from graphrefly import Graph, state
        from graphrefly.extra.checkpoint import (
            MemoryCheckpointAdapter,
            restore_graph_checkpoint,
            save_graph_checkpoint,
        )
        g = Graph("g"); x = state(0); g.add("x", x)
        adapter = MemoryCheckpointAdapter()
        save_graph_checkpoint(g, adapter)
        x.down([("DATA", 99)])
        restored = restore_graph_checkpoint(g, adapter)
        assert restored and g.get("x") == 0
        ```
    """
    data = adapter.load(graph.name)
    if data is None:
        return False
    graph.restore(data)
    return True


def checkpoint_node_value(node: Node[Any]) -> dict[str, Any]:
    """Build a minimal versioned JSON payload for a single node's last cached value.

    Useful for custom adapters that persist individual nodes rather than whole
    graph snapshots. Emits a warning when the value is not JSON-serializable.

    Args:
        node: Any :class:`~graphrefly.core.node.Node` whose ``get()`` value to capture.

    Returns:
        A ``dict`` with ``version`` (``1``) and ``value`` keys.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.extra.checkpoint import checkpoint_node_value
        x = state(42)
        payload = checkpoint_node_value(x)
        assert payload == {"version": 1, "value": 42}
        ```
    """
    result: dict[str, Any] = {"version": 1, "value": node.get()}
    _check_json_serializable(result)
    return result
