# Changelog

## [0.23.0](https://github.com/graphrefly/graphrefly-py/compare/v0.22.0...v0.23.0) (2026-07-09)


### Features

* add HTTP and SSE network source adapters ([42293dd](https://github.com/graphrefly/graphrefly-py/commit/42293ddac8fbd4c40aaad181e135273df670a77b))
* add Python package documentation policy ([d80c0a3](https://github.com/graphrefly/graphrefly-py/commit/d80c0a380bd84a1173b53a316bd36bfc2a24ad94))


### Bug Fixes

* **ci:** read authority fixtures from main ([9dcee07](https://github.com/graphrefly/graphrefly-py/commit/9dcee070949436e621884e2f2eb3c3e8a04d40c8))
* release process ([ec40774](https://github.com/graphrefly/graphrefly-py/commit/ec40774586a6384083ae7b200d3241febe08cd10))
* version ([33a150e](https://github.com/graphrefly/graphrefly-py/commit/33a150e714ee7c50f3e2fb35875470d2b96ec158))


### Documentation

* align python docs header with shared site ([3c9fbde](https://github.com/graphrefly/graphrefly-py/commit/3c9fbde14464c8391a4ae87478fb75eeeb2ac56e))
* complete python api docs generation ([b5e7eff](https://github.com/graphrefly/graphrefly-py/commit/b5e7eff2b3e82a7cd9b49ecd14c6aeabab8929d7))
* deploy python docs on subdomain ([71c686a](https://github.com/graphrefly/graphrefly-py/commit/71c686ad71428f0b888b1e0a0e064b37b8076337))
* migrate python docs to starlight ([186e2cb](https://github.com/graphrefly/graphrefly-py/commit/186e2cbe22ab5860e9ecf8101b8da6db9207ce8a))
* point authority branch to main ([400b099](https://github.com/graphrefly/graphrefly-py/commit/400b0999f5e8b4e7f38a4bb7192a705dd8b809aa))
* remove stale mkdocstrings wording ([8636cd3](https://github.com/graphrefly/graphrefly-py/commit/8636cd3ac05a7edc6e1b41efaedc6e050e7c0eb4))
* switch python docs to zensical ([7b0c38a](https://github.com/graphrefly/graphrefly-py/commit/7b0c38a2dd21b8343c8731837c36e92a90de70a7))
* update Python package documentation and clarify boundaries ([f0e8064](https://github.com/graphrefly/graphrefly-py/commit/f0e8064b039053d9bacf78dc7c386bdf6390c7fd))

## 0.22.0 - 2026-06-29

- Reintroduces GraphReFly Python as a clean-slate host package over the Rust native engine.
- Adds the public Python facade for graph construction, state, derived values, subscribers, snapshots, async runner adapters, and wire bridge helpers.
- Adds wheel-only PyPI release automation for Linux, macOS, Windows, and PyPI Trusted Publishing.
- Adds MkDocs API documentation, local docs commands, and a quick-start README.

## v0.20.0 - 2026-04-11

### Chores

- Add integrations page.
- Update docs.

### Features

- Fix bug.

## v0.19.0 - 2026-04-10

- Historical pre-clean-slate release.
