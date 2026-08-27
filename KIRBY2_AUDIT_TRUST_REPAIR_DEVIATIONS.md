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

## ATR-06A — Align the core-flow duration capability

Discovered: 2026-08-27 during ATR-07 preflight at
`609156a4d0d34d1408dba6a5284ac5eb6237c416`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.executors import CAPABILITY_MATRIX; from kirby2.auditlab.models import ExecutorLane; print(CAPABILITY_MATRIX[ExecutorLane.CORE_FLOW].credited_dimensions)"
```

The result contains seven credits, including `duration_events`. The fixed
architecture names six CORE_FLOW dimensions, and ATR-07 prescribes execution
through `RegimeOrderFlow.advance_to(configuration.duration_us)` while explicitly
requiring only six credits. No ATR-07 operation consumes `duration_events`, so
the seventh declaration cannot truthfully earn an `ExerciseRecord`.

Root cause: ATR-06 translated the conceptual `duration` capability into both
legacy minimization controls instead of the one simulation-time control used by
the real executor.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/auditlab/executors/base.py`
- `kirby2/audit/model_risk_lab.py`

Repair:

1. Credit `duration_us` as the sole CORE_FLOW duration dimension.
2. Keep `duration_events` in generated-configuration schema v2 for legacy
   compatibility and later predicate-aware minimization, without claiming that
   the real core-flow executor exercises it.
3. Make the runtime contract audit require the exact ordered six-dimension
   tuple so the overclaim cannot recur silently.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.executors import CAPABILITY_MATRIX; from kirby2.auditlab.models import ExecutorLane; expected = ('seed', 'duration_us', 'flow_model', 'regime', 'volume', 'liquidity'); assert CAPABILITY_MATRIX[ExecutorLane.CORE_FLOW].credited_dimensions == expected; print('ATR_06A_CAPABILITY_MATRIX PASS dimensions=6')"
git diff --check
```

Acceptance: CORE_FLOW exposes exactly the six roadmap-authorized credits and
the complete model-risk runtime audit remains green.

Commit: `Align core flow duration capability`
