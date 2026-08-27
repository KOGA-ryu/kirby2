"""Real hidden-liquidity and fragmented-venue executor for audit cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.exchange import OrderOwner, Side
from kirby2.immutable import thaw_json
from kirby2.latency import LatencyComponent, get_latency_profile
from kirby2.multivenue import (
    CoordinatorEventType,
    MarketCoordinator,
    MultiVenueRecording,
    RoutePolicy,
    RouteStyle,
    RoutingRequest,
    VenueConfig,
    VenueFeeSchedule,
    VenueOrderStatus,
    replay_multivenue_recording,
)
from kirby2.multivenue.scenarios import MultiVenueScenarioBuilder
from kirby2.observability import (
    HiddenLiquidityRules,
    LiquidityKind,
    ObservableEventType,
    RefreshEventVisibility,
    TruthEventType,
)
from kirby2.observability.scenarios import (
    displayed_order,
    hidden_order,
    iceberg_order,
)

from ..models import (
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExerciseRecord,
    ExerciseStatus,
    ExecutorLane,
    FailureKind,
    FailureObservation,
    GeneratedCaseResult,
    GeneratedConfiguration,
    canonical_sha256,
)


FRAGMENTED_RECORDING_TYPE = "NATIVE_FRAGMENTED_RECORDING"
_RECORDING_FIELDS = frozenset(
    {
        "configuration",
        "native_recording",
        "observable_crossed_intervals",
    }
)
_HIDDEN_MODES = frozenset({"NONE", "ICEBERG", "HIDDEN_MIDPOINT"})
_VENUE_IDS = ("AUDIT-V01", "AUDIT-V02", "AUDIT-V03", "AUDIT-V04")
_VENUE_LATENCY_PROFILES = (
    "ZERO_LATENCY",
    "LOW_LATENCY",
    "NORMAL",
    "STRESSED",
)
_VENUE_FEES = (
    (10, 5),
    (20, 10),
    (30, 15),
    (40, 20),
)
_VENUE_QUOTES = (
    (102, 104),
    (98, 100),
    (99, 103),
    (97, 105),
)
_ROUTE_TIME_US = 1_000
_FLOW_QUANTITY = 80
_PLAYER_QUANTITY_PER_VENUE = 60
_ROUTING_COMPONENTS = (
    LatencyComponent.CLIENT_ROUTING,
    LatencyComponent.UPLINK,
    LatencyComponent.GATEWAY,
    LatencyComponent.VENUE_PROCESSING,
)
_FORBIDDEN_OBSERVABLE_KEYS = frozenset(
    {
        "agent_intent",
        "future",
        "hidden",
        "hidden_quantity",
        "intent",
        "latent_value",
        "maker_id",
        "maker_identity",
        "maker_order_id",
        "priority",
        "priority_sequence",
        "reserve",
        "reserve_quantity",
    }
)


@dataclass(frozen=True, slots=True)
class _Scenario:
    coordinator: MarketCoordinator
    native_recording: MultiVenueRecording
    observable_crossed_intervals: tuple[dict[str, object], ...]


class FragmentedExecutor:
    """Execute hidden-liquidity and venue-count axes on the production stack."""

    lane = ExecutorLane.FRAGMENTED

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        scenario = _build_scenario(configuration)
        recording = CaseRecording(
            lane=self.lane,
            recording_type=FRAGMENTED_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "native_recording": scenario.native_recording.as_dict(),
                "observable_crossed_intervals": list(
                    scenario.observable_crossed_intervals
                ),
            },
        )
        return _result(
            configuration,
            recording,
            scenario.native_recording,
            scenario.coordinator,
            scenario.observable_crossed_intervals,
            replay_mismatches=(),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("fragmented replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("fragmented replay received a different lane")
        if recording.recording_type != FRAGMENTED_RECORDING_TYPE:
            raise ValueError("unsupported fragmented recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("fragmented recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("fragmented recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_native = payload["native_recording"]
        raw_intervals = payload["observable_crossed_intervals"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("fragmented configuration must be an object")
        if not isinstance(raw_native, dict):
            raise TypeError("native fragmented recording must be an object")
        if not isinstance(raw_intervals, list) or any(
            not isinstance(item, dict) for item in raw_intervals
        ):
            raise TypeError("observable crossed intervals must be objects")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        native = MultiVenueRecording.from_dict(raw_native)
        replay = replay_multivenue_recording(native)
        intervals = tuple(dict(item) for item in raw_intervals)
        recomputed_intervals = _observable_crossed_intervals(replay.coordinator)
        expected_configs = tuple(
            item.as_dict() for item in _venue_configs(configuration.venue_count)
        )
        mismatches: list[str] = []
        for name, matches in {
            "native_events": replay.events_match,
            "native_feed": replay.feed_match,
            "native_ground_truth": replay.ground_truth_match,
            "native_scores": replay.scores_match,
            "native_state": replay.state_match,
            "native_seed": native.seed == configuration.seed,
            "native_venue_configs": native.venue_configs == expected_configs,
            "observable_crossed_intervals": (
                recomputed_intervals == intervals
            ),
        }.items():
            if not matches:
                mismatches.append(name)
        return _result(
            configuration,
            recording,
            native,
            replay.coordinator,
            intervals,
            replay_mismatches=tuple(mismatches),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("fragmented executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("fragmented executor received a different lane")
        if configuration.hidden_liquidity not in _HIDDEN_MODES:
            raise ValueError("unsupported generated hidden-liquidity mode")
        if not 1 <= configuration.venue_count <= len(_VENUE_IDS):
            raise ValueError("fragmented executor supports one through four venues")


def _build_scenario(configuration: GeneratedConfiguration) -> _Scenario:
    configs = _venue_configs(configuration.venue_count)
    venue_ids = frozenset(config.venue_id for config in configs)
    builder = MultiVenueScenarioBuilder(
        configs,
        seed=configuration.seed,
        subscriptions=venue_ids,
    )
    for index, config in enumerate(configs):
        bid, ask = _VENUE_QUOTES[index]
        _seed_bid(builder, config.venue_id, bid, configuration.hidden_liquidity)
        builder.add(
            0,
            config.venue_id,
            displayed_order(
                f"{config.venue_id}-SEED-ASK",
                Side.SELL,
                50,
                ask,
                account_id="AUDIT-SIMULATED",
            ),
        )
        if configuration.hidden_liquidity == "HIDDEN_MIDPOINT":
            builder.add(
                0,
                config.venue_id,
                hidden_order(
                    f"{config.venue_id}-MIDPOINT-BID",
                    Side.BUY,
                    20,
                    kind=LiquidityKind.MIDPOINT_HIDDEN,
                    account_id="AUDIT-SIMULATED",
                ),
            )

    builder.route(
        _ROUTE_TIME_US,
        RoutingRequest(
            "AUDIT-PLAYER-PASSIVE",
            Side.BUY,
            _PLAYER_QUANTITY_PER_VENUE * configuration.venue_count,
            RoutePolicy.PASSIVE_QUEUE,
            style=RouteStyle.PASSIVE,
            max_venues=configuration.venue_count,
        ),
    )
    arrival_bound = _ROUTE_TIME_US + max(
        sum(
            config.latency_profile.distribution(component).upper_us
            for component in _ROUTING_COMPONENTS
        )
        for config in configs
    )
    builder.advance(arrival_bound)
    flow_time = arrival_bound + 100
    for config in configs:
        builder.simulated_market(
            flow_time,
            config.venue_id,
            f"{config.venue_id}-AGGRESSIVE-SELL",
            Side.SELL,
            _FLOW_QUANTITY,
        )
    builder.cancel_all(flow_time + 100)
    cleanup_time = builder.coordinator.clock.current_time_us + 100
    builder.simulated_market(
        cleanup_time,
        configs[0].venue_id,
        "AUDIT-V01-CROSS-CLEANUP",
        Side.SELL,
        200,
    )
    builder.complete(builder.coordinator.clock.current_time_us + 100)
    scenario = builder.finish(
        f"generated-{configuration.hidden_liquidity.lower()}-"
        f"{configuration.venue_count}-venues",
        {
            "observable_only_routing": True,
            "venue_ids": sorted(venue_ids),
        },
    )
    intervals = _observable_crossed_intervals(scenario.coordinator)
    return _Scenario(scenario.coordinator, scenario.recording, intervals)


def _venue_configs(venue_count: int) -> tuple[VenueConfig, ...]:
    return tuple(
        VenueConfig(
            venue_id=_VENUE_IDS[index],
            latency_profile=get_latency_profile(_VENUE_LATENCY_PROFILES[index]),
            fees=VenueFeeSchedule(*_VENUE_FEES[index]),
            hidden_rules=HiddenLiquidityRules(),
            expected_fill_probability_bps=7_000 + index * 500,
        )
        for index in range(venue_count)
    )


def _seed_bid(
    builder: MultiVenueScenarioBuilder,
    venue_id: str,
    bid: int,
    hidden_mode: str,
) -> None:
    if hidden_mode == "ICEBERG":
        request = iceberg_order(
            f"{venue_id}-SEED-BID",
            Side.BUY,
            bid,
            display=40,
            reserve=80,
            refresh=40,
            visibility=RefreshEventVisibility.QUOTE_UPDATE_ONLY,
            account_id="AUDIT-SIMULATED",
        )
    else:
        request = displayed_order(
            f"{venue_id}-SEED-BID",
            Side.BUY,
            50,
            bid,
            account_id="AUDIT-SIMULATED",
        )
    builder.add(0, venue_id, request)


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: MultiVenueRecording,
    coordinator: MarketCoordinator,
    crossed_intervals: tuple[dict[str, object], ...],
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    coordinator.assert_invariants()
    route_id = _single_route_id(native)
    route = coordinator.route_result(route_id)
    observable_projection = _observable_projection(
        coordinator,
        route_id,
        crossed_intervals,
    )
    exercises = _exercises(
        configuration,
        recording,
        native,
        coordinator,
        route_id,
    )
    checks = _checks(
        configuration,
        coordinator,
        route_id,
        crossed_intervals,
        observable_projection,
    )
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"FRAGMENTED_{check.name.upper()}",
            message=check.detail,
            evidence={
                "check": check.name,
                "check_evidence_sha256": canonical_sha256(
                    check.as_dict()["evidence"]
                ),
            },
        )
        for check in checks
        if check.status is CheckStatus.FAIL
    ]
    failures.extend(
        FailureObservation(
            kind=FailureKind.EXECUTION_ERROR,
            code=f"FRAGMENTED_{exercise.capability.upper()}_NOT_EXERCISED",
            message=(
                "configured fragmented-market dimension was not exercised: "
                f"{exercise.capability}"
            ),
            evidence={
                "capability": exercise.capability,
                "configured_value": thaw_json(exercise.configured_value),
            },
        )
        for exercise in exercises
        if exercise.status is ExerciseStatus.NOT_EXERCISED
    )
    if replay_mismatches:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="FRAGMENTED_REPLAY_MISMATCH",
                message="native fragmented recording did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    truth = coordinator.post_session_ground_truth()
    truth_events = [
        event
        for venue in truth["venues"]
        for event in venue["state"]["events"]
    ]
    trade_events = [
        event for event in truth_events if event["event_type"] == "TRADE"
    ]
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.FRAGMENTED,
        recording=recording,
        event_projection=_event_projection(coordinator),
        final_state_projection={
            "complete": coordinator.complete,
            "crossed_composite_intervals": list(crossed_intervals),
            "global_player_position": coordinator.global_player_position,
            "ground_truth": truth,
            "route": route.as_dict(),
            "score": coordinator.score_route(route_id).as_dict(),
            "state_sha256": coordinator.state_sha256(),
            "venue_engine_state_sha256": {
                venue_id: venue.engine.state_sha256()
                for venue_id, venue in sorted(coordinator.venues.items())
            },
        },
        metrics={
            "coordinator_event_count": len(coordinator.events),
            "crossed_composite_duration_us": sum(
                int(item["duration_us"]) for item in crossed_intervals
            ),
            "crossed_composite_episode_count": len(crossed_intervals),
            "global_player_position_shares": coordinator.global_player_position,
            "route_completed_quantity": route.completed_quantity,
            "route_leg_count": len(route.executions),
            "simulation_duration_us": coordinator.clock.current_time_us,
            "trade_count": len(trade_events),
            "traded_volume_shares": sum(
                int(event["data"]["quantity"]) for event in trade_events
            ),
            "venue_count": len(coordinator.venues),
        },
        exercises=exercises,
        checks=checks,
        failures=tuple(failures),
        observable_projection=observable_projection,
    )


def _exercises(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: MultiVenueRecording,
    coordinator: MarketCoordinator,
    route_id: str,
) -> tuple[ExerciseRecord, ...]:
    truth = coordinator.post_session_ground_truth()
    requests = [
        command.parameters["request"]
        for command in native.commands
        if command.command_type == "ADD"
    ]
    kinds = [str(request["kind"]) for request in requests]
    refresh_events = _truth_event_markers(
        truth,
        TruthEventType.ICEBERG_REFRESHED.value,
    )
    midpoint_trades = _truth_event_markers(
        truth,
        TruthEventType.TRADE.value,
        liquidity_source="MIDPOINT_HIDDEN",
    )
    if configuration.hidden_liquidity == "NONE":
        hidden_exercised = (
            kinds
            and set(kinds) == {LiquidityKind.DISPLAYED_LIMIT.value}
            and not refresh_events
            and not midpoint_trades
        )
    elif configuration.hidden_liquidity == "ICEBERG":
        icebergs = [
            request
            for request in requests
            if request["kind"] == LiquidityKind.ICEBERG.value
        ]
        hidden_exercised = (
            len(icebergs) == configuration.venue_count
            and all(
                request["iceberg"]
                == {
                    "display_quantity": 40,
                    "event_visibility": "QUOTE_UPDATE_ONLY",
                    "refresh_behavior": "AUTOMATIC",
                    "refresh_quantity": 40,
                    "reserve_quantity": 80,
                }
                for request in icebergs
            )
            and len(refresh_events) >= configuration.venue_count
        )
    else:
        midpoint_requests = [
            request
            for request in requests
            if request["kind"] == LiquidityKind.MIDPOINT_HIDDEN.value
        ]
        hidden_exercised = (
            len(midpoint_requests) == configuration.venue_count
            and len(midpoint_trades) >= configuration.venue_count
        )

    route = coordinator.route_result(route_id)
    expected_venues = set(_VENUE_IDS[: configuration.venue_count])
    decision_venues = {leg.venue_id for leg in route.decision.legs}
    execution_venues = {execution.venue_id for execution in route.executions}
    depth_venues = {
        str(item["venue_id"])
        for item in route.decision.observable_feed["subscribed_depth"]
    }
    venue_exercised = all(
        (
            set(coordinator.venues) == expected_venues,
            decision_venues == expected_venues,
            execution_venues == expected_venues,
            depth_venues == expected_venues,
            len(route.executions) == configuration.venue_count,
        )
    )
    common = {
        "executor": type(coordinator).__name__,
        "recording_sha256": recording.sha256,
    }
    return (
        ExerciseRecord(
            ExecutorLane.FRAGMENTED,
            "hidden_liquidity",
            configuration.hidden_liquidity,
            (
                ExerciseStatus.EXERCISED
                if hidden_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "configured_mode": configuration.hidden_liquidity,
                "iceberg_refresh_events": refresh_events,
                "midpoint_trade_events": midpoint_trades,
                "request_kind_counts": {
                    kind: kinds.count(kind) for kind in sorted(set(kinds))
                },
            },
        ),
        ExerciseRecord(
            ExecutorLane.FRAGMENTED,
            "venue_count",
            configuration.venue_count,
            (
                ExerciseStatus.EXERCISED
                if venue_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "decision_venues": sorted(decision_venues),
                "depth_subscription_venues": sorted(depth_venues),
                "execution_venues": sorted(execution_venues),
                "expected_venues": sorted(expected_venues),
                "route_event_sequences": [
                    event.sequence
                    for event in coordinator.events
                    if event.event_type
                    in {
                        CoordinatorEventType.ROUTE_DECISION,
                        CoordinatorEventType.ROUTE_LEG_ACCEPTED,
                    }
                ],
            },
        ),
    )


def _checks(
    configuration: GeneratedConfiguration,
    coordinator: MarketCoordinator,
    route_id: str,
    crossed_intervals: tuple[dict[str, object], ...],
    observable_projection: dict[str, object],
) -> tuple[CheckResult, ...]:
    venue_ok, venue_evidence = _venue_invariants(coordinator)
    position_ok, position_evidence = _global_position_reconciliation(coordinator)
    route_ok, route_evidence = _route_leg_conservation(coordinator, route_id)
    quote_ok, quote_evidence = _observable_quote_construction(
        coordinator,
        route_id,
    )
    leaked = _forbidden_observable_keys(observable_projection)
    recomputed_intervals = _observable_crossed_intervals(coordinator)
    intervals_well_formed = all(
        item["end_time_us"] is not None
        and int(item["duration_us"]) >= 0
        and int(item["end_time_us"]) - int(item["start_time_us"])
        == int(item["duration_us"])
        for item in crossed_intervals
    )
    crossed_ok = all(
        (
            recomputed_intervals == crossed_intervals,
            intervals_well_formed,
            (
                not crossed_intervals
                if configuration.venue_count == 1
                else bool(crossed_intervals)
            ),
            coordinator.consolidated_feed().composite_state != "CROSSED",
        )
    )
    return (
        _check("venue_invariants", venue_ok, venue_evidence),
        _check(
            "global_position_reconciliation",
            position_ok,
            position_evidence,
        ),
        _check("route_leg_conservation", route_ok, route_evidence),
        _check(
            "observable_quote_construction",
            quote_ok,
            quote_evidence,
        ),
        _check(
            "observable_projection_boundary",
            not leaked,
            {
                "forbidden_fields_found": leaked,
                "observable_projection_sha256": canonical_sha256(
                    observable_projection
                ),
            },
        ),
        _check(
            "crossed_composite_intervals_recorded",
            crossed_ok,
            {
                "ending_composite_state": (
                    coordinator.consolidated_feed().composite_state
                ),
                "interval_count": len(crossed_intervals),
                "intervals": list(crossed_intervals),
                "recomputed_sha256": canonical_sha256(
                    list(recomputed_intervals)
                ),
                "recorded_sha256": canonical_sha256(list(crossed_intervals)),
            },
        ),
    )


def _venue_invariants(
    coordinator: MarketCoordinator,
) -> tuple[bool, dict[str, object]]:
    venues: dict[str, object] = {}
    passed = True
    for venue_id, venue in sorted(coordinator.venues.items()):
        venue.assert_invariants()
        feed = venue.observable_feed()
        truth = venue.engine.post_session_ground_truth()
        uncrossed = (
            feed.book.best_bid is None
            or feed.book.best_ask is None
            or feed.book.best_bid < feed.book.best_ask
        )
        conserved = all(
            order.original_quantity
            == order.filled_quantity
            + order.cancelled_quantity
            + order.remaining_quantity
            and min(
                order.filled_quantity,
                order.cancelled_quantity,
                order.remaining_quantity,
            )
            >= 0
            for order in truth.orders
        )
        passed = passed and uncrossed and conserved
        venues[venue_id] = {
            "best_ask_ticks": feed.book.best_ask,
            "best_bid_ticks": feed.book.best_bid,
            "order_count": len(truth.orders),
            "quantity_conserved": conserved,
            "state_sha256": venue.engine.state_sha256(),
            "uncrossed": uncrossed,
        }
    return passed, {"venue_count": len(venues), "venues": venues}


def _global_position_reconciliation(
    coordinator: MarketCoordinator,
) -> tuple[bool, dict[str, object]]:
    truth = coordinator.post_session_ground_truth()
    venue_position_sum = sum(
        int(venue["state"]["player_position"]) for venue in truth["venues"]
    )
    fill_position = 0
    player_fill_quantity = 0
    player_trade_markers: list[dict[str, object]] = []
    for venue in truth["venues"]:
        venue_id = str(venue["venue_id"])
        for event in venue["state"]["events"]:
            if event["event_type"] != TruthEventType.TRADE.value:
                continue
            data = event["data"]
            quantity = int(data["quantity"])
            if data["maker_owner"] == OrderOwner.PLAYER.value:
                fill_position += Side(str(data["maker_side"])).sign * quantity
                player_fill_quantity += quantity
                player_trade_markers.append(
                    {"sequence": event["sequence"], "venue_id": venue_id}
                )
            if data["taker_owner"] == OrderOwner.PLAYER.value:
                fill_position += Side(str(data["taker_side"])).sign * quantity
                player_fill_quantity += quantity
                player_trade_markers.append(
                    {"sequence": event["sequence"], "venue_id": venue_id}
                )
    event_position = 0
    chain_ok = True
    position_events = []
    for event in coordinator.events:
        if event.event_type is not CoordinatorEventType.GLOBAL_POSITION_CHANGED:
            continue
        previous = int(event.data["previous_position"])
        delta = int(event.data["delta"])
        current = int(event.data["position"])
        chain_ok = chain_ok and previous == event_position
        event_position += delta
        chain_ok = chain_ok and current == event_position
        position_events.append(event.sequence)
    passed = all(
        (
            chain_ok,
            fill_position == coordinator.global_player_position,
            venue_position_sum == coordinator.global_player_position,
            event_position == coordinator.global_player_position,
        )
    )
    return passed, {
        "coordinator_position": coordinator.global_player_position,
        "event_chain_position": event_position,
        "fill_ledger_position": fill_position,
        "global_position_event_sequences": position_events,
        "player_fill_quantity": player_fill_quantity,
        "player_trade_markers": player_trade_markers,
        "venue_position_sum": venue_position_sum,
    }


def _route_leg_conservation(
    coordinator: MarketCoordinator,
    route_id: str,
) -> tuple[bool, dict[str, object]]:
    route = coordinator.route_result(route_id)
    truth_orders = {
        (venue_id, order.order_id): order
        for venue_id, venue in sorted(coordinator.venues.items())
        for order in venue.engine.post_session_ground_truth().orders
    }
    legs: list[dict[str, object]] = []
    passed = route.complete and len(route.executions) == len(route.decision.legs)
    for execution in route.executions:
        truth_order = truth_orders.get((execution.venue_id, execution.order_id))
        if execution.status is VenueOrderStatus.REJECTED:
            filled = 0
            cancelled = 0
            rejected = execution.requested_quantity
            remaining = 0
            conserved = truth_order is None and execution.filled_quantity == 0
        elif truth_order is None:
            filled = cancelled = rejected = remaining = 0
            conserved = False
        else:
            filled = truth_order.filled_quantity
            cancelled = truth_order.cancelled_quantity
            rejected = 0
            remaining = truth_order.remaining_quantity
            conserved = all(
                (
                    truth_order.original_quantity == execution.requested_quantity,
                    execution.filled_quantity == filled,
                    execution.requested_quantity
                    == filled + cancelled + remaining,
                )
            )
        passed = passed and conserved
        legs.append(
            {
                "cancelled_quantity": cancelled,
                "conserved": conserved,
                "filled_quantity": filled,
                "order_id": execution.order_id,
                "original_quantity": execution.requested_quantity,
                "rejected_quantity": rejected,
                "remaining_quantity": remaining,
                "venue_id": execution.venue_id,
            }
        )
    return passed, {
        "completed_quantity": route.completed_quantity,
        "leg_count": len(legs),
        "legs": legs,
        "route_id": route_id,
        "target_quantity": route.request.quantity,
    }


def _observable_quote_construction(
    coordinator: MarketCoordinator,
    route_id: str,
) -> tuple[bool, dict[str, object]]:
    decision = coordinator.route_result(route_id).decision
    reconstructed, update_sequences = _reconstruct_feed_at(
        coordinator,
        decision.decision_time_us,
    )
    expected = decision.observable_feed
    passed = all(
        (
            reconstructed == expected,
            canonical_sha256(reconstructed) == decision.observable_feed_sha256,
            set(update_sequences) == set(coordinator.venues),
        )
    )
    return passed, {
        "decision_feed_sha256": decision.observable_feed_sha256,
        "decision_time_us": decision.decision_time_us,
        "received_book_update_sequences": update_sequences,
        "reconstructed_feed_sha256": canonical_sha256(reconstructed),
        "reconstruction_match": reconstructed == expected,
        "source": "received_observable_book_and_trade_events",
    }


def _reconstruct_feed_at(
    coordinator: MarketCoordinator,
    decision_time_us: int,
) -> tuple[dict[str, object], dict[str, int]]:
    quotes: list[dict[str, object]] = []
    depths: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    update_sequences: dict[str, int] = {}
    for venue_id, venue in sorted(coordinator.venues.items()):
        events = tuple(
            event
            for event in venue.observable_feed().events
            if event.received_time_us <= decision_time_us
            and event.source_time_us < decision_time_us
        )
        snapshots = tuple(
            event
            for event in events
            if event.event_type is ObservableEventType.BOOK_SNAPSHOT
        )
        latest = None if not snapshots else snapshots[-1]
        if latest is None:
            book: dict[str, object] = {"asks": [], "bids": []}
            source_time_us = 0
            received_time_us = 0
        else:
            raw_book = latest.as_dict()["data"]["book"]
            if not isinstance(raw_book, dict):
                raise TypeError("observable snapshot book must be an object")
            book = raw_book
            source_time_us = latest.source_time_us
            received_time_us = latest.received_time_us
            update_sequences[venue_id] = latest.sequence
        bids = book["bids"]
        asks = book["asks"]
        if not isinstance(bids, list) or not isinstance(asks, list):
            raise TypeError("observable snapshot levels must be arrays")
        best_bid = None if not bids else bids[0]
        best_ask = None if not asks else asks[0]
        config = venue.config
        quotes.append(
            {
                "best_ask_quantity": (
                    0 if best_ask is None else int(best_ask["total_quantity"])
                ),
                "best_ask_ticks": (
                    None if best_ask is None else int(best_ask["price_ticks"])
                ),
                "best_bid_quantity": (
                    0 if best_bid is None else int(best_bid["total_quantity"])
                ),
                "best_bid_ticks": (
                    None if best_bid is None else int(best_bid["price_ticks"])
                ),
                "expected_fill_probability_bps": (
                    config.expected_fill_probability_bps
                ),
                "expected_routing_latency_us": (
                    config.expected_routing_latency_us
                ),
                "maker_rebate_micros_per_share": (
                    config.fees.maker_rebate_micros_per_share
                ),
                "quote_age_us": decision_time_us - source_time_us,
                "quote_received_time_us": received_time_us,
                "quote_source_time_us": source_time_us,
                "session_state": config.session_state.value,
                "supported_instructions": sorted(
                    item.value for item in config.supported_instructions
                ),
                "taker_fee_micros_per_share": (
                    config.fees.taker_fee_micros_per_share
                ),
                "tick_value_micros": config.fees.tick_value_micros,
                "venue_id": venue_id,
            }
        )
        if venue_id in coordinator.depth_subscriptions:
            depths.append({"book": book, "venue_id": venue_id})
        for event in events:
            if event.event_type is not ObservableEventType.TRADE:
                continue
            data = event.as_dict()["data"]
            trades.append(
                {
                    "aggressor_side": data["aggressor_side"],
                    "price_x2": int(data["price_x2"]),
                    "quantity": int(data["quantity"]),
                    "received_time_us": event.received_time_us,
                    "source_time_us": event.source_time_us,
                    "trade_id": str(data["trade_id"]),
                    "venue_id": venue_id,
                }
            )
    trades.sort(
        key=lambda item: (
            int(item["received_time_us"]),
            str(item["venue_id"]),
            str(item["trade_id"]),
        )
    )
    bid_prices = [
        int(quote["best_bid_ticks"])
        for quote in quotes
        if quote["best_bid_ticks"] is not None
    ]
    ask_prices = [
        int(quote["best_ask_ticks"])
        for quote in quotes
        if quote["best_ask_ticks"] is not None
    ]
    best_bid = None if not bid_prices else max(bid_prices)
    best_ask = None if not ask_prices else min(ask_prices)
    if best_bid is None or best_ask is None:
        composite_state = "ONE_SIDED"
    elif best_bid > best_ask:
        composite_state = "CROSSED"
    elif best_bid == best_ask:
        composite_state = "LOCKED"
    else:
        composite_state = "NORMAL"
    return {
        "best_ask_ticks": best_ask,
        "best_ask_venues": [
            str(quote["venue_id"])
            for quote in quotes
            if quote["best_ask_ticks"] == best_ask
        ],
        "best_bid_ticks": best_bid,
        "best_bid_venues": [
            str(quote["venue_id"])
            for quote in quotes
            if quote["best_bid_ticks"] == best_bid
        ],
        "composite_state": composite_state,
        "quotes": quotes,
        "representation": "CONSOLIDATED_OBSERVABLE_FEED",
        "simulation_time_us": decision_time_us,
        "subscribed_depth": depths,
        "trades": trades,
    }, update_sequences


def _observable_crossed_intervals(
    coordinator: MarketCoordinator,
) -> tuple[dict[str, object], ...]:
    updates: list[tuple[int, str, int, dict[str, object]]] = []
    for venue_id, venue in sorted(coordinator.venues.items()):
        for event in venue.observable_feed().events:
            if event.event_type is not ObservableEventType.BOOK_SNAPSHOT:
                continue
            raw_book = event.as_dict()["data"]["book"]
            if not isinstance(raw_book, dict):
                raise TypeError("observable snapshot book must be an object")
            updates.append(
                (event.received_time_us, venue_id, event.sequence, raw_book)
            )
    books: dict[str, dict[str, object]] = {}
    intervals: list[dict[str, object]] = []
    active: dict[str, object] | None = None
    for received_time_us, venue_id, event_sequence, book in sorted(updates):
        books[venue_id] = book
        best_bid, best_ask = _composite_prices(books)
        crossed = (
            best_bid is not None
            and best_ask is not None
            and best_bid > best_ask
        )
        marker = {
            "event_sequence": event_sequence,
            "received_time_us": received_time_us,
            "venue_id": venue_id,
        }
        if crossed and active is None:
            active = {
                "start_best_ask_ticks": best_ask,
                "start_best_bid_ticks": best_bid,
                "start_time_us": received_time_us,
                "start_update": marker,
            }
        elif not crossed and active is not None:
            start_time_us = int(active["start_time_us"])
            intervals.append(
                {
                    "duration_us": received_time_us - start_time_us,
                    "end_best_ask_ticks": best_ask,
                    "end_best_bid_ticks": best_bid,
                    "end_time_us": received_time_us,
                    "end_update": marker,
                    "episode_sequence": len(intervals) + 1,
                    "source": "received_observable_book_snapshots",
                    **active,
                }
            )
            active = None
    if active is not None:
        start_time_us = int(active["start_time_us"])
        intervals.append(
            {
                "duration_us": coordinator.clock.current_time_us - start_time_us,
                "end_best_ask_ticks": None,
                "end_best_bid_ticks": None,
                "end_time_us": None,
                "end_update": None,
                "episode_sequence": len(intervals) + 1,
                "source": "received_observable_book_snapshots",
                **active,
            }
        )
    return tuple(intervals)


def _composite_prices(
    books: Mapping[str, Mapping[str, object]],
) -> tuple[int | None, int | None]:
    bids: list[int] = []
    asks: list[int] = []
    for book in books.values():
        raw_bids = book["bids"]
        raw_asks = book["asks"]
        if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
            raise TypeError("observable snapshot levels must be arrays")
        if raw_bids:
            bids.append(int(raw_bids[0]["price_ticks"]))
        if raw_asks:
            asks.append(int(raw_asks[0]["price_ticks"]))
    return (
        None if not bids else max(bids),
        None if not asks else min(asks),
    )


def _truth_event_markers(
    truth: dict[str, object],
    event_type: str,
    **required_data: object,
) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    for venue in truth["venues"]:
        for event in venue["state"]["events"]:
            if event["event_type"] != event_type or any(
                event["data"].get(key) != value
                for key, value in required_data.items()
            ):
                continue
            markers.append(
                {
                    "sequence": event["sequence"],
                    "simulation_time_us": event["simulation_time_us"],
                    "venue_id": venue["venue_id"],
                }
            )
    return markers


def _forbidden_observable_keys(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object, path: str) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).lower()
                next_path = f"{path}.{key}" if path else str(key)
                if normalized in _FORBIDDEN_OBSERVABLE_KEYS:
                    found.add(next_path)
                visit(nested, next_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, "")
    return sorted(found)


def _event_projection(
    coordinator: MarketCoordinator,
) -> tuple[dict[str, object], ...]:
    projected = [
        {"record_type": "coordinator_event", **event.as_dict()}
        for event in coordinator.events
    ]
    for venue_id, venue in sorted(coordinator.venues.items()):
        truth = venue.engine.post_session_ground_truth()
        projected.extend(
            {
                "record_type": "venue_truth_event",
                "venue_id": venue_id,
                **event.as_dict(),
            }
            for event in truth.events
        )
        projected.extend(
            {
                "record_type": "venue_observable_event",
                "venue_id": venue_id,
                **event.as_dict(),
            }
            for event in venue.observable_feed().events
        )
    return tuple(projected)


def _observable_projection(
    coordinator: MarketCoordinator,
    route_id: str,
    crossed_intervals: tuple[dict[str, object], ...],
) -> dict[str, object]:
    route = coordinator.route_result(route_id)
    return {
        "consolidated_feed": coordinator.consolidated_feed().as_dict(),
        "crossed_composite_intervals": list(crossed_intervals),
        "representation": "FRAGMENTED_OBSERVABLE_AUDIT_PROJECTION",
        "route_decision": route.decision.as_dict(),
        "venue_feeds": [
            {
                "feed": venue.observable_feed().as_dict(),
                "venue_id": venue_id,
            }
            for venue_id, venue in sorted(coordinator.venues.items())
        ],
    }


def _single_route_id(native: MultiVenueRecording) -> str:
    if len(native.route_ids) != 1:
        raise RuntimeError("fragmented audit case requires exactly one route")
    return native.route_ids[0]


def _check(
    name: str,
    passed: bool,
    evidence: dict[str, object],
) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        required=True,
        detail=(
            f"real fragmented-market check passed: {name}"
            if passed
            else f"real fragmented-market check failed: {name}"
        ),
        evidence={"source": "FragmentedExecutor", **evidence},
    )
