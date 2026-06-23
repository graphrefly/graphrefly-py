# GraphReFly Python

`graphrefly` is the Python host package for GraphReFly. The package is published under the legal PyPI distribution name `graphrefly` and imported as `graphrefly`; `@graphrefly/py` is only the product/repository label.

This clean-slate foundation layers a small Python-owned facade over the Rust native graph engine from `~/src/graphrefly-rs/crates/graphrefly-bindings-py`. It does not implement a second Python wave core, and it does not expose the raw PyO3 module as the final public API.

## v1 Foundation Surface

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

with plus_one.subscribe(lambda msg: print(msg.kind, msg.value)), advanced.subscribe(
    lambda _msg: None
):
    source.set(4)

assert plus_one.cache() == 5
assert advanced.cache() == 14
assert plus_one.status in {"settled", "resolved"}
```

The Python-owned facade exposes:

- `Graph`
- `Node[T]`
- `Ctx`
- `PullContext`
- `Subscription`
- `DataMessage[T]`, `ErrorMessage`, `ControlMessage`, `Message[T]`, and `GraphEvent`
- `SENTINEL`, the Python protocol marker used inside raw ctx `wave_data` for INVALIDATE/no-DATA projection
- synchronous `Graph.state`, `Graph.producer`, `Graph.node`, `Graph.derived`, `Graph.effect`, and `Graph.batch`
- explicit-Graph decorator sugar for `producer`, `derived`, and `effect`
- advanced `Ctx` helpers for dep DATA presence/value reads, raw `wave_data`, `terminal(index)`, `emit`, per-node `state`, `on_invalidate`, `on_deactivation`, read-only `pull` / `pull_params()`, narrow `request_pull(...)` / `request_pull_next(...)`, and `ctx.rewire_next`
- `Node.set`, `Node.cache(default=...)`, `Node.has_value`, `Node.status`, `Node.subscribe`
- graph-owned control convenience: `Graph.pause(node, lock_id)`, `Graph.resume(node, lock_id)`, and `Graph.invalidate(node)`
- `Graph.describe` and `Graph.observe`
- framework-neutral async runner helpers: `from_awaitable(graph, runner, factory, ...)`, `from_async_iter(graph, runner, factory, ...)`, `async_node(graph, deps, runner, callback, ...)`, the `AsyncRunner` protocol, `Graph.reentry_queue().wrap_runner(runner)` for explicit owner-thread completion draining, and the optional caller-owned `asyncio_runner(...)` adapter
- `Graph.close`, `Graph.closed`, and `Subscription.closed`

## Boundary Notes

- The sync wave protocol runs in Rust; Python callbacks enter through the native dispatcher path.
- Native graph handles are single-thread host objects in this foundation slice.
- Python values are held as strong object references by the native engine. No serialization, copy, or immutability promise is made yet.
- `None` is valid Python DATA. Absence of DATA is represented by private native presence flags, not by a public `None` sentinel.
- Raw advanced ctx input is `ctx.wave_data`: `dep -> waves -> values`, where `[]` means no wave for that dep, `[[]]` means a RESOLVED-only wave, DATA payloads appear directly, and INVALIDATE appears as the exported `graphrefly.SENTINEL` object. `ctx.terminal(index)` is separate metadata: `False` for no terminal, `True` for COMPLETE, or an ERROR diagnostic payload. Ergonomic `ctx.data()` / `ctx.has_data()` are derived helpers, not the raw protocol shape. `graphrefly.SENTINEL` itself is not legal DATA.
- Pull-mode nodes use `Graph.node(..., pull_id="name", pausable=True | "resumeAll")`. During a PULL invocation, `ctx.pull` is a read-only `PullContext` and `ctx.pull_params(default=None)` reads the demand params. Downstream nodes may issue only narrow PULL demand via `ctx.request_pull(...)` or boundary-deferred `ctx.request_pull_next(...)`; these helpers do not expose arbitrary message construction.
- Deferred topology mutation uses `ctx.rewire_next.subscribe_dep(dep, callback)`, `ctx.rewire_next.unsubscribe_dep(dep, callback)`, or `ctx.rewire_next.replace_deps(deps, callback)`. The callback is required so each dep-shape change explicitly re-declares the positional fn/deps pairing. This is a narrow facade over the existing deferred rewire protocol, not raw `ctx.up`, raw message construction, cross-graph rewire, or immediate in-fn topology mutation.
- `Node.cache()` returns cached DATA, including `None`, or raises `GraphReflyNoDataError` when no DATA is present. Use `Node.cache(default=...)` or `Node.has_value` for non-exceptional absence handling.
- `Graph.close()` and `with Graph(...)` are Python host lifetime scopes. They release facade-created subscriptions/observers and reject later facade use without emitting protocol `TEARDOWN` or `COMPLETE`. Fatal host-boundary aborts automatically close/poison the facade after propagating the original fatal exception.
- Async work enters only through the explicit runner helpers. Ordinary `Graph.node`, `Graph.derived`, `Graph.effect`, `Graph.batch`, `Node.subscribe`, `Graph.observe`, lifecycle hooks, and rewire callbacks remain synchronous and reject awaitables. The core API does not own an asyncio loop; `asyncio_runner(...)` is only a convenience adapter over a caller-owned loop, and Trio/AnyIO can supply protocol-compatible runners without becoming package dependencies. When a runner completes on a non-owner thread, use the graph-owned re-entry queue: `queue = graph.reentry_queue()`, pass `queue.wrap_runner(runner)` into the async helper, then call `queue.drain(max_items=None)` from the graph owner thread. The queue accepts only GraphReFly-owned private completions; it is not a public callable enqueue or graph mutation channel.
- Node callback failures become graph `ERROR` observations wrapped as `GraphCallbackError`. Subscribe/observe callback failures stay at the Python observer boundary as `SubscriberCallbackError`. Public API value/runtime failures use `GraphReflyValueError` and `GraphReflyRuntimeError`.
- Fatal Python `BaseException` process-control failures such as `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` propagate back to the initiating Python caller instead of becoming graph `ERROR` or `SubscriberCallbackError`. Per D431/D436, a fatal first observed after native batch commit has begun aborts the host boundary but does not claim full transactional rollback of graph effects already committed; the facade is then closed/poisoned and later use is rejected.
- `DataIssue` is a reserved passive DATA envelope for future domain/material issue payloads; this slice does not emit it and does not change protocol `ERROR` semantics.
- Public Python does not expose raw `Node.up(msgs)`, raw `Node.down(msgs)`, arbitrary message construction/sending, raw `ctx.up(msgs)`, raw PyO3 handles, or parallel raw value aliases such as `latest`, `prevData`, `latestData`, or `depRecords[i].latest`.

## Local Development

This package expects a sibling checkout:

```text
~/src/graphrefly-py
~/src/graphrefly-rs
```

Install and test:

```bash
uv sync --group dev
uv run maturin develop --release
uv run pytest
uv run ruff check .
uv run mypy src
python -c "import graphrefly; print(graphrefly.version())"
```

The Rust foundation can be checked directly from the sibling repo:

```bash
cd ~/src/graphrefly-rs
mise exec -- cargo test -p graphrefly-bindings-py
```
