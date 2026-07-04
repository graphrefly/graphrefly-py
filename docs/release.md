# Release

GraphReFly Python is built with `maturin` over the native Rust binding crate.

The 0.22 line publishes wheels only. Source distributions are deferred until
the release artifact can include a self-contained Rust source tree instead of
depending on a sibling checkout.

## Version

The Python package version lives in `pyproject.toml`.

## Wheels

Release wheels target the stable CPython ABI for Python 3.12+ (`abi3-py312`).
This keeps one wheel usable across supported ordinary CPython 3.12, 3.13, and
3.14 runtimes on the same platform.

Free-threaded CPython wheels are not part of the 0.22 release policy.

The release workflow checks out `graphrefly-rs@main`. Before tagging, confirm
Rust `main` contains the exact native binding commit intended for the Python
release.

## Publish

Release publishing is handled by GitHub Actions from tags matching `v*`.
Configure PyPI Trusted Publishing for this repository before pushing a release
tag. The workflow uses OpenID Connect, not a long-lived PyPI API token.

Before tagging, rebuild the package-local docs from source:

```bash
uv run python scripts/check_api_docs.py
pnpm --dir website docs:gen:check
pnpm --dir website build
```

Shared website/blog publishing remains owned by `~/src/graphrefly`. This package
deploys its own Starlight output to `https://py.graphrefly.dev/`; the shared site
links to that package-local artifact and does not copy generated Python API
pages into the main `graphrefly.dev` artifact.

```bash
git tag v0.22.0
git push origin v0.22.0
```
