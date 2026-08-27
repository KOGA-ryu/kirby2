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


def run_cancel_race(
    race: str | CancelRace,
    *,
    seed: int = 42,
) -> LatencyRaceResult:
    parsed = race if isinstance(race, CancelRace) else CancelRace(race)
    session = AsynchronousExecutionSession(
        seed=seed,
        profile=get_latency_profile(LatencyProfileName.NORMAL),
    )
    external_time_us = 10_000 if parsed is CancelRace.CANCEL_WINS else 8_000
    commands = (
        LatencyCommand(
            1,
            2_000,
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
            6_000,
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
                external_time_us,
                Side.SELL,
                200,
                order_id="RACE-AGGRESSIVE-SELL",
            )
    session.advance_to(15_000)
    session.assert_invariants()
    captured = LatencyRecording.capture(session, commands)
    recording = LatencyRecording.from_dict(
        json.loads(json.dumps(captured.as_dict(), sort_keys=True))
    )
    replay = replay_latency_recording(recording)
    if not replay.passed:
        raise RuntimeError("cancel race failed exact asynchronous replay")
    order = next(item for item in session.orders if item.order_id == "RACE-PLAYER-BID")
    expected_outcome = (
        "CANCEL_WON" if parsed is CancelRace.CANCEL_WINS else "FILL_BEFORE_CANCEL"
    )
    if order.cancel_race_outcome != expected_outcome:
        raise RuntimeError(
            f"cancel race produced {order.cancel_race_outcome}, expected {expected_outcome}"
        )
    return LatencyRaceResult(
        parsed,
        session,
        order,
        session.metrics(order.order_id),
        recording,
        replay,
    )
