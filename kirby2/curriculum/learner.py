"""Pure WO34-B reducer from immutable learner evidence to V1 estimates."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LearnerErrorTypeV1, score_cap_for_error_v1
from .evidence import (
    AttemptAmbiguityV1,
    AttemptAssessmentV1,
    AttemptEvidenceSufficiencyV1,
    LearnerEvidenceLedgerV1,
    POLICY_SCALE_V1,
    SkillEvidenceV1,
)
from .projections import (
    DEVELOPING_MAX_EXCLUSIVE_PPM_V1,
    EVIDENCE_CONFIDENCE_DIVISOR_V1,
    FAILURE_SCORE_MAX_PPM_V1,
    LEARNER_PROJECTION_MODEL_ID_V1,
    LEARNER_PROJECTION_POLICY_V1,
    MODE_BASE_WEIGHT_PPM_V1,
    NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1,
    RECENT_HISTORY_LIMIT_V1,
    SUCCESS_SCORE_MIN_PPM_V1,
    SUFFICIENT_CONFIDENCE_PPM_V1,
    SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1,
    SUFFICIENT_LIQUIDITY_BAND_COUNT_V1,
    SUFFICIENT_OPPORTUNITY_COUNT_V1,
    SUFFICIENT_SCENARIO_COUNT_V1,
    SUFFICIENT_VOLUME_BAND_COUNT_V1,
    DemonstratedOutcomeKindV1,
    DemonstratedOutcomeV1,
    LearnerProjectionV1,
    ObservedEvidenceCountsV1,
    ProjectedSkillLabelV1,
    ProjectionEvidenceObservationV1,
    ProjectionEvidenceReferenceV1,
    ProjectionSufficiencyV1,
    SkillProjectionV1,
    mul_ppm_v1,
    projection_diversity_band_v1,
    recency_factor_ppm_v1,
    round_div_even_v1,
    unsigned_share_ppm_v1,
)
from .skills import CURRICULUM_SKILL_IDS_V1, sha256_json


@dataclass(frozen=True, slots=True)
class _LedgerEvidenceRowV1:
    assessment: AttemptAssessmentV1
    evidence: SkillEvidenceV1

    @property
    def sort_key(self) -> tuple[int, bytes, bytes, bytes]:
        return (
            self.assessment.attempt_ordinal,
            self.assessment.assessment_id.encode("utf-8"),
            self.evidence.evidence_id.encode("utf-8"),
            self.evidence.skill_id.encode("utf-8"),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment.assessment_id,
            "attempt_ordinal": self.assessment.attempt_ordinal,
            "evidence_id": self.evidence.evidence_id,
            "skill_id": self.evidence.skill_id,
        }


def _input_rows(
    ledger: LearnerEvidenceLedgerV1,
    as_of_attempt_ordinal: int,
) -> tuple[tuple[AttemptAssessmentV1, ...], tuple[_LedgerEvidenceRowV1, ...]]:
    assessments = tuple(
        assessment
        for assessment in ledger.assessments
        if assessment.attempt_ordinal <= as_of_attempt_ordinal
    )
    rows = tuple(
        sorted(
            (
                _LedgerEvidenceRowV1(assessment, evidence)
                for assessment in assessments
                for evidence in assessment.skill_evidence
            ),
            key=lambda item: item.sort_key,
        )
    )
    return assessments, rows


def _applied_errors_and_score(
    assessment: AttemptAssessmentV1,
    evidence: SkillEvidenceV1,
) -> tuple[tuple[LearnerErrorTypeV1, ...], int]:
    applied: list[LearnerErrorTypeV1] = []
    caps = [evidence.score_ppm]
    for error in assessment.errors:
        if error.mapped_skill_id != evidence.skill_id:
            continue
        cap = score_cap_for_error_v1(error.error_type, assessment.primary_skill_id)
        if cap is None:
            continue
        applied.append(error.error_type)
        caps.append(cap)
    return (
        tuple(sorted(applied, key=lambda item: item.value)),
        min(caps),
    )


def _positive_observation(
    row: _LedgerEvidenceRowV1,
    as_of_attempt_ordinal: int,
) -> ProjectionEvidenceObservationV1 | None:
    assessment = row.assessment
    evidence = row.evidence
    if (
        not evidence.opportunity_present
        or not evidence.observable
        or assessment.ambiguity is not AttemptAmbiguityV1.NONE
        or assessment.evidence_sufficiency
        is not AttemptEvidenceSufficiencyV1.SUFFICIENT
    ):
        return None
    age = as_of_attempt_ordinal - assessment.attempt_ordinal
    recency = recency_factor_ppm_v1(age)
    base_weight = MODE_BASE_WEIGHT_PPM_V1[assessment.mode]
    effective_weight = mul_ppm_v1(base_weight, recency)
    if effective_weight <= 0:
        return None
    applied_errors, post_cap_score = _applied_errors_and_score(
        assessment,
        evidence,
    )
    context = assessment.observable_context
    return ProjectionEvidenceObservationV1(
        skill_id=evidence.skill_id,
        assessment_id=assessment.assessment_id,
        evidence_id=evidence.evidence_id,
        attempt_ordinal=assessment.attempt_ordinal,
        mode=assessment.mode,
        raw_score_ppm=evidence.score_ppm,
        post_cap_score_ppm=post_cap_score,
        applied_error_types=applied_errors,
        base_weight_ppm=base_weight,
        age_attempts=age,
        recency_factor_ppm=recency,
        effective_weight_ppm=effective_weight,
        scenario_semantic_sha256=context.scenario_semantic_sha256,
        volume_band=projection_diversity_band_v1(context.volume_multiplier_ppm),
        liquidity_band=projection_diversity_band_v1(
            context.liquidity_multiplier_ppm
        ),
        source_class=context.source_class,
        study_timestamp_utc=assessment.study_timestamp_utc,
        simulation_time_us=context.simulation_time_us,
    )


def _reference(observation: ProjectionEvidenceObservationV1) -> ProjectionEvidenceReferenceV1:
    return ProjectionEvidenceReferenceV1(
        assessment_id=observation.assessment_id,
        evidence_id=observation.evidence_id,
        attempt_ordinal=observation.attempt_ordinal,
        post_cap_score_ppm=observation.post_cap_score_ppm,
    )


def _diversity_confidence(
    scenario_count: int,
    volume_band_count: int,
    liquidity_band_count: int,
    source_class_count: int,
) -> int:
    components = (
        unsigned_share_ppm_v1(min(scenario_count, 4), 4),
        unsigned_share_ppm_v1(min(volume_band_count, 3), 3),
        unsigned_share_ppm_v1(min(liquidity_band_count, 3), 3),
        unsigned_share_ppm_v1(min(source_class_count, 2), 2),
    )
    return round_div_even_v1(sum(components), len(components))


def _skill_projection(
    skill_id: str,
    rows: tuple[_LedgerEvidenceRowV1, ...],
    as_of_attempt_ordinal: int,
) -> SkillProjectionV1:
    skill_rows = tuple(row for row in rows if row.evidence.skill_id == skill_id)
    observations = tuple(
        observation
        for row in skill_rows
        if (observation := _positive_observation(row, as_of_attempt_ordinal))
        is not None
    )
    effective_weight = sum(item.effective_weight_ppm for item in observations)
    weighted_scores = sum(
        item.effective_weight_ppm * item.post_cap_score_ppm
        for item in observations
    )
    mastery = round_div_even_v1(
        2 * POLICY_SCALE_V1 * POLICY_SCALE_V1 + weighted_scores,
        4 * POLICY_SCALE_V1 + effective_weight,
    )
    evidence_confidence = min(
        POLICY_SCALE_V1,
        round_div_even_v1(effective_weight, EVIDENCE_CONFIDENCE_DIVISOR_V1),
    )
    scenario_values = {
        item.scenario_semantic_sha256 for item in observations
    }
    volume_bands = tuple(
        sorted({item.volume_band for item in observations}, key=lambda item: item.value)
    )
    liquidity_bands = tuple(
        sorted(
            {item.liquidity_band for item in observations},
            key=lambda item: item.value,
        )
    )
    source_classes = tuple(
        sorted({item.source_class for item in observations}, key=lambda item: item.value)
    )
    diversity_confidence = _diversity_confidence(
        len(scenario_values),
        len(volume_bands),
        len(liquidity_bands),
        len(source_classes),
    )
    confidence = min(evidence_confidence, diversity_confidence)
    sufficient = (
        len(observations) >= SUFFICIENT_OPPORTUNITY_COUNT_V1
        and effective_weight >= SUFFICIENT_EFFECTIVE_WEIGHT_PPM_V1
        and len(scenario_values) >= SUFFICIENT_SCENARIO_COUNT_V1
        and len(volume_bands) >= SUFFICIENT_VOLUME_BAND_COUNT_V1
        and len(liquidity_bands) >= SUFFICIENT_LIQUIDITY_BAND_COUNT_V1
        and confidence >= SUFFICIENT_CONFIDENCE_PPM_V1
    )
    label = (
        ProjectedSkillLabelV1.INSUFFICIENT
        if not sufficient
        else (
            ProjectedSkillLabelV1.NEEDS_WORK
            if mastery < NEEDS_WORK_MAX_EXCLUSIVE_PPM_V1
            else (
                ProjectedSkillLabelV1.DEVELOPING
                if mastery < DEVELOPING_MAX_EXCLUSIVE_PPM_V1
                else ProjectedSkillLabelV1.STRONG
            )
        )
    )
    successes = tuple(
        item
        for item in observations
        if item.post_cap_score_ppm >= SUCCESS_SCORE_MIN_PPM_V1
    )
    failures = tuple(
        item
        for item in observations
        if item.post_cap_score_ppm <= FAILURE_SCORE_MAX_PPM_V1
    )
    recent_history = observations[-RECENT_HISTORY_LIMIT_V1:]
    last = observations[-1] if observations else None
    known_errors = tuple(
        sorted(
            {
                error_type
                for observation in observations
                for error_type in observation.applied_error_types
            },
            key=lambda item: item.value,
        )
    )
    counts = ObservedEvidenceCountsV1(
        total_rows=len(skill_rows),
        opportunity_present_rows=sum(
            row.evidence.opportunity_present for row in skill_rows
        ),
        observable_rows=sum(row.evidence.observable for row in skill_rows),
        ambiguous_rows=sum(
            row.assessment.ambiguity is not AttemptAmbiguityV1.NONE
            for row in skill_rows
        ),
        positive_weight_rows=len(observations),
        zero_weight_rows=len(skill_rows) - len(observations),
        demonstrated_success_rows=len(successes),
        demonstrated_failure_rows=len(failures),
    )
    return SkillProjectionV1(
        skill_id=skill_id,
        mastery_ppm=mastery,
        confidence_ppm=confidence,
        uncertainty_ppm=POLICY_SCALE_V1 - confidence,
        attempt_count=len(observations),
        effective_weight_ppm=effective_weight,
        weighted_score_sum=weighted_scores,
        model_evidence_score_ppm=(
            None
            if effective_weight == 0
            else round_div_even_v1(weighted_scores, effective_weight)
        ),
        evidence_confidence_ppm=evidence_confidence,
        diversity_confidence_ppm=diversity_confidence,
        scenario_diversity_count=len(scenario_values),
        volume_band_diversity=volume_bands,
        liquidity_band_diversity=liquidity_bands,
        source_class_diversity=source_classes,
        recent_attempt_history=recent_history,
        last_opportunity=None if last is None else _reference(last),
        last_opportunity_age_attempts=None if last is None else last.age_attempts,
        last_demonstrated_success=(
            None
            if not successes
            else DemonstratedOutcomeV1(
                DemonstratedOutcomeKindV1.SUCCESS,
                _reference(successes[-1]),
            )
        ),
        last_demonstrated_failure=(
            None
            if not failures
            else DemonstratedOutcomeV1(
                DemonstratedOutcomeKindV1.FAILURE,
                _reference(failures[-1]),
            )
        ),
        known_error_types=known_errors,
        observed_counts=counts,
        sufficiency=(
            ProjectionSufficiencyV1.SUFFICIENT
            if sufficient
            else ProjectionSufficiencyV1.INSUFFICIENT
        ),
        label=label,
        recommendation_eligible=sufficient,
    )


def build_learner_projection_v1(
    ledger: LearnerEvidenceLedgerV1,
    *,
    as_of_attempt_ordinal: int,
    model_id: str = LEARNER_PROJECTION_MODEL_ID_V1,
) -> LearnerProjectionV1:
    """Rebuild V1 solely from the complete immutable ledger prefix."""

    if not isinstance(ledger, LearnerEvidenceLedgerV1):
        raise TypeError("learner projection requires a typed evidence ledger")
    if type(as_of_attempt_ordinal) is not int or as_of_attempt_ordinal < 0:
        raise ValueError("projection as-of ordinal must be a nonnegative integer")
    if model_id != LEARNER_PROJECTION_MODEL_ID_V1:
        raise ValueError("unknown learner projection model version")
    assessments, rows = _input_rows(ledger, as_of_attempt_ordinal)
    input_digest = sha256_json([row.identity_dict() for row in rows])
    projections = tuple(
        _skill_projection(skill_id, rows, as_of_attempt_ordinal)
        for skill_id in CURRICULUM_SKILL_IDS_V1
    )
    return LearnerProjectionV1(
        learner_id=ledger.learner_id,
        as_of_attempt_ordinal=as_of_attempt_ordinal,
        input_assessment_count=len(assessments),
        input_skill_evidence_count=len(rows),
        input_evidence_sha256=input_digest,
        skill_projections=projections,
        model_id=model_id,
        model_policy_digest=LEARNER_PROJECTION_POLICY_V1.policy_digest,
    )


def rebuild_learner_projection_v1(
    ledger: LearnerEvidenceLedgerV1,
    *,
    as_of_attempt_ordinal: int,
) -> LearnerProjectionV1:
    return build_learner_projection_v1(
        ledger,
        as_of_attempt_ordinal=as_of_attempt_ordinal,
    )


__all__ = [
    "build_learner_projection_v1",
    "rebuild_learner_projection_v1",
]
