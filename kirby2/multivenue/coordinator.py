"""Deterministic owner of independent venues, routing, and consolidated feeds."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from kirby2.exchange import SessionState
from kirby2.exchange.models import OrderOwner, Side
from kirby2.observability import HiddenOrderRequest, ObservableEventType, TruthEventType
from kirby2.simulation import SimulationClock

from .models import (
    ConsolidatedFeed,
    ConsolidatedTrade,
    CoordinatorEvent,
    CoordinatorEventType,
    ExecutionScore,
    RouteDecision,
    RouteLegExecution,
    RouteStyle,
    RoutedOrderResult,
    RoutingRequest,
    VenueConfig,
    VenueDepth,
    VenueOrderStatus,
    VenueQuote,
    canonical_sha256,
)
from .routers import router_for_policy
from .venue import Venue, VenueResponse


@dataclass(order=True, slots=True)
class _PendingLeg:
    due_time_us: int
    schedule_sequence: int
    route_id: str = field(compare=False)
    leg_index: int = field(compare=False)
    order_id: str = field(compare=False)
    routing_latency_us: int = field(compare=False)


@dataclass(slots=True)
class _RouteState:
    request: RoutingRequest
    decision: RouteDecision
    executions: dict[int, RouteLegExecution] = field(default_factory=dict)


class MarketCoordinator:
    """Coordinates venue truth while routers receive only public snapshots."""

    def __init__(
        self,
        venue_configs: tuple[VenueConfig, ...],
        *,
        seed: int,
        depth_subscriptions: frozenset[str] = frozenset(),
    ) -> None:
        if type(seed) is not int:
            raise TypeError("multi-venue seed must be an integer")
        if not venue_configs:
            raise ValueError("market coordinator requires at least one venue")
        ids = tuple(config.venue_id for config in venue_configs)
        if len(ids) != len(set(ids)):
            raise ValueError("market coordinator venue IDs must be unique")
        tick_values = {config.fees.tick_value_micros for config in venue_configs}
        if len(tick_values) != 1:
            raise ValueError("venues trading one instrument must share one tick value")
        if not depth_subscriptions <= set(ids):
            raise ValueError("depth subscription references an unknown venue")
        self.seed = seed
        self.clock = SimulationClock()
        ordered_configs = tuple(sorted(venue_configs, key=lambda item: item.venue_id))
        self.venues = {
            config.venue_id: Venue(config, seed + (index + 1) * 1_000_003)
            for index, config in enumerate(ordered_configs)
        }
        self.depth_subscriptions = depth_subscriptions
        self._events: list[CoordinatorEvent] = []
        self._routes: dict[str, _RouteState] = {}
        self._pending: list[_PendingLeg] = []
        self._route_sequence = 0
        self._schedule_sequence = 0
        self._global_player_position = 0
        self._complete = False
        self.assert_invariants()

    @property
    def events(self) -> tuple[CoordinatorEvent, ...]:
        return tuple(self._events)

    @property
    def global_player_position(self) -> int:
        return self._global_player_position

    @property
    def complete(self) -> bool:
        return self._complete

    def advance_to(self, target_time_us: int) -> None:
        if type(target_time_us) is not int or target_time_us < self.clock.current_time_us:
            raise ValueError("market coordinator time cannot move backward")
        while self._pending and self._pending[0].due_time_us <= target_time_us:
            due = self._pending[0].due_time_us
            self._advance_venues(due)
            while self._pending and self._pending[0].due_time_us == due:
                self._execute_pending_leg(heapq.heappop(self._pending))
        self._advance_venues(target_time_us)
        self.assert_invariants()

    def consolidated_feed(self) -> ConsolidatedFeed:
        quotes: list[VenueQuote] = []
        trades: list[ConsolidatedTrade] = []
        depths: list[VenueDepth] = []
        for venue_id in sorted(self.venues):
            venue = self.venues[venue_id]
            feed = venue.observable_feed()
            book_events = [
                event
                for event in feed.events
                if event.event_type is ObservableEventType.BOOK_SNAPSHOT
            ]
            latest = None if not book_events else book_events[-1]
            source_time = 0 if latest is None else latest.source_time_us
            received_time = 0 if latest is None else latest.received_time_us
            bid_quantity = (
                0 if not feed.book.bid_levels else feed.book.bid_levels[0].total_quantity
            )
            ask_quantity = (
                0 if not feed.book.ask_levels else feed.book.ask_levels[0].total_quantity
            )
            config = venue.config
            quotes.append(
                VenueQuote(
                    venue_id=venue_id,
                    best_bid_ticks=feed.book.best_bid,
                    best_bid_quantity=bid_quantity,
                    best_ask_ticks=feed.book.best_ask,
                    best_ask_quantity=ask_quantity,
                    quote_source_time_us=source_time,
                    quote_received_time_us=received_time,
                    quote_age_us=self.clock.current_time_us - source_time,
                    expected_routing_latency_us=config.expected_routing_latency_us,
                    expected_fill_probability_bps=config.expected_fill_probability_bps,
                    taker_fee_micros_per_share=config.fees.taker_fee_micros_per_share,
                    maker_rebate_micros_per_share=config.fees.maker_rebate_micros_per_share,
                    tick_value_micros=config.fees.tick_value_micros,
                    supported_instructions=tuple(
                        sorted(value.value for value in config.supported_instructions)
                    ),
                    session_state=venue.session_state,
                )
            )
            if venue_id in self.depth_subscriptions:
                depths.append(VenueDepth(venue_id, feed.book))
            trade_events = {
                str(event.data.get("trade_id")): event
                for event in feed.events
                if event.event_type is ObservableEventType.TRADE
            }
            for trade in feed.tape:
                event = trade_events.get(trade.trade_id)
                trades.append(
                    ConsolidatedTrade(
                        venue_id,
                        trade.trade_id,
                        trade.simulation_time_us,
                        trade.simulation_time_us if event is None else event.received_time_us,
                        trade.price_x2,
                        trade.quantity,
                        trade.aggressor_side,
                    )
                )
        return ConsolidatedFeed(
            self.clock.current_time_us,
            tuple(quotes),
            tuple(
                sorted(
                    trades,
                    key=lambda value: (
                        value.received_time_us,
                        value.venue_id,
                        value.trade_id,
                    ),
                )
            ),
            tuple(depths),
        )

    def add_resting_order(self, venue_id: str, request: HiddenOrderRequest) -> VenueResponse:
        self._require_open()
        venue = self._venue(venue_id)
        response = venue.seed_resting(request)
        self._emit(
            CoordinatorEventType.VENUE_ORDER_ADDED,
            request=request.as_dict(),
            response=_response_dict(response),
            venue_id=venue_id,
        )
        self.assert_invariants()
        return response

    def execute_simulated_market(
        self,
        venue_id: str,
        order_id: str,
        side: Side,
        quantity: int,
    ) -> VenueResponse:
        self._require_open()
        venue = self._venue(venue_id)
        before = venue.player_position
        response = venue.execute_simulated_market(order_id, side, quantity)
        self._emit(
            CoordinatorEventType.VENUE_MARKET_FLOW,
            order_id=order_id,
            quantity=quantity,
            response=_response_dict(response),
            side=side.value,
            venue_id=venue_id,
        )
        self._reconcile_position(before, venue)
        self.assert_invariants()
        return response

    def set_venue_session_state(self, venue_id: str, state: SessionState) -> None:
        self._require_open()
        venue = self._venue(venue_id)
        previous = venue.session_state
        venue.set_session_state(state)
        self._emit(
            CoordinatorEventType.VENUE_SESSION_CHANGED,
            previous_state=previous.value,
            state=state.value,
            venue_id=venue_id,
        )
        self.assert_invariants()

    def submit_route(self, request: RoutingRequest) -> str:
        self._require_open()
        self._route_sequence += 1
        route_id = f"R{self._route_sequence:06d}"
        feed = self.consolidated_feed()
        router = router_for_policy(request.policy)
        decision = router.decide(route_id, request, feed)
        self._routes[route_id] = _RouteState(request, decision)
        self._emit(
            CoordinatorEventType.ROUTE_DECISION,
            decision=decision.as_dict(),
            request=request.as_dict(),
        )
        for index, leg in enumerate(decision.legs, start=1):
            venue = self._venue(leg.venue_id)
            latency = venue.sample_routing_latency(f"route:{route_id}:leg:{index}")
            due = self.clock.current_time_us + latency
            self._schedule_sequence += 1
            order_id = f"{request.order_id}-{route_id}-L{index}"
            heapq.heappush(
                self._pending,
                _PendingLeg(
                    due,
                    self._schedule_sequence,
                    route_id,
                    index,
                    order_id,
                    latency,
                ),
            )
            self._emit(
                CoordinatorEventType.ROUTE_LEG_SCHEDULED,
                arrival_time_us=due,
                leg_index=index,
                order_id=order_id,
                route_id=route_id,
                routing_latency_us=latency,
                venue_id=leg.venue_id,
            )
        self.assert_invariants()
        return route_id

    def route_result(self, route_id: str) -> RoutedOrderResult:
        state = self._route_state(route_id)
        return RoutedOrderResult(
            state.request,
            state.decision,
            tuple(state.executions[index] for index in sorted(state.executions)),
        )

    def cancel_all(self) -> tuple[VenueResponse, ...]:
        self._require_open()
        targets = tuple(
            (venue_id, order_id)
            for venue_id, venue in sorted(self.venues.items())
            for order_id in venue.player_order_ids
        )
        self._emit(
            CoordinatorEventType.CANCEL_ALL_REQUESTED,
            target_count=len(targets),
            targets=[{"order_id": order_id, "venue_id": venue_id} for venue_id, order_id in targets],
        )
        responses: list[VenueResponse] = []
        for venue_id, order_id in targets:
            venue = self.venues[venue_id]
            latency = venue.sample_routing_latency(f"cancel-all:{order_id}")
            self.advance_to(self.clock.current_time_us + latency)
            response = venue.cancel_player_order(order_id)
            responses.append(response)
            self._emit(
                CoordinatorEventType.VENUE_ORDER_CANCELLED,
                response=_response_dict(response),
                venue_id=venue_id,
            )
        self.assert_invariants()
        return tuple(responses)

    def complete_session(self) -> None:
        if self._complete:
            return
        if self._pending:
            self.advance_to(max(item.due_time_us for item in self._pending))
        for venue in self.venues.values():
            venue.complete_session()
        self._reconcile_passive_executions()
        flush_time = self.clock.current_time_us + max(
            venue.config.hidden_rules.feed_delay_us for venue in self.venues.values()
        )
        self._advance_venues(flush_time)
        self._complete = True
        self._emit(CoordinatorEventType.SESSION_COMPLETE)
        self.assert_invariants()

    def post_session_ground_truth(self) -> dict[str, object]:
        if not self._complete:
            raise RuntimeError("multi-venue ground truth is unavailable before completion")
        return {
            "global_player_position": self._global_player_position,
            "label": "SIMULATOR_GROUND_TRUTH_POST_SESSION",
            "simulation_time_us": self.clock.current_time_us,
            "venues": [
                {
                    "latency_draws": [draw.as_dict() for draw in venue.latency_sampler.draws],
                    "state": venue.engine.post_session_ground_truth().as_dict(),
                    "venue_id": venue_id,
                }
                for venue_id, venue in sorted(self.venues.items())
            ],
        }

    def score_route(self, route_id: str) -> ExecutionScore:
        if not self._complete:
            raise RuntimeError("execution scoring requires completed venue truth")
        state = self._route_state(route_id)
        order_to_execution = {
            execution.order_id: execution
            for execution in state.executions.values()
        }
        receipts: list[tuple[str, int, int, bool, RouteLegExecution]] = []
        for venue_id, venue in sorted(self.venues.items()):
            truth = venue.engine.post_session_ground_truth()
            for event in truth.events:
                if event.event_type is not TruthEventType.TRADE:
                    continue
                taker_id = str(event.data["taker_order_id"])
                maker_id = str(event.data["maker_order_id"])
                if taker_id in order_to_execution:
                    receipts.append(
                        (
                            venue_id,
                            int(event.data["price_x2"]),
                            int(event.data["quantity"]),
                            True,
                            order_to_execution[taker_id],
                        )
                    )
                if maker_id in order_to_execution:
                    receipts.append(
                        (
                            venue_id,
                            int(event.data["price_x2"]),
                            int(event.data["quantity"]),
                            False,
                            order_to_execution[maker_id],
                        )
                    )
        quantity = sum(item[2] for item in receipts)
        gross_numerator_x2 = sum(price_x2 * fill for _, price_x2, fill, _, _ in receipts)
        fees = sum(
            self.venues[venue_id].config.fees.taker_fee_micros_per_share * fill
            for venue_id, _, fill, aggressive, _ in receipts
            if aggressive
        )
        rebates = sum(
            self.venues[venue_id].config.fees.maker_rebate_micros_per_share * fill
            for venue_id, _, fill, aggressive, _ in receipts
            if not aggressive
        )
        gross_cash = sum(
            price_x2
            * fill
            * self.venues[venue_id].config.fees.tick_value_micros
            // 2
            for venue_id, price_x2, fill, _, _ in receipts
        )
        net_cost = state.request.side.sign * gross_cash + fees - rebates
        feed = state.decision.observable_feed
        raw_quotes = feed.get("quotes", [])
        if not isinstance(raw_quotes, list):  # pragma: no cover - decision validates source
            raise RuntimeError("route evidence lost venue quotes")
        quote_side = "best_ask" if state.request.side is Side.BUY else "best_bid"
        remaining_observed = {
            str(quote["venue_id"]): int(quote[f"{quote_side}_quantity"])
            for quote in raw_quotes
            if isinstance(quote, dict) and quote.get(f"{quote_side}_ticks") is not None
        }
        observed_prices = {
            str(quote["venue_id"]): int(quote[f"{quote_side}_ticks"])
            for quote in raw_quotes
            if isinstance(quote, dict) and quote.get(f"{quote_side}_ticks") is not None
        }
        fill_by_leg: dict[int, int] = {}
        stale_quantity = 0
        for _, _, fill, _, execution in receipts:
            fill_by_leg[execution.leg_index] = (
                fill_by_leg.get(execution.leg_index, 0) + fill
            )
            if execution.stale_quote_exposure:
                stale_quantity += fill
        missed = 0
        for index, leg in enumerate(state.decision.legs, start=1):
            filled = fill_by_leg.get(index, 0)
            reference = leg.reference_price_ticks
            better = [
                (price, remaining_observed[venue_id])
                for venue_id, price in observed_prices.items()
                if remaining_observed.get(venue_id, 0) > 0
                and reference is not None
                and (
                    price < reference
                    if state.request.side is Side.BUY
                    else price > reference
                )
            ]
            if better and reference is not None and filled:
                best_price = (
                    min(price for price, _ in better)
                    if state.request.side is Side.BUY
                    else max(price for price, _ in better)
                )
                better_quantity = sum(
                    quantity for price, quantity in better if price == best_price
                )
                missed += abs(reference - best_price) * min(filled, better_quantity)
            remaining_observed[leg.venue_id] = max(
                0,
                remaining_observed.get(leg.venue_id, 0) - leg.quantity,
            )
        routing_delay = max(
            (execution.arrival_time_us - state.decision.decision_time_us for execution in state.executions.values()),
            default=0,
        )
        return ExecutionScore(
            route_id=route_id,
            target_quantity=state.request.quantity,
            completed_quantity=quantity,
            gross_price_numerator_x2=gross_numerator_x2,
            gross_price_denominator=quantity,
            fees_micros=fees,
            rebates_micros=rebates,
            net_execution_cost_micros=net_cost,
            routing_delay_us=routing_delay,
            venue_selection_quality=(
                "BEST_OBSERVABLE_OR_EQUIVALENT"
                if missed == 0
                else "WORSE_THAN_OBSERVED_BEST"
            ),
            missed_better_displayed_ticks_x_quantity=missed,
            stale_quote_exposure_quantity=stale_quantity,
        )

    def event_stream_sha256(self) -> str:
        return canonical_sha256([event.as_dict() for event in self._events])

    def state_sha256(self) -> str:
        return canonical_sha256(
            {
                "complete": self._complete,
                "events": [event.as_dict() for event in self._events],
                "feed": self.consolidated_feed().as_dict(),
                "global_player_position": self._global_player_position,
                "pending": [
                    {
                        "due_time_us": item.due_time_us,
                        "leg_index": item.leg_index,
                        "order_id": item.order_id,
                        "route_id": item.route_id,
                        "routing_latency_us": item.routing_latency_us,
                        "schedule_sequence": item.schedule_sequence,
                    }
                    for item in sorted(self._pending)
                ],
                "routes": {
                    route_id: self.route_result(route_id).as_dict()
                    for route_id in sorted(self._routes)
                },
                "seed": self.seed,
                "simulation_time_us": self.clock.current_time_us,
                "venues": {
                    venue_id: {
                        "config": venue.config.as_dict(),
                        "engine_state_sha256": venue.engine.state_sha256(),
                        "latency_draws": [draw.as_dict() for draw in venue.latency_sampler.draws],
                        "session_state": venue.session_state.value,
                    }
                    for venue_id, venue in sorted(self.venues.items())
                },
            }
        )

    def assert_invariants(self) -> None:
        for venue in self.venues.values():
            venue.assert_invariants()
        if self._global_player_position != sum(
            venue.player_position for venue in self.venues.values()
        ):
            raise RuntimeError("global player position does not reconcile across venues")
        sequences = [event.sequence for event in self._events]
        times = [event.simulation_time_us for event in self._events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError("coordinator event sequence is not contiguous")
        if times != sorted(times):
            raise RuntimeError("coordinator event time moved backward")
        if any(item.due_time_us < self.clock.current_time_us for item in self._pending):
            raise RuntimeError("pending route leg is overdue")
        for route_id, state in self._routes.items():
            if state.decision.route_id != route_id:
                raise RuntimeError("route state identity does not reconcile")
            if state.decision.observable_feed_sha256 != canonical_sha256(
                state.decision.observable_feed
            ):
                raise RuntimeError("route decision evidence was mutated")
            if set(state.executions) - set(range(1, len(state.decision.legs) + 1)):
                raise RuntimeError("route execution references a nonexistent leg")

    def _execute_pending_leg(self, pending: _PendingLeg) -> None:
        state = self._route_state(pending.route_id)
        leg = state.decision.legs[pending.leg_index - 1]
        venue = self._venue(leg.venue_id)
        current_feed = venue.observable_feed()
        current_price = (
            current_feed.book.best_ask
            if state.request.style is RouteStyle.AGGRESSIVE
            and state.request.side is Side.BUY
            else current_feed.book.best_bid
            if state.request.style is RouteStyle.AGGRESSIVE
            else current_feed.book.best_bid
            if state.request.side is Side.BUY
            else current_feed.book.best_ask
        )
        stale = leg.observed_quote_age_us > 0 or current_price != leg.reference_price_ticks
        before = venue.player_position
        if state.request.style is RouteStyle.AGGRESSIVE:
            response = venue.execute_player_market(
                pending.order_id,
                state.request.side,
                leg.quantity,
            )
        else:
            if leg.reference_price_ticks is None:
                response = VenueResponse(
                    venue.venue_id,
                    pending.order_id,
                    VenueOrderStatus.REJECTED,
                    leg.quantity,
                    rejection_reason="PASSIVE_PRICE_UNAVAILABLE",
                )
            else:
                response = venue.submit_player_passive(
                    pending.order_id,
                    state.request.side,
                    leg.quantity,
                    leg.reference_price_ticks,
                )
        execution = RouteLegExecution(
            pending.leg_index,
            venue.venue_id,
            pending.order_id,
            leg.quantity,
            response.filled_quantity,
            self.clock.current_time_us,
            pending.routing_latency_us,
            response.status,
            response.rejection_reason,
            stale,
        )
        state.executions[pending.leg_index] = execution
        self._emit(
            CoordinatorEventType.ROUTE_LEG_REJECTED
            if response.status is VenueOrderStatus.REJECTED
            else CoordinatorEventType.ROUTE_LEG_ACCEPTED,
            execution=execution.as_dict(),
            route_id=pending.route_id,
        )
        if response.filled_quantity:
            self._emit(
                CoordinatorEventType.ROUTE_LEG_FILL,
                fill_quantity=response.filled_quantity,
                order_id=pending.order_id,
                route_id=pending.route_id,
                venue_id=venue.venue_id,
            )
        self._reconcile_position(before, venue)

    def _advance_venues(self, target_time_us: int) -> None:
        self.clock.advance_to(target_time_us)
        for venue in self.venues.values():
            venue.advance_to(target_time_us)

    def _reconcile_position(self, before: int, venue: Venue) -> None:
        delta = venue.player_position - before
        if not delta:
            return
        previous = self._global_player_position
        self._global_player_position += delta
        self._emit(
            CoordinatorEventType.GLOBAL_POSITION_CHANGED,
            delta=delta,
            position=self._global_player_position,
            previous_position=previous,
            venue_id=venue.venue_id,
        )

    def _reconcile_passive_executions(self) -> None:
        for route_id, state in sorted(self._routes.items()):
            if state.request.style is not RouteStyle.PASSIVE:
                continue
            for index, execution in sorted(state.executions.items()):
                truth = self.venues[execution.venue_id].engine.post_session_ground_truth()
                order = next(
                    (item for item in truth.orders if item.order_id == execution.order_id),
                    None,
                )
                if order is None:
                    continue
                if order.status == "FILLED":
                    status = VenueOrderStatus.FILLED
                elif order.status == "CANCELLED":
                    status = VenueOrderStatus.CANCELLED
                elif order.filled_quantity:
                    status = VenueOrderStatus.PARTIALLY_FILLED
                else:
                    status = VenueOrderStatus.RESTING
                state.executions[index] = RouteLegExecution(
                    execution.leg_index,
                    execution.venue_id,
                    execution.order_id,
                    execution.requested_quantity,
                    order.filled_quantity,
                    execution.arrival_time_us,
                    execution.routing_latency_us,
                    status,
                    execution.rejection_reason,
                    execution.stale_quote_exposure,
                )
                if order.filled_quantity:
                    self._emit(
                        CoordinatorEventType.ROUTE_LEG_FILL,
                        fill_quantity=order.filled_quantity,
                        order_id=execution.order_id,
                        post_session_reconciled=True,
                        route_id=route_id,
                        venue_id=execution.venue_id,
                    )

    def _emit(self, event_type: CoordinatorEventType, **data: object) -> None:
        self._events.append(
            CoordinatorEvent(
                len(self._events) + 1,
                self.clock.current_time_us,
                event_type,
                dict(data),
            )
        )

    def _venue(self, venue_id: str) -> Venue:
        try:
            return self.venues[venue_id]
        except KeyError as error:
            raise ValueError(f"unknown venue: {venue_id}") from error

    def _route_state(self, route_id: str) -> _RouteState:
        try:
            return self._routes[route_id]
        except KeyError as error:
            raise ValueError(f"unknown route ID: {route_id}") from error

    def _require_open(self) -> None:
        if self._complete:
            raise RuntimeError("multi-venue session is already complete")


def _response_dict(response: VenueResponse) -> dict[str, object]:
    return {
        "filled_quantity": response.filled_quantity,
        "order_id": response.order_id,
        "rejection_reason": response.rejection_reason,
        "requested_quantity": response.requested_quantity,
        "status": response.status.value,
        "venue_id": response.venue_id,
    }
