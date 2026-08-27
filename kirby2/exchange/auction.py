"""Deterministic single-price auction book with documented allocation rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .mechanics_models import (
    AuctionIndication,
    ManagedOrder,
    OrderInstruction,
    SelfTradePreventionMode,
)
from .models import Side


@dataclass(frozen=True, slots=True)
class AuctionExecution:
    trade_id: str
    price_ticks: int
    quantity: int
    buy_order_id: str
    sell_order_id: str


@dataclass(frozen=True, slots=True)
class AuctionUncrossResult:
    indication: AuctionIndication
    executions: tuple[AuctionExecution, ...]
    stp_cancellations: tuple[tuple[str, str], ...]
    expirations: tuple[tuple[str, int], ...]

    @property
    def matched_quantity(self) -> int:
        return sum(item.quantity for item in self.executions)


class AuctionBook:
    """One auction pool using price priority and FIFO within equal prices.

    The clearing-price tie break is: maximum match, minimum absolute imbalance,
    closest to the configured reference, then the lower tick. Allocation puts
    market orders first, then better-priced limits, then FIFO at a price. If the
    two allocation heads share an account, the later arrival is the deterministic
    aggressor for self-trade prevention. Unmatched auction-only quantity expires.
    """

    def __init__(self) -> None:
        self._orders: dict[str, ManagedOrder] = {}
        self._trade_sequence = 0

    @property
    def orders(self) -> tuple[ManagedOrder, ...]:
        return tuple(
            self._orders[key]
            for key in sorted(
                self._orders,
                key=lambda item: self._orders[item].arrival_sequence,
            )
        )

    @property
    def active_orders(self) -> tuple[ManagedOrder, ...]:
        return tuple(order for order in self.orders if order.remaining_quantity > 0)

    def add(self, order: ManagedOrder) -> None:
        order_id = order.request.order_id
        if order_id in self._orders:
            raise ValueError(f"duplicate auction order ID: {order_id}")
        if order.remaining_quantity <= 0:
            raise ValueError("auction order must have positive remaining quantity")
        order.status = "AUCTION_WORKING"
        order.resting_sequence = order.arrival_sequence
        self._orders[order_id] = order
        self.assert_invariants()

    def cancel(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        if order is None or order.remaining_quantity <= 0:
            raise ValueError(f"auction order is not active: {order_id}")
        quantity = order.remaining_quantity
        order.cancelled_quantity += quantity
        order.status = "CANCELLED"
        self.assert_invariants()
        return quantity

    def expire(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        if order is None or order.remaining_quantity <= 0:
            raise ValueError(f"auction order is not active: {order_id}")
        quantity = order.remaining_quantity
        order.expired_quantity += quantity
        order.status = "EXPIRED"
        self.assert_invariants()
        return quantity

    def indication(self, reference_price_ticks: int) -> AuctionIndication:
        active = self.active_orders
        if not active:
            return AuctionIndication(None, 0, 0, None)
        candidates = {
            order.request.price_ticks
            for order in active
            if order.request.price_ticks is not None
        }
        candidates.add(reference_price_ticks)
        outcomes = []
        for price in sorted(candidates):
            demand = sum(
                order.remaining_quantity
                for order in active
                if order.request.side is Side.BUY
                and _auction_marketable(order, price)
            )
            supply = sum(
                order.remaining_quantity
                for order in active
                if order.request.side is Side.SELL
                and _auction_marketable(order, price)
            )
            matched = min(demand, supply)
            imbalance = demand - supply
            outcomes.append((price, matched, imbalance))
        price, matched, signed_imbalance = min(
            outcomes,
            key=lambda item: (
                -item[1],
                abs(item[2]),
                abs(item[0] - reference_price_ticks),
                item[0],
            ),
        )
        imbalance_side = (
            Side.BUY
            if signed_imbalance > 0
            else Side.SELL
            if signed_imbalance < 0
            else None
        )
        return AuctionIndication(
            price,
            matched,
            abs(signed_imbalance),
            imbalance_side,
        )

    def uncross(
        self,
        reference_price_ticks: int,
        stp_mode: Callable[[str], SelfTradePreventionMode],
    ) -> AuctionUncrossResult:
        indication = self.indication(reference_price_ticks)
        price = indication.clearing_price_ticks
        if price is None:
            return AuctionUncrossResult(indication, (), (), ())
        buys = sorted(
            (
                order
                for order in self.active_orders
                if order.request.side is Side.BUY
                and _auction_marketable(order, price)
            ),
            key=_buy_priority,
        )
        sells = sorted(
            (
                order
                for order in self.active_orders
                if order.request.side is Side.SELL
                and _auction_marketable(order, price)
            ),
            key=_sell_priority,
        )
        executions: list[AuctionExecution] = []
        stp_cancellations: list[tuple[str, str]] = []
        buy_index = 0
        sell_index = 0
        while buy_index < len(buys) and sell_index < len(sells):
            buy = buys[buy_index]
            sell = sells[sell_index]
            if not buy.remaining_quantity:
                buy_index += 1
                continue
            if not sell.remaining_quantity:
                sell_index += 1
                continue
            if buy.request.account_id == sell.request.account_id:
                mode = stp_mode(buy.request.account_id)
                if mode is not SelfTradePreventionMode.NONE:
                    cancelled = self._apply_auction_stp(buy, sell, mode)
                    stp_cancellations.extend(cancelled)
                    continue
            quantity = min(buy.remaining_quantity, sell.remaining_quantity)
            buy.filled_quantity += quantity
            sell.filled_quantity += quantity
            buy.status = "FILLED" if not buy.remaining_quantity else "PARTIALLY_FILLED"
            sell.status = (
                "FILLED" if not sell.remaining_quantity else "PARTIALLY_FILLED"
            )
            self._trade_sequence += 1
            executions.append(
                AuctionExecution(
                    f"AT{self._trade_sequence:06d}",
                    price,
                    quantity,
                    buy.request.order_id,
                    sell.request.order_id,
                )
            )
        expirations: list[tuple[str, int]] = []
        for order in self.active_orders:
            quantity = order.remaining_quantity
            order.expired_quantity += quantity
            order.status = "EXPIRED"
            expirations.append((order.request.order_id, quantity))
        self.assert_invariants()
        return AuctionUncrossResult(
            indication,
            tuple(executions),
            tuple(stp_cancellations),
            tuple(expirations),
        )

    def assert_invariants(self) -> None:
        sequences = [order.arrival_sequence for order in self.orders]
        if len(sequences) != len(set(sequences)):
            raise RuntimeError("auction arrival sequences are not unique")
        for order in self.orders:
            if min(
                order.filled_quantity,
                order.cancelled_quantity,
                order.expired_quantity,
                order.remaining_quantity,
            ) < 0:
                raise RuntimeError("auction order contains negative quantity")
            if (
                order.filled_quantity
                + order.cancelled_quantity
                + order.expired_quantity
                + order.remaining_quantity
                != order.request.quantity
            ):
                raise RuntimeError("auction order quantity does not conserve")
            if order.remaining_quantity > 0 and order.status not in {
                "AUCTION_WORKING",
                "PARTIALLY_FILLED",
            }:
                raise RuntimeError("inactive auction order retains quantity")

    def _apply_auction_stp(
        self,
        buy: ManagedOrder,
        sell: ManagedOrder,
        mode: SelfTradePreventionMode,
    ) -> tuple[tuple[str, str], ...]:
        earlier, later = sorted(
            (buy, sell),
            key=lambda order: order.arrival_sequence,
        )
        if mode is SelfTradePreventionMode.CANCEL_AGGRESSOR:
            targets = (later,)
        elif mode is SelfTradePreventionMode.CANCEL_RESTING:
            targets = (earlier,)
        elif mode is SelfTradePreventionMode.CANCEL_BOTH:
            targets = (earlier, later)
        else:  # pragma: no cover - caller excludes NONE
            raise RuntimeError("unsupported auction self-trade prevention mode")
        cancelled: list[tuple[str, str]] = []
        for order in targets:
            quantity = order.remaining_quantity
            order.cancelled_quantity += quantity
            order.status = "CANCELLED_STP"
            cancelled.append((order.request.order_id, mode.value))
        return tuple(cancelled)


def _auction_marketable(order: ManagedOrder, price_ticks: int) -> bool:
    if order.request.instruction is OrderInstruction.MARKET:
        return True
    if order.request.side is Side.BUY:
        return order.request.price_ticks >= price_ticks  # type: ignore[operator]
    return order.request.price_ticks <= price_ticks  # type: ignore[operator]


def _buy_priority(order: ManagedOrder) -> tuple[int, int, int]:
    if order.request.instruction is OrderInstruction.MARKET:
        return (0, 0, order.arrival_sequence)
    return (1, -int(order.request.price_ticks), order.arrival_sequence)


def _sell_priority(order: ManagedOrder) -> tuple[int, int, int]:
    if order.request.instruction is OrderInstruction.MARKET:
        return (0, 0, order.arrival_sequence)
    return (1, int(order.request.price_ticks), order.arrival_sequence)
