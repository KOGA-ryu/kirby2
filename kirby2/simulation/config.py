"""Validated synthetic-flow configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from .distributions import IntegerDistribution, WeightedDiscreteDistribution


@dataclass(frozen=True, slots=True)
class EventRates:
    limit_buy_rate: float = 2.5
    limit_sell_rate: float = 2.5
    market_buy_rate: float = 1.5
    market_sell_rate: float = 1.5
    cancel_bid_rate: float = 1.25
    cancel_ask_rate: float = 1.25

    def __post_init__(self) -> None:
        for name, rate in self.as_dict().items():
            if not math.isfinite(rate) or rate < 0:
                raise ValueError(f"{name} must be finite and nonnegative")

    def as_dict(self) -> dict[str, float]:
        return {
            "cancel_ask_rate": self.cancel_ask_rate,
            "cancel_bid_rate": self.cancel_bid_rate,
            "limit_buy_rate": self.limit_buy_rate,
            "limit_sell_rate": self.limit_sell_rate,
            "market_buy_rate": self.market_buy_rate,
            "market_sell_rate": self.market_sell_rate,
        }


def _default_queue_sizes() -> WeightedDiscreteDistribution:
    return WeightedDiscreteDistribution(
        values=(100, 200, 400, 800, 1_200),
        weights=(15, 30, 30, 20, 5),
    )


def _default_order_sizes() -> WeightedDiscreteDistribution:
    return WeightedDiscreteDistribution(
        values=(50, 100, 200, 400, 800),
        weights=(10, 25, 30, 25, 10),
    )


def _default_depth_placement() -> WeightedDiscreteDistribution:
    return WeightedDiscreteDistribution(
        values=(0, 1, 2, 3, 4),
        weights=(40, 30, 15, 10, 5),
    )


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    tick_size: Decimal = Decimal("0.01")
    initial_mid_ticks: int = 10_000
    initial_depth: int = 8
    initial_half_spread_ticks: int = 1
    event_intensity: float = 1.0
    rates: EventRates = field(default_factory=EventRates)
    queue_size_distribution: IntegerDistribution = field(default_factory=_default_queue_sizes)
    order_size_distribution: IntegerDistribution = field(default_factory=_default_order_sizes)
    depth_placement_distribution: IntegerDistribution = field(default_factory=_default_depth_placement)

    def __post_init__(self) -> None:
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite():
            raise TypeError("tick_size must be a finite Decimal")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if type(self.initial_mid_ticks) is not int or self.initial_mid_ticks <= 0:
            raise ValueError("initial_mid_ticks must be a positive integer")
        if type(self.initial_depth) is not int or self.initial_depth <= 0:
            raise ValueError("initial_depth must be a positive integer")
        if type(self.initial_half_spread_ticks) is not int or self.initial_half_spread_ticks <= 0:
            raise ValueError("initial_half_spread_ticks must be a positive integer")
        if not math.isfinite(self.event_intensity) or self.event_intensity < 0:
            raise ValueError("event_intensity must be finite and nonnegative")
        if any(value <= 0 for value in self.queue_size_distribution.values):
            raise ValueError("initial queue sizes must be positive")
        if any(value <= 0 for value in self.order_size_distribution.values):
            raise ValueError("order sizes must be positive")
        if any(value < 0 for value in self.depth_placement_distribution.values):
            raise ValueError("depth placement must be nonnegative")
        deepest_bid = (
            self.initial_mid_ticks
            - self.initial_half_spread_ticks
            - self.initial_depth
            - max(self.depth_placement_distribution.values)
        )
        if deepest_bid <= 0:
            raise ValueError("initial mid is too low for configured depth")

    def as_dict(self) -> dict[str, object]:
        return {
            "depth_placement_distribution": self.depth_placement_distribution.as_dict(),
            "event_intensity": self.event_intensity,
            "initial_depth": self.initial_depth,
            "initial_half_spread_ticks": self.initial_half_spread_ticks,
            "initial_mid_ticks": self.initial_mid_ticks,
            "order_size_distribution": self.order_size_distribution.as_dict(),
            "queue_size_distribution": self.queue_size_distribution.as_dict(),
            "rates": self.rates.as_dict(),
            "tick_size": str(self.tick_size),
        }
