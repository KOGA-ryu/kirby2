# Kirby2 simplification and Python data-oriented-design audit

- **Status:** architecture analysis; advisory and non-canonical
- **Repository snapshot:** `d6cf2b5` (`Separate learner identity from evidence`)
- **Analysis date:** 2026-08-30
- **Scope:** simplification, reduction, and Python-first data-oriented design
- **Not authorized by this document:** a work-order deviation, a wire-format change,
  an evidence rewrite, or a native-language migration

## 1. Executive decision

Kirby2 is not at a general Python language limit. It has two more specific
problems:

1. The hot simulation path repeatedly materializes, copies, serializes, sorts, and
   revalidates state whose size grows with the run history.
2. The control and evidence layers hand-write a large amount of mechanically similar
   wire-schema code.

Those problems need different treatments.

- Apply runtime data-oriented design to the order book, mechanics synchronization,
  the full-day work queue, append-only event ledgers, rollback state, and repeated
  analytical scans.
- Apply declarative specifications to wire codecs, record shapes, artifact metadata,
  standard audit-case projection, and future command registration.
- Keep object-rich, strongly named domain contracts at public, persistence, review,
  and authority boundaries.
- Do not collapse identity families, provenance classes, consent records, review
  authority, source versus derived evidence, or replay truth into generic records.
- Do not start with C++, NumPy, Arrow, an ECS, or a repository-wide rewrite. The
  current evidence points to algorithmic and representation costs in Python, not to
  insufficient memory capacity or a proven native-code requirement.

The highest-value first engineering lane is:

1. measure the current HEAD by named operation;
2. remove successful-path full-state transaction copies;
3. stop constructing whole-book detached views for internal consumers;
4. replace repeated whole-history checks with incrementally maintained witnesses,
   while retaining the complete forensic replay verifier;
5. then convert the order/work/event kernels to stable integer handles and typed
   columns.

This lane must be separately authorized under the work-order amendment protocol.
The canonical sequence currently permits exactly one card at a time, and the next
card is WO37-B. A broad architecture refactor must not be smuggled into that card.

## 2. What was read and measured

The audit covered the canonical project specification, the execution roadmap, the
WO31-40 execution contract, package READMEs, the full-day runtime and store, exchange
book and mechanics engine, research store and table schemas, CLI registration,
expansion audits, and the record-heavy packages.

Static inventory at the snapshot:

| Measurement | Result | Interpretation |
|---|---:|---|
| Python source | 247,745 lines in 291 files | Large, but not all of it is runtime product code. |
| `kirby2/audit` | 59,018 lines, 23.8% of Python | Much of the size is executable evidence and adversarial verification. |
| `kirby2/full_day` | 43,031 lines, 17.4% | The largest product/runtime concentration. |
| `kirby2/microscope` | 21,355 lines | A second substantial validation and projection surface. |
| Dataclasses | 836 | All use slots; 812 are frozen. The count alone is not a defect. |
| Dataclass record mechanics | about 29,635 lines | `as_dict`, `from_dict`, and `__post_init__` account for about 12% of the tree. |
| Dataclass encoders | 650 methods / 6,680 lines | Strong candidate for declarative reduction. |
| Dataclass decoders | 227 methods / 7,742 lines | Compatibility-sensitive because accepted coercions differ. |
| Dataclass post-init validation | 631 methods / 15,213 lines | A mixture of mechanical checks and essential domain semantics. |
| Enums | 270 | Appropriate for many closed wire vocabularies; not a hot-path representation. |
| Canonical-JSON helpers | 40 definitions across 24 files | The behavior is similar but not universally identical. |
| Exact-field/scalar wire helpers | 19 field helpers and 105 scalar decoders | Repeated substrate, with legacy acceptance differences. |
| Expansion audit gate functions | 52 | Forty-eight follow a recognizable case-to-check projection. |
| Legacy CLI command branches | 54 | New commands already have a registry; legacy compatibility is frozen. |
| Runtime dependency set | DuckDB only | A stdlib-first DOD prototype does not require a new dependency. |

The largest individual source files are `kirby2/audit/full_day.py` (17,816
lines), `kirby2/audit/replay_microscope.py` (7,652),
`kirby2/full_day/runtime.py` (7,548), `kirby2/microscope/report.py` (5,238),
and `kirby2/audit/drill_mining.py` (5,003). Large audit files should be judged by
truth coverage and duplication, not by production LOC targets.

### 2.1 Existing performance evidence

The latest governed result is summarized in
`KIRBY2_FULL_DAY_QUALIFICATION_EVIDENCE.md`; its ignored raw artifact is
`.kirby2/full_day/qualification/runs/run-bf21e61c4b0d50862f901be1/performance.json`.
It was created for commit `1a4bbebd576487bff0d9088bc77ecf726bb47bc5`, not the
current snapshot, so it is a diagnostic clue rather than a current benchmark.

Its three measured generations report:

| Metric | Persisted result | Status in artifact |
|---|---:|---|
| Outer events per measured run | 592 | workload observation |
| Generation P50 | 78.598 seconds | PASS |
| Replay P50 | 27.941 seconds | PASS |
| Generation throughput | 7 events/second | FAIL |
| Peak RSS | 115,818,496 bytes | PASS |
| Largest checkpoint | 1,455,091 bytes | PASS |
| Complete run bytes | 8,345,643 bytes | PASS |

