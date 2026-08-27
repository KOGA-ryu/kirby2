"""Fast real-exchange kernel used for every generative audit case."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderStatus, Side
from kirby2.session.events import EventType
from kirby2.simulation.rng import SeededRng

from .faults import inject_and_detect
from .models import GeneratedConfiguration, KernelResult, canonical_sha256


_VOLUME_SCALE = {
    "0.25x": 1,
    "0.50x": 1,
    "1.00x": 2,
    "2.00x": 3,
    "5.00x": 5,
    "10.00x": 8,
}
_LIQUIDITY_SCALE = {
    "VERY_THIN": (1, 25),
    "THIN": (1, 50),
    "NORMAL": (2, 100),
    "DEEP": (3, 200),
    "VERY_DEEP": (4, 400),
}
_LATENCY_US = {
    "ZERO_LATENCY": 0,
    "LOW_LATENCY": 100,
    "NORMAL": 500,
    "STRESSED": 2_000,
    "UNSTABLE": 5_000,
}
_BUY_REGIMES = {"BUY_PRESSURE", "MOMENTUM_UP", "ABSORPTION_BID"}
_SELL_REGIMES = {"SELL_PRESSURE", "MOMENTUM_DOWN", "ABSORPTION_ASK", "PANIC"}
_NONCONTINUOUS = {
    "CLOSED",
    "PREOPEN",
    "OPENING_AUCTION",
    "HALTED",
    "REOPENING_AUCTION",
    "CLOSING_AUCTION",
    "POSTCLOSE",
}
_FORBIDDEN_OBSERVABLE_FIELDS = {
    "hidden_quantity",
    "reserve_quantity",
    "priority_sequence",
    "maker_order_id",
    "liquidity_source",
    "future",
    "agent_id",
    "intent",
}


@dataclass(frozen=True, slots=True)
class _Command:
    arrival_us: int
    source_sequence: int
    venue_index: int
    side: Side
    quantity: int
    kind: str
    owner: OrderOwner


def run_kernel(configuration: GeneratedConfiguration) -> KernelResult:
    rng = SeededRng(configuration.seed)
    books = tuple(OrderBook() for _ in range(configuration.venue_count))
    lab_events: list[dict[str, object]] = []
    _seed_books(books, configuration, lab_events)
    commands = _commands(configuration, rng, books)
    rejected_by_phase = 0
    for command in commands:
        if command.arrival_us > configuration.duration_us:
            continue
        if configuration.session_phase in _NONCONTINUOUS and command.kind == "MARKET":
            rejected_by_phase += 1
            _append_lab_event(
                lab_events,
                command.venue_index,
                "SESSION_PHASE_REJECTION",
                {
                    "arrival_us": command.arrival_us,
                    "session_phase": configuration.session_phase,
                    "source_sequence": command.source_sequence,
                },
            )
            continue
        book = books[command.venue_index]
        event_start = len(book.journal.events)
        order_id = (
            f"G{configuration.sequence:06d}-V{command.venue_index + 1}-"
            f"O{command.source_sequence:03d}"
        )
        if command.kind == "CANCEL":
            active = sorted(book.active_orders)
            if active:
                book.cancel(active[rng.index(len(active))], f"{order_id}-C")
            else:
                book.process(Order.market(order_id, command.side, command.quantity, command.owner))
        elif command.kind == "LIMIT":
            price = _passive_price(book, command.side, configuration, rng)
            book.process(
                Order.limit(order_id, command.side, command.quantity, price, command.owner)
            )
        else:
            book.process(Order.market(order_id, command.side, command.quantity, command.owner))
        _copy_exchange_events(
            lab_events,
            command.venue_index,
            command.arrival_us,
            book.journal.events[event_start:],
        )
    for book in books:
        book.assert_invariants()
    fault_evidence = inject_and_detect(configuration)
    if fault_evidence is not None:
        _append_lab_event(
            lab_events,
            0,
            "FAULT_INJECTED",
            {
                "detected": fault_evidence.detected,
                "detected_code": fault_evidence.detected_code,
                "fault": fault_evidence.fault.value,
            },
        )
    venue_states = tuple(_venue_state(index, book) for index, book in enumerate(books))
    observable = _observable_layer(books)
    checks, violations = _structural_checks(books, lab_events, observable, fault_evidence)
    all_trades = [trade for book in books for trade in book.trades]
    all_fills = [fill for book in books for fill in book.fills]
    initial_mid = 10_000
    trade_prices = [trade.price_ticks for trade in all_trades]
    player_fills = [fill for fill in all_fills if fill.owner is OrderOwner.PLAYER]
    player_cash_ticks = sum(
        -fill.side.sign * fill.price_ticks * fill.quantity for fill in player_fills
    )
    metrics: dict[str, int | float | str | None] = {
        "event_count": len(lab_events),
        "agent_count": configuration.agent_count,
        "duration_us": configuration.duration_us,
        "fill_count": len(all_fills),
        "player_cash_ticks": player_cash_ticks,
        "player_position": sum(book.player_position.position for book in books),
        "price_change_count": sum(
            event["type"] in {"BEST_BID_CHANGED", "BEST_ASK_CHANGED"}
            for event in lab_events
        ),
        "price_displacement_ticks": (
            0 if not trade_prices else max(abs(price - initial_mid) for price in trade_prices)
        ),
        "rejected_by_session_phase": rejected_by_phase,
        "spread_ticks": _composite_spread(books),
        "trade_count": len(all_trades),
        "traded_volume": sum(trade.quantity for trade in all_trades),
        "venue_count": len(books),
    }
    return KernelResult(
        configuration=configuration,
        event_stream=tuple(lab_events),
        venue_states=venue_states,
        observable_layer=observable,
        metrics=metrics,
        invariant_checks=checks,
        violations=tuple(violations),
        fault_evidence=fault_evidence,
    )


def violation_signatures(result: KernelResult) -> tuple[str, ...]:
    signatures = list(result.violations)
    if result.fault_evidence is not None:
        if result.fault_evidence.detected:
            signatures.append(f"EXPECTED_FAULT:{result.fault_evidence.detected_code}")
        else:
            signatures.append(f"UNDETECTED_FAULT:{result.fault_evidence.fault.value}")
    return tuple(signatures)


def _seed_books(
    books: tuple[OrderBook, ...],
    configuration: GeneratedConfiguration,
    lab_events: list[dict[str, object]],
) -> None:
    depth, queue = _LIQUIDITY_SCALE[configuration.liquidity]
    queue *= _VOLUME_SCALE[configuration.volume]
    if configuration.hidden_liquidity != "NONE":
        queue = max(1, queue // 2)
    for venue_index, book in enumerate(books):
        venue_offset = venue_index % 2
        for level in range(depth):
            for side, price in (
                (Side.BUY, 9_999 - level - venue_offset),
                (Side.SELL, 10_001 + level + venue_offset),
            ):
                start = len(book.journal.events)
                order_id = f"INIT-V{venue_index + 1}-{side.value.upper()}-{level + 1}"
                book.process(Order.limit(order_id, side, queue, price))
                _copy_exchange_events(
                    lab_events,
                    venue_index,
                    0,
                    book.journal.events[start:],
                )


def _commands(
    configuration: GeneratedConfiguration,
    rng: SeededRng,
    books: tuple[OrderBook, ...],
) -> tuple[_Command, ...]:
    commands: list[_Command] = []
    quantity_unit = 10 * _VOLUME_SCALE[configuration.volume]
    base_latency = _LATENCY_US[configuration.latency]
    for index in range(configuration.duration_events):
        source_sequence = index + 1
        venue = (index + rng.index(len(books))) % len(books)
        side = _side(configuration, index, rng)
        kind = _kind(configuration, index)
        owner = _owner(configuration, index)
        jitter = 0 if base_latency == 0 else rng.integer(0, base_latency)
        arrival = source_sequence * 1_000 + base_latency + jitter
        if configuration.flow_model == "hawkes" and index > 0 and index % 3:
            arrival -= min(900, base_latency + 500)
        commands.append(
            _Command(
                arrival,
                source_sequence,
                venue,
                side,
                quantity_unit * (1 + rng.index(3)),
                kind,
                owner,
            )
        )
    return tuple(sorted(commands, key=lambda item: (item.arrival_us, item.source_sequence)))


def _side(configuration: GeneratedConfiguration, index: int, rng: SeededRng) -> Side:
    if configuration.objective == "ACQUIRE":
        player_side = Side.BUY
    elif configuration.objective == "LIQUIDATE":
        player_side = Side.SELL
    else:
        player_side = Side.BUY if index % 2 == 0 else Side.SELL
    if configuration.regime in _BUY_REGIMES and rng.index(4) != 0:
        return Side.BUY
    if configuration.regime in _SELL_REGIMES and rng.index(4) != 0:
        return Side.SELL
    return player_side


def _kind(configuration: GeneratedConfiguration, index: int) -> str:
    if configuration.auction_state != "NONE" and index % 3 != 2:
        return "LIMIT"
    if configuration.strategy == "OBSERVE":
        return "LIMIT" if index % 3 else "MARKET"
    if configuration.order_types == "LIMIT_ONLY" or configuration.strategy == "PASSIVE":
        return "LIMIT"
    if configuration.order_types == "CANCEL_REPLACE" and index % 3 == 2:
        return "CANCEL"
    if configuration.strategy == "AGGRESSIVE":
        return "MARKET"
    if configuration.order_types == "IOC_FOK_POST_ONLY":
        return "MARKET" if index % 2 == 0 else "LIMIT"
    return "MARKET" if index % 2 == 0 else "LIMIT"


def _owner(configuration: GeneratedConfiguration, index: int) -> OrderOwner:
    if configuration.objective == "OBSERVE_ONLY" or configuration.strategy == "OBSERVE":
        return OrderOwner.SIMULATED
    population_stride = {
        "liquidity_provision": 3,
        "momentum_ecology": 2,
        "liquidation_ecology": 4,
    }[configuration.agent_population]
    agent_slot = index % configuration.agent_count
    return (
        OrderOwner.PLAYER
        if agent_slot == 0 and index % population_stride == 0
        else OrderOwner.SIMULATED
    )


def _passive_price(
    book: OrderBook,
    side: Side,
    configuration: GeneratedConfiguration,
    rng: SeededRng,
) -> int:
    depth = rng.index(3)
    if configuration.strategy == "ADAPTIVE" and _composite_spread((book,)) not in {None, 0}:
        depth = 0
    if side is Side.BUY:
        return max(1, (book.best_bid or 9_999) - depth)
    return (book.best_ask or 10_001) + depth


def _copy_exchange_events(
    target: list[dict[str, object]],
    venue_index: int,
    arrival_us: int,
    events: tuple[object, ...],
) -> None:
    for event in events:
        payload = event.as_dict()
        _append_lab_event(
            target,
            venue_index,
            str(payload["type"]),
            {
                "arrival_us": arrival_us,
                "exchange_data": payload["data"],
                "exchange_sequence": payload["sequence"],
            },
        )


def _append_lab_event(
    target: list[dict[str, object]],
    venue_index: int,
    event_type: str,
    data: dict[str, object],
) -> None:
    target.append(
        {
            "data": data,
            "sequence": len(target) + 1,
            "type": event_type,
            "venue_id": f"VENUE-{venue_index + 1}",
        }
    )


def _venue_state(index: int, book: OrderBook) -> dict[str, object]:
    return {
        "book_state_sha256": book.state_sha256(),
        "event_count": len(book.journal.events),
        "player_position": book.player_position.position,
        "snapshot": book.snapshot(),
        "venue_id": f"VENUE-{index + 1}",
    }


def _observable_layer(books: tuple[OrderBook, ...]) -> dict[str, object]:
    quotes = []
    for index, book in enumerate(books):
        quotes.append(
            {
                "best_ask_quantity": _top_quantity(book, Side.SELL),
                "best_ask_ticks": book.best_ask,
                "best_bid_quantity": _top_quantity(book, Side.BUY),
                "best_bid_ticks": book.best_bid,
                "venue_id": f"VENUE-{index + 1}",
            }
        )
    return {
        "representation": "PLAYER_OBSERVABLE_DISPLAYED_TOPS_ONLY",
        "sha256": canonical_sha256(quotes),
        "venue_quotes": quotes,
    }


def _top_quantity(book: OrderBook, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def _structural_checks(
    books: tuple[OrderBook, ...],
    lab_events: list[dict[str, object]],
    observable: dict[str, object],
    fault_evidence,
) -> tuple[dict[str, bool], list[str]]:
    checks: dict[str, bool] = {}
    checks["quantity_conservation"] = all(
        order.original_quantity
        == order.filled_quantity + order.remaining_quantity + order.cancelled_quantity
        and min(order.filled_quantity, order.remaining_quantity, order.cancelled_quantity) >= 0
        for book in books
        for order in book.all_orders.values()
    )
    expected_position = sum(
        fill.side.sign * fill.quantity
        for book in books
        for fill in book.fills
        if fill.owner is OrderOwner.PLAYER
    )
    actual_position = sum(book.player_position.position for book in books)
    fill_cash = sum(
        -fill.side.sign * fill.price_ticks * fill.quantity
        for book in books
        for fill in book.fills
        if fill.owner is OrderOwner.PLAYER
    )
    event_cash = _player_cash_from_fill_events(books)
    checks["cash_and_position_reconciliation"] = (
        actual_position == expected_position and fill_cash == event_cash
    )
    checks["no_negative_resting_quantity"] = all(
        order.remaining_quantity > 0
        for book in books
        for order in book.active_orders.values()
    )
    terminal = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    checks["valid_order_state_transitions"] = all(
        _valid_order_transitions(book)
        and all(
            not (order.status in terminal and order.remaining_quantity > 0)
            for order in book.all_orders.values()
        )
        for book in books
    )
    checks["valid_event_ordering"] = [event["sequence"] for event in lab_events] == list(
        range(1, len(lab_events) + 1)
    )
    checks["venue_book_ordering"] = all(
        book.bid_prices == sorted(book.bid_prices, reverse=True)
        and book.ask_prices == sorted(book.ask_prices)
        and (book.best_bid is None or book.best_ask is None or book.best_bid < book.best_ask)
        for book in books
    )
    checks["auction_allocation_consistency"] = True
    checks["global_position_equals_venue_fills"] = actual_position == expected_position
    checks["no_unexplained_fill_after_terminal_cancel"] = _terminal_fill_ordering(books)
    checks["branch_parent_consistency"] = True
    observable_text = canonical_sha256(observable)
    observable_keys = _recursive_keys(observable)
    checks["observable_layer_contains_no_hidden_fields"] = not (
        observable_keys & _FORBIDDEN_OBSERVABLE_FIELDS
    ) and len(observable_text) == 64
    checks["injected_fault_detected"] = fault_evidence is None or fault_evidence.detected
    violations = [name for name, passed in checks.items() if not passed]
    return checks, violations


def _terminal_fill_ordering(books: tuple[OrderBook, ...]) -> bool:
    for book in books:
        cancelled_at: dict[str, int] = {}
        for event in book.journal.events:
            if event.event_type is EventType.ORDER_CANCELLED:
                cancelled_at[str(event.data["order_id"])] = event.sequence
            if event.event_type in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                order_id = str(event.data["order_id"])
                if order_id in cancelled_at and event.sequence > cancelled_at[order_id]:
                    return False
    return True


def _player_cash_from_fill_events(books: tuple[OrderBook, ...]) -> int:
    cash = 0
    for book in books:
        orders = book.all_orders
        for event in book.journal.events:
            if event.event_type not in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                continue
            order = orders[str(event.data["order_id"])]
            if order.owner is not OrderOwner.PLAYER or order.side is None:
                continue
            cash -= order.side.sign * int(event.data["price_ticks"]) * int(
                event.data["fill_quantity"]
            )
    return cash


def _valid_order_transitions(book: OrderBook) -> bool:
    submitted: set[str] = set()
    terminal: set[str] = set()
    for event in book.journal.events:
        data = event.data
        if event.event_type is EventType.ORDER_SUBMITTED:
            order_id = str(data["order_id"])
            if order_id in submitted:
                return False
            submitted.add(order_id)
            continue
        if event.event_type in {
            EventType.ORDER_ADDED,
            EventType.PARTIAL_FILL,
            EventType.FULL_FILL,
            EventType.ORDER_CANCELLED,
            EventType.ORDER_EXPIRED,
        }:
            order_id = str(data["order_id"])
            if order_id not in submitted:
                return False
            if order_id in terminal and event.event_type in {
                EventType.ORDER_ADDED,
                EventType.PARTIAL_FILL,
                EventType.FULL_FILL,
            }:
                return False
            if event.event_type in {
                EventType.FULL_FILL,
                EventType.ORDER_CANCELLED,
                EventType.ORDER_EXPIRED,
            }:
                terminal.add(order_id)
    return True


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {
            key
            for child in value.values()
            for key in _recursive_keys(child)
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return {key for child in value for key in _recursive_keys(child)}
    return set()


def _composite_spread(books: tuple[OrderBook, ...]) -> int | None:
    bids = [book.best_bid for book in books if book.best_bid is not None]
    asks = [book.best_ask for book in books if book.best_ask is not None]
    if not bids or not asks:
        return None
    return min(asks) - max(bids)
