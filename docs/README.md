# Python documentation map

| Category | Question | Current entrypoint | Maintenance boundary |
|---|---|---|---|
| Rules | What applies now? | [Root revision contract](../../graphrefly/authority/README.md#protocol-revision-contract), [Python boundary guidance](development.md), [docs policy](docs.jsonl) | Python owns its kernel, sugar and facade (graphrefly:D800) |
| Draft rules | What is inactive or proposed? | Root [rule revisions](../../graphrefly/spec/rules.jsonl), local proposals | Do not describe draft behavior as implemented Python API |
| Decisions | Why this choice? | [Local ledger](../decisions/decisions.jsonl), [root locator](../../graphrefly/authority/ledgers.jsonl) | Root-origin history is relocation-only |
| Work | What is being done? | [Local work](../plan/work.jsonl), [root work locator](../../graphrefly/authority/work-ledgers.jsonl) | Keep empty work surfaces empty; do not infer a local cursor |
| Evidence | What was checked? | Owning work receipts, [B142 checks](../../graphrefly/sessions/active/docs-reorganization-evidence/2026-11-05-py.md) | Source-bound checks do not activate rules or accept product behavior |
| Design / guides | How do I use it? | [Package overview](index.md), [quick start](quickstart.md), [API](api.md), [development](development.md), [releases](release.md) | Docstrings/API generation own API bodies; index.md remains the package landing guide |
| Archive | What did earlier work say? | [Changelog](../CHANGELOG.md) and retained Git history | No new archive/trash candidate established |
| Reference | What informs further work? | [Root reference kernel](../../graphrefly/reference/kernel/README.md), [root guides](../../graphrefly/guide/guide.jsonl) | Read for behavior only; do not copy foreign authority, code or shared site/blog content |

[Docs policy](docs.jsonl) owns maintenance and generation checks. This map links
current sources; [index.md](index.md) teaches the package rather than repeating
this category table. No generated API page or site source was edited.
