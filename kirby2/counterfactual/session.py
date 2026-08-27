"""Deterministic session-run counterfactual branching."""

from __future__ import annotations

from kirby2.exchange import OrderOwner, OrderType
from kirby2.multivenue.models import canonical_sha256
from kirby2.research import RunStore
from kirby2.scenarios import ScenarioDefinition
from kirby2.session.bindings import SessionCommand
from kirby2.session.live import CommandOutcome, LiveMarketSession
from kirby2.session.replay import SessionRecording, replay_recording
from kirby2.simulation.flow import FlowEvent
from kirby2.strategy import parse_strategy

from .models import (
    ActionMutation,
    BranchSnapshot,
    ComponentStatus,
    CounterfactualMode,
    CounterfactualOutcome,
    CounterfactualReport,
    CounterfactualTimelineEntry,
    FirstDivergence,
    MutationManifest,
    PlannedAction,
    SnapshotComponent,
    TimingSweepCell,
    TimingSweepReport,
)


_BUY_COMMANDS = {
    SessionCommand.BUY_BID,
    SessionCommand.BUY_ASK,
    SessionCommand.MARKET_BUY,
}
_SELL_COMMANDS = {
    SessionCommand.SELL_ASK,
    SessionCommand.SELL_BID,
    SessionCommand.MARKET_SELL,
}


def run_counterfactual(
    parent_run_id: str,
    mutation_manifest: MutationManifest,
    mode: CounterfactualMode,
    *,
    parent_store_root,
) -> CounterfactualReport:
    """Load, verify, fork, mutate, and compare one immutable session run."""

    store = RunStore(parent_store_root)
    verification = store.verify_run(parent_run_id)
    if not verification.passed:
        raise ValueError("parent run failed immutable verification: " + "; ".join(verification.failures))
    recording = store.load_recording(parent_run_id)
    replay = replay_recording(recording)
    if not replay.passed:
        raise RuntimeError("verified parent recording failed exact replay")
    fork_time_us = _fork_time(recording, mutation_manifest)
    original_actions = _plan_actions(recording, (), fork_time_us)
    branch_actions = _plan_actions(
        recording,
        mutation_manifest.mutations,
        fork_time_us,
    )
    original_prefix = _reconstruct_prefix(recording, fork_time_us)
    branch_prefix = _reconstruct_prefix(recording, fork_time_us)
    original_snapshot = _snapshot(parent_run_id, original_prefix)
    branch_snapshot = _snapshot(parent_run_id, branch_prefix)
    snapshots_match = original_snapshot.sha256() == branch_snapshot.sha256()
    if not snapshots_match:
        raise RuntimeError("independent branch reconstructions disagree at the fork")

    fixed_path = tuple(
        event
        for event in replay.session.engine.flow_events
        if event.simulation_time_us >= fork_time_us
        and event.sequence > len(branch_prefix.engine.flow_events)
    )
    reference_sha256 = (
        canonical_sha256([event.as_dict() for event in fixed_path])
        if mode is CounterfactualMode.EXOGENOUS_REPLAY
        else None
    )
    original_session, original_timeline = _continue(
        original_prefix,
        original_actions,
        recording,
        mode,
        fixed_path,
        original_snapshot.sha256(),
    )
    branch_session, branch_timeline = _continue(
        branch_prefix,
        branch_actions,
        recording,
        mode,
        fixed_path,
        branch_snapshot.sha256(),
    )
    if original_session.state_sha256() != replay.session.state_sha256():
        raise RuntimeError(
            f"{mode.value} unmodified control did not reproduce the parent state"
        )
    original_outcome = _outcome(original_session, original_timeline)
    branch_outcome = _outcome(branch_session, branch_timeline)
    divergence = _first_divergence(original_timeline, branch_timeline)
    comparison = {
        key: {
            "branch": branch_outcome.metrics[key],
            "changed": branch_outcome.metrics[key] != original_outcome.metrics[key],
            "original": original_outcome.metrics[key],
        }
        for key in (
            "adverse_selection",
            "completion",
            "deadline",
            "fees",
            "fill",
            "pnl",
            "position",
            "risk",
            "slippage",
            "traffic_light_state",
        )
    }
    hindsight_guard = {
        "analysis_may_use_later_information": True,
        "decision_policy": "STATIC_USER_SUPPLIED_MUTATION",
        "privileged_snapshot_accessible_to_policy": False,
        "decision_records": [
            {
                "action_id": action.action_id,
                "information_cutoff_us": action.information_cutoff_us,
                "uses_future_observations": False,
            }
            for action in branch_actions
            if action.origin != "PARENT"
        ],
        "policy_information": "ONLY_STATE_AVAILABLE_AT_EACH_ACTION_TIME",
        "status": "PASS",
    }
    return CounterfactualReport(
        parent_run_id,
        mode,
        mutation_manifest,
        branch_snapshot,
        snapshots_match,
        original_outcome,
        branch_outcome,
        divergence,
        comparison,
        reference_sha256,
        hindsight_guard,
    )


