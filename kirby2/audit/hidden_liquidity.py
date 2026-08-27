"""Runtime acceptance audit for hidden liquidity and observability boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from kirby2.exchange.models import OrderOwner, Side
from kirby2.observability import (
    DISPLAY_DECREASE_POSSIBLE_CAUSES,
    HiddenLiquidityRules,
    HiddenLiquidityVenue,
    HiddenOrderRequest,
    HiddenPriority,
    IcebergDefinition,
    IcebergRefreshBehavior,
    IcebergRefreshPriority,
    LiquidityKind,
    ObservableEventType,
    ObservabilityCommand,
    ObservabilityRecording,
    QueueDataMode,
    QueuePositionEstimator,
    RefreshEventVisibility,
    TruthEventType,
    recording_json_round_trip,
    replay_observability_recording,
    run_all_hidden_liquidity_scenarios,
    run_blind_hidden_liquidity_exercise,
)
from kirby2.strategy import TrafficLightRuntime, parse_strategy
from kirby2.strategy.language import StrategyDefinition


@dataclass(frozen=True, slots=True)
class HiddenLiquidityAuditCase:
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


def audit_hidden_liquidity() -> tuple[HiddenLiquidityAuditCase, ...]:
    scenarios = run_all_hidden_liquidity_scenarios()
    blind = run_blind_hidden_liquidity_exercise()
    return (
        *(_scenario_case(result) for result in scenarios),
        _blind_pair_case(blind),
        _architectural_boundary_case(),
        _queue_estimation_case(),
        _traffic_light_boundary_case(),
        _display_disappearance_case(scenarios[3]),
        _feed_delay_case(),
        _refresh_priority_case(),
        _manual_refresh_replay_case(),
        _unobservable_scoring_case(blind),
        _venue_permission_case(),
    )


def _scenario_case(result) -> HiddenLiquidityAuditCase:
    expected = {
        "iceberg-absorption": {
            "displayed_before": 100,
            "filled_quantity": 350,
            "refresh_count": 3,
            "remaining_displayed": 50,
            "remaining_reserve": 100,
        },
        "hidden-midpoint-fill": {
            "displayed_book_unchanged": True,
            "filled_quantity": 50,
            "midpoint_price_ticks": "100.5",
        },
        "repeated-displayed-refresh": {
            "filled_quantity": 200,
            "quote_only_refresh_count": 4,
            "tape_print_count": 4,
        },
        "apparent-wall": {
            "cancelled_ground_truth_quantity": 500,
            "market_filled_quantity": 0,
            "public_cause_attribution": "UNRESOLVED_FROM_PUBLIC_FEED",
            "public_possible_causes": list(DISPLAY_DECREASE_POSSIBLE_CAUSES),
        },
        "small-displayed-deep-hidden": {
            "displayed_before": 50,
            "filled_quantity": 300,
            "hidden_fill_quantity": 250,
            "public_trade_count": 2,
        },
    }[result.name]
    failures: list[str] = []
    if result.summary != expected:
        failures.append(f"unexpected scenario summary: {result.summary}")
    if not result.replay.passed:
        failures.append("scenario did not exactly replay truth and public feed")
    try:
        result.venue.assert_invariants()
    except RuntimeError as error:
        failures.append(f"scenario invariant failure: {error}")
    return HiddenLiquidityAuditCase(
        f"scenario_{result.name.replace('-', '_')}",
        {
            "observable_event_sha256": result.venue.observable_event_sha256(),
            "recording_sha256": result.recording.sha256(),
            "replay_status": "PASS" if result.replay.passed else "FAIL",
            "summary": result.summary,
            "timeline_lines": len(result.timeline.splitlines()),
            "truth_event_sha256": result.venue.truth_event_sha256(),
        },
        tuple(failures),
    )


def _blind_pair_case(blind) -> HiddenLiquidityAuditCase:
    shallow_truth = blind.shallow.venue.post_session_ground_truth().sha256()
    deep_truth = blind.deep.venue.post_session_ground_truth().sha256()
    failures: list[str] = []
    if blind.shallow.summary["initial_observable_sha256"] != blind.deep.summary[
        "initial_observable_sha256"
    ]:
        failures.append("paired books were observably different before action")
    if shallow_truth == deep_truth:
        failures.append("paired books did not contain materially different truth")
    if (blind.shallow_fill_quantity, blind.deep_fill_quantity) != (100, 250):
        failures.append("identical action did not expose shallow/deep fill difference")
    if not blind.shallow.replay.passed or not blind.deep.replay.passed:
        failures.append("one side of blind pair failed exact replay")
    return HiddenLiquidityAuditCase(
        "identical_displayed_books_different_hidden_liquidity",
        {
            "deep_fill_quantity": blind.deep_fill_quantity,
            "deep_truth_sha256": deep_truth,
            "initial_observable_sha256": blind.initial_observable_sha256,
            "inference_evidence": "fills,replenishment,tape",
            "shallow_fill_quantity": blind.shallow_fill_quantity,
            "shallow_truth_sha256": shallow_truth,
        },
        tuple(failures),
    )


def _architectural_boundary_case() -> HiddenLiquidityAuditCase:
    venue = HiddenLiquidityVenue()
    venue.submit_resting(_displayed("BOUNDARY-BID", Side.BUY, 100, 99))
    venue.submit_resting(_hidden("BOUNDARY-HIDDEN", Side.SELL, 500, 101))
    reveal_blocked = False
    try:
        venue.post_session_ground_truth()
    except RuntimeError:
        reveal_blocked = True
    feed = venue.observable_feed()
    public_json = json.dumps(feed.as_dict(), sort_keys=True).lower()
    forbidden = {
        "reserve_quantity",
        "hidden_quantity",
        "priority_sequence",
        "maker_order_id",
        "liquidity_source",
        "ground_truth",
    }
    leaked = sorted(field for field in forbidden if field in public_json)
    venue.complete_session()
    truth = venue.post_session_ground_truth()
    failures: list[str] = []
    if not reveal_blocked:
        failures.append("ground truth was available before session completion")
    if leaked or hasattr(feed, "venue"):
        failures.append("observable value contains a truth field or venue backlink")
    if truth.label != "SIMULATOR_GROUND_TRUTH_POST_SESSION":
        failures.append("post-session reveal lacks explicit ground-truth label")
    return HiddenLiquidityAuditCase(
        "architectural_truth_feed_boundary_and_reveal_gate",
        {
            "feed_representation": feed.as_dict()["representation"],
            "forbidden_fields_found": leaked,
            "post_session_label": truth.label,
            "pre_completion_reveal_blocked": reveal_blocked,
            "strategy_book_type": type(feed.strategy_book).__name__,
        },
        tuple(failures),
    )


def _queue_estimation_case() -> HiddenLiquidityAuditCase:
    rules = HiddenLiquidityRules(hidden_priority=HiddenPriority.BEFORE_DISPLAYED)
    venue = HiddenLiquidityVenue(rules)
    venue.submit_resting(_displayed("QUEUE-SIM", Side.BUY, 100, 99))
    venue.submit_resting(_hidden("QUEUE-HIDDEN", Side.BUY, 200, 99))
    venue.submit_resting(
        _displayed(
            "QUEUE-PLAYER",
            Side.BUY,
            50,
            99,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
        )
    )
    feed = venue.observable_feed()
    estimator = QueuePositionEstimator()
    aggregate = estimator.estimate(
        feed,
        "QUEUE-PLAYER",
        data_mode=QueueDataMode.AGGREGATED_DEPTH,
    )
    mbo = estimator.estimate(
        feed,
        "QUEUE-PLAYER",
        data_mode=QueueDataMode.MARKET_BY_ORDER,
        market_by_order_quantity_ahead=300,
    )
    own = next(order for order in feed.own_orders if order.order_id == "QUEUE-PLAYER")
    failures: list[str] = []
    if aggregate.is_exact or aggregate.estimated_quantity_ahead != 100:
        failures.append("aggregate feed claimed exact queue or used hidden quantity")
    if aggregate.lower_bound != 0 or aggregate.upper_bound < 100:
        failures.append("aggregate queue estimate has invalid uncertainty bounds")
    if not mbo.is_exact or mbo.estimated_quantity_ahead != 300:
        failures.append("MBO mode did not preserve supplied exact evidence")
    if (own.original_quantity, own.remaining_quantity, own.filled_quantity) != (
        50,
        50,
        0,
    ):
        failures.append("own acknowledged state was not exact")
    venue.complete_session()
    return HiddenLiquidityAuditCase(
        "opponent_queue_estimate_and_precise_own_state",
        {
            "aggregated_estimate": aggregate.as_dict(),
            "market_by_order_estimate": mbo.as_dict(),
            "own_order": own.as_dict(),
        },
        tuple(failures),
    )


def _traffic_light_boundary_case() -> HiddenLiquidityAuditCase:
    shallow = HiddenLiquidityVenue()
    deep = HiddenLiquidityVenue()
    for venue in (shallow, deep):
        venue.submit_resting(_displayed("TRAFFIC-BID", Side.BUY, 100, 99))
    shallow.submit_resting(_displayed("TRAFFIC-ASK-S", Side.SELL, 100, 101))
    deep.submit_resting(
        _iceberg(
            "TRAFFIC-ASK-D",
            Side.SELL,
            101,
            100,
            400,
            100,
        )
    )
    shallow_feed = shallow.observable_feed()
    deep_feed = deep.observable_feed()
    definition = parse_strategy(
        """setup observable_boundary
