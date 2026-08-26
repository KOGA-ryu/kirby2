"""Observable-state market regimes implemented as order-flow policies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kirby2.exchange import OrderBook, Side

from .clock import MICROSECONDS_PER_SECOND
from .config import SimulationConfig
from .distributions import WeightedDiscreteDistribution
from .flow import FlowEvent, FlowEventFamily, SimulationResult, SyntheticOrderFlow
from .scaling import ScenarioDimensions


class Regime(str, Enum):
    BALANCED = "BALANCED"
    BUY_PRESSURE = "BUY_PRESSURE"
    SELL_PRESSURE = "SELL_PRESSURE"
    MOMENTUM_UP = "MOMENTUM_UP"
    MOMENTUM_DOWN = "MOMENTUM_DOWN"
    ABSORPTION_BID = "ABSORPTION_BID"
    ABSORPTION_ASK = "ABSORPTION_ASK"
    THIN_LIQUIDITY = "THIN_LIQUIDITY"
    LIQUIDITY_VACUUM = "LIQUIDITY_VACUUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    HIGH_CANCELLATION = "HIGH_CANCELLATION"
    PANIC = "PANIC"


_FAMILIES = tuple(FlowEventFamily)


def _distribution(
    values: tuple[int, ...],
    weights: tuple[int, ...],
) -> WeightedDiscreteDistribution:
    return WeightedDiscreteDistribution(values=values, weights=weights)


SMALL_SIZES = _distribution((25, 50, 100, 200, 400), (20, 30, 30, 15, 5))
NORMAL_SIZES = _distribution((50, 100, 200, 400, 800), (10, 25, 30, 25, 10))
LARGE_SIZES = _distribution((100, 200, 400, 800, 1_200), (10, 20, 30, 25, 15))
HEAVY_SIZES = _distribution((200, 400, 800, 1_200, 2_000), (10, 20, 30, 25, 15))

AT_BEST_DEPTH = _distribution((0, 1, 2), (75, 20, 5))
NORMAL_DEPTH = _distribution((0, 1, 2, 3, 4), (40, 30, 15, 10, 5))
DEEP_DEPTH = _distribution((1, 2, 3, 4, 6), (10, 25, 30, 25, 10))


@dataclass(frozen=True, slots=True)
class RegimeProfile:
    regime: Regime
    rate_multipliers: tuple[float, float, float, float, float, float]
    limit_buy_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    limit_sell_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    market_buy_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    market_sell_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    bid_depth: WeightedDiscreteDistribution = NORMAL_DEPTH
    ask_depth: WeightedDiscreteDistribution = NORMAL_DEPTH
    imbalance_feedback: float = 0.0
    trend_feedback: float = 0.0
    initial_queue_sizes: WeightedDiscreteDistribution = NORMAL_SIZES

    def __post_init__(self) -> None:
        if len(self.rate_multipliers) != len(_FAMILIES):
            raise ValueError("regime must provide one rate multiplier per flow family")
        if any(not math.isfinite(value) or value < 0 for value in self.rate_multipliers):
            raise ValueError("rate multipliers must be finite and nonnegative")


def regime_profiles() -> dict[Regime, RegimeProfile]:
    return {
        Regime.BALANCED: RegimeProfile(
            regime=Regime.BALANCED,
            rate_multipliers=(1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            imbalance_feedback=0.20,
        ),
        Regime.BUY_PRESSURE: RegimeProfile(
            regime=Regime.BUY_PRESSURE,
            rate_multipliers=(1.35, 1.10, 1.70, 0.80, 0.80, 1.20),
            limit_buy_sizes=LARGE_SIZES,
            limit_sell_sizes=NORMAL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.20,
        ),
        Regime.SELL_PRESSURE: RegimeProfile(
            regime=Regime.SELL_PRESSURE,
            rate_multipliers=(1.10, 1.35, 0.80, 1.70, 1.20, 0.80),
            limit_buy_sizes=NORMAL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.20,
        ),
        Regime.MOMENTUM_UP: RegimeProfile(
            regime=Regime.MOMENTUM_UP,
            rate_multipliers=(1.25, 0.65, 2.80, 0.50, 0.45, 2.00),
            limit_buy_sizes=LARGE_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=HEAVY_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=DEEP_DEPTH,
            imbalance_feedback=0.90,
            trend_feedback=0.70,
        ),
        Regime.MOMENTUM_DOWN: RegimeProfile(
            regime=Regime.MOMENTUM_DOWN,
            rate_multipliers=(0.65, 1.25, 0.50, 2.80, 2.00, 0.45),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_sell_sizes=HEAVY_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.90,
            trend_feedback=0.70,
        ),
        Regime.ABSORPTION_BID: RegimeProfile(
            regime=Regime.ABSORPTION_BID,
            rate_multipliers=(3.00, 2.00, 0.55, 2.20, 0.25, 0.20),
            limit_buy_sizes=HEAVY_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.15,
            initial_queue_sizes=LARGE_SIZES,
        ),
        Regime.ABSORPTION_ASK: RegimeProfile(
            regime=Regime.ABSORPTION_ASK,
            rate_multipliers=(2.00, 3.00, 2.20, 0.55, 0.20, 0.25),
            limit_sell_sizes=HEAVY_SIZES,
            market_buy_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.15,
            initial_queue_sizes=LARGE_SIZES,
        ),
        Regime.THIN_LIQUIDITY: RegimeProfile(
            regime=Regime.THIN_LIQUIDITY,
            rate_multipliers=(0.55, 0.55, 1.00, 1.00, 1.40, 1.40),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=DEEP_DEPTH,
            initial_queue_sizes=SMALL_SIZES,
        ),
        Regime.LIQUIDITY_VACUUM: RegimeProfile(
            regime=Regime.LIQUIDITY_VACUUM,
            rate_multipliers=(0.25, 0.25, 0.90, 0.90, 3.50, 3.50),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=DEEP_DEPTH,
            initial_queue_sizes=SMALL_SIZES,
        ),
        Regime.MEAN_REVERSION: RegimeProfile(
            regime=Regime.MEAN_REVERSION,
            rate_multipliers=(1.10, 1.10, 1.00, 1.00, 0.90, 0.90),
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=-0.15,
            trend_feedback=-1.60,
        ),
        Regime.HIGH_CANCELLATION: RegimeProfile(
            regime=Regime.HIGH_CANCELLATION,
            rate_multipliers=(1.60, 1.60, 0.80, 0.80, 4.00, 4.00),
            bid_depth=NORMAL_DEPTH,
            ask_depth=NORMAL_DEPTH,
        ),
        Regime.PANIC: RegimeProfile(
            regime=Regime.PANIC,
            rate_multipliers=(0.35, 1.40, 0.35, 4.50, 3.00, 0.50),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_buy_sizes=SMALL_SIZES,
            market_sell_sizes=HEAVY_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.80,
            trend_feedback=0.50,
            initial_queue_sizes=SMALL_SIZES,
        ),
    }


def best_level_size(book: OrderBook, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def book_imbalance(book: OrderBook) -> float:
    bid_size = best_level_size(book, Side.BUY)
    ask_size = best_level_size(book, Side.SELL)
    denominator = bid_size + ask_size
    if denominator == 0:
        return 0.0
    return (bid_size - ask_size) / denominator


def mid_displacement_ticks(book: OrderBook, initial_mid_ticks: int) -> float:
    if book.best_bid is not None and book.best_ask is not None:
        return ((book.best_bid + book.best_ask) / 2.0) - initial_mid_ticks
    if book.best_bid is not None:
        return float(book.best_bid - initial_mid_ticks)
    if book.best_ask is not None:
        return float(book.best_ask - initial_mid_ticks)
    return 0.0


@dataclass(frozen=True, slots=True)
class BookObservation:
    simulation_time_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    best_bid_size: int
    best_ask_size: int
    imbalance: float
    spread_ticks: int | None


class RegimePolicy:
    def __init__(
        self,
        profile: RegimeProfile,
        config: SimulationConfig,
        parameter_overrides: dict[str, Any] | None = None,
        dimensions: ScenarioDimensions | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.parameter_overrides = parameter_overrides or {}
        self.dimensions = dimensions or ScenarioDimensions()

    def rates(self, book: OrderBook) -> dict[FlowEventFamily, float]:
        base_rates = self.config.rates
        base = (
            base_rates.limit_buy_rate,
            base_rates.limit_sell_rate,
            base_rates.market_buy_rate,
            base_rates.market_sell_rate,
            base_rates.cancel_bid_rate,
            base_rates.cancel_ask_rate,
        )
        multiplier_overrides = self.parameter_overrides.get("rate_multipliers", {})
        if not isinstance(multiplier_overrides, dict):
            raise ValueError("rate_multipliers override must be an object")
        imbalance = book_imbalance(book)
        displacement = max(
            -1.0,
            min(1.0, mid_displacement_ticks(book, self.config.initial_mid_ticks) / 5.0),
        )
        directional_signal = (
            self.profile.imbalance_feedback * imbalance
            + self.profile.trend_feedback * displacement
        )
        direction_weights = (0.40, -0.40, 1.00, -1.00, -0.50, 0.50)
        reversion_trend_weights = (0.80, -0.80, -1.00, 1.00, 0.00, 0.00)
        balance_weights = (-0.50, 0.50, -0.80, 0.80, 0.30, -0.30)
        rates: dict[FlowEventFamily, float] = {}
        for index, family in enumerate(_FAMILIES):
            profile_multiplier = self.profile.rate_multipliers[index]
            override = float(multiplier_overrides.get(family.value, 1.0))
            if self.profile.regime is Regime.MEAN_REVERSION:
                exponent = (
                    reversion_trend_weights[index]
                    * abs(self.profile.trend_feedback)
                    * displacement
                    + balance_weights[index] * 0.60 * imbalance
                )
            else:
                exponent = direction_weights[index] * directional_signal
            exponent = max(-2.0, min(2.0, exponent))
            rate = (
                base[index]
                * self.config.event_intensity
                * profile_multiplier
                * override
                * math.exp(exponent)
                * self.dimensions.rate_scale(family)
            )
            rates[family] = min(100.0, max(0.0, rate))
        return rates

    def size_distribution(self, family: FlowEventFamily) -> WeightedDiscreteDistribution:
        return {
            FlowEventFamily.LIMIT_BUY: self.profile.limit_buy_sizes,
            FlowEventFamily.LIMIT_SELL: self.profile.limit_sell_sizes,
            FlowEventFamily.MARKET_BUY: self.profile.market_buy_sizes,
            FlowEventFamily.MARKET_SELL: self.profile.market_sell_sizes,
            FlowEventFamily.CANCEL_BID: NORMAL_SIZES,
            FlowEventFamily.CANCEL_ASK: NORMAL_SIZES,
        }[family]

    def depth_distribution(self, family: FlowEventFamily) -> WeightedDiscreteDistribution:
        if family is FlowEventFamily.LIMIT_BUY:
            return self.dimensions.depth_distribution(self.profile.bid_depth)
        if family is FlowEventFamily.LIMIT_SELL:
            return self.dimensions.depth_distribution(self.profile.ask_depth)
        raise ValueError("only limit flow has a placement depth")


class RegimeOrderFlow(SyntheticOrderFlow):
    def __init__(
        self,
        seed: int,
        regime: Regime,
        config: SimulationConfig,
        parameter_overrides: dict[str, Any] | None = None,
        dimensions: ScenarioDimensions | None = None,
    ) -> None:
        super().__init__(seed=seed, config=config)
        self.regime = regime
        self.profile = regime_profiles()[regime]
        self.dimensions = dimensions or ScenarioDimensions()
        self.policy = RegimePolicy(
            self.profile,
            config,
            parameter_overrides,
            self.dimensions,
        )
        self.observations: list[BookObservation] = []

    def run(self, seconds: int) -> SimulationResult:
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        self._initialize_book()
        initial_exchange_event_count = len(self.book.journal.events)
        initial_trade_count = len(self.book.trades)
        end_time_us = seconds * MICROSECONDS_PER_SECOND
        flow_events: list[FlowEvent] = []

        while self.clock.current_time_us < end_time_us:
            rates = self.policy.rates(self.book)
            weights = [rates[family] for family in _FAMILIES]
            total_rate = sum(weights)
            if total_rate <= 0:
                break
            arrival_time_us = (
                self.clock.current_time_us
                + self.rng.exponential_interval_microseconds(total_rate)
            )
            if arrival_time_us > end_time_us:
                break
            self.clock.advance_to(arrival_time_us)
            family = _FAMILIES[self.rng.weighted_float_index(weights)]
            flow_events.append(self._apply_arrival(len(flow_events) + 1, family))

        self.clock.advance_to(end_time_us)
        self.book.assert_invariants()
        return SimulationResult(
            seed=self.seed,
            seconds=seconds,
            config=self.config,
            clock=self.clock,
            book=self.book,
            flow_events=tuple(flow_events),
            initial_exchange_event_count=initial_exchange_event_count,
            initial_trade_count=initial_trade_count,
        )

    def _draw_order_size(self, family: FlowEventFamily) -> int:
        distribution = self.policy.size_distribution(family)
        size = distribution.draw(self.rng)
        scale = float(self.policy.parameter_overrides.get("order_size_scale", 1.0))
        scale *= self.dimensions.order_size_scale(family)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("order_size_scale must be finite and positive")
        return max(1, round(size * scale))

    def _draw_depth(self, family: FlowEventFamily) -> int:
        return self.policy.depth_distribution(family).draw(self.rng)

    def _after_flow_event(self, event: FlowEvent) -> None:
        bid_size = best_level_size(self.book, Side.BUY)
        ask_size = best_level_size(self.book, Side.SELL)
        spread = (
            self.book.best_ask - self.book.best_bid
            if self.book.best_bid is not None and self.book.best_ask is not None
            else None
        )
        self.observations.append(
            BookObservation(
                simulation_time_us=event.simulation_time_us,
                best_bid_ticks=self.book.best_bid,
                best_ask_ticks=self.book.best_ask,
                best_bid_size=bid_size,
                best_ask_size=ask_size,
                imbalance=book_imbalance(self.book),
                spread_ticks=spread,
            )
        )
