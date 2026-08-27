"""Independent venue wrapper around the hidden-liquidity matching boundary."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.exchange import OrderInstruction, SessionState
from kirby2.exchange.models import OrderOwner, Side
from kirby2.latency import LatencyComponent, LatencySampler
from kirby2.observability import (
    HiddenLiquidityVenue,
    HiddenOrderRequest,
    LiquidityKind,
    ObservableMarketFeed,
)

from .models import VenueConfig, VenueOrderStatus


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
            return VenueResponse(
                self.venue_id,
                order_id,
                VenueOrderStatus.ALREADY_CLOSED,
                0,
            )
        return VenueResponse(
            self.venue_id,
            order_id,
            VenueOrderStatus.CANCELLED,
            quantity,
        )

    @property
    def player_order_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._player_order_ids))

    def complete_session(self) -> None:
        self.engine.complete_session()

    def assert_invariants(self) -> None:
        self.engine.assert_invariants()

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
