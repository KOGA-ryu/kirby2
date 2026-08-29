"""Canonical semantic candidate deduplication for WO33-C."""

from __future__ import annotations

import unicodedata
from collections.abc import Hashable, Iterable, Set
from dataclasses import dataclass, field
from enum import Enum

from .models import (
    LessonCandidateV1,
    POLICY_SCALE_V1,
    RegimeSignatureV1,
    canonical_json_bytes,
    unsigned_share_ppm,
)
from .ranking import difficulty_order_key


TIME_IOU_MIN_PPM_V1 = 800_000
FEATURE_JACCARD_MIN_PPM_V1 = 900_000
EVENT_FIVE_GRAM_JACCARD_MIN_PPM_V1 = 850_000
OBJECTIVE_JACCARD_MIN_PPM_V1 = 500_000
EVENT_SIDES_V1 = frozenset({"BUY", "SELL", "NONE"})
EVENT_PRICE_RELATIONS_V1 = frozenset(
    {
        "BELOW_BID",
        "AT_BID",
        "INSIDE",
        "AT_ASK",
        "ABOVE_ASK",
        "NO_PRICE",
        "NO_REFERENCE_QUOTE",
    }
)


class DeduplicationStatusV1(str, Enum):
    RETAINED = "RETAINED"
    DUPLICATE = "DUPLICATE"


