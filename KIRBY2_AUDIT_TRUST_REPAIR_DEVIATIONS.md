# Kirby2 Audit Trust Repair Deviations

This file records prerequisite repairs discovered while executing the canonical
sequence in `KIRBY2_AUDIT_TRUST_REPAIR_ROADMAP.md`.

## ATR-19A — Correct capped-arrival statistical inference

Discovered: 2026-08-27 during the first ATR-19 10,000-case persisted run at
`570875ac10ef9d9771e4ef309d10992792f00a13`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-lab --budget 10000 --seed 771 --save-failures
```

Observed failure:

```text
RUN seed=771 budget=10000 status=FAIL
STATISTIC unrealistic_event_explosion status=FAIL
STATISTICAL_STATUS FAIL
AGGREGATE_STATUS FAIL
PACKET id=audit-2fe994077b7bf5c9818750ee verification=PASS
```

The rejected packet and its `acceptance-20d989f2c7cd8c0b8d15` automated
precheck rejection remain immutable. The failing cell
`core_flow-00000012-cb082d3e` contains six Hawkes arrivals across six 8 ms
replicates. Its realized rate is 125 events per second, while the accepted
profile declares `max_total_intensity=120.0` events per simulated second.

Root cause: `HawkesConfig.max_total_intensity` is an instantaneous conditional-
intensity ceiling enforced at every thinning proposal. It is not a deterministic
ceiling on a realized finite-window count. Under the conservative homogeneous
Poisson dominating process, the cell has cap exposure `120 * 0.048 = 5.76`
events and `P[N >= 6]` is approximately `0.5150434175`; the observation is
not anomalous under the conservative cap-only envelope. One constituent 8 ms
replicate contains two arrivals, which also
proves that summing `ceil(cap * replicate_duration)` would invent a count rule
the production generator does not have. ATR-17 compared the random ratio
`sum(count) / sum(duration)` directly to the conditional-intensity cap and
therefore made a normal stochastic exceedance a hard failure.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/auditlab/executors/core_flow.py`
- `kirby2/auditlab/runner.py`
- `kirby2/auditlab/statistics.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Repair:

1. Keep realized events per simulated second and its ratio to the configured
   cap as descriptive evidence, never as a deterministic count invariant.
2. For each complete capped core-flow cell, aggregate cap exposure as
   `sum(cap_events_per_second * duration_seconds)` and compute the one-sided
   upper-tail probability of its realized count under the homogeneous Poisson
   process that dominates a counting process whose conditional intensity never
   exceeds that cap.
3. Predeclare a family-wise false-positive probability of one part per million
   in the immutable threshold manifest. Apply a Bonferroni correction across
   every valid capped cell and fail only when the corrected upper-tail
   probability is below that threshold.
4. Bump the threshold-manifest schema and name the Poisson-dominating,
   Bonferroni-corrected integer-count method because the decision semantics
   changed.
5. Record whether the core-flow executor applied an inter-arrival timing
   transform. The `C * T` envelope is eligible only for Hawkes cells with a
   path that has no post-model interval compression; production distribution
   profiles may compress sampled intervals and need a separately derived
   effective bound.
6. Require exact flow-model/cap pairing: Hawkes cells require a numeric cap and
   simple-flow cells must be uncapped. Treat mismatched, inconsistent, or
   malformed cap/count/duration/timing evidence as a failed statistical claim
   rather than silently discarding the cell. Reconcile projected caps and
   timing paths to the unique required source invariant, durations to both the
   generated configuration and typed metrics, and counts to typed metrics.
7. Add runtime-audit probes proving the original six-event observation passes,
   the corrected boundary is deterministic, and a controlled implausible count
   fails. Preserve the old failed packet; only a new post-repair run may
   supersede its evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.statistics import classify_capped_event_count; ordinary=classify_capped_event_count(event_count=6, simulated_duration_us=48000, configured_intensity_cap_eps=120.0, comparison_count=119); boundary=classify_capped_event_count(event_count=24, simulated_duration_us=48000, configured_intensity_cap_eps=120.0, comparison_count=119); implausible=classify_capped_event_count(event_count=25, simulated_duration_us=48000, configured_intensity_cap_eps=120.0, comparison_count=119); assert ordinary['classification']=='PASS' and boundary['classification']=='PASS' and implausible['classification']=='FAIL'; print('ATR_19A_CAPPED_ARRIVAL_INFERENCE PASS')"
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hawkes-stability
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab import run_audit_lab; result=run_audit_lab(budget=512, seed=771, persist=False, fresh_process_samples=2); check=next(item for item in result.statistics if item.name == 'unrealistic_event_explosion'); assert check.status == 'PASS', check.as_dict(); assert result.passed, result.render(); assert result.packet is None; print('ATR_19A_512_CASE_GATE PASS')"
git diff --check
```

