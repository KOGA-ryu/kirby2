"""Exogenous counterfactual branches for immutable multi-venue algorithm runs."""

from __future__ import annotations

from decimal import Decimal

from kirby2.algorithms import AlgorithmRunStore
from kirby2.exchange import OrderType, Side
from kirby2.multivenue import (
    MarketCoordinator,
    MultiVenueCommand,
    RoutePolicy,
    RouteStyle,
    RoutingRequest,
    VenueConfig,
    apply_multivenue_command,
    replay_multivenue_recording,
)
from kirby2.multivenue.models import canonical_sha256
from kirby2.session.bindings import SessionCommand

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
    SnapshotComponent,
    TimingSweepCell,
    TimingSweepReport,
)


_PLAYER_COMMAND_TYPES = {"ROUTE", "CANCEL_ALL"}


def run_multivenue_counterfactual(
    parent_run_id: str,
    mutation_manifest: MutationManifest,
    mode: CounterfactualMode,
    *,
    parent_store_root,
) -> CounterfactualReport:
    if mode is not CounterfactualMode.EXOGENOUS_REPLAY:
        raise ValueError(
            "immutable algorithm parents contain a fixed background tape and support "
            "EXOGENOUS_REPLAY only; use a simulation-run parent for ENDOGENOUS_FORK"
        )
    store = AlgorithmRunStore(parent_store_root)
    verification = store.verify_run(parent_run_id)
    if not verification.passed:
        raise ValueError(
            "algorithm parent failed immutable verification: "
            + "; ".join(verification.failures)
        )
    recording = store.load_recording(parent_run_id)
    replay = replay_multivenue_recording(recording)
    if not replay.passed:
        raise RuntimeError("verified multi-venue parent failed exact replay")
    player_commands = tuple(
        command
        for command in recording.commands
        if command.command_type in _PLAYER_COMMAND_TYPES
    )
    fork_time_us = _fork_time(player_commands, mutation_manifest)
    original_prefix = _reconstruct_prefix(recording, fork_time_us)
    branch_prefix = _reconstruct_prefix(recording, fork_time_us)
    original_snapshot = _snapshot(parent_run_id, original_prefix)
    branch_snapshot = _snapshot(parent_run_id, branch_prefix)
    snapshots_match = original_snapshot.sha256() == branch_snapshot.sha256()
    if not snapshots_match:
        raise RuntimeError("multi-venue fork reconstruction disagrees")
    original_schedule = _plan_commands(recording.commands, player_commands, (), fork_time_us)
    branch_schedule = _plan_commands(
        recording.commands,
        player_commands,
        mutation_manifest.mutations,
        fork_time_us,
    )
    original, original_timeline, original_route_ids = _continue(
        original_prefix,
        original_schedule,
        recording.completed_time_us,
        original_snapshot.sha256(),
    )
    branch, branch_timeline, branch_route_ids = _continue(
        branch_prefix,
        branch_schedule,
        recording.completed_time_us,
        branch_snapshot.sha256(),
    )
    if original.state_sha256() != recording.expected_state_sha256:
        raise RuntimeError("unmodified multi-venue control did not reproduce parent state")
    original_outcome = _outcome(original, original_timeline, original_route_ids)
    branch_outcome = _outcome(branch, branch_timeline, branch_route_ids)
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
    external_commands = [
        command.as_dict()
        for command in original_schedule
        if command.command_type not in _PLAYER_COMMAND_TYPES
    ]
    changed_actions = _mutated_action_evidence(player_commands, mutation_manifest)
    return CounterfactualReport(
        parent_run_id,
        mode,
        mutation_manifest,
        branch_snapshot,
        snapshots_match,
        original_outcome,
        branch_outcome,
        _first_divergence(original_timeline, branch_timeline),
        comparison,
        canonical_sha256(external_commands),
        {
            "analysis_may_use_later_information": True,
            "decision_policy": "STATIC_USER_SUPPLIED_MUTATION",
            "decision_records": changed_actions,
            "privileged_snapshot_accessible_to_policy": False,
            "policy_information": "ONLY_STATE_AVAILABLE_AT_EACH_ROUTE_TIME",
            "status": "PASS",
        },
    )


