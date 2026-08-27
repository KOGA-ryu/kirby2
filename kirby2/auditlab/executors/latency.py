"""Real asynchronous-latency executor for generated audit cases."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from kirby2.immutable import thaw_json
from kirby2.latency import (
    AsyncOrder,
    AsyncOrderState,
    AsynchronousExecutionSession,
    LatencyComponent,
    LatencyEvent,
    LatencyEventType,
    LatencyRecording,
    get_latency_profile,
    replay_latency_recording,
    run_cancel_race,
)
from kirby2.latency.scenarios import cancel_race_for_seed

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


LATENCY_RECORDING_TYPE = "NATIVE_LATENCY_RECORDING"
_PLAYER_ORDER_ID = "RACE-PLAYER-BID"
_RECORDING_FIELDS = frozenset(
    {
        "configuration",
        "native_recording",
        "observable_ready_time_us",
        "race_schedule",
    }
)
_EXPECTED_COMMAND_OFFSETS = {
    "cancel-wins": (2_000, 6_000, 10_000),
    "fill-wins": (2_000, 6_000, 8_000),
}
_ALLOWED_TRANSITIONS = {
    AsyncOrderState.CREATED: {
        AsyncOrderState.PENDING_NEW,
        AsyncOrderState.PENDING_CANCEL,
        AsyncOrderState.REJECTED,
    },
    AsyncOrderState.PENDING_NEW: {
        AsyncOrderState.WORKING,
        AsyncOrderState.PARTIALLY_FILLED,
        AsyncOrderState.PENDING_CANCEL,
        AsyncOrderState.FILLED,
        AsyncOrderState.REJECTED,
        AsyncOrderState.EXPIRED,
    },
    AsyncOrderState.WORKING: {
        AsyncOrderState.PARTIALLY_FILLED,
        AsyncOrderState.PENDING_CANCEL,
        AsyncOrderState.FILLED,
        AsyncOrderState.CANCELLED,
        AsyncOrderState.EXPIRED,
    },
    AsyncOrderState.PARTIALLY_FILLED: {
        AsyncOrderState.PENDING_CANCEL,
        AsyncOrderState.FILLED,
        AsyncOrderState.CANCELLED,
        AsyncOrderState.EXPIRED,
    },
    AsyncOrderState.PENDING_CANCEL: {
        AsyncOrderState.PARTIALLY_FILLED,
        AsyncOrderState.CANCELLED,
        AsyncOrderState.FILLED,
        AsyncOrderState.REJECTED,
        AsyncOrderState.EXPIRED,
    },
    AsyncOrderState.CANCELLED: set(),
    AsyncOrderState.FILLED: set(),
    AsyncOrderState.REJECTED: set(),
    AsyncOrderState.EXPIRED: set(),
}


class LatencyExecutor:
    """Execute one configured profile through the production message lifecycle."""

    lane = ExecutorLane.LATENCY

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        schedule = cancel_race_for_seed(configuration.seed)
        scenario = run_cancel_race(
            schedule,
            seed=configuration.seed,
            profile=configuration.latency,
        )
        recording = CaseRecording(
            lane=self.lane,
            recording_type=LATENCY_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "native_recording": scenario.recording.as_dict(),
                "observable_ready_time_us": scenario.observable_ready_time_us,
                "race_schedule": scenario.race.value,
            },
        )
        return _result(
            configuration,
            recording,
            scenario.recording,
            scenario.session,
            scenario.observable_ready_time_us,
            scenario.race.value,
            replay_mismatches=(),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("latency replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("latency replay received a different lane")
        if recording.recording_type != LATENCY_RECORDING_TYPE:
            raise ValueError("unsupported latency recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("latency recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("latency recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_native = payload["native_recording"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("latency configuration must be an object")
        if not isinstance(raw_native, dict):
            raise TypeError("native latency recording must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        native = LatencyRecording.from_dict(raw_native)
        ready_time_us = int(payload["observable_ready_time_us"])
        race_schedule = str(payload["race_schedule"])
        replay = replay_latency_recording(native)
        mismatches: list[str] = []
        if not replay.event_stream_match:
            mismatches.append("native_event_stream")
        if not replay.state_match:
            mismatches.append("native_final_state")
        if native.seed != configuration.seed:
            mismatches.append("native_seed")
        if native.profile.get("name") != configuration.latency:
            mismatches.append("native_profile")
        if race_schedule != cancel_race_for_seed(configuration.seed).value:
            mismatches.append("race_schedule")
        if _command_offsets(native, ready_time_us) != _EXPECTED_COMMAND_OFFSETS.get(
            race_schedule
        ):
            mismatches.append("command_offsets")
        return _result(
            configuration,
            recording,
            native,
            replay.session,
            ready_time_us,
            race_schedule,
            replay_mismatches=tuple(mismatches),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("latency executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("latency executor received a different lane")
        get_latency_profile(configuration.latency)


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: LatencyRecording,
    session: AsynchronousExecutionSession,
    ready_time_us: int,
    race_schedule: str,
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    session.assert_invariants()
    order = _player_order(session)
    checks = _checks(
        configuration,
        native,
        session,
        order,
        ready_time_us,
        race_schedule,
    )
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"LATENCY_{check.name.upper()}",
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
    exercise = _exercise(
        configuration,
        recording,
        native,
        session,
        order,
        ready_time_us,
        race_schedule,
    )
    if exercise.status is ExerciseStatus.NOT_EXERCISED:
        failures.append(
            FailureObservation(
                kind=FailureKind.EXECUTION_ERROR,
                code="LATENCY_PROFILE_NOT_EXERCISED",
                message="configured latency profile lacked native event evidence",
                evidence={
                    "configured_profile": configuration.latency,
                    "exercise_evidence_sha256": canonical_sha256(
                        exercise.as_dict()["evidence"]
                    ),
                },
            )
        )
    if replay_mismatches:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="LATENCY_REPLAY_MISMATCH",
                message="native latency recording did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    metrics = session.metrics(order.order_id)
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.LATENCY,
        recording=recording,
        event_projection=tuple(
            {"record_type": "latency_event", **event.as_dict()}
            for event in session.events
        ),
        final_state_projection={
            "book": session.book.runtime_state(),
            "client_position_shares": session.client_position,
            "clock_time_us": session.clock.current_time_us,
            "latest_display": (
                None
                if session.latest_display is None
                else session.latest_display.as_dict()
            ),
            "orders": [item.as_dict() for item in session.orders],
            "pending_event_horizon_us": session.pending_event_horizon_us,
            "player_position_shares": session.player_position,
            "session_state_sha256": session.state_sha256(),
        },
        metrics={
            **metrics.as_dict(),
            "event_count": len(session.events),
            "latency_draw_count": len(session.sampler.draws),
            "observable_ready_time_us": ready_time_us,
            "race_schedule": race_schedule,
            "simulation_duration_us": session.clock.current_time_us,
        },
        exercises=(exercise,),
        checks=checks,
        failures=tuple(failures),
        observable_projection=_observable_projection(session),
    )


def _exercise(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: LatencyRecording,
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
    ready_time_us: int,
    race_schedule: str,
) -> ExerciseRecord:
    relevant_events = tuple(
        event
        for event in session.events
        if event.order_id == order.order_id
        or event.event_type is LatencyEventType.EXTERNAL_AGGRESSIVE_ORDER
    )
    offsets = _command_offsets(native, ready_time_us)
    expected_offsets = _EXPECTED_COMMAND_OFFSETS.get(race_schedule)
    profile_name = native.profile.get("name")
    exercised = all(
        (
            session.profile.name.value == configuration.latency,
            profile_name == configuration.latency,
            native.seed == configuration.seed,
            bool(session.sampler.draws),
            bool(relevant_events),
            offsets == expected_offsets,
            order.cancel_race_outcome is not None,
            session.pending_event_horizon_us is None,
        )
    )
    return ExerciseRecord(
        ExecutorLane.LATENCY,
        "latency",
        configuration.latency,
        (
            ExerciseStatus.EXERCISED
            if exercised
            else ExerciseStatus.NOT_EXERCISED
        ),
        {
            "actual_outcome": order.cancel_race_outcome,
            "command_offsets_us": list(offsets),
            "event_sequences": [event.sequence for event in relevant_events],
            "latency_draw_sequences": [
                draw.sequence for draw in session.sampler.draws
            ],
            "native_profile_sha256": canonical_sha256(native.profile),
            "observable_ready_time_us": ready_time_us,
            "profile": profile_name,
            "race_schedule": race_schedule,
            "recording_sha256": recording.sha256,
        },
    )


def _checks(
    configuration: GeneratedConfiguration,
    native: LatencyRecording,
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
    ready_time_us: int,
    race_schedule: str,
) -> tuple[CheckResult, ...]:
    causal_ok, causal = _causal_timestamps(
        native,
        session,
        ready_time_us,
        race_schedule,
    )
    lifecycle_ok, lifecycle = _lifecycle_reconciliation(session, order)
    quantity_ok, quantity = _quantity_conservation(session, order)
    terminal_ok, terminal = _terminal_cancel_fill_ordering(session, order)
    race_ok, race = _cancel_race_reconciliation(session, order)
    metrics_ok, metrics = _latency_metric_reconciliation(
        configuration,
        session,
        order,
    )
    return (
        _check("causal_timestamps", causal_ok, causal),
        _check(
            "async_lifecycle_reconciliation",
            lifecycle_ok,
            lifecycle,
        ),
        _check("quantity_conservation", quantity_ok, quantity),
        _check(
            "terminal_cancel_fill_ordering",
            terminal_ok,
            terminal,
        ),
        _check("cancel_race_reconciliation", race_ok, race),
        _check(
            "latency_metric_reconciliation",
            metrics_ok,
            metrics,
        ),
    )


def _causal_timestamps(
    native: LatencyRecording,
    session: AsynchronousExecutionSession,
    ready_time_us: int,
    race_schedule: str,
) -> tuple[bool, dict[str, object]]:
    events = session.events
    event_sequences = [event.sequence for event in events]
    event_times = [event.simulation_time_us for event in events]
    command_sequences = [command.sequence for command in native.commands]
    command_times = [command.simulation_time_us for command in native.commands]
    new_chain = (
        _event(events, LatencyEventType.KEY_PRESSED, action="NEW_LIMIT"),
        _event(events, LatencyEventType.CLIENT_CREATED_ORDER),
        _event(events, LatencyEventType.ORDER_LEFT_CLIENT),
        _event(events, LatencyEventType.GATEWAY_RECEIVED_ORDER),
        _event(events, LatencyEventType.VENUE_RECEIVED_ORDER),
        _event(events, LatencyEventType.VENUE_ACKNOWLEDGED_ORDER),
        _event(events, LatencyEventType.CLIENT_RECEIVED_ACK),
    )
    cancel_chain = (
        _event(events, LatencyEventType.KEY_PRESSED, action="CANCEL"),
        _event(events, LatencyEventType.CANCEL_CREATED),
        _event(events, LatencyEventType.CANCEL_LEFT_CLIENT),
        _event(events, LatencyEventType.GATEWAY_RECEIVED_CANCEL),
        _event(events, LatencyEventType.VENUE_RECEIVED_CANCEL),
        _event(events, LatencyEventType.VENUE_ACKNOWLEDGED_CANCEL),
        _event(events, LatencyEventType.CLIENT_RECEIVED_CANCEL_ACK),
    )
    fill_events = tuple(
        event
        for event in events
        if event.order_id == _PLAYER_ORDER_ID
        and event.event_type is LatencyEventType.FILL_OCCURRED
    )
    fill_chain_ok = True
    if fill_events:
        fill_chain = (
            _event(events, LatencyEventType.EXTERNAL_AGGRESSIVE_ORDER),
            fill_events[0],
            _event(events, LatencyEventType.FILL_REPORT_LEFT_VENUE),
            _event(events, LatencyEventType.CLIENT_RECEIVED_FILL),
            _event(events, LatencyEventType.UI_DISPLAYED_FILL),
        )
        fill_chain_ok = _ordered_events(fill_chain)
    expected_offsets = _EXPECTED_COMMAND_OFFSETS.get(race_schedule)
    offsets = _command_offsets(native, ready_time_us)
    passed = all(
        (
            event_sequences == list(range(1, len(events) + 1)),
            event_times == sorted(event_times),
            all(time >= 0 for time in event_times),
            command_sequences == list(range(1, len(native.commands) + 1)),
            command_times == sorted(command_times),
            all(time >= 0 for time in command_times),
            offsets == expected_offsets,
            ready_time_us >= 0,
            _ordered_events(new_chain),
            _ordered_events(cancel_chain),
            fill_chain_ok,
            not event_times or event_times[-1] <= native.completed_time_us,
            session.clock.current_time_us == native.completed_time_us,
            session.pending_event_horizon_us is None,
        )
    )
    return passed, {
        "cancel_chain": _event_markers(cancel_chain),
        "command_offsets_us": list(offsets),
        "command_times_us": command_times,
        "event_count": len(events),
        "expected_command_offsets_us": (
            None if expected_offsets is None else list(expected_offsets)
        ),
        "fill_chain_present": bool(fill_events),
        "new_chain": _event_markers(new_chain),
        "observable_ready_time_us": ready_time_us,
        "pending_event_horizon_us": session.pending_event_horizon_us,
    }


def _lifecycle_reconciliation(
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> tuple[bool, dict[str, object]]:
    projected = AsyncOrderState.CREATED
    transitions: list[dict[str, object]] = []
    failures: list[str] = []
    terminal_marker: tuple[int, int] | None = None
    for event in session.events:
        if (
            event.order_id != order.order_id
            or event.event_type is not LatencyEventType.ORDER_STATE_CHANGED
        ):
            continue
        previous = AsyncOrderState(str(event.data["previous_state"]))
        current = AsyncOrderState(str(event.data["current_state"]))
        legal = previous is projected and current in _ALLOWED_TRANSITIONS[projected]
        if not legal:
            failures.append(
                f"{projected.value}:{previous.value}->{current.value}"
            )
        projected = current
        transitions.append(
            {
                "current": current.value,
                "previous": previous.value,
                "sequence": event.sequence,
                "simulation_time_us": event.simulation_time_us,
            }
        )
        if current.terminal and current is not AsyncOrderState.FILLED:
            terminal_marker = _marker(event)
    events_after_terminal = []
    if terminal_marker is not None:
        events_after_terminal = [
            event.sequence
            for event in session.events
            if event.order_id == order.order_id
            and event.event_type is LatencyEventType.FILL_OCCURRED
            and _marker(event) > terminal_marker
        ]
    passed = (
        not failures
        and projected is order.state
        and bool(transitions)
        and not events_after_terminal
        and (
            not order.state.terminal
            or order.state is AsyncOrderState.REJECTED
            or order.remaining_quantity == 0
        )
    )
    return passed, {
        "events_after_terminal": events_after_terminal,
        "final_projected_state": projected.value,
        "final_recorded_state": order.state.value,
        "illegal_transitions": failures,
        "transitions": transitions,
    }


def _quantity_conservation(
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> tuple[bool, dict[str, object]]:
    managed = {
        "cancelled": order.cancelled_quantity,
        "expired": order.expired_quantity,
        "filled": order.filled_quantity,
        "remaining": order.remaining_quantity,
    }
    core = session.book.all_orders.get(order.order_id)
    core_values = (
        None
        if core is None
        else {
            "cancelled": core.cancelled_quantity,
            "filled": core.filled_quantity,
            "remaining": core.remaining_quantity,
        }
    )
    latency_fill_quantity = sum(
        int(event.data["quantity"])
        for event in session.events
        if event.order_id == order.order_id
        and event.event_type is LatencyEventType.FILL_OCCURRED
    )
    client_fill_quantity = sum(
        int(event.data["quantity"])
        for event in session.events
        if event.order_id == order.order_id
        and event.event_type is LatencyEventType.CLIENT_RECEIVED_FILL
    )
    core_fill_quantity = sum(
        fill.quantity
        for fill in session.book.player_position.fills
        if fill.order_id == order.order_id
    )
    passed = all(
        (
            min(managed.values()) >= 0,
            sum(managed.values()) == order.quantity,
            core_values is not None,
            core_values is not None
            and min(core_values.values()) >= 0,
            core_values is not None
            and sum(core_values.values()) == order.quantity,
            core_values is not None
            and managed["filled"] == core_values["filled"],
            core_values is not None
            and managed["cancelled"] + managed["expired"]
            == core_values["cancelled"],
            order.filled_quantity
            == latency_fill_quantity
            == client_fill_quantity
            == core_fill_quantity,
            session.client_position == session.player_position,
        )
    )
    return passed, {
        "client_fill_quantity": client_fill_quantity,
        "client_position_shares": session.client_position,
        "core": core_values,
        "core_fill_quantity": core_fill_quantity,
        "latency_fill_quantity": latency_fill_quantity,
        "managed": managed,
        "original_quantity": order.quantity,
        "player_position_shares": session.player_position,
    }


def _terminal_cancel_fill_ordering(
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> tuple[bool, dict[str, object]]:
    cancel_request = _event(
        session.events,
        LatencyEventType.KEY_PRESSED,
        action="CANCEL",
    )
    cancel_ack = _event(
        session.events,
        LatencyEventType.VENUE_ACKNOWLEDGED_CANCEL,
    )
    fills = tuple(
        event
        for event in session.events
        if event.order_id == order.order_id
        and event.event_type is LatencyEventType.FILL_OCCURRED
    )
    after_request = tuple(
        event for event in fills if _marker(event) > _marker(cancel_request)
    )
    after_terminal_cancel = tuple(
        event for event in fills if _marker(event) > _marker(cancel_ack)
    )
    venue_fill_won = all(
        _marker(event) < _marker(cancel_ack) for event in after_request
    )
    passed = venue_fill_won and not after_terminal_cancel
    return passed, {
        "cancel_request": _event_marker(cancel_request),
        "fill_after_cancel_request_sequences": [
            event.sequence for event in after_request
        ],
        "fill_after_terminal_cancel_sequences": [
            event.sequence for event in after_terminal_cancel
        ],
        "fill_markers": [_event_marker(event) for event in fills],
        "terminal_cancel_ack": _event_marker(cancel_ack),
        "venue_fill_preceded_terminal_cancel": venue_fill_won,
    }


def _cancel_race_reconciliation(
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> tuple[bool, dict[str, object]]:
    cancel_ack = _event(
        session.events,
        LatencyEventType.VENUE_ACKNOWLEDGED_CANCEL,
    )
    fill_events = tuple(
        event
        for event in session.events
        if event.order_id == order.order_id
        and event.event_type is LatencyEventType.FILL_OCCURRED
    )
    fills_before_ack = tuple(
        event for event in fill_events if _marker(event) < _marker(cancel_ack)
    )
    if order.filled_quantity == order.quantity and fills_before_ack:
        expected = "FILL_BEFORE_CANCEL"
    elif order.filled_quantity and order.cancelled_quantity and fills_before_ack:
        expected = "PARTIAL_FILL_THEN_CANCELLED"
    elif not order.filled_quantity and order.cancelled_quantity == order.quantity:
        expected = "CANCEL_WON"
    else:
        expected = f"TOO_LATE_{order.state.value}"
    acknowledged = str(cancel_ack.data["outcome"])
    passed = (
        expected == acknowledged == order.cancel_race_outcome
        and (
            expected != "FILL_BEFORE_CANCEL"
            or order.state is AsyncOrderState.FILLED
        )
        and (
            expected != "CANCEL_WON"
            or order.state is AsyncOrderState.CANCELLED
        )
    )
    return passed, {
        "acknowledged_outcome": acknowledged,
        "calculated_outcome": expected,
        "cancel_ack": _event_marker(cancel_ack),
        "fill_before_cancel_ack_sequences": [
            event.sequence for event in fills_before_ack
        ],
        "final_order_state": order.state.value,
        "recorded_outcome": order.cancel_race_outcome,
    }


def _latency_metric_reconciliation(
    configuration: GeneratedConfiguration,
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> tuple[bool, dict[str, object]]:
    timestamps = order.timestamps
    sent = timestamps.get("order_left_client_us")
    acknowledged = timestamps.get("venue_acknowledged_us")
    filled = timestamps.get("first_fill_occurred_us")
    expected = {
        "cancel_race_outcome": order.cancel_race_outcome,
        "decision_to_send_latency_us": (
            None if sent is None else sent - order.intention_time_us
        ),
        "execution_against_stale_quote": (
            filled is not None
            and (
                order.observed_ask_ticks
                if order.side.value == "buy"
                else order.observed_bid_ticks
            )
            != (
                order.venue_ask_ticks
                if order.side.value == "buy"
                else order.venue_bid_ticks
            )
        ),
        "intention_time_us": order.intention_time_us,
        "latency_induced_slippage_ticks": _slippage(session, order),
        "observed_quote_age_us": (
            order.intention_time_us - order.observed_quote_time_us
        ),
        "order_id": order.order_id,
        "send_to_ack_latency_us": (
            None
            if sent is None or acknowledged is None
            else acknowledged - sent
        ),
        "send_to_fill_latency_us": (
            None if sent is None or filled is None else filled - sent
        ),
        "venue_execution_time_us": filled,
    }
    actual = session.metrics(order.order_id).as_dict()
    profile = get_latency_profile(configuration.latency)
    draws = session.sampler.draws
    draws_valid = all(
        draw.distribution is profile.distribution(draw.component).kind
        and profile.distribution(draw.component).lower_us
        <= draw.sampled_latency_us
        <= profile.distribution(draw.component).upper_us
        for draw in draws
    )
    draw_sequences = [draw.sequence for draw in draws]
    draw_times = [draw.simulation_time_us for draw in draws]
    purpose_draws = {draw.purpose: draw for draw in draws}
    component_timestamps_ok, component_evidence = _component_timestamp_checks(
        purpose_draws,
        timestamps,
        _event(
            session.events,
            LatencyEventType.KEY_PRESSED,
            action="CANCEL",
        ).simulation_time_us,
    )
    passed = all(
        (
            actual == expected,
            session.profile.as_dict() == profile.as_dict(),
            bool(draws),
            draws_valid,
            draw_sequences == list(range(1, len(draws) + 1)),
            draw_times == sorted(draw_times),
            component_timestamps_ok,
        )
    )
    return passed, {
        "actual_metrics": actual,
        "component_timestamp_checks": component_evidence,
        "configured_profile": profile.as_dict(),
        "draw_component_counts": {
            component.value: sum(draw.component is component for draw in draws)
            for component in LatencyComponent
        },
        "draw_trace_sha256": canonical_sha256(
            [draw.as_dict() for draw in draws]
        ),
        "draws_within_configured_profile": draws_valid,
        "expected_metrics": expected,
        "latency_draw_count": len(draws),
        "latency_draws": [draw.as_dict() for draw in draws],
    }


def _component_timestamp_checks(
    draws: Mapping[str, object],
    timestamps: Mapping[str, int],
    cancel_request_time_us: int,
) -> tuple[bool, dict[str, object]]:
    paths = {
        "new_input": (
            "order:RACE-PLAYER-BID:create",
            "player_pressed_key_us",
            "client_created_order_us",
            "exact",
        ),
        "new_route": (
            "order:RACE-PLAYER-BID:route",
            "client_created_order_us",
            "order_left_client_us",
            "exact",
        ),
        "new_uplink": (
            "order:RACE-PLAYER-BID:uplink",
            "order_left_client_us",
            "gateway_received_order_us",
            "exact",
        ),
        "new_gateway": (
            "order:RACE-PLAYER-BID:gateway",
            "gateway_received_order_us",
            "venue_received_us",
            "exact",
        ),
        "new_venue": (
            "order:RACE-PLAYER-BID:venue_processing",
            "venue_received_us",
            "venue_acknowledged_us",
            "exact",
        ),
        "new_ack_downlink": (
            "order:RACE-PLAYER-BID:ack_downlink",
            "venue_acknowledged_us",
            "client_received_ack_us",
            "exact",
        ),
        "cancel_input": (
            "cancel:ASYNC-CANCEL-000001:create",
            "player_cancel_request_us",
            "client_created_cancel_us",
            "exact",
        ),
        "cancel_route": (
            "cancel:ASYNC-CANCEL-000001:route",
            "client_created_cancel_us",
            "cancel_left_client_us",
            "exact",
        ),
        "cancel_uplink": (
            "cancel:ASYNC-CANCEL-000001:uplink",
            "cancel_left_client_us",
            "gateway_received_cancel_us",
            "exact",
        ),
        "cancel_gateway": (
            "cancel:ASYNC-CANCEL-000001:gateway",
            "gateway_received_cancel_us",
            "venue_received_cancel_us",
            "at_least",
        ),
        "cancel_venue": (
            "cancel:ASYNC-CANCEL-000001:venue_processing",
            "venue_received_cancel_us",
            "venue_cancel_acknowledged_us",
            "exact",
        ),
        "cancel_ack_downlink": (
            "cancel:ASYNC-CANCEL-000001:ack_downlink",
            "venue_cancel_acknowledged_us",
            "client_received_cancel_ack_us",
            "exact",
        ),
    }
    augmented = dict(timestamps)
    augmented["player_cancel_request_us"] = cancel_request_time_us
    results: dict[str, object] = {}
    passed = True
    for name, (purpose, start_name, end_name, mode) in paths.items():
        draw = draws.get(purpose)
        start = augmented.get(start_name)
        end = augmented.get(end_name)
        sampled = getattr(draw, "sampled_latency_us", None)
        delta = None if start is None or end is None else end - start
        matched = (
            isinstance(sampled, int)
            and isinstance(delta, int)
            and (delta == sampled if mode == "exact" else delta >= sampled)
        )
        results[name] = {
            "delta_us": delta,
            "mode": mode,
            "purpose": purpose,
            "sampled_us": sampled,
            "status": "PASS" if matched else "FAIL",
        }
        passed = passed and matched
    fill_purpose = next(
        (
            purpose
            for purpose in draws
            if purpose.startswith("fill:") and purpose.endswith(":report")
        ),
        None,
    )
    if "first_fill_occurred_us" in timestamps:
        report_draw = None if fill_purpose is None else draws.get(fill_purpose)
        report_sampled = getattr(report_draw, "sampled_latency_us", None)
        report_delta = (
            timestamps["client_received_fill_us"]
            - timestamps["first_fill_occurred_us"]
        )
        render_purpose = (
            None
            if fill_purpose is None
            else f"{fill_purpose.removesuffix(':report')}:render"
        )
        render_draw = None if render_purpose is None else draws.get(render_purpose)
        render_sampled = getattr(render_draw, "sampled_latency_us", None)
        render_delta = (
            timestamps["ui_displayed_fill_us"]
            - timestamps["client_received_fill_us"]
        )
        fill_matched = (
            report_delta == report_sampled and render_delta == render_sampled
        )
        results["fill_report"] = {
            "client_delta_us": report_delta,
            "client_sampled_us": report_sampled,
            "render_delta_us": render_delta,
            "render_sampled_us": render_sampled,
            "status": "PASS" if fill_matched else "FAIL",
        }
        passed = passed and fill_matched
    return passed, results


def _slippage(
    session: AsynchronousExecutionSession,
    order: AsyncOrder,
) -> str | None:
    fills = tuple(
        fill
        for fill in session.book.player_position.fills
        if fill.order_id == order.order_id
    )
    if not fills:
        return None
    reference = (
        order.observed_ask_ticks
        if order.order_type.value == "market" and order.side.value == "buy"
        else order.observed_bid_ticks
        if order.order_type.value == "market"
        else order.price_ticks
    )
    if reference is None:
        raise RuntimeError("filled asynchronous order lacks a price reference")
    value = sum(fill.price_ticks * fill.quantity for fill in fills)
    quantity = sum(fill.quantity for fill in fills)
    average = Decimal(value) / Decimal(quantity)
    slippage = (
        average - Decimal(reference)
        if order.side.value == "buy"
        else Decimal(reference) - average
    )
    return str(slippage)


def _command_offsets(
    native: LatencyRecording,
    ready_time_us: int,
) -> tuple[int, ...]:
    return tuple(
        command.simulation_time_us - ready_time_us
        for command in native.commands
    )


def _player_order(session: AsynchronousExecutionSession) -> AsyncOrder:
    return next(
        order for order in session.orders if order.order_id == _PLAYER_ORDER_ID
    )


def _event(
    events: tuple[LatencyEvent, ...],
    event_type: LatencyEventType,
    **data: object,
) -> LatencyEvent:
    matches = [
        event
        for event in events
        if event.event_type is event_type
        and (
            event.order_id in {None, _PLAYER_ORDER_ID}
            or event_type is LatencyEventType.EXTERNAL_AGGRESSIVE_ORDER
        )
        and all(event.data.get(key) == value for key, value in data.items())
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {event_type.value} event, observed {len(matches)}"
        )
    return matches[0]


def _marker(event: LatencyEvent) -> tuple[int, int]:
    return event.simulation_time_us, event.sequence


def _ordered_events(events: tuple[LatencyEvent, ...]) -> bool:
    markers = tuple(_marker(event) for event in events)
    return markers == tuple(sorted(markers)) and len(markers) == len(set(markers))


def _event_marker(event: LatencyEvent) -> dict[str, int]:
    return {
        "sequence": event.sequence,
        "simulation_time_us": event.simulation_time_us,
    }


def _event_markers(events: tuple[LatencyEvent, ...]) -> list[dict[str, object]]:
    return [
        {
            "event_type": event.event_type.value,
            **_event_marker(event),
        }
        for event in events
    ]


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
            f"{name} reconciled from native latency evidence"
            if passed
            else f"{name} did not reconcile from native latency evidence"
        ),
        evidence=evidence,
    )


def _observable_projection(
    session: AsynchronousExecutionSession,
) -> dict[str, object]:
    client_event_types = {
        LatencyEventType.CLIENT_RECEIVED_ACK,
        LatencyEventType.CLIENT_RECEIVED_CANCEL_ACK,
        LatencyEventType.CLIENT_RECEIVED_FILL,
        LatencyEventType.CLIENT_RECEIVED_MARKET_DATA,
        LatencyEventType.KEY_PRESSED,
        LatencyEventType.UI_DISPLAYED_FILL,
        LatencyEventType.UI_RENDERED_MARKET_STATE,
    }
    return {
        "client_events": [
            event.as_dict()
            for event in session.events
            if event.event_type in client_event_types
        ],
        "client_position_shares": session.client_position,
        "latest_display": (
            None
            if session.latest_display is None
            else session.latest_display.as_dict()
        ),
    }
