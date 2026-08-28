"""Immutable contracts for fragmented synthetic markets and baseline routing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from kirby2.exchange import OrderInstruction, SessionState
from kirby2.exchange.models import Side
from kirby2.immutable import freeze_json, thaw_json
from kirby2.latency import LatencyComponent, LatencyProfile
from kirby2.observability import HiddenLiquidityRules, ObservableDepthBook


MULTIVENUE_RECORDING_SCHEMA_VERSION = 1


class RoutePolicy(str, Enum):
    DIRECT = "DIRECT"
    BEST_DISPLAYED_PRICE = "BEST_DISPLAYED_PRICE"
    LOWEST_EXPECTED_COST = "LOWEST_EXPECTED_COST"
    PASSIVE_QUEUE = "PASSIVE_QUEUE"
    SWEEP = "SWEEP"
    LATENCY_AWARE = "LATENCY_AWARE"


class RouteStyle(str, Enum):
    AGGRESSIVE = "AGGRESSIVE"
    PASSIVE = "PASSIVE"


class VenueOrderStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    RESTING = "RESTING"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ALREADY_CLOSED = "ALREADY_CLOSED"


class CoordinatorEventType(str, Enum):
    VENUE_ORDER_ADDED = "VENUE_ORDER_ADDED"
    VENUE_MARKET_FLOW = "VENUE_MARKET_FLOW"
    ROUTE_DECISION = "ROUTE_DECISION"
    ROUTE_LEG_SCHEDULED = "ROUTE_LEG_SCHEDULED"
    ROUTE_LEG_ACCEPTED = "ROUTE_LEG_ACCEPTED"
    ROUTE_LEG_REJECTED = "ROUTE_LEG_REJECTED"
    ROUTE_LEG_FILL = "ROUTE_LEG_FILL"
    GLOBAL_POSITION_CHANGED = "GLOBAL_POSITION_CHANGED"
    CANCEL_ALL_REQUESTED = "CANCEL_ALL_REQUESTED"
    VENUE_ORDER_CANCELLED = "VENUE_ORDER_CANCELLED"
    VENUE_SESSION_CHANGED = "VENUE_SESSION_CHANGED"
    SESSION_COMPLETE = "SESSION_COMPLETE"


@dataclass(frozen=True, slots=True)
class VenueFeeSchedule:
    """Integer-micro monetary schedule; positive values are charges/rebates."""

    taker_fee_micros_per_share: int = 30
    maker_rebate_micros_per_share: int = 10
    tick_value_micros: int = 10_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.taker_fee_micros_per_share,
                self.maker_rebate_micros_per_share,
            )
        ):
            raise ValueError("venue fees and rebates must be nonnegative integer micros")
        if (
            type(self.tick_value_micros) is not int
            or self.tick_value_micros <= 0
            or self.tick_value_micros % 2
        ):
            raise ValueError("tick value must be a positive even integer-micro amount")

    def as_dict(self) -> dict[str, int]:
        return {
            "maker_rebate_micros_per_share": self.maker_rebate_micros_per_share,
            "taker_fee_micros_per_share": self.taker_fee_micros_per_share,
            "tick_value_micros": self.tick_value_micros,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> VenueFeeSchedule:
        return cls(
            int(payload["taker_fee_micros_per_share"]),
            int(payload["maker_rebate_micros_per_share"]),
            int(payload["tick_value_micros"]),
        )


@dataclass(frozen=True, slots=True)
class VenueConfig:
    venue_id: str
    latency_profile: LatencyProfile
    fees: VenueFeeSchedule = VenueFeeSchedule()
    supported_instructions: frozenset[OrderInstruction] = field(
        default_factory=lambda: frozenset(
            {OrderInstruction.LIMIT, OrderInstruction.MARKET}
        )
    )
    hidden_rules: HiddenLiquidityRules = HiddenLiquidityRules()
    session_state: SessionState = SessionState.CONTINUOUS
    expected_fill_probability_bps: int = 8_000

    def __post_init__(self) -> None:
        if not self.venue_id:
            raise ValueError("venue ID is required")
        if not isinstance(self.latency_profile, LatencyProfile):
            raise TypeError("venue latency must use LatencyProfile")
        if not self.supported_instructions or any(
            not isinstance(value, OrderInstruction)
            for value in self.supported_instructions
        ):
            raise ValueError("venue supported instructions are invalid")
        if not isinstance(self.session_state, SessionState):
            raise TypeError("venue session state must use SessionState")
        if (
            type(self.expected_fill_probability_bps) is not int
            or not 0 <= self.expected_fill_probability_bps <= 10_000
        ):
            raise ValueError("expected fill probability must be 0..10000 basis points")

    @property
    def expected_routing_latency_us(self) -> int:
        components = (
            LatencyComponent.CLIENT_ROUTING,
            LatencyComponent.UPLINK,
            LatencyComponent.GATEWAY,
            LatencyComponent.VENUE_PROCESSING,
        )
        return sum(
            (
                self.latency_profile.distribution(component).lower_us
                + self.latency_profile.distribution(component).upper_us
            )
            // 2
            for component in components
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_fill_probability_bps": self.expected_fill_probability_bps,
            "fees": self.fees.as_dict(),
            "hidden_rules": self.hidden_rules.as_dict(),
            "latency_profile": self.latency_profile.as_dict(),
            "session_state": self.session_state.value,
            "supported_instructions": sorted(
                value.value for value in self.supported_instructions
            ),
            "venue_id": self.venue_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> VenueConfig:
        raw_fees = payload.get("fees")
        raw_rules = payload.get("hidden_rules")
        raw_profile = payload.get("latency_profile")
        raw_supported = payload.get("supported_instructions")
        if not all(isinstance(value, dict) for value in (raw_fees, raw_rules, raw_profile)):
            raise ValueError("venue configuration objects are missing")
        if not isinstance(raw_supported, list):
            raise ValueError("venue supported instructions are missing")
        return cls(
            venue_id=str(payload["venue_id"]),
            latency_profile=LatencyProfile.from_dict(raw_profile),
            fees=VenueFeeSchedule.from_dict(raw_fees),
            supported_instructions=frozenset(
                OrderInstruction(str(value)) for value in raw_supported
            ),
            hidden_rules=HiddenLiquidityRules.from_dict(raw_rules),
            session_state=SessionState(str(payload["session_state"])),
            expected_fill_probability_bps=int(
                payload["expected_fill_probability_bps"]
            ),
        )


@dataclass(frozen=True, slots=True)
class VenueQuote:
    venue_id: str
    best_bid_ticks: int | None
    best_bid_quantity: int
    best_ask_ticks: int | None
    best_ask_quantity: int
    quote_source_time_us: int
    quote_received_time_us: int
    quote_age_us: int
    expected_routing_latency_us: int
    expected_fill_probability_bps: int
    taker_fee_micros_per_share: int
    maker_rebate_micros_per_share: int
    tick_value_micros: int
    supported_instructions: tuple[str, ...]
    session_state: SessionState

    def __post_init__(self) -> None:
        if not self.venue_id or not isinstance(self.session_state, SessionState):
            raise ValueError("venue quote identity or state is invalid")
        if min(
            self.best_bid_quantity,
            self.best_ask_quantity,
            self.quote_source_time_us,
            self.quote_received_time_us,
            self.quote_age_us,
            self.expected_routing_latency_us,
            self.expected_fill_probability_bps,
            self.taker_fee_micros_per_share,
            self.maker_rebate_micros_per_share,
            self.tick_value_micros,
        ) < 0:
            raise ValueError("venue quote quantities, times, and costs cannot be negative")
        if self.quote_received_time_us < self.quote_source_time_us:
            raise ValueError("venue quote received before its source time")

    def displayed_price(self, side: Side, style: RouteStyle) -> int | None:
        if style is RouteStyle.AGGRESSIVE:
            return self.best_ask_ticks if side is Side.BUY else self.best_bid_ticks
        return self.best_bid_ticks if side is Side.BUY else self.best_ask_ticks

    def displayed_quantity(self, side: Side, style: RouteStyle) -> int:
        if style is RouteStyle.AGGRESSIVE:
            return self.best_ask_quantity if side is Side.BUY else self.best_bid_quantity
        return self.best_bid_quantity if side is Side.BUY else self.best_ask_quantity

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_quantity": self.best_ask_quantity,
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_quantity": self.best_bid_quantity,
            "best_bid_ticks": self.best_bid_ticks,
            "expected_fill_probability_bps": self.expected_fill_probability_bps,
            "expected_routing_latency_us": self.expected_routing_latency_us,
            "maker_rebate_micros_per_share": self.maker_rebate_micros_per_share,
            "quote_age_us": self.quote_age_us,
            "quote_received_time_us": self.quote_received_time_us,
            "quote_source_time_us": self.quote_source_time_us,
            "session_state": self.session_state.value,
            "supported_instructions": list(self.supported_instructions),
            "taker_fee_micros_per_share": self.taker_fee_micros_per_share,
            "tick_value_micros": self.tick_value_micros,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class VenueDepth:
    venue_id: str
    book: ObservableDepthBook

    def as_dict(self) -> dict[str, object]:
        return {"book": self.book.as_dict(), "venue_id": self.venue_id}


@dataclass(frozen=True, slots=True)
class ConsolidatedTrade:
    venue_id: str
    trade_id: str
    source_time_us: int
    received_time_us: int
    price_x2: int
    quantity: int
    aggressor_side: Side

    def as_dict(self) -> dict[str, object]:
        return {
            "aggressor_side": self.aggressor_side.value,
            "price_x2": self.price_x2,
            "quantity": self.quantity,
            "received_time_us": self.received_time_us,
            "source_time_us": self.source_time_us,
            "trade_id": self.trade_id,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class ConsolidatedFeed:
    simulation_time_us: int
    quotes: tuple[VenueQuote, ...]
    trades: tuple[ConsolidatedTrade, ...]
    subscribed_depth: tuple[VenueDepth, ...]

    @property
    def best_bid_ticks(self) -> int | None:
        prices = [q.best_bid_ticks for q in self.quotes if q.best_bid_ticks is not None]
        return None if not prices else max(prices)

    @property
    def best_ask_ticks(self) -> int | None:
        prices = [q.best_ask_ticks for q in self.quotes if q.best_ask_ticks is not None]
        return None if not prices else min(prices)

    @property
    def best_bid_venues(self) -> tuple[str, ...]:
        best = self.best_bid_ticks
        return tuple(q.venue_id for q in self.quotes if q.best_bid_ticks == best)

    @property
    def best_ask_venues(self) -> tuple[str, ...]:
        best = self.best_ask_ticks
        return tuple(q.venue_id for q in self.quotes if q.best_ask_ticks == best)

    @property
    def composite_state(self) -> str:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return "ONE_SIDED"
        if self.best_bid_ticks > self.best_ask_ticks:
            return "CROSSED"
        if self.best_bid_ticks == self.best_ask_ticks:
            return "LOCKED"
        return "NORMAL"

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_ask_venues": list(self.best_ask_venues),
            "best_bid_ticks": self.best_bid_ticks,
            "best_bid_venues": list(self.best_bid_venues),
            "composite_state": self.composite_state,
            "quotes": [quote.as_dict() for quote in self.quotes],
            "representation": "CONSOLIDATED_OBSERVABLE_FEED",
            "simulation_time_us": self.simulation_time_us,
            "subscribed_depth": [depth.as_dict() for depth in self.subscribed_depth],
            "trades": [trade.as_dict() for trade in self.trades],
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    order_id: str
    side: Side
    quantity: int
    policy: RoutePolicy
    style: RouteStyle = RouteStyle.AGGRESSIVE
    direct_venue_id: str | None = None
    limit_price_ticks: int | None = None
    max_venues: int = 1

    def __post_init__(self) -> None:
        if not self.order_id or not isinstance(self.side, Side):
            raise ValueError("routing order identity or side is invalid")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("routing quantity must be positive")
        if not isinstance(self.policy, RoutePolicy) or not isinstance(self.style, RouteStyle):
            raise TypeError("routing policy and style must use canonical enums")
        if self.policy is RoutePolicy.DIRECT and not self.direct_venue_id:
            raise ValueError("DIRECT routing requires a venue ID")
        if self.limit_price_ticks is not None and (
            type(self.limit_price_ticks) is not int or self.limit_price_ticks <= 0
        ):
            raise ValueError("route limit price must be positive integer ticks")
        if type(self.max_venues) is not int or self.max_venues <= 0:
            raise ValueError("route max venues must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "direct_venue_id": self.direct_venue_id,
            "limit_price_ticks": self.limit_price_ticks,
            "max_venues": self.max_venues,
            "order_id": self.order_id,
            "policy": self.policy.value,
            "quantity": self.quantity,
            "side": self.side.value,
            "style": self.style.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> RoutingRequest:
        raw_price = payload.get("limit_price_ticks")
        raw_venue = payload.get("direct_venue_id")
        return cls(
            str(payload["order_id"]),
            Side(str(payload["side"])),
            int(payload["quantity"]),
            RoutePolicy(str(payload["policy"])),
            RouteStyle(str(payload["style"])),
            None if raw_venue is None else str(raw_venue),
            None if raw_price is None else int(raw_price),
            int(payload["max_venues"]),
        )


@dataclass(frozen=True, slots=True)
class RouteLegPlan:
    venue_id: str
    quantity: int
    reference_price_ticks: int | None
    observed_quote_age_us: int
    rationale: str

    def __post_init__(self) -> None:
        if not self.venue_id or not self.rationale:
            raise ValueError("route leg identity and rationale are required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("route leg quantity must be positive")
        if self.reference_price_ticks is not None and (
            type(self.reference_price_ticks) is not int
            or self.reference_price_ticks <= 0
        ):
            raise ValueError("route leg reference price must be positive ticks")
        if type(self.observed_quote_age_us) is not int or self.observed_quote_age_us < 0:
            raise ValueError("route leg quote age must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_quote_age_us": self.observed_quote_age_us,
            "quantity": self.quantity,
            "rationale": self.rationale,
            "reference_price_ticks": self.reference_price_ticks,
            "venue_id": self.venue_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RouteLegPlan:
        _require_model_fields(
            payload,
            {
                "observed_quote_age_us",
                "quantity",
                "rationale",
                "reference_price_ticks",
                "venue_id",
            },
            "route leg plan",
        )
        return cls(
            venue_id=_model_string(payload["venue_id"], "route leg venue ID"),
            quantity=_model_int(payload["quantity"], "route leg quantity", minimum=1),
            reference_price_ticks=_model_optional_int(
                payload["reference_price_ticks"], "route leg reference price", minimum=1
            ),
            observed_quote_age_us=_model_int(
                payload["observed_quote_age_us"], "route leg quote age"
            ),
            rationale=_model_string(payload["rationale"], "route leg rationale"),
        )


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_id: str
    decision_time_us: int
    policy: RoutePolicy
    observable_feed_sha256: str
    observable_feed: dict[str, object]
    legs: tuple[RouteLegPlan, ...]
    explanation: str

    def __post_init__(self) -> None:
        if not self.route_id or type(self.decision_time_us) is not int or self.decision_time_us < 0:
            raise ValueError("route decision identity or time is invalid")
        if not isinstance(self.policy, RoutePolicy) or not self.explanation:
            raise ValueError("route decision policy or explanation is invalid")
        if type(self.observable_feed) is not dict:
            raise TypeError("route decision observable feed must be an object")
        if type(self.legs) is not tuple or any(
            type(leg) is not RouteLegPlan for leg in self.legs
        ):
            raise TypeError("route decision legs must be canonical route plans")
        if canonical_sha256(self.observable_feed) != self.observable_feed_sha256:
            raise ValueError("route decision evidence digest is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_time_us": self.decision_time_us,
            "explanation": self.explanation,
            "legs": [leg.as_dict() for leg in self.legs],
            "observable_feed": self.observable_feed,
            "observable_feed_sha256": self.observable_feed_sha256,
            "policy": self.policy.value,
            "route_id": self.route_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RouteDecision:
        _require_model_fields(
            payload,
            {
                "decision_time_us",
                "explanation",
                "legs",
                "observable_feed",
                "observable_feed_sha256",
                "policy",
                "route_id",
            },
            "route decision",
        )
        raw_feed = payload["observable_feed"]
        raw_legs = payload["legs"]
        if type(raw_feed) is not dict:
            raise TypeError("route decision feed must be an object")
        if type(raw_legs) is not list or any(type(row) is not dict for row in raw_legs):
            raise TypeError("route decision legs must be an object array")
        return cls(
            route_id=_model_string(payload["route_id"], "route ID"),
            decision_time_us=_model_int(payload["decision_time_us"], "decision time"),
            policy=RoutePolicy(_model_string(payload["policy"], "route policy")),
            observable_feed_sha256=_model_sha256(
                payload["observable_feed_sha256"], "observable feed digest"
            ),
            observable_feed=dict(raw_feed),
            legs=tuple(RouteLegPlan.from_dict(row) for row in raw_legs),
            explanation=_model_string(payload["explanation"], "route explanation"),
        )


@dataclass(frozen=True, slots=True)
class RouteLegExecution:
    leg_index: int
    venue_id: str
    order_id: str
    requested_quantity: int
    filled_quantity: int
    arrival_time_us: int
    routing_latency_us: int
    status: VenueOrderStatus
    rejection_reason: str | None
    stale_quote_exposure: bool

    def __post_init__(self) -> None:
        if type(self.leg_index) is not int or self.leg_index <= 0:
            raise ValueError("route execution leg index must be positive")
        if not self.venue_id or not self.order_id:
            raise ValueError("route execution venue and order IDs are required")
        if any(
            type(value) is not int or value < 0
            for value in (
                self.requested_quantity,
                self.filled_quantity,
                self.arrival_time_us,
                self.routing_latency_us,
            )
        ) or self.requested_quantity <= 0 or self.filled_quantity > self.requested_quantity:
            raise ValueError("route execution quantities or times are invalid")
        if not isinstance(self.status, VenueOrderStatus):
            raise TypeError("route execution status must use VenueOrderStatus")
        if self.rejection_reason is not None and (
            type(self.rejection_reason) is not str or not self.rejection_reason
        ):
            raise TypeError("route rejection reason must be null or nonempty")
        if type(self.stale_quote_exposure) is not bool:
            raise TypeError("route stale-quote exposure must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "arrival_time_us": self.arrival_time_us,
            "filled_quantity": self.filled_quantity,
            "leg_index": self.leg_index,
            "order_id": self.order_id,
            "rejection_reason": self.rejection_reason,
            "requested_quantity": self.requested_quantity,
            "routing_latency_us": self.routing_latency_us,
            "stale_quote_exposure": self.stale_quote_exposure,
            "status": self.status.value,
            "venue_id": self.venue_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RouteLegExecution:
        _require_model_fields(
            payload,
            {
                "arrival_time_us",
                "filled_quantity",
                "leg_index",
                "order_id",
                "rejection_reason",
                "requested_quantity",
                "routing_latency_us",
                "stale_quote_exposure",
                "status",
                "venue_id",
            },
            "route leg execution",
        )
        rejection = payload["rejection_reason"]
        stale = payload["stale_quote_exposure"]
        if rejection is not None and (type(rejection) is not str or not rejection):
            raise TypeError("route rejection reason must be null or nonempty")
        if type(stale) is not bool:
            raise TypeError("route stale-quote exposure must be boolean")
        return cls(
            leg_index=_model_int(payload["leg_index"], "route leg index", minimum=1),
            venue_id=_model_string(payload["venue_id"], "route venue ID"),
            order_id=_model_string(payload["order_id"], "route order ID"),
            requested_quantity=_model_int(
                payload["requested_quantity"], "route requested quantity", minimum=1
            ),
            filled_quantity=_model_int(payload["filled_quantity"], "route filled quantity"),
            arrival_time_us=_model_int(payload["arrival_time_us"], "route arrival time"),
            routing_latency_us=_model_int(
                payload["routing_latency_us"], "route latency"
            ),
            status=VenueOrderStatus(_model_string(payload["status"], "route status")),
            rejection_reason=rejection,
            stale_quote_exposure=stale,
        )


@dataclass(frozen=True, slots=True)
class RoutedOrderResult:
    request: RoutingRequest
    decision: RouteDecision
    executions: tuple[RouteLegExecution, ...]

    @property
    def completed_quantity(self) -> int:
        return sum(item.filled_quantity for item in self.executions)

    @property
    def remaining_quantity(self) -> int:
        return self.request.quantity - self.completed_quantity

    @property
    def complete(self) -> bool:
        return len(self.executions) == len(self.decision.legs)

    def as_dict(self) -> dict[str, object]:
        return {
            "completed_quantity": self.completed_quantity,
            "decision": self.decision.as_dict(),
            "executions": [item.as_dict() for item in self.executions],
            "remaining_quantity": self.remaining_quantity,
            "request": self.request.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ExecutionScore:
    route_id: str
    target_quantity: int
    completed_quantity: int
    gross_price_numerator_x2: int
    gross_price_denominator: int
    fees_micros: int
    rebates_micros: int
    net_execution_cost_micros: int
    routing_delay_us: int
    venue_selection_quality: str
    missed_better_displayed_ticks_x_quantity: int
    stale_quote_exposure_quantity: int

    def as_dict(self) -> dict[str, object]:
        return {
            "completed_quantity": self.completed_quantity,
            "fees_micros": self.fees_micros,
            "gross_price_denominator": self.gross_price_denominator,
            "gross_price_numerator_x2": self.gross_price_numerator_x2,
            "missed_better_displayed_ticks_x_quantity": self.missed_better_displayed_ticks_x_quantity,
            "net_execution_cost_micros": self.net_execution_cost_micros,
            "rebates_micros": self.rebates_micros,
            "route_id": self.route_id,
            "routing_delay_us": self.routing_delay_us,
            "stale_quote_exposure_quantity": self.stale_quote_exposure_quantity,
            "target_quantity": self.target_quantity,
            "venue_selection_quality": self.venue_selection_quality,
        }


@dataclass(frozen=True, slots=True)
class CoordinatorEvent:
    sequence: int
    simulation_time_us: int
    event_type: CoordinatorEventType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("coordinator event sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("coordinator event time must be nonnegative")
        if not isinstance(self.event_type, CoordinatorEventType):
            raise TypeError("coordinator event type must use CoordinatorEventType")
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("coordinator event data must be a JSON object")
        object.__setattr__(self, "data", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CoordinatorEvent:
        _require_model_fields(
            payload,
            {"data", "event_type", "sequence", "simulation_time_us"},
            "coordinator event",
        )
        data = payload["data"]
        if type(data) is not dict:
            raise TypeError("coordinator event data must be an object")
        return cls(
            _model_int(payload["sequence"], "coordinator event sequence", minimum=1),
            _model_int(payload["simulation_time_us"], "coordinator event time"),
            CoordinatorEventType(
                _model_string(payload["event_type"], "coordinator event type")
            ),
            data,
        )


def canonical_sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_model_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _model_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _model_optional_int(
    value: object, label: str, *, minimum: int = 0
) -> int | None:
    if value is None:
        return None
    return _model_int(value, label, minimum=minimum)


def _model_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a nonempty string")
    return value


def _model_sha256(value: object, label: str) -> str:
    digest = _model_string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return digest
