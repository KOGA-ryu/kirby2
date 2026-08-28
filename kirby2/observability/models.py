"""Contracts separating hidden venue truth from the observable market feed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from kirby2.exchange.models import OrderOwner, OrderType, Side
from kirby2.immutable import freeze_json, thaw_json
from kirby2.session.events import SimulationEvent


OBSERVABILITY_RECORDING_SCHEMA_VERSION = 1


class LiquidityKind(str, Enum):
    DISPLAYED_LIMIT = "DISPLAYED_LIMIT"
    ICEBERG = "ICEBERG"
    HIDDEN_LIMIT = "HIDDEN_LIMIT"
    MIDPOINT_HIDDEN = "MIDPOINT_HIDDEN"


class IcebergRefreshBehavior(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"


class IcebergRefreshPriority(str, Enum):
    PRESERVE = "PRESERVE"
    LOSE = "LOSE"


class RefreshEventVisibility(str, Enum):
    QUOTE_UPDATE_ONLY = "QUOTE_UPDATE_ONLY"
    EXPLICIT_REPLENISHMENT = "EXPLICIT_REPLENISHMENT"


class HiddenPriority(str, Enum):
    AFTER_DISPLAYED = "AFTER_DISPLAYED"
    BEFORE_DISPLAYED = "BEFORE_DISPLAYED"


class QueueDataMode(str, Enum):
    AGGREGATED_DEPTH = "AGGREGATED_DEPTH"
    MARKET_BY_ORDER = "MARKET_BY_ORDER"


class ObservableEventType(str, Enum):
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    DISPLAY_QUANTITY_CHANGED = "DISPLAY_QUANTITY_CHANGED"
    TRADE = "TRADE"
    EXPLICIT_REPLENISHMENT = "EXPLICIT_REPLENISHMENT"
    OWN_ORDER_ACKNOWLEDGED = "OWN_ORDER_ACKNOWLEDGED"
    OWN_ORDER_FILL = "OWN_ORDER_FILL"
    OWN_ORDER_CANCELLED = "OWN_ORDER_CANCELLED"
    SESSION_COMPLETE = "SESSION_COMPLETE"


class TruthEventType(str, Enum):
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ICEBERG_REFRESHED = "ICEBERG_REFRESHED"
    TRADE = "TRADE"
    SESSION_COMPLETE = "SESSION_COMPLETE"


DISPLAY_DECREASE_POSSIBLE_CAUSES = (
    "EXECUTION",
    "CANCELLATION",
    "REFRESH",
    "FEED_DELAY",
    "SNAPSHOT_REPLACEMENT",
)


@dataclass(frozen=True, slots=True)
class HiddenLiquidityRules:
    allow_fully_hidden: bool = True
    allow_midpoint_hidden: bool = True
    iceberg_refresh_priority: IcebergRefreshPriority = (
        IcebergRefreshPriority.LOSE
    )
    hidden_priority: HiddenPriority = HiddenPriority.AFTER_DISPLAYED
    queue_data_mode: QueueDataMode = QueueDataMode.AGGREGATED_DEPTH
    feed_delay_us: int = 0

    def __post_init__(self) -> None:
        if type(self.allow_fully_hidden) is not bool:
            raise TypeError("fully-hidden permission must be boolean")
        if type(self.allow_midpoint_hidden) is not bool:
            raise TypeError("midpoint-hidden permission must be boolean")
        if not isinstance(self.iceberg_refresh_priority, IcebergRefreshPriority):
            raise TypeError("iceberg refresh priority must use the canonical enum")
        if not isinstance(self.hidden_priority, HiddenPriority):
            raise TypeError("hidden priority must use the canonical enum")
        if not isinstance(self.queue_data_mode, QueueDataMode):
            raise TypeError("queue data mode must use the canonical enum")
        if type(self.feed_delay_us) is not int or self.feed_delay_us < 0:
            raise ValueError("feed delay must be nonnegative simulation microseconds")

    def as_dict(self) -> dict[str, object]:
        return {
            "allow_fully_hidden": self.allow_fully_hidden,
            "allow_midpoint_hidden": self.allow_midpoint_hidden,
            "feed_delay_us": self.feed_delay_us,
            "hidden_priority": self.hidden_priority.value,
            "iceberg_refresh_priority": self.iceberg_refresh_priority.value,
            "queue_data_mode": self.queue_data_mode.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> HiddenLiquidityRules:
        return cls(
            allow_fully_hidden=_required_bool(payload, "allow_fully_hidden"),
            allow_midpoint_hidden=_required_bool(
                payload,
                "allow_midpoint_hidden",
            ),
            iceberg_refresh_priority=IcebergRefreshPriority(
                str(payload["iceberg_refresh_priority"])
            ),
            hidden_priority=HiddenPriority(str(payload["hidden_priority"])),
            queue_data_mode=QueueDataMode(str(payload["queue_data_mode"])),
            feed_delay_us=_required_int(payload, "feed_delay_us"),
        )


@dataclass(frozen=True, slots=True)
class IcebergDefinition:
    display_quantity: int
    reserve_quantity: int
    refresh_quantity: int
    refresh_behavior: IcebergRefreshBehavior
    event_visibility: RefreshEventVisibility

    def __post_init__(self) -> None:
        quantities = (
            self.display_quantity,
            self.reserve_quantity,
            self.refresh_quantity,
        )
        if any(type(value) is not int or value <= 0 for value in quantities):
            raise ValueError("iceberg quantities must be positive integers")
        if not isinstance(self.refresh_behavior, IcebergRefreshBehavior):
            raise TypeError("iceberg refresh behavior must use the canonical enum")
        if not isinstance(self.event_visibility, RefreshEventVisibility):
            raise TypeError("iceberg event visibility must use the canonical enum")

    @property
    def total_quantity(self) -> int:
        return self.display_quantity + self.reserve_quantity

    def as_dict(self) -> dict[str, object]:
        return {
            "display_quantity": self.display_quantity,
            "event_visibility": self.event_visibility.value,
            "refresh_behavior": self.refresh_behavior.value,
            "refresh_quantity": self.refresh_quantity,
            "reserve_quantity": self.reserve_quantity,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> IcebergDefinition:
        return cls(
            display_quantity=_required_int(payload, "display_quantity"),
            reserve_quantity=_required_int(payload, "reserve_quantity"),
            refresh_quantity=_required_int(payload, "refresh_quantity"),
            refresh_behavior=IcebergRefreshBehavior(
                str(payload["refresh_behavior"])
            ),
            event_visibility=RefreshEventVisibility(
                str(payload["event_visibility"])
            ),
        )


@dataclass(frozen=True, slots=True)
class HiddenOrderRequest:
    order_id: str
    side: Side
    kind: LiquidityKind
    owner: OrderOwner
    account_id: str
    quantity: int
    price_ticks: int | None = None
    iceberg: IcebergDefinition | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.account_id:
            raise ValueError("hidden-liquidity order identity and account are required")
        if not isinstance(self.side, Side) or not isinstance(self.owner, OrderOwner):
            raise TypeError("order side and owner must use canonical enums")
        if not isinstance(self.kind, LiquidityKind):
            raise TypeError("liquidity kind must use the canonical enum")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("hidden-liquidity order quantity must be positive")
        if self.kind is LiquidityKind.MIDPOINT_HIDDEN:
            if self.price_ticks is not None:
                raise ValueError("midpoint-hidden order cannot carry a limit price")
        elif type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("priced hidden-liquidity order requires integer ticks")
        if self.kind is LiquidityKind.ICEBERG:
            if self.iceberg is None or self.iceberg.total_quantity != self.quantity:
                raise ValueError("iceberg display plus reserve must equal order quantity")
        elif self.iceberg is not None:
            raise ValueError("iceberg definition requires ICEBERG liquidity kind")

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "iceberg": None if self.iceberg is None else self.iceberg.as_dict(),
            "kind": self.kind.value,
            "order_id": self.order_id,
            "owner": self.owner.value,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "side": self.side.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> HiddenOrderRequest:
        raw_iceberg = payload.get("iceberg")
        if raw_iceberg is not None and not isinstance(raw_iceberg, dict):
            raise ValueError("iceberg request payload is invalid")
        return cls(
            order_id=str(payload["order_id"]),
            side=Side(str(payload["side"])),
            kind=LiquidityKind(str(payload["kind"])),
            owner=OrderOwner(str(payload["owner"])),
            account_id=str(payload["account_id"]),
            quantity=_required_int(payload, "quantity"),
            price_ticks=_optional_int(payload.get("price_ticks")),
            iceberg=(
                None
                if raw_iceberg is None
                else IcebergDefinition.from_dict(raw_iceberg)
            ),
        )


@dataclass(frozen=True, slots=True)
class ObservablePriceLevel:
    price_ticks: int
    side: Side
    total_quantity: int

    def __post_init__(self) -> None:
        if (
            type(self.price_ticks) is not int
            or type(self.total_quantity) is not int
            or self.price_ticks <= 0
            or self.total_quantity <= 0
        ):
            raise ValueError("observable level price and quantity must be positive")
        if not isinstance(self.side, Side):
            raise TypeError("observable level side must use the canonical enum")

    def as_dict(self) -> dict[str, object]:
        return {
            "price_ticks": self.price_ticks,
            "side": self.side.value,
            "total_quantity": self.total_quantity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ObservablePriceLevel:
        _require_exact_fields(
            payload,
            {"price_ticks", "side", "total_quantity"},
            "observable price level",
        )
        return cls(
            _required_int(payload, "price_ticks"),
            Side(_required_string(payload, "side")),
            _required_int(payload, "total_quantity"),
        )


@dataclass(frozen=True, slots=True)
class ObservableDepthBook:
    bid_levels: tuple[ObservablePriceLevel, ...] = ()
    ask_levels: tuple[ObservablePriceLevel, ...] = ()

    def __post_init__(self) -> None:
        if any(level.side is not Side.BUY for level in self.bid_levels):
            raise ValueError("observable bid side contains a non-bid level")
        if any(level.side is not Side.SELL for level in self.ask_levels):
            raise ValueError("observable ask side contains a non-ask level")
        bid_prices = tuple(item.price_ticks for item in self.bid_levels)
        ask_prices = tuple(item.price_ticks for item in self.ask_levels)
        if bid_prices != tuple(sorted(bid_prices, reverse=True)):
            raise ValueError("observable bids are not descending")
        if ask_prices != tuple(sorted(ask_prices)):
            raise ValueError("observable asks are not ascending")
        if len(bid_prices) != len(set(bid_prices)):
            raise ValueError("observable bid prices are duplicated")
        if len(ask_prices) != len(set(ask_prices)):
            raise ValueError("observable ask prices are duplicated")
        if self.best_bid is not None and self.best_ask is not None:
            if self.best_bid >= self.best_ask:
                raise ValueError("observable resting book is locked or crossed")

    @property
    def bids(self) -> dict[int, ObservablePriceLevel]:
        return {level.price_ticks: level for level in self.bid_levels}

    @property
    def asks(self) -> dict[int, ObservablePriceLevel]:
        return {level.price_ticks: level for level in self.ask_levels}

    @property
    def bid_prices(self) -> list[int]:
        return [level.price_ticks for level in self.bid_levels]

    @property
    def ask_prices(self) -> list[int]:
        return [level.price_ticks for level in self.ask_levels]

    @property
    def best_bid(self) -> int | None:
        return None if not self.bid_levels else self.bid_levels[0].price_ticks

    @property
    def best_ask(self) -> int | None:
        return None if not self.ask_levels else self.ask_levels[0].price_ticks

    def as_dict(self) -> dict[str, object]:
        return {
            "asks": [level.as_dict() for level in self.ask_levels],
            "bids": [level.as_dict() for level in self.bid_levels],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ObservableDepthBook:
        _require_exact_fields(payload, {"asks", "bids"}, "observable depth book")
        bids = _object_array(payload["bids"], "observable bids")
        asks = _object_array(payload["asks"], "observable asks")
        return cls(
            tuple(ObservablePriceLevel.from_dict(row) for row in bids),
            tuple(ObservablePriceLevel.from_dict(row) for row in asks),
        )


@dataclass(frozen=True, slots=True)
class PublicTrade:
    trade_id: str
    simulation_time_us: int
    price_x2: int
    quantity: int
    aggressor_side: Side

    def __post_init__(self) -> None:
        if (
            not self.trade_id
            or type(self.simulation_time_us) is not int
            or self.simulation_time_us < 0
            or type(self.price_x2) is not int
            or self.price_x2 <= 0
            or type(self.quantity) is not int
            or self.quantity <= 0
        ):
            raise ValueError("public trade identity, time, price, or quantity is invalid")
        if not isinstance(self.aggressor_side, Side):
            raise TypeError("public trade aggressor side must use the canonical enum")

    @property
    def price_ticks(self) -> Decimal:
        return Decimal(self.price_x2) / Decimal(2)

    def as_dict(self) -> dict[str, object]:
        return {
            "aggressor_side": self.aggressor_side.value,
            "price_ticks": str(self.price_ticks),
            "price_x2": self.price_x2,
            "quantity": self.quantity,
            "simulation_time_us": self.simulation_time_us,
            "trade_id": self.trade_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PublicTrade:
        _require_exact_fields(
            payload,
            {
                "aggressor_side",
                "price_ticks",
                "price_x2",
                "quantity",
                "simulation_time_us",
                "trade_id",
            },
            "public trade",
        )
        trade = cls(
            _required_string(payload, "trade_id"),
            _required_int(payload, "simulation_time_us"),
            _required_int(payload, "price_x2"),
            _required_int(payload, "quantity"),
            Side(_required_string(payload, "aggressor_side")),
        )
        if payload["price_ticks"] != str(trade.price_ticks):
            raise ValueError("public trade price representations differ")
        return trade


@dataclass(frozen=True, slots=True)
class OwnOrderState:
    order_id: str
    side: Side
    price_ticks: int | None
    acknowledged: bool
    status: str
    original_quantity: int
    filled_quantity: int
    remaining_quantity: int
    displayed_quantity: int

    def __post_init__(self) -> None:
        if not self.order_id or not isinstance(self.side, Side):
            raise ValueError("own order identity or side is invalid")
        if self.price_ticks is not None and (
            type(self.price_ticks) is not int or self.price_ticks <= 0
        ):
            raise ValueError("own order price must be positive integer ticks")
        quantities = (
            self.original_quantity,
            self.filled_quantity,
            self.remaining_quantity,
            self.displayed_quantity,
        )
        if any(type(value) is not int or value < 0 for value in quantities):
            raise ValueError("own order quantities must be nonnegative integers")
        if (
            self.filled_quantity + self.remaining_quantity > self.original_quantity
            or self.displayed_quantity > self.remaining_quantity
        ):
            raise ValueError("own order quantities do not conserve")

    def as_dict(self) -> dict[str, object]:
        return {
            "acknowledged": self.acknowledged,
            "displayed_quantity": self.displayed_quantity,
            "filled_quantity": self.filled_quantity,
            "order_id": self.order_id,
            "original_quantity": self.original_quantity,
            "price_ticks": self.price_ticks,
            "remaining_quantity": self.remaining_quantity,
            "side": self.side.value,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> OwnOrderState:
        _require_exact_fields(
            payload,
            {
                "acknowledged",
                "displayed_quantity",
                "filled_quantity",
                "order_id",
                "original_quantity",
                "price_ticks",
                "remaining_quantity",
                "side",
                "status",
            },
            "own order state",
        )
        acknowledged = payload["acknowledged"]
        if type(acknowledged) is not bool:
            raise TypeError("own order acknowledgement must be boolean")
        return cls(
            order_id=_required_string(payload, "order_id"),
            side=Side(_required_string(payload, "side")),
            price_ticks=_optional_int(payload["price_ticks"]),
            acknowledged=acknowledged,
            status=_required_string(payload, "status"),
            original_quantity=_required_int(payload, "original_quantity"),
            filled_quantity=_required_int(payload, "filled_quantity"),
            remaining_quantity=_required_int(payload, "remaining_quantity"),
            displayed_quantity=_required_int(payload, "displayed_quantity"),
        )


@dataclass(frozen=True, slots=True)
class ObservablePlayerPosition:
    position: int
    bought_quantity: int
    sold_quantity: int

    def __post_init__(self) -> None:
        if (
            type(self.position) is not int
            or type(self.bought_quantity) is not int
            or type(self.sold_quantity) is not int
            or min(self.bought_quantity, self.sold_quantity) < 0
            or self.position != self.bought_quantity - self.sold_quantity
        ):
            raise ValueError("observable player position does not conserve")

    def as_dict(self) -> dict[str, int]:
        return {
            "bought_quantity": self.bought_quantity,
            "position": self.position,
            "sold_quantity": self.sold_quantity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ObservablePlayerPosition:
        _require_exact_fields(
            payload,
            {"bought_quantity", "position", "sold_quantity"},
            "observable player position",
        )
        return cls(
            _required_int(payload, "position"),
            _required_int(payload, "bought_quantity"),
            _required_int(payload, "sold_quantity"),
        )


@dataclass(frozen=True, slots=True)
class ObservableWorkingOrder:
    order_id: str
    owner: OrderOwner = OrderOwner.PLAYER
    order_type: OrderType = OrderType.LIMIT


@dataclass(frozen=True, slots=True)
class ObservableStrategyBook:
    """Book-shaped strategy input containing public depth and exact own state."""

    depth: ObservableDepthBook
    player_position: ObservablePlayerPosition
    own_orders: tuple[OwnOrderState, ...]

    @property
    def bids(self) -> dict[int, ObservablePriceLevel]:
        return self.depth.bids

    @property
    def asks(self) -> dict[int, ObservablePriceLevel]:
        return self.depth.asks

    @property
    def bid_prices(self) -> list[int]:
        return self.depth.bid_prices

    @property
    def ask_prices(self) -> list[int]:
        return self.depth.ask_prices

    @property
    def best_bid(self) -> int | None:
        return self.depth.best_bid

    @property
    def best_ask(self) -> int | None:
        return self.depth.best_ask

    @property
    def active_orders(self) -> dict[str, ObservableWorkingOrder]:
        return {
            order.order_id: ObservableWorkingOrder(order.order_id)
            for order in self.own_orders
            if order.remaining_quantity > 0
            and order.status in {"WORKING", "PARTIALLY_FILLED"}
        }


@dataclass(frozen=True, slots=True)
class ObservableEvent:
    sequence: int
    source_time_us: int
    received_time_us: int
    event_type: ObservableEventType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("observable event data must be a JSON object")
        object.__setattr__(self, "data", frozen)
        if (
            type(self.sequence) is not int
            or self.sequence <= 0
            or type(self.source_time_us) is not int
            or type(self.received_time_us) is not int
            or self.source_time_us < 0
            or self.received_time_us < self.source_time_us
        ):
            raise ValueError("observable event identity or causal time is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type.value,
            "received_time_us": self.received_time_us,
            "sequence": self.sequence,
            "source_time_us": self.source_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ObservableEvent:
        _require_exact_fields(
            payload,
            {
                "data",
                "event_type",
                "received_time_us",
                "sequence",
                "source_time_us",
            },
            "observable event",
        )
        data = payload["data"]
        if type(data) is not dict:
            raise TypeError("observable event data must be an object")
        return cls(
            _required_int(payload, "sequence"),
            _required_int(payload, "source_time_us"),
            _required_int(payload, "received_time_us"),
            ObservableEventType(_required_string(payload, "event_type")),
            data,
        )


@dataclass(frozen=True, slots=True)
class ObservableMarketFeed:
    simulation_time_us: int
    book: ObservableDepthBook
    tape: tuple[PublicTrade, ...]
    events: tuple[ObservableEvent, ...]
    own_orders: tuple[OwnOrderState, ...]
    player_position: ObservablePlayerPosition
    strategy_events: tuple[SimulationEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("observable feed time must be nonnegative microseconds")
        event_sequences = tuple(event.sequence for event in self.events)
        strategy_sequences = tuple(event.sequence for event in self.strategy_events)
        if event_sequences and event_sequences != tuple(
            range(event_sequences[0], event_sequences[-1] + 1)
        ):
            raise ValueError("observable feed event slice is not contiguous")
        if strategy_sequences and strategy_sequences != tuple(
            range(strategy_sequences[0], strategy_sequences[-1] + 1)
        ):
            raise ValueError("observable strategy event slice is not contiguous")

    @property
    def strategy_book(self) -> ObservableStrategyBook:
        return ObservableStrategyBook(
            self.book,
            self.player_position,
            self.own_orders,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "book": self.book.as_dict(),
            "events": [event.as_dict() for event in self.events],
            "own_orders": [order.as_dict() for order in self.own_orders],
            "player_position": self.player_position.as_dict(),
            "representation": "OBSERVABLE_MARKET_FEED",
            "simulation_time_us": self.simulation_time_us,
            "strategy_events": [event.as_dict() for event in self.strategy_events],
            "tape": [trade.as_dict() for trade in self.tape],
        }

    def sha256(self) -> str:
        return _sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class TruthEvent:
    sequence: int
    simulation_time_us: int
    event_type: TruthEventType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("truth event data must be a JSON object")
        object.__setattr__(self, "data", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TruthEvent:
        _require_exact_fields(
            payload,
            {"data", "event_type", "sequence", "simulation_time_us"},
            "truth event",
        )
        data = payload["data"]
        if type(data) is not dict:
            raise TypeError("truth event data must be an object")
        return cls(
            _required_int(payload, "sequence"),
            _required_int(payload, "simulation_time_us"),
            TruthEventType(_required_string(payload, "event_type")),
            data,
        )


@dataclass(frozen=True, slots=True)
class GroundTruthOrderState:
    order_id: str
    side: Side
    kind: LiquidityKind
    owner: OrderOwner
    price_ticks: int | None
    original_quantity: int
    displayed_quantity: int
    reserve_quantity: int
    hidden_quantity: int
    filled_quantity: int
    cancelled_quantity: int
    priority_sequence: int
    status: str

    @property
    def remaining_quantity(self) -> int:
        return (
            self.original_quantity
            - self.filled_quantity
            - self.cancelled_quantity
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "cancelled_quantity": self.cancelled_quantity,
            "displayed_quantity": self.displayed_quantity,
            "filled_quantity": self.filled_quantity,
            "hidden_quantity": self.hidden_quantity,
            "kind": self.kind.value,
            "order_id": self.order_id,
            "original_quantity": self.original_quantity,
            "owner": self.owner.value,
            "price_ticks": self.price_ticks,
            "priority_sequence": self.priority_sequence,
            "remaining_quantity": self.remaining_quantity,
            "reserve_quantity": self.reserve_quantity,
            "side": self.side.value,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class GroundTruthExchangeState:
    simulation_time_us: int
    orders: tuple[GroundTruthOrderState, ...]
    player_position: int
    events: tuple[TruthEvent, ...]
    label: str = "SIMULATOR_GROUND_TRUTH_POST_SESSION"

    def as_dict(self) -> dict[str, object]:
        return {
            "events": [event.as_dict() for event in self.events],
            "label": self.label,
            "orders": [order.as_dict() for order in self.orders],
            "player_position": self.player_position,
            "representation": "GROUND_TRUTH_EXCHANGE_STATE",
            "simulation_time_us": self.simulation_time_us,
        }

    def sha256(self) -> str:
        return _sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class QueuePositionEstimate:
    order_id: str
    estimated_quantity_ahead: int
    lower_bound: int
    upper_bound: int
    confidence: Decimal
    last_update_time_us: int
    assumptions: tuple[str, ...]
    evidence_mode: QueueDataMode
    is_exact: bool

    def __post_init__(self) -> None:
        if (
            not self.order_id
            or type(self.last_update_time_us) is not int
            or self.last_update_time_us < 0
            or not self.assumptions
            or not isinstance(self.evidence_mode, QueueDataMode)
        ):
            raise ValueError("queue estimate identity, time, assumptions, or mode is invalid")
        if not (
            0 <= self.lower_bound
            <= self.estimated_quantity_ahead
            <= self.upper_bound
        ):
            raise ValueError("queue estimate is outside its bounds")
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("queue estimate confidence must be in [0,1]")
        if self.evidence_mode is QueueDataMode.AGGREGATED_DEPTH and self.is_exact:
            raise ValueError("aggregated depth cannot claim exact opponent queue")

    def as_dict(self) -> dict[str, object]:
        return {
            "assumptions": list(self.assumptions),
            "confidence": str(self.confidence),
            "estimated_quantity_ahead": self.estimated_quantity_ahead,
            "evidence_mode": self.evidence_mode.value,
            "is_exact": self.is_exact,
            "last_update_time_us": self.last_update_time_us,
            "lower_bound": self.lower_bound,
            "order_id": self.order_id,
            "upper_bound": self.upper_bound,
        }


@dataclass(frozen=True, slots=True)
class ObservabilityScore:
    target_quantity: int
    completed_quantity: int
    observable_liquidity_at_decisions: int
    missed_observable_liquidity: int
    revealed_hidden_liquidity: int
    hidden_liquidity_penalty: int = 0
    hidden_liquidity_scoring_status: str = "NOT_SCORED_UNOBSERVABLE"

    def as_dict(self) -> dict[str, object]:
        return {
            "completed_quantity": self.completed_quantity,
            "hidden_liquidity_penalty": self.hidden_liquidity_penalty,
            "hidden_liquidity_scoring_status": (
                self.hidden_liquidity_scoring_status
            ),
            "missed_observable_liquidity": self.missed_observable_liquidity,
            "observable_liquidity_at_decisions": (
                self.observable_liquidity_at_decisions
            ),
            "revealed_hidden_liquidity": self.revealed_hidden_liquidity,
            "target_quantity": self.target_quantity,
        }


def _sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be boolean")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("optional integer field is invalid")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str or not value:
        raise TypeError(f"{key} must be a nonempty string")
    return value


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _object_array(value: object, label: str) -> tuple[dict[str, object], ...]:
    if type(value) is not list or any(type(row) is not dict for row in value):
        raise TypeError(f"{label} must be an object array")
    return tuple(value)
