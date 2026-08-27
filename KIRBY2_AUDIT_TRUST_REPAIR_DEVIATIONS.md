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

## ATR-08A — Preserve managed-order expiration classification

Discovered: 2026-08-27 while executing real lifecycle reconstruction for
ATR-08 at `1621964f1ca02f0ee7c5b951ea5fff937fcdea78`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.audit.market_mechanics import audit_market_mechanics; cases=audit_market_mechanics(); case=next(item for item in cases if item.name == 'expiration_classification_survives_later_synchronization'); assert case.passed, case.as_dict(); print('ATR_08A_EXPIRATION_CLASSIFICATION PASS')"
```

Before repair, an IOC order partially fills and correctly becomes `EXPIRED`
with 30 expired shares. Submitting a later continuous order calls
`_sync_continuous_orders()`, which changes the first order to `CANCELLED`, moves
the 30 shares from `expired_quantity` to `cancelled_quantity`, and destroys the
real lifecycle classification. DAY, SESSION, GOOD_UNTIL_TIME, and market-order
remainder expiration use the same synchronization path.

Root cause: the core FIFO ledger represents both cancellation and policy expiry
in `cancelled_quantity`, while `ManagedOrder` deliberately separates
`cancelled_quantity` and `expired_quantity`. Every synchronization discarded the
managed classification and recopied the undifferentiated core value.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/exchange/mechanics_engine.py`
- `kirby2/audit/market_mechanics.py`

Repair:

1. Preserve the already classified managed expired quantity during subsequent
   core-ledger synchronization and derive only the residual cancelled quantity.
2. Keep an expired managed order terminally `EXPIRED` after later submissions,
   cancellations, replacements, session transitions, and replay.
3. Add runtime probes for IOC, SESSION, GOOD_UNTIL_TIME, and DAY expiration,
   each followed by a later synchronization-triggering command.
4. Reconcile the separated managed quantities back to the core ledger total and
   reject an impossible over-classification.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.audit.market_mechanics import audit_market_mechanics; cases=audit_market_mechanics(); case=next(item for item in cases if item.name == 'expiration_classification_survives_later_synchronization'); assert case.passed, case.as_dict(); print('ATR_08A_EXPIRATION_CLASSIFICATION PASS')"
git diff --check
```

Acceptance: every probed policy expiry remains event-backed `EXPIRED` with its
expired quantity unchanged after a later synchronization; cancelled plus
expired quantity still equals the core cancelled quantity; native mechanics
replay and the complete model-risk audit remain green.

Commit: `Preserve mechanics expiry classification`

Handoff: restore the preserved ATR-08 work and resume the real mechanics
executor from this clean prerequisite commit.

## ATR-08B — Implement genuine GTC mechanics

Discovered: 2026-08-27 during ATR-08 instruction-inventory review at
`3d8d24cc0c48d2c1a518d918e1092427dfd54449`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.exchange import OrderInstruction; assert 'GTC' in {item.value for item in OrderInstruction}; print('ATR_08B_GTC_INVENTORY PASS')"
```

The canonical ATR-08 card requires a real GTC lifecycle. Production exposes
`SESSION`, which expires when continuous trading ends, and
`GOOD_UNTIL_TIME`, which expires at a simulation timestamp. Neither is GTC, and
labeling `SESSION` as a GTC alias would make the audit evidence false.

Root cause: Work Order 24 deliberately implemented its original instruction
inventory, while the later trust-repair roadmap added GTC to the literal
mechanics mapping without first adding the production instruction.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/exchange/mechanics_models.py`
- `kirby2/exchange/MECHANICS.md`
- `kirby2/audit/market_mechanics.py`

Repair:

1. Add canonical `OrderInstruction.GTC` as a time-in-force instruction.
2. Define GTC to survive halts, closing, postclose, closed, and the following
   open while retaining its FIFO resting sequence and conserved quantity.
3. Keep GTC live until a real fill or explicit cancellation; do not reuse DAY,
   SESSION, or GOOD_UNTIL_TIME expiry behavior.
4. Add a runtime scenario that crosses a complete session boundary, verifies
   the same active core order and priority, then explicitly cancels it and
   verifies exact replay-compatible event evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.exchange import OrderInstruction; assert 'GTC' in {item.value for item in OrderInstruction}; print('ATR_08B_GTC_INVENTORY PASS')"
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.audit.market_mechanics import audit_market_mechanics; case=next(item for item in audit_market_mechanics() if item.name == 'gtc_persists_until_explicit_cancel'); assert case.passed, case.as_dict(); print('ATR_08B_GTC_LIFECYCLE PASS')"
git diff --check
```

Acceptance: GTC is a serialized canonical instruction, remains active with the
same FIFO priority across the full session/day boundary, closes only on the
explicit cancellation in the runtime probe, and all existing mechanics and
model-risk audits remain green.

Commit: `Implement genuine GTC mechanics`

Handoff: restore the preserved ATR-08 work, replace its false SESSION-to-GTC
alias with the genuine instruction, and resume the real mechanics executor.
