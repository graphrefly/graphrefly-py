"""Checkpoint adapters and :class:`~graphrefly.graph.Graph` save/restore helpers (roadmap §3.1)."""

from __future__ import annotations

import json
import os
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


@runtime_checkable
class CheckpointAdapter(Protocol):
    """JSON-friendly snapshot persistence (single blob in / out)."""

    def save(self, data: dict[str, Any]) -> None: ...
    def load(self) -> dict[str, Any] | None: ...


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
        assert adapter.load()["nodes"]["x"]["value"] == 1
        ```
    """

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def save(self, data: dict[str, Any]) -> None:
        self._data = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class DictCheckpointAdapter:
    """Store a checkpoint under a fixed key inside a caller-owned ``dict``.

    Useful for tests or environments where you already manage a shared dict.

    Args:
        storage: The dict to store the checkpoint in.
        key: Key under which the snapshot is stored (default ``"graphrefly_checkpoint"``).

    Example:
        ```python
        from graphrefly.extra.checkpoint import DictCheckpointAdapter
        store = {}
        adapter = DictCheckpointAdapter(store)
        adapter.save({"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"})
        assert "graphrefly_checkpoint" in store
        ```
    """

    __slots__ = ("_key", "_storage")

    def __init__(self, storage: dict[str, Any], *, key: str = "graphrefly_checkpoint") -> None:
        self._storage = storage
        self._key = key

    def save(self, data: dict[str, Any]) -> None:
        self._storage[self._key] = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self) -> dict[str, Any] | None:
        raw = self._storage.get(self._key)
        return dict(raw) if isinstance(raw, dict) else None


class FileCheckpointAdapter:
    """Persist checkpoint data as JSON to a file using atomic write-then-replace.

    Writes to a temporary file in the same directory, then renames it over the
    target path to avoid partial writes.

    Args:
        path: Destination file path (``str`` or :class:`pathlib.Path`).

    Example:
        ```python
        import tempfile, os
        from graphrefly.extra.checkpoint import FileCheckpointAdapter
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = f.name
        adapter = FileCheckpointAdapter(tmp)
        adapter.save({"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"})
        assert os.path.exists(tmp)
        os.unlink(tmp)
        ```
    """

    __slots__ = ("_path",)

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def load(self) -> dict[str, Any] | None:
        if not self._path.is_file():
            return None
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        data = json.loads(text)
        return data if isinstance(data, dict) else None


def _stable_snapshot_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqliteCheckpointAdapter:
    """Persist one checkpoint blob under a fixed key using :mod:`sqlite3` (stdlib, zero deps).

    Uses a single-row table. Call :meth:`close` when the adapter is no longer needed.

    Args:
        path: Path to the SQLite database file (``str`` or :class:`pathlib.Path`).
        key: Row key for the checkpoint (default ``"graphrefly_checkpoint"``).

    Example:
        ```python
        import tempfile, os
        from graphrefly.extra.checkpoint import SqliteCheckpointAdapter
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp = f.name
        adapter = SqliteCheckpointAdapter(tmp)
        adapter.save({"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"})
        assert adapter.load()["version"] == 1
        adapter.close()
        os.unlink(tmp)
        ```
    """

    __slots__ = ("_conn", "_key")

    def __init__(self, path: str | Path, *, key: str = "graphrefly_checkpoint") -> None:
        self._conn = sqlite3.connect(str(path))
        self._key = key
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS graphrefly_checkpoint (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        self._conn.commit()

    def save(self, data: dict[str, Any]) -> None:
        payload = _stable_snapshot_json(data)
        self._conn.execute(
            "INSERT OR REPLACE INTO graphrefly_checkpoint (k, v) VALUES (?, ?)",
            (self._key, payload),
        )
        self._conn.commit()

    def load(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT v FROM graphrefly_checkpoint WHERE k = ?", (self._key,)
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0].strip():
            return None
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None

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
    adapter.save(snap)


def restore_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> bool:
    """Load a snapshot from *adapter* and apply it to *graph* via :meth:`~graphrefly.graph.Graph.restore`.

    Args:
        graph: The target :class:`~graphrefly.graph.Graph`.
        adapter: Any :class:`CheckpointAdapter` to load from.

    Returns:
        ``True`` if snapshot data existed and was applied; ``False`` if the adapter
        had no saved data.

    Example:
        ```python
        from graphrefly import Graph, state
        from graphrefly.extra.checkpoint import MemoryCheckpointAdapter, save_graph_checkpoint, restore_graph_checkpoint
        g = Graph("g"); x = state(0); g.add("x", x)
        adapter = MemoryCheckpointAdapter()
        save_graph_checkpoint(g, adapter)
        x.down([("DATA", 99)])
        restored = restore_graph_checkpoint(g, adapter)
        assert restored and g.get("x") == 0
        ```
    """
    data = adapter.load()
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
