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

from kirby2.counterfactual.models import CounterfactualMode
from kirby2.immutable import freeze_json, thaw_json

from .annotations import (
    HUMAN_REVIEW_AUTHORITY,
    REPLAY_ANNOTATION_SCHEMA_ID,
    REPLAY_ANNOTATION_SCHEMA_VERSION,
    REPLAY_BOOKMARK_SCHEMA_ID,
    REPLAY_BOOKMARK_SCHEMA_VERSION,
    REPLAY_SIDECAR_TARGET_SCHEMA_ID,
    REPLAY_SIDECAR_TARGET_SCHEMA_VERSION,
    SOURCE_MUTATION_POLICY,
    TIMING_LIE_REVIEW_PACKET_SCHEMA_ID,
    TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION,
    TIMING_LIE_REVIEW_RESULT_SCHEMA_ID,
    TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION,
    TIMING_LIE_RUBRIC_ORDER,
    TIMING_LIE_RUBRIC_PROMPTS,
    TIMING_LIE_RUBRIC_VERSION,
    ReplayAnnotationV1,
    ReplayBookmarkV1,
    ReplaySidecarTargetV1,
    TimingLieHumanResult,
    TimingLieReviewPacketV1,
    TimingLieReviewResultV1,
    TimingLieTechnicalStatus,
)
from .comparison import (
    BRANCH_COMPARISON_SCHEMA_ID,
    BRANCH_COMPARISON_SCHEMA_VERSION,
    COMPARISON_INTERPRETATION,
    COMPARISON_OVERLAY_ORDER,
    COMPARISON_SERIES_ORDER,
    COMPARISON_EVENT_SCHEMA_ID,
    COMPARISON_EVENT_SCHEMA_VERSION,
    COMPARISON_OVERLAY_SCHEMA_ID,
    COMPARISON_OVERLAY_SCHEMA_VERSION,
    COMPARISON_RUN_INPUT_SCHEMA_ID,
    COMPARISON_RUN_INPUT_SCHEMA_VERSION,
    COMPARISON_SERIES_SCHEMA_ID,
    COMPARISON_SERIES_SCHEMA_VERSION,
    COMPARISON_TRACE_SCHEMA_ID,
    COMPARISON_TRACE_SCHEMA_VERSION,
    BranchComparisonV1,
    ComparisonAvailability,
    ComparisonEvidenceScope,
    ComparisonRecordStatus,
    ComparisonTraceAvailability,
    CounterfactualRngPolicy,
)
from .models import (
    MECHANISTIC_INTERPRETATION,
    TRACE_EDGE_ORDER,
    TRACE_INDEX_SCHEMA_ID,
    TRACE_INDEX_SCHEMA_VERSION,
    TRACE_STAGE_ORDER,
    TraceAvailability,
    TraceLinkStatus,
    TraceUnavailableReason,
)
from .overlays import (
    OVERLAY_KIND_ORDER,
    OverlayAvailability,
    OverlayKind,
    OverlaySet,
    OverlayUnit,
)
from .panes import (
    COUNTERFACTUAL_COMPARISON_BINDING_SCHEMA_ID,
    COUNTERFACTUAL_COMPARISON_BINDING_SCHEMA_VERSION,
    COUNTERFACTUAL_COMPARISON_REFERENCE_SCHEMA_ID,
    COUNTERFACTUAL_COMPARISON_REFERENCE_SCHEMA_VERSION,
    COUNTERFACTUAL_COMPARISON_REPORT_SECTION_KIND,
    PANE_ORDER,
    PaneAvailability,
    PaneKind,
    PaneUnavailableReason,
    QueueTruthAvailability,
    SynchronizedPaneSnapshot,
)
from .policy import ObservationMode, ObservationPolicy, RevealAvailability
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
_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
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
    AVAILABLE = "AVAILABLE"
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
    available_wo36e_capabilities: tuple[DeferredCapabilityKind, ...] = ()

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
        if type(self.available_wo36e_capabilities) is not tuple or any(
            type(item) is not DeferredCapabilityKind
            for item in self.available_wo36e_capabilities
        ):
            raise TypeError(
                "available WO36-E presentation capabilities must be a typed tuple"
            )
        canonical_capabilities = tuple(
            item
            for item in DeferredCapabilityKind
            if item in self.available_wo36e_capabilities
        )
        if canonical_capabilities != self.available_wo36e_capabilities:
            raise ValueError(
                "available WO36-E presentation capabilities are not canonical"
            )
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
            PaneRendererKind.EVIDENCE_CARD,
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
    deferred_rows: list[dict[str, object]] = []
    for item in DeferredCapabilityKind:
        status = (
            DeferredCapabilityStatus.AVAILABLE
            if item in context.available_wo36e_capabilities
            else DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E
        )
        deferred_rows.append(
            {
                "capability": item.value,
                "reason": (
                    None
                    if status is DeferredCapabilityStatus.AVAILABLE
                    else status.value
                ),
                "status": status.value,
            }
        )
    deferred = tuple(deferred_rows)
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
    if (
        pane.pane_kind is PaneKind.COUNTERFACTUAL_COMPARISON
        and pane.availability is not PaneAvailability.AVAILABLE
    ):
        renderer = PaneRendererKind.TYPED_UNAVAILABLE
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
    for comparison_reference in pane.comparison_references:
        reference = comparison_reference.as_dict()
        binding = reference["binding"]
        assert isinstance(binding, dict)
        rows.append(
            {
                "branch_run_id": binding["branch_run_id"],
                "cited_comparison_reference_id": reference["reference_id"],
                "comparison_id": binding["comparison_id"],
                "comparison_sha256": binding["comparison_sha256"],
                "display_order": len(rows),
                "display_text": _canonical_json_bytes(reference).decode("ascii"),
                "label": "selected counterfactual branch",
                "raw_value_kind": "CANONICAL_JSON_REFERENCE",
                "semantic_role": PresentationSemanticRole.NEUTRAL.value,
                "source_event_ids": [],
                "unit": "content_addressed_reference",
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
    bookmarks: tuple[ReplayBookmarkV1, ...] | None = None,
    annotations: tuple[ReplayAnnotationV1, ...] | None = None,
    branch_comparison: BranchComparisonV1 | None = None,
    timing_review_packet: TimingLieReviewPacketV1 | None = None,
    timing_review_result: TimingLieReviewResultV1 | None = None,
    display_generated_at: str | None = None,
) -> PortableReplayReportV1:
    """Build reserved V1 sections around optional immutable WO36-E sidecars.

    ``None`` means that a WO36-E producer was not invoked and retains the exact
    WO36-D deferred representation.  An explicitly supplied empty tuple records
    a completed producer with no bookmark or annotation rows.
    """

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
    _validate_frame_wo36e_capabilities(
        canonical_frames,
        bookmarks_supplied=bookmarks is not None,
        annotations_supplied=(
            annotations is not None or timing_review_packet is not None
        ),
        comparison_supplied=branch_comparison is not None,
    )
    canonical_bookmarks, canonical_annotations = _validate_annotation_sidecars(
        canonical_frames,
        bookmarks,
        annotations,
        timing_review_packet,
        timing_review_result,
    )
    _validate_branch_comparison(canonical_frames, branch_comparison)
    sections = _build_report_sections(
        canonical_frames,
        bookmarks=canonical_bookmarks,
        annotations=canonical_annotations,
        branch_comparison=branch_comparison,
        timing_review_packet=timing_review_packet,
        timing_review_result=timing_review_result,
    )
    return PortableReplayReportV1(
        frames=canonical_frames,
        sections=sections,
        renderer_assets=load_installed_renderer_assets(),
        display_generated_at=display_generated_at,
        _construction_token=_REPORT_TOKEN,
    )


def _validate_frame_wo36e_capabilities(
    frames: tuple[ReplayPresentationFrameV1, ...],
    *,
    bookmarks_supplied: bool,
    annotations_supplied: bool,
    comparison_supplied: bool,
) -> None:
    expected_available = {
        DeferredCapabilityKind.BOOKMARK: bookmarks_supplied,
        DeferredCapabilityKind.ANNOTATION: annotations_supplied,
        DeferredCapabilityKind.COUNTERFACTUAL_SELECTION: comparison_supplied,
        DeferredCapabilityKind.COMPARISON: comparison_supplied,
    }
    expected = [
        {
            "capability": capability.value,
            "reason": (
                None
                if expected_available[capability]
                else DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value
            ),
            "status": (
                DeferredCapabilityStatus.AVAILABLE.value
                if expected_available[capability]
                else DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value
            ),
        }
        for capability in DeferredCapabilityKind
    ]
    if any(
        frame.presentation.as_dict()["deferred_capabilities"] != expected
        for frame in frames
    ):
        raise ValueError(
            "WO36-E frame capability declarations differ from supplied producers"
        )


def _validate_annotation_sidecars(
    frames: tuple[ReplayPresentationFrameV1, ...],
    bookmarks: tuple[ReplayBookmarkV1, ...] | None,
    annotations: tuple[ReplayAnnotationV1, ...] | None,
    timing_review_packet: TimingLieReviewPacketV1 | None,
    timing_review_result: TimingLieReviewResultV1 | None,
) -> tuple[
    tuple[ReplayBookmarkV1, ...] | None,
    tuple[ReplayAnnotationV1, ...] | None,
]:
    if bookmarks is not None and (
        type(bookmarks) is not tuple
        or any(type(item) is not ReplayBookmarkV1 for item in bookmarks)
    ):
        raise TypeError("portable report bookmarks must be an exact tuple")
    if annotations is not None and (
        type(annotations) is not tuple
        or any(type(item) is not ReplayAnnotationV1 for item in annotations)
    ):
        raise TypeError("portable report annotations must be an exact tuple")
    if timing_review_packet is not None and type(
        timing_review_packet
    ) is not TimingLieReviewPacketV1:
        raise TypeError("portable report timing review packet is invalid")
    if timing_review_result is not None and type(
        timing_review_result
    ) is not TimingLieReviewResultV1:
        raise TypeError("portable report timing review result is invalid")
    if (timing_review_packet is None) != (timing_review_result is None):
        raise ValueError("timing review packet and result must travel together")

    canonical_bookmarks = (
        None
        if bookmarks is None
        else tuple(sorted(bookmarks, key=lambda item: item.bookmark_id))
    )
    canonical_annotations = (
        None
        if annotations is None
        else tuple(sorted(annotations, key=lambda item: item.annotation_id))
    )
    if canonical_bookmarks is not None and len(
        {item.bookmark_id for item in canonical_bookmarks}
    ) != len(canonical_bookmarks):
        raise ValueError("portable report contains duplicate bookmarks")
    if canonical_annotations is not None and len(
        {item.annotation_id for item in canonical_annotations}
    ) != len(canonical_annotations):
        raise ValueError("portable report contains duplicate annotations")

    targets = [
        item.target
        for collection in (canonical_bookmarks, canonical_annotations)
        if collection is not None
        for item in collection
    ]
    if timing_review_packet is not None:
        targets.extend(
            target
            for search in timing_review_packet.searches
            for target in search.targets
        )
        if (
            timing_review_result.packet_id != timing_review_packet.packet_id
            or timing_review_result.packet_sha256
            != timing_review_packet.packet_sha256
            or timing_review_result.source_run_id
            != timing_review_packet.source_run_id
            or timing_review_result.source_event_sha256
            != timing_review_packet.source_event_sha256
        ):
            raise ValueError("timing review result is not bound to its packet")
    for target in targets:
        _validate_sidecar_target_against_frames(target, frames)
    return canonical_bookmarks, canonical_annotations


def _validate_branch_comparison(
    frames: tuple[ReplayPresentationFrameV1, ...],
    branch_comparison: BranchComparisonV1 | None,
) -> None:
    serialized_frames = [item.as_dict() for item in frames]
    if branch_comparison is None:
        for frame in serialized_frames:
            _validate_serialized_unavailable_comparison_pane(frame)
        return
    if type(branch_comparison) is not BranchComparisonV1:
        raise TypeError("portable report branch comparison is invalid")
    identity = thaw_json(frames[0].identity)
    expected = (
        identity["source_run_id"],
        identity["source_event_sha256"],
        identity["observation_mode"],
        identity["policy_id"],
    )
    actual = (
        branch_comparison.source_run_id,
        branch_comparison.source_event_sha256,
        branch_comparison.observation_mode.value,
        branch_comparison.policy_id,
    )
    if actual != expected:
        raise ValueError("branch comparison differs from report frame authority")
    required = {
        DeferredCapabilityKind.COUNTERFACTUAL_SELECTION.value,
        DeferredCapabilityKind.COMPARISON.value,
    }
    for frame in frames:
        statuses = {
            item["capability"]: item["status"]
            for item in frame.presentation.as_dict()["deferred_capabilities"]
        }
        if any(
            statuses.get(capability)
            != DeferredCapabilityStatus.AVAILABLE.value
            for capability in required
        ):
            raise ValueError(
                "WO36-E report frames do not declare comparison capabilities"
            )
    _validate_serialized_comparison_pane_bindings(
        serialized_frames,
        branch_comparison.as_dict(),
    )


def _validate_sidecar_target_against_frames(
    target: ReplaySidecarTargetV1,
    frames: tuple[ReplayPresentationFrameV1, ...],
) -> None:
    if type(target) is not ReplaySidecarTargetV1:
        raise TypeError("portable report sidecar target is invalid")
    matches = [
        frame
        for frame in frames
        if thaw_json(frame.identity)["cursor_id"] == target.cursor_id
    ]
    if len(matches) != 1:
        raise ValueError("sidecar target cursor is absent or ambiguous in report")
    frame = matches[0]
    identity = thaw_json(frame.identity)
    snapshot = thaw_json(frame.pane_snapshot)
    cursor = thaw_json(frame.cursor)
    pane = next(
        (
            item
            for item in snapshot["panes"]
            if item["pane_kind"] == target.pane_kind.value
        ),
        None,
    )
    if pane is None:
        raise ValueError("sidecar target pane is absent from its snapshot")
    expected = (
        target.source_run_id,
        target.source_event_sha256,
        target.timeline_id,
        target.cursor_id,
        target.query_id,
        target.observed_projection_sha256,
        target.observation_mode.value,
        target.policy_id,
        target.render_cursor_time_us,
        target.snapshot_id,
        target.pane_availability.value,
        target.pane_sha256,
    )
    actual = (
        identity["source_run_id"],
        identity["source_event_sha256"],
        identity["timeline_id"],
        cursor["cursor_id"],
        snapshot["query_id"],
        identity["observed_projection_sha256"],
        identity["observation_mode"],
        identity["policy_id"],
        identity["render_cursor_time_us"],
        snapshot["snapshot_id"],
        pane["availability"],
        _canonical_sha256(pane),
    )
    if actual != expected:
        raise ValueError("sidecar target differs from its exact report frame")


def _build_report_sections(
    frames: tuple[ReplayPresentationFrameV1, ...],
    *,
    bookmarks: tuple[ReplayBookmarkV1, ...] | None = None,
    annotations: tuple[ReplayAnnotationV1, ...] | None = None,
    branch_comparison: BranchComparisonV1 | None = None,
    timing_review_packet: TimingLieReviewPacketV1 | None = None,
    timing_review_result: TimingLieReviewResultV1 | None = None,
) -> tuple[PortableReportSection, ...]:
    first = frames[0]
    first_identity = thaw_json(first.identity)
    first_presentation = first.presentation.as_dict()
    deferred_payload = {"reason": "NOT_AVAILABLE_UNTIL_WO36_E", "records": []}
    bookmark_availability = (
        ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E
        if bookmarks is None
        else (
            ReportSectionAvailability.AVAILABLE
            if bookmarks
            else ReportSectionAvailability.RECORDED_EMPTY
        )
    )
    annotation_available = bool(annotations) or timing_review_packet is not None
    annotation_availability = (
        ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E
        if annotations is None and timing_review_packet is None
        else (
            ReportSectionAvailability.AVAILABLE
            if annotation_available
            else ReportSectionAvailability.RECORDED_EMPTY
        )
    )
    comparison_availability = (
        ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E
        if branch_comparison is None
        else ReportSectionAvailability.AVAILABLE
    )
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
            bookmark_availability,
            (
                "Immutable bookmarks bound to exact pane, cursor, and snapshot IDs."
                if bookmarks is not None
                else "Immutable bookmark producers are intentionally deferred."
            ),
            (
                deferred_payload
                if bookmarks is None
                else {"records": [item.as_dict() for item in bookmarks]}
            ),
        ),
        (
            ReportSectionKind.ANNOTATIONS,
            "Annotations",
            annotation_availability,
            (
                "Immutable analysis revisions and the separate timing-lie review state."
                if annotations is not None or timing_review_packet is not None
                else "Immutable annotation sidecars are intentionally deferred."
            ),
            (
                deferred_payload
                if annotations is None and timing_review_packet is None
                else {
                    "records": [
                        item.as_dict() for item in (annotations or ())
                    ],
                    "timing_lie_review_packet": (
                        None
                        if timing_review_packet is None
                        else timing_review_packet.as_dict()
                    ),
                    "timing_lie_review_result": (
                        None
                        if timing_review_result is None
                        else timing_review_result.as_dict()
                    ),
                }
            ),
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
            comparison_availability,
            (
                "Exact parent/branch prefix, divergence, suffix, and outcome comparison."
                if branch_comparison is not None
                else (
                    "Counterfactual selection and branch comparison are intentionally "
                    "deferred."
                )
            ),
            (
                deferred_payload
                if branch_comparison is None
                else branch_comparison.as_dict()
            ),
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
    if any(
        set(item) != {"availability", "kind", "payload", "summary", "title"}
        or item["availability"] not in {value.value for value in ReportSectionAvailability}
        or type(item["summary"]) is not str
        or type(item["title"]) is not str
        for item in sections
    ):
        raise ValueError("portable report section fields are invalid")
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
    _validate_serialized_wo36e_sections(sections, frames)
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


def _serialized_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} fields are invalid")
    return value


