"""Pure, policy-bound cursor navigation for the replay microscope.

The public :class:`ReplayTimeline` is a narrow query facade.  It retains a closed
inventory of immutable event references so it can answer a requested navigation
operation, but it never exports that inventory, its size, its partitions, or its
maximum time.  Those full-run facts belong to :class:`TimelineReceipt`, which is a
backend-only verification artifact and is not cursor-safe UI data.

This is an architectural boundary for cooperative first-party code, not a Python
secrecy primitive.  Code already executing in this interpreter can bypass name
mangling.  An adversarial UI/plugin boundary requires a separate process.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from types import MappingProxyType

from kirby2.full_day.models import canonical_json_bytes
from kirby2.immutable import thaw_json

from .policy import ObservationMode, ObservationPolicy, RevealCapability
from .query import (
    EvidenceSourceKind,
    ObservationQueryResult,
    QueriedValue,
    RecordDisposition,
    RevealAvailability,
    SelectionKind,
)


TIMELINE_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_V1"
TIMELINE_SCHEMA_VERSION = 1
TIMELINE_EVENT_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_EVENT_V1"
TIMELINE_EVENT_SCHEMA_VERSION = 1
TIMELINE_CURSOR_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_CURSOR_V1"
TIMELINE_CURSOR_SCHEMA_VERSION = 1
TIMELINE_RECEIPT_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_RECEIPT_V1"
TIMELINE_RECEIPT_SCHEMA_VERSION = 1
TIMELINE_NAVIGATION_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_NAVIGATION_V1"
TIMELINE_NAVIGATION_SCHEMA_VERSION = 1

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_QUERY_ID = re.compile(r"^observation-query-[0-9a-f]{24}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")
_EVENT_CONSTRUCTION_TOKEN = object()
_OUTPUT_CONSTRUCTION_TOKEN = object()

_DERIVED_CALCULATION = {
    "INVARIANT_WARNING": ("microscope.invariant-warning.v1", 1),
    "BRANCH_DIVERGENCE": ("microscope.branch-divergence.v1", 1),
}


class TimelinePlaybackState(str, Enum):
    PAUSED = "PAUSED"
    PLAYING = "PLAYING"


class TimelineDirection(str, Enum):
    PREVIOUS = "PREVIOUS"
    NEXT = "NEXT"


class TimelineEventKind(str, Enum):
    """Closed event classes used by stepping and the six required jumps."""

    OBSERVED_UPDATE = "OBSERVED_UPDATE"
    PLAYER_ACTION = "PLAYER_ACTION"
    FILL = "FILL"
    TRAFFIC_LIGHT_TRANSITION = "TRAFFIC_LIGHT_TRANSITION"
    REVEALED_REGIME_TRANSITION = "REVEALED_REGIME_TRANSITION"
    INVARIANT_WARNING = "INVARIANT_WARNING"
    BRANCH_DIVERGENCE = "BRANCH_DIVERGENCE"


class TimelineJumpTarget(str, Enum):
    PLAYER_ACTION = "PLAYER_ACTION"
    FILL = "FILL"
    TRAFFIC_LIGHT_TRANSITION = "TRAFFIC_LIGHT_TRANSITION"
    REVEALED_REGIME_TRANSITION = "REVEALED_REGIME_TRANSITION"
    INVARIANT_WARNING = "INVARIANT_WARNING"
    BRANCH_DIVERGENCE = "BRANCH_DIVERGENCE"


class TimelineEvidenceSource(str, Enum):
    CLIENT_DELIVERED = "CLIENT_DELIVERED"
    RECORDED_DECISION_SNAPSHOT = "RECORDED_DECISION_SNAPSHOT"
    AUTHORIZED_GROUND_TRUTH = "AUTHORIZED_GROUND_TRUTH"
    AUTHORIZED_HIDDEN_STATE = "AUTHORIZED_HIDDEN_STATE"
    DECLARED_DERIVATION = "DECLARED_DERIVATION"


class TimelineNavigationKind(str, Enum):
    EVENT_STEP = "EVENT_STEP"
    FIXED_TIME_STEP = "FIXED_TIME_STEP"
    JUMP = "JUMP"


class TimelineNavigationAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class TimelineNavigationUnavailableReason(str, Enum):
    TIMELINE_BOUNDARY = "TIMELINE_BOUNDARY"
    NO_MATCHING_EVENT_IN_DIRECTION = "NO_MATCHING_EVENT_IN_DIRECTION"
    REVEAL_NOT_AUTHORIZED = "REVEAL_NOT_AUTHORIZED"


class TimelineSidecarOperation(str, Enum):
    BOOKMARK = "BOOKMARK"
    ANNOTATE = "ANNOTATE"


class TimelineSidecarStatus(str, Enum):
    REFUSED = "REFUSED"


class TimelineSidecarRefusalReason(str, Enum):
    DEFERRED_TO_WO36_E = "DEFERRED_TO_WO36_E"


_JUMP_EVENT_KIND = {
    TimelineJumpTarget.PLAYER_ACTION: TimelineEventKind.PLAYER_ACTION,
    TimelineJumpTarget.FILL: TimelineEventKind.FILL,
    TimelineJumpTarget.TRAFFIC_LIGHT_TRANSITION: (
        TimelineEventKind.TRAFFIC_LIGHT_TRANSITION
    ),
    TimelineJumpTarget.REVEALED_REGIME_TRANSITION: (
        TimelineEventKind.REVEALED_REGIME_TRANSITION
    ),
    TimelineJumpTarget.INVARIANT_WARNING: TimelineEventKind.INVARIANT_WARNING,
    TimelineJumpTarget.BRANCH_DIVERGENCE: TimelineEventKind.BRANCH_DIVERGENCE,
}

_REVEAL_SOURCES = frozenset(
    {
        TimelineEvidenceSource.AUTHORIZED_GROUND_TRUTH,
        TimelineEvidenceSource.AUTHORIZED_HIDDEN_STATE,
    }
)


@dataclass(frozen=True, slots=True)
class TimelineDerivation:
    """A declared calculation over already policy-visible source events."""

    calculation_id: str
    calculation_version: int
    source_event_ids: tuple[str, ...]
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _EVENT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline derivations are produced only by derive_timeline_event")
        _identifier(self.calculation_id, "timeline calculation ID")
        _positive_exact_int(
            self.calculation_version,
            "timeline calculation version",
        )
        source_event_ids = _identifiers(
            self.source_event_ids,
            "timeline calculation source event IDs",
            empty=False,
        )
        object.__setattr__(self, "source_event_ids", source_event_ids)

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "calculation_version": self.calculation_version,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True, slots=True, repr=False)
class TimelineEvidenceEvent:
    """Backend input containing no market payload, only one event classification.

    Direct events link exactly one policy-enforced source event.  Derived events link
    one or more direct events and carry a versioned calculation.  Full instances are
    backend inventory and must not be handed to a cursor-facing consumer.
    """

    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    event_id: str
    event_kind: TimelineEventKind
    sequence: int
    source_event_time_us: int
    policy_visible_at_time_us: int
    source: TimelineEvidenceSource
    source_event_ids: tuple[str, ...]
    query_id: str | None = None
    query_render_cursor_time_us: int | None = None
    query_projection_sha256: str | None = field(default=None, repr=False)
    series_id: str | None = None
    payload_sha256: str | None = field(default=None, repr=False)
    source_evidence_sha256: str | None = field(default=None, repr=False)
    derivation: TimelineDerivation | None = None
    reveal_authorization_id: str | None = field(default=None, repr=False)
    reveal_evidence_sha256: str | None = field(default=None, repr=False)
    _construction_token: InitVar[object] = None
    schema_id: str = TIMELINE_EVENT_SCHEMA_ID
    schema_version: int = TIMELINE_EVENT_SCHEMA_VERSION

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _EVENT_CONSTRUCTION_TOKEN:
            raise TypeError(
                "timeline evidence events require a verified query or derivation factory"
            )
        _source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("timeline event observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("timeline event policy ID differs from its mode")
        _identifier(self.event_id, "timeline event ID")
        if type(self.event_kind) is not TimelineEventKind:
            raise TypeError("timeline event kind is invalid")
        _positive_exact_int(self.sequence, "timeline event sequence")
        _nonnegative_exact_int(
            self.source_event_time_us,
            "timeline source event time",
        )
        _nonnegative_exact_int(
            self.policy_visible_at_time_us,
            "timeline policy visibility time",
        )
        if self.policy_visible_at_time_us < self.source_event_time_us:
            raise ValueError("timeline event is visible before its source event")
        if type(self.source) is not TimelineEvidenceSource:
            raise TypeError("timeline event evidence source is invalid")
        source_event_ids = _identifiers(
            self.source_event_ids,
            "timeline source event IDs",
            empty=False,
        )
        object.__setattr__(self, "source_event_ids", source_event_ids)
        if (
            self.schema_id != TIMELINE_EVENT_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != TIMELINE_EVENT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported timeline event schema")

        if self.source is TimelineEvidenceSource.DECLARED_DERIVATION:
            if type(self.derivation) is not TimelineDerivation:
                raise ValueError("derived timeline event requires a calculation")
            if self.derivation.source_event_ids != source_event_ids:
                raise ValueError("timeline derivation source links differ")
            if self.reveal_authorization_id is not None:
                raise ValueError("derived timeline event carries reveal authorization")
            if self.reveal_evidence_sha256 is not None:
                raise ValueError("derived timeline event carries reveal evidence")
            if any(
                value is not None
                for value in (
                    self.query_id,
                    self.query_render_cursor_time_us,
                    self.query_projection_sha256,
                    self.series_id,
                    self.payload_sha256,
                    self.source_evidence_sha256,
                )
            ):
                raise ValueError("derived timeline event carries direct-query state")
            if self.event_kind not in {
                TimelineEventKind.INVARIANT_WARNING,
                TimelineEventKind.BRANCH_DIVERGENCE,
            }:
                raise ValueError("declared derivation has an unsupported event kind")
        else:
            if self.derivation is not None:
                raise ValueError("direct timeline event carries a derivation")
            if len(source_event_ids) != 1:
                raise ValueError("direct timeline event requires exactly one source event")
            if source_event_ids != (self.event_id,):
                raise ValueError("direct timeline event ID differs from its query event")
            if type(self.query_id) is not str or _QUERY_ID.fullmatch(self.query_id) is None:
                raise ValueError("direct timeline event query ID is invalid")
            _nonnegative_exact_int(
                self.query_render_cursor_time_us,
                "direct timeline event query cursor",
            )
            if self.query_render_cursor_time_us != self.policy_visible_at_time_us:
                raise ValueError("direct timeline event was not exact at its query cursor")
            _sha256(
                self.query_projection_sha256,
                "direct timeline query projection SHA-256",
            )
            _identifier(self.series_id, "direct timeline source series ID")
            _sha256(self.payload_sha256, "direct timeline source payload SHA-256")
            _sha256(
                self.source_evidence_sha256,
                "direct timeline source evidence SHA-256",
            )
            if self.source not in _REVEAL_SOURCES and (
                self.source_evidence_sha256 != self.query_projection_sha256
            ):
                raise ValueError("observed timeline source differs from its query projection")

        if self.source in _REVEAL_SOURCES:
            if self.observation_mode is not ObservationMode.POSTMORTEM:
                raise ValueError("reveal timeline event requires POSTMORTEM policy")
            _identifier(
                self.reveal_authorization_id,
                "timeline reveal authorization ID",
            )
            _sha256(
                self.reveal_evidence_sha256,
                "timeline reveal evidence SHA-256",
            )
            if self.policy_visible_at_time_us != self.source_event_time_us:
                raise ValueError("authorized reveal event visibility must equal source time")
            if self.source_evidence_sha256 != self.reveal_evidence_sha256:
                raise ValueError("timeline reveal source differs from its authorized evidence")
        elif (
            self.reveal_authorization_id is not None
            or self.reveal_evidence_sha256 is not None
        ):
            raise ValueError("non-reveal timeline event carries reveal state")

        if self.event_kind is TimelineEventKind.REVEALED_REGIME_TRANSITION:
            if self.source is not TimelineEvidenceSource.AUTHORIZED_GROUND_TRUTH:
                raise ValueError(
                    "revealed regime transition requires ground-truth evidence"
                )
        elif self.source in _REVEAL_SOURCES:
            raise ValueError("authorized reveal source has the wrong event kind")
        if self.event_kind is TimelineEventKind.BRANCH_DIVERGENCE and (
            self.source is not TimelineEvidenceSource.DECLARED_DERIVATION
        ):
            raise ValueError("branch divergence requires a declared derivation")
        if self.event_kind is TimelineEventKind.PLAYER_ACTION and (
            self.source is not TimelineEvidenceSource.RECORDED_DECISION_SNAPSHOT
        ):
            raise ValueError("player action must come from a decision snapshot")
        if self.event_kind is TimelineEventKind.FILL and (
            self.source is not TimelineEvidenceSource.CLIENT_DELIVERED
        ):
            raise ValueError("fill must come from client-delivered evidence")
        if self.event_kind is TimelineEventKind.TRAFFIC_LIGHT_TRANSITION and (
            self.source is not TimelineEvidenceSource.RECORDED_DECISION_SNAPSHOT
        ):
            raise ValueError("traffic-light transition must come from a decision snapshot")

    def __repr__(self) -> str:
        return (
            "TimelineEvidenceEvent(backend_inventory=True, "
            f"source_run_id={self.source_run_id!r}, event_id={self.event_id!r})"
        )

    def _receipt_dict(self) -> dict[str, object]:
        return {
            "derivation": (
                None if self.derivation is None else self.derivation.as_dict()
            ),
            "event_id": self.event_id,
            "event_kind": self.event_kind.value,
            "observation_mode": self.observation_mode.value,
            "payload_sha256": self.payload_sha256,
            "policy_id": self.policy_id,
            "policy_visible_at_time_us": self.policy_visible_at_time_us,
            "query_id": self.query_id,
            "query_projection_sha256": self.query_projection_sha256,
            "query_render_cursor_time_us": self.query_render_cursor_time_us,
            "reveal_authorization_id": self.reveal_authorization_id,
            "reveal_evidence_sha256": self.reveal_evidence_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "source": self.source.value,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_event_ids": list(self.source_event_ids),
            "source_event_sha256": self.source_event_sha256,
            "source_event_time_us": self.source_event_time_us,
            "source_run_id": self.source_run_id,
            "series_id": self.series_id,
        }


@dataclass(frozen=True, slots=True)
class TimelineEventLink:
    """Cursor-safe reference for an event at the selected cursor only."""

    event_id: str
    event_kind: TimelineEventKind
    sequence: int
    source_event_time_us: int
    policy_visible_at_time_us: int
    source_event_ids: tuple[str, ...]
    derivation: TimelineDerivation | None
    query_id: str | None
    query_projection_sha256: str | None = field(repr=False)
    series_id: str | None
    payload_sha256: str | None = field(repr=False)
    source_evidence_sha256: str | None = field(repr=False)
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _OUTPUT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline event links are facade outputs only")
        _identifier(self.event_id, "timeline event-link ID")
        if type(self.event_kind) is not TimelineEventKind:
            raise TypeError("timeline event-link kind is invalid")
        _positive_exact_int(self.sequence, "timeline event-link sequence")
        _nonnegative_exact_int(
            self.source_event_time_us,
            "timeline event-link source time",
        )
        _nonnegative_exact_int(
            self.policy_visible_at_time_us,
            "timeline event-link visibility time",
        )
        if self.policy_visible_at_time_us < self.source_event_time_us:
            raise ValueError("timeline event-link is visible before its source")
        source_event_ids = _identifiers(
            self.source_event_ids,
            "timeline event-link sources",
            empty=False,
        )
        object.__setattr__(self, "source_event_ids", source_event_ids)
        if self.derivation is not None:
            if type(self.derivation) is not TimelineDerivation:
                raise TypeError("timeline event-link derivation is invalid")
            if self.derivation.source_event_ids != source_event_ids:
                raise ValueError("timeline event-link derivation sources differ")
            if any(
                item is not None
                for item in (
                    self.query_id,
                    self.query_projection_sha256,
                    self.series_id,
                    self.payload_sha256,
                    self.source_evidence_sha256,
                )
            ):
                raise ValueError("derived timeline event link carries direct-query state")
        else:
            if type(self.query_id) is not str or _QUERY_ID.fullmatch(self.query_id) is None:
                raise ValueError("direct timeline event-link query ID is invalid")
            _sha256(
                self.query_projection_sha256,
                "timeline event-link query projection SHA-256",
            )
            _identifier(self.series_id, "timeline event-link series ID")
            _sha256(self.payload_sha256, "timeline event-link payload SHA-256")
            _sha256(
                self.source_evidence_sha256,
                "timeline event-link source evidence SHA-256",
            )
        if (
            self.event_kind is TimelineEventKind.BRANCH_DIVERGENCE
            and self.derivation is None
        ):
            raise ValueError("branch-divergence event link lacks its derivation")

    def as_dict(self) -> dict[str, object]:
        return {
            "derivation": (
                None if self.derivation is None else self.derivation.as_dict()
            ),
            "event_id": self.event_id,
            "event_kind": self.event_kind.value,
            "policy_visible_at_time_us": self.policy_visible_at_time_us,
            "query_id": self.query_id,
            "query_projection_sha256": self.query_projection_sha256,
            "sequence": self.sequence,
            "series_id": self.series_id,
            "payload_sha256": self.payload_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_event_ids": list(self.source_event_ids),
            "source_event_time_us": self.source_event_time_us,
        }


@dataclass(frozen=True, slots=True)
class TimelineCursor:
    """One immutable integer simulation-time cursor and its current partition."""

    timeline_id: str
    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    playback_state: TimelinePlaybackState
    _facade_binding: object = field(repr=False, compare=False)
    _construction_token: InitVar[object]
    current_events: tuple[TimelineEventLink, ...] = ()
    schema_id: str = TIMELINE_CURSOR_SCHEMA_ID
    schema_version: int = TIMELINE_CURSOR_SCHEMA_VERSION
    cursor_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _OUTPUT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline cursors are produced only by ReplayTimeline")
        if type(self._facade_binding) is not object:
            raise TypeError("timeline cursor facade binding is invalid")
        _identifier(self.timeline_id, "timeline ID")
        _source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("timeline cursor observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("timeline cursor policy ID differs from its mode")
        _nonnegative_exact_int(
            self.render_cursor_time_us,
            "timeline render cursor",
        )
        if type(self.playback_state) is not TimelinePlaybackState:
            raise TypeError("timeline playback state is invalid")
        if type(self.current_events) is not tuple or any(
            type(item) is not TimelineEventLink for item in self.current_events
        ):
            raise TypeError("timeline cursor current events are invalid")
        events = tuple(
            sorted(self.current_events, key=lambda item: (item.sequence, item.event_id))
        )
        if len({item.event_id for item in events}) != len(events):
            raise ValueError("timeline cursor contains duplicate events")
        if len({item.sequence for item in events}) != len(events):
            raise ValueError("timeline cursor contains duplicate event sequences")
        if any(
            item.policy_visible_at_time_us != self.render_cursor_time_us
            for item in events
        ):
            raise ValueError("timeline cursor event belongs to another partition")
        if (
            self.observation_mode is ObservationMode.AS_OBSERVED
            and any(
                item.event_kind is TimelineEventKind.REVEALED_REGIME_TRANSITION
                for item in events
            )
        ):
            raise ValueError("AS_OBSERVED cursor contains a revealed event")
        object.__setattr__(self, "current_events", events)
        if (
            self.schema_id != TIMELINE_CURSOR_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != TIMELINE_CURSOR_SCHEMA_VERSION
        ):
            raise ValueError("unsupported timeline cursor schema")
        object.__setattr__(
            self,
            "cursor_id",
            "timeline-cursor-" + _canonical_sha256(self._identity_dict())[:24],
        )

    @property
    def simulation_time_us(self) -> int:
        return self.render_cursor_time_us

    def _identity_dict(self) -> dict[str, object]:
        return {
            "current_events": [item.as_dict() for item in self.current_events],
            "observation_mode": self.observation_mode.value,
            "playback_state": self.playback_state.value,
            "policy_id": self.policy_id,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "timeline_id": self.timeline_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_dict(), "cursor_id": self.cursor_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True, repr=False)
class TimelineReceipt:
    """Backend-only verification of the complete timeline inventory.

    Counts, bounds, partition commitments, authorization identities, and full-run
    inventory commitments are future/full-run side channels.  Do not expose this
    object or its serialization through cursor, pane, screenshot, or report APIs.
    """

    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    timeline_id: str
    root_query_id: str
    root_query_sha256: str
    root_render_cursor_time_us: int
    reveal_availability: RevealAvailability
    event_inventory_sha256: str
    event_count: int
    partition_inventory_sha256: str
    partition_count: int
    minimum_cursor_time_us: int | None
    maximum_cursor_time_us: int | None
    reveal_authorization_ids: tuple[str, ...]
    reveal_evidence_sha256s: tuple[str, ...]
    _construction_token: InitVar[object]
    schema_id: str = TIMELINE_RECEIPT_SCHEMA_ID
    schema_version: int = TIMELINE_RECEIPT_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _OUTPUT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline receipts are produced only by verified assembly")
        _source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("timeline receipt observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("timeline receipt policy ID differs from its mode")
        _identifier(self.timeline_id, "timeline receipt timeline ID")
        if type(self.root_query_id) is not str or not _QUERY_ID.fullmatch(
            self.root_query_id
        ):
            raise ValueError("timeline receipt root query ID is invalid")
        _sha256(self.root_query_sha256, "timeline receipt root query SHA-256")
        _nonnegative_exact_int(
            self.root_render_cursor_time_us,
            "timeline receipt root query cursor",
        )
        if type(self.reveal_availability) is not RevealAvailability:
            raise TypeError("timeline receipt reveal availability is invalid")
        _sha256(self.event_inventory_sha256, "timeline event inventory SHA-256")
        if self.timeline_id != _timeline_id(
            self.source_run_id,
            self.source_event_sha256,
            self.observation_mode,
            self.policy_id,
        ):
            raise ValueError("timeline receipt identity differs from its public source")
        _nonnegative_exact_int(self.event_count, "timeline event count")
        _sha256(
            self.partition_inventory_sha256,
            "timeline partition inventory SHA-256",
        )
        _nonnegative_exact_int(self.partition_count, "timeline partition count")
        if self.event_count == 0:
            if (
                self.partition_count != 0
                or self.minimum_cursor_time_us is not None
                or self.maximum_cursor_time_us is not None
            ):
                raise ValueError("empty timeline receipt carries partitions or bounds")
        else:
            if self.partition_count <= 0:
                raise ValueError("nonempty timeline receipt lacks partitions")
            if self.partition_count > self.event_count:
                raise ValueError("timeline receipt has more partitions than events")
            _nonnegative_exact_int(
                self.minimum_cursor_time_us,
                "timeline minimum cursor time",
            )
            _nonnegative_exact_int(
                self.maximum_cursor_time_us,
                "timeline maximum cursor time",
            )
            if self.minimum_cursor_time_us > self.maximum_cursor_time_us:
                raise ValueError("timeline receipt bounds are reversed")
        authorization_ids = _identifiers(
            self.reveal_authorization_ids,
            "timeline receipt reveal authorization IDs",
            empty=True,
        )
        reveal_sha256s = _sha256s(
            self.reveal_evidence_sha256s,
            "timeline receipt reveal evidence SHA-256s",
        )
        if self.observation_mode is ObservationMode.AS_OBSERVED:
            if self.reveal_availability is not RevealAvailability.NOT_REQUESTED:
                raise ValueError("AS_OBSERVED timeline receipt has a reveal decision")
            if authorization_ids or reveal_sha256s:
                raise ValueError("AS_OBSERVED timeline receipt contains reveal state")
        elif self.reveal_availability is RevealAvailability.NOT_REQUESTED:
            raise ValueError("POSTMORTEM timeline receipt lacks a reveal decision")
        if len(authorization_ids) != len(reveal_sha256s):
            raise ValueError("timeline receipt reveal bindings are incomplete")
        if len(authorization_ids) > 1:
            raise ValueError("timeline receipt spans multiple reveal grants")
        if self.reveal_availability is RevealAvailability.AVAILABLE:
            if len(authorization_ids) != 1:
                raise ValueError("available reveal receipt lacks its exact grant")
        elif authorization_ids or reveal_sha256s:
            raise ValueError("unavailable reveal receipt contains protected bindings")
        object.__setattr__(self, "reveal_authorization_ids", authorization_ids)
        object.__setattr__(self, "reveal_evidence_sha256s", reveal_sha256s)
        if (
            self.schema_id != TIMELINE_RECEIPT_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != TIMELINE_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported timeline receipt schema")
        object.__setattr__(
            self,
            "receipt_sha256",
            _canonical_sha256(self._identity_dict()),
        )

    def __repr__(self) -> str:
        return (
            "TimelineReceipt(backend_only=True, "
            f"source_run_id={self.source_run_id!r}, receipt_sha256={self.receipt_sha256!r})"
        )

    def _identity_dict(self) -> dict[str, object]:
        return {
            "event_count": self.event_count,
            "event_inventory_sha256": self.event_inventory_sha256,
            "maximum_cursor_time_us": self.maximum_cursor_time_us,
            "minimum_cursor_time_us": self.minimum_cursor_time_us,
            "observation_mode": self.observation_mode.value,
            "partition_count": self.partition_count,
            "partition_inventory_sha256": self.partition_inventory_sha256,
            "policy_id": self.policy_id,
            "reveal_availability": self.reveal_availability.value,
            "reveal_authorization_ids": list(self.reveal_authorization_ids),
            "reveal_evidence_sha256s": list(self.reveal_evidence_sha256s),
            "root_query_id": self.root_query_id,
            "root_query_sha256": self.root_query_sha256,
            "root_render_cursor_time_us": self.root_render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "timeline_id": self.timeline_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_dict(), "receipt_sha256": self.receipt_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class TimelineNavigationResult:
    operation: TimelineNavigationKind
    availability: TimelineNavigationAvailability
    direction: TimelineDirection
    origin_cursor_time_us: int
    cursor: TimelineCursor
    _construction_token: InitVar[object]
    selected_events: tuple[TimelineEventLink, ...] = ()
    jump_target: TimelineJumpTarget | None = None
    fixed_step_us: int | None = None
    unavailable_reason: TimelineNavigationUnavailableReason | None = None
    schema_id: str = TIMELINE_NAVIGATION_SCHEMA_ID
    schema_version: int = TIMELINE_NAVIGATION_SCHEMA_VERSION

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _OUTPUT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline navigation results are facade outputs only")
        if type(self.operation) is not TimelineNavigationKind:
            raise TypeError("timeline navigation operation is invalid")
        if type(self.availability) is not TimelineNavigationAvailability:
            raise TypeError("timeline navigation availability is invalid")
        if type(self.direction) is not TimelineDirection:
            raise TypeError("timeline navigation direction is invalid")
        _nonnegative_exact_int(
            self.origin_cursor_time_us,
            "timeline navigation origin cursor",
        )
        if type(self.cursor) is not TimelineCursor:
            raise TypeError("timeline navigation cursor is invalid")
        if type(self.selected_events) is not tuple or any(
            type(item) is not TimelineEventLink for item in self.selected_events
        ):
            raise TypeError("timeline navigation selected events are invalid")
        selected_events = tuple(
            sorted(self.selected_events, key=lambda item: (item.sequence, item.event_id))
        )
        if len({item.event_id for item in selected_events}) != len(selected_events):
            raise ValueError("timeline navigation selected events are duplicated")
        object.__setattr__(self, "selected_events", selected_events)

        if self.operation is TimelineNavigationKind.JUMP:
            if type(self.jump_target) is not TimelineJumpTarget:
                raise ValueError("timeline jump requires a closed target")
            if self.fixed_step_us is not None:
                raise ValueError("timeline jump carries a fixed step")
        elif self.operation is TimelineNavigationKind.FIXED_TIME_STEP:
            if self.jump_target is not None:
                raise ValueError("fixed-time step carries a jump target")
            _positive_exact_int(self.fixed_step_us, "timeline fixed step")
            if selected_events:
                raise ValueError("fixed-time step cannot claim selected events")
        elif self.jump_target is not None or self.fixed_step_us is not None:
            raise ValueError("event step carries unrelated operation state")

        if self.availability is TimelineNavigationAvailability.AVAILABLE:
            if self.unavailable_reason is not None:
                raise ValueError("available navigation carries an absence reason")
            if self.operation is not TimelineNavigationKind.FIXED_TIME_STEP:
                if not selected_events:
                    raise ValueError("event navigation lacks its selected events")
                if any(
                    item.policy_visible_at_time_us
                    != self.cursor.render_cursor_time_us
                    for item in selected_events
                ):
                    raise ValueError("selected event differs from destination cursor")
                cursor_events = {
                    canonical_json_bytes(item.as_dict())
                    for item in self.cursor.current_events
                }
                if any(
                    canonical_json_bytes(item.as_dict()) not in cursor_events
                    for item in selected_events
                ):
                    raise ValueError("selected event is absent from destination cursor")
                if self.operation is TimelineNavigationKind.EVENT_STEP and (
                    selected_events != self.cursor.current_events
                ):
                    raise ValueError("event step does not expose its exact partition")
                if self.operation is TimelineNavigationKind.JUMP:
                    if self.jump_target is None:  # pragma: no cover - validated above
                        raise RuntimeError("validated jump target disappeared")
                    expected_kind = _JUMP_EVENT_KIND[self.jump_target]
                    if any(
                        item.event_kind is not expected_kind
                        for item in selected_events
                    ):
                        raise ValueError("jump selected an event outside its target kind")
                    exact_target_partition = tuple(
                        item
                        for item in self.cursor.current_events
                        if item.event_kind is expected_kind
                    )
                    if selected_events != exact_target_partition:
                        raise ValueError("jump omitted a matching event at its partition")
                if self.direction is TimelineDirection.NEXT and (
                    self.cursor.render_cursor_time_us <= self.origin_cursor_time_us
                ):
                    raise ValueError("next event navigation did not advance")
                if self.direction is TimelineDirection.PREVIOUS and (
                    self.cursor.render_cursor_time_us >= self.origin_cursor_time_us
                ):
                    raise ValueError("previous event navigation did not retreat")
            elif self.direction is TimelineDirection.NEXT:
                if self.cursor.render_cursor_time_us != (
                    self.origin_cursor_time_us + self.fixed_step_us
                ):
                    raise ValueError("next fixed-time step has the wrong destination")
            elif self.cursor.render_cursor_time_us != max(
                0,
                self.origin_cursor_time_us - self.fixed_step_us,
            ):
                raise ValueError("previous fixed-time step has the wrong destination")
        else:
            if self.operation is TimelineNavigationKind.FIXED_TIME_STEP:
                raise ValueError("fixed-time navigation cannot be unavailable")
            if type(self.unavailable_reason) is not TimelineNavigationUnavailableReason:
                raise ValueError("unavailable navigation requires a typed reason")
            if selected_events:
                raise ValueError("unavailable navigation exposes selected events")
            if self.cursor.render_cursor_time_us != self.origin_cursor_time_us:
                raise ValueError("unavailable navigation moved the cursor")
            if (
                self.operation is TimelineNavigationKind.EVENT_STEP
                and self.unavailable_reason
                is not TimelineNavigationUnavailableReason.TIMELINE_BOUNDARY
            ):
                raise ValueError("event-step unavailability reason is inconsistent")
            if self.operation is TimelineNavigationKind.JUMP:
                observed_reveal_refusal = (
                    self.jump_target
                    is TimelineJumpTarget.REVEALED_REGIME_TRANSITION
                    and self.cursor.observation_mode is ObservationMode.AS_OBSERVED
                )
                if self.jump_target is TimelineJumpTarget.REVEALED_REGIME_TRANSITION:
                    allowed_reasons = (
                        {TimelineNavigationUnavailableReason.REVEAL_NOT_AUTHORIZED}
                        if observed_reveal_refusal
                        else {
                            TimelineNavigationUnavailableReason.REVEAL_NOT_AUTHORIZED,
                            TimelineNavigationUnavailableReason.NO_MATCHING_EVENT_IN_DIRECTION,
                        }
                    )
                else:
                    allowed_reasons = {
                        TimelineNavigationUnavailableReason.NO_MATCHING_EVENT_IN_DIRECTION
                    }
                if self.unavailable_reason not in allowed_reasons:
                    raise ValueError("jump unavailability reason is inconsistent")
        if (
            self.schema_id != TIMELINE_NAVIGATION_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != TIMELINE_NAVIGATION_SCHEMA_VERSION
        ):
            raise ValueError("unsupported timeline navigation schema")

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "cursor": self.cursor.as_dict(),
            "direction": self.direction.value,
            "fixed_step_us": self.fixed_step_us,
            "jump_target": (
                None if self.jump_target is None else self.jump_target.value
            ),
            "operation": self.operation.value,
            "origin_cursor_time_us": self.origin_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "selected_events": [item.as_dict() for item in self.selected_events],
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class TimelineSidecarRefusal:
    operation: TimelineSidecarOperation
    status: TimelineSidecarStatus
    reason: TimelineSidecarRefusalReason
    timeline_id: str
    source_run_id: str
    source_event_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _OUTPUT_CONSTRUCTION_TOKEN:
            raise TypeError("timeline sidecar refusals are facade outputs only")
        if type(self.operation) is not TimelineSidecarOperation:
            raise TypeError("timeline sidecar operation is invalid")
        if self.status is not TimelineSidecarStatus.REFUSED:
            raise ValueError("timeline sidecar operation must be refused in WO36-C")
        if self.reason is not TimelineSidecarRefusalReason.DEFERRED_TO_WO36_E:
            raise ValueError("timeline sidecar refusal reason is not exact")
        _identifier(self.timeline_id, "timeline sidecar timeline ID")
        _source_identity(self.source_run_id, self.source_event_sha256)
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("timeline sidecar observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("timeline sidecar policy differs from its mode")
        _nonnegative_exact_int(
            self.render_cursor_time_us,
            "timeline sidecar render cursor",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_mode": self.observation_mode.value,
            "operation": self.operation.value,
            "policy_id": self.policy_id,
            "reason": self.reason.value,
            "render_cursor_time_us": self.render_cursor_time_us,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "status": self.status.value,
            "timeline_id": self.timeline_id,
        }


_TIMELINE_CONSTRUCTION_TOKEN = object()


class ReplayTimeline:
    """Cursor-safe facade over a private, immutable timeline inventory."""

    __slots__ = (
        "__cursor_binding",
        "__events",
        "__events_by_time",
        "__inventory_sha256",
        "__observation_mode",
        "__policy_id",
        "__reveal_authorized",
        "__sealed",
        "__source_event_sha256",
        "__source_run_id",
        "__timeline_id",
    )

    def __init__(
        self,
        source_run_id: str,
        source_event_sha256: str,
        observation_mode: ObservationMode,
        events: tuple[TimelineEvidenceEvent, ...],
        inventory_sha256: str,
        reveal_authorized: bool,
        *,
        _construction_token: object | None = None,
    ) -> None:
        if _construction_token is not _TIMELINE_CONSTRUCTION_TOKEN:
            raise TypeError("ReplayTimeline must be built by build_replay_timeline")
        _sha256(inventory_sha256, "timeline private inventory SHA-256")
        if type(reveal_authorized) is not bool:
            raise TypeError("timeline reveal authority flag is invalid")
        policy_id = ObservationPolicy(observation_mode).policy_id
        timeline_id = _timeline_id(
            source_run_id,
            source_event_sha256,
            observation_mode,
            policy_id,
        )
        events_by_time: dict[int, list[TimelineEvidenceEvent]] = {}
        for event in events:
            events_by_time.setdefault(event.policy_visible_at_time_us, []).append(event)
        object.__setattr__(self, "_ReplayTimeline__sealed", False)
        object.__setattr__(self, "_ReplayTimeline__source_run_id", source_run_id)
        object.__setattr__(
            self,
            "_ReplayTimeline__source_event_sha256",
            source_event_sha256,
        )
        object.__setattr__(
            self,
            "_ReplayTimeline__observation_mode",
            observation_mode,
        )
        object.__setattr__(self, "_ReplayTimeline__policy_id", policy_id)
        object.__setattr__(
            self,
            "_ReplayTimeline__reveal_authorized",
            reveal_authorized,
        )
        object.__setattr__(self, "_ReplayTimeline__timeline_id", timeline_id)
        object.__setattr__(self, "_ReplayTimeline__cursor_binding", object())
        object.__setattr__(
            self,
            "_ReplayTimeline__inventory_sha256",
            inventory_sha256,
        )
        object.__setattr__(self, "_ReplayTimeline__events", events)
        object.__setattr__(
            self,
            "_ReplayTimeline__events_by_time",
            MappingProxyType(
                {
                    cursor_time: tuple(
                        sorted(items, key=lambda item: (item.sequence, item.event_id))
                    )
                    for cursor_time, items in events_by_time.items()
                }
            ),
        )
        object.__setattr__(self, "_ReplayTimeline__sealed", True)

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("ReplayTimeline is closed to subclassing")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_ReplayTimeline__sealed", False):
            raise AttributeError("ReplayTimeline is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return (
            "ReplayTimeline("
            f"source_run_id={self.source_run_id!r}, "
            f"observation_mode={self.observation_mode.value!r})"
        )

    def __reduce__(self) -> object:
        raise TypeError("ReplayTimeline is not serializable")

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
    def timeline_id(self) -> str:
        return self.__timeline_id

    def as_dict(self) -> dict[str, object]:
        """Return cursor-safe root metadata, never full inventory facts."""

        return {
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "schema_id": TIMELINE_SCHEMA_ID,
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "timeline_id": self.timeline_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def cursor(
        self,
        render_cursor_time_us: int,
        *,
        playback_state: TimelinePlaybackState = TimelinePlaybackState.PAUSED,
    ) -> TimelineCursor:
        """Select one time directly without exposing any other partition."""

        _nonnegative_exact_int(render_cursor_time_us, "timeline render cursor")
        if type(playback_state) is not TimelinePlaybackState:
            raise TypeError("timeline playback state is invalid")
        return self.__cursor(render_cursor_time_us, playback_state)

    def play(self, cursor: TimelineCursor) -> TimelineCursor:
        """Return a playing cursor without mutating the supplied cursor."""

        self.__validate_cursor(cursor)
        return self.__cursor(
            cursor.render_cursor_time_us,
            TimelinePlaybackState.PLAYING,
        )

    def pause(self, cursor: TimelineCursor) -> TimelineCursor:
        """Return a paused cursor without mutating the supplied cursor."""

        self.__validate_cursor(cursor)
        return self.__cursor(
            cursor.render_cursor_time_us,
            TimelinePlaybackState.PAUSED,
        )

    def step_event(
        self,
        cursor: TimelineCursor,
        direction: TimelineDirection,
    ) -> TimelineNavigationResult:
        """Move to the nearest distinct policy-visible event partition."""

        self.__validate_cursor(cursor)
        _direction(direction)
        destination = _nearest_time(
            tuple(self.__events_by_time),
            cursor.render_cursor_time_us,
            direction,
        )
        if destination is None:
            return self.__unavailable(
                TimelineNavigationKind.EVENT_STEP,
                cursor,
                direction,
                TimelineNavigationUnavailableReason.TIMELINE_BOUNDARY,
            )
        moved = self.__cursor(destination, cursor.playback_state)
        return TimelineNavigationResult(
            operation=TimelineNavigationKind.EVENT_STEP,
            availability=TimelineNavigationAvailability.AVAILABLE,
            direction=direction,
            origin_cursor_time_us=cursor.render_cursor_time_us,
            cursor=moved,
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
            selected_events=moved.current_events,
        )

    def step_fixed_time(
        self,
        cursor: TimelineCursor,
        step_us: int,
        direction: TimelineDirection,
    ) -> TimelineNavigationResult:
        """Move by an exact positive duration; previous steps saturate at zero."""

        self.__validate_cursor(cursor)
        _positive_exact_int(step_us, "timeline fixed step")
        _direction(direction)
        if direction is TimelineDirection.NEXT:
            destination = cursor.render_cursor_time_us + step_us
        else:
            destination = max(0, cursor.render_cursor_time_us - step_us)
        moved = self.__cursor(destination, cursor.playback_state)
        return TimelineNavigationResult(
            operation=TimelineNavigationKind.FIXED_TIME_STEP,
            availability=TimelineNavigationAvailability.AVAILABLE,
            direction=direction,
            origin_cursor_time_us=cursor.render_cursor_time_us,
            cursor=moved,
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
            fixed_step_us=step_us,
        )

    def jump(
        self,
        cursor: TimelineCursor,
        target: TimelineJumpTarget,
        direction: TimelineDirection,
    ) -> TimelineNavigationResult:
        """Move only to the requested nearest event class in one direction."""

        self.__validate_cursor(cursor)
        if type(target) is not TimelineJumpTarget:
            raise TypeError("timeline jump target is invalid")
        _direction(direction)
        if (
            target is TimelineJumpTarget.REVEALED_REGIME_TRANSITION
            and not self.__reveal_authorized
        ):
            return self.__unavailable(
                TimelineNavigationKind.JUMP,
                cursor,
                direction,
                TimelineNavigationUnavailableReason.REVEAL_NOT_AUTHORIZED,
                jump_target=target,
            )
        event_kind = _JUMP_EVENT_KIND[target]
        candidates = tuple(
            event for event in self.__events if event.event_kind is event_kind
        )
        destination = _nearest_time(
            tuple(item.policy_visible_at_time_us for item in candidates),
            cursor.render_cursor_time_us,
            direction,
        )
        if destination is None:
            return self.__unavailable(
                TimelineNavigationKind.JUMP,
                cursor,
                direction,
                TimelineNavigationUnavailableReason.NO_MATCHING_EVENT_IN_DIRECTION,
                jump_target=target,
            )
        selected = tuple(
            _event_link(item)
            for item in candidates
            if item.policy_visible_at_time_us == destination
        )
        return TimelineNavigationResult(
            operation=TimelineNavigationKind.JUMP,
            availability=TimelineNavigationAvailability.AVAILABLE,
            direction=direction,
            origin_cursor_time_us=cursor.render_cursor_time_us,
            cursor=self.__cursor(destination, cursor.playback_state),
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
            selected_events=selected,
            jump_target=target,
        )

    def bookmark(self, cursor: TimelineCursor) -> TimelineSidecarRefusal:
        """Refuse bookmark persistence until the WO36-E sidecar contract exists."""

        self.__validate_cursor(cursor)
        return self.__sidecar_refusal(TimelineSidecarOperation.BOOKMARK, cursor)

    def annotate(self, cursor: TimelineCursor) -> TimelineSidecarRefusal:
        """Refuse annotation persistence until the WO36-E sidecar contract exists."""

        self.__validate_cursor(cursor)
        return self.__sidecar_refusal(TimelineSidecarOperation.ANNOTATE, cursor)

    def __cursor(
        self,
        render_cursor_time_us: int,
        playback_state: TimelinePlaybackState,
    ) -> TimelineCursor:
        events = tuple(
            _event_link(item)
            for item in self.__events_by_time.get(render_cursor_time_us, ())
        )
        return TimelineCursor(
            timeline_id=self.timeline_id,
            source_run_id=self.source_run_id,
            source_event_sha256=self.source_event_sha256,
            observation_mode=self.observation_mode,
            policy_id=self.policy_id,
            render_cursor_time_us=render_cursor_time_us,
            playback_state=playback_state,
            _facade_binding=self.__cursor_binding,
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
            current_events=events,
        )

    def __validate_cursor(self, cursor: TimelineCursor) -> None:
        if type(cursor) is not TimelineCursor:
            raise TypeError("timeline operation requires TimelineCursor")
        if cursor._facade_binding is not self.__cursor_binding:
            raise ValueError("timeline cursor belongs to another facade")
        expected = self.__cursor(
            cursor.render_cursor_time_us,
            cursor.playback_state,
        )
        if cursor.canonical_bytes() != expected.canonical_bytes():
            raise ValueError("timeline cursor does not belong to this exact facade")

    def __unavailable(
        self,
        operation: TimelineNavigationKind,
        cursor: TimelineCursor,
        direction: TimelineDirection,
        reason: TimelineNavigationUnavailableReason,
        *,
        jump_target: TimelineJumpTarget | None = None,
    ) -> TimelineNavigationResult:
        return TimelineNavigationResult(
            operation=operation,
            availability=TimelineNavigationAvailability.UNAVAILABLE,
            direction=direction,
            origin_cursor_time_us=cursor.render_cursor_time_us,
            cursor=cursor,
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
            jump_target=jump_target,
            unavailable_reason=reason,
        )

    def __sidecar_refusal(
        self,
        operation: TimelineSidecarOperation,
        cursor: TimelineCursor,
    ) -> TimelineSidecarRefusal:
        return TimelineSidecarRefusal(
            operation=operation,
            status=TimelineSidecarStatus.REFUSED,
            reason=TimelineSidecarRefusalReason.DEFERRED_TO_WO36_E,
            timeline_id=self.timeline_id,
            source_run_id=self.source_run_id,
            source_event_sha256=self.source_event_sha256,
            observation_mode=self.observation_mode,
            policy_id=self.policy_id,
            render_cursor_time_us=cursor.render_cursor_time_us,
            _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
        )


def timeline_event_from_query_result(
    result: ObservationQueryResult,
    event_id: str,
    event_kind: TimelineEventKind | None = None,
) -> TimelineEvidenceEvent:
    """Classify one value recorded exactly at a policy-enforced query cursor.

    Held-last-known values are refused: they are state at a cursor, not new timeline
    events.  The returned object contains no payload and is intended only for the
    backend timeline builder.
    """

    if type(result) is not ObservationQueryResult:
        raise TypeError("timeline event adapter requires ObservationQueryResult")
    _identifier(event_id, "timeline source query event ID")
    if event_kind is not None and type(event_kind) is not TimelineEventKind:
        raise TypeError("timeline source query event kind is invalid")
    matches = tuple(item for item in result.values if item.event_id == event_id)
    if len(matches) != 1:
        raise ValueError("timeline source query must select exactly one event ID")
    value = matches[0]
    if (
        value.selection is not SelectionKind.EXACT_RECORDED
        or value.data_age.policy_visible_at_time_us
        != result.request.render_cursor_time_us
    ):
        raise ValueError("timeline source query event is held rather than newly visible")
    source = _timeline_source(value)
    classified_kind = _classify_timeline_value(value, source)
    if event_kind is not None and event_kind is not classified_kind:
        raise ValueError("caller-supplied timeline kind differs from the closed contract")
    authorization_id: str | None = None
    reveal_evidence_sha256: str | None = None
    if source in _REVEAL_SOURCES:
        if (
            result.reveal.availability is not RevealAvailability.AVAILABLE
            or result.reveal.authorization_id is None
            or result.reveal_evidence_sha256 is None
        ):
            raise ValueError("timeline reveal event lacks an authorized query result")
        authorization_id = result.reveal.authorization_id
        reveal_evidence_sha256 = result.reveal_evidence_sha256
    return TimelineEvidenceEvent(
        source_run_id=result.source_run_id,
        source_event_sha256=result.source_event_sha256,
        observation_mode=result.policy.mode,
        policy_id=result.policy.policy_id,
        event_id=value.event_id,
        event_kind=classified_kind,
        sequence=value.sequence,
        source_event_time_us=value.data_age.source_event_time_us,
        policy_visible_at_time_us=value.data_age.policy_visible_at_time_us,
        source=source,
        source_event_ids=(value.event_id,),
        query_id=result.query_id,
        query_render_cursor_time_us=result.request.render_cursor_time_us,
        query_projection_sha256=result.observed_projection_sha256,
        series_id=value.series_id,
        payload_sha256=value.payload_sha256,
        source_evidence_sha256=value.source_evidence_sha256,
        reveal_authorization_id=authorization_id,
        reveal_evidence_sha256=reveal_evidence_sha256,
        _construction_token=_EVENT_CONSTRUCTION_TOKEN,
    )


def derive_timeline_event(
    event_id: str,
    event_kind: TimelineEventKind,
    sequence: int,
    source_events: tuple[TimelineEvidenceEvent, ...],
) -> TimelineEvidenceEvent:
    """Declare one closed derived jump event over verified direct query events."""

    _identifier(event_id, "derived timeline event ID")
    if event_kind not in {
        TimelineEventKind.INVARIANT_WARNING,
        TimelineEventKind.BRANCH_DIVERGENCE,
    }:
        raise ValueError("derived timeline event kind is not supported")
    _positive_exact_int(sequence, "derived timeline event sequence")
    if type(source_events) is not tuple or not source_events or any(
        type(item) is not TimelineEvidenceEvent for item in source_events
    ):
        raise TypeError("derived timeline sources must be a nonempty exact tuple")
    if any(
        item.source is TimelineEvidenceSource.DECLARED_DERIVATION
        for item in source_events
    ):
        raise ValueError("timeline derivations must cite direct query events")
    first = source_events[0]
    if any(
        item.source_run_id != first.source_run_id
        or item.source_event_sha256 != first.source_event_sha256
        or item.observation_mode is not first.observation_mode
        or item.policy_id != first.policy_id
        for item in source_events
    ):
        raise ValueError("timeline derivation sources span source or policy roots")
    source_event_ids = tuple(
        sorted({source_id for item in source_events for source_id in item.source_event_ids})
    )
    if (
        event_kind is TimelineEventKind.BRANCH_DIVERGENCE
        and len(source_event_ids) < 2
    ):
        raise ValueError("branch divergence requires at least two source events")
    calculation_id, calculation_version = _DERIVED_CALCULATION[event_kind.value]
    derivation = TimelineDerivation(
        calculation_id,
        calculation_version,
        source_event_ids,
        _EVENT_CONSTRUCTION_TOKEN,
    )
    return TimelineEvidenceEvent(
        source_run_id=first.source_run_id,
        source_event_sha256=first.source_event_sha256,
        observation_mode=first.observation_mode,
        policy_id=first.policy_id,
        event_id=event_id,
        event_kind=event_kind,
        sequence=sequence,
        source_event_time_us=max(
            item.source_event_time_us for item in source_events
        ),
        policy_visible_at_time_us=max(
            item.policy_visible_at_time_us for item in source_events
        ),
        source=TimelineEvidenceSource.DECLARED_DERIVATION,
        source_event_ids=source_event_ids,
        derivation=derivation,
        _construction_token=_EVENT_CONSTRUCTION_TOKEN,
    )


def build_replay_timeline(
    root_query: ObservationQueryResult,
    events: tuple[TimelineEvidenceEvent, ...],
) -> tuple[ReplayTimeline, TimelineReceipt]:
    """Build one inventory under an exact policy/query authority root.

    In particular, ``POSTMORTEM`` is only a mode label.  Reveal navigation is
    authorized from the root query's verified reveal decision and requested
    capability, so an unavailable or unrequested grant cannot become authority by
    passing a mode enum or by omitting reveal events from the inventory.
    """

    if type(root_query) is not ObservationQueryResult:
        raise TypeError("timeline root must be an ObservationQueryResult")
    source_run_id = root_query.source_run_id
    source_event_sha256 = root_query.source_event_sha256
    observation_mode = root_query.policy.mode
    _source_identity(source_run_id, source_event_sha256)
    if type(events) is not tuple or any(
        type(item) is not TimelineEvidenceEvent for item in events
    ):
        raise TypeError("timeline evidence events must be an exact tuple")
    policy_id = root_query.policy.policy_id
    if any(
        item.source_run_id != source_run_id
        or item.source_event_sha256 != source_event_sha256
        or item.observation_mode is not observation_mode
        or item.policy_id != policy_id
        or item.policy_visible_at_time_us
        > root_query.request.render_cursor_time_us
        for item in events
    ):
        raise ValueError("timeline event is outside its root query authority")
    canonical_events = tuple(
        sorted(
            events,
            key=lambda item: (
                item.policy_visible_at_time_us,
                item.sequence,
                item.event_id,
            ),
        )
    )
    event_ids = tuple(item.event_id for item in canonical_events)
    sequences = tuple(item.sequence for item in canonical_events)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("timeline event IDs must be unique")
    if len(sequences) != len(set(sequences)):
        raise ValueError("timeline event sequences must be unique")
    _validate_derivations(canonical_events)

    event_reveal_authorization_ids = {
        item.reveal_authorization_id
        for item in canonical_events
        if item.reveal_authorization_id is not None
    }
    event_reveal_evidence_sha256s = {
        item.reveal_evidence_sha256
        for item in canonical_events
        if item.reveal_evidence_sha256 is not None
    }
    if (
        len(event_reveal_authorization_ids) > 1
        or len(event_reveal_evidence_sha256s) > 1
    ):
        raise ValueError("one timeline cannot span multiple reveal grants")

    reveal_available = (
        root_query.reveal.availability is RevealAvailability.AVAILABLE
    )
    root_authorization_ids = (
        ()
        if not reveal_available
        else (root_query.reveal.authorization_id,)
    )
    root_reveal_sha256s = (
        ()
        if not reveal_available
        else (root_query.reveal_evidence_sha256,)
    )
    if reveal_available and (
        root_authorization_ids[0] is None or root_reveal_sha256s[0] is None
    ):
        raise ValueError("available timeline root lacks reveal evidence binding")
    if event_reveal_authorization_ids and event_reveal_authorization_ids != set(
        root_authorization_ids
    ):
        raise ValueError("timeline reveal event belongs to another authorization")
    if event_reveal_evidence_sha256s and event_reveal_evidence_sha256s != set(
        root_reveal_sha256s
    ):
        raise ValueError("timeline reveal event belongs to another reveal source")
    reveal_authorized = reveal_available and (
        RevealCapability.GROUND_TRUTH
        in root_query.reveal.requested_capabilities
    )

    event_inventory_sha256 = _canonical_sha256(
        [item._receipt_dict() for item in canonical_events]
    )
    timeline = ReplayTimeline(
        source_run_id,
        source_event_sha256,
        observation_mode,
        canonical_events,
        event_inventory_sha256,
        reveal_authorized,
        _construction_token=_TIMELINE_CONSTRUCTION_TOKEN,
    )
    partition_rows = _partition_rows(canonical_events)
    cursor_times = tuple(row["render_cursor_time_us"] for row in partition_rows)
    receipt = TimelineReceipt(
        source_run_id=source_run_id,
        source_event_sha256=source_event_sha256,
        observation_mode=observation_mode,
        policy_id=policy_id,
        timeline_id=timeline.timeline_id,
        root_query_id=root_query.query_id,
        root_query_sha256=hashlib.sha256(root_query.canonical_bytes()).hexdigest(),
        root_render_cursor_time_us=root_query.request.render_cursor_time_us,
        reveal_availability=root_query.reveal.availability,
        event_inventory_sha256=event_inventory_sha256,
        event_count=len(canonical_events),
        partition_inventory_sha256=_canonical_sha256(partition_rows),
        partition_count=len(partition_rows),
        minimum_cursor_time_us=(None if not cursor_times else min(cursor_times)),
        maximum_cursor_time_us=(None if not cursor_times else max(cursor_times)),
        reveal_authorization_ids=tuple(root_authorization_ids),
        reveal_evidence_sha256s=tuple(root_reveal_sha256s),
        _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
    )
    return timeline, receipt


def _validate_derivations(events: tuple[TimelineEvidenceEvent, ...]) -> None:
    direct_by_source_id: dict[str, TimelineEvidenceEvent] = {}
    for event in events:
        if event.source is TimelineEvidenceSource.DECLARED_DERIVATION:
            continue
        source_event_id = event.source_event_ids[0]
        if source_event_id in direct_by_source_id:
            raise ValueError("one direct source event has multiple timeline owners")
        direct_by_source_id[source_event_id] = event
    for event in events:
        if event.source is not TimelineEvidenceSource.DECLARED_DERIVATION:
            continue
        try:
            sources = tuple(direct_by_source_id[item] for item in event.source_event_ids)
        except KeyError as error:
            raise ValueError(
                "timeline derivation cites an event outside the policy-bound inventory"
            ) from error
        if event.source_event_time_us != max(
            item.source_event_time_us for item in sources
        ):
            raise ValueError("timeline derivation source time differs from its inputs")
        if event.policy_visible_at_time_us != max(
            item.policy_visible_at_time_us for item in sources
        ):
            raise ValueError("timeline derivation visibility differs from its inputs")


def _partition_rows(
    events: tuple[TimelineEvidenceEvent, ...],
) -> list[dict[str, object]]:
    grouped: dict[int, list[TimelineEvidenceEvent]] = {}
    for event in events:
        grouped.setdefault(event.policy_visible_at_time_us, []).append(event)
    return [
        {
            "event_ids": [
                item.event_id
                for item in sorted(
                    grouped[cursor_time],
                    key=lambda item: (item.sequence, item.event_id),
                )
            ],
            "render_cursor_time_us": cursor_time,
        }
        for cursor_time in sorted(grouped)
    ]


def _event_link(event: TimelineEvidenceEvent) -> TimelineEventLink:
    return TimelineEventLink(
        event_id=event.event_id,
        event_kind=event.event_kind,
        sequence=event.sequence,
        source_event_time_us=event.source_event_time_us,
        policy_visible_at_time_us=event.policy_visible_at_time_us,
        source_event_ids=event.source_event_ids,
        derivation=event.derivation,
        query_id=event.query_id,
        query_projection_sha256=event.query_projection_sha256,
        series_id=event.series_id,
        payload_sha256=event.payload_sha256,
        source_evidence_sha256=event.source_evidence_sha256,
        _construction_token=_OUTPUT_CONSTRUCTION_TOKEN,
    )


def _timeline_source(value: QueriedValue) -> TimelineEvidenceSource:
    sources = {
        EvidenceSourceKind.CLIENT_DELIVERED: TimelineEvidenceSource.CLIENT_DELIVERED,
        EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT: (
            TimelineEvidenceSource.RECORDED_DECISION_SNAPSHOT
        ),
        EvidenceSourceKind.REVEALED_GROUND_TRUTH: (
            TimelineEvidenceSource.AUTHORIZED_GROUND_TRUTH
        ),
        EvidenceSourceKind.REVEALED_HIDDEN_STATE: (
            TimelineEvidenceSource.AUTHORIZED_HIDDEN_STATE
        ),
    }
    try:
        return sources[value.source_kind]
    except KeyError as error:  # pragma: no cover - enum is closed today
        raise ValueError("query value has no timeline evidence source") from error


def _classify_timeline_value(
    value: QueriedValue,
    source: TimelineEvidenceSource,
) -> TimelineEventKind:
    """Derive a jump role from a closed source, series, and payload contract."""

    series_id = value.series_id
    payload = _value_payload(value)
    if series_id == "order.client-intention":
        if source is not TimelineEvidenceSource.RECORDED_DECISION_SNAPSHOT:
            raise ValueError("client intention has the wrong timeline source plane")
        if not _closed_payload(
            payload,
            {
                "side": frozenset({"BUY", "SELL"}),
                "venue_state": frozenset({"NOT_OBSERVED", "RECEIVED"}),
            },
        ):
            raise ValueError("client intention payload violates its timeline contract")
        return TimelineEventKind.PLAYER_ACTION
    if series_id.startswith("fill."):
        if source is not TimelineEvidenceSource.CLIENT_DELIVERED:
            raise ValueError("fill has the wrong timeline source plane")
        if (
            payload is None
            or set(payload) != {"filled_quantity"}
            or type(payload["filled_quantity"]) is not int
            or payload["filled_quantity"] < 0
        ):
            raise ValueError("fill payload violates its timeline contract")
        return TimelineEventKind.FILL
    if series_id == "strategy.signal":
        if source is not TimelineEvidenceSource.RECORDED_DECISION_SNAPSHOT:
            raise ValueError("strategy signal has the wrong timeline source plane")
        if not _closed_payload(
            payload,
            {"recorded_signal": frozenset({"GREEN", "RED", "WAIT"})},
        ):
            raise ValueError("strategy signal payload violates its timeline contract")
        return TimelineEventKind.TRAFFIC_LIGHT_TRANSITION
    if series_id == "regime.transition":
        if source is not TimelineEvidenceSource.AUTHORIZED_GROUND_TRUTH:
            raise ValueError("regime transition requires authorized ground truth")
        if payload is None or set(payload) != {"from_regime", "to_regime"}:
            raise ValueError("regime transition payload violates its timeline contract")
        from_regime = payload["from_regime"]
        to_regime = payload["to_regime"]
        _identifier(from_regime, "regime transition source regime")
        _identifier(to_regime, "regime transition destination regime")
        if from_regime == to_regime:
            raise ValueError("regime transition cannot retain the same regime")
        return TimelineEventKind.REVEALED_REGIME_TRANSITION
    if series_id == "warning.invariant" or series_id.startswith("warning.invariant."):
        if source in _REVEAL_SOURCES:
            raise ValueError("invariant warning cannot be introduced by reveal evidence")
        if payload is None or set(payload) != {"warning_code"}:
            raise ValueError("invariant warning payload violates its timeline contract")
        _identifier(payload["warning_code"], "invariant warning code")
        return TimelineEventKind.INVARIANT_WARNING
    if source in _REVEAL_SOURCES:
        raise ValueError("untyped reveal value cannot enter the timeline")
    return TimelineEventKind.OBSERVED_UPDATE


def _value_payload(value: QueriedValue) -> Mapping[str, object] | None:
    if value.disposition is not RecordDisposition.VALUE:
        return None
    payload = thaw_json(value.payload)
    return payload if isinstance(payload, Mapping) else None


def _closed_payload(
    payload: Mapping[str, object] | None,
    contract: Mapping[str, frozenset[str]],
) -> bool:
    if payload is None or set(payload) != set(contract):
        return False
    return all(
        type(payload[field_name]) is str
        and payload[field_name] in allowed
        for field_name, allowed in contract.items()
    )


def _nearest_time(
    values: tuple[int, ...],
    cursor_time_us: int,
    direction: TimelineDirection,
) -> int | None:
    candidates = tuple(
        value
        for value in values
        if (
            value > cursor_time_us
            if direction is TimelineDirection.NEXT
            else value < cursor_time_us
        )
    )
    if not candidates:
        return None
    return min(candidates) if direction is TimelineDirection.NEXT else max(candidates)


def _timeline_id(
    source_run_id: str,
    source_event_sha256: str,
    observation_mode: ObservationMode,
    policy_id: str,
) -> str:
    # Full inventory identity is deliberately absent.  Even an opaque digest of
    # future partitions is a comparison oracle for cursor-safe consumers.  Exact
    # facade ownership is enforced by a private per-instance cursor capability;
    # the backend TimelineReceipt separately commits to the complete inventory.
    return "replay-timeline-" + _canonical_sha256(
        {
            "observation_mode": observation_mode.value,
            "policy_id": policy_id,
            "schema_id": TIMELINE_SCHEMA_ID,
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "source_event_sha256": source_event_sha256,
            "source_run_id": source_run_id,
        }
    )[:24]


def _direction(value: object) -> None:
    if type(value) is not TimelineDirection:
        raise TypeError("timeline direction is invalid")


def _source_identity(source_run_id: object, source_event_sha256: object) -> None:
    if type(source_run_id) is not str or not _RUN_ID.fullmatch(source_run_id):
        raise ValueError("timeline source run ID is invalid")
    _sha256(source_event_sha256, "timeline source event SHA-256")


def _identifier(value: object, label: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _identifiers(
    values: object,
    label: str,
    *,
    empty: bool,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an exact tuple")
    if not empty and not values:
        raise ValueError(f"{label} cannot be empty")
    for value in values:
        _identifier(value, label)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def _sha256s(values: object, label: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an exact tuple")
    for value in values:
        _sha256(value, label)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def _sha256(value: object, label: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _nonnegative_exact_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative exact integer")


def _positive_exact_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive exact integer")


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = [
    "TIMELINE_CURSOR_SCHEMA_ID",
    "TIMELINE_CURSOR_SCHEMA_VERSION",
    "TIMELINE_EVENT_SCHEMA_ID",
    "TIMELINE_EVENT_SCHEMA_VERSION",
    "TIMELINE_NAVIGATION_SCHEMA_ID",
    "TIMELINE_NAVIGATION_SCHEMA_VERSION",
    "TIMELINE_RECEIPT_SCHEMA_ID",
    "TIMELINE_RECEIPT_SCHEMA_VERSION",
    "TIMELINE_SCHEMA_ID",
    "TIMELINE_SCHEMA_VERSION",
    "ReplayTimeline",
    "TimelineCursor",
    "TimelineDerivation",
    "TimelineDirection",
    "TimelineEventKind",
    "TimelineEventLink",
    "TimelineEvidenceEvent",
    "TimelineEvidenceSource",
    "TimelineJumpTarget",
    "TimelineNavigationAvailability",
    "TimelineNavigationKind",
    "TimelineNavigationResult",
    "TimelineNavigationUnavailableReason",
    "TimelinePlaybackState",
    "TimelineReceipt",
    "TimelineSidecarOperation",
    "TimelineSidecarRefusal",
    "TimelineSidecarRefusalReason",
    "TimelineSidecarStatus",
    "build_replay_timeline",
    "derive_timeline_event",
    "timeline_event_from_query_result",
]
