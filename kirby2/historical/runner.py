"""Run exact-message fixtures and constraint-driven reconstructions."""

from __future__ import annotations

from typing import Iterable

from kirby2.exchange import Order, OrderBook, Side
from kirby2.session import EventType
from kirby2.simulation import (
    EventRates,
    Regime,
    RegimeOrderFlow,
    SimulationClock,
    SimulationConfig,
    WeightedDiscreteDistribution,
)
from kirby2.simulation.regimes import regime_profiles

from .models import (
    ExactReplayFixture,
    ExpectedTrade,
    HistoricalCommandRecord,
    HistoricalDataMode,
    HistoricalRun,
    ReconstructionFixture,
)


def run_exact_replay(fixture: ExactReplayFixture) -> HistoricalRun:
    """Replay every source fixture message at its recorded simulation time."""
    clock = SimulationClock()
    book = OrderBook()
    commands: list[HistoricalCommandRecord] = []
    spread_samples: list[int] = []

    for message in fixture.messages:
        clock.advance_to(message.timestamp_us)
        event_start = len(book.journal.events) + 1
        if message.action == "limit":
            book.process(
                Order.limit(
                    message.order_id,
                    Side(message.side),
                    message.quantity,
                    message.price_ticks,  # type: ignore[arg-type]
                )
            )
        elif message.action == "market":
            book.process(
                Order.market(
                    message.order_id,
                    Side(message.side),
                    message.quantity,
                )
            )
        else:
            book.process(
                Order.cancel(
                    message.order_id,
                    message.target_order_id,  # type: ignore[arg-type]
                )
            )
        commands.append(
            HistoricalCommandRecord(
                sequence=message.sequence,
                simulation_time_us=message.timestamp_us,
                action=message.action,
                applied=True,
                command=message.as_dict(),
                exchange_event_start=event_start,
                exchange_event_end=len(book.journal.events),
                order_provenance="SOURCE_FIXTURE_EXACT_MESSAGES",
            )
        )
        spread = _spread(book)
        if spread is not None:
            spread_samples.append(spread)

    clock.advance_to(fixture.duration_us)
    book.assert_invariants()
    actual_trades = tuple(
        ExpectedTrade(
            trade_id=trade.trade_id,
            price_ticks=trade.price_ticks,
            quantity=trade.quantity,
            maker_order_id=trade.maker_order_id,
            taker_order_id=trade.taker_order_id,
            taker_side=trade.taker_side.value,
        )
        for trade in book.trades
    )
    if actual_trades != fixture.expected_trades:
        raise RuntimeError(
            "exact fixture trade validation failed: "
            f"expected={_trade_dicts(fixture.expected_trades)!r} "
            f"actual={_trade_dicts(actual_trades)!r}"
        )

    return HistoricalRun(
        fixture_id=fixture.fixture_id,
        label=fixture.label,
        mode=HistoricalDataMode.EXACT_REPLAY,
        provenance=fixture.provenance,
        duration_us=fixture.duration_us,
        tick_size=fixture.tick_size,
        book=book,
        source_message_count=len(fixture.messages),
        source_trade_count=len(fixture.expected_trades),
        synthetic_command_count=0,
        commands=tuple(commands),
        spread_samples_ticks=tuple(spread_samples),
    )


