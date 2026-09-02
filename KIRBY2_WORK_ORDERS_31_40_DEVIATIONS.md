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
  timeouts for every qualification workload command.  The repaired boot then
  reached the cleanliness proof and correctly refused the stock Tart image because
  its `admin-nopasswd` sudo drop-in grants unrestricted passwordless root authority.
  Preparing a derived base exposed one further identity gap: the fixed provider
  bound its configuration, NVRAM, disk size, owner, link count, and writable-mode
  restrictions, but not the 80 GB disk contents that actually carry the guest
  policy.  The first hardened-provider attempt then exposed an incorrect VirtioFS
  predicate: Tart correctly mounts the shared filesystem at `/Volumes/My Shared
  Files` and places the named `release` share beneath it, while the controller
  incorrectly required the child directory to appear as its own mount-table row.
  After that predicate was repaired, the next pre-install refusal exposed five
  controller probes pinned to nonexistent macOS path `/usr/bin/test`; an exhaustive
  guest executable inventory confirmed `/bin/test` as the sole required correction
  and found every other mandatory absolute command path present.
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
  unchanged.  Preserve the stock VM and every superseded WO40-F artifact set;
  derive a separately named offline base, remove only its exact passwordless-sudo
  drop-in while retaining a root-only recovery copy, and pin its new configuration,
  NVRAM, and streamed whole-disk SHA-256 under a distinct hardened-provider policy.
  The disk verifier must use a no-follow descriptor, bounded streaming memory, and
  stable before/after file identity rather than loading the image or trusting its
  path metadata alone.  Each randomized disposable clone must then reproduce the
  fixed hardware/ECID projection, NVRAM digest, disk digest, and exact disk mode
  immediately before its sequential boot; hashing only the named base is
  insufficient evidence for the bytes copied into the qualification guest.  Bind
  the shared-directory proof to the unique parent `AppleVirtIOFS` mount row, then
  independently require the named `release` child directory before proving its
  `:ro` behavior with a failed write and absent sentinel.  Pin all five directory,
  sentinel, launcher, and attempt-root predicates to the observed macOS
  `/bin/test` executable and structurally refuse the nonexistent `/usr/bin/test`
  spelling.
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
provider-free read-only verification does not forge that ambient marker; previous
candidate artifacts remain in canonical local release-history snapshots; the
hardened provider and each preboot clone bind exact configuration capability, NVRAM,
disk contents, size, and mode; hostile digest inputs fail closed; and the deviation
audit distinguishes the parent VirtioFS mount from the named read-only child without
cloning, booting, connecting to, mutating, or deleting a provider, pins the five
macOS `test` call sites, and does not execute the one-time qualification workload.

## DEV-0015 — Execute Linux clean-environment qualification

- Interrupted canonical card: `WO40-H`
- Exact first-parent predecessor:
  `df2d63ef15766c5edc71b28091f696db97e3fd32`