Acceptance: the preserved reproducer is explained without relabeling or editing
it; ordinary stochastic count variation does not fail an intensity cap; a
predeclared family-wise upper-tail breach does fail; raw count-rate evidence
remains visible; the cap-only claim requires an unmodified Hawkes timing path;
model/cap mismatches and malformed capped-cell evidence cannot earn a pass; and
all runtime invariants survive.

Commit: `Correct capped event count statistics`

Handoff: return to a clean amendment commit and repeat ATR-19 from step 1.

## ATR-13A — Refuse the facsimile-era event-expansion statistic

Discovered: 2026-08-27 during the ATR-13 real-executor runner cutover at
`afcae0f5e672037dd8f28dfeeb1491fab0430c15`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
```

Observed failure:

```text
train_holdout_drift_overfit_seed_and_pathology_screens FAIL
statistical risk gates failed: ['unrealistic_event_explosion']
```

Root cause: the legacy screen divides one generic `event_count` by
`GeneratedConfiguration.duration_events`. The facsimile kernel used
`duration_events` as its synthetic command count, but the seven real executors
emit different native event families and advance by simulation time. After the
cutover the numerator and denominator no longer share a scientific meaning.
Preserving the threshold would turn a unit mismatch into either a false pass or
a false failure.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/auditlab/statistics.py`

Repair:

1. Return `NOT_EXERCISED` for the legacy event-expansion screen.
2. Record that real events per simulated second and the configured production
   cap are required before the claim can be evaluated.
3. Do not tune the old threshold or normalize heterogeneous lane events.
4. Leave the controlled implementation to ATR-17, which owns the statistical
   experiment design and makes an unexercised required statistic block the
   substantial audit.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.auditlab.statistics import statistical_checks; case={'configuration':{'duration_events':1,'strategy':'PROBE'},'metrics':{'event_count':999,'traded_volume':1,'trade_count':1,'price_displacement_ticks':0,'spread_ticks':1}}; result=next(item for item in statistical_checks((case,)) if item.name == 'unrealistic_event_explosion'); assert result.status == 'NOT_EXERCISED'; print('ATR_13A_EVENT_RATE_REFUSAL PASS')"
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: no audit labels heterogeneous native event counts divided by a
facsimile command-count field as an event-rate measurement; the screen remains
visibly unexercised until ATR-17 supplies common units and a production cap.

Commit: `Refuse invalid mixed-lane event expansion rate`

Handoff: resume ATR-13 from the preserved interrupted-slice changes.

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

## ATR-09A — Expose the asynchronous pending-event horizon

