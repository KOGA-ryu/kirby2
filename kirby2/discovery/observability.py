"""Leakage-safe observations and one-time terminal reveal for WO35-E.

This module deliberately separates decision-time observable inputs from reference
labels and keeps the holdout/adversarial material outside the reveal controller
until robustness has passed.  Persistence and CLI integration remain WO35-F work.
"""

from __future__ import annotations

import hashlib
import re
import struct
import threading
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .identity import canonical_identity_bytes
from .objectives import POLICY_SCALE_V1, unsigned_share_ppm
from .partitions import StrategyPartitionV1
from .robustness import (
    RobustnessEvidenceV1,
    RobustnessOutcomeV1,
    RobustnessQualificationV1,
    qualify_robustness,
)


OBSERVABILITY_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_DECISION_OBSERVABILITY_V1"
OBSERVABILITY_SCHEMA_VERSION_V1 = 1
REFERENCE_LABEL_SCHEMA_ID_V1 = "KIRBY2_REFERENCE_DECISION_LABEL_V1"
REFERENCE_LABEL_SCHEMA_VERSION_V1 = 1
REFERENCE_EXECUTION_ORACLE_ID_V1 = "REFERENCE_EXECUTION_ORACLE_V1"
TERMINAL_REVEAL_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_TERMINAL_REVEAL_V1"
TERMINAL_REVEAL_SCHEMA_VERSION_V1 = 1
TERMINAL_REVEAL_POLICY_ID_V1 = "ROBUSTNESS_BEFORE_ATOMIC_TERMINAL_REVEAL_V1"
ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1 = "SIMULATOR_COUNTERFACTUAL_ONLY_V1"
TERMINAL_ROOT_ORDER_V1 = (
    *(f"HOLDOUT:{root}" for root in range(3_503_000, 3_503_008)),
    *(f"ADVERSARIAL_HOLDOUT:{root}" for root in range(3_504_000, 3_504_008)),
)
OBSERVABLE_FEATURE_NAMES_V1 = (
    "aggressive_buy_volume",
    "aggressive_sell_volume",
    "book_imbalance_ppm",
    "pending_order_count",
    "position_shares",
    "relative_volume_ppm",
    "spread_ticks",
)
FORBIDDEN_REFERENCE_FIELDS_V1 = frozenset(
    {
        "future_adverse_move_ticks",
        "future_completion_shares",
        "future_fill_quantity",
        "future_mid_ticks",
        "hidden_quantity",
        "hidden_regime",
        "is_true_opportunity",
        "label_class",
        "opportunity",
        "oracle_id",
        "oracle_sha256",
        "oracle_version",
        "priority",
        "reference_action",
        "reference_state",
        "reserve_quantity",
        "source_event_ids",
        "truth_event",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ObservationStatusV1(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class CandidateSignalV1(str, Enum):
    GREEN = "GREEN"
    WAIT = "WAIT"
    RED = "RED"


class CandidatePermissionV1(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DisciplineEligibilityV1(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"


class DisciplineReasonV1(str, Enum):
    NONE = "NONE"
    CHASED_AFTER_INVALIDATION = "CHASED_AFTER_INVALIDATION"
    ACTED_DURING_RED = "ACTED_DURING_RED"


class DisciplineEvidenceStatusV1(str, Enum):
    MEASURED = "MEASURED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RevealStageV1(str, Enum):
    CANDIDATE_FROZEN = "CANDIDATE_FROZEN"
    ROBUSTNESS_PASSED = "ROBUSTNESS_PASSED"
    TERMINAL_REVEALED = "TERMINAL_REVEALED"
    CLOSED_INSUFFICIENT_EVIDENCE = "CLOSED_INSUFFICIENT_EVIDENCE"
    EXPERIMENT_INVALID = "EXPERIMENT_INVALID"


class ScientificConclusionV1(str, Enum):
    CONFIRMED_WITHIN_DECLARED_SCOPE = "CONFIRMED_WITHIN_DECLARED_SCOPE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXPERIMENT_INVALID = "EXPERIMENT_INVALID"
    NO_CANDIDATE_MET_CRITERIA = "NO_CANDIDATE_MET_CRITERIA"


class ObservationUnavailableError(RuntimeError):
    code = "UNAVAILABLE_OBSERVATION"


class MissingReferenceLabelError(RuntimeError):
    code = "MISSING_REFERENCE_LABEL"


class RevealProtocolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ObservableDecisionInputV1:
    decision_id: str
    root_seed: int
    decision_time_us: int
    observable_cut_sha256: str
    status: ObservationStatusV1
    spread_ticks: int | None
    book_imbalance_ppm: int | None
    relative_volume_ppm: int | None
    aggressive_buy_volume: int | None
    aggressive_sell_volume: int | None
    position_shares: int | None
    pending_order_count: int | None

    def __post_init__(self) -> None:
        _require_nfc(self.decision_id, "decision ID")
        _require_uint64(self.root_seed, "decision root")
        _require_nonnegative_int(self.decision_time_us, "decision time")
        _require_sha256(self.observable_cut_sha256, "observable cut digest")
        if not isinstance(self.status, ObservationStatusV1):
            raise TypeError("observation status must be typed")
        values = self.feature_values
        if self.status is ObservationStatusV1.UNAVAILABLE:
            if any(value is not None for _, value in values):
                raise ValueError("unavailable observation cannot carry feature values")
            return
        if any(type(value) is not int for _, value in values):
            raise TypeError("available observation requires every integer feature")
        if self.spread_ticks is not None and self.spread_ticks < 0:
            raise ValueError("observable spread must be nonnegative")
        if self.relative_volume_ppm is not None and self.relative_volume_ppm < 0:
            raise ValueError("observable relative volume must be nonnegative")
        if self.aggressive_buy_volume is not None and self.aggressive_buy_volume < 0:
            raise ValueError("observable aggressive-buy volume must be nonnegative")
        if self.aggressive_sell_volume is not None and self.aggressive_sell_volume < 0:
            raise ValueError("observable aggressive-sell volume must be nonnegative")
        if self.pending_order_count is not None and self.pending_order_count < 0:
            raise ValueError("observable pending-order count must be nonnegative")

    @property
    def feature_values(self) -> tuple[tuple[str, int | None], ...]:
        return (
            ("aggressive_buy_volume", self.aggressive_buy_volume),
            ("aggressive_sell_volume", self.aggressive_sell_volume),
            ("book_imbalance_ppm", self.book_imbalance_ppm),
            ("pending_order_count", self.pending_order_count),
            ("position_shares", self.position_shares),
            ("relative_volume_ppm", self.relative_volume_ppm),
            ("spread_ticks", self.spread_ticks),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "decision_time_us": self.decision_time_us,
            "features": dict(self.feature_values),
            "observable_cut_sha256": self.observable_cut_sha256,
            "root_seed": self.root_seed,
            "schema_id": OBSERVABILITY_SCHEMA_ID_V1,
            "schema_version": OBSERVABILITY_SCHEMA_VERSION_V1,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class CandidateDecisionProjectionV1:
    decision_id: str
    label_id: str
    root_seed: int
    decision_time_us: int
    observable_cut_sha256: str
    candidate_state: CandidateSignalV1
    permission: CandidatePermissionV1
    discipline_eligibility: DisciplineEligibilityV1
    discipline_violation: bool
    discipline_reason: DisciplineReasonV1
    frozen_at_us: int

    def __post_init__(self) -> None:
        _require_nfc(self.decision_id, "candidate projection decision ID")
        _require_nfc(self.label_id, "candidate projection label ID")
        if self.decision_id != self.label_id:
            raise ValueError("candidate projection must reference its exact decision label")
        _require_uint64(self.root_seed, "candidate projection root")
        _require_nonnegative_int(self.decision_time_us, "candidate decision time")
        _require_sha256(self.observable_cut_sha256, "candidate observable cut digest")
        if not isinstance(self.candidate_state, CandidateSignalV1):
            raise TypeError("candidate projection signal must be typed")
        if not isinstance(self.permission, CandidatePermissionV1):
            raise TypeError("candidate projection permission must be typed")
        if not isinstance(self.discipline_eligibility, DisciplineEligibilityV1):
            raise TypeError("candidate discipline eligibility must be typed")
        if type(self.discipline_violation) is not bool:
            raise TypeError("candidate discipline violation must be Boolean")
        if not isinstance(self.discipline_reason, DisciplineReasonV1):
            raise TypeError("candidate discipline reason must be typed")
        if self.discipline_eligibility is DisciplineEligibilityV1.INELIGIBLE:
            if self.discipline_violation or self.discipline_reason is not DisciplineReasonV1.NONE:
                raise ValueError("ineligible discipline row cannot report a violation")
        elif self.discipline_violation:
            if self.discipline_reason not in {
                DisciplineReasonV1.CHASED_AFTER_INVALIDATION,
                DisciplineReasonV1.ACTED_DURING_RED,
            }:
                raise ValueError("discipline violation lacks its exact typed reason")
        elif self.discipline_reason is not DisciplineReasonV1.NONE:
            raise ValueError("eligible nonviolation must use reason NONE")
        if self.frozen_at_us != self.decision_time_us:
            raise ValueError("candidate projection was not frozen at decision time")
        recursive_keys = _recursive_keys(self.as_dict())
        leaked = recursive_keys.intersection(FORBIDDEN_REFERENCE_FIELDS_V1)
        if leaked:
            raise ValueError(f"candidate projection leaked reference fields: {sorted(leaked)!r}")

    @property
    def projection_sha256(self) -> str:
        return hashlib.sha256(canonical_identity_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_state": self.candidate_state.value,
            "decision_id": self.decision_id,
            "decision_time_us": self.decision_time_us,
            "discipline_eligibility": self.discipline_eligibility.value,
            "discipline_reason": self.discipline_reason.value,
            "discipline_violation": self.discipline_violation,
            "frozen_at_us": self.frozen_at_us,
            "label_id": self.label_id,
            "observable_cut_sha256": self.observable_cut_sha256,
            "permission": self.permission.value,
            "root_seed": self.root_seed,
        }


@dataclass(frozen=True, slots=True)
class ReferenceDecisionLabelV1:
    label_id: str
    root_seed: int
    decision_time_us: int
    reference_state: CandidateSignalV1
    opportunity: bool
    source_event_ids: tuple[str, ...]
    oracle_id: str
    oracle_version: int
    oracle_sha256: str
    label_sha256: str

    def __post_init__(self) -> None:
        _require_nfc(self.label_id, "reference label ID")
        _require_uint64(self.root_seed, "reference label root")
        _require_nonnegative_int(self.decision_time_us, "reference decision time")
        if not isinstance(self.reference_state, CandidateSignalV1):
            raise TypeError("reference state must be typed")
        if type(self.opportunity) is not bool:
            raise TypeError("reference opportunity must be Boolean")
        if self.opportunity != (self.reference_state is CandidateSignalV1.GREEN):
            raise ValueError("executable opportunity must be exactly reference GREEN")
        if type(self.source_event_ids) is not tuple or not self.source_event_ids:
            raise ValueError("reference label requires source event IDs")
        for event_id in self.source_event_ids:
            _require_nfc(event_id, "reference source event ID")
        if len(self.source_event_ids) != len(set(self.source_event_ids)):
            raise ValueError("reference source event IDs must be unique")
        if self.oracle_id != REFERENCE_EXECUTION_ORACLE_ID_V1:
            raise ValueError("reference oracle ID changed")
        if self.oracle_version != 1:
            raise ValueError("reference oracle version changed")
        _require_sha256(self.oracle_sha256, "reference oracle digest")
        _require_sha256(self.label_sha256, "reference label digest")
        if self.label_sha256 != _reference_label_sha256(self.identity_dict()):
            raise ValueError("reference label digest does not bind exact label bytes")

    def identity_dict(self) -> dict[str, object]:
        return {
            "decision_time_us": self.decision_time_us,
            "label_id": self.label_id,
            "opportunity": self.opportunity,
            "oracle_id": self.oracle_id,
            "oracle_sha256": self.oracle_sha256,
            "oracle_version": self.oracle_version,
            "reference_state": self.reference_state.value,
            "root_seed": self.root_seed,
            "schema_id": REFERENCE_LABEL_SCHEMA_ID_V1,
            "schema_version": REFERENCE_LABEL_SCHEMA_VERSION_V1,
            "source_event_ids": list(self.source_event_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "label_sha256": self.label_sha256}


def bind_reference_decision_label(
    *,
    label_id: str,
    root_seed: int,
    decision_time_us: int,
    reference_state: CandidateSignalV1,
    opportunity: bool,
    source_event_ids: tuple[str, ...],
    oracle_sha256: str,
) -> ReferenceDecisionLabelV1:
    if not isinstance(reference_state, CandidateSignalV1):
        raise TypeError("reference state must be typed")
    identity = {
        "decision_time_us": decision_time_us,
        "label_id": label_id,
        "opportunity": opportunity,
        "oracle_id": REFERENCE_EXECUTION_ORACLE_ID_V1,
        "oracle_sha256": oracle_sha256,
        "oracle_version": 1,
        "reference_state": reference_state.value,
        "root_seed": root_seed,
        "schema_id": REFERENCE_LABEL_SCHEMA_ID_V1,
        "schema_version": REFERENCE_LABEL_SCHEMA_VERSION_V1,
        "source_event_ids": list(source_event_ids),
    }
    return ReferenceDecisionLabelV1(
        label_id,
        root_seed,
        decision_time_us,
        reference_state,
        opportunity,
        source_event_ids,
        REFERENCE_EXECUTION_ORACLE_ID_V1,
        1,
        oracle_sha256,
        _reference_label_sha256(identity),
    )


@dataclass(frozen=True, slots=True)
class ScoredDecisionV1:
    label_id: str
    classification_correct: bool
    false_green: bool
    missed_opportunity: bool

    def __post_init__(self) -> None:
        _require_nfc(self.label_id, "scored label ID")
        for value in (
            self.classification_correct,
            self.false_green,
            self.missed_opportunity,
        ):
            if type(value) is not bool:
                raise TypeError("scored-decision flags must be Boolean")


@dataclass(frozen=True, slots=True)
class DisciplineEvidenceV1:
    status: DisciplineEvidenceStatusV1
    eligible_decisions: int
    violations: int
    utility: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DisciplineEvidenceStatusV1):
            raise TypeError("discipline evidence status must be typed")
        _require_nonnegative_int(self.eligible_decisions, "eligible decisions")
        _require_nonnegative_int(self.violations, "discipline violations")
        if self.violations > self.eligible_decisions:
            raise ValueError("discipline violations exceed eligible decisions")
        if self.status is DisciplineEvidenceStatusV1.INSUFFICIENT_EVIDENCE:
            if self.eligible_decisions != 0 or self.violations != 0 or self.utility is not None:
                raise ValueError("missing discipline evidence cannot carry a perfect score")
        elif type(self.utility) is not int or not 0 <= self.utility <= POLICY_SCALE_V1:
            raise ValueError("measured discipline utility must be in 0..S")


def project_candidate_decision(
    observation: ObservableDecisionInputV1,
    label: ReferenceDecisionLabelV1 | None,
    *,
    candidate_state: CandidateSignalV1,
    permission: CandidatePermissionV1,
) -> CandidateDecisionProjectionV1:
    if not isinstance(observation, ObservableDecisionInputV1):
        raise TypeError("candidate projection requires a typed observation")
    if observation.status is ObservationStatusV1.UNAVAILABLE:
        raise ObservationUnavailableError(
            "candidate decision refused because the required observation is unavailable"
        )
    if label is None:
        raise MissingReferenceLabelError(
            "candidate decision lacks its immutable reference label"
        )
    if not isinstance(label, ReferenceDecisionLabelV1):
        raise TypeError("candidate projection reference label must be typed or absent")
    if not isinstance(candidate_state, CandidateSignalV1):
        raise TypeError("candidate signal must be typed")
    if not isinstance(permission, CandidatePermissionV1):
        raise TypeError("candidate permission must be typed")
    if (
        observation.root_seed != label.root_seed
        or observation.decision_time_us != label.decision_time_us
        or observation.decision_id != label.label_id
    ):
        raise ValueError("observable decision and immutable reference label are not aligned")
    eligible = label.reference_state in {CandidateSignalV1.WAIT, CandidateSignalV1.RED}
    violation = eligible and permission is CandidatePermissionV1.ALLOW
    if violation and label.reference_state is CandidateSignalV1.WAIT:
        reason = DisciplineReasonV1.CHASED_AFTER_INVALIDATION
    elif violation:
        reason = DisciplineReasonV1.ACTED_DURING_RED
    else:
        reason = DisciplineReasonV1.NONE
    return CandidateDecisionProjectionV1(
        observation.decision_id,
        label.label_id,
        observation.root_seed,
        observation.decision_time_us,
        observation.observable_cut_sha256,
        candidate_state,
        permission,
        (
            DisciplineEligibilityV1.ELIGIBLE
            if eligible
            else DisciplineEligibilityV1.INELIGIBLE
        ),
        violation,
        reason,
        observation.decision_time_us,
    )


def score_candidate_decision(
    projection: CandidateDecisionProjectionV1,
    label: ReferenceDecisionLabelV1,
) -> ScoredDecisionV1:
    """Join a completed projection to separately owned truth only for scoring."""

    if not isinstance(projection, CandidateDecisionProjectionV1) or not isinstance(
        label,
        ReferenceDecisionLabelV1,
    ):
        raise TypeError("decision scoring requires typed projection and label")
    if (
        projection.decision_id != label.label_id
        or projection.label_id != label.label_id
        or projection.root_seed != label.root_seed
        or projection.decision_time_us != label.decision_time_us
    ):
        raise ValueError("candidate projection and reference label are not aligned")
    eligible = label.reference_state in {CandidateSignalV1.WAIT, CandidateSignalV1.RED}
    expected_violation = eligible and projection.permission is CandidatePermissionV1.ALLOW
    expected_reason = (
        DisciplineReasonV1.CHASED_AFTER_INVALIDATION
        if expected_violation and label.reference_state is CandidateSignalV1.WAIT
        else DisciplineReasonV1.ACTED_DURING_RED
        if expected_violation
        else DisciplineReasonV1.NONE
    )
    if (
        projection.discipline_eligibility
        is not (
            DisciplineEligibilityV1.ELIGIBLE
            if eligible
            else DisciplineEligibilityV1.INELIGIBLE
        )
        or projection.discipline_violation != expected_violation
        or projection.discipline_reason is not expected_reason
    ):
        raise ValueError("candidate discipline projection differs from immutable reference")
    candidate_green = (
        projection.candidate_state is CandidateSignalV1.GREEN
        and projection.permission is CandidatePermissionV1.ALLOW
    )
    return ScoredDecisionV1(
        label_id=projection.label_id,
        classification_correct=projection.candidate_state is label.reference_state,
        false_green=candidate_green and not label.opportunity,
        missed_opportunity=label.opportunity and not candidate_green,
    )


def summarize_discipline(
    projections: tuple[CandidateDecisionProjectionV1, ...],
) -> DisciplineEvidenceV1:
    if type(projections) is not tuple or any(
        not isinstance(item, CandidateDecisionProjectionV1) for item in projections
    ):
        raise TypeError("discipline summary requires a typed projection tuple")
    eligible = tuple(
        item
        for item in projections
        if item.discipline_eligibility is DisciplineEligibilityV1.ELIGIBLE
    )
    if not eligible:
        return DisciplineEvidenceV1(
            DisciplineEvidenceStatusV1.INSUFFICIENT_EVIDENCE,
            0,
            0,
            None,
        )
    violations = sum(item.discipline_violation for item in eligible)
    utility = POLICY_SCALE_V1 - unsigned_share_ppm(violations, len(eligible))
    return DisciplineEvidenceV1(
        DisciplineEvidenceStatusV1.MEASURED,
        len(eligible),
        violations,
        utility,
    )


@dataclass(frozen=True, slots=True)
class EndogenousDivergenceRecordV1:
    root_seed: int
    base_execution_sha256: str
    candidate_execution_sha256: str
    paired_observable_prefix_sha256: str
    execution_delta: int
    claim_scope: str = ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1
    real_market_superiority: bool = False

    def __post_init__(self) -> None:
        if type(self.root_seed) is not int or not 0 <= self.root_seed < 1 << 64:
            raise ValueError("divergence root must be unsigned 64-bit")
        for value, label in (
            (self.base_execution_sha256, "base execution digest"),
            (self.candidate_execution_sha256, "candidate execution digest"),
            (self.paired_observable_prefix_sha256, "paired observable prefix digest"),
        ):
            _require_sha256(value, label)
        if type(self.execution_delta) is not int:
            raise TypeError("endogenous execution delta must be an integer")
        if self.claim_scope != ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1:
            raise ValueError("endogenous divergence claim scope is overstated")
        if self.real_market_superiority is not False:
            raise ValueError("simulator counterfactual cannot claim real-market superiority")

    def as_dict(self) -> dict[str, object]:
        return {
            "base_execution_sha256": self.base_execution_sha256,
            "candidate_execution_sha256": self.candidate_execution_sha256,
            "claim_scope": self.claim_scope,
            "execution_delta": self.execution_delta,
            "paired_observable_prefix_sha256": self.paired_observable_prefix_sha256,
            "real_market_superiority": self.real_market_superiority,
            "root_seed": self.root_seed,
        }


@dataclass(frozen=True, slots=True)
class TerminalPartitionReferenceV1:
    partition: StrategyPartitionV1
    manifest_sha256: str
    member_inventory_sha256: str

    def __post_init__(self) -> None:
        if self.partition not in {
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        }:
            raise ValueError("terminal reference partition is invalid")
        _require_sha256(self.manifest_sha256, "terminal manifest digest")
        _require_sha256(self.member_inventory_sha256, "terminal member inventory digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_sha256": self.manifest_sha256,
            "member_inventory_sha256": self.member_inventory_sha256,
            "partition": self.partition.value,
        }


@dataclass(frozen=True, slots=True)
class SealedTerminalMaterialV1:
    candidate_semantic_sha256: str
    holdout: TerminalPartitionReferenceV1
    adversarial: TerminalPartitionReferenceV1
    reveal_token_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_semantic_sha256, "sealed candidate digest")
        if (
            not isinstance(self.holdout, TerminalPartitionReferenceV1)
            or self.holdout.partition is not StrategyPartitionV1.HOLDOUT
        ):
            raise ValueError("sealed holdout reference is invalid")
        if (
            not isinstance(self.adversarial, TerminalPartitionReferenceV1)
            or self.adversarial.partition is not StrategyPartitionV1.ADVERSARIAL_HOLDOUT
        ):
            raise ValueError("sealed adversarial reference is invalid")
        _require_sha256(self.reveal_token_sha256, "sealed reveal-token digest")

    @property
    def commitment_sha256(self) -> str:
        raw = canonical_identity_bytes(self.as_dict())
        digest = hashlib.sha256()
        digest.update(b"KIRBY2_SEALED_TERMINAL_MATERIAL_V1\x00")
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "adversarial": self.adversarial.as_dict(),
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "holdout": self.holdout.as_dict(),
            "reveal_token_sha256": self.reveal_token_sha256,
            "schema_id": TERMINAL_REVEAL_SCHEMA_ID_V1,
            "schema_version": TERMINAL_REVEAL_SCHEMA_VERSION_V1,
        }


def seal_terminal_material(
    *,
    candidate_semantic_sha256: str,
    holdout_manifest_sha256: str,
    holdout_member_inventory_sha256: str,
    adversarial_manifest_sha256: str,
    adversarial_member_inventory_sha256: str,
    reveal_token: str,
) -> SealedTerminalMaterialV1:
    _require_nfc(reveal_token, "reveal token")
    return SealedTerminalMaterialV1(
        candidate_semantic_sha256,
        TerminalPartitionReferenceV1(
            StrategyPartitionV1.HOLDOUT,
            holdout_manifest_sha256,
            holdout_member_inventory_sha256,
        ),
        TerminalPartitionReferenceV1(
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
            adversarial_manifest_sha256,
            adversarial_member_inventory_sha256,
        ),
        _token_sha256(reveal_token),
    )


@dataclass(frozen=True, slots=True)
class TerminalAccessRecordV1:
    access_ordinal: int
    candidate_semantic_sha256: str
    sealed_material_commitment_sha256: str
    robustness_evidence_sha256: str
    partitions: tuple[StrategyPartitionV1, ...]
    token_sha256: str

    def __post_init__(self) -> None:
        if self.access_ordinal != 1:
            raise ValueError("V1 terminal reveal has exactly one access ordinal")
        for value, label in (
            (self.candidate_semantic_sha256, "terminal access candidate digest"),
            (self.sealed_material_commitment_sha256, "terminal material commitment"),
            (self.robustness_evidence_sha256, "terminal robustness digest"),
            (self.token_sha256, "terminal token digest"),
        ):
            _require_sha256(value, label)
        if self.partitions != (
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        ):
            raise ValueError("terminal access must atomically record both partitions")

    @property
    def access_sha256(self) -> str:
        return hashlib.sha256(canonical_identity_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "access_ordinal": self.access_ordinal,
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "partitions": [item.value for item in self.partitions],
            "policy_id": TERMINAL_REVEAL_POLICY_ID_V1,
            "robustness_evidence_sha256": self.robustness_evidence_sha256,
            "sealed_material_commitment_sha256": self.sealed_material_commitment_sha256,
            "token_sha256": self.token_sha256,
        }


@dataclass(frozen=True, slots=True)
class TerminalRevealResultV1:
    access_record: TerminalAccessRecordV1
    holdout: TerminalPartitionReferenceV1
    adversarial: TerminalPartitionReferenceV1
    execution_order: tuple[str, ...]
    access_recorded_before_exposure: bool

    def __post_init__(self) -> None:
        if not isinstance(self.access_record, TerminalAccessRecordV1):
            raise TypeError("terminal reveal access record must be typed")
        if self.holdout.partition is not StrategyPartitionV1.HOLDOUT:
            raise ValueError("terminal reveal holdout reference is invalid")
        if self.adversarial.partition is not StrategyPartitionV1.ADVERSARIAL_HOLDOUT:
            raise ValueError("terminal reveal adversarial reference is invalid")
        if self.execution_order != TERMINAL_ROOT_ORDER_V1:
            raise ValueError("terminal roots are not holdout-then-adversarial ascending")
        if self.access_recorded_before_exposure is not True:
            raise ValueError("terminal material was exposed before its access record")


class TerminalRevealControllerV1:
    """In-process atomic boundary; durable enforcement is added by WO35-F."""

    __slots__ = (
        "_access_records",
        "_candidate_semantic_sha256",
        "_lock",
        "_material_commitment_sha256",
        "_robustness_evidence_sha256",
        "_robustness_record_count",
        "_stage",
        "_token_consumed",
    )

    def __init__(
        self,
        *,
        candidate_semantic_sha256: str,
        sealed_material_commitment_sha256: str,
    ) -> None:
        _require_sha256(candidate_semantic_sha256, "frozen candidate digest")
        _require_sha256(
            sealed_material_commitment_sha256,
            "sealed material commitment",
        )
        self._candidate_semantic_sha256 = candidate_semantic_sha256
        self._material_commitment_sha256 = sealed_material_commitment_sha256
        self._stage = RevealStageV1.CANDIDATE_FROZEN
        self._robustness_evidence_sha256: str | None = None
        self._robustness_record_count = 0
        self._token_consumed = False
        self._access_records: list[TerminalAccessRecordV1] = []
        self._lock = threading.Lock()

    @property
    def candidate_semantic_sha256(self) -> str:
        return self._candidate_semantic_sha256

    @property
    def sealed_material_commitment_sha256(self) -> str:
        return self._material_commitment_sha256

    @property
    def stage(self) -> RevealStageV1:
        return self._stage

    @property
    def robustness_record_count(self) -> int:
        return self._robustness_record_count

    @property
    def token_consumed(self) -> bool:
        return self._token_consumed

    @property
    def access_records(self) -> tuple[TerminalAccessRecordV1, ...]:
        return tuple(self._access_records)

    def record_robustness(
        self,
        evidence: RobustnessEvidenceV1,
        qualification: RobustnessQualificationV1,
    ) -> RevealStageV1:
        with self._lock:
            if self._stage is not RevealStageV1.CANDIDATE_FROZEN:
                self._invalidate()
                raise RevealProtocolError(
                    "ROBUSTNESS_ALREADY_RECORDED",
                    "robustness may run exactly once after candidate freeze",
                )
            self._robustness_record_count += 1
            if not isinstance(evidence, RobustnessEvidenceV1) or not isinstance(
                qualification,
                RobustnessQualificationV1,
            ):
                self._invalidate()
                raise RevealProtocolError(
                    "UNTYPED_ROBUSTNESS_EVIDENCE",
                    "robustness result was not typed",
                )
            if (
                evidence.candidate_semantic_sha256 != self._candidate_semantic_sha256
                or qualification.evidence_sha256 != evidence.evidence_sha256
                or qualification != qualify_robustness(evidence)
            ):
                self._invalidate()
                raise RevealProtocolError(
                    "ROBUSTNESS_BINDING_MISMATCH",
                    "robustness did not bind the frozen candidate and exact evidence",
                )
            self._robustness_evidence_sha256 = evidence.evidence_sha256
            if qualification.outcome is RobustnessOutcomeV1.PASSED:
                self._stage = RevealStageV1.ROBUSTNESS_PASSED
            elif qualification.outcome is RobustnessOutcomeV1.INSUFFICIENT_EVIDENCE:
                self._stage = RevealStageV1.CLOSED_INSUFFICIENT_EVIDENCE
            else:
                self._stage = RevealStageV1.EXPERIMENT_INVALID
            return self._stage

    def reveal(
        self,
        material: SealedTerminalMaterialV1,
        *,
        reveal_token: str,
    ) -> TerminalRevealResultV1:
        with self._lock:
            if self._token_consumed or self._access_records:
                self._invalidate()
                raise RevealProtocolError(
                    "REVEAL_ALREADY_CONSUMED",
                    "the atomic terminal reveal token was already consumed",
                )
            if self._stage is not RevealStageV1.ROBUSTNESS_PASSED:
                self._invalidate()
                raise RevealProtocolError(
                    "ROBUSTNESS_NOT_PASSED",
                    "terminal reveal requires one passing robustness result",
                )
            if not isinstance(material, SealedTerminalMaterialV1):
                self._invalidate()
                raise RevealProtocolError(
                    "UNTYPED_SEALED_MATERIAL",
                    "terminal material must use the sealed typed envelope",
                )
            if (
                material.commitment_sha256 != self._material_commitment_sha256
                or material.candidate_semantic_sha256
                != self._candidate_semantic_sha256
            ):
                self._invalidate()
                raise RevealProtocolError(
                    "SEALED_MATERIAL_MISMATCH",
                    "terminal material differs from the frozen commitment",
                )
            _require_nfc(reveal_token, "reveal token")
            token_sha256 = _token_sha256(reveal_token)
            if token_sha256 != material.reveal_token_sha256:
                self._invalidate()
                raise RevealProtocolError(
                    "REVEAL_TOKEN_MISMATCH",
                    "terminal reveal token differs from the sealed token",
                )
            if self._robustness_evidence_sha256 is None:
                self._invalidate()
                raise RevealProtocolError(
                    "ROBUSTNESS_EVIDENCE_MISSING",
                    "passing stage lacks its robustness evidence digest",
                )

            # Consumed and appended while the lock is held.  Only after this record
            # exists do we construct the object that exposes both sealed references.
            self._token_consumed = True
            record = TerminalAccessRecordV1(
                1,
                self._candidate_semantic_sha256,
                self._material_commitment_sha256,
                self._robustness_evidence_sha256,
                (
                    StrategyPartitionV1.HOLDOUT,
                    StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
                ),
                token_sha256,
            )
            self._access_records.append(record)
            result = TerminalRevealResultV1(
                record,
                material.holdout,
                material.adversarial,
                TERMINAL_ROOT_ORDER_V1,
                bool(self._access_records and self._access_records[-1] is record),
            )
            self._stage = RevealStageV1.TERMINAL_REVEALED
            return result

    def _invalidate(self) -> None:
        self._stage = RevealStageV1.EXPERIMENT_INVALID


def scientific_conclusion(
    *,
    candidate_selected: bool,
    validation_qualified: bool,
    robustness_qualified: bool,
    holdout_qualified: bool,
    adversarial_qualified: bool,
    reveal_stage: RevealStageV1,
) -> ScientificConclusionV1:
    for value in (
        candidate_selected,
        validation_qualified,
        robustness_qualified,
        holdout_qualified,
        adversarial_qualified,
    ):
        if type(value) is not bool:
            raise TypeError("scientific qualification flags must be Boolean")
    if not isinstance(reveal_stage, RevealStageV1):
        raise TypeError("scientific conclusion reveal stage must be typed")
    if reveal_stage is RevealStageV1.EXPERIMENT_INVALID:
        return ScientificConclusionV1.EXPERIMENT_INVALID
    if not candidate_selected:
        return ScientificConclusionV1.NO_CANDIDATE_MET_CRITERIA
    if (
        validation_qualified
        and robustness_qualified
        and holdout_qualified
        and adversarial_qualified
        and reveal_stage is RevealStageV1.TERMINAL_REVEALED
    ):
        return ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
    return ScientificConclusionV1.INSUFFICIENT_EVIDENCE


def _token_sha256(value: str) -> str:
    return hashlib.sha256(
        b"KIRBY2_TERMINAL_REVEAL_TOKEN_V1\x00" + value.encode("utf-8")
    ).hexdigest()


def _reference_label_sha256(payload: dict[str, object]) -> str:
    raw = canonical_identity_bytes(payload)
    digest = hashlib.sha256()
    digest.update(b"KIRBY2_REFERENCE_DECISION_LABEL_V1\x00")
    digest.update(struct.pack(">Q", len(raw)))
    digest.update(raw)
    return digest.hexdigest()


def _recursive_keys(value: object) -> frozenset[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_recursive_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(_recursive_keys(child))
    return frozenset(keys)


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _require_uint64(value: object, label: str) -> None:
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError(f"{label} must be unsigned 64-bit")


def _require_nfc(value: object, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")


__all__ = [
    "CandidateDecisionProjectionV1",
    "CandidatePermissionV1",
    "CandidateSignalV1",
    "DisciplineEligibilityV1",
    "DisciplineEvidenceStatusV1",
    "DisciplineEvidenceV1",
    "DisciplineReasonV1",
    "ENDOGENOUS_DIVERGENCE_CLAIM_SCOPE_V1",
    "EndogenousDivergenceRecordV1",
    "FORBIDDEN_REFERENCE_FIELDS_V1",
    "OBSERVABILITY_SCHEMA_ID_V1",
    "OBSERVABLE_FEATURE_NAMES_V1",
    "ObservableDecisionInputV1",
    "ObservationStatusV1",
    "ObservationUnavailableError",
    "MissingReferenceLabelError",
    "REFERENCE_EXECUTION_ORACLE_ID_V1",
    "REFERENCE_LABEL_SCHEMA_ID_V1",
    "ReferenceDecisionLabelV1",
    "RevealProtocolError",
    "RevealStageV1",
    "ScientificConclusionV1",
    "ScoredDecisionV1",
    "SealedTerminalMaterialV1",
    "TERMINAL_REVEAL_POLICY_ID_V1",
    "TERMINAL_REVEAL_SCHEMA_ID_V1",
    "TERMINAL_ROOT_ORDER_V1",
    "TerminalAccessRecordV1",
    "TerminalPartitionReferenceV1",
    "TerminalRevealControllerV1",
    "TerminalRevealResultV1",
    "bind_reference_decision_label",
    "project_candidate_decision",
    "scientific_conclusion",
    "score_candidate_decision",
    "seal_terminal_material",
    "summarize_discipline",
]