- Historical release: the predecessor is the completed WO40-G evidence commit for
  source candidate `3111727d2ee6a411e07242ed4c4e34c9b8bfed27`.  Before this
  repair changes candidate source, its immutable history snapshot is preserved at
  `.kirby2/release-history/df2d63ef15766c5edc71b28091f696db97e3fd32/`.
  Its V2 manifest binds exactly thirteen payloads: WO40-F and WO40-G evidence;
  macOS provider attestation; provider inventory; WO40-G attempt; six release
  transports; artifact index; and build record.  The history manifest itself binds
  the release-evidence commit and source candidate and remains outside the governed
  active artifact store.
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py qualify-release --platform linux-x86_64 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release`
- Observed terminal result: `REFUSED` with exit status two and typed code
  `QUALIFICATION_TARGET_UNSUPPORTED`; the closed Tart implementation reported that
  it supported only `macos-arm64`.  The command did not connect to the Fedora host,
  stage an artifact, create a remote attempt root, enter a network namespace, run
  the installed matrix, clean a provider path, or publish WO40-H evidence.
- Root cause: DEV-0014 intentionally authorized and closed only the macOS Tart
  provider while preserving Linux as out of scope.  The public command, typed
  records, and deep verifier already named `linux-x86_64`, but the public executor
  was the macOS implementation rather than a target-neutral dispatcher and no
  controller owned Linux provider identity, transport, isolation, cleanup, or
  cross-platform-baseline reconstruction.
- Repair: retain one public `execute_release_qualification` contract and route its
  exact target IDs through a fixed declarative dispatcher to private macOS and Linux
  implementations.  Add one closed Linux executor for the pinned Fedora SSH
  provider: existing identity file only; exact user, address, port, host-key
  algorithm, public key, and fingerprint; batch-only SSH/SFTP with forwarding,
  password, keyboard-interactive, local-command, proxy-command, ambient config, and
  unpinned-host-key behavior disabled.  Permit macOS Keychain to unlock only that
  explicitly pinned passphrase-protected identity while keeping identity-agent use
  disabled and refusing to add the key to an agent.  Attest the remote machine and
  required executables before accepting it; create only a nonce-bearing owner root
  beneath the fixed remote qualification prefix; stage only the immutable WO40-F
  inputs; install without network; and execute the same desktop and headless forms
  sequentially inside a fresh user and network namespace.  Count only non-loopback
  IPv6 defaults so the kernel's unreachable loopback sentinel routes do not forge
  external reachability; independently require `lo` as the sole interface, no IPv4
  default, and a failed TEST-NET connection.  Recreate the same logical installed
  worker attempt root between forms, require the worker's common
  target-neutral execution policy, parse every probe and result canonically, and
  publish an attempt only after all proof and cleanup obligations pass.  Put the
  installed runtime and worker behind `setpriv --no-new-privs`, prove the inherited
  kernel bit inside that same user/network/PID namespace, and use only an owned
  root-local home and temporary directory for candidate-controlled processes; this
  prevents setuid or file-capability elevation and keeps normal candidate state
  inside the marker-bound root even if the account's sudo policy or real home
  changes.  Rewind every pinned descriptor-backed pack-stage directory before its
  exact scan because Linux shares directory cursors across duplicates, and prefer
  bounded-memory, NFC-normalized, control-free stdout diagnostics capped by UTF-8
  bytes when a product command fails without stderr.  Both host controllers
  independently validate the closed failure-code grammar and diagnostic byte budget
  so a typed refusal cannot be masked by malformed worker output.  Cleanup
  revalidates the owned remote-root grammar and marker, preflights same-UID and
  same-device descendants, makes only owned sealed directories removable, and
  deletes them through no-follow descriptors before revalidating and deleting the
  ownership marker last.  Deletion relies on the immediately preceding global
  process/SFTP absence proof while the retained provider lock excludes another
  controller.  Cleanup runs from `finally` and turns any ambiguity into a typed
  refusal with the marker and lock retained whenever deletion did not complete.
  Bound every isolated
  command with a shorter remote timeout and a kill-on-parent-exit PID namespace;
  require the owned root in every lifetime leader's command line, scan every
  same-account command line for the fixed qualification prefix, and rely on PID-
  namespace ownership to make descendants unable to outlive that leader.  Also
  refuse deletion while any non-ancestor same-account SFTP server or non-TTY SSH
  transport session remains, covering an ambiguously closed transfer channel.
  Arm a process-quarantine flag before the final root-absence command and clear it
  only after the exact global empty-process receipt.  Retain the fixed provider
  lock as a quarantine marker whenever process or root cleanup remains ambiguous
  rather than admitting a later controller.  A passing
  Linux record must use `GUEST_NETWORK_DISABLED_VERIFIED` and the
  `SSH_EPHEMERAL_HOST_V1` adapter.  Before Linux publication and again during deep
  WO40-H verification, independently open, parse, and pure-verify the preserved
  WO40-G provider and attempt records, require their exact `PASS`, candidate,
  protocol, source-manifest, artifact-index, build-evidence, selected-artifact, and
  integer-core bindings, and recheck their no-follow directory/file identities at
  the end.  Retire local projections and temporary SSH trust material, then run the
  full deep verifier against the candidate provider/attempt bytes before publishing
  the provider first and activating the attempt last.  Keep
  `release/qualification.toml` byte unchanged.
- Owned repair paths: `kirby2/release/qualification_executor.py`,
  `kirby2/release/qualification_linux_executor.py`,
  `kirby2/release/qualification_worker.py`,
  `kirby2/packs/staging.py`,
  `kirby2/release/qualification_records.py`,
  `kirby2/release/qualification.py`, `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0015` through K2X-02 immediately after completed
  `WO40-G` and before resumed `WO40-H`; the release sequence is exactly
  `DEV-0013`, `WO40-F`, `DEV-0014`, `WO40-G`, `DEV-0015`, `WO40-H`, and the
  deviation inventory extends monotonically through `DEV-0015`.
- Inherited gates: candidate and history immutability, five frozen protocol files,
  target/artifact/member identities, dependency wheels, launchers, documentation,
  starter content, performance thresholds, retry count, step and check order,
  product limitations, and all earlier production contracts remain unchanged.  No
  brokerage, live-market connection, credential creation, telemetry, updater,
  background service, network download, GPU workload, performance run, or final
  closeout is authorized.  After repair source and the mechanically regenerated
  source lock commit cleanly, that commit is the sole repaired candidate; WO40-F is
  rebuilt from it, WO40-G is requalified against it, and only then may WO40-H run on
  the pinned Fedora provider.
- Exact commit subject: `Execute Linux clean-environment qualification`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0013
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0014
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0015
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
git diff --check
```

Acceptance: the public wrapper and exact two-target dispatch remain one stable
contract; the Linux executor owns a pinned provider, closed transport grammar,
fresh user/network namespace, canonical results, typed refusals, and finally-owned
cleanup; passing Linux evidence is bound to the independently reparsed passing
macOS baseline and exact shared integer core; the installed worker policy is
target-neutral; the complete registry contains DEV-0001 through DEV-0015 in the
required release order; the `df2d63e` historical snapshot remains immutable; and
the four-case deviation audit proves dispatch, transport/isolation, hostile-result
cleanup, and cross-platform-baseline structure without importing the executor,
opening SSH/SFTP, touching a provider, or executing a qualification workload.

## DEV-0016 — Execute release performance qualification

- Interrupted canonical card: `WO40-I`
- Exact first-parent predecessor:
  `627d6a446ac56f5f60623f75823845805a70aeeb`
