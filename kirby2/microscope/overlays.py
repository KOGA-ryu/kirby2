"""Deterministic, source-linked replay microscope overlay models.

The module deliberately accepts only event identities retained by a closed window
projection assembled from exact-recorded ``ObservationQueryResult`` snapshots.
Repeated updates to one series therefore remain distinct without accepting arbitrary
numeric samples.  Calculation inputs are read back from those policy-enforced queried
values; callers cannot supply a convenient price, size, or quantity beside an
unrelated event ID.  Output models retain only the source-event references used at
the requested cursor and never serialize the projection or full-run inventory.

All V1 calculations use exact integers.  Fixed-point scales and rounding rules are
part of the calculation contracts below, so a later formula change requires a new
contract and overlay schema version rather than an in-place reinterpretation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, TypeAlias

from kirby2.immutable import thaw_json

from .data_age import DataAge
from .policy import ObservationMode, ObservationPolicy
from .query import (
    EvidenceSourceKind,
    ObservationQueryResult,
    QueriedValue,
    RecordDisposition,
    SelectionKind,
)


OVERLAY_SCHEMA_VERSION = 1
OVERLAY_SET_SCHEMA_ID = "KIRBY2_MICROSCOPE_OVERLAY_SET_V1"
OVERLAY_SET_SCHEMA_VERSION = 1
OVERLAY_WINDOW_PROJECTION_SCHEMA_ID = (
    "KIRBY2_MICROSCOPE_OVERLAY_WINDOW_PROJECTION_V1"
)
OVERLAY_WINDOW_PROJECTION_SCHEMA_VERSION = 1
OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_ID = (
    "KIRBY2_MICROSCOPE_OVERLAY_WINDOW_PROJECTION_RECEIPT_V1"
)
OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_VERSION = 1

TRADE_VELOCITY_WINDOW_US = 1_000_000
CANCELLATION_VELOCITY_WINDOW_US = 1_000_000
REPLENISHMENT_WINDOW_US = 1_000_000
RELATIVE_VOLUME_WINDOW_US = 60_000_000
SHORT_TERM_VOLATILITY_WINDOW_US = 5_000_000

RATIO_SCALE_PPM = 1_000_000
MICRO_UNITS_PER_UNIT = 1_000_000
MICROSECONDS_PER_SECOND = 1_000_000
MICROBASIS_POINTS_PER_RETURN = 10_000_000_000

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUERY_ID = re.compile(r"^observation-query-[0-9a-f]{24}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")
_CONSTRUCTION_TOKEN = object()
_PROJECTION_CONSTRUCTION_TOKEN = object()


def _require_identifier(value: object, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")


def _require_source_identity(source_run_id: str, source_event_sha256: str) -> None:
    if type(source_run_id) is not str or _RUN_ID.fullmatch(source_run_id) is None:
        raise ValueError("overlay source run ID is invalid")
    _require_sha256(source_event_sha256, "overlay source event digest")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class OverlayKind(str, Enum):
    SPREAD = "SPREAD"
    MICROPRICE = "MICROPRICE"
    IMBALANCE = "IMBALANCE"
    TRADE_VELOCITY = "TRADE_VELOCITY"
    CANCELLATION_VELOCITY = "CANCELLATION_VELOCITY"
    REPLENISHMENT = "REPLENISHMENT"
    RELATIVE_VOLUME = "RELATIVE_VOLUME"
    SHORT_TERM_VOLATILITY = "SHORT_TERM_VOLATILITY"
    IMPLEMENTATION_SHORTFALL = "IMPLEMENTATION_SHORTFALL"


OVERLAY_KIND_ORDER = (
    OverlayKind.SPREAD,
    OverlayKind.MICROPRICE,
    OverlayKind.IMBALANCE,
    OverlayKind.TRADE_VELOCITY,
    OverlayKind.CANCELLATION_VELOCITY,
    OverlayKind.REPLENISHMENT,
    OverlayKind.RELATIVE_VOLUME,
    OverlayKind.SHORT_TERM_VOLATILITY,
    OverlayKind.IMPLEMENTATION_SHORTFALL,
)


class OverlayAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class OverlayUnavailableReason(str, Enum):
    SOURCE_NOT_SELECTED = "SOURCE_NOT_SELECTED"
    SOURCE_FIELDS_MISSING = "SOURCE_FIELDS_MISSING"
    SOURCE_FIELDS_INVALID = "SOURCE_FIELDS_INVALID"
    NO_EVENTS_IN_WINDOW = "NO_EVENTS_IN_WINDOW"
    INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"
    ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
    BASELINE_NOT_SELECTED = "BASELINE_NOT_SELECTED"
    BASELINE_WINDOW_MISMATCH = "BASELINE_WINDOW_MISMATCH"
    EXECUTION_ARRIVAL_NOT_SELECTED = "EXECUTION_ARRIVAL_NOT_SELECTED"
    EXECUTION_FILLS_NOT_SELECTED = "EXECUTION_FILLS_NOT_SELECTED"
    EVENT_CHRONOLOGY_INVALID = "EVENT_CHRONOLOGY_INVALID"
    EXECUTION_IDENTITY_MISMATCH = "EXECUTION_IDENTITY_MISMATCH"
    SEMANTIC_ROLE_MISMATCH = "SEMANTIC_ROLE_MISMATCH"
    ZERO_WINDOW_DURATION = "ZERO_WINDOW_DURATION"


class OverlayRecordRole(str, Enum):
    """Closed semantic roles emitted by a governed observation adapter.

    V1 role classification requires an exact series family, evidence plane, and
    matching ``record_role`` field.  The current DEV-0006 ingress vocabulary does
    not emit these richer records, so those sources correctly produce typed
    unavailability until a future adapter version explicitly adds the contract.
    """

    TOP_OF_BOOK = "TOP_OF_BOOK"
    TRADE = "TRADE"
    CANCELLATION = "CANCELLATION"
    REPLENISHMENT = "REPLENISHMENT"
    RELATIVE_VOLUME_BASELINE = "RELATIVE_VOLUME_BASELINE"
    EXECUTION_ARRIVAL = "EXECUTION_ARRIVAL"
    EXECUTION_FILL = "EXECUTION_FILL"


class OverlayUnit(str, Enum):
    TICKS = "TICKS"
    MICROTICKS = "MICROTICKS"
    SIGNED_RATIO_PPM = "SIGNED_RATIO_PPM"
    MICROTRADES_PER_SECOND = "MICROTRADES_PER_SECOND"
    MICROSHARES_PER_SECOND = "MICROSHARES_PER_SECOND"
    RATIO_PPM = "RATIO_PPM"
    MICROBASIS_POINTS = "MICROBASIS_POINTS"
    X2_TICK_SHARES = "X2_TICK_SHARES"


class OverlayWindowBasis(str, Enum):
    INSTANTANEOUS_AT_CURSOR = "INSTANTANEOUS_AT_CURSOR"
    TRAILING_CLOSED_INTERVAL = "TRAILING_CLOSED_INTERVAL"
    SESSION_START_TO_CURSOR = "SESSION_START_TO_CURSOR"


class OverlayRoundingRule(str, Enum):
    EXACT_INTEGER = "EXACT_INTEGER"
    SIGNED_TIES_TO_EVEN = "SIGNED_TIES_TO_EVEN"
    NONNEGATIVE_INTEGER_SQRT_FLOOR = "NONNEGATIVE_INTEGER_SQRT_FLOOR"


@dataclass(frozen=True, slots=True)
class DerivedCalculationContract:
    """A versioned declaration of one exact derived calculation."""

    calculation_id: str
    calculation_version: int
    formula: str
    required_fields: tuple[str, ...]
    rounding_rule: OverlayRoundingRule
    fixed_point_scale: int

    def __post_init__(self) -> None:
        _require_identifier(self.calculation_id, "overlay calculation ID")
        if type(self.calculation_version) is not int or self.calculation_version <= 0:
            raise ValueError("overlay calculation version must be a positive integer")
        if type(self.formula) is not str or not self.formula:
            raise ValueError("overlay calculation formula must be declared")
        if type(self.required_fields) is not tuple or not self.required_fields:
            raise TypeError("overlay calculation fields must be a nonempty tuple")
        if any(
            type(item) is not str or _IDENTIFIER.fullmatch(item) is None
            for item in self.required_fields
        ):
            raise ValueError("overlay calculation fields contain an invalid identifier")
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("overlay calculation fields must be unique")
        if type(self.rounding_rule) is not OverlayRoundingRule:
            raise TypeError("overlay rounding rule is invalid")
        if type(self.fixed_point_scale) is not int or self.fixed_point_scale <= 0:
            raise ValueError("overlay fixed-point scale must be a positive integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "calculation_version": self.calculation_version,
            "fixed_point_scale": self.fixed_point_scale,
            "formula": self.formula,
            "required_fields": list(self.required_fields),
            "rounding_rule": self.rounding_rule.value,
        }


@dataclass(frozen=True, slots=True)
class OverlaySpecification:
    """Closed V1 identity, unit, window, and formula for one overlay."""

    kind: OverlayKind
    schema_id: str
    schema_version: int
    unit: OverlayUnit
    window_basis: OverlayWindowBasis
    lookback_us: int | None
    calculation: DerivedCalculationContract

    def __post_init__(self) -> None:
        if type(self.kind) is not OverlayKind:
            raise TypeError("overlay specification kind is invalid")
        _require_identifier(self.schema_id, "overlay schema ID")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("overlay schema version must be positive")
        if type(self.unit) is not OverlayUnit:
            raise TypeError("overlay unit is invalid")
        if type(self.window_basis) is not OverlayWindowBasis:
            raise TypeError("overlay window basis is invalid")
        if self.window_basis is OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR:
            if self.lookback_us != 0 or type(self.lookback_us) is not int:
                raise ValueError("instantaneous overlay lookback must be exact zero")
        elif self.window_basis is OverlayWindowBasis.TRAILING_CLOSED_INTERVAL:
            if type(self.lookback_us) is not int or self.lookback_us <= 0:
                raise ValueError("trailing overlay lookback must be positive")
        elif self.lookback_us is not None:
            raise ValueError("session-to-cursor overlay cannot carry a lookback")
        if type(self.calculation) is not DerivedCalculationContract:
            raise TypeError("overlay calculation contract is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation": self.calculation.as_dict(),
            "kind": self.kind.value,
            "lookback_us": self.lookback_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "unit": self.unit.value,
            "window_basis": self.window_basis.value,
        }


SPREAD_SPECIFICATION = OverlaySpecification(
    OverlayKind.SPREAD,
    "KIRBY2_MICROSCOPE_SPREAD_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.TICKS,
    OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR,
    0,
    DerivedCalculationContract(
        "microscope.spread.v1",
        1,
        "best_ask_ticks-best_bid_ticks",
        ("best_ask_ticks", "best_bid_ticks"),
        OverlayRoundingRule.EXACT_INTEGER,
        1,
    ),
)
MICROPRICE_SPECIFICATION = OverlaySpecification(
    OverlayKind.MICROPRICE,
    "KIRBY2_MICROSCOPE_MICROPRICE_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.MICROTICKS,
    OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR,
    0,
    DerivedCalculationContract(
        "microscope.microprice.v1",
        1,
        "round_div_even((best_ask_ticks*best_bid_size+best_bid_ticks*"
        "best_ask_size)*1000000,best_bid_size+best_ask_size)",
        (
            "best_ask_size",
            "best_ask_ticks",
            "best_bid_size",
            "best_bid_ticks",
        ),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        MICRO_UNITS_PER_UNIT,
    ),
)
IMBALANCE_SPECIFICATION = OverlaySpecification(
    OverlayKind.IMBALANCE,
    "KIRBY2_MICROSCOPE_IMBALANCE_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.SIGNED_RATIO_PPM,
    OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR,
    0,
    DerivedCalculationContract(
        "microscope.top-level-imbalance.v1",
        1,
        "round_div_even((best_bid_size-best_ask_size)*1000000,best_bid_size+best_ask_size)",
        ("best_ask_size", "best_bid_size"),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        RATIO_SCALE_PPM,
    ),
)
TRADE_VELOCITY_SPECIFICATION = OverlaySpecification(
    OverlayKind.TRADE_VELOCITY,
    "KIRBY2_MICROSCOPE_TRADE_VELOCITY_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.MICROTRADES_PER_SECOND,
    OverlayWindowBasis.TRAILING_CLOSED_INTERVAL,
    TRADE_VELOCITY_WINDOW_US,
    DerivedCalculationContract(
        "microscope.trade-velocity.v1",
        1,
        "round_div_even(trade_count*1000000*1000000,window_duration_us)",
        ("trade_count", "window_duration_us"),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        MICRO_UNITS_PER_UNIT,
    ),
)
CANCELLATION_VELOCITY_SPECIFICATION = OverlaySpecification(
    OverlayKind.CANCELLATION_VELOCITY,
    "KIRBY2_MICROSCOPE_CANCELLATION_VELOCITY_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.MICROSHARES_PER_SECOND,
    OverlayWindowBasis.TRAILING_CLOSED_INTERVAL,
    CANCELLATION_VELOCITY_WINDOW_US,
    DerivedCalculationContract(
        "microscope.cancellation-velocity.v1",
        1,
        "round_div_even(sum(cancelled_quantity)*1000000*1000000,window_duration_us)",
        ("cancelled_quantity", "window_duration_us"),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        MICRO_UNITS_PER_UNIT,
    ),
)
REPLENISHMENT_SPECIFICATION = OverlaySpecification(
    OverlayKind.REPLENISHMENT,
    "KIRBY2_MICROSCOPE_REPLENISHMENT_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.MICROSHARES_PER_SECOND,
    OverlayWindowBasis.TRAILING_CLOSED_INTERVAL,
    REPLENISHMENT_WINDOW_US,
    DerivedCalculationContract(
        "microscope.replenishment.v1",
        1,
        "round_div_even(sum(added_quantity)*1000000*1000000,window_duration_us)",
        ("added_quantity", "window_duration_us"),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        MICRO_UNITS_PER_UNIT,
    ),
)
RELATIVE_VOLUME_SPECIFICATION = OverlaySpecification(
    OverlayKind.RELATIVE_VOLUME,
    "KIRBY2_MICROSCOPE_RELATIVE_VOLUME_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.RATIO_PPM,
    OverlayWindowBasis.TRAILING_CLOSED_INTERVAL,
    RELATIVE_VOLUME_WINDOW_US,
    DerivedCalculationContract(
        "microscope.relative-volume.v1",
        1,
        "round_div_even(sum(quantity)*1000000,expected_volume)",
        ("expected_volume", "quantity", "window_duration_us"),
        OverlayRoundingRule.SIGNED_TIES_TO_EVEN,
        RATIO_SCALE_PPM,
    ),
)
SHORT_TERM_VOLATILITY_SPECIFICATION = OverlaySpecification(
    OverlayKind.SHORT_TERM_VOLATILITY,
    "KIRBY2_MICROSCOPE_SHORT_TERM_VOLATILITY_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.MICROBASIS_POINTS,
    OverlayWindowBasis.TRAILING_CLOSED_INTERVAL,
    SHORT_TERM_VOLATILITY_WINDOW_US,
    DerivedCalculationContract(
        "microscope.short-term-volatility.v1",
        1,
        "midpoint_x2=best_bid_ticks+best_ask_ticks;isqrt(sum(round_div_even("
        "(right_midpoint_x2-left_midpoint_x2)*10000000000,left_midpoint_x2)^2))",
        ("best_ask_ticks", "best_bid_ticks", "window_duration_us"),
        OverlayRoundingRule.NONNEGATIVE_INTEGER_SQRT_FLOOR,
        MICROBASIS_POINTS_PER_RETURN,
    ),
)
IMPLEMENTATION_SHORTFALL_SPECIFICATION = OverlaySpecification(
    OverlayKind.IMPLEMENTATION_SHORTFALL,
    "KIRBY2_MICROSCOPE_IMPLEMENTATION_SHORTFALL_OVERLAY_V1",
    OVERLAY_SCHEMA_VERSION,
    OverlayUnit.X2_TICK_SHARES,
    OverlayWindowBasis.SESSION_START_TO_CURSOR,
    None,
    DerivedCalculationContract(
        "microscope.implementation-shortfall.v1",
        1,
        "sum(side_sign*(price_x2-arrival_midpoint_x2)*quantity)",
        (
            "arrival_midpoint_x2",
            "correlation_id",
            "execution_id",
            "order_id",
            "price_x2",
            "quantity",
            "side",
        ),
        OverlayRoundingRule.EXACT_INTEGER,
        1,
    ),
)

OVERLAY_SPECIFICATIONS = (
    SPREAD_SPECIFICATION,
    MICROPRICE_SPECIFICATION,
    IMBALANCE_SPECIFICATION,
    TRADE_VELOCITY_SPECIFICATION,
    CANCELLATION_VELOCITY_SPECIFICATION,
    REPLENISHMENT_SPECIFICATION,
    RELATIVE_VOLUME_SPECIFICATION,
    SHORT_TERM_VOLATILITY_SPECIFICATION,
    IMPLEMENTATION_SHORTFALL_SPECIFICATION,
)


@dataclass(frozen=True, slots=True)
class OverlayWindow:
    basis: OverlayWindowBasis
    start_time_us: int
    end_time_us: int
    lookback_us: int | None
    window_projection_id: str

    def __post_init__(self) -> None:
        if type(self.basis) is not OverlayWindowBasis:
            raise TypeError("overlay window basis is invalid")
        if (
            type(self.window_projection_id) is not str
            or not self.window_projection_id.startswith("overlay-window-projection-")
            or len(self.window_projection_id)
            != len("overlay-window-projection-") + 24
        ):
            raise ValueError("overlay window projection ID is invalid")
        if (
            type(self.start_time_us) is not int
            or type(self.end_time_us) is not int
            or self.start_time_us < 0
            or self.end_time_us < self.start_time_us
        ):
            raise ValueError("overlay window bounds must be ordered microseconds")
        if self.basis is OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR:
            if self.start_time_us != self.end_time_us or self.lookback_us != 0:
                raise ValueError("instantaneous overlay window is inconsistent")
        elif self.basis is OverlayWindowBasis.TRAILING_CLOSED_INTERVAL:
            if type(self.lookback_us) is not int or self.lookback_us <= 0:
                raise ValueError("trailing overlay window lookback must be positive")
            if self.end_time_us - self.start_time_us > self.lookback_us:
                raise ValueError("trailing overlay bounds exceed the declared lookback")
        elif self.start_time_us != 0 or self.lookback_us is not None:
            raise ValueError("session overlay window must start at zero")

    def contains(self, policy_visible_at_time_us: int) -> bool:
        if type(policy_visible_at_time_us) is not int:
            raise TypeError("overlay window lookup time must be an exact integer")
        return self.start_time_us <= policy_visible_at_time_us <= self.end_time_us

    @property
    def duration_us(self) -> int:
        """Actual serialized duration, including early-session truncation."""

        return self.end_time_us - self.start_time_us

    def as_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis.value,
            "duration_us": self.duration_us,
            "end_time_us": self.end_time_us,
            "lookback_us": self.lookback_us,
            "start_time_us": self.start_time_us,
            "window_projection_id": self.window_projection_id,
        }


_ROLE_SERIES_ROOT = {
    OverlayRecordRole.TOP_OF_BOOK: "market.top-of-book",
    OverlayRecordRole.TRADE: "market.trade",
    OverlayRecordRole.CANCELLATION: "market.cancellation",
    OverlayRecordRole.REPLENISHMENT: "market.replenishment",
    OverlayRecordRole.RELATIVE_VOLUME_BASELINE: (
        "market.relative-volume-baseline"
    ),
    OverlayRecordRole.EXECUTION_ARRIVAL: "execution.arrival",
    OverlayRecordRole.EXECUTION_FILL: "execution.fill",
}
_ROLE_SOURCE_KIND = {
    OverlayRecordRole.TOP_OF_BOOK: EvidenceSourceKind.CLIENT_DELIVERED,
    OverlayRecordRole.TRADE: EvidenceSourceKind.CLIENT_DELIVERED,
    OverlayRecordRole.CANCELLATION: EvidenceSourceKind.CLIENT_DELIVERED,
    OverlayRecordRole.REPLENISHMENT: EvidenceSourceKind.CLIENT_DELIVERED,
    OverlayRecordRole.RELATIVE_VOLUME_BASELINE: (
        EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT
    ),
    OverlayRecordRole.EXECUTION_ARRIVAL: (
        EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT
    ),
    OverlayRecordRole.EXECUTION_FILL: EvidenceSourceKind.CLIENT_DELIVERED,
}


def overlay_series_id(role: OverlayRecordRole, identity: str) -> str:
    """Return the exact V1 series family for a governed overlay source record."""

    if type(role) is not OverlayRecordRole:
        raise TypeError("overlay record role is invalid")
    _require_identifier(identity, "overlay record identity")
    return f"{_ROLE_SERIES_ROOT[role]}.{identity}"


@dataclass(frozen=True, slots=True, repr=False)
class _ProjectedOverlayEvent:
    queried_value: QueriedValue = field(repr=False)
    query_ids: tuple[str, ...]
    record_role: OverlayRecordRole | None
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _PROJECTION_CONSTRUCTION_TOKEN:
            raise TypeError("projected overlay events require the projection factory")
        if type(self.queried_value) is not QueriedValue:
            raise TypeError("projected overlay event requires one queried value")
        if type(self.query_ids) is not tuple or not self.query_ids:
            raise TypeError("projected overlay event query IDs must be a nonempty tuple")
        query_ids = tuple(sorted(self.query_ids))
        if len(query_ids) != len(set(query_ids)) or any(
            type(item) is not str or _QUERY_ID.fullmatch(item) is None
            for item in query_ids
        ):
            raise ValueError("projected overlay event query IDs are invalid")
        object.__setattr__(self, "query_ids", query_ids)
        if self.record_role is not None and type(self.record_role) is not OverlayRecordRole:
            raise TypeError("projected overlay event record role is invalid")

    @property
    def event_id(self) -> str:
        return self.queried_value.event_id

    @property
    def series_id(self) -> str:
        return self.queried_value.series_id

    @property
    def sequence(self) -> int:
        return self.queried_value.sequence

    @property
    def data_age(self) -> DataAge:
        return self.queried_value.data_age

    def _receipt_dict(self) -> dict[str, object]:
        value = self.queried_value
        return {
            "event_id": value.event_id,
            "payload_sha256": value.payload_sha256,
            "policy_visible_at_time_us": value.data_age.policy_visible_at_time_us,
            "query_ids": list(self.query_ids),
            "record_role": (
                None if self.record_role is None else self.record_role.value
            ),
            "sequence": value.sequence,
            "series_id": value.series_id,
            "source_event_time_us": value.data_age.source_event_time_us,
            "source_evidence_sha256": value.source_evidence_sha256,
            "source_kind": value.source_kind.value,
        }


class OverlayWindowProjection:
    """Closed facade over exact-recorded events from policy-enforced queries.

    Repeated series are retained as distinct event IDs across query snapshots.  The
    public surface exposes only source/policy/current-query identity; counts, bounds,
    query inventory, and unused events remain in the backend receipt.
    """

    __slots__ = (
        "__events_by_id",
        "__observation_mode",
        "__policy_id",
        "__projection_id",
        "__render_cursor_time_us",
        "__sealed",
        "__source_event_sha256",
        "__source_run_id",
        "__terminal_query_id",
    )

    def __init__(
        self,
        source_run_id: str,
        source_event_sha256: str,
        observation_mode: ObservationMode,
        policy_id: str,
        terminal_query_id: str,
        render_cursor_time_us: int,
        events_by_id: Mapping[str, _ProjectedOverlayEvent],
        query_inventory_sha256: str,
        event_inventory_sha256: str,
        *,
        _construction_token: object | None = None,
    ) -> None:
        if _construction_token is not _PROJECTION_CONSTRUCTION_TOKEN:
            raise TypeError(
                "OverlayWindowProjection must be built by "
                "build_overlay_window_projection"
            )
        _require_source_identity(source_run_id, source_event_sha256)
        if type(observation_mode) is not ObservationMode:
            raise TypeError("overlay window projection mode is invalid")
        if policy_id != ObservationPolicy(observation_mode).policy_id:
            raise ValueError("overlay window projection policy differs from its mode")
        if type(terminal_query_id) is not str or _QUERY_ID.fullmatch(terminal_query_id) is None:
            raise ValueError("overlay window projection terminal query ID is invalid")
        if type(render_cursor_time_us) is not int or render_cursor_time_us < 0:
            raise ValueError("overlay window projection cursor is invalid")
        _require_sha256(
            query_inventory_sha256,
            "overlay window projection query inventory digest",
        )
        _require_sha256(
            event_inventory_sha256,
            "overlay window projection event inventory digest",
        )
        if not isinstance(events_by_id, Mapping) or any(
            type(key) is not str
            or type(value) is not _ProjectedOverlayEvent
            or key != value.event_id
            or value.data_age.policy_visible_at_time_us > render_cursor_time_us
            for key, value in events_by_id.items()
        ):
            raise ValueError("overlay window projection event inventory is invalid")
        projection_id = _projection_id_from_root(
            source_run_id,
            source_event_sha256,
            observation_mode,
            policy_id,
            terminal_query_id,
            render_cursor_time_us,
            query_inventory_sha256,
            event_inventory_sha256,
        )
        object.__setattr__(self, "_OverlayWindowProjection__sealed", False)
        object.__setattr__(self, "_OverlayWindowProjection__source_run_id", source_run_id)
        object.__setattr__(
            self,
            "_OverlayWindowProjection__source_event_sha256",
            source_event_sha256,
        )
        object.__setattr__(
            self,
            "_OverlayWindowProjection__observation_mode",
            observation_mode,
        )
        object.__setattr__(self, "_OverlayWindowProjection__policy_id", policy_id)
        object.__setattr__(
            self,
            "_OverlayWindowProjection__terminal_query_id",
            terminal_query_id,
        )
        object.__setattr__(
            self,
            "_OverlayWindowProjection__render_cursor_time_us",
            render_cursor_time_us,
        )
        object.__setattr__(
            self,
            "_OverlayWindowProjection__events_by_id",
            MappingProxyType(dict(sorted(events_by_id.items()))),
        )
        object.__setattr__(
            self,
            "_OverlayWindowProjection__projection_id",
            projection_id,
        )
        object.__setattr__(self, "_OverlayWindowProjection__sealed", True)

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("OverlayWindowProjection is closed to subclassing")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_OverlayWindowProjection__sealed", False):
            raise AttributeError("OverlayWindowProjection is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return (
            "OverlayWindowProjection("
            f"source_run_id={self.source_run_id!r}, "
            f"terminal_query_id={self.terminal_query_id!r})"
        )

    def __reduce__(self) -> object:
        raise TypeError("OverlayWindowProjection is not serializable")

    @property
    def source_run_id(self) -> str:
        return self.__source_run_id

    @property
    def source_event_sha256(self) -> str:
        return self.__source_event_sha256

    @property
    def observation_mode(self) -> ObservationMode:
        return self.__observation_mode

    @property
    def policy_id(self) -> str:
        return self.__policy_id

    @property
    def terminal_query_id(self) -> str:
        return self.__terminal_query_id

    @property
    def render_cursor_time_us(self) -> int:
        return self.__render_cursor_time_us

    @property
    def projection_id(self) -> str:
        return self.__projection_id

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "projection_id": self.projection_id,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": OVERLAY_WINDOW_PROJECTION_SCHEMA_ID,
            "schema_version": OVERLAY_WINDOW_PROJECTION_SCHEMA_VERSION,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "terminal_query_id": self.terminal_query_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    def _resolve_for_build(
        self,
        event_ids: tuple[str, ...],
        *,
        _construction_token: object | None = None,
    ) -> tuple[_ProjectedOverlayEvent, ...]:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("overlay projection inventory is backend-only")
        missing = tuple(
            event_id for event_id in event_ids if event_id not in self.__events_by_id
        )
        if missing:
            raise ValueError(
                "overlay selection is not present in the policy-bound window "
                "projection: " + ",".join(missing)
            )
        return tuple(
            sorted(
                (self.__events_by_id[event_id] for event_id in event_ids),
                key=_projected_event_order,
            )
        )


@dataclass(frozen=True, slots=True, repr=False)
class OverlayWindowProjectionReceipt:
    """Backend-only commitment to query and repeated-event window inventory."""

    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    projection_id: str
    terminal_query_id: str
    render_cursor_time_us: int
    query_inventory_sha256: str
    event_inventory_sha256: str
    query_count: int
    event_count: int
    minimum_policy_visible_time_us: int | None
    maximum_policy_visible_time_us: int | None
    _construction_token: InitVar[object]
    schema_id: str = OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_ID
    schema_version: int = OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _PROJECTION_CONSTRUCTION_TOKEN:
            raise TypeError("overlay window projection receipts require the factory")
        _require_source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("overlay window receipt mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("overlay window receipt policy differs from its mode")
        _require_identifier(self.projection_id, "overlay window projection ID")
        if (
            type(self.terminal_query_id) is not str
            or _QUERY_ID.fullmatch(self.terminal_query_id) is None
        ):
            raise ValueError("overlay window receipt terminal query ID is invalid")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("overlay window receipt cursor is invalid")
        _require_sha256(self.query_inventory_sha256, "overlay query inventory digest")
        _require_sha256(self.event_inventory_sha256, "overlay event inventory digest")
        if (
            type(self.query_count) is not int
            or type(self.event_count) is not int
            or self.query_count <= 0
            or self.event_count < 0
        ):
            raise ValueError("overlay window receipt inventory counts are invalid")
        if self.event_count == 0:
            if (
                self.minimum_policy_visible_time_us is not None
                or self.maximum_policy_visible_time_us is not None
            ):
                raise ValueError("empty overlay window receipt carries event bounds")
        else:
            if (
                type(self.minimum_policy_visible_time_us) is not int
                or type(self.maximum_policy_visible_time_us) is not int
                or self.minimum_policy_visible_time_us < 0
                or self.maximum_policy_visible_time_us
                < self.minimum_policy_visible_time_us
                or self.maximum_policy_visible_time_us > self.render_cursor_time_us
            ):
                raise ValueError("overlay window receipt event bounds are invalid")
        if (
            self.schema_id != OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version
            != OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported overlay window projection receipt schema")
        object.__setattr__(
            self,
            "receipt_sha256",
            _canonical_sha256(self._identity_dict()),
        )

    def __repr__(self) -> str:
        return (
            "OverlayWindowProjectionReceipt(backend_only=True, "
            f"source_run_id={self.source_run_id!r}, "
            f"receipt_sha256={self.receipt_sha256!r})"
        )

    def _identity_dict(self) -> dict[str, object]:
        return {
            "event_count": self.event_count,
            "event_inventory_sha256": self.event_inventory_sha256,
            "maximum_policy_visible_time_us": self.maximum_policy_visible_time_us,
            "minimum_policy_visible_time_us": self.minimum_policy_visible_time_us,
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "projection_id": self.projection_id,
            "query_count": self.query_count,
            "query_inventory_sha256": self.query_inventory_sha256,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "terminal_query_id": self.terminal_query_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_dict(), "receipt_sha256": self.receipt_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def build_overlay_window_projection(
    terminal_query: ObservationQueryResult,
    event_queries: tuple[ObservationQueryResult, ...] = (),
) -> tuple[OverlayWindowProjection, OverlayWindowProjectionReceipt]:
    """Commit exact-recorded events from same-source queries through one cursor."""

    if type(terminal_query) is not ObservationQueryResult:
        raise TypeError("overlay terminal query must be ObservationQueryResult")
    if type(event_queries) is not tuple or any(
        type(item) is not ObservationQueryResult for item in event_queries
    ):
        raise TypeError("overlay event queries must be an exact query-result tuple")
    candidates = (terminal_query, *event_queries)
    by_query_id: dict[str, ObservationQueryResult] = {}
    for query in candidates:
        if (
            query.source_run_id != terminal_query.source_run_id
            or query.source_event_sha256 != terminal_query.source_event_sha256
            or query.policy.mode is not terminal_query.policy.mode
            or query.policy.policy_id != terminal_query.policy.policy_id
        ):
            raise ValueError("overlay event query belongs to another source or policy")
        if query.request.render_cursor_time_us > terminal_query.request.render_cursor_time_us:
            raise ValueError("overlay event query is beyond the terminal cursor")
        prior = by_query_id.setdefault(query.query_id, query)
        if prior.canonical_bytes() != query.canonical_bytes():
            raise ValueError("one overlay query ID maps to different canonical bytes")
    queries = tuple(
        sorted(
            by_query_id.values(),
            key=lambda item: (item.request.render_cursor_time_us, item.query_id),
        )
    )
    events: dict[str, tuple[QueriedValue, set[str]]] = {}
    for query in queries:
        for value in query.values:
            if (
                value.selection is not SelectionKind.EXACT_RECORDED
                or value.data_age.policy_visible_at_time_us
                != query.request.render_cursor_time_us
            ):
                continue
            prior = events.get(value.event_id)
            if prior is None:
                events[value.event_id] = (value, {query.query_id})
                continue
            prior_value, query_ids = prior
            if _canonical_json_bytes(prior_value.as_dict()) != _canonical_json_bytes(
                value.as_dict()
            ):
                raise ValueError("one overlay event ID maps to different queried values")
            query_ids.add(query.query_id)
    projected = {
        event_id: _ProjectedOverlayEvent(
            queried_value=value,
            query_ids=tuple(query_ids),
            record_role=_classify_record_role(value),
            _construction_token=_PROJECTION_CONSTRUCTION_TOKEN,
        )
        for event_id, (value, query_ids) in events.items()
    }
    event_rows = [
        projected[event_id]._receipt_dict() for event_id in sorted(projected)
    ]
    query_rows = [
        {
            "canonical_sha256": hashlib.sha256(query.canonical_bytes()).hexdigest(),
            "query_id": query.query_id,
            "render_cursor_time_us": query.request.render_cursor_time_us,
        }
        for query in queries
    ]
    query_inventory_sha256 = _canonical_sha256(query_rows)
    event_inventory_sha256 = _canonical_sha256(event_rows)
    projection = OverlayWindowProjection(
        terminal_query.source_run_id,
        terminal_query.source_event_sha256,
        terminal_query.policy.mode,
        terminal_query.policy.policy_id,
        terminal_query.query_id,
        terminal_query.request.render_cursor_time_us,
        projected,
        query_inventory_sha256,
        event_inventory_sha256,
        _construction_token=_PROJECTION_CONSTRUCTION_TOKEN,
    )
    times = tuple(
        event.data_age.policy_visible_at_time_us for event in projected.values()
    )
    receipt = OverlayWindowProjectionReceipt(
        source_run_id=terminal_query.source_run_id,
        source_event_sha256=terminal_query.source_event_sha256,
        observation_mode=terminal_query.policy.mode,
        policy_id=terminal_query.policy.policy_id,
        projection_id=projection.projection_id,
        terminal_query_id=terminal_query.query_id,
        render_cursor_time_us=terminal_query.request.render_cursor_time_us,
        query_inventory_sha256=query_inventory_sha256,
        event_inventory_sha256=event_inventory_sha256,
        query_count=len(queries),
        event_count=len(projected),
        minimum_policy_visible_time_us=None if not times else min(times),
        maximum_policy_visible_time_us=None if not times else max(times),
        _construction_token=_PROJECTION_CONSTRUCTION_TOKEN,
    )
    return projection, receipt


@dataclass(frozen=True, slots=True)
class OverlaySourceEvent:
    """Payload-free reference copied from one policy-visible queried value."""

    event_id: str
    series_id: str
    sequence: int
    source_kind: EvidenceSourceKind
    source_event_time_us: int
    policy_visible_at_time_us: int
    payload_sha256: str
    source_evidence_sha256: str
    query_ids: tuple[str, ...]
    record_role: OverlayRecordRole | None
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("overlay source events are produced only from query values")
        _require_identifier(self.event_id, "overlay source event ID")
        _require_identifier(self.series_id, "overlay source series ID")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("overlay source sequence must be a nonnegative integer")
        if type(self.source_kind) is not EvidenceSourceKind:
            raise TypeError("overlay source kind is invalid")
        if (
            type(self.source_event_time_us) is not int
            or type(self.policy_visible_at_time_us) is not int
            or self.source_event_time_us < 0
            or self.policy_visible_at_time_us < self.source_event_time_us
        ):
            raise ValueError("overlay source timing is invalid")
        _require_sha256(self.payload_sha256, "overlay source payload digest")
        _require_sha256(
            self.source_evidence_sha256,
            "overlay source evidence digest",
        )
        if type(self.query_ids) is not tuple or not self.query_ids:
            raise TypeError("overlay source query IDs must be a nonempty tuple")
        query_ids = tuple(sorted(self.query_ids))
        if len(query_ids) != len(set(query_ids)) or any(
            type(item) is not str or _QUERY_ID.fullmatch(item) is None
            for item in query_ids
        ):
            raise ValueError("overlay source query IDs are invalid")
        object.__setattr__(self, "query_ids", query_ids)
        if self.record_role is not None and type(self.record_role) is not OverlayRecordRole:
            raise TypeError("overlay source record role is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "payload_sha256": self.payload_sha256,
            "policy_visible_at_time_us": self.policy_visible_at_time_us,
            "query_ids": list(self.query_ids),
            "record_role": (
                None if self.record_role is None else self.record_role.value
            ),
            "sequence": self.sequence,
            "series_id": self.series_id,
            "source_event_time_us": self.source_event_time_us,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_kind": self.source_kind.value,
        }


@dataclass(frozen=True, slots=True)
class OverlayInputSelection:
    """Semantic uses for event IDs retained by one window projection.

    The selection contains no calculation values.  All selected IDs are resolved
    against the supplied policy-bound projection by :func:`build_overlay_set`; an
    absent ID is a hard boundary violation, while an observed payload lacking a
    required semantic field produces a typed unavailable overlay.
    """

    top_of_book_event_ids: tuple[str, ...] = ()
    trade_event_ids: tuple[str, ...] = ()
    cancellation_event_ids: tuple[str, ...] = ()
    replenishment_event_ids: tuple[str, ...] = ()
    relative_volume_baseline_event_id: str | None = None
    execution_arrival_event_id: str | None = None
    execution_fill_event_ids: tuple[str, ...] = ()
    selection_id: str = field(init=False)

    def __post_init__(self) -> None:
        tuple_fields = (
            "top_of_book_event_ids",
            "trade_event_ids",
            "cancellation_event_ids",
            "replenishment_event_ids",
            "execution_fill_event_ids",
        )
        all_ids: list[str] = []
        for field_name in tuple_fields:
            values = getattr(self, field_name)
            if type(values) is not tuple:
                raise TypeError(f"{field_name} must be an immutable tuple")
            if any(
                type(value) is not str or _IDENTIFIER.fullmatch(value) is None
                for value in values
            ):
                raise ValueError(f"{field_name} contains an invalid event ID")
            canonical = tuple(sorted(values))
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{field_name} contains duplicate event IDs")
            object.__setattr__(self, field_name, canonical)
            all_ids.extend(canonical)
        for field_name in (
            "relative_volume_baseline_event_id",
            "execution_arrival_event_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                type(value) is not str or _IDENTIFIER.fullmatch(value) is None
            ):
                raise ValueError(f"{field_name} is invalid")
            if value is not None:
                all_ids.append(value)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("one event ID cannot be assigned multiple overlay roles")
        object.__setattr__(
            self,
            "selection_id",
            "overlay-selection-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "cancellation_event_ids": list(self.cancellation_event_ids),
            "execution_arrival_event_id": self.execution_arrival_event_id,
            "execution_fill_event_ids": list(self.execution_fill_event_ids),
            "relative_volume_baseline_event_id": (
                self.relative_volume_baseline_event_id
            ),
            "replenishment_event_ids": list(self.replenishment_event_ids),
            "top_of_book_event_ids": list(self.top_of_book_event_ids),
            "trade_event_ids": list(self.trade_event_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "selection_id": self.selection_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class _OverlayResult:
    source_run_id: str
    source_event_sha256: str
    query_id: str
    window_projection_id: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    window: OverlayWindow
    availability: OverlayAvailability
    value: int | None
    source_events: tuple[OverlaySourceEvent, ...]
    unavailable_reason: OverlayUnavailableReason | None
    _construction_token: InitVar[object]
    overlay_id: str = field(init=False)

    SPECIFICATION: ClassVar[OverlaySpecification]
    VALUE_MINIMUM: ClassVar[int | None] = None
    VALUE_MAXIMUM: ClassVar[int | None] = None

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("overlay results are produced only by build_overlay_set")
        _require_source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.query_id) is not str or _QUERY_ID.fullmatch(self.query_id) is None:
            raise ValueError("overlay query ID is invalid")
        if (
            type(self.window_projection_id) is not str
            or not self.window_projection_id.startswith("overlay-window-projection-")
            or len(self.window_projection_id)
            != len("overlay-window-projection-") + 24
        ):
            raise ValueError("overlay window projection ID is invalid")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("overlay observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("overlay policy ID differs from its observation mode")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("overlay cursor must be nonnegative integer microseconds")
        if type(self.window) is not OverlayWindow:
            raise TypeError("overlay window is invalid")
        if self.window.window_projection_id != self.window_projection_id:
            raise ValueError("overlay window belongs to another projection")
        _validate_window_for_specification(
            self.window,
            self.render_cursor_time_us,
            self.SPECIFICATION,
        )
        if type(self.availability) is not OverlayAvailability:
            raise TypeError("overlay availability is invalid")
        if type(self.source_events) is not tuple or any(
            type(item) is not OverlaySourceEvent for item in self.source_events
        ):
            raise TypeError("overlay source events must be an immutable reference tuple")
        events = tuple(sorted(self.source_events, key=_source_event_order))
        if len(events) != len({item.event_id for item in events}):
            raise ValueError("overlay source events contain duplicate identities")
        if any(item.policy_visible_at_time_us > self.render_cursor_time_us for item in events):
            raise ValueError("overlay source events contain future policy-visible data")
        object.__setattr__(self, "source_events", events)
        if self.availability is OverlayAvailability.AVAILABLE:
            if type(self.value) is not int:
                raise TypeError("available overlay value must be an exact integer")
            if not events:
                raise ValueError("available overlay requires source-event provenance")
            if self.unavailable_reason is not None:
                raise ValueError("available overlay cannot carry an unavailability reason")
            if self.VALUE_MINIMUM is not None and self.value < self.VALUE_MINIMUM:
                raise ValueError("overlay value is below its declared range")
            if self.VALUE_MAXIMUM is not None and self.value > self.VALUE_MAXIMUM:
                raise ValueError("overlay value is above its declared range")
        else:
            if self.value is not None:
                raise ValueError("unavailable overlay cannot carry a numeric value")
            if type(self.unavailable_reason) is not OverlayUnavailableReason:
                raise ValueError("unavailable overlay requires a typed reason")
        object.__setattr__(
            self,
            "overlay_id",
            self.SPECIFICATION.kind.value.lower().replace("_", "-")
            + "-overlay-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    @property
    def kind(self) -> OverlayKind:
        return self.SPECIFICATION.kind

    @property
    def unit(self) -> OverlayUnit:
        return self.SPECIFICATION.unit

    @property
    def calculation(self) -> DerivedCalculationContract:
        return self.SPECIFICATION.calculation

    @property
    def schema_id(self) -> str:
        return self.SPECIFICATION.schema_id

    @property
    def schema_version(self) -> int:
        return self.SPECIFICATION.schema_version

    def identity_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "calculation": self.calculation.as_dict(),
            "kind": self.kind.value,
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_events": [item.as_dict() for item in self.source_events],
            "source_run_id": self.source_run_id,
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
            "unit": self.unit.value,
            "value": self.value,
            "window": self.window.as_dict(),
            "window_projection_id": self.window_projection_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "overlay_id": self.overlay_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class SpreadOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = SPREAD_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def spread_ticks(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class MicropriceOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = MICROPRICE_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def microprice_microticks(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class ImbalanceOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = IMBALANCE_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = -RATIO_SCALE_PPM
    VALUE_MAXIMUM: ClassVar[int | None] = RATIO_SCALE_PPM

    @property
    def imbalance_ppm(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class TradeVelocityOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = TRADE_VELOCITY_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def microtrades_per_second(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class CancellationVelocityOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = (
        CANCELLATION_VELOCITY_SPECIFICATION
    )
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def microshares_per_second(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class ReplenishmentOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = REPLENISHMENT_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def microshares_per_second(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class RelativeVolumeOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = RELATIVE_VOLUME_SPECIFICATION
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def relative_volume_ppm(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class ShortTermVolatilityOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = (
        SHORT_TERM_VOLATILITY_SPECIFICATION
    )
    VALUE_MINIMUM: ClassVar[int | None] = 0

    @property
    def volatility_microbasis_points(self) -> int | None:
        return self.value


@dataclass(frozen=True, slots=True)
class ImplementationShortfallOverlay(_OverlayResult):
    SPECIFICATION: ClassVar[OverlaySpecification] = (
        IMPLEMENTATION_SHORTFALL_SPECIFICATION
    )

    @property
    def implementation_shortfall_x2_tick_shares(self) -> int | None:
        return self.value


OverlayResult: TypeAlias = (
    SpreadOverlay
    | MicropriceOverlay
    | ImbalanceOverlay
    | TradeVelocityOverlay
    | CancellationVelocityOverlay
    | ReplenishmentOverlay
    | RelativeVolumeOverlay
    | ShortTermVolatilityOverlay
    | ImplementationShortfallOverlay
)

_OVERLAY_TYPE_ORDER = (
    SpreadOverlay,
    MicropriceOverlay,
    ImbalanceOverlay,
    TradeVelocityOverlay,
    CancellationVelocityOverlay,
    ReplenishmentOverlay,
    RelativeVolumeOverlay,
    ShortTermVolatilityOverlay,
    ImplementationShortfallOverlay,
)


@dataclass(frozen=True, slots=True)
class OverlaySet:
    """The exact nine-overlay projection for one query and integer cursor."""

    source_run_id: str
    source_event_sha256: str
    query_id: str
    window_projection_id: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    overlays: tuple[OverlayResult, ...]
    input_selection_id: str
    _construction_token: InitVar[object]
    schema_id: str = OVERLAY_SET_SCHEMA_ID
    schema_version: int = OVERLAY_SET_SCHEMA_VERSION
    overlay_set_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise TypeError("overlay sets are produced only by build_overlay_set")
        _require_source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.query_id) is not str or _QUERY_ID.fullmatch(self.query_id) is None:
            raise ValueError("overlay-set query ID is invalid")
        if (
            type(self.window_projection_id) is not str
            or not self.window_projection_id.startswith("overlay-window-projection-")
            or len(self.window_projection_id)
            != len("overlay-window-projection-") + 24
        ):
            raise ValueError("overlay-set window projection ID is invalid")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("overlay-set observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("overlay-set policy ID differs from its mode")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("overlay-set cursor must be nonnegative microseconds")
        if type(self.overlays) is not tuple or tuple(map(type, self.overlays)) != (
            _OVERLAY_TYPE_ORDER
        ):
            raise ValueError("overlay set must contain the exact ordered nine-item inventory")
        if any(
            item.source_run_id != self.source_run_id
            or item.source_event_sha256 != self.source_event_sha256
            or item.query_id != self.query_id
            or item.window_projection_id != self.window_projection_id
            or item.observation_mode is not self.observation_mode
            or item.policy_id != self.policy_id
            or item.render_cursor_time_us != self.render_cursor_time_us
            for item in self.overlays
        ):
            raise ValueError("overlay roots differ from their overlay set")
        if (
            type(self.input_selection_id) is not str
            or not self.input_selection_id.startswith("overlay-selection-")
            or len(self.input_selection_id) != len("overlay-selection-") + 24
        ):
            raise ValueError("overlay input selection ID is invalid")
        if (
            self.schema_id != OVERLAY_SET_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OVERLAY_SET_SCHEMA_VERSION
        ):
            raise ValueError("unsupported overlay-set schema")
        object.__setattr__(
            self,
            "overlay_set_id",
            "overlay-set-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def overlay(self, kind: OverlayKind) -> OverlayResult:
        if type(kind) is not OverlayKind:
            raise TypeError("overlay lookup kind is invalid")
        return self.overlays[OVERLAY_KIND_ORDER.index(kind)]

    @property
    def spread(self) -> SpreadOverlay:
        return self.overlays[0]  # type: ignore[return-value]

    @property
    def microprice(self) -> MicropriceOverlay:
        return self.overlays[1]  # type: ignore[return-value]

    @property
    def imbalance(self) -> ImbalanceOverlay:
        return self.overlays[2]  # type: ignore[return-value]

    @property
    def trade_velocity(self) -> TradeVelocityOverlay:
        return self.overlays[3]  # type: ignore[return-value]

    @property
    def cancellation_velocity(self) -> CancellationVelocityOverlay:
        return self.overlays[4]  # type: ignore[return-value]

    @property
    def replenishment(self) -> ReplenishmentOverlay:
        return self.overlays[5]  # type: ignore[return-value]

    @property
    def relative_volume(self) -> RelativeVolumeOverlay:
        return self.overlays[6]  # type: ignore[return-value]

    @property
    def short_term_volatility(self) -> ShortTermVolatilityOverlay:
        return self.overlays[7]  # type: ignore[return-value]

    @property
    def implementation_shortfall(self) -> ImplementationShortfallOverlay:
        return self.overlays[8]  # type: ignore[return-value]

    def identity_dict(self) -> dict[str, object]:
        return {
            "input_selection_id": self.input_selection_id,
            "observation_mode": self.observation_mode.value,
            "overlays": [item.as_dict() for item in self.overlays],
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "window_projection_id": self.window_projection_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "overlay_set_id": self.overlay_set_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def build_overlay_set(
    query: ObservationQueryResult,
    window_projection: OverlayWindowProjection,
    selection: OverlayInputSelection,
) -> OverlaySet:
    """Build all nine V1 overlays from an exact query-bound window projection."""

    if type(query) is not ObservationQueryResult:
        raise TypeError("overlay construction requires ObservationQueryResult")
    if type(window_projection) is not OverlayWindowProjection:
        raise TypeError("overlay construction requires OverlayWindowProjection")
    if type(selection) is not OverlayInputSelection:
        raise TypeError("overlay construction requires OverlayInputSelection")
    if (
        window_projection.source_run_id != query.source_run_id
        or window_projection.source_event_sha256 != query.source_event_sha256
        or window_projection.observation_mode is not query.policy.mode
        or window_projection.policy_id != query.policy.policy_id
        or window_projection.terminal_query_id != query.query_id
        or window_projection.render_cursor_time_us
        != query.request.render_cursor_time_us
    ):
        raise ValueError("overlay window projection differs from the terminal query")

    def resolve_many(event_ids: tuple[str, ...]) -> tuple[_ProjectedOverlayEvent, ...]:
        return window_projection._resolve_for_build(
            event_ids,
            _construction_token=_CONSTRUCTION_TOKEN,
        )

    def resolve_one(event_id: str | None) -> _ProjectedOverlayEvent | None:
        if event_id is None:
            return None
        return resolve_many((event_id,))[0]

    top = resolve_many(selection.top_of_book_event_ids)
    trades = resolve_many(selection.trade_event_ids)
    cancellations = resolve_many(selection.cancellation_event_ids)
    replenishments = resolve_many(selection.replenishment_event_ids)
    baseline = resolve_one(selection.relative_volume_baseline_event_id)
    arrival = resolve_one(selection.execution_arrival_event_id)
    fills = resolve_many(selection.execution_fill_event_ids)

    overlays: tuple[OverlayResult, ...] = (
        _build_spread(query, window_projection.projection_id, top),
        _build_microprice(query, window_projection.projection_id, top),
        _build_imbalance(query, window_projection.projection_id, top),
        _build_trade_velocity(query, window_projection.projection_id, trades),
        _build_cancellation_velocity(
            query,
            window_projection.projection_id,
            cancellations,
        ),
        _build_replenishment(
            query,
            window_projection.projection_id,
            replenishments,
        ),
        _build_relative_volume(
            query,
            window_projection.projection_id,
            trades,
            baseline,
        ),
        _build_short_term_volatility(
            query,
            window_projection.projection_id,
            top,
        ),
        _build_implementation_shortfall(
            query,
            window_projection.projection_id,
            arrival,
            fills,
        ),
    )
    return OverlaySet(
        source_run_id=query.source_run_id,
        source_event_sha256=query.source_event_sha256,
        query_id=query.query_id,
        window_projection_id=window_projection.projection_id,
        observation_mode=query.policy.mode,
        policy_id=query.policy.policy_id,
        render_cursor_time_us=query.request.render_cursor_time_us,
        overlays=overlays,
        input_selection_id=selection.selection_id,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _build_spread(
    query: ObservationQueryResult,
    window_projection_id: str,
    top: tuple[_ProjectedOverlayEvent, ...],
) -> SpreadOverlay:
    window = _window(query, SPREAD_SPECIFICATION, window_projection_id)
    latest = _latest(top)
    if latest is None:
        return _unavailable(
            SpreadOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    sources = (_source_reference(latest),)
    if latest.record_role is not OverlayRecordRole.TOP_OF_BOOK:
        return _unavailable(
            SpreadOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    payload = _payload_mapping(latest)
    if payload is None:
        return _unavailable(
            SpreadOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
        )
    bid = _payload_exact_int(payload, "best_bid_ticks", minimum=0)
    ask = _payload_exact_int(payload, "best_ask_ticks", minimum=0)
    if bid is None or ask is None:
        return _unavailable(
            SpreadOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    if ask < bid:
        return _unavailable(
            SpreadOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    return _available(SpreadOverlay, query, window, ask - bid, sources)


def _build_microprice(
    query: ObservationQueryResult,
    window_projection_id: str,
    top: tuple[_ProjectedOverlayEvent, ...],
) -> MicropriceOverlay:
    window = _window(query, MICROPRICE_SPECIFICATION, window_projection_id)
    latest = _latest(top)
    if latest is None:
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    sources = (_source_reference(latest),)
    if latest.record_role is not OverlayRecordRole.TOP_OF_BOOK:
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    payload = _payload_mapping(latest)
    if payload is None:
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
        )
    bid = _payload_exact_int(payload, "best_bid_ticks", minimum=0)
    ask = _payload_exact_int(payload, "best_ask_ticks", minimum=0)
    bid_size = _payload_exact_int(payload, "best_bid_size", minimum=0)
    ask_size = _payload_exact_int(payload, "best_ask_size", minimum=0)
    if None in (bid, ask, bid_size, ask_size):
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    assert bid is not None and ask is not None
    assert bid_size is not None and ask_size is not None
    if ask < bid:
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    denominator = bid_size + ask_size
    if denominator == 0:
        return _unavailable(
            MicropriceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.ZERO_DENOMINATOR,
        )
    numerator = (ask * bid_size + bid * ask_size) * MICRO_UNITS_PER_UNIT
    return _available(
        MicropriceOverlay,
        query,
        window,
        _round_div_even(numerator, denominator),
        sources,
    )


def _build_imbalance(
    query: ObservationQueryResult,
    window_projection_id: str,
    top: tuple[_ProjectedOverlayEvent, ...],
) -> ImbalanceOverlay:
    window = _window(query, IMBALANCE_SPECIFICATION, window_projection_id)
    latest = _latest(top)
    if latest is None:
        return _unavailable(
            ImbalanceOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    sources = (_source_reference(latest),)
    if latest.record_role is not OverlayRecordRole.TOP_OF_BOOK:
        return _unavailable(
            ImbalanceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    payload = _payload_mapping(latest)
    if payload is None:
        return _unavailable(
            ImbalanceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
        )
    bid_size = _payload_exact_int(payload, "best_bid_size", minimum=0)
    ask_size = _payload_exact_int(payload, "best_ask_size", minimum=0)
    if bid_size is None or ask_size is None:
        return _unavailable(
            ImbalanceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    denominator = bid_size + ask_size
    if denominator == 0:
        return _unavailable(
            ImbalanceOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.ZERO_DENOMINATOR,
        )
    return _available(
        ImbalanceOverlay,
        query,
        window,
        _round_div_even((bid_size - ask_size) * RATIO_SCALE_PPM, denominator),
        sources,
    )


def _build_trade_velocity(
    query: ObservationQueryResult,
    window_projection_id: str,
    trades: tuple[_ProjectedOverlayEvent, ...],
) -> TradeVelocityOverlay:
    window = _window(query, TRADE_VELOCITY_SPECIFICATION, window_projection_id)
    if not trades:
        return _unavailable(
            TradeVelocityOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    selected = _within_window(trades, window)
    if not selected:
        return _unavailable(
            TradeVelocityOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.NO_EVENTS_IN_WINDOW,
        )
    sources = _source_references(selected)
    if not _roles_match(selected, OverlayRecordRole.TRADE):
        return _unavailable(
            TradeVelocityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    if window.duration_us == 0:
        return _unavailable(
            TradeVelocityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.ZERO_WINDOW_DURATION,
        )
    value = _round_div_even(
        len(selected) * MICRO_UNITS_PER_UNIT * MICROSECONDS_PER_SECOND,
        window.duration_us,
    )
    return _available(TradeVelocityOverlay, query, window, value, sources)


def _build_cancellation_velocity(
    query: ObservationQueryResult,
    window_projection_id: str,
    cancellations: tuple[_ProjectedOverlayEvent, ...],
) -> CancellationVelocityOverlay:
    window = _window(
        query,
        CANCELLATION_VELOCITY_SPECIFICATION,
        window_projection_id,
    )
    if not cancellations:
        return _unavailable(
            CancellationVelocityOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    selected = _within_window(cancellations, window)
    if not selected:
        return _unavailable(
            CancellationVelocityOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.NO_EVENTS_IN_WINDOW,
        )
    sources = _source_references(selected)
    if not _roles_match(selected, OverlayRecordRole.CANCELLATION):
        return _unavailable(
            CancellationVelocityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    if window.duration_us == 0:
        return _unavailable(
            CancellationVelocityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.ZERO_WINDOW_DURATION,
        )
    quantities = tuple(
        _payload_field_int(item, "cancelled_quantity", minimum=1)
        for item in selected
    )
    if any(value is None for value in quantities):
        return _unavailable(
            CancellationVelocityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    value = _round_div_even(
        sum(value for value in quantities if value is not None)
        * MICRO_UNITS_PER_UNIT
        * MICROSECONDS_PER_SECOND,
        window.duration_us,
    )
    return _available(CancellationVelocityOverlay, query, window, value, sources)


def _build_replenishment(
    query: ObservationQueryResult,
    window_projection_id: str,
    replenishments: tuple[_ProjectedOverlayEvent, ...],
) -> ReplenishmentOverlay:
    window = _window(query, REPLENISHMENT_SPECIFICATION, window_projection_id)
    if not replenishments:
        return _unavailable(
            ReplenishmentOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    selected = _within_window(replenishments, window)
    if not selected:
        return _unavailable(
            ReplenishmentOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.NO_EVENTS_IN_WINDOW,
        )
    sources = _source_references(selected)
    if not _roles_match(selected, OverlayRecordRole.REPLENISHMENT):
        return _unavailable(
            ReplenishmentOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    if window.duration_us == 0:
        return _unavailable(
            ReplenishmentOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.ZERO_WINDOW_DURATION,
        )
    quantities = tuple(
        _payload_field_int(item, "added_quantity", minimum=1)
        for item in selected
    )
    if any(value is None for value in quantities):
        return _unavailable(
            ReplenishmentOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    value = _round_div_even(
        sum(value for value in quantities if value is not None)
        * MICRO_UNITS_PER_UNIT
        * MICROSECONDS_PER_SECOND,
        window.duration_us,
    )
    return _available(ReplenishmentOverlay, query, window, value, sources)


def _build_relative_volume(
    query: ObservationQueryResult,
    window_projection_id: str,
    trades: tuple[_ProjectedOverlayEvent, ...],
    baseline: _ProjectedOverlayEvent | None,
) -> RelativeVolumeOverlay:
    window = _window(query, RELATIVE_VOLUME_SPECIFICATION, window_projection_id)
    if baseline is None:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.BASELINE_NOT_SELECTED,
        )
    baseline_source = _source_reference(baseline)
    if baseline.record_role is not OverlayRecordRole.RELATIVE_VOLUME_BASELINE:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    baseline_payload = _payload_mapping(baseline)
    if baseline_payload is None:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
        )
    expected_volume = _payload_exact_int(baseline_payload, "expected_volume", minimum=0)
    baseline_window = _payload_exact_int(
        baseline_payload,
        "window_duration_us",
        minimum=0,
    )
    if expected_volume is None or baseline_window is None:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    if baseline_window != window.duration_us:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.BASELINE_WINDOW_MISMATCH,
        )
    if window.duration_us == 0:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.ZERO_WINDOW_DURATION,
        )
    if expected_volume == 0:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.ZERO_DENOMINATOR,
        )
    selected = _within_window(trades, window)
    if not selected:
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            (baseline_source,),
            OverlayUnavailableReason.NO_EVENTS_IN_WINDOW,
        )
    sources = _source_references((*selected, baseline))
    if not _roles_match(selected, OverlayRecordRole.TRADE):
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    quantities = tuple(
        _payload_field_int(item, "quantity", minimum=1) for item in selected
    )
    if any(value is None for value in quantities):
        return _unavailable(
            RelativeVolumeOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    value = _round_div_even(
        sum(value for value in quantities if value is not None) * RATIO_SCALE_PPM,
        expected_volume,
    )
    return _available(RelativeVolumeOverlay, query, window, value, sources)


def _build_short_term_volatility(
    query: ObservationQueryResult,
    window_projection_id: str,
    top: tuple[_ProjectedOverlayEvent, ...],
) -> ShortTermVolatilityOverlay:
    window = _window(
        query,
        SHORT_TERM_VOLATILITY_SPECIFICATION,
        window_projection_id,
    )
    if not top:
        return _unavailable(
            ShortTermVolatilityOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.SOURCE_NOT_SELECTED,
        )
    selected = _within_window(top, window)
    if len(selected) < 2:
        return _unavailable(
            ShortTermVolatilityOverlay,
            query,
            window,
            _source_references(selected),
            OverlayUnavailableReason.INSUFFICIENT_SAMPLES,
        )
    sources = _source_references(selected)
    if not _roles_match(selected, OverlayRecordRole.TOP_OF_BOOK):
        return _unavailable(
            ShortTermVolatilityOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    midpoints: list[int] = []
    for item in selected:
        payload = _payload_mapping(item)
        if payload is None:
            return _unavailable(
                ShortTermVolatilityOverlay,
                query,
                window,
                sources,
                OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
            )
        bid = _payload_exact_int(payload, "best_bid_ticks", minimum=0)
        ask = _payload_exact_int(payload, "best_ask_ticks", minimum=0)
        if bid is None or ask is None or ask < bid or bid + ask <= 0:
            return _unavailable(
                ShortTermVolatilityOverlay,
                query,
                window,
                sources,
                OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
            )
        midpoints.append(bid + ask)
    squared_returns = 0
    for left, right in zip(midpoints, midpoints[1:]):
        return_microbasis_points = _round_div_even(
            (right - left) * MICROBASIS_POINTS_PER_RETURN,
            left,
        )
        squared_returns += return_microbasis_points * return_microbasis_points
    return _available(
        ShortTermVolatilityOverlay,
        query,
        window,
        math.isqrt(squared_returns),
        sources,
    )


def _build_implementation_shortfall(
    query: ObservationQueryResult,
    window_projection_id: str,
    arrival: _ProjectedOverlayEvent | None,
    fills: tuple[_ProjectedOverlayEvent, ...],
) -> ImplementationShortfallOverlay:
    window = _window(
        query,
        IMPLEMENTATION_SHORTFALL_SPECIFICATION,
        window_projection_id,
    )
    if arrival is None:
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (),
            OverlayUnavailableReason.EXECUTION_ARRIVAL_NOT_SELECTED,
        )
    arrival_source = _source_reference(arrival)
    if arrival.record_role is not OverlayRecordRole.EXECUTION_ARRIVAL:
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (arrival_source,),
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    arrival_payload = _payload_mapping(arrival)
    if arrival_payload is None:
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (arrival_source,),
            OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
        )
    arrival_midpoint_x2 = _payload_exact_int(
        arrival_payload,
        "arrival_midpoint_x2",
        minimum=1,
    )
    execution_id = _payload_identifier(arrival_payload, "execution_id")
    order_id = _payload_identifier(arrival_payload, "order_id")
    correlation_id = _payload_identifier(arrival_payload, "correlation_id")
    arrival_side = arrival_payload.get("side")
    if (
        arrival_midpoint_x2 is None
        or execution_id is None
        or order_id is None
        or correlation_id is None
        or type(arrival_side) is not str
        or arrival_side not in {"BUY", "SELL"}
    ):
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (arrival_source,),
            OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
        )
    if arrival.series_id != overlay_series_id(
        OverlayRecordRole.EXECUTION_ARRIVAL,
        execution_id,
    ):
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (arrival_source,),
            OverlayUnavailableReason.EXECUTION_IDENTITY_MISMATCH,
        )
    if not fills:
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            (arrival_source,),
            OverlayUnavailableReason.EXECUTION_FILLS_NOT_SELECTED,
        )
    sources = _source_references((arrival, *fills))
    if not _roles_match(fills, OverlayRecordRole.EXECUTION_FILL):
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH,
        )
    if any(
        item.data_age.policy_visible_at_time_us
        < arrival.data_age.policy_visible_at_time_us
        or item.data_age.source_event_time_us
        < arrival.data_age.source_event_time_us
        for item in fills
    ):
        return _unavailable(
            ImplementationShortfallOverlay,
            query,
            window,
            sources,
            OverlayUnavailableReason.EVENT_CHRONOLOGY_INVALID,
        )
    total = 0
    for fill in fills:
        payload = _payload_mapping(fill)
        if payload is None:
            return _unavailable(
                ImplementationShortfallOverlay,
                query,
                window,
                sources,
                OverlayUnavailableReason.SOURCE_FIELDS_MISSING,
            )
        price_x2 = _payload_exact_int(payload, "price_x2", minimum=0)
        quantity = _payload_exact_int(payload, "quantity", minimum=1)
        side = payload.get("side")
        fill_execution_id = _payload_identifier(payload, "execution_id")
        fill_order_id = _payload_identifier(payload, "order_id")
        fill_correlation_id = _payload_identifier(payload, "correlation_id")
        if (
            price_x2 is None
            or quantity is None
            or type(side) is not str
            or side not in {"BUY", "SELL"}
            or fill_execution_id is None
            or fill_order_id is None
            or fill_correlation_id is None
        ):
            return _unavailable(
                ImplementationShortfallOverlay,
                query,
                window,
                sources,
                OverlayUnavailableReason.SOURCE_FIELDS_INVALID,
            )
        if (
            fill_execution_id != execution_id
            or fill_order_id != order_id
            or fill_correlation_id != correlation_id
            or side != arrival_side
            or fill.series_id
            != overlay_series_id(
                OverlayRecordRole.EXECUTION_FILL,
                fill_execution_id,
            )
        ):
            return _unavailable(
                ImplementationShortfallOverlay,
                query,
                window,
                sources,
                OverlayUnavailableReason.EXECUTION_IDENTITY_MISMATCH,
            )
        sign = 1 if arrival_side == "BUY" else -1
        total += sign * (price_x2 - arrival_midpoint_x2) * quantity
    return _available(ImplementationShortfallOverlay, query, window, total, sources)


def _available(
    overlay_type: type[_OverlayResult],
    query: ObservationQueryResult,
    window: OverlayWindow,
    value: int,
    sources: tuple[OverlaySourceEvent, ...],
) -> _OverlayResult:
    return overlay_type(
        query.source_run_id,
        query.source_event_sha256,
        query.query_id,
        window.window_projection_id,
        query.policy.mode,
        query.policy.policy_id,
        query.request.render_cursor_time_us,
        window,
        OverlayAvailability.AVAILABLE,
        value,
        sources,
        None,
        _CONSTRUCTION_TOKEN,
    )


def _unavailable(
    overlay_type: type[_OverlayResult],
    query: ObservationQueryResult,
    window: OverlayWindow,
    sources: tuple[OverlaySourceEvent, ...],
    reason: OverlayUnavailableReason,
) -> _OverlayResult:
    return overlay_type(
        query.source_run_id,
        query.source_event_sha256,
        query.query_id,
        window.window_projection_id,
        query.policy.mode,
        query.policy.policy_id,
        query.request.render_cursor_time_us,
        window,
        OverlayAvailability.UNAVAILABLE,
        None,
        sources,
        reason,
        _CONSTRUCTION_TOKEN,
    )


def _window(
    query: ObservationQueryResult,
    specification: OverlaySpecification,
    window_projection_id: str,
) -> OverlayWindow:
    cursor = query.request.render_cursor_time_us
    if specification.window_basis is OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR:
        start = cursor
    elif specification.window_basis is OverlayWindowBasis.TRAILING_CLOSED_INTERVAL:
        lookback = specification.lookback_us
        if lookback is None:  # pragma: no cover - closed specification invariant
            raise RuntimeError("trailing overlay specification lost its lookback")
        start = max(0, cursor - lookback)
    else:
        start = 0
    return OverlayWindow(
        specification.window_basis,
        start,
        cursor,
        specification.lookback_us,
        window_projection_id,
    )


def _validate_window_for_specification(
    window: OverlayWindow,
    cursor: int,
    specification: OverlaySpecification,
) -> None:
    if (
        window.basis is not specification.window_basis
        or window.lookback_us != specification.lookback_us
        or window.end_time_us != cursor
    ):
        raise ValueError("overlay window differs from its V1 specification")
    expected_start = (
        cursor
        if specification.window_basis is OverlayWindowBasis.INSTANTANEOUS_AT_CURSOR
        else (
            max(0, cursor - specification.lookback_us)
            if specification.lookback_us is not None
            else 0
        )
    )
    if window.start_time_us != expected_start:
        raise ValueError("overlay window start differs from its deterministic cursor rule")


def _latest(
    values: tuple[_ProjectedOverlayEvent, ...],
) -> _ProjectedOverlayEvent | None:
    return None if not values else max(values, key=_projected_event_order)


def _within_window(
    values: tuple[_ProjectedOverlayEvent, ...],
    window: OverlayWindow,
) -> tuple[_ProjectedOverlayEvent, ...]:
    return tuple(
        item
        for item in sorted(values, key=_projected_event_order)
        if window.contains(item.data_age.policy_visible_at_time_us)
    )


def _projected_event_order(
    value: _ProjectedOverlayEvent,
) -> tuple[int, int, str]:
    return (
        value.data_age.policy_visible_at_time_us,
        value.sequence,
        value.event_id,
    )


def _source_event_order(event: OverlaySourceEvent) -> tuple[int, int, str]:
    return (event.policy_visible_at_time_us, event.sequence, event.event_id)


def _source_reference(value: _ProjectedOverlayEvent) -> OverlaySourceEvent:
    queried = value.queried_value
    return OverlaySourceEvent(
        event_id=value.event_id,
        series_id=value.series_id,
        sequence=value.sequence,
        source_kind=queried.source_kind,
        source_event_time_us=value.data_age.source_event_time_us,
        policy_visible_at_time_us=value.data_age.policy_visible_at_time_us,
        payload_sha256=queried.payload_sha256,
        source_evidence_sha256=queried.source_evidence_sha256,
        query_ids=value.query_ids,
        record_role=value.record_role,
        _construction_token=_CONSTRUCTION_TOKEN,
    )


def _source_references(
    values: tuple[_ProjectedOverlayEvent, ...],
) -> tuple[OverlaySourceEvent, ...]:
    by_event: dict[str, OverlaySourceEvent] = {}
    for value in values:
        by_event.setdefault(value.event_id, _source_reference(value))
    return tuple(sorted(by_event.values(), key=_source_event_order))


def _payload_mapping(
    value: _ProjectedOverlayEvent,
) -> Mapping[str, object] | None:
    queried = value.queried_value
    if queried.disposition is not RecordDisposition.VALUE:
        return None
    payload = thaw_json(queried.payload)
    return payload if isinstance(payload, Mapping) else None


def _payload_exact_int(
    payload: Mapping[str, object],
    field_name: str,
    *,
    minimum: int | None = None,
) -> int | None:
    value = payload.get(field_name)
    if type(value) is not int:
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def _payload_field_int(
    value: _ProjectedOverlayEvent,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int | None:
    payload = _payload_mapping(value)
    if payload is None:
        return None
    return _payload_exact_int(payload, field_name, minimum=minimum)


def _payload_identifier(
    payload: Mapping[str, object],
    field_name: str,
) -> str | None:
    value = payload.get(field_name)
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        return None
    return value


def _roles_match(
    values: tuple[_ProjectedOverlayEvent, ...],
    role: OverlayRecordRole,
) -> bool:
    return all(value.record_role is role for value in values)


def _classify_record_role(value: QueriedValue) -> OverlayRecordRole | None:
    if value.disposition is not RecordDisposition.VALUE:
        return None
    payload = thaw_json(value.payload)
    if not isinstance(payload, Mapping):
        return None
    declared_role = payload.get("record_role")
    for role in OverlayRecordRole:
        root = _ROLE_SERIES_ROOT[role]
        if (
            value.series_id.startswith(root + ".")
            and value.source_kind is _ROLE_SOURCE_KIND[role]
            and declared_role == role.value
            and type(declared_role) is str
        ):
            return role
    return None


def _projection_id_from_root(
    source_run_id: str,
    source_event_sha256: str,
    observation_mode: ObservationMode,
    policy_id: str,
    terminal_query_id: str,
    render_cursor_time_us: int,
    query_inventory_sha256: str,
    event_inventory_sha256: str,
) -> str:
    return "overlay-window-projection-" + _canonical_sha256(
        {
            "observation_mode": observation_mode.value,
            "event_inventory_sha256": event_inventory_sha256,
            "policy_id": policy_id,
            "query_inventory_sha256": query_inventory_sha256,
            "render_cursor_time_us": render_cursor_time_us,
            "schema_id": OVERLAY_WINDOW_PROJECTION_SCHEMA_ID,
            "schema_version": OVERLAY_WINDOW_PROJECTION_SCHEMA_VERSION,
            "source_event_sha256": source_event_sha256,
            "source_run_id": source_run_id,
            "terminal_query_id": terminal_query_id,
        }
    )[:24]


def _round_div_even(numerator: int, denominator: int) -> int:
    if type(numerator) is not int or type(denominator) is not int:
        raise TypeError("overlay round-div-even operands must be exact integers")
    if denominator <= 0:
        raise ValueError("overlay round-div-even denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return -quotient if numerator < 0 else quotient


__all__ = [
    "CANCELLATION_VELOCITY_SPECIFICATION",
    "CANCELLATION_VELOCITY_WINDOW_US",
    "CancellationVelocityOverlay",
    "DerivedCalculationContract",
    "IMPLEMENTATION_SHORTFALL_SPECIFICATION",
    "IMBALANCE_SPECIFICATION",
    "ImplementationShortfallOverlay",
    "ImbalanceOverlay",
    "MICROBASIS_POINTS_PER_RETURN",
    "MICROPRICE_SPECIFICATION",
    "MicropriceOverlay",
    "OVERLAY_KIND_ORDER",
    "OVERLAY_SCHEMA_VERSION",
    "OVERLAY_SET_SCHEMA_ID",
    "OVERLAY_SET_SCHEMA_VERSION",
    "OVERLAY_SPECIFICATIONS",
    "OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_ID",
    "OVERLAY_WINDOW_PROJECTION_RECEIPT_SCHEMA_VERSION",
    "OVERLAY_WINDOW_PROJECTION_SCHEMA_ID",
    "OVERLAY_WINDOW_PROJECTION_SCHEMA_VERSION",
    "OverlayAvailability",
    "OverlayInputSelection",
    "OverlayKind",
    "OverlayRecordRole",
    "OverlayResult",
    "OverlayRoundingRule",
    "OverlaySet",
    "OverlaySourceEvent",
    "OverlaySpecification",
    "OverlayUnavailableReason",
    "OverlayUnit",
    "OverlayWindow",
    "OverlayWindowBasis",
    "OverlayWindowProjection",
    "OverlayWindowProjectionReceipt",
    "RATIO_SCALE_PPM",
    "RELATIVE_VOLUME_SPECIFICATION",
    "RELATIVE_VOLUME_WINDOW_US",
    "REPLENISHMENT_SPECIFICATION",
    "REPLENISHMENT_WINDOW_US",
    "RelativeVolumeOverlay",
    "ReplenishmentOverlay",
    "SHORT_TERM_VOLATILITY_SPECIFICATION",
    "SHORT_TERM_VOLATILITY_WINDOW_US",
    "SPREAD_SPECIFICATION",
    "ShortTermVolatilityOverlay",
    "SpreadOverlay",
    "TRADE_VELOCITY_SPECIFICATION",
    "TRADE_VELOCITY_WINDOW_US",
    "TradeVelocityOverlay",
    "build_overlay_set",
    "build_overlay_window_projection",
    "overlay_series_id",
]
