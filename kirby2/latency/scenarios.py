"""Deterministic cancel-race acceptance scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from kirby2.exchange import Side

from .engine import AsynchronousExecutionSession
from .models import AsyncOrder, LatencyMetrics
from .profiles import LatencyProfileName, get_latency_profile
from .replay import (
    LatencyCommand,
    LatencyRecording,
    LatencyReplayReport,
    replay_latency_recording,
)


class CancelRace(str, Enum):
    CANCEL_WINS = "cancel-wins"
    FILL_WINS = "fill-wins"


@dataclass(frozen=True, slots=True)
class LatencyRaceResult:
    race: CancelRace
    session: AsynchronousExecutionSession
    order: AsyncOrder
    metrics: LatencyMetrics
    recording: LatencyRecording
    replay: LatencyReplayReport
    observable_ready_time_us: int


def cancel_race_for_seed(seed: int) -> CancelRace:
    """Choose the 8 ms or 10 ms external-flow schedule from owned seed bits."""

    if type(seed) is not int or seed < 0:
        raise ValueError("cancel-race seed must be a nonnegative integer")
    return (
        CancelRace.FILL_WINS
        if (seed >> 1) & 1
        else CancelRace.CANCEL_WINS
    )


def run_cancel_race(
    race: str | CancelRace,
    *,
    seed: int = 42,
    profile: str | LatencyProfileName = LatencyProfileName.NORMAL,
) -> LatencyRaceResult:
    parsed = race if isinstance(race, CancelRace) else CancelRace(race)
    session = AsynchronousExecutionSession(
        seed=seed,
        profile=get_latency_profile(profile),
    )
    observable_ready_time_us = _advance_to_observable_ready(session)
    external_offset_us = (
        10_000 if parsed is CancelRace.CANCEL_WINS else 8_000
    )
    limit_time_us = observable_ready_time_us + 2_000
    cancel_time_us = observable_ready_time_us + 6_000
    external_time_us = observable_ready_time_us + external_offset_us
    commands = (
        LatencyCommand(
            1,
            limit_time_us,
            "LIMIT",
            {
                "order_id": "RACE-PLAYER-BID",
                "price_ticks": 99,
                "quantity": 100,
                "side": "buy",
            },
        ),
        LatencyCommand(
            2,
            cancel_time_us,
            "CANCEL",
            {
                "cancel_id": "ASYNC-CANCEL-000001",
                "target_order_id": "RACE-PLAYER-BID",
            },
        ),
        LatencyCommand(
            3,
            external_time_us,
            "EXTERNAL_MARKET",
            {
                "order_id": "RACE-AGGRESSIVE-SELL",
                "quantity": 200,
                "side": "sell",
            },
        ),
    )
    for command in commands:
        session.advance_to(command.simulation_time_us)
        if command.command_type == "LIMIT":
            session.request_limit(
                Side.BUY,
                100,
                99,
                order_id="RACE-PLAYER-BID",
            )
        elif command.command_type == "CANCEL":
            cancel_id = session.request_cancel("RACE-PLAYER-BID")
            if cancel_id != "ASYNC-CANCEL-000001":
                raise RuntimeError("cancel race command identity diverged")
        else:
            session.schedule_aggressive_order(
                command.simulation_time_us,
                Side.SELL,
                200,
                order_id="RACE-AGGRESSIVE-SELL",
            )
    _advance_to_idle(session)
    session.assert_invariants()
    captured = LatencyRecording.capture(session, commands)
    recording = LatencyRecording.from_dict(
        json.loads(json.dumps(captured.as_dict(), sort_keys=True))
    )
    replay = replay_latency_recording(recording)
    if not replay.passed:
        raise RuntimeError("cancel race failed exact asynchronous replay")
    order = next(item for item in session.orders if item.order_id == "RACE-PLAYER-BID")
    return LatencyRaceResult(
        parsed,
        session,
        order,
        session.metrics(order.order_id),
        recording,
        replay,
        observable_ready_time_us,
    )


def _advance_to_observable_ready(
    session: AsynchronousExecutionSession,
) -> int:
    while session.latest_display is None:
        horizon = session.pending_event_horizon_us
        if horizon is None:
            raise RuntimeError("initial observable market state cannot arrive")
        session.advance_to(horizon)
    return session.clock.current_time_us


def _advance_to_idle(session: AsynchronousExecutionSession) -> None:
    while session.pending_event_horizon_us is not None:
        horizon = session.pending_event_horizon_us
        if horizon is None:  # pragma: no cover - loop condition narrows this
            raise RuntimeError("pending latency horizon disappeared")
        session.advance_to(horizon)