- Historical release: the predecessor is the completed WO40-H evidence commit for
  source candidate `674d3094764622e34d88f1df7095a8f7db9e5bdc`.  Before this
  repair changes candidate source, its immutable history snapshot is preserved at
  `.kirby2/release-history/627d6a446ac56f5f60623f75823845805a70aeeb/`.
  Its V2 manifest binds exactly sixteen payloads: WO40-F, WO40-G, and WO40-H
  evidence; the provider inventory; both provider attestations; both qualification
  attempts; six release transports; the artifact index; and the build record.  The
  history manifest separately binds the release-evidence commit and source candidate
  and remains outside the governed active artifact store.
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py qualify-performance --manifest release/performance_thresholds.toml --complete-run-work-units 10000 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release`
- Observed terminal result: `READY` with exit status zero after validating only the
  manifest path, requested count, build-evidence path, artifact-index path, and
  runner-source-lock path.  The command did not execute an installed artifact,
  dispatch a row, start four workers, admit a FIFO item, measure an auxiliary
  workload, enforce an attempt limit, retry a failure, write an immutable performance
  record, activate an aggregate, create WO40-I evidence, or deeply verify a result.
- Root cause: WO40-D preregistered the complete row/auxiliary schemas, templates,
  thresholds, command names, and generic evidence envelope, but its production
  command handlers deliberately stopped at source binding and readiness.  No closed
  installed-artifact row executor consumes a bound row, no coordinator owns the
  four-worker/256-row FIFO and retry/resource protocol, no measurement adapter owns
  the five auxiliary workloads, no immutable performance-record/activation grammar
  exists, and the generic WO40-I audit trusts declared counts and opaque referenced
  bytes instead of reconstructing them through a deep verifier.
- Repair: retain the canonical `qualify-performance` and
  `qualify-performance-row` command names, but replace their `READY` endpoints with
  one closed execution policy `KIRBY2_RELEASE_PERFORMANCE_EXECUTION_V1`.  Add an
  installed-artifact row worker that consumes only the committed protocol, bound
  source projection, immutable WO40-F artifacts, exact work-unit ID, and authorized
  attempt; executes the row's named production runner; materializes the six semantic
  members and permitted operational/legacy sidecars; and independently reparses and
  verifies the result before returning it.  Normalize the mathematically zero input
  produced by a negatively directed queue-reactive rule to positive zero at the
  production term boundary so the frozen float-free release projection cannot be
  defeated by an IEEE-754 sign bit with no numerical meaning.  Attempt two is admitted only after
  attempt one records `PROCESS_FAILURE` or `RESOURCE_LIMIT`, and it never creates a
  second logical work unit.  Add one coordinator with exactly four ready worker
  processes, at most four concurrent attempts, and a FIFO capacity of 256; it enforces the
  committed per-attempt and 36-hour limits, records every attempt, refuses duplicate
  or missing IDs, verifies every completed tuple before admission, and derives the
  deterministic 10,000-unit aggregate without allowing operational timing to alter
  semantic identity.  Execute the five committed auxiliary templates against their
  selected installed desktop/headless artifact, retain exact warmup and measured
  series, apply the preregistered reductions and thresholds, and preserve every
  honest warning, failure, and unavailable reduction.  Add typed immutable work-unit,
  aggregate, and activation records beneath one closed release-store path grammar;
  publish content-addressed records first, the verified aggregate next, and the
  activation record plus `KIRBY2_RELEASE_PERFORMANCE_EVIDENCE.md` last.  Existing
  identical bytes are idempotent and any conflicting path, digest, candidate,
  protocol, source, artifact, count, status, or activation fails closed.  Add a deep
  provider-free verifier that never executes a workload: it reparses every referenced
  typed record, reconstructs the five auxiliary results and 10,000 unique complete
  work-unit/result/artifact/audit tuples, re-evaluates attempts, resource/retry rules,
  semantic/CAS identities, thresholds, throughput, artifact accounting, aggregate,
  and evidence checks, and rereads the activation and immutable identities at the
  end.  Keep `release/performance_thresholds.toml` byte unchanged and mechanically
  regenerate only `release/performance_runner_sources.lock` after the repair source
  is final.
- Owned repair paths: `kirby2/release/performance_execution.py`,
  `kirby2/release/performance_worker.py`, `kirby2/release/performance_records.py`,
  `kirby2/release/performance_auxiliary.py`,
  `kirby2/simulation/queue_reactive.py`,
  `kirby2/release/commands.py`, `kirby2/release/__init__.py`,
  `kirby2/audit/release.py`, `kirby2/audit/expansion.py`,
  `release/performance_runner_sources.lock`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0016` through K2X-02 immediately after completed
  `WO40-H` and before resumed `WO40-I`; the release sequence is exactly
  `DEV-0014`, `WO40-G`, `DEV-0015`, `WO40-H`, `DEV-0016`, `WO40-I`, and
  `WO40-J`, and the deviation inventory extends monotonically through
  `DEV-0016`.
- Inherited gates: candidate/history immutability; five frozen protocol files;
  exact performance manifest bytes; row corpus, cell/root/order, generated
  configurations, native fixtures, runner sources, capability/check arrays, semantic
  artifact tuple, four-worker/256-FIFO resources, retry count, attempt/total limits,
  auxiliary sample counts, reduction formulas, thresholds, aggregate accounting,
  designated installed target, artifact identities, product limitations, and every
  earlier production contract remain unchanged.  This amendment authorizes only the
  closed qualification executor, immutable publication, and provider-free verifier;
  it does not authorize a performance workload, threshold observation, threshold
  change, brokerage, live-market connection, credential creation, telemetry,
  updater, background service, network download, GPU workload, or final closeout.
  After repair source and the mechanically regenerated source lock commit cleanly,
  that commit is the sole repaired candidate; WO40-F is rebuilt from it, WO40-G and
  WO40-H are requalified against it in order, and only then may canonical WO40-I
  execute the already committed performance protocol.