def run_timing_sweep(
    parent_run_id: str,
    action_sequence: int,
    mode: CounterfactualMode,
    *,
    parent_store_root,
) -> TimingSweepReport:
    from pathlib import Path

    from .store import CounterfactualStore

    recording = RunStore(parent_store_root).load_recording(parent_run_id)
    target = _recording_action(recording, action_sequence)
    command = _recorded_command(target.resolved_command)
    if command is None:
        raise ValueError("timing sweep target must resolve to a command")
    cells: list[TimingSweepCell] = []
    branch_store = CounterfactualStore(
        Path(parent_store_root) / "counterfactual_runs"
    )
    for offset in (-500_000, -250_000, 0, 250_000, 500_000):
        mutation = ActionMutation(
            action_sequence,
            expected_command=command,
            command=command,
            timing_delta_us=offset,
        )
        report = run_counterfactual(
            parent_run_id,
            MutationManifest((mutation,)),
            mode,
            parent_store_root=parent_store_root,
        )
        branch_manifest = branch_store.record(report)
        cells.append(
            TimingSweepCell(
                offset,
                branch_manifest.run_id,
                report.result_sha256(),
                report.branch.metrics,
                report.first_divergence.index,
            )
        )
    return TimingSweepReport(parent_run_id, mode, action_sequence, tuple(cells))


def parse_counterfactual_command(value: str) -> SessionCommand:
    aliases = {
        "BUY_AT_ASK": SessionCommand.BUY_ASK,
        "CROSS_ASK": SessionCommand.BUY_ASK,
        "HIT_BID": SessionCommand.SELL_BID,
        "JOIN_ASK": SessionCommand.SELL_ASK,
        "JOIN_BID": SessionCommand.BUY_BID,
        "LIFT_ASK": SessionCommand.BUY_ASK,
        "MKT_BUY": SessionCommand.MARKET_BUY,
        "MKT_SELL": SessionCommand.MARKET_SELL,
        "SELL_AT_BID": SessionCommand.SELL_BID,
    }
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in aliases:
        return aliases[normalized]
    try:
        return SessionCommand(normalized.lower())
    except ValueError as error:
        raise ValueError(f"unknown counterfactual command: {value}") from error


def _new_session(recording: SessionRecording) -> LiveMarketSession:
    strategy = (
        None
        if recording.strategy_source is None
        else parse_strategy(recording.strategy_source)
    )
    session = LiveMarketSession(
        ScenarioDefinition.from_dict(recording.scenario_definition),
        seed=recording.seed,
        duration_seconds=recording.duration_seconds,
        relative_volume=recording.relative_volume,
        liquidity=recording.liquidity,
        initial_quantity=recording.initial_quantity,
        quantity_options=recording.quantity_options,
        strategy_definition=strategy,
        objective=recording.objective,
        curriculum_drill=recording.curriculum_drill,
    )
    if recording.auto_start:
        session.start()
    return session


