# Chapter 6 — Radar session work order

Authority: user requested continuation of the direct build after Chapter 5.
Active section: revision-3 `chapters/06-radar-and-session.md`, C6-01–C6-08.
Baseline backend: `119fcfe5d64a76e30621f976654f21e1e5933461` (clean).
UI baseline: `ca07faf81079083ca9350aaba1ca3b19b6fa17b5` plus preserved Chapters
2–5 work. This chapter must not discard or claim authorship of that baseline.

## Bounded implementation

One backend composite owns three independent FullDayRuntime books, client
commitment ledgers, a fixed ordered scheduler and all execution routing.
The versioned first session is an eight-second rotation (world cuts 1–9 seconds),
with a 500 ms published observation cadence. Continuous means 1× elapsed
presentation time; guided pauses all instruments. Late desktop delivery beyond
the declared backlog budget pauses the whole world with an explicit overload
receipt. It never silently slows a session labeled continuous.

Fictional Aster, Brio and Cinder have independent derived seeds. There is no
shared market factor in V1. Ordinary order schedules and seeded background
orders produce the books; no forced fills or price paths. The public contract
declares three instruments, 512 commands and bounded candidate/event evidence.
The additional bounds are 16 learner orders, 128 candidates and 1,000 world
events. Only one desktop worker may operate on a world. Four overdue published
steps are the backlog limit. Overload pauses; an internal ownership failure
locks the composite for cleanup and never advertises mixed clocks as coherent.

The existing C5 source parser and evaluator supply eligibility. A separate C6
adapter accepts the longer world clock; C4/C5 V1 meanings stay unchanged.
Its nested observation ID is `KIRBY2_SESSION_RULE_OBSERVATION_V1`; the original
`KIRBY2_RULE_OBSERVATION_V1` keeps its C4 clock limit and is not reinterpreted.
Manual session orders are explicitly learner instructions, not automatic C5
trial execution. Alert TTL/cooldown are independent from order cancellation.
Ranking uses observable spread and depth only; no hidden scenario label or
outcome enters selection. Ties use instrument identity and a declared score
margin retains the incumbent. C3-style activity must use delivered observations
with explicit availability provenance, never relabel delayed data as zero-delay.
The new C3 quote-only observation schema reports source, availability and
observation times, spread and total visible depth. Its event-window features
(executed volume, cancellation and replenishment) and relative-activity prior
are explicitly unavailable. The original C3 complete-window contract is intact.
Score = 1,000,000 − 1,000 × spread ticks + min(total visible bid shares, 999).
Ties use the symbol; an incumbent stays first for a challenger advantage below
100 points. A 1.5-second alert TTL ends independently of a 0.75-second cooldown.

Inspection, candidate selection, arming and submission are distinct. Selection
revalidates current eligibility and expiry atomically; orders retain the armed
symbol despite table movement. Any confirmed position, unresolved quantity,
pending cancel or unknown ledger blocks transferring execution to another book.

Persistence retains all instruments, decisions, visibility receipts and refused
actions. Opening verifies deterministic replay, including pending messages and
economic commitments. Checkpoint markers are explicit world instructions so
they cannot silently change the saved continuation. Closing releases simulation
resources and never claims cancellation or settlement.
Each saved record includes a unique attempt identity; repeat content does not
erase a distinct attempt. Frozen review reuses C5 signed fill-cost and markout
arithmetic. Midpoints for arrival/markout are source-time venue facts, revealed
only after freeze; a truncated horizon or missing midpoint is not a zero result.

The first dense-flow probe exceeded the existing runtime ownership-scan bound
and could not sustain the intended publication rate. The accepted implementation
must retain that guard. This versioned recipe uses 0.1 background additions per
second per side and ordinary deep additions just before each published cut.
It is a bounded attention exercise, not a high-activity or calibrated tape model.

## Deviations and stop boundary

User prohibits **all images**, including screenshots and image-producing tests.
Use public executable checks and protected offscreen widget assertions only;
do not claim visual/human acceptance. No visible Qt, pushes, release gate or
expensive remote qualification. No contact with Sol or other workers. Stop
before Chapter 7. Report any unfulfilled C6 acceptance explicitly.