That shape matters: memory and artifact size are already comfortably bounded, while
CPU time per event fails. The immediate problem is repeated work, not that the data
set is too large to fit in Python memory.

The canonical performance policy remains authoritative. Operational profiling data
must stay outside semantic identity, and thresholds must never be relaxed after a
result is observed.

## 3. Complexity that must not be removed

Kirby2 deliberately pays for guarantees that ordinary application code does not
have. These are product semantics, not accidental bloat:

- integer ticks, quantities, fixed-point ratios, and exact microsecond clocks;
- deterministic ordering, owned seeded RNG state, and stable substream derivation;
- FIFO matching, auction/session rules, latency, venue, and agent ownership;
- append-only evidence, exact event sequence, checkpoint restore, and replay parity;
- observable, reveal-only, and ground-truth separation with no lookahead;
- distinct source, semantic, artifact, run, operational-attempt, and review-sidecar
  identities;
- source evidence versus derived sidecars and explicit provenance classes;
- fail-closed hostile-input, consent, pseudonym, and reviewer-authority boundaries;
- automated status kept separate from human acceptance.

Any reduction that merges these distinctions is a semantic regression even if it
removes many lines.

The safe simplification question is therefore not “can this become one generic
record?” It is “can repeated mechanics be driven by an explicit table without
changing the named domain contract or one accepted/refused byte?”

## 4. Where complexity is accidental

### 4.1 Full-history work in the success path

`FullDayRuntime.advance_to()` calls `_capture_transaction_state()` before processing
work. That capture:

- checkpoints the exchange engine and every active component owner;
- hashes the engine checkpoint;
- copies the heap, pending/executed/retired work dictionaries, event ledger, native
  ledger, sequence maps, time indexes, lists, and sets;
- retains all owner identities so a failure can reconstruct and transplant complete
  owner graphs.

At the requested target and at checkpoint work, `assert_invariants()` performs broad
prefix reconciliation and replay checks. Cost therefore grows with accumulated
history even when the new delta is small and succeeds.

The guarantee—failure-atomic public advance—is essential. The successful-path copy
mechanism is not.

### 4.2 Repeated exchange serialization and detached views

`FullDayRuntime._handle_agent_work()` serializes the complete mechanics engine both
before and after each agent item to prove whether a stage mutated exchange state.

`OrderBook.active_orders`, `all_orders`, `bids`, and `asks` each construct a new
dictionary of detached view objects and wrap it in `MappingProxyType` on every
access. Internal flow, scheduler, feature, and mechanics code repeatedly consumes
those properties.

`MarketMechanicsEngine._sync_continuous_orders()` obtains a complete `all_orders`
projection and scans the sorted managed-order inventory after mechanics operations.
Several invariant paths obtain the same detached book repeatedly.

Detached immutable views are correct at an external trust boundary. Rebuilding an
entire view for internal read-only iteration is unnecessary allocation and object
dispatch.

### 4.3 Repeated checkpoint fixed-point work

`FullDayStore.generate_day()` intentionally proves each checkpoint can round-trip.
For every capture it serializes runtime state, restores a fresh runtime, serializes
that runtime again, and then asks `_checkpoint_entry()` for another complete
`checkpoint_state()` and `state_sha256()`.

The fixed-point proof belongs in persisted evidence. The implementation should
compute one validated checkpoint projection and reuse its bytes, digest, metadata,
and restored proof result rather than independently rebuilding the same graph.

### 4.4 Handwritten wire mechanics

Record fields, exact keys, enum conversion, tuple/list conversion, schema versions,
canonical bytes, and routine primitive validation are repeated across hundreds of
classes. In 182 classes, encoder and decoder methods repeat 1,330 overlapping key
literals before counting the dataclass declarations themselves.

This is the clearest LOC-reduction opportunity, but it is compatibility-sensitive:
167 of 227 decoders appear to require exact key inventories, while 60 do not; 173
contain explicit `str`, `int`, `bool`, or `float` coercions. A universal strict
decoder would silently change historical acceptance behavior.

### 4.5 Parallel serialized-shape validation

The microscope contains 101 dataclasses and 79 encoders but no corresponding
`from_dict` methods. `microscope/report.py` instead carries roughly 38 serialized
shape/canonical-bundle validators totaling about 2,261 lines. Many mirror records in
annotations, comparison, panes, and query modules.

Untrusted offline report validation must remain separate from privileged object
construction. Its mechanical shape rules can still come from the same explicit
record specification.

### 4.6 Repeated artifact-family metadata

Artifact identity is distributed among `ArtifactType`, family sets in
`research/tables.py`, run-store inventory tuples, SQL `IN` lists, verifier dispatch,
media types, paths, and schema versions. Adding one artifact can require synchronized
edits in several places.

`research/tables.py::TABLE_SPECS` already demonstrates the right shape: explicit
declarations interpreted by common I/O. Artifact metadata should follow that pattern.

### 4.7 Audit and CLI boilerplate

Forty-eight expansion gates repeat the same transformation:

1. call a named audit-case provider;
2. map each case to a required PASS/FAIL check;
3. flatten case failures;
4. build a report with explicit metadata.

The gate order, card ownership, metadata, and custom truth checks must stay explicit.
The standard projection can be shared.

