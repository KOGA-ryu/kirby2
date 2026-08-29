"""Executable WO34-A audit for immutable learner evidence and skill graph."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

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
    SkillEvidenceV1,
    SupportingEvidenceReferenceV1,
    require_scoring_policy_v1,
)
from kirby2.curriculum.models import CurriculumDrill, CurriculumMode
from kirby2.curriculum.skills import (
    PREREQUISITE_CONFIDENCE_MIN_PPM_V1,
    PREREQUISITE_MASTERY_MIN_PPM_V1,
    PREREQUISITE_POLICY_ID_V1,
    SKILL_GRAPH_V1,
    STABLE_SKILL_REGISTRY_SHA256_V1,
    SkillGraphV1,
    SkillPrerequisiteEdgeV1,
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


__all__ = [
    "WO34A_EDGE_COUNT",
    "WO34A_ERROR_COUNT",
    "WO34A_EVIDENCE_FAMILY_COUNT",
    "WO34A_LEGACY_LESSON_COUNT",
    "WO34A_ROOT_COUNT",
    "WO34A_SKILL_COUNT",
    "WO34A_SKILL_GRAPH_SHA256",
    "AdaptiveCurriculumAuditCase",
    "audit_wo34a_adaptive_curriculum",
]
