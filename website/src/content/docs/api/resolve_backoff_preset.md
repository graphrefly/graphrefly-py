---
title: 'resolve_backoff_preset'
description: 'Resolve a preset name string to a :data:`BackoffStrategy` with default parameters.'
---

Resolve a preset name string to a :data:`BackoffStrategy` with default parameters.

## Signature

```python
def resolve_backoff_preset(name: BackoffPreset) -> BackoffStrategy
```

## Documentation

Resolve a preset name string to a :data:`BackoffStrategy` with default parameters.

Args:
    name: One of ``"constant"``, ``"linear"``, ``"exponential"``,
        ``"fibonacci"``, or ``"decorrelated_jitter"``.

Returns:
    A :data:`BackoffStrategy` configured with default nanosecond parameters.

Example:
    ```python
    from graphrefly.extra.backoff import resolve_backoff_preset
    s = resolve_backoff_preset("exponential")
    assert s(0, None, None) == 100_000_000
    ```