The legacy CLI still uses a 54-branch command dispatcher. New expansion commands
already use `CommandModule`, `CommandSpec`, and `CommandRegistry`; all new work should
use that route. Migrating old parser or help behavior is a later, versioned
compatibility task because its projection is digest-pinned.

The legacy parser and handler body can still be separated safely: keep every
`add_parser` call, order, help string, default, choice, and namespace field unchanged,
while moving command bodies into named domain functions. Sixteen legacy audit
branches share import/run/render/failure/exit orchestration; one small renderer can
serve those branches without registering them through the new registry. `audit-lab`
and any command with materially different output remain explicit.

The expansion gate inventories are intentionally redundant: canonical cards,
deviations, the registerable frontier, and `GATE_SPECS` cross-check one another. Do
not derive all of them from one declaration unless an independent frozen ordered-ID
manifest or digest replaces the omission/reordering detector.

### 4.8 Research-store repeated reads

`RunStore.verify_run()` authenticates artifacts, reads Parquet files for row counts,
reads the 13 canonical tables again for evidence checks, and then reconstructs replay
state from those tables. `read_parquet_table()` opens a fresh in-memory DuckDB
connection per table.

`RunStore._catalog_is_current()` reparses the run manifests multiple times to build
the all-artifact and typed-family projections, then materializes the same columns
from several views. A single authenticated table snapshot per verification and one
parsed manifest snapshot per catalog check can serve all projections while retaining
the critical order: verify path and bytes first, then admit parsed rows.

Pre-persistence replay and post-atomic-rename verification prove different trust
boundaries and must remain separate.

### 4.9 Small-window rescans

Queue-reactive state recomputes rolling quantities by scanning retained windows.
The full-day queue-reactive owner stores windows in lists and evicts with `pop(0)`.
This is a simple algorithmic fix before any array rewrite: use `deque`, maintain
rolling depletion/replenishment/buy/sell totals on append, subtract on `popleft`, and
retain the first midpoint needed for price movement.

### 4.10 Columnar data converted to rows too early

The market-data Parquet adapter asks DuckDB for all rows, calls `fetchall`, constructs
dicts and dataclasses, and later reconstructs row tuples to write DuckDB/Parquet.
Large normalized inputs should remain typed DuckDB relations through validation and
ordered `COPY`. SQL/window operations can detect timestamp or sequence reversal,
duplicates, gaps, and session-bound violations. Materialize only manifests, bounded
rejection samples, and deliberately small API results.

This is data-oriented work at an existing columnar boundary. It needs no new
dependency and must preserve the logical record, quality, capability, digest, and
physical artifact contracts that are pinned.

### 4.11 Replay indexes rebuilt per query

Microscope query selection scans all evidence and reserializes the visible observed
projection. Timeline stepping and jumping repeatedly build/filter time inventories.
Immutable per-run indexes can be built once: sorted cursor times, event-kind time
partitions, and `(source plane, series ID)` records ordered by visibility time. Use
`bisect` for held-last-known and next/previous navigation; materialize authority-
bearing result dataclasses only for selected rows.

### 4.12 Monolithic audit fixture modules

The largest audit modules already expose work-order-specific entry points. Split
their physical fixture implementations along those boundaries while keeping current
top-level modules as ordered re-export facades. This does not substantially reduce
LOC, but it reduces the cognitive and merge cost of a change and makes ownership
boundaries visible.

At least 21 domain `*AuditCase` dataclasses share a structural shape. Use a small
protocol and common renderer, not one permissive universal case. Some cases carry
evidence; full-day cases additionally carry `status_override`, `reason_code`, and
`required` semantics.

WO36-B and DEV-0005 both execute the same replay-observation-policy producer. A
single immutable result may be cached within one `ExpansionGateRegistry.run()` call
while still constructing two independent gate reports. Never cache across runs or
collapse the two gate identities.

### 4.13 Consolidated-feed rebuilds

The multivenue coordinator rescans observable events and venue tapes, rebuilds and
sorts consolidated trades, and serializes the feed for leakage checks after actions.
Maintain per-venue event/tape cursors, cached latest quotes, and one append-only
consolidated tape. Keep schema-based leakage validation and the full forensic
reconstruction at audit boundaries.

## 5. Simplification inventory