def _reconstruct_prefix(
    recording: SessionRecording,
    fork_time_us: int,
) -> LiveMarketSession:
    session = _new_session(recording)
    for expected in recording.input_records:
        if expected.simulation_time_us >= fork_time_us:
            break
        _advance_endogenous(session, expected.simulation_time_us)
        actual = session.handle_input(expected.input_key, recording.layout.bindings)
        if actual.as_dict() != expected.as_dict():
            raise RuntimeError("stored action prefix failed exact reconstruction")
    _advance_endogenous(session, fork_time_us)
    session.engine.book.assert_invariants()
    return session


def _fork_time(recording: SessionRecording, manifest: MutationManifest) -> int:
    candidates: list[int] = []
    for mutation in manifest.mutations:
        target = _recording_action(recording, mutation.target_action_sequence)
        changed_time = target.simulation_time_us + mutation.timing_delta_us
        if changed_time < 0 or changed_time > recording.completed_time_us:
            raise ValueError("mutated action time lies outside the stored run")
        candidates.append(
            changed_time
            if mutation.insert
            else min(target.simulation_time_us, changed_time)
        )
    return min(candidates)


def _plan_actions(
    recording: SessionRecording,
    mutations: tuple[ActionMutation, ...],
    fork_time_us: int,
) -> tuple[PlannedAction, ...]:
    changed = {item.target_action_sequence: item for item in mutations if not item.insert}
    inserts: dict[int, list[tuple[int, ActionMutation]]] = {}
    for index, mutation in enumerate(mutations, start=1):
        if mutation.insert:
            inserts.setdefault(mutation.target_action_sequence, []).append((index, mutation))
    actions: list[tuple[int, int, PlannedAction]] = []
    for record in recording.input_records:
        command = _recorded_command(record.resolved_command)
        mutation = changed.get(record.sequence)
        if mutation is not None:
            if (
                mutation.expected_command is not None
                and mutation.expected_command is not command
            ):
                raise ValueError(
                    f"action {record.sequence} resolved to "
                    f"{None if command is None else command.value}, not "
                    f"{mutation.expected_command.value}"
                )
            if mutation.remove:
                command = None
                action = None
            else:
                command = mutation.command or mutation.hotkey_outcome or command
                command = _apply_order_type(command, mutation.order_type)
                action = PlannedAction(
                    f"action:{record.sequence}",
                    record.sequence,
                    record.simulation_time_us + mutation.timing_delta_us,
                    command,
                    record.input_key,
                    mutation.quantity,
                    mutation.price_ticks,
                    mutation.venue_id or "PRIMARY",
                    (
                        "HOTKEY_OUTCOME_MUTATION"
                        if mutation.hotkey_outcome is not None
                        else "MUTATED"
                    ),
                    record.simulation_time_us + mutation.timing_delta_us,
                )
        else:
            action = PlannedAction(
                f"action:{record.sequence}",
                record.sequence,
                record.simulation_time_us,
                command,
                record.input_key,
                None,
                None,
                "PRIMARY",
                "PARENT",
                record.simulation_time_us,
            )
        if action is not None and action.simulation_time_us >= fork_time_us:
            _validate_single_venue_action(action)
            actions.append((action.simulation_time_us, record.sequence * 1_000, action))
        for mutation_index, insertion in inserts.get(record.sequence, []):
            insertion_command = insertion.command or insertion.hotkey_outcome
            insertion_command = _apply_order_type(
                insertion_command,
                insertion.order_type,
            )
            inserted = PlannedAction(
                f"insert:{mutation_index}:after:{record.sequence}",
                None,
                record.simulation_time_us + insertion.timing_delta_us,
                insertion_command,
                f"INSERT-{mutation_index}",
                insertion.quantity,
                insertion.price_ticks,
                insertion.venue_id or "PRIMARY",
                "INSERTED",
                record.simulation_time_us + insertion.timing_delta_us,
            )
            if inserted.simulation_time_us >= fork_time_us:
                _validate_single_venue_action(inserted)
                actions.append(
                    (
                        inserted.simulation_time_us,
                        record.sequence * 1_000 + mutation_index,
                        inserted,
                    )
                )
    return tuple(item[2] for item in sorted(actions, key=lambda item: (item[0], item[1])))


