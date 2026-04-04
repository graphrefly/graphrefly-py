---
title: 'race'
description: 'First source to emit ``DATA`` wins; subsequent traffic follows only that source.'
---

First source to emit ``DATA`` wins; subsequent traffic follows only that source.

## Signature

```python
def race(*sources: Node[Any]) -> Node[Any]
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `sources` | Contestants (empty → immediate ``COMPLETE``; one node is returned as-is). |

## Returns

A :class:`~graphrefly.core.node.Node`.

## Basic Usage

```python
from graphrefly.extra import race
from graphrefly import state
n = race(state(1), state(2))
```
