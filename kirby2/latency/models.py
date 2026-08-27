"""Asynchronous client-order lifecycle, event, and measurement contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from kirby2.exchange import OrderType, Side
from kirby2.immutable import freeze_json, thaw_json


LATENCY_RECORDING_SCHEMA_VERSION = 1


class AsyncOrderState(str, Enum):
    CREATED = "CREATED"
    PENDING_NEW = "PENDING_NEW"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def terminal(self) -> bool:
        return self in {
            AsyncOrderState.CANCELLED,
            AsyncOrderState.FILLED,
            AsyncOrderState.REJECTED,
            AsyncOrderState.EXPIRED,
        }


_ALLOWED_TRANSITIONS: dict[AsyncOrderState, frozenset[AsyncOrderState]] = {
    AsyncOrderState.CREATED: frozenset(
        {
            AsyncOrderState.PENDING_NEW,
            AsyncOrderState.PENDING_CANCEL,
            AsyncOrderState.REJECTED,
        }
    ),
    AsyncOrderState.PENDING_NEW: frozenset(
        {
            AsyncOrderState.WORKING,
            AsyncOrderState.PARTIALLY_FILLED,
            AsyncOrderState.PENDING_CANCEL,
            AsyncOrderState.FILLED,
            AsyncOrderState.REJECTED,
            AsyncOrderState.EXPIRED,
        }
    ),
    AsyncOrderState.WORKING: frozenset(
        {
            AsyncOrderState.PARTIALLY_FILLED,
            AsyncOrderState.PENDING_CANCEL,
            AsyncOrderState.FILLED,
            AsyncOrderState.CANCELLED,
            AsyncOrderState.EXPIRED,
        }
    ),
    AsyncOrderState.PARTIALLY_FILLED: frozenset(
        {
            AsyncOrderState.PENDING_CANCEL,
            AsyncOrderState.FILLED,
            AsyncOrderState.CANCELLED,
            AsyncOrderState.EXPIRED,
        }
    ),
    AsyncOrderState.PENDING_CANCEL: frozenset(
        {
            AsyncOrderState.PARTIALLY_FILLED,
            AsyncOrderState.CANCELLED,
            AsyncOrderState.FILLED,
            AsyncOrderState.REJECTED,
            AsyncOrderState.EXPIRED,
        }
    ),
    AsyncOrderState.CANCELLED: frozenset(),
    AsyncOrderState.FILLED: frozenset(),
    AsyncOrderState.REJECTED: frozenset(),
    AsyncOrderState.EXPIRED: frozenset(),
}


class LatencyEventType(str, Enum):
    MARKET_EVENT_OCCURRED = "MARKET_EVENT_OCCURRED"
    MARKET_DATA_PUBLISHED = "MARKET_DATA_PUBLISHED"
    CLIENT_RECEIVED_MARKET_DATA = "CLIENT_RECEIVED_MARKET_DATA"
    UI_RENDERED_MARKET_STATE = "UI_RENDERED_MARKET_STATE"
    KEY_PRESSED = "KEY_PRESSED"
    CLIENT_CREATED_ORDER = "CLIENT_CREATED_ORDER"
    ORDER_LEFT_CLIENT = "ORDER_LEFT_CLIENT"
    GATEWAY_RECEIVED_ORDER = "GATEWAY_RECEIVED_ORDER"
    VENUE_RECEIVED_ORDER = "VENUE_RECEIVED_ORDER"
    VENUE_ACKNOWLEDGED_ORDER = "VENUE_ACKNOWLEDGED_ORDER"
    VENUE_REJECTED_ORDER = "VENUE_REJECTED_ORDER"
    CLIENT_RECEIVED_ACK = "CLIENT_RECEIVED_ACK"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED"
    FILL_OCCURRED = "FILL_OCCURRED"
    FILL_REPORT_LEFT_VENUE = "FILL_REPORT_LEFT_VENUE"
    CLIENT_RECEIVED_FILL = "CLIENT_RECEIVED_FILL"
    UI_DISPLAYED_FILL = "UI_DISPLAYED_FILL"
    CANCEL_CREATED = "CANCEL_CREATED"
    CANCEL_LEFT_CLIENT = "CANCEL_LEFT_CLIENT"
    GATEWAY_RECEIVED_CANCEL = "GATEWAY_RECEIVED_CANCEL"
    VENUE_RECEIVED_CANCEL = "VENUE_RECEIVED_CANCEL"
    VENUE_ACKNOWLEDGED_CANCEL = "VENUE_ACKNOWLEDGED_CANCEL"
    CLIENT_RECEIVED_CANCEL_ACK = "CLIENT_RECEIVED_CANCEL_ACK"
    CANCEL_HELD_PENDING_NEW = "CANCEL_HELD_PENDING_NEW"
    REPLACE_REJECTED_BEFORE_ACK = "REPLACE_REJECTED_BEFORE_ACK"
    EXTERNAL_AGGRESSIVE_ORDER = "EXTERNAL_AGGRESSIVE_ORDER"
    EXTERNAL_LIQUIDITY_REPRICE = "EXTERNAL_LIQUIDITY_REPRICE"


@dataclass(frozen=True, slots=True)
class LatencyEvent:
    sequence: int
    simulation_time_us: int
    event_type: LatencyEventType
    order_id: str | None
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("latency event data must be a JSON object")
        object.__setattr__(self, "data", frozen)
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("latency event sequence or time is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type.value,
            "order_id": self.order_id,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(slots=True)
class AsyncOrder:
    order_id: str
    order_type: OrderType
    side: Side
    quantity: int
    price_ticks: int | None
    intention_time_us: int
    observed_quote_time_us: int
    observed_bid_ticks: int | None
    observed_ask_ticks: int | None
    venue_bid_ticks: int | None = None
    venue_ask_ticks: int | None = None
    state: AsyncOrderState = AsyncOrderState.CREATED
    filled_quantity: int = 0
    cancelled_quantity: int = 0
    expired_quantity: int = 0
    fill_value_ticks: int = 0
    timestamps: dict[str, int] = field(default_factory=dict)
    cancel_race_outcome: str | None = None

    def __post_init__(self) -> None:
        if not self.order_id or self.quantity <= 0 or self.intention_time_us < 0:
            raise ValueError("asynchronous order identity is invalid")
        if self.order_type.value == "limit" and (
            self.price_ticks is None or self.price_ticks <= 0
        ):
            raise ValueError("asynchronous limit order requires a positive tick price")
        if self.order_type.value == "market" and self.price_ticks is not None:
            raise ValueError("asynchronous market order cannot carry a price")
        if not 0 <= self.observed_quote_time_us <= self.intention_time_us:
            raise ValueError("observed quote time is outside order intention time")
        self.timestamps["player_pressed_key_us"] = self.intention_time_us

    @property
    def remaining_quantity(self) -> int:
        return (
            self.quantity
            - self.filled_quantity
            - self.cancelled_quantity
            - self.expired_quantity
        )

    def transition(self, state: AsyncOrderState, time_us: int) -> None:
        if state is self.state:
            return
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise RuntimeError(
                f"invalid asynchronous order transition {self.state.value}->{state.value}"
            )
        self.state = state
        self.timestamps[f"state_{state.value.lower()}_us"] = time_us

    def record_fill(self, quantity: int, price_ticks: int, time_us: int) -> None:
        if quantity <= 0 or price_ticks <= 0:
            raise ValueError("asynchronous fill is invalid")
        if quantity > self.remaining_quantity:
            raise RuntimeError("asynchronous fill exceeds original order quantity")
        self.filled_quantity += quantity
        self.fill_value_ticks += quantity * price_ticks
        self.timestamps.setdefault("first_fill_occurred_us", time_us)
        self.timestamps["last_fill_occurred_us"] = time_us

    def record_cancel(self, quantity: int) -> None:
        if quantity <= 0 or quantity > self.remaining_quantity:
            raise RuntimeError("asynchronous cancellation quantity is invalid")
        self.cancelled_quantity += quantity

    def record_expiration(self, quantity: int) -> None:
        if quantity <= 0 or quantity > self.remaining_quantity:
            raise RuntimeError("asynchronous expiration quantity is invalid")
        self.expired_quantity += quantity

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_race_outcome": self.cancel_race_outcome,
            "cancelled_quantity": self.cancelled_quantity,
            "expired_quantity": self.expired_quantity,
            "fill_value_ticks": self.fill_value_ticks,
            "filled_quantity": self.filled_quantity,
            "intention_time_us": self.intention_time_us,
            "observed_ask_ticks": self.observed_ask_ticks,
            "observed_bid_ticks": self.observed_bid_ticks,
            "observed_quote_time_us": self.observed_quote_time_us,
            "order_id": self.order_id,
            "order_type": self.order_type.value,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "remaining_quantity": self.remaining_quantity,
            "side": self.side.value,
            "state": self.state.value,
            "timestamps": dict(sorted(self.timestamps.items())),
            "venue_ask_ticks": self.venue_ask_ticks,
            "venue_bid_ticks": self.venue_bid_ticks,
        }


@dataclass(frozen=True, slots=True)
class DisplayedMarketState:
    state_id: str
    market_event_time_us: int
    published_time_us: int
    client_receive_time_us: int
    render_time_us: int
    exchange_event_sequence: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    snapshot: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_ticks": self.best_bid_ticks,
            "client_receive_time_us": self.client_receive_time_us,
            "exchange_event_sequence": self.exchange_event_sequence,
            "market_event_time_us": self.market_event_time_us,
            "published_time_us": self.published_time_us,
            "render_time_us": self.render_time_us,
            "snapshot": self.snapshot,
            "state_id": self.state_id,
        }


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    order_id: str
    decision_to_send_latency_us: int | None
    send_to_ack_latency_us: int | None
    send_to_fill_latency_us: int | None
    observed_quote_age_us: int
    execution_against_stale_quote: bool
    cancel_race_outcome: str | None
    latency_induced_slippage_ticks: str | None
    intention_time_us: int
    venue_execution_time_us: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_race_outcome": self.cancel_race_outcome,
            "decision_to_send_latency_us": self.decision_to_send_latency_us,
            "execution_against_stale_quote": self.execution_against_stale_quote,
            "intention_time_us": self.intention_time_us,
            "latency_induced_slippage_ticks": self.latency_induced_slippage_ticks,
            "observed_quote_age_us": self.observed_quote_age_us,
            "order_id": self.order_id,
            "send_to_ack_latency_us": self.send_to_ack_latency_us,
            "send_to_fill_latency_us": self.send_to_fill_latency_us,
            "venue_execution_time_us": self.venue_execution_time_us,
        }
