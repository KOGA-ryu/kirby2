# Kirby2 Audit Trust Repair Roadmap

Status: canonical repair sequence

Baseline: `a71c3f2e98097bda0f733f0c7c0bd83ba390eec5` (`Add generative model-risk laboratory`)

Scope boundary: repair defects found in the post-Work-Order-30 review; do not begin an expansion work order

Execution rhythm: one slice, its runtime evidence, one commit, a clean worktree, then the next slice

## 1. Goal contract

Repair Kirby2's evidence, state-ownership, generated-audit, replay, minimization,
statistics, and provenance boundaries so that a green audit result means the real
subsystems were exercised and the preserved evidence can support that claim.

The sequence is complete only when every slice `ATR-01` through `ATR-19` has its
own accepted commit and the final 10,000-case run has produced a verified v2
packet. A statistically clean result is not human behavioral acceptance; manual
acceptance remains a separate immutable record.

This roadmap does not authorize:

- Work Order 31 or any other feature expansion;
- UI, networking, brokerage, historical-data, or database expansion;
- conventional `pytest` infrastructure, mocks, fixtures, or coverage tooling;
- weakening an invariant, detector, threshold, or replay comparison to obtain a
  green result;
- rewriting or deleting an existing immutable packet or acceptance record;
- pushing commits to a remote.

## 2. Review defects this sequence closes

| ID | Confirmed defect | Current source boundary | Closing slice(s) |
|---|---|---|---|
| D01 | Artifact names can escape an audit packet through absolute paths, `..`, or symlinks. | `kirby2/auditlab/store.py` | ATR-01 |
| D02 | Packet identity hashes run identity but not the artifact inventory or artifact bytes. | `kirby2/auditlab/store.py`, `kirby2/auditlab/runner.py` | ATR-02 |
| D03 | Frozen event records retain mutable dictionaries and return aliases from `as_dict()`. | `kirby2/session/events.py` and subsystem event models | ATR-03, ATR-04 |
| D04 | `OrderBook` retains a caller-owned mutable `Order` and exposes mutable orders, queues, and price levels. | `kirby2/exchange/book.py`, `kirby2/exchange/models.py` | ATR-05 |
| D05 | Generated cases label simplified `OrderBook` behavior as Hawkes, auction, hidden, latency, multi-venue, agent, strategy, and objective coverage. | `kirby2/auditlab/kernel.py` | ATR-06 through ATR-13 |
| D06 | Several structural checks are constants or reconcile two projections derived by the same helper. | `kirby2/auditlab/kernel.py` | ATR-06 through ATR-13 |
| D07 | Fault detectors manufacture their own invalid constants and then return the expected code. | `kirby2/auditlab/faults.py` | ATR-14 |
| D08 | “Loaded replay” serializes a configuration and regenerates the case instead of loading and replaying a command tape. | `kirby2/auditlab/runner.py` | ATR-15 |
| D09 | Expected fault detections are minimized as defects, while a replay mismatch cannot be preserved by the current minimizer. | `kirby2/auditlab/minimizer.py`, `kirby2/audit/model_risk_lab.py` | ATR-16 |
| D10 | Train/holdout, drift, strategy-overfit, seed-sensitivity, and permanent-cross checks are not controlled comparisons. | `kirby2/auditlab/generator.py`, `kirby2/auditlab/statistics.py` | ATR-17 |
| D11 | Provenance omits most executed subsystem source and hashes dirty path names without dirty bytes. | `kirby2/auditlab/runner.py` | ATR-18 |
| D12 | One aggregate `PASS` conflates runtime structure, statistical warnings, unexercised capabilities, and pending human review. | `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py` | ATR-18 |

## 3. Mandatory slice protocol

Run this protocol literally for every repair slice.

### 3.1 Preflight

1. Run `git status --short` and `git rev-parse HEAD`.
2. The worktree must be clean. If it is not, classify every path before editing:
   changes from the just-finished slice must be committed; unrelated user changes
   are a stop condition and must not be folded into a repair commit.
3. Read this card, the files in its ownership list, and the preceding slice's
   handoff. Do not implement a later card.
4. Record the starting commit in the progress update.

### 3.2 Implementation

1. Modify only the owned files and the minimum call sites required by the card.
2. Preserve integer ticks, explicitly owned RNG state, simulation time, FIFO,
   append-only evidence, and refusal semantics.
3. Add acceptance evidence to the existing runtime-audit layer. Do not add a
   conventional test directory or test runner.
4. A detected expected fault is an adversarial observation, not a product defect.
5. A capability not executed by a real subsystem is `NOT_EXERCISED`; it is never
   translated to `PASS`.

### 3.3 Verification

1. Run `git diff --check`.
2. Run every command printed in the card's `Required evidence` section.
3. Run every existing subsystem audit named by the card.
4. If a command fails, repair within the same slice and rerun it. Do not commit a
   known partial implementation merely to advance the sequence.
5. Capture exit status, case counts, failure counts, replay status, and packet or
   digest identity when the command prints them.

Use `PYTHONDONTWRITEBYTECODE=1` for Python acceptance commands. Runtime audits,
adversarial probes, and deterministic CLI scenarios are allowed; a new pytest
suite is not.

### 3.4 Commit gate

1. Inspect `git diff --stat` and `git diff --name-only`.
2. Confirm the diff contains one repair idea and no generated `.kirby2` evidence.
3. Stage only the slice files.
4. Commit with the exact subject specified by the card.
5. Run `git show --stat --oneline --summary HEAD`.
6. Run `git status --short`; it must print nothing.
7. Only then start the next card.

### 3.5 Unforeseen-event protocol

Do not improvise around a surprise or silently reduce scope.

- If it is the same root cause and fits the current ownership boundary, add the
  reproducer to the current runtime audit, repair it in the current slice, and
  mention it in that commit's evidence.
