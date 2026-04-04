---
title: 'ReactiveLogBundle'
description: 'Append-only log of values stored as an immutable versioned tuple.'
---

Append-only log of values stored as an immutable versioned tuple.

Attributes:
    entries: Node whose value is a :class:`Versioned` wrapping a ``tuple``
        of all log entries.

## Signature

```python
class ReactiveLogBundle
```

## Basic Usage

```python
from graphrefly.extra import reactive_log
lg = reactive_log()
lg.append("event1")
assert lg.entries.get().value == ("event1",)
```
