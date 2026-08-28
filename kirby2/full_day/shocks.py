"""Bounded deterministic unscheduled shocks for composed full-day runs.

Candidate generation consumes only the plan-declared runtime shock substream.
One pending candidate is retained at a time, every emitted candidate resolves
synchronously to exactly one accepted or rejected outcome, and checkpoint
restore replays the finite draw history to verify the complete RNG state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kirby2.simulation.rng import SeededRng

from .models import (
    FlowSideV1,
    FullDayPlanV1,
    UnscheduledShockPolicyV1,
    VersionedReferenceV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)
from .states import ParameterEffectV1


SHOCK_RUNTIME_SCHEMA_VERSION = 1
SHOCK_RUNTIME_ABSENT_REASON = "FULL_DAY_COMPOSITION_NOT_REQUESTED"


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


@dataclass(frozen=True, slots=True)
class ShockQuantityDistributionV1:
    """Executable uniform-integer quantity law bound to the plan reference."""

    reference: VersionedReferenceV1
    minimum_quantity: int
    maximum_quantity: int

    def __post_init__(self) -> None:
        if type(self.reference) is not VersionedReferenceV1:
            raise TypeError("shock quantity distribution requires a versioned reference")
        _exact_int(self.minimum_quantity, "shock minimum quantity", minimum=1)
        _exact_int(self.maximum_quantity, "shock maximum quantity", minimum=1)
        if self.maximum_quantity < self.minimum_quantity:
            raise ValueError("shock quantity distribution bounds are reversed")
        if self.reference.sha256 != canonical_sha256(self.semantic_identity_dict()):
            raise ValueError(
                "shock quantity distribution bytes differ from the plan digest"
            )

    def semantic_identity_dict(self) -> dict[str, object]:
        return {
            "distribution": "UNIFORM_INTEGER_INCLUSIVE",
            "maximum_quantity": self.maximum_quantity,
            "minimum_quantity": self.minimum_quantity,
        }

    def draw(self, rng: SeededRng) -> int:
        if type(rng) is not SeededRng:
            raise TypeError("shock quantity distribution requires SeededRng")
        return rng.integer(self.minimum_quantity, self.maximum_quantity)

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_quantity": self.maximum_quantity,
            "minimum_quantity": self.minimum_quantity,
            "reference": self.reference.as_dict(),
            "schema_version": 1,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ShockQuantityDistributionV1:
        _require_exact_fields(
            payload,
            {"maximum_quantity", "minimum_quantity", "reference", "schema_version"},
            "shock quantity distribution",
        )
        if payload["schema_version"] != 1:
            raise ValueError("shock quantity distribution schema is unsupported")
        reference = payload["reference"]
        if not isinstance(reference, Mapping):
            raise TypeError("shock quantity distribution reference must be an object")
        return cls(
            reference=VersionedReferenceV1.from_dict(reference),
            minimum_quantity=_exact_int(
                payload["minimum_quantity"], "minimum_quantity", minimum=1
            ),
            maximum_quantity=_exact_int(
                payload["maximum_quantity"], "maximum_quantity", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class ShockCandidateV1:
    candidate_id: str
    proposal_sequence: int
    scheduled_time_us: int
    information_cutoff_us: int
    side: FlowSideV1
    quantity_shares: int
    acceptance_draw: int

    def __post_init__(self) -> None:
        _exact_string(self.candidate_id, "shock candidate ID")
        _exact_int(self.proposal_sequence, "shock proposal sequence", minimum=1)
        _exact_int(self.scheduled_time_us, "shock scheduled time")
        _exact_int(self.information_cutoff_us, "shock information cutoff")
        if self.information_cutoff_us != self.scheduled_time_us:
            raise ValueError("shock candidate cutoff must equal its scheduled time")
        if self.side not in {FlowSideV1.BUY, FlowSideV1.SELL}:
            raise ValueError("shock candidate side must be BUY or SELL")
        _exact_int(self.quantity_shares, "shock candidate quantity", minimum=1)
        _exact_int(self.acceptance_draw, "shock acceptance draw")
        if self.candidate_id != f"SHOCK-{self.proposal_sequence:06d}":
            raise ValueError("shock candidate ID differs from its proposal sequence")

    def as_dict(self) -> dict[str, object]:
        return {
            "acceptance_draw": self.acceptance_draw,
            "candidate_id": self.candidate_id,
            "information_cutoff_us": self.information_cutoff_us,
            "proposal_sequence": self.proposal_sequence,
            "quantity_shares": self.quantity_shares,
            "scheduled_time_us": self.scheduled_time_us,
            "side": self.side.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ShockCandidateV1:
        _require_exact_fields(
            payload,
            {
                "acceptance_draw",
                "candidate_id",
                "information_cutoff_us",
                "proposal_sequence",
                "quantity_shares",
                "scheduled_time_us",
                "side",
            },
            "shock candidate",
        )
        side = payload["side"]
        if type(side) is not str:
            raise TypeError("shock candidate side must be a string")
        return cls(
            candidate_id=_exact_string(payload["candidate_id"], "candidate_id"),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence", minimum=1
            ),
            scheduled_time_us=_exact_int(
                payload["scheduled_time_us"], "scheduled_time_us"
            ),
            information_cutoff_us=_exact_int(
                payload["information_cutoff_us"], "information_cutoff_us"
            ),
            side=FlowSideV1(side),
            quantity_shares=_exact_int(
                payload["quantity_shares"], "quantity_shares", minimum=1
            ),
            acceptance_draw=_exact_int(
                payload["acceptance_draw"], "acceptance_draw"
            ),
        )


@dataclass(frozen=True, slots=True)
class ShockOutcomeV1:
    candidate: ShockCandidateV1
    accepted: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        if type(self.candidate) is not ShockCandidateV1:
            raise TypeError("shock outcome requires ShockCandidateV1")
        if type(self.accepted) is not bool:
            raise TypeError("shock outcome accepted flag must be boolean")
        if self.accepted:
            if self.reason_code is not None:
                raise ValueError("accepted shock cannot carry a rejection reason")
        elif type(self.reason_code) is not str or not self.reason_code:
            raise ValueError("rejected shock requires a reason code")

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "candidate": self.candidate.as_dict(),
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ShockOutcomeV1:
        _require_exact_fields(
            payload,
            {"accepted", "candidate", "reason_code"},
            "shock outcome",
        )
        candidate = payload["candidate"]
        accepted = payload["accepted"]
        reason = payload["reason_code"]
        if not isinstance(candidate, Mapping) or type(accepted) is not bool:
            raise TypeError("shock outcome fields are invalid")
        if reason is not None and type(reason) is not str:
            raise TypeError("shock rejection reason must be a string or null")
        return cls(ShockCandidateV1.from_dict(candidate), accepted, reason)


class UnscheduledShockRuntimeV1:
    """Finite deterministic candidate generator and lifecycle state."""

    def __init__(
        self,
        plan: FullDayPlanV1,
        distribution: ShockQuantityDistributionV1,
        *,
        rng: SeededRng | None = None,
        proposal_sequence: int = 0,
        candidate_draw_count: int = 0,
        accepted_count: int = 0,
        rejected_count: int = 0,
        last_accepted_time_us: int | None = None,
        pending_candidate: ShockCandidateV1 | None = None,
        outcomes: Sequence[ShockOutcomeV1] = (),
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("shock runtime requires FullDayPlanV1")
        if type(distribution) is not ShockQuantityDistributionV1:
            raise TypeError("shock runtime requires ShockQuantityDistributionV1")
        policy = plan.unscheduled_shock_policy
        if distribution.reference != policy.quantity_distribution_reference:
            raise ValueError("shock quantity distribution differs from the plan reference")
        rules = plan.instrument_profile.mechanics_rules
        if (
            distribution.minimum_quantity < rules.minimum_quantity
            or distribution.maximum_quantity > rules.maximum_quantity
        ):
            raise ValueError("shock quantity distribution exceeds mechanics bounds")
        self._plan = plan
        self.plan_sha256 = plan.semantic_sha256
        self.policy = policy
        self.policy_sha256 = canonical_sha256(policy.as_dict())
        self.distribution = distribution
        self.rng = rng or SeededRng(plan.seed_policy.derive(policy.substream_label))
        self.proposal_sequence = proposal_sequence
        self.candidate_draw_count = candidate_draw_count
        self.accepted_count = accepted_count
        self.rejected_count = rejected_count
        self.last_accepted_time_us = last_accepted_time_us
        self.pending_candidate = pending_candidate
        self.outcomes = list(outcomes)
        self.assert_invariants()

    @classmethod
    def create(
        cls,
        plan: FullDayPlanV1,
        distribution: ShockQuantityDistributionV1,
    ) -> UnscheduledShockRuntimeV1:
        return cls(plan, distribution)

    @property
    def exhausted(self) -> bool:
        return (
            not self.policy.enabled
            or self.candidate_draw_count >= self.policy.maximum_candidate_draws
        ) and self.pending_candidate is None

    @property
    def accepted_parameter_effect_batches(
        self,
    ) -> tuple[tuple[ParameterEffectV1, ...], ...]:
        return tuple(
            self.policy.parameter_effects for outcome in self.outcomes if outcome.accepted
        )

    def plan_next(self, *, not_before_us: int) -> ShockCandidateV1 | None:
        """Draw exactly one future candidate without an unbounded retry loop."""

        _exact_int(not_before_us, "shock not-before time")
        if self.pending_candidate is not None:
            raise RuntimeError("shock runtime already has a pending candidate")
        if not self.policy.enabled or self.proposal_sequence >= self.policy.maximum_candidate_draws:
            return None
        sequence = self.proposal_sequence + 1
        count = self.policy.maximum_candidate_draws
        width = self.policy.candidate_window_end_us - self.policy.candidate_window_start_us
        slot_start = self.policy.candidate_window_start_us + (
            width * (sequence - 1)
        ) // count
        slot_end_exclusive = self.policy.candidate_window_start_us + (
            width * sequence
        ) // count
        lower = max(slot_start, not_before_us)
        upper = max(lower, slot_end_exclusive - 1)
        if lower >= self.policy.candidate_window_end_us:
            raise RuntimeError("shock candidate planning crossed its bounded window")
        upper = min(upper, self.policy.candidate_window_end_us - 1)
        scheduled_time_us = self.rng.integer(lower, upper)
        side = self.policy.allowed_sides[
            self.rng.index(len(self.policy.allowed_sides))
        ]
        quantity = self.distribution.draw(self.rng)
        acceptance_draw = self.rng.index(self.policy.acceptance_denominator)
        candidate = ShockCandidateV1(
            candidate_id=f"SHOCK-{sequence:06d}",
            proposal_sequence=sequence,
            scheduled_time_us=scheduled_time_us,
            information_cutoff_us=scheduled_time_us,
            side=side,
            quantity_shares=quantity,
            acceptance_draw=acceptance_draw,
        )
        self.proposal_sequence = sequence
        self.pending_candidate = candidate
        self.assert_invariants()
        return candidate

    def resolve_due(
        self, candidate_id: str, *, simulation_time_us: int
    ) -> ShockOutcomeV1:
        candidate = self.pending_candidate
        if candidate is None or candidate.candidate_id != candidate_id:
            raise RuntimeError("shock work differs from the pending candidate")
        if candidate.scheduled_time_us != simulation_time_us:
            raise RuntimeError("shock candidate executed at the wrong time")
        reason: str | None = None
        if self.accepted_count >= self.policy.maximum_accepted_shocks:
            reason = "MAXIMUM_ACCEPTED_SHOCKS_REACHED"
        elif (
            self.last_accepted_time_us is not None
            and simulation_time_us - self.last_accepted_time_us
            < self.policy.minimum_spacing_us
        ):
            reason = "MINIMUM_SPACING_NOT_MET"
        elif candidate.acceptance_draw >= self.policy.acceptance_numerator:
            reason = "PROBABILITY_DRAW_REJECTED"
        accepted = reason is None
        outcome = ShockOutcomeV1(candidate, accepted, reason)
        self.pending_candidate = None
        self.candidate_draw_count += 1
        if accepted:
            self.accepted_count += 1
            self.last_accepted_time_us = simulation_time_us
        else:
            self.rejected_count += 1
        self.outcomes.append(outcome)
        self.assert_invariants()
        return outcome

    def assert_invariants(self) -> None:
        policy = self.policy
        if (
            self.plan_sha256 != self._plan.semantic_sha256
            or self.policy_sha256 != canonical_sha256(policy.as_dict())
            or policy != self._plan.unscheduled_shock_policy
        ):
            raise RuntimeError("shock runtime identity differs from the plan")
        expected_seed = self._plan.seed_policy.derive(policy.substream_label)
        if type(self.rng) is not SeededRng or self.rng.seed != expected_seed:
            raise RuntimeError("shock runtime RNG differs from its owned substream")
        counters = (
            self.proposal_sequence,
            self.candidate_draw_count,
            self.accepted_count,
            self.rejected_count,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise RuntimeError("shock runtime counters are invalid")
        if self.proposal_sequence > policy.maximum_candidate_draws:
            raise RuntimeError("shock proposal sequence exceeds its bound")
        if self.accepted_count > policy.maximum_accepted_shocks:
            raise RuntimeError("shock accepted count exceeds its bound")
        if self.candidate_draw_count != len(self.outcomes):
            raise RuntimeError("shock draw count differs from outcome history")
        if self.accepted_count + self.rejected_count != self.candidate_draw_count:
            raise RuntimeError("shock terminal counts do not conserve candidates")
        expected_proposals = self.candidate_draw_count + (
            1 if self.pending_candidate is not None else 0
        )
        if self.proposal_sequence != expected_proposals:
            raise RuntimeError("shock proposal allocator differs from pending/history")
        for sequence, outcome in enumerate(self.outcomes, start=1):
            if outcome.candidate.proposal_sequence != sequence:
                raise RuntimeError("shock outcome history sequence is noncanonical")
            if outcome.candidate.acceptance_draw >= policy.acceptance_denominator:
                raise RuntimeError("shock acceptance draw exceeds its denominator")
            if outcome.candidate.side not in policy.allowed_sides:
                raise RuntimeError("shock outcome side is outside the policy")
            if not (
                self.distribution.minimum_quantity
                <= outcome.candidate.quantity_shares
                <= self.distribution.maximum_quantity
            ):
                raise RuntimeError("shock outcome quantity is outside its distribution")
        accepted_times = [
            outcome.candidate.scheduled_time_us
            for outcome in self.outcomes
            if outcome.accepted
        ]
        expected_last = None if not accepted_times else accepted_times[-1]
        if self.last_accepted_time_us != expected_last:
            raise RuntimeError("shock last accepted time differs from its history")
        if self.pending_candidate is not None:
            if self.pending_candidate.proposal_sequence != self.proposal_sequence:
                raise RuntimeError("pending shock candidate sequence is inconsistent")
            if self.pending_candidate.acceptance_draw >= policy.acceptance_denominator:
                raise RuntimeError("pending shock acceptance draw exceeds its denominator")

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        state = {
            "accepted_count": self.accepted_count,
            "candidate_draw_count": self.candidate_draw_count,
            "distribution": self.distribution.as_dict(),
            "last_accepted_time_us": self.last_accepted_time_us,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "pending_candidate": (
                None
                if self.pending_candidate is None
                else self.pending_candidate.as_dict()
            ),
            "plan_sha256": self.plan_sha256,
            "policy_sha256": self.policy_sha256,
            "proposal_sequence": self.proposal_sequence,
            "rejected_count": self.rejected_count,
            "rng_state": self.rng.runtime_state(),
            "schema_version": SHOCK_RUNTIME_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls, payload: Mapping[str, object], *, plan: FullDayPlanV1
    ) -> UnscheduledShockRuntimeV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "accepted_count",
                "candidate_draw_count",
                "distribution",
                "last_accepted_time_us",
                "outcomes",
                "pending_candidate",
                "plan_sha256",
                "policy_sha256",
                "proposal_sequence",
                "rejected_count",
                "rng_state",
                "schema_version",
            },
            "shock runtime",
        )
        if payload["schema_version"] != SHOCK_RUNTIME_SCHEMA_VERSION:
            raise ValueError("shock runtime schema is unsupported")
        if payload["plan_sha256"] != plan.semantic_sha256:
            raise ValueError("shock runtime plan identity mismatch")
        if payload["policy_sha256"] != canonical_sha256(
            plan.unscheduled_shock_policy.as_dict()
        ):
            raise ValueError("shock runtime policy identity mismatch")
        raw_distribution = payload["distribution"]
        raw_rng = payload["rng_state"]
        raw_pending = payload["pending_candidate"]
        raw_outcomes = payload["outcomes"]
        if not isinstance(raw_distribution, Mapping) or not isinstance(raw_rng, Mapping):
            raise TypeError("shock distribution and RNG state must be objects")
        if raw_pending is not None and not isinstance(raw_pending, Mapping):
            raise TypeError("pending shock candidate must be an object or null")
        if type(raw_outcomes) is not list or any(
            not isinstance(row, Mapping) for row in raw_outcomes
        ):
            raise TypeError("shock outcomes must be an object array")
        last_accepted = payload["last_accepted_time_us"]
        if last_accepted is not None:
            last_accepted = _exact_int(last_accepted, "last_accepted_time_us")
        runtime = cls(
            plan,
            ShockQuantityDistributionV1.from_dict(raw_distribution),
            rng=SeededRng.from_runtime_state(raw_rng),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence"
            ),
            candidate_draw_count=_exact_int(
                payload["candidate_draw_count"], "candidate_draw_count"
            ),
            accepted_count=_exact_int(payload["accepted_count"], "accepted_count"),
            rejected_count=_exact_int(payload["rejected_count"], "rejected_count"),
            last_accepted_time_us=last_accepted,
            pending_candidate=(
                None
                if raw_pending is None
                else ShockCandidateV1.from_dict(raw_pending)
            ),
            outcomes=tuple(ShockOutcomeV1.from_dict(row) for row in raw_outcomes),
        )

        # Recreate the complete finite draw prefix from the plan seed.  This
        # binds candidate timing, side, quantity, probability draws, counters,
        # pending work, and the portable PRNG state in one hostile-input check.
        replay = cls.create(plan, runtime.distribution)
        for expected_outcome in runtime.outcomes:
            candidate = replay.plan_next(
                not_before_us=(
                    plan.unscheduled_shock_policy.candidate_window_start_us
                    if replay.pending_candidate is None and not replay.outcomes
                    else replay.outcomes[-1].candidate.scheduled_time_us
                )
            )
            if candidate != expected_outcome.candidate:
                raise ValueError("shock checkpoint candidate history differs from replay")
            actual_outcome = replay.resolve_due(
                candidate.candidate_id,
                simulation_time_us=candidate.scheduled_time_us,
            )
            if actual_outcome != expected_outcome:
                raise ValueError("shock checkpoint terminal history differs from replay")
        if runtime.pending_candidate is not None:
            not_before = (
                plan.unscheduled_shock_policy.candidate_window_start_us
                if not replay.outcomes
                else replay.outcomes[-1].candidate.scheduled_time_us
            )
            if replay.plan_next(not_before_us=not_before) != runtime.pending_candidate:
                raise ValueError("pending shock candidate differs from replay")
        if canonical_json_bytes(replay.checkpoint_state()) != canonical_json_bytes(payload):
            raise ValueError("shock runtime checkpoint is not a canonical replay fixed point")
        return runtime

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())


__all__ = [
    "SHOCK_RUNTIME_ABSENT_REASON",
    "SHOCK_RUNTIME_SCHEMA_VERSION",
    "ShockCandidateV1",
    "ShockOutcomeV1",
    "ShockQuantityDistributionV1",
    "UnscheduledShockRuntimeV1",
]
