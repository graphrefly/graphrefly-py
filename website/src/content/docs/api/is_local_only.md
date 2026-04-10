---
title: 'is_local_only'
description: 'Whether ``t`` is a graph-local signal that should NOT cross a wire/transport boundary.'
---

Whether ``t`` is a graph-local signal that should NOT cross a wire/transport boundary.

Local-only signals (tier 0–2): START, DIRTY, INVALIDATE, PAUSE, RESUME.
Wire-crossing signals (tier 3+): DATA, RESOLVED, COMPLETE, ERROR, TEARDOWN.
Unknown message types (spec §1.3.6 forward-compat) also cross the wire.

## Signature

```python
def is_local_only(t: MessageType) -> bool
```
