---
title: 'to_array'
description: 'Collect all DATA values; on COMPLETE emit one DATA (the list) then COMPLETE.'
---

Collect all DATA values; on COMPLETE emit one DATA (the list) then COMPLETE.

Reactive version -- returns a Node. For blocking sync bridge, use :func:`to_list`.

## Signature

```python
def to_array(source: Node[Any]) -> Node[list[Any]]
```
