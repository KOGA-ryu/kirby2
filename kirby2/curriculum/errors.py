"""Closed WO34-A learner error vocabulary and skill mappings."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType

from .skills import require_stable_skill_v1


DEFAULT_SCORED_ERROR_CAP_PPM_V1 = 250_000
CRITICAL_ERROR_CAP_PPM_V1 = 0


class LearnerErrorTypeV1(str, Enum):
    ACTED_DURING_RED = "ACTED_DURING_RED"
    FAILED_TO_ACT_DURING_GREEN = "FAILED_TO_ACT_DURING_GREEN"
    CROSSED_UNNECESSARILY = "CROSSED_UNNECESSARILY"
    WAITED_PAST_USEFUL_LIQUIDITY = "WAITED_PAST_USEFUL_LIQUIDITY"
    CANCELLED_TOO_LATE = "CANCELLED_TOO_LATE"
    CANCELLED_TOO_EARLY = "CANCELLED_TOO_EARLY"
    MISREAD_REPLENISHMENT = "MISREAD_REPLENISHMENT"
    CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH = (
        "CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH"
    )
    IGNORED_SPREAD_EXPANSION = "IGNORED_SPREAD_EXPANSION"
    CHASED_AFTER_INVALIDATION = "CHASED_AFTER_INVALIDATION"
    WRONG_HOTKEY = "WRONG_HOTKEY"
    OVERSIZED_RELATIVE_TO_LIQUIDITY = "OVERSIZED_RELATIVE_TO_LIQUIDITY"
    FAILED_TO_COMPLETE_OBJECTIVE = "FAILED_TO_COMPLETE_OBJECTIVE"
    UNSCORABLE = "UNSCORABLE"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT_OBSERVABILITY = "INSUFFICIENT_OBSERVABILITY"


AMBIGUITY_ERROR_TYPES_V1 = frozenset(
    {
        LearnerErrorTypeV1.UNSCORABLE,
        LearnerErrorTypeV1.AMBIGUOUS,
        LearnerErrorTypeV1.INSUFFICIENT_OBSERVABILITY,
    }
)
REMEDIATION_ERROR_PRIORITY_V1 = (
    LearnerErrorTypeV1.ACTED_DURING_RED,
    LearnerErrorTypeV1.WRONG_HOTKEY,
    LearnerErrorTypeV1.OVERSIZED_RELATIVE_TO_LIQUIDITY,
    LearnerErrorTypeV1.CHASED_AFTER_INVALIDATION,
    LearnerErrorTypeV1.CANCELLED_TOO_LATE,
    LearnerErrorTypeV1.CROSSED_UNNECESSARILY,
    LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
    LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN,
    LearnerErrorTypeV1.WAITED_PAST_USEFUL_LIQUIDITY,
    LearnerErrorTypeV1.CANCELLED_TOO_EARLY,
    LearnerErrorTypeV1.IGNORED_SPREAD_EXPANSION,
    LearnerErrorTypeV1.CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH,
    LearnerErrorTypeV1.MISREAD_REPLENISHMENT,
)
_STATIC_ERROR_SKILLS = MappingProxyType(
    {
        LearnerErrorTypeV1.ACTED_DURING_RED: "SCRIPT_DISCIPLINE",
        LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN: "SCRIPT_DISCIPLINE",
        LearnerErrorTypeV1.CROSSED_UNNECESSARILY: "SPREAD_DECISION",
        LearnerErrorTypeV1.WAITED_PAST_USEFUL_LIQUIDITY: "AGGRESSIVE_ENTRY",
        LearnerErrorTypeV1.CANCELLED_TOO_LATE: "CANCEL_TIMING",
        LearnerErrorTypeV1.CANCELLED_TOO_EARLY: "CANCEL_TIMING",
        LearnerErrorTypeV1.MISREAD_REPLENISHMENT: "ABSORPTION_RECOGNITION",
        LearnerErrorTypeV1.CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH: (
            "HIDDEN_LIQUIDITY"
        ),
        LearnerErrorTypeV1.IGNORED_SPREAD_EXPANSION: "SPREAD_DECISION",
        LearnerErrorTypeV1.CHASED_AFTER_INVALIDATION: "SCRIPT_DISCIPLINE",
        LearnerErrorTypeV1.WRONG_HOTKEY: "HOTKEY_ACCURACY",
        LearnerErrorTypeV1.OVERSIZED_RELATIVE_TO_LIQUIDITY: (
            "POSITION_MANAGEMENT"
        ),
    }
)
CRITICAL_ERROR_TYPES_V1 = frozenset(
    {
        LearnerErrorTypeV1.ACTED_DURING_RED,
        LearnerErrorTypeV1.WRONG_HOTKEY,
        LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
    }
)


def mapped_skill_for_error_v1(
    error_type: LearnerErrorTypeV1,
    primary_skill_id: str,
) -> str | None:
    if not isinstance(error_type, LearnerErrorTypeV1):
        raise TypeError("learner error type is invalid")
    require_stable_skill_v1(primary_skill_id)
    if error_type in AMBIGUITY_ERROR_TYPES_V1:
        return None
    if error_type is LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE:
        return primary_skill_id
    mapped = _STATIC_ERROR_SKILLS.get(error_type)
    if mapped is None:
        raise ValueError("scored learner error lacks a skill mapping")
    require_stable_skill_v1(mapped)
    return mapped


def score_cap_for_error_v1(
    error_type: LearnerErrorTypeV1,
    primary_skill_id: str,
) -> int | None:
    mapped = mapped_skill_for_error_v1(error_type, primary_skill_id)
    if mapped is None:
        return None
    if error_type in CRITICAL_ERROR_TYPES_V1:
        return CRITICAL_ERROR_CAP_PPM_V1
    return DEFAULT_SCORED_ERROR_CAP_PPM_V1


ERROR_SKILL_MAPPING_V1 = MappingProxyType(
    {
        error_type: mapped_skill_for_error_v1(error_type, "BOOK_READING")
        for error_type in LearnerErrorTypeV1
        if error_type
        not in {
            *AMBIGUITY_ERROR_TYPES_V1,
            LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
        }
    }
)


__all__ = [
    "AMBIGUITY_ERROR_TYPES_V1",
    "CRITICAL_ERROR_CAP_PPM_V1",
    "CRITICAL_ERROR_TYPES_V1",
    "DEFAULT_SCORED_ERROR_CAP_PPM_V1",
    "ERROR_SKILL_MAPPING_V1",
    "REMEDIATION_ERROR_PRIORITY_V1",
    "LearnerErrorTypeV1",
    "mapped_skill_for_error_v1",
    "score_cap_for_error_v1",
]
