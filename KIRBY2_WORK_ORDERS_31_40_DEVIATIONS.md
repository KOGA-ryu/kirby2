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
