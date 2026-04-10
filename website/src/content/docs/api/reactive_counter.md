---
title: 'reactive_counter'
description: 'Reactive counter with a cap — the building block for circuit breakers.'
---

Reactive counter with a cap — the building block for circuit breakers.

Wraps a ``state(0)`` node with ``increment()`` that respects a maximum.
The ``node`` is subscribable and composable like any reactive node. When
the cap is reached, ``increment()`` returns ``False``.

Returns a :class:`ReactiveCounterBundle` with ``node``, ``increment``,
``get``, and ``at_cap`` members.

## Signature

```python
def reactive_counter(cap: int) -> ReactiveCounterBundle
```
