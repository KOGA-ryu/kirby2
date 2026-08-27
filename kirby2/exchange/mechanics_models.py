"""Generic configurable market-mechanics contracts for advanced exchange sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json

from .models import OrderOwner, Side


MECHANICS_RECORDING_SCHEMA_VERSION = 1


class OrderInstruction(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "POST_ONLY"
    GTC = "GTC"
    DAY = "DAY"
    SESSION = "SESSION"
    GOOD_UNTIL_TIME = "GOOD_UNTIL_TIME"


PRIMARY_INSTRUCTIONS = frozenset(
    {
        OrderInstruction.LIMIT,
        OrderInstruction.MARKET,
        OrderInstruction.MARKETABLE_LIMIT,
    }
)
TIME_IN_FORCE_INSTRUCTIONS = frozenset(
    {
        OrderInstruction.IOC,
        OrderInstruction.FOK,
        OrderInstruction.GTC,
        OrderInstruction.DAY,
        OrderInstruction.SESSION,
        OrderInstruction.GOOD_UNTIL_TIME,
    }
)


class SessionState(str, Enum):
    CLOSED = "CLOSED"
    PREOPEN = "PREOPEN"
    OPENING_AUCTION = "OPENING_AUCTION"
    CONTINUOUS = "CONTINUOUS"
    HALTED = "HALTED"
    REOPENING_AUCTION = "REOPENING_AUCTION"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    POSTCLOSE = "POSTCLOSE"


class SelfTradePreventionMode(str, Enum):
    NONE = "NONE"
    CANCEL_AGGRESSOR = "CANCEL_AGGRESSOR"
    CANCEL_RESTING = "CANCEL_RESTING"
    CANCEL_BOTH = "CANCEL_BOTH"


class MechanicsEventType(str, Enum):
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_REPLACED = "ORDER_REPLACED"
    PRIORITY_PRESERVED = "PRIORITY_PRESERVED"
    PRIORITY_LOST = "PRIORITY_LOST"
    SELF_TRADE_PREVENTION = "SELF_TRADE_PREVENTION"
    TRADE = "TRADE"
    SESSION_STATE_CHANGED = "SESSION_STATE_CHANGED"
    AUCTION_ORDER_ADDED = "AUCTION_ORDER_ADDED"
    AUCTION_ORDER_CANCELLED = "AUCTION_ORDER_CANCELLED"
    AUCTION_INDICATION = "AUCTION_INDICATION"
    AUCTION_UNCROSS = "AUCTION_UNCROSS"
    AUCTION_FILL = "AUCTION_FILL"
    PROTECTION_TRIGGERED = "PROTECTION_TRIGGERED"
    HALT = "HALT"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class ScheduledSessionState:
    simulation_time_us: int
    state: SessionState

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("scheduled session time must be nonnegative microseconds")
        if not isinstance(self.state, SessionState):
            raise TypeError("scheduled session state must use the canonical enum")

    def as_dict(self) -> dict[str, object]:
        return {
            "simulation_time_us": self.simulation_time_us,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class SessionSchedule:
    transitions: tuple[ScheduledSessionState, ...] = ()

    def __post_init__(self) -> None:
        times = tuple(item.simulation_time_us for item in self.transitions)
        if times != tuple(sorted(times)) or len(times) != len(set(times)):
            raise ValueError("session schedule times must be strictly increasing")

    def as_dict(self) -> dict[str, object]:
        return {"transitions": [item.as_dict() for item in self.transitions]}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> SessionSchedule:
        raw = payload.get("transitions", [])
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("session schedule transitions are invalid")
        return cls(
            tuple(
                ScheduledSessionState(
                    int(item["simulation_time_us"]),
                    SessionState(str(item["state"])),
                )
                for item in raw
            )
        )


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    tick_size: Decimal = Decimal("0.01")
    lot_size: int = 1
    minimum_quantity: int = 1
    maximum_quantity: int = 1_000_000
    lower_price_band_ticks: int = 1
    upper_price_band_ticks: int = 1_000_000_000
    supported_order_instructions: frozenset[OrderInstruction] = field(
        default_factory=lambda: frozenset(OrderInstruction)
    )
    session_schedule: SessionSchedule = field(default_factory=SessionSchedule)
    preserve_priority_on_quantity_reduction: bool = True
    reference_price_ticks: int = 100
    price_collar_ticks: int | None = None
    volatility_interruption_ticks: int | None = None
    fat_finger_ticks: int | None = None
    account_stp_modes: tuple[tuple[str, SelfTradePreventionMode], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tick_size, Decimal)
            or not self.tick_size.is_finite()
            or self.tick_size <= 0
        ):
            raise ValueError("instrument tick size must be finite and positive")
        quantities = (
            self.lot_size,
            self.minimum_quantity,
            self.maximum_quantity,
        )
        if any(type(value) is not int or value <= 0 for value in quantities):
            raise ValueError("instrument quantity rules must be positive integers")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("instrument minimum quantity exceeds maximum")
        if self.minimum_quantity % self.lot_size:
            raise ValueError("instrument minimum quantity must align to lot size")
        if self.maximum_quantity % self.lot_size:
            raise ValueError("instrument maximum quantity must align to lot size")
        if (
            type(self.lower_price_band_ticks) is not int
            or type(self.upper_price_band_ticks) is not int
            or self.lower_price_band_ticks <= 0
            or self.upper_price_band_ticks < self.lower_price_band_ticks
        ):
            raise ValueError("instrument price bands are invalid")
        if not (
            type(self.reference_price_ticks) is int
            and self.lower_price_band_ticks
            <= self.reference_price_ticks
            <= self.upper_price_band_ticks
        ):
            raise ValueError("instrument reference price is outside its price band")
        if type(self.preserve_priority_on_quantity_reduction) is not bool:
            raise TypeError("priority-reduction rule must be boolean")
        for name, value in (
            ("price collar", self.price_collar_ticks),
            ("volatility interruption", self.volatility_interruption_ticks),
            ("fat-finger distance", self.fat_finger_ticks),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"instrument {name} must be positive ticks")
        if not self.supported_order_instructions:
            raise ValueError("instrument must support at least one order instruction")
        if any(
            not isinstance(item, OrderInstruction)
            for item in self.supported_order_instructions
        ):
            raise TypeError("supported order instructions must use the canonical enum")
        account_ids = tuple(account_id for account_id, _mode in self.account_stp_modes)
        if any(not account_id for account_id in account_ids):
            raise ValueError("self-trade prevention account ID cannot be empty")
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("self-trade prevention account IDs must be unique")
        if any(
            not isinstance(mode, SelfTradePreventionMode)
            for _account_id, mode in self.account_stp_modes
        ):
            raise TypeError("self-trade prevention modes must use the canonical enum")

    def stp_mode(self, account_id: str) -> SelfTradePreventionMode:
        return dict(self.account_stp_modes).get(
            account_id,
            SelfTradePreventionMode.NONE,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "account_stp_modes": [
                {"account_id": account_id, "mode": mode.value}
                for account_id, mode in self.account_stp_modes
            ],
            "fat_finger_ticks": self.fat_finger_ticks,
            "lot_size": self.lot_size,
            "lower_price_band_ticks": self.lower_price_band_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_quantity": self.minimum_quantity,
            "preserve_priority_on_quantity_reduction": (
                self.preserve_priority_on_quantity_reduction
            ),
            "price_collar_ticks": self.price_collar_ticks,
            "reference_price_ticks": self.reference_price_ticks,
            "session_schedule": self.session_schedule.as_dict(),
            "supported_order_instructions": sorted(
                item.value for item in self.supported_order_instructions
            ),
            "tick_size": str(self.tick_size),
            "upper_price_band_ticks": self.upper_price_band_ticks,
            "volatility_interruption_ticks": (
                self.volatility_interruption_ticks
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> InstrumentRules:
        raw_supported = payload.get("supported_order_instructions")
        raw_schedule = payload.get("session_schedule")
        raw_stp = payload.get("account_stp_modes", [])
        if (
            not isinstance(raw_supported, list)
            or not isinstance(raw_schedule, dict)
            or not isinstance(raw_stp, list)
            or any(not isinstance(item, dict) for item in raw_stp)
        ):
            raise ValueError("instrument rule arrays are invalid")
        if len(raw_supported) != len(set(map(str, raw_supported))):
            raise ValueError("supported order instructions must be unique")
        return cls(
            tick_size=Decimal(str(payload["tick_size"])),
            lot_size=int(payload["lot_size"]),
            minimum_quantity=int(payload["minimum_quantity"]),
            maximum_quantity=int(payload["maximum_quantity"]),
            lower_price_band_ticks=int(payload["lower_price_band_ticks"]),
            upper_price_band_ticks=int(payload["upper_price_band_ticks"]),
            supported_order_instructions=frozenset(
                OrderInstruction(str(item)) for item in raw_supported
            ),
            session_schedule=SessionSchedule.from_dict(raw_schedule),
            preserve_priority_on_quantity_reduction=(
                payload.get("preserve_priority_on_quantity_reduction") is True
            ),
            reference_price_ticks=int(payload["reference_price_ticks"]),
            price_collar_ticks=_optional_int(payload.get("price_collar_ticks")),
            volatility_interruption_ticks=_optional_int(
                payload.get("volatility_interruption_ticks")
            ),
            fat_finger_ticks=_optional_int(payload.get("fat_finger_ticks")),
            account_stp_modes=tuple(
                (
                    str(item["account_id"]),
                    SelfTradePreventionMode(str(item["mode"])),
                )
                for item in raw_stp
            ),
        )


@dataclass(frozen=True, slots=True)
class AdvancedOrderRequest:
    order_id: str
    side: Side
    quantity: int
    instruction: OrderInstruction
    owner: OrderOwner
    account_id: str
    price_ticks: int | None = None
    time_in_force: OrderInstruction = OrderInstruction.DAY
    modifiers: frozenset[OrderInstruction] = frozenset()
    good_until_time_us: int | None = None
    auction_only: bool = False

    def __post_init__(self) -> None:
        if not self.order_id or not self.account_id:
            raise ValueError("advanced order identity and account are required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("advanced order quantity must be positive")
        if not isinstance(self.side, Side) or not isinstance(self.owner, OrderOwner):
            raise TypeError("advanced order side and owner must use canonical enums")
        if not isinstance(self.instruction, OrderInstruction) or not isinstance(
            self.time_in_force,
            OrderInstruction,
        ):
            raise TypeError("advanced order instructions must use the canonical enum")
        if self.instruction not in PRIMARY_INSTRUCTIONS:
            raise ValueError("advanced order primary instruction is invalid")
        if self.time_in_force not in TIME_IN_FORCE_INSTRUCTIONS:
            raise ValueError("advanced order time in force is invalid")
        if self.modifiers - {OrderInstruction.POST_ONLY}:
            raise ValueError("advanced order modifier is unsupported")
        if self.instruction is OrderInstruction.MARKET:
            if self.price_ticks is not None:
                raise ValueError("market order cannot carry a price")
            if OrderInstruction.POST_ONLY in self.modifiers:
                raise ValueError("market order cannot be post-only")
        elif type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("priced advanced order requires positive integer ticks")
        if self.time_in_force is OrderInstruction.GOOD_UNTIL_TIME:
            if type(self.good_until_time_us) is not int or self.good_until_time_us < 0:
                raise ValueError("good-until-time order requires nonnegative expiry")
        elif self.good_until_time_us is not None:
            raise ValueError("expiry timestamp requires GOOD_UNTIL_TIME")
        if type(self.auction_only) is not bool:
            raise TypeError("auction-only flag must be boolean")

    @property
    def post_only(self) -> bool:
        return OrderInstruction.POST_ONLY in self.modifiers

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "auction_only": self.auction_only,
            "good_until_time_us": self.good_until_time_us,
            "instruction": self.instruction.value,
            "modifiers": sorted(item.value for item in self.modifiers),
            "order_id": self.order_id,
            "owner": self.owner.value,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "side": self.side.value,
            "time_in_force": self.time_in_force.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> AdvancedOrderRequest:
        raw_modifiers = payload.get("modifiers", [])
        if not isinstance(raw_modifiers, list):
            raise ValueError("advanced order modifiers are invalid")
        return cls(
            order_id=str(payload["order_id"]),
            side=Side(str(payload["side"])),
            quantity=int(payload["quantity"]),
            instruction=OrderInstruction(str(payload["instruction"])),
            owner=OrderOwner(str(payload["owner"])),
            account_id=str(payload["account_id"]),
            price_ticks=_optional_int(payload.get("price_ticks")),
            time_in_force=OrderInstruction(str(payload["time_in_force"])),
            modifiers=frozenset(
                OrderInstruction(str(item)) for item in raw_modifiers
            ),
            good_until_time_us=_optional_int(payload.get("good_until_time_us")),
            auction_only=payload.get("auction_only") is True,
        )


@dataclass(slots=True)
class ManagedOrder:
    request: AdvancedOrderRequest
    arrival_sequence: int
    status: str = "PENDING"
    filled_quantity: int = 0
    cancelled_quantity: int = 0
    expired_quantity: int = 0
    resting_sequence: int | None = None

    @property
    def remaining_quantity(self) -> int:
        return (
            self.request.quantity
            - self.filled_quantity
            - self.cancelled_quantity
            - self.expired_quantity
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "arrival_sequence": self.arrival_sequence,
            "cancelled_quantity": self.cancelled_quantity,
            "expired_quantity": self.expired_quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "request": self.request.as_dict(),
            "resting_sequence": self.resting_sequence,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class AuctionIndication:
    clearing_price_ticks: int | None
    matched_quantity: int
    imbalance_quantity: int
    imbalance_side: Side | None

    def as_dict(self) -> dict[str, object]:
        return {
            "clearing_price_ticks": self.clearing_price_ticks,
            "imbalance_quantity": self.imbalance_quantity,
            "imbalance_side": (
                None if self.imbalance_side is None else self.imbalance_side.value
            ),
            "matched_quantity": self.matched_quantity,
        }


@dataclass(frozen=True, slots=True)
class MechanicsEvent:
    sequence: int
    simulation_time_us: int
    event_type: MechanicsEventType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("market-mechanics event data must be a JSON object")
        object.__setattr__(self, "data", frozen)
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("market-mechanics event identity is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