- If it is a prerequisite defect in a different subsystem, append an `ATR-xxA`
  amendment to `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`. The amendment must give
  the reproducer, root cause, exact files, exact acceptance commands, and commit
  subject. Execute and commit that amendment before resuming the interrupted
  slice.
- If the surprise is an environment or dependency problem, preserve the command
  and exact error. Retry only after a concrete correction; do not replace the
  gate with an easier command.
- If unrelated user work overlaps a required file, stop and ask for ownership
  direction. Never reset, discard, or absorb that work.
- If a persisted artifact reveals a defect, preserve the artifact. Produce a new
  superseding record after the repair; never edit the old evidence.

## 4. Final audit architecture (fixed design)

The repaired generated sweep uses seven executor lanes. A generated
configuration may declare the full experiment cell, but coverage is credited
only for values listed in a real executor's `ExerciseRecord`.

| Executor lane | Real implementation | Credited dimensions |
|---|---|---|
| `CORE_FLOW` | `ScenarioDefinition` + `RegimeOrderFlow` + real simple/Hawkes flow model | seed, duration, flow model, regime, volume, liquidity |
| `MECHANICS` | `MarketMechanicsEngine` | session phase, order instruction/lifecycle, auction state |
| `LATENCY` | `AsynchronousExecutionSession` | latency profile and terminal race outcome |
| `FRAGMENTED` | `HiddenLiquidityVenue` + `MarketCoordinator` | hidden-liquidity mode and venue count |
| `ECOLOGY` | `AgentEcology` | participant population and agent count |
| `ALGORITHM` | the same single-cell runtime used by the execution benchmark | execution algorithm and objective |
| `FAULT` | production subsystem adapters selected by fault kind | one explicit injected fault and its raw detector observation |

The generator maintains an independent case index per lane. Scientific lanes
emit six seed replicates for one non-seed cell: replicates 0–2 are `TRAIN` and
3–5 are `HOLDOUT`. The `FAULT` lane rotates through every `FaultKind`. Lane
assignment, cell identity, partition, and seeds are derived only from the master
seed and integer indices. Pairwise claims are permitted only within a lane that
actually composes those dimensions.

Every executor returns:

- the original generated configuration and stable lane name;
- a native or audit-owned serialized command recording;
- canonical event and final-state projections;
- metrics with units in their names;
- `ExerciseRecord` values proving which configured capabilities ran;
- `CheckResult` values with `PASS`, `FAIL`, or `NOT_EXERCISED`;
- unexpected `FailureObservation` values separate from expected fault evidence;
- an observable projection that is checked against a forbidden-field inventory.

Universal checks are not forced onto incapable lanes. A check is required only
where its capability applies; a required `NOT_EXERCISED` result fails the case.
The coverage gate separately requires every configured value and every required
check to be exercised by at least one real lane.

## 5. Slice index

| Slice | Deliverable | Exact commit subject |
|---|---|---|
| ATR-00 | This canonical execution contract | `Document audit trust repair sequence` |
| ATR-01 | Packet-path containment | `Contain audit packet artifact paths` |
| ATR-02 | Artifact-bound packet schema v2 | `Bind audit packet identity to artifacts` |
| ATR-03 | Immutable core JSON/event boundary | `Freeze core replay payloads` |
| ATR-04 | Immutable subsystem evidence records | `Freeze subsystem evidence payloads` |
| ATR-05 | Exchange-owned order state and read-only views | `Enforce exchange state ownership` |
| ATR-06 | Typed truthful audit contracts and lane scheduler | `Define truthful audit execution contracts` |
| ATR-07 | Real core-flow executor | `Exercise real synthetic flow in audit lab` |
| ATR-08 | Real market-mechanics executor | `Exercise real market mechanics in audit lab` |
| ATR-09 | Real asynchronous-latency executor | `Exercise real asynchronous latency in audit lab` |
| ATR-10 | Real hidden/fragmented-market executor | `Exercise real fragmented venues in audit lab` |
| ATR-11 | Real agent-ecology executor | `Exercise real agent ecology in audit lab` |
| ATR-12 | Real execution-algorithm executor | `Exercise real execution algorithms in audit lab` |
| ATR-13 | Runner cutover and computed structural checks | `Route generated audits through real systems` |
| ATR-14 | Production fault adapters and independent oracle | `Replace fabricated audit fault detectors` |
| ATR-15 | Serialized command-tape replay and process determinism | `Replay serialized generated recordings` |
| ATR-16 | Failure classification and predicate-aware minimization | `Minimize only reproducible audit defects` |
| ATR-17 | Matched statistical experiment and pathology screens | `Build controlled audit risk statistics` |
| ATR-18 | Complete provenance and truthful aggregate statuses | `Bind audit provenance and gate statuses` |
| ATR-19 | 10,000-case closeout and preserved evidence | `Close audit trust repair sequence` |

## 6. Detailed slice cards

### ATR-00 — Canonical repair contract

Objective: preserve the review findings, fixed architectural decisions, execution
order, and commit gates before touching production behavior.

Owned files:

- create `KIRBY2_AUDIT_TRUST_REPAIR_ROADMAP.md` only.

Required evidence:

```text
git diff --check
rg -n '^### ATR-[0-9]{2} ' KIRBY2_AUDIT_TRUST_REPAIR_ROADMAP.md
git status --short
```

Acceptance:

- cards `ATR-00` through `ATR-19` occur once and in increasing order;
- each repair card owns files, fixes a named defect, supplies literal evidence
  commands, has acceptance criteria, and names its commit;
- no production Python file changes in this slice.

Commit: `Document audit trust repair sequence`

Handoff: start ATR-01 only from a clean commit.

### ATR-01 — Contain packet artifact paths

