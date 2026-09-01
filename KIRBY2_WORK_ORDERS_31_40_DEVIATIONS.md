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

## DEV-0007 — Enforce pseudonymous learner run identities

- Interrupted canonical card: `WO37-A`
- Exact first-parent predecessor: `10bb9f2120a36af191a79b05a6a6c670ca904929`
- Reproducer: create an empty `LearnerEvidenceLedgerV1` and its rebuilt projection
  with learner ID `ada.learner@example.invalid`, then pass both to
  `LearnerArtifactStore.record_update` and inspect the resulting manifest and JSON
  artifacts.
- Observed terminal result: the update persisted successfully and the email-like
  learner ID appeared in the immutable manifest, evidence ledger, and projection.
- Root cause: WO34-D required matching nonempty learner IDs at the ledger/projection
  boundary but did not distinguish a role-scoped pseudonym from direct identity.
  WO37-A's erasable identity mapping therefore could not guarantee separation while
  the already-sealed learner artifact writer still accepted and immortalized an
  arbitrary caller string.
- Repair: add a lightweight top-level pseudonym contract with exact lowercase
  instructor and learner namespaces, 32-to-4096-byte direct-identity-independent
  entropy, and role-domain-separated SHA-256 derivation. Require a strict opaque
  learner ID both before `LearnerArtifactStore` writes and after it loads artifact
  bytes. Migrate the six WO34-D synthetic fixtures to deterministic opaque learner
  IDs derived from an independent synthetic-only entropy domain; update the sealed
  seed-42 demonstration pin from
  `d88a2d0bad0c4ccfac25cacbc68ee632ffc29b951fb5698677471f1fe03e6b29` to
  `575bf85959fdff9590d10c30071b7d7e24415b0ea0f48f0f399af88309689576` while
  retaining the established first five pattern routes and the sixth cold-start
  broad route. Add a deterministic hostile-boundary audit that proves refusal occurs
  before persistence, valid opaque artifacts write/load/verify, tampered load bytes
  fail closed, and no direct marker enters a valid run. This is pseudonymization, not
  anonymity.
- Owned repair paths: create `kirby2/pseudonyms.py`; modify
  `kirby2/research/store.py`, `kirby2/curriculum/adaptive_commands.py`, and
  `kirby2/audit/adaptive_curriculum.py`; create
  `kirby2/audit/pseudonymous_learner.py`; modify
  `kirby2/audit/expansion.py`
- Deviation record path: `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`
- Gate registration: `DEV-0007` through the existing K2X-02 expansion seam,
  immediately after `WO36-E` and before resumed `WO37-A`.
- Inherited gates: `WO34-D` with the explicitly recorded identity-derived pin,
  run-store, `WO36-E`, and `K2X-02` remain required and must remain green.
- Exact commit subject: `Enforce pseudonymous learner run identities`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0007
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 adaptive-curriculum-demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO34-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
git diff --check
```

Acceptance: an email-like learner ID is refused before a staging or run artifact is
created; strict lowercase learner IDs write, load, and verify through the real store;
an instructor ID, malformed ID, uppercase digest, short entropy, non-bytes entropy,
and load-time direct-identity tamper all fail closed; the same entropy produces stable
but role-distinct instructor and learner pseudonyms; valid persisted bytes contain no
direct marker; the six synthetic learner IDs are unique deterministic pseudonyms
derived without direct identity; the seed-42 demo digest is exactly
`575bf85959fdff9590d10c30071b7d7e24415b0ea0f48f0f399af88309689576`; the first
five required evidence-pattern routes remain position management, book reading,
passive entry, aggressive entry, and liquidity withdrawal respectively, and the new
learner retains its distinct broad cold-start route; no identity mapping, brokerage,
order-routing, anonymity, educational-effectiveness, or real-trading capability is
claimed.

## DEV-0008 — Rebind final release starter identities

- Interrupted canonical card: `WO40-E`
- Exact first-parent predecessor: `18ae41d24503a46f1ee5641b64565075859dacc3`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 release-resource-preflight --platforms release/platforms.toml --lock release/requirements.lock --qualification release/qualification.toml --no-network --output KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`
- Observed terminal result: `ReleaseBuildRefused: PROTOCOL_INVALID`, caused by
  `ValueError: artifact layout starter literals differ from committed resources`.