def _canonical_token_text(value: str, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or "|" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{label} must be nonempty NFC text without a separator")
    return value


def canonical_feature_value_v1(value: int | bool | str) -> tuple[str, str]:
    """Return the exact type tag and text used in observable feature tokens."""

    if type(value) is bool:
        return "FLAG", "true" if value else "false"
    if type(value) is int:
        return "INTEGER", str(value)
    if type(value) is str:
        return "STRING", _canonical_token_text(value, "feature string")
    raise TypeError("observable feature values are limited to int, bool, and NFC str")


def observable_feature_token_v1(
    event_type: str,
    field_path: str,
    value: int | bool | str,
) -> str:
    """Construct one canonical observable activation-feature token."""

    event = _canonical_token_text(event_type, "feature event type")
    path = _canonical_token_text(field_path, "observable field path")
    type_tag, canonical_value = canonical_feature_value_v1(value)
    return f"{event}|{path}|{type_tag}|{canonical_value}"


def observable_feature_tokens_v1(feature_tokens: Iterable[str]) -> tuple[str, ...]:
    """Validate, deduplicate, and NFC-byte sort observable feature tokens."""

    tokens = tuple(feature_tokens)
    if not tokens:
        raise ValueError("observable feature inventory must not be empty")
    for token in tokens:
        parts = token.split("|") if type(token) is str else ()
        if (
            len(parts) != 4
            or any(not part for part in parts)
            or unicodedata.normalize("NFC", token) != token
            or parts[2] not in {"FLAG", "INTEGER", "STRING"}
        ):
            raise ValueError("observable feature inventory contains a bad token")
        type_tag, value = parts[2], parts[3]
        if type_tag == "FLAG" and value not in {"true", "false"}:
            raise ValueError("observable feature flag is not canonical")
        if type_tag == "INTEGER":
            try:
                canonical_integer = str(int(value))
            except ValueError as error:
                raise ValueError("observable feature integer is not canonical") from error
            if canonical_integer != value:
                raise ValueError("observable feature integer is not canonical")
    return tuple(sorted(set(tokens), key=lambda item: item.encode("utf-8")))


def event_price_relation_v1(
    price_ticks: int | None,
    pre_event_best_bid_ticks: int | None,
    pre_event_best_ask_ticks: int | None,
) -> str:
    """Classify event price against the complete pre-event best quotes."""

    values = (
        (price_ticks, "event price"),
        (pre_event_best_bid_ticks, "pre-event best bid"),
        (pre_event_best_ask_ticks, "pre-event best ask"),
    )
    for value, label in values:
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{label} must be positive ticks or null")
    if price_ticks is None:
        return "NO_PRICE"
    if pre_event_best_bid_ticks is None or pre_event_best_ask_ticks is None:
        return "NO_REFERENCE_QUOTE"
    if pre_event_best_bid_ticks >= pre_event_best_ask_ticks:
        raise ValueError("pre-event reference quote must be positive and uncrossed")
    if price_ticks < pre_event_best_bid_ticks:
        return "BELOW_BID"
    if price_ticks == pre_event_best_bid_ticks:
        return "AT_BID"
    if price_ticks < pre_event_best_ask_ticks:
        return "INSIDE"
    if price_ticks == pre_event_best_ask_ticks:
        return "AT_ASK"
    return "ABOVE_ASK"


def canonical_event_token_v1(
    event_type: str,
    side: str,
    price_ticks: int | None,
    pre_event_best_bid_ticks: int | None,
    pre_event_best_ask_ticks: int | None,
) -> str:
    event = _canonical_token_text(event_type, "event token type")
    if side not in EVENT_SIDES_V1:
        raise ValueError("event token side is outside BUY, SELL, NONE")
    relation = event_price_relation_v1(
        price_ticks,
        pre_event_best_bid_ticks,
        pre_event_best_ask_ticks,
    )
    return f"{event}|{side}|{relation}"


def event_five_grams_v1(event_tokens: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Build unique canonical consecutive five-grams, or one shorter tuple."""

    tokens = tuple(event_tokens)
    if not tokens:
        raise ValueError("event sequence must contain at least one canonical token")
    for token in tokens:
        parts = token.split("|") if type(token) is str else ()
        if (
            len(parts) != 3
            or not parts[0]
            or parts[1] not in EVENT_SIDES_V1
            or parts[2] not in EVENT_PRICE_RELATIONS_V1
            or unicodedata.normalize("NFC", token) != token
        ):
            raise ValueError("event sequence contains a noncanonical token")
    grams = (
        (tokens,)
        if len(tokens) < 5
        else tuple(tokens[index : index + 5] for index in range(len(tokens) - 4))
    )
    unique = {item for item in grams}
    return tuple(sorted(unique, key=lambda item: canonical_json_bytes(list(item))))


def pressure_band_v1(value_ppm: int | None) -> str:
    if value_ppm is None:
        return "NOT_APPLICABLE"
    if type(value_ppm) is not int or value_ppm < 0:
        raise ValueError("volume/liquidity pressure must be nonnegative PPM or null")
    if value_ppm < 750_000:
        return "LOW"
    if value_ppm <= 1_250_000:
        return "NORMAL"
    return "HIGH"


def spread_band_v1(spread_ticks: int | None) -> str:
    if spread_ticks is None:
        return "NOT_APPLICABLE"
    if type(spread_ticks) is not int or spread_ticks <= 0:
        raise ValueError("spread band requires positive integer ticks or null")
    if spread_ticks == 1:
        return "ONE"
    if spread_ticks == 2:
        return "TWO"
    if spread_ticks <= 4:
        return "MODERATE"
    if spread_ticks <= 8:
        return "WIDE"
    return "EXTREME"


def build_regime_signature_v1(
    *,
    phase: str,
    regime_id: str | None,
    volume_pressure_ppm: int | None,
    liquidity_pressure_ppm: int | None,
    spread_ticks: int | None,
) -> RegimeSignatureV1:
    """Build the exact five-field day/local regime signature."""

    return RegimeSignatureV1(
        phase=phase,
        regime_id="NOT_APPLICABLE" if regime_id is None else regime_id,
        volume_band=pressure_band_v1(volume_pressure_ppm),
        liquidity_band=pressure_band_v1(liquidity_pressure_ppm),
        spread_band=spread_band_v1(spread_ticks),
    )


def jaccard_ppm(left: Set[Hashable], right: Set[Hashable]) -> int:
    """Return exact fixed-point Jaccard with the preregistered empty rules."""

    if not isinstance(left, Set) or not isinstance(right, Set):
        raise TypeError("Jaccard operands must be set-like")
    left_values = set(left)
    right_values = set(right)
    if not left_values and not right_values:
        return POLICY_SCALE_V1
    if not left_values or not right_values:
        return 0
    return unsigned_share_ppm(
        len(left_values.intersection(right_values)),
        len(left_values.union(right_values)),
    )


def time_iou_ppm(
    left_start_us: int,
    left_end_us: int,
    right_start_us: int,
    right_end_us: int,
) -> int:
    """Compare two positive half-open decision windows."""

    values = (left_start_us, left_end_us, right_start_us, right_end_us)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("time-IoU bounds must be nonnegative exact integers")
    if left_end_us <= left_start_us or right_end_us <= right_start_us:
        raise ValueError("time-IoU intervals must have positive duration")
    intersection = max(
        0,
        min(left_end_us, right_end_us) - max(left_start_us, right_start_us),
    )
    union = (
        left_end_us
        - left_start_us
        + right_end_us
        - right_start_us
        - intersection
    )
    if union <= 0:
        raise ValueError("time-IoU union must have positive duration")
    return unsigned_share_ppm(intersection, union)


@dataclass(frozen=True, slots=True)
class SemanticDuplicateComparisonV1:
    ancestry_matches: bool
    time_iou_ppm: int
    feature_jaccard_ppm: int
    regime_matches: bool
    event_five_gram_jaccard_ppm: int
    objective_jaccard_ppm: int

    def __post_init__(self) -> None:
        if type(self.ancestry_matches) is not bool or type(self.regime_matches) is not bool:
            raise TypeError("semantic equality values must be exact bools")
        for label, value in (
            ("time IoU", self.time_iou_ppm),
            ("feature Jaccard", self.feature_jaccard_ppm),
            ("event-five-gram Jaccard", self.event_five_gram_jaccard_ppm),
            ("objective Jaccard", self.objective_jaccard_ppm),
        ):
            if type(value) is not int or not 0 <= value <= POLICY_SCALE_V1:
                raise ValueError(f"{label} must be an exact fixed-point share")

    @property
    def is_duplicate(self) -> bool:
        return (
            self.ancestry_matches
            and self.time_iou_ppm >= TIME_IOU_MIN_PPM_V1
            and self.feature_jaccard_ppm >= FEATURE_JACCARD_MIN_PPM_V1
            and self.regime_matches
            and self.event_five_gram_jaccard_ppm
            >= EVENT_FIVE_GRAM_JACCARD_MIN_PPM_V1
            and self.objective_jaccard_ppm >= OBJECTIVE_JACCARD_MIN_PPM_V1
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "ancestry_matches": self.ancestry_matches,
            "event_five_gram_jaccard_ppm": self.event_five_gram_jaccard_ppm,
            "feature_jaccard_ppm": self.feature_jaccard_ppm,
            "is_duplicate": self.is_duplicate,
            "objective_jaccard_ppm": self.objective_jaccard_ppm,
            "regime_matches": self.regime_matches,
            "schema_version": 1,
            "time_iou_ppm": self.time_iou_ppm,
        }


def _objective_set(candidate: LessonCandidateV1) -> set[str]:
    return {candidate.primary_skill_id, *candidate.supporting_skill_ids}


def compare_candidates(
    left: LessonCandidateV1,
    right: LessonCandidateV1,
) -> SemanticDuplicateComparisonV1:
    """Inspect every preregistered semantic duplicate predicate."""

    if not isinstance(left, LessonCandidateV1) or not isinstance(
        right,
        LessonCandidateV1,
    ):
        raise TypeError("semantic comparison requires lesson candidates")
    left_features = left.observable_feature_summary
    right_features = right.observable_feature_summary
    return SemanticDuplicateComparisonV1(
        ancestry_matches=(
            left.source_ancestry.sha256 == right.source_ancestry.sha256
        ),
        time_iou_ppm=time_iou_ppm(
            left.bounds.active_start_us,
            left.bounds.post_end_us,
            right.bounds.active_start_us,
            right.bounds.post_end_us,
        ),
        feature_jaccard_ppm=jaccard_ppm(
            set(left_features.feature_tokens),
            set(right_features.feature_tokens),
        ),
        regime_matches=(
            left_features.regime_signature == right_features.regime_signature
        ),
        event_five_gram_jaccard_ppm=jaccard_ppm(
            set(left_features.event_five_grams),
            set(right_features.event_five_grams),
        ),
        objective_jaccard_ppm=jaccard_ppm(
            _objective_set(left),
            _objective_set(right),
        ),
    )


@dataclass(frozen=True, slots=True)
class DeduplicationDecisionV1:
    candidate_id: str
    status: DeduplicationStatusV1
    duplicate_of: str | None
    comparison: SemanticDuplicateComparisonV1 | None

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or not self.candidate_id:
            raise ValueError("deduplication candidate ID must be nonempty")
        if not isinstance(self.status, DeduplicationStatusV1):
            raise TypeError("deduplication status is invalid")
        if self.status is DeduplicationStatusV1.RETAINED:
            if self.duplicate_of is not None or self.comparison is not None:
                raise ValueError("retained candidate cannot carry duplicate evidence")
        elif (
            type(self.duplicate_of) is not str
            or not self.duplicate_of
            or not isinstance(self.comparison, SemanticDuplicateComparisonV1)
            or not self.comparison.is_duplicate
        ):
            raise ValueError("duplicate decision lacks a qualifying retained match")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "comparison": (
                None if self.comparison is None else self.comparison.as_dict()
            ),
            "duplicate_of": self.duplicate_of,
            "schema_version": 1,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class DeduplicationResultV1:
    retained: tuple[LessonCandidateV1, ...] = field(repr=False)
    decisions: tuple[DeduplicationDecisionV1, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(item, LessonCandidateV1) for item in self.retained):
            raise TypeError("deduplication retained pool contains a non-candidate")
        if any(
            not isinstance(item, DeduplicationDecisionV1)
            for item in self.decisions
        ):
            raise TypeError("deduplication decision inventory is invalid")
        retained_ids = tuple(item.candidate_id for item in self.retained)
        if len(set(retained_ids)) != len(retained_ids):
            raise ValueError("deduplication retained identity is not unique")
        decision_retained = tuple(
            item.candidate_id
            for item in self.decisions
            if item.status is DeduplicationStatusV1.RETAINED
        )
        if decision_retained != retained_ids:
            raise ValueError("deduplication decisions and retained pool disagree")

    @property
    def duplicate_count(self) -> int:
        return sum(
            item.status is DeduplicationStatusV1.DUPLICATE
            for item in self.decisions
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "decisions": [item.as_dict() for item in self.decisions],
            "duplicate_count": self.duplicate_count,
            "policy_id": "LESSON_SEMANTIC_DEDUPLICATION_V1",
            "retained_candidate_ids": [item.candidate_id for item in self.retained],
            "schema_version": 1,
            "thresholds_ppm": {
                "event_five_gram_jaccard_min_ppm": (
                    EVENT_FIVE_GRAM_JACCARD_MIN_PPM_V1
                ),
                "feature_jaccard_min_ppm": FEATURE_JACCARD_MIN_PPM_V1,
                "objective_jaccard_min_ppm": OBJECTIVE_JACCARD_MIN_PPM_V1,
                "time_iou_min_ppm": TIME_IOU_MIN_PPM_V1,
            },
        }


def deduplicate_candidates(
    candidates: Iterable[LessonCandidateV1],
) -> DeduplicationResultV1:
    """Collapse semantic duplicates in one deterministic ordered greedy pass."""

    pool = tuple(candidates)
    if any(not isinstance(item, LessonCandidateV1) for item in pool):
        raise TypeError("deduplication pool contains a non-candidate")
    candidate_ids = tuple(item.candidate_id for item in pool)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("deduplication pool contains repeated candidate identity")

    ordered = sorted(pool, key=difficulty_order_key)
    retained: list[LessonCandidateV1] = []
    decisions: list[DeduplicationDecisionV1] = []
    for candidate in ordered:
        duplicate_of: LessonCandidateV1 | None = None
        duplicate_comparison: SemanticDuplicateComparisonV1 | None = None
        for prior in retained:
            comparison = compare_candidates(candidate, prior)
            if comparison.is_duplicate:
                duplicate_of = prior
                duplicate_comparison = comparison
                break
        if duplicate_of is None:
            retained.append(candidate)
            decisions.append(
                DeduplicationDecisionV1(
                    candidate_id=candidate.candidate_id,
                    status=DeduplicationStatusV1.RETAINED,
                    duplicate_of=None,
                    comparison=None,
                )
            )
        else:
            decisions.append(
                DeduplicationDecisionV1(
                    candidate_id=candidate.candidate_id,
                    status=DeduplicationStatusV1.DUPLICATE,
                    duplicate_of=duplicate_of.candidate_id,
                    comparison=duplicate_comparison,
                )
            )
    return DeduplicationResultV1(tuple(retained), tuple(decisions))


__all__ = [
    "EVENT_FIVE_GRAM_JACCARD_MIN_PPM_V1",
    "EVENT_PRICE_RELATIONS_V1",
    "EVENT_SIDES_V1",
    "FEATURE_JACCARD_MIN_PPM_V1",
    "OBJECTIVE_JACCARD_MIN_PPM_V1",
    "TIME_IOU_MIN_PPM_V1",
    "DeduplicationDecisionV1",
    "DeduplicationResultV1",
    "DeduplicationStatusV1",
    "SemanticDuplicateComparisonV1",
    "compare_candidates",
    "build_regime_signature_v1",
    "canonical_event_token_v1",
    "canonical_feature_value_v1",
    "deduplicate_candidates",
    "event_five_grams_v1",
    "event_price_relation_v1",
    "jaccard_ppm",
    "observable_feature_token_v1",
    "observable_feature_tokens_v1",
    "pressure_band_v1",
    "spread_band_v1",
    "time_iou_ppm",
]