window 1s
GREEN when
    best_ask_size == 100
WAIT when
    best_ask_size > 100
RED otherwise
"""
    )
    if not isinstance(definition, StrategyDefinition):
        raise RuntimeError("traffic-light audit parsed the wrong strategy kind")
    shallow_runtime = TrafficLightRuntime(definition, Decimal(1))
    deep_runtime = TrafficLightRuntime(definition, Decimal(1))
    shallow_eval = shallow_runtime.reset(0, shallow_feed.strategy_book).evaluation
    deep_eval = deep_runtime.reset(0, deep_feed.strategy_book).evaluation
    failures: list[str] = []
    if shallow_eval.as_dict() != deep_eval.as_dict():
        failures.append("traffic script distinguished identical displayed books")
    feature_json = json.dumps(shallow_eval.features.as_dict(), sort_keys=True).lower()
    if "reserve" in feature_json or "hidden" in feature_json or "queue_ahead" in feature_json:
        failures.append("traffic feature snapshot contains hidden state")
    return HiddenLiquidityAuditCase(
        "traffic_light_receives_only_observable_strategy_book",
        {
            "evaluation_identical": shallow_eval.as_dict() == deep_eval.as_dict(),
            "feature_keys": sorted(shallow_eval.features.as_dict()),
            "signal": shallow_eval.state.value,
            "strategy_input_type": type(shallow_feed.strategy_book).__name__,
        },
        tuple(failures),
    )


def _display_disappearance_case(result) -> HiddenLiquidityAuditCase:
    events = [
        event
        for event in result.venue.observable_feed().events
        if event.event_type is ObservableEventType.DISPLAY_QUANTITY_CHANGED
        and event.data.get("new_displayed_quantity") == 0
    ]
    failures: list[str] = []
    if not events:
        failures.append("scenario omitted displayed-quantity disappearance")
        possible: list[str] = []
        attribution = None
    else:
        possible = list(events[-1].data.get("possible_causes", []))
        attribution = events[-1].data.get("cause_attribution")
        if tuple(possible) != DISPLAY_DECREASE_POSSIBLE_CAUSES:
            failures.append("public disappearance omitted plausible causes")
        if attribution != "UNRESOLVED_FROM_PUBLIC_FEED":
            failures.append("public feed asserted a private disappearance cause")
    return HiddenLiquidityAuditCase(
        "display_disappearance_preserves_causal_uncertainty",
        {"cause_attribution": attribution, "possible_causes": possible},
        tuple(failures),
    )


def _feed_delay_case() -> HiddenLiquidityAuditCase:
    venue = HiddenLiquidityVenue(HiddenLiquidityRules(feed_delay_us=50))
    venue.submit_resting(_displayed("DELAY-BID", Side.BUY, 100, 99))
    empty_at_source = venue.observable_feed().book.best_bid is None
    venue.advance_to(49)
    empty_before_delivery = venue.observable_feed().book.best_bid is None
    venue.advance_to(50)
    feed = venue.observable_feed()
    delivered = feed.book.best_bid == 99
    causal_times = all(
        event.source_time_us == 0 and event.received_time_us == 50
        for event in feed.events
    )
    venue.execute_market(
        "DELAY-PLAYER-SELL",
        Side.SELL,
        10,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-DELAY",
    )
    position_at_source = venue.observable_feed().player_position.position
    venue.advance_to(99)
    position_before_fill_delivery = venue.observable_feed().player_position.position
    venue.advance_to(100)
    position_at_fill_delivery = venue.observable_feed().player_position.position
    venue.complete_session()
    venue.advance_to(150)
    failures = []
    if not (
        empty_at_source
        and empty_before_delivery
        and delivered
        and causal_times
        and position_at_source == 0
        and position_before_fill_delivery == 0
        and position_at_fill_delivery == -10
    ):
        failures.append("feed delay did not preserve stale public state and causal times")
    return HiddenLiquidityAuditCase(
        "simulation_time_feed_delay_and_quote_age",
        {
            "causal_times_valid": causal_times,
            "displayed_at_50us": delivered,
            "empty_at_0us": empty_at_source,
            "empty_at_49us": empty_before_delivery,
            "position_at_fill_delivery": position_at_fill_delivery,
            "position_at_source": position_at_source,
        },
        tuple(failures),
    )


def _refresh_priority_case() -> HiddenLiquidityAuditCase:
    outcomes: dict[str, str] = {}
    failures: list[str] = []
    for rule in (
        IcebergRefreshPriority.PRESERVE,
        IcebergRefreshPriority.LOSE,
    ):
        venue = HiddenLiquidityVenue(
            HiddenLiquidityRules(iceberg_refresh_priority=rule)
        )
        venue.submit_resting(
            _iceberg("PRIORITY-ICE", Side.SELL, 101, 50, 100, 50)
        )
        venue.submit_resting(
            _displayed("PRIORITY-PLAIN", Side.SELL, 100, 101)
        )
        venue.execute_market("PRIORITY-BUY-1", Side.BUY, 50)
        venue.execute_market("PRIORITY-BUY-2", Side.BUY, 50)
        venue.complete_session()
        trades = [
            event
            for event in venue.post_session_ground_truth().events
            if event.event_type is TruthEventType.TRADE
        ]
        outcomes[rule.value] = str(trades[-1].data["maker_order_id"])
    if outcomes != {
        "PRESERVE": "PRIORITY-ICE",
        "LOSE": "PRIORITY-PLAIN",
    }:
        failures.append("configured iceberg refresh priority did not alter allocation")
    return HiddenLiquidityAuditCase(
        "explicit_iceberg_refresh_priority_rule",
        {"second_fill_maker": outcomes},
        tuple(failures),
    )


def _unobservable_scoring_case(blind) -> HiddenLiquidityAuditCase:
    shallow = blind.shallow_score
    deep = blind.deep_score
    failures: list[str] = []
    if shallow.hidden_liquidity_penalty or deep.hidden_liquidity_penalty:
        failures.append("score penalized the player for unobservable hidden state")
    if (
        shallow.hidden_liquidity_scoring_status != "NOT_SCORED_UNOBSERVABLE"
        or deep.hidden_liquidity_scoring_status != "NOT_SCORED_UNOBSERVABLE"
    ):
        failures.append("score omitted explicit unobservable-liquidity status")
    if deep.revealed_hidden_liquidity <= shallow.revealed_hidden_liquidity:
        failures.append("post-session score did not report deeper revealed liquidity")
    return HiddenLiquidityAuditCase(
        "scoring_excludes_unobservable_hidden_liquidity",
        {"deep": deep.as_dict(), "shallow": shallow.as_dict()},
        tuple(failures),
    )


def _manual_refresh_replay_case() -> HiddenLiquidityAuditCase:
    venue = HiddenLiquidityVenue(
        HiddenLiquidityRules(
            feed_delay_us=10,
            iceberg_refresh_priority=IcebergRefreshPriority.PRESERVE,
        )
    )
    commands: list[ObservabilityCommand] = []

    def record(time_us: int, kind: str, parameters: dict[str, object]) -> None:
        commands.append(
            ObservabilityCommand(len(commands) + 1, time_us, kind, parameters)
        )

    bid = _displayed("MANUAL-BID", Side.BUY, 100, 99)
    definition = IcebergDefinition(
        50,
        100,
        50,
        IcebergRefreshBehavior.MANUAL,
        RefreshEventVisibility.EXPLICIT_REPLENISHMENT,
    )
    ask = HiddenOrderRequest(
        "MANUAL-ASK",
        Side.SELL,
        LiquidityKind.ICEBERG,
        OrderOwner.SIMULATED,
        "SIM-MANUAL",
        definition.total_quantity,
        101,
        definition,
    )
    for request in (bid, ask):
        venue.submit_resting(request)
        record(0, "SUBMIT", {"request": request.as_dict()})
    venue.advance_to(20)
    venue.execute_market("MANUAL-BUY", Side.BUY, 50)
    record(
        20,
        "MARKET",
        {
            "account_id": "AGGRESSOR",
            "order_id": "MANUAL-BUY",
            "owner": OrderOwner.SIMULATED.value,
            "quantity": 50,
            "side": Side.BUY.value,
        },
    )
    venue.advance_to(30)
    dormant = venue.observable_feed().book.best_ask is None
    venue.refresh_order("MANUAL-ASK")
    record(30, "REFRESH", {"order_id": "MANUAL-ASK"})
    venue.advance_to(40)
    refreshed = venue.observable_feed().book.best_ask == 101
    venue.complete_session()
    record(40, "COMPLETE", {})
    venue.advance_to(50)
    recording = recording_json_round_trip(
        ObservabilityRecording.capture(venue, tuple(commands))
    )
    replay = replay_observability_recording(recording)
    failures: list[str] = []
    if not dormant or not refreshed:
        failures.append("manual iceberg did not remain dormant then refresh")
    if not replay.passed:
        failures.append("manual refresh and feed delay did not replay exactly")
    return HiddenLiquidityAuditCase(
        "manual_refresh_feed_delay_exact_replay",
        {
            "dormant_after_slice_depletion": dormant,
            "recording_sha256": recording.sha256(),
            "refreshed_after_command": refreshed,
            "replay_status": "PASS" if replay.passed else "FAIL",
        },
        tuple(failures),
    )


def _venue_permission_case() -> HiddenLiquidityAuditCase:
    venue = HiddenLiquidityVenue(
        HiddenLiquidityRules(
            allow_fully_hidden=False,
            allow_midpoint_hidden=False,
        )
    )
    hidden_rejected = midpoint_rejected = False
    try:
        venue.submit_resting(_hidden("DISALLOWED-H", Side.BUY, 10, 99))
    except ValueError:
        hidden_rejected = True
    try:
        venue.submit_resting(
            _hidden(
                "DISALLOWED-M",
                Side.BUY,
                10,
                None,
                kind=LiquidityKind.MIDPOINT_HIDDEN,
            )
        )
    except ValueError:
        midpoint_rejected = True
    venue.complete_session()
    failures = []
    if not hidden_rejected or not midpoint_rejected:
        failures.append("venue accepted a disabled hidden-liquidity mechanism")
    return HiddenLiquidityAuditCase(
        "venue_config_gates_hidden_and_midpoint_liquidity",
        {
            "fully_hidden_rejected": hidden_rejected,
            "midpoint_hidden_rejected": midpoint_rejected,
        },
        tuple(failures),
    )


def _displayed(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    *,
    owner: OrderOwner = OrderOwner.SIMULATED,
    account_id: str = "SIM",
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.DISPLAYED_LIMIT,
        owner,
        account_id,
        quantity,
        price_ticks,
    )


def _hidden(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int | None,
    *,
    kind: LiquidityKind = LiquidityKind.HIDDEN_LIMIT,
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        kind,
        OrderOwner.SIMULATED,
        "SIM-HIDDEN",
        quantity,
        price_ticks,
    )


def _iceberg(
    order_id: str,
    side: Side,
    price_ticks: int,
    display: int,
    reserve: int,
    refresh: int,
) -> HiddenOrderRequest:
    definition = IcebergDefinition(
        display,
        reserve,
        refresh,
        IcebergRefreshBehavior.AUTOMATIC,
        RefreshEventVisibility.QUOTE_UPDATE_ONLY,
    )
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.ICEBERG,
        OrderOwner.SIMULATED,
        "SIM-ICEBERG",
        definition.total_quantity,
        price_ticks,
        definition,
    )
