---
title: "Recipes"
description: "Python package recipes for runtime and graph boundaries."
---

Recipes describe Python-specific composition around the clean-slate graph
facade. Cross-language protocol guarantees stay in the shared docs; exact Python
syntax stays here.

## Use an explicit async runner

Async work belongs at package-owned source and adapter boundaries. Python callers
provide the runtime runner explicitly:

```python
from graphrefly import Graph, from_awaitable, asyncio_runner

async def load_value() -> int:
    return 7

graph = Graph("runner")
node = from_awaitable(graph, asyncio_runner(), load_value, name="value")
```

## Keep re-entry explicit

When host-owned work completes away from the graph owner thread, use the
graph-owned re-entry queue. It is not a public graph mutation channel; it only
drains GraphReFly-owned completions.

```python
queue = graph.reentry_queue()
runner = queue.wrap_runner(host_owned_runner)
```
