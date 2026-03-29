---
title: 'from_event_emitter'
description: 'Subscribe to an event emitter (e.g. custom emitter).'
---

Subscribe to an event emitter (e.g. custom emitter).

## Signature

```python
def from_event_emitter(
    emitter: Any,
    event_name: str,
    *,
    add_method: str = "add_listener",
    remove_method: str = "remove_listener",
) -> Node[Any]
```

## Documentation

Subscribe to an event emitter (e.g. custom emitter).

Emits each event payload as DATA. Teardown removes the listener.
Compatible with any object that has add/remove listener methods.