Objective: make every artifact name a validated packet-relative POSIX path and
fail before any artifact write when one name is invalid.

Owned files:

- `kirby2/auditlab/store.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. Add one name validator used by both `record()` and `verify()`.
2. Require a nonempty `str` whose canonical `PurePosixPath.as_posix()` equals the
   input. Reject absolute paths, empty/`.`/`..` segments, NUL, backslashes,
   Windows drives/UNC forms, and the reserved `manifest.json` name.
3. Validate the complete sorted name inventory before creating a staging packet
   or writing any content.
4. Resolve each destination and require `destination.is_relative_to(packet_root)`.
5. Verification rejects every symlink in the packet tree, validates manifest
   names before joining them, and requires every resolved artifact to remain in
   the packet directory.
6. Preserve nested safe names such as `failures/example.json`.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The runtime audit must attempt `../escape`, an absolute path, a Windows drive,
backslash traversal, a dot segment, `manifest.json`, and a symlink escape. It
must prove rejection, prove no outside file exists, prove staging has no partial
packet, and then record and verify one safe nested packet.

Acceptance: all adversarial names fail before write; safe packets and ledger
verification still pass.

Commit: `Contain audit packet artifact paths`

### ATR-02 — Bind packet identity to artifact bytes

Objective: make two different artifact sets incapable of sharing a new packet
identity merely because their run metadata matches.

Owned files:

- `kirby2/auditlab/store.py`
- `kirby2/auditlab/runner.py`
- `kirby2/auditlab/models.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. Introduce packet schema v2 without rewriting schema-v1 packets.
2. Encode every artifact as UTF-8 once in memory and build the sorted inventory
   `{name: {bytes, sha256}}` before computing an ID.
3. Derive v2 `packet_id` from canonical
   `{schema_version: 2, identity, artifacts: inventory}` and retain the existing
   `audit-` prefix and 24 hexadecimal-character suffix.
4. Write only v2 packets. `verify()` remains read-compatible with v1 and labels
   it `IDENTITY_ONLY_LEGACY`; v2 is `IDENTITY_AND_ARTIFACTS`.
5. If a target ID exists, require both a passing verification and exact equality
   with the candidate manifest. A mismatch is an immutable collision and fails
   closed.
6. Remove the provisional-ID cycle from `report.txt`. The stored report says
   `PACKET id=SEE_MANIFEST`; the post-record CLI render may print the actual ID.
7. Include packet schema and identity scope in `PacketRecord` and ledger checks.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The audit must prove: identical identity plus different artifact bytes produces
different IDs; identical identity plus identical bytes is idempotent; byte
tampering fails; a locally constructed v1 packet verifies as legacy; v1 is never
rewritten; v2 ledger inventory reconciles.

Acceptance: packet identity is artifact-bound and the human report contains no
guessed ID.

Commit: `Bind audit packet identity to artifacts`

### ATR-03 — Freeze core replay payloads

Objective: prevent caller mutation or `as_dict()` mutation from rewriting the
core event journal or session replay evidence.

Owned files:

- create `kirby2/immutable.py`
- `kirby2/session/events.py`
- `kirby2/session/records.py`
- minimum type/serialization call sites in `kirby2/session/live.py` and
  `kirby2/session/replay.py`
- `kirby2/audit/model_risk_lab.py`

Implementation decisions:

1. Implement `freeze_json()` and `thaw_json()` once. Recursively freeze mappings
   into sorted `MappingProxyType` values, sequences into tuples, and accept only
   JSON scalars with finite floats. Reject non-string mapping keys and unsupported
   objects.
2. Every public `as_dict()` returns a fully detached mutable JSON tree produced
   by `thaw_json()`; no stored mapping or nested sequence is returned by alias.
3. Freeze constructor inputs in `SimulationEvent`, `InputRecord`,
   `MarketStateRecord`, and `TimelineRecord` using `object.__setattr__`.
4. Keep JSON wire shapes, field names, event ordering, and canonical digests
   unchanged.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 demo --seed 42
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-strategy-time
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The model-risk audit must mutate the original nested payload after emission,
attempt direct nested event mutation, and mutate a returned `as_dict()` tree.
The first and third operations must leave canonical journal bytes unchanged; the
second must raise `TypeError`.

Acceptance: the core event digest is append-only through all public references.

Commit: `Freeze core replay payloads`

### ATR-04 — Freeze subsystem evidence payloads

Objective: apply the same ownership boundary to every dict-bearing event or
replay-evidence record used by the systems that the audit lab will execute.

Owned files:

- `kirby2/simulation/flow.py` (`FlowEvent` command and diagnostic)
- `kirby2/exchange/mechanics_models.py` (`MechanicsEvent`)
- `kirby2/latency/models.py` (`LatencyEvent`)
- `kirby2/observability/models.py` (`ObservableEvent`, `TruthEvent`)
- `kirby2/observability/venue.py` internal queued event payloads
- `kirby2/multivenue/models.py` (`CoordinatorEvent`)
- `kirby2/agents/models.py` (`PublicEcologyEvent`)
- `kirby2/counterfactual/models.py` timeline/divergence/outcome mappings
- `kirby2/auditlab/models.py` fault, case, statistic, and acceptance mappings
- minimum construction/recursive-inspection call sites for those records
- `kirby2/audit/model_risk_lab.py`

Implementation decisions:

1. Use `freeze_json()`/`thaw_json()`; do not create subsystem-specific copy
   helpers.
2. Change public annotations from mutable `dict` to `Mapping` where storage is
   frozen. Change recursive inspectors from `isinstance(value, dict)` to
   `Mapping` and support tuple sequences.
