---
title: 'ReactiveLogBundle'
description: 'Append-only log of values stored as an immutable versioned tuple.'
---

Append-only log of values stored as an immutable versioned tuple.

## Signature

```python
class ReactiveLogBundle
```

## Documentation

Append-only log of values stored as an immutable versioned tuple.

Attributes:
    entries: Node whose value is a :class:`Versioned` wrapping a ``tuple``
        of all log entries.

Example:
    ```python
    from graphrefly.extra import reactive_log
    lg = reactive_log()
    lg.append("event1")
    assert lg.entries.get().value == ("event1",)
    ```