def _apply_order_type(
    command: SessionCommand | None,
    order_type: OrderType | None,
) -> SessionCommand | None:
    if order_type is None:
        return command
    if command is None:
        raise ValueError("order-type mutation requires a side-bearing command")
    if command in _BUY_COMMANDS:
        if order_type is OrderType.LIMIT:
            return SessionCommand.BUY_BID
        if order_type is OrderType.MARKET:
            return SessionCommand.MARKET_BUY
    elif command in _SELL_COMMANDS:
        if order_type is OrderType.LIMIT:
            return SessionCommand.SELL_ASK
        if order_type is OrderType.MARKET:
            return SessionCommand.MARKET_SELL
    if order_type is OrderType.CANCEL:
        return SessionCommand.CANCEL_NEAREST
    raise ValueError("order-type mutation requires a buy or sell order command")


def _validate_single_venue_action(action: PlannedAction) -> None:
    if action.venue_id != "PRIMARY":
        raise ValueError(
            "single-venue session parent cannot route to another venue; "
            "use an immutable multi-venue parent"
        )


def _continue(
    session: LiveMarketSession,
    actions: tuple[PlannedAction, ...],
    recording: SessionRecording,
    mode: CounterfactualMode,
    fixed_path: tuple[FlowEvent, ...],
    snapshot_sha256: str,
) -> tuple[LiveMarketSession, tuple[CounterfactualTimelineEntry, ...]]:
    collector = _TimelineCollector(session, snapshot_sha256)
    action_index = 0
    if mode is CounterfactualMode.ENDOGENOUS_FORK:
        for action in actions:
            collector.advance_endogenous(action.simulation_time_us)
            collector.action(action)
        collector.advance_endogenous(recording.completed_time_us)
    else:
        flow_index = 0
        while action_index < len(actions) or flow_index < len(fixed_path):
            action_time = (
                actions[action_index].simulation_time_us
                if action_index < len(actions)
                else None
            )
            flow_time = (
                fixed_path[flow_index].simulation_time_us
                if flow_index < len(fixed_path)
                else None
            )
            times = [value for value in (action_time, flow_time) if value is not None]
            if not times:
                break
            time_us = min(times)
            has_flow = flow_time == time_us
            collector.advance_exogenous(time_us, defer_strategy_at_target=has_flow)
            while (
                flow_index < len(fixed_path)
                and fixed_path[flow_index].simulation_time_us == time_us
            ):
                collector.external_flow(fixed_path[flow_index])
                flow_index += 1
            if has_flow:
                collector.advance_exogenous(time_us)
            while (
                action_index < len(actions)
                and actions[action_index].simulation_time_us == time_us
            ):
                collector.action(actions[action_index])
                action_index += 1
        collector.advance_exogenous(recording.completed_time_us)
    session.engine.book.assert_invariants()
    return session, collector.entries