3. Keep all `as_dict()` and recording outputs ordinary JSON dictionaries/lists.
4. Include nested payloads; a shallow mapping proxy is insufficient.
5. Do not freeze live mutable engine state such as `AsyncOrder`; this card owns
   immutable evidence records, not engine internals.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-counterfactuals
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-agent-ecology
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: representative nested-mutation probes for every named event family
fail without changing event, recording, replay, or result digests.

Commit: `Freeze subsystem evidence payloads`

### ATR-05 — Enforce exchange state ownership

Objective: ensure only `OrderBook` can mutate lifecycle quantities, status, FIFO
queues, and price-level membership after a command is submitted.

Owned files:

- `kirby2/exchange/models.py`
- `kirby2/exchange/book.py`
- `kirby2/audit/invariants.py`
- read-only call-site migrations found by `.active_orders`, `.all_orders`,
  `.bids`, and `.asks`
- `kirby2/audit/model_risk_lab.py`

Implementation decisions:

1. Add frozen `OrderView` and `PriceLevelView` value objects. A level view owns a
   tuple of order views; all quantities are scalar snapshots.
2. `OrderBook.process()` validates that an incoming `Order` is pristine, clones
   its command fields into an exchange-owned mutable order, and never stores the
   caller object. `replace()` applies the same rule to the replacement.
3. `bids`, `asks`, `active_orders`, and `all_orders` return read-only mappings of
   frozen views. `trades` and `fills` return tuples.
4. Internal matching, reduction, cancellation, and invariants use private mutable
   indexes directly. External engines query views by ID; they never receive an
   internal mutable order.
5. Preserve public read fields, FIFO order, state digests, command semantics, and
   event bytes. Callers that relied on mutation must query the book view instead.
6. Reject a caller-supplied order whose filled/cancelled/remaining/status fields
   were forged before submission.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 demo --seed 42
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The adversarial audit must mutate the original submitted order, attempt to
mutate an order view, attempt to clear a returned price-level queue, and mutate a
returned active-order mapping. Book state and event bytes must remain unchanged,
then a genuine command must still change both through normal events.

Acceptance: no public reference can cause an unjournaled exchange-state change.

Commit: `Enforce exchange state ownership`

### ATR-06 — Define truthful audit execution contracts

Objective: establish typed case, exercise, check, failure, lane, partition, and
recording contracts before replacing the fast facsimile kernel.

Owned files:

