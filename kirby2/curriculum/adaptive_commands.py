"""Executable deterministic synthetic-learner curriculum demonstration.

The fixtures in this module are behavioral evidence generators.  They declare
observable score/error tendencies and never carry an expected drill, recommendation,
or pass/fail outcome.  The adaptive selector remains solely responsible for routing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.curriculum.errors import LearnerErrorTypeV1, mapped_skill_for_error_v1
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
    EvidenceFamilyV1,
    EvidenceReferenceKindV1,
    EvidenceSourceClassV1,
    LearnerEvidenceLedgerV1,
    ObservableAttemptContextV1,
    OpportunityStateV1,
    SkillEvidenceV1,
    SupportingEvidenceReferenceV1,
)
from kirby2.curriculum.learner import build_learner_projection_v1
from kirby2.curriculum.models import CurriculumMode
from kirby2.curriculum.plans import NOT_APPLICABLE_V1
from kirby2.curriculum.projections import (
    LEARNER_PROJECTION_STATUS_V1,
    LearnerProjectionV1,
    projection_diversity_band_v1,
)
from kirby2.curriculum.selection import (
    CURRICULUM_SELECTION_MODEL_STATUS_V1,
    CurriculumDrillCandidateV1,
    CurriculumSelectionRequestV1,
    CurriculumSelectionStatusV1,
    SelectionHistoryEntryV1,
    SelectionSemanticValueV1,
    build_legacy_candidate_catalog_v1,
    projection_digest_v1,
    select_curriculum_v1,
    selection_history_entry_v1,
)
from kirby2.curriculum.skills import (
    canonical_json_bytes,
    require_stable_skill_v1,
    sha256_json,
)


ADAPTIVE_CURRICULUM_DEMO_SCHEMA_VERSION_V1 = 1
ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1 = 4
SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1 = 16
ADAPTIVE_ROUTING_CLAIM_V1 = (
    "DETERMINISTIC_ROUTING_AND_EXPLANATION_CONSISTENCY_ONLY"
)
CROSS_LEARNER_COMPARISON_POLICY_V1 = (
    "NOT_COMPARABLE_ACROSS_LEARNERS_BECAUSE_SELECTION_CHANGES_EXPOSURE"
)

SYNTHETIC_PRACTICE_TARGET_SKILLS_V1 = (
    "ABSORPTION_RECOGNITION",
    "AGGRESSIVE_ENTRY",
    "BOOK_READING",
    "LIQUIDITY_WITHDRAWAL",
    "PASSIVE_ENTRY",
    "POSITION_MANAGEMENT",
    "QUEUE_POSITION",
    "REGIME_RECOGNITION",
    "TAPE_READING",
    "VOLUME_CONTEXT",
)


@dataclass(frozen=True, slots=True)
class SyntheticLearnerFixtureV1:
    fixture_id: str
    learner_id: str
    label: str
    declared_pattern: str
    initial_evidence: bool
    default_score_ppm: int
    score_overrides: tuple[tuple[str, int], ...]
    pattern_primary_skill_id: str | None
    pattern_supporting_skill_id: str | None
    observed_error_type: LearnerErrorTypeV1 | None

    def __post_init__(self) -> None:
        if not self.fixture_id or not self.learner_id or not self.label:
            raise ValueError("synthetic learner fixture identity is required")
        if not self.declared_pattern:
            raise ValueError("synthetic learner fixture pattern is required")
        if type(self.initial_evidence) is not bool:
            raise TypeError("synthetic learner evidence flag must be boolean")
        if not 0 <= self.default_score_ppm <= 1_000_000:
            raise ValueError("synthetic learner default score must be ppm")
        if self.score_overrides != tuple(
            sorted(self.score_overrides, key=lambda item: item[0].encode("utf-8"))
        ):
            raise ValueError("synthetic score overrides are not canonically ordered")
        if len({item[0] for item in self.score_overrides}) != len(
            self.score_overrides
        ):
            raise ValueError("synthetic score overrides are duplicated")
        for skill_id, score_ppm in self.score_overrides:
            require_stable_skill_v1(skill_id)
            if type(score_ppm) is not int or not 0 <= score_ppm <= 1_000_000:
                raise ValueError("synthetic score override must be integer ppm")
        if self.pattern_primary_skill_id is not None:
            require_stable_skill_v1(self.pattern_primary_skill_id)
        if self.pattern_supporting_skill_id is not None:
            require_stable_skill_v1(self.pattern_supporting_skill_id)
        if self.observed_error_type is not None:
            if self.pattern_primary_skill_id is None:
                raise ValueError("synthetic error requires a primary opportunity skill")
            expected = mapped_skill_for_error_v1(
                self.observed_error_type,
                self.pattern_primary_skill_id,
            )
            if expected != self.pattern_supporting_skill_id:
                raise ValueError("synthetic error mapping differs from its support skill")
        if not self.initial_evidence and (
            self.score_overrides
            or self.pattern_primary_skill_id is not None
            or self.pattern_supporting_skill_id is not None
            or self.observed_error_type is not None
        ):
            raise ValueError("new-learner fixture cannot smuggle prior evidence")

    def score_for(self, skill_id: str) -> int:
        require_stable_skill_v1(skill_id)
        return dict(self.score_overrides).get(skill_id, self.default_score_ppm)

    def as_dict(self) -> dict[str, object]:
        return {
            "behavioral_score_overrides_ppm": dict(self.score_overrides),
            "declared_pattern": self.declared_pattern,
            "default_score_ppm": self.default_score_ppm,
            "fixture_id": self.fixture_id,
            "initial_evidence": self.initial_evidence,
            "label": self.label,
            "learner_id": self.learner_id,
            "observed_error_type": (
                None
                if self.observed_error_type is None
                else self.observed_error_type.value
            ),
            "pattern_primary_skill_id": self.pattern_primary_skill_id,
            "pattern_supporting_skill_id": self.pattern_supporting_skill_id,
        }


SYNTHETIC_LEARNER_FIXTURES_V1 = (
    SyntheticLearnerFixtureV1(
        "STRONG_READER_WEAK_EXECUTION",
        "synthetic-strong-reader-weak-execution",
        "strong reader / weak execution",
        "BOOK_READING_HIGH_AND_POSITION_MANAGEMENT_LOW",
        True,
        800_000,
        (("BOOK_READING", 900_000), ("POSITION_MANAGEMENT", 150_000)),
        "POSITION_MANAGEMENT",
        "BOOK_READING",
        None,
    ),
    SyntheticLearnerFixtureV1(
        "WEAK_READER_STRONG_HOTKEYS",
        "synthetic-weak-reader-strong-hotkeys",
        "weak reader / strong hotkeys",
        "BOOK_READING_LOW_AND_HOTKEY_ACCURACY_HIGH",
        True,
        800_000,
        (("BOOK_READING", 150_000), ("HOTKEY_ACCURACY", 900_000)),
        "BOOK_READING",
        "HOTKEY_ACCURACY",
        None,
    ),
    SyntheticLearnerFixtureV1(
        "OVER_AGGRESSIVE_TRADER",
        "synthetic-over-aggressive-trader",
        "over-aggressive trader",
        "PASSIVE_ENTRY_LOW_WITH_OBSERVED_RED_STATE_ACTIONS",
        True,
        800_000,
        (("PASSIVE_ENTRY", 150_000), ("SCRIPT_DISCIPLINE", 150_000)),
        "PASSIVE_ENTRY",
        "SCRIPT_DISCIPLINE",
        LearnerErrorTypeV1.ACTED_DURING_RED,
    ),
    SyntheticLearnerFixtureV1(
        "OVER_PASSIVE_TRADER",
        "synthetic-over-passive-trader",
        "over-passive trader",
        "AGGRESSIVE_ENTRY_LOW_WITH_OBSERVED_GREEN_STATE_OMISSIONS",
        True,
        800_000,
        (("AGGRESSIVE_ENTRY", 150_000), ("SCRIPT_DISCIPLINE", 150_000)),
        "AGGRESSIVE_ENTRY",
        "SCRIPT_DISCIPLINE",
        LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN,
    ),
    SyntheticLearnerFixtureV1(
        "HIDDEN_LIQUIDITY_CONFUSION",
        "synthetic-hidden-liquidity-confusion",
        "hidden-liquidity confusion",
        "LIQUIDITY_WITHDRAWAL_LOW_WITH_DISPLAYED_DEPTH_CONFUSION",
        True,
        800_000,
        (("HIDDEN_LIQUIDITY", 150_000), ("LIQUIDITY_WITHDRAWAL", 150_000)),
        "LIQUIDITY_WITHDRAWAL",
        "HIDDEN_LIQUIDITY",
        LearnerErrorTypeV1.CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH,
    ),
    SyntheticLearnerFixtureV1(
        "NEW_LEARNER_INSUFFICIENT_EVIDENCE",
        "synthetic-new-learner-insufficient-evidence",
        "new learner with insufficient evidence",
        "NO_PRIOR_ASSESSMENTS",
        False,
        500_000,
        (),
        None,
        None,
        None,
    ),
)


@dataclass(frozen=True, slots=True)
class AdaptiveCurriculumStepV1:
    selection_ordinal: int
    update_sequence: int
    pre_update_ledger_sha256: str
    projection_digest: str
    eligible_candidate_ids: tuple[str, ...]
    selected_candidate_id: str
    selected_skill_id: str
    selection_digest: str
    selection_reason: str
    explanation: dict[str, object]
    ranking: dict[str, object]
    update_assessment_id: str
    post_update_ledger_sha256: str
    post_update_projection_digest: str

    def core_dict(self) -> dict[str, object]:
        return {
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "explanation": self.explanation,
            "post_update_ledger_sha256": self.post_update_ledger_sha256,
            "post_update_projection_digest": self.post_update_projection_digest,
            "pre_update_ledger_sha256": self.pre_update_ledger_sha256,
            "projection_digest": self.projection_digest,
            "ranking": self.ranking,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_skill_id": self.selected_skill_id,
            "selection_digest": self.selection_digest,
            "selection_ordinal": self.selection_ordinal,
            "selection_reason": self.selection_reason,
            "update_assessment_id": self.update_assessment_id,
            "update_sequence": self.update_sequence,
        }

    @property
    def replay_digest(self) -> str:
        return sha256_json(self.core_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.core_dict(), "replay_digest": self.replay_digest}


@dataclass(frozen=True, slots=True)
class SyntheticLearnerDemoResultV1:
    fixture: SyntheticLearnerFixtureV1
    initial_assessment_count: int
    initial_ledger_sha256: str
    steps: tuple[AdaptiveCurriculumStepV1, ...]
    final_ledger: LearnerEvidenceLedgerV1
    final_projection: LearnerProjectionV1
    final_history: tuple[SelectionHistoryEntryV1, ...]

    @property
    def selected_skill_sequence(self) -> tuple[str, ...]:
        return tuple(item.selected_skill_id for item in self.steps)

    def core_dict(self) -> dict[str, object]:
        return {
            "claim_scope": ADAPTIVE_ROUTING_CLAIM_V1,
            "comparison_policy": CROSS_LEARNER_COMPARISON_POLICY_V1,
            "final_assessment_count": len(self.final_ledger.assessments),
            "final_ledger_sha256": self.final_ledger.ledger_sha256,
            "final_projection_digest": projection_digest_v1(self.final_projection),
            "fixture": self.fixture.as_dict(),
            "initial_assessment_count": self.initial_assessment_count,
            "initial_ledger_sha256": self.initial_ledger_sha256,
            "model_status": LEARNER_PROJECTION_STATUS_V1,
            "selected_skill_sequence": list(self.selected_skill_sequence),
            "steps": [item.as_dict() for item in self.steps],
        }

    @property
    def replay_digest(self) -> str:
        return sha256_json(self.core_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.core_dict(), "replay_digest": self.replay_digest}


@dataclass(frozen=True, slots=True)
class AdaptiveCurriculumDemoV1:
    seed: int
    learners: tuple[SyntheticLearnerDemoResultV1, ...]
    schema_version: int = ADAPTIVE_CURRICULUM_DEMO_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("adaptive curriculum demo seed must be nonnegative")
        if tuple(item.fixture for item in self.learners) != SYNTHETIC_LEARNER_FIXTURES_V1:
            raise ValueError("adaptive curriculum demo fixture inventory differs")
        if self.schema_version != ADAPTIVE_CURRICULUM_DEMO_SCHEMA_VERSION_V1:
            raise ValueError("adaptive curriculum demo schema version differs")

    def core_dict(self) -> dict[str, object]:
        return {
            "claim_scope": ADAPTIVE_ROUTING_CLAIM_V1,
            "comparison_policy": CROSS_LEARNER_COMPARISON_POLICY_V1,
            "learner_count": len(self.learners),
            "learners": [item.as_dict() for item in self.learners],
            "model_status": CURRICULUM_SELECTION_MODEL_STATUS_V1,
            "record_kind": "ADAPTIVE_CURRICULUM_DEMO_V1",
            "schema_version": self.schema_version,
            "seed": self.seed,
        }

    @property
    def demo_digest(self) -> str:
        return sha256_json(self.core_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.core_dict(), "demo_digest": self.demo_digest}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def _reference(
    fixture: SyntheticLearnerFixtureV1,
    ordinal: int,
    kind: EvidenceReferenceKindV1,
    role: str,
    family: EvidenceFamilyV1 | None = None,
) -> SupportingEvidenceReferenceV1:
    identity = {
        "fixture_id": fixture.fixture_id,
        "kind": kind.value,
        "ordinal": ordinal,
        "role": role,
    }
    return SupportingEvidenceReferenceV1(
        kind,
        f"synthetic-{fixture.fixture_id.lower()}-{ordinal}-{role}",
        sha256_json(identity),
        family,
    )


def _evidence_family(skill_id: str) -> EvidenceFamilyV1:
    if skill_id == "HOTKEY_ACCURACY":
        return EvidenceFamilyV1.HOTKEY_ERROR
    if skill_id == "HIDDEN_LIQUIDITY":
        return EvidenceFamilyV1.QUEUE_MISUNDERSTANDING
    if skill_id in {
        "AGGRESSIVE_ENTRY",
        "PASSIVE_ENTRY",
        "POSITION_MANAGEMENT",
        "SCRIPT_DISCIPLINE",
    }:
        return EvidenceFamilyV1.DISCIPLINE_COMPLIANCE
    return EvidenceFamilyV1.CORRECT_CLASSIFICATION


def _study_timestamp(ordinal: int) -> str:
    value = datetime(2026, 8, 29, tzinfo=timezone.utc) + timedelta(seconds=ordinal)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _band_multiplier(value) -> int:
    return {"LOW": 500_000, "NORMAL": 1_000_000, "HIGH": 1_500_000}[
        value.value
    ]


def _assessment(
    fixture: SyntheticLearnerFixtureV1,
    *,
    ordinal: int,
    primary_skill_id: str,
    sample_index: int,
    candidate: CurriculumDrillCandidateV1 | None = None,
) -> AttemptAssessmentV1:
    patterned = primary_skill_id == fixture.pattern_primary_skill_id
    supporting = (
        ()
        if not patterned or fixture.pattern_supporting_skill_id is None
        else (fixture.pattern_supporting_skill_id,)
    )
    errors = (
        ()
        if not patterned or fixture.observed_error_type is None
        else (fixture.observed_error_type,)
    )
    error_type = None if not errors else errors[0]
    no_action = error_type is LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN
    reference_state = (
        OpportunityStateV1.RED
        if error_type is LearnerErrorTypeV1.ACTED_DURING_RED
        else OpportunityStateV1.GREEN
    )
    activation_us = ordinal * 10_000
    if candidate is None:
        volumes = (500_000, 1_000_000, 1_500_000)
        liquidities = (1_500_000, 500_000, 1_000_000)
        volume_multiplier = volumes[sample_index % len(volumes)]
        liquidity_multiplier = liquidities[sample_index % len(liquidities)]
        source_class = EvidenceSourceClassV1.SYNTHETIC
        scenario_digest = sha256_json(
            {
                "fixture_id": fixture.fixture_id,
                "primary_skill_id": primary_skill_id,
                "scenario_variant": sample_index % 4,
            }
        )
        lesson_reference_id = (
            f"synthetic-baseline-{fixture.fixture_id.lower()}-"
            f"{primary_skill_id.lower()}-{sample_index}"
        )
        lesson_digest = sha256_json(
            {
                "fixture_id": fixture.fixture_id,
                "primary_skill_id": primary_skill_id,
                "sample_index": sample_index,
                "synthetic_lesson": 1,
            }
        )
        mode = AttemptModeV1.ASSESSMENT
    else:
        volume_multiplier = _band_multiplier(candidate.volume_band)
        liquidity_multiplier = _band_multiplier(candidate.liquidity_band)
        source_class = candidate.source_class
        scenario_digest = candidate.scenario_semantic_digest
        lesson_reference_id = candidate.candidate_id
        lesson_digest = candidate.lesson_digest
        mode = AttemptModeV1(candidate.drill.mode.value)

    policy = SCORING_POLICY_REGISTRY_V1["LEGACY_OBJECTIVE_SCORING_V1"]
    skill_ids = tuple(sorted({primary_skill_id, *supporting}))
    skill_evidence = tuple(
        SkillEvidenceV1(
            skill_id=skill_id,
            opportunity_present=True,
            observable=True,
            score_ppm=fixture.score_for(skill_id),
            scoring_policy_id=policy.policy_id,
            scoring_policy_digest=policy.policy_digest,
            supporting_evidence_references=(
                _reference(
                    fixture,
                    ordinal,
                    EvidenceReferenceKindV1.SCORING_INPUT,
                    f"score-{skill_id.lower()}",
                    _evidence_family(skill_id),
                ),
            ),
        )
        for skill_id in skill_ids
    )
    error_records = tuple(
        AttemptErrorRecordV1(
            item,
            mapped_skill_for_error_v1(item, primary_skill_id),
            (
                _reference(
                    fixture,
                    ordinal,
                    EvidenceReferenceKindV1.SCORING_INPUT,
                    f"error-{item.value.lower()}",
                    _evidence_family(
                        mapped_skill_for_error_v1(item, primary_skill_id)
                        or primary_skill_id
                    ),
                ),
            ),
        )
        for item in sorted(errors, key=lambda value: value.value)
    )
    return AttemptAssessmentV1(
        learner_id=fixture.learner_id,
        attempt_ordinal=ordinal,
        lesson_reference_id=lesson_reference_id,
        lesson_digest=lesson_digest,
        primary_skill_id=primary_skill_id,
        supporting_skill_ids=tuple(sorted(supporting)),
        mode=mode,
        opportunity=AttemptOpportunityV1(
            opportunity_id=f"synthetic-opportunity-{fixture.fixture_id.lower()}-{ordinal}",
            opportunity_present=True,
            observable=True,
            reaction_time_sufficient=True,
            reference_state=reference_state,
            activation_us=activation_us,
            reaction_deadline_us=activation_us + 2_000,
            supporting_evidence_references=(
                _reference(
                    fixture,
                    ordinal,
                    EvidenceReferenceKindV1.OPPORTUNITY,
                    "opportunity",
                ),
            ),
        ),
        action=AttemptActionV1(
            action_id=f"synthetic-action-{fixture.fixture_id.lower()}-{ordinal}",
            action_kind=(
                AttemptActionKindV1.NO_ACTION
                if no_action
                else AttemptActionKindV1.CLASSIFICATION
            ),
            occurred_us=None if no_action else activation_us + 500,
            action_sha256=(
                None
                if no_action
                else sha256_json(
                    {"fixture_id": fixture.fixture_id, "ordinal": ordinal}
                )
            ),
            supporting_evidence_references=(
                _reference(
                    fixture,
                    ordinal,
                    EvidenceReferenceKindV1.ACTION,
                    "action",
                ),
            ),
        ),
        observable_context=ObservableAttemptContextV1(
            context_id=f"synthetic-context-{fixture.fixture_id.lower()}-{ordinal}",
            scenario_semantic_sha256=scenario_digest,
            volume_multiplier_ppm=volume_multiplier,
            liquidity_multiplier_ppm=liquidity_multiplier,
            source_class=source_class,
            simulation_time_us=activation_us,
            supporting_evidence_references=(
                _reference(
                    fixture,
                    ordinal,
                    EvidenceReferenceKindV1.OBSERVABLE_CONTEXT,
                    "context",
                ),
            ),
        ),
        scoring_policy_id=policy.policy_id,
        scoring_policy_digest=policy.policy_digest,
        skill_evidence=skill_evidence,
        errors=error_records,
        ambiguity=AttemptAmbiguityV1.NONE,
        evidence_sufficiency=AttemptEvidenceSufficiencyV1.SUFFICIENT,
        auxiliary_outcomes=(),
        study_timestamp_utc=_study_timestamp(ordinal),
    )


def _baseline_history_entry(
    assessment: AttemptAssessmentV1,
    fixture: SyntheticLearnerFixtureV1,
) -> SelectionHistoryEntryV1:
    context = assessment.observable_context
    ordinal = assessment.attempt_ordinal
    return SelectionHistoryEntryV1(
        assessment_id=assessment.assessment_id,
        attempt_ordinal=ordinal,
        lesson_digest=assessment.lesson_digest,
        primary_skill_id=assessment.primary_skill_id,
        parameter_digest=sha256_json(
            {"fixture_id": fixture.fixture_id, "parameter_ordinal": ordinal}
        ),
        scenario_semantic_digest=context.scenario_semantic_sha256,
        scenario_seed=SelectionSemanticValueV1.concrete(str(340_000 + ordinal)),
        visible_queue_shape=SelectionSemanticValueV1.concrete(
            sha256_json(
                {"fixture_id": fixture.fixture_id, "queue_ordinal": ordinal}
            )
        ),
        symbol=SelectionSemanticValueV1.not_applicable(),
        regime_parameter=SelectionSemanticValueV1.concrete(
            sha256_json(
                {"fixture_id": fixture.fixture_id, "regime_ordinal": ordinal}
            )
        ),
        volume_band=projection_diversity_band_v1(context.volume_multiplier_ppm),
        liquidity_band=projection_diversity_band_v1(
            context.liquidity_multiplier_ppm
        ),
        source_class=context.source_class,
    )


def build_synthetic_learner_evidence_v1(
    fixture: SyntheticLearnerFixtureV1,
) -> tuple[LearnerEvidenceLedgerV1, tuple[SelectionHistoryEntryV1, ...]]:
    """Build immutable observations from a declared pattern, not a recommendation."""

    if not isinstance(fixture, SyntheticLearnerFixtureV1):
        raise TypeError("synthetic evidence requires a typed fixture")
    if not fixture.initial_evidence:
        return LearnerEvidenceLedgerV1(fixture.learner_id, ()), ()
    assessments: list[AttemptAssessmentV1] = []
    ordinal = 0
    for sample_index in range(SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1):
        for primary_skill_id in SYNTHETIC_PRACTICE_TARGET_SKILLS_V1:
            ordinal += 1
            assessments.append(
                _assessment(
                    fixture,
                    ordinal=ordinal,
                    primary_skill_id=primary_skill_id,
                    sample_index=sample_index,
                )
            )
    ledger = LearnerEvidenceLedgerV1(fixture.learner_id, tuple(assessments))
    history = tuple(
        _baseline_history_entry(item, fixture) for item in ledger.assessments
    )
    return ledger, history


def _fixture_root_seed(seed: int, fixture_id: str) -> int:
    raw = hashlib.sha256(
        f"WO34-D/{seed}/{fixture_id}".encode("ascii")
    ).digest()
    return int.from_bytes(raw[:8], "big")


def _execute_fixture(
    fixture: SyntheticLearnerFixtureV1,
    *,
    seed: int,
    catalog,
) -> SyntheticLearnerDemoResultV1:
    ledger, history = build_synthetic_learner_evidence_v1(fixture)
    initial_assessment_count = len(ledger.assessments)
    initial_ledger_sha256 = ledger.ledger_sha256
    steps: list[AdaptiveCurriculumStepV1] = []
    for selection_ordinal in range(
        1,
        ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1 + 1,
    ):
        as_of = 0 if not ledger.assessments else ledger.assessments[-1].attempt_ordinal
        projection = build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=as_of,
        )
        projection_digest = projection_digest_v1(projection)
        request = CurriculumSelectionRequestV1(
            projection_digest=projection_digest,
            selection_ordinal=selection_ordinal,
            root_seed=_fixture_root_seed(seed, fixture.fixture_id),
            mode=CurriculumMode.PRACTICE,
            catalog_digest=catalog.catalog_digest,
            plan_assignment_digest=NOT_APPLICABLE_V1,
            as_of_attempt_ordinal=as_of,
        )
        selection = select_curriculum_v1(
            request,
            projection,
            ledger,
            catalog,
            history,
        )
        if (
            selection.status is not CurriculumSelectionStatusV1.SELECTED
            or selection.selected_candidate_id is None
            or selection.selected_skill_id is None
        ):
            raise RuntimeError(
                f"synthetic learner selection refused: {selection.reason}"
            )
        candidate = catalog.candidate(selection.selected_candidate_id)
        selected_evaluation = next(
            item
            for item in selection.candidate_evaluations
            if item.candidate_id == selection.selected_candidate_id
        )
        if selected_evaluation.ranking is None:
            raise RuntimeError("selected synthetic drill lacks a ranking rationale")
        update_sequence = as_of + 1
        update = _assessment(
            fixture,
            ordinal=update_sequence,
            primary_skill_id=candidate.drill.primary_skill_id,
            sample_index=1_000 + selection_ordinal,
            candidate=candidate,
        )
        pre_update_ledger_sha256 = ledger.ledger_sha256
        ledger = ledger.append(update)
        history = (*history, selection_history_entry_v1(update, candidate))
        post_projection = build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=update_sequence,
        )
        steps.append(
            AdaptiveCurriculumStepV1(
                selection_ordinal=selection_ordinal,
                update_sequence=update_sequence,
                pre_update_ledger_sha256=pre_update_ledger_sha256,
                projection_digest=projection_digest,
                eligible_candidate_ids=selection.eligible_candidate_ids,
                selected_candidate_id=candidate.candidate_id,
                selected_skill_id=candidate.drill.primary_skill_id,
                selection_digest=selection.selection_digest,
                selection_reason=selection.reason,
                explanation=selection.explanation.as_dict(),
                ranking=selected_evaluation.ranking.as_dict(),
                update_assessment_id=update.assessment_id,
                post_update_ledger_sha256=ledger.ledger_sha256,
                post_update_projection_digest=projection_digest_v1(post_projection),
            )
        )
    final_ordinal = ledger.assessments[-1].attempt_ordinal
    final_projection = build_learner_projection_v1(
        ledger,
        as_of_attempt_ordinal=final_ordinal,
    )
    return SyntheticLearnerDemoResultV1(
        fixture=fixture,
        initial_assessment_count=initial_assessment_count,
        initial_ledger_sha256=initial_ledger_sha256,
        steps=tuple(steps),
        final_ledger=ledger,
        final_projection=final_projection,
        final_history=history,
    )


def run_adaptive_curriculum_demo_v1(seed: int = 42) -> AdaptiveCurriculumDemoV1:
    """Execute every fixture twice and refuse any replay discrepancy."""

    if type(seed) is not int or seed < 0:
        raise ValueError("adaptive curriculum demo seed must be nonnegative")
    catalog = build_legacy_candidate_catalog_v1(CurriculumMode.PRACTICE)
    target_skills = tuple(
        sorted(
            {item.drill.primary_skill_id for item in catalog.candidates},
            key=lambda item: item.encode("utf-8"),
        )
    )
    if target_skills != SYNTHETIC_PRACTICE_TARGET_SKILLS_V1:
        raise RuntimeError("practice catalog target skills differ from the demo contract")
    learners: list[SyntheticLearnerDemoResultV1] = []
    for fixture in SYNTHETIC_LEARNER_FIXTURES_V1:
        first = _execute_fixture(fixture, seed=seed, catalog=catalog)
        replay = _execute_fixture(fixture, seed=seed, catalog=catalog)
        if (
            first.core_dict() != replay.core_dict()
            or first.final_ledger.canonical_bytes()
            != replay.final_ledger.canonical_bytes()
            or first.final_projection.canonical_bytes()
            != replay.final_projection.canonical_bytes()
        ):
            raise RuntimeError(
                f"adaptive curriculum replay diverged for {fixture.fixture_id}"
            )
        learners.append(first)
    return AdaptiveCurriculumDemoV1(seed, tuple(learners))


def render_adaptive_curriculum_demo_v1(
    demo: AdaptiveCurriculumDemoV1,
    persistence: tuple[tuple[str, str, int, bool], ...] = (),
) -> str:
    lines = [
        "KIRBY2_ADAPTIVE_CURRICULUM_DEMO",
        f"SEED {demo.seed}",
        f"MODEL_STATUS {CURRICULUM_SELECTION_MODEL_STATUS_V1}",
        f"CLAIM_SCOPE {ADAPTIVE_ROUTING_CLAIM_V1}",
        f"COMPARISON_POLICY {CROSS_LEARNER_COMPARISON_POLICY_V1}",
    ]
    for learner in demo.learners:
        sequence = ",".join(learner.selected_skill_sequence)
        lines.append(
            f"LEARNER fixture={learner.fixture.fixture_id} "
            f'label="{learner.fixture.label}" '
            f"initial_assessments={learner.initial_assessment_count} "
            f"selected_skills={sequence}"
        )
        for step in learner.steps:
            components = step.ranking["components_ppm"]
            if not isinstance(components, dict):
                raise TypeError("adaptive curriculum ranking components are invalid")
            eligible_sha256 = sha256_json(list(step.eligible_candidate_ids))
            lines.append(
                f"STEP fixture={learner.fixture.fixture_id} "
                f"selection={step.selection_ordinal} update={step.update_sequence} "
                f"projection={step.projection_digest} "
                f"eligible_drills={len(step.eligible_candidate_ids)} "
                f"eligible_sha256={eligible_sha256} "
                f"selected={step.selected_candidate_id} "
                f"skill={step.selected_skill_id} reason={step.selection_reason}"
            )
            lines.append(
                f"RATIONALE fixture={learner.fixture.fixture_id} "
                f"selection={step.selection_ordinal} "
                f"weakness_ppm={components['weakness']} "
                f"uncertainty_ppm={components['uncertainty']} "
                f"ranking_score_ppm={step.ranking['score_ppm']} "
                f"selection_digest={step.selection_digest}"
            )
            lines.append(
                f"UPDATE fixture={learner.fixture.fixture_id} "
                f"sequence={step.update_sequence} "
                f"assessment={step.update_assessment_id} "
                f"ledger={step.post_update_ledger_sha256} "
                f"post_projection={step.post_update_projection_digest} "
                f"replay={step.replay_digest}"
            )
        lines.append(
            f"REPLAY fixture={learner.fixture.fixture_id} "
            f"digest={learner.replay_digest} status=PASS"
        )
    for fixture_id, run_id, artifact_count, passed in persistence:
        lines.append(
            f"PERSISTENCE fixture={fixture_id} run_id={run_id} "
            f"typed_artifacts={artifact_count} "
            f"verification={'PASS' if passed else 'FAIL'}"
        )
    lines.append(f"DEMO_DIGEST {demo.demo_digest}")
    lines.append(
        f"ADAPTIVE_CURRICULUM_DEMO PASS learners={len(demo.learners)} "
        f"steps={sum(len(item.steps) for item in demo.learners)} "
        "real_weakness_measurement=false educational_effectiveness=false"
    )
    return "\n".join(lines)


def _nonnegative_seed(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("seed must be an integer") from error
    if selected < 0:
        raise argparse.ArgumentTypeError("seed must be nonnegative")
    return selected


def _configure_adaptive_curriculum_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=_nonnegative_seed, default=42)


def _handle_adaptive_curriculum_demo(args: argparse.Namespace) -> int:
    from kirby2.research import LearnerArtifactStore, RunStore

    try:
        demo = run_adaptive_curriculum_demo_v1(args.seed)
        persistence: list[tuple[str, str, int, bool]] = []
        with tempfile.TemporaryDirectory(
            prefix="kirby2-adaptive-curriculum-demo-"
        ) as temporary:
            root = Path(temporary) / "research"
            learner_store = LearnerArtifactStore(root)
            repository = Path(__file__).resolve().parents[2]
            for learner in demo.learners:
                manifest = learner_store.record_update(
                    learner.final_ledger,
                    learner.final_projection,
                    seed=demo.seed,
                    repository=repository,
                )
                research_store = RunStore(root)
                verification = research_store.verify_run(manifest.run_id)
                typed_artifacts = research_store.query_learner_artifacts(
                    manifest.run_id
                )
                persistence.append(
                    (
                        learner.fixture.fixture_id,
                        manifest.run_id,
                        len(typed_artifacts),
                        verification.passed,
                    )
                )
        print(render_adaptive_curriculum_demo_v1(demo, tuple(persistence)))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"ADAPTIVE_CURRICULUM_DEMO_ERROR {error}", file=sys.stderr)
        return 1
    return 0 if all(item[3] for item in persistence) else 1


ADAPTIVE_CURRICULUM_COMMAND_MODULE = CommandModule(
    module_id="ADAPTIVE_CURRICULUM",
    commands=(
        CommandSpec(
            command_id="ADAPTIVE_CURRICULUM_DEMO",
            name="adaptive-curriculum-demo",
            help="run six deterministic synthetic learner routing sequences",
            handler=_handle_adaptive_curriculum_demo,
            configure=_configure_adaptive_curriculum_demo,
        ),
    ),
)


__all__ = [
    "ADAPTIVE_CURRICULUM_COMMAND_MODULE",
    "ADAPTIVE_CURRICULUM_DEMO_SCHEMA_VERSION_V1",
    "ADAPTIVE_CURRICULUM_DEMO_SEQUENCE_LENGTH_V1",
    "ADAPTIVE_ROUTING_CLAIM_V1",
    "CROSS_LEARNER_COMPARISON_POLICY_V1",
    "SYNTHETIC_INITIAL_EVIDENCE_ROUNDS_V1",
    "SYNTHETIC_LEARNER_FIXTURES_V1",
    "SYNTHETIC_PRACTICE_TARGET_SKILLS_V1",
    "AdaptiveCurriculumDemoV1",
    "AdaptiveCurriculumStepV1",
    "SyntheticLearnerDemoResultV1",
    "SyntheticLearnerFixtureV1",
    "build_synthetic_learner_evidence_v1",
    "render_adaptive_curriculum_demo_v1",
    "run_adaptive_curriculum_demo_v1",
]