- Root cause: WO40-D and its original D1 preflight were committed before the final
  WO39-E portability card standardized the required starter scenario identity to
  `KIRBY2_STARTER_PLACE_CANCEL_SCENARIO_V1`. That canonical manifest change altered
  the scenario manifest digest, both content-derived starter pack IDs, the starter-set
  digest, and both content-addressed archive names while the frozen artifact layout
  retained the provisional literals.
- Repair: mechanically bind the final two starter manifests, content-derived pack
  IDs, set digest, and archive names into `release/artifact_layout.toml`; add a focused
  gate that pins those exact identities and requires the complete release protocol to
  parse against the real starter builder. Rebind the same exact identities in the
  preregistered interactive-ack input template without changing its workload or
  thresholds.
- Owned repair paths: `release/artifact_layout.toml`,
  `release/performance_thresholds.toml`,
  `kirby2/audit/expansion.py`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0008` through K2X-02 immediately before resumed `WO40-E`.
- Inherited gates: `WO39-E`, the WO40-D protocol invariants, and the
  resource/provider requirements of `WO40-D1` remain unchanged. The missing WO40-D
  audit registration is a separate pre-freeze defect, and the original D1 report
  must be regenerated from the amended clean protocol commit before candidate freeze.
- Exact commit subject: `Rebind final release starter identities`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0008
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'from pathlib import Path; from kirby2.release.build import load_release_protocol_bundle; print(load_release_protocol_bundle(Path.cwd()).protocol_set_sha256)'
git diff --check
```

Acceptance: the final WO39-E starter manifests produce the two pinned content IDs;
the release layout carries their exact manifest digests, set digest, and archive
names; the complete release protocol parses without substituting provisional values;
and no target, dependency, threshold, qualification matrix, or product boundary is
changed.

## DEV-0009 — Register release qualification frontier

- Interrupted canonical card: `WO40-E`
- Exact first-parent predecessor: `8730ba83b4f54beb2308d7ef710b29e06e99a9fb`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-E`
- Observed terminal result: `NOT_REGISTERED`; the explicit expansion registry ended
  at `DEV-0008` even though WO40-A through WO40-E production surfaces and the WO40-D
  release commands/protocols already existed.
- Root cause: production-card implementation had intentionally deferred tests, but
  WO40-D's required `kirby2/audit/release.py`, WO40-A through WO40-J gate
  registrations, frozen future-evidence validators, and aggregate-to-closeout
  prerequisite publication were never recovered before the release-candidate source
  work began. Executing the recovered WO40-B audit also exposed three consequences of
  that missing frontier: legacy scenario floats entered a strict canonical recovery
  identity, checkpoint recordings attempted to persist those floats directly, and a
  reopened journal passed a frozen mapping to a decoder that required a mutable dict.
- Repair: add one release audit module that exercises public production APIs for
  paths/migrations, exact recovery, backup/restore, first run/diagnostics, and the
  frozen release protocol; preregister strict canonical immutable-evidence envelopes
  for WO40-F through WO40-I; register the non-self-referential WO40-J prerequisite
  validator; and make a fully passing aggregate publish the exact prior gate-report
  bytes consumed by the existing closeout command. Project legacy recovery floats
  through the already frozen exact-rational release semantic policy and reverse that
  representation only when reconstructing the legacy session recording. Accept
  immutable mappings at the journal source decoder.
- Owned repair paths: `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `kirby2/session/journal.py`,
  `kirby2/release/recovery.py`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: WO40-A, WO40-B, WO40-B1, WO40-C, WO40-D, WO40-D1,
  `DEV-0009`, and WO40-E through WO40-J through the existing K2X-02 seam;
  `DEV-0008` and `DEV-0009` remain immediately before resumed WO40-E in numeric
  deviation order.
- Inherited gates: WO39-E and DEV-0008 remain unchanged. WO40-D1 is expected to fail
  until its one owned Markdown report is mechanically refreshed from this clean
  protocol revision. WO40-E is expected to fail until its one mechanically derived
  runner-source lock is regenerated after all candidate source changes are staged.
  WO40-F through WO40-J remain `NOT_EXERCISED`; registration never fabricates or runs
  their one-time release evidence.
