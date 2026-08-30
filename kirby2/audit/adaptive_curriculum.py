"""Executable WO34-A audit for immutable learner evidence and skill graph."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.curriculum.catalog import load_curriculum, prepare_lesson
from kirby2.curriculum.errors import (
    AMBIGUITY_ERROR_TYPES_V1,
    CRITICAL_ERROR_TYPES_V1,
    DEFAULT_SCORED_ERROR_CAP_PPM_V1,
    ERROR_SKILL_MAPPING_V1,
    REMEDIATION_ERROR_PRIORITY_V1,
    LearnerErrorTypeV1,
    mapped_skill_for_error_v1,
    score_cap_for_error_v1,
)
from kirby2.curriculum.evidence import (
    SCORING_POLICY_REGISTRY_V1,
    AttemptActionKindV1,
    AttemptActionV1,
    AttemptAmbiguityV1,
    AttemptAssessmentV1,
    AttemptErrorRecordV1,
    AttemptEvidenceSufficiencyV1,
    AttemptModeV1,
    AttemptOpportunityV1,
    AuxiliaryOutcomeV1,
    EvidenceFamilyV1,
    EvidenceReferenceKindV1,
    EvidenceSourceClassV1,
    LearnerEvidenceLedgerV1,
    ObservableAttemptContextV1,
    OpportunityStateV1,
    POLICY_SCALE_V1,
    SkillEvidenceV1,
    SupportingEvidenceReferenceV1,
    require_scoring_policy_v1,
)
from kirby2.curriculum.models import CurriculumDrill, CurriculumMode
from kirby2.curriculum.learner import build_learner_projection_v1
from kirby2.curriculum.adaptive_modes import (
    ADAPTIVE_MODE_POLICIES_V1,
    ASSESSMENT_BATCH_SIZE_V1,
    AssessmentFreezeStatusV1,
    freeze_assessment_v1,
    reveal_closed_assessment_v1,
    score_frozen_assessment_v1,
)
from kirby2.curriculum.plans import (
    NOT_APPLICABLE_V1,
    CurriculumPlanEntryV1,
    CurriculumPlanV1,
)
from kirby2.curriculum.projections import (
    ASSESSMENT_BASE_WEIGHT_PPM_V1,
    GUIDED_BASE_WEIGHT_PPM_V1,
    LEARNER_PROJECTION_MODEL_ID_V1,
    LEARNER_PROJECTION_POLICY_V1,
    LEARNER_PROJECTION_STATUS_V1,
    PRACTICE_BASE_WEIGHT_PPM_V1,
    RECENT_HISTORY_LIMIT_V1,
    REMEDIATION_BASE_WEIGHT_PPM_V1,
    LearnerProjectionV1,
    ProjectedSkillLabelV1,
    ProjectionDiversityBandV1,
    ProjectionSufficiencyV1,
    mul_ppm_v1,
    projection_diversity_band_v1,
    recency_factor_ppm_v1,
    round_div_even_v1,
)
from kirby2.curriculum.skills import (
    PREREQUISITE_CONFIDENCE_MIN_PPM_V1,
    PREREQUISITE_MASTERY_MIN_PPM_V1,
    PREREQUISITE_POLICY_ID_V1,
    SKILL_GRAPH_V1,
    STABLE_SKILL_REGISTRY_SHA256_V1,
    SkillGraphV1,
    SkillPrerequisiteEdgeV1,
)
from kirby2.curriculum.selection import (
    CURRICULUM_SELECTION_POLICY_SHA256_V1,
    SELECTION_COMPONENT_WEIGHTS_PPM_V1,
    SELECTION_COOLDOWN_WINDOWS_V1,
    CandidateExclusionV1,
    CurriculumCandidateCatalogV1,
    CurriculumSelectionRequestV1,
    CurriculumSelectionStatusV1,
    ManualPlanStatusV1,
    SelectionHistoryEntryV1,
    SelectionSemanticValueV1,
    build_legacy_candidate_catalog_v1,
    projection_digest_v1,
    select_curriculum_v1,
)
from kirby2.mining.models import canonical_json_bytes, sha256_json
from kirby2.mining.skills import SKILL_REGISTRY_V1, STABLE_SKILL_IDS_V1


WO34A_SKILL_COUNT = 23
WO34A_EDGE_COUNT = 4
WO34A_ROOT_COUNT = 19
WO34A_ERROR_COUNT = 16
WO34A_EVIDENCE_FAMILY_COUNT = 10
WO34A_LEGACY_LESSON_COUNT = 14
WO34A_SKILL_GRAPH_SHA256 = (
    "ec08caff691eb62915a9b9dd0ceb6481981e8e5e4a2452a7c01cfcf6b05b5141"
)
WO34B_SKILL_PROJECTION_COUNT = 23
WO34B_AUDIT_CASE_COUNT = 5
WO34B_PROJECTION_POLICY_SHA256 = (
    "8440d8cf51c69eb6cd287d9ec8c65715f4328e0854bfc3e3f3853c92f33f2550"
)
WO34C_AUDIT_CASE_COUNT = 6
WO34C_SELECTION_POLICY_SHA256 = (
    "47261cc202a532d8b01d9f1dbabddacd01fd7d0d60a26833ff7cb4e02cf0b8cf"
)
WO34D_AUDIT_CASE_COUNT = 4
WO34D_SYNTHETIC_LEARNER_COUNT = 6
WO34D_DEMO_SHA256 = (
    "575bf85959fdff9590d10c30071b7d7e24415b0ea0f48f0f399af88309689576"
)
_DIGEST = "1" * 64


@dataclass(frozen=True, slots=True)
class AdaptiveCurriculumAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    required: bool = True


def _raises(operation) -> bool:
    try:
        operation()
    except (AttributeError, KeyError, PermissionError, TypeError, ValueError):
        return True
    return False


def _ref(
    kind: EvidenceReferenceKindV1,
    reference_id: str,
    digest: str = _DIGEST,
    family: EvidenceFamilyV1 | None = None,
) -> SupportingEvidenceReferenceV1:
    return SupportingEvidenceReferenceV1(kind, reference_id, digest, family)


def _successful_assessment(
    *,
    ordinal: int = 1,
    learner_id: str = "learner-audit-001",
) -> AttemptAssessmentV1:
    policy = SCORING_POLICY_REGISTRY_V1["LEGACY_OBJECTIVE_SCORING_V1"]
    opportunity = AttemptOpportunityV1(
        opportunity_id=f"opportunity-{ordinal}",
        opportunity_present=True,
        observable=True,
        reaction_time_sufficient=True,
        reference_state=OpportunityStateV1.GREEN,
        activation_us=100,
        reaction_deadline_us=500,
        supporting_evidence_references=(
            _ref(EvidenceReferenceKindV1.OPPORTUNITY, f"opportunity-proof-{ordinal}"),
        ),
    )
    action = AttemptActionV1(
        action_id=f"action-{ordinal}",
        action_kind=AttemptActionKindV1.NO_ACTION,
        occurred_us=None,
        action_sha256=None,
        supporting_evidence_references=(
            _ref(EvidenceReferenceKindV1.ACTION, f"action-stream-{ordinal}"),
        ),
    )
    context = ObservableAttemptContextV1(
        context_id=f"context-{ordinal}",
        scenario_semantic_sha256=_DIGEST,
        volume_multiplier_ppm=1_000_000,
        liquidity_multiplier_ppm=1_000_000,
        source_class=EvidenceSourceClassV1.SYNTHETIC,
        simulation_time_us=100,
        supporting_evidence_references=(
            _ref(
                EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                f"context-proof-{ordinal}",
            ),
        ),
    )
    skill_evidence = tuple(
        SkillEvidenceV1(
            skill_id=skill_id,
            opportunity_present=True,
            observable=True,
            score_ppm=800_000,
            scoring_policy_id=policy.policy_id,
            scoring_policy_digest=policy.policy_digest,
            supporting_evidence_references=(
                _ref(
                    EvidenceReferenceKindV1.SCORING_INPUT,
                    f"score-{ordinal}-{skill_id.lower()}",
                    family=(
                        EvidenceFamilyV1.CORRECT_CLASSIFICATION
                        if skill_id == "BOOK_READING"
                        else EvidenceFamilyV1.APPROPRIATE_NO_TRADE
                    ),
                ),
            ),
        )
        for skill_id in ("BOOK_READING", "SCRIPT_DISCIPLINE")
    )
    error = AttemptErrorRecordV1(
        LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN,
        "SCRIPT_DISCIPLINE",
        (
            _ref(
                EvidenceReferenceKindV1.OPPORTUNITY,
                f"reaction-proof-{ordinal}",
            ),
        ),
    )
    auxiliary = AuxiliaryOutcomeV1(
        "PNL",
        125,
        "MILLITICKS",
        (
            _ref(
                EvidenceReferenceKindV1.SOURCE_RECORD,
                f"pnl-outcome-{ordinal}",
            ),
        ),
    )
    return AttemptAssessmentV1(
        learner_id=learner_id,
        attempt_ordinal=ordinal,
        lesson_reference_id="legacy-lesson-01",
        lesson_digest=_DIGEST,
        primary_skill_id="BOOK_READING",
        supporting_skill_ids=("SCRIPT_DISCIPLINE",),
        mode=AttemptModeV1.GUIDED,
        opportunity=opportunity,
        action=action,
        observable_context=context,
        scoring_policy_id=policy.policy_id,
        scoring_policy_digest=policy.policy_digest,
        skill_evidence=skill_evidence,
        errors=(error,),
        ambiguity=AttemptAmbiguityV1.NONE,
        evidence_sufficiency=AttemptEvidenceSufficiencyV1.SUFFICIENT,
        auxiliary_outcomes=(auxiliary,),
        study_timestamp_utc=f"2026-08-29T00:00:{ordinal:02d}Z",
    )


def _ambiguous_assessment() -> AttemptAssessmentV1:
    policy = SCORING_POLICY_REGISTRY_V1["LEGACY_OBJECTIVE_SCORING_V1"]
    return AttemptAssessmentV1(
        learner_id="learner-audit-001",
        attempt_ordinal=2,
        lesson_reference_id="legacy-lesson-01",
        lesson_digest=_DIGEST,
        primary_skill_id="BOOK_READING",
        supporting_skill_ids=(),
        mode=AttemptModeV1.PRACTICE,
        opportunity=AttemptOpportunityV1(
            "opportunity-2",
            False,
            False,
            False,
            OpportunityStateV1.NOT_APPLICABLE,
            None,
            None,
            (
                _ref(
                    EvidenceReferenceKindV1.OPPORTUNITY,
                    "opportunity-absence-proof-2",
                ),
            ),
        ),
        action=AttemptActionV1(
            "action-2",
            AttemptActionKindV1.NO_ACTION,
            None,
            None,
            (_ref(EvidenceReferenceKindV1.ACTION, "action-stream-2"),),
        ),
        observable_context=ObservableAttemptContextV1(
            "context-2",
            _DIGEST,
            500_000,
            1_500_000,
            EvidenceSourceClassV1.SYNTHETIC,
            200,
            (
                _ref(
                    EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                    "context-proof-2",
                ),
            ),
        ),
        scoring_policy_id=policy.policy_id,
        scoring_policy_digest=policy.policy_digest,
        skill_evidence=(
            SkillEvidenceV1(
                "BOOK_READING",
                False,
                False,
                900_000,
                policy.policy_id,
                policy.policy_digest,
                (
                    _ref(
                        EvidenceReferenceKindV1.SCORING_INPUT,
                        "unobservable-score-2",
                        family=EvidenceFamilyV1.CORRECT_CLASSIFICATION,
                    ),
                ),
            ),
        ),
        errors=(
            AttemptErrorRecordV1(
                LearnerErrorTypeV1.INSUFFICIENT_OBSERVABILITY,
                None,
                (
                    _ref(
                        EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                        "observability-gap-2",
                    ),
                ),
            ),
        ),
        ambiguity=AttemptAmbiguityV1.INSUFFICIENT_OBSERVABILITY,
        evidence_sufficiency=AttemptEvidenceSufficiencyV1.INSUFFICIENT,
        auxiliary_outcomes=(),
        study_timestamp_utc="2026-08-29T00:00:02Z",
    )


def _projection_assessment(
    ordinal: int,
    *,
    learner_id: str = "learner-projection-audit-001",
    primary_skill_id: str = "BOOK_READING",
    score_ppm: int = 900_000,
    mode: AttemptModeV1 = AttemptModeV1.ASSESSMENT,
    scenario_index: int | None = None,
    volume_multiplier_ppm: int = 1_000_000,
    liquidity_multiplier_ppm: int = 1_000_000,
    source_class: EvidenceSourceClassV1 = EvidenceSourceClassV1.SYNTHETIC,
    error_types: tuple[LearnerErrorTypeV1, ...] = (),
    pnl_value: int | None = None,
    study_second: int | None = None,
    simulation_time_us: int | None = None,
) -> AttemptAssessmentV1:
    policy = SCORING_POLICY_REGISTRY_V1["LEGACY_OBJECTIVE_SCORING_V1"]
    if scenario_index is None:
        scenario_index = ordinal
    mapped_skills = {
        mapped
        for error_type in error_types
        if (
            mapped := mapped_skill_for_error_v1(error_type, primary_skill_id)
        )
        is not None
    }
    supporting_skills = tuple(sorted(mapped_skills.difference({primary_skill_id})))
    sorted_errors = tuple(sorted(error_types, key=lambda item: item.value))
    no_action = LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN in error_types
    reference_state = (
        OpportunityStateV1.RED
        if LearnerErrorTypeV1.ACTED_DURING_RED in error_types
        else (
            OpportunityStateV1.WAIT
            if LearnerErrorTypeV1.CHASED_AFTER_INVALIDATION in error_types
            else OpportunityStateV1.GREEN
        )
    )
    activation_us = ordinal * 1_000
    action = AttemptActionV1(
        action_id=f"projection-action-{ordinal}",
        action_kind=(
            AttemptActionKindV1.NO_ACTION
            if no_action
            else AttemptActionKindV1.CLASSIFICATION
        ),
        occurred_us=None if no_action else activation_us + 100,
        action_sha256=(
            None
            if no_action
            else sha256_json({"projection_action": ordinal})
        ),
        supporting_evidence_references=(
            _ref(
                EvidenceReferenceKindV1.ACTION,
                f"projection-action-proof-{ordinal}",
            ),
        ),
    )
    context = ObservableAttemptContextV1(
        context_id=f"projection-context-{ordinal}",
        scenario_semantic_sha256=sha256_json(
            {"projection_scenario": scenario_index}
        ),
        volume_multiplier_ppm=volume_multiplier_ppm,
        liquidity_multiplier_ppm=liquidity_multiplier_ppm,
        source_class=source_class,
        simulation_time_us=(
            ordinal * 10_000
            if simulation_time_us is None
            else simulation_time_us
        ),
        supporting_evidence_references=(
            _ref(
                EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                f"projection-context-proof-{ordinal}",
            ),
        ),
    )
    skills = tuple(sorted({primary_skill_id, *supporting_skills}))
    skill_evidence = tuple(
        SkillEvidenceV1(
            skill_id=skill_id,
            opportunity_present=True,
            observable=True,
            score_ppm=score_ppm,
            scoring_policy_id=policy.policy_id,
            scoring_policy_digest=policy.policy_digest,
            supporting_evidence_references=(
                _ref(
                    EvidenceReferenceKindV1.SCORING_INPUT,
                    f"projection-score-{ordinal}-{skill_id.lower()}",
                    family=(
                        EvidenceFamilyV1.HOTKEY_ERROR
                        if skill_id == "HOTKEY_ACCURACY"
                        else EvidenceFamilyV1.CORRECT_CLASSIFICATION
                    ),
                ),
            ),
        )
        for skill_id in skills
    )
    errors = tuple(
        AttemptErrorRecordV1(
            error_type,
            mapped_skill_for_error_v1(error_type, primary_skill_id),
            (
                _ref(
                    EvidenceReferenceKindV1.SCORING_INPUT,
                    f"projection-error-{ordinal}-{error_type.value.lower()}",
                    family=EvidenceFamilyV1.DISCIPLINE_COMPLIANCE,
                ),
            ),
        )
        for error_type in sorted_errors
    )
    auxiliary = (
        ()
        if pnl_value is None
        else (
            AuxiliaryOutcomeV1(
                "PNL",
                pnl_value,
                "MILLITICKS",
                (
                    _ref(
                        EvidenceReferenceKindV1.SOURCE_RECORD,
                        f"projection-pnl-{ordinal}",
                    ),
                ),
            ),
        )
    )
    second = ordinal if study_second is None else study_second
    return AttemptAssessmentV1(
        learner_id=learner_id,
        attempt_ordinal=ordinal,
        lesson_reference_id=f"projection-lesson-{ordinal}",
        lesson_digest=sha256_json({"projection_lesson": ordinal}),
        primary_skill_id=primary_skill_id,
        supporting_skill_ids=supporting_skills,
        mode=mode,
        opportunity=AttemptOpportunityV1(
            opportunity_id=f"projection-opportunity-{ordinal}",
            opportunity_present=True,
            observable=True,
            reaction_time_sufficient=True,
            reference_state=reference_state,
            activation_us=activation_us,
            reaction_deadline_us=activation_us + 500,
            supporting_evidence_references=(
                _ref(
                    EvidenceReferenceKindV1.OPPORTUNITY,
                    f"projection-opportunity-proof-{ordinal}",
                ),
            ),
        ),
        action=action,
        observable_context=context,
        scoring_policy_id=policy.policy_id,
        scoring_policy_digest=policy.policy_digest,
        skill_evidence=skill_evidence,
        errors=errors,
        ambiguity=AttemptAmbiguityV1.NONE,
        evidence_sufficiency=AttemptEvidenceSufficiencyV1.SUFFICIENT,
        auxiliary_outcomes=auxiliary,
        study_timestamp_utc=f"2026-08-29T00:00:{second:02d}Z",
    )


def _projection_ledger(
    count: int,
    *,
    score_ppm: int = 900_000,
    distinct_scenarios: bool = True,
) -> LearnerEvidenceLedgerV1:
    volumes = (500_000, 1_000_000, 1_500_000)
    liquidities = (1_500_000, 500_000, 1_000_000)
    assessments = tuple(
        _projection_assessment(
            ordinal,
            score_ppm=score_ppm,
            scenario_index=ordinal if distinct_scenarios else 1,
            volume_multiplier_ppm=volumes[(ordinal - 1) % len(volumes)],
            liquidity_multiplier_ppm=liquidities[
                (ordinal - 1) % len(liquidities)
            ],
            source_class=(
                EvidenceSourceClassV1.SYNTHETIC
                if ordinal % 2
                else EvidenceSourceClassV1.HISTORICAL_OR_RECONSTRUCTION
            ),
        )
        for ordinal in range(1, count + 1)
    )
    return LearnerEvidenceLedgerV1("learner-projection-audit-001", assessments)


def _skill_graph_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    graph = SKILL_GRAPH_V1
    rebuilt = SkillGraphV1.from_json_bytes(graph.canonical_bytes())
    expected_edges = {
        ("QUEUE_POSITION", "MULTI_VENUE_ROUTING"),
        ("TAPE_READING", "ABSORPTION_RECOGNITION"),
        ("LATENCY_AWARENESS", "CANCEL_TIMING"),
        ("BOOK_READING", "HIDDEN_LIQUIDITY"),
    }
    if (
        len(graph.skills) != WO34A_SKILL_COUNT
        or graph.skills != STABLE_SKILL_IDS_V1
        or len(graph.edges) != WO34A_EDGE_COUNT
        or {
            (edge.prerequisite_skill_id, edge.dependent_skill_id)
            for edge in graph.edges
        }
        != expected_edges
        or len(graph.roots) != WO34A_ROOT_COUNT
        or rebuilt.canonical_bytes() != graph.canonical_bytes()
        or graph.graph_sha256 != WO34A_SKILL_GRAPH_SHA256
        or graph.skill_registry_sha256 != STABLE_SKILL_REGISTRY_SHA256_V1
        or graph.skill_registry_sha256 != SKILL_REGISTRY_V1.sha256
    ):
        failures.append("fixed skill graph inventory or roundtrip differs")
    policy = graph.readiness_policy
    if (
        policy.policy_id != PREREQUISITE_POLICY_ID_V1
        or policy.mastery_min_ppm != PREREQUISITE_MASTERY_MIN_PPM_V1
        or policy.confidence_min_ppm != PREREQUISITE_CONFIDENCE_MIN_PPM_V1
        or not policy.sufficient_evidence_required
    ):
        failures.append("uniform prerequisite-readiness policy differs")
    reverse = SkillPrerequisiteEdgeV1(
        "HIDDEN_LIQUIDITY",
        "BOOK_READING",
        "HOSTILE_REVERSE_EDGE",
    )
    cyclic_edges = tuple(
        sorted((*graph.edges, reverse), key=lambda edge: edge.sort_key)
    )
    cyclic_roots = tuple(
        skill
        for skill in graph.skills
        if skill not in {edge.dependent_skill_id for edge in cyclic_edges}
    )
    hostile = (
        lambda: replace(graph, skills=graph.skills[:-1]),
        lambda: replace(graph, edges=graph.edges[:-1]),
        lambda: replace(graph, edges=cyclic_edges, roots=cyclic_roots),
        lambda: replace(graph, skill_registry_sha256="0" * 64),
        lambda: replace(graph, previous_graph_sha256="0" * 64),
        lambda: SkillPrerequisiteEdgeV1(
            "BOOK_READING",
            "HIDDEN_LIQUIDITY",
            "HOSTILE_POLICY_OVERRIDE",
            "WEAKENED_POLICY",
        ),
    )
    refusal_count = sum(_raises(operation) for operation in hostile)
    if refusal_count != len(hostile):
        failures.append("skill graph hostile mutation was accepted")
    cold_imports = (
        "import kirby2.mining",
        "import kirby2.curriculum",
        "import kirby2.session.replay",
    )
    cold_import_count = 0
    cold_env = dict(os.environ)
    cold_env["PYTHONDONTWRITEBYTECODE"] = "1"
    repository_root = Path(__file__).resolve().parents[2]
    for script in cold_imports:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repository_root,
            env=cold_env,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        if completed.returncode == 0:
            cold_import_count += 1
        else:
            failures.append(f"cold package import failed: {script}")
    return AdaptiveCurriculumAuditCase(
        "a_skill_graph_is_exact_acyclic_content_addressed_and_uniform",
        (
            f"skills={len(graph.skills)}/23 edges={len(graph.edges)}/4 "
            f"roots={len(graph.roots)}/19 graph_sha256={graph.graph_sha256} "
            f"hostile_refusals={refusal_count}/{len(hostile)} "
            f"cold_imports={cold_import_count}/{len(cold_imports)}"
        ),
        tuple(failures),
    )


def _error_taxonomy_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    expected_mapping = {
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
    if (
        len(LearnerErrorTypeV1) != WO34A_ERROR_COUNT
        or dict(ERROR_SKILL_MAPPING_V1) != expected_mapping
        or len(REMEDIATION_ERROR_PRIORITY_V1) != 13
        or set(REMEDIATION_ERROR_PRIORITY_V1)
        != set(LearnerErrorTypeV1).difference(AMBIGUITY_ERROR_TYPES_V1)
    ):
        failures.append("closed error vocabulary, priority, or mapping differs")
    for error_type in REMEDIATION_ERROR_PRIORITY_V1:
        cap = score_cap_for_error_v1(error_type, "BOOK_READING")
        expected = 0 if error_type in CRITICAL_ERROR_TYPES_V1 else 250_000
        if cap != expected:
            failures.append(f"{error_type.value} score cap differs")
    if (
        mapped_skill_for_error_v1(
            LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
            "EXIT_EXECUTION",
        )
        != "EXIT_EXECUTION"
        or DEFAULT_SCORED_ERROR_CAP_PPM_V1 != 250_000
        or any(
            score_cap_for_error_v1(error_type, "BOOK_READING") is not None
            for error_type in AMBIGUITY_ERROR_TYPES_V1
        )
    ):
        failures.append("dynamic primary mapping or ambiguity cap differs")
    return AdaptiveCurriculumAuditCase(
        "a_error_vocabulary_mappings_priority_and_caps_are_closed",
        (
            f"errors={len(LearnerErrorTypeV1)}/16 remediation_priority="
            f"{len(REMEDIATION_ERROR_PRIORITY_V1)}/13 critical_caps=3@0 "
            "default_cap=250000 ambiguity_weight=0"
        ),
        tuple(failures),
    )


def _immutable_evidence_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    first = _successful_assessment()
    ambiguous = _ambiguous_assessment()
    original = LearnerEvidenceLedgerV1(first.learner_id, (first,))
    before = original.canonical_bytes()
    extended = original.append(ambiguous)
    rebuilt = LearnerEvidenceLedgerV1.from_json_bytes(extended.canonical_bytes())
    exact_skill_fields = {
        "observable",
        "opportunity_present",
        "score_ppm",
        "scoring_policy_digest",
        "scoring_policy_id",
        "skill_id",
        "supporting_evidence_references",
    }
    exact_families = (
        "CORRECT_CLASSIFICATION",
        "APPROPRIATE_NO_TRADE",
        "DISCIPLINE_COMPLIANCE",
        "FILL_QUALITY",
        "REACTION_TIMING",
        "CANCEL_MISTAKE",
        "QUEUE_MISUNDERSTANDING",
        "ROUTING_ERROR",
        "ADVERSE_SELECTION",
        "HOTKEY_ERROR",
    )
    if (
        original.canonical_bytes() != before
        or len(original.assessments) != 1
        or len(extended.assessments) != 2
        or rebuilt.canonical_bytes() != extended.canonical_bytes()
        or first.assessment_id == ambiguous.assessment_id
        or b"projection_version" in extended.canonical_bytes()
        or extended.as_dict()["projection_records"] != []
        or set(first.skill_evidence[0].as_dict()) != exact_skill_fields
        or tuple(item.value for item in EvidenceFamilyV1) != exact_families
    ):
        failures.append("append-only evidence roundtrip or projection boundary differs")
    unknown_family = first.skill_evidence[0].as_dict()
    unknown_family["supporting_evidence_references"][0][
        "evidence_family"
    ] = "PNL"
    bool_ordinal = extended.as_dict()
    bool_ordinal["assessments"][0]["attempt_ordinal"] = True
    hostiles = (
        lambda: original.append(replace(first, attempt_ordinal=1)),
        lambda: original.append(
            _successful_assessment(ordinal=2, learner_id="foreign-learner")
        ),
        lambda: replace(first, skill_evidence=first.skill_evidence[:-1]),
        lambda: replace(
            first,
            skill_evidence=(*first.skill_evidence, first.skill_evidence[0]),
        ),
        lambda: replace(
            first,
            skill_evidence=(
                replace(first.skill_evidence[0], score_ppm=1_000_001),
                first.skill_evidence[1],
            ),
        ),
        lambda: replace(
            first,
            skill_evidence=(
                replace(
                    first.skill_evidence[0],
                    scoring_policy_digest="0" * 64,
                ),
                first.skill_evidence[1],
            ),
        ),
        lambda: require_scoring_policy_v1("UNKNOWN_POLICY", "0" * 64),
        lambda: SkillEvidenceV1.from_dict(unknown_family),
        lambda: LearnerEvidenceLedgerV1.from_json_bytes(b" " + extended.canonical_bytes()),
        lambda: LearnerEvidenceLedgerV1.from_json_bytes(
            canonical_json_bytes(bool_ordinal)
        ),
    )
    refusal_count = sum(_raises(operation) for operation in hostiles)
    if refusal_count != len(hostiles):
        failures.append("evidence ledger or scoring-policy mutation was accepted")
    return AdaptiveCurriculumAuditCase(
        "a_attempt_evidence_is_canonical_append_only_and_policy_bound",
        (
            f"assessments={len(extended.assessments)} roundtrip=byte_identical "
            f"ledger_sha256={extended.ledger_sha256} "
            f"evidence_families={len(EvidenceFamilyV1)}/10 "
            f"hostile_refusals={refusal_count}/{len(hostiles)} projection=ABSENT"
        ),
        tuple(failures),
    )


def _inaction_ambiguity_and_pnl_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    valid = _successful_assessment()
    ambiguous = _ambiguous_assessment()
    observed_ambiguous = replace(
        valid,
        errors=(
            AttemptErrorRecordV1(
                LearnerErrorTypeV1.AMBIGUOUS,
                None,
                (
                    _ref(
                        EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                        "observed-ambiguity-proof",
                    ),
                ),
            ),
        ),
        ambiguity=AttemptAmbiguityV1.AMBIGUOUS,
        evidence_sufficiency=AttemptEvidenceSufficiencyV1.INSUFFICIENT,
    )
    hostile_opportunities = (
        replace(
            valid.opportunity,
            reaction_time_sufficient=False,
            reaction_deadline_us=None,
        ),
        replace(
            valid.opportunity,
            observable=False,
            reaction_time_sufficient=False,
            reaction_deadline_us=None,
        ),
        replace(valid.opportunity, reference_state=OpportunityStateV1.WAIT),
    )
    hostiles = tuple(
        (lambda opportunity=opportunity: replace(valid, opportunity=opportunity))
        for opportunity in hostile_opportunities
    )
    hostiles = (
        *hostiles,
        lambda: replace(
            valid,
            action=AttemptActionV1(
                "hostile-action",
                AttemptActionKindV1.CLASSIFICATION,
                200,
                _DIGEST,
                (_ref(EvidenceReferenceKindV1.ACTION, "hostile-action-proof"),),
            ),
        ),
        lambda: SkillEvidenceV1(
            "BOOK_READING",
            True,
            True,
            900_000,
            valid.scoring_policy_id,
            valid.scoring_policy_digest,
            (_ref(EvidenceReferenceKindV1.SOURCE_RECORD, "pnl-only"),),
        ),
        lambda: replace(
            valid,
            errors=(
                replace(valid.errors[0], mapped_skill_id="BOOK_READING"),
            ),
        ),
    )
    refusal_count = sum(_raises(operation) for operation in hostiles)
    if refusal_count != len(hostiles):
        failures.append(
            "inaction proof, error mapping, or PNL-only score was accepted"
        )
    ambiguous_row = ambiguous.skill_evidence[0]
    if (
        ambiguous.evidence_sufficiency
        is not AttemptEvidenceSufficiencyV1.INSUFFICIENT
        or ambiguous.ambiguity
        is not AttemptAmbiguityV1.INSUFFICIENT_OBSERVABILITY
        or ambiguous_row.projection_weight_eligible
        or observed_ambiguous.projection_weight_eligible_skill_evidence
        or ambiguous_row.score_ppm != 900_000
        or valid.auxiliary_outcomes[0].as_dict()["projection_weight"] != 0
    ):
        failures.append("ambiguity or auxiliary-outcome zero-weight boundary differs")
    return AdaptiveCurriculumAuditCase(
        "a_inaction_requires_proof_and_ambiguity_or_pnl_cannot_create_mastery",
        (
            f"valid_inaction=GREEN+observable+reaction_time "
            f"hostile_refusals={refusal_count}/{len(hostiles)} "
            "unobservable_weight=0 ambiguous_weight=0 pnl_weight=0 "
            "mastery_projection=ABSENT"
        ),
        tuple(failures),
    )


def _legacy_mapping_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    expected = {
        "01": ("BOOK_READING", ("SPREAD_DECISION", "TAPE_READING")),
        "02": ("BOOK_READING", ("QUEUE_POSITION", "TAPE_READING")),
        "03": ("BOOK_READING", ("QUEUE_POSITION", "TAPE_READING")),
        "04": ("QUEUE_POSITION", ("BOOK_READING",)),
        "05": (
            "PASSIVE_ENTRY",
            ("ADVERSE_SELECTION", "PARTIAL_FILL_MANAGEMENT", "QUEUE_POSITION"),
        ),
        "06": ("AGGRESSIVE_ENTRY", ("SPREAD_DECISION", "VOLUME_CONTEXT")),
        "07": (
            "ABSORPTION_RECOGNITION",
            ("BOOK_READING", "TAPE_READING"),
        ),
        "08": (
            "ABSORPTION_RECOGNITION",
            ("BOOK_READING", "TAPE_READING"),
        ),
        "09": ("REGIME_RECOGNITION", ("EXIT_EXECUTION", "TAPE_READING")),
        "10": (
            "LIQUIDITY_WITHDRAWAL",
            ("BOOK_READING", "POSITION_MANAGEMENT"),
        ),
        "11": ("TAPE_READING", ("AGGRESSIVE_ENTRY", "VOLUME_CONTEXT")),
        "12": ("VOLUME_CONTEXT", ("AGGRESSIVE_ENTRY", "TAPE_READING")),
        "13": (
            "POSITION_MANAGEMENT",
            ("AGGRESSIVE_ENTRY", "SPREAD_DECISION", "VOLUME_CONTEXT"),
        ),
        "14": (
            "POSITION_MANAGEMENT",
            ("EXIT_EXECUTION", "LIQUIDITY_WITHDRAWAL", "SCRIPT_DISCIPLINE"),
        ),
    }
    lessons = load_curriculum()
    actual = {
        lesson_id: (lesson.primary_skill_id, lesson.supporting_skill_ids)
        for lesson_id, lesson in lessons.items()
    }
    if len(lessons) != WO34A_LEGACY_LESSON_COUNT or actual != expected:
        failures.append("legacy lesson skill mapping differs")
    roundtrip_count = 0
    blind_count = 0
    for lesson_id, lesson in lessons.items():
        for mode in (CurriculumMode.LEARN, CurriculumMode.BLIND):
            drill = prepare_lesson(lesson_id, mode, 42)
            rebuilt = CurriculumDrill.from_dict(drill.as_dict())
            lesson.assert_contains(rebuilt)
            roundtrip_count += 1
            if mode is CurriculumMode.BLIND:
                briefing = drill.render_briefing()
                if (
                    lesson.primary_skill_id in briefing
                    or lesson.title in briefing
                    or "PRIMARY_SKILL WITHHELD UNTIL COMPLETION" not in briefing
                ):
                    failures.append(
                        f"legacy blind lesson {lesson_id} leaked skill identity"
                    )
                blind_count += 1
    catalog_sha256 = sha256_json(
        [lesson.catalog_dict() for lesson in lessons.values()]
    )
    return AdaptiveCurriculumAuditCase(
        "a_legacy_lessons_map_one_primary_skill_without_changing_blind_modes",
        (
            f"lessons={len(lessons)}/14 mapped_primary=14 "
            f"roundtrips={roundtrip_count}/28 blind_skill_withheld={blind_count}/14 "
            f"catalog_sha256={catalog_sha256}"
        ),
        tuple(failures),
    )


def audit_wo34a_adaptive_curriculum() -> tuple[AdaptiveCurriculumAuditCase, ...]:
    return (
        _skill_graph_case(),
        _error_taxonomy_case(),
        _immutable_evidence_case(),
        _inaction_ambiguity_and_pnl_case(),
        _legacy_mapping_case(),
    )


def _projection_policy_and_prior_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    empty_ledger = LearnerEvidenceLedgerV1("learner-projection-empty", ())
    projection = build_learner_projection_v1(
        empty_ledger,
        as_of_attempt_ordinal=0,
    )
    if (
        projection.model_id != LEARNER_PROJECTION_MODEL_ID_V1
        or projection.model_status != LEARNER_PROJECTION_STATUS_V1
        or projection.model_policy_digest != WO34B_PROJECTION_POLICY_SHA256
        or LEARNER_PROJECTION_POLICY_V1.policy_digest
        != WO34B_PROJECTION_POLICY_SHA256
        or projection.input_assessment_count != 0
        or projection.input_skill_evidence_count != 0
        or projection.input_evidence_sha256 != sha256_json([])
        or len(projection.skill_projections) != WO34B_SKILL_PROJECTION_COUNT
    ):
        failures.append("empty projection identity, input binding, or skill count differs")
    if any(
        row.mastery_ppm != 500_000
        or row.confidence_ppm != 0
        or row.uncertainty_ppm != POLICY_SCALE_V1
        or row.attempt_count != 0
        or row.effective_weight_ppm != 0
        or row.weighted_score_sum != 0
        or row.model_evidence_score_ppm is not None
        or row.sufficiency is not ProjectionSufficiencyV1.INSUFFICIENT
        or row.label is not ProjectedSkillLabelV1.INSUFFICIENT
        or row.recommendation_eligible
        or row.recent_attempt_history
        for row in projection.skill_projections
    ):
        failures.append("empty projection differs from the four-observation prior")
    if (
        GUIDED_BASE_WEIGHT_PPM_V1 != 250_000
        or PRACTICE_BASE_WEIGHT_PPM_V1 != 600_000
        or ASSESSMENT_BASE_WEIGHT_PPM_V1 != POLICY_SCALE_V1
        or REMEDIATION_BASE_WEIGHT_PPM_V1 != 700_000
        or recency_factor_ppm_v1(0) != POLICY_SCALE_V1
        or recency_factor_ppm_v1(20) != 500_000
        or round_div_even_v1(1, 2) != 0
        or round_div_even_v1(3, 2) != 2
        or round_div_even_v1(-1, 2) != 0
        or round_div_even_v1(-3, 2) != -2
        or projection_diversity_band_v1(749_999)
        is not ProjectionDiversityBandV1.LOW
        or projection_diversity_band_v1(750_000)
        is not ProjectionDiversityBandV1.NORMAL
        or projection_diversity_band_v1(1_250_000)
        is not ProjectionDiversityBandV1.NORMAL
        or projection_diversity_band_v1(1_250_001)
        is not ProjectionDiversityBandV1.HIGH
    ):
        failures.append("base weights, decay, bands, or round-to-even equations differ")
    if LearnerProjectionV1.from_json_bytes(
        projection.canonical_bytes()
    ).canonical_bytes() != projection.canonical_bytes():
        failures.append("empty projection canonical roundtrip differs")
    hostile_operations = (
        lambda: build_learner_projection_v1(
            empty_ledger,
            as_of_attempt_ordinal=0,
            model_id="LEARNER_PROJECTION_V2",
        ),
        lambda: build_learner_projection_v1(
            empty_ledger,
            as_of_attempt_ordinal=True,
        ),
        lambda: build_learner_projection_v1(
            empty_ledger,
            as_of_attempt_ordinal=-1,
        ),
        lambda: replace(projection, model_policy_digest=_DIGEST),
        lambda: replace(
            projection,
            skill_projections=projection.skill_projections[:-1],
        ),
        lambda: LearnerProjectionV1.from_json_bytes(
            projection.canonical_bytes() + b"\n"
        ),
    )
    refusal_count = sum(_raises(operation) for operation in hostile_operations)
    if refusal_count != len(hostile_operations):
        failures.append("projection version, type, policy, shape, or canonical refusal differs")
    return AdaptiveCurriculumAuditCase(
        "b_policy_equations_and_empty_prior_are_exact",
        (
            f"model={projection.model_id} status={projection.model_status} "
            f"policy_sha256={projection.model_policy_digest} skills=23 "
            "prior_mastery_ppm=500000 prior_confidence_ppm=0 "
            f"hostile_refusals={refusal_count}/{len(hostile_operations)}"
        ),
        tuple(failures),
    )


def _projection_caps_modes_and_recency_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learner_id = "learner-projection-audit-001"
    assessments = (
        _successful_assessment(ordinal=1, learner_id=learner_id),
        _projection_assessment(2, mode=AttemptModeV1.PRACTICE),
        _projection_assessment(3, mode=AttemptModeV1.ASSESSMENT),
        _projection_assessment(4, mode=AttemptModeV1.REMEDIATION),
        _projection_assessment(
            5,
            error_types=(LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,),
        ),
        _projection_assessment(
            6,
            primary_skill_id="SPREAD_DECISION",
            error_types=(LearnerErrorTypeV1.CROSSED_UNNECESSARILY,),
        ),
        _projection_assessment(
            7,
            primary_skill_id="SPREAD_DECISION",
            error_types=(
                LearnerErrorTypeV1.CROSSED_UNNECESSARILY,
                LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
            ),
        ),
        _projection_assessment(
            8,
            primary_skill_id="EXIT_EXECUTION",
            error_types=(LearnerErrorTypeV1.ACTED_DURING_RED,),
        ),
        _projection_assessment(
            9,
            primary_skill_id="EXIT_EXECUTION",
            error_types=(LearnerErrorTypeV1.WRONG_HOTKEY,),
        ),
    )
    projection = build_learner_projection_v1(
        LearnerEvidenceLedgerV1(learner_id, assessments),
        as_of_attempt_ordinal=9,
    )
    book = projection.skill("BOOK_READING")
    script = projection.skill("SCRIPT_DISCIPLINE")
    spread = projection.skill("SPREAD_DECISION")
    hotkey = projection.skill("HOTKEY_ACCURACY")
    exit_execution = projection.skill("EXIT_EXECUTION")
    book_history = book.recent_attempt_history
    if (
        tuple(item.base_weight_ppm for item in book_history)
        != (
            GUIDED_BASE_WEIGHT_PPM_V1,
            PRACTICE_BASE_WEIGHT_PPM_V1,
            ASSESSMENT_BASE_WEIGHT_PPM_V1,
            REMEDIATION_BASE_WEIGHT_PPM_V1,
            ASSESSMENT_BASE_WEIGHT_PPM_V1,
        )
        or tuple(item.post_cap_score_ppm for item in book_history)
        != (800_000, 900_000, 900_000, 900_000, 0)
    ):
        failures.append("mode weights or primary-skill critical cap differ")
    if any(
        item.age_attempts != 9 - item.attempt_ordinal
        or item.recency_factor_ppm
        != recency_factor_ppm_v1(9 - item.attempt_ordinal)
        or item.effective_weight_ppm
        != mul_ppm_v1(item.base_weight_ppm, item.recency_factor_ppm)
        for row in (book, script, spread, hotkey, exit_execution)
        for item in row.recent_attempt_history
    ):
        failures.append("ordinal age, recency, or effective weight differs")
    if (
        tuple(item.post_cap_score_ppm for item in script.recent_attempt_history)
        != (DEFAULT_SCORED_ERROR_CAP_PPM_V1, 0)
        or script.known_error_types
        != (
            LearnerErrorTypeV1.ACTED_DURING_RED,
            LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN,
        )
        or LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN in book.known_error_types
        or tuple(item.post_cap_score_ppm for item in hotkey.recent_attempt_history)
        != (0,)
        or hotkey.known_error_types != (LearnerErrorTypeV1.WRONG_HOTKEY,)
        or tuple(
            item.post_cap_score_ppm
            for item in exit_execution.recent_attempt_history
        )
        != (900_000, 900_000)
    ):
        failures.append("default or critical cap leaked beyond its mapped skill")
    if (
        tuple(item.post_cap_score_ppm for item in spread.recent_attempt_history)
        != (DEFAULT_SCORED_ERROR_CAP_PPM_V1, 0)
        or spread.known_error_types
        != (
            LearnerErrorTypeV1.CROSSED_UNNECESSARILY,
            LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
        )
        or spread.last_demonstrated_failure is None
        or spread.last_demonstrated_failure.reference.attempt_ordinal != 7
    ):
        failures.append("default, multiple-minimum, or latest-failure cap differs")
    if (
        book.last_demonstrated_failure is None
        or book.last_demonstrated_failure.reference.attempt_ordinal != 5
        or script.last_demonstrated_failure is None
        or script.last_demonstrated_failure.reference.attempt_ordinal != 8
        or hotkey.last_demonstrated_failure is None
        or hotkey.last_demonstrated_failure.reference.attempt_ordinal != 9
    ):
        failures.append("demonstrated failure references differ")
    return AdaptiveCurriculumAuditCase(
        "b_error_caps_modes_and_recency_are_exact",
        (
            "mode_weights_ppm=250000,600000,1000000,700000 "
            "mapped_caps_ppm=250000,0 multiple_caps=MINIMUM "
            f"book_weight_ppm={book.effective_weight_ppm} "
            f"script_errors={len(script.known_error_types)} "
            f"spread_errors={len(spread.known_error_types)} hotkey_errors=1"
        ),
        tuple(failures),
    )


def _projection_confidence_and_history_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    projection_8 = build_learner_projection_v1(
        _projection_ledger(8),
        as_of_attempt_ordinal=8,
    )
    book = projection_8.skill("BOOK_READING")
    expected_weight = sum(
        mul_ppm_v1(ASSESSMENT_BASE_WEIGHT_PPM_V1, recency_factor_ppm_v1(age))
        for age in range(7, -1, -1)
    )
    expected_weighted_score = expected_weight * 900_000
    expected_mastery = round_div_even_v1(
        2 * POLICY_SCALE_V1 * POLICY_SCALE_V1 + expected_weighted_score,
        4 * POLICY_SCALE_V1 + expected_weight,
    )
    expected_evidence_confidence = min(
        POLICY_SCALE_V1,
        round_div_even_v1(expected_weight, 8),
    )
    if (
        book.effective_weight_ppm != expected_weight
        or book.weighted_score_sum != expected_weighted_score
        or book.mastery_ppm != expected_mastery
        or book.evidence_confidence_ppm != expected_evidence_confidence
        or book.diversity_confidence_ppm != POLICY_SCALE_V1
        or book.confidence_ppm != expected_evidence_confidence
        or book.uncertainty_ppm
        != POLICY_SCALE_V1 - expected_evidence_confidence
    ):
        failures.append("mastery, evidence confidence, or diversity equation differs")
    if (
        book.attempt_count != 8
        or book.scenario_diversity_count != 8
        or set(book.volume_band_diversity) != set(ProjectionDiversityBandV1)
        or set(book.liquidity_band_diversity) != set(ProjectionDiversityBandV1)
        or len(book.source_class_diversity) != 2
        or book.sufficiency is not ProjectionSufficiencyV1.SUFFICIENT
        or book.label is not ProjectedSkillLabelV1.STRONG
        or not book.recommendation_eligible
        or book.last_demonstrated_success is None
        or book.last_demonstrated_success.reference.attempt_ordinal != 8
        or book.last_demonstrated_failure is not None
    ):
        failures.append("diversity, sufficiency, label, or outcome boundary differs")
    label_boundaries = tuple(
        (
            expected_mastery,
            expected_label,
            build_learner_projection_v1(
                _projection_ledger(8, score_ppm=score_ppm),
                as_of_attempt_ordinal=8,
            ).skill("BOOK_READING"),
        )
        for score_ppm, expected_mastery, expected_label in (
            (499_999, 499_999, ProjectedSkillLabelV1.NEEDS_WORK),
            (500_000, 500_000, ProjectedSkillLabelV1.DEVELOPING),
            (816_373, 699_999, ProjectedSkillLabelV1.DEVELOPING),
            (816_374, 700_000, ProjectedSkillLabelV1.STRONG),
        )
    )
    if any(
        row.mastery_ppm != expected_mastery
        or row.label is not expected_label
        or row.sufficiency is not ProjectionSufficiencyV1.SUFFICIENT
        for expected_mastery, expected_label, row in label_boundaries
    ):
        failures.append("needs-work, developing, or strong label boundary differs")
    threshold_ledger = LearnerEvidenceLedgerV1(
        "learner-projection-audit-001",
        (
            _projection_assessment(1, score_ppm=300_000),
            _projection_assessment(2, score_ppm=700_000),
        ),
    )
    threshold_book = build_learner_projection_v1(
        threshold_ledger,
        as_of_attempt_ordinal=2,
    ).skill("BOOK_READING")
    if (
        threshold_book.last_demonstrated_failure is None
        or threshold_book.last_demonstrated_failure.reference.attempt_ordinal != 1
        or threshold_book.last_demonstrated_success is None
        or threshold_book.last_demonstrated_success.reference.attempt_ordinal != 2
    ):
        failures.append("inclusive success or failure score threshold differs")
    projection_7 = build_learner_projection_v1(
        _projection_ledger(7),
        as_of_attempt_ordinal=7,
    )
    same_scenario = build_learner_projection_v1(
        _projection_ledger(8, distinct_scenarios=False),
        as_of_attempt_ordinal=8,
    )
    if (
        projection_7.skill("BOOK_READING").sufficiency
        is not ProjectionSufficiencyV1.INSUFFICIENT
        or same_scenario.skill("BOOK_READING").scenario_diversity_count != 1
        or same_scenario.skill("BOOK_READING").sufficiency
        is not ProjectionSufficiencyV1.INSUFFICIENT
    ):
        failures.append("opportunity-count or scenario-diversity gate differs")
    projection_25 = build_learner_projection_v1(
        _projection_ledger(25),
        as_of_attempt_ordinal=25,
    )
    history_25 = projection_25.skill("BOOK_READING").recent_attempt_history
    if (
        len(history_25) != RECENT_HISTORY_LIMIT_V1
        or tuple(item.attempt_ordinal for item in history_25)
        != tuple(range(6, 26))
        or projection_25.skill("BOOK_READING").attempt_count != 25
    ):
        failures.append("recent positive-history cap or total count differs")
    return AdaptiveCurriculumAuditCase(
        "b_confidence_diversity_sufficiency_and_history_are_exact",
        (
            f"attempts={book.attempt_count}/8 weight_ppm={expected_weight} "
            f"mastery_ppm={book.mastery_ppm} confidence_ppm={book.confidence_ppm} "
            "diversity=scenario8,volume3,liquidity3,source2 "
            "labels=499999/500000/699999/700000 "
            f"history={len(history_25)}/{RECENT_HISTORY_LIMIT_V1}"
        ),
        tuple(failures),
    )


def _projection_rebuild_and_version_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    ledger = _projection_ledger(8)
    ledger_bytes = ledger.canonical_bytes()
    first = build_learner_projection_v1(ledger, as_of_attempt_ordinal=8)
    second = build_learner_projection_v1(ledger, as_of_attempt_ordinal=8)
    if first.canonical_bytes() != second.canonical_bytes():
        failures.append("identical rebuilds produced different projection bytes")
    future_ledger = ledger.append(_projection_assessment(9))
    prefix = build_learner_projection_v1(
        future_ledger,
        as_of_attempt_ordinal=8,
    )
    if prefix.canonical_bytes() != first.canonical_bytes():
        failures.append("future immutable evidence changed the earlier projection prefix")
    changed_first = replace(
        ledger.assessments[0],
        study_timestamp_utc="2039-12-31T23:59:59Z",
        observable_context=replace(
            ledger.assessments[0].observable_context,
            simulation_time_us=987_654_321,
        ),
    )
    changed_ledger = LearnerEvidenceLedgerV1(
        ledger.learner_id,
        (changed_first, *ledger.assessments[1:]),
    )
    changed = build_learner_projection_v1(
        changed_ledger,
        as_of_attempt_ordinal=8,
    )
    if (
        changed.model_values_sha256 != first.model_values_sha256
        or changed.input_evidence_sha256 == first.input_evidence_sha256
        or changed.projection_id == first.projection_id
        or changed.canonical_bytes() == first.canonical_bytes()
    ):
        failures.append("clock provenance was not separated from deterministic model values")
    history = first.skill("BOOK_READING").recent_attempt_history
    if (
        tuple(item.attempt_ordinal for item in history) != tuple(range(1, 9))
        or first.input_assessment_count != 8
        or first.input_skill_evidence_count != 8
        or LearnerProjectionV1.from_json_bytes(first.canonical_bytes()) != first
        or ledger.canonical_bytes() != ledger_bytes
    ):
        failures.append("input ordering, roundtrip, counts, or ledger immutability differs")
    tampered = first.as_dict()
    tampered["model_values_sha256"] = _DIGEST
    hostile_operations = (
        lambda: LearnerEvidenceLedgerV1(
            ledger.learner_id,
            tuple(reversed(ledger.assessments)),
        ),
        lambda: build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=8,
            model_id="LEARNER_PROJECTION_V2",
        ),
        lambda: build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=True,
        ),
        lambda: build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=-1,
        ),
        lambda: replace(first, as_of_attempt_ordinal=9),
        lambda: replace(history[0], study_timestamp_utc="NOT_EXPLICIT_UTC"),
        lambda: LearnerProjectionV1.from_json_bytes(canonical_json_bytes(tampered)),
        lambda: LearnerProjectionV1.from_json_bytes(first.canonical_bytes() + b" "),
    )
    refusal_count = sum(_raises(operation) for operation in hostile_operations)
    if refusal_count != len(hostile_operations):
        failures.append("ordering, version, ordinal, digest, or canonical refusal differs")
    return AdaptiveCurriculumAuditCase(
        "b_rebuild_is_prefix_order_clock_and_version_deterministic",
        (
            f"input_assessments={first.input_assessment_count} "
            f"input_skill_rows={first.input_skill_evidence_count} "
            f"model_values_sha256={first.model_values_sha256} "
            "future_prefix_unchanged=true clock_values_excluded=true "
            f"hostile_refusals={refusal_count}/{len(hostile_operations)}"
        ),
        tuple(failures),
    )


def _projection_zero_weight_and_pnl_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    ambiguous = _ambiguous_assessment()
    pnl_reference = (
        _ref(EvidenceReferenceKindV1.SOURCE_RECORD, "projection-pnl-hostile-2"),
    )
    ambiguous_low = replace(
        ambiguous,
        auxiliary_outcomes=(AuxiliaryOutcomeV1("PNL", 125, "MILLITICKS", pnl_reference),),
    )
    ambiguous_high = replace(
        ambiguous,
        auxiliary_outcomes=(
            AuxiliaryOutcomeV1("PNL", 987_654_321, "MILLITICKS", pnl_reference),
        ),
    )
    low = build_learner_projection_v1(
        LearnerEvidenceLedgerV1(ambiguous.learner_id, (ambiguous_low,)),
        as_of_attempt_ordinal=2,
    )
    high = build_learner_projection_v1(
        LearnerEvidenceLedgerV1(ambiguous.learner_id, (ambiguous_high,)),
        as_of_attempt_ordinal=2,
    )
    book = low.skill("BOOK_READING")
    if (
        low.model_values_sha256 != high.model_values_sha256
        or low.input_evidence_sha256 == high.input_evidence_sha256
        or low.projection_id == high.projection_id
        or book.mastery_ppm != 500_000
        or book.confidence_ppm != 0
        or book.attempt_count != 0
        or book.model_evidence_score_ppm is not None
        or book.observed_counts.total_rows != 1
        or book.observed_counts.ambiguous_rows != 1
        or book.observed_counts.positive_weight_rows != 0
        or book.observed_counts.zero_weight_rows != 1
    ):
        failures.append("ambiguous evidence or PNL changed the learner estimate")
    positive_low_assessment = _projection_assessment(1, pnl_value=1)
    positive_high_assessment = _projection_assessment(1, pnl_value=987_654_321)
    positive_low = build_learner_projection_v1(
        LearnerEvidenceLedgerV1(
            positive_low_assessment.learner_id,
            (positive_low_assessment,),
        ),
        as_of_attempt_ordinal=1,
    )
    positive_high = build_learner_projection_v1(
        LearnerEvidenceLedgerV1(
            positive_high_assessment.learner_id,
            (positive_high_assessment,),
        ),
        as_of_attempt_ordinal=1,
    )
    if (
        positive_low.model_values_sha256 != positive_high.model_values_sha256
        or positive_low.input_evidence_sha256
        == positive_high.input_evidence_sha256
        or positive_low.skill("BOOK_READING").mastery_ppm
        != positive_high.skill("BOOK_READING").mastery_ppm
        or positive_low.skill("BOOK_READING").model_evidence_score_ppm
        != positive_high.skill("BOOK_READING").model_evidence_score_ppm
        or b"987654321" in positive_high.canonical_bytes()
        or any(
            "pnl" in key.lower()
            for key in positive_high
            .skill("BOOK_READING")
            .recent_attempt_history[0]
            .as_dict()
        )
    ):
        failures.append("auxiliary PNL leaked into positive model values")
    return AdaptiveCurriculumAuditCase(
        "b_zero_weight_and_pnl_cannot_update_projection",
        (
            "ambiguous_rows=1 positive_weight_rows=0 mastery_ppm=500000 "
            "pnl_weight_ppm=0 provenance_digest_changes=true "
            f"model_values_sha256={positive_low.model_values_sha256}"
        ),
        tuple(failures),
    )


def audit_wo34b_adaptive_curriculum() -> tuple[AdaptiveCurriculumAuditCase, ...]:
    return (
        _projection_policy_and_prior_case(),
        _projection_caps_modes_and_recency_case(),
        _projection_confidence_and_history_case(),
        _projection_rebuild_and_version_case(),
        _projection_zero_weight_and_pnl_case(),
    )


def _band_multiplier(band: ProjectionDiversityBandV1) -> int:
    return {
        ProjectionDiversityBandV1.LOW: 500_000,
        ProjectionDiversityBandV1.NORMAL: 1_000_000,
        ProjectionDiversityBandV1.HIGH: 2_000_000,
    }[band]


def _selection_history_sidecar(
    assessment: AttemptAssessmentV1,
    *,
    candidate=None,
) -> SelectionHistoryEntryV1:
    ordinal = assessment.attempt_ordinal
    return SelectionHistoryEntryV1(
        assessment_id=assessment.assessment_id,
        attempt_ordinal=ordinal,
        lesson_digest=assessment.lesson_digest,
        primary_skill_id=assessment.primary_skill_id,
        parameter_digest=(
            sha256_json({"selection_parameter": ordinal})
            if candidate is None
            else candidate.parameter_digest
        ),
        scenario_semantic_digest=(
            assessment.observable_context.scenario_semantic_sha256
        ),
        scenario_seed=(
            SelectionSemanticValueV1.concrete(str(34_000 + ordinal))
            if candidate is None
            else candidate.scenario_seed
        ),
        visible_queue_shape=(
            SelectionSemanticValueV1.concrete(
                sha256_json({"selection_queue_shape": ordinal})
            )
            if candidate is None
            else candidate.visible_queue_shape
        ),
        symbol=(
            SelectionSemanticValueV1.not_applicable()
            if candidate is None
            else candidate.symbol
        ),
        regime_parameter=(
            SelectionSemanticValueV1.concrete(
                sha256_json({"selection_regime": ordinal})
            )
            if candidate is None
            else candidate.regime_parameter
        ),
        volume_band=projection_diversity_band_v1(
            assessment.observable_context.volume_multiplier_ppm
        ),
        liquidity_band=projection_diversity_band_v1(
            assessment.observable_context.liquidity_multiplier_ppm
        ),
        source_class=assessment.observable_context.source_class,
    )


def _selection_attempt_for_candidate(
    candidate,
    ordinal: int,
    *,
    learner_id: str,
    score_ppm: int = 800_000,
    error_types: tuple[LearnerErrorTypeV1, ...] = (),
) -> AttemptAssessmentV1:
    base = _projection_assessment(
        ordinal,
        learner_id=learner_id,
        primary_skill_id=candidate.drill.primary_skill_id,
        score_ppm=score_ppm,
        mode=AttemptModeV1(candidate.drill.mode.value),
        volume_multiplier_ppm=_band_multiplier(candidate.volume_band),
        liquidity_multiplier_ppm=_band_multiplier(candidate.liquidity_band),
        source_class=candidate.source_class,
        error_types=error_types,
    )
    context = replace(
        base.observable_context,
        scenario_semantic_sha256=candidate.scenario_semantic_digest,
    )
    return replace(
        base,
        lesson_reference_id=candidate.candidate_id,
        lesson_digest=candidate.lesson_digest,
        observable_context=context,
    )


def _selection_modes_and_contract_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learn = prepare_lesson("01", CurriculumMode.LEARN, 42)
    blind = prepare_lesson("01", CurriculumMode.BLIND, 42)
    guided = prepare_lesson("01", CurriculumMode.GUIDED, 42)
    practice = prepare_lesson("01", CurriculumMode.PRACTICE, 42)
    assessment = prepare_lesson("01", CurriculumMode.ASSESSMENT, 42)
    remediation = prepare_lesson("01", CurriculumMode.REMEDIATION, 42)
    if (
        CurriculumMode.parse("learn") is not CurriculumMode.LEARN
        or CurriculumMode.parse("blind") is not CurriculumMode.BLIND
        or CurriculumMode.parse("guided") is not CurriculumMode.GUIDED
        or set(ADAPTIVE_MODE_POLICIES_V1)
        != {
            CurriculumMode.GUIDED,
            CurriculumMode.PRACTICE,
            CurriculumMode.ASSESSMENT,
            CurriculumMode.REMEDIATION,
        }
    ):
        failures.append("legacy or adaptive mode parsing differs")
    learn_brief = learn.render_briefing()
    blind_brief = blind.render_briefing()
    if (
        "MODE LEARN\nDRILL 01 Balanced book" not in learn_brief
        or "PRIMARY_SKILL BOOK_READING" not in learn_brief
        or "ASSISTANCE" in learn_brief
        or "MODE BLIND\nDRILL BLIND DRILL" not in blind_brief
        or "PRIMARY_SKILL WITHHELD UNTIL COMPLETION" not in blind_brief
        or "Balanced book" in blind_brief
        or "BOOK_READING" in blind_brief
    ):
        failures.append("legacy LEARN or BLIND briefing meaning changed")
    if (
        "CONCEPT_EXPLANATION" not in guided.render_briefing()
        or "ASSISTANCE DECLARED" not in guided.render_briefing()
        or "ASSISTANCE FEEDBACK_AFTER_ATTEMPT" not in practice.render_briefing()
        or "CONCEPT_EXPLANATION" in practice.render_briefing()
        or "Balanced book" in assessment.render_briefing()
        or "BOOK_READING" in assessment.render_briefing()
        or "ASSISTANCE RESTRICTED" not in assessment.render_briefing()
        or "DIAGNOSED_ERROR_AND_PREREQUISITE_CONTEXT"
        not in remediation.render_briefing()
    ):
        failures.append("adaptive modes are not behaviorally distinct or disclosure-safe")
    if any(
        CurriculumDrill.from_dict(item.as_dict()) != item
        for item in (learn, blind, guided, practice, assessment, remediation)
    ):
        failures.append("curriculum mode roundtrip differs")
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.PRACTICE)
    if (
        CurriculumCandidateCatalogV1.from_json_bytes(catalog.canonical_bytes())
        != catalog
        or sum(SELECTION_COMPONENT_WEIGHTS_PPM_V1.values()) != POLICY_SCALE_V1
        or dict(SELECTION_COOLDOWN_WINDOWS_V1)
        != {
            "lesson_digest": 5,
            "parameter_digest": 4,
            "scenario_seed": 4,
            "visible_queue_shape": 3,
            "symbol": 2,
            "regime_parameter": 2,
        }
        or CURRICULUM_SELECTION_POLICY_SHA256_V1
        != WO34C_SELECTION_POLICY_SHA256
    ):
        failures.append("selection policy, cooldowns, weights, or catalog bytes differ")
    return AdaptiveCurriculumAuditCase(
        "c_modes_policy_catalog_and_legacy_compatibility_are_fixed",
        (
            f"modes=6 adaptive_policies=4 candidates={len(catalog.candidates)} "
            f"weights_ppm={sum(SELECTION_COMPONENT_WEIGHTS_PPM_V1.values())} "
            f"policy_sha256={CURRICULUM_SELECTION_POLICY_SHA256_V1} "
            "learn_blind_compatible=true assessment_identity_hidden=true"
        ),
        tuple(failures),
    )


def _selection_cold_start_ranking_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    ledger = LearnerEvidenceLedgerV1("learner-selection-cold", ())
    projection = build_learner_projection_v1(ledger, as_of_attempt_ordinal=0)
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.PRACTICE)
    request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        1,
        34_001,
        CurriculumMode.PRACTICE,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        0,
    )
    first = select_curriculum_v1(request, projection, ledger, catalog)
    second = select_curriculum_v1(request, projection, ledger, catalog)
    expected_targets = tuple(
        sorted(
            {item.drill.primary_skill_id for item in catalog.candidates},
            key=lambda value: value.encode("utf-8"),
        )
    )
    selected_eval = next(
        item
        for item in first.candidate_evaluations
        if item.candidate_id == first.selected_candidate_id
    )
    if (
        first.status is not CurriculumSelectionStatusV1.SELECTED
        or not first.cold_start
        or first.target_universe != expected_targets
        or len(first.target_universe) != 10
        or first.canonical_bytes() != second.canonical_bytes()
        or type(first).from_json_bytes(first.canonical_bytes()) != first
        or not _raises(
            lambda: type(first).from_json_bytes(first.canonical_bytes() + b"\n")
        )
        or selected_eval.ranking is None
        or selected_eval.ranking.weakness_ppm != 500_000
        or selected_eval.ranking.uncertainty_ppm != POLICY_SCALE_V1
        or selected_eval.ranking.recency_need_ppm != POLICY_SCALE_V1
        or selected_eval.ranking.recent_variety_need_ppm != POLICY_SCALE_V1
    ):
        failures.append("cold-start universe, exact ranking, or replay differs")
    absorption = tuple(
        item
        for item in first.candidate_evaluations
        if item.primary_skill_id == "ABSORPTION_RECOGNITION"
    )
    if not absorption or any(item.eligible for item in absorption) or any(
        CandidateExclusionV1.PREREQUISITE_NOT_READY not in item.exclusions
        for item in absorption
    ):
        failures.append("cold start bypassed the TAPE_READING prerequisite")
    changed_request = replace(request, root_seed=request.root_seed + 1)
    changed = select_curriculum_v1(changed_request, projection, ledger, catalog)
    common_id = first.ranking_order[0]
    first_tie = next(
        item.ranking.tie_digest
        for item in first.candidate_evaluations
        if item.candidate_id == common_id and item.ranking is not None
    )
    changed_tie = next(
        item.ranking.tie_digest
        for item in changed.candidate_evaluations
        if item.candidate_id == common_id and item.ranking is not None
    )
    if first_tie == changed_tie or request.tie_context != (
        f"WO34/PRACTICE/{request.projection_digest}/1"
    ):
        failures.append("selection root seed or exact tie context is inert")
    guided_catalog = build_legacy_candidate_catalog_v1(CurriculumMode.GUIDED)
    guided_request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        1,
        34_001,
        CurriculumMode.GUIDED,
        guided_catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        0,
    )
    guided_record = select_curriculum_v1(
        guided_request,
        projection,
        ledger,
        guided_catalog,
    )
    if guided_record.selected_skill_id != "AGGRESSIVE_ENTRY" or any(
        item.primary_skill_id != "AGGRESSIVE_ENTRY"
        for item in guided_record.candidate_evaluations
        if item.eligible
    ):
        failures.append("guided cold-start skill ordering differs")
    return AdaptiveCurriculumAuditCase(
        "c_cold_start_ranking_prerequisites_and_seeded_ties_are_exact",
        (
            f"targets={len(first.target_universe)}/10 "
            f"eligible={len(first.eligible_candidate_ids)} "
            f"selected_skill={first.selected_skill_id} "
            f"guided_skill={guided_record.selected_skill_id} "
            "weakness_ppm=500000 uncertainty_ppm=1000000 "
            f"tie_context={request.tie_context}"
        ),
        tuple(failures),
    )


def _selection_prerequisite_and_cooldown_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learner_id = "learner-selection-prerequisite"
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.PRACTICE)
    target = next(
        item
        for item in catalog.candidates
        if item.drill.primary_skill_id == "ABSORPTION_RECOGNITION"
        and item.drill.lesson_id == "07"
    )
    assessments: list[AttemptAssessmentV1] = []
    for ordinal in range(1, 9):
        source = (
            EvidenceSourceClassV1.SYNTHETIC
            if ordinal % 2
            else EvidenceSourceClassV1.HISTORICAL_OR_RECONSTRUCTION
        )
        assessment = _projection_assessment(
            ordinal,
            learner_id=learner_id,
            primary_skill_id="TAPE_READING",
            score_ppm=900_000,
            scenario_index=34_100 + ordinal,
            volume_multiplier_ppm=(500_000, 1_000_000, 2_000_000)[
                (ordinal - 1) % 3
            ],
            liquidity_multiplier_ppm=(500_000, 1_000_000, 2_000_000)[
                (ordinal + 1) % 3
            ],
            source_class=source,
        )
        if ordinal == 8:
            assessment = replace(
                assessment,
                lesson_reference_id=target.candidate_id,
                lesson_digest=target.lesson_digest,
                observable_context=replace(
                    assessment.observable_context,
                    scenario_semantic_sha256=target.scenario_semantic_digest,
                    volume_multiplier_ppm=_band_multiplier(target.volume_band),
                    liquidity_multiplier_ppm=_band_multiplier(target.liquidity_band),
                    source_class=target.source_class,
                ),
            )
        assessments.append(assessment)
    ledger = LearnerEvidenceLedgerV1(learner_id, tuple(assessments))
    projection = build_learner_projection_v1(ledger, as_of_attempt_ordinal=8)
    history = tuple(
        _selection_history_sidecar(
            assessment,
            candidate=(target if assessment.attempt_ordinal == 8 else None),
        )
        for assessment in assessments
    )
    request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        2,
        34_002,
        CurriculumMode.PRACTICE,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        8,
    )
    record = select_curriculum_v1(
        request,
        projection,
        ledger,
        catalog,
        history,
    )
    tape = projection.skill("TAPE_READING")
    target_eval = next(
        item for item in record.candidate_evaluations if item.candidate_id == target.candidate_id
    )
    expected_cooldowns = {
        "lesson_digest",
        "parameter_digest",
        "regime_parameter",
        "scenario_seed",
        "visible_queue_shape",
    }
    if (
        tape.sufficiency is not ProjectionSufficiencyV1.SUFFICIENT
        or tape.mastery_ppm < 650_000
        or tape.confidence_ppm < 500_000
        or not target_eval.prerequisite_checks
        or not all(item.ready for item in target_eval.prerequisite_checks)
        or set(target_eval.cooldown_matches) != expected_cooldowns
        or CandidateExclusionV1.COOLDOWN_SYMBOL in target_eval.exclusions
    ):
        failures.append("prerequisite readiness, cooldown windows, or NA semantics differ")
    if not any(
        item.eligible and item.primary_skill_id == "ABSORPTION_RECOGNITION"
        for item in record.candidate_evaluations
    ):
        failures.append("ready prerequisite did not admit another absorption drill")
    single_catalog = CurriculumCandidateCatalogV1(CurriculumMode.PRACTICE, (target,))
    single_request = replace(request, catalog_digest=single_catalog.catalog_digest)
    exhausted = select_curriculum_v1(
        single_request,
        projection,
        ledger,
        single_catalog,
        history,
    )
    if exhausted.status is not CurriculumSelectionStatusV1.NO_ELIGIBLE_DRILL:
        failures.append("empty cooldown eligibility was relaxed")
    missing = replace(
        target,
        visible_queue_shape=SelectionSemanticValueV1.missing(),
    )
    missing_catalog = CurriculumCandidateCatalogV1(CurriculumMode.PRACTICE, (missing,))
    missing_request = replace(request, catalog_digest=missing_catalog.catalog_digest)
    missing_record = select_curriculum_v1(
        missing_request,
        projection,
        ledger,
        missing_catalog,
        history,
    )
    if CandidateExclusionV1.REQUIRED_METADATA_MISSING not in (
        missing_record.candidate_evaluations[0].exclusions
    ):
        failures.append("missing required semantic metadata remained eligible")
    return AdaptiveCurriculumAuditCase(
        "c_prerequisite_readiness_cooldowns_and_missing_metadata_fail_closed",
        (
            f"tape_mastery_ppm={tape.mastery_ppm} "
            f"tape_confidence_ppm={tape.confidence_ppm} "
            f"cooldown_matches={','.join(target_eval.cooldown_matches)} "
            "not_applicable_symbol_match=false no_relaxation=true "
            "missing_metadata_eligible=false"
        ),
        tuple(failures),
    )


def _selection_manual_plan_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learner_id = "learner-selection-plan"
    ledger = LearnerEvidenceLedgerV1(learner_id, ())
    projection = build_learner_projection_v1(ledger, as_of_attempt_ordinal=0)
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.PRACTICE)
    chosen = next(item for item in catalog.candidates if item.drill.lesson_id == "14")
    plan = CurriculumPlanV1(
        "manual-plan-scope-a",
        learner_id,
        1,
        1,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        (CurriculumPlanEntryV1(1, chosen.lesson_digest, CurriculumMode.PRACTICE),),
    )
    request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        1,
        34_003,
        CurriculumMode.PRACTICE,
        catalog.catalog_digest,
        plan.plan_digest,
        0,
    )
    selected = select_curriculum_v1(
        request,
        projection,
        ledger,
        catalog,
        plans=(plan,),
    )
    if (
        selected.status is not CurriculumSelectionStatusV1.SELECTED
        or selected.manual_plan.status is not ManualPlanStatusV1.APPLIED
        or catalog.candidate(selected.selected_candidate_id).lesson_digest
        != chosen.lesson_digest
        or CurriculumPlanV1.from_json_bytes(plan.canonical_bytes()) != plan
        or not _raises(lambda: setattr(plan, "learner_id", "mutated"))
    ):
        failures.append("valid immutable manual plan did not take exact precedence")
    locked = next(
        item
        for item in catalog.candidates
        if item.drill.primary_skill_id == "ABSORPTION_RECOGNITION"
    )
    invalid_plan = CurriculumPlanV1(
        "manual-plan-scope-locked",
        learner_id,
        1,
        1,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        (CurriculumPlanEntryV1(1, locked.lesson_digest, CurriculumMode.PRACTICE),),
    )
    invalid_request = replace(
        request,
        plan_assignment_digest=invalid_plan.plan_digest,
    )
    refused = select_curriculum_v1(
        invalid_request,
        projection,
        ledger,
        catalog,
        plans=(invalid_plan,),
    )
    if (
        refused.status is not CurriculumSelectionStatusV1.MANUAL_PLAN_REFUSED
        or refused.reason != "PLANNED_LESSON_INELIGIBLE_OR_EXHAUSTED"
        or refused.selected_candidate_id is not None
    ):
        failures.append("ineligible plan silently fell back to adaptive ranking")
    second_plan = CurriculumPlanV1(
        "manual-plan-scope-b",
        learner_id,
        1,
        1,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        (
            CurriculumPlanEntryV1(
                1,
                next(item for item in catalog.candidates if item.drill.lesson_id == "10").lesson_digest,
                CurriculumMode.PRACTICE,
            ),
        ),
    )
    conflict = select_curriculum_v1(
        request,
        projection,
        ledger,
        catalog,
        plans=(plan, second_plan),
    )
    if (
        conflict.status is not CurriculumSelectionStatusV1.MANUAL_PLAN_REFUSED
        or conflict.reason != "MULTIPLE_APPLICABLE_MANUAL_PLANS"
        or len(conflict.manual_plan.plan_digests) != 2
    ):
        failures.append("multiple applicable plans did not refuse selection")
    return AdaptiveCurriculumAuditCase(
        "c_manual_plan_precedence_immutability_and_refusals_are_explicit",
        (
            f"plan_digest={plan.plan_digest} applied=true "
            f"selected_lesson={catalog.candidate(selected.selected_candidate_id).drill.lesson_id} "
            "ineligible_fallback=false conflict_refused=true canonical_roundtrip=true"
        ),
        tuple(failures),
    )


def _selection_remediation_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learner_id = "learner-selection-remediation"
    assessment = _projection_assessment(
        1,
        learner_id=learner_id,
        primary_skill_id="BOOK_READING",
        mode=AttemptModeV1.PRACTICE,
        error_types=(
            LearnerErrorTypeV1.WAITED_PAST_USEFUL_LIQUIDITY,
            LearnerErrorTypeV1.OVERSIZED_RELATIVE_TO_LIQUIDITY,
        ),
    )
    ledger = LearnerEvidenceLedgerV1(learner_id, (assessment,))
    projection = build_learner_projection_v1(ledger, as_of_attempt_ordinal=1)
    history = (_selection_history_sidecar(assessment),)
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.REMEDIATION)
    request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        1,
        34_004,
        CurriculumMode.REMEDIATION,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        1,
    )
    record = select_curriculum_v1(
        request,
        projection,
        ledger,
        catalog,
        history,
    )
    if (
        record.status is not CurriculumSelectionStatusV1.SELECTED
        or record.selected_skill_id != "POSITION_MANAGEMENT"
        or any(
            item.primary_skill_id != "POSITION_MANAGEMENT"
            for item in record.candidate_evaluations
            if item.eligible
        )
    ):
        failures.append("remediation did not use newest-attempt fixed error priority")
    empty_ledger = LearnerEvidenceLedgerV1("learner-remediation-empty", ())
    empty_projection = build_learner_projection_v1(
        empty_ledger,
        as_of_attempt_ordinal=0,
    )
    empty_request = CurriculumSelectionRequestV1(
        projection_digest_v1(empty_projection),
        1,
        34_004,
        CurriculumMode.REMEDIATION,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        0,
    )
    empty = select_curriculum_v1(
        empty_request,
        empty_projection,
        empty_ledger,
        catalog,
    )
    if (
        empty.status is not CurriculumSelectionStatusV1.NO_ELIGIBLE_DRILL
        or empty.reason != "NO_REMEDIATION_ERROR_WITH_ELIGIBLE_DRILL"
    ):
        failures.append("remediation exhaustion did not return NO_ELIGIBLE_DRILL")
    return AdaptiveCurriculumAuditCase(
        "c_remediation_uses_latest_ten_fixed_error_priority_without_fallback",
        (
            "errors=OVERSIZED_RELATIVE_TO_LIQUIDITY,WAITED_PAST_USEFUL_LIQUIDITY "
            f"selected_skill={record.selected_skill_id} latest_window=10 "
            "unmapped_or_unavailable=SKIP exhaustion=NO_ELIGIBLE_DRILL"
        ),
        tuple(failures),
    )


def _selection_assessment_case() -> AdaptiveCurriculumAuditCase:
    failures: list[str] = []
    learner_id = "learner-selection-assessment"
    ledger = LearnerEvidenceLedgerV1(learner_id, ())
    projection = build_learner_projection_v1(ledger, as_of_attempt_ordinal=0)
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.ASSESSMENT)
    request = CurriculumSelectionRequestV1(
        projection_digest_v1(projection),
        1,
        34_005,
        CurriculumMode.ASSESSMENT,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        0,
    )
    first = freeze_assessment_v1(request, projection, ledger, catalog)
    second = freeze_assessment_v1(request, projection, ledger, catalog)
    frozen = first.frozen_assessment
    if (
        first.status is not AssessmentFreezeStatusV1.FROZEN
        or frozen is None
        or second.frozen_assessment is None
        or frozen.canonical_bytes() != second.frozen_assessment.canonical_bytes()
        or type(frozen).from_json_bytes(frozen.canonical_bytes()) != frozen
        or not _raises(
            lambda: type(frozen).from_json_bytes(frozen.canonical_bytes() + b"\n")
        )
        or len(frozen.drills) != ASSESSMENT_BATCH_SIZE_V1
        or len({item.primary_skill_id for item in frozen.drills[:4]}) != 4
        or frozen.preassessment_ledger_digest != ledger.ledger_sha256
    ):
        failures.append("assessment bytes, seed, order, or first-four skill freeze differs")
        return AdaptiveCurriculumAuditCase(
            "c_assessment_freeze_scoring_anti_memorization_and_reveal_are_fixed",
            "assessment_freeze_failed_before_scoring",
            tuple(failures),
        )
    public_view = canonical_json_bytes(frozen.preclosure_view())
    identities = {
        item.primary_skill_id for item in frozen.drills
    } | {
        catalog.candidate(item.candidate_id).drill.title for item in frozen.drills
    }
    if any(identity.encode("ascii") in public_view for identity in identities):
        failures.append("assessment preclosure view leaked lesson or detector identity")
    attempts = tuple(
        _selection_attempt_for_candidate(
            catalog.candidate(item.candidate_id),
            index,
            learner_id=learner_id,
            score_ppm=800_000,
            error_types=(
                (
                    LearnerErrorTypeV1.FAILED_TO_COMPLETE_OBJECTIVE,
                    LearnerErrorTypeV1.WRONG_HOTKEY,
                )
                if index == 1
                else ()
            ),
        )
        for index, item in enumerate(frozen.drills, start=1)
    )
    closure = score_frozen_assessment_v1(frozen, attempts)
    reveal = reveal_closed_assessment_v1(frozen, closure, catalog)
    if (
        closure.score_ppm != 700_000
        or closure.critical_error_record_count != 2
        or closure.passed
        or type(closure).from_json_bytes(closure.canonical_bytes()) != closure
        or reveal["reveal_state"] != "REVEALED_AFTER_CLOSURE"
        or len(reveal["drills"]) != 8
    ):
        failures.append("assessment post-cap average, critical-record count, or reveal differs")
    repeated_lesson = catalog.candidate(frozen.drills[0].candidate_id).lesson_digest
    plan_entries = tuple(
        CurriculumPlanEntryV1(
            ordinal,
            repeated_lesson,
            CurriculumMode.ASSESSMENT,
        )
        for ordinal in range(1, 9)
    )
    invalid_plan = CurriculumPlanV1(
        "assessment-repeat-plan",
        learner_id,
        1,
        8,
        catalog.catalog_digest,
        NOT_APPLICABLE_V1,
        plan_entries,
    )
    planned_request = replace(
        request,
        plan_assignment_digest=invalid_plan.plan_digest,
    )
    planned = freeze_assessment_v1(
        planned_request,
        projection,
        ledger,
        catalog,
        plans=(invalid_plan,),
    )
    if (
        planned.status is not AssessmentFreezeStatusV1.REFUSED
        or planned.reason != "MANUAL_PLAN_REFUSED_ASSESSMENT_LOCKS"
    ):
        failures.append("manual assessment plan bypassed anti-memorization locks")
    return AdaptiveCurriculumAuditCase(
        "c_assessment_freeze_scoring_anti_memorization_and_reveal_are_fixed",
        (
            f"batch={len(frozen.drills)}/8 distinct_first_four=4 "
            f"freeze_sha256={frozen.assessment_digest} "
            f"score_ppm={closure.score_ppm} critical_error_records=2 passed=false "
            "identity_preclosure=HIDDEN reveal=AFTER_CLOSURE "
            "manual_repeat_plan=REFUSED"
        ),
        tuple(failures),
    )


def audit_wo34c_adaptive_curriculum() -> tuple[AdaptiveCurriculumAuditCase, ...]:
    return (
        _selection_modes_and_contract_case(),
        _selection_cold_start_ranking_case(),
        _selection_prerequisite_and_cooldown_case(),
        _selection_manual_plan_case(),
        _selection_remediation_case(),
        _selection_assessment_case(),
    )


def _synthetic_fixture_and_sequence_case(demo) -> AdaptiveCurriculumAuditCase:
    from kirby2.curriculum.adaptive_commands import (
        SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1,
        SYNTHETIC_LEARNER_ENTROPY_DERIVATION_ID_V1,
        SYNTHETIC_LEARNER_FIXTURES_V1,
        SYNTHETIC_PRACTICE_TARGET_SKILLS_V1,
        build_synthetic_learner_evidence_v1,
    )
    from kirby2.pseudonyms import (
        derive_learner_profile_id,
        require_learner_profile_id,
    )

    failures: list[str] = []
    expected_labels = (
        "strong reader / weak execution",
        "weak reader / strong hotkeys",
        "over-aggressive trader",
        "over-passive trader",
        "hidden-liquidity confusion",
        "new learner with insufficient evidence",
    )
    expected_first_routes = {
        "STRONG_READER_WEAK_EXECUTION": "POSITION_MANAGEMENT",
        "WEAK_READER_STRONG_HOTKEYS": "BOOK_READING",
        "OVER_AGGRESSIVE_TRADER": "PASSIVE_ENTRY",
        "OVER_PASSIVE_TRADER": "AGGRESSIVE_ENTRY",
        "HIDDEN_LIQUIDITY_CONFUSION": "LIQUIDITY_WITHDRAWAL",
    }
    if (
        len(SYNTHETIC_LEARNER_FIXTURES_V1) != WO34D_SYNTHETIC_LEARNER_COUNT
        or tuple(item.label for item in SYNTHETIC_LEARNER_FIXTURES_V1)
        != expected_labels
        or tuple(item.fixture for item in demo.learners)
        != SYNTHETIC_LEARNER_FIXTURES_V1
    ):
        failures.append("six fixed synthetic learner identities or ordering differ")
    if len({item.learner_id for item in SYNTHETIC_LEARNER_FIXTURES_V1}) != len(
        SYNTHETIC_LEARNER_FIXTURES_V1
    ):
        failures.append("synthetic learner pseudonyms are not unique")
    for fixture in SYNTHETIC_LEARNER_FIXTURES_V1:
        entropy = hashlib.sha256(
            SYNTHETIC_LEARNER_ENTROPY_DERIVATION_ID_V1.encode("ascii")
            + b"\x00"
            + fixture.fixture_id.encode("ascii")
        ).digest()
        if (
            _raises(lambda fixture=fixture: require_learner_profile_id(
                fixture.learner_id
            ))
            or fixture.learner_id != derive_learner_profile_id(entropy)
        ):
            failures.append(
                f"{fixture.fixture_id} learner ID is not the deterministic opaque pseudonym"
            )
    sequences = tuple(item.selected_skill_sequence for item in demo.learners)
    if len(set(sequences)) != WO34D_SYNTHETIC_LEARNER_COUNT:
        failures.append("synthetic evidence did not produce six distinct sequences")
    for learner in demo.learners:
        fixture = learner.fixture
        fixture_field_names = tuple(fixture.__dataclass_fields__)
        if any(
            token in field_name.lower()
            for field_name in fixture_field_names
            for token in ("expected", "recommendation", "selected")
        ):
            failures.append(
                f"{fixture.fixture_id} injects a recommendation-shaped fixture field"
            )
        ledger, history = build_synthetic_learner_evidence_v1(fixture)
        if fixture.initial_evidence:
            expected_count = (
                SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1
                * len(SYNTHETIC_PRACTICE_TARGET_SKILLS_V1)
            )
            projection = build_learner_projection_v1(
                ledger,
                as_of_attempt_ordinal=ledger.assessments[-1].attempt_ordinal,
            )
            pattern = fixture.pattern_primary_skill_id
            if pattern is None:
                failures.append(
                    f"{fixture.fixture_id} established fixture lacks a pattern skill"
                )
                continue
            pattern_row = projection.skill(pattern)
            other_target_masteries = tuple(
                projection.skill(skill_id).mastery_ppm
                for skill_id in SYNTHETIC_PRACTICE_TARGET_SKILLS_V1
                if skill_id != pattern
            )
            if (
                len(ledger.assessments) != expected_count
                or len(history) != expected_count
                or learner.initial_assessment_count != expected_count
                or {
                    item.observable_context.source_class
                    for item in ledger.assessments
                }
                != {EvidenceSourceClassV1.SYNTHETIC}
                or pattern_row.sufficiency is not ProjectionSufficiencyV1.SUFFICIENT
                or pattern_row.mastery_ppm >= min(other_target_masteries)
                or learner.selected_skill_sequence[0]
                != expected_first_routes[fixture.fixture_id]
            ):
                failures.append(
                    f"{fixture.fixture_id} evidence does not isolate its routed pattern"
                )
            observed_errors = tuple(
                error.error_type
                for assessment in ledger.assessments
                for error in assessment.errors
            )
            if fixture.observed_error_type is None:
                if observed_errors:
                    failures.append(
                        f"{fixture.fixture_id} contains undeclared synthetic errors"
                    )
            elif (
                set(observed_errors) != {fixture.observed_error_type}
                or len(observed_errors) != SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1
            ):
                failures.append(
                    f"{fixture.fixture_id} observed error pattern is not isolated"
                )
        else:
            projection = build_learner_projection_v1(
                ledger,
                as_of_attempt_ordinal=0,
            )
            if (
                ledger.assessments
                or history
                or learner.initial_assessment_count != 0
                or any(
                    item.sufficiency is not ProjectionSufficiencyV1.INSUFFICIENT
                    for item in projection.skill_projections
                )
            ):
                failures.append("new learner fixture contains injected prior evidence")
    return AdaptiveCurriculumAuditCase(
        "d_six_evidence_only_fixtures_route_to_distinct_sequences",
        (
            f"learners={len(demo.learners)} sequences={len(set(sequences))} "
            f"established_assessments={SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1 * len(SYNTHETIC_PRACTICE_TARGET_SKILLS_V1)} "
            "new_learner_assessments=0 source=SYNTHETIC recommendation_fixture_fields=0"
        ),
        tuple(failures),
    )


def _adaptive_replay_and_projection_case(demo) -> AdaptiveCurriculumAuditCase:
    from kirby2.curriculum.adaptive_commands import (
        ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1,
    )

    failures: list[str] = []
    for learner in demo.learners:
        rebuilt = build_learner_projection_v1(
            learner.final_ledger,
            as_of_attempt_ordinal=learner.final_projection.as_of_attempt_ordinal,
        )
        if rebuilt.canonical_bytes() != learner.final_projection.canonical_bytes():
            failures.append(
                f"{learner.fixture.fixture_id} final projection rebuild differs"
            )
        if len(learner.steps) != ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1:
            failures.append(
                f"{learner.fixture.fixture_id} update sequence length differs"
            )
            continue
        expected_pre_ledger = learner.initial_ledger_sha256
        expected_projection = learner.steps[0].projection_digest
        expected_update = learner.initial_assessment_count + 1
        for step in learner.steps:
            explanation = step.explanation
            if (
                step.update_sequence != expected_update
                or step.pre_update_ledger_sha256 != expected_pre_ledger
                or step.projection_digest != expected_projection
                or not step.eligible_candidate_ids
                or step.selected_candidate_id not in step.eligible_candidate_ids
                or explanation.get("selected_skill_id") != step.selected_skill_id
                or explanation.get("model_status")
                != LEARNER_PROJECTION_STATUS_V1
                or not any(
                    "unvalidated" in str(statement).lower()
                    for statement in explanation.get("statements", [])
                )
                or len(step.replay_digest) != 64
            ):
                failures.append(
                    f"{learner.fixture.fixture_id} step {step.selection_ordinal} log or explanation differs"
                )
            expected_update += 1
            expected_pre_ledger = step.post_update_ledger_sha256
            expected_projection = step.post_update_projection_digest
        if (
            expected_pre_ledger != learner.final_ledger.ledger_sha256
            or expected_projection != projection_digest_v1(learner.final_projection)
            or len(learner.replay_digest) != 64
        ):
            failures.append(
                f"{learner.fixture.fixture_id} replay chain does not reach final artifacts"
            )
    if demo.demo_digest != WO34D_DEMO_SHA256:
        failures.append("seed-42 adaptive curriculum demonstration digest differs")
    return AdaptiveCurriculumAuditCase(
        "d_update_projection_selection_and_replay_chain_is_exact",
        (
            f"steps={sum(len(item.steps) for item in demo.learners)} "
            f"demo_sha256={demo.demo_digest} projection_rebuilds=6 "
            "eligible_sets_logged=true rationales_logged=true replays=PASS"
        ),
        tuple(failures),
    )


def _learner_artifact_store_case(demo) -> AdaptiveCurriculumAuditCase:
    from kirby2.research import (
        ArtifactType,
        LearnerArtifactStore,
        RunStore,
        RunType,
    )

    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory) / "research"
        learner_store = LearnerArtifactStore(root)
        repository = Path(__file__).resolve().parents[2]
        manifests = []
        for learner in demo.learners:
            manifest = learner_store.record_update(
                learner.final_ledger,
                learner.final_projection,
                seed=demo.seed,
                repository=repository,
            )
            manifests.append(manifest)
            loaded_ledger, loaded_projection = learner_store.load_update(
                manifest.run_id
            )
            verification = RunStore(root).verify_run(manifest.run_id)
            typed = RunStore(root).query_learner_artifacts(manifest.run_id)
            if (
                manifest.run_type is not RunType.LEARNER_UPDATE
                or tuple(item.artifact_type for item in manifest.artifacts)
                != (
                    ArtifactType.LEARNER_EVIDENCE_UPDATE,
                    ArtifactType.LEARNER_STATE_PROJECTION,
                )
                or loaded_ledger.canonical_bytes()
                != learner.final_ledger.canonical_bytes()
                or loaded_projection.canonical_bytes()
                != learner.final_projection.canonical_bytes()
                or not verification.passed
                or len(typed) != 2
                or {item["artifact_type"] for item in typed}
                != {
                    ArtifactType.LEARNER_EVIDENCE_UPDATE.value,
                    ArtifactType.LEARNER_STATE_PROJECTION.value,
                }
            ):
                failures.append(
                    f"{learner.fixture.fixture_id} learner artifacts did not persist and rebuild"
                )
        first = manifests[0]
        manifest_path = learner_store.run_directory(first.run_id) / "manifest.toml"
        original_manifest = manifest_path.read_bytes()
        repeated = learner_store.record_update(
            demo.learners[0].final_ledger,
            demo.learners[0].final_projection,
            seed=demo.seed,
            repository=repository,
        )
        if (
            repeated.run_id != first.run_id
            or manifest_path.read_bytes() != original_manifest
        ):
            failures.append("idempotent learner persistence rewrote immutable evidence")
        all_typed = RunStore(root).query_learner_artifacts()
        if len(all_typed) != 2 * WO34D_SYNTHETIC_LEARNER_COUNT:
            failures.append("learner artifact catalog projection is incomplete")
        projection_path = (
            learner_store.run_directory(first.run_id) / "learner-projection.json"
        )
        projection_path.write_bytes(projection_path.read_bytes() + b"\n")
        tampered = RunStore(root).verify_run(first.run_id)
        if tampered.passed or tampered.artifact_digests_match or tampered.replay_passed:
            failures.append("learner projection tamper did not fail closed")
    return AdaptiveCurriculumAuditCase(
        "d_typed_learner_update_and_projection_artifacts_rebuild_and_fail_closed",
        (
            f"runs={len(demo.learners)} typed_artifacts={2 * len(demo.learners)} "
            "idempotent=true catalog_projected=true projection_tamper=REFUSED"
        ),
        tuple(failures),
    )


def _adaptive_claim_boundary_case(demo) -> AdaptiveCurriculumAuditCase:
    from kirby2.curriculum.adaptive_commands import (
        ADAPTIVE_ROUTING_CLAIM_V1,
        CROSS_LEARNER_COMPARISON_POLICY_V1,
        render_adaptive_curriculum_demo_v1,
    )

    failures: list[str] = []
    payload = demo.as_dict()
    rendered = render_adaptive_curriculum_demo_v1(demo)
    forbidden_keys: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {
                    "leaderboard",
                    "public_rank",
                    "cross_learner_score",
                    "educational_effectiveness_score",
                }:
                    forbidden_keys.append(str(key))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    exposure_signatures = {
        tuple(sha256_json(list(step.eligible_candidate_ids)) for step in learner.steps)
        for learner in demo.learners
    }
    if (
        payload.get("claim_scope") != ADAPTIVE_ROUTING_CLAIM_V1
        or payload.get("comparison_policy")
        != CROSS_LEARNER_COMPARISON_POLICY_V1
        or payload.get("model_status") != LEARNER_PROJECTION_STATUS_V1
        or len(exposure_signatures) <= 1
        or forbidden_keys
        or "real_weakness_measurement=false" not in rendered
        or "educational_effectiveness=false" not in rendered
        or f"MODEL_STATUS {LEARNER_PROJECTION_STATUS_V1}" not in rendered
    ):
        failures.append(
            "adaptive demonstration overclaims measurement, effectiveness, or comparability"
        )
    return AdaptiveCurriculumAuditCase(
        "d_claims_remain_unvalidated_and_cross_learner_scores_are_not_compared",
        (
            f"claim_scope={ADAPTIVE_ROUTING_CLAIM_V1} "
            f"exposure_signatures={len(exposure_signatures)} "
            "real_measurement=false effectiveness=false leaderboard_fields=0"
        ),
        tuple(failures),
    )


def audit_wo34d_adaptive_curriculum() -> tuple[AdaptiveCurriculumAuditCase, ...]:
    from kirby2.curriculum.adaptive_commands import (
        run_adaptive_curriculum_demo_v1,
    )

    demo = run_adaptive_curriculum_demo_v1(42)
    return (
        _synthetic_fixture_and_sequence_case(demo),
        _adaptive_replay_and_projection_case(demo),
        _learner_artifact_store_case(demo),
        _adaptive_claim_boundary_case(demo),
    )


__all__ = [
    "WO34A_EDGE_COUNT",
    "WO34A_ERROR_COUNT",
    "WO34A_EVIDENCE_FAMILY_COUNT",
    "WO34A_LEGACY_LESSON_COUNT",
    "WO34A_ROOT_COUNT",
    "WO34A_SKILL_COUNT",
    "WO34A_SKILL_GRAPH_SHA256",
    "WO34B_AUDIT_CASE_COUNT",
    "WO34B_PROJECTION_POLICY_SHA256",
    "WO34B_SKILL_PROJECTION_COUNT",
    "WO34C_AUDIT_CASE_COUNT",
    "WO34C_SELECTION_POLICY_SHA256",
    "WO34D_AUDIT_CASE_COUNT",
    "WO34D_DEMO_SHA256",
    "WO34D_SYNTHETIC_LEARNER_COUNT",
    "AdaptiveCurriculumAuditCase",
    "audit_wo34a_adaptive_curriculum",
    "audit_wo34b_adaptive_curriculum",
    "audit_wo34c_adaptive_curriculum",
    "audit_wo34d_adaptive_curriculum",
]