Discovered: 2026-08-27 during ATR-09 preflight at
`134c54fff53a2de21bbaab1dddebfd74388d1434`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.exchange import Side; from kirby2.latency import AsynchronousExecutionSession, get_latency_profile; session=AsynchronousExecutionSession(seed=3991301936962863233, profile=get_latency_profile('UNSTABLE')); session.advance_to(2_000); assert session.latest_display is None; session.request_limit(Side.BUY, 100, 99, order_id='ATR09-PROBE')"
```

Observed failure:

```text
RuntimeError: client cannot act before a market state is rendered
```

The generated `UNSTABLE` seed schedules the initial publication at 2,000 us,
the downlink at 22,000 us, and the render at 26,000 us. `STRESSED` can likewise
render after 2,000 us. The ATR-09 card's absolute 2,000 us player-command
assumption therefore conflicts with the production observability guard. The
session also exposes no read-only pending-message horizon, so a caller cannot
truthfully advance through the remaining asynchronous chain without reading
its private scheduler heap or guessing a fixed terminal time.

Root cause: the fixed ATR-09 command times omitted the initial observable-state
precondition while the asynchronous session intentionally applies the selected
latency profile to that initial state. The session's scheduler owns the exact
pending horizon but does not publish it.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/latency/engine.py`
- `kirby2/audit/latency.py`

Repair:

1. Expose a read-only `pending_event_horizon_us` property equal to the maximum
   currently queued simulation timestamp, or `None` when the queue is empty.
2. Prove that reading the horizon does not mutate the queue, RNG, events, or
   state, and that advancing to successive horizons drains chains which enqueue
   later work.
3. Preserve the production refusal to submit before an observable market state;
   do not bootstrap a client quote from hidden venue state.
4. In the resumed ATR-09 scenario, define `observable_ready_time_us` as the
   simulation time when the first real `UI_RENDERED_MARKET_STATE` arrives. Keep
   the mandated command offsets exactly 2,000, 6,000, and 8,000 or 10,000 us
   from that epoch, and record both the epoch and absolute command timestamps in
   native evidence.
5. Complete the scenario by repeatedly advancing to the public pending horizon
   until it is `None`, rather than guessing a wall-clock or fixed terminal time.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.latency import AsynchronousExecutionSession, get_latency_profile; session=AsynchronousExecutionSession(seed=3991301936962863233, profile=get_latency_profile('UNSTABLE')); seen=[]; exec('while session.pending_event_horizon_us is not None:\n seen.append(session.pending_event_horizon_us)\n session.advance_to(session.pending_event_horizon_us)'); assert session.latest_display is not None and session.pending_event_horizon_us is None; print('ATR_09A_PENDING_HORIZON PASS steps=%d ready_us=%d' % (len(seen), session.latest_display.render_time_us))"
git diff --check
```

Acceptance: every queued asynchronous chain can be advanced to true idleness
through a read-only public horizon; the horizon is `None` only when no message
remains; the initial quote still arrives through the configured profile; and
the existing latency runtime audit remains green.

Commit: `Expose latency pending event horizon`

Handoff: resume ATR-09 from a clean worktree and use the observable-ready epoch
for its exact relative command schedule.

## ATR-11A — Embed composed population definitions in ecology recordings

Discovered: 2026-08-27 during ATR-11 preflight at
`29672795911674705b6566efa7c8252f178c8199`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.agents import AgentEcology, AgentFamily, EcologyRecording, compose_population; definition=compose_population('liquidity_provision', {AgentFamily.NOISE_TRADER: 1}, duration_us=1000000); result=AgentEcology(definition, 42).run(); EcologyRecording.capture(result)"
```

Observed failure:

```text
ValueError: ecology recording population digest is stale or forged
```

ATR-11 requires `compose_population()` to create exactly one through eight
agents for each generated case, followed by native `EcologyRecording` capture
and exact replay. The recording stores only `population_id` and replay resolves
that ID through `get_population()`, whose three canonical definitions have
fixed compositions and a fixed four-second duration. A composed definition can
therefore neither be captured under a canonical ID nor reconstructed under a
new ID.