- Exact commit subject: `Register release qualification frontier`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0009
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-B1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-E
git diff --check
```

Acceptance: the first six requested gates pass and exercise real production APIs;
the D1 and E gates name only their expected stale mechanical bindings; F-J resolve as
registered `NOT_EXERCISED` gates rather than unknown selectors; exact recovery
restores the same complete state digest and pending/corrupt boundaries fail closed;
future evidence is strict, digest-bound, and read-only; no qualification workload,
release artifact, target, threshold, retry policy, product boundary, brokerage,
network, telemetry, updater, or background service is added or executed.

## DEV-0010 — Stabilize release preflight provenance

- Interrupted canonical card: `WO40-E`
- Exact first-parent predecessor: `7d58e4519b94ff07df1f947507e90b981a79d0bb`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D1`
  immediately after committing the exact passing D1 report.
- Observed terminal result: `FAIL`; the live renderer changed only
  `protocol_commit` from the report's predecessor to the report commit itself, so the
  report became stale merely by being committed.
- Root cause: `release_resource_preflight` resolved repository `HEAD` and called it
  the WO40-D protocol commit. That identity included later evidence/audit commits even
  though the protocol set is exactly the five paths in `RELEASE_PROTOCOL_PATHS_V1`.
  A self-invalidating D1 report could pass before its commit but never remain exact
  after it, blocking every clean WO40-E freeze.
- Repair: resolve the newest first-parent commit touching any exact frozen protocol
  path, independently compare all five current protocol files with that tree, and
  bind the report to that path-owned revision. Register a focused gate proving a
  later nonprotocol commit does not change either the resolved protocol revision or
  the exact report rendering, then regenerate the one D1 report from the repair.
- Owned repair paths: `kirby2/release/build.py`, `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0010` through K2X-02 immediately after `DEV-0009` and
  before resumed `WO40-E`; the closeout prerequisite deviation inventory extends
  monotonically through `DEV-0010`.
- Inherited gates: WO40-D's five protocol paths and protocol-set digest, WO40-D1's
  wheel/provider requirements, DEV-0008's final starter identities, and DEV-0009's
  release-audit contracts remain unchanged. The repair performs no build, remote
  connection, qualification, or performance work.
- Exact commit subject: `Stabilize release preflight provenance`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 release-resource-preflight --platforms release/platforms.toml --lock release/requirements.lock --qualification release/qualification.toml --no-network --output KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0010
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0009
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
git diff --check
```

Acceptance: D1 resolves the same protocol-owning commit before and after its report is
committed; every current protocol byte equals that revision; both exact wheelhouses,
both clean providers, tools, and starter packs remain PASS; the report is byte-exact;
the complete registry contains DEV-0001 through DEV-0010; and no target, dependency,
threshold, matrix, retry rule, product boundary, network action, artifact build,
qualification workload, or performance workload changes or runs.

## DEV-0011 — Restart release candidate with verified build inputs

- Interrupted canonical card: `WO40-F`
- Exact first-parent predecessor: `da9612349db2f76863ee16fb7726c6d8f85f5329`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 build-release --candidate da961232c550068bb0d58c4fb3bc49144c1e5a62 --protocol release/qualification.toml --artifact-store .kirby2/release`
- Observed terminal result: `READY` with exit status zero even though the supplied
  forty-character lowercase identity did not exist as any Git object. No artifact
  was created, but the build-plan decision falsely claimed that all preregistered
  inputs were addressable.
- Root cause: the CLI resolved Git only for the literal selector `HEAD`; an explicit
  hash-shaped value reached `plan_release_build`, which checked syntax, ambient lock
  presence, and ambient path presence but never resolved the commit or reconstructed
  any input from its Git tree. The planner therefore did not prove HEAD equality,
  tracked cleanliness, candidate-tree modes/paths, source-lock reproduction, or exact
  candidate protocol/dependency/layout bytes before returning `READY`.
