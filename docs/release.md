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

The release workflow checks out `graphrefly-rs@main`. Before merging a release
PR or manually publishing, confirm Rust `main` contains the exact native binding
commit intended for the Python release.

## Publish

Release publishing is handled by GitHub Actions with release-please and PyPI
Trusted Publishing. The normal path is:

1. Merge conventional commits to `main`, such as `fix: ...`, `feat: ...`, or a
   breaking `feat!: ...`.
2. The release workflow opens or updates a release PR that bumps
   `pyproject.toml`, updates `CHANGELOG.md`, and updates the release-please
   manifest.
3. Merge the release PR when ready.
4. The release workflow creates the GitHub release, builds wheels, and publishes
   them to PyPI with OpenID Connect.

Configure PyPI Trusted Publishing for this repository before merging the first
release PR. The workflow uses OpenID Connect, not a long-lived PyPI API token.

Before merging a release PR, rebuild the package-local docs from source:

```bash
uv run python scripts/check_api_docs.py
cd website && pnpm docs:gen:check
cd website && pnpm build
```

Shared website/blog publishing remains owned by `~/src/graphrefly`. This package
deploys its own Starlight output to `https://py.graphrefly.dev/`; the shared site
links to that package-local artifact and does not copy generated Python API
pages into the main `graphrefly.dev` artifact.

Manual fallback remains available. Dispatch the release workflow with
`publish=true` to publish the selected ref. To publish an already-created tag,
pass that tag as the workflow ref:

```bash
git tag v0.22.0
git push origin v0.22.0
gh workflow run release.yml --repo graphrefly/graphrefly-py --ref v0.22.0 -f publish=true
```
