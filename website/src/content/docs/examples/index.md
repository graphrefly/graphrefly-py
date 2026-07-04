---
title: "Examples"
description: "Small Python entry points for the graphrefly package."
---

Examples stay package-local so runnable Python code can track the exact facade,
typing, lifetime, and exception behavior shipped by `graphrefly`.

## Tiny derived value

```python
from graphrefly import Graph

with Graph("example") as graph:
    source = graph.state(2, name="source")
    doubled = graph.derived([source], lambda value: value * 2, name="doubled")

    with doubled.subscribe(lambda msg: None):
        source.set(5)

    assert doubled.cache() == 10
```

## Graph-owned cleanup

Use a context manager when a graph should reject later facade use after cleanup:

```python
from graphrefly import Graph

with Graph("lifetime") as graph:
    node = graph.state("open", name="state")
    assert node.cache() == "open"
```

Exact behavior and public methods are generated under the API Reference.
