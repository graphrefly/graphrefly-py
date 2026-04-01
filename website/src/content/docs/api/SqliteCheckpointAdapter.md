---
title: 'SqliteCheckpointAdapter'
description: 'Persist one checkpoint blob under a fixed key using :mod:`sqlite3` (stdlib, zero deps).'
---

Persist one checkpoint blob under a fixed key using :mod:`sqlite3` (stdlib, zero deps).

## Signature

```python
class SqliteCheckpointAdapter
```

## Documentation

Persist one checkpoint blob under a fixed key using :mod:`sqlite3` (stdlib, zero deps).

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