def _serialized_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _serialized_text(
    value: object,
    label: str,
    *,
    maximum: int = 4096,
    empty: bool = False,
) -> str:
    if type(value) is not str or (not empty and not value):
        raise ValueError(f"{label} is invalid")
    if len(value) > maximum or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} is not bounded NFC text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _serialized_run_id(value: object, label: str) -> str:
    if type(value) is not str or _RUN_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _serialized_content_id(
    value: dict[str, object],
    id_field: str,
    prefix: str,
    label: str,
) -> None:
    identity = dict(value)
    actual = identity.pop(id_field)
    expected = prefix + _canonical_sha256(identity)[:24]
    if actual != expected:
        raise ValueError(f"{label} content-derived ID is invalid")


def _validate_serialized_wo36e_sections(
    sections: list[object],
    frames: list[object],
) -> None:
    by_kind = {
        section["kind"]: section
        for section in sections
        if isinstance(section, dict)
    }
    bookmarks = by_kind[ReportSectionKind.BOOKMARKS.value]
    annotations = by_kind[ReportSectionKind.ANNOTATIONS.value]
    comparison = by_kind[ReportSectionKind.BRANCH_COMPARISON.value]
    _validate_serialized_capability_state(
        frames,
        bookmarks_available=(
            bookmarks["availability"]
            != ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value
        ),
        annotations_available=(
            annotations["availability"]
            != ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value
        ),
        comparison_available=(
            comparison["availability"]
            != ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value
        ),
    )
    _validate_serialized_bookmarks_section(bookmarks, frames)
    _validate_serialized_annotations_section(annotations, frames)
    _validate_serialized_comparison_section(comparison, frames)


def _validate_serialized_capability_state(
    frames: list[object],
    *,
    bookmarks_available: bool,
    annotations_available: bool,
    comparison_available: bool,
) -> None:
    availability = {
        DeferredCapabilityKind.BOOKMARK: bookmarks_available,
        DeferredCapabilityKind.ANNOTATION: annotations_available,
        DeferredCapabilityKind.COUNTERFACTUAL_SELECTION: comparison_available,
        DeferredCapabilityKind.COMPARISON: comparison_available,
    }
    expected = [
        {
            "capability": capability.value,
            "reason": (
                None
                if availability[capability]
                else DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value
            ),
            "status": (
                DeferredCapabilityStatus.AVAILABLE.value
                if availability[capability]
                else DeferredCapabilityStatus.NOT_AVAILABLE_UNTIL_WO36_E.value
            ),
        }
        for capability in DeferredCapabilityKind
    ]
    for frame in frames:
        frame_root = _serialized_object(
            frame,
            {
                "cursor",
                "frame_id",
                "identity",
                "overlay_set",
                "pane_snapshot",
                "presentation",
                "schema_id",
                "schema_version",
                "timeline_root",
            },
            "portable report frame",
        )
        presentation = frame_root["presentation"]
        if not isinstance(presentation, dict) or presentation.get(
            "deferred_capabilities"
        ) != expected:
            raise ValueError(
                "serialized WO36-E capability declarations differ from sections"
            )
        if not comparison_available:
            _validate_serialized_unavailable_comparison_pane(frame_root)


def _deferred_wo36e_payload() -> dict[str, object]:
    return {"reason": "NOT_AVAILABLE_UNTIL_WO36_E", "records": []}


def _validate_serialized_wo36e_section_declaration(
    section: dict[str, object],
    *,
    kind: ReportSectionKind,
    title: str,
    available_summary: str,
    deferred_summary: str,
) -> None:
    deferred = (
        section["availability"]
        == ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value
    )
    if (
        section["kind"] != kind.value
        or section["title"] != title
        or section["summary"]
        != (deferred_summary if deferred else available_summary)
    ):
        raise ValueError(f"serialized {kind.value} section declaration changed")


def _validate_serialized_bookmarks_section(
    section: dict[str, object],
    frames: list[object],
) -> None:
    _validate_serialized_wo36e_section_declaration(
        section,
        kind=ReportSectionKind.BOOKMARKS,
        title="Bookmarks",
        available_summary=(
            "Immutable bookmarks bound to exact pane, cursor, and snapshot IDs."
        ),
        deferred_summary="Immutable bookmark producers are intentionally deferred.",
    )
    availability = section["availability"]
    if availability == ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value:
        if section["payload"] != _deferred_wo36e_payload():
            raise ValueError("deferred bookmark section payload changed")
        return
    if availability not in {
        ReportSectionAvailability.AVAILABLE.value,
        ReportSectionAvailability.RECORDED_EMPTY.value,
    }:
        raise ValueError("bookmark section availability is invalid")
    payload = _serialized_object(
        section["payload"],
        {"records"},
        "bookmark section payload",
    )
    records = _serialized_list(payload["records"], "bookmark records")
    if (availability == ReportSectionAvailability.AVAILABLE.value) != bool(records):
        raise ValueError("bookmark section availability differs from its records")
    ids: list[str] = []
    by_id: dict[str, dict[str, object]] = {}
    for value in records:
        record = _validate_serialized_bookmark(value, frames)
        bookmark_id = record["bookmark_id"]
        assert isinstance(bookmark_id, str)
        ids.append(bookmark_id)
        by_id[bookmark_id] = record
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("bookmark section order or identity is noncanonical")
    _validate_serialized_predecessor_links(
        by_id,
        id_field="bookmark_id",
        predecessor_id_field="predecessor_bookmark_id",
        predecessor_digest_field="predecessor_sha256",
        target_field="target",
        label="bookmark",
    )