- Exact commit subject: `Execute release performance qualification`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0013
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0014
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0015
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0016
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q kirby2
git diff --check
```

Acceptance: the canonical commands route to the installed-artifact qualification
policy instead of reporting readiness; one verified row executor, exact
four-worker/256-FIFO coordinator, and five auxiliary adapters produce immutable typed
records and publish a single activation only after a fully reconstructed aggregate;
the deep verifier reconstructs all counts, identities, attempts, thresholds, and
evidence without executing a workload; the performance-threshold manifest remains
byte identical; the complete registry contains DEV-0001 through DEV-0016 in the
required release order; the `627d6a4` V2 historical snapshot remains immutable; and
the deviation gate proves the public source seams, execution policy, command routing,
publication/deep-verification surface, and frozen threshold bytes without importing
the performance executor, starting a worker, opening a provider, or running any
performance or auxiliary workload.

## DEV-0017 — Repair measured performance failures and restart closeout

- Interrupted canonical card: `WO40-J`; closeout remains interrupted until the
  immutable failed `WO40-I` result is preserved, its measured failures are repaired,
  and `WO40-F`, `WO40-G`, `WO40-H`, and `WO40-I` are requalified in that order.
- Exact first-parent predecessor:
  `8ee892575372c3e296454ae6c3b2b991e481699e`
- Historical release: the predecessor is a deeply verified `WO40-I` evidence commit
  for source candidate `10b0d205ce0efdeff5e4e833c7cbfa808ccaf1cc`.
  Its `WO40-F`, `WO40-G`, and `WO40-H` evidence is `PASS`; its `WO40-I`
  evidence is an honest terminal `FAIL`, not an executor, publication, or verifier
  failure.  Before any repaired candidate replaces the active store, preserve the
  current `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`, all four F-I evidence
  documents, and the complete active `.kirby2/release` tree at
  `.kirby2/release-history/8ee892575372c3e296454ae6c3b2b991e481699e/`.
  The V3 history manifest records each gate's actual status, inventories every
  regular file, and binds every size, SHA-256, and historical mode.  It may not
  reduce the snapshot to the evidence documents' immediate records: the WO40-I
  activation depends on its aggregate, attempt, auxiliary results, all 10,000
  work-unit/result/artifact/audit publications, and their content-addressed objects.
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-I`
- Observed terminal result: `RELEASE_AUDIT FAIL` for a deeply verified, canonically
  published performance result.  All twelve row cells, exactly 10,000 unique
  complete work units, full result/artifact/audit accounting, deterministic
  aggregate, retry policy, interactive ACK, and full-day replay passed.  The
  terminal-update auxiliary failed its workload invariant; microscope load exceeded
  its preregistered latency threshold; and full-day generation retained one
  nonblocking preregistered ledger-growth warning.  The warning remains evidence and
  is not a DEV-0017 failure or permission to redesign the ledger.
- Root cause — rerun lifecycle: the canonical WO40-I public root is activation-once
  and correctly refuses an occupied destination.  The failed result therefore cannot
  be overwritten, deleted, or reinterpreted as a repaired attempt.  The prior local
  history process copied selected payloads and had no typed status for a failed gate;
  applying that process to this approximately 4.5-GiB active store would either omit
  deep evidence or perform an unnecessary bulk copy.
- Root cause — terminal contract: the V1 terminal-update input treated each delivered
  market-state message as a distinct visible terminal update and required 5,100 of
  them after continuous-session start.  The pinned root-3102000 immutable source has
  only 592 outer events and 41 client-delivered market-state messages in total.
  Only 39 eligible messages occur after continuous-session start, so it cannot
  contain 5,100
  distinct delivered changes.
  This is a structurally impossible input/sample contract, not a latency threshold
  miss and not authority to substitute a different unregistered workload after
  observing the result.
- Root cause — microscope context: each fresh-process repetition sought twice and
  then inspected the same immutable full-day run through independent store calls.
  Each public call repeated complete verification and reload work, so the measured
  operation paid several full integrity traversals instead of one verified context
  followed by bound-plan access, one midpoint seek, pane inspection, and report
  render.
- Root cause — measurement clock: the auxiliary executor used
  `time.monotonic_ns` for measured wall and latency samples even though canonical
  section 5.7.8 explicitly requires `time.perf_counter_ns` in the measured fresh
  process.  Coordinator attempt/total deadlines are a different operational
  authority and correctly remain monotonic.
- Repair — history lifecycle: add
  `KIRBY2_RELEASE_HISTORY_ATOMIC_RENAME_V1` and the strict
  `KIRBY2_RELEASE_HISTORY_SNAPSHOT_V3` manifest.  Under one exclusive local lock,
  pin and exclusively lock the same no-follow active-store directory inode used by
  release writers, inventory and hash every active file, independently reparse all
  five predecessor public evidence documents from the exact Git object, record
  D1/F/G/H `PASS` and I `FAIL` as typed manifest rows, and verify every immediately
  referenced active-store anchor.  Execution additionally requires a clean checked-out
  D1 commit with exact subject `Reverify release resources for DEV-0017`, whose sole
  first parent has exact subject `Repair measured release performance failures` and
  whose parent is the failed predecessor; the ordinary candidate verifier must bind
  that D1 commit, the regenerated source lock, and the source parent's protocol bytes.
  Stage only those small evidence documents and an exact
  `clean-providers.toml` for the next active store.  Require the active, history,
  and next-store parents to share
  one filesystem; use Darwin's atomic no-replace rename to move the complete old active directory beneath a
  hidden history stage, atomically activate the config-only next directory, harden
  and fsync the moved history, deeply verify its exact post-quarantine inventory,
  and atomically publish and reverify its final commit-named root.  Both parents of
  every cross-directory rename are fsynced.  There is no recursive copy, overwrite,
  active-store deletion, history deletion, or partial-history fallback.  Before the
  active store moves, an interrupted `.building` root may be recovered only by
  no-follow inspection, unlinking its bounded known small staging files, and removing
  the now-empty directory; no recursive cleanup exists.  Every
  interruption has one typed visible state; a missing active root is unavailable,
  never evidence that two publications are active, and ambiguous staging refuses
  pending explicit recovery.
