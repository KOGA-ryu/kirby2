"""Runtime acceptance audit for fragmented venues and explainable routing."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass

from kirby2.exchange import OrderInstruction, SessionState
from kirby2.exchange.models import OrderOwner, Side
from kirby2.latency import LatencyProfileName, get_latency_profile
from kirby2.multivenue import (
    MarketCoordinator,
    RoutePolicy,
    RouteStyle,
    RoutingRequest,
    SmartOrderRouter,
    VenueConfig,
    VenueFeeSchedule,
    VenueOrderStatus,
    router_for_policy,
    run_all_multivenue_scenarios,
)
from kirby2.observability import HiddenOrderRequest, LiquidityKind


@dataclass(frozen=True, slots=True)
class MultiVenueAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_multivenue() -> tuple[MultiVenueAuditCase, ...]:
    scenarios = run_all_multivenue_scenarios()
    return (
        *(_scenario_case(result) for result in scenarios),
        _router_interface_case(),
        _observable_boundary_case(scenarios[4]),
        _asynchronous_feed_case(scenarios[4]),
        _locked_crossed_case(scenarios[4]),
        _venue_rejection_case(),
        _position_cancel_case(scenarios[3]),
        _economics_case(scenarios[5]),
        _advance_partition_case(),
        _seed_discipline_case(),
        _ground_truth_gate_case(),
    )


def _scenario_case(result) -> MultiVenueAuditCase:
    expected = {
        "better-price-poor-fill-probability": {
            "best_displayed_venue": "CHEAP",
            "better_price_depleted_before_selected_arrival": True,
            "completed_quantity": 100,
            "selected_venue": "RELIABLE",
            "selection_used_hidden_state": False,
        },
        "deep-slow-versus-shallow-fast": {
            "completed_quantity": 75,
            "deep_expected_latency_us": 2600,
            "deep_visible_quantity": 500,
            "selected_venue": "SHALLOW_FAST",
            "shallow_expected_latency_us": 312,
            "shallow_visible_quantity": 75,
        },
        "sweep-during-momentum": {
            "completed_quantity": 100,
            "leg_arrival_order": ["FAST", "SLOW"],
            "stale_exposure_quantity": 100,
            "target_quantity": 300,
        },
        "passive-routing-two-venues": {
            "cancel_all_venues": ["ALPHA", "BETA"],
            "completed_quantity": 40,
            "global_player_position": 40,
            "queue_estimates_by_venue": {"ALPHA": 100, "BETA": 300},
            "rebates_micros": 1000,
        },
        "stale-composite-quote": {
            "completed_quantity": 0,
            "composite_state_at_decision": "CROSSED",
            "selected_venue": "STALE",
            "stale_leg": True,
        },
        "partial-multi-venue-completion": {
            "completed_quantity": 125,
            "fees_micros": 2750,
            "remaining_quantity": 75,
            "target_quantity": 200,
            "venue_fill_count": 2,
        },
    }[result.name]
    failures: list[str] = []
    if result.summary != expected:
        failures.append(f"scenario summary differs: {result.summary}")
    if not result.replay.passed:
        failures.append("scenario did not exactly replay")
    for route_id in result.route_ids:
        decision = result.coordinator.route_result(route_id).decision
        if decision.observable_feed_sha256 != _sha(decision.observable_feed):
            failures.append(f"route evidence digest changed: {route_id}")
        if not decision.explanation:
            failures.append(f"route is unexplained: {route_id}")
    try:
        result.coordinator.assert_invariants()
    except RuntimeError as error:
        failures.append(f"runtime invariant failure: {error}")
    return MultiVenueAuditCase(
        f"scenario_{result.name.replace('-', '_')}",
        {
            "event_stream_sha256": result.coordinator.event_stream_sha256(),
            "recording_sha256": result.recording.sha256(),
            "replay_status": "PASS" if result.replay.passed else "FAIL",
            "route_evidence_sha256": [
                result.coordinator.route_result(route_id).decision.observable_feed_sha256
                for route_id in result.route_ids
            ],
            "summary": result.summary,
        },
        tuple(failures),
    )


def _router_interface_case() -> MultiVenueAuditCase:
    coordinator = MarketCoordinator(
        (
            VenueConfig("ONE", get_latency_profile(LatencyProfileName.ZERO_LATENCY)),
            VenueConfig("TWO", get_latency_profile(LatencyProfileName.ZERO_LATENCY)),
        ),
        seed=17,
        depth_subscriptions=frozenset({"ONE", "TWO"}),
    )
    for venue_id, bid, ask in (("ONE", 99, 101), ("TWO", 98, 102)):
        _seed(coordinator, venue_id, f"{venue_id}-B", Side.BUY, bid, 100)
        _seed(coordinator, venue_id, f"{venue_id}-A", Side.SELL, ask, 100)
    feed = coordinator.consolidated_feed()
    requests = {
        RoutePolicy.DIRECT: RoutingRequest(
            "DIRECT", Side.BUY, 10, RoutePolicy.DIRECT, direct_venue_id="ONE"
        ),
        RoutePolicy.BEST_DISPLAYED_PRICE: RoutingRequest(
            "BEST", Side.BUY, 10, RoutePolicy.BEST_DISPLAYED_PRICE
        ),
        RoutePolicy.LOWEST_EXPECTED_COST: RoutingRequest(
            "COST", Side.BUY, 10, RoutePolicy.LOWEST_EXPECTED_COST
        ),
        RoutePolicy.PASSIVE_QUEUE: RoutingRequest(
            "PASSIVE",
            Side.BUY,
            10,
            RoutePolicy.PASSIVE_QUEUE,
            style=RouteStyle.PASSIVE,
            max_venues=2,
        ),
        RoutePolicy.SWEEP: RoutingRequest(
            "SWEEP", Side.BUY, 10, RoutePolicy.SWEEP, max_venues=2
        ),
        RoutePolicy.LATENCY_AWARE: RoutingRequest(
            "LATENCY", Side.BUY, 10, RoutePolicy.LATENCY_AWARE
        ),
    }
    decisions = {
        policy.value: router_for_policy(policy).decide(
            f"AUDIT-{index}", request, feed
        )
        for index, (policy, request) in enumerate(requests.items(), start=1)
    }
    signature = tuple(inspect.signature(SmartOrderRouter.decide).parameters)
    failures: list[str] = []
    if signature != ("self", "route_id", "request", "feed"):
        failures.append("router interface accepts a non-observable dependency")
    if set(decisions) != {policy.value for policy in RoutePolicy}:
        failures.append("one or more baseline routers are unavailable")
    if any(
        not decision.explanation
        or decision.observable_feed_sha256 != feed.sha256()
        for decision in decisions.values()
    ):
        failures.append("a router did not retain its public decision evidence")
    return MultiVenueAuditCase(
        "all_router_baselines_use_one_observable_interface",
        {
            "decision_venues": {
                policy: [leg.venue_id for leg in decision.legs]
                for policy, decision in decisions.items()
            },
            "interface_parameters": list(signature),
            "policies": sorted(decisions),
        },
        tuple(failures),
    )


def _observable_boundary_case(result) -> MultiVenueAuditCase:
    decision = result.coordinator.route_result(result.route_ids[0]).decision
    payload = json.dumps(decision.observable_feed, sort_keys=True).lower()
    forbidden = {
        "hidden_quantity",
        "reserve_quantity",
        "priority_sequence",
        "maker_order_id",
        "liquidity_source",
        "future",
    }
    leaked = sorted(value for value in forbidden if value in payload)
    failures = () if not leaked else (f"router evidence leaked fields: {leaked}",)
    return MultiVenueAuditCase(
        "router_observable_boundary_and_immutable_evidence",
        {
            "decision_time_us": decision.decision_time_us,
            "feed_sha256": decision.observable_feed_sha256,
            "forbidden_fields_found": leaked,
            "representation": decision.observable_feed["representation"],
        },
        failures,
    )


def _locked_crossed_case(result) -> MultiVenueAuditCase:
    decision = result.coordinator.route_result(result.route_ids[0]).decision
    failures: list[str] = []
    if decision.observable_feed["composite_state"] != "CROSSED":
        failures.append("independent venue quotes did not create expected crossed composite")
    for venue in result.coordinator.venues.values():
        try:
            venue.assert_invariants()
        except RuntimeError as error:
            failures.append(f"venue internal book crossed: {error}")
    return MultiVenueAuditCase(
        "crossed_composite_allowed_while_each_venue_remains_uncrossed",
        {
            "best_ask_ticks": decision.observable_feed["best_ask_ticks"],
            "best_bid_ticks": decision.observable_feed["best_bid_ticks"],
            "composite_state": decision.observable_feed["composite_state"],
            "venue_count": len(result.coordinator.venues),
        },
        tuple(failures),
    )


def _asynchronous_feed_case(result) -> MultiVenueAuditCase:
    feed = result.coordinator.consolidated_feed()
    stale_trades = [trade for trade in feed.trades if trade.venue_id == "STALE"]
    subscribed = {depth.venue_id for depth in feed.subscribed_depth}
    failures: list[str] = []
    if not stale_trades or any(
        trade.received_time_us - trade.source_time_us != 1_000
        for trade in stale_trades
    ):
        failures.append("consolidated trade did not preserve venue feed delay")
    if subscribed != {"STALE", "OTHER"}:
        failures.append("per-venue depth subscription attribution is incomplete")
    if any(quote.quote_age_us < 0 for quote in feed.quotes):
        failures.append("consolidated quote age is invalid")
    return MultiVenueAuditCase(
        "asynchronous_consolidated_trades_depth_and_quote_age",
        {
            "depth_venues": sorted(subscribed),
            "quote_ages_us": {
                quote.venue_id: quote.quote_age_us for quote in feed.quotes
            },
            "stale_trade_times": [
                {
                    "received_time_us": trade.received_time_us,
                    "source_time_us": trade.source_time_us,
                    "venue_id": trade.venue_id,
                }
                for trade in stale_trades
            ],
        },
        tuple(failures),
    )


def _venue_rejection_case() -> MultiVenueAuditCase:
    coordinator = _basic_coordinator(seed=7)
    _seed(coordinator, "A", "REJECT-BID", Side.BUY, 99, 100)
    _seed(coordinator, "A", "REJECT-ASK", Side.SELL, 101, 100)
    coordinator.set_venue_session_state("A", SessionState.HALTED)
    route_id = coordinator.submit_route(
        RoutingRequest(
            "REJECT",
            Side.BUY,
            10,
            RoutePolicy.DIRECT,
            direct_venue_id="A",
        )
    )
    coordinator.advance_to(0)
    execution = coordinator.route_result(route_id).executions[0]
    unsupported = MarketCoordinator(
        (
            VenueConfig(
                "LIMIT_ONLY",
                get_latency_profile(LatencyProfileName.ZERO_LATENCY),
                supported_instructions=frozenset({OrderInstruction.LIMIT}),
            ),
        ),
        seed=8,
    )
    _seed(unsupported, "LIMIT_ONLY", "ONLY-BID", Side.BUY, 99, 100)
    _seed(unsupported, "LIMIT_ONLY", "ONLY-ASK", Side.SELL, 101, 100)
    unsupported_route = unsupported.submit_route(
        RoutingRequest(
            "UNSUPPORTED",
            Side.BUY,
            10,
            RoutePolicy.DIRECT,
            direct_venue_id="LIMIT_ONLY",
        )
    )
    unsupported.advance_to(0)
    unsupported_execution = unsupported.route_result(unsupported_route).executions[0]
    failures = () if (
        execution.status is VenueOrderStatus.REJECTED
        and execution.rejection_reason == "SESSION_HALTED"
        and unsupported_execution.status is VenueOrderStatus.REJECTED
        and unsupported_execution.rejection_reason == "UNSUPPORTED_MARKET_INSTRUCTION"
    ) else ("venue state or instruction capability did not reject explicitly",)
    return MultiVenueAuditCase(
        "venue_session_and_instruction_rejection",
        {
            "halted": execution.as_dict(),
            "unsupported_instruction": unsupported_execution.as_dict(),
        },
        failures,
    )


def _position_cancel_case(result) -> MultiVenueAuditCase:
    score = result.coordinator.score_route(result.route_ids[0])
    cancel_events = [
        event
        for event in result.coordinator.events
        if event.event_type.value == "VENUE_ORDER_CANCELLED"
    ]
    failures: list[str] = []
    if result.coordinator.global_player_position != score.completed_quantity:
        failures.append("global position does not reconcile to passive fills")
    if {event.data["venue_id"] for event in cancel_events} != {"ALPHA", "BETA"}:
        failures.append("cancel-all did not address both venue orders")
    return MultiVenueAuditCase(
        "global_position_and_cancel_all_reconcile",
        {
            "cancel_statuses": [event.data["response"]["status"] for event in cancel_events],
            "cancel_venues": [event.data["venue_id"] for event in cancel_events],
            "completed_quantity": score.completed_quantity,
            "global_player_position": result.coordinator.global_player_position,
        },
        tuple(failures),
    )


def _economics_case(result) -> MultiVenueAuditCase:
    score = result.coordinator.score_route(result.route_ids[0])
    failures: list[str] = []
    if score.fees_micros != 2_750 or score.rebates_micros != 0:
        failures.append("venue-specific fees did not reconcile to fills")
    if score.gross_price_numerator_x2 != 25_150 or score.gross_price_denominator != 125:
        failures.append("gross execution price did not reconcile")
    if score.net_execution_cost_micros != 125_752_750:
        failures.append("net execution cost did not reconcile")
    return MultiVenueAuditCase(
        "gross_fees_rebates_and_net_cost",
        score.as_dict(),
        tuple(failures),
    )


def _advance_partition_case() -> MultiVenueAuditCase:
    one = _routed_probe(99)
    many = _routed_probe(99, partitions=(250, 500, 750, 1_000, 2_000, 3_000, 5_000))
    failures = () if one.state_sha256() == many.state_sha256() else (
        "large and partitioned simulation-time advances diverged",
    )
    return MultiVenueAuditCase(
        "large_and_partitioned_advance_equivalence",
        {
            "large_state_sha256": one.state_sha256(),
            "partitioned_state_sha256": many.state_sha256(),
        },
        failures,
    )


def _seed_discipline_case() -> MultiVenueAuditCase:
    first = _routed_probe(123)
    repeat = _routed_probe(123)
    different = _routed_probe(124)
    failures: list[str] = []
    if first.event_stream_sha256() != repeat.event_stream_sha256():
        failures.append("same seed did not reproduce routing event stream")
    if first.event_stream_sha256() == different.event_stream_sha256():
        failures.append("different seeds did not alter sampled routing latency")
    return MultiVenueAuditCase(
        "owned_seeded_latency_determinism",
        {
            "different_seed_sha256": different.event_stream_sha256(),
            "first_sha256": first.event_stream_sha256(),
            "repeat_sha256": repeat.event_stream_sha256(),
        },
        tuple(failures),
    )


def _ground_truth_gate_case() -> MultiVenueAuditCase:
    coordinator = _basic_coordinator(seed=5)
    blocked = False
    try:
        coordinator.post_session_ground_truth()
    except RuntimeError:
        blocked = True
    coordinator.complete_session()
    truth = coordinator.post_session_ground_truth()
    failures = () if (
        blocked and truth["label"] == "SIMULATOR_GROUND_TRUTH_POST_SESSION"
    ) else ("multi-venue truth reveal gate or label is invalid",)
    return MultiVenueAuditCase(
        "post_session_truth_reveal_gate",
        {"label": truth["label"], "pre_completion_blocked": blocked},
        failures,
    )


def _routed_probe(seed: int, partitions: tuple[int, ...] | None = None) -> MarketCoordinator:
    coordinator = MarketCoordinator(
        (
            VenueConfig("A", get_latency_profile(LatencyProfileName.LOW_LATENCY)),
            VenueConfig("B", get_latency_profile(LatencyProfileName.NORMAL)),
        ),
        seed=seed,
        depth_subscriptions=frozenset({"A", "B"}),
    )
    for venue_id, ask in (("A", 101), ("B", 100)):
        _seed(coordinator, venue_id, f"{venue_id}-BID", Side.BUY, 99, 100)
        _seed(coordinator, venue_id, f"{venue_id}-ASK", Side.SELL, ask, 100)
    coordinator.submit_route(
        RoutingRequest("PROBE", Side.BUY, 150, RoutePolicy.SWEEP, max_venues=2)
    )
    if partitions is None:
        coordinator.advance_to(5_000)
    else:
        for target in partitions:
            coordinator.advance_to(target)
    coordinator.complete_session()
    return coordinator


def _basic_coordinator(seed: int) -> MarketCoordinator:
    return MarketCoordinator(
        (
            VenueConfig(
                "A",
                get_latency_profile(LatencyProfileName.ZERO_LATENCY),
                VenueFeeSchedule(),
            ),
        ),
        seed=seed,
        depth_subscriptions=frozenset({"A"}),
    )


def _seed(
    coordinator: MarketCoordinator,
    venue_id: str,
    order_id: str,
    side: Side,
    price: int,
    quantity: int,
) -> None:
    coordinator.add_resting_order(
        venue_id,
        HiddenOrderRequest(
            order_id,
            side,
            LiquidityKind.DISPLAYED_LIMIT,
            OrderOwner.SIMULATED,
            "SIMULATED",
            quantity,
            price,
        ),
    )


def _sha(payload: object) -> str:
    from kirby2.multivenue.models import canonical_sha256

    return canonical_sha256(payload)