| Priority | Area | Simplification | Expected value | Main risk |
|---|---|---|---|---|
| P0 | Full-day transaction boundary | High-water marks plus undo receipts instead of whole-owner copies on success | Removes history-sized work from every public advance | Failure rollback and owner identity must remain exact |
| P0 | Runtime validation | Incremental witnesses behind the same boundary contract; retain full forensic audit | Changes repeated prefix scans into delta work | A latent corruption must still fail at the required boundary |
| P0 | Exchange internal reads | Internal handle/column scans and delta receipts; materialize views only at boundaries | Removes repeated whole-book object graphs | No mutable object may escape |
| P0 | Checkpoint generation | Build one validated checkpoint bundle and reuse bytes/digest/metadata | Removes redundant serialization/restore/hash passes | Fixed-point proof must remain independent enough to detect bugs |
| P0 | Wire substrate | Dependency-neutral named codec profiles and exact field/scalar helpers | Immediate reduction and one place to audit canonical behavior | Similar helpers have non-identical legacy behavior |
| P0 | New records | Explicit `RecordSpec` for mechanical wire shape | Prevents WO37-B and later cards from duplicating boilerplate | Specification magic must not infer semantic identity |
| P1 | Order book storage | Stable integer handles plus typed columns and per-price FIFO handle queues | Primary Python DOD kernel | FIFO, lifecycle, IDs, and checkpoint bytes must be identical |
| P1 | Mechanics synchronization | Consume mutation receipts instead of rescanning all managed/core orders | O(changed orders) synchronization | Advanced-order classifications must remain conserved |
| P1 | Work/event ledgers | Dense lifecycle/event tables with boundary projection | Less object churn and duplicate indexing | Global ordering and replay evidence are identity-bearing |
| P1 | Microscope wire validation | Generate shape checks from the same record specs | Removes a parallel schema implementation | Reviewer authority and relational truth checks remain handwritten |
| P1 | Artifact metadata | Explicit `ArtifactSpec` declarations | Fewer coordinated edits and drift opportunities | Public enums and historical SQL behavior must remain compatible |
| P1 | Expansion audit projection | Shared standard case-gate adapter | Shrinks repetitive gate wrappers | Canonical order and custom checks cannot be hidden |
| P1 | Run verification | One authenticated table snapshot and one DuckDB connection | Avoids reading all canonical tables several times | Bytes must be authenticated before rows are trusted |
| P1 | Catalog currentness | Parse each manifest once and derive expected projections | Avoids five equivalent manifest passes | Typed views must still be independently checked |
| P2 | Revision mechanics | Small pure lineage/successor/content-ID helpers | Reuses proven rules without universal inheritance | Identity inclusion differs by domain |
| P2 | Negative schema audits | Generate mechanical mutation corpus from `RecordSpec` | Slows future audit growth | Domain/provenance/replay attacks remain manual |
| P2 | Replay/microscope series | Columnar indexes and batched derivations after profiling | Efficient windows, joins, and synchronized cuts | Observation/reveal policy must be carried with every column |
| P2 | Mining feature windows | Batch detector inputs by source/time window | Reduces repeated record traversal | No-lookahead cutoff and evidence class must remain explicit |
| P2 | Queue-reactive windows | `deque` plus rolling aggregates | Removes repeated scans and linear front eviction | Checkpoint order and cutoff semantics must remain exact |
| P2 | Market-data normalization | Keep typed batches inside DuckDB | Removes Python row-object round trips | Logical and pinned physical identities must remain exact |
| P2 | Audit fixture organization | Work-order modules behind existing ordered facades | Reduces cognitive/merge cost without weakening cases | Physical reordering must not change case order |
| P2 | Multivenue feed | Per-venue cursors and cached consolidated state | Avoids full feed/tape rebuild after each action | Leakage and venue-time semantics must remain exact |
| Defer | Legacy CLI migration | Convert parser/dispatch only under a compatibility card | Reduces branch chain later | Frozen help/parser projection |
| Reject | Generic domain record | Merge all manifests/revisions/evidence into one dict model | Superficial LOC reduction only | Destroys ownership, authority, and identity clarity |

## 6. Python DOD suitability matrix

Data-oriented design is useful where many records undergo the same operations. It is
not a mandate to turn the entire application into arrays.

| Subsystem | Cardinality / iteration | Mutation | DOD fit | Decision |
|---|---|---|---|---|
| Order book orders and price levels | High and repeatedly scanned | High | Very high | First kernel target |
| Managed-order synchronization | Repeated whole-ledger reconciliation | High | Very high | Drive from order mutation receipts |
| Full-day scheduled work | Heap plus lifecycle ledgers | High | High | Stable integer work slots and integer type codes |
| Outer/native event ledgers | Append-only, repeatedly projected | Append-only | High | Typed columns plus boundary materialization |
| Transaction rollback state | Delta is small relative to history | High | High | Marks, truncation, scalar snapshots, key undo records |
| Replay and microscope time series | Large ordered scans/windows | Low | High | Columnar read models with policy columns |
| Mining detector/source windows | Repeated grouped scans | Low | Medium-high | Batch once hot paths are measured |
| Queue-reactive windows | Repeated sliding-window aggregates | Medium | Medium-high | Rolling totals first; ring buffers only if measured |
| Market-data normalization | Potentially large typed tables | Pipeline | High | Keep relations in DuckDB; materialize exceptions |
| Consolidated venue feed | Repeated event/tape merge | Append-only | Medium-high | Per-venue cursors and cached merge state |
| Research Parquet facts | Already tabular and ordered | Immutable | Already DOD | Keep `TABLE_SPECS` and DuckDB boundary |
| CLI parsing and command orchestration | Small | Low | Low | Declarative registry, not array-oriented kernel |
| Consent and identity mappings | Small, authority-sensitive | Low | Low | Keep explicit domain objects |
| Manifests and review sidecars | Small, identity-sensitive | Immutable | Low | Keep explicit records; share codecs only |
| Configuration and policy contracts | Small | Immutable | Low | TOML and named dataclasses are appropriate |

## 7. Proposed Python-first runtime layouts

### 7.1 `OrderTableV2`: dense internal order state

Keep public order IDs and public `OrderView` contracts. Internally assign each order
a monotonically increasing integer slot.

Suggested columns:

