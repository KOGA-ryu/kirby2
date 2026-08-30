"""Policy-bound counterfactual comparison contracts for the replay microscope.

This module turns immutable parent/branch evidence into a deterministic presentation
model.  It deliberately does not expose branch snapshots, backend receipts, raw
query inventories, or reveal bearer grants.  Those values may be checked by the
factory, but only the safe comparison projection can cross into a portable report.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum

from kirby2.counterfactual.models import (
    CounterfactualMode,
    CounterfactualOutcome,
    CounterfactualReport,
)
from kirby2.full_day.models import canonical_json_bytes
from kirby2.immutable import freeze_json, thaw_json

from .models import MechanisticTraceIndex
from .overlays import OVERLAY_KIND_ORDER, OverlayAvailability, OverlayKind, OverlaySet
from .policy import (
    ObservationMode,
    ObservationPolicy,
    RevealAuthorization,
    RevealCapability,
)
from .query import EvidenceSourceKind


BRANCH_COMPARISON_SCHEMA_ID = "KIRBY2_MICROSCOPE_BRANCH_COMPARISON_V1"
BRANCH_COMPARISON_SCHEMA_VERSION = 1
COMPARISON_EVENT_SCHEMA_ID = "KIRBY2_MICROSCOPE_COMPARISON_EVENT_V1"
COMPARISON_EVENT_SCHEMA_VERSION = 1
COMPARISON_RUN_INPUT_SCHEMA_ID = "KIRBY2_MICROSCOPE_COMPARISON_RUN_INPUT_V1"
COMPARISON_RUN_INPUT_SCHEMA_VERSION = 1
COMPARISON_SERIES_SCHEMA_ID = "KIRBY2_MICROSCOPE_COMPARISON_SERIES_V1"
COMPARISON_SERIES_SCHEMA_VERSION = 1
COMPARISON_OVERLAY_SCHEMA_ID = "KIRBY2_MICROSCOPE_COMPARISON_OVERLAY_V1"
COMPARISON_OVERLAY_SCHEMA_VERSION = 1
COMPARISON_TRACE_SCHEMA_ID = "KIRBY2_MICROSCOPE_COMPARISON_TRACE_V1"
COMPARISON_TRACE_SCHEMA_VERSION = 1

COMPARISON_INTERPRETATION = (
    "This comparison describes two deterministic Kirby2 simulation outcomes after "
    "one synchronized prefix. It is mechanistic evidence within the configured "
    "Kirby2 model, not proof of an unobserved real-market counterfactual."
)

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_CONSTRUCTION_TOKEN = object()

# Portable-report output must remain a safe projection.  This mirrors the public
# report boundary without importing report.py and creating a dependency cycle.
_FORBIDDEN_PRESENTATION_KEYS = frozenset(
    {
        "authorization_id",
        "backend_callback",
        "backend_handle",
        "capability_manifest_bytes",
        "event_count",
        "event_inventory_sha256",
        "ingestion_receipt",
        "inventory_commitment",
        "inventory_sha256",
        "maximum_cursor_time_us",
        "maximum_policy_visible_time_us",
        "minimum_cursor_time_us",
        "minimum_policy_visible_time_us",
        "observation_query_result",
        "overlay_projection_receipt",
        "partition_count",
        "partition_inventory_sha256",
        "private_event_bounds",
        "private_event_count",
        "private_partition_count",
        "query_count",
        "query_inventory_sha256",
        "query_receipt",
        "raw_observed_evidence",
        "raw_reveal_evidence",
        "receipt_sha256",
        "reveal_authorization",
        "reveal_authorization_id",
        "reveal_authorization_ids",
        "reveal_authorization_sha256",
        "root_query_sha256",
        "root_render_cursor_time_us",
        "timeline_receipt",
    }
)

_AS_OBSERVED_FORBIDDEN_PRESENTATION_VALUES = frozenset(
    {
        EvidenceSourceKind.REVEALED_GROUND_TRUTH.value,
        EvidenceSourceKind.REVEALED_HIDDEN_STATE.value,
        "POSTMORTEM_TRUTH",
        "POSTMORTEM_HIDDEN_STATE",
        "AUTHORIZED_GROUND_TRUTH",
        "AUTHORIZED_HIDDEN_STATE",
        "HIDDEN_ICEBERG",
        "REFRESHING_HIDDEN_ICEBERG",
    }
)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_identifier(value: object, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_run_id(value: object, label: str) -> None:
    if type(value) is not str or _RUN_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _bounded_text(value: object, label: str) -> None:
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError(f"{label} must be nonempty bounded text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC-normalized")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{label} contains a control character")


def _freeze_mapping(value: object, label: str) -> Mapping[str, object]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    # The strict canonical serializer rejects binary floats and non-normalized text.
    canonical_json_bytes(frozen)
    return frozen


def _freeze_value(value: object, label: str) -> object:
    frozen = freeze_json(value)
    try:
        canonical_json_bytes(frozen)
    except (TypeError, ValueError) as error:
        raise type(error)(f"{label}: {error}") from error
    return frozen


def _canonical_identifiers(
    values: tuple[str, ...],
    label: str,
    *,
    nonempty: bool,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    if nonempty and not values:
        raise ValueError(f"{label} must not be empty")
    for value in values:
        _require_identifier(value, label)
    canonical = tuple(sorted(values))
    if len(canonical) != len(set(canonical)):
        raise ValueError(f"{label} contains duplicate values")
    return canonical


def _safe_presentation_payload(
    value: object,
    mode: ObservationMode,
    path: str = "$",
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError(f"comparison output contains a non-text key at {path}")
            if key in _FORBIDDEN_PRESENTATION_KEYS:
                raise ValueError(f"comparison output contains backend material at {path}.{key}")
            _safe_presentation_payload(child, mode, f"{path}.{key}")
        return
    if type(value) in {list, tuple}:
        for index, child in enumerate(value):
            _safe_presentation_payload(child, mode, f"{path}[{index}]")
        return
    if value is None or type(value) in {bool, int, str}:
        if (
            mode is ObservationMode.AS_OBSERVED
            and value in _AS_OBSERVED_FORBIDDEN_PRESENTATION_VALUES
        ):
            raise ValueError(f"observed comparison exposes reveal material at {path}")
        return
    raise TypeError(f"comparison output contains non-semantic JSON at {path}")


class ComparisonSeriesKind(str, Enum):
    ORDERS = "ORDERS"
    QUEUE_STATES = "QUEUE_STATES"
    FILLS = "FILLS"
    DECLARED_METRICS = "DECLARED_METRICS"
    ENDOGENOUS_MARKET_PATH = "ENDOGENOUS_MARKET_PATH"


COMPARISON_SERIES_ORDER = tuple(ComparisonSeriesKind)


class ComparisonAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    RECORDED_EMPTY = "RECORDED_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"


class ComparisonRecordStatus(str, Enum):
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    PARENT_ONLY = "PARENT_ONLY"
    BRANCH_ONLY = "BRANCH_ONLY"


class CounterfactualRngPolicy(str, Enum):
    FIXED_EXOGENOUS_PATH = "FIXED_EXOGENOUS_PATH"
    FORK_SNAPSHOT_OWNED_RNG_STATE = "FORK_SNAPSHOT_OWNED_RNG_STATE"


class ComparisonOverlayKind(str, Enum):
    SPREAD = "SPREAD"
    MICROPRICE = "MICROPRICE"
    IMBALANCE = "IMBALANCE"
    TRADE_VELOCITY = "TRADE_VELOCITY"
    CANCELLATION_VELOCITY = "CANCELLATION_VELOCITY"
    REPLENISHMENT = "REPLENISHMENT"
    RELATIVE_VOLUME = "RELATIVE_VOLUME"
    SHORT_TERM_VOLATILITY = "SHORT_TERM_VOLATILITY"
    IMPLEMENTATION_SHORTFALL = "IMPLEMENTATION_SHORTFALL"
    LATENCY = "LATENCY"
    STALE_QUOTE_AGE = "STALE_QUOTE_AGE"
    QUEUE_ESTIMATE = "QUEUE_ESTIMATE"
    ADVERSE_SELECTION = "ADVERSE_SELECTION"
    ALGORITHM_SCHEDULE = "ALGORITHM_SCHEDULE"
    REGIME_STATE = "REGIME_STATE"
    AGENT_TRUTH = "AGENT_TRUTH"


COMPARISON_OVERLAY_ORDER = tuple(ComparisonOverlayKind)

if tuple(item.value for item in COMPARISON_OVERLAY_ORDER[:9]) != tuple(
    item.value for item in OVERLAY_KIND_ORDER
):  # pragma: no cover - import-time contract assertion
    raise RuntimeError("comparison overlay prefix differs from WO36-C")


class ComparisonEvidenceScope(str, Enum):
    POLICY_VISIBLE = "POLICY_VISIBLE"
    DECLARED_CALCULATION = "DECLARED_CALCULATION"
    POSTMORTEM_TRUTH = "POSTMORTEM_TRUTH"
    POSTMORTEM_HIDDEN_STATE = "POSTMORTEM_HIDDEN_STATE"


class ComparisonTraceAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ComparisonEventInput:
    """One immutable event admitted to parent/branch synchronization."""

    sequence: int
    simulation_time_us: int
    kind: str
    payload: Mapping[str, object]
    schema_id: str = COMPARISON_EVENT_SCHEMA_ID
    schema_version: int = COMPARISON_EVENT_SCHEMA_VERSION
    payload_sha256: str = field(init=False)
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        _positive_int(self.sequence, "comparison event sequence")
        _nonnegative_int(self.simulation_time_us, "comparison event time")
        _require_identifier(self.kind, "comparison event kind")
        payload = _freeze_mapping(self.payload, "comparison event payload")
        if (
            self.schema_id != COMPARISON_EVENT_SCHEMA_ID
            or self.schema_version != COMPARISON_EVENT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported comparison event schema")
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "payload_sha256", _canonical_sha256(payload))
        object.__setattr__(
            self,
            "event_id",
            "comparison-event-" + _canonical_sha256(self.semantic_dict())[:24],
        )

    def semantic_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "payload": thaw_json(self.payload),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.semantic_dict(),
            "event_id": self.event_id,
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class ComparisonRecordInput:
    """One source-linked value inside a required comparison series."""

    record_key: str
    simulation_time_us: int
    value: object
    source_event_ids: tuple[str, ...]
    calculation_id: str | None = None
    calculation_version: int | None = None
    record_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identifier(self.record_key, "comparison record key")
        _nonnegative_int(self.simulation_time_us, "comparison record time")
        value = _freeze_value(self.value, "comparison record value")
        source_ids = _canonical_identifiers(
            self.source_event_ids,
            "comparison record source event IDs",
            nonempty=True,
        )
        if (self.calculation_id is None) != (self.calculation_version is None):
            raise ValueError("comparison calculation ID and version must be paired")
        if self.calculation_id is not None:
            _require_identifier(self.calculation_id, "comparison calculation ID")
            _positive_int(self.calculation_version, "comparison calculation version")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source_event_ids", source_ids)
        object.__setattr__(
            self,
            "record_sha256",
            _canonical_sha256(self.identity_dict()),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "calculation_version": self.calculation_version,
            "record_key": self.record_key,
            "simulation_time_us": self.simulation_time_us,
            "source_event_ids": list(self.source_event_ids),
            "value": thaw_json(self.value),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "record_sha256": self.record_sha256}


@dataclass(frozen=True, slots=True)
class ComparisonSeriesInput:
    kind: ComparisonSeriesKind
    availability: ComparisonAvailability
    records: tuple[ComparisonRecordInput, ...] = ()
    unavailable_reason: str | None = None
    schema_id: str = COMPARISON_SERIES_SCHEMA_ID
    schema_version: int = COMPARISON_SERIES_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.kind) is not ComparisonSeriesKind:
            raise TypeError("comparison series kind is invalid")
        if type(self.availability) is not ComparisonAvailability:
            raise TypeError("comparison series availability is invalid")
        if type(self.records) is not tuple or any(
            type(item) is not ComparisonRecordInput for item in self.records
        ):
            raise TypeError("comparison series records must be an immutable typed tuple")
        records = tuple(sorted(self.records, key=lambda item: item.record_key))
        keys = tuple(item.record_key for item in records)
        if len(keys) != len(set(keys)):
            raise ValueError("comparison series record keys must be unique")
        if self.availability is ComparisonAvailability.AVAILABLE:
            if not records or self.unavailable_reason is not None:
                raise ValueError("available comparison series requires records only")
        elif self.availability is ComparisonAvailability.RECORDED_EMPTY:
            if records or self.unavailable_reason is not None:
                raise ValueError("recorded-empty comparison series cannot carry data")
        else:
            if records or self.unavailable_reason is None:
                raise ValueError("unavailable comparison series requires one reason")
            _bounded_text(self.unavailable_reason, "comparison unavailability reason")
        if (
            self.schema_id != COMPARISON_SERIES_SCHEMA_ID
            or self.schema_version != COMPARISON_SERIES_SCHEMA_VERSION
        ):
            raise ValueError("unsupported comparison series schema")
        object.__setattr__(self, "records", records)

    @classmethod
    def from_records(
        cls,
        kind: ComparisonSeriesKind,
        records: tuple[ComparisonRecordInput, ...],
    ) -> ComparisonSeriesInput:
        return cls(
            kind,
            (
                ComparisonAvailability.AVAILABLE
                if records
                else ComparisonAvailability.RECORDED_EMPTY
            ),
            records,
        )

    @classmethod
    def unavailable(
        cls,
        kind: ComparisonSeriesKind,
        reason: str,
    ) -> ComparisonSeriesInput:
        return cls(kind, ComparisonAvailability.UNAVAILABLE, unavailable_reason=reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "kind": self.kind.value,
            "records": [item.as_dict() for item in self.records],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class ComparisonRunInput:
    """One immutable, observation-policy-bound comparison projection.

    The mode and policy are part of the content commitment.  A postmortem input
    therefore cannot be reused as an as-observed input by changing only the mode
    passed to :func:`build_branch_comparison`.
    """

    run_id: str
    source_event_sha256: str
    timeline_sha256: str
    observation_mode: ObservationMode
    events: tuple[ComparisonEventInput, ...]
    series: tuple[ComparisonSeriesInput, ...]
    schema_id: str = COMPARISON_RUN_INPUT_SCHEMA_ID
    schema_version: int = COMPARISON_RUN_INPUT_SCHEMA_VERSION
    policy_id: str = field(init=False)
    input_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_run_id(self.run_id, "comparison run ID")
        _require_sha256(self.source_event_sha256, "comparison source event digest")
        _require_sha256(self.timeline_sha256, "comparison timeline digest")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("comparison run observation mode is invalid")
        object.__setattr__(
            self,
            "policy_id",
            ObservationPolicy(self.observation_mode).policy_id,
        )
        if type(self.events) is not tuple or not self.events or any(
            type(item) is not ComparisonEventInput for item in self.events
        ):
            raise TypeError("comparison run requires an immutable event tuple")
        expected_sequences = tuple(range(1, len(self.events) + 1))
        if tuple(item.sequence for item in self.events) != expected_sequences:
            raise ValueError("comparison event sequence must be contiguous from one")
        # Counterfactual collectors preserve append order. A batch can append exact
        # source-time flow rows before later receipt/state rows, so sequence—not a
        # re-sort by timestamp—is the authoritative comparison order.
        if type(self.series) is not tuple or tuple(
            item.kind for item in self.series
        ) != COMPARISON_SERIES_ORDER:
            raise ValueError("comparison run series inventory or order changed")
        event_ids = {item.event_id for item in self.events}
        if any(
            source_id not in event_ids
            for series in self.series
            for record in series.records
            for source_id in record.source_event_ids
        ):
            raise ValueError("comparison series cites an event outside its run input")
        if (
            self.schema_id != COMPARISON_RUN_INPUT_SCHEMA_ID
            or self.schema_version != COMPARISON_RUN_INPUT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported comparison run-input schema")
        _safe_presentation_payload(self.identity_dict(), self.observation_mode)
        object.__setattr__(
            self,
            "input_id",
            "comparison-run-input-" + _canonical_sha256(self.commitment_dict())[:24],
        )

    def series_for(self, kind: ComparisonSeriesKind) -> ComparisonSeriesInput:
        if type(kind) is not ComparisonSeriesKind:
            raise TypeError("comparison series lookup kind is invalid")
        return self.series[COMPARISON_SERIES_ORDER.index(kind)]

    def identity_dict(self) -> dict[str, object]:
        return {
            "events": [item.as_dict() for item in self.events],
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "run_id": self.run_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "series": [item.as_dict() for item in self.series],
            "source_event_sha256": self.source_event_sha256,
            "timeline_sha256": self.timeline_sha256,
        }

    def commitment_dict(self) -> dict[str, object]:
        """Return the payload-minimal preimage for the portable input ID.

        Event IDs already commit to each event's complete semantic payload.  The
        paired payload digest makes overlay provenance independently checkable,
        while avoiding disclosure of synchronized-prefix payloads.  Per-series
        digests bind even an intentionally unavailable series whose records and
        reason are not disclosed by the portable comparison.
        """

        return {
            "event_sources": [
                {
                    "event_id": item.event_id,
                    "payload_sha256": item.payload_sha256,
                }
                for item in self.events
            ],
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "run_id": self.run_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "series_sources": [
                {
                    "kind": item.kind.value,
                    "series_sha256": _canonical_sha256(item.as_dict()),
                }
                for item in self.series
            ],
            "source_event_sha256": self.source_event_sha256,
            "timeline_sha256": self.timeline_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "input_id": self.input_id}


@dataclass(frozen=True, slots=True)
class CounterfactualBranchInput:
    """Safe branch selection metadata; snapshot component payloads are excluded."""

    parent_run_id: str
    branch_run_id: str
    parent_prefix_sha256: str
    snapshot_sha256: str
    fork_time_us: int
    intervention: Mapping[str, object]
    mutation_manifest_sha256: str
    branch_mode: CounterfactualMode
    rng_policy: CounterfactualRngPolicy
    exogenous_reference_path_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_run_id(self.parent_run_id, "comparison parent run ID")
        _require_run_id(self.branch_run_id, "comparison branch run ID")
        if self.branch_run_id == self.parent_run_id:
            raise ValueError("counterfactual branch must differ from its parent")
        _require_sha256(self.parent_prefix_sha256, "parent prefix digest")
        _require_sha256(self.snapshot_sha256, "branch snapshot digest")
        _nonnegative_int(self.fork_time_us, "branch fork time")
        intervention = _freeze_mapping(self.intervention, "branch intervention")
        _require_sha256(self.mutation_manifest_sha256, "mutation manifest digest")
        if _canonical_sha256(intervention) != self.mutation_manifest_sha256:
            raise ValueError("mutation manifest digest differs from the intervention")
        if type(self.branch_mode) is not CounterfactualMode:
            raise TypeError("counterfactual branch mode is invalid")
        if type(self.rng_policy) is not CounterfactualRngPolicy:
            raise TypeError("counterfactual RNG policy is invalid")
        if self.branch_mode is CounterfactualMode.EXOGENOUS_REPLAY:
            if self.rng_policy is not CounterfactualRngPolicy.FIXED_EXOGENOUS_PATH:
                raise ValueError("exogenous comparison requires the fixed-path RNG policy")
            _require_sha256(
                self.exogenous_reference_path_sha256,
                "exogenous reference path digest",
            )
        else:
            if (
                self.rng_policy
                is not CounterfactualRngPolicy.FORK_SNAPSHOT_OWNED_RNG_STATE
            ):
                raise ValueError("endogenous comparison requires fork-owned RNG state")
            if self.exogenous_reference_path_sha256 is not None:
                raise ValueError("endogenous comparison cannot claim an exogenous path")
        object.__setattr__(self, "intervention", intervention)

    def as_dict(self) -> dict[str, object]:
        return {
            "branch_mode": self.branch_mode.value,
            "branch_run_id": self.branch_run_id,
            "exogenous_reference_path_sha256": self.exogenous_reference_path_sha256,
            "fork_time_us": self.fork_time_us,
            "intervention": thaw_json(self.intervention),
            "mutation_manifest_sha256": self.mutation_manifest_sha256,
            "parent_prefix_sha256": self.parent_prefix_sha256,
            "parent_run_id": self.parent_run_id,
            "rng_policy": self.rng_policy.value,
            "snapshot_sha256": self.snapshot_sha256,
        }


@dataclass(frozen=True, slots=True)
class ComparisonOverlayInput:
    """A safe paired overlay value plus payload-free source references."""

    kind: ComparisonOverlayKind
    availability: ComparisonAvailability
    parent_value: object | None = None
    branch_value: object | None = None
    unit: str | None = None
    calculation_id: str | None = None
    calculation_version: int | None = None
    parent_run_id: str | None = None
    branch_run_id: str | None = None
    parent_source_event_ids: tuple[str, ...] = ()
    branch_source_event_ids: tuple[str, ...] = ()
    parent_source_payload_sha256: tuple[str, ...] = ()
    branch_source_payload_sha256: tuple[str, ...] = ()
    evidence_scope: ComparisonEvidenceScope = ComparisonEvidenceScope.POLICY_VISIBLE
    required_capability: RevealCapability | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not ComparisonOverlayKind:
            raise TypeError("comparison overlay kind is invalid")
        if type(self.availability) is not ComparisonAvailability:
            raise TypeError("comparison overlay availability is invalid")
        if type(self.evidence_scope) is not ComparisonEvidenceScope:
            raise TypeError("comparison overlay evidence scope is invalid")
        if self.unit is not None:
            _require_identifier(self.unit, "comparison overlay unit")
        if (self.calculation_id is None) != (self.calculation_version is None):
            raise ValueError("overlay calculation ID and version must be paired")
        if self.calculation_id is not None:
            _require_identifier(self.calculation_id, "overlay calculation ID")
            _positive_int(self.calculation_version, "overlay calculation version")

        parent_ids, parent_digests = _canonical_source_pairs(
            self.parent_source_event_ids,
            self.parent_source_payload_sha256,
            "parent overlay sources",
        )
        branch_ids, branch_digests = _canonical_source_pairs(
            self.branch_source_event_ids,
            self.branch_source_payload_sha256,
            "branch overlay sources",
        )

        if self.availability is ComparisonAvailability.AVAILABLE:
            if self.parent_value is None or self.branch_value is None:
                raise ValueError("available comparison overlay requires both values")
            if self.unit is None or self.calculation_id is None:
                raise ValueError("available comparison overlay requires its calculation")
            _require_run_id(self.parent_run_id, "parent overlay run ID")
            _require_run_id(self.branch_run_id, "branch overlay run ID")
            if not parent_ids or not branch_ids:
                raise ValueError("available comparison overlay requires paired provenance")
            if self.unavailable_reason is not None:
                raise ValueError("available comparison overlay carries an unavailable reason")
            parent_value = _freeze_value(self.parent_value, "parent overlay value")
            branch_value = _freeze_value(self.branch_value, "branch overlay value")
        else:
            if any(
                value is not None
                for value in (
                    self.parent_value,
                    self.branch_value,
                    self.parent_run_id,
                    self.branch_run_id,
                )
            ) or parent_ids or branch_ids or parent_digests or branch_digests:
                raise ValueError("non-available comparison overlay cannot carry evidence")
            parent_value = None
            branch_value = None
            if self.availability is ComparisonAvailability.UNAVAILABLE:
                if self.unavailable_reason is None:
                    raise ValueError("unavailable comparison overlay requires a reason")
                _bounded_text(self.unavailable_reason, "overlay unavailability reason")
            elif self.unavailable_reason is not None:
                raise ValueError("recorded-empty overlay cannot carry an unavailable reason")

        reveal_scope = self.evidence_scope in {
            ComparisonEvidenceScope.POSTMORTEM_TRUTH,
            ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE,
        }
        if reveal_scope:
            expected = (
                RevealCapability.GROUND_TRUTH
                if self.evidence_scope is ComparisonEvidenceScope.POSTMORTEM_TRUTH
                else RevealCapability.HIDDEN_STATE
            )
            if self.required_capability is not expected:
                raise ValueError("comparison overlay reveal capability differs from its scope")
        elif self.required_capability is not None:
            raise ValueError("non-reveal comparison overlay claims a reveal capability")
        if (
            self.kind is ComparisonOverlayKind.AGENT_TRUTH
            and self.availability is ComparisonAvailability.AVAILABLE
            and self.evidence_scope
            is not ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE
        ):
            raise PermissionError(
                "available agent truth requires authorized postmortem hidden state"
            )

        object.__setattr__(self, "parent_value", parent_value)
        object.__setattr__(self, "branch_value", branch_value)
        object.__setattr__(self, "parent_source_event_ids", parent_ids)
        object.__setattr__(self, "branch_source_event_ids", branch_ids)
        object.__setattr__(self, "parent_source_payload_sha256", parent_digests)
        object.__setattr__(self, "branch_source_payload_sha256", branch_digests)

    @classmethod
    def unavailable(
        cls,
        kind: ComparisonOverlayKind,
        reason: str = "COMPARISON_EVIDENCE_NOT_SUPPLIED",
    ) -> ComparisonOverlayInput:
        return cls(kind, ComparisonAvailability.UNAVAILABLE, unavailable_reason=reason)

    def as_input_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "branch_run_id": self.branch_run_id,
            "branch_source_event_ids": list(self.branch_source_event_ids),
            "branch_source_payload_sha256": list(
                self.branch_source_payload_sha256
            ),
            "branch_value": (
                None if self.branch_value is None else thaw_json(self.branch_value)
            ),
            "calculation_id": self.calculation_id,
            "calculation_version": self.calculation_version,
            "evidence_scope": self.evidence_scope.value,
            "kind": self.kind.value,
            "parent_run_id": self.parent_run_id,
            "parent_source_event_ids": list(self.parent_source_event_ids),
            "parent_source_payload_sha256": list(
                self.parent_source_payload_sha256
            ),
            "parent_value": (
                None if self.parent_value is None else thaw_json(self.parent_value)
            ),
            "required_capability": (
                None
                if self.required_capability is None
                else self.required_capability.value
            ),
            "unit": self.unit,
            "unavailable_reason": self.unavailable_reason,
        }


def _canonical_source_pairs(
    event_ids: tuple[str, ...],
    payload_digests: tuple[str, ...],
    label: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(event_ids) is not tuple or type(payload_digests) is not tuple:
        raise TypeError(f"{label} must be immutable tuples")
    if len(event_ids) != len(payload_digests):
        raise ValueError(f"{label} IDs and payload digests differ in length")
    for event_id in event_ids:
        _require_identifier(event_id, f"{label} event ID")
    for digest in payload_digests:
        _require_sha256(digest, f"{label} payload digest")
    if len(event_ids) != len(set(event_ids)):
        raise ValueError(f"{label} contains duplicate event IDs")
    pairs = tuple(sorted(zip(event_ids, payload_digests, strict=True)))
    return tuple(item[0] for item in pairs), tuple(item[1] for item in pairs)


@dataclass(frozen=True, slots=True)
class SynchronizedPrefixV1:
    prefix_events: tuple[ComparisonEventInput, ...]
    parent_suffix: tuple[ComparisonEventInput, ...]
    branch_suffix: tuple[ComparisonEventInput, ...]
    _construction_token: InitVar[object]
    synchronized_prefix_sha256: str = field(init=False)
    parent_suffix_sha256: str = field(init=False)
    branch_suffix_sha256: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("synchronized prefixes require the comparison factory")
        if type(self.prefix_events) is not tuple or not self.prefix_events:
            raise ValueError("counterfactual comparison requires a nonempty shared prefix")
        if type(self.parent_suffix) is not tuple or type(self.branch_suffix) is not tuple:
            raise TypeError("counterfactual suffixes must be immutable tuples")
        if not self.parent_suffix and not self.branch_suffix:
            raise ValueError("counterfactual comparison requires a distinct suffix")
        if any(
            type(item) is not ComparisonEventInput
            for item in (*self.prefix_events, *self.parent_suffix, *self.branch_suffix)
        ):
            raise TypeError("counterfactual synchronization contains an invalid event")
        prefix_sha256 = _canonical_sha256(
            [item.semantic_dict() for item in self.prefix_events]
        )
        parent_sha256 = _canonical_sha256(
            [item.semantic_dict() for item in self.parent_suffix]
        )
        branch_sha256 = _canonical_sha256(
            [item.semantic_dict() for item in self.branch_suffix]
        )
        if parent_sha256 == branch_sha256:
            raise ValueError("counterfactual suffix digests are not distinct")
        object.__setattr__(self, "synchronized_prefix_sha256", prefix_sha256)
        object.__setattr__(self, "parent_suffix_sha256", parent_sha256)
        object.__setattr__(self, "branch_suffix_sha256", branch_sha256)

    @property
    def prefix_length(self) -> int:
        return len(self.prefix_events)

    def as_dict(self) -> dict[str, object]:
        return {
            "branch_suffix": [item.as_dict() for item in self.branch_suffix],
            "branch_suffix_sha256": self.branch_suffix_sha256,
            "parent_suffix": [item.as_dict() for item in self.parent_suffix],
            "parent_suffix_sha256": self.parent_suffix_sha256,
            "prefix_end_time_us": self.prefix_events[-1].simulation_time_us,
            "prefix_event_ids": [item.event_id for item in self.prefix_events],
            "prefix_event_sources": [
                {
                    "event_id": item.event_id,
                    "payload_sha256": item.payload_sha256,
                }
                for item in self.prefix_events
            ],
            "prefix_length": self.prefix_length,
            "synchronized_prefix_sha256": self.synchronized_prefix_sha256,
        }


@dataclass(frozen=True, slots=True)
class FirstDifferingEventV1:
    index: int
    divergence_time_us: int
    parent: ComparisonEventInput | None
    branch: ComparisonEventInput | None
    _construction_token: InitVar[object]
    divergence_event_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("first differences require the comparison factory")
        _nonnegative_int(self.index, "first differing event index")
        _nonnegative_int(self.divergence_time_us, "branch divergence time")
        if self.parent is None and self.branch is None:
            raise ValueError("first differing event cannot omit both sides")
        if self.parent is not None and type(self.parent) is not ComparisonEventInput:
            raise TypeError("first differing parent event is invalid")
        if self.branch is not None and type(self.branch) is not ComparisonEventInput:
            raise TypeError("first differing branch event is invalid")
        expected_time = min(
            item.simulation_time_us
            for item in (self.parent, self.branch)
            if item is not None
        )
        if self.divergence_time_us != expected_time:
            raise ValueError("branch divergence time differs from its first event")
        object.__setattr__(
            self,
            "divergence_event_id",
            "branch-divergence-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "branch": None if self.branch is None else self.branch.as_dict(),
            "divergence_time_us": self.divergence_time_us,
            "index": self.index,
            "parent": None if self.parent is None else self.parent.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "divergence_event_id": self.divergence_event_id}


@dataclass(frozen=True, slots=True)
class BranchIdentityV1:
    selection: CounterfactualBranchInput
    parent_source_event_sha256: str
    branch_source_event_sha256: str
    parent_input_id: str
    branch_input_id: str
    synchronized_prefix_sha256: str
    parent_suffix_sha256: str
    branch_suffix_sha256: str
    parent_timeline_sha256: str
    branch_timeline_sha256: str
    divergence_event_id: str
    divergence_time_us: int
    _construction_token: InitVar[object]
    branch_identity_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("branch identities require the comparison factory")
        if type(self.selection) is not CounterfactualBranchInput:
            raise TypeError("branch selection input is invalid")
        _require_sha256(
            self.parent_source_event_sha256,
            "branch identity parent source digest",
        )
        _require_sha256(
            self.branch_source_event_sha256,
            "branch identity branch source digest",
        )
        _require_identifier(self.parent_input_id, "branch identity parent input ID")
        _require_identifier(self.branch_input_id, "branch identity branch input ID")
        if self.parent_input_id == self.branch_input_id:
            raise ValueError("branch identity inputs must be distinct")
        for value, label in (
            (self.synchronized_prefix_sha256, "synchronized prefix digest"),
            (self.parent_suffix_sha256, "parent suffix digest"),
            (self.branch_suffix_sha256, "branch suffix digest"),
            (self.parent_timeline_sha256, "parent timeline digest"),
            (self.branch_timeline_sha256, "branch timeline digest"),
        ):
            _require_sha256(value, label)
        _require_identifier(self.divergence_event_id, "branch divergence event ID")
        _nonnegative_int(self.divergence_time_us, "branch divergence time")
        if self.divergence_time_us < self.selection.fork_time_us:
            raise ValueError("branch divergence precedes its fork")
        if self.parent_suffix_sha256 == self.branch_suffix_sha256:
            raise ValueError("branch identity does not bind distinct suffixes")
        object.__setattr__(
            self,
            "branch_identity_id",
            "branch-identity-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            **self.selection.as_dict(),
            "branch_input_id": self.branch_input_id,
            "branch_source_event_sha256": self.branch_source_event_sha256,
            "branch_suffix_sha256": self.branch_suffix_sha256,
            "branch_timeline_sha256": self.branch_timeline_sha256,
            "divergence_event_id": self.divergence_event_id,
            "divergence_time_us": self.divergence_time_us,
            "parent_input_id": self.parent_input_id,
            "parent_source_event_sha256": self.parent_source_event_sha256,
            "parent_suffix_sha256": self.parent_suffix_sha256,
            "parent_timeline_sha256": self.parent_timeline_sha256,
            "synchronized_prefix_sha256": self.synchronized_prefix_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "branch_identity_id": self.branch_identity_id}


@dataclass(frozen=True, slots=True)
class ComparisonRecordDeltaV1:
    record_key: str
    status: ComparisonRecordStatus
    parent: ComparisonRecordInput | None
    branch: ComparisonRecordInput | None
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("comparison record deltas require the comparison factory")
        _require_identifier(self.record_key, "comparison delta record key")
        if type(self.status) is not ComparisonRecordStatus:
            raise TypeError("comparison record status is invalid")
        if self.parent is not None and self.parent.record_key != self.record_key:
            raise ValueError("parent comparison record key differs")
        if self.branch is not None and self.branch.record_key != self.record_key:
            raise ValueError("branch comparison record key differs")
        expected = _record_status(self.parent, self.branch)
        if self.status is not expected:
            raise ValueError("comparison record status differs from its evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": None if self.branch is None else self.branch.as_dict(),
            "parent": None if self.parent is None else self.parent.as_dict(),
            "record_key": self.record_key,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class ComparisonSeriesDeltaV1:
    kind: ComparisonSeriesKind
    availability: ComparisonAvailability
    parent_availability: ComparisonAvailability
    branch_availability: ComparisonAvailability
    parent_series_sha256: str
    branch_series_sha256: str
    records: tuple[ComparisonRecordDeltaV1, ...]
    unavailable_reason: str | None
    _construction_token: InitVar[object]
    delta_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("comparison series deltas require the comparison factory")
        if type(self.kind) is not ComparisonSeriesKind:
            raise TypeError("comparison delta kind is invalid")
        for value in (
            self.availability,
            self.parent_availability,
            self.branch_availability,
        ):
            if type(value) is not ComparisonAvailability:
                raise TypeError("comparison delta availability is invalid")
        _require_sha256(self.parent_series_sha256, "parent comparison series digest")
        _require_sha256(self.branch_series_sha256, "branch comparison series digest")
        if type(self.records) is not tuple or any(
            type(item) is not ComparisonRecordDeltaV1 for item in self.records
        ):
            raise TypeError("comparison record delta inventory is invalid")
        if self.availability is ComparisonAvailability.AVAILABLE:
            if not self.records or self.unavailable_reason is not None:
                raise ValueError("available series delta requires records only")
        elif self.availability is ComparisonAvailability.RECORDED_EMPTY:
            if self.records or self.unavailable_reason is not None:
                raise ValueError("recorded-empty series delta cannot carry records")
        else:
            if self.records or self.unavailable_reason is None:
                raise ValueError("unavailable series delta requires one reason")
            _bounded_text(self.unavailable_reason, "series delta unavailability reason")
        object.__setattr__(
            self,
            "delta_id",
            self.kind.value.lower().replace("_", "-")
            + "-comparison-delta-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    @property
    def changed(self) -> bool:
        return self.parent_series_sha256 != self.branch_series_sha256

    def identity_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "branch_availability": self.branch_availability.value,
            "branch_series_sha256": self.branch_series_sha256,
            "changed": self.changed,
            "kind": self.kind.value,
            "parent_availability": self.parent_availability.value,
            "parent_series_sha256": self.parent_series_sha256,
            "records": [item.as_dict() for item in self.records],
            "unavailable_reason": self.unavailable_reason,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "delta_id": self.delta_id}


@dataclass(frozen=True, slots=True)
class ComparisonOverlayDeltaV1:
    overlay: ComparisonOverlayInput
    policy_grant_verified: bool
    _construction_token: InitVar[object]
    schema_id: str = COMPARISON_OVERLAY_SCHEMA_ID
    schema_version: int = COMPARISON_OVERLAY_SCHEMA_VERSION
    overlay_delta_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("comparison overlay deltas require the comparison factory")
        if type(self.overlay) is not ComparisonOverlayInput:
            raise TypeError("comparison overlay input is invalid")
        if type(self.policy_grant_verified) is not bool:
            raise TypeError("overlay grant result must be boolean")
        reveal = self.overlay.evidence_scope in {
            ComparisonEvidenceScope.POSTMORTEM_TRUTH,
            ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE,
        }
        if self.policy_grant_verified != reveal:
            raise ValueError("overlay grant result differs from its evidence scope")
        if (
            self.schema_id != COMPARISON_OVERLAY_SCHEMA_ID
            or self.schema_version != COMPARISON_OVERLAY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported comparison overlay schema")
        object.__setattr__(
            self,
            "overlay_delta_id",
            self.overlay.kind.value.lower().replace("_", "-")
            + "-comparison-overlay-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    @property
    def changed(self) -> bool:
        return self.overlay.parent_value != self.overlay.branch_value

    def identity_dict(self) -> dict[str, object]:
        payload = self.overlay.as_input_dict()
        # The factory consumes the capability and serializes only a safe result.
        payload.pop("required_capability")
        return {
            **payload,
            "changed": self.changed,
            "policy_grant_verified": self.policy_grant_verified,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "overlay_delta_id": self.overlay_delta_id}


@dataclass(frozen=True, slots=True)
class ComparisonTraceV1:
    availability: ComparisonTraceAvailability
    source_run_id: str
    source_event_sha256: str
    trace_payload: object | None
    unavailable_reason: str | None
    complete_required: bool
    _construction_token: InitVar[object]
    schema_id: str = COMPARISON_TRACE_SCHEMA_ID
    schema_version: int = COMPARISON_TRACE_SCHEMA_VERSION

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("comparison trace bindings require the comparison factory")
        _require_run_id(self.source_run_id, "comparison trace source run ID")
        _require_sha256(self.source_event_sha256, "comparison trace source digest")
        if type(self.availability) is not ComparisonTraceAvailability:
            raise TypeError("comparison trace availability is invalid")
        if type(self.complete_required) is not bool:
            raise TypeError("comparison trace completeness policy is invalid")
        if self.availability is ComparisonTraceAvailability.AVAILABLE:
            if self.trace_payload is None or self.unavailable_reason is not None:
                raise ValueError("available comparison trace requires its safe payload")
            payload = _freeze_mapping(self.trace_payload, "comparison trace payload")
        else:
            if self.trace_payload is not None or self.unavailable_reason is None:
                raise ValueError("unavailable comparison trace requires one reason")
            _bounded_text(self.unavailable_reason, "comparison trace unavailability reason")
            payload = None
        if (
            self.schema_id != COMPARISON_TRACE_SCHEMA_ID
            or self.schema_version != COMPARISON_TRACE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported comparison trace schema")
        object.__setattr__(self, "trace_payload", payload)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "complete_required": self.complete_required,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "trace_payload": (
                None if self.trace_payload is None else thaw_json(self.trace_payload)
            ),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class BranchComparisonV1:
    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    branch_identity: BranchIdentityV1
    synchronization: SynchronizedPrefixV1
    first_difference: FirstDifferingEventV1
    deltas: tuple[ComparisonSeriesDeltaV1, ...]
    overlays: tuple[ComparisonOverlayDeltaV1, ...]
    mechanistic_trace: ComparisonTraceV1
    _construction_token: InitVar[object]
    interpretation: str = COMPARISON_INTERPRETATION
    schema_id: str = BRANCH_COMPARISON_SCHEMA_ID
    schema_version: int = BRANCH_COMPARISON_SCHEMA_VERSION
    comparison_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("branch comparisons require build_branch_comparison")
        _require_run_id(self.source_run_id, "comparison source run ID")
        _require_sha256(self.source_event_sha256, "comparison source event digest")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("comparison observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("comparison policy differs from its observation mode")
        if type(self.branch_identity) is not BranchIdentityV1:
            raise TypeError("comparison branch identity is invalid")
        if (
            self.branch_identity.selection.parent_run_id != self.source_run_id
            or self.branch_identity.parent_source_event_sha256
            != self.source_event_sha256
        ):
            raise ValueError("comparison branch identity belongs to another parent")
        if type(self.synchronization) is not SynchronizedPrefixV1:
            raise TypeError("comparison synchronization is invalid")
        if type(self.first_difference) is not FirstDifferingEventV1:
            raise TypeError("comparison first difference is invalid")
        if (
            self.first_difference.index != self.synchronization.prefix_length
            or self.first_difference.divergence_event_id
            != self.branch_identity.divergence_event_id
        ):
            raise ValueError("comparison divergence roots disagree")
        if type(self.deltas) is not tuple or tuple(
            item.kind for item in self.deltas
        ) != COMPARISON_SERIES_ORDER:
            raise ValueError("comparison delta inventory or order changed")
        if type(self.overlays) is not tuple or tuple(
            item.overlay.kind for item in self.overlays
        ) != COMPARISON_OVERLAY_ORDER:
            raise ValueError("comparison overlay inventory or order changed")
        if type(self.mechanistic_trace) is not ComparisonTraceV1:
            raise TypeError("comparison trace binding is invalid")
        if self.mechanistic_trace.source_run_id != self.source_run_id:
            raise ValueError("comparison trace belongs to another run")
        if self.interpretation != COMPARISON_INTERPRETATION:
            raise ValueError("comparison interpretation caveat is mandatory")
        if (
            self.schema_id != BRANCH_COMPARISON_SCHEMA_ID
            or self.schema_version != BRANCH_COMPARISON_SCHEMA_VERSION
        ):
            raise ValueError("unsupported branch-comparison schema")
        _safe_presentation_payload(self.identity_dict(), self.observation_mode)
        object.__setattr__(
            self,
            "comparison_id",
            "branch-comparison-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "branch_identity": self.branch_identity.as_dict(),
            "deltas": [item.as_dict() for item in self.deltas],
            "first_difference": self.first_difference.as_dict(),
            "interpretation": self.interpretation,
            "mechanistic_trace": self.mechanistic_trace.as_dict(),
            "observation_mode": self.observation_mode.value,
            "overlays": [item.as_dict() for item in self.overlays],
            "policy_id": self.policy_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "synchronization": self.synchronization.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "comparison_id": self.comparison_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def build_branch_comparison(
    parent: ComparisonRunInput,
    branch: ComparisonRunInput,
    selection: CounterfactualBranchInput,
    observation_mode: ObservationMode,
    *,
    overlays: tuple[ComparisonOverlayInput, ...] = (),
    reveal_authorization: RevealAuthorization | None = None,
    mechanistic_trace: MechanisticTraceIndex | None = None,
    require_complete_trace: bool = False,
) -> BranchComparisonV1:
    """Build one exact parent/branch comparison from safe typed evidence.

    Reveal bearer grants are consumed only to authorize postmortem overlay inputs;
    the resulting portable contract records a boolean verification result and never
    serializes the grant, its ID, or its digests.
    """

    if type(parent) is not ComparisonRunInput or type(branch) is not ComparisonRunInput:
        raise TypeError("branch comparison requires typed parent and branch inputs")
    if type(selection) is not CounterfactualBranchInput:
        raise TypeError("branch comparison requires typed selection metadata")
    if type(observation_mode) is not ObservationMode:
        raise TypeError("branch comparison observation mode is invalid")
    expected_policy_id = ObservationPolicy(observation_mode).policy_id
    if any(
        item.observation_mode is not observation_mode
        or item.policy_id != expected_policy_id
        for item in (parent, branch)
    ):
        raise PermissionError(
            "comparison run inputs were projected under another observation policy"
        )
    if type(require_complete_trace) is not bool:
        raise TypeError("complete-trace policy must be boolean")
    if (
        parent.run_id != selection.parent_run_id
        or branch.run_id != selection.branch_run_id
    ):
        raise ValueError("comparison run inputs differ from branch selection")

    prefix_length = 0
    for left, right in zip(parent.events, branch.events):
        if left.semantic_dict() != right.semantic_dict():
            break
        prefix_length += 1
    if prefix_length == 0:
        raise ValueError("parent and branch have no synchronized event prefix")
    synchronization = SynchronizedPrefixV1(
        parent.events[:prefix_length],
        parent.events[prefix_length:],
        branch.events[prefix_length:],
        _construction_token=_CONSTRUCTION_TOKEN,
    )
    if selection.parent_prefix_sha256 != synchronization.synchronized_prefix_sha256:
        raise ValueError("declared parent prefix digest differs from synchronized events")
    left = (
        None if prefix_length >= len(parent.events) else parent.events[prefix_length]
    )
    right = (
        None if prefix_length >= len(branch.events) else branch.events[prefix_length]
    )
    divergence_time_us = min(
        item.simulation_time_us for item in (left, right) if item is not None
    )
    first = FirstDifferingEventV1(
        prefix_length,
        divergence_time_us,
        left,
        right,
        _construction_token=_CONSTRUCTION_TOKEN,
    )
    identity = BranchIdentityV1(
        selection,
        parent.source_event_sha256,
        branch.source_event_sha256,
        parent.input_id,
        branch.input_id,
        synchronization.synchronized_prefix_sha256,
        synchronization.parent_suffix_sha256,
        synchronization.branch_suffix_sha256,
        parent.timeline_sha256,
        branch.timeline_sha256,
        first.divergence_event_id,
        first.divergence_time_us,
        _construction_token=_CONSTRUCTION_TOKEN,
    )
    deltas = tuple(
        _build_series_delta(parent.series_for(kind), branch.series_for(kind))
        for kind in COMPARISON_SERIES_ORDER
    )
    overlay_inputs = _complete_overlay_inventory(overlays)
    overlay_deltas = tuple(
        _build_overlay_delta(
            item,
            parent,
            branch,
            observation_mode,
            reveal_authorization,
        )
        for item in overlay_inputs
    )
    trace = _build_trace_projection(
        parent,
        mechanistic_trace,
        require_complete_trace,
    )
    return BranchComparisonV1(
        source_run_id=parent.run_id,
        source_event_sha256=parent.source_event_sha256,
        observation_mode=observation_mode,
        policy_id=expected_policy_id,
        branch_identity=identity,
        synchronization=synchronization,
        first_difference=first,
        deltas=deltas,
        overlays=overlay_deltas,
        mechanistic_trace=trace,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def comparison_run_from_counterfactual_outcome(
    run_id: str,
    source_event_sha256: str,
    outcome: CounterfactualOutcome,
    observation_mode: ObservationMode,
) -> ComparisonRunInput:
    """Project an existing immutable counterfactual outcome into safe input data."""

    _require_run_id(run_id, "counterfactual comparison run ID")
    _require_sha256(source_event_sha256, "counterfactual comparison source digest")
    if type(outcome) is not CounterfactualOutcome:
        raise TypeError("comparison projection requires CounterfactualOutcome")
    if type(observation_mode) is not ObservationMode:
        raise TypeError("comparison projection observation mode is invalid")
    raw_timeline = [item.as_dict() for item in outcome.timeline]
    if _canonical_sha256(raw_timeline) != outcome.timeline_sha256:
        raise ValueError("counterfactual timeline digest does not verify")
    events = tuple(
        ComparisonEventInput(
            item.sequence,
            item.simulation_time_us,
            item.kind,
            item.payload,
        )
        for item in outcome.timeline
    )
    series = _extract_counterfactual_series(events, outcome)
    return ComparisonRunInput(
        run_id=run_id,
        source_event_sha256=source_event_sha256,
        timeline_sha256=outcome.timeline_sha256,
        observation_mode=observation_mode,
        events=events,
        series=series,
    )


def build_branch_comparison_from_counterfactual(
    report: CounterfactualReport,
    branch_run_id: str,
    observation_mode: ObservationMode,
    *,
    source_event_sha256: str | None = None,
    branch_source_event_sha256: str | None = None,
    parent_prefix_sha256: str | None = None,
    overlays: tuple[ComparisonOverlayInput, ...] = (),
    reveal_authorization: RevealAuthorization | None = None,
    mechanistic_trace: MechanisticTraceIndex | None = None,
    require_complete_trace: bool = False,
) -> BranchComparisonV1:
    """Convenience factory for the repository's existing branch report contract."""

    if type(report) is not CounterfactualReport:
        raise TypeError("comparison factory requires CounterfactualReport")
    _require_run_id(branch_run_id, "counterfactual branch run ID")
    parent_source = source_event_sha256 or report.original.timeline_sha256
    branch_source = branch_source_event_sha256 or report.branch.timeline_sha256
    parent = comparison_run_from_counterfactual_outcome(
        report.parent_run_id,
        parent_source,
        report.original,
        observation_mode,
    )
    branch = comparison_run_from_counterfactual_outcome(
        branch_run_id,
        branch_source,
        report.branch,
        observation_mode,
    )
    snapshot_sha256 = report.snapshot.sha256()
    synchronized_prefix_sha256 = _common_prefix_sha256(parent.events, branch.events)
    selection = CounterfactualBranchInput(
        parent_run_id=report.parent_run_id,
        branch_run_id=branch_run_id,
        parent_prefix_sha256=parent_prefix_sha256 or synchronized_prefix_sha256,
        snapshot_sha256=snapshot_sha256,
        fork_time_us=report.snapshot.fork_time_us,
        intervention=report.mutation_manifest.as_dict(),
        mutation_manifest_sha256=report.mutation_manifest.sha256(),
        branch_mode=report.mode,
        rng_policy=(
            CounterfactualRngPolicy.FIXED_EXOGENOUS_PATH
            if report.mode is CounterfactualMode.EXOGENOUS_REPLAY
            else CounterfactualRngPolicy.FORK_SNAPSHOT_OWNED_RNG_STATE
        ),
        exogenous_reference_path_sha256=report.exogenous_reference_path_sha256,
    )
    comparison = build_branch_comparison(
        parent,
        branch,
        selection,
        observation_mode,
        overlays=overlays,
        reveal_authorization=reveal_authorization,
        mechanistic_trace=mechanistic_trace,
        require_complete_trace=require_complete_trace,
    )
    if report.first_divergence.index != comparison.first_difference.index:
        raise ValueError("safe comparison divergence differs from branch report")
    return comparison


