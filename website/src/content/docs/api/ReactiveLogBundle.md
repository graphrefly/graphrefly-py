---
title: 'ReactiveLogBundle'
description: 'Append-only log of values stored as an immutable tuple.'
---

Append-only log of values stored as an immutable tuple.

Attributes:
    entries: Node whose value is a ``tuple`` of all log entries.

## Signature

```python
class ReactiveLogBundle
```

## Basic Usage

```python
from graphrefly.extra import reactive_log
lg = reactive_log()
lg.append("event1")
assert lg.entries.get() == ("event1",)
```
