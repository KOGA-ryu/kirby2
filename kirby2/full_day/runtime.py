"""Authoritative deterministic scheduling kernel for one synthetic full day.

Only ``SINGLE_VENUE_AGENT_MECHANICS_V1`` is executable here.  The runtime owns
the calendar, simulation clock, scheduling heap, global/event/order allocators,
quiescent-cut controller, and the one market-mechanics engine.  Optional agent
code is an injected scheduler over those owners; it cannot advance time or
construct a second exchange.
"""

from __future__ import annotations

import heapq
import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from kirby2.exchange import (
    AdvancedOrderRequest,
    MarketMechanicsEngine,
    MechanicsEvent,
    MechanicsEventType,
    SessionState,
)
from kirby2.exchange.auction import AuctionBook
from kirby2.exchange.book import OrderBook
from kirby2.immutable import freeze_json, thaw_json
from kirby2.simulation.clock import SimulationClock

from .calendar import TradingDayCalendarV1
from .checkpoint_contract import QuiescentCutV1
from .composition import (
    ABSENT_REASON_COMPONENT_INACTIVE,
    AGENT_SCHEDULER_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
    INITIAL_PROFILE_ID,
    MECHANICS_COMPONENT,
    agent_scheduler_is_active,
    executable_agent_mechanics_composition_matrix,
)
from .events import (
    FullDayEventPayloadV1,
    FullDayEventTypeV1,
    FullDayEventV1,
    NativeEventReferenceV1,
    NativeLedgerEntryV1,
    ScheduledWorkKeyV1,
    WorkStageV1,
    canonical_event_prefix_sha256,
    validate_full_day_event_stream,
)
from .models import (
    FullDayPlanV1,
    ParticipantScheduleActionV1,
    ScheduledEventTypeV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from .transitions import (
    DayStateAnchorEmissionV1,
    HierarchicalStateRuntimeStateV1,
    HierarchicalStateRuntimeV1,
    StateTransitionEmissionV1,
)
from .states import DurationExhaustionBehaviorV1


FULL_DAY_RUNTIME_CHECKPOINT_SCHEMA_VERSION = 1
FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION = 1
FULL_DAY_RUNTIME_PROFILE_ID = INITIAL_PROFILE_ID
FULL_DAY_RUNTIME_PROFILE_VERSION = 2
MECHANICS_NATIVE_LEDGER_ID = "MARKET_MECHANICS_EVENTS_V1"
AGENT_NATIVE_LEDGER_ID = "AGENT_SCHEDULER_EVENTS_V1"

_WORK_CALENDAR_BOUNDARY = "CALENDAR_BOUNDARY"
_WORK_SCHEDULED_INFORMATION = "SCHEDULED_INFORMATION"
_WORK_STATE_EMISSION = "STATE_EMISSION"
_WORK_PARTICIPANT_SCHEDULE = "PARTICIPANT_SCHEDULE"
_WORK_AGENT_ARRIVAL = "AGENT_ARRIVAL"
_WORK_AGENT_DECISION = "AGENT_DECISION"
_WORK_CHECKPOINT_CAPTURE = "CHECKPOINT_CAPTURE"
_WORK_MECHANICS_SUBMIT = "MECHANICS_SUBMIT"
_WORK_MECHANICS_CANCEL = "MECHANICS_CANCEL"
_WORK_MECHANICS_REPLACE = "MECHANICS_REPLACE"
_WORK_GTT_EXPIRY = "GTT_EXPIRY"
_WORK_REOPEN_COMPLETE = "REOPEN_COMPLETE"

_WORK_TYPES = frozenset(
    {
        _WORK_AGENT_ARRIVAL,
        _WORK_AGENT_DECISION,
        _WORK_CALENDAR_BOUNDARY,
        _WORK_CHECKPOINT_CAPTURE,
        _WORK_GTT_EXPIRY,
        _WORK_MECHANICS_CANCEL,
        _WORK_MECHANICS_REPLACE,
        _WORK_MECHANICS_SUBMIT,
        _WORK_PARTICIPANT_SCHEDULE,
        _WORK_REOPEN_COMPLETE,
        _WORK_SCHEDULED_INFORMATION,
        _WORK_STATE_EMISSION,
    }
)
_RUNTIME_ORDER_ID_RE = re.compile(r"FD-O-([0-9]{10})\Z")
_WORK_CONTRACTS: Mapping[
    str,
    tuple[str, frozenset[WorkStageV1], frozenset[str]],
] = MappingProxyType(
    {
        _WORK_AGENT_ARRIVAL: (
            AGENT_SCHEDULER_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset(),
        ),
        _WORK_AGENT_DECISION: (
            AGENT_SCHEDULER_COMPONENT,
            frozenset({WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION}),
            frozenset(),
        ),
        _WORK_CALENDAR_BOUNDARY: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.ATOMIC_CALENDAR_BOUNDARY}),
            frozenset({"boundary_operation_index"}),
        ),
        _WORK_CHECKPOINT_CAPTURE: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.CHECKPOINT_CAPTURE}),
            frozenset({"checkpoint_request_ids"}),
        ),
        _WORK_GTT_EXPIRY: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset({"expiry_time_us"}),
        ),
        _WORK_MECHANICS_CANCEL: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset({"order_id", "reason"}),
        ),
        _WORK_MECHANICS_REPLACE: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset(
                {"new_order_id", "new_price_ticks", "new_quantity", "order_id"}
            ),
        ),
        _WORK_MECHANICS_SUBMIT: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset({"request"}),
        ),
        _WORK_PARTICIPANT_SCHEDULE: (
            AGENT_SCHEDULER_COMPONENT,
            frozenset(
                {WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE}
            ),
            frozenset({"schedule_id"}),
        ),
        _WORK_REOPEN_COMPLETE: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.PENDING_VENUE_ARRIVAL}),
            frozenset({"scheduled_event_id"}),
        ),
        _WORK_SCHEDULED_INFORMATION: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset({WorkStageV1.SCHEDULED_INFORMATION}),
            frozenset({"scheduled_event_id"}),
        ),
        _WORK_STATE_EMISSION: (
            FULL_DAY_RUNTIME_COMPONENT,
            frozenset(
                {
                    WorkStageV1.DAY_STATE_TRANSITION,
                    WorkStageV1.LOCAL_STATE_TRANSITION,
                }
            ),
            frozenset({"batch_time_us", "emission_index"}),
        ),
    }
)


def _validate_runtime_work_contract(item: RuntimeWorkItemV1) -> None:
    """Bind every pending work payload to one exact owner/stage contract."""

    owner, stages, payload_fields = _WORK_CONTRACTS[item.work_type]
    if item.key.source_component_id != owner:
        raise ValueError(
            f"{item.work_type} work is owned by {owner}, not "
            f"{item.key.source_component_id}"
        )
    if item.key.stage_ordinal not in stages:
        raise ValueError(f"{item.work_type} work uses a forbidden stage")
    _require_exact_fields(item.payload, set(payload_fields), item.work_type)


def _validate_component_allocation_inventory(
    *,
    events: Sequence[FullDayEventV1],
    pending: Sequence[RuntimeWorkItemV1],
    executed: Sequence[RuntimeWorkItemV1],
    retired: Sequence[RuntimeWorkItemV1],
    component_sequences: Mapping[str, int],
) -> None:
    """Reconcile every issued component-local identity without gaps or reuse."""

    issued: dict[str, set[int]] = {
        component_id: set() for component_id in component_sequences
    }

    def record(component_id: str, sequence: int, label: str) -> None:
        if component_id not in issued:
            raise ValueError(f"{label} cites an inactive component allocator")
        if sequence in issued[component_id]:
            raise ValueError(
                f"component-local sequence is reused by {label}: "
                f"{component_id}:{sequence}"
            )
        issued[component_id].add(sequence)

    for event in events:
        record(
            event.source_component_id,
            event.component_local_sequence,
            "outer event",
        )
    for item in pending:
        record(
            item.key.source_component_id,
            item.key.component_local_sequence,
            "pending work",
        )
    for item in executed:
        record(
            item.key.source_component_id,
            item.key.component_local_sequence,
            "executed work",
        )
    for item in retired:
        record(
            item.key.source_component_id,
            item.key.component_local_sequence,
            "retired work",
        )

    for component_id, highwater in component_sequences.items():
        if type(highwater) is not int or highwater < 0:
            raise ValueError("component allocator highwater is not canonical")
        if issued[component_id] != set(range(1, highwater + 1)):
            raise ValueError(
                "component-local allocation inventory has a gap or hidden identity"
            )


def _plain(value: object) -> object:
    return thaw_json(freeze_json(value))


def _plain_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a strict JSON object")
    validate_strict_json(value)
    result = _plain(value)
    if type(result) is not dict:  # pragma: no cover - mapping checked above
        raise TypeError(f"{field} did not detach to an object")
    return result


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_optional_int(value: object, field: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _exact_int(value, field, minimum=minimum)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty identifier")
    validate_strict_json(value)
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:-" for character in value):
        raise ValueError(f"{field} contains a noncanonical character")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeWorkItemV1:
    """One serializable payload bound to the frozen five-field queue key."""

    key: ScheduledWorkKeyV1
    work_type: str
    payload: Mapping[str, object]
    causal_parent_id: str

    def __post_init__(self) -> None:
        if type(self.key) is not ScheduledWorkKeyV1:
            raise TypeError("runtime work key must use ScheduledWorkKeyV1")
        if self.work_type not in _WORK_TYPES:
            raise ValueError("runtime work type is unsupported")
        if not isinstance(self.payload, Mapping):
            raise TypeError("runtime work payload must be a JSON object")
        validate_strict_json(self.payload)
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):  # pragma: no cover
            raise TypeError("runtime work payload must remain an object")
        if self.causal_parent_id != self.key.work_id:
            raise ValueError("pending runtime work must cite its exact work identity")
        object.__setattr__(self, "payload", frozen)
        _validate_runtime_work_contract(self)

    @property
    def work_id(self) -> str:
        return self.key.work_id

    def as_dict(self) -> dict[str, object]:
        return {
            "causal_parent_id": self.causal_parent_id,
            "key": self.key.as_dict(),
            "payload": _plain(self.payload),
            "work_type": self.work_type,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RuntimeWorkItemV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {"causal_parent_id", "key", "payload", "work_type"},
            "RuntimeWorkItemV1",
        )
        raw_key = payload["key"]
        raw_payload = payload["payload"]
        if not isinstance(raw_key, Mapping) or not isinstance(raw_payload, Mapping):
            raise TypeError("serialized runtime work key/payload must be objects")
        return cls(
            key=ScheduledWorkKeyV1.from_dict(raw_key),
            work_type=_identifier(payload["work_type"], "work_type"),
            payload=raw_payload,
            causal_parent_id=_identifier(
                payload["causal_parent_id"], "causal_parent_id"
            ),
        )


@dataclass(slots=True)
class RuntimeOrderIdAllocatorV1:
    """The only order-ID allocator in the E1 runtime profile."""

    next_sequence: int = 1

    def __post_init__(self) -> None:
        _exact_int(self.next_sequence, "order allocator next sequence", minimum=1)

    def allocate(self) -> str:
        value = self.next_sequence
        self.next_sequence += 1
        return f"FD-O-{value:010d}"

    def checkpoint_state(self) -> dict[str, object]:
        return {"next_sequence": self.next_sequence, "schema_version": 1}

    @classmethod
    def from_checkpoint_state(
        cls, payload: Mapping[str, object]
    ) -> RuntimeOrderIdAllocatorV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload, {"next_sequence", "schema_version"}, "order allocator"
        )
        if payload["schema_version"] != 1:
            raise ValueError("unsupported order allocator schema")
        return cls(_exact_int(payload["next_sequence"], "next_sequence", minimum=1))


def _validate_plan_work_inventory(
    *,
    plan: FullDayPlanV1,
    current_time_us: int,
    calendar_boundary_index: int,
    scheduled_event_index: int,
    participant_schedule_index: int,
    pending: Sequence[RuntimeWorkItemV1],
    events: Sequence[FullDayEventV1],
) -> None:
    """Require every immutable plan suffix row exactly once in the queue."""

    def signature(item: RuntimeWorkItemV1) -> tuple[object, ...]:
        return (
            item.key.simulation_time_us,
            item.key.microstep,
            item.key.stage_ordinal,
            item.key.source_component_id,
            item.work_type,
            _plain(item.payload),
        )

    expected_calendar = tuple(
        (
            operation.boundary.simulation_time_us,
            0,
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            FULL_DAY_RUNTIME_COMPONENT,
            _WORK_CALENDAR_BOUNDARY,
            {"boundary_operation_index": index},
        )
        for index, operation in enumerate(
            plan.calendar.boundary_operations[calendar_boundary_index:],
            start=calendar_boundary_index,
        )
    )
    expected_scheduled = tuple(
        (
            event.simulation_time_us,
            0,
            WorkStageV1.SCHEDULED_INFORMATION,
            FULL_DAY_RUNTIME_COMPONENT,
            _WORK_SCHEDULED_INFORMATION,
            {"scheduled_event_id": event.event_id},
        )
        for event in plan.scheduled_events[scheduled_event_index:]
    )
    expected_participants = tuple(
        (
            entry.simulation_time_us,
            0,
            WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
            AGENT_SCHEDULER_COMPONENT,
            _WORK_PARTICIPANT_SCHEDULE,
            {"schedule_id": entry.schedule_id},
        )
        for entry in plan.participant_schedule[participant_schedule_index:]
    )
    for work_type, expected, label in (
        (_WORK_CALENDAR_BOUNDARY, expected_calendar, "calendar"),
        (_WORK_SCHEDULED_INFORMATION, expected_scheduled, "scheduled-event"),
        (_WORK_PARTICIPANT_SCHEDULE, expected_participants, "participant"),
    ):
        observed = tuple(
            signature(item)
            for item in sorted(pending, key=lambda row: row.key.ordering_key)
            if item.work_type == work_type
        )
        if observed != expected:
            raise ValueError(f"pending {label} work differs from the exact plan suffix")

    markers_by_id = {
        str(event.payload.data["checkpoint_request_id"]): event
        for event in events
        if event.event_type is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
    }
    for sequence, time_us in enumerate(plan.resolved_checkpoint_times_us, start=1):
        request_id = f"CHECKPOINT-{sequence:06d}"
        marker = markers_by_id.get(request_id)
        pending_matches = tuple(
            item
            for item in pending
            if item.work_type == _WORK_CHECKPOINT_CAPTURE
            and request_id in item.payload["checkpoint_request_ids"]
        )
        if marker is not None:
            if marker.simulation_time_us != time_us or pending_matches:
                raise ValueError(
                    "resolved checkpoint policy differs from completed evidence"
                )
        elif (
            len(pending_matches) != 1
            or pending_matches[0].key.simulation_time_us != time_us
            or pending_matches[0].key.microstep != 0
            or time_us < current_time_us
        ):
            raise ValueError(
                "resolved checkpoint policy differs from pending work"
            )


def _validate_checkpoint_cut_inventory(
    *,
    cuts: Sequence[QuiescentCutV1],
    events: Sequence[FullDayEventV1],
    executed: Sequence[ScheduledWorkKeyV1],
    pending: Sequence[RuntimeWorkItemV1],
    current_time_us: int,
    require_current_cut: bool,
) -> None:
    """Cross-bind every cut to one exact checkpoint-marker causal chain."""

    groups: list[tuple[FullDayEventV1, ...]] = []
    active: list[FullDayEventV1] = []
    previous_index: int | None = None
    for index, event in enumerate(events):
        if event.event_type is not FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER:
            if active:
                groups.append(tuple(active))
                active = []
                previous_index = None
            continue
        continues_chain = bool(
            active
            and previous_index is not None
            and index == previous_index + 1
            and event.causal_parent_ids == (active[-1].event_id,)
        )
        if not continues_chain and active:
            groups.append(tuple(active))
            active = []
        active.append(event)
        previous_index = index
    if active:
        groups.append(tuple(active))

    if len(groups) != len(cuts):
        raise ValueError(
            "quiescent cuts do not correspond one-for-one with marker chains"
        )
    executed_by_id = {key.work_id: key for key in executed}
    if len(executed_by_id) != len(executed):
        raise ValueError("checkpoint cut validation received duplicate executed work")
    observed_request_ids: list[str] = []
    prior_last_sequence = 0
    for cut, group in zip(cuts, groups, strict=True):
        cut.validate_quiescent()
        first = group[0]
        last = group[-1]
        parent_id = first.causal_parent_ids[0]
        work = executed_by_id.get(parent_id)
        if (
            work is None
            or work.source_component_id != FULL_DAY_RUNTIME_COMPONENT
            or work.stage_ordinal is not WorkStageV1.CHECKPOINT_CAPTURE
            or work.simulation_time_us != first.simulation_time_us
            or work.microstep != first.microstep
        ):
            raise ValueError(
                "checkpoint marker chain does not begin at exact executed capture work"
            )
        request_ids: list[str] = []
        for offset, marker in enumerate(group):
            expected_parent = parent_id if offset == 0 else group[offset - 1].event_id
            request_id = _identifier(
                marker.payload.data["checkpoint_request_id"],
                "checkpoint marker request ID",
            )
            if (
                marker.source_component_id != FULL_DAY_RUNTIME_COMPONENT
                or marker.stage is not WorkStageV1.CHECKPOINT_CAPTURE
                or marker.simulation_time_us != work.simulation_time_us
                or marker.microstep != work.microstep
                or marker.causal_parent_ids != (expected_parent,)
                or marker.payload.native_event is not None
            ):
                raise ValueError("checkpoint marker causal chain is noncanonical")
            request_ids.append(request_id)
        if request_ids != sorted(set(request_ids)):
            raise ValueError(
                "checkpoint marker request IDs are not sorted and unique per cut"
            )
        observed_request_ids.extend(request_ids)
        prefix_length = last.global_event_sequence
        if (
            first.global_event_sequence <= prior_last_sequence
            or cut.schema_version != 1
            or cut.simulation_time_us != work.simulation_time_us
            or cut.microstep != work.microstep
            or cut.checkpoint_stage_ordinal != int(WorkStageV1.CHECKPOINT_CAPTURE)
            or cut.last_global_event_sequence != prefix_length
            or cut.event_prefix_last_global_sequence != prefix_length
            or prefix_length > len(events)
            or events[prefix_length - 1] is not last
            or canonical_event_prefix_sha256(events[:prefix_length])
            != cut.event_prefix_sha256
        ):
            raise ValueError(
                "quiescent cut differs from its exact checkpoint marker prefix"
            )
        prior_last_sequence = prefix_length
    if len(observed_request_ids) != len(set(observed_request_ids)):
        raise ValueError("completed checkpoint request ID is duplicated")

    if not cuts:
        if require_current_cut:
            raise ValueError("checkpoint snapshot omits its current quiescent cut")
        return
    if not require_current_cut:
        return
    ordered_pending = tuple(sorted(pending, key=lambda row: row.key.ordering_key))
    next_pending = None if not ordered_pending else ordered_pending[0]
    latest = cuts[-1]
    if (
        latest.simulation_time_us != current_time_us
        or latest.last_global_event_sequence != len(events)
        or latest.pending_work_count != len(ordered_pending)
        or latest.next_pending_time_us
        != (
            None
            if next_pending is None
            else next_pending.key.simulation_time_us
        )
        or latest.next_pending_microstep
        != (None if next_pending is None else next_pending.key.microstep)
        or (
            next_pending is not None
            and next_pending.key.simulation_time_us <= current_time_us
        )
    ):
        raise ValueError(
            "latest quiescent cut differs from the exact current queue frontier"
        )


def _state_emission_outer_data(
    emission: DayStateAnchorEmissionV1 | StateTransitionEmissionV1,
) -> dict[str, object]:
    if type(emission) is DayStateAnchorEmissionV1:
        return {
            "anchored_state": emission.anchored_state.value,
            "entered_time_us": emission.simulation_time_us,
            "macro_segment_index": emission.macro_segment_index,
            "macro_segment_sha256": emission.macro_segment_sha256,
            "previous_state": emission.previous_state.value,
            "sampled_duration_us": emission.sampled_duration_us,
        }
    return {
        "entered_time_us": emission.simulation_time_us,
        "new_state": emission.new_state,
        "previous_state": emission.previous_state,
        "sampled_duration_us": emission.sampled_duration_us,
        "transition_id": emission.transition_id,
        "trigger_id": emission.trigger_id,
        "trigger_version": emission.trigger_version,
    }


def _validate_state_runtime_replay(
    *,
    plan: FullDayPlanV1,
    state: HierarchicalStateRuntimeStateV1,
    events: Sequence[FullDayEventV1],
) -> None:
    """Recompute the observation-free state machine and its exact outer projection."""

    replay = HierarchicalStateRuntimeV1.create(plan)
    if state.input_closed_through_time_us is None:
        if state.current_time_us != 0:
            raise ValueError("unclosed hierarchical state exists after time zero")
        emissions: tuple[
            DayStateAnchorEmissionV1 | StateTransitionEmissionV1, ...
        ] = ()
    else:
        emissions = replay.advance_to(state.current_time_us)
    observed_state = state.as_dict()
    replayed_state = replay.state().as_dict()
    for allocator_field in (
        "component_local_sequence",
        "component_sequence_offset",
    ):
        del observed_state[allocator_field]
        del replayed_state[allocator_field]
    if observed_state != replayed_state:
        raise ValueError(
            "hierarchical state snapshot differs from deterministic plan replay"
        )

    state_event_types = {
        FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
        FullDayEventTypeV1.DAY_STATE_TRANSITION,
        FullDayEventTypeV1.LOCAL_STATE_TRANSITION,
    }
    observed_events = tuple(
        event for event in events if event.event_type in state_event_types
    )
    if len(observed_events) != len(emissions):
        raise ValueError(
            "hierarchical state emission count differs from its outer-event ledger"
        )
    for event, emission in zip(observed_events, emissions, strict=True):
        if (
            event.event_type is not emission.event_type
            or event.simulation_time_us != emission.simulation_time_us
            or event.microstep != emission.microstep
            or event.stage is not emission.stage
            or event.source_component_id != FULL_DAY_RUNTIME_COMPONENT
            or _plain(event.payload.data) != _state_emission_outer_data(emission)
        ):
            raise ValueError(
                "hierarchical state outer event differs from deterministic replay"
            )


