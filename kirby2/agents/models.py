"""Contracts for bounded synthetic participants and disclosure-safe ecology runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.exchange import OrderType, SessionState, Side
from kirby2.immutable import freeze_json, thaw_json
from kirby2.multivenue.models import canonical_sha256


AGENT_ECOLOGY_SCHEMA_VERSION = 1
SYNTHETIC_VENUE_ID = "KIRBY2_SYNTHETIC_ONLY"


class AgentFamily(str, Enum):
    NOISE_TRADER = "NOISE_TRADER"
    PASSIVE_MARKET_MAKER = "PASSIVE_MARKET_MAKER"
    INVENTORY_SENSITIVE_MARKET_MAKER = "INVENTORY_SENSITIVE_MARKET_MAKER"
    MOMENTUM_TRADER = "MOMENTUM_TRADER"
    MEAN_REVERSION_TRADER = "MEAN_REVERSION_TRADER"
    SCHEDULED_METAORDER = "SCHEDULED_METAORDER"
    DISTRESSED_LIQUIDATOR = "DISTRESSED_LIQUIDATOR"
    LIQUIDITY_WITHDRAWER = "LIQUIDITY_WITHDRAWER"
    LATENT_VALUE_TRADER = "LATENT_VALUE_TRADER"
    AUCTION_PARTICIPANT = "AUCTION_PARTICIPANT"
    DECEPTIVE_DISPLAY = "DECEPTIVE_DISPLAY"


class AgentInformationSet(str, Enum):
    PUBLIC_MARKET_AND_OWN_STATE = "PUBLIC_MARKET_AND_OWN_STATE"
    CONTROLLED_LATENT_VALUE = "CONTROLLED_LATENT_VALUE"


class AgentSafetyClass(str, Enum):
    STANDARD_SYNTHETIC = "STANDARD_SYNTHETIC"
    CONTROLLED_LATENT_INFORMATION = "CONTROLLED_LATENT_INFORMATION"
    RECOGNITION_DRILL_ONLY = "RECOGNITION_DRILL_ONLY"


class AgentIntentType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class AgentActionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class AgentBounds:
    """Hard gateway limits; all quantities are shares and all time is simulation time."""

    quantity_budget: int
    max_abs_inventory: int
    max_working_quantity: int
    max_orders_per_second: int
    max_order_quantity: int
    max_price_distance_ticks: int
    latency_us: int
    lifetime_start_us: int
    lifetime_end_us: int
    decision_interval_us: int
    information_set: AgentInformationSet = (
        AgentInformationSet.PUBLIC_MARKET_AND_OWN_STATE
    )

    def __post_init__(self) -> None:
        positive = (
            self.quantity_budget,
            self.max_abs_inventory,
            self.max_working_quantity,
            self.max_orders_per_second,
            self.max_order_quantity,
            self.max_price_distance_ticks,
            self.decision_interval_us,
        )
        if any(type(value) is not int or value <= 0 for value in positive):
            raise ValueError("agent budgets, risk limits, rates, and intervals must be positive")
        if type(self.latency_us) is not int or self.latency_us < 0:
            raise ValueError("agent latency must be nonnegative integer microseconds")
        if (
            type(self.lifetime_start_us) is not int
            or type(self.lifetime_end_us) is not int
            or self.lifetime_start_us < 0
            or self.lifetime_end_us <= self.lifetime_start_us
        ):
            raise ValueError("agent lifetime must be a positive simulation-time interval")
        minimum_interval = (1_000_000 + self.max_orders_per_second - 1) // (
            self.max_orders_per_second
        )
        if self.decision_interval_us < minimum_interval:
            raise ValueError("decision cadence could exceed the bounded order rate")
        if self.max_order_quantity > self.quantity_budget:
            raise ValueError("maximum order quantity exceeds the quantity budget")
        if self.max_order_quantity > self.max_working_quantity:
            raise ValueError("maximum order quantity exceeds working-quantity risk")
        if not isinstance(self.information_set, AgentInformationSet):
            raise TypeError("agent information set must use the canonical enum")

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_interval_us": self.decision_interval_us,
            "information_set": self.information_set.value,
            "latency_us": self.latency_us,
            "lifetime_end_us": self.lifetime_end_us,
            "lifetime_start_us": self.lifetime_start_us,
            "max_abs_inventory": self.max_abs_inventory,
            "max_order_quantity": self.max_order_quantity,
            "max_orders_per_second": self.max_orders_per_second,
            "max_price_distance_ticks": self.max_price_distance_ticks,
            "max_working_quantity": self.max_working_quantity,
            "quantity_budget": self.quantity_budget,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AgentBounds:
        _require_fields(
            payload,
            {
                "decision_interval_us",
                "information_set",
                "latency_us",
                "lifetime_end_us",
                "lifetime_start_us",
                "max_abs_inventory",
                "max_order_quantity",
                "max_orders_per_second",
                "max_price_distance_ticks",
                "max_working_quantity",
                "quantity_budget",
            },
            "agent bounds",
        )
        return cls(
            quantity_budget=_wire_int(payload, "quantity_budget"),
            max_abs_inventory=_wire_int(payload, "max_abs_inventory"),
            max_working_quantity=_wire_int(payload, "max_working_quantity"),
            max_orders_per_second=_wire_int(payload, "max_orders_per_second"),
            max_order_quantity=_wire_int(payload, "max_order_quantity"),
            max_price_distance_ticks=_wire_int(
                payload,
                "max_price_distance_ticks",
            ),
            latency_us=_wire_int(payload, "latency_us"),
            lifetime_start_us=_wire_int(payload, "lifetime_start_us"),
            lifetime_end_us=_wire_int(payload, "lifetime_end_us"),
            decision_interval_us=_wire_int(payload, "decision_interval_us"),
            information_set=AgentInformationSet(
                _wire_str(payload, "information_set")
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentPolicyParameters:
    """Internal simulator policy knobs; deceptive-display values are never exported."""

    clip_quantity: int
    preferred_side: Side | None = None
    activation_time_us: int = 0
    withdrawal_time_us: int | None = None
    latent_value_ticks: int | None = None
    quote_offset_ticks: int = 0
    reserve_price_ticks: int | None = None
    auction_only: bool = False
    repeat_display: bool = False

    def __post_init__(self) -> None:
        if type(self.clip_quantity) is not int or self.clip_quantity <= 0:
            raise ValueError("agent clip quantity must be positive")
        if type(self.activation_time_us) is not int or self.activation_time_us < 0:
            raise ValueError("agent activation time must be nonnegative")
        if self.withdrawal_time_us is not None and (
            type(self.withdrawal_time_us) is not int
            or self.withdrawal_time_us < self.activation_time_us
        ):
            raise ValueError("agent withdrawal time precedes activation")
        for value, label in (
            (self.latent_value_ticks, "latent value"),
            (self.reserve_price_ticks, "reserve price"),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"agent {label} must use positive integer ticks")
        if type(self.quote_offset_ticks) is not int or self.quote_offset_ticks < 0:
            raise ValueError("agent quote offset must be nonnegative integer ticks")

    def identity_dict(self) -> dict[str, object]:
        return {
            "activation_time_us": self.activation_time_us,
            "auction_only": self.auction_only,
            "clip_quantity": self.clip_quantity,
            "latent_value_ticks": self.latent_value_ticks,
            "preferred_side": (
                None if self.preferred_side is None else self.preferred_side.value
            ),
            "quote_offset_ticks": self.quote_offset_ticks,
            "repeat_display": self.repeat_display,
            "reserve_price_ticks": self.reserve_price_ticks,
            "withdrawal_time_us": self.withdrawal_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AgentPolicyParameters:
        _require_fields(
            payload,
            {
                "activation_time_us",
                "auction_only",
                "clip_quantity",
                "latent_value_ticks",
                "preferred_side",
                "quote_offset_ticks",
                "repeat_display",
                "reserve_price_ticks",
                "withdrawal_time_us",
            },
            "agent policy",
        )
        raw_side = payload["preferred_side"]
        if raw_side is not None and type(raw_side) is not str:
            raise TypeError("serialized preferred side must be a string or null")
        return cls(
            clip_quantity=_wire_int(payload, "clip_quantity"),
            preferred_side=None if raw_side is None else Side(raw_side),
            activation_time_us=_wire_int(payload, "activation_time_us"),
            withdrawal_time_us=_wire_optional_int(
                payload,
                "withdrawal_time_us",
            ),
            latent_value_ticks=_wire_optional_int(
                payload,
                "latent_value_ticks",
            ),
            quote_offset_ticks=_wire_int(payload, "quote_offset_ticks"),
            reserve_price_ticks=_wire_optional_int(
                payload,
                "reserve_price_ticks",
            ),
            auction_only=_wire_bool(payload, "auction_only"),
            repeat_display=_wire_bool(payload, "repeat_display"),
        )


@dataclass(frozen=True, slots=True)
class AgentSpec:
    agent_id: str
    family: AgentFamily
    bounds: AgentBounds
    policy: AgentPolicyParameters
    safety_class: AgentSafetyClass = AgentSafetyClass.STANDARD_SYNTHETIC

    def __post_init__(self) -> None:
        if not self.agent_id or not self.agent_id.isascii():
            raise ValueError("agent ID must be nonempty ASCII")
        if not isinstance(self.family, AgentFamily):
            raise TypeError("agent family must use the canonical enum")
        if self.policy.clip_quantity > self.bounds.max_order_quantity:
            raise ValueError("agent policy clip exceeds its hard order-size limit")
        if self.policy.activation_time_us < self.bounds.lifetime_start_us:
            raise ValueError("agent policy activates before its bounded lifetime")
        if self.family is AgentFamily.LATENT_VALUE_TRADER:
            if (
                self.safety_class is not AgentSafetyClass.CONTROLLED_LATENT_INFORMATION
                or self.bounds.information_set
                is not AgentInformationSet.CONTROLLED_LATENT_VALUE
                or self.policy.latent_value_ticks is None
            ):
                raise ValueError("latent-value agents require explicit controlled information")
        elif self.bounds.information_set is AgentInformationSet.CONTROLLED_LATENT_VALUE:
            raise ValueError("only the controlled latent-value family may receive latent value")
        if self.family is AgentFamily.DECEPTIVE_DISPLAY:
            if self.safety_class is not AgentSafetyClass.RECOGNITION_DRILL_ONLY:
                raise ValueError("deceptive display is restricted to recognition drills")
        elif self.safety_class is AgentSafetyClass.RECOGNITION_DRILL_ONLY:
            raise ValueError("recognition-only safety class is reserved for deceptive display")

    def identity_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "bounds": self.bounds.as_dict(),
            "family": self.family.value,
            "policy": self.policy.identity_dict(),
            "safety_class": self.safety_class.value,
        }

    def public_dict(self) -> dict[str, object]:
        policy: object = (
            "REDACTED_SIMULATOR_RECOGNITION_POLICY"
            if self.family is AgentFamily.DECEPTIVE_DISPLAY
            else self.policy.identity_dict()
        )
        return {
            "agent_id": self.agent_id,
            "bounds": self.bounds.as_dict(),
            "family": self.family.value,
            "policy": policy,
            "safety_class": self.safety_class.value,
            "venue_scope": SYNTHETIC_VENUE_ID,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AgentSpec:
        _require_fields(
            payload,
            {"agent_id", "bounds", "family", "policy", "safety_class"},
            "agent specification",
        )
        raw_bounds = payload["bounds"]
        raw_policy = payload["policy"]
        if not isinstance(raw_bounds, Mapping) or not isinstance(
            raw_policy,
            Mapping,
        ):
            raise TypeError("serialized agent bounds and policy must be objects")
        return cls(
            agent_id=_wire_str(payload, "agent_id"),
            family=AgentFamily(_wire_str(payload, "family")),
            bounds=AgentBounds.from_dict(raw_bounds),
            policy=AgentPolicyParameters.from_dict(raw_policy),
            safety_class=AgentSafetyClass(
                _wire_str(payload, "safety_class")
            ),
        )


@dataclass(frozen=True, slots=True)
class EcologyTransition:
    simulation_time_us: int
    state: SessionState
    uncross_before: bool = False

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("ecology transition time must be nonnegative")
        if not isinstance(self.state, SessionState):
            raise TypeError("ecology transition state must use the canonical enum")

    def as_dict(self) -> dict[str, object]:
        return {
            "simulation_time_us": self.simulation_time_us,
            "state": self.state.value,
            "uncross_before": self.uncross_before,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> EcologyTransition:
        _require_fields(
            payload,
            {"simulation_time_us", "state", "uncross_before"},
            "ecology transition",
        )
        return cls(
            simulation_time_us=_wire_int(payload, "simulation_time_us"),
            state=SessionState(_wire_str(payload, "state")),
            uncross_before=_wire_bool(payload, "uncross_before"),
        )


@dataclass(frozen=True, slots=True)
class PopulationDefinition:
    population_id: str
    description: str
    agents: tuple[AgentSpec, ...]
    duration_us: int
    initial_mid_ticks: int = 10_000
    initial_depth_levels: int = 3
    initial_level_quantity: int = 200
    descriptive_regime_label: str = "emergent_agent_ecology"
    recognition_drill: bool = False
    post_session_explanation: str = "Participant composition generated the observed synthetic flow."
    start_state: SessionState = SessionState.CONTINUOUS
    transitions: tuple[EcologyTransition, ...] = ()

    def __post_init__(self) -> None:
        if not self.population_id or not self.description or not self.agents:
            raise ValueError("population identity, description, and agents are required")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("population duration must be positive simulation time")
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.initial_mid_ticks,
                self.initial_depth_levels,
                self.initial_level_quantity,
            )
        ):
            raise ValueError("population initial-book parameters must be positive integers")
        ids = tuple(agent.agent_id for agent in self.agents)
        if len(ids) != len(set(ids)):
            raise ValueError("population agent IDs must be unique")
        has_deceptive = any(
            agent.family is AgentFamily.DECEPTIVE_DISPLAY for agent in self.agents
        )
        if has_deceptive and not self.recognition_drill:
            raise ValueError("deceptive display cannot exist outside a recognition drill")
        if self.recognition_drill and not self.post_session_explanation:
            raise ValueError("recognition drill requires a defensive post-session explanation")
        for agent in self.agents:
            if agent.bounds.lifetime_end_us > self.duration_us:
                raise ValueError("agent lifetime exceeds population duration")
        transition_times = tuple(item.simulation_time_us for item in self.transitions)
        if transition_times != tuple(sorted(transition_times)):
            raise ValueError("population session transitions must be time ordered")
        if any(item.simulation_time_us > self.duration_us for item in self.transitions):
            raise ValueError("population transition exceeds duration")

    def identity_dict(self) -> dict[str, object]:
        return {
            "agents": [agent.identity_dict() for agent in self.agents],
            "description": self.description,
            "descriptive_regime_label": self.descriptive_regime_label,
            "duration_us": self.duration_us,
            "initial_depth_levels": self.initial_depth_levels,
            "initial_level_quantity": self.initial_level_quantity,
            "initial_mid_ticks": self.initial_mid_ticks,
            "population_id": self.population_id,
            "post_session_explanation": self.post_session_explanation,
            "recognition_drill": self.recognition_drill,
            "schema_version": AGENT_ECOLOGY_SCHEMA_VERSION,
            "start_state": self.start_state.value,
            "transitions": [item.as_dict() for item in self.transitions],
        }

    def public_manifest(self) -> dict[str, object]:
        payload = self.identity_dict()
        payload["agents"] = [agent.public_dict() for agent in self.agents]
        return payload

    def sha256(self) -> str:
        return canonical_sha256(self.identity_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PopulationDefinition:
        _require_fields(
            payload,
            {
                "agents",
                "description",
                "descriptive_regime_label",
                "duration_us",
                "initial_depth_levels",
                "initial_level_quantity",
                "initial_mid_ticks",
                "population_id",
                "post_session_explanation",
                "recognition_drill",
                "schema_version",
                "start_state",
                "transitions",
            },
            "population definition",
        )
        if _wire_int(payload, "schema_version") != AGENT_ECOLOGY_SCHEMA_VERSION:
            raise ValueError("unsupported population-definition schema")
        raw_agents = payload["agents"]
        raw_transitions = payload["transitions"]
        if not isinstance(raw_agents, list) or any(
            not isinstance(item, Mapping) for item in raw_agents
        ):
            raise TypeError("serialized population agents must be objects")
        if not isinstance(raw_transitions, list) or any(
            not isinstance(item, Mapping) for item in raw_transitions
        ):
            raise TypeError("serialized population transitions must be objects")
        return cls(
            population_id=_wire_str(payload, "population_id"),
            description=_wire_str(payload, "description"),
            agents=tuple(AgentSpec.from_dict(item) for item in raw_agents),
            duration_us=_wire_int(payload, "duration_us"),
            initial_mid_ticks=_wire_int(payload, "initial_mid_ticks"),
            initial_depth_levels=_wire_int(payload, "initial_depth_levels"),
            initial_level_quantity=_wire_int(
                payload,
                "initial_level_quantity",
            ),
            descriptive_regime_label=_wire_str(
                payload,
                "descriptive_regime_label",
            ),
            recognition_drill=_wire_bool(payload, "recognition_drill"),
            post_session_explanation=_wire_str(
                payload,
                "post_session_explanation",
            ),
            start_state=SessionState(_wire_str(payload, "start_state")),
            transitions=tuple(
                EcologyTransition.from_dict(item) for item in raw_transitions
            ),
        )


@dataclass(frozen=True, slots=True)
class OwnOrderView:
    order_id: str
    side: Side
    price_ticks: int | None
    remaining_quantity: int
    auction_only: bool

    def __post_init__(self) -> None:
        if type(self.order_id) is not str or not self.order_id:
            raise ValueError("own-order view requires a nonempty order ID")
        if type(self.side) is not Side:
            raise TypeError("own-order view side must use Side")
        if self.price_ticks is not None and (
            type(self.price_ticks) is not int or self.price_ticks <= 0
        ):
            raise ValueError("own-order view price must use positive integer ticks")
        if type(self.remaining_quantity) is not int or self.remaining_quantity <= 0:
            raise ValueError("own-order view remaining quantity must be positive")
        if type(self.auction_only) is not bool:
            raise TypeError("own-order view auction flag must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "auction_only": self.auction_only,
            "order_id": self.order_id,
            "price_ticks": self.price_ticks,
            "remaining_quantity": self.remaining_quantity,
            "side": self.side.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> OwnOrderView:
        _require_fields(
            payload,
            {
                "auction_only",
                "order_id",
                "price_ticks",
                "remaining_quantity",
                "side",
            },
            "own-order view",
        )
        return cls(
            order_id=_wire_str(payload, "order_id"),
            side=Side(_wire_str(payload, "side")),
            price_ticks=_wire_optional_int(payload, "price_ticks"),
            remaining_quantity=_wire_int(payload, "remaining_quantity"),
            auction_only=_wire_bool(payload, "auction_only"),
        )


@dataclass(frozen=True, slots=True)
class PublicTradeView:
    simulation_time_us: int
    price_ticks: int
    quantity: int

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("public trade time must be nonnegative")
        if type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("public trade price must use positive integer ticks")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("public trade quantity must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PublicTradeView:
        _require_fields(
            payload,
            {"price_ticks", "quantity", "simulation_time_us"},
            "public trade view",
        )
        return cls(
            simulation_time_us=_wire_int(payload, "simulation_time_us"),
            price_ticks=_wire_int(payload, "price_ticks"),
            quantity=_wire_int(payload, "quantity"),
        )


@dataclass(frozen=True, slots=True)
class AgentObservation:
    simulation_time_us: int
    session_state: SessionState
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
    recent_trades: tuple[PublicTradeView, ...]
    own_inventory: int
    own_remaining_budget: int
    own_orders: tuple[OwnOrderView, ...]
    auction_indication: dict[str, object] | None
    information_boundary: str = "PUBLIC_MARKET_AND_OWN_STATE_AT_DECISION_TIME"

    @property
    def best_bid_ticks(self) -> int | None:
        return None if not self.bids else self.bids[0][0]

    @property
    def best_ask_ticks(self) -> int | None:
        return None if not self.asks else self.asks[0][0]

    @property
    def midpoint_x2(self) -> int | None:
        if self.best_bid_ticks is None or self.best_ask_ticks is None:
            return None
        return self.best_bid_ticks + self.best_ask_ticks


@dataclass(frozen=True, slots=True)
class AgentIntent:
    intent_type: AgentIntentType
    rationale: str
    order_type: OrderType | None = None
    side: Side | None = None
    quantity: int | None = None
    price_ticks: int | None = None
    cancel_target_order_id: str | None = None
    auction_only: bool = False

    def __post_init__(self) -> None:
        if not self.rationale:
            raise ValueError("agent intent requires a private rationale code")
        if self.intent_type is AgentIntentType.CANCEL:
            if not self.cancel_target_order_id:
                raise ValueError("cancel intent requires an own order ID")
            if any(
                value is not None
                for value in (self.order_type, self.side, self.quantity, self.price_ticks)
            ):
                raise ValueError("cancel intent cannot carry submission fields")
            if self.auction_only:
                raise ValueError("cancel intent cannot set auction-only")
            return
        if self.order_type not in {OrderType.LIMIT, OrderType.MARKET}:
            raise ValueError("agent submissions support limit and market orders only")
        if self.side is None or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("agent submission requires side and positive quantity")
        if self.order_type is OrderType.LIMIT:
            if type(self.price_ticks) is not int or self.price_ticks <= 0:
                raise ValueError("agent limit intent requires positive integer ticks")
        elif self.price_ticks is not None:
            raise ValueError("agent market intent cannot carry a price")
        if self.cancel_target_order_id is not None:
            raise ValueError("submission intent cannot carry a cancel target")

    def as_dict(self) -> dict[str, object]:
        return {
            "auction_only": self.auction_only,
            "cancel_target_order_id": self.cancel_target_order_id,
            "intent_type": self.intent_type.value,
            "order_type": None if self.order_type is None else self.order_type.value,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "rationale": self.rationale,
            "side": None if self.side is None else self.side.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AgentIntent:
        _require_fields(
            payload,
            {
                "auction_only",
                "cancel_target_order_id",
                "intent_type",
                "order_type",
                "price_ticks",
                "quantity",
                "rationale",
                "side",
            },
            "agent intent",
        )
        raw_order_type = payload["order_type"]
        raw_side = payload["side"]
        raw_cancel = payload["cancel_target_order_id"]
        for value, label in (
            (raw_order_type, "order_type"),
            (raw_side, "side"),
            (raw_cancel, "cancel_target_order_id"),
        ):
            if value is not None and type(value) is not str:
                raise TypeError(f"serialized {label} must be a string or null")
        return cls(
            intent_type=AgentIntentType(_wire_str(payload, "intent_type")),
            rationale=_wire_str(payload, "rationale"),
            order_type=(
                None if raw_order_type is None else OrderType(raw_order_type)
            ),
            side=None if raw_side is None else Side(raw_side),
            quantity=_wire_optional_int(payload, "quantity"),
            price_ticks=_wire_optional_int(payload, "price_ticks"),
            cancel_target_order_id=raw_cancel,
            auction_only=_wire_bool(payload, "auction_only"),
        )


@dataclass(frozen=True, slots=True)
class PublicEcologyEvent:
    sequence: int
    simulation_time_us: int
    event_type: str
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("public ecology event sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("public ecology event time must be nonnegative")
        if type(self.event_type) is not str or not self.event_type:
            raise ValueError("public ecology event type must be nonempty")
        frozen = freeze_json(self.data)
        if not isinstance(frozen, Mapping):
            raise TypeError("public ecology event data must be a JSON object")
        object.__setattr__(self, "data", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "data": thaw_json(self.data),
            "event_type": self.event_type,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PublicEcologyEvent:
        _require_fields(
            payload,
            {"data", "event_type", "sequence", "simulation_time_us"},
            "public ecology event",
        )
        raw_data = payload["data"]
        if not isinstance(raw_data, Mapping):
            raise TypeError("serialized public ecology event data must be an object")
        return cls(
            sequence=_wire_int(payload, "sequence"),
            simulation_time_us=_wire_int(payload, "simulation_time_us"),
            event_type=_wire_str(payload, "event_type"),
            data=raw_data,
        )


@dataclass(frozen=True, slots=True)
class AgentTruthEvent:
    sequence: int
    decision_time_us: int
    arrival_time_us: int
    agent_id: str
    family: AgentFamily
    intent: AgentIntent
    status: AgentActionStatus
    result_reason: str
    order_id: str | None
    exchange_event_start: int | None
    exchange_event_end: int | None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("agent truth sequence must be positive")
        if (
            type(self.decision_time_us) is not int
            or type(self.arrival_time_us) is not int
            or self.decision_time_us < 0
            or self.arrival_time_us < self.decision_time_us
        ):
            raise ValueError("agent truth decision/arrival time is invalid")
        if type(self.agent_id) is not str or not self.agent_id:
            raise ValueError("agent truth event requires an agent ID")
        if type(self.family) is not AgentFamily:
            raise TypeError("agent truth family must use AgentFamily")
        if type(self.intent) is not AgentIntent:
            raise TypeError("agent truth intent must use AgentIntent")
        if type(self.status) is not AgentActionStatus:
            raise TypeError("agent truth status must use AgentActionStatus")
        if type(self.result_reason) is not str or not self.result_reason:
            raise ValueError("agent truth event requires a result reason")
        if self.order_id is not None and (
            type(self.order_id) is not str or not self.order_id
        ):
            raise ValueError("agent truth order ID must be nonempty or null")
        if (self.exchange_event_start is None) != (self.exchange_event_end is None):
            raise ValueError("agent truth exchange-event range must be wholly present or null")
        if self.exchange_event_start is not None and (
            type(self.exchange_event_start) is not int
            or type(self.exchange_event_end) is not int
            or self.exchange_event_start <= 0
            or self.exchange_event_end < self.exchange_event_start
        ):
            raise ValueError("agent truth exchange-event range is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "arrival_time_us": self.arrival_time_us,
            "decision_time_us": self.decision_time_us,
            "exchange_event_end": self.exchange_event_end,
            "exchange_event_start": self.exchange_event_start,
            "family": self.family.value,
            "intent": self.intent.as_dict(),
            "order_id": self.order_id,
            "result_reason": self.result_reason,
            "sequence": self.sequence,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AgentTruthEvent:
        _require_fields(
            payload,
            {
                "agent_id",
                "arrival_time_us",
                "decision_time_us",
                "exchange_event_end",
                "exchange_event_start",
                "family",
                "intent",
                "order_id",
                "result_reason",
                "sequence",
                "status",
            },
            "agent truth event",
        )
        raw_intent = payload["intent"]
        if not isinstance(raw_intent, Mapping):
            raise TypeError("serialized agent truth intent must be an object")
        raw_order_id = payload["order_id"]
        if raw_order_id is not None and type(raw_order_id) is not str:
            raise TypeError("serialized agent truth order ID must be a string or null")
        return cls(
            sequence=_wire_int(payload, "sequence"),
            decision_time_us=_wire_int(payload, "decision_time_us"),
            arrival_time_us=_wire_int(payload, "arrival_time_us"),
            agent_id=_wire_str(payload, "agent_id"),
            family=AgentFamily(_wire_str(payload, "family")),
            intent=AgentIntent.from_dict(raw_intent),
            status=AgentActionStatus(_wire_str(payload, "status")),
            result_reason=_wire_str(payload, "result_reason"),
            order_id=raw_order_id,
            exchange_event_start=_wire_optional_int(
                payload, "exchange_event_start"
            ),
            exchange_event_end=_wire_optional_int(payload, "exchange_event_end"),
        )


def _require_fields(
    payload: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    fields = set(payload)
    missing = sorted(expected.difference(fields))
    unknown = sorted(fields.difference(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {label} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )


def _wire_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _wire_optional_int(
    payload: Mapping[str, object],
    name: str,
) -> int | None:
    value = payload[name]
    if value is not None and type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer or null")
    return value


def _wire_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _wire_bool(payload: Mapping[str, object], name: str) -> bool:
    value = payload[name]
    if type(value) is not bool:
        raise TypeError(f"serialized {name} must be a boolean")
    return value
