---
title: 'partition_for_batch'
description: 'Split *messages* into three groups by signal tier.'
---

Split *messages* into three groups by signal tier.

Returns ``(immediate, deferred, terminal)`` — tier 0-2/5, tier 3, tier 4.
Order within each group is preserved.

## Signature

```python
def partition_for_batch(messages: Messages) -> tuple[Messages, Messages, Messages]
```
