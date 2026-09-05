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
projection: the pinned run request, the opaque handle's exact prepared request and
recipe identity (request ID, episode ID/version, timing policy, and prefix), plus
`LiveMarketSession.branch_runtime_state`. It covers engine/arrival history, market
and queue state, player and working orders, input history, strategy/objective state,
and deterministic counters. No branch-state bytes, replay inventory, or private
handle fields enter the UI wire record. The backend intentionally exports no general
full-model-digest oracle to UI consumers.

`verify_prepared_simulation_episode()` compares an active opaque handle with that
commitment and returns only a strict `MATCH` or `MISMATCH` result. A prepared handle is
settled only by finalization or `release_simulation_episode()`.

## Internal audit command and result

```text
python3 -B -m kirby2.audit.simulation_episode
```

The audit reports four passing cases:

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
- `A04_IDENTITY_REBIND_SCHEMA_AND_START_CLEANUP`: recomputed prepared-result outer
  IDs cannot rebind episode ID/version, request ID, timing policy, or prefix identity
  to the original opaque handle; all five Packet A decoders reject
  `schema_version=true`; and an invalid or nonavailable Start record paired with a
  non-null allocated handle is closed as `USER_ABANDONED` rather than losing cleanup
  ownership. A
  malformed close projection instead returns the exact retained opaque cleanup handle
  with `CLEANUP_UNCONFIRMED`.

The audit uses public episode operations and one backend-private opaque commitment
helper solely to compare final-model identities. It does not read private
live-session, replay-store, or handle state.

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

## Packet B — curated decision repeater

Status: `IMPLEMENTED_INTERNAL_AUDIT_PASS_PENDING_SOL_REVIEW`

Packet B adds a process-local, no-Qt practice boundary over Packet A. It publishes
exactly six immutable public recipes and creates a fresh ordinary prepared source
for every attempt. The catalog records the complete pinned profile reference, seed,
all selected control values, T=0 preparation actions, anchor, recipe digest, and
variation parent. No checkpoint injection, hidden regime label, replay inventory, or
simulation-private handle field is exposed through the new public records.

| Recipe | Recipe SHA-256 | Measured anchor |
| --- | --- | --- |
| `practice.f1.place-and-cancel.v1` | `e5245133976ab5e0766f3cc164a5279ee4787c218ee3d7353c4b2bbd10500586` | balanced/simple seed 101; T=1; learner increases 100→200, places, recognizes, then cancels |
| `practice.f1.place-and-replace.v1` | `656a2515109fb42e23297a1801dc1199725f9f2bcac7aa5923c019296e29fee4` | balanced/simple seed 102; T=1; learner decreases 100→50, places, recognizes, then replaces |
| `practice.f2.public-pressure.v1` | `62ef9ec704b92bd45b9aaf09a61a7d21703da09b7c960c465b86e6693e228122` | buy-pressure/simple seed 202; NORMAL/1.00x/1,000,000 ppm; T=1,000,000; 13 public trades and 3,050 bid vs 0 ask displayed quantity |
| `practice.f2.replenishment.v1` | `8b8b748e2156ef187dc5e490ccb09e8823e5e8aaac668ab0c26e3085cc7a3d21` | balanced/simple seed 101; NORMAL/1.00x/1,000,000 ppm; T=1,000,000; 2 public trades and 1,300 bid vs 2,500 ask displayed quantity |
| `practice.f3.cancel-partial-residual.v1` | `a29b223fb7ad3342a1142a70132d41a46e4bc3dc80ffa6824995f4424fdb7474` | buy-pressure/simple seed 202; prefix Play + Buy Bid; T=1,000,000; one `PLAYER-O-000001`, 50 filled and 50 remaining, position 50 |
| `practice.f3.cancel-volume-variation.v1` | `bbea819721cd659794c94672b6649e941d46629b972c42e2fbabbdc31827b9e6` | buy-pressure/simple seed 190; NORMAL/0.50x/1,000,000 ppm; prefix Play + Buy Bid; T=1,000,000; the same one-order shape, 28 filled and 72 remaining |

