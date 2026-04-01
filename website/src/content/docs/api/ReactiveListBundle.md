---
title: 'ReactiveListBundle'
description: 'Positional list backed by an immutable versioned tuple snapshot.'
---

Positional list backed by an immutable versioned tuple snapshot.

## Signature

```python
class ReactiveListBundle
```

## Documentation

Positional list backed by an immutable versioned tuple snapshot.

Attributes:
    items: Node whose value is a :class:`Versioned` wrapping the current
        item tuple.

Example:
    ```python
    from graphrefly.extra import reactive_list
    lst = reactive_list([1, 2])
    lst.append(3)
    assert lst.items.get().value == (1, 2, 3)
    ```
