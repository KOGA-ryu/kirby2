"""Deterministic owner of independent venues, routing, and consolidated feeds."""

from __future__ import annotations

import heapq
import json
from collections.abc import Mapping
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


MULTIVENUE_CHECKPOINT_SCHEMA_VERSION = 1


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
                        "routing_state": venue.routing_state(),
                    }
                    for venue_id, venue in sorted(self.venues.items())
                },
            }
        )

    def branch_runtime_state(self) -> dict[str, object]:
        """Complete fragmented-market state used by counterfactual forks."""

        return {
            "complete": self._complete,
            "events": [event.as_dict() for event in self._events],
            "feed": self.consolidated_feed().as_dict(),
            "global_player_position": self._global_player_position,
            "pending_latency_messages": [
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
            "state_sha256": self.state_sha256(),
            "venues": {
                venue_id: {
                    "config": venue.config.as_dict(),
                    "engine_state": venue.engine.branch_runtime_state(),
                    "engine_state_sha256": venue.engine.state_sha256(),
                    "latency_rng": venue.latency_sampler.runtime_state(),
                    "observable_feed": venue.observable_feed().as_dict(),
                    "player_position": venue.player_position,
                    "routing_state": venue.routing_state(),
                    "session_state": venue.session_state.value,
                }
                for venue_id, venue in sorted(self.venues.items())
            },
            "working_orders": [
                {
                    **order.as_dict(),
                    "venue_id": venue_id,
                }
                for venue_id, venue in sorted(self.venues.items())
                for order in venue.observable_feed().own_orders
                if order.remaining_quantity > 0
            ],
        }

    def checkpoint_state(self) -> dict[str, object]:
        """Return the complete fragmented-market owner state for exact restore."""

        self.assert_invariants()
        state: dict[str, object] = {
            "allocators": {
                "route_sequence": self._route_sequence,
                "schedule_sequence": self._schedule_sequence,
            },
            "clock": self.clock.checkpoint_state(),
            "complete": self._complete,
            "consolidated_feed_state": self.consolidated_feed().as_dict(),
            "depth_subscriptions": sorted(self.depth_subscriptions),
            "events": [event.as_dict() for event in self._events],
            "global_player_position": self._global_player_position,
            "observable_cursors": {
                venue_id: len(venue.engine._observable_events)
                for venue_id, venue in sorted(self.venues.items())
            },
            "pending_route_legs": [
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
                route_id: {
                    "decision": state.decision.as_dict(),
                    "executions": [
                        state.executions[index].as_dict()
                        for index in sorted(state.executions)
                    ],
                    "request": state.request.as_dict(),
                }
                for route_id, state in sorted(self._routes.items())
            },
            "schema_version": MULTIVENUE_CHECKPOINT_SCHEMA_VERSION,
            "seed": self.seed,
            "truth_cursors": {
                venue_id: len(venue.engine._truth_events)
                for venue_id, venue in sorted(self.venues.items())
            },
            "venues": {
                venue_id: venue.checkpoint_state()
                for venue_id, venue in sorted(self.venues.items())
            },
        }
        _validate_multivenue_checkpoint_json(state)
        return state

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> MarketCoordinator:
        """Reconstruct every venue and coordinator owner without prefix replay."""

        _require_checkpoint_fields(
            payload,
            {
                "allocators",
                "clock",
                "complete",
                "consolidated_feed_state",
                "depth_subscriptions",
                "events",
                "global_player_position",
                "observable_cursors",
                "pending_route_legs",
                "routes",
                "schema_version",
                "seed",
                "truth_cursors",
                "venues",
            },
            "multi-venue checkpoint",
        )
        _validate_multivenue_checkpoint_json(payload)
        if payload["schema_version"] != MULTIVENUE_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported multi-venue checkpoint schema")
        raw_venues = _checkpoint_object(payload["venues"], "multi-venue venues")
        if not raw_venues:
            raise ValueError("multi-venue checkpoint requires at least one venue")
        venues: dict[str, Venue] = {}
        for venue_id, raw in sorted(raw_venues.items()):
            restored_venue = Venue.from_checkpoint_state(
                _checkpoint_object(raw, f"venue {venue_id}")
            )
            if restored_venue.venue_id != venue_id:
                raise ValueError("multi-venue checkpoint venue key differs from config")
            venues[venue_id] = restored_venue
        subscriptions = _checkpoint_string_array(
            payload["depth_subscriptions"], "depth subscriptions"
        )
        seed = _checkpoint_signed_int(payload["seed"], "multi-venue seed")
        restored = cls(
            tuple(venue.config for venue in venues.values()),
            seed=seed,
            depth_subscriptions=frozenset(subscriptions),
        )
        restored.clock = SimulationClock.from_checkpoint_state(
            _checkpoint_object(payload["clock"], "multi-venue clock")
        )
        restored.venues = venues
        restored._events = [
            CoordinatorEvent.from_dict(row)
            for row in _checkpoint_object_array(payload["events"], "coordinator events")
        ]
        routes: dict[str, _RouteState] = {}
        raw_routes = _checkpoint_object(payload["routes"], "multi-venue routes")
        for route_id, raw in sorted(raw_routes.items()):
            route = _checkpoint_object(raw, f"route {route_id}")
            _require_checkpoint_fields(
                route,
                {"decision", "executions", "request"},
                f"route {route_id}",
            )
            request_payload = _checkpoint_object(
                route["request"], f"route {route_id} request"
            )
            _require_checkpoint_fields(
                request_payload,
                {
                    "direct_venue_id",
                    "limit_price_ticks",
                    "max_venues",
                    "order_id",
                    "policy",
                    "quantity",
                    "side",
                    "style",
                },
                f"route {route_id} request",
            )
            request = RoutingRequest.from_dict(request_payload)
            decision = RouteDecision.from_dict(
                _checkpoint_object(route["decision"], f"route {route_id} decision")
            )
            if decision.route_id != route_id:
                raise ValueError("route decision ID differs from route key")
            executions = [
                RouteLegExecution.from_dict(row)
                for row in _checkpoint_object_array(
                    route["executions"], f"route {route_id} executions"
                )
            ]
            by_index = {execution.leg_index: execution for execution in executions}
            if len(by_index) != len(executions):
                raise ValueError("route execution leg indexes are duplicated")
            routes[route_id] = _RouteState(request, decision, by_index)
        restored._routes = routes
        restored._pending = []
        for raw in _checkpoint_object_array(
            payload["pending_route_legs"], "pending route legs"
        ):
            _require_checkpoint_fields(
                raw,
                {
                    "due_time_us",
                    "leg_index",
                    "order_id",
                    "route_id",
                    "routing_latency_us",
                    "schedule_sequence",
                },
                "pending route leg",
            )
            restored._pending.append(
                _PendingLeg(
                    due_time_us=_checkpoint_int(raw["due_time_us"], "pending due time"),
                    schedule_sequence=_checkpoint_int(
                        raw["schedule_sequence"], "pending schedule sequence", minimum=1
                    ),
                    route_id=_checkpoint_string(raw["route_id"], "pending route ID"),
                    leg_index=_checkpoint_int(
                        raw["leg_index"], "pending leg index", minimum=1
                    ),
                    order_id=_checkpoint_string(raw["order_id"], "pending order ID"),
                    routing_latency_us=_checkpoint_int(
                        raw["routing_latency_us"], "pending routing latency"
                    ),
                )
            )
        heapq.heapify(restored._pending)
        allocators = _checkpoint_object(payload["allocators"], "multi-venue allocators")
        _require_checkpoint_fields(
            allocators,
            {"route_sequence", "schedule_sequence"},
            "multi-venue allocators",
        )
        restored._route_sequence = _checkpoint_int(
            allocators["route_sequence"], "route allocator"
        )
        restored._schedule_sequence = _checkpoint_int(
            allocators["schedule_sequence"], "schedule allocator"
        )
        restored._global_player_position = _checkpoint_signed_int(
            payload["global_player_position"], "global player position"
        )
        if type(payload["complete"]) is not bool:
            raise TypeError("multi-venue completion state must be boolean")
        restored._complete = payload["complete"]
        _validate_cursor_projection(
            payload["observable_cursors"],
            {
                venue_id: len(venue.engine._observable_events)
                for venue_id, venue in venues.items()
            },
            "observable cursors",
        )
        _validate_cursor_projection(
            payload["truth_cursors"],
            {
                venue_id: len(venue.engine._truth_events)
                for venue_id, venue in venues.items()
            },
            "truth cursors",
        )
        restored.assert_invariants()
        if restored.consolidated_feed().as_dict() != payload["consolidated_feed_state"]:
            raise ValueError("restored consolidated feed differs from checkpoint")
        if restored.checkpoint_state() != dict(payload):
            raise ValueError("multi-venue checkpoint is not a canonical fixed point")
        return restored

    def assert_invariants(self) -> None:
        for venue in self.venues.values():
            venue.assert_invariants()
        if any(
            venue.engine.clock.current_time_us != self.clock.current_time_us
            for venue in self.venues.values()
        ):
            raise RuntimeError("venue and coordinator clocks differ")
        if any(venue.engine.complete != self._complete for venue in self.venues.values()):
            raise RuntimeError("venue and coordinator completion states differ")
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
        route_ids = tuple(sorted(self._routes))
        expected_route_ids = tuple(
            f"R{sequence:06d}" for sequence in range(1, self._route_sequence + 1)
        )
        if route_ids != expected_route_ids:
            raise RuntimeError("route allocator or route identity is inconsistent")
        scheduled_events = sum(
            event.event_type is CoordinatorEventType.ROUTE_LEG_SCHEDULED
            for event in self._events
        )
        if self._schedule_sequence != scheduled_events:
            raise RuntimeError("route schedule allocator differs from event history")
        pending_sequences = [item.schedule_sequence for item in self._pending]
        if len(pending_sequences) != len(set(pending_sequences)) or any(
            sequence <= 0 or sequence > self._schedule_sequence
            for sequence in pending_sequences
        ):
            raise RuntimeError("pending route schedule sequences are invalid")
        for route_id, state in self._routes.items():
            if state.decision.route_id != route_id:
                raise RuntimeError("route state identity does not reconcile")
            if state.decision.policy is not state.request.policy:
                raise RuntimeError("route decision policy differs from its request")
            if sum(leg.quantity for leg in state.decision.legs) > state.request.quantity:
                raise RuntimeError("route plan exceeds its requested quantity")
            if any(leg.venue_id not in self.venues for leg in state.decision.legs):
                raise RuntimeError("route plan references an unknown venue")
            if state.decision.observable_feed_sha256 != canonical_sha256(
                state.decision.observable_feed
            ):
                raise RuntimeError("route decision evidence was mutated")
            if set(state.executions) - set(range(1, len(state.decision.legs) + 1)):
                raise RuntimeError("route execution references a nonexistent leg")
            if state.decision.decision_time_us > self.clock.current_time_us:
                raise RuntimeError("route decision lies beyond coordinator time")
            pending_for_route = {
                item.leg_index: item
                for item in self._pending
                if item.route_id == route_id
            }
            if len(pending_for_route) != sum(
                item.route_id == route_id for item in self._pending
            ):
                raise RuntimeError("route has duplicate pending leg indexes")
            expected_legs = set(range(1, len(state.decision.legs) + 1))
            if set(state.executions) | set(pending_for_route) != expected_legs:
                raise RuntimeError("route lifecycle does not cover every planned leg")
            if set(state.executions) & set(pending_for_route):
                raise RuntimeError("route leg is both pending and executed")
            for leg_index, pending in pending_for_route.items():
                leg = state.decision.legs[leg_index - 1]
                if (
                    pending.order_id
                    != f"{state.request.order_id}-{route_id}-L{leg_index}"
                    or leg.venue_id not in self.venues
                    or pending.due_time_us
                    != state.decision.decision_time_us + pending.routing_latency_us
                ):
                    raise RuntimeError("pending route leg identity or causal time differs")
            for leg_index, execution in state.executions.items():
                leg = state.decision.legs[leg_index - 1]
                if (
                    execution.venue_id != leg.venue_id
                    or execution.requested_quantity != leg.quantity
                    or execution.arrival_time_us > self.clock.current_time_us
                ):
                    raise RuntimeError("route execution differs from its planned leg")
        if any(item.route_id not in self._routes for item in self._pending):
            raise RuntimeError("pending route leg references an orphan route")
        public_feed = json.dumps(
            self.consolidated_feed().as_dict(), sort_keys=True, separators=(",", ":")
        ).lower()
        if any(
            field in public_feed
            for field in (
                "reserve_quantity",
                "reserve_remaining",
                "hidden_quantity",
                "hidden_remaining",
                "priority_sequence",
                "ground_truth",
            )
        ):
            raise RuntimeError("consolidated observable feed leaked venue truth")

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
                state.request.limit_price_ticks,
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


def _validate_cursor_projection(
    value: object,
    expected: Mapping[str, int],
    label: str,
) -> None:
    payload = _checkpoint_object(value, label)
    if set(payload) != set(expected):
        raise ValueError(f"{label} venue inventory differs")
    actual = {
        venue_id: _checkpoint_int(payload[venue_id], f"{label} {venue_id}")
        for venue_id in sorted(payload)
    }
    if actual != dict(sorted(expected.items())):
        raise ValueError(f"{label} differ from venue event prefixes")


def _validate_multivenue_checkpoint_json(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        raise TypeError("binary floats are forbidden in multi-venue checkpoints")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("multi-venue checkpoint keys must be strings")
        for item in value.values():
            _validate_multivenue_checkpoint_json(item)
        return
    if type(value) in {list, tuple}:
        for item in value:
            _validate_multivenue_checkpoint_json(item)
        return
    raise TypeError(f"unsupported multi-venue checkpoint value: {type(value).__name__}")


def _require_checkpoint_fields(
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


def _checkpoint_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _checkpoint_object_array(
    value: object, label: str
) -> tuple[dict[str, object], ...]:
    if type(value) is not list or any(type(row) is not dict for row in value):
        raise TypeError(f"{label} must be an object array")
    return tuple(value)


def _checkpoint_string_array(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise TypeError(f"{label} must be a string array")
    if value != sorted(set(value)):
        raise ValueError(f"{label} must be sorted and unique")
    return tuple(value)


def _checkpoint_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _checkpoint_signed_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


def _checkpoint_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a nonempty string")
    return value