def _validate_serialized_bookmark(
    value: object,
    frames: list[object],
) -> dict[str, object]:
    record = _serialized_object(
        value,
        {
            "author_id",
            "bookmark_id",
            "label",
            "predecessor_bookmark_id",
            "predecessor_sha256",
            "revision",
            "schema_id",
            "schema_version",
            "source_mutation_policy",
            "tags",
            "target",
        },
        "bookmark",
    )
    if (
        record["schema_id"] != REPLAY_BOOKMARK_SCHEMA_ID
        or record["schema_version"] != REPLAY_BOOKMARK_SCHEMA_VERSION
        or record["source_mutation_policy"] != SOURCE_MUTATION_POLICY
    ):
        raise ValueError("bookmark schema or source policy is invalid")
    _identifier(record["author_id"], "bookmark author ID")
    _serialized_text(record["label"], "bookmark label", maximum=256)
    _positive_int(record["revision"], "bookmark revision")
    _validate_serialized_tags(record["tags"], "bookmark tags")
    _validate_serialized_predecessor_fields(
        record,
        "predecessor_bookmark_id",
        "predecessor_sha256",
        "bookmark",
    )
    _validate_serialized_sidecar_target(record["target"], frames)
    _serialized_content_id(record, "bookmark_id", "replay-bookmark-", "bookmark")
    return record


def _validate_serialized_annotations_section(
    section: dict[str, object],
    frames: list[object],
) -> None:
    _validate_serialized_wo36e_section_declaration(
        section,
        kind=ReportSectionKind.ANNOTATIONS,
        title="Annotations",
        available_summary=(
            "Immutable analysis revisions and the separate timing-lie review state."
        ),
        deferred_summary="Immutable annotation sidecars are intentionally deferred.",
    )
    availability = section["availability"]
    if availability == ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value:
        if section["payload"] != _deferred_wo36e_payload():
            raise ValueError("deferred annotation section payload changed")
        return
    if availability not in {
        ReportSectionAvailability.AVAILABLE.value,
        ReportSectionAvailability.RECORDED_EMPTY.value,
    }:
        raise ValueError("annotation section availability is invalid")
    payload = _serialized_object(
        section["payload"],
        {"records", "timing_lie_review_packet", "timing_lie_review_result"},
        "annotation section payload",
    )
    records = _serialized_list(payload["records"], "annotation records")
    packet = payload["timing_lie_review_packet"]
    result = payload["timing_lie_review_result"]
    if (packet is None) != (result is None):
        raise ValueError("serialized timing packet and result must travel together")
    has_content = bool(records) or packet is not None
    if (availability == ReportSectionAvailability.AVAILABLE.value) != has_content:
        raise ValueError("annotation section availability differs from its records")
    ids: list[str] = []
    by_id: dict[str, dict[str, object]] = {}
    for value in records:
        record = _validate_serialized_annotation(value, frames)
        annotation_id = record["annotation_id"]
        assert isinstance(annotation_id, str)
        ids.append(annotation_id)
        by_id[annotation_id] = record
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("annotation section order or identity is noncanonical")
    _validate_serialized_predecessor_links(
        by_id,
        id_field="annotation_id",
        predecessor_id_field="predecessor_annotation_id",
        predecessor_digest_field="predecessor_sha256",
        target_field="target",
        label="annotation",
    )
    if packet is not None:
        packet_root = _validate_serialized_timing_packet(packet, frames)
        _validate_serialized_timing_result(result, packet_root)


def _validate_serialized_annotation(
    value: object,
    frames: list[object],
) -> dict[str, object]:
    record = _serialized_object(
        value,
        {
            "annotation_id",
            "author_id",
            "body",
            "kind",
            "predecessor_annotation_id",
            "predecessor_sha256",
            "revision",
            "schema_id",
            "schema_version",
            "source_mutation_policy",
            "tags",
            "target",
        },
        "annotation",
    )
    if (
        record["schema_id"] != REPLAY_ANNOTATION_SCHEMA_ID
        or record["schema_version"] != REPLAY_ANNOTATION_SCHEMA_VERSION
        or record["source_mutation_policy"] != SOURCE_MUTATION_POLICY
        or record["kind"]
        not in {
            "ANALYSIS_NOTE",
            "QUESTION",
            "TIMING_LIE_CANDIDATE",
            "CAUSAL_LANGUAGE_NOTE",
        }
    ):
        raise ValueError("annotation schema, kind, or source policy is invalid")
    _identifier(record["author_id"], "annotation author ID")
    _serialized_text(record["body"], "annotation body", maximum=8192)
    _positive_int(record["revision"], "annotation revision")
    _validate_serialized_tags(record["tags"], "annotation tags")
    _validate_serialized_predecessor_fields(
        record,
        "predecessor_annotation_id",
        "predecessor_sha256",
        "annotation",
    )
    _validate_serialized_sidecar_target(record["target"], frames)
    _serialized_content_id(
        record,
        "annotation_id",
        "replay-annotation-",
        "annotation",
    )
    return record


def _validate_serialized_tags(value: object, label: str) -> None:
    tags = _serialized_list(value, label)
    if any(type(item) is not str for item in tags):
        raise ValueError(f"{label} contains a non-text tag")
    for item in tags:
        _identifier(item, label)
    if tags != sorted(tags, key=lambda item: item.encode("utf-8")) or len(tags) != len(
        set(tags)
    ):
        raise ValueError(f"{label} is noncanonical")


def _validate_serialized_predecessor_fields(
    record: dict[str, object],
    predecessor_id_field: str,
    predecessor_digest_field: str,
    label: str,
) -> None:
    predecessor_id = record[predecessor_id_field]
    predecessor_digest = record[predecessor_digest_field]
    revision = record["revision"]
    if (predecessor_id is None) != (predecessor_digest is None):
        raise ValueError(f"{label} predecessor ID and digest differ")
    if revision == 1:
        if predecessor_id is not None:
            raise ValueError(f"first {label} revision has a predecessor")
    else:
        _identifier(predecessor_id, f"{label} predecessor ID")
        _sha256(predecessor_digest, f"{label} predecessor digest")


def _validate_serialized_predecessor_links(
    records: dict[str, dict[str, object]],
    *,
    id_field: str,
    predecessor_id_field: str,
    predecessor_digest_field: str,
    target_field: str,
    label: str,
) -> None:
    for record in records.values():
        predecessor_id = record[predecessor_id_field]
        if predecessor_id not in records:
            continue
        predecessor = records[predecessor_id]
        if (
            record["revision"] != predecessor["revision"] + 1
            or record[target_field] != predecessor[target_field]
            or record[predecessor_digest_field]
            != hashlib.sha256(_canonical_json_bytes(predecessor)).hexdigest()
            or predecessor[id_field] != predecessor_id
        ):
            raise ValueError(f"serialized {label} predecessor chain is invalid")


def _validate_serialized_sidecar_target(
    value: object,
    frames: list[object],
) -> dict[str, object]:
    target = _serialized_object(
        value,
        {
            "cursor_id",
            "observation_mode",
            "observed_projection_sha256",
            "pane_availability",
            "pane_kind",
            "pane_sha256",
            "policy_id",
            "query_id",
            "render_cursor_time_us",
            "schema_id",
            "schema_version",
            "snapshot_id",
            "source_event_sha256",
            "source_mutation_policy",
            "source_run_id",
            "target_id",
            "timeline_id",
        },
        "replay sidecar target",
    )
    if (
        target["schema_id"] != REPLAY_SIDECAR_TARGET_SCHEMA_ID
        or target["schema_version"] != REPLAY_SIDECAR_TARGET_SCHEMA_VERSION
        or target["source_mutation_policy"] != SOURCE_MUTATION_POLICY
        or target["observation_mode"] not in {item.value for item in ObservationMode}
        or target["pane_kind"] not in {item.value for item in PaneKind}
        or target["pane_availability"] not in {item.value for item in PaneAvailability}
    ):
        raise ValueError("replay sidecar target schema or enum is invalid")
    _serialized_run_id(target["source_run_id"], "sidecar target source run ID")
    _sha256(target["source_event_sha256"], "sidecar target source digest")
    _sha256(
        target["observed_projection_sha256"],
        "sidecar target observed projection digest",
    )
    _sha256(target["pane_sha256"], "sidecar target pane digest")
    for field_name in ("cursor_id", "query_id", "snapshot_id", "timeline_id"):
        _identifier(target[field_name], f"sidecar target {field_name}")
    _nonnegative_int(target["render_cursor_time_us"], "sidecar target cursor")
    mode = ObservationMode(target["observation_mode"])
    if target["policy_id"] != ObservationPolicy(mode).policy_id:
        raise ValueError("sidecar target mode and policy differ")
    _serialized_content_id(
        target,
        "target_id",
        "replay-sidecar-target-",
        "replay sidecar target",
    )
    _validate_serialized_target_against_frames(target, frames)
    return target


def _validate_serialized_target_against_frames(
    target: dict[str, object],
    frames: list[object],
) -> None:
    matches = [
        frame
        for frame in frames
        if isinstance(frame, dict)
        and isinstance(frame.get("identity"), dict)
        and frame["identity"].get("cursor_id") == target["cursor_id"]
    ]
    if len(matches) != 1:
        raise ValueError("serialized sidecar target cursor is absent or ambiguous")
    frame = matches[0]
    identity = frame["identity"]
    cursor = frame["cursor"]
    snapshot = frame["pane_snapshot"]
    if not all(isinstance(item, dict) for item in (identity, cursor, snapshot)):
        raise ValueError("serialized sidecar target frame roots are invalid")
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise ValueError("serialized sidecar target pane inventory is invalid")
    pane_matches = [
        item
        for item in panes
        if isinstance(item, dict) and item.get("pane_kind") == target["pane_kind"]
    ]
    if len(pane_matches) != 1:
        raise ValueError("serialized sidecar target pane is absent or ambiguous")
    pane = pane_matches[0]
    expected = (
        identity.get("source_run_id"),
        identity.get("source_event_sha256"),
        identity.get("timeline_id"),
        cursor.get("cursor_id"),
        snapshot.get("query_id"),
        identity.get("observed_projection_sha256"),
        identity.get("observation_mode"),
        identity.get("policy_id"),
        identity.get("render_cursor_time_us"),
        snapshot.get("snapshot_id"),
        pane.get("availability"),
        _canonical_sha256(pane),
    )
    actual = (
        target["source_run_id"],
        target["source_event_sha256"],
        target["timeline_id"],
        target["cursor_id"],
        target["query_id"],
        target["observed_projection_sha256"],
        target["observation_mode"],
        target["policy_id"],
        target["render_cursor_time_us"],
        target["snapshot_id"],
        target["pane_availability"],
        target["pane_sha256"],
    )
    if actual != expected:
        raise ValueError("serialized sidecar target differs from its exact frame")


