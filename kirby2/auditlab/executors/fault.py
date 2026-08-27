"""Typed wrapper for production-subsystem fault observations."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from kirby2.exchange import SessionState, Side
from kirby2.immutable import thaw_json
from kirby2.latency import (
    AsynchronousExecutionSession,
    LatencyCommand,
    LatencyProfile,
    LatencyProfileName,
    acknowledgement_budget_diagnostic,
    get_latency_profile,
    terminal_race_diagnostic,
)
from kirby2.marketdata import RawDataset, SourceCapability, normalize_raw_dataset
from kirby2.multivenue import (
    MarketCoordinator,
    MultiVenueCommand,
    VenueConfig,
    apply_multivenue_command,
    pending_order_halt_diagnostic,
)

from ..faults import inject_and_observe
from ..models import (
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExerciseRecord,
    ExerciseStatus,
    ExecutorLane,
    FailureKind,
    FailureObservation,
    FaultKind,
    FaultObservation,
    GeneratedCaseResult,
    GeneratedConfiguration,
    UnsupportedSchemaVersionError,
    canonical_sha256,
)
from .base import finalize_recording


FAULT_RECORDING_TYPE = "EXPLICIT_FAULT_OBSERVATION"
_RECORDING_FIELDS = frozenset(
    {"configuration", "fault_observation", "replay_context"}
)
_MARKET_DATA_FAULTS = frozenset(
    {
        FaultKind.DUPLICATE_MESSAGE,
        FaultKind.DROPPED_MARKET_DATA,
        FaultKind.OUT_OF_ORDER_DELIVERY,
        FaultKind.SNAPSHOT_GAP,
        FaultKind.CORRUPTED_DATASET_ROW,
    }
)


class FaultExecutor:
    """Return raw evidence for one explicitly selected production fault."""

    lane = ExecutorLane.FAULT

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        observation = inject_and_observe(configuration)
        if observation is None:
            raise RuntimeError("fault executor produced no fault observation")
        replay_context = _fault_replay_context(observation)
        replay_match, replay_evidence = _replay_fault_tape(
            configuration,
            observation,
            replay_context,
        )
        recording = CaseRecording(
            lane=self.lane,
            recording_type=FAULT_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "fault_observation": observation.as_dict(),
                "replay_context": replay_context,
            },
        )
        return finalize_recording(
            recording,
            lambda finalized: _result(
                configuration,
                finalized,
                observation,
                replay_match=replay_match,
                replay_evidence=replay_evidence,
            ),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("fault replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("fault replay received a different lane")
        if recording.recording_type != FAULT_RECORDING_TYPE:
            raise ValueError("unsupported fault recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict) or set(payload) != _RECORDING_FIELDS:
            raise ValueError("fault recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_observation = payload["fault_observation"]
        raw_context = payload["replay_context"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("fault recording configuration must be an object")
        if not isinstance(raw_observation, dict):
            raise TypeError("fault recording observation must be an object")
        if not isinstance(raw_context, dict):
            raise TypeError("fault recording replay context must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        observation = FaultObservation.from_dict(raw_observation)
        replay_match, replay_evidence = _replay_fault_tape(
            configuration,
            observation,
            raw_context,
        )
        return _result(
            configuration,
            recording,
            observation,
            replay_match=replay_match,
            replay_evidence=replay_evidence,
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("fault executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("fault executor received a different lane")
        if configuration.injected_fault is None:
            raise ValueError("fault executor requires one explicit fault")


def _fault_replay_context(observation: FaultObservation) -> dict[str, object]:
    context: dict[str, object] = {
        "fault_tape_version": 1,
        "starting_configuration_sha256": observation.details.get(
            "source_configuration_sha256"
        ),
    }
    if observation.fault in _MARKET_DATA_FAULTS:
        context.update(
            {
                "expected_snapshot_interval_ns": (
                    1_000_000_000
                    if observation.fault is FaultKind.SNAPSHOT_GAP
                    else None
                ),
                "source_timezone": "UTC",
            }
        )
    elif observation.fault in {
        FaultKind.DELAYED_ACKNOWLEDGEMENT,
        FaultKind.CANCEL_FILL_RACE,
    }:
        context.update(
            {
                "initial_ask_ticks": 101,
                "initial_bid_ticks": 99,
                "initial_queue_quantity": 100,
                "latency_profile": (
                    observation.manifest["latency_profile"]
                    if observation.fault is FaultKind.DELAYED_ACKNOWLEDGEMENT
                    else get_latency_profile(
                        LatencyProfileName.NORMAL
                    ).as_dict()
                ),
            }
        )
    elif observation.fault in {
        FaultKind.VENUE_REJECTION,
        FaultKind.HALT_DURING_PENDING_ORDER,
    }:
        context["venue_config"] = observation.manifest["venue_config"]
    return context


def _replay_fault_tape(
    configuration: GeneratedConfiguration,
    observation: FaultObservation,
    context: Mapping[str, object],
) -> tuple[bool, dict[str, object]]:
    if observation.fault is not configuration.injected_fault:
        raise ValueError("fault tape differs from its starting configuration")
    if context.get("fault_tape_version") != 1:
        raise ValueError("unsupported fault tape version")
    if observation.fault in _MARKET_DATA_FAULTS:
        code, events, issues = _replay_market_data(observation, context)
    elif observation.fault is FaultKind.DELAYED_ACKNOWLEDGEMENT:
        code, events, issues = _replay_delayed_ack(observation, context)
    elif observation.fault in {
        FaultKind.VENUE_REJECTION,
        FaultKind.HALT_DURING_PENDING_ORDER,
    }:
        code, events, issues = _replay_multivenue_fault(observation)
    elif observation.fault is FaultKind.CANCEL_FILL_RACE:
        code, events, issues = _replay_cancel_race(observation, context)
    elif observation.fault is FaultKind.SCHEMA_MISMATCH:
        code, events, issues = _replay_schema_mismatch(observation)
    else:  # pragma: no cover - FaultKind is exhaustively dispatched
        raise RuntimeError("unsupported serialized fault tape")
    expected_events = thaw_json(observation.raw_events)
    expected_issues = thaw_json(observation.raw_issues)
    event_match = events == expected_events
    issue_match = issues == expected_issues
    code_match = code == observation.observed_code
    evidence = {
        "actual_event_sha256": canonical_sha256(events),
        "actual_issue_sha256": canonical_sha256(issues),
        "actual_observed_code": code,
        "detector": observation.detector,
        "event_match": event_match,
        "expected_event_sha256": canonical_sha256(expected_events),
        "expected_issue_sha256": canonical_sha256(expected_issues),
        "expected_observed_code": observation.observed_code,
        "issue_match": issue_match,
        "observed_code_match": code_match,
        "production_subsystem": observation.subsystem,
        "serialized_tape_consumed": True,
    }
    return event_match and issue_match and code_match, evidence


def _replay_market_data(
    observation: FaultObservation,
    context: Mapping[str, object],
) -> tuple[str | None, list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    for expected_source_row, event in enumerate(observation.raw_events, start=1):
        row = event.get("row")
        if (
            event.get("record_type") != "raw_market_data_row"
            or event.get("source_row") != expected_source_row
            or not isinstance(row, Mapping)
        ):
            raise ValueError("serialized market-data fault row is invalid")
        thawed_row = thaw_json(row)
        if not isinstance(thawed_row, dict):
            raise TypeError("serialized market-data row must thaw to an object")
        rows.append(thawed_row)
    raw = RawDataset(
        adapter=str(observation.manifest["adapter"]),
        source_locator="memory://auditlab/serialized-fault-replay",
        source_digest="0" * 64,
        source_name="Kirby2 serialized production fault replay",
        license_note="Synthetic runtime audit evidence",
        real_market_data=False,
        capability=SourceCapability(str(observation.manifest["capability"])),
        tick_size=Decimal(str(observation.manifest["tick_size"])),
        source_timezone=str(context["source_timezone"]),
        expected_snapshot_interval_ns=(
            None
            if context["expected_snapshot_interval_ns"] is None
            else int(context["expected_snapshot_interval_ns"])
        ),
        rows=tuple(rows),
    )
    dataset = normalize_raw_dataset(raw)
    issues = [
        {"issue_kind": "rejection", **issue.as_dict()}
        for issue in dataset.report.rejections
    ]
    issues.extend(
        {"issue_kind": "warning", **issue.as_dict()}
        for issue in dataset.report.warnings
    )
    issues.extend(
        {"code": gap.gap_type, "issue_kind": "data_gap", **gap.as_dict()}
        for gap in dataset.report.gaps
    )
    events = [
        {
            "record_type": "raw_market_data_row",
            "row": row,
            "source_row": index,
        }
        for index, row in enumerate(rows, start=1)
    ]
    return (None if not issues else str(issues[0]["code"]), events, issues)


def _latency_session(
    seed: int,
    context: Mapping[str, object],
) -> AsynchronousExecutionSession:
    raw_profile = context["latency_profile"]
    if not isinstance(raw_profile, Mapping):
        raise TypeError("fault latency profile must be an object")
    return AsynchronousExecutionSession(
        seed=seed,
        profile=LatencyProfile.from_dict(thaw_json(raw_profile)),
        initial_bid_ticks=int(context["initial_bid_ticks"]),
        initial_ask_ticks=int(context["initial_ask_ticks"]),
        initial_queue_quantity=int(context["initial_queue_quantity"]),
    )


def _replay_delayed_ack(
    observation: FaultObservation,
    context: Mapping[str, object],
) -> tuple[str | None, list[dict[str, object]], list[dict[str, object]]]:
    session = _latency_session(int(observation.manifest["seed"]), context)
    commands = observation.manifest["commands"]
    if not isinstance(commands, tuple) or len(commands) != 1:
        raise ValueError("delayed-ack tape requires one command")
    command = commands[0]
    if not isinstance(command, Mapping) or command.get("command_type") != "LIMIT":
        raise ValueError("delayed-ack command is invalid")
    session.advance_to(int(command["simulation_time_us"]))
    session.request_limit(
        Side(str(command["side"])),
        int(command["quantity"]),
        int(command["price_ticks"]),
        order_id=str(command["order_id"]),
    )
    _drain_latency_session(session)
    session.assert_invariants()
    metrics = session.metrics(str(command["order_id"]))
    diagnostic = acknowledgement_budget_diagnostic(
        metrics,
        budget_us=int(observation.manifest["declared_acknowledgement_budget_us"]),
    )
    issues = [] if diagnostic is None else [diagnostic.as_dict()]
    return (
        None if diagnostic is None else diagnostic.code,
        [event.as_dict() for event in session.events],
        issues,
    )


def _replay_cancel_race(
    observation: FaultObservation,
    context: Mapping[str, object],
) -> tuple[str | None, list[dict[str, object]], list[dict[str, object]]]:
    session = _latency_session(int(observation.manifest["seed"]), context)
    raw_commands = observation.manifest["commands"]
    if not isinstance(raw_commands, tuple):
        raise TypeError("cancel-race commands must be an array")
    for raw_command in raw_commands:
        if not isinstance(raw_command, Mapping):
            raise TypeError("cancel-race command must be an object")
        thawed_command = thaw_json(raw_command)
        if not isinstance(thawed_command, dict):
            raise TypeError("cancel-race command must thaw to an object")
        command = LatencyCommand.from_dict(thawed_command)
        session.advance_to(command.simulation_time_us)
        _apply_latency_command(session, command)
    _drain_latency_session(session)
    session.assert_invariants()
    order = next(
        item for item in session.orders if item.order_id == "RACE-PLAYER-BID"
    )
    diagnostic = terminal_race_diagnostic(order)
    issues = [] if diagnostic is None else [diagnostic.as_dict()]
    return (
        None if diagnostic is None else diagnostic.code,
        [event.as_dict() for event in session.events],
        issues,
    )


def _apply_latency_command(
    session: AsynchronousExecutionSession,
    command: LatencyCommand,
) -> None:
    values = command.parameters
    if command.command_type == "LIMIT":
        session.request_limit(
            Side(str(values["side"])),
            int(values["quantity"]),
            int(values["price_ticks"]),
            order_id=str(values["order_id"]),
        )
    elif command.command_type == "CANCEL":
        actual = session.request_cancel(str(values["target_order_id"]))
        if actual != str(values["cancel_id"]):
            raise RuntimeError("serialized cancel identity diverged")
    elif command.command_type == "EXTERNAL_MARKET":
        session.schedule_aggressive_order(
            command.simulation_time_us,
            Side(str(values["side"])),
            int(values["quantity"]),
            order_id=str(values["order_id"]),
        )
    else:
        raise ValueError("fault tape contains an unsupported latency command")


def _replay_multivenue_fault(
    observation: FaultObservation,
) -> tuple[str | None, list[dict[str, object]], list[dict[str, object]]]:
    raw_config = observation.manifest["venue_config"]
    raw_commands = observation.manifest["commands"]
    if not isinstance(raw_config, Mapping):
        raise TypeError("fault venue configuration must be an object")
    if not isinstance(raw_commands, tuple):
        raise TypeError("fault venue commands must be an array")
    thawed_config = thaw_json(raw_config)
    if not isinstance(thawed_config, dict):
        raise TypeError("fault venue configuration must thaw to an object")
    venue_config = VenueConfig.from_dict(thawed_config)
    coordinator = MarketCoordinator(
        (venue_config,),
        seed=int(observation.manifest["seed"]),
    )
    route_ids: list[str] = []
    for sequence, raw_command in enumerate(raw_commands, start=1):
        if not isinstance(raw_command, Mapping):
            raise TypeError("fault venue command must be an object")
        command_type = str(raw_command["command_type"])
        time_us = int(raw_command.get("simulation_time_us", 0))
        parameters = {
            str(key): thaw_json(value)
            for key, value in raw_command.items()
            if key not in {"command_type", "simulation_time_us"}
        }
        command = MultiVenueCommand(
            sequence,
            time_us,
            command_type,
            parameters,
        )
        coordinator.advance_to(time_us)
        route_id = apply_multivenue_command(coordinator, command)
        if route_id is not None:
            route_ids.append(route_id)
    coordinator.advance_to(coordinator.clock.current_time_us)
    coordinator.assert_invariants()
    if len(route_ids) != 1:
        raise RuntimeError("fault venue replay requires one routed order")
    route_id = route_ids[0]
    execution = coordinator.route_result(route_id).executions[0]
    if observation.fault is FaultKind.VENUE_REJECTION:
        code = execution.rejection_reason
        issues = (
            []
            if code is None
            else [
                {
                    "code": code,
                    "execution": execution.as_dict(),
                    "issue_kind": "venue_rejection",
                }
            ]
        )
    else:
        diagnostic = pending_order_halt_diagnostic(
            coordinator.events,
            route_id=route_id,
            execution=execution,
        )
        code = None if diagnostic is None else diagnostic.code
        issues = [] if diagnostic is None else [diagnostic.as_dict()]
    return code, [event.as_dict() for event in coordinator.events], issues


def _replay_schema_mismatch(
    observation: FaultObservation,
) -> tuple[str | None, list[dict[str, object]], list[dict[str, object]]]:
    if len(observation.raw_events) != 1:
        raise ValueError("schema fault tape requires one loader command")
    event = observation.raw_events[0]
    raw_configuration = event.get("serialized_configuration")
    if not isinstance(raw_configuration, Mapping):
        raise TypeError("schema fault command must contain a configuration")
    issue: dict[str, object] | None = None
    try:
        GeneratedConfiguration.from_dict(raw_configuration)
    except UnsupportedSchemaVersionError as error:
        issue = error.as_dict()
    return (
        None if issue is None else str(issue["code"]),
        [thaw_json(event)],
        [] if issue is None else [issue],
    )


def _drain_latency_session(session: AsynchronousExecutionSession) -> None:
    while session.pending_event_horizon_us is not None:
        horizon = session.pending_event_horizon_us
        if horizon is None:  # pragma: no cover - loop condition narrows this
            raise RuntimeError("latency replay horizon disappeared")
        session.advance_to(horizon)


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    observation: FaultObservation,
    *,
    replay_match: bool,
    replay_evidence: dict[str, object],
) -> GeneratedCaseResult:
    configuration_round_trip = (
        GeneratedConfiguration.from_dict(configuration.as_dict())
        == configuration
    )
    raw_evidence_sha256 = canonical_sha256(observation.as_dict())
    checks = (
        _check(
            "fault_injected",
            observation.fault is configuration.injected_fault,
            {
                "configuration_sha256": configuration.sha256,
                "fault": observation.fault.value,
                "injection_event": observation.injection_event,
                "injection_location": observation.injection_location,
            },
        ),
        _check(
            "production_detector_exercised",
            bool(observation.raw_events or observation.raw_issues),
            {
                "detector": observation.detector,
                "observed_code": observation.observed_code,
                "raw_evidence_sha256": raw_evidence_sha256,
                "raw_event_count": len(observation.raw_events),
                "raw_issue_count": len(observation.raw_issues),
                "subsystem": observation.subsystem,
            },
        ),
        _check(
            "unrelated_invariants_survive",
            configuration_round_trip and replay_match,
            {
                "configuration_round_trip_match": configuration_round_trip,
                "configuration_sha256": configuration.sha256,
                "raw_evidence_sha256": raw_evidence_sha256,
                "serialized_tape_replay": replay_evidence,
                "serialized_tape_replay_match": replay_match,
            },
        ),
    )
    failures = [
        FailureObservation(
            kind=FailureKind.INVARIANT_VIOLATION,
            code=f"FAULT_{check.name.upper()}",
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
    if not replay_match:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="FAULT_REPLAY_MISMATCH",
                message="serialized fault tape did not reproduce through production",
                evidence={
                    "recording_sha256": recording.sha256,
                    "replay_evidence": replay_evidence,
                },
            )
        )
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.FAULT,
        recording=recording,
        event_projection=(
            {
                "data": observation.as_dict(),
                "record_type": "fault_observation",
                "sequence": 1,
            },
        ),
        final_state_projection={
            "configuration_sha256": configuration.sha256,
            "fault_observation": observation.as_dict(),
            "raw_evidence_sha256": raw_evidence_sha256,
        },
        metrics={
            "injected_fault_count": 1,
            "observed_code_count": int(observation.observed_code is not None),
            "raw_observation_count": 1,
        },
        exercises=(
            ExerciseRecord(
                ExecutorLane.FAULT,
                "injected_fault",
                observation.fault.value,
                ExerciseStatus.EXERCISED,
                {
                    "detector": observation.detector,
                    "raw_evidence_sha256": raw_evidence_sha256,
                    "recording_sha256": recording.sha256,
                    "subsystem": observation.subsystem,
                },
            ),
        ),
        checks=checks,
        failures=tuple(failures),
        observable_projection={
            "fault": observation.fault.value,
            "observed_code": observation.observed_code,
            "representation": "PRODUCTION_FAULT_OBSERVATION",
            "subsystem": observation.subsystem,
        },
        fault_observation=observation,
    )


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
            f"production fault-observation check passed: {name}"
            if passed
            else f"production fault-observation check failed: {name}"
        ),
        evidence={"source": "FaultExecutor", **evidence},
    )