- `kirby2/auditlab/models.py`
- `kirby2/auditlab/generator.py`
- create `kirby2/auditlab/executors/__init__.py`
- create `kirby2/auditlab/executors/base.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. Add enums `ExecutorLane`, `ExperimentPartition`, `ExerciseStatus`,
   `CheckStatus`, `FailureKind`, and `AutomatedStatus`.
2. Add immutable `ExerciseRecord`, `CheckResult`, `FailureObservation`,
   `CaseRecording`, and `GeneratedCaseResult` contracts. All JSON payloads use
   the immutable boundary from ATR-03.
3. A case passes only when it has no unexpected failures, no `FAIL` checks, and
   no required `NOT_EXERCISED` checks. Expected fault detection is stored outside
   `FailureObservation`.
4. Bump generated-configuration schema to v2. Add `lane`, `cell_id`,
   `replicate_index`, and `partition`. Reject unknown/missing fields.
5. Implement the seven-lane independent-index scheduler described in section 4.
   Scientific cells have six replicates; fault cases rotate the ten fault kinds.
6. Define one capability matrix in `executors/base.py`; coverage code uses
   executor-returned exercise records, never declaration alone.
7. Provide a registry protocol with `execute(configuration)` and
   `replay(recording)`; do not register a placeholder executor.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The runtime audit must prove deterministic schedule bytes, six equal non-seed
replicates per complete cell, disjoint train/holdout seeds, complete fault
rotation, schema refusal, and that declared-but-unreported capabilities receive
no coverage credit.

Acceptance: contracts make unsupported behavior representable only as
`NOT_EXERCISED`, never as a Boolean success.

Commit: `Define truthful audit execution contracts`

### ATR-07 — Execute real simple and Hawkes flow

Objective: replace the synthetic timing/side facsimiles for flow, regime, volume,
and liquidity with the production `RegimeOrderFlow` stack.

Owned files:

- create `kirby2/auditlab/executors/core_flow.py`
- `kirby2/auditlab/executors/__init__.py`
- `kirby2/simulation/comparison.py` only to expose the existing real
  simple/Hawkes model factory instead of duplicating private construction
- `kirby2/audit/model_risk_lab.py`

Literal configuration mapping:

- scenario definition: `get_scenario_definition(configuration.regime.lower())`;
- volume: `VolumePreset(configuration.volume)`;
- liquidity: `LiquidityPreset(configuration.liquidity)`;
- simple flow: `SimpleFlowModel()`;
- Hawkes flow: the accepted profile selected by
  `accepted_hawkes_profile_for_regime()` and the same regime shaping currently
  used by `compare_flow_models()`;
- runtime: `create_market_engine(...)`, then `advance_to(duration_us)`;
- command tape: emitted real `FlowEvent` values, including skipped commands and
  exchange sequence spans.

Computed checks:

- quantity conservation and no negative active quantity;
- FIFO/book ordering and non-crossed venue book;
- contiguous flow and exchange sequences;
- player position and cash using independent fill and event projectors;
- real Hawkes stability certificate and event-rate cap;
- observable projection contains no forbidden truth-only fields.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 compare-flow --scenario balanced --seed 771
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hawkes-stability
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: the executor reports its actual flow-model replay config and only
credits the six `CORE_FLOW` dimensions.

Commit: `Exercise real synthetic flow in audit lab`

### ATR-08 — Execute real market mechanics

Objective: exercise session states, advanced instructions/lifecycles, and
auctions through `MarketMechanicsEngine` rather than ordinary limit orders.

Owned files:

- create `kirby2/auditlab/executors/mechanics.py`
- `kirby2/auditlab/executors/__init__.py`
- minimum reusable builder extraction from
  `kirby2/exchange/mechanics_scenarios.py`
- `kirby2/audit/model_risk_lab.py`

Literal mapping:

- session state uses every canonical `SessionState` value through
  `transition_session()`;
- instruction axis uses real `LIMIT`, `MARKET`, `IOC`, `FOK`, `POST_ONLY`,
  `DAY`, `GTC`, `GTD`, priority-preserving reduction, and cancel+new replacement;
- auction `OPENING`, `REOPENING`, and `CLOSING` transitions to the corresponding
  auction state, submits crossing and non-crossing interest, obtains an
  indication, and calls `uncross_auction()`; `NONE` remains continuous;
- an operation rejected by the selected phase is credited only when the real
  rejection event and reason match engine rules.

Computed checks:

- order lifecycle transitions reconstructed from mechanics events;
- core exchange conservation/order-book checks;
- auction matched quantity equals buy fills, sell fills, and uncross quantity;
- clearing allocation does not exceed any original order and honors the native
  allocation result;
- indication versus actual differences are explained by intervening commands or
  self-trade prevention, never ignored;
- session timestamps and event sequences are monotonic.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: no auction or branch check is a constant; each configured mechanics
value has an event-backed `ExerciseRecord`.

Commit: `Exercise real market mechanics in audit lab`

### ATR-09 — Execute real asynchronous latency

Objective: exercise every latency profile and terminal cancel/fill ordering in
the actual discrete-event lifecycle.

Owned files:

- create `kirby2/auditlab/executors/latency.py`
- `kirby2/auditlab/executors/__init__.py`
- `kirby2/latency/scenarios.py` to parameterize the existing cancel-race builder
  by canonical latency profile without duplicating it
- `kirby2/audit/model_risk_lab.py`

Literal mapping:

- construct `AsynchronousExecutionSession(seed, get_latency_profile(value))`;
- issue one player limit at 2,000 us, one cancel at 6,000 us, and an external
  aggressive order at 8,000 or 10,000 us according to the case seed;
- advance beyond all queued acknowledgements/fills using the session's pending
  event horizon, not wall time;
- capture the native `LatencyRecording` and raw `LatencyEvent` sequence.

Computed checks:

- every timestamp is nonnegative and event-causally ordered;
- async order transitions are legal and conserve quantity;
- a fill after cancel request is allowed only when the venue fill wins before
  the venue cancel; a fill after terminal cancel acknowledgement is a failure;
- `cancel_race_outcome` reconciles with event arrival order;
- latency metrics reconcile to component timestamps and configured profile
  draws.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: all latency profiles and both race outcomes are backed by native
recordings and computed checks.

Commit: `Exercise real asynchronous latency in audit lab`

### ATR-10 — Execute real hidden and fragmented venues

Objective: exercise hidden-liquidity semantics and one-to-four venue routing
through the production observability and coordinator layers.

Owned files:

- create `kirby2/auditlab/executors/fragmented.py`
- `kirby2/auditlab/executors/__init__.py`
- minimum reusable builder extraction from `kirby2/observability/scenarios.py`
  and `kirby2/multivenue/scenarios.py`
- `kirby2/audit/model_risk_lab.py`

Literal mapping:

- `NONE`: displayed limit liquidity;
- `ICEBERG`: `IcebergDefinition` with displayed clip, reserve, refresh quantity,
  and canonical refresh policy;
- `HIDDEN_MIDPOINT`: two-sided displayed reference plus midpoint-hidden interest;
- venue count: construct exactly `N` deterministic `VenueConfig` values named
  `AUDIT-V01` through `AUDIT-V04`, seed both sides, subscribe to displayed depth,
  submit one route, execute bounded external flow, cancel remaining routes, and
  complete the session;
- select latency profiles and fees by venue index from fixed canonical tables;
  do not derive routing from hidden truth.

Computed checks:

- every venue's actual hidden-liquidity and core-book invariants;
- global player position equals the sum of venue fills;
- route-leg original quantity equals filled, cancelled/rejected, and remaining;
- consolidated quote is formed only from received observable venue data;
- observable payload recursive keys exclude reserve, hidden quantity, priority,
  maker identity, future, agent intent, and latent value;
- crossed-composite episodes are recorded with start/end/duration for ATR-17.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: hidden mode and venue count receive credit only from the real
observable/coordinator stack; no hidden field appears in the player projection.

Commit: `Exercise real fragmented venues in audit lab`

### ATR-11 — Execute real agent ecology

Objective: make agent-population and count coverage arise from actual bounded
agents with owned RNG streams and information boundaries.

Owned files:

- create `kirby2/auditlab/executors/ecology.py`
- `kirby2/auditlab/executors/__init__.py`
- `kirby2/agents/populations.py` only for a reusable deterministic bounded
  composition helper
- `kirby2/audit/model_risk_lab.py`

Literal population templates, cycled until exactly `agent_count` agents exist:

- `liquidity_provision`: noise trader, passive maker, liquidity withdrawer,
  scheduled metaorder;
- `momentum_ecology`: momentum trader, inventory-sensitive maker, noise trader,
  mean-reversion trader;
- `liquidation_ecology`: distressed liquidator, liquidity withdrawer,
  mean-reversion trader, noise trader.

Use `compose_population()` with deterministic family counts, unique IDs, the
case duration, and the case seed. Run `AgentEcology`, capture
`EcologyRecording`, and retain distinct public and truth digests.

Computed checks:

- per-agent quantity, order-rate, working-quantity, inventory, lifetime, and
  information-set bounds;
- agent inventory reconciles to mechanics fills;
- public events omit agent ID, intent, latent value, reserve price, and future
  decisions;
- identical population/seed is byte-identical and different owned seeds do not
  all collapse to one state;
- event and decision times are monotonic.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-agent-ecology
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: population and agent-count coverage is backed by the real ecology,
not by assigning exchange-order owners.

Commit: `Exercise real agent ecology in audit lab`

### ATR-12 — Execute real algorithms and objectives

Objective: make strategy/objective coverage use the same client observation,
routing, risk, fork, metric, and replay runtime as Kirby2's algorithm benchmark.

Owned files:

- create `kirby2/auditlab/executors/algorithms.py`
- `kirby2/auditlab/executors/__init__.py`
- `kirby2/algorithms/benchmark.py` to extract a public, non-persisting
  `run_execution_cell()` used by both the benchmark and audit lab
- `kirby2/algorithms/__init__.py`
- `kirby2/audit/model_risk_lab.py`

Literal mapping:

- strategy axis is every automated `AlgorithmName` except `MANUAL_REPLAY`;
- manifest comes from `default_algorithm_manifest()` without audit-only policy;
- `ACQUIRE` is a buy objective and `LIQUIDATE` a sell objective;
- `ROUND_TRIP` runs equal buy and sell legs in sequence from one preserved
  background path and reports both legs;
- `OBSERVE_ONLY` runs the same background control with no algorithm child order
  and proves zero algorithm quantity;
- target quantity, deadline, risk limits, and scenario selection come from one
  fixed mapping documented in the executor and recorded in every case.

Computed checks:

- all policy inputs are from `AlgorithmObservation`, never ground truth;
- child-order/risk-limit and objective quantity conservation;
- client fills equal coordinator venue fills and final signed position;
- every run starts from the recorded control-fork digest;
- no cross-strategy winner is declared from a single generated case;
- native multi-venue recording replay is captured for ATR-15.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-execution-algorithms
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: every credited strategy/objective case has real decisions, real
client observations, and reconciled venue execution—or an explicit observe-only
zero-action proof.

Commit: `Exercise real execution algorithms in audit lab`

### ATR-13 — Cut the runner over to real executors

Objective: remove the facsimile kernel from all generated pass/fail and coverage
claims and aggregate only typed evidence returned by the seven lanes.

Owned files:

- `kirby2/auditlab/kernel.py` (replace with registry dispatch or remove obsolete
  facsimile code while preserving one clear public entry point)
- `kirby2/auditlab/runner.py`
- `kirby2/auditlab/generator.py`
- `kirby2/auditlab/probes.py`
- `kirby2/auditlab/models.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. `run_generated_case()` dispatches strictly by `configuration.lane`.
2. Remove `_LATENCY_US`, hidden queue halving, auction-to-limit substitution,
   agent-owner substitution, IOC/FOK/post-only-to-market mapping, and every
   hardcoded `True` check from the generated path.
