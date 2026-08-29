"""Executable WO33-A audit for lesson-candidate contract boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from kirby2.audit.historical_lessons import audit_historical_lessons
from kirby2.historical import (
    MinedCheckpointReferenceV1,
    MinedSourceRunReferenceV1,
)
from kirby2.mining import (
    DIFFICULTY_ESTIMATE_STATE_V1,
    DIVERSITY_DIMENSIONS_V1,
    DIVERSITY_WEIGHTS_PPM_V1,
    POSITIVE_INFINITY_V1,
    RESERVED_COUNTS_V1,
    REVIEW_TARGET_COUNT_V1,
    DETECTOR_IDS_V1,
    DETECTOR_REGISTRY_V1,
    SKILL_REGISTRY_V1,
    STABLE_SKILL_IDS_V1,
    CandidateBoundsV1,
    CandidateDirectionV1,
    CandidateKeyV1,
    CandidatePresentationModeV1,
    CandidateSideV1,
    CapabilityEvidenceKindV1,
    CapabilityEvidenceReferenceV1,
    CapabilityRecordRowV1,
    CheckpointReferenceV1,
    DetectorProjectionV1,
    DetectorThresholdsManifestV1,
    DetectorSupportStatusV1,
    DifficultyProjectionV1,
    EvidenceClassV1,
    GroundTruthSummaryV1,
    HumanReviewDecisionV1,
    HumanReviewSidecarV1,
    LessonCandidateV1,
    MiningPlanManifestV1,
    MiningPolicyBundleV1,
    ObservableFeatureSummaryV1,
    ObserveClassifyObjectiveV1,
    RarityProjectionV1,
    RegimeSignatureV1,
    RevealMaterialV1,
    SourceAncestryV1,
    SourceCapabilityInventoryV1,
    SourceIdentityV1,
    SourceKindV1,
    SourceWindowOutcomeV1,
    QualificationSourcesManifestV1,
    FrequencyReportV1,
    MinedLessonAssessmentV1,
    MinedLessonPlayerActionV1,
    MinedLessonRevealGrantV1,
    RecordedClientFeedEventV1,
    RecordedLessonSourceV1,
    SignalClauseOrientationV1,
    aggressive_conflict_ppm,
    and_legibility_ppm,
    boolean_legibility_ppm,
    build_difficulty_projection,
    build_playable_lesson_v1,
    build_player_overlay_v1,
    build_regime_signature_v1,
    canonical_event_token_v1,
    canonical_feature_value_v1,
    canonical_json_bytes,
    compare_candidates,
    coverage_counts_v1,
    deduplicate_candidates,
    difficulty_band_v1,
    duration_legibility_ppm,
    event_five_grams_v1,
    event_price_relation_v1,
    extract_observable_lesson_source_v1,
    jaccard_ppm,
    load_mining_policy_bundle,
    lower_bound_legibility_ppm,
    marginal_diversity_v1,
    observable_feature_token_v1,
    observable_feature_tokens_v1,
    or_legibility_ppm,
    orient_signal_magnitude_v1,
    rank_candidates,
    replay_player_overlay_v1,
    round_div_even,
    select_technical_review_candidates,
    sha256_json,
    spread_band_v1,
    time_iou_ppm,
    upper_bound_legibility_ppm,
    source_window_outcome_v1,
    assessment_replay_sha256_v1,
)


WO33A_DETECTOR_COUNT = 22
WO33A_SKILL_COUNT = 23
WO33A_IDENTITY_KEY_COUNT = 21
WO33A_REVIEW_DECISION_COUNT = 4
WO33A1_SOURCE_COUNT = 5
WO33A1_REVIEW_TARGET_COUNT = 20
WO33B1_DETECTOR_COUNT = 15
WO33B1_SYNTHETIC_REPORT_SHA256 = (
    "66a3de3f170a9c21d401489685514edc505f1de4ffbd14607caee32c247c8516"
)
WO33B2_DETECTOR_COUNT = 7
WO33B2_SYNTHETIC_REPORT_SHA256 = (
    "832b09a7b459a3d2404b3a39e3100d6405d8d392628e9c77d25e89536c3fcac2"
)
WO33C_DIFFICULTY_COMPONENT_COUNT = 11
WO33C_DIVERSITY_DIMENSION_COUNT = 6
WO33C_REVIEW_TARGET_COUNT = 20
WO33D_SOURCE_LINEAGE_FIELD_COUNT = 7
WO33D_ASSESSMENT_FIELD_COUNT = 12

WO33A1_THRESHOLD_MANIFEST_SHA256 = (
    "4996ddce777527cf5350f3eaaeeff83911d8dd95dc510c704411ec7d8f708899"
)
WO33A1_SOURCE_MANIFEST_SHA256 = (
    "ff0cb292d1ed764b73f197462cd49c0c8a345fffcd547bab1c60b726b7d5eda5"
)
WO33A1_MINING_PLAN_MANIFEST_SHA256 = (
    "e1cc07dc1a8dffb5110a2987b6a1f2534d42ad6fc221517446ef327143f0cf7e"
)
WO33A1_POLICY_BUNDLE_SHA256 = (
    "e5986bca08593cf2ac933a924e4895886d55b48e258690d8f3ef3d31cb5383ec"
)


@dataclass(frozen=True, slots=True)
class DrillMiningAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    required: bool = True

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "failures": list(self.failures),
            "name": self.name,
            "required": self.required,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Validate contracts only; no detector implementation or mining is invoked."""

    return (
        _candidate_identity_and_ancestry_case(),
        _boundary_and_review_sidecar_case(),
        _content_addressing_and_truth_access_case(),
        _versioned_skill_and_objective_case(),
        _detector_capability_admission_case(),
        _assessment_reveal_and_historical_regression_case(),
    )


def audit_wo33a1_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Validate preregistration and source identity without mining candidates."""

    bundle = load_mining_policy_bundle()
    return (
        _a1_detector_threshold_manifest_case(bundle),
        _a1_difficulty_sampling_and_shortfall_case(bundle),
        _a1_dedup_diversity_and_review_case(bundle),
        _a1_qualification_source_matrix_case(bundle),
        _a1_source_replay_identity_case(bundle),
        _a1_unexercised_and_hostile_refusal_case(bundle),
    )


def audit_wo33b1_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Exercise B1 detector rules without mining the protected source matrix."""

    runtime, opportunities, reports, source, ancestry = _b1_synthetic_reports()
    return (
        _b1_runtime_and_manifest_binding_case(runtime, reports),
        _b1_synthetic_activation_and_denominator_case(
            runtime,
            opportunities,
            reports,
            ancestry,
        ),
        _b1_historical_capability_and_label_case(
            runtime,
            opportunities,
        ),
        _b1_canonical_order_and_exclusion_case(
            runtime,
            opportunities,
            source,
            ancestry,
        ),
        _b1_hostile_runtime_refusal_case(
            runtime,
            opportunities,
            source,
            ancestry,
        ),
    )


def audit_wo33b2_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Exercise B2 execution-mechanics rules and timing-safe projections."""

    runtime, opportunities, reports, ancestry = _b2_synthetic_reports()
    return (
        _b2_runtime_and_manifest_binding_case(runtime, reports),
        _b2_synthetic_activation_and_branch_case(
            runtime,
            opportunities,
            reports,
            ancestry,
        ),
        _b2_capability_refusal_case(runtime, opportunities),
        _b2_canonical_ancestry_label_and_timing_case(
            runtime,
            opportunities,
            ancestry,
        ),
        _b2_hostile_contract_refusal_case(runtime, opportunities, ancestry),
    )


def audit_wo33c_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Exercise transparent ranking, semantic collapse, and fixed review selection."""

    return (
        _c_difficulty_and_frequency_case(),
        _c_deterministic_ranking_case(),
        _c_semantic_deduplication_case(),
        _c_diversity_selection_case(),
        _c_shortfall_and_hostile_quota_case(),
    )


def audit_wo33d_drill_mining() -> tuple[DrillMiningAuditCase, ...]:
    """Exercise exact extraction, blind playback, reveal, and source overlays."""

    candidate, recorded, extracted, lesson = _d_playable_lesson_fixture()
    return (
        _d_source_lineage_and_prefix_parity_case(candidate, recorded, extracted),
        _d_warmup_and_information_fairness_case(candidate, recorded, lesson),
        _d_blind_boundary_and_reveal_authorization_case(candidate, lesson),
        _d_deterministic_build_and_replay_case(candidate, recorded, lesson),
        _d_parent_linked_source_authoritative_overlay_case(lesson),
    )


