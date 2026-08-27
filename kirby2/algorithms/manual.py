"""Verified projection of player session recordings into benchmark policies."""

from __future__ import annotations

from kirby2.exchange.models import Side
from kirby2.multivenue.models import canonical_sha256
from kirby2.session.bindings import SessionCommand
from kirby2.session.replay import SessionRecording, replay_recording

from .models import AlgorithmName, AlgorithmParameterManifest


MANUAL_REPLAY_TRANSLATION_VERSION = 1


def manual_manifest_from_session_recording(
    recording: SessionRecording,
    *,
    objective_side: Side,
    benchmark_duration_us: int,
    decision_interval_us: int,
) -> AlgorithmParameterManifest:
    """Project an exact player recording onto the benchmark decision grid.

    The source session and fragmented benchmark are different market runtimes. This
    adapter therefore replays the player's observable action schedule, not the
    source fills. Every translation is committed in the parameter manifest.
    """

    if not isinstance(objective_side, Side):
        raise TypeError("manual replay objective side must use Side")
    if benchmark_duration_us <= 0 or decision_interval_us <= 0:
        raise ValueError("manual replay benchmark timing must be positive")
    if benchmark_duration_us % decision_interval_us:
        raise ValueError("manual replay duration must divide into decision intervals")

    source_replay = replay_recording(recording)
    if not source_replay.passed:
        raise ValueError("player session recording failed exact source replay")

    replay_actions: list[dict[str, object]] = []
    mappings: list[dict[str, object]] = []
    rejected_inputs: list[dict[str, object]] = []
    ignored_inputs: list[dict[str, object]] = []
    occupied_times: dict[int, int] = {}

    for item in recording.input_records:
        if not item.accepted:
            rejected_inputs.append(
                {
                    "command": item.resolved_command,
                    "input_sequence": item.sequence,
                    "reason": item.rejection_reason,
                }
            )
            continue
        if item.resolved_command is None:
            raise ValueError("accepted player input lacks a resolved command")
        command = SessionCommand(item.resolved_command)
        if command in {
            SessionCommand.INCREASE_QUANTITY,
            SessionCommand.DECREASE_QUANTITY,
            SessionCommand.TOGGLE_RUN,
            SessionCommand.QUIT,
        }:
            ignored_inputs.append(
                {
                    "command": command.value,
                    "input_sequence": item.sequence,
                    "reason": "CONTROL_ONLY_NO_EXECUTION_ACTION",
                }
            )
            continue
        if command in {SessionCommand.RESET, SessionCommand.FLATTEN}:
            raise ValueError(
                f"player command {command.value} cannot be projected into a one-way "
                "execution objective"
            )

        action = _translate_execution_input(command, item.order_parameters, objective_side)
        elapsed_time_us = _ceil_to_grid(
            item.simulation_time_us,
            decision_interval_us,
        )
        if elapsed_time_us > benchmark_duration_us:
            raise ValueError(
                f"player input sequence {item.sequence} occurs after the benchmark deadline"
            )
        prior_sequence = occupied_times.get(elapsed_time_us)
        if prior_sequence is not None:
            raise ValueError(
                "player inputs project to the same benchmark decision time; increase "
                f"decision frequency (sequences {prior_sequence} and {item.sequence})"
            )
        occupied_times[elapsed_time_us] = item.sequence
        action["elapsed_time_us"] = elapsed_time_us
        replay_actions.append(action)
        mappings.append(
            {
                "command": command.value,
                "effective_elapsed_time_us": elapsed_time_us,
                "input_sequence": item.sequence,
                "projected_action_type": action["action_type"],
                "source_simulation_time_us": item.simulation_time_us,
            }
        )

    if not replay_actions:
        raise ValueError("player recording contains no projectable accepted execution action")

    source_payload = recording.as_dict()
    provenance: dict[str, object] = {
        "action_mappings": mappings,
        "benchmark_duration_us": benchmark_duration_us,
        "decision_interval_us": decision_interval_us,
        "ignored_inputs": ignored_inputs,
        "objective_side": objective_side.value,
        "projected_action_count": len(replay_actions),
        "rejected_inputs": rejected_inputs,
        "source_recording_sha256": canonical_sha256(source_payload),
        "source_scenario_name": str(recording.scenario_definition.get("name", "")),
        "source_seed": recording.seed,
        "source_state_sha256": recording.expected_state_sha256,
        "source_sha256": canonical_sha256(source_payload),
        "source_strategy_sha256": (
            None
            if recording.strategy_source is None
            else canonical_sha256(recording.strategy_source)
        ),
        "source_timeline_sha256": recording.expected_timeline_sha256,
        "source_type": "KIRBY2_PLAYER_SESSION_RECORDING",
        "source_verification": "EXACT_SESSION_REPLAY",
        "traffic_light_guided": recording.strategy_source is not None,
        "translation_limitations": [
            "SOURCE_FILLS_ARE_NOT_IMPORTED",
            "CANCEL_NEAREST_PROJECTS_TO_CANCEL_ALL_VISIBLE_WORKING",
            "REPLACE_NEAREST_PROJECTS_TO_CANCEL_ALL_THEN_NEW",
            "ACTION_TIMES_CEIL_TO_DECISION_GRID",
        ],
        "translation_version": MANUAL_REPLAY_TRANSLATION_VERSION,
    }
    return AlgorithmParameterManifest(
        AlgorithmName.MANUAL_REPLAY,
        {
            "replay_actions": replay_actions,
            "replay_provenance": provenance,
        },
    )


