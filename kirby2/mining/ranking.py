"""Transparent fixed-point difficulty and frequency reporting for WO33-C.

The difficulty number is an inspectable ordering heuristic.  It is deliberately
labelled ``UNVALIDATED_ESTIMATE`` and never presented as measured pedagogy.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .models import (
    POLICY_SCALE_V1,
    CandidateDirectionV1,
    DifficultyProjectionV1,
    EvidenceClassV1,
    LessonCandidateV1,
    RarityProjectionV1,
    round_div_even,
    unsigned_share_ppm,
)


POSITIVE_INFINITY_V1 = "POSITIVE_INFINITY"
DIFFICULTY_POLICY_ID_V1 = "LESSON_DIFFICULTY_V1"
DIFFICULTY_ESTIMATE_STATE_V1 = "UNVALIDATED_ESTIMATE"


class SignalClauseOrientationV1(str, Enum):
    NEGATIVE_UPPER_BOUND = "NEGATIVE_UPPER_BOUND"
    ABSOLUTE_MAGNITUDE = "ABSOLUTE_MAGNITUDE"
    DIRECTIONAL = "DIRECTIONAL"


def _require_exact_int(value: int, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an exact integer >= {minimum}")
    return value


def _require_ppm(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= POLICY_SCALE_V1:
        raise ValueError(f"{label} must be an integer in [0, 1000000]")
    return value


def _clamp_ppm(value: int) -> int:
    return min(POLICY_SCALE_V1, max(0, value))


def orient_signal_magnitude_v1(
    observed_value: int,
    orientation: SignalClauseOrientationV1,
    *,
    direction: CandidateDirectionV1 | None = None,
) -> int:
    """Orient a signed activation observation before legibility scoring."""

    if type(observed_value) is not int:
        raise TypeError("signed activation observation must be an exact integer")
    if not isinstance(orientation, SignalClauseOrientationV1):
        raise TypeError("signal-clause orientation is invalid")
    if orientation is SignalClauseOrientationV1.NEGATIVE_UPPER_BOUND:
        if direction is not None:
            raise ValueError("negative upper-bound orientation does not use direction")
        return -observed_value
    if orientation is SignalClauseOrientationV1.ABSOLUTE_MAGNITUDE:
        if direction is not None:
            raise ValueError("absolute orientation does not use direction")
        return abs(observed_value)
    if direction is CandidateDirectionV1.BUY:
        return observed_value
    if direction is CandidateDirectionV1.SELL:
        return -observed_value
    raise ValueError("directional orientation requires BUY or SELL")


def lower_bound_legibility_ppm(
    observed_magnitude: int | str,
    lower_bound: int,
) -> int:
    """Score one satisfied ``x >= L`` activation clause.

    ``POSITIVE_INFINITY`` is maximally legible and is never passed through integer
    arithmetic.  Other sentinels and observations below the activation bound fail
    closed because they are not satisfied candidate evidence.
    """

    _require_exact_int(lower_bound, "lower bound", minimum=1)
    if observed_magnitude == POSITIVE_INFINITY_V1:
        return POLICY_SCALE_V1
    _require_exact_int(observed_magnitude, "observed magnitude")
    if observed_magnitude < lower_bound:
        raise ValueError("lower-bound clause is not satisfied")
    return _clamp_ppm(
        round_div_even(
            observed_magnitude * POLICY_SCALE_V1,
            2 * lower_bound,
        )
    )


def upper_bound_legibility_ppm(observed_magnitude: int, upper_bound: int) -> int:
    """Score one satisfied nonnegative ``x <= U`` activation clause."""

    _require_exact_int(upper_bound, "upper bound")
    _require_exact_int(observed_magnitude, "observed magnitude")
    if observed_magnitude > upper_bound:
        raise ValueError("upper-bound clause is not satisfied")
    return _clamp_ppm(
        round_div_even(
            (2 * upper_bound - observed_magnitude) * POLICY_SCALE_V1,
            max(1, 2 * upper_bound),
        )
    )


def boolean_legibility_ppm(satisfied: bool) -> int:
    """Return the exact legibility of a required Boolean/state predicate."""

    if type(satisfied) is not bool:
        raise TypeError("Boolean clause satisfaction must be an exact bool")
    if not satisfied:
        raise ValueError("required Boolean clause is not satisfied")
    return POLICY_SCALE_V1


def and_legibility_ppm(clause_values: Sequence[int]) -> int:
    """Combine satisfied AND clauses by their least-legible clause."""

    values = tuple(clause_values)
    if not values:
        raise ValueError("AND legibility requires at least one clause")
    for value in values:
        _require_ppm(value, "AND clause legibility")
    return min(values)


def or_legibility_ppm(fully_satisfied_branch_values: Sequence[int]) -> int:
    """Combine fully satisfied OR branches by their most-legible branch."""

    values = tuple(fully_satisfied_branch_values)
    if not values:
        raise ValueError("OR legibility requires a fully satisfied branch")
    for value in values:
        _require_ppm(value, "OR branch legibility")
    return max(values)


def duration_legibility_ppm(
    observed_contiguous_duration_us: int | None,
    required_duration_us: int | None,
) -> int | None:
    """Score persistence, or return ``None`` when persistence is inapplicable."""

    if required_duration_us is None:
        if observed_contiguous_duration_us is not None:
            raise ValueError("duration observation has no persistence requirement")
        return None
    _require_exact_int(required_duration_us, "required duration", minimum=1)
    _require_exact_int(
        observed_contiguous_duration_us,
        "observed contiguous duration",
    )
    if observed_contiguous_duration_us < required_duration_us:
        raise ValueError("persistence clause is not satisfied")
    return _clamp_ppm(
        round_div_even(
            observed_contiguous_duration_us * POLICY_SCALE_V1,
            2 * required_duration_us,
        )
    )


def aggressive_conflict_ppm(
    same_direction_aggressive_shares: int,
    opposite_direction_aggressive_shares: int,
) -> int:
    """Measure opposite aggressive flow in the exact activation interval."""

    same = _require_exact_int(
        same_direction_aggressive_shares,
        "same-direction aggressive shares",
    )
    opposite = _require_exact_int(
        opposite_direction_aggressive_shares,
        "opposite-direction aggressive shares",
    )
    if same + opposite == 0:
        raise ValueError("directional conflict has no aggressive-share evidence")
    return unsigned_share_ppm(opposite, same + opposite)


def build_difficulty_projection(
    *,
    signal_legibility_ppm: int,
    duration_legibility_ppm: int | None,
    conflict_ppm: int | None,
    reaction_us: int,
    spread_ticks: int | None,
    latency_us: int | None,
    three_level_depth: int | None,
    venue_count: int | None,
    hidden_liquidity_relevant: bool,
    feature_count: int,
    evidence_class: EvidenceClassV1,
) -> DifficultyProjectionV1:
    """Build the exact V1 classification projection from observable inputs."""

    if type(hidden_liquidity_relevant) is not bool:
        raise TypeError("hidden-liquidity relevance must be an exact bool")
    if not isinstance(evidence_class, EvidenceClassV1):
        raise TypeError("difficulty evidence class is invalid")
    hidden_uncertainty = (
        {
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: 0,
            EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: 250_000,
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: 750_000,
        }[evidence_class]
        if hidden_liquidity_relevant
        else None
    )
    return DifficultyProjectionV1(
        signal_legibility_ppm=signal_legibility_ppm,
        duration_legibility_ppm=duration_legibility_ppm,
        conflict_ppm=conflict_ppm,
        reaction_us=reaction_us,
        spread_ticks=spread_ticks,
        latency_us=latency_us,
        three_level_depth=three_level_depth,
        venue_count=venue_count,
        hidden_uncertainty_ppm=hidden_uncertainty,
        objective_shares=None,
        executable_depth=None,
        feature_count=feature_count,
        evidence_quality_ppm=evidence_class.evidence_quality_ppm,
    )


@dataclass(frozen=True, slots=True)
class FrequencyReportV1:
    """An observed frequency, with rarity only for an explicit population."""

    qualifying_units: int
    eligible_units: int
    reference_population: str | None = None

    def __post_init__(self) -> None:
        _require_exact_int(self.qualifying_units, "qualifying units")
        _require_exact_int(self.eligible_units, "eligible units", minimum=1)
        if self.qualifying_units > self.eligible_units:
            raise ValueError("qualifying units cannot exceed eligible units")
        if self.reference_population is not None and (
            type(self.reference_population) is not str
            or not self.reference_population
        ):
            raise ValueError("reference population must be nonempty text or null")

    @property
    def sample_frequency_ppm(self) -> int:
        return unsigned_share_ppm(self.qualifying_units, self.eligible_units)

    @property
    def rarity_ppm(self) -> int | None:
        if self.reference_population is None:
            return None
        return POLICY_SCALE_V1 - self.sample_frequency_ppm

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible_units": self.eligible_units,
            "qualifying_units": self.qualifying_units,
            "rarity_ppm": self.rarity_ppm,
            "reference_population": self.reference_population,
            "sample_frequency_ppm": self.sample_frequency_ppm,
            "schema_version": 1,
        }

    def as_rarity_projection(self) -> RarityProjectionV1:
        if self.reference_population is None:
            raise ValueError("rarity requires an explicit reference population")
        return RarityProjectionV1(
            qualification_source_row=self.reference_population,
            qualifying_units=self.qualifying_units,
            eligible_units=self.eligible_units,
        )


@dataclass(frozen=True, slots=True)
class CandidateRankV1:
    ordinal: int
    candidate: LessonCandidateV1 = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("candidate rank ordinal must be positive")
        if not isinstance(self.candidate, LessonCandidateV1):
            raise TypeError("candidate rank requires a lesson candidate")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "difficulty_estimate": (
                self.candidate.difficulty_projection.inspection_projection()
            ),
            "ordinal": self.ordinal,
            "rarity_projection": self.candidate.rarity_projection.as_dict(),
            "schema_version": 1,
        }


def difficulty_order_key(candidate: LessonCandidateV1) -> tuple[int, int, bytes]:
    if not isinstance(candidate, LessonCandidateV1):
        raise TypeError("difficulty ordering requires a lesson candidate")
    return (
        candidate.difficulty_projection.difficulty_ppm,
        candidate.bounds.active_start_us,
        candidate.candidate_id.encode("utf-8"),
    )


def rank_candidates(
    candidates: Iterable[LessonCandidateV1],
) -> tuple[CandidateRankV1, ...]:
    """Order candidates deterministically without treating rarity as validity."""

    pool = tuple(candidates)
    if any(not isinstance(item, LessonCandidateV1) for item in pool):
        raise TypeError("ranking pool contains a non-candidate")
    candidate_ids = tuple(item.candidate_id for item in pool)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("ranking pool contains repeated candidate identity")
    ordered = sorted(pool, key=difficulty_order_key)
    return tuple(
        CandidateRankV1(ordinal=index, candidate=candidate)
        for index, candidate in enumerate(ordered, start=1)
    )


__all__ = [
    "DIFFICULTY_ESTIMATE_STATE_V1",
    "DIFFICULTY_POLICY_ID_V1",
    "POSITIVE_INFINITY_V1",
    "SignalClauseOrientationV1",
    "CandidateRankV1",
    "FrequencyReportV1",
    "aggressive_conflict_ppm",
    "and_legibility_ppm",
    "boolean_legibility_ppm",
    "build_difficulty_projection",
    "difficulty_order_key",
    "duration_legibility_ppm",
    "lower_bound_legibility_ppm",
    "or_legibility_ppm",
    "orient_signal_magnitude_v1",
    "rank_candidates",
    "upper_bound_legibility_ppm",
]
