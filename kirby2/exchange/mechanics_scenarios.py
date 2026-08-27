"""Deterministic Work Order 24 acceptance scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from .mechanics_engine import MarketMechanicsEngine
from .mechanics_models import (
    AdvancedOrderRequest,
    InstrumentRules,
    MechanicsEventType,
    OrderInstruction,
    SessionState,
)
from .mechanics_replay import (
    MechanicsCommand,
    MechanicsRecording,
    MechanicsReplayReport,
    replay_mechanics_recording,
)
from .models import OrderOwner, Side


MECHANICS_SCENARIOS = (
    "opening-auction",
    "closing-auction",
    "halt-during-momentum",
    "reopening-gap",
    "ioc-partial-fill",
    "fok-rejection",
    "post-only-rejection",
)


@dataclass(frozen=True, slots=True)
class MechanicsScenarioResult:
    name: str
    engine: MarketMechanicsEngine
    recording: MechanicsRecording
    replay: MechanicsReplayReport
    summary: dict[str, object]


class _ScenarioBuilder:
    def __init__(self, rules: InstrumentRules | None = None) -> None:
        self.engine = MarketMechanicsEngine(rules)
        self.commands: list[MechanicsCommand] = []

    def transition(self, time_us: int, state: SessionState, reason: str) -> None:
        self.engine.advance_to(time_us)
        self.engine.transition_session(state, reason=reason)
        self._record(
            time_us,
            "TRANSITION",
            {"reason": reason, "state": state.value},
        )

    def submit(self, time_us: int, request: AdvancedOrderRequest) -> None:
        self.engine.advance_to(time_us)
        self.engine.submit(request)
        self._record(time_us, "SUBMIT", {"request": request.as_dict()})

    def uncross(self, time_us: int):
        self.engine.advance_to(time_us)
        result = self.engine.uncross_auction()
        self._record(time_us, "UNCROSS", {})
        return result

    def finish(
        self,
        name: str,
        completed_time_us: int,
        summary: dict[str, object],
    ) -> MechanicsScenarioResult:
        self.engine.advance_to(completed_time_us)
        self.engine.assert_invariants()
        captured = MechanicsRecording.capture(
            self.engine,
            tuple(self.commands),
        )
        recording = MechanicsRecording.from_dict(
            json.loads(json.dumps(captured.as_dict(), sort_keys=True))
        )
        replay = replay_mechanics_recording(recording)
        if not replay.passed:
            raise RuntimeError(f"market-mechanics scenario replay failed: {name}")
        return MechanicsScenarioResult(
            name,
            self.engine,
            recording,
            replay,
            summary,
        )

    def _record(
        self,
        time_us: int,
        command_type: str,
        parameters: dict[str, object],
    ) -> None:
        self.commands.append(
            MechanicsCommand(
                len(self.commands) + 1,
                time_us,
                command_type,
                parameters,
            )
        )


def run_mechanics_scenario(name: str) -> MechanicsScenarioResult:
    runners = {
        "opening-auction": _opening_auction,
        "closing-auction": _closing_auction,
        "halt-during-momentum": _halt_during_momentum,
        "reopening-gap": _reopening_gap,
        "ioc-partial-fill": _ioc_partial_fill,
        "fok-rejection": _fok_rejection,
        "post-only-rejection": _post_only_rejection,
    }
    try:
        return runners[name]()
    except KeyError as error:
        raise ValueError(f"unknown market-mechanics scenario: {name}") from error


def run_all_mechanics_scenarios() -> tuple[MechanicsScenarioResult, ...]:
    return tuple(run_mechanics_scenario(name) for name in MECHANICS_SCENARIOS)


def _opening_auction() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    builder.transition(0, SessionState.PREOPEN, "OPENING_SEQUENCE")
    builder.submit(
        100,
        _limit(
            "OPEN-BUY",
            Side.BUY,
            100,
            102,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
            auction_only=True,
        ),
    )
    builder.submit(
        110,
        _limit(
            "OPEN-SELL-1",
            Side.SELL,
            70,
            100,
            account_id="SIM-SELL-1",
            auction_only=True,
        ),
    )
    builder.submit(
        120,
        _limit(
            "OPEN-SELL-2",
            Side.SELL,
            30,
            102,
            account_id="SIM-SELL-2",
            auction_only=True,
        ),
    )
    indication = builder.engine.auction_indication()
    builder.transition(500, SessionState.OPENING_AUCTION, "OPENING_CALL_END")
    uncross = builder.uncross(500)
    builder.transition(600, SessionState.CONTINUOUS, "OPENING_COMPLETE")
    return builder.finish(
        "opening-auction",
        1_000,
        {
            "clearing_price_ticks": uncross.indication.clearing_price_ticks,
            "indicative_matched_quantity": indication.matched_quantity,
            "matched_quantity": uncross.matched_quantity,
            "player_position": builder.engine.player_position,
            "session_state": builder.engine.session_state.value,
        },
    )


def _closing_auction() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    _open_continuous(builder)
    builder.transition(100, SessionState.CLOSING_AUCTION, "CLOSING_CALL_START")
    builder.submit(
        110,
        _limit(
            "CLOSE-BUY",
            Side.BUY,
            80,
            100,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
            auction_only=True,
        ),
    )
    builder.submit(
        120,
        _limit(
            "CLOSE-SELL",
            Side.SELL,
            100,
            99,
            account_id="SIM-CLOSE-SELL",
            auction_only=True,
        ),
    )
    indication = builder.engine.auction_indication()
    uncross = builder.uncross(200)
    builder.transition(300, SessionState.POSTCLOSE, "CLOSING_COMPLETE")
    return builder.finish(
        "closing-auction",
        500,
        {
            "clearing_price_ticks": indication.clearing_price_ticks,
            "imbalance_quantity": indication.imbalance_quantity,
            "matched_quantity": uncross.matched_quantity,
            "player_position": builder.engine.player_position,
            "session_state": builder.engine.session_state.value,
        },
    )


def _halt_during_momentum() -> MechanicsScenarioResult:
    rules = InstrumentRules(volatility_interruption_ticks=1)
    builder = _ScenarioBuilder(rules)
    _open_continuous(builder)
    builder.submit(
        100,
        _limit("MOMENTUM-ASK-1", Side.SELL, 50, 101, account_id="SIM-A"),
    )
    builder.submit(
        110,
        _limit("MOMENTUM-ASK-2", Side.SELL, 50, 103, account_id="SIM-B"),
    )
    builder.submit(
        200,
        _market(
            "MOMENTUM-BUY-1",
            Side.BUY,
            50,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
        ),
    )
    builder.submit(
        300,
        _market(
            "MOMENTUM-BUY-2",
            Side.BUY,
            50,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
        ),
    )
    protection_events = [
        event
        for event in builder.engine.events
        if event.event_type is MechanicsEventType.PROTECTION_TRIGGERED
    ]
    return builder.finish(
        "halt-during-momentum",
        500,
        {
            "last_trade_price_ticks": builder.engine.last_trade_price_ticks,
            "player_position": builder.engine.player_position,
            "protection_trigger_count": len(protection_events),
            "session_state": builder.engine.session_state.value,
        },
    )


def _reopening_gap() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    _open_continuous(builder)
    builder.transition(100, SessionState.HALTED, "MANUAL_VOLATILITY_HALT")
    builder.submit(
        110,
        _limit(
            "REOPEN-BUY",
            Side.BUY,
            100,
            105,
            owner=OrderOwner.PLAYER,
            account_id="PLAYER-1",
            auction_only=True,
        ),
    )
    builder.submit(
        120,
        _limit(
            "REOPEN-SELL",
            Side.SELL,
            100,
            104,
            account_id="SIM-REOPEN-SELL",
            auction_only=True,
        ),
    )
    indication = builder.engine.auction_indication()
    builder.transition(200, SessionState.REOPENING_AUCTION, "REOPEN_CALL")
    uncross = builder.uncross(200)
    builder.transition(300, SessionState.CONTINUOUS, "REOPEN_COMPLETE")
    return builder.finish(
        "reopening-gap",
        500,
        {
            "clearing_price_ticks": indication.clearing_price_ticks,
            "gap_ticks": indication.clearing_price_ticks - 100,  # type: ignore[operator]
            "matched_quantity": uncross.matched_quantity,
            "player_position": builder.engine.player_position,
            "session_state": builder.engine.session_state.value,
        },
    )


def _ioc_partial_fill() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    _open_continuous(builder)
    builder.submit(
        100,
        _limit("IOC-ASK", Side.SELL, 50, 101, account_id="SIM-ASK"),
    )
    request = _limit(
        "IOC-BUY",
        Side.BUY,
        100,
        101,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
        time_in_force=OrderInstruction.IOC,
    )
    builder.submit(200, request)
    order = builder.engine.get_order(request.order_id)
    return builder.finish(
        "ioc-partial-fill",
        500,
        {
            "expired_quantity": order.expired_quantity,
            "filled_quantity": order.filled_quantity,
            "player_position": builder.engine.player_position,
            "remaining_quantity": order.remaining_quantity,
            "status": order.status,
        },
    )


def _fok_rejection() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    _open_continuous(builder)
    builder.submit(
        100,
        _limit("FOK-ASK", Side.SELL, 50, 101, account_id="SIM-ASK"),
    )
    request = _limit(
        "FOK-BUY",
        Side.BUY,
        100,
        101,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
        time_in_force=OrderInstruction.FOK,
    )
    builder.submit(200, request)
    order = builder.engine.get_order(request.order_id)
    return builder.finish(
        "fok-rejection",
        500,
        {
            "available_ask_quantity": 50,
            "filled_quantity": order.filled_quantity,
            "player_position": builder.engine.player_position,
            "status": order.status,
        },
    )


def _post_only_rejection() -> MechanicsScenarioResult:
    builder = _ScenarioBuilder()
    _open_continuous(builder)
    builder.submit(
        100,
        _limit("POST-ASK", Side.SELL, 50, 101, account_id="SIM-ASK"),
    )
    request = _limit(
        "POST-BUY",
        Side.BUY,
        100,
        101,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
        modifiers=frozenset({OrderInstruction.POST_ONLY}),
    )
    builder.submit(200, request)
    order = builder.engine.get_order(request.order_id)
    return builder.finish(
        "post-only-rejection",
        500,
        {
            "best_ask_ticks": builder.engine.book.best_ask,
            "filled_quantity": order.filled_quantity,
            "player_position": builder.engine.player_position,
            "status": order.status,
        },
    )


def _open_continuous(builder: _ScenarioBuilder) -> None:
    builder.transition(0, SessionState.PREOPEN, "SESSION_START")
    builder.transition(0, SessionState.OPENING_AUCTION, "EMPTY_OPENING_CALL")
    builder.uncross(0)
    builder.transition(0, SessionState.CONTINUOUS, "OPENING_COMPLETE")


def _limit(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    *,
    account_id: str,
    owner: OrderOwner = OrderOwner.SIMULATED,
    time_in_force: OrderInstruction = OrderInstruction.DAY,
    modifiers: frozenset[OrderInstruction] = frozenset(),
    auction_only: bool = False,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        OrderInstruction.LIMIT,
        owner,
        account_id,
        price_ticks,
        time_in_force,
        modifiers,
        auction_only=auction_only,
    )


def _market(
    order_id: str,
    side: Side,
    quantity: int,
    *,
    account_id: str,
    owner: OrderOwner = OrderOwner.SIMULATED,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        OrderInstruction.MARKET,
        owner,
        account_id,
    )


def default_mechanics_rules() -> InstrumentRules:
    return InstrumentRules(
        tick_size=Decimal("0.01"),
        lot_size=1,
        minimum_quantity=1,
        maximum_quantity=1_000_000,
        lower_price_band_ticks=1,
        upper_price_band_ticks=1_000_000_000,
        reference_price_ticks=100,
    )
