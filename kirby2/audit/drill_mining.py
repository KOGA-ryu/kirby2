"""Executable WO33-A audit for lesson-candidate contract boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from kirby2.audit.historical_lessons import audit_historical_lessons
from kirby2.mining import (
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
    canonical_json_bytes,
    load_mining_policy_bundle,
    round_div_even,
    sha256_json,
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
    if (
        runtime.handler_ids != B1_DETECTOR_IDS_V1
        or len(runtime.handler_ids) != WO33B1_DETECTOR_COUNT
        or len({id(handler) for handler in handlers.values()})
        != WO33B1_DETECTOR_COUNT
    ):
        failures.append("B1 does not expose fifteen distinct operational handlers")
    if (
        runtime.threshold_manifest.manifest_sha256
        != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
    ):
        failures.append("B1 runtime did not pin the committed A1 threshold manifest")
    for detector_id in runtime.handler_ids:
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
        [reports[detector_id].as_dict() for detector_id in runtime.handler_ids]
    )
    if aggregate_sha256 != WO33B1_SYNTHETIC_REPORT_SHA256:
        failures.append("B1 synthetic runtime evidence digest changed")
    return DrillMiningAuditCase(
        "b1_fifteen_distinct_handlers_consume_the_committed_a1_manifest",
        (
            f"handlers={len(runtime.handler_ids)}/15 versions=1 "
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
    from kirby2.mining.runtime import MiningDetectorRuntimeV1

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
        for detector_id in runtime.handler_ids
    }
    reports: dict[str, object] = {}
    for detector_id in runtime.handler_ids:
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


def _sample_candidate(
    *,
    bounds: CandidateBoundsV1 | None = None,
    evidence_class: EvidenceClassV1 = EvidenceClassV1.SYNTHETIC_GROUND_TRUTH,
    source_kind: SourceKindV1 = SourceKindV1.RUN,
    detector_id: str = "STRONG_QUEUE_IMBALANCE",
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
    source_id = {
        SourceKindV1.RUN: "qualification-run-0001",
        SourceKindV1.DATASET: "qualification-dataset-0001",
        SourceKindV1.RECONSTRUCTION: "qualification-reconstruction-0001",
    }[source_kind]
    source_identity = SourceIdentityV1(
        source_kind,
        source_id,
        _digest("source-run"),
    )
    checkpoint = CheckpointReferenceV1(
        "checkpoint-0001",
        _digest("checkpoint"),
    )
    ancestry = SourceAncestryV1(
        source_kind=source_kind,
        source_id=source_id,
        source_sha256=_digest("source-run"),
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        event_prefix_sha256=_digest("event-prefix"),
        parent_source_ancestry_sha256=None,
    )
    observable = ObservableFeatureSummaryV1(
        feature_tokens=(
            "BOOK_UPDATE|best_ask_size|INTEGER|400",
            "BOOK_UPDATE|best_bid_size|INTEGER|1600",
        ),
        regime_signature=RegimeSignatureV1(
            phase="CONTINUOUS",
            regime_id="BALANCED",
            volume_band="NORMAL",
            liquidity_band="NORMAL",
            spread_band="TWO",
        ),
        event_five_grams=(
            (
                "BOOK_UPDATE|BUY|AT_BID",
                "BOOK_UPDATE|SELL|AT_ASK",
            ),
        ),
        contributing_source_event_ids=("event-0001", "event-0002"),
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
            ("event-0002",),
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
            signal_legibility_ppm=650_000,
            duration_legibility_ppm=800_000,
            conflict_ppm=100_000,
            reaction_us=reaction_us,
            spread_ticks=2,
            latency_us=250,
            three_level_depth=3_000,
            venue_count=1,
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
            feature_count=2,
            evidence_quality_ppm=evidence_class.evidence_quality_ppm,
        ),
        rarity_projection=RarityProjectionV1(
            qualification_source_row="quiet-full-day",
            qualifying_units=2,
            eligible_units=100,
        ),
        source_window_outcome=SourceWindowOutcomeV1.CONTINUATION,
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
    "audit_drill_mining",
    "audit_wo33a1_drill_mining",
    "audit_wo33b1_drill_mining",
]