def _validate_pending_state_work(
    *,
    plan: FullDayPlanV1,
    state: HierarchicalStateRuntimeStateV1,
    state_scheduled_time: int | None,
    pending: Sequence[RuntimeWorkItemV1],
) -> None:
    expected_scheduled_time = _next_state_time_from_state(plan, state)
    if state_scheduled_time != expected_scheduled_time:
        raise ValueError(
            "state scheduled-time marker differs from deterministic state frontier"
        )
    state_work = tuple(
        sorted(
            (item for item in pending if item.work_type == _WORK_STATE_EMISSION),
            key=lambda item: item.key.ordering_key,
        )
    )
    if (state_scheduled_time is None) == bool(state_work):
        raise ValueError("state scheduled-time marker differs from pending work")
    if state_scheduled_time is None:
        return
    if (
        state_scheduled_time < state.current_time_us
        or state_scheduled_time > plan.calendar.end_time_us
    ):
        raise ValueError("pending state batch lies outside the remaining horizon")
    preview = HierarchicalStateRuntimeV1.from_state(
        plan,
        state,
        verified_component_local_sequence_floor=state.component_local_sequence,
    )
    emissions = preview.advance_to(state_scheduled_time)
    if len(emissions) != len(state_work):
        raise ValueError("pending state work differs from exact preview inventory")
    for index, (item, emission) in enumerate(
        zip(state_work, emissions, strict=True)
    ):
        if (
            item.key.simulation_time_us != state_scheduled_time
            or item.key.simulation_time_us != emission.simulation_time_us
            or item.key.microstep != emission.microstep
            or item.key.stage_ordinal is not emission.stage
            or _plain(item.payload)
            != {
                "batch_time_us": state_scheduled_time,
                "emission_index": index,
            }
        ):
            raise ValueError(
                "pending state work differs from its exact previewed emission"
            )


def _next_state_time_from_state(
    plan: FullDayPlanV1,
    state: HierarchicalStateRuntimeStateV1,
) -> int | None:
    candidates: list[int] = []
    if state.next_macro_segment_index < len(plan.macro_regime_schedule):
        candidates.append(
            plan.macro_regime_schedule[state.next_macro_segment_index].start_us
        )
    for level, definitions in (
        (state.day, plan.state_model.day_definitions),
        (state.local, plan.state_model.local_definitions),
    ):
        definition = next(
            row for row in definitions if row.state.value == level.current_state
        )
        selected = next(
            transition
            for transition in definition.transitions
            if transition.transition_id == level.next_eligible_transition_id
        )
        eligible = (
            level.entered_time_us + selected.minimum_age_us
            if level.next_eligible_transition_time_us is None
            else level.next_eligible_transition_time_us
        )
        if (
            selected.duration_exhaustion_behavior
            is DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ):
            candidates.append(max(level.deadline_time_us, eligible))
        if any(
            memory.observation.triggered
            and memory.observation.transition_id == selected.transition_id
            for memory in level.trigger_memory
        ):
            candidates.append(max(state.current_time_us, eligible))
    remaining = tuple(
        value
        for value in candidates
        if state.current_time_us <= value <= plan.calendar.end_time_us
    )
    return min(remaining) if remaining else None


def _mechanics_session_state_from_native_ledger(
    native: Sequence[NativeLedgerEntryV1],
    *,
    plan: FullDayPlanV1,
    events: Sequence[FullDayEventV1],
) -> SessionState:
    """Derive the owner-adapter session state from its exact mechanics prefix."""

    session_state = SessionState.CLOSED
    outer_by_native_key = {
        event.payload.native_event.ledger_key: event
        for event in events
        if event.payload.native_event is not None
    }
    calendar_causes = {
        int(event.payload.data["boundary_operation_index"]): event
        for event in events
        if event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY
    }
    scheduled_causes = {
        str(event.payload.data["scheduled_event_id"]): event
        for event in events
        if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
    }
    scheduled_by_id = {event.event_id: event for event in plan.scheduled_events}
    mechanics_rows = tuple(
        sorted(
            (
                entry
                for entry in native
                if entry.reference.owner_component_id == MECHANICS_COMPONENT
            ),
            key=lambda entry: entry.reference.local_sequence,
        )
    )
    for sequence, entry in enumerate(mechanics_rows, start=1):
        payload = _plain(entry.payload)
        outer = outer_by_native_key.get(entry.ledger_key)
        if type(payload) is not dict or (
            entry.reference.native_ledger_id != MECHANICS_NATIVE_LEDGER_ID
            or entry.reference.local_sequence != sequence
            or entry.reference.event_id != f"MECHANICS_EVENT_{sequence:012d}"
            or payload.get("sequence") != sequence
            or payload.get("event_type") != entry.reference.event_type
            or set(payload) != {
                "data",
                "event_type",
                "sequence",
                "simulation_time_us",
            }
            or type(payload.get("simulation_time_us")) is not int
            or outer is None
            or outer.simulation_time_us != payload.get("simulation_time_us")
        ):
            raise ValueError("runtime-owner mechanics native prefix is noncanonical")
        if entry.reference.event_type != MechanicsEventType.SESSION_STATE_CHANGED.value:
            continue
        data = payload.get("data")
        if type(data) is not dict or set(data) != {
            "current_state",
            "previous_state",
            "reason",
        }:
            raise ValueError("mechanics session transition payload is not an object")
        try:
            previous = SessionState(str(data["previous_state"]))
            current = SessionState(str(data["current_state"]))
        except (KeyError, ValueError) as error:
            raise ValueError("mechanics session transition state is invalid") from error
        if previous is not session_state:
            raise ValueError("mechanics session transition prefix is discontinuous")
        reason = data["reason"]
        time_us = payload["simulation_time_us"]
        if type(reason) is not str or type(time_us) is not int:
            raise ValueError("mechanics session transition cause is invalid")
        if reason.startswith("FULL_DAY_CALENDAR_BOUNDARY_"):
            suffix = reason.removeprefix("FULL_DAY_CALENDAR_BOUNDARY_")
            if not suffix.isdigit():
                raise ValueError("calendar transition reason is noncanonical")
            index = int(suffix)
            if index >= len(plan.calendar.boundary_operations):
                raise ValueError("calendar transition reason exceeds the plan")
            operation = plan.calendar.boundary_operations[index]
            cause = calendar_causes.get(index)
            if (
                cause is None
                or cause.simulation_time_us != time_us
                or operation.boundary.simulation_time_us != time_us
                or current is not operation.destination_session_state
            ):
                raise ValueError("calendar mechanics transition lacks its plan cause")
        elif reason in {
            "FULL_DAY_HALT",
            "FULL_DAY_VOLATILITY_INTERRUPTION",
            "FULL_DAY_SCHEDULED_REOPENING",
        }:
            matching = tuple(
                scheduled
                for scheduled_id, cause in scheduled_causes.items()
                if (scheduled := scheduled_by_id[scheduled_id]).simulation_time_us
                == time_us
                and (
                    (
                        reason == "FULL_DAY_SCHEDULED_REOPENING"
                        and scheduled.event_type is ScheduledEventTypeV1.REOPENING
                        and current is SessionState.REOPENING_AUCTION
                    )
                    or (
                        reason == f"FULL_DAY_{scheduled.event_type.value}"
                        and scheduled.event_type
                        in {
                            ScheduledEventTypeV1.HALT,
                            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
                        }
                        and current is SessionState.HALTED
                    )
                )
            )
            if len(matching) != 1:
                raise ValueError("scheduled mechanics transition lacks its plan cause")
        elif reason == "FULL_DAY_REOPENING_COMPLETE":
            matching = tuple(
                scheduled
                for scheduled_id in scheduled_causes
                if (scheduled := scheduled_by_id[scheduled_id]).event_type
                is ScheduledEventTypeV1.REOPENING
                and scheduled.simulation_time_us
                + {
                    parameter.name: parameter.value
                    for parameter in scheduled.parameters
                }["reopening_auction_duration_us"]
                == time_us
            )
            if (
                len(matching) != 1
                or previous is not SessionState.REOPENING_AUCTION
                or current is not SessionState.CONTINUOUS
            ):
                raise ValueError("reopening completion lacks its scheduled cause")
        elif reason == "VOLATILITY_INTERRUPTION":
            preceding_protection = any(
                row.reference.event_type
                == MechanicsEventType.PROTECTION_TRIGGERED.value
                and row.reference.local_sequence < sequence
                and _plain(row.payload).get("simulation_time_us") == time_us
                and isinstance(_plain(row.payload).get("data"), dict)
                and _plain(row.payload)["data"].get("protection")
                == "VOLATILITY_INTERRUPTION"
                for row in mechanics_rows
            )
            if (
                not preceding_protection
                or previous is not SessionState.CONTINUOUS
                or current is not SessionState.HALTED
            ):
                raise ValueError("volatility transition lacks mechanics protection cause")
        else:
            raise ValueError("mechanics session transition reason is not runtime-owned")
        session_state = current
    return session_state


def _validate_halt_reopen_snapshot(
    *,
    plan: FullDayPlanV1,
    scheduled_event_index: int,
    pending: Sequence[RuntimeWorkItemV1],
    halt_state: Mapping[str, object],
    session_state: SessionState,
) -> None:
    _require_exact_fields(
        halt_state,
        {
            "halt_count",
            "halt_entered_time_us",
            "maximum_resume_deadline_us",
            "minimum_resume_eligible_time_us",
            "reopening_auction_end_time_us",
        },
        "halt/reopen state",
    )
    halt_count = _exact_int(halt_state["halt_count"], "halt_count")
    transient = (
        _exact_optional_int(
            halt_state["halt_entered_time_us"], "halt_entered_time_us"
        ),
        _exact_optional_int(
            halt_state["minimum_resume_eligible_time_us"],
            "minimum_resume_eligible_time_us",
        ),
        _exact_optional_int(
            halt_state["maximum_resume_deadline_us"],
            "maximum_resume_deadline_us",
        ),
        _exact_optional_int(
            halt_state["reopening_auction_end_time_us"],
            "reopening_auction_end_time_us",
        ),
    )
    processed = plan.scheduled_events[:scheduled_event_index]
    processed_halts = tuple(
        event
        for event in processed
        if event.event_type
        in {
            ScheduledEventTypeV1.HALT,
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
        }
    )
    if (
        halt_count != len(processed_halts)
        or halt_count > plan.halt_reopen_rules.maximum_halts
    ):
        raise ValueError("halt counter differs from the exact scheduled prefix")
    reopen_work = tuple(
        item for item in pending if item.work_type == _WORK_REOPEN_COMPLETE
    )
    if session_state in {SessionState.HALTED, SessionState.REOPENING_AUCTION}:
        if not processed_halts:
            raise ValueError("halted session has no scheduled halt cause")
        halt_event = processed_halts[-1]
        parameters = {
            parameter.name: parameter.value for parameter in halt_event.parameters
        }
        halt_duration = parameters["halt_duration_us"]
        expected_first_three = (
            halt_event.simulation_time_us,
            halt_event.simulation_time_us
            + plan.halt_reopen_rules.minimum_halt_duration_us,
            min(
                halt_event.simulation_time_us + halt_duration,
                halt_event.simulation_time_us
                + plan.halt_reopen_rules.maximum_halt_duration_us,
            ),
        )
        if transient[:3] != expected_first_three:
            raise ValueError("halt timing state differs from its scheduled cause")
        if session_state is SessionState.HALTED:
            if transient[3] is not None or reopen_work:
                raise ValueError("HALTED state carries reopening completion work")
            return
        processed_reopens = tuple(
            event
            for event in processed
            if event.event_type is ScheduledEventTypeV1.REOPENING
            and event.simulation_time_us >= halt_event.simulation_time_us
        )
        if not processed_reopens:
            raise ValueError("reopening session has no scheduled reopen cause")
        reopen_event = processed_reopens[-1]
        reopen_parameters = {
            parameter.name: parameter.value
            for parameter in reopen_event.parameters
        }
        expected_end = reopen_event.simulation_time_us + reopen_parameters[
            "reopening_auction_duration_us"
        ]
        if (
            transient[3] != expected_end
            or len(reopen_work) != 1
            or reopen_work[0].key.simulation_time_us != expected_end
            or reopen_work[0].payload["scheduled_event_id"]
            != reopen_event.event_id
        ):
            raise ValueError("reopening state differs from exact completion work")
    elif any(value is not None for value in transient) or reopen_work:
        raise ValueError("non-halted session retains transient halt/reopen state")


