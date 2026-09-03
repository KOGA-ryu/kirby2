"""Self-contained start boundary and authoritative initial-frame projection."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from kirby2.scenarios import get_scenario_definition
from kirby2.session.bindings import SessionCommand
from kirby2.session.events import EventType
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession, SessionFlowConfiguration, SessionSnapshot
from kirby2.simulation import (
    LiquidityPreset,
    Regime,
    VolumePreset,
    accepted_hawkes_profile_for_regime,
    load_accepted_hawkes_configs,
)
from kirby2.simulation.flow_models import ACCEPTED_HAWKES_PATH
from kirby2.simulation.regimes import regime_profiles
from kirby2.strategy import StateMachineDefinition

from .simulation_contract import (
    SimulationComponentRefV1,
    SimulationContractDecodeError,
    SimulationContractIntegrityError,
    SimulationProfileResolutionV1,
    SimulationResolutionRefusal,
    canonical_digest,
)
from .simulation_facade import (
    _COMMAND_ACTIONS,
    _catalog_state,
    _regime_payload,
    _scenario_payload,
    resolve_simulation_profile,
)
from .simulation_interaction_contract import (
    ADVANCE_RESULT_SCHEMA_ID,
    COMMAND_RESULT_SCHEMA_ID,
    CURRENT_FRAME_RESULT_SCHEMA_ID,
    SimulationAdvanceResultV1,
    SimulationCommandRequestV1,
    SimulationCommandResultV1,
    SimulationCurrentFrameResultV1,
)
from .simulation_live_contract import (
    FRAME_SCHEMA_ID,
    RUN_REQUEST_SCHEMA_ID,
    START_RESULT_SCHEMA_ID,
    SimulationFrameV1,
    SimulationStartRefusal,
    SimulationStartResultV1,
    SimulationTrainingOptionsV1,
)


@dataclass(slots=True)
class _SimulationRunHandle:
    session: LiveMarketSession
    source_run_id: str
    run_request_sha256: str
    resolved_configuration_sha256: str
    resolution: SimulationProfileResolutionV1
    training_options: SimulationTrainingOptionsV1
    observation_policy_disclosure: str
    layout: HotkeyLayout
    semantic_input_keys: Mapping[str, str]
    frame_sequence: int
    run_state: str
    current_frame: SimulationFrameV1
    terminal_disposition: str | None = None
    reset_pending: bool = False


_PLAYER_ACTION_COMMANDS: Mapping[str, SessionCommand] = MappingProxyType(
    {
        semantic_action_id: command
        for command, (semantic_action_id, action_kind, _) in _COMMAND_ACTIONS.items()
        if action_kind == "PLAYER_ACTION"
    }
)
_RUN_ID_PATTERN = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_FRAME_ID_PATTERN = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR_ID_PATTERN = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")


def _start_result_record(
    *,
    status: str,
    source_run_id: str | None,
    run_request_sha256: str | None,
    initial_frame: dict[str, object] | None,
    refusal: dict[str, object] | None,
) -> dict[str, object]:
    basis = {
        "schema_id": START_RESULT_SCHEMA_ID,
        "schema_version": 1,
        "status": status,
        "source_run_id": source_run_id,
        "run_request_sha256": run_request_sha256,
        "initial_frame": initial_frame,
        "refusal": refusal,
    }
    return {
        **basis,
        "result_id": f"simulation-start-result-{canonical_digest(basis)[:24]}",
    }


def _refused_start(reason_code: str, explanation: str) -> dict[str, object]:
    record = _start_result_record(
        status="REFUSED",
        source_run_id=None,
        run_request_sha256=None,
        initial_frame=None,
        refusal={"reason_code": reason_code, "explanation": explanation},
    )
    return SimulationStartResultV1.from_dict(record).as_dict()


def _component_payload(reference: SimulationComponentRefV1) -> dict[str, object]:
    try:
        return _catalog_state().components.verify(reference)
    except SimulationResolutionRefusal as refusal:
        if refusal.reason_code not in {"COMPONENT_NOT_FOUND", "COMPONENT_DIGEST_MISMATCH"}:
            raise SimulationContractIntegrityError(refusal.explanation) from refusal
        raise SimulationStartRefusal(refusal.reason_code, refusal.explanation) from refusal


def _materialize_session(
    resolution: SimulationProfileResolutionV1,
    training: SimulationTrainingOptionsV1,
) -> tuple[LiveMarketSession, str, HotkeyLayout, Mapping[str, str]]:
    configuration = resolution.resolved_configuration
    if configuration is None:
        raise SimulationStartRefusal(
            "RESOLUTION_NOT_AVAILABLE", "A refused profile resolution cannot start a run."
        )
    state = _catalog_state()
    policy = training.validate_against(
        state.training,
        duration_us=configuration.duration_us,
    )
    layout_payload = _component_payload(training.layout_ref)
    layout = HotkeyLayout.default()
    if layout_payload.get("layout_name") != layout.name:
        raise SimulationStartRefusal(
            "INVALID_TRAINING_OPTIONS",
            "The selected hotkey layout has no V1 runtime materializer.",
        )
    keys_by_command = {
        binding.command: binding.key for binding in layout.bindings.bindings
    }
    semantic_input_keys = MappingProxyType(
        {
            semantic_action_id: keys_by_command[command]
            for semantic_action_id, command in _PLAYER_ACTION_COMMANDS.items()
        }
    )
    policy_payload = _component_payload(training.observation_policy_ref)
    if policy_payload.get("player_queue_disclosure") != policy["player_queue_disclosure"]:
        raise SimulationContractIntegrityError(
            "observation policy component and catalog disclosure differ"
        )
    scenario_payload = _component_payload(configuration.scenario_definition_ref)
    scenario_name = scenario_payload.get("scenario_name")
    if type(scenario_name) is not str:
        raise SimulationContractIntegrityError("scenario component omits its canonical name")
    definition = get_scenario_definition(scenario_name)
    if _scenario_payload(definition) != scenario_payload:
        raise SimulationStartRefusal(
            "RESOLUTION_CHANGED", "The accepted scenario changed after profile resolution."
        )
    if definition.regime.value != configuration.regime:
        raise SimulationContractIntegrityError("scenario regime differs from the resolution")
    regime_payload = _component_payload(configuration.regime_profile_ref)
    if _regime_payload(regime_profiles()[Regime(configuration.regime)]) != regime_payload:
        raise SimulationStartRefusal(
            "RESOLUTION_CHANGED", "The regime policy changed after profile resolution."
        )
    distribution_payload = _component_payload(configuration.distribution_bundle_ref)
    if distribution_payload != {
        "implementation_id": "KIRBY2_REGIME_NATIVE_DISTRIBUTIONS_V1",
        "ownership": "REGIME_PROFILE_FIELDS",
    }:
        raise SimulationContractIntegrityError("distribution component is not runtime-native V1")
    if configuration.queue_reactive_ref is not None or configuration.intraday_ref is not None:
        raise SimulationStartRefusal(
            "RESOLUTION_CHANGED",
            "The V1 runtime received an unadvertised queue-reactive or intraday component.",
        )
    hawkes_config = None
    if configuration.hawkes_ref is not None:
        hawkes_payload = _component_payload(configuration.hawkes_ref)
        source_sha256 = hashlib.sha256(ACCEPTED_HAWKES_PATH.read_bytes()).hexdigest()
        if hawkes_payload.get("accepted_source_sha256") != source_sha256:
            raise SimulationStartRefusal(
                "RESOLUTION_CHANGED", "The accepted Hawkes source changed after resolution."
            )
        accepted_id = hawkes_payload.get("accepted_profile_id")
        expected_id = accepted_hawkes_profile_for_regime(Regime(configuration.regime))
        if accepted_id != expected_id:
            raise SimulationContractIntegrityError("Hawkes component and regime mapping differ")
        configs = load_accepted_hawkes_configs()
        if accepted_id not in configs:
            raise SimulationStartRefusal(
                "COMPONENT_NOT_FOUND", "The accepted Hawkes profile is unavailable."
            )
        hawkes_config = configs[str(accepted_id)]
    flow_configuration = SessionFlowConfiguration(
        configuration.arrival_model_family,
        configuration.intensity_scale_ppm,
        hawkes_config,
    )
    objective = None if training.objective is None else training.objective.to_session_objective()
    session = LiveMarketSession(
        definition,
        seed=configuration.seed,
        duration_seconds=configuration.duration_us // 1_000_000,
        relative_volume=VolumePreset.parse(configuration.relative_volume),
        liquidity=LiquidityPreset.parse(configuration.liquidity),
        initial_quantity=training.initial_quantity,
        quantity_options=training.quantity_options,
        strategy_definition=None,
        objective=objective,
        curriculum_drill=None,
        flow_configuration=flow_configuration,
    )
    if training.initial_run_state == "RUNNING":
        session.start()
    disclosure = policy["player_queue_disclosure"]
    if disclosure not in {"AVAILABLE", "UNAVAILABLE"}:
        raise SimulationContractIntegrityError("observation policy disclosure is invalid")
    return session, str(disclosure), layout, semantic_input_keys


def _instrument(session: LiveMarketSession) -> dict[str, object]:
    tick_size: Decimal = session.engine.config.tick_size
    numerator, denominator = tick_size.as_integer_ratio()
    reduced = Decimal(tick_size).normalize()
    precision = max(0, -reduced.as_tuple().exponent)
    return {
        "instrument_id": "kirby2-synthetic-instrument-v1",
        "symbol": "K2-SIM",
        "display_name": "Kirby2 Synthetic Instrument",
        "venue_labels": ["KIRBY2 MATCHING ENGINE"],
        "tick_numerator": numerator,
        "tick_denominator": denominator,
        "price_precision": precision,
        "lot_size": 1,
    }


def _queue_record(quantity: int | None, disclosure: str, *, level: bool) -> dict[str, object]:
    if quantity is None and level:
        return {
            "availability": "NOT_APPLICABLE",
            "quantity": None,
            "reason": "NO_PLAYER_ORDER_AT_LEVEL",
        }
    if disclosure == "AVAILABLE":
        if quantity is None:
            raise SimulationContractIntegrityError("applicable queue disclosure lacks a quantity")
        return {"availability": "AVAILABLE", "quantity": quantity, "reason": None}
    return {
        "availability": "UNAVAILABLE",
        "quantity": None,
        "reason": "HIDDEN_BY_OBSERVATION_POLICY",
    }


def _book(snapshot: SessionSnapshot, disclosure: str) -> dict[str, object]:
    def side(rows: tuple[object, ...]) -> list[dict[str, object]]:
        return [
            {
                "price_ticks": row.price_ticks,
                "display_price": row.price,
                "aggregate_quantity": row.aggregate_quantity,
                "player_quantity": row.player_quantity,
                "first_player_queue_ahead": _queue_record(
                    row.queue_ahead_quantity,
                    disclosure,
                    level=True,
                ),
            }
            for row in rows
        ]

    return {"bids": side(snapshot.bids), "asks": side(snapshot.asks)}


def _trade_exchange_sequences(session: LiveMarketSession) -> dict[str, int]:
    result: dict[str, int] = {}
    for event in session.engine.book.journal.events:
        if event.event_type is EventType.TRADE:
            result[str(event.data["trade_id"])] = event.sequence
    return result


def _recent_trades(session: LiveMarketSession, snapshot: SessionSnapshot) -> list[dict[str, object]]:
    sequences = _trade_exchange_sequences(session)
    start = max(0, len(snapshot.tape) - 256)
    return [
        {
            "trade_sequence": index + 1,
            "exchange_event_sequence": sequences[item.trade_id],
            "trade_id": item.trade_id,
            "simulation_time_us": item.simulation_time_us,
            "price_ticks": item.price_ticks,
            "display_price": item.price,
            "quantity": item.quantity,
            "aggressor_side": item.aggressor_side.name,
        }
        for index, item in enumerate(snapshot.tape[start:], start=start)
    ]


def _working_orders(
    session: LiveMarketSession,
    snapshot: SessionSnapshot,
    disclosure: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in snapshot.working_orders:
        order = session.engine.book.active_orders.get(row.order_id)
        if order is None or order.resting_sequence is None:
            raise SimulationContractIntegrityError("working-order projection lost its active order")
        result.append(
            {
                "order_id": row.order_id,
                "side": row.side.name,
                "price_ticks": row.price_ticks,
                "display_price": row.price,
                "remaining_quantity": row.remaining_quantity,
                "filled_quantity": row.filled_quantity,
                "resting_sequence": order.resting_sequence,
                "queue_ahead": _queue_record(
                    row.queue_ahead_quantity,
                    disclosure,
                    level=False,
                ),
            }
        )
    return result


def _strategy(session: LiveMarketSession, snapshot: SessionSnapshot) -> dict[str, object]:
    definition = session.strategy_definition
    if definition is None:
        return {
            "configured": False,
            "strategy_kind": None,
            "traffic_light": "UNCONFIGURED",
            "traffic_setup": None,
            "strategy_state": None,
            "entry_permission": "UNRESTRICTED",
            "exit_permission": "UNRESTRICTED",
            "reason": snapshot.traffic_reason,
        }
    return {
        "configured": True,
        "strategy_kind": (
            "STATE_MACHINE" if isinstance(definition, StateMachineDefinition) else "TRAFFIC_LIGHT"
        ),
        "traffic_light": snapshot.traffic_light,
        "traffic_setup": snapshot.traffic_setup,
        "strategy_state": snapshot.strategy_state,
        "entry_permission": snapshot.strategy_entry_permission,
        "exit_permission": snapshot.strategy_exit_permission,
        "reason": snapshot.traffic_reason,
    }


def _completion_ppm(completed: int, target: int) -> int:
    if target == 0:
        return 1_000_000
    return min(1_000_000, (2 * completed * 1_000_000 + target) // (2 * target))


def _completion_display(completion_ppm: int) -> str:
    hundredth_percent = (completion_ppm + 50) // 100
    whole, fractional = divmod(hundredth_percent, 100)
    if fractional == 0:
        return f"{whole}%"
    return f"{whole}.{fractional:02d}".rstrip("0") + "%"


def _objective(session: LiveMarketSession) -> dict[str, object]:
    objective = session.objective
    tracker = session.execution_tracker
    if objective is None or tracker is None:
        return {
            "configured": False,
            "objective_type": None,
            "target_quantity": 0,
            "completed_quantity": 0,
            "completion_ppm": 0,
            "display_completion": "0%",
            "time_limit_us": None,
            "preferred_slippage_ticks": None,
            "complete": False,
            "completion_time_us": None,
        }
    progress = tracker.progress()
    ppm = _completion_ppm(progress.completed_quantity, progress.target_quantity)
    return {
        "configured": True,
        "objective_type": objective.objective_type.value,
        "target_quantity": progress.target_quantity,
        "completed_quantity": progress.completed_quantity,
        "completion_ppm": ppm,
        "display_completion": _completion_display(ppm),
        "time_limit_us": objective.time_limit_us,
        "preferred_slippage_ticks": objective.preferred_slippage_ticks,
        "complete": progress.complete,
        "completion_time_us": progress.completion_time_us,
    }


def _metric(
    metric_id: str,
    label: str,
    value: int | None,
    display: str | None,
    unit: str,
    exchange_sequence: int,
    *,
    semantic_role: str = "NEUTRAL",
    scope: str = "INSTANTANEOUS",
    sample_count: int = 1,
    unavailable_reason: str | None = None,
) -> dict[str, object]:
    available = value is not None and display is not None
    return {
        "metric_id": metric_id,
        "label": label,
        "availability": "AVAILABLE" if available else "UNAVAILABLE",
        "scaled_value": value,
        "display_value": display,
        "scale": 1_000_000 if unit == "PERCENT" else 1,
        "unit": unit,
        "sample_count": sample_count,
        "aggregation_scope": scope,
        "window_us": None,
        "heuristic": False,
        "as_of_exchange_event_sequence": exchange_sequence,
        "unavailable_reason": None if available else unavailable_reason,
        "semantic_role": semantic_role if available else "UNAVAILABLE",
    }


def _rounded_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("ratio denominator must be positive")
    sign = -1 if numerator < 0 else 1
    magnitude = abs(numerator)
    return sign * ((2 * magnitude + denominator) // (2 * denominator))


def _percent_metric_display(scaled_percent: int) -> str:
    sign = "-" if scaled_percent < 0 else ""
    hundredths = (abs(scaled_percent) + 5_000) // 10_000
    whole, fractional = divmod(hundredths, 100)
    return f"{sign}{whole}.{fractional:02d}%"


def _metrics(session: LiveMarketSession, snapshot: SessionSnapshot) -> list[dict[str, object]]:
    exchange_sequence = snapshot.exchange_event_sequence
    best_bid = snapshot.bids[0] if snapshot.bids else None
    best_ask = snapshot.asks[0] if snapshot.asks else None
    spread = None if best_bid is None or best_ask is None else best_ask.price_ticks - best_bid.price_ticks
    bid_depth = None if best_bid is None else best_bid.aggregate_quantity
    ask_depth = None if best_ask is None else best_ask.aggregate_quantity
    depth_total = 0 if bid_depth is None or ask_depth is None else bid_depth + ask_depth
    imbalance = (
        None
        if depth_total == 0
        else _rounded_ratio((bid_depth - ask_depth) * 100_000_000, depth_total)
    )
    flow_count = len(session.engine.flow_events)
    return [
        _metric(
            "spread_ticks",
            "Spread",
            spread,
            None if spread is None else str(spread),
            "TICKS",
            exchange_sequence,
            unavailable_reason="BOTH_BOOK_SIDES_REQUIRED",
        ),
        _metric(
            "top_bid_depth",
            "Top bid depth",
            bid_depth,
            None if bid_depth is None else str(bid_depth),
            "SHARES",
            exchange_sequence,
            semantic_role="BID",
            unavailable_reason="BID_BOOK_EMPTY",
        ),
        _metric(
            "top_ask_depth",
            "Top ask depth",
            ask_depth,
            None if ask_depth is None else str(ask_depth),
            "SHARES",
            exchange_sequence,
            semantic_role="ASK",
            unavailable_reason="ASK_BOOK_EMPTY",
        ),
        _metric(
            "top_book_imbalance",
            "Top-book imbalance",
            imbalance,
            None if imbalance is None else _percent_metric_display(imbalance),
            "PERCENT",
            exchange_sequence,
            unavailable_reason="NONZERO_TWO_SIDED_DEPTH_REQUIRED",
        ),
        _metric(
            "flow_event_count",
            "Flow events",
            flow_count,
            str(flow_count),
            "COUNT",
            exchange_sequence,
            scope="RUN_TO_CURSOR",
            sample_count=flow_count,
        ),
        _metric(
            "trade_count",
            "Trades",
            len(snapshot.tape),
            str(len(snapshot.tape)),
            "COUNT",
            exchange_sequence,
            scope="RUN_TO_CURSOR",
            sample_count=len(snapshot.tape),
        ),
    ]


def _diagnostics(session: LiveMarketSession, snapshot: SessionSnapshot) -> list[dict[str, object]]:
    details = session.engine.flow_model.diagnostics()
    model = str(details.get("model", "unknown"))
    stability = str(details.get("stability", "UNAVAILABLE"))
    status = "WARN" if stability.startswith("WARNING") else "PASS"
    return [
        {
            "diagnostic_id": "arrival_model",
            "label": "Arrival model",
            "availability": "AVAILABLE",
            "status": "INFO",
            "display_value": model,
            "unit": None,
            "explanation": "Backend-owned synthetic order-arrival model active for this run.",
            "as_of_exchange_event_sequence": snapshot.exchange_event_sequence,
            "unavailable_reason": None,
            "semantic_role": "NEUTRAL",
        },
        {
            "diagnostic_id": "arrival_stability",
            "label": "Arrival stability",
            "availability": "AVAILABLE",
            "status": status,
            "display_value": stability,
            "unit": None,
            "explanation": "Runtime model stability classification; not a release acceptance gate.",
            "as_of_exchange_event_sequence": snapshot.exchange_event_sequence,
            "unavailable_reason": None,
            "semantic_role": "WARNING" if status == "WARN" else "NEUTRAL",
        },
    ]


def _cursor_label(time_us: int) -> str:
    seconds, micros = divmod(time_us, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"T+{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}"


def _validated_run_state(snapshot: SessionSnapshot, run_state: str) -> str:
    if run_state not in {"READY", "RUNNING", "PAUSED", "COMPLETE"}:
        raise SimulationContractIntegrityError("run handle carries an invalid lifecycle state")
    if snapshot.complete != (run_state == "COMPLETE"):
        raise SimulationContractIntegrityError("session completion and run state disagree")
    if snapshot.running != (run_state == "RUNNING"):
        raise SimulationContractIntegrityError("session running flag and run state disagree")
    if run_state == "READY" and snapshot.simulation_time_us != 0:
        raise SimulationContractIntegrityError("READY run state exists only at time zero")
    return run_state


def _frame(
    handle_identity: tuple[str, str, str],
    resolution: SimulationProfileResolutionV1,
    training: SimulationTrainingOptionsV1,
    session: LiveMarketSession,
    disclosure: str,
    frame_sequence: int,
    run_state: str,
) -> SimulationFrameV1:
    source_run_id, run_request_sha256, configuration_sha256 = handle_identity
    snapshot = session.snapshot()
    book = _book(snapshot, disclosure)
    book_sha256 = canonical_digest(book)
    run_state = _validated_run_state(snapshot, run_state)
    cursor_basis = {
        "source_run_id": source_run_id,
        "simulation_time_us": snapshot.simulation_time_us,
        "duration_us": snapshot.duration_us,
        "run_state": run_state,
        "input_sequence": len(session.input_records),
        "flow_sequence": len(session.engine.flow_events),
        "exchange_event_sequence": snapshot.exchange_event_sequence,
        "trade_sequence": len(snapshot.tape),
    }
    cursor = {
        "cursor_id": f"simulation-cursor-{canonical_digest(cursor_basis)[:24]}",
        **cursor_basis,
    }
    market_basis = {
        "source_run_id": source_run_id,
        "market_state_time_us": snapshot.market_state_time_us,
        "exchange_event_sequence": snapshot.exchange_event_sequence,
        "book_state_sha256": book_sha256,
    }
    profile_ref = resolution.selection.profile_ref
    basis = {
        "schema_id": FRAME_SCHEMA_ID,
        "schema_version": 1,
        "frame_sequence": frame_sequence,
        "source_run_id": source_run_id,
        "run_request_sha256": run_request_sha256,
        "resolved_configuration_sha256": configuration_sha256,
        "profile_ref": profile_ref.as_dict(),
        "cursor": cursor,
        "market_state": {
            "market_state_id": f"simulation-market-state-{canonical_digest(market_basis)[:24]}",
            "market_state_time_us": snapshot.market_state_time_us,
            "book_state_sha256": book_sha256,
        },
        "instrument": _instrument(session),
        "clock": {
            "time_basis": "SIMULATION_ELAPSED",
            "session_origin_time_us": None,
            "display_precision_us": 1,
            "cursor_label": _cursor_label(snapshot.simulation_time_us),
            "intraday_phase": (
                resolution.resolved_configuration.intraday_phase
                if resolution.resolved_configuration is not None
                else "NOT_APPLICABLE"
            ),
        },
        "book": book,
        "recent_trades": _recent_trades(session, snapshot),
        "working_orders": _working_orders(session, snapshot, disclosure),
        "account": {
            "selected_quantity": snapshot.selected_quantity,
            "position": snapshot.position,
            "bought_quantity": snapshot.bought_quantity,
            "sold_quantity": snapshot.sold_quantity,
            "working_order_count": len(snapshot.working_orders),
        },
        "strategy": _strategy(session, snapshot),
        "objective": _objective(session),
        "diagnostics": _diagnostics(session, snapshot),
        "metrics": _metrics(session, snapshot),
        "status_message": snapshot.status_message,
        "status_role": run_state,
        "provenance": {
            "classification": "SYNTHETIC_SIMULATION_ONLY",
            "real_market_data": False,
            "matching_engine_derived": True,
            "generation_method": "ORDER_FLOW_THROUGH_MATCHING_ENGINE",
            "level2_origin": "MATCHING_ENGINE_BOOK_STATE",
            "profile_sha256": profile_ref.profile_sha256,
            "resolved_configuration_sha256": configuration_sha256,
            "run_request_sha256": run_request_sha256,
            "observation_policy_ref": training.observation_policy_ref.as_dict(),
            "display_label": "Synthetic matching-engine simulation",
        },
    }
    record = {**basis, "frame_id": f"simulation-frame-{canonical_digest(basis)[:24]}"}
    return SimulationFrameV1.from_dict(record)


def _start_simulation_run_with_source_id(
    resolution_payload: Mapping[str, object],
    training_options_payload: Mapping[str, object],
    source_run_id: str | None,
) -> tuple[object | None, dict[str, object]]:
    state = _catalog_state()
    try:
        resolution = SimulationProfileResolutionV1.from_dict(
            resolution_payload,
            catalog=state.profiles,
        )
    except SimulationResolutionRefusal as refusal:
        return None, _refused_start(
            "RESOLUTION_CHANGED", refusal.explanation
        )
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error
    if not resolution.available:
        return None, _refused_start(
            "RESOLUTION_NOT_AVAILABLE", "A refused profile resolution cannot start a run."
        )
    fresh_record = resolve_simulation_profile(resolution.selection.as_dict())
    if fresh_record.get("status") != "AVAILABLE" or fresh_record != resolution.as_dict():
        return None, _refused_start(
            "RESOLUTION_CHANGED", "The profile resolution changed before Start."
        )
    try:
        training = SimulationTrainingOptionsV1.from_dict(training_options_payload)
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error
    try:
        session, disclosure, layout, semantic_input_keys = _materialize_session(
            resolution,
            training,
        )
    except SimulationStartRefusal as refusal:
        return None, _refused_start(refusal.reason_code, refusal.explanation)
    actual_source_run_id = (
        f"simulation-run-{secrets.token_hex(16)}"
        if source_run_id is None
        else source_run_id
    )
    configuration_sha256 = resolution.resolved_configuration_sha256
    if configuration_sha256 is None:
        raise SimulationContractIntegrityError("available resolution lost its configuration digest")
    run_request = {
        "schema_id": RUN_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "resolved_configuration_sha256": configuration_sha256,
        "training_options": training.as_dict(),
    }
    run_request_sha256 = canonical_digest(run_request)
    initial_frame = _frame(
        (actual_source_run_id, run_request_sha256, configuration_sha256),
        resolution,
        training,
        session,
        disclosure,
        1,
        training.initial_run_state,
    )
    result_record = _start_result_record(
        status="AVAILABLE",
        source_run_id=actual_source_run_id,
        run_request_sha256=run_request_sha256,
        initial_frame=initial_frame.as_dict(),
        refusal=None,
    )
    result = SimulationStartResultV1.from_dict(
        result_record,
        resolution=resolution,
        training_options=training,
    )
    handle = _SimulationRunHandle(
        session=session,
        source_run_id=actual_source_run_id,
        run_request_sha256=run_request_sha256,
        resolved_configuration_sha256=configuration_sha256,
        resolution=resolution,
        training_options=training,
        observation_policy_disclosure=disclosure,
        layout=layout,
        semantic_input_keys=semantic_input_keys,
        frame_sequence=1,
        run_state=training.initial_run_state,
        current_frame=initial_frame,
    )
    return handle, result.as_dict()


def start_simulation_run(
    resolution_payload: Mapping[str, object],
    training_options_payload: Mapping[str, object],
) -> tuple[object | None, dict[str, object]]:
    """Validate a self-contained resolution and publish one fresh run atomically."""

    return _start_simulation_run_with_source_id(
        resolution_payload,
        training_options_payload,
        None,
    )


def _run_handle(value: object) -> _SimulationRunHandle:
    if type(value) is not _SimulationRunHandle:
        raise TypeError("simulation operation requires an opaque run handle")
    return value


def _operation_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise SimulationContractDecodeError(f"{label} has an invalid V1 form")
    return value


def _current_cursor(handle: _SimulationRunHandle) -> Mapping[str, object]:
    cursor = handle.current_frame.record["cursor"]
    if not isinstance(cursor, Mapping):
        raise SimulationContractIntegrityError("current simulation frame lost its cursor")
    return cursor


def _identified_result(
    schema_id: str,
    prefix: str,
    fields: Mapping[str, object],
) -> dict[str, object]:
    basis = {
        "schema_id": schema_id,
        "schema_version": 1,
        **fields,
    }
    return {**basis, "result_id": f"{prefix}{canonical_digest(basis)[:24]}"}


def _command_unavailable(
    request: SimulationCommandRequestV1,
    reason: str,
) -> dict[str, object]:
    record = _identified_result(
        COMMAND_RESULT_SCHEMA_ID,
        "simulation-command-result-",
        {
            "status": "UNAVAILABLE",
            "command_id": request.command_id,
            "source_run_id": request.source_run_id,
            "origin_frame_id": request.origin_frame_id,
            "origin_cursor_id": request.origin_cursor_id,
            "outcome": None,
            "destination_frame": None,
            "unavailable_reason": reason,
        },
    )
    return SimulationCommandResultV1.from_dict(record, request=request).as_dict()


def _command_origin_unavailable_reason(
    handle: _SimulationRunHandle,
    request: SimulationCommandRequestV1,
) -> str | None:
    if request.source_run_id != handle.source_run_id:
        return "SOURCE_RUN_MISMATCH"
    if handle.reset_pending:
        return "RESET_PENDING"
    if handle.terminal_disposition is not None:
        return "RUN_FINALIZED"
    cursor = _current_cursor(handle)
    if (
        request.origin_frame_id != handle.current_frame.frame_id
        or request.origin_cursor_id != cursor["cursor_id"]
    ):
        return "STALE_ORIGIN"
    if cursor["run_state"] == "COMPLETE":
        return "RUN_COMPLETE"
    return None


def _lifecycle_outcome(
    handle: _SimulationRunHandle,
    semantic_action_id: str,
) -> tuple[dict[str, object], str]:
    session = handle.session
    next_run_state = handle.run_state
    if semantic_action_id == "SIMULATION_PLAY":
        if session.running:
            accepted = False
            message = "SIMULATION_PLAY rejected: run already running"
            session.status_message = message
        else:
            accepted = True
            session.start()
            message = session.status_message
            next_run_state = "RUNNING"
    else:
        if not session.running:
            accepted = False
            message = "SIMULATION_PAUSE rejected: run is not running"
            session.status_message = message
        else:
            accepted = True
            session.pause()
            message = session.status_message
            next_run_state = "PAUSED"
    return (
        {
            "action_kind": "LIFECYCLE",
            "semantic_action_id": semantic_action_id,
            "accepted": accepted,
            "message": message,
            "rejection_reason": None if accepted else message,
            "input_sequence": None,
            "resulting_order_ids": [],
        },
        next_run_state,
    )


def _player_outcome(
    handle: _SimulationRunHandle,
    semantic_action_id: str,
) -> dict[str, object]:
    command = _PLAYER_ACTION_COMMANDS.get(semantic_action_id)
    key = handle.semantic_input_keys.get(semantic_action_id)
    if command is None or key is None:
        raise SimulationContractDecodeError(
            "semantic action is not a player action in the active hotkey layout"
        )
    input_record = handle.session.handle_input(key, handle.layout.bindings)
    if input_record.resolved_command != command.value:
        raise SimulationContractIntegrityError(
            "active hotkey layout resolved a different session command"
        )
    return {
        "action_kind": "PLAYER_ACTION",
        "semantic_action_id": semantic_action_id,
        "accepted": input_record.accepted,
        "message": handle.session.status_message,
        "rejection_reason": input_record.rejection_reason,
        "input_sequence": input_record.sequence,
        "resulting_order_ids": list(input_record.resulting_order_ids),
    }


def dispatch_simulation_command(
    handle_value: object,
    request_payload: Mapping[str, object],
) -> dict[str, object]:
    """Apply one origin-fenced semantic command and publish one complete frame."""

    handle = _run_handle(handle_value)
    try:
        request = SimulationCommandRequestV1.from_dict(request_payload)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error
    unavailable = _command_origin_unavailable_reason(handle, request)
    if unavailable is not None:
        return _command_unavailable(request, unavailable)
    semantic_action_id = request.semantic_action_id
    next_run_state = handle.run_state
    if semantic_action_id in {"SIMULATION_PLAY", "SIMULATION_PAUSE"}:
        outcome, next_run_state = _lifecycle_outcome(handle, semantic_action_id)
    elif semantic_action_id in _PLAYER_ACTION_COMMANDS:
        outcome = _player_outcome(handle, semantic_action_id)
    else:
        raise SimulationContractDecodeError(
            "semantic action is not command-dispatchable in the active layout"
        )
    origin = handle.current_frame
    destination = _frame(
        (
            handle.source_run_id,
            handle.run_request_sha256,
            handle.resolved_configuration_sha256,
        ),
        handle.resolution,
        handle.training_options,
        handle.session,
        handle.observation_policy_disclosure,
        handle.frame_sequence + 1,
        next_run_state,
    )
    record = _identified_result(
        COMMAND_RESULT_SCHEMA_ID,
        "simulation-command-result-",
        {
            "status": "AVAILABLE",
            "command_id": request.command_id,
            "source_run_id": request.source_run_id,
            "origin_frame_id": request.origin_frame_id,
            "origin_cursor_id": request.origin_cursor_id,
            "outcome": outcome,
            "destination_frame": destination.as_dict(),
            "unavailable_reason": None,
        },
    )
    result = SimulationCommandResultV1.from_dict(
        record,
        request=request,
        origin_frame=origin,
    )
    handle.frame_sequence = destination.frame_sequence
    handle.run_state = next_run_state
    handle.current_frame = destination
    return result.as_dict()


def _advance_unavailable(
    source_run_id: str,
    origin_frame_id: str,
    origin_cursor_id: str,
    target_time_us: int,
    reason: str,
) -> dict[str, object]:
    record = _identified_result(
        ADVANCE_RESULT_SCHEMA_ID,
        "simulation-advance-result-",
        {
            "status": "UNAVAILABLE",
            "source_run_id": source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "target_time_us": target_time_us,
            "destination_frame": None,
            "unavailable_reason": reason,
        },
    )
    return SimulationAdvanceResultV1.from_dict(record).as_dict()


def advance_simulation_run(
    handle_value: object,
    source_run_id: str,
    origin_frame_id: str,
    origin_cursor_id: str,
    target_time_us: int,
) -> dict[str, object]:
    """Advance one active run to an absolute simulation timestamp."""

    handle = _run_handle(handle_value)
    if type(target_time_us) is not int or target_time_us < 0:
        raise SimulationContractDecodeError(
            "simulation advance target must be a nonnegative integer"
        )
    source_run_id = _operation_id(
        source_run_id,
        _RUN_ID_PATTERN,
        "simulation advance source run ID",
    )
    origin_frame_id = _operation_id(
        origin_frame_id,
        _FRAME_ID_PATTERN,
        "simulation advance origin frame ID",
    )
    origin_cursor_id = _operation_id(
        origin_cursor_id,
        _CURSOR_ID_PATTERN,
        "simulation advance origin cursor ID",
    )
    cursor = _current_cursor(handle)
    if source_run_id != handle.source_run_id:
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "SOURCE_RUN_MISMATCH",
        )
    if handle.reset_pending:
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "RESET_PENDING",
        )
    if handle.terminal_disposition is not None:
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "RUN_FINALIZED",
        )
    if (
        origin_frame_id != handle.current_frame.frame_id
        or origin_cursor_id != cursor["cursor_id"]
    ):
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "STALE_ORIGIN",
        )
    if cursor["run_state"] == "COMPLETE":
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "RUN_COMPLETE",
        )
    if not handle.session.running:
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "RUN_NOT_RUNNING",
        )
    if target_time_us <= int(cursor["simulation_time_us"]):
        return _advance_unavailable(
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            "TARGET_NOT_AFTER_CURSOR",
        )
    origin = handle.current_frame
    handle.session.advance_by(target_time_us - int(cursor["simulation_time_us"]))
    next_run_state = "COMPLETE" if handle.session.complete else "RUNNING"
    destination = _frame(
        (
            handle.source_run_id,
            handle.run_request_sha256,
            handle.resolved_configuration_sha256,
        ),
        handle.resolution,
        handle.training_options,
        handle.session,
        handle.observation_policy_disclosure,
        handle.frame_sequence + 1,
        next_run_state,
    )
    record = _identified_result(
        ADVANCE_RESULT_SCHEMA_ID,
        "simulation-advance-result-",
        {
            "status": "AVAILABLE",
            "source_run_id": source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "target_time_us": target_time_us,
            "destination_frame": destination.as_dict(),
            "unavailable_reason": None,
        },
    )
    result = SimulationAdvanceResultV1.from_dict(record, origin_frame=origin)
    handle.frame_sequence = destination.frame_sequence
    handle.run_state = next_run_state
    handle.current_frame = destination
    return result.as_dict()


def read_current_simulation_frame(
    handle_value: object,
    source_run_id: str,
) -> dict[str, object]:
    """Return the exact published frame without changing any sequence."""

    handle = _run_handle(handle_value)
    source_run_id = _operation_id(
        source_run_id,
        _RUN_ID_PATTERN,
        "simulation source run ID",
    )
    if source_run_id != handle.source_run_id:
        record = {
            "schema_id": CURRENT_FRAME_RESULT_SCHEMA_ID,
            "schema_version": 1,
            "status": "UNAVAILABLE",
            "source_run_id": source_run_id,
            "current_frame": None,
            "unavailable_reason": "SOURCE_RUN_MISMATCH",
        }
    elif handle.terminal_disposition == "ABANDONED_BY_RESET":
        record = {
            "schema_id": CURRENT_FRAME_RESULT_SCHEMA_ID,
            "schema_version": 1,
            "status": "UNAVAILABLE",
            "source_run_id": source_run_id,
            "current_frame": None,
            "unavailable_reason": "RUN_ABANDONED",
        }
    else:
        record = {
            "schema_id": CURRENT_FRAME_RESULT_SCHEMA_ID,
            "schema_version": 1,
            "status": "AVAILABLE",
            "source_run_id": source_run_id,
            "current_frame": handle.current_frame.as_dict(),
            "unavailable_reason": None,
        }
    try:
        return SimulationCurrentFrameResultV1.from_dict(record).as_dict()
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error


__all__ = [
    "RUN_REQUEST_SCHEMA_ID",
    "advance_simulation_run",
    "dispatch_simulation_command",
    "read_current_simulation_frame",
    "start_simulation_run",
]
