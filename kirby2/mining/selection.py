"""Preregistered greedy diversity selection and explicit shortfall reporting."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from kirby2.full_day.profiles import policy_tie_digest

from .deduplication import DeduplicationResultV1, deduplicate_candidates
from .models import (
    CandidateDirectionV1,
    CandidatePresentationModeV1,
    LessonCandidateV1,
    POLICY_SCALE_V1,
    SESSION_PHASE_VALUES_V1,
    SourceWindowOutcomeV1,
    round_div_even,
)


LESSON_MINING_POLICY_VERSION_V1 = "LESSON_MINING_V1"
REVIEW_TIE_CONTEXT_V1 = "WO33_REVIEW_V1"
REVIEW_SELECTION_ROOT_V1 = 3_399_001
REVIEW_TARGET_COUNT_V1 = 20

SOURCE_ORDER_V1 = ("event", "quiet", "hidden", "fragmented", "historical")
RESERVED_COUNTS_V1 = MappingProxyType(
    {
        "event": 5,
        "quiet": 3,
        "hidden": 3,
        "fragmented": 3,
        "historical": 3,
    }
)
DIVERSITY_DIMENSIONS_V1 = (
    "primary_skill",
    "detector_family",
    "source",
    "phase",
    "source_window_outcome",
    "difficulty_band",
)
DIVERSITY_WEIGHTS_PPM_V1 = MappingProxyType(
    {
        "primary_skill": 250_000,
        "detector_family": 200_000,
        "source": 200_000,
        "phase": 100_000,
        "source_window_outcome": 100_000,
        "difficulty_band": 150_000,
    }
)
DIFFICULTY_BANDS_V1 = (
    (0, 250_000, False, "[0,250000)"),
    (250_000, 500_000, False, "[250000,500000)"),
    (500_000, 750_000, False, "[500000,750000)"),
    (750_000, POLICY_SCALE_V1, True, "[750000,1000000]"),
)
SESSION_PHASES_V1 = SESSION_PHASE_VALUES_V1

_FAMILY_MEMBERS_V1 = {
    "QUEUE": (
        "STRONG_QUEUE_IMBALANCE",
        "QUEUE_DEPLETION",
        "QUEUE_REPLENISHMENT",
    ),
    "ABSORPTION": (
        "BID_ABSORPTION",
        "ASK_ABSORPTION",
        "HIDDEN_RESERVE_REFRESH",
        "APPARENT_LIQUIDITY_MIRAGE",
    ),
    "PRICE_LIQUIDITY": (
        "FAILED_BREAKOUT",
        "LIQUIDITY_VACUUM",
        "SPREAD_EXPANSION",
        "SPREAD_RECOVERY",
    ),
    "FLOW": (
        "AGGRESSIVE_FLOW_BURST",
        "CANCELLATION_BURST",
        "DISTRESSED_LIQUIDATION",
        "MOMENTUM_EXHAUSTION",
        "MEAN_REVERSION_TRANSITION",
    ),
    "EXECUTION": (
        "LATENCY_SENSITIVE_OPPORTUNITY",
        "CANCEL_FILL_RACE",
    ),
    "FRAGMENTATION": (
        "MULTI_VENUE_FRAGMENTATION",
        "ROUTING_DILEMMA",
    ),
    "SESSION": (
        "AUCTION_IMBALANCE_CHANGE",
        "HALT_REOPENING",
    ),
}
DETECTOR_FAMILY_BY_ID_V1 = MappingProxyType(
    {
        detector_id: family
        for family, detector_ids in _FAMILY_MEMBERS_V1.items()
        for detector_id in detector_ids
    }
)


class ReviewSelectionStageV1(str, Enum):
    EVENT_MATERIALLY_DISTINCT = "EVENT_MATERIALLY_DISTINCT"
    SOURCE_RESERVED = "SOURCE_RESERVED"
    GLOBAL_FILL = "GLOBAL_FILL"


def difficulty_band_v1(difficulty_ppm: int) -> str:
    if type(difficulty_ppm) is not int or not 0 <= difficulty_ppm <= POLICY_SCALE_V1:
        raise ValueError("difficulty must be an exact fixed-point value")
    for lower, upper, upper_inclusive, label in DIFFICULTY_BANDS_V1:
        if lower <= difficulty_ppm and (
            difficulty_ppm <= upper if upper_inclusive else difficulty_ppm < upper
        ):
            return label
    raise AssertionError("fixed difficulty bands do not cover the policy scale")


def detector_family_v1(detector_id: str) -> str:
    try:
        return DETECTOR_FAMILY_BY_ID_V1[detector_id]
    except KeyError as error:
        raise ValueError(f"detector has no preregistered family: {detector_id}") from error


def source_window_outcome_v1(
    direction: CandidateDirectionV1,
    activation_mid_x2: int | None,
    final_mid_x2: int | None,
) -> SourceWindowOutcomeV1:
    """Classify the mined path from the two frozen two-sided quote projections."""

    if not isinstance(direction, CandidateDirectionV1):
        raise TypeError("source-window direction is invalid")
    if direction is CandidateDirectionV1.NOT_APPLICABLE:
        return SourceWindowOutcomeV1.NOT_APPLICABLE
    for value, label in (
        (activation_mid_x2, "activation mid_x2"),
        (final_mid_x2, "final mid_x2"),
    ):
        if value is not None and type(value) is not int:
            raise TypeError(f"{label} must be an exact integer or null")
    if activation_mid_x2 is None or final_mid_x2 is None:
        return SourceWindowOutcomeV1.NOT_OBSERVABLE
    oriented = (
        final_mid_x2 - activation_mid_x2
        if direction is CandidateDirectionV1.BUY
        else activation_mid_x2 - final_mid_x2
    )
    if oriented >= 2:
        return SourceWindowOutcomeV1.CONTINUATION
    if oriented <= -2:
        return SourceWindowOutcomeV1.REVERSAL
    return SourceWindowOutcomeV1.STASIS


def candidate_dimension_values_v1(
    candidate: LessonCandidateV1,
) -> tuple[tuple[str, str], ...]:
    """Project exactly one value for each preregistered dimension."""

    if not isinstance(candidate, LessonCandidateV1):
        raise TypeError("diversity projection requires a lesson candidate")
    source = candidate.rarity_projection.qualification_source_row
    if source not in SOURCE_ORDER_V1:
        raise ValueError("candidate source is outside the five qualification rows")
    phase = candidate.observable_feature_summary.regime_signature.phase
    if phase not in SESSION_PHASES_V1:
        raise ValueError("candidate phase is outside the recorded session-phase enum")
    values = {
        "primary_skill": candidate.primary_skill_id,
        "detector_family": detector_family_v1(candidate.detector.detector_id),
        "source": source,
        "phase": phase,
        "source_window_outcome": candidate.source_window_outcome.value,
        "difficulty_band": difficulty_band_v1(
            candidate.difficulty_projection.difficulty_ppm
        ),
    }
    return tuple((name, values[name]) for name in DIVERSITY_DIMENSIONS_V1)


def _dimension_mapping(candidate: LessonCandidateV1) -> dict[str, str]:
    return dict(candidate_dimension_values_v1(candidate))


def marginal_diversity_v1(
    candidate: LessonCandidateV1,
    selected: Sequence[LessonCandidateV1],
) -> tuple[int, tuple[tuple[str, int], ...]]:
    """Return exact marginal score and visible per-dimension novelty."""

    if any(not isinstance(item, LessonCandidateV1) for item in selected):
        raise TypeError("selected diversity context contains a non-candidate")
    values = _dimension_mapping(candidate)
    selected_values = tuple(_dimension_mapping(item) for item in selected)
    novelty: list[tuple[str, int]] = []
    for dimension in DIVERSITY_DIMENSIONS_V1:
        selected_count = sum(
            item[dimension] == values[dimension] for item in selected_values
        )
        novelty.append(
            (
                dimension,
                round_div_even(POLICY_SCALE_V1, 1 + selected_count),
            )
        )
    score = round_div_even(
        sum(
            DIVERSITY_WEIGHTS_PPM_V1[dimension] * value
            for dimension, value in novelty
        ),
        POLICY_SCALE_V1,
    )
    return score, tuple(novelty)


def _tie_key(candidate: LessonCandidateV1) -> tuple[bytes, bytes]:
    return (
        policy_tie_digest(
            LESSON_MINING_POLICY_VERSION_V1,
            REVIEW_TIE_CONTEXT_V1,
            REVIEW_SELECTION_ROOT_V1,
            candidate.candidate_digest,
        ),
        candidate.candidate_id.encode("utf-8"),
    )


@dataclass(frozen=True, slots=True)
class DiversitySelectionDecisionV1:
    ordinal: int
    stage: ReviewSelectionStageV1
    candidate_id: str
    source: str
    marginal_score_ppm: int
    novelty_ppm: tuple[tuple[str, int], ...]
    dimension_values: tuple[tuple[str, str], ...]
    tie_digest: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("selection ordinal must be positive")
        if not isinstance(self.stage, ReviewSelectionStageV1):
            raise TypeError("selection stage is invalid")
        if type(self.candidate_id) is not str or not self.candidate_id:
            raise ValueError("selection candidate ID must be nonempty")
        if self.source not in SOURCE_ORDER_V1:
            raise ValueError("selection source is outside the qualification matrix")
        if (
            type(self.marginal_score_ppm) is not int
            or not 0 <= self.marginal_score_ppm <= POLICY_SCALE_V1
        ):
            raise ValueError("selection marginal score is not fixed-point")
        if tuple(name for name, _ in self.novelty_ppm) != DIVERSITY_DIMENSIONS_V1:
            raise ValueError("selection novelty dimensions differ from policy")
        if tuple(name for name, _ in self.dimension_values) != DIVERSITY_DIMENSIONS_V1:
            raise ValueError("selection dimension values differ from policy")
        if any(
            type(value) is not int or not 0 <= value <= POLICY_SCALE_V1
            for _, value in self.novelty_ppm
        ):
            raise ValueError("selection novelty is not exact fixed-point")
        if (
            type(self.tie_digest) is not str
            or len(self.tie_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.tie_digest)
        ):
            raise ValueError("selection tie digest is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "dimension_values": dict(self.dimension_values),
            "marginal_score_ppm": self.marginal_score_ppm,
            "novelty_ppm": dict(self.novelty_ppm),
            "ordinal": self.ordinal,
            "schema_version": 1,
            "source": self.source,
            "stage": self.stage.value,
            "tie_digest": self.tie_digest,
        }

    def technical_review_projection(self) -> dict[str, object]:
        """Redact the mined future outcome from a reviewer-facing decision."""

        payload = self.as_dict()
        dimensions = dict(self.dimension_values)
        dimensions["source_window_outcome"] = (
            "WITHHELD_DURING_TECHNICAL_REVIEW"
        )
        payload["dimension_values"] = dimensions
        return payload


@dataclass(frozen=True, slots=True)
class ReservedSourceShortfallV1:
    source: str
    reserved_count: int
    selected_in_reserved_step: int

    def __post_init__(self) -> None:
        if self.source not in SOURCE_ORDER_V1:
            raise ValueError("shortfall source is outside the qualification matrix")
        if (
            type(self.reserved_count) is not int
            or self.reserved_count < 0
            or type(self.selected_in_reserved_step) is not int
            or not 0 <= self.selected_in_reserved_step <= self.reserved_count
        ):
            raise ValueError("reserved-source counts are invalid")

    @property
    def shortfall_count(self) -> int:
        return self.reserved_count - self.selected_in_reserved_step

    def as_dict(self) -> dict[str, object]:
        return {
            "reserved_count": self.reserved_count,
            "selected_in_reserved_step": self.selected_in_reserved_step,
            "shortfall_count": self.shortfall_count,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ReviewSelectionResultV1:
    deduplication: DeduplicationResultV1
    selected: tuple[LessonCandidateV1, ...] = field(repr=False)
    decisions: tuple[DiversitySelectionDecisionV1, ...]
    reserved_shortfalls: tuple[ReservedSourceShortfallV1, ...]
    event_five_gate_passed: bool
    target_count: int = REVIEW_TARGET_COUNT_V1

    def __post_init__(self) -> None:
        if not isinstance(self.deduplication, DeduplicationResultV1):
            raise TypeError("review selection requires a deduplication result")
        if type(self.target_count) is not int or self.target_count <= 0:
            raise ValueError("review target must be positive")
        if type(self.event_five_gate_passed) is not bool:
            raise TypeError("event-five gate status must be exact bool")
        if any(not isinstance(item, LessonCandidateV1) for item in self.selected):
            raise TypeError("review selection contains a non-candidate")
        if any(
            not isinstance(item, DiversitySelectionDecisionV1)
            for item in self.decisions
        ):
            raise TypeError("review selection decision is invalid")
        if any(
            not isinstance(item, ReservedSourceShortfallV1)
            for item in self.reserved_shortfalls
        ):
            raise TypeError("reserved-source shortfall is invalid")
        selected_ids = tuple(item.candidate_id for item in self.selected)
        decision_ids = tuple(item.candidate_id for item in self.decisions)
        retained_ids = {item.candidate_id for item in self.deduplication.retained}
        if selected_ids != decision_ids or len(set(selected_ids)) != len(selected_ids):
            raise ValueError("selected candidates and decisions disagree")
        if not set(selected_ids).issubset(retained_ids):
            raise ValueError("selection admitted a semantic duplicate")
        if len(self.selected) > self.target_count:
            raise ValueError("review selection exceeded its target")
        if tuple(item.source for item in self.reserved_shortfalls) != SOURCE_ORDER_V1:
            raise ValueError("reserved shortfalls do not cover sources in fixed order")
        event_selected = next(
            item.selected_in_reserved_step
            for item in self.reserved_shortfalls
            if item.source == "event"
        )
        if self.event_five_gate_passed != (event_selected == 5):
            raise ValueError("event-five gate does not reflect step-one selection")

    @property
    def selected_count(self) -> int:
        return len(self.selected)

    @property
    def shortfall_count(self) -> int:
        return self.target_count - self.selected_count

    @property
    def pool_exhausted(self) -> bool:
        return self.shortfall_count > 0

    @property
    def coverage_counts(self) -> tuple[tuple[str, int], ...]:
        projections = tuple(_dimension_mapping(item) for item in self.selected)
        return tuple(
            (dimension, len({item[dimension] for item in projections}))
            for dimension in DIVERSITY_DIMENSIONS_V1
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "coverage_counts": dict(self.coverage_counts),
            "deduplication": self.deduplication.as_dict(),
            "decisions": [item.as_dict() for item in self.decisions],
            "duplicates_admitted": False,
            "event_five_gate_passed": self.event_five_gate_passed,
            "pool_exhausted": self.pool_exhausted,
            "policy_id": "WO33_REVIEW_SELECTION_V1",
            "reserved_shortfalls": [
                item.as_dict() for item in self.reserved_shortfalls
            ],
            "schema_version": 1,
            "selected_candidate_ids": [item.candidate_id for item in self.selected],
            "selected_count": self.selected_count,
            "shortfall_count": self.shortfall_count,
            "target_count": self.target_count,
            "thresholds_weakened": False,
        }

    def technical_review_projection(self) -> dict[str, object]:
        """Return selection evidence without leaking any mined future outcome."""

        return {
            "decisions": [
                item.technical_review_projection() for item in self.decisions
            ],
            "duplicates_admitted": False,
            "event_five_gate_passed": self.event_five_gate_passed,
            "pool_exhausted": self.pool_exhausted,
            "policy_id": "WO33_REVIEW_SELECTION_V1",
            "reserved_shortfalls": [
                item.as_dict() for item in self.reserved_shortfalls
            ],
            "schema_version": 1,
            "selected_candidates": [
                item.review_projection(CandidatePresentationModeV1.TECHNICAL_REVIEW)
                for item in self.selected
            ],
            "selected_count": self.selected_count,
            "shortfall_count": self.shortfall_count,
            "target_count": self.target_count,
            "thresholds_weakened": False,
        }


def materially_distinct_event_candidate_v1(
    candidate: LessonCandidateV1,
    selected_event_candidates: Sequence[LessonCandidateV1],
) -> bool:
    """Apply the pairwise event-five material-distinctness predicate."""

    candidate_values = _dimension_mapping(candidate)
    if candidate_values["source"] != "event":
        return False
    for prior in selected_event_candidates:
        prior_values = _dimension_mapping(prior)
        first_group = (
            candidate_values["detector_family"]
            != prior_values["detector_family"]
            or candidate_values["primary_skill"]
            != prior_values["primary_skill"]
        )
        second_group = (
            candidate_values["phase"] != prior_values["phase"]
            or candidate_values["source_window_outcome"]
            != prior_values["source_window_outcome"]
            or candidate_values["difficulty_band"]
            != prior_values["difficulty_band"]
        )
        if not (first_group and second_group):
            return False
    return True


def _choose_highest_marginal(
    candidates: Sequence[LessonCandidateV1],
    selected: Sequence[LessonCandidateV1],
) -> tuple[LessonCandidateV1, int, tuple[tuple[str, int], ...]]:
    scored = []
    for candidate in candidates:
        score, novelty = marginal_diversity_v1(candidate, selected)
        scored.append((candidate, score, novelty))
    if not scored:
        raise ValueError("cannot select from an empty candidate pool")
    return min(
        scored,
        key=lambda item: (
            -item[1],
            *_tie_key(item[0]),
        ),
    )


def _append_selection(
    candidate: LessonCandidateV1,
    score: int,
    novelty: tuple[tuple[str, int], ...],
    stage: ReviewSelectionStageV1,
    selected: list[LessonCandidateV1],
    decisions: list[DiversitySelectionDecisionV1],
) -> None:
    values = candidate_dimension_values_v1(candidate)
    tie_digest = _tie_key(candidate)[0].hex()
    selected.append(candidate)
    decisions.append(
        DiversitySelectionDecisionV1(
            ordinal=len(selected),
            stage=stage,
            candidate_id=candidate.candidate_id,
            source=dict(values)["source"],
            marginal_score_ppm=score,
            novelty_ppm=novelty,
            dimension_values=values,
            tie_digest=tie_digest,
        )
    )


def _select_from_pool(
    remaining: list[LessonCandidateV1],
    selected: list[LessonCandidateV1],
    decisions: list[DiversitySelectionDecisionV1],
    *,
    count: int,
    stage: ReviewSelectionStageV1,
    predicate: Callable[[LessonCandidateV1], bool],
) -> int:
    selected_here = 0
    while selected_here < count:
        eligible = tuple(item for item in remaining if predicate(item))
        if not eligible:
            break
        candidate, score, novelty = _choose_highest_marginal(eligible, selected)
        remaining.remove(candidate)
        _append_selection(candidate, score, novelty, stage, selected, decisions)
        selected_here += 1
    return selected_here


def select_technical_review_candidates(
    candidates: Iterable[LessonCandidateV1],
    *,
    target_count: int = REVIEW_TARGET_COUNT_V1,
) -> ReviewSelectionResultV1:
    """Deduplicate, stratify, globally fill, and report every shortfall.

    The requested count influences only when selection stops.  It cannot alter
    semantic duplicate thresholds, candidate validity, reserved requirements, or the
    event-five gate.
    """

    if type(target_count) is not int or target_count <= 0:
        raise ValueError("review target must be a positive exact integer")
    deduplication = deduplicate_candidates(candidates)
    remaining = list(deduplication.retained)
    for candidate in remaining:
        candidate_dimension_values_v1(candidate)

    selected: list[LessonCandidateV1] = []
    decisions: list[DiversitySelectionDecisionV1] = []
    shortfalls: list[ReservedSourceShortfallV1] = []

    event_selected: list[LessonCandidateV1] = []
    event_goal = min(RESERVED_COUNTS_V1["event"], target_count)
    while len(event_selected) < event_goal:
        eligible = tuple(
            item
            for item in remaining
            if materially_distinct_event_candidate_v1(item, event_selected)
        )
        if not eligible:
            break
        candidate, score, novelty = _choose_highest_marginal(eligible, selected)
        remaining.remove(candidate)
        _append_selection(
            candidate,
            score,
            novelty,
            ReviewSelectionStageV1.EVENT_MATERIALLY_DISTINCT,
            selected,
            decisions,
        )
        event_selected.append(candidate)
    shortfalls.append(
        ReservedSourceShortfallV1(
            "event",
            RESERVED_COUNTS_V1["event"],
            len(event_selected),
        )
    )

    for source in SOURCE_ORDER_V1[1:]:
        available_slots = max(0, target_count - len(selected))
        source_goal = min(RESERVED_COUNTS_V1[source], available_slots)
        selected_here = _select_from_pool(
            remaining,
            selected,
            decisions,
            count=source_goal,
            stage=ReviewSelectionStageV1.SOURCE_RESERVED,
            predicate=lambda candidate, expected=source: (
                candidate.rarity_projection.qualification_source_row == expected
            ),
        )
        shortfalls.append(
            ReservedSourceShortfallV1(
                source,
                RESERVED_COUNTS_V1[source],
                selected_here,
            )
        )

    _select_from_pool(
        remaining,
        selected,
        decisions,
        count=max(0, target_count - len(selected)),
        stage=ReviewSelectionStageV1.GLOBAL_FILL,
        predicate=lambda _candidate: True,
    )
    return ReviewSelectionResultV1(
        deduplication=deduplication,
        selected=tuple(selected),
        decisions=tuple(decisions),
        reserved_shortfalls=tuple(shortfalls),
        event_five_gate_passed=(len(event_selected) == RESERVED_COUNTS_V1["event"]),
        target_count=target_count,
    )


def coverage_counts_v1(
    candidates: Sequence[LessonCandidateV1],
) -> Mapping[str, int]:
    """Expose unweighted unique-value coverage for deterministic comparisons."""

    projections = tuple(_dimension_mapping(item) for item in candidates)
    return MappingProxyType(
        {
            dimension: len({item[dimension] for item in projections})
            for dimension in DIVERSITY_DIMENSIONS_V1
        }
    )


__all__ = [
    "DETECTOR_FAMILY_BY_ID_V1",
    "DIFFICULTY_BANDS_V1",
    "DIVERSITY_DIMENSIONS_V1",
    "DIVERSITY_WEIGHTS_PPM_V1",
    "LESSON_MINING_POLICY_VERSION_V1",
    "RESERVED_COUNTS_V1",
    "REVIEW_SELECTION_ROOT_V1",
    "REVIEW_TARGET_COUNT_V1",
    "REVIEW_TIE_CONTEXT_V1",
    "SESSION_PHASES_V1",
    "SOURCE_ORDER_V1",
    "DiversitySelectionDecisionV1",
    "ReservedSourceShortfallV1",
    "ReviewSelectionResultV1",
    "ReviewSelectionStageV1",
    "candidate_dimension_values_v1",
    "coverage_counts_v1",
    "detector_family_v1",
    "difficulty_band_v1",
    "marginal_diversity_v1",
    "materially_distinct_event_candidate_v1",
    "select_technical_review_candidates",
    "source_window_outcome_v1",
]