- Repair: resolve the exact candidate commit, tree, and committer epoch; require it to
  equal a clean tracked HEAD; enumerate the complete candidate Git tree with strict
  UTF-8/NFC/case-fold/ustar and regular-blob rules; batch-read immutable Git blobs;
  disable local replacement objects and inherited Git repository-routing overrides;
  reject assume-unchanged/skip-worktree index flags and ignored or visible untracked
  files inside build-input namespaces; and recheck cleanliness after every checkout
  read;
  reproduce the canonical `kirby2/` plus `pyproject.toml` source projection; compare
  it with the exact candidate lock blob; and bind every frozen protocol file, source
  identity, logical build ID, and current checkout byte to that candidate. Rerun the
  complete no-network resource preflight immediately before `READY` and require its
  rendering to equal the passing report. Return typed `REFUSED` outcomes for missing
  commits, dirty tracked state, lock mismatch, protocol mismatch, or changed offline
  resources. External wheelhouses, provider inventory, agent maps, and artifact-store
  files remain outside the Git candidate projection.
- Owned repair paths: `kirby2/release/build.py`, `kirby2/release/__init__.py`,
  `kirby2/audit/release.py`, `kirby2/audit/expansion.py`,
  `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0011` through K2X-02 immediately after resumed `WO40-E`
  and before `WO40-F`; the closeout prerequisite deviation inventory extends
  monotonically through `DEV-0011`.
- Inherited gates: the five WO40-D protocol paths and protocol-set digest, the exact
  passing WO40-D1 report/resources, and all WO40-E launchers, documentation, and
  public release surfaces remain unchanged. The mechanically generated runner lock
  is refreshed only because the repair changes production and audit Python sources.
  This deviation performs no artifact build, provider connection, qualification, or
  performance workload.
- Exact commit subject: `Restart release candidate with verified build inputs`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 build-release --candidate da961232c550068bb0d58c4fb3bc49144c1e5a62 --protocol release/qualification.toml --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the hostile nonexistent identity refuses as
`CANDIDATE_COMMIT_INVALID`; a clean fixed predecessor fixture returns `READY` only
after reproducing its 482-entry source lock and five-file protocol set; existing
non-HEAD, replacement-object, staged, unstaged, assume-unchanged, skip-worktree,
ignored untracked build-input, committed source-drift, and each protocol-drift
fixture refuses with the exact typed code; a changed locked wheel refuses as
`RESOURCE_PREFLIGHT_INCOMPLETE`; planning creates no output; WO40-E passes against
the refreshed candidate lock; the complete registry contains DEV-0001 through
DEV-0011; and no release artifact, network action, provider operation, qualification,
or performance workload is created or executed.

## DEV-0012 — Bind release preflight resource fingerprints

- Interrupted canonical card: `WO40-F`
- Exact first-parent predecessor: `470cb3e1f11b0cbb431a97588e27a5d3017bbdfc`
- Reproducer: in a clean detached candidate fixture with the exact passing report,
  replace the executable `.venv/bin/pip` bytes with `#!/bin/sh` plus `exit 99` while
  retaining executable mode, then rerun `build-release` for that candidate. A second
  read-only reproducer inspects the active virtual environment: `pip` is present but
  `setuptools` is absent even though `pyproject.toml` requires `setuptools>=68` and
  the frozen frontend uses `pip wheel --no-build-isolation --no-index`.
- Observed terminal result: `READY` with no refusal code. The report SHA-256 before
  and after the tool-byte replacement was identical even though the live preflight
  observed a different packaging-tool digest. The same passing preflight also
  declared the offline no-isolation wheel frontend ready without its importable build
  backend, and did not bind the preregistered CPython/zlib reproducibility fingerprint.
- Root cause: passing resource rows carried `expected_sha256` and
  `observed_sha256` in memory, but the committed Markdown rendered neither value and
  serialized only failing rows. Exact report comparison therefore did not bind
  passing packaging tools, the raw provider-inventory bytes, provider fingerprints,
  or other successful resources. Packaging-tool discovery checked only launcher
  existence and executable mode; it did not inventory the active interpreter, zlib
  extension/behavior, or the actual `pip` and `setuptools` distribution bytes.
