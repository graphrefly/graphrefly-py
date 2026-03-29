---
title: 'rescue'
description: 'Turn upstream ``ERROR`` into a normal ``DATA`` from ``recover(exc)``.'
---

Turn upstream ``ERROR`` into a normal ``DATA`` from ``recover(exc)``.

## Signature

```python
def rescue(recover: Callable[[BaseException], Any]) -> PipeOperator
```

## Documentation

Turn upstream ``ERROR`` into a normal ``DATA`` from ``recover(exc)``.
