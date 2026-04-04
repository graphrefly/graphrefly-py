---
title: 'propagates_to_meta'
description: 'Whether *t* should be propagated from a parent node to its companion meta nodes.'
---

Whether *t* should be propagated from a parent node to its companion meta nodes.

Only TEARDOWN propagates; COMPLETE/ERROR/INVALIDATE do not.

## Signature

```python
def propagates_to_meta(t: MessageType) -> bool
```
