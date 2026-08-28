"""Passive, restorable delivery scheduling for the authoritative full-day runtime.

``DeliveryOwnerV1`` owns latency draws and two queues only: commands waiting to
reach the venue and immutable client messages waiting to be delivered.  It is
deliberately unable to construct or retain a clock, book, matching engine, or
gateway.  The full-day runtime remains the sole interpreter of due work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from kirby2.latency.distributions import (
    LatencyComponent,
    LatencyDistributionKind,
    LatencyDistributionSpec,
)
from kirby2.latency.profiles import LatencyProfile, LatencyProfileName, get_latency_profile
from kirby2.simulation.rng import SeededRng

from .components import ComponentSnapshotV1, FullDayComponentAdapterV1
from .composition import (
    DELIVERY_ASYNC_COMPONENT,
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


DELIVERY_SCHEMA_VERSION = 1
DELIVERY_IMPLEMENTATION_VERSION = 1
DELIVERY_PROFILE_VERSION = 1
DELIVERY_RNG_LABEL = "full_day/delivery/latency"
DELIVERY_OWNED_STATE_ID = "PENDING_LATENCY_CLIENT_DELIVERY_V1"
DELIVERY_NATIVE_LEDGER_ID = "DELIVERY_ASYNC_EVENTS_V1"

_ROUTE_ACTIONS = frozenset({"CANCEL", "REPLACE", "SUBMIT"})
_MESSAGE_KINDS = frozenset(
    {
        "CANCEL_ACK",
        "EVENT_REPORT",
        "FILL_REPORT",
        "MARKET_STATE",
        "ORDER_ACK",
        "ORDER_REJECT",
        "REPLACE_ACK",
    }
)
_SUPPORTED_ACTIVE_PROFILES = frozenset(
    {
        LatencyProfileName.LOW_LATENCY,
        LatencyProfileName.NORMAL,
        LatencyProfileName.UNSTABLE,
    }
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


@dataclass(frozen=True, slots=True)
class DeliveryConfigurationV1:
    schema_version: int
    configuration_id: str
    configuration_version: int
    latency_profile_name: str
    latency_profile_version: int
    latency_profile_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != DELIVERY_SCHEMA_VERSION:
            raise ValueError("delivery configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        if (
            type(self.latency_profile_version) is not int
            or self.latency_profile_version != DELIVERY_PROFILE_VERSION
        ):
            raise ValueError("delivery latency profile version is unsupported")
        digest = _exact_string(self.latency_profile_sha256, "latency_profile_sha256")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("delivery latency profile digest must be lowercase SHA-256")
        profile = self.profile
        if profile.name not in _SUPPORTED_ACTIVE_PROFILES:
            raise ValueError(
                "active delivery requires LOW_LATENCY, NORMAL, or UNSTABLE; "
                "latency-free compositions omit the component"
            )

    @classmethod
    def from_builtin(
        cls,
        *,
        configuration_id: str,
        configuration_version: int,
        latency_profile_name: str | LatencyProfileName = LatencyProfileName.NORMAL,
    ) -> DeliveryConfigurationV1:
        profile = get_latency_profile(latency_profile_name)
        return cls(
            schema_version=DELIVERY_SCHEMA_VERSION,
            configuration_id=configuration_id,
            configuration_version=configuration_version,
            latency_profile_name=profile.name.value,
            latency_profile_version=DELIVERY_PROFILE_VERSION,
            latency_profile_sha256=canonical_sha256(profile.as_dict()),
        )

    @property
    def profile(self) -> LatencyProfile:
        profile = get_latency_profile(self.latency_profile_name)
        if canonical_sha256(profile.as_dict()) != self.latency_profile_sha256:
            raise ValueError("delivery latency profile digest differs from builtin profile")
        return profile

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
            "latency_profile_name": self.latency_profile_name,
            "latency_profile_sha256": self.latency_profile_sha256,
            "latency_profile_version": self.latency_profile_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DeliveryConfigurationV1:
        validate_strict_json(payload)
        fields = {
            "configuration_id",
            "configuration_version",
            "latency_profile_name",
            "latency_profile_sha256",
            "latency_profile_version",
            "schema_version",
        }
        _require_exact_fields(payload, fields, "DeliveryConfigurationV1")
        return cls(**{field: payload[field] for field in fields})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DeliveryRouteV1:
    schema_version: int
    route_id: str
    route_sequence: int
    action: str
    source_time_us: int
    arrival_time_us: int
    order_id: str
    command_payload: Mapping[str, object]
    work_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != DELIVERY_SCHEMA_VERSION:
            raise ValueError("delivery route schema version must be 1")
        _exact_string(self.route_id, "route_id")
        _exact_int(self.route_sequence, "route_sequence", minimum=1)
        if self.action not in _ROUTE_ACTIONS:
            raise ValueError("delivery route action is unsupported")
        _exact_int(self.source_time_us, "source_time_us")
        if _exact_int(self.arrival_time_us, "arrival_time_us") < self.source_time_us:
            raise ValueError("delivery route arrives before its source")
        _exact_string(self.order_id, "order_id")
        validate_strict_json(self.command_payload)
        _optional_string(self.work_id, "work_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "arrival_time_us": self.arrival_time_us,
            "command_payload": dict(self.command_payload),
            "order_id": self.order_id,
            "route_id": self.route_id,
            "route_sequence": self.route_sequence,
            "schema_version": self.schema_version,
            "source_time_us": self.source_time_us,
            "work_id": self.work_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DeliveryRouteV1:
        fields = {
            "action",
            "arrival_time_us",
            "command_payload",
            "order_id",
            "route_id",
            "route_sequence",
            "schema_version",
            "source_time_us",
            "work_id",
        }
        _require_exact_fields(payload, fields, "DeliveryRouteV1")
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            route_id=payload["route_id"],  # type: ignore[arg-type]
            route_sequence=payload["route_sequence"],  # type: ignore[arg-type]
            action=payload["action"],  # type: ignore[arg-type]
            source_time_us=payload["source_time_us"],  # type: ignore[arg-type]
            arrival_time_us=payload["arrival_time_us"],  # type: ignore[arg-type]
            order_id=payload["order_id"],  # type: ignore[arg-type]
            command_payload=_plain_object(payload["command_payload"], "command_payload"),
            work_id=payload["work_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class DeliveryMessageV1:
    schema_version: int
    message_id: str
    message_sequence: int
    kind: str
    source_time_us: int
    information_cutoff_us: int
    delivery_time_us: int
    causal_outer_event_ids: tuple[str, ...]
    client_payload: Mapping[str, object]
    work_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != DELIVERY_SCHEMA_VERSION:
            raise ValueError("delivery message schema version must be 1")
        _exact_string(self.message_id, "message_id")
        _exact_int(self.message_sequence, "message_sequence", minimum=1)
        if self.kind not in _MESSAGE_KINDS:
            raise ValueError("delivery message kind is unsupported")
        _exact_int(self.source_time_us, "source_time_us")
        cutoff = _exact_int(self.information_cutoff_us, "information_cutoff_us")
        if cutoff > self.source_time_us:
            raise ValueError("delivery cutoff exceeds its source time")
        if _exact_int(self.delivery_time_us, "delivery_time_us") < self.source_time_us:
            raise ValueError("client message arrives before venue truth")
        if (
            type(self.causal_outer_event_ids) is not tuple
            or not self.causal_outer_event_ids
            or any(type(value) is not str or not value for value in self.causal_outer_event_ids)
            or len(set(self.causal_outer_event_ids)) != len(self.causal_outer_event_ids)
        ):
            raise ValueError("delivery causal outer-event IDs must be unique and nonempty")
        validate_strict_json(self.client_payload)
        _optional_string(self.work_id, "work_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "causal_outer_event_ids": list(self.causal_outer_event_ids),
            "client_payload": dict(self.client_payload),
            "delivery_time_us": self.delivery_time_us,
            "information_cutoff_us": self.information_cutoff_us,
            "kind": self.kind,
            "message_id": self.message_id,
            "message_sequence": self.message_sequence,
            "schema_version": self.schema_version,
            "source_time_us": self.source_time_us,
            "work_id": self.work_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DeliveryMessageV1:
        fields = {
            "causal_outer_event_ids",
            "client_payload",
            "delivery_time_us",
            "information_cutoff_us",
            "kind",
            "message_id",
            "message_sequence",
            "schema_version",
            "source_time_us",
            "work_id",
        }
        _require_exact_fields(payload, fields, "DeliveryMessageV1")
        causal = payload["causal_outer_event_ids"]
        if type(causal) is not list:
            raise TypeError("delivery causal IDs must be an array")
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            message_id=payload["message_id"],  # type: ignore[arg-type]
            message_sequence=payload["message_sequence"],  # type: ignore[arg-type]
            kind=payload["kind"],  # type: ignore[arg-type]
            source_time_us=payload["source_time_us"],  # type: ignore[arg-type]
            information_cutoff_us=payload["information_cutoff_us"],  # type: ignore[arg-type]
            delivery_time_us=payload["delivery_time_us"],  # type: ignore[arg-type]
            causal_outer_event_ids=tuple(causal),  # type: ignore[arg-type]
            client_payload=_plain_object(payload["client_payload"], "client_payload"),
            work_id=payload["work_id"],  # type: ignore[arg-type]
        )


class DeliveryOwnerV1:
    """Latency, causal queue, and delayed client-knowledge owner."""

    COMPONENT_ID = DELIVERY_ASYNC_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: DeliveryConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("delivery owner requires FullDayPlanV1")
        if type(configuration) is not DeliveryConfigurationV1:
            raise TypeError("delivery owner requires DeliveryConfigurationV1")
        self.configuration = configuration
        self.rng_label = DELIVERY_RNG_LABEL
        self.rng = SeededRng(plan.seed_policy.derive(self.rng_label))
        self.draws: list[dict[str, object]] = []
        self.route_sequence = 0
        self.message_sequence = 0
        self.native_sequence = 0
        self.pending_routes: dict[str, DeliveryRouteV1] = {}
        self.pending_messages: dict[str, DeliveryMessageV1] = {}
        self.completed_routes: list[dict[str, object]] = []
        self.delivered_messages: list[dict[str, object]] = []
        self.client_known_orders: dict[str, dict[str, object]] = {}
        self.client_order_cutoffs: dict[str, int] = {}
        self.tracked_order_ids: set[str] = set()
        self.client_fill_reports: list[dict[str, object]] = []
        self.client_position = 0
        self.publication_cursor = 0
        self.latest_market_state: dict[str, object] | None = None
        self.last_observed_mechanics_sequence = 0
        self.source_timestamps: dict[str, int] = {}
        self.venue_timestamps: dict[str, int] = {}
        self.client_timestamps: dict[str, int] = {}
        self._validate_plan_binding(plan)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError("delivery configuration is absent from the plan") from error
        if references != (self.configuration.reference,):
            raise ValueError("delivery configuration differs from plan binding")
        if self.rng_label not in {row.semantic_path for row in plan.seed_policy.substreams}:
            raise ValueError("delivery RNG substream is undeclared")

    def _sample(
        self,
        component: LatencyComponent,
        *,
        source_time_us: int,
        purpose: str,
    ) -> int:
        spec: LatencyDistributionSpec = self.configuration.profile.distribution(component)
        before = self.rng.state_sha256()
        if spec.kind is LatencyDistributionKind.FIXED:
            value = spec.lower_us
        elif spec.kind is LatencyDistributionKind.UNIFORM_BOUNDED:
            value = self.rng.integer(spec.lower_us, spec.upper_us)
        elif spec.kind is LatencyDistributionKind.EMPIRICAL_SAMPLES:
            value = spec.samples_us[self.rng.index(len(spec.samples_us))]
        else:
            raise ValueError("active delivery profile is not exactly portable in V1")
        self.draws.append(
            {
                "component": component.value,
                "distribution": spec.kind.value,
                "draw_sequence": len(self.draws) + 1,
                "purpose": purpose,
                "rng_state_after_sha256": self.rng.state_sha256(),
                "rng_state_before_sha256": before,
                "sampled_latency_us": value,
                "source_time_us": source_time_us,
            }
        )
        return value

    def plan_route(
        self,
        *,
        action: str,
        command_payload: Mapping[str, object],
        order_id: str,
        source_time_us: int,
        horizon_us: int,
    ) -> DeliveryRouteV1:
        if action not in _ROUTE_ACTIONS:
            raise ValueError("delivery route action is unsupported")
        _exact_int(source_time_us, "source_time_us")
        _exact_int(horizon_us, "horizon_us", minimum=source_time_us)
        if any(route.order_id == order_id and route.action == action for route in self.pending_routes.values()):
            raise ValueError("duplicate pending delivery route")
        delay = sum(
            self._sample(component, source_time_us=source_time_us, purpose=f"ROUTE_{action}")
            for component in (
                LatencyComponent.INPUT_PROCESSING,
                LatencyComponent.CLIENT_ROUTING,
                LatencyComponent.UPLINK,
                LatencyComponent.GATEWAY,
                LatencyComponent.VENUE_PROCESSING,
            )
        )
        arrival = source_time_us + delay
        if arrival > horizon_us:
            raise ValueError("delivery route arrives beyond the plan horizon")
        sequence = self.route_sequence + 1
        route = DeliveryRouteV1(
            DELIVERY_SCHEMA_VERSION,
            f"DELIVERY-ROUTE-{sequence:010d}",
            sequence,
            action,
            source_time_us,
            arrival,
            _exact_string(order_id, "order_id"),
            dict(command_payload),
            None,
        )
        self.route_sequence = sequence
        self.pending_routes[route.route_id] = route
        self.source_timestamps[route.route_id] = source_time_us
        if action == "SUBMIT":
            self.tracked_order_ids.add(order_id)
        elif action == "REPLACE":
            new_order_id = command_payload.get("new_order_id")
            if type(new_order_id) is not str or not new_order_id:
                raise ValueError("replacement delivery route omits new order ID")
            self.tracked_order_ids.add(new_order_id)
        return route

    def bind_route_work(self, route_id: str, work_id: str) -> DeliveryRouteV1:
        route = self.pending_routes[_exact_string(route_id, "route_id")]
        if route.work_id is not None:
            raise RuntimeError("delivery route work is already bound")
        bound = replace(route, work_id=_exact_string(work_id, "work_id"))
        self.pending_routes[route_id] = bound
        return bound

    def take_due_route(self, route_id: str, *, work_id: str, now_us: int) -> DeliveryRouteV1:
        route = self.pending_routes[_exact_string(route_id, "route_id")]
        if route.work_id != work_id or route.arrival_time_us != now_us:
            raise RuntimeError("delivery route differs from exact due work")
        del self.pending_routes[route_id]
        self.venue_timestamps[route_id] = now_us
        return route

    def complete_route(self, route: DeliveryRouteV1, *, outcome: str) -> None:
        self.completed_routes.append({"outcome": _exact_string(outcome, "outcome"), "route": route.as_dict()})

    @staticmethod
    def _message_kind(event_type: str) -> str:
        return {
            "ORDER_ACCEPTED": "ORDER_ACK",
            "ORDER_REJECTED": "ORDER_REJECT",
            "ORDER_CANCELLED": "CANCEL_ACK",
            "AUCTION_ORDER_CANCELLED": "CANCEL_ACK",
            "ORDER_REPLACED": "REPLACE_ACK",
            "TRADE": "FILL_REPORT",
            "AUCTION_FILL": "FILL_REPORT",
        }.get(event_type, "EVENT_REPORT")

    def observe_mechanics(
        self,
        *,
        mechanics_events: tuple[Mapping[str, object], ...],
        causal_outer_event_ids: tuple[str, ...],
        public_market_cut: Mapping[str, object],
        order_snapshots: Mapping[str, Mapping[str, object]],
        source_time_us: int,
        horizon_us: int,
    ) -> tuple[DeliveryMessageV1, ...]:
        if len(mechanics_events) != len(causal_outer_event_ids):
            raise ValueError("mechanics suffix and causal outer IDs differ")
        created: list[DeliveryMessageV1] = []
        for raw_event, causal_id in zip(mechanics_events, causal_outer_event_ids, strict=True):
            event = _plain_object(raw_event, "mechanics_event")
            sequence = _exact_int(event.get("sequence"), "mechanics sequence", minimum=1)
            if sequence != self.last_observed_mechanics_sequence + 1:
                raise RuntimeError("delivery mechanics observation has a gap or replay")
            event_type = _exact_string(event.get("event_type"), "mechanics event type")
            data = _plain_object(event.get("data"), "mechanics event data")
            kind = self._message_kind(event_type)
            order_ids = tuple(
                sorted(
                    {
                        value
                        for key, value in data.items()
                        if key.endswith("order_id") and type(value) is str
                    }
                )
            )
            relevant = {
                order_id: dict(order_snapshots[order_id])
                for order_id in order_ids
                if order_id in order_snapshots and order_id in self.tracked_order_ids
            }
            self.last_observed_mechanics_sequence = sequence
            if not set(order_ids).intersection(self.tracked_order_ids):
                continue
            component = (
                LatencyComponent.FILL_REPORT
                if kind == "FILL_REPORT"
                else LatencyComponent.DOWNLINK
            )
            delivery_time = min(
                horizon_us,
                source_time_us
                + self._sample(
                    component,
                    source_time_us=source_time_us,
                    purpose=f"MESSAGE_{kind}",
                ),
            )
            message_sequence = self.message_sequence + 1
            message = DeliveryMessageV1(
                DELIVERY_SCHEMA_VERSION,
                f"DELIVERY-MESSAGE-{message_sequence:010d}",
                message_sequence,
                kind,
                source_time_us,
                source_time_us,
                delivery_time,
                (causal_id,),
                {
                    "event_data": data,
                    "event_type": event_type,
                    "mechanics_sequence": sequence,
                    "order_snapshots": relevant,
                },
                None,
            )
            self.message_sequence = message_sequence
            self.pending_messages[message.message_id] = message
            self.source_timestamps[message.message_id] = source_time_us
            created.append(message)

        if mechanics_events:
            publication = self._sample(
                LatencyComponent.MARKET_DATA_PUBLICATION,
                source_time_us=source_time_us,
                purpose="MESSAGE_MARKET_STATE",
            )
            downlink = self._sample(
                LatencyComponent.DOWNLINK,
                source_time_us=source_time_us,
                purpose="MESSAGE_MARKET_STATE",
            )
            render = self._sample(
                LatencyComponent.RENDER,
                source_time_us=source_time_us,
                purpose="MESSAGE_MARKET_STATE",
            )
            message_sequence = self.message_sequence + 1
            message = DeliveryMessageV1(
                DELIVERY_SCHEMA_VERSION,
                f"DELIVERY-MESSAGE-{message_sequence:010d}",
                message_sequence,
                "MARKET_STATE",
                source_time_us,
                source_time_us,
                min(horizon_us, source_time_us + publication + downlink + render),
                causal_outer_event_ids,
                {"market_state": dict(public_market_cut)},
                None,
            )
            self.message_sequence = message_sequence
            self.pending_messages[message.message_id] = message
            self.source_timestamps[message.message_id] = source_time_us
            created.append(message)
        return tuple(created)

    def bind_message_work(self, message_id: str, work_id: str) -> DeliveryMessageV1:
        message = self.pending_messages[_exact_string(message_id, "message_id")]
        if message.work_id is not None:
            raise RuntimeError("delivery message work is already bound")
        payload = message.as_dict()
        payload["work_id"] = _exact_string(work_id, "work_id")
        bound = DeliveryMessageV1.from_dict(payload)
        self.pending_messages[message_id] = bound
        return bound

    def deliver(self, message_id: str, *, work_id: str, now_us: int) -> DeliveryMessageV1:
        message = self.pending_messages[_exact_string(message_id, "message_id")]
        if message.work_id != work_id or message.delivery_time_us != now_us:
            raise RuntimeError("client delivery differs from exact due work")
        del self.pending_messages[message_id]
        payload = dict(message.client_payload)
        raw_orders = payload.get("order_snapshots", {})
        if isinstance(raw_orders, Mapping):
            for order_id, state in raw_orders.items():
                if (
                    type(order_id) is str
                    and isinstance(state, Mapping)
                    and message.information_cutoff_us
                    >= self.client_order_cutoffs.get(order_id, -1)
                ):
                    self.client_known_orders[order_id] = dict(state)
                    self.client_order_cutoffs[order_id] = (
                        message.information_cutoff_us
                    )
        if message.kind == "FILL_REPORT":
            self.client_fill_reports.append(payload)
            data = payload.get("event_data")
            if isinstance(data, Mapping):
                quantity = data.get("quantity")
                if type(quantity) is int:
                    for state in self.client_known_orders.values():
                        request = state.get("request")
                        if not isinstance(request, Mapping):
                            continue
                        order_id = request.get("order_id")
                        if order_id not in {
                            data.get("maker_order_id"),
                            data.get("taker_order_id"),
                        } or request.get("owner") != "player":
                            continue
                        self.client_position += (
                            quantity if request.get("side") == "buy" else -quantity
                        )
        if message.kind == "MARKET_STATE":
            market = payload.get("market_state")
            if not isinstance(market, Mapping):
                raise RuntimeError("market-state message omits its materialized cut")
            prior_time = (
                -1
                if self.latest_market_state is None
                else self.latest_market_state.get("simulation_time_us", -1)
            )
            market_time = market.get("simulation_time_us")
            if type(prior_time) is not int or type(market_time) is not int:
                raise RuntimeError("market-state message has a malformed cutoff")
            if market_time >= prior_time:
                self.latest_market_state = dict(market)
        mechanics_sequence = payload.get("mechanics_sequence")
        if type(mechanics_sequence) is int:
            self.publication_cursor = max(self.publication_cursor, mechanics_sequence)
        self.client_timestamps[message.message_id] = now_us
        self.delivered_messages.append(message.as_dict())
        return message

    def checkpoint_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "client_fill_reports": list(self.client_fill_reports),
            "client_known_orders": dict(sorted(self.client_known_orders.items())),
            "client_order_cutoffs": dict(sorted(self.client_order_cutoffs.items())),
            "client_position": self.client_position,
            "completed_routes": list(self.completed_routes),
            "configuration": self.configuration.as_dict(),
            "delivered_messages": list(self.delivered_messages),
            "draws": list(self.draws),
            "last_observed_mechanics_sequence": self.last_observed_mechanics_sequence,
            "latest_market_state": self.latest_market_state,
            "message_sequence": self.message_sequence,
            "native_sequence": self.native_sequence,
            "pending_messages": [
                self.pending_messages[key].as_dict() for key in sorted(self.pending_messages)
            ],
            "pending_routes": [
                self.pending_routes[key].as_dict() for key in sorted(self.pending_routes)
            ],
            "publication_cursor": self.publication_cursor,
            "rng_label": self.rng_label,
            "rng_state": self.rng.runtime_state(),
            "route_sequence": self.route_sequence,
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "timestamps": {
                "client": dict(sorted(self.client_timestamps.items())),
                "source": dict(sorted(self.source_timestamps.items())),
                "venue": dict(sorted(self.venue_timestamps.items())),
            },
            "tracked_order_ids": sorted(self.tracked_order_ids),
        }
        validate_strict_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
    ) -> DeliveryOwnerV1:
        validate_strict_json(payload)
        fields = {
            "client_fill_reports",
            "client_known_orders",
            "client_order_cutoffs",
            "client_position",
            "completed_routes",
            "configuration",
            "delivered_messages",
            "draws",
            "last_observed_mechanics_sequence",
            "latest_market_state",
            "message_sequence",
            "native_sequence",
            "pending_messages",
            "pending_routes",
            "publication_cursor",
            "rng_label",
            "rng_state",
            "route_sequence",
            "schema_version",
            "timestamps",
            "tracked_order_ids",
        }
        _require_exact_fields(payload, fields, "DeliveryOwnerV1")
        configuration = DeliveryConfigurationV1.from_dict(
            _plain_object(payload["configuration"], "delivery configuration")
        )
        owner = cls(plan, configuration)
        if payload["rng_label"] != DELIVERY_RNG_LABEL:
            raise ValueError("delivery RNG label is unsupported")
        owner.rng = SeededRng.from_runtime_state(
            _plain_object(payload["rng_state"], "delivery RNG state")
        )
        for name in (
            "draws",
            "completed_routes",
            "delivered_messages",
            "client_fill_reports",
        ):
            raw = payload[name]
            if type(raw) is not list or any(not isinstance(row, Mapping) for row in raw):
                raise TypeError(f"delivery {name} must be an object array")
            setattr(owner, name, [dict(row) for row in raw])
        completed_routes: list[dict[str, object]] = []
        for raw_completed in owner.completed_routes:
            _require_exact_fields(
                raw_completed, {"outcome", "route"}, "completed delivery route"
            )
            completed_routes.append(
                {
                    "outcome": _exact_string(
                        raw_completed["outcome"], "completed route outcome"
                    ),
                    "route": DeliveryRouteV1.from_dict(
                        _plain_object(raw_completed["route"], "completed route")
                    ).as_dict(),
                }
            )
        owner.completed_routes = completed_routes
        owner.delivered_messages = [
            DeliveryMessageV1.from_dict(row).as_dict()
            for row in owner.delivered_messages
        ]
        raw_routes = payload["pending_routes"]
        raw_messages = payload["pending_messages"]
        if type(raw_routes) is not list or type(raw_messages) is not list:
            raise TypeError("delivery pending queues must be arrays")
        routes = [DeliveryRouteV1.from_dict(row) for row in raw_routes]  # type: ignore[arg-type]
        messages = [DeliveryMessageV1.from_dict(row) for row in raw_messages]  # type: ignore[arg-type]
        owner.pending_routes = {row.route_id: row for row in routes}
        owner.pending_messages = {row.message_id: row for row in messages}
        if len(owner.pending_routes) != len(routes) or len(owner.pending_messages) != len(messages):
            raise ValueError("delivery pending queues contain duplicate identities")
        owner.route_sequence = _exact_int(payload["route_sequence"], "route_sequence")
        owner.message_sequence = _exact_int(payload["message_sequence"], "message_sequence")
        owner.native_sequence = _exact_int(payload["native_sequence"], "native_sequence")
        owner.last_observed_mechanics_sequence = _exact_int(
            payload["last_observed_mechanics_sequence"], "last_observed_mechanics_sequence"
        )
        owner.publication_cursor = _exact_int(payload["publication_cursor"], "publication_cursor")
        owner.client_position = payload["client_position"]  # validated by invariants
        owner.latest_market_state = (
            None
            if payload["latest_market_state"] is None
            else _plain_object(payload["latest_market_state"], "latest_market_state")
        )
        owner.client_known_orders = {
            key: _plain_object(value, f"client order {key}")
            for key, value in _plain_object(payload["client_known_orders"], "client_known_orders").items()
        }
        owner.client_order_cutoffs = {
            _exact_string(key, "client order cutoff key"): _exact_int(
                value, "client order cutoff"
            )
            for key, value in _plain_object(
                payload["client_order_cutoffs"], "client_order_cutoffs"
            ).items()
        }
        timestamps = _plain_object(payload["timestamps"], "delivery timestamps")
        _require_exact_fields(timestamps, {"client", "source", "venue"}, "delivery timestamps")
        def timestamp_map(value: object, field: str) -> dict[str, int]:
            raw = _plain_object(value, field)
            return {
                _exact_string(key, f"{field} key"): _exact_int(
                    timestamp, f"{field} timestamp"
                )
                for key, timestamp in raw.items()
            }

        owner.client_timestamps = timestamp_map(timestamps["client"], "client timestamps")
        owner.source_timestamps = timestamp_map(timestamps["source"], "source timestamps")
        owner.venue_timestamps = timestamp_map(timestamps["venue"], "venue timestamps")
        raw_tracked = payload["tracked_order_ids"]
        if (
            type(raw_tracked) is not list
            or any(type(value) is not str or not value for value in raw_tracked)
            or raw_tracked != sorted(set(raw_tracked))
        ):
            raise ValueError("delivery tracked order IDs must be sorted and unique")
        owner.tracked_order_ids = set(raw_tracked)
        owner.assert_invariants(plan)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("delivery checkpoint is not a canonical fixed point")
        return owner

    def assert_invariants(self, plan: FullDayPlanV1) -> None:
        self._validate_plan_binding(plan)
        if self.rng.seed != plan.seed_policy.derive(self.rng_label):
            raise RuntimeError("delivery RNG differs from plan substream")
        if type(self.client_position) is not int:
            raise RuntimeError("delivery client position must be an integer")
        if self.route_sequence != len(self.pending_routes) + len(self.completed_routes):
            raise RuntimeError("delivery route lifecycle is not conserved")
        if self.message_sequence != len(self.pending_messages) + len(self.delivered_messages):
            raise RuntimeError("delivery message lifecycle is not conserved")
        completed_routes = tuple(
            DeliveryRouteV1.from_dict(
                _plain_object(row.get("route"), "completed route")
            )
            for row in self.completed_routes
        )
        delivered_messages = tuple(
            DeliveryMessageV1.from_dict(row) for row in self.delivered_messages
        )
        route_sequences = {
            route.route_sequence
            for route in (*self.pending_routes.values(), *completed_routes)
        }
        message_sequences = {
            message.message_sequence
            for message in (*self.pending_messages.values(), *delivered_messages)
        }
        if route_sequences != set(range(1, self.route_sequence + 1)):
            raise RuntimeError("delivery route allocator inventory has a gap or reuse")
        if message_sequences != set(range(1, self.message_sequence + 1)):
            raise RuntimeError("delivery message allocator inventory has a gap or reuse")
        work_ids = [
            item.work_id
            for item in (
                *self.pending_routes.values(),
                *completed_routes,
                *self.pending_messages.values(),
                *delivered_messages,
            )
        ]
        if any(work_id is None for work_id in work_ids) or len(work_ids) != len(
            set(work_ids)
        ):
            raise RuntimeError("delivery work bindings are absent or reused")
        if self.native_sequence != len(completed_routes) + len(delivered_messages):
            raise RuntimeError("delivery native allocator differs from completed work")
        if self.publication_cursor > self.last_observed_mechanics_sequence:
            raise RuntimeError("delivery publication cursor exceeds venue truth cursor")
        if set(self.client_order_cutoffs) != set(self.client_known_orders):
            raise RuntimeError("client order cutoffs differ from known order state")
        for sequence, row in enumerate(self.draws, start=1):
            if row.get("draw_sequence") != sequence:
                raise RuntimeError("delivery draw sequence has a gap")
        if self.draws and self.draws[-1].get("rng_state_after_sha256") != self.rng.state_sha256():
            raise RuntimeError("delivery draw trace tail differs from RNG state")
        for route in self.pending_routes.values():
            if route.work_id is None:
                raise RuntimeError("pending delivery route lacks work binding")
        for message in self.pending_messages.values():
            if message.work_id is None:
                raise RuntimeError("pending delivery message lacks work binding")
        expected_fill_reports = [
            dict(message.client_payload)
            for message in delivered_messages
            if message.kind == "FILL_REPORT"
        ]
        if self.client_fill_reports != expected_fill_reports:
            raise RuntimeError("client fill reports differ from delivered messages")
        all_routes = (*self.pending_routes.values(), *completed_routes)
        all_messages = (*self.pending_messages.values(), *delivered_messages)
        if set(self.source_timestamps) != {
            *(route.route_id for route in all_routes),
            *(message.message_id for message in all_messages),
        }:
            raise RuntimeError("delivery source timestamp inventory is incomplete")
        if set(self.venue_timestamps) != {route.route_id for route in completed_routes}:
            raise RuntimeError("delivery venue timestamp inventory is incomplete")
        if set(self.client_timestamps) != {
            message.message_id for message in delivered_messages
        }:
            raise RuntimeError("delivery client timestamp inventory is incomplete")
        validate_strict_json(self.checkpoint_state())

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())


class DeliveryComponentAdapterV1(FullDayComponentAdapterV1):
    component_id = DELIVERY_ASYNC_COMPONENT
    active_predicate = component_configured_predicate(DELIVERY_ASYNC_COMPONENT)
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                "CLIENT_DELIVERY_QUEUE",
                "CLIENT_KNOWN_WORKING_ORDER_STATE",
                "DELIVERY_MESSAGE_ALLOCATOR",
                "DELIVERY_RNG_SUBSTREAM",
                "VENUE_RECEIPT_QUEUE",
            }
        )
    )
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))
    owned_state_ids = (DELIVERY_OWNED_STATE_ID,)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("delivery adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not DeliveryOwnerV1:
            raise TypeError("delivery adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=DELIVERY_IMPLEMENTATION_VERSION,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if type(plan) is not FullDayPlanV1:
            raise ValueError("delivery restore requires exact plan")
        state = snapshot.as_dict()["state"]
        if not isinstance(state, Mapping):
            raise TypeError("delivery snapshot state is not an object")
        return DeliveryOwnerV1.from_checkpoint_state(state, plan=plan)


__all__ = [
    "DELIVERY_IMPLEMENTATION_VERSION",
    "DELIVERY_NATIVE_LEDGER_ID",
    "DELIVERY_OWNED_STATE_ID",
    "DELIVERY_PROFILE_VERSION",
    "DELIVERY_RNG_LABEL",
    "DELIVERY_SCHEMA_VERSION",
    "DeliveryComponentAdapterV1",
    "DeliveryConfigurationV1",
    "DeliveryMessageV1",
    "DeliveryOwnerV1",
    "DeliveryRouteV1",
]
