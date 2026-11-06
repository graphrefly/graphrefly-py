# GraphReFly Python

`graphrefly` is the Python host package over GraphReFly's native Rust engine.
Python owns the public facade, typing, lifetimes and ecosystem integration.

## Where things are

| Path | Contents |
|---|---|
| [src/graphrefly/](src/graphrefly/) | Python facade and public docstrings |
| [tests/](tests/) | Python package tests |
| [docs/](docs/README.md) | Documentation map, package guides and policy |
| [website/](website/) | Package-local site and generated API pipeline |
| [Rust foundation](../graphrefly-rs/) | Native engine and Python binding crate |

## Where things stand

- **Work:** [local ledger](plan/work.jsonl), [root work locator](../graphrefly/authority/work-ledgers.jsonl).
- **Rules, drafts and decisions:** [documentation map](docs/README.md).
- **Package use:** [quick start](docs/quickstart.md), [API reference](docs/api.md)
  and [Python docs](https://py.graphrefly.dev/).
- **Releases:** [release guide](docs/release.md) and [changelog](CHANGELOG.md).

## Start

```sh
pip install graphrefly
```

For development, use Python 3.12+ and uv, with the sibling Rust checkout available.
[Development guidance](docs/development.md) explains native/toolchain setup.

| Command | What it does |
|---|---|
| `uv sync --group dev --group docs` | Set up development/docs dependencies |
| `uv run maturin develop --release` | Build/install the local native extension |
| `uv run pytest` | Package tests |
| `uv run ruff check .` / `uv run mypy src` | Python lint and types |
| `uv run python scripts/check_api_docs.py` | Public API documentation check |
| In `website/`: `pnpm docs:gen:check` | Generated API freshness |
| In `website/`: `pnpm build` | Local package-site build |

[Agent entrypoint](AGENTS.md).