def run_reconstruction(
    fixture: ReconstructionFixture,
    seed: int | None = None,
) -> HistoricalRun:
    """Generate synthetic microstructure constrained by aggregate observations."""
    constraints = fixture.constraints
    actual_seed = fixture.seed if seed is None else seed
    if type(actual_seed) is not int or actual_seed < 0:
        raise ValueError("reconstruction seed must be a nonnegative integer")
    if constraints.duration_us % 1_000_000 != 0:
        raise ValueError("demonstration reconstruction duration must use whole seconds")

    regime = _constraint_regime(fixture)
    rates = EventRates()
    profile = regime_profiles()[regime]
    duration_seconds = constraints.duration_us // 1_000_000
    volatility_intensity = min(
        2.5,
        max(0.5, 0.75 + float(constraints.realized_volatility_bps) / 30.0),
    )
    expected_market_volume_per_second = volatility_intensity * (
        rates.market_buy_rate
        * profile.rate_multipliers[2]
        * _weighted_mean(profile.market_buy_sizes.values, profile.market_buy_sizes.weights)
        + rates.market_sell_rate
        * profile.rate_multipliers[3]
        * _weighted_mean(profile.market_sell_sizes.values, profile.market_sell_sizes.weights)
    )
    target_volume_per_second = constraints.aggregate_volume / duration_seconds
    order_size_scale = min(
        4.0,
        max(
            0.1,
            target_volume_per_second / (expected_market_volume_per_second * 0.65),
        ),
    )
    observed_spreads = [item.spread_ticks for item in constraints.spread_observations]
    average_spread = (
        sum(observed_spreads) / len(observed_spreads) if observed_spreads else 2.0
    )
    observed_print_sizes = [item.quantity for item in constraints.trade_prints]
    queue_anchor = (
        round(sum(observed_print_sizes) / len(observed_print_sizes))
        if observed_print_sizes
        else max(100, round(target_volume_per_second))
    )
    queue_sizes = WeightedDiscreteDistribution(
        values=tuple(max(1, round(queue_anchor * factor)) for factor in (0.25, 0.5, 1.0, 2.0)),
        weights=(20, 35, 30, 15),
    )
    initial_depth = min(10, max(4, len(observed_spreads) + 2))
    config = SimulationConfig(
        tick_size=constraints.tick_size,
        initial_mid_ticks=constraints.open_ticks,
        initial_depth=initial_depth,
        initial_half_spread_ticks=max(1, round(average_spread / 2.0)),
        event_intensity=volatility_intensity,
        rates=rates,
        queue_size_distribution=queue_sizes,
    )
    parameter_overrides: dict[str, object] = {
        "order_size_scale": order_size_scale,
    }
    candidate_count = 24
    candidates = []
    for offset in range(candidate_count):
        engine_seed = actual_seed + offset
        candidate_engine = RegimeOrderFlow(
            seed=engine_seed,
            regime=regime,
            config=config,
            parameter_overrides=parameter_overrides,
        )
        candidate_simulation = candidate_engine.run(duration_seconds)
        candidates.append(
            (
                _constraint_fit_score(fixture, candidate_engine),
                offset,
                engine_seed,
                candidate_engine,
                candidate_simulation,
            )
        )
    fit_score, _, selected_engine_seed, engine, simulation = min(
        candidates,
        key=lambda candidate: (candidate[0], candidate[1]),
    )
    simulation.book.assert_invariants()

    commands = _reconstruction_commands(simulation, engine)
    spread_samples = [
        observation.spread_ticks
        for observation in engine.observations
        if observation.spread_ticks is not None
    ]
    initial_spread = _spread(simulation.book)
    if not spread_samples and initial_spread is not None:
        spread_samples.append(initial_spread)
    reconstruction_config: dict[str, object] = {
        "constraint_derivation": {
            "average_observed_spread_ticks": average_spread,
            "assumed_marketable_fill_fraction": 0.65,
            "order_size_scale": order_size_scale,
            "queue_anchor_quantity": queue_anchor,
            "target_volume_per_second": target_volume_per_second,
            "volatility_intensity": volatility_intensity,
        },
        "deterministic_calibration": {
            "candidate_count": candidate_count,
            "candidate_seed_start": actual_seed,
            "fit_score": fit_score,
            "selected_engine_seed": selected_engine_seed,
        },
        "regime": regime.value,
        "simulation_config": config.as_dict(),
    }
    return HistoricalRun(
        fixture_id=fixture.fixture_id,
        label=fixture.label,
        mode=HistoricalDataMode.RECONSTRUCTION,
        provenance=fixture.provenance,
        duration_us=constraints.duration_us,
        tick_size=constraints.tick_size,
        book=simulation.book,
        source_message_count=0,
        source_trade_count=0,
        synthetic_command_count=len(commands),
        commands=commands,
        spread_samples_ticks=tuple(spread_samples),
        constraints=constraints,
        reconstruction_seed=actual_seed,
        reconstruction_config=reconstruction_config,
        initial_trade_count=simulation.initial_trade_count,
    )


