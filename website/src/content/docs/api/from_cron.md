---
title: 'from_cron'
description: 'Fire on a cron schedule; each tick emits the tick index ``0, 1, 2, …``.'
---

Fire on a cron schedule; each tick emits the tick index ``0, 1, 2, …``.

## Signature

```python
def from_cron(expr: str) -> Node[Any]
```

## Documentation

Fire on a cron schedule; each tick emits the tick index ``0, 1, 2, …``.

Requires the third-party ``croniter`` package (``uv add croniter`` or ``pip install croniter``).
