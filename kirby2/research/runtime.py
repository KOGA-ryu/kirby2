"""Translate completed live sessions into replayable research-store facts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from kirby2 import __version__
from kirby2.curriculum.models import CurriculumDrill
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.objectives import SessionObjective
from kirby2.session.records import InputRecord, MarketStateRecord, TimelineKind
from kirby2.session.replay import RECORDING_SCHEMA_VERSION, SessionRecording
from kirby2.session.scoring import build_session_report
from kirby2.simulation import LiquidityPreset, VolumePreset

from .models import RUN_CONFIGURATION_SCHEMA_VERSION
from .tables import TABLE_SPECS, read_parquet_table
from .toml_codec import canonical_digest, canonical_toml, decode_payload, encode_payload


def session_configuration(
    recording: SessionRecording,
    session: LiveMarketSession,
) -> dict[str, object]:
    flow_model = session.engine.flow_model.replay_config() or {
        "model": type(session.engine.flow_model).__name__,
    }
    payload: dict[str, object] = {
        "schema_version": RUN_CONFIGURATION_SCHEMA_VERSION,
        "recording_schema_version": RECORDING_SCHEMA_VERSION,
        "session": {
            "auto_start": recording.auto_start,
            "complete": recording.complete,
            "completed_time_us": recording.completed_time_us,
            "duration_seconds": recording.duration_seconds,
            "initial_quantity": recording.initial_quantity,
            "liquidity": recording.liquidity.value,
            "quantity_options": list(recording.quantity_options),
            "relative_volume": recording.relative_volume.value,
            "seed": recording.seed,
        },
        "scenario": recording.scenario_definition,
        "layout": recording.layout.as_dict(),
        "flow_model": flow_model,
        "latency": {
            "action_model": "OBSERVED_UI_DECISION_LATENCY",
            "exchange_transport_model": "NONE_WORK_ORDER_21",
            "reference": "LATEST_VISIBLE_MARKET_STATE_TIME",
        },
        "strategy": {
            "enabled": recording.strategy_source is not None,
            "strategy_id": strategy_id(recording.strategy_source),
        },
        "objective": {
            "enabled": recording.objective is not None,
        },
        "curriculum": {
            "enabled": recording.curriculum_drill is not None,
        },
        "result": {
            "expected_state_sha256": recording.expected_state_sha256,
            "expected_timeline_sha256": recording.expected_timeline_sha256,
            "result_digest": recording_result_digest(recording),
        },
    }
    if recording.strategy_source is not None:
        payload["strategy"]["source"] = recording.strategy_source  # type: ignore[index]
    if recording.objective is not None:
        payload["objective"]["definition"] = recording.objective.as_dict()  # type: ignore[index]
    if recording.curriculum_drill is not None:
        payload["curriculum"]["drill"] = (  # type: ignore[index]
            recording.curriculum_drill.as_dict()
        )
    return payload


def recording_result_digest(recording: SessionRecording) -> str:
    return canonical_digest(
        {
            "state_sha256": recording.expected_state_sha256,
            "timeline_sha256": recording.expected_timeline_sha256,
        }
    )


def strategy_id(source: str | None) -> str:
    return "NONE" if source is None else "strategy-" + canonical_digest({"source": source})[:20]


def flow_model_id(session: LiveMarketSession) -> str:
    config = session.engine.flow_model.replay_config() or {
        "model": type(session.engine.flow_model).__name__,
    }
    return str(config.get("model", config.get("type", "UNKNOWN"))).upper()


def market_profile_id(recording: SessionRecording) -> str:
    regime = str(recording.scenario_definition.get("regime", "UNKNOWN"))
    return f"{regime}:{recording.relative_volume.value}:{recording.liquidity.value}"


def git_commit(repository: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNAVAILABLE"


def extract_session_tables(
    recording: SessionRecording,
    session: LiveMarketSession,
) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {
        spec.name: [] for spec in TABLE_SPECS
    }
    event_times, event_origins = _event_context(session, recording)
    for event in session.engine.book.journal.events:
        tables["events"].append(
            {
                "event_sequence": event.sequence,
                "simulation_time_us": event_times.get(event.sequence),
                "event_type": event.event_type.value,
                "origin": event_origins.get(event.sequence, "UNMAPPED_EXCHANGE"),
                "payload_toml": encode_payload(event.data),
            }
        )
    for state in recording.market_states:
        tables["book_snapshots"].append(
            {
                "snapshot_id": state.state_id,
                "snapshot_kind": "PLAYER_OBSERVATION",
                "simulation_time_us": state.simulation_time_us,
                "observed_state_time_us": state.observed_state_time_us,
                "exchange_event_sequence": state.exchange_event_sequence,
                "snapshot_toml": encode_payload(state.snapshot),
            }
        )
    tables["book_snapshots"].append(
        {
            "snapshot_id": "FINAL-" + recording.expected_state_sha256[:20],
            "snapshot_kind": "FINAL_STATE",
            "simulation_time_us": recording.completed_time_us,
            "observed_state_time_us": recording.completed_time_us,
            "exchange_event_sequence": len(session.engine.book.journal.events),
            "snapshot_toml": encode_payload(
                {
                    "book": session.engine.book.snapshot(),
                    "selected_quantity": session.selected_quantity,
                    "working_order_ids": [
                        order.order_id
                        for order in session.engine.book.active_orders.values()
                        if order.owner.value == "player"
                    ],
                }
            ),
        }
    )
    for order in session.engine.book.all_orders.values():
        tables["orders"].append(
            {
                "order_id": order.order_id,
                "order_type": order.order_type.value,
                "owner": order.owner.value,
                "side": None if order.side is None else order.side.value,
                "price_ticks": order.price_ticks,
                "original_quantity": order.original_quantity,
                "filled_quantity": order.filled_quantity,
                "remaining_quantity": order.remaining_quantity,
                "cancelled_quantity": order.cancelled_quantity,
                "status": order.status.value,
                "resting_sequence": order.resting_sequence,
                "cancel_target_id": order.cancel_target_id,
            }
        )
    for sequence, fill in enumerate(session.engine.book.fills, start=1):
        tables["fills"].append(
            {
                "fill_sequence": sequence,
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "owner": fill.owner.value,
                "side": fill.side.value,
                "price_ticks": fill.price_ticks,
                "quantity": fill.quantity,
                "liquidity": fill.liquidity,
            }
        )
    for sequence, trade in enumerate(session.engine.book.trades, start=1):
        tables["trades"].append(
            {
                "trade_sequence": sequence,
                "trade_id": trade.trade_id,
                "price_ticks": trade.price_ticks,
                "quantity": trade.quantity,
                "maker_order_id": trade.maker_order_id,
                "taker_order_id": trade.taker_order_id,
                "taker_side": trade.taker_side.value,
            }
        )
    for action in recording.input_records:
        tables["player_actions"].append(
            {
                "action_sequence": action.sequence,
                "simulation_time_us": action.simulation_time_us,
                "input_key": action.input_key,
                "resolved_command": action.resolved_command,
                "parameters_toml": encode_payload(action.order_parameters),
                "market_state_id": action.market_state_id,
                "latency_reference_time_us": action.latency_reference_time_us,
                "action_latency_us": action.action_latency_us,
                "accepted": action.accepted,
                "rejection_reason": action.rejection_reason,
                "resulting_order_id": action.resulting_order_id,
                "resulting_order_ids_toml": encode_payload(
                    {"values": list(action.resulting_order_ids)}
                ),
            }
        )
    transition_sequence = 0
    state_sequence = 0
    for record in session.timeline:
        if record.kind is TimelineKind.TRAFFIC:
            transition_sequence += 1
            tables["traffic_light_transitions"].append(
                {
                    "transition_sequence": transition_sequence,
                    "simulation_time_us": record.simulation_time_us,
                    "message": record.message,
                    "data_toml": encode_payload(record.data),
                }
            )
        elif record.kind is TimelineKind.STRATEGY_EVALUATION:
            state_sequence += 1
            tables["strategy_states"].append(
                {
                    "state_sequence": state_sequence,
                    "simulation_time_us": record.simulation_time_us,
                    "message": record.message,
                    "data_toml": encode_payload(record.data),
                }
            )
    if recording.objective is not None:
        report = build_session_report(session)
        for family in (report.reading, report.discipline, report.execution):
            tables["scores"].append(
                {
                    "score_name": family.name,
                    "score_value": None if family.score is None else str(family.score),
                    "status": family.status,
                    "heuristic": family.heuristic,
                    "explanation": family.explanation,
                    "components_toml": encode_payload(family.components),
                }
            )
    scenario_reference = (
        "package://kirby2.scenarios/accepted_scenarios.json#"
        + str(recording.scenario_definition.get("name", "UNKNOWN"))
    )
    tables["data_provenance"].append(
        {
            "provenance_sequence": 1,
            "dataset_reference": scenario_reference,
            "capability": "SYNTHETIC_EVENT_GENERATOR",
            "provenance_type": "SYNTHETIC",
            "content_sha256": canonical_digest(
                {"scenario": recording.scenario_definition}
            ),
            "notes": "Deterministic synthetic scenario definition; not real-market data.",
        }
    )
    return tables


def load_recording_from_artifacts(
    configuration: dict[str, object],
    tables_directory: Path,
) -> SessionRecording:
    if configuration.get("schema_version") != RUN_CONFIGURATION_SCHEMA_VERSION:
        raise ValueError("unsupported run configuration schema version")
    if configuration.get("recording_schema_version") != RECORDING_SCHEMA_VERSION:
        raise ValueError("unsupported embedded session recording schema version")
    session = _table(configuration, "session")
    scenario = _table(configuration, "scenario")
    layout = _table(configuration, "layout")
    strategy = _table(configuration, "strategy")
    objective = _table(configuration, "objective")
    curriculum = _table(configuration, "curriculum")
    result = _table(configuration, "result")
    action_rows = read_parquet_table(tables_directory / "player_actions.parquet")
    snapshot_rows = read_parquet_table(tables_directory / "book_snapshots.parquet")
    input_records = tuple(
        InputRecord(
            sequence=int(row["action_sequence"]),
            simulation_time_us=int(row["simulation_time_us"]),
            input_key=str(row["input_key"]),
            resolved_command=(
                None if row["resolved_command"] is None else str(row["resolved_command"])
            ),
            order_parameters=decode_payload(str(row["parameters_toml"])),
            market_state_id=str(row["market_state_id"]),
            latency_reference_time_us=int(row["latency_reference_time_us"]),
            action_latency_us=int(row["action_latency_us"]),
            accepted=bool(row["accepted"]),
            rejection_reason=(
                None if row["rejection_reason"] is None else str(row["rejection_reason"])
            ),
            resulting_order_id=(
                None if row["resulting_order_id"] is None else str(row["resulting_order_id"])
            ),
            resulting_order_ids=tuple(
                str(value)
                for value in decode_payload(
                    str(row["resulting_order_ids_toml"])
                )["values"]
            ),
        )
        for row in sorted(action_rows, key=lambda item: int(item["action_sequence"]))
    )
    market_states = tuple(
        MarketStateRecord(
            state_id=str(row["snapshot_id"]),
            simulation_time_us=int(row["simulation_time_us"]),
            observed_state_time_us=int(row["observed_state_time_us"]),
            exchange_event_sequence=int(row["exchange_event_sequence"]),
            snapshot=decode_payload(str(row["snapshot_toml"])),
        )
        for row in sorted(
            (
                row
                for row in snapshot_rows
                if row["snapshot_kind"] == "PLAYER_OBSERVATION"
            ),
            key=lambda item: (int(item["simulation_time_us"]), str(item["snapshot_id"])),
        )
    )
    raw_quantities = session.get("quantity_options")
    if not isinstance(raw_quantities, list):
        raise ValueError("run configuration quantity options are invalid")
    raw_bindings = layout.get("bindings")
    if not isinstance(raw_bindings, dict):
        raise ValueError("run configuration layout bindings are invalid")
    objective_definition = objective.get("definition")
    curriculum_definition = curriculum.get("drill")
    return SessionRecording(
        scenario_definition=scenario,
        seed=int(session["seed"]),
        duration_seconds=int(session["duration_seconds"]),
        relative_volume=VolumePreset.parse(str(session["relative_volume"])),
        liquidity=LiquidityPreset.parse(str(session["liquidity"])),
        initial_quantity=int(session["initial_quantity"]),
        quantity_options=tuple(int(value) for value in raw_quantities),
        layout=HotkeyLayout.from_dict(layout),
        strategy_source=(
            None if not bool(strategy["enabled"]) else str(strategy["source"])
        ),
        objective=(
            None
            if not bool(objective["enabled"])
            else SessionObjective.from_dict(_dict_value(objective_definition, "objective"))
        ),
        auto_start=bool(session["auto_start"]),
        input_records=input_records,
        market_states=market_states,
        completed_time_us=int(session["completed_time_us"]),
        complete=bool(session["complete"]),
        expected_state_sha256=str(result["expected_state_sha256"]),
        expected_timeline_sha256=str(result["expected_timeline_sha256"]),
        curriculum_drill=(
            None
            if not bool(curriculum["enabled"])
            else CurriculumDrill.from_dict(
                _dict_value(curriculum_definition, "curriculum drill")
            )
        ),
    )


def configuration_toml(
    recording: SessionRecording,
    session: LiveMarketSession,
) -> str:
    return canonical_toml(session_configuration(recording, session))


def software_version() -> str:
    return __version__


def _event_context(
    session: LiveMarketSession,
    recording: SessionRecording,
) -> tuple[dict[int, int], dict[int, str]]:
    event_times = {
        sequence: 0
        for sequence in range(1, session.engine.initial_exchange_event_count + 1)
    }
    event_origins = {
        sequence: "INITIAL_BOOK"
        for sequence in range(1, session.engine.initial_exchange_event_count + 1)
    }
    for flow_event in session.engine.flow_events:
        if flow_event.exchange_event_start is None or flow_event.exchange_event_end is None:
            continue
        for sequence in range(
            flow_event.exchange_event_start,
            flow_event.exchange_event_end + 1,
        ):
            event_times[sequence] = flow_event.simulation_time_us
            event_origins[sequence] = "SYNTHETIC_FLOW"
    action_by_order_id = {
        order_id: action
        for action in recording.input_records
        for order_id in action.resulting_order_ids
    }
    journal = session.engine.book.journal.events
    starts: list[tuple[int, InputRecord]] = []
    for event in journal:
        order_id = event.data.get("order_id")
        action = action_by_order_id.get(str(order_id))
        if event.event_type.value == "ORDER_SUBMITTED" and action is not None:
            starts.append((event.sequence, action))
    known_boundaries = sorted(
        {
            1,
            len(journal) + 1,
            *(
                flow.exchange_event_start
                for flow in session.engine.flow_events
                if flow.exchange_event_start
            ),
            *(sequence for sequence, _action in starts),
        }
    )
    for start, action in starts:
        next_boundary = next(
            boundary for boundary in known_boundaries if boundary > start
        )
        for sequence in range(start, next_boundary):
            if event_origins.get(sequence) == "SYNTHETIC_FLOW":
                break
            event_times[sequence] = action.simulation_time_us
            event_origins[sequence] = "PLAYER_ACTION"
    return event_times, event_origins


def _table(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"run configuration {key} table is missing")
    return value


def _dict_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"run configuration {label} is invalid")
    return value
