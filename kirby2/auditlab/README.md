# Kirby2 model-risk laboratory

The laboratory attacks simulator correctness through runtime invariants, exact
replay, generated scenario coverage, explicit fault injection, deterministic
failure reduction, statistical holdouts, and immutable evidence. It is not a
conventional unit-test framework.

Generated-configuration schema v2 schedules seven independent executor lanes:
core flow, market mechanics, asynchronous latency, fragmented venues, agent
ecology, execution algorithms, and explicit faults. Each scientific cell has
six otherwise identical replicates. Replicates zero through two are `TRAIN`;
replicates three through five are `HOLDOUT`. Fault cells rotate all ten fault
kinds. Lane assignment, cell identity, partition, and case seed depend only on
the master seed and integer scheduler indices.

Executors return immutable typed records for exercised configuration values,
checks, unexpected failures, native recordings, event/state projections, and
observable output. Status is an enum, not a Boolean: unsupported work is
`NOT_EXERCISED`. A required `NOT_EXERCISED` check fails its case. The sole
capability matrix lives in `executors/base.py`, and evidence coverage credits a
declared value only when the corresponding real executor returns an
`EXERCISED` record for that exact value. An empty registry refuses execution;
there is no placeholder executor.

The generated runner dispatches every case through `run_generated_case()` and
the lane-keyed executor registry. The former facsimile kernel and its
declaration-based coverage report have been removed. Coverage is derived only
from matching `EXERCISED` records and required typed checks. It reports missing
configured values and supported pairs inside each lane; no cross-lane
interaction receives implicit credit.

Player cash and position in the core-flow lane are independently reconstructed
by `FillLedgerProjector` from the immutable fill ledger and by
`EventLedgerProjector` from submitted/fill events. The projectors have separate
algorithms and local accumulators. Targeted subsystem probes also return typed
`CheckResult` records. In particular, `branch_parent_consistency` records the
verified parent ID and digest, exact fork-prefix equality, post-fork mutation,
and immutable branch verification; generated lanes do not claim that check.

The fault lane now has the same typed execution contract as the six scientific
lanes. Its current explicit detector observations remain the input to the next
repair stage, which separates production fault injection from an independent
expected-code oracle. Typed wrapping alone is not evidence that those detector
internals are already independent.

The ten injected faults are never hidden in random noise. Each has a manifest,
injection point, detector, expected code, detected code, and evidence. Detected
injected faults are expected adversarial observations; detector misses and
structural invariant failures are unexpected violations.

Each stable violation signature is reduced by rerunning candidates while
shrinking duration, event count, participant count, venue count, and remaining
configuration complexity. A reduction is accepted only if the same signature
survives.

Run the requested substantial audit:

```text
python3 -m kirby2 audit-lab --budget 10000 --seed 771 --save-failures
```

Packets are content-addressed beneath `.kirby2/research/audit_lab/packets` and
indexed by an append-only ledger. Schema-v2 packet identity binds the canonical
run identity to the sorted byte count and SHA-256 inventory of every UTF-8
artifact. Re-recording identical material is idempotent; changed artifact bytes
produce a different packet ID. The human-readable report stores
`PACKET id=SEE_MANIFEST` so the ID is never guessed before all artifacts exist.

Artifact names must be canonical, packet-relative POSIX paths. Absolute paths,
parent/dot segments, Windows path forms, backslashes, the reserved manifest
name, and symlinks are rejected. The complete name inventory is validated before
staging writes, and verification rechecks both lexical names and resolved
containment before reading artifacts. Schema-v1 packets remain verifiable as
`IDENTITY_ONLY_LEGACY`, but all new writes use schema v2 and
`IDENTITY_AND_ARTIFACTS`; legacy packets are never rewritten during recording.

Passing automated gates creates a separate immutable acceptance record whose
decision remains `PENDING_HUMAN_REVIEW`. `AuditLabStore.record_acceptance` can
append a human decision that names this record in `supersedes_record_id`;
earlier records are never rewritten.
