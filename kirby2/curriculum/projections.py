"""Immutable, versioned learner-state projection contracts for WO34-B.

These records are estimates derived from immutable evidence.  They never rewrite the
evidence ledger and they are explicitly unvalidated for real learning outcomes.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .errors import AMBIGUITY_ERROR_TYPES_V1, LearnerErrorTypeV1
from .evidence import AttemptModeV1, EvidenceSourceClassV1, POLICY_SCALE_V1
from .skills import (
    CURRICULUM_SKILL_IDS_V1,
    canonical_json_bytes,
    require_stable_skill_v1,
    sha256_json,
)


LEARNER_PROJECTION_SCHEMA_VERSION_V1 = 1
LEARNER_PROJECTION_MODEL_ID_V1 = "LEARNER_PROJECTION_V1"
LEARNER_PROJECTION_STATUS_V1 = "UNVALIDATED_FOR_LEARNING_OUTCOMES"
LEARNER_PROJECTION_ID_PREFIX_V1 = "learner-projection-"

GUIDED_BASE_WEIGHT_PPM_V1 = 250_000
PRACTICE_BASE_WEIGHT_PPM_V1 = 600_000
ASSESSMENT_BASE_WEIGHT_PPM_V1 = 1_000_000
REMEDIATION_BASE_WEIGHT_PPM_V1 = 700_000
RECENCY_AGE_SLOPE_PPM_V1 = 50_000
PSEUDO_OBSERVATION_COUNT_V1 = 4
PSEUDO_SUCCESS_COUNT_V1 = 2
EVIDENCE_CONFIDENCE_DIVISOR_V1 = 8
RECENT_HISTORY_LIMIT_V1 = 20
SUCCESS_SCORE_MIN_PPM_V1 = 700_000
FAILURE_SCORE_MAX_PPM_V1 = 300_000
SUFFICIENT_OPPORTUNITY_COUNT_V1 = 8
SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1 = 4_000_000
SUFFICIENT_SCENARIO_COUNT_V1 = 3
SUFFICIENT_VOLUME_BAND_COUNT_V1 = 2
SUFFICIENT_LIQUIDITY_BAND_COUNT_V1 = 2
SUFFICIENT_CONFIDENCE_PPM_V1 = 500_000
NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1 = 500_000
DEVELOPING_MAX_EXCLUSIVE_PPM_V1 = 700_000

MODE_BASE_WEIGHT_PPM_V1 = MappingProxyType(
    {
        AttemptModeV1.GUIDED: GUIDED_BASE_WEIGHT_PPM_V1,
        AttemptModeV1.PRACTICE: PRACTICE_BASE_WEIGHT_PPM_V1,
        AttemptModeV1.ASSESSMENT: ASSESSMENT_BASE_WEIGHT_PPM_V1,
        AttemptModeV1.REMEDIATION: REMEDIATION_BASE_WEIGHT_PPM_V1,
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def round_div_even_v1(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise TypeError("projection round-div-even requires exact integers")
    if denominator <= 0:
        raise ValueError("projection round-div-even denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return -quotient if numerator < 0 else quotient


def mul_ppm_v1(left: int, right: int) -> int:
    if type(left) is not int or type(right) is not int:
        raise TypeError("projection ppm multiplication requires exact integers")
    return round_div_even_v1(left * right, POLICY_SCALE_V1)


def unsigned_share_ppm_v1(numerator: int, denominator: int) -> int:
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or numerator < 0
        or denominator <= 0
        or numerator > denominator
    ):
        raise ValueError("projection unsigned share requires 0 <= n <= d")
    return round_div_even_v1(numerator * POLICY_SCALE_V1, denominator)


def recency_factor_ppm_v1(age_attempts: int) -> int:
    if type(age_attempts) is not int or age_attempts < 0:
        raise ValueError("projection age must be a nonnegative exact integer")
    return round_div_even_v1(
        POLICY_SCALE_V1 * POLICY_SCALE_V1,
        POLICY_SCALE_V1 + RECENCY_AGE_SLOPE_PPM_V1 * age_attempts,
    )


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{label} must be nonempty NFC text")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, label: str) -> str:
    selected = _text(value, label)
    if not selected.endswith("Z"):
        raise ValueError(f"{label} must use explicit UTC Z notation")
    try:
        parsed = datetime.fromisoformat(selected[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")
    return selected


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an exact integer >= {minimum}")
    return value


def _ppm(value: object, label: str) -> int:
    selected = _integer(value, label)
    if selected > POLICY_SCALE_V1:
        raise ValueError(f"{label} must be in [0,S]")
    return selected


def _optional_ppm(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _ppm(value, label)


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} is not one canonical JSON object")
    return payload


class ProjectionDiversityBandV1(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


def projection_diversity_band_v1(multiplier_ppm: int) -> ProjectionDiversityBandV1:
    if type(multiplier_ppm) is not int or multiplier_ppm <= 0:
        raise ValueError("projection diversity multiplier must be positive integer ppm")
    if multiplier_ppm < 750_000:
        return ProjectionDiversityBandV1.LOW
    if multiplier_ppm <= 1_250_000:
        return ProjectionDiversityBandV1.NORMAL
    return ProjectionDiversityBandV1.HIGH


class ProjectionSufficiencyV1(str, Enum):
    INSUFFICIENT = "INSUFFICIENT"
    SUFFICIENT = "SUFFICIENT"


class ProjectedSkillLabelV1(str, Enum):
    INSUFFICIENT = "INSUFFICIENT"
    NEEDS_WORK = "NEEDS_WORK"
    DEVELOPING = "DEVELOPING"
    STRONG = "STRONG"


class DemonstratedOutcomeKindV1(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class LearnerProjectionPolicyV1:
    model_id: str = LEARNER_PROJECTION_MODEL_ID_V1
    schema_version: int = LEARNER_PROJECTION_SCHEMA_VERSION_V1
    model_status: str = LEARNER_PROJECTION_STATUS_V1

    def __post_init__(self) -> None:
        if (
            self.model_id != LEARNER_PROJECTION_MODEL_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != LEARNER_PROJECTION_SCHEMA_VERSION_V1
            or self.model_status != LEARNER_PROJECTION_STATUS_V1
        ):
            raise ValueError("learner projection policy identity differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "confidence": {
                "diversity_caps": {
                    "liquidity_bands": 3,
                    "scenarios": 4,
                    "source_classes": 2,
                    "volume_bands": 3,
                },
                "diversity_formula": "ROUND_MEAN_OF_FOUR_UNSIGNED_SHARES",
                "evidence_divisor": EVIDENCE_CONFIDENCE_DIVISOR_V1,
                "final_formula": "MIN_EVIDENCE_AND_DIVERSITY",
                "uncertainty_formula": "S-CONFIDENCE",
            },
            "decay": {
                "age_source": "ATTEMPT_ORDINAL_ONLY",
                "effective_weight_formula": "MUL_PPM(BASE_WEIGHT,RECENCY_FACTOR)",
                "formula": "ROUND_DIV_EVEN(S*S,S+50000*AGE)",
                "slope_ppm": RECENCY_AGE_SLOPE_PPM_V1,
            },
            "diversity_bands_ppm": {
                "high_min_exclusive": 1_250_000,
                "low_max_exclusive": 750_000,
                "normal_max_inclusive": 1_250_000,
                "normal_min_inclusive": 750_000,
            },
            "error_caps": {
                "critical_ppm": 0,
                "default_ppm": 250_000,
                "multiple_caps": "MINIMUM",
            },
            "history_limit": RECENT_HISTORY_LIMIT_V1,
            "labels": {
                "developing_max_exclusive_ppm": DEVELOPING_MAX_EXCLUSIVE_PPM_V1,
                "needs_work_max_exclusive_ppm": NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1,
            },
            "mastery": {
                "formula": "ROUND_DIV_EVEN(2*S*S+SUM(W*SCORE),4*S+SUM(W))",
                "model_evidence_score_formula": (
                    "ROUND_DIV_EVEN(SUM(W*SCORE),SUM(W))_OR_NULL"
                ),
                "pseudo_observations": PSEUDO_OBSERVATION_COUNT_V1,
                "pseudo_failure_score_ppm": 0,
                "pseudo_successes": PSEUDO_SUCCESS_COUNT_V1,
                "pseudo_success_score_ppm": POLICY_SCALE_V1,
            },
            "mode_base_weights_ppm": {
                mode.value: weight for mode, weight in MODE_BASE_WEIGHT_PPM_V1.items()
            },
            "model_id": self.model_id,
            "model_status": self.model_status,
            "opportunity_count_definition": "POSITIVE_WEIGHT_SKILL_ROWS",
            "ordering": [
                "attempt_ordinal",
                "assessment_id_nfc_utf8",
                "evidence_id_nfc_utf8",
                "skill_id_nfc_utf8",
            ],
            "pnl_weight_ppm": 0,
            "policy_scale_ppm": POLICY_SCALE_V1,
            "recommendation_eligible_rule": "SUFFICIENCY_IS_SUFFICIENT",
            "schema_version": self.schema_version,
            "score_thresholds_ppm": {
                "failure_max": FAILURE_SCORE_MAX_PPM_V1,
                "success_min": SUCCESS_SCORE_MIN_PPM_V1,
            },
            "sufficiency": {
                "confidence_min_ppm": SUFFICIENT_CONFIDENCE_PPM_V1,
                "effective_weight_min_ppm": SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1,
                "liquidity_band_count_min": SUFFICIENT_LIQUIDITY_BAND_COUNT_V1,
                "opportunity_count_min": SUFFICIENT_OPPORTUNITY_COUNT_V1,
                "scenario_count_min": SUFFICIENT_SCENARIO_COUNT_V1,
                "volume_band_count_min": SUFFICIENT_VOLUME_BAND_COUNT_V1,
            },
            "zero_weight_rules": [
                "NO_OPPORTUNITY",
                "UNOBSERVABLE",
                "UNSCORABLE",
                "AMBIGUOUS",
                "INSUFFICIENT_OBSERVABILITY",
                "PNL",
            ],
        }

    @property
    def policy_digest(self) -> str:
        return sha256_json(self.as_dict())


LEARNER_PROJECTION_POLICY_V1 = LearnerProjectionPolicyV1()


@dataclass(frozen=True, slots=True)
class ProjectionEvidenceObservationV1:
    skill_id: str
    assessment_id: str
    evidence_id: str
    attempt_ordinal: int
    mode: AttemptModeV1
    raw_score_ppm: int
    post_cap_score_ppm: int
    applied_error_types: tuple[LearnerErrorTypeV1, ...]
    base_weight_ppm: int
    age_attempts: int
    recency_factor_ppm: int
    effective_weight_ppm: int
    scenario_semantic_sha256: str
    volume_band: ProjectionDiversityBandV1
    liquidity_band: ProjectionDiversityBandV1
    source_class: EvidenceSourceClassV1
    study_timestamp_utc: str
    simulation_time_us: int

    def __post_init__(self) -> None:
        require_stable_skill_v1(self.skill_id)
        _text(self.assessment_id, "projection assessment ID")
        _text(self.evidence_id, "projection evidence ID")
        _integer(self.attempt_ordinal, "projection attempt ordinal", minimum=1)
        if not isinstance(self.mode, AttemptModeV1):
            raise TypeError("projection observation mode is invalid")
        _ppm(self.raw_score_ppm, "projection raw score")
        _ppm(self.post_cap_score_ppm, "projection post-cap score")
        if self.post_cap_score_ppm > self.raw_score_ppm:
            raise ValueError("projection error cap increased a score")
        if (
            type(self.applied_error_types) is not tuple
            or any(
                not isinstance(item, LearnerErrorTypeV1)
                or item in AMBIGUITY_ERROR_TYPES_V1
                for item in self.applied_error_types
            )
            or self.applied_error_types
            != tuple(sorted(set(self.applied_error_types), key=lambda item: item.value))
        ):
            raise ValueError("projection applied errors are invalid")
        if self.base_weight_ppm != MODE_BASE_WEIGHT_PPM_V1[self.mode]:
            raise ValueError("projection base weight differs from its mode")
        _integer(self.age_attempts, "projection attempt age")
        if self.recency_factor_ppm != recency_factor_ppm_v1(self.age_attempts):
            raise ValueError("projection recency factor differs")
        if self.effective_weight_ppm != mul_ppm_v1(
            self.base_weight_ppm,
            self.recency_factor_ppm,
        ):
            raise ValueError("projection effective weight differs")
        _digest(self.scenario_semantic_sha256, "projection scenario digest")
        if not isinstance(self.volume_band, ProjectionDiversityBandV1) or not isinstance(
            self.liquidity_band,
            ProjectionDiversityBandV1,
        ):
            raise TypeError("projection diversity band is invalid")
        if not isinstance(self.source_class, EvidenceSourceClassV1):
            raise TypeError("projection source class is invalid")
        _utc(self.study_timestamp_utc, "projection study timestamp provenance")
        _integer(self.simulation_time_us, "projection simulation-time provenance")

    @property
    def sort_key(self) -> tuple[int, bytes, bytes, bytes]:
        return (
            self.attempt_ordinal,
            self.assessment_id.encode("utf-8"),
            self.evidence_id.encode("utf-8"),
            self.skill_id.encode("utf-8"),
        )

    def reference_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "attempt_ordinal": self.attempt_ordinal,
            "evidence_id": self.evidence_id,
            "post_cap_score_ppm": self.post_cap_score_ppm,
        }

    def model_values_dict(self) -> dict[str, object]:
        return {
            "age_attempts": self.age_attempts,
            "applied_error_types": [item.value for item in self.applied_error_types],
            "attempt_ordinal": self.attempt_ordinal,
            "base_weight_ppm": self.base_weight_ppm,
            "effective_weight_ppm": self.effective_weight_ppm,
            "liquidity_band": self.liquidity_band.value,
            "mode": self.mode.value,
            "post_cap_score_ppm": self.post_cap_score_ppm,
            "raw_score_ppm": self.raw_score_ppm,
            "recency_factor_ppm": self.recency_factor_ppm,
            "scenario_semantic_sha256": self.scenario_semantic_sha256,
            "source_class": self.source_class.value,
            "volume_band": self.volume_band.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.model_values_dict(),
            "assessment_id": self.assessment_id,
            "evidence_id": self.evidence_id,
            "simulation_time_us": self.simulation_time_us,
            "skill_id": self.skill_id,
            "study_timestamp_utc": self.study_timestamp_utc,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ProjectionEvidenceObservationV1:
        expected = {
            "age_attempts",
            "applied_error_types",
            "assessment_id",
            "attempt_ordinal",
            "base_weight_ppm",
            "effective_weight_ppm",
            "evidence_id",
            "liquidity_band",
            "mode",
            "post_cap_score_ppm",
            "raw_score_ppm",
            "recency_factor_ppm",
            "scenario_semantic_sha256",
            "simulation_time_us",
            "skill_id",
            "source_class",
            "study_timestamp_utc",
            "volume_band",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("projection observation fields differ")
        raw_errors = payload["applied_error_types"]
        if not isinstance(raw_errors, list):
            raise TypeError("projection observation errors must be an array")
        return cls(
            skill_id=_text(payload["skill_id"], "projection skill ID"),
            assessment_id=_text(payload["assessment_id"], "projection assessment ID"),
            evidence_id=_text(payload["evidence_id"], "projection evidence ID"),
            attempt_ordinal=_integer(payload["attempt_ordinal"], "attempt ordinal", minimum=1),
            mode=AttemptModeV1(_text(payload["mode"], "projection mode")),
            raw_score_ppm=_ppm(payload["raw_score_ppm"], "projection raw score"),
            post_cap_score_ppm=_ppm(
                payload["post_cap_score_ppm"],
                "projection post-cap score",
            ),
            applied_error_types=tuple(
                LearnerErrorTypeV1(_text(item, "projection applied error"))
                for item in raw_errors
            ),
            base_weight_ppm=_ppm(payload["base_weight_ppm"], "projection base weight"),
            age_attempts=_integer(payload["age_attempts"], "projection age"),
            recency_factor_ppm=_ppm(
                payload["recency_factor_ppm"],
                "projection recency factor",
            ),
            effective_weight_ppm=_ppm(
                payload["effective_weight_ppm"],
                "projection effective weight",
            ),
            scenario_semantic_sha256=_digest(
                payload["scenario_semantic_sha256"],
                "projection scenario digest",
            ),
            volume_band=ProjectionDiversityBandV1(
                _text(payload["volume_band"], "projection volume band")
            ),
            liquidity_band=ProjectionDiversityBandV1(
                _text(payload["liquidity_band"], "projection liquidity band")
            ),
            source_class=EvidenceSourceClassV1(
                _text(payload["source_class"], "projection source class")
            ),
            study_timestamp_utc=_utc(
                payload["study_timestamp_utc"],
                "projection study timestamp",
            ),
            simulation_time_us=_integer(
                payload["simulation_time_us"],
                "projection simulation time",
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectionEvidenceReferenceV1:
    assessment_id: str
    evidence_id: str
    attempt_ordinal: int
    post_cap_score_ppm: int

    def __post_init__(self) -> None:
        _text(self.assessment_id, "projected evidence-reference assessment ID")
        _text(self.evidence_id, "projected evidence-reference evidence ID")
        _integer(self.attempt_ordinal, "projected evidence-reference ordinal", minimum=1)
        _ppm(self.post_cap_score_ppm, "projected evidence-reference score")

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "attempt_ordinal": self.attempt_ordinal,
            "evidence_id": self.evidence_id,
            "post_cap_score_ppm": self.post_cap_score_ppm,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ProjectionEvidenceReferenceV1:
        if not isinstance(payload, dict) or set(payload) != {
            "assessment_id",
            "attempt_ordinal",
            "evidence_id",
            "post_cap_score_ppm",
        }:
            raise ValueError("projected evidence reference fields differ")
        return cls(
            assessment_id=_text(payload["assessment_id"], "projected assessment ID"),
            evidence_id=_text(payload["evidence_id"], "projected evidence ID"),
            attempt_ordinal=_integer(payload["attempt_ordinal"], "projected ordinal", minimum=1),
            post_cap_score_ppm=_ppm(
                payload["post_cap_score_ppm"],
                "projected post-cap score",
            ),
        )


@dataclass(frozen=True, slots=True)
class DemonstratedOutcomeV1:
    kind: DemonstratedOutcomeKindV1
    reference: ProjectionEvidenceReferenceV1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DemonstratedOutcomeKindV1) or not isinstance(
            self.reference,
            ProjectionEvidenceReferenceV1,
        ):
            raise TypeError("demonstrated outcome is invalid")
        if (
            self.kind is DemonstratedOutcomeKindV1.SUCCESS
            and self.reference.post_cap_score_ppm < SUCCESS_SCORE_MIN_PPM_V1
        ) or (
            self.kind is DemonstratedOutcomeKindV1.FAILURE
            and self.reference.post_cap_score_ppm > FAILURE_SCORE_MAX_PPM_V1
        ):
            raise ValueError("demonstrated outcome score differs from its threshold")

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "reference": self.reference.as_dict()}

    @classmethod
    def from_dict(cls, payload: object) -> DemonstratedOutcomeV1:
        if not isinstance(payload, dict) or set(payload) != {"kind", "reference"}:
            raise ValueError("demonstrated outcome fields differ")
        return cls(
            DemonstratedOutcomeKindV1(_text(payload["kind"], "outcome kind")),
            ProjectionEvidenceReferenceV1.from_dict(payload["reference"]),
        )


@dataclass(frozen=True, slots=True)
class ObservedEvidenceCountsV1:
    total_rows: int
    opportunity_present_rows: int
    observable_rows: int
    ambiguous_rows: int
    positive_weight_rows: int
    zero_weight_rows: int
    demonstrated_success_rows: int
    demonstrated_failure_rows: int

    def __post_init__(self) -> None:
        values = tuple(
            _integer(value, "observed evidence count")
            for value in (
                self.total_rows,
                self.opportunity_present_rows,
                self.observable_rows,
                self.ambiguous_rows,
                self.positive_weight_rows,
                self.zero_weight_rows,
                self.demonstrated_success_rows,
                self.demonstrated_failure_rows,
            )
        )
        if (
            self.opportunity_present_rows > self.total_rows
            or self.observable_rows > self.total_rows
            or self.ambiguous_rows > self.total_rows
            or self.positive_weight_rows > min(
                self.opportunity_present_rows,
                self.observable_rows,
            )
            or self.zero_weight_rows != self.total_rows - self.positive_weight_rows
            or self.demonstrated_success_rows > self.positive_weight_rows
            or self.demonstrated_failure_rows > self.positive_weight_rows
            or len(values) != 8
        ):
            raise ValueError("observed evidence counts are inconsistent")

    def as_dict(self) -> dict[str, object]:
        return {
            "ambiguous_rows": self.ambiguous_rows,
            "demonstrated_failure_rows": self.demonstrated_failure_rows,
            "demonstrated_success_rows": self.demonstrated_success_rows,
            "observable_rows": self.observable_rows,
            "opportunity_present_rows": self.opportunity_present_rows,
            "positive_weight_rows": self.positive_weight_rows,
            "total_rows": self.total_rows,
            "zero_weight_rows": self.zero_weight_rows,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ObservedEvidenceCountsV1:
        expected = {
            "ambiguous_rows",
            "demonstrated_failure_rows",
            "demonstrated_success_rows",
            "observable_rows",
            "opportunity_present_rows",
            "positive_weight_rows",
            "total_rows",
            "zero_weight_rows",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("observed evidence count fields differ")
        return cls(**{key: _integer(payload[key], key) for key in expected})


@dataclass(frozen=True, slots=True)
class SkillProjectionV1:
    skill_id: str
    mastery_ppm: int
    confidence_ppm: int
    uncertainty_ppm: int
    attempt_count: int
    effective_weight_ppm: int
    weighted_score_sum: int
    model_evidence_score_ppm: int | None
    evidence_confidence_ppm: int
    diversity_confidence_ppm: int
    scenario_diversity_count: int
    volume_band_diversity: tuple[ProjectionDiversityBandV1, ...]
    liquidity_band_diversity: tuple[ProjectionDiversityBandV1, ...]
    source_class_diversity: tuple[EvidenceSourceClassV1, ...]
    recent_attempt_history: tuple[ProjectionEvidenceObservationV1, ...]
    last_opportunity: ProjectionEvidenceReferenceV1 | None
    last_opportunity_age_attempts: int | None
    last_demonstrated_success: DemonstratedOutcomeV1 | None
    last_demonstrated_failure: DemonstratedOutcomeV1 | None
    known_error_types: tuple[LearnerErrorTypeV1, ...]
    observed_counts: ObservedEvidenceCountsV1
    sufficiency: ProjectionSufficiencyV1
    label: ProjectedSkillLabelV1
    recommendation_eligible: bool

    def __post_init__(self) -> None:
        require_stable_skill_v1(self.skill_id)
        _ppm(self.mastery_ppm, "projected mastery")
        _ppm(self.confidence_ppm, "projected confidence")
        _ppm(self.uncertainty_ppm, "projected uncertainty")
        if self.uncertainty_ppm != POLICY_SCALE_V1 - self.confidence_ppm:
            raise ValueError("projection uncertainty differs from confidence")
        _integer(self.attempt_count, "projected attempt count")
        _integer(self.effective_weight_ppm, "projected effective weight")
        _integer(self.weighted_score_sum, "projected weighted score sum")
        _optional_ppm(self.model_evidence_score_ppm, "model evidence score")
        _ppm(self.evidence_confidence_ppm, "evidence confidence")
        _ppm(self.diversity_confidence_ppm, "diversity confidence")
        if self.confidence_ppm != min(
            self.evidence_confidence_ppm,
            self.diversity_confidence_ppm,
        ):
            raise ValueError("projection confidence differs from its components")
        if self.evidence_confidence_ppm != min(
            POLICY_SCALE_V1,
            round_div_even_v1(
                self.effective_weight_ppm,
                EVIDENCE_CONFIDENCE_DIVISOR_V1,
            ),
        ):
            raise ValueError("projection evidence confidence differs")
        _integer(self.scenario_diversity_count, "scenario diversity count")
        for values, enum_type, label in (
            (self.volume_band_diversity, ProjectionDiversityBandV1, "volume bands"),
            (
                self.liquidity_band_diversity,
                ProjectionDiversityBandV1,
                "liquidity bands",
            ),
            (self.source_class_diversity, EvidenceSourceClassV1, "source classes"),
        ):
            if (
                type(values) is not tuple
                or any(not isinstance(item, enum_type) for item in values)
                or values != tuple(sorted(set(values), key=lambda item: item.value))
            ):
                raise ValueError(f"projection {label} are invalid")
        if (
            type(self.recent_attempt_history) is not tuple
            or len(self.recent_attempt_history) > RECENT_HISTORY_LIMIT_V1
            or any(
                not isinstance(item, ProjectionEvidenceObservationV1)
                or item.skill_id != self.skill_id
                or item.effective_weight_ppm <= 0
                for item in self.recent_attempt_history
            )
            or self.recent_attempt_history
            != tuple(sorted(self.recent_attempt_history, key=lambda item: item.sort_key))
        ):
            raise ValueError("projection recent history is invalid")
        if not isinstance(self.observed_counts, ObservedEvidenceCountsV1):
            raise TypeError("projection observed counts are invalid")
        if (
            self.attempt_count != self.observed_counts.positive_weight_rows
            or (self.model_evidence_score_ppm is None) != (self.attempt_count == 0)
            or len(self.recent_attempt_history)
            != min(self.attempt_count, RECENT_HISTORY_LIMIT_V1)
        ):
            raise ValueError("projection attempt/evidence counts differ")
        if self.attempt_count == 0:
            if self.effective_weight_ppm != 0 or self.weighted_score_sum != 0:
                raise ValueError("empty projection carries weighted evidence")
        elif (
            self.effective_weight_ppm <= 0
            or self.model_evidence_score_ppm
            != round_div_even_v1(
                self.weighted_score_sum,
                self.effective_weight_ppm,
            )
        ):
            raise ValueError("projection model evidence score differs")
        if self.mastery_ppm != round_div_even_v1(
            2 * POLICY_SCALE_V1 * POLICY_SCALE_V1 + self.weighted_score_sum,
            4 * POLICY_SCALE_V1 + self.effective_weight_ppm,
        ):
            raise ValueError("projection mastery differs from the V1 equation")
        expected_diversity_confidence = round_div_even_v1(
            sum(
                (
                    unsigned_share_ppm_v1(
                        min(self.scenario_diversity_count, 4),
                        4,
                    ),
                    unsigned_share_ppm_v1(
                        min(len(self.volume_band_diversity), 3),
                        3,
                    ),
                    unsigned_share_ppm_v1(
                        min(len(self.liquidity_band_diversity), 3),
                        3,
                    ),
                    unsigned_share_ppm_v1(
                        min(len(self.source_class_diversity), 2),
                        2,
                    ),
                )
            ),
            4,
        )
        if self.diversity_confidence_ppm != expected_diversity_confidence:
            raise ValueError("projection diversity confidence differs")
        _optional_integer(
            self.last_opportunity_age_attempts,
            "last-opportunity age",
        )
        if self.attempt_count == 0:
            if (
                self.last_opportunity is not None
                or self.last_opportunity_age_attempts is not None
                or self.recent_attempt_history
            ):
                raise ValueError("empty projection claims a last opportunity")
        else:
            if (
                not isinstance(self.last_opportunity, ProjectionEvidenceReferenceV1)
                or self.last_opportunity_age_attempts is None
                or not self.recent_attempt_history
                or self.last_opportunity.as_dict()
                != self.recent_attempt_history[-1].reference_dict()
                or self.last_opportunity_age_attempts
                != self.recent_attempt_history[-1].age_attempts
            ):
                raise ValueError("projection last opportunity differs from history")
        for outcome, kind in (
            (self.last_demonstrated_success, DemonstratedOutcomeKindV1.SUCCESS),
            (self.last_demonstrated_failure, DemonstratedOutcomeKindV1.FAILURE),
        ):
            if outcome is not None and (
                not isinstance(outcome, DemonstratedOutcomeV1) or outcome.kind is not kind
            ):
                raise ValueError("projection demonstrated outcome is invalid")
        if (
            (self.last_demonstrated_success is None)
            != (self.observed_counts.demonstrated_success_rows == 0)
            or (self.last_demonstrated_failure is None)
            != (self.observed_counts.demonstrated_failure_rows == 0)
        ):
            raise ValueError("projection latest outcomes differ from observed counts")
        if (
            type(self.known_error_types) is not tuple
            or any(
                not isinstance(item, LearnerErrorTypeV1)
                or item in AMBIGUITY_ERROR_TYPES_V1
                for item in self.known_error_types
            )
            or self.known_error_types
            != tuple(sorted(set(self.known_error_types), key=lambda item: item.value))
        ):
            raise ValueError("projection known error types are invalid")
        expected_sufficient = (
            self.attempt_count >= SUFFICIENT_OPPORTUNITY_COUNT_V1
            and self.effective_weight_ppm >= SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1
            and self.scenario_diversity_count >= SUFFICIENT_SCENARIO_COUNT_V1
            and len(self.volume_band_diversity)
            >= SUFFICIENT_VOLUME_BAND_COUNT_V1
            and len(self.liquidity_band_diversity)
            >= SUFFICIENT_LIQUIDITY_BAND_COUNT_V1
            and self.confidence_ppm >= SUFFICIENT_CONFIDENCE_PPM_V1
        )
        if not isinstance(self.sufficiency, ProjectionSufficiencyV1):
            raise TypeError("projection sufficiency is invalid")
        if (self.sufficiency is ProjectionSufficiencyV1.SUFFICIENT) != expected_sufficient:
            raise ValueError("projection sufficiency differs from the V1 policy")
        if type(self.recommendation_eligible) is not bool or (
            self.recommendation_eligible != expected_sufficient
        ):
            raise ValueError("projection recommendation eligibility differs")
        if not isinstance(self.label, ProjectedSkillLabelV1):
            raise TypeError("projected skill label is invalid")
        expected_label = (
            ProjectedSkillLabelV1.INSUFFICIENT
            if not expected_sufficient
            else (
                ProjectedSkillLabelV1.NEEDS_WORK
                if self.mastery_ppm < NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1
                else (
                    ProjectedSkillLabelV1.DEVELOPING
                    if self.mastery_ppm < DEVELOPING_MAX_EXCLUSIVE_PPM_V1
                    else ProjectedSkillLabelV1.STRONG
                )
            )
        )
        if self.label is not expected_label:
            raise ValueError("projected skill label differs from the V1 policy")

    def model_values_dict(self) -> dict[str, object]:
        def outcome_values(
            outcome: DemonstratedOutcomeV1 | None,
        ) -> dict[str, object] | None:
            if outcome is None:
                return None
            return {
                "attempt_ordinal": outcome.reference.attempt_ordinal,
                "kind": outcome.kind.value,
                "post_cap_score_ppm": outcome.reference.post_cap_score_ppm,
            }

        return {
            "attempt_count": self.attempt_count,
            "confidence_ppm": self.confidence_ppm,
            "diversity_confidence_ppm": self.diversity_confidence_ppm,
            "effective_weight_ppm": self.effective_weight_ppm,
            "evidence_confidence_ppm": self.evidence_confidence_ppm,
            "known_error_types": [item.value for item in self.known_error_types],
            "label": self.label.value,
            "last_demonstrated_failure": outcome_values(
                self.last_demonstrated_failure
            ),
            "last_demonstrated_success": outcome_values(
                self.last_demonstrated_success
            ),
            "last_opportunity_age_attempts": self.last_opportunity_age_attempts,
            "liquidity_band_diversity": [
                item.value for item in self.liquidity_band_diversity
            ],
            "mastery_ppm": self.mastery_ppm,
            "model_evidence_score_ppm": self.model_evidence_score_ppm,
            "observed_counts": self.observed_counts.as_dict(),
            "recent_attempt_history": [
                item.model_values_dict() for item in self.recent_attempt_history
            ],
            "recommendation_eligible": self.recommendation_eligible,
            "scenario_diversity_count": self.scenario_diversity_count,
            "skill_id": self.skill_id,
            "source_class_diversity": [
                item.value for item in self.source_class_diversity
            ],
            "sufficiency": self.sufficiency.value,
            "uncertainty_ppm": self.uncertainty_ppm,
            "volume_band_diversity": [item.value for item in self.volume_band_diversity],
            "weighted_score_sum": self.weighted_score_sum,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.model_values_dict(),
            "last_demonstrated_failure": (
                None
                if self.last_demonstrated_failure is None
                else self.last_demonstrated_failure.as_dict()
            ),
            "last_demonstrated_success": (
                None
                if self.last_demonstrated_success is None
                else self.last_demonstrated_success.as_dict()
            ),
            "last_opportunity": (
                None if self.last_opportunity is None else self.last_opportunity.as_dict()
            ),
            "recent_attempt_history": [
                item.as_dict() for item in self.recent_attempt_history
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> SkillProjectionV1:
        expected = {
            "attempt_count",
            "confidence_ppm",
            "diversity_confidence_ppm",
            "effective_weight_ppm",
            "evidence_confidence_ppm",
            "known_error_types",
            "label",
            "last_demonstrated_failure",
            "last_demonstrated_success",
            "last_opportunity",
            "last_opportunity_age_attempts",
            "liquidity_band_diversity",
            "mastery_ppm",
            "model_evidence_score_ppm",
            "observed_counts",
            "recent_attempt_history",
            "recommendation_eligible",
            "scenario_diversity_count",
            "skill_id",
            "source_class_diversity",
            "sufficiency",
            "uncertainty_ppm",
            "volume_band_diversity",
            "weighted_score_sum",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("skill projection fields differ")
        arrays = tuple(
            payload[key]
            for key in (
                "known_error_types",
                "liquidity_band_diversity",
                "recent_attempt_history",
                "source_class_diversity",
                "volume_band_diversity",
            )
        )
        if any(not isinstance(item, list) for item in arrays):
            raise TypeError("skill projection arrays are invalid")
        last_opportunity = payload["last_opportunity"]
        last_success = payload["last_demonstrated_success"]
        last_failure = payload["last_demonstrated_failure"]
        return cls(
            skill_id=_text(payload["skill_id"], "projected skill ID"),
            mastery_ppm=_ppm(payload["mastery_ppm"], "projected mastery"),
            confidence_ppm=_ppm(payload["confidence_ppm"], "projected confidence"),
            uncertainty_ppm=_ppm(payload["uncertainty_ppm"], "projected uncertainty"),
            attempt_count=_integer(payload["attempt_count"], "projected attempts"),
            effective_weight_ppm=_integer(
                payload["effective_weight_ppm"],
                "projected effective weight",
            ),
            weighted_score_sum=_integer(
                payload["weighted_score_sum"],
                "projected weighted score sum",
            ),
            model_evidence_score_ppm=_optional_ppm(
                payload["model_evidence_score_ppm"],
                "model evidence score",
            ),
            evidence_confidence_ppm=_ppm(
                payload["evidence_confidence_ppm"],
                "evidence confidence",
            ),
            diversity_confidence_ppm=_ppm(
                payload["diversity_confidence_ppm"],
                "diversity confidence",
            ),
            scenario_diversity_count=_integer(
                payload["scenario_diversity_count"],
                "scenario diversity count",
            ),
            volume_band_diversity=tuple(
                ProjectionDiversityBandV1(_text(item, "volume band"))
                for item in payload["volume_band_diversity"]
            ),
            liquidity_band_diversity=tuple(
                ProjectionDiversityBandV1(_text(item, "liquidity band"))
                for item in payload["liquidity_band_diversity"]
            ),
            source_class_diversity=tuple(
                EvidenceSourceClassV1(_text(item, "source class"))
                for item in payload["source_class_diversity"]
            ),
            recent_attempt_history=tuple(
                ProjectionEvidenceObservationV1.from_dict(item)
                for item in payload["recent_attempt_history"]
            ),
            last_opportunity=(
                None
                if last_opportunity is None
                else ProjectionEvidenceReferenceV1.from_dict(last_opportunity)
            ),
            last_opportunity_age_attempts=_optional_integer(
                payload["last_opportunity_age_attempts"],
                "last opportunity age",
            ),
            last_demonstrated_success=(
                None
                if last_success is None
                else DemonstratedOutcomeV1.from_dict(last_success)
            ),
            last_demonstrated_failure=(
                None
                if last_failure is None
                else DemonstratedOutcomeV1.from_dict(last_failure)
            ),
            known_error_types=tuple(
                LearnerErrorTypeV1(_text(item, "known error type"))
                for item in payload["known_error_types"]
            ),
            observed_counts=ObservedEvidenceCountsV1.from_dict(
                payload["observed_counts"]
            ),
            sufficiency=ProjectionSufficiencyV1(
                _text(payload["sufficiency"], "projection sufficiency")
            ),
            label=ProjectedSkillLabelV1(
                _text(payload["label"], "projected skill label")
            ),
            recommendation_eligible=payload["recommendation_eligible"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class LearnerProjectionV1:
    learner_id: str
    as_of_attempt_ordinal: int
    input_assessment_count: int
    input_skill_evidence_count: int
    input_evidence_sha256: str
    skill_projections: tuple[SkillProjectionV1, ...]
    model_id: str = LEARNER_PROJECTION_MODEL_ID_V1
    model_policy_digest: str = LEARNER_PROJECTION_POLICY_V1.policy_digest
    model_status: str = LEARNER_PROJECTION_STATUS_V1
    schema_version: int = LEARNER_PROJECTION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.learner_id, "projected learner ID")
        _integer(self.as_of_attempt_ordinal, "projection as-of ordinal")
        _integer(self.input_assessment_count, "projection input assessment count")
        _integer(self.input_skill_evidence_count, "projection input evidence count")
        _digest(self.input_evidence_sha256, "projection input evidence digest")
        if (
            self.input_assessment_count > self.input_skill_evidence_count
            or (self.input_assessment_count == 0)
            != (self.input_skill_evidence_count == 0)
        ):
            raise ValueError("projection input counts are inconsistent")
        if (
            self.model_id != LEARNER_PROJECTION_MODEL_ID_V1
            or self.model_policy_digest != LEARNER_PROJECTION_POLICY_V1.policy_digest
            or self.model_status != LEARNER_PROJECTION_STATUS_V1
            or type(self.schema_version) is not int
            or self.schema_version != LEARNER_PROJECTION_SCHEMA_VERSION_V1
        ):
            raise ValueError("learner projection policy binding differs")
        if (
            type(self.skill_projections) is not tuple
            or any(not isinstance(item, SkillProjectionV1) for item in self.skill_projections)
            or tuple(item.skill_id for item in self.skill_projections)
            != CURRICULUM_SKILL_IDS_V1
        ):
            raise ValueError("learner projection must contain all 23 skills in order")
        if any(
            observation.attempt_ordinal > self.as_of_attempt_ordinal
            or observation.age_attempts
            != self.as_of_attempt_ordinal - observation.attempt_ordinal
            for skill in self.skill_projections
            for observation in skill.recent_attempt_history
        ):
            raise ValueError("projection observation age differs from its as-of ordinal")
        if any(
            outcome.reference.attempt_ordinal > self.as_of_attempt_ordinal
            for skill in self.skill_projections
            for outcome in (
                skill.last_demonstrated_success,
                skill.last_demonstrated_failure,
            )
            if outcome is not None
        ):
            raise ValueError("projection outcome occurs after its as-of ordinal")

    @property
    def model_values_sha256(self) -> str:
        return sha256_json(
            {
                "as_of_attempt_ordinal": self.as_of_attempt_ordinal,
                "model_id": self.model_id,
                "model_policy_digest": self.model_policy_digest,
                "schema_version": self.schema_version,
                "skills": [item.model_values_dict() for item in self.skill_projections],
            }
        )

    @property
    def projection_id(self) -> str:
        return LEARNER_PROJECTION_ID_PREFIX_V1 + sha256_json(self.as_dict())

    def skill(self, skill_id: str) -> SkillProjectionV1:
        require_stable_skill_v1(skill_id)
        return next(item for item in self.skill_projections if item.skill_id == skill_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_attempt_ordinal": self.as_of_attempt_ordinal,
            "input_assessment_count": self.input_assessment_count,
            "input_evidence_sha256": self.input_evidence_sha256,
            "input_skill_evidence_count": self.input_skill_evidence_count,
            "learner_id": self.learner_id,
            "model_id": self.model_id,
            "model_policy_digest": self.model_policy_digest,
            "model_status": self.model_status,
            "model_values_sha256": self.model_values_sha256,
            "record_kind": "LEARNER_STATE_PROJECTION_V1",
            "schema_version": self.schema_version,
            "skill_projections": [item.as_dict() for item in self.skill_projections],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> LearnerProjectionV1:
        projection = cls.from_dict(_canonical_object(raw, "learner projection"))
        if projection.canonical_bytes() != raw:
            raise ValueError("learner projection changed during restoration")
        return projection

    @classmethod
    def from_dict(cls, payload: object) -> LearnerProjectionV1:
        expected = {
            "as_of_attempt_ordinal",
            "input_assessment_count",
            "input_evidence_sha256",
            "input_skill_evidence_count",
            "learner_id",
            "model_id",
            "model_policy_digest",
            "model_status",
            "model_values_sha256",
            "record_kind",
            "schema_version",
            "skill_projections",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["record_kind"] != "LEARNER_STATE_PROJECTION_V1"
        ):
            raise ValueError("learner projection fields differ")
        raw_skills = payload["skill_projections"]
        if not isinstance(raw_skills, list):
            raise TypeError("learner projection skills must be an array")
        projection = cls(
            learner_id=_text(payload["learner_id"], "projected learner ID"),
            as_of_attempt_ordinal=_integer(
                payload["as_of_attempt_ordinal"],
                "projection as-of ordinal",
            ),
            input_assessment_count=_integer(
                payload["input_assessment_count"],
                "projection input assessment count",
            ),
            input_skill_evidence_count=_integer(
                payload["input_skill_evidence_count"],
                "projection input evidence count",
            ),
            input_evidence_sha256=_digest(
                payload["input_evidence_sha256"],
                "projection input evidence digest",
            ),
            skill_projections=tuple(
                SkillProjectionV1.from_dict(item) for item in raw_skills
            ),
            model_id=_text(payload["model_id"], "projection model ID"),
            model_policy_digest=_digest(
                payload["model_policy_digest"],
                "projection policy digest",
            ),
            model_status=_text(payload["model_status"], "projection model status"),
            schema_version=_integer(
                payload["schema_version"],
                "projection schema version",
                minimum=1,
            ),
        )
        if payload["model_values_sha256"] != projection.model_values_sha256:
            raise ValueError("learner projection model-values digest differs")
        return projection


__all__ = [
    "ASSESSMENT_BASE_WEIGHT_PPM_V1",
    "DEVELOPING_MAX_EXCLUSIVE_PPM_V1",
    "DemonstratedOutcomeKindV1",
    "DemonstratedOutcomeV1",
    "EVIDENCE_CONFIDENCE_DIVISOR_V1",
    "FAILURE_SCORE_MAX_PPM_V1",
    "GUIDED_BASE_WEIGHT_PPM_V1",
    "LEARNER_PROJECTION_MODEL_ID_V1",
    "LEARNER_PROJECTION_POLICY_V1",
    "LEARNER_PROJECTION_SCHEMA_VERSION_V1",
    "LEARNER_PROJECTION_STATUS_V1",
    "LearnerProjectionPolicyV1",
    "LearnerProjectionV1",
    "MODE_BASE_WEIGHT_PPM_V1",
    "NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1",
    "ObservedEvidenceCountsV1",
    "PRACTICE_BASE_WEIGHT_PPM_V1",
    "ProjectedSkillLabelV1",
    "ProjectionDiversityBandV1",
    "ProjectionEvidenceObservationV1",
    "ProjectionEvidenceReferenceV1",
    "ProjectionSufficiencyV1",
    "RECENT_HISTORY_LIMIT_V1",
    "RECENCY_AGE_SLOPE_PPM_V1",
    "REMEDIATION_BASE_WEIGHT_PPM_V1",
    "SUCCESS_SCORE_MIN_PPM_V1",
    "SUFFICIENT_CONFIDENCE_PPM_V1",
    "SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1",
    "SUFFICIENT_LIQUIDITY_BAND_COUNT_V1",
    "SUFFICIENT_OPPORTUNITY_COUNT_V1",
    "SUFFICIENT_SCENARIO_COUNT_V1",
    "SUFFICIENT_VOLUME_BAND_COUNT_V1",
    "SkillProjectionV1",
    "mul_ppm_v1",
    "projection_diversity_band_v1",
    "recency_factor_ppm_v1",
    "round_div_even_v1",
    "unsigned_share_ppm_v1",
]