class FullDayRuntime:
    """Sole E1 session-calendar/exchange/scheduler owner."""

    COMPONENT_ID = FULL_DAY_RUNTIME_COMPONENT
    PROFILE_ID = FULL_DAY_RUNTIME_PROFILE_ID
    PROFILE_VERSION = FULL_DAY_RUNTIME_PROFILE_VERSION

    def __init__(
        self,
        plan: FullDayPlanV1,
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock,
        agent_scheduler: object | None,
        order_id_allocator: RuntimeOrderIdAllocatorV1,
        bootstrap: bool,
        restoring: bool = False,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("FullDayRuntime requires FullDayPlanV1")
        self.plan = plan
        self.engine = engine
        self.clock = clock
        self.agent_scheduler = agent_scheduler
        self._order_id_allocator = order_id_allocator
        self._validate_profile_and_core_owners(restoring=restoring)

        self._state_runtime = HierarchicalStateRuntimeV1.create(plan)
        self._heap: list[tuple[tuple[int, int, int, str, int], str]] = []
        self._pending: dict[str, RuntimeWorkItemV1] = {}
        # Retain the complete immutable work record after execution.  A bare
        # ScheduledWorkKeyV1 is insufficient replay evidence because it drops
        # the work type and payload that bind the key to its exact owner/stage
        # contract.
        self._executed_work: dict[str, RuntimeWorkItemV1] = {}
        self._retired_work: dict[str, RuntimeWorkItemV1] = {}
        self._dequeued_count = 0
        self._events: list[FullDayEventV1] = []
        self._native_ledger: dict[tuple[str, str, str], NativeLedgerEntryV1] = {}
        self._next_global_event_sequence = 1
        self._component_sequences: dict[str, int] = {
            FULL_DAY_RUNTIME_COMPONENT: 0,
            MECHANICS_COMPONENT: 0,
        }
        if self.agent_scheduler is not None:
            self._component_sequences[AGENT_SCHEDULER_COMPONENT] = 0
        self._native_sequences: dict[str, int] = (
            {AGENT_SCHEDULER_COMPONENT: 0}
            if self.agent_scheduler is not None
            else {}
        )
        self._mechanics_event_cursor = len(self.engine.events)
        self._calendar_boundary_index = 0
        self._participant_schedule_index = 0
        self._scheduled_event_index = 0
        self._checkpoint_request_next_sequence = 1
        self._allocated_checkpoint_request_ids: list[str] = []
        self._checkpoint_completed_count = 0
        self._quiescent_cuts: list[QuiescentCutV1] = []
        self._executing: RuntimeWorkItemV1 | None = None
        self._last_completed_key: ScheduledWorkKeyV1 | None = None
        self._events_at_time: dict[int, int] = {}
        self._microsteps_at_time: dict[int, set[int]] = {}
        self._agent_tokens: set[tuple[str, int]] = set()
        self._state_scheduled_time: int | None = None
        self._state_emission_buffer: tuple[
            DayStateAnchorEmissionV1 | StateTransitionEmissionV1, ...
        ] = ()
        self._state_emission_consumed: set[int] = set()
        self._executing_event_count = 0
        self._executing_zero_delay_children = 0
        self._halt_count = 0
        self._halt_entered_time_us: int | None = None
        self._minimum_resume_eligible_time_us: int | None = None
        self._maximum_resume_deadline_us: int | None = None
        self._reopening_auction_end_time_us: int | None = None
        self._handler_names = MappingProxyType(
            {
                _WORK_AGENT_ARRIVAL: "_handle_agent_work",
                _WORK_AGENT_DECISION: "_handle_agent_work",
                _WORK_CALENDAR_BOUNDARY: "_handle_calendar_boundary",
                _WORK_CHECKPOINT_CAPTURE: "_handle_checkpoint_capture",
                _WORK_GTT_EXPIRY: "_handle_gtt_expiry",
                _WORK_MECHANICS_CANCEL: "_handle_mechanics_cancel",
                _WORK_MECHANICS_REPLACE: "_handle_mechanics_replace",
                _WORK_MECHANICS_SUBMIT: "_handle_mechanics_submit",
                _WORK_PARTICIPANT_SCHEDULE: "_handle_participant_schedule",
                _WORK_REOPEN_COMPLETE: "_handle_reopen_complete",
                _WORK_SCHEDULED_INFORMATION: "_handle_scheduled_information",
                _WORK_STATE_EMISSION: "_handle_state_emission",
            }
        )
        if bootstrap:
            if self.engine.events:
                raise ValueError(
                    "a new full-day runtime cannot inherit an unowned mechanics prefix"
                )
            self._mechanics_event_cursor = 0
            self._bootstrap_plan_work()
            self._schedule_agent_work()
            self._schedule_next_state_batch()
            self.assert_invariants()

    @classmethod
    def create(
        cls,
        plan: FullDayPlanV1,
        *,
        engine: MarketMechanicsEngine | None = None,
        clock: SimulationClock | None = None,
        agent_scheduler: object | None = None,
        order_id_allocator: RuntimeOrderIdAllocatorV1 | None = None,
    ) -> FullDayRuntime:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("FullDayRuntime.create requires FullDayPlanV1")
        selected_engine = (
            MarketMechanicsEngine(
                plan.instrument_profile.mechanics_rules.to_instrument_rules()
            )
            if engine is None
            else engine
        )
        if type(selected_engine) is not MarketMechanicsEngine:
            raise TypeError("full-day engine must be MarketMechanicsEngine")
        selected_clock = selected_engine.clock if clock is None else clock
        allocator = order_id_allocator or RuntimeOrderIdAllocatorV1()
        return cls(
            plan,
            engine=selected_engine,
            clock=selected_clock,
            agent_scheduler=agent_scheduler,
            order_id_allocator=allocator,
            bootstrap=True,
        )

    @classmethod
    def create_with_agent_scheduler(
        cls,
        plan: FullDayPlanV1,
        definition: object,
        *,
        seed: int | None = None,
        engine: MarketMechanicsEngine | None = None,
    ) -> FullDayRuntime:
        """Construct the scheduler only after the sole owners exist."""

        from kirby2.agents.ecology import AgentScheduler
        from kirby2.agents.models import PopulationDefinition

        if type(definition) is not PopulationDefinition:
            raise TypeError("full-day scheduler definition must be PopulationDefinition")
        plan_ids = tuple(
            participant.participant_id
            for participant in plan.participant_definitions
        )
        definition_ids = tuple(sorted(spec.agent_id for spec in definition.agents))
        if definition_ids != plan_ids:
            raise ValueError(
                "scheduler population IDs differ from the exact plan participant set"
            )
        if definition.duration_us != plan.calendar.end_time_us:
            raise ValueError("scheduler population duration differs from the full-day calendar")
        root_seed = plan.seed_policy.root_seed
        if seed is not None and (type(seed) is not int or seed != root_seed):
            raise ValueError("scheduler seed must be the plan root seed")
        active_agent_ids = tuple(
            participant.participant_id
            for participant in plan.participant_definitions
            if participant.initially_active
        )
        agent_seeds = {
            participant.participant_id: plan.seed_policy.derive(
                participant.rng_substream_label
            )
            for participant in plan.participant_definitions
        }
        rng_labels = {
            participant.participant_id: participant.rng_substream_label
            for participant in plan.participant_definitions
        }

        selected_engine = engine or MarketMechanicsEngine(
            plan.instrument_profile.mechanics_rules.to_instrument_rules()
        )
        allocator = RuntimeOrderIdAllocatorV1()
        scheduler = AgentScheduler(
            definition,
            root_seed,
            engine=selected_engine,
            clock=selected_engine.clock,
            active_agent_ids=active_agent_ids,
            agent_seeds=agent_seeds,
            rng_labels=rng_labels,
            order_id_allocator=allocator.allocate,
        )
        return cls.create(
            plan,
            engine=selected_engine,
            clock=selected_engine.clock,
            agent_scheduler=scheduler,
            order_id_allocator=allocator,
        )

    @property
    def events(self) -> tuple[FullDayEventV1, ...]:
        return tuple(self._events)

    @property
    def pending_work(self) -> tuple[RuntimeWorkItemV1, ...]:
        return tuple(
            self._pending[work_id]
            for _ordering, work_id in sorted(self._heap)
        )

    @property
    def executed_work_items(self) -> Mapping[str, ScheduledWorkKeyV1]:
        return MappingProxyType(
            {work_id: item.key for work_id, item in self._executed_work.items()}
        )

    @property
    def native_event_ledger(
        self,
    ) -> Mapping[tuple[str, str, str], NativeLedgerEntryV1]:
        return MappingProxyType(dict(self._native_ledger))

    @property
    def quiescent_cuts(self) -> tuple[QuiescentCutV1, ...]:
        return tuple(self._quiescent_cuts)

    @property
    def latest_quiescent_cut(self) -> QuiescentCutV1 | None:
        return None if not self._quiescent_cuts else self._quiescent_cuts[-1]

    def _validate_profile_and_core_owners(self, *, restoring: bool) -> None:
        matrix = executable_agent_mechanics_composition_matrix()
        if (
            self.plan.composition_profile.reference_id != FULL_DAY_RUNTIME_PROFILE_ID
            or self.plan.composition_profile.version != FULL_DAY_RUNTIME_PROFILE_VERSION
            or self.plan.composition_profile.sha256 != matrix.sha256
        ):
            raise ValueError(
                "FullDayRuntime requires the exact executable "
                "SINGLE_VENUE_AGENT_MECHANICS_V1 revision"
            )
        if type(self.engine) is not MarketMechanicsEngine:
            raise TypeError("runtime engine must be exactly MarketMechanicsEngine")
        if type(self.clock) is not SimulationClock or self.clock is not self.engine.clock:
            raise ValueError("runtime and mechanics engine must share one clock by identity")
        if type(self.engine.book) is not OrderBook or type(self.engine.auction) is not AuctionBook:
            raise ValueError("runtime engine must own the canonical book and auction")
        if self.engine.rules.session_schedule.transitions:
            raise ValueError("full-day engine must not own a native session calendar")
        expected_rules = self.plan.instrument_profile.mechanics_rules.to_instrument_rules()
        if self.engine.rules.as_dict() != expected_rules.as_dict():
            raise ValueError("full-day engine rules differ from the semantic plan")
        scheduler_required = agent_scheduler_is_active(
            participant_schedule_nonempty=bool(self.plan.participant_schedule),
            any_participant_initially_active=any(
                participant.initially_active
                for participant in self.plan.participant_definitions
            ),
        )
        if scheduler_required != (self.agent_scheduler is not None):
            raise ValueError("agent scheduler presence differs from its active predicate")
        if self.agent_scheduler is not None:
            from kirby2.agents.ecology import AgentScheduler

            if type(self.agent_scheduler) is not AgentScheduler:
                raise ValueError(
                    "FullDayRuntime requires the exact injected AgentScheduler owner"
                )
            if getattr(self.agent_scheduler, "_compatibility_mode", None) is not False:
                raise ValueError("compatibility-mode scheduler cannot enter FullDayRuntime")
            if getattr(self.agent_scheduler, "_allocator_owner", None) != "INJECTED_RUNTIME":
                raise ValueError("scheduler does not declare the injected runtime allocator")
            if getattr(self.agent_scheduler, "COMPONENT_ID", None) != AGENT_SCHEDULER_COMPONENT:
                raise ValueError("injected scheduler has the wrong component owner ID")
            if getattr(self.agent_scheduler, "engine", None) is not self.engine:
                raise ValueError("injected scheduler must borrow the authoritative engine")
            if getattr(self.agent_scheduler, "clock", None) is not self.clock:
                raise ValueError("injected scheduler must borrow the authoritative clock")
            allocator_callback = getattr(
                self.agent_scheduler, "_order_id_allocator", None
            )
            if (
                getattr(allocator_callback, "__self__", None)
                is not self._order_id_allocator
                or getattr(allocator_callback, "__func__", None)
                is not RuntimeOrderIdAllocatorV1.allocate
            ):
                raise ValueError(
                    "scheduler order allocator is not bound to this exact runtime owner"
                )
            definition = getattr(self.agent_scheduler, "definition", None)
            definition_agents = getattr(definition, "agents", ())
            if getattr(definition, "duration_us", None) != self.plan.calendar.end_time_us:
                raise ValueError("scheduler duration differs from the plan calendar")
            scheduler_ids = tuple(
                sorted(getattr(spec, "agent_id", "") for spec in definition_agents)
            )
            plan_ids = tuple(
                participant.participant_id
                for participant in self.plan.participant_definitions
            )
            if scheduler_ids != plan_ids:
                raise ValueError(
                    "scheduler participant IDs differ from the exact plan inventory"
                )
            scheduler_specs = {
                getattr(spec, "agent_id", ""): spec for spec in definition_agents
            }
            for participant in self.plan.participant_definitions:
                spec = scheduler_specs[participant.participant_id]
                identity = getattr(spec, "identity_dict", None)
                if (
                    not callable(identity)
                    or canonical_sha256(identity())
                    != participant.specification.sha256
                ):
                    raise ValueError(
                        "scheduler participant specification differs from the plan reference"
                    )
            for entry in self.plan.participant_schedule:
                if entry.action is not ParticipantScheduleActionV1.RETUNE:
                    continue
                if entry.replacement_specification is None:
                    raise ValueError("retune schedule omits a replacement specification")
                resolved = tuple(
                    spec
                    for spec in definition_agents
                    if getattr(spec, "agent_id", None) == entry.participant_id
                    and callable(getattr(spec, "identity_dict", None))
                    and canonical_sha256(spec.identity_dict())
                    == entry.replacement_specification.sha256
                )
                if len(resolved) != 1:
                    raise ValueError(
                        "retune specification is not uniquely resolved by the "
                        "injected population definition"
                    )
            if getattr(self.agent_scheduler, "seed", None) != self.plan.seed_policy.root_seed:
                raise ValueError("scheduler seed differs from the plan root seed")
            expected_rng_labels = {
                participant.participant_id: participant.rng_substream_label
                for participant in self.plan.participant_definitions
            }
            expected_agent_seeds = {
                participant.participant_id: self.plan.seed_policy.derive(
                    participant.rng_substream_label
                )
                for participant in self.plan.participant_definitions
            }
            if getattr(self.agent_scheduler, "_rng_labels", None) != expected_rng_labels:
                raise ValueError("scheduler RNG labels differ from the plan registry")
            if getattr(self.agent_scheduler, "_agent_seeds", None) != expected_agent_seeds:
                raise ValueError("scheduler derived seeds differ from the plan registry")
            if not restoring:
                expected_active = {
                    participant.participant_id: participant.initially_active
                    for participant in self.plan.participant_definitions
                }
                if getattr(self.agent_scheduler, "_active", None) != expected_active:
                    raise ValueError(
                        "scheduler activation differs from the initial plan inventory"
                    )
            self._reject_smuggled_core_owner(self.agent_scheduler)
        self.engine.assert_invariants()

    def _reject_smuggled_core_owner(self, owner: object) -> None:
        values: list[object] = []
        namespace = getattr(owner, "__dict__", None)
        if isinstance(namespace, Mapping):
            values.extend(namespace.values())
        for slot in getattr(type(owner), "__slots__", ()):
            if type(slot) is str and hasattr(owner, slot):
                values.append(getattr(owner, slot))
        seen: set[int] = set()

        def inspect(value: object, depth: int) -> None:
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            if len(seen) > 10_000:
                raise ValueError("scheduler owner graph exceeds the bounded ownership scan")
            if isinstance(value, MarketMechanicsEngine) and value is not self.engine:
                raise ValueError("scheduler smuggles a second mechanics engine")
            if isinstance(value, MarketMechanicsEngine):
                return
            if isinstance(value, SimulationClock) and value is not self.clock:
                raise ValueError("scheduler smuggles a second simulation clock")
            if isinstance(value, SimulationClock):
                return
            if isinstance(value, OrderBook) and value is not self.engine.book:
                raise ValueError("scheduler smuggles a second order book")
            if isinstance(value, OrderBook):
                return
            if isinstance(value, AuctionBook) and value is not self.engine.auction:
                raise ValueError("scheduler smuggles a second auction book")
            if isinstance(value, AuctionBook):
                return
            if isinstance(value, TradingDayCalendarV1):
                raise ValueError("scheduler smuggles a second session calendar")
            if (
                isinstance(value, RuntimeOrderIdAllocatorV1)
                and value is not self._order_id_allocator
            ):
                raise ValueError("scheduler smuggles a second order allocator")
            if isinstance(value, RuntimeOrderIdAllocatorV1):
                return
            if depth >= 16:
                raise ValueError(
                    "scheduler owner graph exceeds the bounded ownership depth"
                )
            if value is None or type(value) in {
                bool,
                bytes,
                float,
                int,
                str,
            }:
                return
            if callable(value):
                if isinstance(value, functools.partial):
                    inspect(value.func, depth + 1)
                    inspect(value.args, depth + 1)
                    if value.keywords is not None:
                        inspect(value.keywords, depth + 1)
                bound_owner = getattr(value, "__self__", None)
                if bound_owner is not None:
                    inspect(bound_owner, depth + 1)
                defaults = getattr(value, "__defaults__", None)
                if defaults is not None:
                    inspect(defaults, depth + 1)
                keyword_defaults = getattr(value, "__kwdefaults__", None)
                if keyword_defaults is not None:
                    inspect(keyword_defaults, depth + 1)
                closure = getattr(value, "__closure__", None)
                if closure is not None:
                    for cell in closure:
                        try:
                            captured = cell.cell_contents
                        except ValueError:
                            continue
                        inspect(captured, depth + 1)
            if isinstance(value, Mapping):
                for key, member in value.items():
                    inspect(key, depth + 1)
                    inspect(member, depth + 1)
            elif isinstance(value, (tuple, list, set, frozenset)):
                for member in value:
                    inspect(member, depth + 1)
            else:
                nested_namespace = getattr(value, "__dict__", None)
                if isinstance(nested_namespace, Mapping):
                    for member in nested_namespace.values():
                        inspect(member, depth + 1)
                for nested_slot in getattr(type(value), "__slots__", ()):
                    if type(nested_slot) is str and hasattr(value, nested_slot):
                        inspect(getattr(value, nested_slot), depth + 1)

        for value in values:
            inspect(value, 0)

    def _bootstrap_plan_work(self) -> None:
        for index, operation in enumerate(self.plan.calendar.boundary_operations):
            self._enqueue_new(
                simulation_time_us=operation.boundary.simulation_time_us,
                microstep=0,
                stage=WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                work_type=_WORK_CALENDAR_BOUNDARY,
                payload={"boundary_operation_index": index},
            )
        for scheduled_event in self.plan.scheduled_events:
            self._enqueue_new(
                simulation_time_us=scheduled_event.simulation_time_us,
                microstep=0,
                stage=WorkStageV1.SCHEDULED_INFORMATION,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                work_type=_WORK_SCHEDULED_INFORMATION,
                payload={"scheduled_event_id": scheduled_event.event_id},
            )
        for entry in self.plan.participant_schedule:
            self._enqueue_new(
                simulation_time_us=entry.simulation_time_us,
                microstep=0,
                stage=WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
                source_component_id=AGENT_SCHEDULER_COMPONENT,
                work_type=_WORK_PARTICIPANT_SCHEDULE,
                payload={"schedule_id": entry.schedule_id},
            )
        for time_us in self.plan.resolved_checkpoint_times_us:
            request_id = self._allocate_checkpoint_request_id()
            self._enqueue_checkpoint(time_us, request_id)

    def _reserve_component_sequence(self, component_id: str) -> int:
        if component_id == FULL_DAY_RUNTIME_COMPONENT:
            sequence = self._state_runtime.reserve_component_local_sequence()
            self._component_sequences[component_id] = sequence
            return sequence
        if component_id not in self._component_sequences:
            raise ValueError("work/event source has no active component owner")
        sequence = self._component_sequences[component_id] + 1
        self._component_sequences[component_id] = sequence
        return sequence

    def _enqueue_new(
        self,
        *,
        simulation_time_us: int,
        microstep: int,
        stage: WorkStageV1,
        source_component_id: str,
        work_type: str,
        payload: Mapping[str, object],
    ) -> RuntimeWorkItemV1:
        zero_delay_child = bool(
            self._executing is not None
            and simulation_time_us == self._executing.key.simulation_time_us
        )
        if zero_delay_child and (
            self._executing_zero_delay_children
            >= self.plan.deterministic_limits.maximum_zero_delay_children_per_work_item
        ):
            raise RuntimeError(
                "maximum zero-delay children per work item exceeded"
            )
        if source_component_id == FULL_DAY_RUNTIME_COMPONENT:
            sequence = self._state_runtime.component_local_sequence + 1
        else:
            try:
                sequence = self._component_sequences[source_component_id] + 1
            except KeyError as error:
                raise ValueError(
                    "work source has no active component owner"
                ) from error
        preview_key = ScheduledWorkKeyV1(
            simulation_time_us=simulation_time_us,
            microstep=microstep,
            stage_ordinal=stage,
            source_component_id=source_component_id,
            component_local_sequence=sequence,
        )
        item = RuntimeWorkItemV1(
            preview_key,
            work_type,
            payload,
            preview_key.work_id,
        )
        self._preflight_enqueue_item(item)
        allocated = self._reserve_component_sequence(source_component_id)
        if allocated != sequence:  # pragma: no cover - preflight is side-effect free
            raise RuntimeError("component allocator changed during enqueue preflight")
        self._enqueue_item(item)
        if zero_delay_child:
            self._executing_zero_delay_children += 1
        return item

    def _preflight_enqueue_item(self, item: RuntimeWorkItemV1) -> None:
        """Prove an enqueue can succeed before reserving any owned identity."""

        key = item.key
        if key.simulation_time_us < self.clock.current_time_us:
            raise ValueError("runtime work cannot be scheduled in the past")
        if key.simulation_time_us > self.plan.calendar.end_time_us:
            raise ValueError("runtime work cannot exceed the plan calendar")
        if (
            self._executing is not None
            and key.simulation_time_us == self._executing.key.simulation_time_us
            and key.microstep <= self._executing.key.microstep
        ):
            raise ValueError(
                "zero-delay child work requires a strictly later microstep"
            )
        if (
            item.work_id in self._pending
            or item.work_id in self._executed_work
            or item.work_id in self._retired_work
        ):
            raise ValueError("runtime work identity is duplicated")
        if (
            len(self._pending)
            >= self.plan.deterministic_limits.maximum_pending_work_items
        ):
            raise RuntimeError("maximum pending-work limit exceeded")

    def _enqueue_item(self, item: RuntimeWorkItemV1) -> None:
        self._preflight_enqueue_item(item)
        key = item.key
        self._pending[item.work_id] = item
        heapq.heappush(self._heap, (key.ordering_key, item.work_id))

    def _allocate_checkpoint_request_id(self) -> str:
        while True:
            sequence = self._checkpoint_request_next_sequence
            self._checkpoint_request_next_sequence += 1
            request_id = f"CHECKPOINT-{sequence:06d}"
            if request_id not in self._allocated_checkpoint_request_ids:
                break
        self._allocated_checkpoint_request_ids.append(request_id)
        return request_id

    def _enqueue_checkpoint(self, time_us: int, request_id: str) -> RuntimeWorkItemV1:
        _identifier(request_id, "checkpoint request ID")
        for item in self._pending.values():
            if (
                item.work_type == _WORK_CHECKPOINT_CAPTURE
                and item.key.simulation_time_us == time_us
            ):
                request_ids = list(item.payload["checkpoint_request_ids"])
                if request_id in request_ids:
                    raise ValueError("checkpoint request ID is duplicated")
                request_ids.append(request_id)
                request_ids.sort()
                replacement = RuntimeWorkItemV1(
                    item.key,
                    item.work_type,
                    {"checkpoint_request_ids": request_ids},
                    item.causal_parent_id,
                )
                self._pending[item.work_id] = replacement
                return replacement
        microstep = 0
        if time_us == self.clock.current_time_us and self._last_completed_key is not None and self._last_completed_key.simulation_time_us == time_us:
            microstep = self._last_completed_key.microstep + 1
        return self._enqueue_new(
            simulation_time_us=time_us,
            microstep=microstep,
            stage=WorkStageV1.CHECKPOINT_CAPTURE,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            work_type=_WORK_CHECKPOINT_CAPTURE,
            payload={"checkpoint_request_ids": [request_id]},
        )

    def _next_state_time(self) -> int | None:
        return _next_state_time_from_state(
            self.plan,
            self._state_runtime.state(),
        )

    def _schedule_next_state_batch(self) -> None:
        if self._state_scheduled_time is not None:
            return
        time_us = self._next_state_time()
        if time_us is None:
            return
        snapshot = self._state_runtime.state()
        preview = HierarchicalStateRuntimeV1.from_state(
            self.plan,
            snapshot,
            verified_component_local_sequence_floor=snapshot.component_local_sequence,
        )
        emissions = preview.advance_to(time_us)
        if not emissions:
            raise RuntimeError("state scheduler selected a time with no due emission")
        self._state_scheduled_time = time_us
        for index, emission in enumerate(emissions):
            self._enqueue_new(
                simulation_time_us=emission.simulation_time_us,
                microstep=emission.microstep,
                stage=emission.stage,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                work_type=_WORK_STATE_EMISSION,
                payload={"batch_time_us": time_us, "emission_index": index},
            )

    def _schedule_agent_work(self) -> None:
        if self.agent_scheduler is None:
            return
        candidates = (
            (
                _WORK_AGENT_ARRIVAL,
                getattr(self.agent_scheduler, "next_pending_arrival_time_us", None),
                WorkStageV1.PENDING_VENUE_ARRIVAL,
            ),
            (
                _WORK_AGENT_DECISION,
                getattr(self.agent_scheduler, "next_decision_time_us", None),
                WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            ),
        )
        desired = {
            (work_type, time_us): stage
            for work_type, time_us, stage in candidates
            if time_us is not None and time_us <= self.plan.calendar.end_time_us
        }
        stale_ids = {
            item.work_id
            for item in self._pending.values()
            if item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
            and (item.work_type, item.key.simulation_time_us) not in desired
        }
        if stale_ids:
            for work_id in stale_ids:
                retired = self._pending.pop(work_id)
                if work_id in self._retired_work:
                    raise RuntimeError("retired runtime work identity is duplicated")
                self._retired_work[work_id] = retired
            self._heap = [
                (ordering, work_id)
                for ordering, work_id in self._heap
                if work_id not in stale_ids
            ]
            heapq.heapify(self._heap)
        existing = {
            (item.work_type, item.key.simulation_time_us)
            for item in self._pending.values()
            if item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
        }
        self._agent_tokens = set(existing)
        for work_type, time_us, stage in candidates:
            if time_us is None:
                continue
            _exact_int(time_us, f"{work_type} next time")
            if time_us > self.plan.calendar.end_time_us:
                continue
            token = (work_type, time_us)
            if token in self._agent_tokens:
                continue
            microstep = 0
            if self._executing is not None and time_us == self._executing.key.simulation_time_us:
                microstep = self._executing.key.microstep + 1
            self._enqueue_new(
                simulation_time_us=time_us,
                microstep=microstep,
                stage=stage,
                source_component_id=AGENT_SCHEDULER_COMPONENT,
                work_type=work_type,
                payload={},
            )
            self._agent_tokens.add(token)

    def _capture_transaction_state(self) -> dict[str, object]:
        """Capture every mutable owner field for failure-atomic public advance."""

        owner_bundle = {
            "agent_scheduler": (
                None
                if self.agent_scheduler is None
                else self.agent_scheduler.checkpoint_state()
            ),
            "engine": self.engine.checkpoint_state(),
            "order_id_allocator": self._order_id_allocator.checkpoint_state(),
            "state_runtime": self._state_runtime.state().as_dict(),
        }
        return {
            "agent_tokens": set(self._agent_tokens),
            "allocated_checkpoint_request_ids": list(
                self._allocated_checkpoint_request_ids
            ),
            "calendar_boundary_index": self._calendar_boundary_index,
            "checkpoint_completed_count": self._checkpoint_completed_count,
            "checkpoint_request_next_sequence": (
                self._checkpoint_request_next_sequence
            ),
            "component_sequences": dict(self._component_sequences),
            "dequeued_count": self._dequeued_count,
            "events": list(self._events),
            "events_at_time": dict(self._events_at_time),
            "executed_work": dict(self._executed_work),
            "executing": self._executing,
            "executing_event_count": self._executing_event_count,
            "executing_zero_delay_children": (
                self._executing_zero_delay_children
            ),
            "halt_count": self._halt_count,
            "halt_entered_time_us": self._halt_entered_time_us,
            "heap": list(self._heap),
            "last_completed_key": self._last_completed_key,
            "maximum_resume_deadline_us": self._maximum_resume_deadline_us,
            "mechanics_event_cursor": self._mechanics_event_cursor,
            "microsteps_at_time": {
                time_us: set(values)
                for time_us, values in self._microsteps_at_time.items()
            },
            "minimum_resume_eligible_time_us": (
                self._minimum_resume_eligible_time_us
            ),
            "native_ledger": dict(self._native_ledger),
            "native_sequences": dict(self._native_sequences),
            "next_global_event_sequence": self._next_global_event_sequence,
            "owner_bundle": owner_bundle,
            "owner_identities": (
                self.engine,
                self.clock,
                self.engine.book,
                self.engine.auction,
                self.agent_scheduler,
                self._state_runtime,
                self._order_id_allocator,
            ),
            "participant_schedule_index": self._participant_schedule_index,
            "pending": dict(self._pending),
            "quiescent_cuts": list(self._quiescent_cuts),
            "reopening_auction_end_time_us": (
                self._reopening_auction_end_time_us
            ),
            "retired_work": dict(self._retired_work),
            "scheduled_event_index": self._scheduled_event_index,
            "state_emission_buffer": self._state_emission_buffer,
            "state_emission_consumed": set(self._state_emission_consumed),
            "state_scheduled_time": self._state_scheduled_time,
        }

    def _restore_transaction_state(self, snapshot: Mapping[str, object]) -> None:
        """Restore a snapshot captured by :meth:`_capture_transaction_state`."""

        owner_bundle = snapshot["owner_bundle"]
        restored_engine = MarketMechanicsEngine.from_checkpoint_state(
            owner_bundle["engine"]
        )
        restored_order_allocator = RuntimeOrderIdAllocatorV1.from_checkpoint_state(
            owner_bundle["order_id_allocator"]
        )
        (
            engine,
            clock,
            book,
            auction,
            scheduler,
            state_runtime,
            order_allocator,
        ) = snapshot["owner_identities"]
        book.__dict__.clear()
        book.__dict__.update(restored_engine.book.__dict__)
        auction.__dict__.clear()
        auction.__dict__.update(restored_engine.auction.__dict__)
        clock.current_time_us = restored_engine.clock.current_time_us
        engine.__dict__.clear()
        engine.__dict__.update(restored_engine.__dict__)
        engine.book = book
        engine.auction = auction
        engine.clock = clock
        order_allocator.next_sequence = restored_order_allocator.next_sequence
        raw_scheduler = owner_bundle["agent_scheduler"]
        if raw_scheduler is None:
            if scheduler is not None:
                raise RuntimeError(
                    "transaction snapshot scheduler identity is inconsistent"
                )
        else:
            from kirby2.agents.ecology import AgentScheduler

            restored_scheduler = AgentScheduler.from_checkpoint_state(
                raw_scheduler,
                engine=engine,
                clock=clock,
                order_id_allocator=order_allocator.allocate,
            )
            if scheduler is None:
                raise RuntimeError(
                    "transaction snapshot scheduler identity is inconsistent"
                )
            scheduler.__dict__.clear()
            scheduler.__dict__.update(restored_scheduler.__dict__)
            scheduler.engine = engine
            scheduler.clock = clock
            scheduler._order_id_allocator = order_allocator.allocate
        state_runtime_state = HierarchicalStateRuntimeStateV1.from_dict(
            owner_bundle["state_runtime"]
        )
        restored_state_runtime = HierarchicalStateRuntimeV1.from_state(
            self.plan,
            state_runtime_state,
            verified_component_local_sequence_floor=(
                state_runtime_state.component_local_sequence
            ),
        )
        state_runtime.__dict__.clear()
        state_runtime.__dict__.update(restored_state_runtime.__dict__)
        self.engine = engine
        self.clock = clock
        self.agent_scheduler = scheduler
        self._state_runtime = state_runtime
        self._order_id_allocator = order_allocator
        self._heap = snapshot["heap"]
        self._pending = snapshot["pending"]
        self._executed_work = snapshot["executed_work"]
        self._retired_work = snapshot["retired_work"]
        self._dequeued_count = snapshot["dequeued_count"]
        self._events = snapshot["events"]
        self._native_ledger = snapshot["native_ledger"]
        self._next_global_event_sequence = snapshot[
            "next_global_event_sequence"
        ]
        self._component_sequences = snapshot["component_sequences"]
        self._native_sequences = snapshot["native_sequences"]
        self._mechanics_event_cursor = snapshot["mechanics_event_cursor"]
        self._calendar_boundary_index = snapshot["calendar_boundary_index"]
        self._participant_schedule_index = snapshot[
            "participant_schedule_index"
        ]
        self._scheduled_event_index = snapshot["scheduled_event_index"]
        self._checkpoint_request_next_sequence = snapshot[
            "checkpoint_request_next_sequence"
        ]
        self._allocated_checkpoint_request_ids = snapshot[
            "allocated_checkpoint_request_ids"
        ]
        self._checkpoint_completed_count = snapshot[
            "checkpoint_completed_count"
        ]
        self._quiescent_cuts = snapshot["quiescent_cuts"]
        self._executing = snapshot["executing"]
        self._last_completed_key = snapshot["last_completed_key"]
        self._events_at_time = snapshot["events_at_time"]
        self._microsteps_at_time = snapshot["microsteps_at_time"]
        self._agent_tokens = snapshot["agent_tokens"]
        self._state_scheduled_time = snapshot["state_scheduled_time"]
        self._state_emission_buffer = snapshot["state_emission_buffer"]
        self._state_emission_consumed = snapshot[
            "state_emission_consumed"
        ]
        self._executing_event_count = snapshot["executing_event_count"]
        self._executing_zero_delay_children = snapshot[
            "executing_zero_delay_children"
        ]
        self._halt_count = snapshot["halt_count"]
        self._halt_entered_time_us = snapshot["halt_entered_time_us"]
        self._minimum_resume_eligible_time_us = snapshot[
            "minimum_resume_eligible_time_us"
        ]
        self._maximum_resume_deadline_us = snapshot[
            "maximum_resume_deadline_us"
        ]
        self._reopening_auction_end_time_us = snapshot[
            "reopening_auction_end_time_us"
        ]

    def advance_to(self, target_time_us: int) -> tuple[FullDayEventV1, ...]:
        if type(target_time_us) is not int or target_time_us < self.clock.current_time_us:
            raise ValueError("full-day runtime cannot move backward")
        if target_time_us > self.plan.calendar.end_time_us:
            raise ValueError("full-day runtime cannot advance beyond the plan calendar")
        if self._executing is not None:
            raise RuntimeError("full-day runtime advance is not reentrant")
        transaction = self._capture_transaction_state()
        start = len(self._events)
        try:
            while self._heap and self._heap[0][0][0] <= target_time_us:
                _ordering, work_id = heapq.heappop(self._heap)
                item = self._pending.pop(work_id)
                if item.key.simulation_time_us < self.clock.current_time_us:
                    raise RuntimeError("runtime heap moved behind its clock")
                self.clock.advance_to(item.key.simulation_time_us)
                self._executing = item
                self._executing_event_count = 0
                self._executing_zero_delay_children = 0
                handler = getattr(self, self._handler_names[item.work_type])
                handler(item)
                self._executing = None
                self._executing_event_count = 0
                self._executing_zero_delay_children = 0
                self._executed_work[item.work_id] = item
                self._dequeued_count += 1
                self._last_completed_key = item.key
                self._microsteps_at_time.setdefault(
                    item.key.simulation_time_us, set()
                ).add(item.key.microstep)
                if (
                    len(self._microsteps_at_time[item.key.simulation_time_us])
                    > self.plan.deterministic_limits.maximum_microsteps_per_timestamp
                ):
                    raise RuntimeError(
                        "maximum microsteps-per-timestamp limit exceeded"
                    )
                if item.work_type == _WORK_CHECKPOINT_CAPTURE:
                    checkpoint_state_emissions = self._state_runtime.advance_to(
                        self.clock.current_time_us
                    )
                    if checkpoint_state_emissions:
                        raise RuntimeError(
                            "state work was omitted before checkpoint capture"
                        )
                    self._component_sequences[FULL_DAY_RUNTIME_COMPONENT] = (
                        self._state_runtime.component_local_sequence
                    )
                    self.assert_invariants()
                    self.checkpoint_state()
            self.clock.advance_to(target_time_us)
            state_emissions = self._state_runtime.advance_to(target_time_us)
            if state_emissions:
                raise RuntimeError("state work was omitted from the authoritative heap")
            self._component_sequences[FULL_DAY_RUNTIME_COMPONENT] = (
                self._state_runtime.component_local_sequence
            )
            self._schedule_next_state_batch()
            self._schedule_agent_work()
            self.assert_invariants()
            return tuple(self._events[start:])
        except BaseException:
            self._restore_transaction_state(transaction)
            raise

    def advance_by(self, delta_us: int) -> tuple[FullDayEventV1, ...]:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("full-day runtime delta must be nonnegative integer microseconds")
        return self.advance_to(self.clock.current_time_us + delta_us)

    def capture_quiescent_cut(
        self,
        request_id: str | None = None,
        *,
        at_time_us: int | None = None,
    ) -> QuiescentCutV1:
        """Capture one marker-complete cut as a failure-atomic operation."""

        if self._executing is not None:
            raise RuntimeError("checkpoint capture is not reentrant")
        transaction = self._capture_transaction_state()
        try:
            cut = self._capture_quiescent_cut_unchecked(
                request_id,
                at_time_us=at_time_us,
            )
            # A completed capture marker must correspond to a serializable
            # checkpoint within the identity-bearing plan byte limit.
            self.checkpoint_state()
            return cut
        except BaseException:
            self._restore_transaction_state(transaction)
            raise

    def _capture_quiescent_cut_unchecked(
        self,
        request_id: str | None = None,
        *,
        at_time_us: int | None = None,
    ) -> QuiescentCutV1:
        time_us = self.clock.current_time_us if at_time_us is None else at_time_us
        if (
            type(time_us) is not int
            or time_us < self.clock.current_time_us
            or time_us > self.plan.calendar.end_time_us
        ):
            raise ValueError("checkpoint cut must lie within the remaining plan horizon")
        merges_existing = any(
            item.work_type == _WORK_CHECKPOINT_CAPTURE
            and item.key.simulation_time_us == time_us
            for item in self._pending.values()
        )
        if (
            not merges_existing
            and len(self._pending)
            >= self.plan.deterministic_limits.maximum_pending_work_items
        ):
            raise RuntimeError("maximum pending-work limit exceeded")
        if request_id is not None:
            _identifier(request_id, "checkpoint request ID")
            if request_id in self._allocated_checkpoint_request_ids:
                raise ValueError("checkpoint request ID is already allocated")
        selected_id = self._allocate_checkpoint_request_id() if request_id is None else request_id
        if request_id is not None:
            self._allocated_checkpoint_request_ids.append(request_id)
            suffix = request_id.removeprefix("CHECKPOINT-")
            if request_id.startswith("CHECKPOINT-") and suffix.isdigit():
                self._checkpoint_request_next_sequence = max(
                    self._checkpoint_request_next_sequence,
                    int(suffix) + 1,
                )
        self._enqueue_checkpoint(time_us, selected_id)
        self.advance_to(time_us)
        marker = next(
            (
                event
                for event in reversed(self._events)
                if event.event_type
                is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
                and event.payload.data["checkpoint_request_id"] == selected_id
            ),
            None,
        )
        matching_cuts = tuple(
            cut
            for cut in self._quiescent_cuts
            if marker is not None
            and cut.simulation_time_us == time_us
            and cut.last_global_event_sequence >= marker.global_event_sequence
        )
        if marker is None or not matching_cuts:
            raise RuntimeError("checkpoint marker did not reach a quiescent cut")
        return matching_cuts[-1]

    def submit_request(
        self,
        request: AdvancedOrderRequest,
        *,
        at_time_us: int | None = None,
        source_component_id: str = FULL_DAY_RUNTIME_COMPONENT,
    ) -> RuntimeWorkItemV1:
        if type(request) is not AdvancedOrderRequest:
            raise TypeError("runtime gateway submit requires AdvancedOrderRequest")
        if _RUNTIME_ORDER_ID_RE.fullmatch(request.order_id) is not None:
            raise ValueError(
                "FD-O order IDs are reserved for the runtime-owned allocator"
            )
        time_us = self.clock.current_time_us if at_time_us is None else at_time_us
        if source_component_id != FULL_DAY_RUNTIME_COMPONENT:
            raise ValueError(
                "external E1 order submissions must use the full-day runtime owner"
            )
        microstep = self._next_external_microstep(time_us)
        expiry_time = request.good_until_time_us
        if expiry_time is not None and (
            expiry_time < time_us or expiry_time > self.plan.calendar.end_time_us
        ):
            raise ValueError("GTT expiry must lie between arrival and plan end")
        required_capacity = 1 + int(expiry_time is not None)
        if (
            len(self._pending) + required_capacity
            > self.plan.deterministic_limits.maximum_pending_work_items
        ):
            raise RuntimeError("maximum pending-work limit exceeded")
        item = self._enqueue_new(
            simulation_time_us=time_us,
            microstep=microstep,
            stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
            source_component_id=source_component_id,
            work_type=_WORK_MECHANICS_SUBMIT,
            payload={"request": request.as_dict()},
        )
        if expiry_time is not None:
            expiry_microstep = 0 if expiry_time > time_us else microstep + 1
            self._enqueue_new(
                simulation_time_us=expiry_time,
                microstep=expiry_microstep,
                stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                work_type=_WORK_GTT_EXPIRY,
                payload={"expiry_time_us": expiry_time},
            )
        return item

    def cancel_order(
        self,
        order_id: str,
        *,
        reason: str = "FULL_DAY_CANCEL",
        at_time_us: int | None = None,
    ) -> RuntimeWorkItemV1:
        time_us = self.clock.current_time_us if at_time_us is None else at_time_us
        return self._enqueue_new(
            simulation_time_us=time_us,
            microstep=self._next_external_microstep(time_us),
            stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            work_type=_WORK_MECHANICS_CANCEL,
            payload={"order_id": _identifier(order_id, "order_id"), "reason": _identifier(reason, "reason")},
        )

    def replace_order(
        self,
        order_id: str,
        *,
        new_order_id: str,
        new_quantity: int,
        new_price_ticks: int | None = None,
        at_time_us: int | None = None,
    ) -> RuntimeWorkItemV1:
        canonical_new_order_id = _identifier(new_order_id, "new_order_id")
        if _RUNTIME_ORDER_ID_RE.fullmatch(canonical_new_order_id) is not None:
            raise ValueError(
                "FD-O order IDs are reserved for the runtime-owned allocator"
            )
        time_us = self.clock.current_time_us if at_time_us is None else at_time_us
        return self._enqueue_new(
            simulation_time_us=time_us,
            microstep=self._next_external_microstep(time_us),
            stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            work_type=_WORK_MECHANICS_REPLACE,
            payload={
                "new_order_id": canonical_new_order_id,
                "new_price_ticks": new_price_ticks,
                "new_quantity": _exact_int(new_quantity, "new_quantity", minimum=1),
                "order_id": _identifier(order_id, "order_id"),
            },
        )

    def _next_external_microstep(self, time_us: int) -> int:
        if (
            type(time_us) is not int
            or time_us < self.clock.current_time_us
            or time_us > self.plan.calendar.end_time_us
        ):
            raise ValueError("external runtime work must lie within the plan horizon")
        if self._executing is not None and self._executing.key.simulation_time_us == time_us:
            return self._executing.key.microstep + 1
        if self._last_completed_key is not None and self._last_completed_key.simulation_time_us == time_us:
            return self._last_completed_key.microstep + 1
        return 0

    def _emit_outer(
        self,
        event_type: FullDayEventTypeV1,
        *,
        source_component_id: str,
        data: Mapping[str, object],
        native_event: NativeEventReferenceV1 | None = None,
        parent_id: str | None = None,
        component_local_sequence: int | None = None,
    ) -> FullDayEventV1:
        if self._executing is None:
            raise RuntimeError("outer events require one dequeued causal work item")
        if len(self._events) >= self.plan.deterministic_limits.maximum_outer_events:
            raise RuntimeError("maximum outer-event limit exceeded")
        item = self._executing
        next_consequence_count = self._executing_event_count + 1
        if (
            next_consequence_count
            > self.plan.deterministic_limits.maximum_synchronous_consequences_per_work_item
        ):
            raise RuntimeError(
                "maximum synchronous consequences per work item exceeded"
            )
        timestamp_count = (
            self._events_at_time.get(item.key.simulation_time_us, 0) + 1
        )
        if (
            timestamp_count
            > self.plan.deterministic_limits.maximum_events_per_timestamp
        ):
            raise RuntimeError("maximum events-per-timestamp limit exceeded")
        sequence = (
            self._reserve_component_sequence(source_component_id)
            if component_local_sequence is None
            else component_local_sequence
        )
        prior_same_owner = max(
            (
                event.component_local_sequence
                for event in self._events
                if event.source_component_id == source_component_id
            ),
            default=0,
        )
        if type(sequence) is not int or sequence <= prior_same_owner:
            raise RuntimeError("outer component-local sequence did not increase")
        self._component_sequences[source_component_id] = max(
            self._component_sequences.get(source_component_id, 0), sequence
        )
        event = FullDayEventV1(
            schema_version=1,
            global_event_sequence=self._next_global_event_sequence,
            simulation_time_us=item.key.simulation_time_us,
            microstep=item.key.microstep,
            stage=item.key.stage_ordinal,
            source_component_id=source_component_id,
            component_local_sequence=sequence,
            event_type=event_type,
            causal_parent_ids=(item.work_id if parent_id is None else parent_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=event_type.value,
                payload_version=1,
                native_event=native_event,
                data=data,
            ),
        )
        self._next_global_event_sequence += 1
        self._events.append(event)
        self._events_at_time[event.simulation_time_us] = timestamp_count
        self._executing_event_count = next_consequence_count
        return event

    def _emit_native(
        self,
        *,
        owner_component_id: str,
        native_ledger_id: str,
        native_event_type: str,
        native_local_sequence: int,
        native_event_id: str,
        native_payload: Mapping[str, object],
        outer_event_type: FullDayEventTypeV1 = FullDayEventTypeV1.SUBSYSTEM_EVENT,
        outer_data: Mapping[str, object] | None = None,
        parent_id: str | None = None,
    ) -> FullDayEventV1:
        reference = NativeEventReferenceV1(
            schema_version=1,
            owner_component_id=owner_component_id,
            native_ledger_id=native_ledger_id,
            event_type=native_event_type,
            local_sequence=native_local_sequence,
            event_id=native_event_id,
        )
        entry = NativeLedgerEntryV1(reference=reference, payload=native_payload)
        if entry.ledger_key in self._native_ledger:
            raise RuntimeError("native event identity is duplicated")
        projection: dict[str, object] = {
            "native_payload_sha256": entry.payload_sha256
        }
        if outer_data is not None:
            projection.update(dict(outer_data))
        event = self._emit_outer(
            outer_event_type,
            source_component_id=owner_component_id,
            data=projection,
            native_event=reference,
            parent_id=parent_id,
        )
        self._native_ledger[entry.ledger_key] = entry
        return event

    def _next_agent_native_sequence(self) -> int:
        value = self._native_sequences[AGENT_SCHEDULER_COMPONENT] + 1
        self._native_sequences[AGENT_SCHEDULER_COMPONENT] = value
        return value

    def _scheduler_market_snapshot(self) -> dict[str, object] | None:
        if self.agent_scheduler is None:
            return None
        snapshot = getattr(self.agent_scheduler, "market_snapshot", None)
        if not callable(snapshot):
            raise RuntimeError("active scheduler omits its aggregate market snapshot")
        value = snapshot()
        if type(value) is not dict:
            raise RuntimeError("scheduler aggregate market snapshot is not detached")
        return value

    def _wrap_new_scheduler_public_events(
        self, *, parent_id: str | None
    ) -> str | None:
        if self.agent_scheduler is None:
            return parent_id
        published = tuple(getattr(self.agent_scheduler, "public_events", ()))
        emitted = tuple(
            sorted(
                (
                    entry
                    for entry in self._native_ledger.values()
                    if entry.reference.owner_component_id
                    == AGENT_SCHEDULER_COMPONENT
                    and entry.reference.event_type == "PUBLICECOLOGYEVENT"
                ),
                key=lambda entry: entry.reference.local_sequence,
            )
        )
        if len(emitted) > len(published):
            raise RuntimeError("agent public native ledger exceeds scheduler history")
        for entry, row in zip(emitted, published, strict=False):
            as_dict = getattr(row, "as_dict", None)
            if not callable(as_dict) or _plain(entry.payload) != as_dict():
                raise RuntimeError(
                    "agent public native ledger prefix differs from scheduler history"
                )
        causal_parent = parent_id
        for row in published[len(emitted) :]:
            as_dict = getattr(row, "as_dict", None)
            if not callable(as_dict):
                raise TypeError("scheduler public row has no canonical as_dict")
            payload = as_dict()
            sequence = self._next_agent_native_sequence()
            outer = self._emit_native(
                owner_component_id=AGENT_SCHEDULER_COMPONENT,
                native_ledger_id=AGENT_NATIVE_LEDGER_ID,
                native_event_type="PUBLICECOLOGYEVENT",
                native_local_sequence=sequence,
                native_event_id=f"AGENT_SCHEDULER_EVENT_{sequence:012d}",
                native_payload=payload,
                parent_id=causal_parent,
            )
            causal_parent = outer.event_id
        return causal_parent

    def _assert_agent_native_reconciliation(self) -> None:
        """Cross-bind scheduler state to its exact runtime-owned native ledger."""

        agent_native = tuple(
            sorted(
                (
                    entry
                    for entry in self._native_ledger.values()
                    if entry.reference.owner_component_id
                    == AGENT_SCHEDULER_COMPONENT
                ),
                key=lambda entry: entry.reference.local_sequence,
            )
        )
        if self.agent_scheduler is None:
            if agent_native:
                raise RuntimeError("inactive scheduler retains native ledger rows")
            return

        from kirby2.agents.ecology import ScheduledAgentIntent

        allowed_types = {
            "AGENTTRUTHEVENT",
            "PARTICIPANT_ACTIVATED",
            "PARTICIPANT_DEACTIVATED",
            "PARTICIPANT_RETUNED",
            "PUBLICECOLOGYEVENT",
            "SCHEDULEDAGENTINTENT",
        }
        for sequence, entry in enumerate(agent_native, start=1):
            reference = entry.reference
            if (
                reference.native_ledger_id != AGENT_NATIVE_LEDGER_ID
                or reference.local_sequence != sequence
                or reference.event_id
                != f"AGENT_SCHEDULER_EVENT_{sequence:012d}"
                or reference.event_type not in allowed_types
            ):
                raise RuntimeError(
                    "agent native ledger identity/type sequence is noncanonical"
                )

        truth_payloads = [
            _plain(entry.payload)
            for entry in agent_native
            if entry.reference.event_type == "AGENTTRUTHEVENT"
        ]
        expected_truth = [
            row.as_dict()
            for row in getattr(self.agent_scheduler, "_truth_events", ())
        ]
        if truth_payloads != expected_truth:
            raise RuntimeError(
                "agent truth history differs from its exact native ledger"
            )

        public_payloads = [
            _plain(entry.payload)
            for entry in agent_native
            if entry.reference.event_type == "PUBLICECOLOGYEVENT"
        ]
        expected_public = [
            row.as_dict()
            for row in getattr(self.agent_scheduler, "_public_events", ())
        ]
        if public_payloads != expected_public:
            raise RuntimeError(
                "agent public history differs from its exact native ledger"
            )

        outer_by_native_key = {
            event.payload.native_event.ledger_key: event
            for event in self._events
            if event.payload.native_event is not None
        }
        plan_lifecycle_by_id = {
            row.schedule_id: row for row in self.plan.participant_schedule
        }
        lifecycle_type_by_action = {
            ParticipantScheduleActionV1.ACTIVATE: (
                FullDayEventTypeV1.PARTICIPANT_ACTIVATED
            ),
            ParticipantScheduleActionV1.DEACTIVATE: (
                FullDayEventTypeV1.PARTICIPANT_DEACTIVATED
            ),
            ParticipantScheduleActionV1.RETUNE: (
                FullDayEventTypeV1.PARTICIPANT_RETUNED
            ),
        }
        for native_entry in agent_native:
            if native_entry.reference.event_type not in {
                event_type.value for event_type in lifecycle_type_by_action.values()
            }:
                continue
            payload = _plain(native_entry.payload)
            if type(payload) is not dict:
                raise RuntimeError("participant lifecycle native payload is not an object")
            schedule_id = payload.get("schedule_id")
            if type(schedule_id) is not str or schedule_id not in plan_lifecycle_by_id:
                raise RuntimeError("participant lifecycle cites an unknown plan row")
            plan_row = plan_lifecycle_by_id[schedule_id]
            expected_payload: dict[str, object] = {
                "action": plan_row.action.value,
                "discarded_pending_sequences": payload.get(
                    "discarded_pending_sequences"
                ),
                "participant_id": plan_row.participant_id,
                "schedule_id": plan_row.schedule_id,
                "simulation_time_us": plan_row.simulation_time_us,
            }
            expected_outer_data: dict[str, object] = {
                "native_payload_sha256": native_entry.payload_sha256,
                "participant_id": plan_row.participant_id,
                "schedule_id": plan_row.schedule_id,
            }
            if plan_row.replacement_specification is not None:
                digest = plan_row.replacement_specification.sha256
                expected_payload["replacement_specification_sha256"] = digest
                expected_outer_data["replacement_specification_sha256"] = digest
            expected_outer_type = lifecycle_type_by_action[plan_row.action]
            outer = outer_by_native_key.get(native_entry.ledger_key)
            if (
                payload != expected_payload
                or native_entry.reference.event_type != expected_outer_type.value
                or outer is None
                or outer.event_type is not expected_outer_type
                or outer.simulation_time_us != plan_row.simulation_time_us
                or _plain(outer.payload.data) != expected_outer_data
            ):
                raise RuntimeError(
                    "participant lifecycle evidence differs from its exact plan row"
                )

        scheduled_by_sequence: dict[int, ScheduledAgentIntent] = {}
        for entry in agent_native:
            if entry.reference.event_type != "SCHEDULEDAGENTINTENT":
                continue
            payload = _plain(entry.payload)
            if type(payload) is not dict:  # pragma: no cover - strict ledger object
                raise RuntimeError("scheduled intent native payload is not an object")
            row = ScheduledAgentIntent.from_dict(payload)
            if row.sequence in scheduled_by_sequence:
                raise RuntimeError("scheduled intent sequence is duplicated")
            scheduled_by_sequence[row.sequence] = row
        pending_sequence = getattr(self.agent_scheduler, "_pending_sequence", None)
        if (
            type(pending_sequence) is not int
            or tuple(sorted(scheduled_by_sequence))
            != tuple(range(1, pending_sequence + 1))
        ):
            raise RuntimeError(
                "scheduler pending allocator differs from scheduled-intent evidence"
            )

        outstanding: dict[int, ScheduledAgentIntent] = {}
        for entry in agent_native:
            event_type = entry.reference.event_type
            payload = _plain(entry.payload)
            if type(payload) is not dict:
                raise RuntimeError("agent native payload is not an object")
            if event_type == "SCHEDULEDAGENTINTENT":
                row = ScheduledAgentIntent.from_dict(payload)
                outstanding[row.sequence] = row
            elif event_type == "AGENTTRUTHEVENT":
                if not outstanding:
                    raise RuntimeError("agent truth row has no scheduled intent")
                sequence, scheduled = min(
                    outstanding.items(),
                    key=lambda item: (
                        item[1].arrival_time_us,
                        item[1].sequence,
                    ),
                )
                if (
                    payload["agent_id"] != scheduled.agent_id
                    or payload["arrival_time_us"] != scheduled.arrival_time_us
                    or payload["decision_time_us"] != scheduled.decision_time_us
                    or payload["intent"] != scheduled.intent.as_dict()
                ):
                    raise RuntimeError(
                        "agent truth row differs from the earliest scheduled intent"
                    )
                del outstanding[sequence]
            elif event_type in {
                "PARTICIPANT_ACTIVATED",
                "PARTICIPANT_DEACTIVATED",
                "PARTICIPANT_RETUNED",
            }:
                raw_discarded = payload.get("discarded_pending_sequences")
                if type(raw_discarded) is not list or any(
                    type(value) is not int for value in raw_discarded
                ):
                    raise RuntimeError(
                        "participant lifecycle lacks exact discarded-intent evidence"
                    )
                participant_id = str(payload.get("participant_id"))
                participant_pending = tuple(
                    sorted(
                        sequence
                        for sequence, row in outstanding.items()
                        if row.agent_id == participant_id
                    )
                )
                discarded = tuple(raw_discarded)
                if event_type == "PARTICIPANT_DEACTIVATED":
                    if discarded != participant_pending:
                        raise RuntimeError(
                            "deactivation discarded-intent ledger is not exact"
                        )
                    for sequence in discarded:
                        del outstanding[sequence]
                elif discarded or (
                    event_type == "PARTICIPANT_RETUNED" and participant_pending
                ):
                    raise RuntimeError(
                        "activation/retune has inconsistent pending intents"
                    )

        scheduler_pending = {
            row.sequence: row.as_dict()
            for row in getattr(self.agent_scheduler, "_pending", ())
        }
        replayed_pending = {
            sequence: row.as_dict()
            for sequence, row in outstanding.items()
        }
        if replayed_pending != scheduler_pending:
            raise RuntimeError(
                "scheduler pending intents differ from native-ledger replay"
            )

    def _assert_agent_deadline_replay(self) -> None:
        """Rebuild participant deadlines from lifecycle and executed work."""

        if self.agent_scheduler is None:
            return
        scheduler = self.agent_scheduler
        definition_specs = {
            spec.agent_id: spec for spec in scheduler.definition.agents
        }
        current_specs = {
            agent_id: agent.spec
            for agent_id, agent in scheduler.agents.items()
        }
        digest_specs: dict[tuple[str, str], object] = {}
        for spec in (*definition_specs.values(), *current_specs.values()):
            digest_specs[
                (spec.agent_id, canonical_sha256(spec.identity_dict()))
            ] = spec
        specs = dict(definition_specs)
        active = {
            participant.participant_id: participant.initially_active
            for participant in self.plan.participant_definitions
        }

        def scheduled_deadline(spec: object, candidate: int) -> int | None:
            bounds = spec.bounds
            return (
                candidate
                if candidate + bounds.latency_us <= bounds.lifetime_end_us
                else None
            )

        deadlines: dict[str, int | None] = {}
        for agent_id, spec in specs.items():
            candidate = max(0, spec.bounds.lifetime_start_us)
            deadlines[agent_id] = (
                scheduled_deadline(spec, candidate)
                if active[agent_id]
                else None
            )
        decision_counts = {agent_id: 0 for agent_id in specs}
        schedule_by_id = {
            entry.schedule_id: entry for entry in self.plan.participant_schedule
        }
        for item in sorted(
            self._executed_work.values(),
            key=lambda value: value.key.ordering_key,
        ):
            time_us = item.key.simulation_time_us
            if item.work_type == _WORK_PARTICIPANT_SCHEDULE:
                schedule_id = str(item.payload["schedule_id"])
                try:
                    entry = schedule_by_id[schedule_id]
                except KeyError as error:
                    raise RuntimeError(
                        "executed participant work has no immutable plan row"
                    ) from error
                if entry.simulation_time_us != time_us:
                    raise RuntimeError(
                        "executed participant work time differs from its plan row"
                    )
                agent_id = entry.participant_id
                if entry.action is ParticipantScheduleActionV1.ACTIVATE:
                    if active[agent_id]:
                        raise RuntimeError(
                            "deadline replay activates an active participant"
                        )
                    active[agent_id] = True
                    spec = specs[agent_id]
                    deadlines[agent_id] = scheduled_deadline(
                        spec,
                        max(time_us, spec.bounds.lifetime_start_us),
                    )
                elif entry.action is ParticipantScheduleActionV1.DEACTIVATE:
                    if not active[agent_id]:
                        raise RuntimeError(
                            "deadline replay deactivates an inactive participant"
                        )
                    active[agent_id] = False
                    deadlines[agent_id] = None
                else:
                    reference = entry.replacement_specification
                    if reference is None:
                        raise RuntimeError(
                            "deadline replay retune omits its replacement spec"
                        )
                    try:
                        spec = digest_specs[(agent_id, reference.sha256)]
                    except KeyError as error:
                        raise RuntimeError(
                            "deadline replay cannot resolve the retune spec"
                        ) from error
                    specs[agent_id] = spec
                    if active[agent_id]:
                        current = deadlines[agent_id]
                        candidate = max(
                            time_us,
                            spec.bounds.lifetime_start_us,
                            time_us if current is None else current,
                        )
                        deadlines[agent_id] = scheduled_deadline(
                            spec, candidate
                        )
            elif item.work_type == _WORK_AGENT_DECISION:
                due = tuple(
                    agent_id
                    for agent_id in sorted(deadlines)
                    if active[agent_id] and deadlines[agent_id] == time_us
                )
                if not due:
                    raise RuntimeError(
                        "executed agent decision has no replay-derived due participant"
                    )
                for agent_id in due:
                    spec = specs[agent_id]
                    decision_counts[agent_id] += 1
                    deadlines[agent_id] = scheduled_deadline(
                        spec,
                        time_us + spec.bounds.decision_interval_us,
                    )
        observed_counts = {
            agent_id: agent.decision_count
            for agent_id, agent in scheduler.agents.items()
        }
        if active != scheduler._active:
            raise RuntimeError(
                "agent activation state differs from lifecycle/work replay"
            )
        if deadlines != scheduler._next_decision_us:
            raise RuntimeError(
                "agent next-decision deadlines differ from lifecycle/work replay"
            )
        if decision_counts != observed_counts:
            raise RuntimeError(
                "agent decision counters differ from executed decision work"
            )

    def _wrap_new_mechanics(
        self,
        *,
        parent_id: str | None = None,
        scheduler_book_before: dict[str, object] | None = None,
    ) -> str | None:
        events = self.engine.events
        if self._mechanics_event_cursor > len(events):
            raise RuntimeError("mechanics event cursor exceeds its ledger")
        emitted_parent = parent_id
        for native in events[self._mechanics_event_cursor :]:
            if native.simulation_time_us != self.clock.current_time_us:
                raise RuntimeError("new mechanics event differs from dequeued work time")
            outer = self._emit_native(
                owner_component_id=MECHANICS_COMPONENT,
                native_ledger_id=MECHANICS_NATIVE_LEDGER_ID,
                native_event_type=native.event_type.value,
                native_local_sequence=native.sequence,
                native_event_id=f"MECHANICS_EVENT_{native.sequence:012d}",
                native_payload=native.as_dict(),
                parent_id=emitted_parent,
            )
            emitted_parent = outer.event_id
        self._mechanics_event_cursor = len(events)
        if self.agent_scheduler is not None:
            synchronize = getattr(
                self.agent_scheduler, "synchronize_external_mechanics", None
            )
            if not callable(synchronize):
                raise RuntimeError("active scheduler omits mechanics synchronization")
            synchronize(scheduler_book_before)
            emitted_parent = self._wrap_new_scheduler_public_events(
                parent_id=emitted_parent
            )
        return emitted_parent

    def _handle_calendar_boundary(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        index = _exact_int(
            item.payload.get("boundary_operation_index"),
            "boundary_operation_index",
        )
        if index != self._calendar_boundary_index:
            raise RuntimeError("calendar boundary cursor is not canonical")
        try:
            operation = self.plan.calendar.boundary_operations[index]
        except IndexError as error:
            raise ValueError("calendar boundary index exceeds the plan") from error
        if operation.boundary.simulation_time_us != self.clock.current_time_us:
            raise RuntimeError("calendar operation differs from dequeued work time")
        mechanics_start = len(self.engine.events)
        if operation.uncross_before:
            self.engine.uncross_auction()
        self.engine.transition_session(
            operation.destination_session_state,
            reason=f"FULL_DAY_CALENDAR_BOUNDARY_{index}",
        )
        # GTT expiry is deliberately after uncross/transition-owned expiry and
        # session events at an atomic calendar boundary.
        self.engine.advance_to(self.clock.current_time_us)
        outer = self._emit_outer(
            FullDayEventTypeV1.CALENDAR_BOUNDARY,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            data={
                "boundary_operation_index": index,
                "destination_session_state": operation.destination_session_state.value,
                "uncross_before": operation.uncross_before,
            },
        )
        if len(self.engine.events) < mechanics_start:
            raise RuntimeError("mechanics ledger shrank during calendar boundary")
        self._wrap_new_mechanics(
            parent_id=outer.event_id,
            scheduler_book_before=scheduler_book_before,
        )
        self._calendar_boundary_index += 1

    def _handle_scheduled_information(self, item: RuntimeWorkItemV1) -> None:
        event_id = _identifier(item.payload.get("scheduled_event_id"), "scheduled_event_id")
        by_id = {event.event_id: event for event in self.plan.scheduled_events}
        try:
            scheduled = by_id[event_id]
        except KeyError as error:
            raise ValueError("scheduled work cites an unknown plan event") from error
        ordered_ids = [event.event_id for event in self.plan.scheduled_events]
        if self._scheduled_event_index >= len(ordered_ids) or ordered_ids[self._scheduled_event_index] != event_id:
            raise RuntimeError("scheduled information cursor is not canonical")
        outer = self._emit_outer(
            FullDayEventTypeV1.SCHEDULED_INFORMATION,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            data={
                "parameter_set_sha256": scheduled.parameter_set_sha256,
                "scheduled_event_id": scheduled.event_id,
                "scheduled_event_type": scheduled.event_type.value,
                "side": scheduled.side.value,
            },
        )
        transition_handlers = {
            ScheduledEventTypeV1.HALT: self._enter_halt,
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION: self._enter_halt,
            ScheduledEventTypeV1.REOPENING: self._begin_reopening,
        }
        handler = transition_handlers.get(scheduled.event_type)
        if handler is not None:
            handler(scheduled, parent_id=outer.event_id)
        self._scheduled_event_index += 1

    @staticmethod
    def _scheduled_parameters(scheduled: object) -> dict[str, int]:
        return {
            parameter.name: parameter.value
            for parameter in getattr(scheduled, "parameters")
        }

    def _enter_halt(self, scheduled: object, *, parent_id: str) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        if self.engine.session_state is not SessionState.CONTINUOUS:
            raise RuntimeError("scheduled halt requires CONTINUOUS session state")
        if self._halt_count >= self.plan.halt_reopen_rules.maximum_halts:
            raise RuntimeError("scheduled halt exceeds the plan maximum")
        duration = self._scheduled_parameters(scheduled)["halt_duration_us"]
        self.engine.transition_session(
            SessionState.HALTED,
            reason=f"FULL_DAY_{getattr(scheduled, 'event_type').value}",
        )
        self._halt_count += 1
        self._halt_entered_time_us = self.clock.current_time_us
        self._minimum_resume_eligible_time_us = (
            self.clock.current_time_us
            + self.plan.halt_reopen_rules.minimum_halt_duration_us
        )
        self._maximum_resume_deadline_us = min(
            self.clock.current_time_us + duration,
            self.clock.current_time_us
            + self.plan.halt_reopen_rules.maximum_halt_duration_us,
        )
        self._wrap_new_mechanics(
            parent_id=parent_id,
            scheduler_book_before=scheduler_book_before,
        )

    def _begin_reopening(self, scheduled: object, *, parent_id: str) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        if self.engine.session_state is not SessionState.HALTED:
            raise RuntimeError("scheduled reopening requires HALTED session state")
        if (
            self._minimum_resume_eligible_time_us is not None
            and self.clock.current_time_us < self._minimum_resume_eligible_time_us
        ):
            raise RuntimeError("scheduled reopening precedes minimum halt duration")
        self.engine.transition_session(
            SessionState.REOPENING_AUCTION,
            reason="FULL_DAY_SCHEDULED_REOPENING",
        )
        self._wrap_new_mechanics(
            parent_id=parent_id,
            scheduler_book_before=scheduler_book_before,
        )
        duration = self._scheduled_parameters(scheduled)[
            "reopening_auction_duration_us"
        ]
        self._reopening_auction_end_time_us = self.clock.current_time_us + duration
        self._enqueue_new(
            simulation_time_us=self._reopening_auction_end_time_us,
            microstep=0,
            stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            work_type=_WORK_REOPEN_COMPLETE,
            payload={"scheduled_event_id": getattr(scheduled, "event_id")},
        )

    def _handle_reopen_complete(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        if (
            self.engine.session_state is not SessionState.REOPENING_AUCTION
            or self._reopening_auction_end_time_us != self.clock.current_time_us
        ):
            raise RuntimeError("reopening completion state is inconsistent")
        if self.plan.halt_reopen_rules.uncross_before_resume:
            self.engine.uncross_auction()
        self.engine.transition_session(
            SessionState.CONTINUOUS,
            reason="FULL_DAY_REOPENING_COMPLETE",
        )
        self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )
        self._halt_entered_time_us = None
        self._minimum_resume_eligible_time_us = None
        self._maximum_resume_deadline_us = None
        self._reopening_auction_end_time_us = None

    def _handle_participant_schedule(self, item: RuntimeWorkItemV1) -> None:
        if self.agent_scheduler is None:
            raise RuntimeError("participant work has no active scheduler owner")
        schedule_id = _identifier(item.payload.get("schedule_id"), "schedule_id")
        scheduler_book_before = self._scheduler_market_snapshot()
        synchronize = getattr(
            self.agent_scheduler, "synchronize_external_mechanics", None
        )
        if not callable(synchronize):
            raise RuntimeError("active scheduler omits mechanics synchronization")
        synchronize(scheduler_book_before)
        by_id = {entry.schedule_id: entry for entry in self.plan.participant_schedule}
        try:
            entry = by_id[schedule_id]
        except KeyError as error:
            raise ValueError("participant work cites an unknown schedule row") from error
        ordered_ids = [row.schedule_id for row in self.plan.participant_schedule]
        if self._participant_schedule_index >= len(ordered_ids) or ordered_ids[self._participant_schedule_index] != schedule_id:
            raise RuntimeError("participant schedule cursor is not canonical")
        method_by_action = {
            ParticipantScheduleActionV1.ACTIVATE: "activate_agent",
            ParticipantScheduleActionV1.DEACTIVATE: "deactivate_agent",
            ParticipantScheduleActionV1.RETUNE: "retune_agent",
        }
        method = getattr(self.agent_scheduler, method_by_action[entry.action], None)
        if not callable(method):
            raise RuntimeError("scheduler omits its participant lifecycle hook")
        pending_before = {
            int(getattr(row, "sequence"))
            for row in getattr(self.agent_scheduler, "_pending", ())
            if getattr(row, "agent_id", None) == entry.participant_id
        }
        if entry.action is ParticipantScheduleActionV1.RETUNE:
            if entry.replacement_specification is None:
                raise RuntimeError("retune schedule omits its replacement specification")
            candidates = tuple(
                spec
                for spec in getattr(
                    getattr(self.agent_scheduler, "definition", None), "agents", ()
                )
                if getattr(spec, "agent_id", None) == entry.participant_id
                and canonical_sha256(spec.identity_dict())
                == entry.replacement_specification.sha256
            )
            if len(candidates) != 1:
                raise RuntimeError(
                    "retune specification is not uniquely resolved by the injected definition"
                )
            method(
                entry.participant_id,
                candidates[0],
                simulation_time_us=self.clock.current_time_us,
            )
        else:
            method(
                entry.participant_id,
                simulation_time_us=self.clock.current_time_us,
            )
        pending_after = {
            int(getattr(row, "sequence"))
            for row in getattr(self.agent_scheduler, "_pending", ())
            if getattr(row, "agent_id", None) == entry.participant_id
        }
        discarded_pending_sequences = tuple(sorted(pending_before - pending_after))
        if (
            entry.action is not ParticipantScheduleActionV1.DEACTIVATE
            and discarded_pending_sequences
        ):
            raise RuntimeError("non-deactivation lifecycle discarded pending intents")
        native_sequence = self._next_agent_native_sequence()
        native_payload: dict[str, object] = {
            "action": entry.action.value,
            "discarded_pending_sequences": list(discarded_pending_sequences),
            "participant_id": entry.participant_id,
            "schedule_id": entry.schedule_id,
            "simulation_time_us": self.clock.current_time_us,
        }
        outer_type_by_action = {
            ParticipantScheduleActionV1.ACTIVATE: FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
            ParticipantScheduleActionV1.DEACTIVATE: FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
            ParticipantScheduleActionV1.RETUNE: FullDayEventTypeV1.PARTICIPANT_RETUNED,
        }
        outer_data: dict[str, object] = {
            "participant_id": entry.participant_id,
            "schedule_id": entry.schedule_id,
        }
        if entry.replacement_specification is not None:
            digest = entry.replacement_specification.sha256
            native_payload["replacement_specification_sha256"] = digest
            outer_data["replacement_specification_sha256"] = digest
        lifecycle_event = self._emit_native(
            owner_component_id=AGENT_SCHEDULER_COMPONENT,
            native_ledger_id=AGENT_NATIVE_LEDGER_ID,
            native_event_type=outer_type_by_action[entry.action].value,
            native_local_sequence=native_sequence,
            native_event_id=f"AGENT_SCHEDULER_EVENT_{native_sequence:012d}",
            native_payload=native_payload,
            outer_event_type=outer_type_by_action[entry.action],
            outer_data=outer_data,
        )
        self._wrap_new_mechanics(
            parent_id=lifecycle_event.event_id,
            scheduler_book_before=scheduler_book_before,
        )
        self._participant_schedule_index += 1
        self._schedule_agent_work()

    def _handle_agent_work(self, item: RuntimeWorkItemV1) -> None:
        if self.agent_scheduler is None:
            raise RuntimeError("agent work has no active scheduler")
        token = (item.work_type, item.key.simulation_time_us)
        self._agent_tokens.discard(token)
        scheduler_book_before = self._scheduler_market_snapshot()
        before_time = self.clock.current_time_us
        before_engine = self.engine.canonical_state_bytes()
        before_event_count = len(self.engine.events)
        execute = getattr(self.agent_scheduler, "execute_due_work", None)
        if not callable(execute):
            raise RuntimeError("agent scheduler omits execute_due_work")
        result = execute(item.key)
        if self.clock.current_time_us != before_time or self.engine.clock is not self.clock:
            raise RuntimeError("agent scheduler advanced or replaced the runtime clock")
        after_engine = self.engine.canonical_state_bytes()
        if item.work_type == _WORK_AGENT_DECISION and after_engine != before_engine:
            raise RuntimeError("agent decision stage mutated the exchange instead of proposing")
        if (
            item.work_type == _WORK_AGENT_ARRIVAL
            and after_engine != before_engine
            and len(self.engine.events) == before_event_count
        ):
            raise RuntimeError("agent arrival mutated exchange state without mechanics events")
        parent = self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )
        self._emit_agent_result_rows(result, parent_id=parent)
        self._schedule_agent_work()

    def _emit_agent_result_rows(self, result: object, *, parent_id: str | None) -> None:
        if result is None:
            return
        rows: Sequence[object]
        if type(result) in {tuple, list}:
            rows = result  # type: ignore[assignment]
        else:
            discovered: list[object] = []
            recognized = False
            for field in ("scheduled_intents", "truth_events"):
                value = getattr(result, field, None)
                if type(value) in {tuple, list}:
                    recognized = True
                    discovered.extend(value)
            if not recognized:
                value = getattr(result, "rows", None)
                if type(value) in {tuple, list}:
                    discovered.extend(value)
            rows = tuple(discovered)
        causal_parent = parent_id
        for row in rows:
            as_dict = getattr(row, "as_dict", None)
            if not callable(as_dict):
                raise TypeError("agent work result row has no canonical as_dict")
            payload = as_dict()
            if not isinstance(payload, Mapping):
                raise TypeError("agent work result payload must be an object")
            sequence = self._next_agent_native_sequence()
            native_type = type(row).__name__.upper()
            outer = self._emit_native(
                owner_component_id=AGENT_SCHEDULER_COMPONENT,
                native_ledger_id=AGENT_NATIVE_LEDGER_ID,
                native_event_type=native_type,
                native_local_sequence=sequence,
                native_event_id=f"AGENT_SCHEDULER_EVENT_{sequence:012d}",
                native_payload=payload,
                parent_id=causal_parent,
            )
            causal_parent = outer.event_id

    def _handle_state_emission(self, item: RuntimeWorkItemV1) -> None:
        batch_time = _exact_int(item.payload.get("batch_time_us"), "batch_time_us")
        index = _exact_int(item.payload.get("emission_index"), "emission_index")
        if batch_time != self.clock.current_time_us or batch_time != self._state_scheduled_time:
            raise RuntimeError("state emission batch time is inconsistent")
        if not self._state_emission_buffer:
            self._state_emission_buffer = self._state_runtime.advance_to(batch_time)
            self._component_sequences[FULL_DAY_RUNTIME_COMPONENT] = (
                self._state_runtime.component_local_sequence
            )
        if index >= len(self._state_emission_buffer) or index in self._state_emission_consumed:
            raise RuntimeError("state emission index is invalid or duplicated")
        emission = self._state_emission_buffer[index]
        if (
            emission.simulation_time_us != item.key.simulation_time_us
            or emission.microstep != item.key.microstep
            or emission.stage is not item.key.stage_ordinal
        ):
            raise RuntimeError("state emission differs from its previewed work key")
        self._emit_outer(
            emission.event_type,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            data=_state_emission_outer_data(emission),
            component_local_sequence=emission.component_local_sequence,
        )
        self._state_emission_consumed.add(index)
        if len(self._state_emission_consumed) == len(self._state_emission_buffer):
            self._state_emission_buffer = ()
            self._state_emission_consumed.clear()
            self._state_scheduled_time = None
            self._schedule_next_state_batch()

    def _handle_mechanics_submit(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        raw = _plain_object(item.payload.get("request"), "mechanics request")
        request = AdvancedOrderRequest.from_dict(raw)
        if request.as_dict() != raw:
            raise ValueError("mechanics request is not strict canonical state")
        if _RUNTIME_ORDER_ID_RE.fullmatch(request.order_id) is not None:
            raise ValueError(
                "external mechanics work cannot claim a runtime-allocated order ID"
            )
        self.engine.submit(request)
        self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )

    def _handle_mechanics_cancel(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        self.engine.cancel(
            _identifier(item.payload.get("order_id"), "order_id"),
            reason=_identifier(item.payload.get("reason"), "reason"),
        )
        self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )

    def _handle_mechanics_replace(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        new_order_id = _identifier(
            item.payload.get("new_order_id"), "new_order_id"
        )
        if _RUNTIME_ORDER_ID_RE.fullmatch(new_order_id) is not None:
            raise ValueError(
                "external replacement cannot claim a runtime-allocated order ID"
            )
        self.engine.replace_order(
            _identifier(item.payload.get("order_id"), "order_id"),
            new_order_id=new_order_id,
            new_quantity=_exact_int(item.payload.get("new_quantity"), "new_quantity", minimum=1),
            new_price_ticks=_exact_optional_int(
                item.payload.get("new_price_ticks"), "new_price_ticks", minimum=1
            ),
        )
        self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )

    def _handle_gtt_expiry(self, item: RuntimeWorkItemV1) -> None:
        scheduler_book_before = self._scheduler_market_snapshot()
        if _exact_int(item.payload.get("expiry_time_us"), "expiry_time_us") != self.clock.current_time_us:
            raise RuntimeError("GTT expiry work time is inconsistent")
        self.engine.advance_to(self.clock.current_time_us)
        self._wrap_new_mechanics(
            scheduler_book_before=scheduler_book_before
        )

    def _handle_checkpoint_capture(self, item: RuntimeWorkItemV1) -> None:
        raw_ids = item.payload.get("checkpoint_request_ids")
        if type(raw_ids) is not tuple:
            # Frozen JSON arrays are tuples.
            raise TypeError("checkpoint request IDs must be an ordered array")
        request_ids = tuple(_identifier(value, "checkpoint request ID") for value in raw_ids)
        if request_ids != tuple(sorted(set(request_ids))):
            raise ValueError("checkpoint request IDs must be sorted and unique")
        same_time = [
            pending
            for pending in self._pending.values()
            if pending.key.simulation_time_us == self.clock.current_time_us
        ]
        if same_time:
            later_microstep = max(
                item.key.microstep,
                *(pending.key.microstep for pending in same_time),
            ) + 1
            self._enqueue_new(
                simulation_time_us=self.clock.current_time_us,
                microstep=later_microstep,
                stage=WorkStageV1.CHECKPOINT_CAPTURE,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                work_type=_WORK_CHECKPOINT_CAPTURE,
                payload={"checkpoint_request_ids": list(request_ids)},
            )
            return
        if self._checkpoint_completed_count >= self.plan.checkpoint_policy.maximum_checkpoint_count:
            raise RuntimeError("maximum checkpoint count exceeded")
        last_marker: FullDayEventV1 | None = None
        for request_id in request_ids:
            last_marker = self._emit_outer(
                FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
                source_component_id=FULL_DAY_RUNTIME_COMPONENT,
                data={"checkpoint_request_id": request_id},
                parent_id=None if last_marker is None else last_marker.event_id,
            )
        assert last_marker is not None
        pending = self.pending_work
        if pending and pending[0].key.simulation_time_us <= self.clock.current_time_us:
            raise RuntimeError("checkpoint marker left due work at its cut")
        boundary_complete = all(
            operation.boundary.simulation_time_us > self.clock.current_time_us
            or index < self._calendar_boundary_index
            for index, operation in enumerate(self.plan.calendar.boundary_operations)
        )
        cut = QuiescentCutV1(
            schema_version=1,
            simulation_time_us=self.clock.current_time_us,
            microstep=item.key.microstep,
            checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
            last_global_event_sequence=last_marker.global_event_sequence,
            event_prefix_last_global_sequence=last_marker.global_event_sequence,
            event_prefix_sha256=canonical_event_prefix_sha256(self._events),
            pending_work_count=len(pending),
            next_pending_time_us=(None if not pending else pending[0].key.simulation_time_us),
            next_pending_microstep=(None if not pending else pending[0].key.microstep),
            due_work_at_or_before_cut=0,
            generated_microsteps_complete=True,
            checkpoint_stage_complete=True,
            boundary_complete_at_cut=boundary_complete,
        )
        self._quiescent_cuts.append(cut)
        self._checkpoint_completed_count += 1

    def _scheduler_checkpoint_union(self) -> dict[str, object]:
        if self.agent_scheduler is None:
            return {
                "absent_reason": ABSENT_REASON_COMPONENT_INACTIVE,
                "status": "ABSENT",
            }
        state_method = getattr(self.agent_scheduler, "checkpoint_state", None)
        if not callable(state_method):
            raise RuntimeError("active scheduler has no checkpoint state")
        state = state_method()
        if not isinstance(state, Mapping):
            raise RuntimeError("active scheduler checkpoint is not an object")
        return {
            "state": _plain(state),
            "state_sha256": canonical_sha256(state),
            "status": "PRESERVED",
        }

    @staticmethod
    def _component_presence_inventory(
        plan: FullDayPlanV1, *, scheduler_present: bool
    ) -> list[dict[str, object]]:
        matrix = executable_agent_mechanics_composition_matrix()
        profile = matrix.profile(
            plan.composition_profile.reference_id,
            plan.composition_profile.version,
        )
        predicate_values = profile.predicate_values_for_plan_bindings(
            tuple(
                sorted(
                    {
                        binding.component_id
                        for binding in plan.component_configurations
                    }
                )
            ),
            participant_schedule_nonempty=bool(plan.participant_schedule),
            any_participant_initially_active=any(
                participant.initially_active
                for participant in plan.participant_definitions
            ),
        )
        active = set(profile.resolve_active_components(predicate_values))
        if (AGENT_SCHEDULER_COMPONENT in active) != scheduler_present:
            raise ValueError(
                "scheduler presence differs from the composition active predicate"
            )
        rows: list[dict[str, object]] = []
        for component in profile.components:
            if component.component_id in active:
                rows.append(
                    {
                        "component_id": component.component_id,
                        "reason": None,
                        "status": "PRESERVED",
                    }
                )
            else:
                rows.append(
                    {
                        "component_id": component.component_id,
                        "reason": profile.absence_reason_code(
                            component.component_id, predicate_values
                        ),
                        "status": "ABSENT",
                    }
                )
        for component_id in profile.refused_component_ids:
            rows.append(
                {
                    "component_id": component_id,
                    "reason": profile.absence_reason_code(
                        component_id, predicate_values
                    ),
                    "status": "ABSENT",
                }
            )
        rows.sort(key=lambda row: str(row["component_id"]))
        return rows

    def checkpoint_state(self) -> dict[str, object]:
        """Return the complete quiescent E1 runtime state.

        This is the composed runtime envelope used by fresh-process restore.  It
        is intentionally distinct from the runtime-owner adapter projection:
        engine and scheduler state appear here once as dependency-bound child
        records and are not claimed as fields owned by the runtime component.
        """

        self.assert_invariants()
        if self._executing is not None or self._state_emission_buffer:
            raise RuntimeError("runtime checkpoint requires a quiescent work boundary")
        cut = self.latest_quiescent_cut
        if (
            cut is None
            or cut.simulation_time_us != self.clock.current_time_us
            or cut.last_global_event_sequence != len(self._events)
            or (self._heap and self._heap[0][0][0] <= self.clock.current_time_us)
        ):
            raise RuntimeError("runtime checkpoint requires a marker-complete quiescent cut")
        _validate_checkpoint_cut_inventory(
            cuts=tuple(self._quiescent_cuts),
            events=tuple(self._events),
            executed=tuple(item.key for item in self._executed_work.values()),
            pending=tuple(self._pending.values()),
            current_time_us=self.clock.current_time_us,
            require_current_cut=True,
        )
        state: dict[str, object] = {
            "agent_scheduler": self._scheduler_checkpoint_union(),
            "agent_tokens": [
                {"time_us": time_us, "work_type": work_type}
                for work_type, time_us in sorted(self._agent_tokens)
            ],
            "calendar_boundary_index": self._calendar_boundary_index,
            "checkpoint_controller": {
                "allocated_request_ids": list(self._allocated_checkpoint_request_ids),
                "completed_count": self._checkpoint_completed_count,
                "next_request_sequence": self._checkpoint_request_next_sequence,
                "quiescent_cuts": [item.as_dict() for item in self._quiescent_cuts],
            },
            "clock": self.clock.checkpoint_state(),
            "component_sequences": dict(sorted(self._component_sequences.items())),
            "component_presence": self._component_presence_inventory(
                self.plan, scheduler_present=self.agent_scheduler is not None
            ),
            "dequeued_count": self._dequeued_count,
            "engine": self.engine.checkpoint_state(),
            "engine_state_sha256": canonical_sha256(self.engine.checkpoint_state()),
            "events": [event.as_dict() for event in self._events],
            "executed_work": [
                item.as_dict()
                for item in sorted(
                    self._executed_work.values(),
                    key=lambda value: value.key.ordering_key,
                )
            ],
            "global_event_next_sequence": self._next_global_event_sequence,
            "halt_reopen_state": {
                "halt_count": self._halt_count,
                "halt_entered_time_us": self._halt_entered_time_us,
                "maximum_resume_deadline_us": self._maximum_resume_deadline_us,
                "minimum_resume_eligible_time_us": self._minimum_resume_eligible_time_us,
                "reopening_auction_end_time_us": self._reopening_auction_end_time_us,
            },
            "implementation_version": FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION,
            "mechanics_event_cursor": self._mechanics_event_cursor,
            "native_ledger": [
                entry.as_dict()
                for entry in sorted(
                    self._native_ledger.values(),
                    key=lambda value: (
                        value.reference.owner_component_id,
                        value.reference.native_ledger_id,
                        value.reference.local_sequence,
                    ),
                )
            ],
            "native_sequences": dict(sorted(self._native_sequences.items())),
            "order_id_allocator": self._order_id_allocator.checkpoint_state(),
            "participant_schedule_index": self._participant_schedule_index,
            "pending_work": [item.as_dict() for item in self.pending_work],
            "plan": self.plan.as_dict(),
            "plan_sha256": self.plan.semantic_sha256,
            "profile_id": FULL_DAY_RUNTIME_PROFILE_ID,
            "profile_version": FULL_DAY_RUNTIME_PROFILE_VERSION,
            "retired_work": [
                item.as_dict()
                for item in sorted(
                    self._retired_work.values(),
                    key=lambda value: value.key.ordering_key,
                )
            ],
            "scheduled_event_index": self._scheduled_event_index,
            "schema_version": FULL_DAY_RUNTIME_CHECKPOINT_SCHEMA_VERSION,
            "state_runtime": self._state_runtime.state().as_dict(),
            "state_scheduled_time": self._state_scheduled_time,
        }
        validate_strict_json(state)
        if (
            len(canonical_json_bytes(state))
            > self.plan.deterministic_limits.maximum_checkpoint_bytes
        ):
            raise RuntimeError(
                "full-day checkpoint exceeds the plan deterministic byte limit"
            )
        return state

    def runtime_owner_checkpoint_state(self) -> dict[str, object]:
        """Project only state owned by ``FULL_DAY_RUNTIME_V1``.

        The complete composed checkpoint deliberately carries mechanics and
        scheduler children for convenient fresh-process restore.  A component
        adapter must not claim either child's state, so this projection removes
        those records and their mechanics digest while retaining the runtime's
        own clock, queue, cursors, allocators, outer ledger, and plan identity.
        """

        state = self.checkpoint_state()
        for field in ("agent_scheduler", "engine", "engine_state_sha256"):
            del state[field]
        validate_strict_json(state)
        return state

    @classmethod
    def validate_runtime_owner_checkpoint_state(
        cls, payload: Mapping[str, object]
    ) -> None:
        """Validate an engine-free runtime-owner adapter snapshot."""

        if not isinstance(payload, Mapping):
            raise TypeError("runtime-owner checkpoint must be an object")
        validate_strict_json(payload)
        expected = {
            "agent_tokens",
            "calendar_boundary_index",
            "checkpoint_controller",
            "clock",
            "component_presence",
            "component_sequences",
            "dequeued_count",
            "events",
            "executed_work",
            "global_event_next_sequence",
            "halt_reopen_state",
            "implementation_version",
            "mechanics_event_cursor",
            "native_ledger",
            "native_sequences",
            "order_id_allocator",
            "participant_schedule_index",
            "pending_work",
            "plan",
            "plan_sha256",
            "profile_id",
            "profile_version",
            "retired_work",
            "scheduled_event_index",
            "schema_version",
            "state_runtime",
            "state_scheduled_time",
        }
        _require_exact_fields(payload, expected, "runtime-owner checkpoint")
        if (
            payload["schema_version"] != FULL_DAY_RUNTIME_CHECKPOINT_SCHEMA_VERSION
            or payload["implementation_version"]
            != FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION
            or payload["profile_id"] != FULL_DAY_RUNTIME_PROFILE_ID
            or payload["profile_version"] != FULL_DAY_RUNTIME_PROFILE_VERSION
        ):
            raise ValueError("runtime-owner checkpoint version/profile is unsupported")
        raw_plan = _plain_object(payload["plan"], "runtime-owner plan")
        plan = FullDayPlanV1.from_dict(raw_plan)
        if plan.as_dict() != raw_plan or payload["plan_sha256"] != plan.semantic_sha256:
            raise ValueError("runtime-owner checkpoint plan identity mismatch")
        expected_presence = cls._component_presence_inventory(
            plan,
            scheduler_present=agent_scheduler_is_active(
                participant_schedule_nonempty=bool(plan.participant_schedule),
                any_participant_initially_active=any(
                    participant.initially_active
                    for participant in plan.participant_definitions
                ),
            ),
        )
        if payload["component_presence"] != expected_presence:
            raise ValueError(
                "runtime-owner component presence differs from composition"
            )
        matrix = executable_agent_mechanics_composition_matrix()
        if (
            plan.composition_profile.reference_id != FULL_DAY_RUNTIME_PROFILE_ID
            or plan.composition_profile.version != FULL_DAY_RUNTIME_PROFILE_VERSION
            or plan.composition_profile.sha256 != matrix.sha256
        ):
            raise ValueError("runtime-owner checkpoint has a nonexecutable composition")
        clock = SimulationClock.from_checkpoint_state(
            _plain_object(payload["clock"], "runtime-owner clock")
        )
        order_allocator = RuntimeOrderIdAllocatorV1.from_checkpoint_state(
            _plain_object(payload["order_id_allocator"], "runtime-owner order allocator")
        )
        state_runtime = HierarchicalStateRuntimeStateV1.from_dict(
            _plain_object(payload["state_runtime"], "runtime-owner state runtime")
        )
        if state_runtime.current_time_us != clock.current_time_us:
            raise ValueError("runtime-owner state time differs from its sole clock")
        raw_pending = payload["pending_work"]
        raw_executed = payload["executed_work"]
        raw_retired = payload["retired_work"]
        raw_events = payload["events"]
        raw_native = payload["native_ledger"]
        if type(raw_pending) is not list or any(
            not isinstance(row, Mapping) for row in raw_pending
        ):
            raise TypeError("runtime-owner pending work must be an object array")
        if type(raw_executed) is not list or any(
            not isinstance(row, Mapping) for row in raw_executed
        ):
            raise TypeError("runtime-owner executed work must be an object array")
        if type(raw_retired) is not list or any(
            not isinstance(row, Mapping) for row in raw_retired
        ):
            raise TypeError("runtime-owner retired work must be an object array")
        if type(raw_events) is not list or any(
            not isinstance(row, Mapping) for row in raw_events
        ):
            raise TypeError("runtime-owner events must be an object array")
        if type(raw_native) is not list or any(
            not isinstance(row, Mapping) for row in raw_native
        ):
            raise TypeError("runtime-owner native ledger must be an object array")
        pending = tuple(RuntimeWorkItemV1.from_dict(row) for row in raw_pending)
        executed = tuple(RuntimeWorkItemV1.from_dict(row) for row in raw_executed)
        retired = tuple(RuntimeWorkItemV1.from_dict(row) for row in raw_retired)
        events = tuple(FullDayEventV1.from_dict(row) for row in raw_events)
        native = tuple(NativeLedgerEntryV1.from_dict(row) for row in raw_native)
        pending_by_id = {item.work_id: item for item in pending}
        executed_by_id = {item.work_id: item for item in executed}
        retired_by_id = {item.work_id: item for item in retired}
        executed_keys_by_id = {
            item.work_id: item.key for item in executed
        }
        native_by_id = {item.ledger_key: item for item in native}
        if (
            len(pending_by_id) != len(pending)
            or len(executed_by_id) != len(executed)
            or len(retired_by_id) != len(retired)
            or len(native_by_id) != len(native)
            or set(pending_by_id) & set(executed_by_id)
            or set(pending_by_id) & set(retired_by_id)
            or set(executed_by_id) & set(retired_by_id)
        ):
            raise ValueError("runtime-owner checkpoint identities are duplicated")
        if pending != tuple(sorted(pending, key=lambda item: item.key.ordering_key)):
            raise ValueError("runtime-owner pending work is not canonically ordered")
        if executed != tuple(
            sorted(executed, key=lambda item: item.key.ordering_key)
        ):
            raise ValueError("runtime-owner executed work is not canonically ordered")
        if retired != tuple(
            sorted(retired, key=lambda item: item.key.ordering_key)
        ):
            raise ValueError("runtime-owner retired work is not canonically ordered")
        for item in executed:
            _validate_runtime_work_contract(item)
        for item in retired:
            _validate_runtime_work_contract(item)
        for item in pending:
            if (
                item.key.simulation_time_us < clock.current_time_us
                or item.key.simulation_time_us > plan.calendar.end_time_us
            ):
                raise ValueError("runtime-owner pending work lies outside its horizon")
        published_scheduled_ids = {
            str(event.payload.data["scheduled_event_id"])
            for event in events
            if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
        }
        scheduled_by_id = {
            event.event_id: event for event in plan.scheduled_events
        }
        validate_full_day_event_stream(
            events,
            executed_work_items=executed_keys_by_id,
            native_event_ledger=native_by_id,
            scheduled_event_ledger={
                event_id: scheduled_by_id[event_id]
                for event_id in sorted(published_scheduled_ids)
            },
            full_day_plan=plan,
        )
        _validate_state_runtime_replay(
            plan=plan,
            state=state_runtime,
            events=events,
        )
        _validate_pending_state_work(
            plan=plan,
            state=state_runtime,
            state_scheduled_time=_exact_optional_int(
                payload["state_scheduled_time"],
                "runtime-owner state scheduled time",
            ),
            pending=pending,
        )
        if _exact_int(
            payload["global_event_next_sequence"],
            "runtime-owner global event sequence",
            minimum=1,
        ) != len(events) + 1:
            raise ValueError("runtime-owner global allocator differs from its prefix")
        if _exact_int(
            payload["dequeued_count"], "runtime-owner dequeued count"
        ) != len(executed):
            raise ValueError("runtime-owner dequeued count differs from executed work")

        scheduler_active = agent_scheduler_is_active(
            participant_schedule_nonempty=bool(plan.participant_schedule),
            any_participant_initially_active=any(
                participant.initially_active
                for participant in plan.participant_definitions
            ),
        )
        active_components = {
            FULL_DAY_RUNTIME_COMPONENT,
            MECHANICS_COMPONENT,
            *(
                (AGENT_SCHEDULER_COMPONENT,)
                if scheduler_active
                else ()
            ),
        }
        expected_component_sequences = {
            component_id: 0 for component_id in active_components
        }
        allowed_owner_stages = {
            (owner, stage)
            for owner, stages, _fields in _WORK_CONTRACTS.values()
            for stage in stages
        }
        for event in events:
            if event.source_component_id not in active_components:
                raise ValueError("runtime-owner event cites an inactive component")
            expected_component_sequences[event.source_component_id] = max(
                expected_component_sequences[event.source_component_id],
                event.component_local_sequence,
            )
        for item in pending:
            if item.key.source_component_id not in active_components:
                raise ValueError("runtime-owner pending work cites an inactive component")
            expected_component_sequences[item.key.source_component_id] = max(
                expected_component_sequences[item.key.source_component_id],
                item.key.component_local_sequence,
            )
        for item in executed:
            key = item.key
            if (
                key.source_component_id not in active_components
                or (key.source_component_id, key.stage_ordinal)
                not in allowed_owner_stages
            ):
                raise ValueError("runtime-owner executed work has no valid owner/stage")
            expected_component_sequences[key.source_component_id] = max(
                expected_component_sequences[key.source_component_id],
                key.component_local_sequence,
            )
        for item in retired:
            key = item.key
            if (
                key.source_component_id not in active_components
                or (key.source_component_id, key.stage_ordinal)
                not in allowed_owner_stages
            ):
                raise ValueError("runtime-owner retired work has no valid owner/stage")
            expected_component_sequences[key.source_component_id] = max(
                expected_component_sequences[key.source_component_id],
                key.component_local_sequence,
            )
        raw_component_sequences = _plain_object(
            payload["component_sequences"], "runtime-owner component sequences"
        )
        component_sequences = {
            _identifier(key, "runtime-owner component sequence key"): _exact_int(
                value, f"runtime-owner component sequence {key}"
            )
            for key, value in raw_component_sequences.items()
        }
        if (
            state_runtime.component_local_sequence
            != expected_component_sequences[FULL_DAY_RUNTIME_COMPONENT]
            or component_sequences != expected_component_sequences
        ):
            raise ValueError("runtime-owner component allocator highwaters differ")
        _validate_component_allocation_inventory(
            events=events,
            pending=pending,
            executed=executed,
            retired=retired,
            component_sequences=component_sequences,
        )

        expected_native_sequences = (
            {
                AGENT_SCHEDULER_COMPONENT: max(
                    (
                        entry.reference.local_sequence
                        for entry in native
                        if entry.reference.owner_component_id
                        == AGENT_SCHEDULER_COMPONENT
                    ),
                    default=0,
                )
            }
            if scheduler_active
            else {}
        )
        raw_native_sequences = _plain_object(
            payload["native_sequences"], "runtime-owner native sequences"
        )
        if raw_native_sequences != expected_native_sequences:
            raise ValueError("runtime-owner native allocator highwaters differ")
        mechanics_native_count = sum(
            entry.reference.owner_component_id == MECHANICS_COMPONENT
            for entry in native
        )
        if _exact_int(
            payload["mechanics_event_cursor"],
            "runtime-owner mechanics cursor",
        ) != mechanics_native_count:
            raise ValueError("runtime-owner mechanics cursor differs from native rows")

        allocated_fd_sequences = tuple(
            int(match.group(1))
            for entry in native
            if entry.reference.owner_component_id == AGENT_SCHEDULER_COMPONENT
            and entry.reference.event_type == "AGENTTRUTHEVENT"
            and (match := _RUNTIME_ORDER_ID_RE.fullmatch(
                str(entry.payload.get("order_id"))
            ))
            is not None
        )
        if (
            len(allocated_fd_sequences) != len(set(allocated_fd_sequences))
            or order_allocator.next_sequence
            != max(allocated_fd_sequences, default=0) + 1
        ):
            raise ValueError("runtime-owner order allocator highwater differs")

        cursor_specs = (
            (
                "calendar_boundary_index",
                tuple(
                    int(event.payload.data["boundary_operation_index"])
                    for event in events
                    if event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY
                ),
                tuple(range(len(plan.calendar.boundary_operations))),
            ),
            (
                "scheduled_event_index",
                tuple(
                    str(event.payload.data["scheduled_event_id"])
                    for event in events
                    if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
                ),
                tuple(event.event_id for event in plan.scheduled_events),
            ),
            (
                "participant_schedule_index",
                tuple(
                    str(event.payload.data["schedule_id"])
                    for event in events
                    if event.event_type
                    in {
                        FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
                        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
                        FullDayEventTypeV1.PARTICIPANT_RETUNED,
                    }
                ),
                tuple(entry.schedule_id for entry in plan.participant_schedule),
            ),
        )
        for field, observed_prefix, complete_inventory in cursor_specs:
            cursor = _exact_int(payload[field], f"runtime-owner {field}")
            if observed_prefix != complete_inventory[:cursor]:
                raise ValueError(f"runtime-owner {field} differs from emitted rows")
        _validate_halt_reopen_snapshot(
            plan=plan,
            scheduled_event_index=_exact_int(
                payload["scheduled_event_index"],
                "runtime-owner scheduled event index",
            ),
            pending=pending,
            halt_state=_plain_object(
                payload["halt_reopen_state"], "runtime-owner halt/reopen state"
            ),
            session_state=_mechanics_session_state_from_native_ledger(
                native,
                plan=plan,
                events=events,
            ),
        )
        _validate_plan_work_inventory(
            plan=plan,
            current_time_us=clock.current_time_us,
            calendar_boundary_index=_exact_int(
                payload["calendar_boundary_index"],
                "runtime-owner calendar boundary index",
            ),
            scheduled_event_index=_exact_int(
                payload["scheduled_event_index"],
                "runtime-owner scheduled event index",
            ),
            participant_schedule_index=_exact_int(
                payload["participant_schedule_index"],
                "runtime-owner participant schedule index",
            ),
            pending=pending,
            events=events,
        )

        raw_agent_tokens = payload["agent_tokens"]
        if type(raw_agent_tokens) is not list or any(
            not isinstance(row, Mapping) for row in raw_agent_tokens
        ):
            raise TypeError("runtime-owner agent tokens must be an object array")
        agent_tokens = {
            (
                _identifier(row.get("work_type"), "runtime-owner agent work type"),
                _exact_int(row.get("time_us"), "runtime-owner agent work time"),
            )
            for row in raw_agent_tokens
        }
        if len(agent_tokens) != len(raw_agent_tokens):
            raise ValueError("runtime-owner agent token rows are duplicated")
        expected_agent_tokens = {
            (item.work_type, item.key.simulation_time_us)
            for item in pending
            if item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
        }
        if agent_tokens != expected_agent_tokens:
            raise ValueError("runtime-owner agent tokens differ from pending work")
        if len(expected_agent_tokens) != sum(
            item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
            for item in pending
        ):
            raise ValueError("runtime-owner pending agent deadlines are duplicated")

        raw_controller = _plain_object(
            payload["checkpoint_controller"], "runtime-owner checkpoint controller"
        )
        _require_exact_fields(
            raw_controller,
            {
                "allocated_request_ids",
                "completed_count",
                "next_request_sequence",
                "quiescent_cuts",
            },
            "runtime-owner checkpoint controller",
        )
        raw_request_ids = raw_controller["allocated_request_ids"]
        raw_cuts = raw_controller["quiescent_cuts"]
        if type(raw_request_ids) is not list or any(
            type(value) is not str for value in raw_request_ids
        ):
            raise TypeError("runtime-owner checkpoint IDs must be a string array")
        if type(raw_cuts) is not list or any(
            not isinstance(row, Mapping) for row in raw_cuts
        ):
            raise TypeError("runtime-owner quiescent cuts must be an object array")
        request_ids = tuple(
            _identifier(value, "runtime-owner checkpoint request ID")
            for value in raw_request_ids
        )
        cuts = tuple(QuiescentCutV1.from_dict(row) for row in raw_cuts)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("runtime-owner checkpoint IDs are duplicated")
        generated_sequences = tuple(
            int(request_id.removeprefix("CHECKPOINT-"))
            for request_id in request_ids
            if request_id.startswith("CHECKPOINT-")
            and request_id.removeprefix("CHECKPOINT-").isdigit()
        )
        if _exact_int(
            raw_controller["next_request_sequence"],
            "runtime-owner checkpoint next sequence",
            minimum=1,
        ) != max(generated_sequences, default=0) + 1:
            raise ValueError("runtime-owner checkpoint allocator highwater differs")
        completed_ids = {
            str(event.payload.data["checkpoint_request_id"])
            for event in events
            if event.event_type is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
        }
        pending_ids = {
            str(request_id)
            for item in pending
            if item.work_type == _WORK_CHECKPOINT_CAPTURE
            for request_id in item.payload["checkpoint_request_ids"]
        }
        if (
            completed_ids & pending_ids
            or set(request_ids) != completed_ids | pending_ids
            or _exact_int(
                raw_controller["completed_count"],
                "runtime-owner checkpoint completed count",
            )
            != len(cuts)
        ):
            raise ValueError("runtime-owner checkpoint controller is inconsistent")
        _validate_checkpoint_cut_inventory(
            cuts=cuts,
            events=events,
            executed=tuple(item.key for item in executed),
            pending=pending,
            current_time_us=clock.current_time_us,
            require_current_cut=True,
        )

    @classmethod
    def restore_runtime_owner_checkpoint_state(
        cls, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Restore a detached engine-free owner shell for dependency wiring."""

        cls.validate_runtime_owner_checkpoint_state(payload)
        frozen = freeze_json(payload)
        if not isinstance(frozen, Mapping):  # pragma: no cover
            raise TypeError("runtime-owner checkpoint did not remain an object")
        return frozen

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    def state_sha256(self) -> str:
        return canonical_sha256(self.checkpoint_state())

    def event_stream_bytes(self) -> bytes:
        return canonical_json_bytes([event.as_dict() for event in self._events])

    def result_projection(self) -> dict[str, object]:
        scheduler = self._scheduler_checkpoint_union()
        return {
            "clock_time_us": self.clock.current_time_us,
            "engine_state_sha256": canonical_sha256(self.engine.checkpoint_state()),
            "event_count": len(self._events),
            "event_prefix_sha256": canonical_event_prefix_sha256(self._events),
            "global_event_next_sequence": self._next_global_event_sequence,
            "last_global_event_sequence": len(self._events),
            "order_allocator_next_sequence": self._order_id_allocator.next_sequence,
            "pending_work_count": len(self._pending),
            "pending_work_sha256": canonical_sha256(
                [item.as_dict() for item in self.pending_work]
            ),
            "profile_id": FULL_DAY_RUNTIME_PROFILE_ID,
            "profile_version": FULL_DAY_RUNTIME_PROFILE_VERSION,
            "scheduler_state_sha256": scheduler.get("state_sha256"),
            "scheduler_status": scheduler["status"],
        }

    @classmethod
    def from_checkpoint_state(
        cls, payload: Mapping[str, object]
    ) -> FullDayRuntime:
        """Restore all E1 owners in one fresh-process dependency order."""

        if not isinstance(payload, Mapping):
            raise TypeError("full-day runtime checkpoint must be an object")
        validate_strict_json(payload)
        expected = {
            "agent_scheduler",
            "agent_tokens",
            "calendar_boundary_index",
            "checkpoint_controller",
            "clock",
            "component_presence",
            "component_sequences",
            "dequeued_count",
            "engine",
            "engine_state_sha256",
            "events",
            "executed_work",
            "global_event_next_sequence",
            "halt_reopen_state",
            "implementation_version",
            "mechanics_event_cursor",
            "native_ledger",
            "native_sequences",
            "order_id_allocator",
            "participant_schedule_index",
            "pending_work",
            "plan",
            "plan_sha256",
            "profile_id",
            "profile_version",
            "retired_work",
            "scheduled_event_index",
            "schema_version",
            "state_runtime",
            "state_scheduled_time",
        }
        _require_exact_fields(payload, expected, "FullDayRuntime checkpoint")
        if (
            payload["schema_version"] != FULL_DAY_RUNTIME_CHECKPOINT_SCHEMA_VERSION
            or payload["implementation_version"]
            != FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION
            or payload["profile_id"] != FULL_DAY_RUNTIME_PROFILE_ID
            or payload["profile_version"] != FULL_DAY_RUNTIME_PROFILE_VERSION
        ):
            raise ValueError("full-day runtime checkpoint version/profile is unsupported")
        raw_plan = _plain_object(payload["plan"], "full-day plan")
        plan = FullDayPlanV1.from_dict(raw_plan)
        if plan.as_dict() != raw_plan or payload["plan_sha256"] != plan.semantic_sha256:
            raise ValueError("runtime checkpoint plan identity mismatch")
        if (
            len(canonical_json_bytes(payload))
            > plan.deterministic_limits.maximum_checkpoint_bytes
        ):
            raise ValueError(
                "full-day checkpoint exceeds the plan deterministic byte limit"
            )
        raw_engine = _plain_object(payload["engine"], "mechanics engine")
        engine = MarketMechanicsEngine.from_checkpoint_state(raw_engine)
        if payload["engine_state_sha256"] != canonical_sha256(raw_engine):
            raise ValueError("runtime checkpoint engine digest mismatch")
        raw_clock = _plain_object(payload["clock"], "runtime clock")
        if engine.clock.checkpoint_state() != raw_clock:
            raise ValueError("runtime and engine checkpoint clocks differ")
        raw_allocator = _plain_object(payload["order_id_allocator"], "order allocator")
        allocator = RuntimeOrderIdAllocatorV1.from_checkpoint_state(raw_allocator)

        raw_scheduler = _plain_object(payload["agent_scheduler"], "agent scheduler union")
        status = raw_scheduler.get("status")
        scheduler: object | None
        if status == "ABSENT":
            _require_exact_fields(
                raw_scheduler, {"absent_reason", "status"}, "absent agent scheduler"
            )
            if raw_scheduler["absent_reason"] != ABSENT_REASON_COMPONENT_INACTIVE:
                raise ValueError("agent scheduler absence reason is unsupported")
            scheduler = None
        elif status == "PRESERVED":
            _require_exact_fields(
                raw_scheduler,
                {"state", "state_sha256", "status"},
                "preserved agent scheduler",
            )
            raw_scheduler_state = _plain_object(
                raw_scheduler["state"], "agent scheduler state"
            )
            if raw_scheduler["state_sha256"] != canonical_sha256(raw_scheduler_state):
                raise ValueError("agent scheduler state digest mismatch")
            from kirby2.agents.ecology import AgentScheduler

            scheduler = AgentScheduler.from_checkpoint_state(
                raw_scheduler_state,
                engine=engine,
                clock=engine.clock,
                order_id_allocator=allocator.allocate,
            )
        else:
            raise ValueError("agent scheduler checkpoint union status is unsupported")
        if payload["component_presence"] != cls._component_presence_inventory(
            plan, scheduler_present=scheduler is not None
        ):
            raise ValueError(
                "runtime checkpoint component presence differs from composition"
            )
        runtime = cls(
            plan,
            engine=engine,
            clock=engine.clock,
            agent_scheduler=scheduler,
            order_id_allocator=allocator,
            bootstrap=False,
            restoring=True,
        )

        raw_state_runtime = _plain_object(payload["state_runtime"], "state runtime")
        state_runtime_state = HierarchicalStateRuntimeStateV1.from_dict(raw_state_runtime)
        runtime._state_runtime = HierarchicalStateRuntimeV1.from_state(
            plan,
            state_runtime_state,
            verified_component_local_sequence_floor=(
                state_runtime_state.component_local_sequence
            ),
        )
        raw_sequences = _plain_object(payload["component_sequences"], "component sequences")
        runtime._component_sequences = {
            _identifier(key, "component sequence owner"): _exact_int(
                value, f"component sequence {key}"
            )
            for key, value in raw_sequences.items()
        }
        raw_native_sequences = _plain_object(payload["native_sequences"], "native sequences")
        runtime._native_sequences = {
            _identifier(key, "native sequence owner"): _exact_int(
                value, f"native sequence {key}"
            )
            for key, value in raw_native_sequences.items()
        }
        raw_events = payload["events"]
        if type(raw_events) is not list or any(not isinstance(row, Mapping) for row in raw_events):
            raise TypeError("runtime outer events must be an object array")
        runtime._events = [FullDayEventV1.from_dict(row) for row in raw_events]
        runtime._next_global_event_sequence = _exact_int(
            payload["global_event_next_sequence"],
            "global_event_next_sequence",
            minimum=1,
        )
        raw_native = payload["native_ledger"]
        if type(raw_native) is not list or any(not isinstance(row, Mapping) for row in raw_native):
            raise TypeError("runtime native ledger must be an object array")
        native_entries = [NativeLedgerEntryV1.from_dict(row) for row in raw_native]
        runtime._native_ledger = {entry.ledger_key: entry for entry in native_entries}
        if len(runtime._native_ledger) != len(native_entries):
            raise ValueError("runtime native ledger contains duplicate identities")
        raw_pending = payload["pending_work"]
        if type(raw_pending) is not list or any(not isinstance(row, Mapping) for row in raw_pending):
            raise TypeError("runtime pending work must be an object array")
        pending = [RuntimeWorkItemV1.from_dict(row) for row in raw_pending]
        runtime._pending = {item.work_id: item for item in pending}
        if len(runtime._pending) != len(pending):
            raise ValueError("runtime pending work contains duplicate identities")
        runtime._heap = [(item.key.ordering_key, item.work_id) for item in pending]
        heapq.heapify(runtime._heap)
        raw_executed = payload["executed_work"]
        if type(raw_executed) is not list or any(not isinstance(row, Mapping) for row in raw_executed):
            raise TypeError("runtime executed work must be an object array")
        executed_items = [RuntimeWorkItemV1.from_dict(row) for row in raw_executed]
        runtime._executed_work = {
            item.work_id: item for item in executed_items
        }
        if len(runtime._executed_work) != len(executed_items):
            raise ValueError("runtime executed work contains duplicate identities")
        raw_retired = payload["retired_work"]
        if type(raw_retired) is not list or any(
            not isinstance(row, Mapping) for row in raw_retired
        ):
            raise TypeError("runtime retired work must be an object array")
        retired_items = [RuntimeWorkItemV1.from_dict(row) for row in raw_retired]
        runtime._retired_work = {
            item.work_id: item for item in retired_items
        }
        if len(runtime._retired_work) != len(retired_items):
            raise ValueError("runtime retired work contains duplicate identities")
        runtime._dequeued_count = _exact_int(payload["dequeued_count"], "dequeued_count")
        runtime._last_completed_key = (
            None
            if not executed_items
            else max(
                (item.key for item in executed_items),
                key=lambda key: key.ordering_key,
            )
        )
        runtime._mechanics_event_cursor = _exact_int(
            payload["mechanics_event_cursor"], "mechanics_event_cursor"
        )
        runtime._calendar_boundary_index = _exact_int(
            payload["calendar_boundary_index"], "calendar_boundary_index"
        )
        runtime._participant_schedule_index = _exact_int(
            payload["participant_schedule_index"], "participant_schedule_index"
        )
        runtime._scheduled_event_index = _exact_int(
            payload["scheduled_event_index"], "scheduled_event_index"
        )

        controller = _plain_object(payload["checkpoint_controller"], "checkpoint controller")
        _require_exact_fields(
            controller,
            {
                "allocated_request_ids",
                "completed_count",
                "next_request_sequence",
                "quiescent_cuts",
            },
            "checkpoint controller",
        )
        raw_ids = controller["allocated_request_ids"]
        raw_cuts = controller["quiescent_cuts"]
        if type(raw_ids) is not list or any(type(value) is not str for value in raw_ids):
            raise TypeError("allocated checkpoint request IDs must be an array")
        if type(raw_cuts) is not list or any(not isinstance(row, Mapping) for row in raw_cuts):
            raise TypeError("quiescent cuts must be an object array")
        runtime._allocated_checkpoint_request_ids = list(raw_ids)
        runtime._checkpoint_request_next_sequence = _exact_int(
            controller["next_request_sequence"], "next checkpoint request sequence", minimum=1
        )
        runtime._checkpoint_completed_count = _exact_int(
            controller["completed_count"], "checkpoint completed count"
        )
        runtime._quiescent_cuts = [QuiescentCutV1.from_dict(row) for row in raw_cuts]

        halt_state = _plain_object(payload["halt_reopen_state"], "halt/reopen state")
        _require_exact_fields(
            halt_state,
            {
                "halt_count",
                "halt_entered_time_us",
                "maximum_resume_deadline_us",
                "minimum_resume_eligible_time_us",
                "reopening_auction_end_time_us",
            },
            "halt/reopen state",
        )
        runtime._halt_count = _exact_int(halt_state["halt_count"], "halt_count")
        runtime._halt_entered_time_us = _exact_optional_int(
            halt_state["halt_entered_time_us"], "halt_entered_time_us"
        )
        runtime._maximum_resume_deadline_us = _exact_optional_int(
            halt_state["maximum_resume_deadline_us"], "maximum_resume_deadline_us"
        )
        runtime._minimum_resume_eligible_time_us = _exact_optional_int(
            halt_state["minimum_resume_eligible_time_us"], "minimum_resume_eligible_time_us"
        )
        runtime._reopening_auction_end_time_us = _exact_optional_int(
            halt_state["reopening_auction_end_time_us"], "reopening_auction_end_time_us"
        )
        raw_tokens = payload["agent_tokens"]
        if type(raw_tokens) is not list or any(not isinstance(row, Mapping) for row in raw_tokens):
            raise TypeError("agent tokens must be an object array")
        runtime._agent_tokens = {
            (
                _identifier(row.get("work_type"), "agent token work type"),
                _exact_int(row.get("time_us"), "agent token time"),
            )
            for row in raw_tokens
        }
        runtime._state_scheduled_time = _exact_optional_int(
            payload["state_scheduled_time"], "state_scheduled_time"
        )
        runtime._state_emission_buffer = ()
        runtime._state_emission_consumed = set()
        runtime._events_at_time = {}
        runtime._microsteps_at_time = {}
        for event in runtime._events:
            runtime._events_at_time[event.simulation_time_us] = (
                runtime._events_at_time.get(event.simulation_time_us, 0) + 1
            )
        for item in runtime._executed_work.values():
            key = item.key
            runtime._microsteps_at_time.setdefault(key.simulation_time_us, set()).add(
                key.microstep
            )
        runtime.assert_invariants()
        if canonical_json_bytes(runtime.checkpoint_state()) != canonical_json_bytes(payload):
            raise ValueError("full-day runtime checkpoint is not a canonical fixed point")
        return runtime

    @classmethod
    def from_canonical_state_bytes(cls, payload: bytes) -> FullDayRuntime:
        return cls.from_checkpoint_state(parse_canonical_json_object(payload))

    def assert_invariants(self) -> None:
        self._validate_profile_and_core_owners(restoring=True)
        active_component_ids = {
            FULL_DAY_RUNTIME_COMPONENT,
            MECHANICS_COMPONENT,
        }
        if self.agent_scheduler is not None:
            active_component_ids.add(AGENT_SCHEDULER_COMPONENT)
        if set(self._component_sequences) != active_component_ids:
            raise RuntimeError(
                "component allocator owners differ from the exact active profile"
            )
        if self._state_runtime.state().current_time_us != self.clock.current_time_us:
            raise RuntimeError("hierarchical state time differs from the sole runtime clock")
        if self._mechanics_event_cursor != len(self.engine.events):
            raise RuntimeError("mechanics events exist without outer-event ownership")
        if self._next_global_event_sequence != len(self._events) + 1:
            raise RuntimeError("global outer-event allocator is inconsistent")
        if [event.global_event_sequence for event in self._events] != list(
            range(1, len(self._events) + 1)
        ):
            raise RuntimeError("outer-event sequence is not contiguous")
        chronological = [event.chronological_key for event in self._events]
        if chronological != sorted(chronological):
            raise RuntimeError("outer-event prefix moves backward")
        owner_sequences: dict[str, int] = {}
        native_keys: set[tuple[str, str, str]] = set()
        for event in self._events:
            if event.source_component_id not in active_component_ids:
                raise RuntimeError("outer event cites an inactive or unknown owner")
            prior = owner_sequences.get(event.source_component_id, 0)
            if event.component_local_sequence <= prior:
                raise RuntimeError("outer component sequence did not increase")
            owner_sequences[event.source_component_id] = event.component_local_sequence
            native = event.payload.native_event
            if native is not None:
                if native.owner_component_id not in active_component_ids:
                    raise RuntimeError("native event cites an inactive or unknown owner")
                if native.ledger_key in native_keys:
                    raise RuntimeError("outer prefix duplicates native event identity")
                native_keys.add(native.ledger_key)
        if native_keys != set(self._native_ledger):
            raise RuntimeError("native ledger does not exactly cover outer references")
        for key, entry in self._native_ledger.items():
            if key != entry.ledger_key:
                raise RuntimeError("native ledger key differs from its entry")
            if entry.reference.owner_component_id not in active_component_ids:
                raise RuntimeError("native ledger contains an inactive or unknown owner")
        mechanics_native = tuple(
            sorted(
                (
                    entry
                    for entry in self._native_ledger.values()
                    if entry.reference.owner_component_id == MECHANICS_COMPONENT
                ),
                key=lambda entry: entry.reference.local_sequence,
            )
        )
        if len(mechanics_native) != len(self.engine.events):
            raise RuntimeError(
                "mechanics native ledger cardinality differs from engine events"
            )
        for entry, native in zip(
            mechanics_native, self.engine.events, strict=True
        ):
            reference = entry.reference
            if (
                reference.native_ledger_id != MECHANICS_NATIVE_LEDGER_ID
                or reference.local_sequence != native.sequence
                or reference.event_type != native.event_type.value
                or reference.event_id
                != f"MECHANICS_EVENT_{native.sequence:012d}"
                or _plain(entry.payload) != native.as_dict()
            ):
                raise RuntimeError(
                    "mechanics native ledger differs from the authoritative engine ledger"
                )
        self._assert_agent_native_reconciliation()
        self._assert_agent_deadline_replay()
        published_scheduled_ids = {
            str(event.payload.data["scheduled_event_id"])
            for event in self._events
            if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
        }
        scheduled_by_id = {
            event.event_id: event for event in self.plan.scheduled_events
        }
        validate_full_day_event_stream(
            self._events,
            executed_work_items={
                work_id: item.key
                for work_id, item in self._executed_work.items()
            },
            native_event_ledger=self._native_ledger,
            scheduled_event_ledger={
                event_id: scheduled_by_id[event_id]
                for event_id in sorted(published_scheduled_ids)
            },
            full_day_plan=self.plan,
        )
        try:
            _validate_state_runtime_replay(
                plan=self.plan,
                state=self._state_runtime.state(),
                events=tuple(self._events),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error
        if len(self._pending) != len(self._heap):
            raise RuntimeError("runtime heap/payload index count differs")
        heap_ids = [work_id for _ordering, work_id in self._heap]
        if len(heap_ids) != len(set(heap_ids)) or set(heap_ids) != set(self._pending):
            raise RuntimeError("runtime heap/payload identities differ")
        for ordering, work_id in self._heap:
            if self._pending[work_id].key.ordering_key != ordering:
                raise RuntimeError("runtime heap ordering key differs from payload")
        for item in self._pending.values():
            _validate_runtime_work_contract(item)
            if item.key.simulation_time_us > self.plan.calendar.end_time_us:
                raise RuntimeError("pending work exceeds the plan calendar")
        if (
            set(self._pending) & set(self._executed_work)
            or set(self._pending) & set(self._retired_work)
            or set(self._executed_work) & set(self._retired_work)
        ):
            raise RuntimeError(
                "runtime work identity appears in multiple lifecycle ledgers"
            )
        if self._dequeued_count != len(self._executed_work):
            raise RuntimeError("runtime dequeued counter is inconsistent")
        expected_component_sequences = {
            component_id: 0 for component_id in active_component_ids
        }
        for event in self._events:
            expected_component_sequences[event.source_component_id] = max(
                expected_component_sequences[event.source_component_id],
                event.component_local_sequence,
            )
        for item in self._pending.values():
            if item.key.source_component_id not in active_component_ids:
                raise RuntimeError("pending work cites an inactive or unknown owner")
            expected_component_sequences[item.key.source_component_id] = max(
                expected_component_sequences[item.key.source_component_id],
                item.key.component_local_sequence,
            )
        allowed_owner_stages = {
            (owner, stage)
            for owner, stages, _fields in _WORK_CONTRACTS.values()
            for stage in stages
        }
        for item in self._executed_work.values():
            try:
                _validate_runtime_work_contract(item)
            except (TypeError, ValueError) as error:
                raise RuntimeError(str(error)) from error
            key = item.key
            if key.source_component_id not in active_component_ids:
                raise RuntimeError("executed work cites an inactive or unknown owner")
            if (key.source_component_id, key.stage_ordinal) not in allowed_owner_stages:
                raise RuntimeError("executed work uses an unowned stage")
            expected_component_sequences[key.source_component_id] = max(
                expected_component_sequences[key.source_component_id],
                key.component_local_sequence,
            )
        for item in self._retired_work.values():
            try:
                _validate_runtime_work_contract(item)
            except (TypeError, ValueError) as error:
                raise RuntimeError(str(error)) from error
            key = item.key
            if key.source_component_id not in active_component_ids:
                raise RuntimeError("retired work cites an inactive or unknown owner")
            if (key.source_component_id, key.stage_ordinal) not in allowed_owner_stages:
                raise RuntimeError("retired work uses an unowned stage")
            expected_component_sequences[key.source_component_id] = max(
                expected_component_sequences[key.source_component_id],
                key.component_local_sequence,
            )
        if (
            self._state_runtime.component_local_sequence
            != expected_component_sequences[FULL_DAY_RUNTIME_COMPONENT]
            or self._component_sequences != expected_component_sequences
        ):
            raise RuntimeError(
                "component allocators do not equal the exact work/event highwater"
            )
        try:
            _validate_component_allocation_inventory(
                events=tuple(self._events),
                pending=tuple(self._pending.values()),
                executed=tuple(self._executed_work.values()),
                retired=tuple(self._retired_work.values()),
                component_sequences=self._component_sequences,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error
        if self._calendar_boundary_index > len(self.plan.calendar.boundary_operations):
            raise RuntimeError("calendar cursor exceeds the plan")
        if self._participant_schedule_index > len(self.plan.participant_schedule):
            raise RuntimeError("participant cursor exceeds the plan")
        if self._scheduled_event_index > len(self.plan.scheduled_events):
            raise RuntimeError("scheduled-event cursor exceeds the plan")
        calendar_prefix = tuple(
            int(event.payload.data["boundary_operation_index"])
            for event in self._events
            if event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY
        )
        if calendar_prefix != tuple(range(self._calendar_boundary_index)):
            raise RuntimeError("calendar cursor differs from its exact emitted prefix")
        scheduled_prefix = tuple(
            str(event.payload.data["scheduled_event_id"])
            for event in self._events
            if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
        )
        if scheduled_prefix != tuple(
            event.event_id
            for event in self.plan.scheduled_events[: self._scheduled_event_index]
        ):
            raise RuntimeError(
                "scheduled-event cursor differs from its exact emitted prefix"
            )
        processed_scheduled = self.plan.scheduled_events[
            : self._scheduled_event_index
        ]
        processed_halts = tuple(
            event
            for event in processed_scheduled
            if event.event_type
            in {
                ScheduledEventTypeV1.HALT,
                ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
            }
        )
        if self._halt_count != len(processed_halts):
            raise RuntimeError("halt counter differs from the exact scheduled prefix")
        reopen_work = tuple(
            item
            for item in self._pending.values()
            if item.work_type == _WORK_REOPEN_COMPLETE
        )
        transient_halt_state = (
            self._halt_entered_time_us,
            self._minimum_resume_eligible_time_us,
            self._maximum_resume_deadline_us,
            self._reopening_auction_end_time_us,
        )
        if self.engine.session_state in {
            SessionState.HALTED,
            SessionState.REOPENING_AUCTION,
        }:
            if not processed_halts:
                raise RuntimeError("halted session has no scheduled halt cause")
            halt_event = processed_halts[-1]
            halt_duration = self._scheduled_parameters(halt_event)[
                "halt_duration_us"
            ]
            expected_halt_state = (
                halt_event.simulation_time_us,
                halt_event.simulation_time_us
                + self.plan.halt_reopen_rules.minimum_halt_duration_us,
                min(
                    halt_event.simulation_time_us + halt_duration,
                    halt_event.simulation_time_us
                    + self.plan.halt_reopen_rules.maximum_halt_duration_us,
                ),
            )
            if transient_halt_state[:3] != expected_halt_state:
                raise RuntimeError(
                    "halt timing state differs from its exact scheduled cause"
                )
            if self.engine.session_state is SessionState.HALTED:
                if self._reopening_auction_end_time_us is not None or reopen_work:
                    raise RuntimeError("HALTED state carries reopening completion work")
            else:
                processed_reopens = tuple(
                    event
                    for event in processed_scheduled
                    if event.event_type is ScheduledEventTypeV1.REOPENING
                )
                if not processed_reopens:
                    raise RuntimeError("reopening session has no scheduled reopen cause")
                reopen_event = processed_reopens[-1]
                expected_end = reopen_event.simulation_time_us + (
                    self._scheduled_parameters(reopen_event)[
                        "reopening_auction_duration_us"
                    ]
                )
                if (
                    self._reopening_auction_end_time_us != expected_end
                    or len(reopen_work) != 1
                    or reopen_work[0].key.simulation_time_us != expected_end
                    or reopen_work[0].payload["scheduled_event_id"]
                    != reopen_event.event_id
                ):
                    raise RuntimeError(
                        "reopening state differs from its exact completion work"
                    )
        elif any(value is not None for value in transient_halt_state) or reopen_work:
            raise RuntimeError("non-halted session retains transient halt/reopen state")
        lifecycle_types = {
            FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
            FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
            FullDayEventTypeV1.PARTICIPANT_RETUNED,
        }
        participant_prefix = tuple(
            str(event.payload.data["schedule_id"])
            for event in self._events
            if event.event_type in lifecycle_types
        )
        if participant_prefix != tuple(
            entry.schedule_id
            for entry in self.plan.participant_schedule[
                : self._participant_schedule_index
            ]
        ):
            raise RuntimeError(
                "participant cursor differs from its exact emitted prefix"
            )
        try:
            _validate_plan_work_inventory(
                plan=self.plan,
                current_time_us=self.clock.current_time_us,
                calendar_boundary_index=self._calendar_boundary_index,
                scheduled_event_index=self._scheduled_event_index,
                participant_schedule_index=self._participant_schedule_index,
                pending=tuple(self._pending.values()),
                events=tuple(self._events),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error
        allocated_generated_sequences = tuple(
            int(request_id.removeprefix("CHECKPOINT-"))
            for request_id in self._allocated_checkpoint_request_ids
            if request_id.startswith("CHECKPOINT-")
            and request_id.removeprefix("CHECKPOINT-").isdigit()
        )
        if self._checkpoint_request_next_sequence != (
            max(allocated_generated_sequences, default=0) + 1
        ):
            raise RuntimeError(
                "checkpoint request allocator differs from its exact inventory highwater"
            )
        if len(self._allocated_checkpoint_request_ids) != len(set(self._allocated_checkpoint_request_ids)):
            raise RuntimeError("checkpoint request IDs are duplicated")
        if any(
            _identifier(request_id, "checkpoint request ID") != request_id
            for request_id in self._allocated_checkpoint_request_ids
        ):
            raise RuntimeError("checkpoint request ID inventory is noncanonical")
        completed_checkpoint_ids = {
            str(event.payload.data["checkpoint_request_id"])
            for event in self._events
            if event.event_type is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
        }
        pending_checkpoint_ids = {
            str(request_id)
            for item in self._pending.values()
            if item.work_type == _WORK_CHECKPOINT_CAPTURE
            for request_id in item.payload["checkpoint_request_ids"]
        }
        if completed_checkpoint_ids & pending_checkpoint_ids:
            raise RuntimeError("checkpoint request is both pending and completed")
        if set(self._allocated_checkpoint_request_ids) != (
            completed_checkpoint_ids | pending_checkpoint_ids
        ):
            raise RuntimeError(
                "allocated checkpoint IDs differ from pending/completed evidence"
            )
        expected_native_sequences = (
            {
                AGENT_SCHEDULER_COMPONENT: max(
                    (
                        entry.reference.local_sequence
                        for entry in self._native_ledger.values()
                        if entry.reference.owner_component_id
                        == AGENT_SCHEDULER_COMPONENT
                    ),
                    default=0,
                )
            }
            if self.agent_scheduler is not None
            else {}
        )
        if self._native_sequences != expected_native_sequences:
            raise RuntimeError(
                "native sequence allocators do not equal their exact ledger highwater"
            )
        allocated_order_sequences = [
            int(match.group(1))
            for managed in self.engine.orders
            if (match := _RUNTIME_ORDER_ID_RE.fullmatch(managed.request.order_id))
            is not None
        ]
        if (
            len(allocated_order_sequences) != len(set(allocated_order_sequences))
            or self._order_id_allocator.next_sequence
            != max(allocated_order_sequences, default=0) + 1
        ):
            raise RuntimeError(
                "runtime order allocator does not equal its exact managed-order highwater"
            )
        if self._checkpoint_completed_count != len(self._quiescent_cuts):
            raise RuntimeError("checkpoint completion counter is inconsistent")
        try:
            _validate_checkpoint_cut_inventory(
                cuts=tuple(self._quiescent_cuts),
                events=tuple(self._events),
                executed=tuple(
                    item.key for item in self._executed_work.values()
                ),
                pending=tuple(self._pending.values()),
                current_time_us=self.clock.current_time_us,
                require_current_cut=False,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error
        try:
            _validate_pending_state_work(
                plan=self.plan,
                state=self._state_runtime.state(),
                state_scheduled_time=self._state_scheduled_time,
                pending=tuple(self._pending.values()),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error
        expected_agent_tokens = {
            (item.work_type, item.key.simulation_time_us)
            for item in self._pending.values()
            if item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
        }
        if self._agent_tokens != expected_agent_tokens:
            raise RuntimeError("agent scheduling tokens differ from pending scheduler work")
        pending_agent_work_count = sum(
            item.work_type in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
            for item in self._pending.values()
        )
        if pending_agent_work_count != len(expected_agent_tokens):
            raise RuntimeError("scheduler work queue contains a duplicate deadline")
        for work_type, time_us in self._agent_tokens:
            if (
                work_type not in {_WORK_AGENT_ARRIVAL, _WORK_AGENT_DECISION}
                or type(time_us) is not int
                or time_us < self.clock.current_time_us
            ):
                raise RuntimeError("agent scheduling token is malformed or stale")
        if self.agent_scheduler is not None:
            scheduler_tokens = {
                (work_type, time_us)
                for work_type, time_us in (
                    (
                        _WORK_AGENT_ARRIVAL,
                        getattr(
                            self.agent_scheduler,
                            "next_pending_arrival_time_us",
                            None,
                        ),
                    ),
                    (
                        _WORK_AGENT_DECISION,
                        getattr(
                            self.agent_scheduler,
                            "next_decision_time_us",
                            None,
                        ),
                    ),
                )
                if time_us is not None
            }
            if scheduler_tokens != self._agent_tokens:
                raise RuntimeError(
                    "runtime agent work tokens differ from scheduler-owned deadlines"
                )
            expected_active = {
                participant.participant_id: participant.initially_active
                for participant in self.plan.participant_definitions
            }
            for entry in self.plan.participant_schedule[
                : self._participant_schedule_index
            ]:
                if entry.action is ParticipantScheduleActionV1.ACTIVATE:
                    expected_active[entry.participant_id] = True
                elif entry.action is ParticipantScheduleActionV1.DEACTIVATE:
                    expected_active[entry.participant_id] = False
            if getattr(self.agent_scheduler, "_active", None) != expected_active:
                raise RuntimeError(
                    "scheduler activation state does not reconcile with the plan cursor"
                )
            scheduler_assert = getattr(self.agent_scheduler, "assert_invariants", None)
            if not callable(scheduler_assert):
                raise RuntimeError("active scheduler omits invariants")
            scheduler_assert()


__all__ = [
    "AGENT_NATIVE_LEDGER_ID",
    "FULL_DAY_RUNTIME_CHECKPOINT_SCHEMA_VERSION",
    "FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION",
    "FULL_DAY_RUNTIME_PROFILE_ID",
    "FULL_DAY_RUNTIME_PROFILE_VERSION",
    "FullDayRuntime",
    "MECHANICS_NATIVE_LEDGER_ID",
    "RuntimeOrderIdAllocatorV1",
    "RuntimeWorkItemV1",
]
