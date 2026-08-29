"""Versioned detector declarations and fail-closed capability admission.

This module does not implement mining.  It freezes the WO33 detector registry and
answers only whether a named detector may be exercised against declared evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .models import (
    MINING_SCHEMA_VERSION_V1,
    CapabilityRecordRowV1,
    CapabilityRecordV1,
    DetectorProjectionV1,
    EvidenceClassV1,
    SourceIdentityV1,
    canonical_json_bytes,
    sha256_json,
)
from .skills import SKILL_REGISTRY_V1


DETECTOR_REGISTRY_ID_V1 = "LESSON_DETECTOR_REGISTRY_V1"
DETECTOR_REGISTRY_VERSION_V1 = 1
_HISTORICAL_EVIDENCE_FOUNDATION_V1 = (
    "COMPLETE_EVENT_SEQUENCE",
    "DEPTH_DELTAS",
    "DEPTH_SNAPSHOTS",
    "MICROSECOND_TIMESTAMP_PRECISION",
    "ORDER_IDENTITY",
    "QUOTES",
    "SESSION_EVENTS",
)


class DetectorSupportStatusV1(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    NOT_EXERCISED = "NOT_EXERCISED"


class UnknownDetectorReferenceV1(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DetectorDeclarationV1:
    detector_id: str
    version: int
    display_name: str
    revealed_title: str
    required_capabilities: tuple[str, ...]
    supported_evidence_classes: tuple[EvidenceClassV1, ...]
    primary_skill_id: str
    supporting_skill_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        DetectorProjectionV1(self.detector_id, self.version, "0" * 64)
        if not self.display_name.strip() or not self.revealed_title.strip():
            raise ValueError("detector names must not be empty")
        _require_sorted_unique(self.required_capabilities, "detector capabilities")
        if (
            type(self.supported_evidence_classes) is not tuple
            or not self.supported_evidence_classes
            or any(
                not isinstance(item, EvidenceClassV1)
                for item in self.supported_evidence_classes
            )
        ):
            raise ValueError("detector evidence support is invalid")
        evidence_order = tuple(EvidenceClassV1)
        expected = tuple(
            item for item in evidence_order if item in self.supported_evidence_classes
        )
        if self.supported_evidence_classes != expected:
            raise ValueError("detector evidence classes must use S/H/R canonical order")
        SKILL_REGISTRY_V1.require(self.primary_skill_id)
        _require_sorted_unique(self.supporting_skill_ids, "detector supporting skills")
        for skill_id in self.supporting_skill_ids:
            SKILL_REGISTRY_V1.require(skill_id)
        if self.primary_skill_id in self.supporting_skill_ids:
            raise ValueError("detector primary skill cannot be supporting")

    @property
    def supports_synthetic_ground_truth(self) -> bool:
        return EvidenceClassV1.SYNTHETIC_GROUND_TRUTH in self.supported_evidence_classes

    @property
    def supports_historical(self) -> bool:
        return EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER in self.supported_evidence_classes

    @property
    def supports_reconstruction(self) -> bool:
        return (
            EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL
            in self.supported_evidence_classes
        )

    @property
    def hidden_liquidity_relevant(self) -> bool:
        return self.primary_skill_id == "HIDDEN_LIQUIDITY"

    def required_capabilities_for(
        self,
        evidence_class: EvidenceClassV1,
    ) -> tuple[str, ...]:
        if evidence_class not in self.supported_evidence_classes:
            return self.required_capabilities
        foundation = (
            _HISTORICAL_EVIDENCE_FOUNDATION_V1
            if evidence_class is EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER
            else ()
        )
        return tuple(
            sorted(
                set((*self.required_capabilities, *foundation)),
                key=lambda item: item.encode("utf-8"),
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "hidden_liquidity_relevant": self.hidden_liquidity_relevant,
            "id": self.detector_id,
            "primary_skill_id": self.primary_skill_id,
            "required_capabilities": list(self.required_capabilities),
            "required_capabilities_by_evidence_class": {
                item.value: list(self.required_capabilities_for(item))
                for item in self.supported_evidence_classes
            },
            "revealed_title": self.revealed_title,
            "supporting_skill_ids": list(self.supporting_skill_ids),
            "supports_historical": self.supports_historical,
            "supports_reconstruction": self.supports_reconstruction,
            "supports_synthetic_ground_truth": (
                self.supports_synthetic_ground_truth
            ),
            "version": self.version,
        }


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{label} must not be empty")
    if any(type(value) is not str or not value for value in values):
        raise ValueError(f"{label} contains invalid text")
    if len(set(values)) != len(values) or values != tuple(
        sorted(values, key=lambda item: item.encode("utf-8"))
    ):
        raise ValueError(f"{label} must be unique and NFC-byte sorted")


@dataclass(frozen=True, slots=True)
class SourceCapabilityInventoryV1:
    source_identity: SourceIdentityV1
    evidence_class: EvidenceClassV1
    available_records: tuple[CapabilityRecordRowV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_identity, SourceIdentityV1):
            raise TypeError("source capability identity is invalid")
        if not isinstance(self.evidence_class, EvidenceClassV1):
            raise TypeError("source evidence class is invalid")
        if type(self.available_records) is not tuple:
            raise TypeError("source capability inventory must be a tuple")
        if any(
            not isinstance(item, CapabilityRecordRowV1)
            for item in self.available_records
        ):
            raise TypeError("source capability inventory row is invalid")
        names = tuple(item.capability for item in self.available_records)
        if len(set(names)) != len(names) or names != tuple(sorted(names)):
            raise ValueError("source capabilities must be unique and sorted")
        if (
            self.evidence_class is EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER
            and self.source_identity.kind.value != "DATASET"
        ):
            raise ValueError("historical evidence class requires DATASET identity")
        if (
            self.evidence_class
            is EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL
            and self.source_identity.kind.value != "RECONSTRUCTION"
        ):
            raise ValueError(
                "reconstruction evidence class requires RECONSTRUCTION identity"
            )
        if self.evidence_class is EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER:
            missing_foundation = set(_HISTORICAL_EVIDENCE_FOUNDATION_V1).difference(
                names
            )
            if missing_foundation:
                raise ValueError(
                    "historical evidence class requires market-by-order foundation"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "available_records": [item.as_dict() for item in self.available_records],
            "evidence_class": self.evidence_class.value,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_identity": self.source_identity.as_dict(),
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class DetectorSupportDecisionV1:
    detector: DetectorDeclarationV1
    status: DetectorSupportStatusV1
    reason_code: str | None
    missing_capabilities: tuple[str, ...]
    capability_record: CapabilityRecordV1 | None

    def __post_init__(self) -> None:
        if not isinstance(self.detector, DetectorDeclarationV1):
            raise TypeError("detector support declaration is invalid")
        if not isinstance(self.status, DetectorSupportStatusV1):
            raise TypeError("detector support status is invalid")
        if self.status is DetectorSupportStatusV1.ELIGIBLE:
            if (
                self.reason_code is not None
                or self.missing_capabilities
                or self.capability_record is None
            ):
                raise ValueError("eligible detector support has refusal evidence")
        else:
            if self.reason_code not in {
                "UNSUPPORTED_EVIDENCE_CLASS",
                "INSUFFICIENT_SOURCE_CAPABILITY",
            } or self.capability_record is not None:
                raise ValueError("NOT_EXERCISED detector support is malformed")

    @property
    def exercised(self) -> bool:
        return self.status is DetectorSupportStatusV1.ELIGIBLE

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_record_sha256": (
                None
                if self.capability_record is None
                else self.capability_record.sha256
            ),
            "detector_id": self.detector.detector_id,
            "detector_version": self.detector.version,
            "missing_capabilities": list(self.missing_capabilities),
            "reason_code": self.reason_code,
            "status": self.status.value,
        }


class DetectorRegistryV1:
    __slots__ = ("_declarations", "_mapping")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("detector registry is immutable")

    def __init__(self, declarations: tuple[DetectorDeclarationV1, ...]) -> None:
        if type(declarations) is not tuple or not declarations:
            raise ValueError("detector registry requires declarations")
        if any(not isinstance(item, DetectorDeclarationV1) for item in declarations):
            raise TypeError("detector registry declaration is invalid")
        ordered = tuple(
            sorted(
                declarations,
                key=lambda item: (item.detector_id.encode("utf-8"), item.version),
            )
        )
        keys = tuple((item.detector_id, item.version) for item in ordered)
        if len(set(keys)) != len(keys):
            raise ValueError("detector registry IDs and versions must be unique")
        object.__setattr__(self, "_declarations", ordered)
        object.__setattr__(
            self,
            "_mapping",
            MappingProxyType(
                {(item.detector_id, item.version): item for item in ordered}
            ),
        )

    @property
    def declarations(self) -> tuple[DetectorDeclarationV1, ...]:
        return self._declarations

    def require(self, detector_id: str, version: int = 1) -> DetectorDeclarationV1:
        declaration = self._mapping.get((detector_id, version))
        if declaration is None:
            raise UnknownDetectorReferenceV1(
                f"UNKNOWN_DETECTOR_REFERENCE: {detector_id}@{version}"
            )
        return declaration

    def assess(
        self,
        detector_id: str,
        version: int,
        threshold_sha256: str,
        source: SourceCapabilityInventoryV1,
    ) -> DetectorSupportDecisionV1:
        declaration = self.require(detector_id, version)
        projection = DetectorProjectionV1(detector_id, version, threshold_sha256)
        if source.evidence_class not in declaration.supported_evidence_classes:
            return DetectorSupportDecisionV1(
                declaration,
                DetectorSupportStatusV1.NOT_EXERCISED,
                "UNSUPPORTED_EVIDENCE_CLASS",
                (),
                None,
            )
        by_name = {item.capability: item for item in source.available_records}
        required_capabilities = declaration.required_capabilities_for(
            source.evidence_class
        )
        missing = tuple(
            capability
            for capability in required_capabilities
            if capability not in by_name
        )
        if missing:
            return DetectorSupportDecisionV1(
                declaration,
                DetectorSupportStatusV1.NOT_EXERCISED,
                "INSUFFICIENT_SOURCE_CAPABILITY",
                missing,
                None,
            )
        record = CapabilityRecordV1(
            source.source_identity,
            projection,
            tuple(by_name[name] for name in required_capabilities),
        )
        return DetectorSupportDecisionV1(
            declaration,
            DetectorSupportStatusV1.ELIGIBLE,
            None,
            (),
            record,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "detectors": [item.as_dict() for item in self._declarations],
            "registry_id": DETECTOR_REGISTRY_ID_V1,
            "registry_version": DETECTOR_REGISTRY_VERSION_V1,
            "schema_version": MINING_SCHEMA_VERSION_V1,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


def _bundle(*capabilities: str) -> tuple[str, ...]:
    return tuple(sorted(set(capabilities), key=lambda item: item.encode("utf-8")))


Q2_CAPABILITIES_V1 = _bundle(
    "COMPLETE_EVENT_SEQUENCE",
    "DEPTH_DELTAS",
    "DEPTH_SNAPSHOTS",
    "MICROSECOND_TIMESTAMP_PRECISION",
    "QUOTES",
    "SESSION_EVENTS",
)
TQ_CAPABILITIES_V1 = _bundle(
    "COMPLETE_EVENT_SEQUENCE",
    "MICROSECOND_TIMESTAMP_PRECISION",
    "QUOTES",
    "SESSION_EVENTS",
    "TRADES",
    "TRADE_AGGRESSOR_SIDE",
)
MBO_CAPABILITIES_V1 = _bundle(*Q2_CAPABILITIES_V1, "ORDER_IDENTITY")
MBO_TQ_CAPABILITIES_V1 = _bundle(*MBO_CAPABILITIES_V1, *TQ_CAPABILITIES_V1)
HID_CAPABILITIES_V1 = _bundle(
    *MBO_CAPABILITIES_V1,
    "AUTHORITATIVE_RESERVE_REFRESH_LABELS",
)
LAT_CAPABILITIES_V1 = _bundle(
    *TQ_CAPABILITIES_V1,
    "DETERMINISTIC_LATENCY_INTERVENTION",
    "PORTABLE_CHECKPOINT",
)
LAT_MBO_CAPABILITIES_V1 = _bundle(*LAT_CAPABILITIES_V1, *MBO_CAPABILITIES_V1)
VEN_CAPABILITIES_V1 = _bundle(
    "COMPLETE_EVENT_SEQUENCE",
    "EXECUTABLE_DEPTH",
    "FEE_SCHEDULE",
    "PER_VENUE_QUOTES",
    "RECEIPT_LATENCY_MODEL",
)
AUC_CAPABILITIES_V1 = _bundle(
    "AUCTION_STATE",
    "COMPLETE_EVENT_SEQUENCE",
    "INDICATIVE_PRICE",
    "PUBLISHED_IMBALANCE",
)
HALT_CAPABILITIES_V1 = _bundle(
    "COMPLETE_EVENT_SEQUENCE",
    "HALT_STATE",
    "QUOTES",
    "TRADES",
)
PART_CAPABILITIES_V1 = _bundle(
    *TQ_CAPABILITIES_V1,
    "AUTHORITATIVE_PARTICIPANT_IDENTITY",
)

_S = EvidenceClassV1.SYNTHETIC_GROUND_TRUTH
_H = EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER
_R = EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL


def _detector(
    detector_id: str,
    display_name: str,
    capabilities: tuple[str, ...],
    evidence: tuple[EvidenceClassV1, ...],
    primary: str,
    supporting: tuple[str, ...],
) -> DetectorDeclarationV1:
    return DetectorDeclarationV1(
        detector_id=detector_id,
        version=1,
        display_name=display_name,
        revealed_title=f"Recognize {display_name}",
        required_capabilities=capabilities,
        supported_evidence_classes=evidence,
        primary_skill_id=primary,
        supporting_skill_ids=tuple(sorted(supporting)),
    )


_DETECTORS = (
    _detector("STRONG_QUEUE_IMBALANCE", "strong queue imbalance", Q2_CAPABILITIES_V1, (_S, _H), "BOOK_READING", ("QUEUE_POSITION",)),
    _detector("QUEUE_DEPLETION", "queue depletion", Q2_CAPABILITIES_V1, (_S, _H), "QUEUE_POSITION", ("BOOK_READING",)),
    _detector("QUEUE_REPLENISHMENT", "queue replenishment", MBO_CAPABILITIES_V1, (_S, _H), "ABSORPTION_RECOGNITION", ("QUEUE_POSITION", "TAPE_READING")),
    _detector("BID_ABSORPTION", "bid absorption", MBO_TQ_CAPABILITIES_V1, (_S, _H), "ABSORPTION_RECOGNITION", ("BOOK_READING", "TAPE_READING")),
    _detector("ASK_ABSORPTION", "ask absorption", MBO_TQ_CAPABILITIES_V1, (_S, _H), "ABSORPTION_RECOGNITION", ("BOOK_READING", "TAPE_READING")),
    _detector("FAILED_BREAKOUT", "failed breakout", TQ_CAPABILITIES_V1, (_S, _H, _R), "REGIME_RECOGNITION", ("EXIT_EXECUTION", "TAPE_READING")),
    _detector("LIQUIDITY_VACUUM", "liquidity vacuum", MBO_TQ_CAPABILITIES_V1, (_S, _H), "LIQUIDITY_WITHDRAWAL", ("BOOK_READING", "POSITION_MANAGEMENT")),
    _detector("SPREAD_EXPANSION", "spread expansion", TQ_CAPABILITIES_V1, (_S, _H, _R), "SPREAD_DECISION", ("BOOK_READING",)),
    _detector("SPREAD_RECOVERY", "spread recovery", TQ_CAPABILITIES_V1, (_S, _H, _R), "SPREAD_DECISION", ("REGIME_RECOGNITION",)),
    _detector("AGGRESSIVE_FLOW_BURST", "aggressive-flow burst", TQ_CAPABILITIES_V1, (_S, _H, _R), "TAPE_READING", ("AGGRESSIVE_ENTRY", "VOLUME_CONTEXT")),
    _detector("CANCELLATION_BURST", "cancellation burst", MBO_CAPABILITIES_V1, (_S, _H), "LIQUIDITY_WITHDRAWAL", ("QUEUE_POSITION",)),
    _detector("HIDDEN_RESERVE_REFRESH", "hidden reserve refresh", HID_CAPABILITIES_V1, (_S, _H), "HIDDEN_LIQUIDITY", ("ABSORPTION_RECOGNITION", "BOOK_READING")),
    _detector("APPARENT_LIQUIDITY_MIRAGE", "apparent liquidity mirage", MBO_CAPABILITIES_V1, (_S, _H), "HIDDEN_LIQUIDITY", ("BOOK_READING", "SCRIPT_DISCIPLINE")),
    _detector("LATENCY_SENSITIVE_OPPORTUNITY", "latency-sensitive opportunity", LAT_CAPABILITIES_V1, (_S, _R), "LATENCY_AWARENESS", ("PASSIVE_ENTRY",)),
    _detector("CANCEL_FILL_RACE", "cancel/fill race", LAT_MBO_CAPABILITIES_V1, (_S, _R), "CANCEL_TIMING", ("LATENCY_AWARENESS", "PARTIAL_FILL_MANAGEMENT")),
    _detector("MULTI_VENUE_FRAGMENTATION", "multi-venue fragmentation", VEN_CAPABILITIES_V1, (_S, _H), "MULTI_VENUE_ROUTING", ("BOOK_READING",)),
    _detector("ROUTING_DILEMMA", "routing dilemma", VEN_CAPABILITIES_V1, (_S, _R), "MULTI_VENUE_ROUTING", ("LATENCY_AWARENESS", "SPREAD_DECISION")),
    _detector("AUCTION_IMBALANCE_CHANGE", "auction imbalance change", AUC_CAPABILITIES_V1, (_S, _H), "AUCTION_EXECUTION", ("BOOK_READING",)),
    _detector("HALT_REOPENING", "halt/reopening", HALT_CAPABILITIES_V1, (_S, _H, _R), "HALT_REOPENING", ("SCRIPT_DISCIPLINE",)),
    _detector("DISTRESSED_LIQUIDATION", "distressed liquidation", PART_CAPABILITIES_V1, (_S, _H), "POSITION_MANAGEMENT", ("EXIT_EXECUTION", "TAPE_READING")),
    _detector("MOMENTUM_EXHAUSTION", "momentum exhaustion", TQ_CAPABILITIES_V1, (_S, _H, _R), "REGIME_RECOGNITION", ("EXIT_EXECUTION", "TAPE_READING")),
    _detector("MEAN_REVERSION_TRANSITION", "mean-reversion transition", TQ_CAPABILITIES_V1, (_S, _H, _R), "REGIME_RECOGNITION", ("SPREAD_DECISION", "TAPE_READING")),
)

DETECTOR_REGISTRY_V1 = DetectorRegistryV1(_DETECTORS)
DETECTOR_IDS_V1 = tuple(
    declaration.detector_id for declaration in DETECTOR_REGISTRY_V1.declarations
)


__all__ = [
    "AUC_CAPABILITIES_V1",
    "DETECTOR_IDS_V1",
    "DETECTOR_REGISTRY_ID_V1",
    "DETECTOR_REGISTRY_VERSION_V1",
    "DETECTOR_REGISTRY_V1",
    "HALT_CAPABILITIES_V1",
    "HID_CAPABILITIES_V1",
    "LAT_CAPABILITIES_V1",
    "LAT_MBO_CAPABILITIES_V1",
    "MBO_CAPABILITIES_V1",
    "MBO_TQ_CAPABILITIES_V1",
    "PART_CAPABILITIES_V1",
    "Q2_CAPABILITIES_V1",
    "TQ_CAPABILITIES_V1",
    "VEN_CAPABILITIES_V1",
    "DetectorDeclarationV1",
    "DetectorRegistryV1",
    "DetectorSupportDecisionV1",
    "DetectorSupportStatusV1",
    "SourceCapabilityInventoryV1",
    "UnknownDetectorReferenceV1",
]
