"""Deterministic discrete-event communications and exchange lifecycle engine."""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderStatus, OrderType, Side
from kirby2.session.events import EventType, SimulationEvent
from kirby2.simulation.clock import SimulationClock

from .distributions import LatencyComponent, LatencySampler
from .models import (
    AsyncOrder,
    AsyncOrderState,
    DisplayedMarketState,
    LatencyEvent,
    LatencyEventType,
    LatencyMetrics,
)
from .profiles import LatencyProfile


class _MessageKind(str, Enum):
    MARKET_DATA_PUBLISH = "MARKET_DATA_PUBLISH"
    MARKET_DATA_CLIENT = "MARKET_DATA_CLIENT"
    MARKET_DATA_RENDER = "MARKET_DATA_RENDER"
    NEW_CREATED = "NEW_CREATED"
    NEW_SENT = "NEW_SENT"
    NEW_GATEWAY = "NEW_GATEWAY"
    NEW_VENUE = "NEW_VENUE"
    NEW_PROCESS = "NEW_PROCESS"
    NEW_ACK_CLIENT = "NEW_ACK_CLIENT"
    CANCEL_CREATED = "CANCEL_CREATED"
    CANCEL_SENT = "CANCEL_SENT"
    CANCEL_GATEWAY = "CANCEL_GATEWAY"
    CANCEL_VENUE = "CANCEL_VENUE"
    CANCEL_PROCESS = "CANCEL_PROCESS"
    CANCEL_ACK_CLIENT = "CANCEL_ACK_CLIENT"
    FILL_CLIENT = "FILL_CLIENT"
    FILL_UI = "FILL_UI"
    EXTERNAL_MARKET = "EXTERNAL_MARKET"
    EXTERNAL_REPRICE = "EXTERNAL_REPRICE"


_MESSAGE_PRIORITY = {
    _MessageKind.EXTERNAL_MARKET: 0,
    _MessageKind.EXTERNAL_REPRICE: 0,
    _MessageKind.NEW_PROCESS: 10,
    _MessageKind.CANCEL_PROCESS: 10,
    _MessageKind.NEW_CREATED: 20,
    _MessageKind.NEW_SENT: 20,
    _MessageKind.NEW_GATEWAY: 20,
    _MessageKind.NEW_VENUE: 20,
    _MessageKind.CANCEL_CREATED: 20,
    _MessageKind.CANCEL_SENT: 20,
    _MessageKind.CANCEL_GATEWAY: 20,
    _MessageKind.CANCEL_VENUE: 20,
    _MessageKind.NEW_ACK_CLIENT: 25,
    _MessageKind.CANCEL_ACK_CLIENT: 25,
    _MessageKind.MARKET_DATA_PUBLISH: 30,
    _MessageKind.MARKET_DATA_CLIENT: 30,
    _MessageKind.FILL_CLIENT: 30,
    _MessageKind.MARKET_DATA_RENDER: 40,
    _MessageKind.FILL_UI: 40,
}


@dataclass(order=True, slots=True)
class _ScheduledMessage:
    time_us: int
    priority: int
    sequence: int
    kind: _MessageKind = field(compare=False)
    data: dict[str, object] = field(compare=False)


