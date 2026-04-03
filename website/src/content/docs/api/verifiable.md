---
title: 'verifiable'
description: 'Compose a value node with a reactive verification companion.'
---

Compose a value node with a reactive verification companion.

## Signature

```python
def verifiable(
    source: Any,
    verify_fn: Callable[[Any], Any],
    *,
    trigger: Any | None = None,
    auto_verify: bool = False,
    initial_verified: Any = None,
) -> VerifiableBundle
```

## Documentation

Compose a value node with a reactive verification companion.

Args:
    source: Value source (`Node`, scalar, awaitable, iterable, async iterable).
    verify_fn: Verification function returning a `NodeInput`.
    trigger: Optional reactive trigger; each `DATA` triggers verification.
    auto_verify: When true, source value changes also trigger verification.
    initial_verified: Initial value for the verification companion.
