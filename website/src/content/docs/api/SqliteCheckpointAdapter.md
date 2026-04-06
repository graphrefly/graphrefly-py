---
title: 'SqliteCheckpointAdapter'
description: 'Persist checkpoint blobs by key using :mod:`sqlite3` (stdlib, zero deps).'
---

Persist checkpoint blobs by key using :mod:`sqlite3` (stdlib, zero deps).

Uses a key-value table. Call :meth:`close` when the adapter is no longer needed.

## Signature

```python
class SqliteCheckpointAdapter
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `path` | Path to the SQLite database file (``str`` or :class:`pathlib.Path`). |

## Basic Usage

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
