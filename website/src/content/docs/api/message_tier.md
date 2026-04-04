---
title: 'message_tier'
description: 'Return the signal tier for a message type (see module docstring).'
---

Return the signal tier for a message type (see module docstring).

0: notification (DIRTY, INVALIDATE) — immediate
1: flow control (PAUSE, RESUME) — immediate
2: value (DATA, RESOLVED) — deferred inside batch()
3: terminal (COMPLETE, ERROR) — delivered after phase-2
4: destruction (TEARDOWN) — immediate, usually alone
0 for unknown types (forward-compat: immediate)

## Signature

```python
def message_tier(t: MessageType) -> int
```
