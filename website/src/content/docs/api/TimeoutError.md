---
title: 'TimeoutError'
description: 'Raised when :func:`timeout` fires before upstream delivers ``DATA``.'
---

Raised when :func:`timeout` fires before upstream delivers ``DATA``.

Subclasses the built-in :class:`builtins.TimeoutError` so ``except TimeoutError``
catches both GraphReFly timeouts and stdlib timeouts.

## Signature

```python
class TimeoutError(builtins.TimeoutError):  # noqa: A001
```