def _translate_execution_input(
    command: SessionCommand,
    parameters: dict[str, object],
    objective_side: Side,
) -> dict[str, object]:
    if command in {SessionCommand.CANCEL_NEAREST, SessionCommand.CANCEL_ALL}:
        return {"action_type": "CANCEL"}

    expected_side = {
        SessionCommand.BUY_BID: Side.BUY,
        SessionCommand.BUY_ASK: Side.BUY,
        SessionCommand.MARKET_BUY: Side.BUY,
        SessionCommand.SELL_ASK: Side.SELL,
        SessionCommand.SELL_BID: Side.SELL,
        SessionCommand.MARKET_SELL: Side.SELL,
    }.get(command)
    if command is SessionCommand.REPLACE_NEAREST:
        try:
            expected_side = Side(str(parameters["side"]))
        except (KeyError, ValueError) as error:
            raise ValueError("recorded replacement lacks a valid side") from error
    if expected_side is None:
        raise ValueError(f"unsupported accepted player execution command: {command.value}")
    if expected_side is not objective_side:
        raise ValueError(
            f"player command {command.value} side differs from the benchmark objective"
        )

    try:
        quantity = int(parameters["quantity"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"recorded {command.value} lacks a valid quantity") from error
    if quantity <= 0:
        raise ValueError(f"recorded {command.value} quantity must be positive")

    if command in {SessionCommand.MARKET_BUY, SessionCommand.MARKET_SELL}:
        return {
            "action_type": "SUBMIT",
            "maximum_venues": 3,
            "quantity": quantity,
            "route_policy": "SWEEP",
            "route_style": "AGGRESSIVE",
        }

    try:
        price_ticks = int(parameters["price_ticks"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"recorded {command.value} lacks a valid integer-tick price") from error
    if price_ticks <= 0:
        raise ValueError(f"recorded {command.value} price must be positive")

    passive = command in {
        SessionCommand.BUY_BID,
        SessionCommand.SELL_ASK,
        SessionCommand.REPLACE_NEAREST,
    }
    return {
        "action_type": (
            "REPLACE" if command is SessionCommand.REPLACE_NEAREST else "SUBMIT"
        ),
        "limit_price_ticks": price_ticks,
        "maximum_venues": 1,
        "quantity": quantity,
        "route_policy": "PASSIVE_QUEUE" if passive else "BEST_DISPLAYED_PRICE",
        "route_style": "PASSIVE" if passive else "AGGRESSIVE",
    }


def _ceil_to_grid(time_us: int, interval_us: int) -> int:
    if time_us < 0:
        raise ValueError("player action time cannot be negative")
    return ((time_us + interval_us - 1) // interval_us) * interval_us
