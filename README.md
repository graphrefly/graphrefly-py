# GraphReFly Python

`graphrefly` is the Python host package for GraphReFly. The package is published under the legal PyPI distribution name `graphrefly` and imported as `graphrefly`; `@graphrefly/py` is only the product/repository label.

This clean-slate foundation layers a small Python-owned facade over the Rust native graph engine from `~/src/graphrefly-rs/crates/graphrefly-bindings-py`. It does not implement a second Python wave core, and it does not expose the raw PyO3 module as the final public API.

## v1 Foundation Surface

```python
from graphrefly import Graph

graph = Graph("demo")
source = graph.state(1, name="source")
plus_one = graph.derived([source], lambda value: value + 1, name="plus_one")

with plus_one.subscribe(lambda msg: print(msg.kind, msg.value)):
    source.set(4)

assert plus_one.cache() == 5
assert plus_one.status in {"settled", "resolved"}
```

The Python-owned facade exposes:

- `Graph`
- `Node[T]`
- `Subscription`
- `DataMessage[T]`, `ErrorMessage`, `ControlMessage`, `Message[T]`, and `GraphEvent`
- synchronous `Graph.state`, `Graph.producer`, `Graph.derived`, `Graph.effect`, and `Graph.batch`
- explicit-Graph decorator sugar for `producer`, `derived`, and `effect`
- `Node.set`, `Node.cache(default=...)`, `Node.has_value`, `Node.status`, `Node.subscribe`
- graph-owned control convenience: `Graph.pause(node, lock_id)`, `Graph.resume(node, lock_id)`, and `Graph.invalidate(node)`
- `Graph.describe` and `Graph.observe`
- `Graph.close`, `Graph.closed`, and `Subscription.closed`

## Boundary Notes

- The sync wave protocol runs in Rust; Python callbacks enter through the native dispatcher path.
- Native graph handles are single-thread host objects in this foundation slice.
- Python values are held as strong object references by the native engine. No serialization, copy, or immutability promise is made yet.
- `None` is valid Python DATA. Absence of DATA is represented by private native presence flags, not by a public `None` sentinel.
- `Node.cache()` returns cached DATA, including `None`, or raises `GraphReflyNoDataError` when no DATA is present. Use `Node.cache(default=...)` or `Node.has_value` for non-exceptional absence handling.
- `Graph.close()` and `with Graph(...)` are Python host lifetime scopes. They release facade-created subscriptions/observers and reject later facade use without emitting protocol `TEARDOWN` or `COMPLETE`. Fatal host-boundary aborts automatically close/poison the facade after propagating the original fatal exception.
- Async callbacks are not accepted in the sync core. Asyncio/trio adapters are deferred to later CSP-7 slices.
- Node callback failures become graph `ERROR` observations wrapped as `GraphCallbackError`. Subscribe/observe callback failures stay at the Python observer boundary as `SubscriberCallbackError`. Public API value/runtime failures use `GraphReflyValueError` and `GraphReflyRuntimeError`.
- Fatal Python `BaseException` process-control failures such as `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` propagate back to the initiating Python caller instead of becoming graph `ERROR` or `SubscriberCallbackError`. Per D431/D436, a fatal first observed after native batch commit has begun aborts the host boundary but does not claim full transactional rollback of graph effects already committed; the facade is then closed/poisoned and later use is rejected.
- `DataIssue` is a reserved passive DATA envelope for future domain/material issue payloads; this slice does not emit it and does not change protocol `ERROR` semantics.

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
