"""Standalone restorable owner for fragmented venues and hidden liquidity.

This component deliberately replaces the single ``MarketMechanicsEngine`` owner;
it is not an adapter around ``FullDayRuntime``.  Its checkpoint is privileged, but
every result projection is public-feed shaped and exposes hidden truth only through
counts and cryptographic digests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from kirby2.full_day.models import (
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from kirby2.multivenue import (
    MarketCoordinator,
    MultiVenueCommand,
    apply_multivenue_command,
)


MULTIVENUE_HIDDEN_COMPONENT_ID = "VENUE_MULTIVENUE_HIDDEN_V1"
MULTIVENUE_HIDDEN_COMPONENT_SCHEMA_VERSION = 1
MULTIVENUE_HIDDEN_IMPLEMENTATION_VERSION = 1
MULTIVENUE_STATE_ID = "MULTIVENUE_V1"
HIDDEN_LIQUIDITY_STATE_ID = "HIDDEN_LIQUIDITY_V1"


class MultiVenueHiddenOwnerV1:
    """One exact owner of coordinator, venue, hidden, latency, and feed state."""

    def __init__(self, coordinator: MarketCoordinator) -> None:
        if type(coordinator) is not MarketCoordinator:
            raise TypeError("multi-venue owner requires the exact MarketCoordinator")
        self.coordinator = coordinator
        self.assert_invariants()

    def checkpoint_state(self) -> dict[str, object]:
        state = {
            "component_id": MULTIVENUE_HIDDEN_COMPONENT_ID,
            "implementation_version": MULTIVENUE_HIDDEN_IMPLEMENTATION_VERSION,
            "coordinator": self.coordinator.checkpoint_state(),
            "owned_state_ids": [HIDDEN_LIQUIDITY_STATE_ID, MULTIVENUE_STATE_ID],
            "schema_version": MULTIVENUE_HIDDEN_COMPONENT_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> MultiVenueHiddenOwnerV1:
        _require_exact_fields(
            payload,
            {
                "component_id",
                "coordinator",
                "implementation_version",
                "owned_state_ids",
                "schema_version",
            },
            "multi-venue component checkpoint",
        )
        validate_strict_json(payload)
        if payload["schema_version"] != MULTIVENUE_HIDDEN_COMPONENT_SCHEMA_VERSION:
            raise ValueError("unsupported multi-venue component checkpoint schema")
        if payload["implementation_version"] != MULTIVENUE_HIDDEN_IMPLEMENTATION_VERSION:
            raise ValueError("unsupported multi-venue component implementation version")
        if payload["component_id"] != MULTIVENUE_HIDDEN_COMPONENT_ID:
            raise ValueError("multi-venue component checkpoint has the wrong owner")
        if payload["owned_state_ids"] != [
            HIDDEN_LIQUIDITY_STATE_ID,
            MULTIVENUE_STATE_ID,
        ]:
            raise ValueError("multi-venue component owned-state inventory differs")
        owner = cls(
            MarketCoordinator.from_checkpoint_state(
                _object(payload["coordinator"], "multi-venue coordinator state")
            )
        )
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("multi-venue component checkpoint is not a fixed point")
        return owner

    @classmethod
    def from_canonical_state_bytes(cls, payload: bytes) -> MultiVenueHiddenOwnerV1:
        return cls.from_checkpoint_state(parse_canonical_json_object(payload))

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    def state_sha256(self) -> str:
        return canonical_sha256(self.checkpoint_state())

    def assert_invariants(self) -> None:
        self.coordinator.assert_invariants()

    def public_projection(self) -> dict[str, object]:
        """Return only client-observable state and non-reversible truth digests."""

        coordinator = self.coordinator
        feeds = {
            venue_id: venue.observable_feed()
            for venue_id, venue in sorted(coordinator.venues.items())
        }
        projection: dict[str, object] = {
            "client_known_global_player_position": sum(
                feed.player_position.position for feed in feeds.values()
            ),
            "complete": coordinator.complete,
            "consolidated_feed": coordinator.consolidated_feed().as_dict(),
            "coordinator_event_count": len(coordinator.events),
            "coordinator_event_sha256": coordinator.event_stream_sha256(),
            "routes": {
                route_id: {
                    "decision": state.decision.as_dict(),
                    "execution_count": len(state.executions),
                    "execution_sha256": canonical_sha256(
                        [
                            state.executions[index].as_dict()
                            for index in sorted(state.executions)
                        ]
                    ),
                    "request": state.request.as_dict(),
                }
                for route_id, state in sorted(coordinator._routes.items())
            },
            "simulation_time_us": coordinator.clock.current_time_us,
            "venues": {
                venue_id: {
                    "observable_event_sha256": venue.engine.observable_event_sha256(),
                    "observable_feed": feeds[venue_id].as_dict(),
                    "truth_event_count": len(venue.engine._truth_events),
                    "truth_event_sha256": venue.engine.truth_event_sha256(),
                }
                for venue_id, venue in sorted(coordinator.venues.items())
            },
        }
        _assert_public_projection(projection)
        validate_strict_json(projection)
        return projection


def apply_multivenue_hidden_suffix(
    owner: MultiVenueHiddenOwnerV1,
    commands: Sequence[MultiVenueCommand],
    *,
    completed_time_us: int,
) -> tuple[str, ...]:
    """Apply only commands at or after the captured component checkpoint."""

    if type(owner) is not MultiVenueHiddenOwnerV1:
        raise TypeError("multi-venue suffix requires MultiVenueHiddenOwnerV1")
    if not isinstance(commands, Sequence) or isinstance(
        commands, (str, bytes, bytearray)
    ):
        raise TypeError("multi-venue suffix commands must be a sequence")
    command_tuple = tuple(commands)
    if any(type(command) is not MultiVenueCommand for command in command_tuple):
        raise TypeError("multi-venue suffix contains a noncanonical command")
    if tuple(command.sequence for command in command_tuple) != tuple(
        range(1, len(command_tuple) + 1)
    ):
        raise ValueError("multi-venue suffix command sequence must start at one")
    times = tuple(command.simulation_time_us for command in command_tuple)
    if times != tuple(sorted(times)):
        raise ValueError("multi-venue suffix command time moved backward")
    if times and times[0] < owner.coordinator.clock.current_time_us:
        raise ValueError("multi-venue suffix precedes its checkpoint")
    if type(completed_time_us) is not int or completed_time_us < 0:
        raise ValueError("multi-venue suffix completion time must be nonnegative")
    route_ids: list[str] = []
    for command in command_tuple:
        owner.coordinator.advance_to(command.simulation_time_us)
        route_id = apply_multivenue_command(owner.coordinator, command)
        if route_id is not None:
            route_ids.append(route_id)
    if completed_time_us < owner.coordinator.clock.current_time_us:
        raise ValueError("multi-venue suffix completion precedes resulting state")
    owner.coordinator.advance_to(completed_time_us)
    owner.assert_invariants()
    return tuple(route_ids)


def _assert_public_projection(payload: object) -> None:
    import json

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()
    forbidden = (
        "reserve_quantity",
        "reserve_remaining",
        "hidden_quantity",
        "hidden_remaining",
        "priority_sequence",
        "maker_order_id",
        "liquidity_source",
        "ground_truth",
    )
    if any(field in serialized for field in forbidden):
        raise RuntimeError("multi-venue public projection leaked hidden venue truth")


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


__all__ = [
    "HIDDEN_LIQUIDITY_STATE_ID",
    "MULTIVENUE_HIDDEN_COMPONENT_ID",
    "MULTIVENUE_HIDDEN_COMPONENT_SCHEMA_VERSION",
    "MULTIVENUE_HIDDEN_IMPLEMENTATION_VERSION",
    "MULTIVENUE_STATE_ID",
    "MultiVenueHiddenOwnerV1",
    "apply_multivenue_hidden_suffix",
]
