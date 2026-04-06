---
title: 'FileCheckpointAdapter'
description: 'Persist checkpoint data as JSON files in a directory, keyed by sanitized filename.'
---

Persist checkpoint data as JSON files in a directory, keyed by sanitized filename.

Writes to a temporary file in the same directory, then renames it over the
target path to avoid partial writes.

## Signature

```python
class FileCheckpointAdapter
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `directory` | Destination directory path (``str`` or :class:`pathlib.Path`). |

## Basic Usage

```python
import tempfile, os
from graphrefly.extra.checkpoint import FileCheckpointAdapter
with tempfile.TemporaryDirectory() as d:
    adapter = FileCheckpointAdapter(d)
    data = {"version": 1, "nodes": {}, "edges": [], "subgraphs": [], "name": "g"}
    adapter.save("g", data)
    assert os.path.exists(os.path.join(d, "g.json"))
```
