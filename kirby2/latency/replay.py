"""Portable asynchronous command recordings and exact deterministic replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from kirby2.exchange import Side

from .engine import AsynchronousExecutionSession
from .models import LATENCY_RECORDING_SCHEMA_VERSION
from .profiles import LatencyProfile


@dataclass(frozen=True, slots=True)
class LatencyCommand:
    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("latency command sequence or time is invalid")
        if self.command_type not in {
            "LIMIT",
            "MARKET",
            "CANCEL",
            "REPLACE",
            "EXTERNAL_MARKET",
            "EXTERNAL_REPRICE",
        }:
            raise ValueError("unsupported latency replay command")

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": self.parameters,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LatencyCommand:
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("latency command parameters are invalid")
        return cls(
            int(payload["sequence"]),
            int(payload["simulation_time_us"]),
            str(payload["command_type"]),
            dict(parameters),
        )


@dataclass(frozen=True, slots=True)
class LatencyRecording:
    seed: int
    profile: dict[str, object]
    initial_bid_ticks: int
    initial_ask_ticks: int
    initial_queue_quantity: int
    commands: tuple[LatencyCommand, ...]
    expected_events: tuple[dict[str, object], ...]
    completed_time_us: int
    expected_event_stream_sha256: str
    expected_state_sha256: str
    schema_version: int = LATENCY_RECORDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LATENCY_RECORDING_SCHEMA_VERSION:
            raise ValueError("unsupported latency recording schema")
        sequences = tuple(command.sequence for command in self.commands)
        times = tuple(command.simulation_time_us for command in self.commands)
        if sequences != tuple(range(1, len(self.commands) + 1)):
            raise ValueError("latency recording command sequence is incomplete")
        if times != tuple(sorted(times)) or (
            times and times[-1] > self.completed_time_us
        ):
            raise ValueError("latency recording command time is invalid")
        event_sequences = tuple(
            int(event["sequence"]) for event in self.expected_events
        )
        event_times = tuple(
            int(event["simulation_time_us"]) for event in self.expected_events
        )
        if event_sequences != tuple(range(1, len(self.expected_events) + 1)):
            raise ValueError("latency recording event sequence is incomplete")
        if event_times != tuple(sorted(event_times)):
            raise ValueError("latency recording event time is not monotonic")
        if _event_stream_sha256(self.expected_events) != self.expected_event_stream_sha256:
            raise ValueError("latency recording event stream digest does not match")

    @classmethod
    def capture(
        cls,
        session: AsynchronousExecutionSession,
        commands: tuple[LatencyCommand, ...],
    ) -> LatencyRecording:
        session.assert_invariants()
        return cls(
            seed=session.seed,
            profile=session.profile.as_dict(),
            initial_bid_ticks=session.initial_bid_ticks,
            initial_ask_ticks=session.initial_ask_ticks,
            initial_queue_quantity=session.initial_queue_quantity,
            commands=commands,
            expected_events=tuple(event.as_dict() for event in session.events),
            completed_time_us=session.clock.current_time_us,
            expected_event_stream_sha256=session.event_stream_sha256(),
            expected_state_sha256=session.state_sha256(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": [command.as_dict() for command in self.commands],
            "completed_time_us": self.completed_time_us,
            "expected_event_stream_sha256": self.expected_event_stream_sha256,
            "expected_events": list(self.expected_events),
            "expected_state_sha256": self.expected_state_sha256,
            "initial_ask_ticks": self.initial_ask_ticks,
            "initial_bid_ticks": self.initial_bid_ticks,
            "initial_queue_quantity": self.initial_queue_quantity,
            "profile": self.profile,
            "schema_version": self.schema_version,
            "seed": self.seed,
        }

    def sha256(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LatencyRecording:
        profile = payload.get("profile")
        commands = payload.get("commands")
        expected_events = payload.get("expected_events")
        if (
            not isinstance(profile, dict)
            or not isinstance(commands, list)
            or not isinstance(expected_events, list)
        ):
            raise ValueError("latency recording profile or commands are invalid")
        if any(not isinstance(item, dict) for item in commands):
            raise ValueError("every latency recording command must be an object")
        if any(not isinstance(item, dict) for item in expected_events):
            raise ValueError("every recorded latency event must be an object")
        return cls(
            seed=int(payload["seed"]),
            profile=dict(profile),
            initial_bid_ticks=int(payload["initial_bid_ticks"]),
            initial_ask_ticks=int(payload["initial_ask_ticks"]),
            initial_queue_quantity=int(payload["initial_queue_quantity"]),
            commands=tuple(LatencyCommand.from_dict(item) for item in commands),
            expected_events=tuple(dict(item) for item in expected_events),
            completed_time_us=int(payload["completed_time_us"]),
            expected_event_stream_sha256=str(
                payload["expected_event_stream_sha256"]
            ),
            expected_state_sha256=str(payload["expected_state_sha256"]),
            schema_version=int(payload["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class LatencyReplayReport:
    recording: LatencyRecording
    session: AsynchronousExecutionSession
    event_stream_match: bool
    state_match: bool

    @property
    def passed(self) -> bool:
        return self.event_stream_match and self.state_match


def replay_latency_recording(recording: LatencyRecording) -> LatencyReplayReport:
    profile = LatencyProfile.from_dict(recording.profile)
    session = AsynchronousExecutionSession(
        seed=recording.seed,
        profile=profile,
        initial_bid_ticks=recording.initial_bid_ticks,
        initial_ask_ticks=recording.initial_ask_ticks,
        initial_queue_quantity=recording.initial_queue_quantity,
    )
    for command in recording.commands:
        session.advance_to(command.simulation_time_us)
        _apply_command(session, command)
    session.advance_to(recording.completed_time_us)
    session.assert_invariants()
    return LatencyReplayReport(
        recording,
        session,
        (
            tuple(event.as_dict() for event in session.events)
            == recording.expected_events
            and session.event_stream_sha256()
            == recording.expected_event_stream_sha256
        ),
        session.state_sha256() == recording.expected_state_sha256,
    )


def _apply_command(
    session: AsynchronousExecutionSession,
    command: LatencyCommand,
) -> None:
    values = command.parameters
    if command.command_type == "LIMIT":
        session.request_limit(
            Side(str(values["side"])),
            int(values["quantity"]),
            int(values["price_ticks"]),
            order_id=str(values["order_id"]),
        )
    elif command.command_type == "MARKET":
        session.request_market(
            Side(str(values["side"])),
            int(values["quantity"]),
            order_id=str(values["order_id"]),
        )
    elif command.command_type == "CANCEL":
        actual = session.request_cancel(str(values["target_order_id"]))
        if actual != str(values["cancel_id"]):
            raise RuntimeError("replayed cancel identity diverged")
    elif command.command_type == "REPLACE":
        session.request_replace(
            str(values["target_order_id"]),
            quantity=int(values["quantity"]),
            price_ticks=int(values["price_ticks"]),
        )
    elif command.command_type == "EXTERNAL_MARKET":
        session.schedule_aggressive_order(
            command.simulation_time_us,
            Side(str(values["side"])),
            int(values["quantity"]),
            order_id=str(values["order_id"]),
        )
    elif command.command_type == "EXTERNAL_REPRICE":
        session.schedule_liquidity_reprice(
            command.simulation_time_us,
            target_order_id=str(values["target_order_id"]),
            new_order_id=str(values["new_order_id"]),
            side=Side(str(values["side"])),
            quantity=int(values["quantity"]),
            price_ticks=int(values["price_ticks"]),
        )
    else:  # pragma: no cover - LatencyCommand validates this
        raise RuntimeError("unsupported latency replay command")


def _event_stream_sha256(events: tuple[dict[str, object], ...]) -> str:
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