class _TimelineCollector:
    def __init__(self, session: LiveMarketSession, snapshot_sha256: str) -> None:
        self.session = session
        self._entries: list[CounterfactualTimelineEntry] = []
        self._timeline_cursor = len(session.timeline)
        self._append("FORK", {"snapshot_sha256": snapshot_sha256})

    @property
    def entries(self) -> tuple[CounterfactualTimelineEntry, ...]:
        return tuple(self._entries)

    def advance_endogenous(self, target_time_us: int) -> None:
        before = len(self.session.engine.book.journal.events)
        emitted = _advance_endogenous(self.session, target_time_us)
        for event in emitted:
            self._append("ENDOGENOUS_FLOW", event.as_dict(), event.simulation_time_us)
        self._exchange_since(before)
        self._session_timeline_since_cursor()

    def advance_exogenous(
        self,
        target_time_us: int,
        *,
        defer_strategy_at_target: bool = False,
    ) -> None:
        self.session.advance_exogenous_to(
            target_time_us,
            defer_strategy_at_target=defer_strategy_at_target,
        )
        self._session_timeline_since_cursor()

    def external_flow(self, reference: FlowEvent) -> None:
        before = len(self.session.engine.book.journal.events)
        realized = self.session.apply_exogenous_flow_event(reference)
        self._append(
            "EXOGENOUS_FLOW",
            {
                "realized": realized.as_dict(),
                "reference": reference.as_dict(),
            },
            reference.simulation_time_us,
        )
        self._exchange_since(before)
        self._session_timeline_since_cursor()

    def action(self, action: PlannedAction) -> None:
        before = len(self.session.engine.book.journal.events)
        outcome = _execute_action(self.session, action)
        self._append(
            "PLAYER_ACTION",
            {
                "action": action.as_dict(),
                "outcome": _outcome_dict(outcome),
            },
            action.simulation_time_us,
        )
        self._exchange_since(before)
        self._session_timeline_since_cursor()

    def _exchange_since(self, before: int) -> None:
        for event in self.session.engine.book.journal.events[before:]:
            self._append("EXCHANGE_EVENT", event.as_dict())

    def _session_timeline_since_cursor(self) -> None:
        timeline = self.session.timeline
        for record in timeline[self._timeline_cursor :]:
            self._append(
                "SESSION_STATE",
                record.as_dict(),
                record.simulation_time_us,
            )
        self._timeline_cursor = len(timeline)

    def _append(
        self,
        kind: str,
        payload: dict[str, object],
        simulation_time_us: int | None = None,
    ) -> None:
        self._entries.append(
            CounterfactualTimelineEntry(
                len(self._entries) + 1,
                (
                    self.session.simulation_time_us
                    if simulation_time_us is None
                    else simulation_time_us
                ),
                kind,
                payload,
            )
        )


def _execute_action(
    session: LiveMarketSession,
    action: PlannedAction,
) -> CommandOutcome:
    if action.command is None:
        return CommandOutcome(None, False, f"UNBOUND KEY {action.input_key!r}")
    return session.execute(
        action.command,
        quantity_override=action.quantity,
        price_ticks_override=action.price_ticks,
    )


def _advance_endogenous(
    session: LiveMarketSession,
    target_time_us: int,
) -> tuple[FlowEvent, ...]:
    if target_time_us < session.simulation_time_us:
        raise ValueError("counterfactual action schedule moves backward")
    delta_us = target_time_us - session.simulation_time_us
    if delta_us == 0:
        return ()
    if not session.running:
        raise ValueError("counterfactual schedule advances while the session is paused")
    emitted = session.advance_by(delta_us)
    if session.simulation_time_us != target_time_us:
        raise ValueError("counterfactual action lies after session completion")
    return emitted


