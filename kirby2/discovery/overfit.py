"""Exact pre/post reveal overfit predicates for WO35-E.

The development fixture is deliberately disjoint from the bounded search oracle.
It proves that a training star dominated by one validation seed and one scenario
family is labeled and rejected without opening any real partition.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .identity import canonical_identity_bytes
from .objectives import nearest_rank_p50, ratio_ppm, unsigned_share_ppm
from .partitions import StrategyPartitionV1
from .robustness import (
    ROBUSTNESS_ROOTS_V1,
    ROBUSTNESS_SETTINGS_V1,
    RobustnessEvidenceV1,
    RobustnessFamilyV1,
)


OVERFIT_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_OVERFIT_ASSESSMENT_V1"
OVERFIT_SCHEMA_VERSION_V1 = 1
OVERFIT_POLICY_ID_V1 = "STRATEGY_OVERFIT_EXACT_PREDICATES_V1"
DEVELOPMENT_OVERFIT_FIXTURE_ID_V1 = "WO35E_DEVELOPMENT_OVERFIT_FIXTURE_V1"
DEVELOPMENT_OVERFIT_DATA_SOURCE_V1 = "SYNTHETIC_INTEGER_FIXTURE_ONLY_V1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OverfitStageV1(str, Enum):
    PRE_REVEAL = "PRE_REVEAL"
    POST_REVEAL = "POST_REVEAL"


class OverfitLabelV1(str, Enum):
    TRAIN_VALIDATION_DIVERGENCE = "TRAIN_VALIDATION_DIVERGENCE"
    ONE_SEED_DEPENDENCE = "ONE_SEED_DEPENDENCE"
    ONE_SCENARIO_DEPENDENCE = "ONE_SCENARIO_DEPENDENCE"
    THRESHOLD_SENSITIVITY = "THRESHOLD_SENSITIVITY"
    COMPLEXITY_WITHOUT_HOLDOUT_GAIN = "COMPLEXITY_WITHOUT_HOLDOUT_GAIN"
    TRADE_SUPPRESSION = "TRADE_SUPPRESSION"
    EXCESSIVE_TRADE_FREQUENCY = "EXCESSIVE_TRADE_FREQUENCY"
    ONE_SEED_DEPENDENCE_HOLDOUT = "ONE_SEED_DEPENDENCE_HOLDOUT"
    ONE_SEED_DEPENDENCE_ADVERSARIAL = "ONE_SEED_DEPENDENCE_ADVERSARIAL"
    ONE_SCENARIO_DEPENDENCE_HOLDOUT = "ONE_SCENARIO_DEPENDENCE_HOLDOUT"
    ONE_SCENARIO_DEPENDENCE_ADVERSARIAL = "ONE_SCENARIO_DEPENDENCE_ADVERSARIAL"
    TRADE_SUPPRESSION_HOLDOUT = "TRADE_SUPPRESSION_HOLDOUT"
    TRADE_SUPPRESSION_ADVERSARIAL = "TRADE_SUPPRESSION_ADVERSARIAL"
    EXCESSIVE_TRADE_FREQUENCY_HOLDOUT = "EXCESSIVE_TRADE_FREQUENCY_HOLDOUT"
    EXCESSIVE_TRADE_FREQUENCY_ADVERSARIAL = "EXCESSIVE_TRADE_FREQUENCY_ADVERSARIAL"


PRE_REVEAL_APPLICABILITY_V1 = (
    OverfitLabelV1.TRAIN_VALIDATION_DIVERGENCE,
    OverfitLabelV1.ONE_SEED_DEPENDENCE,
    OverfitLabelV1.ONE_SCENARIO_DEPENDENCE,
    OverfitLabelV1.THRESHOLD_SENSITIVITY,
    OverfitLabelV1.TRADE_SUPPRESSION,
    OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY,
)
PRE_REVEAL_SEALED_V1 = (OverfitLabelV1.COMPLEXITY_WITHOUT_HOLDOUT_GAIN,)
POST_REVEAL_ADDITIONS_V1 = (
    OverfitLabelV1.COMPLEXITY_WITHOUT_HOLDOUT_GAIN,
    OverfitLabelV1.ONE_SEED_DEPENDENCE_HOLDOUT,
    OverfitLabelV1.ONE_SEED_DEPENDENCE_ADVERSARIAL,
    OverfitLabelV1.ONE_SCENARIO_DEPENDENCE_HOLDOUT,
    OverfitLabelV1.ONE_SCENARIO_DEPENDENCE_ADVERSARIAL,
    OverfitLabelV1.TRADE_SUPPRESSION_HOLDOUT,
    OverfitLabelV1.TRADE_SUPPRESSION_ADVERSARIAL,
    OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY_HOLDOUT,
    OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY_ADVERSARIAL,
)
_THRESHOLD_SETTING_IDS = tuple(
    item.setting_id
    for item in ROBUSTNESS_SETTINGS_V1
    if item.family is RobustnessFamilyV1.THRESHOLD
)


class OverfitEvidenceUnavailableError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OverfitCellV1:
    root_seed: int
    scenario_family: str
    composite_delta: int

    def __post_init__(self) -> None:
        if type(self.root_seed) is not int or not 0 <= self.root_seed < 1 << 64:
            raise ValueError("overfit root seed must be unsigned 64-bit")
        _require_nfc(self.scenario_family, "overfit scenario family")
        if type(self.composite_delta) is not int:
            raise TypeError("overfit composite delta must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "composite_delta": self.composite_delta,
            "root_seed": self.root_seed,
            "scenario_family": self.scenario_family,
        }


@dataclass(frozen=True, slots=True)
class OverfitPartitionEvidenceV1:
    candidate_semantic_sha256: str
    partition: StrategyPartitionV1
    cells: tuple[OverfitCellV1, ...]
    candidate_trades: int
    base_trades: int
    false_green_delta: int

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_semantic_sha256, "overfit candidate digest")
        if self.partition not in {
            StrategyPartitionV1.TRAIN,
            StrategyPartitionV1.VALIDATION,
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        }:
            raise ValueError("overfit partition is not eligible")
        if type(self.cells) is not tuple or not self.cells or any(
            not isinstance(item, OverfitCellV1) for item in self.cells
        ):
            raise TypeError("overfit evidence cells must be a nonempty typed tuple")
        ordered = tuple(
            sorted(
                self.cells,
                key=lambda item: (
                    item.root_seed,
                    item.scenario_family.encode("utf-8"),
                ),
            )
        )
        roots = tuple(item.root_seed for item in ordered)
        if len(roots) != len(set(roots)):
            raise ValueError("overfit partition requires exactly one cell per root")
        object.__setattr__(self, "cells", ordered)
        _require_nonnegative_int(self.candidate_trades, "overfit candidate trades")
        _require_nonnegative_int(self.base_trades, "overfit base trades")
        if type(self.false_green_delta) is not int:
            raise TypeError("overfit false-green delta must be an integer")

    @property
    def deltas(self) -> tuple[int, ...]:
        return tuple(item.composite_delta for item in self.cells)

    @property
    def median_delta(self) -> int:
        return nearest_rank_p50(self.deltas)

    def as_dict(self) -> dict[str, object]:
        return {
            "base_trades": self.base_trades,
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "candidate_trades": self.candidate_trades,
            "cells": [item.as_dict() for item in self.cells],
            "false_green_delta": self.false_green_delta,
            "median_delta": self.median_delta,
            "partition": self.partition.value,
        }


@dataclass(frozen=True, slots=True)
class ThresholdSettingMedianV1:
    setting_id: str
    median_delta: int

    def __post_init__(self) -> None:
        if self.setting_id not in _THRESHOLD_SETTING_IDS:
            raise ValueError("threshold overfit setting is not preregistered")
        if type(self.median_delta) is not int:
            raise TypeError("threshold setting median must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {"median_delta": self.median_delta, "setting_id": self.setting_id}


@dataclass(frozen=True, slots=True)
class ThresholdSensitivityEvidenceV1:
    candidate_semantic_sha256: str
    settings: tuple[ThresholdSettingMedianV1, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_semantic_sha256, "threshold candidate digest")
        if type(self.settings) is not tuple or any(
            not isinstance(item, ThresholdSettingMedianV1) for item in self.settings
        ):
            raise TypeError("threshold medians must be a typed tuple")
        if tuple(item.setting_id for item in self.settings) != _THRESHOLD_SETTING_IDS:
            raise ValueError("threshold medians differ from exact setting order")

    @property
    def deltas(self) -> tuple[int, ...]:
        return tuple(item.median_delta for item in self.settings)

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "settings": [item.as_dict() for item in self.settings],
        }


def threshold_evidence_from_robustness(
    evidence: RobustnessEvidenceV1,
) -> ThresholdSensitivityEvidenceV1:
    if not isinstance(evidence, RobustnessEvidenceV1):
        raise TypeError("threshold extraction requires typed robustness evidence")
    family = next(
        item
        for item in evidence.families
        if item.family is RobustnessFamilyV1.THRESHOLD
    )
    settings: list[ThresholdSettingMedianV1] = []
    for setting_id in _THRESHOLD_SETTING_IDS:
        cells = tuple(item for item in family.cells if item.setting_id == setting_id)
        if tuple(item.root_seed for item in cells) != ROBUSTNESS_ROOTS_V1:
            raise ValueError("threshold setting does not contain all robustness roots")
        settings.append(
            ThresholdSettingMedianV1(
                setting_id,
                nearest_rank_p50(tuple(item.composite_delta for item in cells)),
            )
        )
    return ThresholdSensitivityEvidenceV1(
        evidence.candidate_semantic_sha256,
        tuple(settings),
    )


@dataclass(frozen=True, slots=True)
class OverfitAssessmentV1:
    candidate_semantic_sha256: str
    stage: OverfitStageV1
    evaluated_labels: tuple[OverfitLabelV1, ...]
    sealed_not_evaluated: tuple[OverfitLabelV1, ...]
    labels: tuple[OverfitLabelV1, ...]
    preserved_pre_reveal_sha256: str | None

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_semantic_sha256, "assessment candidate digest")
        if not isinstance(self.stage, OverfitStageV1):
            raise TypeError("overfit assessment stage must be typed")
        for value, label in (
            (self.evaluated_labels, "evaluated overfit labels"),
            (self.sealed_not_evaluated, "sealed overfit labels"),
            (self.labels, "triggered overfit labels"),
        ):
            if type(value) is not tuple or any(
                not isinstance(item, OverfitLabelV1) for item in value
            ):
                raise TypeError(f"{label} must be a typed tuple")
            if len(value) != len(set(value)):
                raise ValueError(f"{label} must be unique")
        if not set(self.labels) <= set(self.evaluated_labels):
            raise ValueError("triggered overfit labels include an unevaluated predicate")
        if self.labels != tuple(
            item for item in self.evaluated_labels if item in set(self.labels)
        ):
            raise ValueError("triggered overfit labels are not in applicability order")
        if set(self.evaluated_labels).intersection(self.sealed_not_evaluated):
            raise ValueError("overfit label cannot be evaluated and sealed")
        if self.stage is OverfitStageV1.PRE_REVEAL:
            if (
                self.evaluated_labels != PRE_REVEAL_APPLICABILITY_V1
                or self.sealed_not_evaluated != PRE_REVEAL_SEALED_V1
                or self.preserved_pre_reveal_sha256 is not None
            ):
                raise ValueError("pre-reveal overfit applicability matrix changed")
        else:
            if (
                self.evaluated_labels
                != PRE_REVEAL_APPLICABILITY_V1 + POST_REVEAL_ADDITIONS_V1
                or self.sealed_not_evaluated
                or self.preserved_pre_reveal_sha256 is None
            ):
                raise ValueError("post-reveal overfit applicability matrix changed")
            _require_sha256(
                self.preserved_pre_reveal_sha256,
                "preserved pre-reveal assessment digest",
            )

    @property
    def rejected(self) -> bool:
        return bool(self.labels)

    @property
    def assessment_sha256(self) -> str:
        raw = canonical_identity_bytes(self.as_dict())
        digest = hashlib.sha256()
        digest.update(b"KIRBY2_STRATEGY_OVERFIT_ASSESSMENT_V1\x00")
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "evaluated_labels": [item.value for item in self.evaluated_labels],
            "labels": [item.value for item in self.labels],
            "policy_id": OVERFIT_POLICY_ID_V1,
            "preserved_pre_reveal_sha256": self.preserved_pre_reveal_sha256,
            "rejected": self.rejected,
            "schema_id": OVERFIT_SCHEMA_ID_V1,
            "schema_version": OVERFIT_SCHEMA_VERSION_V1,
            "sealed_not_evaluated": [
                item.value for item in self.sealed_not_evaluated
            ],
            "stage": self.stage.value,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentOverfitFixtureV1:
    fixture_id: str
    train: OverfitPartitionEvidenceV1
    validation: OverfitPartitionEvidenceV1
    threshold: ThresholdSensitivityEvidenceV1
    assessment: OverfitAssessmentV1
    data_source: str = DEVELOPMENT_OVERFIT_DATA_SOURCE_V1

    def __post_init__(self) -> None:
        if self.fixture_id != DEVELOPMENT_OVERFIT_FIXTURE_ID_V1:
            raise ValueError("development overfit fixture ID changed")
        if self.data_source != DEVELOPMENT_OVERFIT_DATA_SOURCE_V1:
            raise ValueError("development overfit fixture source changed")
        if self.assessment.candidate_semantic_sha256 != self.train.candidate_semantic_sha256:
            raise ValueError("development overfit fixture candidate binding changed")

    @property
    def fixture_sha256(self) -> str:
        return hashlib.sha256(canonical_identity_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment": self.assessment.as_dict(),
            "data_source": self.data_source,
            "fixture_id": self.fixture_id,
            "threshold": self.threshold.as_dict(),
            "train": self.train.as_dict(),
            "validation": self.validation.as_dict(),
        }


def assess_pre_reveal_overfit(
    train: OverfitPartitionEvidenceV1,
    validation: OverfitPartitionEvidenceV1,
    threshold: ThresholdSensitivityEvidenceV1,
) -> OverfitAssessmentV1:
    _require_partition(train, StrategyPartitionV1.TRAIN)
    _require_partition(validation, StrategyPartitionV1.VALIDATION)
    candidate = train.candidate_semantic_sha256
    if (
        validation.candidate_semantic_sha256 != candidate
        or threshold.candidate_semantic_sha256 != candidate
    ):
        raise ValueError("pre-reveal overfit evidence candidates differ")
    _require_trade_inputs(validation)
    labels: list[OverfitLabelV1] = []
    if train.median_delta >= 80_000 and validation.median_delta <= 0:
        labels.append(OverfitLabelV1.TRAIN_VALIDATION_DIVERGENCE)
    if one_seed_dependence(validation):
        labels.append(OverfitLabelV1.ONE_SEED_DEPENDENCE)
    if one_scenario_dependence(validation):
        labels.append(OverfitLabelV1.ONE_SCENARIO_DEPENDENCE)
    if threshold_sensitivity(threshold):
        labels.append(OverfitLabelV1.THRESHOLD_SENSITIVITY)
    if trade_suppression(validation):
        labels.append(OverfitLabelV1.TRADE_SUPPRESSION)
    if excessive_trade_frequency(validation):
        labels.append(OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY)
    return OverfitAssessmentV1(
        candidate,
        OverfitStageV1.PRE_REVEAL,
        PRE_REVEAL_APPLICABILITY_V1,
        PRE_REVEAL_SEALED_V1,
        tuple(labels),
        None,
    )


def assess_post_reveal_overfit(
    pre_reveal: OverfitAssessmentV1,
    holdout: OverfitPartitionEvidenceV1,
    adversarial: OverfitPartitionEvidenceV1,
    *,
    candidate_complexity_points: int,
    base_complexity_points: int,
) -> OverfitAssessmentV1:
    if (
        not isinstance(pre_reveal, OverfitAssessmentV1)
        or pre_reveal.stage is not OverfitStageV1.PRE_REVEAL
    ):
        raise TypeError("post-reveal assessment requires a frozen pre-reveal assessment")
    _require_partition(holdout, StrategyPartitionV1.HOLDOUT)
    _require_partition(adversarial, StrategyPartitionV1.ADVERSARIAL_HOLDOUT)
    candidate = pre_reveal.candidate_semantic_sha256
    if (
        holdout.candidate_semantic_sha256 != candidate
        or adversarial.candidate_semantic_sha256 != candidate
    ):
        raise ValueError("post-reveal overfit evidence candidates differ")
    _require_trade_inputs(holdout)
    _require_trade_inputs(adversarial)
    _require_nonnegative_int(candidate_complexity_points, "candidate complexity")
    _require_nonnegative_int(base_complexity_points, "base complexity")
    additions: list[OverfitLabelV1] = []
    if (
        candidate_complexity_points - base_complexity_points >= 20
        and holdout.median_delta < 30_000
    ):
        additions.append(OverfitLabelV1.COMPLEXITY_WITHOUT_HOLDOUT_GAIN)
    if one_seed_dependence(holdout):
        additions.append(OverfitLabelV1.ONE_SEED_DEPENDENCE_HOLDOUT)
    if one_seed_dependence(adversarial):
        additions.append(OverfitLabelV1.ONE_SEED_DEPENDENCE_ADVERSARIAL)
    if one_scenario_dependence(holdout):
        additions.append(OverfitLabelV1.ONE_SCENARIO_DEPENDENCE_HOLDOUT)
    if one_scenario_dependence(adversarial):
        additions.append(OverfitLabelV1.ONE_SCENARIO_DEPENDENCE_ADVERSARIAL)
    if trade_suppression(holdout):
        additions.append(OverfitLabelV1.TRADE_SUPPRESSION_HOLDOUT)
    if trade_suppression(adversarial):
        additions.append(OverfitLabelV1.TRADE_SUPPRESSION_ADVERSARIAL)
    if excessive_trade_frequency(holdout):
        additions.append(OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY_HOLDOUT)
    if excessive_trade_frequency(adversarial):
        additions.append(OverfitLabelV1.EXCESSIVE_TRADE_FREQUENCY_ADVERSARIAL)
    return OverfitAssessmentV1(
        candidate,
        OverfitStageV1.POST_REVEAL,
        PRE_REVEAL_APPLICABILITY_V1 + POST_REVEAL_ADDITIONS_V1,
        (),
        pre_reveal.labels + tuple(additions),
        pre_reveal.assessment_sha256,
    )


def one_seed_dependence(evidence: OverfitPartitionEvidenceV1) -> bool:
    _require_typed_evidence(evidence)
    total_positive = sum(max(0, item.composite_delta) for item in evidence.cells)
    full_median = evidence.median_delta
    for removed in evidence.cells:
        contribution = (
            total_positive > 0
            and unsigned_share_ppm(
                max(0, removed.composite_delta),
                total_positive,
            )
            >= 500_000
        )
        remaining = tuple(
            item.composite_delta
            for item in evidence.cells
            if item.root_seed != removed.root_seed
        )
        flip = bool(
            remaining
            and full_median > 0
            and nearest_rank_p50(remaining) <= 0
        )
        if contribution or flip:
            return True
    return False


def one_scenario_dependence(evidence: OverfitPartitionEvidenceV1) -> bool:
    _require_typed_evidence(evidence)
    total_positive = sum(max(0, item.composite_delta) for item in evidence.cells)
    full_median = evidence.median_delta
    families = tuple(
        sorted(
            {item.scenario_family for item in evidence.cells},
            key=lambda item: item.encode("utf-8"),
        )
    )
    for family in families:
        removed = tuple(
            item for item in evidence.cells if item.scenario_family == family
        )
        removed_positive = sum(max(0, item.composite_delta) for item in removed)
        contribution = (
            total_positive > 0
            and unsigned_share_ppm(removed_positive, total_positive) >= 500_000
        )
        remaining = tuple(
            item.composite_delta
            for item in evidence.cells
            if item.scenario_family != family
        )
        flip = bool(
            remaining
            and full_median > 0
            and nearest_rank_p50(remaining) <= 0
        )
        if contribution or flip:
            return True
    return False


def threshold_sensitivity(evidence: ThresholdSensitivityEvidenceV1) -> bool:
    if not isinstance(evidence, ThresholdSensitivityEvidenceV1):
        raise TypeError("threshold sensitivity requires typed evidence")
    values = evidence.deltas
    return (any(value < 0 for value in values) and any(value > 0 for value in values)) or (
        max(values) - min(values) > 100_000
    )


def trade_suppression(evidence: OverfitPartitionEvidenceV1) -> bool:
    _require_trade_inputs(evidence)
    return evidence.candidate_trades < 30 or ratio_ppm(
        evidence.candidate_trades,
        evidence.base_trades,
    ) < 600_000


def excessive_trade_frequency(evidence: OverfitPartitionEvidenceV1) -> bool:
    _require_trade_inputs(evidence)
    return (
        ratio_ppm(evidence.candidate_trades, evidence.base_trades) > 1_600_000
        and evidence.false_green_delta < -20_000
    )


def build_development_overfit_fixture() -> DevelopmentOverfitFixtureV1:
    candidate = hashlib.sha256(b"wo35e/development-overfit-candidate").hexdigest()
    roots = tuple(range(3_509_000, 3_509_004))
    families = (
        "SOLE_POSITIVE_FAMILY",
        "CONTROL_FAMILY",
        "CONTROL_FAMILY",
        "CONTROL_FAMILY",
    )
    train = OverfitPartitionEvidenceV1(
        candidate,
        StrategyPartitionV1.TRAIN,
        tuple(
            OverfitCellV1(root, family, 100_000)
            for root, family in zip(roots, families, strict=True)
        ),
        40,
        40,
        0,
    )
    validation_deltas = (600_000, -20_000, -20_000, -20_000)
    validation = OverfitPartitionEvidenceV1(
        candidate,
        StrategyPartitionV1.VALIDATION,
        tuple(
            OverfitCellV1(root, family, delta)
            for root, family, delta in zip(
                roots,
                families,
                validation_deltas,
                strict=True,
            )
        ),
        40,
        40,
        0,
    )
    threshold = ThresholdSensitivityEvidenceV1(
        candidate,
        tuple(
            ThresholdSettingMedianV1(setting_id, 10_000)
            for setting_id in _THRESHOLD_SETTING_IDS
        ),
    )
    assessment = assess_pre_reveal_overfit(train, validation, threshold)
    return DevelopmentOverfitFixtureV1(
        DEVELOPMENT_OVERFIT_FIXTURE_ID_V1,
        train,
        validation,
        threshold,
        assessment,
    )


def _require_partition(
    evidence: OverfitPartitionEvidenceV1,
    partition: StrategyPartitionV1,
) -> None:
    _require_typed_evidence(evidence)
    if evidence.partition is not partition:
        raise ValueError(f"overfit evidence must use {partition.value}")


def _require_typed_evidence(evidence: OverfitPartitionEvidenceV1) -> None:
    if not isinstance(evidence, OverfitPartitionEvidenceV1):
        raise TypeError("overfit predicate requires typed partition evidence")


def _require_trade_inputs(evidence: OverfitPartitionEvidenceV1) -> None:
    _require_typed_evidence(evidence)
    if evidence.base_trades <= 0:
        raise OverfitEvidenceUnavailableError(
            "ZERO_BASE_TRADES",
            "trade overfit predicates require nonzero base trades",
        )


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _require_nfc(value: object, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")


__all__ = [
    "DEVELOPMENT_OVERFIT_DATA_SOURCE_V1",
    "DEVELOPMENT_OVERFIT_FIXTURE_ID_V1",
    "DevelopmentOverfitFixtureV1",
    "OVERFIT_POLICY_ID_V1",
    "OVERFIT_SCHEMA_ID_V1",
    "OverfitAssessmentV1",
    "OverfitCellV1",
    "OverfitEvidenceUnavailableError",
    "OverfitLabelV1",
    "OverfitPartitionEvidenceV1",
    "OverfitStageV1",
    "POST_REVEAL_ADDITIONS_V1",
    "PRE_REVEAL_APPLICABILITY_V1",
    "PRE_REVEAL_SEALED_V1",
    "ThresholdSensitivityEvidenceV1",
    "ThresholdSettingMedianV1",
    "assess_post_reveal_overfit",
    "assess_pre_reveal_overfit",
    "build_development_overfit_fixture",
    "excessive_trade_frequency",
    "one_scenario_dependence",
    "one_seed_dependence",
    "threshold_sensitivity",
    "threshold_evidence_from_robustness",
    "trade_suppression",
]