- Repair: hash the canonical ordered projection of every complete resource item and
  include that resource-snapshot SHA-256 in both the tracked report and
  machine-readable preflight result. Add a typed build-runtime snapshot containing
  the exact CPython manifest fields, the resolved complete CPython installation
  projection, executable and zlib-extension digests, compile and runtime zlib
  versions, a frozen archive-encoder behavior probe, and canonical
  path/mode/size/digest projections for actual `pip` and `setuptools` files; require
  CPython 3.14, reject unrecorded files beneath either distribution's owned roots,
  bind the complete active site-packages file projection including executable
  bytecode caches, bind the exact `pyvenv.cfg` bytes and ordered effective import
  path plus the complete virtual-environment tree (including the frontend directory
  and constrained interpreter symlinks), require a real virtual environment with
  user and system site-packages
  disabled, reject every package/import root outside the projected CPython and
  virtual-environment trees, and require the resolved
  `pip`, `setuptools`, and `setuptools.build_meta` origins to be their recorded files;
  reject Python path overrides, require isolated safe-path interpreter startup and
  load only the named candidate `kirby2` package without adding the checkout root to
  `sys.path`, require exact `setuptools==80.9.0`,
  and require a wheel frontend whose declared interpreter is the inspected runtime.
  The missing backend was
  supplied as exact external resource
  `setuptools==80.9.0` before regenerating the read-only report. Preserve the
  planner's exact live-report byte comparison and carry the full runtime snapshot in
  its READY record. Extend the detached DEV-0011 fixture so changing either executable
  `pip` bytes or parse-valid raw provider-inventory bytes refuses as
  `RESOURCE_PREFLIGHT_INCOMPLETE`, while the unchanged fixture remains `READY` and
  creates no artifact path. Register a focused audit proving a status-preserving
  packaging-tool fingerprint change alters both the resource snapshot and report,
  backend-file or virtual-environment-policy drift changes the runtime identity,
  external import roots are refused, and unrecorded backend shadows or unchecked-hash
  bytecode cannot remain invisible.
- Owned repair paths: `kirby2/__main__.py`, `kirby2/release/build.py`,
  `kirby2/release/__init__.py`,
  `kirby2/audit/release.py`, `kirby2/audit/expansion.py`,
  `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`,
  `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0012` through K2X-02 immediately after `DEV-0011` and
  before resumed `WO40-F`; the closeout prerequisite deviation inventory extends
  monotonically through `DEV-0012`.
- Inherited gates: the exact candidate, source lock, five frozen protocol files,
  artifact layout, dependency wheel digests, clean-provider capability rules, and
  all product boundaries remain unchanged. One bounded setup download supplies the
  previously absent build backend; every release preflight and build remains
  no-network. This repair performs no artifact build, provider connection,
  qualification, or performance workload.
- Exact commit subject: `Bind release preflight resource fingerprints`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py release-resource-preflight --platforms release/platforms.toml --lock release/requirements.lock --qualification release/qualification.toml --no-network --output KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the tracked passing report contains one exact canonical resource-snapshot
digest and a passing build-runtime/backend item; its runtime snapshot binds CPython,
the full resolved CPython installation, zlib, the archive encoder, `pip`, and
`setuptools`, plus the exact virtual-environment configuration and ordered effective
import roots and complete virtual-environment tree; changing only a passing tool
fingerprint changes the report identity without changing readiness; backend-file
drift changes runtime identity; non-isolated startup and any backend version other
than exact `80.9.0` are refused; its resolved import origins and complete
site-packages projection excludes invisible backend shadows and executable bytecode;
user/system site-packages and unprojected external import roots are refused;
changing actual fixture
`pip` bytes or adding a
parse-valid comment to the raw provider inventory refuses the build plan; the prior
DEV-0011 candidate, source, protocol, and wheel drift checks remain passing; the
complete registry contains DEV-0001 through DEV-0012; and no release artifact,
provider operation, qualification, or performance workload is created or executed.

## DEV-0013 — Execute deterministic release artifact builds

