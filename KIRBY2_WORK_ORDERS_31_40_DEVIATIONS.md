# Kirby2 Work Orders 31-40 Deviation Ledger

This append-only ledger records unforeseen prerequisite repairs encountered while
executing `KIRBY2_WORK_ORDERS_31_40_GOAL.md`. Records are never renumbered or edited
after their repair commit.

## DEV-0001 — Bind explicit CLI modules to audit provenance

- Interrupted canonical card: `K2X-02`
- Exact first-parent predecessor: `2e8969baa2c3cf436c176e18a3ac21391cda2ee0`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab`
- Observed terminal result:
  `MODEL_RISK_LAB_AUDIT FAIL cases=21 failures=1`
- Observed failing case: `byte_bound_provenance_and_orthogonal_gate_truth`
- Root cause: K2X-02 introduces the explicit `kirby2/cli` source package. The
  inherited byte-bound provenance manifest enumerates explicit package roots and did
  not yet include `cli`, so the three loaded CLI modules were correctly reported as
  unbound even though their behavior and legacy compatibility checks passed.
- Repair: add `cli` to `PROVENANCE_PACKAGE_ROOTS`. This makes the existing generic
  manifest and its independent model-risk oracle bind every CLI source byte without
  weakening loaded-module checks or special-casing filenames.
- Owned repair path: `kirby2/auditlab/runner.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0001` through the in-progress K2X-02 explicit expansion
  seam. The registration source lands with the resumed K2X-02 commit because that
  seam does not exist in the predecessor tree.
- Inherited gates: K2X-02 legacy CLI projection, deterministic smoke pair, sealed
  `.kirby2` tree, and all model-risk cases remain unchanged.
- Exact commit subject: `Bind CLI sources to audit provenance`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0001
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'from pathlib import Path; from kirby2.auditlab.runner import PROVENANCE_PACKAGE_ROOTS,_implementation_manifest; root=Path.cwd(); manifest,links,errors=_implementation_manifest(root); expected={"kirby2/cli/__init__.py","kirby2/cli/expansion.py","kirby2/cli/registry.py"}; assert PROVENANCE_PACKAGE_ROOTS.count("cli")==1; assert expected.issubset(manifest); assert not links and not errors; print("CLI_PROVENANCE_BINDING PASS files=3")'
git diff --check
```

Acceptance: `cli` occurs exactly once in the explicit provenance roots; the three
K2X-02 CLI source files are byte-bound by the generic implementation manifest;
loaded-module binding passes without exclusion or unloading; the model-risk command
returns zero with `MODEL_RISK_LAB_AUDIT PASS cases=21 failures=0`; and no `.kirby2`
artifact changes.

## DEV-0002 — Reconcile zero-time macro anchor transitions

- Interrupted canonical card: `WO31-B`
- Exact first-parent predecessor: `e50f43c596482e0b91ccddb63b10a38e4d3db09c`
- Reproducer: construct a valid acyclic day-state path whose macro-anchored state
  samples duration zero, then validate its plan-bound anchor at `(0,0,stage 2)` and
  forced successor at `(0,1,stage 2)` with `validate_full_day_event_stream`.
- Observed terminal result:
  `ValueError: macro anchor replacement forbids a same-time day graph transition`
- Root cause: the sealed WO31-A validator reduced anchor and graph-transition
  ordering to intersecting timestamp sets. That discarded the frozen microstep
  distinction even though zero-duration acyclic paths are legal and same-time child
  work is required to execute at a strictly later microstep.
- Repair: retain exact plan edge, state-continuity, duration-support, and canonical
  ordering checks; require anchors at microstep zero; require an anchor to precede
  graph transitions at its timestamp; reconcile each observed day-state entry with
  the selected edge's minimum age; seed suffix reconciliation from the already
  verified prefix's terminal day/local state and day-entry time; and permit a
  same-time successor only at a strictly later microstep. The validator does not
  infer an unrecorded transition cause: both forced exhaustion and a cutoff-safe
  trigger remain legal for a minimum-age-zero edge.
- Owned repair paths: `kirby2/full_day/events.py`, `kirby2/audit/full_day.py`,
  `kirby2/audit/expansion.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0002` through the existing K2X-02 expansion seam,
  immediately before resumed `WO31-B`.
- Inherited gates: `WO31-A`, `K2X-02`, market mechanics, and agent ecology remain
  unchanged.
