# GraphReFly Python

`graphrefly` is GraphReFly for Python: a reactive graph on its own pure-Python
kernel, with a typed facade, host lifetimes and ecosystem integration.

## Where things are

| Path | Contents |
|---|---|
| [src/graphrefly/](src/graphrefly/) | Kernel (`_kernel`), checkpoint, facade and public docstrings |
| [tests/](tests/) | Package tests; [tests/kernel/](tests/kernel/) qualifies the kernel scenarios |
| [docs/](docs/README.md) | Documentation map, package guides and policy |
| [website/](website/) | Package-local site and generated API pipeline |
| [scripts/](scripts/) | API docs check, kernel benchmark, reference differential check |

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

For development, use Python 3.12+ and uv. No Rust toolchain or sibling checkout is
needed. [Development guidance](docs/development.md) has the details.

| Command | What it does |
|---|---|
| `uv sync --group dev --group docs` | Set up development/docs dependencies |
| `uv run pytest` | Package tests |
| `uv run ruff check .` / `uv run mypy src` | Python lint and types |
| `uv run python scripts/check_api_docs.py` | Public API documentation check |
| `uv build` | Build the pure-Python wheel and sdist |
| In `website/`: `pnpm docs:gen:check` | Generated API freshness |
| In `website/`: `pnpm build` | Local package-site build |

[Agent entrypoint](AGENTS.md).