- Interrupted canonical card: `WO40-F`
- Exact first-parent predecessor: `d314080c64f2551085b852094bc99c8f60cf0daa`
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py build-release --candidate HEAD --protocol release/qualification.toml --artifact-store .kirby2/release`
- Observed terminal result: `READY` with exit status zero after candidate and resource
  validation, but with no six-artifact build, repeated-build comparison, canonical
  build record, structural artifact verification, or immutable activation marker.
  `verify-release-artifacts` checked only the sizes and SHA-256 values named by an
  existing index; it did not reconstruct or validate the promised wheel, source
  archive, desktop bundles, embedded manifests, member plans, license inventory,
  starter packs, display assets, or developer-data exclusions.
- Root cause: DEV-0011 correctly made the candidate, source lock, and protocol bytes
  immutable inputs, and DEV-0012 correctly bound the complete runtime and offline
  resource fingerprints. Those repairs were necessary preconditions, but they
  deliberately stopped at a read-only `READY` plan. The preregistered WO40-F command
  therefore still had no production executor, typed build record, two-attempt byte
  comparison, or verifier capable of proving the artifact semantics fixed by WO40-D.
- Repair: add one deterministic, no-network artifact executor which consumes only the
  verified candidate blobs and frozen external wheelhouse resources; materializes
  two isolated attempts under the frozen reproducibility environment; builds the
  exact project wheel, source archive, two headless wheelhouse archives, and two
  desktop bundles; compares all six transports byte-for-byte across attempts; and
  publishes immutable artifacts plus a canonical build record before writing the
  artifact index last as the activation marker. Add a strict wheel and archive
  verifier which recomputes member, manifest, license, starter-pack, display-asset,
  source, and no-developer-data claims rather than trusting the index. Bind the build
  record to the candidate tree/epoch, source lock, protocol, resource snapshot,
  runtime snapshot, exact attempt observations, seven preregistered WO40-F checks,
  and the final artifact index. Isolate disposable attempt trees in owner-only system
  temporary storage outside both the candidate and governed artifact store. Treat
  metadata-only File Provider provenance updates as non-content changes while still
  binding publication rollback to the no-follow file identity and exact SHA-256.
  Wire the existing declarative CLI handlers to these public contracts and extend
  immutable build-evidence validation to require both the index and build record.
  The deviation audit inspects policy and typed parsing only; it does not perform the
  one-time artifact build.
- Owned repair paths: `kirby2/release/artifacts.py`, `kirby2/release/wheels.py`,
  `kirby2/release/build.py`, `kirby2/release/manifest.py`,
  `kirby2/release/commands.py`, `kirby2/release/__init__.py`,
  `kirby2/audit/release.py`, `kirby2/audit/expansion.py`,
  `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0013` through K2X-02 immediately after `DEV-0012` and
  before resumed `WO40-F`; the closeout prerequisite deviation inventory extends
  monotonically through `DEV-0013`.
- Inherited gates: targets, artifact IDs and member layout, dependency pins and wheel
  digests, launchers, documentation, starter packs, display assets, license policy,
  qualification matrix, performance thresholds, retry policy, and all five product
  boundaries remain unchanged. No network, provider connection, brokerage action,
  qualification workload, performance workload, telemetry, updater, or background
  service is authorized. After the owned source changes and mechanically regenerated
  source lock commit cleanly, that commit is the new sole candidate; WO40-F then runs
  the already registered build command and commits evidence only.
- Exact commit subject: `Execute deterministic release artifact builds`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0013
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the public executor policy is exactly versioned and requires two
attempts; the canonical build-record parser refuses noncanonical, wrong-schema, and
wrong-attempt fixtures without invoking a build; both build and verification entry
points are public; the complete registry contains DEV-0001 through DEV-0013; the
prior candidate/resource gates remain passing; and no artifact, provider operation,
qualification workload, or performance workload is created or executed by the
deviation audit.

## DEV-0014 — Execute clean-environment release qualification

- Interrupted canonical card: `WO40-G`
- Exact first-parent predecessor: `5132c54f24c705641b217e8c4314a173e83bb4db`
- Historical candidate: `71f98c06f9b878fad3a165b0275c87e59fce6409`; its
  six immutable artifacts, artifact index, build record, build-evidence document,
  provider inventory, and a canonical snapshot manifest remain preserved under the
  untracked local release-history root before this repair changes candidate source.
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py qualify-release --platform macos-arm64 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release`
- Observed terminal result: `READY` with exit status zero after only checking that
  the WO40-F evidence, artifact index, and an untyped provider sidecar existed.  The
  command did not create a clean environment, install either selected artifact,
  execute any of the 38 functional rows, compare desktop/headless or cross-platform
  identities, publish an attempt, or verify returned evidence.  The generic WO40-G
  audit accepted caller-supplied SHA-256 strings without parsing a qualification
  attempt or reconstructing the 42 preregistered checks.