class AsynchronousExecutionSession:
    """One owned clock, RNG, message heap, lifecycle ledger, and FIFO book."""

    def __init__(
        self,
        *,
        seed: int,
        profile: LatencyProfile,
        initial_bid_ticks: int = 99,
        initial_ask_ticks: int = 101,
        initial_queue_quantity: int = 100,
    ) -> None:
        if (
            initial_bid_ticks <= 0
            or initial_ask_ticks <= initial_bid_ticks
            or initial_queue_quantity <= 0
        ):
            raise ValueError("asynchronous initial book configuration is invalid")
        self.seed = seed
        self.profile = profile
        self.initial_bid_ticks = initial_bid_ticks
        self.initial_ask_ticks = initial_ask_ticks
        self.initial_queue_quantity = initial_queue_quantity
        self.clock = SimulationClock()
        self.sampler = LatencySampler(seed)
        self.book = OrderBook()
        self._events: list[LatencyEvent] = []
        self._messages: list[_ScheduledMessage] = []
        self._message_sequence = 0
        self._order_sequence = 0
        self._cancel_sequence = 0
        self._external_sequence = 0
        self._market_state_sequence = 0
        self._orders: dict[str, AsyncOrder] = {}
        self._gateway_held_cancels: dict[str, list[dict[str, object]]] = {}
        self._latest_display: DisplayedMarketState | None = None
        self._client_position = 0
        self._fill_report_count = 0
        self.book.process(
            Order.limit(
                "ASYNC-SIM-BID-1",
                Side.BUY,
                initial_queue_quantity,
                initial_bid_ticks,
            )
        )
        self.book.process(
            Order.limit(
                "ASYNC-SIM-ASK-1",
                Side.SELL,
                initial_queue_quantity,
                initial_ask_ticks,
            )
        )
        self._record_market_change("INITIAL_BOOK")

    @property
    def events(self) -> tuple[LatencyEvent, ...]:
        return tuple(self._events)

    @property
    def orders(self) -> tuple[AsyncOrder, ...]:
        return tuple(self._orders[key] for key in sorted(self._orders))

    @property
    def latest_display(self) -> DisplayedMarketState | None:
        return self._latest_display

    @property
    def player_position(self) -> int:
        return self.book.player_position.position

    @property
    def client_position(self) -> int:
        return self._client_position

    def advance_to(self, target_time_us: int) -> None:
        if type(target_time_us) is not int or target_time_us < self.clock.current_time_us:
            raise ValueError("asynchronous session time cannot move backward")
        while self._messages and self._messages[0].time_us <= target_time_us:
            message = heapq.heappop(self._messages)
            self.clock.advance_to(message.time_us)
            self._dispatch(message)
        self.clock.advance_to(target_time_us)

    def request_limit(
        self,
        side: Side,
        quantity: int,
        price_ticks: int,
        *,
        order_id: str | None = None,
        emit_key: bool = True,
    ) -> str:
        return self._request_order(
            OrderType.LIMIT,
            side,
            quantity,
            price_ticks,
            order_id,
            emit_key,
        )

    def request_market(
        self,
        side: Side,
        quantity: int,
        *,
        order_id: str | None = None,
        emit_key: bool = True,
    ) -> str:
        return self._request_order(
            OrderType.MARKET,
            side,
            quantity,
            None,
            order_id,
            emit_key,
        )

    def request_cancel(
        self,
        target_order_id: str,
        *,
        emit_key: bool = True,
    ) -> str:
        target = self._require_order(target_order_id)
        self._cancel_sequence += 1
        cancel_id = f"ASYNC-CANCEL-{self._cancel_sequence:06d}"
        if emit_key:
            self._emit(
                LatencyEventType.KEY_PRESSED,
                target_order_id,
                action="CANCEL",
                cancel_id=cancel_id,
                displayed_state_id=self._display_state_id(),
            )
        self._schedule_after(
            LatencyComponent.INPUT_PROCESSING,
            _MessageKind.CANCEL_CREATED,
            {"cancel_id": cancel_id, "target_order_id": target.order_id},
            f"cancel:{cancel_id}:create",
        )
        return cancel_id

    def request_replace(
        self,
        target_order_id: str,
        *,
        quantity: int,
        price_ticks: int,
    ) -> str | None:
        target = self._require_order(target_order_id)
        self._emit(
            LatencyEventType.KEY_PRESSED,
            target_order_id,
            action="REPLACE",
            displayed_state_id=self._display_state_id(),
        )
        if target.state in {AsyncOrderState.CREATED, AsyncOrderState.PENDING_NEW}:
            self._emit(
                LatencyEventType.REPLACE_REJECTED_BEFORE_ACK,
                target_order_id,
                reason="original order has not been acknowledged",
            )
            return None
        if target.state.terminal:
            self._emit(
                LatencyEventType.REPLACE_REJECTED_BEFORE_ACK,
                target_order_id,
                reason=f"original order is already {target.state.value}",
            )
            return None
        self.request_cancel(target_order_id, emit_key=False)
        return self.request_limit(
            target.side,
            quantity,
            price_ticks,
            emit_key=False,
        )

    def schedule_aggressive_order(
        self,
        time_us: int,
        side: Side,
        quantity: int,
        *,
        order_id: str,
    ) -> None:
        if time_us < self.clock.current_time_us or quantity <= 0 or not order_id:
            raise ValueError("external aggressive order schedule is invalid")
        self._schedule(
            time_us,
            _MessageKind.EXTERNAL_MARKET,
            {"order_id": order_id, "quantity": quantity, "side": side.value},
        )

    def schedule_liquidity_reprice(
        self,
        time_us: int,
        *,
        target_order_id: str,
        new_order_id: str,
        side: Side,
        quantity: int,
        price_ticks: int,
    ) -> None:
        if (
            time_us < self.clock.current_time_us
            or quantity <= 0
            or price_ticks <= 0
            or not target_order_id
            or not new_order_id
        ):
            raise ValueError("external liquidity reprice schedule is invalid")
        self._schedule(
            time_us,
            _MessageKind.EXTERNAL_REPRICE,
            {
                "new_order_id": new_order_id,
                "price_ticks": price_ticks,
                "quantity": quantity,
                "side": side.value,
                "target_order_id": target_order_id,
            },
        )

    def metrics(self, order_id: str) -> LatencyMetrics:
        order = self._require_order(order_id)
        sent = order.timestamps.get("order_left_client_us")
        acknowledged = order.timestamps.get("venue_acknowledged_us")
        filled = order.timestamps.get("first_fill_occurred_us")
        decision_to_send = None if sent is None else sent - order.intention_time_us
        send_to_ack = (
            None if sent is None or acknowledged is None else acknowledged - sent
        )
        send_to_fill = None if sent is None or filled is None else filled - sent
        touch_reference = (
            order.observed_ask_ticks
            if order.side is Side.BUY
            else order.observed_bid_ticks
        )
        slippage_reference = (
            touch_reference
            if order.order_type is OrderType.MARKET
            else order.price_ticks
        )
        observed_touch = (
            order.observed_ask_ticks
            if order.side is Side.BUY
            else order.observed_bid_ticks
        )
        venue_touch = (
            order.venue_ask_ticks
            if order.side is Side.BUY
            else order.venue_bid_ticks
        )
        stale_execution = filled is not None and observed_touch != venue_touch
        slippage: Decimal | None = None
        if order.filled_quantity and slippage_reference is not None:
            average = Decimal(order.fill_value_ticks) / Decimal(order.filled_quantity)
            slippage = (
                average - Decimal(slippage_reference)
                if order.side is Side.BUY
                else Decimal(slippage_reference) - average
            )
        return LatencyMetrics(
            order_id=order.order_id,
            decision_to_send_latency_us=decision_to_send,
            send_to_ack_latency_us=send_to_ack,
            send_to_fill_latency_us=send_to_fill,
            observed_quote_age_us=(
                order.intention_time_us - order.observed_quote_time_us
            ),
            execution_against_stale_quote=stale_execution,
            cancel_race_outcome=order.cancel_race_outcome,
            latency_induced_slippage_ticks=None
            if slippage is None
            else str(slippage),
            intention_time_us=order.intention_time_us,
            venue_execution_time_us=filled,
        )

    def assert_invariants(self) -> None:
        self.book.assert_invariants()
        sequences = [event.sequence for event in self._events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError("latency event sequence is not contiguous")
        times = [event.simulation_time_us for event in self._events]
        if times != sorted(times):
            raise RuntimeError("latency event times are not monotonic")
        fill_position = sum(
            Side(str(event.data["side"])).sign * int(event.data["quantity"])
            for event in self._events
            if event.event_type is LatencyEventType.FILL_OCCURRED
        )
        if fill_position != self.book.player_position.position:
            raise RuntimeError("player position does not reconcile to exchange fills")
        latency_fills = [
            (
                str(event.data["trade_id"]),
                str(event.order_id),
                str(event.data["side"]),
                int(event.data["price_ticks"]),
                int(event.data["quantity"]),
            )
            for event in self._events
            if event.event_type is LatencyEventType.FILL_OCCURRED
        ]
        exchange_fills = [
            (
                fill.trade_id,
                fill.order_id,
                fill.side.value,
                fill.price_ticks,
                fill.quantity,
            )
            for fill in self.book.player_position.fills
        ]
        if latency_fills != exchange_fills:
            raise RuntimeError("latency fill ledger does not match exchange fills")
        reported_position = sum(
            Side(str(event.data["side"])).sign * int(event.data["quantity"])
            for event in self._events
            if event.event_type is LatencyEventType.CLIENT_RECEIVED_FILL
        )
        if reported_position != self._client_position:
            raise RuntimeError("client position does not reconcile to fill reports")
        if (
            not any(
                message.kind is _MessageKind.FILL_CLIENT
                for message in self._messages
            )
            and self._client_position != self.book.player_position.position
        ):
            raise RuntimeError("settled client position differs from exchange position")
        for order in self._orders.values():
            accounted_quantity = (
                order.filled_quantity
                + order.cancelled_quantity
                + order.expired_quantity
            )
            if not 0 <= accounted_quantity <= order.quantity:
                raise RuntimeError("asynchronous order fill accounting is invalid")
            if order.state.terminal and order.state is not AsyncOrderState.REJECTED:
                if order.remaining_quantity != 0:
                    raise RuntimeError("terminal asynchronous order retains quantity")
            exchange = self.book.all_orders.get(order.order_id)
            if exchange is None and "venue_acknowledged_us" in order.timestamps:
                raise RuntimeError("venue-received order is absent from exchange ledger")
            if order.state is AsyncOrderState.FILLED and (
                exchange is None or exchange.status is not OrderStatus.FILLED
            ):
                raise RuntimeError("filled client state contradicts exchange order")
            if order.state is AsyncOrderState.CANCELLED and (
                exchange is None or exchange.status is not OrderStatus.CANCELLED
            ):
                raise RuntimeError("cancelled client state contradicts exchange order")
            if order.state is AsyncOrderState.EXPIRED and (
                exchange is None or exchange.status is not OrderStatus.EXPIRED
            ):
                raise RuntimeError("expired client state contradicts exchange order")
            if order.state is AsyncOrderState.WORKING and (
                exchange is None or exchange.status is not OrderStatus.ACTIVE
            ):
                raise RuntimeError("working client state contradicts exchange order")
            if order.state is AsyncOrderState.PARTIALLY_FILLED and (
                exchange is None
                or exchange.status is not OrderStatus.PARTIALLY_FILLED
            ):
                raise RuntimeError(
                    "partially-filled client state contradicts exchange order"
                )
            if (
                order.state is AsyncOrderState.PENDING_CANCEL
                and exchange is not None
                and exchange.status
                not in {OrderStatus.ACTIVE, OrderStatus.PARTIALLY_FILLED}
            ):
                raise RuntimeError("pending-cancel state contradicts exchange order")
        if self._messages and self._messages[0].time_us < self.clock.current_time_us:
            raise RuntimeError("latency scheduler retains a past message")

    def state_sha256(self) -> str:
        payload = {
            "book": self.book.snapshot(),
            "clock_us": self.clock.current_time_us,
            "client_position": self._client_position,
            "events": [event.as_dict() for event in self._events],
            "initial_book": {
                "ask_ticks": self.initial_ask_ticks,
                "bid_ticks": self.initial_bid_ticks,
                "queue_quantity": self.initial_queue_quantity,
            },
            "latest_display": (
                None
                if self._latest_display is None
                else self._latest_display.as_dict()
            ),
            "message_counters": {
                "cancel": self._cancel_sequence,
                "external": self._external_sequence,
                "fill_report": self._fill_report_count,
                "market_state": self._market_state_sequence,
                "message": self._message_sequence,
                "order": self._order_sequence,
            },
            "orders": [order.as_dict() for order in self.orders],
            "pending_gateway_cancels": {
                key: values
                for key, values in sorted(self._gateway_held_cancels.items())
            },
            "pending_messages": [
                {
                    "data": message.data,
                    "kind": message.kind.value,
                    "priority": message.priority,
                    "sequence": message.sequence,
                    "time_us": message.time_us,
                }
                for message in sorted(self._messages)
            ],
            "player_position": self.player_position,
            "profile": self.profile.as_dict(),
            "rng_draws": [draw.as_dict() for draw in self.sampler.draws],
            "seed": self.seed,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def event_stream_sha256(self) -> str:
        canonical = json.dumps(
            [event.as_dict() for event in self._events],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _request_order(
        self,
        order_type: OrderType,
        side: Side,
        quantity: int,
        price_ticks: int | None,
        order_id: str | None,
        emit_key: bool,
    ) -> str:
        if quantity <= 0:
            raise ValueError("asynchronous order quantity must be positive")
        if order_type is OrderType.LIMIT and (
            price_ticks is None or price_ticks <= 0
        ):
            raise ValueError("asynchronous limit price must be positive")
        if self._latest_display is None:
            raise RuntimeError("client cannot act before a market state is rendered")
        if order_id is None:
            self._order_sequence += 1
            order_id = f"ASYNC-PLAYER-{self._order_sequence:06d}"
        if order_id in self._orders or order_id in self.book.all_orders:
            raise ValueError(f"duplicate asynchronous order ID: {order_id}")
        display = self._latest_display
        order = AsyncOrder(
            order_id,
            order_type,
            side,
            quantity,
            price_ticks,
            self.clock.current_time_us,
            display.market_event_time_us,
            display.best_bid_ticks,
            display.best_ask_ticks,
        )
        self._orders[order_id] = order
        if emit_key:
            self._emit(
                LatencyEventType.KEY_PRESSED,
                order_id,
                action=f"NEW_{order_type.value.upper()}",
                displayed_state_id=display.state_id,
                observed_quote_age_us=(
                    self.clock.current_time_us - display.market_event_time_us
                ),
            )
        self._schedule_after(
            LatencyComponent.INPUT_PROCESSING,
            _MessageKind.NEW_CREATED,
            {"order_id": order_id},
            f"order:{order_id}:create",
        )
        return order_id

    def _dispatch(self, message: _ScheduledMessage) -> None:
        handlers = {
            _MessageKind.MARKET_DATA_PUBLISH: self._market_data_publish,
            _MessageKind.MARKET_DATA_CLIENT: self._market_data_client,
            _MessageKind.MARKET_DATA_RENDER: self._market_data_render,
            _MessageKind.NEW_CREATED: self._new_created,
            _MessageKind.NEW_SENT: self._new_sent,
            _MessageKind.NEW_GATEWAY: self._new_gateway,
            _MessageKind.NEW_VENUE: self._new_venue,
            _MessageKind.NEW_PROCESS: self._new_process,
            _MessageKind.NEW_ACK_CLIENT: self._new_ack_client,
            _MessageKind.CANCEL_CREATED: self._cancel_created,
            _MessageKind.CANCEL_SENT: self._cancel_sent,
            _MessageKind.CANCEL_GATEWAY: self._cancel_gateway,
            _MessageKind.CANCEL_VENUE: self._cancel_venue,
            _MessageKind.CANCEL_PROCESS: self._cancel_process,
            _MessageKind.CANCEL_ACK_CLIENT: self._cancel_ack_client,
            _MessageKind.FILL_CLIENT: self._fill_client,
            _MessageKind.FILL_UI: self._fill_ui,
            _MessageKind.EXTERNAL_MARKET: self._external_market,
            _MessageKind.EXTERNAL_REPRICE: self._external_reprice,
        }
        handlers[message.kind](message.data)

    def _market_data_publish(self, data: dict[str, object]) -> None:
        data["published_time_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.MARKET_DATA_PUBLISHED,
            None,
            market_event_time_us=int(data["market_event_time_us"]),
            reason=str(data["reason"]),
        )
        self._schedule_after(
            LatencyComponent.DOWNLINK,
            _MessageKind.MARKET_DATA_CLIENT,
            data,
            f"market_state:{data['state_id']}:downlink",
        )

    def _market_data_client(self, data: dict[str, object]) -> None:
        data["client_receive_time_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.CLIENT_RECEIVED_MARKET_DATA,
            None,
            market_event_time_us=int(data["market_event_time_us"]),
            state_id=str(data["state_id"]),
        )
        self._schedule_after(
            LatencyComponent.RENDER,
            _MessageKind.MARKET_DATA_RENDER,
            data,
            f"market_state:{data['state_id']}:render",
        )

    def _market_data_render(self, data: dict[str, object]) -> None:
        snapshot = data["snapshot"]
        if not isinstance(snapshot, dict):
            raise RuntimeError("scheduled market state snapshot is invalid")
        incoming_sequence = int(data["exchange_event_sequence"])
        incoming_version = (
            incoming_sequence,
            int(data["market_event_time_us"]),
            str(data["state_id"]),
        )
        current_version = (
            None
            if self._latest_display is None
            else (
                self._latest_display.exchange_event_sequence,
                self._latest_display.market_event_time_us,
                self._latest_display.state_id,
            )
        )
        if current_version is not None and incoming_version < current_version:
            self._emit(
                LatencyEventType.UI_RENDERED_MARKET_STATE,
                None,
                applied=False,
                reason="OUT_OF_ORDER_STALE_UPDATE_DISCARDED",
                state_id=str(data["state_id"]),
            )
            return
        self._latest_display = DisplayedMarketState(
            state_id=str(data["state_id"]),
            market_event_time_us=int(data["market_event_time_us"]),
            published_time_us=int(data["published_time_us"]),
            client_receive_time_us=int(data["client_receive_time_us"]),
            render_time_us=self.clock.current_time_us,
            exchange_event_sequence=incoming_sequence,
            best_bid_ticks=_best_price(snapshot, "bids"),
            best_ask_ticks=_best_price(snapshot, "asks"),
            snapshot=snapshot,
        )
        self._emit(
            LatencyEventType.UI_RENDERED_MARKET_STATE,
            None,
            applied=True,
            market_event_time_us=int(data["market_event_time_us"]),
            quote_age_us=self.clock.current_time_us - int(data["market_event_time_us"]),
            state_id=str(data["state_id"]),
        )

    def _new_created(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps["client_created_order_us"] = self.clock.current_time_us
        self._emit(LatencyEventType.CLIENT_CREATED_ORDER, order.order_id)
        self._schedule_after(
            LatencyComponent.CLIENT_ROUTING,
            _MessageKind.NEW_SENT,
            data,
            f"order:{order.order_id}:route",
        )

    def _new_sent(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps["order_left_client_us"] = self.clock.current_time_us
        if order.state is AsyncOrderState.CREATED:
            self._transition(order, AsyncOrderState.PENDING_NEW)
        self._emit(LatencyEventType.ORDER_LEFT_CLIENT, order.order_id)
        self._schedule_after(
            LatencyComponent.UPLINK,
            _MessageKind.NEW_GATEWAY,
            data,
            f"order:{order.order_id}:uplink",
        )

    def _new_gateway(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps["gateway_received_order_us"] = self.clock.current_time_us
        self._emit(LatencyEventType.GATEWAY_RECEIVED_ORDER, order.order_id)
        self._schedule_after(
            LatencyComponent.GATEWAY,
            _MessageKind.NEW_VENUE,
            data,
            f"order:{order.order_id}:gateway",
        )

    def _new_venue(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps["venue_received_us"] = self.clock.current_time_us
        self._emit(LatencyEventType.VENUE_RECEIVED_ORDER, order.order_id)
        self._schedule_after(
            LatencyComponent.VENUE_PROCESSING,
            _MessageKind.NEW_PROCESS,
            data,
            f"order:{order.order_id}:venue_processing",
        )

    def _new_process(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.venue_bid_ticks = self.book.best_bid
        order.venue_ask_ticks = self.book.best_ask
        exchange = (
            Order.limit(
                order.order_id,
                order.side,
                order.quantity,
                order.price_ticks,  # type: ignore[arg-type]
                OrderOwner.PLAYER,
            )
            if order.order_type is OrderType.LIMIT
            else Order.market(
                order.order_id,
                order.side,
                order.quantity,
                OrderOwner.PLAYER,
            )
        )
        try:
            events = self.book.process(exchange)
        except (TypeError, ValueError) as error:
            order.timestamps["venue_rejected_us"] = self.clock.current_time_us
            self._transition(order, AsyncOrderState.REJECTED)
            self._emit(
                LatencyEventType.VENUE_REJECTED_ORDER,
                order.order_id,
                reason=str(error),
            )
            self._release_gateway_cancels(order)
            return
        order.timestamps["venue_acknowledged_us"] = self.clock.current_time_us
        self._consume_exchange_events(events)
        exchange_order = self.book.all_orders[order.order_id]
        state = {
            OrderStatus.ACTIVE: AsyncOrderState.WORKING,
            OrderStatus.PARTIALLY_FILLED: AsyncOrderState.PARTIALLY_FILLED,
            OrderStatus.FILLED: AsyncOrderState.FILLED,
            OrderStatus.EXPIRED: AsyncOrderState.EXPIRED,
        }.get(exchange_order.status)
        if state is None:
            raise RuntimeError("new exchange order ended in unsupported state")
        if state is AsyncOrderState.EXPIRED and exchange_order.cancelled_quantity:
            order.record_expiration(exchange_order.cancelled_quantity)
        if (
            order.state is not AsyncOrderState.PENDING_CANCEL
            or state in {AsyncOrderState.FILLED, AsyncOrderState.EXPIRED}
        ):
            self._transition(order, state)
        self._emit(
            LatencyEventType.VENUE_ACKNOWLEDGED_ORDER,
            order.order_id,
            exchange_event_end=len(self.book.journal.events),
            exchange_status=self.book.all_orders[order.order_id].status.value,
        )
        self._release_gateway_cancels(order)
        ack_payload = {
            **data,
            "acknowledged_state": state.value,
            "exchange_status": exchange_order.status.value,
        }
        self._schedule_after(
            LatencyComponent.DOWNLINK,
            _MessageKind.NEW_ACK_CLIENT,
            ack_payload,
            f"order:{order.order_id}:ack_downlink",
        )
        self._record_market_change("PLAYER_ORDER_AT_VENUE")

    def _new_ack_client(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps["client_received_ack_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.CLIENT_RECEIVED_ACK,
            order.order_id,
            acknowledged_state=str(data["acknowledged_state"]),
            current_state=order.state.value,
            exchange_status=str(data["exchange_status"]),
        )

    def _cancel_created(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        target.timestamps["client_created_cancel_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.CANCEL_CREATED,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
        )
        self._schedule_after(
            LatencyComponent.CLIENT_ROUTING,
            _MessageKind.CANCEL_SENT,
            data,
            f"cancel:{data['cancel_id']}:route",
        )

    def _cancel_sent(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        target.timestamps["cancel_left_client_us"] = self.clock.current_time_us
        if not target.state.terminal:
            self._transition(target, AsyncOrderState.PENDING_CANCEL)
        self._emit(
            LatencyEventType.CANCEL_LEFT_CLIENT,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
        )
        self._schedule_after(
            LatencyComponent.UPLINK,
            _MessageKind.CANCEL_GATEWAY,
            data,
            f"cancel:{data['cancel_id']}:uplink",
        )

    def _cancel_gateway(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        target.timestamps["gateway_received_cancel_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.GATEWAY_RECEIVED_CANCEL,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
        )
        if (
            not target.state.terminal
            and "venue_acknowledged_us" not in target.timestamps
        ):
            self._gateway_held_cancels.setdefault(target.order_id, []).append(
                dict(data)
            )
            self._emit(
                LatencyEventType.CANCEL_HELD_PENDING_NEW,
                target.order_id,
                cancel_id=str(data["cancel_id"]),
                reason="gateway preserves new-before-cancel causality",
            )
            return
        self._send_cancel_to_venue(data)

    def _send_cancel_to_venue(self, data: dict[str, object]) -> None:
        self._schedule_after(
            LatencyComponent.GATEWAY,
            _MessageKind.CANCEL_VENUE,
            data,
            f"cancel:{data['cancel_id']}:gateway",
        )

    def _release_gateway_cancels(self, target: AsyncOrder) -> None:
        for data in self._gateway_held_cancels.pop(target.order_id, []):
            self._send_cancel_to_venue(data)

    def _cancel_venue(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        target.timestamps["venue_received_cancel_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.VENUE_RECEIVED_CANCEL,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
        )
        self._schedule_after(
            LatencyComponent.VENUE_PROCESSING,
            _MessageKind.CANCEL_PROCESS,
            data,
            f"cancel:{data['cancel_id']}:venue_processing",
        )

    def _cancel_process(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        exchange_target = self.book.active_orders.get(target.order_id)
        active_player_order = (
            exchange_target is not None
            and exchange_target.owner is OrderOwner.PLAYER
            and target.state is not AsyncOrderState.REJECTED
        )
        if active_player_order:
            cancelled_quantity = exchange_target.remaining_quantity
            events = self.book.cancel(target.order_id, str(data["cancel_id"]))
            self._consume_exchange_events(events)
            target.record_cancel(cancelled_quantity)
            self._transition(target, AsyncOrderState.CANCELLED)
            outcome = (
                "PARTIAL_FILL_THEN_CANCELLED"
                if target.filled_quantity
                else "CANCEL_WON"
            )
            self._record_market_change("PLAYER_CANCEL_AT_VENUE")
        else:
            outcome = (
                "FILL_BEFORE_CANCEL"
                if target.state is AsyncOrderState.FILLED
                else f"TOO_LATE_{target.state.value}"
            )
        target.cancel_race_outcome = outcome
        target.timestamps["venue_cancel_acknowledged_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.VENUE_ACKNOWLEDGED_CANCEL,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
            outcome=outcome,
        )
        payload = {**data, "outcome": outcome}
        self._schedule_after(
            LatencyComponent.DOWNLINK,
            _MessageKind.CANCEL_ACK_CLIENT,
            payload,
            f"cancel:{data['cancel_id']}:ack_downlink",
        )

    def _cancel_ack_client(self, data: dict[str, object]) -> None:
        target = self._require_order(str(data["target_order_id"]))
        target.timestamps["client_received_cancel_ack_us"] = self.clock.current_time_us
        self._emit(
            LatencyEventType.CLIENT_RECEIVED_CANCEL_ACK,
            target.order_id,
            cancel_id=str(data["cancel_id"]),
            outcome=str(data["outcome"]),
            state=target.state.value,
        )

    def _external_market(self, data: dict[str, object]) -> None:
        side = Side(str(data["side"]))
        order = Order.market(
            str(data["order_id"]),
            side,
            int(data["quantity"]),
            OrderOwner.SIMULATED,
        )
        events = self.book.process(order)
        self._emit(
            LatencyEventType.EXTERNAL_AGGRESSIVE_ORDER,
            None,
            external_order_id=order.order_id,
            quantity=order.original_quantity,
            side=side.value,
        )
        self._consume_exchange_events(events)
        self._record_market_change("EXTERNAL_AGGRESSIVE_ORDER")

    def _external_reprice(self, data: dict[str, object]) -> None:
        target = str(data["target_order_id"])
        if target not in self.book.active_orders:
            raise RuntimeError("external liquidity reprice target is not active")
        self._external_sequence += 1
        cancel_id = f"ASYNC-EXTERNAL-CANCEL-{self._external_sequence:06d}"
        events = list(self.book.cancel(target, cancel_id))
        replacement = Order.limit(
            str(data["new_order_id"]),
            Side(str(data["side"])),
            int(data["quantity"]),
            int(data["price_ticks"]),
            OrderOwner.SIMULATED,
        )
        events.extend(self.book.process(replacement))
        self._emit(
            LatencyEventType.EXTERNAL_LIQUIDITY_REPRICE,
            None,
            new_order_id=replacement.order_id,
            price_ticks=replacement.price_ticks,
            side=replacement.side.value,  # type: ignore[union-attr]
            target_order_id=target,
        )
        self._consume_exchange_events(tuple(events))
        self._record_market_change("EXTERNAL_LIQUIDITY_REPRICE")

    def _consume_exchange_events(self, events: tuple[SimulationEvent, ...]) -> None:
        for event in events:
            if event.event_type not in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                continue
            order_id = str(event.data["order_id"])
            order = self._orders.get(order_id)
            if order is None:
                continue
            quantity = int(event.data["fill_quantity"])
            price_ticks = int(event.data["price_ticks"])
            order.record_fill(quantity, price_ticks, self.clock.current_time_us)
            if event.event_type is EventType.FULL_FILL:
                self._transition(order, AsyncOrderState.FILLED)
            elif order.state is not AsyncOrderState.PENDING_CANCEL:
                self._transition(order, AsyncOrderState.PARTIALLY_FILLED)
            self._emit(
                LatencyEventType.FILL_OCCURRED,
                order_id,
                exchange_event_sequence=event.sequence,
                price_ticks=price_ticks,
                quantity=quantity,
                side=order.side.value,
                trade_id=str(event.data["trade_id"]),
            )
            order.timestamps.setdefault(
                "fill_report_left_venue_us", self.clock.current_time_us
            )
            self._emit(
                LatencyEventType.FILL_REPORT_LEFT_VENUE,
                order_id,
                price_ticks=price_ticks,
                quantity=quantity,
                trade_id=str(event.data["trade_id"]),
            )
            self._fill_report_count += 1
            payload = {
                "fill_report_sequence": self._fill_report_count,
                "order_id": order_id,
                "price_ticks": price_ticks,
                "quantity": quantity,
                "side": order.side.value,
                "trade_id": str(event.data["trade_id"]),
            }
            self._schedule_after(
                LatencyComponent.FILL_REPORT,
                _MessageKind.FILL_CLIENT,
                payload,
                f"fill:{payload['trade_id']}:report",
            )

    def _fill_client(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps.setdefault("client_received_fill_us", self.clock.current_time_us)
        self._client_position += Side(str(data["side"])).sign * int(data["quantity"])
        self._emit(
            LatencyEventType.CLIENT_RECEIVED_FILL,
            order.order_id,
            client_position=self._client_position,
            price_ticks=int(data["price_ticks"]),
            quantity=int(data["quantity"]),
            side=str(data["side"]),
            trade_id=str(data["trade_id"]),
        )
        self._schedule_after(
            LatencyComponent.RENDER,
            _MessageKind.FILL_UI,
            data,
            f"fill:{data['trade_id']}:render",
        )

    def _fill_ui(self, data: dict[str, object]) -> None:
        order = self._require_order(str(data["order_id"]))
        order.timestamps.setdefault("ui_displayed_fill_us", self.clock.current_time_us)
        self._emit(
            LatencyEventType.UI_DISPLAYED_FILL,
            order.order_id,
            price_ticks=int(data["price_ticks"]),
            quantity=int(data["quantity"]),
            trade_id=str(data["trade_id"]),
        )

    def _record_market_change(self, reason: str) -> None:
        self._market_state_sequence += 1
        state_id = f"LATENCY-MS-{self._market_state_sequence:06d}"
        snapshot = self.book.snapshot()
        data: dict[str, object] = {
            "exchange_event_sequence": len(self.book.journal.events),
            "market_event_time_us": self.clock.current_time_us,
            "reason": reason,
            "snapshot": snapshot,
            "state_id": state_id,
        }
        self._emit(
            LatencyEventType.MARKET_EVENT_OCCURRED,
            None,
            exchange_event_sequence=len(self.book.journal.events),
            reason=reason,
            state_id=state_id,
        )
        self._schedule_after(
            LatencyComponent.MARKET_DATA_PUBLICATION,
            _MessageKind.MARKET_DATA_PUBLISH,
            data,
            f"market_state:{state_id}:publish",
        )

    def _transition(self, order: AsyncOrder, state: AsyncOrderState) -> None:
        previous = order.state
        order.transition(state, self.clock.current_time_us)
        if previous is not state:
            self._emit(
                LatencyEventType.ORDER_STATE_CHANGED,
                order.order_id,
                current_state=state.value,
                previous_state=previous.value,
            )

    def _schedule_after(
        self,
        component: LatencyComponent,
        kind: _MessageKind,
        data: dict[str, object],
        purpose: str,
    ) -> None:
        delay = self.sampler.sample(
            component,
            self.profile.distribution(component),
            self.clock.current_time_us,
            purpose,
        )
        self._schedule(self.clock.current_time_us + delay, kind, data)

    def _schedule(
        self,
        time_us: int,
        kind: _MessageKind,
        data: dict[str, object],
    ) -> None:
        if time_us < self.clock.current_time_us:
            raise RuntimeError("cannot schedule a latency message in the past")
        self._message_sequence += 1
        heapq.heappush(
            self._messages,
            _ScheduledMessage(
                time_us,
                _MESSAGE_PRIORITY[kind],
                self._message_sequence,
                kind,
                dict(data),
            ),
        )

    def _emit(
        self,
        event_type: LatencyEventType,
        order_id: str | None,
        **data: object,
    ) -> None:
        self._events.append(
            LatencyEvent(
                len(self._events) + 1,
                self.clock.current_time_us,
                event_type,
                order_id,
                dict(data),
            )
        )

    def _require_order(self, order_id: str) -> AsyncOrder:
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise ValueError(f"unknown asynchronous order: {order_id}") from error

    def _display_state_id(self) -> str | None:
        return None if self._latest_display is None else self._latest_display.state_id


class LatencyTimelineInspector:
    def __init__(self, events: tuple[LatencyEvent, ...]) -> None:
        self.events = events

    def render(self, order_id: str | None = None) -> str:
        selected = (
            self.events
            if order_id is None
            else tuple(event for event in self.events if event.order_id in {None, order_id})
        )
        return "\n".join(
            f"{_market_time(event.simulation_time_us)}  {event.event_type.value}"
            + ("" if event.order_id is None else f" order={event.order_id}")
            for event in selected
        )


def _best_price(snapshot: dict[str, object], side: str) -> int | None:
    levels = snapshot.get(side)
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if not isinstance(first, dict):
        raise RuntimeError("exchange snapshot level is invalid")
    return int(first["price_ticks"])


def _market_time(simulation_time_us: int) -> str:
    total_us = (9 * 60 * 60 + 30 * 60) * 1_000_000 + simulation_time_us
    hours, remainder = divmod(total_us, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    return (
        f"{hours % 24:02d}:{minutes:02d}:{seconds:02d}."
        f"{microseconds:06d}"
    )
