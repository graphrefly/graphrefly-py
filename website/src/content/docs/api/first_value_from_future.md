---
title: 'first_value_from_future'
description: 'Non-blocking bridge: return a :class:`concurrent.futures.Future` completed by first ``DATA``.'
---

Non-blocking bridge: return a :class:`concurrent.futures.Future` completed by first ``DATA``.

## Signature

```python
def first_value_from_future(source: Node[Any]) -> Future[Any]
```

## Documentation

Non-blocking bridge: return a :class:`concurrent.futures.Future` completed by first ``DATA``.

The future fails with the ``ERROR`` payload, or :class:`LookupError` if the source completes
without ``DATA``. Unsubscribes from *source* when the future completes.

There is no built-in timeout: sources like :func:`never` leave the future pending until
cancelled (``future.cancel()``). For blocking use with a deadline, prefer
:func:`first_value_from` with *timeout*.