def _validate_serialized_timing_packet(
    value: object,
    frames: list[object],
) -> dict[str, object]:
    packet = _serialized_object(
        value,
        {
            "human_result",
            "human_review_authority",
            "observation_mode",
            "packet_id",
            "policy_id",
            "rubric_version",
            "schema_id",
            "schema_version",
            "searches",
            "source_event_sha256",
            "source_mutation_policy",
            "source_run_id",
            "technical_status",
            "timeline_id",
        },
        "timing-lie review packet",
    )
    if (
        packet["schema_id"] != TIMING_LIE_REVIEW_PACKET_SCHEMA_ID
        or packet["schema_version"] != TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION
        or packet["rubric_version"] != TIMING_LIE_RUBRIC_VERSION
        or packet["source_mutation_policy"] != SOURCE_MUTATION_POLICY
        or packet["human_review_authority"] != HUMAN_REVIEW_AUTHORITY
        or packet["technical_status"]
        != TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW.value
        or packet["human_result"] != TimingLieHumanResult.PENDING.value
    ):
        raise ValueError("timing-lie review packet authority or schema is invalid")
    searches = _serialized_list(packet["searches"], "timing-lie searches")
    if len(searches) != len(TIMING_LIE_RUBRIC_ORDER):
        raise ValueError("timing-lie packet search inventory changed")
    authority: tuple[object, ...] | None = None
    for expected_search, value_search in zip(
        TIMING_LIE_RUBRIC_ORDER,
        searches,
        strict=True,
    ):
        search = _serialized_object(
            value_search,
            {
                "human_result",
                "prompt",
                "search",
                "search_id",
                "targets",
                "technical_status",
            },
            "timing-lie search",
        )
        if (
            search["search"] != expected_search.value
            or search["prompt"] != TIMING_LIE_RUBRIC_PROMPTS[expected_search]
            or search["technical_status"]
            != TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW.value
            or search["human_result"] != TimingLieHumanResult.PENDING.value
        ):
            raise ValueError("timing-lie search contract changed")
        targets = _serialized_list(search["targets"], "timing-lie search targets")
        validated = [
            _validate_serialized_sidecar_target(item, frames) for item in targets
        ]
        target_ids = [item["target_id"] for item in validated]
        if (
            not validated
            or target_ids != sorted(target_ids)
            or len(target_ids) != len(set(target_ids))
        ):
            raise ValueError("timing-lie search targets are noncanonical")
        for target in validated:
            target_authority = (
                target["source_run_id"],
                target["source_event_sha256"],
                target["timeline_id"],
                target["observation_mode"],
                target["policy_id"],
            )
            if authority is None:
                authority = target_authority
            elif authority != target_authority:
                raise ValueError("timing-lie targets span multiple authorities")
        _serialized_content_id(
            search,
            "search_id",
            "timing-lie-search-",
            "timing-lie search",
        )
    assert authority is not None
    if (
        packet["source_run_id"],
        packet["source_event_sha256"],
        packet["timeline_id"],
        packet["observation_mode"],
        packet["policy_id"],
    ) != authority:
        raise ValueError("timing-lie packet root differs from its targets")
    _serialized_run_id(packet["source_run_id"], "timing packet source run ID")
    _sha256(packet["source_event_sha256"], "timing packet source digest")
    _identifier(packet["timeline_id"], "timing packet timeline ID")
    _serialized_content_id(
        packet,
        "packet_id",
        "timing-lie-review-packet-",
        "timing-lie review packet",
    )
    return packet


def _validate_serialized_timing_result(
    value: object,
    packet: dict[str, object],
) -> None:
    result = _serialized_object(
        value,
        {
            "human_result",
            "packet_id",
            "packet_sha256",
            "result_id",
            "reviewer_sidecar_id",
            "reviewer_sidecar_sha256",
            "schema_id",
            "schema_version",
            "source_event_sha256",
            "source_mutation_policy",
            "source_run_id",
            "technical_status",
        },
        "timing-lie review result",
    )
    if (
        result["schema_id"] != TIMING_LIE_REVIEW_RESULT_SCHEMA_ID
        or result["schema_version"] != TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION
        or result["source_mutation_policy"] != SOURCE_MUTATION_POLICY
        or result["technical_status"]
        != TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW.value
        or result["human_result"] not in {item.value for item in TimingLieHumanResult}
    ):
        raise ValueError("timing-lie review result schema or status is invalid")
    if (
        result["packet_id"] != packet["packet_id"]
        or result["packet_sha256"]
        != hashlib.sha256(_canonical_json_bytes(packet)).hexdigest()
        or result["source_run_id"] != packet["source_run_id"]
        or result["source_event_sha256"] != packet["source_event_sha256"]
    ):
        raise ValueError("timing-lie review result differs from its packet")
    sidecar_id = result["reviewer_sidecar_id"]
    sidecar_sha256 = result["reviewer_sidecar_sha256"]
    if (sidecar_id is None) != (sidecar_sha256 is None):
        raise ValueError("timing-lie reviewer reference is incomplete")
    if sidecar_id is not None:
        _identifier(sidecar_id, "timing-lie reviewer sidecar ID")
        _sha256(sidecar_sha256, "timing-lie reviewer sidecar digest")
        # A pointer is not reviewer authority.  Until this portable schema carries
        # and verifies the sidecar bytes (and their authority receipt), relocation
        # verification must fail closed instead of laundering a repinned verdict.
        raise ValueError(
            "portable timing-lie result cannot verify an external reviewer sidecar"
        )
    if result["human_result"] != TimingLieHumanResult.PENDING.value:
        raise ValueError("portable timing-lie human result must remain pending")
    _serialized_content_id(
        result,
        "result_id",
        "timing-lie-review-result-",
        "timing-lie review result",
    )