```text
slot -> external_order_id
slot -> order_type_code
slot -> owner_code
slot -> side_code
slot -> status_code
slot -> price_ticks
slot -> original_quantity
slot -> filled_quantity
slot -> remaining_quantity
slot -> cancelled_quantity
slot -> resting_sequence
slot -> cancel_target_slot_or_sentinel
external_order_id -> slot
```

Use `bytearray` for small closed codes and `array.array` with explicitly selected
signed/unsigned widths for integers after range assertions. A Python `list[int]`
still boxes each integer, but it is a useful prototype when mutation convenience is
more important than compactness. Keep strings in one identity pool rather than in
every hot record.

In the first seam, each price level may keep `deque[int]` FIFO slots plus a cached
total remaining quantity. In the final dense kernel, store `head`, `tail`, `previous`,
and `next` handle columns so a known order can unlink in constant time without
`deque.remove`. Maintain a `price_ticks -> level_slot` index. Insert a new price with
`bisect` into the sorted active-price list rather than sorting the complete list after
every new level. A truly dense price ladder should be used only if a plan supplies a
safe, bounded tick range; do not assume one globally.

An order mutation returns an immutable internal receipt:

```text
OrderMutationReceipt
  command_slot
  touched_order_slots
  touched_level_slots
  appended_trade_span
  appended_fill_span
  appended_event_span
  previous_best_bid
  previous_best_ask
  resulting_best_bid
  resulting_best_ask
```

Mechanics synchronization, delivery, scheduler observation, and incremental
invariants consume the receipt. External callers receive the existing event and view
projections.

The mechanics engine should also maintain a good-until-time expiry heap keyed by
`(expiry_time_us, order_handle)` rather than scanning the sorted managed-order
inventory for expiry. Longer term, `ManagedOrder` should become a boundary view over
one authoritative lifecycle table; its current duplicate lifecycle state is why
whole-ledger synchronization and reconciliation are required.

### 7.2 `WorkTableV2`: dense scheduled-work lifecycle

The current heap and three dictionaries retain overlapping work identity and object
graphs. A dense work table can store:

```text
slot -> simulation_time_us
slot -> microstep
slot -> stage_code
slot -> source_component_code
slot -> component_local_sequence
slot -> work_type_code
slot -> lifecycle_code
slot -> payload_variant_slot
slot -> external_work_id
external_work_id -> slot
```

The heap may remain a Python heap of ordering tuples, but its payload becomes an
integer slot. Closed work-type codes index a tuple of bound handler callables; this
removes string-to-string lookup plus `getattr` from every item without obscuring
dispatch.

Heterogeneous payloads should use small type-specific tables, not one large generic
payload dictionary. The public/checkpoint projection reconstructs the exact current
`RuntimeWorkItemV1` shape and work ID.

Lifecycle transition is a code update (`PENDING`, `EXECUTED`, `RETIRED`) rather than
moving the full object among three dictionaries. Append-only execution order is
tracked separately so checkpoint sorting is not repeatedly rediscovered.

### 7.3 `EventColumnsV2`: append-only evidence kernel

Store frequently scanned event fields as parallel append-only columns:

```text
global_sequence
simulation_time_us
microstep
stage_code
source_component_code
component_local_sequence
event_type_code
parent_event_slot_or_sentinel
causal_work_slot_or_sentinel
payload_variant_slot
```

Keep external IDs in stable pools and payloads in explicit variant tables. Expose
read-only spans or iterators for internal scans. Materialize `FullDayEventV1`, dicts,
canonical JSON, and Parquet rows only at a named boundary.

The new layout does not change event order or event identity. The projection function
is part of the compatibility contract and must be tested byte-for-byte against the
current encoder.

### 7.4 Transaction journal instead of transaction clone

A transaction begins by recording:

- append high-water marks for work, events, trades, fills, native entries, and
  quiescent cuts;
- the heap pop log and the slots enqueued during the transaction;
- compact scalar counters and clocks;
- prior values only for mutable array slots or mapping keys that are touched;
- component-specific journal marks.

On success, discard the undo records. On failure:

1. truncate append-only columns to their marks;
2. restore touched slots and keys in reverse mutation order;
3. restore scalar counters and clock;
4. return popped heap entries and remove newly enqueued entries;
5. verify the pre-transaction state digest in an audit/debug mode.

Rollback may be more expensive than success because failure is exceptional. It must
still be bounded and deterministic. No owner may mutate outside the journaled kernel;
the transition requires explicit owner receipts before removing the current full
snapshot fallback.

### 7.5 Incremental invariant witnesses

Do not delete the complete verifier. Split validation into three layers:

1. **Mutation checks:** exact local conservation, FIFO head/tail, sequence increment,
   legal lifecycle transition, bounded work/event counts, and touched-key ownership.
2. **Incremental witnesses:** maintained counts, set-equivalence digests, allocator
   high-water values, per-component sequence heads, event-prefix digest state, active
   quantity totals, and lifecycle population counts.
3. **Forensic verification:** reconstruct and replay complete prefixes exactly as the
   present audit gates require.

Initially, run both incremental and forensic validation in shadow mode and compare
their decisions. Only after a fixed corpus and the frozen full-day workload prove
equivalence should the normal successful path use the incremental witness at frequent
boundaries. Checkpoint, persistence, replay verification, qualification, and explicit
audit operations retain forensic verification.

