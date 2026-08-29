"""Immutable contracts for reviewable lesson-mining candidates.

WO33-A deliberately defines identity and evidence boundaries before any detector is
allowed to mine a source.  Candidate identity is a closed projection; human review
is a separate sidecar and assessment presentation is a redacted view.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from importlib.resources import files
from types import MappingProxyType

from kirby2.research.toml_codec import canonical_toml


MINING_SCHEMA_VERSION_V1 = 1
POLICY_SCALE_V1 = 1_000_000
LESSON_CANDIDATE_ID_PREFIX_V1 = "lesson-candidate-"

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_NOT_APPLICABLE = "NOT_APPLICABLE"
_CONSOLIDATED = "CONSOLIDATED"

DETECTOR_THRESHOLDS_MANIFEST_ID_V1 = "WO33_A1_DETECTOR_THRESHOLDS_V1"
MINING_PLAN_MANIFEST_ID_V1 = "WO33_A1_MINING_PLAN_V1"
QUALIFICATION_SOURCES_MANIFEST_ID_V1 = "WO33_A1_QUALIFICATION_SOURCES_V1"
SESSION_PHASE_VALUES_V1 = frozenset(
    {
        "CLOSED",
        "PREOPEN",
        "OPENING_AUCTION",
        "CONTINUOUS",
        "HALTED",
        "REOPENING_AUCTION",
        "CLOSING_AUCTION",
        "POSTCLOSE",
    }
)


def canonical_json_bytes(value: object) -> bytes:
    """Return the one compact, sorted, ASCII-escaped WO33 canonical encoding."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def round_div_even(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise TypeError("round_div_even requires exact integers")
    if denominator <= 0:
        raise ValueError("round_div_even denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return -quotient if numerator < 0 else quotient


def ratio_ppm(numerator: int, denominator: int) -> int:
    return round_div_even(numerator * POLICY_SCALE_V1, denominator)


def unsigned_share_ppm(numerator: int, denominator: int) -> int:
    return _clamp(ratio_ppm(numerator, denominator), 0, POLICY_SCALE_V1)


def _clamp(value: int, lower: int, upper: int) -> int:
    return min(upper, max(lower, value))


def _require_digest(value: str, label: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")


def _require_optional_digest(value: str | None, label: str) -> None:
    if value is not None:
        _require_digest(value, label)


def _require_identifier(value: str, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be an uppercase stable identifier")


def _require_nfc(value: str, label: str) -> None:
    if (
        type(value) is not str
        or not value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{label} must be nonempty NFC text")


def _nfc_sorted(values: tuple[str, ...]) -> bool:
    return values == tuple(sorted(values, key=lambda item: item.encode("utf-8")))


def _require_sorted_unique_nfc(
    values: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool,
) -> None:
    if type(values) is not tuple or (not allow_empty and not values):
        raise ValueError(f"{label} has an invalid inventory")
    for value in values:
        _require_nfc(value, label)
    if len(set(values)) != len(values) or not _nfc_sorted(values):
        raise ValueError(f"{label} must be unique and NFC-byte sorted")


def _require_ppm(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= POLICY_SCALE_V1:
        raise ValueError(f"{label} must be an integer in [0, 1000000]")


def _require_optional_ppm(value: int | None, label: str) -> None:
    if value is not None:
        _require_ppm(value, label)


class SourceKindV1(str, Enum):
    RUN = "RUN"
    DATASET = "DATASET"
    RECONSTRUCTION = "RECONSTRUCTION"


class EvidenceClassV1(str, Enum):
    SYNTHETIC_GROUND_TRUTH = "S"
    HISTORICAL_MARKET_BY_ORDER = "H"
    RECONSTRUCTION_COUNTERFACTUAL = "R"

    @property
    def evidence_quality_ppm(self) -> int:
        return {
            EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: 1_000_000,
            EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: 850_000,
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: 500_000,
        }[self]


class CandidateDirectionV1(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NOT_APPLICABLE = _NOT_APPLICABLE


class CandidateSideV1(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NOT_APPLICABLE = _NOT_APPLICABLE


class CandidateLessonTypeV1(str, Enum):
    OBSERVE_CLASSIFY = "OBSERVE_CLASSIFY"


class CandidateProposalStateV1(str, Enum):
    PROPOSED = "PROPOSED"


class SourceWindowOutcomeV1(str, Enum):
    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"
    STASIS = "STASIS"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_OBSERVABLE = "NOT_OBSERVABLE"


class DifficultyEstimateStateV1(str, Enum):
    """Calibration status for the transparent WO33-C heuristic."""

    UNVALIDATED_ESTIMATE = "UNVALIDATED_ESTIMATE"


class CandidatePresentationModeV1(str, Enum):
    ASSESSMENT = "ASSESSMENT"
    TECHNICAL_REVIEW = "TECHNICAL_REVIEW"
    REVEALED = "REVEALED"


class HumanReviewDecisionV1(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NEEDS_EDIT = "NEEDS_EDIT"
    SUPERSEDED = "SUPERSEDED"


class CapabilityEvidenceKindV1(str, Enum):
    SOURCE_MANIFEST = "SOURCE_MANIFEST"
    EVENT_RANGE = "EVENT_RANGE"
    CHECKPOINT = "CHECKPOINT"
    ADAPTER_CONTRACT = "ADAPTER_CONTRACT"


@dataclass(frozen=True, slots=True)
class SourceIdentityV1:
    kind: SourceKindV1
    source_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKindV1):
            raise TypeError("source kind must be RUN, DATASET, or RECONSTRUCTION")
        _require_nfc(self.source_id, "source ID")
        _require_digest(self.source_sha256, "source digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.source_id,
            "kind": self.kind.value,
            "sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class CheckpointReferenceV1:
    checkpoint_id: str
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        _require_nfc(self.checkpoint_id, "checkpoint ID")
        _require_digest(self.checkpoint_sha256, "checkpoint digest")

    def as_dict(self) -> dict[str, str]:
        return {"id": self.checkpoint_id, "sha256": self.checkpoint_sha256}


@dataclass(frozen=True, slots=True)
class SourceAncestryV1:
    source_kind: SourceKindV1
    source_id: str
    source_sha256: str
    checkpoint_id: str | None = None
    checkpoint_sha256: str | None = None
    event_prefix_sha256: str | None = None
    parent_source_ancestry_sha256: str | None = None

    def __post_init__(self) -> None:
        SourceIdentityV1(self.source_kind, self.source_id, self.source_sha256)
        if (self.checkpoint_id is None) != (self.checkpoint_sha256 is None):
            raise ValueError("checkpoint ID and digest must both be null or nonnull")
        if self.checkpoint_id is not None:
            CheckpointReferenceV1(self.checkpoint_id, self.checkpoint_sha256 or "")
        _require_optional_digest(self.event_prefix_sha256, "event-prefix digest")
        _require_optional_digest(
            self.parent_source_ancestry_sha256,
            "parent source-ancestry digest",
        )

    @property
    def source_identity(self) -> SourceIdentityV1:
        return SourceIdentityV1(
            self.source_kind,
            self.source_id,
            self.source_sha256,
        )

    @property
    def checkpoint(self) -> CheckpointReferenceV1 | None:
        if self.checkpoint_id is None:
            return None
        return CheckpointReferenceV1(
            self.checkpoint_id,
            self.checkpoint_sha256 or "",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "event_prefix_sha256": self.event_prefix_sha256,
            "parent_source_ancestry_sha256": (
                self.parent_source_ancestry_sha256
            ),
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_sha256": self.source_sha256,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class CandidateKeyV1:
    detector_id: str
    direction: CandidateDirectionV1
    side: CandidateSideV1
    venue: str
    price: int | str
    witness_key: str
    anchor_start_us: int
    evidence_discriminator: str

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "detector ID")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("candidate direction is invalid")
        if not isinstance(self.side, CandidateSideV1):
            raise TypeError("candidate side is invalid")
        if self.venue not in {_NOT_APPLICABLE, _CONSOLIDATED}:
            _require_nfc(self.venue, "candidate venue")
        if not (
            (type(self.price) is int and self.price > 0)
            or self.price == _NOT_APPLICABLE
        ):
            raise ValueError("candidate price must be positive or NOT_APPLICABLE")
        if self.witness_key != _NOT_APPLICABLE:
            _require_digest(self.witness_key, "candidate witness key")
        if type(self.anchor_start_us) is not int or self.anchor_start_us < 0:
            raise ValueError("candidate anchor start must be nonnegative microseconds")
        _require_digest(
            self.evidence_discriminator,
            "candidate evidence discriminator",
        )

    def as_list(self) -> list[object]:
        return [
            self.detector_id,
            self.direction.value,
            self.side.value,
            self.venue,
            self.price,
            self.witness_key,
            self.anchor_start_us,
            self.evidence_discriminator,
        ]


@dataclass(frozen=True, slots=True)
class DetectorProjectionV1:
    detector_id: str
    version: int
    threshold_sha256: str

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "detector ID")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("detector version must be a positive integer")
        _require_digest(self.threshold_sha256, "detector threshold digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.detector_id,
            "threshold_sha256": self.threshold_sha256,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class CandidateBoundsV1:
    source_start_us: int
    source_end_us: int
    warmup_start_us: int
    active_start_us: int
    active_end_us: int
    post_end_us: int

    def __post_init__(self) -> None:
        values = (
            self.source_start_us,
            self.source_end_us,
            self.warmup_start_us,
            self.active_start_us,
            self.active_end_us,
            self.post_end_us,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("candidate bounds must be nonnegative integer microseconds")
        if not (
            self.source_start_us
            <= self.warmup_start_us
            <= self.active_start_us
            < self.active_end_us
            <= self.post_end_us
            <= self.source_end_us
        ):
            raise ValueError("candidate half-open bounds are inconsistent")

    @property
    def activation_us(self) -> int:
        return self.active_end_us - 1

    def as_dict(self) -> dict[str, int]:
        return {
            "active_end_us": self.active_end_us,
            "active_start_us": self.active_start_us,
            "post_end_us": self.post_end_us,
            "source_end_us": self.source_end_us,
            "source_start_us": self.source_start_us,
            "warmup_start_us": self.warmup_start_us,
        }


@dataclass(frozen=True, slots=True)
class RegimeSignatureV1:
    phase: str
    regime_id: str
    volume_band: str
    liquidity_band: str
    spread_band: str

    def __post_init__(self) -> None:
        for label, value in (
            ("phase", self.phase),
            ("regime ID", self.regime_id),
            ("volume band", self.volume_band),
            ("liquidity band", self.liquidity_band),
            ("spread band", self.spread_band),
        ):
            _require_nfc(value, label)
        if self.phase not in SESSION_PHASE_VALUES_V1:
            raise ValueError("regime phase is not a closed session-phase value")
        for label, value in (
            ("volume", self.volume_band),
            ("liquidity", self.liquidity_band),
        ):
            if value not in {"LOW", "NORMAL", "HIGH", "NOT_APPLICABLE"}:
                raise ValueError(f"regime {label} band is not a closed V1 value")
        if self.spread_band not in {
            "ONE",
            "TWO",
            "MODERATE",
            "WIDE",
            "EXTREME",
            "NOT_APPLICABLE",
        }:
            raise ValueError("regime spread band is not a closed V1 value")

    def as_dict(self) -> dict[str, str]:
        return {
            "liquidity_band": self.liquidity_band,
            "phase": self.phase,
            "regime_id": self.regime_id,
            "spread_band": self.spread_band,
            "volume_band": self.volume_band,
        }


@dataclass(frozen=True, slots=True)
class ObservableFeatureSummaryV1:
    feature_tokens: tuple[str, ...]
    regime_signature: RegimeSignatureV1
    event_five_grams: tuple[tuple[str, ...], ...]
    contributing_source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sorted_unique_nfc(
            self.feature_tokens,
            "observable feature tokens",
            allow_empty=False,
        )
        for token in self.feature_tokens:
            parts = token.split("|")
            if len(parts) != 4 or any(not part for part in parts):
                raise ValueError("observable feature token must have four fields")
            _, _, type_tag, canonical_value = parts
            if type_tag == "INTEGER" and re.fullmatch(
                r"-?(?:0|[1-9][0-9]*)",
                canonical_value,
            ) is None:
                raise ValueError("observable integer token is not canonical")
            if type_tag == "FLAG" and canonical_value not in {"true", "false"}:
                raise ValueError("observable flag token is not canonical")
        if not isinstance(self.regime_signature, RegimeSignatureV1):
            raise TypeError("observable regime signature is invalid")
        if type(self.event_five_grams) is not tuple or not self.event_five_grams:
            raise ValueError("observable event five-grams must not be empty")
        for gram in self.event_five_grams:
            if type(gram) is not tuple or not 1 <= len(gram) <= 5:
                raise ValueError("each event five-gram must contain one to five tokens")
            for token in gram:
                _require_nfc(token, "event five-gram token")
                parts = token.split("|")
                if (
                    len(parts) != 3
                    or parts[1] not in {"BUY", "SELL", "NONE"}
                    or parts[2]
                    not in {
                        "BELOW_BID",
                        "AT_BID",
                        "INSIDE",
                        "AT_ASK",
                        "ABOVE_ASK",
                        "NO_PRICE",
                        "NO_REFERENCE_QUOTE",
                    }
                ):
                    raise ValueError("event five-gram token is not canonical")
        gram_keys = tuple(canonical_json_bytes(list(item)) for item in self.event_five_grams)
        if len(set(gram_keys)) != len(gram_keys) or gram_keys != tuple(sorted(gram_keys)):
            raise ValueError("event five-grams must be unique and canonically sorted")
        _require_sorted_source_events(self.contributing_source_event_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "contributing_source_event_ids": list(
                self.contributing_source_event_ids
            ),
            "event_five_grams": [list(item) for item in self.event_five_grams],
            "feature_tokens": list(self.feature_tokens),
            "regime_signature": self.regime_signature.as_dict(),
            "schema_version": MINING_SCHEMA_VERSION_V1,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def evidence_discriminator(self) -> str:
        return sha256_json(list(self.contributing_source_event_ids))


def _require_sorted_source_events(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or not values:
        raise ValueError("contributing source event IDs must not be empty")
    for value in values:
        _require_nfc(value, "contributing source event ID")
    if len(set(values)) != len(values):
        raise ValueError("source event IDs must be unique within the source sequence")


@dataclass(frozen=True, slots=True)
class GroundTruthSummaryV1:
    detector_id: str
    direction: CandidateDirectionV1
    supporting_source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "ground-truth detector ID")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("ground-truth direction is invalid")
        _require_sorted_source_events(self.supporting_source_event_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "authoritative_activation": True,
            "evidence_class": EvidenceClassV1.SYNTHETIC_GROUND_TRUTH.value,
            "expected_classification": {
                "detector_id": self.detector_id,
                "direction": self.direction.value,
            },
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "supporting_source_event_ids": list(self.supporting_source_event_ids),
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class RevealMaterialV1:
    detector_id: str
    detector_version: int
    direction: CandidateDirectionV1
    observable_feature_summary_sha256: str
    ground_truth_summary_sha256: str | None
    supporting_source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "reveal detector ID")
        if type(self.detector_version) is not int or self.detector_version <= 0:
            raise ValueError("reveal detector version must be positive")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("reveal direction is invalid")
        _require_digest(
            self.observable_feature_summary_sha256,
            "reveal observable-summary digest",
        )
        _require_optional_digest(
            self.ground_truth_summary_sha256,
            "reveal ground-truth digest",
        )
        _require_sorted_source_events(self.supporting_source_event_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "direction": self.direction.value,
            "ground_truth_summary_sha256": self.ground_truth_summary_sha256,
            "observable_feature_summary_sha256": (
                self.observable_feature_summary_sha256
            ),
            "outcome_mapping_id": "OBSERVE_CLASSIFY_OUTCOME_V1",
            "policy_id": "OBSERVE_CLASSIFY_REVEAL_V1",
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "supporting_source_event_ids": list(self.supporting_source_event_ids),
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class CapabilityEvidenceReferenceV1:
    kind: CapabilityEvidenceKindV1
    reference_id: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CapabilityEvidenceKindV1):
            raise TypeError("capability evidence kind is invalid")
        _require_nfc(self.reference_id, "capability evidence reference ID")
        _require_digest(self.sha256, "capability evidence digest")

    @property
    def sort_key(self) -> tuple[str, bytes, str]:
        return (self.kind.value, self.reference_id.encode("utf-8"), self.sha256)

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.reference_id,
            "kind": self.kind.value,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRecordRowV1:
    capability: str
    evidence: tuple[CapabilityEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.capability, "capability ID")
        if type(self.evidence) is not tuple or not self.evidence:
            raise ValueError("available capability requires evidence references")
        if any(
            not isinstance(item, CapabilityEvidenceReferenceV1)
            for item in self.evidence
        ):
            raise TypeError("capability evidence reference is invalid")
        keys = tuple(item.sort_key for item in self.evidence)
        if len(set(keys)) != len(keys) or keys != tuple(sorted(keys)):
            raise ValueError("capability evidence must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "evidence": [item.as_dict() for item in self.evidence],
            "status": "AVAILABLE",
        }


@dataclass(frozen=True, slots=True)
class CapabilityRecordV1:
    source_identity: SourceIdentityV1
    detector: DetectorProjectionV1
    records: tuple[CapabilityRecordRowV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_identity, SourceIdentityV1):
            raise TypeError("capability source identity is invalid")
        if not isinstance(self.detector, DetectorProjectionV1):
            raise TypeError("capability detector projection is invalid")
        if type(self.records) is not tuple or not self.records:
            raise ValueError("capability record must contain available rows")
        if any(not isinstance(item, CapabilityRecordRowV1) for item in self.records):
            raise TypeError("capability record row is invalid")
        names = tuple(item.capability for item in self.records)
        if len(set(names)) != len(names) or not _nfc_sorted(names):
            raise ValueError("capability rows must be unique and NFC-byte sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "detector": self.detector.as_dict(),
            "records": [item.as_dict() for item in self.records],
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_identity": self.source_identity.as_dict(),
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class ObserveClassifyObjectiveV1:
    detector_id: str
    direction: CandidateDirectionV1
    response_start_us: int
    response_end_us: int

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "objective detector ID")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("objective direction is invalid")
        if (
            type(self.response_start_us) is not int
            or self.response_start_us < 0
            or type(self.response_end_us) is not int
            or self.response_end_us <= self.response_start_us
        ):
            raise ValueError("objective response bounds must be a positive interval")

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "direction": self.direction.value,
            "kind": "OBSERVE_CLASSIFY_V1",
            "outcome_mapping_id": "OBSERVE_CLASSIFY_OUTCOME_V1",
            "response_end_us": self.response_end_us,
            "response_start_us": self.response_start_us,
        }


@dataclass(frozen=True, slots=True)
class RarityProjectionV1:
    qualification_source_row: str
    qualifying_units: int
    eligible_units: int

    def __post_init__(self) -> None:
        _require_nfc(self.qualification_source_row, "qualification source row")
        if (
            type(self.qualifying_units) is not int
            or type(self.eligible_units) is not int
            or self.qualifying_units < 0
            or self.eligible_units <= 0
            or self.qualifying_units > self.eligible_units
        ):
            raise ValueError("rarity counts require 0 <= qualifying <= eligible")

    @property
    def sample_frequency_ppm(self) -> int:
        return unsigned_share_ppm(self.qualifying_units, self.eligible_units)

    @property
    def rarity_ppm(self) -> int:
        return POLICY_SCALE_V1 - self.sample_frequency_ppm

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible_units": self.eligible_units,
            "policy_id": "MINING_RARITY_V1",
            "qualification_source_row": self.qualification_source_row,
            "qualifying_units": self.qualifying_units,
            "rarity_ppm": self.rarity_ppm,
            "sample_frequency_ppm": self.sample_frequency_ppm,
        }


@dataclass(frozen=True, slots=True)
class DifficultyProjectionV1:
    signal_legibility_ppm: int
    duration_legibility_ppm: int | None
    conflict_ppm: int | None
    reaction_us: int
    spread_ticks: int | None
    latency_us: int | None
    three_level_depth: int | None
    venue_count: int | None
    hidden_uncertainty_ppm: int | None
    objective_shares: int | None
    executable_depth: int | None
    feature_count: int
    evidence_quality_ppm: int

    _WEIGHTS = MappingProxyType({
        "inverse_signal_duration_ppm": 160_000,
        "conflict_ppm": 100_000,
        "reaction_hardness_ppm": 110_000,
        "spread_hardness_ppm": 100_000,
        "latency_hardness_ppm": 70_000,
        "inverse_liquidity_ppm": 100_000,
        "venue_hardness_ppm": 80_000,
        "hidden_uncertainty_ppm": 60_000,
        "objective_depth_hardness_ppm": 80_000,
        "feature_hardness_ppm": 80_000,
        "inverse_quality_ppm": 60_000,
    })

    def __post_init__(self) -> None:
        _require_ppm(self.signal_legibility_ppm, "signal legibility")
        _require_optional_ppm(self.duration_legibility_ppm, "duration legibility")
        _require_optional_ppm(self.conflict_ppm, "conflict")
        if type(self.reaction_us) is not int or self.reaction_us <= 0:
            raise ValueError("classification reaction time must be positive")
        for label, value, positive in (
            ("spread ticks", self.spread_ticks, True),
            ("latency", self.latency_us, False),
            ("three-level depth", self.three_level_depth, False),
            ("venue count", self.venue_count, True),
        ):
            if value is not None and (
                type(value) is not int or value < (1 if positive else 0)
            ):
                raise ValueError(f"{label} has an invalid exact integer value")
        _require_optional_ppm(
            self.hidden_uncertainty_ppm,
            "hidden-liquidity uncertainty",
        )
        if self.objective_shares is not None or self.executable_depth is not None:
            raise ValueError(
                "OBSERVE_CLASSIFY_V1 objective size/depth must be NOT_APPLICABLE"
            )
        if type(self.feature_count) is not int or self.feature_count <= 0:
            raise ValueError("difficulty feature count must be positive")
        _require_ppm(self.evidence_quality_ppm, "evidence quality")

    @property
    def signal_duration_legibility_ppm(self) -> int:
        values = [self.signal_legibility_ppm]
        if self.duration_legibility_ppm is not None:
            values.append(self.duration_legibility_ppm)
        return round_div_even(sum(values), len(values))

    @property
    def inverse_signal_duration_ppm(self) -> int:
        return POLICY_SCALE_V1 - self.signal_duration_legibility_ppm

    @property
    def reaction_hardness_ppm(self) -> int:
        return _clamp(
            round_div_even(
                (2_000_000 - self.reaction_us) * POLICY_SCALE_V1,
                2_000_000,
            ),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def spread_hardness_ppm(self) -> int | None:
        if self.spread_ticks is None:
            return None
        return _clamp(
            round_div_even(
                (self.spread_ticks - 1) * POLICY_SCALE_V1,
                9,
            ),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def latency_hardness_ppm(self) -> int | None:
        if self.latency_us is None:
            return None
        return _clamp(
            round_div_even(self.latency_us * POLICY_SCALE_V1, 10_000),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def inverse_liquidity_ppm(self) -> int | None:
        if self.three_level_depth is None:
            return None
        return POLICY_SCALE_V1 - _clamp(
            round_div_even(self.three_level_depth * POLICY_SCALE_V1, 5_000),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def venue_hardness_ppm(self) -> int | None:
        if self.venue_count is None:
            return None
        return _clamp(
            round_div_even(
                (self.venue_count - 1) * POLICY_SCALE_V1,
                3,
            ),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def objective_depth_hardness_ppm(self) -> None:
        return None

    @property
    def feature_hardness_ppm(self) -> int:
        return _clamp(
            round_div_even(
                (self.feature_count - 1) * POLICY_SCALE_V1,
                9,
            ),
            0,
            POLICY_SCALE_V1,
        )

    @property
    def inverse_quality_ppm(self) -> int:
        return POLICY_SCALE_V1 - self.evidence_quality_ppm

    def _weighted_components(self) -> tuple[tuple[int, int], ...]:
        values = {
            "inverse_signal_duration_ppm": self.inverse_signal_duration_ppm,
            "conflict_ppm": self.conflict_ppm,
            "reaction_hardness_ppm": self.reaction_hardness_ppm,
            "spread_hardness_ppm": self.spread_hardness_ppm,
            "latency_hardness_ppm": self.latency_hardness_ppm,
            "inverse_liquidity_ppm": self.inverse_liquidity_ppm,
            "venue_hardness_ppm": self.venue_hardness_ppm,
            "hidden_uncertainty_ppm": self.hidden_uncertainty_ppm,
            "objective_depth_hardness_ppm": self.objective_depth_hardness_ppm,
            "feature_hardness_ppm": self.feature_hardness_ppm,
            "inverse_quality_ppm": self.inverse_quality_ppm,
        }
        return tuple(
            (self._WEIGHTS[name], value)
            for name, value in values.items()
            if value is not None
        )

    @property
    def applicable_weight_sum(self) -> int:
        return sum(weight for weight, _ in self._weighted_components())

    @property
    def difficulty_ppm(self) -> int:
        components = self._weighted_components()
        if not components:
            raise ValueError("difficulty has no applicable components")
        return round_div_even(
            sum(weight * value for weight, value in components),
            sum(weight for weight, _ in components),
        )

    @property
    def estimate_state(self) -> DifficultyEstimateStateV1:
        return DifficultyEstimateStateV1.UNVALIDATED_ESTIMATE

    @property
    def missing_components(self) -> tuple[str, ...]:
        """Name every omitted nominal component in fixed policy order."""

        values = {
            "inverse_signal_duration_ppm": self.inverse_signal_duration_ppm,
            "conflict_ppm": self.conflict_ppm,
            "reaction_hardness_ppm": self.reaction_hardness_ppm,
            "spread_hardness_ppm": self.spread_hardness_ppm,
            "latency_hardness_ppm": self.latency_hardness_ppm,
            "inverse_liquidity_ppm": self.inverse_liquidity_ppm,
            "venue_hardness_ppm": self.venue_hardness_ppm,
            "hidden_uncertainty_ppm": self.hidden_uncertainty_ppm,
            "objective_depth_hardness_ppm": self.objective_depth_hardness_ppm,
            "feature_hardness_ppm": self.feature_hardness_ppm,
            "inverse_quality_ppm": self.inverse_quality_ppm,
        }
        return tuple(name for name in self._WEIGHTS if values[name] is None)

    def inspection_projection(self) -> dict[str, object]:
        """Expose the estimate label, omissions, inputs, and exact result.

        This is deliberately separate from :meth:`as_dict`, whose exact key set is
        part of candidate identity.  Review tooling can therefore make the
        unvalidated status prominent without changing existing candidate digests.
        """

        component_payload = self.as_dict()
        missing_inputs = tuple(
            name
            for name in (
                "duration_legibility_ppm",
                "conflict_ppm",
                "spread_ticks",
                "latency_us",
                "three_level_depth",
                "venue_count",
                "hidden_uncertainty_ppm",
                "objective_shares",
                "executable_depth",
            )
            if component_payload[name] is None
        )
        return {
            "calculation": "ROUND_DIV_EVEN_WEIGHTED_MEAN_APPLICABLE_ONLY",
            "component_order": list(self._WEIGHTS),
            "components": component_payload,
            "estimate_state": self.estimate_state.value,
            "missing_components": list(self.missing_components),
            "missing_inputs": list(missing_inputs),
            "policy_id": "LESSON_DIFFICULTY_V1",
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "weights_ppm": dict(self._WEIGHTS),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "applicable_weight_sum": self.applicable_weight_sum,
            "conflict_ppm": self.conflict_ppm,
            "difficulty_ppm": self.difficulty_ppm,
            "duration_legibility_ppm": self.duration_legibility_ppm,
            "evidence_quality_ppm": self.evidence_quality_ppm,
            "executable_depth": self.executable_depth,
            "feature_count": self.feature_count,
            "feature_hardness_ppm": self.feature_hardness_ppm,
            "hidden_uncertainty_ppm": self.hidden_uncertainty_ppm,
            "inverse_liquidity_ppm": self.inverse_liquidity_ppm,
            "inverse_quality_ppm": self.inverse_quality_ppm,
            "inverse_signal_duration_ppm": self.inverse_signal_duration_ppm,
            "latency_hardness_ppm": self.latency_hardness_ppm,
            "latency_us": self.latency_us,
            "objective_depth_hardness_ppm": self.objective_depth_hardness_ppm,
            "objective_shares": self.objective_shares,
            "policy_id": "LESSON_DIFFICULTY_V1",
            "reaction_hardness_ppm": self.reaction_hardness_ppm,
            "reaction_us": self.reaction_us,
            "signal_duration_legibility_ppm": (
                self.signal_duration_legibility_ppm
            ),
            "signal_legibility_ppm": self.signal_legibility_ppm,
            "spread_hardness_ppm": self.spread_hardness_ppm,
            "spread_ticks": self.spread_ticks,
            "three_level_depth": self.three_level_depth,
            "venue_count": self.venue_count,
            "venue_hardness_ppm": self.venue_hardness_ppm,
        }


@dataclass(frozen=True, slots=True)
class HumanReviewSidecarV1:
    candidate_id: str
    candidate_digest: str
    decision: HumanReviewDecisionV1
    reviewer_id: str
    review_ordinal: int
    rationale: str
    superseded_by_candidate_id: str | None = None

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id, self.candidate_digest)
        if not isinstance(self.decision, HumanReviewDecisionV1):
            raise TypeError("human review decision is invalid")
        _require_nfc(self.reviewer_id, "reviewer ID")
        if type(self.review_ordinal) is not int or self.review_ordinal <= 0:
            raise ValueError("review ordinal must be positive")
        _require_nfc(self.rationale, "review rationale")
        if self.decision is HumanReviewDecisionV1.SUPERSEDED:
            if self.superseded_by_candidate_id is None:
                raise ValueError("SUPERSEDED review requires the replacement candidate")
            _require_candidate_id_text(self.superseded_by_candidate_id)
            if self.superseded_by_candidate_id == self.candidate_id:
                raise ValueError("candidate cannot supersede itself")
        elif self.superseded_by_candidate_id is not None:
            raise ValueError("only SUPERSEDED review may name a replacement candidate")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "decision": self.decision.value,
            "rationale": self.rationale,
            "review_ordinal": self.review_ordinal,
            "reviewer_id": self.reviewer_id,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "superseded_by_candidate_id": self.superseded_by_candidate_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def _require_candidate_id_text(candidate_id: str) -> None:
    if (
        type(candidate_id) is not str
        or not candidate_id.startswith(LESSON_CANDIDATE_ID_PREFIX_V1)
        or _DIGEST.fullmatch(candidate_id[len(LESSON_CANDIDATE_ID_PREFIX_V1) :])
        is None
    ):
        raise ValueError("candidate ID must retain its complete identity digest")


def _require_candidate_id(candidate_id: str, candidate_digest: str) -> None:
    _require_digest(candidate_digest, "candidate digest")
    _require_candidate_id_text(candidate_id)
    if candidate_id != LESSON_CANDIDATE_ID_PREFIX_V1 + candidate_digest:
        raise ValueError("candidate ID and candidate digest disagree")


@dataclass(frozen=True, slots=True)
class GroundTruthAccessGrantV1:
    candidate_id: str
    candidate_digest: str
    reveal_material_sha256: str
    scope: str = "REVEAL_AUTHORIZED"

    def __post_init__(self) -> None:
        _require_candidate_id(self.candidate_id, self.candidate_digest)
        _require_digest(self.reveal_material_sha256, "reveal-material digest")
        if self.scope != "REVEAL_AUTHORIZED":
            raise ValueError("ground-truth access scope is invalid")


@dataclass(frozen=True, slots=True)
class LessonCandidateV1:
    source_ancestry: SourceAncestryV1
    candidate_key: CandidateKeyV1
    detector: DetectorProjectionV1
    bounds: CandidateBoundsV1
    checkpoint: CheckpointReferenceV1 | None
    observable_feature_summary: ObservableFeatureSummaryV1
    ground_truth_summary: GroundTruthSummaryV1 | None = field(repr=False)
    difficulty_projection: DifficultyProjectionV1
    rarity_projection: RarityProjectionV1
    source_window_outcome: SourceWindowOutcomeV1
    primary_skill_id: str
    supporting_skill_ids: tuple[str, ...]
    objective_projection: ObserveClassifyObjectiveV1
    reveal_material: RevealMaterialV1
    known_ambiguity: tuple[str, ...]
    capability_record: CapabilityRecordV1
    evidence_class: EvidenceClassV1
    lesson_type: CandidateLessonTypeV1 = CandidateLessonTypeV1.OBSERVE_CLASSIFY
    proposal_state: CandidateProposalStateV1 = CandidateProposalStateV1.PROPOSED

    def __post_init__(self) -> None:
        _validate_candidate_types(self)
        _require_identifier(self.primary_skill_id, "primary skill ID")
        _require_sorted_unique_nfc(
            self.supporting_skill_ids,
            "supporting skill IDs",
            allow_empty=True,
        )
        if self.primary_skill_id in self.supporting_skill_ids:
            raise ValueError("primary skill cannot also be a supporting skill")
        _require_sorted_unique_nfc(
            self.known_ambiguity,
            "known ambiguity",
            allow_empty=True,
        )
        self._validate_cross_record_contracts()
        self._validate_registry_contracts()

    def _validate_cross_record_contracts(self) -> None:
        if self.candidate_key.detector_id != self.detector.detector_id:
            raise ValueError("candidate key and detector projection disagree")
        if self.candidate_key.anchor_start_us != self.bounds.active_start_us:
            raise ValueError("candidate anchor must equal active_start_us")
        if (
            self.candidate_key.evidence_discriminator
            != self.observable_feature_summary.evidence_discriminator
        ):
            raise ValueError("candidate evidence discriminator is not reproducible")
        if self.checkpoint != self.source_ancestry.checkpoint:
            raise ValueError("candidate checkpoint and source ancestry disagree")
        if self.capability_record.source_identity != self.source_ancestry.source_identity:
            raise ValueError("candidate source and capability source disagree")
        if self.capability_record.detector != self.detector:
            raise ValueError("candidate detector and capability detector disagree")
        objective = self.objective_projection
        if (
            objective.detector_id != self.detector.detector_id
            or objective.direction is not self.candidate_key.direction
            or objective.response_start_us != self.bounds.activation_us
            or objective.response_end_us != self.bounds.post_end_us
        ):
            raise ValueError("candidate objective is not the exact activation response")
        if self.difficulty_projection.reaction_us != (
            objective.response_end_us - objective.response_start_us
        ):
            raise ValueError("difficulty reaction time differs from objective bounds")
        feature_paths = {
            token.split("|", 3)[1]
            for token in self.observable_feature_summary.feature_tokens
        }
        if self.difficulty_projection.feature_count != len(feature_paths):
            raise ValueError("difficulty feature count is not reproducible")
        if (
            self.candidate_key.direction is CandidateDirectionV1.NOT_APPLICABLE
            and self.source_window_outcome is not SourceWindowOutcomeV1.NOT_APPLICABLE
        ):
            raise ValueError("nondirectional candidate must have NOT_APPLICABLE outcome")
        expected_quality = self.evidence_class.evidence_quality_ppm
        if self.difficulty_projection.evidence_quality_ppm != expected_quality:
            raise ValueError("difficulty evidence quality conflicts with evidence class")
        if (
            self.evidence_class is EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER
            and self.source_ancestry.source_kind is not SourceKindV1.DATASET
        ):
            raise ValueError("historical evidence requires DATASET source ancestry")
        if (
            self.evidence_class
            is EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL
            and self.source_ancestry.source_kind is not SourceKindV1.RECONSTRUCTION
        ):
            raise ValueError(
                "reconstruction evidence requires RECONSTRUCTION source ancestry"
            )
        ground_truth_digest = (
            None if self.ground_truth_summary is None else self.ground_truth_summary.sha256
        )
        if self.evidence_class is EvidenceClassV1.SYNTHETIC_GROUND_TRUTH:
            if self.ground_truth_summary is None:
                raise ValueError("synthetic evidence requires a ground-truth summary")
        elif self.ground_truth_summary is not None:
            raise ValueError("historical/reconstruction candidates cannot synthesize truth")
        if self.ground_truth_summary is not None:
            truth = self.ground_truth_summary
            source_positions = {
                event_id: index
                for index, event_id in enumerate(
                    self.observable_feature_summary.contributing_source_event_ids
                )
            }
            truth_positions = tuple(
                source_positions.get(event_id, -1)
                for event_id in truth.supporting_source_event_ids
            )
            if (
                truth.detector_id != self.detector.detector_id
                or truth.direction is not self.candidate_key.direction
                or not set(truth.supporting_source_event_ids).issubset(
                    self.observable_feature_summary.contributing_source_event_ids
                )
                or truth_positions != tuple(sorted(truth_positions))
            ):
                raise ValueError("ground-truth summary is not bound to candidate evidence")
        reveal = self.reveal_material
        expected_reveal_ids = (
            self.observable_feature_summary.contributing_source_event_ids
            if self.ground_truth_summary is None
            else self.ground_truth_summary.supporting_source_event_ids
        )
        if (
            reveal.detector_id != self.detector.detector_id
            or reveal.detector_version != self.detector.version
            or reveal.direction is not self.candidate_key.direction
            or reveal.observable_feature_summary_sha256
            != self.observable_feature_summary.sha256
            or reveal.ground_truth_summary_sha256 != ground_truth_digest
            or reveal.supporting_source_event_ids != expected_reveal_ids
        ):
            raise ValueError("reveal material is not exact candidate evidence")

    def _validate_registry_contracts(self) -> None:
        from .detectors import DETECTOR_REGISTRY_V1
        from .skills import SKILL_REGISTRY_V1

        detector = DETECTOR_REGISTRY_V1.require(
            self.detector.detector_id,
            self.detector.version,
        )
        SKILL_REGISTRY_V1.require(self.primary_skill_id)
        for skill_id in self.supporting_skill_ids:
            SKILL_REGISTRY_V1.require(skill_id)
        if (
            detector.primary_skill_id != self.primary_skill_id
            or detector.supporting_skill_ids != self.supporting_skill_ids
        ):
            raise ValueError("candidate skills differ from the detector registry")
        expected_hidden_uncertainty = (
            {
                EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: 0,
                EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: 250_000,
                EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: 750_000,
            }[self.evidence_class]
            if detector.hidden_liquidity_relevant
            else None
        )
        if (
            self.difficulty_projection.hidden_uncertainty_ppm
            != expected_hidden_uncertainty
        ):
            raise ValueError(
                "difficulty hidden uncertainty conflicts with detector evidence"
            )
        if self.evidence_class not in detector.supported_evidence_classes:
            raise ValueError("detector does not support the candidate evidence class")
        available = tuple(item.capability for item in self.capability_record.records)
        if available != detector.required_capabilities_for(self.evidence_class):
            raise ValueError("candidate capability record is not the exact detector bundle")

    def identity_projection(self) -> dict[str, object]:
        return {
            "bounds": self.bounds.as_dict(),
            "candidate_key": self.candidate_key.as_list(),
            "capability_record_sha256": self.capability_record.sha256,
            "checkpoint": None if self.checkpoint is None else self.checkpoint.as_dict(),
            "detector": self.detector.as_dict(),
            "difficulty_projection": self.difficulty_projection.as_dict(),
            "evidence_class": self.evidence_class.value,
            "ground_truth_summary_sha256": (
                None
                if self.ground_truth_summary is None
                else self.ground_truth_summary.sha256
            ),
            "known_ambiguity": list(self.known_ambiguity),
            "lesson_type": self.lesson_type.value,
            "objective_projection": self.objective_projection.as_dict(),
            "observable_feature_summary_sha256": (
                self.observable_feature_summary.sha256
            ),
            "primary_skill_id": self.primary_skill_id,
            "proposal_state": self.proposal_state.value,
            "rarity_projection": self.rarity_projection.as_dict(),
            "reveal_material_sha256": self.reveal_material.sha256,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_ancestry_sha256": self.source_ancestry.sha256,
            "source_identity": self.source_ancestry.source_identity.as_dict(),
            "source_window_outcome": self.source_window_outcome.value,
            "supporting_skill_ids": list(self.supporting_skill_ids),
        }

    def identity_bytes(self) -> bytes:
        return canonical_json_bytes(self.identity_projection())

    @property
    def candidate_digest(self) -> str:
        return hashlib.sha256(self.identity_bytes()).hexdigest()

    @property
    def candidate_id(self) -> str:
        return LESSON_CANDIDATE_ID_PREFIX_V1 + self.candidate_digest

    def review_projection(
        self,
        mode: CandidatePresentationModeV1,
    ) -> dict[str, object]:
        if not isinstance(mode, CandidatePresentationModeV1):
            raise TypeError("candidate presentation mode is invalid")
        base: dict[str, object] = {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "human_review_status": "PENDING",
            "lesson_type": self.lesson_type.value,
            "mode": mode.value,
            "proposal_state": self.proposal_state.value,
            "response_end_us": self.objective_projection.response_end_us,
            "response_start_us": self.objective_projection.response_start_us,
            "schema_version": MINING_SCHEMA_VERSION_V1,
        }
        if mode is CandidatePresentationModeV1.ASSESSMENT:
            base.update(
                {
                    "detector": "WITHHELD_UNTIL_REVEAL",
                    "direction": "WITHHELD_UNTIL_REVEAL",
                    "source_window_outcome": "WITHHELD_UNTIL_REVEAL",
                    "title": "MARKET STRUCTURE CLASSIFICATION",
                }
            )
        elif mode is CandidatePresentationModeV1.TECHNICAL_REVIEW:
            base.update(
                {
                    "detector": self.detector.as_dict(),
                    "direction": self.candidate_key.direction.value,
                    "source_window_outcome": "WITHHELD_DURING_TECHNICAL_REVIEW",
                    "title": "REVIEWABLE DETECTOR CANDIDATE",
                }
            )
        else:
            from .detectors import DETECTOR_REGISTRY_V1

            declaration = DETECTOR_REGISTRY_V1.require(
                self.detector.detector_id,
                self.detector.version,
            )
            base.update(
                {
                    "detector": self.detector.as_dict(),
                    "detector_name": declaration.display_name,
                    "direction": self.candidate_key.direction.value,
                    "reveal_material_sha256": self.reveal_material.sha256,
                    "source_window_outcome": self.source_window_outcome.value,
                    "title": declaration.revealed_title,
                }
            )
        return base

    def issue_ground_truth_access(
        self,
        mode: CandidatePresentationModeV1,
    ) -> GroundTruthAccessGrantV1:
        if mode is not CandidatePresentationModeV1.REVEALED:
            raise PermissionError("ground-truth access is issued only after reveal")
        if self.ground_truth_summary is None:
            raise ValueError("candidate has no authoritative ground-truth summary")
        return GroundTruthAccessGrantV1(
            self.candidate_id,
            self.candidate_digest,
            self.reveal_material.sha256,
        )

    def protected_ground_truth(
        self,
        access: GroundTruthAccessGrantV1,
    ) -> dict[str, object]:
        if not isinstance(access, GroundTruthAccessGrantV1) or (
            access.candidate_id != self.candidate_id
            or access.candidate_digest != self.candidate_digest
            or access.reveal_material_sha256 != self.reveal_material.sha256
        ):
            raise PermissionError("ground-truth grant is not bound to this candidate")
        if self.ground_truth_summary is None:
            raise ValueError("candidate has no authoritative ground-truth summary")
        return self.ground_truth_summary.as_dict()

    def assert_review_sidecar(self, sidecar: HumanReviewSidecarV1) -> None:
        if not isinstance(sidecar, HumanReviewSidecarV1):
            raise TypeError("candidate review sidecar is invalid")
        if (
            sidecar.candidate_id != self.candidate_id
            or sidecar.candidate_digest != self.candidate_digest
        ):
            raise ValueError("review sidecar targets a different candidate")

    def as_dict(self) -> dict[str, object]:
        """Return the stable candidate record without protected truth or sidecars."""

        return {
            "capability_record": self.capability_record.as_dict(),
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "identity_projection": self.identity_projection(),
            "observable_feature_summary": self.observable_feature_summary.as_dict(),
            "protected_ground_truth": (
                "SEPARATE_CONTENT_ADDRESSED_RECORD"
                if self.ground_truth_summary is not None
                else None
            ),
            "reveal_material": self.reveal_material.as_dict(),
            "review_projection": self.review_projection(
                CandidatePresentationModeV1.TECHNICAL_REVIEW
            ),
            "schema_version": MINING_SCHEMA_VERSION_V1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def _validate_candidate_types(candidate: LessonCandidateV1) -> None:
    expected = (
        (candidate.source_ancestry, SourceAncestryV1, "source ancestry"),
        (candidate.candidate_key, CandidateKeyV1, "candidate key"),
        (candidate.detector, DetectorProjectionV1, "detector projection"),
        (candidate.bounds, CandidateBoundsV1, "candidate bounds"),
        (
            candidate.observable_feature_summary,
            ObservableFeatureSummaryV1,
            "observable feature summary",
        ),
        (
            candidate.difficulty_projection,
            DifficultyProjectionV1,
            "difficulty projection",
        ),
        (candidate.rarity_projection, RarityProjectionV1, "rarity projection"),
        (
            candidate.objective_projection,
            ObserveClassifyObjectiveV1,
            "objective projection",
        ),
        (candidate.reveal_material, RevealMaterialV1, "reveal material"),
        (candidate.capability_record, CapabilityRecordV1, "capability record"),
    )
    for value, expected_type, label in expected:
        if not isinstance(value, expected_type):
            raise TypeError(f"candidate {label} is invalid")
    if candidate.checkpoint is not None and not isinstance(
        candidate.checkpoint,
        CheckpointReferenceV1,
    ):
        raise TypeError("candidate checkpoint is invalid")
    if candidate.ground_truth_summary is not None and not isinstance(
        candidate.ground_truth_summary,
        GroundTruthSummaryV1,
    ):
        raise TypeError("candidate ground-truth summary is invalid")
    for value, expected_type, label in (
        (candidate.source_window_outcome, SourceWindowOutcomeV1, "window outcome"),
        (candidate.evidence_class, EvidenceClassV1, "evidence class"),
        (candidate.lesson_type, CandidateLessonTypeV1, "lesson type"),
        (candidate.proposal_state, CandidateProposalStateV1, "proposal state"),
    ):
        if not isinstance(value, expected_type):
            raise TypeError(f"candidate {label} is invalid")


def _strict_manifest_payload(
    raw: bytes,
    *,
    filename: str,
    manifest_id: str,
    expected_keys: set[str],
) -> tuple[dict[str, object], bytes]:
    """Parse one canonical, self-digesting WO33-A1 TOML policy document."""

    if type(raw) is not bytes:
        raise TypeError(f"{filename} input must be exact bytes")
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{filename} is not strict UTF-8 TOML") from error
    if type(payload) is not dict or set(payload) != expected_keys:
        raise ValueError(f"{filename} root fields differ from its closed schema")
    _validate_manifest_value(payload, filename)
    canonical = canonical_toml(payload).encode("utf-8")
    if canonical != raw:
        raise ValueError(f"{filename} is not canonical TOML")
    if (
        payload["schema_version"] != MINING_SCHEMA_VERSION_V1
        or payload["manifest_version"] != 1
        or payload["manifest_id"] != manifest_id
        or payload["policy_version"] != "LESSON_MINING_V1"
    ):
        raise ValueError(f"{filename} identity is not WO33-A1 V1")
    semantic_sha256 = payload["semantic_sha256"]
    manifest_sha256 = payload["manifest_sha256"]
    _require_digest(str(semantic_sha256), f"{filename} semantic digest")
    _require_digest(str(manifest_sha256), f"{filename} manifest digest")
    manifest_identity = {
        key: value for key, value in payload.items() if key != "manifest_sha256"
    }
    if sha256_json(manifest_identity) != manifest_sha256:
        raise ValueError(f"{filename} manifest digest is not reproducible")
    semantic_identity = {
        key: value
        for key, value in manifest_identity.items()
        if key not in {"manifest_version", "semantic_sha256"}
    }
    if sha256_json(semantic_identity) != semantic_sha256:
        raise ValueError(f"{filename} semantic digest is not reproducible")
    return payload, canonical


def _validate_manifest_value(value: object, label: str) -> None:
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"{label} contains non-NFC text")
        return
    if type(value) is list:
        for item in value:
            _validate_manifest_value(item, label)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ValueError(f"{label} contains an invalid table key")
            _validate_manifest_value(key, label)
            _validate_manifest_value(item, label)
        return
    raise TypeError(f"{label} contains a noncanonical value type")


def _require_manifest_table(
    payload: object,
    expected_keys: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise ValueError(f"{label} fields differ from its closed schema")
    return payload


def _require_digest_or_not_applicable(value: object, label: str) -> None:
    if value == _NOT_APPLICABLE:
        return
    if type(value) is not str:
        raise TypeError(f"{label} must be a digest or NOT_APPLICABLE")
    _require_digest(value, label)


def _require_relative_manifest_path(value: object, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty relative path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must be confined and normalized")
    return value


@dataclass(frozen=True, slots=True)
class DetectorThresholdsManifestV1:
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    detector_ids: tuple[str, ...]
    _payload: dict[str, object] = field(repr=False)
    _canonical_bytes: bytes = field(repr=False)

    def as_dict(self) -> dict[str, object]:
        return json.loads(canonical_json_bytes(self._payload).decode("ascii"))

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    def detector(self, detector_id: str) -> dict[str, object]:
        detectors = self._payload["detectors"]
        if not isinstance(detectors, Mapping) or detector_id not in detectors:
            raise KeyError(detector_id)
        row = detectors[detector_id]
        if not isinstance(row, Mapping):
            raise TypeError("detector threshold row is not a table")
        return json.loads(canonical_json_bytes(row).decode("ascii"))

    def detector_threshold_sha256(self, detector_id: str) -> str:
        return sha256_json(self.detector(detector_id))

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> DetectorThresholdsManifestV1:
        payload, canonical = _strict_manifest_payload(
            raw,
            filename="detector_thresholds.toml",
            manifest_id=DETECTOR_THRESHOLDS_MANIFEST_ID_V1,
            expected_keys={
                "arithmetic",
                "binning",
                "candidate_enumeration",
                "capability_bundles",
                "detector_order",
                "detectors",
                "evidence_scope",
                "execution_scope",
                "exclusions",
                "manifest_id",
                "manifest_sha256",
                "manifest_version",
                "policy_version",
                "schema_version",
                "semantic_sha256",
            },
        )
        order = payload["detector_order"]
        detectors = payload["detectors"]
        from .detectors import DETECTOR_IDS_V1

        if (
            type(order) is not list
            or any(type(item) is not str for item in order)
            or tuple(order) != DETECTOR_IDS_V1
            or not isinstance(detectors, Mapping)
            or set(detectors) != set(order)
        ):
            raise ValueError("detector threshold inventory is not exact")
        capability_bundles = payload["capability_bundles"]
        if not isinstance(capability_bundles, Mapping) or not capability_bundles:
            raise ValueError("detector capability bundles are absent")
        for bundle_id, inventory in capability_bundles.items():
            _require_identifier(str(bundle_id), "capability bundle ID")
            if (
                type(inventory) is not list
                or not inventory
                or any(type(item) is not str for item in inventory)
                or inventory
                != sorted(set(inventory), key=lambda item: item.encode("utf-8"))
            ):
                raise ValueError(f"{bundle_id} capability bundle is not canonical")
        exact_row_keys = {
            "ambiguity_rules",
            "capability_bundle",
            "detector_id",
            "evidence_classes",
            "exclusion_rules",
            "family",
            "horizons",
            "key_axes",
            "rule_expression",
            "rule_id",
            "sampling_unit",
            "special_rules",
            "thresholds",
            "version",
            "witness_kind",
        }
        for detector_id in order:
            row = detectors[detector_id]
            if not isinstance(row, Mapping) or set(row) != exact_row_keys:
                raise ValueError(f"{detector_id} threshold fields differ")
            if row["detector_id"] != detector_id or row["version"] != 1:
                raise ValueError(f"{detector_id} threshold identity differs")
            if row["capability_bundle"] not in capability_bundles:
                raise ValueError(f"{detector_id} names an unknown capability bundle")
            for name, allow_empty in (
                ("ambiguity_rules", False),
                ("evidence_classes", False),
                ("exclusion_rules", True),
                ("special_rules", True),
            ):
                values = row[name]
                if (
                    type(values) is not list
                    or (not allow_empty and not values)
                    or any(type(item) is not str or not item for item in values)
                    or len(set(values)) != len(values)
                ):
                    raise ValueError(f"{detector_id} {name} inventory differs")
            if (
                type(row["key_axes"]) is not list
                or len(row["key_axes"]) != 4
                or any(type(item) is not str or not item for item in row["key_axes"])
            ):
                raise ValueError(f"{detector_id} key axes differ")
            if any(item not in {"S", "H", "R"} for item in row["evidence_classes"]):
                raise ValueError(f"{detector_id} evidence class is unknown")
            horizons = _require_manifest_table(
                row["horizons"],
                {
                    "maximum_post_activation_horizon_us",
                    "maximum_pre_activation_lookback_us",
                    "required_persistence_us",
                },
                f"{detector_id} horizons",
            )
            if any(type(value) is not int or value < 0 for value in horizons.values()):
                raise ValueError(f"{detector_id} horizons must be nonnegative integers")
            for name in (
                "family",
                "rule_expression",
                "rule_id",
                "sampling_unit",
                "witness_kind",
            ):
                if type(row[name]) is not str or not row[name]:
                    raise ValueError(f"{detector_id} {name} must be nonempty text")
            thresholds = row["thresholds"]
            if type(thresholds) is not list or not thresholds:
                raise ValueError(f"{detector_id} has no operational thresholds")
            for threshold in thresholds:
                if not isinstance(threshold, Mapping) or set(threshold) != {
                    "name",
                    "operator",
                    "unit",
                    "value",
                }:
                    raise ValueError(f"{detector_id} threshold clause differs")
                if any(
                    type(threshold[name]) is not str or not threshold[name]
                    for name in ("name", "operator", "unit")
                ) or type(threshold["value"]) not in {int, str}:
                    raise TypeError(f"{detector_id} threshold values are not exact")
        return cls(
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            detector_ids=tuple(str(item) for item in order),
            _payload=payload,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class MiningPlanManifestV1:
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    _payload: dict[str, object] = field(repr=False)
    _canonical_bytes: bytes = field(repr=False)

    def as_dict(self) -> dict[str, object]:
        return json.loads(canonical_json_bytes(self._payload).decode("ascii"))

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> MiningPlanManifestV1:
        payload, canonical = _strict_manifest_payload(
            raw,
            filename="mining_plan.toml",
            manifest_id=MINING_PLAN_MANIFEST_ID_V1,
            expected_keys={
                "arithmetic",
                "candidate_shortfall",
                "deduplication",
                "deduplication_inputs",
                "detector_families",
                "difficulty",
                "diversity",
                "execution_scope",
                "manifest_id",
                "manifest_sha256",
                "manifest_version",
                "policy_version",
                "qualification_sources_manifest_sha256",
                "review_sampling",
                "sampling",
                "schema_version",
                "semantic_sha256",
                "threshold_manifest_sha256",
            },
        )
        for name in (
            "qualification_sources_manifest_sha256",
            "threshold_manifest_sha256",
        ):
            _require_digest(str(payload[name]), f"mining plan {name}")
        table_keys = {
            "arithmetic": {
                "clamp",
                "division",
                "fixed_point_scale",
                "jaccard_empty_empty_ppm",
                "jaccard_one_empty_ppm",
                "missing_required",
                "not_applicable_weighting",
                "ratio",
                "share",
            },
            "candidate_shortfall": {
                "duplicates_may_fill_quota",
                "event_five_gate",
                "quota_may_weaken_thresholds",
                "reserved_shortfall",
                "threshold_relaxation",
                "under_target_result",
            },
            "deduplication": {
                "candidate_order",
                "collapse",
                "difficulty_insufficient",
                "duplicate_of",
                "event_five_gram_jaccard_min_ppm",
                "feature_jaccard_min_ppm",
                "objective_jaccard_min_ppm",
                "regime_signature",
                "source_ancestry",
                "time_interval",
                "time_iou_min_ppm",
            },
            "deduplication_inputs": {
                "canonical_feature_value",
                "event_five_grams",
                "event_five_gram_order",
                "event_price_relations",
                "event_sides",
                "event_token",
                "feature_token",
                "feature_token_exclusions",
                "objective_set",
                "regime_fields",
                "regime_missing_metadata",
                "source_ancestry_encoding",
                "source_ancestry_fields",
                "spread_bands",
                "time_iou",
                "volume_liquidity_bands_ppm",
            },
            "difficulty": {
                "component_order",
                "evidence_quality_ppm",
                "formulas",
                "hidden_uncertainty_ppm",
                "input_rules",
                "policy_id",
                "positive_infinity_legibility_ppm",
                "weights_ppm",
            },
            "diversity": {
                "difficulty_bands_ppm",
                "dimension_values",
                "dimensions",
                "marginal_score",
                "novelty",
                "selection",
                "weights_ppm",
            },
            "execution_scope": {
                "candidate_mining",
                "candidate_outcomes",
                "detector_invocation",
                "human_review",
                "policy_action",
                "selection",
                "threshold_relaxation",
            },
            "review_sampling": {
                "event_material_distinctness",
                "global_fill",
                "reserved_counts",
                "selection_root",
                "source_order",
                "step_order",
                "target_count",
                "tie_context",
            },
            "sampling": {
                "alternate_units",
                "candidate_rarity",
                "denominator_exclusions",
                "denominator_zero",
                "eligible_default_unit",
                "frequency_ppm",
                "multiple_qualifying_keys_per_unit",
                "overall_detector_frequency",
                "population",
                "rarity_ppm",
            },
        }
        for name, keys in table_keys.items():
            _require_manifest_table(payload[name], keys, f"mining plan {name}")
        difficulty_bands = payload["diversity"]["difficulty_bands_ppm"]
        if type(difficulty_bands) is not list or len(difficulty_bands) != 4:
            raise ValueError("mining difficulty bands must contain four rows")
        for band in difficulty_bands:
            row = _require_manifest_table(
                band,
                {"lower_ppm", "upper_inclusive", "upper_ppm"},
                "mining difficulty band",
            )
            if (
                type(row["lower_ppm"]) is not int
                or type(row["upper_ppm"]) is not int
                or type(row["upper_inclusive"]) is not bool
                or not 0 <= row["lower_ppm"] < row["upper_ppm"] <= POLICY_SCALE_V1
            ):
                raise ValueError("mining difficulty band bounds are invalid")
        from .detectors import DETECTOR_IDS_V1

        families = payload["detector_families"]
        if not isinstance(families, Mapping) or set(families) != {
            "ABSORPTION",
            "EXECUTION",
            "FLOW",
            "FRAGMENTATION",
            "PRICE_LIQUIDITY",
            "QUEUE",
            "SESSION",
        }:
            raise ValueError("mining detector families differ from the closed V1 set")
        family_members = [item for members in families.values() for item in members]
        if (
            any(type(members) is not list or not members for members in families.values())
            or len(family_members) != len(set(family_members))
            or set(family_members) != set(DETECTOR_IDS_V1)
        ):
            raise ValueError("mining detector families do not partition the registry")
        return cls(
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            _payload=payload,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class QualificationSourceRowV1:
    row_id: str
    stratum: str
    source: dict[str, object]
    adapter: dict[str, object]
    configuration: dict[str, object]
    identity: dict[str, object]
    bounds: dict[str, object]
    capabilities: dict[str, object]
    provenance: dict[str, object]
    execution: dict[str, object]

    @classmethod
    def from_dict(
        cls,
        row_id: str,
        payload: Mapping[str, object],
    ) -> QualificationSourceRowV1:
        if set(payload) != {
            "adapter",
            "bounds",
            "capabilities",
            "configuration",
            "execution",
            "identity",
            "provenance",
            "row_id",
            "source",
            "stratum",
        }:
            raise ValueError(f"qualification source {row_id} fields differ")
        table_keys = {
            "source": {
                "compiled_artifact_sha256",
                "example_adapter_id",
                "example_adapter_version",
                "example_path",
                "example_selected_root_seed",
                "example_target_kind",
                "raw_bytes_length",
                "raw_sha256",
                "semantic_plan_sha256",
                "source_bundle_sha256",
            },
            "adapter": {
                "checkpoint_adapter_id",
                "generator_adapter_id",
                "generator_adapter_version",
                "generator_target_kind",
            },
            "configuration": {
                "bytes_format",
                "bytes_json",
                "bytes_length",
                "bytes_sha256",
                "native_payload_canonical_sha256",
                "native_payload_path",
                "native_payload_raw_bytes_length",
                "native_payload_raw_sha256",
            },
            "identity": {
                "evidence_class",
                "expected_final_state_sha256",
                "expected_native_run_digest",
                "expected_replay_digest",
                "qualification_profile_id",
                "qualification_root_seed",
                "source_id",
                "source_kind",
                "source_selected_root_seed",
                "source_sha256",
            },
            "bounds": {"source_end_us", "source_start_us"},
            "capabilities": {
                "adapter_contract_sha256",
                "provided",
                "required",
            },
            "provenance": {
                "checkpoint_sha256",
                "event_prefix_sha256",
                "evidence_run_id",
                "full_day_run_id",
                "parent_artifact_path",
                "parent_artifact_sha256",
                "parent_selector",
                "plan_sha256",
                "workload_sha256",
            },
            "execution": {
                "candidate_outcomes_inspected",
                "protected_seed_access",
                "qualification_root_application",
                "replay_verification",
                "source_generation",
            },
        }
        values: dict[str, dict[str, object]] = {}
        for name, keys in table_keys.items():
            table = payload[name]
            if not isinstance(table, Mapping) or set(table) != keys:
                raise ValueError(f"qualification source {row_id} {name} fields differ")
            values[name] = dict(table)
        if payload["row_id"] != row_id:
            raise ValueError(f"qualification source {row_id} identity differs")
        _require_nfc(str(payload["stratum"]), f"qualification source {row_id} stratum")
        source = values["source"]
        _require_relative_manifest_path(
            source["example_path"],
            f"qualification source {row_id} example path",
        )
        for name in (
            "compiled_artifact_sha256",
            "raw_sha256",
            "semantic_plan_sha256",
            "source_bundle_sha256",
        ):
            if type(source[name]) is not str:
                raise TypeError(f"qualification source {row_id} {name} is not text")
            _require_digest(source[name], f"qualification source {row_id} {name}")
        for name in (
            "example_adapter_version",
            "example_selected_root_seed",
            "raw_bytes_length",
        ):
            if type(source[name]) is not int or source[name] < 0:
                raise ValueError(f"qualification source {row_id} {name} is invalid")
        if source["example_adapter_version"] != 1 or source["raw_bytes_length"] == 0:
            raise ValueError(f"qualification source {row_id} example identity differs")
        for name in ("example_adapter_id", "example_target_kind"):
            _require_identifier(str(source[name]), f"qualification source {row_id} {name}")
        adapter = values["adapter"]
        for name in (
            "checkpoint_adapter_id",
            "generator_adapter_id",
            "generator_target_kind",
        ):
            _require_identifier(str(adapter[name]), f"qualification source {row_id} {name}")
        if adapter["generator_adapter_version"] != 1:
            raise ValueError(f"qualification source {row_id} adapter version differs")
        config = values["configuration"]
        config_text = config["bytes_json"]
        if type(config_text) is not str:
            raise TypeError(f"qualification source {row_id} config bytes are not text")
        config_raw = config_text.encode("utf-8")
        try:
            config_payload = json.loads(config_text)
        except json.JSONDecodeError as error:
            raise ValueError(f"qualification source {row_id} config is not JSON") from error
        if not isinstance(config_payload, Mapping):
            raise TypeError(f"qualification source {row_id} config is not an object")
        if canonical_json_bytes(config_payload) != config_raw:
            raise ValueError(f"qualification source {row_id} config is not canonical JSON")
        if (
            config["bytes_format"] != "CANONICAL_JSON_V1"
            or config["bytes_length"] != len(config_raw)
            or config["bytes_sha256"] != hashlib.sha256(config_raw).hexdigest()
        ):
            raise ValueError(f"qualification source {row_id} config digest differs")
        _require_digest(
            str(config["native_payload_canonical_sha256"]),
            f"qualification source {row_id} native payload digest",
        )
        _require_relative_manifest_path(
            config["native_payload_path"],
            f"qualification source {row_id} native payload path",
        )
        if (
            type(config["native_payload_raw_bytes_length"]) is not int
            or config["native_payload_raw_bytes_length"] < 0
        ):
            raise ValueError(f"qualification source {row_id} native length is invalid")
        _require_digest_or_not_applicable(
            config["native_payload_raw_sha256"],
            f"qualification source {row_id} native raw digest",
        )
        if (
            config["native_payload_raw_bytes_length"] == 0
        ) != (config["native_payload_raw_sha256"] == _NOT_APPLICABLE):
            raise ValueError(f"qualification source {row_id} native raw identity differs")
        identity = values["identity"]
        if identity["evidence_class"] not in {"S", "H", "R"}:
            raise ValueError(f"qualification source {row_id} evidence class is unknown")
        if identity["source_kind"] not in {"RUN", "DATASET", "RECONSTRUCTION"}:
            raise ValueError(f"qualification source {row_id} source kind is unknown")
        for name in (
            "expected_final_state_sha256",
            "expected_native_run_digest",
            "expected_replay_digest",
        ):
            _require_digest_or_not_applicable(
                identity[name],
                f"qualification source {row_id} {name}",
            )
        _require_digest(
            str(identity["source_sha256"]),
            f"qualification source {row_id} source digest",
        )
        for name in ("qualification_profile_id", "source_id"):
            _require_nfc(str(identity[name]), f"qualification source {row_id} {name}")
        root_seed = identity["qualification_root_seed"]
        if not (
            (type(root_seed) is int and root_seed >= 0)
            or root_seed == _NOT_APPLICABLE
        ):
            raise ValueError(f"qualification source {row_id} root seed is invalid")
        if (
            type(identity["source_selected_root_seed"]) is not int
            or identity["source_selected_root_seed"] < 0
        ):
            raise ValueError(f"qualification source {row_id} source seed is invalid")
        bounds = values["bounds"]
        if (
            type(bounds["source_start_us"]) is not int
            or type(bounds["source_end_us"]) is not int
            or bounds["source_start_us"] < 0
            or bounds["source_end_us"] <= bounds["source_start_us"]
        ):
            raise ValueError(f"qualification source {row_id} bounds are invalid")
        capabilities = values["capabilities"]
        _require_digest(
            str(capabilities["adapter_contract_sha256"]),
            f"qualification source {row_id} adapter contract digest",
        )
        for name in ("required", "provided"):
            inventory = capabilities[name]
            if (
                type(inventory) is not list
                or any(type(item) is not str for item in inventory)
                or inventory != sorted(set(inventory), key=lambda item: item.encode("utf-8"))
            ):
                raise ValueError(f"qualification source {row_id} capabilities differ")
        if not set(capabilities["required"]).issubset(capabilities["provided"]):
            raise ValueError(f"qualification source {row_id} lacks a required capability")
        provenance = values["provenance"]
        _require_relative_manifest_path(
            provenance["parent_artifact_path"],
            f"qualification source {row_id} parent artifact path",
        )
        _require_digest(
            str(provenance["parent_artifact_sha256"]),
            f"qualification source {row_id} parent artifact digest",
        )
        for name in (
            "checkpoint_sha256",
            "event_prefix_sha256",
            "plan_sha256",
            "workload_sha256",
        ):
            _require_digest_or_not_applicable(
                provenance[name],
                f"qualification source {row_id} {name}",
            )
        for name in (
            "evidence_run_id",
            "full_day_run_id",
            "parent_selector",
        ):
            _require_nfc(str(provenance[name]), f"qualification source {row_id} {name}")
        execution = values["execution"]
        for name, value in execution.items():
            _require_nfc(str(value), f"qualification source {row_id} execution {name}")
        return cls(
            row_id=row_id,
            stratum=str(payload["stratum"]),
            source=values["source"],
            adapter=values["adapter"],
            configuration=config,
            identity=values["identity"],
            bounds=bounds,
            capabilities=capabilities,
            provenance=provenance,
            execution=execution,
        )


@dataclass(frozen=True, slots=True)
class QualificationSourcesManifestV1:
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    rows: tuple[QualificationSourceRowV1, ...]
    _payload: dict[str, object] = field(repr=False)
    _canonical_bytes: bytes = field(repr=False)

    def as_dict(self) -> dict[str, object]:
        return json.loads(canonical_json_bytes(self._payload).decode("ascii"))

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    def row(self, row_id: str) -> QualificationSourceRowV1:
        for row in self.rows:
            if row.row_id == row_id:
                return row
        raise KeyError(row_id)

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> QualificationSourcesManifestV1:
        payload, canonical = _strict_manifest_payload(
            raw,
            filename="qualification_sources.toml",
            manifest_id=QUALIFICATION_SOURCES_MANIFEST_ID_V1,
            expected_keys={
                "execution_scope",
                "manifest_id",
                "manifest_sha256",
                "manifest_version",
                "policy_version",
                "row_order",
                "rows",
                "schema_version",
                "semantic_sha256",
            },
        )
        order = payload["row_order"]
        rows = payload["rows"]
        if (
            order != ["quiet", "event", "hidden", "fragmented", "historical"]
            or not isinstance(rows, Mapping)
            or set(rows) != set(order)
        ):
            raise ValueError("qualification source matrix is not the fixed five rows")
        typed_rows = tuple(
            QualificationSourceRowV1.from_dict(row_id, rows[row_id])
            for row_id in order
            if isinstance(rows[row_id], Mapping)
        )
        if len(typed_rows) != 5:
            raise ValueError("qualification source matrix did not parse five rows")
        return cls(
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            rows=typed_rows,
            _payload=payload,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class MiningPolicyBundleV1:
    thresholds: DetectorThresholdsManifestV1
    plan: MiningPlanManifestV1
    sources: QualificationSourcesManifestV1

    def __post_init__(self) -> None:
        plan = self.plan.as_dict()
        if (
            plan["threshold_manifest_sha256"]
            != self.thresholds.manifest_sha256
            or plan["qualification_sources_manifest_sha256"]
            != self.sources.manifest_sha256
        ):
            raise ValueError("mining plan does not bind both preregistered manifests")

    @property
    def bundle_sha256(self) -> str:
        return sha256_json(
            {
                "mining_plan_manifest_sha256": self.plan.manifest_sha256,
                "qualification_sources_manifest_sha256": self.sources.manifest_sha256,
                "threshold_manifest_sha256": self.thresholds.manifest_sha256,
            }
        )


def load_detector_thresholds() -> DetectorThresholdsManifestV1:
    raw = files("kirby2.mining").joinpath("detector_thresholds.toml").read_bytes()
    return DetectorThresholdsManifestV1.from_toml_bytes(raw)


def load_mining_plan() -> MiningPlanManifestV1:
    raw = files("kirby2.mining").joinpath("mining_plan.toml").read_bytes()
    return MiningPlanManifestV1.from_toml_bytes(raw)


def load_qualification_sources() -> QualificationSourcesManifestV1:
    raw = files("kirby2.mining").joinpath(
        "fixtures/qualification_sources.toml"
    ).read_bytes()
    return QualificationSourcesManifestV1.from_toml_bytes(raw)


def load_mining_policy_bundle() -> MiningPolicyBundleV1:
    return MiningPolicyBundleV1(
        thresholds=load_detector_thresholds(),
        plan=load_mining_plan(),
        sources=load_qualification_sources(),
    )


__all__ = [
    "DETECTOR_THRESHOLDS_MANIFEST_ID_V1",
    "LESSON_CANDIDATE_ID_PREFIX_V1",
    "MINING_PLAN_MANIFEST_ID_V1",
    "MINING_SCHEMA_VERSION_V1",
    "POLICY_SCALE_V1",
    "QUALIFICATION_SOURCES_MANIFEST_ID_V1",
    "SESSION_PHASE_VALUES_V1",
    "CandidateBoundsV1",
    "CandidateDirectionV1",
    "CandidateKeyV1",
    "CandidateLessonTypeV1",
    "CandidatePresentationModeV1",
    "CandidateProposalStateV1",
    "CandidateSideV1",
    "CapabilityEvidenceKindV1",
    "CapabilityEvidenceReferenceV1",
    "CapabilityRecordRowV1",
    "CapabilityRecordV1",
    "CheckpointReferenceV1",
    "DetectorProjectionV1",
    "DetectorThresholdsManifestV1",
    "DifficultyEstimateStateV1",
    "DifficultyProjectionV1",
    "EvidenceClassV1",
    "GroundTruthAccessGrantV1",
    "GroundTruthSummaryV1",
    "HumanReviewDecisionV1",
    "HumanReviewSidecarV1",
    "LessonCandidateV1",
    "MiningPlanManifestV1",
    "MiningPolicyBundleV1",
    "ObservableFeatureSummaryV1",
    "ObserveClassifyObjectiveV1",
    "RarityProjectionV1",
    "RegimeSignatureV1",
    "RevealMaterialV1",
    "SourceAncestryV1",
    "SourceIdentityV1",
    "SourceKindV1",
    "SourceWindowOutcomeV1",
    "QualificationSourceRowV1",
    "QualificationSourcesManifestV1",
    "canonical_json_bytes",
    "load_detector_thresholds",
    "load_mining_plan",
    "load_mining_policy_bundle",
    "load_qualification_sources",
    "ratio_ppm",
    "round_div_even",
    "sha256_json",
    "unsigned_share_ppm",
]
