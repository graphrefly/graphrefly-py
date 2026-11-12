---
title: "Quick Start"
description: "Build and observe a small GraphReFly graph from Python."
---

Install:

```bash
pip install graphrefly
```

These examples use the public Python facade over the package's own Python kernel.
A subscriber is called once per array, with a tuple of messages such as
`("DATA", 1)` or `("COMPLETE",)`; the first array is START and the node's snapshot.

Create a graph:

```python
from graphrefly import DATA, Graph


with Graph() as graph:
    count = graph.state(0, name="count")
    doubled = graph.derived([count], lambda value: value * 2, name="doubled")

    seen: list[int] = []

    def on_array(array) -> None:
        seen.extend(m[1] for m in array if m[0] == DATA)

    with doubled.subscribe(on_array):
        count.set(1)
        count.set(lambda value: value + 1)  # computed from the current value
        assert doubled.cache() == 4

    assert seen == [0, 2, 4]
    # Without a subscriber, a computed node holds no value until it runs again.
    assert not doubled.has_value
```

## Advanced Node Callback

Use `Graph.node(...)` when the callback needs the `Ctx` surface. Its callback runs
on every wave it is given; `setup` runs once per lifetime and may return a cleanup:

```python
from graphrefly import Ctx, Graph


with Graph() as graph:
    source = graph.state(1, name="source")

    def add_ten(ctx: Ctx) -> None:
        (dep,) = ctx.deps  # dep.value, dep.has, dep.waves, dep.terminal
        ctx.emit(dep.value + 10)

    def setup(ctx: Ctx):
        print("activated")
        return lambda: print("closed")

    result = graph.node([source], add_ten, name="result", setup=setup)

    with result.subscribe(lambda array: None):
        source.set(5)
        assert result.cache() == 15
```

## Async Boundary

GraphReFly does not own your async runtime. Install optional adapters when you
want Trio or AnyIO convenience helpers:

```bash
pip install "graphrefly[async]"
```

```python
import trio
from graphrefly import Graph, from_awaitable, trio_runner


async def fetch_value() -> int:
    return 42


async def main() -> None:
    graph = Graph()
    async with trio.open_nursery() as nursery:
        node = from_awaitable(graph, trio_runner(nursery), fetch_value, name="value")
        with node.subscribe(lambda array: print([m[0] for m in array])):
            await trio.lowlevel.checkpoint()
```
