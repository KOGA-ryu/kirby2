"""Value objects used by the Kirby2 matching engine."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque


def _require_integer(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")


def _require_positive_ticks(value: int, name: str = "price_ticks") -> None:
    _require_integer(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    CANCEL = "cancel"


class OrderOwner(str, Enum):
    SIMULATED = "simulated"
    PLAYER = "player"


class OrderStatus(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    APPLIED = "applied"


@dataclass(slots=True)
class Order:
    order_id: str
    order_type: OrderType
    original_quantity: int
    side: Side | None = None
    price_ticks: int | None = None
    owner: OrderOwner = OrderOwner.SIMULATED
    cancel_target_id: str | None = None
    remaining_quantity: int = field(init=False)
    filled_quantity: int = field(default=0, init=False)
    cancelled_quantity: int = field(default=0, init=False)
    resting_sequence: int | None = field(default=None, init=False)
    status: OrderStatus = field(default=OrderStatus.NEW, init=False)

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must not be empty")
        _require_integer(self.original_quantity, "original_quantity")
        if self.order_type is OrderType.CANCEL:
            if self.original_quantity != 0:
                raise ValueError("cancel commands must have quantity 0")
            if not self.cancel_target_id:
                raise ValueError("cancel commands require cancel_target_id")
            if self.side is not None or self.price_ticks is not None:
                raise ValueError("cancel commands cannot carry side or price")
        else:
            if self.original_quantity <= 0:
                raise ValueError("order quantity must be positive")
            if self.side is None:
                raise ValueError("trading orders require a side")
            if self.order_type is OrderType.LIMIT:
                _require_positive_ticks(self.price_ticks, "price_ticks")  # type: ignore[arg-type]
            elif self.price_ticks is not None:
                raise ValueError("market orders cannot carry a limit price")
        self.remaining_quantity = self.original_quantity

    @classmethod
    def limit(
        cls,
        order_id: str,
        side: Side,
        quantity: int,
        price_ticks: int,
        owner: OrderOwner = OrderOwner.SIMULATED,
    ) -> Order:
        return cls(order_id, OrderType.LIMIT, quantity, side, price_ticks, owner)

    @classmethod
    def market(
        cls,
        order_id: str,
        side: Side,
        quantity: int,
        owner: OrderOwner = OrderOwner.SIMULATED,
    ) -> Order:
        return cls(order_id, OrderType.MARKET, quantity, side, None, owner)

    @classmethod
    def cancel(cls, command_id: str, target_order_id: str) -> Order:
        return cls(
            order_id=command_id,
            order_type=OrderType.CANCEL,
            original_quantity=0,
            cancel_target_id=target_order_id,
        )

    def apply_fill(self, quantity: int) -> None:
        if quantity <= 0 or quantity > self.remaining_quantity:
            raise ValueError("fill quantity must be positive and no greater than remaining quantity")
        self.remaining_quantity -= quantity
        self.filled_quantity += quantity
        self.status = (
            OrderStatus.FILLED
            if self.remaining_quantity == 0
            else OrderStatus.PARTIALLY_FILLED
        )

    def cancel_remainder(self, status: OrderStatus = OrderStatus.CANCELLED) -> int:
        cancelled = self.remaining_quantity
        self.cancelled_quantity += cancelled
        self.remaining_quantity = 0
        self.status = status
        return cancelled


@dataclass(slots=True)
class PriceLevel:
    price_ticks: int
    side: Side
    orders: Deque[Order] = field(default_factory=deque)

    def __post_init__(self) -> None:
        _require_positive_ticks(self.price_ticks)

    @property
    def total_quantity(self) -> int:
        return sum(order.remaining_quantity for order in self.orders)

    def add(self, order: Order) -> int:
        if order.price_ticks != self.price_ticks or order.side is not self.side:
            raise ValueError("order does not belong to this price level")
        queue_ahead = self.total_quantity
        self.orders.append(order)
        return queue_ahead

    def remove(self, order_id: str) -> Order | None:
        for order in self.orders:
            if order.order_id == order_id:
                self.orders.remove(order)
                return order
        return None


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    price_ticks: int
    quantity: int
    maker_order_id: str
    taker_order_id: str
    taker_side: Side

    def __post_init__(self) -> None:
        _require_positive_ticks(self.price_ticks)
        _require_integer(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("trade quantity must be positive")


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: str
    order_id: str
    owner: OrderOwner
    side: Side
    price_ticks: int
    quantity: int
    liquidity: str

    def __post_init__(self) -> None:
        _require_positive_ticks(self.price_ticks)
        _require_integer(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.liquidity not in {"maker", "taker"}:
            raise ValueError("fill liquidity must be maker or taker")