3. Coverage consumes successful `ExerciseRecord` values and separately reports
   missing configured values, required-check exercise counts, and supported
   within-lane pair counts.
4. Add independent `FillLedgerProjector` and `EventLedgerProjector`; neither may
   call the other or share an accumulator.
5. Convert subsystem probes to typed checks. The counterfactual probe must prove
   parent ID/digest, exact prefix equality through the fork, mutation only after
   the fork, and immutable branch verification. It is the source of
   `branch_parent_consistency`; no generated lane may claim it.
6. Overall generated status fails when a required lane/value/check lacks real
   evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-counterfactuals
rg -n 'auction_allocation_consistency.*True|branch_parent_consistency.*True|queue = max\(1, queue // 2\)|_LATENCY_US' kirby2/auditlab
git diff --check
```

The `rg` command must find no active facsimile or constant-check implementation.

Acceptance: the compact audit reports lane, exercises, check statuses, and source
evidence digests for every case; unsupported cross-lane interactions are
explicitly absent from coverage.

Commit: `Route generated audits through real systems`

### ATR-14 — Inject faults into production subsystems

Objective: separate the expected-code oracle from fault execution and obtain the
detected code only from real subsystem output or a production exception gate.

Owned files:

- `kirby2/auditlab/faults.py`
- create `kirby2/auditlab/fault_oracle.py`
- production adapter call sites only where an existing detector lacks a stable
  machine code
- `kirby2/auditlab/executors/base.py`
- `kirby2/audit/model_risk_lab.py`

Exact routing:

| Fault | Production path | Raw observed code |
|---|---|---|
| duplicate message | `normalize_raw_dataset()` with a repeated normalized row | `DUPLICATE_RECORD` |
| dropped market data | normalized MBO source with sequence 1, 2, 4 | `MISSING_SEQUENCE` |
| delayed acknowledgement | real latency session plus declared acknowledgement budget | `ACK_LATENCY_BUDGET_EXCEEDED` |
| out-of-order delivery | normalized source with reversed timestamp/sequence | `OUT_OF_ORDER_RECORD` |
| snapshot gap | normalized snapshot cadence gap | `SNAPSHOT_GAP` |
| corrupted dataset row | normalized negative-quantity row | `INVALID_QUANTITY` |
| venue rejection | real unsupported instruction or halted venue response | `UNSUPPORTED_MARKET_INSTRUCTION` or the oracle-declared alternate for that manifest |
| halt during pending order | real pending latency/venue order followed by halt before arrival | `PENDING_ORDER_HALTED` |
| cancel/fill race | real asynchronous race classification | `TERMINAL_RACE_CLASSIFIED` |
| schema mismatch | actual v2 recording/config loader given another version | `UNSUPPORTED_SCHEMA_VERSION` |

Implementation decisions:

1. `fault_oracle.py` owns expected code(s). Fault adapters do not import it and do
   not receive expected values.
2. `faults.py` returns manifest, injection location, subsystem, raw events/issues,
   and observed code. Runner compares observed to oracle afterward.
3. An expected detection is counted in `fault_observations`, never in unexpected
   failures or minimization sources. A missed/wrong detector is `FAULT_MISS`.
4. Preserve source rows, commands, and detector events in the case recording.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-data
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance: all ten faults are seen through real subsystem evidence; source
inspection in the runtime audit proves adapters do not import the oracle.

Commit: `Replace fabricated audit fault detectors`

### ATR-15 — Replay serialized command tapes

Objective: make replay run through a loader and native command application path,
not configuration regeneration.

Owned files:

- `kirby2/auditlab/models.py`
- `kirby2/auditlab/runner.py`
- `kirby2/auditlab/worker.py`
- all `kirby2/auditlab/executors/*.py`
- a production replay module only if the lane has no existing native recording
- `kirby2/audit/model_risk_lab.py`

Implementation decisions:

1. Every executor captures starting configuration/snapshot, explicit commands or
   native recording, expected event digest, expected state digest, and declared
   outputs in `CaseRecording` schema v2.
2. Serialize with canonical JSON, parse with `CaseRecording.from_dict()`, then
   call the lane's `replay()` in a distinct code path.
3. Use native `MechanicsRecording`, `LatencyRecording`,
   `ObservabilityRecording`, `MultiVenueRecording`, and `EcologyRecording`.
4. `CORE_FLOW` reconstructs real `FlowEvent` commands and applies them through
   `advance_exogenous_clock_to()`/`apply_exogenous_event()`; it does not call the
   stochastic scheduler during replay.
5. Algorithm replay uses its captured multi-venue recording and compares client
   decisions/metrics stored in the audit recording.
6. Replay parity compares event, state, observable, metric, and declared-output
   digests. Report the first differing field and both digests.
7. Fresh-process determinism remains separate: the worker receives the original
   v2 generated configuration and runs the real executor twice with
   `PYTHONHASHSEED=0`. Sample every lane and every fault family, not only evenly
   spaced global indices.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-agent-ecology
git diff --check
```

The audit must mutate one serialized command while leaving configuration
unchanged and prove replay changes/fails, demonstrating that replay consumed the
tape.

Acceptance: every real lane has loaded replay parity and sampled fresh-process
determinism evidence.

Commit: `Replay serialized generated recordings`

### ATR-16 — Minimize only actual reproducible failures

Objective: reduce unexpected failures with a predicate that can reproduce that
failure class, while keeping expected adversarial observations out of the
defect inventory.

Owned files:

- `kirby2/auditlab/minimizer.py`
- `kirby2/auditlab/models.py`
- `kirby2/auditlab/runner.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. `FailureObservation` identity includes kind, stable code, lane, check or field
   name, and source configuration/recording digests.
2. Implement predicates for `STRUCTURAL_CHECK`, `REPLAY_MISMATCH`,
   `DETERMINISM_MISMATCH`, `FAULT_MISS`, and `SUBSYSTEM_PROBE`.
3. The replay predicate captures, serializes, loads, and replays; it cannot call
   only `run_generated_case()`. The determinism predicate uses distinct workers.
4. Reduce duration, command count, agent count, venue count, and lane-supported
   configuration complexity only when the identical predicate remains true.
   Never change executor lane or required fault kind.
5. Verify the final reproducer twice. Store attempts, accepted reductions,
   rejected reductions, final recording, and both final digests.
6. If no unexpected failure exists, minimized-defect count is zero. Ten expected
   fault detections do not create ten minimized failures.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The runtime audit must use controlled injected audit-only predicates to prove one
structural and one replay mismatch minimize and still reproduce, while the normal
expected-fault set yields zero defect minimizations.

Acceptance: minimized count equals unique unexpected reproducible failure
identities, and every stored reproducer preserves its own failure class.

Commit: `Minimize only reproducible audit defects`

### ATR-17 — Build controlled statistical checks

Objective: compare matched experimental cells and use actual calibration or
time-series evidence for each model-risk claim.

Owned files:

- `kirby2/auditlab/generator.py`
- `kirby2/auditlab/statistics.py`
- `kirby2/auditlab/models.py`
- `kirby2/auditlab/runner.py`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/auditlab/README.md`

Implementation decisions:

1. Statistics use only complete six-replicate, no-fault cells. Match by lane and
   every non-seed exercised parameter. Never compare unrelated configurations.
2. `calibration_train_vs_holdout` runs `calibrate_market()` with disjoint seed
   sets derived from the master seed and reports fitting and heldout loss; mean
   generated volume is not called calibration.
3. `distribution_drift` compares train/holdout core-flow event-family and volume
   histograms within matched cells using total-variation basis points.
4. `scenario_overfitting` compares objective-normalized algorithm cost rankings
   only within matched scenario/objective cells and reports rank concordance; it
   does not declare a universal winner.
5. `seed_sensitivity` computes within-cell median, range, and median absolute
   deviation, then reports deterministic cross-cell quantiles.
6. Hawkes stability uses production certifications. Event explosion uses events
   per simulated second and configured cap. No-trade rate uses only continuous
   trade-eligible cells. Price runaway is measured from each lane's recorded
   initial reference.
7. Permanent cross uses the episode timeline from ATR-10. Fail only when a
   continuous crossed/locked episode exceeds
   `max(100_000 us, 4 * maximum configured market-data latency)`; record shorter
   episodes separately. An ending sample alone is not a duration proof.
8. Put every predeclared threshold and its unit in one immutable threshold
   manifest included in statistical evidence and packet identity.
9. Status is `PASS`, `WARNING`, `FAIL`, or `NOT_EXERCISED`. Insufficient complete
   cells are `NOT_EXERCISED` and prevent the substantial audit from passing.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 calibrate scenario:balanced --scenario balanced --seconds 1 --stages 1 --fit-seeds 101,202 --heldout-seeds 303,404 --reference-seed 771 --search-seed 771 --candidates 2
git diff --check
```

The audit must prove each train seed set is disjoint from holdout, all included
comparisons share one cell ID, changing only seed can change sensitivity, and a
short synthetic cross is not mislabeled permanent while an over-threshold one is.

Acceptance: every statistical label describes the data and design actually used.

Commit: `Build controlled audit risk statistics`

### ATR-18 — Bind provenance and report gate truth

Objective: make the packet identify all executed implementation inputs and keep
structural, statistical, coverage, replay, and human decisions visibly separate.

Owned files:

- `kirby2/auditlab/runner.py`
- `kirby2/auditlab/models.py`
- `kirby2/auditlab/store.py`
- `kirby2/auditlab/README.md`
- `kirby2/audit/model_risk_lab.py`
- `kirby2/__main__.py` only for CLI status/exit rendering

Implementation decisions:

1. Hash `pyproject.toml` and every regular source/config file under the executed
   package roots: auditlab, audit modules called by probes, exchange, session,
   simulation, latency, observability, multivenue, agents, algorithms,
   counterfactual, marketdata, calibration, scenarios, strategy, and player.
   Record the sorted path-to-digest manifest and its aggregate digest.
2. Dirty provenance records porcelain status plus working bytes, symlink target,
   or deletion marker for every changed/untracked path. Hashing path names alone
   is forbidden. A Git command failure is `UNAVAILABLE` and fails provenance.
3. Read package version from installed metadata, with `pyproject.toml` fallback
   and an explicit version source; remove the hardcoded version literal.
4. Separate report fields:
   `STRUCTURAL_STATUS`, `COVERAGE_STATUS`, `REPLAY_STATUS`,
   `DETERMINISM_STATUS`, `FAULT_STATUS`, `STATISTICAL_STATUS`,
   `PROVENANCE_STATUS`, and `MANUAL_ACCEPTANCE`.
5. Aggregate status is `FAIL` for any automated failure/unexercised required
   gate; `PASS_WITH_WARNINGS` for statistical warnings only; otherwise
   `AUTOMATED_PASS_PENDING_HUMAN` while the manual record remains pending.
6. Print `RUNTIME_INVARIANTS PASS` only when all required runtime checks were
   actually exercised and passed. Do not use that phrase for an aggregate with
   unexercised checks.
7. Packet v2 identity includes provenance manifest digest, threshold manifest
   digest, all result artifact digests, and acceptance-record digest.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

The runtime audit must alter bytes in a temporary copied source while preserving
the same dirty path name and prove the dirty digest changes. It must also prove a
warning-only result, an unexercised result, a runtime failure, and pending human
review render as four distinct states.

Acceptance: a reviewer can tell exactly what ran, what passed, what warned, what
was not exercised, what source bytes were used, and what still requires a human.

Commit: `Bind audit provenance and gate statuses`

### ATR-19 — Substantial closeout and immutable evidence

Objective: run the repaired laboratory at the requested budget, preserve honest
evidence, and close the repair sequence without beginning new product work.

Owned files:

- production files only for genuine defects reproduced by this slice, following
  the unforeseen-event protocol;
- `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md` only if an amendment was necessary;
- no checked-in `.kirby2` packet artifacts.

Execution order:

1. From a clean ATR-18 commit, run every focused audit below.
2. Run a non-persisting 512-case audit twice with seed 771 and compare summary,
   event, state, coverage, statistic, and result digests.
3. Run at least seeds 1, 42, 771, and 4,294,967,295 at a 512-case non-persisting
   budget. Report every invariant, replay, detector, or provenance failure.
4. Run the exact substantial command once with a clean worktree:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-lab --budget 10000 --seed 771 --save-failures
```

5. Verify the returned packet, packet ledger, acceptance ledger, artifact
   inventory, schema/identity scope, and all stored digests with
   `AuditLabStore.verify()` and `verify_ledgers()`.
6. Rerun the same 10,000 configuration set without persistence and compare its
   declared result digest to the persisted packet result. Do not create a second
   packet merely to demonstrate determinism.
7. Inspect saved reproducers for every unexpected defect. The count must be zero
   or equal the unique minimized unexpected-failure count; expected fault
   observations are not reproducers.
8. If a genuine defect appears, repair it under an `ATR-19A` amendment, commit
   that repair, return to a clean worktree, and repeat this card from step 1.

Focused audit commands:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 demo --seed 42
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hawkes-stability
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-data
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-execution-algorithms
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-counterfactuals
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-agent-ecology
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-model-risk-lab
git diff --check
```

Acceptance evidence to report:

- exact starting and ending commit;
- exact files changed in every repair/amendment commit;
- all focused audit case/failure counts;
- per-seed 512-case invariant/replay/fault results;
- generated cases, real lane counts, exercised coverage, and unexercised count;
- expected fault injected/detected counts and unexpected fault misses;
- replay mismatch and fresh-process determinism mismatch counts;
- unexpected failures and unique minimized reproducer counts;
- all statistical statuses with thresholds and complete-cell counts;
- v2 packet ID, manifest digest, path, artifact count, identity scope, and
  verification status;
- packet/acceptance ledger verification;
- manual acceptance decision, kept separate from automated status;
- final `git status --short`, which must be empty.

Commit rules:

- If the full run exposes and ATR-19 repairs code, commit the repair first under
  its amendment subject, rerun all evidence, then make the closeout commit.
- The closeout commit contains only documentation needed to preserve the final
  evidence summary; do not check in `.kirby2` artifacts.

Commit: `Close audit trust repair sequence`

Stop after ATR-19. Do not begin Work Order 31.
