---
title: 'with_max_attempts'
description: 'Cap any strategy at *max_attempts*; returns ``None`` after the cap.'
---

Cap any strategy at *max_attempts*; returns ``None`` after the cap.

## Signature

```python
def with_max_attempts(strategy: BackoffStrategy, max_attempts: int) -> BackoffStrategy
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `strategy` | Inner strategy to wrap. |
| `max_attempts` | Maximum number of attempts (inclusive). |
