"""Executable WO33-A audit for lesson-candidate contract boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace

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
    DetectorSupportStatusV1,
    DifficultyProjectionV1,
    EvidenceClassV1,
    GroundTruthSummaryV1,
    HumanReviewDecisionV1,
    HumanReviewSidecarV1,
    LessonCandidateV1,
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
    canonical_json_bytes,
    round_div_even,
)


WO33A_DETECTOR_COUNT = 22
WO33A_SKILL_COUNT = 23
WO33A_IDENTITY_KEY_COUNT = 21
WO33A_REVIEW_DECISION_COUNT = 4


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
    "audit_drill_mining",
]
