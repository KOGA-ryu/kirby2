# Kirby2 Full-Day Profile Qualification Evidence

## Status

WO31-I1 completed its one-time protected execution with the frozen WO31-I
implementation. The governed evidence store verified successfully, every required
audit passed, and no source, manifest, command, gate, threshold, or package-data byte
was changed after outcome revelation.

Evidence-integrity `PASS` does not mean every candidate is automatically ready. All
eight candidate/partition dispositions are `NOT_READY` because platform performance
failed the preregistered throughput threshold. Quiet-range behavior passed its
behavioral envelope in qualification and holdout; the other three candidates produced
reproducible behavioral warnings. Human review remains `PENDING`.

## Immutable identity

| Field | Value |
| --- | --- |
| Work order | `WO31-I1` |
| Execution kind | `REAL_ONE_TIME` |
| Evidence run ID | `run-bf21e61c4b0d50862f901be1` |
| Implementation commit | `1a4bbebd576487bff0d9088bc77ecf726bb47bc5` |
| WO31-H preregistration commit | `1d1a1bc1c189c75d0d1aac4d223256b1aed67e9a` |
| Qualification identity | `d26c26dac2cf82ef0305e1c3f4529e9a398baca6568ddf0a511812bbddfd8e63` |
| Profile-bundle SHA-256 | `81835d8b8cbe9577da1bceef56a5ce1ac18bf43f068fb871a09d0ea5f769cc19` |
| Configuration digest | `d623460493f750e2781603d721443f9dfbe8577c54fbcd95e4b9197e4fd4a885` |
| Evidence digest | `c3cec2a0dc7e151c83a575b7b7bcd88337a636557d55c3de8ac78e1e83e0105e` |
| Result digest | `a0885b37c1f49cf2f6ff7dc49e8b0667783c6b61a251372ed7920e38b4d55698` |
| Reveal-token ID | `reveal-e9dd1d2e55701037125addb3` |
| Creation timestamp | `2026-08-29T05:21:27.294965Z` |
| Protected seed access | `QUALIFICATION_AND_HOLDOUT` |
| Replay verification | `PASS` |
| Re-entry policy | `VERIFY_ONLY_NEVER_RERUN_OR_OVERWRITE` |

The reveal token is consumed and bound to the exact qualification identity and
implementation commit. Later command entry is verification-only and cannot rerun or
overwrite the qualification or holdout.

## Execution completeness and engineering findings

The frozen inventory executed four candidates over eight qualification roots and four
holdout roots each: 48 complete full trading days.

| Check | Observed result |
| --- | ---: |
| Raw run-metric rows | 48 |
| Typed run-proof rows | 48 |
| Exact replay proofs | 48 `PASS`, 0 failed |
| Runtime-invariant failures | 0 |
| Safety aborts | 0 |
| Target-price operations | 0 |
| Forced-trade operations | 0 |
| Minimum trades in any day | 104 |
| Maximum trades in any day | 133 |
| Maximum continuous spread | 2 ticks |
| Maximum non-halt empty-side episode | 0 microseconds |

Trade-count ranges by candidate were:

| Candidate | Roots | Minimum trades | Maximum trades |
| --- | ---: | ---: | ---: |
| Quiet range | 12 | 104 | 109 |
| Trend | 12 | 118 | 127 |
| Event shock and recovery | 12 | 124 | 133 |
| Disorderly open stabilization | 12 | 111 | 123 |

All eight candidate/partition engineering statuses are `PASS`. The engine therefore
demonstrated stable complete-day generation, fully occupied two-sided quoting, bounded
spreads, invariant preservation, and exact stored replay across every protected root.

## Candidate findings

### Disposition summary

| Candidate | Partition | Engineering | Behavioral | Performance | Automated | Human |
| --- | --- | --- | --- | --- | --- | --- |
| Quiet range | Qualification | `PASS` | `PASS` | `FAIL` | `NOT_READY` | `PENDING` |
| Quiet range | Holdout | `PASS` | `PASS` | `FAIL` | `NOT_READY` | `PENDING` |
| Trend | Qualification | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |
| Trend | Holdout | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |
| Event shock and recovery | Qualification | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |
| Event shock and recovery | Holdout | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |
| Disorderly open stabilization | Qualification | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |
| Disorderly open stabilization | Holdout | `PASS` | `WARNING` | `FAIL` | `NOT_READY` | `PENDING` |

Statistical status is `NOT_APPLICABLE` for all eight rows under the preregistered
policy. Behavioral misses are warnings, not engine defects or real-market realism
claims.

### Quiet range

Quiet range passed every behavioral condition in both partitions.

| Metric | Qualification | Holdout | Envelope |
| --- | ---: | ---: | ---: |
| Absolute aggressive-volume imbalance | 66,170 ppm | 81,081 ppm | at most 250,000 ppm |
| Maximum absolute trade displacement | 2 ticks | 2 ticks | at most 80 ticks |
| Time-weighted spread p50 | 2 ticks | 2 ticks | at most 4 ticks |
| Time-weighted spread p95 | 2 ticks | 2 ticks | at most 8 ticks |

