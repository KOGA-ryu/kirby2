"""Observable-state intensity modifiers for queue-reactive order flow."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable

from kirby2.exchange import OrderBook, Side

from .flow import FlowEvent, FlowEventFamily


class QueueStateVariable(str, Enum):
    BEST_BID_SIZE = "best_bid_size"
    BEST_ASK_SIZE = "best_ask_size"
    IMBALANCE = "imbalance"
    SPREAD_TICKS = "spread_ticks"
    DEPTH_NEAR_TOUCH_BID = "depth_near_touch_bid"
    DEPTH_NEAR_TOUCH_ASK = "depth_near_touch_ask"
    RECENT_DEPLETION_BID = "recent_depletion_bid"
    RECENT_DEPLETION_ASK = "recent_depletion_ask"
    RECENT_REPLENISHMENT_BID = "recent_replenishment_bid"
    RECENT_REPLENISHMENT_ASK = "recent_replenishment_ask"
    DEPLETION_RATIO_BID = "depletion_ratio_bid"
    DEPLETION_RATIO_ASK = "depletion_ratio_ask"
    REPLENISHMENT_RATIO_BID = "replenishment_ratio_bid"
    REPLENISHMENT_RATIO_ASK = "replenishment_ratio_ask"
    RECENT_AGGRESSIVE_FLOW_IMBALANCE = "recent_aggressive_flow_imbalance"
    SHORT_TERM_PRICE_MOVEMENT_TICKS = "short_term_price_movement_ticks"


@dataclass(frozen=True, slots=True)
class QueueReactiveState:
    simulation_time_us: int
    window_us: int
    best_bid_size: int
    best_ask_size: int
    imbalance: float
    spread_ticks: int | None
    depth_near_touch_bid: int
    depth_near_touch_ask: int
    recent_depletion_bid: int
    recent_depletion_ask: int
    recent_replenishment_bid: int
    recent_replenishment_ask: int
    recent_aggressive_buy_volume: int
    recent_aggressive_sell_volume: int
    recent_aggressive_flow_imbalance: float
    short_term_price_movement_ticks: float

    def value(self, variable: QueueStateVariable) -> float:
        direct = {
            QueueStateVariable.BEST_BID_SIZE: float(self.best_bid_size),
            QueueStateVariable.BEST_ASK_SIZE: float(self.best_ask_size),
            QueueStateVariable.IMBALANCE: self.imbalance,
            QueueStateVariable.SPREAD_TICKS: float(self.spread_ticks or 0),
            QueueStateVariable.DEPTH_NEAR_TOUCH_BID: float(self.depth_near_touch_bid),
            QueueStateVariable.DEPTH_NEAR_TOUCH_ASK: float(self.depth_near_touch_ask),
            QueueStateVariable.RECENT_DEPLETION_BID: float(self.recent_depletion_bid),
            QueueStateVariable.RECENT_DEPLETION_ASK: float(self.recent_depletion_ask),
            QueueStateVariable.RECENT_REPLENISHMENT_BID: float(
                self.recent_replenishment_bid
            ),
            QueueStateVariable.RECENT_REPLENISHMENT_ASK: float(
                self.recent_replenishment_ask
            ),
            QueueStateVariable.RECENT_AGGRESSIVE_FLOW_IMBALANCE: (
                self.recent_aggressive_flow_imbalance
            ),
            QueueStateVariable.SHORT_TERM_PRICE_MOVEMENT_TICKS: (
                self.short_term_price_movement_ticks
            ),
        }
        if variable in direct:
            return direct[variable]
        if variable is QueueStateVariable.DEPLETION_RATIO_BID:
            return _bounded_ratio(
                self.recent_depletion_bid,
                self.depth_near_touch_bid + self.recent_depletion_bid,
            )
        if variable is QueueStateVariable.DEPLETION_RATIO_ASK:
            return _bounded_ratio(
                self.recent_depletion_ask,
                self.depth_near_touch_ask + self.recent_depletion_ask,
            )
        if variable is QueueStateVariable.REPLENISHMENT_RATIO_BID:
            return _bounded_ratio(
                self.recent_replenishment_bid,
                self.depth_near_touch_bid + self.recent_replenishment_bid,
            )
        if variable is QueueStateVariable.REPLENISHMENT_RATIO_ASK:
            return _bounded_ratio(
                self.recent_replenishment_ask,
                self.depth_near_touch_ask + self.recent_replenishment_ask,
            )
        raise ValueError(f"unsupported queue state variable: {variable}")

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_size": self.best_ask_size,
            "best_bid_size": self.best_bid_size,
            "depth_near_touch_ask": self.depth_near_touch_ask,
            "depth_near_touch_bid": self.depth_near_touch_bid,
            "imbalance": round(self.imbalance, 9),
            "recent_aggressive_buy_volume": self.recent_aggressive_buy_volume,
            "recent_aggressive_flow_imbalance": round(
                self.recent_aggressive_flow_imbalance,
                9,
            ),
            "recent_aggressive_sell_volume": self.recent_aggressive_sell_volume,
            "recent_depletion_ask": self.recent_depletion_ask,
            "recent_depletion_bid": self.recent_depletion_bid,
            "recent_replenishment_ask": self.recent_replenishment_ask,
            "recent_replenishment_bid": self.recent_replenishment_bid,
            "short_term_price_movement_ticks": round(
                self.short_term_price_movement_ticks,
                9,
            ),
            "simulation_time_us": self.simulation_time_us,
            "spread_ticks": self.spread_ticks,
            "window_us": self.window_us,
        }


@runtime_checkable
class ResponseFunction(Protocol):
    def evaluate(self, value: float) -> float: ...

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ExponentialResponse:
    alpha: float
    minimum: float = 0.25
    maximum: float = 4.0

    def __post_init__(self) -> None:
        _validate_response_bounds(self.minimum, self.maximum)
        if not math.isfinite(self.alpha):
            raise ValueError("exponential response alpha must be finite")

    def evaluate(self, value: float) -> float:
        exponent = min(60.0, max(-60.0, self.alpha * value))
        return _clamp(math.exp(exponent), self.minimum, self.maximum)

    def as_dict(self) -> dict[str, object]:
        return {
            "alpha": self.alpha,
            "kind": "exponential",
            "maximum": self.maximum,
            "minimum": self.minimum,
        }


@dataclass(frozen=True, slots=True)
class SigmoidResponse:
    alpha: float
    midpoint: float = 0.0
    minimum: float = 0.5
    maximum: float = 2.0

    def __post_init__(self) -> None:
        _validate_response_bounds(self.minimum, self.maximum)
        if not math.isfinite(self.alpha) or not math.isfinite(self.midpoint):
            raise ValueError("sigmoid response parameters must be finite")

    def evaluate(self, value: float) -> float:
        exponent = min(60.0, max(-60.0, -self.alpha * (value - self.midpoint)))
        unit = 1.0 / (1.0 + math.exp(exponent))
        return self.minimum + (self.maximum - self.minimum) * unit

    def as_dict(self) -> dict[str, object]:
        return {
            "alpha": self.alpha,
            "kind": "sigmoid",
            "maximum": self.maximum,
            "midpoint": self.midpoint,
            "minimum": self.minimum,
        }


@dataclass(frozen=True, slots=True)
class PiecewiseResponse:
    points: tuple[tuple[float, float], ...]
    minimum: float = 0.25
    maximum: float = 4.0

    def __post_init__(self) -> None:
        _validate_response_bounds(self.minimum, self.maximum)
        if len(self.points) < 2:
            raise ValueError("piecewise response requires at least two points")
        xs = tuple(point[0] for point in self.points)
        if xs != tuple(sorted(xs)) or len(xs) != len(set(xs)):
            raise ValueError("piecewise response x values must be strictly increasing")
        if any(
            not math.isfinite(value)
            for point in self.points
            for value in point
        ):
            raise ValueError("piecewise response points must be finite")

    def evaluate(self, value: float) -> float:
        if value <= self.points[0][0]:
            return _clamp(self.points[0][1], self.minimum, self.maximum)
        if value >= self.points[-1][0]:
            return _clamp(self.points[-1][1], self.minimum, self.maximum)
        for (left_x, left_y), (right_x, right_y) in zip(
            self.points,
            self.points[1:],
        ):
            if left_x <= value <= right_x:
                fraction = (value - left_x) / (right_x - left_x)
                interpolated = left_y + fraction * (right_y - left_y)
                return _clamp(interpolated, self.minimum, self.maximum)
        raise RuntimeError("piecewise response failed to bracket a finite value")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "piecewise",
            "maximum": self.maximum,
            "minimum": self.minimum,
            "points": [list(point) for point in self.points],
        }


@dataclass(frozen=True, slots=True)
class BoundedLinearResponse:
    slope: float
    intercept: float = 1.0
    minimum: float = 0.25
    maximum: float = 4.0

    def __post_init__(self) -> None:
        _validate_response_bounds(self.minimum, self.maximum)
        if not math.isfinite(self.slope) or not math.isfinite(self.intercept):
            raise ValueError("bounded linear parameters must be finite")

    def evaluate(self, value: float) -> float:
        return _clamp(
            self.intercept + self.slope * value,
            self.minimum,
            self.maximum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "intercept": self.intercept,
            "kind": "bounded_linear",
            "maximum": self.maximum,
            "minimum": self.minimum,
            "slope": self.slope,
        }


@dataclass(frozen=True, slots=True)
class StateResponseTerm:
    variable: QueueStateVariable
    response: ResponseFunction
    direction: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.response, ResponseFunction):
            raise TypeError("state response term requires a response function")
        if not math.isfinite(self.direction) or self.direction == 0:
            raise ValueError("state response direction must be finite and nonzero")

    def evaluate(self, state: QueueReactiveState) -> tuple[float, float]:
        input_value = self.direction * state.value(self.variable)
        return input_value, self.response.evaluate(input_value)

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "response": self.response.as_dict(),
            "variable": self.variable.value,
        }


@dataclass(frozen=True, slots=True)
class QueueReactiveConfig:
    profile_id: str
    rules: Mapping[FlowEventFamily, tuple[StateResponseTerm, ...]]
    window_us: int = 1_000_000
    near_touch_levels: int = 3
    minimum_multiplier: float = 0.20
    maximum_multiplier: float = 5.0
    maximum_intensity: float = 100.0

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("queue-reactive profile ID must not be empty")
        if set(self.rules) != set(FlowEventFamily):
            raise ValueError("queue-reactive rules must cover every flow family")
        if type(self.window_us) is not int or self.window_us <= 0:
            raise ValueError("queue-reactive window must be positive microseconds")
        if type(self.near_touch_levels) is not int or self.near_touch_levels <= 0:
            raise ValueError("near-touch level count must be positive")
        _validate_response_bounds(self.minimum_multiplier, self.maximum_multiplier)
        if not math.isfinite(self.maximum_intensity) or self.maximum_intensity <= 0:
            raise ValueError("queue-reactive maximum intensity must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_intensity": self.maximum_intensity,
            "maximum_multiplier": self.maximum_multiplier,
            "minimum_multiplier": self.minimum_multiplier,
            "near_touch_levels": self.near_touch_levels,
            "profile_id": self.profile_id,
            "rules": {
                family.value: [term.as_dict() for term in self.rules[family]]
                for family in FlowEventFamily
            },
            "window_us": self.window_us,
        }


@dataclass(frozen=True, slots=True)
class ChannelIntensity:
    family: FlowEventFamily
    base_intensity: float
    state_multiplier: float
    final_intensity: float
    term_results: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "base_intensity": round(self.base_intensity, 9),
            "channel": self.family.value,
            "final_intensity": round(self.final_intensity, 9),
            "state_multiplier": round(self.state_multiplier, 9),
            "terms": list(self.term_results),
        }


@dataclass(frozen=True, slots=True)
class IntensityInspection:
    state: QueueReactiveState
    channels: tuple[ChannelIntensity, ...]

    @property
    def final_intensities(self) -> dict[FlowEventFamily, float]:
        return {channel.family: channel.final_intensity for channel in self.channels}

    def as_dict(self) -> dict[str, object]:
        return {
            "book_state": self.state.as_dict(),
            "channels": [channel.as_dict() for channel in self.channels],
        }


@runtime_checkable
class FlowIntensityModifier(Protocol):
    def initialize(self, book: OrderBook, simulation_time_us: int = 0) -> None: ...

    def observe(
        self,
        event: FlowEvent,
        book: OrderBook,
        simulation_time_us: int,
    ) -> None: ...

    def inspect(
        self,
        baseline_intensities: Mapping[FlowEventFamily, float],
        book: OrderBook,
        simulation_time_us: int,
    ) -> IntensityInspection: ...

    def replay_config(self) -> dict[str, object]: ...

    def runtime_state(self) -> dict[str, object]: ...


class QueueReactiveStateTracker:
    def __init__(self, window_us: int, near_touch_levels: int) -> None:
        self.window_us = window_us
        self.near_touch_levels = near_touch_levels
        self._queue_changes: deque[tuple[int, int, int, int, int]] = deque(maxlen=10_000)
        self._aggressive_flow: deque[tuple[int, int, int]] = deque(maxlen=10_000)
        self._midpoints: deque[tuple[int, float]] = deque(maxlen=10_000)
        self._previous_bid_depth = 0
        self._previous_ask_depth = 0
        self._last_trade_count = 0
        self._initialized = False

    def initialize(self, book: OrderBook, simulation_time_us: int = 0) -> None:
        self._previous_bid_depth = _near_touch_depth(
            book,
            Side.BUY,
            self.near_touch_levels,
        )
        self._previous_ask_depth = _near_touch_depth(
            book,
            Side.SELL,
            self.near_touch_levels,
        )
        self._last_trade_count = len(book.trades)
        midpoint = _midpoint(book)
        if midpoint is not None:
            self._midpoints.append((simulation_time_us, midpoint))
        self._initialized = True

    def observe(
        self,
        event: FlowEvent,
        book: OrderBook,
        simulation_time_us: int,
    ) -> None:
        if not self._initialized:
            self.initialize(book, simulation_time_us)
        bid_depth = _near_touch_depth(book, Side.BUY, self.near_touch_levels)
        ask_depth = _near_touch_depth(book, Side.SELL, self.near_touch_levels)
        bid_delta = bid_depth - self._previous_bid_depth
        ask_delta = ask_depth - self._previous_ask_depth
        self._queue_changes.append(
            (
                simulation_time_us,
                max(0, -bid_delta),
                max(0, -ask_delta),
                max(0, bid_delta),
                max(0, ask_delta),
            )
        )
        self._previous_bid_depth = bid_depth
        self._previous_ask_depth = ask_depth
        new_trades = book.trades[self._last_trade_count :]
        aggressive_buy = sum(
            trade.quantity for trade in new_trades if trade.taker_side is Side.BUY
        )
        aggressive_sell = sum(
            trade.quantity for trade in new_trades if trade.taker_side is Side.SELL
        )
        if aggressive_buy or aggressive_sell:
            self._aggressive_flow.append(
                (simulation_time_us, aggressive_buy, aggressive_sell)
            )
        self._last_trade_count = len(book.trades)
        midpoint = _midpoint(book)
        if midpoint is not None:
            self._midpoints.append((simulation_time_us, midpoint))
        self._prune(simulation_time_us)

    def snapshot(
        self,
        book: OrderBook,
        simulation_time_us: int,
    ) -> QueueReactiveState:
        if not self._initialized:
            self.initialize(book, simulation_time_us)
        self._prune(simulation_time_us)
        best_bid_size = _best_size(book, Side.BUY)
        best_ask_size = _best_size(book, Side.SELL)
        denominator = best_bid_size + best_ask_size
        imbalance = (
            (best_bid_size - best_ask_size) / denominator if denominator else 0.0
        )
        depletion_bid = sum(item[1] for item in self._queue_changes)
        depletion_ask = sum(item[2] for item in self._queue_changes)
        replenishment_bid = sum(item[3] for item in self._queue_changes)
        replenishment_ask = sum(item[4] for item in self._queue_changes)
        aggressive_buy = sum(item[1] for item in self._aggressive_flow)
        aggressive_sell = sum(item[2] for item in self._aggressive_flow)
        aggressive_total = aggressive_buy + aggressive_sell
        aggressive_imbalance = (
            (aggressive_buy - aggressive_sell) / aggressive_total
            if aggressive_total
            else 0.0
        )
        midpoint = _midpoint(book)
        price_movement = (
            midpoint - self._midpoints[0][1]
            if midpoint is not None and self._midpoints
            else 0.0
        )
        return QueueReactiveState(
            simulation_time_us=simulation_time_us,
            window_us=self.window_us,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            imbalance=imbalance,
            spread_ticks=(
                book.best_ask - book.best_bid
                if book.best_bid is not None and book.best_ask is not None
                else None
            ),
            depth_near_touch_bid=_near_touch_depth(
                book,
                Side.BUY,
                self.near_touch_levels,
            ),
            depth_near_touch_ask=_near_touch_depth(
                book,
                Side.SELL,
                self.near_touch_levels,
            ),
            recent_depletion_bid=depletion_bid,
            recent_depletion_ask=depletion_ask,
            recent_replenishment_bid=replenishment_bid,
            recent_replenishment_ask=replenishment_ask,
            recent_aggressive_buy_volume=aggressive_buy,
            recent_aggressive_sell_volume=aggressive_sell,
            recent_aggressive_flow_imbalance=aggressive_imbalance,
            short_term_price_movement_ticks=price_movement,
        )

    def _prune(self, simulation_time_us: int) -> None:
        cutoff = simulation_time_us - self.window_us
        for records in (self._queue_changes, self._aggressive_flow, self._midpoints):
            while records and records[0][0] < cutoff:
                records.popleft()

    def runtime_state(self) -> dict[str, object]:
        return {
            "aggressive_flow": [list(item) for item in self._aggressive_flow],
            "initialized": self._initialized,
            "last_trade_count": self._last_trade_count,
            "midpoints": [list(item) for item in self._midpoints],
            "near_touch_levels": self.near_touch_levels,
            "previous_ask_depth": self._previous_ask_depth,
            "previous_bid_depth": self._previous_bid_depth,
            "queue_changes": [list(item) for item in self._queue_changes],
            "window_us": self.window_us,
        }


class QueueReactiveFlowModifier:
    def __init__(self, config: QueueReactiveConfig | None = None) -> None:
        self.config = config or default_queue_reactive_config()
        self.tracker = QueueReactiveStateTracker(
            self.config.window_us,
            self.config.near_touch_levels,
        )
        self.last_inspection: IntensityInspection | None = None

    def initialize(self, book: OrderBook, simulation_time_us: int = 0) -> None:
        self.tracker.initialize(book, simulation_time_us)

    def observe(
        self,
        event: FlowEvent,
        book: OrderBook,
        simulation_time_us: int,
    ) -> None:
        self.tracker.observe(event, book, simulation_time_us)

    def inspect(
        self,
        baseline_intensities: Mapping[FlowEventFamily, float],
        book: OrderBook,
        simulation_time_us: int,
    ) -> IntensityInspection:
        state = self.tracker.snapshot(book, simulation_time_us)
        return self.inspect_state(baseline_intensities, state)

    def inspect_state(
        self,
        baseline_intensities: Mapping[FlowEventFamily, float],
        state: QueueReactiveState,
    ) -> IntensityInspection:
        if set(baseline_intensities) != set(FlowEventFamily):
            raise ValueError("baseline intensities must cover every flow family")
        channels: list[ChannelIntensity] = []
        for family in FlowEventFamily:
            base = float(baseline_intensities[family])
            if not math.isfinite(base) or base < 0:
                raise ValueError("baseline intensities must be finite and nonnegative")
            multiplier = 1.0
            term_results: list[dict[str, object]] = []
            for term in self.config.rules[family]:
                input_value, term_multiplier = term.evaluate(state)
                multiplier *= term_multiplier
                term_results.append(
                    {
                        "input": round(input_value, 9),
                        "multiplier": round(term_multiplier, 9),
                        "variable": term.variable.value,
                    }
                )
            multiplier = _clamp(
                multiplier,
                self.config.minimum_multiplier,
                self.config.maximum_multiplier,
            )
            final = min(self.config.maximum_intensity, base * multiplier)
            channels.append(
                ChannelIntensity(
                    family=family,
                    base_intensity=base,
                    state_multiplier=multiplier,
                    final_intensity=final,
                    term_results=tuple(term_results),
                )
            )
        inspection = IntensityInspection(state, tuple(channels))
        self.last_inspection = inspection
        return inspection

    def replay_config(self) -> dict[str, object]:
        return {"kind": "queue_reactive", **self.config.as_dict()}

    def runtime_state(self) -> dict[str, object]:
        return {
            "config": self.config.as_dict(),
            "last_inspection": (
                None
                if self.last_inspection is None
                else self.last_inspection.as_dict()
            ),
            "tracker": self.tracker.runtime_state(),
        }


def default_queue_reactive_config() -> QueueReactiveConfig:
    spread_replenishment = PiecewiseResponse(
        points=((1.0, 0.8), (2.0, 1.0), (4.0, 1.5), (8.0, 2.2)),
        minimum=0.6,
        maximum=2.5,
    )
    depletion_replenishment = SigmoidResponse(
        alpha=7.0,
        midpoint=0.18,
        minimum=0.75,
        maximum=2.2,
    )
    aggressive_follow = SigmoidResponse(
        alpha=3.5,
        midpoint=0.05,
        minimum=0.65,
        maximum=1.9,
    )
    cancel_pressure = SigmoidResponse(
        alpha=4.0,
        midpoint=0.10,
        minimum=0.60,
        maximum=2.2,
    )
    return QueueReactiveConfig(
        profile_id="observable_queue_feedback_v1",
        rules={
            FlowEventFamily.LIMIT_BUY: (
                StateResponseTerm(
                    QueueStateVariable.DEPLETION_RATIO_BID,
                    depletion_replenishment,
                ),
                StateResponseTerm(
                    QueueStateVariable.SPREAD_TICKS,
                    spread_replenishment,
                ),
            ),
            FlowEventFamily.LIMIT_SELL: (
                StateResponseTerm(
                    QueueStateVariable.DEPLETION_RATIO_ASK,
                    depletion_replenishment,
                ),
                StateResponseTerm(
                    QueueStateVariable.SPREAD_TICKS,
                    spread_replenishment,
                ),
            ),
            FlowEventFamily.MARKET_BUY: (
                StateResponseTerm(
                    QueueStateVariable.IMBALANCE,
                    ExponentialResponse(alpha=0.9, minimum=0.45, maximum=2.5),
                ),
                StateResponseTerm(
                    QueueStateVariable.RECENT_AGGRESSIVE_FLOW_IMBALANCE,
                    aggressive_follow,
                ),
            ),
            FlowEventFamily.MARKET_SELL: (
                StateResponseTerm(
                    QueueStateVariable.IMBALANCE,
                    ExponentialResponse(alpha=0.9, minimum=0.45, maximum=2.5),
                    direction=-1.0,
                ),
                StateResponseTerm(
                    QueueStateVariable.RECENT_AGGRESSIVE_FLOW_IMBALANCE,
                    aggressive_follow,
                    direction=-1.0,
                ),
            ),
            FlowEventFamily.CANCEL_BID: (
                StateResponseTerm(
                    QueueStateVariable.RECENT_AGGRESSIVE_FLOW_IMBALANCE,
                    cancel_pressure,
                    direction=-1.0,
                ),
                StateResponseTerm(
                    QueueStateVariable.SHORT_TERM_PRICE_MOVEMENT_TICKS,
                    BoundedLinearResponse(
                        slope=-0.12,
                        minimum=0.5,
                        maximum=2.0,
                    ),
                ),
            ),
            FlowEventFamily.CANCEL_ASK: (
                StateResponseTerm(
                    QueueStateVariable.RECENT_AGGRESSIVE_FLOW_IMBALANCE,
                    cancel_pressure,
                ),
                StateResponseTerm(
                    QueueStateVariable.SHORT_TERM_PRICE_MOVEMENT_TICKS,
                    BoundedLinearResponse(
                        slope=0.12,
                        minimum=0.5,
                        maximum=2.0,
                    ),
                ),
            ),
        },
    )


def imbalance_probe_state(imbalance: float, total_top_size: int = 2_000) -> QueueReactiveState:
    if not math.isfinite(imbalance) or not -1.0 <= imbalance <= 1.0:
        raise ValueError("probe imbalance must lie in [-1, 1]")
    if type(total_top_size) is not int or total_top_size <= 0:
        raise ValueError("probe top size must be positive")
    bid_size = round(total_top_size * (1.0 + imbalance) / 2.0)
    ask_size = total_top_size - bid_size
    return QueueReactiveState(
        simulation_time_us=0,
        window_us=1_000_000,
        best_bid_size=bid_size,
        best_ask_size=ask_size,
        imbalance=(bid_size - ask_size) / total_top_size,
        spread_ticks=2,
        depth_near_touch_bid=bid_size * 2,
        depth_near_touch_ask=ask_size * 2,
        recent_depletion_bid=0,
        recent_depletion_ask=0,
        recent_replenishment_bid=0,
        recent_replenishment_ask=0,
        recent_aggressive_buy_volume=0,
        recent_aggressive_sell_volume=0,
        recent_aggressive_flow_imbalance=0.0,
        short_term_price_movement_ticks=0.0,
    )


def _best_size(book: OrderBook, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def _near_touch_depth(book: OrderBook, side: Side, levels: int) -> int:
    prices = book.bid_prices if side is Side.BUY else book.ask_prices
    side_levels = book.bids if side is Side.BUY else book.asks
    return sum(side_levels[price].total_quantity for price in prices[:levels])


def _midpoint(book: OrderBook) -> float | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return (book.best_bid + book.best_ask) / 2.0


def _bounded_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return min(1.0, max(0.0, numerator / denominator))


def _validate_response_bounds(minimum: float, maximum: float) -> None:
    if (
        not math.isfinite(minimum)
        or not math.isfinite(maximum)
        or minimum <= 0
        or maximum < minimum
    ):
        raise ValueError("response bounds must be finite, positive, and ordered")


def _clamp(value: float, minimum: float, maximum: float) -> float:
    if not math.isfinite(value):
        raise ValueError("response produced a non-finite multiplier")
    return min(maximum, max(minimum, value))
