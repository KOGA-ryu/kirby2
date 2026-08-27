"""Deterministic fragmented-market routing drills."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.exchange.models import OrderOwner, Side
from kirby2.latency import LatencyProfileName, get_latency_profile
from kirby2.observability import (
    HiddenLiquidityRules,
    HiddenOrderRequest,
    LiquidityKind,
    QueueDataMode,
    QueuePositionEstimator,
)

from .coordinator import MarketCoordinator
from .models import (
    CoordinatorEvent,
    RoutePolicy,
    RouteStyle,
    RoutingRequest,
    VenueConfig,
    VenueFeeSchedule,
)
from .replay import (
    MultiVenueCommand,
    MultiVenueRecording,
    MultiVenueReplayReport,
    recording_json_round_trip,
    replay_multivenue_recording,
)


MULTIVENUE_SCENARIOS = (
    "better-price-poor-fill-probability",
    "deep-slow-versus-shallow-fast",
    "sweep-during-momentum",
    "passive-routing-two-venues",
    "stale-composite-quote",
    "partial-multi-venue-completion",
)


@dataclass(frozen=True, slots=True)
class MultiVenueScenarioResult:
    name: str
    coordinator: MarketCoordinator
    recording: MultiVenueRecording
    replay: MultiVenueReplayReport
    route_ids: tuple[str, ...]
    summary: dict[str, object]

    @property
    def timeline(self) -> str:
        return render_routing_timeline(self.coordinator.events)


class MultiVenueScenarioBuilder:
    def __init__(
        self,
        configs: tuple[VenueConfig, ...],
        *,
        seed: int = 42,
        subscriptions: frozenset[str] = frozenset(),
    ) -> None:
        self.coordinator = MarketCoordinator(
            configs,
            seed=seed,
            depth_subscriptions=subscriptions,
        )
        self.commands: list[MultiVenueCommand] = []
        self.route_ids: list[str] = []

    def add(self, time_us: int, venue_id: str, request: HiddenOrderRequest) -> None:
        self.coordinator.advance_to(time_us)
        self.coordinator.add_resting_order(venue_id, request)
        self._record(time_us, "ADD", {"request": request.as_dict(), "venue_id": venue_id})

    def route(self, time_us: int, request: RoutingRequest) -> str:
        self.coordinator.advance_to(time_us)
        route_id = self.coordinator.submit_route(request)
        self.route_ids.append(route_id)
        self._record(time_us, "ROUTE", {"request": request.as_dict()})
        return route_id

    def simulated_market(
        self,
        time_us: int,
        venue_id: str,
        order_id: str,
        side: Side,
        quantity: int,
    ) -> None:
        self.coordinator.advance_to(time_us)
        self.coordinator.execute_simulated_market(venue_id, order_id, side, quantity)
        self._record(
            time_us,
            "SIM_MARKET",
            {
                "order_id": order_id,
                "quantity": quantity,
                "side": side.value,
                "venue_id": venue_id,
            },
        )

    def advance(self, time_us: int) -> None:
        self.coordinator.advance_to(time_us)
        self._record(time_us, "ADVANCE", {})

    def cancel_all(self, time_us: int) -> None:
        self.coordinator.advance_to(time_us)
        command_time = self.coordinator.clock.current_time_us
        self.coordinator.cancel_all()
        self._record(command_time, "CANCEL_ALL", {})

    def complete(self, time_us: int) -> None:
        self.coordinator.advance_to(time_us)
        command_time = self.coordinator.clock.current_time_us
        self.coordinator.complete_session()
        self._record(command_time, "COMPLETE", {})

    def finish(self, name: str, summary: dict[str, object]) -> MultiVenueScenarioResult:
        recording = recording_json_round_trip(
            MultiVenueRecording.capture(
                self.coordinator,
                tuple(self.commands),
                tuple(self.route_ids),
            )
        )
        replay = replay_multivenue_recording(recording)
        if not replay.passed:
            raise RuntimeError(f"multi-venue exact replay failed: {name}")
        return MultiVenueScenarioResult(
            name,
            self.coordinator,
            recording,
            replay,
            tuple(self.route_ids),
            summary,
        )

    def _record(self, time_us: int, command_type: str, parameters: dict[str, object]) -> None:
        self.commands.append(
            MultiVenueCommand(
                len(self.commands) + 1,
                time_us,
                command_type,
                parameters,
            )
        )


def run_multivenue_scenario(name: str) -> MultiVenueScenarioResult:
    runners = {
        "better-price-poor-fill-probability": _better_price_poor_fill,
        "deep-slow-versus-shallow-fast": _deep_slow_shallow_fast,
        "sweep-during-momentum": _sweep_momentum,
        "passive-routing-two-venues": _passive_two_venues,
        "stale-composite-quote": _stale_composite,
        "partial-multi-venue-completion": _partial_completion,
    }
    try:
        return runners[name]()
    except KeyError as error:
        raise ValueError(f"unknown multi-venue scenario: {name}") from error


def run_all_multivenue_scenarios() -> tuple[MultiVenueScenarioResult, ...]:
    return tuple(run_multivenue_scenario(name) for name in MULTIVENUE_SCENARIOS)


def _better_price_poor_fill() -> MultiVenueScenarioResult:
    builder = MultiVenueScenarioBuilder(
        (
            _config("CHEAP", "LOW_LATENCY", fill_bps=1_000, taker_fee=50),
            _config("RELIABLE", "LOW_LATENCY", fill_bps=9_800, taker_fee=20),
        ),
        subscriptions=frozenset({"CHEAP", "RELIABLE"}),
    )
    _seed_two_sided(builder, "CHEAP", 99, 100, 200)
    _seed_two_sided(builder, "RELIABLE", 99, 101, 200)
    route_id = builder.route(
        1_000,
        RoutingRequest(
            "POOR-FILL",
            Side.BUY,
            100,
            RoutePolicy.LATENCY_AWARE,
        ),
    )
    builder.simulated_market(1_100, "CHEAP", "CHEAP-QUEUE-LOSS", Side.BUY, 200)
    builder.advance(2_000)
    builder.complete(3_000)
    result = builder.coordinator.route_result(route_id)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "better-price-poor-fill-probability",
        {
            "best_displayed_venue": "CHEAP",
            "better_price_depleted_before_selected_arrival": True,
            "completed_quantity": score.completed_quantity,
            "selected_venue": result.decision.legs[0].venue_id,
            "selection_used_hidden_state": False,
        },
    )


def _deep_slow_shallow_fast() -> MultiVenueScenarioResult:
    builder = MultiVenueScenarioBuilder(
        (
            _config("DEEP_SLOW", "NORMAL", fill_bps=5_000),
            _config("SHALLOW_FAST", "LOW_LATENCY", fill_bps=9_800),
        ),
        subscriptions=frozenset({"DEEP_SLOW", "SHALLOW_FAST"}),
    )
    _seed_two_sided(builder, "DEEP_SLOW", 99, 100, 500)
    _seed_two_sided(builder, "SHALLOW_FAST", 99, 101, 75)
    route_id = builder.route(
        1_000,
        RoutingRequest("SPEED-DEPTH", Side.BUY, 200, RoutePolicy.LATENCY_AWARE),
    )
    builder.advance(5_000)
    builder.complete(6_000)
    result = builder.coordinator.route_result(route_id)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "deep-slow-versus-shallow-fast",
        {
            "completed_quantity": score.completed_quantity,
            "deep_expected_latency_us": builder.coordinator.venues["DEEP_SLOW"].config.expected_routing_latency_us,
            "deep_visible_quantity": 500,
            "selected_venue": result.decision.legs[0].venue_id,
            "shallow_expected_latency_us": builder.coordinator.venues["SHALLOW_FAST"].config.expected_routing_latency_us,
            "shallow_visible_quantity": 75,
        },
    )


def _sweep_momentum() -> MultiVenueScenarioResult:
    builder = MultiVenueScenarioBuilder(
        (
            _config("SLOW", "NORMAL", fill_bps=9_000),
            _config("FAST", "LOW_LATENCY", fill_bps=9_000),
        ),
        subscriptions=frozenset({"SLOW", "FAST"}),
    )
    _seed_two_sided(builder, "SLOW", 99, 100, 200)
    _seed_two_sided(builder, "FAST", 99, 101, 150)
    route_id = builder.route(
        1_000,
        RoutingRequest(
            "MOMENTUM-SWEEP",
            Side.BUY,
            300,
            RoutePolicy.SWEEP,
            max_venues=2,
        ),
    )
    builder.simulated_market(1_200, "SLOW", "MOMENTUM-TAKER", Side.BUY, 200)
    builder.advance(5_000)
    builder.complete(6_000)
    result = builder.coordinator.route_result(route_id)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "sweep-during-momentum",
        {
            "completed_quantity": score.completed_quantity,
            "leg_arrival_order": [
                item.venue_id for item in sorted(result.executions, key=lambda item: item.arrival_time_us)
            ],
            "stale_exposure_quantity": score.stale_quote_exposure_quantity,
            "target_quantity": 300,
        },
    )


def _passive_two_venues() -> MultiVenueScenarioResult:
    builder = MultiVenueScenarioBuilder(
        (
            _config("ALPHA", "ZERO_LATENCY", maker_rebate=20),
            _config("BETA", "ZERO_LATENCY", maker_rebate=30),
        ),
        subscriptions=frozenset({"ALPHA", "BETA"}),
    )
    _seed_two_sided(builder, "ALPHA", 99, 101, 100)
    _seed_two_sided(builder, "BETA", 99, 101, 300)
    route_id = builder.route(
        1_000,
        RoutingRequest(
            "PASSIVE-TWO",
            Side.BUY,
            200,
            RoutePolicy.PASSIVE_QUEUE,
            style=RouteStyle.PASSIVE,
            max_venues=2,
        ),
    )
    builder.advance(1_000)
    result = builder.coordinator.route_result(route_id)
    estimates: dict[str, int] = {}
    estimator = QueuePositionEstimator()
    for execution in result.executions:
        feed = builder.coordinator.venues[execution.venue_id].observable_feed()
        estimate = estimator.estimate(
            feed,
            execution.order_id,
            data_mode=QueueDataMode.AGGREGATED_DEPTH,
        )
        estimates[execution.venue_id] = estimate.estimated_quantity_ahead
    builder.simulated_market(1_100, "ALPHA", "SELL-A", Side.SELL, 120)
    builder.simulated_market(1_100, "BETA", "SELL-B", Side.SELL, 320)
    builder.cancel_all(1_200)
    builder.complete(1_300)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "passive-routing-two-venues",
        {
            "cancel_all_venues": ["ALPHA", "BETA"],
            "completed_quantity": score.completed_quantity,
            "global_player_position": builder.coordinator.global_player_position,
            "queue_estimates_by_venue": estimates,
            "rebates_micros": score.rebates_micros,
        },
    )


def _stale_composite() -> MultiVenueScenarioResult:
    delayed = HiddenLiquidityRules(feed_delay_us=1_000)
    builder = MultiVenueScenarioBuilder(
        (
            _config("STALE", "NORMAL", rules=delayed),
            _config("OTHER", "LOW_LATENCY"),
        ),
        subscriptions=frozenset({"STALE", "OTHER"}),
    )
    _seed_two_sided(builder, "STALE", 101, 103, 100)
    _seed_two_sided(builder, "OTHER", 99, 100, 100)
    builder.advance(1_000)
    decision_feed = builder.coordinator.consolidated_feed()
    route_id = builder.route(
        1_000,
        RoutingRequest("STALE-SELL", Side.SELL, 100, RoutePolicy.BEST_DISPLAYED_PRICE),
    )
    builder.simulated_market(1_200, "STALE", "DEPLETE-STALE", Side.SELL, 100)
    builder.advance(5_000)
    builder.complete(6_000)
    result = builder.coordinator.route_result(route_id)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "stale-composite-quote",
        {
            "composite_state_at_decision": decision_feed.composite_state,
            "completed_quantity": score.completed_quantity,
            "selected_venue": result.decision.legs[0].venue_id,
            "stale_leg": result.executions[0].stale_quote_exposure,
        },
    )


def _partial_completion() -> MultiVenueScenarioResult:
    builder = MultiVenueScenarioBuilder(
        (
            _config("ONE", "ZERO_LATENCY", taker_fee=40),
            _config("TWO", "ZERO_LATENCY", taker_fee=10),
        ),
        subscriptions=frozenset({"ONE", "TWO"}),
    )
    _seed_two_sided(builder, "ONE", 99, 100, 50)
    _seed_two_sided(builder, "TWO", 99, 101, 75)
    route_id = builder.route(
        1_000,
        RoutingRequest(
            "PARTIAL-SWEEP",
            Side.BUY,
            200,
            RoutePolicy.SWEEP,
            max_venues=2,
        ),
    )
    builder.advance(1_000)
    builder.complete(2_000)
    score = builder.coordinator.score_route(route_id)
    return builder.finish(
        "partial-multi-venue-completion",
        {
            "completed_quantity": score.completed_quantity,
            "fees_micros": score.fees_micros,
            "remaining_quantity": 200 - score.completed_quantity,
            "target_quantity": 200,
            "venue_fill_count": sum(
                item.filled_quantity > 0
                for item in builder.coordinator.route_result(route_id).executions
            ),
        },
    )


def _config(
    venue_id: str,
    profile: str,
    *,
    fill_bps: int = 8_000,
    taker_fee: int = 30,
    maker_rebate: int = 10,
    rules: HiddenLiquidityRules | None = None,
) -> VenueConfig:
    return VenueConfig(
        venue_id,
        get_latency_profile(LatencyProfileName(profile)),
        VenueFeeSchedule(taker_fee, maker_rebate),
        hidden_rules=rules or HiddenLiquidityRules(),
        expected_fill_probability_bps=fill_bps,
    )


def _seed_two_sided(
    builder: MultiVenueScenarioBuilder,
    venue_id: str,
    bid: int,
    ask: int,
    quantity: int,
) -> None:
    builder.add(0, venue_id, _displayed(f"{venue_id}-BID", Side.BUY, quantity, bid))
    builder.add(0, venue_id, _displayed(f"{venue_id}-ASK", Side.SELL, quantity, ask))


def _displayed(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.DISPLAYED_LIMIT,
        OrderOwner.SIMULATED,
        "SIMULATED",
        quantity,
        price_ticks,
    )


def render_routing_timeline(events: tuple[CoordinatorEvent, ...]) -> str:
    lines: list[str] = []
    for event in events:
        if event.event_type.value.startswith("ROUTE_") or event.event_type.value in {
            "GLOBAL_POSITION_CHANGED",
            "CANCEL_ALL_REQUESTED",
            "VENUE_ORDER_CANCELLED",
        }:
            route_id = event.data.get("route_id", "-")
            venue_id = event.data.get("venue_id", "-")
            lines.append(
                f"{event.sequence:04d} t={event.simulation_time_us:>7} "
                f"{event.event_type.value:<28} route={route_id} venue={venue_id}"
            )
    return "\n".join(lines)