- Repair — terminal contract: retain workload and sample IDs, the exact source
  artifact, terminal geometry/encoding/drain policy, 100 warmup and 5,000 measured
  visible updates, and the existing latency reductions and numeric thresholds.
  Amend the input identity with explicit
  `RELEASE_TERMINAL_PRESENTATION_POLICY_V2`: a 50-ms production presentation clock
  advances simulation by 500,000 microseconds per tick at speed-milli 10,000 over
  the include-start/exclude-end continuous interval.  Under exact causal policy
  `LATEST_NONREGRESSING_CLIENT_VISIBLE_MARKET_STATE_AT_OR_BEFORE_TICK_V1`, each tick
  consumes every market-state delivery at or before that tick in nondecreasing
  delivery order.  A candidate replaces the client-visible cut only when its source
  market time is at least the currently selected source market time; a later-delivered
  stale cut remains raw delivery evidence and never rolls presentation backward.
  Positive raw delivered source message IDs are unique, but asynchronous delivery may
  expose them out of creation order.  Evidence therefore records the complete raw
  delivered source-sequence array plus its count and reorder count separately from
  presented source-sequence reuse and reorder counts.  Tick ordinals are contiguous;
  visible-update ordinals are contiguous; and only an adjacent rendered-frame SHA-256
  change becomes a visible sample.  `TerminalFramePresenterV2` owns rendering and
  synchronous frame flush; release desktop adapters own tick generation and causal
  snapshot selection.  Exact latency boundary
  `RENDER_HASH_WRITE_AND_DRAIN_V1` starts immediately before rendering and ends only
  after the synchronous sink write-and-drain return.  Every V2 update row binds its
  frame SHA-256 and ordered frame-digest-chain SHA-256; the PTY independently records
  complete written- and drained-stream SHA-256 values plus the same payload-derived
  frame chain.  Deep verification recomputes every chain link from the ordered row
  inventory, requires its tail to equal the PTY chain, requires written and drained
  byte counts to equal the sum of row bytes, and requires written and drained stream
  digests to match.  A legacy template without the V2 `presentation` object remains
  the exact historical V1 branch, including its original `sha256` receipt and verifier
  grammar, and cannot be silently reinterpreted.  The default release-protocol loader
  remains closed to that legacy shape and accepts only active V2.  The DEV-0011
  detached historical audit must explicitly select the private typed
  `HISTORICAL_TERMINAL_V1` compatibility path, which removes exactly the unique
  terminal template's generated V2 `presentation` member and leaves every other
  template, threshold, and protocol field exact.  The active performance executor
  pins the new exact V2 threshold-manifest SHA-256.  DEV-0016 independently reads the
  predecessor threshold TOML from its exact Git object, removes only the current
  production presentation identity, requires that identity to equal
  `RELEASE_TERMINAL_PRESENTATION_POLICY_V2`, and requires the complete remaining
  object to equal the predecessor; numeric thresholds, counts, formulas, and all
  unrelated protocol identities therefore remain frozen.
- Repair — microscope context: add `VerifiedFullDaySessionV1` and
  `FullDayStore.open_verified_day(run_id)`.  Each of the unchanged one warmup and
  twenty measured fresh processes opens exactly one verified immutable session, then
  derives the continuous bounds from its bound `.plan`, performs one midpoint
  `.seek(target_time_us)`, and uses `.inspection()` and `.verification` for all
  supported panes and the complete standalone report.  Existing independent
  `verify_day`, `seek`, and
  `inspect_day` contracts remain available, and the repair removes only a duplicate
  post-restore invariant pass after the store has already established the same
  verified state.
- Repair — composed restore fixed point: `FullDayRuntime` is the sole owner of the
  private identity token `_NESTED_RESTORE_CONSTRUCTION_TOKEN`.  During only its
  composed `from_checkpoint_state` path, it passes that exact object to private
  mechanics-engine and agent-scheduler restore entrypoints.  A forged or merely equal
  token refuses.  The token defers only each child's final canonical reserialization
  comparison: strict JSON parsing, exact fields, digest checks, and child invariants
  remain unconditional.  Public standalone child `from_checkpoint_state` entrypoints
  always pass `None` and retain their own canonical fixed point.  After all children
  are composed, `FullDayRuntime` retains one complete outer comparison of
  `canonical_json_bytes(runtime.checkpoint_state())` with the original payload, so no
  child state escapes the complete runtime fixed point.
- Repair — clocks and release restart: use `time.perf_counter_ns` only for the five
  auxiliary workloads' measured wall/latency start and end samples, including
  terminal frame presentation; retain monotonic clocks for coordinator deadlines,
  worker lifetime bounds, retry admission, and total qualification timeout.  After
  the terminal/microscope/clock repair is final, mechanically regenerate
  `release/performance_runner_sources.lock` from the complete staged source tree.
  Because the V2 terminal input identity changes
  `release/performance_thresholds.toml`, source/protocol repair must first commit
  under the exact subject `Repair measured release performance failures`.  Only
  after that commit exists, run the exact read-only canonical command
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py release-resource-preflight --platforms release/platforms.toml --lock release/requirements.lock --qualification release/qualification.toml --no-network --output KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`.
  The regenerated report must bind the new exact protocol-set SHA-256 and resolve
  that source/protocol commit as the newest first-parent protocol-owning commit;
  `audit-expansion --gate WO40-D1` must then pass.  Commit only the exact regenerated
  `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md` under the unique subject
  `Reverify release resources for DEV-0017`.  That D1 evidence commit, whose parent
  already contains the mechanically regenerated source lock, becomes the sole clean
  repaired candidate.  This two-commit order is mandatory: generating the report
  before the protocol repair is committed would bind the predecessor protocol
  owner, while changing source after the lock is generated would invalidate the
  candidate.  Execute the history rollover; rebuild
  `WO40-F`; requalify `WO40-G` on disposable host-only Tart clones; requalify
  `WO40-H` on the pinned Fedora SSH provider; execute canonical `WO40-I` into its
  newly empty public root; and resume `WO40-J` only if the new deeply verified result
  satisfies its preregistered status contract.
- Owned repair paths: `kirby2/release/history.py`,
  `kirby2/agents/ecology.py`, `kirby2/exchange/mechanics_engine.py`,
  `kirby2/release/build.py`,
  `kirby2/release/performance.py`, `kirby2/release/performance_execution.py`,
  `kirby2/release/performance_auxiliary.py`,
  `kirby2/release/desktop.py`, `kirby2/ui/terminal.py`,
  `kirby2/full_day/store.py`, `kirby2/full_day/runtime.py`,
  `kirby2/audit/release.py`, `kirby2/audit/expansion.py`,
  `release/performance_thresholds.toml`,
  `release/performance_runner_sources.lock`,
  `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0017` through K2X-02 immediately after failed `WO40-I`
  and before resumed `WO40-J`; the deviation inventory extends monotonically through
  `DEV-0017`.  The failed predecessor remains historical evidence and does not
  satisfy the new candidate's WO40-I prerequisite.
- Inherited gates: release targets, artifact IDs and member layout, dependency pins,
  launchers, documentation, starter content, qualification rows and check order,
  four-worker/256-FIFO resources, 10,000-row corpus, retry count, attempt/total
  limits, auxiliary repetition counts, reduction formulas, all numeric performance
  thresholds, designated installed target, product limitations, and every earlier
  production contract remain unchanged.  No brokerage, live-market connection,
  credential creation, telemetry, updater, background service, network download,
  GPU workload, provider operation, history rollover, build, qualification, or
  performance workload is executed by the deviation audit.
- Exact source/protocol repair commit subject:
  `Repair measured release performance failures`
- Exact D1 evidence commit subject: `Reverify release resources for DEV-0017`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0011
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0012
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0013
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0014
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0015
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0016
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0017
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q kirby2
git diff --check
```

