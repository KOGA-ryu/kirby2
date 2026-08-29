"""FIFO, price-time-priority order book with replay events."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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


ORDER_BOOK_CHECKPOINT_SCHEMA_VERSION = 1


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str] | frozenset[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    actual = set(payload)
    missing = sorted(set(expected).difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {context} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )


def _validate_strict_json(value: object, active: set[int] | None = None) -> None:
    active = set() if active is None else active
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("checkpoint JSON strings must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("checkpoint JSON strings must be Unicode scalar values")
        return
    if type(value) is float:
        raise TypeError("binary floats are forbidden in checkpoint JSON")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("checkpoint JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_strict_json(key, active)
                _validate_strict_json(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_strict_json(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported checkpoint JSON value: {type(value).__name__}")


def _plain_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, Mapping):
        return {key: _plain_json(value[key]) for key in sorted(value)}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    raise TypeError(f"unsupported checkpoint JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    _validate_strict_json(value)
    return json.dumps(
        _plain_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _parse_canonical_json_object(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError("canonical JSON input must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("canonical JSON must be valid UTF-8") from error

    def reject_float(_value: str) -> object:
        raise TypeError("decimal JSON numbers are forbidden in checkpoint state")

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON numbers are forbidden")

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=exact_object,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("canonical JSON is malformed") from error
    if type(value) is not dict:
        raise TypeError("canonical checkpoint root must be a JSON object")
    _validate_strict_json(value)
    if _canonical_json_bytes(value) != raw:
        raise ValueError("JSON bytes are not in canonical form")
    return value


@dataclass(frozen=True, slots=True)
class _DecodedOrderBookState:
    bids: dict[int, PriceLevel]
    asks: dict[int, PriceLevel]
    bid_prices: list[int]
    ask_prices: list[int]
    active_orders: dict[str, Order]
    all_orders: dict[str, Order]
    seen_order_ids: set[str]
    resting_sequence: int
    trades: list[Trade]
    fills: list[Fill]
    journal: EventJournal
    player_position: PlayerPosition


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

    def process(
        self, order: Order, *, validate: bool = True
    ) -> tuple[SimulationEvent, ...]:
        if type(validate) is not bool:
            raise TypeError("order-book validation mode must be boolean")
        owned_order = self._clone_pristine_order(order)
        return self._process_owned(owned_order, validate=validate)

    def _process_owned(
        self, order: Order, *, validate: bool = True
    ) -> tuple[SimulationEvent, ...]:
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

        if validate:
            assert_order_book_invariants(self)
        return self.journal.events[event_start:]

    def cancel(
        self,
        target_order_id: str,
        command_id: str,
        *,
        validate: bool = True,
    ) -> tuple[SimulationEvent, ...]:
        return self.process(
            Order.cancel(command_id, target_order_id), validate=validate
        )

    def replace(
        self,
        target_order_id: str,
        replacement: Order,
        command_id: str,
        *,
        validate: bool = True,
    ) -> tuple[SimulationEvent, ...]:
        owned_replacement = self._clone_pristine_order(replacement)
        if owned_replacement.order_type is not OrderType.LIMIT:
            raise ValueError("replacement must be a new limit order")
        if owned_replacement.order_id in self._seen_order_ids:
            raise ValueError(f"duplicate order ID: {owned_replacement.order_id}")
        event_start = len(self.journal.events)
        target_was_active = target_order_id in self._active_orders
        self.cancel(target_order_id, command_id, validate=validate)
        if target_was_active:
            self._process_owned(owned_replacement, validate=validate)
            self.journal.emit(
                EventType.ORDER_REPLACED,
                new_order_id=owned_replacement.order_id,
                old_order_id=target_order_id,
            )
            if validate:
                assert_order_book_invariants(self)
        return self.journal.events[event_start:]

    def reduce_order(
        self,
        target_order_id: str,
        new_total_quantity: int,
        command_id: str,
        *,
        validate: bool = True,
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
        if validate:
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

    def checkpoint_state(self) -> dict[str, object]:
        """Return strict V1 state without changing the legacy runtime projection."""

        self.assert_invariants()
        payload = _checkpoint_state_payload(self)
        # Checkpoint production is a validation boundary too.  A latent history
        # corruption must not be made authoritative merely because it serializes.
        _decode_order_book_state(
            payload,
            journal=self.journal,
            player_position=self.player_position,
        )
        return payload

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        journal: EventJournal | None = None,
        player_position: PlayerPosition | None = None,
    ) -> OrderBook:
        """Validate a complete V1 graph before attaching it to a new book."""

        decoded = _decode_order_book_state(
            payload,
            journal=journal,
            player_position=player_position,
        )
        restored = cls(
            journal=decoded.journal,
            player_position=decoded.player_position,
        )
        restored._bids = decoded.bids
        restored._asks = decoded.asks
        restored._bid_prices = decoded.bid_prices
        restored._ask_prices = decoded.ask_prices
        restored._active_orders = decoded.active_orders
        restored._all_orders = decoded.all_orders
        restored._seen_order_ids = decoded.seen_order_ids
        restored._resting_sequence = decoded.resting_sequence
        restored._trades = decoded.trades
        restored._fills = decoded.fills
        restored.assert_invariants()
        return restored

    @classmethod
    def from_canonical_state_bytes(
        cls,
        raw: bytes,
        *,
        journal: EventJournal | None = None,
        player_position: PlayerPosition | None = None,
    ) -> OrderBook:
        return cls.from_checkpoint_state(
            _parse_canonical_json_object(raw),
            journal=journal,
            player_position=player_position,
        )

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


def _checkpoint_order(order: Order) -> dict[str, object]:
    return {
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


def _checkpoint_levels(
    levels: Mapping[int, PriceLevel],
    prices: Iterable[int],
) -> list[dict[str, object]]:
    return [
        {
            "order_ids": [order.order_id for order in levels[price].orders],
            "price_ticks": price,
        }
        for price in prices
    ]


def _checkpoint_trade(trade: Trade) -> dict[str, object]:
    return {
        "maker_order_id": trade.maker_order_id,
        "price_ticks": trade.price_ticks,
        "quantity": trade.quantity,
        "taker_order_id": trade.taker_order_id,
        "taker_side": trade.taker_side.value,
        "trade_id": trade.trade_id,
    }


def _checkpoint_fill(fill: Fill) -> dict[str, object]:
    return {
        "liquidity": fill.liquidity,
        "order_id": fill.order_id,
        "owner": fill.owner.value,
        "price_ticks": fill.price_ticks,
        "quantity": fill.quantity,
        "side": fill.side.value,
        "trade_id": fill.trade_id,
    }


def _checkpoint_state_payload(book: OrderBook) -> dict[str, object]:
    """Project owned state without recursively invoking checkpoint validation."""

    return {
        "ask_levels": _checkpoint_levels(book._asks, book._ask_prices),
        "bid_levels": _checkpoint_levels(book._bids, book._bid_prices),
        "fills": [_checkpoint_fill(fill) for fill in book._fills],
        "journal": book.journal.checkpoint_state(),
        "order_count": len(book._all_orders),
        "orders": [
            _checkpoint_order(order)
            for _order_id, order in sorted(book._all_orders.items())
        ],
        "player_position": book.player_position.checkpoint_state(),
        "resting_sequence": book._resting_sequence,
        "schema_version": ORDER_BOOK_CHECKPOINT_SCHEMA_VERSION,
        "seen_order_ids": sorted(book._seen_order_ids),
        "trade_sequence": len(book._trades),
        "trades": [_checkpoint_trade(trade) for trade in book._trades],
    }


def _wire_object(payload: Mapping[str, object], field: str) -> Mapping[str, object]:
    value = payload[field]
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {field} must be an object")
    return value


def _wire_array(payload: Mapping[str, object], field: str) -> list[object]:
    value = payload[field]
    if type(value) is not list:
        raise TypeError(f"serialized {field} must be an array")
    return value


def _wire_int(
    payload: Mapping[str, object],
    field: str,
    *,
    minimum: int = 0,
) -> int:
    value = payload[field]
    if type(value) is not int or value < minimum:
        raise ValueError(f"serialized {field} must be an integer >= {minimum}")
    return value


def _wire_optional_int(payload: Mapping[str, object], field: str) -> int | None:
    value = payload[field]
    if value is not None and type(value) is not int:
        raise TypeError(f"serialized {field} must be an integer or null")
    return value


def _wire_string(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if type(value) is not str:
        raise TypeError(f"serialized {field} must be a string")
    return value


def _decode_order(row: object, index: int) -> Order:
    if not isinstance(row, Mapping):
        raise TypeError(f"serialized orders[{index}] must be an object")
    _require_exact_fields(
        row,
        {
            "cancel_target_id",
            "cancelled_quantity",
            "filled_quantity",
            "order_id",
            "order_type",
            "original_quantity",
            "owner",
            "price_ticks",
            "remaining_quantity",
            "resting_sequence",
            "side",
            "status",
        },
        f"OrderBook.orders[{index}]",
    )
    order_id = _wire_string(row, "order_id")
    order_type = OrderType(_wire_string(row, "order_type"))
    original_quantity = _wire_int(row, "original_quantity")
    owner = OrderOwner(_wire_string(row, "owner"))
    raw_side = row["side"]
    if raw_side is not None and type(raw_side) is not str:
        raise TypeError("serialized order side must be a string or null")
    side = None if raw_side is None else Side(raw_side)
    price_ticks = _wire_optional_int(row, "price_ticks")
    raw_target = row["cancel_target_id"]
    if raw_target is not None and type(raw_target) is not str:
        raise TypeError("serialized cancel_target_id must be a string or null")
    order = Order(
        order_id=order_id,
        order_type=order_type,
        original_quantity=original_quantity,
        side=side,
        price_ticks=price_ticks,
        owner=owner,
        cancel_target_id=raw_target,
    )
    remaining = _wire_int(row, "remaining_quantity")
    filled = _wire_int(row, "filled_quantity")
    cancelled = _wire_int(row, "cancelled_quantity")
    resting = _wire_optional_int(row, "resting_sequence")
    if resting is not None and resting < 1:
        raise ValueError("serialized resting_sequence must be positive or null")
    status = OrderStatus(_wire_string(row, "status"))
    if remaining + filled + cancelled != original_quantity:
        raise ValueError("serialized order quantities do not conserve")
    if filled > original_quantity:
        raise ValueError("serialized filled quantity exceeds original quantity")
    if order_type is OrderType.CANCEL:
        if status is not OrderStatus.APPLIED or resting is not None:
            raise ValueError("serialized cancel command lifecycle is invalid")
    else:
        if status in {OrderStatus.NEW, OrderStatus.APPLIED}:
            raise ValueError("serialized trading order has nonquiescent status")
        if remaining > 0:
            if (
                order_type is not OrderType.LIMIT
                or resting is None
                or status not in {OrderStatus.ACTIVE, OrderStatus.PARTIALLY_FILLED}
            ):
                raise ValueError("serialized active order lifecycle is invalid")
        elif status in {OrderStatus.ACTIVE, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("serialized inactive order carries an active status")
        if status is OrderStatus.ACTIVE and filled != 0:
            raise ValueError("serialized ACTIVE order cannot have fills")
        if status is OrderStatus.PARTIALLY_FILLED and filled <= 0:
            raise ValueError("serialized PARTIALLY_FILLED order requires fills")
        if status is OrderStatus.FILLED and filled <= 0:
            raise ValueError("serialized FILLED order requires filled quantity")
        if status in {OrderStatus.CANCELLED, OrderStatus.EXPIRED} and cancelled <= 0:
            raise ValueError("serialized closed order requires cancelled quantity")
        if resting is not None and order_type is not OrderType.LIMIT:
            raise ValueError("only limit orders can carry resting_sequence")
    order.remaining_quantity = remaining
    order.filled_quantity = filled
    order.cancelled_quantity = cancelled
    order.resting_sequence = resting
    order.status = status
    if _checkpoint_order(order) != dict(row):
        raise ValueError("serialized order did not round-trip exactly")
    return order


def _decode_levels(
    raw: object,
    *,
    side: Side,
    orders: Mapping[str, Order],
) -> tuple[dict[int, PriceLevel], list[int], list[str]]:
    if type(raw) is not list:
        raise TypeError("serialized price levels must be an array")
    levels: dict[int, PriceLevel] = {}
    prices: list[int] = []
    queued_ids: list[str] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise TypeError(f"serialized price level {index} must be an object")
        _require_exact_fields(
            row,
            {"order_ids", "price_ticks"},
            f"OrderBook.{side.value}_levels[{index}]",
        )
        price = _wire_int(row, "price_ticks", minimum=1)
        if price in levels:
            raise ValueError("serialized price levels contain a duplicate price")
        raw_ids = _wire_array(row, "order_ids")
        if not raw_ids:
            raise ValueError("serialized price level cannot be empty")
        level_orders: list[Order] = []
        sequences: list[int] = []
        for order_index, raw_id in enumerate(raw_ids):
            if type(raw_id) is not str or not raw_id:
                raise ValueError(
                    f"serialized order_ids[{order_index}] must be nonempty text"
                )
            try:
                order = orders[raw_id]
            except KeyError as error:
                raise ValueError("serialized price level references unknown order") from error
            if raw_id in queued_ids:
                raise ValueError("serialized active order is queued more than once")
            if (
                order.side is not side
                or order.price_ticks != price
                or order.remaining_quantity <= 0
                or order.status not in {OrderStatus.ACTIVE, OrderStatus.PARTIALLY_FILLED}
                or order.resting_sequence is None
            ):
                raise ValueError("serialized price-level order is inconsistent")
            queued_ids.append(raw_id)
            level_orders.append(order)
            sequences.append(order.resting_sequence)
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("serialized FIFO order is corrupt")
        levels[price] = PriceLevel(price, side, deque(level_orders))
        prices.append(price)
    expected_prices = sorted(prices, reverse=side is Side.BUY)
    if prices != expected_prices:
        raise ValueError("serialized price levels are not canonically ordered")
    return levels, prices, queued_ids


def _decode_trade(row: object, index: int) -> Trade:
    if not isinstance(row, Mapping):
        raise TypeError(f"serialized trades[{index}] must be an object")
    _require_exact_fields(
        row,
        {
            "maker_order_id",
            "price_ticks",
            "quantity",
            "taker_order_id",
            "taker_side",
            "trade_id",
        },
        f"OrderBook.trades[{index}]",
    )
    trade = Trade(
        trade_id=_wire_string(row, "trade_id"),
        price_ticks=_wire_int(row, "price_ticks", minimum=1),
        quantity=_wire_int(row, "quantity", minimum=1),
        maker_order_id=_wire_string(row, "maker_order_id"),
        taker_order_id=_wire_string(row, "taker_order_id"),
        taker_side=Side(_wire_string(row, "taker_side")),
    )
    if trade.trade_id != f"T{index + 1:06d}":
        raise ValueError("serialized trade IDs are not contiguous")
    if _checkpoint_trade(trade) != dict(row):
        raise ValueError("serialized trade did not round-trip exactly")
    return trade


def _decode_fill(row: object, index: int) -> Fill:
    if not isinstance(row, Mapping):
        raise TypeError(f"serialized fills[{index}] must be an object")
    _require_exact_fields(
        row,
        {
            "liquidity",
            "order_id",
            "owner",
            "price_ticks",
            "quantity",
            "side",
            "trade_id",
        },
        f"OrderBook.fills[{index}]",
    )
    fill = Fill(
        trade_id=_wire_string(row, "trade_id"),
        order_id=_wire_string(row, "order_id"),
        owner=OrderOwner(_wire_string(row, "owner")),
        side=Side(_wire_string(row, "side")),
        price_ticks=_wire_int(row, "price_ticks", minimum=1),
        quantity=_wire_int(row, "quantity", minimum=1),
        liquidity=_wire_string(row, "liquidity"),
    )
    if _checkpoint_fill(fill) != dict(row):
        raise ValueError("serialized fill did not round-trip exactly")
    return fill


def _validate_trade_and_fill_history(
    orders: Mapping[str, Order],
    trades: list[Trade],
    fills: list[Fill],
) -> None:
    if len(fills) != 2 * len(trades):
        raise ValueError("serialized fill history must contain two fills per trade")
    filled_by_order = {order_id: 0 for order_id in orders}
    for index, trade in enumerate(trades):
        try:
            maker = orders[trade.maker_order_id]
            taker = orders[trade.taker_order_id]
        except KeyError as error:
            raise ValueError("serialized trade references an unknown order") from error
        if maker is taker:
            raise ValueError("serialized trade cannot match an order with itself")
        if (
            maker.order_type is not OrderType.LIMIT
            or maker.price_ticks != trade.price_ticks
            or maker.side is trade.taker_side
            or taker.side is not trade.taker_side
        ):
            raise ValueError("serialized trade order accounting is inconsistent")
        maker_fill, taker_fill = fills[2 * index : 2 * index + 2]
        if (
            maker_fill.trade_id != trade.trade_id
            or maker_fill.order_id != maker.order_id
            or maker_fill.owner is not maker.owner
            or maker_fill.side is not maker.side
            or maker_fill.price_ticks != trade.price_ticks
            or maker_fill.quantity != trade.quantity
            or maker_fill.liquidity != "maker"
            or taker_fill.trade_id != trade.trade_id
            or taker_fill.order_id != taker.order_id
            or taker_fill.owner is not taker.owner
            or taker_fill.side is not taker.side
            or taker_fill.price_ticks != trade.price_ticks
            or taker_fill.quantity != trade.quantity
            or taker_fill.liquidity != "taker"
        ):
            raise ValueError("serialized fill pair disagrees with trade history")
        filled_by_order[maker.order_id] += trade.quantity
        filled_by_order[taker.order_id] += trade.quantity
    if any(
        order.filled_quantity != filled_by_order[order_id]
        for order_id, order in orders.items()
    ):
        raise ValueError("serialized order fill accounting disagrees with fill history")


def _submitted_order(event: SimulationEvent) -> Order:
    """Rebuild one pristine command from its exact ORDER_SUBMITTED record."""

    data = dict(event.data)
    common = {"order_id", "order_type", "original_quantity", "owner"}
    order_type = OrderType(_wire_string(data, "order_type"))
    if order_type is OrderType.CANCEL:
        _require_exact_fields(
            data,
            common | {"cancel_target_id"},
            "ORDER_SUBMITTED cancel event",
        )
        return Order(
            order_id=_wire_string(data, "order_id"),
            order_type=order_type,
            original_quantity=_wire_int(data, "original_quantity"),
            owner=OrderOwner(_wire_string(data, "owner")),
            cancel_target_id=_wire_string(data, "cancel_target_id"),
        )
    expected = common | {"side"}
    if order_type is OrderType.LIMIT:
        expected.add("price_ticks")
    _require_exact_fields(data, expected, "ORDER_SUBMITTED trading event")
    return Order(
        order_id=_wire_string(data, "order_id"),
        order_type=order_type,
        original_quantity=_wire_int(data, "original_quantity", minimum=1),
        side=Side(_wire_string(data, "side")),
        price_ticks=(
            _wire_int(data, "price_ticks", minimum=1)
            if order_type is OrderType.LIMIT
            else None
        ),
        owner=OrderOwner(_wire_string(data, "owner")),
    )


def _event_rows(events: Iterable[SimulationEvent]) -> list[dict[str, object]]:
    return [event.as_dict() for event in events]


def _replay_journal(journal: EventJournal) -> OrderBook:
    """Re-execute every journal lifecycle transition through a fresh book.

    Public exchange operations are the executable lifecycle specification.  An
    exact event-by-event replay therefore checks submitted fields, cancel
    targets, reductions, FIFO/resting allocation, best-price changes, trades,
    fills, player-position events, and replacement markers in one closed path.
    """

    expected = list(journal.events)
    replayed = OrderBook()
    operation_history: list[tuple[str, str, str | None, bool]] = []
    index = 0
    while index < len(expected):
        event = expected[index]
        if event.event_type is EventType.ORDER_REPLACED:
            data = dict(event.data)
            _require_exact_fields(
                data,
                {"new_order_id", "old_order_id"},
                "ORDER_REPLACED event",
            )
            new_order_id = _wire_string(data, "new_order_id")
            old_order_id = _wire_string(data, "old_order_id")
            if len(operation_history) < 2:
                raise ValueError("ORDER_REPLACED lacks its cancel-and-new lifecycle")
            cancelled, replacement = operation_history[-2:]
            if (
                cancelled[0] != "cancel"
                or cancelled[2] != old_order_id
                or not cancelled[3]
                or replacement[0] != "trading"
                or replacement[1] != new_order_id
            ):
                raise ValueError("ORDER_REPLACED lifecycle linkage is inconsistent")
            replacement_order = replayed._all_orders[new_order_id]
            if replacement_order.order_type is not OrderType.LIMIT:
                raise ValueError("ORDER_REPLACED must introduce a limit order")
            generated = replayed.journal.emit(
                EventType.ORDER_REPLACED,
                new_order_id=new_order_id,
                old_order_id=old_order_id,
            )
            if _canonical_json_bytes(generated.as_dict()) != _canonical_json_bytes(
                event.as_dict()
            ):
                raise ValueError("ORDER_REPLACED event bytes are inconsistent")
            replayed.assert_invariants()
            operation_history.clear()
            index += 1
            continue
        if event.event_type is not EventType.ORDER_SUBMITTED:
            raise ValueError("journal event is outside an exchange command lifecycle")

        command = _submitted_order(event)
        start = len(replayed.journal.events)
        next_event = expected[index + 1] if index + 1 < len(expected) else None
        if (
            command.order_type is OrderType.CANCEL
            and next_event is not None
            and next_event.event_type is EventType.ORDER_REDUCED
        ):
            reduced = dict(next_event.data)
            _require_exact_fields(
                reduced,
                {
                    "cancelled_quantity",
                    "command_id",
                    "new_total_quantity",
                    "order_id",
                    "priority_preserved",
                    "remaining_quantity",
                    "resting_sequence",
                },
                "ORDER_REDUCED event",
            )
            if reduced["priority_preserved"] is not True:
                raise ValueError("ORDER_REDUCED must preserve priority")
            target_id = _wire_string(reduced, "order_id")
            if (
                command.cancel_target_id != target_id
                or _wire_string(reduced, "command_id") != command.order_id
            ):
                raise ValueError("ORDER_REDUCED command linkage is inconsistent")
            replayed.reduce_order(
                target_id,
                _wire_int(reduced, "new_total_quantity", minimum=1),
                command.order_id,
            )
            operation = ("reduce", command.order_id, target_id, True)
        else:
            replayed.process(command)
            succeeded = any(
                generated.event_type is EventType.ORDER_CANCELLED
                for generated in replayed.journal.events[start:]
            )
            operation = (
                "cancel" if command.order_type is OrderType.CANCEL else "trading",
                command.order_id,
                command.cancel_target_id,
                succeeded,
            )
        generated_events = replayed.journal.events[start:]
        actual_events = expected[index : index + len(generated_events)]
        if _canonical_json_bytes(
            _event_rows(generated_events)
        ) != _canonical_json_bytes(_event_rows(actual_events)):
            raise ValueError("journal lifecycle differs from deterministic replay")
        operation_history.append(operation)
        if len(operation_history) > 2:
            operation_history.pop(0)
        index += len(generated_events)
    return replayed


def _decode_order_book_state(
    payload: Mapping[str, object],
    *,
    journal: EventJournal | None,
    player_position: PlayerPosition | None,
) -> _DecodedOrderBookState:
    _validate_strict_json(payload)
    _require_exact_fields(
        payload,
        {
            "ask_levels",
            "bid_levels",
            "fills",
            "journal",
            "order_count",
            "orders",
            "player_position",
            "resting_sequence",
            "schema_version",
            "seen_order_ids",
            "trade_sequence",
            "trades",
        },
        "OrderBookCheckpointV1",
    )
    if _wire_int(payload, "schema_version", minimum=1) != ORDER_BOOK_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("OrderBook checkpoint schema version is unsupported")
    raw_orders = _wire_array(payload, "orders")
    decoded_orders = [_decode_order(row, index) for index, row in enumerate(raw_orders)]
    order_ids = [order.order_id for order in decoded_orders]
    if order_ids != sorted(order_ids) or len(order_ids) != len(set(order_ids)):
        raise ValueError("serialized orders must be sorted by unique order ID")
    if _wire_int(payload, "order_count") != len(decoded_orders):
        raise ValueError("serialized order counter disagrees with order history")
    all_orders = {order.order_id: order for order in decoded_orders}
    raw_seen = _wire_array(payload, "seen_order_ids")
    if any(type(order_id) is not str or not order_id for order_id in raw_seen):
        raise ValueError("serialized seen_order_ids contains an invalid ID")
    seen_order_ids = list(raw_seen)
    if (
        seen_order_ids != sorted(seen_order_ids)
        or len(seen_order_ids) != len(set(seen_order_ids))
        or seen_order_ids != order_ids
    ):
        raise ValueError("historical and seen order IDs do not reconcile")
    bids, bid_prices, bid_ids = _decode_levels(
        payload["bid_levels"],
        side=Side.BUY,
        orders=all_orders,
    )
    asks, ask_prices, ask_ids = _decode_levels(
        payload["ask_levels"],
        side=Side.SELL,
        orders=all_orders,
    )
    queued_ids = bid_ids + ask_ids
    expected_active = {
        order.order_id for order in decoded_orders if order.remaining_quantity > 0
    }
    if set(queued_ids) != expected_active:
        raise ValueError("serialized active-order index does not match FIFO levels")
    if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
        raise ValueError("serialized order book is crossed")
    resting_sequence = _wire_int(payload, "resting_sequence")
    observed_resting = sorted(
        order.resting_sequence
        for order in decoded_orders
        if order.resting_sequence is not None
    )
    if observed_resting != list(range(1, resting_sequence + 1)):
        raise ValueError("serialized resting allocator is rolled back or discontinuous")
    raw_trades = _wire_array(payload, "trades")
    trades = [_decode_trade(row, index) for index, row in enumerate(raw_trades)]
    if _wire_int(payload, "trade_sequence") != len(trades):
        raise ValueError("serialized trade allocator disagrees with trade history")
    raw_fills = _wire_array(payload, "fills")
    fills = [_decode_fill(row, index) for index, row in enumerate(raw_fills)]
    _validate_trade_and_fill_history(all_orders, trades, fills)

    raw_journal = _wire_object(payload, "journal")
    if journal is not None:
        if type(journal) is not EventJournal:
            raise TypeError("journal must be an EventJournal")
        if _canonical_json_bytes(
            journal.checkpoint_state()
        ) != _canonical_json_bytes(raw_journal):
            raise ValueError("injected event journal disagrees with book checkpoint")
    restored_journal = EventJournal.from_checkpoint_state(raw_journal)
    raw_position = _wire_object(payload, "player_position")
    if player_position is not None:
        if type(player_position) is not PlayerPosition:
            raise TypeError("player_position must be a PlayerPosition")
        if _canonical_json_bytes(
            player_position.checkpoint_state()
        ) != _canonical_json_bytes(raw_position):
            raise ValueError("injected player position disagrees with book checkpoint")
    restored_position = PlayerPosition.from_checkpoint_state(raw_position)
    expected_player_fills = [
        fill for fill in fills if fill.owner is OrderOwner.PLAYER
    ]
    expected_bought = sum(
        fill.quantity for fill in expected_player_fills if fill.side is Side.BUY
    )
    expected_sold = sum(
        fill.quantity for fill in expected_player_fills if fill.side is Side.SELL
    )
    if (
        restored_position.fills != expected_player_fills
        or restored_position.bought_quantity != expected_bought
        or restored_position.sold_quantity != expected_sold
        or restored_position.position != expected_bought - expected_sold
    ):
        raise ValueError("player position does not reconcile to ordered book fills")
    replayed = _replay_journal(restored_journal)
    if _canonical_json_bytes(_checkpoint_state_payload(replayed)) != _canonical_json_bytes(
        payload
    ):
        raise ValueError(
            "serialized OrderBook state differs from complete journal lifecycle replay"
        )
    return _DecodedOrderBookState(
        bids=bids,
        asks=asks,
        bid_prices=bid_prices,
        ask_prices=ask_prices,
        active_orders={order_id: all_orders[order_id] for order_id in queued_ids},
        all_orders=all_orders,
        seen_order_ids=set(seen_order_ids),
        resting_sequence=resting_sequence,
        trades=trades,
        fills=fills,
        journal=restored_journal,
        player_position=restored_position,
    )
