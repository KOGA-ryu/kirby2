"""Typed scheduled-information orchestration for the full-day runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import (
    FlowSideV1,
    FullDayPlanV1,
    ScheduledEventTypeV1,
    _require_exact_fields,
    canonical_json_bytes,
    validate_strict_json,
)


SCHEDULED_EVENT_RUNTIME_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScheduledEventApplicationV1:
    """One plan-bound scheduled event after its information publication."""

    event_id: str
    simulation_time_us: int
    event_type: ScheduledEventTypeV1
    side: FlowSideV1
    parameter_set_sha256: str
    parameters: tuple[tuple[str, int], ...]
    mechanics_action: str

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise ValueError("scheduled application requires an event ID")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("scheduled application time must be nonnegative")
        if type(self.event_type) is not ScheduledEventTypeV1:
            raise TypeError("scheduled application event type is invalid")
        if type(self.side) is not FlowSideV1:
            raise TypeError("scheduled application side is invalid")
        if (
            type(self.parameter_set_sha256) is not str
            or len(self.parameter_set_sha256) != 64
        ):
            raise ValueError("scheduled application parameter digest is invalid")
        if (
            type(self.parameters) is not tuple
            or any(
                type(name) is not str or type(value) is not int
                for name, value in self.parameters
            )
            or tuple(name for name, _value in self.parameters)
            != tuple(sorted({name for name, _value in self.parameters}))
        ):
            raise ValueError("scheduled application parameters are noncanonical")
        expected_action = {
            ScheduledEventTypeV1.HALT: "ENTER_HALT",
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION: "ENTER_HALT",
            ScheduledEventTypeV1.REOPENING: "BEGIN_REOPENING",
        }.get(self.event_type, "PUBLISH_INFORMATION")
        if self.mechanics_action != expected_action:
            raise ValueError("scheduled application mechanics action is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "mechanics_action": self.mechanics_action,
            "parameter_set_sha256": self.parameter_set_sha256,
            "parameters": [
                {"name": name, "value": value} for name, value in self.parameters
            ],
            "side": self.side.value,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ScheduledEventApplicationV1:
        _require_exact_fields(
            payload,
            {
                "event_id",
                "event_type",
                "mechanics_action",
                "parameter_set_sha256",
                "parameters",
                "side",
                "simulation_time_us",
            },
            "scheduled event application",
        )
        parameters = payload["parameters"]
        if type(parameters) is not list or any(
            not isinstance(row, Mapping) for row in parameters
        ):
            raise TypeError("scheduled application parameters must be objects")
        parsed: list[tuple[str, int]] = []
        for row in parameters:
            _require_exact_fields(row, {"name", "value"}, "scheduled parameter")
            name = row["name"]
            value = row["value"]
            if type(name) is not str or type(value) is not int:
                raise TypeError("scheduled application parameter fields are invalid")
            parsed.append((name, value))
        event_id = payload["event_id"]
        time_us = payload["simulation_time_us"]
        event_type = payload["event_type"]
        side = payload["side"]
        digest = payload["parameter_set_sha256"]
        action = payload["mechanics_action"]
        if (
            type(event_id) is not str
            or type(time_us) is not int
            or type(event_type) is not str
            or type(side) is not str
            or type(digest) is not str
            or type(action) is not str
        ):
            raise TypeError("scheduled application scalar fields are invalid")
        return cls(
            event_id=event_id,
            simulation_time_us=time_us,
            event_type=ScheduledEventTypeV1(event_type),
            side=FlowSideV1(side),
            parameter_set_sha256=digest,
            parameters=tuple(parsed),
            mechanics_action=action,
        )


class ScheduledEventRuntimeV1:
    """Maintain the exact applied prefix of the immutable event schedule."""

    def __init__(
        self,
        plan: FullDayPlanV1,
        *,
        next_index: int = 0,
        history: Sequence[ScheduledEventApplicationV1] = (),
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("scheduled event runtime requires FullDayPlanV1")
        self._plan = plan
        self.plan_sha256 = plan.semantic_sha256
        self.next_index = next_index
        self.history = list(history)
        self.assert_invariants()

    @classmethod
    def create(cls, plan: FullDayPlanV1) -> ScheduledEventRuntimeV1:
        return cls(plan)

    @property
    def applied_state(self) -> Mapping[str, Mapping[str, object]]:
        return {
            row.event_id: {
                "applied_time_us": row.simulation_time_us,
                "parameter_set_sha256": row.parameter_set_sha256,
                "scheduled_event_type": row.event_type.value,
                "side": row.side.value,
            }
            for row in self.history
        }

    def apply_due(
        self, event_id: str, *, simulation_time_us: int
    ) -> ScheduledEventApplicationV1:
        if self.next_index >= len(self._plan.scheduled_events):
            raise RuntimeError("scheduled event stream is already exhausted")
        event = self._plan.scheduled_events[self.next_index]
        if event.event_id != event_id:
            raise RuntimeError("scheduled event cursor is not canonical")
        if event.simulation_time_us != simulation_time_us:
            raise RuntimeError("scheduled event executed at the wrong time")
        application = ScheduledEventApplicationV1(
            event_id=event.event_id,
            simulation_time_us=event.simulation_time_us,
            event_type=event.event_type,
            side=event.side,
            parameter_set_sha256=event.parameter_set_sha256,
            parameters=tuple(
                (parameter.name, parameter.value) for parameter in event.parameters
            ),
            mechanics_action={
                ScheduledEventTypeV1.HALT: "ENTER_HALT",
                ScheduledEventTypeV1.VOLATILITY_INTERRUPTION: "ENTER_HALT",
                ScheduledEventTypeV1.REOPENING: "BEGIN_REOPENING",
            }.get(event.event_type, "PUBLISH_INFORMATION"),
        )
        self.history.append(application)
        self.next_index += 1
        self.assert_invariants()
        return application

    def assert_invariants(self) -> None:
        if self.plan_sha256 != self._plan.semantic_sha256:
            raise RuntimeError("scheduled event runtime belongs to another plan")
        if type(self.next_index) is not int or not 0 <= self.next_index <= len(
            self._plan.scheduled_events
        ):
            raise RuntimeError("scheduled event cursor exceeds the plan")
        if len(self.history) != self.next_index or any(
            type(row) is not ScheduledEventApplicationV1 for row in self.history
        ):
            raise RuntimeError("scheduled event history differs from its cursor")
        for event, application in zip(
            self._plan.scheduled_events[: self.next_index],
            self.history,
            strict=True,
        ):
            expected = ScheduledEventApplicationV1(
                event_id=event.event_id,
                simulation_time_us=event.simulation_time_us,
                event_type=event.event_type,
                side=event.side,
                parameter_set_sha256=event.parameter_set_sha256,
                parameters=tuple(
                    (parameter.name, parameter.value)
                    for parameter in event.parameters
                ),
                mechanics_action={
                    ScheduledEventTypeV1.HALT: "ENTER_HALT",
                    ScheduledEventTypeV1.VOLATILITY_INTERRUPTION: "ENTER_HALT",
                    ScheduledEventTypeV1.REOPENING: "BEGIN_REOPENING",
                }.get(event.event_type, "PUBLISH_INFORMATION"),
            )
            if application != expected:
                raise RuntimeError("scheduled event application differs from the plan")

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        state = {
            "history": [row.as_dict() for row in self.history],
            "next_index": self.next_index,
            "plan_sha256": self.plan_sha256,
            "schema_version": SCHEDULED_EVENT_RUNTIME_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls, payload: Mapping[str, object], *, plan: FullDayPlanV1
    ) -> ScheduledEventRuntimeV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {"history", "next_index", "plan_sha256", "schema_version"},
            "scheduled event runtime",
        )
        if payload["schema_version"] != SCHEDULED_EVENT_RUNTIME_SCHEMA_VERSION:
            raise ValueError("scheduled event runtime schema is unsupported")
        if payload["plan_sha256"] != plan.semantic_sha256:
            raise ValueError("scheduled event runtime plan identity mismatch")
        history = payload["history"]
        next_index = payload["next_index"]
        if type(history) is not list or any(
            not isinstance(row, Mapping) for row in history
        ):
            raise TypeError("scheduled event history must be an object array")
        if type(next_index) is not int:
            raise TypeError("scheduled event next index must be an integer")
        runtime = cls(
            plan,
            next_index=next_index,
            history=tuple(ScheduledEventApplicationV1.from_dict(row) for row in history),
        )
        if canonical_json_bytes(runtime.checkpoint_state()) != canonical_json_bytes(payload):
            raise ValueError("scheduled event checkpoint is not a canonical fixed point")
        return runtime

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())


__all__ = [
    "SCHEDULED_EVENT_RUNTIME_SCHEMA_VERSION",
    "ScheduledEventApplicationV1",
    "ScheduledEventRuntimeV1",
]
