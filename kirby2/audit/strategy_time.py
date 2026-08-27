"""Executable timing, ordering, and replay audit for strategy state machines."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.exchange import Order, OrderBook, Side
from kirby2.scenarios import get_scenario_definition
from kirby2.session.events import EventType, SimulationEvent
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.records import TimelineKind
from kirby2.session.replay import SessionRecording, replay_recording
from kirby2.strategy import (
    StateMachineDefinition,
    StateMachineInvariantViolation,
    StateMachineRuntime,
    parse_strategy,
)


QUIET_TRUE_FOR_SOURCE = """\
machine quiet_timer
window 1s
initial WATCH
state WATCH signal WAIT entry DENY exit ALLOW
state GO signal GREEN entry ALLOW exit ALLOW
transition WATCH -> GO when for 500ms
    spread_ticks >= 1
transition GO -> WATCH when
    spread_ticks < 1
"""


@dataclass(frozen=True, slots=True)
class StrategyTimeAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_strategy_time() -> tuple[StrategyTimeAuditCase, ...]:
    return (
        _quiet_true_for_case(),
        _slicing_and_replay_case(),
        _cooldown_case(),
        _window_expiry_case(),
        _same_time_ordering_case(),
        _cycle_guard_case(),
    )


def _quiet_true_for_case() -> StrategyTimeAuditCase:
    session = _live_session(QUIET_TRUE_FOR_SOURCE, quiet=True, duration_seconds=2)
    session.advance_by(499_999)
    before = session.snapshot().strategy_state
    session.advance_by(1)
    after = session.snapshot().strategy_state
    transitions = [
        record
        for record in session.timeline
        if record.kind is TimelineKind.TRAFFIC
        and record.data.get("current_machine_state") == "GO"
    ]
    failures: list[str] = []
    if before != "WATCH":
        failures.append("TRUE_FOR transitioned before its exact deadline")
    if after != "GO":
        failures.append("TRUE_FOR did not transition on its exact deadline")
    if [record.simulation_time_us for record in transitions] != [500_000]:
        failures.append("TRUE_FOR transition timeline omitted the 500000us boundary")
    if session.engine.flow_events:
        failures.append("quiet timing case unexpectedly emitted flow")
    return StrategyTimeAuditCase(
        "quiet_true_for_exact_deadline",
        {
            "flow_event_count": len(session.engine.flow_events),
            "state_at_499999us": before,
            "state_at_500000us": after,
            "transition_times_us": [record.simulation_time_us for record in transitions],
        },
        tuple(failures),
    )


def _slicing_and_replay_case() -> StrategyTimeAuditCase:
    one = _live_session(QUIET_TRUE_FOR_SOURCE, quiet=False, duration_seconds=2)
    many = _live_session(QUIET_TRUE_FOR_SOURCE, quiet=False, duration_seconds=2)
    one.advance_by(2_000_000)
    for _ in range(20):
        many.advance_by(100_000)
    flow_equal = [event.as_dict() for event in one.engine.flow_events] == [
        event.as_dict() for event in many.engine.flow_events
    ]
    state_equal = one.state_sha256() == many.state_sha256()
    timeline_equal = one.timeline_sha256() == many.timeline_sha256()
    recording = SessionRecording.capture(one, HotkeyLayout.default(), auto_start=True)
    with TemporaryDirectory(prefix="kirby2-strategy-time-") as directory:
        recording_path = recording.save(Path(directory) / "session.json")
        loaded = SessionRecording.load(recording_path)
    replay = replay_recording(loaded)
    failures: list[str] = []
    if not flow_equal:
        failures.append("caller slicing changed the synthetic flow path")
    if not state_equal:
        failures.append("caller slicing changed the canonical session state")
    if not timeline_equal:
        failures.append("caller slicing changed the canonical strategy timeline")
    if not replay.passed:
        failures.append("state-machine session replay did not reproduce its digests")
    return StrategyTimeAuditCase(
        "active_flow_slicing_and_replay",
        {
            "flow_equal": flow_equal,
            "flow_event_count": len(one.engine.flow_events),
            "replay_status": "PASS" if replay.passed else "FAIL",
            "recording_schema_version": loaded.as_dict()["schema_version"],
            "state_equal": state_equal,
            "state_sha256": one.state_sha256(),
            "timeline_equal": timeline_equal,
            "timeline_sha256": one.timeline_sha256(),
        },
        tuple(failures),
    )


def _cooldown_case() -> StrategyTimeAuditCase:
    source = """\
machine cooldown_timer
window 1s
initial HOLD
state HOLD signal WAIT entry DENY exit ALLOW cooldown 500ms
state GO signal GREEN entry ALLOW exit ALLOW
transition HOLD -> GO when
    spread_ticks >= 1
transition GO -> HOLD when
    spread_ticks < 1
"""
    session = _live_session(source, quiet=True, duration_seconds=2)
    session.advance_by(499_999)
    before = session.snapshot().strategy_state
    session.advance_by(1)
    after = session.snapshot().strategy_state
    failures = []
    if before != "HOLD":
        failures.append("cooldown permitted a transition before expiry")
    if after != "GO":
        failures.append("cooldown did not reevaluate at its exact expiry")
    return StrategyTimeAuditCase(
        "cooldown_exact_expiry",
        {
            "state_at_499999us": before,
            "state_at_500000us": after,
        },
        tuple(failures),
    )


def _window_expiry_case() -> StrategyTimeAuditCase:
    book = _book()
    event = SimulationEvent(
        1,
        EventType.ORDER_ADDED,
        {"remaining_quantity": 100, "side": Side.BUY.value},
    )
    occurred = _runtime(
        """\