Observed finding: the current system can reliably generate a narrow, balanced,
low-displacement day, and this behavior reproduced in the untouched holdout roots.

### Trend

Trend produced the same three behavioral warnings in qualification and holdout:
`FAVORED_AGGRESSIVE_VOLUME_SHARE`, `MEDIAN_FAVORED_SIGNED_DISPLACEMENT`, and
`POSITIVE_ROOT_COUNT`.

| Metric | Qualification | Holdout | Envelope |
| --- | ---: | ---: | ---: |
| Favored aggressive-volume share | 587,027 ppm | 589,662 ppm | at least 600,000 ppm |
| Median favored signed displacement | 0 ticks | -2 ticks | at least 2 ticks |
| Positive roots | 1 of 8 | 0 of 4 | at least 6 of 8 / 3 of 4 |

Observed finding: the pressure nearly reached the requested one-sided aggressive-flow
share but did not transmit into sustained favored price movement. The untouched
holdout reproduced, and slightly strengthened, that negative result.

### Event shock and recovery

Event shock produced the same `SHOCK_OVER_PRE_RANGE_INSUFFICIENT` warning in both
partitions.

| Metric | Qualification | Holdout | Envelope |
| --- | ---: | ---: | ---: |
| Shock/pre-event aggressive volume | 6,096,618 ppm | 6,300,000 ppm | at least 1,500,000 ppm |
| Shock/pre-event range ratio | `INSUFFICIENT_EVIDENCE` | `INSUFFICIENT_EVIDENCE` | at least 1,200,000 ppm |
| Shock spread p50 | 2 ticks | 2 ticks | comparison input |
| Recovery spread p50 | 2 ticks | 2 ticks | no greater than shock |
| Recovery two-sided occupancy | 1,000,000 ppm | 1,000,000 ppm | at least 900,000 ppm |

Every event root had zero pre-event quote range and zero shock quote range while both
periods remained quoted. The frozen range-basis rule therefore used quote range and
correctly classified the ratio as insufficient because its pre-event denominator was
zero. The corresponding trade ranges were 2 ticks in both periods, but the policy does
not substitute trade range when valid quotes are present.

Observed finding: the workload created a strong, repeatable volume shock and retained
perfect recovery liquidity, but it did not create a measurable quote-range shock.

### Disorderly open stabilization

Disorderly open produced the same `OPEN_CANCEL_RATE_VS_MIDDAY` warning in both
partitions.

| Metric | Qualification | Holdout | Envelope |
| --- | ---: | ---: | ---: |
| First-eight-percent/midday cancel-rate ratio | 0 ppm | 234,375 ppm | at least 1,500,000 ppm |
| First-eight-percent spread p50 | 2 ticks | 2 ticks | at least midday |
| Midday spread p50 | 2 ticks | 2 ticks | comparison input |
| Final-eighty-percent spread p50 | 2 ticks | 2 ticks | at most 8 ticks |
| Final-eighty-percent two-sided occupancy | 1,000,000 ppm | 1,000,000 ppm | at least 950,000 ppm |

All eight qualification roots recorded zero opening cancellations. Three holdout roots
also recorded zero, while one recorded a single opening cancellation. Midday roots
recorded two to six cancellations. The profile therefore had nothing substantial to
stabilize from: its opening spread was already the same two ticks as midday and final
trading.

Observed finding: later-day market quality was excellent, but the workload did not
produce the preregistered disorderly opening condition.

## Cross-profile interpretation

The following statements are inferences from the frozen observations, not additional
acceptance claims:

1. The full-day engine is deterministic and mechanically robust across the tested
   roots. Correctness, book continuity, and replay are not the immediate problem.
2. Current pressure multipliers clearly alter activity: candidate trade counts and the
   event shock's aggressive volume differ materially. They do not yet transmit strongly
   into spread or price formation. Every one of the 48 days remained fully two-sided
   with a maximum spread of exactly two ticks.
3. Quiet range is the only behaviorally qualified candidate. It is not automatically
   ready solely because the shared performance disposition is `FAIL`.
4. Trend needs stronger causal transmission from favored-side flow to price movement,
   not merely a small threshold adjustment: holdout produced zero positive roots.
5. Event shock needs a mechanism that creates observable quote-range response, or a
   separately preregistered range-basis policy. The existing result must not be
   retroactively reinterpreted using trade range.
6. Disorderly open needs an actual opening cancellation/spread disturbance. Scaling a
   near-zero realized opening-cancellation process does not create the intended regime.

No threshold or pressure profile should be changed in place based on these outcomes.
Any remediation must be a new committed and preregistered implementation/evidence
identity.

## Performance findings

The host satisfied the exact eligible-platform predicate: Darwin arm64, CPython
3.14.3, 10 logical CPUs, 24 GiB physical memory, and more than 12 GiB free governed
storage. Threshold classification was therefore active rather than `UNSUPPORTED`.

