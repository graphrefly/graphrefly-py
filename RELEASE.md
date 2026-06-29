# Release Checklist

This repo publishes the `graphrefly` Python distribution.

The 0.22 line is wheel-only. The Python project builds against a sibling Rust
checkout, so source distributions are intentionally not uploaded until the
release artifact can include a self-contained Rust source tree.

## Before Release

1. Confirm `/Users/davidchenallio/src/graphrefly-rs` `main` contains the exact
   native binding commit intended for release. The publish workflow checks out
   `graphrefly-rs@main` by policy.
2. Confirm `pyproject.toml` has the target version.
3. Confirm PyPI Trusted Publishing is configured for:
   - repository: `graphrefly/graphrefly-py`
   - workflow: `release.yml`
   - environment: `pypi`
4. Run local gates:

```bash
uv sync --group dev --group docs
cd ../graphrefly-rs
mise exec -- bash -lc 'cd ../graphrefly-py && uv run maturin develop --release'
cd ../graphrefly-py
uv run pytest
uv run ruff check .
uv run mypy src
uv run mkdocs build --strict
```

5. Confirm the Rust binding gates:

```bash
cd ../graphrefly-rs
mise exec -- cargo test -p graphrefly-bindings-py
```

## Publish

```bash
git tag v0.22.0
git push origin v0.22.0
```

The tag workflow builds wheels, then publishes via PyPI Trusted Publishing.

## After Release

1. Confirm PyPI lists the new files.
2. Install from a clean environment:

```bash
python -m venv /tmp/graphrefly-smoke
/tmp/graphrefly-smoke/bin/python -m pip install graphrefly
/tmp/graphrefly-smoke/bin/python - <<'PY'
from graphrefly import Graph

with Graph("smoke") as graph:
    source = graph.state(1)
    doubled = graph.derived([source], lambda value: value * 2)
    with doubled.subscribe(lambda msg: None):
        source.set(2)
    assert doubled.cache() == 4
print("ok")
PY
```

3. Confirm https://graphrefly.dev/py/ reflects the release docs.
