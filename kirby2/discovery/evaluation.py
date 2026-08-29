"""Typed evidence and a development-only score oracle for WO35-D.

The synthetic oracle cannot open a dataset, simulator run, or sealed partition.
It exists only to make search ordering, budgets, qualification, and access control
executable before the first real experiment in WO35-F1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .objectives import (
    EvidenceCompatibilityKeyV1,
    PartitionStatisticV1,
    REQUIRED_OBJECTIVE_SPECS_V1,
    StrategyObjectiveIdV1,
    partition_statistic,
    ratio_ppm,
)
from .partitions import StrategyPartitionV1


SYNTHETIC_ORACLE_CONTROLLED_ID_V1 = "DEVELOPMENT_SYNTHETIC_SCORE_ORACLE_V1"
SYNTHETIC_ORACLE_NO_WINNER_ID_V1 = "NO_WINNER_SYNTHETIC_SCORE_ORACLE_V1"
SYNTHETIC_ORACLE_SCHEMA_ID_V1 = "KIRBY2_DEVELOPMENT_SCORE_ORACLE_V1"
SYNTHETIC_ORACLE_DATA_SOURCE_V1 = "SYNTHETIC_INTEGER_FUNCTION_ONLY_V1"
VALIDATION_QUALIFICATION_RULE_ID_V1 = "BOUNDED_SEARCH_VALIDATION_V1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

TRAIN_ROOTS_V1 = tuple(range(3_501_000, 3_501_012))
VALIDATION_ROOTS_V1 = tuple(range(3_502_000, 3_502_008))


class SyntheticOracleModeV1(str, Enum):
    CONTROLLED = "CONTROLLED"
    NO_WINNER = "NO_WINNER"


class EvaluationAccessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RootDeltaV1:
    root_seed: int
    composite_delta: int

    def __post_init__(self) -> None:
        if type(self.root_seed) is not int or not 0 <= self.root_seed < 1 << 64:
            raise ValueError("root delta seed must be unsigned 64-bit")
        if type(self.composite_delta) is not int:
            raise TypeError("root composite delta must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "composite_delta": self.composite_delta,
            "root_seed": self.root_seed,
        }


@dataclass(frozen=True, slots=True)
class ComponentDeltaV1:
    objective_id: StrategyObjectiveIdV1
    delta: int

    def __post_init__(self) -> None:
        if not isinstance(self.objective_id, StrategyObjectiveIdV1):
            raise TypeError("component delta objective must be typed")
        if self.objective_id is StrategyObjectiveIdV1.PNL:
            raise ValueError("zero-weight P&L cannot qualify a candidate")
        if type(self.delta) is not int:
            raise TypeError("component delta must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {"delta": self.delta, "objective_id": self.objective_id.value}


@dataclass(frozen=True, slots=True)
class CandidatePartitionEvidenceV1:
    candidate_id: str
    semantic_sha256: str
    partition: StrategyPartitionV1
    compatibility: EvidenceCompatibilityKeyV1
    root_deltas: tuple[RootDeltaV1, ...]
    component_deltas: tuple[ComponentDeltaV1, ...]
    candidate_trades: int
    base_trades: int
    complexity_points: int
    oracle_id: str

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or not self.candidate_id:
            raise ValueError("candidate evidence ID must be nonempty")
        if type(self.semantic_sha256) is not str or _SHA256.fullmatch(
            self.semantic_sha256
        ) is None:
            raise ValueError("candidate evidence semantic identity must be SHA-256")
        if not isinstance(self.partition, StrategyPartitionV1):
            raise TypeError("candidate evidence partition must be typed")
        if not isinstance(self.compatibility, EvidenceCompatibilityKeyV1):
            raise TypeError("candidate evidence compatibility key must be typed")
        if type(self.root_deltas) is not tuple or not self.root_deltas or any(
            not isinstance(item, RootDeltaV1) for item in self.root_deltas
        ):
            raise TypeError("candidate root deltas must be a nonempty typed tuple")
        ordered_roots = tuple(sorted(self.root_deltas, key=lambda item: item.root_seed))
        roots = tuple(item.root_seed for item in ordered_roots)
        if len(roots) != len(set(roots)):
            raise ValueError("candidate root deltas must use unique roots")
        object.__setattr__(self, "root_deltas", ordered_roots)
        if type(self.component_deltas) is not tuple or any(
            not isinstance(item, ComponentDeltaV1) for item in self.component_deltas
        ):
            raise TypeError("candidate component deltas must be a typed tuple")
        ordered_components = tuple(
            sorted(self.component_deltas, key=lambda item: item.objective_id.value)
        )
        component_ids = tuple(item.objective_id for item in ordered_components)
        required_ids = {item.objective_id for item in REQUIRED_OBJECTIVE_SPECS_V1}
        if len(component_ids) != len(set(component_ids)) or set(component_ids) != required_ids:
            raise ValueError("candidate evidence must contain every required component once")
        object.__setattr__(self, "component_deltas", ordered_components)
        if type(self.candidate_trades) is not int or self.candidate_trades < 0:
            raise ValueError("candidate trade count must be nonnegative")
        if type(self.base_trades) is not int or self.base_trades < 0:
            raise ValueError("base trade count must be nonnegative")
        if type(self.complexity_points) is not int or self.complexity_points < 0:
            raise ValueError("candidate complexity points must be nonnegative")
        if type(self.oracle_id) is not str or not self.oracle_id:
            raise ValueError("candidate evidence oracle ID must be nonempty")

    @property
    def deltas(self) -> tuple[int, ...]:
        return tuple(item.composite_delta for item in self.root_deltas)

    @property
    def training_merit(self) -> int:
        return partition_statistic(
            self.deltas,
            trained_candidate_count=0,
            apply_multiplicity=False,
        ).training_merit

    def statistic(self, trained_candidate_count: int) -> PartitionStatisticV1:
        return partition_statistic(
            self.deltas,
            trained_candidate_count=trained_candidate_count,
        )

    def component_delta(self, objective_id: StrategyObjectiveIdV1) -> int:
        for item in self.component_deltas:
            if item.objective_id is objective_id:
                return item.delta
        raise KeyError(objective_id)

    def as_dict(self, trained_candidate_count: int) -> dict[str, object]:
        return {
            "base_trades": self.base_trades,
            "candidate_id": self.candidate_id,
            "candidate_trades": self.candidate_trades,
            "compatibility": self.compatibility.as_dict(),
            "complexity_points": self.complexity_points,
            "component_deltas": [item.as_dict() for item in self.component_deltas],
            "oracle_id": self.oracle_id,
            "partition": self.partition.value,
            "root_deltas": [item.as_dict() for item in self.root_deltas],
            "semantic_sha256": self.semantic_sha256,
            "statistic": self.statistic(trained_candidate_count).as_dict(),
        }


@dataclass(frozen=True, slots=True)
class QualificationDecisionV1:
    qualified: bool
    reasons: tuple[str, ...]
    statistic: PartitionStatisticV1

    def __post_init__(self) -> None:
        if type(self.qualified) is not bool:
            raise TypeError("qualification decision must be Boolean")
        if type(self.reasons) is not tuple or any(
            type(item) is not str or not item for item in self.reasons
        ):
            raise TypeError("qualification reasons must be a text tuple")
        if not isinstance(self.statistic, PartitionStatisticV1):
            raise TypeError("qualification statistic must be typed")
        if self.qualified == bool(self.reasons):
            raise ValueError("qualification Boolean and reasons disagree")

    def as_dict(self) -> dict[str, object]:
        return {
            "qualified": self.qualified,
            "reasons": list(self.reasons),
            "statistic": self.statistic.as_dict(),
        }


def require_compatible_evidence(
    rows: tuple[CandidatePartitionEvidenceV1, ...],
) -> EvidenceCompatibilityKeyV1:
    if type(rows) is not tuple or not rows or any(
        not isinstance(item, CandidatePartitionEvidenceV1) for item in rows
    ):
        raise TypeError("comparison requires a nonempty typed evidence tuple")
    first = rows[0].compatibility
    if any(item.compatibility != first for item in rows[1:]):
        raise ValueError("candidate evidence groups are incompatible")
    partitions = {item.partition for item in rows}
    if len(partitions) != 1:
        raise ValueError("candidate evidence partitions are incompatible")
    return first


def validation_qualification(
    evidence: CandidatePartitionEvidenceV1,
    *,
    trained_candidate_count: int,
) -> QualificationDecisionV1:
    if not isinstance(evidence, CandidatePartitionEvidenceV1):
        raise TypeError("validation qualification requires typed evidence")
    if evidence.partition is not StrategyPartitionV1.VALIDATION:
        raise ValueError("validation qualification requires validation evidence")
    statistic = evidence.statistic(trained_candidate_count)
    failures: list[str] = []
    if tuple(item.root_seed for item in evidence.root_deltas) != VALIDATION_ROOTS_V1:
        failures.append("VALIDATION_ROOT_SET_INCOMPLETE")
    if statistic.statistic < 30_000:
        failures.append("STATISTIC_BELOW_30000")
    if sum(item > 0 for item in evidence.deltas) < 6:
        failures.append("FEWER_THAN_SIX_POSITIVE_ROOTS")
    classification = evidence.component_delta(
        StrategyObjectiveIdV1.BALANCED_CLASSIFICATION
    )
    opportunity = evidence.component_delta(StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY)
    if max(classification, opportunity) < 50_000:
        failures.append("NO_CLASSIFICATION_OR_OPPORTUNITY_EFFECT_50000")
    for item in evidence.component_deltas:
        if item.delta < -50_000:
            failures.append(f"REQUIRED_COMPONENT_BELOW_NEGATIVE_50000:{item.objective_id.value}")
    for objective_id in (
        StrategyObjectiveIdV1.FALSE_GREEN,
        StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
        StrategyObjectiveIdV1.COMPLETION,
    ):
        if evidence.component_delta(objective_id) < -20_000:
            failures.append(f"SENSITIVE_COMPONENT_BELOW_NEGATIVE_20000:{objective_id.value}")
    if evidence.candidate_trades < 30:
        failures.append("FEWER_THAN_THIRTY_CANDIDATE_TRADES")
    if evidence.base_trades <= 0:
        failures.append("ZERO_BASE_TRADES")
    else:
        trade_ratio = ratio_ppm(evidence.candidate_trades, evidence.base_trades)
        if not 600_000 <= trade_ratio <= 1_600_000:
            failures.append("TRADE_RATIO_OUTSIDE_600000_1600000")
    return QualificationDecisionV1(not failures, tuple(failures), statistic)


class DevelopmentSyntheticScoreOracleV1:
    """Stateful access boundary around a deterministic integer score function."""

    def __init__(
        self,
        *,
        mode: SyntheticOracleModeV1,
        compatibility: EvidenceCompatibilityKeyV1,
        train_budget: int,
    ) -> None:
        if not isinstance(mode, SyntheticOracleModeV1):
            raise TypeError("synthetic oracle mode must be typed")
        if not isinstance(compatibility, EvidenceCompatibilityKeyV1):
            raise TypeError("synthetic oracle compatibility key must be typed")
        if type(train_budget) is not int or not 1 <= train_budget <= 64:
            raise ValueError("synthetic oracle train budget must be in 1..64")
        self.mode = mode
        self.compatibility = compatibility
        self.train_budget = train_budget
        self._train: dict[str, CandidatePartitionEvidenceV1] = {}
        self._validation: dict[str, CandidatePartitionEvidenceV1] = {}
        self._validation_finalists: tuple[str, ...] | None = None
        self._access_log: list[tuple[str, str]] = []

    @property
    def oracle_id(self) -> str:
        if self.mode is SyntheticOracleModeV1.NO_WINNER:
            return SYNTHETIC_ORACLE_NO_WINNER_ID_V1
        return SYNTHETIC_ORACLE_CONTROLLED_ID_V1

    @property
    def train_evaluation_count(self) -> int:
        return len(self._train)

    @property
    def validation_evaluation_count(self) -> int:
        return len(self._validation)

    @property
    def real_partition_access_count(self) -> int:
        return 0

    @property
    def access_log(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._access_log)

    def freeze_validation(self, finalist_semantic_sha256: tuple[str, ...]) -> None:
        if self._validation_finalists is not None:
            raise EvaluationAccessError(
                "VALIDATION_ALREADY_FROZEN",
                "validation finalists can be frozen only once",
            )
        if type(finalist_semantic_sha256) is not tuple or not finalist_semantic_sha256:
            raise ValueError("validation finalist set must be nonempty")
        if len(finalist_semantic_sha256) > 8:
            raise ValueError("validation finalist set exceeds the fixed limit")
        if len(set(finalist_semantic_sha256)) != len(finalist_semantic_sha256):
            raise ValueError("validation finalist set contains duplicates")
        if any(item not in self._train for item in finalist_semantic_sha256):
            raise EvaluationAccessError(
                "FINALIST_NOT_TRAINED",
                "validation finalist is absent from training evidence",
            )
        self._validation_finalists = finalist_semantic_sha256
        self._access_log.append(("FREEZE", ",".join(finalist_semantic_sha256)))

    def evaluate(
        self,
        *,
        candidate_id: str,
        semantic_sha256: str,
        vector_values: tuple[int, int, int, int],
        complexity_points: int,
        partition: StrategyPartitionV1,
    ) -> CandidatePartitionEvidenceV1:
        if type(candidate_id) is not str or not candidate_id:
            raise ValueError("oracle candidate ID must be nonempty")
        if type(semantic_sha256) is not str or _SHA256.fullmatch(semantic_sha256) is None:
            raise ValueError("oracle candidate semantic digest must be SHA-256")
        if (
            type(vector_values) is not tuple
            or len(vector_values) != 4
            or any(type(item) is not int for item in vector_values)
        ):
            raise TypeError("oracle vector must contain four integers")
        if type(complexity_points) is not int or complexity_points < 0:
            raise ValueError("oracle complexity points must be nonnegative")
        if not isinstance(partition, StrategyPartitionV1):
            raise TypeError("oracle partition must be typed")
        if partition not in {StrategyPartitionV1.TRAIN, StrategyPartitionV1.VALIDATION}:
            raise EvaluationAccessError(
                "REAL_PARTITION_FORBIDDEN",
                "the WO35-D development oracle cannot access real or sealed partitions",
            )
        if partition is StrategyPartitionV1.TRAIN:
            cached = self._train.get(semantic_sha256)
            if cached is not None:
                return cached
            if self._validation_finalists is not None:
                raise EvaluationAccessError(
                    "TRAINING_AFTER_FINALIST_FREEZE",
                    "training cannot resume after validation finalists freeze",
                )
            if len(self._train) >= self.train_budget:
                raise EvaluationAccessError(
                    "TRAIN_BUDGET_EXHAUSTED",
                    "first-time training evaluation exceeds the effective budget",
                )
            evidence = self._score(
                candidate_id,
                semantic_sha256,
                vector_values,
                complexity_points,
                partition,
            )
            self._train[semantic_sha256] = evidence
            self._access_log.append((partition.value, semantic_sha256))
            return evidence
        if self._validation_finalists is None:
            raise EvaluationAccessError(
                "VALIDATION_BEFORE_FINALIST_FREEZE",
                "validation cannot open before the finalist set freezes",
            )
        if semantic_sha256 not in self._validation_finalists:
            raise EvaluationAccessError(
                "VALIDATION_NON_FINALIST",
                "validation access is limited to frozen finalists",
            )
        cached = self._validation.get(semantic_sha256)
        if cached is not None:
            return cached
        evidence = self._score(
            candidate_id,
            semantic_sha256,
            vector_values,
            complexity_points,
            partition,
        )
        self._validation[semantic_sha256] = evidence
        self._access_log.append((partition.value, semantic_sha256))
        return evidence

    def _score(
        self,
        candidate_id: str,
        semantic_sha256: str,
        vector_values: tuple[int, int, int, int],
        candidate_complexity_points: int,
        partition: StrategyPartitionV1,
    ) -> CandidatePartitionEvidenceV1:
        window_us, green_ticks, imbalance_ppm, wait_ticks = vector_values
        if self.mode is SyntheticOracleModeV1.NO_WINNER:
            quality = 12_000
        else:
            window_cost = {2_000_000: 12_000, 5_000_000: 0, 10_000_000: 14_000}[window_us]
            imbalance_cost = abs(imbalance_ppm - 300_000) // 20
            wait_cost = abs(wait_ticks - 4) * 5_000
            preferred_green = 2 if partition is StrategyPartitionV1.TRAIN else 1
            green_cost = abs(green_ticks - preferred_green) * 6_000
            quality = 125_000 - window_cost - imbalance_cost - wait_cost - green_cost
            if partition is StrategyPartitionV1.VALIDATION and (
                window_us,
                green_ticks,
                imbalance_ppm,
                wait_ticks,
            ) == (5_000_000, 2, 300_000, 4):
                quality = -25_000
        roots = TRAIN_ROOTS_V1 if partition is StrategyPartitionV1.TRAIN else VALIDATION_ROOTS_V1
        offsets = (-4_000, -2_000, -1_000, 0, 1_000, 2_000, 3_000, 4_000)
        root_deltas = tuple(
            RootDeltaV1(root, quality + offsets[index % len(offsets)])
            for index, root in enumerate(roots)
        )
        if self.mode is SyntheticOracleModeV1.NO_WINNER or quality < 0:
            primary = max(-40_000, quality)
            candidate_trades = 24
        else:
            primary = quality
            candidate_trades = 48
        neutral = max(-40_000, min(primary, 40_000))
        component_by_id = {
            StrategyObjectiveIdV1.BALANCED_CLASSIFICATION: primary,
            StrategyObjectiveIdV1.DISCIPLINE_COMPATIBILITY: neutral,
            StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY: primary - 2_000,
            StrategyObjectiveIdV1.FALSE_GREEN: neutral,
            StrategyObjectiveIdV1.MISSED_OPPORTUNITY: neutral,
            StrategyObjectiveIdV1.ADVERSE_SELECTION: neutral,
            StrategyObjectiveIdV1.TURNOVER: 0,
            StrategyObjectiveIdV1.SPREAD_PAID: neutral,
            StrategyObjectiveIdV1.COMPLETION: neutral,
            StrategyObjectiveIdV1.CROSS_CELL_STABILITY: neutral,
            StrategyObjectiveIdV1.COMPLEXITY: 0,
        }
        return CandidatePartitionEvidenceV1(
            candidate_id=candidate_id,
            semantic_sha256=semantic_sha256,
            partition=partition,
            compatibility=self.compatibility,
            root_deltas=root_deltas,
            component_deltas=tuple(
                ComponentDeltaV1(objective_id, component_by_id[objective_id])
                for objective_id in sorted(component_by_id, key=lambda item: item.value)
            ),
            candidate_trades=candidate_trades,
            base_trades=40,
            complexity_points=candidate_complexity_points,
            oracle_id=self.oracle_id,
        )


__all__ = [
    "CandidatePartitionEvidenceV1",
    "ComponentDeltaV1",
    "DevelopmentSyntheticScoreOracleV1",
    "EvaluationAccessError",
    "QualificationDecisionV1",
    "RootDeltaV1",
    "SYNTHETIC_ORACLE_CONTROLLED_ID_V1",
    "SYNTHETIC_ORACLE_DATA_SOURCE_V1",
    "SYNTHETIC_ORACLE_NO_WINNER_ID_V1",
    "SYNTHETIC_ORACLE_SCHEMA_ID_V1",
    "SyntheticOracleModeV1",
    "TRAIN_ROOTS_V1",
    "VALIDATION_QUALIFICATION_RULE_ID_V1",
    "VALIDATION_ROOTS_V1",
    "require_compatible_evidence",
    "validation_qualification",
]
