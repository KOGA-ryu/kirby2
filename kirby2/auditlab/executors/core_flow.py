"""Real simple/Hawkes synthetic-flow executor for generated audit cases."""

from __future__ import annotations

import math
from collections.abc import Mapping

from kirby2.exchange import OrderBook, OrderOwner, Side
from kirby2.immutable import thaw_json
from kirby2.scenarios import create_market_engine, get_scenario_definition
from kirby2.session import EventType, SimulationEvent
from kirby2.simulation import (
    FlowEvent,
    FlowEventFamily,
    LiquidityPreset,
    RegimeOrderFlow,
    ScenarioDimensions,
    VolumePreset,
)
from kirby2.simulation.comparison import create_flow_model
from kirby2.simulation.flow_models import FlowModel

from ..models import (
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExerciseRecord,
    ExerciseStatus,
    ExecutorLane,
    FailureKind,
    FailureObservation,
    GeneratedCaseResult,
    GeneratedConfiguration,
    canonical_sha256,
)


CORE_FLOW_RECORDING_TYPE = "CORE_FLOW_EVENT_TAPE"
_RECORDING_FIELDS = frozenset(
    {
        "configuration",
        "dimensions",
        "duration_us",
        "flow_events",
        "flow_model",
        "initial_exchange_event_count",
        "scenario_definition_sha256",
        "scenario_name",
    }
)
_FLOW_EVENT_FIELDS = frozenset(
    {
        "applied",
        "command",
        "exchange_event_end",
        "exchange_event_start",
        "family",
        "flow_sequence",
        "reason",
        "simulation_time_us",
    }
)
_FORBIDDEN_OBSERVABLE_FIELDS = frozenset(
    {
        "account_id",
        "command",
        "diagnostic",
        "future",
        "ground_truth",
        "hidden_quantity",
        "liquidity_source",
        "maker_order_id",
        "order_id",
        "owner",
        "priority_sequence",
        "reserve_quantity",
        "taker_order_id",
    }
)