def _snapshot(parent_run_id: str, session: LiveMarketSession) -> BranchSnapshot:
    runtime = session.branch_runtime_state()
    engine = runtime["engine"]
    if not isinstance(engine, dict):
        raise RuntimeError("session branch engine state must be an object")
    flow_model = engine["flow_model"]
    if not isinstance(flow_model, dict):
        raise RuntimeError("flow-model state must be an object")
    strategy = runtime["strategy_state"]
    feature_windows = (
        strategy.get("feature_windows") if isinstance(strategy, dict) else None
    )
    book = session.engine.book.snapshot()
    exchange_state = session.engine.book.runtime_state()
    components = (
        _present(
            "exchange_state",
            {
                "exchange": exchange_state,
                "book_state_sha256": session.engine.book.state_sha256(),
                "pending_flow_schedule": engine["pending"],
            },
            "canonical exchange ledger, queues, fills, trades, and pending flow boundary",
        ),
        _present(
            "all_venue_states",
            {"PRIMARY": exchange_state},
            "single active venue preserved under its explicit PRIMARY identity",
        ),
        _present(
            "flow_model_state",
            flow_model,
            "active arrival-model runtime state preserved",
        ),
        (
            _present(
                "hawkes_decay_state",
                flow_model,
                "active Hawkes excitation and decay clock preserved",
            )
            if flow_model.get("model") == "hawkes"
            else _absent(
                "hawkes_decay_state",
                "parent uses the stateless simple flow model; no Hawkes state exists",
            )
        ),
        _present("rng_state", engine["rng"], "complete explicitly owned PRNG state"),
        _present(
            "simulation_clock",
            {"simulation_time_us": session.simulation_time_us},
            "simulation time is independent of wall-clock time",
        ),
        _absent(
            "pending_latency_messages",
            "single-venue live session has no latency-message subsystem",
        ),
        _present(
            "working_orders",
            runtime["working_orders"],
            "all active player orders and queue-ahead quantities preserved",
        ),
        _present("player_state", runtime["player_state"], "player position ledger preserved"),
        (
            _present("strategy_state", strategy, "active strategy runtime preserved")
            if strategy is not None
            else _absent("strategy_state", "parent has no configured strategy")
        ),
        _absent("agent_state", "synthetic session predates and does not activate WO29 agents"),
        _absent(
            "historical_replay_cursor",
            "parent is a synthetic session, not a historical replay",
        ),
        (
            _present(
                "feature_windows",
                feature_windows,
                "retained causal feature-window observations preserved",
            )
            if feature_windows is not None
            else _absent("feature_windows", "no strategy feature tracker is active")
        ),
    )
    return BranchSnapshot(
        parent_run_id,
        session.simulation_time_us,
        tuple(sorted(components, key=lambda item: item.name)),
    )


def _present(name: str, payload: object, detail: str) -> SnapshotComponent:
    return SnapshotComponent(name, ComponentStatus.PRESERVED, payload, detail)


def _absent(name: str, detail: str) -> SnapshotComponent:
    return SnapshotComponent(name, ComponentStatus.ABSENT, None, detail)


def _outcome(
    session: LiveMarketSession,
    timeline: tuple[CounterfactualTimelineEntry, ...],
) -> CounterfactualOutcome:
    session.engine.book.assert_invariants()
    metrics = _metrics(session)
    timeline_payload = [item.as_dict() for item in timeline]
    return CounterfactualOutcome(
        session.state_sha256(),
        canonical_sha256(timeline_payload),
        metrics,
        timeline,
        "PASS",
    )


