# Quick Start

Install:

```bash
pip install graphrefly
```

Create a graph:

```python
from graphrefly import Graph


with Graph("counter") as graph:
    count = graph.state(0, name="count")
    doubled = graph.derived([count], lambda value: value * 2, name="doubled")

    seen: list[int] = []

    def on_message(msg) -> None:
        if msg.kind == "DATA":
            seen.append(msg.value)

    with doubled.subscribe(on_message):
        count.set(1)
        count.set(2)

    assert seen[-1] == 4
    assert doubled.cache() == 4
```

## Advanced Node Callback

Use `Graph.node(...)` when the callback needs the `Ctx` surface:

```python
from graphrefly import Ctx, Graph


with Graph("advanced") as graph:
    source = graph.state(1, name="source")

    def add_ten(ctx: Ctx) -> None:
        ctx.emit(ctx.data(0) + 10)

    result = graph.node([source], add_ten, name="result")

    with result.subscribe(lambda msg: None):
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
    graph = Graph("async-demo")
    async with trio.open_nursery() as nursery:
        node = from_awaitable(graph, trio_runner(nursery), fetch_value, name="value")
        with node.subscribe(lambda msg: print(msg.kind, msg.value)):
            await trio.lowlevel.checkpoint()
```
