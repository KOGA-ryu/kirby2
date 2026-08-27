"""Portable command recording and exact replay for fragmented-market drills."""

from __future__ import annotations

import json
from dataclasses import dataclass

from kirby2.exchange import SessionState
from kirby2.exchange.models import Side
from kirby2.observability import HiddenOrderRequest

from .coordinator import MarketCoordinator
from .models import (
    MULTIVENUE_RECORDING_SCHEMA_VERSION,
    RoutingRequest,
    VenueConfig,
    canonical_sha256,
)


_COMMAND_TYPES = frozenset(
    {"ADD", "SIM_MARKET", "ROUTE", "ADVANCE", "CANCEL_ALL", "SESSION", "COMPLETE"}
)


@dataclass(frozen=True, slots=True)
class MultiVenueCommand:
    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: dict[str, object]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("multi-venue replay command identity is invalid")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("unsupported multi-venue replay command")

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": self.parameters,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MultiVenueCommand:
        raw = payload.get("parameters")
        if not isinstance(raw, dict):
            raise ValueError("multi-venue command parameters are invalid")
        return cls(
            int(payload["sequence"]),
            int(payload["simulation_time_us"]),
            str(payload["command_type"]),
            dict(raw),
        )


@dataclass(frozen=True, slots=True)
class MultiVenueRecording:
    seed: int
    venue_configs: tuple[dict[str, object], ...]
    depth_subscriptions: tuple[str, ...]
    commands: tuple[MultiVenueCommand, ...]
    completed_time_us: int
    route_ids: tuple[str, ...]
    expected_events: tuple[dict[str, object], ...]
    expected_feed: dict[str, object]
    expected_ground_truth: dict[str, object]
    expected_scores: dict[str, dict[str, object]]
    expected_state_sha256: str
    schema_version: int = MULTIVENUE_RECORDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTIVENUE_RECORDING_SCHEMA_VERSION:
            raise ValueError("unsupported multi-venue recording schema")
        sequences = tuple(command.sequence for command in self.commands)
        times = tuple(command.simulation_time_us for command in self.commands)
        if sequences != tuple(range(1, len(self.commands) + 1)):
            raise ValueError("multi-venue command sequence is incomplete")
        if times != tuple(sorted(times)):
            raise ValueError("multi-venue command time moved backward")

    @classmethod
    def capture(
        cls,
        coordinator: MarketCoordinator,
        commands: tuple[MultiVenueCommand, ...],
        route_ids: tuple[str, ...],
    ) -> MultiVenueRecording:
        if not coordinator.complete:
            raise RuntimeError("multi-venue recording requires a completed coordinator")
        coordinator.assert_invariants()
        return cls(
            seed=coordinator.seed,
            venue_configs=tuple(
                venue.config.as_dict()
                for _, venue in sorted(coordinator.venues.items())
            ),
            depth_subscriptions=tuple(sorted(coordinator.depth_subscriptions)),
            commands=commands,
            completed_time_us=coordinator.clock.current_time_us,
            route_ids=route_ids,
            expected_events=tuple(event.as_dict() for event in coordinator.events),
            expected_feed=coordinator.consolidated_feed().as_dict(),
            expected_ground_truth=coordinator.post_session_ground_truth(),
            expected_scores={
                route_id: coordinator.score_route(route_id).as_dict()
                for route_id in route_ids
            },
            expected_state_sha256=coordinator.state_sha256(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": [command.as_dict() for command in self.commands],
            "completed_time_us": self.completed_time_us,
            "depth_subscriptions": list(self.depth_subscriptions),
            "expected_events": list(self.expected_events),
            "expected_feed": self.expected_feed,
            "expected_ground_truth": self.expected_ground_truth,
            "expected_scores": self.expected_scores,
            "expected_state_sha256": self.expected_state_sha256,
            "route_ids": list(self.route_ids),
            "schema_version": self.schema_version,
            "seed": self.seed,
            "venue_configs": list(self.venue_configs),
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MultiVenueRecording:
        configs = payload.get("venue_configs")
        commands = payload.get("commands")
        events = payload.get("expected_events")
        feed = payload.get("expected_feed")
        truth = payload.get("expected_ground_truth")
        scores = payload.get("expected_scores")
        if (
            not isinstance(configs, list)
            or any(not isinstance(item, dict) for item in configs)
            or not isinstance(commands, list)
            or any(not isinstance(item, dict) for item in commands)
            or not isinstance(events, list)
            or any(not isinstance(item, dict) for item in events)
            or not isinstance(feed, dict)
            or not isinstance(truth, dict)
            or not isinstance(scores, dict)
            or any(not isinstance(value, dict) for value in scores.values())
        ):
            raise ValueError("multi-venue recording payload is invalid")
        return cls(
            seed=int(payload["seed"]),
            venue_configs=tuple(dict(item) for item in configs),
            depth_subscriptions=tuple(str(item) for item in payload["depth_subscriptions"]),
            commands=tuple(MultiVenueCommand.from_dict(item) for item in commands),
            completed_time_us=int(payload["completed_time_us"]),
            route_ids=tuple(str(item) for item in payload["route_ids"]),
            expected_events=tuple(dict(item) for item in events),
            expected_feed=dict(feed),
            expected_ground_truth=dict(truth),
            expected_scores={str(key): dict(value) for key, value in scores.items()},
            expected_state_sha256=str(payload["expected_state_sha256"]),
            schema_version=int(payload["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class MultiVenueReplayReport:
    coordinator: MarketCoordinator
    events_match: bool
    feed_match: bool
    ground_truth_match: bool
    scores_match: bool
    state_match: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.events_match,
                self.feed_match,
                self.ground_truth_match,
                self.scores_match,
                self.state_match,
            )
        )


def replay_multivenue_recording(
    recording: MultiVenueRecording,
) -> MultiVenueReplayReport:
    coordinator = MarketCoordinator(
        tuple(VenueConfig.from_dict(item) for item in recording.venue_configs),
        seed=recording.seed,
        depth_subscriptions=frozenset(recording.depth_subscriptions),
    )
    route_ids: list[str] = []
    for command in recording.commands:
        coordinator.advance_to(command.simulation_time_us)
        route_id = _apply_command(coordinator, command)
        if route_id is not None:
            route_ids.append(route_id)
    if coordinator.clock.current_time_us < recording.completed_time_us:
        coordinator.advance_to(recording.completed_time_us)
    coordinator.assert_invariants()
    scores = {
        route_id: coordinator.score_route(route_id).as_dict()
        for route_id in route_ids
    }
    return MultiVenueReplayReport(
        coordinator,
        tuple(event.as_dict() for event in coordinator.events)
        == recording.expected_events,
        coordinator.consolidated_feed().as_dict() == recording.expected_feed,
        coordinator.post_session_ground_truth() == recording.expected_ground_truth,
        scores == recording.expected_scores,
        coordinator.state_sha256() == recording.expected_state_sha256,
    )


def recording_json_round_trip(recording: MultiVenueRecording) -> MultiVenueRecording:
    return MultiVenueRecording.from_dict(
        json.loads(json.dumps(recording.as_dict(), sort_keys=True))
    )


def _apply_command(
    coordinator: MarketCoordinator,
    command: MultiVenueCommand,
) -> str | None:
    values = command.parameters
    if command.command_type == "ADD":
        raw_request = values.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("recorded venue order request is invalid")
        coordinator.add_resting_order(
            str(values["venue_id"]),
            HiddenOrderRequest.from_dict(raw_request),
        )
    elif command.command_type == "SIM_MARKET":
        coordinator.execute_simulated_market(
            str(values["venue_id"]),
            str(values["order_id"]),
            Side(str(values["side"])),
            int(values["quantity"]),
        )
    elif command.command_type == "ROUTE":
        raw_request = values.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("recorded routing request is invalid")
        return coordinator.submit_route(RoutingRequest.from_dict(raw_request))
    elif command.command_type == "CANCEL_ALL":
        coordinator.cancel_all()
    elif command.command_type == "SESSION":
        coordinator.set_venue_session_state(
            str(values["venue_id"]),
            SessionState(str(values["state"])),
        )
    elif command.command_type == "COMPLETE":
        coordinator.complete_session()
    elif command.command_type != "ADVANCE":  # pragma: no cover - command validates
        raise RuntimeError("unsupported multi-venue replay command")
    return None
