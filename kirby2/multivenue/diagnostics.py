"""Stable diagnostics derived from real fragmented-market event histories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.exchange import SessionState
from kirby2.immutable import freeze_json, thaw_json

from .models import (
    CoordinatorEvent,
    CoordinatorEventType,
    RouteLegExecution,
    VenueOrderStatus,
)


@dataclass(frozen=True, slots=True)
class MultiVenueDiagnostic:
    """A machine-readable finding emitted by a fragmented-market gate."""

    code: str
    gate: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.code or not self.gate:
            raise ValueError("multi-venue diagnostic requires a code and gate")
        frozen = freeze_json(self.evidence)
        if not isinstance(frozen, Mapping) or not frozen:
            raise ValueError("multi-venue diagnostic requires source evidence")
        object.__setattr__(self, "evidence", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "evidence": thaw_json(self.evidence),
            "gate": self.gate,
        }


def pending_order_halt_diagnostic(
    events: tuple[CoordinatorEvent, ...],
    *,
    route_id: str,
    execution: RouteLegExecution,
) -> MultiVenueDiagnostic | None:
    """Prove a scheduled route leg was halted before its real venue arrival."""

    if not route_id:
        raise ValueError("pending-order halt diagnostic requires a route ID")
    if not isinstance(execution, RouteLegExecution):
        raise TypeError("pending-order halt diagnostic requires RouteLegExecution")
    if any(not isinstance(event, CoordinatorEvent) for event in events):
        raise TypeError("pending-order halt diagnostic requires coordinator events")

    decision = _first_event(events, CoordinatorEventType.ROUTE_DECISION, route_id)
    scheduled = _first_event(events, CoordinatorEventType.ROUTE_LEG_SCHEDULED, route_id)
    rejected = _first_event(events, CoordinatorEventType.ROUTE_LEG_REJECTED, route_id)
    halt = next(
        (
            event
            for event in events
            if event.event_type is CoordinatorEventType.VENUE_SESSION_CHANGED
            and event.data.get("venue_id") == execution.venue_id
            and event.data.get("state") == SessionState.HALTED.value
        ),
        None,
    )
    if any(event is None for event in (decision, scheduled, halt, rejected)):
        return None
    assert decision is not None
    assert scheduled is not None
    assert halt is not None
    assert rejected is not None
    sequence = (decision.sequence, scheduled.sequence, halt.sequence, rejected.sequence)
    if sequence != tuple(sorted(sequence)) or len(set(sequence)) != len(sequence):
        return None
    if not (
        execution.status is VenueOrderStatus.REJECTED
        and execution.rejection_reason == "SESSION_HALTED"
        and scheduled.simulation_time_us
        <= halt.simulation_time_us
        < execution.arrival_time_us
    ):
        return None
    return MultiVenueDiagnostic(
        code="PENDING_ORDER_HALTED",
        gate="PendingRouteLegSessionGate",
        evidence={
            "arrival_time_us": execution.arrival_time_us,
            "event_sequences": list(sequence),
            "execution": execution.as_dict(),
            "halt_time_us": halt.simulation_time_us,
            "route_id": route_id,
            "scheduled_time_us": scheduled.simulation_time_us,
        },
    )


def _first_event(
    events: tuple[CoordinatorEvent, ...],
    event_type: CoordinatorEventType,
    route_id: str,
) -> CoordinatorEvent | None:
    return next(
        (
            event
            for event in events
            if event.event_type is event_type
            and _route_id(event.data) == route_id
        ),
        None,
    )


def _route_id(data: Mapping[str, object]) -> str | None:
    direct = data.get("route_id")
    if isinstance(direct, str):
        return direct
    decision = data.get("decision")
    if isinstance(decision, Mapping):
        nested = decision.get("route_id")
        return nested if isinstance(nested, str) else None
    return None
