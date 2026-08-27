"""Portable command recordings and exact replay for market mechanics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .mechanics_engine import MarketMechanicsEngine
from .mechanics_models import (
    MECHANICS_RECORDING_SCHEMA_VERSION,
    AdvancedOrderRequest,
    InstrumentRules,
    SessionState,
)


_COMMAND_TYPES = frozenset(
    {"TRANSITION", "SUBMIT", "CANCEL", "REPLACE", "UNCROSS"}
)


@dataclass(frozen=True, slots=True)
class MechanicsCommand:
    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("market-mechanics command identity is invalid")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("unsupported market-mechanics replay command")

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": self.parameters,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MechanicsCommand:
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("market-mechanics command parameters are invalid")
        return cls(
            int(payload["sequence"]),
            int(payload["simulation_time_us"]),
            str(payload["command_type"]),
            dict(parameters),
        )


@dataclass(frozen=True, slots=True)
class MechanicsRecording:
    rules: dict[str, object]
    commands: tuple[MechanicsCommand, ...]
    completed_time_us: int
    expected_events: tuple[dict[str, object], ...]
    expected_event_stream_sha256: str
    expected_state_sha256: str
    schema_version: int = MECHANICS_RECORDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MECHANICS_RECORDING_SCHEMA_VERSION:
            raise ValueError("unsupported market-mechanics recording schema")
        sequences = tuple(command.sequence for command in self.commands)
        times = tuple(command.simulation_time_us for command in self.commands)
        if sequences != tuple(range(1, len(self.commands) + 1)):
            raise ValueError("market-mechanics command sequence is incomplete")
        if times != tuple(sorted(times)) or (
            times and times[-1] > self.completed_time_us
        ):
            raise ValueError("market-mechanics command times are invalid")
        event_sequences = tuple(
            int(event["sequence"]) for event in self.expected_events
        )
        event_times = tuple(
            int(event["simulation_time_us"]) for event in self.expected_events
        )
        if event_sequences != tuple(range(1, len(self.expected_events) + 1)):
            raise ValueError("recorded market-mechanics event sequence is incomplete")
        if event_times != tuple(sorted(event_times)):
            raise ValueError("recorded market-mechanics event times are invalid")
        if event_times and event_times[-1] > self.completed_time_us:
            raise ValueError("recorded event occurs after recording completion")
        if _events_sha256(self.expected_events) != self.expected_event_stream_sha256:
            raise ValueError("recorded market-mechanics event digest does not match")

    @classmethod
    def capture(
        cls,
        engine: MarketMechanicsEngine,
        commands: tuple[MechanicsCommand, ...],
    ) -> MechanicsRecording:
        engine.assert_invariants()
        events = tuple(event.as_dict() for event in engine.events)
        return cls(
            rules=engine.rules.as_dict(),
            commands=commands,
            completed_time_us=engine.clock.current_time_us,
            expected_events=events,
            expected_event_stream_sha256=_events_sha256(events),
            expected_state_sha256=engine.state_sha256(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": [command.as_dict() for command in self.commands],
            "completed_time_us": self.completed_time_us,
            "expected_event_stream_sha256": self.expected_event_stream_sha256,
            "expected_events": list(self.expected_events),
            "expected_state_sha256": self.expected_state_sha256,
            "rules": self.rules,
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MechanicsRecording:
        raw_rules = payload.get("rules")
        raw_commands = payload.get("commands")
        raw_events = payload.get("expected_events")
        if (
            not isinstance(raw_rules, dict)
            or not isinstance(raw_commands, list)
            or not isinstance(raw_events, list)
            or any(not isinstance(item, dict) for item in raw_commands)
            or any(not isinstance(item, dict) for item in raw_events)
        ):
            raise ValueError("market-mechanics recording payload is invalid")
        return cls(
            rules=dict(raw_rules),
            commands=tuple(
                MechanicsCommand.from_dict(item) for item in raw_commands
            ),
            completed_time_us=int(payload["completed_time_us"]),
            expected_events=tuple(dict(item) for item in raw_events),
            expected_event_stream_sha256=str(
                payload["expected_event_stream_sha256"]
            ),
            expected_state_sha256=str(payload["expected_state_sha256"]),
            schema_version=int(payload["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class MechanicsReplayReport:
    recording: MechanicsRecording
    engine: MarketMechanicsEngine
    event_stream_match: bool
    state_match: bool

    @property
    def passed(self) -> bool:
        return self.event_stream_match and self.state_match


def replay_mechanics_recording(
    recording: MechanicsRecording,
) -> MechanicsReplayReport:
    engine = MarketMechanicsEngine(InstrumentRules.from_dict(recording.rules))
    for command in recording.commands:
        engine.advance_to(command.simulation_time_us)
        _apply_command(engine, command)
    engine.advance_to(recording.completed_time_us)
    engine.assert_invariants()
    actual_events = tuple(event.as_dict() for event in engine.events)
    return MechanicsReplayReport(
        recording,
        engine,
        actual_events == recording.expected_events
        and engine.event_stream_sha256()
        == recording.expected_event_stream_sha256,
        engine.state_sha256() == recording.expected_state_sha256,
    )


def _apply_command(
    engine: MarketMechanicsEngine,
    command: MechanicsCommand,
) -> None:
    values = command.parameters
    if command.command_type == "TRANSITION":
        engine.transition_session(
            SessionState(str(values["state"])),
            reason=str(values["reason"]),
        )
    elif command.command_type == "SUBMIT":
        request = values.get("request")
        if not isinstance(request, dict):
            raise ValueError("recorded advanced order request is invalid")
        engine.submit(AdvancedOrderRequest.from_dict(request))
    elif command.command_type == "CANCEL":
        engine.cancel(
            str(values["order_id"]),
            reason=str(values["reason"]),
        )
    elif command.command_type == "REPLACE":
        engine.replace_order(
            str(values["order_id"]),
            new_order_id=str(values["new_order_id"]),
            new_quantity=int(values["new_quantity"]),
            new_price_ticks=_optional_int(values.get("new_price_ticks")),
        )
    elif command.command_type == "UNCROSS":
        engine.uncross_auction()
    else:  # pragma: no cover - MechanicsCommand validates this
        raise RuntimeError("unsupported market-mechanics replay command")


def _events_sha256(events: tuple[dict[str, object], ...]) -> str:
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