- Exact commit subject: `Reconcile zero-time macro anchor transitions`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0002
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-agent-ecology
git diff --check
```

Acceptance: a zero-duration macro anchor followed by one forced successor and a
two-hop acyclic chain validate at strictly increasing microsteps; a same-time
minimum-age-zero triggered successor remains representable without inventing an
outer-event cause field; same-microstep successors, nonzero-microstep anchors, and
minimum-age violations fail closed; a checkpoint suffix must continue the verified
prefix's state and entry age; the repair changes no V1 wire schema and no unrelated
runtime capability claim.

## DEV-0003 — Reconcile state runtime checkpoint inventory

- Interrupted canonical card: `WO31-B`
- Exact first-parent predecessor: `5203f62611e90cdeadace46b3882d4af97a831db`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'from kirby2.full_day.checkpoint_contract import checkpoint_inventory_v1; item=next(x for x in checkpoint_inventory_v1().items if x.component_id=="CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"); required={"state.component_sequence_offset","state.runtime_emission_count","state.day_transition_count","state.local_transition_count","state.day_transitions_since_macro_anchor","state.next_macro_segment_index","state.observation_ids_seen","state.input_closed_through_time_us","state.plan_sha256","state.state_model_sha256"}; missing=sorted(required-set(item.owned_state_fields)); assert not missing, f"missing checkpoint state ownership: {missing}"'`
- Observed terminal result: `AssertionError: missing checkpoint state ownership:
  ['state.component_sequence_offset', 'state.day_transition_count',
  'state.day_transitions_since_macro_anchor',
  'state.input_closed_through_time_us', 'state.local_transition_count',
  'state.next_macro_segment_index', 'state.observation_ids_seen',
  'state.plan_sha256', 'state.runtime_emission_count',
  'state.state_model_sha256']`
- Root cause: the frozen WO31-A inventory described only the initial day/local
  state, age, sampled duration/deadline, selected transition, trigger memory, and
  component-local high-water value. WO31-B's exact continuation contract also owns
  the allocator offset and emission reconciliation counters, separate day/local
  transition counts, macro-anchor cursor, observation-consumption history, closed
  input frontier, plan/model bindings, and derived next-eligibility projections.
  The old semantic-alias table also retained an orphan aggregate
  `state.completed_transition_count`, which cannot represent the separate counters.
- Repair: expand the existing state-runtime inventory row to the complete 27-field
  semantic authority set, including the plan/model bindings and both
  next-eligibility projections; preserve day and local transition counters
  separately; remove the orphan aggregate alias; and make every one-field omission
  plus an aggregate-counter substitution fail the exact inventory contract.
- Owned repair paths: `kirby2/full_day/checkpoint_contract.py`,
  `kirby2/audit/full_day.py`, `kirby2/audit/expansion.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0003` through the existing K2X-02 expansion seam,
  immediately after `DEV-0002` and before resumed `WO31-B`.
- Inherited gates: `WO31-A`, `DEV-0002`, and `K2X-02` remain unchanged.
- Exact commit subject: `Reconcile state runtime checkpoint inventory`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0003
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0002
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the state-runtime inventory declares exactly 27 sorted unique semantic
fields; every field needed to reconcile exact restore authority is represented;
each one-field omission fails closed; plan/model bindings, closed-input frontier,
macro cursor, observation history, allocator offset, and separate transition counts
cannot be omitted; the obsolete aggregate counter is absent and cannot replace the
level counters; canonical inventory bytes round-trip without identity drift; and no
runtime capability beyond the interrupted WO31-B implementation is claimed.

## DEV-0004 — Reconcile atomic boundary replay ordering

- Interrupted canonical card: `WO31-E6`
- Exact first-parent predecessor: `f34274032577167d72c65d76bd2a88241f485787`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-A`
- Observed terminal result: `GATE_EXCEPTION`, caused by
  `RuntimeError: outer mechanics group differs from deterministic public replay`
  during the postclose auction uncross.
- Root cause: the full-day runtime owns a shared mechanics clock and advances it
  before executing its atomic stage-zero calendar boundary. The frozen boundary
  order is auction uncross, transition-owned expirations, session transition, then
  same-time GTT expiry. The strengthened outer-command validator instead called
  `advance_to` before replaying the first boundary event, causing the shadow engine
  to publish the same-time GTT expiry first and disagree with the valid native
  ledger.
- Repair: when no configured engine-owned session transition or overdue timer is
  pending, defer only GTT work due at the exact boundary timestamp while replaying
  preceding outer-owned operations. Non-strict live invariant checks may observe
  that in-flight prefix; strict checkpoint validation still refuses the state until
  every due GTT event is present. Completed boundary checkpoints round-trip through
  the exact public-operation replay.
- Owned repair paths: `kirby2/exchange/mechanics_engine.py`,
  `kirby2/audit/full_day.py`, `kirby2/audit/expansion.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0004` immediately before the unregistered `WO31-E6`
  frontier.
- Inherited gates: `WO31-A`, market mechanics, `WO31-E1` through `WO31-E5`, and
  `K2X-02` remain unchanged.
- Exact commit subject: `Reconcile atomic boundary replay ordering`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0004
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate all
git diff --check
```

Acceptance: the postclose boundary retains native uncross, transition-owned expiry,
session, and exact-time GTT order; completed state round-trips through a strict
mechanics checkpoint; an in-flight boundary that still owes exact-time GTT work is
not checkpointable; configured session transitions and overdue timers cannot use the
deferral; the aggregate expansion gate is green; and no V1 wire schema or unrelated
runtime capability changes.

## DEV-0005 — Harden replay observation invariants