def _common_prefix_sha256(
    parent: tuple[ComparisonEventInput, ...],
    branch: tuple[ComparisonEventInput, ...],
) -> str:
    prefix: list[dict[str, object]] = []
    for left, right in zip(parent, branch):
        if left.semantic_dict() != right.semantic_dict():
            break
        prefix.append(left.semantic_dict())
    if not prefix:
        raise ValueError("parent and branch have no synchronized event prefix")
    return _canonical_sha256(prefix)


def comparison_overlay_inventory_from_sets(
    parent: OverlaySet,
    branch: OverlaySet,
    *,
    supplemental: tuple[ComparisonOverlayInput, ...] = (),
) -> tuple[ComparisonOverlayInput, ...]:
    """Project paired WO36-C overlay sets and complete the WO36-E inventory."""

    if type(parent) is not OverlaySet or type(branch) is not OverlaySet:
        raise TypeError("overlay comparison requires paired OverlaySet values")
    if (
        parent.observation_mode is not branch.observation_mode
        or parent.policy_id != branch.policy_id
        or parent.render_cursor_time_us != branch.render_cursor_time_us
    ):
        raise ValueError("paired overlay sets do not share one cursor policy")
    projected = tuple(
        _overlay_input_from_results(parent, branch, kind)
        for kind in OVERLAY_KIND_ORDER
    )
    return _complete_overlay_inventory((*projected, *supplemental))


