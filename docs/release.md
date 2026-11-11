# Release

GraphReFly Python is a pure-Python package built with hatchling and released as
one universal wheel (`py3-none-any`) and an sdist for Python 3.12 and later
(graphrefly-py:D6). It has no build or runtime dependency on graphrefly-rs
(graphrefly:D800).

## Version

The Python package version lives in `pyproject.toml` and stays below 1.0
(graphrefly-py:D13): release-please bumps the minor version for a breaking change
(`bump-minor-pre-major`) and also writes the fallback `_VERSION` in
`src/graphrefly/_facade.py`. 0.23.0 was the last native wheel; the first pure-Python
release is 0.24.0.

## Distributions

`uv build` writes the wheel and the sdist to `dist/`. CI builds both on every pull
request; the release workflow builds them once and publishes them.

## Publish

Release publishing is handled by GitHub Actions with release-please and PyPI
Trusted Publishing. The normal path is:

1. Merge conventional commits to `main`, such as `fix: ...`, `feat: ...`, or a
   breaking `feat!: ...`.
2. The release workflow opens or updates a release PR that bumps
   `pyproject.toml`, updates `CHANGELOG.md`, and updates the release-please
   manifest.
3. Merge the release PR when ready.
4. The release workflow creates the GitHub release, runs the tests, builds the
   wheel and sdist, and publishes them to PyPI with OpenID Connect.

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
git tag v0.24.0
git push origin v0.24.0
gh workflow run release.yml --repo graphrefly/graphrefly-py --ref v0.24.0 -f publish=true
```
