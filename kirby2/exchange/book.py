"""FIFO, price-time-priority order book with replay events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from kirby2.audit.invariants import assert_order_book_invariants
from kirby2.player.position import PlayerPosition
from kirby2.session.events import EventJournal, EventType, SimulationEvent

from .models import (
    Fill,
    Order,
    OrderOwner,
    OrderStatus,
    OrderType,
    OrderView,
    PriceLevel,
    PriceLevelView,
    Side,
    Trade,
)


class OrderBook:
    def __init__(
        self,
        journal: EventJournal | None = None,
        player_position: PlayerPosition | None = None,
    ) -> None:
        self.journal = journal or EventJournal()
        self.player_position = player_position or PlayerPosition()
        self._bids: dict[int, PriceLevel] = {}
        self._asks: dict[int, PriceLevel] = {}
        self._bid_prices: list[int] = []
        self._ask_prices: list[int] = []
        self._active_orders: dict[str, Order] = {}
        self._all_orders: dict[str, Order] = {}
        self._seen_order_ids: set[str] = set()
        self._resting_sequence = 0
        self._trades: list[Trade] = []
        self._fills: list[Fill] = []

    @property
    def bids(self) -> Mapping[int, PriceLevelView]:
        return MappingProxyType(
            {
                price: PriceLevelView._from_level(level)
                for price, level in self._bids.items()
            }
        )

    @property
    def asks(self) -> Mapping[int, PriceLevelView]:
        return MappingProxyType(
            {
                price: PriceLevelView._from_level(level)
                for price, level in self._asks.items()
            }
        )

    @property
    def bid_prices(self) -> list[int]:
        return list(self._bid_prices)

    @property
    def ask_prices(self) -> list[int]:
        return list(self._ask_prices)

    @property
    def active_orders(self) -> Mapping[str, OrderView]:
        return MappingProxyType(
            {
                order_id: OrderView._from_order(order)
                for order_id, order in self._active_orders.items()
            }
        )

    @property
    def all_orders(self) -> Mapping[str, OrderView]:
        return MappingProxyType(
            {
                order_id: OrderView._from_order(order)
                for order_id, order in self._all_orders.items()
            }
        )

    @property
    def trades(self) -> tuple[Trade, ...]:
        return tuple(self._trades)

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def best_bid(self) -> int | None:
        return self._bid_prices[0] if self._bid_prices else None

    @property
    def best_ask(self) -> int | None:
        return self._ask_prices[0] if self._ask_prices else None

    def process(self, order: Order) -> tuple[SimulationEvent, ...]:
        owned_order = self._clone_pristine_order(order)
        return self._process_owned(owned_order)

    def _process_owned(self, order: Order) -> tuple[SimulationEvent, ...]:
        event_start = len(self.journal.events)
        self._register(order)
        self._emit_submitted(order)

        if order.order_type is OrderType.CANCEL:
            self._apply_cancel(order)
        else:
            self._match(order)
            if order.remaining_quantity > 0 and order.order_type is OrderType.LIMIT:
                self._rest(order)
            elif order.remaining_quantity > 0:
                cancelled = order.cancel_remainder(OrderStatus.EXPIRED)
                self.journal.emit(
                    EventType.ORDER_EXPIRED,
                    order_id=order.order_id,
                    unfilled_quantity=cancelled,
                )

        assert_order_book_invariants(self)
        return self.journal.events[event_start:]

    def cancel(self, target_order_id: str, command_id: str) -> tuple[SimulationEvent, ...]:
        return self.process(Order.cancel(command_id, target_order_id))

    def replace(
        self,
        target_order_id: str,
        replacement: Order,
        command_id: str,
    ) -> tuple[SimulationEvent, ...]:
        owned_replacement = self._clone_pristine_order(replacement)
        if owned_replacement.order_type is not OrderType.LIMIT:
            raise ValueError("replacement must be a new limit order")
        if owned_replacement.order_id in self._seen_order_ids:
            raise ValueError(f"duplicate order ID: {owned_replacement.order_id}")
        event_start = len(self.journal.events)
        target_was_active = target_order_id in self._active_orders
        self.cancel(target_order_id, command_id)
        if target_was_active:
            self._process_owned(owned_replacement)
            self.journal.emit(
                EventType.ORDER_REPLACED,
                new_order_id=owned_replacement.order_id,
                old_order_id=target_order_id,
            )
            assert_order_book_invariants(self)
        return self.journal.events[event_start:]

    def reduce_order(
        self,
        target_order_id: str,
        new_total_quantity: int,
        command_id: str,
    ) -> tuple[SimulationEvent, ...]:
        """Reduce live quantity in place while preserving FIFO position.

        The submitted quantity remains the conservation basis; the reduction is
        recorded as cancelled quantity. Increasing quantity or reducing to the
        already-filled amount is not an in-place operation.
        """

        target = self._active_orders.get(target_order_id)
        if target is None:
            raise ValueError(f"order is not active: {target_order_id}")
        if type(new_total_quantity) is not int:
            raise TypeError("replacement quantity must be an integer")
        current_total = target.filled_quantity + target.remaining_quantity
        if not target.filled_quantity < new_total_quantity < current_total:
            raise ValueError(
                "priority-preserving reduction must remain above filled quantity"
            )
        event_start = len(self.journal.events)
        command = Order.cancel(command_id, target_order_id)
        self._register(command)
        self._emit_submitted(command)
        reduction = current_total - new_total_quantity
        target.remaining_quantity -= reduction
        target.cancelled_quantity += reduction
        command.status = OrderStatus.APPLIED
        self.journal.emit(
            EventType.ORDER_REDUCED,
            cancelled_quantity=reduction,
            command_id=command_id,
            new_total_quantity=new_total_quantity,
            order_id=target_order_id,
            priority_preserved=True,
            remaining_quantity=target.remaining_quantity,
            resting_sequence=target.resting_sequence,
        )
        assert_order_book_invariants(self)
        return self.journal.events[event_start:]

    def assert_invariants(self) -> None:
        assert_order_book_invariants(self)

    def snapshot(self) -> dict[str, object]:
        return {
            "asks": self._levels_snapshot(self._asks, self._ask_prices),
            "bids": self._levels_snapshot(self._bids, self._bid_prices),
            "player": self.player_position.snapshot(),
        }

    def state_sha256(self) -> str:
        payload = self.runtime_state()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def runtime_state(self) -> dict[str, object]:
        return {
            "all_orders": [
                {
                    "cancel_target_id": order.cancel_target_id,
                    "cancelled_quantity": order.cancelled_quantity,
                    "filled_quantity": order.filled_quantity,
                    "order_id": order.order_id,
                    "order_type": order.order_type.value,
                    "original_quantity": order.original_quantity,
                    "owner": order.owner.value,
                    "price_ticks": order.price_ticks,
                    "remaining_quantity": order.remaining_quantity,
                    "resting_sequence": order.resting_sequence,
                    "side": None if order.side is None else order.side.value,
                    "status": order.status.value,
                }
                for _, order in sorted(self._all_orders.items())
            ],
            "events": [event.as_dict() for event in self.journal.events],
            "fills": [
                {
                    "liquidity": fill.liquidity,
                    "order_id": fill.order_id,
                    "owner": fill.owner.value,
                    "price_ticks": fill.price_ticks,
                    "quantity": fill.quantity,
                    "side": fill.side.value,
                    "trade_id": fill.trade_id,
                }
                for fill in self._fills
            ],
            "resting_sequence": self._resting_sequence,
            "seen_order_ids": sorted(self._seen_order_ids),
            "snapshot": self.snapshot(),
            "trades": [
                {
                    "maker_order_id": trade.maker_order_id,
                    "price_ticks": trade.price_ticks,
                    "quantity": trade.quantity,
                    "taker_order_id": trade.taker_order_id,
                    "taker_side": trade.taker_side.value,
                    "trade_id": trade.trade_id,
                }
                for trade in self._trades
            ],
        }

    def _levels_snapshot(
        self,
        levels: dict[int, PriceLevel],
        prices: Iterable[int],
    ) -> list[dict[str, object]]:
        return [
            {
                "orders": [
                    {
                        "order_id": order.order_id,
                        "owner": order.owner.value,
                        "remaining_quantity": order.remaining_quantity,
                    }
                    for order in levels[price].orders
                ],
                "price_ticks": price,
                "total_quantity": levels[price].total_quantity,
            }
            for price in prices
        ]

    @staticmethod
    def _clone_pristine_order(order: Order) -> Order:
        if not isinstance(order, Order):
            raise TypeError("exchange commands must use Order")
        pristine = all(
            (
                type(order.remaining_quantity) is int,
                order.remaining_quantity == order.original_quantity,
                type(order.filled_quantity) is int,
                order.filled_quantity == 0,
                type(order.cancelled_quantity) is int,
                order.cancelled_quantity == 0,
                order.resting_sequence is None,
                order.status is OrderStatus.NEW,
            )
        )
        if not pristine:
            raise ValueError("exchange commands must carry pristine lifecycle state")
        return Order(
            order_id=order.order_id,
            order_type=order.order_type,
            original_quantity=order.original_quantity,
            side=order.side,
            price_ticks=order.price_ticks,
            owner=order.owner,
            cancel_target_id=order.cancel_target_id,
        )

    def _register(self, order: Order) -> None:
        if order.order_id in self._seen_order_ids:
            raise ValueError(f"duplicate order ID: {order.order_id}")
        self._seen_order_ids.add(order.order_id)
        self._all_orders[order.order_id] = order

    def _emit_submitted(self, order: Order) -> None:
        data: dict[str, object] = {
            "order_id": order.order_id,
            "order_type": order.order_type.value,
            "owner": order.owner.value,
            "original_quantity": order.original_quantity,
        }
        if order.side is not None:
            data["side"] = order.side.value
        if order.price_ticks is not None:
            data["price_ticks"] = order.price_ticks
        if order.cancel_target_id is not None:
            data["cancel_target_id"] = order.cancel_target_id
        self.journal.emit(EventType.ORDER_SUBMITTED, **data)

    def _apply_cancel(self, command: Order) -> None:
        target_id = command.cancel_target_id
        if target_id is None:
            raise ValueError("cancel command missing target")
        target = self._active_orders.get(target_id)
        if target is None:
            command.status = OrderStatus.APPLIED
            self.journal.emit(
                EventType.CANCEL_REJECTED,
                command_id=command.order_id,
                reason="order_not_active",
                target_order_id=target_id,
            )
            return

        previous_bid, previous_ask = self.best_bid, self.best_ask
        level = self._levels_for(target.side)[target.price_ticks]  # type: ignore[index]
        removed = level.remove(target_id)
        if removed is not target:
            raise RuntimeError("active order index and price level disagree")
        cancelled = target.cancel_remainder()
        del self._active_orders[target_id]
        if not level.orders:
            self._remove_level(target.side, target.price_ticks)  # type: ignore[arg-type]
        command.status = OrderStatus.APPLIED
        self.journal.emit(
            EventType.ORDER_CANCELLED,
            cancelled_quantity=cancelled,
            command_id=command.order_id,
            order_id=target_id,
            price_ticks=target.price_ticks,
            side=target.side.value,  # type: ignore[union-attr]
        )
        self._emit_top_changes(previous_bid, previous_ask)

    def _match(self, incoming: Order) -> None:
        while incoming.remaining_quantity > 0:
            best_opposite = self.best_ask if incoming.side is Side.BUY else self.best_bid
            if best_opposite is None or not self._is_marketable(incoming, best_opposite):
                return

            opposite_side = Side.SELL if incoming.side is Side.BUY else Side.BUY
            level = self._levels_for(opposite_side)[best_opposite]
            maker = level.orders[0]
            quantity = min(incoming.remaining_quantity, maker.remaining_quantity)
            previous_bid, previous_ask = self.best_bid, self.best_ask

            maker.apply_fill(quantity)
            incoming.apply_fill(quantity)
            trade = Trade(
                trade_id=f"T{len(self._trades) + 1:06d}",
                price_ticks=maker.price_ticks,  # type: ignore[arg-type]
                quantity=quantity,
                maker_order_id=maker.order_id,
                taker_order_id=incoming.order_id,
                taker_side=incoming.side,  # type: ignore[arg-type]
            )
            self._trades.append(trade)
            self.journal.emit(
                EventType.TRADE,
                maker_order_id=trade.maker_order_id,
                price_ticks=trade.price_ticks,
                quantity=trade.quantity,
                taker_order_id=trade.taker_order_id,
                taker_side=trade.taker_side.value,
                trade_id=trade.trade_id,
            )

            self._record_fill(maker, trade, "maker")
            self._record_fill(incoming, trade, "taker")

            if maker.remaining_quantity == 0:
                popped = level.orders.popleft()
                if popped is not maker:
                    raise RuntimeError("FIFO queue head changed during match")
                del self._active_orders[maker.order_id]
                if not level.orders:
                    self._remove_level(opposite_side, best_opposite)
                self._emit_top_changes(previous_bid, previous_ask)

    def _record_fill(self, order: Order, trade: Trade, liquidity: str) -> None:
        fill = Fill(
            trade_id=trade.trade_id,
            order_id=order.order_id,
            owner=order.owner,
            side=order.side,  # type: ignore[arg-type]
            price_ticks=trade.price_ticks,
            quantity=trade.quantity,
            liquidity=liquidity,
        )
        self._fills.append(fill)
        event_type = (
            EventType.FULL_FILL
            if order.remaining_quantity == 0
            else EventType.PARTIAL_FILL
        )
        self.journal.emit(
            event_type,
            fill_quantity=fill.quantity,
            filled_quantity=order.filled_quantity,
            liquidity=fill.liquidity,
            order_id=fill.order_id,
            price_ticks=fill.price_ticks,
            remaining_quantity=order.remaining_quantity,
            trade_id=fill.trade_id,
        )
        if order.owner is OrderOwner.PLAYER:
            self.player_position.apply(fill)
            self.journal.emit(
                EventType.PLAYER_POSITION_CHANGED,
                bought_quantity=self.player_position.bought_quantity,
                fill_quantity=fill.quantity,
                fill_side=fill.side.value,
                order_id=fill.order_id,
                position=self.player_position.position,
                sold_quantity=self.player_position.sold_quantity,
                trade_id=fill.trade_id,
            )

    def _rest(self, order: Order) -> None:
        if order.side is None or order.price_ticks is None:
            raise ValueError("only priced trading orders can rest")
        previous_bid, previous_ask = self.best_bid, self.best_ask
        levels = self._levels_for(order.side)
        level = levels.get(order.price_ticks)
        if level is None:
            level = PriceLevel(order.price_ticks, order.side)
            levels[order.price_ticks] = level
            prices = self._prices_for(order.side)
            prices.append(order.price_ticks)
            prices.sort(reverse=order.side is Side.BUY)

        self._resting_sequence += 1
        order.resting_sequence = self._resting_sequence
        if order.filled_quantity == 0:
            order.status = OrderStatus.ACTIVE
        queue_ahead = level.add(order)
        self._active_orders[order.order_id] = order
        self.journal.emit(
            EventType.ORDER_ADDED,
            order_id=order.order_id,
            owner=order.owner.value,
            price_ticks=order.price_ticks,
            queue_ahead_quantity=queue_ahead,
            remaining_quantity=order.remaining_quantity,
            resting_sequence=order.resting_sequence,
            side=order.side.value,
        )
        self._emit_top_changes(previous_bid, previous_ask)

    def _is_marketable(self, order: Order, opposite_price: int) -> bool:
        if order.order_type is OrderType.MARKET:
            return True
        if order.side is Side.BUY:
            return order.price_ticks >= opposite_price  # type: ignore[operator]
        return order.price_ticks <= opposite_price  # type: ignore[operator]

    def _levels_for(self, side: Side | None) -> dict[int, PriceLevel]:
        if side is Side.BUY:
            return self._bids
        if side is Side.SELL:
            return self._asks
        raise ValueError("trading side is required")

    def _prices_for(self, side: Side) -> list[int]:
        return self._bid_prices if side is Side.BUY else self._ask_prices

    def _remove_level(self, side: Side, price_ticks: int) -> None:
        del self._levels_for(side)[price_ticks]
        self._prices_for(side).remove(price_ticks)

    def _emit_top_changes(self, previous_bid: int | None, previous_ask: int | None) -> None:
        if previous_bid != self.best_bid:
            self.journal.emit(
                EventType.BEST_BID_CHANGED,
                new_price_ticks=self.best_bid,
                previous_price_ticks=previous_bid,
            )
        if previous_ask != self.best_ask:
            self.journal.emit(
                EventType.BEST_ASK_CHANGED,
                new_price_ticks=self.best_ask,
                previous_price_ticks=previous_ask,
            )
