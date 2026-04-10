---
title: 'message_tier'
description: 'Return the signal tier for a message type (see module docstring).'
---

Return the signal tier for a message type (see module docstring).

0: subscribe handshake (START) — immediate, first in canonical order
1: notification (DIRTY, INVALIDATE) — immediate
2: flow control (PAUSE, RESUME) — immediate
3: value (DATA, RESOLVED) — deferred inside batch()
4: terminal (COMPLETE, ERROR) — delivered after phase-3
5: destruction (TEARDOWN) — immediate, usually alone
1 for unknown types (forward-compat: immediate, after START)

## Signature

```python
def message_tier(t: MessageType) -> int
```
