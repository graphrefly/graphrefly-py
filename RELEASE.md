# Release Checklist

This repo publishes the `graphrefly` Python distribution: one pure-Python wheel
(`py3-none-any`) and an sdist, built with hatchling (graphrefly-py:D6).

## Before Release

1. Confirm `pyproject.toml` has the target version. Versions stay below 1.0
   (graphrefly-py:D13): release-please bumps the minor version for a breaking change
   (`bump-minor-pre-major`) and updates the fallback `_VERSION` in
   `src/graphrefly/_facade.py`. 0.23.0 was the last native wheel, so the first
   pure-Python release is 0.24.0.
2. Confirm PyPI Trusted Publishing is configured for:
   - repository: `graphrefly/graphrefly-py`
   - workflow: `release.yml`
   - environment: `pypi`
3. Run local gates:

```bash
uv sync --group dev --group docs
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/check_api_docs.py
uv build
```

## Publish

```bash
git tag v0.24.0
git push origin v0.24.0
```

The tag workflow builds the wheel and sdist, then publishes via PyPI Trusted
Publishing.

## After Release

1. Confirm PyPI lists the new files.
2. Install from a clean environment:

```bash
python -m venv /tmp/graphrefly-smoke
/tmp/graphrefly-smoke/bin/python -m pip install graphrefly
/tmp/graphrefly-smoke/bin/python - <<'PY'
from graphrefly import Graph

with Graph() as graph:
    source = graph.state(1)
    doubled = graph.derived([source], lambda value: value * 2)
    with doubled.subscribe(lambda array: None):
        source.set(2)
        # A computed node holds its value while it has a subscriber.
        assert doubled.cache() == 4
print("ok")
PY
```

3. Confirm https://py.graphrefly.dev/ reflects the release docs.