F1’s learner actions are deliberately absent from preparation. Quantity choice,
placement, public resting-order recognition, and cancel/replace are individual
ordinary semantic commands. F3’s residual is not a market fill paired with a second
order: ordinary matching partially fills the exact player bid that remains public
and cancellable at the cut.

### Guided holds and assessment

Guided attempts bind an opaque handle, source run, frame, and cursor to one hold.
Generic command dispatch and advance return typed `GUIDED_HOLD_ACTIVE` unavailability
without changing the readable current frame. A wrong staged response records guided
feedback and sends no command. A correct staged response, `WAIT`, or a valid
`DECLINE` remains staged until a matching `CONTINUE` request revalidates every public
identity; it releases the hold once and issues at most its one staged semantic
command. Later mechanical steps receive a newly bound hold. Unassisted semantic
actions go through the ordinary dispatch boundary immediately.

The attempt request ID is also a process-local idempotency fence: a duplicate active
request returns the original Begin result only while its exact published anchor/hold
is still active. Once the attempt has progressed, retry returns a typed
`DUPLICATE_REQUEST_ALREADY_PROGRESSED` record with the current frame, step, hold, and
original opaque owner rather than allocating a second live source or republishing a
stale Begin state. It is not relabeled as a fresh attempt. If ordinary
command dispatch fails after Continue releases a hold, the facade returns an explicit
`SYSTEM_FAILURE` assessment and restores (or, after an already-published rejected
destination, rebinds) a guided hold; it does not silently leave an unlocked staged
step. Preparation publication failures close the acquired source, or return the
opaque cleanup owner with `CLEANUP_UNCONFIRMED` if close cannot be confirmed.

Wall-time evidence is optional and explicitly typed: `MONOTONIC_CALLER` carries a
positive declared resolution and nonnegative elapsed value; unavailable timing is
recorded as `UNAVAILABLE` with null measurement fields. No learner reaction time is
inferred from simulation time.

Assessment classes are `EXACT_MECHANICAL`, `DECLARED_RULE`, and `OPEN_JUDGMENT`.
Outcomes keep `PASS` and `FAIL` distinct from `NO_OPPORTUNITY`,
`INSUFFICIENT_EVIDENCE`, `LEARNER_ABORT`, and `SYSTEM_FAILURE`. F2 reads only the
published book and recent-trade fields using its recipe rule; it has no profile or
regime input. Public-rule branch audits also measure a thin-liquidity no-trade cut
(`0` trades, `850` bid / `825` ask) for `WAIT`/valid `DECLINE`, and a sell-pressure
cut (`8` trades, `2,250` bid / `1,800` ask) for the explicit
`INSUFFICIENT_EVIDENCE` answer. These are separately exercised public frames, not
extra catalog recipes or fabricated missing observations. Debriefs retain only public source/frame/cursor,
action request, semantic action, and order identifiers.

Practice catalog, request, and result decoders use exact nested record shapes and
recursively detached snapshots. Result decoding also binds attempt recipe/source and
prepared identity to its published episode/current frame, mode to hold nullability,
action-index bounds to the recipe, and assessment/debrief outcome and causal public
evidence to each other. The F3 cancellation debrief retains the pre-dispatch public
partial-order ID even though that order is absent from the post-cancel frame.

Exact repeats require the same recipe and create a fresh source; a variation must
name the original recipe as its declared parent and uses its own pinned recipe. This
V1 boundary is process-local only: it does not implement persistence, learner
progression, eligibility, UI/Qt wiring, release qualification, or a broader episode
catalog.

### Packet B audit

```text
python3 -B -m kirby2.audit.simulation_practice
python3 -B -m kirby2.audit.simulation_episode
```

The first audit covers the immutable six-recipe catalog and hostile decoder input,
measured F2/F3 cuts, represented pressure/replenishment/no-opportunity/insufficient
answers, guided hold blocking and exactly-once release, unassisted dispatch, and
fresh exact-repeat/variation lineage. It also proves idempotent retry does not open a
second source, nested mutation/unknown-field/bool-versus-int rejection, causal F3
debrief retention, and the failure-path rehold transaction. The second is the
required Packet A regression.
