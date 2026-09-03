"""Strict V1 training, start-result, and authoritative live-frame records."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.session import ObjectiveType, SessionObjective

from .simulation_contract import (
    INTRADAY_PHASES,
    SimulationComponentRefV1,
    SimulationContractIntegrityError,
    SimulationProfileRefV1,
    SimulationProfileResolutionV1,
    SimulationTrainingResourceCatalogV1,
    _array,
    _digest,
    _enum,
    _exact,
    _freeze,
    _identifier,
    _integer,
    _object,
    _plain,
    _positive_integer,
    _power_of_ten,
    _snapshot,
    _text,
    canonical_digest,
)


TRAINING_OPTIONS_SCHEMA_ID = "KIRBY2_SIMULATION_TRAINING_OPTIONS_V1"
START_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_START_RESULT_V1"
FRAME_SCHEMA_ID = "KIRBY2_SIMULATION_FRAME_V1"
RUN_REQUEST_SCHEMA_ID = "KIRBY2_SIMULATION_RUN_REQUEST_V1"
SCHEMA_VERSION = 1

RUN_STATES = frozenset({"READY", "RUNNING", "PAUSED", "COMPLETE"})
START_INITIAL_STATES = frozenset({"READY", "RUNNING"})
START_STATUSES = frozenset({"AVAILABLE", "REFUSED"})
START_REFUSAL_REASONS = frozenset(
    {
        "RESOLUTION_NOT_AVAILABLE",
        "RESOLUTION_CHANGED",
        "COMPONENT_NOT_FOUND",
        "COMPONENT_DIGEST_MISMATCH",
        "INVALID_TRAINING_OPTIONS",
        "CURRICULUM_CONFLICT",
        "OBJECTIVE_EXCEEDS_DURATION",
    }
)

_RUN_ID = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_CURSOR_ID = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_FRAME_ID = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_MARKET_STATE_ID = re.compile(r"simulation-market-state-[0-9a-f]{24}\Z")
_START_RESULT_ID = re.compile(r"simulation-start-result-[0-9a-f]{24}\Z")

_OBJECTIVE_FIELDS = frozenset(
    {
        "objective_type",
        "target_quantity",
        "time_limit_us",
        "preferred_slippage_ticks",
    }
)
_TRAINING_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "quantity_options",
        "initial_quantity",
        "layout_ref",
        "strategy_ref",
        "objective",
        "curriculum_drill_ref",
        "initial_run_state",
        "observation_policy_ref",
    }
)
_START_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "source_run_id",
        "run_request_sha256",
        "initial_frame",
        "refusal",
    }
)
_START_REFUSAL_FIELDS = frozenset({"reason_code", "explanation"})
_FRAME_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "frame_id",
        "frame_sequence",
        "source_run_id",
        "run_request_sha256",
        "resolved_configuration_sha256",
        "profile_ref",
        "cursor",
        "market_state",
        "instrument",
        "clock",
        "book",
        "recent_trades",
        "working_orders",
        "account",
        "strategy",
        "objective",
        "diagnostics",
        "metrics",
        "status_message",
        "status_role",
        "provenance",
    }
)
_CURSOR_FIELDS = frozenset(
    {
        "cursor_id",
        "source_run_id",
        "simulation_time_us",
        "duration_us",
        "run_state",
        "input_sequence",
        "flow_sequence",
        "exchange_event_sequence",
        "trade_sequence",
    }
)
_MARKET_FIELDS = frozenset(
    {"market_state_id", "market_state_time_us", "book_state_sha256"}
)
_INSTRUMENT_FIELDS = frozenset(
    {
        "instrument_id",
        "symbol",
        "display_name",
        "venue_labels",
        "tick_numerator",
        "tick_denominator",
        "price_precision",
        "lot_size",
    }
)
_CLOCK_FIELDS = frozenset(
    {
        "time_basis",
        "session_origin_time_us",
        "display_precision_us",
        "cursor_label",
        "intraday_phase",
    }
)
_BOOK_FIELDS = frozenset({"bids", "asks"})
_LEVEL_FIELDS = frozenset(
    {
        "price_ticks",
        "display_price",
        "aggregate_quantity",
        "player_quantity",
        "first_player_queue_ahead",
    }
)
_QUEUE_FIELDS = frozenset({"availability", "quantity", "reason"})
_TRADE_FIELDS = frozenset(
    {
        "trade_sequence",
        "exchange_event_sequence",
        "trade_id",
        "simulation_time_us",
        "price_ticks",
        "display_price",
        "quantity",
        "aggressor_side",
    }
)
_WORKING_ORDER_FIELDS = frozenset(
    {
        "order_id",
        "side",
        "price_ticks",
        "display_price",
        "remaining_quantity",
        "filled_quantity",
        "resting_sequence",
        "queue_ahead",
    }
)
_ACCOUNT_FIELDS = frozenset(
    {
        "selected_quantity",
        "position",
        "bought_quantity",
        "sold_quantity",
        "working_order_count",
    }
)
_STRATEGY_FIELDS = frozenset(
    {
        "configured",
        "strategy_kind",
        "traffic_light",
        "traffic_setup",
        "strategy_state",
        "entry_permission",
        "exit_permission",
        "reason",
    }
)
_OBJECTIVE_PROJECTION_FIELDS = frozenset(
    {
        "configured",
        "objective_type",
        "target_quantity",
        "completed_quantity",
        "completion_ppm",
        "display_completion",
        "time_limit_us",
        "preferred_slippage_ticks",
        "complete",
        "completion_time_us",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "metric_id",
        "label",
        "availability",
        "scaled_value",
        "display_value",
        "scale",
        "unit",
        "sample_count",
        "aggregation_scope",
        "window_us",
        "heuristic",
        "as_of_exchange_event_sequence",
        "unavailable_reason",
        "semantic_role",
    }
)
_DIAGNOSTIC_FIELDS = frozenset(
    {
        "diagnostic_id",
        "label",
        "availability",
        "status",
        "display_value",
        "unit",
        "explanation",
        "as_of_exchange_event_sequence",
        "unavailable_reason",
        "semantic_role",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {
        "classification",
        "real_market_data",
        "matching_engine_derived",
        "generation_method",
        "level2_origin",
        "profile_sha256",
        "resolved_configuration_sha256",
        "run_request_sha256",
        "observation_policy_ref",
        "display_label",
    }
)


class SimulationStartRefusal(Exception):
    """Internal typed refusal raised before a run handle is published."""

    def __init__(self, reason_code: str, explanation: str) -> None:
        super().__init__(explanation)
        self.reason_code = _enum(reason_code, START_REFUSAL_REASONS, "start refusal")
        self.explanation = _text(explanation, "start refusal explanation")


def _optional_integer(
    value: object,
    label: str,
    *,
    minimum: int | None = 0,
) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum=minimum)


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _prefixed_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    result = _text(value, label)
    if pattern.fullmatch(result) is None:
        raise ValueError(f"{label} has an invalid V1 form")
    return result


@dataclass(frozen=True, slots=True)
class ObjectiveDefinitionV1:
    objective_type: str
    target_quantity: int
    time_limit_us: int
    preferred_slippage_ticks: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ObjectiveDefinitionV1:
        root = _object(_snapshot(payload), "simulation objective")
        _exact(root, _OBJECTIVE_FIELDS, "simulation objective")
        objective_type = _enum(
            root["objective_type"],
            frozenset(item.value for item in ObjectiveType),
            "simulation objective.objective_type",
        )
        target = _integer(
            root["target_quantity"], "simulation objective.target_quantity", minimum=0
        )
        if objective_type == "OBSERVE_ONLY":
            if target != 0:
                raise ValueError("OBSERVE_ONLY objective target must be zero")
        elif target == 0:
            raise ValueError("trading objective target must be positive")
        return cls(
            objective_type,
            target,
            _positive_integer(root["time_limit_us"], "simulation objective.time_limit_us"),
            _integer(
                root["preferred_slippage_ticks"],
                "simulation objective.preferred_slippage_ticks",
                minimum=0,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_type": self.objective_type,
            "target_quantity": self.target_quantity,
            "time_limit_us": self.time_limit_us,
            "preferred_slippage_ticks": self.preferred_slippage_ticks,
        }

    def to_session_objective(self) -> SessionObjective:
        return SessionObjective(
            ObjectiveType(self.objective_type),
            self.target_quantity,
            self.time_limit_us,
            self.preferred_slippage_ticks,
        )


@dataclass(frozen=True, slots=True)
class SimulationTrainingOptionsV1:
    quantity_options: tuple[int, ...]
    initial_quantity: int
    layout_ref: SimulationComponentRefV1
    strategy_ref: SimulationComponentRefV1 | None
    objective: ObjectiveDefinitionV1 | None
    curriculum_drill_ref: SimulationComponentRefV1 | None
    initial_run_state: str
    observation_policy_ref: SimulationComponentRefV1

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationTrainingOptionsV1:
        root = _object(_snapshot(payload), "simulation training options")
        _exact(root, _TRAINING_FIELDS, "simulation training options")
        if root["schema_id"] != TRAINING_OPTIONS_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation training options schema is unsupported")
        quantities = tuple(
            _positive_integer(item, f"simulation training options.quantity_options[{index}]")
            for index, item in enumerate(
                _array(root["quantity_options"], "simulation training options.quantity_options")
            )
        )
        if not quantities or any(left >= right for left, right in zip(quantities, quantities[1:])):
            raise ValueError("training quantities must be strictly ascending and unique")
        initial_quantity = _positive_integer(
            root["initial_quantity"], "simulation training options.initial_quantity"
        )
        if initial_quantity not in quantities:
            raise ValueError("initial quantity must be an advertised quantity")
        return cls(
            quantities,
            initial_quantity,
            SimulationComponentRefV1.from_dict(
                _object(root["layout_ref"], "simulation training options.layout_ref"),
                expected_kind="HOTKEY_LAYOUT",
            ),
            None
            if root["strategy_ref"] is None
            else SimulationComponentRefV1.from_dict(
                _object(root["strategy_ref"], "simulation training options.strategy_ref"),
                expected_kind="STRATEGY_DEFINITION",
            ),
            None
            if root["objective"] is None
            else ObjectiveDefinitionV1.from_dict(
                _object(root["objective"], "simulation training options.objective")
            ),
            None
            if root["curriculum_drill_ref"] is None
            else SimulationComponentRefV1.from_dict(
                _object(
                    root["curriculum_drill_ref"],
                    "simulation training options.curriculum_drill_ref",
                ),
                expected_kind="CURRICULUM_DRILL",
            ),
            _enum(
                root["initial_run_state"],
                START_INITIAL_STATES,
                "simulation training options.initial_run_state",
            ),
            SimulationComponentRefV1.from_dict(
                _object(
                    root["observation_policy_ref"],
                    "simulation training options.observation_policy_ref",
                ),
                expected_kind="OBSERVATION_POLICY",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": TRAINING_OPTIONS_SCHEMA_ID,
            "schema_version": 1,
            "quantity_options": list(self.quantity_options),
            "initial_quantity": self.initial_quantity,
            "layout_ref": self.layout_ref.as_dict(),
            "strategy_ref": None if self.strategy_ref is None else self.strategy_ref.as_dict(),
            "objective": None if self.objective is None else self.objective.as_dict(),
            "curriculum_drill_ref": (
                None
                if self.curriculum_drill_ref is None
                else self.curriculum_drill_ref.as_dict()
            ),
            "initial_run_state": self.initial_run_state,
            "observation_policy_ref": self.observation_policy_ref.as_dict(),
        }

    def validate_against(
        self,
        catalog: SimulationTrainingResourceCatalogV1,
        *,
        duration_us: int,
    ) -> Mapping[str, object]:
        defaults = catalog.defaults
        if self.quantity_options != tuple(defaults["quantity_options"]):
            raise SimulationStartRefusal(
                "INVALID_TRAINING_OPTIONS",
                "Training quantities differ from the catalog-authored options.",
            )
        if not _contains_ref(catalog.layouts, "layout_ref", self.layout_ref):
            raise SimulationStartRefusal(
                "INVALID_TRAINING_OPTIONS", "The selected hotkey layout is unavailable."
            )
        policy = _row_for_ref(
            catalog.observation_policies,
            "policy_ref",
            self.observation_policy_ref,
            "The selected observation policy is unavailable.",
        )
        if self.strategy_ref is not None and not _contains_ref(
            catalog.strategies, "strategy_ref", self.strategy_ref
        ):
            raise SimulationStartRefusal(
                "INVALID_TRAINING_OPTIONS", "The selected strategy is unavailable."
            )
        if self.curriculum_drill_ref is not None and not _contains_ref(
            catalog.curriculum_drills,
            "curriculum_drill_ref",
            self.curriculum_drill_ref,
        ):
            raise SimulationStartRefusal(
                "CURRICULUM_CONFLICT", "The selected curriculum drill is unavailable."
            )
        if self.objective is not None and self.objective.time_limit_us > duration_us:
            raise SimulationStartRefusal(
                "OBJECTIVE_EXCEEDS_DURATION",
                "The objective time limit exceeds the resolved run duration.",
            )
        return policy


def _contains_ref(
    rows: tuple[Mapping[str, object], ...],
    field: str,
    reference: SimulationComponentRefV1,
) -> bool:
    return any(SimulationComponentRefV1.from_dict(row[field]) == reference for row in rows)


def _row_for_ref(
    rows: tuple[Mapping[str, object], ...],
    field: str,
    reference: SimulationComponentRefV1,
    explanation: str,
) -> Mapping[str, object]:
    for row in rows:
        if SimulationComponentRefV1.from_dict(row[field]) == reference:
            return row
    raise SimulationStartRefusal("INVALID_TRAINING_OPTIONS", explanation)


def _validate_queue(value: object, label: str, *, level: bool) -> dict[str, object]:
    root = _object(value, label)
    _exact(root, _QUEUE_FIELDS, label)
    availability = _enum(
        root["availability"],
        frozenset({"AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}),
        f"{label}.availability",
    )
    quantity = _optional_integer(root["quantity"], f"{label}.quantity")
    reason = _optional_text(root["reason"], f"{label}.reason")
    if availability == "AVAILABLE":
        if quantity is None or reason is not None:
            raise ValueError(f"{label} AVAILABLE nullability is invalid")
    elif availability == "UNAVAILABLE":
        if quantity is not None or reason not in {
            "HIDDEN_BY_OBSERVATION_POLICY",
            "CAPABILITY_NOT_AVAILABLE",
        }:
            raise ValueError(f"{label} UNAVAILABLE fields are invalid")
    else:
        if not level or quantity is not None or reason != "NO_PLAYER_ORDER_AT_LEVEL":
            raise ValueError(f"{label} NOT_APPLICABLE fields are invalid")
    return {"availability": availability, "quantity": quantity, "reason": reason}


def _display_price(
    price_ticks: int,
    tick_numerator: int,
    tick_denominator: int,
    precision: int,
) -> str:
    scaled = price_ticks * tick_numerator * (10**precision)
    if scaled % tick_denominator:
        raise ValueError("instrument precision cannot exactly display its tick size")
    units = scaled // tick_denominator
    if precision == 0:
        return str(units)
    power = 10**precision
    return f"{units // power}.{units % power:0{precision}d}"


def _validate_instrument(value: object) -> dict[str, object]:
    label = "simulation frame.instrument"
    root = _object(value, label)
    _exact(root, _INSTRUMENT_FIELDS, label)
    numerator = _positive_integer(root["tick_numerator"], f"{label}.tick_numerator")
    denominator = _positive_integer(root["tick_denominator"], f"{label}.tick_denominator")
    precision = _integer(root["price_precision"], f"{label}.price_precision", minimum=0)
    if math.gcd(numerator, denominator) != 1 or (10**precision) % denominator:
        raise ValueError("instrument tick fraction is not canonical at its precision")
    return {
        "instrument_id": _identifier(root["instrument_id"], f"{label}.instrument_id"),
        "symbol": _text(root["symbol"], f"{label}.symbol"),
        "display_name": _text(root["display_name"], f"{label}.display_name"),
        "venue_labels": [
            _text(item, f"{label}.venue_labels[{index}]")
            for index, item in enumerate(_array(root["venue_labels"], f"{label}.venue_labels"))
        ],
        "tick_numerator": numerator,
        "tick_denominator": denominator,
        "price_precision": precision,
        "lot_size": _positive_integer(root["lot_size"], f"{label}.lot_size"),
    }


def _validate_book(value: object, instrument: Mapping[str, object]) -> dict[str, object]:
    label = "simulation frame.book"
    root = _object(value, label)
    _exact(root, _BOOK_FIELDS, label)
    result: dict[str, object] = {}
    for side, descending in (("bids", True), ("asks", False)):
        rows: list[dict[str, object]] = []
        for index, item in enumerate(_array(root[side], f"{label}.{side}")):
            row_label = f"{label}.{side}[{index}]"
            row = _object(item, row_label)
            _exact(row, _LEVEL_FIELDS, row_label)
            ticks = _positive_integer(row["price_ticks"], f"{row_label}.price_ticks")
            display = _text(row["display_price"], f"{row_label}.display_price")
            expected_display = _display_price(
                ticks,
                int(instrument["tick_numerator"]),
                int(instrument["tick_denominator"]),
                int(instrument["price_precision"]),
            )
            if display != expected_display:
                raise ValueError(f"{row_label}.display_price differs from integer ticks")
            aggregate = _integer(
                row["aggregate_quantity"], f"{row_label}.aggregate_quantity", minimum=0
            )
            player = _integer(
                row["player_quantity"], f"{row_label}.player_quantity", minimum=0
            )
            if player > aggregate:
                raise ValueError(f"{row_label} player quantity exceeds aggregate")
            queue = _validate_queue(
                row["first_player_queue_ahead"],
                f"{row_label}.first_player_queue_ahead",
                level=True,
            )
            if player == 0 and queue["availability"] != "NOT_APPLICABLE":
                raise ValueError(f"{row_label} without player quantity needs NOT_APPLICABLE queue")
            if player > 0 and queue["availability"] == "NOT_APPLICABLE":
                raise ValueError(f"{row_label} with player quantity needs an applicable queue")
            rows.append(
                {
                    "price_ticks": ticks,
                    "display_price": display,
                    "aggregate_quantity": aggregate,
                    "player_quantity": player,
                    "first_player_queue_ahead": queue,
                }
            )
        prices = [int(row["price_ticks"]) for row in rows]
        expected = sorted(prices, reverse=descending)
        if prices != expected or len(prices) != len(set(prices)):
            raise ValueError(f"{label}.{side} is not unique best-to-worst order")
        result[side] = rows
    return result


def _validate_cursor(value: object, source_run_id: str) -> dict[str, object]:
    label = "simulation frame.cursor"
    root = _object(value, label)
    _exact(root, _CURSOR_FIELDS, label)
    cursor_run = _prefixed_id(root["source_run_id"], _RUN_ID, f"{label}.source_run_id")
    if cursor_run != source_run_id:
        raise ValueError("frame and cursor source runs differ")
    time_us = _integer(root["simulation_time_us"], f"{label}.simulation_time_us", minimum=0)
    duration_us = _positive_integer(root["duration_us"], f"{label}.duration_us")
    if time_us > duration_us:
        raise ValueError("simulation cursor exceeds duration")
    run_state = _enum(root["run_state"], RUN_STATES, f"{label}.run_state")
    if (run_state == "READY" and time_us != 0) or (
        run_state == "COMPLETE" and time_us != duration_us
    ) or (run_state in {"RUNNING", "PAUSED"} and time_us >= duration_us):
        raise ValueError("simulation cursor state and time disagree")
    basis = {
        "source_run_id": cursor_run,
        "simulation_time_us": time_us,
        "duration_us": duration_us,
        "run_state": run_state,
        "input_sequence": _integer(root["input_sequence"], f"{label}.input_sequence", minimum=0),
        "flow_sequence": _integer(root["flow_sequence"], f"{label}.flow_sequence", minimum=0),
        "exchange_event_sequence": _integer(
            root["exchange_event_sequence"], f"{label}.exchange_event_sequence", minimum=0
        ),
        "trade_sequence": _integer(root["trade_sequence"], f"{label}.trade_sequence", minimum=0),
    }
    cursor_id = _prefixed_id(root["cursor_id"], _CURSOR_ID, f"{label}.cursor_id")
    expected_id = f"simulation-cursor-{canonical_digest(basis)[:24]}"
    if cursor_id != expected_id:
        raise SimulationContractIntegrityError("simulation cursor ID does not match its content")
    return {"cursor_id": cursor_id, **basis}


def _validate_clock(value: object, cursor: Mapping[str, object]) -> dict[str, object]:
    label = "simulation frame.clock"
    root = _object(value, label)
    _exact(root, _CLOCK_FIELDS, label)
    basis = _enum(
        root["time_basis"], frozenset({"SIMULATION_ELAPSED", "SESSION_CLOCK"}), f"{label}.time_basis"
    )
    origin = _optional_integer(root["session_origin_time_us"], f"{label}.session_origin_time_us")
    if (basis == "SIMULATION_ELAPSED") != (origin is None):
        raise ValueError("simulation clock basis and origin disagree")
    precision = _power_of_ten(root["display_precision_us"], f"{label}.display_precision_us")
    if precision > 1_000_000 or 1_000_000 % precision:
        raise ValueError("clock display precision must divide one second")
    return {
        "time_basis": basis,
        "session_origin_time_us": origin,
        "display_precision_us": precision,
        "cursor_label": _text(root["cursor_label"], f"{label}.cursor_label"),
        "intraday_phase": _enum(root["intraday_phase"], INTRADAY_PHASES, f"{label}.intraday_phase"),
    }


def _validate_trades(
    value: object,
    cursor: Mapping[str, object],
    instrument: Mapping[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, item in enumerate(_array(value, "simulation frame.recent_trades")):
        label = f"simulation frame.recent_trades[{index}]"
        row = _object(item, label)
        _exact(row, _TRADE_FIELDS, label)
        sequence = _positive_integer(row["trade_sequence"], f"{label}.trade_sequence")
        event_sequence = _positive_integer(
            row["exchange_event_sequence"], f"{label}.exchange_event_sequence"
        )
        if event_sequence > cursor["exchange_event_sequence"]:
            raise ValueError(f"{label} exceeds the cursor exchange sequence")
        time_us = _integer(row["simulation_time_us"], f"{label}.simulation_time_us", minimum=0)
        if time_us > cursor["simulation_time_us"]:
            raise ValueError(f"{label} occurs after the cursor")
        ticks = _positive_integer(row["price_ticks"], f"{label}.price_ticks")
        display = _text(row["display_price"], f"{label}.display_price")
        if display != _display_price(
            ticks,
            int(instrument["tick_numerator"]),
            int(instrument["tick_denominator"]),
            int(instrument["price_precision"]),
        ):
            raise ValueError(f"{label}.display_price differs from integer ticks")
        rows.append(
            {
                "trade_sequence": sequence,
                "exchange_event_sequence": event_sequence,
                "trade_id": _identifier(row["trade_id"], f"{label}.trade_id"),
                "simulation_time_us": time_us,
                "price_ticks": ticks,
                "display_price": display,
                "quantity": _positive_integer(row["quantity"], f"{label}.quantity"),
                "aggressor_side": _enum(
                    row["aggressor_side"], frozenset({"BUY", "SELL"}), f"{label}.aggressor_side"
                ),
            }
        )
    total = int(cursor["trade_sequence"])
    expected_sequences = list(range(max(1, total - 255), total + 1)) if total else []
    if [int(row["trade_sequence"]) for row in rows] != expected_sequences:
        raise ValueError("recent trade window is not the canonical oldest-to-newest tail")
    if len({str(row["trade_id"]) for row in rows}) != len(rows):
        raise ValueError("recent trade IDs must be unique")
    return rows


def _validate_working_orders(value: object, instrument: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, item in enumerate(_array(value, "simulation frame.working_orders")):
        label = f"simulation frame.working_orders[{index}]"
        row = _object(item, label)
        _exact(row, _WORKING_ORDER_FIELDS, label)
        ticks = _positive_integer(row["price_ticks"], f"{label}.price_ticks")
        display = _text(row["display_price"], f"{label}.display_price")
        if display != _display_price(
            ticks,
            int(instrument["tick_numerator"]),
            int(instrument["tick_denominator"]),
            int(instrument["price_precision"]),
        ):
            raise ValueError(f"{label}.display_price differs from integer ticks")
        rows.append(
            {
                "order_id": _identifier(row["order_id"], f"{label}.order_id"),
                "side": _enum(row["side"], frozenset({"BUY", "SELL"}), f"{label}.side"),
                "price_ticks": ticks,
                "display_price": display,
                "remaining_quantity": _positive_integer(
                    row["remaining_quantity"], f"{label}.remaining_quantity"
                ),
                "filled_quantity": _integer(
                    row["filled_quantity"], f"{label}.filled_quantity", minimum=0
                ),
                "resting_sequence": _positive_integer(
                    row["resting_sequence"], f"{label}.resting_sequence"
                ),
                "queue_ahead": _validate_queue(row["queue_ahead"], f"{label}.queue_ahead", level=False),
            }
        )
    ordering = [(int(row["resting_sequence"]), str(row["order_id"])) for row in rows]
    if ordering != sorted(ordering) or len({row["order_id"] for row in rows}) != len(rows):
        raise ValueError("working orders are not unique resting order")
    return rows


def _validate_account(value: object, working_count: int) -> dict[str, object]:
    label = "simulation frame.account"
    root = _object(value, label)
    _exact(root, _ACCOUNT_FIELDS, label)
    bought = _integer(root["bought_quantity"], f"{label}.bought_quantity", minimum=0)
    sold = _integer(root["sold_quantity"], f"{label}.sold_quantity", minimum=0)
    position = _integer(root["position"], f"{label}.position")
    count = _integer(root["working_order_count"], f"{label}.working_order_count", minimum=0)
    if position != bought - sold or count != working_count:
        raise ValueError("account conservation or working-order count differs")
    return {
        "selected_quantity": _positive_integer(root["selected_quantity"], f"{label}.selected_quantity"),
        "position": position,
        "bought_quantity": bought,
        "sold_quantity": sold,
        "working_order_count": count,
    }


def _validate_strategy(value: object) -> dict[str, object]:
    label = "simulation frame.strategy"
    root = _object(value, label)
    _exact(root, _STRATEGY_FIELDS, label)
    configured = root["configured"]
    if type(configured) is not bool:
        raise ValueError(f"{label}.configured must be boolean")
    kind = None if root["strategy_kind"] is None else _enum(
        root["strategy_kind"], frozenset({"TRAFFIC_LIGHT", "STATE_MACHINE"}), f"{label}.strategy_kind"
    )
    light = _enum(
        root["traffic_light"],
        frozenset({"GREEN", "WAIT", "RED", "UNCONFIGURED"}),
        f"{label}.traffic_light",
    )
    setup = _optional_text(root["traffic_setup"], f"{label}.traffic_setup")
    state = _optional_text(root["strategy_state"], f"{label}.strategy_state")
    entry = _enum(
        root["entry_permission"], frozenset({"ALLOW", "DENY", "UNRESTRICTED"}), f"{label}.entry_permission"
    )
    exit_permission = _enum(
        root["exit_permission"], frozenset({"ALLOW", "DENY", "UNRESTRICTED"}), f"{label}.exit_permission"
    )
    if not configured:
        if (kind, setup, state, light, entry, exit_permission) != (
            None,
            None,
            None,
            "UNCONFIGURED",
            "UNRESTRICTED",
            "UNRESTRICTED",
        ):
            raise ValueError("unconfigured strategy projection is inconsistent")
    elif kind == "TRAFFIC_LIGHT":
        if setup is None or state is not None or light == "UNCONFIGURED" or entry != "UNRESTRICTED" or exit_permission != "UNRESTRICTED":
            raise ValueError("traffic-light strategy projection is inconsistent")
    elif kind == "STATE_MACHINE":
        if setup is None or state is None or light == "UNCONFIGURED" or entry == "UNRESTRICTED" or exit_permission == "UNRESTRICTED":
            raise ValueError("state-machine strategy projection is inconsistent")
    else:
        raise ValueError("configured strategy projection requires a strategy kind")
    return {
        "configured": configured,
        "strategy_kind": kind,
        "traffic_light": light,
        "traffic_setup": setup,
        "strategy_state": state,
        "entry_permission": entry,
        "exit_permission": exit_permission,
        "reason": _text(root["reason"], f"{label}.reason"),
    }


def _completion_display(completion_ppm: int) -> str:
    hundredth_percent = (completion_ppm + 50) // 100
    whole, fractional = divmod(hundredth_percent, 100)
    if fractional == 0:
        return f"{whole}%"
    return f"{whole}.{fractional:02d}".rstrip("0") + "%"


def _validate_objective_projection(value: object) -> dict[str, object]:
    label = "simulation frame.objective"
    root = _object(value, label)
    _exact(root, _OBJECTIVE_PROJECTION_FIELDS, label)
    configured = root["configured"]
    complete = root["complete"]
    if type(configured) is not bool or type(complete) is not bool:
        raise ValueError("objective configured and complete fields must be booleans")
    objective_type = None if root["objective_type"] is None else _enum(
        root["objective_type"],
        frozenset(item.value for item in ObjectiveType),
        f"{label}.objective_type",
    )
    target = _integer(root["target_quantity"], f"{label}.target_quantity", minimum=0)
    completed = _integer(root["completed_quantity"], f"{label}.completed_quantity", minimum=0)
    completion_ppm = _integer(root["completion_ppm"], f"{label}.completion_ppm", minimum=0)
    if completion_ppm > 1_000_000:
        raise ValueError("objective completion exceeds one million ppm")
    time_limit = _optional_integer(root["time_limit_us"], f"{label}.time_limit_us", minimum=1)
    slippage = _optional_integer(
        root["preferred_slippage_ticks"], f"{label}.preferred_slippage_ticks"
    )
    completion_time = _optional_integer(root["completion_time_us"], f"{label}.completion_time_us")
    if not configured:
        if (
            objective_type is not None
            or target != 0
            or completed != 0
            or completion_ppm != 0
            or root["display_completion"] != "0%"
            or time_limit is not None
            or slippage is not None
            or complete
            or completion_time is not None
        ):
            raise ValueError("unconfigured objective projection is inconsistent")
    else:
        if objective_type is None or time_limit is None or slippage is None:
            raise ValueError("configured objective projection omits its definition")
        if objective_type == "OBSERVE_ONLY":
            expected_ppm = 1_000_000
            expected_complete = True
            if target != 0 or completed != 0 or completion_time != 0:
                raise ValueError("OBSERVE_ONLY objective projection is inconsistent")
        else:
            if target <= 0 or completed > target:
                raise ValueError("trading objective quantities are inconsistent")
            expected_ppm = min(
                1_000_000,
                (2 * completed * 1_000_000 + target) // (2 * target),
            )
            expected_complete = completed == target
            if expected_complete != (completion_time is not None):
                raise ValueError("objective completion time disagrees with completion")
        if complete != expected_complete or completion_ppm != expected_ppm:
            raise ValueError("objective completion fields disagree")
        if root["display_completion"] != _completion_display(completion_ppm):
            raise ValueError("objective display completion differs from its ppm value")
    return {
        "configured": configured,
        "objective_type": objective_type,
        "target_quantity": target,
        "completed_quantity": completed,
        "completion_ppm": completion_ppm,
        "display_completion": _text(root["display_completion"], f"{label}.display_completion"),
        "time_limit_us": time_limit,
        "preferred_slippage_ticks": slippage,
        "complete": complete,
        "completion_time_us": completion_time,
    }


def _validate_metric(value: object, index: int, exchange_sequence: int) -> dict[str, object]:
    label = f"simulation frame.metrics[{index}]"
    root = _object(value, label)
    _exact(root, _METRIC_FIELDS, label)
    availability = _enum(
        root["availability"], frozenset({"AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}), f"{label}.availability"
    )
    scaled = _optional_integer(root["scaled_value"], f"{label}.scaled_value", minimum=None)
    display = _optional_text(root["display_value"], f"{label}.display_value")
    reason = _optional_text(root["unavailable_reason"], f"{label}.unavailable_reason")
    if availability == "AVAILABLE":
        if scaled is None or display is None or reason is not None:
            raise ValueError(f"{label} AVAILABLE nullability is invalid")
    elif scaled is not None or display is not None or reason is None:
        raise ValueError(f"{label} unavailable nullability is invalid")
    scope = _enum(
        root["aggregation_scope"],
        frozenset({"INSTANTANEOUS", "ROLLING_WINDOW", "RUN_TO_CURSOR"}),
        f"{label}.aggregation_scope",
    )
    window = _optional_integer(root["window_us"], f"{label}.window_us", minimum=1)
    if (scope == "ROLLING_WINDOW") != (window is not None):
        raise ValueError(f"{label} window and aggregation scope disagree")
    heuristic = root["heuristic"]
    if type(heuristic) is not bool:
        raise ValueError(f"{label}.heuristic must be boolean")
    as_of = _integer(
        root["as_of_exchange_event_sequence"],
        f"{label}.as_of_exchange_event_sequence",
        minimum=0,
    )
    if as_of > exchange_sequence:
        raise ValueError(f"{label} is newer than the frame cursor")
    role = _enum(
        root["semantic_role"],
        frozenset({"NEUTRAL", "BID", "ASK", "POSITIVE", "NEGATIVE", "WARNING", "UNAVAILABLE"}),
        f"{label}.semantic_role",
    )
    if availability != "AVAILABLE" and role != "UNAVAILABLE":
        raise ValueError(f"{label} unavailable row needs UNAVAILABLE semantic role")
    return {
        "metric_id": _identifier(root["metric_id"], f"{label}.metric_id"),
        "label": _text(root["label"], f"{label}.label"),
        "availability": availability,
        "scaled_value": scaled,
        "display_value": display,
        "scale": _power_of_ten(root["scale"], f"{label}.scale"),
        "unit": _text(root["unit"], f"{label}.unit", allow_empty=True),
        "sample_count": _integer(root["sample_count"], f"{label}.sample_count", minimum=0),
        "aggregation_scope": scope,
        "window_us": window,
        "heuristic": heuristic,
        "as_of_exchange_event_sequence": as_of,
        "unavailable_reason": reason,
        "semantic_role": role,
    }


def _validate_diagnostic(value: object, index: int, exchange_sequence: int) -> dict[str, object]:
    label = f"simulation frame.diagnostics[{index}]"
    root = _object(value, label)
    _exact(root, _DIAGNOSTIC_FIELDS, label)
    availability = _enum(
        root["availability"], frozenset({"AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}), f"{label}.availability"
    )
    status = _enum(
        root["status"],
        frozenset({"PASS", "WARN", "FAIL", "INFO", "UNAVAILABLE", "NOT_APPLICABLE"}),
        f"{label}.status",
    )
    display = _optional_text(root["display_value"], f"{label}.display_value")
    unit = _optional_text(root["unit"], f"{label}.unit")
    reason = _optional_text(root["unavailable_reason"], f"{label}.unavailable_reason")
    if availability == "AVAILABLE":
        if status not in {"PASS", "WARN", "FAIL", "INFO"} or display is None or reason is not None:
            raise ValueError(f"{label} available diagnostic fields disagree")
    else:
        expected_status = "UNAVAILABLE" if availability == "UNAVAILABLE" else "NOT_APPLICABLE"
        if status != expected_status or display is not None or reason is None:
            raise ValueError(f"{label} unavailable diagnostic fields disagree")
    as_of = _integer(
        root["as_of_exchange_event_sequence"],
        f"{label}.as_of_exchange_event_sequence",
        minimum=0,
    )
    if as_of > exchange_sequence:
        raise ValueError(f"{label} is newer than the frame cursor")
    role = _enum(
        root["semantic_role"],
        frozenset({"NEUTRAL", "BID", "ASK", "POSITIVE", "NEGATIVE", "WARNING", "UNAVAILABLE"}),
        f"{label}.semantic_role",
    )
    if availability != "AVAILABLE" and role != "UNAVAILABLE":
        raise ValueError(f"{label} unavailable diagnostic needs UNAVAILABLE role")
    if status in {"WARN", "FAIL"} and role != "WARNING":
        raise ValueError(f"{label} warning/failure diagnostic needs WARNING role")
    return {
        "diagnostic_id": _identifier(root["diagnostic_id"], f"{label}.diagnostic_id"),
        "label": _text(root["label"], f"{label}.label"),
        "availability": availability,
        "status": status,
        "display_value": display,
        "unit": unit,
        "explanation": _text(root["explanation"], f"{label}.explanation"),
        "as_of_exchange_event_sequence": as_of,
        "unavailable_reason": reason,
        "semantic_role": role,
    }


def _validate_provenance(value: object) -> dict[str, object]:
    label = "simulation frame.provenance"
    root = _object(value, label)
    _exact(root, _PROVENANCE_FIELDS, label)
    sealed: dict[str, object] = {
        "classification": "SYNTHETIC_SIMULATION_ONLY",
        "real_market_data": False,
        "matching_engine_derived": True,
        "generation_method": "ORDER_FLOW_THROUGH_MATCHING_ENGINE",
        "level2_origin": "MATCHING_ENGINE_BOOK_STATE",
    }
    if any(root[field] != expected or type(root[field]) is not type(expected) for field, expected in sealed.items()):
        raise ValueError("simulation frame provenance violates the synthetic-only contract")
    return {
        **sealed,
        "profile_sha256": _digest(root["profile_sha256"], f"{label}.profile_sha256"),
        "resolved_configuration_sha256": _digest(
            root["resolved_configuration_sha256"], f"{label}.resolved_configuration_sha256"
        ),
        "run_request_sha256": _digest(root["run_request_sha256"], f"{label}.run_request_sha256"),
        "observation_policy_ref": SimulationComponentRefV1.from_dict(
            _object(root["observation_policy_ref"], f"{label}.observation_policy_ref"),
            expected_kind="OBSERVATION_POLICY",
        ).as_dict(),
        "display_label": _text(root["display_label"], f"{label}.display_label"),
    }


@dataclass(frozen=True, slots=True)
class SimulationFrameV1:
    frame_id: str
    frame_sequence: int
    source_run_id: str
    run_request_sha256: str
    resolved_configuration_sha256: str
    profile_ref: SimulationProfileRefV1
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationFrameV1:
        root = _object(_snapshot(payload), "simulation frame")
        _exact(root, _FRAME_FIELDS, "simulation frame")
        if root["schema_id"] != FRAME_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation frame schema is unsupported")
        source_run_id = _prefixed_id(root["source_run_id"], _RUN_ID, "simulation frame.source_run_id")
        run_request_sha256 = _digest(root["run_request_sha256"], "simulation frame.run_request_sha256")
        configuration_sha256 = _digest(
            root["resolved_configuration_sha256"],
            "simulation frame.resolved_configuration_sha256",
        )
        profile_ref = SimulationProfileRefV1.from_dict(
            _object(root["profile_ref"], "simulation frame.profile_ref")
        )
        cursor = _validate_cursor(root["cursor"], source_run_id)
        instrument = _validate_instrument(root["instrument"])
        book = _validate_book(root["book"], instrument)
        book_sha256 = canonical_digest(book)
        market = _object(root["market_state"], "simulation frame.market_state")
        _exact(market, _MARKET_FIELDS, "simulation frame.market_state")
        market_time = _integer(
            market["market_state_time_us"],
            "simulation frame.market_state.market_state_time_us",
            minimum=0,
        )
        if market_time > cursor["simulation_time_us"]:
            raise ValueError("market state time exceeds the cursor")
        recorded_book_sha256 = _digest(
            market["book_state_sha256"], "simulation frame.market_state.book_state_sha256"
        )
        if recorded_book_sha256 != book_sha256:
            raise SimulationContractIntegrityError("frame book digest does not match the book")
        market_id = _prefixed_id(
            market["market_state_id"],
            _MARKET_STATE_ID,
            "simulation frame.market_state.market_state_id",
        )
        expected_market_id = "simulation-market-state-" + canonical_digest(
            {
                "source_run_id": source_run_id,
                "market_state_time_us": market_time,
                "exchange_event_sequence": cursor["exchange_event_sequence"],
                "book_state_sha256": book_sha256,
            }
        )[:24]
        if market_id != expected_market_id:
            raise SimulationContractIntegrityError("market-state ID does not match its content")
        clock = _validate_clock(root["clock"], cursor)
        trades = _validate_trades(root["recent_trades"], cursor, instrument)
        working = _validate_working_orders(root["working_orders"], instrument)
        account = _validate_account(root["account"], len(working))
        strategy = _validate_strategy(root["strategy"])
        objective = _validate_objective_projection(root["objective"])
        diagnostics = [
            _validate_diagnostic(item, index, int(cursor["exchange_event_sequence"]))
            for index, item in enumerate(_array(root["diagnostics"], "simulation frame.diagnostics"))
        ]
        metrics = [
            _validate_metric(item, index, int(cursor["exchange_event_sequence"]))
            for index, item in enumerate(_array(root["metrics"], "simulation frame.metrics"))
        ]
        for rows, field in ((diagnostics, "diagnostic_id"), (metrics, "metric_id")):
            ids = [str(row[field]) for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"simulation frame {field}s must be unique")
        role = _enum(
            root["status_role"],
            frozenset({"NEUTRAL", "READY", "RUNNING", "PAUSED", "COMPLETE", "WARNING", "ERROR"}),
            "simulation frame.status_role",
        )
        if role not in {cursor["run_state"], "WARNING", "ERROR"}:
            raise ValueError("simulation frame status role disagrees with run state")
        provenance = _validate_provenance(root["provenance"])
        if (
            provenance["profile_sha256"] != profile_ref.profile_sha256
            or provenance["resolved_configuration_sha256"] != configuration_sha256
            or provenance["run_request_sha256"] != run_request_sha256
        ):
            raise SimulationContractIntegrityError("frame provenance identities differ from its roots")
        normalized = {
            "schema_id": FRAME_SCHEMA_ID,
            "schema_version": 1,
            "frame_id": _prefixed_id(root["frame_id"], _FRAME_ID, "simulation frame.frame_id"),
            "frame_sequence": _positive_integer(root["frame_sequence"], "simulation frame.frame_sequence"),
            "source_run_id": source_run_id,
            "run_request_sha256": run_request_sha256,
            "resolved_configuration_sha256": configuration_sha256,
            "profile_ref": profile_ref.as_dict(),
            "cursor": cursor,
            "market_state": {
                "market_state_id": market_id,
                "market_state_time_us": market_time,
                "book_state_sha256": book_sha256,
            },
            "instrument": instrument,
            "clock": clock,
            "book": book,
            "recent_trades": trades,
            "working_orders": working,
            "account": account,
            "strategy": strategy,
            "objective": objective,
            "diagnostics": diagnostics,
            "metrics": metrics,
            "status_message": _text(root["status_message"], "simulation frame.status_message", allow_empty=True),
            "status_role": role,
            "provenance": provenance,
        }
        frame_basis = {key: value for key, value in normalized.items() if key != "frame_id"}
        expected_frame_id = f"simulation-frame-{canonical_digest(frame_basis)[:24]}"
        if normalized["frame_id"] != expected_frame_id:
            raise SimulationContractIntegrityError("simulation frame ID does not match its content")
        return cls(
            normalized["frame_id"],
            normalized["frame_sequence"],
            source_run_id,
            run_request_sha256,
            configuration_sha256,
            profile_ref,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationStartRefusalV1:
    reason_code: str
    explanation: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationStartRefusalV1:
        root = _object(_snapshot(payload), "simulation start refusal")
        _exact(root, _START_REFUSAL_FIELDS, "simulation start refusal")
        return cls(
            _enum(root["reason_code"], START_REFUSAL_REASONS, "start refusal.reason_code"),
            _text(root["explanation"], "start refusal.explanation"),
        )

    def as_dict(self) -> dict[str, object]:
        return {"reason_code": self.reason_code, "explanation": self.explanation}


@dataclass(frozen=True, slots=True)
class SimulationStartResultV1:
    result_id: str
    status: str
    source_run_id: str | None
    run_request_sha256: str | None
    initial_frame: SimulationFrameV1 | None
    refusal: SimulationStartRefusalV1 | None

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        resolution: SimulationProfileResolutionV1 | None = None,
        training_options: SimulationTrainingOptionsV1 | None = None,
    ) -> SimulationStartResultV1:
        root = _object(_snapshot(payload), "simulation start result")
        _exact(root, _START_RESULT_FIELDS, "simulation start result")
        if root["schema_id"] != START_RESULT_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation start result schema is unsupported")
        status = _enum(root["status"], START_STATUSES, "simulation start result.status")
        if status == "AVAILABLE":
            if (
                root["source_run_id"] is None
                or root["run_request_sha256"] is None
                or root["initial_frame"] is None
                or root["refusal"] is not None
            ):
                raise ValueError("AVAILABLE start result nullability is invalid")
            source_run_id = _prefixed_id(
                root["source_run_id"], _RUN_ID, "simulation start result.source_run_id"
            )
            run_request_sha256 = _digest(
                root["run_request_sha256"], "simulation start result.run_request_sha256"
            )
            frame = SimulationFrameV1.from_dict(
                _object(root["initial_frame"], "simulation start result.initial_frame")
            )
            if (
                frame.frame_sequence != 1
                or frame.source_run_id != source_run_id
                or frame.run_request_sha256 != run_request_sha256
                or frame.as_dict()["cursor"]["simulation_time_us"] != 0
            ):
                raise SimulationContractIntegrityError("start result and initial frame identities differ")
            if resolution is not None:
                configuration = resolution.resolved_configuration
                if configuration is None or root["initial_frame"] is None:
                    raise SimulationContractIntegrityError("available start has no available resolution")
                if (
                    frame.resolved_configuration_sha256
                    != resolution.resolved_configuration_sha256
                    or frame.profile_ref != resolution.selection.profile_ref
                ):
                    raise SimulationContractIntegrityError("start frame differs from its resolution")
            if training_options is not None:
                frame_record = frame.as_dict()
                policy = frame_record["provenance"]["observation_policy_ref"]
                if policy != training_options.observation_policy_ref.as_dict():
                    raise SimulationContractIntegrityError("start frame observation policy differs")
                if frame_record["cursor"]["run_state"] != training_options.initial_run_state:
                    raise SimulationContractIntegrityError("start frame initial state differs")
                frame_objective = frame_record["objective"]
                if training_options.objective is None:
                    if frame_objective["configured"] is not False:
                        raise SimulationContractIntegrityError("start frame invented an objective")
                else:
                    expected_objective = training_options.objective.as_dict()
                    if any(
                        frame_objective[field] != expected_objective[field]
                        for field in _OBJECTIVE_FIELDS
                    ):
                        raise SimulationContractIntegrityError(
                            "start frame objective differs from training options"
                        )
                if resolution is not None and resolution.resolved_configuration_sha256 is not None:
                    expected_request_sha256 = canonical_digest(
                        {
                            "schema_id": RUN_REQUEST_SCHEMA_ID,
                            "schema_version": 1,
                            "resolved_configuration_sha256": (
                                resolution.resolved_configuration_sha256
                            ),
                            "training_options": training_options.as_dict(),
                        }
                    )
                    if run_request_sha256 != expected_request_sha256:
                        raise SimulationContractIntegrityError(
                            "start run-request digest differs from its inputs"
                        )
                    configuration = resolution.resolved_configuration
                    if configuration is None:
                        raise SimulationContractIntegrityError(
                            "start input resolution lost its configuration"
                        )
                    if (
                        frame_record["cursor"]["duration_us"] != configuration.duration_us
                        or frame_record["clock"]["intraday_phase"]
                        != configuration.intraday_phase
                    ):
                        raise SimulationContractIntegrityError(
                            "start frame duration or clock differs from its configuration"
                        )
            refusal = None
        else:
            if (
                root["source_run_id"] is not None
                or root["run_request_sha256"] is not None
                or root["initial_frame"] is not None
                or root["refusal"] is None
            ):
                raise ValueError("REFUSED start result nullability is invalid")
            source_run_id = None
            run_request_sha256 = None
            frame = None
            refusal = SimulationStartRefusalV1.from_dict(
                _object(root["refusal"], "simulation start result.refusal")
            )
        result_id = _prefixed_id(root["result_id"], _START_RESULT_ID, "simulation start result.result_id")
        basis = {key: value for key, value in root.items() if key != "result_id"}
        if result_id != f"simulation-start-result-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("start result ID does not match its content")
        return cls(result_id, status, source_run_id, run_request_sha256, frame, refusal)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": START_RESULT_SCHEMA_ID,
            "schema_version": 1,
            "result_id": self.result_id,
            "status": self.status,
            "source_run_id": self.source_run_id,
            "run_request_sha256": self.run_request_sha256,
            "initial_frame": None if self.initial_frame is None else self.initial_frame.as_dict(),
            "refusal": None if self.refusal is None else self.refusal.as_dict(),
        }


__all__ = [
    "FRAME_SCHEMA_ID",
    "ObjectiveDefinitionV1",
    "RUN_REQUEST_SCHEMA_ID",
    "SimulationFrameV1",
    "SimulationStartResultV1",
    "SimulationStartRefusalV1",
    "SimulationTrainingOptionsV1",
    "START_RESULT_SCHEMA_ID",
    "TRAINING_OPTIONS_SCHEMA_ID",
]