If the timing of an invariant refusal is part of a public contract, the incremental
validator must fail at the same `advance_to` boundary. Moving the failure later is not
an optimization.

### 7.6 Mutation epochs for stage-purity checks

Agent decision work currently compares complete engine canonical bytes before and
after execution. Add an engine mutation epoch plus append-only cursor counts and
owner/clock identity checks. A stage that is required to be pure records the epoch
before execution and proves it is unchanged after execution; an arrival stage proves
that any epoch change is accompanied by the required mechanics-event span.

The epoch is an operational invariant witness, not semantic evidence. Full canonical
state comparison remains in shadow/audit mode until the epoch and receipt path is
proven to detect the same injected mutations.

## 8. Proposed declarative reduction

Declarative record specifications reduce code size, but they are not the same as
cache-oriented runtime DOD. Treat them as a control-plane simplification.

### 8.1 Named wire codec profiles

Create a dependency-neutral package such as:

```text
kirby2/wire/
  semantic_json.py
  checkpoint_json.py
  fields.py
```

Use named profiles rather than a universal JSON helper. At minimum distinguish:

- integer-only semantic identity JSON;
- checkpoint JSON with its current exact boundary behavior;
- legacy formats whose accepted corpus includes finite floats or coercive decoding.

`runtime_state.py` currently imports neutral codec and field utilities from
`full_day.models`; moving the substrate to a top-level dependency-neutral module
also repairs that ownership direction.

Do not reuse `immutable.freeze_json` as the semantic codec: its general immutable
JSON policy permits finite floats, whereas identity-bearing semantic JSON forbids
binary floats.

Retain compatibility aliases at old import locations. Every migration must prove
identical accepted bytes and identical rejection behavior.

Estimated immediate net reduction: 500-900 lines, with a larger prevention benefit.

### 8.2 Explicit `RecordSpec`

A conservative record declaration owns mechanical wire behavior only:

```python
ATTEMPT_SPEC = RecordSpec(
    fields=(
        field("schema_version", exact_int, constant=1),
        field("status", enum_value(AttemptStatus)),
        field("events", tuple_of(nested(EVENT_SPEC)), wire_container=list),
        field("optional_digest", optional(sha256_text)),
    ),
    exact_keys=True,
)
```

It may provide exact-key checking, primitive decoding, enum conversion, tuple/list
wire conversion, nested record validation, encoding, and canonical bytes through a
named codec.

It must not:

- infer identity from all dataclass fields;
- silently coerce values;
- choose omission versus null;
- create reviewer or human authority;
- replace relational, provenance, graph, replay, consent, or no-lookahead checks;
- use `dataclasses.asdict`, Pydantic-style coercion, or annotation-only magic.

Keep domain semantics readable:

```python
def __post_init__(self) -> None:
    ATTEMPT_SPEC.validate_instance_shape(self)
    self._validate_domain_semantics()
```

Start with new WO37-B leaf records so new work stops adding boilerplate. Pilot an
existing migration in `curriculum`, where record mechanics are about 36.7% of the
package. A mature, staged migration could remove approximately 8,000-12,000 net
lines. A repository-wide conversion is explicitly rejected.

### 8.3 Artifact specifications

Add immutable explicit `ArtifactSpec` declarations containing:

- public `ArtifactType`;
- artifact family and permitted `RunType`;
- logical name and relative path;
- media type and schema version;
- required/optional cardinality and row-count policy.

Derive family subsets, inventory checks, and parameterized SQL filter values from
the declarations. Keep the public enum explicit and stable; do not dynamically create
it. This should reduce 300-600 lines immediately and, more importantly, remove
multi-file drift from later instructor/study artifacts.

### 8.4 Standard audit-case projection

Introduce a small explicit adapter for standard case-based gates. A gate declaration
still names:

- card ID;
- ordered case provider;
- expected case count;
- required metadata;
- any extra custom checks.

The adapter performs only case-to-check conversion and failure flattening. Keep the
registry's canonical order, deviation placement, completeness assertions, and every
custom truth claim visible.

`RecordSpec` may also generate mechanical negative cases—missing key, unknown key,
wrong primitive, invalid enum, wrong version, tuple/array mismatch, malformed digest,
duplicate JSON key, and noncanonical bytes. Authority, provenance, replay, cutoff,
and cross-record attacks remain handwritten.

## 9. Areas to keep as they are

- `research/tables.py::TABLE_SPECS` is already a good declarative and columnar
  boundary.
- DuckDB and Parquet are appropriate for immutable research facts and rebuildable
  catalogs. Do not turn DuckDB into the live mutation engine.
- `cli/registry.py` is the correct route for every new command.
- Explicit mining skill and detector declarations preserve readable domain meaning.
- Frozen/slot dataclasses are appropriate at external boundaries and for small
  immutable policy values.
- The 427-line `FullDayPlanV1.__post_init__` contains substantial plan-wide calendar,
  component, seed, ordering, and schedule semantics. Leaf decoding can be shared;
  the plan-wide rules should stay explicit.
- Consent, pseudonym mapping, deletion receipts, review sidecars, and construction
  tokens are small, authority-sensitive records. Do not apply array-oriented DOD.
