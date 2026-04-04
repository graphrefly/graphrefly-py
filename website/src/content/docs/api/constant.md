---
title: 'constant'
description: 'Create a backoff strategy that always returns the same delay.'
---

Create a backoff strategy that always returns the same delay.

## Signature

```python
def constant(delay_ns: int) -> BackoffStrategy
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `delay_ns` | Fixed delay in nanoseconds (clamped to 0 if negative). |

## Returns

A :data:`BackoffStrategy` callable.

## Basic Usage

```python
from graphrefly.extra.backoff import constant, NS_PER_SEC
s = constant(2 * NS_PER_SEC)
assert s(0, None, None) == 2_000_000_000
assert s(5, None, None) == 2_000_000_000
```