def _candidate_identity_and_ancestry_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    candidate = _sample_candidate()
    repeated = _sample_candidate()
    expected_identity_keys = {
        "bounds",
        "candidate_key",
        "capability_record_sha256",
        "checkpoint",
        "detector",
        "difficulty_projection",
        "evidence_class",
        "ground_truth_summary_sha256",
        "known_ambiguity",
        "lesson_type",
        "objective_projection",
        "observable_feature_summary_sha256",
        "primary_skill_id",
        "proposal_state",
        "rarity_projection",
        "reveal_material_sha256",
        "schema_version",
        "source_ancestry_sha256",
        "source_identity",
        "source_window_outcome",
        "supporting_skill_ids",
    }
    projection = candidate.identity_projection()
    if set(projection) != expected_identity_keys:
        failures.append("candidate identity projection does not have the exact key set")
    if len(projection) != WO33A_IDENTITY_KEY_COUNT:
        failures.append("candidate identity key count changed")
    ancestry_payload = {
        "checkpoint_id": "checkpoint-0001",
        "checkpoint_sha256": _digest("checkpoint"),
        "event_prefix_sha256": _digest("event-prefix"),
        "parent_source_ancestry_sha256": None,
        "source_id": "qualification-run-0001",
        "source_kind": "RUN",
        "source_sha256": _digest("source-run"),
    }
    expected_ancestry = hashlib.sha256(
        json.dumps(
            ancestry_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    if candidate.source_ancestry.as_dict() != ancestry_payload:
        failures.append("source ancestry projection is not exact")
    if candidate.source_ancestry.sha256 != expected_ancestry:
        failures.append("source_ancestry_sha256 did not reproduce independently")
    if projection["source_ancestry_sha256"] != expected_ancestry:
        failures.append("candidate identity did not bind source ancestry")
    if (
        candidate.identity_bytes() != repeated.identity_bytes()
        or candidate.candidate_id != repeated.candidate_id
    ):
        failures.append("identical candidate construction was not byte-stable")
    if candidate.candidate_id != "lesson-candidate-" + candidate.candidate_digest:
        failures.append("candidate ID did not retain the complete candidate digest")
    detached = candidate.identity_projection()
    detached["source_window_outcome"] = "FORGED"
    if candidate.identity_bytes() != repeated.identity_bytes():
        failures.append("detached identity mutation changed the candidate")
    if b"review_projection" in candidate.identity_bytes():
        failures.append("review projection entered candidate identity")
    return DrillMiningAuditCase(
        "lesson_candidate_exact_identity_and_ancestry",
        (
            f"identity_keys={len(projection)} ancestry_sha256=exact "
            "candidate_id=full_digest deterministic=true"
        ),
        tuple(failures),
    )


def _boundary_and_review_sidecar_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    candidate = _sample_candidate()
    base_bytes = candidate.canonical_bytes()
    base_digest = candidate.candidate_digest
    bounds = candidate.bounds
    variants = (
        replace(bounds, source_start_us=1),
        replace(bounds, source_end_us=bounds.source_end_us + 1),
        replace(bounds, warmup_start_us=bounds.warmup_start_us + 1),
        replace(bounds, active_start_us=bounds.active_start_us + 1),
        replace(bounds, active_end_us=bounds.active_end_us + 1),
        replace(bounds, post_end_us=bounds.post_end_us + 1),
    )
    changed = {
        _sample_candidate(bounds=item).candidate_digest for item in variants
    }
    if len(changed) != len(variants) or base_digest in changed:
        failures.append("an evidentiary boundary did not change candidate identity")
    decisions = tuple(HumanReviewDecisionV1)
    for ordinal, decision in enumerate(decisions, start=1):
        sidecar = HumanReviewSidecarV1(
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            decision=decision,
            reviewer_id="reviewer-001",
            review_ordinal=ordinal,
            rationale=f"Bounded {decision.value} review evidence.",
            superseded_by_candidate_id=(
                "lesson-candidate-" + _digest("replacement")
                if decision is HumanReviewDecisionV1.SUPERSEDED
                else None
            ),
        )
        candidate.assert_review_sidecar(sidecar)
        if candidate.canonical_bytes() != base_bytes:
            failures.append(f"{decision.value} sidecar changed candidate bytes")
    foreign_digest = _digest("foreign-candidate")
    foreign_sidecar = HumanReviewSidecarV1(
        candidate_id="lesson-candidate-" + foreign_digest,
        candidate_digest=foreign_digest,
        decision=HumanReviewDecisionV1.REJECTED,
        reviewer_id="reviewer-001",
        review_ordinal=5,
        rationale="Targets a different immutable candidate.",
    )
    if not _raises(lambda: candidate.assert_review_sidecar(foreign_sidecar)):
        failures.append("candidate accepted a review sidecar for another identity")
    pending = candidate.review_projection(CandidatePresentationModeV1.TECHNICAL_REVIEW)
    if (
        pending["proposal_state"] != "PROPOSED"
        or pending["human_review_status"] != "PENDING"
    ):
        failures.append("candidate proposal lifecycle did not map to pending review")
    if len(decisions) != WO33A_REVIEW_DECISION_COUNT:
        failures.append("human review sidecar decision inventory changed")
    mutation_refused = _raises(
        lambda: setattr(candidate, "proposal_state", "ACCEPTED")
    )
    if not mutation_refused:
        failures.append("frozen candidate accepted in-place review mutation")
    return DrillMiningAuditCase(
        "candidate_boundaries_and_review_sidecars_are_separate",
        (
            f"boundary_changes={len(changed)}/6 sidecar_decisions={len(decisions)} "
            "proposal=PROPOSED review=PENDING candidate_bytes=unchanged"
        ),
        tuple(failures),
    )


def _content_addressing_and_truth_access_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    synthetic = _sample_candidate()
    historical = _sample_candidate(
        evidence_class=EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
        source_kind=SourceKindV1.DATASET,
    )
    projection = synthetic.identity_projection()
    exact_record_keys = (
        (
            "observable summary",
            synthetic.observable_feature_summary.as_dict(),
            {
                "schema_version",
                "feature_tokens",
                "regime_signature",
                "event_five_grams",
                "contributing_source_event_ids",
            },
        ),
        (
            "ground-truth summary",
            synthetic.ground_truth_summary.as_dict(),
            {
                "schema_version",
                "evidence_class",
                "expected_classification",
                "authoritative_activation",
                "supporting_source_event_ids",
            },
        ),
        (
            "reveal material",
            synthetic.reveal_material.as_dict(),
            {
                "schema_version",
                "policy_id",
                "detector_id",
                "detector_version",
                "direction",
                "outcome_mapping_id",
                "observable_feature_summary_sha256",
                "ground_truth_summary_sha256",
                "supporting_source_event_ids",
            },
        ),
        (
            "capability record",
            synthetic.capability_record.as_dict(),
            {"schema_version", "source_identity", "detector", "records"},
        ),
        (
            "rarity projection",
            synthetic.rarity_projection.as_dict(),
            {
                "policy_id",
                "qualification_source_row",
                "qualifying_units",
                "eligible_units",
                "sample_frequency_ppm",
                "rarity_ppm",
            },
        ),
        (
            "objective projection",
            synthetic.objective_projection.as_dict(),
            {
                "kind",
                "detector_id",
                "direction",
                "response_start_us",
                "response_end_us",
                "outcome_mapping_id",
            },
        ),
    )
    for label, payload, expected_keys in exact_record_keys:
        if set(payload) != expected_keys:
            failures.append(f"{label} did not retain its exact V1 fields")
    difficulty_keys = {
        "policy_id",
        "signal_legibility_ppm",
        "duration_legibility_ppm",
        "signal_duration_legibility_ppm",
        "inverse_signal_duration_ppm",
        "conflict_ppm",
        "reaction_us",
        "reaction_hardness_ppm",
        "spread_ticks",
        "spread_hardness_ppm",
        "latency_us",
        "latency_hardness_ppm",
        "three_level_depth",
        "inverse_liquidity_ppm",
        "venue_count",
        "venue_hardness_ppm",
        "hidden_uncertainty_ppm",
        "objective_shares",
        "executable_depth",
        "objective_depth_hardness_ppm",
        "feature_count",
        "feature_hardness_ppm",
        "evidence_quality_ppm",
        "inverse_quality_ppm",
        "applicable_weight_sum",
        "difficulty_ppm",
    }
    if set(synthetic.difficulty_projection.as_dict()) != difficulty_keys:
        failures.append("difficulty projection did not retain its exact V1 fields")
    if tuple(round_div_even(numerator, 2) for numerator in (5, 7, -5, -7)) != (
        2,
        4,
        -2,
        -4,
    ):
        failures.append("difficulty arithmetic did not use ties-to-even rounding")
    if not _raises(
        lambda: DifficultyProjectionV1._WEIGHTS.__setitem__(
            "inverse_signal_duration_ppm",
            1,
        )
    ):
        failures.append("difficulty policy weights accepted mutation")
    if (
        projection["observable_feature_summary_sha256"]
        != synthetic.observable_feature_summary.sha256
        or projection["ground_truth_summary_sha256"]
        != synthetic.ground_truth_summary.sha256
        or projection["reveal_material_sha256"] != synthetic.reveal_material.sha256
        or projection["capability_record_sha256"]
        != synthetic.capability_record.sha256
    ):
        failures.append("candidate content-addressed record digests did not reproduce")
    if b"authoritative_activation" in synthetic.canonical_bytes():
        failures.append("public candidate bytes embedded protected ground truth")
    if "GroundTruthSummaryV1" in repr(synthetic):
        failures.append("candidate representation exposed protected ground truth")
    if not _raises(lambda: synthetic.protected_ground_truth("REVEAL_AUTHORIZED")):
        failures.append("ground truth accepted an untyped access token")
    if not _raises(
        lambda: synthetic.issue_ground_truth_access(
            CandidatePresentationModeV1.ASSESSMENT
        )
    ):
        failures.append("assessment mode issued a ground-truth access grant")
    protected = synthetic.protected_ground_truth(
        synthetic.issue_ground_truth_access(CandidatePresentationModeV1.REVEALED)
    )
    if protected.get("authoritative_activation") is not True:
        failures.append("authorized ground-truth read lost its authority label")
    if (
        historical.ground_truth_summary is not None
        or historical.identity_projection()["ground_truth_summary_sha256"] is not None
        or historical.reveal_material.ground_truth_summary_sha256 is not None
    ):
        failures.append("historical evidence synthesized a ground-truth record")
    forged_reveal = replace(
        synthetic.reveal_material,
        observable_feature_summary_sha256="0" * 64,
    )
    if not _raises(lambda: replace(synthetic, reveal_material=forged_reveal)):
        failures.append("candidate accepted a mismatched reveal-material digest")
    return DrillMiningAuditCase(
        "candidate_evidence_records_are_content_addressed_and_access_controlled",
        (
            "observable=bound capability=bound reveal=bound "
            "synthetic_truth=separate historical_truth=null"
        ),
        tuple(failures),
    )


def _versioned_skill_and_objective_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    candidate = _sample_candidate()
    expected_ids = {
        "BOOK_READING",
        "TAPE_READING",
        "QUEUE_POSITION",
        "PASSIVE_ENTRY",
        "AGGRESSIVE_ENTRY",
        "CANCEL_TIMING",
        "REPLACE_TIMING",
        "PARTIAL_FILL_MANAGEMENT",
        "ADVERSE_SELECTION",
        "SPREAD_DECISION",
        "VOLUME_CONTEXT",
        "REGIME_RECOGNITION",
        "ABSORPTION_RECOGNITION",
        "LIQUIDITY_WITHDRAWAL",
        "HIDDEN_LIQUIDITY",
        "LATENCY_AWARENESS",
        "MULTI_VENUE_ROUTING",
        "AUCTION_EXECUTION",
        "HALT_REOPENING",
        "SCRIPT_DISCIPLINE",
        "HOTKEY_ACCURACY",
        "POSITION_MANAGEMENT",
        "EXIT_EXECUTION",
    }
    if set(STABLE_SKILL_IDS_V1) != expected_ids or len(expected_ids) != WO33A_SKILL_COUNT:
        failures.append("stable V1 skill catalog differs from the WO34 inventory")
    if any(item.version != 1 for item in SKILL_REGISTRY_V1.definitions):
        failures.append("skill references are not explicitly versioned")
    if SKILL_REGISTRY_V1.canonical_bytes() != canonical_json_bytes(
        SKILL_REGISTRY_V1.as_dict()
    ):
        failures.append("skill registry bytes are not canonical")
    if not _raises(lambda: SKILL_REGISTRY_V1.require("UNKNOWN_SKILL")):
        failures.append("unknown skill registry reference was accepted")
    if not _raises(lambda: setattr(SKILL_REGISTRY_V1, "_definitions", ())):
        failures.append("versioned skill registry accepted mutation")
    if not _raises(lambda: replace(candidate, primary_skill_id="UNKNOWN_SKILL")):
        failures.append("candidate accepted an unknown primary skill")
    if not _raises(
        lambda: replace(
            candidate,
            supporting_skill_ids=("QUEUE_POSITION", "UNKNOWN_SKILL"),
        )
    ):
        failures.append("candidate accepted an unknown supporting skill")
    if not isinstance(candidate.objective_projection, ObserveClassifyObjectiveV1):
        failures.append("candidate objective is not a typed objective contract")
    if not _raises(lambda: replace(candidate, objective_projection="classify it")):
        failures.append("candidate accepted a free-text-only objective")
    if (
        candidate.primary_skill_id != "BOOK_READING"
        or candidate.supporting_skill_ids != ("QUEUE_POSITION",)
    ):
        failures.append("candidate did not retain exactly one detector-owned primary skill")
    return DrillMiningAuditCase(
        "stable_versioned_skills_and_typed_objectives",
        (
            f"skills={len(STABLE_SKILL_IDS_V1)} version=1 primary=exactly_one "
            "unknown=refused objective=OBSERVE_CLASSIFY_V1"
        ),
        tuple(failures),
    )


def _detector_capability_admission_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    threshold = _digest("thresholds")
    strong = DETECTOR_REGISTRY_V1.require("STRONG_QUEUE_IMBALANCE", 1)
    inventory = _inventory_for(strong.detector_id, EvidenceClassV1.SYNTHETIC_GROUND_TRUTH)
    eligible = DETECTOR_REGISTRY_V1.assess(
        strong.detector_id,
        1,
        threshold,
        inventory,
    )
    missing_inventory = replace(
        inventory,
        available_records=tuple(
            item
            for item in inventory.available_records
            if item.capability != "QUOTES"
        ),
    )
    missing = DETECTOR_REGISTRY_V1.assess(
        strong.detector_id,
        1,
        threshold,
        missing_inventory,
    )
    reconstructed = _inventory_for(
        strong.detector_id,
        EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
    )
    unsupported = DETECTOR_REGISTRY_V1.assess(
        strong.detector_id,
        1,
        threshold,
        reconstructed,
    )
    failed_breakout = DETECTOR_REGISTRY_V1.require("FAILED_BREAKOUT", 1)
    reconstruction_eligible = DETECTOR_REGISTRY_V1.assess(
        failed_breakout.detector_id,
        1,
        threshold,
        _inventory_for(
            failed_breakout.detector_id,
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
        ),
    )
    hidden = DETECTOR_REGISTRY_V1.require("HIDDEN_RESERVE_REFRESH", 1)
    hidden_inventory = _inventory_for(
        hidden.detector_id,
        EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
    )
    no_reserve_label = replace(
        hidden_inventory,
        available_records=tuple(
            item
            for item in hidden_inventory.available_records
            if item.capability != "AUTHORITATIVE_RESERVE_REFRESH_LABELS"
        ),
    )
    hidden_missing = DETECTOR_REGISTRY_V1.assess(
        hidden.detector_id,
        1,
        threshold,
        no_reserve_label,
    )
    hidden_candidate = _sample_candidate(detector_id=hidden.detector_id)
    historical_without_mbo = _raises(
        lambda: SourceCapabilityInventoryV1(
            SourceIdentityV1(
                SourceKindV1.DATASET,
                "historical-without-mbo",
                _digest("historical-without-mbo"),
            ),
            EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
            inventory.available_records,
        )
    )
    reconstruction_wrong_kind = _raises(
        lambda: SourceCapabilityInventoryV1(
            inventory.source_identity,
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
            inventory.available_records,
        )
    )
    if (
        eligible.status is not DetectorSupportStatusV1.ELIGIBLE
        or eligible.capability_record is None
    ):
        failures.append("supported detector/source capability bundle was not eligible")
    if (
        missing.status is not DetectorSupportStatusV1.NOT_EXERCISED
        or missing.reason_code != "INSUFFICIENT_SOURCE_CAPABILITY"
        or missing.missing_capabilities != ("QUOTES",)
        or missing.capability_record is not None
    ):
        failures.append("missing source capability was not truthfully NOT_EXERCISED")
    if (
        unsupported.status is not DetectorSupportStatusV1.NOT_EXERCISED
        or unsupported.reason_code != "UNSUPPORTED_EVIDENCE_CLASS"
        or unsupported.capability_record is not None
    ):
        failures.append("unsupported evidence class was treated as detector false/pass")
    if reconstruction_eligible.status is not DetectorSupportStatusV1.ELIGIBLE:
        failures.append("declared reconstruction support was not exercised")
    if (
        hidden_missing.status is not DetectorSupportStatusV1.NOT_EXERCISED
        or hidden_missing.missing_capabilities
        != ("AUTHORITATIVE_RESERVE_REFRESH_LABELS",)
    ):
        failures.append("hidden-truth capability absence was not explicit")
    if hidden_candidate.difficulty_projection.hidden_uncertainty_ppm != 0:
        failures.append("synthetic hidden-liquidity uncertainty was not exact")
    if not _raises(
        lambda: replace(
            hidden_candidate,
            difficulty_projection=replace(
                hidden_candidate.difficulty_projection,
                hidden_uncertainty_ppm=1,
            ),
        )
    ):
        failures.append("candidate accepted inconsistent hidden uncertainty")
    if not historical_without_mbo:
        failures.append("historical evidence class accepted no MBO foundation")
    if not reconstruction_wrong_kind:
        failures.append("reconstruction evidence accepted non-reconstruction identity")
    if not _raises(lambda: DETECTOR_REGISTRY_V1.require("UNKNOWN_DETECTOR", 1)):
        failures.append("unknown detector reference was accepted")
    if not _raises(lambda: setattr(DETECTOR_REGISTRY_V1, "_declarations", ())):
        failures.append("versioned detector registry accepted mutation")
    for declaration in DETECTOR_REGISTRY_V1.declarations:
        payload = declaration.as_dict()
        if not {
            "supports_synthetic_ground_truth",
            "supports_historical",
            "supports_reconstruction",
        }.issubset(payload):
            failures.append(
                f"{declaration.detector_id} omitted an explicit support declaration"
            )
    if len(DETECTOR_IDS_V1) != WO33A_DETECTOR_COUNT:
        failures.append("detector registry count changed")
    return DrillMiningAuditCase(
        "detector_capabilities_fail_closed_as_not_exercised",
        (
            f"detectors={len(DETECTOR_IDS_V1)} support_flags=3 "
            "eligible=true missing=NOT_EXERCISED unsupported=NOT_EXERCISED"
        ),
        tuple(failures),
    )


def _assessment_reveal_and_historical_regression_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    candidate = _sample_candidate()
    declaration = DETECTOR_REGISTRY_V1.require(
        candidate.detector.detector_id,
        candidate.detector.version,
    )
    assessment = candidate.review_projection(CandidatePresentationModeV1.ASSESSMENT)
    assessment_bytes = canonical_json_bytes(assessment)
    protected = (
        candidate.detector.detector_id,
        declaration.display_name,
        declaration.revealed_title,
        candidate.source_window_outcome.value,
    )
    if any(value.encode("utf-8") in assessment_bytes for value in protected):
        failures.append("assessment projection leaked detector or outcome identity")
    if assessment.get("title") != "MARKET STRUCTURE CLASSIFICATION":
        failures.append("assessment title was outcome-loaded")
    technical = candidate.review_projection(
        CandidatePresentationModeV1.TECHNICAL_REVIEW
    )
    if technical.get("source_window_outcome") != "WITHHELD_DURING_TECHNICAL_REVIEW":
        failures.append("technical review exposed future source-window outcome")
    revealed = candidate.review_projection(CandidatePresentationModeV1.REVEALED)
    if (
        revealed.get("detector_name") != declaration.display_name
        or revealed.get("source_window_outcome")
        != candidate.source_window_outcome.value
    ):
        failures.append("explicit reveal did not restore detector/outcome labels")
    historical_cases = audit_historical_lessons()
    historical_failures = tuple(
        f"{case.name}: {failure}"
        for case in historical_cases
        for failure in case.failures
    )
    failures.extend(historical_failures)
    return DrillMiningAuditCase(
        "assessment_is_blind_and_historical_lessons_regress",
        (
            f"assessment_detector=withheld outcome=withheld reveal=explicit "
            f"historical_cases={len(historical_cases)} passing="
            f"{sum(not case.failures for case in historical_cases)}"
        ),
        tuple(failures),
    )


def _a1_detector_threshold_manifest_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    threshold_manifest = bundle.thresholds
    payload = threshold_manifest.as_dict()
    if (
        threshold_manifest.manifest_sha256 != WO33A1_THRESHOLD_MANIFEST_SHA256
        or bundle.plan.manifest_sha256 != WO33A1_MINING_PLAN_MANIFEST_SHA256
        or bundle.sources.manifest_sha256 != WO33A1_SOURCE_MANIFEST_SHA256
        or bundle.bundle_sha256 != WO33A1_POLICY_BUNDLE_SHA256
    ):
        failures.append("WO33-A1 policy bundle differs from its preregistered digest")
    if threshold_manifest.detector_ids != DETECTOR_IDS_V1:
        failures.append("threshold manifest detector inventory/order differs from registry")
    bundles = payload["capability_bundles"]
    detectors = payload["detectors"]
    if not isinstance(bundles, dict) or not isinstance(detectors, dict):
        failures.append("threshold capability/detector tables are malformed")
    else:
        for detector_id in DETECTOR_IDS_V1:
            declaration = DETECTOR_REGISTRY_V1.require(detector_id, 1)
            row = detectors[detector_id]
            if not isinstance(row, dict):
                failures.append(f"{detector_id} threshold row is malformed")
                continue
            bundle_id = row["capability_bundle"]
            if bundles.get(bundle_id) != list(declaration.required_capabilities):
                failures.append(f"{detector_id} capability bundle differs from registry")
            if row["evidence_classes"] != [
                item.value for item in declaration.supported_evidence_classes
            ]:
                failures.append(f"{detector_id} evidence scope differs from registry")
            if not row["rule_expression"] or not row["thresholds"]:
                failures.append(f"{detector_id} lacks an operational activation rule")
            if len(threshold_manifest.detector_threshold_sha256(detector_id)) != 64:
                failures.append(f"{detector_id} threshold row is not content addressed")
    distinctive_thresholds = {
        "AGGRESSIVE_FLOW_BURST": ("absolute_aggressive_flow_imbalance_ppm", 700_000),
        "APPARENT_LIQUIDITY_MIRAGE": ("cohort_cancelled_share_ppm", 800_000),
        "ASK_ABSORPTION": ("aggressive_buy_quantity", 1_000),
        "AUCTION_IMBALANCE_CHANGE": ("relative_change_ppm", 250_000),
        "BID_ABSORPTION": ("aggressive_sell_quantity", 1_000),
        "CANCELLATION_BURST": ("cancel_to_add_ratio_ppm", 3_000_000),
        "CANCEL_FILL_RACE": ("latency_intervention_delta", 1_000),
        "DISTRESSED_LIQUIDATION": ("distressed_sell_quantity", 5_000),
        "FAILED_BREAKOUT": ("return_deadline", 3_000_000),
        "HALT_REOPENING": ("spread_ratio_ppm", 2_000_000),
        "HIDDEN_RESERVE_REFRESH": ("reserve_refresh_count", 3),
        "LATENCY_SENSITIVE_OPPORTUNITY": ("slow_latency", 2_500),
        "LIQUIDITY_VACUUM": ("depletion_share_ppm", 750_000),
        "MEAN_REVERSION_TRANSITION": ("trailing_p50_window", 30_000_000),
        "MOMENTUM_EXHAUSTION": ("initial_mid_x2_movement", 10),
        "MULTI_VENUE_FRAGMENTATION": ("best_price_difference", 2),
        "QUEUE_DEPLETION": ("depletion_share_ppm", 700_000),
        "QUEUE_REPLENISHMENT": ("cycle_count", 3),
        "ROUTING_DILEMMA": ("absolute_quantity_difference", 250),
        "SPREAD_EXPANSION": ("expanded_spread", 4),
        "SPREAD_RECOVERY": ("recovery_deadline", 5_000_000),
        "STRONG_QUEUE_IMBALANCE": ("absolute_queue_imbalance_ppm", 600_000),
    }
    for detector_id, (name, expected) in distinctive_thresholds.items():
        row = threshold_manifest.detector(detector_id)
        observed = {
            item["name"]: item["value"] for item in row["thresholds"]
        }.get(name)
        if observed != expected:
            failures.append(f"{detector_id} distinctive threshold differs")
    auction = threshold_manifest.detector("AUCTION_IMBALANCE_CHANGE")
    halt = threshold_manifest.detector("HALT_REOPENING")
    if (
        "AUCTION" in auction["exclusion_rules"]
        or "SESSION_BOUNDARY" in auction["exclusion_rules"]
        or "HALT" in halt["exclusion_rules"]
        or payload["binning"]["observable_bin_us"] != 100_000
    ):
        failures.append("ordinary exclusions or the two explicit exceptions differ")
    return DrillMiningAuditCase(
        "mining_detector_thresholds_are_complete_operational_and_digest_bound",
        (
            f"detectors={len(threshold_manifest.detector_ids)}/22 "
            f"threshold_manifest_sha256={threshold_manifest.manifest_sha256} "
            "bin_us=100000 evidence=S,H,R exceptions=auction,halt"
        ),
        tuple(failures),
    )


def _a1_difficulty_sampling_and_shortfall_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    plan = bundle.plan.as_dict()
    difficulty = plan["difficulty"]
    weights = difficulty["weights_ppm"]
    expected_weights = {
        "conflict_ppm": 100_000,
        "feature_hardness_ppm": 80_000,
        "hidden_uncertainty_ppm": 60_000,
        "inverse_liquidity_ppm": 100_000,
        "inverse_quality_ppm": 60_000,
        "inverse_signal_duration_ppm": 160_000,
        "latency_hardness_ppm": 70_000,
        "objective_depth_hardness_ppm": 80_000,
        "reaction_hardness_ppm": 110_000,
        "spread_hardness_ppm": 100_000,
        "venue_hardness_ppm": 80_000,
    }
    if weights != expected_weights or sum(weights.values()) != 1_000_000:
        failures.append("the eleven nominal difficulty weights differ")
    if difficulty["evidence_quality_ppm"] != {
        "H": 850_000,
        "R": 500_000,
        "S": 1_000_000,
    } or difficulty["hidden_uncertainty_ppm"] != {
        "H": 250_000,
        "R": 750_000,
        "S": 0,
    }:
        failures.append("evidence quality or hidden uncertainty differs")
    formulas = difficulty["formulas"]
    input_rules = difficulty["input_rules"]
    if (
        formulas["difficulty_ppm"]
        != "round_div_even(sum(applicable_weight*component),sum(applicable_weight))"
        or formulas["reaction_hardness_ppm"]
        != "clamp(round_div_even((2000000-reaction_us)*S,2000000),0,S)"
        or input_rules["objective_size_depth"]
        != "NOT_APPLICABLE_FOR_OBSERVE_CLASSIFY_V1"
        or input_rules["and_legibility"] != "MIN_CLAUSE_LEGIBILITY"
        or input_rules["boolean_legibility"] != "REQUIRED_TRUE_EQUALS_S"
        or input_rules["or_legibility"]
        != "MAX_LEGIBILITY_OF_FULLY_SATISFIED_BRANCH"
        or input_rules["signal_duration_legibility"]
        != "ROUND_DIV_EVEN_MEAN_OF_APPLICABLE_SIGNAL_AND_DURATION"
        or input_rules["signed_clause_orientation"]
        != (
            "X_LE_NEGATIVE_L_TO_NEGATIVE_X_GE_L;ABS_X_GE_L_TO_ABS_X;"
            "DIRECTIONAL_X_TO_BUY_X_OR_SELL_NEGATIVE_X"
        )
    ):
        failures.append("difficulty formula, clause orientation, or input rule differs")
    sampling = plan["sampling"]
    if (
        sampling["eligible_default_unit"]
        != "ONE_DETECTOR_SOURCE_OBSERVABLE_BIN_100000_US"
        or sampling["denominator_zero"] != "NOT_EXERCISED"
        or sampling["multiple_qualifying_keys_per_unit"] != "COUNT_ONCE"
        or len(sampling["alternate_units"]) != 6
    ):
        failures.append("sample-frequency denominator or alternate units differ")
    shortfall = plan["candidate_shortfall"]
    if (
        shortfall["quota_may_weaken_thresholds"] is not False
        or shortfall["duplicates_may_fill_quota"] is not False
        or shortfall["event_five_gate"]
        != "PASS_ONLY_IF_STEP_1_OBTAINS_FIVE"
    ):
        failures.append("candidate shortfall can conceal or weaken a fixed gate")
    return DrillMiningAuditCase(
        "mining_difficulty_sampling_and_shortfall_are_preregistered",
        (
            f"difficulty_components={len(weights)} weight_sum={sum(weights.values())} "
            "default_unit=detector/source/100000us alternate_units=6 "
            "zero_eligible=NOT_EXERCISED thresholds_never_weaken=true"
        ),
        tuple(failures),
    )


def _a1_dedup_diversity_and_review_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    plan = bundle.plan.as_dict()
    dedup = plan["deduplication"]
    if {
        "time_iou_min_ppm": dedup["time_iou_min_ppm"],
        "feature_jaccard_min_ppm": dedup["feature_jaccard_min_ppm"],
        "event_five_gram_jaccard_min_ppm": dedup[
            "event_five_gram_jaccard_min_ppm"
        ],
        "objective_jaccard_min_ppm": dedup["objective_jaccard_min_ppm"],
    } != {
        "time_iou_min_ppm": 800_000,
        "feature_jaccard_min_ppm": 900_000,
        "event_five_gram_jaccard_min_ppm": 850_000,
        "objective_jaccard_min_ppm": 500_000,
    }:
        failures.append("deduplication similarity thresholds differ")
    if (
        dedup["source_ancestry"] != "EXACT_COMPLETE_DIGEST_MATCH"
        or dedup["regime_signature"] != "EXACT_MATCH"
        or dedup["collapse"] != "ONE_ORDERED_GREEDY_PASS_NOT_CONNECTED_COMPONENTS"
        or dedup["candidate_order"]
        != ["difficulty_ppm", "active_start_us", "candidate_id_nfc_utf8"]
    ):
        failures.append("deduplication identity or greedy ordering differs")
    dedup_inputs = plan["deduplication_inputs"]
    if (
        dedup_inputs["time_iou"]
        != (
            "unsigned_share_ppm(max(0,min(a_end,b_end)-max(a_start,b_start)),"
            "(a_end-a_start)+(b_end-b_start)-intersection)"
        )
        or dedup_inputs["event_five_gram_order"]
        != "UNIQUE_TUPLES_SORTED_BY_COMPACT_CANONICAL_JSON_BYTES"
        or dedup_inputs["source_ancestry_encoding"]
        != "SHA256_COMPACT_SORTED_KEY_CANONICAL_JSON_EXPLICIT_NULLS"
    ):
        failures.append("deduplication canonical encodings or IoU formula differ")
    diversity = plan["diversity"]
    if diversity["weights_ppm"] != {
        "detector_family": 200_000,
        "difficulty_band": 150_000,
        "phase": 100_000,
        "primary_skill": 250_000,
        "source": 200_000,
        "source_window_outcome": 100_000,
    } or sum(diversity["weights_ppm"].values()) != 1_000_000:
        failures.append("diversity dimensions or weights differ")
    if diversity["difficulty_bands_ppm"] != [
        {"lower_ppm": 0, "upper_inclusive": False, "upper_ppm": 250_000},
        {
            "lower_ppm": 250_000,
            "upper_inclusive": False,
            "upper_ppm": 500_000,
        },
        {
            "lower_ppm": 500_000,
            "upper_inclusive": False,
            "upper_ppm": 750_000,
        },
        {
            "lower_ppm": 750_000,
            "upper_inclusive": True,
            "upper_ppm": 1_000_000,
        },
    ] or diversity["dimension_values"]["source_window_outcome_rule"] != (
        "ORIENTED_MID_X2_GE_2_CONTINUATION;LE_NEGATIVE_2_REVERSAL;"
        "ELSE_STASIS;NONDIRECTIONAL_NOT_APPLICABLE;"
        "MISSING_QUOTES_NOT_OBSERVABLE"
    ):
        failures.append("difficulty bands or source-window outcome rule differ")
    review = plan["review_sampling"]
    if (
        review["target_count"] != WO33A1_REVIEW_TARGET_COUNT
        or review["selection_root"] != 3_399_001
        or review["tie_context"] != "WO33_REVIEW_V1"
        or review["source_order"]
        != ["event", "quiet", "hidden", "fragmented", "historical"]
        or review["reserved_counts"]
        != {"event": 5, "fragmented": 3, "hidden": 3, "historical": 3, "quiet": 3}
    ):
        failures.append("stratified twenty-candidate review sampling differs")
    if len(plan["detector_families"]) != 7 or sum(
        len(values) for values in plan["detector_families"].values()
    ) != 22:
        failures.append("detector family partition does not cover twenty-two detectors")
    return DrillMiningAuditCase(
        "mining_dedup_diversity_and_review_sampling_are_preregistered",
        (
            "dedup=ancestry+iou800000+features900000+regime+fivegram850000+"
            "objective500000 diversity_dimensions=6 review_target=20 "
            "reserved=event5+other3x4 global_fill=true"
        ),
        tuple(failures),
    )


def _a1_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _a1_qualification_source_matrix_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    from kirby2.scenario_lang.commands import inspect_scenario_source

    failures: list[str] = []
    repository = _a1_repository_root()
    expected_profiles = {
        "event": ("EVENT_SHOCK_PRESSURE", 3_102_000, "S", 0, 6_000_000_000),
        "fragmented": ("MULTIVENUE_RECORDING_V1", 3_302_002, "S", 0, 1_300),
        "hidden": ("HIDDEN_LIQUIDITY_RECORDING_V1", 3_302_001, "S", 0, 300),
        "historical": (
            "HISTORICAL_LESSON_V1",
            "NOT_APPLICABLE",
            "R",
            0,
            30_000_000,
        ),
        "quiet": ("QUIET_RANGE_PRESSURE", 3_102_000, "S", 0, 6_000_000_000),
    }
    for row in bundle.sources.rows:
        path = repository / str(row.source["example_path"])
        if not path.is_file():
            failures.append(f"{row.row_id} example source is absent")
            continue
        raw = path.read_bytes()
        if (
            len(raw) != row.source["raw_bytes_length"]
            or hashlib.sha256(raw).hexdigest() != row.source["raw_sha256"]
        ):
            failures.append(f"{row.row_id} example source bytes differ")
            continue
        report = inspect_scenario_source(path)
        artifact = report.artifact
        if not report.passed or artifact is None:
            failures.append(f"{row.row_id} exact WO32 example no longer compiles")
            continue
        if (
            artifact.compiled_artifact_digest
            != row.source["compiled_artifact_sha256"]
            or artifact.semantic_plan_digest != row.source["semantic_plan_sha256"]
            or artifact.source_bundle_digest != row.source["source_bundle_sha256"]
            or artifact.adapter_id != row.source["example_adapter_id"]
            or artifact.adapter_version != row.source["example_adapter_version"]
            or artifact.target_kind.value != row.source["example_target_kind"]
            or artifact.seed_policy.selected_root_seed
            != row.source["example_selected_root_seed"]
        ):
            failures.append(f"{row.row_id} compiled WO32 source identity differs")
        expected = expected_profiles[row.row_id]
        observed = (
            row.identity["qualification_profile_id"],
            row.identity["qualification_root_seed"],
            row.identity["evidence_class"],
            row.bounds["source_start_us"],
            row.bounds["source_end_us"],
        )
        if observed != expected:
            failures.append(f"{row.row_id} qualification identity/bounds differ")
        native_path = row.configuration["native_payload_path"]
        if native_path not in {
            "WO31_I1_IMMUTABLE_PLAN_ARTIFACT",
            "NOT_APPLICABLE",
        }:
            native = repository / str(native_path)
            if (
                not native.is_file()
                or native.stat().st_size
                != row.configuration["native_payload_raw_bytes_length"]
                or hashlib.sha256(native.read_bytes()).hexdigest()
                != row.configuration["native_payload_raw_sha256"]
            ):
                failures.append(f"{row.row_id} native payload bytes differ")
    if len(bundle.sources.rows) != WO33A1_SOURCE_COUNT:
        failures.append("qualification source matrix count differs")
    return DrillMiningAuditCase(
        "mining_five_source_matrix_resolves_exact_bytes_bounds_and_capabilities",
        (
            f"sources={len(bundle.sources.rows)}/5 roots=3102000,3102000,"
            "3302001,3302002,NOT_APPLICABLE evidence=S,S,S,S,R "
            "source_and_config_bytes=digest_bound"
        ),
        tuple(failures),
    )


def _a1_source_replay_identity_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    from kirby2.historical.lesson_runner import run_historical_lesson
    from kirby2.multivenue.replay import replay_multivenue_recording
    from kirby2.observability.replay import replay_observability_recording
    from kirby2.scenario_lang.commands import inspect_scenario_source

    failures: list[str] = []
    repository = _a1_repository_root()
    protected_rows = {"quiet", "event"}
    for row in bundle.sources.rows:
        if row.row_id in protected_rows:
            parent = repository / str(row.provenance["parent_artifact_path"])
            if not parent.is_file():
                failures.append(f"{row.row_id} immutable WO31-I1 proof is absent")
                continue
            raw = parent.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row.provenance["parent_artifact_sha256"]:
                failures.append(f"{row.row_id} WO31-I1 proof parent digest differs")
                continue
            try:
                proof_payload = json.loads(raw)
            except json.JSONDecodeError:
                failures.append(f"{row.row_id} WO31-I1 proof parent is invalid JSON")
                continue
            matches = [
                item
                for item in proof_payload.get("run_proofs", [])
                if item.get("candidate_id")
                == row.identity["qualification_profile_id"]
                and item.get("partition") == "QUALIFICATION"
                and item.get("root_seed") == 3_102_000
            ]
            embedded = json.loads(str(row.configuration["bytes_json"]))
            if len(matches) != 1 or matches[0] != embedded:
                failures.append(f"{row.row_id} did not resolve one exact WO31-I1 proof")
                continue
            proof = matches[0]
            if (
                proof["run_digest"] != row.identity["expected_native_run_digest"]
                or proof["run_digest"] != row.identity["expected_replay_digest"]
                or proof["full_day_run_id"] != row.provenance["full_day_run_id"]
                or proof["replay_verification_status"] != "PASS"
            ):
                failures.append(f"{row.row_id} WO31-I1 replay identity differs")
            review_path = parent.with_name("review-source.json")
            if not review_path.is_file():
                failures.append(f"{row.row_id} WO31-I1 review source is absent")
                continue
            review_raw = review_path.read_bytes()
            review_sha = hashlib.sha256(review_raw).hexdigest()
            try:
                review = json.loads(review_raw)
            except json.JSONDecodeError:
                failures.append(f"{row.row_id} WO31-I1 review source is invalid JSON")
                continue
            review_matches = [
                item
                for item in review.get("runs", [])
                if item.get("candidate_id")
                == row.identity["qualification_profile_id"]
                and item.get("root_seed") == 3_102_000
            ]
            if (
                len(review_matches) != 1
                or review_matches[0]["session_start_us"]
                != row.bounds["source_start_us"]
                or review_matches[0]["session_end_us"]
                != row.bounds["source_end_us"]
                or f"review_source_sha256={review_sha}"
                not in str(row.provenance["parent_selector"])
            ):
                failures.append(f"{row.row_id} WO31-I1 session bounds differ")
            continue

        source_path = repository / str(row.source["example_path"])
        report = inspect_scenario_source(source_path)
        if not report.passed or report.artifact is None:
            failures.append(f"{row.row_id} source cannot resolve for replay")
            continue
        artifact = report.artifact
        native = artifact.plan_envelope.payload
        if artifact.run_identity_digest != row.identity["expected_native_run_digest"]:
            failures.append(f"{row.row_id} native run identity differs")
        if row.row_id == "hidden":
            replay = replay_observability_recording(native)
            replay_digest = native.sha256()
            state_digest = replay.venue.state_sha256()
        elif row.row_id == "fragmented":
            replay = replay_multivenue_recording(native)
            replay_digest = native.sha256()
            state_digest = replay.coordinator.state_sha256()
        else:
            session = run_historical_lesson(native)
            replay = None
            replay_digest = session.run.replay_sha256()
            state_digest = "NOT_APPLICABLE"
        if replay is not None and not replay.passed:
            failures.append(f"{row.row_id} deterministic recording replay failed")
        if (
            replay_digest != row.identity["expected_replay_digest"]
            or state_digest != row.identity["expected_final_state_sha256"]
        ):
            failures.append(f"{row.row_id} replay or final-state digest differs")
        if row.execution["candidate_outcomes_inspected"] != "FORBIDDEN":
            failures.append(f"{row.row_id} permits candidate outcome inspection")
    return DrillMiningAuditCase(
        "mining_source_replay_identities_verify_without_protected_regeneration",
        (
            "wo31_i1_rows=2 read_only=true protected_seed_execution=absent "
            "fixed_recording_replays=2 historical_sources=1 replay_identity=exact"
        ),
        tuple(failures),
    )


def _a1_unexercised_and_hostile_refusal_case(
    bundle: MiningPolicyBundleV1,
) -> DrillMiningAuditCase:
    from kirby2.research.toml_codec import canonical_toml

    failures: list[str] = []
    scopes = (
        bundle.thresholds.as_dict()["execution_scope"],
        bundle.plan.as_dict()["execution_scope"],
        bundle.sources.as_dict()["execution_scope"],
    )
    if any(scope["candidate_mining"] != "NOT_EXERCISED" for scope in scopes):
        failures.append("a preregistration manifest overstates candidate mining")
    if (
        scopes[0]["detector_invocation"] != "NOT_EXERCISED"
        or scopes[1]["detector_invocation"] != "NOT_EXERCISED"
        or scopes[2]["detector_invocation"] != "NOT_EXERCISED"
        or scopes[1]["selection"] != "NOT_EXERCISED"
        or scopes[2]["selection"] != "NOT_EXERCISED"
    ):
        failures.append("detector or selection execution was claimed prematurely")

    refusals = 0
    probes: list[Callable[[], object]] = [
        lambda: DetectorThresholdsManifestV1.from_toml_bytes(
            b" " + bundle.thresholds.canonical_bytes()
        ),
        lambda: QualificationSourcesManifestV1.from_toml_bytes(
            bundle.sources.canonical_bytes().replace(
                b"3302001", b"3102000", 1
            )
        ),
    ]
    changed_thresholds = bundle.thresholds.as_dict()
    changed_thresholds["unexpected_policy"] = True
    _rehash_a1_manifest(changed_thresholds)
    probes.append(
        lambda: DetectorThresholdsManifestV1.from_toml_bytes(
            canonical_toml(changed_thresholds).encode("utf-8")
        )
    )
    changed_plan = bundle.plan.as_dict()
    changed_plan["threshold_manifest_sha256"] = "0" * 64
    _rehash_a1_manifest(changed_plan)

    def wrong_cross_binding() -> object:
        changed = MiningPlanManifestV1.from_toml_bytes(
            canonical_toml(changed_plan).encode("utf-8")
        )
        return MiningPolicyBundleV1(bundle.thresholds, changed, bundle.sources)

    probes.append(wrong_cross_binding)
    for operation in probes:
        if _raises(operation):
            refusals += 1
        else:
            failures.append("a hostile manifest mutation was accepted")
    return DrillMiningAuditCase(
        "mining_preregistration_is_unexercised_and_hostile_mutations_fail_closed",
        (
            f"refusals={refusals}/4 detector_invocation=NOT_EXERCISED "
            "candidate_mining=NOT_EXERCISED selection=NOT_EXERCISED "
            "human_review=PENDING"
        ),
        tuple(failures),
    )


def _rehash_a1_manifest(payload: dict[str, object]) -> None:
    payload.pop("manifest_sha256", None)
    payload.pop("semantic_sha256", None)
    semantic = {
        key: value for key, value in payload.items() if key != "manifest_version"
    }
    payload["semantic_sha256"] = sha256_json(semantic)
    payload["manifest_sha256"] = sha256_json(payload)


def _b1_runtime_and_manifest_binding_case(
    runtime,
    reports: dict[str, object],
) -> DrillMiningAuditCase:
    from kirby2.mining.flow_detectors import FLOW_DETECTOR_HANDLERS_V1
    from kirby2.mining.queue_detectors import QUEUE_DETECTOR_HANDLERS_V1
    from kirby2.mining.runtime import (
        B1_DETECTOR_IDS_V1,
        DETECTOR_RUNTIME_ID_V1,
        WO33A1_THRESHOLD_MANIFEST_SHA256_V1,
    )

    failures: list[str] = []
    handlers = {**QUEUE_DETECTOR_HANDLERS_V1, **FLOW_DETECTOR_HANDLERS_V1}
    operational_b1_ids = tuple(
        detector_id
        for detector_id in runtime.handler_ids
        if detector_id in set(B1_DETECTOR_IDS_V1)
    )
    if (
        operational_b1_ids != B1_DETECTOR_IDS_V1
        or len(operational_b1_ids) != WO33B1_DETECTOR_COUNT
        or len({id(handler) for handler in handlers.values()})
        != WO33B1_DETECTOR_COUNT
    ):
        failures.append("B1 does not expose fifteen distinct operational handlers")
    if (
        runtime.threshold_manifest.manifest_sha256
        != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
    ):
        failures.append("B1 runtime did not pin the committed A1 threshold manifest")
    for detector_id in B1_DETECTOR_IDS_V1:
        row = runtime.threshold_manifest.detector(detector_id)
        report = reports[detector_id]
        if (
            row["detector_id"] != detector_id
            or row["version"] != 1
            or not row["thresholds"]
            or report.detector.threshold_sha256
            != runtime.threshold_manifest.detector_threshold_sha256(detector_id)
            or report.threshold_manifest_sha256
            != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
            or report.as_dict()["runtime_id"] != DETECTOR_RUNTIME_ID_V1
            or report.as_dict()["schema_version"] != 1
        ):
            failures.append(f"{detector_id} did not consume its exact A1 rule row")
    aggregate_sha256 = sha256_json(
        [reports[detector_id].as_dict() for detector_id in B1_DETECTOR_IDS_V1]
    )
    if aggregate_sha256 != WO33B1_SYNTHETIC_REPORT_SHA256:
        failures.append("B1 synthetic runtime evidence digest changed")
    return DrillMiningAuditCase(
        "b1_fifteen_distinct_handlers_consume_the_committed_a1_manifest",
        (
            f"handlers={len(operational_b1_ids)}/15 versions=1 "
            f"threshold_manifest_sha256={runtime.threshold_manifest.manifest_sha256} "
            f"synthetic_report_sha256={aggregate_sha256}"
        ),
        tuple(failures),
    )


def _b1_synthetic_activation_and_denominator_case(
    runtime,
    opportunities: dict[str, object],
    reports: dict[str, object],
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        DetectorMeasurementV1,
        DetectorRunStatusV1,
        OpportunityDispositionV1,
    )

    failures: list[str] = []
    emitted_ids = {
        detector_id
        for detector_id, report in reports.items()
        if report.status is DetectorRunStatusV1.EXERCISED
        and report.qualifying_units == 1
        and len(report.findings) == 1
    }
    if len(emitted_ids) != WO33B1_DETECTOR_COUNT:
        failures.append("a B1 synthetic boundary fixture did not emit exactly once")
    considered = sum(len(report.considered) for report in reports.values())
    eligible = sum(report.eligible_units for report in reports.values())
    emitted = sum(report.qualifying_units for report in reports.values())
    below = sum(
        item.disposition is OpportunityDispositionV1.BELOW_THRESHOLD
        for report in reports.values()
        for item in report.considered
    )
    if (considered, eligible, emitted, below) != (16, 16, 15, 1):
        failures.append("B1 considered/eligible/emitted denominator ledger differs")
    strong = reports["STRONG_QUEUE_IMBALANCE"]
    if strong.sample_frequency_ppm != 500_000 or any(
        report.sample_frequency_ppm != 1_000_000
        for detector_id, report in reports.items()
        if detector_id != "STRONG_QUEUE_IMBALANCE"
    ):
        failures.append("B1 sample frequency is not derived from the explicit denominator")
    findings = [item for report in reports.values() for item in report.findings]
    if (
        len({item.finding_sha256 for item in findings}) != WO33B1_DETECTOR_COUNT
        or any(
            item.as_dict()["interpretation_scope"]
            != "DETECTOR_INTERPRETATION_NOT_HISTORICAL_FACT"
            or item.as_dict()["record_kind"] != "RAW_DETECTOR_FINDING_V1"
            or item.as_dict()["schema_version"] != 1
            or item.source_ancestry_sha256 != ancestry.sha256
            for item in findings
        )
    ):
        failures.append("B1 findings are not distinct interpretation-scoped evidence")
    sell_measurements = {
        "AGGRESSIVE_FLOW_BURST": {
            "active_buy_quantity": 300,
            "active_sell_quantity": 1700,
            "group_duration_us": 1_000_000,
            "trailing_group_volumes": (400,) * 20,
        },
        "FAILED_BREAKOUT": {
            "first_breakout_mid_x2": 198,
            "last_beyond_extreme_elapsed_us": 1_999_999,
            "prior_extreme_lookback_us": 5_000_000,
            "prior_extreme_mid_x2": 200,
            "return_elapsed_us": 3_000_000,
            "return_mid_x2": 202,
        },
        "MEAN_REVERSION_TRANSITION": {
            "activation_aggressive_flow_imbalance_ppm": -300_000,
            "final_displacement_mid_x2": -5,
            "initial_displacement_mid_x2": -10,
            "return_aggressive_flow_imbalance_ppm": 300_000,
            "return_elapsed_us": 15_000_000,
            "trailing_p50_window_us": 30_000_000,
        },
        "MOMENTUM_EXHAUSTION": {
            "additional_mid_x2_movement": -2,
            "forward_aggressive_flow_imbalance_ppm": -200_000,
            "forward_window_us": 5_000_000,
            "initial_aggressive_flow_imbalance_ppm": -700_000,
            "initial_mid_x2_movement": -10,
            "initial_window_us": 10_000_000,
        },
        "STRONG_QUEUE_IMBALANCE": {
            "ask_top": 800,
            "best_ask_ticks": 101,
            "best_bid_ticks": 100,
            "bid_top": 200,
            "continuous_duration_us": 2_000_000,
        },
    }
    sell_reports = []
    for detector_id, values in sell_measurements.items():
        base = opportunities[detector_id]
        sell = replace(
            base,
            opportunity_id=base.opportunity_id.replace("qualifying", "sell-symmetry"),
            direction=CandidateDirectionV1.SELL,
            side=(
                CandidateSideV1.NOT_APPLICABLE
                if detector_id in {"FAILED_BREAKOUT", "MEAN_REVERSION_TRANSITION"}
                else CandidateSideV1.SELL
            ),
            price=(101 if detector_id == "STRONG_QUEUE_IMBALANCE" else base.price),
            measurements=tuple(
                DetectorMeasurementV1(name, value)
                for name, value in values.items()
            ),
        )
        sell_reports.append(
            runtime.run(
                detector_id,
                _inventory_for(
                    detector_id,
                    EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                    ancestry.source_identity,
                ),
                ancestry,
                (sell,),
            )
        )
    if any(
        report.status is not DetectorRunStatusV1.EXERCISED
        or report.qualifying_units != 1
        for report in sell_reports
    ):
        failures.append("a directional B1 detector is not BUY/SELL symmetric")
    cancellation = opportunities["CANCELLATION_BURST"]
    positive_infinity = replace(
        cancellation,
        opportunity_id="b1-cancellation-burst-positive-infinity",
        measurements=(
            DetectorMeasurementV1("active_added_quantity", 0),
            DetectorMeasurementV1("active_cancelled_quantity", 2000),
            DetectorMeasurementV1("group_duration_us", 1_000_000),
            DetectorMeasurementV1(
                "trailing_group_cancelled_quantities",
                (400,) * 20,
            ),
        ),
    )
    positive_infinity_report = runtime.run(
        "CANCELLATION_BURST",
        _inventory_for(
            "CANCELLATION_BURST",
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
            ancestry.source_identity,
        ),
        ancestry,
        (positive_infinity,),
    )
    positive_infinity_derived = {
        item.name: item.value
        for item in positive_infinity_report.findings[0].derived_measurements
    } if positive_infinity_report.findings else {}
    if (
        positive_infinity_report.qualifying_units != 1
        or positive_infinity_derived.get("cancel_to_add_positive_infinity") is not True
    ):
        failures.append("positive cancellations with zero adds lost infinity semantics")
    return DrillMiningAuditCase(
        "b1_synthetic_boundaries_activate_every_detector_with_explicit_denominators",
        (
            f"detectors={len(emitted_ids)}/15 considered={considered} "
            f"eligible={eligible} emitted={emitted} below_threshold={below} "
            f"sell_symmetry={sum(report.qualifying_units for report in sell_reports)}/5 "
            "positive_infinity=qualified threshold_equality=qualified"
        ),
        tuple(failures),
    )


def _b1_historical_capability_and_label_case(
    runtime,
    opportunities: dict[str, object],
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        DetectorRunStatusV1,
        FindingEvidenceLabelV1,
        OpportunityDispositionV1,
    )

    failures: list[str] = []
    historical_identity = SourceIdentityV1(
        SourceKindV1.DATASET,
        "b1-weak-historical-source",
        _digest("b1-weak-historical-source"),
    )
    historical_ancestry = SourceAncestryV1(
        historical_identity.kind,
        historical_identity.source_id,
        historical_identity.source_sha256,
    )
    hidden_inventory = _inventory_for(
        "HIDDEN_RESERVE_REFRESH",
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
        historical_identity,
    )
    weak_hidden = replace(
        hidden_inventory,
        available_records=tuple(
            item
            for item in hidden_inventory.available_records
            if item.capability != "AUTHORITATIVE_RESERVE_REFRESH_LABELS"
        ),
    )
    hidden_report = runtime.run(
        "HIDDEN_RESERVE_REFRESH",
        weak_hidden,
        historical_ancestry,
        (opportunities["HIDDEN_RESERVE_REFRESH"],),
    )
    flow_inventory = _inventory_for(
        "FAILED_BREAKOUT",
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
        historical_identity,
    )
    historical_interpretation = runtime.run(
        "FAILED_BREAKOUT",
        flow_inventory,
        historical_ancestry,
        (opportunities["FAILED_BREAKOUT"],),
    )
    weak_flow = replace(
        flow_inventory,
        available_records=tuple(
            item
            for item in flow_inventory.available_records
            if item.capability != "TRADE_AGGRESSOR_SIDE"
        ),
    )
    flow_report = runtime.run(
        "FAILED_BREAKOUT",
        weak_flow,
        historical_ancestry,
        (opportunities["FAILED_BREAKOUT"],),
    )
    for report, capability in (
        (hidden_report, "AUTHORITATIVE_RESERVE_REFRESH_LABELS"),
        (flow_report, "TRADE_AGGRESSOR_SIDE"),
    ):
        if (
            report.status is not DetectorRunStatusV1.NOT_EXERCISED
            or report.reason_code != "INSUFFICIENT_SOURCE_CAPABILITY"
            or report.missing_capabilities != (capability,)
            or report.eligible_units != 0
            or report.findings
            or report.considered[0].disposition
            is not OpportunityDispositionV1.NOT_EXERCISED
        ):
            failures.append(f"weak historical source did not refuse {capability}")

    reconstruction_identity = SourceIdentityV1(
        SourceKindV1.RECONSTRUCTION,
        "b1-reconstruction-source",
        _digest("b1-reconstruction-source"),
    )
    reconstruction_ancestry = SourceAncestryV1(
        reconstruction_identity.kind,
        reconstruction_identity.source_id,
        reconstruction_identity.source_sha256,
    )
    reconstruction_report = runtime.run(
        "FAILED_BREAKOUT",
        _inventory_for(
            "FAILED_BREAKOUT",
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
            reconstruction_identity,
        ),
        reconstruction_ancestry,
        (opportunities["FAILED_BREAKOUT"],),
    )
    unsupported_report = runtime.run(
        "STRONG_QUEUE_IMBALANCE",
        _inventory_for(
            "STRONG_QUEUE_IMBALANCE",
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
            reconstruction_identity,
        ),
        reconstruction_ancestry,
        (opportunities["STRONG_QUEUE_IMBALANCE"],),
    )
    if (
        reconstruction_report.status is not DetectorRunStatusV1.EXERCISED
        or len(reconstruction_report.findings) != 1
        or reconstruction_report.findings[0].evidence_label
        is not FindingEvidenceLabelV1.SYNTHETIC_RECONSTRUCTION
        or reconstruction_report.findings[0].as_dict()["interpretation_scope"]
        != "DETECTOR_INTERPRETATION_NOT_HISTORICAL_FACT"
    ):
        failures.append("reconstruction detector evidence was mislabeled as historical fact")
    if (
        historical_interpretation.status is not DetectorRunStatusV1.EXERCISED
        or len(historical_interpretation.findings) != 1
        or historical_interpretation.findings[0].evidence_label
        is not FindingEvidenceLabelV1.HISTORICAL_DETECTOR_INTERPRETATION
        or historical_interpretation.findings[0].as_dict()["interpretation_scope"]
        != "DETECTOR_INTERPRETATION_NOT_HISTORICAL_FACT"
    ):
        failures.append("historical detector interpretation claimed fact status")
    if (
        unsupported_report.status is not DetectorRunStatusV1.NOT_EXERCISED
        or unsupported_report.reason_code != "UNSUPPORTED_EVIDENCE_CLASS"
        or unsupported_report.eligible_units != 0
        or unsupported_report.findings
    ):
        failures.append("unsupported reconstruction was treated as detector false")
    return DrillMiningAuditCase(
        "b1_weak_historical_sources_refuse_and_reconstruction_stays_synthetic",
        (
            "weak_historical_refusals=2/2 denominator=0 "
            "hidden_label_required=true aggressor_side_required=true "
            "historical_label=HISTORICAL_DETECTOR_INTERPRETATION "
            "reconstruction_label=SYNTHETIC_RECONSTRUCTION "
            "unsupported_reconstruction=NOT_EXERCISED"
        ),
        tuple(failures),
    )


def _b1_canonical_order_and_exclusion_case(
    runtime,
    opportunities: dict[str, object],
    source: SourceCapabilityInventoryV1,
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        DetectorRunStatusV1,
        MiningExclusionV1,
        OpportunityDispositionV1,
    )

    failures: list[str] = []
    qualifying = opportunities["STRONG_QUEUE_IMBALANCE"]
    weak = _b1_weak_strong_opportunity(qualifying)
    canonical = runtime.run(
        "STRONG_QUEUE_IMBALANCE",
        source,
        ancestry,
        (qualifying, weak),
    )
    reordered_qualifying = replace(
        qualifying,
        measurements=tuple(reversed(qualifying.measurements)),
        contributing_events=tuple(reversed(qualifying.contributing_events)),
    )
    reordered_weak = replace(
        weak,
        measurements=tuple(reversed(weak.measurements)),
        contributing_events=tuple(reversed(weak.contributing_events)),
    )
    reordered = runtime.run(
        "STRONG_QUEUE_IMBALANCE",
        source,
        ancestry,
        (reordered_weak, reordered_qualifying),
    )
    if canonical.report_sha256 != reordered.report_sha256:
        failures.append("reordered opportunity storage changed detector evidence")
    excluded = replace(
        qualifying,
        opportunity_id="b1-strong-queue-auction-excluded",
        exclusions=(MiningExclusionV1.AUCTION,),
    )
    excluded_report = runtime.run(
        "STRONG_QUEUE_IMBALANCE",
        source,
        ancestry,
        (excluded,),
    )
    if (
        excluded_report.status is not DetectorRunStatusV1.NOT_EXERCISED
        or excluded_report.reason_code != "ZERO_ELIGIBLE_DENOMINATOR"
        or excluded_report.eligible_units != 0
        or excluded_report.excluded_units != 1
        or excluded_report.qualifying_units != 0
        or excluded_report.considered[0].disposition
        is not OpportunityDispositionV1.EXCLUDED
        or excluded_report.considered[0].reason_codes != ("AUCTION",)
    ):
        failures.append("ordinary exclusion did not remain outside the denominator")
    return DrillMiningAuditCase(
        "b1_canonical_order_is_storage_independent_and_exclusions_are_recorded",
        (
            f"canonical_report_sha256={canonical.report_sha256} "
            "reordered_equal=true exclusions=1 eligible=0 emitted=0"
        ),
        tuple(failures),
    )


def _b1_hostile_runtime_refusal_case(
    runtime,
    opportunities: dict[str, object],
    source: SourceCapabilityInventoryV1,
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.flow_detectors import FLOW_DETECTOR_HANDLERS_V1
    from kirby2.mining.queue_detectors import QUEUE_DETECTOR_HANDLERS_V1
    from kirby2.mining.runtime import (
        DetectorMeasurementV1,
        MiningDetectorRuntimeV1,
    )

    failures: list[str] = []
    refusals = 0
    changed_manifest = replace(
        runtime.threshold_manifest,
        manifest_sha256="0" * 64,
    )
    strong = opportunities["STRONG_QUEUE_IMBALANCE"]
    incomplete_handlers = {**QUEUE_DETECTOR_HANDLERS_V1, **FLOW_DETECTOR_HANDLERS_V1}
    incomplete_handlers.pop("STRONG_QUEUE_IMBALANCE")
    probes: tuple[Callable[[], object], ...] = (
        lambda: MiningDetectorRuntimeV1(threshold_manifest=changed_manifest),
        lambda: MiningDetectorRuntimeV1(handlers=incomplete_handlers),
        lambda: MiningDetectorRuntimeV1(handlers={}),
        lambda: runtime.run(
            "STRONG_QUEUE_IMBALANCE",
            source,
            ancestry,
            (replace(strong, sampling_unit="ALIGNED_ONE_SECOND_GROUP"),),
        ),
        lambda: runtime.run(
            "STRONG_QUEUE_IMBALANCE",
            source,
            ancestry,
            (replace(strong, venue="XNAS"),),
        ),
        lambda: runtime.run(
            "STRONG_QUEUE_IMBALANCE",
            source,
            ancestry,
            (
                replace(
                    strong,
                    measurements=(DetectorMeasurementV1("bid_top", 800),),
                ),
            ),
        ),
        lambda: replace(
            strong,
            contributing_events=(
                strong.contributing_events[0],
                strong.contributing_events[0],
            ),
        ),
        lambda: replace(
            strong,
            contributing_events=(
                replace(strong.contributing_events[0], source_sequence=2),
                replace(strong.contributing_events[1], source_sequence=1),
            ),
        ),
    )
    for probe in probes:
        if _raises(probe):
            refusals += 1
        else:
            failures.append("hostile B1 runtime input was accepted")
    if refusals != len(probes):
        failures.append("B1 hostile refusal inventory is incomplete")
    return DrillMiningAuditCase(
        "b1_manifest_schema_sampling_measurement_and_event_mutations_fail_closed",
        (
            f"refusals={refusals}/{len(probes)} manifest_pin=true "
            "handler_inventory=closed key_axes=closed sampling_unit=closed "
            "measurements=exact source_sequence=unique_and_chronological"
        ),
        tuple(failures),
    )


def _b1_synthetic_reports():
    from kirby2.mining.runtime import B1_DETECTOR_IDS_V1, MiningDetectorRuntimeV1

    runtime = MiningDetectorRuntimeV1()
    source_identity = SourceIdentityV1(
        SourceKindV1.RUN,
        "b1-synthetic-source",
        _digest("b1-synthetic-source"),
    )
    ancestry = SourceAncestryV1(
        source_identity.kind,
        source_identity.source_id,
        source_identity.source_sha256,
    )
    opportunities = {
        detector_id: _b1_qualifying_opportunity(runtime, detector_id)
        for detector_id in B1_DETECTOR_IDS_V1
    }
    reports: dict[str, object] = {}
    for detector_id in B1_DETECTOR_IDS_V1:
        detector_opportunities = (opportunities[detector_id],)
        if detector_id == "STRONG_QUEUE_IMBALANCE":
            detector_opportunities = (
                opportunities[detector_id],
                _b1_weak_strong_opportunity(opportunities[detector_id]),
            )
        reports[detector_id] = runtime.run(
            detector_id,
            _inventory_for(
                detector_id,
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            detector_opportunities,
        )
    strong_source = _inventory_for(
        "STRONG_QUEUE_IMBALANCE",
        EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
        source_identity,
    )
    return runtime, opportunities, reports, strong_source, ancestry


def _b1_qualifying_opportunity(runtime, detector_id: str):
    from kirby2.mining.runtime import (
        DetectorMeasurementV1,
        DetectorOpportunityV1,
        MiningEventReferenceV1,
    )

    specifications: dict[str, tuple[object, ...]] = {
        "AGGRESSIVE_FLOW_BURST": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.BUY,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "active_buy_quantity": 1700,
                "active_sell_quantity": 300,
                "group_duration_us": 1_000_000,
                "trailing_group_volumes": (400,) * 20,
            },
        ),
        "APPARENT_LIQUIDITY_MIRAGE": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "XNAS",
            101,
            "ORDER_COHORT",
            ("ORDER-B", "ORDER-A"),
            {
                "cohort_cancelled_quantity": 1600,
                "cohort_displayed_peak": 2000,
                "cohort_executed_quantity": 400,
                "cohort_frozen_at_first_boundary": True,
                "elapsed_us": 500_000,
            },
        ),
        "ASK_ABSORPTION": (
            CandidateDirectionV1.SELL,
            CandidateSideV1.SELL,
            "CONSOLIDATED",
            101,
            "NOT_APPLICABLE",
            (),
            {
                "aggressive_buy_quantity": 1000,
                "ask_add_and_refresh_at_opening_price": 300,
                "elapsed_us": 2_000_000,
                "maximum_best_ask_ticks": 101,
                "opening_best_ask_ticks": 101,
            },
        ),
        "BID_ABSORPTION": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.BUY,
            "CONSOLIDATED",
            100,
            "NOT_APPLICABLE",
            (),
            {
                "aggressive_sell_quantity": 1000,
                "bid_add_and_refresh_at_opening_price": 300,
                "elapsed_us": 2_000_000,
                "minimum_best_bid_ticks": 100,
                "opening_best_bid_ticks": 100,
            },
        ),
        "CANCELLATION_BURST": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "active_added_quantity": 1000,
                "active_cancelled_quantity": 3000,
                "group_duration_us": 1_000_000,
                "trailing_group_cancelled_quantities": (400,) * 20,
            },
        ),
        "FAILED_BREAKOUT": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.NOT_APPLICABLE,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "first_breakout_mid_x2": 202,
                "last_beyond_extreme_elapsed_us": 1_999_999,
                "prior_extreme_lookback_us": 5_000_000,
                "prior_extreme_mid_x2": 200,
                "return_elapsed_us": 3_000_000,
                "return_mid_x2": 198,
            },
        ),
        "HIDDEN_RESERVE_REFRESH": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "XNAS",
            101,
            "NOT_APPLICABLE",
            (),
            {
                "authoritative_refresh_labels": True,
                "elapsed_us": 5_000_000,
                "executed_quantity": 1500,
                "maximum_displayed_quantity": 500,
                "reserve_refresh_after_execution_count": 3,
            },
        ),
        "LIQUIDITY_VACUUM": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "CONSOLIDATED",
            101,
            "NOT_APPLICABLE",
            (),
            {
                "cancelled_quantity": 600,
                "elapsed_us": 1_000_000,
                "ending_spread_ticks": 4,
                "executed_quantity": 400,
                "minimum_three_level_depth": 1000,
                "side_empty": False,
                "starting_spread_ticks": 2,
                "starting_three_level_depth": 4000,
            },
        ),
        "MEAN_REVERSION_TRANSITION": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.NOT_APPLICABLE,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "activation_aggressive_flow_imbalance_ppm": 300_000,
                "final_displacement_mid_x2": 5,
                "initial_displacement_mid_x2": 10,
                "return_aggressive_flow_imbalance_ppm": -300_000,
                "return_elapsed_us": 15_000_000,
                "trailing_p50_window_us": 30_000_000,
            },
        ),
        "MOMENTUM_EXHAUSTION": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.BUY,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "additional_mid_x2_movement": 2,
                "forward_aggressive_flow_imbalance_ppm": 200_000,
                "forward_window_us": 5_000_000,
                "initial_aggressive_flow_imbalance_ppm": 700_000,
                "initial_mid_x2_movement": 10,
                "initial_window_us": 10_000_000,
            },
        ),
        "QUEUE_DEPLETION": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "CONSOLIDATED",
            101,
            "NOT_APPLICABLE",
            (),
            {
                "elapsed_us": 1_000_000,
                "minimum_displayed_quantity": 300,
                "starting_displayed_quantity": 1000,
            },
        ),
        "QUEUE_REPLENISHMENT": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.SELL,
            "CONSOLIDATED",
            101,
            "NOT_APPLICABLE",
            (),
            {
                "cumulative_add_and_refresh": 1000,
                "cycle_minimum_quantities": (500, 500, 500),
                "cycle_return_elapsed_us": (500_000, 500_000, 500_000),
                "cycle_return_quantities": (500, 500, 500),
                "cycle_start_quantities": (1000, 1000, 1000),
                "elapsed_us": 5_000_000,
                "greedy_nonoverlapping_cycles": True,
            },
        ),
        "SPREAD_EXPANSION": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.NOT_APPLICABLE,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "NOT_APPLICABLE",
            (),
            {
                "expanded_spread_ticks": 4,
                "persistence_us": 500_000,
                "starting_spread_ticks": 2,
                "transition_elapsed_us": 500_000,
            },
        ),
        "SPREAD_RECOVERY": (
            CandidateDirectionV1.NOT_APPLICABLE,
            CandidateSideV1.NOT_APPLICABLE,
            "CONSOLIDATED",
            "NOT_APPLICABLE",
            "SPREAD_EXPANSION_PARENT",
            ("lesson-candidate-" + _digest("b1-spread-expansion-parent"),),
            {
                "persistence_us": 1_000_000,
                "recovered_spread_ticks": 2,
                "recovery_elapsed_us": 5_000_000,
            },
        ),
        "STRONG_QUEUE_IMBALANCE": (
            CandidateDirectionV1.BUY,
            CandidateSideV1.BUY,
            "CONSOLIDATED",
            100,
            "NOT_APPLICABLE",
            (),
            {
                "ask_top": 200,
                "best_ask_ticks": 101,
                "best_bid_ticks": 100,
                "bid_top": 800,
                "continuous_duration_us": 2_000_000,
            },
        ),
    }
    (
        direction,
        side,
        venue,
        price,
        witness_kind,
        witness_ids,
        measurements,
    ) = specifications[detector_id]
    row = runtime.threshold_manifest.detector(detector_id)
    active_duration_us = {
        "AGGRESSIVE_FLOW_BURST": 1_000_000,
        "APPARENT_LIQUIDITY_MIRAGE": 500_000,
        "ASK_ABSORPTION": 2_000_000,
        "BID_ABSORPTION": 2_000_000,
        "CANCELLATION_BURST": 1_000_000,
        "FAILED_BREAKOUT": 3_000_000,
        "HIDDEN_RESERVE_REFRESH": 5_000_000,
        "LIQUIDITY_VACUUM": 1_000_000,
        "MEAN_REVERSION_TRANSITION": 15_000_000,
        "MOMENTUM_EXHAUSTION": 15_000_000,
        "QUEUE_DEPLETION": 1_000_000,
        "QUEUE_REPLENISHMENT": 5_000_000,
        "SPREAD_EXPANSION": 1_000_000,
        "SPREAD_RECOVERY": 6_000_000,
        "STRONG_QUEUE_IMBALANCE": 2_000_000,
    }[detector_id]
    activation_us = 60_000_000
    active_start_us = activation_us - active_duration_us
    event_prefix = detector_id.lower().replace("_", "-")
    return DetectorOpportunityV1(
        detector_id=detector_id,
        opportunity_id=f"b1-{event_prefix}-qualifying",
        sampling_unit=str(row["sampling_unit"]),
        source_start_us=0,
        source_end_us=100_000_000,
        active_start_us=active_start_us,
        activation_us=activation_us,
        direction=direction,
        side=side,
        venue=venue,
        price=price,
        witness_kind=witness_kind,
        witness_ids=witness_ids,
        measurements=tuple(
            DetectorMeasurementV1(name, value)
            for name, value in reversed(tuple(measurements.items()))
        ),
        contributing_events=(
            MiningEventReferenceV1(
                f"{event_prefix}-event-2",
                activation_us,
                2,
            ),
            MiningEventReferenceV1(
                f"{event_prefix}-event-1",
                active_start_us,
                1,
            ),
        ),
    )