- Complete replay/audit verifiers are the reference implementation against which
  optimized witnesses are checked.
- Independent expansion gate inventories are valuable drift detectors. Consolidate
  report construction, not all sources of gate truth.

## 10. Migration sequence

Because the canonical work-order contract allows exactly one card at a time, this
sequence requires either an explicit deviation card or a separately scheduled lane.

### DOD-0 — Current-HEAD measurement

No semantic behavior changes.

1. Run the frozen `FULL_DAY_PERFORMANCE_V1` workload on the current clean HEAD.
2. Add operational-only counters/timers for:
   - transaction snapshot capture and restore;
   - full versus incremental invariant time;
   - checkpoint projection, canonical encoding, hashing, restore, and fixed-point
     validation;
   - `OrderBook` view materializations and rows materialized;
   - mechanics managed-order sync scans;
   - agent before/after engine serialization;
   - heap operations, work items, events, mechanics commands, and touched orders;
   - queue-window rows scanned/evicted and replay-query records visited;
   - Parquet reads, DuckDB connections, manifest parses, and Python rows materialized.
3. Use stdlib `cProfile`, `tracemalloc`, `resource`, and explicit nanosecond timers in
   separate diagnostic artifacts. Do not bind operational measurements into semantic
   run identity.
4. Publish call counts and cumulative time by named operation, not only a flame graph.

Exit: at least 80% of measured generation CPU time is assigned to named operations,
or the remaining unknown share is explicitly reported.

### REDUCE-1 — Wire substrate and new-record prevention

1. Extract named codecs and field helpers behind compatibility aliases.
2. Freeze accepted and rejected corpora per migrated format.
3. Use `RecordSpec` for new WO37-B leaf records.
4. Add `ArtifactSpec` only for new instructor artifacts, then backfill existing
   families after parity evidence.

Exit: byte, digest, ID, accepted-input, and rejected-input parity; no artifact rewrite.

### DOD-1 — Internal book scan API and mutation receipts

Before changing storage, stop creating whole detached views internally.

1. Add internal read-only iterators/cuts that expose no mutable `Order` or
   `PriceLevel` object.
2. Produce mutation receipts from existing object storage.
3. Drive mechanics synchronization and repeated observations from receipts/cached
   totals.
4. Keep public properties and checkpoint projections unchanged.

Exit: same event/checkpoint bytes; internal full-book materialization count is zero on
the frozen full-day success path except at named projection boundaries.

### DOD-2 — Journaled full-day transactions

1. Add owner mutation receipts and transaction marks while retaining the snapshot
   fallback.
2. Run fault injection through both rollback mechanisms and compare full state bytes.
3. Shadow incremental invariant witnesses against the forensic verifier.
4. Remove successful-path full-state capture only after every injected failure
   restores byte-identical pre-state.

Exit: exact atomicity, failure code, failure boundary, replay, and checkpoint parity.

### DOD-3 — Dense `OrderTableV2`

1. Implement the stable-handle kernel behind the current `OrderBook` API.
2. Run old and new kernels in shadow on the accepted mechanics scenario corpus.
3. Compare every command receipt, event, trade, fill, depth cut, state projection,
   checkpoint byte, and rejection.
4. Switch normal execution only after shadow parity is exact.

Exit: no semantic diff; material reduction in CPU/event and allocations/order.

### DOD-4 — Dense work and event ledgers

1. Convert work lifecycle to slots and type-specific payload tables.
2. Convert event/native ledgers to append-only columns.
3. Retain exact object and wire projections at module boundaries.
4. Prove one-large-advance versus many-small-advances equivalence, checkpoint resume,
   and failure rollback.

Exit: exact ordering and digest parity across the full expansion suite.

### DOD-5 — Analytical batches

Profile replay, microscope, mining, and discovery separately. Convert only repeated
series/window operations with measured benefit. Carry simulation time, observation
cutoff, evidence class, source identity, and reveal authorization alongside each
batch; never keep those as ambient context.

Exit: query/result bytes and refusal behavior are unchanged, with a demonstrated
load/window improvement.

### REDUCE-2 — Authenticated research snapshots

1. Authenticate every artifact path and digest exactly as today.
2. Open one DuckDB connection and load each canonical table once.
3. Derive row counts, foreign-run checks, sequence completeness, evidence digest,
   and replay reconstruction from the admitted snapshot.
4. Parse each manifest once per catalog-currentness check and derive all expected
   typed projections from that snapshot.
5. Compare every catalog table/view, verification report, refusal, and logical row
   before and after.

Exit: identical reports and catalog surfaces with fewer reads, connections, parses,
and Python row materializations.

### REDUCE-3 — Audit and CLI mechanical cleanup

1. Freeze every registered gate's ordered `as_dict()` result, rendered output, and
   exit code individually and through `all`.
2. Freeze K2X-02 parser/help projections and the legacy command order.
3. Extract legacy handler bodies while leaving parser registration untouched.
4. Add the narrow standard case-report adapter while keeping every gate function and
   `GATE_SPECS` entry explicit.
5. Split the largest audit fixtures by work-order boundary behind stable facades.
6. Preserve fresh-process probes, statistical budgets, attack names, case counts,
   and final ordered aggregate receipts.

Exit: identical gate reports, ordering, stdout, exit codes, parser projection, and
failure corpus. Lowering budgets is not simplification.

