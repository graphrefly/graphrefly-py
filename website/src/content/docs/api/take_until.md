---
title: 'take_until'
description: 'Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.'
---

Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.

## Signature

```python
def take_until(
    notifier: Node[Any],
    *,
    predicate: Callable[[Any], bool] | None = None,
) -> PipeOperator
```

## Documentation

Forward the main source until ``notifier`` matches ``predicate``, then ``COMPLETE``.

Default ``predicate`` fires on ``DATA`` from the notifier (full message tuple is passed in).

Args:
    notifier: Second input observed for the stop condition.
    predicate: Optional ``(msg) -&gt; bool``; default tests ``msg[0] is MessageType.DATA``.

Returns:
    A :class:`~graphrefly.core.sugar.PipeOperator`.

Examples:
    &gt;&gt;&gt; from graphrefly import pipe, producer, state
    &gt;&gt;&gt; from graphrefly.extra import take_until as grf_tu
    &gt;&gt;&gt; stop = producer(lambda _d, a: a.emit(0))
    &gt;&gt;&gt; n = pipe(state(1), grf_tu(stop))
