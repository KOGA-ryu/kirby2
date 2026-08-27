"""Deterministic hidden-liquidity scenarios and paired blind exercise."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.exchange.models import OrderOwner, Side

from .models import (
    HiddenLiquidityRules,
    HiddenOrderRequest,
    IcebergDefinition,
    IcebergRefreshBehavior,
    LiquidityKind,
    ObservableEvent,
    ObservableEventType,
    ObservabilityScore,
    RefreshEventVisibility,
)
from .replay import (
    ObservabilityCommand,
    ObservabilityRecording,
    ObservabilityReplayReport,
    recording_json_round_trip,
    replay_observability_recording,
)
from .venue import HiddenLiquidityVenue


HIDDEN_LIQUIDITY_SCENARIOS = (
    "iceberg-absorption",
    "hidden-midpoint-fill",
    "repeated-displayed-refresh",
    "apparent-wall",
    "small-displayed-deep-hidden",
)


@dataclass(frozen=True, slots=True)
class HiddenLiquidityScenarioResult:
    name: str
    venue: HiddenLiquidityVenue
    recording: ObservabilityRecording
    replay: ObservabilityReplayReport
    summary: dict[str, object]

    @property
    def timeline(self) -> str:
        return render_observable_timeline(self.venue.observable_feed().events)


@dataclass(frozen=True, slots=True)
class BlindExerciseResult:
    shallow: HiddenLiquidityScenarioResult
    deep: HiddenLiquidityScenarioResult
    initial_observable_sha256: str
    shallow_fill_quantity: int
    deep_fill_quantity: int
    shallow_score: ObservabilityScore
    deep_score: ObservabilityScore


class _Builder:
    def __init__(self, rules: HiddenLiquidityRules | None = None) -> None:
        self.venue = HiddenLiquidityVenue(rules)
        self.commands: list[ObservabilityCommand] = []

    def submit(self, time_us: int, request: HiddenOrderRequest) -> None:
        self.venue.advance_to(time_us)
        self.venue.submit_resting(request)
        self._record(time_us, "SUBMIT", {"request": request.as_dict()})

    def market(
        self,
        time_us: int,
        order_id: str,
        side: Side,
        quantity: int,
        *,
        owner: OrderOwner = OrderOwner.SIMULATED,
        account_id: str = "AGGRESSOR",
    ) -> int:
        self.venue.advance_to(time_us)
        filled = self.venue.execute_market(
            order_id,
            side,
            quantity,
            owner=owner,
            account_id=account_id,
        )
        self._record(
            time_us,
            "MARKET",
            {
                "account_id": account_id,
                "order_id": order_id,
                "owner": owner.value,
                "quantity": quantity,
                "side": side.value,
            },
        )
        return filled

    def cancel(self, time_us: int, order_id: str) -> int:
        self.venue.advance_to(time_us)
        quantity = self.venue.cancel(order_id)
        self._record(time_us, "CANCEL", {"order_id": order_id})
        return quantity

    def refresh(self, time_us: int, order_id: str) -> int:
        self.venue.advance_to(time_us)
        quantity = self.venue.refresh_order(order_id)
        self._record(time_us, "REFRESH", {"order_id": order_id})
        return quantity

    def complete(self, time_us: int) -> None:
        self.venue.advance_to(time_us)
        self.venue.complete_session()
        self._record(time_us, "COMPLETE", {})

    def finish(
        self,
        name: str,
        completed_time_us: int,
        summary: dict[str, object],
    ) -> HiddenLiquidityScenarioResult:
        self.venue.advance_to(completed_time_us)
        self.venue.assert_invariants()
        recording = recording_json_round_trip(
            ObservabilityRecording.capture(
                self.venue,
                tuple(self.commands),
            )
        )
        replay = replay_observability_recording(recording)
        if not replay.passed:
            raise RuntimeError(f"hidden-liquidity scenario replay failed: {name}")
        return HiddenLiquidityScenarioResult(
            name,
            self.venue,
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
            ObservabilityCommand(
                len(self.commands) + 1,
                time_us,
                command_type,
                parameters,
            )
        )


def run_hidden_liquidity_scenario(
    name: str,
) -> HiddenLiquidityScenarioResult:
    runners = {
        "iceberg-absorption": _iceberg_absorption,
        "hidden-midpoint-fill": _hidden_midpoint_fill,
        "repeated-displayed-refresh": _repeated_displayed_refresh,
        "apparent-wall": _apparent_wall,
        "small-displayed-deep-hidden": _small_displayed_deep_hidden,
    }
    try:
        return runners[name]()
    except KeyError as error:
        raise ValueError(f"unknown hidden-liquidity scenario: {name}") from error


def run_all_hidden_liquidity_scenarios(
) -> tuple[HiddenLiquidityScenarioResult, ...]:
    return tuple(
        run_hidden_liquidity_scenario(name)
        for name in HIDDEN_LIQUIDITY_SCENARIOS
    )


def run_blind_hidden_liquidity_exercise() -> BlindExerciseResult:
    shallow_builder = _Builder()
    deep_builder = _Builder()
    for builder in (shallow_builder, deep_builder):
        builder.submit(0, _displayed("BLIND-BID", Side.BUY, 100, 99))
    shallow_builder.submit(
        0,
        _displayed("BLIND-ASK-SHALLOW", Side.SELL, 100, 101),
    )
    deep_builder.submit(
        0,
        _iceberg(
            "BLIND-ASK-DEEP",
            Side.SELL,
            101,
            display=100,
            reserve=400,
            refresh=100,
            visibility=RefreshEventVisibility.QUOTE_UPDATE_ONLY,
        ),
    )
    shallow_initial = shallow_builder.venue.observable_feed().sha256()
    deep_initial = deep_builder.venue.observable_feed().sha256()
    if shallow_initial != deep_initial:
        raise RuntimeError("paired blind exercise leaked hidden state before action")
    shallow_filled = shallow_builder.market(
        100,
        "BLIND-BUY",
        Side.BUY,
        250,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
    )
    deep_filled = deep_builder.market(
        100,
        "BLIND-BUY",
        Side.BUY,
        250,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
    )
    shallow_score = shallow_builder.venue.score_observable_execution(
        target_quantity=250,
        completed_quantity=shallow_filled,
        observable_liquidity_at_decisions=100,
    )
    deep_score = deep_builder.venue.score_observable_execution(
        target_quantity=250,
        completed_quantity=deep_filled,
        observable_liquidity_at_decisions=100,
    )
    shallow_builder.complete(200)
    deep_builder.complete(200)
    shallow = shallow_builder.finish(
        "blind-shallow",
        300,
        {
            "filled_quantity": shallow_filled,
            "initial_observable_sha256": shallow_initial,
            "pre_action_displayed_ask": 100,
            "score": shallow_score.as_dict(),
        },
    )
    deep = deep_builder.finish(
        "blind-deep",
        300,
        {
            "filled_quantity": deep_filled,
            "initial_observable_sha256": deep_initial,
            "pre_action_displayed_ask": 100,
            "score": deep_score.as_dict(),
        },
    )
    return BlindExerciseResult(
        shallow,
        deep,
        shallow_initial,
        shallow_filled,
        deep_filled,
        shallow_score,
        deep_score,
    )


def render_observable_timeline(events: tuple[ObservableEvent, ...]) -> str:
    lines: list[str] = []
    for event in events:
        data = event.data
        details: list[str] = []
        for key in (
            "trade_id",
            "price_ticks",
            "quantity",
            "side",
            "previous_displayed_quantity",
            "new_displayed_quantity",
            "cause_attribution",
        ):
            if key in data:
                details.append(f"{key}={data[key]}")
        suffix = "" if not details else " " + " ".join(details)
        lines.append(
            f"{event.received_time_us:08d}us {event.event_type.value}{suffix}"
        )
    return "\n".join(lines)


def _iceberg_absorption() -> HiddenLiquidityScenarioResult:
    builder = _Builder()
    builder.submit(0, _displayed("ABSORB-BID", Side.BUY, 100, 99))
    builder.submit(
        0,
        _iceberg(
            "ABSORB-ASK",
            Side.SELL,
            101,
            display=100,
            reserve=400,
            refresh=100,
            visibility=RefreshEventVisibility.EXPLICIT_REPLENISHMENT,
        ),
    )
    displayed_before = builder.venue.observable_feed().book.asks[101].total_quantity
    filled = builder.market(
        100,
        "ABSORB-BUY",
        Side.BUY,
        350,
        owner=OrderOwner.PLAYER,
        account_id="PLAYER-1",
    )
    builder.complete(200)
    truth = builder.venue.post_session_ground_truth()
    order = next(item for item in truth.orders if item.order_id == "ABSORB-ASK")
    return builder.finish(
        "iceberg-absorption",
        300,
        {
            "displayed_before": displayed_before,
            "filled_quantity": filled,
            "refresh_count": sum(
                event.event_type.value == "ICEBERG_REFRESHED"
                for event in truth.events
            ),
            "remaining_displayed": order.displayed_quantity,
            "remaining_reserve": order.reserve_quantity,
        },
    )


def _hidden_midpoint_fill() -> HiddenLiquidityScenarioResult:
    builder = _Builder()
    builder.submit(0, _displayed("MID-BID", Side.BUY, 100, 99))
    builder.submit(0, _displayed("MID-ASK", Side.SELL, 100, 102))
    builder.submit(
        0,
        _hidden("MID-HIDDEN-SELL", Side.SELL, 80, kind=LiquidityKind.MIDPOINT_HIDDEN),
    )
    before = builder.venue.observable_feed().book.as_dict()
    filled = builder.market(100, "MID-BUY", Side.BUY, 50)
    after = builder.venue.observable_feed().book.as_dict()
    builder.complete(200)
    tape = builder.venue.observable_feed().tape
    return builder.finish(
        "hidden-midpoint-fill",
        300,
        {
            "displayed_book_unchanged": before == after,
            "filled_quantity": filled,
            "midpoint_price_ticks": str(tape[-1].price_ticks),
        },
    )


def _repeated_displayed_refresh() -> HiddenLiquidityScenarioResult:
    builder = _Builder()
    builder.submit(0, _displayed("REFRESH-BID", Side.BUY, 100, 99))
    builder.submit(
        0,
        _iceberg(
            "REFRESH-ASK",
            Side.SELL,
            101,
            display=50,
            reserve=350,
            refresh=50,
            visibility=RefreshEventVisibility.QUOTE_UPDATE_ONLY,
        ),
    )
    total = 0
    for index, time_us in enumerate((100, 200, 300, 400), start=1):
        total += builder.market(time_us, f"REFRESH-BUY-{index}", Side.BUY, 50)
    builder.complete(500)
    truth = builder.venue.post_session_ground_truth()
    return builder.finish(
        "repeated-displayed-refresh",
        600,
        {
            "filled_quantity": total,
            "quote_only_refresh_count": sum(
                event.event_type.value == "ICEBERG_REFRESHED"
                for event in truth.events
            ),
            "tape_print_count": len(builder.venue.observable_feed().tape),
        },
    )


def _apparent_wall() -> HiddenLiquidityScenarioResult:
    builder = _Builder()
    builder.submit(0, _displayed("WALL-BID", Side.BUY, 100, 99))
    builder.submit(0, _displayed("WALL-ASK", Side.SELL, 500, 101))
    cancelled = builder.cancel(100, "WALL-ASK")
    filled = builder.market(200, "WALL-BUY", Side.BUY, 200)
    builder.complete(300)
    ambiguous = [
        event
        for event in builder.venue.observable_feed().events
        if event.event_type is ObservableEventType.DISPLAY_QUANTITY_CHANGED
        and event.data.get("new_displayed_quantity") == 0
    ]
    return builder.finish(
        "apparent-wall",
        400,
        {
            "cancelled_ground_truth_quantity": cancelled,
            "market_filled_quantity": filled,
            "public_cause_attribution": ambiguous[-1].data["cause_attribution"],
            "public_possible_causes": list(
                ambiguous[-1].data["possible_causes"]  # type: ignore[arg-type]
            ),
        },
    )


def _small_displayed_deep_hidden() -> HiddenLiquidityScenarioResult:
    builder = _Builder()
    builder.submit(0, _displayed("DEEP-BID", Side.BUY, 100, 99))
    builder.submit(0, _displayed("DEEP-ASK", Side.SELL, 50, 101))
    builder.submit(
        0,
        _hidden(
            "DEEP-HIDDEN-ASK",
            Side.SELL,
            450,
            price_ticks=101,
        ),
    )
    displayed_before = builder.venue.observable_feed().book.asks[101].total_quantity
    filled = builder.market(100, "DEEP-BUY", Side.BUY, 300)
    builder.complete(200)
    return builder.finish(
        "small-displayed-deep-hidden",
        300,
        {
            "displayed_before": displayed_before,
            "filled_quantity": filled,
            "hidden_fill_quantity": filled - displayed_before,
            "public_trade_count": len(builder.venue.observable_feed().tape),
        },
    )


def _displayed(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    *,
    owner: OrderOwner = OrderOwner.SIMULATED,
    account_id: str = "SIM",
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.DISPLAYED_LIMIT,
        owner,
        account_id,
        quantity,
        price_ticks,
    )


def _iceberg(
    order_id: str,
    side: Side,
    price_ticks: int,
    *,
    display: int,
    reserve: int,
    refresh: int,
    visibility: RefreshEventVisibility,
    owner: OrderOwner = OrderOwner.SIMULATED,
    account_id: str = "SIM",
) -> HiddenOrderRequest:
    definition = IcebergDefinition(
        display,
        reserve,
        refresh,
        IcebergRefreshBehavior.AUTOMATIC,
        visibility,
    )
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.ICEBERG,
        owner,
        account_id,
        definition.total_quantity,
        price_ticks,
        definition,
    )


def _hidden(
    order_id: str,
    side: Side,
    quantity: int,
    *,
    price_ticks: int | None = None,
    kind: LiquidityKind = LiquidityKind.HIDDEN_LIMIT,
    owner: OrderOwner = OrderOwner.SIMULATED,
    account_id: str = "SIM",
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        kind,
        owner,
        account_id,
        quantity,
        price_ticks,
    )