### NATIVE-0 — Reconsider the language boundary

Evaluate a Rust/C++ extension only after DOD-3/4 if a pure kernel remains dominant.
The candidate must have:

- a closed, primitive-array request/response contract;
- no file I/O, wall clock, network, Python callbacks, or authority decisions;
- explicit integer widths and overflow behavior;
- deterministic ordering and seeded state;
- byte-identical Python reference results;
- a Python fallback used by parity audits.

Do not move orchestration, manifests, consent, replay authority, or research storage
into native code. The first plausible native seam is a pure matching or batch feature
kernel, not `FullDayRuntime` as a whole.

## 11. Required parity and performance gates

Every reduction slice must satisfy all applicable gates below.

### Semantic parity

- identical order, event, work, trade, fill, and native-ledger ordering;
- identical canonical JSON/TOML/Parquet logical rows;
- identical content IDs, SHA-256 values, run IDs, manifest identities, and artifact
  references;
- identical one-large-advance and many-small-advances result;
- identical checkpoint bytes and fresh-process restore fixed point;
- identical accepted input and rejection code/boundary;
- identical observation/reveal/truth and no-lookahead decisions;
- identical source-versus-derived and human-versus-automated claims.

### Failure atomicity

- inject a failure after every mutation stage that can be named;
- compare complete pre-state and restored state bytes;
- verify owner object identities where the API promises identity preservation;
- verify no ID, RNG value, sequence, heap item, event, or side effect is consumed;
- verify retry produces the same result as an uninterrupted run.

### Wire compatibility

- canonical byte corpus before/after;
- rejected corpus for missing/unknown fields, booleans as integers, coercible text,
  floats, NaN/infinity, duplicate keys, non-NFC text, surrogates, cycles, tuple/list
  mismatches, malformed digests, and future versions;
- preserve legacy coercions until a separately versioned compatibility decision;
- verify existing immutable runs; never rewrite them;
- rebuild the DuckDB catalog and compare logical rows/views.

### Performance evidence

For every measured lane, report:

- wall CPU and elapsed time separately where available;
- events, commands, work items, orders, trades, fills, and touched rows;
- successful-path bytes copied/serialized/hashed;
- allocation count or traced peak for the target operation;
- median and tail cost per event/command;
- complete-run, checkpoint, replay, and load metrics from the frozen policy;
- machine, Python version, commit, clean/dirty state, and profiler overhead mode.

The existing `FULL_DAY_PERFORMANCE_V1` throughput policy is the gate: at least 100
outer events/second leaves FAIL for WARNING, and at least 500 reaches PASS on an
eligible host. Retain the other preregistered thresholds and hard-abort rules.

The runtime lane should additionally report:

- fixed command batches after 1x, 2x, and 4x unrelated historical prefixes, proving
  incremental cost no longer grows linearly with untouched history;
- a 10,000-command submit/match/cancel benchmark with commands/second, view objects,
  orders walked, bytes serialized, and full-invariant calls;
- zero whole-book `OrderView` reconstruction in the mechanics mutation loop;
- zero full canonical-engine serialization in normal agent decision purity checks;
- forensic full-history checks proportional to explicit proof boundaries rather than
  work items, with incremental checks still occurring at every required public
  boundary;
- market-data results at 100,000 and 1,000,000 rows without production-path Python
  `fetchall()`.

Performance improvements never excuse a semantic mismatch. A slower exact shadow
implementation is acceptable during migration; only the selected normal path must
meet the frozen performance gate.

## 12. Stop rules

Stop and reconsider a slice if any of the following occurs:

- a canonical byte, digest, identity, ordering, or rejection changes without a
  separately approved version;
- a “generic” abstraction needs domain-name conditionals to recover lost meaning;
- an optimized internal view can mutate authoritative state;
- invariant failure moves to a later boundary;
- rollback depends on re-running nondeterministic code;
- batch columns omit cutoff, provenance, or reveal policy;
- performance evidence improves only by disabling forensic verification at required
  gates;
- a dependency is proposed before stdlib containers establish the benefit;
- a native extension requires Python callbacks inside its hot loop;
- LOC decreases while the number of identity or authority decision sites increases.

## 13. Recommended immediate focus

The next architecture work should be a bounded measurement-and-seam card, not a C++
port and not a global model rewrite.

Recommended deliverable:

1. current-HEAD named-operation profile of `FULL_DAY_PERFORMANCE_V1`;
2. an internal non-materializing `OrderBook` scan interface;
3. an `OrderMutationReceipt` produced by the existing object implementation;
4. mechanics synchronization driven by changed orders rather than the whole ledger;
5. byte/digest/replay/fault parity evidence;
6. before/after time, call count, and allocation results.

This slice creates the boundary needed for later stable-handle storage while touching
less semantic surface than a direct rewrite. In parallel at the roadmap level—but
not in the same implementation commit—new WO37-B records should use the named wire
codec and explicit `RecordSpec` pattern so the control plane stops accumulating
avoidable boilerplate.

The expected long-term schema/control-plane reduction is approximately 10,000-15,000
net lines, or 4-6% of the current Python tree, without deleting domain guarantees.
Runtime DOD may initially add shadow/parity code before it removes old storage; judge
that lane by exactness and CPU/event, not immediate LOC.
