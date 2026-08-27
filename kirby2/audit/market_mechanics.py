"""Runtime acceptance audit for advanced exchange and session mechanics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
from kirby2.exchange.mechanics_models import (
    AdvancedOrderRequest,
    InstrumentRules,
    MechanicsEventType,
    OrderInstruction,
    ScheduledSessionState,
    SelfTradePreventionMode,
    SessionSchedule,
    SessionState,
)
from kirby2.exchange.mechanics_scenarios import (
    MechanicsScenarioResult,
    run_all_mechanics_scenarios,
)
from kirby2.exchange.mechanics_replay import (
    MechanicsCommand,
    MechanicsRecording,
    replay_mechanics_recording,
)
from kirby2.exchange.models import OrderOwner, Side


@dataclass(frozen=True, slots=True)
class MarketMechanicsAuditCase:
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


def audit_market_mechanics() -> tuple[MarketMechanicsAuditCase, ...]:
    scenarios = run_all_mechanics_scenarios()
    return (
        *(_scenario_case(result) for result in scenarios),
        _rule_inventory_and_schedule_case(),
        _replace_priority_case(),
        _cancel_replace_replay_case(),
        _time_in_force_case(),
        _immediate_instruction_case(),
        _self_trade_prevention_case(),
        _generic_protection_case(),
        _auction_allocation_case(scenarios[0]),
        _event_logging_inventory_case(scenarios),
    )


def _scenario_case(result: MechanicsScenarioResult) -> MarketMechanicsAuditCase:
    failures: list[str] = []
    expected = {
        "opening-auction": {
            "clearing_price_ticks": 102,
            "matched_quantity": 100,
            "player_position": 100,
            "session_state": "CONTINUOUS",
        },
        "closing-auction": {
            "clearing_price_ticks": 100,
            "imbalance_quantity": 20,
            "matched_quantity": 80,
            "session_state": "POSTCLOSE",
        },
        "halt-during-momentum": {
            "last_trade_price_ticks": 101,
            "player_position": 50,
            "protection_trigger_count": 1,
            "session_state": "HALTED",
        },
        "reopening-gap": {
            "clearing_price_ticks": 104,
            "gap_ticks": 4,
            "matched_quantity": 100,
            "session_state": "CONTINUOUS",
        },
        "ioc-partial-fill": {
            "expired_quantity": 50,
            "filled_quantity": 50,
            "remaining_quantity": 0,
            "status": "EXPIRED",
        },
        "fok-rejection": {
            "filled_quantity": 0,
            "player_position": 0,
            "status": "REJECTED",
        },
        "post-only-rejection": {
            "best_ask_ticks": 101,
            "filled_quantity": 0,
            "player_position": 0,
            "status": "REJECTED",
        },
    }[result.name]
    for key, value in expected.items():
        if result.summary.get(key) != value:
            failures.append(
                f"{result.name} expected {key}={value}, got {result.summary.get(key)}"
            )
    if not result.replay.passed:
        failures.append(f"{result.name} did not replay exactly")
    try:
        result.engine.assert_invariants()
    except RuntimeError as error:
        failures.append(f"{result.name} invariant failure: {error}")
    evidence = {
        "event_count": len(result.engine.events),
        "event_stream_sha256": result.engine.event_stream_sha256(),
        "recording_sha256": result.recording.sha256(),
        "replay_status": "PASS" if result.replay.passed else "FAIL",
        "summary": result.summary,
    }
    return MarketMechanicsAuditCase(
        f"scenario_{result.name.replace('-', '_')}",
        evidence,
        tuple(failures),
    )


def _rule_inventory_and_schedule_case() -> MarketMechanicsAuditCase:
    schedule = SessionSchedule(
        (
            ScheduledSessionState(0, SessionState.PREOPEN),
            ScheduledSessionState(10, SessionState.OPENING_AUCTION),
            ScheduledSessionState(20, SessionState.CONTINUOUS),
            ScheduledSessionState(100, SessionState.CLOSING_AUCTION),
            ScheduledSessionState(110, SessionState.POSTCLOSE),
        )
    )
    rules = InstrumentRules(
        tick_size=Decimal("0.05"),
        lot_size=10,
        minimum_quantity=10,
        maximum_quantity=1_000,
        lower_price_band_ticks=80,
        upper_price_band_ticks=120,
        session_schedule=schedule,
        preserve_priority_on_quantity_reduction=True,
        reference_price_ticks=100,
        price_collar_ticks=15,
        volatility_interruption_ticks=5,
        fat_finger_ticks=10,
        account_stp_modes=(
            ("PLAYER-1", SelfTradePreventionMode.CANCEL_AGGRESSOR),
            ("SIM-1", SelfTradePreventionMode.CANCEL_RESTING),
        ),
    )
    round_trip = InstrumentRules.from_dict(rules.as_dict())
    engine = MarketMechanicsEngine(round_trip)
    engine.advance_to(120)
    failures: list[str] = []
    required_instructions = {
        "LIMIT",
        "MARKET",
        "MARKETABLE_LIMIT",
        "IOC",
        "FOK",
        "POST_ONLY",
        "DAY",
        "SESSION",
        "GOOD_UNTIL_TIME",
    }
    required_states = {
        "CLOSED",
        "PREOPEN",
        "OPENING_AUCTION",
        "CONTINUOUS",
        "HALTED",
        "REOPENING_AUCTION",
        "CLOSING_AUCTION",
        "POSTCLOSE",
    }
    if {item.value for item in OrderInstruction} != required_instructions:
        failures.append("order instruction inventory is incomplete")
    if {item.value for item in SessionState} != required_states:
        failures.append("session state inventory is incomplete")
    if round_trip != rules:
        failures.append("instrument rule serialization is not lossless")
    if engine.session_state is not SessionState.POSTCLOSE:
        failures.append("configured session schedule did not reach POSTCLOSE")
    evidence = {
        "final_session_state": engine.session_state.value,
        "instructions": sorted(required_instructions),
        "rules_round_trip": round_trip == rules,
        "scheduled_transition_count": len(schedule.transitions),
        "session_states": sorted(required_states),
        "tick_size": str(rules.tick_size),
    }
    return MarketMechanicsAuditCase(
        "instrument_rules_instructions_and_session_schedule",
        evidence,
        tuple(failures),
    )


def _replace_priority_case() -> MarketMechanicsAuditCase:
    engine = _continuous_engine()
    engine.submit(_limit("PRIORITY-A", Side.BUY, 100, 99, "ACCOUNT-A"))
    engine.submit(_limit("PRIORITY-B", Side.BUY, 100, 99, "ACCOUNT-B"))
    original_sequence = engine.book.active_orders["PRIORITY-A"].resting_sequence
    reduced = engine.replace_order(
        "PRIORITY-A",
        new_order_id="REDUCE-REQUEST-1",
        new_quantity=60,
    )
    preserved_sequence = engine.book.active_orders["PRIORITY-A"].resting_sequence
    queue_after_reduce = [
        order.order_id for order in engine.book.bids[99].orders
    ]
    increased = engine.replace_order(
        "PRIORITY-A",
        new_order_id="PRIORITY-A-INCREASED",
        new_quantity=120,
    )
    queue_after_increase = [
        order.order_id for order in engine.book.bids[99].orders
    ]

    partial = _continuous_engine()
    partial.submit(
        _limit(
            "PARTIAL-ORIGINAL",
            Side.BUY,
            100,
            99,
            "PLAYER-PARTIAL",
            owner=OrderOwner.PLAYER,
        )
    )
    partial.submit(_market("PARTIAL-TAKER", Side.SELL, 30, "SIM-TAKER"))
    partial.replace_order(
        "PARTIAL-ORIGINAL",
        new_order_id="PARTIAL-REPLACEMENT",
        new_quantity=60,
        new_price_ticks=98,
    )
    partial_original = partial.get_order("PARTIAL-ORIGINAL")
    partial_replacement = partial.get_order("PARTIAL-REPLACEMENT")
    failures: list[str] = []
    if reduced is None or original_sequence != preserved_sequence:
        failures.append("permitted quantity reduction lost FIFO priority")
    if queue_after_reduce != ["PRIORITY-A", "PRIORITY-B"]:
        failures.append("quantity reduction changed queue order")
    if increased is None or queue_after_increase != [
        "PRIORITY-B",
        "PRIORITY-A-INCREASED",
    ]:
        failures.append("quantity increase did not lose priority")
    if (
        partial_original.filled_quantity != 30
        or partial_original.cancelled_quantity != 70
        or partial_replacement.remaining_quantity != 30
        or partial_replacement.request.quantity != 30
    ):
        failures.append("partially filled replacement did not conserve target total")
    event_types = {event.event_type for event in engine.events}
    if MechanicsEventType.PRIORITY_PRESERVED not in event_types:
        failures.append("priority-preservation event is absent")
    if MechanicsEventType.PRIORITY_LOST not in event_types:
        failures.append("priority-loss event is absent")
    evidence = {
        "original_resting_sequence": original_sequence,
        "preserved_resting_sequence": preserved_sequence,
        "queue_after_increase": queue_after_increase,
        "queue_after_reduce": queue_after_reduce,
        "partial_fill_before_replace": partial_original.filled_quantity,
        "partial_replacement_leaves": partial_replacement.remaining_quantity,
        "partial_target_total": 60,
    }
    return MarketMechanicsAuditCase(
        "cancel_replace_priority_rules",
        evidence,
        tuple(failures),
    )


def _cancel_replace_replay_case() -> MarketMechanicsAuditCase:
    engine = MarketMechanicsEngine()
    commands: list[MechanicsCommand] = []

    def record(
        simulation_time_us: int,
        command_type: str,
        parameters: dict[str, object],
    ) -> None:
        commands.append(
            MechanicsCommand(
                len(commands) + 1,
                simulation_time_us,
                command_type,
                parameters,
            )
        )

    for state, reason in (
        (SessionState.PREOPEN, "REPLAY_OPEN"),
        (SessionState.OPENING_AUCTION, "REPLAY_OPEN"),
    ):
        engine.transition_session(state, reason=reason)
        record(0, "TRANSITION", {"reason": reason, "state": state.value})
    engine.uncross_auction()
    record(0, "UNCROSS", {})
    engine.transition_session(SessionState.CONTINUOUS, reason="REPLAY_OPEN")
    record(
        0,
        "TRANSITION",
        {"reason": "REPLAY_OPEN", "state": SessionState.CONTINUOUS.value},
    )
    first = _limit("REPLAY-A", Side.BUY, 100, 99, "ACCOUNT-A")
    second = _limit("REPLAY-B", Side.BUY, 100, 99, "ACCOUNT-B")
    for simulation_time_us, request in ((10, first), (11, second)):
        engine.advance_to(simulation_time_us)
        engine.submit(request)
        record(
            simulation_time_us,
            "SUBMIT",
            {"request": request.as_dict()},
        )
    engine.advance_to(12)
    engine.replace_order(
        "REPLAY-A",
        new_order_id="REPLAY-REDUCE-REQUEST",
        new_quantity=60,
    )
    record(
        12,
        "REPLACE",
        {
            "new_order_id": "REPLAY-REDUCE-REQUEST",
            "new_price_ticks": None,
            "new_quantity": 60,
            "order_id": "REPLAY-A",
        },
    )
    engine.advance_to(13)
    engine.replace_order(
        "REPLAY-A",
        new_order_id="REPLAY-A-INCREASED",
        new_quantity=120,
    )
    record(
        13,
        "REPLACE",
        {
            "new_order_id": "REPLAY-A-INCREASED",
            "new_price_ticks": None,
            "new_quantity": 120,
            "order_id": "REPLAY-A",
        },
    )
    engine.advance_to(14)
    engine.cancel("REPLAY-B", reason="REPLAY_CANCEL")
    record(
        14,
        "CANCEL",
        {"order_id": "REPLAY-B", "reason": "REPLAY_CANCEL"},
    )
    engine.advance_to(20)
    captured = MechanicsRecording.capture(engine, tuple(commands))
    recording = MechanicsRecording.from_dict(
        json.loads(json.dumps(captured.as_dict(), sort_keys=True))
    )
    replay = replay_mechanics_recording(recording)
    event_types = {event.event_type for event in engine.events}
    required = {
        MechanicsEventType.ORDER_CANCELLED,
        MechanicsEventType.ORDER_REPLACED,
        MechanicsEventType.PRIORITY_LOST,
        MechanicsEventType.PRIORITY_PRESERVED,
    }
    failures: list[str] = []
    if not replay.passed:
        failures.append("cancel/replace command recording did not replay exactly")
    if not required <= event_types:
        failures.append("cancel/replace replay omitted required lifecycle events")
    evidence = {
        "command_count": len(commands),
        "event_stream_sha256": engine.event_stream_sha256(),
        "recording_sha256": recording.sha256(),
        "replay_event_stream_match": replay.event_stream_match,
        "replay_state_match": replay.state_match,
    }
    return MarketMechanicsAuditCase(
        "cancel_replace_exact_command_replay",
        evidence,
        tuple(failures),
    )


def _time_in_force_case() -> MarketMechanicsAuditCase:
    gut = _continuous_engine()
    gut.submit(
        AdvancedOrderRequest(
            "GUT-BID",
            Side.BUY,
            100,
            OrderInstruction.LIMIT,
            OrderOwner.PLAYER,
            "PLAYER-GUT",
            99,
            OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=100,
        )
    )
    gut.advance_to(99)
    active_before_expiry = "GUT-BID" in gut.book.active_orders
    gut.advance_to(100)
    gut_order = gut.get_order("GUT-BID")

    session = _continuous_engine()
    session.submit(
        _limit(
            "SESSION-BID",
            Side.BUY,
            100,
            99,
            "PLAYER-SESSION",
            time_in_force=OrderInstruction.SESSION,
        )
    )
    session.transition_session(SessionState.HALTED, reason="TIF_AUDIT_HALT")
    session_order = session.get_order("SESSION-BID")

    day = _continuous_engine()
    day.submit(_limit("DAY-BID", Side.BUY, 100, 99, "PLAYER-DAY"))
    day.transition_session(SessionState.CLOSING_AUCTION, reason="DAY_CLOSE")
    active_during_close = "DAY-BID" in day.book.active_orders
    day.transition_session(SessionState.POSTCLOSE, reason="DAY_END")
    day_order = day.get_order("DAY-BID")

    failures: list[str] = []
    if not active_before_expiry or gut_order.status != "EXPIRED":
        failures.append("GOOD_UNTIL_TIME did not expire at its exact timestamp")
    if session_order.status != "EXPIRED":
        failures.append("SESSION order survived the continuous-session boundary")
    if not active_during_close or day_order.status != "EXPIRED":
        failures.append("DAY order did not survive call then expire at postclose")
    evidence = {
        "day_active_during_closing_call": active_during_close,
        "day_final_status": day_order.status,
        "gut_active_at_99us": active_before_expiry,
        "gut_final_status": gut_order.status,
        "session_final_status": session_order.status,
    }
    return MarketMechanicsAuditCase(
        "day_session_and_good_until_time_expiration",
        evidence,
        tuple(failures),
    )


def _immediate_instruction_case() -> MarketMechanicsAuditCase:
    marketable = _continuous_engine()
    marketable.submit(_limit("ML-ASK", Side.SELL, 50, 101, "SIM-ASK"))
    marketable.submit(
        _limit(
            "ML-NONMARKETABLE",
            Side.BUY,
            100,
            100,
            "PLAYER-1",
            instruction=OrderInstruction.MARKETABLE_LIMIT,
        )
    )
    marketable.submit(
        _limit(
            "ML-BUY",
            Side.BUY,
            100,
            101,
            "PLAYER-1",
            instruction=OrderInstruction.MARKETABLE_LIMIT,
        )
    )
    nonmarketable = marketable.get_order("ML-NONMARKETABLE")
    marketable_order = marketable.get_order("ML-BUY")

    fok = _continuous_engine()
    fok.submit(_limit("FOK-FULL-ASK", Side.SELL, 100, 101, "SIM-ASK"))
    fok.submit(
        _limit(
            "FOK-FULL-BUY",
            Side.BUY,
            100,
            101,
            "PLAYER-FOK",
            time_in_force=OrderInstruction.FOK,
        )
    )
    fok_order = fok.get_order("FOK-FULL-BUY")

    post = _continuous_engine()
    post.submit(_limit("POST-PASSIVE-ASK", Side.SELL, 50, 102, "SIM-ASK"))
    post.submit(
        _limit(
            "POST-PASSIVE-BID",
            Side.BUY,
            100,
            101,
            "PLAYER-POST",
            modifiers=frozenset({OrderInstruction.POST_ONLY}),
        )
    )
    post_order = post.get_order("POST-PASSIVE-BID")
    failures: list[str] = []
    if nonmarketable.status != "REJECTED":
        failures.append("nonmarketable MARKETABLE_LIMIT was accepted")
    if (
        marketable_order.status != "PARTIALLY_FILLED"
        or marketable_order.filled_quantity != 50
        or marketable_order.remaining_quantity != 50
    ):
        failures.append("marketable limit fill-and-rest semantics are incorrect")
    if fok_order.status != "FILLED" or fok_order.filled_quantity != 100:
        failures.append("fully executable FOK did not fill atomically")
    if post_order.status != "WORKING" or post.book.best_bid != 101:
        failures.append("noncrossing post-only limit did not rest")
    evidence = {
        "fok_full_status": fok_order.status,
        "marketable_limit_filled": marketable_order.filled_quantity,
        "marketable_limit_remaining": marketable_order.remaining_quantity,
        "nonmarketable_status": nonmarketable.status,
        "post_only_passive_status": post_order.status,
    }
    return MarketMechanicsAuditCase(
        "marketable_limit_fok_and_post_only_semantics",
        evidence,
        tuple(failures),
    )


def _self_trade_prevention_case() -> MarketMechanicsAuditCase:
    outcomes: dict[str, dict[str, object]] = {}
    failures: list[str] = []
    for mode in (
        SelfTradePreventionMode.CANCEL_AGGRESSOR,
        SelfTradePreventionMode.CANCEL_RESTING,
        SelfTradePreventionMode.CANCEL_BOTH,
    ):
        rules = InstrumentRules(account_stp_modes=(("SHARED", mode),))
        engine = _continuous_engine(rules)
        owner = (
            OrderOwner.SIMULATED
            if mode is SelfTradePreventionMode.CANCEL_BOTH
            else OrderOwner.PLAYER
        )
        engine.submit(
            _limit(
                f"STP-SELL-{mode.value}",
                Side.SELL,
                50,
                101,
                "SHARED",
                owner=owner,
            )
        )
        engine.submit(
            _market(
                f"STP-BUY-{mode.value}",
                Side.BUY,
                50,
                "SHARED",
                owner=owner,
            )
        )
        trade_count = len(engine.book.trades)
        stp_count = sum(
            event.event_type is MechanicsEventType.SELF_TRADE_PREVENTION
            for event in engine.events
        )
        if trade_count or not stp_count:
            failures.append(f"{mode.value} allowed self-trade or omitted event")
        outcomes[mode.value] = {
            "owner": owner.value,
            "player_position": engine.player_position,
            "stp_event_count": stp_count,
            "trade_count": trade_count,
        }
    return MarketMechanicsAuditCase(
        "player_and_simulator_self_trade_prevention_modes",
        {"modes": outcomes},
        tuple(failures),
    )


def _generic_protection_case() -> MarketMechanicsAuditCase:
    rules = InstrumentRules(
        lot_size=10,
        minimum_quantity=10,
        maximum_quantity=100,
        lower_price_band_ticks=90,
        upper_price_band_ticks=110,
        reference_price_ticks=100,
        price_collar_ticks=3,
        fat_finger_ticks=5,
    )
    engine = _continuous_engine(rules)
    requests = (
        _limit("PROTECT-MAX", Side.BUY, 110, 100, "P1"),
        _limit("PROTECT-BAND", Side.BUY, 10, 111, "P2"),
        _limit("PROTECT-FAT", Side.BUY, 10, 106, "P3"),
        _limit("PROTECT-COLLAR", Side.BUY, 10, 104, "P4"),
    )
    for request in requests:
        engine.submit(request)
    protections = [
        str(event.data["protection"])
        for event in engine.events
        if event.event_type is MechanicsEventType.PROTECTION_TRIGGERED
    ]
    execution_collar = _continuous_engine(
        InstrumentRules(price_collar_ticks=3)
    )
    execution_collar.submit(
        _limit("COLLAR-ASK", Side.SELL, 10, 103, "COLLAR-ASK-ACCOUNT")
    )
    execution_collar.submit(
        _limit("COLLAR-BID", Side.BUY, 10, 97, "COLLAR-BID-ACCOUNT")
    )
    execution_collar.submit(
        _market("COLLAR-SELL", Side.SELL, 1, "COLLAR-SELL-ACCOUNT")
    )
    execution_collar.submit(
        _market("COLLAR-BUY", Side.BUY, 1, "COLLAR-BUY-ACCOUNT")
    )
    execution_collar_order = execution_collar.get_order("COLLAR-BUY")
    protections.extend(
        str(event.data["protection"])
        for event in execution_collar.events
        if event.event_type is MechanicsEventType.PROTECTION_TRIGGERED
    )
    statuses = {
        request.order_id: engine.get_order(request.order_id).status
        for request in requests
    }
    expected = {
        "MAXIMUM_ORDER_SIZE",
        "ORDER_PRICE_REJECTION",
        "FAT_FINGER_PROTECTION",
        "PRICE_COLLAR",
    }
    failures: list[str] = []
    if set(protections) != expected:
        failures.append("generic protection event inventory is incomplete")
    if set(statuses.values()) != {"REJECTED"}:
        failures.append("one or more protected orders were not rejected")
    if execution_collar_order.status != "REJECTED":
        failures.append("market execution escaped the dynamic price collar")
    evidence = {
        "dynamic_execution_collar_status": execution_collar_order.status,
        "protection_events": protections,
        "statuses": statuses,
    }
    return MarketMechanicsAuditCase(
        "price_band_collar_size_and_fat_finger_protections",
        evidence,
        tuple(failures),
    )


def _auction_allocation_case(
    opening: MechanicsScenarioResult,
) -> MarketMechanicsAuditCase:
    fills = [
        event
        for event in opening.engine.events
        if event.event_type is MechanicsEventType.AUCTION_FILL
    ]
    sell_order_ids = [str(event.data["sell_order_id"]) for event in fills]
    quantities = [int(event.data["quantity"]) for event in fills]
    failures: list[str] = []
    if sell_order_ids != ["OPEN-SELL-1", "OPEN-SELL-2"]:
        failures.append("auction allocation did not preserve price/FIFO priority")
    if quantities != [70, 30]:
        failures.append("auction uncross allocation quantities are incorrect")
    evidence = {
        "allocation_quantities": quantities,
        "sell_allocation_order": sell_order_ids,
        "tie_break": "max_match,min_abs_imbalance,closest_reference,lower_tick",
    }
    return MarketMechanicsAuditCase(
        "deterministic_auction_clearing_and_allocation",
        evidence,
        tuple(failures),
    )


def _event_logging_inventory_case(
    scenarios: tuple[MechanicsScenarioResult, ...],
) -> MarketMechanicsAuditCase:
    required = {
        MechanicsEventType.ORDER_REJECTED,
        MechanicsEventType.ORDER_EXPIRED,
        MechanicsEventType.AUCTION_INDICATION,
        MechanicsEventType.HALT,
        MechanicsEventType.RESUME,
        MechanicsEventType.AUCTION_FILL,
        MechanicsEventType.PROTECTION_TRIGGERED,
    }
    actual = {
        event.event_type
        for scenario in scenarios
        for event in scenario.engine.events
    }
    missing = required - actual
    evidence = {
        "required_event_types": sorted(item.value for item in required),
        "scenario_event_types": sorted(item.value for item in actual),
    }
    return MarketMechanicsAuditCase(
        "replay_event_inventory_for_mechanics_outcomes",
        evidence,
        ()
        if not missing
        else ("missing mechanics events: " + ",".join(sorted(item.value for item in missing)),),
    )


def _continuous_engine(
    rules: InstrumentRules | None = None,
) -> MarketMechanicsEngine:
    engine = MarketMechanicsEngine(rules)
    engine.transition_session(SessionState.PREOPEN, reason="AUDIT_OPEN")
    engine.transition_session(SessionState.OPENING_AUCTION, reason="AUDIT_OPEN")
    engine.uncross_auction()
    engine.transition_session(SessionState.CONTINUOUS, reason="AUDIT_OPEN")
    return engine


def _limit(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    account_id: str,
    *,
    owner: OrderOwner = OrderOwner.SIMULATED,
    instruction: OrderInstruction = OrderInstruction.LIMIT,
    time_in_force: OrderInstruction = OrderInstruction.DAY,
    modifiers: frozenset[OrderInstruction] = frozenset(),
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        instruction,
        owner,
        account_id,
        price_ticks,
        time_in_force,
        modifiers,
    )


def _market(
    order_id: str,
    side: Side,
    quantity: int,
    account_id: str,
    *,
    owner: OrderOwner = OrderOwner.SIMULATED,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        OrderInstruction.MARKET,
        owner,
        account_id,
    )
