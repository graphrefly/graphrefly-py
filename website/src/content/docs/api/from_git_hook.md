---
title: 'from_git_hook'
description: 'Git change detection as a reactive source.'
---

Git change detection as a reactive source.

## Signature

```python
def from_git_hook(
    repo_path: str,
    *,
    poll_ms: int = 5000,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **kwargs: Any,
) -> Node[Any]
```

## Documentation

Git change detection as a reactive source.

Polls for new commits on an interval and emits a structured ``GitEvent`` dict
whenever HEAD advances.  Zero filesystem side effects — no hook script installation.

**Limitations:** Polling cannot distinguish commit vs merge vs rebase — ``hook``
is always ``"post-commit"``.  When multiple commits land between polls, files are
aggregated but ``message``/``author`` reflect only the latest commit.

The emitted dict has keys: ``hook``, ``commit``, ``files``, ``message``, ``author``,
``timestamp_ns``.

Cross-repo usage::

    merge([from_git_hook(ts_repo), from_git_hook(py_repo)])

Args:
    repo_path: Absolute path to the git repository root.
    poll_ms: Polling interval in milliseconds.  Default ``5000``.
    include: Glob patterns — only include matching changed files.
    exclude: Glob patterns — exclude matching changed files.

Returns:
    A :class:`~graphrefly.core.node.Node` emitting one ``DATA`` per new commit.
