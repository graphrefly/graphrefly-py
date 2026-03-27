# Contributing

## Setup

```bash
# Install mise (if not already)
curl https://mise.run | sh

# Setup the project
mise trust && mise install
uv sync
```

## Development

```bash
uv run pytest
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
```

## Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` - new feature
- `fix:` - bug fix
- `docs:` - documentation only
- `refactor:` - code change that neither fixes a bug nor adds a feature
- `test:` - adding or updating tests
- `chore:` - maintenance tasks

## Architecture

- Behavior and protocol: `docs/GRAPHREFLY-SPEC.md`
- Implementation phases: `docs/roadmap.md`
