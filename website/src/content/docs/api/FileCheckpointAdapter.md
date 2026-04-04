---
title: 'FileCheckpointAdapter'
description: 'Persist checkpoint data as JSON to a file using atomic write-then-replace.'
---

Persist checkpoint data as JSON to a file using atomic write-then-replace.

Writes to a temporary file in the same directory, then renames it over the
target path to avoid partial writes.

## Signature

```python
class FileCheckpointAdapter
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `path` | Destination file path (``str`` or :class:`pathlib.Path`). |

## Basic Usage

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
