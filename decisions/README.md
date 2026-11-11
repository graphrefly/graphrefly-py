# Python-local decisions

This ledger owns only `graphrefly-py` package and implementation decisions. Its record identity is
`graphrefly-py:<D#>`. Language-neutral product, protocol, cross-runtime and cross-project decisions
remain owned by `graphrefly`; protocol changes still use `/spec-amend` there.

`root-origin-history.jsonl` separately owns records originally authored as `graphrefly:<D#>` and
physically relocated here under D783. Their origin-qualified identity, body and provenance do not
change. New Python decisions never enter that historical ledger.

`decisions.jsonl` holds package-local durable locks and `execution.jsonl` holds package-local
execution approvals; a record enters either only when it is admitted. Do not copy upstream
decision text between ledgers.