class CoreFlowExecutor:
    """Execute only capabilities genuinely implemented by RegimeOrderFlow."""

    lane = ExecutorLane.CORE_FLOW

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        engine, dimensions, model, profile_id, definition_sha256 = _engine(
            configuration
        )
        engine.advance_to(configuration.duration_us)
        recording = _recording(
            configuration,
            engine,
            dimensions,
            model,
            profile_id,
            definition_sha256,
        )
        return _result(
            configuration,
            recording,
            engine,
            dimensions,
            model,
            profile_id,
            replay_mismatches=(),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("core-flow replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("core-flow replay received a different lane")
        if recording.recording_type != CORE_FLOW_RECORDING_TYPE:
            raise ValueError("unsupported core-flow recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("core-flow recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("core-flow recording fields are not exact")
        raw_configuration = payload["configuration"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("core-flow recording configuration must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        engine, dimensions, model, profile_id, definition_sha256 = _engine(
            configuration
        )
        engine.start()
        mismatches: list[str] = []
        expected_static = {
            "dimensions": dimensions.as_dict(),
            "duration_us": configuration.duration_us,
            "flow_model": _flow_model_identity(model, profile_id),
            "initial_exchange_event_count": engine.initial_exchange_event_count,
            "scenario_definition_sha256": definition_sha256,
            "scenario_name": configuration.regime.lower(),
        }
        for name, expected in expected_static.items():
            if payload[name] != expected:
                mismatches.append(f"recording_{name}_mismatch")
        raw_events = payload["flow_events"]
        if not isinstance(raw_events, list):
            raise TypeError("core-flow recording events must be an array")
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                raise TypeError("core-flow recording event must be an object")
            reference = _flow_event(raw_event)
            realized, _ = engine.apply_exogenous_event(reference)
            if realized.as_dict() != reference.as_dict():
                mismatches.append(
                    f"flow_event_{reference.sequence}_realization_mismatch"
                )
        engine.advance_exogenous_clock_to(configuration.duration_us)
        return _result(
            configuration,
            recording,
            engine,
            dimensions,
            model,
            profile_id,
            replay_mismatches=tuple(mismatches),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("core-flow executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("core-flow executor received a different lane")


def _engine(
    configuration: GeneratedConfiguration,
) -> tuple[
    RegimeOrderFlow,
    ScenarioDimensions,
    FlowModel,
    str | None,
    str,
]:
    definition = get_scenario_definition(configuration.regime.lower())
    model, profile_id = create_flow_model(
        configuration.flow_model,
        definition.regime,
    )
    engine, dimensions = create_market_engine(
        definition,
        seed=configuration.seed,
        relative_volume=VolumePreset(configuration.volume),
        liquidity=LiquidityPreset(configuration.liquidity),
        flow_model=model,
    )
    return (
        engine,
        dimensions,
        model,
        profile_id,
        canonical_sha256(definition.as_dict()),
    )


def _recording(
    configuration: GeneratedConfiguration,
    engine: RegimeOrderFlow,
    dimensions: ScenarioDimensions,
    model: FlowModel,
    profile_id: str | None,
    definition_sha256: str,
) -> CaseRecording:
    return CaseRecording(
        lane=ExecutorLane.CORE_FLOW,
        recording_type=CORE_FLOW_RECORDING_TYPE,
        payload={
            "configuration": configuration.as_dict(),
            "dimensions": dimensions.as_dict(),
            "duration_us": configuration.duration_us,
            "flow_events": [item.as_dict() for item in engine.flow_events],
            "flow_model": _flow_model_identity(model, profile_id),
            "initial_exchange_event_count": engine.initial_exchange_event_count,
            "scenario_definition_sha256": definition_sha256,
            "scenario_name": configuration.regime.lower(),
        },
    )


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    engine: RegimeOrderFlow,
    dimensions: ScenarioDimensions,
    model: FlowModel,
    profile_id: str | None,
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    engine.book.assert_invariants()
    event_projection = _event_projection(engine)
    observable_projection = _observable_projection(engine)
    static_flow_model = _flow_model_identity(model, profile_id)
    checks = _checks(
        engine,
        model,
        static_flow_model,
        observable_projection,
    )
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"CORE_FLOW_{check.name.upper()}",
            message=check.detail,
            evidence={
                "check": check.name,
                "check_evidence_sha256": canonical_sha256(
                    check.as_dict()["evidence"]
                ),
            },
        )
        for check in checks
        if check.status is CheckStatus.FAIL
    ]
    if replay_mismatches:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="CORE_FLOW_REPLAY_MISMATCH",
                message="serialized core-flow tape did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    fill_ledger = _player_ledger_from_fills(engine.book)
    trades = engine.book.trades[engine.initial_trade_count :]
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.CORE_FLOW,
        recording=recording,
        event_projection=event_projection,
        final_state_projection={
            "book": engine.book.runtime_state(),
            "clock_time_us": engine.clock.current_time_us,
            "dimensions": dimensions.as_dict(),
            "flow_model": static_flow_model,
            "initial_exchange_event_count": engine.initial_exchange_event_count,
            "initial_trade_count": engine.initial_trade_count,
            "observations": _observations(engine),
        },
        metrics={
            "applied_flow_event_count": sum(
                item.applied for item in engine.flow_events
            ),
            "ending_best_ask_ticks": engine.book.best_ask,
            "ending_best_bid_ticks": engine.book.best_bid,
            "exchange_event_count": len(engine.book.journal.events),
            "flow_event_count": len(engine.flow_events),
            "player_cash_tick_shares": fill_ledger["cash_tick_shares"],
            "player_position_shares": fill_ledger["position_shares"],
            "simulation_duration_us": engine.clock.current_time_us,
            "skipped_flow_event_count": sum(
                not item.applied for item in engine.flow_events
            ),
            "trade_count": len(trades),
            "traded_volume_shares": sum(item.quantity for item in trades),
        },
        exercises=_exercises(
            configuration,
            recording,
            engine,
            dimensions,
            static_flow_model,
        ),
        checks=checks,
        failures=tuple(failures),
        observable_projection=observable_projection,
    )


def _exercises(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    engine: RegimeOrderFlow,
    dimensions: ScenarioDimensions,
    static_flow_model: dict[str, object],
) -> tuple[ExerciseRecord, ...]:
    common = {
        "executor": type(engine).__name__,
        "recording_sha256": recording.sha256,
    }
    return (
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "seed",
            configuration.seed,
            ExerciseStatus.EXERCISED,
            {**common, "observed_seed": engine.seed},
        ),
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "duration_us",
            configuration.duration_us,
            ExerciseStatus.EXERCISED,
            {
                **common,
                "clock_end_us": engine.clock.current_time_us,
                "clock_start_us": 0,
            },
        ),
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "flow_model",
            configuration.flow_model,
            ExerciseStatus.EXERCISED,
            {**common, "model_replay_config": static_flow_model},
        ),
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "regime",
            configuration.regime,
            ExerciseStatus.EXERCISED,
            {**common, "observed_regime": engine.regime.value},
        ),
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "volume",
            configuration.volume,
            ExerciseStatus.EXERCISED,
            {**common, "scenario_dimensions": dimensions.as_dict()},
        ),
        ExerciseRecord(
            ExecutorLane.CORE_FLOW,
            "liquidity",
            configuration.liquidity,
            ExerciseStatus.EXERCISED,
            {**common, "scenario_dimensions": dimensions.as_dict()},
        ),
    )


