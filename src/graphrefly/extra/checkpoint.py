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
    """In-memory adapter (process-local; useful for tests)."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def save(self, data: dict[str, Any]) -> None:
        self._data = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class DictCheckpointAdapter:
    """Store under a fixed key inside a caller-owned ``dict`` (tests / embedding)."""

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
    """Atomic JSON file persistence (write temp + replace)."""

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
    """Persist one JSON blob under a fixed key using :mod:`sqlite3` (stdlib, zero deps).

    Call :meth:`close` when discarding the adapter.
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
    """Persist :meth:`~graphrefly.graph.Graph.snapshot`."""
    snap = graph.snapshot()
    _check_json_serializable(snap)
    adapter.save(snap)


def restore_graph_checkpoint(graph: Graph, adapter: CheckpointAdapter) -> bool:
    """Load a snapshot via :meth:`~graphrefly.graph.Graph.restore`; return whether data existed."""
    data = adapter.load()
    if data is None:
        return False
    graph.restore(data)
    return True


def checkpoint_node_value(node: Node[Any]) -> dict[str, Any]:
    """Minimal JSON-shaped payload for a single node's last value (for custom adapters)."""
    result: dict[str, Any] = {"version": 1, "value": node.get()}
    _check_json_serializable(result)
    return result
