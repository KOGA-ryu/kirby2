"""Exact objective arithmetic for deterministic strategy discovery.

This module deliberately contains no simulator integration.  It turns already
frozen, compatible evidence into integer utilities and qualification decisions;
WO35-D's development oracle is the only producer used by this work order.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .diffs import StrategyComplexityV1


POLICY_SCALE_V1 = 1_000_000
STRATEGY_OBJECTIVE_PROTOCOL_ID_V1 = "BOUNDED_SEARCH_OBJECTIVES_V1"
STRATEGY_OBJECTIVE_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_OBJECTIVE_PROTOCOL_V1"
STRATEGY_DISCOVERY_POLICY_VERSION_V1 = "STRATEGY_DISCOVERY_V1"
STRATEGY_DISCOVERY_TIE_ROOT_V1 = 3_599_001
UNCERTAINTY_METHOD_V1 = "MEDIAN_DELTA_MINUS_MAD_V1"
MULTIPLICITY_METHOD_V1 = "5000_TIMES_CEIL_LOG2_ONE_PLUS_N_V1"
ROOT_REDUCTION_ORDER_V1 = "ASCENDING_ROOT_SEED_V1"
MATERIAL_EQUIVALENCE_THRESHOLD_V1 = 30_000
MULTIPLICITY_PENALTY_STEP_V1 = 5_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrategyObjectiveIdV1(str, Enum):
    BALANCED_CLASSIFICATION = "BALANCED_CLASSIFICATION"
    DISCIPLINE_COMPATIBILITY = "DISCIPLINE_COMPATIBILITY"
    EXECUTION_OPPORTUNITY = "EXECUTION_OPPORTUNITY"
    FALSE_GREEN = "FALSE_GREEN"
    MISSED_OPPORTUNITY = "MISSED_OPPORTUNITY"
    ADVERSE_SELECTION = "ADVERSE_SELECTION"
    TURNOVER = "TURNOVER"
    SPREAD_PAID = "SPREAD_PAID"
    COMPLETION = "COMPLETION"
    CROSS_CELL_STABILITY = "CROSS_CELL_STABILITY"
    COMPLEXITY = "COMPLEXITY"
    PNL = "PNL"


class ObjectiveApplicabilityV1(str, Enum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class ObjectiveSpecV1:
    objective_id: StrategyObjectiveIdV1
    weight: int
    required: bool
    utility_rule: str

    def __post_init__(self) -> None:
        if not isinstance(self.objective_id, StrategyObjectiveIdV1):
            raise TypeError("objective ID must be typed")
        if type(self.weight) is not int or not 0 <= self.weight <= POLICY_SCALE_V1:
            raise ValueError("objective weight must be an integer in 0..S")
        if type(self.required) is not bool:
            raise TypeError("objective required flag must be Boolean")
        if type(self.utility_rule) is not str or not self.utility_rule:
            raise ValueError("objective utility rule must be nonempty")
        if self.objective_id is StrategyObjectiveIdV1.PNL:
            if self.required or self.weight != 0:
                raise ValueError("P&L must remain optional and zero-weight")
        elif not self.required or self.weight <= 0:
            raise ValueError("every non-P&L objective must be required and weighted")

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id.value,
            "required": self.required,
            "utility_rule": self.utility_rule,
            "weight": self.weight,
        }


REQUIRED_OBJECTIVE_SPECS_V1 = (
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.BALANCED_CLASSIFICATION,
        180_000,
        True,
        "ROUNDED_THREE_CLASS_RECALL_MEAN_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.DISCIPLINE_COMPATIBILITY,
        120_000,
        True,
        "S_MINUS_VIOLATION_SHARE_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY,
        130_000,
        True,
        "HARMONIC_PRECISION_RECALL_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.FALSE_GREEN,
        90_000,
        True,
        "S_MINUS_FALSE_GREEN_RATE_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
        90_000,
        True,
        "S_MINUS_MISSED_OPPORTUNITY_RATE_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.ADVERSE_SELECTION,
        80_000,
        True,
        "CAPPED_5000_MILLITICKS_COST_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.TURNOVER,
        40_000,
        True,
        "CAPPED_EXCESS_18_ROUND_TRIPS_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.SPREAD_PAID,
        40_000,
        True,
        "CAPPED_5000_MILLITICKS_COST_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.COMPLETION,
        100_000,
        True,
        "COMPLETED_OVER_OBJECTIVE_SHARES_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.CROSS_CELL_STABILITY,
        80_000,
        True,
        "S_MINUS_CELL_MEDIAN_RANGE_V1",
    ),
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.COMPLEXITY,
        50_000,
        True,
        "S_MINUS_CAPPED_COMPLEXITY_POINTS_OVER_200_V1",
    ),
)
OPTIONAL_OBJECTIVE_SPECS_V1 = (
    ObjectiveSpecV1(
        StrategyObjectiveIdV1.PNL,
        0,
        False,
        "RECORDED_ZERO_WEIGHT_V1",
    ),
)
ALL_OBJECTIVE_SPECS_V1 = REQUIRED_OBJECTIVE_SPECS_V1 + OPTIONAL_OBJECTIVE_SPECS_V1


@dataclass(frozen=True, slots=True)
class EvidenceCompatibilityKeyV1:
    """The three compatibility axes required before candidate comparison."""

    scenario_group_id: str
    objective_group_id: str
    evidence_group_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.scenario_group_id, "scenario group"),
            (self.objective_group_id, "objective group"),
            (self.evidence_group_id, "evidence group"),
        ):
            _require_nfc_text(value, label)

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_group_id": self.evidence_group_id,
            "objective_group_id": self.objective_group_id,
            "scenario_group_id": self.scenario_group_id,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveValueV1:
    objective_id: StrategyObjectiveIdV1
    applicability: ObjectiveApplicabilityV1
    utility_ppm: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.objective_id, StrategyObjectiveIdV1):
            raise TypeError("objective value ID must be typed")
        if not isinstance(self.applicability, ObjectiveApplicabilityV1):
            raise TypeError("objective applicability must be typed")
        if self.applicability is ObjectiveApplicabilityV1.APPLICABLE:
            if (
                type(self.utility_ppm) is not int
                or not 0 <= self.utility_ppm <= POLICY_SCALE_V1
            ):
                raise ValueError("applicable objective utility must be in 0..S")
        elif self.utility_ppm is not None:
            raise ValueError("missing objective evidence cannot carry a utility")

    def as_dict(self) -> dict[str, object]:
        return {
            "applicability": self.applicability.value,
            "objective_id": self.objective_id.value,
            "utility_ppm": self.utility_ppm,
        }


@dataclass(frozen=True, slots=True)
class PartitionStatisticV1:
    median_delta: int
    mad: int
    multiplicity_penalty: int

    def __post_init__(self) -> None:
        if type(self.median_delta) is not int:
            raise TypeError("partition median delta must be an integer")
        if type(self.mad) is not int or self.mad < 0:
            raise ValueError("partition MAD must be nonnegative")
        if type(self.multiplicity_penalty) is not int or self.multiplicity_penalty < 0:
            raise ValueError("partition multiplicity penalty must be nonnegative")

    @property
    def training_merit(self) -> int:
        return self.median_delta - self.mad

    @property
    def statistic(self) -> int:
        return self.training_merit - self.multiplicity_penalty

    def as_dict(self) -> dict[str, object]:
        return {
            "mad": self.mad,
            "median_delta": self.median_delta,
            "multiplicity_penalty": self.multiplicity_penalty,
            "statistic": self.statistic,
            "training_merit": self.training_merit,
        }


def round_div_even(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise TypeError("round_div_even requires integer operands")
    if denominator <= 0:
        raise ValueError("round_div_even denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return -quotient if numerator < 0 else quotient


def clamp(value: int, lower: int, upper: int) -> int:
    if not all(type(item) is int for item in (value, lower, upper)):
        raise TypeError("clamp requires integer operands")
    if lower > upper:
        raise ValueError("clamp lower bound exceeds upper bound")
    return min(upper, max(lower, value))


def ratio_ppm(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise ValueError("ratio denominator is missing")
    return round_div_even(numerator * POLICY_SCALE_V1, denominator)


def unsigned_share_ppm(numerator: int, denominator: int) -> int:
    return clamp(ratio_ppm(numerator, denominator), 0, POLICY_SCALE_V1)


def nearest_rank_p50(values: tuple[int, ...]) -> int:
    if type(values) is not tuple or not values:
        raise ValueError("P50 requires a nonempty integer tuple")
    if any(type(item) is not int for item in values):
        raise TypeError("P50 values must be integers")
    ordered = tuple(sorted(values))
    one_based_rank = max(1, (len(ordered) + 1) // 2)
    return ordered[one_based_rank - 1]


def median_and_mad(values: tuple[int, ...]) -> tuple[int, int]:
    median = nearest_rank_p50(values)
    return median, nearest_rank_p50(tuple(abs(item - median) for item in values))


def ceil_log2_one_plus(count: int) -> int:
    if type(count) is not int or count < 0:
        raise ValueError("candidate count must be nonnegative")
    target = 1 + count
    exponent = 0
    power = 1
    while power < target:
        power *= 2
        exponent += 1
    return exponent


def multiplicity_penalty(candidate_count: int) -> int:
    return MULTIPLICITY_PENALTY_STEP_V1 * ceil_log2_one_plus(candidate_count)


def partition_statistic(
    root_deltas: tuple[int, ...],
    *,
    trained_candidate_count: int,
    apply_multiplicity: bool = True,
) -> PartitionStatisticV1:
    median, mad = median_and_mad(root_deltas)
    penalty = multiplicity_penalty(trained_candidate_count) if apply_multiplicity else 0
    return PartitionStatisticV1(median, mad, penalty)


def complexity_points(complexity: StrategyComplexityV1) -> int:
    if not isinstance(complexity, StrategyComplexityV1):
        raise TypeError("complexity points require the six-dimensional schema")
    return (
        4 * complexity.conditions
        + 3 * complexity.features
        + 8 * complexity.states
        + 6 * complexity.transitions
        + 2 * complexity.rolling_windows
        + complexity.parameters
    )


def complexity_utility(complexity: StrategyComplexityV1) -> int:
    cost = clamp(
        round_div_even(complexity_points(complexity) * POLICY_SCALE_V1, 200),
        0,
        POLICY_SCALE_V1,
    )
    return POLICY_SCALE_V1 - cost


def balanced_classification_utility(
    *,
    correct_green: int,
    reference_green: int,
    correct_wait: int,
    reference_wait: int,
    correct_red: int,
    reference_red: int,
) -> int:
    recalls = (
        _count_share(correct_green, reference_green, "GREEN recall"),
        _count_share(correct_wait, reference_wait, "WAIT recall"),
        _count_share(correct_red, reference_red, "RED recall"),
    )
    return round_div_even(sum(recalls), 3)


def discipline_compatibility_utility(*, violations: int, eligible: int) -> int:
    return POLICY_SCALE_V1 - _count_share(violations, eligible, "discipline")


def execution_opportunity_utility(
    *,
    true_positive: int,
    predicted_green_allow: int,
    true_opportunities: int,
) -> int:
    precision = _count_share(
        true_positive,
        predicted_green_allow,
        "opportunity precision",
    )
    recall = _count_share(true_positive, true_opportunities, "opportunity recall")
    if precision == 0 and recall == 0:
        return 0
    return round_div_even(2 * precision * recall, precision + recall)


def false_green_utility(*, false_green: int, non_green: int) -> int:
    return POLICY_SCALE_V1 - _count_share(false_green, non_green, "false-green")


def missed_opportunity_utility(*, missed: int, true_opportunities: int) -> int:
    return POLICY_SCALE_V1 - _count_share(
        missed,
        true_opportunities,
        "missed opportunity",
    )


def signed_cost_utility(cost_milliticks_per_share: int) -> int:
    if type(cost_milliticks_per_share) is not int:
        raise TypeError("signed cost must be integer milliticks per share")
    normalized = clamp(
        round_div_even(
            max(0, cost_milliticks_per_share) * POLICY_SCALE_V1,
            5_000,
        ),
        0,
        POLICY_SCALE_V1,
    )
    return POLICY_SCALE_V1 - normalized


def turnover_utility(*, traded_shares: int, objective_shares: int) -> int:
    _require_nonnegative_int(traded_shares, "traded shares")
    if type(objective_shares) is not int or objective_shares <= 0:
        raise ValueError("objective shares must be positive")
    excess = max(0, traded_shares - 2 * objective_shares)
    normalized = clamp(
        round_div_even(
            excess * POLICY_SCALE_V1,
            18 * objective_shares,
        ),
        0,
        POLICY_SCALE_V1,
    )
    return POLICY_SCALE_V1 - normalized


def completion_utility(*, completed_shares: int, objective_shares: int) -> int:
    _require_nonnegative_int(completed_shares, "completed shares")
    if type(objective_shares) is not int or objective_shares <= 0:
        raise ValueError("objective shares must be positive")
    return clamp(
        round_div_even(completed_shares * POLICY_SCALE_V1, objective_shares),
        0,
        POLICY_SCALE_V1,
    )


def cross_cell_stability_utility(cell_medians: tuple[int, ...]) -> int:
    if type(cell_medians) is not tuple or len(cell_medians) < 2:
        raise ValueError("cross-cell stability requires at least two cells")
    if any(
        type(item) is not int or not 0 <= item <= POLICY_SCALE_V1
        for item in cell_medians
    ):
        raise ValueError("cell medians must be integer utilities in 0..S")
    return POLICY_SCALE_V1 - (max(cell_medians) - min(cell_medians))


def root_composite(values: tuple[ObjectiveValueV1, ...]) -> int:
    """Reduce applicable utilities in the fixed objective inventory order."""

    if type(values) is not tuple or any(
        not isinstance(item, ObjectiveValueV1) for item in values
    ):
        raise TypeError("root composite requires typed objective values")
    by_id = {item.objective_id: item for item in values}
    if len(by_id) != len(values):
        raise ValueError("objective values must be unique")
    numerator = 0
    denominator = 0
    for spec in ALL_OBJECTIVE_SPECS_V1:
        value = by_id.get(spec.objective_id)
        if spec.required and value is None:
            raise ValueError(f"required objective {spec.objective_id.value} is absent")
        if value is None or value.applicability is ObjectiveApplicabilityV1.NOT_APPLICABLE:
            continue
        if value.applicability is ObjectiveApplicabilityV1.INSUFFICIENT_EVIDENCE:
            raise ValueError(
                f"objective {spec.objective_id.value} has insufficient evidence"
            )
        assert value.utility_ppm is not None
        numerator += spec.weight * value.utility_ppm
        denominator += spec.weight
    if denominator <= 0:
        raise ValueError("P&L cannot be the sole applicable objective")
    return round_div_even(numerator, denominator)


def common_tie_digest(
    *,
    context_id: str,
    semantic_sha256: str,
    policy_version: str = STRATEGY_DISCOVERY_POLICY_VERSION_V1,
    selection_root_seed: int = STRATEGY_DISCOVERY_TIE_ROOT_V1,
) -> bytes:
    context = _nfc_bytes(context_id, "tie context")
    policy = _nfc_bytes(policy_version, "tie policy")
    if type(semantic_sha256) is not str or _SHA256.fullmatch(semantic_sha256) is None:
        raise ValueError("tie semantic digest must be lowercase SHA-256")
    if (
        type(selection_root_seed) is not int
        or not 0 <= selection_root_seed < 1 << 64
    ):
        raise ValueError("tie root must be unsigned 64-bit")
    digest = hashlib.sha256()
    digest.update(b"KIRBY2_POLICY_TIE_V1\x00")
    digest.update(policy)
    digest.update(b"\x00")
    digest.update(context)
    digest.update(b"\x00")
    digest.update(struct.pack(">Q", selection_root_seed))
    digest.update(b"\x00")
    digest.update(bytes.fromhex(semantic_sha256))
    return digest.digest()


def materially_equivalent(first: int, second: int) -> bool:
    if type(first) is not int or type(second) is not int:
        raise TypeError("material-equivalence inputs must be integers")
    return abs(first - second) < MATERIAL_EQUIVALENCE_THRESHOLD_V1


def objective_protocol_projection() -> dict[str, object]:
    return {
        "complexity_coefficients": {
            "conditions": 4,
            "features": 3,
            "parameters": 1,
            "rolling_windows": 2,
            "states": 8,
            "transitions": 6,
        },
        "complexity_normalization_points": 200,
        "material_equivalence_threshold": MATERIAL_EQUIVALENCE_THRESHOLD_V1,
        "multiplicity_method": MULTIPLICITY_METHOD_V1,
        "multiplicity_penalty_step": MULTIPLICITY_PENALTY_STEP_V1,
        "objectives": [item.as_dict() for item in ALL_OBJECTIVE_SPECS_V1],
        "policy_scale": POLICY_SCALE_V1,
        "protocol_id": STRATEGY_OBJECTIVE_PROTOCOL_ID_V1,
        "root_reduction_order": ROOT_REDUCTION_ORDER_V1,
        "schema_id": STRATEGY_OBJECTIVE_SCHEMA_ID_V1,
        "schema_version": 1,
        "uncertainty_method": UNCERTAINTY_METHOD_V1,
    }


def _count_share(numerator: int, denominator: int, label: str) -> int:
    _require_nonnegative_int(numerator, f"{label} numerator")
    if type(denominator) is not int or denominator <= 0:
        raise ValueError(f"{label} denominator must be positive")
    if numerator > denominator:
        raise ValueError(f"{label} numerator exceeds denominator")
    return unsigned_share_ppm(numerator, denominator)


def _require_nonnegative_int(value: int, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _require_nfc_text(value: str, label: str) -> None:
    _nfc_bytes(value, label)


def _nfc_bytes(value: str, label: str) -> bytes:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")
    return value.encode("utf-8")


__all__ = [
    "ALL_OBJECTIVE_SPECS_V1",
    "EvidenceCompatibilityKeyV1",
    "MATERIAL_EQUIVALENCE_THRESHOLD_V1",
    "MULTIPLICITY_METHOD_V1",
    "MULTIPLICITY_PENALTY_STEP_V1",
    "ObjectiveApplicabilityV1",
    "ObjectiveSpecV1",
    "ObjectiveValueV1",
    "OPTIONAL_OBJECTIVE_SPECS_V1",
    "POLICY_SCALE_V1",
    "PartitionStatisticV1",
    "REQUIRED_OBJECTIVE_SPECS_V1",
    "ROOT_REDUCTION_ORDER_V1",
    "STRATEGY_DISCOVERY_POLICY_VERSION_V1",
    "STRATEGY_DISCOVERY_TIE_ROOT_V1",
    "STRATEGY_OBJECTIVE_PROTOCOL_ID_V1",
    "STRATEGY_OBJECTIVE_SCHEMA_ID_V1",
    "StrategyObjectiveIdV1",
    "UNCERTAINTY_METHOD_V1",
    "ceil_log2_one_plus",
    "balanced_classification_utility",
    "clamp",
    "common_tie_digest",
    "completion_utility",
    "complexity_points",
    "complexity_utility",
    "cross_cell_stability_utility",
    "discipline_compatibility_utility",
    "execution_opportunity_utility",
    "false_green_utility",
    "materially_equivalent",
    "median_and_mad",
    "missed_opportunity_utility",
    "multiplicity_penalty",
    "nearest_rank_p50",
    "objective_protocol_projection",
    "partition_statistic",
    "ratio_ppm",
    "root_composite",
    "round_div_even",
    "signed_cost_utility",
    "turnover_utility",
    "unsigned_share_ppm",
]
