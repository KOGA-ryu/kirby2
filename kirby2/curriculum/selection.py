"""Deterministic, explainable WO34-C curriculum selection.

The selector consumes immutable evidence and a learner-state projection.  It does
not update either.  Every decision records the complete candidate inventory,
exclusions, prerequisite checks, cooldown state, ranking components, tie digest,
manual-plan disposition, and the projection evidence references used to explain the
recommendation.  The learner-state values remain explicitly unvalidated estimates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .catalog import load_curriculum
from .errors import REMEDIATION_ERROR_PRIORITY_V1, mapped_skill_for_error_v1
from .evidence import (
    POLICY_SCALE_V1,
    AttemptAssessmentV1,
    EvidenceSourceClassV1,
    LearnerEvidenceLedgerV1,
)
from .learner import build_learner_projection_v1
from .models import CurriculumDrill, CurriculumLesson, CurriculumMode
from .plans import CurriculumPlanV1, NOT_APPLICABLE_V1
from .projections import (
    LearnerProjectionV1,
    ProjectionDiversityBandV1,
    ProjectionSufficiencyV1,
    SkillProjectionV1,
    mul_ppm_v1,
    projection_diversity_band_v1,
    round_div_even_v1,
)
from .skills import (
    SKILL_GRAPH_V1,
    canonical_json_bytes,
    require_stable_skill_v1,
    sha256_json,
)


CURRICULUM_SELECTION_SCHEMA_VERSION_V1 = 1
CURRICULUM_SELECTION_POLICY_ID_V1 = "CURRICULUM_SELECTION_POLICY_V1"
CURRICULUM_SELECTION_MODEL_STATUS_V1 = "UNVALIDATED_FOR_LEARNING_OUTCOMES"

ADAPTIVE_CURRICULUM_MODES_V1 = (
    CurriculumMode.GUIDED,
    CurriculumMode.PRACTICE,
    CurriculumMode.ASSESSMENT,
    CurriculumMode.REMEDIATION,
)

SELECTION_COMPONENT_WEIGHTS_PPM_V1 = MappingProxyType(
    {
        "weakness": 250_000,
        "uncertainty": 150_000,
        "prerequisite_readiness": 100_000,
        "recency_need": 100_000,
        "recent_variety_need": 100_000,
        "difficulty_progression": 100_000,
        "scenario_diversity_need": 60_000,
        "volume_diversity_need": 50_000,
        "liquidity_diversity_need": 50_000,
        "source_balance_need": 40_000,
    }
)

SELECTION_COOLDOWN_WINDOWS_V1 = MappingProxyType(
    {
        "lesson_digest": 5,
        "parameter_digest": 4,
        "scenario_seed": 4,
        "visible_queue_shape": 3,
        "symbol": 2,
        "regime_parameter": 2,
    }
)

_DIVERSITY_CAPS_V1 = MappingProxyType(
    {
        "scenario": 4,
        "volume": 3,
        "liquidity": 3,
        "source": 2,
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be nonempty exact text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an exact integer >= {minimum}")
    return value


def _ppm(value: object, label: str) -> int:
    selected = _integer(value, label)
    if selected > POLICY_SCALE_V1:
        raise ValueError(f"{label} must be in [0,S]")
    return selected


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _digest_or_not_applicable(value: object, label: str) -> str:
    if value == NOT_APPLICABLE_V1:
        return NOT_APPLICABLE_V1
    return _digest(value, label)


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact bool")
    return value


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


def _adaptive_mode(value: object, label: str = "adaptive curriculum mode") -> CurriculumMode:
    mode = value if isinstance(value, CurriculumMode) else CurriculumMode.parse(_text(value, label))
    if mode not in ADAPTIVE_CURRICULUM_MODES_V1:
        raise ValueError(f"{label} must be GUIDED, PRACTICE, ASSESSMENT, or REMEDIATION")
    return mode


def projection_digest_v1(projection: LearnerProjectionV1) -> str:
    if not isinstance(projection, LearnerProjectionV1):
        raise TypeError("selection projection is invalid")
    return sha256_json(projection.as_dict())


class SemanticValueStateV1(str, Enum):
    CONCRETE = "CONCRETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class SelectionSemanticValueV1:
    """A cooldown dimension that cannot confuse absence with inapplicability."""

    state: SemanticValueStateV1
    value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SemanticValueStateV1):
            raise TypeError("selection semantic value state is invalid")
        if self.state is SemanticValueStateV1.CONCRETE:
            _text(self.value, "selection semantic value")
        elif self.value is not None:
            raise ValueError("non-concrete selection semantic value must not carry data")

    @classmethod
    def concrete(cls, value: str) -> SelectionSemanticValueV1:
        return cls(SemanticValueStateV1.CONCRETE, value)

    @classmethod
    def not_applicable(cls) -> SelectionSemanticValueV1:
        return cls(SemanticValueStateV1.NOT_APPLICABLE)

    @classmethod
    def missing(cls) -> SelectionSemanticValueV1:
        return cls(SemanticValueStateV1.MISSING)

    def cooldown_matches(self, other: SelectionSemanticValueV1) -> bool:
        if not isinstance(other, SelectionSemanticValueV1):
            raise TypeError("cooldown comparison requires typed semantic values")
        return (
            self.state is SemanticValueStateV1.CONCRETE
            and other.state is SemanticValueStateV1.CONCRETE
            and self.value == other.value
        )

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state.value, "value": self.value}

    @classmethod
    def from_dict(cls, payload: object) -> SelectionSemanticValueV1:
        if not isinstance(payload, dict) or set(payload) != {"state", "value"}:
            raise ValueError("selection semantic value fields differ")
        value = payload["value"]
        if value is not None and type(value) is not str:
            raise TypeError("selection semantic value must be exact text or null")
        return cls(
            SemanticValueStateV1(_text(payload["state"], "semantic value state")),
            value,
        )


def _lesson_semantic_dict_v1(lesson: CurriculumLesson) -> dict[str, object]:
    if not isinstance(lesson, CurriculumLesson):
        raise TypeError("lesson semantic digest requires a curriculum lesson")
    return {
        "duration_seconds": list(lesson.duration_seconds),
        "learning_objective": lesson.learning_objective,
        "lesson_id": lesson.lesson_id,
        "liquidities": [item.value for item in lesson.liquidities],
        "player_objective": lesson.player_objective.as_dict(),
        "post_session_explanation": lesson.post_session_explanation,
        "primary_skill_id": lesson.primary_skill_id,
        "scenario_name": lesson.scenario_name,
        "seed_pool": list(lesson.seed_pool),
        "supporting_skill_ids": list(lesson.supporting_skill_ids),
        "title": lesson.title,
        "volumes": [item.value for item in lesson.volumes],
    }


def lesson_semantic_digest_v1(lesson: CurriculumLesson) -> str:
    return sha256_json(_lesson_semantic_dict_v1(lesson))


@dataclass(frozen=True, slots=True)
class CurriculumDrillCandidateV1:
    """One fully parameterized drill plus admission and anti-memorization metadata."""

    drill: CurriculumDrill
    lesson_digest: str
    parameter_digest: str
    scenario_semantic_digest: str
    scenario_seed: SelectionSemanticValueV1
    visible_queue_shape: SelectionSemanticValueV1
    symbol: SelectionSemanticValueV1
    regime_parameter: SelectionSemanticValueV1
    volume_band: ProjectionDiversityBandV1
    liquidity_band: ProjectionDiversityBandV1
    source_class: EvidenceSourceClassV1
    difficulty_ppm: int
    capability_eligible: bool = True
    consent_eligible: bool = True
    assignment_eligible: bool = True
    mode_eligible: bool = True
    observability_eligible: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.drill, CurriculumDrill):
            raise TypeError("curriculum candidate drill is invalid")
        _adaptive_mode(self.drill.mode, "candidate drill mode")
        _digest(self.lesson_digest, "candidate lesson digest")
        _digest(self.parameter_digest, "candidate parameter digest")
        _digest(self.scenario_semantic_digest, "candidate scenario digest")
        for value, label in (
            (self.scenario_seed, "scenario seed"),
            (self.visible_queue_shape, "visible queue shape"),
            (self.symbol, "symbol"),
            (self.regime_parameter, "regime parameter"),
        ):
            if not isinstance(value, SelectionSemanticValueV1):
                raise TypeError(f"candidate {label} metadata is invalid")
        if (
            self.scenario_seed.state is SemanticValueStateV1.CONCRETE
            and self.scenario_seed.value != str(self.drill.scenario_seed)
        ):
            raise ValueError("candidate scenario seed differs from its drill")
        if not isinstance(self.volume_band, ProjectionDiversityBandV1) or not isinstance(
            self.liquidity_band,
            ProjectionDiversityBandV1,
        ):
            raise TypeError("candidate diversity band is invalid")
        if not isinstance(self.source_class, EvidenceSourceClassV1):
            raise TypeError("candidate source class is invalid")
        _ppm(self.difficulty_ppm, "candidate difficulty")
        for value, label in (
            (self.capability_eligible, "candidate capability eligibility"),
            (self.consent_eligible, "candidate consent eligibility"),
            (self.assignment_eligible, "candidate assignment eligibility"),
            (self.mode_eligible, "candidate mode eligibility"),
            (self.observability_eligible, "candidate observability eligibility"),
        ):
            _bool(value, label)

    @property
    def metadata_complete(self) -> bool:
        return all(
            value.state is not SemanticValueStateV1.MISSING
            for value in (
                self.scenario_seed,
                self.visible_queue_shape,
                self.symbol,
                self.regime_parameter,
            )
        )

    @property
    def candidate_digest(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def candidate_id(self) -> str:
        return "curriculum-candidate-" + self.candidate_digest

    @property
    def sort_key(self) -> tuple[bytes, ...]:
        return (
            self.drill.primary_skill_id.encode("utf-8"),
            self.lesson_digest.encode("ascii"),
            self.parameter_digest.encode("ascii"),
            self.scenario_seed.state.value.encode("ascii"),
            (self.scenario_seed.value or "").encode("utf-8"),
            self.candidate_id.encode("ascii"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "admission": {
                "assignment_eligible": self.assignment_eligible,
                "capability_eligible": self.capability_eligible,
                "consent_eligible": self.consent_eligible,
                "mode_eligible": self.mode_eligible,
                "observability_eligible": self.observability_eligible,
            },
            "difficulty_ppm": self.difficulty_ppm,
            "drill": self.drill.as_dict(),
            "lesson_digest": self.lesson_digest,
            "liquidity_band": self.liquidity_band.value,
            "parameter_digest": self.parameter_digest,
            "regime_parameter": self.regime_parameter.as_dict(),
            "scenario_seed": self.scenario_seed.as_dict(),
            "scenario_semantic_digest": self.scenario_semantic_digest,
            "source_class": self.source_class.value,
            "symbol": self.symbol.as_dict(),
            "visible_queue_shape": self.visible_queue_shape.as_dict(),
            "volume_band": self.volume_band.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumDrillCandidateV1:
        expected = {
            "admission",
            "difficulty_ppm",
            "drill",
            "lesson_digest",
            "liquidity_band",
            "parameter_digest",
            "regime_parameter",
            "scenario_seed",
            "scenario_semantic_digest",
            "source_class",
            "symbol",
            "visible_queue_shape",
            "volume_band",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("curriculum candidate fields differ")
        admission = payload["admission"]
        if not isinstance(admission, dict) or set(admission) != {
            "assignment_eligible",
            "capability_eligible",
            "consent_eligible",
            "mode_eligible",
            "observability_eligible",
        }:
            raise ValueError("curriculum candidate admission fields differ")
        drill_payload = payload["drill"]
        if not isinstance(drill_payload, dict):
            raise TypeError("curriculum candidate drill must be an object")
        return cls(
            drill=CurriculumDrill.from_dict(drill_payload),
            lesson_digest=_digest(payload["lesson_digest"], "candidate lesson digest"),
            parameter_digest=_digest(
                payload["parameter_digest"],
                "candidate parameter digest",
            ),
            scenario_semantic_digest=_digest(
                payload["scenario_semantic_digest"],
                "candidate scenario digest",
            ),
            scenario_seed=SelectionSemanticValueV1.from_dict(payload["scenario_seed"]),
            visible_queue_shape=SelectionSemanticValueV1.from_dict(
                payload["visible_queue_shape"]
            ),
            symbol=SelectionSemanticValueV1.from_dict(payload["symbol"]),
            regime_parameter=SelectionSemanticValueV1.from_dict(
                payload["regime_parameter"]
            ),
            volume_band=ProjectionDiversityBandV1(
                _text(payload["volume_band"], "candidate volume band")
            ),
            liquidity_band=ProjectionDiversityBandV1(
                _text(payload["liquidity_band"], "candidate liquidity band")
            ),
            source_class=EvidenceSourceClassV1(
                _text(payload["source_class"], "candidate source class")
            ),
            difficulty_ppm=_ppm(payload["difficulty_ppm"], "candidate difficulty"),
            capability_eligible=_bool(
                admission["capability_eligible"],
                "candidate capability eligibility",
            ),
            consent_eligible=_bool(
                admission["consent_eligible"],
                "candidate consent eligibility",
            ),
            assignment_eligible=_bool(
                admission["assignment_eligible"],
                "candidate assignment eligibility",
            ),
            mode_eligible=_bool(
                admission["mode_eligible"],
                "candidate mode eligibility",
            ),
            observability_eligible=_bool(
                admission["observability_eligible"],
                "candidate observability eligibility",
            ),
        )


@dataclass(frozen=True, slots=True)
class CurriculumCandidateCatalogV1:
    mode: CurriculumMode
    candidates: tuple[CurriculumDrillCandidateV1, ...]
    catalog_version: int = 1
    schema_version: int = CURRICULUM_SELECTION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _adaptive_mode(self.mode, "candidate catalog mode")
        if (
            type(self.candidates) is not tuple
            or not self.candidates
            or any(not isinstance(item, CurriculumDrillCandidateV1) for item in self.candidates)
        ):
            raise ValueError("candidate catalog requires typed candidates")
        if any(item.drill.mode is not self.mode for item in self.candidates):
            raise ValueError("candidate catalog mixes curriculum modes")
        if self.candidates != tuple(sorted(self.candidates, key=lambda item: item.sort_key)):
            raise ValueError("candidate catalog is not canonically ordered")
        ids = tuple(item.candidate_id for item in self.candidates)
        if len(ids) != len(set(ids)):
            raise ValueError("candidate catalog contains duplicate semantics")
        _integer(self.catalog_version, "candidate catalog version", minimum=1)
        if self.schema_version != CURRICULUM_SELECTION_SCHEMA_VERSION_V1:
            raise ValueError("candidate catalog schema version differs")

    @property
    def catalog_digest(self) -> str:
        return sha256_json(self.as_dict())

    def candidate(self, candidate_id: str) -> CurriculumDrillCandidateV1:
        _text(candidate_id, "curriculum candidate ID")
        try:
            return next(item for item in self.candidates if item.candidate_id == candidate_id)
        except StopIteration as error:
            raise KeyError(candidate_id) from error

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [item.as_dict() for item in self.candidates],
            "catalog_version": self.catalog_version,
            "mode": self.mode.value,
            "record_kind": "CURRICULUM_CANDIDATE_CATALOG_V1",
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CurriculumCandidateCatalogV1:
        catalog = cls.from_dict(_canonical_object(raw, "curriculum candidate catalog"))
        if catalog.canonical_bytes() != raw:
            raise ValueError("candidate catalog changed during restoration")
        return catalog

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumCandidateCatalogV1:
        if not isinstance(payload, dict) or set(payload) != {
            "candidates",
            "catalog_version",
            "mode",
            "record_kind",
            "schema_version",
        } or payload["record_kind"] != "CURRICULUM_CANDIDATE_CATALOG_V1":
            raise ValueError("candidate catalog fields differ")
        raw_candidates = payload["candidates"]
        if not isinstance(raw_candidates, list):
            raise TypeError("candidate catalog candidates must be an array")
        return cls(
            mode=_adaptive_mode(payload["mode"], "candidate catalog mode"),
            candidates=tuple(
                CurriculumDrillCandidateV1.from_dict(item) for item in raw_candidates
            ),
            catalog_version=_integer(
                payload["catalog_version"],
                "candidate catalog version",
                minimum=1,
            ),
            schema_version=_integer(
                payload["schema_version"],
                "candidate catalog schema version",
                minimum=1,
            ),
        )


_VOLUME_MULTIPLIER_PPM_V1 = {
    "0.25x": 250_000,
    "0.50x": 500_000,
    "1.00x": 1_000_000,
    "2.00x": 2_000_000,
    "5.00x": 5_000_000,
    "10.00x": 10_000_000,
}
_LIQUIDITY_BAND_V1 = {
    "VERY_THIN": ProjectionDiversityBandV1.LOW,
    "THIN": ProjectionDiversityBandV1.LOW,
    "NORMAL": ProjectionDiversityBandV1.NORMAL,
    "DEEP": ProjectionDiversityBandV1.HIGH,
    "VERY_DEEP": ProjectionDiversityBandV1.HIGH,
}


def _legacy_candidate_v1(
    lesson: CurriculumLesson,
    mode: CurriculumMode,
    variation_seed: int,
) -> CurriculumDrillCandidateV1:
    drill = lesson.prepare(mode, variation_seed)
    lesson_digest = lesson_semantic_digest_v1(lesson)
    parameter_digest = sha256_json(
        {
            "duration_seconds": drill.duration_seconds,
            "liquidity": drill.liquidity.value,
            "objective": drill.player_objective.as_dict(),
            "scenario_name": drill.scenario_name,
            "volume": drill.volume.value,
        }
    )
    scenario_digest = sha256_json(
        {"scenario_name": drill.scenario_name, "semantic_version": 1}
    )
    queue_shape = sha256_json(
        {
            "liquidity": drill.liquidity.value,
            "scenario_name": drill.scenario_name,
            "visible_queue_schema": 1,
        }
    )
    regime = sha256_json(
        {"regime_parameterization": drill.scenario_name, "schema_version": 1}
    )
    difficulty = 200_000 + round_div_even_v1(
        (int(lesson.lesson_id) - 1) * 600_000,
        13,
    )
    return CurriculumDrillCandidateV1(
        drill=drill,
        lesson_digest=lesson_digest,
        parameter_digest=parameter_digest,
        scenario_semantic_digest=scenario_digest,
        scenario_seed=SelectionSemanticValueV1.concrete(str(drill.scenario_seed)),
        visible_queue_shape=SelectionSemanticValueV1.concrete(queue_shape),
        symbol=SelectionSemanticValueV1.not_applicable(),
        regime_parameter=SelectionSemanticValueV1.concrete(regime),
        volume_band=projection_diversity_band_v1(
            _VOLUME_MULTIPLIER_PPM_V1[drill.volume.value]
        ),
        liquidity_band=_LIQUIDITY_BAND_V1[drill.liquidity.value],
        source_class=EvidenceSourceClassV1.SYNTHETIC,
        difficulty_ppm=difficulty,
    )


def build_legacy_candidate_catalog_v1(
    mode: CurriculumMode,
    *,
    variation_seed_count: int = 32,
) -> CurriculumCandidateCatalogV1:
    """Expand the legacy catalog into a deterministic semantic candidate catalog."""

    selected_mode = _adaptive_mode(mode)
    count = _integer(
        variation_seed_count,
        "legacy variation seed count",
        minimum=1,
    )
    by_semantics: dict[tuple[str, str, str, str, str], CurriculumDrillCandidateV1] = {}
    for lesson in load_curriculum().values():
        for variation_seed in range(count):
            candidate = _legacy_candidate_v1(lesson, selected_mode, variation_seed)
            key = (
                candidate.lesson_digest,
                candidate.parameter_digest,
                candidate.scenario_seed.value or "",
                candidate.visible_queue_shape.value or "",
                candidate.regime_parameter.value or "",
            )
            by_semantics.setdefault(key, candidate)
    return CurriculumCandidateCatalogV1(
        selected_mode,
        tuple(sorted(by_semantics.values(), key=lambda item: item.sort_key)),
    )


@dataclass(frozen=True, slots=True)
class SelectionHistoryEntryV1:
    """Immutable semantic sidecar for one positive-weight opportunity attempt."""

    assessment_id: str
    attempt_ordinal: int
    lesson_digest: str
    primary_skill_id: str
    parameter_digest: str
    scenario_semantic_digest: str
    scenario_seed: SelectionSemanticValueV1
    visible_queue_shape: SelectionSemanticValueV1
    symbol: SelectionSemanticValueV1
    regime_parameter: SelectionSemanticValueV1
    volume_band: ProjectionDiversityBandV1
    liquidity_band: ProjectionDiversityBandV1
    source_class: EvidenceSourceClassV1

    def __post_init__(self) -> None:
        _text(self.assessment_id, "selection-history assessment ID")
        _integer(self.attempt_ordinal, "selection-history attempt ordinal", minimum=1)
        _digest(self.lesson_digest, "selection-history lesson digest")
        require_stable_skill_v1(self.primary_skill_id)
        _digest(self.parameter_digest, "selection-history parameter digest")
        _digest(self.scenario_semantic_digest, "selection-history scenario digest")
        for item in (
            self.scenario_seed,
            self.visible_queue_shape,
            self.symbol,
            self.regime_parameter,
        ):
            if not isinstance(item, SelectionSemanticValueV1):
                raise TypeError("selection-history semantic metadata is invalid")
            if item.state is SemanticValueStateV1.MISSING:
                raise ValueError("positive selection history cannot omit required metadata")
        if not isinstance(self.volume_band, ProjectionDiversityBandV1) or not isinstance(
            self.liquidity_band,
            ProjectionDiversityBandV1,
        ):
            raise TypeError("selection-history diversity band is invalid")
        if not isinstance(self.source_class, EvidenceSourceClassV1):
            raise TypeError("selection-history source class is invalid")

    @property
    def history_digest(self) -> str:
        return sha256_json(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "attempt_ordinal": self.attempt_ordinal,
            "lesson_digest": self.lesson_digest,
            "liquidity_band": self.liquidity_band.value,
            "parameter_digest": self.parameter_digest,
            "primary_skill_id": self.primary_skill_id,
            "regime_parameter": self.regime_parameter.as_dict(),
            "scenario_seed": self.scenario_seed.as_dict(),
            "scenario_semantic_digest": self.scenario_semantic_digest,
            "source_class": self.source_class.value,
            "symbol": self.symbol.as_dict(),
            "visible_queue_shape": self.visible_queue_shape.as_dict(),
            "volume_band": self.volume_band.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SelectionHistoryEntryV1:
        expected = {
            "assessment_id",
            "attempt_ordinal",
            "lesson_digest",
            "liquidity_band",
            "parameter_digest",
            "primary_skill_id",
            "regime_parameter",
            "scenario_seed",
            "scenario_semantic_digest",
            "source_class",
            "symbol",
            "visible_queue_shape",
            "volume_band",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("selection-history fields differ")
        return cls(
            assessment_id=_text(payload["assessment_id"], "history assessment ID"),
            attempt_ordinal=_integer(
                payload["attempt_ordinal"],
                "history attempt ordinal",
                minimum=1,
            ),
            lesson_digest=_digest(payload["lesson_digest"], "history lesson digest"),
            primary_skill_id=_text(payload["primary_skill_id"], "history skill ID"),
            parameter_digest=_digest(
                payload["parameter_digest"],
                "history parameter digest",
            ),
            scenario_semantic_digest=_digest(
                payload["scenario_semantic_digest"],
                "history scenario digest",
            ),
            scenario_seed=SelectionSemanticValueV1.from_dict(payload["scenario_seed"]),
            visible_queue_shape=SelectionSemanticValueV1.from_dict(
                payload["visible_queue_shape"]
            ),
            symbol=SelectionSemanticValueV1.from_dict(payload["symbol"]),
            regime_parameter=SelectionSemanticValueV1.from_dict(
                payload["regime_parameter"]
            ),
            volume_band=ProjectionDiversityBandV1(
                _text(payload["volume_band"], "history volume band")
            ),
            liquidity_band=ProjectionDiversityBandV1(
                _text(payload["liquidity_band"], "history liquidity band")
            ),
            source_class=EvidenceSourceClassV1(
                _text(payload["source_class"], "history source class")
            ),
        )


def selection_history_entry_v1(
    assessment: AttemptAssessmentV1,
    candidate: CurriculumDrillCandidateV1,
) -> SelectionHistoryEntryV1:
    """Bind a completed assessment to its selected candidate semantics."""

    if not isinstance(assessment, AttemptAssessmentV1) or not isinstance(
        candidate,
        CurriculumDrillCandidateV1,
    ):
        raise TypeError("selection history requires an assessment and candidate")
    if (
        assessment.lesson_digest != candidate.lesson_digest
        or assessment.primary_skill_id != candidate.drill.primary_skill_id
    ):
        raise ValueError("assessment and selected candidate identity differ")
    context = assessment.observable_context
    if context.scenario_semantic_sha256 != candidate.scenario_semantic_digest:
        raise ValueError("assessment and candidate scenario semantics differ")
    volume_band = projection_diversity_band_v1(context.volume_multiplier_ppm)
    liquidity_band = projection_diversity_band_v1(context.liquidity_multiplier_ppm)
    if (
        volume_band is not candidate.volume_band
        or liquidity_band is not candidate.liquidity_band
        or context.source_class is not candidate.source_class
    ):
        raise ValueError("assessment and candidate diversity semantics differ")
    return SelectionHistoryEntryV1(
        assessment_id=assessment.assessment_id,
        attempt_ordinal=assessment.attempt_ordinal,
        lesson_digest=candidate.lesson_digest,
        primary_skill_id=candidate.drill.primary_skill_id,
        parameter_digest=candidate.parameter_digest,
        scenario_semantic_digest=candidate.scenario_semantic_digest,
        scenario_seed=candidate.scenario_seed,
        visible_queue_shape=candidate.visible_queue_shape,
        symbol=candidate.symbol,
        regime_parameter=candidate.regime_parameter,
        volume_band=candidate.volume_band,
        liquidity_band=candidate.liquidity_band,
        source_class=candidate.source_class,
    )


@dataclass(frozen=True, slots=True)
class CurriculumSelectionRequestV1:
    projection_digest: str
    selection_ordinal: int
    root_seed: int
    mode: CurriculumMode
    catalog_digest: str
    plan_assignment_digest: str
    as_of_attempt_ordinal: int

    def __post_init__(self) -> None:
        _digest(self.projection_digest, "selection projection digest")
        _integer(self.selection_ordinal, "selection ordinal", minimum=1)
        _integer(self.root_seed, "selection root seed")
        _adaptive_mode(self.mode, "selection mode")
        _digest(self.catalog_digest, "selection catalog digest")
        _digest_or_not_applicable(
            self.plan_assignment_digest,
            "selection plan/assignment digest",
        )
        _integer(self.as_of_attempt_ordinal, "selection as-of attempt ordinal")

    @property
    def tie_context(self) -> str:
        return (
            f"WO34/{self.mode.value}/{self.projection_digest}/"
            f"{self.selection_ordinal}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_attempt_ordinal": self.as_of_attempt_ordinal,
            "catalog_digest": self.catalog_digest,
            "mode": self.mode.value,
            "plan_assignment_digest": self.plan_assignment_digest,
            "projection_digest": self.projection_digest,
            "root_seed": self.root_seed,
            "selection_ordinal": self.selection_ordinal,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumSelectionRequestV1:
        if not isinstance(payload, dict) or set(payload) != {
            "as_of_attempt_ordinal",
            "catalog_digest",
            "mode",
            "plan_assignment_digest",
            "projection_digest",
            "root_seed",
            "selection_ordinal",
        }:
            raise ValueError("curriculum selection request fields differ")
        return cls(
            projection_digest=_digest(
                payload["projection_digest"],
                "request projection digest",
            ),
            selection_ordinal=_integer(
                payload["selection_ordinal"],
                "request selection ordinal",
                minimum=1,
            ),
            root_seed=_integer(payload["root_seed"], "request root seed"),
            mode=_adaptive_mode(payload["mode"], "request mode"),
            catalog_digest=_digest(payload["catalog_digest"], "request catalog digest"),
            plan_assignment_digest=_digest_or_not_applicable(
                payload["plan_assignment_digest"],
                "request plan/assignment digest",
            ),
            as_of_attempt_ordinal=_integer(
                payload["as_of_attempt_ordinal"],
                "request as-of attempt ordinal",
            ),
        )


@dataclass(frozen=True, slots=True)
class PrerequisiteCheckV1:
    prerequisite_skill_id: str
    sufficient: bool
    mastery_ppm: int
    confidence_ppm: int
    ready: bool

    def __post_init__(self) -> None:
        require_stable_skill_v1(self.prerequisite_skill_id)
        _bool(self.sufficient, "prerequisite sufficiency")
        _ppm(self.mastery_ppm, "prerequisite mastery")
        _ppm(self.confidence_ppm, "prerequisite confidence")
        expected = (
            self.sufficient
            and self.mastery_ppm >= 650_000
            and self.confidence_ppm >= 500_000
        )
        if self.ready is not expected:
            raise ValueError("prerequisite readiness differs from WO34-C thresholds")

    def as_dict(self) -> dict[str, object]:
        return {
            "confidence_ppm": self.confidence_ppm,
            "mastery_ppm": self.mastery_ppm,
            "prerequisite_skill_id": self.prerequisite_skill_id,
            "ready": self.ready,
            "sufficient": self.sufficient,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PrerequisiteCheckV1:
        if not isinstance(payload, dict) or set(payload) != {
            "confidence_ppm",
            "mastery_ppm",
            "prerequisite_skill_id",
            "ready",
            "sufficient",
        }:
            raise ValueError("prerequisite-check fields differ")
        return cls(
            prerequisite_skill_id=_text(
                payload["prerequisite_skill_id"],
                "prerequisite-check skill ID",
            ),
            sufficient=_bool(payload["sufficient"], "prerequisite sufficiency"),
            mastery_ppm=_ppm(payload["mastery_ppm"], "prerequisite mastery"),
            confidence_ppm=_ppm(
                payload["confidence_ppm"],
                "prerequisite confidence",
            ),
            ready=_bool(payload["ready"], "prerequisite readiness"),
        )


@dataclass(frozen=True, slots=True)
class SelectionRankingV1:
    weakness_ppm: int
    uncertainty_ppm: int
    prerequisite_readiness_ppm: int
    recency_need_ppm: int
    recent_variety_need_ppm: int
    difficulty_progression_ppm: int
    scenario_diversity_need_ppm: int
    volume_diversity_need_ppm: int
    liquidity_diversity_need_ppm: int
    source_balance_need_ppm: int
    target_difficulty_ppm: int
    score_ppm: int
    tie_digest: str

    def __post_init__(self) -> None:
        for value, label in self.component_items:
            _ppm(value, f"ranking {label}")
        _ppm(self.target_difficulty_ppm, "ranking target difficulty")
        _ppm(self.score_ppm, "ranking score")
        _digest(self.tie_digest, "ranking tie digest")
        expected = round_div_even_v1(
            sum(
                SELECTION_COMPONENT_WEIGHTS_PPM_V1[name] * value
                for value, name in self.component_items
            ),
            POLICY_SCALE_V1,
        )
        if self.score_ppm != expected:
            raise ValueError("selection ranking score differs from exact weights")

    @property
    def component_items(self) -> tuple[tuple[int, str], ...]:
        return (
            (self.weakness_ppm, "weakness"),
            (self.uncertainty_ppm, "uncertainty"),
            (self.prerequisite_readiness_ppm, "prerequisite_readiness"),
            (self.recency_need_ppm, "recency_need"),
            (self.recent_variety_need_ppm, "recent_variety_need"),
            (self.difficulty_progression_ppm, "difficulty_progression"),
            (self.scenario_diversity_need_ppm, "scenario_diversity_need"),
            (self.volume_diversity_need_ppm, "volume_diversity_need"),
            (self.liquidity_diversity_need_ppm, "liquidity_diversity_need"),
            (self.source_balance_need_ppm, "source_balance_need"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "components_ppm": {name: value for value, name in self.component_items},
            "score_ppm": self.score_ppm,
            "target_difficulty_ppm": self.target_difficulty_ppm,
            "tie_digest": self.tie_digest,
            "weights_ppm": dict(SELECTION_COMPONENT_WEIGHTS_PPM_V1),
        }

    @classmethod
    def from_dict(cls, payload: object) -> SelectionRankingV1:
        if not isinstance(payload, dict) or set(payload) != {
            "components_ppm",
            "score_ppm",
            "target_difficulty_ppm",
            "tie_digest",
            "weights_ppm",
        }:
            raise ValueError("selection ranking fields differ")
        components = payload["components_ppm"]
        weights = payload["weights_ppm"]
        if (
            not isinstance(components, dict)
            or set(components) != set(SELECTION_COMPONENT_WEIGHTS_PPM_V1)
            or not isinstance(weights, dict)
            or weights != dict(SELECTION_COMPONENT_WEIGHTS_PPM_V1)
        ):
            raise ValueError("selection ranking components or weights differ")
        values = {
            name: _ppm(components[name], f"ranking {name}")
            for name in SELECTION_COMPONENT_WEIGHTS_PPM_V1
        }
        return cls(
            weakness_ppm=values["weakness"],
            uncertainty_ppm=values["uncertainty"],
            prerequisite_readiness_ppm=values["prerequisite_readiness"],
            recency_need_ppm=values["recency_need"],
            recent_variety_need_ppm=values["recent_variety_need"],
            difficulty_progression_ppm=values["difficulty_progression"],
            scenario_diversity_need_ppm=values["scenario_diversity_need"],
            volume_diversity_need_ppm=values["volume_diversity_need"],
            liquidity_diversity_need_ppm=values["liquidity_diversity_need"],
            source_balance_need_ppm=values["source_balance_need"],
            target_difficulty_ppm=_ppm(
                payload["target_difficulty_ppm"],
                "ranking target difficulty",
            ),
            score_ppm=_ppm(payload["score_ppm"], "ranking score"),
            tie_digest=_digest(payload["tie_digest"], "ranking tie digest"),
        )


class CandidateExclusionV1(str, Enum):
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    CONSENT_DENIED = "CONSENT_DENIED"
    ASSIGNMENT_LOCKED = "ASSIGNMENT_LOCKED"
    MODE_MISMATCH = "MODE_MISMATCH"
    MANUAL_PLAN_OTHER_LESSON = "MANUAL_PLAN_OTHER_LESSON"
    OBSERVABILITY_MISSING = "OBSERVABILITY_MISSING"
    REQUIRED_METADATA_MISSING = "REQUIRED_METADATA_MISSING"
    PREREQUISITE_NOT_READY = "PREREQUISITE_NOT_READY"
    GUIDED_TARGET_NOT_SELECTED = "GUIDED_TARGET_NOT_SELECTED"
    REMEDIATION_TARGET_NOT_SELECTED = "REMEDIATION_TARGET_NOT_SELECTED"
    COOLDOWN_LESSON = "COOLDOWN_LESSON"
    COOLDOWN_PARAMETER = "COOLDOWN_PARAMETER"
    COOLDOWN_SEED = "COOLDOWN_SEED"
    COOLDOWN_VISIBLE_QUEUE_SHAPE = "COOLDOWN_VISIBLE_QUEUE_SHAPE"
    COOLDOWN_SYMBOL = "COOLDOWN_SYMBOL"
    COOLDOWN_REGIME_PARAMETER = "COOLDOWN_REGIME_PARAMETER"


@dataclass(frozen=True, slots=True)
class CandidateEvaluationV1:
    candidate_id: str
    lesson_digest: str
    primary_skill_id: str
    exclusions: tuple[CandidateExclusionV1, ...]
    prerequisite_checks: tuple[PrerequisiteCheckV1, ...]
    cooldown_matches: tuple[str, ...]
    ranking: SelectionRankingV1 | None

    def __post_init__(self) -> None:
        _text(self.candidate_id, "evaluated candidate ID")
        _digest(self.lesson_digest, "evaluated lesson digest")
        require_stable_skill_v1(self.primary_skill_id)
        if (
            type(self.exclusions) is not tuple
            or any(not isinstance(item, CandidateExclusionV1) for item in self.exclusions)
            or self.exclusions
            != tuple(sorted(set(self.exclusions), key=lambda item: item.value.encode("utf-8")))
        ):
            raise ValueError("candidate exclusions are not canonical")
        if (
            type(self.prerequisite_checks) is not tuple
            or any(not isinstance(item, PrerequisiteCheckV1) for item in self.prerequisite_checks)
            or self.prerequisite_checks
            != tuple(
                sorted(
                    self.prerequisite_checks,
                    key=lambda item: item.prerequisite_skill_id.encode("utf-8"),
                )
            )
        ):
            raise ValueError("candidate prerequisite checks are not canonical")
        if self.cooldown_matches != tuple(sorted(set(self.cooldown_matches))):
            raise ValueError("candidate cooldown matches are not canonical")
        if (not self.exclusions) != isinstance(self.ranking, SelectionRankingV1):
            raise ValueError("only eligible candidates may carry a ranking")

    @property
    def eligible(self) -> bool:
        return not self.exclusions

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "cooldown_matches": list(self.cooldown_matches),
            "eligible": self.eligible,
            "exclusions": [item.value for item in self.exclusions],
            "lesson_digest": self.lesson_digest,
            "prerequisite_checks": [item.as_dict() for item in self.prerequisite_checks],
            "primary_skill_id": self.primary_skill_id,
            "ranking": None if self.ranking is None else self.ranking.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> CandidateEvaluationV1:
        if not isinstance(payload, dict) or set(payload) != {
            "candidate_id",
            "cooldown_matches",
            "eligible",
            "exclusions",
            "lesson_digest",
            "prerequisite_checks",
            "primary_skill_id",
            "ranking",
        }:
            raise ValueError("candidate-evaluation fields differ")
        raw_cooldowns = payload["cooldown_matches"]
        raw_exclusions = payload["exclusions"]
        raw_checks = payload["prerequisite_checks"]
        raw_ranking = payload["ranking"]
        if (
            not isinstance(raw_cooldowns, list)
            or any(type(item) is not str for item in raw_cooldowns)
            or not isinstance(raw_exclusions, list)
            or any(type(item) is not str for item in raw_exclusions)
            or not isinstance(raw_checks, list)
            or (raw_ranking is not None and not isinstance(raw_ranking, dict))
        ):
            raise TypeError("candidate-evaluation nested records are invalid")
        evaluation = cls(
            candidate_id=_text(payload["candidate_id"], "evaluated candidate ID"),
            lesson_digest=_digest(
                payload["lesson_digest"],
                "evaluated lesson digest",
            ),
            primary_skill_id=_text(
                payload["primary_skill_id"],
                "evaluated primary skill ID",
            ),
            exclusions=tuple(CandidateExclusionV1(item) for item in raw_exclusions),
            prerequisite_checks=tuple(
                PrerequisiteCheckV1.from_dict(item) for item in raw_checks
            ),
            cooldown_matches=tuple(raw_cooldowns),
            ranking=(
                None
                if raw_ranking is None
                else SelectionRankingV1.from_dict(raw_ranking)
            ),
        )
        if _bool(payload["eligible"], "candidate eligibility") is not evaluation.eligible:
            raise ValueError("candidate eligibility projection differs")
        return evaluation


class ManualPlanStatusV1(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    APPLIED = "APPLIED"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class ManualPlanResolutionV1:
    status: ManualPlanStatusV1
    plan_digests: tuple[str, ...]
    selected_entry_ordinal: int | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, ManualPlanStatusV1):
            raise TypeError("manual plan resolution status is invalid")
        if (
            type(self.plan_digests) is not tuple
            or any(_SHA256.fullmatch(item) is None for item in self.plan_digests)
            or self.plan_digests != tuple(sorted(set(self.plan_digests)))
        ):
            raise ValueError("manual plan resolution digests are not canonical")
        if self.selected_entry_ordinal is not None:
            _integer(self.selected_entry_ordinal, "manual plan entry ordinal", minimum=1)
        _text(self.reason, "manual plan resolution reason")
        if self.status is ManualPlanStatusV1.NOT_APPLICABLE and (
            self.plan_digests or self.selected_entry_ordinal is not None
        ):
            raise ValueError("not-applicable manual plan carries plan state")

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_digests": list(self.plan_digests),
            "reason": self.reason,
            "selected_entry_ordinal": self.selected_entry_ordinal,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ManualPlanResolutionV1:
        if not isinstance(payload, dict) or set(payload) != {
            "plan_digests",
            "reason",
            "selected_entry_ordinal",
            "status",
        }:
            raise ValueError("manual-plan resolution fields differ")
        raw_digests = payload["plan_digests"]
        raw_ordinal = payload["selected_entry_ordinal"]
        if not isinstance(raw_digests, list) or any(
            type(item) is not str for item in raw_digests
        ):
            raise TypeError("manual-plan resolution digests must be an array")
        return cls(
            status=ManualPlanStatusV1(
                _text(payload["status"], "manual-plan status")
            ),
            plan_digests=tuple(raw_digests),
            selected_entry_ordinal=(
                None
                if raw_ordinal is None
                else _integer(raw_ordinal, "manual-plan entry ordinal", minimum=1)
            ),
            reason=_text(payload["reason"], "manual-plan reason"),
        )


class CurriculumSelectionStatusV1(str, Enum):
    SELECTED = "SELECTED"
    NO_ELIGIBLE_DRILL = "NO_ELIGIBLE_DRILL"
    MANUAL_PLAN_REFUSED = "MANUAL_PLAN_REFUSED"


@dataclass(frozen=True, slots=True)
class SelectionExplanationV1:
    selected_skill_id: str | None
    mastery_ppm: int | None
    confidence_ppm: int | None
    uncertainty_ppm: int | None
    sufficiency: str | None
    evidence_assessment_ids: tuple[str, ...]
    statements: tuple[str, ...]
    model_status: str = CURRICULUM_SELECTION_MODEL_STATUS_V1

    def __post_init__(self) -> None:
        if self.selected_skill_id is None:
            if any(
                item is not None
                for item in (
                    self.mastery_ppm,
                    self.confidence_ppm,
                    self.uncertainty_ppm,
                    self.sufficiency,
                )
            ):
                raise ValueError("empty selection explanation carries skill estimates")
        else:
            require_stable_skill_v1(self.selected_skill_id)
            _ppm(self.mastery_ppm, "explanation mastery")
            _ppm(self.confidence_ppm, "explanation confidence")
            _ppm(self.uncertainty_ppm, "explanation uncertainty")
            _text(self.sufficiency, "explanation sufficiency")
        if self.evidence_assessment_ids != tuple(dict.fromkeys(self.evidence_assessment_ids)):
            raise ValueError("explanation evidence references are duplicated")
        if any(type(item) is not str or not item for item in self.evidence_assessment_ids):
            raise ValueError("explanation evidence references are invalid")
        if not self.statements or any(type(item) is not str or not item for item in self.statements):
            raise ValueError("selection explanation statements are invalid")
        if self.model_status != CURRICULUM_SELECTION_MODEL_STATUS_V1:
            raise ValueError("selection explanation model status differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "confidence_ppm": self.confidence_ppm,
            "evidence_assessment_ids": list(self.evidence_assessment_ids),
            "mastery_ppm": self.mastery_ppm,
            "model_status": self.model_status,
            "selected_skill_id": self.selected_skill_id,
            "statements": list(self.statements),
            "sufficiency": self.sufficiency,
            "uncertainty_ppm": self.uncertainty_ppm,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SelectionExplanationV1:
        if not isinstance(payload, dict) or set(payload) != {
            "confidence_ppm",
            "evidence_assessment_ids",
            "mastery_ppm",
            "model_status",
            "selected_skill_id",
            "statements",
            "sufficiency",
            "uncertainty_ppm",
        }:
            raise ValueError("selection explanation fields differ")
        raw_evidence = payload["evidence_assessment_ids"]
        raw_statements = payload["statements"]
        skill_id = payload["selected_skill_id"]
        if (
            not isinstance(raw_evidence, list)
            or any(type(item) is not str for item in raw_evidence)
            or not isinstance(raw_statements, list)
            or any(type(item) is not str for item in raw_statements)
            or (skill_id is not None and type(skill_id) is not str)
        ):
            raise TypeError("selection explanation nested fields are invalid")
        if skill_id is None:
            mastery = confidence = uncertainty = sufficiency = None
        else:
            mastery = _ppm(payload["mastery_ppm"], "explanation mastery")
            confidence = _ppm(
                payload["confidence_ppm"],
                "explanation confidence",
            )
            uncertainty = _ppm(
                payload["uncertainty_ppm"],
                "explanation uncertainty",
            )
            sufficiency = _text(payload["sufficiency"], "explanation sufficiency")
        return cls(
            selected_skill_id=skill_id,
            mastery_ppm=mastery,
            confidence_ppm=confidence,
            uncertainty_ppm=uncertainty,
            sufficiency=sufficiency,
            evidence_assessment_ids=tuple(raw_evidence),
            statements=tuple(raw_statements),
            model_status=_text(payload["model_status"], "explanation model status"),
        )


@dataclass(frozen=True, slots=True)
class CurriculumSelectionRecordV1:
    request: CurriculumSelectionRequestV1
    status: CurriculumSelectionStatusV1
    target_universe: tuple[str, ...]
    cold_start: bool
    candidate_evaluations: tuple[CandidateEvaluationV1, ...]
    ranking_order: tuple[str, ...]
    selected_candidate_id: str | None
    selected_skill_id: str | None
    manual_plan: ManualPlanResolutionV1
    explanation: SelectionExplanationV1
    policy_digest: str
    reason: str
    schema_version: int = CURRICULUM_SELECTION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if not isinstance(self.request, CurriculumSelectionRequestV1):
            raise TypeError("selection record request is invalid")
        if not isinstance(self.status, CurriculumSelectionStatusV1):
            raise TypeError("selection record status is invalid")
        if self.target_universe != tuple(
            sorted(set(self.target_universe), key=lambda value: value.encode("utf-8"))
        ):
            raise ValueError("selection target universe is not canonical")
        for skill_id in self.target_universe:
            require_stable_skill_v1(skill_id)
        _bool(self.cold_start, "selection cold-start flag")
        if (
            type(self.candidate_evaluations) is not tuple
            or any(not isinstance(item, CandidateEvaluationV1) for item in self.candidate_evaluations)
        ):
            raise TypeError("selection candidate evaluations are invalid")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_evaluations)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("selection candidate evaluations are duplicated")
        eligible_ids = {
            item.candidate_id for item in self.candidate_evaluations if item.eligible
        }
        if set(self.ranking_order) != eligible_ids or len(self.ranking_order) != len(
            eligible_ids
        ):
            raise ValueError("selection ranking order differs from the eligible set")
        if self.status is CurriculumSelectionStatusV1.SELECTED:
            if (
                self.selected_candidate_id not in eligible_ids
                or self.selected_skill_id is None
            ):
                raise ValueError("selected curriculum record lacks an eligible choice")
        elif self.selected_candidate_id is not None or self.selected_skill_id is not None:
            raise ValueError("refused curriculum record carries a selected drill")
        if not isinstance(self.manual_plan, ManualPlanResolutionV1) or not isinstance(
            self.explanation,
            SelectionExplanationV1,
        ):
            raise TypeError("selection plan resolution or explanation is invalid")
        _digest(self.policy_digest, "selection policy digest")
        if self.policy_digest != CURRICULUM_SELECTION_POLICY_SHA256_V1:
            raise ValueError("selection policy binding differs")
        _text(self.reason, "selection record reason")
        if self.schema_version != CURRICULUM_SELECTION_SCHEMA_VERSION_V1:
            raise ValueError("selection record schema version differs")

    @property
    def selection_digest(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def selection_id(self) -> str:
        return "curriculum-selection-" + self.selection_digest

    @property
    def eligible_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            item.candidate_id for item in self.candidate_evaluations if item.eligible
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_evaluations": [item.as_dict() for item in self.candidate_evaluations],
            "cold_start": self.cold_start,
            "explanation": self.explanation.as_dict(),
            "manual_plan": self.manual_plan.as_dict(),
            "policy_digest": self.policy_digest,
            "ranking_order": list(self.ranking_order),
            "reason": self.reason,
            "record_kind": "CURRICULUM_SELECTION_RECORD_V1",
            "request": self.request.as_dict(),
            "schema_version": self.schema_version,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_skill_id": self.selected_skill_id,
            "status": self.status.value,
            "target_universe": list(self.target_universe),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CurriculumSelectionRecordV1:
        record = cls.from_dict(_canonical_object(raw, "curriculum selection record"))
        if record.canonical_bytes() != raw:
            raise ValueError("curriculum selection record changed during restoration")
        return record

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumSelectionRecordV1:
        expected = {
            "candidate_evaluations",
            "cold_start",
            "explanation",
            "manual_plan",
            "policy_digest",
            "ranking_order",
            "reason",
            "record_kind",
            "request",
            "schema_version",
            "selected_candidate_id",
            "selected_skill_id",
            "status",
            "target_universe",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["record_kind"] != "CURRICULUM_SELECTION_RECORD_V1"
        ):
            raise ValueError("curriculum selection record fields differ")
        raw_evaluations = payload["candidate_evaluations"]
        raw_ranking = payload["ranking_order"]
        raw_targets = payload["target_universe"]
        raw_request = payload["request"]
        raw_manual = payload["manual_plan"]
        raw_explanation = payload["explanation"]
        selected_candidate = payload["selected_candidate_id"]
        selected_skill = payload["selected_skill_id"]
        if (
            not isinstance(raw_evaluations, list)
            or not isinstance(raw_ranking, list)
            or any(type(item) is not str for item in raw_ranking)
            or not isinstance(raw_targets, list)
            or any(type(item) is not str for item in raw_targets)
            or not isinstance(raw_request, dict)
            or not isinstance(raw_manual, dict)
            or not isinstance(raw_explanation, dict)
            or (selected_candidate is not None and type(selected_candidate) is not str)
            or (selected_skill is not None and type(selected_skill) is not str)
        ):
            raise TypeError("curriculum selection record nested fields are invalid")
        return cls(
            request=CurriculumSelectionRequestV1.from_dict(raw_request),
            status=CurriculumSelectionStatusV1(
                _text(payload["status"], "selection status")
            ),
            target_universe=tuple(raw_targets),
            cold_start=_bool(payload["cold_start"], "selection cold-start flag"),
            candidate_evaluations=tuple(
                CandidateEvaluationV1.from_dict(item) for item in raw_evaluations
            ),
            ranking_order=tuple(raw_ranking),
            selected_candidate_id=selected_candidate,
            selected_skill_id=selected_skill,
            manual_plan=ManualPlanResolutionV1.from_dict(raw_manual),
            explanation=SelectionExplanationV1.from_dict(raw_explanation),
            policy_digest=_digest(
                payload["policy_digest"],
                "selection policy digest",
            ),
            reason=_text(payload["reason"], "selection reason"),
            schema_version=_integer(
                payload["schema_version"],
                "selection schema version",
                minimum=1,
            ),
        )


def _selection_policy_dict_v1() -> dict[str, object]:
    return {
        "assessment": {
            "batch_size": 8,
            "critical_error_record_max": 1,
            "distinct_skill_representatives": 4,
            "max_drills_per_skill": 2,
            "pass_score_ppm": 700_000,
        },
        "cold_start": "ALL_TARGETS_INSUFFICIENT_THEN_NO_UNSATISFIED_PREREQUISITE",
        "cooldown_attempt_windows": dict(SELECTION_COOLDOWN_WINDOWS_V1),
        "diversity_caps": dict(_DIVERSITY_CAPS_V1),
        "mode_order": [item.value for item in ADAPTIVE_CURRICULUM_MODES_V1],
        "model_status": CURRICULUM_SELECTION_MODEL_STATUS_V1,
        "policy_id": CURRICULUM_SELECTION_POLICY_ID_V1,
        "policy_scale_ppm": POLICY_SCALE_V1,
        "prerequisite_ready": {
            "confidence_min_ppm": 500_000,
            "mastery_min_ppm": 650_000,
            "sufficient_required": True,
        },
        "ranking_weights_ppm": dict(SELECTION_COMPONENT_WEIGHTS_PPM_V1),
        "schema_version": CURRICULUM_SELECTION_SCHEMA_VERSION_V1,
        "tie_context": "WO34/<mode>/<projection_digest>/<selection_ordinal>",
    }


CURRICULUM_SELECTION_POLICY_SHA256_V1 = sha256_json(_selection_policy_dict_v1())


def selection_policy_v1() -> dict[str, object]:
    return _selection_policy_dict_v1()


def _positive_assessments_v1(
    ledger: LearnerEvidenceLedgerV1,
    as_of_attempt_ordinal: int,
) -> tuple[AttemptAssessmentV1, ...]:
    return tuple(
        assessment
        for assessment in ledger.assessments
        if assessment.attempt_ordinal <= as_of_attempt_ordinal
        and assessment.projection_weight_eligible_skill_evidence
    )


def _validate_inputs_v1(
    request: CurriculumSelectionRequestV1,
    projection: LearnerProjectionV1,
    ledger: LearnerEvidenceLedgerV1,
    catalog: CurriculumCandidateCatalogV1,
    history: tuple[SelectionHistoryEntryV1, ...],
) -> tuple[AttemptAssessmentV1, ...]:
    if not isinstance(request, CurriculumSelectionRequestV1):
        raise TypeError("curriculum selection request is invalid")
    if not isinstance(projection, LearnerProjectionV1):
        raise TypeError("curriculum selection projection is invalid")
    if not isinstance(ledger, LearnerEvidenceLedgerV1):
        raise TypeError("curriculum selection evidence ledger is invalid")
    if not isinstance(catalog, CurriculumCandidateCatalogV1):
        raise TypeError("curriculum selection catalog is invalid")
    if type(history) is not tuple or any(
        not isinstance(item, SelectionHistoryEntryV1) for item in history
    ):
        raise TypeError("curriculum selection history must be an immutable typed tuple")
    if projection.learner_id != ledger.learner_id:
        raise ValueError("selection projection and ledger learner differ")
    if request.projection_digest != projection_digest_v1(projection):
        raise ValueError("selection request projection digest differs")
    if request.catalog_digest != catalog.catalog_digest:
        raise ValueError("selection request catalog digest differs")
    if request.mode is not catalog.mode:
        raise ValueError("selection request and catalog mode differ")
    if request.as_of_attempt_ordinal != projection.as_of_attempt_ordinal:
        raise ValueError("selection request and projection as-of ordinal differ")
    prefix = LearnerEvidenceLedgerV1(
        ledger.learner_id,
        tuple(
            item
            for item in ledger.assessments
            if item.attempt_ordinal <= request.as_of_attempt_ordinal
        ),
    )
    rebuilt = build_learner_projection_v1(
        prefix,
        as_of_attempt_ordinal=request.as_of_attempt_ordinal,
    )
    if rebuilt.canonical_bytes() != projection.canonical_bytes():
        raise ValueError("selection projection is not the deterministic ledger projection")
    positive = _positive_assessments_v1(ledger, request.as_of_attempt_ordinal)
    expected_ids = tuple(item.assessment_id for item in positive)
    history_ids = tuple(item.assessment_id for item in history)
    if history != tuple(sorted(history, key=lambda item: item.attempt_ordinal)):
        raise ValueError("selection history is not ordered by attempt ordinal")
    if len(history_ids) != len(set(history_ids)) or history_ids != expected_ids:
        raise ValueError("selection history does not cover the positive attempt prefix")
    for assessment, sidecar in zip(positive, history, strict=True):
        context = assessment.observable_context
        if (
            sidecar.attempt_ordinal != assessment.attempt_ordinal
            or sidecar.lesson_digest != assessment.lesson_digest
            or sidecar.primary_skill_id != assessment.primary_skill_id
            or sidecar.scenario_semantic_digest != context.scenario_semantic_sha256
            or sidecar.volume_band
            is not projection_diversity_band_v1(context.volume_multiplier_ppm)
            or sidecar.liquidity_band
            is not projection_diversity_band_v1(context.liquidity_multiplier_ppm)
            or sidecar.source_class is not context.source_class
        ):
            raise ValueError("selection-history sidecar differs from evidence")
    return positive


def _prerequisite_checks_v1(
    skill_id: str,
    projection: LearnerProjectionV1,
) -> tuple[PrerequisiteCheckV1, ...]:
    checks: list[PrerequisiteCheckV1] = []
    for prerequisite in SKILL_GRAPH_V1.prerequisites(skill_id):
        row = projection.skill(prerequisite)
        sufficient = row.sufficiency is ProjectionSufficiencyV1.SUFFICIENT
        checks.append(
            PrerequisiteCheckV1(
                prerequisite,
                sufficient,
                row.mastery_ppm,
                row.confidence_ppm,
                sufficient
                and row.mastery_ppm >= 650_000
                and row.confidence_ppm >= 500_000,
            )
        )
    return tuple(checks)


def _semantic_cooldown_match_v1(
    candidate: SelectionSemanticValueV1,
    history_value: SelectionSemanticValueV1,
) -> bool:
    return candidate.cooldown_matches(history_value)


def _cooldown_matches_v1(
    candidate: CurriculumDrillCandidateV1,
    history: tuple[SelectionHistoryEntryV1, ...],
) -> tuple[str, ...]:
    newest = tuple(reversed(history))
    matches: list[str] = []
    if any(
        item.lesson_digest == candidate.lesson_digest
        for item in newest[: SELECTION_COOLDOWN_WINDOWS_V1["lesson_digest"]]
    ):
        matches.append("lesson_digest")
    if any(
        item.parameter_digest == candidate.parameter_digest
        for item in newest[: SELECTION_COOLDOWN_WINDOWS_V1["parameter_digest"]]
    ):
        matches.append("parameter_digest")
    semantic_dimensions = (
        ("scenario_seed", candidate.scenario_seed),
        ("visible_queue_shape", candidate.visible_queue_shape),
        ("symbol", candidate.symbol),
        ("regime_parameter", candidate.regime_parameter),
    )
    for name, selected in semantic_dimensions:
        if any(
            _semantic_cooldown_match_v1(selected, getattr(item, name))
            for item in newest[: SELECTION_COOLDOWN_WINDOWS_V1[name]]
        ):
            matches.append(name)
    return tuple(sorted(matches))


_COOLDOWN_EXCLUSION_V1 = {
    "lesson_digest": CandidateExclusionV1.COOLDOWN_LESSON,
    "parameter_digest": CandidateExclusionV1.COOLDOWN_PARAMETER,
    "scenario_seed": CandidateExclusionV1.COOLDOWN_SEED,
    "visible_queue_shape": CandidateExclusionV1.COOLDOWN_VISIBLE_QUEUE_SHAPE,
    "symbol": CandidateExclusionV1.COOLDOWN_SYMBOL,
    "regime_parameter": CandidateExclusionV1.COOLDOWN_REGIME_PARAMETER,
}


def _diversity_need_v1(count: int, cap: int) -> int:
    return POLICY_SCALE_V1 - min(
        POLICY_SCALE_V1,
        round_div_even_v1(count * POLICY_SCALE_V1, cap),
    )


def _tie_digest_v1(
    request: CurriculumSelectionRequestV1,
    candidate: CurriculumDrillCandidateV1,
) -> str:
    return sha256_json(
        {
            "context": request.tie_context,
            "root_seed": request.root_seed,
            "semantic_digest": candidate.candidate_digest,
        }
    )


def _rank_candidate_v1(
    request: CurriculumSelectionRequestV1,
    candidate: CurriculumDrillCandidateV1,
    projection: LearnerProjectionV1,
    history: tuple[SelectionHistoryEntryV1, ...],
    prerequisite_checks: tuple[PrerequisiteCheckV1, ...],
) -> SelectionRankingV1:
    skill = projection.skill(candidate.drill.primary_skill_id)
    insufficient = skill.sufficiency is ProjectionSufficiencyV1.INSUFFICIENT
    weakness = 500_000 if insufficient else POLICY_SCALE_V1 - skill.mastery_ppm
    age = skill.last_opportunity_age_attempts
    recency = (
        POLICY_SCALE_V1
        if age is None
        else round_div_even_v1(min(age, 20) * POLICY_SCALE_V1, 20)
    )
    newest = tuple(reversed(history))
    target_count = sum(
        item.primary_skill_id == candidate.drill.primary_skill_id for item in newest[:8]
    )
    recent_variety = POLICY_SCALE_V1 - min(
        POLICY_SCALE_V1,
        round_div_even_v1(target_count * POLICY_SCALE_V1, 4),
    )
    recent_twelve = newest[:12]
    scenario_count = sum(
        item.scenario_semantic_digest == candidate.scenario_semantic_digest
        for item in recent_twelve
    )
    volume_count = sum(item.volume_band is candidate.volume_band for item in recent_twelve)
    liquidity_count = sum(
        item.liquidity_band is candidate.liquidity_band for item in recent_twelve
    )
    source_count = sum(item.source_class is candidate.source_class for item in recent_twelve)
    target_difficulty = 200_000 + mul_ppm_v1(600_000, skill.mastery_ppm)
    difficulty_progression = POLICY_SCALE_V1 - abs(
        candidate.difficulty_ppm - target_difficulty
    )
    prerequisite_readiness = (
        min(item.confidence_ppm for item in prerequisite_checks)
        if prerequisite_checks
        else POLICY_SCALE_V1
    )
    values = {
        "weakness": weakness,
        "uncertainty": skill.uncertainty_ppm,
        "prerequisite_readiness": prerequisite_readiness,
        "recency_need": recency,
        "recent_variety_need": recent_variety,
        "difficulty_progression": difficulty_progression,
        "scenario_diversity_need": _diversity_need_v1(
            scenario_count,
            _DIVERSITY_CAPS_V1["scenario"],
        ),
        "volume_diversity_need": _diversity_need_v1(
            volume_count,
            _DIVERSITY_CAPS_V1["volume"],
        ),
        "liquidity_diversity_need": _diversity_need_v1(
            liquidity_count,
            _DIVERSITY_CAPS_V1["liquidity"],
        ),
        "source_balance_need": _diversity_need_v1(
            source_count,
            _DIVERSITY_CAPS_V1["source"],
        ),
    }
    score = round_div_even_v1(
        sum(SELECTION_COMPONENT_WEIGHTS_PPM_V1[name] * value for name, value in values.items()),
        POLICY_SCALE_V1,
    )
    return SelectionRankingV1(
        weakness_ppm=values["weakness"],
        uncertainty_ppm=values["uncertainty"],
        prerequisite_readiness_ppm=values["prerequisite_readiness"],
        recency_need_ppm=values["recency_need"],
        recent_variety_need_ppm=values["recent_variety_need"],
        difficulty_progression_ppm=values["difficulty_progression"],
        scenario_diversity_need_ppm=values["scenario_diversity_need"],
        volume_diversity_need_ppm=values["volume_diversity_need"],
        liquidity_diversity_need_ppm=values["liquidity_diversity_need"],
        source_balance_need_ppm=values["source_balance_need"],
        target_difficulty_ppm=target_difficulty,
        score_ppm=score,
        tie_digest=_tie_digest_v1(request, candidate),
    )


def _ranking_sort_key_v1(
    evaluation: CandidateEvaluationV1,
) -> tuple[int, bytes, bytes]:
    assert evaluation.ranking is not None
    return (
        -evaluation.ranking.score_ppm,
        evaluation.ranking.tie_digest.encode("ascii"),
        evaluation.candidate_id.encode("ascii"),
    )


def _guided_skill_sort_key_v1(
    skill: SkillProjectionV1,
) -> tuple[int, int, int, int, bytes]:
    age = skill.last_opportunity_age_attempts
    return (
        skill.mastery_ppm,
        -skill.uncertainty_ppm,
        0 if age is None else 1,
        0 if age is None else -age,
        skill.skill_id.encode("utf-8"),
    )


def _manual_resolution_v1(
    plans: tuple[CurriculumPlanV1, ...],
    learner_id: str,
    request: CurriculumSelectionRequestV1,
    catalog: CurriculumCandidateCatalogV1,
) -> tuple[ManualPlanResolutionV1, CurriculumPlanV1 | None, str | None]:
    if type(plans) is not tuple or any(not isinstance(item, CurriculumPlanV1) for item in plans):
        raise TypeError("manual curriculum plans must be an immutable typed tuple")
    applicable = tuple(
        sorted(
            (
                item
                for item in plans
                if item.applies_to(learner_id, request.selection_ordinal)
            ),
            key=lambda item: item.plan_digest,
        )
    )
    digests = tuple(item.plan_digest for item in applicable)
    if not applicable:
        return (
            ManualPlanResolutionV1(
                ManualPlanStatusV1.NOT_APPLICABLE,
                (),
                None,
                "NO_APPLICABLE_MANUAL_PLAN",
            ),
            None,
            None,
        )
    if len(applicable) > 1:
        return (
            ManualPlanResolutionV1(
                ManualPlanStatusV1.REFUSED,
                digests,
                request.selection_ordinal,
                "MULTIPLE_APPLICABLE_MANUAL_PLANS",
            ),
            None,
            "MULTIPLE_APPLICABLE_MANUAL_PLANS",
        )
    plan = applicable[0]
    entry = plan.entry_for(request.selection_ordinal)
    reason: str | None = None
    if request.plan_assignment_digest != plan.plan_digest:
        reason = "REQUEST_PLAN_DIGEST_MISMATCH"
    elif plan.catalog_digest != catalog.catalog_digest:
        reason = "PLAN_CATALOG_DIGEST_MISMATCH"
    elif entry.mode is not request.mode:
        reason = "PLAN_MODE_MISMATCH"
    status = ManualPlanStatusV1.REFUSED if reason else ManualPlanStatusV1.APPLIED
    return (
        ManualPlanResolutionV1(
            status,
            (plan.plan_digest,),
            entry.selection_ordinal,
            reason or "SOLE_APPLICABLE_PLAN_VALID",
        ),
        plan,
        reason,
    )


def _remediation_target_v1(
    positive: tuple[AttemptAssessmentV1, ...],
    evaluations: tuple[CandidateEvaluationV1, ...],
) -> str | None:
    available_skills = {
        item.primary_skill_id for item in evaluations if item.eligible
    }
    priorities = {item: index for index, item in enumerate(REMEDIATION_ERROR_PRIORITY_V1)}
    for assessment in reversed(positive[-10:]):
        errors = sorted(
            (item for item in assessment.errors if item.error_type in priorities),
            key=lambda item: priorities[item.error_type],
        )
        for error in errors:
            skill_id = mapped_skill_for_error_v1(
                error.error_type,
                assessment.primary_skill_id,
            )
            if skill_id is not None and skill_id in available_skills:
                return skill_id
    return None


def _explanation_v1(
    selected_skill_id: str | None,
    projection: LearnerProjectionV1,
    status: CurriculumSelectionStatusV1,
    reason: str,
) -> SelectionExplanationV1:
    if selected_skill_id is None:
        return SelectionExplanationV1(
            None,
            None,
            None,
            None,
            None,
            (),
            (
                "No recommendation was issued because the fixed eligibility policy refused selection.",
                f"Refusal reason: {reason}.",
                "No cooldown, prerequisite, consent, capability, assignment, or assessment lock was relaxed.",
            ),
        )
    row = projection.skill(selected_skill_id)
    references = tuple(
        dict.fromkeys(
            item.assessment_id for item in reversed(row.recent_attempt_history[-4:])
        )
    )
    return SelectionExplanationV1(
        selected_skill_id,
        row.mastery_ppm,
        row.confidence_ppm,
        row.uncertainty_ppm,
        row.sufficiency.value,
        references,
        (
            "This recommendation is derived from an unvalidated learner-state estimate, not a fact about ability.",
            "The record exposes weakness, uncertainty, recency, variety, progression, diversity, and prerequisite components.",
            f"Selection outcome: {status.value}; reason: {reason}.",
        ),
    )


def select_curriculum_v1(
    request: CurriculumSelectionRequestV1,
    projection: LearnerProjectionV1,
    ledger: LearnerEvidenceLedgerV1,
    catalog: CurriculumCandidateCatalogV1,
    history: tuple[SelectionHistoryEntryV1, ...] = (),
    plans: tuple[CurriculumPlanV1, ...] = (),
) -> CurriculumSelectionRecordV1:
    """Select one drill or return a deterministic, fully explained refusal."""

    positive = _validate_inputs_v1(request, projection, ledger, catalog, history)
    manual, applicable_plan, manual_error = _manual_resolution_v1(
        plans,
        projection.learner_id,
        request,
        catalog,
    )
    plan_entry = (
        None
        if applicable_plan is None
        else applicable_plan.entry_for(request.selection_ordinal)
    )

    target_candidates = []
    for candidate in catalog.candidates:
        passes_declared_target_gates = (
            candidate.capability_eligible
            and candidate.consent_eligible
            and candidate.assignment_eligible
            and candidate.mode_eligible
            and candidate.drill.mode is request.mode
            and (plan_entry is None or candidate.lesson_digest == plan_entry.lesson_digest)
        )
        if passes_declared_target_gates:
            target_candidates.append(candidate)
    target_universe = tuple(
        sorted(
            {item.drill.primary_skill_id for item in target_candidates},
            key=lambda value: value.encode("utf-8"),
        )
    )
    cold_start = bool(target_universe) and all(
        projection.skill(skill_id).sufficiency is ProjectionSufficiencyV1.INSUFFICIENT
        for skill_id in target_universe
    )

    staged: list[
        tuple[
            CurriculumDrillCandidateV1,
            list[CandidateExclusionV1],
            tuple[PrerequisiteCheckV1, ...],
            tuple[str, ...],
        ]
    ] = []
    for candidate in catalog.candidates:
        exclusions: list[CandidateExclusionV1] = []
        if not candidate.capability_eligible:
            exclusions.append(CandidateExclusionV1.CAPABILITY_DENIED)
        if not candidate.consent_eligible:
            exclusions.append(CandidateExclusionV1.CONSENT_DENIED)
        if not candidate.assignment_eligible:
            exclusions.append(CandidateExclusionV1.ASSIGNMENT_LOCKED)
        if not candidate.mode_eligible or candidate.drill.mode is not request.mode:
            exclusions.append(CandidateExclusionV1.MODE_MISMATCH)
        if plan_entry is not None and candidate.lesson_digest != plan_entry.lesson_digest:
            exclusions.append(CandidateExclusionV1.MANUAL_PLAN_OTHER_LESSON)
        if not candidate.observability_eligible:
            exclusions.append(CandidateExclusionV1.OBSERVABILITY_MISSING)
        if not candidate.metadata_complete:
            exclusions.append(CandidateExclusionV1.REQUIRED_METADATA_MISSING)
        checks = _prerequisite_checks_v1(candidate.drill.primary_skill_id, projection)
        if any(not item.ready for item in checks):
            exclusions.append(CandidateExclusionV1.PREREQUISITE_NOT_READY)
        cooldown = _cooldown_matches_v1(candidate, history)
        exclusions.extend(_COOLDOWN_EXCLUSION_V1[item] for item in cooldown)
        staged.append((candidate, exclusions, checks, cooldown))

    # Mode-specific skill targeting is applied after the universal locks.  The
    # guided and remediation target calculations can therefore never use an
    # otherwise ineligible drill as evidence that a skill is available.
    prelim: tuple[CandidateEvaluationV1, ...] = tuple(
        CandidateEvaluationV1(
            candidate.candidate_id,
            candidate.lesson_digest,
            candidate.drill.primary_skill_id,
            tuple(sorted(set(exclusions), key=lambda item: item.value.encode("utf-8"))),
            checks,
            cooldown,
            (
                None
                if exclusions
                else _rank_candidate_v1(request, candidate, projection, history, checks)
            ),
        )
        for candidate, exclusions, checks, cooldown in staged
    )

    selected_target_skill: str | None = None
    if applicable_plan is None:
        if request.mode is CurriculumMode.GUIDED:
            available_skills = {
                item.primary_skill_id for item in prelim if item.eligible
            }
            if cold_start:
                available_skills &= set(SKILL_GRAPH_V1.roots)
            if available_skills:
                selected_target_skill = min(
                    (projection.skill(item) for item in available_skills),
                    key=_guided_skill_sort_key_v1,
                ).skill_id
        elif request.mode is CurriculumMode.REMEDIATION:
            selected_target_skill = _remediation_target_v1(positive, prelim)

    evaluations: list[CandidateEvaluationV1] = []
    for evaluation, staged_item in zip(prelim, staged, strict=True):
        candidate, exclusions, checks, cooldown = staged_item
        if applicable_plan is None and request.mode is CurriculumMode.GUIDED:
            if selected_target_skill is None or candidate.drill.primary_skill_id != selected_target_skill:
                exclusions.append(CandidateExclusionV1.GUIDED_TARGET_NOT_SELECTED)
        if applicable_plan is None and request.mode is CurriculumMode.REMEDIATION:
            if selected_target_skill is None or candidate.drill.primary_skill_id != selected_target_skill:
                exclusions.append(CandidateExclusionV1.REMEDIATION_TARGET_NOT_SELECTED)
        canonical_exclusions = tuple(
            sorted(set(exclusions), key=lambda item: item.value.encode("utf-8"))
        )
        evaluations.append(
            CandidateEvaluationV1(
                candidate.candidate_id,
                candidate.lesson_digest,
                candidate.drill.primary_skill_id,
                canonical_exclusions,
                checks,
                cooldown,
                (
                    None
                    if canonical_exclusions
                    else _rank_candidate_v1(
                        request,
                        candidate,
                        projection,
                        history,
                        checks,
                    )
                ),
            )
        )
    evaluation_tuple = tuple(evaluations)
    ranked = tuple(
        sorted(
            (item for item in evaluation_tuple if item.eligible),
            key=_ranking_sort_key_v1,
        )
    )
    ranking_order = tuple(item.candidate_id for item in ranked)

    selected: CandidateEvaluationV1 | None = None
    status = CurriculumSelectionStatusV1.NO_ELIGIBLE_DRILL
    reason = "NO_ELIGIBLE_DRILL"
    if manual_error is not None:
        status = CurriculumSelectionStatusV1.MANUAL_PLAN_REFUSED
        reason = manual_error
    elif applicable_plan is not None:
        if not ranked:
            status = CurriculumSelectionStatusV1.MANUAL_PLAN_REFUSED
            reason = "PLANNED_LESSON_INELIGIBLE_OR_EXHAUSTED"
            manual = ManualPlanResolutionV1(
                ManualPlanStatusV1.REFUSED,
                (applicable_plan.plan_digest,),
                request.selection_ordinal,
                reason,
            )
        else:
            selected = ranked[0]
            status = CurriculumSelectionStatusV1.SELECTED
            reason = "MANUAL_PLAN_PRECEDENCE"
    elif ranked:
        selected = ranked[0]
        status = CurriculumSelectionStatusV1.SELECTED
        reason = f"{request.mode.value}_POLICY_SELECTED"
    elif request.mode is CurriculumMode.REMEDIATION and selected_target_skill is None:
        reason = "NO_REMEDIATION_ERROR_WITH_ELIGIBLE_DRILL"

    selected_skill_id = None if selected is None else selected.primary_skill_id
    explanation = _explanation_v1(
        selected_skill_id,
        projection,
        status,
        reason,
    )
    return CurriculumSelectionRecordV1(
        request=request,
        status=status,
        target_universe=target_universe,
        cold_start=cold_start,
        candidate_evaluations=evaluation_tuple,
        ranking_order=ranking_order,
        selected_candidate_id=None if selected is None else selected.candidate_id,
        selected_skill_id=selected_skill_id,
        manual_plan=manual,
        explanation=explanation,
        policy_digest=CURRICULUM_SELECTION_POLICY_SHA256_V1,
        reason=reason,
    )


__all__ = [
    "ADAPTIVE_CURRICULUM_MODES_V1",
    "CURRICULUM_SELECTION_MODEL_STATUS_V1",
    "CURRICULUM_SELECTION_POLICY_ID_V1",
    "CURRICULUM_SELECTION_POLICY_SHA256_V1",
    "CURRICULUM_SELECTION_SCHEMA_VERSION_V1",
    "SELECTION_COMPONENT_WEIGHTS_PPM_V1",
    "SELECTION_COOLDOWN_WINDOWS_V1",
    "CandidateEvaluationV1",
    "CandidateExclusionV1",
    "CurriculumCandidateCatalogV1",
    "CurriculumDrillCandidateV1",
    "CurriculumSelectionRecordV1",
    "CurriculumSelectionRequestV1",
    "CurriculumSelectionStatusV1",
    "ManualPlanResolutionV1",
    "ManualPlanStatusV1",
    "PrerequisiteCheckV1",
    "SelectionExplanationV1",
    "SelectionHistoryEntryV1",
    "SelectionRankingV1",
    "SelectionSemanticValueV1",
    "SemanticValueStateV1",
    "build_legacy_candidate_catalog_v1",
    "lesson_semantic_digest_v1",
    "projection_digest_v1",
    "select_curriculum_v1",
    "selection_history_entry_v1",
    "selection_policy_v1",
]