def _checks(
    engine: RegimeOrderFlow,
    model: FlowModel,
    static_flow_model: dict[str, object],
    observable_projection: dict[str, object],
) -> tuple[CheckResult, ...]:
    book = engine.book
    orders = tuple(book.all_orders.values())
    fills_by_trade = {
        trade.trade_id: tuple(
            fill for fill in book.fills if fill.trade_id == trade.trade_id
        )
        for trade in book.trades
    }
    trade_fill_counts = {
        trade_id: len(fills) for trade_id, fills in fills_by_trade.items()
    }
    trade_fill_pairs_ok = all(
        all(
            (
                len(fills_by_trade[trade.trade_id]) == 2,
                {
                    fill.order_id
                    for fill in fills_by_trade[trade.trade_id]
                }
                == {trade.maker_order_id, trade.taker_order_id},
                all(
                    fill.price_ticks == trade.price_ticks
                    and fill.quantity == trade.quantity
                    for fill in fills_by_trade[trade.trade_id]
                ),
            )
        )
        for trade in book.trades
    )
    quantity_ok = all(
        all(
            (
                order.original_quantity >= 0,
                order.remaining_quantity >= 0,
                order.filled_quantity >= 0,
                order.cancelled_quantity >= 0,
                order.original_quantity
                == order.remaining_quantity
                + order.filled_quantity
                + order.cancelled_quantity,
            )
        )
        for order in orders
    ) and trade_fill_pairs_ok

    queued_ids: list[str] = []
    queue_sequences: list[list[int]] = []
    levels_ok = True
    for side, prices, levels in (
        (Side.BUY, book.bid_prices, book.bids),
        (Side.SELL, book.ask_prices, book.asks),
    ):
        expected_prices = sorted(prices, reverse=side is Side.BUY)
        levels_ok = levels_ok and prices == expected_prices
        levels_ok = levels_ok and len(prices) == len(set(prices))
        levels_ok = levels_ok and set(prices) == set(levels)
        for price in prices:
            level = levels[price]
            sequences = [
                order.resting_sequence
                for order in level.orders
                if order.resting_sequence is not None
            ]
            queue_sequences.append(sequences)
            queued_ids.extend(order.order_id for order in level.orders)
            levels_ok = levels_ok and all(
                (
                    order.side is side
                    and order.price_ticks == price
                    and order.remaining_quantity > 0
                    and order.resting_sequence is not None
                )
                for order in level.orders
            )
            levels_ok = levels_ok and sequences == sorted(sequences)
            levels_ok = levels_ok and len(sequences) == len(set(sequences))
            levels_ok = levels_ok and level.total_quantity == sum(
                order.remaining_quantity for order in level.orders
            )
    fifo_ok = (
        levels_ok
        and len(queued_ids) == len(set(queued_ids))
        and set(queued_ids) == set(book.active_orders)
    )
    non_crossed = (
        book.best_bid is None
        or book.best_ask is None
        or book.best_bid < book.best_ask
    )

    flow_sequences = [item.sequence for item in engine.flow_events]
    flow_times = [item.simulation_time_us for item in engine.flow_events]
    exchange_sequences = [item.sequence for item in book.journal.events]
    spanned: list[int] = []
    spans_valid = True
    for item in engine.flow_events:
        if item.applied:
            if (
                item.exchange_event_start is None
                or item.exchange_event_end is None
                or item.exchange_event_start > item.exchange_event_end
            ):
                spans_valid = False
                continue
            spanned.extend(
                range(item.exchange_event_start, item.exchange_event_end + 1)
            )
        elif (
            item.exchange_event_start is not None
            or item.exchange_event_end is not None
        ):
            spans_valid = False
    expected_spanned = list(
        range(engine.initial_exchange_event_count + 1, len(exchange_sequences) + 1)
    )
    sequences_ok = all(
        (
            flow_sequences == list(range(1, len(flow_sequences) + 1)),
            flow_times == sorted(flow_times),
            exchange_sequences
            == list(range(1, len(exchange_sequences) + 1)),
            spans_valid,
            spanned == expected_spanned,
        )
    )

    fill_ledger = _player_ledger_from_fills(book)
    event_ledger = _player_ledger_from_events(book.journal.events)
    book_ledger = {
        "bought_shares": book.player_position.bought_quantity,
        "position_shares": book.player_position.position,
        "sold_shares": book.player_position.sold_quantity,
    }
    position_ok = all(
        (
            fill_ledger["position_shares"]
            == event_ledger["position_shares"]
            == book_ledger["position_shares"],
            fill_ledger["bought_shares"]
            == event_ledger["bought_shares"]
            == book_ledger["bought_shares"],
            fill_ledger["sold_shares"]
            == event_ledger["sold_shares"]
            == book_ledger["sold_shares"],
        )
    )
    cash_ok = (
        fill_ledger["cash_tick_shares"]
        == event_ledger["cash_tick_shares"]
    )

    replay_config = static_flow_model["replay_config"]
    if model.model_name == "simple":
        diagnostics = model.diagnostics()
        stability_ok = diagnostics.get("stability") == "POISSON_BASELINE"
        stability_evidence: dict[str, object] = {
            "model": "simple",
            "stability": diagnostics.get("stability"),
        }
    else:
        if not isinstance(replay_config, dict):
            raise RuntimeError("Hawkes model omitted its replay configuration")
        stability_status = replay_config.get("stability_status")
        radius = replay_config.get("branching_spectral_radius")
        stability_ok = (
            stability_status
            in {"PASS_SUBCRITICAL", "WARNING_NEAR_CRITICAL"}
            and isinstance(radius, (int, float))
            and float(radius) < 1.0
        )
        stability_evidence = {
            "branching_spectral_radius": radius,
            "model": "hawkes",
            "profile_id": replay_config.get("profile_id"),
            "stability_status": stability_status,
        }

    baseline = engine.policy.rates(book)
    current_intensities = model.current_intensities(
        engine.clock.current_time_us,
        baseline,
    )
    ending_total_intensity = sum(current_intensities.values())
    configured_cap = (
        None
        if not isinstance(replay_config, dict)
        else replay_config.get("max_total_intensity")
    )
    intensity_ok = all(
        math.isfinite(value) and value >= 0
        for value in current_intensities.values()
    ) and (
        configured_cap is None
        or (
            isinstance(configured_cap, (int, float))
            and ending_total_intensity <= float(configured_cap) + 1e-9
        )
    )

    observable_keys = _all_keys(observable_projection)
    leaked = sorted(observable_keys.intersection(_FORBIDDEN_OBSERVABLE_FIELDS))

    return (
        _check(
            "quantity_conservation",
            quantity_ok,
            {
                "fill_count": len(book.fills),
                "order_count": len(orders),
                "trade_count": len(book.trades),
                "trade_fill_counts": trade_fill_counts,
                "trade_fill_pairs_reconciled": trade_fill_pairs_ok,
            },
        ),
        _check(
            "fifo_book_ordering",
            fifo_ok,
            {
                "active_order_count": len(book.active_orders),
                "ask_prices_ticks": book.ask_prices,
                "bid_prices_ticks": book.bid_prices,
                "queue_resting_sequences": queue_sequences,
            },
        ),
        _check(
            "non_crossed_book",
            non_crossed,
            {
                "best_ask_ticks": book.best_ask,
                "best_bid_ticks": book.best_bid,
            },
        ),
        _check(
            "contiguous_sequences",
            sequences_ok,
            {
                "exchange_event_count": len(exchange_sequences),
                "flow_event_count": len(flow_sequences),
                "initial_exchange_event_count": engine.initial_exchange_event_count,
                "spanned_exchange_event_count": len(spanned),
            },
        ),
        _check(
            "player_position_reconciliation",
            position_ok,
            {
                "book_ledger": book_ledger,
                "event_projector": event_ledger,
                "fill_projector": fill_ledger,
            },
        ),
        _check(
            "player_cash_reconciliation",
            cash_ok,
            {
                "event_cash_tick_shares": event_ledger["cash_tick_shares"],
                "fill_cash_tick_shares": fill_ledger["cash_tick_shares"],
            },
        ),
        _check("hawkes_stability", stability_ok, stability_evidence),
        _check(
            "event_rate_cap",
            intensity_ok,
            {
                "configured_cap_events_per_second": configured_cap,
                "ending_intensities_events_per_second": {
                    family.value: round(value, 9)
                    for family, value in current_intensities.items()
                },
                "ending_total_intensity_events_per_second": round(
                    ending_total_intensity,
                    9,
                ),
            },
        ),
        _check(
            "observable_projection_boundary",
            not leaked,
            {
                "forbidden_fields_found": leaked,
                "observable_projection_sha256": canonical_sha256(
                    observable_projection
                ),
            },
        ),
    )