Acceptance: every byte through DEV-0016 is preserved as the exact predecessor
ledger prefix; the registry contains DEV-0001 through DEV-0017 in release order;
the predecessor Git evidence reconstructs D1/F/G/H `PASS`, I `FAIL`, both measured
failures, the retained full-day warning, and all 10,000 complete records; the V3
history contract binds the complete active store, all five documents, and exact gate
statuses; its executor uses the shared active-store lock, exclusive no-replace
renames, exact final re-verification, and only bounded pre-move staging cleanup while
exposing no bulk copy, recursive deletion, overwrite, provider, or workload surface;
terminal presentation, raw-delivery versus presented-sequence evidence, ordered
frame-chain and PTY stream-digest reconciliation, one-context microscope access,
sole-owner composed-restore deferral with complete public/outer fixed points, and
auxiliary `perf_counter_ns` samples are explicit production contracts while
coordinator deadlines remain monotonic; numeric thresholds are unchanged; the
repaired candidate has a mechanically regenerated source lock and resource preflight;
F/G/H/I are requalified in order; and no audit, planning call, or history verifier
mutates the active store or executes the one-time rollover.

## DEV-0018 — Repair V2 performance publication verification and restart qualification

- Interrupted release sequence: the DEV-0017 requalification of `WO40-I` refused
  before activation; canonical `WO40-J` remains blocked until this verifier repair
  is committed and `WO40-F`, `WO40-G`, `WO40-H`, and `WO40-I` are requalified in
  that order.
- Exact first-parent predecessor:
  `901e31c3e7d7a2ce5a423011e36d363440f20cc2`
- Superseded source candidate:
  `a198c69426551b8d2f44269cbdc82980a8978b03`.  Its current active release store
  contains the immutable WO40-F artifacts plus passing WO40-G and WO40-H attempt
  records.  No WO40-I public root, activation, attempt, aggregate, auxiliary result,
  or performance evidence document for this candidate exists.
- Reproducer:
  `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py qualify-performance --manifest release/performance_thresholds.toml --complete-run-work-units 10000 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release`
- Observed terminal result: all four fixed workers completed the preregistered
  10,000-work-unit corpus on attempt 1 and the coordinator entered aggregate
  verification, then returned typed refusal `PERFORMANCE_VERIFICATION_FAILED` with
  detail `terminal update receipt fields differ from the V1 schema`.  The disposable
  executor root was cleaned, atomic activation left the public WO40-I root absent,
  and the active D1/F/G/H store and tracked tree remained unchanged.  This is an
  operational verifier defect, not a measured threshold result, and it grants no
  authority to synthesize WO40-I evidence or recover unpublished temporary bytes.
- Root cause: the active terminal template contains the exact committed
  `RELEASE_TERMINAL_PRESENTATION_POLICY_V2` member.  The workload dispatcher and
  primary auxiliary verifier therefore correctly select V2, whose top-level receipt
  additionally carries `first_update_ordinal`, `last_update_ordinal`, `presentation`,
  and `presentation_feasibility`, and whose PTY receipt carries separate written,
  drained, and frame-chain SHA-256 values.  The independent release-publication
  verifier in `performance_records.py` nevertheless exact-matched every terminal
  receipt and PTY receipt against only the historical V1 shapes.  The already
  validated V2 bytes were consequently rejected before activation.
- Repair — receipt verification: select the secondary terminal receipt grammar only
  from the frozen template parameters.  A template without `presentation` retains
  the exact historical V1 top-level and `sha256` PTY shapes.  A template whose
  `presentation` member equals `RELEASE_TERMINAL_PRESENTATION_POLICY_V2` requires the
  exact V2 top-level, feasibility, and PTY shapes; independently validates nonzero
  equal written/drained byte counts, all three SHA-256 values, matching written and
  drained stream digests, fixed terminal geometry/encoding/drain policy, presentation
  identity, update ordinals/count, status, source materialization, and inventory
  digest; and continues to rely on the existing primary V2 verifier for complete row,
  cadence, causality, frame-chain, series, and threshold reconciliation.  Unknown or
  malformed presentation members refuse.  No producer, public record schema,
  workload, sample, reduction, threshold, resource, or product behavior changes.
