"""Independent venue wrapper around the hidden-liquidity matching boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.exchange import OrderInstruction, SessionState
from kirby2.exchange.models import OrderOwner, Side
from kirby2.latency import (
    LatencyComponent,
    LatencyDistributionKind,
    LatencyDraw,
    LatencySampler,
)
from kirby2.observability import (
    HiddenLiquidityVenue,
    HiddenOrderRequest,
    LiquidityKind,
    ObservableMarketFeed,
)

from .models import VenueConfig, VenueOrderStatus


VENUE_CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class VenueResponse:
    venue_id: str
    order_id: str
    status: VenueOrderStatus
    requested_quantity: int
    filled_quantity: int = 0
    rejection_reason: str | None = None


class Venue:
    """Owns one matching engine, session state, rules, latency, and economics."""

    def __init__(self, config: VenueConfig, seed: int) -> None:
        self.config = config
        self.engine = HiddenLiquidityVenue(config.hidden_rules)
        self.session_state = config.session_state
        self.latency_sampler = LatencySampler(seed)
        self._player_order_ids: set[str] = set()
        self._closed_player_order_ids: set[str] = set()

    @property
    def venue_id(self) -> str:
        return self.config.venue_id

    @property
    def player_position(self) -> int:
        return self.engine.player_position

    def advance_to(self, simulation_time_us: int) -> None:
        self.engine.advance_to(simulation_time_us)

    def set_session_state(self, state: SessionState) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("venue session state must use SessionState")
        self.session_state = state

    def observable_feed(self) -> ObservableMarketFeed:
        return self.engine.observable_feed()

    def sample_routing_latency(self, purpose: str) -> int:
        return sum(
            self.latency_sampler.sample(
                component,
                self.config.latency_profile.distribution(component),
                self.engine.clock.current_time_us,
                f"{self.venue_id}:{purpose}:{component.value}",
            )
            for component in (
                LatencyComponent.CLIENT_ROUTING,
                LatencyComponent.UPLINK,
                LatencyComponent.GATEWAY,
                LatencyComponent.VENUE_PROCESSING,
            )
        )

    def seed_resting(self, request: HiddenOrderRequest) -> VenueResponse:
        rejection = self._resting_rejection(request)
        if rejection is not None:
            return VenueResponse(
                self.venue_id,
                request.order_id,
                VenueOrderStatus.REJECTED,
                request.quantity,
                rejection_reason=rejection,
            )
        try:
            self.engine.submit_resting(request)
        except ValueError as error:
            return VenueResponse(
                self.venue_id,
                request.order_id,
                VenueOrderStatus.REJECTED,
                request.quantity,
                rejection_reason=f"VENUE_RULE_REJECTION:{error}",
            )
        if request.owner is OrderOwner.PLAYER:
            self._player_order_ids.add(request.order_id)
        return VenueResponse(
            self.venue_id,
            request.order_id,
            VenueOrderStatus.RESTING,
            request.quantity,
        )

    def submit_player_passive(
        self,
        order_id: str,
        side: Side,
        quantity: int,
        price_ticks: int,
    ) -> VenueResponse:
        return self.seed_resting(
            HiddenOrderRequest(
                order_id=order_id,
                side=side,
                kind=LiquidityKind.DISPLAYED_LIMIT,
                owner=OrderOwner.PLAYER,
                account_id="PLAYER",
                quantity=quantity,
                price_ticks=price_ticks,
            )
        )

    def execute_player_market(
        self,
        order_id: str,
        side: Side,
        quantity: int,
        limit_price_ticks: int | None = None,
    ) -> VenueResponse:
        rejection = self._market_rejection()
        if rejection is not None:
            return VenueResponse(
                self.venue_id,
                order_id,
                VenueOrderStatus.REJECTED,
                quantity,
                rejection_reason=rejection,
            )
        filled = self.engine.execute_market(
            order_id,
            side,
            quantity,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER",
            limit_price_ticks=limit_price_ticks,
        )
        status = (
            VenueOrderStatus.FILLED
            if filled == quantity
            else VenueOrderStatus.PARTIALLY_FILLED
            if filled
            else VenueOrderStatus.ACCEPTED
        )
        return VenueResponse(
            self.venue_id,
            order_id,
            status,
            quantity,
            filled,
        )

    def execute_simulated_market(
        self,
        order_id: str,
        side: Side,
        quantity: int,
    ) -> VenueResponse:
        rejection = self._market_rejection()
        if rejection is not None:
            return VenueResponse(
                self.venue_id,
                order_id,
                VenueOrderStatus.REJECTED,
                quantity,
                rejection_reason=rejection,
            )
        filled = self.engine.execute_market(order_id, side, quantity)
        return VenueResponse(
            self.venue_id,
            order_id,
            VenueOrderStatus.FILLED
            if filled == quantity
            else VenueOrderStatus.PARTIALLY_FILLED
            if filled
            else VenueOrderStatus.ACCEPTED,
            quantity,
            filled,
        )

    def cancel_player_order(self, order_id: str) -> VenueResponse:
        if order_id not in self._player_order_ids:
            return VenueResponse(
                self.venue_id,
                order_id,
                VenueOrderStatus.REJECTED,
                0,
                rejection_reason="UNKNOWN_PLAYER_ORDER",
            )
        try:
            quantity = self.engine.cancel(order_id)
        except ValueError:
            self._closed_player_order_ids.add(order_id)
            return VenueResponse(
                self.venue_id,
                order_id,
                VenueOrderStatus.ALREADY_CLOSED,
                0,
            )
        self._closed_player_order_ids.add(order_id)
        return VenueResponse(
            self.venue_id,
            order_id,
            VenueOrderStatus.CANCELLED,
            quantity,
        )

    @property
    def player_order_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._player_order_ids - self._closed_player_order_ids))

    def complete_session(self) -> None:
        self.engine.complete_session()

    def assert_invariants(self) -> None:
        self.engine.assert_invariants()
        if not self._closed_player_order_ids <= self._player_order_ids:
            raise RuntimeError("venue closed-order routing state references an unknown order")
        player_truth_ids = {
            order.request.order_id
            for order in self.engine._ordered_orders()
            if order.request.owner is OrderOwner.PLAYER
        }
        if not self._player_order_ids <= player_truth_ids:
            raise RuntimeError("venue routing state references a non-player resting order")
        draw_sequences = [draw.sequence for draw in self.latency_sampler.draws]
        if draw_sequences != list(range(1, len(draw_sequences) + 1)):
            raise RuntimeError("venue latency draw sequence is not contiguous")
        if any(
            draw.simulation_time_us > self.engine.clock.current_time_us
            for draw in self.latency_sampler.draws
        ):
            raise RuntimeError("venue latency draw lies beyond venue time")
        for draw in self.latency_sampler.draws:
            spec = self.config.latency_profile.distribution(draw.component)
            if (
                draw.distribution is not spec.kind
                or not spec.lower_us <= draw.sampled_latency_us <= spec.upper_us
                or not draw.purpose.startswith(self.venue_id + ":")
            ):
                raise RuntimeError("venue latency draw differs from its owned profile")

    def checkpoint_state(self) -> dict[str, object]:
        """Return the complete venue wrapper and hidden-engine owner state."""

        self.assert_invariants()
        return {
            "closed_player_order_ids": sorted(self._closed_player_order_ids),
            "config": _config_checkpoint_state(self.config),
            "engine": self.engine.checkpoint_state(),
            "latency_rng": _latency_checkpoint_state(self.latency_sampler),
            "player_order_ids": sorted(self._player_order_ids),
            "schema_version": VENUE_CHECKPOINT_SCHEMA_VERSION,
            "session_state": self.session_state.value,
        }

    @classmethod
    def from_checkpoint_state(cls, payload: Mapping[str, object]) -> Venue:
        """Reconstruct a venue without replaying orders, fills, or latency draws."""

        _require_fields(
            payload,
            {
                "closed_player_order_ids",
                "config",
                "engine",
                "latency_rng",
                "player_order_ids",
                "schema_version",
                "session_state",
            },
            "venue checkpoint",
        )
        if payload["schema_version"] != VENUE_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported venue checkpoint schema")
        config = _config_from_checkpoint_state(
            _object(payload["config"], "venue configuration")
        )
        latency = _latency_from_checkpoint_state(
            _object(payload["latency_rng"], "venue latency state")
        )
        restored = cls(config, latency.seed)
        restored.engine = HiddenLiquidityVenue.from_checkpoint_state(
            _object(payload["engine"], "hidden-liquidity engine state")
        )
        if restored.engine.rules != config.hidden_rules:
            raise ValueError("venue configuration and hidden engine rules differ")
        restored.latency_sampler = latency
        restored.session_state = SessionState(
            _string(payload["session_state"], "venue session state")
        )
        restored._player_order_ids = set(
            _canonical_string_array(payload["player_order_ids"], "player order IDs")
        )
        restored._closed_player_order_ids = set(
            _canonical_string_array(
                payload["closed_player_order_ids"], "closed player order IDs"
            )
        )
        restored.assert_invariants()
        if restored.checkpoint_state() != dict(payload):
            raise ValueError("venue checkpoint is not a canonical fixed point")
        return restored

    def routing_state(self) -> dict[str, object]:
        return {
            "closed_player_order_ids": sorted(self._closed_player_order_ids),
            "player_order_ids": sorted(self._player_order_ids),
            "session_state": self.session_state.value,
        }

    def _market_rejection(self) -> str | None:
        if self.session_state is not SessionState.CONTINUOUS:
            return f"SESSION_{self.session_state.value}"
        if OrderInstruction.MARKET not in self.config.supported_instructions:
            return "UNSUPPORTED_MARKET_INSTRUCTION"
        return None

    def _resting_rejection(self, request: HiddenOrderRequest) -> str | None:
        if self.session_state is not SessionState.CONTINUOUS:
            return f"SESSION_{self.session_state.value}"
        if OrderInstruction.LIMIT not in self.config.supported_instructions:
            return "UNSUPPORTED_LIMIT_INSTRUCTION"
        if request.kind is LiquidityKind.HIDDEN_LIMIT and not self.config.hidden_rules.allow_fully_hidden:
            return "FULLY_HIDDEN_NOT_SUPPORTED"
        if request.kind is LiquidityKind.MIDPOINT_HIDDEN and not self.config.hidden_rules.allow_midpoint_hidden:
            return "MIDPOINT_HIDDEN_NOT_SUPPORTED"
        return None


def _config_checkpoint_state(config: VenueConfig) -> dict[str, object]:
    profile = config.latency_profile
    return {
        "expected_fill_probability_bps": config.expected_fill_probability_bps,
        "fees": config.fees.as_dict(),
        "hidden_rules": config.hidden_rules.as_dict(),
        "latency_profile": {
            "components": {
                component.value: {
                    "kind": spec.kind.value,
                    "lower_us": spec.lower_us,
                    "parameters_hex": [value.hex() for value in spec.parameters],
                    "samples_us": list(spec.samples_us),
                    "upper_us": spec.upper_us,
                }
                for component, spec in sorted(
                    profile.components.items(), key=lambda item: item[0].value
                )
            },
            "name": profile.name.value,
            "simulator_only": profile.simulator_only,
        },
        "session_state": config.session_state.value,
        "supported_instructions": sorted(
            value.value for value in config.supported_instructions
        ),
        "venue_id": config.venue_id,
    }


def _config_from_checkpoint_state(payload: Mapping[str, object]) -> VenueConfig:
    _require_fields(
        payload,
        {
            "expected_fill_probability_bps",
            "fees",
            "hidden_rules",
            "latency_profile",
            "session_state",
            "supported_instructions",
            "venue_id",
        },
        "venue configuration",
    )
    raw_profile = _object(payload["latency_profile"], "latency profile")
    _require_fields(
        raw_profile,
        {"components", "name", "simulator_only"},
        "latency profile",
    )
    raw_components = _object(raw_profile["components"], "latency components")
    components: dict[str, object] = {}
    for component_id, raw in raw_components.items():
        spec = _object(raw, f"latency component {component_id}")
        _require_fields(
            spec,
            {"kind", "lower_us", "parameters_hex", "samples_us", "upper_us"},
            f"latency component {component_id}",
        )
        parameters = spec["parameters_hex"]
        samples = spec["samples_us"]
        if type(parameters) is not list or any(type(value) is not str for value in parameters):
            raise TypeError("latency parameters must be hexadecimal string arrays")
        if type(samples) is not list or any(type(value) is not int for value in samples):
            raise TypeError("latency samples must be integer arrays")
        components[component_id] = {
            "kind": _string(spec["kind"], "latency distribution kind"),
            "lower_us": _integer(spec["lower_us"], "latency lower bound"),
            "parameters": [float.fromhex(value) for value in parameters],
            "samples_us": samples,
            "upper_us": _integer(spec["upper_us"], "latency upper bound"),
        }
    standard = {
        **dict(payload),
        "latency_profile": {
            "components": components,
            "name": raw_profile["name"],
            "simulator_only": raw_profile["simulator_only"],
        },
    }
    return VenueConfig.from_dict(standard)


def _latency_checkpoint_state(sampler: LatencySampler) -> dict[str, object]:
    state = sampler.runtime_state()
    gaussian = state["gaussian_cache"]
    if gaussian is not None and type(gaussian) is not float:
        raise RuntimeError("latency Gaussian cache has an invalid type")
    return {
        "draws": state["draws"],
        "gaussian_cache_hex": None if gaussian is None else gaussian.hex(),
        "internal_state": state["internal_state"],
        "random_state_version": state["random_state_version"],
        "seed": state["seed"],
    }


def _latency_from_checkpoint_state(payload: Mapping[str, object]) -> LatencySampler:
    _require_fields(
        payload,
        {
            "draws",
            "gaussian_cache_hex",
            "internal_state",
            "random_state_version",
            "seed",
        },
        "venue latency state",
    )
    seed = _signed_integer(payload["seed"], "latency seed")
    version = _integer(payload["random_state_version"], "random state version")
    internal = payload["internal_state"]
    if type(internal) is not list or any(type(value) is not int for value in internal):
        raise TypeError("latency random internal state must be an integer array")
    gaussian_hex = payload["gaussian_cache_hex"]
    if gaussian_hex is not None and type(gaussian_hex) is not str:
        raise TypeError("latency Gaussian cache must be null or hexadecimal")
    gaussian = None if gaussian_hex is None else float.fromhex(gaussian_hex)
    draws: list[LatencyDraw] = []
    for raw in _object_array(payload["draws"], "latency draws"):
        _require_fields(
            raw,
            {
                "component",
                "distribution",
                "purpose",
                "sampled_latency_us",
                "sequence",
                "simulation_time_us",
            },
            "latency draw",
        )
        draws.append(
            LatencyDraw(
                _integer(raw["sequence"], "latency draw sequence", minimum=1),
                _integer(raw["simulation_time_us"], "latency draw time"),
                LatencyComponent(_string(raw["component"], "latency component")),
                LatencyDistributionKind(
                    _string(raw["distribution"], "latency distribution")
                ),
                _integer(raw["sampled_latency_us"], "sampled latency"),
                _string(raw["purpose"], "latency draw purpose"),
            )
        )
    if [draw.sequence for draw in draws] != list(range(1, len(draws) + 1)):
        raise ValueError("latency draw sequence is not contiguous")
    restored = LatencySampler(seed)
    try:
        restored._rng.setstate((version, tuple(internal), gaussian))
    except (TypeError, ValueError) as error:
        raise ValueError("latency random state is invalid") from error
    restored._draws = draws
    if _latency_checkpoint_state(restored) != dict(payload):
        raise ValueError("latency state is not a canonical fixed point")
    return restored


def _require_fields(
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


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _object_array(value: object, label: str) -> tuple[dict[str, object], ...]:
    if type(value) is not list or any(type(row) is not dict for row in value):
        raise TypeError(f"{label} must be an object array")
    return tuple(value)


def _canonical_string_array(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise TypeError(f"{label} must be a nonempty string array")
    if value != sorted(set(value)):
        raise ValueError(f"{label} must be sorted and unique")
    return tuple(value)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _signed_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a nonempty string")
    return value
