"""Passive observable feature, strategy, and player state for full-day runs.

The owner consumes only messages already delivered by ``DELIVERY_ASYNC_V1``.
It retains no exchange, book, clock, calendar, or gateway.  Player decisions are
immutable proposals that the authoritative full-day runtime later routes through
the ordinary delivery and venue stages.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType

from kirby2.exchange import AdvancedOrderRequest, OrderOwner, OrderType, Side
from kirby2.features import MicrostructureFeatureEngine
from kirby2.player import PlayerPosition
from kirby2.session.events import EventType, SimulationEvent
from kirby2.strategy import (
    StateMachineDefinition,
    StateMachineRuntime,
    StrategyDefinition,
    TrafficLightRuntime,
    parse_strategy,
)

from .components import ComponentSnapshotV1, FullDayComponentAdapterV1
from .components_delivery import DeliveryMessageV1, DeliveryOwnerV1
from .composition import (
    DELIVERY_ASYNC_COMPONENT,
    FEATURE_STRATEGY_PLAYER_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
    MECHANICS_COMPONENT,
    component_configured_predicate,
)
from .models import (
    FullDayPlanV1,
    VersionedReferenceV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)


RESEARCH_SCHEMA_VERSION = 1
RESEARCH_IMPLEMENTATION_VERSION = 1
RESEARCH_ENGINE_ID = "MICROSTRUCTURE_FEATURE_ENGINE_V1"
RESEARCH_STRATEGY_VERSION = 1
RESEARCH_NATIVE_LEDGER_ID = "FEATURE_STRATEGY_PLAYER_EVENTS_V1"
RESEARCH_FEATURES_STATE_ID = "FEATURES_V1"
RESEARCH_PLAYER_STATE_ID = "PLAYER_OVERLAY_WORKING_ORDERS_V1"
RESEARCH_STRATEGY_STATE_ID = "STRATEGIES_V1"

_DECISION_ACTIONS = frozenset({"CANCEL", "REPLACE", "SUBMIT"})
_WORKING_STATUSES = frozenset(
    {"ACCEPTED", "AUCTION_PENDING", "PARTIALLY_FILLED", "PENDING", "WORKING"}
)


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _exact_string(value, field)


def _plain_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    validate_strict_json(value)
    return dict(value)


def _validate_decision_action_payload(
    action: str,
    payload: Mapping[str, object],
) -> None:
    fields = {
        "SUBMIT": {"request"},
        "CANCEL": {"order_id", "reason"},
        "REPLACE": {
            "new_order_id",
            "new_price_ticks",
            "new_quantity",
            "order_id",
        },
    }[action]
    _require_exact_fields(payload, fields, f"player {action}")
    if action == "SUBMIT":
        request = payload["request"]
        if not isinstance(request, Mapping):
            raise TypeError("player submit request must be an object")
        AdvancedOrderRequest.from_dict(dict(request))
    elif action == "CANCEL":
        _exact_string(payload["order_id"], "order_id")
        _exact_string(payload["reason"], "reason")
    else:
        _exact_string(payload["order_id"], "order_id")
        _exact_string(payload["new_order_id"], "new_order_id")
        _exact_int(payload["new_quantity"], "new_quantity", minimum=1)
        price = payload["new_price_ticks"]
        if price is not None:
            _exact_int(price, "new_price_ticks", minimum=1)


@dataclass(frozen=True, slots=True)
class ResearchConfigurationV1:
    schema_version: int
    configuration_id: str
    configuration_version: int
    feature_engine_id: str
    feature_engine_version: int
    feature_windows_us: tuple[int, ...]
    depth_levels: int
    relative_volume_millionths: int
    strategy_source: str
    strategy_source_sha256: str
    strategy_version: int

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_SCHEMA_VERSION:
            raise ValueError("research configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        if self.feature_engine_id != RESEARCH_ENGINE_ID:
            raise ValueError("research feature engine ID is unsupported")
        if self.feature_engine_version != 1:
            raise ValueError("research feature engine version is unsupported")
        if (
            type(self.feature_windows_us) is not tuple
            or not self.feature_windows_us
            or self.feature_windows_us != tuple(sorted(set(self.feature_windows_us)))
            or any(type(value) is not int or value <= 0 for value in self.feature_windows_us)
        ):
            raise ValueError("research feature windows must be sorted unique positives")
        _exact_int(self.depth_levels, "depth_levels", minimum=1)
        _exact_int(
            self.relative_volume_millionths,
            "relative_volume_millionths",
        )
        source = _exact_string(self.strategy_source, "strategy_source")
        digest = _exact_string(self.strategy_source_sha256, "strategy_source_sha256")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != digest:
            raise ValueError("research strategy source digest mismatch")
        if self.strategy_version != RESEARCH_STRATEGY_VERSION:
            raise ValueError("research strategy version is unsupported")
        if self.strategy_definition.window_us not in self.feature_windows_us:
            raise ValueError("strategy window is absent from the feature configuration")

    @classmethod
    def create(
        cls,
        *,
        configuration_id: str,
        configuration_version: int,
        strategy_source: str,
        feature_windows_us: tuple[int, ...] | None = None,
        depth_levels: int = 5,
        relative_volume_millionths: int = 1_000_000,
    ) -> ResearchConfigurationV1:
        definition = parse_strategy(strategy_source)
        windows = (
            (definition.window_us,)
            if feature_windows_us is None
            else feature_windows_us
        )
        return cls(
            schema_version=RESEARCH_SCHEMA_VERSION,
            configuration_id=configuration_id,
            configuration_version=configuration_version,
            feature_engine_id=RESEARCH_ENGINE_ID,
            feature_engine_version=1,
            feature_windows_us=windows,
            depth_levels=depth_levels,
            relative_volume_millionths=relative_volume_millionths,
            strategy_source=strategy_source,
            strategy_source_sha256=hashlib.sha256(
                strategy_source.encode("utf-8")
            ).hexdigest(),
            strategy_version=RESEARCH_STRATEGY_VERSION,
        )

    @property
    def relative_volume(self) -> Decimal:
        return Decimal(self.relative_volume_millionths) / Decimal(1_000_000)

    @property
    def strategy_definition(self) -> StrategyDefinition | StateMachineDefinition:
        definition = parse_strategy(self.strategy_source)
        if definition.source_sha256 != self.strategy_source_sha256:
            raise ValueError("research strategy parser changed the source identity")
        return definition

    @property
    def reference(self) -> VersionedReferenceV1:
        return VersionedReferenceV1(
            self.configuration_id,
            self.configuration_version,
            self.sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "depth_levels": self.depth_levels,
            "feature_engine_id": self.feature_engine_id,
            "feature_engine_version": self.feature_engine_version,
            "feature_windows_us": list(self.feature_windows_us),
            "relative_volume_millionths": self.relative_volume_millionths,
            "schema_version": self.schema_version,
            "strategy_source": self.strategy_source,
            "strategy_source_sha256": self.strategy_source_sha256,
            "strategy_version": self.strategy_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ResearchConfigurationV1:
        fields = {
            "configuration_id",
            "configuration_version",
            "depth_levels",
            "feature_engine_id",
            "feature_engine_version",
            "feature_windows_us",
            "relative_volume_millionths",
            "schema_version",
            "strategy_source",
            "strategy_source_sha256",
            "strategy_version",
        }
        _require_exact_fields(payload, fields, "ResearchConfigurationV1")
        windows = payload["feature_windows_us"]
        if type(windows) is not list:
            raise TypeError("research feature windows must be an array")
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            configuration_id=payload["configuration_id"],  # type: ignore[arg-type]
            configuration_version=payload["configuration_version"],  # type: ignore[arg-type]
            feature_engine_id=payload["feature_engine_id"],  # type: ignore[arg-type]
            feature_engine_version=payload["feature_engine_version"],  # type: ignore[arg-type]
            feature_windows_us=tuple(windows),
            depth_levels=payload["depth_levels"],  # type: ignore[arg-type]
            relative_volume_millionths=payload["relative_volume_millionths"],  # type: ignore[arg-type]
            strategy_source=payload["strategy_source"],  # type: ignore[arg-type]
            strategy_source_sha256=payload["strategy_source_sha256"],  # type: ignore[arg-type]
            strategy_version=payload["strategy_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PlayerDecisionV1:
    schema_version: int
    decision_id: str
    decision_sequence: int
    action: str
    decided_at_us: int
    scheduled_time_us: int
    information_cutoff_us: int
    action_payload: Mapping[str, object]
    work_id: str | None
    route_work_id: str | None

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_SCHEMA_VERSION:
            raise ValueError("player decision schema version must be 1")
        _exact_string(self.decision_id, "decision_id")
        _exact_int(self.decision_sequence, "decision_sequence", minimum=1)
        if self.action not in _DECISION_ACTIONS:
            raise ValueError("player decision action is unsupported")
        decided = _exact_int(self.decided_at_us, "decided_at_us")
        scheduled = _exact_int(self.scheduled_time_us, "scheduled_time_us")
        cutoff = _exact_int(self.information_cutoff_us, "information_cutoff_us")
        if cutoff > decided or scheduled < decided:
            raise ValueError("player decision times violate causal ordering")
        validate_strict_json(self.action_payload)
        _validate_decision_action_payload(self.action, self.action_payload)
        _optional_string(self.work_id, "work_id")
        _optional_string(self.route_work_id, "route_work_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "action_payload": dict(self.action_payload),
            "decided_at_us": self.decided_at_us,
            "decision_id": self.decision_id,
            "decision_sequence": self.decision_sequence,
            "information_cutoff_us": self.information_cutoff_us,
            "route_work_id": self.route_work_id,
            "scheduled_time_us": self.scheduled_time_us,
            "schema_version": self.schema_version,
            "work_id": self.work_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PlayerDecisionV1:
        fields = {
            "action",
            "action_payload",
            "decided_at_us",
            "decision_id",
            "decision_sequence",
            "information_cutoff_us",
            "route_work_id",
            "scheduled_time_us",
            "schema_version",
            "work_id",
        }
        _require_exact_fields(payload, fields, "PlayerDecisionV1")
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            decision_id=payload["decision_id"],  # type: ignore[arg-type]
            decision_sequence=payload["decision_sequence"],  # type: ignore[arg-type]
            action=payload["action"],  # type: ignore[arg-type]
            decided_at_us=payload["decided_at_us"],  # type: ignore[arg-type]
            scheduled_time_us=payload["scheduled_time_us"],  # type: ignore[arg-type]
            information_cutoff_us=payload["information_cutoff_us"],  # type: ignore[arg-type]
            action_payload=_plain_object(payload["action_payload"], "action_payload"),
            work_id=payload["work_id"],  # type: ignore[arg-type]
            route_work_id=payload["route_work_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class _DepthLevel:
    total_quantity: int


@dataclass(frozen=True, slots=True)
class _ClientOrder:
    owner: OrderOwner
    order_type: OrderType


class ClientResearchViewV1:
    """Detached client-visible depth and player state, never an exchange book."""

    def __init__(
        self,
        market_state: Mapping[str, object] | None,
        player_position: PlayerPosition,
        known_orders: Mapping[str, Mapping[str, object]],
    ) -> None:
        self._market_state = None if market_state is None else dict(market_state)
        self._player_position = player_position
        self._known_orders = known_orders
        self._bids, self._asks = self._levels()

    def _levels(self) -> tuple[dict[int, _DepthLevel], dict[int, _DepthLevel]]:
        if self._market_state is None:
            return {}, {}
        result: list[dict[int, _DepthLevel]] = []
        for field in ("bid_levels", "ask_levels"):
            rows = self._market_state.get(field)
            if type(rows) is not list:
                raise ValueError("client market depth must be an array")
            levels: dict[int, _DepthLevel] = {}
            for row in rows:
                if not isinstance(row, Mapping) or set(row) != {
                    "price_ticks",
                    "quantity",
                }:
                    raise ValueError("client market level fields are not exact")
                price = _exact_int(row["price_ticks"], "price_ticks", minimum=1)
                quantity = _exact_int(row["quantity"], "quantity")
                if price in levels:
                    raise ValueError("client market depth contains a duplicate price")
                levels[price] = _DepthLevel(quantity)
            result.append(levels)
        return result[0], result[1]

    @property
    def best_bid(self) -> int | None:
        return None if not self._bids else max(self._bids)

    @property
    def best_ask(self) -> int | None:
        return None if not self._asks else min(self._asks)

    @property
    def bid_prices(self) -> list[int]:
        return sorted(self._bids, reverse=True)

    @property
    def ask_prices(self) -> list[int]:
        return sorted(self._asks)

    @property
    def bids(self) -> Mapping[int, _DepthLevel]:
        return MappingProxyType(self._bids)

    @property
    def asks(self) -> Mapping[int, _DepthLevel]:
        return MappingProxyType(self._asks)

    @property
    def player_position(self) -> PlayerPosition:
        return self._player_position

    @property
    def active_orders(self) -> Mapping[str, _ClientOrder]:
        result: dict[str, _ClientOrder] = {}
        for order_id, state in self._known_orders.items():
            request = state.get("request")
            if not isinstance(request, Mapping):
                continue
            if (
                request.get("owner") != OrderOwner.PLAYER.value
                or state.get("status") not in _WORKING_STATUSES
                or type(state.get("remaining_quantity")) is not int
                or state["remaining_quantity"] <= 0
            ):
                continue
            instruction = request.get("instruction")
            order_type = (
                OrderType.MARKET
                if instruction == "MARKET"
                else OrderType.LIMIT
            )
            result[order_id] = _ClientOrder(OrderOwner.PLAYER, order_type)
        return MappingProxyType(result)


class ResearchOwnerV1:
    COMPONENT_ID = FEATURE_STRATEGY_PLAYER_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: ResearchConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("research owner requires FullDayPlanV1")
        if type(configuration) is not ResearchConfigurationV1:
            raise TypeError("research owner requires ResearchConfigurationV1")
        self.configuration = configuration
        self.feature_engine = MicrostructureFeatureEngine(
            windows_us=configuration.feature_windows_us,
            relative_volume=configuration.relative_volume,
            depth_levels=configuration.depth_levels,
        )
        definition = configuration.strategy_definition
        self.strategy: TrafficLightRuntime | StateMachineRuntime
        if type(definition) is StrategyDefinition:
            self.strategy = TrafficLightRuntime(
                definition,
                configuration.relative_volume,
            )
        elif type(definition) is StateMachineDefinition:
            self.strategy = StateMachineRuntime(
                definition,
                configuration.relative_volume,
            )
        else:  # pragma: no cover - the parser has a closed return union
            raise TypeError("research strategy definition is unsupported")
        self.player_position = PlayerPosition()
        self.client_market_state: dict[str, object] | None = None
        self.client_known_orders: dict[str, dict[str, object]] = {}
        self.client_order_cutoffs: dict[str, int] = {}
        self.processed_message_ids: list[str] = []
        self.delivered_message_cursor = 0
        self.fill_report_cursor = 0
        self.observable_event_sequence = 0
        self.feature_batch_sequence = 0
        self.decision_sequence = 0
        self.action_sequence = 0
        self.native_sequence = 0
        self.last_observation_time_us = 0
        self.last_information_cutoff_us = 0
        self.feature_batches: list[dict[str, object]] = []
        self.strategy_deadlines: list[dict[str, object]] = []
        self.causal_windows: list[dict[str, object]] = []
        self.pending_decisions: dict[str, PlayerDecisionV1] = {}
        self.completed_decisions: list[dict[str, object]] = []
        self._validate_plan_binding(plan)
        view = self.client_view()
        self.feature_engine.reset(0, view)
        self.strategy.reset(0, view)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError("research configuration is absent from the plan") from error
        if references != (self.configuration.reference,):
            raise ValueError("research configuration differs from plan binding")

    def client_view(self) -> ClientResearchViewV1:
        return ClientResearchViewV1(
            self.client_market_state,
            self.player_position,
            self.client_known_orders,
        )

    @property
    def next_strategy_deadline_us(self) -> int | None:
        deadline = self.strategy.next_deadline_us
        if deadline is None or deadline <= self.last_observation_time_us:
            return None
        return deadline

    @property
    def working_order_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.client_view().active_orders))

    def _simulation_events(
        self,
        message: DeliveryMessageV1,
    ) -> tuple[SimulationEvent, ...]:
        payload = dict(message.client_payload)
        raw_event_type = payload.get("event_type")
        data = payload.get("event_data")
        if type(raw_event_type) is not str or not isinstance(data, Mapping):
            return ()
        records: list[tuple[EventType, dict[str, object]]] = []
        event_data: dict[str, object] = dict(data)
        if raw_event_type in {"TRADE", "AUCTION_FILL"}:
            player_order_id, side, liquidity = self._reported_player_fill(data)
            if player_order_id is not None and side is not None and liquidity is not None:
                event_data["taker_side"] = (
                    side.value
                    if liquidity == "taker"
                    else (Side.SELL if side is Side.BUY else Side.BUY).value
                )
            elif "taker_side" not in event_data:
                return ()
            records.append((EventType.TRADE, event_data))
            records.extend(
                (
                    EventType.PLAYER_POSITION_CHANGED,
                    {
                        "fill_quantity": data.get("quantity"),
                        "fill_side": player_side.value,
                        "order_id": order_id,
                    },
                )
                for order_id, player_side, _liquidity in self._reported_player_fills(
                    data
                )
            )
        elif raw_event_type == "ORDER_CANCELLED":
            order_id = data.get("order_id")
            state = self.client_known_orders.get(str(order_id), {})
            request = state.get("request") if isinstance(state, Mapping) else None
            if not isinstance(request, Mapping):
                return ()
            event_data.setdefault("side", request.get("side"))
            event_data.setdefault("cancelled_quantity", state.get("cancelled_quantity", 0))
            records.append((EventType.ORDER_CANCELLED, event_data))
        elif raw_event_type == "ORDER_ACCEPTED":
            order_id = data.get("order_id")
            state = self.client_known_orders.get(str(order_id), {})
            request = state.get("request") if isinstance(state, Mapping) else None
            if not isinstance(request, Mapping):
                return ()
            event_data = {
                "order_id": order_id,
                "remaining_quantity": state.get("remaining_quantity", 0),
                "side": request.get("side"),
            }
            records.append((EventType.ORDER_ADDED, event_data))
            if request.get("owner") == OrderOwner.PLAYER.value:
                instruction = request.get("instruction")
                records.append(
                    (
                        EventType.ORDER_SUBMITTED,
                        {
                            "order_id": order_id,
                            "order_type": (
                                OrderType.MARKET.value
                                if instruction == "MARKET"
                                else OrderType.LIMIT.value
                            ),
                            "original_quantity": request.get("quantity"),
                            "owner": OrderOwner.PLAYER.value,
                            "side": request.get("side"),
                        },
                    )
                )
        if not records:
            return ()
        result: list[SimulationEvent] = []
        for event_type, data_row in records:
            self.observable_event_sequence += 1
            result.append(
                SimulationEvent(
                    self.observable_event_sequence,
                    event_type,
                    data_row,
                )
            )
        return tuple(result)

    def _reported_player_fill(
        self,
        data: Mapping[str, object],
    ) -> tuple[str | None, Side | None, str | None]:
        fills = self._reported_player_fills(data)
        return (None, None, None) if not fills else fills[0]

    def _reported_player_fills(
        self,
        data: Mapping[str, object],
    ) -> tuple[tuple[str, Side, str], ...]:
        result: list[tuple[str, Side, str]] = []
        for liquidity, field in (
            ("maker", "maker_order_id"),
            ("taker", "taker_order_id"),
        ):
            order_id = data.get(field)
            if type(order_id) is not str:
                continue
            state = self.client_known_orders.get(order_id)
            request = None if state is None else state.get("request")
            if not isinstance(request, Mapping) or request.get("owner") != "player":
                continue
            result.append((order_id, Side(str(request["side"])), liquidity))
        return tuple(result)

    def _apply_client_payload(self, message: DeliveryMessageV1) -> None:
        payload = dict(message.client_payload)
        market = payload.get("market_state")
        if isinstance(market, Mapping):
            market_time = _exact_int(
                market.get("simulation_time_us"),
                "client market-state cutoff",
            )
            prior = (
                -1
                if self.client_market_state is None
                else _exact_int(
                    self.client_market_state.get("simulation_time_us"),
                    "prior market-state cutoff",
                )
            )
            if market_time >= prior:
                self.client_market_state = dict(market)
        snapshots = payload.get("order_snapshots")
        if isinstance(snapshots, Mapping):
            for order_id, state in snapshots.items():
                if type(order_id) is not str or not isinstance(state, Mapping):
                    raise ValueError("client order snapshot is malformed")
                if message.information_cutoff_us >= self.client_order_cutoffs.get(
                    order_id, -1
                ):
                    self.client_known_orders[order_id] = dict(state)
                    self.client_order_cutoffs[order_id] = (
                        message.information_cutoff_us
                    )
        if message.kind != "FILL_REPORT":
            return
        self.fill_report_cursor += 1
        data = payload.get("event_data")
        if not isinstance(data, Mapping):
            raise ValueError("client fill report omits event data")
        fills = self._reported_player_fills(data)
        if not fills:
            raise ValueError("client fill report has no client-known player order")
        for order_id, side, liquidity in fills:
            self.player_position.apply_reported_fill(
                trade_id=_exact_string(data.get("trade_id"), "trade_id"),
                order_id=order_id,
                side=side,
                price_ticks=_exact_int(
                    data.get("price_ticks"), "price_ticks", minimum=1
                ),
                quantity=_exact_int(data.get("quantity"), "quantity", minimum=1),
                liquidity=liquidity,
            )

    def observe_delivery(
        self,
        message: DeliveryMessageV1,
        *,
        now_us: int,
    ) -> dict[str, object]:
        if type(message) is not DeliveryMessageV1:
            raise TypeError("research observation requires DeliveryMessageV1")
        _exact_int(now_us, "research observation time")
        if message.delivery_time_us != now_us:
            raise ValueError("research observation time differs from client delivery")
        if message.message_id in set(self.processed_message_ids):
            raise ValueError("research observation duplicated a client message")
        if now_us < self.last_observation_time_us:
            raise ValueError("research observation time moved backward")
        self._apply_client_payload(message)
        events = self._simulation_events(message)
        view = self.client_view()
        frame = self.feature_engine.observe(now_us, events, view)
        if isinstance(self.strategy, StateMachineRuntime):
            steps = self.strategy.settle(now_us, events, view)
            strategy_payload = steps[-1].evaluation.as_dict()
        else:
            self.strategy.observe(now_us, events, view)
            if self.strategy.current is None:  # pragma: no cover - reset is unconditional
                raise RuntimeError("traffic-light observation omitted its evaluation")
            strategy_payload = self.strategy.current.as_dict()
        self.delivered_message_cursor += 1
        self.processed_message_ids.append(message.message_id)
        self.last_observation_time_us = now_us
        self.last_information_cutoff_us = max(
            self.last_information_cutoff_us,
            message.information_cutoff_us,
        )
        self.feature_batch_sequence += 1
        feature_batch_id = f"RESEARCH-FEATURE-{self.feature_batch_sequence:010d}"
        batch = {
            "delivery_time_us": now_us,
            "feature_batch_id": feature_batch_id,
            "feature_frame": {
                "simulation_time_us": frame.simulation_time_us,
                "values": frame.as_dict(),
                "windows_us": list(frame.windows_us),
            },
            "feature_frame_sha256": frame.sha256(),
            "information_cutoff_us": message.information_cutoff_us,
            "message_id": message.message_id,
            "observable_event_count": len(events),
            "strategy_state": strategy_payload,
            "strategy_state_sha256": canonical_sha256(strategy_payload),
        }
        self.feature_batches.append(batch)
        self.causal_windows.append(
            {
                "decision_time_us": now_us,
                "feature_batch_id": feature_batch_id,
                "information_cutoff_us": message.information_cutoff_us,
                "message_id": message.message_id,
            }
        )
        return {
            "feature_batch_id": feature_batch_id,
            "information_cutoff_us": message.information_cutoff_us,
            "message_id": message.message_id,
            "observable_event_count": len(events),
            "strategy_state_sha256": canonical_sha256(strategy_payload),
        }

    def observe_strategy_deadline(self, *, now_us: int) -> dict[str, object]:
        expected = self.next_strategy_deadline_us
        if expected is None or expected != now_us:
            raise ValueError("strategy deadline differs from its recorded timer")
        view = self.client_view()
        self.feature_engine.advance_to(now_us, view)
        if isinstance(self.strategy, StateMachineRuntime):
            steps = self.strategy.settle(now_us, (), view)
            strategy_payload = steps[-1].evaluation.as_dict()
        else:
            self.strategy.observe(now_us, (), view)
            if self.strategy.current is None:  # pragma: no cover
                raise RuntimeError("traffic-light deadline omitted its evaluation")
            strategy_payload = self.strategy.current.as_dict()
        self.last_observation_time_us = now_us
        deadline_id = f"RESEARCH-DEADLINE-{len(self.strategy_deadlines) + 1:010d}"
        row = {
            "deadline_id": deadline_id,
            "information_cutoff_us": self.last_information_cutoff_us,
            "simulation_time_us": now_us,
            "strategy_state": strategy_payload,
            "strategy_state_sha256": canonical_sha256(strategy_payload),
        }
        self.strategy_deadlines.append(row)
        return row

    def queue_player_decision(
        self,
        *,
        action: str,
        action_payload: Mapping[str, object],
        decided_at_us: int,
        scheduled_time_us: int,
    ) -> PlayerDecisionV1:
        if action not in _DECISION_ACTIONS:
            raise ValueError("player decision action is unsupported")
        if decided_at_us != self.last_observation_time_us:
            raise ValueError("player decision must bind the current observable cut")
        sequence = self.decision_sequence + 1
        decision = PlayerDecisionV1(
            RESEARCH_SCHEMA_VERSION,
            f"PLAYER-DECISION-{sequence:010d}",
            sequence,
            action,
            decided_at_us,
            scheduled_time_us,
            self.last_information_cutoff_us,
            dict(action_payload),
            None,
            None,
        )
        self.decision_sequence = sequence
        self.pending_decisions[decision.decision_id] = decision
        return decision

    def bind_decision_work(self, decision_id: str, work_id: str) -> PlayerDecisionV1:
        decision = self.pending_decisions[_exact_string(decision_id, "decision_id")]
        if decision.work_id is not None:
            raise ValueError("player decision work is already bound")
        bound = replace(decision, work_id=_exact_string(work_id, "work_id"))
        self.pending_decisions[decision_id] = bound
        return bound

    def take_due_decision(
        self,
        decision_id: str,
        *,
        work_id: str,
        now_us: int,
    ) -> PlayerDecisionV1:
        decision = self.pending_decisions[_exact_string(decision_id, "decision_id")]
        if decision.work_id != work_id or decision.scheduled_time_us != now_us:
            raise ValueError("player decision differs from exact due work")
        del self.pending_decisions[decision_id]
        return decision

    def complete_decision(
        self,
        decision: PlayerDecisionV1,
        *,
        route_work_id: str,
    ) -> PlayerDecisionV1:
        if decision.route_work_id is not None:
            raise ValueError("player decision is already completed")
        completed = replace(
            decision,
            route_work_id=_exact_string(route_work_id, "route_work_id"),
        )
        self.action_sequence += 1
        self.completed_decisions.append(completed.as_dict())
        return completed

    def checkpoint_state(self) -> dict[str, object]:
        state = {
            "action_sequence": self.action_sequence,
            "causal_windows": list(self.causal_windows),
            "client_known_orders": dict(sorted(self.client_known_orders.items())),
            "client_order_cutoffs": dict(sorted(self.client_order_cutoffs.items())),
            "client_market_state": self.client_market_state,
            "completed_decisions": list(self.completed_decisions),
            "configuration": self.configuration.as_dict(),
            "decision_sequence": self.decision_sequence,
            "delivered_message_cursor": self.delivered_message_cursor,
            "feature_batch_sequence": self.feature_batch_sequence,
            "feature_batches": list(self.feature_batches),
            "feature_engine_state": self.feature_engine.runtime_state(),
            "fill_report_cursor": self.fill_report_cursor,
            "last_information_cutoff_us": self.last_information_cutoff_us,
            "last_observation_time_us": self.last_observation_time_us,
            "native_sequence": self.native_sequence,
            "observable_event_sequence": self.observable_event_sequence,
            "pending_decisions": [
                self.pending_decisions[key].as_dict()
                for key in sorted(self.pending_decisions)
            ],
            "player_position": self.player_position.checkpoint_state(),
            "processed_message_ids": list(self.processed_message_ids),
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "strategy_deadlines": list(self.strategy_deadlines),
            "strategy_state": self.strategy.runtime_state(),
            "working_order_ids": list(self.working_order_ids),
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
        delivery: DeliveryOwnerV1 | None = None,
    ) -> ResearchOwnerV1:
        fields = {
            "action_sequence",
            "causal_windows",
            "client_known_orders",
            "client_order_cutoffs",
            "client_market_state",
            "completed_decisions",
            "configuration",
            "decision_sequence",
            "delivered_message_cursor",
            "feature_batch_sequence",
            "feature_batches",
            "feature_engine_state",
            "fill_report_cursor",
            "last_information_cutoff_us",
            "last_observation_time_us",
            "native_sequence",
            "observable_event_sequence",
            "pending_decisions",
            "player_position",
            "processed_message_ids",
            "schema_version",
            "strategy_deadlines",
            "strategy_state",
            "working_order_ids",
        }
        _require_exact_fields(payload, fields, "ResearchOwnerV1")
        if payload["schema_version"] != RESEARCH_SCHEMA_VERSION:
            raise ValueError("research checkpoint schema version is unsupported")
        configuration = ResearchConfigurationV1.from_dict(
            _plain_object(payload["configuration"], "research configuration")
        )
        owner = cls(plan, configuration)
        owner.feature_engine = MicrostructureFeatureEngine.from_runtime_state(
            _plain_object(payload["feature_engine_state"], "feature engine state")
        )
        definition = configuration.strategy_definition
        strategy_state = _plain_object(payload["strategy_state"], "strategy state")
        if type(definition) is StrategyDefinition:
            owner.strategy = TrafficLightRuntime.from_runtime_state(
                definition,
                configuration.relative_volume,
                strategy_state,
            )
        else:
            owner.strategy = StateMachineRuntime.from_runtime_state(
                definition,
                configuration.relative_volume,
                strategy_state,
            )
        owner.player_position = PlayerPosition.from_checkpoint_state(
            _plain_object(payload["player_position"], "player position")
        )
        owner.client_market_state = (
            None
            if payload["client_market_state"] is None
            else _plain_object(payload["client_market_state"], "client market state")
        )
        owner.client_known_orders = {
            _exact_string(key, "client order ID"): _plain_object(
                value,
                f"client order {key}",
            )
            for key, value in _plain_object(
                payload["client_known_orders"],
                "client known orders",
            ).items()
        }
        owner.client_order_cutoffs = {
            _exact_string(key, "client order cutoff ID"): _exact_int(
                value,
                f"client order cutoff {key}",
            )
            for key, value in _plain_object(
                payload["client_order_cutoffs"],
                "client order cutoffs",
            ).items()
        }
        if set(owner.client_order_cutoffs) != set(owner.client_known_orders):
            raise ValueError("research client order cutoff inventory is incomplete")
        for name in (
            "action_sequence",
            "decision_sequence",
            "delivered_message_cursor",
            "feature_batch_sequence",
            "fill_report_cursor",
            "last_information_cutoff_us",
            "last_observation_time_us",
            "native_sequence",
            "observable_event_sequence",
        ):
            setattr(owner, name, _exact_int(payload[name], name))
        for name in (
            "causal_windows",
            "completed_decisions",
            "feature_batches",
            "strategy_deadlines",
        ):
            raw = payload[name]
            if type(raw) is not list or any(not isinstance(row, Mapping) for row in raw):
                raise TypeError(f"research {name} must be an object array")
            setattr(owner, name, [dict(row) for row in raw])
        raw_message_ids = payload["processed_message_ids"]
        if type(raw_message_ids) is not list or any(
            type(value) is not str or not value for value in raw_message_ids
        ):
            raise TypeError("research processed message IDs must be a string array")
        owner.processed_message_ids = list(raw_message_ids)
        raw_pending = payload["pending_decisions"]
        if type(raw_pending) is not list:
            raise TypeError("research pending decisions must be an array")
        decisions = [PlayerDecisionV1.from_dict(row) for row in raw_pending]  # type: ignore[arg-type]
        owner.pending_decisions = {row.decision_id: row for row in decisions}
        if len(owner.pending_decisions) != len(decisions):
            raise ValueError("research pending decision IDs are duplicated")
        raw_working = payload["working_order_ids"]
        if type(raw_working) is not list or raw_working != sorted(set(raw_working)):
            raise ValueError("research working order IDs are not canonical")
        if tuple(raw_working) != owner.working_order_ids:
            raise ValueError("research working order IDs differ from client state")
        owner.assert_invariants(plan, delivery=delivery)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("research checkpoint is not a canonical fixed point")
        return owner

    def assert_invariants(
        self,
        plan: FullDayPlanV1,
        *,
        delivery: DeliveryOwnerV1 | None = None,
    ) -> None:
        self._validate_plan_binding(plan)
        self.player_position.assert_invariants()
        if self.feature_engine.windows_us != self.configuration.feature_windows_us:
            raise RuntimeError("research feature windows differ from configuration")
        feature_state = self.feature_engine.runtime_state()
        strategy_state = self.strategy.runtime_state()
        if feature_state["last_time_us"] != self.last_observation_time_us:
            raise RuntimeError("research feature time differs from observation time")
        strategy_features = strategy_state["feature_windows"]
        if not isinstance(strategy_features, Mapping) or (
            strategy_features["last_time_us"] != self.last_observation_time_us
        ):
            raise RuntimeError("research strategy time differs from observation time")
        if self.last_information_cutoff_us > self.last_observation_time_us:
            raise RuntimeError("research information cutoff lies in the future")
        if set(self.client_order_cutoffs) != set(self.client_known_orders):
            raise RuntimeError("research client order cutoff inventory is incomplete")
        if any(
            cutoff > self.last_information_cutoff_us
            for cutoff in self.client_order_cutoffs.values()
        ):
            raise RuntimeError("research order cutoff exceeds observable information")
        if self.delivered_message_cursor != len(self.processed_message_ids):
            raise RuntimeError("research delivery cursor differs from message IDs")
        if len(self.processed_message_ids) != len(set(self.processed_message_ids)):
            raise RuntimeError("research processed a client message twice")
        if self.feature_batch_sequence != len(self.feature_batches):
            raise RuntimeError("research feature batch allocator is inconsistent")
        if [row.get("message_id") for row in self.feature_batches] != self.processed_message_ids:
            raise RuntimeError("research feature provenance differs from message order")
        if [row.get("feature_batch_id") for row in self.feature_batches] != [
            f"RESEARCH-FEATURE-{sequence:010d}"
            for sequence in range(1, self.feature_batch_sequence + 1)
        ]:
            raise RuntimeError("research feature batch allocator has a gap or reuse")
        if any(
            type(row.get("delivery_time_us")) is not int
            or type(row.get("information_cutoff_us")) is not int
            or row["information_cutoff_us"] > row["delivery_time_us"]
            for row in self.feature_batches
        ):
            raise RuntimeError("research feature provenance violates its causal cut")
        expected_windows = [
            {
                "decision_time_us": row["delivery_time_us"],
                "feature_batch_id": row["feature_batch_id"],
                "information_cutoff_us": row["information_cutoff_us"],
                "message_id": row["message_id"],
            }
            for row in self.feature_batches
        ]
        if self.causal_windows != expected_windows:
            raise RuntimeError("research causal windows differ from feature provenance")
        if self.feature_batches and self.last_information_cutoff_us != max(
            row["information_cutoff_us"] for row in self.feature_batches
        ):
            raise RuntimeError("research information cutoff differs from feature prefix")
        if self.decision_sequence != len(self.pending_decisions) + len(self.completed_decisions):
            raise RuntimeError("research decision lifecycle is not conserved")
        if self.action_sequence != len(self.completed_decisions):
            raise RuntimeError("research action allocator is inconsistent")
        if self.native_sequence != (
            len(self.feature_batches)
            + len(self.strategy_deadlines)
            + len(self.completed_decisions)
        ):
            raise RuntimeError("research native sequence differs from emitted state")
        decision_sequences = {
            decision.decision_sequence for decision in self.pending_decisions.values()
        }
        for raw in self.completed_decisions:
            decision = PlayerDecisionV1.from_dict(raw)
            decision_sequences.add(decision.decision_sequence)
            if decision.route_work_id is None:
                raise RuntimeError("completed player decision lacks route work")
            if (
                decision.decided_at_us > self.last_observation_time_us
                or decision.information_cutoff_us > self.last_information_cutoff_us
            ):
                raise RuntimeError("completed decision exceeds observable research state")
        if decision_sequences != set(range(1, self.decision_sequence + 1)):
            raise RuntimeError("research decision allocator has a gap or reuse")
        if any(decision.work_id is None for decision in self.pending_decisions.values()):
            raise RuntimeError("pending research decision lacks work binding")
        if any(
            decision.decided_at_us > self.last_observation_time_us
            or decision.information_cutoff_us > self.last_information_cutoff_us
            for decision in self.pending_decisions.values()
        ):
            raise RuntimeError("pending decision exceeds observable research state")
        if delivery is not None:
            delivered = [
                DeliveryMessageV1.from_dict(row)
                for row in delivery.delivered_messages
            ]
            if self.processed_message_ids != [row.message_id for row in delivered]:
                raise RuntimeError("research delivery cursor differs from delivered messages")
            if self.client_known_orders != delivery.client_known_orders:
                raise RuntimeError("research client orders differ from delivery state")
            if self.client_order_cutoffs != delivery.client_order_cutoffs:
                raise RuntimeError("research order cutoffs differ from delivery state")
            if self.client_market_state != delivery.latest_market_state:
                raise RuntimeError("research market state differs from delivery state")
            if self.fill_report_cursor != len(delivery.client_fill_reports):
                raise RuntimeError("research fill cursor differs from delivery state")
            if self.player_position.position != delivery.client_position:
                raise RuntimeError("research player position differs from delivery state")
        validate_strict_json(self.checkpoint_state())

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())


class ResearchComponentAdapterV1(FullDayComponentAdapterV1):
    component_id = FEATURE_STRATEGY_PLAYER_COMPONENT
    active_predicate = component_configured_predicate(FEATURE_STRATEGY_PLAYER_COMPONENT)
    dependencies = tuple(
        sorted(
            {
                DELIVERY_ASYNC_COMPONENT,
                FULL_DAY_RUNTIME_COMPONENT,
                MECHANICS_COMPONENT,
            }
        )
    )
    owned_resource_ids = tuple(
        sorted(
            {
                "FEATURE_WINDOWS",
                "PLAYER_DECISION_STATE",
                "PLAYER_POSITION_PROJECTION",
                "STRATEGY_TIMER_STATE",
            }
        )
    )
    borrowed_resource_ids = tuple(
        sorted(
            {
                "CLIENT_KNOWN_WORKING_ORDER_STATE",
                "ORDER_GATEWAY",
                "SIMULATION_CLOCK",
            }
        )
    )
    owned_state_ids = tuple(
        sorted(
            {
                RESEARCH_FEATURES_STATE_ID,
                RESEARCH_PLAYER_STATE_ID,
                RESEARCH_STRATEGY_STATE_ID,
            }
        )
    )

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("research adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not ResearchOwnerV1:
            raise TypeError("research adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=RESEARCH_IMPLEMENTATION_VERSION,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        delivery = context.get("delivery")
        if type(plan) is not FullDayPlanV1 or type(delivery) is not DeliveryOwnerV1:
            raise ValueError("research restore requires exact plan and delivery owner")
        state = snapshot.as_dict()["state"]
        if not isinstance(state, Mapping):
            raise TypeError("research snapshot state is not an object")
        return ResearchOwnerV1.from_checkpoint_state(
            state,
            plan=plan,
            delivery=delivery,
        )


__all__ = [
    "RESEARCH_ENGINE_ID",
    "RESEARCH_FEATURES_STATE_ID",
    "RESEARCH_IMPLEMENTATION_VERSION",
    "RESEARCH_NATIVE_LEDGER_ID",
    "RESEARCH_PLAYER_STATE_ID",
    "RESEARCH_SCHEMA_VERSION",
    "RESEARCH_STRATEGY_STATE_ID",
    "RESEARCH_STRATEGY_VERSION",
    "ClientResearchViewV1",
    "PlayerDecisionV1",
    "ResearchComponentAdapterV1",
    "ResearchConfigurationV1",
    "ResearchOwnerV1",
]