- Repair — incomplete-candidate history: add the typed
  `KIRBY2_RELEASE_HISTORY_SNAPSHOT_V4` profile without changing or reinterpreting the
  existing DEV-0017 V3 snapshot.  The V4 manifest records D1/F/G/H as `PASS` and
  WO40-I as `NOT_RUN`, inventories the exact four D1/F/G/H evidence documents from
  the predecessor Git object, and inventories every regular file in the active
  release store.  It requires WO40-I publication paths to be absent and verifies all
  immediate F/G/H and build anchors.  Under the existing exclusive global and
  active-store locks, the executor uses same-filesystem no-replace directory renames
  to quarantine the complete active store, activate an exact config-only replacement,
  harden and deeply verify the quarantined snapshot, and publish it at
  `.kirby2/release-history/901e31c3e7d7a2ce5a423011e36d363440f20cc2/`.
  Both rename parents are fsynced.  Interruption states are typed and resumable;
  ambiguous paths refuse; there is no bulk copy, recursive deletion, overwrite, or
  partial-history fallback.
- Repair — release restart: regenerate
  `release/performance_runner_sources.lock` mechanically after the verifier,
  history, deviation, and audit repairs are final.  Commit the complete source repair
  under exact subject `Repair WO40-I V2 publication verification`.  Only after that
  commit exists, run the canonical no-network resource preflight.  This repair changes
  no release-protocol or resource input, so the renderer must reproduce the tracked D1
  report byte-for-byte with frozen protocol owner
  `020da2c90c0f0000f822aad7c66538fe68c6c6e6`; any changed rendering refuses and
  requires new authority.  Create an intentional empty candidate-boundary commit
  under exact subject `Reverify release resources for DEV-0018`.  It changes no tree
  path, directly parents the source repair, and truthfully reuses the still-canonical
  report rather than fabricating different evidence bytes.  That D1 boundary commit
  becomes the new candidate.  Execute the V4 rollover, rebuild WO40-F, requalify
  WO40-G in disposable host-only Tart clones, requalify WO40-H on the pinned Fedora
  SSH provider, and rerun canonical WO40-I into the empty public root.  Resume WO40-J
  only after the new WO40-I publication deeply verifies and its preregistered status
  satisfies closeout.
- Owned repair paths: `kirby2/release/performance_records.py`,
  `kirby2/release/history.py`, `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `release/performance_runner_sources.lock`,
  `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0018` appears immediately after `DEV-0017` and before
  resumed `WO40-J`; the deviation inventory extends monotonically through
  `DEV-0018`.  The superseded candidate's D1/F/G/H evidence remains historical and
  does not satisfy the new candidate's prerequisites.
- Inherited gates: every DEV-0017 production, terminal-presentation, history,
  qualification, performance, safety, and product-limitation contract remains in
  force.  Release targets, artifact/member layout, dependency pins, launchers,
  documentation, starter content, qualification rows/check order, four-worker and
  256-FIFO resources, 10,000-row corpus, retry/timeout limits, auxiliary repetitions,
  reductions, numeric thresholds, and designated installed target are unchanged.
  No brokerage, live-market connection, credential creation, telemetry, updater,
  background service, network download, GPU workload, provider operation, history
  rollover, build, qualification, or performance workload is executed by the
  deviation audit.
- Exact source repair commit subject:
  `Repair WO40-I V2 publication verification`
- Exact D1 evidence commit subject: `Reverify release resources for DEV-0018`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0017
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0018
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q kirby2
git diff --check
```

Acceptance: the ledger prefix through DEV-0017 is byte-preserved; DEV-0018 is the
only appended authority record; active V2 and detached historical V1 terminal
receipts both receive exact independent deep verification; malformed or cross-version
receipts refuse; the V4 snapshot truthfully distinguishes absent WO40-I from a
measured `FAIL` and preserves the complete superseded active store by atomic rename;
the repaired source lock and D1 report bind the new candidate; F/G/H/I are
requalified in order; and no audit or planning call mutates the active store or runs
the one-time rollover.

## DEV-0019 — Repair inherited closeout qualification gates and restart closeout

- Interrupted canonical card: `WO40-J`.  The first complete closeout aggregate
  re-exercised the inherited implementation frontier and found eight failing gates:
  `WO37-C`, `WO37-E`, `WO38-B`, `WO38-C`, `WO38-D`, `WO38-E`, `WO39-D1`, and
  `WO39-D2`.  Closeout remains interrupted until those production/audit defects are
  repaired and `WO40-F`, `WO40-G`, `WO40-H`, and `WO40-I` are requalified in that
  order for a new candidate.
- Exact first-parent predecessor:
  `81317d731c0d9d1c38370a4f69b61890e97dac74`
- Superseded source candidate:
  `3818dc83c4c031ea99137f04909a595031ab6e52`.  Its active release store contains
  the complete immutable D1/F/G/H/I publication.  The five public documents record
  D1/F/G/H as `PASS` and WO40-I as `PASS_WITH_WARNINGS`; the performance warning is
  retained truthfully and is neither promoted to `PASS` nor reinterpreted as a
  failure.
- Observed closeout failures: causal-claim validation selected the stronger design
  validator before producing the promised domain-specific refusal; exact-class
  checks rejected platform-native `PosixPath` values; eager worker import polluted
  `python -m` subprocess stderr; one refusal-text audit expected `differs` instead
  of the production contract's `differ`; the recovery metrics reader applied the
  float-forbidden pack-identity JSON grammar to finite operational metrics; and the
  training-pack command audit treated its owned command set as the entire later CLI
  surface.  `WO38-D` additionally needs real loopback sockets to exercise its local
  authenticated-LAN fixture; a socket-restricted sandbox refusal is not a product
  failure and does not authorize broader network access.
- Repair — `WO37-C`: preserve the stronger design validator for executable study
  designs, but validate causal-claim capability first so unsupported causal claims
  return their stable domain-specific refusal without weakening design validation.
- Repair — `WO37-E` and `WO39-D2`: accept ordinary platform-native `Path`
  implementations at the public path boundary while preserving absolute/root,
  traversal, symlink, ownership, and immutable-evidence checks.
- Repair — `WO38-B` and `WO38-E`: replace direct runpy worker re-execution with the
  fixed `-P -s -B -m kirby2 orchestrate worker` declarative action.  Sanitize ambient
  Python startup controls, pin the measured checkout import root, and preserve the
  explicit hash seed, no-bytecode policy, and clean startup stream.