def run_multivenue_timing_sweep(
    parent_run_id: str,
    action_sequence: int,
    mode: CounterfactualMode,
    *,
    parent_store_root,
) -> TimingSweepReport:
    from pathlib import Path

    from .store import CounterfactualStore

    if mode is not CounterfactualMode.EXOGENOUS_REPLAY:
        raise ValueError("multi-venue timing sweeps require EXOGENOUS_REPLAY")
    recording = AlgorithmRunStore(parent_store_root).load_recording(parent_run_id)
    player_commands = tuple(
        command
        for command in recording.commands
        if command.command_type in _PLAYER_COMMAND_TYPES
    )
    semantic = _semantic_command(_player_action(player_commands, action_sequence))
    branch_store = CounterfactualStore(
        Path(parent_store_root) / "counterfactual_runs"
    )
    cells: list[TimingSweepCell] = []
    for offset in (-500_000, -250_000, 0, 250_000, 500_000):
        report = run_multivenue_counterfactual(
            parent_run_id,
            MutationManifest(
                (
                    ActionMutation(
                        action_sequence,
                        expected_command=semantic,
                        command=semantic,
                        timing_delta_us=offset,
                    ),
                )
            ),
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


def _new_coordinator(recording) -> MarketCoordinator:
    return MarketCoordinator(
        tuple(VenueConfig.from_dict(item) for item in recording.venue_configs),
        seed=recording.seed,
        depth_subscriptions=frozenset(recording.depth_subscriptions),
    )


def _reconstruct_prefix(recording, fork_time_us: int) -> MarketCoordinator:
    coordinator = _new_coordinator(recording)
    for command in recording.commands:
        if command.simulation_time_us >= fork_time_us:
            break
        coordinator.advance_to(command.simulation_time_us)
        apply_multivenue_command(coordinator, command)
    coordinator.advance_to(fork_time_us)
    coordinator.assert_invariants()
    return coordinator


def _fork_time(
    player_commands: tuple[MultiVenueCommand, ...],
    manifest: MutationManifest,
) -> int:
    values: list[int] = []
    for mutation in manifest.mutations:
        target = _player_action(player_commands, mutation.target_action_sequence)
        changed = target.simulation_time_us + mutation.timing_delta_us
        if changed < 0:
            raise ValueError("multi-venue mutation time cannot be negative")
        values.append(changed if mutation.insert else min(target.simulation_time_us, changed))
    return min(values)


def _plan_commands(
    commands: tuple[MultiVenueCommand, ...],
    player_commands: tuple[MultiVenueCommand, ...],
    mutations: tuple[ActionMutation, ...],
    fork_time_us: int,
) -> tuple[MultiVenueCommand, ...]:
    target_by_command = {
        _player_action(player_commands, item.target_action_sequence).sequence: item
        for item in mutations
        if not item.insert
    }
    insert_by_command: dict[int, list[tuple[int, ActionMutation]]] = {}
    for index, mutation in enumerate(mutations, start=1):
        if mutation.insert:
            anchor = _player_action(player_commands, mutation.target_action_sequence)
            insert_by_command.setdefault(anchor.sequence, []).append((index, mutation))
    planned: list[tuple[int, int, str, dict[str, object]]] = []
    for command in commands:
        mutation = target_by_command.get(command.sequence)
        if mutation is not None:
            expected = _semantic_command(command)
            if mutation.expected_command is not None and mutation.expected_command is not expected:
                raise ValueError(
                    f"multi-venue action resolved to {expected.value}, not "
                    f"{mutation.expected_command.value}"
                )
            if mutation.remove:
                command_type = None
                parameters = {}
            else:
                command_type, parameters = _mutate_player_command(command, mutation)
            time_us = command.simulation_time_us + mutation.timing_delta_us
        else:
            command_type = command.command_type
            parameters = command.parameters
            time_us = command.simulation_time_us
        if command_type is not None and time_us >= fork_time_us:
            planned.append((time_us, command.sequence * 1_000, command_type, parameters))
        for mutation_index, insertion in insert_by_command.get(command.sequence, []):
            command_type, parameters = _inserted_player_command(
                insertion,
                mutation_index,
            )
            time_us = command.simulation_time_us + insertion.timing_delta_us
            if time_us >= fork_time_us:
                planned.append(
                    (
                        time_us,
                        command.sequence * 1_000 + mutation_index,
                        command_type,
                        parameters,
                    )
                )
    ordered = sorted(planned, key=lambda item: (item[0], item[1]))
    return tuple(
        MultiVenueCommand(index, time_us, command_type, dict(parameters))
        for index, (time_us, _order, command_type, parameters) in enumerate(
            ordered,
            start=1,
        )
    )


def _mutate_player_command(
    command: MultiVenueCommand,
    mutation: ActionMutation,
) -> tuple[str, dict[str, object]]:
    semantic = mutation.command or mutation.hotkey_outcome
    if command.command_type == "CANCEL_ALL":
        if semantic is None and mutation.order_type is None:
            if any(
                value is not None
                for value in (mutation.price_ticks, mutation.quantity, mutation.venue_id)
            ):
                raise ValueError("cancel-all action cannot take price, size, or venue")
            return "CANCEL_ALL", {}
        return _new_route_from_semantic(semantic, mutation, f"CF-REPLACE-{command.sequence}")
    raw_request = command.parameters.get("request")
    if not isinstance(raw_request, dict):
        raise ValueError("recorded route request is invalid")
    request = RoutingRequest.from_dict(raw_request)
    if mutation.order_type is OrderType.CANCEL or semantic in {
        SessionCommand.CANCEL_NEAREST,
        SessionCommand.CANCEL_ALL,
    }:
        return "CANCEL_ALL", {}
    side, style = _side_style(semantic, request.side, request.style)
    if mutation.order_type is OrderType.LIMIT:
        style = RouteStyle.PASSIVE
    elif mutation.order_type is OrderType.MARKET:
        style = RouteStyle.AGGRESSIVE
    policy = request.policy
    direct_venue = request.direct_venue_id
    if mutation.venue_id is not None:
        policy = RoutePolicy.DIRECT
        direct_venue = mutation.venue_id
    result = RoutingRequest(
        request.order_id,
        side,
        request.quantity if mutation.quantity is None else mutation.quantity,
        policy,
        style,
        direct_venue,
        request.limit_price_ticks if mutation.price_ticks is None else mutation.price_ticks,
        request.max_venues,
    )
    return "ROUTE", {"request": result.as_dict()}


def _inserted_player_command(
    mutation: ActionMutation,
    mutation_index: int,
) -> tuple[str, dict[str, object]]:
    semantic = mutation.command or mutation.hotkey_outcome
    return _new_route_from_semantic(
        semantic,
        mutation,
        f"CF-INSERT-{mutation_index:04d}",
    )


def _new_route_from_semantic(
    semantic: SessionCommand | None,
    mutation: ActionMutation,
    order_id: str,
) -> tuple[str, dict[str, object]]:
    if semantic in {SessionCommand.CANCEL_NEAREST, SessionCommand.CANCEL_ALL} or mutation.order_type is OrderType.CANCEL:
        return "CANCEL_ALL", {}
    if semantic is None:
        raise ValueError("inserted or replacement route requires a buy/sell command")
    side, style = _side_style(semantic, None, None)
    if mutation.order_type is OrderType.LIMIT:
        style = RouteStyle.PASSIVE
    elif mutation.order_type is OrderType.MARKET:
        style = RouteStyle.AGGRESSIVE
    policy = RoutePolicy.DIRECT if mutation.venue_id is not None else RoutePolicy.BEST_DISPLAYED_PRICE
    request = RoutingRequest(
        order_id,
        side,
        mutation.quantity or 100,
        policy,
        style,
        mutation.venue_id,
        mutation.price_ticks,
        1,
    )
    return "ROUTE", {"request": request.as_dict()}


def _side_style(
    semantic: SessionCommand | None,
    default_side: Side | None,
    default_style: RouteStyle | None,
) -> tuple[Side, RouteStyle]:
    if semantic in {SessionCommand.BUY_BID}:
        return Side.BUY, RouteStyle.PASSIVE
    if semantic in {SessionCommand.BUY_ASK, SessionCommand.MARKET_BUY}:
        return Side.BUY, RouteStyle.AGGRESSIVE
    if semantic in {SessionCommand.SELL_ASK}:
        return Side.SELL, RouteStyle.PASSIVE
    if semantic in {SessionCommand.SELL_BID, SessionCommand.MARKET_SELL}:
        return Side.SELL, RouteStyle.AGGRESSIVE
    if default_side is not None and default_style is not None:
        return default_side, default_style
    raise ValueError("route mutation requires a buy or sell semantic command")


def _semantic_command(command: MultiVenueCommand) -> SessionCommand:
    if command.command_type == "CANCEL_ALL":
        return SessionCommand.CANCEL_ALL
    raw = command.parameters.get("request")
    if not isinstance(raw, dict):
        raise ValueError("route command lacks a request")
    request = RoutingRequest.from_dict(raw)
    if request.side is Side.BUY:
        return (
            SessionCommand.BUY_BID
            if request.style is RouteStyle.PASSIVE
            else SessionCommand.BUY_ASK
        )
    return (
        SessionCommand.SELL_ASK
        if request.style is RouteStyle.PASSIVE
        else SessionCommand.SELL_BID
    )


def _continue(
    coordinator: MarketCoordinator,
    commands: tuple[MultiVenueCommand, ...],
    completed_time_us: int,
    snapshot_sha256: str,
) -> tuple[
    MarketCoordinator,
    tuple[CounterfactualTimelineEntry, ...],
    tuple[str, ...],
]:
    entries = [
        CounterfactualTimelineEntry(
            1,
            coordinator.clock.current_time_us,
            "FORK",
            {"snapshot_sha256": snapshot_sha256},
        )
    ]
    raw_routes = coordinator.branch_runtime_state()["routes"]
    if not isinstance(raw_routes, dict):
        raise RuntimeError("coordinator route state must be an object")
    route_ids: list[str] = sorted(str(value) for value in raw_routes)
    for command in commands:
        before = len(coordinator.events)
        coordinator.advance_to(command.simulation_time_us)
        route_id = apply_multivenue_command(coordinator, command)
        if route_id is not None:
            route_ids.append(route_id)
        kind = (
            "PLAYER_ACTION"
            if command.command_type in _PLAYER_COMMAND_TYPES
            else "EXOGENOUS_MARKET_COMMAND"
        )
        entries.append(
            CounterfactualTimelineEntry(
                len(entries) + 1,
                command.simulation_time_us,
                kind,
                command.as_dict(),
            )
        )
        for event in coordinator.events[before:]:
            entries.append(
                CounterfactualTimelineEntry(
                    len(entries) + 1,
                    event.simulation_time_us,
                    "COORDINATOR_EVENT",
                    event.as_dict(),
                )
            )
    if coordinator.clock.current_time_us < completed_time_us:
        coordinator.advance_to(completed_time_us)
    coordinator.assert_invariants()
    return coordinator, tuple(entries), tuple(route_ids)


def _snapshot(parent_run_id: str, coordinator: MarketCoordinator) -> BranchSnapshot:
    state = coordinator.branch_runtime_state()
    components = (
        _present("exchange_state", state, "coordinator, venue exchange, route, and event state"),
        _present("all_venue_states", state["venues"], "every independent venue state preserved"),
        _absent("flow_model_state", "algorithm parent uses a stored exogenous command tape"),
        _absent("hawkes_decay_state", "algorithm parent has no Hawkes flow model"),
        _present(
            "rng_state",
            {
                venue_id: value["latency_rng"]
                for venue_id, value in state["venues"].items()
            },
            "all explicitly owned venue latency RNG states preserved",
        ),
        _present(
            "simulation_clock",
            {"simulation_time_us": coordinator.clock.current_time_us},
            "coordinator and venue simulation clocks preserved",
        ),
        _present(
            "pending_latency_messages",
            state["pending_latency_messages"],
            "scheduled route legs and their due-time ordering preserved",
        ),
        _present("working_orders", state["working_orders"], "working orders on every venue preserved"),
        _present(
            "player_state",
            {
                "global_position": state["global_player_position"],
                "venue_positions": {
                    key: value["player_position"] for key, value in state["venues"].items()
                },
            },
            "global and per-venue player ledgers preserved",
        ),
        _absent("strategy_state", "stored algorithm continuation is a fixed action tape"),
        _absent("agent_state", "algorithm benchmark does not activate WO29 agents"),
        _absent("historical_replay_cursor", "parent is synthetic, not historical replay"),
        _absent("feature_windows", "stored action tape has no live strategy feature tracker"),
    )
    return BranchSnapshot(
        parent_run_id,
        coordinator.clock.current_time_us,
        tuple(sorted(components, key=lambda item: item.name)),
    )


def _outcome(
    coordinator: MarketCoordinator,
    timeline: tuple[CounterfactualTimelineEntry, ...],
    route_ids: tuple[str, ...],
) -> CounterfactualOutcome:
    scores = [coordinator.score_route(route_id) for route_id in route_ids]
    completed = sum(score.completed_quantity for score in scores)
    target = sum(score.target_quantity for score in scores)
    denominator = sum(score.gross_price_denominator for score in scores)
    numerator = sum(score.gross_price_numerator_x2 for score in scores)
    slippage_x2_shares = 0
    for route_id, score in zip(route_ids, scores, strict=True):
        route = coordinator.route_result(route_id)
        feed = route.decision.observable_feed
        bid = feed.get("best_bid_ticks")
        ask = feed.get("best_ask_ticks")
        if bid is None or ask is None:
            continue
        midpoint_x2 = int(bid) + int(ask)
        signed = (
            score.gross_price_numerator_x2 - midpoint_x2 * score.gross_price_denominator
            if route.request.side is Side.BUY
            else midpoint_x2 * score.gross_price_denominator - score.gross_price_numerator_x2
        )
        slippage_x2_shares += signed
    slippage = (
        None
        if denominator == 0
        else str(Decimal(slippage_x2_shares) / Decimal(2 * denominator))
    )
    metrics = {
        "adverse_selection": {
            "reason": "multi-venue recording has no configured post-fill horizon mark",
            "status": "UNAVAILABLE",
            "ticks": None,
        },
        "completion": {
            "complete": completed >= target if target else True,
            "completed_quantity": completed,
            "completion_percentage": (
                "100" if target == 0 else str(Decimal(completed) * 100 / Decimal(target))
            ),
            "time_us": coordinator.clock.current_time_us,
        },
        "deadline": {
            "completed_within_limit": completed >= target,
            "deadline_us": coordinator.clock.current_time_us,
            "simulation_end_us": coordinator.clock.current_time_us,
            "status": "MEASURED_IN_MODEL",
        },
        "fees": {
            "fees_micros": sum(score.fees_micros for score in scores),
            "net_fees_micros": sum(
                score.fees_micros - score.rebates_micros for score in scores
            ),
            "rebates_micros": sum(score.rebates_micros for score in scores),
            "status": "MEASURED_IN_MODEL",
        },
        "fill": {
            "average_fill_price_ticks": (
                None if denominator == 0 else str(Decimal(numerator) / Decimal(2 * denominator))
            ),
            "fill_count": sum(
                execution.filled_quantity > 0
                for route_id in route_ids
                for execution in coordinator.route_result(route_id).executions
            ),
            "filled_quantity": completed,
        },
        "pnl": {
            "reason": "recording has execution costs but no terminal valuation policy",
            "status": "NOT_APPLICABLE",
            "value": None,
        },
        "position": {
            "net_quantity": coordinator.global_player_position,
            "venue_positions": {
                venue_id: venue.player_position
                for venue_id, venue in sorted(coordinator.venues.items())
            },
        },
        "risk": {
            "configured_limit": None,
            "maximum_absolute_position": abs(coordinator.global_player_position),
            "pending_route_count": sum(
                not coordinator.route_result(route_id).complete for route_id in route_ids
            ),
            "status": "MEASURED_NO_CONFIGURED_LIMIT",
        },
        "slippage": {
            "status": "MEASURED_IN_MODEL" if slippage is not None else "UNAVAILABLE",
            "ticks": slippage,
        },
        "traffic_light_state": {
            "reason": "algorithm benchmark parent has no traffic-light runtime",
            "signal": None,
            "status": "NOT_CONFIGURED",
        },
    }
    return CounterfactualOutcome(
        coordinator.state_sha256(),
        canonical_sha256([item.as_dict() for item in timeline]),
        metrics,
        timeline,
        "PASS",
    )


def _first_divergence(original, branch) -> FirstDivergence:
    left = [item.as_dict() for item in original]
    right = [item.as_dict() for item in branch]
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a != b:
            return FirstDivergence(
                index,
                a,
                b,
                f"paired multi-venue timelines first differ at index {index}",
            )
    return FirstDivergence(None, None, None, "paired timelines are structurally identical")


def _mutated_action_evidence(player_commands, manifest):
    return [
        {
            "action_id": f"action:{mutation.target_action_sequence}",
            "information_cutoff_us": (
                _player_action(player_commands, mutation.target_action_sequence).simulation_time_us
                + mutation.timing_delta_us
            ),
            "uses_future_observations": False,
        }
        for mutation in manifest.mutations
    ]


def _player_action(commands, sequence):
    if sequence <= 0 or sequence > len(commands):
        raise ValueError(f"stored multi-venue run has no action:{sequence}")
    return commands[sequence - 1]


def _present(name: str, payload: object, detail: str) -> SnapshotComponent:
    return SnapshotComponent(name, ComponentStatus.PRESERVED, payload, detail)


def _absent(name: str, detail: str) -> SnapshotComponent:
    return SnapshotComponent(name, ComponentStatus.ABSENT, None, detail)
