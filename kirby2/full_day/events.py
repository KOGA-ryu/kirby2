"""Scheduled-work keys and outer replay events for a full trading day."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from kirby2.exchange import SessionState

if TYPE_CHECKING:
    from .checkpoint_contract import QuiescentCutV1

from .models import (
    FlowSideV1,
    FullDayPlanV1,
    ParticipantScheduleActionV1,
    SCHEDULED_EVENT_SEMANTICS_V1,
    ScheduledEventTypeV1,
    ScheduledEventV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from .states import DayStateV1, LocalStateV1


FULL_DAY_EVENT_SCHEMA_VERSION = 1
FULL_DAY_PAYLOAD_SCHEMA_VERSION = 1
NATIVE_EVENT_REFERENCE_SCHEMA_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORK_PARENT_RE = re.compile(r"^work:[0-9a-f]{64}$")
_EVENT_PARENT_RE = re.compile(r"^event:([1-9][0-9]*)$")


class WorkStageV1(IntEnum):
    ATOMIC_CALENDAR_BOUNDARY = 0
    SCHEDULED_INFORMATION = 1
    DAY_STATE_TRANSITION = 2
    LOCAL_STATE_TRANSITION = 3
    PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE = 4
    PENDING_VENUE_ARRIVAL = 5
    ENDOGENOUS_PARTICIPANT_DECISION = 6
    BACKGROUND_FLOW_PROPOSAL = 7
    OBSERVABLE_CLIENT_DELIVERY = 8
    FEATURE_UPDATE = 9
    STRATEGY_ALGORITHM_DEADLINE = 10
    CHECKPOINT_CAPTURE = 11


class FullDayEventTypeV1(str, Enum):
    CALENDAR_BOUNDARY = "CALENDAR_BOUNDARY"
    SCHEDULED_INFORMATION = "SCHEDULED_INFORMATION"
    DAY_STATE_ANCHOR_RESET = "DAY_STATE_ANCHOR_RESET"
    DAY_STATE_TRANSITION = "DAY_STATE_TRANSITION"
    LOCAL_STATE_TRANSITION = "LOCAL_STATE_TRANSITION"
    PARTICIPANT_ACTIVATED = "PARTICIPANT_ACTIVATED"
    PARTICIPANT_DEACTIVATED = "PARTICIPANT_DEACTIVATED"
    PARTICIPANT_RETUNED = "PARTICIPANT_RETUNED"
    PENDING_VENUE_ARRIVAL = "PENDING_VENUE_ARRIVAL"
    PARTICIPANT_DECISION = "PARTICIPANT_DECISION"
    BACKGROUND_FLOW_PROPOSAL = "BACKGROUND_FLOW_PROPOSAL"
    OBSERVABLE_DELIVERY = "OBSERVABLE_DELIVERY"
    FEATURE_UPDATED = "FEATURE_UPDATED"
    STRATEGY_ALGORITHM_DEADLINE = "STRATEGY_ALGORITHM_DEADLINE"
    CHECKPOINT_CAPTURE_MARKER = "CHECKPOINT_CAPTURE_MARKER"
    SHOCK_CANDIDATE = "SHOCK_CANDIDATE"
    SHOCK_ACCEPTED = "SHOCK_ACCEPTED"
    SHOCK_REJECTED = "SHOCK_REJECTED"
    SUBSYSTEM_EVENT = "SUBSYSTEM_EVENT"
    RESOURCE_LIMIT_ABORT = "RESOURCE_LIMIT_ABORT"
    CAPABILITY_REFUSED = "CAPABILITY_REFUSED"


@dataclass(frozen=True, slots=True)
class FullDayPayloadFieldRuleV1:
    """One exact scalar field rule in the frozen outer-payload registry."""

    field_name: str
    value_kind: str
    allowed_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.field_name) is not str or not _IDENTIFIER_RE.fullmatch(
            self.field_name
        ):
            raise ValueError("full-day payload field name must be a canonical identifier")
        if self.value_kind not in {
            "BOOLEAN",
            "IDENTIFIER",
            "NONNEGATIVE_INTEGER",
            "POSITIVE_INTEGER",
            "SHA256",
        }:
            raise ValueError("full-day payload field kind is unsupported")
        if type(self.allowed_values) is not tuple or any(
            type(value) is not str or not value for value in self.allowed_values
        ):
            raise TypeError("payload allowed values must be an immutable string tuple")
        if self.allowed_values != tuple(sorted(set(self.allowed_values))):
            raise ValueError("payload allowed values must be sorted and unique")
        if self.allowed_values and self.value_kind != "IDENTIFIER":
            raise ValueError("only identifier payload fields may use an allowed-value set")


def _field(
    field_name: str,
    value_kind: str,
    allowed_values: tuple[str, ...] = (),
) -> FullDayPayloadFieldRuleV1:
    return FullDayPayloadFieldRuleV1(
        field_name=field_name,
        value_kind=value_kind,
        allowed_values=tuple(sorted(allowed_values)),
    )


def _schema(
    *rules: FullDayPayloadFieldRuleV1,
) -> tuple[FullDayPayloadFieldRuleV1, ...]:
    result = tuple(sorted(rules, key=lambda rule: rule.field_name))
    names = tuple(rule.field_name for rule in result)
    if len(names) != len(set(names)):
        raise RuntimeError("full-day payload schema contains duplicate fields")
    return result


_ALL_SCHEDULED_EVENT_TYPES = tuple(item.value for item in ScheduledEventTypeV1)
_ALL_FLOW_SIDES = tuple(item.value for item in FlowSideV1)
_AGGRESSIVE_FLOW_SIDES = (FlowSideV1.BUY.value, FlowSideV1.SELL.value)
_ALL_DAY_STATES = tuple(item.value for item in DayStateV1)
_ALL_LOCAL_STATES = tuple(item.value for item in LocalStateV1)
_ALL_SESSION_STATES = tuple(item.value for item in SessionState)


CALENDAR_BOUNDARY_INDEX_V1: Mapping[int, tuple[SessionState, bool]] = (
    MappingProxyType(
        {
            0: (SessionState.PREOPEN, False),
            1: (SessionState.OPENING_AUCTION, False),
            2: (SessionState.CONTINUOUS, True),
            3: (SessionState.CLOSING_AUCTION, False),
            4: (SessionState.POSTCLOSE, True),
            5: (SessionState.CLOSED, False),
        }
    )
)
if tuple(CALENDAR_BOUNDARY_INDEX_V1.items()) != (
    (0, (SessionState.PREOPEN, False)),
    (1, (SessionState.OPENING_AUCTION, False)),
    (2, (SessionState.CONTINUOUS, True)),
    (3, (SessionState.CLOSING_AUCTION, False)),
    (4, (SessionState.POSTCLOSE, True)),
    (5, (SessionState.CLOSED, False)),
):
    raise RuntimeError("calendar boundary index V1 changed")


FULL_DAY_PAYLOAD_FIELD_RULES_V1: Mapping[
    FullDayEventTypeV1, tuple[FullDayPayloadFieldRuleV1, ...]
] = MappingProxyType(
    {
        FullDayEventTypeV1.CALENDAR_BOUNDARY: _schema(
            _field("boundary_operation_index", "NONNEGATIVE_INTEGER"),
            _field(
                "destination_session_state", "IDENTIFIER", _ALL_SESSION_STATES
            ),
            _field("uncross_before", "BOOLEAN"),
        ),
        FullDayEventTypeV1.SCHEDULED_INFORMATION: _schema(
            _field("parameter_set_sha256", "SHA256"),
            _field("scheduled_event_id", "IDENTIFIER"),
            _field(
                "scheduled_event_type", "IDENTIFIER", _ALL_SCHEDULED_EVENT_TYPES
            ),
            _field("side", "IDENTIFIER", _ALL_FLOW_SIDES),
        ),
        FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET: _schema(
            _field("anchored_state", "IDENTIFIER", _ALL_DAY_STATES),
            _field("entered_time_us", "NONNEGATIVE_INTEGER"),
            _field("macro_segment_index", "NONNEGATIVE_INTEGER"),
            _field("macro_segment_sha256", "SHA256"),
            _field("previous_state", "IDENTIFIER", _ALL_DAY_STATES),
            _field("sampled_duration_us", "NONNEGATIVE_INTEGER"),
        ),
        FullDayEventTypeV1.DAY_STATE_TRANSITION: _schema(
            _field("entered_time_us", "NONNEGATIVE_INTEGER"),
            _field("new_state", "IDENTIFIER", _ALL_DAY_STATES),
            _field("previous_state", "IDENTIFIER", _ALL_DAY_STATES),
            _field("sampled_duration_us", "NONNEGATIVE_INTEGER"),
            _field("transition_id", "IDENTIFIER"),
            _field("trigger_id", "IDENTIFIER"),
            _field("trigger_version", "POSITIVE_INTEGER"),
        ),
        FullDayEventTypeV1.LOCAL_STATE_TRANSITION: _schema(
            _field("entered_time_us", "NONNEGATIVE_INTEGER"),
            _field("new_state", "IDENTIFIER", _ALL_LOCAL_STATES),
            _field("previous_state", "IDENTIFIER", _ALL_LOCAL_STATES),
            _field("sampled_duration_us", "NONNEGATIVE_INTEGER"),
            _field("transition_id", "IDENTIFIER"),
            _field("trigger_id", "IDENTIFIER"),
            _field("trigger_version", "POSITIVE_INTEGER"),
        ),
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED: _schema(
            _field("native_payload_sha256", "SHA256"),
            _field("participant_id", "IDENTIFIER"),
            _field("schedule_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED: _schema(
            _field("native_payload_sha256", "SHA256"),
            _field("participant_id", "IDENTIFIER"),
            _field("schedule_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.PARTICIPANT_RETUNED: _schema(
            _field("native_payload_sha256", "SHA256"),
            _field("participant_id", "IDENTIFIER"),
            _field("replacement_specification_sha256", "SHA256"),
            _field("schedule_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.PENDING_VENUE_ARRIVAL: _schema(
            _field("arrival_time_us", "NONNEGATIVE_INTEGER"),
            _field("native_payload_sha256", "SHA256"),
            _field("order_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.PARTICIPANT_DECISION: _schema(
            _field("decision_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("native_payload_sha256", "SHA256"),
            _field("participant_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL: _schema(
            _field("native_payload_sha256", "SHA256"),
            _field("observation_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("proposal_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.OBSERVABLE_DELIVERY: _schema(
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("message_id", "IDENTIFIER"),
            _field("native_payload_sha256", "SHA256"),
        ),
        FullDayEventTypeV1.FEATURE_UPDATED: _schema(
            _field("feature_batch_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("native_payload_sha256", "SHA256"),
        ),
        FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE: _schema(
            _field("deadline_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("native_payload_sha256", "SHA256"),
        ),
        FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER: _schema(
            _field("checkpoint_request_id", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.SHOCK_CANDIDATE: _schema(
            _field("candidate_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("quantity_shares", "POSITIVE_INTEGER"),
            _field("side", "IDENTIFIER", _AGGRESSIVE_FLOW_SIDES),
        ),
        FullDayEventTypeV1.SHOCK_ACCEPTED: _schema(
            _field("candidate_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("quantity_shares", "POSITIVE_INTEGER"),
            _field("side", "IDENTIFIER", _AGGRESSIVE_FLOW_SIDES),
        ),
        FullDayEventTypeV1.SHOCK_REJECTED: _schema(
            _field("candidate_id", "IDENTIFIER"),
            _field("information_cutoff_us", "NONNEGATIVE_INTEGER"),
            _field("reason_code", "IDENTIFIER"),
        ),
        FullDayEventTypeV1.SUBSYSTEM_EVENT: _schema(
            _field("native_payload_sha256", "SHA256"),
        ),
        FullDayEventTypeV1.RESOURCE_LIMIT_ABORT: _schema(
            _field("limit_id", "IDENTIFIER"),
            _field("maximum_value", "NONNEGATIVE_INTEGER"),
            _field("observed_value", "NONNEGATIVE_INTEGER"),
        ),
        FullDayEventTypeV1.CAPABILITY_REFUSED: _schema(
            _field("capability_id", "IDENTIFIER"),
            _field("reason_code", "IDENTIFIER"),
        ),
    }
)
if frozenset(FULL_DAY_PAYLOAD_FIELD_RULES_V1) != frozenset(FullDayEventTypeV1):
    raise RuntimeError("full-day payload schema registry is not exhaustive")


_NATIVE_EVENT_REQUIRED_TYPES = frozenset(
    {
        FullDayEventTypeV1.SUBSYSTEM_EVENT,
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
        FullDayEventTypeV1.PARTICIPANT_RETUNED,
        FullDayEventTypeV1.PENDING_VENUE_ARRIVAL,
        FullDayEventTypeV1.PARTICIPANT_DECISION,
        FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL,
        FullDayEventTypeV1.OBSERVABLE_DELIVERY,
        FullDayEventTypeV1.FEATURE_UPDATED,
        FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE,
    }
)
FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1: Mapping[
    FullDayEventTypeV1, str | None
] = MappingProxyType(
    {
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED: "PARTICIPANT_ACTIVATED",
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED: "PARTICIPANT_DEACTIVATED",
        FullDayEventTypeV1.PARTICIPANT_RETUNED: "PARTICIPANT_RETUNED",
        FullDayEventTypeV1.PENDING_VENUE_ARRIVAL: "PENDING_VENUE_ARRIVAL",
        FullDayEventTypeV1.PARTICIPANT_DECISION: "PARTICIPANT_DECISION",
        FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL: "BACKGROUND_FLOW_PROPOSAL",
        FullDayEventTypeV1.OBSERVABLE_DELIVERY: "CLIENT_MESSAGE_DELIVERED",
        FullDayEventTypeV1.FEATURE_UPDATED: "FEATURE_UPDATED",
        FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE: (
            "STRATEGY_ALGORITHM_DEADLINE"
        ),
        FullDayEventTypeV1.SUBSYSTEM_EVENT: None,
    }
)
if frozenset(FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1) != _NATIVE_EVENT_REQUIRED_TYPES:
    raise RuntimeError(
        "allowed native-event-type registry must cover every native outer type"
    )
for _native_outer_type in _NATIVE_EVENT_REQUIRED_TYPES:
    if "native_payload_sha256" not in {
        rule.field_name
        for rule in FULL_DAY_PAYLOAD_FIELD_RULES_V1[_native_outer_type]
    }:
        raise RuntimeError(
            f"{_native_outer_type.value} must bind its native payload digest"
        )

_ALL_STAGES = frozenset(WorkStageV1)
_EVENT_ALLOWED_STAGES = {
    FullDayEventTypeV1.CALENDAR_BOUNDARY: frozenset(
        {WorkStageV1.ATOMIC_CALENDAR_BOUNDARY}
    ),
    FullDayEventTypeV1.SCHEDULED_INFORMATION: frozenset(
        {WorkStageV1.SCHEDULED_INFORMATION}
    ),
    FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET: frozenset(
        {WorkStageV1.DAY_STATE_TRANSITION}
    ),
    FullDayEventTypeV1.DAY_STATE_TRANSITION: frozenset(
        {WorkStageV1.DAY_STATE_TRANSITION}
    ),
    FullDayEventTypeV1.LOCAL_STATE_TRANSITION: frozenset(
        {WorkStageV1.LOCAL_STATE_TRANSITION}
    ),
    FullDayEventTypeV1.PARTICIPANT_ACTIVATED: frozenset(
        {WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE}
    ),
    FullDayEventTypeV1.PARTICIPANT_DEACTIVATED: frozenset(
        {WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE}
    ),
    FullDayEventTypeV1.PARTICIPANT_RETUNED: frozenset(
        {WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE}
    ),
    FullDayEventTypeV1.PENDING_VENUE_ARRIVAL: frozenset(
        {WorkStageV1.PENDING_VENUE_ARRIVAL}
    ),
    FullDayEventTypeV1.PARTICIPANT_DECISION: frozenset(
        {WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION}
    ),
    FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL: frozenset(
        {WorkStageV1.BACKGROUND_FLOW_PROPOSAL}
    ),
    FullDayEventTypeV1.OBSERVABLE_DELIVERY: frozenset(
        {WorkStageV1.OBSERVABLE_CLIENT_DELIVERY}
    ),
    FullDayEventTypeV1.FEATURE_UPDATED: frozenset({WorkStageV1.FEATURE_UPDATE}),
    FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE: frozenset(
        {WorkStageV1.STRATEGY_ALGORITHM_DEADLINE}
    ),
    FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER: frozenset(
        {WorkStageV1.CHECKPOINT_CAPTURE}
    ),
    FullDayEventTypeV1.SHOCK_CANDIDATE: frozenset(
        {WorkStageV1.SCHEDULED_INFORMATION}
    ),
    FullDayEventTypeV1.SHOCK_ACCEPTED: frozenset(
        {WorkStageV1.SCHEDULED_INFORMATION}
    ),
    FullDayEventTypeV1.SHOCK_REJECTED: frozenset(
        {WorkStageV1.SCHEDULED_INFORMATION}
    ),
    FullDayEventTypeV1.SUBSYSTEM_EVENT: _ALL_STAGES
    - {WorkStageV1.CHECKPOINT_CAPTURE},
    FullDayEventTypeV1.RESOURCE_LIMIT_ABORT: _ALL_STAGES,
    FullDayEventTypeV1.CAPABILITY_REFUSED: _ALL_STAGES,
}
if frozenset(_EVENT_ALLOWED_STAGES) != frozenset(FullDayEventTypeV1):
    raise RuntimeError("outer event stage registry is not exhaustive")


WorkStage = WorkStageV1
FullDayEventType = FullDayEventTypeV1


def _identifier(value: object, context: str) -> str:
    if type(value) is not str or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{context} must be a canonical identifier")
    return value


def _validate_payload_data(
    payload_type: str,
    payload_version: int,
    data: Mapping[str, object],
) -> FullDayEventTypeV1:
    if type(payload_version) is not int or payload_version != 1:
        raise ValueError("full-day payload version must be exactly 1")
    try:
        event_type = FullDayEventTypeV1(payload_type)
    except ValueError as error:
        raise ValueError("full-day payload type is not registered") from error
    if not isinstance(data, Mapping):
        raise TypeError("full-day payload data must be a JSON object")
    validate_strict_json(data)
    rules = FULL_DAY_PAYLOAD_FIELD_RULES_V1[event_type]
    expected_fields = {rule.field_name for rule in rules}
    _require_exact_fields(data, expected_fields, f"{event_type.value} payload data")
    for rule in rules:
        value = data[rule.field_name]
        if rule.value_kind == "BOOLEAN":
            if type(value) is not bool:
                raise TypeError(f"payload {rule.field_name} must be a boolean")
        elif rule.value_kind == "IDENTIFIER":
            _identifier(value, f"payload {rule.field_name}")
            if rule.allowed_values and value not in rule.allowed_values:
                raise ValueError(
                    f"payload {rule.field_name} is outside its allowed enum"
                )
        elif rule.value_kind == "NONNEGATIVE_INTEGER":
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"payload {rule.field_name} must be a nonnegative integer"
                )
        elif rule.value_kind == "POSITIVE_INTEGER":
            if type(value) is not int or value <= 0:
                raise ValueError(
                    f"payload {rule.field_name} must be a positive integer"
                )
        elif rule.value_kind == "SHA256":
            if type(value) is not str or not _SHA256_RE.fullmatch(value):
                raise ValueError(
                    f"payload {rule.field_name} must be a lowercase SHA-256"
                )
        else:  # pragma: no cover - registry construction closes this branch
            raise RuntimeError("full-day payload registry contains an unknown rule")
    if event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY:
        boundary_index = data["boundary_operation_index"]
        expected_boundary = CALENDAR_BOUNDARY_INDEX_V1.get(boundary_index)
        if expected_boundary is None:
            raise ValueError("calendar boundary operation index must lie in 0..5")
        actual_boundary = (
            SessionState(data["destination_session_state"]),
            data["uncross_before"],
        )
        if actual_boundary != expected_boundary:
            raise ValueError(
                "calendar boundary destination/uncross tuple does not match its index"
            )
    if event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION:
        scheduled_type = ScheduledEventTypeV1(data["scheduled_event_type"])
        scheduled_side = FlowSideV1(data["side"])
        if scheduled_side not in SCHEDULED_EVENT_SEMANTICS_V1[
            scheduled_type
        ].allowed_sides:
            raise ValueError(
                "scheduled-information side is forbidden for its scheduled event type"
            )
    return event_type


def _exact_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _exact_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _exact_object(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = payload[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {name} must be an object")
    return value


def _exact_array(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload[name]
    if type(value) is not list:
        raise TypeError(f"serialized {name} must be an array")
    return value


def _freeze_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    validate_strict_json(value)

    def freeze(item: object) -> object:
        if item is None or type(item) in {bool, int, str}:
            return item
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(item[key]) for key in sorted(item)})
        if type(item) in {list, tuple}:
            return tuple(freeze(child) for child in item)
        raise TypeError(f"unsupported event payload value: {type(item).__name__}")

    result = freeze(value)
    assert isinstance(result, Mapping)
    return result


def _plain_payload(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, Mapping):
        return {key: _plain_payload(value[key]) for key in sorted(value)}
    if type(value) in {list, tuple}:
        return [_plain_payload(item) for item in value]
    raise TypeError(f"unsupported event payload value: {type(value).__name__}")


@dataclass(frozen=True, slots=True, order=True)
class ScheduledWorkKeyV1:
    """The exact five-field queue identity and sort order."""

    simulation_time_us: int
    microstep: int
    stage_ordinal: WorkStageV1
    source_component_id: str
    component_local_sequence: int

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("scheduled work time must be nonnegative microseconds")
        if type(self.microstep) is not int or self.microstep < 0:
            raise ValueError("scheduled work microstep must be nonnegative")
        if type(self.stage_ordinal) is not WorkStageV1:
            raise TypeError("scheduled work stage must use WorkStageV1")
        _identifier(self.source_component_id, "scheduled work source component ID")
        if type(self.component_local_sequence) is not int or self.component_local_sequence < 0:
            raise ValueError("component-local sequence must be nonnegative")
        if self.stage_ordinal in {
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            WorkStageV1.SCHEDULED_INFORMATION,
        } and self.microstep != 0:
            raise ValueError("calendar/information work may execute only at microstep zero")

    @property
    def ordering_key(self) -> tuple[int, int, int, str, int]:
        return (
            self.simulation_time_us,
            self.microstep,
            int(self.stage_ordinal),
            self.source_component_id,
            self.component_local_sequence,
        )

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def work_id(self) -> str:
        return f"work:{self.identity_sha256}"

    def as_dict(self) -> dict[str, object]:
        return {
            "component_local_sequence": self.component_local_sequence,
            "microstep": self.microstep,
            "simulation_time_us": self.simulation_time_us,
            "source_component_id": self.source_component_id,
            "stage_ordinal": int(self.stage_ordinal),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScheduledWorkKeyV1:
        _require_exact_fields(
            payload,
            {
                "component_local_sequence",
                "microstep",
                "simulation_time_us",
                "source_component_id",
                "stage_ordinal",
            },
            "scheduled work key",
        )
        return cls(
            simulation_time_us=_exact_int(payload, "simulation_time_us"),
            microstep=_exact_int(payload, "microstep"),
            stage_ordinal=WorkStageV1(_exact_int(payload, "stage_ordinal")),
            source_component_id=_exact_str(payload, "source_component_id"),
            component_local_sequence=_exact_int(payload, "component_local_sequence"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ScheduledWorkKeyV1:
        return cls.from_dict(parse_canonical_json_object(raw))


@dataclass(frozen=True, slots=True)
class NativeEventReferenceV1:
    schema_version: int
    owner_component_id: str
    native_ledger_id: str
    event_type: str
    local_sequence: int
    event_id: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != NATIVE_EVENT_REFERENCE_SCHEMA_VERSION:
            raise ValueError("native-event-reference schema version must be 1")
        _identifier(self.owner_component_id, "native owner component ID")
        _identifier(self.native_ledger_id, "native ledger ID")
        _identifier(self.event_type, "native event type")
        _identifier(self.event_id, "native event ID")
        if type(self.local_sequence) is not int or self.local_sequence <= 0:
            raise ValueError("native event local sequence must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "local_sequence": self.local_sequence,
            "native_ledger_id": self.native_ledger_id,
            "owner_component_id": self.owner_component_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NativeEventReferenceV1:
        _require_exact_fields(
            payload,
            {
                "event_id",
                "event_type",
                "local_sequence",
                "native_ledger_id",
                "owner_component_id",
                "schema_version",
            },
            "native event reference",
        )
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            owner_component_id=_exact_str(payload, "owner_component_id"),
            native_ledger_id=_exact_str(payload, "native_ledger_id"),
            event_type=_exact_str(payload, "event_type"),
            local_sequence=_exact_int(payload, "local_sequence"),
            event_id=_exact_str(payload, "event_id"),
        )

    @property
    def ledger_key(self) -> tuple[str, str, str]:
        return (
            self.owner_component_id,
            self.native_ledger_id,
            self.event_id,
        )


@dataclass(frozen=True, slots=True)
class NativeLedgerEntryV1:
    """One immutable native-ledger row bound to its canonical payload bytes."""

    reference: NativeEventReferenceV1
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.reference) is not NativeEventReferenceV1:
            raise TypeError("native ledger reference must use NativeEventReferenceV1")
        if not isinstance(self.payload, Mapping):
            raise TypeError("native ledger payload must be a JSON object")
        object.__setattr__(self, "payload", _freeze_payload(self.payload))

    @property
    def ledger_key(self) -> tuple[str, str, str]:
        return self.reference.ledger_key

    @property
    def payload_sha256(self) -> str:
        return canonical_sha256(_plain_payload(self.payload))

    def as_dict(self) -> dict[str, object]:
        return {
            "payload": _plain_payload(self.payload),
            "reference": self.reference.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NativeLedgerEntryV1:
        _require_exact_fields(payload, {"payload", "reference"}, "native ledger entry")
        return cls(
            reference=NativeEventReferenceV1.from_dict(
                _exact_object(payload, "reference")
            ),
            payload=_exact_object(payload, "payload"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> NativeLedgerEntryV1:
        return cls.from_dict(parse_canonical_json_object(raw))


@dataclass(frozen=True, slots=True)
class FullDayEventPayloadV1:
    schema_version: int
    payload_type: str
    payload_version: int
    native_event: NativeEventReferenceV1 | None
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != FULL_DAY_PAYLOAD_SCHEMA_VERSION:
            raise ValueError("full-day payload schema version must be 1")
        _identifier(self.payload_type, "full-day payload type")
        if self.native_event is not None and type(self.native_event) is not NativeEventReferenceV1:
            raise TypeError("native event must use NativeEventReferenceV1 or null")
        _validate_payload_data(self.payload_type, self.payload_version, self.data)
        object.__setattr__(self, "data", _freeze_payload(self.data))

    def as_dict(self) -> dict[str, object]:
        return {
            "data": _plain_payload(self.data),
            "native_event": None if self.native_event is None else self.native_event.as_dict(),
            "payload_type": self.payload_type,
            "payload_version": self.payload_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FullDayEventPayloadV1:
        _require_exact_fields(
            payload,
            {"data", "native_event", "payload_type", "payload_version", "schema_version"},
            "full-day event payload",
        )
        native = payload["native_event"]
        if native is not None and not isinstance(native, Mapping):
            raise TypeError("native event reference must be an object or null")
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            payload_type=_exact_str(payload, "payload_type"),
            payload_version=_exact_int(payload, "payload_version"),
            native_event=None if native is None else NativeEventReferenceV1.from_dict(native),
            data=_exact_object(payload, "data"),
        )


@dataclass(frozen=True, slots=True)
class FullDayEventV1:
    schema_version: int
    global_event_sequence: int
    simulation_time_us: int
    microstep: int
    stage: WorkStageV1
    source_component_id: str
    component_local_sequence: int
    event_type: FullDayEventTypeV1
    causal_parent_ids: tuple[str, ...]
    payload: FullDayEventPayloadV1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != FULL_DAY_EVENT_SCHEMA_VERSION:
            raise ValueError("full-day event schema version must be 1")
        if type(self.global_event_sequence) is not int or self.global_event_sequence <= 0:
            raise ValueError("global event sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("full-day event time must be nonnegative microseconds")
        if type(self.microstep) is not int or self.microstep < 0:
            raise ValueError("full-day event microstep must be nonnegative")
        if type(self.stage) is not WorkStageV1:
            raise TypeError("full-day event stage must use WorkStageV1")
        if self.stage in {
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            WorkStageV1.SCHEDULED_INFORMATION,
        } and self.microstep != 0:
            raise ValueError("calendar/information events may occur only at microstep zero")
        _identifier(self.source_component_id, "full-day event source component ID")
        if type(self.component_local_sequence) is not int or self.component_local_sequence < 0:
            raise ValueError("full-day event component-local sequence must be nonnegative")
        if type(self.event_type) is not FullDayEventTypeV1:
            raise TypeError("full-day event type uses the wrong enum")
        if self.stage not in _EVENT_ALLOWED_STAGES[self.event_type]:
            raise ValueError(
                f"{self.event_type.value} is not valid at stage {int(self.stage)}"
            )
        if (
            type(self.causal_parent_ids) is not tuple
            or len(self.causal_parent_ids) != 1
        ):
            raise ValueError(
                "full-day event must cite exactly one work or immediate-event parent"
            )
        if any(type(item) is not str or not item for item in self.causal_parent_ids):
            raise TypeError("causal parent IDs must be nonempty strings")
        for parent_id in self.causal_parent_ids:
            event_match = _EVENT_PARENT_RE.fullmatch(parent_id)
            if _WORK_PARENT_RE.fullmatch(parent_id):
                continue
            if event_match and int(event_match.group(1)) < self.global_event_sequence:
                continue
            raise ValueError("causal parent must be a work digest or a prior outer event")
        if type(self.payload) is not FullDayEventPayloadV1:
            raise TypeError("full-day event payload uses the wrong contract")
        native = self.payload.native_event
        if self.payload.payload_type != self.event_type.value:
            raise ValueError("event payload type must equal the outer event type")
        native_required = self.event_type in _NATIVE_EVENT_REQUIRED_TYPES
        if native_required and native is None:
            raise ValueError(f"{self.event_type.value} requires a native event reference")
        if not native_required and native is not None:
            raise ValueError(
                f"{self.event_type.value} may not carry a native event reference"
            )
        if native is not None:
            if native.owner_component_id != self.source_component_id:
                raise ValueError("outer source and native owner component IDs disagree")
            allowed_native_type = FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1[
                self.event_type
            ]
            if (
                allowed_native_type is not None
                and native.event_type != allowed_native_type
            ):
                raise ValueError(
                    f"{self.event_type.value} requires native event type "
                    f"{allowed_native_type}"
                )
        self._validate_payload_context()

    def _validate_payload_context(self) -> None:
        data = self.payload.data
        for field in ("information_cutoff_us", "observation_cutoff_us"):
            value = data.get(field)
            if value is not None and value > self.simulation_time_us:
                raise ValueError(f"payload {field} cannot be in the future")
        for field in ("arrival_time_us", "entered_time_us"):
            value = data.get(field)
            if value is not None and value != self.simulation_time_us:
                raise ValueError(f"payload {field} must equal outer event time")
        if self.event_type in {
            FullDayEventTypeV1.DAY_STATE_TRANSITION,
            FullDayEventTypeV1.LOCAL_STATE_TRANSITION,
        } and data["previous_state"] == data["new_state"]:
            raise ValueError("state-transition payload must change state")
        if (
            self.event_type is FullDayEventTypeV1.RESOURCE_LIMIT_ABORT
            and data["observed_value"] <= data["maximum_value"]
        ):
            raise ValueError("resource abort requires an observed limit exceedance")

    @property
    def event_id(self) -> str:
        return f"event:{self.global_event_sequence}"

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def chronological_key(self) -> tuple[int, int, int]:
        return (self.simulation_time_us, self.microstep, int(self.stage))

    def as_dict(self) -> dict[str, object]:
        return {
            "causal_parent_ids": list(self.causal_parent_ids),
            "component_local_sequence": self.component_local_sequence,
            "event_type": self.event_type.value,
            "global_event_sequence": self.global_event_sequence,
            "microstep": self.microstep,
            "payload": self.payload.as_dict(),
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
            "source_component_id": self.source_component_id,
            "stage": int(self.stage),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FullDayEventV1:
        fields = {
            "causal_parent_ids",
            "component_local_sequence",
            "event_type",
            "global_event_sequence",
            "microstep",
            "payload",
            "schema_version",
            "simulation_time_us",
            "source_component_id",
            "stage",
        }
        _require_exact_fields(payload, fields, "full-day event")
        parents = _exact_array(payload, "causal_parent_ids")
        if any(type(item) is not str for item in parents):
            raise TypeError("serialized causal parent IDs must be strings")
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            global_event_sequence=_exact_int(payload, "global_event_sequence"),
            simulation_time_us=_exact_int(payload, "simulation_time_us"),
            microstep=_exact_int(payload, "microstep"),
            stage=WorkStageV1(_exact_int(payload, "stage")),
            source_component_id=_exact_str(payload, "source_component_id"),
            component_local_sequence=_exact_int(payload, "component_local_sequence"),
            event_type=FullDayEventTypeV1(_exact_str(payload, "event_type")),
            causal_parent_ids=tuple(parents),
            payload=FullDayEventPayloadV1.from_dict(_exact_object(payload, "payload")),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> FullDayEventV1:
        return cls.from_dict(parse_canonical_json_object(raw))


def canonical_event_prefix_sha256(events: Sequence[FullDayEventV1]) -> str:
    """Digest a complete outer-event prefix as one canonical JSON array.

    The empty prefix is the canonical array ``[]``. A nonempty prefix must begin
    at global sequence one and be contiguous, so suffixes and selected subsets
    cannot be mistaken for the checkpointed prefix.
    """

    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
        raise TypeError("event prefix must be a sequence of FullDayEventV1")
    canonical_rows: list[dict[str, object]] = []
    for expected_sequence, event in enumerate(events, start=1):
        if type(event) is not FullDayEventV1:
            raise TypeError("event prefix must contain FullDayEventV1")
        if event.global_event_sequence != expected_sequence:
            raise ValueError(
                "event prefix global sequence must be contiguous and start at one"
            )
        canonical_rows.append(event.as_dict())
    return canonical_sha256(canonical_rows)


def _validate_verified_prefix_ordering(
    events: Sequence[FullDayEventV1],
) -> None:
    previous_chronological_key: tuple[int, int, int] | None = None
    last_component_sequences: dict[str, int] = {}
    last_native_sequences: dict[tuple[str, str], int] = {}
    native_event_ids: set[tuple[str, str, str]] = set()
    prior_event_keys: dict[str, tuple[int, int, int]] = {}
    for expected_sequence, event in enumerate(events, start=1):
        if type(event) is not FullDayEventV1:
            raise TypeError("verified prefix must contain FullDayEventV1")
        if event.global_event_sequence != expected_sequence:
            raise ValueError(
                "verified prefix global sequence must be contiguous and start at one"
            )
        if (
            previous_chronological_key is not None
            and event.chronological_key < previous_chronological_key
        ):
            raise ValueError("verified prefix moves backward chronologically")
        prior_component_sequence = last_component_sequences.get(
            event.source_component_id
        )
        if (
            prior_component_sequence is not None
            and event.component_local_sequence <= prior_component_sequence
        ):
            raise ValueError(
                "verified prefix component-local sequence does not increase per source"
            )
        last_component_sequences[event.source_component_id] = (
            event.component_local_sequence
        )
        native = event.payload.native_event
        if native is not None:
            if native.ledger_key in native_event_ids:
                raise ValueError("verified prefix repeats a native event identity")
            native_owner = (
                native.owner_component_id,
                native.native_ledger_id,
            )
            prior_native_sequence = last_native_sequences.get(native_owner)
            if (
                prior_native_sequence is not None
                and native.local_sequence <= prior_native_sequence
            ):
                raise ValueError(
                    "verified prefix native local sequence does not increase per ledger"
                )
            native_event_ids.add(native.ledger_key)
            last_native_sequences[native_owner] = native.local_sequence
        parent_id = event.causal_parent_ids[0]
        event_parent_match = _EVENT_PARENT_RE.fullmatch(parent_id)
        if event_parent_match is not None:
            if parent_id not in prior_event_keys:
                raise ValueError(
                    "verified prefix event-parent is not an earlier prefix event"
                )
            if prior_event_keys[parent_id] != event.chronological_key:
                raise ValueError(
                    "verified prefix event-parent must share its chronological key"
                )
        prior_event_keys[event.event_id] = event.chronological_key
        previous_chronological_key = event.chronological_key


def validate_deferred_work_key(
    parent: ScheduledWorkKeyV1,
    child: ScheduledWorkKeyV1,
    maximum_microsteps_per_timestamp: int,
) -> None:
    if type(parent) is not ScheduledWorkKeyV1 or type(child) is not ScheduledWorkKeyV1:
        raise TypeError("deferred-work validation requires ScheduledWorkKeyV1")
    if type(maximum_microsteps_per_timestamp) is not int or maximum_microsteps_per_timestamp <= 0:
        raise ValueError("maximum microsteps must be positive")
    if child.simulation_time_us < parent.simulation_time_us:
        raise ValueError("deferred work cannot move backward in simulation time")
    if child.microstep >= maximum_microsteps_per_timestamp:
        raise ValueError("deferred work exceeds the microstep-count bound")
    if child.simulation_time_us > parent.simulation_time_us:
        if child.microstep != 0:
            raise ValueError("future-time deferred work must begin at microstep zero")
        return
    if child.simulation_time_us == parent.simulation_time_us:
        if child.microstep <= parent.microstep:
            raise ValueError("same-time deferred work requires a strictly later microstep")
        if child.stage_ordinal in {
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            WorkStageV1.SCHEDULED_INFORMATION,
        }:
            raise ValueError("calendar/information stages cannot be regenerated at a timestamp")


def validate_full_day_event_stream(
    events: Sequence[FullDayEventV1],
    *,
    executed_work_items: Mapping[str, ScheduledWorkKeyV1],
    native_event_ledger: Mapping[tuple[str, str, str], NativeLedgerEntryV1],
    scheduled_event_ledger: Mapping[str, ScheduledEventV1],
    full_day_plan: FullDayPlanV1,
) -> None:
    """Validate a complete outer stream beginning at global sequence one."""

    _validate_full_day_event_segment(
        events,
        executed_work_items=executed_work_items,
        native_event_ledger=native_event_ledger,
        scheduled_event_ledger=scheduled_event_ledger,
        full_day_plan=full_day_plan,
        suffix_cut_time_us=None,
        verified_prefix_events=(),
        expected_first_global_sequence=1,
        chronological_lower_bound=None,
        verified_prefix_last_global_sequence=0,
    )


def validate_full_day_event_suffix(
    events: Sequence[FullDayEventV1],
    *,
    executed_work_items: Mapping[str, ScheduledWorkKeyV1],
    native_event_ledger: Mapping[tuple[str, str, str], NativeLedgerEntryV1],
    scheduled_event_ledger: Mapping[str, ScheduledEventV1],
    full_day_plan: FullDayPlanV1,
    verified_prefix_cut: QuiescentCutV1,
    verified_prefix_events: Sequence[FullDayEventV1],
) -> None:
    """Validate only events emitted after a verified quiescent checkpoint.

    The checkpoint prefix is intentionally not re-emitted. Its cut binds the prior
    allocator value, digest, and chronological lower bound. Because the cut is
    quiescent, a suffix event may cite only suffix work or an immediately causal
    suffix event; it may not bypass the checkpoint by citing a prefix event directly.
    """

    from .checkpoint_contract import QuiescentCutV1

    if type(verified_prefix_cut) is not QuiescentCutV1:
        raise TypeError("verified_prefix_cut must use QuiescentCutV1")
    verified_prefix_cut.validate_quiescent()
    prefix_sha256 = canonical_event_prefix_sha256(verified_prefix_events)
    _validate_verified_prefix_ordering(verified_prefix_events)
    prefix_length = len(verified_prefix_events)
    if verified_prefix_cut.last_global_event_sequence != prefix_length:
        raise ValueError(
            "checkpoint last global sequence must equal the verified prefix length"
        )
    if verified_prefix_cut.event_prefix_last_global_sequence != prefix_length:
        raise ValueError(
            "checkpoint event-prefix sequence must equal the verified prefix length"
        )
    if verified_prefix_cut.event_prefix_sha256 != prefix_sha256:
        raise ValueError("checkpoint event-prefix digest differs from verified events")
    cut_key = (
        verified_prefix_cut.simulation_time_us,
        verified_prefix_cut.microstep,
        verified_prefix_cut.checkpoint_stage_ordinal,
    )
    if not verified_prefix_events:
        raise ValueError("a full-day checkpoint suffix requires a nonempty event prefix")
    prefix_last_event = verified_prefix_events[-1]
    if prefix_last_event.global_event_sequence != prefix_length:
        raise ValueError(
            "verified prefix last event does not match its sequence length"
        )
    if prefix_last_event.chronological_key != cut_key:
        raise ValueError(
            "verified prefix must end with an event exactly aligned to the checkpoint cut"
        )
    if (
        prefix_last_event.event_type
        is not FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
        or prefix_last_event.source_component_id != "FULL_DAY_RUNTIME_V1"
    ):
        raise ValueError(
            "verified prefix must end with the full-day runtime checkpoint marker"
        )
    _validate_full_day_plan_bindings(
        verified_prefix_events,
        full_day_plan,
        cut_key,
        suffix_cut_time_us=None,
    )
    _validate_shock_lifecycle(verified_prefix_events)
    _validate_full_day_event_segment(
        events,
        executed_work_items=executed_work_items,
        native_event_ledger=native_event_ledger,
        scheduled_event_ledger=scheduled_event_ledger,
        full_day_plan=full_day_plan,
        suffix_cut_time_us=verified_prefix_cut.simulation_time_us,
        verified_prefix_events=verified_prefix_events,
        expected_first_global_sequence=(
            verified_prefix_cut.last_global_event_sequence + 1
        ),
        chronological_lower_bound=(
            verified_prefix_cut.simulation_time_us,
            verified_prefix_cut.microstep,
            verified_prefix_cut.checkpoint_stage_ordinal,
        ),
        verified_prefix_last_global_sequence=(
            verified_prefix_cut.last_global_event_sequence
        ),
    )


def _validate_native_event_ledger(
    events: Sequence[FullDayEventV1],
    native_event_ledger: Mapping[tuple[str, str, str], NativeLedgerEntryV1],
) -> None:
    if not isinstance(native_event_ledger, Mapping):
        raise TypeError(
            "native_event_ledger must map "
            "(owner_component_id,native_ledger_id,event_id) to NativeLedgerEntryV1"
        )
    ledger: dict[tuple[str, str, str], NativeLedgerEntryV1] = {}
    for key, entry in native_event_ledger.items():
        if (
            type(key) is not tuple
            or len(key) != 3
            or any(type(value) is not str or not value for value in key)
        ):
            raise TypeError(
                "native ledger keys must be exact "
                "(owner_component_id,native_ledger_id,event_id) tuples"
            )
        if type(entry) is not NativeLedgerEntryV1:
            raise TypeError("native ledger values must use NativeLedgerEntryV1")
        if key != entry.ledger_key:
            raise ValueError("native ledger key does not match its embedded reference")
        if key in ledger:
            raise ValueError("native ledger contains a duplicate reference")
        ledger[key] = entry

    expected: dict[
        tuple[str, str, str], tuple[NativeEventReferenceV1, FullDayEventV1]
    ] = {}
    for event in events:
        if type(event) is not FullDayEventV1:
            raise TypeError("event stream must contain FullDayEventV1")
        native = event.payload.native_event
        if native is None:
            continue
        key = native.ledger_key
        if key in expected:
            raise ValueError("native subsystem event identity was emitted twice")
        expected[key] = (native, event)

    missing = tuple(sorted(set(expected) - set(ledger)))
    extra = tuple(sorted(set(ledger) - set(expected)))
    if missing or extra:
        raise ValueError(
            "native event ledger does not exactly cover outer native references: "
            f"missing={missing} extra={extra}"
        )
    for key, (native, event) in expected.items():
        entry = ledger[key]
        if entry.reference.as_dict() != native.as_dict():
            raise ValueError("outer native reference differs from its native ledger row")
        if event.payload.data["native_payload_sha256"] != entry.payload_sha256:
            raise ValueError(
                "outer native payload digest differs from native ledger payload"
            )
        if event.event_type is FullDayEventTypeV1.SUBSYSTEM_EVENT:
            continue
        for field_name, outer_value in event.payload.data.items():
            if field_name == "native_payload_sha256":
                continue
            if field_name not in entry.payload:
                raise ValueError(
                    f"typed outer projection field {field_name} is absent from its "
                    "native ledger payload"
                )
            native_value = entry.payload[field_name]
            if type(native_value) is not type(outer_value) or native_value != outer_value:
                raise ValueError(
                    f"typed outer projection field {field_name} differs from its "
                    "native ledger payload"
                )


def _validate_scheduled_event_ledger(
    events: Sequence[FullDayEventV1],
    scheduled_event_ledger: Mapping[str, ScheduledEventV1],
    full_day_plan: FullDayPlanV1,
    horizon_key: tuple[int, int, int] | None,
    suffix_cut_time_us: int | None,
) -> None:
    if not isinstance(scheduled_event_ledger, Mapping):
        raise TypeError(
            "scheduled_event_ledger must map event IDs to ScheduledEventV1"
        )
    ledger: dict[str, ScheduledEventV1] = {}
    plan_rows = {
        scheduled_event.event_id: scheduled_event
        for scheduled_event in full_day_plan.scheduled_events
    }
    for event_id, scheduled_event in scheduled_event_ledger.items():
        _identifier(event_id, "scheduled event ledger key")
        if type(scheduled_event) is not ScheduledEventV1:
            raise TypeError(
                "scheduled event ledger values must use ScheduledEventV1"
            )
        if event_id != scheduled_event.event_id:
            raise ValueError(
                "scheduled event ledger key does not match its embedded event ID"
            )
        plan_row = plan_rows.get(event_id)
        if plan_row is None:
            raise ValueError("scheduled event ledger contains an ID absent from the plan")
        if plan_row.as_dict() != scheduled_event.as_dict():
            raise ValueError("scheduled event ledger row differs from the plan row")
        ledger[event_id] = scheduled_event

    expected: dict[str, FullDayEventV1] = {}
    for event in events:
        if type(event) is not FullDayEventV1:
            raise TypeError("event stream must contain FullDayEventV1")
        if event.event_type is not FullDayEventTypeV1.SCHEDULED_INFORMATION:
            continue
        scheduled_event_id = event.payload.data["scheduled_event_id"]
        if scheduled_event_id in expected:
            raise ValueError(
                "one scheduled event was published more than once in the outer stream"
            )
        expected[scheduled_event_id] = event

    missing = tuple(sorted(set(expected) - set(ledger)))
    extra = tuple(sorted(set(ledger) - set(expected)))
    if missing or extra:
        raise ValueError(
            "scheduled event ledger does not exactly cover SCHEDULED_INFORMATION "
            f"rows: missing={missing} extra={extra}"
        )
    if horizon_key is not None:
        due_plan_ids = tuple(
            scheduled_event.event_id
            for scheduled_event in full_day_plan.scheduled_events
            if (
                suffix_cut_time_us is None
                or scheduled_event.simulation_time_us > suffix_cut_time_us
            )
            and (
                scheduled_event.simulation_time_us,
                0,
                int(WorkStageV1.SCHEDULED_INFORMATION),
            )
            <= horizon_key
        )
        if tuple(expected) != due_plan_ids:
            missing_due = tuple(sorted(set(due_plan_ids) - set(expected)))
            unexpected = tuple(sorted(set(expected) - set(due_plan_ids)))
            raise ValueError(
                "scheduled-information rows do not exactly cover plan events in "
                f"canonical order through the segment horizon: missing={missing_due} "
                f"unexpected={unexpected}"
            )
    for event_id, outer_event in expected.items():
        scheduled_event = ledger[event_id]
        outer_data = outer_event.payload.data
        if outer_event.simulation_time_us != scheduled_event.simulation_time_us:
            raise ValueError("scheduled-information outer time differs from its ledger row")
        if outer_data["scheduled_event_type"] != scheduled_event.event_type.value:
            raise ValueError("scheduled-information type differs from its ledger row")
        if outer_data["side"] != scheduled_event.side.value:
            raise ValueError("scheduled-information side differs from its ledger row")
        if outer_data["parameter_set_sha256"] != scheduled_event.parameter_set_sha256:
            raise ValueError(
                "scheduled-information parameter-set digest differs from its ledger row"
            )


def _duration_support(definition: object) -> frozenset[int]:
    duration_law = getattr(definition, "duration_law")
    return frozenset(mass.duration_us for mass in duration_law.masses)


def _validate_full_day_plan_bindings(
    events: Sequence[FullDayEventV1],
    full_day_plan: FullDayPlanV1,
    horizon_key: tuple[int, int, int] | None,
    suffix_cut_time_us: int | None,
) -> None:
    if type(full_day_plan) is not FullDayPlanV1:
        raise TypeError("full_day_plan must use FullDayPlanV1")
    calendar_indices: list[int] = []
    anchor_indices: list[int] = []
    scheduled_information_ids: list[str] = []
    participant_schedule_ids: list[str] = []
    day_state_events: list[FullDayEventV1] = []
    anchor_times: set[int] = set()
    graph_day_transition_times: set[int] = set()

    day_definitions = {
        definition.state.value: definition
        for definition in full_day_plan.state_model.day_definitions
    }
    local_definitions = {
        definition.state.value: definition
        for definition in full_day_plan.state_model.local_definitions
    }
    day_transitions = {
        transition.transition_id: transition
        for definition in full_day_plan.state_model.day_definitions
        for transition in definition.transitions
    }
    local_transitions = {
        transition.transition_id: transition
        for definition in full_day_plan.state_model.local_definitions
        for transition in definition.transitions
    }
    participant_schedule = {
        entry.schedule_id: entry for entry in full_day_plan.participant_schedule
    }
    scheduled_events = {
        entry.event_id: entry for entry in full_day_plan.scheduled_events
    }
    action_event_types = {
        ParticipantScheduleActionV1.ACTIVATE: (
            FullDayEventTypeV1.PARTICIPANT_ACTIVATED
        ),
        ParticipantScheduleActionV1.DEACTIVATE: (
            FullDayEventTypeV1.PARTICIPANT_DEACTIVATED
        ),
        ParticipantScheduleActionV1.RETUNE: FullDayEventTypeV1.PARTICIPANT_RETUNED,
    }

    current_day_state = (
        None
        if suffix_cut_time_us is not None
        else full_day_plan.state_model.initial_day_state.value
    )
    current_local_state = (
        None
        if suffix_cut_time_us is not None
        else full_day_plan.state_model.initial_local_state.value
    )

    for event in events:
        data = event.payload.data
        if event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "calendar boundary source must be FULL_DAY_RUNTIME_V1"
                )
            boundary_index = data["boundary_operation_index"]
            if boundary_index >= len(full_day_plan.calendar.boundary_operations):
                raise ValueError("calendar boundary index is absent from the plan")
            operation = full_day_plan.calendar.boundary_operations[boundary_index]
            if event.simulation_time_us != operation.boundary.simulation_time_us:
                raise ValueError("calendar boundary time differs from the plan")
            if (
                data["destination_session_state"]
                != operation.destination_session_state.value
                or data["uncross_before"] is not operation.uncross_before
            ):
                raise ValueError("calendar boundary tuple differs from the plan")
            calendar_indices.append(boundary_index)
            continue

        if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "scheduled-information source must be FULL_DAY_RUNTIME_V1"
                )
            scheduled_event_id = data["scheduled_event_id"]
            scheduled_event = scheduled_events.get(scheduled_event_id)
            if scheduled_event is None:
                raise ValueError(
                    "scheduled-information ID is absent from the full-day plan"
                )
            if (
                event.simulation_time_us != scheduled_event.simulation_time_us
                or data["scheduled_event_type"] != scheduled_event.event_type.value
                or data["side"] != scheduled_event.side.value
                or data["parameter_set_sha256"]
                != scheduled_event.parameter_set_sha256
            ):
                raise ValueError(
                    "scheduled-information event differs from its exact plan row"
                )
            scheduled_information_ids.append(scheduled_event_id)
            continue

        if event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "day-state anchor source must be FULL_DAY_RUNTIME_V1"
                )
            macro_index = data["macro_segment_index"]
            if macro_index >= len(full_day_plan.macro_regime_schedule):
                raise ValueError("macro anchor index is absent from the plan")
            segment = full_day_plan.macro_regime_schedule[macro_index]
            if event.simulation_time_us != segment.start_us:
                raise ValueError("macro anchor time differs from its plan segment")
            if data["anchored_state"] != segment.day_state.value:
                raise ValueError("macro anchor state differs from its plan segment")
            if data["macro_segment_sha256"] != canonical_sha256(segment.as_dict()):
                raise ValueError("macro anchor digest differs from its plan segment")
            if data["sampled_duration_us"] not in _duration_support(
                day_definitions[segment.day_state.value]
            ):
                raise ValueError(
                    "macro anchor duration is absent from the anchored-state mass support"
                )
            if (
                current_day_state is not None
                and data["previous_state"] != current_day_state
            ):
                raise ValueError("day-state continuity breaks at a macro anchor")
            current_day_state = data["anchored_state"]
            anchor_indices.append(macro_index)
            anchor_times.add(event.simulation_time_us)
            day_state_events.append(event)
            continue

        if event.event_type is FullDayEventTypeV1.DAY_STATE_TRANSITION:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "day-state transition source must be FULL_DAY_RUNTIME_V1"
                )
            transition = day_transitions.get(data["transition_id"])
            if transition is None:
                raise ValueError("day-state transition ID is absent from the plan graph")
            if (
                data["previous_state"] != transition.source_state
                or data["new_state"] != transition.successor_state
                or data["trigger_id"] != transition.trigger_id
                or data["trigger_version"] != transition.trigger_version
            ):
                raise ValueError("day-state event differs from its exact plan edge")
            if data["sampled_duration_us"] not in _duration_support(
                day_definitions[transition.successor_state]
            ):
                raise ValueError(
                    "day-state duration is absent from successor mass support"
                )
            if (
                current_day_state is not None
                and data["previous_state"] != current_day_state
            ):
                raise ValueError("day-state transition breaks stream continuity")
            current_day_state = data["new_state"]
            graph_day_transition_times.add(event.simulation_time_us)
            day_state_events.append(event)
            continue

        if event.event_type is FullDayEventTypeV1.LOCAL_STATE_TRANSITION:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "local-state transition source must be FULL_DAY_RUNTIME_V1"
                )
            transition = local_transitions.get(data["transition_id"])
            if transition is None:
                raise ValueError("local-state transition ID is absent from the plan graph")
            if (
                data["previous_state"] != transition.source_state
                or data["new_state"] != transition.successor_state
                or data["trigger_id"] != transition.trigger_id
                or data["trigger_version"] != transition.trigger_version
            ):
                raise ValueError("local-state event differs from its exact plan edge")
            if data["sampled_duration_us"] not in _duration_support(
                local_definitions[transition.successor_state]
            ):
                raise ValueError(
                    "local-state duration is absent from successor mass support"
                )
            if (
                current_local_state is not None
                and data["previous_state"] != current_local_state
            ):
                raise ValueError("local-state transition breaks stream continuity")
            current_local_state = data["new_state"]
            continue

        if event.event_type in {
            FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
            FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
            FullDayEventTypeV1.PARTICIPANT_RETUNED,
        }:
            if event.source_component_id != "AGENT_SCHEDULER_V1":
                raise ValueError(
                    "participant schedule event source must be AGENT_SCHEDULER_V1"
                )
            schedule_id = data["schedule_id"]
            schedule_entry = participant_schedule.get(schedule_id)
            if schedule_entry is None:
                raise ValueError("participant schedule ID is absent from the plan")
            if event.event_type is not action_event_types[schedule_entry.action]:
                raise ValueError("participant event type differs from its plan action")
            if (
                event.simulation_time_us != schedule_entry.simulation_time_us
                or event.microstep != 0
                or data["participant_id"] != schedule_entry.participant_id
            ):
                raise ValueError("participant event differs from its plan schedule row")
            if event.event_type is FullDayEventTypeV1.PARTICIPANT_RETUNED:
                replacement = schedule_entry.replacement_specification
                assert replacement is not None
                if data["replacement_specification_sha256"] != replacement.sha256:
                    raise ValueError(
                        "participant retune digest differs from its plan schedule row"
                    )
            participant_schedule_ids.append(schedule_id)
            continue

        if event.event_type is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError(
                    "checkpoint marker source must be FULL_DAY_RUNTIME_V1"
                )

    if len(calendar_indices) != len(set(calendar_indices)):
        raise ValueError("calendar boundary index is duplicated within the segment")
    if calendar_indices != sorted(calendar_indices):
        raise ValueError("calendar boundaries are not in canonical operation order")
    if len(anchor_indices) != len(set(anchor_indices)):
        raise ValueError("macro anchor index is duplicated within the segment")
    if anchor_indices != sorted(anchor_indices):
        raise ValueError("macro anchors are not in canonical segment order")
    if anchor_times.intersection(graph_day_transition_times):
        raise ValueError(
            "macro anchor replacement forbids a same-time day graph transition"
        )
    if len(scheduled_information_ids) != len(set(scheduled_information_ids)):
        raise ValueError("scheduled-information ID is duplicated in the outer segment")
    if len(participant_schedule_ids) != len(set(participant_schedule_ids)):
        raise ValueError("participant schedule ID is duplicated in the outer segment")

    if suffix_cut_time_us is None and day_state_events:
        first_day_event = day_state_events[0]
        if (
            first_day_event.event_type is not FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET
            or first_day_event.payload.data["macro_segment_index"] != 0
            or first_day_event.payload.data["previous_state"]
            != full_day_plan.state_model.initial_day_state.value
            or first_day_event.payload.data["anchored_state"]
            != full_day_plan.state_model.initial_day_state.value
        ):
            raise ValueError(
                "a full stream with day-state events must begin at the index-0 "
                "initial-state macro anchor"
            )

    if horizon_key is None:
        return
    due_boundary_indices = {
        index
        for index, operation in enumerate(
            full_day_plan.calendar.boundary_operations
        )
        if (
            suffix_cut_time_us is None
            or operation.boundary.simulation_time_us > suffix_cut_time_us
        )
        and (
            operation.boundary.simulation_time_us,
            0,
            int(WorkStageV1.ATOMIC_CALENDAR_BOUNDARY),
        )
        <= horizon_key
    }
    if set(calendar_indices) != due_boundary_indices:
        raise ValueError(
            "calendar events do not exactly cover plan boundaries due through "
            "the segment horizon"
        )
    due_anchor_indices = {
        index
        for index, segment in enumerate(full_day_plan.macro_regime_schedule)
        if (
            suffix_cut_time_us is None or segment.start_us > suffix_cut_time_us
        )
        and (
            segment.start_us,
            0,
            int(WorkStageV1.DAY_STATE_TRANSITION),
        )
        <= horizon_key
    }
    if set(anchor_indices) != due_anchor_indices:
        raise ValueError(
            "macro anchors do not exactly cover plan segments due through the "
            "outer segment horizon"
        )
    due_scheduled_information_ids = tuple(
        entry.event_id
        for entry in full_day_plan.scheduled_events
        if (
            suffix_cut_time_us is None
            or entry.simulation_time_us > suffix_cut_time_us
        )
        and (
            entry.simulation_time_us,
            0,
            int(WorkStageV1.SCHEDULED_INFORMATION),
        )
        <= horizon_key
    )
    if tuple(scheduled_information_ids) != due_scheduled_information_ids:
        raise ValueError(
            "scheduled-information events do not exactly cover plan rows in canonical "
            "order through the segment horizon"
        )
    due_participant_schedule_ids = tuple(
        entry.schedule_id
        for entry in full_day_plan.participant_schedule
        if (
            suffix_cut_time_us is None
            or entry.simulation_time_us > suffix_cut_time_us
        )
        and (
            entry.simulation_time_us,
            0,
            int(WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE),
        )
        <= horizon_key
    )
    if tuple(participant_schedule_ids) != due_participant_schedule_ids:
        raise ValueError(
            "participant events do not exactly cover plan schedule rows in canonical "
            "order through the segment horizon"
        )


def _validate_shock_lifecycle(events: Sequence[FullDayEventV1]) -> None:
    candidates: dict[str, FullDayEventV1] = {}
    terminal_candidate_ids: set[str] = set()
    for event in events:
        if type(event) is not FullDayEventV1:
            raise TypeError("event stream must contain FullDayEventV1")
        if event.event_type is FullDayEventTypeV1.SHOCK_CANDIDATE:
            if event.source_component_id != "FULL_DAY_RUNTIME_V1":
                raise ValueError("shock candidate source must be FULL_DAY_RUNTIME_V1")
            candidate_id = event.payload.data["candidate_id"]
            if candidate_id in candidates:
                raise ValueError("shock candidate IDs must be unique")
            candidates[candidate_id] = event
            continue
        if event.event_type not in {
            FullDayEventTypeV1.SHOCK_ACCEPTED,
            FullDayEventTypeV1.SHOCK_REJECTED,
        }:
            continue
        candidate_id = event.payload.data["candidate_id"]
        candidate_event = candidates.get(candidate_id)
        if candidate_event is None:
            raise ValueError("shock terminal must follow its unique candidate")
        if candidate_id in terminal_candidate_ids:
            raise ValueError("shock candidate may have exactly one terminal result")
        if event.chronological_key != candidate_event.chronological_key:
            raise ValueError("shock terminal must be synchronous with its candidate")
        if event.causal_parent_ids != (candidate_event.event_id,):
            raise ValueError("shock terminal must directly cite its candidate event")
        if event.source_component_id != candidate_event.source_component_id:
            raise ValueError("shock terminal source must equal its candidate source")
        candidate = candidate_event.payload.data
        if (
            event.payload.data["information_cutoff_us"]
            != candidate["information_cutoff_us"]
        ):
            raise ValueError("shock terminal cutoff differs from its candidate")
        if event.event_type is FullDayEventTypeV1.SHOCK_ACCEPTED:
            for field_name in ("quantity_shares", "side"):
                if event.payload.data[field_name] != candidate[field_name]:
                    raise ValueError(
                        f"accepted shock {field_name} differs from its candidate"
                    )
        terminal_candidate_ids.add(candidate_id)
    unresolved = tuple(sorted(set(candidates) - terminal_candidate_ids))
    if unresolved:
        raise ValueError(f"shock candidates require one terminal result: {unresolved}")


def _validate_full_day_event_segment(
    events: Sequence[FullDayEventV1],
    *,
    executed_work_items: Mapping[str, ScheduledWorkKeyV1],
    native_event_ledger: Mapping[tuple[str, str, str], NativeLedgerEntryV1],
    scheduled_event_ledger: Mapping[str, ScheduledEventV1],
    full_day_plan: FullDayPlanV1,
    suffix_cut_time_us: int | None,
    verified_prefix_events: Sequence[FullDayEventV1],
    expected_first_global_sequence: int,
    chronological_lower_bound: tuple[int, int, int] | None,
    verified_prefix_last_global_sequence: int,
) -> None:
    if type(full_day_plan) is not FullDayPlanV1:
        raise TypeError("full_day_plan must use FullDayPlanV1")
    if suffix_cut_time_us is not None and (
        type(suffix_cut_time_us) is not int or suffix_cut_time_us < 0
    ):
        raise ValueError("suffix cut time must be nonnegative integer microseconds")
    if (
        type(expected_first_global_sequence) is not int
        or expected_first_global_sequence <= 0
        or type(verified_prefix_last_global_sequence) is not int
        or verified_prefix_last_global_sequence < 0
        or expected_first_global_sequence
        != verified_prefix_last_global_sequence + 1
    ):
        raise ValueError("event segment allocator boundary is invalid")
    if chronological_lower_bound is not None and (
        type(chronological_lower_bound) is not tuple
        or len(chronological_lower_bound) != 3
        or any(type(value) is not int or value < 0 for value in chronological_lower_bound)
    ):
        raise TypeError("event segment chronological lower bound is invalid")
    if not isinstance(executed_work_items, Mapping):
        raise TypeError("executed_work_items must map work IDs to ScheduledWorkKeyV1")
    for work_id, work in executed_work_items.items():
        if type(work_id) is not str or type(work) is not ScheduledWorkKeyV1:
            raise TypeError("executed-work entries use the wrong key/value contract")
        if work_id != work.work_id:
            raise ValueError("executed-work key does not match its work identity")
    for event in events:
        if type(event) is not FullDayEventV1:
            raise TypeError("event stream must contain FullDayEventV1")
        if (
            suffix_cut_time_us is not None
            and event.simulation_time_us <= suffix_cut_time_us
        ):
            raise ValueError(
                "a quiescent checkpoint suffix must begin at a strictly later "
                "simulation time"
            )
    horizon_key = max(
        (event.chronological_key for event in events),
        default=None,
    )
    _validate_native_event_ledger(events, native_event_ledger)
    _validate_scheduled_event_ledger(
        events,
        scheduled_event_ledger,
        full_day_plan,
        horizon_key,
        suffix_cut_time_us,
    )
    _validate_full_day_plan_bindings(
        events,
        full_day_plan,
        horizon_key,
        suffix_cut_time_us,
    )
    _validate_shock_lifecycle(events)
    previous_chronological_key = chronological_lower_bound
    prior_event_ids: set[str] = set()
    prior_event_keys: dict[str, tuple[int, int, int]] = {}
    prior_event_work_ids: dict[str, str] = {}
    native_event_ids: set[tuple[str, str, str]] = {
        event.payload.native_event.ledger_key
        for event in verified_prefix_events
        if event.payload.native_event is not None
    }
    last_native_sequences: dict[tuple[str, str], int] = {}
    last_component_sequences: dict[str, int] = {}
    for prefix_event in verified_prefix_events:
        last_component_sequences[prefix_event.source_component_id] = (
            prefix_event.component_local_sequence
        )
        prefix_native = prefix_event.payload.native_event
        if prefix_native is not None:
            last_native_sequences[
                (prefix_native.owner_component_id, prefix_native.native_ledger_id)
            ] = prefix_native.local_sequence
    current_work_id: str | None = None
    completed_work_ids: set[str] = set()
    previous_work_ordering_key: tuple[int, int, int, str, int] | None = None
    for expected_sequence, event in enumerate(
        events, start=expected_first_global_sequence
    ):
        if type(event) is not FullDayEventV1:
            raise TypeError("event stream must contain FullDayEventV1")
        if event.global_event_sequence != expected_sequence:
            raise ValueError("global event sequence must be contiguous and start at 1")
        if (
            previous_chronological_key is not None
            and event.chronological_key < previous_chronological_key
        ):
            raise ValueError("outer event stream moves backward in time/microstep/stage")
        prior_component_sequence = last_component_sequences.get(
            event.source_component_id
        )
        if (
            prior_component_sequence is not None
            and event.component_local_sequence <= prior_component_sequence
        ):
            raise ValueError(
                "outer component-local sequence must increase per source component"
            )
        last_component_sequences[event.source_component_id] = (
            event.component_local_sequence
        )
        native = event.payload.native_event
        if native is not None:
            native_key = native.ledger_key
            if native_key in native_event_ids:
                raise ValueError("native subsystem event identity was emitted twice")
            native_sequence_owner = (
                native.owner_component_id,
                native.native_ledger_id,
            )
            prior_native_sequence = last_native_sequences.get(native_sequence_owner)
            if (
                prior_native_sequence is not None
                and native.local_sequence <= prior_native_sequence
            ):
                raise ValueError(
                    "native local sequence must increase per owner/ledger within "
                    "an outer event segment"
                )
            native_event_ids.add(native_key)
            last_native_sequences[native_sequence_owner] = native.local_sequence
        causal_work_ids: set[str] = set()
        for parent_id in event.causal_parent_ids:
            if _WORK_PARENT_RE.fullmatch(parent_id):
                work = executed_work_items.get(parent_id)
                if work is None:
                    raise ValueError("event cites an unexecuted/orphan causal work item")
                if (
                    work.simulation_time_us != event.simulation_time_us
                    or work.microstep != event.microstep
                    or work.stage_ordinal is not event.stage
                ):
                    raise ValueError("event does not align with its causal work item")
                causal_work_ids.add(parent_id)
            event_parent_match = _EVENT_PARENT_RE.fullmatch(parent_id)
            if event_parent_match is not None:
                parent_sequence = int(event_parent_match.group(1))
                if parent_sequence <= verified_prefix_last_global_sequence:
                    raise ValueError(
                        "a quiescent suffix event cannot cite a checkpoint-prefix event"
                    )
                if parent_id not in prior_event_ids:
                    raise ValueError("event cites a future or nonexistent causal outer event")
                if prior_event_keys[parent_id] != event.chronological_key:
                    raise ValueError(
                        "deferred causality must cite a newly executed work item, not "
                        "an outer event from another time/microstep/stage"
                    )
                causal_work_ids.add(prior_event_work_ids[parent_id])
        if len(causal_work_ids) != 1:
            raise ValueError("every outer event must resolve to exactly one dequeued work item")
        resolved_work_id = next(iter(causal_work_ids))
        if resolved_work_id != current_work_id:
            if resolved_work_id in completed_work_ids:
                raise ValueError("outer events for one dequeued work item must be contiguous")
            resolved_work = executed_work_items[resolved_work_id]
            if (
                previous_work_ordering_key is not None
                and resolved_work.ordering_key <= previous_work_ordering_key
            ):
                raise ValueError(
                    "dequeued work items are not represented in five-field queue order"
                )
            if current_work_id is not None:
                completed_work_ids.add(current_work_id)
            current_work_id = resolved_work_id
            previous_work_ordering_key = resolved_work.ordering_key
        previous_chronological_key = event.chronological_key
        prior_event_ids.add(event.event_id)
        prior_event_keys[event.event_id] = event.chronological_key
        prior_event_work_ids[event.event_id] = resolved_work_id


__all__ = [
    "CALENDAR_BOUNDARY_INDEX_V1",
    "FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1",
    "FULL_DAY_EVENT_SCHEMA_VERSION",
    "FULL_DAY_PAYLOAD_FIELD_RULES_V1",
    "FULL_DAY_PAYLOAD_SCHEMA_VERSION",
    "FullDayEventPayloadV1",
    "FullDayEventType",
    "FullDayEventTypeV1",
    "FullDayEventV1",
    "FullDayPayloadFieldRuleV1",
    "NATIVE_EVENT_REFERENCE_SCHEMA_VERSION",
    "NativeLedgerEntryV1",
    "NativeEventReferenceV1",
    "ScheduledWorkKeyV1",
    "WorkStage",
    "WorkStageV1",
    "canonical_event_prefix_sha256",
    "validate_deferred_work_key",
    "validate_full_day_event_suffix",
    "validate_full_day_event_stream",
]
