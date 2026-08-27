"""Portable exact replay for hidden-liquidity venue exercises."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from kirby2.exchange.models import OrderOwner, Side

from .models import (
    OBSERVABILITY_RECORDING_SCHEMA_VERSION,
    HiddenLiquidityRules,
    HiddenOrderRequest,
)
from .venue import HiddenLiquidityVenue


_COMMAND_TYPES = frozenset({"SUBMIT", "MARKET", "CANCEL", "REFRESH", "COMPLETE"})


@dataclass(frozen=True, slots=True)
class ObservabilityCommand:
    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if (
            type(self.sequence) is not int
            or self.sequence <= 0
            or type(self.simulation_time_us) is not int
            or self.simulation_time_us < 0
        ):
            raise ValueError("observability command identity is invalid")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("unsupported observability replay command")

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": self.parameters,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ObservabilityCommand:
        raw_parameters = payload.get("parameters")
        if not isinstance(raw_parameters, dict):
            raise ValueError("observability command parameters are invalid")
        return cls(
            _required_int(payload, "sequence"),
            _required_int(payload, "simulation_time_us"),
            str(payload["command_type"]),
            dict(raw_parameters),
        )


@dataclass(frozen=True, slots=True)
class ObservabilityRecording:
    rules: dict[str, object]
    commands: tuple[ObservabilityCommand, ...]
    completed_time_us: int
    expected_observable_feed: dict[str, object]
    expected_ground_truth: dict[str, object]
    expected_observable_sha256: str
    expected_truth_sha256: str
    expected_state_sha256: str
    schema_version: int = OBSERVABILITY_RECORDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVABILITY_RECORDING_SCHEMA_VERSION:
            raise ValueError("unsupported observability recording schema")
        sequences = tuple(command.sequence for command in self.commands)
        times = tuple(command.simulation_time_us for command in self.commands)
        if sequences != tuple(range(1, len(self.commands) + 1)):
            raise ValueError("observability command sequence is incomplete")
        if times != tuple(sorted(times)) or (
            times and times[-1] > self.completed_time_us
        ):
            raise ValueError("observability command times are invalid")
        if _sha256(self.expected_observable_feed) != self.expected_observable_sha256:
            raise ValueError("observable feed digest does not match recording")
        if _sha256(self.expected_ground_truth) != self.expected_truth_sha256:
            raise ValueError("ground-truth digest does not match recording")

    @classmethod
    def capture(
        cls,
        venue: HiddenLiquidityVenue,
        commands: tuple[ObservabilityCommand, ...],
    ) -> ObservabilityRecording:
        if not venue.complete:
            raise RuntimeError("observability recording requires a completed session")
        venue.assert_invariants()
        feed = venue.observable_feed().as_dict()
        truth = venue.post_session_ground_truth().as_dict()
        return cls(
            rules=venue.rules.as_dict(),
            commands=commands,
            completed_time_us=venue.clock.current_time_us,
            expected_observable_feed=feed,
            expected_ground_truth=truth,
            expected_observable_sha256=_sha256(feed),
            expected_truth_sha256=_sha256(truth),
            expected_state_sha256=venue.state_sha256(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": [command.as_dict() for command in self.commands],
            "completed_time_us": self.completed_time_us,
            "expected_ground_truth": self.expected_ground_truth,
            "expected_observable_feed": self.expected_observable_feed,
            "expected_observable_sha256": self.expected_observable_sha256,
            "expected_state_sha256": self.expected_state_sha256,
            "expected_truth_sha256": self.expected_truth_sha256,
            "rules": self.rules,
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return _sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ObservabilityRecording:
        raw_rules = payload.get("rules")
        raw_commands = payload.get("commands")
        raw_feed = payload.get("expected_observable_feed")
        raw_truth = payload.get("expected_ground_truth")
        if (
            not isinstance(raw_rules, dict)
            or not isinstance(raw_commands, list)
            or any(not isinstance(item, dict) for item in raw_commands)
            or not isinstance(raw_feed, dict)
            or not isinstance(raw_truth, dict)
        ):
            raise ValueError("observability recording payload is invalid")
        return cls(
            rules=dict(raw_rules),
            commands=tuple(
                ObservabilityCommand.from_dict(item) for item in raw_commands
            ),
            completed_time_us=_required_int(payload, "completed_time_us"),
            expected_observable_feed=dict(raw_feed),
            expected_ground_truth=dict(raw_truth),
            expected_observable_sha256=str(payload["expected_observable_sha256"]),
            expected_truth_sha256=str(payload["expected_truth_sha256"]),
            expected_state_sha256=str(payload["expected_state_sha256"]),
            schema_version=_required_int(payload, "schema_version"),
        )


@dataclass(frozen=True, slots=True)
class ObservabilityReplayReport:
    recording: ObservabilityRecording
    venue: HiddenLiquidityVenue
    observable_match: bool
    ground_truth_match: bool
    state_match: bool

    @property
    def passed(self) -> bool:
        return self.observable_match and self.ground_truth_match and self.state_match


def replay_observability_recording(
    recording: ObservabilityRecording,
) -> ObservabilityReplayReport:
    venue = HiddenLiquidityVenue(HiddenLiquidityRules.from_dict(recording.rules))
    for command in recording.commands:
        venue.advance_to(command.simulation_time_us)
        _apply_command(venue, command)
    venue.advance_to(recording.completed_time_us)
    venue.assert_invariants()
    feed = venue.observable_feed().as_dict()
    truth = venue.post_session_ground_truth().as_dict()
    return ObservabilityReplayReport(
        recording,
        venue,
        feed == recording.expected_observable_feed
        and _sha256(feed) == recording.expected_observable_sha256,
        truth == recording.expected_ground_truth
        and _sha256(truth) == recording.expected_truth_sha256,
        venue.state_sha256() == recording.expected_state_sha256,
    )


def _apply_command(
    venue: HiddenLiquidityVenue,
    command: ObservabilityCommand,
) -> None:
    values = command.parameters
    if command.command_type == "SUBMIT":
        raw_request = values.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("recorded hidden order request is invalid")
        venue.submit_resting(HiddenOrderRequest.from_dict(raw_request))
    elif command.command_type == "MARKET":
        venue.execute_market(
            str(values["order_id"]),
            Side(str(values["side"])),
            _required_int(values, "quantity"),
            owner=OrderOwner(str(values["owner"])),
            account_id=str(values["account_id"]),
        )
    elif command.command_type == "CANCEL":
        venue.cancel(str(values["order_id"]))
    elif command.command_type == "REFRESH":
        venue.refresh_order(str(values["order_id"]))
    elif command.command_type == "COMPLETE":
        venue.complete_session()
    else:  # pragma: no cover - command validates
        raise RuntimeError("unsupported observability replay command")


def recording_json_round_trip(
    recording: ObservabilityRecording,
) -> ObservabilityRecording:
    return ObservabilityRecording.from_dict(
        json.loads(json.dumps(recording.as_dict(), sort_keys=True))
    )


def _sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value
