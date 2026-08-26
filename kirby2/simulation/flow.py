"""Seeded Poisson-style synthetic order flow for the deterministic exchange."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderType, Side
from kirby2.session import EventType

from .clock import MICROSECONDS_PER_SECOND, SimulationClock
from .config import SimulationConfig
from .rng import SeededRng


class FlowEventFamily(str, Enum):
    LIMIT_BUY = "limit_buy"
    LIMIT_SELL = "limit_sell"
    MARKET_BUY = "market_buy"
    MARKET_SELL = "market_sell"
    CANCEL_BID = "cancel_bid"
    CANCEL_ASK = "cancel_ask"


_FAMILY_ORDER = (
    FlowEventFamily.LIMIT_BUY,
    FlowEventFamily.LIMIT_SELL,
    FlowEventFamily.MARKET_BUY,
    FlowEventFamily.MARKET_SELL,
    FlowEventFamily.CANCEL_BID,
    FlowEventFamily.CANCEL_ASK,
)


@dataclass(frozen=True, slots=True)
class FlowEvent:
    sequence: int
    simulation_time_us: int
    family: FlowEventFamily
    applied: bool
    command: dict[str, Any] | None
    reason: str | None
    exchange_event_start: int | None
    exchange_event_end: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "command": self.command,
            "exchange_event_end": self.exchange_event_end,
            "exchange_event_start": self.exchange_event_start,
            "family": self.family.value,
            "flow_sequence": self.sequence,
            "reason": self.reason,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(slots=True)
class SimulationResult:
    seed: int
    seconds: int
    config: SimulationConfig
    clock: SimulationClock
    book: OrderBook
    flow_events: tuple[FlowEvent, ...]
    initial_exchange_event_count: int
    initial_trade_count: int
    flow_model_config: dict[str, object] | None = None
    intensity_modifier_config: dict[str, object] | None = None
    distribution_profile_config: dict[str, object] | None = None
    intraday_profile_config: dict[str, object] | None = None

    def replay_json_lines(self) -> str:
        header = {
            "config": self.config.as_dict(),
            "duration_seconds": self.seconds,
            "record_type": "simulation_config",
            "seed": self.seed,
        }
        if self.flow_model_config is not None:
            header["flow_model"] = self.flow_model_config
        if self.intensity_modifier_config is not None:
            header["intensity_modifier"] = self.intensity_modifier_config
        if self.distribution_profile_config is not None:
            header["distribution_profile"] = self.distribution_profile_config
        if self.intraday_profile_config is not None:
            header["intraday_profile"] = self.intraday_profile_config
        lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
        exchange_events = {event.sequence: event for event in self.book.journal.events}

        for sequence in range(1, self.initial_exchange_event_count + 1):
            lines.append(self._exchange_record(exchange_events[sequence]))

        for flow_event in self.flow_events:
            flow_record = {"record_type": "flow_event", **flow_event.as_dict()}
            lines.append(json.dumps(flow_record, sort_keys=True, separators=(",", ":")))
            if flow_event.exchange_event_start is None or flow_event.exchange_event_end is None:
                continue
            for sequence in range(
                flow_event.exchange_event_start,
                flow_event.exchange_event_end + 1,
            ):
                lines.append(self._exchange_record(exchange_events[sequence]))

        return "\n".join(lines)

    def replay_sha256(self) -> str:
        return hashlib.sha256(self.replay_json_lines().encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        self.book.assert_invariants()
        flow_exchange_events = self.book.journal.events[self.initial_exchange_event_count :]
        price_changes = sum(
            event.event_type in {EventType.BEST_BID_CHANGED, EventType.BEST_ASK_CHANGED}
            for event in flow_exchange_events
        )
        trades = self.book.trades[self.initial_trade_count :]
        family_counts = Counter(event.family.value for event in self.flow_events)
        applied_family_counts = Counter(
            event.family.value for event in self.flow_events if event.applied
        )
        best_bid = self.book.best_bid
        best_ask = self.book.best_ask
        summary = {
            "applied_event_count": sum(event.applied for event in self.flow_events),
            "applied_event_family_counts": {
                family.value: applied_family_counts[family.value] for family in _FAMILY_ORDER
            },
            "ending_best_ask": self._price_string(best_ask),
            "ending_best_ask_ticks": best_ask,
            "ending_best_bid": self._price_string(best_bid),
            "ending_best_bid_ticks": best_bid,
            "ending_depth": {
                "active_orders": len(self.book.active_orders),
                "ask_levels": len(self.book.ask_prices),
                "ask_quantity": sum(level.total_quantity for level in self.book.asks.values()),
                "bid_levels": len(self.book.bid_prices),
                "bid_quantity": sum(level.total_quantity for level in self.book.bids.values()),
            },
            "event_count": len(self.flow_events),
            "event_family_counts": {
                family.value: family_counts[family.value] for family in _FAMILY_ORDER
            },
            "exchange_event_count": len(self.book.journal.events),
            "invariant_status": "PASS",
            "price_changes": price_changes,
            "replay_sha256": self.replay_sha256(),
            "seed": self.seed,
            "simulation_time_us": self.clock.current_time_us,
            "skipped_event_count": sum(not event.applied for event in self.flow_events),
            "total_traded_volume": sum(trade.quantity for trade in trades),
            "trade_count": len(trades),
        }
        if self.flow_model_config is not None:
            summary["flow_model"] = self.flow_model_config
        if self.intensity_modifier_config is not None:
            summary["intensity_modifier"] = self.intensity_modifier_config
        if self.distribution_profile_config is not None:
            summary["distribution_profile"] = self.distribution_profile_config
        if self.intraday_profile_config is not None:
            summary["intraday_profile"] = self.intraday_profile_config
        return summary

    def _price_string(self, price_ticks: int | None) -> str | None:
        if price_ticks is None:
            return None
        return format(self.config.tick_size * price_ticks, "f")

    @staticmethod
    def _exchange_record(event: Any) -> str:
        record = {"record_type": "exchange_event", **event.as_dict()}
        return json.dumps(record, sort_keys=True, separators=(",", ":"))


class SyntheticOrderFlow:
    def __init__(self, seed: int, config: SimulationConfig | None = None) -> None:
        if type(seed) is not int:
            raise TypeError("seed must be an integer")
        self.seed = seed
        self.config = config or SimulationConfig()
        self.rng = SeededRng(seed)
        self.clock = SimulationClock()
        self.book = OrderBook()

    def run(self, seconds: int) -> SimulationResult:
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        self._initialize_book()
        initial_exchange_event_count = len(self.book.journal.events)
        initial_trade_count = len(self.book.trades)
        end_time_us = seconds * MICROSECONDS_PER_SECOND
        schedule: list[tuple[int, int, FlowEventFamily]] = []
        rates = self._effective_rates()

        for rank, family in enumerate(_FAMILY_ORDER):
            rate = rates[family]
            if rate > 0:
                first_arrival = self.rng.exponential_interval_microseconds(rate)
                heapq.heappush(schedule, (first_arrival, rank, family))

        flow_events: list[FlowEvent] = []
        while schedule:
            arrival_time_us, rank, family = heapq.heappop(schedule)
            if arrival_time_us > end_time_us:
                break
            self.clock.advance_to(arrival_time_us)
            flow_sequence = len(flow_events) + 1
            flow_events.append(self._apply_arrival(flow_sequence, family))

            next_arrival = arrival_time_us + self.rng.exponential_interval_microseconds(
                rates[family]
            )
            heapq.heappush(schedule, (next_arrival, rank, family))

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

    def _initialize_book(self) -> None:
        for depth in range(self.config.initial_depth):
            bid_price = (
                self.config.initial_mid_ticks
                - self.config.initial_half_spread_ticks
                - depth
            )
            ask_price = (
                self.config.initial_mid_ticks
                + self.config.initial_half_spread_ticks
                + depth
            )
            bid_quantity = self._draw_initial_queue_size()
            ask_quantity = self._draw_initial_queue_size()
            self.book.process(
                Order.limit(f"INIT-B-{depth + 1:03d}", Side.BUY, bid_quantity, bid_price)
            )
            self.book.process(
                Order.limit(f"INIT-A-{depth + 1:03d}", Side.SELL, ask_quantity, ask_price)
            )

    def _draw_initial_queue_size(self) -> int:
        return self.config.queue_size_distribution.draw(self.rng)

    def _effective_rates(self) -> dict[FlowEventFamily, float]:
        rates = self.config.rates
        intensity = self.config.event_intensity
        return {
            FlowEventFamily.LIMIT_BUY: rates.limit_buy_rate * intensity,
            FlowEventFamily.LIMIT_SELL: rates.limit_sell_rate * intensity,
            FlowEventFamily.MARKET_BUY: rates.market_buy_rate * intensity,
            FlowEventFamily.MARKET_SELL: rates.market_sell_rate * intensity,
            FlowEventFamily.CANCEL_BID: rates.cancel_bid_rate * intensity,
            FlowEventFamily.CANCEL_ASK: rates.cancel_ask_rate * intensity,
        }

    def _apply_arrival(self, sequence: int, family: FlowEventFamily) -> FlowEvent:
        exchange_start = len(self.book.journal.events) + 1
        command: dict[str, Any] | None
        reason: str | None = None

        if family is FlowEventFamily.LIMIT_BUY:
            command, reason = self._submit_limit(sequence, Side.BUY)
        elif family is FlowEventFamily.LIMIT_SELL:
            command, reason = self._submit_limit(sequence, Side.SELL)
        elif family is FlowEventFamily.MARKET_BUY:
            command = self._submit_market(sequence, Side.BUY)
        elif family is FlowEventFamily.MARKET_SELL:
            command = self._submit_market(sequence, Side.SELL)
        elif family is FlowEventFamily.CANCEL_BID:
            command, reason = self._submit_cancel(sequence, Side.BUY)
        else:
            command, reason = self._submit_cancel(sequence, Side.SELL)

        applied = command is not None
        exchange_end = len(self.book.journal.events) if applied else None
        event = FlowEvent(
            sequence=sequence,
            simulation_time_us=self.clock.current_time_us,
            family=family,
            applied=applied,
            command=command,
            reason=reason,
            exchange_event_start=exchange_start if applied else None,
            exchange_event_end=exchange_end,
        )
        self._after_flow_event(event)
        return event

    def _submit_limit(self, sequence: int, side: Side) -> tuple[dict[str, Any] | None, str | None]:
        family = (
            FlowEventFamily.LIMIT_BUY if side is Side.BUY else FlowEventFamily.LIMIT_SELL
        )
        depth = self._draw_depth(family)
        price_ticks = self._resting_price(side, depth)
        if price_ticks is None:
            return None, "no_valid_non_crossing_price"
        quantity = self._draw_order_size(family)
        order_id = self._command_id(sequence, "LB" if side is Side.BUY else "LS")
        self.book.process(Order.limit(order_id, side, quantity, price_ticks))
        return {
            "depth": depth,
            "order_id": order_id,
            "order_type": OrderType.LIMIT.value,
            "price_ticks": price_ticks,
            "quantity": quantity,
            "side": side.value,
        }, None

    def _submit_market(self, sequence: int, side: Side) -> dict[str, Any]:
        family = (
            FlowEventFamily.MARKET_BUY if side is Side.BUY else FlowEventFamily.MARKET_SELL
        )
        quantity = self._draw_order_size(family)
        order_id = self._command_id(sequence, "MB" if side is Side.BUY else "MS")
        self.book.process(Order.market(order_id, side, quantity))
        return {
            "order_id": order_id,
            "order_type": OrderType.MARKET.value,
            "quantity": quantity,
            "side": side.value,
        }

    def _submit_cancel(self, sequence: int, side: Side) -> tuple[dict[str, Any] | None, str | None]:
        candidates = sorted(
            (
                order
                for order in self.book.active_orders.values()
                if order.side is side
                and order.order_type is OrderType.LIMIT
                and order.owner is OrderOwner.SIMULATED
            ),
            key=lambda order: (order.resting_sequence, order.order_id),
        )
        if not candidates:
            return None, "no_active_liquidity"
        target = candidates[self.rng.index(len(candidates))]
        command_id = self._command_id(sequence, "CB" if side is Side.BUY else "CA")
        target_details = {
            "cancelled_quantity": target.remaining_quantity,
            "command_id": command_id,
            "order_type": OrderType.CANCEL.value,
            "price_ticks": target.price_ticks,
            "side": side.value,
            "target_order_id": target.order_id,
        }
        self.book.cancel(target.order_id, command_id)
        return target_details, None

    def _resting_price(self, side: Side, depth: int) -> int | None:
        if side is Side.BUY:
            if self.book.best_bid is not None:
                price = self.book.best_bid - depth
            elif self.book.best_ask is not None:
                price = self.book.best_ask - 1 - depth
            else:
                price = self.config.initial_mid_ticks - self.config.initial_half_spread_ticks - depth
            if self.book.best_ask is not None:
                price = min(price, self.book.best_ask - 1)
            return price if price > 0 else None

        if self.book.best_ask is not None:
            price = self.book.best_ask + depth
        elif self.book.best_bid is not None:
            price = self.book.best_bid + 1 + depth
        else:
            price = self.config.initial_mid_ticks + self.config.initial_half_spread_ticks + depth
        if self.book.best_bid is not None:
            price = max(price, self.book.best_bid + 1)
        return price

    def _draw_order_size(self, family: FlowEventFamily) -> int:
        return self.config.order_size_distribution.draw(self.rng)

    def _draw_depth(self, family: FlowEventFamily) -> int:
        return self.config.depth_placement_distribution.draw(self.rng)

    def _after_flow_event(self, event: FlowEvent) -> None:
        return None

    @staticmethod
    def _command_id(sequence: int, suffix: str) -> str:
        return f"FLOW-{sequence:08d}-{suffix}"


def run_simulation(
    seed: int,
    seconds: int,
    config: SimulationConfig | None = None,
) -> SimulationResult:
    return SyntheticOrderFlow(seed=seed, config=config).run(seconds)
