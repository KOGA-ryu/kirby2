"""Causal, simulation-time microstructure feature computation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Iterable, Mapping, Protocol

from kirby2.exchange import Side
from kirby2.session.events import EventType, SimulationEvent

from .models import FEATURE_CATALOG, FeatureFrame, FeatureKey


MICROSECONDS_PER_SECOND = 1_000_000


class DepthLevelView(Protocol):
    total_quantity: int


class MarketDepthView(Protocol):
    """Minimum aggregate-depth surface accepted by observable features."""

    @property
    def best_bid(self) -> int | None: ...

    @property
    def best_ask(self) -> int | None: ...

    @property
    def bid_prices(self) -> list[int]: ...

    @property
    def ask_prices(self) -> list[int]: ...

    @property
    def bids(self) -> Mapping[int, DepthLevelView]: ...

    @property
    def asks(self) -> Mapping[int, DepthLevelView]: ...


@dataclass(frozen=True, slots=True)
class _Activity:
    time_us: int
    aggressive_buy: int = 0
    aggressive_sell: int = 0
    trades: int = 0
    cancel_bid: int = 0
    cancel_ask: int = 0
    depletion_bid: int = 0
    depletion_ask: int = 0
    replenishment_bid: int = 0
    replenishment_ask: int = 0


@dataclass(frozen=True, slots=True)
class _Midpoint:
    time_us: int
    value: Decimal | None


class MicrostructureFeatureEngine:
    """Consumes only events already emitted and the current canonical book."""

    def __init__(
        self,
        windows_us: tuple[int, ...] = (250_000, 1_000_000, 5_000_000),
        relative_volume: Decimal = Decimal("1"),
        depth_levels: int = 5,
    ) -> None:
        normalized = tuple(sorted(set(windows_us)))
        if not normalized or any(type(window) is not int or window <= 0 for window in normalized):
            raise ValueError("feature windows must be positive integer microseconds")
        if not isinstance(relative_volume, Decimal) or not relative_volume.is_finite():
            raise TypeError("relative volume must be a finite Decimal")
        if relative_volume < 0:
            raise ValueError("relative volume cannot be negative")
        if type(depth_levels) is not int or depth_levels <= 0:
            raise ValueError("feature depth level count must be positive")
        self.windows_us = normalized
        self.relative_volume = relative_volume
        self.depth_levels = depth_levels
        self._activities: deque[_Activity] = deque(maxlen=100_000)
        self._midpoints: deque[_Midpoint] = deque(maxlen=100_000)
        self._last_time_us = 0
        self._initialized = False

    def reset(self, simulation_time_us: int, book: MarketDepthView) -> FeatureFrame:
        if type(simulation_time_us) is not int or simulation_time_us < 0:
            raise ValueError("feature reset time must be nonnegative microseconds")
        self._activities.clear()
        self._midpoints.clear()
        self._last_time_us = simulation_time_us
        self._midpoints.append(_Midpoint(simulation_time_us, _midpoint(book)))
        self._initialized = True
        return self.snapshot(simulation_time_us, book)

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: MarketDepthView,
    ) -> FeatureFrame:
        if not self._initialized:
            self.reset(simulation_time_us, book)
        if simulation_time_us < self._last_time_us:
            raise ValueError("feature observation time cannot move backward")
        captured = tuple(events)
        if captured:
            self._activities.append(_activity(simulation_time_us, captured))
        self._midpoints.append(_Midpoint(simulation_time_us, _midpoint(book)))
        self._last_time_us = simulation_time_us
        self._prune(simulation_time_us)
        return self.snapshot(simulation_time_us, book)

    def advance_to(self, simulation_time_us: int, book: MarketDepthView) -> FeatureFrame:
        """Advance rolling windows without inventing market activity."""

        if not self._initialized:
            return self.reset(simulation_time_us, book)
        if simulation_time_us < self._last_time_us:
            raise ValueError("feature clock cannot move backward")
        self._last_time_us = simulation_time_us
        self._prune(simulation_time_us)
        return self.snapshot(simulation_time_us, book)

    @property
    def next_expiry_time_us(self) -> int | None:
        """Earliest time at which a retained rolling observation can change."""

        if not self._initialized:
            return None
        candidates: list[int] = []
        for window_us in self.windows_us:
            for activity in self._activities:
                expiry = activity.time_us + window_us + 1
                if expiry > self._last_time_us:
                    candidates.append(expiry)
                    break

        max_window_us = max(self.windows_us)
        if len(self._midpoints) > 1:
            expiry = self._midpoints[1].time_us + max_window_us
            if expiry > self._last_time_us:
                candidates.append(expiry)
        for window_us in self.windows_us:
            if window_us == max_window_us:
                continue
            for midpoint in tuple(self._midpoints)[1:]:
                expiry = midpoint.time_us + window_us + 1
                if expiry > self._last_time_us:
                    candidates.append(expiry)
                    break
        return min(candidates) if candidates else None

    def snapshot(self, simulation_time_us: int, book: MarketDepthView) -> FeatureFrame:
        if not self._initialized:
            return self.reset(simulation_time_us, book)
        if simulation_time_us < self._last_time_us:
            raise ValueError("feature snapshot cannot precede observed state")
        self._prune(simulation_time_us)
        values: dict[tuple[FeatureKey, int | None], Decimal | None] = {}
        best_bid_size = _best_size(book, Side.BUY)
        best_ask_size = _best_size(book, Side.SELL)
        midpoint = _midpoint(book)
        values[(FeatureKey.MID_PRICE, None)] = midpoint
        values[(FeatureKey.MICROPRICE, None)] = _microprice(
            book,
            best_bid_size,
            best_ask_size,
        )
        values[(FeatureKey.SPREAD_TICKS, None)] = (
            Decimal(book.best_ask - book.best_bid)
            if book.best_bid is not None and book.best_ask is not None
            else None
        )
        values[(FeatureKey.BEST_BID_SIZE, None)] = Decimal(best_bid_size)
        values[(FeatureKey.BEST_ASK_SIZE, None)] = Decimal(best_ask_size)
        values[(FeatureKey.TOP_LEVEL_IMBALANCE, None)] = _imbalance(
            best_bid_size,
            best_ask_size,
        )
        bid_depth = sum(
            book.bids[price].total_quantity for price in book.bid_prices[: self.depth_levels]
        )
        ask_depth = sum(
            book.asks[price].total_quantity for price in book.ask_prices[: self.depth_levels]
        )
        values[(FeatureKey.MULTI_LEVEL_IMBALANCE, None)] = _imbalance(
            bid_depth,
            ask_depth,
        )
        values[(FeatureKey.WEIGHTED_DEPTH_BID, None)] = _weighted_depth(
            book,
            Side.BUY,
            self.depth_levels,
        )
        values[(FeatureKey.WEIGHTED_DEPTH_ASK, None)] = _weighted_depth(
            book,
            Side.SELL,
            self.depth_levels,
        )
        values[(FeatureKey.RELATIVE_VOLUME, None)] = self.relative_volume
        for window_us in self.windows_us:
            activities = tuple(
                item
                for item in self._activities
                if item.time_us >= simulation_time_us - window_us
            )
            totals = {
                field: sum(getattr(item, field) for item in activities)
                for field in (
                    "aggressive_buy",
                    "aggressive_sell",
                    "trades",
                    "cancel_bid",
                    "cancel_ask",
                    "depletion_bid",
                    "depletion_ask",
                    "replenishment_bid",
                    "replenishment_ask",
                )
            }
            seconds = Decimal(window_us) / Decimal(MICROSECONDS_PER_SECOND)
            buy = totals["aggressive_buy"]
            sell = totals["aggressive_sell"]
            values[(FeatureKey.AGGRESSIVE_BUY_VOLUME, window_us)] = Decimal(buy)
            values[(FeatureKey.AGGRESSIVE_SELL_VOLUME, window_us)] = Decimal(sell)
            values[(FeatureKey.TRADE_IMBALANCE, window_us)] = _imbalance(buy, sell)
            values[(FeatureKey.BUY_SELL_RATIO, window_us)] = Decimal(buy + 1) / Decimal(sell + 1)
            for key, field in (
                (FeatureKey.TRADE_VELOCITY, "trades"),
                (FeatureKey.CANCEL_VELOCITY_BID, "cancel_bid"),
                (FeatureKey.CANCEL_VELOCITY_ASK, "cancel_ask"),
                (FeatureKey.QUEUE_DEPLETION_BID, "depletion_bid"),
                (FeatureKey.QUEUE_DEPLETION_ASK, "depletion_ask"),
                (FeatureKey.QUEUE_REPLENISHMENT_BID, "replenishment_bid"),
                (FeatureKey.QUEUE_REPLENISHMENT_ASK, "replenishment_ask"),
            ):
                values[(key, window_us)] = Decimal(totals[field]) / seconds
            midpoint_samples = _window_midpoints(
                self._midpoints,
                simulation_time_us,
                window_us,
            )
            price_values = _price_features(midpoint_samples, midpoint, window_us)
            for key, value in price_values.items():
                values[(key, window_us)] = value
        expected = {
            (definition.key, window if definition.windowed else None)
            for definition in FEATURE_CATALOG.values()
            for window in (self.windows_us if definition.windowed else (None,))
        }
        if set(values) != expected:
            missing = expected - set(values)
            raise RuntimeError(f"canonical feature engine omitted values: {missing!r}")
        return FeatureFrame(simulation_time_us, self.windows_us, values)

    def _prune(self, simulation_time_us: int) -> None:
        cutoff = simulation_time_us - max(self.windows_us)
        while self._activities and self._activities[0].time_us < cutoff:
            self._activities.popleft()
        while len(self._midpoints) > 1 and self._midpoints[1].time_us <= cutoff:
            self._midpoints.popleft()


def _activity(time_us: int, events: tuple[SimulationEvent, ...]) -> _Activity:
    totals = {
        "aggressive_buy": 0,
        "aggressive_sell": 0,
        "trades": 0,
        "cancel_bid": 0,
        "cancel_ask": 0,
        "depletion_bid": 0,
        "depletion_ask": 0,
        "replenishment_bid": 0,
        "replenishment_ask": 0,
    }
    for event in events:
        data = event.data
        if event.event_type is EventType.TRADE:
            quantity = int(data["quantity"])
            totals["trades"] += 1
            if data["taker_side"] == Side.BUY.value:
                totals["aggressive_buy"] += quantity
                totals["depletion_ask"] += quantity
            else:
                totals["aggressive_sell"] += quantity
                totals["depletion_bid"] += quantity
        elif event.event_type is EventType.ORDER_ADDED:
            side = "bid" if data["side"] == Side.BUY.value else "ask"
            totals[f"replenishment_{side}"] += int(data["remaining_quantity"])
        elif event.event_type is EventType.ORDER_CANCELLED:
            side = "bid" if data["side"] == Side.BUY.value else "ask"
            totals[f"cancel_{side}"] += int(data["cancelled_quantity"])
    return _Activity(time_us=time_us, **totals)


def _price_features(
    samples: tuple[_Midpoint, ...],
    current: Decimal | None,
    window_us: int,
) -> dict[FeatureKey, Decimal | None]:
    valid = tuple(sample for sample in samples if sample.value is not None)
    if current is None or not valid:
        return {
            FeatureKey.SHORT_TERM_RETURN: None,
            FeatureKey.SHORT_TERM_VOLATILITY: None,
            FeatureKey.PRICE_VELOCITY: None,
            FeatureKey.PRICE_ACCELERATION: None,
            FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS: None,
        }
    baseline = valid[0]
    change = current - baseline.value
    elapsed_us = max(1, valid[-1].time_us - baseline.time_us)
    elapsed_seconds = Decimal(elapsed_us) / Decimal(MICROSECONDS_PER_SECOND)
    velocity = change / elapsed_seconds
    returns = [
        (right.value / left.value) - Decimal(1)
        for left, right in zip(valid, valid[1:])
        if left.value and right.value is not None
    ]
    with localcontext() as context:
        context.prec = 28
        volatility = (
            sum((value * value for value in returns), Decimal(0)).sqrt()
            * Decimal(10_000)
            if returns
            else Decimal(0)
        )
    half_cutoff = valid[-1].time_us - window_us // 2
    pivot = next((sample for sample in valid if sample.time_us >= half_cutoff), valid[0])
    prior_elapsed = max(1, pivot.time_us - baseline.time_us)
    recent_elapsed = max(1, valid[-1].time_us - pivot.time_us)
    prior_velocity = (pivot.value - baseline.value) / (
        Decimal(prior_elapsed) / Decimal(MICROSECONDS_PER_SECOND)
    )
    recent_velocity = (current - pivot.value) / (
        Decimal(recent_elapsed) / Decimal(MICROSECONDS_PER_SECOND)
    )
    half_seconds = Decimal(max(1, window_us // 2)) / Decimal(MICROSECONDS_PER_SECOND)
    return {
        FeatureKey.SHORT_TERM_RETURN: (current / baseline.value) - Decimal(1),
        FeatureKey.SHORT_TERM_VOLATILITY: volatility,
        FeatureKey.PRICE_VELOCITY: velocity,
        FeatureKey.PRICE_ACCELERATION: (recent_velocity - prior_velocity) / half_seconds,
        FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS: change,
    }


def _window_midpoints(
    samples: deque[_Midpoint],
    simulation_time_us: int,
    window_us: int,
) -> tuple[_Midpoint, ...]:
    cutoff = simulation_time_us - window_us
    selected = [sample for sample in samples if sample.time_us >= cutoff]
    earlier = [sample for sample in samples if sample.time_us < cutoff]
    if earlier:
        selected.insert(0, earlier[-1])
    return tuple(selected)


def _best_size(book: MarketDepthView, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def _midpoint(book: MarketDepthView) -> Decimal | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return Decimal(book.best_bid + book.best_ask) / Decimal(2)


def _microprice(
    book: MarketDepthView,
    best_bid_size: int,
    best_ask_size: int,
) -> Decimal | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    total = best_bid_size + best_ask_size
    if total == 0:
        return None
    numerator = book.best_ask * best_bid_size + book.best_bid * best_ask_size
    return Decimal(numerator) / Decimal(total)


def _imbalance(bid: int, ask: int) -> Decimal:
    total = bid + ask
    return Decimal(bid - ask) / Decimal(total) if total else Decimal(0)


def _weighted_depth(book: MarketDepthView, side: Side, levels: int) -> Decimal:
    prices = book.bid_prices if side is Side.BUY else book.ask_prices
    side_levels = book.bids if side is Side.BUY else book.asks
    return sum(
        (
            Decimal(side_levels[price].total_quantity) / Decimal(rank)
            for rank, price in enumerate(prices[:levels], start=1)
        ),
        Decimal(0),
    )
