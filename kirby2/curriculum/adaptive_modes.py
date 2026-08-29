"""Behaviorally distinct WO34-C modes and frozen assessment contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .errors import CRITICAL_ERROR_TYPES_V1, score_cap_for_error_v1
from .evidence import AttemptAssessmentV1, AttemptModeV1, LearnerEvidenceLedgerV1
from .models import CurriculumMode
from .plans import CurriculumPlanV1
from .projections import LearnerProjectionV1, round_div_even_v1
from .selection import (
    CURRICULUM_SELECTION_POLICY_SHA256_V1,
    CurriculumCandidateCatalogV1,
    CurriculumDrillCandidateV1,
    CurriculumSelectionRecordV1,
    CurriculumSelectionRequestV1,
    CurriculumSelectionStatusV1,
    SelectionHistoryEntryV1,
    SelectionSemanticValueV1,
    SemanticValueStateV1,
    select_curriculum_v1,
)
from .skills import canonical_json_bytes, require_stable_skill_v1, sha256_json


ASSESSMENT_BATCH_SIZE_V1 = 8
ASSESSMENT_DISTINCT_SKILL_REPRESENTATIVES_V1 = 4
ASSESSMENT_MAX_DRILLS_PER_SKILL_V1 = 2
ASSESSMENT_PASS_SCORE_PPM_V1 = 700_000
ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1 = 1
ASSESSMENT_SCORING_POLICY_ID_V1 = "FROZEN_EIGHT_DRILL_ASSESSMENT_V1"
ASSESSMENT_REVEAL_POLICY_ID_V1 = "REVEAL_ONLY_AFTER_ASSESSMENT_CLOSURE_V1"


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


@dataclass(frozen=True, slots=True)
class AdaptiveModePolicyV1:
    mode: CurriculumMode
    target_policy: str
    concept_before_play: bool
    feedback_timing: str
    identity_hidden_before_closure: bool
    assistance_policy: str
    prerequisite_context_required: bool

    def __post_init__(self) -> None:
        if self.mode not in {
            CurriculumMode.GUIDED,
            CurriculumMode.PRACTICE,
            CurriculumMode.ASSESSMENT,
            CurriculumMode.REMEDIATION,
        }:
            raise ValueError("adaptive mode policy uses a legacy mode")
        for value in (
            self.target_policy,
            self.feedback_timing,
            self.assistance_policy,
        ):
            if type(value) is not str or not value:
                raise ValueError("adaptive mode policy text must not be empty")
        for value in (
            self.concept_before_play,
            self.identity_hidden_before_closure,
            self.prerequisite_context_required,
        ):
            if type(value) is not bool:
                raise TypeError("adaptive mode policy flags must be exact bools")

    def as_dict(self) -> dict[str, object]:
        return {
            "assistance_policy": self.assistance_policy,
            "concept_before_play": self.concept_before_play,
            "feedback_timing": self.feedback_timing,
            "identity_hidden_before_closure": self.identity_hidden_before_closure,
            "mode": self.mode.value,
            "prerequisite_context_required": self.prerequisite_context_required,
            "target_policy": self.target_policy,
        }


ADAPTIVE_MODE_POLICIES_V1 = MappingProxyType(
    {
        CurriculumMode.GUIDED: AdaptiveModePolicyV1(
            CurriculumMode.GUIDED,
            "ONE_LOWEST_MASTERY_HIGHEST_UNCERTAINTY_ELIGIBLE_SKILL",
            True,
            "EXPLANATION_BEFORE_PLAY_AND_DEBRIEF_AFTER",
            False,
            "DECLARED_CONCEPT_ASSISTANCE",
            True,
        ),
        CurriculumMode.PRACTICE: AdaptiveModePolicyV1(
            CurriculumMode.PRACTICE,
            "COMPLETE_ELIGIBLE_SET",
            False,
            "FEEDBACK_AFTER_ATTEMPT",
            False,
            "NO_CONCEPT_EXPLANATION_BEFORE_PLAY",
            False,
        ),
        CurriculumMode.ASSESSMENT: AdaptiveModePolicyV1(
            CurriculumMode.ASSESSMENT,
            "FROZEN_EIGHT_DRILL_BATCH",
            False,
            "REVEAL_ONLY_AFTER_BATCH_CLOSURE",
            True,
            "RESTRICTED_AND_FROZEN_BEFORE_ATTEMPT_ONE",
            False,
        ),
        CurriculumMode.REMEDIATION: AdaptiveModePolicyV1(
            CurriculumMode.REMEDIATION,
            "LATEST_TEN_ERRORS_BY_FIXED_PRIORITY",
            False,
            "DIAGNOSED_ERROR_FEEDBACK_AFTER_ATTEMPT",
            False,
            "DECLARED_ERROR_AND_PREREQUISITE_CONTEXT",
            True,
        ),
    }
)


def adaptive_mode_policy_v1(mode: CurriculumMode) -> AdaptiveModePolicyV1:
    if not isinstance(mode, CurriculumMode) or mode not in ADAPTIVE_MODE_POLICIES_V1:
        raise ValueError("adaptive mode policy requires a WO34-C mode")
    return ADAPTIVE_MODE_POLICIES_V1[mode]


@dataclass(frozen=True, slots=True)
class FrozenAssessmentDrillV1:
    assessment_position: int
    candidate_id: str
    candidate_digest: str
    lesson_digest: str
    primary_skill_id: str
    parameter_digest: str
    scenario_seed: SelectionSemanticValueV1
    visible_queue_shape: SelectionSemanticValueV1
    symbol: SelectionSemanticValueV1
    regime_parameter: SelectionSemanticValueV1

    def __post_init__(self) -> None:
        if (
            type(self.assessment_position) is not int
            or not 1 <= self.assessment_position <= ASSESSMENT_BATCH_SIZE_V1
        ):
            raise ValueError("frozen assessment position is invalid")
        if type(self.candidate_id) is not str or not self.candidate_id:
            raise ValueError("frozen assessment candidate ID is empty")
        for value, label in (
            (self.candidate_digest, "candidate"),
            (self.lesson_digest, "lesson"),
            (self.parameter_digest, "parameter"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"frozen assessment {label} digest is invalid")
        if self.candidate_id != "curriculum-candidate-" + self.candidate_digest:
            raise ValueError("frozen assessment candidate ID and digest differ")
        require_stable_skill_v1(self.primary_skill_id)
        for item in (
            self.scenario_seed,
            self.visible_queue_shape,
            self.symbol,
            self.regime_parameter,
        ):
            if not isinstance(item, SelectionSemanticValueV1):
                raise TypeError("frozen assessment semantic metadata is invalid")
            if item.state is SemanticValueStateV1.MISSING:
                raise ValueError("frozen assessment metadata cannot be missing")

    @classmethod
    def from_candidate(
        cls,
        position: int,
        candidate: CurriculumDrillCandidateV1,
    ) -> FrozenAssessmentDrillV1:
        return cls(
            position,
            candidate.candidate_id,
            candidate.candidate_digest,
            candidate.lesson_digest,
            candidate.drill.primary_skill_id,
            candidate.parameter_digest,
            candidate.scenario_seed,
            candidate.visible_queue_shape,
            candidate.symbol,
            candidate.regime_parameter,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_position": self.assessment_position,
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "lesson_digest": self.lesson_digest,
            "parameter_digest": self.parameter_digest,
            "primary_skill_id": self.primary_skill_id,
            "regime_parameter": self.regime_parameter.as_dict(),
            "scenario_seed": self.scenario_seed.as_dict(),
            "symbol": self.symbol.as_dict(),
            "visible_queue_shape": self.visible_queue_shape.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> FrozenAssessmentDrillV1:
        if not isinstance(payload, dict) or set(payload) != {
            "assessment_position",
            "candidate_digest",
            "candidate_id",
            "lesson_digest",
            "parameter_digest",
            "primary_skill_id",
            "regime_parameter",
            "scenario_seed",
            "symbol",
            "visible_queue_shape",
        }:
            raise ValueError("frozen assessment drill fields differ")
        position = payload["assessment_position"]
        if type(position) is not int:
            raise TypeError("frozen assessment position must be an exact integer")
        values = {
            key: payload[key]
            for key in (
                "candidate_digest",
                "candidate_id",
                "lesson_digest",
                "parameter_digest",
                "primary_skill_id",
            )
        }
        if any(type(item) is not str for item in values.values()):
            raise TypeError("frozen assessment drill identity must be exact text")
        return cls(
            assessment_position=position,
            candidate_id=values["candidate_id"],
            candidate_digest=values["candidate_digest"],
            lesson_digest=values["lesson_digest"],
            primary_skill_id=values["primary_skill_id"],
            parameter_digest=values["parameter_digest"],
            scenario_seed=SelectionSemanticValueV1.from_dict(payload["scenario_seed"]),
            visible_queue_shape=SelectionSemanticValueV1.from_dict(
                payload["visible_queue_shape"]
            ),
            symbol=SelectionSemanticValueV1.from_dict(payload["symbol"]),
            regime_parameter=SelectionSemanticValueV1.from_dict(
                payload["regime_parameter"]
            ),
        )


@dataclass(frozen=True, slots=True)
class FrozenAssessmentV1:
    request: CurriculumSelectionRequestV1
    preassessment_ledger_digest: str
    selection_record_digest: str
    drills: tuple[FrozenAssessmentDrillV1, ...]
    manual_plan_digest: str | None
    scoring_policy_id: str = ASSESSMENT_SCORING_POLICY_ID_V1
    reveal_policy_id: str = ASSESSMENT_REVEAL_POLICY_ID_V1
    selection_policy_digest: str = CURRICULUM_SELECTION_POLICY_SHA256_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request, CurriculumSelectionRequestV1)
            or self.request.mode is not CurriculumMode.ASSESSMENT
        ):
            raise ValueError("frozen assessment requires an assessment request")
        for value, label in (
            (self.preassessment_ledger_digest, "preassessment ledger"),
            (self.selection_record_digest, "selection record"),
            (self.selection_policy_digest, "selection policy"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"frozen assessment {label} digest is invalid")
        if self.selection_policy_digest != CURRICULUM_SELECTION_POLICY_SHA256_V1:
            raise ValueError("frozen assessment selection policy differs")
        if (
            type(self.drills) is not tuple
            or len(self.drills) != ASSESSMENT_BATCH_SIZE_V1
            or tuple(item.assessment_position for item in self.drills)
            != tuple(range(1, ASSESSMENT_BATCH_SIZE_V1 + 1))
        ):
            raise ValueError("frozen assessment must contain eight ordered drills")
        _validate_batch_constraints_v1(self.drills)
        if self.manual_plan_digest is not None and (
            len(self.manual_plan_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.manual_plan_digest)
        ):
            raise ValueError("frozen assessment manual plan digest is invalid")
        if (
            self.scoring_policy_id != ASSESSMENT_SCORING_POLICY_ID_V1
            or self.reveal_policy_id != ASSESSMENT_REVEAL_POLICY_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("frozen assessment policy metadata differs")

    @property
    def assessment_digest(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def assessment_id(self) -> str:
        return "frozen-assessment-" + self.assessment_digest

    def as_dict(self) -> dict[str, object]:
        return {
            "drills": [item.as_dict() for item in self.drills],
            "manual_plan_digest": self.manual_plan_digest,
            "preassessment_ledger_digest": self.preassessment_ledger_digest,
            "record_kind": "FROZEN_ASSESSMENT_V1",
            "request": self.request.as_dict(),
            "reveal_policy_id": self.reveal_policy_id,
            "schema_version": self.schema_version,
            "scoring_policy": {
                "batch_size": ASSESSMENT_BATCH_SIZE_V1,
                "critical_error_record_max": ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1,
                "drill_score": "PRIMARY_SKILL_POST_CAP_SCORE_PPM",
                "pass_score_ppm": ASSESSMENT_PASS_SCORE_PPM_V1,
                "policy_id": self.scoring_policy_id,
                "score": "ROUND_DIV_EVEN_SUM_EIGHT_OVER_EIGHT",
            },
            "selection_policy_digest": self.selection_policy_digest,
            "selection_record_digest": self.selection_record_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> FrozenAssessmentV1:
        frozen = cls.from_dict(_canonical_object(raw, "frozen assessment"))
        if frozen.canonical_bytes() != raw:
            raise ValueError("frozen assessment changed during restoration")
        return frozen

    @classmethod
    def from_dict(cls, payload: object) -> FrozenAssessmentV1:
        expected = {
            "drills",
            "manual_plan_digest",
            "preassessment_ledger_digest",
            "record_kind",
            "request",
            "reveal_policy_id",
            "schema_version",
            "scoring_policy",
            "selection_policy_digest",
            "selection_record_digest",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["record_kind"] != "FROZEN_ASSESSMENT_V1"
        ):
            raise ValueError("frozen assessment fields differ")
        raw_drills = payload["drills"]
        raw_request = payload["request"]
        scoring = payload["scoring_policy"]
        manual = payload["manual_plan_digest"]
        if (
            not isinstance(raw_drills, list)
            or not isinstance(raw_request, dict)
            or not isinstance(scoring, dict)
            or (manual is not None and type(manual) is not str)
        ):
            raise TypeError("frozen assessment nested records are invalid")
        expected_scoring = {
            "batch_size": ASSESSMENT_BATCH_SIZE_V1,
            "critical_error_record_max": ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1,
            "drill_score": "PRIMARY_SKILL_POST_CAP_SCORE_PPM",
            "pass_score_ppm": ASSESSMENT_PASS_SCORE_PPM_V1,
            "policy_id": ASSESSMENT_SCORING_POLICY_ID_V1,
            "score": "ROUND_DIV_EVEN_SUM_EIGHT_OVER_EIGHT",
        }
        if scoring != expected_scoring:
            raise ValueError("frozen assessment scoring policy differs")
        string_fields = {
            key: payload[key]
            for key in (
                "preassessment_ledger_digest",
                "reveal_policy_id",
                "selection_policy_digest",
                "selection_record_digest",
            )
        }
        if any(type(item) is not str for item in string_fields.values()):
            raise TypeError("frozen assessment policy binding must be exact text")
        schema = payload["schema_version"]
        if type(schema) is not int:
            raise TypeError("frozen assessment schema must be an exact integer")
        return cls(
            request=CurriculumSelectionRequestV1.from_dict(raw_request),
            preassessment_ledger_digest=string_fields[
                "preassessment_ledger_digest"
            ],
            selection_record_digest=string_fields["selection_record_digest"],
            drills=tuple(FrozenAssessmentDrillV1.from_dict(item) for item in raw_drills),
            manual_plan_digest=manual,
            scoring_policy_id=scoring["policy_id"],
            reveal_policy_id=string_fields["reveal_policy_id"],
            selection_policy_digest=string_fields["selection_policy_digest"],
            schema_version=schema,
        )

    def preclosure_view(self) -> dict[str, object]:
        """Learner-facing view; intentionally excludes all candidate identity."""

        return {
            "assessment_id": self.assessment_id,
            "assistance": "RESTRICTED",
            "drills": [
                {
                    "assessment_position": item.assessment_position,
                    "identity": "WITHHELD_UNTIL_ASSESSMENT_CLOSURE",
                }
                for item in self.drills
            ],
            "mode": CurriculumMode.ASSESSMENT.value,
            "reveal": "LOCKED_UNTIL_EIGHT_ATTEMPTS_CLOSE",
        }


class AssessmentFreezeStatusV1(str, Enum):
    FROZEN = "FROZEN"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class AssessmentFreezeResultV1:
    status: AssessmentFreezeStatusV1
    reason: str
    selection_record: CurriculumSelectionRecordV1
    frozen_assessment: FrozenAssessmentV1 | None
    applicable_plan_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, AssessmentFreezeStatusV1):
            raise TypeError("assessment freeze status is invalid")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("assessment freeze reason is empty")
        if not isinstance(self.selection_record, CurriculumSelectionRecordV1):
            raise TypeError("assessment freeze selection record is invalid")
        if (self.status is AssessmentFreezeStatusV1.FROZEN) != isinstance(
            self.frozen_assessment,
            FrozenAssessmentV1,
        ):
            raise ValueError("assessment freeze result payload differs from status")
        if self.applicable_plan_digests != tuple(
            sorted(set(self.applicable_plan_digests))
        ):
            raise ValueError("assessment freeze plan digests are not canonical")


def _concrete_key(value: SelectionSemanticValueV1) -> str | None:
    return value.value if value.state is SemanticValueStateV1.CONCRETE else None


def _candidate_conflicts_v1(
    candidate: CurriculumDrillCandidateV1,
    selected: tuple[CurriculumDrillCandidateV1, ...],
) -> bool:
    if any(item.lesson_digest == candidate.lesson_digest for item in selected):
        return True
    if any(item.parameter_digest == candidate.parameter_digest for item in selected):
        return True
    for attribute in (
        "scenario_seed",
        "visible_queue_shape",
        "symbol",
        "regime_parameter",
    ):
        value = _concrete_key(getattr(candidate, attribute))
        if value is not None and any(
            _concrete_key(getattr(item, attribute)) == value for item in selected
        ):
            return True
    return False


def _can_add_candidate_v1(
    candidate: CurriculumDrillCandidateV1,
    selected: tuple[CurriculumDrillCandidateV1, ...],
    *,
    require_new_skill: bool = False,
) -> bool:
    skill_id = candidate.drill.primary_skill_id
    skill_count = sum(item.drill.primary_skill_id == skill_id for item in selected)
    return (
        skill_count < ASSESSMENT_MAX_DRILLS_PER_SKILL_V1
        and (not require_new_skill or skill_count == 0)
        and not _candidate_conflicts_v1(candidate, selected)
    )


def _validate_batch_constraints_v1(
    drills: tuple[FrozenAssessmentDrillV1, ...],
) -> None:
    if len({item.primary_skill_id for item in drills[:4]}) != 4:
        raise ValueError("assessment first four drills must represent distinct skills")
    if any(
        sum(item.primary_skill_id == skill_id for item in drills)
        > ASSESSMENT_MAX_DRILLS_PER_SKILL_V1
        for skill_id in {item.primary_skill_id for item in drills}
    ):
        raise ValueError("assessment contains more than two drills for one skill")
    for index, item in enumerate(drills):
        earlier = drills[:index]
        if any(previous.lesson_digest == item.lesson_digest for previous in earlier):
            raise ValueError("assessment repeats a lesson digest")
        if any(previous.parameter_digest == item.parameter_digest for previous in earlier):
            raise ValueError("assessment repeats a parameter digest")
        for attribute in (
            "scenario_seed",
            "visible_queue_shape",
            "symbol",
            "regime_parameter",
        ):
            value = _concrete_key(getattr(item, attribute))
            if value is not None and any(
                _concrete_key(getattr(previous, attribute)) == value
                for previous in earlier
            ):
                raise ValueError(f"assessment repeats concrete {attribute}")


def _applicable_assessment_plan_v1(
    plans: tuple[CurriculumPlanV1, ...],
    learner_id: str,
    request: CurriculumSelectionRequestV1,
    catalog: CurriculumCandidateCatalogV1,
) -> tuple[CurriculumPlanV1 | None, tuple[str, ...], str | None]:
    if type(plans) is not tuple or any(not isinstance(item, CurriculumPlanV1) for item in plans):
        raise TypeError("assessment plans must be an immutable typed tuple")
    applicable = tuple(
        sorted(
            (item for item in plans if item.applies_to(learner_id, request.selection_ordinal)),
            key=lambda item: item.plan_digest,
        )
    )
    digests = tuple(item.plan_digest for item in applicable)
    if not applicable:
        return None, (), None
    if len(applicable) > 1:
        return None, digests, "MULTIPLE_APPLICABLE_MANUAL_PLANS"
    plan = applicable[0]
    if request.plan_assignment_digest != plan.plan_digest:
        return None, digests, "REQUEST_PLAN_DIGEST_MISMATCH"
    if plan.catalog_digest != catalog.catalog_digest:
        return None, digests, "PLAN_CATALOG_DIGEST_MISMATCH"
    end = request.selection_ordinal + ASSESSMENT_BATCH_SIZE_V1 - 1
    if end > plan.end_selection_ordinal:
        return None, digests, "PLAN_DOES_NOT_COVER_EIGHT_ASSESSMENT_POSITIONS"
    entries = tuple(plan.entry_for(ordinal) for ordinal in range(request.selection_ordinal, end + 1))
    if any(item.mode is not CurriculumMode.ASSESSMENT for item in entries):
        return None, digests, "PLAN_MODE_MISMATCH"
    return plan, digests, None


def _adaptive_assessment_batch_v1(
    ranked: tuple[CurriculumDrillCandidateV1, ...],
) -> tuple[CurriculumDrillCandidateV1, ...] | None:
    representatives: list[CurriculumDrillCandidateV1] = []
    seen_skills: set[str] = set()
    for candidate in ranked:
        skill_id = candidate.drill.primary_skill_id
        if skill_id in seen_skills:
            continue
        # Each representative is the highest-ranked still-admissible drill for
        # its skill.  Global anti-memorization locks apply to the full batch.
        if not _can_add_candidate_v1(
            candidate,
            tuple(representatives),
            require_new_skill=True,
        ):
            continue
        representatives.append(candidate)
        seen_skills.add(skill_id)
        if len(representatives) == ASSESSMENT_DISTINCT_SKILL_REPRESENTATIVES_V1:
            break
    if len(representatives) < ASSESSMENT_DISTINCT_SKILL_REPRESENTATIVES_V1:
        return None
    selected = list(representatives)
    while len(selected) < ASSESSMENT_BATCH_SIZE_V1:
        next_candidate = next(
            (
                item
                for item in ranked
                if item not in selected
                and _can_add_candidate_v1(item, tuple(selected))
            ),
            None,
        )
        if next_candidate is None:
            return None
        selected.append(next_candidate)
    return tuple(selected)


def _planned_assessment_batch_v1(
    plan: CurriculumPlanV1,
    request: CurriculumSelectionRequestV1,
    ranked: tuple[CurriculumDrillCandidateV1, ...],
) -> tuple[CurriculumDrillCandidateV1, ...] | None:
    selected: list[CurriculumDrillCandidateV1] = []
    for offset in range(ASSESSMENT_BATCH_SIZE_V1):
        entry = plan.entry_for(request.selection_ordinal + offset)
        candidate = next(
            (
                item
                for item in ranked
                if item.lesson_digest == entry.lesson_digest
                and _can_add_candidate_v1(
                    item,
                    tuple(selected),
                    require_new_skill=(
                        offset < ASSESSMENT_DISTINCT_SKILL_REPRESENTATIVES_V1
                    ),
                )
            ),
            None,
        )
        if candidate is None:
            return None
        selected.append(candidate)
    return tuple(selected)


def freeze_assessment_v1(
    request: CurriculumSelectionRequestV1,
    projection: LearnerProjectionV1,
    ledger: LearnerEvidenceLedgerV1,
    catalog: CurriculumCandidateCatalogV1,
    history: tuple[SelectionHistoryEntryV1, ...] = (),
    plans: tuple[CurriculumPlanV1, ...] = (),
) -> AssessmentFreezeResultV1:
    """Freeze exact assessment bytes, seed, order, scoring, and reveal policy."""

    if request.mode is not CurriculumMode.ASSESSMENT:
        raise ValueError("assessment freeze requires ASSESSMENT mode")
    # Universal eligibility and normal cooldowns are evaluated against the
    # pre-assessment prefix.  Plan sequencing is resolved only after those locks.
    selection = select_curriculum_v1(
        request,
        projection,
        ledger,
        catalog,
        history,
        (),
    )
    plan, plan_digests, plan_error = _applicable_assessment_plan_v1(
        plans,
        projection.learner_id,
        request,
        catalog,
    )
    if plan_error is not None:
        return AssessmentFreezeResultV1(
            AssessmentFreezeStatusV1.REFUSED,
            plan_error,
            selection,
            None,
            plan_digests,
        )
    if selection.status is not CurriculumSelectionStatusV1.SELECTED:
        return AssessmentFreezeResultV1(
            AssessmentFreezeStatusV1.REFUSED,
            selection.reason,
            selection,
            None,
            plan_digests,
        )
    ranked = tuple(catalog.candidate(item) for item in selection.ranking_order)
    batch = (
        _adaptive_assessment_batch_v1(ranked)
        if plan is None
        else _planned_assessment_batch_v1(plan, request, ranked)
    )
    if batch is None:
        reason = (
            "MANUAL_PLAN_REFUSED_ASSESSMENT_LOCKS"
            if plan is not None
            else "NO_ELIGIBLE_EIGHT_DRILL_ASSESSMENT"
        )
        return AssessmentFreezeResultV1(
            AssessmentFreezeStatusV1.REFUSED,
            reason,
            selection,
            None,
            plan_digests,
        )
    frozen = FrozenAssessmentV1(
        request=request,
        preassessment_ledger_digest=ledger.ledger_sha256,
        selection_record_digest=selection.selection_digest,
        drills=tuple(
            FrozenAssessmentDrillV1.from_candidate(index, candidate)
            for index, candidate in enumerate(batch, start=1)
        ),
        manual_plan_digest=None if plan is None else plan.plan_digest,
    )
    return AssessmentFreezeResultV1(
        AssessmentFreezeStatusV1.FROZEN,
        "ASSESSMENT_BYTES_SCORING_SEED_AND_REVEAL_FROZEN",
        selection,
        frozen,
        plan_digests,
    )


@dataclass(frozen=True, slots=True)
class AssessmentDrillScoreV1:
    assessment_position: int
    attempt_assessment_id: str
    primary_skill_id: str
    raw_score_ppm: int
    post_cap_score_ppm: int

    def __post_init__(self) -> None:
        if (
            type(self.assessment_position) is not int
            or not 1 <= self.assessment_position <= ASSESSMENT_BATCH_SIZE_V1
        ):
            raise ValueError("assessment drill-score position is invalid")
        if type(self.attempt_assessment_id) is not str or not self.attempt_assessment_id:
            raise ValueError("assessment drill-score evidence ID is empty")
        require_stable_skill_v1(self.primary_skill_id)
        for value in (self.raw_score_ppm, self.post_cap_score_ppm):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("assessment drill score is outside [0,S]")
        if self.post_cap_score_ppm > self.raw_score_ppm:
            raise ValueError("assessment error cap increased a score")

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_position": self.assessment_position,
            "attempt_assessment_id": self.attempt_assessment_id,
            "post_cap_score_ppm": self.post_cap_score_ppm,
            "primary_skill_id": self.primary_skill_id,
            "raw_score_ppm": self.raw_score_ppm,
        }

    @classmethod
    def from_dict(cls, payload: object) -> AssessmentDrillScoreV1:
        if not isinstance(payload, dict) or set(payload) != {
            "assessment_position",
            "attempt_assessment_id",
            "post_cap_score_ppm",
            "primary_skill_id",
            "raw_score_ppm",
        }:
            raise ValueError("assessment drill-score fields differ")
        for key in (
            "assessment_position",
            "post_cap_score_ppm",
            "raw_score_ppm",
        ):
            if type(payload[key]) is not int:
                raise TypeError("assessment drill-score integers must be exact")
        for key in ("attempt_assessment_id", "primary_skill_id"):
            if type(payload[key]) is not str:
                raise TypeError("assessment drill-score identity must be exact text")
        return cls(
            assessment_position=payload["assessment_position"],
            attempt_assessment_id=payload["attempt_assessment_id"],
            primary_skill_id=payload["primary_skill_id"],
            raw_score_ppm=payload["raw_score_ppm"],
            post_cap_score_ppm=payload["post_cap_score_ppm"],
        )


@dataclass(frozen=True, slots=True)
class ClosedAssessmentScoreV1:
    frozen_assessment_digest: str
    drill_scores: tuple[AssessmentDrillScoreV1, ...]
    score_ppm: int
    critical_error_record_count: int
    passed: bool
    scoring_policy_id: str = ASSESSMENT_SCORING_POLICY_ID_V1
    reveal_policy_id: str = ASSESSMENT_REVEAL_POLICY_ID_V1
    closure_state: str = "CLOSED_REVEAL_AUTHORIZED"

    def __post_init__(self) -> None:
        if (
            type(self.frozen_assessment_digest) is not str
            or len(self.frozen_assessment_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.frozen_assessment_digest)
        ):
            raise ValueError("closed assessment freeze digest is invalid")
        if (
            type(self.drill_scores) is not tuple
            or len(self.drill_scores) != ASSESSMENT_BATCH_SIZE_V1
            or tuple(item.assessment_position for item in self.drill_scores)
            != tuple(range(1, ASSESSMENT_BATCH_SIZE_V1 + 1))
        ):
            raise ValueError("closed assessment requires eight ordered scores")
        expected_score = round_div_even_v1(
            sum(item.post_cap_score_ppm for item in self.drill_scores),
            ASSESSMENT_BATCH_SIZE_V1,
        )
        if self.score_ppm != expected_score:
            raise ValueError("closed assessment average differs")
        if type(self.critical_error_record_count) is not int or self.critical_error_record_count < 0:
            raise ValueError("closed assessment critical-error count is invalid")
        expected_pass = (
            self.score_ppm >= ASSESSMENT_PASS_SCORE_PPM_V1
            and self.critical_error_record_count
            <= ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1
        )
        if self.passed is not expected_pass:
            raise ValueError("closed assessment pass boundary differs")
        if (
            self.scoring_policy_id != ASSESSMENT_SCORING_POLICY_ID_V1
            or self.reveal_policy_id != ASSESSMENT_REVEAL_POLICY_ID_V1
            or self.closure_state != "CLOSED_REVEAL_AUTHORIZED"
        ):
            raise ValueError("closed assessment policy or reveal state differs")

    @property
    def closure_digest(self) -> str:
        return sha256_json(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "closure_state": self.closure_state,
            "critical_error_record_count": self.critical_error_record_count,
            "drill_scores": [item.as_dict() for item in self.drill_scores],
            "frozen_assessment_digest": self.frozen_assessment_digest,
            "passed": self.passed,
            "record_kind": "CLOSED_ASSESSMENT_SCORE_V1",
            "reveal_policy_id": self.reveal_policy_id,
            "score_ppm": self.score_ppm,
            "scoring_policy_id": self.scoring_policy_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ClosedAssessmentScoreV1:
        closure = cls.from_dict(_canonical_object(raw, "closed assessment score"))
        if closure.canonical_bytes() != raw:
            raise ValueError("closed assessment score changed during restoration")
        return closure

    @classmethod
    def from_dict(cls, payload: object) -> ClosedAssessmentScoreV1:
        if not isinstance(payload, dict) or set(payload) != {
            "closure_state",
            "critical_error_record_count",
            "drill_scores",
            "frozen_assessment_digest",
            "passed",
            "record_kind",
            "reveal_policy_id",
            "score_ppm",
            "scoring_policy_id",
        } or payload["record_kind"] != "CLOSED_ASSESSMENT_SCORE_V1":
            raise ValueError("closed assessment score fields differ")
        raw_scores = payload["drill_scores"]
        if not isinstance(raw_scores, list):
            raise TypeError("closed assessment drill scores must be an array")
        for key in ("critical_error_record_count", "score_ppm"):
            if type(payload[key]) is not int:
                raise TypeError("closed assessment counts must be exact integers")
        if type(payload["passed"]) is not bool:
            raise TypeError("closed assessment pass state must be an exact bool")
        string_fields = {
            key: payload[key]
            for key in (
                "closure_state",
                "frozen_assessment_digest",
                "reveal_policy_id",
                "scoring_policy_id",
            )
        }
        if any(type(item) is not str for item in string_fields.values()):
            raise TypeError("closed assessment policy binding must be exact text")
        return cls(
            frozen_assessment_digest=string_fields["frozen_assessment_digest"],
            drill_scores=tuple(
                AssessmentDrillScoreV1.from_dict(item) for item in raw_scores
            ),
            score_ppm=payload["score_ppm"],
            critical_error_record_count=payload["critical_error_record_count"],
            passed=payload["passed"],
            scoring_policy_id=string_fields["scoring_policy_id"],
            reveal_policy_id=string_fields["reveal_policy_id"],
            closure_state=string_fields["closure_state"],
        )


def score_frozen_assessment_v1(
    frozen: FrozenAssessmentV1,
    attempts: tuple[AttemptAssessmentV1, ...],
) -> ClosedAssessmentScoreV1:
    """Close and score exactly eight attempts against the preregistered batch."""

    if not isinstance(frozen, FrozenAssessmentV1):
        raise TypeError("assessment scoring requires a frozen assessment")
    if (
        type(attempts) is not tuple
        or len(attempts) != ASSESSMENT_BATCH_SIZE_V1
        or any(not isinstance(item, AttemptAssessmentV1) for item in attempts)
    ):
        raise ValueError("assessment closure requires exactly eight typed attempts")
    ordinals = tuple(item.attempt_ordinal for item in attempts)
    if ordinals != tuple(sorted(set(ordinals))):
        raise ValueError("assessment closure attempts are not in strict ordinal order")
    scores: list[AssessmentDrillScoreV1] = []
    critical_count = 0
    for position, (expected, attempt) in enumerate(
        zip(frozen.drills, attempts, strict=True),
        start=1,
    ):
        if (
            attempt.mode is not AttemptModeV1.ASSESSMENT
            or attempt.lesson_digest != expected.lesson_digest
            or attempt.primary_skill_id != expected.primary_skill_id
        ):
            raise ValueError("assessment attempt identity differs from frozen order")
        row = next(
            (
                item
                for item in attempt.skill_evidence
                if item.skill_id == expected.primary_skill_id
            ),
            None,
        )
        if row is None:
            raise ValueError("assessment attempt is missing its primary skill row")
        caps = [row.score_ppm]
        for error in attempt.errors:
            if error.mapped_skill_id == expected.primary_skill_id:
                cap = score_cap_for_error_v1(
                    error.error_type,
                    attempt.primary_skill_id,
                )
                if cap is not None:
                    caps.append(cap)
            if error.error_type in CRITICAL_ERROR_TYPES_V1:
                critical_count += 1
        scores.append(
            AssessmentDrillScoreV1(
                position,
                attempt.assessment_id,
                expected.primary_skill_id,
                row.score_ppm,
                min(caps),
            )
        )
    score = round_div_even_v1(
        sum(item.post_cap_score_ppm for item in scores),
        ASSESSMENT_BATCH_SIZE_V1,
    )
    return ClosedAssessmentScoreV1(
        frozen_assessment_digest=frozen.assessment_digest,
        drill_scores=tuple(scores),
        score_ppm=score,
        critical_error_record_count=critical_count,
        passed=(
            score >= ASSESSMENT_PASS_SCORE_PPM_V1
            and critical_count <= ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1
        ),
    )


def reveal_closed_assessment_v1(
    frozen: FrozenAssessmentV1,
    closure: ClosedAssessmentScoreV1,
    catalog: CurriculumCandidateCatalogV1,
) -> dict[str, object]:
    """Reveal identity only after a valid score record closes all eight drills."""

    if not isinstance(frozen, FrozenAssessmentV1) or not isinstance(
        closure,
        ClosedAssessmentScoreV1,
    ):
        raise TypeError("assessment reveal requires frozen and closed records")
    if closure.frozen_assessment_digest != frozen.assessment_digest:
        raise ValueError("assessment closure is not bound to the frozen batch")
    rows = []
    for frozen_drill, score in zip(frozen.drills, closure.drill_scores, strict=True):
        candidate = catalog.candidate(frozen_drill.candidate_id)
        if candidate.candidate_digest != frozen_drill.candidate_digest:
            raise ValueError("assessment reveal catalog semantics differ")
        rows.append(
            {
                "assessment_position": frozen_drill.assessment_position,
                "lesson_id": candidate.drill.lesson_id,
                "post_cap_score_ppm": score.post_cap_score_ppm,
                "primary_skill_id": candidate.drill.primary_skill_id,
                "title": candidate.drill.title,
            }
        )
    return {
        "assessment_id": frozen.assessment_id,
        "closure_digest": closure.closure_digest,
        "critical_error_record_count": closure.critical_error_record_count,
        "drills": rows,
        "passed": closure.passed,
        "reveal_state": "REVEALED_AFTER_CLOSURE",
        "score_ppm": closure.score_ppm,
    }


__all__ = [
    "ADAPTIVE_MODE_POLICIES_V1",
    "ASSESSMENT_BATCH_SIZE_V1",
    "ASSESSMENT_CRITICAL_ERROR_RECORD_MAX_V1",
    "ASSESSMENT_DISTINCT_SKILL_REPRESENTATIVES_V1",
    "ASSESSMENT_MAX_DRILLS_PER_SKILL_V1",
    "ASSESSMENT_PASS_SCORE_PPM_V1",
    "ASSESSMENT_REVEAL_POLICY_ID_V1",
    "ASSESSMENT_SCORING_POLICY_ID_V1",
    "AdaptiveModePolicyV1",
    "AssessmentDrillScoreV1",
    "AssessmentFreezeResultV1",
    "AssessmentFreezeStatusV1",
    "ClosedAssessmentScoreV1",
    "FrozenAssessmentDrillV1",
    "FrozenAssessmentV1",
    "adaptive_mode_policy_v1",
    "freeze_assessment_v1",
    "reveal_closed_assessment_v1",
    "score_frozen_assessment_v1",
]