machine occurred_expiry
window 1s
initial HOLD
state HOLD signal WAIT entry DENY exit ALLOW cooldown 600ms
state GO signal GREEN entry ALLOW exit ALLOW
transition HOLD -> GO when occurred within 500ms
    spread_ticks >= 1
transition GO -> HOLD when
    spread_ticks < 1
""",
        book,
    )
    occurred.settle(0, (event,), book)
    occurred_deadline = occurred.next_deadline_us
    occurred.settle(500_001, (), book)
    occurred.settle(600_000, (), book)

    counted = _runtime(
        """\
machine event_expiry
window 1s
initial HOLD
state HOLD signal WAIT entry DENY exit ALLOW cooldown 600ms
state GO signal GREEN entry ALLOW exit ALLOW
transition HOLD -> GO when events 2 within 500ms
    spread_ticks >= 1
transition GO -> HOLD when
    spread_ticks < 1
""",
        book,
    )
    counted.settle(0, (event,), book)
    event_deadline = counted.next_deadline_us
    counted.settle(500_001, (), book)
    counted.settle(600_000, (event,), book)
    failures = []
    if occurred_deadline != 500_001 or _current_state(occurred) != "HOLD":
        failures.append("OCCURRED_WITHIN did not expire during quiet time")
    if event_deadline != 500_001 or _current_state(counted) != "HOLD":
        failures.append("EVENTS_WITHIN counted a clock tick or retained an expired event")
    return StrategyTimeAuditCase(
        "event_and_occurrence_window_expiry",
        {
            "event_expiry_deadline_us": event_deadline,
            "event_state_after_second_event": _current_state(counted),
            "occurred_expiry_deadline_us": occurred_deadline,
            "occurred_state_after_cooldown": _current_state(occurred),
        },
        tuple(failures),
    )


def _same_time_ordering_case() -> StrategyTimeAuditCase:
    book = _book()
    runtime = _runtime(
        """\
machine simultaneous_boundary
window 1s
initial HOLD
state HOLD signal WAIT entry DENY exit ALLOW
state GO signal GREEN entry ALLOW exit ALLOW
transition HOLD -> GO when for 500ms
    aggressive_buy_volume == 0
transition GO -> HOLD when
    aggressive_buy_volume > 0
""",
        book,
    )
    deadline = runtime.next_deadline_us
    trade = SimulationEvent(
        2,
        EventType.TRADE,
        {"quantity": 100, "taker_side": Side.BUY.value},
    )
    runtime.settle(500_000, (trade,), book)
    state = _current_state(runtime)
    failures = []
    if deadline != 500_000:
        failures.append("TRUE_FOR deadline was not scheduled at 500000us")
    if state != "HOLD":
        failures.append("strategy transitioned before consuming same-time exchange evidence")
    return StrategyTimeAuditCase(
        "same_time_exchange_before_strategy",
        {
            "deadline_us": deadline,
            "state_after_same_time_trade": state,
        },
        tuple(failures),
    )


def _cycle_guard_case() -> StrategyTimeAuditCase:
    book = _book()
    runtime = _runtime(
        """\
machine zero_time_cycle
window 1s
initial A
state A signal WAIT entry DENY exit ALLOW
state B signal WAIT entry DENY exit ALLOW
transition A -> B when
    spread_ticks >= 1
transition B -> A when
    spread_ticks >= 1
""",
        book,
    )
    message = None
    try:
        runtime.settle(0, (), book)
    except StateMachineInvariantViolation as error:
        message = str(error)
    failures = []
    if message is None or "zero-time transition bound exceeded" not in message:
        failures.append("instantaneous transition cycle did not fail its invariant")
    return StrategyTimeAuditCase(
        "zero_time_cycle_guard",
        {"invariant_message": message},
        tuple(failures),
    )


def _live_session(
    source: str,
    *,
    quiet: bool,
    duration_seconds: int,
) -> LiveMarketSession:
    definition = get_scenario_definition("balanced")
    if quiet:
        definition = replace(
            definition,
            parameter_overrides={
                **definition.parameter_overrides,
                "event_intensity": 0.0,
            },
        )
    session = LiveMarketSession(
        definition,
        seed=42,
        duration_seconds=duration_seconds,
        strategy_definition=parse_strategy(source),
    )
    session.start()
    return session


def _book() -> OrderBook:
    book = OrderBook()
    book.process(Order.limit("AUDIT-BID", Side.BUY, 100, 100))
    book.process(Order.limit("AUDIT-ASK", Side.SELL, 100, 101))
    return book


def _runtime(source: str, book: OrderBook) -> StateMachineRuntime:
    definition = parse_strategy(source)
    if not isinstance(definition, StateMachineDefinition):
        raise RuntimeError("strategy-time audit source did not parse as a state machine")
    runtime = StateMachineRuntime(definition, Decimal("1"))
    runtime.reset(0, book)
    return runtime


def _current_state(runtime: StateMachineRuntime) -> str:
    if runtime.current is None:
        raise RuntimeError("strategy-time audit runtime lacks a current state")
    return runtime.current.machine_state
