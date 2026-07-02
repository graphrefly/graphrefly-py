# GraphReFly Python

`graphrefly` is the Python host package for GraphReFly: a reactive graph runtime
with a Python-owned facade over the native Rust engine.

Install it from PyPI:

```bash
pip install graphrefly
```

GraphReFly is alpha software, but the Python host/native binding closeout is now
backed by the clean-slate conformance suite. The package requires Python 3.12+.

## Quick Start

```python
from graphrefly import Graph


with Graph("hello") as graph:
    count = graph.state(1, name="count")
    doubled = graph.derived([count], lambda value: value * 2, name="doubled")

    with doubled.subscribe(lambda msg: print(msg.kind, msg.value)):
        count.set(2)
        count.set(3)

    assert doubled.cache() == 6
```

The subscription receives the initial cached value and each later update. The
graph owns the node topology; `with Graph(...)` closes facade resources when the
scope exits.

## Core Shape

```python
from graphrefly import Graph


graph = Graph("demo")

source = graph.state(1, name="source")
plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")
advanced = graph.node(
    [source],
    lambda ctx: ctx.emit(ctx.data(0) + 10),
    name="advanced",
)

with plus_one.subscribe(lambda msg: print(msg.kind, msg.value)):
    source.set(4)

assert plus_one.cache() == 5
assert advanced.cache() == 14
assert plus_one.status in {"settled", "resolved"}
```

Use `Graph.derived(...)` for value-level functions. Use `Graph.node(...)` when
you need the callback-scoped `Ctx` surface for advanced graph behavior such as
per-node state, raw wave data reads, invalidation hooks, pull demand, or
deferred rewire.

## Public Surface

The Python facade exports:

- `Graph`, `Node[T]`, `Ctx`, `PullContext`, `RewireNext`
- `Subscription`, `Retain`, `GraphReentryQueue`
- `DataMessage[T]`, `ErrorMessage`, `ControlMessage`, `Message[T]`,
  `GraphEvent`
- `SENTINEL` for raw `ctx.wave_data` INVALIDATE/no-DATA projection
- checkpoint and restore helpers: `GraphCheckpoint`, `RestoreRef`,
  `RestoreContext`, `RestoreDescriptor`, `RestoreRegistry`, `restore_ref`,
  `restore_registry`, `restore_graph`
- async boundary helpers: `AsyncRunner`, `from_awaitable`, `from_async_iter`,
  `async_node`, `asyncio_runner`, `trio_runner`, `anyio_runner`
- network source adapters: `HttpRequest`, `HttpResponse`, `HttpStreamHead`,
  `HttpStreamHeadEvent`, `HttpStreamChunkEvent`, `HttpStreamErrorEvent`,
  `HttpStreamCompleteEvent`, `HttpStreamDriverEvent`, `SseEvent`,
  `LocalHttpDriver`, `LocalHttpStreamDriver`, `from_http`, `from_sse`
- wire bridge facades: `wire_bridge`, `wire_bridge_protobuf`,
  `wire_edge_group`, `wire_bridge_ack_driver`
- public exceptions under `GraphReflyError`

The private native extension is loaded as `graphrefly._native`; it is not the
public API.

## Boundary Notes

- The sync wave protocol runs in Rust. Python callbacks enter through the native
  dispatcher path; Python does not reimplement the wave core.
- Native graph handles are single-thread host objects in this foundation slice.
- `None` is valid Python DATA. Absence of DATA is separate and `Node.cache()`
  raises `GraphReflyNoDataError` when no DATA is present. Use
  `Node.cache(default=...)` or `Node.has_value` for non-exceptional absence
  handling.
- `ctx.wave_data` is the raw advanced dep input shape. Ergonomic helpers such as
  `ctx.data()` and `ctx.has_data()` are derived from that shape.
- `Graph.close()` and `with Graph(...)` are Python host lifetime scopes. They
  release facade-created subscriptions/observers and graph-owned retain roots;
  they do not emit protocol `TEARDOWN` or `COMPLETE`.
- Public Python does not expose raw `Node.up(msgs)`, raw `Node.down(msgs)`,
  arbitrary message construction/sending, raw `ctx.up(msgs)`, or raw PyO3
  handles.

## Async Runners

Async work enters only through explicit runner helpers. The core API does not
own an asyncio loop, Trio nursery, AnyIO task group, background thread, portal,
or hidden pump.

Install optional runtime adapters with:

```bash
pip install "graphrefly[async]"
```

```python
import trio
from graphrefly import Graph, from_awaitable, trio_runner


async def fetch_value() -> int:
    return 42


async def main() -> None:
    graph = Graph("trio-demo")

    async with trio.open_nursery() as nursery:
        node = from_awaitable(
            graph,
            trio_runner(nursery),
            fetch_value,
            name="value",
        )
        with node.subscribe(lambda msg: print(msg.kind, msg.value)):
            await trio.lowlevel.checkpoint()
```

When a host runtime completes work away from the graph owner thread, keep
re-entry explicit:

```python
graph = Graph("queued-demo")
queue = graph.reentry_queue()
runner = queue.wrap_runner(host_owned_runner)
node = from_awaitable(graph, runner, fetch_value, name="queued")

with node.subscribe(lambda msg: None):
    queue.drain(max_items=None)
```

The queue accepts only GraphReFly-owned private completions; it is not a public
callable enqueue or graph mutation channel.

`from_http` and `from_sse` follow the same boundary rule: callers provide both
an explicit `AsyncRunner` and a host-owned driver. The package does not own an
event loop or ship a default HTTP client in this slice; `from_sse` parses
`text/event-stream` bytes from `LocalHttpStreamDriver` without hidden retry,
reconnect, or `Last-Event-ID` management.

## Documentation

- Python docs: https://graphrefly.dev/py/
- API reference: https://graphrefly.dev/py/api/
- Language-neutral spec: https://graphrefly.dev/spec/
- Repository: https://github.com/graphrefly/graphrefly-py

## Local Development

This package expects sibling checkouts:

```text
~/src/graphrefly-py
~/src/graphrefly-rs
```

Install and test:

```bash
uv sync --group dev --group docs
cd ../graphrefly-rs
mise exec -- bash -lc 'cd ../graphrefly-py && uv run maturin develop --release'
cd ../graphrefly-py
uv run pytest
uv run ruff check .
uv run mypy src
uv run mkdocs build --strict
python -c "import graphrefly; print(graphrefly.version())"
```

The Rust foundation can be checked directly from the sibling repo:

```bash
cd ~/src/graphrefly-rs
mise exec -- cargo test -p graphrefly-bindings-py
```
