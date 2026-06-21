# GraphReFly Python

`graphrefly` is the Python host package for GraphReFly. The package is published under the legal PyPI distribution name `graphrefly` and imported as `graphrefly`; `@graphrefly/py` is only the product/repository label.

This clean-slate foundation layers a small Python-owned facade over the Rust native graph engine from `~/src/graphrefly-rs/crates/graphrefly-bindings-py`. It does not implement a second Python wave core, and it does not expose the raw PyO3 module as the final public API.

## v0 Surface

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

The v0 facade exposes:

- `Graph`
- `Node[T]`
- `Subscription`
- `Message[T]` and `GraphEvent`
- synchronous `Graph.state`, `Graph.producer`, `Graph.derived`, `Graph.effect`, and `Graph.batch`
- `Node.set`, `Node.cache`, `Node.status`, `Node.subscribe`
- `Graph.describe` and `Graph.observe`

## Boundary Notes

- The sync wave protocol runs in Rust; Python callbacks enter through the native dispatcher path.
- Native graph handles are single-thread host objects in v0.
- Python values are held as strong object references by the native engine. No serialization, copy, or immutability promise is made yet.
- `None` is reserved as the v0 no-DATA sentinel and cannot be emitted as DATA.
- Async callbacks are not accepted in the sync core. Asyncio/trio adapters are deferred to later CSP-7 slices.
- Callback failures become graph `ERROR` observations. Public API value/runtime failures use `GraphReflyValueError` and `GraphReflyRuntimeError`.

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
