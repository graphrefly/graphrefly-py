# Python agent entrypoint

Start with [README](README.md) and the [documentation map](docs/README.md).
Python owns its kernel, sugar and host facade in pure Python (graphrefly:D800,
graphrefly-py:D2); [Rust](../graphrefly-rs/AGENTS.md) is a separate package.
[Root authority](../graphrefly/AGENTS.md) governs protocol/cross-project work.

- [Development](docs/development.md) owns Python boundary/lifetime guidance,
  contributor constraints, setup and checks.
- [Docs policy](docs/docs.jsonl) owns package-local docs and generation; do not migrate skills.
- [Local work](plan/work.jsonl) and [root work locator](../graphrefly/authority/work-ledgers.jsonl)
  locate delivery without inferring state from history.
- [Local decisions](decisions/decisions.jsonl), [root locator](../graphrefly/authority/ledgers.jsonl)
  and [admission contract](../graphrefly/authority/README.md) locate rationale/history.
- [Current rules and inactive drafts](../graphrefly/authority/README.md#protocol-revision-contract)
  stay distinct; semantic changes require root spec-amend before code.

Follow [personal governance](/Users/davidchenallio/.codex/skills/project-governance/SKILL.md)
for authority/execution classification. Use local [bmad-build](.agents/skills/bmad-build/SKILL.md)
and [bmad-qa](.agents/skills/bmad-qa/SKILL.md) within approved work, with local gates.
Human review uses [bmad-checkpoint-preview](.agents/skills/bmad-checkpoint-preview/SKILL.md):
build-handoff for ordinary delivery; interactive for checkpoint/`--practice`.
Shared contract/design workflows remain [root-owned](../graphrefly/AGENTS.md#workflow-routing).