Root cause: ecology recording schema v1 identifies a mutable code-defined
fixture instead of carrying the exact immutable population definition that
produced the recorded event stream.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/agents/models.py`
- `kirby2/agents/replay.py`
- `kirby2/agents/__init__.py`
- `kirby2/audit/agent_ecology.py`

Repair:

1. Add exact `from_dict()` reconstruction for population definitions, agent
   specifications, bounds, policy parameters, and session transitions.
2. Introduce portable ecology recording schema v2, embedding the complete
   definition and binding it to `population_definition_sha256`.
3. Let `EcologyRecording.capture()` accept any validated safe population;
   replay v2 from the embedded definition rather than process-global fixtures.
4. Continue reading schema-v1 canonical recordings through `get_population()`
   without changing their field inventory or interpretation.
5. Prove a noncanonical composed definition round-trips and replays exactly,
   an embedded-definition mutation is refused, and a reconstructed v1 record
   still replays.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-agent-ecology
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.agents import AgentEcology, AgentFamily, EcologyRecording, compose_population, replay_agent_ecology; definition=compose_population('audit-portable', {AgentFamily.NOISE_TRADER: 1}, duration_us=1000000); recording=EcologyRecording.capture(AgentEcology(definition, 42).run()); loaded=EcologyRecording.from_dict(recording.as_dict()); assert loaded.definition == definition and replay_agent_ecology(loaded).passed; print('ATR_11A_PORTABLE_ECOLOGY_REPLAY PASS schema=%d' % recording.schema_version)"
git diff --check
```

Acceptance: every safe composed population has a self-contained exact native
recording; its embedded definition is digest-bound and schema-exact; legacy v1
canonical recordings remain readable and replayable.

Commit: `Embed ecology definitions in replay records`

Handoff: resume ATR-11 from this clean prerequisite commit and capture each
generated population through portable ecology recording schema v2.

## ATR-12A — Expose automated algorithm names on the strategy axis

Discovered: 2026-08-27 during ATR-12 preflight at
`4a877f18a5c79325b3d2b0615d4da085d595cb89`.

Reproducer:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.algorithms import AlgorithmName; from kirby2.auditlab.generator import AXES; expected={item.value for item in AlgorithmName if item is not AlgorithmName.MANUAL_REPLAY}; assert set(AXES['strategy']) == expected"
```

Observed values:

```text
PASSIVE AGGRESSIVE ADAPTIVE OBSERVE
```

ATR-12 requires each credited strategy value to be one real automated
`AlgorithmName` other than `MANUAL_REPLAY`. The four legacy labels cannot map
bijectively to the nine production algorithms, so retaining them would either
omit algorithms or let one configured value silently select different policy
implementations.

Root cause: the original generative kernel used four behavioral labels before
the typed executor architecture existed. The later roadmap made the strategy
dimension an algorithm-identity axis, but the generated-axis inventory was not
updated with that contract.

Owned files:

- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`
- `kirby2/auditlab/generator.py`

Repair:

1. Define the strategy axis from every canonical `AlgorithmName` except
   `MANUAL_REPLAY`, preserving enum order.
2. Import the enum from its model module so generation does not depend on an
   audit-only mapping or instantiate benchmark infrastructure.
3. Keep the generated-configuration schema unchanged; strategy remains a
   serialized string whose value now has exact production identity.
4. Prove the deterministic per-lane scheduler emits all nine names and that the
   existing model-risk audit remains green before resuming ATR-12.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -c "from kirby2.algorithms import AlgorithmName; from kirby2.auditlab.generator import AXES, generate_configurations; from kirby2.auditlab.models import ExecutorLane; expected=tuple(item.value for item in AlgorithmName if item is not AlgorithmName.MANUAL_REPLAY); observed={item.strategy for item in generate_configurations(771,4200) if item.lane is ExecutorLane.ALGORITHM and item.replicate_index == 0}; assert AXES['strategy'] == expected and observed == set(expected); print('ATR_12A_AUTOMATED_STRATEGY_AXIS PASS values=%d' % len(expected))"
git diff --check
```

Acceptance: the strategy axis contains exactly the nine automated production
algorithm identities, the ALGORITHM lane deterministically reaches all nine,
`MANUAL_REPLAY` is absent, and existing audit generation remains deterministic.

Commit: `Expose automated algorithm strategy axis`

Handoff: resume ATR-12 from the clean prerequisite commit and map each
configured strategy directly through `default_algorithm_manifest()`.
