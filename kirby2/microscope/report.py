"""Canonical, offline presentation frames and portable replay reports.

The objects in this module are output contracts.  They bind the synchronized
WO36-C read models without exporting the query/evidence inputs, timeline receipt,
private inventory, or installed-content-pack code.  The bundled renderer is static
HTML/CSS/JavaScript loaded only from this installed Kirby2 package.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from kirby2.immutable import freeze_json, thaw_json

from .overlays import (
    OVERLAY_KIND_ORDER,
    OverlayAvailability,
    OverlayKind,
    OverlaySet,
    OverlayUnit,
)
from .panes import (
    PANE_ORDER,
    PaneAvailability,
    PaneKind,
    QueueTruthAvailability,
    SynchronizedPaneSnapshot,
)
from .policy import ObservationMode, RevealAvailability
from .query import EvidenceSourceKind, ObservationQueryResult, QueriedValue
from .timeline import (
    ReplayTimeline,
    TimelineCursor,
    TimelineEventKind,
    TimelinePlaybackState,
)


REPLAY_PRESENTATION_FRAME_SCHEMA_ID = "KIRBY2_REPLAY_PRESENTATION_FRAME_V1"
REPLAY_PRESENTATION_FRAME_SCHEMA_VERSION = 1
REPLAY_PRESENTATION_METADATA_SCHEMA_ID = "KIRBY2_REPLAY_PRESENTATION_METADATA_V1"
REPLAY_PRESENTATION_METADATA_SCHEMA_VERSION = 1
PORTABLE_REPLAY_REPORT_SCHEMA_ID = "KIRBY2_PORTABLE_REPLAY_REPORT_V1"
PORTABLE_REPLAY_REPORT_SCHEMA_VERSION = 1
PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID = "KIRBY2_PORTABLE_REPORT_BUNDLE_V1"
PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_VERSION = 1
OFFLINE_RENDERER_ID = "KIRBY2_OFFLINE_REPLAY_RENDERER_V1"
OFFLINE_RENDERER_VERSION = 1
REPORT_ASSET_LICENSE_ID = "KIRBY2_PROJECT_LICENSE"

REPORT_ASSET_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "report.css": "f6a209cbb33706c428478d512a452b49e6e0f28857790c525db641929adc0ade",
        "report.html": "74f9c81d47de288cf77554ec3e39f7f99a9c5cca990172e31976cbae36d6c96e",
        "report.js": "130bc096ba68295447b7ab9d737725abc68da6b03abbc5fbd9467c31560ba1bd",
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_FRAME_TOKEN = object()
_PRESENTATION_TOKEN = object()
_REPORT_TOKEN = object()
_SECTION_TOKEN = object()
_BUNDLE_TOKEN = object()

_MATERIAL_REPORT_MEMBERS = (
    "assets/report.css",
    "assets/report.js",
    "index.html",
)
_COMPLETE_REPORT_MEMBERS = (*_MATERIAL_REPORT_MEMBERS, "manifest.json")
_REPORT_DATA_OPEN = '<script id="kirby2-report-data" type="application/json">'
_REPORT_DATA_CLOSE = "</script>"


def _identifier(value: object, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _display_text(
    value: object,
    label: str,
    *,
    empty: bool = False,
) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be text")
    if not empty and not value:
        raise ValueError(f"{label} must be nonempty")
    if len(value) > 4096 or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} is not normalized bounded text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{label} contains a control character")


def _nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


class PresentationSemanticRole(str, Enum):
    NEUTRAL = "NEUTRAL"
    BID = "BID"
    ASK = "ASK"
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    CAUTION = "CAUTION"
    RECORDED_EMPTY = "RECORDED_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    AS_OBSERVED = "AS_OBSERVED"
    POSTMORTEM = "POSTMORTEM"
    INVARIANT_WARNING = "INVARIANT_WARNING"


class PaneRendererKind(str, Enum):
    PRICE_LADDER = "PRICE_LADDER"
    EVENT_TABLE = "EVENT_TABLE"
    HEATMAP_CELLS = "HEATMAP_CELLS"
    EVIDENCE_CARD = "EVIDENCE_CARD"
    STATE_TABLE = "STATE_TABLE"
    CHRONOLOGY = "CHRONOLOGY"
    LEDGER = "LEDGER"
    STATUS_EVIDENCE = "STATUS_EVIDENCE"
    RULE_ROWS = "RULE_ROWS"
    PROVENANCE_ROWS = "PROVENANCE_ROWS"
    TIMING_TABLE = "TIMING_TABLE"
    QUOTE_GRID = "QUOTE_GRID"
    METRIC_LEDGER = "METRIC_LEDGER"
    TRACE_REFERENCES = "TRACE_REFERENCES"
    TYPED_UNAVAILABLE = "TYPED_UNAVAILABLE"


class ReportSectionKind(str, Enum):
    RUN_REFERENCES = "RUN_REFERENCES"
    BOOKMARKS = "BOOKMARKS"
    ANNOTATIONS = "ANNOTATIONS"
    SELECTED_SNAPSHOTS = "SELECTED_SNAPSHOTS"
    CAUSAL_TRACES = "CAUSAL_TRACES"
    BRANCH_COMPARISON = "BRANCH_COMPARISON"
    METRIC_SUMMARY = "METRIC_SUMMARY"
    PROVENANCE = "PROVENANCE"
    ACTIVE_OBSERVATION_POLICY = "ACTIVE_OBSERVATION_POLICY"
    RENDERER_VERSION = "RENDERER_VERSION"
    LIMITATIONS = "LIMITATIONS"


REPORT_SECTION_ORDER = tuple(ReportSectionKind)


class ReportSectionAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    RECORDED_EMPTY = "RECORDED_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_AVAILABLE_UNTIL_WO36_E = "NOT_AVAILABLE_UNTIL_WO36_E"


class DeferredCapabilityKind(str, Enum):
    BOOKMARK = "BOOKMARK"
    ANNOTATION = "ANNOTATION"
    COUNTERFACTUAL_SELECTION = "COUNTERFACTUAL_SELECTION"
    COMPARISON = "COMPARISON"


class DeferredCapabilityStatus(str, Enum):
    NOT_AVAILABLE_UNTIL_WO36_E = "NOT_AVAILABLE_UNTIL_WO36_E"


class ClockTimeBasis(str, Enum):
    SIMULATION_TIME = "SIMULATION_TIME"
    UTC = "UTC"
    EXCHANGE_LOCAL = "EXCHANGE_LOCAL"


class PresentationMetadataAuthority(str, Enum):
    SOURCE_BOUND_DISPLAY_DECLARATION = "SOURCE_BOUND_DISPLAY_DECLARATION"


class FormatterSignPolicy(str, Enum):
    AUTO = "AUTO"
    ALWAYS = "ALWAYS"
    NEVER = "NEVER"


class FormatterTrailingZeroPolicy(str, Enum):
    TRIM = "TRIM"
    PRESERVE = "PRESERVE"


class MarketClassification(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NORMAL = "NORMAL"
    LOCKED = "LOCKED"
    CROSSED_COMPOSITE = "CROSSED_COMPOSITE"


class IntegrityAssessment(str, Enum):
    NOT_ASSESSED = "NOT_ASSESSED"
    RECORDED_VALID = "RECORDED_VALID"
    RECORDED_INVALID = "RECORDED_INVALID"


@dataclass(frozen=True, slots=True)
class RecordingPresentation:
    recording_id: str
    display_name: str
    content_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.recording_id, "recording ID")
        _display_text(self.display_name, "recording display name")
        _sha256(self.content_sha256, "recording content SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {
            "content_sha256": self.content_sha256,
            "display_name": self.display_name,
            "recording_id": self.recording_id,
        }


@dataclass(frozen=True, slots=True)
class ReportPresentation:
    summary: str
    status: str = "COMPLETE"

    def __post_init__(self) -> None:
        _display_text(self.summary, "report summary")
        if self.status != "COMPLETE":
            raise ValueError("portable report presentation status must be COMPLETE")

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class ClockPresentation:
    time_basis: ClockTimeBasis
    session_origin_time_us: int
    display_precision_us: int
    cursor_label: str
    session_date: str | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        if type(self.time_basis) is not ClockTimeBasis:
            raise TypeError("clock time basis is invalid")
        _nonnegative_int(self.session_origin_time_us, "clock session origin")
        _positive_int(self.display_precision_us, "clock display precision")
        _display_text(self.cursor_label, "clock cursor label")
        if self.session_date is not None:
            _display_text(self.session_date, "clock session date")
        if self.timezone is not None:
            _display_text(self.timezone, "clock timezone")
        if self.time_basis is ClockTimeBasis.SIMULATION_TIME and (
            self.session_date is not None or self.timezone is not None
        ):
            raise ValueError("simulation clock cannot claim calendar or timezone metadata")
        if self.time_basis is not ClockTimeBasis.SIMULATION_TIME and (
            self.session_date is None or self.timezone is None
        ):
            raise ValueError("calendar clock requires session date and timezone")

    def as_dict(self) -> dict[str, object]:
        return {
            "cursor_label": self.cursor_label,
            "display_precision_us": self.display_precision_us,
            "session_date": self.session_date,
            "session_origin_time_us": self.session_origin_time_us,
            "time_basis": self.time_basis.value,
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class InstrumentPresentation:
    instrument_id: str
    symbol: str
    display_name: str
    venue_labels: tuple[str, ...]
    currency: str
    tick_numerator: int
    tick_denominator: int
    price_precision: int
    quantity_unit: str
    lot_size: int

    def __post_init__(self) -> None:
        _identifier(self.instrument_id, "instrument ID")
        _identifier(self.symbol, "instrument symbol")
        _display_text(self.display_name, "instrument display name")
        if type(self.venue_labels) is not tuple or not self.venue_labels:
            raise ValueError("instrument venue labels must be a nonempty tuple")
        labels = tuple(sorted(self.venue_labels))
        if len(labels) != len(set(labels)):
            raise ValueError("instrument venue labels are duplicated")
        for label in labels:
            _display_text(label, "instrument venue label")
        object.__setattr__(self, "venue_labels", labels)
        _identifier(self.currency, "instrument currency")
        _positive_int(self.tick_numerator, "instrument tick numerator")
        _positive_int(self.tick_denominator, "instrument tick denominator")
        if math.gcd(self.tick_numerator, self.tick_denominator) != 1:
            raise ValueError("instrument tick ratio must be reduced")
        _nonnegative_int(self.price_precision, "instrument price precision")
        _identifier(self.quantity_unit, "instrument quantity unit")
        _positive_int(self.lot_size, "instrument lot size")

    def as_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "display_name": self.display_name,
            "instrument_id": self.instrument_id,
            "lot_size": self.lot_size,
            "price_precision": self.price_precision,
            "quantity_unit": self.quantity_unit,
            "symbol": self.symbol,
            "tick_denominator": self.tick_denominator,
            "tick_numerator": self.tick_numerator,
            "venue_labels": list(self.venue_labels),
        }


@dataclass(frozen=True, slots=True)
class ReplayPresentationContext:
    source_run_id: str
    source_event_sha256: str
    metadata_authority: PresentationMetadataAuthority
    recording: RecordingPresentation
    report: ReportPresentation
    clock: ClockPresentation
    instrument: InstrumentPresentation
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.source_run_id, "presentation source run ID")
        _sha256(self.source_event_sha256, "presentation source event SHA-256")
        if type(self.metadata_authority) is not PresentationMetadataAuthority:
            raise TypeError("presentation metadata authority is invalid")
        if type(self.recording) is not RecordingPresentation:
            raise TypeError("presentation recording metadata is invalid")
        if type(self.report) is not ReportPresentation:
            raise TypeError("presentation report metadata is invalid")
        if type(self.clock) is not ClockPresentation:
            raise TypeError("presentation clock metadata is invalid")
        if type(self.instrument) is not InstrumentPresentation:
            raise TypeError("presentation instrument metadata is invalid")
        if type(self.limitations) is not tuple or not self.limitations:
            raise ValueError("presentation limitations must be a nonempty tuple")
        for limitation in self.limitations:
            _display_text(limitation, "presentation limitation")
        canonical = tuple(sorted(self.limitations))
        if len(canonical) != len(set(canonical)):
            raise ValueError("presentation limitations must be unique")
        object.__setattr__(self, "limitations", canonical)


@dataclass(frozen=True, slots=True)
class RendererAsset:
    name: str
    sha256: str
    license_id: str
    bytes_payload: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.name not in REPORT_ASSET_SHA256:
            raise ValueError("renderer asset name is outside the closed inventory")
        _sha256(self.sha256, "renderer asset SHA-256")
        if self.sha256 != REPORT_ASSET_SHA256[self.name]:
            raise ValueError("renderer asset digest differs from the installed pin")
        if self.license_id != REPORT_ASSET_LICENSE_ID:
            raise ValueError("renderer asset license inventory changed")
        if type(self.bytes_payload) is not bytes or not self.bytes_payload:
            raise ValueError("renderer asset bytes are empty")
        if hashlib.sha256(self.bytes_payload).hexdigest() != self.sha256:
            raise ValueError("renderer asset bytes differ from the declared digest")

    def as_dict(self) -> dict[str, str]:
        return {
            "license_id": self.license_id,
            "name": self.name,
            "sha256": self.sha256,
        }


_PANE_PRESENTATION = MappingProxyType(
    {
        PaneKind.LEVEL_2_LADDER: ("Level 2 ladder", PaneRendererKind.PRICE_LADDER),
        PaneKind.TIME_AND_SALES: ("Time & Sales", PaneRendererKind.EVENT_TABLE),
        PaneKind.DEPTH_HEATMAP: ("Depth heatmap", PaneRendererKind.HEATMAP_CELLS),
        PaneKind.INDIVIDUAL_QUEUE: ("Individual queue", PaneRendererKind.EVIDENCE_CARD),
        PaneKind.PLAYER_ORDERS: ("Player orders", PaneRendererKind.STATE_TABLE),
        PaneKind.ORDER_STATE_LIFECYCLE: ("Order lifecycle", PaneRendererKind.CHRONOLOGY),
        PaneKind.POSITION: ("Position", PaneRendererKind.LEDGER),
        PaneKind.TRAFFIC_LIGHT: ("Traffic light", PaneRendererKind.STATUS_EVIDENCE),
        PaneKind.STRATEGY_RULE_EVIDENCE: ("Strategy rule evidence", PaneRendererKind.RULE_ROWS),
        PaneKind.FEATURE_PROVENANCE: ("Feature provenance", PaneRendererKind.PROVENANCE_ROWS),
        PaneKind.AGENT_ACTIVITY: ("Agent activity", PaneRendererKind.EVIDENCE_CARD),
        PaneKind.LATENCY_TIMELINE: ("Latency timeline", PaneRendererKind.TIMING_TABLE),
        PaneKind.VENUE_QUOTES: ("Venue quotes", PaneRendererKind.QUOTE_GRID),
        PaneKind.CONSOLIDATED_QUOTES: ("Consolidated quotes", PaneRendererKind.QUOTE_GRID),
        PaneKind.FILLS: ("Fills", PaneRendererKind.EVENT_TABLE),
        PaneKind.EXECUTION_METRICS: ("Execution metrics", PaneRendererKind.METRIC_LEDGER),
        PaneKind.MECHANISTIC_TRACE: ("Mechanistic trace", PaneRendererKind.TRACE_REFERENCES),
        PaneKind.COUNTERFACTUAL_COMPARISON: (
            "Counterfactual comparison",
            PaneRendererKind.TYPED_UNAVAILABLE,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class OverlayFormatter:
    kind: OverlayKind
    formatter_id: str
    formatter_version: int
    display_divisor: int
    decimal_precision: int
    suffix: str
    sign_policy: FormatterSignPolicy
    trailing_zero_policy: FormatterTrailingZeroPolicy
    semantic_role: PresentationSemanticRole

    def __post_init__(self) -> None:
        if type(self.kind) is not OverlayKind:
            raise TypeError("overlay formatter kind is invalid")
        _identifier(self.formatter_id, "overlay formatter ID")
        _positive_int(self.formatter_version, "overlay formatter version")
        _positive_int(self.display_divisor, "overlay display divisor")
        _nonnegative_int(self.decimal_precision, "overlay decimal precision")
        _display_text(self.suffix, "overlay suffix", empty=True)
        if type(self.sign_policy) is not FormatterSignPolicy:
            raise TypeError("overlay formatter sign policy is invalid")
        if type(self.trailing_zero_policy) is not FormatterTrailingZeroPolicy:
            raise TypeError("overlay trailing-zero policy is invalid")
        if type(self.semantic_role) is not PresentationSemanticRole:
            raise TypeError("overlay semantic role is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "decimal_precision": self.decimal_precision,
            "display_divisor": self.display_divisor,
            "formatter_id": self.formatter_id,
            "formatter_version": self.formatter_version,
            "kind": self.kind.value,
            "semantic_role": self.semantic_role.value,
            "sign_policy": self.sign_policy.value,
            "suffix": self.suffix,
            "trailing_zero_policy": self.trailing_zero_policy.value,
        }


OVERLAY_FORMATTERS = (
    OverlayFormatter(
        OverlayKind.SPREAD,
        "SPREAD_TICKS_V1",
        1,
        1,
        0,
        " ticks",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.MICROPRICE,
        "MICROPRICE_TICKS_V1",
        1,
        1_000_000,
        6,
        " ticks",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.IMBALANCE,
        "IMBALANCE_PERCENT_V1",
        1,
        10_000,
        2,
        "%",
        FormatterSignPolicy.ALWAYS,
        FormatterTrailingZeroPolicy.PRESERVE,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.TRADE_VELOCITY,
        "TRADE_VELOCITY_V1",
        1,
        1_000_000,
        2,
        " trades/s",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.CANCELLATION_VELOCITY,
        "CANCEL_VELOCITY_V1",
        1,
        1_000_000,
        2,
        " shares/s",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.REPLENISHMENT,
        "REPLENISHMENT_V1",
        1,
        1_000_000,
        2,
        " shares/s",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.RELATIVE_VOLUME,
        "RELATIVE_VOLUME_V1",
        1,
        1_000_000,
        3,
        "×",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.SHORT_TERM_VOLATILITY,
        "VOLATILITY_BPS_V1",
        1,
        1_000_000,
        4,
        " bp",
        FormatterSignPolicy.NEVER,
        FormatterTrailingZeroPolicy.TRIM,
        PresentationSemanticRole.NEUTRAL,
    ),
    OverlayFormatter(
        OverlayKind.IMPLEMENTATION_SHORTFALL,
        "SHORTFALL_TICK_SHARES_V1",
        1,
        2,
        1,
        " tick-shares",
        FormatterSignPolicy.ALWAYS,
        FormatterTrailingZeroPolicy.PRESERVE,
        PresentationSemanticRole.NEUTRAL,
    ),
)

if tuple(item.kind for item in OVERLAY_FORMATTERS) != OVERLAY_KIND_ORDER:
    raise RuntimeError("overlay formatter inventory differs from WO36-C")


_EVENT_PRESENTATION = MappingProxyType(
    {
        TimelineEventKind.OBSERVED_UPDATE: (
            "Observed update",
            "A client-visible value changed at this cursor.",
            PresentationSemanticRole.NEUTRAL,
        ),
        TimelineEventKind.PLAYER_ACTION: (
            "Player action",
            "A recorded player decision became policy-visible.",
            PresentationSemanticRole.CAUTION,
        ),
        TimelineEventKind.FILL: (
            "Fill",
            "A client-delivered fill report became policy-visible.",
            PresentationSemanticRole.POSITIVE,
        ),
        TimelineEventKind.TRAFFIC_LIGHT_TRANSITION: (
            "Traffic-light transition",
            "A recorded strategy permission state changed.",
            PresentationSemanticRole.CAUTION,
        ),
        TimelineEventKind.REVEALED_REGIME_TRANSITION: (
            "Revealed regime transition",
            "Authorized postmortem ground truth records a regime transition.",
            PresentationSemanticRole.POSTMORTEM,
        ),
        TimelineEventKind.INVARIANT_WARNING: (
            "Invariant warning",
            "A declared invariant diagnostic is attached to this partition.",
            PresentationSemanticRole.INVARIANT_WARNING,
        ),
        TimelineEventKind.BRANCH_DIVERGENCE: (
            "Branch divergence",
            "A declared derivation identifies a branch divergence.",
            PresentationSemanticRole.CAUTION,
        ),
    }
)


def load_installed_renderer_assets() -> tuple[RendererAsset, ...]:
    """Load and verify the closed renderer inventory from this installed package."""

    root = files("kirby2.microscope").joinpath("assets")
    loaded: list[RendererAsset] = []
    for name in sorted(REPORT_ASSET_SHA256):
        payload = root.joinpath(name).read_bytes()
        loaded.append(
            RendererAsset(
                name=name,
                sha256=REPORT_ASSET_SHA256[name],
                license_id=REPORT_ASSET_LICENSE_ID,
                bytes_payload=payload,
            )
        )
    return tuple(loaded)


@dataclass(frozen=True, slots=True)
class ReplayPresentationMetadataV1:
    recording: RecordingPresentation
    report: ReportPresentation
    clock: ClockPresentation
    instrument: InstrumentPresentation
    metadata_authority: object
    watermark: object
    events: tuple[object, ...]
    panes: tuple[object, ...]
    overlays: tuple[object, ...]
    provenance: tuple[object, ...]
    deferred_capabilities: tuple[object, ...]
    limitations: tuple[str, ...]
    _construction_token: InitVar[object]
    schema_id: str = REPLAY_PRESENTATION_METADATA_SCHEMA_ID
    schema_version: int = REPLAY_PRESENTATION_METADATA_SCHEMA_VERSION

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _PRESENTATION_TOKEN:
            raise TypeError("replay presentation metadata requires the governed adapter")
        if type(self.recording) is not RecordingPresentation:
            raise TypeError("replay presentation recording is invalid")
        if type(self.report) is not ReportPresentation:
            raise TypeError("replay presentation report is invalid")
        if type(self.clock) is not ClockPresentation:
            raise TypeError("replay presentation clock is invalid")
        if type(self.instrument) is not InstrumentPresentation:
            raise TypeError("replay presentation instrument is invalid")
        object.__setattr__(
            self,
            "metadata_authority",
            freeze_json(self.metadata_authority),
        )
        object.__setattr__(self, "watermark", freeze_json(self.watermark))
        for name in ("events", "panes", "overlays", "provenance", "deferred_capabilities"):
            values = getattr(self, name)
            if type(values) is not tuple:
                raise TypeError(f"presentation {name} must be a tuple")
            object.__setattr__(self, name, tuple(freeze_json(item) for item in values))
        if type(self.limitations) is not tuple or not self.limitations:
            raise ValueError("presentation limitations are invalid")
        if (
            self.schema_id != REPLAY_PRESENTATION_METADATA_SCHEMA_ID
            or self.schema_version != REPLAY_PRESENTATION_METADATA_SCHEMA_VERSION
        ):
            raise ValueError("unsupported replay presentation metadata schema")

    def as_dict(self) -> dict[str, object]:
        return {
            "clock": self.clock.as_dict(),
            "deferred_capabilities": [thaw_json(item) for item in self.deferred_capabilities],
            "events": [thaw_json(item) for item in self.events],
            "instrument": self.instrument.as_dict(),
            "limitations": list(self.limitations),
            "metadata_authority": thaw_json(self.metadata_authority),
            "overlays": [thaw_json(item) for item in self.overlays],
            "panes": [thaw_json(item) for item in self.panes],
            "provenance": [thaw_json(item) for item in self.provenance],
            "recording": self.recording.as_dict(),
            "report": self.report.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "watermark": thaw_json(self.watermark),
        }


@dataclass(frozen=True, slots=True)
class ReplayPresentationFrameV1:
    """One detached, atomically renderable safe projection of WO36-C outputs."""

    identity: object
    timeline_root: object
    cursor: object
    pane_snapshot: object
    overlay_set: object
    presentation: ReplayPresentationMetadataV1
    _construction_token: InitVar[object]
    schema_id: str = REPLAY_PRESENTATION_FRAME_SCHEMA_ID
    schema_version: int = REPLAY_PRESENTATION_FRAME_SCHEMA_VERSION
    frame_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _FRAME_TOKEN:
            raise TypeError("replay presentation frames require the governed factory")
        if type(self.presentation) is not ReplayPresentationMetadataV1:
            raise TypeError("replay frame presentation metadata is invalid")
        for name in ("identity", "timeline_root", "cursor", "pane_snapshot", "overlay_set"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        if (
            self.schema_id != REPLAY_PRESENTATION_FRAME_SCHEMA_ID
            or self.schema_version != REPLAY_PRESENTATION_FRAME_SCHEMA_VERSION
        ):
            raise ValueError("unsupported replay presentation frame schema")
        identity = thaw_json(self.identity)
        if not isinstance(identity, dict):
            raise TypeError("replay frame identity must be an object")
        expected_fields = {
            "action_time_us",
            "cursor_id",
            "observation_mode",
            "observed_projection_sha256",
            "overlay_set_id",
            "playback_state",
            "policy_id",
            "query_id",
            "render_cursor_time_us",
            "requested_reveal_capabilities",
            "reveal_availability",
            "reveal_evidence_sha256",
            "reveal_unavailable_reason",
            "snapshot_id",
            "source_event_sha256",
            "source_run_id",
            "timeline_id",
        }
        if set(identity) != expected_fields:
            raise ValueError("replay frame identity fields changed")
        _reject_forbidden_serialized_material(self._identity_payload())
        if identity["observation_mode"] == ObservationMode.AS_OBSERVED.value:
            _validate_observed_only_payload(self._identity_payload())
        object.__setattr__(
            self,
            "frame_id",
            "replay-presentation-frame-"
            + _canonical_sha256(self._identity_payload())[:24],
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "cursor": thaw_json(self.cursor),
            "identity": thaw_json(self.identity),
            "overlay_set": thaw_json(self.overlay_set),
            "pane_snapshot": thaw_json(self.pane_snapshot),
            "presentation": self.presentation.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "timeline_root": thaw_json(self.timeline_root),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "frame_id": self.frame_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def build_replay_presentation_frame(
    timeline: ReplayTimeline,
    cursor: TimelineCursor,
    query: ObservationQueryResult,
    pane_snapshot: SynchronizedPaneSnapshot,
    overlay_set: OverlaySet,
    context: ReplayPresentationContext,
    *,
    source_queries: tuple[ObservationQueryResult, ...] = (),
) -> ReplayPresentationFrameV1:
    """Validate backend roots, then detach only their safe public projections.

    ``query`` and ``source_queries`` are factory authority.  They are deliberately
    absent from the returned frame.  Calling play/pause against ``timeline`` proves
    that ``cursor`` carries this exact facade's private binding before any public
    dictionary is accepted.
    """

    if type(timeline) is not ReplayTimeline:
        raise TypeError("presentation frame requires ReplayTimeline")
    if type(cursor) is not TimelineCursor:
        raise TypeError("presentation frame requires TimelineCursor")
    if type(query) is not ObservationQueryResult:
        raise TypeError("presentation frame requires an exact query result")
    if type(pane_snapshot) is not SynchronizedPaneSnapshot:
        raise TypeError("presentation frame pane snapshot is invalid")
    if type(overlay_set) is not OverlaySet:
        raise TypeError("presentation frame overlay set is invalid")
    if type(context) is not ReplayPresentationContext:
        raise TypeError("presentation frame context is invalid")
    if type(source_queries) is not tuple or any(
        type(item) is not ObservationQueryResult for item in source_queries
    ):
        raise TypeError("presentation source queries must be an exact tuple")

    canonical_cursor = (
        timeline.play(cursor)
        if cursor.playback_state is TimelinePlaybackState.PLAYING
        else timeline.pause(cursor)
    )
    if canonical_cursor.canonical_bytes() != cursor.canonical_bytes():
        raise ValueError("presentation cursor is not canonical for its timeline")

    timeline_fields = (
        timeline.source_run_id,
        timeline.source_event_sha256,
        timeline.observation_mode,
        timeline.policy_id,
    )
    if timeline_fields != (
        cursor.source_run_id,
        cursor.source_event_sha256,
        cursor.observation_mode,
        cursor.policy_id,
    ) or timeline.timeline_id != cursor.timeline_id:
        raise ValueError("timeline root and presentation cursor disagree")

    _validate_frame_query_roots(query, cursor, pane_snapshot, overlay_set)
    all_queries = _canonical_source_queries(query, source_queries)
    _validate_source_queries(query, all_queries, cursor.render_cursor_time_us)
    if (
        context.source_run_id != query.source_run_id
        or context.source_event_sha256 != query.source_event_sha256
        or context.recording.content_sha256 != query.source_event_sha256
    ):
        raise ValueError("presentation context is not bound to the query source")
    if (
        context.clock.time_basis is ClockTimeBasis.SIMULATION_TIME
        and context.clock.cursor_label
        != f"T+{query.request.render_cursor_time_us}us"
    ):
        raise ValueError("simulation clock label differs from the exact query cursor")

    presentation = _build_presentation_metadata(
        cursor,
        query,
        pane_snapshot,
        overlay_set,
        context,
        all_queries,
    )
    reveal_reason = query.reveal.unavailable_reason
    identity = {
        "action_time_us": query.request.action_time_us,
        "cursor_id": cursor.cursor_id,
        "observation_mode": query.policy.mode.value,
        "observed_projection_sha256": query.observed_projection_sha256,
        "overlay_set_id": overlay_set.overlay_set_id,
        "playback_state": cursor.playback_state.value,
        "policy_id": query.policy.policy_id,
        "query_id": query.query_id,
        "render_cursor_time_us": query.request.render_cursor_time_us,
        "requested_reveal_capabilities": [
            item.value for item in query.request.requested_reveal_capabilities
        ],
        "reveal_availability": query.reveal.availability.value,
        "reveal_evidence_sha256": query.reveal_evidence_sha256,
        "reveal_unavailable_reason": (
            None if reveal_reason is None else reveal_reason.value
        ),
        "snapshot_id": pane_snapshot.snapshot_id,
        "source_event_sha256": query.source_event_sha256,
        "source_run_id": query.source_run_id,
        "timeline_id": timeline.timeline_id,
    }
    return ReplayPresentationFrameV1(
        identity=identity,
        timeline_root=timeline.as_dict(),
        cursor=cursor.as_dict(),
        pane_snapshot=pane_snapshot.as_dict(),
        overlay_set=overlay_set.as_dict(),
        presentation=presentation,
        _construction_token=_FRAME_TOKEN,
    )


def _validate_frame_query_roots(
    query: ObservationQueryResult,
    cursor: TimelineCursor,
    pane_snapshot: SynchronizedPaneSnapshot,
    overlay_set: OverlaySet,
) -> None:
    shared = (
        query.source_run_id,
        query.source_event_sha256,
        query.policy.mode,
        query.policy.policy_id,
        query.request.render_cursor_time_us,
    )
    if shared != (
        cursor.source_run_id,
        cursor.source_event_sha256,
        cursor.observation_mode,
        cursor.policy_id,
        cursor.render_cursor_time_us,
    ):
        raise ValueError("query and timeline cursor roots disagree")
    if shared != (
        pane_snapshot.source_run_id,
        pane_snapshot.source_event_sha256,
        pane_snapshot.observation_mode,
        pane_snapshot.policy_id,
        pane_snapshot.render_cursor_time_us,
    ):
        raise ValueError("query and synchronized pane roots disagree")
    if shared != (
        overlay_set.source_run_id,
        overlay_set.source_event_sha256,
        overlay_set.observation_mode,
        overlay_set.policy_id,
        overlay_set.render_cursor_time_us,
    ):
        raise ValueError("query and overlay roots disagree")
    if pane_snapshot.query_id != query.query_id or overlay_set.query_id != query.query_id:
        raise ValueError("pane and overlay query identities disagree")
    if pane_snapshot.observed_projection_sha256 != query.observed_projection_sha256:
        raise ValueError("pane snapshot observed projection differs from its query")
    if pane_snapshot.action_time_us != query.request.action_time_us:
        raise ValueError("pane snapshot action time differs from its query")
    if pane_snapshot.reveal_availability is not query.reveal.availability:
        raise ValueError("pane snapshot reveal decision differs from its query")
    if pane_snapshot.reveal_evidence_sha256 != query.reveal_evidence_sha256:
        raise ValueError("pane snapshot reveal evidence differs from its query")
    if tuple(item.pane_kind for item in pane_snapshot.panes) != PANE_ORDER:
        raise ValueError("presentation frame pane inventory changed")
    if tuple(item.kind for item in overlay_set.overlays) != OVERLAY_KIND_ORDER:
        raise ValueError("presentation frame overlay inventory changed")


def _canonical_source_queries(
    current: ObservationQueryResult,
    supplied: tuple[ObservationQueryResult, ...],
) -> tuple[ObservationQueryResult, ...]:
    by_id = {item.query_id: item for item in (current, *supplied)}
    if len(by_id) != len((current, *supplied)):
        repeated = tuple(item.query_id for item in (current, *supplied))
        if len(set(repeated)) != len(repeated):
            for query_id in set(repeated):
                matching = tuple(
                    item for item in (current, *supplied) if item.query_id == query_id
                )
                if len({item.canonical_bytes() for item in matching}) != 1:
                    raise ValueError("duplicate source query ID has different bytes")
    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (item.request.render_cursor_time_us, item.query_id),
        )
    )


def _validate_source_queries(
    current: ObservationQueryResult,
    queries: tuple[ObservationQueryResult, ...],
    render_cursor_time_us: int,
) -> None:
    for item in queries:
        if (
            item.source_run_id != current.source_run_id
            or item.source_event_sha256 != current.source_event_sha256
            or item.policy.mode is not current.policy.mode
            or item.policy.policy_id != current.policy.policy_id
            or item.request.render_cursor_time_us > render_cursor_time_us
        ):
            raise ValueError("presentation source query belongs to another frame root")
        if (
            item.reveal.availability is not current.reveal.availability
            or item.reveal_evidence_sha256 != current.reveal_evidence_sha256
            or item.request.requested_reveal_capabilities
            != current.request.requested_reveal_capabilities
        ):
            raise ValueError("presentation source query reveal scope differs")


def _build_presentation_metadata(
    cursor: TimelineCursor,
    query: ObservationQueryResult,
    pane_snapshot: SynchronizedPaneSnapshot,
    overlay_set: OverlaySet,
    context: ReplayPresentationContext,
    queries: tuple[ObservationQueryResult, ...],
) -> ReplayPresentationMetadataV1:
    query_values = _query_value_index(queries)
    events = tuple(
        _event_presentation(item, query_values)
        for item in cursor.current_events
    )
    panes = tuple(
        _pane_presentation(item, query_values, index)
        for index, item in enumerate(pane_snapshot.panes)
    )
    overlays = tuple(
        _overlay_presentation(item, query_values, formatter, index)
        for index, (item, formatter) in enumerate(
            zip(overlay_set.overlays, OVERLAY_FORMATTERS, strict=True)
        )
    )
    provenance = _presentation_provenance(events, panes, overlays)
    deferred = tuple(
        {
            "capability": item.value,
            "reason": DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value,
            "status": DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value,
        }
        for item in DeferredCapabilityKind
    )
    if query.policy.mode is ObservationMode.AS_OBSERVED:
        watermark = {
            "label": "AS OBSERVED · CLIENT-KNOWN EVIDENCE ONLY",
            "semantic_role": PresentationSemanticRole.AS_OBSERVED.value,
        }
    elif query.reveal.availability is RevealAvailability.AVAILABLE:
        watermark = {
            "label": "AUTHORIZED POSTMORTEM · REVEALED EVIDENCE",
            "semantic_role": PresentationSemanticRole.POSTMORTEM.value,
        }
    else:
        watermark = {
            "label": "POSTMORTEM · REVEAL UNAVAILABLE",
            "semantic_role": PresentationSemanticRole.UNAVAILABLE.value,
        }
    return ReplayPresentationMetadataV1(
        recording=context.recording,
        report=context.report,
        clock=context.clock,
        instrument=context.instrument,
        metadata_authority={
            "authority": context.metadata_authority.value,
            "evidence_classification": "PRESENTATION_ONLY_NOT_MARKET_EVIDENCE",
            "source_event_sha256": context.source_event_sha256,
            "source_run_id": context.source_run_id,
        },
        watermark=watermark,
        events=events,
        panes=panes,
        overlays=overlays,
        provenance=provenance,
        deferred_capabilities=deferred,
        limitations=context.limitations,
        _construction_token=_PRESENTATION_TOKEN,
    )


def _query_value_index(
    queries: tuple[ObservationQueryResult, ...],
) -> Mapping[str, tuple[tuple[ObservationQueryResult, QueriedValue], ...]]:
    indexed: dict[str, list[tuple[ObservationQueryResult, QueriedValue]]] = {}
    for query in queries:
        for value in query.values:
            indexed.setdefault(value.event_id, []).append((query, value))
    return MappingProxyType(
        {
            event_id: tuple(
                sorted(
                    values,
                    key=lambda pair: (
                        pair[0].request.render_cursor_time_us,
                        pair[0].query_id,
                    ),
                )
            )
            for event_id, values in indexed.items()
        }
    )


def _event_presentation(
    event: object,
    query_values: Mapping[str, tuple[tuple[ObservationQueryResult, QueriedValue], ...]],
) -> dict[str, object]:
    event_kind = event.event_kind
    title, summary, semantic_role = _EVENT_PRESENTATION[event_kind]
    if event.derivation is not None:
        evidence_role = "DECLARED_DERIVATION"
    else:
        matches = query_values.get(event.event_id, ())
        exact = tuple(
            value
            for source_query, value in matches
            if source_query.query_id == event.query_id
        )
        if len(exact) != 1:
            raise ValueError("current event presentation lacks its exact query source")
        evidence_role = exact[0].source_kind.value
    return {
        "event_id": event.event_id,
        "event_kind": event_kind.value,
        "evidence_role": evidence_role,
        "policy_visible_at_time_us": event.policy_visible_at_time_us,
        "semantic_role": semantic_role.value,
        "source_reference": event.as_dict(),
        "summary": summary,
        "title": title,
    }


def _pane_presentation(
    pane: object,
    query_values: Mapping[str, tuple[tuple[ObservationQueryResult, QueriedValue], ...]],
    display_order: int,
) -> dict[str, object]:
    title, renderer = _PANE_PRESENTATION[pane.pane_kind]
    references: dict[tuple[str, str], dict[str, object]] = {}
    rows: list[dict[str, object]] = []
    for datum in pane.data:
        source_ids: list[str] = []
        for source in datum.source_events:
            source_ids.append(source.event_id)
            reference = _source_presentation(source.as_dict(), query_values)
            references[(source.event_id, source.payload_sha256)] = reference
        rows.append(
            {
                "cited_datum_id": datum.datum_id,
                "display_order": len(rows),
                "display_text": _display_json_value(thaw_json(datum.value), datum.unit),
                "label": datum.label.replace("_", " "),
                "raw_value_kind": _json_value_kind(thaw_json(datum.value)),
                "semantic_role": PresentationSemanticRole.NEUTRAL.value,
                "source_event_ids": sorted(source_ids),
                "unit": datum.unit,
            }
        )
    for estimate in pane.queue_estimates:
        source_ids = []
        for source in estimate.source_events:
            source_ids.append(source.event_id)
            reference = _source_presentation(source.as_dict(), query_values)
            references[(source.event_id, source.payload_sha256)] = reference
        rows.append(
            {
                "cited_queue_estimate_id": estimate.queue_id,
                "display_order": len(rows),
                "display_text": _canonical_json_bytes(estimate.as_dict()).decode("ascii"),
                "label": "queue estimate evidence",
                "raw_value_kind": "CANONICAL_JSON",
                "semantic_role": PresentationSemanticRole.NEUTRAL.value,
                "source_event_ids": sorted(source_ids),
                "truth_availability": estimate.truth_availability.value,
                "unit": "quantity",
            }
        )
    explanation = (
        None
        if pane.explanation is None
        else f"{pane.explanation.reason.value}: {pane.explanation.detail}"
    )
    classification, integrity = _pane_market_classification(pane)
    semantic = {
        PaneAvailability.AVAILABLE: PresentationSemanticRole.NEUTRAL,
        PaneAvailability.RECORDED_EMPTY: PresentationSemanticRole.RECORDED_EMPTY,
        PaneAvailability.UNAVAILABLE: PresentationSemanticRole.UNAVAILABLE,
    }[pane.availability]
    return {
        "availability": pane.availability.value,
        "display_order": display_order,
        "explanation": explanation,
        "integrity_assessment": integrity.value,
        "market_classification": classification.value,
        "pane_kind": pane.pane_kind.value,
        "presentation_schema_id": "KIRBY2_PANE_PRESENTATION_V1",
        "presentation_schema_version": 1,
        "renderer_kind": renderer.value,
        "rows": rows,
        "semantic_role": semantic.value,
        "source_references": [
            references[key] for key in sorted(references)
        ],
        "title": title,
    }


def _pane_market_classification(
    pane: object,
) -> tuple[MarketClassification, IntegrityAssessment]:
    if pane.pane_kind is not PaneKind.CONSOLIDATED_QUOTES or not pane.data:
        return MarketClassification.NOT_APPLICABLE, IntegrityAssessment.NOT_ASSESSED
    classifications: list[MarketClassification] = []
    for datum in pane.data:
        value = thaw_json(datum.value)
        if not isinstance(value, dict):
            continue
        bid = value.get("best_bid_ticks")
        ask = value.get("best_ask_ticks")
        if type(bid) is not int or type(ask) is not int:
            continue
        if bid > ask:
            classifications.append(MarketClassification.CROSSED_COMPOSITE)
        elif bid == ask:
            classifications.append(MarketClassification.LOCKED)
        else:
            classifications.append(MarketClassification.NORMAL)
    if not classifications:
        return MarketClassification.NOT_APPLICABLE, IntegrityAssessment.NOT_ASSESSED
    classification = max(
        classifications,
        key=lambda item: tuple(MarketClassification).index(item),
    )
    return classification, IntegrityAssessment.NOT_ASSESSED


def _overlay_presentation(
    overlay: object,
    query_values: Mapping[str, tuple[tuple[ObservationQueryResult, QueriedValue], ...]],
    formatter: OverlayFormatter,
    display_order: int,
) -> dict[str, object]:
    if overlay.kind is not formatter.kind:
        raise ValueError("overlay formatter belongs to another overlay")
    references = tuple(
        _source_presentation(source.as_dict(), query_values)
        for source in overlay.source_events
    )
    available = overlay.availability is OverlayAvailability.AVAILABLE
    display_value = (
        None if not available else _format_scaled_integer(overlay.value, formatter)
    )
    explanation = (
        None
        if available
        else overlay.unavailable_reason.value.replace("_", " ").title()
    )
    window = overlay.window
    if window.lookback_us is None:
        window_label = window.basis.value.replace("_", " ").title()
    else:
        window_label = f"{window.basis.value.replace('_', ' ').title()} · {window.lookback_us} µs"
    return {
        "availability": overlay.availability.value,
        "display_order": display_order,
        "display_value": display_value,
        "explanation": explanation,
        "formatter": formatter.as_dict(),
        "formatter_id": formatter.formatter_id,
        "kind": overlay.kind.value,
        "raw_value_decimal": None if overlay.value is None else str(overlay.value),
        "semantic_role": formatter.semantic_role.value,
        "source_references": list(references),
        "title": overlay.kind.value.replace("_", " ").title(),
        "unit": overlay.unit.value,
        "unavailable_reason": (
            None if overlay.unavailable_reason is None else overlay.unavailable_reason.value
        ),
        "window_label": window_label,
    }


def _source_presentation(
    source: Mapping[str, object],
    query_values: Mapping[str, tuple[tuple[ObservationQueryResult, QueriedValue], ...]],
) -> dict[str, object]:
    event_id = source.get("event_id")
    payload_sha256 = source.get("payload_sha256")
    matches = tuple(
        (query, value)
        for query, value in query_values.get(str(event_id), ())
        if value.payload_sha256 == payload_sha256
    )
    query_ids = source.get("query_ids")
    if query_ids is None:
        allowed_query_ids = {query.query_id for query, _ in matches}
    else:
        if not isinstance(query_ids, list):
            raise TypeError("overlay source query IDs are not a list")
        allowed_query_ids = set(query_ids)
        matches = tuple(pair for pair in matches if pair[0].query_id in allowed_query_ids)
    if not matches:
        raise ValueError("presentation source reference lacks an exact query value")
    observations = [
        {
            "data_age": value.data_age.as_dict(),
            "is_current": (
                value.selection.value == "EXACT_RECORDED"
            ),
            "query_id": query.query_id,
            "query_render_cursor_time_us": query.request.render_cursor_time_us,
            "selection_kind": value.selection.value,
        }
        for query, value in matches
    ]
    return {
        **dict(source),
        "source_observations": observations,
    }


def _presentation_provenance(
    events: tuple[dict[str, object], ...],
    panes: tuple[dict[str, object], ...],
    overlays: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    references: dict[str, dict[str, object]] = {}
    for event in events:
        source = event["source_reference"]
        assert isinstance(source, dict)
        references[f"timeline:{event['event_id']}"] = source
    for pane in panes:
        source_references = pane["source_references"]
        assert isinstance(source_references, list)
        for source in source_references:
            assert isinstance(source, dict)
            references[
                f"pane:{source['event_id']}:{source['payload_sha256']}"
            ] = source
    for overlay in overlays:
        source_references = overlay["source_references"]
        assert isinstance(source_references, list)
        for source in source_references:
            assert isinstance(source, dict)
            references[
                f"overlay:{source['event_id']}:{source['payload_sha256']}"
            ] = source
    return tuple(
        {"reference_id": key, "source": references[key]}
        for key in sorted(references)
    )


@dataclass(frozen=True, slots=True)
class PortableReportSection:
    kind: ReportSectionKind
    title: str
    availability: ReportSectionAvailability
    summary: str
    payload: object
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _SECTION_TOKEN:
            raise TypeError("portable report sections require the report builder")
        if type(self.kind) is not ReportSectionKind:
            raise TypeError("portable report section kind is invalid")
        _display_text(self.title, "portable report section title")
        if type(self.availability) is not ReportSectionAvailability:
            raise TypeError("portable report section availability is invalid")
        _display_text(self.summary, "portable report section summary")
        frozen = freeze_json(self.payload)
        if self.availability is ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E:
            if thaw_json(frozen) != {
                "reason": "NOT_AVAILABLE_UNTIL_WO36_E",
                "records": [],
            }:
                raise ValueError("deferred report section payload changed")
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "kind": self.kind.value,
            "payload": thaw_json(self.payload),
            "summary": self.summary,
            "title": self.title,
        }


def _frame_report_authority(
    frame: ReplayPresentationFrameV1,
) -> dict[str, object]:
    identity = thaw_json(frame.identity)
    presentation = frame.presentation.as_dict()
    assert isinstance(identity, dict)
    clock = dict(presentation["clock"])
    clock.pop("cursor_label", None)
    return {
        "clock": clock,
        "instrument": presentation["instrument"],
        "limitations": presentation["limitations"],
        "metadata_authority": presentation["metadata_authority"],
        "observation_mode": identity["observation_mode"],
        "policy_id": identity["policy_id"],
        "recording": presentation["recording"],
        "report": presentation["report"],
        "requested_reveal_capabilities": identity[
            "requested_reveal_capabilities"
        ],
        "reveal_availability": identity["reveal_availability"],
        "reveal_evidence_sha256": identity["reveal_evidence_sha256"],
        "reveal_unavailable_reason": identity["reveal_unavailable_reason"],
        "source_event_sha256": identity["source_event_sha256"],
        "source_run_id": identity["source_run_id"],
        "watermark": presentation["watermark"],
    }


@dataclass(frozen=True, slots=True)
class PortableReplayReportV1:
    frames: tuple[ReplayPresentationFrameV1, ...]
    sections: tuple[PortableReportSection, ...]
    renderer_assets: tuple[RendererAsset, ...]
    _construction_token: InitVar[object]
    display_generated_at: str | None = None
    schema_id: str = PORTABLE_REPLAY_REPORT_SCHEMA_ID
    schema_version: int = PORTABLE_REPLAY_REPORT_SCHEMA_VERSION
    renderer_id: str = OFFLINE_RENDERER_ID
    renderer_version: int = OFFLINE_RENDERER_VERSION
    report_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _REPORT_TOKEN:
            raise TypeError("portable replay reports require the governed report builder")
        if type(self.frames) is not tuple or not self.frames or any(
            type(item) is not ReplayPresentationFrameV1 for item in self.frames
        ):
            raise ValueError("portable replay report frames are invalid")
        frames = tuple(
            sorted(
                self.frames,
                key=lambda item: (
                    thaw_json(item.identity)["render_cursor_time_us"],
                    item.frame_id,
                ),
            )
        )
        if len({item.frame_id for item in frames}) != len(frames):
            raise ValueError("portable replay report contains duplicate frames")
        authorities = {
            _canonical_json_bytes(_frame_report_authority(item))
            for item in frames
        }
        if len(authorities) != 1:
            raise ValueError(
                "portable replay report frames belong to different authorities"
            )
        object.__setattr__(self, "frames", frames)
        if (
            type(self.sections) is not tuple
            or tuple(item.kind for item in self.sections) != REPORT_SECTION_ORDER
        ):
            raise ValueError("portable report reserved section inventory changed")
        if type(self.renderer_assets) is not tuple or tuple(
            item.name for item in self.renderer_assets
        ) != tuple(sorted(REPORT_ASSET_SHA256)):
            raise ValueError("portable report renderer asset inventory changed")
        if self.display_generated_at is not None:
            _display_text(self.display_generated_at, "display generation timestamp")
        if (
            self.schema_id != PORTABLE_REPLAY_REPORT_SCHEMA_ID
            or self.schema_version != PORTABLE_REPLAY_REPORT_SCHEMA_VERSION
            or self.renderer_id != OFFLINE_RENDERER_ID
            or self.renderer_version != OFFLINE_RENDERER_VERSION
        ):
            raise ValueError("portable report schema or renderer identity changed")
        _reject_forbidden_serialized_material(self.identity_dict())
        object.__setattr__(
            self,
            "report_id",
            "portable-replay-report-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        """Semantic identity deliberately excludes display-only generation time."""

        return {
            "frames": [item.as_dict() for item in self.frames],
            "renderer_assets": [item.as_dict() for item in self.renderer_assets],
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sections": [item.as_dict() for item in self.sections],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "nonidentity_metadata": {
                "display_generated_at": self.display_generated_at,
                "identity_exclusion": "DISPLAY_GENERATED_AT",
            },
            "report_id": self.report_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def build_portable_replay_report(
    frames: tuple[ReplayPresentationFrameV1, ...],
    *,
    display_generated_at: str | None = None,
) -> PortableReplayReportV1:
    """Build all reserved V1 report sections without creating WO36-E sidecars."""

    if type(frames) is not tuple or not frames or any(
        type(item) is not ReplayPresentationFrameV1 for item in frames
    ):
        raise TypeError("portable report requires presentation frames")
    canonical_frames = tuple(
        sorted(
            frames,
            key=lambda item: (
                thaw_json(item.identity)["render_cursor_time_us"],
                item.frame_id,
            ),
        )
    )
    sections = _build_report_sections(canonical_frames)
    return PortableReplayReportV1(
        frames=canonical_frames,
        sections=sections,
        renderer_assets=load_installed_renderer_assets(),
        display_generated_at=display_generated_at,
        _construction_token=_REPORT_TOKEN,
    )


def _build_report_sections(
    frames: tuple[ReplayPresentationFrameV1, ...],
) -> tuple[PortableReportSection, ...]:
    first = frames[0]
    first_identity = thaw_json(first.identity)
    first_presentation = first.presentation.as_dict()
    deferred_payload = {"reason": "NOT_AVAILABLE_UNTIL_WO36_E", "records": []}
    trace_rows: list[object] = []
    metric_rows: list[object] = []
    provenance_rows: list[object] = []
    for frame in frames:
        pane_root = thaw_json(frame.pane_snapshot)
        overlay_root = thaw_json(frame.overlay_set)
        trace = next(
            item
            for item in pane_root["panes"]
            if item["pane_kind"] == PaneKind.MECHANISTIC_TRACE.value
        )
        trace_rows.append(
            {
                "availability": trace["availability"],
                "data": trace["data"],
                "frame_id": frame.frame_id,
            }
        )
        metric_rows.append(
            {
                "frame_id": frame.frame_id,
                "overlays": overlay_root["overlays"],
            }
        )
        provenance_rows.append(
            {
                "frame_id": frame.frame_id,
                "references": frame.presentation.as_dict()["provenance"],
            }
        )
    trace_availabilities = {item["availability"] for item in trace_rows}
    if "AVAILABLE" in trace_availabilities:
        trace_section_availability = ReportSectionAvailability.AVAILABLE
    elif "UNAVAILABLE" in trace_availabilities:
        trace_section_availability = ReportSectionAvailability.UNAVAILABLE
    else:
        trace_section_availability = ReportSectionAvailability.RECORDED_EMPTY
    assets = load_installed_renderer_assets()
    rows = (
        (
            ReportSectionKind.RUN_REFERENCES,
            "Run references",
            ReportSectionAvailability.AVAILABLE,
            "Canonical recording and source identities represented by this report.",
            {
                "recording": first_presentation["recording"],
                "source_event_sha256": first_identity["source_event_sha256"],
                "source_run_id": first_identity["source_run_id"],
            },
        ),
        (
            ReportSectionKind.BOOKMARKS,
            "Bookmarks",
            ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E,
            "Immutable bookmark producers are intentionally deferred.",
            deferred_payload,
        ),
        (
            ReportSectionKind.ANNOTATIONS,
            "Annotations",
            ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E,
            "Immutable annotation sidecars are intentionally deferred.",
            deferred_payload,
        ),
        (
            ReportSectionKind.SELECTED_SNAPSHOTS,
            "Selected snapshots",
            ReportSectionAvailability.AVAILABLE,
            "Content-derived safe frame identities selected for this artifact.",
            {
                "frames": [
                    {
                        "frame_id": item.frame_id,
                        "render_cursor_time_us": thaw_json(item.identity)[
                            "render_cursor_time_us"
                        ],
                    }
                    for item in frames
                ]
            },
        ),
        (
            ReportSectionKind.CAUSAL_TRACES,
            "Causal traces",
            trace_section_availability,
            "Source-linked mechanistic trace references at the selected frames.",
            {"records": trace_rows},
        ),
        (
            ReportSectionKind.BRANCH_COMPARISON,
            "Branch comparison",
            ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E,
            "Counterfactual selection and branch comparison are intentionally deferred.",
            deferred_payload,
        ),
        (
            ReportSectionKind.METRIC_SUMMARY,
            "Metric summary",
            ReportSectionAvailability.AVAILABLE,
            "Exact WO36-C overlay outputs; no report-side metric recomputation.",
            {"records": metric_rows},
        ),
        (
            ReportSectionKind.PROVENANCE,
            "Provenance",
            ReportSectionAvailability.AVAILABLE,
            "Payload-free source links and governed selection/freshness observations.",
            {"records": provenance_rows},
        ),
        (
            ReportSectionKind.ACTIVE_OBSERVATION_POLICY,
            "Active observation and reveal policy",
            ReportSectionAvailability.AVAILABLE,
            "The policy watermark and reveal status are semantic report content.",
            {
                "observation_mode": first_identity["observation_mode"],
                "policy_id": first_identity["policy_id"],
                "requested_reveal_capabilities": first_identity[
                    "requested_reveal_capabilities"
                ],
                "reveal_availability": first_identity["reveal_availability"],
                "reveal_unavailable_reason": first_identity[
                    "reveal_unavailable_reason"
                ],
                "watermark": first_presentation["watermark"],
            },
        ),
        (
            ReportSectionKind.RENDERER_VERSION,
            "Renderer version",
            ReportSectionAvailability.AVAILABLE,
            "Installed digest-bound Kirby2 renderer inventory and licenses.",
            {
                "assets": [item.as_dict() for item in assets],
                "renderer_id": OFFLINE_RENDERER_ID,
                "renderer_version": OFFLINE_RENDERER_VERSION,
            },
        ),
        (
            ReportSectionKind.LIMITATIONS,
            "Limitations",
            ReportSectionAvailability.AVAILABLE,
            "Explicit capability and interpretation limits for this artifact.",
            {"records": list(first_presentation["limitations"])},
        ),
    )
    if tuple(item[0] for item in rows) != REPORT_SECTION_ORDER:
        raise RuntimeError("portable report section builder order changed")
    return tuple(
        PortableReportSection(
            kind=kind,
            title=title,
            availability=availability,
            summary=summary,
            payload=payload,
            _construction_token=_SECTION_TOKEN,
        )
        for kind, title, availability, summary, payload in rows
    )


@dataclass(frozen=True, slots=True)
class PortableReportBundle:
    report_id: str
    members: object
    manifest: object
    bundle_id: str
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _BUNDLE_TOKEN:
            raise TypeError("portable report bundles require the governed renderer")
        _identifier(self.report_id, "portable bundle report ID")
        _identifier(self.bundle_id, "portable bundle ID")
        frozen_members = freeze_json(self.members)
        frozen_manifest = freeze_json(self.manifest)
        members = thaw_json(frozen_members)
        if not isinstance(members, dict) or set(members) != set(
            _COMPLETE_REPORT_MEMBERS
        ):
            raise ValueError("portable report bundle member inventory changed")
        object.__setattr__(self, "members", frozen_members)
        object.__setattr__(self, "manifest", frozen_manifest)
        decoded = {
            name: self.member_bytes(name)
            for name in _COMPLETE_REPORT_MEMBERS
        }
        _validate_bundle_members(
            decoded,
            expected_manifest=thaw_json(frozen_manifest),
            expected_report_id=self.report_id,
            expected_bundle_id=self.bundle_id,
        )

    def member_bytes(self, name: str) -> bytes:
        members = thaw_json(self.members)
        assert isinstance(members, dict)
        encoded = members[name]
        if not isinstance(encoded, str):
            raise TypeError("portable bundle member encoding is invalid")
        return base64.b64decode(encoded.encode("ascii"), validate=True)

    def as_dict(self) -> dict[str, object]:
        manifest = thaw_json(self.manifest)
        assert isinstance(manifest, dict)
        return {
            "bundle_id": self.bundle_id,
            "manifest": manifest,
            "report_id": self.report_id,
        }


_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'none'; "
    "font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; "
    "frame-src 'none'; worker-src 'none'; manifest-src 'none'; base-uri 'none'; "
    "form-action 'none'"
)


def render_portable_report_bundle(
    report: PortableReplayReportV1,
) -> PortableReportBundle:
    """Render a deterministic, relocatable local directory artifact in memory."""

    if type(report) is not PortableReplayReportV1:
        raise TypeError("portable renderer requires PortableReplayReportV1")
    assets = {item.name: item for item in load_installed_renderer_assets()}
    first = report.frames[0]
    watermark = first.presentation.as_dict()["watermark"]
    if not isinstance(watermark, dict) or not isinstance(watermark.get("label"), str):
        raise ValueError("portable report watermark is invalid")
    index_bytes = _render_index_bytes(
        report.canonical_bytes(),
        report.report_id,
        watermark["label"],
        assets,
    )
    material_members = {
        "assets/report.css": assets["report.css"].bytes_payload,
        "assets/report.js": assets["report.js"].bytes_payload,
        "index.html": index_bytes,
    }
    manifest = {
        "members": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(material_members.items())
        ],
        "renderer_assets": [item.as_dict() for item in assets.values()],
        "report_id": report.report_id,
        "report_semantic_sha256": hashlib.sha256(
            _canonical_json_bytes(report.identity_dict())
        ).hexdigest(),
        "schema_id": PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID,
        "schema_version": PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_VERSION,
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    bundle_id = "portable-report-bundle-" + hashlib.sha256(manifest_bytes).hexdigest()[:24]
    complete_members = {
        **material_members,
        "manifest.json": manifest_bytes,
    }
    encoded_members = {
        name: base64.b64encode(payload).decode("ascii")
        for name, payload in complete_members.items()
    }
    return PortableReportBundle(
        report_id=report.report_id,
        members=encoded_members,
        manifest=manifest,
        bundle_id=bundle_id,
        _construction_token=_BUNDLE_TOKEN,
    )


def _render_index_bytes(
    report_bytes: bytes,
    report_id: str,
    watermark_label: str,
    assets: Mapping[str, RendererAsset],
) -> bytes:
    template = assets["report.html"].bytes_payload.decode("utf-8")
    replacements = {
        "@@CSP@@": html.escape(_CSP, quote=True),
        "@@CSS_INTEGRITY@@": _sri(assets["report.css"].bytes_payload),
        "@@JS_INTEGRITY@@": _sri(assets["report.js"].bytes_payload),
        "@@REPORT_DATA@@": _escape_embedded_json(report_bytes.decode("ascii")),
        "@@REPORT_ID@@": html.escape(report_id, quote=True),
        "@@WATERMARK@@": html.escape(watermark_label, quote=False),
    }
    template_without_markers = template
    for marker in replacements:
        if template.count(marker) != 1:
            raise ValueError(f"portable report template marker changed: {marker}")
        template_without_markers = template_without_markers.replace(marker, "")
    if "@@" in template_without_markers:
        raise ValueError("portable report template contains an unknown marker")
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered.encode("utf-8")


def _embedded_report(index_bytes: bytes) -> tuple[dict[str, object], bytes]:
    try:
        index = index_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("portable report index is not UTF-8") from error
    if index.count(_REPORT_DATA_OPEN) != 1:
        raise ValueError("portable report data element inventory changed")
    prefix, remainder = index.split(_REPORT_DATA_OPEN, 1)
    del prefix
    if remainder.count(_REPORT_DATA_CLOSE) != 2:
        raise ValueError("portable report script inventory changed")
    report_text, _suffix = remainder.split(_REPORT_DATA_CLOSE, 1)
    try:
        parsed = json.loads(report_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("portable report embedded data is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("portable report embedded data is not an object")
    canonical = _canonical_json_bytes(parsed)
    return parsed, canonical


def _validated_report_payload(
    report: dict[str, object],
) -> tuple[dict[str, object], str, str]:
    identity_fields = {
        "frames",
        "renderer_assets",
        "renderer_id",
        "renderer_version",
        "schema_id",
        "schema_version",
        "sections",
    }
    if set(report) != identity_fields | {"nonidentity_metadata", "report_id"}:
        raise ValueError("portable report payload fields changed")
    if (
        report["schema_id"] != PORTABLE_REPLAY_REPORT_SCHEMA_ID
        or report["schema_version"] != PORTABLE_REPLAY_REPORT_SCHEMA_VERSION
        or report["renderer_id"] != OFFLINE_RENDERER_ID
        or report["renderer_version"] != OFFLINE_RENDERER_VERSION
    ):
        raise ValueError("portable report payload schema or renderer is invalid")
    expected_assets = [
        item.as_dict() for item in load_installed_renderer_assets()
    ]
    if report["renderer_assets"] != expected_assets:
        raise ValueError("portable report payload asset inventory is unpinned")
    nonidentity = report["nonidentity_metadata"]
    if not isinstance(nonidentity, dict) or set(nonidentity) != {
        "display_generated_at",
        "identity_exclusion",
    } or nonidentity["identity_exclusion"] != "DISPLAY_GENERATED_AT":
        raise ValueError("portable report nonidentity metadata is invalid")
    sections = report["sections"]
    if not isinstance(sections, list) or [
        item.get("kind") if isinstance(item, dict) else None
        for item in sections
    ] != [item.value for item in REPORT_SECTION_ORDER]:
        raise ValueError("portable report section inventory changed")
    frames = report["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("portable report payload lacks frames")
    frame_ids: set[str] = set()
    roots: set[bytes] = set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("portable report frame payload is invalid")
        if set(frame) != {
            "cursor",
            "frame_id",
            "identity",
            "overlay_set",
            "pane_snapshot",
            "presentation",
            "schema_id",
            "schema_version",
            "timeline_root",
        }:
            raise ValueError("portable report frame fields changed")
        if (
            frame["schema_id"] != REPLAY_PRESENTATION_FRAME_SCHEMA_ID
            or frame["schema_version"]
            != REPLAY_PRESENTATION_FRAME_SCHEMA_VERSION
        ):
            raise ValueError("portable report frame schema is invalid")
        identity_value = frame["identity"]
        if not isinstance(identity_value, dict) or set(identity_value) != {
            "action_time_us",
            "cursor_id",
            "observation_mode",
            "observed_projection_sha256",
            "overlay_set_id",
            "playback_state",
            "policy_id",
            "query_id",
            "render_cursor_time_us",
            "requested_reveal_capabilities",
            "reveal_availability",
            "reveal_evidence_sha256",
            "reveal_unavailable_reason",
            "snapshot_id",
            "source_event_sha256",
            "source_run_id",
            "timeline_id",
        }:
            raise ValueError("portable report frame identity fields changed")
        presentation_value = frame["presentation"]
        if not isinstance(presentation_value, dict):
            raise ValueError("portable report frame presentation is invalid")
        metadata_authority = presentation_value.get("metadata_authority")
        if not isinstance(metadata_authority, dict) or metadata_authority != {
            "authority": (
                PresentationMetadataAuthority.SOURCE_BOUND_DISPLAY_DECLARATION.value
            ),
            "evidence_classification": "PRESENTATION_ONLY_NOT_MARKET_EVIDENCE",
            "source_event_sha256": identity_value["source_event_sha256"],
            "source_run_id": identity_value["source_run_id"],
        }:
            raise ValueError("portable report presentation authority is invalid")
        frame_identity = dict(frame)
        frame_id = frame_identity.pop("frame_id")
        expected_frame_id = (
            "replay-presentation-frame-"
            + _canonical_sha256(frame_identity)[:24]
        )
        if frame_id != expected_frame_id or frame_id in frame_ids:
            raise ValueError("portable report frame identity is invalid")
        frame_ids.add(frame_id)
        roots.add(_canonical_json_bytes(_serialized_frame_authority(frame)))
    ordered_frames = sorted(
        frames,
        key=lambda item: (
            item["identity"]["render_cursor_time_us"],
            item["frame_id"],
        ),
    )
    if frames != ordered_frames:
        raise ValueError("portable report frame order is noncanonical")
    if len(roots) != 1:
        raise ValueError("portable report payload spans multiple authorities")
    identity = {key: report[key] for key in sorted(identity_fields)}
    expected_report_id = (
        "portable-replay-report-" + _canonical_sha256(identity)[:24]
    )
    if report["report_id"] != expected_report_id:
        raise ValueError("portable report identity is invalid")
    _reject_forbidden_serialized_material(report)
    modes = {
        frame["identity"]["observation_mode"]
        for frame in frames
    }
    if modes == {ObservationMode.AS_OBSERVED.value}:
        _validate_observed_only_payload(report)
    first = frames[0]
    watermark = first["presentation"]["watermark"]
    if not isinstance(watermark, dict) or type(watermark.get("label")) is not str:
        raise ValueError("portable report watermark is invalid")
    return identity, expected_report_id, watermark["label"]


def _serialized_frame_authority(frame: Mapping[str, object]) -> dict[str, object]:
    identity = frame.get("identity")
    presentation = frame.get("presentation")
    if not isinstance(identity, Mapping) or not isinstance(presentation, Mapping):
        raise ValueError("portable report frame authority is invalid")
    clock_value = presentation.get("clock")
    if not isinstance(clock_value, Mapping):
        raise ValueError("portable report frame clock is invalid")
    clock = dict(clock_value)
    clock.pop("cursor_label", None)
    keys = (
        "observation_mode",
        "policy_id",
        "requested_reveal_capabilities",
        "reveal_availability",
        "reveal_evidence_sha256",
        "reveal_unavailable_reason",
        "source_event_sha256",
        "source_run_id",
    )
    try:
        authority = {key: identity[key] for key in keys}
        authority.update(
            {
                "clock": clock,
                "instrument": presentation["instrument"],
                "limitations": presentation["limitations"],
                "metadata_authority": presentation["metadata_authority"],
                "recording": presentation["recording"],
                "report": presentation["report"],
                "watermark": presentation["watermark"],
            }
        )
    except KeyError as error:
        raise ValueError("portable report frame authority field is missing") from error
    return authority


def _validate_bundle_members(
    members: Mapping[str, bytes],
    *,
    expected_manifest: object | None = None,
    expected_report_id: str | None = None,
    expected_bundle_id: str | None = None,
) -> dict[str, object]:
    if set(members) != set(_COMPLETE_REPORT_MEMBERS) or any(
        type(name) is not str or type(payload) is not bytes
        for name, payload in members.items()
    ):
        raise ValueError("portable report bundle member inventory changed")
    manifest_bytes = members["manifest.json"]
    manifest = _strict_canonical_json(manifest_bytes)
    if expected_manifest is not None and manifest != expected_manifest:
        raise ValueError("portable report manifest object differs from its bytes")
    if set(manifest) != {
        "members",
        "renderer_assets",
        "report_id",
        "report_semantic_sha256",
        "schema_id",
        "schema_version",
    }:
        raise ValueError("portable report manifest fields changed")
    if (
        manifest["schema_id"] != PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID
        or manifest["schema_version"]
        != PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError("portable report bundle schema is invalid")
    rows = manifest["members"]
    if not isinstance(rows, list) or len(rows) != len(_MATERIAL_REPORT_MEMBERS):
        raise ValueError("portable report manifest member inventory changed")
    for expected_name, row in zip(_MATERIAL_REPORT_MEMBERS, rows, strict=True):
        if not isinstance(row, dict) or set(row) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("portable report manifest member row is invalid")
        if row["path"] != expected_name:
            raise ValueError("portable report manifest member order changed")
        payload = members[expected_name]
        if (
            type(row["size_bytes"]) is not int
            or row["size_bytes"] != len(payload)
            or type(row["sha256"]) is not str
            or row["sha256"] != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError(f"portable report member digest mismatch: {expected_name}")
    installed = {
        item.name: item for item in load_installed_renderer_assets()
    }
    if manifest["renderer_assets"] != [
        installed[name].as_dict() for name in sorted(installed)
    ]:
        raise ValueError("portable report manifest asset inventory is unpinned")
    for path, asset_name in (
        ("assets/report.css", "report.css"),
        ("assets/report.js", "report.js"),
    ):
        if members[path] != installed[asset_name].bytes_payload:
            raise ValueError(f"portable report installed asset differs: {asset_name}")
    report, report_bytes = _embedded_report(members["index.html"])
    identity, report_id, watermark = _validated_report_payload(report)
    if manifest["report_id"] != report_id:
        raise ValueError("portable report manifest report identity differs")
    if (
        manifest["report_semantic_sha256"]
        != hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
    ):
        raise ValueError("portable report semantic digest differs")
    expected_index = _render_index_bytes(
        report_bytes,
        report_id,
        watermark,
        installed,
    )
    if members["index.html"] != expected_index:
        raise ValueError("portable report index differs from deterministic rendering")
    bundle_id = (
        "portable-report-bundle-" + hashlib.sha256(manifest_bytes).hexdigest()[:24]
    )
    if expected_report_id is not None and expected_report_id != report_id:
        raise ValueError("portable report bundle report identity differs")
    if expected_bundle_id is not None and expected_bundle_id != bundle_id:
        raise ValueError("portable report bundle identity differs")
    return {
        "bundle_id": bundle_id,
        "member_count": len(members),
        "report_id": report_id,
        "status": "PASS",
    }


def write_portable_report_bundle(
    bundle: PortableReportBundle,
    destination: Path,
) -> Path:
    """Atomically materialize one new explicit directory without overwriting."""

    if type(bundle) is not PortableReportBundle:
        raise TypeError("portable report writer requires PortableReportBundle")
    decoded = {
        name: bundle.member_bytes(name)
        for name in _COMPLETE_REPORT_MEMBERS
    }
    _validate_bundle_members(
        decoded,
        expected_manifest=thaw_json(bundle.manifest),
        expected_report_id=bundle.report_id,
        expected_bundle_id=bundle.bundle_id,
    )
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError("portable report destination must be an absolute Path")
    if destination != destination.resolve(strict=False):
        raise ValueError("portable report destination must already be resolved")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("portable report destination already exists")
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("portable report destination parent is unavailable")
    temporary = Path(tempfile.mkdtemp(prefix=".kirby2-report-", dir=parent))
    try:
        for name in sorted(decoded):
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("portable report bundle contains path traversal")
            target = temporary.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(decoded[name])
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination / "index.html"


def verify_portable_report_bundle(root: Path) -> dict[str, object]:
    """Verify a materialized or relocated report without executing JavaScript."""

    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("portable report verification root must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("portable report verification root is not a plain directory")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("portable report bundle contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(_COMPLETE_REPORT_MEMBERS):
        raise ValueError("portable report bundle member inventory differs")
    members = {
        name: root.joinpath(*PurePosixPath(name).parts).read_bytes()
        for name in _COMPLETE_REPORT_MEMBERS
    }
    return _validate_bundle_members(members)


_FORBIDDEN_SERIALIZED_KEYS = frozenset(
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
        "reveal_authorization",
        "reveal_authorization_id",
        "reveal_authorization_ids",
        "reveal_authorization_sha256",
        "receipt_sha256",
        "root_query_sha256",
        "root_render_cursor_time_us",
        "timeline_receipt",
    }
)

_FORBIDDEN_SERIALIZED_SCHEMA_IDS = frozenset(
    {
        "KIRBY2_MICROSCOPE_OVERLAY_WINDOW_PROJECTION_RECEIPT_V1",
        "KIRBY2_MICROSCOPE_SOURCE_CAPABILITY_MANIFEST_V1",
        "KIRBY2_MICROSCOPE_TIMELINE_RECEIPT_V1",
        "KIRBY2_OBSERVATION_INGESTION_RECEIPT_V1",
    }
)

_REVEAL_SOURCE_VALUES = frozenset(
    {
        EvidenceSourceKind.REVEALED_GROUND_TRUTH.value,
        EvidenceSourceKind.REVEALED_HIDDEN_STATE.value,
        "AUTHORIZED_GROUND_TRUTH",
        "AUTHORIZED_HIDDEN_STATE",
    }
)


def _reject_forbidden_serialized_material(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("portable report mappings require text keys")
            if key in _FORBIDDEN_SERIALIZED_KEYS:
                raise ValueError(f"portable report contains forbidden field at {path}.{key}")
            if key == "schema_id" and child in _FORBIDDEN_SERIALIZED_SCHEMA_IDS:
                raise ValueError(
                    f"portable report contains a backend-only schema at {path}.{key}"
                )
            _reject_forbidden_serialized_material(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_serialized_material(child, f"{path}[{index}]")
        return
    if value is None or type(value) in {bool, int, str}:
        return
    raise TypeError(f"portable report contains non-JSON material at {path}")


def _validate_observed_only_payload(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "reveal_evidence_sha256" and child is not None:
                raise ValueError("AS_OBSERVED frame exposes a reveal evidence digest")
            if key == "requested_reveal_capabilities" and child not in ([], ()):
                raise ValueError("AS_OBSERVED frame exposes a reveal capability request")
            _validate_observed_only_payload(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_observed_only_payload(child, f"{path}[{index}]")
        return
    if type(value) is str and value in _REVEAL_SOURCE_VALUES:
        raise ValueError(f"AS_OBSERVED frame exposes reveal material at {path}")


def _format_scaled_integer(value: int | None, formatter: OverlayFormatter) -> str:
    if type(value) is not int:
        raise TypeError("available overlay display value must be an integer")
    negative = value < 0
    magnitude = abs(value)
    decimal_scale = 10**formatter.decimal_precision
    numerator = magnitude * decimal_scale
    quotient, remainder = divmod(numerator, formatter.display_divisor)
    doubled = remainder * 2
    if doubled > formatter.display_divisor or (
        doubled == formatter.display_divisor and quotient % 2 == 1
    ):
        quotient += 1
    integer, fraction = divmod(quotient, decimal_scale)
    if formatter.decimal_precision:
        fraction_text = f"{fraction:0{formatter.decimal_precision}d}"
        if formatter.trailing_zero_policy is FormatterTrailingZeroPolicy.TRIM:
            fraction_text = fraction_text.rstrip("0")
        number = str(integer) + ("." + fraction_text if fraction_text else "")
    else:
        number = str(integer)
    if negative:
        sign = "−"
    elif formatter.sign_policy is FormatterSignPolicy.ALWAYS:
        sign = "+"
    else:
        sign = ""
    return sign + number + formatter.suffix


def _display_json_value(value: object, unit: str) -> str:
    if type(value) is bool:
        rendered = "true" if value else "false"
    elif type(value) is int:
        rendered = str(value)
    elif type(value) is str:
        rendered = value
    else:
        rendered = _canonical_json_bytes(value).decode("ascii")
    return rendered if unit in {"state", "record", "json"} else f"{rendered} {unit}"


def _json_value_kind(value: object) -> str:
    if type(value) is bool:
        return "BOOLEAN"
    if type(value) is int:
        return "INTEGER"
    if type(value) is str:
        return "TEXT"
    return "CANONICAL_JSON"


def _escape_embedded_json(value: str) -> str:
    return (
        value.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _sri(payload: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _strict_canonical_json(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("portable report manifest must be ASCII JSON") from error

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("portable report manifest contains a duplicate key")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=lambda _: (_ for _ in ()).throw(
                ValueError("portable report manifest cannot contain floats")
            ),
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("portable report manifest cannot contain constants")
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("portable report manifest is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("portable report manifest root must be an object")
    if _canonical_json_bytes(parsed) != payload:
        raise ValueError("portable report manifest is not canonical JSON")
    return parsed


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


__all__ = [
    "ClockPresentation",
    "ClockTimeBasis",
    "DeferredCapabilityKind",
    "DeferredCapabilityStatus",
    "FormatterSignPolicy",
    "FormatterTrailingZeroPolicy",
    "InstrumentPresentation",
    "IntegrityAssessment",
    "MarketClassification",
    "OFFLINE_RENDERER_ID",
    "OFFLINE_RENDERER_VERSION",
    "OVERLAY_FORMATTERS",
    "PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_ID",
    "PORTABLE_REPLAY_REPORT_BUNDLE_SCHEMA_VERSION",
    "PORTABLE_REPLAY_REPORT_SCHEMA_ID",
    "PORTABLE_REPLAY_REPORT_SCHEMA_VERSION",
    "PaneRendererKind",
    "PortableReplayReportV1",
    "PortableReportBundle",
    "PortableReportSection",
    "PresentationMetadataAuthority",
    "PresentationSemanticRole",
    "REPLAY_PRESENTATION_FRAME_SCHEMA_ID",
    "REPLAY_PRESENTATION_FRAME_SCHEMA_VERSION",
    "REPORT_ASSET_LICENSE_ID",
    "REPORT_ASSET_SHA256",
    "REPORT_SECTION_ORDER",
    "RecordingPresentation",
    "ReplayPresentationContext",
    "ReplayPresentationFrameV1",
    "ReplayPresentationMetadataV1",
    "ReportPresentation",
    "ReportSectionAvailability",
    "ReportSectionKind",
    "RendererAsset",
    "build_portable_replay_report",
    "build_replay_presentation_frame",
    "load_installed_renderer_assets",
    "render_portable_report_bundle",
    "verify_portable_report_bundle",
    "write_portable_report_bundle",
]
