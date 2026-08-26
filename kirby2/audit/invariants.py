"""Runtime invariants executed after every public exchange command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kirby2.exchange.models import OrderOwner, OrderStatus, Side

if TYPE_CHECKING:
    from kirby2.exchange.book import OrderBook


class InvariantViolation(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvariantViolation(message)


def assert_order_book_invariants(book: OrderBook) -> None:
    best_bid = book.best_bid
    best_ask = book.best_ask
    _require(
        best_bid is None or best_ask is None or best_bid < best_ask,
        "best bid must remain below best ask",
    )

    _require(book.bid_prices == sorted(book.bid_prices, reverse=True), "bid prices out of order")
    _require(book.ask_prices == sorted(book.ask_prices), "ask prices out of order")
    _require(len(book.bid_prices) == len(set(book.bid_prices)), "duplicate bid price level")
    _require(len(book.ask_prices) == len(set(book.ask_prices)), "duplicate ask price level")
    _require(set(book.bid_prices) == set(book.bids), "bid price index does not match levels")
    _require(set(book.ask_prices) == set(book.asks), "ask price index does not match levels")

    queued_ids: list[str] = []
    for side, levels, prices in (
        (Side.BUY, book.bids, book.bid_prices),
        (Side.SELL, book.asks, book.ask_prices),
    ):
        for price in prices:
            level = levels[price]
            _require(level.side is side, "price level stored on wrong side")
            _require(level.price_ticks == price, "price level key mismatch")
            _require(bool(level.orders), "empty price level must not remain indexed")
            sequences: list[int] = []
            for order in level.orders:
                queued_ids.append(order.order_id)
                _require(order.remaining_quantity > 0, "active order quantity must be positive")
                _require(order.status in {OrderStatus.ACTIVE, OrderStatus.PARTIALLY_FILLED}, "inactive order remains queued")
                _require(order.side is side, "queued order side mismatch")
                _require(order.price_ticks == price, "queued order price mismatch")
                _require(order.resting_sequence is not None, "queued order lacks arrival sequence")
                sequences.append(order.resting_sequence)
            _require(sequences == sorted(sequences), "FIFO queue order was not preserved")
            _require(len(sequences) == len(set(sequences)), "duplicate resting sequence")

    _require(len(queued_ids) == len(set(queued_ids)), "duplicate active order ID")
    _require(set(queued_ids) == set(book.active_orders), "active order index does not match queues")

    for order in book.all_orders.values():
        _require(type(order.original_quantity) is int, "original quantity must be an integer")
        _require(type(order.remaining_quantity) is int, "remaining quantity must be an integer")
        _require(type(order.filled_quantity) is int, "filled quantity must be an integer")
        _require(type(order.cancelled_quantity) is int, "cancelled quantity must be an integer")
        _require(order.original_quantity >= 0, "negative original quantity")
        _require(order.remaining_quantity >= 0, "negative remaining quantity")
        _require(order.filled_quantity >= 0, "negative filled quantity")
        _require(order.cancelled_quantity >= 0, "negative cancelled quantity")
        _require(order.filled_quantity <= order.original_quantity, "filled quantity exceeds original")
        _require(
            order.filled_quantity + order.remaining_quantity + order.cancelled_quantity
            == order.original_quantity,
            "order quantities do not conserve",
        )

    for order_id, order in book.active_orders.items():
        _require(order_id == order.order_id, "active order index key mismatch")
        _require(order.remaining_quantity > 0, "active order quantity must be positive")

    event_sequences = [event.sequence for event in book.journal.events]
    _require(
        event_sequences == list(range(1, len(event_sequences) + 1)),
        "event sequence must be contiguous and monotonic",
    )

    player_fills = [fill for fill in book.fills if fill.owner is OrderOwner.PLAYER]
    expected_bought = sum(fill.quantity for fill in player_fills if fill.side is Side.BUY)
    expected_sold = sum(fill.quantity for fill in player_fills if fill.side is Side.SELL)
    _require(book.player_position.bought_quantity == expected_bought, "player buy ledger mismatch")
    _require(book.player_position.sold_quantity == expected_sold, "player sell ledger mismatch")
    _require(
        book.player_position.position == expected_bought - expected_sold,
        "player position cannot be reconciled to fills",
    )
    _require(book.player_position.fills == player_fills, "player fill ledger mismatch")
