"""Deterministic side-by-side diagnostics for simple and Hawkes flow."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable

from kirby2.session import EventType

from .flow import FlowEvent, FlowEventFamily, SimulationResult
from .flow_models import (
    FlowModel,
    HawkesFlowModel,
    SimpleFlowModel,
    load_accepted_hawkes_configs,
)
from .regimes import Regime, RegimeOrderFlow, regime_profiles

if TYPE_CHECKING:
    from kirby2.scenarios.market import ScenarioDefinition


SUPPORTED_FLOW_MODELS = ("simple", "hawkes")


@dataclass(frozen=True, slots=True)
class FlowModelComparison:
    model: str
    hawkes_profile: str | None
    metrics: dict[str, object]
    diagnostics: dict[str, object]
    replay_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "diagnostics": self.diagnostics,
            "hawkes_profile": self.hawkes_profile,
            "metrics": self.metrics,
            "model": self.model,
            "replay_sha256": self.replay_sha256,
        }


@dataclass(frozen=True, slots=True)
class FlowComparison:
    scenario: str
    seed: int
    models: tuple[FlowModelComparison, ...]

    def clustering_delta(self) -> dict[str, float] | None:
        by_name = {result.model: result for result in self.models}
        if set(by_name) != set(SUPPORTED_FLOW_MODELS):
            return None
        simple = by_name["simple"].metrics
        hawkes = by_name["hawkes"].metrics
        keys = (
            "aggressive_flow_fano_1s",
            "cancel_flow_fano_1s",
            "trade_fano_1s",
            "event_interarrival_cv",
        )
        return {
            key: round(float(hawkes[key]) - float(simple[key]), 6)
            for key in keys
        }


def compare_flow_models(
    definition: ScenarioDefinition,
    seed: int,
    models: Iterable[str] = SUPPORTED_FLOW_MODELS,
) -> FlowComparison:
    from kirby2.scenarios.market import create_market_engine

    requested = tuple(models)
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("flow comparison models must be nonempty and unique")
    unknown = set(requested) - set(SUPPORTED_FLOW_MODELS)
    if unknown:
        raise ValueError(f"unsupported flow models: {sorted(unknown)!r}")
    results: list[FlowModelComparison] = []
    for name in requested:
        model, profile_id = create_flow_model(name, definition.regime)
        engine, _ = create_market_engine(
            definition,
            seed=seed,
            flow_model=model,
        )
        simulation = engine.run(definition.duration_seconds)
        simulation.book.assert_invariants()
        results.append(
            FlowModelComparison(
                model=name,
                hawkes_profile=profile_id,
                metrics=_comparison_metrics(simulation, engine),
                diagnostics=model.diagnostics(),
                replay_sha256=simulation.replay_sha256(),
            )
        )
    return FlowComparison(definition.name, seed, tuple(results))


def accepted_hawkes_profile_for_regime(regime: Regime) -> str:
    if regime in {Regime.MOMENTUM_UP, Regime.MOMENTUM_DOWN}:
        return "momentum"
    if regime in {Regime.PANIC, Regime.LIQUIDITY_VACUUM}:
        return "panic"
    if regime in {Regime.ABSORPTION_BID, Regime.ABSORPTION_ASK}:
        return "absorption"
    return "balanced"


def create_flow_model(
    name: str,
    regime: Regime,
) -> tuple[FlowModel, str | None]:
    """Build the accepted production arrival model for one regime."""

    if name not in SUPPORTED_FLOW_MODELS:
        raise ValueError(f"unsupported flow model: {name!r}")
    if name == "simple":
        return SimpleFlowModel(), None
    profile_id = accepted_hawkes_profile_for_regime(regime)
    config = load_accepted_hawkes_configs()[profile_id]
    profile_multipliers = regime_profiles()[regime].rate_multipliers
    shaped_mu = tuple(
        baseline * multiplier
        for baseline, multiplier in zip(config.baseline_mu, profile_multipliers)
    )
    normalization = sum(config.baseline_mu) / sum(shaped_mu)
    composed_config = replace(
        config,
        baseline_mu=tuple(value * normalization for value in shaped_mu),
    )
    return HawkesFlowModel(composed_config, use_runtime_baseline=False), profile_id


def _comparison_metrics(
    simulation: SimulationResult,
    engine: RegimeOrderFlow,
) -> dict[str, object]:
    flow_events = simulation.flow_events
    flow_times = [event.simulation_time_us for event in flow_events]
    aggressive_times = [
        event.simulation_time_us
        for event in flow_events
        if event.family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
    ]
    cancel_times = [
        event.simulation_time_us
        for event in flow_events
        if event.family in {FlowEventFamily.CANCEL_BID, FlowEventFamily.CANCEL_ASK}
    ]
    trade_times = _exchange_event_times(
        flow_events,
        simulation,
        EventType.TRADE,
    )
    event_intervals = _intervals(flow_times)
    trades = simulation.book.trades[simulation.initial_trade_count :]
    trade_prices = [trade.price_ticks for trade in trades]
    observations = engine.observations
    spreads = [
        observation.spread_ticks
        for observation in observations
        if observation.spread_ticks is not None
    ]
    top_depths = [
        observation.best_bid_size + observation.best_ask_size
        for observation in observations
    ]
    flow_exchange_events = simulation.book.journal.events[
        simulation.initial_exchange_event_count :
    ]
    return {
        "aggressive_flow_count": len(aggressive_times),
        "aggressive_flow_fano_1s": _fano(aggressive_times, simulation.seconds),
        "aggressive_short_gap_fraction_100ms": _short_gap_fraction(
            aggressive_times,
            100_000,
        ),
        "average_spread_ticks": round(sum(spreads) / len(spreads), 6)
        if spreads
        else None,
        "average_top_depth": round(sum(top_depths) / len(top_depths), 3)
        if top_depths
        else 0.0,
        "cancel_flow_count": len(cancel_times),
        "cancel_flow_fano_1s": _fano(cancel_times, simulation.seconds),
        "cancel_short_gap_fraction_100ms": _short_gap_fraction(cancel_times, 100_000),
        "event_count": len(flow_events),
        "event_interarrival_cv": _coefficient_of_variation(event_intervals),
        "event_interarrival_mean_us": round(statistics.fmean(event_intervals), 3)
        if event_intervals
        else None,
        "event_interarrival_median_us": round(statistics.median(event_intervals), 3)
        if event_intervals
        else None,
        "invariant_status": "PASS",
        "max_spread_ticks": max(spreads) if spreads else None,
        "price_change_count": sum(
            event.event_type
            in {EventType.BEST_BID_CHANGED, EventType.BEST_ASK_CHANGED}
            for event in flow_exchange_events
        ),
        "price_displacement_ticks": (
            trade_prices[-1] - simulation.config.initial_mid_ticks
            if trade_prices
            else 0
        ),
        "price_range_ticks": max(trade_prices) - min(trade_prices)
        if trade_prices
        else 0,
        "trade_count": len(trades),
        "trade_fano_1s": _fano(trade_times, simulation.seconds),
        "trade_short_gap_fraction_100ms": _short_gap_fraction(trade_times, 100_000),
        "traded_volume": sum(trade.quantity for trade in trades),
    }


def _exchange_event_times(
    flow_events: tuple[FlowEvent, ...],
    simulation: SimulationResult,
    event_type: EventType,
) -> list[int]:
    times: list[int] = []
    events = simulation.book.journal.events
    for flow_event in flow_events:
        if (
            flow_event.exchange_event_start is None
            or flow_event.exchange_event_end is None
        ):
            continue
        matching = sum(
            event.event_type is event_type
            for event in events[
                flow_event.exchange_event_start - 1 : flow_event.exchange_event_end
            ]
        )
        times.extend([flow_event.simulation_time_us] * matching)
    return times


def _intervals(timestamps: list[int]) -> list[int]:
    return [current - previous for previous, current in zip(timestamps, timestamps[1:])]


def _fano(timestamps: list[int], seconds: int) -> float:
    counts = [0 for _ in range(max(1, seconds))]
    for timestamp in timestamps:
        index = min(len(counts) - 1, timestamp // 1_000_000)
        counts[index] += 1
    mean = statistics.fmean(counts)
    if mean == 0:
        return 0.0
    return round(statistics.pvariance(counts) / mean, 6)


def _short_gap_fraction(timestamps: list[int], threshold_us: int) -> float:
    intervals = _intervals(timestamps)
    if not intervals:
        return 0.0
    return round(sum(interval <= threshold_us for interval in intervals) / len(intervals), 6)


def _coefficient_of_variation(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return round(math.sqrt(statistics.pvariance(values)) / mean, 6)
