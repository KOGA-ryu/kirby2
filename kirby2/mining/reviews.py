"""Immutable lesson-mining qualification and review contracts (WO33-E).

This module deliberately keeps three states separate:

* a detector-bound candidate proposal;
* software-completed technical readiness for human review; and
* a human decision recorded in a separate immutable sidecar.

No method in the qualification path can emit a human ``ACCEPTED`` decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from .detectors import DETECTOR_REGISTRY_V1, SourceCapabilityInventoryV1
from .models import (
    MINING_SCHEMA_VERSION_V1,
    CandidateDirectionV1,
    CandidateSideV1,
    CapabilityEvidenceKindV1,
    CapabilityEvidenceReferenceV1,
    CapabilityRecordRowV1,
    DetectorProjectionV1,
    EvidenceClassV1,
    GroundTruthSummaryV1,
    LessonCandidateV1,
    ObservableFeatureSummaryV1,
    ObserveClassifyObjectiveV1,
    QualificationSourceRowV1,
    QualificationSourcesManifestV1,
    RarityProjectionV1,
    RegimeSignatureV1,
    RevealMaterialV1,
    SourceAncestryV1,
    SourceIdentityV1,
    SourceKindV1,
    SourceWindowOutcomeV1,
    canonical_json_bytes,
    round_div_even,
    sha256_json,
)
from .selection import (
    REVIEW_TARGET_COUNT_V1,
    ReviewSelectionResultV1,
    materially_distinct_event_candidate_v1,
    select_technical_review_candidates,
    source_window_outcome_v1,
)
from .deduplication import (
    canonical_event_token_v1,
    event_five_grams_v1,
    observable_feature_token_v1,
    spread_band_v1,
)
from .ranking import (
    and_legibility_ppm,
    build_difficulty_projection,
    lower_bound_legibility_ppm,
    upper_bound_legibility_ppm,
)
from .runtime import (
    OPERATIONAL_DETECTOR_IDS_V1,
    DetectorMeasurementV1,
    DetectorOpportunityV1,
    DetectorRunReportV1,
    MiningDetectorRuntimeV1,
    MiningEventReferenceV1,
    MiningExclusionV1,
)


LESSON_REVIEW_SCHEMA_VERSION_V1 = 1
LESSON_REVIEW_RUBRIC_VERSION_V1 = "LESSON_REVIEW_RUBRIC_V1"
WO33A1_SOURCE_MANIFEST_SHA256_V1 = (
    "ff0cb292d1ed764b73f197462cd49c0c8a345fffcd547bab1c60b726b7d5eda5"
)
OUTCOME_CONDITIONING_CAVEAT_V1 = (
    "PERFORMANCE_ON_OUTCOME_CONDITIONED_MINED_WINDOWS_IS_NOT_PERFORMANCE_"
    "OVER_UNSELECTED_MARKET_TIME"
)
HUMAN_REVIEW_FIELDS_V1 = (
    "useful_candidate_judgment",
    "duplicate",
    "false_positive",
    "unfair_window",
    "missing_context",
    "detector_adjustment_recommendation",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_NOT_APPLICABLE = "NOT_APPLICABLE"


class TechnicalCandidateStatusV1(str, Enum):
    PENDING = "PENDING"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"


class LessonReviewDecisionV1(str, Enum):
    PENDING = "PENDING"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NEEDS_EDIT = "NEEDS_EDIT"
    SUPERSEDED = "SUPERSEDED"


class ReviewerAuthorityV1(str, Enum):
    AUTOMATION = "AUTOMATION"
    LOCAL_AUTHENTICATED = "LOCAL_AUTHENTICATED"


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC")
    return value


def _parse_canonical_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not canonical JSON") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} is not one canonical JSON object")
    return payload


def _iso_utc(value: object, label: str) -> str:
    text = _require_text(value, label)
    if not text.endswith("Z"):
        raise ValueError(f"{label} must use explicit UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")
    return text


@dataclass(frozen=True, slots=True)
class SourceMaterializationV1:
    row_id: str
    stratum: str
    source_id: str
    source_sha256: str
    example_path: str
    example_raw_sha256: str
    compiled_artifact_sha256: str
    native_run_digest: str
    replay_digest: str
    final_state_sha256: str
    evidence_unit_count: int
    capability_contract_sha256: str
    detector_adapter_status: str
    detector_input_sha256: str
    detector_opportunity_count: int
    protected_seed_execution: bool = False
    replay_status: str = "PASS"

    def __post_init__(self) -> None:
        for value, label in (
            (self.row_id, "source row ID"),
            (self.stratum, "source stratum"),
            (self.source_id, "source ID"),
            (self.example_path, "materialized example path"),
            (self.detector_adapter_status, "detector adapter status"),
        ):
            _require_text(value, label)
        for value, label in (
            (self.source_sha256, "source digest"),
            (self.example_raw_sha256, "example digest"),
            (self.compiled_artifact_sha256, "compiled artifact digest"),
            (self.capability_contract_sha256, "capability contract digest"),
            (self.detector_input_sha256, "detector input digest"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.native_run_digest, "native run digest"),
            (self.replay_digest, "replay digest"),
            (self.final_state_sha256, "final state digest"),
        ):
            if value != _NOT_APPLICABLE:
                _require_sha256(value, label)
        if type(self.evidence_unit_count) is not int or self.evidence_unit_count <= 0:
            raise ValueError("materialized source must expose positive evidence units")
        if (
            type(self.detector_opportunity_count) is not int
            or self.detector_opportunity_count < 0
        ):
            raise ValueError("detector opportunity count must be nonnegative")
        if self.protected_seed_execution is not False:
            raise ValueError("WO33-E may not execute protected WO31 qualification seeds")
        if self.replay_status != "PASS":
            raise ValueError("only replay-verified source materialization is admissible")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_contract_sha256": self.capability_contract_sha256,
            "compiled_artifact_sha256": self.compiled_artifact_sha256,
            "detector_adapter_status": self.detector_adapter_status,
            "detector_input_sha256": self.detector_input_sha256,
            "detector_opportunity_count": self.detector_opportunity_count,
            "evidence_unit_count": self.evidence_unit_count,
            "example_path": self.example_path,
            "example_raw_sha256": self.example_raw_sha256,
            "final_state_sha256": self.final_state_sha256,
            "native_run_digest": self.native_run_digest,
            "protected_seed_execution": self.protected_seed_execution,
            "replay_digest": self.replay_digest,
            "replay_status": self.replay_status,
            "row_id": self.row_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "stratum": self.stratum,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SourceMaterializationV1:
        expected = {
            "capability_contract_sha256",
            "compiled_artifact_sha256",
            "detector_adapter_status",
            "detector_input_sha256",
            "detector_opportunity_count",
            "evidence_unit_count",
            "example_path",
            "example_raw_sha256",
            "final_state_sha256",
            "native_run_digest",
            "protected_seed_execution",
            "replay_digest",
            "replay_status",
            "row_id",
            "source_id",
            "source_sha256",
            "stratum",
        }
        if set(payload) != expected:
            raise ValueError("source materialization fields differ")
        return cls(
            row_id=str(payload["row_id"]),
            stratum=str(payload["stratum"]),
            source_id=str(payload["source_id"]),
            source_sha256=str(payload["source_sha256"]),
            example_path=str(payload["example_path"]),
            example_raw_sha256=str(payload["example_raw_sha256"]),
            compiled_artifact_sha256=str(payload["compiled_artifact_sha256"]),
            detector_adapter_status=str(payload["detector_adapter_status"]),
            detector_input_sha256=str(payload["detector_input_sha256"]),
            detector_opportunity_count=int(payload["detector_opportunity_count"]),
            native_run_digest=str(payload["native_run_digest"]),
            replay_digest=str(payload["replay_digest"]),
            final_state_sha256=str(payload["final_state_sha256"]),
            evidence_unit_count=int(payload["evidence_unit_count"]),
            capability_contract_sha256=str(payload["capability_contract_sha256"]),
            protected_seed_execution=bool(payload["protected_seed_execution"]),
            replay_status=str(payload["replay_status"]),
        )


@dataclass(frozen=True, slots=True)
class DetectorExecutionV1:
    """One exact operational-detector invocation over one materialized source."""

    row_id: str
    detector_id: str
    adapter_status: str
    source_evidence_sha256: str
    opportunities: tuple[DetectorOpportunityV1, ...]
    report: DetectorRunReportV1

    def __post_init__(self) -> None:
        _require_text(self.row_id, "detector execution source row")
        if _IDENTIFIER.fullmatch(self.detector_id) is None:
            raise ValueError("detector execution ID is invalid")
        _require_text(self.adapter_status, "detector execution adapter status")
        _require_sha256(
            self.source_evidence_sha256,
            "detector execution source evidence digest",
        )
        if type(self.opportunities) is not tuple or any(
            not isinstance(item, DetectorOpportunityV1)
            for item in self.opportunities
        ):
            raise TypeError("detector execution opportunities are invalid")
        ordered = tuple(sorted(self.opportunities, key=lambda item: item.sort_key))
        if ordered != self.opportunities:
            raise ValueError("detector execution opportunities are not canonical")
        if any(item.detector_id != self.detector_id for item in self.opportunities):
            raise ValueError("detector execution contains a foreign opportunity")
        if not isinstance(self.report, DetectorRunReportV1):
            raise TypeError("detector execution report is invalid")
        if self.report.detector.detector_id != self.detector_id:
            raise ValueError("detector execution report names another detector")
        opportunity_digests = {item.sha256 for item in self.opportunities}
        considered_digests = {
            item.opportunity_sha256 for item in self.report.considered
        }
        if opportunity_digests != considered_digests:
            raise ValueError("detector report does not account for every opportunity")

    @property
    def execution_sha256(self) -> str:
        return sha256_json(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "adapter_status": self.adapter_status,
            "detector_id": self.detector_id,
            "opportunities": [item.as_dict() for item in self.opportunities],
            "record_kind": "LESSON_DETECTOR_EXECUTION_V1",
            "report": self.report.as_dict(),
            "report_sha256": self.report.report_sha256,
            "row_id": self.row_id,
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "source_evidence_sha256": self.source_evidence_sha256,
        }
        if include_digest:
            payload["execution_sha256"] = self.execution_sha256
        return payload


@dataclass(slots=True)
class _PreparedSourceEvidenceV1:
    materialization: SourceMaterializationV1
    opportunities_by_detector: dict[str, tuple[DetectorOpportunityV1, ...]]
    timeline: object | None = None


@dataclass(frozen=True, slots=True)
class CandidateRecipeV1:
    row_id: str
    ordinal: int
    detector_id: str
    detector_report_sha256: str
    finding_sha256: str
    opportunity_id: str
    opportunity_sha256: str
    phase: str
    direction: CandidateDirectionV1
    activation_mid_x2: int | None
    activation_spread_ticks: int | None
    final_mid_x2: int | None
    source_window_outcome: SourceWindowOutcomeV1
    seed: int

    def __post_init__(self) -> None:
        _require_text(self.row_id, "candidate recipe source row")
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("candidate recipe ordinal must be positive")
        if _IDENTIFIER.fullmatch(self.detector_id) is None:
            raise ValueError("candidate recipe detector ID is invalid")
        for value, label in (
            (self.detector_report_sha256, "candidate detector report digest"),
            (self.finding_sha256, "candidate finding digest"),
            (self.opportunity_sha256, "candidate opportunity digest"),
        ):
            _require_sha256(value, label)
        _require_text(self.opportunity_id, "candidate opportunity ID")
        _require_text(self.phase, "candidate recipe phase")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("candidate recipe direction is invalid")
        for value, label in (
            (self.activation_mid_x2, "candidate activation mid_x2"),
            (self.final_mid_x2, "candidate final mid_x2"),
        ):
            if value is not None and type(value) is not int:
                raise TypeError(f"{label} must be an exact integer or null")
        if self.activation_spread_ticks is not None and (
            type(self.activation_spread_ticks) is not int
            or self.activation_spread_ticks <= 0
        ):
            raise ValueError("candidate activation spread must be positive or null")
        if not isinstance(self.source_window_outcome, SourceWindowOutcomeV1):
            raise TypeError("candidate recipe outcome is invalid")
        if self.source_window_outcome is not source_window_outcome_v1(
            self.direction,
            self.activation_mid_x2,
            self.final_mid_x2,
        ):
            raise ValueError("candidate recipe outcome differs from frozen quotes")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("candidate recipe seed must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "activation_mid_x2": self.activation_mid_x2,
            "activation_spread_ticks": self.activation_spread_ticks,
            "detector_id": self.detector_id,
            "detector_report_sha256": self.detector_report_sha256,
            "direction": self.direction.value,
            "final_mid_x2": self.final_mid_x2,
            "finding_sha256": self.finding_sha256,
            "opportunity_id": self.opportunity_id,
            "opportunity_sha256": self.opportunity_sha256,
            "ordinal": self.ordinal,
            "phase": self.phase,
            "row_id": self.row_id,
            "seed": self.seed,
            "source_window_outcome": self.source_window_outcome.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateRecipeV1:
        if set(payload) != {
            "activation_mid_x2",
            "activation_spread_ticks",
            "detector_id",
            "detector_report_sha256",
            "direction",
            "final_mid_x2",
            "finding_sha256",
            "opportunity_id",
            "opportunity_sha256",
            "ordinal",
            "phase",
            "row_id",
            "seed",
            "source_window_outcome",
        }:
            raise ValueError("candidate recipe fields differ")
        return cls(
            row_id=str(payload["row_id"]),
            ordinal=int(payload["ordinal"]),
            detector_id=str(payload["detector_id"]),
            detector_report_sha256=str(payload["detector_report_sha256"]),
            finding_sha256=str(payload["finding_sha256"]),
            opportunity_id=str(payload["opportunity_id"]),
            opportunity_sha256=str(payload["opportunity_sha256"]),
            phase=str(payload["phase"]),
            direction=CandidateDirectionV1(str(payload["direction"])),
            activation_mid_x2=(
                None
                if payload["activation_mid_x2"] is None
                else int(payload["activation_mid_x2"])
            ),
            activation_spread_ticks=(
                None
                if payload["activation_spread_ticks"] is None
                else int(payload["activation_spread_ticks"])
            ),
            final_mid_x2=(
                None
                if payload["final_mid_x2"] is None
                else int(payload["final_mid_x2"])
            ),
            source_window_outcome=SourceWindowOutcomeV1(
                str(payload["source_window_outcome"])
            ),
            seed=int(payload["seed"]),
        )


@dataclass(frozen=True, slots=True)
class ReviewReadyCandidateV1:
    recipe: CandidateRecipeV1
    candidate: LessonCandidateV1
    technical_status: TechnicalCandidateStatusV1
    technical_reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.recipe, CandidateRecipeV1):
            raise TypeError("review-ready candidate recipe is invalid")
        if not isinstance(self.candidate, LessonCandidateV1):
            raise TypeError("review-ready candidate is untyped")
        if self.candidate.rarity_projection.qualification_source_row != self.recipe.row_id:
            raise ValueError("review-ready candidate source and recipe differ")
        if self.candidate.detector.detector_id != self.recipe.detector_id:
            raise ValueError("review-ready candidate detector and recipe differ")
        if not isinstance(self.technical_status, TechnicalCandidateStatusV1):
            raise TypeError("candidate technical status is invalid")
        if (
            type(self.technical_reason_codes) is not tuple
            or not self.technical_reason_codes
            or any(_IDENTIFIER.fullmatch(item) is None for item in self.technical_reason_codes)
            or tuple(sorted(set(self.technical_reason_codes)))
            != self.technical_reason_codes
        ):
            raise ValueError("candidate technical reasons must be sorted identifiers")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.as_dict(),
            "candidate_digest": self.candidate.candidate_digest,
            "candidate_id": self.candidate.candidate_id,
            "human_review_status": "PENDING",
            "recipe": self.recipe.as_dict(),
            "technical_reason_codes": list(self.technical_reason_codes),
            "technical_status": self.technical_status.value,
        }


@dataclass(frozen=True, slots=True)
class TechnicalReviewRowV1:
    ordinal: int
    candidate_id: str
    candidate_digest: str
    source_row: str
    source_id: str
    source_sha256: str
    detector_id: str
    selection_stage: str
    selection_decision_sha256: str
    capability_record_sha256: str
    source_ancestry_sha256: str
    technical_status: TechnicalCandidateStatusV1
    technical_reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("technical review row ordinal must be positive")
        _require_text(self.candidate_id, "technical review candidate ID")
        _require_sha256(self.candidate_digest, "technical review candidate digest")
        _require_text(self.source_row, "technical review source row")
        _require_text(self.source_id, "technical review source ID")
        _require_sha256(self.source_sha256, "technical review source digest")
        if _IDENTIFIER.fullmatch(self.detector_id) is None:
            raise ValueError("technical review detector ID is invalid")
        _require_text(self.selection_stage, "technical review selection stage")
        _require_sha256(
            self.selection_decision_sha256,
            "technical review selection decision digest",
        )
        _require_sha256(
            self.capability_record_sha256,
            "technical review capability digest",
        )
        _require_sha256(self.source_ancestry_sha256, "technical review ancestry digest")
        if not isinstance(self.technical_status, TechnicalCandidateStatusV1):
            raise TypeError("technical review status is invalid")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "capability_record_sha256": self.capability_record_sha256,
            "detector_id": self.detector_id,
            "human_review_status": "PENDING",
            "ordinal": self.ordinal,
            "outcome_conditioning_caveat": OUTCOME_CONDITIONING_CAVEAT_V1,
            "selection_decision_sha256": self.selection_decision_sha256,
            "selection_stage": self.selection_stage,
            "source_ancestry_sha256": self.source_ancestry_sha256,
            "source_id": self.source_id,
            "source_row": self.source_row,
            "source_sha256": self.source_sha256,
            "technical_reason_codes": list(self.technical_reason_codes),
            "technical_status": self.technical_status.value,
        }
        payload.update({name: "PENDING" for name in HUMAN_REVIEW_FIELDS_V1})
        return payload


@dataclass(frozen=True, slots=True)
class TechnicalReviewPacketV1:
    rows: tuple[TechnicalReviewRowV1, ...]
    target_count: int
    selected_count: int
    shortfall_count: int
    event_five_gate_passed: bool
    mandatory_source_counts: tuple[tuple[str, int], ...]
    human_review_status: str = "PENDING"

    def __post_init__(self) -> None:
        if type(self.rows) is not tuple or any(
            not isinstance(row, TechnicalReviewRowV1) for row in self.rows
        ):
            raise TypeError("technical review packet rows are invalid")
        if tuple(row.ordinal for row in self.rows) != tuple(range(1, len(self.rows) + 1)):
            raise ValueError("technical review packet row ordinals are not contiguous")
        if self.selected_count != len(self.rows):
            raise ValueError("technical review packet selected count differs")
        if self.target_count != REVIEW_TARGET_COUNT_V1:
            raise ValueError("technical review packet target differs from preregistration")
        if self.shortfall_count != self.target_count - self.selected_count:
            raise ValueError("technical review packet shortfall differs")
        if self.human_review_status != "PENDING":
            raise ValueError("software review packet cannot claim human review")
        if tuple(name for name, _count in self.mandatory_source_counts) != (
            "event",
            "quiet",
            "hidden",
            "fragmented",
            "historical",
        ):
            raise ValueError("technical review packet source order differs")

    @property
    def packet_sha256(self) -> str:
        return sha256_json(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "event_five_gate_passed": self.event_five_gate_passed,
            "human_fields": list(HUMAN_REVIEW_FIELDS_V1),
            "human_review_status": self.human_review_status,
            "mandatory_source_counts": {
                name: count for name, count in self.mandatory_source_counts
            },
            "outcome_conditioning_caveat": OUTCOME_CONDITIONING_CAVEAT_V1,
            "record_kind": "LESSON_TECHNICAL_REVIEW_PACKET_V1",
            "rows": [row.as_dict() for row in self.rows],
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "selected_count": self.selected_count,
            "shortfall_count": self.shortfall_count,
            "target_count": self.target_count,
        }
        if include_digest:
            payload["packet_sha256"] = self.packet_sha256
        return payload


@dataclass(frozen=True, slots=True)
class MiningQualificationResultV1:
    source_manifest_raw: bytes
    source_manifest: QualificationSourcesManifestV1
    seed: int
    active_source_rows: tuple[str, ...]
    source_materializations: tuple[SourceMaterializationV1, ...]
    detector_executions: tuple[DetectorExecutionV1, ...]
    candidates: tuple[ReviewReadyCandidateV1, ...]
    selection: ReviewSelectionResultV1
    review_packet: TechnicalReviewPacketV1

    def __post_init__(self) -> None:
        if self.source_manifest.canonical_bytes() != self.source_manifest_raw:
            raise ValueError("qualification result did not preserve exact source matrix bytes")
        if self.source_manifest.manifest_sha256 != WO33A1_SOURCE_MANIFEST_SHA256_V1:
            raise ValueError("qualification result binds a foreign source matrix")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("qualification seed must be nonnegative")
        if tuple(item.row_id for item in self.source_materializations) != self.active_source_rows:
            raise ValueError("qualification source materialization order differs")
        expected_execution_keys = tuple(
            (row_id, detector_id)
            for row_id in self.active_source_rows
            for detector_id in OPERATIONAL_DETECTOR_IDS_V1
        )
        actual_execution_keys = tuple(
            (item.row_id, item.detector_id) for item in self.detector_executions
        )
        if actual_execution_keys != expected_execution_keys:
            raise ValueError("qualification detector matrix is incomplete or reordered")
        sources_by_row = {item.row_id: item for item in self.source_materializations}
        for execution in self.detector_executions:
            source = sources_by_row[execution.row_id]
            if execution.source_evidence_sha256 != source.detector_input_sha256:
                raise ValueError("detector execution is not bound to source input")
        finding_digests = {
            finding.finding_sha256
            for execution in self.detector_executions
            for finding in execution.report.findings
        }
        recipe_findings = {item.recipe.finding_sha256 for item in self.candidates}
        if finding_digests != recipe_findings:
            raise ValueError("qualification candidates differ from emitted findings")
        candidate_ids = {item.candidate.candidate_id for item in self.candidates}
        decision_ids = {
            item.candidate_id for item in self.selection.deduplication.decisions
        }
        if candidate_ids != decision_ids or len(candidate_ids) != len(self.candidates):
            raise ValueError("qualification selection input differs from candidate records")
        if tuple(row.candidate_id for row in self.review_packet.rows) != tuple(
            item.candidate_id for item in self.selection.selected
        ):
            raise ValueError("qualification review packet differs from selection")

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def ready_count(self) -> int:
        return sum(
            item.technical_status is TechnicalCandidateStatusV1.READY_FOR_HUMAN_REVIEW
            for item in self.candidates
        )

    @property
    def event_materially_distinct_count(self) -> int:
        selected: list[LessonCandidateV1] = []
        for candidate in self.selection.selected:
            if candidate.rarity_projection.qualification_source_row != "event":
                continue
            if materially_distinct_event_candidate_v1(candidate, selected):
                selected.append(candidate)
        return len(selected)

    def source_validation_dict(self) -> dict[str, object]:
        return {
            "active_source_rows": list(self.active_source_rows),
            "manifest_file_sha256": hashlib.sha256(self.source_manifest_raw).hexdigest(),
            "manifest_sha256": self.source_manifest.manifest_sha256,
            "protected_seed_execution": False,
            "record_kind": "LESSON_MINING_SOURCE_VALIDATION_V1",
            "rows": [item.as_dict() for item in self.source_materializations],
            "detector_executions": [
                item.as_dict() for item in self.detector_executions
            ],
            "detector_invocation_status": "EXECUTED",
            "detector_report_count": len(self.detector_executions),
            "detector_opportunity_count": sum(
                len(item.opportunities) for item in self.detector_executions
            ),
            "detector_finding_count": sum(
                len(item.report.findings) for item in self.detector_executions
            ),
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "seed": self.seed,
            "status": "PASS",
        }

    def candidates_dict(self) -> dict[str, object]:
        return {
            "candidate_count": self.candidate_count,
            "detector_finding_count": sum(
                len(item.report.findings) for item in self.detector_executions
            ),
            "candidates": [item.as_dict() for item in self.candidates],
            "human_accepted_count": 0,
            "human_review_status": "PENDING",
            "record_kind": "LESSON_MINING_CANDIDATES_V1",
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "seed": self.seed,
        }

    def selection_dict(self) -> dict[str, object]:
        return {
            "human_review_status": "PENDING",
            "record_kind": "LESSON_MINING_SELECTION_V1",
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "selection": self.selection.as_dict(),
        }

    def artifact_payloads(self) -> dict[str, bytes]:
        return {
            "qualification-sources.toml": self.source_manifest_raw,
            "source-validation.json": canonical_json_bytes(self.source_validation_dict()),
            "candidates.json": canonical_json_bytes(self.candidates_dict()),
            "selection.json": canonical_json_bytes(self.selection_dict()),
            "review-packet.json": canonical_json_bytes(self.review_packet.as_dict()),
        }

    def summary(self) -> dict[str, object]:
        return {
            "active_source_rows": list(self.active_source_rows),
            "candidate_count": self.candidate_count,
            "event_materially_distinct_count": self.event_materially_distinct_count,
            "detector_finding_count": sum(
                len(item.report.findings) for item in self.detector_executions
            ),
            "detector_invocation_status": "EXECUTED",
            "detector_opportunity_count": sum(
                len(item.opportunities) for item in self.detector_executions
            ),
            "detector_report_count": len(self.detector_executions),
            "human_accepted_count": 0,
            "human_review_status": "PENDING",
            "mandatory_source_counts": dict(self.review_packet.mandatory_source_counts),
            "outcome_conditioning_caveat": OUTCOME_CONDITIONING_CAVEAT_V1,
            "ready_for_human_review_count": self.ready_count,
            "review_packet_sha256": self.review_packet.packet_sha256,
            "review_selected_count": self.selection.selected_count,
            "review_shortfall_count": self.selection.shortfall_count,
            "source_manifest_sha256": self.source_manifest.manifest_sha256,
            "status": "READY_FOR_HUMAN_REVIEW" if self.ready_count else "PENDING",
        }


@dataclass(frozen=True, slots=True)
class LessonReviewSidecarV1:
    candidate_id: str
    candidate_digest: str
    mining_run_id: str
    decision: LessonReviewDecisionV1
    reviewer_id: str
    reviewer_reference: str
    reviewer_authority: ReviewerAuthorityV1
    rubric_version: str
    reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    created_at_utc: str
    superseded_review_id: str | None = None
    superseded_review_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "review candidate ID")
        if self.candidate_id != "lesson-candidate-" + self.candidate_digest:
            raise ValueError("review candidate ID and digest differ")
        _require_sha256(self.candidate_digest, "review candidate digest")
        if _RUN_ID.fullmatch(self.mining_run_id) is None:
            raise ValueError("review mining run ID is invalid")
        if not isinstance(self.decision, LessonReviewDecisionV1):
            raise TypeError("review decision is invalid")
        _require_text(self.reviewer_id, "reviewer identity")
        _require_text(self.reviewer_reference, "reviewer reference")
        if not isinstance(self.reviewer_authority, ReviewerAuthorityV1):
            raise TypeError("reviewer authority is invalid")
        _require_text(self.rubric_version, "review rubric version")
        if type(self.reasons) is not tuple or not self.reasons:
            raise ValueError("review sidecar requires at least one reason")
        canonical_reasons = tuple(sorted(set(self.reasons), key=lambda item: item.encode("utf-8")))
        if canonical_reasons != self.reasons:
            raise ValueError("review reasons must be unique and NFC-byte sorted")
        for reason in self.reasons:
            _require_text(reason, "review reason")
        if (
            type(self.reason_codes) is not tuple
            or not self.reason_codes
            or any(_IDENTIFIER.fullmatch(item) is None for item in self.reason_codes)
            or tuple(sorted(set(self.reason_codes))) != self.reason_codes
        ):
            raise ValueError("review reason codes must be sorted identifiers")
        _iso_utc(self.created_at_utc, "review timestamp")
        if (self.superseded_review_id is None) != (
            self.superseded_review_sha256 is None
        ):
            raise ValueError("superseded review ID and digest must both be present or absent")
        if self.superseded_review_id is not None:
            _require_text(self.superseded_review_id, "superseded review ID")
            _require_sha256(
                self.superseded_review_sha256,
                "superseded review digest",
            )
        if self.reviewer_authority is ReviewerAuthorityV1.AUTOMATION:
            if not self.reviewer_reference.startswith("automation:"):
                raise PermissionError("automation reviewer reference must use automation:")
            if self.decision not in {
                LessonReviewDecisionV1.PENDING,
                LessonReviewDecisionV1.READY_FOR_HUMAN_REVIEW,
            }:
                raise PermissionError("automation cannot submit a human review decision")
        else:
            if not self.reviewer_reference.startswith(("local:", "auth:")):
                raise PermissionError(
                    "human decisions require a local: or auth: reviewer reference"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "created_at_utc": self.created_at_utc,
            "decision": self.decision.value,
            "mining_run_id": self.mining_run_id,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "record_kind": "LESSON_REVIEW_SIDECAR_V1",
            "reviewer_authority": self.reviewer_authority.value,
            "reviewer_id": self.reviewer_id,
            "reviewer_reference": self.reviewer_reference,
            "rubric_version": self.rubric_version,
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "superseded_review_id": self.superseded_review_id,
            "superseded_review_sha256": self.superseded_review_sha256,
            "timestamp_metadata": {
                "clock": "CALLER_SUPPLIED",
                "precision": "MICROSECONDS_OR_COARSER",
                "timezone": "UTC",
            },
        }

    @property
    def sidecar_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def review_id(self) -> str:
        return "lesson-review-" + self.sidecar_sha256

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LessonReviewSidecarV1:
        expected = {
            "candidate_digest",
            "candidate_id",
            "created_at_utc",
            "decision",
            "mining_run_id",
            "reason_codes",
            "reasons",
            "record_kind",
            "reviewer_authority",
            "reviewer_id",
            "reviewer_reference",
            "rubric_version",
            "schema_version",
            "superseded_review_id",
            "superseded_review_sha256",
            "timestamp_metadata",
        }
        if set(payload) != expected:
            raise ValueError("lesson review sidecar fields differ")
        if (
            payload["record_kind"] != "LESSON_REVIEW_SIDECAR_V1"
            or payload["schema_version"] != LESSON_REVIEW_SCHEMA_VERSION_V1
            or payload["timestamp_metadata"]
            != {
                "clock": "CALLER_SUPPLIED",
                "precision": "MICROSECONDS_OR_COARSER",
                "timezone": "UTC",
            }
        ):
            raise ValueError("lesson review sidecar schema metadata differs")
        reasons = payload["reasons"]
        reason_codes = payload["reason_codes"]
        if not isinstance(reasons, list) or not isinstance(reason_codes, list):
            raise TypeError("lesson review reasons must be arrays")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            candidate_digest=str(payload["candidate_digest"]),
            mining_run_id=str(payload["mining_run_id"]),
            decision=LessonReviewDecisionV1(str(payload["decision"])),
            reviewer_id=str(payload["reviewer_id"]),
            reviewer_reference=str(payload["reviewer_reference"]),
            reviewer_authority=ReviewerAuthorityV1(str(payload["reviewer_authority"])),
            rubric_version=str(payload["rubric_version"]),
            reasons=tuple(str(item) for item in reasons),
            reason_codes=tuple(str(item) for item in reason_codes),
            created_at_utc=str(payload["created_at_utc"]),
            superseded_review_id=(
                None
                if payload["superseded_review_id"] is None
                else str(payload["superseded_review_id"])
            ),
            superseded_review_sha256=(
                None
                if payload["superseded_review_sha256"] is None
                else str(payload["superseded_review_sha256"])
            ),
        )


@dataclass(frozen=True, slots=True)
class LessonBuildProposalV1:
    candidate_id: str
    candidate_digest: str
    mining_run_id: str
    technical_status: TechnicalCandidateStatusV1
    human_acceptance_status: str
    source_ancestry_sha256: str
    candidate_snapshot_sha256: str
    created_at_utc: str

    def __post_init__(self) -> None:
        if self.candidate_id != "lesson-candidate-" + self.candidate_digest:
            raise ValueError("lesson build candidate ID and digest differ")
        _require_sha256(self.candidate_digest, "lesson build candidate digest")
        if _RUN_ID.fullmatch(self.mining_run_id) is None:
            raise ValueError("lesson build mining run ID is invalid")
        if not isinstance(self.technical_status, TechnicalCandidateStatusV1):
            raise TypeError("lesson build technical status is invalid")
        if self.technical_status is not TechnicalCandidateStatusV1.READY_FOR_HUMAN_REVIEW:
            raise ValueError("only technically ready candidates are buildable")
        if self.human_acceptance_status not in {"PENDING", "ACCEPTED"}:
            raise ValueError("lesson build human acceptance status is invalid")
        _require_sha256(self.source_ancestry_sha256, "lesson build ancestry digest")
        _require_sha256(self.candidate_snapshot_sha256, "lesson build snapshot digest")
        _iso_utc(self.created_at_utc, "lesson build timestamp")

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_policy_id": "MINED_LESSON_BLIND_ASSESSMENT_V1",
            "build_status": "BUILT_TECHNICAL_PROPOSAL",
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "candidate_snapshot_sha256": self.candidate_snapshot_sha256,
            "created_at_utc": self.created_at_utc,
            "human_acceptance_status": self.human_acceptance_status,
            "mining_run_id": self.mining_run_id,
            "outcome_conditioning_caveat": OUTCOME_CONDITIONING_CAVEAT_V1,
            "record_kind": "LESSON_BUILD_PROPOSAL_V1",
            "reveal_policy_id": "MINED_LESSON_AUTHORIZED_REVEAL_V1",
            "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
            "source_ancestry_sha256": self.source_ancestry_sha256,
            "technical_status": self.technical_status.value,
        }

    @property
    def proposal_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def lesson_id(self) -> str:
        return "mined-lesson-proposal-" + self.proposal_sha256

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LessonBuildProposalV1:
        if (
            payload.get("record_kind") != "LESSON_BUILD_PROPOSAL_V1"
            or payload.get("schema_version") != LESSON_REVIEW_SCHEMA_VERSION_V1
            or payload.get("build_status") != "BUILT_TECHNICAL_PROPOSAL"
            or payload.get("assessment_policy_id")
            != "MINED_LESSON_BLIND_ASSESSMENT_V1"
            or payload.get("reveal_policy_id")
            != "MINED_LESSON_AUTHORIZED_REVEAL_V1"
            or payload.get("outcome_conditioning_caveat")
            != OUTCOME_CONDITIONING_CAVEAT_V1
        ):
            raise ValueError("lesson build proposal schema metadata differs")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            candidate_digest=str(payload["candidate_digest"]),
            mining_run_id=str(payload["mining_run_id"]),
            technical_status=TechnicalCandidateStatusV1(str(payload["technical_status"])),
            human_acceptance_status=str(payload["human_acceptance_status"]),
            source_ancestry_sha256=str(payload["source_ancestry_sha256"]),
            candidate_snapshot_sha256=str(payload["candidate_snapshot_sha256"]),
            created_at_utc=str(payload["created_at_utc"]),
        )


def load_qualification_source_manifest(path: Path) -> tuple[bytes, QualificationSourcesManifestV1]:
    raw = path.read_bytes()
    manifest = QualificationSourcesManifestV1.from_toml_bytes(raw)
    if manifest.manifest_sha256 != WO33A1_SOURCE_MANIFEST_SHA256_V1:
        raise ValueError("source matrix is not the exact committed WO33-A1 manifest")
    return raw, manifest


def _materialize_and_prepare_sources(
    *,
    repository: Path,
    manifest: QualificationSourcesManifestV1,
    materialization_root: Path,
    active_source_rows: Sequence[str] | None = None,
) -> tuple[
    tuple[SourceMaterializationV1, ...],
    tuple[_PreparedSourceEvidenceV1, ...],
]:
    """Copy exact source bytes into an audit root and verify every replay identity."""

    from kirby2.historical.lesson_runner import run_historical_lesson
    from kirby2.multivenue.replay import replay_multivenue_recording
    from kirby2.observability.replay import replay_observability_recording
    from kirby2.scenario_lang.commands import inspect_scenario_source

    selected = (
        tuple(row.row_id for row in manifest.rows)
        if active_source_rows is None
        else tuple(active_source_rows)
    )
    if len(selected) != len(set(selected)):
        raise ValueError("active qualification source rows are duplicated")
    if any(row_id not in {row.row_id for row in manifest.rows} for row_id in selected):
        raise ValueError("active qualification source row is outside the fixed matrix")
    materialization_root.mkdir(parents=True, exist_ok=True)
    mirror = materialization_root / "repository"
    mirror.mkdir()
    results: list[SourceMaterializationV1] = []
    prepared: list[_PreparedSourceEvidenceV1] = []
    runtime = MiningDetectorRuntimeV1()
    for row_id in selected:
        row = manifest.row(row_id)
        source_relative = Path(str(row.source["example_path"]))
        source_original = repository / source_relative
        source_raw = source_original.read_bytes()
        if (
            len(source_raw) != row.source["raw_bytes_length"]
            or hashlib.sha256(source_raw).hexdigest() != row.source["raw_sha256"]
        ):
            raise ValueError(f"{row_id} example source bytes differ")
        source_copy = mirror / source_relative
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        source_copy.write_bytes(source_raw)

        native_payload: dict[str, object] | None = None
        native_raw: bytes | None = None
        native_path_value = str(row.configuration["native_payload_path"])
        if native_path_value not in {_NOT_APPLICABLE, "WO31_I1_IMMUTABLE_PLAN_ARTIFACT"}:
            native_relative = Path(native_path_value)
            native_original = repository / native_relative
            native_raw = native_original.read_bytes()
            if (
                len(native_raw) != row.configuration["native_payload_raw_bytes_length"]
                or hashlib.sha256(native_raw).hexdigest()
                != row.configuration["native_payload_raw_sha256"]
            ):
                raise ValueError(f"{row_id} native payload bytes differ")
            native_copy = mirror / native_relative
            native_copy.parent.mkdir(parents=True, exist_ok=True)
            native_copy.write_bytes(native_raw)
            native_payload = json.loads(native_raw)

        report = inspect_scenario_source(source_copy)
        artifact = report.artifact
        if not report.passed or artifact is None:
            raise ValueError(f"{row_id} materialized source does not compile")
        if (
            artifact.compiled_artifact_digest != row.source["compiled_artifact_sha256"]
            or artifact.semantic_plan_digest != row.source["semantic_plan_sha256"]
            or artifact.source_bundle_digest != row.source["source_bundle_sha256"]
            or artifact.adapter_id != row.source["example_adapter_id"]
            or artifact.adapter_version != row.source["example_adapter_version"]
            or artifact.target_kind.value != row.source["example_target_kind"]
            or artifact.seed_policy.selected_root_seed
            != row.source["example_selected_root_seed"]
        ):
            raise ValueError(f"{row_id} materialized compiled identity differs")

        native_run_digest = str(row.identity["expected_native_run_digest"])
        replay_digest = str(row.identity["expected_replay_digest"])
        final_state = str(row.identity["expected_final_state_sha256"])
        evidence_units = 0
        detector_status = "NO_COMPLETE_POLICY_INPUT"
        detector_input_sha256 = hashlib.sha256(source_raw).hexdigest()
        opportunities_by_detector: dict[
            str, tuple[DetectorOpportunityV1, ...]
        ] = {}
        timeline: object | None = None
        if row_id in {"quiet", "event"}:
            parent_relative = Path(str(row.provenance["parent_artifact_path"]))
            parent_raw = (repository / parent_relative).read_bytes()
            if hashlib.sha256(parent_raw).hexdigest() != row.provenance["parent_artifact_sha256"]:
                raise ValueError(f"{row_id} immutable WO31-I1 proof digest differs")
            parent_copy = mirror / parent_relative
            parent_copy.parent.mkdir(parents=True, exist_ok=True)
            parent_copy.write_bytes(parent_raw)
            proof_payload = json.loads(parent_raw)
            matches = [
                item
                for item in proof_payload.get("run_proofs", [])
                if item.get("candidate_id") == row.identity["qualification_profile_id"]
                and item.get("partition") == "QUALIFICATION"
                and item.get("root_seed") == 3_102_000
            ]
            embedded = json.loads(str(row.configuration["bytes_json"]))
            if len(matches) != 1 or matches[0] != embedded:
                raise ValueError(f"{row_id} immutable proof selection differs")
            proof = matches[0]
            if (
                proof["run_digest"] != native_run_digest
                or proof["run_digest"] != replay_digest
                or proof["replay_verification_status"] != "PASS"
            ):
                raise ValueError(f"{row_id} immutable replay identity differs")
            review_original = (repository / parent_relative).with_name("review-source.json")
            review_raw = review_original.read_bytes()
            review_copy = parent_copy.with_name("review-source.json")
            review_copy.write_bytes(review_raw)
            review_payload = json.loads(review_raw)
            review_matches = [
                item
                for item in review_payload.get("runs", [])
                if item.get("candidate_id") == row.identity["qualification_profile_id"]
                and item.get("root_seed") == 3_102_000
            ]
            review_sha = hashlib.sha256(review_raw).hexdigest()
            if (
                len(review_matches) != 1
                or f"review_source_sha256={review_sha}"
                not in str(row.provenance["parent_selector"])
            ):
                raise ValueError(f"{row_id} immutable review source selection differs")
            evidence_units = len(review_matches[0].get("samples", []))
            detector_input_sha256 = sha256_json(review_matches[0])
            detector_status = "INCOMPLETE_EVENT_STREAM_NO_POLICY_ENUMERATION"
        else:
            native = artifact.plan_envelope.payload
            if artifact.run_identity_digest != native_run_digest:
                raise ValueError(f"{row_id} native run identity differs")
            if row_id == "hidden":
                replay = replay_observability_recording(native)
                actual_replay = native.sha256()
                actual_final = replay.venue.state_sha256()
                evidence_units = len(
                    (native_payload or {})
                    .get("expected_observable_feed", {})
                    .get("events", [])
                )
                detector_input_sha256 = hashlib.sha256(native_raw or b"").hexdigest()
                detector_status = "SOURCE_SHORTER_THAN_ONE_OBSERVABLE_BIN"
            elif row_id == "fragmented":
                replay = replay_multivenue_recording(native)
                actual_replay = native.sha256()
                actual_final = replay.coordinator.state_sha256()
                evidence_units = len((native_payload or {}).get("expected_events", []))
                detector_input_sha256 = hashlib.sha256(native_raw or b"").hexdigest()
                detector_status = "SOURCE_SHORTER_THAN_ONE_OBSERVABLE_BIN"
            else:
                session = run_historical_lesson(native)
                replay = None
                actual_replay = session.run.replay_sha256()
                actual_final = _NOT_APPLICABLE
                evidence_units = len(session.run.commands)
                timeline, opportunities_by_detector = (
                    _historical_timeline_and_opportunities(session.run, runtime)
                )
                detector_input_sha256 = actual_replay
                detector_status = "EXACT_RECONSTRUCTION_EVENT_STREAM_ENUMERATED"
            if replay is not None and not replay.passed:
                raise ValueError(f"{row_id} deterministic recording replay failed")
            if actual_replay != replay_digest or actual_final != final_state:
                raise ValueError(f"{row_id} replay or final-state digest differs")
        if evidence_units <= 0:
            raise ValueError(f"{row_id} materialization exposed no evidence units")
        materialization = SourceMaterializationV1(
                row_id=row.row_id,
                stratum=row.stratum,
                source_id=str(row.identity["source_id"]),
                source_sha256=str(row.identity["source_sha256"]),
                example_path=(Path("repository") / source_relative).as_posix(),
                example_raw_sha256=str(row.source["raw_sha256"]),
                compiled_artifact_sha256=str(row.source["compiled_artifact_sha256"]),
                native_run_digest=native_run_digest,
                replay_digest=replay_digest,
                final_state_sha256=final_state,
                evidence_unit_count=evidence_units,
                capability_contract_sha256=str(
                    row.capabilities["adapter_contract_sha256"]
                ),
                detector_adapter_status=detector_status,
                detector_input_sha256=detector_input_sha256,
                detector_opportunity_count=sum(
                    len(items) for items in opportunities_by_detector.values()
                ),
            )
        results.append(materialization)
        prepared.append(
            _PreparedSourceEvidenceV1(
                materialization=materialization,
                opportunities_by_detector=opportunities_by_detector,
                timeline=timeline,
            )
        )
    return tuple(results), tuple(prepared)


def materialize_and_verify_sources(
    *,
    repository: Path,
    manifest: QualificationSourcesManifestV1,
    materialization_root: Path,
    active_source_rows: Sequence[str] | None = None,
) -> tuple[SourceMaterializationV1, ...]:
    materializations, _prepared = _materialize_and_prepare_sources(
        repository=repository,
        manifest=manifest,
        materialization_root=materialization_root,
        active_source_rows=active_source_rows,
    )
    return materializations


def _evidence_class(row: QualificationSourceRowV1) -> EvidenceClassV1:
    return EvidenceClassV1(str(row.identity["evidence_class"]))


def _source_capability_inventory(
    row: QualificationSourceRowV1,
    detector: DetectorProjectionV1,
) -> SourceCapabilityInventoryV1:
    del detector
    source_identity = SourceIdentityV1(
        SourceKindV1(str(row.identity["source_kind"])),
        str(row.identity["source_id"]),
        str(row.identity["source_sha256"]),
    )
    evidence_digest = str(row.capabilities["adapter_contract_sha256"])
    records = tuple(
        CapabilityRecordRowV1(
            str(capability),
            (
                CapabilityEvidenceReferenceV1(
                    CapabilityEvidenceKindV1.SOURCE_MANIFEST,
                    f"qualification-sources:{row.row_id}:{str(capability).lower()}",
                    evidence_digest,
                ),
            ),
        )
        for capability in row.capabilities["provided"]
    )
    return SourceCapabilityInventoryV1(source_identity, _evidence_class(row), records)


@dataclass(frozen=True, slots=True)
class _HistoricalEventV1:
    timestamp_us: int
    source_sequence: int
    event_type: str
    data: dict[str, object]

    @property
    def event_id(self) -> str:
        return (
            f"historical:{self.event_type.lower()}:"
            f"{self.source_sequence:06d}"
        )

    def reference(self) -> MiningEventReferenceV1:
        return MiningEventReferenceV1(
            self.event_id,
            self.timestamp_us,
            self.source_sequence,
        )


@dataclass(frozen=True, slots=True)
class _HistoricalStateV1:
    edge_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    bid_top: int
    ask_top: int

    @property
    def mid_x2(self) -> int | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        return self.best_bid_ticks + self.best_ask_ticks

    @property
    def spread_ticks(self) -> int | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        return self.best_ask_ticks - self.best_bid_ticks


@dataclass(frozen=True, slots=True)
class _HistoricalTimelineV1:
    source_end_us: int
    events: tuple[_HistoricalEventV1, ...]
    states: tuple[_HistoricalStateV1, ...]

    def references(
        self,
        start_us: int,
        end_us: int,
        *,
        event_types: frozenset[str] | None = None,
    ) -> tuple[MiningEventReferenceV1, ...]:
        return tuple(
            event.reference()
            for event in self.events
            if start_us <= event.timestamp_us < end_us
            and (event_types is None or event.event_type in event_types)
        )

    def aggressive_flow(self, start_us: int, end_us: int) -> tuple[int, int]:
        buy = 0
        sell = 0
        for event in self.events:
            if not start_us <= event.timestamp_us < end_us:
                continue
            if event.event_type != "TRADE":
                continue
            side = event.data.get("taker_side")
            quantity = event.data.get("quantity")
            if side not in {"buy", "sell"} or type(quantity) is not int:
                raise ValueError("historical trade evidence is incomplete")
            if side == "buy":
                buy += quantity
            else:
                sell += quantity
        return buy, sell

    def state_at(self, timestamp_us: int, *, strictly_before: bool) -> _HistoricalStateV1 | None:
        if timestamp_us < 0:
            return None
        index = (
            (timestamp_us - 1) // 100_000
            if strictly_before
            else timestamp_us // 100_000
        )
        if index < 0 or index >= len(self.states):
            return None
        return self.states[index]


def _historical_timeline(run: object) -> _HistoricalTimelineV1:
    duration_us = getattr(run, "duration_us")
    commands = getattr(run, "commands")
    exchange_events = getattr(run, "exchange_events")
    if type(duration_us) is not int or duration_us <= 0 or duration_us % 100_000:
        raise ValueError("historical detector source does not have complete 100ms bins")
    sequence_times: dict[int, int] = {}
    for command in commands:
        start = command.exchange_event_start
        end = command.exchange_event_end
        if start is None or end is None:
            if command.applied:
                raise ValueError("applied historical command lacks exchange events")
            continue
        for sequence in range(start, end + 1):
            if sequence in sequence_times:
                raise ValueError("historical exchange event has multiple command times")
            sequence_times[sequence] = command.simulation_time_us
    events = tuple(
        sorted(
            (
                _HistoricalEventV1(
                    timestamp_us=sequence_times[event.sequence],
                    source_sequence=event.sequence,
                    event_type=event.event_type.value,
                    data=dict(event.data),
                )
                for event in exchange_events
            ),
            key=lambda item: (item.timestamp_us, item.source_sequence),
        )
    )
    if len(events) != len(exchange_events):
        raise ValueError("historical exchange event timing is incomplete")

    orders: dict[str, tuple[str, int, int]] = {}
    states: list[_HistoricalStateV1] = []
    event_index = 0
    for edge_us in range(0, duration_us + 1, 100_000):
        while event_index < len(events) and events[event_index].timestamp_us < edge_us:
            event = events[event_index]
            data = event.data
            order_id = data.get("order_id")
            if event.event_type == "ORDER_ADDED":
                if not isinstance(order_id, str):
                    raise ValueError("historical order add lacks identity")
                orders[order_id] = (
                    str(data["side"]),
                    int(data["price_ticks"]),
                    int(data["remaining_quantity"]),
                )
            elif event.event_type in {"PARTIAL_FILL", "FULL_FILL"}:
                if isinstance(order_id, str) and order_id in orders:
                    side, price, _quantity = orders[order_id]
                    remaining = int(data["remaining_quantity"])
                    if remaining:
                        orders[order_id] = (side, price, remaining)
                    else:
                        orders.pop(order_id)
            elif event.event_type in {"ORDER_CANCELLED", "ORDER_EXPIRED"}:
                if isinstance(order_id, str):
                    orders.pop(order_id, None)
            event_index += 1
        by_side: dict[str, dict[int, int]] = {
            "buy": defaultdict(int),
            "sell": defaultdict(int),
        }
        for side, price, quantity in orders.values():
            by_side[side][price] += quantity
        best_bid = max(by_side["buy"], default=None)
        best_ask = min(by_side["sell"], default=None)
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            raise ValueError("historical left-limit projection is crossed")
        states.append(
            _HistoricalStateV1(
                edge_us=edge_us,
                best_bid_ticks=best_bid,
                best_ask_ticks=best_ask,
                bid_top=0 if best_bid is None else by_side["buy"][best_bid],
                ask_top=0 if best_ask is None else by_side["sell"][best_ask],
            )
        )
    return _HistoricalTimelineV1(duration_us, events, tuple(states))


def _historical_opportunity(
    timeline: _HistoricalTimelineV1,
    runtime: MiningDetectorRuntimeV1,
    *,
    detector_id: str,
    opportunity_suffix: str,
    active_start_us: int,
    activation_us: int,
    direction: CandidateDirectionV1,
    side: CandidateSideV1,
    measurements: Mapping[str, object],
    event_types: frozenset[str] | None = None,
) -> DetectorOpportunityV1 | None:
    if activation_us >= timeline.source_end_us:
        return None
    references = timeline.references(
        active_start_us,
        activation_us,
        event_types=event_types,
    )
    if not references:
        return None
    threshold = runtime.threshold_manifest.detector(detector_id)
    return DetectorOpportunityV1(
        detector_id=detector_id,
        opportunity_id=f"historical:{detector_id.lower()}:{opportunity_suffix}",
        sampling_unit=str(threshold["sampling_unit"]),
        source_start_us=0,
        source_end_us=timeline.source_end_us,
        active_start_us=active_start_us,
        activation_us=activation_us,
        direction=direction,
        side=side,
        venue="CONSOLIDATED",
        price=_NOT_APPLICABLE,
        witness_kind=str(threshold["witness_kind"]),
        witness_ids=(),
        measurements=tuple(
            DetectorMeasurementV1(name, value)  # type: ignore[arg-type]
            for name, value in measurements.items()
        ),
        contributing_events=references,
    )


def _historical_timeline_and_opportunities(
    run: object,
    runtime: MiningDetectorRuntimeV1,
) -> tuple[_HistoricalTimelineV1, dict[str, tuple[DetectorOpportunityV1, ...]]]:
    timeline = _historical_timeline(run)
    opportunities: dict[str, list[DetectorOpportunityV1]] = defaultdict(list)

    group_flow = tuple(
        timeline.aggressive_flow(group * 1_000_000, (group + 1) * 1_000_000)
        for group in range(timeline.source_end_us // 1_000_000)
    )
    for group in range(20, len(group_flow)):
        buy, sell = group_flow[group]
        signed = buy - sell
        if signed == 0:
            continue
        direction = (
            CandidateDirectionV1.BUY
            if signed > 0
            else CandidateDirectionV1.SELL
        )
        candidate = _historical_opportunity(
            timeline,
            runtime,
            detector_id="AGGRESSIVE_FLOW_BURST",
            opportunity_suffix=f"group-{group:04d}",
            active_start_us=group * 1_000_000,
            activation_us=(group + 1) * 1_000_000,
            direction=direction,
            side=CandidateSideV1(direction.value),
            measurements={
                "active_buy_quantity": buy,
                "active_sell_quantity": sell,
                "group_duration_us": 1_000_000,
                "trailing_group_volumes": tuple(
                    prior_buy + prior_sell
                    for prior_buy, prior_sell in group_flow[group - 20 : group]
                ),
            },
            event_types=frozenset({"TRADE"}),
        )
        if candidate is not None:
            opportunities["AGGRESSIVE_FLOW_BURST"].append(candidate)

    mids = tuple(state.mid_x2 for state in timeline.states)
    for breakout_index in range(50, len(mids)):
        breakout_mid = mids[breakout_index]
        prior = mids[breakout_index - 50 : breakout_index]
        if breakout_mid is None or any(value is None for value in prior):
            continue
        exact_prior = tuple(int(value) for value in prior if value is not None)
        for direction in (CandidateDirectionV1.BUY, CandidateDirectionV1.SELL):
            orientation = 1 if direction is CandidateDirectionV1.BUY else -1
            prior_extreme = max(exact_prior) if orientation == 1 else min(exact_prior)
            if orientation * (breakout_mid - prior_extreme) < 2:
                continue
            beyond: list[int] = []
            return_index: int | None = None
            for index in range(
                breakout_index,
                min(len(mids), breakout_index + 31),
            ):
                current = mids[index]
                if current is None:
                    break
                if orientation * (current - prior_extreme) >= 2:
                    beyond.append(index)
                if orientation * (prior_extreme - current) >= 2:
                    return_index = index
                    break
            if return_index is None or not beyond:
                continue
            candidate = _historical_opportunity(
                timeline,
                runtime,
                detector_id="FAILED_BREAKOUT",
                opportunity_suffix=(
                    f"bin-{breakout_index:04d}-{direction.value.lower()}-"
                    f"return-{return_index:04d}"
                ),
                active_start_us=breakout_index * 100_000,
                activation_us=return_index * 100_000,
                direction=direction,
                side=CandidateSideV1.NOT_APPLICABLE,
                measurements={
                    "first_breakout_mid_x2": breakout_mid,
                    "last_beyond_extreme_elapsed_us": (
                        beyond[-1] - breakout_index
                    )
                    * 100_000,
                    "prior_extreme_lookback_us": 5_000_000,
                    "prior_extreme_mid_x2": prior_extreme,
                    "return_elapsed_us": (
                        return_index - breakout_index
                    )
                    * 100_000,
                    "return_mid_x2": mids[return_index],
                },
            )
            if candidate is not None:
                opportunities["FAILED_BREAKOUT"].append(candidate)

    spreads = tuple(state.spread_ticks for state in timeline.states)
    for start_index, starting_spread in enumerate(spreads):
        if starting_spread is None or starting_spread > 2:
            continue
        for expanded_index in range(
            start_index,
            min(len(spreads), start_index + 6),
        ):
            expanded_spread = spreads[expanded_index]
            if expanded_spread is None or expanded_spread < 4:
                continue
            end_index = expanded_index + 5
            if end_index >= len(spreads) or any(
                value is None or value < 4
                for value in spreads[expanded_index : end_index + 1]
            ):
                continue
            candidate = _historical_opportunity(
                timeline,
                runtime,
                detector_id="SPREAD_EXPANSION",
                opportunity_suffix=(
                    f"start-{start_index:04d}-expand-{expanded_index:04d}"
                ),
                active_start_us=start_index * 100_000,
                activation_us=end_index * 100_000,
                direction=CandidateDirectionV1.NOT_APPLICABLE,
                side=CandidateSideV1.NOT_APPLICABLE,
                measurements={
                    "expanded_spread_ticks": expanded_spread,
                    "persistence_us": 500_000,
                    "starting_spread_ticks": starting_spread,
                    "transition_elapsed_us": (
                        expanded_index - start_index
                    )
                    * 100_000,
                },
            )
            if candidate is not None:
                opportunities["SPREAD_EXPANSION"].append(candidate)
            break

    for start_index in range(0, min(101, len(mids))):
        initial_end_index = start_index + 100
        forward_end_index = initial_end_index + 50
        if forward_end_index >= len(mids):
            break
        start_mid = mids[start_index]
        initial_mid = mids[initial_end_index]
        forward_mid = mids[forward_end_index]
        if start_mid is None or initial_mid is None or forward_mid is None:
            continue
        initial_buy, initial_sell = timeline.aggressive_flow(
            start_index * 100_000,
            initial_end_index * 100_000,
        )
        forward_buy, forward_sell = timeline.aggressive_flow(
            initial_end_index * 100_000,
            forward_end_index * 100_000,
        )
        initial_total = initial_buy + initial_sell
        forward_total = forward_buy + forward_sell
        initial_imbalance = (
            0
            if initial_total == 0
            else round_div_even(
                (initial_buy - initial_sell) * 1_000_000,
                initial_total,
            )
        )
        forward_imbalance = (
            0
            if forward_total == 0
            else round_div_even(
                (forward_buy - forward_sell) * 1_000_000,
                forward_total,
            )
        )
        for direction in (CandidateDirectionV1.BUY, CandidateDirectionV1.SELL):
            candidate = _historical_opportunity(
                timeline,
                runtime,
                detector_id="MOMENTUM_EXHAUSTION",
                opportunity_suffix=(
                    f"start-{start_index:04d}-{direction.value.lower()}"
                ),
                active_start_us=start_index * 100_000,
                activation_us=forward_end_index * 100_000,
                direction=direction,
                side=CandidateSideV1(direction.value),
                measurements={
                    "additional_mid_x2_movement": forward_mid - initial_mid,
                    "forward_aggressive_flow_imbalance_ppm": forward_imbalance,
                    "forward_window_us": 5_000_000,
                    "initial_aggressive_flow_imbalance_ppm": initial_imbalance,
                    "initial_mid_x2_movement": initial_mid - start_mid,
                    "initial_window_us": 10_000_000,
                },
                event_types=frozenset({"TRADE"}),
            )
            if candidate is not None:
                opportunities["MOMENTUM_EXHAUSTION"].append(candidate)

    return timeline, {
        detector_id: tuple(sorted(items, key=lambda item: item.sort_key))
        for detector_id, items in opportunities.items()
    }


def _source_ancestry(
    row: QualificationSourceRowV1,
    source: SourceMaterializationV1,
) -> SourceAncestryV1:
    checkpoint_digest = str(row.provenance["checkpoint_sha256"])
    event_prefix = str(row.provenance["event_prefix_sha256"])
    return SourceAncestryV1(
        source_kind=SourceKindV1(str(row.identity["source_kind"])),
        source_id=source.source_id,
        source_sha256=source.source_sha256,
        checkpoint_id=(
            None
            if checkpoint_digest == _NOT_APPLICABLE
            else f"qualification-checkpoint-{row.row_id}"
        ),
        checkpoint_sha256=(
            None if checkpoint_digest == _NOT_APPLICABLE else checkpoint_digest
        ),
        event_prefix_sha256=(
            None if event_prefix == _NOT_APPLICABLE else event_prefix
        ),
        parent_source_ancestry_sha256=None,
    )


def _execute_detector_matrix(
    manifest: QualificationSourcesManifestV1,
    prepared: Sequence[_PreparedSourceEvidenceV1],
) -> tuple[DetectorExecutionV1, ...]:
    runtime = MiningDetectorRuntimeV1()
    executions: list[DetectorExecutionV1] = []
    for item in prepared:
        source = item.materialization
        row = manifest.row(source.row_id)
        ancestry = _source_ancestry(row, source)
        for detector_id in OPERATIONAL_DETECTOR_IDS_V1:
            detector = DetectorProjectionV1(
                detector_id,
                1,
                runtime.threshold_manifest.detector_threshold_sha256(detector_id),
            )
            inventory = _source_capability_inventory(row, detector)
            opportunities = item.opportunities_by_detector.get(detector_id, ())
            report = runtime.run(
                detector_id,
                inventory,
                ancestry,
                opportunities,
            )
            executions.append(
                DetectorExecutionV1(
                    row_id=row.row_id,
                    detector_id=detector_id,
                    adapter_status=(
                        "OPPORTUNITIES_ENUMERATED"
                        if opportunities
                        else source.detector_adapter_status
                    ),
                    source_evidence_sha256=source.detector_input_sha256,
                    opportunities=opportunities,
                    report=report,
                )
            )
    return tuple(executions)


def _measurement_feature_tokens(
    opportunity: DetectorOpportunityV1,
    report_finding,
) -> tuple[str, ...]:
    tokens: list[str] = []
    for prefix, measurements in (
        ("input", opportunity.measurements),
        ("derived", report_finding.derived_measurements),
    ):
        for measurement in measurements:
            value = measurement.value
            if type(value) is tuple:
                tokens.extend(
                    observable_feature_token_v1(
                        "DETECTOR_MEASUREMENT",
                        f"{prefix}_{measurement.name}_{index}",
                        item,
                    )
                    for index, item in enumerate(value)
                )
            else:
                tokens.append(
                    observable_feature_token_v1(
                        "DETECTOR_MEASUREMENT",
                        f"{prefix}_{measurement.name}",
                        value,
                    )
                )
    return tuple(sorted(set(tokens), key=lambda item: item.encode("utf-8")))


def _threshold_value(runtime: MiningDetectorRuntimeV1, detector_id: str, name: str) -> int:
    row = runtime.threshold_manifest.detector(detector_id)
    matches = [item for item in row["thresholds"] if item.get("name") == name]
    if len(matches) != 1 or type(matches[0].get("value")) is not int:
        raise ValueError(f"detector threshold {detector_id}/{name} is unavailable")
    return int(matches[0]["value"])


def _finding_difficulty(
    runtime: MiningDetectorRuntimeV1,
    opportunity: DetectorOpportunityV1,
    finding,
    recipe: CandidateRecipeV1,
    evidence_class: EvidenceClassV1,
    hidden_liquidity_relevant: bool,
    feature_count: int,
):
    values = opportunity.measurement_map
    if opportunity.detector_id == "SPREAD_EXPANSION":
        signal = and_legibility_ppm(
            (
                upper_bound_legibility_ppm(
                    int(values["starting_spread_ticks"]),
                    _threshold_value(
                        runtime,
                        opportunity.detector_id,
                        "starting_spread",
                    ),
                ),
                lower_bound_legibility_ppm(
                    int(values["expanded_spread_ticks"]),
                    _threshold_value(
                        runtime,
                        opportunity.detector_id,
                        "expanded_spread",
                    ),
                ),
                upper_bound_legibility_ppm(
                    int(values["transition_elapsed_us"]),
                    _threshold_value(
                        runtime,
                        opportunity.detector_id,
                        "transition_window",
                    ),
                ),
            )
        )
        persistence = int(values["persistence_us"])
        required_persistence = _threshold_value(
            runtime,
            opportunity.detector_id,
            "persistence",
        )
        duration = lower_bound_legibility_ppm(
            persistence,
            required_persistence,
        )
    else:
        raise RuntimeError(
            "a detector emitted a finding without a preregistered difficulty adapter: "
            + opportunity.detector_id
        )
    return build_difficulty_projection(
        signal_legibility_ppm=signal,
        duration_legibility_ppm=duration,
        conflict_ppm=None,
        reaction_us=finding.bounds.post_end_us - finding.bounds.activation_us,
        spread_ticks=recipe.activation_spread_ticks,
        latency_us=None,
        three_level_depth=None,
        venue_count=(1 if recipe.activation_spread_ticks is not None else None),
        hidden_liquidity_relevant=hidden_liquidity_relevant,
        feature_count=feature_count,
        evidence_class=evidence_class,
    )


def _build_runtime_candidate(
    *,
    manifest: QualificationSourcesManifestV1,
    source: SourceMaterializationV1,
    execution: DetectorExecutionV1,
    opportunity: DetectorOpportunityV1,
    finding,
    recipe: CandidateRecipeV1,
) -> LessonCandidateV1:
    runtime = MiningDetectorRuntimeV1()
    row = manifest.row(source.row_id)
    ancestry = _source_ancestry(row, source)
    detector = finding.detector
    inventory = _source_capability_inventory(row, detector)
    support = DETECTOR_REGISTRY_V1.assess(
        detector.detector_id,
        detector.version,
        detector.threshold_sha256,
        inventory,
    )
    if support.capability_record is None:
        raise ValueError("emitted detector finding lacks capability evidence")
    if (
        finding.source_ancestry_sha256 != ancestry.sha256
        or finding.capability_record_sha256 != support.capability_record.sha256
        or finding.opportunity_sha256 != opportunity.sha256
        or finding.finding_sha256 != recipe.finding_sha256
        or execution.report.report_sha256 != recipe.detector_report_sha256
    ):
        raise ValueError("candidate recipe is not bound to its runtime finding")
    feature_tokens = _measurement_feature_tokens(opportunity, finding)
    event_tokens = tuple(
        canonical_event_token_v1(
            event.event_id.split(":", 2)[1].upper(),
            "NONE",
            None,
            None,
            None,
        )
        for event in opportunity.contributing_events
    )
    observable = ObservableFeatureSummaryV1(
        feature_tokens=feature_tokens,
        regime_signature=RegimeSignatureV1(
            phase=recipe.phase,
            regime_id=f"QUALIFICATION_{source.row_id.upper()}_RUNTIME",
            volume_band="NOT_APPLICABLE",
            liquidity_band="NOT_APPLICABLE",
            spread_band=spread_band_v1(recipe.activation_spread_ticks),
        ),
        event_five_grams=event_five_grams_v1(event_tokens),
        contributing_source_event_ids=tuple(
            event.event_id for event in opportunity.contributing_events
        ),
    )
    evidence_class = _evidence_class(row)
    ground_truth = (
        GroundTruthSummaryV1(
            detector.detector_id,
            opportunity.direction,
            observable.contributing_source_event_ids,
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
        detector_id=detector.detector_id,
        detector_version=detector.version,
        direction=opportunity.direction,
        observable_feature_summary_sha256=observable.sha256,
        ground_truth_summary_sha256=(
            None if ground_truth is None else ground_truth.sha256
        ),
        supporting_source_event_ids=reveal_ids,
    )
    declaration = DETECTOR_REGISTRY_V1.require(
        detector.detector_id,
        detector.version,
    )
    return LessonCandidateV1(
        source_ancestry=ancestry,
        candidate_key=finding.candidate_key,
        detector=detector,
        bounds=finding.bounds,
        checkpoint=ancestry.checkpoint,
        observable_feature_summary=observable,
        ground_truth_summary=ground_truth,
        difficulty_projection=_finding_difficulty(
            runtime,
            opportunity,
            finding,
            recipe,
            evidence_class,
            declaration.hidden_liquidity_relevant,
            len({token.split("|", 3)[1] for token in feature_tokens}),
        ),
        rarity_projection=RarityProjectionV1(
            qualification_source_row=row.row_id,
            qualifying_units=execution.report.qualifying_units,
            eligible_units=execution.report.eligible_units,
        ),
        source_window_outcome=recipe.source_window_outcome,
        primary_skill_id=declaration.primary_skill_id,
        supporting_skill_ids=declaration.supporting_skill_ids,
        objective_projection=ObserveClassifyObjectiveV1(
            detector.detector_id,
            opportunity.direction,
            finding.bounds.activation_us,
            finding.bounds.post_end_us,
        ),
        reveal_material=reveal,
        known_ambiguity=tuple(
            sorted(
                {
                    "DETECTOR_INTERPRETATION_REQUIRES_HUMAN_FALSE_POSITIVE_REVIEW",
                    "OUTCOME_CONDITIONED_WINDOW_NOT_UNSELECTED_MARKET_TIME",
                    *(
                        {"SYNTHETIC_RECONSTRUCTION_NOT_HISTORICAL_FACT"}
                        if evidence_class
                        is EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL
                        else set()
                    ),
                }
            )
        ),
        capability_record=support.capability_record,
        evidence_class=evidence_class,
    )


def _recipes_and_candidates(
    *,
    manifest: QualificationSourcesManifestV1,
    sources: Sequence[SourceMaterializationV1],
    executions: Sequence[DetectorExecutionV1],
    prepared: Sequence[_PreparedSourceEvidenceV1] | None,
    seed: int,
    recorded_recipes: Sequence[CandidateRecipeV1] | None = None,
) -> tuple[ReviewReadyCandidateV1, ...]:
    source_by_row = {item.row_id: item for item in sources}
    prepared_by_row = (
        {} if prepared is None else {item.materialization.row_id: item for item in prepared}
    )
    recorded_by_finding = (
        {}
        if recorded_recipes is None
        else {item.finding_sha256: item for item in recorded_recipes}
    )
    if len(recorded_by_finding) != len(recorded_recipes or ()):
        raise ValueError("persisted candidate recipes duplicate a finding")
    row_ordinals: dict[str, int] = defaultdict(int)
    candidates: list[ReviewReadyCandidateV1] = []
    for execution in executions:
        source = source_by_row[execution.row_id]
        opportunities = {item.sha256: item for item in execution.opportunities}
        for finding in execution.report.findings:
            opportunity = opportunities.get(finding.opportunity_sha256)
            if opportunity is None:
                raise ValueError("detector finding lacks its exact opportunity")
            row_ordinals[execution.row_id] += 1
            if recorded_recipes is None:
                timeline = prepared_by_row.get(execution.row_id)
                historical = (
                    None
                    if timeline is None
                    or not isinstance(timeline.timeline, _HistoricalTimelineV1)
                    else timeline.timeline
                )
                activation_state = (
                    None
                    if historical is None
                    else historical.state_at(
                        finding.bounds.activation_us,
                        strictly_before=False,
                    )
                )
                final_state = (
                    None
                    if historical is None
                    else historical.state_at(
                        finding.bounds.post_end_us,
                        strictly_before=True,
                    )
                )
                activation_mid = (
                    None if activation_state is None else activation_state.mid_x2
                )
                final_mid = None if final_state is None else final_state.mid_x2
                activation_spread = (
                    None
                    if activation_state is None
                    else activation_state.spread_ticks
                )
                recipe = CandidateRecipeV1(
                    row_id=execution.row_id,
                    ordinal=row_ordinals[execution.row_id],
                    detector_id=execution.detector_id,
                    detector_report_sha256=execution.report.report_sha256,
                    finding_sha256=finding.finding_sha256,
                    opportunity_id=opportunity.opportunity_id,
                    opportunity_sha256=opportunity.sha256,
                    phase="CONTINUOUS",
                    direction=opportunity.direction,
                    activation_mid_x2=activation_mid,
                    activation_spread_ticks=activation_spread,
                    final_mid_x2=final_mid,
                    source_window_outcome=source_window_outcome_v1(
                        opportunity.direction,
                        activation_mid,
                        final_mid,
                    ),
                    seed=seed,
                )
            else:
                recipe = recorded_by_finding.get(finding.finding_sha256)
                if recipe is None:
                    raise ValueError("persisted candidate recipe omits a finding")
                if (
                    recipe.row_id != execution.row_id
                    or recipe.ordinal != row_ordinals[execution.row_id]
                    or recipe.detector_id != execution.detector_id
                    or recipe.detector_report_sha256
                    != execution.report.report_sha256
                    or recipe.opportunity_id != opportunity.opportunity_id
                    or recipe.opportunity_sha256 != opportunity.sha256
                    or recipe.direction is not opportunity.direction
                    or recipe.seed != seed
                ):
                    raise ValueError("persisted candidate recipe binding differs")
            candidate = _build_runtime_candidate(
                manifest=manifest,
                source=source,
                execution=execution,
                opportunity=opportunity,
                finding=finding,
                recipe=recipe,
            )
            candidates.append(
                ReviewReadyCandidateV1(
                    recipe=recipe,
                    candidate=candidate,
                    technical_status=(
                        TechnicalCandidateStatusV1.READY_FOR_HUMAN_REVIEW
                    ),
                    technical_reason_codes=(
                        "CAPABILITIES_VERIFIED",
                        "DETECTOR_RUNTIME_EMITTED_FINDING",
                        "HUMAN_REVIEW_PENDING",
                        "SOURCE_LINEAGE_VERIFIED",
                        "SOURCE_REPLAY_VERIFIED",
                    ),
                )
            )
    if recorded_recipes is not None and len(candidates) != len(recorded_recipes):
        raise ValueError("persisted candidate recipes contain nonfinding rows")
    return tuple(candidates)


def _assemble_runtime_result(
    *,
    source_manifest_raw: bytes,
    source_manifest: QualificationSourcesManifestV1,
    seed: int,
    sources: tuple[SourceMaterializationV1, ...],
    executions: tuple[DetectorExecutionV1, ...],
    prepared: tuple[_PreparedSourceEvidenceV1, ...] | None = None,
    recorded_recipes: tuple[CandidateRecipeV1, ...] | None = None,
) -> MiningQualificationResultV1:
    candidates = _recipes_and_candidates(
        manifest=source_manifest,
        sources=sources,
        executions=executions,
        prepared=prepared,
        seed=seed,
        recorded_recipes=recorded_recipes,
    )
    selection = select_technical_review_candidates(
        tuple(item.candidate for item in candidates)
    )
    by_candidate = {item.candidate.candidate_id: item for item in candidates}
    decisions = {item.candidate_id: item for item in selection.decisions}
    packet_rows = tuple(
        TechnicalReviewRowV1(
            ordinal=ordinal,
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            source_row=candidate.rarity_projection.qualification_source_row,
            source_id=candidate.source_ancestry.source_id,
            source_sha256=candidate.source_ancestry.source_sha256,
            detector_id=candidate.detector.detector_id,
            selection_stage=decisions[candidate.candidate_id].stage.value,
            selection_decision_sha256=sha256_json(
                decisions[candidate.candidate_id].as_dict()
            ),
            capability_record_sha256=candidate.capability_record.sha256,
            source_ancestry_sha256=candidate.source_ancestry.sha256,
            technical_status=by_candidate[candidate.candidate_id].technical_status,
            technical_reason_codes=by_candidate[
                candidate.candidate_id
            ].technical_reason_codes,
        )
        for ordinal, candidate in enumerate(selection.selected, 1)
    )
    counts = tuple(
        (
            row_id,
            sum(
                candidate.rarity_projection.qualification_source_row == row_id
                for candidate in selection.selected
            ),
        )
        for row_id in ("event", "quiet", "hidden", "fragmented", "historical")
    )
    packet = TechnicalReviewPacketV1(
        rows=packet_rows,
        target_count=selection.target_count,
        selected_count=selection.selected_count,
        shortfall_count=selection.shortfall_count,
        event_five_gate_passed=selection.event_five_gate_passed,
        mandatory_source_counts=counts,
    )
    return MiningQualificationResultV1(
        source_manifest_raw=source_manifest_raw,
        source_manifest=source_manifest,
        seed=seed,
        active_source_rows=tuple(item.row_id for item in sources),
        source_materializations=sources,
        detector_executions=executions,
        candidates=candidates,
        selection=selection,
        review_packet=packet,
    )


def _opportunity_from_dict(payload: Mapping[str, object]) -> DetectorOpportunityV1:
    expected = {
        "activation_us",
        "active_start_us",
        "contributing_events",
        "detector_id",
        "direction",
        "exclusions",
        "measurements",
        "opportunity_id",
        "price",
        "record_kind",
        "sampling_unit",
        "schema_version",
        "side",
        "source_end_us",
        "source_start_us",
        "venue",
        "witness_ids",
        "witness_kind",
    }
    if (
        set(payload) != expected
        or payload["record_kind"] != "DETECTOR_OPPORTUNITY_V1"
        or payload["schema_version"] != MINING_SCHEMA_VERSION_V1
    ):
        raise ValueError("persisted detector opportunity schema differs")
    raw_measurements = payload["measurements"]
    raw_events = payload["contributing_events"]
    raw_exclusions = payload["exclusions"]
    raw_witnesses = payload["witness_ids"]
    if (
        not isinstance(raw_measurements, list)
        or not isinstance(raw_events, list)
        or not isinstance(raw_exclusions, list)
        or not isinstance(raw_witnesses, list)
    ):
        raise TypeError("persisted detector opportunity arrays are invalid")
    measurements: list[DetectorMeasurementV1] = []
    for raw_measurement in raw_measurements:
        if not isinstance(raw_measurement, dict) or set(raw_measurement) != {
            "name",
            "value",
        }:
            raise ValueError("persisted detector measurement fields differ")
        value = raw_measurement["value"]
        if isinstance(value, list):
            if any(type(item) is not int for item in value):
                raise TypeError("persisted vector measurement is invalid")
            value = tuple(value)
        measurements.append(
            DetectorMeasurementV1(str(raw_measurement["name"]), value)  # type: ignore[arg-type]
        )
    events: list[MiningEventReferenceV1] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict) or set(raw_event) != {
            "event_id",
            "source_sequence",
            "timestamp_us",
        }:
            raise ValueError("persisted detector event-reference fields differ")
        events.append(
            MiningEventReferenceV1(
                str(raw_event["event_id"]),
                int(raw_event["timestamp_us"]),
                int(raw_event["source_sequence"]),
            )
        )
    price = payload["price"]
    if type(price) not in {int, str}:
        raise TypeError("persisted detector opportunity price is invalid")
    return DetectorOpportunityV1(
        detector_id=str(payload["detector_id"]),
        opportunity_id=str(payload["opportunity_id"]),
        sampling_unit=str(payload["sampling_unit"]),
        source_start_us=int(payload["source_start_us"]),
        source_end_us=int(payload["source_end_us"]),
        active_start_us=int(payload["active_start_us"]),
        activation_us=int(payload["activation_us"]),
        direction=CandidateDirectionV1(str(payload["direction"])),
        side=CandidateSideV1(str(payload["side"])),
        venue=str(payload["venue"]),
        price=price,
        witness_kind=str(payload["witness_kind"]),
        witness_ids=tuple(str(item) for item in raw_witnesses),
        measurements=tuple(measurements),
        contributing_events=tuple(events),
        exclusions=tuple(MiningExclusionV1(str(item)) for item in raw_exclusions),
    )


def _replay_detector_executions(
    *,
    manifest: QualificationSourcesManifestV1,
    sources: tuple[SourceMaterializationV1, ...],
    raw_executions: object,
) -> tuple[DetectorExecutionV1, ...]:
    if not isinstance(raw_executions, list) or any(
        not isinstance(item, dict) for item in raw_executions
    ):
        raise TypeError("persisted detector execution matrix is invalid")
    source_by_row = {item.row_id: item for item in sources}
    runtime = MiningDetectorRuntimeV1()
    executions: list[DetectorExecutionV1] = []
    expected_fields = {
        "adapter_status",
        "detector_id",
        "execution_sha256",
        "opportunities",
        "record_kind",
        "report",
        "report_sha256",
        "row_id",
        "schema_version",
        "source_evidence_sha256",
    }
    for raw_execution in raw_executions:
        if (
            set(raw_execution) != expected_fields
            or raw_execution["record_kind"] != "LESSON_DETECTOR_EXECUTION_V1"
            or raw_execution["schema_version"] != LESSON_REVIEW_SCHEMA_VERSION_V1
        ):
            raise ValueError("persisted detector execution schema differs")
        row_id = str(raw_execution["row_id"])
        detector_id = str(raw_execution["detector_id"])
        source = source_by_row.get(row_id)
        if source is None:
            raise ValueError("persisted detector execution names an inactive source")
        raw_opportunities = raw_execution["opportunities"]
        if not isinstance(raw_opportunities, list) or any(
            not isinstance(item, dict) for item in raw_opportunities
        ):
            raise TypeError("persisted detector opportunities are invalid")
        opportunities = tuple(
            _opportunity_from_dict(item) for item in raw_opportunities
        )
        row = manifest.row(row_id)
        threshold_sha256 = runtime.threshold_manifest.detector_threshold_sha256(
            detector_id
        )
        detector = DetectorProjectionV1(detector_id, 1, threshold_sha256)
        report = runtime.run(
            detector_id,
            _source_capability_inventory(row, detector),
            _source_ancestry(row, source),
            opportunities,
        )
        if (
            raw_execution["report"] != report.as_dict()
            or raw_execution["report_sha256"] != report.report_sha256
        ):
            raise ValueError("persisted detector report does not replay exactly")
        execution = DetectorExecutionV1(
            row_id=row_id,
            detector_id=detector_id,
            adapter_status=str(raw_execution["adapter_status"]),
            source_evidence_sha256=str(raw_execution["source_evidence_sha256"]),
            opportunities=opportunities,
            report=report,
        )
        if raw_execution["execution_sha256"] != execution.execution_sha256:
            raise ValueError("persisted detector execution digest differs")
        executions.append(execution)
    return tuple(executions)


def qualify_lesson_candidates(
    *,
    repository: Path,
    source_manifest_path: Path,
    materialization_root: Path,
    seed: int,
    active_source_rows: Sequence[str] | None = None,
) -> MiningQualificationResultV1:
    if type(seed) is not int or seed < 0:
        raise ValueError("lesson mining seed must be nonnegative")
    raw, manifest = load_qualification_source_manifest(source_manifest_path)
    sources, prepared = _materialize_and_prepare_sources(
        repository=repository,
        manifest=manifest,
        materialization_root=materialization_root,
        active_source_rows=active_source_rows,
    )
    executions = _execute_detector_matrix(manifest, prepared)
    return _assemble_runtime_result(
        source_manifest_raw=raw,
        source_manifest=manifest,
        seed=seed,
        sources=sources,
        executions=executions,
        prepared=prepared,
    )


def replay_qualification_artifacts(
    payloads: Mapping[str, bytes],
) -> MiningQualificationResultV1:
    expected = {
        "qualification-sources.toml",
        "source-validation.json",
        "candidates.json",
        "selection.json",
        "review-packet.json",
    }
    if set(payloads) != expected:
        raise ValueError("lesson mining persisted artifact inventory differs")
    source_raw = payloads["qualification-sources.toml"]
    manifest = QualificationSourcesManifestV1.from_toml_bytes(source_raw)
    if manifest.manifest_sha256 != WO33A1_SOURCE_MANIFEST_SHA256_V1:
        raise ValueError("persisted lesson mining source matrix differs")
    validation = _parse_canonical_object(
        payloads["source-validation.json"], "source validation"
    )
    expected_validation_fields = {
        "active_source_rows",
        "detector_executions",
        "detector_finding_count",
        "detector_invocation_status",
        "detector_opportunity_count",
        "detector_report_count",
        "manifest_file_sha256",
        "manifest_sha256",
        "protected_seed_execution",
        "record_kind",
        "rows",
        "schema_version",
        "seed",
        "status",
    }
    if (
        set(validation) != expected_validation_fields
        or validation.get("record_kind") != "LESSON_MINING_SOURCE_VALIDATION_V1"
        or validation.get("schema_version") != LESSON_REVIEW_SCHEMA_VERSION_V1
        or validation.get("status") != "PASS"
        or validation.get("protected_seed_execution") is not False
        or validation.get("detector_invocation_status") != "EXECUTED"
        or validation.get("manifest_sha256") != manifest.manifest_sha256
        or validation.get("manifest_file_sha256")
        != hashlib.sha256(source_raw).hexdigest()
    ):
        raise ValueError("persisted source validation metadata differs")
    raw_rows = validation.get("rows")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        raise TypeError("persisted source validation rows are invalid")
    sources = tuple(SourceMaterializationV1.from_dict(row) for row in raw_rows)
    active_rows = validation.get("active_source_rows")
    if not isinstance(active_rows, list) or tuple(active_rows) != tuple(
        item.row_id for item in sources
    ):
        raise ValueError("persisted active source rows differ")
    executions = _replay_detector_executions(
        manifest=manifest,
        sources=sources,
        raw_executions=validation["detector_executions"],
    )
    if (
        validation["detector_report_count"] != len(executions)
        or validation["detector_opportunity_count"]
        != sum(len(item.opportunities) for item in executions)
        or validation["detector_finding_count"]
        != sum(len(item.report.findings) for item in executions)
    ):
        raise ValueError("persisted detector execution counts differ")
    candidates_payload = _parse_canonical_object(
        payloads["candidates.json"], "lesson candidates"
    )
    raw_candidates = candidates_payload.get("candidates")
    if not isinstance(raw_candidates, list) or any(
        not isinstance(item, dict) for item in raw_candidates
    ):
        raise TypeError("persisted lesson candidates are invalid")
    recipes: list[CandidateRecipeV1] = []
    for raw_candidate in raw_candidates:
        raw_recipe = raw_candidate.get("recipe")
        if not isinstance(raw_recipe, dict):
            raise TypeError("persisted lesson candidate recipe is invalid")
        recipes.append(CandidateRecipeV1.from_dict(raw_recipe))
    seed = int(validation["seed"])
    rebuilt = _assemble_runtime_result(
        source_manifest_raw=source_raw,
        source_manifest=manifest,
        seed=seed,
        sources=sources,
        executions=executions,
        recorded_recipes=tuple(recipes),
    )
    rebuilt_payloads = rebuilt.artifact_payloads()
    if any(rebuilt_payloads[name] != payloads[name] for name in expected):
        raise ValueError("persisted lesson mining replay diverged")
    return rebuilt


def compare_review_candidates(
    left: ReviewReadyCandidateV1,
    right: ReviewReadyCandidateV1,
) -> dict[str, object]:
    if not isinstance(left, ReviewReadyCandidateV1) or not isinstance(
        right, ReviewReadyCandidateV1
    ):
        raise TypeError("candidate comparison requires two review-ready candidates")
    from .deduplication import compare_candidates
    from .selection import candidate_dimension_values_v1

    duplicate = compare_candidates(left.candidate, right.candidate)
    return {
        "duplicate_comparison": duplicate.as_dict(),
        "left_candidate_id": left.candidate.candidate_id,
        "left_dimensions": dict(candidate_dimension_values_v1(left.candidate)),
        "record_kind": "LESSON_CANDIDATE_COMPARISON_V1",
        "right_candidate_id": right.candidate.candidate_id,
        "right_dimensions": dict(candidate_dimension_values_v1(right.candidate)),
        "schema_version": LESSON_REVIEW_SCHEMA_VERSION_V1,
    }


__all__ = [
    "HUMAN_REVIEW_FIELDS_V1",
    "LESSON_REVIEW_RUBRIC_VERSION_V1",
    "LESSON_REVIEW_SCHEMA_VERSION_V1",
    "OUTCOME_CONDITIONING_CAVEAT_V1",
    "WO33A1_SOURCE_MANIFEST_SHA256_V1",
    "CandidateRecipeV1",
    "DetectorExecutionV1",
    "LessonBuildProposalV1",
    "LessonReviewDecisionV1",
    "LessonReviewSidecarV1",
    "MiningQualificationResultV1",
    "ReviewReadyCandidateV1",
    "ReviewerAuthorityV1",
    "SourceMaterializationV1",
    "TechnicalCandidateStatusV1",
    "TechnicalReviewPacketV1",
    "TechnicalReviewRowV1",
    "compare_review_candidates",
    "load_qualification_source_manifest",
    "materialize_and_verify_sources",
    "qualify_lesson_candidates",
    "replay_qualification_artifacts",
]
