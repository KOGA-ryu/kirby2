"""Beginner strategy vocabulary adapted from the canonical feature engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from kirby2.exchange import OrderBook
from kirby2.features import FeatureFrame, FeatureKey, MicrostructureFeatureEngine
from kirby2.session.events import SimulationEvent

from .language import FeatureName


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    simulation_time_us: int
    window_us: int
    values: dict[FeatureName, Decimal | None]

    def __post_init__(self) -> None:
        if self.simulation_time_us < 0 or self.window_us <= 0:
            raise ValueError("feature snapshot time and window are invalid")
        if set(self.values) != set(FeatureName):
            raise ValueError("feature snapshot must contain the complete vocabulary")

    def as_dict(self) -> dict[str, str | None]:
        return {
            feature.value: None if value is None else str(value)
            for feature, value in self.values.items()
        }


class ObservableFeatureTracker:
    """Compatibility adapter; all calculations live in MicrostructureFeatureEngine."""

    def __init__(self, window_us: int, relative_volume: Decimal) -> None:
        self.window_us = window_us
        self.engine = MicrostructureFeatureEngine(
            windows_us=(window_us,),
            relative_volume=relative_volume,
        )

    def reset(self, simulation_time_us: int, book: OrderBook) -> FeatureSnapshot:
        return self._adapt(self.engine.reset(simulation_time_us, book))

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> FeatureSnapshot:
        captured = tuple(events)
        frame = (
            self.engine.observe(simulation_time_us, captured, book)
            if captured
            else self.engine.advance_to(simulation_time_us, book)
        )
        return self._adapt(frame)

    def advance_to(self, simulation_time_us: int, book: OrderBook) -> FeatureSnapshot:
        return self._adapt(self.engine.advance_to(simulation_time_us, book))

    @property
    def next_expiry_time_us(self) -> int | None:
        return self.engine.next_expiry_time_us

    def snapshot(self, simulation_time_us: int, book: OrderBook) -> FeatureSnapshot:
        return self._adapt(self.engine.snapshot(simulation_time_us, book))

    def _adapt(self, frame: FeatureFrame) -> FeatureSnapshot:
        window = self.window_us
        values = {
            FeatureName.SPREAD_TICKS: frame.value(FeatureKey.SPREAD_TICKS),
            FeatureName.BEST_BID_SIZE: frame.value(FeatureKey.BEST_BID_SIZE),
            FeatureName.BEST_ASK_SIZE: frame.value(FeatureKey.BEST_ASK_SIZE),
            FeatureName.BOOK_IMBALANCE: frame.value(
                FeatureKey.TOP_LEVEL_IMBALANCE
            ),
            FeatureName.AGGRESSIVE_BUY_VOLUME: frame.value(
                FeatureKey.AGGRESSIVE_BUY_VOLUME,
                window,
            ),
            FeatureName.AGGRESSIVE_SELL_VOLUME: frame.value(
                FeatureKey.AGGRESSIVE_SELL_VOLUME,
                window,
            ),
            FeatureName.BUY_SELL_RATIO: frame.value(
                FeatureKey.BUY_SELL_RATIO,
                window,
            ),
            FeatureName.TRADE_VELOCITY: frame.value(
                FeatureKey.TRADE_VELOCITY,
                window,
            ),
            FeatureName.BID_DEPLETION_RATE: frame.value(
                FeatureKey.QUEUE_DEPLETION_BID,
                window,
            ),
            FeatureName.ASK_DEPLETION_RATE: frame.value(
                FeatureKey.QUEUE_DEPLETION_ASK,
                window,
            ),
            FeatureName.BID_REPLENISHMENT_RATE: frame.value(
                FeatureKey.QUEUE_REPLENISHMENT_BID,
                window,
            ),
            FeatureName.ASK_REPLENISHMENT_RATE: frame.value(
                FeatureKey.QUEUE_REPLENISHMENT_ASK,
                window,
            ),
            FeatureName.BID_CANCEL_RATE: frame.value(
                FeatureKey.CANCEL_VELOCITY_BID,
                window,
            ),
            FeatureName.ASK_CANCEL_RATE: frame.value(
                FeatureKey.CANCEL_VELOCITY_ASK,
                window,
            ),
            FeatureName.RELATIVE_VOLUME: frame.value(FeatureKey.RELATIVE_VOLUME),
            FeatureName.SHORT_TERM_PRICE_CHANGE: frame.value(
                FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS,
                window,
            ),
            FeatureName.MICROPRICE: frame.value(FeatureKey.MICROPRICE),
        }
        return FeatureSnapshot(frame.simulation_time_us, window, values)