| Metric | Observed aggregate | Status |
| --- | ---: | --- |
| Complete run bytes | 8,345,643 bytes | `PASS` |
| Generation p50 | 78,598,274,875 ns (about 78.60 s) | `PASS` |
| Generation throughput | 7 outer events/s | `FAIL` |
| Largest checkpoint | 1,455,091 bytes | `PASS` |
| Peak RSS | 115,818,496 bytes | `PASS` |
| Replay p50 | 27,941,049,833 ns (about 27.94 s) | `PASS` |

Each of the three measured artifacts contained 592 outer events. Generation times were
78.60 s, 78.46 s, and 82.00 s; replay times were 28.36 s, 27.94 s, and 27.59 s. No
performance observation aborted and no hard-abort reason was emitted.

The aggregate performance status is `FAIL` solely because the preregistered throughput
pass/warning boundary requires at least 100 outer events/s and the observed rounded
throughput was 7. Storage size, checkpoint size, memory, generation latency, and replay
latency all passed. The dominant implementation bottleneck is therefore CPU work per
outer event rather than artifact volume or memory pressure.

Raw wall time, RSS, and throughput remain persisted and verified but are excluded from
semantic simulation identity as preregistered.

## Blinded review packet

| Review inventory | Count |
| --- | ---: |
| Selected windows | 42 |
| Shortfalls | 0 |
| Explicitly not applicable | 3 |
| Total manifest/packet rows | 45 |

Every applicable candidate/stratum pair supplied both requested 60-second windows.
The three `NOT_APPLICABLE` rows are the event/post-event stratum for the three non-event
candidates. Selection used the separate frozen review RNG and the packet retains only
observable feed and phase-relative time. Human plausibility judgment has not been
performed, and all human statuses remain `PENDING` absent a reviewer sidecar.

## Immutable artifact inventory

| Artifact | Rows | SHA-256 |
| --- | ---: | --- |
| `qualification.json` | 48 | `9e0eb4e43ae5a8306a6ac4c8be24df4b9039d58c0c250ded255c3b50530bdf47` |
| `run-proofs.json` | 48 | `ed719f31782307beaea4a45db7d570f8f853d5e42387009bfe49261cf999c551` |
| `review-source.json` | 48 | `4b7fbbde7181182f41e99e483c4f57655e80f5461343a0879d189bf4dd408004` |
| `review-selection.json` | 45 | `5346074838f3b7aa97397246f1fe360b238deff805b6b9237d89735e64a31eac` |
| `review-packet.json` | 45 | `68030765fdf4c0e446d99b3a07c558e0bc06a5f0505a99dd0547513b6fd6dc86` |
| `performance.json` | n/a | `feceba79ee0859bdf7367602bec9ed32008aee37c18ed8f1c8cc70eb8dff259a` |
| `ledger.json` | n/a | `03d277a3ef5e46f7a9251d83762121091bde025aed238de927e0e44e44743a3c` |
| `reveal-token.json` | n/a | `08412c271ded276eb209de84fae4896a7a29ae394e82655ca89ce0a758214995` |

Verification passed exact artifact inventory, artifact digests, canonical payloads,
schema inventory, configuration/evidence/result digests, reveal-token binding, and
replay validity with no failures.

## Required audit evidence

The required commands completed against the exact immutable evidence root:

```text
qualify-day-profiles-demo
  VERIFIED_IMMUTABLE_EVIDENCE
  run_id=run-bf21e61c4b0d50862f901be1
  verification=PASS
  failures=0

audit-full-day --qualification-evidence .kirby2/full_day/qualification
  AUDIT_FULL_DAY PASS
  cases=97
  failures=0

audit-expansion --gate WO31-I1
  EXPANSION_AUDIT PASS
  checks=1
  failures=0
  warnings=0
  human_acceptance=PENDING

audit-model-risk-lab
  MODEL_RISK_LAB_AUDIT PASS
  cases=21
  failures=0
```

The full-day audit retained two optional historical `NOT_EXERCISED` declarations:
the earlier contract-only runtime-restore capability row and the earlier deferred
Hawkes-composition row. They are not required WO31-I1 failures; later owned restore and
Hawkes composition cases in the same audit passed. Statistical qualification remains
`NOT_APPLICABLE`, and human review remains the principal unexercised qualification
authority.

## Acceptance boundary and next work

WO31-I1 is complete as an evidence card:

- every preregistered qualification and holdout root executed exactly once;
- every failure and warning remained in its declared denominator;
- evidence and replay verification passed;
- review selection completed without shortfalls;
- performance was measured on an eligible host and reported truthfully;
- human review was not inferred;
- no post-reveal policy or implementation byte changed.

The planned next implementation card is WO32-A, the canonical scenario source and
three-part identity model. Until a separately preregistered remediation is completed,
scenario-language work must treat quiet range as behaviorally qualified but
performance-blocked, and the trend, event-shock, and disorderly-open candidates as
behaviorally `NOT_READY`. None may be silently promoted to an accepted human profile.
