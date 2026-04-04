---
title: 'take_until'
description: 'Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.'
---

Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.

Default ``predicate`` fires on ``DATA`` from the notifier (full message tuple is passed in).

## Signature

```python
def take_until(
    notifier: Node[Any],
    *,
    predicate: Callable[[Any], bool] | None = None,
) -> PipeOperator
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `notifier` | Second input observed for the stop condition. |
| `predicate` | Optional ``(msg) -&gt; bool``; default tests ``msg[0] is MessageType.DATA``. |

## Returns

A :class:`~graphrefly.core.sugar.PipeOperator`.

## Basic Usage

```python
from graphrefly import pipe, producer, state
from graphrefly.extra import take_until as grf_tu
stop = producer(lambda _d, a: a.emit(0))
n = pipe(state(1), grf_tu(stop))
```
