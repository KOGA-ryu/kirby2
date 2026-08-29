"""Deterministic runtime contracts for operational lesson detectors.

WO33-B1/B2 evaluate canonical detector opportunities.  Source-specific adapters are
responsible for producing those opportunities from immutable event streams; this
runtime owns capability admission, threshold binding, canonical ordering, explicit
denominators, exclusions, timing-safe assessment projection, and immutable findings.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from .detectors import (
    DETECTOR_REGISTRY_V1,
    DetectorSupportStatusV1,
    SourceCapabilityInventoryV1,
)
from .models import (
    CandidateBoundsV1,
    CandidateDirectionV1,
    CandidateKeyV1,
    CandidateSideV1,
    DetectorProjectionV1,
    DetectorThresholdsManifestV1,
    EvidenceClassV1,
    MINING_SCHEMA_VERSION_V1,
    SourceAncestryV1,
    canonical_json_bytes,
    load_detector_thresholds,
    sha256_json,
    unsigned_share_ppm,
)


WO33A1_THRESHOLD_MANIFEST_SHA256_V1 = (
    "4996ddce777527cf5350f3eaaeeff83911d8dd95dc510c704411ec7d8f708899"
)
DETECTOR_RUNTIME_ID_V1 = "LESSON_DETECTOR_RUNTIME_V1"

B1_DETECTOR_IDS_V1 = tuple(
    sorted(
        {
            "AGGRESSIVE_FLOW_BURST",
            "APPARENT_LIQUIDITY_MIRAGE",
            "ASK_ABSORPTION",
            "BID_ABSORPTION",
            "CANCELLATION_BURST",
            "FAILED_BREAKOUT",
            "HIDDEN_RESERVE_REFRESH",
            "LIQUIDITY_VACUUM",
            "MEAN_REVERSION_TRANSITION",
            "MOMENTUM_EXHAUSTION",
            "QUEUE_DEPLETION",
            "QUEUE_REPLENISHMENT",
            "SPREAD_EXPANSION",
            "SPREAD_RECOVERY",
            "STRONG_QUEUE_IMBALANCE",
        },
        key=lambda item: item.encode("utf-8"),
    )
)
B2_DETECTOR_IDS_V1 = tuple(
    sorted(
        {
            "AUCTION_IMBALANCE_CHANGE",
            "CANCEL_FILL_RACE",
            "DISTRESSED_LIQUIDATION",
            "HALT_REOPENING",
            "LATENCY_SENSITIVE_OPPORTUNITY",
            "MULTI_VENUE_FRAGMENTATION",
            "ROUTING_DILEMMA",
        },
        key=lambda item: item.encode("utf-8"),
    )
)
OPERATIONAL_DETECTOR_IDS_V1 = tuple(
    sorted(
        {*B1_DETECTOR_IDS_V1, *B2_DETECTOR_IDS_V1},
        key=lambda item: item.encode("utf-8"),
    )
)
RETROSPECTIVE_METRIC_NAMES_V1 = ("adverse_selection_x2_tick_shares",)
ASSESSMENT_DATA_POLICY_ID_V1 = "ORIGINAL_DECISION_INFORMATION_ONLY_V1"

_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_MEASUREMENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_NOT_APPLICABLE = "NOT_APPLICABLE"
_CONSOLIDATED = "CONSOLIDATED"


class DetectorRunStatusV1(str, Enum):
    EXERCISED = "EXERCISED"
    NOT_EXERCISED = "NOT_EXERCISED"


class OpportunityDispositionV1(str, Enum):
    EMITTED = "EMITTED"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    EXCLUDED = "EXCLUDED"
    NOT_EXERCISED = "NOT_EXERCISED"


class MiningExclusionV1(str, Enum):
    AUCTION = "AUCTION"
    BOUNDARY_CLIPPED = "BOUNDARY_CLIPPED"
    FIELD_INCOMPLETE = "FIELD_INCOMPLETE"
    HALT = "HALT"
    SESSION_BOUNDARY = "SESSION_BOUNDARY"


class FindingEvidenceLabelV1(str, Enum):
    AUTHORITATIVE_SYNTHETIC_GROUND_TRUTH = (
        "AUTHORITATIVE_SYNTHETIC_GROUND_TRUTH"
    )
    HISTORICAL_DETECTOR_INTERPRETATION = "HISTORICAL_DETECTOR_INTERPRETATION"
    SYNTHETIC_RECONSTRUCTION = "SYNTHETIC_RECONSTRUCTION"


MeasurementScalarV1 = int | bool | str
MeasurementValueV1 = MeasurementScalarV1 | tuple[int, ...]


def _require_nfc(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC")
    return value


def _require_identifier(value: object, label: str) -> str:
    text = _require_nfc(value, label)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{label} must be an uppercase identifier")
    return text


@dataclass(frozen=True, slots=True)
class DetectorMeasurementV1:
    name: str
    value: MeasurementValueV1

    def __post_init__(self) -> None:
        if type(self.name) is not str or _MEASUREMENT_NAME.fullmatch(self.name) is None:
            raise ValueError("detector measurement name is invalid")
        value = self.value
        if type(value) in {int, bool}:
            return
        if type(value) is str:
            _require_nfc(value, f"detector measurement {self.name}")
            return
        if type(value) is tuple and all(type(item) is int for item in value):
            return
        raise TypeError(f"detector measurement {self.name} has a noncanonical value")

    def as_dict(self) -> dict[str, object]:
        value: object = self.value
        if type(value) is tuple:
            value = list(value)
        return {"name": self.name, "value": value}


@dataclass(frozen=True, slots=True)
class MiningEventReferenceV1:
    event_id: str
    timestamp_us: int
    source_sequence: int

    def __post_init__(self) -> None:
        _require_nfc(self.event_id, "mining source-event ID")
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("mining event timestamp must be nonnegative microseconds")
        if type(self.source_sequence) is not int or self.source_sequence < 0:
            raise ValueError("mining source sequence must be nonnegative")

    @property
    def sort_key(self) -> tuple[int, int, bytes]:
        return (
            self.source_sequence,
            self.timestamp_us,
            self.event_id.encode("utf-8"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "source_sequence": self.source_sequence,
            "timestamp_us": self.timestamp_us,
        }


@dataclass(frozen=True, slots=True)
class DetectorOpportunityV1:
    detector_id: str
    opportunity_id: str
    sampling_unit: str
    source_start_us: int
    source_end_us: int
    active_start_us: int
    activation_us: int
    direction: CandidateDirectionV1
    side: CandidateSideV1
    venue: str
    price: int | str
    witness_kind: str
    witness_ids: tuple[str, ...]
    measurements: tuple[DetectorMeasurementV1, ...]
    contributing_events: tuple[MiningEventReferenceV1, ...]
    exclusions: tuple[MiningExclusionV1, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.detector_id, "opportunity detector ID")
        _require_nfc(self.opportunity_id, "detector opportunity ID")
        _require_identifier(self.sampling_unit, "detector sampling unit")
        times = (
            self.source_start_us,
            self.source_end_us,
            self.active_start_us,
            self.activation_us,
        )
        if any(type(value) is not int or value < 0 for value in times):
            raise ValueError("detector opportunity times must be nonnegative integers")
        if not (
            self.source_start_us
            <= self.active_start_us
            <= self.activation_us
            < self.source_end_us
        ):
            raise ValueError("detector opportunity source bounds are inconsistent")
        if not isinstance(self.direction, CandidateDirectionV1):
            raise TypeError("detector opportunity direction is invalid")
        if not isinstance(self.side, CandidateSideV1):
            raise TypeError("detector opportunity side is invalid")
        if self.venue not in {_NOT_APPLICABLE, _CONSOLIDATED}:
            _require_nfc(self.venue, "detector opportunity venue")
        if not (
            (type(self.price) is int and self.price > 0)
            or self.price == _NOT_APPLICABLE
        ):
            raise ValueError("detector opportunity price is invalid")
        witness_ids = self._canonical_witness_ids()
        object.__setattr__(self, "witness_ids", witness_ids)

        if type(self.measurements) is not tuple or not self.measurements:
            raise ValueError("detector opportunity measurements must not be empty")
        if any(not isinstance(item, DetectorMeasurementV1) for item in self.measurements):
            raise TypeError("detector opportunity measurement row is invalid")
        measurements = tuple(
            sorted(self.measurements, key=lambda item: item.name.encode("utf-8"))
        )
        if len({item.name for item in measurements}) != len(measurements):
            raise ValueError("detector opportunity measurement names are duplicated")
        object.__setattr__(self, "measurements", measurements)

        if type(self.contributing_events) is not tuple or not self.contributing_events:
            raise ValueError("detector opportunity requires contributing events")
        if any(
            not isinstance(item, MiningEventReferenceV1)
            for item in self.contributing_events
        ):
            raise TypeError("detector opportunity event reference is invalid")
        events = tuple(sorted(self.contributing_events, key=lambda item: item.sort_key))
        if len({item.event_id for item in events}) != len(events):
            raise ValueError("detector opportunity source-event IDs are duplicated")
        if len({item.source_sequence for item in events}) != len(events):
            raise ValueError("detector opportunity source sequence is ambiguous")
        if any(
            previous.timestamp_us > current.timestamp_us
            for previous, current in zip(events, events[1:])
        ):
            raise ValueError("detector opportunity source timestamps move backward")
        if any(
            not self.active_start_us <= item.timestamp_us <= self.activation_us
            for item in events
        ):
            raise ValueError("contributing event lies outside the active evidence")
        object.__setattr__(self, "contributing_events", events)
        _validate_witness_event_links(self.witness_kind, self.witness_ids, events)

        if type(self.exclusions) is not tuple or any(
            not isinstance(item, MiningExclusionV1) for item in self.exclusions
        ):
            raise TypeError("detector opportunity exclusions are invalid")
        exclusions = tuple(sorted(set(self.exclusions), key=lambda item: item.value))
        object.__setattr__(self, "exclusions", exclusions)

    def _canonical_witness_ids(self) -> tuple[str, ...]:
        if type(self.witness_ids) is not tuple:
            raise TypeError("detector witness IDs must be a tuple")
        for item in self.witness_ids:
            _require_nfc(item, "detector witness ID")
        if len(set(self.witness_ids)) != len(self.witness_ids):
            raise ValueError("detector witness IDs must be unique")
        canonicalizer = _WITNESS_CANONICALIZERS_V1.get(self.witness_kind)
        if canonicalizer is None:
            raise ValueError("detector opportunity witness kind is not operational")
        return canonicalizer(self, self.witness_ids)

    @property
    def measurement_map(self) -> Mapping[str, MeasurementValueV1]:
        return MappingProxyType({item.name: item.value for item in self.measurements})

    @property
    def witness_key(self) -> str:
        if self.witness_kind == _NOT_APPLICABLE:
            return _NOT_APPLICABLE
        return sha256_json(
            {"ids": list(self.witness_ids), "kind": self.witness_kind}
        )

    @property
    def evidence_discriminator(self) -> str:
        return sha256_json([item.event_id for item in self.contributing_events])

    @property
    def candidate_key(self) -> CandidateKeyV1:
        return CandidateKeyV1(
            detector_id=self.detector_id,
            direction=self.direction,
            side=self.side,
            venue=self.venue,
            price=self.price,
            witness_key=self.witness_key,
            anchor_start_us=self.active_start_us,
            evidence_discriminator=self.evidence_discriminator,
        )

    @property
    def sort_key(self) -> tuple[object, ...]:
        direction_order = {
            CandidateDirectionV1.BUY: 0,
            CandidateDirectionV1.SELL: 1,
            CandidateDirectionV1.NOT_APPLICABLE: 2,
        }
        side_order = {
            CandidateSideV1.BUY: 0,
            CandidateSideV1.SELL: 1,
            CandidateSideV1.NOT_APPLICABLE: 2,
        }
        if self.venue == _CONSOLIDATED:
            venue_group, venue_bytes = 1, b""
        elif self.venue == _NOT_APPLICABLE:
            venue_group, venue_bytes = 2, b""
        else:
            venue_group, venue_bytes = 0, self.venue.encode("utf-8")
        price_group = 0 if type(self.price) is int else 1
        price_value = self.price if type(self.price) is int else 0
        witness_bytes = (
            b"NOT_APPLICABLE"
            if self.witness_kind == _NOT_APPLICABLE
            else canonical_json_bytes(
                {"ids": list(self.witness_ids), "kind": self.witness_kind}
            )
        )
        return (
            self.detector_id.encode("utf-8"),
            direction_order[self.direction],
            side_order[self.side],
            venue_group,
            venue_bytes,
            price_group,
            price_value,
            witness_bytes,
            self.active_start_us,
            bytes.fromhex(self.evidence_discriminator),
            self.opportunity_id.encode("utf-8"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "activation_us": self.activation_us,
            "active_start_us": self.active_start_us,
            "contributing_events": [
                item.as_dict() for item in self.contributing_events
            ],
            "detector_id": self.detector_id,
            "direction": self.direction.value,
            "exclusions": [item.value for item in self.exclusions],
            "measurements": [item.as_dict() for item in self.measurements],
            "opportunity_id": self.opportunity_id,
            "price": self.price,
            "record_kind": "DETECTOR_OPPORTUNITY_V1",
            "sampling_unit": self.sampling_unit,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "side": self.side.value,
            "source_end_us": self.source_end_us,
            "source_start_us": self.source_start_us,
            "venue": self.venue,
            "witness_ids": list(self.witness_ids),
            "witness_kind": self.witness_kind,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


def _witness_none(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    if witness_ids:
        raise ValueError("NOT_APPLICABLE witness may not have IDs")
    return ()


def _witness_order_cohort(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    if not witness_ids:
        raise ValueError("order-cohort witness must contain order IDs")
    return tuple(sorted(witness_ids, key=lambda item: item.encode("utf-8")))


def _witness_exact(
    witness_ids: tuple[str, ...],
    count: int,
    label: str,
) -> tuple[str, ...]:
    if len(witness_ids) != count:
        raise ValueError(f"{label} witness requires exactly {count} IDs")
    return witness_ids


def _witness_parent(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    return _witness_exact(witness_ids, 1, "spread-expansion parent")


def _witness_latency_action(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    ordered = _witness_exact(witness_ids, 4, "latency action")
    checkpoint_sha256, _action_id, venue, direction = ordered
    if re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha256) is None:
        raise ValueError("latency-action checkpoint digest is invalid")
    if (
        venue != opportunity.venue
        or venue == _NOT_APPLICABLE
        or direction != opportunity.direction.value
        or opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
    ):
        raise ValueError("latency-action witness differs from its candidate key")
    return ordered


def _witness_cancel_fill(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    return _witness_exact(witness_ids, 3, "cancel/fill")


def _witness_unordered_pair(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    pair = _witness_exact(witness_ids, 2, "unordered pair")
    return tuple(sorted(pair, key=lambda item: item.encode("utf-8")))


def _witness_causal_pair(
    opportunity: DetectorOpportunityV1,
    witness_ids: tuple[str, ...],
) -> tuple[str, ...]:
    del opportunity
    return _witness_exact(witness_ids, 2, "causal pair")


_WITNESS_CANONICALIZERS_V1: Mapping[
    str,
    Callable[[DetectorOpportunityV1, tuple[str, ...]], tuple[str, ...]],
] = MappingProxyType(
    {
        _NOT_APPLICABLE: _witness_none,
        "AUCTION_PUBLICATION_PAIR": _witness_causal_pair,
        "CANCEL_FILL_TUPLE": _witness_cancel_fill,
        "HALT_REOPEN_PAIR": _witness_causal_pair,
        "LATENCY_ACTION": _witness_latency_action,
        "ORDER_COHORT": _witness_order_cohort,
        "ROUTE_PAIR": _witness_unordered_pair,
        "SPREAD_EXPANSION_PARENT": _witness_parent,
        "VENUE_PAIR": _witness_unordered_pair,
    }
)

_WITNESS_EVENT_LINK_POSITIONS_V1: Mapping[str, tuple[int, ...]] = MappingProxyType(
    {
        "AUCTION_PUBLICATION_PAIR": (0, 1),
        "HALT_REOPEN_PAIR": (0, 1),
    }
)
_CAUSALLY_ORDERED_WITNESS_KINDS_V1 = frozenset(
    {
        "AUCTION_PUBLICATION_PAIR",
        "HALT_REOPEN_PAIR",
    }
)


def _validate_witness_event_links(
    witness_kind: str,
    witness_ids: tuple[str, ...],
    events: tuple[MiningEventReferenceV1, ...],
) -> None:
    linked_positions = _WITNESS_EVENT_LINK_POSITIONS_V1.get(witness_kind)
    if linked_positions is None:
        return
    positions = {event.event_id: index for index, event in enumerate(events)}
    linked_ids = tuple(witness_ids[index] for index in linked_positions)
    if any(witness_id not in positions for witness_id in linked_ids):
        raise ValueError("witness does not name its contributing source events")
    observed_positions = tuple(positions[witness_id] for witness_id in linked_ids)
    if (
        witness_kind in _CAUSALLY_ORDERED_WITNESS_KINDS_V1
        and observed_positions != tuple(sorted(observed_positions))
    ):
        raise ValueError("causal witness IDs do not retain source-event order")


@dataclass(frozen=True, slots=True)
class RuleEvaluationV1:
    qualifies: bool
    reason_codes: tuple[str, ...]
    derived_measurements: tuple[DetectorMeasurementV1, ...] = ()

    def __post_init__(self) -> None:
        if type(self.qualifies) is not bool:
            raise TypeError("rule evaluation qualification must be boolean")
        if type(self.reason_codes) is not tuple or any(
            type(item) is not str or _IDENTIFIER.fullmatch(item) is None
            for item in self.reason_codes
        ):
            raise ValueError("rule evaluation reason codes are invalid")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("rule evaluation reason codes are duplicated")
        if self.qualifies == bool(self.reason_codes):
            raise ValueError("rule evaluation qualification and reasons disagree")
        if type(self.derived_measurements) is not tuple or any(
            not isinstance(item, DetectorMeasurementV1)
            for item in self.derived_measurements
        ):
            raise TypeError("derived detector measurements are invalid")
        ordered = tuple(
            sorted(
                self.derived_measurements,
                key=lambda item: item.name.encode("utf-8"),
            )
        )
        if len({item.name for item in ordered}) != len(ordered):
            raise ValueError("derived detector measurements are duplicated")
        object.__setattr__(self, "derived_measurements", ordered)


DetectorHandlerV1 = Callable[
    [DetectorOpportunityV1, Mapping[str, object]],
    RuleEvaluationV1,
]


@dataclass(frozen=True, slots=True)
class DetectorFindingV1:
    detector: DetectorProjectionV1
    source_ancestry_sha256: str
    capability_record_sha256: str
    opportunity_sha256: str
    candidate_key: CandidateKeyV1
    bounds: CandidateBoundsV1
    evidence_label: FindingEvidenceLabelV1
    derived_measurements: tuple[DetectorMeasurementV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.detector, DetectorProjectionV1):
            raise TypeError("detector finding projection is invalid")
        for value in (
            self.source_ancestry_sha256,
            self.capability_record_sha256,
            self.opportunity_sha256,
        ):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("detector finding digest is invalid")
        if not isinstance(self.candidate_key, CandidateKeyV1):
            raise TypeError("detector finding candidate key is invalid")
        if self.candidate_key.detector_id != self.detector.detector_id:
            raise ValueError("detector finding key names another detector")
        if not isinstance(self.bounds, CandidateBoundsV1):
            raise TypeError("detector finding bounds are invalid")
        if not isinstance(self.evidence_label, FindingEvidenceLabelV1):
            raise TypeError("detector finding evidence label is invalid")
        if type(self.derived_measurements) is not tuple or any(
            not isinstance(item, DetectorMeasurementV1)
            for item in self.derived_measurements
        ):
            raise TypeError("detector finding derived measurements are invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "bounds": self.bounds.as_dict(),
            "candidate_key": self.candidate_key.as_list(),
            "capability_record_sha256": self.capability_record_sha256,
            "derived_measurements": [
                item.as_dict() for item in self.derived_measurements
            ],
            "detector": self.detector.as_dict(),
            "evidence_label": self.evidence_label.value,
            "interpretation_scope": "DETECTOR_INTERPRETATION_NOT_HISTORICAL_FACT",
            "opportunity_sha256": self.opportunity_sha256,
            "record_kind": "RAW_DETECTOR_FINDING_V1",
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_ancestry_sha256": self.source_ancestry_sha256,
        }

    def assessment_projection(self) -> dict[str, object]:
        """Return only information admissible at the original decision cutoff.

        Raw detector findings may be created by replay and may later acquire outcome
        metrics.  The assessment surface deliberately omits the detector identity,
        post horizon, replay outcomes, and all derived measurements.
        """

        return {
            "active_start_us": self.bounds.active_start_us,
            "assessment_data_policy_id": ASSESSMENT_DATA_POLICY_ID_V1,
            "detector_identity": "WITHHELD_DURING_ASSESSMENT",
            "evidence_available_through_us": self.bounds.activation_us,
            "outcome_data": "WITHHELD_DURING_ASSESSMENT",
            "record_kind": "DETECTOR_ASSESSMENT_PROJECTION_V1",
            "retrospective_metrics": [
                {
                    "name": name,
                    "status": "WITHHELD_DURING_ASSESSMENT",
                }
                for name in RETROSPECTIVE_METRIC_NAMES_V1
            ],
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_ancestry_sha256": self.source_ancestry_sha256,
        }

    @property
    def finding_sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class DetectorConsiderationV1:
    opportunity_id: str
    opportunity_sha256: str
    disposition: OpportunityDispositionV1
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nfc(self.opportunity_id, "considered opportunity ID")
        if type(self.opportunity_sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", self.opportunity_sha256
        ) is None:
            raise ValueError("considered opportunity digest is invalid")
        if not isinstance(self.disposition, OpportunityDispositionV1):
            raise TypeError("opportunity disposition is invalid")
        if type(self.reason_codes) is not tuple or any(
            type(item) is not str or _IDENTIFIER.fullmatch(item) is None
            for item in self.reason_codes
        ):
            raise ValueError("consideration reason codes are invalid")
        if self.disposition is OpportunityDispositionV1.EMITTED:
            if self.reason_codes:
                raise ValueError("emitted consideration may not have refusal reasons")
        elif not self.reason_codes:
            raise ValueError("non-emitted consideration requires a reason")

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "opportunity_id": self.opportunity_id,
            "opportunity_sha256": self.opportunity_sha256,
            "record_kind": "DETECTOR_CONSIDERATION_V1",
            "reason_codes": list(self.reason_codes),
            "schema_version": MINING_SCHEMA_VERSION_V1,
        }


@dataclass(frozen=True, slots=True)
class DetectorRunReportV1:
    detector: DetectorProjectionV1
    sampling_unit: str
    threshold_manifest_sha256: str
    source_ancestry_sha256: str
    source_capability_inventory_sha256: str
    status: DetectorRunStatusV1
    reason_code: str | None
    missing_capabilities: tuple[str, ...]
    considered: tuple[DetectorConsiderationV1, ...]
    findings: tuple[DetectorFindingV1, ...]
    eligible_units: int
    excluded_units: int
    qualifying_units: int

    def __post_init__(self) -> None:
        if not isinstance(self.detector, DetectorProjectionV1):
            raise TypeError("detector run projection is invalid")
        _require_identifier(self.sampling_unit, "detector report sampling unit")
        for value in (
            self.threshold_manifest_sha256,
            self.source_ancestry_sha256,
            self.source_capability_inventory_sha256,
        ):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("detector run digest is invalid")
        if not isinstance(self.status, DetectorRunStatusV1):
            raise TypeError("detector run status is invalid")
        if self.reason_code is not None:
            _require_identifier(self.reason_code, "detector run reason code")
        if type(self.missing_capabilities) is not tuple or any(
            type(item) is not str for item in self.missing_capabilities
        ):
            raise TypeError("detector run missing capabilities are invalid")
        if type(self.considered) is not tuple or any(
            not isinstance(item, DetectorConsiderationV1) for item in self.considered
        ):
            raise TypeError("detector run considerations are invalid")
        if len({item.opportunity_id for item in self.considered}) != len(
            self.considered
        ):
            raise ValueError("detector run consideration IDs are duplicated")
        if type(self.findings) is not tuple or any(
            not isinstance(item, DetectorFindingV1) for item in self.findings
        ):
            raise TypeError("detector run findings are invalid")
        if any(item.detector != self.detector for item in self.findings):
            raise ValueError("detector run contains a finding for another detector")
        counts = (self.eligible_units, self.excluded_units, self.qualifying_units)
        if any(type(item) is not int or item < 0 for item in counts):
            raise ValueError("detector denominator counts must be nonnegative integers")
        emitted = sum(
            item.disposition is OpportunityDispositionV1.EMITTED
            for item in self.considered
        )
        excluded = sum(
            item.disposition is OpportunityDispositionV1.EXCLUDED
            for item in self.considered
        )
        if (
            emitted != self.qualifying_units
            or len(self.findings) != self.qualifying_units
            or excluded != self.excluded_units
            or self.qualifying_units > self.eligible_units
        ):
            raise ValueError("detector report counts do not reconcile")
        if self.status is DetectorRunStatusV1.EXERCISED:
            if self.reason_code is not None or self.eligible_units == 0:
                raise ValueError("exercised detector report has refusal state")
        elif self.reason_code is None or self.eligible_units != 0:
            raise ValueError("NOT_EXERCISED report lacks its explicit reason")
        if self.reason_code in {
            None,
            "ZERO_ELIGIBLE_DENOMINATOR",
        } and len(self.considered) != self.eligible_units + self.excluded_units:
            raise ValueError("detector denominator does not account for considerations")
        if self.reason_code in {
            "INSUFFICIENT_SOURCE_CAPABILITY",
            "UNSUPPORTED_EVIDENCE_CLASS",
        } and any(
            item.disposition is not OpportunityDispositionV1.NOT_EXERCISED
            for item in self.considered
        ):
            raise ValueError("capability-refused detector considered an opportunity")

    @property
    def sample_frequency_ppm(self) -> int | None:
        if self.eligible_units == 0:
            return None
        return unsigned_share_ppm(self.qualifying_units, self.eligible_units)

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": [item.as_dict() for item in self.considered],
            "detector": self.detector.as_dict(),
            "eligible_units": self.eligible_units,
            "excluded_units": self.excluded_units,
            "findings": [item.as_dict() for item in self.findings],
            "missing_capabilities": list(self.missing_capabilities),
            "qualifying_units": self.qualifying_units,
            "reason_code": self.reason_code,
            "runtime_id": DETECTOR_RUNTIME_ID_V1,
            "sampling_unit": self.sampling_unit,
            "sample_frequency_ppm": self.sample_frequency_ppm,
            "schema_version": MINING_SCHEMA_VERSION_V1,
            "source_ancestry_sha256": self.source_ancestry_sha256,
            "source_capability_inventory_sha256": (
                self.source_capability_inventory_sha256
            ),
            "status": self.status.value,
            "threshold_manifest_sha256": self.threshold_manifest_sha256,
        }

    @property
    def report_sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class MiningDetectorRuntimeV1:
    threshold_manifest: DetectorThresholdsManifestV1 = field(
        default_factory=load_detector_thresholds
    )
    handlers: Mapping[str, DetectorHandlerV1] | None = None
    _handler_map: Mapping[str, DetectorHandlerV1] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.threshold_manifest, DetectorThresholdsManifestV1):
            raise TypeError("mining runtime threshold manifest is invalid")
        canonical_manifest = DetectorThresholdsManifestV1.from_toml_bytes(
            self.threshold_manifest.canonical_bytes()
        )
        if (
            self.threshold_manifest.as_dict() != canonical_manifest.as_dict()
            or self.threshold_manifest.semantic_sha256
            != canonical_manifest.semantic_sha256
            or self.threshold_manifest.manifest_sha256
            != canonical_manifest.manifest_sha256
            or self.threshold_manifest.file_sha256 != canonical_manifest.file_sha256
        ):
            raise ValueError("mining runtime threshold manifest object is inconsistent")
        if (
            canonical_manifest.manifest_sha256
            != WO33A1_THRESHOLD_MANIFEST_SHA256_V1
        ):
            raise ValueError("WO33-A1 threshold manifest changed before detector mining")
        object.__setattr__(self, "threshold_manifest", canonical_manifest)
        handlers = dict(
            _default_operational_handlers() if self.handlers is None else self.handlers
        )
        if set(handlers) != set(OPERATIONAL_DETECTOR_IDS_V1):
            raise ValueError(
                "operational detector handler inventory is incomplete or expanded"
            )
        if any(not callable(handler) for handler in handlers.values()):
            raise TypeError("operational detector handler is not callable")
        object.__setattr__(
            self,
            "_handler_map",
            MappingProxyType(
                dict(sorted(handlers.items(), key=lambda item: item[0].encode("utf-8")))
            ),
        )
        object.__setattr__(self, "handlers", self._handler_map)

    @property
    def handler_ids(self) -> tuple[str, ...]:
        return tuple(self._handler_map)

    def run(
        self,
        detector_id: str,
        source: SourceCapabilityInventoryV1,
        source_ancestry: SourceAncestryV1,
        opportunities: Sequence[DetectorOpportunityV1],
    ) -> DetectorRunReportV1:
        if detector_id not in self._handler_map:
            raise ValueError(f"detector is not operational: {detector_id}")
        if not isinstance(source, SourceCapabilityInventoryV1):
            raise TypeError("detector source capability inventory is invalid")
        if not isinstance(source_ancestry, SourceAncestryV1):
            raise TypeError("detector source ancestry is invalid")
        if source.source_identity.as_dict() != source_ancestry.source_identity.as_dict():
            raise ValueError("detector source capability and ancestry identities differ")
        if isinstance(opportunities, (str, bytes)) or not isinstance(
            opportunities, Sequence
        ):
            raise TypeError("detector opportunities must be a finite sequence")
        raw_opportunities = tuple(opportunities)
        if any(
            not isinstance(item, DetectorOpportunityV1)
            for item in raw_opportunities
        ):
            raise TypeError("detector opportunity row is invalid")
        ordered = tuple(sorted(raw_opportunities, key=lambda item: item.sort_key))
        if len({item.opportunity_id for item in ordered}) != len(ordered):
            raise ValueError("detector opportunity IDs are duplicated")
        candidate_keys = tuple(
            canonical_json_bytes(item.candidate_key.as_list()) for item in ordered
        )
        if len(set(candidate_keys)) != len(candidate_keys):
            raise ValueError("detector opportunities duplicate a canonical candidate key")
        if len(
            {
                (item.source_start_us, item.source_end_us)
                for item in ordered
            }
        ) > 1:
            raise ValueError("detector opportunities disagree on source bounds")

        row = self.threshold_manifest.detector(detector_id)
        threshold_sha256 = self.threshold_manifest.detector_threshold_sha256(
            detector_id
        )
        detector = DetectorProjectionV1(detector_id, int(row["version"]), threshold_sha256)
        for opportunity in ordered:
            if opportunity.detector_id != detector_id:
                raise ValueError("detector run contains an opportunity for another detector")
            if opportunity.sampling_unit != row["sampling_unit"]:
                raise ValueError("detector opportunity sampling unit differs from manifest")
            if opportunity.witness_kind != row["witness_kind"]:
                raise ValueError("detector opportunity witness kind differs from manifest")
            _validate_key_axes(opportunity, row)
            _validate_sampling_alignment(opportunity)

        support = DETECTOR_REGISTRY_V1.assess(
            detector_id,
            int(row["version"]),
            threshold_sha256,
            source,
        )
        inventory_sha256 = source.sha256
        if support.status is DetectorSupportStatusV1.NOT_EXERCISED:
            reason = support.reason_code or "INSUFFICIENT_SOURCE_CAPABILITY"
            considerations = tuple(
                DetectorConsiderationV1(
                    item.opportunity_id,
                    item.sha256,
                    OpportunityDispositionV1.NOT_EXERCISED,
                    (reason,),
                )
                for item in ordered
            )
            return DetectorRunReportV1(
                detector=detector,
                sampling_unit=str(row["sampling_unit"]),
                threshold_manifest_sha256=self.threshold_manifest.manifest_sha256,
                source_ancestry_sha256=source_ancestry.sha256,
                source_capability_inventory_sha256=inventory_sha256,
                status=DetectorRunStatusV1.NOT_EXERCISED,
                reason_code=reason,
                missing_capabilities=support.missing_capabilities,
                considered=considerations,
                findings=(),
                eligible_units=0,
                excluded_units=0,
                qualifying_units=0,
            )
        if support.capability_record is None:
            raise RuntimeError("eligible detector support lacks a capability record")

        handler = self._handler_map[detector_id]
        considerations: list[DetectorConsiderationV1] = []
        findings: list[DetectorFindingV1] = []
        eligible_units = 0
        excluded_units = 0
        horizons = row["horizons"]
        if not isinstance(horizons, Mapping):
            raise TypeError("detector manifest horizons are invalid")
        lookback_us = int(horizons["maximum_pre_activation_lookback_us"])
        post_horizon_us = int(horizons["maximum_post_activation_horizon_us"])
        for opportunity in ordered:
            exclusions = set(opportunity.exclusions)
            if (
                opportunity.active_start_us - lookback_us
                < opportunity.source_start_us
                or opportunity.activation_us + post_horizon_us
                > opportunity.source_end_us
            ):
                exclusions.add(MiningExclusionV1.BOUNDARY_CLIPPED)
            if exclusions:
                excluded_units += 1
                considerations.append(
                    DetectorConsiderationV1(
                        opportunity.opportunity_id,
                        opportunity.sha256,
                        OpportunityDispositionV1.EXCLUDED,
                        tuple(sorted(item.value for item in exclusions)),
                    )
                )
                continue
            eligible_units += 1
            evaluation = handler(opportunity, row)
            if not isinstance(evaluation, RuleEvaluationV1):
                raise TypeError("detector handler returned a noncanonical evaluation")
            if not evaluation.qualifies:
                if evaluation.reason_codes == ("INSUFFICIENT_EVIDENCE",):
                    eligible_units -= 1
                    excluded_units += 1
                    disposition = OpportunityDispositionV1.EXCLUDED
                else:
                    disposition = OpportunityDispositionV1.BELOW_THRESHOLD
                considerations.append(
                    DetectorConsiderationV1(
                        opportunity.opportunity_id,
                        opportunity.sha256,
                        disposition,
                        evaluation.reason_codes,
                    )
                )
                continue
            bounds = CandidateBoundsV1(
                source_start_us=opportunity.source_start_us,
                source_end_us=opportunity.source_end_us,
                warmup_start_us=opportunity.active_start_us - lookback_us,
                active_start_us=opportunity.active_start_us,
                active_end_us=opportunity.activation_us + 1,
                post_end_us=opportunity.activation_us + post_horizon_us,
            )
            finding = DetectorFindingV1(
                detector=detector,
                source_ancestry_sha256=source_ancestry.sha256,
                capability_record_sha256=support.capability_record.sha256,
                opportunity_sha256=opportunity.sha256,
                candidate_key=opportunity.candidate_key,
                bounds=bounds,
                evidence_label=_evidence_label(source.evidence_class),
                derived_measurements=evaluation.derived_measurements,
            )
            findings.append(finding)
            considerations.append(
                DetectorConsiderationV1(
                    opportunity.opportunity_id,
                    opportunity.sha256,
                    OpportunityDispositionV1.EMITTED,
                    (),
                )
            )
        status = (
            DetectorRunStatusV1.EXERCISED
            if eligible_units
            else DetectorRunStatusV1.NOT_EXERCISED
        )
        reason_code = None if eligible_units else "ZERO_ELIGIBLE_DENOMINATOR"
        return DetectorRunReportV1(
            detector=detector,
            sampling_unit=str(row["sampling_unit"]),
            threshold_manifest_sha256=self.threshold_manifest.manifest_sha256,
            source_ancestry_sha256=source_ancestry.sha256,
            source_capability_inventory_sha256=inventory_sha256,
            status=status,
            reason_code=reason_code,
            missing_capabilities=(),
            considered=tuple(considerations),
            findings=tuple(findings),
            eligible_units=eligible_units,
            excluded_units=excluded_units,
            qualifying_units=len(findings),
        )


def _validate_key_axes(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> None:
    axes = row.get("key_axes")
    if type(axes) is not list or len(axes) != 4:
        raise ValueError("detector manifest key axes are invalid")
    direction_values = {
        "BUY": {CandidateDirectionV1.BUY},
        "GAP_SIGN": {
            CandidateDirectionV1.BUY,
            CandidateDirectionV1.SELL,
            CandidateDirectionV1.NOT_APPLICABLE,
        },
        "NOT_APPLICABLE": {CandidateDirectionV1.NOT_APPLICABLE},
        "OBJECTIVE": {CandidateDirectionV1.BUY, CandidateDirectionV1.SELL},
        "SELL": {CandidateDirectionV1.SELL},
        "SIGN": {CandidateDirectionV1.BUY, CandidateDirectionV1.SELL},
    }
    side_values = {
        "AFFECTED": {CandidateSideV1.BUY, CandidateSideV1.SELL},
        "BUY": {CandidateSideV1.BUY},
        "NOT_APPLICABLE": {CandidateSideV1.NOT_APPLICABLE},
        "OBJECTIVE": {CandidateSideV1.BUY, CandidateSideV1.SELL},
        "ORDER_SIDE": {CandidateSideV1.BUY, CandidateSideV1.SELL},
        "SELL": {CandidateSideV1.SELL},
        "SIGN": {CandidateSideV1.BUY, CandidateSideV1.SELL},
    }
    direction_axis, side_axis, venue_axis, price_axis = axes
    if (
        direction_axis not in direction_values
        or opportunity.direction not in direction_values[direction_axis]
    ):
        raise ValueError("detector opportunity direction differs from its key axis")
    if side_axis not in side_values or opportunity.side not in side_values[side_axis]:
        raise ValueError("detector opportunity side differs from its key axis")
    venue_validators: Mapping[str, Callable[[str], bool]] = {
        "CONSOLIDATED": lambda value: value == _CONSOLIDATED,
        "NOT_APPLICABLE": lambda value: value == _NOT_APPLICABLE,
        "SOURCE_VENUE": lambda value: value
        not in {_CONSOLIDATED, _NOT_APPLICABLE},
        "VENUE_OR_CONSOLIDATED": lambda value: value != _NOT_APPLICABLE,
    }
    venue_validator = venue_validators.get(str(venue_axis))
    if venue_validator is None or not venue_validator(opportunity.venue):
        raise ValueError("detector opportunity venue differs from its key axis")
    price_validators: Mapping[str, Callable[[int | str], bool]] = {
        "NOT_APPLICABLE": lambda value: value == _NOT_APPLICABLE,
        "OPENING_BEST_ASK": lambda value: type(value) is int and value > 0,
        "OPENING_BEST_BID": lambda value: type(value) is int and value > 0,
        "RELEVANT_PRICE": lambda value: type(value) is int and value > 0,
    }
    price_validator = price_validators.get(str(price_axis))
    if price_validator is None or not price_validator(opportunity.price):
        raise ValueError("detector opportunity price differs from its key axis")


def _validate_sampling_alignment(opportunity: DetectorOpportunityV1) -> None:
    alignment_by_unit = {
        "ALIGNED_ONE_SECOND_GROUP": 1_000_000,
        "BOUND_REPLAY_PAIR": None,
        "COMPLETE_HALT_REOPEN_EPISODE": None,
        "CONSECUTIVE_PUBLICATION_PAIR": None,
        "OBSERVABLE_BIN_100000_US": 100_000,
    }
    if opportunity.sampling_unit not in alignment_by_unit:
        raise ValueError("detector sampling unit has no canonical alignment rule")
    alignment = alignment_by_unit[opportunity.sampling_unit]
    if alignment is None:
        return
    if (opportunity.active_start_us - opportunity.source_start_us) % alignment != 0:
        raise ValueError("detector opportunity is not aligned to the source lower bound")


def exact_measurements(
    opportunity: DetectorOpportunityV1,
    expected_names: set[str],
) -> Mapping[str, MeasurementValueV1]:
    values = opportunity.measurement_map
    if set(values) != expected_names:
        raise ValueError(
            f"{opportunity.detector_id} measurement fields differ from its rule"
        )
    return values


def measurement_int(values: Mapping[str, MeasurementValueV1], name: str) -> int:
    value = values[name]
    if type(value) is not int:
        raise TypeError(f"detector measurement {name} must be an exact integer")
    return value


def measurement_bool(values: Mapping[str, MeasurementValueV1], name: str) -> bool:
    value = values[name]
    if type(value) is not bool:
        raise TypeError(f"detector measurement {name} must be an exact Boolean")
    return value


def measurement_int_tuple(
    values: Mapping[str, MeasurementValueV1],
    name: str,
) -> tuple[int, ...]:
    value = values[name]
    if type(value) is not tuple or any(type(item) is not int for item in value):
        raise TypeError(f"detector measurement {name} must be an integer tuple")
    return value


def threshold_int(row: Mapping[str, object], name: str) -> int:
    thresholds = row.get("thresholds")
    if not isinstance(thresholds, list):
        raise TypeError("detector threshold rows are invalid")
    matches = [item for item in thresholds if item.get("name") == name]
    if len(matches) != 1 or type(matches[0].get("value")) is not int:
        raise ValueError(f"detector threshold {name} is absent or noninteger")
    return int(matches[0]["value"])


def evaluation(
    clauses: Sequence[tuple[bool, str]],
    *derived_measurements: DetectorMeasurementV1,
) -> RuleEvaluationV1:
    failures = tuple(reason for passed, reason in clauses if not passed)
    return RuleEvaluationV1(
        qualifies=not failures,
        reason_codes=failures,
        derived_measurements=tuple(derived_measurements),
    )


def nearest_rank_p50(values: tuple[int, ...]) -> int:
    if not values or any(type(item) is not int or item < 0 for item in values):
        raise ValueError("detector P50 requires nonnegative integer observations")
    ordered = sorted(values)
    rank = max(1, (500_000 * len(ordered) + 1_000_000 - 1) // 1_000_000)
    return ordered[rank - 1]


def time_weighted_nearest_rank_p50(
    values: tuple[int, ...],
    durations_us: tuple[int, ...],
) -> int:
    if (
        not values
        or len(values) != len(durations_us)
        or any(type(value) is not int or value < 0 for value in values)
        or any(type(duration) is not int or duration <= 0 for duration in durations_us)
    ):
        raise ValueError(
            "time-weighted detector P50 requires nonempty nonnegative values "
            "and positive durations"
        )
    ordered = sorted(
        zip(values, durations_us, range(len(values)), strict=True),
        key=lambda item: (item[0], item[2]),
    )
    total_duration = sum(durations_us)
    target = max(1, (500_000 * total_duration + 1_000_000 - 1) // 1_000_000)
    cumulative = 0
    for value, duration, _canonical_segment_order in ordered:
        cumulative += duration
        if cumulative >= target:
            return value
    raise RuntimeError("time-weighted detector P50 failed to reach its target")


def _evidence_label(evidence_class: EvidenceClassV1) -> FindingEvidenceLabelV1:
    return {
        EvidenceClassV1.SYNTHETIC_GROUND_TRUTH: (
            FindingEvidenceLabelV1.AUTHORITATIVE_SYNTHETIC_GROUND_TRUTH
        ),
        EvidenceClassV1.HISTORICAL_MARKET_BY_ORDER: (
            FindingEvidenceLabelV1.HISTORICAL_DETECTOR_INTERPRETATION
        ),
        EvidenceClassV1.RECONSTRUCTION_COUNTERFACTUAL: (
            FindingEvidenceLabelV1.SYNTHETIC_RECONSTRUCTION
        ),
    }[evidence_class]


def _default_operational_handlers() -> Mapping[str, DetectorHandlerV1]:
    from .flow_detectors import FLOW_DETECTOR_HANDLERS_V1
    from .latency_detectors import LATENCY_DETECTOR_HANDLERS_V1
    from .mechanics_detectors import MECHANICS_DETECTOR_HANDLERS_V1
    from .queue_detectors import QUEUE_DETECTOR_HANDLERS_V1
    from .venue_detectors import VENUE_DETECTOR_HANDLERS_V1

    modules = (
        QUEUE_DETECTOR_HANDLERS_V1,
        FLOW_DETECTOR_HANDLERS_V1,
        LATENCY_DETECTOR_HANDLERS_V1,
        MECHANICS_DETECTOR_HANDLERS_V1,
        VENUE_DETECTOR_HANDLERS_V1,
    )
    combined = {key: value for module in modules for key, value in module.items()}
    if len(combined) != sum(len(module) for module in modules):
        raise ValueError("operational detector handler modules overlap")
    return combined


__all__ = [
    "ASSESSMENT_DATA_POLICY_ID_V1",
    "B1_DETECTOR_IDS_V1",
    "B2_DETECTOR_IDS_V1",
    "DETECTOR_RUNTIME_ID_V1",
    "OPERATIONAL_DETECTOR_IDS_V1",
    "RETROSPECTIVE_METRIC_NAMES_V1",
    "WO33A1_THRESHOLD_MANIFEST_SHA256_V1",
    "DetectorConsiderationV1",
    "DetectorFindingV1",
    "DetectorHandlerV1",
    "DetectorMeasurementV1",
    "DetectorOpportunityV1",
    "DetectorRunReportV1",
    "DetectorRunStatusV1",
    "FindingEvidenceLabelV1",
    "MiningDetectorRuntimeV1",
    "MiningEventReferenceV1",
    "MiningExclusionV1",
    "OpportunityDispositionV1",
    "RuleEvaluationV1",
    "evaluation",
    "exact_measurements",
    "measurement_bool",
    "measurement_int",
    "measurement_int_tuple",
    "nearest_rank_p50",
    "time_weighted_nearest_rank_p50",
    "threshold_int",
]
