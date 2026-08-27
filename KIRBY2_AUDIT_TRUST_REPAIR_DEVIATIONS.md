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

## ATR-06B — Cycle configuration axes within each executor lane

Discovered: 2026-08-27 during the resumed ATR-07 preflight at
`51b948a9e3f67a10b124a9137d45506b50329cb9`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.executors import CAPABILITY_MATRIX; from kirby2.auditlab.generator import generate_configurations; from kirby2.auditlab.models import ExecutorLane; configs=generate_configurations(771,10000); print({lane.value:{dimension:len({getattr(c,dimension) for c in configs if c.lane is lane}) for dimension in spec.credited_dimensions} for lane,spec in CAPABILITY_MATRIX.items() if lane is not ExecutorLane.FAULT})"
```

The CORE_FLOW lane reports only one flow model, one volume, and two of twelve
regimes. MECHANICS, FRAGMENTED, ECOLOGY, and ALGORITHM similarly freeze or omit
values on their own credited axes even though aggregate declaration coverage is
green.

Root cause: the scheduler multiplies each lane's cell index by the six
scientific lanes before applying modular axis placement. That stride is not
coprime with two-, three-, four-, six-, eight-, or twelve-value axes, so those
values cannot cycle within one lane.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/auditlab/generator.py`
- `kirby2/audit/model_risk_lab.py`

Repair:

1. Advance the axis placement index by one for every new cell inside each lane.
2. Retain a deterministic lane offset so lanes need not start on identical
   configurations.
3. Prove every categorical credited value and all eight ecology agent counts
   occur inside their own lane; prove per-lane seed uniqueness and duration
   variation separately.
4. Preserve six equal scientific replicates, fault rotation, deterministic
   bytes, and the temporary 256-case legacy coverage gate.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.executors import CAPABILITY_MATRIX; from kirby2.auditlab.generator import AXES, generate_configurations; from kirby2.auditlab.models import ExecutorLane; configs=generate_configurations(771,4200); assert all({getattr(c,d) for c in configs if c.lane is lane and c.replicate_index == 0} == set(AXES[d]) for lane,spec in CAPABILITY_MATRIX.items() if lane is not ExecutorLane.FAULT for d in spec.credited_dimensions if d in AXES); print('ATR_06B_LANE_AXIS_COVERAGE PASS')"
git diff --check
```

Acceptance: no executor lane can receive declaration credit for an axis value
that its deterministic scheduler can never emit.

Commit: `Cycle audit axes within executor lanes`