def _check(
    name: str,
    passed: bool,
    evidence: dict[str, object],
) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        required=True,
        detail=(
            f"real core-flow check passed: {name}"
            if passed
            else f"real core-flow check failed: {name}"
        ),
        evidence={"source": "CoreFlowExecutor", **evidence},
    )


def _event_projection(
    engine: RegimeOrderFlow,
) -> tuple[dict[str, object], ...]:
    projected: list[dict[str, object]] = []
    exchange = {
        item.sequence: item.as_dict() for item in engine.book.journal.events
    }
    for sequence in range(1, engine.initial_exchange_event_count + 1):
        projected.append(
            {"record_type": "exchange_event", **exchange[sequence]}
        )
    for flow_event in engine.flow_events:
        projected.append(
            {"record_type": "flow_event", **flow_event.as_dict()}
        )
        if (
            flow_event.exchange_event_start is None
            or flow_event.exchange_event_end is None
        ):
            continue
        for sequence in range(
            flow_event.exchange_event_start,
            flow_event.exchange_event_end + 1,
        ):
            projected.append(
                {"record_type": "exchange_event", **exchange[sequence]}
            )
    return tuple(projected)


def _observable_projection(engine: RegimeOrderFlow) -> dict[str, object]:
    return {
        "book": {
            "asks": [
                {
                    "price_ticks": price,
                    "total_quantity_shares": engine.book.asks[price].total_quantity,
                }
                for price in engine.book.ask_prices
            ],
            "best_ask_ticks": engine.book.best_ask,
            "best_bid_ticks": engine.book.best_bid,
            "bids": [
                {
                    "price_ticks": price,
                    "total_quantity_shares": engine.book.bids[price].total_quantity,
                }
                for price in engine.book.bid_prices
            ],
        },
        "clock_time_us": engine.clock.current_time_us,
        "observations": _observations(engine),
        "representation": "AGGREGATED_CORE_FLOW",
        "trades": [
            {
                "price_ticks": trade.price_ticks,
                "quantity_shares": trade.quantity,
                "taker_side": trade.taker_side.value,
            }
            for trade in engine.book.trades[engine.initial_trade_count :]
        ],
    }


