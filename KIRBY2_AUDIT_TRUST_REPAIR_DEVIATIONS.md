# Kirby2 Audit Trust Repair Deviations

This file records prerequisite repairs discovered while executing the canonical
sequence in `KIRBY2_AUDIT_TRUST_REPAIR_ROADMAP.md`.

## ATR-03A — Accept immutable replay mappings in session scoring

Interrupted slice: ATR-03 (`Freeze core replay payloads`)

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
```

Observed failure:

```text
RuntimeError: traffic transition lacks evaluation details
```

Root cause: ATR-03 correctly freezes `TimelineRecord.data` and
`MarketStateRecord.snapshot` into recursively immutable mapping proxies and
tuples. Session scoring treated those JSON interfaces as concrete `dict` and
`list` instances. The first traffic transition was therefore rejected before
the model-risk audit could complete. The same concrete checks also existed in
state-machine reporting, discipline scoring, and condition inspection.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/session/scoring.py`

Repair:

1. Read immutable JSON objects through `collections.abc.Mapping`.
2. Read immutable JSON arrays through non-text `Sequence` values.
3. Thaw condition evidence copied into a public score report so the report
   remains ordinary detached JSON.
4. Do not change score formulas, thresholds, timeline fields, or digests.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-strategy-time
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: the original reproducer passes, strategy-time evidence remains
unchanged, immutable tuples and mappings are consumed without aliasing, and the
public score report remains JSON serializable.

Commit: `Accept immutable replay mappings in scoring`

Handoff: resume ATR-03 from the preserved interrupted-slice changes.
