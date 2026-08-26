"""Work Order 01 deterministic exchange demonstration."""

from __future__ import annotations

import json

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderStatus, Side
from kirby2.session import EventType
from kirby2.simulation import SeededRng


class DemoFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def run_demo(seed: int) -> str:
    rng = SeededRng(seed)
    book = OrderBook()

    # The first two bids establish exactly 1,100 shares ahead of the player.
    initial_orders = (
        Order.limit("SIM-B1", Side.BUY, 600, 2431),
        Order.limit("SIM-B2", Side.BUY, 500, 2431),
        Order.limit("SIM-B3", Side.BUY, 900, 2430),
        Order.limit("SIM-A1", Side.SELL, 300, 2432),
        Order.limit("SIM-A2", Side.SELL, 700, 2433),
        Order.limit("SIM-A3", Side.SELL, 800, 2434),
    )
    for order in initial_orders:
        book.process(order)

    player_order = Order.limit(
        "PLAYER-B1",
        Side.BUY,
        500,
        2431,
        owner=OrderOwner.PLAYER,
    )
    book.process(player_order)

    # Seeded sizes are bounded so every seed preserves the demonstration shape.
    ask_sweep_quantity = 300 + rng.integer(50, 150)
    first_player_fill_quantity = rng.integer(100, 300)
    first_sell_quantity = 1_100 + first_player_fill_quantity
    final_player_fill_quantity = 500 - first_player_fill_quantity

    book.process(Order.market("SIM-MB1", Side.BUY, ask_sweep_quantity))
    book.cancel("SIM-A2", "CANCEL-A2")
    book.process(Order.market("SIM-MS1", Side.SELL, first_sell_quantity))
    book.process(Order.market("SIM-MS2", Side.SELL, final_player_fill_quantity))
    book.assert_invariants()

    _accept_demo(
        book,
        player_order,
        first_player_fill_quantity,
        final_player_fill_quantity,
    )

    simulated_filled_ahead = sum(
        fill.quantity
        for fill in book.fills
        if fill.order_id in {"SIM-B1", "SIM-B2"}
    )
    facts = {
        "cancelled_order_id": "SIM-A2",
        "final_best_ask_ticks": book.best_ask,
        "final_best_bid_ticks": book.best_bid,
        "first_player_fill_quantity": first_player_fill_quantity,
        "player_queue_ahead_at_entry": 1_100,
        "second_player_fill_quantity": final_player_fill_quantity,
        "seed": seed,
        "simulated_quantity_filled_before_player": simulated_filled_ahead,
        "trade_count": len(book.trades),
    }
    lines = [
        f"KIRBY2_DEMO seed={seed}",
        "EVENT_STREAM",
        book.journal.canonical_json_lines(),
        "DEMO_FACTS",
        json.dumps(facts, sort_keys=True, separators=(",", ":")),
        "FINAL_BOOK",
        json.dumps(book.snapshot(), sort_keys=True, separators=(",", ":")),
        "RUNTIME_INVARIANTS PASS",
    ]
    return "\n".join(lines)


def _accept_demo(
    book: OrderBook,
    player_order: Order,
    first_player_fill_quantity: int,
    final_player_fill_quantity: int,
) -> None:
    _require(player_order.status is OrderStatus.FILLED, "player order did not fill completely")
    _require(player_order.filled_quantity == 500, "player filled quantity is wrong")
    _require(book.player_position.position == 500, "player position did not follow fills")
    _require(book.all_orders["SIM-A2"].status is OrderStatus.CANCELLED, "cancel did not apply")

    player_fills = [fill for fill in book.fills if fill.order_id == player_order.order_id]
    _require(
        [fill.quantity for fill in player_fills]
        == [first_player_fill_quantity, final_player_fill_quantity],
        "player partial/full fill sequence is wrong",
    )
    filled_before_player = sum(
        fill.quantity
        for fill in book.fills
        if fill.order_id in {"SIM-B1", "SIM-B2"}
    )
    _require(filled_before_player == 1_100, "player queue position was bypassed")
    _require(
        any(event.event_type is EventType.PARTIAL_FILL for event in book.journal.events),
        "demo did not produce a partial fill event",
    )
    _require(
        any(event.event_type is EventType.FULL_FILL for event in book.journal.events),
        "demo did not produce a full fill event",
    )
    _require(
        any(event.event_type is EventType.ORDER_CANCELLED for event in book.journal.events),
        "demo did not produce a cancellation event",
    )
    _require(book.best_bid == 2430 and book.best_ask == 2434, "spread did not move as expected")