- Interrupted canonical card: `WO36-C`
- Exact first-parent predecessor: `da7f7396dc973c87148875ab38636b5969dc22f9`
- Reproducer: construct an otherwise valid `ObservationQueryResult`, then replace a
  client-delivered value's recorded receipt with typed absence; independently shift
  a revealed value's policy-visible time away from its source-event time or attach a
  recorded client-receipt timestamp to it.
- Observed terminal result: all three contradictory result roots were accepted by
  direct dataclass replacement even though the ordinary query builders did not emit
  them.
- Root cause: WO36-B validated each value's local timing and policy labels, but the
  public result root did not yet restate the final cross-object invariants for exact
  client delivery and reveal-only clocks. A caller could therefore assemble a
  contradictory result after the query builder returned.
- Repair: require a recorded client-receipt timestamp for every
  `CLIENT_DELIVERED` result, require reveal visibility to equal source-event time,
  reject reveal claims of client receipt while preserving the existing
  client-knowledge refusal, and retain one hostile root-constructor probe for each
  newly repaired invariant.
- Owned repair paths: `kirby2/microscope/query.py`,
  `kirby2/audit/replay_microscope.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0005` through the K2X-02 expansion seam immediately
  before `DEV-0006` and resumed `WO36-C`.
- Inherited gates: `WO36-B`, hidden liquidity, strategy time, and `K2X-02` remain
  unchanged.
- Exact commit subject: `Harden replay observation invariants`
- Published-history note: the exact repair commit was pushed before the retrieval
  audit classified this prerequisite. This record and its focused gate are appended
  in the next contiguous deviation commit; published first-parent history is not
  rewritten and the repair subject is not duplicated.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0005
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
git diff --check
```

Acceptance: the three contradictory result roots fail closed; their exact hostile
probes remain part of the fixed WO36-B audit evidence; the six WO36-B cases and both
named regression audits remain green; and no published commit is amended or
duplicated.

## DEV-0006 — Verify replay observation ingestion

- Interrupted canonical card: `WO36-C`
- Exact first-parent predecessor: `87aad32b7cd7c39fce6773dfa9f0059edc311e63`
- Reproducer: import `ObservedValueRecord` and `ObservedEvidenceSet` directly,
  label arbitrary JSON as client-delivered evidence, and query it without presenting
  immutable recorder bytes or a source-manifest digest supplied by the backend run
  index.
- Observed terminal result: the query correctly enforced the supplied observed
  plane but could not prove that the supplied records originated in the recorded
  client feed or decision snapshot artifacts. Post-construction mutation could also
  make a caller-held evidence object disagree with its stored digest.
- Root cause: WO36-B intentionally assigned external provenance to a future trusted
  adapter, but no concrete adapter or UI-safe query facade existed. Frozen Python
  dataclasses and `__all__` document ownership; they are not independent provenance
  or a hostile same-process security boundary.
- Repair: add a closed V1 adapter that accepts exact canonical raw bytes only, checks
  its caller-supplied manifest pin before parsing, requires the exact two
  observed source roles including a recorded empty plane, validates raw and
  normalized digests, schemas, run/source identity, record kinds, payload contracts,
  timing, and sequence order, constructs both planes atomically, and emits an
  immutable backend-only ingestion receipt. Its query facade retains pinned raw bytes
  privately, reconstructs and revalidates evidence for every query, accepts no reveal
  input, exposes no full-run receipt or digest, and returns only policy-enforced result
  objects or detached canonical bytes. The integration contract—not this byte
  loader—must obtain the pin from a governed run index, bind it to the requested run,
  and ensure `source_event_sha256` is a public observed-only identity rather than a
  commitment to hidden or future outcome bytes.
- Owned repair paths: create `kirby2/microscope/ingestion.py`; modify
  `kirby2/audit/replay_microscope.py` and `kirby2/audit/expansion.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0006` through the existing K2X-02 expansion seam,
  immediately after `DEV-0005` and before resumed `WO36-C`.
- Inherited gates: `DEV-0005`, `WO36-B`, `WO36-A`, hidden liquidity, strategy time,
  and `K2X-02` remain unchanged.
- Exact commit subject: `Verify replay observation ingestion`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0006
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0005
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
git diff --check
```

Acceptance: manifest and artifact bytes cannot be co-tampered under the original
backend pin; duplicate keys, noncanonical JSON, floats, boolean integer aliases,
unknown fields, source retargeting, missing/swapped/duplicate roles, unknown record
kinds, forbidden payload fields, bad timing, and non-monotonic sequences fail closed;
raw and normalized identities survive in a deterministic receipt; an explicitly
recorded empty plane is distinct from omission; repeated queries revalidate pinned
bytes and are byte-identical; first-party UI modules do not import raw evidence,
reveal, authorization, ingestion constructors, backend verification, or receipts;
the query facade and its repr expose no full-run receipt commitment; and this
cooperative in-process boundary makes no claim against arbitrary code already
executing inside the backend interpreter or a malicious/misconfigured pin issuer.