def _metrics(session: LiveMarketSession) -> dict[str, object]:
    tracker = session.execution_tracker
    execution = None if tracker is None else tracker.metrics(session.simulation_time_us)
    progress = None if tracker is None else tracker.progress()
    player_fills = [
        fill for fill in session.engine.book.fills if fill.owner is OrderOwner.PLAYER
    ]
    player = session.engine.book.player_position.snapshot()
    positions = [
        int(event.data["position"])
        for event in session.engine.book.journal.events
        if event.event_type.value == "PLAYER_POSITION_CHANGED"
    ]
    working_quantity = sum(
        order.remaining_quantity
        for order in session.engine.book.active_orders.values()
        if order.owner is OrderOwner.PLAYER
    )
    snapshot = session.snapshot()
    return {
        "adverse_selection": {
            "horizon_us": None if execution is None else execution.adverse_selection_horizon_us,
            "status": "UNAVAILABLE" if execution is None else "MEASURED_IN_MODEL",
            "ticks": (
                None
                if execution is None or execution.adverse_selection_after_fill_ticks is None
                else str(execution.adverse_selection_after_fill_ticks)
            ),
        },
        "completion": {
            "complete": False if progress is None else progress.complete,
            "completed_quantity": (
                sum(fill.quantity for fill in player_fills)
                if progress is None
                else progress.completed_quantity
            ),
            "completion_percentage": (
                None if progress is None else str(progress.completion_percentage)
            ),
            "time_us": None if progress is None else progress.completion_time_us,
        },
        "deadline": {
            "completed_within_limit": (
                None if execution is None else execution.completed_within_limit
            ),
            "deadline_us": (
                None if session.objective is None else session.objective.time_limit_us
            ),
            "simulation_end_us": session.simulation_time_us,
            "status": "NOT_CONFIGURED" if session.objective is None else "MEASURED_IN_MODEL",
        },
        "fees": {
            "net_fees_micros": None,
            "reason": "single-venue session has no configured fee schedule",
            "status": "NOT_APPLICABLE",
        },
        "fill": {
            "average_fill_price_ticks": (
                None
                if execution is None or execution.average_fill_price_ticks is None
                else str(execution.average_fill_price_ticks)
            ),
            "fill_count": len(player_fills),
            "filled_quantity": sum(fill.quantity for fill in player_fills),
        },
        "pnl": {
            "reason": "session has no marked cash ledger or terminal valuation policy",
            "status": "NOT_APPLICABLE",
            "value": None,
        },
        "position": {
            "bought_quantity": int(player["bought_quantity"]),
            "net_quantity": int(player["position"]),
            "sold_quantity": int(player["sold_quantity"]),
        },
        "risk": {
            "configured_limit": None,
            "maximum_absolute_position": max((abs(value) for value in positions), default=0),
            "status": "MEASURED_NO_CONFIGURED_LIMIT",
            "working_quantity": working_quantity,
        },
        "slippage": {
            "status": "UNAVAILABLE" if execution is None else "MEASURED_IN_MODEL",
            "ticks": (
                None
                if execution is None or execution.slippage_ticks is None
                else str(execution.slippage_ticks)
            ),
        },
        "traffic_light_state": {
            "machine_state": snapshot.strategy_state,
            "signal": snapshot.traffic_light,
            "status": (
                "NOT_CONFIGURED"
                if snapshot.traffic_light == "UNCONFIGURED"
                else "MEASURED_IN_MODEL"
            ),
        },
    }


def _first_divergence(
    original: tuple[CounterfactualTimelineEntry, ...],
    branch: tuple[CounterfactualTimelineEntry, ...],
) -> FirstDivergence:
    original_payload = [item.as_dict() for item in original]
    branch_payload = [item.as_dict() for item in branch]
    length = max(len(original_payload), len(branch_payload))
    for index in range(length):
        left = original_payload[index] if index < len(original_payload) else None
        right = branch_payload[index] if index < len(branch_payload) else None
        if left != right:
            left_kind = None if left is None else left["kind"]
            right_kind = None if right is None else right["kind"]
            return FirstDivergence(
                index,
                left,
                right,
                f"paired timelines first differ at index {index}: {left_kind} versus {right_kind}",
            )
    return FirstDivergence(None, None, None, "paired timelines are structurally identical")


def _recording_action(recording: SessionRecording, sequence: int):
    if sequence > len(recording.input_records):
        raise ValueError(f"stored run has no action:{sequence}")
    return recording.input_records[sequence - 1]


def _recorded_command(value: str | None) -> SessionCommand | None:
    return None if value is None else SessionCommand(value)


def _outcome_dict(outcome: CommandOutcome) -> dict[str, object]:
    return {
        "accepted": outcome.accepted,
        "command": None if outcome.command is None else outcome.command.value,
        "message": outcome.message,
        "order_ids": list(outcome.order_ids),
        "parameters": outcome.parameters,
    }