def _build_series_delta(
    parent: ComparisonSeriesInput,
    branch: ComparisonSeriesInput,
) -> ComparisonSeriesDeltaV1:
    if parent.kind is not branch.kind:
        raise ValueError("paired comparison series kinds differ")
    parent_sha256 = _canonical_sha256(parent.as_dict())
    branch_sha256 = _canonical_sha256(branch.as_dict())
    if (
        parent.availability is ComparisonAvailability.UNAVAILABLE
        or branch.availability is ComparisonAvailability.UNAVAILABLE
    ):
        availability = ComparisonAvailability.UNAVAILABLE
        reason = (
            "PARENT="
            + (parent.unavailable_reason or parent.availability.value)
            + "; BRANCH="
            + (branch.unavailable_reason or branch.availability.value)
        )
        records: tuple[ComparisonRecordDeltaV1, ...] = ()
    elif not parent.records and not branch.records:
        availability = ComparisonAvailability.RECORDED_EMPTY
        reason = None
        records = ()
    else:
        availability = ComparisonAvailability.AVAILABLE
        reason = None
        left = {item.record_key: item for item in parent.records}
        right = {item.record_key: item for item in branch.records}
        records = tuple(
            ComparisonRecordDeltaV1(
                key,
                _record_status(left.get(key), right.get(key)),
                left.get(key),
                right.get(key),
                _construction_token=_CONSTRUCTION_TOKEN,
            )
            for key in sorted(set(left) | set(right))
        )
    return ComparisonSeriesDeltaV1(
        parent.kind,
        availability,
        parent.availability,
        branch.availability,
        parent_sha256,
        branch_sha256,
        records,
        reason,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _record_status(
    parent: ComparisonRecordInput | None,
    branch: ComparisonRecordInput | None,
) -> ComparisonRecordStatus:
    if parent is None:
        if branch is None:  # pragma: no cover - factory never creates this pair
            raise ValueError("comparison record delta omits both sides")
        return ComparisonRecordStatus.BRANCH_ONLY
    if branch is None:
        return ComparisonRecordStatus.PARENT_ONLY
    return (
        ComparisonRecordStatus.UNCHANGED
        if parent.record_sha256 == branch.record_sha256
        else ComparisonRecordStatus.CHANGED
    )


def _complete_overlay_inventory(
    overlays: tuple[ComparisonOverlayInput, ...],
) -> tuple[ComparisonOverlayInput, ...]:
    if type(overlays) is not tuple or any(
        type(item) is not ComparisonOverlayInput for item in overlays
    ):
        raise TypeError("comparison overlays must be an immutable typed tuple")
    by_kind: dict[ComparisonOverlayKind, ComparisonOverlayInput] = {}
    for item in overlays:
        if item.kind in by_kind:
            raise ValueError("comparison overlay kind is duplicated")
        by_kind[item.kind] = item
    return tuple(
        by_kind.get(kind, ComparisonOverlayInput.unavailable(kind))
        for kind in COMPARISON_OVERLAY_ORDER
    )


def _build_overlay_delta(
    overlay: ComparisonOverlayInput,
    parent: ComparisonRunInput,
    branch: ComparisonRunInput,
    mode: ObservationMode,
    grant: RevealAuthorization | None,
) -> ComparisonOverlayDeltaV1:
    if overlay.availability is ComparisonAvailability.AVAILABLE and (
        overlay.parent_run_id != parent.run_id or overlay.branch_run_id != branch.run_id
    ):
        raise ValueError("comparison overlay belongs to different branch roots")
    if overlay.availability is ComparisonAvailability.AVAILABLE:
        _validate_overlay_source_bindings(overlay, parent, branch)
    reveal = overlay.evidence_scope in {
        ComparisonEvidenceScope.POSTMORTEM_TRUTH,
        ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE,
    }
    if reveal:
        if mode is not ObservationMode.POSTMORTEM:
            raise ValueError("reveal comparison overlay requires POSTMORTEM mode")
        if type(grant) is not RevealAuthorization:
            raise ValueError("reveal comparison overlay requires a verified grant")
        if (
            grant.source_run_id != parent.run_id
            or grant.source_event_sha256 != parent.source_event_sha256
            or overlay.required_capability not in grant.capabilities
        ):
            raise ValueError("reveal comparison overlay grant binding or scope differs")
    return ComparisonOverlayDeltaV1(
        overlay,
        reveal,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _validate_overlay_source_bindings(
    overlay: ComparisonOverlayInput,
    parent: ComparisonRunInput,
    branch: ComparisonRunInput,
) -> None:
    """Require every portable overlay provenance pair to resolve exactly."""

    for label, run, event_ids, payload_digests in (
        (
            "parent",
            parent,
            overlay.parent_source_event_ids,
            overlay.parent_source_payload_sha256,
        ),
        (
            "branch",
            branch,
            overlay.branch_source_event_ids,
            overlay.branch_source_payload_sha256,
        ),
    ):
        recorded = {item.event_id: item.payload_sha256 for item in run.events}
        for event_id, payload_sha256 in zip(
            event_ids,
            payload_digests,
            strict=True,
        ):
            if recorded.get(event_id) != payload_sha256:
                raise ValueError(
                    f"{label} comparison overlay source event binding differs"
                )


def _build_trace_projection(
    parent: ComparisonRunInput,
    trace: MechanisticTraceIndex | None,
    complete_required: bool,
) -> ComparisonTraceV1:
    if trace is None:
        return ComparisonTraceV1(
            ComparisonTraceAvailability.UNAVAILABLE,
            parent.run_id,
            parent.source_event_sha256,
            None,
            "MECHANISTIC_TRACE_NOT_SUPPLIED",
            complete_required,
            _construction_token=_CONSTRUCTION_TOKEN,
        )
    if type(trace) is not MechanisticTraceIndex:
        raise TypeError("comparison mechanistic trace is invalid")
    if trace.source_run_id != parent.run_id:
        raise ValueError("comparison mechanistic trace belongs to another run")
    if complete_required and any(not item.complete for item in trace.traces):
        raise ValueError("comparison requires a complete trace for every player action")
    payload = {
        "all_actions_complete": all(item.complete for item in trace.traces),
        "complete_action_ids": [item.action_id for item in trace.traces if item.complete],
        "incomplete_action_ids": [
            item.action_id for item in trace.traces if not item.complete
        ],
        "index_id": trace.index_id,
        "interpretation": trace.interpretation,
        "lineage_sha256": trace.lineage_sha256,
        "traces": [item.as_dict() for item in trace.traces],
    }
    return ComparisonTraceV1(
        ComparisonTraceAvailability.AVAILABLE,
        parent.run_id,
        trace.source_event_sha256,
        payload,
        None,
        complete_required,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _extract_counterfactual_series(
    events: tuple[ComparisonEventInput, ...],
    outcome: CounterfactualOutcome,
) -> tuple[ComparisonSeriesInput, ...]:
    selected: dict[ComparisonSeriesKind, list[ComparisonRecordInput]] = {
        kind: [] for kind in COMPARISON_SERIES_ORDER
    }
    for event in events:
        marker = _event_marker(event)
        kinds: set[ComparisonSeriesKind] = set()
        if event.kind == "PLAYER_ACTION" or any(
            token in marker
            for token in ("ORDER", "ROUTE", "CANCEL")
        ):
            kinds.add(ComparisonSeriesKind.ORDERS)
        if any(
            token in marker
            for token in ("QUEUE", "ORDER_ADDED", "ORDER_REDUCED", "ORDER_REPLACED")
        ) or any(
            _payload_has_key(event.payload, key)
            for key in ("queue_ahead_quantity", "subscribed_depth")
        ):
            kinds.add(ComparisonSeriesKind.QUEUE_STATES)
        if "FILL" in marker:
            kinds.add(ComparisonSeriesKind.FILLS)
        if event.kind == "ENDOGENOUS_FLOW" or "VENUE_MARKET_FLOW" in marker:
            kinds.add(ComparisonSeriesKind.ENDOGENOUS_MARKET_PATH)
        for kind in kinds:
            selected[kind].append(
                ComparisonRecordInput(
                    f"{kind.value.lower()}:{event.sequence:08d}",
                    event.simulation_time_us,
                    thaw_json(event.payload),
                    (event.event_id,),
                )
            )

    terminal = events[-1]
    for name in sorted(outcome.metrics):
        _require_identifier(name, "declared counterfactual metric name")
        selected[ComparisonSeriesKind.DECLARED_METRICS].append(
            ComparisonRecordInput(
                f"metric:{name}",
                terminal.simulation_time_us,
                outcome.metrics[name],
                (terminal.event_id,),
                "counterfactual.declared-metric.v1",
                1,
            )
        )
    return tuple(
        ComparisonSeriesInput.from_records(kind, tuple(selected[kind]))
        for kind in COMPARISON_SERIES_ORDER
    )


def _event_marker(event: ComparisonEventInput) -> str:
    payload = event.payload
    candidates: list[object] = [event.kind]
    candidates.extend(payload.get(key) for key in ("type", "event_type", "kind"))
    nested = payload.get("data")
    if isinstance(nested, Mapping):
        candidates.extend(nested.get(key) for key in ("type", "event_type", "kind"))
    return "|".join(str(value).upper() for value in candidates if value is not None)


def _payload_has_key(value: object, target: str) -> bool:
    if isinstance(value, Mapping):
        return target in value or any(
            _payload_has_key(item, target) for item in value.values()
        )
    if type(value) in {list, tuple}:
        return any(_payload_has_key(item, target) for item in value)
    return False


def _overlay_input_from_results(
    parent_set: OverlaySet,
    branch_set: OverlaySet,
    kind: OverlayKind,
) -> ComparisonOverlayInput:
    parent = parent_set.overlay(kind)
    branch = branch_set.overlay(kind)
    comparison_kind = ComparisonOverlayKind(kind.value)
    if (
        parent.availability is not OverlayAvailability.AVAILABLE
        or branch.availability is not OverlayAvailability.AVAILABLE
    ):
        return ComparisonOverlayInput(
            comparison_kind,
            ComparisonAvailability.UNAVAILABLE,
            unit=parent.unit.value,
            calculation_id=parent.calculation.calculation_id,
            calculation_version=parent.calculation.calculation_version,
            unavailable_reason=(
                "PARENT="
                + parent.availability.value
                + "; BRANCH="
                + branch.availability.value
            ),
        )
    all_sources = (*parent.source_events, *branch.source_events)
    scope = _scope_from_overlay_sources(all_sources)
    capability = {
        ComparisonEvidenceScope.POSTMORTEM_TRUTH: RevealCapability.GROUND_TRUTH,
        ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE: RevealCapability.HIDDEN_STATE,
    }.get(scope)
    return ComparisonOverlayInput(
        comparison_kind,
        ComparisonAvailability.AVAILABLE,
        parent_value=parent.value,
        branch_value=branch.value,
        unit=parent.unit.value,
        calculation_id=parent.calculation.calculation_id,
        calculation_version=parent.calculation.calculation_version,
        parent_run_id=parent_set.source_run_id,
        branch_run_id=branch_set.source_run_id,
        parent_source_event_ids=tuple(item.event_id for item in parent.source_events),
        branch_source_event_ids=tuple(item.event_id for item in branch.source_events),
        parent_source_payload_sha256=tuple(
            item.payload_sha256 for item in parent.source_events
        ),
        branch_source_payload_sha256=tuple(
            item.payload_sha256 for item in branch.source_events
        ),
        evidence_scope=scope,
        required_capability=capability,
    )


def _scope_from_overlay_sources(values: tuple[object, ...]) -> ComparisonEvidenceScope:
    kinds = {item.source_kind for item in values}
    if EvidenceSourceKind.REVEALED_HIDDEN_STATE in kinds:
        return ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE
    if EvidenceSourceKind.REVEALED_GROUND_TRUTH in kinds:
        return ComparisonEvidenceScope.POSTMORTEM_TRUTH
    return ComparisonEvidenceScope.DECLARED_CALCULATION


__all__ = [
    "BRANCH_COMPARISON_SCHEMA_ID",
    "BRANCH_COMPARISON_SCHEMA_VERSION",
    "COMPARISON_INTERPRETATION",
    "COMPARISON_OVERLAY_ORDER",
    "COMPARISON_SERIES_ORDER",
    "BranchComparisonV1",
    "BranchIdentityV1",
    "ComparisonAvailability",
    "ComparisonEvidenceScope",
    "ComparisonEventInput",
    "ComparisonOverlayDeltaV1",
    "ComparisonOverlayInput",
    "ComparisonOverlayKind",
    "ComparisonRecordDeltaV1",
    "ComparisonRecordInput",
    "ComparisonRecordStatus",
    "ComparisonRunInput",
    "ComparisonSeriesDeltaV1",
    "ComparisonSeriesInput",
    "ComparisonSeriesKind",
    "ComparisonTraceAvailability",
    "ComparisonTraceV1",
    "CounterfactualBranchInput",
    "CounterfactualRngPolicy",
    "FirstDifferingEventV1",
    "SynchronizedPrefixV1",
    "build_branch_comparison",
    "build_branch_comparison_from_counterfactual",
    "comparison_overlay_inventory_from_sets",
    "comparison_run_from_counterfactual_outcome",
]