def _b1_weak_strong_opportunity(qualifying):
    from kirby2.mining.runtime import DetectorMeasurementV1

    return replace(
        qualifying,
        opportunity_id="b1-strong-queue-imbalance-below-threshold",
        measurements=(
            DetectorMeasurementV1("ask_top", 300),
            DetectorMeasurementV1("best_ask_ticks", 101),
            DetectorMeasurementV1("best_bid_ticks", 100),
            DetectorMeasurementV1("bid_top", 700),
            DetectorMeasurementV1("continuous_duration_us", 2_000_000),
        ),
        contributing_events=tuple(
            replace(
                event,
                event_id=event.event_id.replace("event", "weak-event"),
            )
            for event in qualifying.contributing_events
        ),
    )


def _b2_runtime_and_manifest_binding_case(
    runtime,
    reports: dict[str, object],
) -> DrillMiningAuditCase:
    from kirby2.mining.latency_detectors import LATENCY_DETECTOR_HANDLERS_V1
    from kirby2.mining.mechanics_detectors import MECHANICS_DETECTOR_HANDLERS_V1
    from kirby2.mining.runtime import (
        B2_DETECTOR_IDS_V1,
        DETECTOR_RUNTIME_ID_V1,
        OPERATIONAL_DETECTOR_IDS_V1,
        WO33A1_THRESHOLD_MANIFEST_SHA256_V1,
    )
    from kirby2.mining.venue_detectors import VENUE_DETECTOR_HANDLERS_V1

    failures: list[str] = []
    modules = (
        LATENCY_DETECTOR_HANDLERS_V1,
        MECHANICS_DETECTOR_HANDLERS_V1,
        VENUE_DETECTOR_HANDLERS_V1,
    )
    handlers = {key: value for module in modules for key, value in module.items()}
    operational_b2_ids = tuple(
        detector_id
        for detector_id in runtime.handler_ids
        if detector_id in set(B2_DETECTOR_IDS_V1)
    )
    if (
        runtime.handler_ids != OPERATIONAL_DETECTOR_IDS_V1
        or operational_b2_ids != B2_DETECTOR_IDS_V1
        or len(handlers) != WO33B2_DETECTOR_COUNT
        or len({id(handler) for handler in handlers.values()})
        != WO33B2_DETECTOR_COUNT
    ):
        failures.append("B2 does not expose seven distinct handlers in the closed runtime")
    if (
        runtime.threshold_manifest.manifest_sha256
        != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
    ):
        failures.append("B2 runtime did not pin the committed A1 threshold manifest")
    for detector_id in B2_DETECTOR_IDS_V1:
        row = runtime.threshold_manifest.detector(detector_id)
        report = reports[detector_id]
        if (
            row["detector_id"] != detector_id
            or row["version"] != 1
            or not row["thresholds"]
            or report.detector.threshold_sha256
            != runtime.threshold_manifest.detector_threshold_sha256(detector_id)
            or report.threshold_manifest_sha256
            != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
            or report.as_dict()["runtime_id"] != DETECTOR_RUNTIME_ID_V1
            or report.as_dict()["schema_version"] != 1
        ):
            failures.append(f"{detector_id} did not consume its exact A1 rule row")
    aggregate_sha256 = sha256_json(
        [reports[detector_id].as_dict() for detector_id in B2_DETECTOR_IDS_V1]
    )
    if aggregate_sha256 != WO33B2_SYNTHETIC_REPORT_SHA256:
        failures.append("B2 synthetic runtime evidence digest changed")
    return DrillMiningAuditCase(
        "b2_seven_distinct_handlers_extend_the_closed_runtime_and_bind_a1",
        (
            f"handlers={len(operational_b2_ids)}/7 runtime_handlers="
            f"{len(runtime.handler_ids)}/22 versions=1 "
            f"threshold_manifest_sha256={runtime.threshold_manifest.manifest_sha256} "
            f"synthetic_report_sha256={aggregate_sha256}"
        ),
        tuple(failures),
    )