- Root cause: WO40-D deliberately owned dispatch and refusal semantics only, while
  the canonical WO40-G card assumed a later operational executor.  The freeze began
  before that executor and its evidence contract existed.  The first installed-path
  smoke also exposed two latent blockers: strict starter-pack verification passed a
  mapping to a sequence-only archive normalizer, and registered-run export treated
  the run's source `manifest.toml` as the reserved K2PACK manifest path and treated
  store encodings as K2PACK-canonical direct payloads.  The headless protocol lists
  uninstall before its four extra rows; DEV-0014 therefore keeps canonical evidence
  order unchanged but records an explicit execution index and requires headless
  extras to run before the physical uninstall lifecycle action.  First provider
  launch also exposed an ambient-policy coupling: provider-free, read-only artifact
  verification inherited WO40-F's build-only Codex Seatbelt check, while macOS
  correctly forbids Tart from executing its SUID Softnet helper beneath that same
  Seatbelt.  DEV-0014 therefore keeps the network gate on artifact construction,
  removes it only from non-executing verification, and continues to require true
  guest `--net-host` isolation with no NAT fallback.  The first true host-only boot
  then showed that the 10-second guest-agent probe timeout was incorrectly terminal
  even though the provider owns a bounded 300-second boot deadline; the repaired
  poll treats only that fixed probe timeout as retryable and retains terminal
  timeouts for every qualification workload command.
- Repair: add strict canonical provider-attestation, command-observation,
  root-observation, session, step, and qualification-attempt records; a closed
  installed guest worker for the exact desktop/headless matrix; and a closed Tart
  controller that accepts only the declared macOS arm64 provider, stages only
  immutable WO40-F inputs, creates owned disposable clones, uses true Tart
  `--net-host`, installs offline, captures bounded output through the guest agent,
  publishes the attempt last, and never falls back to NAT.  Add saved-run microscope,
  imported-pack replay, crash-recovery, calibration-roundtrip, and one-work-unit
  distributed receipts inside the installed worker.  Reconstruct all 38 step proofs
  and the exact 42-check gate projection from typed observations.  Deep WO40-G/H
  verification must independently reparse the provider, attempt, WO40-F build
  evidence, artifact index, build record, and selected transports; it cannot trust
  check digests supplied by the attempt or evidence document.  Repair only the two
  pack/doctor blockers reproduced above.  Keep `release/qualification.toml` byte
  unchanged.
- Owned repair paths: `kirby2/packs/builders.py`, `kirby2/release/doctor.py`,
  `kirby2/release/artifacts.py`,
  `kirby2/release/qualification.py`,
  `kirby2/release/qualification_records.py`,
  `kirby2/release/qualification_worker.py`,
  `kirby2/release/qualification_executor.py`, `kirby2/release/commands.py`,
  `kirby2/release/__init__.py`, `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0014` through K2X-02 immediately after rebuilt `WO40-F`
  and before resumed `WO40-G`; the deviation inventory extends monotonically
  through `DEV-0014`.
- Inherited gates: release targets, artifact IDs and member layout, dependency pins,
  launchers, documentation, starter content, performance thresholds, retry count,
  functional row identities, product limitations, and every earlier accepted
  production contract remain unchanged.  No brokerage, live-market connection,
  credential creation, telemetry, updater, background service, network download,
  Linux qualification, performance run, or final closeout is authorized.  After the
  repair and mechanically regenerated source lock commit cleanly, that commit is the
  sole repaired candidate.  WO40-F is rebuilt from it before any WO40-G attempt.
- Exact commit subject: `Execute clean-environment release qualification`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0013
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0014
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the attempt parser refuses noncanonical, wrong-schema, wrong-order,
wrong-count, wrong-target, and caller-forged check projections; the deep verifier
reconstructs exactly 38 matrix observations and 42 gate checks; the command owns a
real executor rather than a `READY` dispatcher; the controller has no generic
command, VM, network, or cleanup surface and contains no NAT fallback; the installed
worker refuses checkout execution; the complete registry contains DEV-0001 through
DEV-0014; artifact construction still requires the recorded Codex network gate while
provider-free read-only verification does not forge that ambient marker; the previous
candidate is retained as history; and the deviation audit does not clone, boot,
connect to, mutate, or delete a provider or execute the one-time qualification
workload.
