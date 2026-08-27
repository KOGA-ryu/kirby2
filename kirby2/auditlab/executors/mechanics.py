"""Real market-mechanics executor for generated audit cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.exchange import (
    AdvancedOrderRequest,
    MarketMechanicsEngine,
    MechanicsEvent,
    MechanicsEventType,
    MechanicsRecording,
    OrderInstruction,
    OrderOwner,
    SessionState,
    Side,
    replay_mechanics_recording,
)
from kirby2.exchange.mechanics_scenarios import MechanicsScenarioBuilder
from kirby2.immutable import thaw_json

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


MECHANICS_RECORDING_TYPE = "NATIVE_MECHANICS_RECORDING"
_RECORDING_FIELDS = frozenset({"configuration", "native_recording"})
_AUCTION_SESSION = {
    "OPENING": SessionState.OPENING_AUCTION,
    "REOPENING": SessionState.REOPENING_AUCTION,
    "CLOSING": SessionState.CLOSING_AUCTION,
}
_SESSION_ROUTES = {
    SessionState.CLOSED: (
        SessionState.PREOPEN,
        SessionState.CLOSED,
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.PREOPEN: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.OPENING_AUCTION: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.CONTINUOUS: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.HALTED: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
        SessionState.HALTED,
        SessionState.REOPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.REOPENING_AUCTION: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
        SessionState.HALTED,
        SessionState.REOPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.CLOSING_AUCTION: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
        SessionState.CLOSING_AUCTION,
        SessionState.POSTCLOSE,
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
    SessionState.POSTCLOSE: (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
        SessionState.POSTCLOSE,
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
    ),
}


class MechanicsExecutor:
    """Execute generated mechanics dimensions through the production engine."""

    lane = ExecutorLane.MECHANICS

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        scenario = _build_scenario(configuration)
        recording = CaseRecording(
            lane=self.lane,
            recording_type=MECHANICS_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "native_recording": scenario.recording.as_dict(),
            },
        )
        return _result(
            configuration,
            recording,
            scenario.recording,
            scenario.engine,
            replay_mismatches=(),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("mechanics replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("mechanics replay received a different lane")
        if recording.recording_type != MECHANICS_RECORDING_TYPE:
            raise ValueError("unsupported mechanics recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("mechanics recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("mechanics recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_native = payload["native_recording"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("mechanics configuration must be an object")
        if not isinstance(raw_native, dict):
            raise TypeError("native mechanics recording must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        native = MechanicsRecording.from_dict(raw_native)
        replay = replay_mechanics_recording(native)
        mismatches = [] if replay.passed else [
            "native_event_stream" if not replay.event_stream_match else "",
            "native_final_state" if not replay.state_match else "",
        ]
        return _result(
            configuration,
            recording,
            native,
            replay.engine,
            replay_mismatches=tuple(item for item in mismatches if item),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("mechanics executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("mechanics executor received a different lane")
        SessionState(configuration.session_phase)
        if configuration.order_types not in {
            "LIMIT_ONLY",
            "MARKET_AND_LIMIT",
            "IOC_FOK_POST_ONLY",
            "CANCEL_REPLACE",
        }:
            raise ValueError("unsupported generated mechanics order family")
        if configuration.auction_state not in {"NONE", *_AUCTION_SESSION}:
            raise ValueError("unsupported generated auction state")


@dataclass(slots=True)
class _Program:
    builder: MechanicsScenarioBuilder
    time_us: int = 0
    expiry_horizon_us: int = 0

    def transition(self, state: SessionState, reason: str) -> None:
        self.time_us += 10
        self.builder.transition(self.time_us, state, reason)

    def submit(self, request: AdvancedOrderRequest) -> None:
        self.time_us += 10
        self.builder.submit(self.time_us, request)
        if request.good_until_time_us is not None:
            self.expiry_horizon_us = max(
                self.expiry_horizon_us,
                request.good_until_time_us,
            )

    def cancel(self, order_id: str, reason: str) -> None:
        self.time_us += 10
        self.builder.cancel(
            self.time_us,
            order_id,
            reason=reason,
        )

    def replace(
        self,
        order_id: str,
        *,
        new_order_id: str,
        new_quantity: int,
        new_price_ticks: int | None = None,
    ) -> None:
        self.time_us += 10
        self.builder.replace(
            self.time_us,
            order_id,
            new_order_id=new_order_id,
            new_quantity=new_quantity,
            new_price_ticks=new_price_ticks,
        )

    def uncross(self) -> None:
        self.time_us += 10
        self.builder.uncross(self.time_us)


def _build_scenario(configuration: GeneratedConfiguration):
    builder = MechanicsScenarioBuilder()
    program = _Program(builder)
    target = SessionState(configuration.session_phase)
    for state in _SESSION_ROUTES[target]:
        program.transition(state, f"AUDIT_SESSION_TARGET_{target.value}")
    if builder.engine.session_state is not SessionState.CONTINUOUS:
        raise RuntimeError("session probe did not return to continuous trading")
    _run_instruction_family(program, configuration.order_types)
    _run_auction_family(program, configuration.auction_state)
    completed_time_us = max(
        program.time_us + 50,
        program.expiry_horizon_us + 1,
    )
    return builder.finish(
        f"audit-{configuration.cell_id}",
        completed_time_us,
        {
            "auction_state": configuration.auction_state,
            "order_types": configuration.order_types,
            "session_phase": configuration.session_phase,
        },
    )


def _run_instruction_family(program: _Program, family: str) -> None:
    runners = {
        "LIMIT_ONLY": _run_limit_only,
        "MARKET_AND_LIMIT": _run_market_and_limit,
        "IOC_FOK_POST_ONLY": _run_immediate_instructions,
        "CANCEL_REPLACE": _run_cancel_replace,
    }
    runners[family](program)


def _run_limit_only(program: _Program) -> None:
    program.submit(
        _limit(
            "INS-LIMIT-DAY",
            Side.BUY,
            50,
            98,
            "ACCOUNT-LIMIT-DAY",
        )
    )
    program.submit(
        _limit(
            "INS-LIMIT-GTC",
            Side.BUY,
            50,
            97,
            "ACCOUNT-LIMIT-GTC",
            time_in_force=OrderInstruction.GTC,
        )
    )
    program.submit(
        _limit(
            "INS-LIMIT-SESSION",
            Side.BUY,
            50,
            96,
            "ACCOUNT-LIMIT-SESSION",
            time_in_force=OrderInstruction.SESSION,
        )
    )
    expiry = program.time_us + 40
    program.submit(
        _limit(
            "INS-LIMIT-GTD",
            Side.BUY,
            50,
            95,
            "ACCOUNT-LIMIT-GTD",
            time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=expiry,
        )
    )


def _run_market_and_limit(program: _Program) -> None:
    program.submit(
        _limit(
            "INS-MARKET-ASK",
            Side.SELL,
            40,
            101,
            "ACCOUNT-MARKET-ASK",
        )
    )
    program.submit(
        _market(
            "INS-MARKET-BUY",
            Side.BUY,
            60,
            "ACCOUNT-MARKET-BUY",
            owner=OrderOwner.PLAYER,
        )
    )


def _run_immediate_instructions(program: _Program) -> None:
    program.submit(
        _limit(
            "INS-IOC-ASK",
            Side.SELL,
            50,
            101,
            "ACCOUNT-IOC-ASK",
        )
    )
    program.submit(
        _limit(
            "INS-IOC-BUY",
            Side.BUY,
            80,
            101,
            "ACCOUNT-IOC-BUY",
            owner=OrderOwner.PLAYER,
            time_in_force=OrderInstruction.IOC,
        )
    )
    program.submit(
        _limit(
            "INS-FOK-ASK",
            Side.SELL,
            40,
            102,
            "ACCOUNT-FOK-ASK",
        )
    )
    program.submit(
        _limit(
            "INS-FOK-BUY",
            Side.BUY,
            80,
            102,
            "ACCOUNT-FOK-BUY",
            time_in_force=OrderInstruction.FOK,
        )
    )
    program.submit(
        _limit(
            "INS-POST-BUY",
            Side.BUY,
            40,
            102,
            "ACCOUNT-POST-BUY",
            modifiers=frozenset({OrderInstruction.POST_ONLY}),
        )
    )


def _run_cancel_replace(program: _Program) -> None:
    program.submit(
        _limit(
            "INS-REPLACE-A",
            Side.BUY,
            100,
            99,
            "ACCOUNT-REPLACE-A",
        )
    )
    program.submit(
        _limit(
            "INS-REPLACE-B",
            Side.BUY,
            100,
            99,
            "ACCOUNT-REPLACE-B",
        )
    )
    program.replace(
        "INS-REPLACE-A",
        new_order_id="INS-REDUCE-REQUEST",
        new_quantity=60,
    )
    program.replace(
        "INS-REPLACE-A",
        new_order_id="INS-REPLACED-A",
        new_quantity=80,
        new_price_ticks=98,
    )
    program.cancel("INS-REPLACE-B", "AUDIT_EXPLICIT_CANCEL")


def _run_auction_family(program: _Program, auction_state: str) -> None:
    if auction_state == "NONE":
        program.submit(
            _limit(
                "AUC-NONE-BID",
                Side.BUY,
                25,
                95,
                "ACCOUNT-AUC-NONE",
            )
        )
        program.cancel("AUC-NONE-BID", "AUDIT_CONTINUOUS_AUCTION_NONE")
        return
    if auction_state == "OPENING":
        program.transition(SessionState.POSTCLOSE, "AUDIT_OPENING_RESET")
        program.transition(SessionState.PREOPEN, "AUDIT_OPENING_CALL")
    elif auction_state == "REOPENING":
        program.transition(SessionState.HALTED, "AUDIT_REOPENING_HALT")
    else:
        program.transition(SessionState.CLOSING_AUCTION, "AUDIT_CLOSING_CALL")

    for request in _auction_requests():
        program.submit(request)
    session = _AUCTION_SESSION[auction_state]
    if program.builder.engine.session_state is not session:
        program.transition(session, f"AUDIT_{auction_state}_UNCROSS")
    program.uncross()
    if auction_state in {"OPENING", "REOPENING"}:
        program.transition(SessionState.CONTINUOUS, f"AUDIT_{auction_state}_COMPLETE")
    else:
        program.transition(SessionState.POSTCLOSE, "AUDIT_CLOSING_COMPLETE")


def _auction_requests() -> tuple[AdvancedOrderRequest, ...]:
    return (
        _limit(
            "AUC-BUY",
            Side.BUY,
            100,
            102,
            "ACCOUNT-AUC-BUY",
            owner=OrderOwner.PLAYER,
            auction_only=True,
        ),
        _limit(
            "AUC-SELL-BETTER",
            Side.SELL,
            60,
            100,
            "ACCOUNT-AUC-SELL-BETTER",
            auction_only=True,
        ),
        _limit(
            "AUC-SELL-CLEAR",
            Side.SELL,
            40,
            102,
            "ACCOUNT-AUC-SELL-CLEAR",
            auction_only=True,
        ),
        _limit(
            "AUC-SELL-NONCROSS",
            Side.SELL,
            25,
            104,
            "ACCOUNT-AUC-SELL-NONCROSS",
            auction_only=True,
        ),
    )


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: MechanicsRecording,
    engine: MarketMechanicsEngine,
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    engine.assert_invariants()
    checks = _checks(configuration, native, engine)
    exercises = _exercises(configuration, recording, engine)
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"MECHANICS_{check.name.upper()}",
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
            code=f"MECHANICS_{exercise.capability.upper()}_NOT_EXERCISED",
            message=(
                f"configured mechanics dimension was not exercised: "
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
                code="MECHANICS_REPLAY_MISMATCH",
                message="native mechanics recording did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    event_counts = Counter(event.event_type.value for event in engine.events)
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.MECHANICS,
        recording=recording,
        event_projection=tuple(
            {"record_type": "mechanics_event", **event.as_dict()}
            for event in engine.events
        ),
        final_state_projection={
            "auction_orders": [
                order.as_dict()
                for order in engine.orders
                if order.request.auction_only
            ],
            "book": engine.book.runtime_state(),
            "clock_time_us": engine.clock.current_time_us,
            "last_trade_price_ticks": engine.last_trade_price_ticks,
            "managed_orders": [order.as_dict() for order in engine.orders],
            "mechanics_state_sha256": engine.state_sha256(),
            "player_position_shares": engine.player_position,
            "session_state": engine.session_state.value,
        },
        metrics={
            "auction_fill_count": event_counts[MechanicsEventType.AUCTION_FILL.value],
            "auction_matched_volume_shares": sum(
                int(event.data["quantity"])
                for event in engine.events
                if event.event_type is MechanicsEventType.AUCTION_FILL
            ),
            "core_trade_count": len(engine.book.trades),
            "event_count": len(engine.events),
            "managed_order_count": len(engine.orders),
            "player_position_shares": engine.player_position,
            "simulation_duration_us": engine.clock.current_time_us,
        },
        exercises=exercises,
        checks=checks,
        failures=tuple(failures),
        observable_projection=_observable_projection(engine),
    )


def _exercises(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    engine: MarketMechanicsEngine,
) -> tuple[ExerciseRecord, ...]:
    target_reason = f"AUDIT_SESSION_TARGET_{configuration.session_phase}"
    session_events = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.SESSION_STATE_CHANGED
        and event.data.get("current_state") == configuration.session_phase
        and event.data.get("reason") == target_reason
    ]
    instruction_ok, instruction_evidence = _instruction_evidence(
        configuration.order_types,
        engine,
    )
    auction_ok, auction_evidence = _auction_exercise_evidence(
        configuration.auction_state,
        engine,
    )
    common = {
        "executor": type(engine).__name__,
        "recording_sha256": recording.sha256,
    }
    return (
        ExerciseRecord(
            ExecutorLane.MECHANICS,
            "session_phase",
            configuration.session_phase,
            (
                ExerciseStatus.EXERCISED
                if session_events
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "event_sequences": [event.sequence for event in session_events],
                "transition_reason": target_reason,
            },
        ),
        ExerciseRecord(
            ExecutorLane.MECHANICS,
            "order_types",
            configuration.order_types,
            (
                ExerciseStatus.EXERCISED
                if instruction_ok
                else ExerciseStatus.NOT_EXERCISED
            ),
            {**common, **instruction_evidence},
        ),
        ExerciseRecord(
            ExecutorLane.MECHANICS,
            "auction_state",
            configuration.auction_state,
            (
                ExerciseStatus.EXERCISED
                if auction_ok
                else ExerciseStatus.NOT_EXERCISED
            ),
            {**common, **auction_evidence},
        ),
    )


def _instruction_evidence(
    family: str,
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    orders = {
        order.request.order_id: order
        for order in engine.orders
        if order.request.order_id.startswith("INS-")
    }
    events = [
        event for event in engine.events if _event_mentions_prefix(event, "INS-")
    ]
    event_types = {event.event_type for event in events}
    reasons = {
        str(event.data["reason"])
        for event in events
        if "reason" in event.data
    }
    requests = {
        order_id: order.request.as_dict() for order_id, order in orders.items()
    }
    core_events = [
        event.as_dict()
        for event in engine.book.journal.events
        if "INS-" in str(event.as_dict())
    ]
    if family == "LIMIT_ONLY":
        required = {
            "INS-LIMIT-DAY",
            "INS-LIMIT-GTC",
            "INS-LIMIT-SESSION",
            "INS-LIMIT-GTD",
        }
        passed = (
            required <= set(orders)
            and {
                orders[item].request.time_in_force for item in required
            }
            == {
                OrderInstruction.DAY,
                OrderInstruction.GTC,
                OrderInstruction.SESSION,
                OrderInstruction.GOOD_UNTIL_TIME,
            }
            and all(
                orders[item].request.instruction is OrderInstruction.LIMIT
                for item in required
            )
            and orders["INS-LIMIT-GTD"].status == "EXPIRED"
            and orders["INS-LIMIT-GTC"].status == "WORKING"
            and "INS-LIMIT-GTC" in engine.book.active_orders
            and "GOOD_UNTIL_TIME" in reasons
        )
    elif family == "MARKET_AND_LIMIT":
        required = {"INS-MARKET-ASK", "INS-MARKET-BUY"}
        passed = (
            required <= set(orders)
            and orders["INS-MARKET-ASK"].request.instruction
            is OrderInstruction.LIMIT
            and orders["INS-MARKET-BUY"].request.instruction
            is OrderInstruction.MARKET
            and orders["INS-MARKET-BUY"].filled_quantity == 40
            and orders["INS-MARKET-BUY"].expired_quantity == 20
            and MechanicsEventType.TRADE in event_types
        )
    elif family == "IOC_FOK_POST_ONLY":
        required = {
            "INS-IOC-ASK",
            "INS-IOC-BUY",
            "INS-FOK-ASK",
            "INS-FOK-BUY",
            "INS-POST-BUY",
        }
        passed = (
            required <= set(orders)
            and orders["INS-IOC-BUY"].request.time_in_force
            is OrderInstruction.IOC
            and orders["INS-IOC-BUY"].filled_quantity == 50
            and orders["INS-IOC-BUY"].expired_quantity == 30
            and orders["INS-FOK-BUY"].request.time_in_force
            is OrderInstruction.FOK
            and orders["INS-FOK-BUY"].status == "REJECTED"
            and orders["INS-POST-BUY"].request.post_only
            and orders["INS-POST-BUY"].status == "REJECTED"
            and "FOK_INSUFFICIENT_IMMEDIATE_QUANTITY" in reasons
            and "POST_ONLY_WOULD_CROSS" in reasons
        )
    else:
        required = {
            "INS-REPLACE-A",
            "INS-REPLACE-B",
            "INS-REPLACED-A",
        }
        preserved = [
            event
            for event in events
            if event.event_type is MechanicsEventType.PRIORITY_PRESERVED
        ]
        lost = [
            event
            for event in events
            if event.event_type is MechanicsEventType.PRIORITY_LOST
        ]
        replacements = [
            event
            for event in events
            if event.event_type is MechanicsEventType.ORDER_REPLACED
            and event.data.get("new_order_id") == "INS-REPLACED-A"
        ]
        original_adds = [
            event
            for event in core_events
            if event["type"] == "ORDER_ADDED"
            and event["data"].get("order_id") == "INS-REPLACE-A"
        ]
        reductions = [
            event
            for event in core_events
            if event["type"] == "ORDER_REDUCED"
            and event["data"].get("order_id") == "INS-REPLACE-A"
        ]
        replacement_adds = [
            event
            for event in core_events
            if event["type"] == "ORDER_ADDED"
            and event["data"].get("order_id") == "INS-REPLACED-A"
        ]
        priority_preserved_in_core = (
            len(original_adds) == 1
            and len(reductions) == 1
            and reductions[0]["data"].get("priority_preserved") is True
            and original_adds[0]["data"].get("resting_sequence")
            == reductions[0]["data"].get("resting_sequence")
        )
        priority_lost_in_core = (
            len(replacement_adds) == 1
            and len(original_adds) == 1
            and int(replacement_adds[0]["data"]["resting_sequence"])
            > int(original_adds[0]["data"]["resting_sequence"])
            and replacement_adds[0]["data"].get("price_ticks") == 98
        )
        passed = (
            required <= set(orders)
            and len(preserved) == 1
            and preserved[0].data.get("new_total_quantity") == 60
            and len(lost) == 1
            and lost[0].data.get("reason") == "PRICE_CHANGE"
            and len(replacements) == 1
            and replacements[0].data.get("replacement_accepted") is True
            and replacements[0].data.get("replacement_leaves_quantity") == 80
            and MechanicsEventType.ORDER_CANCELLED in event_types
            and orders["INS-REPLACE-A"].status == "CANCELLED"
            and orders["INS-REPLACE-B"].status == "CANCELLED"
            and orders["INS-REPLACED-A"].request.quantity == 80
            and priority_preserved_in_core
            and priority_lost_in_core
        )
    return passed, {
        "canonical_time_in_force_names": sorted(
            item.value
            for item in {
                OrderInstruction.DAY,
                OrderInstruction.GTC,
                OrderInstruction.SESSION,
                OrderInstruction.GOOD_UNTIL_TIME,
            }
        ),
        "event_sequences": [event.sequence for event in events],
        "event_types": sorted(event_type.value for event_type in event_types),
        "core_event_sha256": canonical_sha256(core_events),
        "requests": requests,
        "statuses": {
            order_id: order.status for order_id, order in orders.items()
        },
    }


def _auction_exercise_evidence(
    configured: str,
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    events = [
        event for event in engine.events if _event_mentions_prefix(event, "AUC-")
    ]
    auction_orders = [
        order
        for order in engine.orders
        if order.request.order_id.startswith("AUC-")
        and order.request.auction_only
    ]
    uncrosses = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_UNCROSS
    ]
    if configured == "NONE":
        order = next(
            (
                item
                for item in engine.orders
                if item.request.order_id == "AUC-NONE-BID"
            ),
            None,
        )
        passed = (
            order is not None
            and not order.request.auction_only
            and order.status == "CANCELLED"
            and engine.session_state is SessionState.CONTINUOUS
            and not auction_orders
            and not uncrosses
        )
        return passed, {
            "auction_order_count": 0,
            "continuous_order_status": None if order is None else order.status,
            "event_sequences": [event.sequence for event in events],
            "final_session_state": engine.session_state.value,
            "uncross_count": len(uncrosses),
        }
    expected_session = _AUCTION_SESSION[configured]
    relevant_uncrosses = [
        event
        for event in uncrosses
        if event.data.get("session_state") == expected_session.value
    ]
    indications = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_INDICATION
    ]
    fills = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_FILL
    ]
    noncross = next(
        (
            order
            for order in auction_orders
            if order.request.order_id == "AUC-SELL-NONCROSS"
        ),
        None,
    )
    passed = (
        len(auction_orders) == 4
        and len(relevant_uncrosses) == 1
        and bool(indications)
        and bool(fills)
        and noncross is not None
        and noncross.filled_quantity == 0
        and noncross.expired_quantity == noncross.request.quantity
    )
    return passed, {
        "auction_order_ids": [order.request.order_id for order in auction_orders],
        "event_sequences": [event.sequence for event in events],
        "fill_sequences": [event.sequence for event in fills],
        "indication_sequences": [event.sequence for event in indications],
        "noncrossing_interest_expired_shares": (
            None if noncross is None else noncross.expired_quantity
        ),
        "uncross_sequences": [event.sequence for event in relevant_uncrosses],
        "uncross_session_state": expected_session.value,
    }


def _checks(
    configuration: GeneratedConfiguration,
    native: MechanicsRecording,
    engine: MarketMechanicsEngine,
) -> tuple[CheckResult, ...]:
    lifecycle_ok, lifecycle = _lifecycle_projection(engine)
    quantity_ok, quantity = _quantity_conservation(engine)
    fifo_ok, fifo = _fifo_book_ordering(engine)
    allocation_ok, allocation = _auction_allocation(
        configuration.auction_state,
        engine,
    )
    indication_ok, indication = _auction_indication(
        configuration.auction_state,
        engine,
    )
    monotonic_ok, monotonic = _monotonic_timeline(native, engine)
    return (
        _check("order_lifecycle_reconciliation", lifecycle_ok, lifecycle),
        _check("quantity_conservation", quantity_ok, quantity),
        _check("fifo_book_ordering", fifo_ok, fifo),
        _check(
            "auction_allocation_reconciliation",
            allocation_ok,
            allocation,
        ),
        _check(
            "auction_indication_reconciliation",
            indication_ok,
            indication,
        ),
        _check("monotonic_event_time", monotonic_ok, monotonic),
    )


def _lifecycle_projection(
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    projected = {
        order.request.order_id: {
            "cancelled_quantity": 0,
            "entry_events": [],
            "expired_quantity": 0,
            "fill_sequences": [],
            "filled_quantity": 0,
            "terminal_sequence": None,
        }
        for order in engine.orders
    }
    invalid_after_terminal: list[str] = []

    def fill(order_id: str, quantity: int, sequence: int) -> None:
        item = projected[order_id]
        terminal = item["terminal_sequence"]
        if isinstance(terminal, int) and sequence > terminal:
            invalid_after_terminal.append(order_id)
        item["filled_quantity"] = int(item["filled_quantity"]) + quantity
        fill_sequences = item["fill_sequences"]
        if isinstance(fill_sequences, list):
            fill_sequences.append(sequence)

    for event in engine.events:
        data = event.data
        if event.event_type in {
            MechanicsEventType.ORDER_ACCEPTED,
            MechanicsEventType.ORDER_REJECTED,
            MechanicsEventType.AUCTION_ORDER_ADDED,
        }:
            order_id = str(data["order_id"])
            entries = projected[order_id]["entry_events"]
            if isinstance(entries, list):
                entries.append(event.event_type.value)
            if event.event_type is MechanicsEventType.ORDER_REJECTED:
                projected[order_id]["terminal_sequence"] = event.sequence
        elif event.event_type is MechanicsEventType.TRADE:
            quantity = int(data["quantity"])
            fill(str(data["maker_order_id"]), quantity, event.sequence)
            fill(str(data["taker_order_id"]), quantity, event.sequence)
        elif event.event_type is MechanicsEventType.AUCTION_FILL:
            quantity = int(data["quantity"])
            fill(str(data["buy_order_id"]), quantity, event.sequence)
            fill(str(data["sell_order_id"]), quantity, event.sequence)
        elif event.event_type is MechanicsEventType.PRIORITY_PRESERVED:
            order_id = str(data["order_id"])
            original = engine.get_order(order_id).request.quantity
            projected[order_id]["cancelled_quantity"] = (
                original - int(data["new_total_quantity"])
            )
        elif event.event_type in {
            MechanicsEventType.ORDER_CANCELLED,
            MechanicsEventType.AUCTION_ORDER_CANCELLED,
        }:
            order_id = str(data["order_id"])
            projected[order_id]["cancelled_quantity"] = int(
                data["cancelled_quantity"]
            )
            projected[order_id]["terminal_sequence"] = event.sequence
        elif event.event_type is MechanicsEventType.ORDER_EXPIRED:
            order_id = str(data["order_id"])
            projected[order_id]["expired_quantity"] = int(
                projected[order_id]["expired_quantity"]
            ) + int(data["expired_quantity"])
            projected[order_id]["terminal_sequence"] = event.sequence

    mismatches: dict[str, object] = {}
    for order in engine.orders:
        order_id = order.request.order_id
        item = projected[order_id]
        entry_events = item["entry_events"]
        expected_entry = (
            MechanicsEventType.ORDER_REJECTED.value
            if order.status == "REJECTED"
            else MechanicsEventType.AUCTION_ORDER_ADDED.value
            if order.request.auction_only
            else MechanicsEventType.ORDER_ACCEPTED.value
        )
        actual = {
            "cancelled_quantity": int(item["cancelled_quantity"]),
            "expired_quantity": int(item["expired_quantity"]),
            "filled_quantity": int(item["filled_quantity"]),
            "remaining_quantity": (
                order.request.quantity
                - int(item["filled_quantity"])
                - int(item["cancelled_quantity"])
                - int(item["expired_quantity"])
            ),
        }
        expected = {
            "cancelled_quantity": order.cancelled_quantity,
            "expired_quantity": order.expired_quantity,
            "filled_quantity": order.filled_quantity,
            "remaining_quantity": order.remaining_quantity,
        }
        terminal = item["terminal_sequence"]
        status_reconciled = {
            "REJECTED": entry_events == [MechanicsEventType.ORDER_REJECTED.value],
            "FILLED": (
                actual["filled_quantity"] == order.request.quantity
                and actual["remaining_quantity"] == 0
            ),
            "CANCELLED": (
                actual["cancelled_quantity"] > 0
                and actual["remaining_quantity"] == 0
                and isinstance(terminal, int)
            ),
            "EXPIRED": (
                actual["expired_quantity"] > 0
                and actual["remaining_quantity"] == 0
                and isinstance(terminal, int)
            ),
            "WORKING": (
                actual["remaining_quantity"] > 0
                and order_id in engine.book.active_orders
            ),
            "PARTIALLY_FILLED": (
                actual["filled_quantity"] > 0
                and actual["remaining_quantity"] > 0
                and (
                    order_id in engine.book.active_orders
                    or order.request.auction_only
                )
            ),
            "AUCTION_WORKING": (
                actual["remaining_quantity"] > 0
                and order.request.auction_only
            ),
        }.get(order.status, False)
        if (
            entry_events != [expected_entry]
            or actual != expected
            or not status_reconciled
        ):
            mismatches[order_id] = {
                "actual": actual,
                "entry_events": entry_events,
                "expected": expected,
                "expected_entry": expected_entry,
                "status": order.status,
                "status_reconciled": status_reconciled,
            }
    passed = not mismatches and not invalid_after_terminal
    return passed, {
        "invalid_fill_after_terminal_order_ids": sorted(
            set(invalid_after_terminal)
        ),
        "lifecycle_sha256": canonical_sha256(projected),
        "mismatches": mismatches,
        "order_count": len(projected),
    }


def _quantity_conservation(
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    managed_failures = []
    for order in engine.orders:
        quantities = (
            order.filled_quantity,
            order.cancelled_quantity,
            order.expired_quantity,
            order.remaining_quantity,
        )
        if min(quantities) < 0 or sum(quantities) != order.request.quantity:
            managed_failures.append(order.request.order_id)
    core_failures = []
    for order in engine.book.all_orders.values():
        quantities = (
            order.filled_quantity,
            order.cancelled_quantity,
            order.remaining_quantity,
        )
        if min(quantities) < 0 or sum(quantities) != order.original_quantity:
            core_failures.append(order.order_id)
    passed = not managed_failures and not core_failures
    return passed, {
        "core_order_count": len(engine.book.all_orders),
        "core_order_failures": core_failures,
        "managed_order_count": len(engine.orders),
        "managed_order_failures": managed_failures,
    }


def _fifo_book_ordering(
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    book = engine.book
    queued_ids: list[str] = []
    queue_sequences: list[list[int]] = []
    levels_ok = True
    for side, prices, levels in (
        (Side.BUY, book.bid_prices, book.bids),
        (Side.SELL, book.ask_prices, book.asks),
    ):
        levels_ok = levels_ok and prices == sorted(
            prices,
            reverse=side is Side.BUY,
        )
        levels_ok = levels_ok and set(prices) == set(levels)
        for price in prices:
            level = levels[price]
            sequences = [
                int(order.resting_sequence)
                for order in level.orders
                if order.resting_sequence is not None
            ]
            queue_sequences.append(sequences)
            queued_ids.extend(order.order_id for order in level.orders)
            levels_ok = levels_ok and sequences == sorted(sequences)
            levels_ok = levels_ok and len(sequences) == len(set(sequences))
            levels_ok = levels_ok and level.total_quantity == sum(
                order.remaining_quantity for order in level.orders
            )
            levels_ok = levels_ok and all(
                order.side is side
                and order.price_ticks == price
                and order.remaining_quantity > 0
                for order in level.orders
            )
    non_crossed = (
        book.best_bid is None
        or book.best_ask is None
        or book.best_bid < book.best_ask
    )
    passed = (
        levels_ok
        and non_crossed
        and len(queued_ids) == len(set(queued_ids))
        and set(queued_ids) == set(book.active_orders)
    )
    return passed, {
        "active_order_count": len(book.active_orders),
        "ask_prices_ticks": book.ask_prices,
        "best_ask_ticks": book.best_ask,
        "best_bid_ticks": book.best_bid,
        "bid_prices_ticks": book.bid_prices,
        "non_crossed": non_crossed,
        "queue_resting_sequences": queue_sequences,
    }


def _auction_allocation(
    configured: str,
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    uncrosses = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_UNCROSS
    ]
    fills = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_FILL
    ]
    if configured == "NONE":
        continuous = engine.get_order("AUC-NONE-BID")
        passed = (
            not uncrosses
            and not fills
            and not continuous.request.auction_only
            and continuous.status == "CANCELLED"
            and engine.session_state is SessionState.CONTINUOUS
        )
        return passed, {
            "actual_fills": [],
            "continuous_probe_status": continuous.status,
            "final_session_state": engine.session_state.value,
            "uncross_count": len(uncrosses),
        }
    if len(uncrosses) != 1:
        return False, {
            "actual_fills": [event.as_dict() for event in fills],
            "failure": "expected exactly one configured auction uncross",
            "uncross_count": len(uncrosses),
        }
    uncross = uncrosses[0]
    indication = uncross.data.get("indication")
    if not isinstance(indication, Mapping):
        return False, {"failure": "uncross omitted native indication"}
    clearing_price = indication.get("clearing_price_ticks")
    if type(clearing_price) is not int:
        return False, {"failure": "uncross omitted integer clearing price"}
    actual = [
        {
            "buy_order_id": str(event.data["buy_order_id"]),
            "price_ticks": int(event.data["price_ticks"]),
            "quantity": int(event.data["quantity"]),
            "sell_order_id": str(event.data["sell_order_id"]),
        }
        for event in fills
    ]
    expected = _expected_auction_fills(engine, clearing_price)
    execution_quantity = sum(int(item["quantity"]) for item in actual)
    buy_quantity = sum(
        order.filled_quantity
        for order in engine.orders
        if order.request.auction_only and order.request.side is Side.BUY
    )
    sell_quantity = sum(
        order.filled_quantity
        for order in engine.orders
        if order.request.auction_only and order.request.side is Side.SELL
    )
    actual_matched = int(uncross.data["actual_matched_quantity"])
    allocated: Counter[str] = Counter()
    for item in actual:
        allocated[item["buy_order_id"]] += item["quantity"]
        allocated[item["sell_order_id"]] += item["quantity"]
    order_limits = {
        order.request.order_id: order.request.quantity
        for order in engine.orders
        if order.request.auction_only
    }
    within_limits = all(
        quantity <= order_limits[order_id]
        for order_id, quantity in allocated.items()
    )
    noncrossing = engine.get_order("AUC-SELL-NONCROSS")
    passed = all(
        (
            actual == expected,
            execution_quantity
            == buy_quantity
            == sell_quantity
            == actual_matched,
            within_limits,
            noncrossing.filled_quantity == 0,
            noncrossing.expired_quantity == noncrossing.request.quantity,
        )
    )
    return passed, {
        "actual_fills": actual,
        "actual_matched_quantity": actual_matched,
        "allocated_by_order": dict(sorted(allocated.items())),
        "buy_fill_quantity": buy_quantity,
        "clearing_price_ticks": clearing_price,
        "expected_native_allocation": expected,
        "execution_event_quantity": execution_quantity,
        "noncrossing_interest_expired_quantity": noncrossing.expired_quantity,
        "order_original_quantities": order_limits,
        "sell_fill_quantity": sell_quantity,
        "within_original_order_limits": within_limits,
    }


def _expected_auction_fills(
    engine: MarketMechanicsEngine,
    clearing_price: int,
) -> list[dict[str, object]]:
    orders = [order for order in engine.orders if order.request.auction_only]
    buys = sorted(
        (
            order
            for order in orders
            if order.request.side is Side.BUY
            and _auction_marketable(order.request, clearing_price)
        ),
        key=lambda order: (
            0 if order.request.instruction is OrderInstruction.MARKET else 1,
            -int(order.request.price_ticks or 0),
            order.arrival_sequence,
        ),
    )
    sells = sorted(
        (
            order
            for order in orders
            if order.request.side is Side.SELL
            and _auction_marketable(order.request, clearing_price)
        ),
        key=lambda order: (
            0 if order.request.instruction is OrderInstruction.MARKET else 1,
            int(order.request.price_ticks or 0),
            order.arrival_sequence,
        ),
    )
    remaining = {
        order.request.order_id: order.request.quantity for order in orders
    }
    expected: list[dict[str, object]] = []
    buy_index = 0
    sell_index = 0
    while buy_index < len(buys) and sell_index < len(sells):
        buy = buys[buy_index]
        sell = sells[sell_index]
        buy_id = buy.request.order_id
        sell_id = sell.request.order_id
        quantity = min(remaining[buy_id], remaining[sell_id])
        expected.append(
            {
                "buy_order_id": buy_id,
                "price_ticks": clearing_price,
                "quantity": quantity,
                "sell_order_id": sell_id,
            }
        )
        remaining[buy_id] -= quantity
        remaining[sell_id] -= quantity
        if not remaining[buy_id]:
            buy_index += 1
        if not remaining[sell_id]:
            sell_index += 1
    return expected


def _auction_indication(
    configured: str,
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    indications = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_INDICATION
    ]
    uncrosses = [
        event
        for event in engine.events
        if event.event_type is MechanicsEventType.AUCTION_UNCROSS
    ]
    if configured == "NONE":
        passed = not indications and not uncrosses
        return passed, {
            "indication_count": len(indications),
            "mode": "CONTINUOUS_NO_AUCTION",
            "uncross_count": len(uncrosses),
        }
    if not indications or len(uncrosses) != 1:
        return False, {
            "failure": "configured auction omitted indication or uncross",
            "indication_count": len(indications),
            "uncross_count": len(uncrosses),
        }
    uncross = uncrosses[0]
    preceding = [event for event in indications if event.sequence < uncross.sequence]
    if not preceding:
        return False, {"failure": "uncross had no preceding indication"}
    last = preceding[-1]
    indicated = last.data.get("indication")
    native = uncross.data.get("indication")
    if not isinstance(indicated, Mapping) or not isinstance(native, Mapping):
        return False, {"failure": "auction indication payload is invalid"}
    intervening_inputs = [
        event
        for event in engine.events
        if last.sequence < event.sequence < uncross.sequence
        and event.event_type
        in {
            MechanicsEventType.AUCTION_ORDER_ADDED,
            MechanicsEventType.AUCTION_ORDER_CANCELLED,
            MechanicsEventType.ORDER_EXPIRED,
        }
        and _event_mentions_prefix(event, "AUC-")
    ]
    stp = [
        event
        for event in engine.events
        if last.sequence < event.sequence < uncross.sequence
        if event.event_type is MechanicsEventType.SELF_TRADE_PREVENTION
    ]
    payload_match = dict(indicated) == dict(native)
    indicated_matched = int(indicated["matched_quantity"])
    actual_matched = int(uncross.data["actual_matched_quantity"])
    difference_explained = (
        indicated_matched == actual_matched
        or bool(intervening_inputs)
        or bool(stp)
    )
    passed = (
        payload_match or bool(intervening_inputs)
    ) and difference_explained
    return passed, {
        "actual_matched_quantity": actual_matched,
        "difference_explained": difference_explained,
        "indicated_matched_quantity": indicated_matched,
        "indication_event_sequence": last.sequence,
        "intervening_input_event_types": [
            event.event_type.value for event in intervening_inputs
        ],
        "native_indication_matches_last_observed": payload_match,
        "self_trade_prevention_count": len(stp),
        "uncross_event_sequence": uncross.sequence,
    }


def _monotonic_timeline(
    native: MechanicsRecording,
    engine: MarketMechanicsEngine,
) -> tuple[bool, dict[str, object]]:
    event_sequences = [event.sequence for event in engine.events]
    event_times = [event.simulation_time_us for event in engine.events]
    command_sequences = [command.sequence for command in native.commands]
    command_times = [command.simulation_time_us for command in native.commands]
    core_sequences = [event.sequence for event in engine.book.journal.events]
    previous = SessionState.CLOSED
    transition_chain_ok = True
    for event in engine.events:
        if event.event_type is not MechanicsEventType.SESSION_STATE_CHANGED:
            continue
        if event.data.get("previous_state") != previous.value:
            transition_chain_ok = False
        previous = SessionState(str(event.data["current_state"]))
    passed = all(
        (
            event_sequences == list(range(1, len(event_sequences) + 1)),
            event_times == sorted(event_times),
            all(time >= 0 for time in event_times),
            command_sequences == list(range(1, len(command_sequences) + 1)),
            command_times == sorted(command_times),
            all(time >= 0 for time in command_times),
            not event_times or event_times[-1] <= native.completed_time_us,
            not command_times or command_times[-1] <= native.completed_time_us,
            core_sequences == list(range(1, len(core_sequences) + 1)),
            transition_chain_ok,
            previous is engine.session_state,
        )
    )
    return passed, {
        "command_count": len(command_sequences),
        "completed_time_us": native.completed_time_us,
        "core_event_count": len(core_sequences),
        "event_count": len(event_sequences),
        "final_session_state": engine.session_state.value,
        "transition_chain_reconciled": transition_chain_ok,
    }


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
            f"real mechanics check passed: {name}"
            if passed
            else f"real mechanics check failed: {name}"
        ),
        evidence={"source": "MechanicsExecutor", **evidence},
    )


def _observable_projection(engine: MarketMechanicsEngine) -> dict[str, object]:
    return {
        "book": {
            "ask_depth_shares": sum(
                engine.book.asks[price].total_quantity
                for price in engine.book.ask_prices
            ),
            "best_ask_ticks": engine.book.best_ask,
            "best_bid_ticks": engine.book.best_bid,
            "bid_depth_shares": sum(
                engine.book.bids[price].total_quantity
                for price in engine.book.bid_prices
            ),
        },
        "event_type_counts": dict(
            sorted(Counter(event.event_type.value for event in engine.events).items())
        ),
        "last_trade_price_ticks": engine.last_trade_price_ticks,
        "player_position_shares": engine.player_position,
        "representation": "AGGREGATED_MARKET_MECHANICS",
        "session_state": engine.session_state.value,
    }


def _event_mentions_prefix(event: MechanicsEvent, prefix: str) -> bool:
    return any(
        isinstance(value, str) and value.startswith(prefix)
        for value in _nested_values(event.data)
    )


def _nested_values(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(
            item
            for child in value.values()
            for item in _nested_values(child)
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            item for child in value for item in _nested_values(child)
        )
    return (value,)


def _auction_marketable(
    request: AdvancedOrderRequest,
    clearing_price: int,
) -> bool:
    if request.instruction is OrderInstruction.MARKET:
        return True
    if request.side is Side.BUY:
        return int(request.price_ticks) >= clearing_price
    return int(request.price_ticks) <= clearing_price


def _limit(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    account_id: str,
    *,
    owner: OrderOwner = OrderOwner.SIMULATED,
    time_in_force: OrderInstruction = OrderInstruction.DAY,
    modifiers: frozenset[OrderInstruction] = frozenset(),
    good_until_time_us: int | None = None,
    auction_only: bool = False,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        OrderInstruction.LIMIT,
        owner,
        account_id,
        price_ticks,
        time_in_force,
        modifiers,
        good_until_time_us,
        auction_only,
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