def _serialized_comparison_pane_pair(
    frame: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    identity = frame.get("identity")
    snapshot = frame.get("pane_snapshot")
    presentation = frame.get("presentation")
    if not all(isinstance(item, dict) for item in (identity, snapshot, presentation)):
        raise ValueError("comparison pane frame roots are invalid")
    assert isinstance(identity, dict)
    assert isinstance(snapshot, dict)
    assert isinstance(presentation, dict)
    panes = snapshot.get("panes")
    presentation_panes = presentation.get("panes")
    if not isinstance(panes, list) or not isinstance(presentation_panes, list):
        raise ValueError("comparison pane inventories are invalid")
    snapshot_matches = [
        item
        for item in panes
        if isinstance(item, dict)
        and item.get("pane_kind") == PaneKind.COUNTERFACTUAL_COMPARISON.value
    ]
    presentation_matches = [
        item
        for item in presentation_panes
        if isinstance(item, dict)
        and item.get("pane_kind") == PaneKind.COUNTERFACTUAL_COMPARISON.value
    ]
    if len(snapshot_matches) != 1 or len(presentation_matches) != 1:
        raise ValueError("comparison pane is absent or ambiguous")
    return identity, snapshot, snapshot_matches[0], presentation_matches[0]


def _validate_serialized_comparison_pane_root(
    *,
    identity: dict[str, object],
    snapshot: dict[str, object],
    pane: dict[str, object],
) -> None:
    expected = (
        PaneKind.COUNTERFACTUAL_COMPARISON.value,
        identity.get("observation_mode"),
        identity.get("policy_id"),
        snapshot.get("query_id"),
        identity.get("render_cursor_time_us"),
    )
    actual = (
        pane["pane_kind"],
        pane["observation_mode"],
        pane["policy_id"],
        pane["query_id"],
        pane["render_cursor_time_us"],
    )
    if actual != expected or snapshot.get("query_id") != identity.get("query_id"):
        raise ValueError("comparison pane root differs from its exact frame")


def _validate_serialized_comparison_presentation_root(
    value: object,
    *,
    availability: PaneAvailability,
    renderer: PaneRendererKind,
    explanation: str | None,
    semantic_role: PresentationSemanticRole,
) -> dict[str, object]:
    presentation = _serialized_object(
        value,
        {
            "availability",
            "display_order",
            "explanation",
            "integrity_assessment",
            "market_classification",
            "pane_kind",
            "presentation_schema_id",
            "presentation_schema_version",
            "renderer_kind",
            "rows",
            "semantic_role",
            "source_references",
            "title",
        },
        "counterfactual comparison pane presentation",
    )
    expected = (
        availability.value,
        PANE_ORDER.index(PaneKind.COUNTERFACTUAL_COMPARISON),
        explanation,
        IntegrityAssessment.NOT_ASSESSED.value,
        MarketClassification.NOT_APPLICABLE.value,
        PaneKind.COUNTERFACTUAL_COMPARISON.value,
        "KIRBY2_PANE_PRESENTATION_V1",
        1,
        renderer.value,
        semantic_role.value,
        [],
        _PANE_PRESENTATION[PaneKind.COUNTERFACTUAL_COMPARISON][0],
    )
    actual = (
        presentation["availability"],
        presentation["display_order"],
        presentation["explanation"],
        presentation["integrity_assessment"],
        presentation["market_classification"],
        presentation["pane_kind"],
        presentation["presentation_schema_id"],
        presentation["presentation_schema_version"],
        presentation["renderer_kind"],
        presentation["semantic_role"],
        presentation["source_references"],
        presentation["title"],
    )
    if actual != expected:
        raise ValueError("comparison pane presentation declaration changed")
    return presentation


def _validate_serialized_unavailable_comparison_pane(
    frame: dict[str, object],
) -> None:
    identity, snapshot, value, presentation_value = (
        _serialized_comparison_pane_pair(frame)
    )
    pane = _serialized_object(
        value,
        {
            "availability",
            "data",
            "explanation",
            "observation_mode",
            "pane_kind",
            "policy_id",
            "query_id",
            "queue_estimates",
            "render_cursor_time_us",
        },
        "unavailable counterfactual comparison pane",
    )
    _validate_serialized_comparison_pane_root(
        identity=identity,
        snapshot=snapshot,
        pane=pane,
    )
    expected_explanation = {
        "detail": "no counterfactual branch comparison is selected",
        "reason": PaneUnavailableReason.COUNTERFACTUAL_NOT_SELECTED.value,
    }
    if (
        pane["availability"] != PaneAvailability.UNAVAILABLE.value
        or pane["data"] != []
        or pane["queue_estimates"] != []
        or pane["explanation"] != expected_explanation
    ):
        raise ValueError("unavailable comparison pane payload is invalid")
    presentation = _validate_serialized_comparison_presentation_root(
        presentation_value,
        availability=PaneAvailability.UNAVAILABLE,
        renderer=PaneRendererKind.TYPED_UNAVAILABLE,
        explanation=(
            f"{PaneUnavailableReason.COUNTERFACTUAL_NOT_SELECTED.value}: "
            "no counterfactual branch comparison is selected"
        ),
        semantic_role=PresentationSemanticRole.UNAVAILABLE,
    )
    if presentation["rows"] != []:
        raise ValueError("unavailable comparison pane presentation carries rows")


def _validate_serialized_comparison_section(
    section: dict[str, object],
    frames: list[object],
) -> None:
    _validate_serialized_wo36e_section_declaration(
        section,
        kind=ReportSectionKind.BRANCH_COMPARISON,
        title="Branch comparison",
        available_summary=(
            "Exact parent/branch prefix, divergence, suffix, and outcome comparison."
        ),
        deferred_summary=(
            "Counterfactual selection and branch comparison are intentionally "
            "deferred."
        ),
    )
    availability = section["availability"]
    if availability == ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E.value:
        if section["payload"] != _deferred_wo36e_payload():
            raise ValueError("deferred comparison section payload changed")
        return
    if availability != ReportSectionAvailability.AVAILABLE.value:
        raise ValueError("branch comparison section availability is invalid")
    comparison = _validate_serialized_branch_comparison(section["payload"])
    first = frames[0]
    if not isinstance(first, dict) or not isinstance(first.get("identity"), dict):
        raise ValueError("branch comparison report frame identity is invalid")
    identity = first["identity"]
    expected = (
        identity.get("source_run_id"),
        identity.get("source_event_sha256"),
        identity.get("observation_mode"),
        identity.get("policy_id"),
    )
    actual = (
        comparison["source_run_id"],
        comparison["source_event_sha256"],
        comparison["observation_mode"],
        comparison["policy_id"],
    )
    if actual != expected:
        raise ValueError("serialized branch comparison differs from frame authority")
    _validate_serialized_comparison_pane_bindings(frames, comparison)


def _validate_serialized_comparison_pane_bindings(
    frames: list[object],
    comparison: dict[str, object],
) -> None:
    comparison_sha256 = hashlib.sha256(
        _canonical_json_bytes(comparison)
    ).hexdigest()
    branch_identity = comparison["branch_identity"]
    assert isinstance(branch_identity, dict)
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("comparison pane frame is invalid")
        identity, snapshot, pane_value, presentation_value = (
            _serialized_comparison_pane_pair(frame)
        )
        pane = _serialized_object(
            pane_value,
            {
                "availability",
                "comparison_references",
                "data",
                "explanation",
                "observation_mode",
                "pane_kind",
                "policy_id",
                "query_id",
                "queue_estimates",
                "render_cursor_time_us",
            },
            "counterfactual comparison pane",
        )
        _validate_serialized_comparison_pane_root(
            identity=identity,
            snapshot=snapshot,
            pane=pane,
        )
        references = _serialized_list(
            pane["comparison_references"],
            "counterfactual comparison pane references",
        )
        if (
            pane["availability"] != PaneAvailability.AVAILABLE.value
            or pane["data"] != []
            or pane["queue_estimates"] != []
            or pane["explanation"] is not None
            or len(references) != 1
        ):
            raise ValueError("available comparison pane payload is invalid")
        reference = _serialized_object(
            references[0],
            {
                "binding",
                "observed_projection_sha256",
                "query_id",
                "reference_id",
                "render_cursor_time_us",
                "schema_id",
                "schema_version",
            },
            "counterfactual comparison pane reference",
        )
        if (
            reference["schema_id"]
            != COUNTERFACTUAL_COMPARISON_REFERENCE_SCHEMA_ID
            or reference["schema_version"]
            != COUNTERFACTUAL_COMPARISON_REFERENCE_SCHEMA_VERSION
            or reference["observed_projection_sha256"]
            != identity.get("observed_projection_sha256")
            or reference["query_id"] != snapshot.get("query_id")
            or reference["render_cursor_time_us"]
            != identity.get("render_cursor_time_us")
        ):
            raise ValueError("comparison pane reference differs from its frame")
        _sha256(
            reference["observed_projection_sha256"],
            "comparison pane observed projection digest",
        )
        _identifier(reference["query_id"], "comparison pane query ID")
        _nonnegative_int(
            reference["render_cursor_time_us"],
            "comparison pane render cursor",
        )
        binding = _serialized_object(
            reference["binding"],
            {
                "binding_id",
                "branch_identity_id",
                "branch_run_id",
                "comparison_id",
                "comparison_sha256",
                "observation_mode",
                "policy_id",
                "report_section_kind",
                "schema_id",
                "schema_version",
                "source_event_sha256",
                "source_run_id",
            },
            "counterfactual comparison pane binding",
        )
        expected_binding = (
            comparison["source_run_id"],
            comparison["source_event_sha256"],
            comparison["observation_mode"],
            comparison["policy_id"],
            comparison["comparison_id"],
            comparison_sha256,
            branch_identity["branch_identity_id"],
            branch_identity["branch_run_id"],
        )
        actual_binding = (
            binding["source_run_id"],
            binding["source_event_sha256"],
            binding["observation_mode"],
            binding["policy_id"],
            binding["comparison_id"],
            binding["comparison_sha256"],
            binding["branch_identity_id"],
            binding["branch_run_id"],
        )
        if (
            actual_binding != expected_binding
            or binding["report_section_kind"]
            != COUNTERFACTUAL_COMPARISON_REPORT_SECTION_KIND
            or binding["schema_id"]
            != COUNTERFACTUAL_COMPARISON_BINDING_SCHEMA_ID
            or binding["schema_version"]
            != COUNTERFACTUAL_COMPARISON_BINDING_SCHEMA_VERSION
        ):
            raise ValueError("comparison pane binding differs from report comparison")
        _serialized_content_id(
            binding,
            "binding_id",
            "counterfactual-comparison-binding-",
            "counterfactual comparison pane binding",
        )
        _serialized_content_id(
            reference,
            "reference_id",
            "counterfactual-comparison-reference-",
            "counterfactual comparison pane reference",
        )
        pane_presentation = _validate_serialized_comparison_presentation_root(
            presentation_value,
            availability=PaneAvailability.AVAILABLE,
            renderer=PaneRendererKind.EVIDENCE_CARD,
            explanation=None,
            semantic_role=PresentationSemanticRole.NEUTRAL,
        )
        rows = _serialized_list(
            pane_presentation["rows"],
            "counterfactual comparison presentation rows",
        )
        if len(rows) != 1:
            raise ValueError("comparison pane presentation is invalid")
        row = _serialized_object(
            rows[0],
            {
                "branch_run_id",
                "cited_comparison_reference_id",
                "comparison_id",
                "comparison_sha256",
                "display_order",
                "display_text",
                "label",
                "raw_value_kind",
                "semantic_role",
                "source_event_ids",
                "unit",
            },
            "counterfactual comparison presentation row",
        )
        expected_row = (
            reference["reference_id"],
            comparison["comparison_id"],
            comparison_sha256,
            branch_identity["branch_run_id"],
            0,
            _canonical_json_bytes(reference).decode("ascii"),
            "selected counterfactual branch",
            "CANONICAL_JSON_REFERENCE",
            PresentationSemanticRole.NEUTRAL.value,
            [],
            "content_addressed_reference",
        )
        actual_row = (
            row["cited_comparison_reference_id"],
            row["comparison_id"],
            row["comparison_sha256"],
            row["branch_run_id"],
            row["display_order"],
            row["display_text"],
            row["label"],
            row["raw_value_kind"],
            row["semantic_role"],
            row["source_event_ids"],
            row["unit"],
        )
        if actual_row != expected_row:
            raise ValueError("comparison pane presentation row is unbound")


def _validate_serialized_branch_comparison(
    value: object,
) -> dict[str, object]:
    comparison = _serialized_object(
        value,
        {
            "branch_identity",
            "comparison_id",
            "deltas",
            "first_difference",
            "interpretation",
            "mechanistic_trace",
            "observation_mode",
            "overlays",
            "policy_id",
            "schema_id",
            "schema_version",
            "source_event_sha256",
            "source_run_id",
            "synchronization",
        },
        "branch comparison",
    )
    if (
        comparison["schema_id"] != BRANCH_COMPARISON_SCHEMA_ID
        or comparison["schema_version"] != BRANCH_COMPARISON_SCHEMA_VERSION
        or comparison["interpretation"] != COMPARISON_INTERPRETATION
        or comparison["observation_mode"] not in {item.value for item in ObservationMode}
    ):
        raise ValueError("branch comparison schema, interpretation, or mode is invalid")
    source_run_id = _serialized_run_id(
        comparison["source_run_id"],
        "branch comparison source run ID",
    )
    _sha256(comparison["source_event_sha256"], "branch comparison source digest")
    mode = ObservationMode(comparison["observation_mode"])
    if comparison["policy_id"] != ObservationPolicy(mode).policy_id:
        raise ValueError("branch comparison mode and policy differ")
    synchronization = _validate_serialized_synchronization(
        comparison["synchronization"]
    )
    first = _validate_serialized_first_difference(
        comparison["first_difference"],
        synchronization,
    )
    branch_identity = _validate_serialized_branch_identity(
        comparison["branch_identity"],
        source_run_id=source_run_id,
        source_event_sha256=comparison["source_event_sha256"],
        synchronization=synchronization,
        first_difference=first,
    )
    known_events = _comparison_known_event_sources(synchronization)
    _validate_serialized_comparison_deltas(
        comparison["deltas"],
        known_events,
    )
    _validate_serialized_comparison_input_ids(
        comparison,
        branch_identity=branch_identity,
        synchronization=synchronization,
    )
    _validate_serialized_comparison_overlays(
        comparison["overlays"],
        branch_identity=branch_identity,
        known_events=known_events,
        mode=mode,
    )
    _validate_serialized_comparison_trace(
        comparison["mechanistic_trace"],
        source_run_id=source_run_id,
    )
    _serialized_content_id(
        comparison,
        "comparison_id",
        "branch-comparison-",
        "branch comparison",
    )
    return comparison


def _validate_serialized_comparison_event(
    value: object,
    label: str,
) -> dict[str, object]:
    event = _serialized_object(
        value,
        {
            "event_id",
            "kind",
            "payload",
            "payload_sha256",
            "schema_id",
            "schema_version",
            "sequence",
            "simulation_time_us",
        },
        label,
    )
    if (
        event["schema_id"] != COMPARISON_EVENT_SCHEMA_ID
        or event["schema_version"] != COMPARISON_EVENT_SCHEMA_VERSION
        or not isinstance(event["payload"], dict)
    ):
        raise ValueError(f"{label} schema or payload is invalid")
    _identifier(event["kind"], f"{label} kind")
    _positive_int(event["sequence"], f"{label} sequence")
    _nonnegative_int(event["simulation_time_us"], f"{label} time")
    if event["payload_sha256"] != _canonical_sha256(event["payload"]):
        raise ValueError(f"{label} payload digest is invalid")
    identity = {
        "kind": event["kind"],
        "payload": event["payload"],
        "schema_id": event["schema_id"],
        "schema_version": event["schema_version"],
        "sequence": event["sequence"],
        "simulation_time_us": event["simulation_time_us"],
    }
    expected_id = "comparison-event-" + _canonical_sha256(identity)[:24]
    if event["event_id"] != expected_id:
        raise ValueError(f"{label} content-derived ID is invalid")
    return event


def _comparison_event_semantic(event: dict[str, object]) -> dict[str, object]:
    return {
        "kind": event["kind"],
        "payload": event["payload"],
        "schema_id": event["schema_id"],
        "schema_version": event["schema_version"],
        "sequence": event["sequence"],
        "simulation_time_us": event["simulation_time_us"],
    }


def _validate_serialized_synchronization(
    value: object,
) -> dict[str, object]:
    synchronization = _serialized_object(
        value,
        {
            "branch_suffix",
            "branch_suffix_sha256",
            "parent_suffix",
            "parent_suffix_sha256",
            "prefix_end_time_us",
            "prefix_event_ids",
            "prefix_event_sources",
            "prefix_length",
            "synchronized_prefix_sha256",
        },
        "branch synchronization",
    )
    prefix_ids = _serialized_list(
        synchronization["prefix_event_ids"],
        "synchronized prefix event IDs",
    )
    _positive_int(synchronization["prefix_length"], "synchronized prefix length")
    _nonnegative_int(synchronization["prefix_end_time_us"], "prefix end time")
    if (
        len(prefix_ids) != synchronization["prefix_length"]
        or len(prefix_ids) != len(set(prefix_ids))
    ):
        raise ValueError("synchronized prefix event inventory is invalid")
    for event_id in prefix_ids:
        _identifier(event_id, "synchronized prefix event ID")
    prefix_sources = _serialized_list(
        synchronization["prefix_event_sources"],
        "synchronized prefix event sources",
    )
    if len(prefix_sources) != len(prefix_ids):
        raise ValueError("synchronized prefix source inventory is invalid")
    for expected_id, value_source in zip(prefix_ids, prefix_sources, strict=True):
        source = _serialized_object(
            value_source,
            {"event_id", "payload_sha256"},
            "synchronized prefix event source",
        )
        if source["event_id"] != expected_id:
            raise ValueError("synchronized prefix source order or identity changed")
        _sha256(source["payload_sha256"], "synchronized prefix payload digest")
    _sha256(
        synchronization["synchronized_prefix_sha256"],
        "synchronized prefix digest",
    )
    parent_suffix = [
        _validate_serialized_comparison_event(item, "parent suffix event")
        for item in _serialized_list(
            synchronization["parent_suffix"],
            "parent comparison suffix",
        )
    ]
    branch_suffix = [
        _validate_serialized_comparison_event(item, "branch suffix event")
        for item in _serialized_list(
            synchronization["branch_suffix"],
            "branch comparison suffix",
        )
    ]
    if not parent_suffix and not branch_suffix:
        raise ValueError("branch synchronization has no distinct suffix")
    expected_start = synchronization["prefix_length"] + 1
    for label, suffix in (("parent", parent_suffix), ("branch", branch_suffix)):
        if [item["sequence"] for item in suffix] != list(
            range(expected_start, expected_start + len(suffix))
        ):
            raise ValueError(f"{label} comparison suffix sequence is invalid")
    parent_digest = _canonical_sha256(
        [_comparison_event_semantic(item) for item in parent_suffix]
    )
    branch_digest = _canonical_sha256(
        [_comparison_event_semantic(item) for item in branch_suffix]
    )
    if (
        synchronization["parent_suffix_sha256"] != parent_digest
        or synchronization["branch_suffix_sha256"] != branch_digest
        or parent_digest == branch_digest
    ):
        raise ValueError("branch synchronization suffix digest is invalid")
    return synchronization


def _validate_serialized_first_difference(
    value: object,
    synchronization: dict[str, object],
) -> dict[str, object]:
    first = _serialized_object(
        value,
        {
            "branch",
            "divergence_event_id",
            "divergence_time_us",
            "index",
            "parent",
        },
        "first differing event",
    )
    _nonnegative_int(first["index"], "first differing event index")
    _nonnegative_int(first["divergence_time_us"], "first differing event time")
    if first["index"] != synchronization["prefix_length"]:
        raise ValueError("first differing event index differs from synchronized prefix")
    parent = (
        None
        if first["parent"] is None
        else _validate_serialized_comparison_event(
            first["parent"],
            "first differing parent event",
        )
    )
    branch = (
        None
        if first["branch"] is None
        else _validate_serialized_comparison_event(
            first["branch"],
            "first differing branch event",
        )
    )
    if parent is None and branch is None:
        raise ValueError("first differing event omits both sides")
    parent_suffix = synchronization["parent_suffix"]
    branch_suffix = synchronization["branch_suffix"]
    if parent != (parent_suffix[0] if parent_suffix else None) or branch != (
        branch_suffix[0] if branch_suffix else None
    ):
        raise ValueError("first differing event differs from the suffix heads")
    expected_time = min(
        item["simulation_time_us"] for item in (parent, branch) if item is not None
    )
    if (
        first["divergence_time_us"] != expected_time
        or first["divergence_time_us"] < synchronization["prefix_end_time_us"]
    ):
        raise ValueError("first differing event time is invalid")
    _serialized_content_id(
        first,
        "divergence_event_id",
        "branch-divergence-",
        "first differing event",
    )
    return first


def _validate_serialized_branch_identity(
    value: object,
    *,
    source_run_id: str,
    source_event_sha256: object,
    synchronization: dict[str, object],
    first_difference: dict[str, object],
) -> dict[str, object]:
    identity = _serialized_object(
        value,
        {
            "branch_identity_id",
            "branch_input_id",
            "branch_mode",
            "branch_run_id",
            "branch_source_event_sha256",
            "branch_suffix_sha256",
            "branch_timeline_sha256",
            "divergence_event_id",
            "divergence_time_us",
            "exogenous_reference_path_sha256",
            "fork_time_us",
            "intervention",
            "mutation_manifest_sha256",
            "parent_input_id",
            "parent_prefix_sha256",
            "parent_run_id",
            "parent_source_event_sha256",
            "parent_suffix_sha256",
            "parent_timeline_sha256",
            "rng_policy",
            "snapshot_sha256",
            "synchronized_prefix_sha256",
        },
        "branch identity",
    )
    if identity["branch_mode"] not in {item.value for item in CounterfactualMode} or identity[
        "rng_policy"
    ] not in {item.value for item in CounterfactualRngPolicy}:
        raise ValueError("branch mode or RNG policy is invalid")
    parent_run_id = _serialized_run_id(identity["parent_run_id"], "parent run ID")
    branch_run_id = _serialized_run_id(identity["branch_run_id"], "branch run ID")
    if parent_run_id != source_run_id or parent_run_id == branch_run_id:
        raise ValueError("branch identity parent/branch roots are invalid")
    digest_fields = (
        "branch_source_event_sha256",
        "branch_suffix_sha256",
        "branch_timeline_sha256",
        "mutation_manifest_sha256",
        "parent_prefix_sha256",
        "parent_source_event_sha256",
        "parent_suffix_sha256",
        "parent_timeline_sha256",
        "snapshot_sha256",
        "synchronized_prefix_sha256",
    )
    for field_name in digest_fields:
        _sha256(identity[field_name], f"branch identity {field_name}")
    for field_name in ("parent_input_id", "branch_input_id"):
        _identifier(identity[field_name], f"branch identity {field_name}")
        if not str(identity[field_name]).startswith("comparison-run-input-"):
            raise ValueError("branch identity input ID has an invalid namespace")
    if identity["parent_input_id"] == identity["branch_input_id"]:
        raise ValueError("branch identity input IDs are not distinct")
    _nonnegative_int(identity["fork_time_us"], "branch fork time")
    _nonnegative_int(identity["divergence_time_us"], "branch divergence time")
    if not isinstance(identity["intervention"], dict) or identity[
        "mutation_manifest_sha256"
    ] != _canonical_sha256(identity["intervention"]):
        raise ValueError("branch intervention commitment is invalid")
    if identity["parent_source_event_sha256"] != source_event_sha256:
        raise ValueError("branch identity parent source digest differs")
    expected_sync = (
        synchronization["synchronized_prefix_sha256"],
        synchronization["parent_suffix_sha256"],
        synchronization["branch_suffix_sha256"],
        first_difference["divergence_event_id"],
        first_difference["divergence_time_us"],
    )
    actual_sync = (
        identity["synchronized_prefix_sha256"],
        identity["parent_suffix_sha256"],
        identity["branch_suffix_sha256"],
        identity["divergence_event_id"],
        identity["divergence_time_us"],
    )
    if actual_sync != expected_sync or identity["parent_prefix_sha256"] != expected_sync[0]:
        raise ValueError("branch identity differs from synchronized evidence")
    if identity["divergence_time_us"] < identity["fork_time_us"]:
        raise ValueError("branch divergence precedes the fork")
    if identity["branch_mode"] == CounterfactualMode.EXOGENOUS_REPLAY.value:
        if (
            identity["rng_policy"]
            != CounterfactualRngPolicy.FIXED_EXOGENOUS_PATH.value
            or identity["exogenous_reference_path_sha256"] is None
        ):
            raise ValueError("exogenous branch identity has an invalid RNG contract")
        _sha256(
            identity["exogenous_reference_path_sha256"],
            "exogenous reference path digest",
        )
    elif (
        identity["rng_policy"]
        != CounterfactualRngPolicy.FORK_SNAPSHOT_OWNED_RNG_STATE.value
        or identity["exogenous_reference_path_sha256"] is not None
    ):
        raise ValueError("endogenous branch identity has an invalid RNG contract")
    _serialized_content_id(
        identity,
        "branch_identity_id",
        "branch-identity-",
        "branch identity",
    )
    return identity


def _comparison_known_event_sources(
    synchronization: dict[str, object],
) -> dict[str, dict[str, str]]:
    prefix = {
        source["event_id"]: source["payload_sha256"]
        for source in synchronization["prefix_event_sources"]
    }
    output: dict[str, dict[str, str]] = {}
    for side in ("parent", "branch"):
        sources = dict(prefix)
        for event in synchronization[f"{side}_suffix"]:
            event_id = event["event_id"]
            payload_sha256 = event["payload_sha256"]
            if event_id in sources and sources[event_id] != payload_sha256:
                raise ValueError(
                    f"{side} comparison event ID has conflicting payload commitments"
                )
            sources[event_id] = payload_sha256
        output[side] = sources
    return output


def _validate_serialized_comparison_input_ids(
    comparison: dict[str, object],
    *,
    branch_identity: dict[str, object],
    synchronization: dict[str, object],
) -> None:
    deltas = comparison["deltas"]
    assert isinstance(deltas, list)
    prefix_sources = synchronization["prefix_event_sources"]
    assert isinstance(prefix_sources, list)
    for side in ("parent", "branch"):
        suffix = synchronization[f"{side}_suffix"]
        assert isinstance(suffix, list)
        event_sources = [
            *prefix_sources,
            *[
                {
                    "event_id": event["event_id"],
                    "payload_sha256": event["payload_sha256"],
                }
                for event in suffix
            ],
        ]
        event_ids = [source["event_id"] for source in event_sources]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError(f"{side} comparison run event IDs are duplicated")
        series_sources = [
            {
                "kind": delta["kind"],
                "series_sha256": delta[f"{side}_series_sha256"],
            }
            for delta in deltas
        ]
        commitment = {
            "event_sources": event_sources,
            "observation_mode": comparison["observation_mode"],
            "policy_id": comparison["policy_id"],
            "run_id": branch_identity[f"{side}_run_id"],
            "schema_id": COMPARISON_RUN_INPUT_SCHEMA_ID,
            "schema_version": COMPARISON_RUN_INPUT_SCHEMA_VERSION,
            "series_sources": series_sources,
            "source_event_sha256": branch_identity[
                f"{side}_source_event_sha256"
            ],
            "timeline_sha256": branch_identity[f"{side}_timeline_sha256"],
        }
        expected = "comparison-run-input-" + _canonical_sha256(commitment)[:24]
        if branch_identity[f"{side}_input_id"] != expected:
            raise ValueError(
                f"{side} comparison input ID differs from its portable commitment"
            )


def _validate_serialized_comparison_deltas(
    value: object,
    known_events: dict[str, dict[str, str]],
) -> None:
    deltas = _serialized_list(value, "comparison deltas")
    if [item.get("kind") if isinstance(item, dict) else None for item in deltas] != [
        item.value for item in COMPARISON_SERIES_ORDER
    ]:
        raise ValueError("comparison delta inventory or order changed")
    for delta in deltas:
        assert isinstance(delta, dict)
        _validate_serialized_series_delta(delta, known_events)


def _validate_serialized_series_delta(
    value: object,
    known_events: dict[str, dict[str, str]],
) -> None:
    delta = _serialized_object(
        value,
        {
            "availability",
            "branch_availability",
            "branch_series_sha256",
            "changed",
            "delta_id",
            "kind",
            "parent_availability",
            "parent_series_sha256",
            "records",
            "unavailable_reason",
        },
        "comparison series delta",
    )
    availabilities = {item.value for item in ComparisonAvailability}
    if any(
        delta[field_name] not in availabilities
        for field_name in ("availability", "parent_availability", "branch_availability")
    ):
        raise ValueError("comparison series availability is invalid")
    _sha256(delta["parent_series_sha256"], "parent comparison series digest")
    _sha256(delta["branch_series_sha256"], "branch comparison series digest")
    if type(delta["changed"]) is not bool or delta["changed"] != (
        delta["parent_series_sha256"] != delta["branch_series_sha256"]
    ):
        raise ValueError("comparison series changed flag is invalid")
    records = _serialized_list(delta["records"], "comparison record deltas")
    record_keys: list[str] = []
    parent_records: list[dict[str, object]] = []
    branch_records: list[dict[str, object]] = []
    for value_record in records:
        record = _serialized_object(
            value_record,
            {"branch", "parent", "record_key", "status"},
            "comparison record delta",
        )
        _identifier(record["record_key"], "comparison delta record key")
        parent = (
            None
            if record["parent"] is None
            else _validate_serialized_comparison_record(
                record["parent"],
                known_events["parent"],
            )
        )
        branch = (
            None
            if record["branch"] is None
            else _validate_serialized_comparison_record(
                record["branch"],
                known_events["branch"],
            )
        )
        if parent is None and branch is None:
            raise ValueError("comparison record delta omits both sides")
        if any(
            item is not None and item["record_key"] != record["record_key"]
            for item in (parent, branch)
        ):
            raise ValueError("comparison record key differs from its delta")
        expected_status = (
            ComparisonRecordStatus.BRANCH_ONLY.value
            if parent is None
            else (
                ComparisonRecordStatus.PARENT_ONLY.value
                if branch is None
                else (
                    ComparisonRecordStatus.UNCHANGED.value
                    if parent["record_sha256"] == branch["record_sha256"]
                    else ComparisonRecordStatus.CHANGED.value
                )
            )
        )
        if record["status"] != expected_status:
            raise ValueError("comparison record status differs from its evidence")
        if parent is not None:
            parent_records.append(parent)
        if branch is not None:
            branch_records.append(branch)
        record_keys.append(record["record_key"])
    if record_keys != sorted(record_keys) or len(record_keys) != len(set(record_keys)):
        raise ValueError("comparison record delta order or identity is noncanonical")
    parent_unavailable = (
        delta["parent_availability"] == ComparisonAvailability.UNAVAILABLE.value
    )
    branch_unavailable = (
        delta["branch_availability"] == ComparisonAvailability.UNAVAILABLE.value
    )
    expected_availability = (
        ComparisonAvailability.UNAVAILABLE.value
        if parent_unavailable or branch_unavailable
        else (
            ComparisonAvailability.RECORDED_EMPTY.value
            if not parent_records and not branch_records
            else ComparisonAvailability.AVAILABLE.value
        )
    )
    if delta["availability"] != expected_availability:
        raise ValueError("comparison series delta availability is invalid")
    if expected_availability == ComparisonAvailability.AVAILABLE.value:
        if not records or delta["unavailable_reason"] is not None:
            raise ValueError("available comparison delta payload is invalid")
    elif expected_availability == ComparisonAvailability.RECORDED_EMPTY.value:
        if records or delta["unavailable_reason"] is not None:
            raise ValueError("recorded-empty comparison delta payload is invalid")
    elif records or type(delta["unavailable_reason"]) is not str or not delta[
        "unavailable_reason"
    ]:
        raise ValueError("unavailable comparison delta payload is invalid")
    if not parent_unavailable and not branch_unavailable:
        parent_series = {
            "availability": delta["parent_availability"],
            "kind": delta["kind"],
            "records": parent_records,
            "schema_id": COMPARISON_SERIES_SCHEMA_ID,
            "schema_version": COMPARISON_SERIES_SCHEMA_VERSION,
            "unavailable_reason": None,
        }
        branch_series = {
            "availability": delta["branch_availability"],
            "kind": delta["kind"],
            "records": branch_records,
            "schema_id": COMPARISON_SERIES_SCHEMA_ID,
            "schema_version": COMPARISON_SERIES_SCHEMA_VERSION,
            "unavailable_reason": None,
        }
        if (
            delta["parent_series_sha256"] != _canonical_sha256(parent_series)
            or delta["branch_series_sha256"] != _canonical_sha256(branch_series)
        ):
            raise ValueError("comparison series digest does not verify")
    prefix = str(delta["kind"]).lower().replace("_", "-") + "-comparison-delta-"
    _serialized_content_id(delta, "delta_id", prefix, "comparison series delta")


def _validate_serialized_comparison_record(
    value: object,
    known_events: dict[str, str],
) -> dict[str, object]:
    record = _serialized_object(
        value,
        {
            "calculation_id",
            "calculation_version",
            "record_key",
            "record_sha256",
            "simulation_time_us",
            "source_event_ids",
            "value",
        },
        "comparison record",
    )
    _identifier(record["record_key"], "comparison record key")
    _nonnegative_int(record["simulation_time_us"], "comparison record time")
    if (record["calculation_id"] is None) != (record["calculation_version"] is None):
        raise ValueError("comparison record calculation reference is incomplete")
    if record["calculation_id"] is not None:
        _identifier(record["calculation_id"], "comparison calculation ID")
        _positive_int(record["calculation_version"], "comparison calculation version")
    source_ids = _serialized_list(
        record["source_event_ids"],
        "comparison record source event IDs",
    )
    if (
        not source_ids
        or any(type(item) is not str or item not in known_events for item in source_ids)
        or source_ids != sorted(source_ids)
        or len(source_ids) != len(set(source_ids))
    ):
        raise ValueError("comparison record source event inventory is invalid")
    identity = dict(record)
    actual = identity.pop("record_sha256")
    if actual != _canonical_sha256(identity):
        raise ValueError("comparison record digest is invalid")
    return record


def _validate_serialized_comparison_overlays(
    value: object,
    *,
    branch_identity: dict[str, object],
    known_events: dict[str, dict[str, str]],
    mode: ObservationMode,
) -> None:
    overlays = _serialized_list(value, "comparison overlays")
    if [item.get("kind") if isinstance(item, dict) else None for item in overlays] != [
        item.value for item in COMPARISON_OVERLAY_ORDER
    ]:
        raise ValueError("comparison overlay inventory or order changed")
    for overlay in overlays:
        _validate_serialized_comparison_overlay(
            overlay,
            branch_identity=branch_identity,
            known_events=known_events,
            mode=mode,
        )


def _validate_serialized_comparison_overlay(
    value: object,
    *,
    branch_identity: dict[str, object],
    known_events: dict[str, dict[str, str]],
    mode: ObservationMode,
) -> None:
    overlay = _serialized_object(
        value,
        {
            "availability",
            "branch_run_id",
            "branch_source_event_ids",
            "branch_source_payload_sha256",
            "branch_value",
            "calculation_id",
            "calculation_version",
            "changed",
            "evidence_scope",
            "kind",
            "overlay_delta_id",
            "parent_run_id",
            "parent_source_event_ids",
            "parent_source_payload_sha256",
            "parent_value",
            "policy_grant_verified",
            "schema_id",
            "schema_version",
            "unavailable_reason",
            "unit",
        },
        "comparison overlay",
    )
    if (
        overlay["schema_id"] != COMPARISON_OVERLAY_SCHEMA_ID
        or overlay["schema_version"] != COMPARISON_OVERLAY_SCHEMA_VERSION
        or overlay["availability"] not in {item.value for item in ComparisonAvailability}
        or overlay["evidence_scope"]
        not in {item.value for item in ComparisonEvidenceScope}
        or type(overlay["changed"]) is not bool
        or type(overlay["policy_grant_verified"]) is not bool
    ):
        raise ValueError("comparison overlay schema or enum is invalid")
    reveal = overlay["evidence_scope"] in {
        ComparisonEvidenceScope.POSTMORTEM_TRUTH.value,
        ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE.value,
    }
    if overlay["policy_grant_verified"] != reveal or (
        reveal and mode is not ObservationMode.POSTMORTEM
    ):
        raise ValueError("comparison overlay reveal boundary is invalid")
    if (
        overlay["kind"] == "AGENT_TRUTH"
        and overlay["availability"] == ComparisonAvailability.AVAILABLE.value
        and overlay["evidence_scope"]
        != ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE.value
    ):
        raise ValueError("available agent truth lacks hidden-state authorization")
    if (overlay["calculation_id"] is None) != (overlay["calculation_version"] is None):
        raise ValueError("comparison overlay calculation reference is incomplete")
    if overlay["calculation_id"] is not None:
        _identifier(overlay["calculation_id"], "comparison overlay calculation ID")
        _positive_int(
            overlay["calculation_version"],
            "comparison overlay calculation version",
        )
    if overlay["unit"] is not None:
        _identifier(overlay["unit"], "comparison overlay unit")
    available = overlay["availability"] == ComparisonAvailability.AVAILABLE.value
    source_pairs: dict[str, tuple[list[object], list[object]]] = {}
    for side in ("parent", "branch"):
        ids = _serialized_list(
            overlay[f"{side}_source_event_ids"],
            f"{side} comparison overlay source IDs",
        )
        digests = _serialized_list(
            overlay[f"{side}_source_payload_sha256"],
            f"{side} comparison overlay source digests",
        )
        if len(ids) != len(digests) or len(ids) != len(set(ids)):
            raise ValueError("comparison overlay source pair inventory is invalid")
        pairs = list(zip(ids, digests, strict=True))
        if pairs != sorted(pairs):
            raise ValueError("comparison overlay source pairs are noncanonical")
        for event_id, digest in pairs:
            _identifier(event_id, "comparison overlay source event ID")
            _sha256(digest, "comparison overlay source payload digest")
            if (
                event_id not in known_events[side]
                or known_events[side][event_id] != digest
            ):
                raise ValueError("comparison overlay source binding is invalid")
        source_pairs[side] = (ids, digests)
    if available:
        if (
            overlay["parent_value"] is None
            or overlay["branch_value"] is None
            or overlay["unit"] is None
            or overlay["calculation_id"] is None
            or overlay["parent_run_id"] != branch_identity["parent_run_id"]
            or overlay["branch_run_id"] != branch_identity["branch_run_id"]
            or not source_pairs["parent"][0]
            or not source_pairs["branch"][0]
            or overlay["unavailable_reason"] is not None
        ):
            raise ValueError("available comparison overlay payload is invalid")
    elif (
        overlay["parent_value"] is not None
        or overlay["branch_value"] is not None
        or overlay["parent_run_id"] is not None
        or overlay["branch_run_id"] is not None
        or source_pairs["parent"][0]
        or source_pairs["branch"][0]
    ):
        raise ValueError("non-available comparison overlay carries evidence")
    elif overlay["availability"] == ComparisonAvailability.UNAVAILABLE.value:
        if type(overlay["unavailable_reason"]) is not str or not overlay[
            "unavailable_reason"
        ]:
            raise ValueError("unavailable comparison overlay lacks a reason")
    elif overlay["unavailable_reason"] is not None:
        raise ValueError("recorded-empty comparison overlay carries a reason")
    if overlay["changed"] != (overlay["parent_value"] != overlay["branch_value"]):
        raise ValueError("comparison overlay changed flag is invalid")
    prefix = str(overlay["kind"]).lower().replace("_", "-") + "-comparison-overlay-"
    _serialized_content_id(
        overlay,
        "overlay_delta_id",
        prefix,
        "comparison overlay",
    )


def _validate_serialized_comparison_trace(
    value: object,
    *,
    source_run_id: str,
) -> None:
    trace = _serialized_object(
        value,
        {
            "availability",
            "complete_required",
            "schema_id",
            "schema_version",
            "source_event_sha256",
            "source_run_id",
            "trace_payload",
            "unavailable_reason",
        },
        "comparison trace",
    )
    if (
        trace["schema_id"] != COMPARISON_TRACE_SCHEMA_ID
        or trace["schema_version"] != COMPARISON_TRACE_SCHEMA_VERSION
        or trace["availability"]
        not in {item.value for item in ComparisonTraceAvailability}
        or type(trace["complete_required"]) is not bool
        or trace["source_run_id"] != source_run_id
    ):
        raise ValueError("comparison trace schema, availability, or root is invalid")
    _sha256(trace["source_event_sha256"], "comparison trace source digest")
    if trace["availability"] == ComparisonTraceAvailability.UNAVAILABLE.value:
        if trace["trace_payload"] is not None or type(
            trace["unavailable_reason"]
        ) is not str or not trace["unavailable_reason"]:
            raise ValueError("unavailable comparison trace payload is invalid")
        return
    if trace["unavailable_reason"] is not None:
        raise ValueError("available comparison trace carries an unavailable reason")
    payload = _serialized_object(
        trace["trace_payload"],
        {
            "all_actions_complete",
            "complete_action_ids",
            "incomplete_action_ids",
            "index_id",
            "interpretation",
            "lineage_sha256",
            "traces",
        },
        "comparison trace payload",
    )
    if payload["interpretation"] != MECHANISTIC_INTERPRETATION:
        raise ValueError("comparison trace interpretation is invalid")
    traces = _serialized_list(payload["traces"], "player action traces")
    if not traces:
        raise ValueError("comparison trace payload has no player actions")
    action_ids: list[str] = []
    complete_ids: list[str] = []
    incomplete_ids: list[str] = []
    for player_trace in traces:
        action_id, complete = _validate_serialized_player_action_trace(
            player_trace,
            source_run_id=source_run_id,
        )
        action_ids.append(action_id)
        (complete_ids if complete else incomplete_ids).append(action_id)
    if len(action_ids) != len(set(action_ids)):
        raise ValueError("comparison trace action IDs are duplicated")
    if (
        payload["complete_action_ids"] != complete_ids
        or payload["incomplete_action_ids"] != incomplete_ids
        or payload["all_actions_complete"] != (not incomplete_ids)
        or type(payload["all_actions_complete"]) is not bool
        or (trace["complete_required"] and incomplete_ids)
    ):
        raise ValueError("comparison trace completeness summary is invalid")
    lineage_sha256 = _canonical_sha256(traces)
    if payload["lineage_sha256"] != lineage_sha256:
        raise ValueError("comparison trace lineage digest is invalid")
    index_identity = {
        "interpretation": payload["interpretation"],
        "lineage_sha256": lineage_sha256,
        "schema_id": TRACE_INDEX_SCHEMA_ID,
        "schema_version": TRACE_INDEX_SCHEMA_VERSION,
        "source_event_sha256": trace["source_event_sha256"],
        "source_run_id": source_run_id,
    }
    if payload["index_id"] != "trace-index-" + _canonical_sha256(index_identity)[:24]:
        raise ValueError("comparison trace index ID is invalid")


def _validate_serialized_player_action_trace(
    value: object,
    *,
    source_run_id: str,
) -> tuple[str, bool]:
    trace = _serialized_object(
        value,
        {
            "action_id",
            "complete",
            "edges",
            "nodes",
            "player_input_event_id",
            "unavailable_edge_count",
            "unavailable_node_count",
        },
        "player action trace",
    )
    action_id = trace["action_id"]
    _identifier(action_id, "player action trace ID")
    _identifier(trace["player_input_event_id"], "player action input event ID")
    nodes = _serialized_list(trace["nodes"], "player action trace nodes")
    edges = _serialized_list(trace["edges"], "player action trace edges")
    if len(nodes) != len(TRACE_STAGE_ORDER) or len(edges) != len(TRACE_EDGE_ORDER):
        raise ValueError("player action trace node or edge inventory changed")
    node_availability: list[str] = []
    node_event_ids: list[object] = []
    node_provenance: list[object] = []
    for expected_stage, node in zip(TRACE_STAGE_ORDER, nodes, strict=True):
        availability, event_id, provenance = _validate_serialized_trace_node(
            node,
            expected_stage=expected_stage.value,
            source_run_id=source_run_id,
        )
        node_availability.append(availability)
        node_event_ids.append(event_id)
        node_provenance.append(provenance)
    edge_statuses: list[str] = []
    for index, (expected_kind, edge) in enumerate(
        zip(TRACE_EDGE_ORDER, edges, strict=True)
    ):
        edge_statuses.append(
            _validate_serialized_trace_edge(
                edge,
                expected_kind=expected_kind.value,
                expected_from_stage=TRACE_STAGE_ORDER[index].value,
                expected_to_stage=TRACE_STAGE_ORDER[index + 1].value,
                expected_from_event_id=node_event_ids[index],
                expected_to_event_id=node_event_ids[index + 1],
                expected_provenance=[
                    item
                    for item in (
                        node_provenance[index],
                        node_provenance[index + 1],
                    )
                    if item is not None
                ],
                endpoints_recorded=(
                    node_availability[index] == TraceAvailability.RECORDED.value
                    and node_availability[index + 1]
                    == TraceAvailability.RECORDED.value
                ),
                source_run_id=source_run_id,
            )
        )
    player_index = [item.value for item in TRACE_STAGE_ORDER].index("PLAYER_INPUT")
    if (
        node_availability[player_index] != TraceAvailability.RECORDED.value
        or node_event_ids[player_index] != trace["player_input_event_id"]
    ):
        raise ValueError("player action trace does not bind its input event")
    unavailable_nodes = sum(
        item == TraceAvailability.UNAVAILABLE.value for item in node_availability
    )
    unavailable_edges = sum(
        item == TraceLinkStatus.UNAVAILABLE.value for item in edge_statuses
    )
    complete = unavailable_nodes == 0 and unavailable_edges == 0
    if (
        trace["unavailable_node_count"] != unavailable_nodes
        or trace["unavailable_edge_count"] != unavailable_edges
        or trace["complete"] != complete
        or type(trace["complete"]) is not bool
    ):
        raise ValueError("player action trace completeness fields are invalid")
    return action_id, complete


def _validate_serialized_trace_node(
    value: object,
    *,
    expected_stage: str,
    source_run_id: str,
) -> tuple[str, object, object]:
    node = _serialized_object(
        value,
        {"availability", "provenance", "source_event_id", "stage", "unavailable_reason"},
        "mechanistic trace node",
    )
    if (
        node["stage"] != expected_stage
        or node["availability"] not in {item.value for item in TraceAvailability}
    ):
        raise ValueError("mechanistic trace node stage or availability is invalid")
    if node["availability"] == TraceAvailability.RECORDED.value:
        if (
            node["source_event_id"] is None
            or node["provenance"] is None
            or node["unavailable_reason"] is not None
        ):
            raise ValueError("recorded mechanistic trace node payload is invalid")
        _identifier(node["source_event_id"], "mechanistic trace node event ID")
        _validate_serialized_trace_provenance(
            node["provenance"],
            source_run_id=source_run_id,
        )
    else:
        if node["unavailable_reason"] not in {
            item.value for item in TraceUnavailableReason
        }:
            raise ValueError("unavailable trace node lacks a typed reason")
        if node["provenance"] is not None:
            _validate_serialized_trace_provenance(
                node["provenance"],
                source_run_id=source_run_id,
            )
        if node["source_event_id"] is not None:
            _identifier(node["source_event_id"], "mechanistic trace node event ID")
        missing = node["unavailable_reason"] in {
            TraceUnavailableReason.SOURCE_EVENT_MISSING.value,
            TraceUnavailableReason.AMBIGUOUS_SOURCE_EVENTS.value,
        }
        if missing != (
            node["source_event_id"] is None and node["provenance"] is None
        ):
            raise ValueError("unavailable trace node source shape is invalid")
        if (
            node["unavailable_reason"]
            == TraceUnavailableReason.RECORDED_ARTIFACT_KIND_MISMATCH.value
            and (node["source_event_id"] is None or node["provenance"] is None)
        ):
            raise ValueError("mismatched trace node lacks its recorded source")
    return node["availability"], node["source_event_id"], node["provenance"]


def _validate_serialized_trace_edge(
    value: object,
    *,
    expected_kind: str,
    expected_from_stage: str,
    expected_to_stage: str,
    expected_from_event_id: object,
    expected_to_event_id: object,
    expected_provenance: list[object],
    endpoints_recorded: bool,
    source_run_id: str,
) -> str:
    edge = _serialized_object(
        value,
        {
            "correlation_ids",
            "from_event_id",
            "from_stage",
            "kind",
            "provenance",
            "status",
            "to_event_id",
            "to_stage",
            "unavailable_reason",
        },
        "mechanistic trace edge",
    )
    if (
        edge["kind"] != expected_kind
        or edge["from_stage"] != expected_from_stage
        or edge["to_stage"] != expected_to_stage
        or edge["status"] not in {item.value for item in TraceLinkStatus}
    ):
        raise ValueError("mechanistic trace edge identity or status is invalid")
    correlations = _serialized_list(
        edge["correlation_ids"],
        "mechanistic trace edge correlations",
    )
    if correlations != sorted(correlations) or len(correlations) != len(
        set(correlations)
    ):
        raise ValueError("mechanistic trace edge correlations are noncanonical")
    for correlation in correlations:
        _identifier(correlation, "mechanistic trace edge correlation ID")
    provenance = _serialized_list(
        edge["provenance"],
        "mechanistic trace edge provenance",
    )
    for item in provenance:
        _validate_serialized_trace_provenance(item, source_run_id=source_run_id)
    for endpoint in (edge["from_event_id"], edge["to_event_id"]):
        if endpoint is not None:
            _identifier(endpoint, "mechanistic trace edge endpoint")
    if (
        edge["from_event_id"] != expected_from_event_id
        or edge["to_event_id"] != expected_to_event_id
        or provenance != expected_provenance
    ):
        raise ValueError("mechanistic trace edge differs from its adjacent nodes")
    if edge["status"] == TraceLinkStatus.LINKED.value:
        if (
            not endpoints_recorded
            or edge["from_event_id"] is None
            or edge["to_event_id"] is None
            or not correlations
            or len(provenance) != 2
            or edge["unavailable_reason"] is not None
        ):
            raise ValueError("linked mechanistic trace edge payload is invalid")
        left_sequence = provenance[0]["event_sequence"]
        right_sequence = provenance[1]["event_sequence"]
        if right_sequence <= left_sequence:
            raise ValueError("linked trace edge reverses recorded source order")
    else:
        if edge["unavailable_reason"] not in {
            item.value for item in TraceUnavailableReason
        }:
            raise ValueError("unavailable trace edge lacks a typed reason")
        endpoint_unavailable = not endpoints_recorded
        if endpoint_unavailable != (
            edge["unavailable_reason"]
            == TraceUnavailableReason.ENDPOINT_UNAVAILABLE.value
        ):
            raise ValueError("unavailable trace edge reason differs from its nodes")
    return edge["status"]


def _validate_serialized_trace_provenance(
    value: object,
    *,
    source_run_id: str,
) -> None:
    provenance = _serialized_object(
        value,
        {
            "artifact_name",
            "artifact_sha256",
            "event_sequence",
            "run_id",
            "schema_id",
            "schema_version",
        },
        "mechanistic trace provenance",
    )
    if provenance["run_id"] != source_run_id:
        raise ValueError("mechanistic trace provenance belongs to another run")
    _serialized_run_id(provenance["run_id"], "mechanistic trace provenance run ID")
    _identifier(provenance["artifact_name"], "trace provenance artifact name")
    _identifier(provenance["schema_id"], "trace provenance schema ID")
    _sha256(provenance["artifact_sha256"], "trace provenance artifact digest")
    _positive_int(provenance["schema_version"], "trace provenance schema version")
    _positive_int(provenance["event_sequence"], "trace provenance event sequence")


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
    """Verify a relocated report's internal commitments without running JavaScript.

    Content-derived IDs detect inconsistent or partially rewritten artifacts; they
    do not authenticate a publisher.  Provenance that must distinguish an authorized
    artifact from a coherently reauthored one needs an externally pinned report ID,
    signature, or trusted issuance receipt.
    """

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
