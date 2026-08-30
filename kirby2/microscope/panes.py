"""Closed synchronized replay-pane read models over policy-enforced query results.

This module is deliberately downstream of :mod:`kirby2.microscope.query`.  It
does not accept raw recordings, observed evidence sets, reveal stores, or ingest
artifacts.  A pane snapshot can therefore rearrange or derive only values that
the observation policy has already admitted at one integer render cursor.

The contracts distinguish three states that presentation code must not collapse:

* ``AVAILABLE`` contains at least one source-linked datum;
* ``RECORDED_EMPTY`` is an independently pinned backend source capability with no
  visible event at the cursor; and
* ``UNAVAILABLE`` carries a typed explanation of the missing capability/read
  model.

Queue truth has an additional fail-closed boundary.  It is admitted only from an
authorized reveal value in ``POSTMORTEM`` mode when a query-bound capability read
declares a synthetic source.  Agent activity follows the same reveal-only rule.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum

from kirby2.full_day.models import (
    canonical_json_bytes,
    parse_canonical_json_object,
)
from kirby2.immutable import freeze_json, thaw_json

from .policy import (
    ObservationMode,
    ObservationPolicy,
    ReplaySourceCapabilityManifest as _ReplaySourceCapabilityManifest,
    RevealAuthorization as _RevealAuthorization,
    RevealAvailability,
    RevealCapability,
)
from .query import (
    EvidenceSourceKind,
    ObservationQueryResult,
    QueriedValue,
    RecordDisposition,
)


PANE_SNAPSHOT_SCHEMA_ID = "KIRBY2_SYNCHRONIZED_REPLAY_PANES_V1"
PANE_SNAPSHOT_SCHEMA_VERSION = 1
PANE_CAPABILITY_SCHEMA_ID = "KIRBY2_REPLAY_PANE_CAPABILITY_READ_V1"
PANE_CAPABILITY_SCHEMA_VERSION = 1
PANE_DATUM_SCHEMA_ID = "KIRBY2_REPLAY_PANE_DATUM_V1"
PANE_DATUM_SCHEMA_VERSION = 1
QUEUE_ESTIMATOR_SCHEMA_ID = "KIRBY2_REPLAY_QUEUE_ESTIMATE_V1"
QUEUE_ESTIMATOR_SCHEMA_VERSION = 1
PANE_CAPABILITY_MANIFEST_SCOPE = "SYNCHRONIZED_REPLAY_PANES"
HISTORICAL_PANE_SOURCE_SCHEMA_ID = "KIRBY2_HISTORICAL_REPLAY_SOURCE_V1"
SYNTHETIC_PANE_SOURCE_SCHEMA_ID = "KIRBY2_SYNTHETIC_REPLAY_SOURCE_V1"
PANE_SOURCE_SCHEMA_VERSION = 1

_MAX_CAPABILITY_MANIFEST_BYTES = 64 * 1024

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")


class PaneKind(str, Enum):
    """The fixed WO36-C synchronized pane inventory."""

    LEVEL_2_LADDER = "LEVEL_2_LADDER"
    TIME_AND_SALES = "TIME_AND_SALES"
    DEPTH_HEATMAP = "DEPTH_HEATMAP"
    INDIVIDUAL_QUEUE = "INDIVIDUAL_QUEUE"
    PLAYER_ORDERS = "PLAYER_ORDERS"
    ORDER_STATE_LIFECYCLE = "ORDER_STATE_LIFECYCLE"
    POSITION = "POSITION"
    TRAFFIC_LIGHT = "TRAFFIC_LIGHT"
    STRATEGY_RULE_EVIDENCE = "STRATEGY_RULE_EVIDENCE"
    FEATURE_PROVENANCE = "FEATURE_PROVENANCE"
    AGENT_ACTIVITY = "AGENT_ACTIVITY"
    LATENCY_TIMELINE = "LATENCY_TIMELINE"
    VENUE_QUOTES = "VENUE_QUOTES"
    CONSOLIDATED_QUOTES = "CONSOLIDATED_QUOTES"
    FILLS = "FILLS"
    EXECUTION_METRICS = "EXECUTION_METRICS"
    MECHANISTIC_TRACE = "MECHANISTIC_TRACE"
    COUNTERFACTUAL_COMPARISON = "COUNTERFACTUAL_COMPARISON"


PANE_ORDER = tuple(PaneKind)


class PaneAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    RECORDED_EMPTY = "RECORDED_EMPTY"
    UNAVAILABLE = "UNAVAILABLE"


class PaneUnavailableReason(str, Enum):
    NO_VISIBLE_EVENTS_AT_CURSOR = "NO_VISIBLE_EVENTS_AT_CURSOR"
    LEVEL_2_NOT_RECORDED = "LEVEL_2_NOT_RECORDED"
    TIME_AND_SALES_NOT_RECORDED = "TIME_AND_SALES_NOT_RECORDED"
    DEPTH_HISTORY_NOT_RECORDED = "DEPTH_HISTORY_NOT_RECORDED"
    QUEUE_CAPABILITY_UNAVAILABLE = "QUEUE_CAPABILITY_UNAVAILABLE"
    PLAYER_ORDER_STATE_NOT_RECORDED = "PLAYER_ORDER_STATE_NOT_RECORDED"
    ORDER_LIFECYCLE_NOT_RECORDED = "ORDER_LIFECYCLE_NOT_RECORDED"
    POSITION_NOT_RECORDED = "POSITION_NOT_RECORDED"
    TRAFFIC_LIGHT_NOT_RECORDED = "TRAFFIC_LIGHT_NOT_RECORDED"
    STRATEGY_EVIDENCE_NOT_RECORDED = "STRATEGY_EVIDENCE_NOT_RECORDED"
    FEATURE_PROVENANCE_NOT_RECORDED = "FEATURE_PROVENANCE_NOT_RECORDED"
    AUTHORIZED_REVEAL_REQUIRED = "AUTHORIZED_REVEAL_REQUIRED"
    AGENT_ACTIVITY_NOT_RECORDED = "AGENT_ACTIVITY_NOT_RECORDED"
    LATENCY_EVIDENCE_NOT_VISIBLE = "LATENCY_EVIDENCE_NOT_VISIBLE"
    VENUE_QUOTES_NOT_RECORDED = "VENUE_QUOTES_NOT_RECORDED"
    CONSOLIDATED_QUOTES_NOT_RECORDED = "CONSOLIDATED_QUOTES_NOT_RECORDED"
    FILLS_NOT_RECORDED = "FILLS_NOT_RECORDED"
    EXECUTION_METRICS_NOT_RECORDED = "EXECUTION_METRICS_NOT_RECORDED"
    TRACE_READ_MODEL_UNAVAILABLE = "TRACE_READ_MODEL_UNAVAILABLE"
    COUNTERFACTUAL_NOT_SELECTED = "COUNTERFACTUAL_NOT_SELECTED"


class ReplaySourceClass(str, Enum):
    HISTORICAL = "HISTORICAL"
    SYNTHETIC = "SYNTHETIC"


class PaneCapabilityAuthority(str, Enum):
    LOCAL_NONAUTHORIZING = "LOCAL_NONAUTHORIZING"
    PINNED_BACKEND_MANIFEST = "PINNED_BACKEND_MANIFEST"


class QueueCapability(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    ESTIMATED = "ESTIMATED"
    RECORDED_EXACT = "RECORDED_EXACT"


class QueueTruthAvailability(str, Enum):
    AUTHORIZED_SYNTHETIC_POSTMORTEM = "AUTHORIZED_SYNTHETIC_POSTMORTEM"
    NOT_RECORDED = "NOT_RECORDED"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    SOURCE_NOT_SYNTHETIC = "SOURCE_NOT_SYNTHETIC"


class CalculationKind(str, Enum):
    """Closed calculations performed by this module over authorized values."""

    TIMING_PROJECTION = "TIMING_PROJECTION"
    QUEUE_ESTIMATE_PROJECTION = "QUEUE_ESTIMATE_PROJECTION"


class _PanePayloadContract(str, Enum):
    LEVEL_2 = "LEVEL_2"
    TIME_AND_SALES = "TIME_AND_SALES"
    DEPTH_HEATMAP = "DEPTH_HEATMAP"
    PLAYER_ORDER_STATE = "PLAYER_ORDER_STATE"
    POSITION = "POSITION"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    TRAFFIC_LIGHT = "TRAFFIC_LIGHT"
    STRATEGY_RULE = "STRATEGY_RULE"
    INGESTED_FEATURE = "INGESTED_FEATURE"
    FEATURE = "FEATURE"
    AGENT_ACTIVITY = "AGENT_ACTIVITY"
    VENUE_QUOTE = "VENUE_QUOTE"
    CONSOLIDATED_QUOTE = "CONSOLIDATED_QUOTE"
    CONSOLIDATED_BID = "CONSOLIDATED_BID"
    CONSOLIDATED_ASK = "CONSOLIDATED_ASK"
    FILL = "FILL"
    EXECUTION_METRICS = "EXECUTION_METRICS"
    MECHANISTIC_TRACE = "MECHANISTIC_TRACE"
    QUEUE_ESTIMATE = "QUEUE_ESTIMATE"
    QUEUE_TRUTH = "QUEUE_TRUTH"


@dataclass(frozen=True, slots=True)
class _PaneRouteSpec:
    """One closed series namespace, evidence plane, and payload contract."""

    route_id: str
    payload_contract: _PanePayloadContract
    allowed_source_kinds: frozenset[EvidenceSourceKind]
    panes: tuple[PaneKind, ...] = ()
    exact_series: tuple[str, ...] = ()
    series_prefixes: tuple[str, ...] = ()
    excluded_series: frozenset[str] = frozenset()

    def matches(self, series_id: str) -> bool:
        if series_id in self.excluded_series:
            return False
        if series_id in self.exact_series:
            return True
        return any(
            series_id.startswith(prefix) and len(series_id) > len(prefix)
            for prefix in self.series_prefixes
        )


_REVEAL_SOURCE_KINDS = frozenset(
    {
        EvidenceSourceKind.REVEALED_GROUND_TRUTH,
        EvidenceSourceKind.REVEALED_HIDDEN_STATE,
    }
)

_CLIENT_DELIVERED_ONLY = frozenset({EvidenceSourceKind.CLIENT_DELIVERED})
_DECISION_SNAPSHOT_ONLY = frozenset(
    {EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT}
)
_OBSERVED_PANE_SOURCE_KINDS = _CLIENT_DELIVERED_ONLY | _DECISION_SNAPSHOT_ONLY
_GROUND_TRUTH_ONLY = frozenset({EvidenceSourceKind.REVEALED_GROUND_TRUTH})
_HIDDEN_STATE_ONLY = frozenset({EvidenceSourceKind.REVEALED_HIDDEN_STATE})

_PLAYER_ORDER_STATES = frozenset(
    {
        "ACCEPTED",
        "AUCTION_PENDING",
        "AUCTION_WORKING",
        "CANCELLED",
        "CANCELLED_STP",
        "EXPIRED",
        "FILLED",
        "PARTIALLY_FILLED",
        "PENDING",
        "PENDING_CANCEL",
        "PENDING_NEW",
        "REJECTED",
        "REPLACED",
        "WORKING",
    }
)
_SIGNAL_STATES = frozenset({"GREEN", "RED", "WAIT"})
_AGENT_ACTIVITIES = frozenset({"CANCEL", "SUBMIT"})

# The first-party pane vocabulary is intentionally a closed declarative table.
# Prefixes choose only a namespace; the selected contract still verifies the exact
# evidence plane and payload shape before any payload can become pane data.
_PANE_ROUTE_SPECS = (
    _PaneRouteSpec(
        "level-2",
        _PanePayloadContract.LEVEL_2,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.LEVEL_2_LADDER,),
        series_prefixes=("book.level2.", "level2."),
    ),
    _PaneRouteSpec(
        "time-and-sales",
        _PanePayloadContract.TIME_AND_SALES,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.TIME_AND_SALES,),
        series_prefixes=("tape.", "trade."),
    ),
    _PaneRouteSpec(
        "depth-heatmap",
        _PanePayloadContract.DEPTH_HEATMAP,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.DEPTH_HEATMAP,),
        series_prefixes=("depth.heatmap.",),
    ),
    _PaneRouteSpec(
        "player-order-state",
        _PanePayloadContract.PLAYER_ORDER_STATE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.PLAYER_ORDERS, PaneKind.ORDER_STATE_LIFECYCLE),
        series_prefixes=("order.",),
        excluded_series=frozenset({"order.client-intention"}),
    ),
    _PaneRouteSpec(
        "order-lifecycle",
        _PanePayloadContract.PLAYER_ORDER_STATE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.ORDER_STATE_LIFECYCLE,),
        series_prefixes=("lifecycle.",),
    ),
    _PaneRouteSpec(
        "position",
        _PanePayloadContract.POSITION,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.POSITION,),
        series_prefixes=("position.",),
    ),
    _PaneRouteSpec(
        "strategy-signal",
        _PanePayloadContract.STRATEGY_SIGNAL,
        _DECISION_SNAPSHOT_ONLY,
        (PaneKind.TRAFFIC_LIGHT, PaneKind.STRATEGY_RULE_EVIDENCE),
        exact_series=("strategy.signal",),
    ),
    _PaneRouteSpec(
        "traffic-light",
        _PanePayloadContract.TRAFFIC_LIGHT,
        _DECISION_SNAPSHOT_ONLY,
        (PaneKind.TRAFFIC_LIGHT,),
        series_prefixes=("traffic-light.",),
    ),
    _PaneRouteSpec(
        "strategy-rule",
        _PanePayloadContract.STRATEGY_RULE,
        _DECISION_SNAPSHOT_ONLY,
        (PaneKind.STRATEGY_RULE_EVIDENCE,),
        series_prefixes=("strategy.rule.",),
    ),
    _PaneRouteSpec(
        "ingested-feature",
        _PanePayloadContract.INGESTED_FEATURE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.FEATURE_PROVENANCE,),
        exact_series=("feature.imbalance",),
    ),
    _PaneRouteSpec(
        "feature-provenance",
        _PanePayloadContract.FEATURE,
        _DECISION_SNAPSHOT_ONLY,
        (PaneKind.FEATURE_PROVENANCE,),
        series_prefixes=("feature.",),
        excluded_series=frozenset({"feature.imbalance"}),
    ),
    _PaneRouteSpec(
        "agent-activity",
        _PanePayloadContract.AGENT_ACTIVITY,
        _HIDDEN_STATE_ONLY,
        (PaneKind.AGENT_ACTIVITY,),
        series_prefixes=("agent.",),
    ),
    _PaneRouteSpec(
        "venue-quote",
        _PanePayloadContract.VENUE_QUOTE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.VENUE_QUOTES,),
        series_prefixes=("quote.venue.",),
    ),
    _PaneRouteSpec(
        "consolidated-quote",
        _PanePayloadContract.CONSOLIDATED_QUOTE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.CONSOLIDATED_QUOTES,),
        series_prefixes=("quote.consolidated.",),
    ),
    _PaneRouteSpec(
        "consolidated-bid",
        _PanePayloadContract.CONSOLIDATED_BID,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.CONSOLIDATED_QUOTES,),
        exact_series=("quote.best-bid", "quote.processed-best-bid"),
    ),
    _PaneRouteSpec(
        "consolidated-ask",
        _PanePayloadContract.CONSOLIDATED_ASK,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.CONSOLIDATED_QUOTES,),
        exact_series=("quote.best-ask", "quote.processed-best-ask"),
    ),
    _PaneRouteSpec(
        "fill",
        _PanePayloadContract.FILL,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.FILLS,),
        series_prefixes=("fill.",),
    ),
    _PaneRouteSpec(
        "execution-metrics",
        _PanePayloadContract.EXECUTION_METRICS,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.EXECUTION_METRICS,),
        series_prefixes=("metrics.execution.", "execution.metrics."),
    ),
    _PaneRouteSpec(
        "mechanistic-trace",
        _PanePayloadContract.MECHANISTIC_TRACE,
        _CLIENT_DELIVERED_ONLY,
        (PaneKind.MECHANISTIC_TRACE,),
        series_prefixes=("trace.",),
    ),
    _PaneRouteSpec(
        "queue-estimate",
        _PanePayloadContract.QUEUE_ESTIMATE,
        _CLIENT_DELIVERED_ONLY,
        series_prefixes=("queue.estimate.",),
    ),
    _PaneRouteSpec(
        "queue-truth",
        _PanePayloadContract.QUEUE_TRUTH,
        _GROUND_TRUTH_ONLY,
        series_prefixes=("queue.truth.",),
    ),
)
if (
    len({item.route_id for item in _PANE_ROUTE_SPECS}) != len(_PANE_ROUTE_SPECS)
    or frozenset(item.payload_contract for item in _PANE_ROUTE_SPECS)
    != frozenset(_PanePayloadContract)
    or any(
        not item.allowed_source_kinds
        or not (item.exact_series or item.series_prefixes)
        for item in _PANE_ROUTE_SPECS
    )
):
    raise RuntimeError("pane route contract registry is incomplete or ambiguous")


@dataclass(frozen=True, slots=True)
class PaneSourceEvent:
    """Portable citation to an event already authorized by the query result."""

    event_id: str
    series_id: str
    sequence: int
    source_kind: EvidenceSourceKind
    source_evidence_sha256: str
    payload_sha256: str
    source_event_time_us: int
    policy_visible_at_time_us: int

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "pane source event ID")
        _require_identifier(self.series_id, "pane source series ID")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("pane source sequence must be a positive integer")
        if type(self.source_kind) is not EvidenceSourceKind:
            raise TypeError("pane source kind is invalid")
        _require_sha256(self.source_evidence_sha256, "pane source evidence digest")
        _require_sha256(self.payload_sha256, "pane source payload digest")
        if type(self.source_event_time_us) is not int or self.source_event_time_us < 0:
            raise ValueError("pane source event time must be nonnegative")
        if (
            type(self.policy_visible_at_time_us) is not int
            or self.policy_visible_at_time_us < self.source_event_time_us
        ):
            raise ValueError("pane policy visibility precedes its source event")

    @classmethod
    def from_query_value(cls, value: QueriedValue) -> PaneSourceEvent:
        if type(value) is not QueriedValue:
            raise TypeError("pane source citation requires QueriedValue")
        return cls(
            event_id=value.event_id,
            series_id=value.series_id,
            sequence=value.sequence,
            source_kind=value.source_kind,
            source_evidence_sha256=value.source_evidence_sha256,
            payload_sha256=value.payload_sha256,
            source_event_time_us=value.data_age.source_event_time_us,
            policy_visible_at_time_us=value.data_age.policy_visible_at_time_us,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "payload_sha256": self.payload_sha256,
            "policy_visible_at_time_us": self.policy_visible_at_time_us,
            "sequence": self.sequence,
            "series_id": self.series_id,
            "source_event_time_us": self.source_event_time_us,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_kind": self.source_kind.value,
        }


@dataclass(frozen=True, slots=True)
class DeclaredCalculation:
    calculation_id: str
    calculation_version: int
    kind: CalculationKind
    expression: str
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.calculation_id, "pane calculation ID")
        if type(self.calculation_version) is not int or self.calculation_version <= 0:
            raise ValueError("pane calculation version must be positive")
        if type(self.kind) is not CalculationKind:
            raise TypeError("pane calculation kind is invalid")
        if type(self.expression) is not str or not self.expression:
            raise ValueError("pane calculation expression must be nonempty")
        source_ids = _canonical_identifiers(
            self.source_event_ids,
            "pane calculation source event IDs",
        )
        if not source_ids:
            raise ValueError("pane calculation requires source events")
        object.__setattr__(self, "source_event_ids", source_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "calculation_version": self.calculation_version,
            "expression": self.expression,
            "kind": self.kind.value,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True, slots=True)
class PaneDatum:
    datum_id: str
    label: str
    unit: str
    value: object
    source_events: tuple[PaneSourceEvent, ...]
    calculation: DeclaredCalculation | None = None
    schema_id: str = PANE_DATUM_SCHEMA_ID
    schema_version: int = PANE_DATUM_SCHEMA_VERSION
    datum_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identifier(self.datum_id, "pane datum ID")
        _require_identifier(self.label, "pane datum label")
        _require_identifier(self.unit, "pane datum unit")
        if self.value is None:
            raise ValueError("pane datum value cannot be null")
        frozen = freeze_json(self.value)
        sources = _canonical_source_events(self.source_events)
        if not sources:
            raise ValueError("pane datum requires source-event provenance")
        if self.calculation is not None:
            if type(self.calculation) is not DeclaredCalculation:
                raise TypeError("pane datum calculation is invalid")
            if self.calculation.source_event_ids != tuple(
                sorted(item.event_id for item in sources)
            ):
                raise ValueError(
                    "pane calculation sources differ from datum source citations"
                )
        if (
            self.schema_id != PANE_DATUM_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != PANE_DATUM_SCHEMA_VERSION
        ):
            raise ValueError("unsupported pane datum schema")
        object.__setattr__(self, "value", frozen)
        object.__setattr__(self, "source_events", sources)
        object.__setattr__(self, "datum_sha256", _canonical_sha256(self.identity_dict()))

    def identity_dict(self) -> dict[str, object]:
        return {
            "calculation": (
                None if self.calculation is None else self.calculation.as_dict()
            ),
            "datum_id": self.datum_id,
            "label": self.label,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_events": [item.as_dict() for item in self.source_events],
            "unit": self.unit,
            "value": thaw_json(self.value),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "datum_sha256": self.datum_sha256}


@dataclass(frozen=True, slots=True)
class PaneExplanation:
    reason: PaneUnavailableReason
    detail: str

    def __post_init__(self) -> None:
        if type(self.reason) is not PaneUnavailableReason:
            raise TypeError("pane explanation reason is invalid")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("pane explanation detail must be nonempty")

    def as_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    """One queue estimate with capability, uncertainty, and guarded truth."""

    queue_id: str
    capability: QueueCapability
    estimator_version: str | None
    estimated_quantity_ahead: int
    uncertainty_lower_quantity: int
    uncertainty_upper_quantity: int
    truth_availability: QueueTruthAvailability
    truth_quantity_ahead: int | None
    source_events: tuple[PaneSourceEvent, ...]
    schema_id: str = QUEUE_ESTIMATOR_SCHEMA_ID
    schema_version: int = QUEUE_ESTIMATOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_identifier(self.queue_id, "queue estimate ID")
        if type(self.capability) is not QueueCapability:
            raise TypeError("queue estimate capability is invalid")
        if self.capability is QueueCapability.UNAVAILABLE:
            raise ValueError("unavailable queue capability cannot create an estimate")
        if self.capability is QueueCapability.ESTIMATED:
            _require_identifier(self.estimator_version, "queue estimator version")
        elif self.estimator_version is not None:
            raise ValueError("recorded exact queue state cannot name an estimator")
        for label, quantity in (
            ("estimated quantity", self.estimated_quantity_ahead),
            ("uncertainty lower quantity", self.uncertainty_lower_quantity),
            ("uncertainty upper quantity", self.uncertainty_upper_quantity),
        ):
            if type(quantity) is not int or quantity < 0:
                raise ValueError(f"queue {label} must be a nonnegative integer")
        if not (
            self.uncertainty_lower_quantity
            <= self.estimated_quantity_ahead
            <= self.uncertainty_upper_quantity
        ):
            raise ValueError("queue estimate lies outside its uncertainty interval")
        if (
            self.capability is QueueCapability.RECORDED_EXACT
            and (
                self.uncertainty_lower_quantity != self.estimated_quantity_ahead
                or self.uncertainty_upper_quantity != self.estimated_quantity_ahead
            )
        ):
            raise ValueError("recorded exact queue state cannot carry uncertainty")
        if type(self.truth_availability) is not QueueTruthAvailability:
            raise TypeError("queue truth availability is invalid")
        if (
            self.truth_availability
            is QueueTruthAvailability.AUTHORIZED_SYNTHETIC_POSTMORTEM
        ):
            if type(self.truth_quantity_ahead) is not int or self.truth_quantity_ahead < 0:
                raise ValueError("available queue truth must be nonnegative")
        elif self.truth_quantity_ahead is not None:
            raise ValueError("unavailable queue truth cannot carry a quantity")
        sources = _canonical_source_events(self.source_events)
        if not sources:
            raise ValueError("queue estimate requires source-event provenance")
        if (
            self.schema_id != QUEUE_ESTIMATOR_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != QUEUE_ESTIMATOR_SCHEMA_VERSION
        ):
            raise ValueError("unsupported queue estimate schema")
        object.__setattr__(self, "source_events", sources)

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "estimated_quantity_ahead": self.estimated_quantity_ahead,
            "estimator_version": self.estimator_version,
            "queue_id": self.queue_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_events": [item.as_dict() for item in self.source_events],
            "truth_availability": self.truth_availability.value,
            "truth_quantity_ahead": self.truth_quantity_ahead,
            "uncertainty_lower_quantity": self.uncertainty_lower_quantity,
            "uncertainty_upper_quantity": self.uncertainty_upper_quantity,
        }


@dataclass(frozen=True, slots=True)
class _PaneCapabilityFields:
    source_run_id: str
    source_event_sha256: str
    observed_projection_sha256: str
    query_id: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    supported_panes: tuple[PaneKind, ...]
    source_class: ReplaySourceClass
    source_schema_id: str
    source_schema_version: int
    queue_capability: QueueCapability
    queue_estimator_version: str | None
    authority: PaneCapabilityAuthority
    manifest_sha256: str | None
    reveal_authorization_id: str | None
    reveal_authorization_sha256: str | None
    reveal_evidence_sha256: str | None
    reveal_source_capability_manifest_sha256: str | None

    def __post_init__(self) -> None:
        _require_run_id(self.source_run_id)
        _require_sha256(self.source_event_sha256, "pane capability source digest")
        _require_sha256(
            self.observed_projection_sha256,
            "pane capability observed projection digest",
        )
        _require_identifier(self.query_id, "pane capability query ID")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("pane capability observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("pane capability mode and policy ID differ")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("pane capability cursor must be nonnegative")
        supported = _canonical_pane_kinds(self.supported_panes)
        if type(self.source_class) is not ReplaySourceClass:
            raise TypeError("pane capability source class is invalid")
        expected_source_schema = {
            ReplaySourceClass.HISTORICAL: HISTORICAL_PANE_SOURCE_SCHEMA_ID,
            ReplaySourceClass.SYNTHETIC: SYNTHETIC_PANE_SOURCE_SCHEMA_ID,
        }[self.source_class]
        if self.source_schema_id != expected_source_schema:
            raise ValueError("pane source class differs from its closed source schema")
        if (
            type(self.source_schema_version) is not int
            or self.source_schema_version != PANE_SOURCE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported pane source schema version")
        if type(self.queue_capability) is not QueueCapability:
            raise TypeError("pane queue capability is invalid")
        if self.queue_capability is QueueCapability.ESTIMATED:
            _require_identifier(
                self.queue_estimator_version,
                "pane queue estimator version",
            )
        elif self.queue_estimator_version is not None:
            raise ValueError("non-estimated queue capability cannot name an estimator")
        queue_supported = PaneKind.INDIVIDUAL_QUEUE in supported
        if queue_supported != (
            self.queue_capability is not QueueCapability.UNAVAILABLE
        ):
            raise ValueError("individual queue pane and queue capability differ")
        if type(self.authority) is not PaneCapabilityAuthority:
            raise TypeError("pane capability authority is invalid")
        if self.authority is PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST:
            _require_sha256(self.manifest_sha256, "pane capability manifest digest")
        elif self.manifest_sha256 is not None:
            raise ValueError("local pane capability cannot carry a manifest pin")
        if (
            self.authority is PaneCapabilityAuthority.LOCAL_NONAUTHORIZING
            and self.source_class is not ReplaySourceClass.HISTORICAL
        ):
            raise ValueError("local capability reads cannot classify synthetic sources")
        reveal_bindings = (
            self.reveal_authorization_id,
            self.reveal_authorization_sha256,
            self.reveal_evidence_sha256,
            self.reveal_source_capability_manifest_sha256,
        )
        if self.source_class is ReplaySourceClass.SYNTHETIC:
            _require_identifier(
                self.reveal_authorization_id,
                "pane capability reveal authorization ID",
            )
            _require_sha256(
                self.reveal_authorization_sha256,
                "pane capability reveal authorization digest",
            )
            _require_sha256(
                self.reveal_evidence_sha256,
                "pane capability reveal evidence digest",
            )
            _require_sha256(
                self.reveal_source_capability_manifest_sha256,
                "pane capability reveal source manifest digest",
            )
            if self.observation_mode is not ObservationMode.POSTMORTEM:
                raise ValueError("synthetic pane classification requires postmortem mode")
        elif any(item is not None for item in reveal_bindings):
            raise ValueError("historical pane capability cannot carry reveal bindings")
        object.__setattr__(self, "supported_panes", supported)

    def manifest_dict(self) -> dict[str, object]:
        return {
            "capability_scope": PANE_CAPABILITY_MANIFEST_SCOPE,
            "observation_mode": self.observation_mode.value,
            "observed_projection_sha256": self.observed_projection_sha256,
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "queue_capability": self.queue_capability.value,
            "queue_estimator_version": self.queue_estimator_version,
            "reveal_authorization_id": self.reveal_authorization_id,
            "reveal_authorization_sha256": self.reveal_authorization_sha256,
            "reveal_evidence_sha256": self.reveal_evidence_sha256,
            "reveal_source_capability_manifest_sha256": (
                self.reveal_source_capability_manifest_sha256
            ),
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": PANE_CAPABILITY_SCHEMA_ID,
            "schema_version": PANE_CAPABILITY_SCHEMA_VERSION,
            "source_class": self.source_class.value,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "source_schema_id": self.source_schema_id,
            "source_schema_version": self.source_schema_version,
            "supported_panes": [item.value for item in self.supported_panes],
        }


_CAPABILITY_CONSTRUCTION_TOKEN = object()


class PaneCapabilityRead:
    """Closed query-bound source capability receipt.

    A pinned receipt retains and revalidates exact canonical backend-manifest bytes.
    The local convenience receipt is explicitly non-authorizing and can never label a
    source synthetic or admit queue truth.
    """

    __slots__ = ("__fields", "__manifest_bytes", "__sealed")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("PaneCapabilityRead is closed to subclassing")

    def __init__(
        self,
        fields: _PaneCapabilityFields,
        manifest_bytes: bytes | None,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _CAPABILITY_CONSTRUCTION_TOKEN:
            raise TypeError(
                "PaneCapabilityRead is constructed only by capability verification"
            )
        if type(fields) is not _PaneCapabilityFields:
            raise TypeError("pane capability fields are invalid")
        if manifest_bytes is not None and type(manifest_bytes) is not bytes:
            raise TypeError("pane capability manifest must be exact bytes")
        if (
            fields.authority is PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
        ) != (manifest_bytes is not None):
            raise ValueError("pane capability authority and retained manifest differ")
        object.__setattr__(self, "_PaneCapabilityRead__sealed", False)
        object.__setattr__(self, "_PaneCapabilityRead__fields", fields)
        object.__setattr__(
            self,
            "_PaneCapabilityRead__manifest_bytes",
            None if manifest_bytes is None else bytes(manifest_bytes),
        )
        object.__setattr__(self, "_PaneCapabilityRead__sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_PaneCapabilityRead__sealed", False):
            raise AttributeError("PaneCapabilityRead is immutable")
        object.__setattr__(self, name, value)

    def __reduce__(self) -> object:
        raise TypeError("PaneCapabilityRead cannot be serialized")

    def __repr__(self) -> str:
        return (
            "PaneCapabilityRead("
            f"query_id={self.query_id!r}, authority={self.authority.value!r})"
        )

    @property
    def source_run_id(self) -> str:
        return self.__fields.source_run_id

    @property
    def source_event_sha256(self) -> str:
        return self.__fields.source_event_sha256

    @property
    def observed_projection_sha256(self) -> str:
        return self.__fields.observed_projection_sha256

    @property
    def query_id(self) -> str:
        return self.__fields.query_id

    @property
    def observation_mode(self) -> ObservationMode:
        return self.__fields.observation_mode

    @property
    def policy_id(self) -> str:
        return self.__fields.policy_id

    @property
    def render_cursor_time_us(self) -> int:
        return self.__fields.render_cursor_time_us

    @property
    def supported_panes(self) -> tuple[PaneKind, ...]:
        return self.__fields.supported_panes

    @property
    def source_class(self) -> ReplaySourceClass:
        return self.__fields.source_class

    @property
    def source_schema_id(self) -> str:
        return self.__fields.source_schema_id

    @property
    def source_schema_version(self) -> int:
        return self.__fields.source_schema_version

    @property
    def queue_capability(self) -> QueueCapability:
        return self.__fields.queue_capability

    @property
    def queue_estimator_version(self) -> str | None:
        return self.__fields.queue_estimator_version

    @property
    def authority(self) -> PaneCapabilityAuthority:
        return self.__fields.authority

    @property
    def manifest_sha256(self) -> str | None:
        return self.__fields.manifest_sha256

    @property
    def reveal_authorization_id(self) -> str | None:
        return self.__fields.reveal_authorization_id

    @property
    def reveal_authorization_sha256(self) -> str | None:
        return self.__fields.reveal_authorization_sha256

    @property
    def reveal_evidence_sha256(self) -> str | None:
        return self.__fields.reveal_evidence_sha256

    @property
    def reveal_source_capability_manifest_sha256(self) -> str | None:
        return self.__fields.reveal_source_capability_manifest_sha256

    @property
    def capability_read_id(self) -> str:
        return "pane-capability-" + _canonical_sha256(self.identity_dict())[:24]

    def identity_dict(self) -> dict[str, object]:
        return {
            **self.__fields.manifest_dict(),
            "authority": self.authority.value,
            "manifest_sha256": self.manifest_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "capability_read_id": self.capability_read_id}

    def _revalidate(self) -> _PaneCapabilityFields:
        if self.authority is PaneCapabilityAuthority.LOCAL_NONAUTHORIZING:
            return self.__fields
        raw = self.__manifest_bytes
        manifest_sha256 = self.manifest_sha256
        if raw is None or manifest_sha256 is None:  # pragma: no cover - constructor
            raise RuntimeError("pinned pane capability lost its manifest bytes")
        fields = _parse_capability_manifest(raw, manifest_sha256)
        if fields != self.__fields:
            raise RuntimeError("pinned pane capability changed during revalidation")
        return fields


@dataclass(frozen=True, slots=True)
class ReplayPane:
    pane_kind: PaneKind
    availability: PaneAvailability
    render_cursor_time_us: int
    observation_mode: ObservationMode
    policy_id: str
    query_id: str
    data: tuple[PaneDatum, ...] = ()
    queue_estimates: tuple[QueueEstimate, ...] = ()
    explanation: PaneExplanation | None = None

    def __post_init__(self) -> None:
        if type(self.pane_kind) is not PaneKind:
            raise TypeError("replay pane kind is invalid")
        if type(self.availability) is not PaneAvailability:
            raise TypeError("replay pane availability is invalid")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("replay pane cursor must be nonnegative")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("replay pane observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("replay pane mode and policy ID differ")
        _require_identifier(self.query_id, "replay pane query ID")
        data = _canonical_pane_data(self.data)
        queue_estimates = _canonical_queue_estimates(self.queue_estimates)
        if queue_estimates and self.pane_kind is not PaneKind.INDIVIDUAL_QUEUE:
            raise ValueError("queue estimates belong only to the individual queue pane")
        has_content = bool(data or queue_estimates)
        if self.availability is PaneAvailability.AVAILABLE:
            if not has_content:
                raise ValueError("available replay pane cannot be empty")
            if self.explanation is not None:
                raise ValueError("available replay pane cannot carry an explanation")
        else:
            if has_content:
                raise ValueError("non-available replay pane cannot carry data")
            if type(self.explanation) is not PaneExplanation:
                raise ValueError("non-available replay pane requires an explanation")
            if (
                self.availability is PaneAvailability.RECORDED_EMPTY
                and self.explanation.reason
                is not PaneUnavailableReason.NO_VISIBLE_EVENTS_AT_CURSOR
            ):
                raise ValueError("recorded-empty pane requires its exact typed reason")
            if (
                self.availability is PaneAvailability.UNAVAILABLE
                and self.explanation.reason
                is PaneUnavailableReason.NO_VISIBLE_EVENTS_AT_CURSOR
            ):
                raise ValueError("unavailable pane cannot claim a recorded empty state")
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "queue_estimates", queue_estimates)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "data": [item.as_dict() for item in self.data],
            "explanation": (
                None if self.explanation is None else self.explanation.as_dict()
            ),
            "observation_mode": self.observation_mode.value,
            "pane_kind": self.pane_kind.value,
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "queue_estimates": [item.as_dict() for item in self.queue_estimates],
            "render_cursor_time_us": self.render_cursor_time_us,
        }


_SNAPSHOT_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class SynchronizedPaneSnapshot:
    """One exact cursor/policy/query binding for all eighteen read panes."""

    source_run_id: str
    source_event_sha256: str
    observed_projection_sha256: str
    query_id: str
    observation_mode: ObservationMode
    policy_id: str
    reveal_availability: RevealAvailability
    reveal_evidence_sha256: str | None
    render_cursor_time_us: int
    action_time_us: int | None
    panes: tuple[ReplayPane, ...]
    capability_read_id: str | None = None
    capability_authority: PaneCapabilityAuthority | None = None
    capability_manifest_sha256: str | None = None
    schema_id: str = PANE_SNAPSHOT_SCHEMA_ID
    schema_version: int = PANE_SNAPSHOT_SCHEMA_VERSION
    snapshot_id: str = field(init=False)
    _construction_token: InitVar[object | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("SynchronizedPaneSnapshot is closed to subclassing")

    def __post_init__(self, _construction_token: object | None) -> None:
        if _construction_token is not _SNAPSHOT_CONSTRUCTION_TOKEN:
            raise TypeError(
                "SynchronizedPaneSnapshot is constructed only by the pane builder"
            )
        _require_run_id(self.source_run_id)
        _require_sha256(self.source_event_sha256, "pane snapshot source digest")
        _require_sha256(
            self.observed_projection_sha256,
            "pane snapshot observed projection digest",
        )
        _require_identifier(self.query_id, "pane snapshot query ID")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("pane snapshot observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("pane snapshot mode and policy ID differ")
        if type(self.reveal_availability) is not RevealAvailability:
            raise TypeError("pane snapshot reveal availability is invalid")
        if self.reveal_availability is RevealAvailability.AVAILABLE:
            _require_sha256(
                self.reveal_evidence_sha256,
                "pane snapshot reveal evidence digest",
            )
        elif self.reveal_evidence_sha256 is not None:
            raise ValueError("pane snapshot exposes an unavailable reveal digest")
        if (
            self.observation_mode is ObservationMode.AS_OBSERVED
            and self.reveal_availability is not RevealAvailability.NOT_REQUESTED
        ):
            raise ValueError("as-observed pane snapshot carries reveal state")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("pane snapshot cursor must be nonnegative")
        if self.action_time_us is not None and (
            type(self.action_time_us) is not int
            or self.action_time_us < 0
            or self.action_time_us > self.render_cursor_time_us
        ):
            raise ValueError("pane snapshot action time is invalid")
        if self.capability_read_id is None:
            if (
                self.capability_authority is not None
                or self.capability_manifest_sha256 is not None
            ):
                raise ValueError("pane snapshot capability metadata lacks a read ID")
        else:
            _require_identifier(self.capability_read_id, "pane capability read ID")
            if type(self.capability_authority) is not PaneCapabilityAuthority:
                raise TypeError("pane snapshot capability authority is invalid")
            if (
                self.capability_authority
                is PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
            ):
                _require_sha256(
                    self.capability_manifest_sha256,
                    "pane snapshot capability manifest digest",
                )
            elif self.capability_manifest_sha256 is not None:
                raise ValueError(
                    "non-authorizing pane snapshot exposes a capability manifest pin"
                )
        if type(self.panes) is not tuple or any(
            type(item) is not ReplayPane for item in self.panes
        ):
            raise TypeError("pane snapshot inventory is invalid")
        if tuple(item.pane_kind for item in self.panes) != PANE_ORDER:
            raise ValueError("pane snapshot must contain the exact ordered pane inventory")
        if any(
            item.render_cursor_time_us != self.render_cursor_time_us
            or item.observation_mode is not self.observation_mode
            or item.policy_id != self.policy_id
            or item.query_id != self.query_id
            for item in self.panes
        ):
            raise ValueError("pane snapshot contains a cursor/policy/query split brain")
        for pane in self.panes:
            for datum in pane.data:
                _validate_datum_at_cursor(
                    datum,
                    self.render_cursor_time_us,
                    self.observation_mode,
                    self.observed_projection_sha256,
                    self.reveal_availability,
                    self.reveal_evidence_sha256,
                )
            for estimate in pane.queue_estimates:
                for source in estimate.source_events:
                    _validate_source_at_snapshot(
                        source,
                        self.render_cursor_time_us,
                        self.observation_mode,
                        self.observed_projection_sha256,
                        self.reveal_availability,
                        self.reveal_evidence_sha256,
                    )
        if (
            self.schema_id != PANE_SNAPSHOT_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != PANE_SNAPSHOT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported pane snapshot schema")
        object.__setattr__(
            self,
            "snapshot_id",
            "pane-snapshot-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def pane(self, pane_kind: PaneKind) -> ReplayPane:
        if type(pane_kind) is not PaneKind:
            raise TypeError("pane lookup requires PaneKind")
        return self.panes[PANE_ORDER.index(pane_kind)]

    @property
    def available_pane_count(self) -> int:
        return sum(item.availability is PaneAvailability.AVAILABLE for item in self.panes)

    @property
    def unavailable_pane_count(self) -> int:
        return sum(
            item.availability is PaneAvailability.UNAVAILABLE for item in self.panes
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "action_time_us": self.action_time_us,
            "capability_authority": (
                None
                if self.capability_authority is None
                else self.capability_authority.value
            ),
            "capability_manifest_sha256": self.capability_manifest_sha256,
            "capability_read_id": self.capability_read_id,
            "observation_mode": self.observation_mode.value,
            "observed_projection_sha256": self.observed_projection_sha256,
            "panes": [item.as_dict() for item in self.panes],
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "reveal_availability": self.reveal_availability.value,
            "reveal_evidence_sha256": self.reveal_evidence_sha256,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "snapshot_id": self.snapshot_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def bind_pane_capabilities(
    result: ObservationQueryResult,
    *,
    supported_panes: tuple[PaneKind, ...],
) -> PaneCapabilityRead:
    """Create an empty historical receipt for non-authorizing boundary probes.

    Local metadata cannot declare pane support, recorded emptiness, queue
    capability, or source class.  All capability claims require
    :func:`load_verified_pane_capabilities`.
    """

    _require_query_result(result)
    if supported_panes:
        raise ValueError("local non-authorizing metadata cannot declare pane support")
    return PaneCapabilityRead(
        _PaneCapabilityFields(
            source_run_id=result.source_run_id,
            source_event_sha256=result.source_event_sha256,
            observed_projection_sha256=result.observed_projection_sha256,
            query_id=result.query_id,
            observation_mode=result.policy.mode,
            policy_id=result.policy.policy_id,
            render_cursor_time_us=result.request.render_cursor_time_us,
            supported_panes=(),
            source_class=ReplaySourceClass.HISTORICAL,
            source_schema_id=HISTORICAL_PANE_SOURCE_SCHEMA_ID,
            source_schema_version=PANE_SOURCE_SCHEMA_VERSION,
            queue_capability=QueueCapability.UNAVAILABLE,
            queue_estimator_version=None,
            authority=PaneCapabilityAuthority.LOCAL_NONAUTHORIZING,
            manifest_sha256=None,
            reveal_authorization_id=None,
            reveal_authorization_sha256=None,
            reveal_evidence_sha256=None,
            reveal_source_capability_manifest_sha256=None,
        ),
        None,
        _token=_CAPABILITY_CONSTRUCTION_TOKEN,
    )


def load_verified_pane_capabilities(
    manifest_bytes: bytes,
    pinned_manifest_sha256: str,
    *,
    query_result: ObservationQueryResult | None = None,
    reveal_source: _ReplaySourceCapabilityManifest | None = None,
    reveal_authorization: _RevealAuthorization | None = None,
) -> PaneCapabilityRead:
    """Load a closed capability receipt from exact independently pinned bytes.

    The pin must be resolved by backend integration from the governed run index;
    an adjacent self-declared digest is not source authentication.  The returned
    object retains and revalidates the bytes whenever it is used by the pane builder.
    A synthetic receipt additionally requires the exact reveal source manifest,
    grant, and query result used by the backend query call.  First-party UI code
    must not call this loader.
    """

    if type(manifest_bytes) is not bytes:
        raise TypeError("pane capability manifest must be exact bytes")
    if len(manifest_bytes) > _MAX_CAPABILITY_MANIFEST_BYTES:
        raise ValueError("pane capability manifest exceeds the byte-size ceiling")
    _require_sha256(
        pinned_manifest_sha256,
        "pinned pane capability manifest digest",
    )
    if _sha256_bytes(manifest_bytes) != pinned_manifest_sha256:
        raise ValueError("pane capability manifest differs from its supplied pin")
    fields = _parse_capability_manifest(
        bytes(manifest_bytes),
        pinned_manifest_sha256,
    )
    reveal_context = (query_result, reveal_source, reveal_authorization)
    if fields.source_class is ReplaySourceClass.SYNTHETIC:
        _verify_synthetic_capability_context(
            fields,
            query_result,
            reveal_source,
            reveal_authorization,
        )
    elif any(item is not None for item in reveal_context):
        raise ValueError(
            "historical pane capability loading does not accept reveal authority"
        )
    return PaneCapabilityRead(
        fields,
        bytes(manifest_bytes),
        _token=_CAPABILITY_CONSTRUCTION_TOKEN,
    )


def build_synchronized_panes(
    result: ObservationQueryResult,
    *,
    capabilities: PaneCapabilityRead | None = None,
) -> SynchronizedPaneSnapshot:
    """Project one policy-enforced result into the exact WO36-C pane inventory."""

    _require_query_result(result)
    if capabilities is not None:
        if type(capabilities) is not PaneCapabilityRead:
            raise TypeError("pane capabilities must use PaneCapabilityRead")
        _validate_capability_binding(result, capabilities)
    effective_capabilities = (
        capabilities
        if capabilities is not None
        and capabilities.authority
        is PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
        else None
    )

    values = tuple(
        item for item in result.values if item.disposition is RecordDisposition.VALUE
    )
    _reject_protected_namespace_laundering(result, values)
    values_by_event_id = _index_query_values(values)
    routed: dict[PaneKind, list[PaneDatum]] = {kind: [] for kind in PANE_ORDER}

    for value in values:
        for pane_kind in _pane_routes(value):
            routed[pane_kind].append(
                _direct_datum(
                    pane_kind,
                    value,
                    values_by_event_id=values_by_event_id,
                )
            )
        routed[PaneKind.LATENCY_TIMELINE].append(_latency_datum(value))

    queue_estimates = _build_queue_estimates(
        result,
        values,
        effective_capabilities,
    )
    if (
        effective_capabilities is not None
    ):
        visible_source_panes = {
            pane_kind
            for pane_kind, pane_data in routed.items()
            if pane_data and pane_kind is not PaneKind.LATENCY_TIMELINE
        }
        if queue_estimates:
            visible_source_panes.add(PaneKind.INDIVIDUAL_QUEUE)
        undeclared = visible_source_panes - set(
            effective_capabilities.supported_panes
        )
        if undeclared:
            raise ValueError(
                "pinned pane capability manifest omits visible source panes: "
                + ",".join(item.value for item in PANE_ORDER if item in undeclared)
            )
    panes = tuple(
        _build_pane(
            pane_kind,
            tuple(routed[pane_kind]),
            queue_estimates if pane_kind is PaneKind.INDIVIDUAL_QUEUE else (),
            result,
            effective_capabilities,
        )
        for pane_kind in PANE_ORDER
    )
    return SynchronizedPaneSnapshot(
        source_run_id=result.source_run_id,
        source_event_sha256=result.source_event_sha256,
        observed_projection_sha256=result.observed_projection_sha256,
        query_id=result.query_id,
        observation_mode=result.policy.mode,
        policy_id=result.policy.policy_id,
        reveal_availability=result.reveal.availability,
        reveal_evidence_sha256=result.reveal_evidence_sha256,
        render_cursor_time_us=result.request.render_cursor_time_us,
        action_time_us=result.request.action_time_us,
        panes=panes,
        capability_read_id=(
            None
            if effective_capabilities is None
            else effective_capabilities.capability_read_id
        ),
        capability_authority=(
            None
            if effective_capabilities is None
            else effective_capabilities.authority
        ),
        capability_manifest_sha256=(
            None
            if effective_capabilities is None
            else effective_capabilities.manifest_sha256
        ),
        _construction_token=_SNAPSHOT_CONSTRUCTION_TOKEN,
    )


def _pane_routes(value: QueriedValue) -> tuple[PaneKind, ...]:
    matches = tuple(spec for spec in _PANE_ROUTE_SPECS if spec.matches(value.series_id))
    if not matches:
        return ()
    if len(matches) != 1:
        raise RuntimeError(
            f"pane route table is ambiguous for series {value.series_id!r}"
        )
    spec = matches[0]
    if value.source_kind not in spec.allowed_source_kinds:
        expected = ",".join(
            item.value for item in sorted(
                spec.allowed_source_kinds,
                key=lambda item: item.value,
            )
        )
        raise ValueError(
            f"pane route {spec.route_id!r} requires evidence source {expected}"
        )
    try:
        _validate_route_payload(spec, value)
    except TypeError as exc:
        raise ValueError(
            f"pane route {spec.route_id!r} payload has an invalid exact type"
        ) from exc
    return spec.panes


def _direct_datum(
    pane_kind: PaneKind,
    value: QueriedValue,
    *,
    values_by_event_id: Mapping[str, QueriedValue],
) -> PaneDatum:
    sources = [PaneSourceEvent.from_query_value(value)]
    if (
        pane_kind is PaneKind.FEATURE_PROVENANCE
        and value.series_id != "feature.imbalance"
    ):
        payload = _exact_mapping(
            value.payload,
            {"provenance_event_ids", "value_ppm"},
            "feature pane payload",
        )
        provenance_ids = _identifier_tuple(
            payload["provenance_event_ids"],
            "feature provenance event IDs",
            nonempty=True,
        )
        for event_id in provenance_ids:
            provenance = values_by_event_id.get(event_id)
            if provenance is None:
                raise ValueError(
                    "feature provenance cites an event outside its exact query result"
                )
            if provenance.event_id == value.event_id:
                raise ValueError("feature provenance cannot cite its own feature event")
            if (
                provenance.disposition is not RecordDisposition.VALUE
                or provenance.source_kind not in _OBSERVED_PANE_SOURCE_KINDS
                or provenance.data_age.policy_visible_at_time_us
                > value.data_age.policy_visible_at_time_us
            ):
                raise ValueError(
                    "feature provenance cites reveal, non-value, or future evidence"
                )
            sources.append(PaneSourceEvent.from_query_value(provenance))
    return PaneDatum(
        datum_id=_datum_id(pane_kind, value.event_id),
        label=value.series_id,
        unit="recorded-payload",
        value=thaw_json(value.payload),
        source_events=tuple(sources),
    )


def _index_query_values(
    values: tuple[QueriedValue, ...],
) -> Mapping[str, QueriedValue]:
    output: dict[str, QueriedValue] = {}
    for value in values:
        if value.event_id in output:
            raise ValueError("pane query contains duplicate event identities")
        output[value.event_id] = value
    return output


def _validate_route_payload(spec: _PaneRouteSpec, value: QueriedValue) -> None:
    """Dispatch the selected route to exactly one closed payload validator."""

    contract = spec.payload_contract
    label = f"{spec.route_id} payload"
    if contract is _PanePayloadContract.LEVEL_2:
        payload = _exact_mapping(value.payload, {"asks", "bids", "record_kind"}, label)
        _closed_text(payload["record_kind"], "level-2 record kind", {"LEVEL_2"})
        _price_quantity_pairs(payload["asks"], "level-2 asks")
        _price_quantity_pairs(payload["bids"], "level-2 bids")
        return
    if contract is _PanePayloadContract.TIME_AND_SALES:
        payload = _exact_mapping(value.payload, {"price_ticks", "quantity"}, label)
        _exact_integer(payload["price_ticks"], "trade price ticks")
        _positive_integer(payload["quantity"], "trade quantity")
        return
    if contract is _PanePayloadContract.DEPTH_HEATMAP:
        payload = _exact_mapping(value.payload, {"record_kind", "rows"}, label)
        _closed_text(
            payload["record_kind"],
            "depth heatmap record kind",
            {"DEPTH_HEATMAP"},
        )
        _price_quantity_pairs(payload["rows"], "depth heatmap rows")
        return
    if contract is _PanePayloadContract.PLAYER_ORDER_STATE:
        if not isinstance(value.payload, Mapping):
            raise TypeError(f"{label} must be an object")
        payload_fields = set(value.payload)
        if payload_fields not in ({"state"}, {"order_id", "state"}):
            raise ValueError(f"{label} fields are not an exact governed shape")
        payload = value.payload
        _closed_text(payload["state"], "player order state", _PLAYER_ORDER_STATES)
        expected_order_id = value.series_id.split(".", 1)[1]
        _require_identifier(expected_order_id, "player order series identity")
        if "order_id" in payload:
            _require_identifier(payload["order_id"], "player order ID")
            if payload["order_id"] != expected_order_id:
                raise ValueError("player order payload differs from its series identity")
        return
    if contract is _PanePayloadContract.POSITION:
        payload = _exact_mapping(value.payload, {"quantity"}, label)
        _exact_integer(payload["quantity"], "position quantity")
        return
    if contract is _PanePayloadContract.STRATEGY_SIGNAL:
        payload = _exact_mapping(value.payload, {"recorded_signal"}, label)
        _closed_text(payload["recorded_signal"], "recorded strategy signal", _SIGNAL_STATES)
        return
    if contract is _PanePayloadContract.TRAFFIC_LIGHT:
        payload = _exact_mapping(value.payload, {"record_kind", "state"}, label)
        _closed_text(
            payload["record_kind"],
            "traffic-light record kind",
            {"TRAFFIC_LIGHT_TRANSITION"},
        )
        _closed_text(payload["state"], "traffic-light state", _SIGNAL_STATES)
        return
    if contract is _PanePayloadContract.STRATEGY_RULE:
        payload = _exact_mapping(
            value.payload,
            {"recorded_rule_id", "result"},
            label,
        )
        _require_identifier(payload["recorded_rule_id"], "recorded strategy rule ID")
        _exact_boolean(payload["result"], "recorded strategy rule result")
        return
    if contract is _PanePayloadContract.INGESTED_FEATURE:
        payload = _exact_mapping(value.payload, {"value_millionths"}, label)
        value_millionths = _exact_integer(
            payload["value_millionths"],
            "ingested feature value millionths",
        )
        if not -1_000_000 <= value_millionths <= 1_000_000:
            raise ValueError("ingested feature value must be within +/-1000000")
        return
    if contract is _PanePayloadContract.FEATURE:
        payload = _exact_mapping(
            value.payload,
            {"provenance_event_ids", "value_ppm"},
            label,
        )
        _identifier_tuple(
            payload["provenance_event_ids"],
            "feature provenance event IDs",
            nonempty=True,
        )
        value_ppm = _exact_integer(payload["value_ppm"], "feature value ppm")
        if not -1_000_000 <= value_ppm <= 1_000_000:
            raise ValueError("feature value ppm must be within +/-1000000")
        return
    if contract is _PanePayloadContract.AGENT_ACTIVITY:
        payload = _exact_mapping(value.payload, {"activity", "agent_id"}, label)
        _closed_text(payload["activity"], "agent activity", _AGENT_ACTIVITIES)
        _require_identifier(payload["agent_id"], "agent activity agent ID")
        return
    if contract is _PanePayloadContract.VENUE_QUOTE:
        payload = _exact_mapping(
            value.payload,
            {"ask_ticks", "bid_ticks", "venue"},
            label,
        )
        _exact_integer(payload["ask_ticks"], "venue ask ticks")
        _exact_integer(payload["bid_ticks"], "venue bid ticks")
        _require_identifier(payload["venue"], "venue quote venue ID")
        series_venue = value.series_id.removeprefix("quote.venue.")
        if str(payload["venue"]).casefold() != series_venue.casefold():
            raise ValueError("venue quote payload differs from its series identity")
        return
    if contract is _PanePayloadContract.CONSOLIDATED_QUOTE:
        payload = _exact_mapping(
            value.payload,
            {"best_ask_ticks", "best_bid_ticks"},
            label,
        )
        # A consolidated composite may intentionally preserve a crossed market.
        _exact_integer(payload["best_ask_ticks"], "consolidated ask ticks")
        _exact_integer(payload["best_bid_ticks"], "consolidated bid ticks")
        return
    if contract is _PanePayloadContract.CONSOLIDATED_BID:
        payload = _exact_mapping(value.payload, {"best_bid_ticks"}, label)
        _exact_integer(payload["best_bid_ticks"], "consolidated bid ticks")
        return
    if contract is _PanePayloadContract.CONSOLIDATED_ASK:
        payload = _exact_mapping(value.payload, {"best_ask_ticks"}, label)
        _exact_integer(payload["best_ask_ticks"], "consolidated ask ticks")
        return
    if contract is _PanePayloadContract.FILL:
        payload = _exact_mapping(value.payload, {"filled_quantity"}, label)
        _nonnegative_integer(payload["filled_quantity"], "fill quantity")
        return
    if contract is _PanePayloadContract.EXECUTION_METRICS:
        payload = _exact_mapping(
            value.payload,
            {"filled_quantity", "implementation_shortfall_x2"},
            label,
        )
        _nonnegative_integer(
            payload["filled_quantity"],
            "execution metrics filled quantity",
        )
        _exact_integer(
            payload["implementation_shortfall_x2"],
            "execution metrics implementation shortfall",
        )
        return
    if contract is _PanePayloadContract.MECHANISTIC_TRACE:
        payload = _exact_mapping(value.payload, {"trace_id"}, label)
        _require_identifier(payload["trace_id"], "mechanistic trace ID")
        return
    if contract is _PanePayloadContract.QUEUE_ESTIMATE:
        payload = _exact_mapping(
            value.payload,
            {
                "estimated_quantity_ahead",
                "uncertainty_lower_quantity",
                "uncertainty_upper_quantity",
            },
            label,
        )
        estimate = _nonnegative_integer(
            payload["estimated_quantity_ahead"],
            "queue estimated quantity",
        )
        lower = _nonnegative_integer(
            payload["uncertainty_lower_quantity"],
            "queue uncertainty lower quantity",
        )
        upper = _nonnegative_integer(
            payload["uncertainty_upper_quantity"],
            "queue uncertainty upper quantity",
        )
        if not lower <= estimate <= upper:
            raise ValueError("queue estimate lies outside its uncertainty interval")
        return
    if contract is _PanePayloadContract.QUEUE_TRUTH:
        payload = _exact_mapping(value.payload, {"truth_quantity_ahead"}, label)
        _nonnegative_integer(payload["truth_quantity_ahead"], "queue truth quantity")
        return
    raise RuntimeError(f"unhandled pane payload contract {contract.value!r}")


def _latency_datum(value: QueriedValue) -> PaneDatum:
    source = PaneSourceEvent.from_query_value(value)
    data_age = value.data_age
    return PaneDatum(
        datum_id=_datum_id(PaneKind.LATENCY_TIMELINE, value.event_id),
        label=f"latency.{value.series_id}",
        unit="microseconds",
        value=data_age.as_dict(),
        source_events=(source,),
        calculation=DeclaredCalculation(
            calculation_id="cursor-safe-latency-projection",
            calculation_version=1,
            kind=CalculationKind.TIMING_PROJECTION,
            expression=(
                "recorded causal timestamps and cursor minus source/policy-visible time"
            ),
            source_event_ids=(value.event_id,),
        ),
    )


def _build_queue_estimates(
    result: ObservationQueryResult,
    values: tuple[QueriedValue, ...],
    capabilities: PaneCapabilityRead | None,
) -> tuple[QueueEstimate, ...]:
    estimate_values = {
        item.series_id.removeprefix("queue.estimate."): item
        for item in values
        if item.series_id.startswith("queue.estimate.")
    }
    truth_values = {
        item.series_id.removeprefix("queue.truth."): item
        for item in values
        if item.series_id.startswith("queue.truth.")
    }
    if len(estimate_values) != sum(
        item.series_id.startswith("queue.estimate.") for item in values
    ):
        raise ValueError("query contains duplicate queue estimate identities")
    if len(truth_values) != sum(
        item.series_id.startswith("queue.truth.") for item in values
    ):
        raise ValueError("query contains duplicate queue truth identities")
    if truth_values and not _queue_truth_authorized(result, capabilities):
        raise ValueError(
            "queue truth requires authorized postmortem synthetic-source evidence"
        )
    if estimate_values and (
        capabilities is None
        or capabilities.authority
        is not PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
        or capabilities.queue_capability is QueueCapability.UNAVAILABLE
    ):
        raise ValueError(
            "queue estimates require a pinned backend queue capability manifest"
        )
    if set(truth_values) - set(estimate_values):
        raise ValueError("queue truth lacks a corresponding visible queue estimate")

    output: list[QueueEstimate] = []
    for queue_id in sorted(estimate_values):
        estimate_value = estimate_values[queue_id]
        estimate_payload = _exact_mapping(
            estimate_value.payload,
            {
                "estimated_quantity_ahead",
                "uncertainty_lower_quantity",
                "uncertainty_upper_quantity",
            },
            "queue estimate payload",
        )
        estimate = _nonnegative_integer(
            estimate_payload["estimated_quantity_ahead"],
            "queue estimated quantity",
        )
        lower = _nonnegative_integer(
            estimate_payload["uncertainty_lower_quantity"],
            "queue uncertainty lower quantity",
        )
        upper = _nonnegative_integer(
            estimate_payload["uncertainty_upper_quantity"],
            "queue uncertainty upper quantity",
        )
        if capabilities is None:  # pragma: no cover - checked above
            raise RuntimeError("queue capability disappeared")
        truth_value = truth_values.get(queue_id)
        truth_quantity: int | None = None
        truth_availability = _queue_truth_unavailable_reason(result, capabilities)
        sources = [PaneSourceEvent.from_query_value(estimate_value)]
        if truth_value is not None:
            truth_payload = _exact_mapping(
                truth_value.payload,
                {"truth_quantity_ahead"},
                "queue truth payload",
            )
            truth_quantity = _nonnegative_integer(
                truth_payload["truth_quantity_ahead"],
                "queue truth quantity",
            )
            truth_availability = (
                QueueTruthAvailability.AUTHORIZED_SYNTHETIC_POSTMORTEM
            )
            sources.append(PaneSourceEvent.from_query_value(truth_value))
        output.append(
            QueueEstimate(
                queue_id=queue_id,
                capability=capabilities.queue_capability,
                estimator_version=capabilities.queue_estimator_version,
                estimated_quantity_ahead=estimate,
                uncertainty_lower_quantity=lower,
                uncertainty_upper_quantity=upper,
                truth_availability=truth_availability,
                truth_quantity_ahead=truth_quantity,
                source_events=tuple(sources),
            )
        )
    return tuple(output)


def _build_pane(
    pane_kind: PaneKind,
    data: tuple[PaneDatum, ...],
    queue_estimates: tuple[QueueEstimate, ...],
    result: ObservationQueryResult,
    capabilities: PaneCapabilityRead | None,
) -> ReplayPane:
    binding = {
        "render_cursor_time_us": result.request.render_cursor_time_us,
        "observation_mode": result.policy.mode,
        "policy_id": result.policy.policy_id,
        "query_id": result.query_id,
    }
    if data or queue_estimates:
        return ReplayPane(
            pane_kind=pane_kind,
            availability=PaneAvailability.AVAILABLE,
            data=data,
            queue_estimates=queue_estimates,
            **binding,
        )
    if (
        capabilities is not None
        and pane_kind in capabilities.supported_panes
        and pane_kind
        not in {
            PaneKind.AGENT_ACTIVITY,
            PaneKind.COUNTERFACTUAL_COMPARISON,
        }
    ):
        return ReplayPane(
            pane_kind=pane_kind,
            availability=PaneAvailability.RECORDED_EMPTY,
            explanation=PaneExplanation(
                PaneUnavailableReason.NO_VISIBLE_EVENTS_AT_CURSOR,
                _empty_detail(pane_kind, result.request.render_cursor_time_us),
            ),
            **binding,
        )
    return ReplayPane(
        pane_kind=pane_kind,
        availability=PaneAvailability.UNAVAILABLE,
        explanation=PaneExplanation(
            _default_unavailable_reason(pane_kind, result),
            _unavailable_detail(pane_kind),
        ),
        **binding,
    )


def _reject_protected_namespace_laundering(
    result: ObservationQueryResult,
    values: tuple[QueriedValue, ...],
) -> None:
    deferred_comparison = tuple(
        item
        for item in values
        if item.series_id.startswith(
            ("comparison.counterfactual.", "counterfactual.comparison.")
        )
    )
    if deferred_comparison:
        raise ValueError(
            "counterfactual comparison pane inputs are unavailable until WO36-E"
        )
    protected_sources = (
        ("agent.", EvidenceSourceKind.REVEALED_HIDDEN_STATE),
        ("queue.truth.", EvidenceSourceKind.REVEALED_GROUND_TRUTH),
    )
    for prefix, required_source_kind in protected_sources:
        protected = tuple(
            item for item in values if item.series_id.startswith(prefix)
        )
        for value in protected:
            if value.source_kind is not required_source_kind:
                raise ValueError(
                    f"protected pane series {value.series_id!r} requires "
                    f"{required_source_kind.value} evidence"
                )
            if (
                result.policy.mode is not ObservationMode.POSTMORTEM
                or result.reveal.availability is not RevealAvailability.AVAILABLE
            ):
                raise ValueError(
                    f"protected pane series {value.series_id!r} lacks authorization"
                )


def _parse_capability_manifest(
    raw: bytes,
    pinned_manifest_sha256: str,
) -> _PaneCapabilityFields:
    if type(raw) is not bytes:
        raise TypeError("pane capability manifest must be exact bytes")
    if len(raw) > _MAX_CAPABILITY_MANIFEST_BYTES:
        raise ValueError("pane capability manifest exceeds the byte-size ceiling")
    if _sha256_bytes(raw) != pinned_manifest_sha256:
        raise ValueError("pinned pane capability manifest bytes changed")
    payload = parse_canonical_json_object(raw)
    expected_fields = {
        "capability_scope",
        "observation_mode",
        "observed_projection_sha256",
        "policy_id",
        "query_id",
        "queue_capability",
        "queue_estimator_version",
        "reveal_authorization_id",
        "reveal_authorization_sha256",
        "reveal_evidence_sha256",
        "reveal_source_capability_manifest_sha256",
        "render_cursor_time_us",
        "schema_id",
        "schema_version",
        "source_class",
        "source_event_sha256",
        "source_run_id",
        "source_schema_id",
        "source_schema_version",
        "supported_panes",
    }
    if set(payload) != expected_fields:
        raise ValueError("pane capability manifest fields are not exact")
    if payload["capability_scope"] != PANE_CAPABILITY_MANIFEST_SCOPE:
        raise ValueError("pane capability manifest scope is invalid")
    if payload["schema_id"] != PANE_CAPABILITY_SCHEMA_ID:
        raise ValueError("unsupported pane capability manifest schema ID")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != PANE_CAPABILITY_SCHEMA_VERSION
    ):
        raise ValueError("unsupported pane capability manifest schema version")
    supported_raw = payload["supported_panes"]
    if type(supported_raw) is not list or any(
        type(item) is not str for item in supported_raw
    ):
        raise TypeError("pane capability supported panes are invalid")
    try:
        supported_panes = tuple(PaneKind(item) for item in supported_raw)
        observation_mode = ObservationMode(payload["observation_mode"])
        source_class = ReplaySourceClass(payload["source_class"])
        queue_capability = QueueCapability(payload["queue_capability"])
    except (TypeError, ValueError) as exc:
        raise ValueError("pane capability manifest contains an unknown enum") from exc
    queue_estimator_version = payload["queue_estimator_version"]
    if queue_estimator_version is not None and type(queue_estimator_version) is not str:
        raise TypeError("pane capability estimator version is invalid")
    for key in (
        "reveal_authorization_id",
        "reveal_authorization_sha256",
        "reveal_evidence_sha256",
        "reveal_source_capability_manifest_sha256",
    ):
        if payload[key] is not None and type(payload[key]) is not str:
            raise TypeError(f"pane capability manifest field {key!r} is invalid")
    source_schema_version = payload["source_schema_version"]
    render_cursor_time_us = payload["render_cursor_time_us"]
    if type(source_schema_version) is not int:
        raise TypeError("pane capability source schema version is invalid")
    if type(render_cursor_time_us) is not int:
        raise TypeError("pane capability cursor is invalid")
    for key in (
        "observed_projection_sha256",
        "policy_id",
        "query_id",
        "source_event_sha256",
        "source_run_id",
        "source_schema_id",
    ):
        if type(payload[key]) is not str:
            raise TypeError(f"pane capability manifest field {key!r} is invalid")
    fields = _PaneCapabilityFields(
        source_run_id=payload["source_run_id"],
        source_event_sha256=payload["source_event_sha256"],
        observed_projection_sha256=payload["observed_projection_sha256"],
        query_id=payload["query_id"],
        observation_mode=observation_mode,
        policy_id=payload["policy_id"],
        render_cursor_time_us=render_cursor_time_us,
        supported_panes=supported_panes,
        source_class=source_class,
        source_schema_id=payload["source_schema_id"],
        source_schema_version=source_schema_version,
        queue_capability=queue_capability,
        queue_estimator_version=queue_estimator_version,
        authority=PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST,
        manifest_sha256=pinned_manifest_sha256,
        reveal_authorization_id=payload["reveal_authorization_id"],
        reveal_authorization_sha256=payload["reveal_authorization_sha256"],
        reveal_evidence_sha256=payload["reveal_evidence_sha256"],
        reveal_source_capability_manifest_sha256=(
            payload["reveal_source_capability_manifest_sha256"]
        ),
    )
    if fields.manifest_dict() != payload:
        raise ValueError("pane capability manifest inventory is not canonical")
    return fields


def _verify_synthetic_capability_context(
    fields: _PaneCapabilityFields,
    query_result: ObservationQueryResult | None,
    reveal_source: _ReplaySourceCapabilityManifest | None,
    reveal_authorization: _RevealAuthorization | None,
) -> None:
    if type(query_result) is not ObservationQueryResult:
        raise TypeError("synthetic pane capability requires its exact query result")
    if type(reveal_source) is not _ReplaySourceCapabilityManifest:
        raise TypeError("synthetic pane capability requires its exact reveal source")
    if type(reveal_authorization) is not _RevealAuthorization:
        raise TypeError("synthetic pane capability requires its exact reveal grant")
    if (
        query_result.policy.mode is not ObservationMode.POSTMORTEM
        or query_result.reveal.availability is not RevealAvailability.AVAILABLE
        or query_result.reveal_evidence_sha256 is None
        or query_result.reveal.authorization_id is None
    ):
        raise ValueError("synthetic pane capability requires authorized reveal output")
    query_binding = (
        query_result.source_run_id,
        query_result.source_event_sha256,
        query_result.observed_projection_sha256,
        query_result.query_id,
        query_result.policy.mode,
        query_result.policy.policy_id,
        query_result.request.render_cursor_time_us,
    )
    manifest_binding = (
        fields.source_run_id,
        fields.source_event_sha256,
        fields.observed_projection_sha256,
        fields.query_id,
        fields.observation_mode,
        fields.policy_id,
        fields.render_cursor_time_us,
    )
    if manifest_binding != query_binding:
        raise ValueError("synthetic pane manifest is bound to another query")
    if (
        reveal_source.source_schema_id != SYNTHETIC_PANE_SOURCE_SCHEMA_ID
        or reveal_source.source_schema_version != PANE_SOURCE_SCHEMA_VERSION
        or fields.source_schema_id != reveal_source.source_schema_id
        or fields.source_schema_version != reveal_source.source_schema_version
    ):
        raise ValueError("synthetic pane classification lacks the closed source schema")
    source_manifest_payload = reveal_source.as_dict()
    supplied_source_manifest_sha256 = source_manifest_payload.pop(
        "manifest_sha256",
        None,
    )
    if (
        supplied_source_manifest_sha256 != reveal_source.manifest_sha256
        or _canonical_sha256(source_manifest_payload)
        != supplied_source_manifest_sha256
    ):
        raise ValueError("synthetic reveal source manifest identity is inconsistent")
    if (
        reveal_source.source_run_id != query_result.source_run_id
        or reveal_source.source_event_sha256 != query_result.source_event_sha256
        or reveal_authorization.source_run_id != query_result.source_run_id
        or reveal_authorization.source_event_sha256
        != query_result.source_event_sha256
    ):
        raise ValueError("synthetic reveal authority belongs to another source run")
    authorization_sha256 = _canonical_sha256(reveal_authorization.as_dict())
    if (
        fields.reveal_authorization_id
        != query_result.reveal.authorization_id
        or fields.reveal_authorization_id
        != reveal_authorization.authorization_id
        or fields.reveal_authorization_sha256 != authorization_sha256
        or fields.reveal_evidence_sha256
        != query_result.reveal_evidence_sha256
        or fields.reveal_evidence_sha256
        != reveal_authorization.reveal_evidence_sha256
        or fields.reveal_source_capability_manifest_sha256
        != reveal_source.manifest_sha256
        or fields.reveal_source_capability_manifest_sha256
        != reveal_authorization.source_capability_manifest_sha256
    ):
        raise ValueError("synthetic pane manifest differs from query reveal authority")
    requested = set(query_result.request.requested_reveal_capabilities)
    if (
        not requested
        or not requested.issubset(reveal_authorization.capabilities)
        or not requested.issubset(reveal_source.capabilities)
    ):
        raise ValueError("synthetic pane reveal authority lacks the query capability scope")


def _queue_truth_authorized(
    result: ObservationQueryResult,
    capabilities: PaneCapabilityRead | None,
) -> bool:
    return (
        result.policy.mode is ObservationMode.POSTMORTEM
        and result.reveal.availability is RevealAvailability.AVAILABLE
        and capabilities is not None
        and capabilities.authority
        is PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
        and capabilities.manifest_sha256 is not None
        and capabilities.source_class is ReplaySourceClass.SYNTHETIC
        and capabilities.reveal_authorization_id
        == result.reveal.authorization_id
        and capabilities.reveal_evidence_sha256
        == result.reveal_evidence_sha256
        and capabilities.reveal_authorization_sha256 is not None
        and capabilities.reveal_source_capability_manifest_sha256 is not None
    )


def _queue_truth_unavailable_reason(
    result: ObservationQueryResult,
    capabilities: PaneCapabilityRead,
) -> QueueTruthAvailability:
    if result.policy.mode is not ObservationMode.POSTMORTEM:
        return QueueTruthAvailability.AUTHORIZATION_REQUIRED
    if result.reveal.availability is not RevealAvailability.AVAILABLE:
        return QueueTruthAvailability.AUTHORIZATION_REQUIRED
    if capabilities.source_class is not ReplaySourceClass.SYNTHETIC:
        return QueueTruthAvailability.SOURCE_NOT_SYNTHETIC
    return QueueTruthAvailability.NOT_RECORDED


def _validate_capability_binding(
    result: ObservationQueryResult,
    capabilities: PaneCapabilityRead,
) -> None:
    fields = capabilities._revalidate()
    expected = (
        result.source_run_id,
        result.source_event_sha256,
        result.observed_projection_sha256,
        result.query_id,
        result.policy.mode,
        result.policy.policy_id,
        result.request.render_cursor_time_us,
    )
    actual = (
        fields.source_run_id,
        fields.source_event_sha256,
        fields.observed_projection_sha256,
        fields.query_id,
        fields.observation_mode,
        fields.policy_id,
        fields.render_cursor_time_us,
    )
    if actual != expected:
        raise ValueError("pane capability read is bound to another query/cursor/policy")
    if fields.source_class is ReplaySourceClass.SYNTHETIC and (
        result.reveal.availability is not RevealAvailability.AVAILABLE
        or result.reveal.authorization_id != fields.reveal_authorization_id
        or result.reveal_evidence_sha256 != fields.reveal_evidence_sha256
    ):
        raise ValueError("synthetic pane receipt differs from query reveal authority")


def _validate_datum_at_cursor(
    datum: PaneDatum,
    cursor_time_us: int,
    mode: ObservationMode,
    observed_projection_sha256: str,
    reveal_availability: RevealAvailability,
    reveal_evidence_sha256: str | None,
) -> None:
    for source in datum.source_events:
        _validate_source_at_snapshot(
            source,
            cursor_time_us,
            mode,
            observed_projection_sha256,
            reveal_availability,
            reveal_evidence_sha256,
        )


def _validate_source_at_snapshot(
    source: PaneSourceEvent,
    cursor_time_us: int,
    mode: ObservationMode,
    observed_projection_sha256: str,
    reveal_availability: RevealAvailability,
    reveal_evidence_sha256: str | None,
) -> None:
    if source.policy_visible_at_time_us > cursor_time_us:
        raise ValueError("pane datum exposes a future source event")
    if source.source_kind in _REVEAL_SOURCE_KINDS:
        if mode is ObservationMode.AS_OBSERVED:
            raise ValueError("as-observed pane datum contains reveal evidence")
        if reveal_availability is not RevealAvailability.AVAILABLE:
            raise ValueError("pane datum exposes reveal evidence without authorization")
        if source.source_evidence_sha256 != reveal_evidence_sha256:
            raise ValueError("pane datum reveal citation has the wrong evidence digest")
        return
    if source.source_evidence_sha256 != observed_projection_sha256:
        raise ValueError("pane datum observed citation has the wrong projection digest")


def _default_unavailable_reason(
    pane_kind: PaneKind,
    result: ObservationQueryResult,
) -> PaneUnavailableReason:
    reasons = {
        PaneKind.LEVEL_2_LADDER: PaneUnavailableReason.LEVEL_2_NOT_RECORDED,
        PaneKind.TIME_AND_SALES: PaneUnavailableReason.TIME_AND_SALES_NOT_RECORDED,
        PaneKind.DEPTH_HEATMAP: PaneUnavailableReason.DEPTH_HISTORY_NOT_RECORDED,
        PaneKind.INDIVIDUAL_QUEUE: PaneUnavailableReason.QUEUE_CAPABILITY_UNAVAILABLE,
        PaneKind.PLAYER_ORDERS: PaneUnavailableReason.PLAYER_ORDER_STATE_NOT_RECORDED,
        PaneKind.ORDER_STATE_LIFECYCLE: (
            PaneUnavailableReason.ORDER_LIFECYCLE_NOT_RECORDED
        ),
        PaneKind.POSITION: PaneUnavailableReason.POSITION_NOT_RECORDED,
        PaneKind.TRAFFIC_LIGHT: PaneUnavailableReason.TRAFFIC_LIGHT_NOT_RECORDED,
        PaneKind.STRATEGY_RULE_EVIDENCE: (
            PaneUnavailableReason.STRATEGY_EVIDENCE_NOT_RECORDED
        ),
        PaneKind.FEATURE_PROVENANCE: (
            PaneUnavailableReason.FEATURE_PROVENANCE_NOT_RECORDED
        ),
        PaneKind.AGENT_ACTIVITY: (
            PaneUnavailableReason.AUTHORIZED_REVEAL_REQUIRED
            if result.policy.mode is ObservationMode.AS_OBSERVED
            or result.reveal.availability is not RevealAvailability.AVAILABLE
            else PaneUnavailableReason.AGENT_ACTIVITY_NOT_RECORDED
        ),
        PaneKind.LATENCY_TIMELINE: (
            PaneUnavailableReason.LATENCY_EVIDENCE_NOT_VISIBLE
        ),
        PaneKind.VENUE_QUOTES: PaneUnavailableReason.VENUE_QUOTES_NOT_RECORDED,
        PaneKind.CONSOLIDATED_QUOTES: (
            PaneUnavailableReason.CONSOLIDATED_QUOTES_NOT_RECORDED
        ),
        PaneKind.FILLS: PaneUnavailableReason.FILLS_NOT_RECORDED,
        PaneKind.EXECUTION_METRICS: (
            PaneUnavailableReason.EXECUTION_METRICS_NOT_RECORDED
        ),
        PaneKind.MECHANISTIC_TRACE: (
            PaneUnavailableReason.TRACE_READ_MODEL_UNAVAILABLE
        ),
        PaneKind.COUNTERFACTUAL_COMPARISON: (
            PaneUnavailableReason.COUNTERFACTUAL_NOT_SELECTED
        ),
    }
    return reasons[pane_kind]


def _empty_detail(pane_kind: PaneKind, cursor_time_us: int) -> str:
    return (
        f"source declares {pane_kind.value} support but records no policy-visible "
        f"datum at integer cursor {cursor_time_us}"
    )


def _unavailable_detail(pane_kind: PaneKind) -> str:
    details = {
        PaneKind.LEVEL_2_LADDER: "source does not expose policy-visible Level 2 depth",
        PaneKind.TIME_AND_SALES: "source does not expose policy-visible trade prints",
        PaneKind.DEPTH_HEATMAP: "source does not record depth history for a heatmap",
        PaneKind.INDIVIDUAL_QUEUE: (
            "source does not declare exact or estimated individual queue capability"
        ),
        PaneKind.PLAYER_ORDERS: "source does not expose player-order state",
        PaneKind.ORDER_STATE_LIFECYCLE: "source does not expose order lifecycle records",
        PaneKind.POSITION: "source does not expose a position snapshot",
        PaneKind.TRAFFIC_LIGHT: "source does not expose recorded traffic-light state",
        PaneKind.STRATEGY_RULE_EVIDENCE: (
            "source does not expose recorded strategy/rule evidence"
        ),
        PaneKind.FEATURE_PROVENANCE: (
            "source does not expose feature values with provenance"
        ),
        PaneKind.AGENT_ACTIVITY: (
            "agent activity requires authorized postmortem reveal evidence"
        ),
        PaneKind.LATENCY_TIMELINE: "no policy-visible value supplies causal timing",
        PaneKind.VENUE_QUOTES: "source does not expose per-venue quotes",
        PaneKind.CONSOLIDATED_QUOTES: "source does not expose consolidated quotes",
        PaneKind.FILLS: "source records no policy-visible fills",
        PaneKind.EXECUTION_METRICS: "source does not expose execution metrics",
        PaneKind.MECHANISTIC_TRACE: "no policy-safe mechanistic trace read is available",
        PaneKind.COUNTERFACTUAL_COMPARISON: (
            "no counterfactual branch comparison is selected"
        ),
    }
    return details[pane_kind]


def _datum_id(pane_kind: PaneKind, event_id: str) -> str:
    prefix = pane_kind.value.lower().replace("_", "-")
    return f"{prefix}:{event_id}"


def _canonical_source_events(
    values: tuple[PaneSourceEvent, ...],
) -> tuple[PaneSourceEvent, ...]:
    if type(values) is not tuple or any(type(item) is not PaneSourceEvent for item in values):
        raise TypeError("pane source-event citations are invalid")
    output = tuple(sorted(values, key=lambda item: (item.sequence, item.event_id)))
    identities = tuple(item.event_id for item in output)
    if len(identities) != len(set(identities)):
        raise ValueError("pane source-event citations must be unique")
    return output


def _canonical_pane_data(values: tuple[PaneDatum, ...]) -> tuple[PaneDatum, ...]:
    if type(values) is not tuple or any(type(item) is not PaneDatum for item in values):
        raise TypeError("replay pane data are invalid")
    output = tuple(sorted(values, key=lambda item: item.datum_id))
    identities = tuple(item.datum_id for item in output)
    if len(identities) != len(set(identities)):
        raise ValueError("replay pane datum IDs must be unique")
    return output


def _canonical_queue_estimates(
    values: tuple[QueueEstimate, ...],
) -> tuple[QueueEstimate, ...]:
    if type(values) is not tuple or any(type(item) is not QueueEstimate for item in values):
        raise TypeError("queue estimate inventory is invalid")
    output = tuple(sorted(values, key=lambda item: item.queue_id))
    identities = tuple(item.queue_id for item in output)
    if len(identities) != len(set(identities)):
        raise ValueError("queue estimate IDs must be unique")
    return output


def _canonical_pane_kinds(values: tuple[PaneKind, ...]) -> tuple[PaneKind, ...]:
    if type(values) is not tuple or any(type(item) is not PaneKind for item in values):
        raise TypeError("supported pane inventory is invalid")
    if len(values) != len(set(values)):
        raise ValueError("supported pane inventory must be unique")
    selected = set(values)
    return tuple(item for item in PANE_ORDER if item in selected)


def _canonical_identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    for value in values:
        _require_identifier(value, label)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def _exact_mapping(
    value: object,
    expected_keys: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    if set(value) != expected_keys:
        raise ValueError(f"{label} fields are not exact")
    return value


def _exact_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    integer = _exact_integer(value, label)
    if integer < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return integer


def _positive_integer(value: object, label: str) -> int:
    integer = _exact_integer(value, label)
    if integer <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return integer


def _exact_boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _closed_text(
    value: object,
    label: str,
    allowed: frozenset[str] | set[str],
) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{label} is outside its closed vocabulary")
    return value


def _identifier_tuple(
    value: object,
    label: str,
    *,
    nonempty: bool,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an exact array")
    if nonempty and not value:
        raise ValueError(f"{label} cannot be empty")
    for item in value:
        _require_identifier(item, label)
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must be unique")
    return value


def _price_quantity_pairs(value: object, label: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an exact array")
    prices: set[int] = set()
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError(f"{label} must contain exact price/quantity pairs")
        price = _exact_integer(item[0], f"{label} price")
        _positive_integer(item[1], f"{label} quantity")
        if price in prices:
            raise ValueError(f"{label} contains a duplicate price level")
        prices.add(price)


def _require_query_result(result: ObservationQueryResult) -> None:
    if type(result) is not ObservationQueryResult:
        raise TypeError("pane construction requires ObservationQueryResult")


def _require_identifier(value: object, label: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_run_id(value: object) -> None:
    if type(value) is not str or _RUN_ID.fullmatch(value) is None:
        raise ValueError("pane source run ID is invalid")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return canonical_json_bytes(payload)


# Capability loaders, manifests, source classifications, and grants are deliberately
# absent from the UI-safe star-import surface.  Backend integration imports those
# exact names explicitly and passes only the resulting snapshot downstream.
__all__ = [
    "PANE_DATUM_SCHEMA_ID",
    "PANE_DATUM_SCHEMA_VERSION",
    "PANE_ORDER",
    "PANE_SNAPSHOT_SCHEMA_ID",
    "PANE_SNAPSHOT_SCHEMA_VERSION",
    "QUEUE_ESTIMATOR_SCHEMA_ID",
    "QUEUE_ESTIMATOR_SCHEMA_VERSION",
    "CalculationKind",
    "DeclaredCalculation",
    "PaneAvailability",
    "PaneCapabilityAuthority",
    "PaneDatum",
    "PaneExplanation",
    "PaneKind",
    "PaneSourceEvent",
    "PaneUnavailableReason",
    "QueueCapability",
    "QueueEstimate",
    "QueueTruthAvailability",
    "ReplayPane",
    "SynchronizedPaneSnapshot",
    "build_synchronized_panes",
]