def _b2_synthetic_activation_and_branch_case(
    runtime,
    opportunities: dict[str, object],
    reports: dict[str, object],
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        B2_DETECTOR_IDS_V1,
        DetectorRunStatusV1,
        FindingEvidenceLabelV1,
        time_weighted_nearest_rank_p50,
    )

    failures: list[str] = []
    emitted_ids = {
        detector_id
        for detector_id, report in reports.items()
        if report.status is DetectorRunStatusV1.EXERCISED
        and report.eligible_units == 1
        and report.qualifying_units == 1
        and len(report.findings) == 1
    }
    if emitted_ids != set(B2_DETECTOR_IDS_V1):
        failures.append("a B2 synthetic boundary fixture did not emit exactly once")
    if any(
        report.sample_frequency_ppm != 1_000_000
        for report in reports.values()
    ):
        failures.append("B2 sample frequency is not derived from its denominator")
    findings = [finding for report in reports.values() for finding in report.findings]
    if (
        len({finding.finding_sha256 for finding in findings})
        != WO33B2_DETECTOR_COUNT
        or any(
            finding.source_ancestry_sha256 != ancestry.sha256
            or finding.evidence_label
            is not FindingEvidenceLabelV1.AUTHORITATIVE_SYNTHETIC_GROUND_TRUTH
            or finding.as_dict()["record_kind"] != "RAW_DETECTOR_FINDING_V1"
            for finding in findings
        )
    ):
        failures.append("B2 findings lost identity, ancestry, or evidence scope")

    source_identity = ancestry.source_identity
    latency_cost_only = _replace_b2_measurements(
        opportunities["LATENCY_SENSITIVE_OPPORTUNITY"],
        opportunity_id="b2-latency-cost-only-branch",
        fast_filled_quantity=100,
        slow_filled_quantity=100,
        fast_fee_adjusted_average_cost_milliticks_per_share=100_000,
        slow_fee_adjusted_average_cost_milliticks_per_share=101_000,
    )
    auction_absolute_only = _replace_b2_measurements(
        opportunities["AUCTION_IMBALANCE_CHANGE"],
        opportunity_id="b2-auction-absolute-relative-only-branch",
        old_imbalance_shares=40_000,
        new_imbalance_shares=50_000,
    )
    halt_spread_only = _replace_b2_measurements(
        opportunities["HALT_REOPENING"],
        opportunity_id="b2-halt-spread-only-branch",
        direction=CandidateDirectionV1.NOT_APPLICABLE,
        first_post_reopen_trade_ticks=101,
    )
    distressed_finite = _replace_b2_measurements(
        opportunities["DISTRESSED_LIQUIDATION"],
        opportunity_id="b2-distressed-finite-ratio-branch",
        distressed_buy_quantity=1_250,
    )
    branch_opportunities = (
        ("LATENCY_SENSITIVE_OPPORTUNITY", latency_cost_only),
        ("AUCTION_IMBALANCE_CHANGE", auction_absolute_only),
        ("HALT_REOPENING", halt_spread_only),
        ("DISTRESSED_LIQUIDATION", distressed_finite),
    )
    branch_reports = {
        detector_id: runtime.run(
            detector_id,
            _inventory_for(
                detector_id,
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (opportunity,),
        )
        for detector_id, opportunity in branch_opportunities
    }
    branch_measurements = {
        detector_id: _derived_measurement_map(report)
        for detector_id, report in branch_reports.items()
    }
    cancel_measurements = _derived_measurement_map(reports["CANCEL_FILL_RACE"])
    latency_base = opportunities["LATENCY_SENSITIVE_OPPORTUNITY"]
    latency_sell = replace(
        latency_base,
        opportunity_id="b2-latency-sell-symmetry",
        direction=CandidateDirectionV1.SELL,
        side=CandidateSideV1.SELL,
        witness_ids=(
            latency_base.witness_ids[0],
            latency_base.witness_ids[1],
            latency_base.witness_ids[2],
            "SELL",
        ),
    )
    symmetric_opportunities = (
        ("LATENCY_SENSITIVE_OPPORTUNITY", latency_sell),
        (
            "CANCEL_FILL_RACE",
            _replace_b2_measurements(
                opportunities["CANCEL_FILL_RACE"],
                opportunity_id="b2-cancel-fill-sell-symmetry",
                side=CandidateSideV1.SELL,
            ),
        ),
        (
            "MULTI_VENUE_FRAGMENTATION",
            _replace_b2_measurements(
                opportunities["MULTI_VENUE_FRAGMENTATION"],
                opportunity_id="b2-fragmentation-sell-symmetry",
                side=CandidateSideV1.SELL,
            ),
        ),
        (
            "ROUTING_DILEMMA",
            _replace_b2_measurements(
                opportunities["ROUTING_DILEMMA"],
                opportunity_id="b2-routing-sell-symmetry",
                direction=CandidateDirectionV1.SELL,
                side=CandidateSideV1.SELL,
            ),
        ),
        (
            "AUCTION_IMBALANCE_CHANGE",
            _replace_b2_measurements(
                opportunities["AUCTION_IMBALANCE_CHANGE"],
                opportunity_id="b2-auction-sell-symmetry",
                direction=CandidateDirectionV1.SELL,
                side=CandidateSideV1.SELL,
                old_imbalance_shares=5_000,
                new_imbalance_shares=-5_000,
            ),
        ),
        (
            "HALT_REOPENING",
            _replace_b2_measurements(
                opportunities["HALT_REOPENING"],
                opportunity_id="b2-halt-sell-symmetry",
                direction=CandidateDirectionV1.SELL,
                first_post_reopen_trade_ticks=97,
            ),
        ),
    )
    symmetric_reports = tuple(
        runtime.run(
            detector_id,
            _inventory_for(
                detector_id,
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (opportunity,),
        )
        for detector_id, opportunity in symmetric_opportunities
    )
    if (
        any(report.qualifying_units != 1 for report in branch_reports.values())
        or any(report.qualifying_units != 1 for report in symmetric_reports)
        or branch_measurements["LATENCY_SENSITIVE_OPPORTUNITY"].get(
            "cost_difference_branch_satisfied"
        )
        is not True
        or branch_measurements["LATENCY_SENSITIVE_OPPORTUNITY"].get(
            "fill_difference_branch_satisfied"
        )
        is not False
        or branch_measurements["AUCTION_IMBALANCE_CHANGE"].get(
            "absolute_relative_branch_satisfied"
        )
        is not True
        or branch_measurements["AUCTION_IMBALANCE_CHANGE"].get(
            "sign_change_branch_satisfied"
        )
        is not False
        or branch_measurements["HALT_REOPENING"].get(
            "price_gap_branch_satisfied"
        )
        is not False
        or branch_measurements["HALT_REOPENING"].get(
            "spread_branch_satisfied"
        )
        is not True
        or branch_measurements["DISTRESSED_LIQUIDATION"].get(
            "sell_to_buy_ratio_ppm"
        )
        != 4_000_000
        or branch_measurements["DISTRESSED_LIQUIDATION"].get(
            "sell_to_buy_positive_infinity"
        )
        is not False
        or cancel_measurements.get("fast_terminal_outcome") != "CANCEL"
        or cancel_measurements.get("slow_terminal_outcome") != "FULL_FILL"
        or time_weighted_nearest_rank_p50(
            (1, 2, 10),
            (1_000_000, 1_000_000, 3_000_000),
        )
        != 10
    ):
        failures.append("a B2 alternate OR branch or terminal race semantic differs")
    return DrillMiningAuditCase(
        "b2_exact_synthetic_boundaries_activate_all_detectors_and_or_branches",
        (
            f"detectors={len(emitted_ids)}/7 considered=7 eligible=7 emitted=7 "
            "alternate_or_branches=4/4 cancel_race=CANCEL_vs_FULL_FILL "
            "positive_infinity_and_finite_ratio=qualified sell_symmetry=6/6 "
            "weighted_p50=10"
        ),
        tuple(failures),
    )


def _b2_capability_refusal_case(
    runtime,
    opportunities: dict[str, object],
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        DetectorRunStatusV1,
        OpportunityDispositionV1,
    )

    failures: list[str] = []
    source_identity = SourceIdentityV1(
        SourceKindV1.RUN,
        "b2-capability-refusal-source",
        _digest("b2-capability-refusal-source"),
    )
    ancestry = SourceAncestryV1(
        source_identity.kind,
        source_identity.source_id,
        source_identity.source_sha256,
    )
    removed_capabilities = {
        "AUCTION_IMBALANCE_CHANGE": "PUBLISHED_IMBALANCE",
        "CANCEL_FILL_RACE": "ORDER_IDENTITY",
        "DISTRESSED_LIQUIDATION": "AUTHORITATIVE_PARTICIPANT_IDENTITY",
        "HALT_REOPENING": "HALT_STATE",
        "LATENCY_SENSITIVE_OPPORTUNITY": "PORTABLE_CHECKPOINT",
        "MULTI_VENUE_FRAGMENTATION": "PER_VENUE_QUOTES",
        "ROUTING_DILEMMA": "RECEIPT_LATENCY_MODEL",
    }
    refused = 0
    for detector_id, missing_capability in removed_capabilities.items():
        inventory = _inventory_for(
            detector_id,
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
            source_identity,
        )
        weak_inventory = replace(
            inventory,
            available_records=tuple(
                record
                for record in inventory.available_records
                if record.capability != missing_capability
            ),
        )
        report = runtime.run(
            detector_id,
            weak_inventory,
            ancestry,
            (opportunities[detector_id],),
        )
        if (
            report.status is DetectorRunStatusV1.NOT_EXERCISED
            and report.reason_code == "INSUFFICIENT_SOURCE_CAPABILITY"
            and report.missing_capabilities == (missing_capability,)
            and report.eligible_units == 0
            and not report.findings
            and report.considered[0].disposition
            is OpportunityDispositionV1.NOT_EXERCISED
        ):
            refused += 1
        else:
            failures.append(
                f"{detector_id} did not refuse missing {missing_capability}"
            )

    historical_identity = SourceIdentityV1(
        SourceKindV1.DATASET,
        "b2-weak-historical-source",
        _digest("b2-weak-historical-source"),
    )
    historical_ancestry = SourceAncestryV1(
        historical_identity.kind,
        historical_identity.source_id,
        historical_identity.source_sha256,
    )
    market_by_order_foundation = _inventory_for(
        "STRONG_QUEUE_IMBALANCE",
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
        historical_identity,
    )
    unsupported_historical = tuple(
        runtime.run(
            detector_id,
            market_by_order_foundation,
            historical_ancestry,
            (opportunities[detector_id],),
        )
        for detector_id in (
            "LATENCY_SENSITIVE_OPPORTUNITY",
            "CANCEL_FILL_RACE",
            "ROUTING_DILEMMA",
        )
    )
    fragmented_inventory = _inventory_for(
        "MULTI_VENUE_FRAGMENTATION",
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
        historical_identity,
    )
    weak_fragmented = replace(
        fragmented_inventory,
        available_records=tuple(
            record
            for record in fragmented_inventory.available_records
            if record.capability != "PER_VENUE_QUOTES"
        ),
    )
    historical_fragmentation = runtime.run(
        "MULTI_VENUE_FRAGMENTATION",
        weak_fragmented,
        historical_ancestry,
        (opportunities["MULTI_VENUE_FRAGMENTATION"],),
    )
    if any(
        report.status is not DetectorRunStatusV1.NOT_EXERCISED
        or report.reason_code != "UNSUPPORTED_EVIDENCE_CLASS"
        or report.eligible_units != 0
        or report.findings
        for report in unsupported_historical
    ) or (
        historical_fragmentation.status
        is not DetectorRunStatusV1.NOT_EXERCISED
        or historical_fragmentation.reason_code
        != "INSUFFICIENT_SOURCE_CAPABILITY"
        or historical_fragmentation.missing_capabilities != ("PER_VENUE_QUOTES",)
    ):
        failures.append("historical execution evidence absence was silently reconstructed")
    return DrillMiningAuditCase(
        "b2_every_missing_capability_and_weak_historical_source_is_not_exercised",
        (
            f"missing_capability_refusals={refused}/7 denominator=0 "
            "historical_latency_cancel_route=UNSUPPORTED_EVIDENCE_CLASS "
            "historical_fragmentation_without_routes=INSUFFICIENT_SOURCE_CAPABILITY"
        ),
        tuple(failures),
    )


def _b2_canonical_ancestry_label_and_timing_case(
    runtime,
    opportunities: dict[str, object],
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        ASSESSMENT_DATA_POLICY_ID_V1,
        RETROSPECTIVE_METRIC_NAMES_V1,
        FindingEvidenceLabelV1,
    )

    failures: list[str] = []
    if RETROSPECTIVE_METRIC_NAMES_V1 != ("adverse_selection_x2_tick_shares",):
        failures.append("the declared retrospective metric inventory changed")
    fragmented = opportunities["MULTI_VENUE_FRAGMENTATION"]
    later_events = tuple(
        replace(
            event,
            event_id=f"{event.event_id}-later",
            timestamp_us=event.timestamp_us + 100_000,
            source_sequence=event.source_sequence + 100,
        )
        for event in fragmented.contributing_events
    )
    later = replace(
        fragmented,
        opportunity_id="b2-multi-venue-fragmentation-later",
        active_start_us=fragmented.active_start_us + 100_000,
        activation_us=fragmented.activation_us + 100_000,
        witness_ids=tuple(reversed(fragmented.witness_ids)),
        measurements=tuple(reversed(fragmented.measurements)),
        contributing_events=tuple(reversed(later_events)),
    )
    canonical_source = _inventory_for(
        "MULTI_VENUE_FRAGMENTATION",
        EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
        ancestry.source_identity,
    )
    canonical = runtime.run(
        "MULTI_VENUE_FRAGMENTATION",
        canonical_source,
        ancestry,
        (fragmented, later),
    )
    reordered_fragmented = replace(
        fragmented,
        witness_ids=tuple(reversed(fragmented.witness_ids)),
        measurements=tuple(reversed(fragmented.measurements)),
        contributing_events=tuple(reversed(fragmented.contributing_events)),
    )
    reordered_later = replace(
        later,
        witness_ids=tuple(reversed(later.witness_ids)),
        measurements=tuple(reversed(later.measurements)),
        contributing_events=tuple(reversed(later.contributing_events)),
    )
    reordered = runtime.run(
        "MULTI_VENUE_FRAGMENTATION",
        canonical_source,
        ancestry,
        (reordered_later, reordered_fragmented),
    )
    if (
        canonical.report_sha256 != reordered.report_sha256
        or any(
            finding.source_ancestry_sha256 != ancestry.sha256
            for finding in canonical.findings
        )
    ):
        failures.append("B2 canonical ordering changed evidence or source ancestry")

    historical_identity = SourceIdentityV1(
        SourceKindV1.DATASET,
        "b2-historical-fragmented-source",
        _digest("b2-historical-fragmented-source"),
    )
    historical_ancestry = SourceAncestryV1(
        historical_identity.kind,
        historical_identity.source_id,
        historical_identity.source_sha256,
    )
    historical = runtime.run(
        "MULTI_VENUE_FRAGMENTATION",
        _inventory_for(
            "MULTI_VENUE_FRAGMENTATION",
            EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
            historical_identity,
        ),
        historical_ancestry,
        (fragmented,),
    )
    reconstruction_identity = SourceIdentityV1(
        SourceKindV1.RECONSTRUCTION,
        "b2-route-reconstruction-source",
        _digest("b2-route-reconstruction-source"),
    )
    reconstruction_ancestry = SourceAncestryV1(
        reconstruction_identity.kind,
        reconstruction_identity.source_id,
        reconstruction_identity.source_sha256,
    )
    reconstruction = runtime.run(
        "ROUTING_DILEMMA",
        _inventory_for(
            "ROUTING_DILEMMA",
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
            reconstruction_identity,
        ),
        reconstruction_ancestry,
        (opportunities["ROUTING_DILEMMA"],),
    )
    if (
        historical.qualifying_units != 1
        or historical.findings[0].evidence_label
        is not FindingEvidenceLabelV1.HISTORICAL_DETECTOR_INTERPRETATION
        or reconstruction.qualifying_units != 1
        or reconstruction.findings[0].evidence_label
        is not FindingEvidenceLabelV1.SYNTHETIC_RECONSTRUCTION
    ):
        failures.append("B2 historical or reconstruction labels claimed fact status")

    assessment_keys = {
        "active_start_us",
        "assessment_data_policy_id",
        "detector_identity",
        "evidence_available_through_us",
        "outcome_data",
        "record_kind",
        "retrospective_metrics",
        "schema_version",
        "source_ancestry_sha256",
    }
    timing_safe = 0
    for detector_id in ("LATENCY_SENSITIVE_OPPORTUNITY", "HALT_REOPENING"):
        report = runtime.run(
            detector_id,
            _inventory_for(
                detector_id,
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                ancestry.source_identity,
            ),
            ancestry,
            (opportunities[detector_id],),
        )
        finding = report.findings[0]
        projection = finding.assessment_projection()
        projection_bytes = canonical_json_bytes(projection)
        retrospective = projection.get("retrospective_metrics")
        safe = (
            set(projection) == assessment_keys
            and projection["assessment_data_policy_id"]
            == ASSESSMENT_DATA_POLICY_ID_V1
            and projection["evidence_available_through_us"]
            == finding.bounds.activation_us
            and projection["active_start_us"] == finding.bounds.active_start_us
            and projection["outcome_data"] == "WITHHELD_DURING_ASSESSMENT"
            and projection["detector_identity"]
            == "WITHHELD_DURING_ASSESSMENT"
            and isinstance(retrospective, list)
            and tuple(row["name"] for row in retrospective)
            == RETROSPECTIVE_METRIC_NAMES_V1
            and all(
                row["status"] == "WITHHELD_DURING_ASSESSMENT"
                for row in retrospective
            )
            and detector_id.encode("utf-8") not in projection_bytes
            and b"post_end_us" not in projection_bytes
            and b"derived_measurements" not in projection_bytes
            and b"opportunity_sha256" not in projection_bytes
        )
        if safe:
            timing_safe += 1
        else:
            failures.append(f"{detector_id} assessment projection leaked replay outcome")
    return DrillMiningAuditCase(
        "b2_order_ancestry_labels_and_original_decision_timing_are_preserved",
        (
            f"canonical_report_sha256={canonical.report_sha256} "
            "reordered_equal=true ancestry_preserved=true "
            "historical_label=INTERPRETATION reconstruction_label=SYNTHETIC "
            f"timing_safe_assessments={timing_safe}/2 adverse_selection=WITHHELD"
        ),
        tuple(failures),
    )


def _b2_hostile_contract_refusal_case(
    runtime,
    opportunities: dict[str, object],
    ancestry: SourceAncestryV1,
) -> DrillMiningAuditCase:
    from kirby2.mining.runtime import (
        DetectorMeasurementV1,
        DetectorRunStatusV1,
        MiningDetectorRuntimeV1,
        OpportunityDispositionV1,
    )

    failures: list[str] = []
    source_identity = ancestry.source_identity
    latency = opportunities["LATENCY_SENSITIVE_OPPORTUNITY"]
    cancel = opportunities["CANCEL_FILL_RACE"]
    venue = opportunities["MULTI_VENUE_FRAGMENTATION"]
    auction = opportunities["AUCTION_IMBALANCE_CHANGE"]
    incomplete_handlers = dict(runtime.handlers)
    incomplete_handlers.pop("ROUTING_DILEMMA")
    probes: tuple[Callable[[], object], ...] = (
        lambda: MiningDetectorRuntimeV1(handlers=incomplete_handlers),
        lambda: replace(
            latency,
            witness_ids=(
                "not-a-checkpoint-digest",
                latency.witness_ids[1],
                latency.witness_ids[2],
                latency.witness_ids[3],
            ),
        ),
        lambda: replace(latency, witness_ids=latency.witness_ids[:3]),
        lambda: replace(venue, witness_ids=(venue.witness_ids[0],)),
        lambda: replace(auction, witness_ids=tuple(reversed(auction.witness_ids))),
        lambda: runtime.run(
            "CANCEL_FILL_RACE",
            _inventory_for(
                "CANCEL_FILL_RACE",
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (replace(cancel, sampling_unit="OBSERVABLE_BIN_100000_US"),),
        ),
        lambda: runtime.run(
            "CANCEL_FILL_RACE",
            _inventory_for(
                "CANCEL_FILL_RACE",
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (
                replace(
                    cancel,
                    witness_kind="ROUTE_PAIR",
                    witness_ids=("ROUTE-A", "ROUTE-B"),
                ),
            ),
        ),
        lambda: runtime.run(
            "LATENCY_SENSITIVE_OPPORTUNITY",
            _inventory_for(
                "LATENCY_SENSITIVE_OPPORTUNITY",
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (
                replace(
                    latency,
                    measurements=(
                        *latency.measurements,
                        DetectorMeasurementV1(
                            "adverse_selection_x2_tick_shares",
                            999_999,
                        ),
                    ),
                ),
            ),
        ),
    )
    refusals = sum(_raises(probe) for probe in probes)
    if refusals != len(probes):
        failures.append("a hostile B2 witness, schema, or handler mutation was accepted")

    route = opportunities["ROUTING_DILEMMA"]
    mismatched_route = replace(route, side=CandidateSideV1.SELL)
    route_report = runtime.run(
        "ROUTING_DILEMMA",
        _inventory_for(
            "ROUTING_DILEMMA",
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
            source_identity,
        ),
        ancestry,
        (mismatched_route,),
    )
    incomplete_halt = _replace_b2_measurements(
        opportunities["HALT_REOPENING"],
        opportunity_id="b2-halt-incomplete-spread-coverage",
        pre_window_coverage_us=4_999_999,
    )
    halt_report = runtime.run(
        "HALT_REOPENING",
        _inventory_for(
            "HALT_REOPENING",
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
            source_identity,
        ),
        ancestry,
        (incomplete_halt,),
    )
    if (
        route_report.status is not DetectorRunStatusV1.EXERCISED
        or route_report.qualifying_units != 0
        or route_report.considered[0].disposition
        is not OpportunityDispositionV1.BELOW_THRESHOLD
        or route_report.considered[0].reason_codes
        != ("ROUTING_DILEMMA_KEY_MISMATCH",)
        or halt_report.status is not DetectorRunStatusV1.NOT_EXERCISED
        or halt_report.reason_code != "ZERO_ELIGIBLE_DENOMINATOR"
        or halt_report.eligible_units != 0
        or halt_report.excluded_units != 1
        or halt_report.qualifying_units != 0
        or halt_report.considered[0].disposition
        is not OpportunityDispositionV1.EXCLUDED
        or halt_report.considered[0].reason_codes != ("INSUFFICIENT_EVIDENCE",)
    ):
        failures.append("B2 key mismatch or incomplete halt evidence did not fail closed")
    return DrillMiningAuditCase(
        "b2_witness_schema_timing_key_and_incomplete_evidence_mutations_fail_closed",
        (
            f"hard_refusals={refusals}/{len(probes)} handler_inventory=closed "
            "witness_arity_and_causality=closed sampling=closed fields=exact "
            "route_key=below_threshold halt_coverage=INSUFFICIENT_EVIDENCE_EXCLUDED"
        ),
        tuple(failures),
    )


def _c_difficulty_and_frequency_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    signal = and_legibility_ppm(
        (
            lower_bound_legibility_ppm(10, 10),
            boolean_legibility_ppm(True),
        )
    )
    duration = duration_legibility_ppm(20, 10)
    conflict = aggressive_conflict_ppm(3, 1)
    projection = build_difficulty_projection(
        signal_legibility_ppm=signal,
        duration_legibility_ppm=duration,
        conflict_ppm=conflict,
        reaction_us=1_000_000,
        spread_ticks=None,
        latency_us=None,
        three_level_depth=None,
        venue_count=None,
        hidden_liquidity_relevant=True,
        feature_count=2,
        evidence_class=EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER,
    )
    expected_missing = (
        "spread_hardness_ppm",
        "latency_hardness_ppm",
        "inverse_liquidity_ppm",
        "venue_hardness_ppm",
        "objective_depth_hardness_ppm",
    )
    inspection = projection.inspection_projection()
    components = projection.as_dict()
    applicable = tuple(
        (DifficultyProjectionV1._WEIGHTS[name], components[name])
        for name in DifficultyProjectionV1._WEIGHTS
        if components[name] is not None
    )
    expected_difficulty = round_div_even(
        sum(weight * value for weight, value in applicable),
        sum(weight for weight, _ in applicable),
    )
    if (
        signal != 500_000
        or duration != 1_000_000
        or conflict != 250_000
        or lower_bound_legibility_ppm(POSITIVE_INFINITY_V1, 10) != 1_000_000
        or upper_bound_legibility_ppm(0, 10) != 1_000_000
        or upper_bound_legibility_ppm(10, 10) != 500_000
        or or_legibility_ppm((500_000, 750_000)) != 750_000
        or orient_signal_magnitude_v1(
            -10,
            SignalClauseOrientationV1.NEGATIVE_UPPER_BOUND,
        )
        != 10
        or orient_signal_magnitude_v1(
            -10,
            SignalClauseOrientationV1.ABSOLUTE_MAGNITUDE,
        )
        != 10
        or orient_signal_magnitude_v1(
            -10,
            SignalClauseOrientationV1.DIRECTIONAL,
            direction=CandidateDirectionV1.SELL,
        )
        != 10
    ):
        failures.append("difficulty clause legibility does not follow exact V1 arithmetic")
    if (
        inspection["estimate_state"] != DIFFICULTY_ESTIMATE_STATE_V1
        or tuple(inspection["missing_components"]) != expected_missing
        or projection.applicable_weight_sum != 570_000
        or projection.difficulty_ppm != expected_difficulty
        or len(DifficultyProjectionV1._WEIGHTS)
        != WO33C_DIFFICULTY_COMPONENT_COUNT
    ):
        failures.append("difficulty estimate label, omissions, or weighted mean differs")

    frequency_only = FrequencyReportV1(2, 8)
    explicit = FrequencyReportV1(2, 8, "event")
    rarity = explicit.as_rarity_projection()
    if (
        frequency_only.sample_frequency_ppm != 250_000
        or frequency_only.rarity_ppm is not None
        or explicit.rarity_ppm != 750_000
        or rarity.sample_frequency_ppm != 250_000
        or rarity.rarity_ppm != 750_000
        or rarity.qualification_source_row != "event"
    ):
        failures.append("frequency or explicit-reference rarity reporting differs")
    hostile = (
        lambda: lower_bound_legibility_ppm(9, 10),
        lambda: lower_bound_legibility_ppm("INFINITY", 10),
        lambda: upper_bound_legibility_ppm(11, 10),
        lambda: boolean_legibility_ppm(False),
        lambda: duration_legibility_ppm(9, 10),
        lambda: orient_signal_magnitude_v1(
            10,
            SignalClauseOrientationV1.DIRECTIONAL,
            direction=CandidateDirectionV1.NOT_APPLICABLE,
        ),
        lambda: FrequencyReportV1(0, 0),
        frequency_only.as_rarity_projection,
    )
    refusals = sum(_raises(probe) for probe in hostile)
    if refusals != len(hostile):
        failures.append("missing, unsatisfied, or unreferenced evidence entered scoring")
    return DrillMiningAuditCase(
        "c_difficulty_is_transparent_fixed_point_and_rarity_requires_reference",
        (
            f"components={len(DifficultyProjectionV1._WEIGHTS)}/11 "
            f"applicable_weight={projection.applicable_weight_sum} "
            f"missing={len(projection.missing_components)} "
            f"estimate={projection.estimate_state.value} "
            f"hard_refusals={refusals}/{len(hostile)} rarity_reference=explicit"
        ),
        tuple(failures),
    )


def _c_deterministic_ranking_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    candidates = (
        _sample_candidate(
            source_ordinal=101,
            event_ordinal=101,
            signal_legibility_ppm=950_000,
            qualifying_units=99,
        ),
        _sample_candidate(
            source_ordinal=102,
            event_ordinal=102,
            signal_legibility_ppm=700_000,
            qualifying_units=50,
        ),
        _sample_candidate(
            source_ordinal=103,
            event_ordinal=103,
            signal_legibility_ppm=500_000,
            qualifying_units=0,
        ),
    )
    forward = rank_candidates(candidates)
    reverse = rank_candidates(tuple(reversed(candidates)))
    forward_ids = tuple(item.candidate.candidate_id for item in forward)
    reverse_ids = tuple(item.candidate.candidate_id for item in reverse)
    difficulties = tuple(
        item.candidate.difficulty_projection.difficulty_ppm for item in forward
    )
    rarities = tuple(
        item.candidate.rarity_projection.rarity_ppm for item in forward
    )
    if forward_ids != reverse_ids or difficulties != tuple(sorted(difficulties)):
        failures.append("difficulty ranking changed with input iteration order")
    if not (
        rarities[0] < rarities[-1]
        and tuple(item.ordinal for item in forward) == (1, 2, 3)
        and all(
            item.as_dict()["difficulty_estimate"]["estimate_state"]
            == "UNVALIDATED_ESTIMATE"
            for item in forward
        )
    ):
        failures.append("ranking hid estimate status or allowed rarity to define order")
    if not _raises(lambda: rank_candidates((candidates[0], candidates[0]))):
        failures.append("ranking accepted repeated content-addressed identity")
    return DrillMiningAuditCase(
        "c_ranking_is_permutation_stable_visible_and_not_rarity_optimized",
        (
            f"ranked={len(forward)} order= difficulty,active_start,candidate_id "
            f"difficulty_range={difficulties[0]}..{difficulties[-1]} "
            "rarity_is_report_only=true estimate=UNVALIDATED_ESTIMATE"
        ),
        tuple(failures),
    )


def _c_bounds(active_start_us: int, post_end_us: int) -> CandidateBoundsV1:
    return CandidateBoundsV1(
        source_start_us=0,
        source_end_us=10_000_000,
        warmup_start_us=max(0, active_start_us - 100_000),
        active_start_us=active_start_us,
        active_end_us=active_start_us + 100_001,
        post_end_us=post_end_us,
    )


def _c_semantic_deduplication_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    first = _sample_candidate(
        bounds=_c_bounds(2_000_000, 3_000_000),
        source_ordinal=201,
        event_ordinal=201,
        signal_legibility_ppm=950_000,
    )
    middle = _sample_candidate(
        bounds=_c_bounds(2_100_000, 3_100_000),
        source_ordinal=201,
        event_ordinal=201,
        signal_legibility_ppm=700_000,
    )
    last = _sample_candidate(
        bounds=_c_bounds(2_200_000, 3_200_000),
        source_ordinal=201,
        event_ordinal=201,
        signal_legibility_ppm=500_000,
    )
    first_middle = compare_candidates(first, middle)
    middle_last = compare_candidates(middle, last)
    first_last = compare_candidates(first, last)
    result = deduplicate_candidates((last, middle, first))
    retained_ids = tuple(item.candidate_id for item in result.retained)
    middle_decision = next(
        item for item in result.decisions if item.candidate_id == middle.candidate_id
    )
    if not (
        first_middle.is_duplicate
        and middle_last.is_duplicate
        and not first_last.is_duplicate
        and retained_ids == (first.candidate_id, last.candidate_id)
        and middle_decision.duplicate_of == first.candidate_id
        and result.duplicate_count == 1
    ):
        failures.append("ordered greedy collapse did not resolve the A-B-C chain exactly")

    foreign_ancestry = _sample_candidate(
        bounds=middle.bounds,
        source_ordinal=202,
        event_ordinal=201,
        signal_legibility_ppm=700_000,
    )
    foreign_regime = _sample_candidate(
        bounds=middle.bounds,
        source_ordinal=201,
        event_ordinal=201,
        phase="HALTED",
        signal_legibility_ppm=700_000,
    )
    if (
        compare_candidates(first, foreign_ancestry).is_duplicate
        or compare_candidates(first, foreign_regime).is_duplicate
    ):
        failures.append("ancestry or regime mismatch was treated as a duplicate")

    boundary_values = (
        time_iou_ppm(0, 1_000_000, 0, 800_000),
        time_iou_ppm(0, 1_000_000, 0, 799_999),
        jaccard_ppm(set(range(9)), set(range(10))),
        jaccard_ppm(set(range(17)), set(range(20))),
        jaccard_ppm({"primary"}, {"primary", "supporting"}),
        jaccard_ppm(set(), set()),
        jaccard_ppm(set(), {"x"}),
    )
    if boundary_values != (
        800_000,
        799_999,
        900_000,
        850_000,
        500_000,
        1_000_000,
        0,
    ):
        failures.append("deduplication fixed-point thresholds or empty-set rules differ")
    event_tokens = tuple(
        canonical_event_token_v1(
            "TRADE",
            "BUY" if index % 2 == 0 else "SELL",
            98 + index,
            99,
            104,
        )
        for index in range(6)
    )
    grams = event_five_grams_v1(event_tokens)
    regime = build_regime_signature_v1(
        phase="CONTINUOUS",
        regime_id=None,
        volume_pressure_ppm=749_999,
        liquidity_pressure_ppm=1_250_000,
        spread_ticks=9,
    )
    if (
        canonical_feature_value_v1(-2) != ("INTEGER", "-2")
        or canonical_feature_value_v1(True) != ("FLAG", "true")
        or observable_feature_token_v1("TRADE", "quantity", 10)
        != "TRADE|quantity|INTEGER|10"
        or observable_feature_tokens_v1(
            (
                "TRADE|quantity|INTEGER|10",
                "BOOK_UPDATE|is_locked|FLAG|false",
                "TRADE|quantity|INTEGER|10",
            )
        )
        != (
            "BOOK_UPDATE|is_locked|FLAG|false",
            "TRADE|quantity|INTEGER|10",
        )
        or event_price_relation_v1(None, 99, 101) != "NO_PRICE"
        or event_price_relation_v1(100, None, 101) != "NO_REFERENCE_QUOTE"
        or tuple(
            event_price_relation_v1(price, 99, 101)
            for price in (98, 99, 100, 101, 102)
        )
        != ("BELOW_BID", "AT_BID", "INSIDE", "AT_ASK", "ABOVE_ASK")
        or len(grams) != 2
        or grams != tuple(sorted(grams, key=lambda row: canonical_json_bytes(list(row))))
        or regime.as_dict()
        != {
            "liquidity_band": "NORMAL",
            "phase": "CONTINUOUS",
            "regime_id": "NOT_APPLICABLE",
            "spread_band": "EXTREME",
            "volume_band": "LOW",
        }
        or spread_band_v1(8) != "WIDE"
    ):
        failures.append("canonical feature, event-sequence, or regime construction differs")
    hostile = (
        lambda: time_iou_ppm(0, 0, 0, 1),
        lambda: canonical_feature_value_v1(1.5),
        lambda: observable_feature_token_v1("TRADE", "bad|path", 1),
        lambda: observable_feature_tokens_v1(("TRADE|quantity|INTEGER|01",)),
        lambda: event_price_relation_v1(100, 101, 100),
        lambda: event_five_grams_v1(()),
        lambda: build_regime_signature_v1(
            phase="LUNCH",
            regime_id="BALANCED",
            volume_pressure_ppm=1_000_000,
            liquidity_pressure_ppm=1_000_000,
            spread_ticks=2,
        ),
    )
    refusals = sum(_raises(probe) for probe in hostile)
    if refusals != len(hostile):
        failures.append("a malformed canonical deduplication input was accepted")
    return DrillMiningAuditCase(
        "c_semantic_deduplication_is_threshold_exact_and_one_pass_greedy",
        (
            "thresholds=iou800000,features900000,fivegram850000,objective500000 "
            f"chain=A-retain,B-duplicate-of-A,C-retain duplicates={result.duplicate_count} "
            f"ancestry=exact regime=exact canonical_input_refusals={refusals}/"
            f"{len(hostile)} scenario_name=unused"
        ),
        tuple(failures),
    )


def _c_selection_pool() -> tuple[LessonCandidateV1, ...]:
    candidates: list[LessonCandidateV1] = []
    ordinal = 300
    for _index in range(20):
        ordinal += 1
        candidates.append(
            _sample_candidate(
                source_row="quiet",
                source_ordinal=ordinal,
                event_ordinal=ordinal,
                detector_id="STRONG_QUEUE_IMBALANCE",
                phase="CONTINUOUS",
                signal_legibility_ppm=1_000_000,
                duration_legibility_ppm=1_000_000,
                conflict_ppm=0,
            )
        )
    event_rows = (
        ("STRONG_QUEUE_IMBALANCE", "PREOPEN", SourceWindowOutcomeV1.CONTINUATION),
        ("BID_ABSORPTION", "OPENING_AUCTION", SourceWindowOutcomeV1.REVERSAL),
        ("FAILED_BREAKOUT", "CONTINUOUS", SourceWindowOutcomeV1.STASIS),
        ("AGGRESSIVE_FLOW_BURST", "HALTED", SourceWindowOutcomeV1.CONTINUATION),
        (
            "LATENCY_SENSITIVE_OPPORTUNITY",
            "REOPENING_AUCTION",
            SourceWindowOutcomeV1.REVERSAL,
        ),
    )
    for detector_id, phase, outcome in event_rows:
        ordinal += 1
        candidates.append(
            _sample_candidate(
                source_row="event",
                source_ordinal=ordinal,
                event_ordinal=ordinal,
                detector_id=detector_id,
                phase=phase,
                source_window_outcome=outcome,
                signal_legibility_ppm=500_000,
            )
        )
    for source, detector_ids in (
        (
            "hidden",
            ("HIDDEN_RESERVE_REFRESH", "ASK_ABSORPTION", "LIQUIDITY_VACUUM"),
        ),
        (
            "fragmented",
            (
                "MULTI_VENUE_FRAGMENTATION",
                "ROUTING_DILEMMA",
                "CANCEL_FILL_RACE",
            ),
        ),
    ):
        for index, detector_id in enumerate(detector_ids):
            ordinal += 1
            candidates.append(
                _sample_candidate(
                    source_row=source,
                    source_ordinal=ordinal,
                    event_ordinal=ordinal,
                    detector_id=detector_id,
                    phase=("CLOSING_AUCTION", "POSTCLOSE", "CONTINUOUS")[index],
                    source_window_outcome=(
                        SourceWindowOutcomeV1.CONTINUATION,
                        SourceWindowOutcomeV1.REVERSAL,
                        SourceWindowOutcomeV1.STASIS,
                    )[index],
                    signal_legibility_ppm=500_000,
                )
            )
    for index, detector_id in enumerate(
        ("FAILED_BREAKOUT", "HALT_REOPENING", "MEAN_REVERSION_TRANSITION")
    ):
        ordinal += 1
        candidates.append(
            _sample_candidate(
                source_row="historical",
                source_ordinal=ordinal,
                event_ordinal=ordinal,
                source_kind=SourceKindV1.RECONSTRUCTION,
                evidence_class=EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL,
                detector_id=detector_id,
                phase=("PREOPEN", "HALTED", "POSTCLOSE")[index],
                source_window_outcome=(
                    SourceWindowOutcomeV1.REVERSAL,
                    SourceWindowOutcomeV1.STASIS,
                    SourceWindowOutcomeV1.CONTINUATION,
                )[index],
                signal_legibility_ppm=500_000,
            )
        )
    return tuple(candidates)


def _c_diversity_selection_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    pool = _c_selection_pool()
    selected = select_technical_review_candidates(pool)
    repeated = select_technical_review_candidates(tuple(reversed(pool)))
    selected_ids = tuple(item.candidate_id for item in selected.selected)
    repeated_ids = tuple(item.candidate_id for item in repeated.selected)
    baseline = tuple(item.candidate for item in rank_candidates(pool)[:20])
    selected_coverage = dict(coverage_counts_v1(selected.selected))
    baseline_coverage = dict(coverage_counts_v1(baseline))
    reserved = {
        item.source: item.selected_in_reserved_step
        for item in selected.reserved_shortfalls
    }
    if (
        selected_ids != repeated_ids
        or selected.selected_count != REVIEW_TARGET_COUNT_V1
        or selected.selected_count != WO33C_REVIEW_TARGET_COUNT
        or selected.shortfall_count != 0
        or not selected.event_five_gate_passed
        or reserved != dict(RESERVED_COUNTS_V1)
    ):
        failures.append("stratified review selection is not exact or permutation stable")
    if not (
        selected_coverage["source"] == 5
        and baseline_coverage["source"] == 1
        and sum(selected_coverage.values()) > sum(baseline_coverage.values())
    ):
        failures.append("declared-metric diversity did not improve over difficulty order")
    if (
        len(DIVERSITY_DIMENSIONS_V1) != WO33C_DIVERSITY_DIMENSION_COUNT
        or sum(DIVERSITY_WEIGHTS_PPM_V1.values()) != 1_000_000
        or difficulty_band_v1(249_999) != "[0,250000)"
        or difficulty_band_v1(250_000) != "[250000,500000)"
        or difficulty_band_v1(1_000_000) != "[750000,1000000]"
        or not _raises(
            lambda: DIVERSITY_WEIGHTS_PPM_V1.__setitem__("source", 1)
        )
        or source_window_outcome_v1(CandidateDirectionV1.BUY, 200, 202)
        is not SourceWindowOutcomeV1.CONTINUATION
        or source_window_outcome_v1(CandidateDirectionV1.SELL, 202, 204)
        is not SourceWindowOutcomeV1.REVERSAL
        or source_window_outcome_v1(CandidateDirectionV1.BUY, 200, 201)
        is not SourceWindowOutcomeV1.STASIS
        or source_window_outcome_v1(CandidateDirectionV1.BUY, None, 202)
        is not SourceWindowOutcomeV1.NOT_OBSERVABLE
        or source_window_outcome_v1(
            CandidateDirectionV1.NOT_APPLICABLE,
            None,
            None,
        )
        is not SourceWindowOutcomeV1.NOT_APPLICABLE
    ):
        failures.append("diversity dimensions, weights, or bands are not frozen")
    for index, decision in enumerate(selected.decisions):
        candidate = selected.selected[index]
        score, novelty = marginal_diversity_v1(
            candidate,
            selected.selected[:index],
        )
        if score != decision.marginal_score_ppm or novelty != decision.novelty_ppm:
            failures.append("a recorded marginal diversity score is not reproducible")
            break
    review_bytes = canonical_json_bytes(selected.technical_review_projection())
    if any(
        outcome.value.encode("ascii") in review_bytes
        for outcome in (
            SourceWindowOutcomeV1.CONTINUATION,
            SourceWindowOutcomeV1.REVERSAL,
            SourceWindowOutcomeV1.STASIS,
        )
    ):
        failures.append("technical-review selection projection leaked future outcome")
    return DrillMiningAuditCase(
        "c_preregistered_greedy_selection_is_deterministic_and_more_diverse",
        (
            f"pool={len(pool)} selected={selected.selected_count}/20 "
            f"dimensions={len(DIVERSITY_DIMENSIONS_V1)} weight_sum=1000000 "
            f"source_coverage={baseline_coverage['source']}->{selected_coverage['source']} "
            f"coverage_sum={sum(baseline_coverage.values())}->"
            f"{sum(selected_coverage.values())} event_five=PASS outcome=WITHHELD"
        ),
        tuple(failures),
    )


def _c_shortfall_and_hostile_quota_case() -> DrillMiningAuditCase:
    failures: list[str] = []
    retained_event = _sample_candidate(
        bounds=_c_bounds(4_000_000, 5_000_000),
        source_row="event",
        source_ordinal=501,
        event_ordinal=501,
        signal_legibility_ppm=900_000,
    )
    event_duplicate = _sample_candidate(
        bounds=_c_bounds(4_050_000, 5_050_000),
        source_row="event",
        source_ordinal=501,
        event_ordinal=501,
        signal_legibility_ppm=500_000,
    )
    retained_quiet = _sample_candidate(
        source_row="quiet",
        source_ordinal=502,
        event_ordinal=502,
        detector_id="FAILED_BREAKOUT",
    )
    pool = (event_duplicate, retained_quiet, retained_event)
    normal = select_technical_review_candidates(pool)
    pressured = select_technical_review_candidates(pool, target_count=40)
    normal_payload = normal.as_dict()
    event_shortfall = next(
        item for item in normal.reserved_shortfalls if item.source == "event"
    )
    if (
        normal.selected_count != 2
        or normal.shortfall_count != 18
        or normal.deduplication.duplicate_count != 1
        or event_duplicate.candidate_id
        in {item.candidate_id for item in normal.selected}
        or event_shortfall.shortfall_count != 4
        or normal.event_five_gate_passed
        or normal_payload["duplicates_admitted"] is not False
        or normal_payload["thresholds_weakened"] is not False
    ):
        failures.append("normal quota concealed duplicate, event, or pool shortfall")
    if (
        pressured.selected_count != 2
        or pressured.shortfall_count != 38
        or pressured.deduplication.duplicate_count != 1
        or pressured.deduplication.as_dict()["thresholds_ppm"]
        != normal.deduplication.as_dict()["thresholds_ppm"]
    ):
        failures.append("larger quota altered validity or semantic duplicate thresholds")

    hostile_source = _sample_candidate(
        source_row="quiet-alias",
        source_ordinal=503,
        event_ordinal=503,
    )
    probes = (
        lambda: select_technical_review_candidates(
            (retained_event, retained_event)
        ),
        lambda: select_technical_review_candidates((hostile_source,)),
        lambda: _sample_candidate(
            source_row="quiet",
            source_ordinal=504,
            event_ordinal=504,
            phase="LUNCH",
        ),
    )
    refusals = sum(_raises(probe) for probe in probes)
    if refusals != len(probes):
        failures.append("identity, source-row, or phase aliases entered selection")
    return DrillMiningAuditCase(
        "c_quota_pressure_reports_shortfall_without_weakening_or_duplicates",
        (
            f"selected={normal.selected_count}/20 shortfall={normal.shortfall_count} "
            f"pressured={pressured.selected_count}/40 "
            f"pressured_shortfall={pressured.shortfall_count} duplicates=1_excluded "
            f"event_reserved_shortfall={event_shortfall.shortfall_count} "
            f"hostile_refusals={refusals}/{len(probes)} thresholds_unchanged=true"
        ),
        tuple(failures),
    )


def _b2_synthetic_reports():
    from kirby2.mining.runtime import B2_DETECTOR_IDS_V1, MiningDetectorRuntimeV1

    runtime = MiningDetectorRuntimeV1()
    source_identity = SourceIdentityV1(
        SourceKindV1.RUN,
        "b2-synthetic-source",
        _digest("b2-synthetic-source"),
    )
    ancestry = SourceAncestryV1(
        source_identity.kind,
        source_identity.source_id,
        source_identity.source_sha256,
    )
    opportunities = {
        detector_id: _b2_qualifying_opportunity(runtime, detector_id)
        for detector_id in B2_DETECTOR_IDS_V1
    }
    reports = {
        detector_id: runtime.run(
            detector_id,
            _inventory_for(
                detector_id,
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
                source_identity,
            ),
            ancestry,
            (opportunities[detector_id],),
        )
        for detector_id in B2_DETECTOR_IDS_V1
    }
    return runtime, opportunities, reports, ancestry


def _b2_qualifying_opportunity(runtime, detector_id: str):
    from kirby2.mining.runtime import (
        DetectorMeasurementV1,
        DetectorOpportunityV1,
        MiningEventReferenceV1,
    )

    checkpoint_sha256 = _digest("b2-latency-checkpoint")
    specifications: dict[str, dict[str, object]] = {
        "AUCTION_IMBALANCE_CHANGE": {
            "direction": CandidateDirectionV1.BUY,
            "side": CandidateSideV1.BUY,
            "venue": "CONSOLIDATED",
            "price": "NOT_APPLICABLE",
            "witness_kind": "AUCTION_PUBLICATION_PAIR",
            "witness_ids": ("AUCTION-PUBLICATION-1", "AUCTION-PUBLICATION-2"),
            "active_start_us": 30_000_000,
            "activation_us": 60_000_000,
            "measurements": {
                "new_imbalance_shares": 5_000,
                "old_imbalance_shares": -5_000,
                "publication_interval_us": 30_000_000,
            },
            "events": (
                ("AUCTION-PUBLICATION-1", 30_000_000, 1),
                ("AUCTION-PUBLICATION-2", 60_000_000, 2),
            ),
        },
        "CANCEL_FILL_RACE": {
            "direction": CandidateDirectionV1.NOT_APPLICABLE,
            "side": CandidateSideV1.BUY,
            "venue": "XNAS",
            "price": 100,
            "witness_kind": "CANCEL_FILL_TUPLE",
            "witness_ids": (
                "ORDER-0001",
                "CANCEL-COMMAND-0001",
                "CONTRA-ARRIVAL-0001",
            ),
            "active_start_us": 59_999_000,
            "activation_us": 60_000_000,
            "measurements": {
                "baseline_cancel_arrival_us": 10_500,
                "baseline_cancel_latency_us": 1_500,
                "baseline_opposing_fill_arrival_us": 10_000,
                "checkpoint_information_identical": True,
                "fast_cancel_latency_us": 500,
                "fast_cancelled_quantity": 60,
                "fast_effective_cancel_source_sequence": 2,
                "fast_effective_cancel_us": 9_500,
                "fast_filled_quantity": 40,
                "fast_opposing_fill_arrival_source_sequence": 1,
                "fast_opposing_fill_arrival_us": 10_000,
                "original_quantity": 100,
                "slow_cancel_latency_us": 2_500,
                "slow_cancelled_quantity": 0,
                "slow_effective_cancel_source_sequence": 2,
                "slow_effective_cancel_us": 11_500,
                "slow_filled_quantity": 100,
                "slow_opposing_fill_arrival_source_sequence": 1,
                "slow_opposing_fill_arrival_us": 10_000,
            },
            "events": (
                ("ORDER-0001", 59_999_000, 1),
                ("CANCEL-COMMAND-0001", 59_999_500, 2),
                ("CONTRA-ARRIVAL-0001", 60_000_000, 3),
            ),
        },
        "DISTRESSED_LIQUIDATION": {
            "direction": CandidateDirectionV1.SELL,
            "side": CandidateSideV1.SELL,
            "venue": "CONSOLIDATED",
            "price": "NOT_APPLICABLE",
            "witness_kind": "NOT_APPLICABLE",
            "witness_ids": (),
            "active_start_us": 55_000_000,
            "activation_us": 60_000_000,
            "measurements": {
                "authoritative_participant_identity": True,
                "distressed_buy_quantity": 0,
                "distressed_sell_quantity": 5_000,
                "elapsed_us": 5_000_000,
                "first_mid_x2": 200,
                "last_mid_x2": 198,
            },
            "events": (
                ("DISTRESSED-FLOW-START", 55_000_000, 1),
                ("DISTRESSED-FLOW-END", 60_000_000, 2),
            ),
        },
        "HALT_REOPENING": {
            "direction": CandidateDirectionV1.BUY,
            "side": CandidateSideV1.NOT_APPLICABLE,
            "venue": "CONSOLIDATED",
            "price": "NOT_APPLICABLE",
            "witness_kind": "HALT_REOPEN_PAIR",
            "witness_ids": ("HALT-EVENT", "REOPEN-EVENT"),
            "active_start_us": 50_000_000,
            "activation_us": 60_000_000,
            "measurements": {
                "first_post_reopen_trade_ticks": 103,
                "halt_time_us": 50_000_000,
                "last_pre_halt_trade_ticks": 100,
                "post_spread_durations_us": (2_500_000, 2_500_000),
                "post_spread_ticks": (2, 4),
                "post_window_coverage_us": 5_000_000,
                "pre_spread_durations_us": (2_500_000, 2_500_000),
                "pre_spread_ticks": (1, 2),
                "pre_window_coverage_us": 5_000_000,
                "reopen_time_us": 55_000_000,
            },
            "events": (
                ("HALT-EVENT", 50_000_000, 1),
                ("REOPEN-EVENT", 55_000_000, 2),
            ),
        },
        "LATENCY_SENSITIVE_OPPORTUNITY": {
            "direction": CandidateDirectionV1.BUY,
            "side": CandidateSideV1.BUY,
            "venue": "XNAS",
            "price": "NOT_APPLICABLE",
            "witness_kind": "LATENCY_ACTION",
            "witness_ids": (checkpoint_sha256, "ACTION-0001", "XNAS", "BUY"),
            "active_start_us": 60_000_000,
            "activation_us": 60_000_000,
            "measurements": {
                "action_identical": True,
                "checkpoint_information_identical": True,
                "fast_fee_adjusted_average_cost_milliticks_per_share": 100_000,
                "fast_filled_quantity": 100,
                "fast_latency_us": 250,
                "objective_shares": 100,
                "slow_fee_adjusted_average_cost_milliticks_per_share": 100_000,
                "slow_filled_quantity": 75,
                "slow_latency_us": 2_500,
            },
            "events": (("ACTION-0001", 60_000_000, 1),),
        },
        "MULTI_VENUE_FRAGMENTATION": {
            "direction": CandidateDirectionV1.NOT_APPLICABLE,
            "side": CandidateSideV1.BUY,
            "venue": "NOT_APPLICABLE",
            "price": "NOT_APPLICABLE",
            "witness_kind": "VENUE_PAIR",
            "witness_ids": ("ARCX", "XNAS"),
            "active_start_us": 60_000_000,
            "activation_us": 60_005_000,
            "measurements": {
                "persistence_us": 5_000,
                "venue_best_ask_ticks": (101, 103),
                "venue_best_bid_ticks": (100, 102),
                "venue_executable_quantities": (100, 100),
            },
            "events": (
                ("FRAGMENTATION-START", 60_000_000, 1),
                ("FRAGMENTATION-END", 60_005_000, 2),
            ),
        },
        "ROUTING_DILEMMA": {
            "direction": CandidateDirectionV1.BUY,
            "side": CandidateSideV1.BUY,
            "venue": "NOT_APPLICABLE",
            "price": "NOT_APPLICABLE",
            "witness_kind": "ROUTE_PAIR",
            "witness_ids": ("ROUTE-A", "ROUTE-B"),
            "active_start_us": 60_000_000,
            "activation_us": 60_000_000,
            "measurements": {
                "route_a_executable_quantity": 100,
                "route_a_expected_receipt_time_us": 2_000,
                "route_a_fee_adjusted_cost_milliticks_per_share": 9_000,
                "route_b_executable_quantity": 350,
                "route_b_expected_receipt_time_us": 1_000,
                "route_b_fee_adjusted_cost_milliticks_per_share": 10_000,
            },
            "events": (
                ("ROUTE-A-EVIDENCE", 60_000_000, 1),
                ("ROUTE-B-EVIDENCE", 60_000_000, 2),
            ),
        },
    }
    specification = specifications[detector_id]
    row = runtime.threshold_manifest.detector(detector_id)
    measurements = specification["measurements"]
    events = specification["events"]
    if not isinstance(measurements, dict) or not isinstance(events, tuple):
        raise TypeError("B2 synthetic fixture specification is malformed")
    return DetectorOpportunityV1(
        detector_id=detector_id,
        opportunity_id=f"b2-{detector_id.lower().replace('_', '-')}-qualifying",
        sampling_unit=str(row["sampling_unit"]),
        source_start_us=0,
        source_end_us=100_000_000,
        active_start_us=int(specification["active_start_us"]),
        activation_us=int(specification["activation_us"]),
        direction=specification["direction"],
        side=specification["side"],
        venue=str(specification["venue"]),
        price=specification["price"],
        witness_kind=str(specification["witness_kind"]),
        witness_ids=specification["witness_ids"],
        measurements=tuple(
            DetectorMeasurementV1(name, value)
            for name, value in reversed(tuple(measurements.items()))
        ),
        contributing_events=tuple(
            MiningEventReferenceV1(event_id, timestamp_us, source_sequence)
            for event_id, timestamp_us, source_sequence in reversed(events)
        ),
    )


def _replace_b2_measurements(
    opportunity,
    *,
    opportunity_id: str,
    direction: CandidateDirectionV1 | None = None,
    side: CandidateSideV1 | None = None,
    **updates: object,
):
    from kirby2.mining.runtime import DetectorMeasurementV1

    values = dict(opportunity.measurement_map)
    unknown = set(updates).difference(values)
    if unknown:
        raise ValueError(f"unknown B2 fixture measurement updates: {sorted(unknown)}")
    values.update(updates)
    return replace(
        opportunity,
        opportunity_id=opportunity_id,
        direction=opportunity.direction if direction is None else direction,
        side=opportunity.side if side is None else side,
        measurements=tuple(
            DetectorMeasurementV1(name, value) for name, value in values.items()
        ),
    )


def _derived_measurement_map(report) -> dict[str, object]:
    if not report.findings:
        return {}
    return {
        measurement.name: measurement.value
        for measurement in report.findings[0].derived_measurements
    }


def _d_source_lineage_and_prefix_parity_case(
    candidate,
    recorded,
    extracted,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    source_before = recorded.semantic_sha256
    record_payload = extracted.source_record.as_dict()
    required_fields = {
        "checkpoint_reference",
        "detector",
        "hidden_state_reveal_policy",
        "historical_provenance",
        "observable_feed_policy",
        "source_run_reference",
        "source_time_bounds",
    }
    extracted_ids = tuple(
        event.client_event_id for event in extracted.envelope.observable_feed
    )
    expected_ids = tuple(
        event.client_event_id
        for event in recorded.observable_feed
        if candidate.bounds.warmup_start_us
        <= event.client_time_us
        < candidate.bounds.post_end_us
    )
    if set(record_payload) != required_fields:
        failures.append("built source record does not preserve exactly seven lineage fields")
    if len(record_payload) != WO33D_SOURCE_LINEAGE_FIELD_COUNT:
        failures.append("built source lineage field count differs from policy")
    if (
        extracted.envelope.source_observable_prefix_sha256
        != extracted.envelope.extracted_observable_prefix_sha256
    ):
        failures.append("source and extracted client-feed prefixes differ")
    if extracted_ids != expected_ids:
        failures.append("extracted client feed differs from the exact source slice")
    if (
        extracted.envelope.authoritative_event_prefix_sha256
        != candidate.source_ancestry.event_prefix_sha256
    ):
        failures.append("source envelope lost the authoritative event-prefix digest")
    if extracted.envelope.capability_labels != tuple(
        row.capability for row in candidate.capability_record.records
    ):
        failures.append("source envelope lost capability labels")
    if recorded.semantic_sha256 != source_before:
        failures.append("source extraction mutated the authoritative replay material")
    return DrillMiningAuditCase(
        "d_seven_field_lineage_and_exact_recorded_feed_prefix_parity",
        (
            f"lineage_fields={len(record_payload)}/7 "
            f"feed_events={len(extracted_ids)} "
            "source_prefix_equals_extracted_prefix=true "
            "rng_schedule_capabilities_parent_linkage=sealed"
        ),
        tuple(failures),
    )


def _d_warmup_and_information_fairness_case(
    candidate,
    recorded,
    lesson,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    initial = lesson.assessment_at(0).as_dict()
    activation = lesson.assessment_at(lesson.activation_elapsed_us).as_dict()
    initial_feed = initial["observable_feed"]
    activation_feed = activation["observable_feed"]
    if (
        not isinstance(initial_feed, list)
        or len(initial_feed) != 1
        or initial_feed[0]["kind"] != "CLIENT_STATE_SNAPSHOT"
    ):
        failures.append("warmup does not begin with the exact recorded client snapshot")
    activation_ids = {
        event["client_event_id"]
        for event in activation_feed
        if isinstance(event, dict)
    }
    contributing_ids = set(
        candidate.observable_feature_summary.contributing_source_event_ids
    )
    if not contributing_ids.issubset(activation_ids):
        failures.append("classification opened before all contributing evidence arrived")
    if initial["classification_status"] != "NOT_YET_OPEN":
        failures.append("classification opened before the recorded activation cut")
    if activation["classification_status"] != "OPEN":
        failures.append("classification did not open at the exact activation cut")

    snapshot_id = next(
        event.client_event_id
        for event in recorded.observable_feed
        if event.kind == "CLIENT_STATE_SNAPSHOT"
    )
    final_evidence_id = candidate.observable_feature_summary.contributing_source_event_ids[-1]
    no_snapshot = replace(
        recorded,
        observable_feed=tuple(
            event
            for event in recorded.observable_feed
            if event.client_event_id != snapshot_id
        ),
    )
    missing_evidence = replace(
        recorded,
        observable_feed=tuple(
            event
            for event in recorded.observable_feed
            if event.client_event_id != final_evidence_id
        ),
    )
    late_evidence = replace(
        recorded,
        observable_feed=tuple(
            replace(event, client_time_us=candidate.bounds.activation_us + 1)
            if event.client_event_id == final_evidence_id
            else event
            for event in recorded.observable_feed
        ),
    )
    late_snapshot = replace(
        recorded,
        observable_feed=tuple(
            replace(event, client_time_us=event.client_time_us + 1)
            if event.client_event_id == snapshot_id
            else event
            for event in recorded.observable_feed
        ),
    )
    probes = (
        lambda: extract_observable_lesson_source_v1(candidate, no_snapshot),
        lambda: extract_observable_lesson_source_v1(candidate, missing_evidence),
        lambda: extract_observable_lesson_source_v1(candidate, late_evidence),
        lambda: extract_observable_lesson_source_v1(candidate, late_snapshot),
    )
    refusals = sum(_raises(probe) for probe in probes)
    if refusals != len(probes):
        failures.append("clipped warmup or unavailable/late client evidence was accepted")
    return DrillMiningAuditCase(
        "d_warmup_snapshot_and_client_delivery_cut_are_information_fair",
        (
            f"warmup_start_us={candidate.bounds.warmup_start_us} "
            f"activation_us={candidate.bounds.activation_us} "
            f"contributing_events_visible={len(contributing_ids)} "
            f"hostile_refusals={refusals}/{len(probes)}"
        ),
        tuple(failures),
    )


def _d_blind_boundary_and_reveal_authorization_case(
    candidate,
    lesson,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    assessment = lesson.assessment_at(lesson.activation_elapsed_us)
    payload = assessment.as_dict()
    expected_fields = {
        "assessment_policy_id",
        "classification_status",
        "lesson_digest",
        "lesson_id",
        "objective_kind",
        "objective_prompt",
        "observable_feed",
        "observable_feed_prefix_sha256",
        "playback_elapsed_us",
        "record_kind",
        "schema_version",
        "source_record_sha256",
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    selection_tie = str(lesson.reveal_payload.selection_reason["tie_digest"])
    protected_fragments = (
        candidate.detector.detector_id,
        candidate.source_window_outcome.value,
        selection_tie,
        "difficulty_ppm",
        "difficulty_projection",
        "post_end_us",
        "post_event_boundary_us",
        "WO33D-RNG-SECRET",
        "WO33D-HIDDEN-SCHEDULE",
    )
    if set(payload) != expected_fields or len(payload) != WO33D_ASSESSMENT_FIELD_COUNT:
        failures.append("blind assessment wire surface differs from the closed allow-list")
    if any(fragment in serialized for fragment in protected_fragments):
        failures.append("blind assessment leaked answer, selection, future, or hidden state")
    if set(MinedLessonAssessmentV1.__dataclass_fields__) - {
        key for key in expected_fields if key != "record_kind"
    }:
        failures.append("blind assessment type contains a non-wire reveal field")

    precomplete_guard = _raises(
        lambda: lesson.authorize_reveal(
            lesson.assessment_at(lesson.activation_elapsed_us)
        )
    )
    unbound_guard = _raises(lambda: lesson.reveal(None))
    completed = lesson.assessment_at(lesson.duration_us)
    grant = lesson.authorize_reveal(completed)
    revealed = lesson.reveal(grant)
    forged_grant = replace(
        grant,
        reveal_payload_sha256=_digest("wo33d-forged-reveal"),
    )
    forged_guard = _raises(lambda: lesson.reveal(forged_grant))
    nested_hidden_guard = _raises(
        lambda: RecordedClientFeedEventV1(
            99,
            1,
            "hostile-hidden-feed",
            "CLIENT_BOOK_UPDATE",
            {"nested": {"hidden_schedule": []}},
        )
    )
    if not all(
        (precomplete_guard, unbound_guard, forged_guard, nested_hidden_guard)
    ):
        failures.append("assessment/reveal boundary accepted an unauthorized path")
    if (
        revealed.detector["id"] != candidate.detector.detector_id
        or revealed.source_window_outcome != candidate.source_window_outcome.value
        or revealed.post_event_boundary_us != candidate.bounds.post_end_us
        or "difficulty_ppm" not in revealed.difficulty_projection
        or revealed.selection_reason["candidate_id"] != candidate.candidate_id
    ):
        failures.append("authorized reveal omitted the separately withheld answer key")
    return DrillMiningAuditCase(
        "d_closed_blind_surface_and_completed_assessment_reveal_grant",
        (
            f"assessment_fields={len(payload)}/12 protected_absent=true "
            f"precomplete_guarded={str(precomplete_guard).lower()} "
            f"forged_guarded={str(forged_guard).lower()} "
            f"nested_hidden_guarded={str(nested_hidden_guard).lower()}"
        ),
        tuple(failures),
    )


def _d_deterministic_build_and_replay_case(
    candidate,
    recorded,
    lesson,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    source_before = recorded.semantic_sha256
    repeated_candidate, repeated_recorded, repeated_extracted, repeated_lesson = (
        _d_playable_lesson_fixture()
    )
    cuts = (
        0,
        lesson.activation_elapsed_us - 1,
        lesson.activation_elapsed_us,
        lesson.duration_us - 1,
        lesson.duration_us,
    )
    first_replay = assessment_replay_sha256_v1(lesson, cuts)
    second_replay = assessment_replay_sha256_v1(repeated_lesson, cuts)
    if candidate.candidate_id != repeated_candidate.candidate_id:
        failures.append("identical source construction changed candidate identity")
    if recorded.semantic_sha256 != repeated_recorded.semantic_sha256:
        failures.append("identical recorded source changed semantic identity")
    if lesson.canonical_bytes() != repeated_lesson.canonical_bytes():
        failures.append("identical lesson inputs changed canonical lesson bytes")
    if lesson.lesson_digest != repeated_lesson.lesson_digest:
        failures.append("identical lesson inputs changed lesson identity")
    if first_replay != second_replay:
        failures.append("identical assessment playback cuts changed replay identity")
    if (
        lesson.source.envelope.extracted_observable_prefix_sha256
        != repeated_extracted.envelope.extracted_observable_prefix_sha256
    ):
        failures.append("repeated extraction changed exact client-feed bytes")
    permutation_guard = _raises(
        lambda: replace(
            recorded,
            observable_feed=tuple(reversed(recorded.observable_feed)),
        )
    )
    if not permutation_guard:
        failures.append("noncanonical source event order was accepted")
    if recorded.semantic_sha256 != source_before:
        failures.append("determinism replay mutated authoritative source material")
    return DrillMiningAuditCase(
        "d_same_source_candidate_and_cuts_replay_byte_identically",
        (
            f"lesson_sha256={lesson.lesson_digest} "
            f"assessment_replay_sha256={first_replay} "
            f"cuts={len(cuts)} permutation_guarded={str(permutation_guard).lower()}"
        ),
        tuple(failures),
    )


def _d_parent_linked_source_authoritative_overlay_case(
    lesson,
) -> DrillMiningAuditCase:
    failures: list[str] = []
    source_before = lesson.source.envelope.sha256
    mutable_payload = {"classification": "QUEUE_PRESSURE"}
    first_action = MinedLessonPlayerActionV1(
        1,
        lesson.activation_elapsed_us,
        "SUBMIT_CLASSIFICATION",
        mutable_payload,
    )
    mutable_payload["classification"] = "MUTATED_AFTER_OWNERSHIP_TRANSFER"
    second_action = MinedLessonPlayerActionV1(
        2,
        lesson.activation_elapsed_us + 1,
        "COUNTERFACTUAL_ORDER",
        {"order_type": "MARKET", "quantity": 25, "side": "BUY"},
    )
    overlay = build_player_overlay_v1(lesson, (first_action, second_action))
    repeated = build_player_overlay_v1(lesson, (first_action, second_action))
    replayed = replay_player_overlay_v1(lesson, overlay)
    if overlay.sha256 != repeated.sha256 or overlay.sha256 != replayed.sha256:
        failures.append("identical player overlay did not replay deterministically")
    if first_action.payload["classification"] != "QUEUE_PRESSURE":
        failures.append("player overlay retained caller-owned mutable action data")
    if (
        overlay.parent_lesson_digest != lesson.lesson_digest
        or overlay.parent_source_record_sha256 != lesson.source.source_record.sha256
        or overlay.parent_source_envelope_sha256 != source_before
        or not overlay.source_authoritative
        or overlay.provenance != "PLAYER_ACTION_OVERLAY_NOT_SOURCE_HISTORY"
    ):
        failures.append("player overlay lost parent linkage or source-authority label")
    if lesson.source.envelope.sha256 != source_before:
        failures.append("player overlay mutated mined source history")
    wrong_sequence = MinedLessonPlayerActionV1(
        2,
        lesson.activation_elapsed_us,
        "SUBMIT_CLASSIFICATION",
        {},
    )
    late_action = MinedLessonPlayerActionV1(
        1,
        lesson.duration_us,
        "COUNTERFACTUAL_ORDER",
        {},
    )
    forged_parent = replace(
        overlay,
        parent_source_record_sha256=_digest("wo33d-foreign-parent"),
    )
    probes = (
        lambda: build_player_overlay_v1(lesson, (wrong_sequence,)),
        lambda: build_player_overlay_v1(lesson, (late_action,)),
        lambda: replay_player_overlay_v1(lesson, forged_parent),
    )
    refusals = sum(_raises(probe) for probe in probes)
    if refusals != len(probes):
        failures.append("invalid timing, sequence, or parent overlay was accepted")
    return DrillMiningAuditCase(
        "d_player_actions_are_deterministic_parent_linked_overlays_not_history",
        (
            f"overlay_sha256={overlay.sha256} actions={len(overlay.actions)} "
            f"source_authoritative=true hostile_refusals={refusals}/{len(probes)}"
        ),
        tuple(failures),
    )


def _d_playable_lesson_fixture():
    candidate = _sample_candidate()
    ancestry = candidate.source_ancestry
    checkpoint = candidate.checkpoint
    if checkpoint is None:
        raise AssertionError("WO33-D fixture requires a checkpoint")
    contributing = candidate.observable_feature_summary.contributing_source_event_ids
    recorded = RecordedLessonSourceV1(
        source_run_reference=MinedSourceRunReferenceV1(
            source_kind=ancestry.source_kind.value,
            source_id=ancestry.source_id,
            source_sha256=ancestry.source_sha256,
            source_replay_sha256=_digest("wo33d-authoritative-source-replay"),
        ),
        source_start_us=candidate.bounds.source_start_us,
        source_end_us=candidate.bounds.source_end_us,
        checkpoint_reference=MinedCheckpointReferenceV1(
            checkpoint.checkpoint_id,
            checkpoint.checkpoint_sha256,
        ),
        source_ancestry_sha256=ancestry.sha256,
        parent_source_ancestry_sha256=ancestry.parent_source_ancestry_sha256,
        authoritative_event_prefix_sha256=ancestry.event_prefix_sha256,
        observable_feed=(
            RecordedClientFeedEventV1(
                1,
                500_000,
                "client-pre-window-0001",
                "CLIENT_BOOK_UPDATE",
                {"best_ask_ticks": 101, "best_bid_ticks": 100},
            ),
            RecordedClientFeedEventV1(
                2,
                candidate.bounds.warmup_start_us,
                "client-warmup-snapshot-0001",
                "CLIENT_STATE_SNAPSHOT",
                {
                    "asks": [{"price_ticks": 101, "quantity": 400}],
                    "bids": [{"price_ticks": 100, "quantity": 400}],
                },
            ),
            RecordedClientFeedEventV1(
                3,
                candidate.bounds.active_start_us,
                contributing[0],
                "CLIENT_BOOK_UPDATE",
                {"best_ask_size": 400, "best_bid_size": 1_600},
            ),
            RecordedClientFeedEventV1(
                4,
                candidate.bounds.activation_us,
                contributing[1],
                "CLIENT_BOOK_UPDATE",
                {"best_ask_size": 350, "best_bid_size": 1_700},
            ),
            RecordedClientFeedEventV1(
                5,
                4_500_000,
                "client-response-window-0001",
                "CLIENT_TRADE_PRINT",
                {"price_ticks": 101, "quantity": 50, "side": "BUY"},
            ),
            RecordedClientFeedEventV1(
                6,
                5_500_000,
                "client-post-window-0001",
                "CLIENT_BOOK_UPDATE",
                {"best_ask_ticks": 102, "best_bid_ticks": 101},
            ),
        ),
        rng_state={"algorithm": "PCG64", "state": "WO33D-RNG-SECRET"},
        hidden_schedule=(
            {
                "label": "WO33D-HIDDEN-SCHEDULE",
                "simulation_time_us": 4_750_000,
            },
        ),
        capability_labels=tuple(
            row.capability for row in candidate.capability_record.records
        ),
        historical_provenance={
            "description": "authoritative synthetic qualification replay",
            "historical_mode": "SYNTHETIC_GROUND_TRUTH",
            "source_locator": "memory://wo33d/source/0001",
        },
    )
    extracted = extract_observable_lesson_source_v1(candidate, recorded)
    selected = select_technical_review_candidates((candidate,), target_count=1)
    if len(selected.decisions) != 1:
        raise AssertionError("WO33-D fixture candidate was not selected")
    lesson = build_playable_lesson_v1(
        candidate,
        extracted,
        selected.decisions[0],
    )
    return candidate, recorded, extracted, lesson


def _sample_candidate(
    *,
    bounds: CandidateBoundsV1 | None = None,
    evidence_class: EvidenceClassV1 = EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
    source_kind: SourceKindV1 = SourceKindV1.RUN,
    detector_id: str = "STRONG_QUEUE_IMBALANCE",
    source_row: str = "quiet",
    source_ordinal: int = 1,
    event_ordinal: int = 1,
    phase: str = "CONTINUOUS",
    regime_id: str = "BALANCED",
    source_window_outcome: SourceWindowOutcomeV1 = (
        SourceWindowOutcomeV1.CONTINUATION
    ),
    signal_legibility_ppm: int = 650_000,
    duration_legibility_ppm: int | None = 800_000,
    conflict_ppm: int | None = 100_000,
    spread_ticks: int | None = 2,
    latency_us: int | None = 250,
    three_level_depth: int | None = 3_000,
    venue_count: int | None = 1,
    qualifying_units: int = 2,
    eligible_units: int = 100,
    feature_tokens: tuple[str, ...] | None = None,
    event_five_grams: tuple[tuple[str, ...], ...] | None = None,
) -> LessonCandidateV1:
    selected_bounds = bounds or CandidateBoundsV1(
        source_start_us=0,
        source_end_us=10_000_000,
        warmup_start_us=1_000_000,
        active_start_us=2_000_000,
        active_end_us=4_000_001,
        post_end_us=5_000_000,
    )
    declaration = DETECTOR_REGISTRY_V1.require(detector_id, 1)
    if type(source_ordinal) is not int or source_ordinal <= 0:
        raise ValueError("sample source ordinal must be positive")
    if type(event_ordinal) is not int or event_ordinal <= 0:
        raise ValueError("sample event ordinal must be positive")
    source_id = {
        SourceKindV1.RUN: f"qualification-run-{source_ordinal:04d}",
        SourceKindV1.DATASET: f"qualification-dataset-{source_ordinal:04d}",
        SourceKindV1.RECONSTRUCTION: (
            f"qualification-reconstruction-{source_ordinal:04d}"
        ),
    }[source_kind]
    source_digest_label = (
        "source-run" if source_ordinal == 1 else f"source-run:{source_ordinal}"
    )
    checkpoint_digest_label = (
        "checkpoint" if source_ordinal == 1 else f"checkpoint:{source_ordinal}"
    )
    prefix_digest_label = (
        "event-prefix" if source_ordinal == 1 else f"event-prefix:{source_ordinal}"
    )
    source_identity = SourceIdentityV1(
        source_kind,
        source_id,
        _digest(source_digest_label),
    )
    checkpoint = CheckpointReferenceV1(
        f"checkpoint-{source_ordinal:04d}",
        _digest(checkpoint_digest_label),
    )
    ancestry = SourceAncestryV1(
        source_kind=source_kind,
        source_id=source_id,
        source_sha256=_digest(source_digest_label),
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        event_prefix_sha256=_digest(prefix_digest_label),
        parent_source_ancestry_sha256=None,
    )
    selected_feature_tokens = feature_tokens or (
        "BOOK_UPDATE|best_ask_size|INTEGER|400",
        "BOOK_UPDATE|best_bid_size|INTEGER|1600",
    )
    selected_event_five_grams = event_five_grams or (
        (
            "BOOK_UPDATE|BUY|AT_BID",
            "BOOK_UPDATE|SELL|AT_ASK",
        ),
    )
    event_ids = (
        f"event-{event_ordinal:04d}-0001",
        f"event-{event_ordinal:04d}-0002",
    )
    observable = ObservableFeatureSummaryV1(
        feature_tokens=selected_feature_tokens,
        regime_signature=RegimeSignatureV1(
            phase=phase,
            regime_id=regime_id,
            volume_band="NORMAL",
            liquidity_band="NORMAL",
            spread_band="TWO",
        ),
        event_five_grams=selected_event_five_grams,
        contributing_source_event_ids=event_ids,
    )
    threshold = _digest("thresholds")
    detector = DetectorProjectionV1(detector_id, 1, threshold)
    inventory = _inventory_for(detector_id, evidence_class, source_identity)
    decision = DETECTOR_REGISTRY_V1.assess(
        detector_id,
        1,
        threshold,
        inventory,
    )
    if decision.capability_record is None:
        raise AssertionError("sample source unexpectedly failed detector admission")
    ground_truth = (
        GroundTruthSummaryV1(
            detector_id,
            CandidateDirectionV1.BUY,
            (event_ids[-1],),
        )
        if evidence_class is EvidenceClassV1.SYNTHETIC_GROUND_TRUTH
        else None
    )
    reveal_ids = (
        observable.contributing_source_event_ids
        if ground_truth is None
        else ground_truth.supporting_source_event_ids
    )
    reveal = RevealMaterialV1(
        detector_id=detector_id,
        detector_version=1,
        direction=CandidateDirectionV1.BUY,
        observable_feature_summary_sha256=observable.sha256,
        ground_truth_summary_sha256=(
            None if ground_truth is None else ground_truth.sha256
        ),
        supporting_source_event_ids=reveal_ids,
    )
    reaction_us = selected_bounds.post_end_us - selected_bounds.activation_us
    return LessonCandidateV1(
        source_ancestry=ancestry,
        candidate_key=CandidateKeyV1(
            detector_id=detector_id,
            direction=CandidateDirectionV1.BUY,
            side=CandidateSideV1.BUY,
            venue="CONSOLIDATED",
            price=100,
            witness_key="NOT_APPLICABLE",
            anchor_start_us=selected_bounds.active_start_us,
            evidence_discriminator=observable.evidence_discriminator,
        ),
        detector=detector,
        bounds=selected_bounds,
        checkpoint=checkpoint,
        observable_feature_summary=observable,
        ground_truth_summary=ground_truth,
        difficulty_projection=DifficultyProjectionV1(
            signal_legibility_ppm=signal_legibility_ppm,
            duration_legibility_ppm=duration_legibility_ppm,
            conflict_ppm=conflict_ppm,
            reaction_us=reaction_us,
            spread_ticks=spread_ticks,
            latency_us=latency_us,
            three_level_depth=three_level_depth,
            venue_count=venue_count,
            hidden_uncertainty_ppm=(
                {
                    EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: 0,
                    EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: 250_000,
                    EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: 750_000,
                }[evidence_class]
                if declaration.hidden_liquidity_relevant
                else None
            ),
            objective_shares=None,
            executable_depth=None,
            feature_count=len(
                {token.split("|", 3)[1] for token in selected_feature_tokens}
            ),
            evidence_quality_ppm=evidence_class.evidence_quality_ppm,
        ),
        rarity_projection=RarityProjectionV1(
            qualification_source_row=source_row,
            qualifying_units=qualifying_units,
            eligible_units=eligible_units,
        ),
        source_window_outcome=source_window_outcome,
        primary_skill_id=declaration.primary_skill_id,
        supporting_skill_ids=declaration.supporting_skill_ids,
        objective_projection=ObserveClassifyObjectiveV1(
            detector_id,
            CandidateDirectionV1.BUY,
            selected_bounds.activation_us,
            selected_bounds.post_end_us,
        ),
        reveal_material=reveal,
        known_ambiguity=(),
        capability_record=decision.capability_record,
        evidence_class=evidence_class,
    )


def _inventory_for(
    detector_id: str,
    evidence_class: EvidenceClassV1,
    source_identity: SourceIdentityV1 | None = None,
) -> SourceCapabilityInventoryV1:
    declaration = DETECTOR_REGISTRY_V1.require(detector_id, 1)
    default_kind = {
        EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: SourceKindV1.RUN,
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: SourceKindV1.DATASET,
        EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: SourceKindV1.RECONSTRUCTION,
    }[evidence_class]
    identity = source_identity or SourceIdentityV1(
        default_kind,
        f"capability-{default_kind.value.lower()}-0001",
        _digest("capability-source"),
    )
    required_capabilities = declaration.required_capabilities_for(evidence_class)
    records = tuple(
        CapabilityRecordRowV1(
            capability,
            (
                CapabilityEvidenceReferenceV1(
                    CapabilityEvidenceKindV1.SOURCE_MANIFEST,
                    f"manifest-{capability.lower()}",
                    _digest(f"capability:{capability}"),
                ),
            ),
        )
        for capability in required_capabilities
    )
    return SourceCapabilityInventoryV1(identity, evidence_class, records)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _raises(operation: Callable[[], object]) -> bool:
    try:
        operation()
    except (AttributeError, PermissionError, TypeError, ValueError):
        return True
    return False


__all__ = [
    "DrillMiningAuditCase",
    "WO33A_DETECTOR_COUNT",
    "WO33A_IDENTITY_KEY_COUNT",
    "WO33A_REVIEW_DECISION_COUNT",
    "WO33A_SKILL_COUNT",
    "WO33A1_MINING_PLAN_MANIFEST_SHA256",
    "WO33A1_POLICY_BUNDLE_SHA256",
    "WO33A1_REVIEW_TARGET_COUNT",
    "WO33A1_SOURCE_COUNT",
    "WO33A1_SOURCE_MANIFEST_SHA256",
    "WO33A1_THRESHOLD_MANIFEST_SHA256",
    "WO33B1_DETECTOR_COUNT",
    "WO33B1_SYNTHETIC_REPORT_SHA256",
    "WO33B2_DETECTOR_COUNT",
    "WO33B2_SYNTHETIC_REPORT_SHA256",
    "WO33C_DIFFICULTY_COMPONENT_COUNT",
    "WO33C_DIVERSITY_DIMENSION_COUNT",
    "WO33C_REVIEW_TARGET_COUNT",
    "WO33D_ASSESSMENT_FIELD_COUNT",
    "WO33D_SOURCE_LINEAGE_FIELD_COUNT",
    "audit_drill_mining",
    "audit_wo33a1_drill_mining",
    "audit_wo33b1_drill_mining",
    "audit_wo33b2_drill_mining",
    "audit_wo33c_drill_mining",
    "audit_wo33d_drill_mining",
]
