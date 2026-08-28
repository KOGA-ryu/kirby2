"""Strict fresh-process restoration for the single-venue mechanics core.

This module deliberately restores only :class:`MarketMechanicsEngine` and the
owners nested beneath it.  It does not reconstruct a prefix from a seed and it
does not claim to restore flow, agents, latency, features, strategies,
algorithms, multiple venues, or historical cursors.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
from kirby2.exchange.mechanics_models import (
    AdvancedOrderRequest,
    OrderInstruction,
    SessionState,
)
from kirby2.exchange.models import OrderOwner, Side
from kirby2.full_day.models import (
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
)
from kirby2.immutable import freeze_json, thaw_json


CORE_SESSION_CHECKPOINT_SCHEMA_VERSION = 1
CORE_SESSION_CHECKPOINT_FORMAT_ID = "KIRBY2_CORE_SESSION_CHECKPOINT_V1"
CORE_RESTORE_REQUEST_SCHEMA_VERSION = 1
CORE_RESTORE_REQUEST_FORMAT_ID = "KIRBY2_CORE_RESTORE_REQUEST_V1"
CORE_RESTORE_RESULT_SCHEMA_VERSION = 1
CORE_RESTORE_RESULT_FORMAT_ID = "KIRBY2_CORE_RESTORE_RESULT_V1"
CORE_RNG_STATE_ABSENT = "ABSENT"
FULL_DAY_RUNTIME_RESTORE_REQUEST_SCHEMA_VERSION = 1
FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID = (
    "KIRBY2_FULL_DAY_RUNTIME_RESTORE_REQUEST_V1"
)
FULL_DAY_RUNTIME_RESTORE_RESULT_SCHEMA_VERSION = 1
FULL_DAY_RUNTIME_RESTORE_RESULT_FORMAT_ID = (
    "KIRBY2_FULL_DAY_RUNTIME_RESTORE_RESULT_V1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMAND_TYPES = frozenset(
    {"TRANSITION", "SUBMIT", "CANCEL", "REPLACE", "UNCROSS"}
)
_REQUEST_FIELDS = frozenset(
    {
        "account_id",
        "auction_only",
        "good_until_time_us",
        "instruction",
        "modifiers",
        "order_id",
        "owner",
        "price_ticks",
        "quantity",
        "side",
        "time_in_force",
    }
)


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str] | frozenset[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    actual = set(payload)
    missing = sorted(set(expected).difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {context} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )


def _wire_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"serialized {name} must be at least {minimum}")
    return value


def _wire_string(value: object, name: str, *, nonempty: bool = True) -> str:
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    if nonempty and not value:
        raise ValueError(f"serialized {name} must not be empty")
    return value


def _wire_object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {name} must be an object")
    return value


def _wire_array(value: object, name: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"serialized {name} must be an array")
    return value


def _wire_optional_int(
    value: object,
    name: str,
    *,
    minimum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return _wire_int(value, name, minimum=minimum)


def _wire_sha256(value: object, name: str) -> str:
    digest = _wire_string(value, name)
    if _SHA256.fullmatch(digest) is None:
        raise ValueError(f"serialized {name} must be a lowercase SHA-256")
    return digest


def _detached_object(value: Mapping[str, object]) -> dict[str, object]:
    """Validate semantic JSON and detach every caller-owned container."""

    return parse_canonical_json_object(canonical_json_bytes(value))


def _as_plain_object(value: Mapping[str, object]) -> dict[str, object]:
    plain = thaw_json(value)
    if type(plain) is not dict:  # pragma: no cover - Mapping input guarantees this
        raise TypeError("frozen JSON object did not thaw to an object")
    return plain


def _strict_advanced_request(
    payload: Mapping[str, object],
) -> AdvancedOrderRequest:
    _require_exact_fields(payload, _REQUEST_FIELDS, "advanced order request")
    order_id = _wire_string(payload["order_id"], "order_id")
    account_id = _wire_string(payload["account_id"], "account_id")
    quantity = _wire_int(payload["quantity"], "quantity", minimum=1)
    side = Side(_wire_string(payload["side"], "side"))
    owner = OrderOwner(_wire_string(payload["owner"], "owner"))
    instruction = OrderInstruction(
        _wire_string(payload["instruction"], "instruction")
    )
    time_in_force = OrderInstruction(
        _wire_string(payload["time_in_force"], "time_in_force")
    )
    raw_modifiers = _wire_array(payload["modifiers"], "modifiers")
    modifiers = tuple(
        _wire_string(value, f"modifiers[{index}]")
        for index, value in enumerate(raw_modifiers)
    )
    if modifiers != tuple(sorted(set(modifiers))):
        raise ValueError("serialized order modifiers must be unique and sorted")
    auction_only = payload["auction_only"]
    if type(auction_only) is not bool:
        raise TypeError("serialized auction_only must be a boolean")
    request = AdvancedOrderRequest(
        order_id=order_id,
        side=side,
        quantity=quantity,
        instruction=instruction,
        owner=owner,
        account_id=account_id,
        price_ticks=_wire_optional_int(
            payload["price_ticks"], "price_ticks", minimum=1
        ),
        time_in_force=time_in_force,
        modifiers=frozenset(OrderInstruction(value) for value in modifiers),
        good_until_time_us=_wire_optional_int(
            payload["good_until_time_us"],
            "good_until_time_us",
            minimum=0,
        ),
        auction_only=auction_only,
    )
    if request.as_dict() != _detached_object(payload):
        raise ValueError("advanced order request is not in canonical semantic form")
    return request


def _validate_command_parameters(
    command_type: str,
    parameters: Mapping[str, object],
) -> None:
    if command_type == "TRANSITION":
        _require_exact_fields(parameters, {"reason", "state"}, "transition command")
        SessionState(_wire_string(parameters["state"], "transition state"))
        _wire_string(parameters["reason"], "transition reason")
    elif command_type == "SUBMIT":
        _require_exact_fields(parameters, {"request"}, "submit command")
        _strict_advanced_request(
            _wire_object(parameters["request"], "submit request")
        )
    elif command_type == "CANCEL":
        _require_exact_fields(parameters, {"order_id", "reason"}, "cancel command")
        _wire_string(parameters["order_id"], "cancel order_id")
        _wire_string(parameters["reason"], "cancel reason")
    elif command_type == "REPLACE":
        _require_exact_fields(
            parameters,
            {"new_order_id", "new_price_ticks", "new_quantity", "order_id"},
            "replace command",
        )
        _wire_string(parameters["order_id"], "replace order_id")
        _wire_string(parameters["new_order_id"], "replace new_order_id")
        _wire_int(parameters["new_quantity"], "replace new_quantity", minimum=1)
        _wire_optional_int(
            parameters["new_price_ticks"],
            "replace new_price_ticks",
            minimum=1,
        )
    elif command_type == "UNCROSS":
        _require_exact_fields(parameters, set(), "uncross command")
    else:  # pragma: no cover - command enum is checked first
        raise ValueError("unsupported core-session command")


@dataclass(frozen=True, slots=True)
class CoreSessionCommandV1:
    """One strict suffix command; its sequence is local to the suffix."""

    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        _wire_int(self.sequence, "command sequence", minimum=1)
        _wire_int(self.simulation_time_us, "command time", minimum=0)
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("unsupported core-session command")
        detached = _detached_object(
            _wire_object(self.parameters, "command parameters")
        )
        _validate_command_parameters(self.command_type, detached)
        object.__setattr__(self, "parameters", freeze_json(detached))

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": _as_plain_object(self.parameters),
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CoreSessionCommandV1:
        _require_exact_fields(
            payload,
            {"command_type", "parameters", "sequence", "simulation_time_us"},
            "CoreSessionCommandV1",
        )
        return cls(
            sequence=_wire_int(payload["sequence"], "command sequence", minimum=1),
            simulation_time_us=_wire_int(
                payload["simulation_time_us"], "command time", minimum=0
            ),
            command_type=_wire_string(payload["command_type"], "command type"),
            parameters=_wire_object(payload["parameters"], "command parameters"),
        )


def _event_prefix_projection(engine: MarketMechanicsEngine) -> dict[str, object]:
    return {
        "local_events": [
            event.as_dict() for event in engine.book.journal.events
        ],
        "outer_events": [event.as_dict() for event in engine.events],
    }


def _verify_prefix_binding(
    engine: MarketMechanicsEngine,
    checkpoint: CoreSessionCheckpointV1,
) -> None:
    outer = engine.events[: checkpoint.prefix_outer_event_count]
    local = engine.book.journal.events[: checkpoint.prefix_local_event_count]
    if (
        len(outer) != checkpoint.prefix_outer_event_count
        or len(local) != checkpoint.prefix_local_event_count
    ):
        raise ValueError("restored engine is shorter than its bound event prefix")
    projection = {
        "local_events": [event.as_dict() for event in local],
        "outer_events": [event.as_dict() for event in outer],
    }
    if canonical_sha256(projection) != checkpoint.prefix_sha256:
        raise ValueError("restored engine event prefix digest differs from checkpoint")


@dataclass(frozen=True, slots=True)
class CoreSessionCheckpointV1:
    """Portable state for exactly one authoritative mechanics core."""

    schema_version: int
    format_id: str
    core_rng_state: str
    engine_state: Mapping[str, object]
    engine_state_sha256: str
    prefix_outer_event_count: int
    prefix_local_event_count: int
    prefix_sha256: str

    def __post_init__(self) -> None:
        if (
            _wire_int(self.schema_version, "checkpoint schema version", minimum=1)
            != CORE_SESSION_CHECKPOINT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported core-session checkpoint schema")
        if self.format_id != CORE_SESSION_CHECKPOINT_FORMAT_ID:
            raise ValueError("unsupported core-session checkpoint format")
        if self.core_rng_state != CORE_RNG_STATE_ABSENT:
            raise ValueError("the deterministic mechanics core must have RNG state ABSENT")
        outer_count = _wire_int(
            self.prefix_outer_event_count,
            "prefix outer event count",
            minimum=0,
        )
        local_count = _wire_int(
            self.prefix_local_event_count,
            "prefix local event count",
            minimum=0,
        )
        engine_state = _detached_object(
            _wire_object(self.engine_state, "engine state")
        )
        state_digest = _wire_sha256(
            self.engine_state_sha256, "engine state SHA-256"
        )
        if canonical_sha256(engine_state) != state_digest:
            raise ValueError("core-session engine state digest does not match")
        prefix_digest = _wire_sha256(self.prefix_sha256, "prefix SHA-256")

        # The owner constructor validates every nested owner before any restored
        # engine can escape this envelope.
        restored = MarketMechanicsEngine.from_checkpoint_state(engine_state)
        restored.assert_invariants()
        if len(restored.events) != outer_count:
            raise ValueError("checkpoint outer-event count differs from engine state")
        if len(restored.book.journal.events) != local_count:
            raise ValueError("checkpoint local-event count differs from engine state")
        if canonical_sha256(_event_prefix_projection(restored)) != prefix_digest:
            raise ValueError("checkpoint event-prefix digest does not match")
        object.__setattr__(self, "engine_state", freeze_json(engine_state))

    @classmethod
    def capture(cls, engine: MarketMechanicsEngine) -> CoreSessionCheckpointV1:
        if type(engine) is not MarketMechanicsEngine:
            raise TypeError("core-session checkpoint requires MarketMechanicsEngine")
        engine.assert_invariants()
        state = engine.checkpoint_state()
        prefix = _event_prefix_projection(engine)
        return cls(
            schema_version=CORE_SESSION_CHECKPOINT_SCHEMA_VERSION,
            format_id=CORE_SESSION_CHECKPOINT_FORMAT_ID,
            core_rng_state=CORE_RNG_STATE_ABSENT,
            engine_state=state,
            engine_state_sha256=canonical_sha256(state),
            prefix_outer_event_count=len(engine.events),
            prefix_local_event_count=len(engine.book.journal.events),
            prefix_sha256=canonical_sha256(prefix),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "core_rng_state": self.core_rng_state,
            "engine_state": _as_plain_object(self.engine_state),
            "engine_state_sha256": self.engine_state_sha256,
            "format_id": self.format_id,
            "prefix_local_event_count": self.prefix_local_event_count,
            "prefix_outer_event_count": self.prefix_outer_event_count,
            "prefix_sha256": self.prefix_sha256,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CoreSessionCheckpointV1:
        _require_exact_fields(
            payload,
            {
                "core_rng_state",
                "engine_state",
                "engine_state_sha256",
                "format_id",
                "prefix_local_event_count",
                "prefix_outer_event_count",
                "prefix_sha256",
                "schema_version",
            },
            "CoreSessionCheckpointV1",
        )
        return cls(
            schema_version=_wire_int(
                payload["schema_version"], "checkpoint schema version", minimum=1
            ),
            format_id=_wire_string(payload["format_id"], "checkpoint format"),
            core_rng_state=_wire_string(payload["core_rng_state"], "core RNG state"),
            engine_state=_wire_object(payload["engine_state"], "engine state"),
            engine_state_sha256=_wire_sha256(
                payload["engine_state_sha256"], "engine state SHA-256"
            ),
            prefix_outer_event_count=_wire_int(
                payload["prefix_outer_event_count"],
                "prefix outer event count",
                minimum=0,
            ),
            prefix_local_event_count=_wire_int(
                payload["prefix_local_event_count"],
                "prefix local event count",
                minimum=0,
            ),
            prefix_sha256=_wire_sha256(
                payload["prefix_sha256"], "prefix SHA-256"
            ),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CoreSessionCheckpointV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    def restore_engine(self) -> MarketMechanicsEngine:
        engine = MarketMechanicsEngine.from_checkpoint_state(
            _as_plain_object(self.engine_state)
        )
        _verify_prefix_binding(engine, self)
        engine.assert_invariants()
        return engine


@dataclass(frozen=True, slots=True)
class CoreRestoreRequestV1:
    """One checkpoint and one suffix, with no seed or prefix-replay surface."""

    schema_version: int
    format_id: str
    checkpoint: CoreSessionCheckpointV1
    suffix_commands: tuple[CoreSessionCommandV1, ...]
    completed_time_us: int

    def __post_init__(self) -> None:
        if (
            _wire_int(self.schema_version, "restore request schema version", minimum=1)
            != CORE_RESTORE_REQUEST_SCHEMA_VERSION
        ):
            raise ValueError("unsupported core restore request schema")
        if self.format_id != CORE_RESTORE_REQUEST_FORMAT_ID:
            raise ValueError("unsupported core restore request format")
        if type(self.checkpoint) is not CoreSessionCheckpointV1:
            raise TypeError("restore request checkpoint must use CoreSessionCheckpointV1")
        if type(self.suffix_commands) is not tuple or any(
            type(command) is not CoreSessionCommandV1
            for command in self.suffix_commands
        ):
            raise TypeError("suffix commands must be a CoreSessionCommandV1 tuple")
        sequences = tuple(command.sequence for command in self.suffix_commands)
        if sequences != tuple(range(1, len(self.suffix_commands) + 1)):
            raise ValueError("suffix command sequence must be contiguous from one")
        times = tuple(command.simulation_time_us for command in self.suffix_commands)
        if times != tuple(sorted(times)):
            raise ValueError("suffix command times must be monotonic")
        checkpoint_clock = _wire_object(
            _wire_object(self.checkpoint.engine_state, "engine state")["clock"],
            "checkpoint clock",
        )
        checkpoint_time = _wire_int(
            checkpoint_clock["current_time_us"],
            "checkpoint current time",
            minimum=0,
        )
        if times and times[0] < checkpoint_time:
            raise ValueError("suffix commands cannot precede the checkpoint time")
        completed_time = _wire_int(
            self.completed_time_us, "restore completion time", minimum=checkpoint_time
        )
        if times and completed_time < times[-1]:
            raise ValueError("restore completion time precedes its final command")

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint": self.checkpoint.as_dict(),
            "completed_time_us": self.completed_time_us,
            "format_id": self.format_id,
            "schema_version": self.schema_version,
            "suffix_commands": [
                command.as_dict() for command in self.suffix_commands
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CoreRestoreRequestV1:
        _require_exact_fields(
            payload,
            {
                "checkpoint",
                "completed_time_us",
                "format_id",
                "schema_version",
                "suffix_commands",
            },
            "CoreRestoreRequestV1",
        )
        raw_commands = _wire_array(payload["suffix_commands"], "suffix commands")
        return cls(
            schema_version=_wire_int(
                payload["schema_version"], "restore request schema version", minimum=1
            ),
            format_id=_wire_string(payload["format_id"], "restore request format"),
            checkpoint=CoreSessionCheckpointV1.from_dict(
                _wire_object(payload["checkpoint"], "checkpoint")
            ),
            suffix_commands=tuple(
                CoreSessionCommandV1.from_dict(
                    _wire_object(command, f"suffix_commands[{index}]")
                )
                for index, command in enumerate(raw_commands)
            ),
            completed_time_us=_wire_int(
                payload["completed_time_us"], "restore completion time", minimum=0
            ),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CoreRestoreRequestV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def apply_core_session_suffix(
    engine: MarketMechanicsEngine,
    commands: Sequence[CoreSessionCommandV1],
    *,
    completed_time_us: int,
) -> None:
    """Apply only the post-checkpoint command suffix to an existing engine."""

    if type(engine) is not MarketMechanicsEngine:
        raise TypeError("suffix execution requires MarketMechanicsEngine")
    if not isinstance(commands, Sequence) or isinstance(
        commands, (str, bytes, bytearray)
    ):
        raise TypeError("suffix commands must be a sequence")
    for command in commands:
        if type(command) is not CoreSessionCommandV1:
            raise TypeError("suffix command must use CoreSessionCommandV1")
        engine.advance_to(command.simulation_time_us)
        values = _as_plain_object(command.parameters)
        if command.command_type == "TRANSITION":
            engine.transition_session(
                SessionState(str(values["state"])),
                reason=str(values["reason"]),
            )
        elif command.command_type == "SUBMIT":
            engine.submit(
                _strict_advanced_request(
                    _wire_object(values["request"], "submit request")
                )
            )
        elif command.command_type == "CANCEL":
            engine.cancel(str(values["order_id"]), reason=str(values["reason"]))
        elif command.command_type == "REPLACE":
            engine.replace_order(
                str(values["order_id"]),
                new_order_id=str(values["new_order_id"]),
                new_quantity=int(values["new_quantity"]),
                new_price_ticks=_wire_optional_int(
                    values["new_price_ticks"],
                    "replace new_price_ticks",
                    minimum=1,
                ),
            )
        elif command.command_type == "UNCROSS":
            engine.uncross_auction()
        else:  # pragma: no cover - CoreSessionCommandV1 closes the enum
            raise RuntimeError("unsupported core-session command")
    engine.advance_to(completed_time_us)
    engine.assert_invariants()


def _without_fields(
    payload: Mapping[str, object],
    omitted: set[str] | frozenset[str],
) -> dict[str, object]:
    return {
        key: value
        for key, value in _detached_object(payload).items()
        if key not in omitted
    }


def _core_result(
    engine: MarketMechanicsEngine,
    checkpoint: CoreSessionCheckpointV1,
) -> dict[str, object]:
    """Project suffix evidence and final state without re-emitting prefix events."""

    engine.assert_invariants()
    _verify_prefix_binding(engine, checkpoint)
    owner_state = _detached_object(engine.checkpoint_state())
    checkpoint_state = _as_plain_object(checkpoint.engine_state)
    book_state = _wire_object(owner_state["book"], "restored book state")
    auction_state = _wire_object(owner_state["auction"], "restored auction state")
    checkpoint_book = _wire_object(
        checkpoint_state["book"], "checkpoint book state"
    )
    checkpoint_auction = _wire_object(
        checkpoint_state["auction"], "checkpoint auction state"
    )
    journal_state = _wire_object(book_state["journal"], "restored journal state")
    position_state = _wire_object(
        book_state["player_position"], "restored player position state"
    )
    outer_suffix = [
        event.as_dict()
        for event in engine.events[checkpoint.prefix_outer_event_count :]
    ]
    local_suffix = [
        event.as_dict()
        for event in engine.book.journal.events[
            checkpoint.prefix_local_event_count :
        ]
    ]
    all_continuous_fills = _wire_array(book_state["fills"], "book fills")
    all_continuous_trades = _wire_array(book_state["trades"], "book trades")
    all_auction_executions = _wire_array(
        auction_state["executions"], "auction executions"
    )
    all_auction_uncrosses = _wire_array(
        auction_state["uncross_history"], "auction uncross history"
    )
    all_player_fills = _wire_array(position_state["fills"], "player fills")
    prefix_continuous_fill_count = len(
        _wire_array(checkpoint_book["fills"], "checkpoint book fills")
    )
    prefix_auction_execution_count = len(
        _wire_array(
            checkpoint_auction["executions"],
            "checkpoint auction executions",
        )
    )
    if prefix_continuous_fill_count > len(all_continuous_fills):
        raise RuntimeError("final continuous fill history is shorter than checkpoint")
    if prefix_auction_execution_count > len(all_auction_executions):
        raise RuntimeError("final auction execution history is shorter than checkpoint")
    position_projection = {
        "bought_quantity": _wire_int(
            position_state["bought_quantity"],
            "player bought quantity",
            minimum=0,
        ),
        "fill_count": len(all_player_fills),
        "fill_history_sha256": canonical_sha256(all_player_fills),
        "position": _wire_int(position_state["position"], "player position"),
        "schema_version": _wire_int(
            position_state["schema_version"],
            "player-position schema version",
            minimum=1,
        ),
        "sold_quantity": _wire_int(
            position_state["sold_quantity"],
            "player sold quantity",
            minimum=0,
        ),
    }
    final = {
        "auction_state": _without_fields(
            auction_state,
            {"executions", "uncross_history"},
        ),
        "book_state": _without_fields(
            book_state,
            {"fills", "journal", "player_position", "trades"},
        ),
        "fills": {
            "auction": all_auction_executions[
                prefix_auction_execution_count:
            ],
            "continuous": all_continuous_fills[
                prefix_continuous_fill_count:
            ],
        },
        "history_bindings": {
            "auction_execution_count": len(all_auction_executions),
            "auction_execution_sha256": canonical_sha256(
                all_auction_executions
            ),
            "auction_uncross_count": len(all_auction_uncrosses),
            "auction_uncross_sha256": canonical_sha256(
                all_auction_uncrosses
            ),
            "continuous_fill_count": len(all_continuous_fills),
            "continuous_fill_sha256": canonical_sha256(all_continuous_fills),
            "continuous_trade_count": len(all_continuous_trades),
            "continuous_trade_sha256": canonical_sha256(
                all_continuous_trades
            ),
            "player_fill_count": len(all_player_fills),
            "player_fill_sha256": canonical_sha256(all_player_fills),
        },
        "mechanics_state": _without_fields(
            owner_state,
            {"auction", "book", "events"},
        ),
        "observables": {
            "auction_indication": engine.auction_indication().as_dict(),
            "best_ask": engine.book.best_ask,
            "best_bid": engine.book.best_bid,
            "book": engine.book.snapshot(),
            "clock_us": engine.clock.current_time_us,
            "last_trade_price_ticks": engine.last_trade_price_ticks,
            "player_position": engine.player_position,
            "session_state": engine.session_state.value,
        },
        "owner_allocators": {
            "auction": {
                "trade_sequence": _wire_int(
                    auction_state["trade_sequence"],
                    "auction trade sequence",
                    minimum=0,
                )
            },
            "book": {
                "order_count": _wire_int(
                    book_state["order_count"], "book order count", minimum=0
                ),
                "resting_sequence": _wire_int(
                    book_state["resting_sequence"],
                    "book resting sequence",
                    minimum=0,
                ),
                "trade_sequence": _wire_int(
                    book_state["trade_sequence"],
                    "book trade sequence",
                    minimum=0,
                ),
            },
            "local_event_next_sequence": _wire_int(
                journal_state["next_sequence"],
                "local event next sequence",
                minimum=1,
            ),
            "mechanics": _wire_object(
                owner_state["allocators"], "mechanics allocators"
            ),
            "outer_event_next_sequence": len(engine.events) + 1,
        },
        "player_position_state": position_projection,
        "restorable_state_sha256": canonical_sha256(owner_state),
        "state_sha256": engine.state_sha256(),
    }
    suffix = {
        "local_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(local_suffix)
        ).hexdigest(),
        "local_events": local_suffix,
        "outer_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(outer_suffix)
        ).hexdigest(),
        "outer_events": outer_suffix,
    }
    invariant_projection = {"final": final, "suffix": suffix}
    return {
        "final": final,
        "format_id": CORE_RESTORE_RESULT_FORMAT_ID,
        "invariant_sha256": canonical_sha256(invariant_projection),
        "prefix": {
            "local_event_count": checkpoint.prefix_local_event_count,
            "outer_event_count": checkpoint.prefix_outer_event_count,
            "sha256": checkpoint.prefix_sha256,
        },
        "schema_version": CORE_RESTORE_RESULT_SCHEMA_VERSION,
        "suffix": suffix,
    }


def execute_core_restore_request(request: CoreRestoreRequestV1) -> dict[str, object]:
    """Restore from state, execute the suffix, and return one canonical result."""

    if type(request) is not CoreRestoreRequestV1:
        raise TypeError("core restore execution requires CoreRestoreRequestV1")
    engine = request.checkpoint.restore_engine()
    apply_core_session_suffix(
        engine,
        request.suffix_commands,
        completed_time_us=request.completed_time_us,
    )
    return _core_result(engine, request.checkpoint)


def execute_uninterrupted_suffix(
    engine: MarketMechanicsEngine,
    checkpoint: CoreSessionCheckpointV1,
    commands: Sequence[CoreSessionCommandV1],
    *,
    completed_time_us: int,
) -> dict[str, object]:
    """Reference path: continue the already-running engine without restoration."""

    if canonical_sha256(engine.checkpoint_state()) != checkpoint.engine_state_sha256:
        raise ValueError("uninterrupted engine no longer equals checkpoint state")
    _verify_prefix_binding(engine, checkpoint)
    apply_core_session_suffix(
        engine,
        commands,
        completed_time_us=completed_time_us,
    )
    return _core_result(engine, checkpoint)


def _full_day_runtime_prefix_counts(
    checkpoint_state: Mapping[str, object],
) -> dict[str, object]:
    """Return strict prefix cursors without inventing a replay surface."""

    events = _wire_array(checkpoint_state["events"], "full-day outer events")
    engine = _wire_object(checkpoint_state["engine"], "full-day engine state")
    mechanics_events = _wire_array(engine["events"], "mechanics events")
    native = _wire_array(
        checkpoint_state["native_ledger"], "full-day native ledger"
    )
    scheduler_union = _wire_object(
        checkpoint_state["agent_scheduler"], "agent scheduler union"
    )
    scheduler_status = _wire_string(
        scheduler_union["status"], "agent scheduler status"
    )
    public_count = 0
    truth_count = 0
    if scheduler_status == "PRESERVED":
        scheduler = _wire_object(
            scheduler_union["state"], "agent scheduler checkpoint"
        )
        owned = _wire_object(scheduler["state"], "agent scheduler owned state")
        public_count = len(
            _wire_array(owned["public_events"], "agent public events")
        )
        truth_count = len(
            _wire_array(owned["truth_events"], "agent truth events")
        )
    elif scheduler_status != "ABSENT":
        raise ValueError("full-day scheduler union has an unsupported status")
    return {
        "agent_public_event_count": public_count,
        "agent_truth_event_count": truth_count,
        "mechanics_event_count": len(mechanics_events),
        "native_ledger_count": len(native),
        "native_ledger_sha256": hashlib.sha256(
            canonical_json_bytes(native)
        ).hexdigest(),
        "outer_event_count": len(events),
        "outer_event_sha256": hashlib.sha256(
            canonical_json_bytes(events)
        ).hexdigest(),
    }


def _full_day_native_ledger_key(
    row: Mapping[str, object],
) -> tuple[str, str, str]:
    """Return the immutable identity of one serialized native-ledger row."""

    reference = _wire_object(row["reference"], "native ledger reference")
    return (
        _wire_string(
            reference["owner_component_id"],
            "native ledger owner component ID",
        ),
        _wire_string(reference["native_ledger_id"], "native ledger ID"),
        _wire_string(reference["event_id"], "native ledger event ID"),
    )


@dataclass(frozen=True, slots=True)
class FullDayRuntimeRestoreRequestV1:
    """One composed checkpoint plus deterministic post-checkpoint time targets."""

    schema_version: int
    format_id: str
    checkpoint_state: Mapping[str, object]
    checkpoint_state_sha256: str
    suffix_targets_us: tuple[int, ...]
    final_checkpoint_request_id: str

    def __post_init__(self) -> None:
        if (
            _wire_int(
                self.schema_version,
                "full-day restore request schema version",
                minimum=1,
            )
            != FULL_DAY_RUNTIME_RESTORE_REQUEST_SCHEMA_VERSION
        ):
            raise ValueError("unsupported full-day restore request schema")
        if self.format_id != FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID:
            raise ValueError("unsupported full-day restore request format")
        detached = _detached_object(
            _wire_object(self.checkpoint_state, "full-day checkpoint state")
        )
        digest = _wire_sha256(
            self.checkpoint_state_sha256, "full-day checkpoint state SHA-256"
        )
        if canonical_sha256(detached) != digest:
            raise ValueError("full-day checkpoint state digest does not match")
        if type(self.suffix_targets_us) is not tuple or any(
            type(value) is not int or value < 0
            for value in self.suffix_targets_us
        ):
            raise TypeError(
                "full-day suffix targets must be an integer tuple"
            )
        if self.suffix_targets_us != tuple(sorted(self.suffix_targets_us)):
            raise ValueError("full-day suffix targets must be monotonic")
        request_id = _wire_string(
            self.final_checkpoint_request_id,
            "final checkpoint request ID",
        )
        if re.fullmatch(r"[A-Z0-9_.:-]+", request_id) is None:
            raise ValueError("final checkpoint request ID is not canonical")

        from kirby2.full_day.runtime import FullDayRuntime

        runtime = FullDayRuntime.from_checkpoint_state(detached)
        current_time_us = runtime.clock.current_time_us
        if self.suffix_targets_us and self.suffix_targets_us[0] < current_time_us:
            raise ValueError("full-day suffix target precedes its checkpoint")
        if self.suffix_targets_us and (
            self.suffix_targets_us[-1] > runtime.plan.calendar.end_time_us
        ):
            raise ValueError("full-day suffix target exceeds the plan calendar")
        controller = _wire_object(
            detached["checkpoint_controller"], "checkpoint controller"
        )
        allocated = _wire_array(
            controller["allocated_request_ids"], "allocated checkpoint IDs"
        )
        if request_id in allocated:
            raise ValueError("final checkpoint request ID was already allocated")
        object.__setattr__(self, "checkpoint_state", freeze_json(detached))

    @classmethod
    def capture(
        cls,
        runtime: object,
        *,
        suffix_targets_us: Sequence[int],
        final_checkpoint_request_id: str,
    ) -> FullDayRuntimeRestoreRequestV1:
        from kirby2.full_day.runtime import FullDayRuntime

        if type(runtime) is not FullDayRuntime:
            raise TypeError("full-day restore capture requires FullDayRuntime")
        if not isinstance(suffix_targets_us, Sequence) or isinstance(
            suffix_targets_us, (str, bytes, bytearray)
        ):
            raise TypeError("full-day suffix targets must be a sequence")
        state = runtime.checkpoint_state()
        return cls(
            schema_version=FULL_DAY_RUNTIME_RESTORE_REQUEST_SCHEMA_VERSION,
            format_id=FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID,
            checkpoint_state=state,
            checkpoint_state_sha256=canonical_sha256(state),
            suffix_targets_us=tuple(suffix_targets_us),
            final_checkpoint_request_id=final_checkpoint_request_id,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_state": _as_plain_object(self.checkpoint_state),
            "checkpoint_state_sha256": self.checkpoint_state_sha256,
            "final_checkpoint_request_id": self.final_checkpoint_request_id,
            "format_id": self.format_id,
            "schema_version": self.schema_version,
            "suffix_targets_us": list(self.suffix_targets_us),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> FullDayRuntimeRestoreRequestV1:
        _require_exact_fields(
            payload,
            {
                "checkpoint_state",
                "checkpoint_state_sha256",
                "final_checkpoint_request_id",
                "format_id",
                "schema_version",
                "suffix_targets_us",
            },
            "FullDayRuntimeRestoreRequestV1",
        )
        targets = _wire_array(payload["suffix_targets_us"], "suffix targets")
        return cls(
            schema_version=_wire_int(
                payload["schema_version"],
                "full-day restore request schema version",
                minimum=1,
            ),
            format_id=_wire_string(payload["format_id"], "restore request format"),
            checkpoint_state=_wire_object(
                payload["checkpoint_state"], "full-day checkpoint state"
            ),
            checkpoint_state_sha256=_wire_sha256(
                payload["checkpoint_state_sha256"],
                "full-day checkpoint state SHA-256",
            ),
            suffix_targets_us=tuple(
                _wire_int(value, f"suffix_targets_us[{index}]", minimum=0)
                for index, value in enumerate(targets)
            ),
            final_checkpoint_request_id=_wire_string(
                payload["final_checkpoint_request_id"],
                "final checkpoint request ID",
            ),
        )

    @classmethod
    def from_json_bytes(
        cls, payload: bytes
    ) -> FullDayRuntimeRestoreRequestV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def _full_day_scheduler_suffix(
    runtime: object,
    *,
    public_start: int,
    truth_start: int,
) -> dict[str, object]:
    scheduler = getattr(runtime, "agent_scheduler")
    if scheduler is None:
        return {
            "public_event_bytes_sha256": hashlib.sha256(
                canonical_json_bytes([])
            ).hexdigest(),
            "public_events": [],
            "status": "ABSENT",
            "truth_event_bytes_sha256": hashlib.sha256(
                canonical_json_bytes([])
            ).hexdigest(),
            "truth_events": [],
        }
    scheduler_state = scheduler.checkpoint_state()
    owned = _wire_object(scheduler_state["state"], "agent scheduler owned state")
    public = _wire_array(owned["public_events"], "agent public events")
    truth = _wire_array(owned["truth_events"], "agent truth events")
    if public_start > len(public) or truth_start > len(truth):
        raise RuntimeError("restored agent ledgers are shorter than their checkpoint")
    public_suffix = public[public_start:]
    truth_suffix = truth[truth_start:]
    return {
        "public_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(public_suffix)
        ).hexdigest(),
        "public_events": public_suffix,
        "status": "PRESERVED",
        "truth_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(truth_suffix)
        ).hexdigest(),
        "truth_events": truth_suffix,
    }


def apply_full_day_runtime_suffix(
    runtime: object,
    targets_us: Sequence[int],
    *,
    final_checkpoint_request_id: str,
) -> None:
    """Advance only the serialized suffix and finish at a quiescent marker."""

    from kirby2.full_day.runtime import FullDayRuntime

    if type(runtime) is not FullDayRuntime:
        raise TypeError("full-day suffix execution requires FullDayRuntime")
    if not isinstance(targets_us, Sequence) or isinstance(
        targets_us, (str, bytes, bytearray)
    ):
        raise TypeError("full-day suffix targets must be a sequence")
    for target in targets_us:
        if type(target) is not int:
            raise TypeError("full-day suffix target must be an integer")
        runtime.advance_to(target)
    runtime.capture_quiescent_cut(
        final_checkpoint_request_id,
        at_time_us=runtime.clock.current_time_us,
    )
    runtime.assert_invariants()


def _full_day_runtime_result(
    runtime: object,
    request: FullDayRuntimeRestoreRequestV1,
) -> dict[str, object]:
    from kirby2.full_day.runtime import FullDayRuntime

    if type(runtime) is not FullDayRuntime:
        raise TypeError("full-day result requires FullDayRuntime")
    checkpoint_state = _as_plain_object(request.checkpoint_state)
    prefix = _full_day_runtime_prefix_counts(checkpoint_state)
    outer_start = int(prefix["outer_event_count"])
    mechanics_start = int(prefix["mechanics_event_count"])
    outer_suffix = [event.as_dict() for event in runtime.events[outer_start:]]
    mechanics_suffix = [
        event.as_dict() for event in runtime.engine.events[mechanics_start:]
    ]
    scheduler_suffix = _full_day_scheduler_suffix(
        runtime,
        public_start=int(prefix["agent_public_event_count"]),
        truth_start=int(prefix["agent_truth_event_count"]),
    )
    final_state = runtime.checkpoint_state()
    prefix_native_rows = _wire_array(
        checkpoint_state["native_ledger"],
        "checkpoint native ledger",
    )
    prefix_native_keys = {
        _full_day_native_ledger_key(
            _wire_object(row, f"checkpoint native ledger[{index}]")
        )
        for index, row in enumerate(prefix_native_rows)
    }
    if len(prefix_native_keys) != len(prefix_native_rows):
        raise RuntimeError("checkpoint native ledger contains duplicate identities")
    final_native_rows = _wire_array(
        final_state["native_ledger"], "final native ledger"
    )
    final_native_keys = {
        _full_day_native_ledger_key(
            _wire_object(row, f"final native ledger[{index}]")
        )
        for index, row in enumerate(final_native_rows)
    }
    if len(final_native_keys) != len(final_native_rows):
        raise RuntimeError("final native ledger contains duplicate identities")
    if not prefix_native_keys.issubset(final_native_keys):
        raise RuntimeError("restored native ledger lost a checkpoint identity")
    native_suffix = [
        row
        for row in final_native_rows
        if _full_day_native_ledger_key(
            _wire_object(row, "final native ledger row")
        )
        not in prefix_native_keys
    ]
    suffix = {
        "agent_scheduler": scheduler_suffix,
        "mechanics_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(mechanics_suffix)
        ).hexdigest(),
        "mechanics_events": mechanics_suffix,
        "native_ledger_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(native_suffix)
        ).hexdigest(),
        "native_ledger": native_suffix,
        "outer_event_bytes_sha256": hashlib.sha256(
            canonical_json_bytes(outer_suffix)
        ).hexdigest(),
        "outer_events": outer_suffix,
    }
    final = {
        "projection": runtime.result_projection(),
        "restorable_state_sha256": canonical_sha256(final_state),
    }
    invariant_projection = {"final": final, "prefix": prefix, "suffix": suffix}
    return {
        "final": final,
        "format_id": FULL_DAY_RUNTIME_RESTORE_RESULT_FORMAT_ID,
        "invariant_sha256": canonical_sha256(invariant_projection),
        "prefix": prefix,
        "schema_version": FULL_DAY_RUNTIME_RESTORE_RESULT_SCHEMA_VERSION,
        "suffix": suffix,
    }


def execute_full_day_runtime_restore_request(
    request: FullDayRuntimeRestoreRequestV1,
) -> dict[str, object]:
    """Restore the composed owner graph and execute only its suffix targets."""

    from kirby2.full_day.runtime import FullDayRuntime

    if type(request) is not FullDayRuntimeRestoreRequestV1:
        raise TypeError(
            "full-day restore execution requires FullDayRuntimeRestoreRequestV1"
        )
    runtime = FullDayRuntime.from_checkpoint_state(
        _as_plain_object(request.checkpoint_state)
    )
    apply_full_day_runtime_suffix(
        runtime,
        request.suffix_targets_us,
        final_checkpoint_request_id=request.final_checkpoint_request_id,
    )
    return _full_day_runtime_result(runtime, request)


def execute_uninterrupted_full_day_runtime_suffix(
    runtime: object,
    request: FullDayRuntimeRestoreRequestV1,
) -> dict[str, object]:
    """Reference path over the already-running authoritative owner graph."""

    from kirby2.full_day.runtime import FullDayRuntime

    if type(runtime) is not FullDayRuntime:
        raise TypeError("uninterrupted full-day suffix requires FullDayRuntime")
    if runtime.canonical_state_bytes() != canonical_json_bytes(
        _as_plain_object(request.checkpoint_state)
    ):
        raise ValueError("uninterrupted runtime no longer equals the checkpoint")
    apply_full_day_runtime_suffix(
        runtime,
        request.suffix_targets_us,
        final_checkpoint_request_id=request.final_checkpoint_request_id,
    )
    return _full_day_runtime_result(runtime, request)


def full_day_runtime_restore_worker_main() -> int:
    """Canonical stdin/stdout worker used by the WO31-E1 fresh-process gate."""

    raw = sys.stdin.buffer.read()
    try:
        request = FullDayRuntimeRestoreRequestV1.from_json_bytes(raw)
        result = execute_full_day_runtime_restore_request(request)
        output = canonical_json_bytes(result)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        diagnostic = (
            f"FULL_DAY_RUNTIME_RESTORE_REFUSED {type(error).__name__}: {error}\n"
        ).encode("utf-8", errors="backslashreplace")
        sys.stderr.buffer.write(diagnostic)
        return 2
    sys.stdout.buffer.write(output)
    return 0


__all__ = [
    "CORE_RESTORE_REQUEST_FORMAT_ID",
    "CORE_RESTORE_REQUEST_SCHEMA_VERSION",
    "CORE_RESTORE_RESULT_FORMAT_ID",
    "CORE_RESTORE_RESULT_SCHEMA_VERSION",
    "CORE_RNG_STATE_ABSENT",
    "CORE_SESSION_CHECKPOINT_FORMAT_ID",
    "CORE_SESSION_CHECKPOINT_SCHEMA_VERSION",
    "CoreRestoreRequestV1",
    "CoreSessionCheckpointV1",
    "CoreSessionCommandV1",
    "FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID",
    "FULL_DAY_RUNTIME_RESTORE_REQUEST_SCHEMA_VERSION",
    "FULL_DAY_RUNTIME_RESTORE_RESULT_FORMAT_ID",
    "FULL_DAY_RUNTIME_RESTORE_RESULT_SCHEMA_VERSION",
    "FullDayRuntimeRestoreRequestV1",
    "apply_full_day_runtime_suffix",
    "apply_core_session_suffix",
    "execute_full_day_runtime_restore_request",
    "execute_core_restore_request",
    "execute_uninterrupted_full_day_runtime_suffix",
    "execute_uninterrupted_suffix",
    "full_day_runtime_restore_worker_main",
]