def run_historical_fixture(
    fixture: ExactReplayFixture | ReconstructionFixture,
    seed: int | None = None,
) -> HistoricalRun:
    if isinstance(fixture, ExactReplayFixture):
        if seed is not None:
            raise ValueError("an exact replay fixture does not accept a synthetic seed")
        return run_exact_replay(fixture)
    return run_reconstruction(fixture, seed)


def _reconstruction_commands(simulation, engine: RegimeOrderFlow) -> tuple[HistoricalCommandRecord, ...]:
    commands: list[HistoricalCommandRecord] = []
    initial_events = simulation.book.journal.events[: simulation.initial_exchange_event_count]
    starts = [
        event.sequence
        for event in initial_events
        if event.event_type is EventType.ORDER_SUBMITTED
    ]
    for index, event_start in enumerate(starts):
        event_end = (
            starts[index + 1] - 1
            if index + 1 < len(starts)
            else simulation.initial_exchange_event_count
        )
        submitted = simulation.book.journal.events[event_start - 1]
        commands.append(
            HistoricalCommandRecord(
                sequence=len(commands) + 1,
                simulation_time_us=0,
                action="initial_limit",
                applied=True,
                command=dict(submitted.data),
                exchange_event_start=event_start,
                exchange_event_end=event_end,
                order_provenance="SYNTHETIC_RECONSTRUCTION",
            )
        )

    for flow_event in engine.flow_events:
        command = (
            None
            if flow_event.command is None
            else {"flow_sequence": flow_event.sequence, **flow_event.command}
        )
        commands.append(
            HistoricalCommandRecord(
                sequence=len(commands) + 1,
                simulation_time_us=flow_event.simulation_time_us,
                action=flow_event.family.value,
                applied=flow_event.applied,
                command=command,
                exchange_event_start=flow_event.exchange_event_start,
                exchange_event_end=flow_event.exchange_event_end,
                order_provenance="SYNTHETIC_RECONSTRUCTION",
            )
        )
    return tuple(commands)


def _constraint_regime(fixture: ReconstructionFixture) -> Regime:
    direction = fixture.constraints.close_ticks - fixture.constraints.open_ticks
    if direction > 0:
        return Regime.BUY_PRESSURE
    if direction < 0:
        return Regime.SELL_PRESSURE
    return Regime.BALANCED


def _spread(book: OrderBook) -> int | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return book.best_ask - book.best_bid


def _weighted_mean(values: tuple[int, ...], weights: tuple[int, ...]) -> float:
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _constraint_fit_score(
    fixture: ReconstructionFixture,
    engine: RegimeOrderFlow,
) -> float:
    constraints = fixture.constraints
    trades = engine.book.trades[engine.initial_trade_count :]
    if not trades:
        return float("inf")
    prices = [trade.price_ticks for trade in trades]
    volume = sum(trade.quantity for trade in trades)
    observed_spreads = [
        item.spread_ticks for item in constraints.spread_observations
    ]
    generated_spreads = [
        observation.spread_ticks
        for observation in engine.observations
        if observation.spread_ticks is not None
    ]
    spread_residual = 0.0
    if observed_spreads and generated_spreads:
        spread_residual = abs(
            sum(generated_spreads) / len(generated_spreads)
            - sum(observed_spreads) / len(observed_spreads)
        )
    side_empty_penalty = (
        25.0 if engine.book.best_bid is None or engine.book.best_ask is None else 0.0
    )
    return round(
        side_empty_penalty
        + abs(prices[0] - constraints.open_ticks)
        + abs(max(prices) - constraints.high_ticks)
        + abs(min(prices) - constraints.low_ticks)
        + 3.0 * abs(prices[-1] - constraints.close_ticks)
        + 10.0 * abs(volume - constraints.aggregate_volume)
        / max(1, constraints.aggregate_volume)
        + 2.0 * spread_residual,
        9,
    )


def _trade_dicts(trades: Iterable[ExpectedTrade]) -> list[dict[str, object]]:
    return [trade.as_dict() for trade in trades]
