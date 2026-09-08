# KIRBY2-PLAYBOOK-005 — bounded local playbooks and Lab

Active authority: the user's request to continue the Chapter 5 build, following
the revision-3 `05-playbooks-and-lab.md` and C5-01–C5-08 acceptance cases.
Backend baseline: f9ec731465e0510ad917e14efec5c4bca5bdc129. UI baseline:
ca07faf81079083ca9350aaba1ca3b19b6fa17b5 plus preserved Chapters 2–4 work.

The user prohibits images under all circumstances. This overrides the plan's
render requirement: use protected offscreen application assertions only; image
and human visual acceptance remain unperformed. No worker contact, commit, push,
visible Qt, release qualification, or Chapter 6 work is authorized by this slice.

Ownership: backend owns the supported declarative rule evaluator, case streams,
execution, experiment accounting, immutable storage and passage reconstruction.
UI owns source/form requests, strict projections, cancellable job ownership and
presentation. Existing simulation catalog and Chapter 1 preparation V1 keep their
meaning. New optional public modules do not expand the base 28-export inventory.

V1 scope: traffic-light source with REFUSE missing-value policy and delivered
top-of-book features; advanced state machines/window features are explicitly
refused while original source is retained. Execution is one bounded long limit
entry, optional simulated decision delay, cancel on loss of eligibility or expiry,
hold partial fills without replenishment, and no automatic exit. These are rules,
not calibrated probabilities. Ranking is optional and absent in this version.
Each Lab cell evaluates one idea at READY, revalidates after the declared delay,
and checks invalidation every 1000 simulated µs plus exact expiry. It does not
repeatedly enter after a new GREEN signal. Scripted decisions carry explicit
DECLARATIVE_PLAYBOOK_V1 attribution; the Execution rule coach remains manual.

Lab uses the two versioned C4 recipes, explicit seeds and at most 16 cells with
one worker. Each variant reconstructs its own book, queues and pending delivery.
The fixed counterparty order schedule and seeded simple background arrivals are
declared; placement/matching consequences are rebuilt. This is not a stored price
tape or a model of strategic traders adapting to the learner. Existing independent
full-day component seed streams are recorded; no worker order draws randomness.
Planned cells are durably recorded before execution. Every result is immutable;
interruption remains incomplete, cancellation preserves finished cells, and retry
creates a separate linked attempt. Results never choose an automatic winner.

Decision benchmark is the delivered midpoint at submission. Arrival benchmark
is the actual venue midpoint at one microsecond before the NORMAL 2700 µs route
arrival. Markout uses actual venue midpoint at fill time plus the declared horizon;
missing sides and horizons past the exercise end are unavailable, never interpolated.
All units are integer ticks, shares, half ticks and milli-tick-share fees; there is
no implied dollar tick value. Inventory marking is distinct from realized P&L.

Reserved means not yet inspected in this Lab, not unfamiliar market experience.
Case lineage is recipe plus seed, independent of variant/name/attempt. Inspecting,
exporting or practicing a case writes an append-only reveal record before returning
its results. Known curated recipe provenance remains visible. No split may assign
one lineage to both practice and reserved roles in the same study.

Extraction reads a deeply verified C4 personal bundle, reconstructs its recorded
nonzero-time instructions and pending state at the chosen cut, and publishes an
immutable candidate with a frozen objective. A separate explicit recipe review
is required to practice it. Missing dependencies or unsupported held/armed cuts
refuse. Repetition acquires fresh C4 authority from that verified prefix; it does
not relocate actions to time zero or silently create an answer key.
The candidate's checkpoint is made on an audit copy. Live acquisition reconstructs
the verified instruction prefix without inserting an unrecorded checkpoint marker
into C4's existing bundle history. Both the audit restore continuation and the
newly saved live repetition are verified. Opening an immutable study independently
rebuilds every completed policy trial; failures and cancellation remain operational
dispositions, not reproducible scientific outcomes. Export carries execution
dependencies and persistent reveal history; no generic import/merge UI is claimed.

Verification (no images):

```
.venv/bin/python -B -m unittest kirby2.audit.playbook_lab kirby2.audit.execution_practice -v
```

In the UI checkout, use the existing protected launcher:

```
./scripts/test_replay_ui.sh test_playbook_no_images.py
./scripts/test_market_no_images.sh
```

The full non-image gate includes the new module and retains the existing explicit
image-producing test exclusions. No renderer, capture tool, or image viewer is used.

Stop after the Chapter 5 source, bounded executable acceptance and text evidence.
