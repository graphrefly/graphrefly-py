---
title: 'Versioned'
description: 'Immutable snapshot paired with a monotonic version for ``equals``.'
---

Immutable snapshot paired with a monotonic version for ``equals``.

When the backing node has V0 versioning (GRAPHREFLY-SPEC §7), ``v0``
carries the node's identity (``id``) and version counter for
diff-friendly observation and cross-snapshot dedup (roadmap §6.0b).

## Signature

```python
class Versioned(NamedTuple)
```
