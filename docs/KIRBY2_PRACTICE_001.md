# KIRBY2-PRACTICE-001 — Packet A public episode preparation

Status: `IMPLEMENTED_INTERNAL_AUDIT_PASS_PENDING_SOL_REVIEW`

## Scope

Packet A prepares one catalog-authored deterministic practice cut through existing
public simulation operations. It introduces no UI wiring, catalog UI, drill
progression, eligibility logic, release qualification, or external persistence.

The public builder pins an episode recipe whose request and identity both seal
`prefix_timing_policy = ACTIONS_AT_T0_THEN_ADVANCE_TO_ANCHOR_V1`. The preparation
facade then performs:

1. normal `start_simulation_run()` producing the existing `READY`, T=0 Start record;
2. the supplied ordered semantic prefix at T=0;
3. one normal absolute `advance_simulation_run()` to the pinned anchor; and
4. normal `SIMULATION_PAUSE`, returning an opaque active handle and a T=anchor
   `PAUSED` frame.

V1 intentionally represents only that timing model. It does **not** claim to support
commands at arbitrary timestamps within a prefix; any such extension needs a separate
versioned schedule schema and acceptance review.

## Public evidence boundary

`SimulationEpisodePreparedResultV1` carries both:

- `prefix_projection_sha256`, a source-ID-free public frame/model comparison; and
- `full_model_prefix_sha256` with projection ID
  `KIRBY2_SIMULATION_FULL_MODEL_PREFIX_PROJECTION_V1`.

The latter is an opaque commitment to a canonical, source-independent backend model
projection: pinned run request and prefix plus `LiveMarketSession.branch_runtime_state`.
It covers engine/arrival history, market and queue state, player and working orders,
input history, strategy/objective state, and deterministic counters. No branch-state
bytes, replay inventory, or private handle fields enter the UI wire record.

`verify_prepared_simulation_episode()` compares an active opaque handle with that
commitment and returns only a strict `MATCH` or `MISMATCH` result. A prepared handle is
settled only by finalization or `release_simulation_episode()`.

## Internal audit command and result

```text
python3 -B -m kirby2.audit.simulation_episode
```

The audit reports three passing cases:

- `A01_CONTROL_RESIDUAL_PREPARE`: two independently allocated source runs reach the
  same T=1 `PAUSED` control-residual cut (100 filled shares and one working order),
  with equal public and full-model prefix digests.
- `A02_CONTINUATION_REPLAY_VERIFICATION`: both cuts accept the same post-anchor
  `PLAYER_CANCEL_NEAREST` command with the same semantic-outcome projection and
  source-independent final full-model digest, finalize with `ALLOW_PARTIAL`, and
  their stored Replay artifacts resolve through the existing public deep verifier.
- `A03_BAD_REQUEST_CLEANUP_AND_HIDDEN_DEPENDENCY`: a stale identity digest is refused
  pre-allocation; a prefix that leaves the run paused is closed with
  `USER_ABANDONED`; and an increase/decrease pair restores the visible book/account
  while changing hidden input-history dependencies. Rebinding the later public frame
  to the old full-model commitment produces `FULL_MODEL_PREFIX_MISMATCH`. The existing
  V1 Start fixture still validates `READY` at T=0; hostile frame and Start records
  with recomputed canonical IDs reject `READY` at T=1 by the lifecycle invariant.

The audit is intentionally backend-public-boundary only: it imports `kirby2.ui`, not
private live-session, replay-store, or handle internals.

## What this proves

- A genuine ordinary simulation prefix can prepare the specified residual-order
  practice state without checkpoint injection or clock rebasing.
- The original Start contract stays zero-time `READY`; the prepared anchor is a
  distinct `PAUSED` state.
- Independent preparation, continuation, artifact finalization, and Replay
  verification work through the public facade.
- A full-model commitment detects required non-visible runtime-history changes.

## What this does not prove

- UI/Qt readiness, UI integration, or a user-facing practice-library workflow.
- Broad episode catalog coverage, arbitrary prefix timings, scoring, persistence,
  eligibility, drill progression, performance/release qualification, or production
  acceptance.
- Approval of this packet as a donor or release-ready surface. Sol review remains the
  gate for that decision.
