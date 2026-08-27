"""Deterministic fault injections routed through production subsystems."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from kirby2.exchange import OrderInstruction, OrderOwner, SessionState, Side
from kirby2.latency import (
    AsynchronousExecutionSession,
    CancelRace,
    LatencyProfileName,
    acknowledgement_budget_diagnostic,
    get_latency_profile,
    run_cancel_race,
    terminal_race_diagnostic,
)
from kirby2.marketdata import RawDataset, SourceCapability, normalize_raw_dataset
from kirby2.multivenue import (
    CoordinatorEventType,
    MarketCoordinator,
    RoutePolicy,
    RoutingRequest,
    VenueConfig,
    pending_order_halt_diagnostic,
)
from kirby2.observability import HiddenOrderRequest, LiquidityKind

from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    FaultKind,
    FaultObservation,
    GeneratedConfiguration,
    UnsupportedSchemaVersionError,
)


_FaultAdapter = Callable[[GeneratedConfiguration], FaultObservation]


def inject_and_observe(
    configuration: GeneratedConfiguration,
) -> FaultObservation | None:
    """Inject one declared fault and return only production-observed evidence."""

    if not isinstance(configuration, GeneratedConfiguration):
        raise TypeError("fault injection requires GeneratedConfiguration")
    fault = configuration.injected_fault
    if fault is None:
        return None
    adapters = _adapters()
    if set(adapters) != set(FaultKind):
        raise RuntimeError("production fault adapters do not cover every fault kind")
    return adapters[fault](configuration)


def _adapters() -> dict[FaultKind, _FaultAdapter]:
    return {
        FaultKind.DUPLICATE_MESSAGE: _duplicate_message,
        FaultKind.DROPPED_MARKET_DATA: _dropped_market_data,
        FaultKind.DELAYED_ACKNOWLEDGEMENT: _delayed_acknowledgement,
        FaultKind.OUT_OF_ORDER_DELIVERY: _out_of_order_delivery,
        FaultKind.SNAPSHOT_GAP: _snapshot_gap,
        FaultKind.CORRUPTED_DATASET_ROW: _corrupted_dataset_row,
        FaultKind.VENUE_REJECTION: _venue_rejection,
        FaultKind.HALT_DURING_PENDING_ORDER: _halt_during_pending_order,
        FaultKind.CANCEL_FILL_RACE: _cancel_fill_race,
        FaultKind.SCHEMA_MISMATCH: _schema_mismatch,
    }


def _duplicate_message(configuration: GeneratedConfiguration) -> FaultObservation:
    row = _trade_row(timestamp_offset_us=0, source_sequence=None)
    rows = (row, dict(row))
    return _market_data_observation(
        configuration,
        rows,
        injection_event=2,
        injection_location="source_row:2",
        mutation="repeat_normalized_source_row",
    )


def _dropped_market_data(
    configuration: GeneratedConfiguration,
) -> FaultObservation:
    rows = tuple(
        _trade_row(timestamp_offset_us=index, source_sequence=sequence)
        for index, sequence in enumerate((1, 2, 4))
    )
    return _market_data_observation(
        configuration,
        rows,
        injection_event=3,
        injection_location="source_row:3.source_sequence",
        mutation="skip_market_by_order_sequence",
    )


def _out_of_order_delivery(
    configuration: GeneratedConfiguration,
) -> FaultObservation:
    rows = tuple(
        _trade_row(timestamp_offset_us=timestamp, source_sequence=sequence)
        for timestamp, sequence in ((0, 1), (2, 3), (1, 2))
    )
    return _market_data_observation(
        configuration,
        rows,
        injection_event=3,
        injection_location="source_row:3.source_sequence",
        mutation="reverse_delivery_sequence",
    )


def _snapshot_gap(configuration: GeneratedConfiguration) -> FaultObservation:
    rows = (
        _snapshot_row(timestamp_offset_us=0, source_sequence=1),
        _snapshot_row(timestamp_offset_us=3_000_000, source_sequence=2),
    )
    return _market_data_observation(
        configuration,
        rows,
        injection_event=2,
        injection_location="source_row:2.source_timestamp",
        mutation="skip_expected_snapshot_intervals",
        expected_snapshot_interval_ns=1_000_000_000,
    )


def _corrupted_dataset_row(
    configuration: GeneratedConfiguration,
) -> FaultObservation:
    rows = (
        _trade_row(
            timestamp_offset_us=0,
            source_sequence=1,
            quantity=-1,
        ),
    )
    return _market_data_observation(
        configuration,
        rows,
        injection_event=1,
        injection_location="source_row:1.quantity",
        mutation="negative_normalized_quantity",
    )


def _market_data_observation(
    configuration: GeneratedConfiguration,
    rows: tuple[dict[str, object], ...],
    *,
    injection_event: int,
    injection_location: str,
    mutation: str,
    expected_snapshot_interval_ns: int | None = None,
) -> FaultObservation:
    raw = RawDataset(
        adapter="auditlab-production-fault-v1",
        source_locator=f"memory://auditlab/{configuration.sha256}",
        source_digest="0" * 64,
        source_name="Kirby2 production fault injection",
        license_note="Synthetic runtime audit evidence",
        real_market_data=False,
        capability=SourceCapability.MARKET_BY_ORDER,
        tick_size=Decimal("0.01"),
        source_timezone="UTC",
        expected_snapshot_interval_ns=expected_snapshot_interval_ns,
        rows=rows,
    )
    dataset = normalize_raw_dataset(raw)
    rejection_issues = tuple(
        {"issue_kind": "rejection", **issue.as_dict()}
        for issue in dataset.report.rejections
    )
    warning_issues = tuple(
        {"issue_kind": "warning", **issue.as_dict()}
        for issue in dataset.report.warnings
    )
    gap_issues = tuple(
        {
            "code": gap.gap_type,
            "issue_kind": "data_gap",
            **gap.as_dict(),
        }
        for gap in dataset.report.gaps
    )
    raw_issues = rejection_issues + warning_issues + gap_issues
    observed_code = None if not raw_issues else str(raw_issues[0]["code"])
    raw_events = tuple(
        {
            "record_type": "raw_market_data_row",
            "row": dict(row),
            "source_row": index,
        }
        for index, row in enumerate(rows, start=1)
    )
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="marketdata.normalization",
        detector="normalize_raw_dataset",
        injection_location=injection_location,
        observed_code=observed_code,
        injection_event=injection_event,
        manifest={
            "adapter": raw.adapter,
            "capability": raw.capability.value,
            "fault_manifest_version": 1,
            "input_row_count": len(rows),
            "mutation": mutation,
            "tick_size": str(raw.tick_size),
        },
        raw_events=raw_events,
        raw_issues=raw_issues,
        details={
            "accepted_records": [record.as_dict() for record in dataset.records],
            "quality_report": dataset.report.as_dict(),
            "replay_decision": dataset.replay.as_dict(),
        },
    )


def _delayed_acknowledgement(
    configuration: GeneratedConfiguration,
) -> FaultObservation:
    profile = get_latency_profile(LatencyProfileName.NORMAL)
    session = AsynchronousExecutionSession(seed=configuration.seed, profile=profile)
    _advance_latency_to_observable(session)
    order_id = "FAULT-DELAYED-ACK"
    session.request_limit(Side.BUY, 10, 99, order_id=order_id)
    _drain_latency_session(session)
    session.assert_invariants()
    metrics = session.metrics(order_id)
    observed_us = metrics.send_to_ack_latency_us
    if observed_us is None or observed_us <= 0:
        raise RuntimeError("production latency session produced no positive acknowledgement")
    budget_us = observed_us - 1
    diagnostic = acknowledgement_budget_diagnostic(metrics, budget_us=budget_us)
    issue = None if diagnostic is None else diagnostic.as_dict()
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="latency.AsynchronousExecutionSession",
        detector=(
            "acknowledgement_budget_diagnostic"
            if diagnostic is None
            else diagnostic.gate
        ),
        injection_location="declared_acknowledgement_budget_us",
        observed_code=None if diagnostic is None else diagnostic.code,
        injection_event=1,
        manifest={
            "commands": [
                {
                    "command_type": "LIMIT",
                    "order_id": order_id,
                    "price_ticks": 99,
                    "quantity": 10,
                    "side": Side.BUY.value,
                    "simulation_time_us": metrics.intention_time_us,
                }
            ],
            "declared_acknowledgement_budget_us": budget_us,
            "fault_manifest_version": 1,
            "latency_profile": profile.as_dict(),
            "seed": configuration.seed,
        },
        raw_events=tuple(event.as_dict() for event in session.events),
        raw_issues=() if issue is None else (issue,),
        details={
            "metrics": metrics.as_dict(),
            "order": next(
                order.as_dict() for order in session.orders if order.order_id == order_id
            ),
            "state_sha256": session.state_sha256(),
        },
    )


def _venue_rejection(configuration: GeneratedConfiguration) -> FaultObservation:
    venue = VenueConfig(
        "FAULT-VENUE",
        get_latency_profile(LatencyProfileName.ZERO_LATENCY),
        supported_instructions=frozenset({OrderInstruction.LIMIT}),
    )
    coordinator = MarketCoordinator((venue,), seed=configuration.seed)
    commands = _seed_venue_book(coordinator, venue.venue_id)
    request = RoutingRequest(
        "FAULT-UNSUPPORTED",
        Side.BUY,
        10,
        RoutePolicy.DIRECT,
        direct_venue_id=venue.venue_id,
    )
    route_id = coordinator.submit_route(request)
    coordinator.advance_to(0)
    coordinator.assert_invariants()
    execution = coordinator.route_result(route_id).executions[0]
    issue = (
        None
        if execution.rejection_reason is None
        else {
            "code": execution.rejection_reason,
            "execution": execution.as_dict(),
            "issue_kind": "venue_rejection",
        }
    )
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="multivenue.Venue",
        detector="Venue.execute_player_market",
        injection_location=f"route:{route_id}:leg:1",
        observed_code=execution.rejection_reason,
        injection_event=len(commands) + 1,
        manifest={
            "commands": [
                *commands,
                {"command_type": "ROUTE", "request": request.as_dict()},
            ],
            "fault_manifest_version": 1,
            "seed": configuration.seed,
            "venue_config": venue.as_dict(),
        },
        raw_events=tuple(event.as_dict() for event in coordinator.events),
        raw_issues=() if issue is None else (issue,),
        details={
            "execution": execution.as_dict(),
            "route_id": route_id,
            "state_sha256": coordinator.state_sha256(),
        },
    )


def _halt_during_pending_order(
    configuration: GeneratedConfiguration,
) -> FaultObservation:
    venue = VenueConfig(
        "FAULT-HALT",
        get_latency_profile(LatencyProfileName.NORMAL),
    )
    coordinator = MarketCoordinator((venue,), seed=configuration.seed)
    commands = _seed_venue_book(coordinator, venue.venue_id)
    request = RoutingRequest(
        "FAULT-PENDING-HALT",
        Side.BUY,
        10,
        RoutePolicy.DIRECT,
        direct_venue_id=venue.venue_id,
    )
    route_id = coordinator.submit_route(request)
    scheduled = next(
        event
        for event in coordinator.events
        if event.event_type is CoordinatorEventType.ROUTE_LEG_SCHEDULED
        and event.data.get("route_id") == route_id
    )
    arrival_time_us = int(scheduled.data["arrival_time_us"])
    if arrival_time_us <= 1:
        raise RuntimeError("production route did not remain pending long enough to halt")
    halt_time_us = max(1, arrival_time_us // 2)
    coordinator.advance_to(halt_time_us)
    coordinator.set_venue_session_state(venue.venue_id, SessionState.HALTED)
    halt_event = coordinator.events[-1]
    coordinator.advance_to(arrival_time_us)
    coordinator.assert_invariants()
    execution = coordinator.route_result(route_id).executions[0]
    diagnostic = pending_order_halt_diagnostic(
        coordinator.events,
        route_id=route_id,
        execution=execution,
    )
    issue = None if diagnostic is None else diagnostic.as_dict()
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="multivenue.MarketCoordinator",
        detector=(
            "pending_order_halt_diagnostic"
            if diagnostic is None
            else diagnostic.gate
        ),
        injection_location=f"coordinator_event:{halt_event.sequence}",
        observed_code=None if diagnostic is None else diagnostic.code,
        injection_event=halt_event.sequence,
        manifest={
            "commands": [
                *commands,
                {"command_type": "ROUTE", "request": request.as_dict()},
                {
                    "command_type": "SESSION",
                    "simulation_time_us": halt_time_us,
                    "state": SessionState.HALTED.value,
                    "venue_id": venue.venue_id,
                },
                {
                    "command_type": "ADVANCE",
                    "simulation_time_us": arrival_time_us,
                },
            ],
            "fault_manifest_version": 1,
            "seed": configuration.seed,
            "venue_config": venue.as_dict(),
        },
        raw_events=tuple(event.as_dict() for event in coordinator.events),
        raw_issues=() if issue is None else (issue,),
        details={
            "execution": execution.as_dict(),
            "route_id": route_id,
            "scheduled_event": scheduled.as_dict(),
            "state_sha256": coordinator.state_sha256(),
        },
    )


def _cancel_fill_race(configuration: GeneratedConfiguration) -> FaultObservation:
    race = (
        CancelRace.CANCEL_WINS
        if configuration.seed % 2
        else CancelRace.FILL_WINS
    )
    result = run_cancel_race(
        race,
        seed=configuration.seed,
        profile=LatencyProfileName.NORMAL,
    )
    diagnostic = terminal_race_diagnostic(result.order)
    issue = None if diagnostic is None else diagnostic.as_dict()
    cancel_command = next(
        command
        for command in result.recording.commands
        if command.command_type == "CANCEL"
    )
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="latency.AsynchronousExecutionSession",
        detector=(
            "terminal_race_diagnostic"
            if diagnostic is None
            else diagnostic.gate
        ),
        injection_location=f"latency_command:{cancel_command.sequence}",
        observed_code=None if diagnostic is None else diagnostic.code,
        injection_event=cancel_command.sequence,
        manifest={
            "commands": [
                command.as_dict() for command in result.recording.commands
            ],
            "fault_manifest_version": 1,
            "race": race.value,
            "seed": configuration.seed,
        },
        raw_events=tuple(event.as_dict() for event in result.session.events),
        raw_issues=() if issue is None else (issue,),
        details={
            "metrics": result.metrics.as_dict(),
            "order": result.order.as_dict(),
            "recording_sha256": result.recording.sha256(),
            "replay_passed": result.replay.passed,
        },
    )


def _schema_mismatch(configuration: GeneratedConfiguration) -> FaultObservation:
    payload = configuration.as_dict()
    payload["schema_version"] = AUDIT_LAB_SCHEMA_VERSION + 1
    issue: dict[str, object] | None = None
    try:
        GeneratedConfiguration.from_dict(payload)
    except UnsupportedSchemaVersionError as error:
        issue = error.as_dict()
    return FaultObservation(
        fault=_required_fault(configuration),
        subsystem="auditlab.GeneratedConfiguration",
        detector="GeneratedConfiguration.from_dict",
        injection_location="configuration.schema_version",
        observed_code=None if issue is None else str(issue["code"]),
        injection_event=1,
        manifest={
            "fault_manifest_version": 1,
            "loader": "GeneratedConfiguration.from_dict",
            "mutation": {
                "field": "schema_version",
                "from": AUDIT_LAB_SCHEMA_VERSION,
                "to": payload["schema_version"],
            },
        },
        raw_events=(
            {
                "record_type": "serialized_configuration_load",
                "serialized_configuration": payload,
            },
        ),
        raw_issues=() if issue is None else (issue,),
        details={
            "loader_refused": issue is not None,
            "source_configuration_sha256": configuration.sha256,
        },
    )


def _trade_row(
    *,
    timestamp_offset_us: int,
    source_sequence: int | None,
    quantity: int = 10,
) -> dict[str, object]:
    row: dict[str, object] = {
        "aggressor_side": "buy",
        "price_ticks": 100,
        "quantity": quantity,
        "record_type": "TRADE",
        "session_id": "auditlab-fault-session",
        "source_timestamp": _timestamp(timestamp_offset_us),
        "symbol": "K2FAULT",
        "timestamp_precision": "MICROSECOND",
    }
    if source_sequence is not None:
        row["source_sequence"] = source_sequence
    return row


def _snapshot_row(
    *,
    timestamp_offset_us: int,
    source_sequence: int,
) -> dict[str, object]:
    return {
        "ask_levels": [{"price_ticks": 101, "quantity": 100}],
        "bid_levels": [{"price_ticks": 99, "quantity": 100}],
        "record_type": "BOOK_SNAPSHOT",
        "session_id": "auditlab-fault-session",
        "snapshot_sequence": source_sequence,
        "source_sequence": source_sequence,
        "source_timestamp": _timestamp(timestamp_offset_us),
        "symbol": "K2FAULT",
        "timestamp_precision": "MICROSECOND",
    }


def _timestamp(offset_us: int) -> str:
    seconds, micros = divmod(offset_us, 1_000_000)
    return f"2024-01-02T14:30:{seconds:02d}.{micros:06d}Z"


def _advance_latency_to_observable(
    session: AsynchronousExecutionSession,
) -> None:
    while session.latest_display is None:
        horizon = session.pending_event_horizon_us
        if horizon is None:
            raise RuntimeError("production latency session has no observable horizon")
        session.advance_to(horizon)


def _drain_latency_session(session: AsynchronousExecutionSession) -> None:
    while session.pending_event_horizon_us is not None:
        horizon = session.pending_event_horizon_us
        if horizon is None:
            raise RuntimeError("production latency session lost its pending horizon")
        session.advance_to(horizon)


def _seed_venue_book(
    coordinator: MarketCoordinator,
    venue_id: str,
) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []
    for order_id, side, price_ticks in (
        ("FAULT-SEED-BID", Side.BUY, 99),
        ("FAULT-SEED-ASK", Side.SELL, 101),
    ):
        request = HiddenOrderRequest(
            order_id,
            side,
            LiquidityKind.DISPLAYED_LIMIT,
            OrderOwner.SIMULATED,
            "SIMULATED",
            100,
            price_ticks,
        )
        coordinator.add_resting_order(venue_id, request)
        commands.append(
            {
                "command_type": "ADD",
                "request": request.as_dict(),
                "venue_id": venue_id,
            }
        )
    return commands


def _required_fault(configuration: GeneratedConfiguration) -> FaultKind:
    fault = configuration.injected_fault
    if fault is None:
        raise ValueError("fault adapter requires an injected fault")
    return fault