def _observations(engine: RegimeOrderFlow) -> list[dict[str, object]]:
    return [
        {
            "best_ask_quantity_shares": item.best_ask_size,
            "best_ask_ticks": item.best_ask_ticks,
            "best_bid_quantity_shares": item.best_bid_size,
            "best_bid_ticks": item.best_bid_ticks,
            "imbalance_ratio": item.imbalance,
            "simulation_time_us": item.simulation_time_us,
            "spread_ticks": item.spread_ticks,
        }
        for item in engine.observations
    ]


def _player_ledger_from_fills(book: OrderBook) -> dict[str, int]:
    bought_shares = 0
    sold_shares = 0
    position_shares = 0
    cash_tick_shares = 0
    for fill in book.fills:
        if fill.owner is not OrderOwner.PLAYER:
            continue
        if fill.side is Side.BUY:
            bought_shares += fill.quantity
            position_shares += fill.quantity
            cash_tick_shares -= fill.price_ticks * fill.quantity
        else:
            sold_shares += fill.quantity
            position_shares -= fill.quantity
            cash_tick_shares += fill.price_ticks * fill.quantity
    return {
        "bought_shares": bought_shares,
        "cash_tick_shares": cash_tick_shares,
        "position_shares": position_shares,
        "sold_shares": sold_shares,
    }