- Repair — `WO38-C`: align the audit with the exact established production refusal
  text; no production refusal code or transfer behavior changes.
- Repair — `WO38-D`: apply the same platform-native path correction while retaining
  loopback-default binding, explicit LAN opt-in, TLS 1.3 mutual authentication,
  certificate pinning, no plaintext fallback, lease ownership, and sealed-stage
  controls.  The resumed WO40-J audit is authorized only for host loopback socket
  creation and connection by the existing WO38-D fixture.  It grants no Internet,
  non-loopback LAN, provider, credential, brokerage, or persistent-service access.
- Repair — `WO38-E`: decode operational metrics with the strict finite-number JSON
  grammar rather than the pack-identity grammar, continuing to reject NaN, infinity,
  duplicate keys, malformed values, and noncanonical identity-bearing payloads.
- Repair — `WO39-D1`: require its exact owned training-pack commands as an ordered
  subset of the declarative registry so the later authorized `pack-portability-demo`
  command remains valid; reordered or missing owned commands still fail.
- Repair — complete-candidate history: add the typed
  `KIRBY2_RELEASE_HISTORY_SNAPSHOT_V5` profile without changing or reinterpreting
  DEV-0017 V3 or DEV-0018 V4.  V5 binds exact predecessor
  `81317d731c0d9d1c38370a4f69b61890e97dac74`, exact source candidate
  `3818dc83c4c031ea99137f04909a595031ab6e52`, all five D1/F/G/H/I documents,
  the actual `PASS`, `PASS`, `PASS`, `PASS`, `PASS_WITH_WARNINGS` gate projection,
  and every regular file in the active release store.  Under the existing exclusive
  global and active-store locks, the one-time executor uses same-filesystem
  no-replace directory renames to quarantine the complete active store, activate an
  exact config-only replacement, harden and deeply verify the quarantined snapshot,
  and publish it at
  `.kirby2/release-history/81317d731c0d9d1c38370a4f69b61890e97dac74/`.
  Both rename parents are fsynced.  Interruption states are typed and resumable;
  ambiguous paths refuse; the active payload has no copy, recursive delete,
  overwrite, or partial-history path.
- Repair — release restart: regenerate
  `release/performance_runner_sources.lock` mechanically only after the gate,
  history, deviation, and audit repairs are final.  Commit the complete source repair
  under exact subject `Repair inherited closeout qualification gates`.  Only after
  that commit exists, run the canonical no-network resource preflight.  The repair
  changes no release protocol or resource input, so the renderer must reproduce
  tracked `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md` byte-for-byte with frozen protocol
  owner `020da2c90c0f0000f822aad7c66538fe68c6c6e6`; any byte change refuses and
  requires new authority.  Create an intentional empty candidate-boundary commit
  under exact subject `Reverify release resources for DEV-0019`; it changes no tree
  path, directly parents the source repair, and becomes the new candidate.  Execute
  V5, rebuild WO40-F, requalify WO40-G in disposable host-only Tart clones,
  requalify WO40-H on the pinned Fedora SSH provider, and rerun canonical WO40-I
  into the empty public root before resuming WO40-J.
- Owned repair paths: `kirby2/instructor/statistics.py`,
  `kirby2/instructor/commands.py`, `kirby2/orchestration/local.py`,
  `kirby2/orchestration/commands.py`, `kirby2/orchestration/security.py`,
  `kirby2/orchestration/lan.py`, `kirby2/orchestration/aggregation.py`,
  `kirby2/audit/orchestration.py`, `kirby2/audit/packs.py`,
  `kirby2/release/history.py`, `kirby2/audit/release.py`,
  `kirby2/audit/expansion.py`, `release/performance_runner_sources.lock`,
  `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md`, and
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md`.
- Gate registration: `DEV-0019` appears immediately after `DEV-0018` and before
  resumed `WO40-J`; the closeout deviation inventory extends monotonically through
  `DEV-0019`.  The superseded candidate's D1/F/G/H/I evidence remains historical and
  cannot satisfy the new candidate's prerequisites.
- Inherited gates: every DEV-0018 production, terminal-presentation, history,
  qualification, performance, safety, and product-limitation contract remains in
  force.  Release targets, artifact/member layout, dependency pins, launchers,
  starter content, qualification rows/check order, four-worker and 256-FIFO
  resources, 10,000-row corpus, retry/timeout limits, auxiliary repetitions,
  reductions, numeric thresholds, and designated installed target are unchanged.
  No brokerage, live-market connection, credential creation, telemetry, updater,
  background service, network download, GPU workload, provider operation, history
  rollover, build, qualification, performance workload, or loopback fixture is
  executed by the DEV-0019 deviation audit.
- Exact source repair commit subject: `Repair inherited closeout qualification gates`
- Exact D1 evidence commit subject: `Reverify release resources for DEV-0019`

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0017
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0018
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate DEV-0019
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO37-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO37-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO38-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO38-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO38-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO38-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO39-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO39-D2
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate WO40-D1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -I kirby2/__main__.py audit-expansion --gate K2X-02
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q kirby2
git diff --check
```

Acceptance: the ledger prefix through DEV-0018 is byte-preserved and DEV-0019 is
the only appended authority record; all eight inherited gates pass for their repaired
production contracts; V5 preserves the five exact predecessor documents plus the
complete active store and retains WO40-I `PASS_WITH_WARNINGS`; V3 and V4 remain
byte-semantically unchanged; the rollover is explicit-only, rename-only for the
active payload, fail-closed, and never invoked by an audit; the repaired source lock
and byte-identical D1 report bind the new candidate; F/G/H/I are requalified in
order; WO38-D alone receives narrowly scoped host-loopback authority during resumed
closeout; and no audit or planning call mutates the active store or executes the
one-time rollover.
