"""Rolling features derived only from visible book and tape activity."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from kirby2.exchange import OrderBook, Side
from kirby2.session.events import EventType, SimulationEvent

from .language import FeatureName


MICROSECONDS_PER_SECOND = 1_000_000


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


@dataclass(frozen=True, slots=True)
class _ActivitySample:
    simulation_time_us: int
    aggressive_buy_volume: int
    aggressive_sell_volume: int
    trade_count: int
    bid_depletion: int
    ask_depletion: int
    bid_replenishment: int
    ask_replenishment: int
    bid_cancels: int
    ask_cancels: int


@dataclass(frozen=True, slots=True)
class _MidpointSample:
    simulation_time_us: int
    midpoint_x2: int | None


class ObservableFeatureTracker:
    """Maintains a deterministic rolling window over observable exchange events."""

    def __init__(self, window_us: int, relative_volume: Decimal) -> None:
        if type(window_us) is not int or window_us <= 0:
            raise ValueError("feature window must be positive integer microseconds")
        if not isinstance(relative_volume, Decimal) or not relative_volume.is_finite():
            raise TypeError("relative volume must be a finite Decimal")
        if relative_volume < 0:
            raise ValueError("relative volume cannot be negative")
        self.window_us = window_us
        self.relative_volume = relative_volume
        self._activities: deque[_ActivitySample] = deque()
        self._midpoints: deque[_MidpointSample] = deque()
        self._last_time_us = 0

    def reset(self, simulation_time_us: int, book: OrderBook) -> FeatureSnapshot:
        if simulation_time_us < 0:
            raise ValueError("feature time cannot be negative")
        self._activities.clear()
        self._midpoints.clear()
        self._last_time_us = simulation_time_us
        self._midpoints.append(
            _MidpointSample(simulation_time_us, self._midpoint_x2(book))
        )
        return self.snapshot(simulation_time_us, book)

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> FeatureSnapshot:
        if simulation_time_us < self._last_time_us:
            raise ValueError("observable feature time cannot move backward")
        captured = tuple(events)
        activity = self._activity(simulation_time_us, captured)
        if captured:
            self._activities.append(activity)
        self._midpoints.append(
            _MidpointSample(simulation_time_us, self._midpoint_x2(book))
        )
        self._last_time_us = simulation_time_us
        self._prune(simulation_time_us)
        return self.snapshot(simulation_time_us, book)

    def snapshot(self, simulation_time_us: int, book: OrderBook) -> FeatureSnapshot:
        if simulation_time_us < self._last_time_us:
            raise ValueError("feature snapshot cannot precede observed time")
        self._prune(simulation_time_us)
        totals = self._totals()
        best_bid_size = self._best_size(book, Side.BUY)
        best_ask_size = self._best_size(book, Side.SELL)
        depth_total = best_bid_size + best_ask_size
        imbalance = (
            Decimal(best_bid_size - best_ask_size) / Decimal(depth_total)
            if depth_total
            else Decimal(0)
        )
        window_seconds = Decimal(self.window_us) / Decimal(MICROSECONDS_PER_SECOND)
        current_midpoint_x2 = self._midpoint_x2(book)
        baseline_midpoint_x2 = self._baseline_midpoint_x2()
        short_term_change = (
            Decimal(current_midpoint_x2 - baseline_midpoint_x2) / Decimal(2)
            if current_midpoint_x2 is not None and baseline_midpoint_x2 is not None
            else None
        )
        spread = (
            Decimal(book.best_ask - book.best_bid)
            if book.best_bid is not None and book.best_ask is not None
            else None
        )
        microprice = self._microprice(
            book.best_bid,
            book.best_ask,
            best_bid_size,
            best_ask_size,
        )
        aggressive_buy = totals["aggressive_buy_volume"]
        aggressive_sell = totals["aggressive_sell_volume"]
        values: dict[FeatureName, Decimal | None] = {
            FeatureName.SPREAD_TICKS: spread,
            FeatureName.BEST_BID_SIZE: Decimal(best_bid_size),
            FeatureName.BEST_ASK_SIZE: Decimal(best_ask_size),
            FeatureName.BOOK_IMBALANCE: imbalance,
            FeatureName.AGGRESSIVE_BUY_VOLUME: Decimal(aggressive_buy),
            FeatureName.AGGRESSIVE_SELL_VOLUME: Decimal(aggressive_sell),
            FeatureName.BUY_SELL_RATIO: (
                Decimal(aggressive_buy + 1) / Decimal(aggressive_sell + 1)
            ),
            FeatureName.TRADE_VELOCITY: Decimal(totals["trade_count"]) / window_seconds,
            FeatureName.BID_DEPLETION_RATE: Decimal(totals["bid_depletion"]) / window_seconds,
            FeatureName.ASK_DEPLETION_RATE: Decimal(totals["ask_depletion"]) / window_seconds,
            FeatureName.BID_REPLENISHMENT_RATE: Decimal(totals["bid_replenishment"]) / window_seconds,
            FeatureName.ASK_REPLENISHMENT_RATE: Decimal(totals["ask_replenishment"]) / window_seconds,
            FeatureName.BID_CANCEL_RATE: Decimal(totals["bid_cancels"]) / window_seconds,
            FeatureName.ASK_CANCEL_RATE: Decimal(totals["ask_cancels"]) / window_seconds,
            FeatureName.RELATIVE_VOLUME: self.relative_volume,
            FeatureName.SHORT_TERM_PRICE_CHANGE: short_term_change,
            FeatureName.MICROPRICE: microprice,
        }
        return FeatureSnapshot(simulation_time_us, self.window_us, values)

    def _activity(
        self,
        simulation_time_us: int,
        events: tuple[SimulationEvent, ...],
    ) -> _ActivitySample:
        values = {
            "aggressive_buy_volume": 0,
            "aggressive_sell_volume": 0,
            "trade_count": 0,
            "bid_depletion": 0,
            "ask_depletion": 0,
            "bid_replenishment": 0,
            "ask_replenishment": 0,
            "bid_cancels": 0,
            "ask_cancels": 0,
        }
        for event in events:
            data = event.data
            if event.event_type is EventType.TRADE:
                quantity = int(data["quantity"])
                values["trade_count"] += 1
                if data["taker_side"] == Side.BUY.value:
                    values["aggressive_buy_volume"] += quantity
                    values["ask_depletion"] += quantity
                else:
                    values["aggressive_sell_volume"] += quantity
                    values["bid_depletion"] += quantity
            elif event.event_type is EventType.ORDER_ADDED:
                quantity = int(data["remaining_quantity"])
                key = (
                    "bid_replenishment"
                    if data["side"] == Side.BUY.value
                    else "ask_replenishment"
                )
                values[key] += quantity
            elif event.event_type is EventType.ORDER_CANCELLED:
                quantity = int(data["cancelled_quantity"])
                key = "bid_cancels" if data["side"] == Side.BUY.value else "ask_cancels"
                values[key] += quantity
        return _ActivitySample(simulation_time_us=simulation_time_us, **values)

    def _prune(self, simulation_time_us: int) -> None:
        cutoff = simulation_time_us - self.window_us
        while self._activities and self._activities[0].simulation_time_us < cutoff:
            self._activities.popleft()
        while len(self._midpoints) > 1 and self._midpoints[1].simulation_time_us <= cutoff:
            self._midpoints.popleft()

    def _totals(self) -> dict[str, int]:
        names = (
            "aggressive_buy_volume",
            "aggressive_sell_volume",
            "trade_count",
            "bid_depletion",
            "ask_depletion",
            "bid_replenishment",
            "ask_replenishment",
            "bid_cancels",
            "ask_cancels",
        )
        return {
            name: sum(getattr(sample, name) for sample in self._activities)
            for name in names
        }

    def _baseline_midpoint_x2(self) -> int | None:
        for sample in self._midpoints:
            if sample.midpoint_x2 is not None:
                return sample.midpoint_x2
        return None

    @staticmethod
    def _best_size(book: OrderBook, side: Side) -> int:
        price = book.best_bid if side is Side.BUY else book.best_ask
        if price is None:
            return 0
        levels = book.bids if side is Side.BUY else book.asks
        return levels[price].total_quantity

    @staticmethod
    def _midpoint_x2(book: OrderBook) -> int | None:
        if book.best_bid is None or book.best_ask is None:
            return None
        return book.best_bid + book.best_ask

    @staticmethod
    def _microprice(
        best_bid: int | None,
        best_ask: int | None,
        best_bid_size: int,
        best_ask_size: int,
    ) -> Decimal | None:
        if best_bid is None or best_ask is None:
            return None
        total_size = best_bid_size + best_ask_size
        if total_size == 0:
            return None
        numerator = best_ask * best_bid_size + best_bid * best_ask_size
        return Decimal(numerator) / Decimal(total_size)