def _player_ledger_from_events(
    events: tuple[SimulationEvent, ...],
) -> dict[str, int]:
    player_sides: dict[str, Side] = {}
    bought_shares = 0
    sold_shares = 0
    position_shares = 0
    cash_tick_shares = 0
    for event in events:
        data = event.data
        if (
            event.event_type is EventType.ORDER_SUBMITTED
            and data.get("owner") == OrderOwner.PLAYER.value
            and data.get("side") is not None
        ):
            player_sides[str(data["order_id"])] = Side(str(data["side"]))
            continue
        if event.event_type not in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
            continue
        order_id = str(data["order_id"])
        side = player_sides.get(order_id)
        if side is None:
            continue
        quantity = int(data["fill_quantity"])
        price_ticks = int(data["price_ticks"])
        if side is Side.BUY:
            bought_shares += quantity
            position_shares += quantity
            cash_tick_shares -= price_ticks * quantity
        else:
            sold_shares += quantity
            position_shares -= quantity
            cash_tick_shares += price_ticks * quantity
    return {
        "bought_shares": bought_shares,
        "cash_tick_shares": cash_tick_shares,
        "position_shares": position_shares,
        "sold_shares": sold_shares,
    }


def _flow_model_identity(
    model: FlowModel,
    profile_id: str | None,
) -> dict[str, object]:
    return {
        "model": model.model_name,
        "profile_id": profile_id,
        "replay_config": model.replay_config(),
    }


def _flow_event(payload: dict[str, object]) -> FlowEvent:
    fields = set(payload)
    unknown = fields.difference(_FLOW_EVENT_FIELDS | {"diagnostic"})
    missing = _FLOW_EVENT_FIELDS.difference(fields)
    if unknown or missing:
        raise ValueError(
            f"core-flow event fields are not exact: missing={sorted(missing)} "
            f"unknown={sorted(unknown)}"
        )
    applied = payload["applied"]
    if type(applied) is not bool:
        raise TypeError("core-flow event applied must be Boolean")
    command = payload["command"]
    diagnostic = payload.get("diagnostic")
    if command is not None and not isinstance(command, dict):
        raise TypeError("core-flow event command must be an object or null")
    if diagnostic is not None and not isinstance(diagnostic, dict):
        raise TypeError("core-flow event diagnostic must be an object or null")
    reason = payload["reason"]
    if reason is not None and type(reason) is not str:
        raise TypeError("core-flow event reason must be a string or null")
    family = payload["family"]
    if type(family) is not str:
        raise TypeError("core-flow event family must be a string")
    sequence = _required_int(payload, "flow_sequence")
    simulation_time_us = _required_int(payload, "simulation_time_us")
    exchange_event_start = _optional_int(payload["exchange_event_start"])
    exchange_event_end = _optional_int(payload["exchange_event_end"])
    if sequence <= 0 or simulation_time_us < 0:
        raise ValueError("core-flow event sequence/time is out of range")
    if any(
        value is not None and value <= 0
        for value in (exchange_event_start, exchange_event_end)
    ):
        raise ValueError("core-flow event span is out of range")
    return FlowEvent(
        sequence=sequence,
        simulation_time_us=simulation_time_us,
        family=FlowEventFamily(family),
        applied=applied,
        command=command,
        reason=reason,
        exchange_event_start=exchange_event_start,
        exchange_event_end=exchange_event_end,
        diagnostic=diagnostic,
    )


def _required_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"core-flow event {name} must be an integer")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("core-flow event span must be an integer or null")
    return value


def _all_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(str(key).lower())
            keys.update(_all_keys(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.update(_all_keys(item))
    return keys
