"""Deterministic execution measurements and separated training score families."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderType, Side

from .events import EventType, SimulationEvent
from .objectives import ObjectiveType, SessionObjective
from .records import InputRecord, MarketStateRecord, TimelineKind, TimelineRecord

if TYPE_CHECKING:
    from .live import LiveMarketSession


ADVERSE_SELECTION_HORIZON_US = 1_000_000


@dataclass(frozen=True, slots=True)
class FillMeasurement:
    simulation_time_us: int
    order_id: str
    side: Side
    order_type: OrderType
    liquidity: str
    quantity: int
    price_ticks: int
    midpoint_x2: int | None
    queue_wait_us: int | None


@dataclass(frozen=True, slots=True)
class ExecutionProgress:
    target_quantity: int
    completed_quantity: int
    completion_percentage: Decimal
    bought_quantity: int
    sold_quantity: int
    complete: bool
    completion_time_us: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "bought_quantity": self.bought_quantity,
            "complete": self.complete,
            "completed_quantity": self.completed_quantity,
            "completion_percentage": str(self.completion_percentage),
            "completion_time_us": self.completion_time_us,
            "sold_quantity": self.sold_quantity,
            "target_quantity": self.target_quantity,
        }


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    target_quantity: int
    completed_quantity: int
    completion_percentage: Decimal
    average_fill_price_ticks: Decimal | None
    average_buy_fill_price_ticks: Decimal | None
    average_sell_fill_price_ticks: Decimal | None
    arrival_bid_ticks: int | None
    arrival_ask_ticks: int | None
    arrival_mid_ticks: Decimal | None
    spread_paid_ticks: Decimal | None
    slippage_ticks: Decimal | None
    time_to_completion_us: int | None
    completed_within_limit: bool
    market_order_quantity: int
    limit_order_quantity: int
    cancel_count: int
    replace_count: int
    queue_waiting_time_us: Decimal | None
    adverse_selection_after_fill_ticks: Decimal | None
    adverse_selection_horizon_us: int
    implementation_shortfall: Decimal
    implementation_shortfall_unit: str
    missed_available_liquidity: int
    missed_available_liquidity_is_heuristic: bool

    def as_dict(self) -> dict[str, object]:
        values = {
            "adverse_selection_after_fill_ticks": self.adverse_selection_after_fill_ticks,
            "adverse_selection_horizon_us": self.adverse_selection_horizon_us,
            "arrival_ask_ticks": self.arrival_ask_ticks,
            "arrival_bid_ticks": self.arrival_bid_ticks,
            "arrival_mid_ticks": self.arrival_mid_ticks,
            "average_buy_fill_price_ticks": self.average_buy_fill_price_ticks,
            "average_fill_price_ticks": self.average_fill_price_ticks,
            "average_sell_fill_price_ticks": self.average_sell_fill_price_ticks,
            "cancel_count": self.cancel_count,
            "completed_quantity": self.completed_quantity,
            "completed_within_limit": self.completed_within_limit,
            "completion_percentage": self.completion_percentage,
            "implementation_shortfall": self.implementation_shortfall,
            "implementation_shortfall_unit": self.implementation_shortfall_unit,
            "limit_order_quantity": self.limit_order_quantity,
            "market_order_quantity": self.market_order_quantity,
            "missed_available_liquidity": self.missed_available_liquidity,
            "missed_available_liquidity_is_heuristic": (
                self.missed_available_liquidity_is_heuristic
            ),
            "queue_waiting_time_us": self.queue_waiting_time_us,
            "replace_count": self.replace_count,
            "slippage_ticks": self.slippage_ticks,
            "spread_paid_ticks": self.spread_paid_ticks,
            "target_quantity": self.target_quantity,
            "time_to_completion_us": self.time_to_completion_us,
        }
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
        }


@dataclass(frozen=True, slots=True)
class ScoreFamily:
    name: str
    score: Decimal | None
    status: str
    heuristic: bool
    explanation: str
    components: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "components": self.components,
            "explanation": self.explanation,
            "heuristic": self.heuristic,
            "name": self.name,
            "score": None if self.score is None else str(self.score),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class SessionReport:
    objective: SessionObjective
    metrics: ExecutionMetrics
    reading: ScoreFamily
    discipline: ScoreFamily
    execution: ScoreFamily
    state_sha256: str
    timeline_sha256: str
    invariant_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "invariant_status": self.invariant_status,
            "metrics": self.metrics.as_dict(),
            "objective": self.objective.as_dict(),
            "replay_linkage": {
                "state_sha256": self.state_sha256,
                "timeline_sha256": self.timeline_sha256,
            },
            "scores": {
                "DISCIPLINE": self.discipline.as_dict(),
                "EXECUTION": self.execution.as_dict(),
                "READING": self.reading.as_dict(),
                "combined_score": None,
            },
        }

    def render(self) -> str:
        metrics = self.metrics
        average = _display_decimal(metrics.average_fill_price_ticks)
        slippage = _display_decimal(metrics.slippage_ticks)
        spread = _display_decimal(metrics.spread_paid_ticks)
        queue = _display_decimal(metrics.queue_waiting_time_us)
        adverse = _display_decimal(metrics.adverse_selection_after_fill_ticks)
        completion_time = (
            "n/a"
            if metrics.time_to_completion_us is None
            else str(metrics.time_to_completion_us)
        )
        lines = [
            "KIRBY2_TRAINING_REPORT",
            (
                f"OBJECTIVE {self.objective.objective_type.value} "
                f"target={metrics.target_quantity} completed={metrics.completed_quantity} "
                f"completion={_display_decimal(metrics.completion_percentage)}% "
                f"time_to_completion_us={completion_time} "
                f"within_limit={str(metrics.completed_within_limit).lower()}"
            ),
            (
                f"ARRIVAL bid={metrics.arrival_bid_ticks} ask={metrics.arrival_ask_ticks} "
                f"mid={_display_decimal(metrics.arrival_mid_ticks)}"
            ),
            (
                f"FILLS avg={average} spread_paid_ticks={spread} "
                f"slippage_ticks={slippage} market_qty={metrics.market_order_quantity} "
                f"limit_qty={metrics.limit_order_quantity}"
            ),
            (
                f"TACTICS cancels={metrics.cancel_count} replaces={metrics.replace_count} "
                f"avg_queue_wait_us={queue} adverse_1s_ticks={adverse} "
                f"missed_liquidity_heuristic={metrics.missed_available_liquidity}"
            ),
            (
                f"SHORTFALL {_display_decimal(metrics.implementation_shortfall)} "
                f"{metrics.implementation_shortfall_unit}"
            ),
            _score_line(self.reading),
            _score_line(self.discipline),
            _score_line(self.execution),
            "COMBINED_SCORE none",
            (
                f"REPLAY state_sha256={self.state_sha256} "
                f"timeline_sha256={self.timeline_sha256}"
            ),
            f"RUNTIME_INVARIANTS {self.invariant_status}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _OrderMeasurement:
    order_type: OrderType
    submitted_time_us: int


@dataclass(frozen=True, slots=True)
class _MidpointObservation:
    simulation_time_us: int
    midpoint_x2: int | None


class ExecutionTracker:
    """Collects execution facts without consulting strategy classification."""

    def __init__(self, objective: SessionObjective, book: OrderBook) -> None:
        self.objective = objective
        self.arrival_bid_ticks = book.best_bid
        self.arrival_ask_ticks = book.best_ask
        self.arrival_midpoint_x2 = self._midpoint_x2(book)
        self._best_bid = book.best_bid
        self._best_ask = book.best_ask
        self._orders: dict[str, _OrderMeasurement] = {}
        self._fills: list[FillMeasurement] = []
        self._midpoints = [_MidpointObservation(0, self.arrival_midpoint_x2)]
        self._bought_quantity = 0
        self._sold_quantity = 0
        self._round_trip_acquired = 0
        self._round_trip_liquidated = 0
        self._completion_time_us: int | None = (
            0 if objective.objective_type is ObjectiveType.OBSERVE_ONLY else None
        )
        self.cancel_count = 0
        self.replace_count = 0
        self._max_eligible_liquidity = 0
        self._observe_available_liquidity(book)

    @property
    def fills(self) -> tuple[FillMeasurement, ...]:
        return tuple(self._fills)

    def register_order(self, order: Order, simulation_time_us: int) -> None:
        if order.order_id in self._orders:
            raise ValueError(f"duplicate measured player order: {order.order_id}")
        self._orders[order.order_id] = _OrderMeasurement(
            order_type=order.order_type,
            submitted_time_us=simulation_time_us,
        )

    def record_cancel(self, count: int = 1) -> None:
        if count <= 0:
            raise ValueError("cancel count increment must be positive")
        self.cancel_count += count

    def record_replace(self) -> None:
        self.replace_count += 1

    def observe_exchange_activity(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> bool:
        was_complete = self.progress().complete
        all_orders = book.all_orders
        for event in events:
            data = event.data
            if event.event_type in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                order_id = str(data["order_id"])
                order = all_orders.get(order_id)
                if order is not None and order.owner is OrderOwner.PLAYER:
                    self._record_fill(simulation_time_us, event, order)
            elif event.event_type is EventType.BEST_BID_CHANGED:
                value = data["new_price_ticks"]
                self._best_bid = None if value is None else int(value)
            elif event.event_type is EventType.BEST_ASK_CHANGED:
                value = data["new_price_ticks"]
                self._best_ask = None if value is None else int(value)
        self._midpoints.append(
            _MidpointObservation(
                simulation_time_us,
                self._current_midpoint_x2(),
            )
        )
        self._observe_available_liquidity(book)
        return not was_complete and self.progress().complete

    def progress(self) -> ExecutionProgress:
        objective_type = self.objective.objective_type
        if objective_type is ObjectiveType.ACQUIRE:
            completed = min(
                self.objective.target_quantity,
                max(0, self._bought_quantity - self._sold_quantity),
            )
        elif objective_type is ObjectiveType.LIQUIDATE:
            completed = min(
                self.objective.target_quantity,
                max(0, self._sold_quantity - self._bought_quantity),
            )
        elif objective_type is ObjectiveType.ROUND_TRIP:
            completed = min(
                self.objective.target_quantity,
                self._round_trip_liquidated,
            )
        else:
            completed = 0
        percentage = (
            Decimal(100)
            if objective_type is ObjectiveType.OBSERVE_ONLY
            else Decimal(completed) * Decimal(100) / Decimal(self.objective.target_quantity)
        )
        complete = (
            objective_type is ObjectiveType.OBSERVE_ONLY
            or completed >= self.objective.target_quantity
        )
        return ExecutionProgress(
            target_quantity=self.objective.target_quantity,
            completed_quantity=completed,
            completion_percentage=percentage,
            bought_quantity=self._bought_quantity,
            sold_quantity=self._sold_quantity,
            complete=complete,
            completion_time_us=self._completion_time_us,
        )

    def metrics(self, ending_time_us: int) -> ExecutionMetrics:
        progress = self.progress()
        total_quantity = sum(fill.quantity for fill in self._fills)
        buy_fills = [fill for fill in self._fills if fill.side is Side.BUY]
        sell_fills = [fill for fill in self._fills if fill.side is Side.SELL]
        average = self._weighted_price(self._fills)
        average_buy = self._weighted_price(buy_fills)
        average_sell = self._weighted_price(sell_fills)
        spread_paid = self._weighted_signed_midpoint_distance(self._fills)
        slippage = self._weighted_slippage(self._fills)
        queue_fills = [fill for fill in self._fills if fill.queue_wait_us is not None]
        queue_waiting = (
            Decimal(
                sum((fill.queue_wait_us or 0) * fill.quantity for fill in queue_fills)
            )
            / Decimal(sum(fill.quantity for fill in queue_fills))
            if queue_fills
            else None
        )
        adverse = self._adverse_selection(ending_time_us)
        shortfall = self._implementation_shortfall()
        remaining = max(0, self.objective.target_quantity - progress.completed_quantity)
        missed = min(remaining, self._max_eligible_liquidity)
        completed_within_limit = (
            progress.complete
            and progress.completion_time_us is not None
            and progress.completion_time_us <= self.objective.time_limit_us
        )
        if self.objective.objective_type is ObjectiveType.OBSERVE_ONLY:
            completed_within_limit = total_quantity == 0
        return ExecutionMetrics(
            target_quantity=progress.target_quantity,
            completed_quantity=progress.completed_quantity,
            completion_percentage=progress.completion_percentage,
            average_fill_price_ticks=average,
            average_buy_fill_price_ticks=average_buy,
            average_sell_fill_price_ticks=average_sell,
            arrival_bid_ticks=self.arrival_bid_ticks,
            arrival_ask_ticks=self.arrival_ask_ticks,
            arrival_mid_ticks=(
                None
                if self.arrival_midpoint_x2 is None
                else Decimal(self.arrival_midpoint_x2) / Decimal(2)
            ),
            spread_paid_ticks=spread_paid,
            slippage_ticks=slippage,
            time_to_completion_us=progress.completion_time_us,
            completed_within_limit=completed_within_limit,
            market_order_quantity=sum(
                fill.quantity for fill in self._fills if fill.order_type is OrderType.MARKET
            ),
            limit_order_quantity=sum(
                fill.quantity for fill in self._fills if fill.order_type is OrderType.LIMIT
            ),
            cancel_count=self.cancel_count,
            replace_count=self.replace_count,
            queue_waiting_time_us=queue_waiting,
            adverse_selection_after_fill_ticks=adverse,
            adverse_selection_horizon_us=ADVERSE_SELECTION_HORIZON_US,
            implementation_shortfall=shortfall,
            implementation_shortfall_unit=(
                "realized tick-shares versus arrival midpoint; "
                "excludes unfilled opportunity cost"
            ),
            missed_available_liquidity=missed,
            missed_available_liquidity_is_heuristic=True,
        )

    def _record_fill(
        self,
        simulation_time_us: int,
        event: SimulationEvent,
        order: Order,
    ) -> None:
        measurement = self._orders.get(order.order_id)
        if measurement is None:
            raise RuntimeError(f"player fill lacks measured order: {order.order_id}")
        quantity = int(event.data["fill_quantity"])
        liquidity = str(event.data["liquidity"])
        queue_wait = (
            simulation_time_us - measurement.submitted_time_us
            if liquidity == "maker" and measurement.order_type is OrderType.LIMIT
            else None
        )
        fill = FillMeasurement(
            simulation_time_us=simulation_time_us,
            order_id=order.order_id,
            side=order.side,  # type: ignore[arg-type]
            order_type=measurement.order_type,
            liquidity=liquidity,
            quantity=quantity,
            price_ticks=int(event.data["price_ticks"]),
            midpoint_x2=self._current_midpoint_x2(),
            queue_wait_us=queue_wait,
        )
        self._fills.append(fill)
        if fill.side is Side.BUY:
            self._bought_quantity += quantity
            if self.objective.objective_type is ObjectiveType.ROUND_TRIP:
                capacity = self.objective.target_quantity - self._round_trip_acquired
                self._round_trip_acquired += min(quantity, max(0, capacity))
        else:
            self._sold_quantity += quantity
            if self.objective.objective_type is ObjectiveType.ROUND_TRIP:
                available = self._round_trip_acquired - self._round_trip_liquidated
                self._round_trip_liquidated += min(quantity, max(0, available))
        if self._completion_time_us is None and self.progress().complete:
            self._completion_time_us = simulation_time_us

    def _observe_available_liquidity(self, book: OrderBook) -> None:
        objective_type = self.objective.objective_type
        if objective_type is ObjectiveType.OBSERVE_ONLY or self.progress().complete:
            return
        if objective_type is ObjectiveType.ROUND_TRIP:
            acquiring = self._round_trip_acquired < self.objective.target_quantity
        else:
            acquiring = objective_type is ObjectiveType.ACQUIRE
        if acquiring:
            if book.best_ask is None or self.arrival_ask_ticks is None:
                return
            if (
                book.best_ask
                > self.arrival_ask_ticks + self.objective.preferred_slippage_ticks
            ):
                return
            quantity = book.asks[book.best_ask].total_quantity
        else:
            if book.best_bid is None or self.arrival_bid_ticks is None:
                return
            if (
                book.best_bid
                < self.arrival_bid_ticks - self.objective.preferred_slippage_ticks
            ):
                return
            quantity = book.bids[book.best_bid].total_quantity
        self._max_eligible_liquidity = max(self._max_eligible_liquidity, quantity)

    def _current_midpoint_x2(self) -> int | None:
        if self._best_bid is None or self._best_ask is None:
            return None
        return self._best_bid + self._best_ask

    @staticmethod
    def _midpoint_x2(book: OrderBook) -> int | None:
        if book.best_bid is None or book.best_ask is None:
            return None
        return book.best_bid + book.best_ask

    @staticmethod
    def _weighted_price(fills: Iterable[FillMeasurement]) -> Decimal | None:
        captured = tuple(fills)
        quantity = sum(fill.quantity for fill in captured)
        if quantity == 0:
            return None
        return Decimal(
            sum(fill.price_ticks * fill.quantity for fill in captured)
        ) / Decimal(quantity)

    @staticmethod
    def _weighted_signed_midpoint_distance(
        fills: Iterable[FillMeasurement],
    ) -> Decimal | None:
        captured = tuple(fill for fill in fills if fill.midpoint_x2 is not None)
        quantity = sum(fill.quantity for fill in captured)
        if quantity == 0:
            return None
        total_x2 = 0
        for fill in captured:
            midpoint_x2 = fill.midpoint_x2
            if midpoint_x2 is None:
                continue
            distance_x2 = (
                fill.price_ticks * 2 - midpoint_x2
                if fill.side is Side.BUY
                else midpoint_x2 - fill.price_ticks * 2
            )
            total_x2 += distance_x2 * fill.quantity
        return Decimal(total_x2) / Decimal(2 * quantity)

    def _weighted_slippage(
        self,
        fills: Iterable[FillMeasurement],
    ) -> Decimal | None:
        measured: list[tuple[int, int]] = []
        for fill in fills:
            if fill.side is Side.BUY and self.arrival_ask_ticks is not None:
                measured.append((fill.price_ticks - self.arrival_ask_ticks, fill.quantity))
            elif fill.side is Side.SELL and self.arrival_bid_ticks is not None:
                measured.append((self.arrival_bid_ticks - fill.price_ticks, fill.quantity))
        quantity = sum(item[1] for item in measured)
        if quantity == 0:
            return None
        return Decimal(sum(ticks * qty for ticks, qty in measured)) / Decimal(quantity)

    def _adverse_selection(self, ending_time_us: int) -> Decimal | None:
        measured: list[tuple[Decimal, int]] = []
        for fill in self._fills:
            future_midpoint_x2 = self._future_midpoint_x2(
                fill.simulation_time_us + ADVERSE_SELECTION_HORIZON_US,
                ending_time_us,
            )
            if future_midpoint_x2 is None:
                continue
            adverse_x2 = (
                fill.price_ticks * 2 - future_midpoint_x2
                if fill.side is Side.BUY
                else future_midpoint_x2 - fill.price_ticks * 2
            )
            measured.append((Decimal(adverse_x2) / Decimal(2), fill.quantity))
        quantity = sum(item[1] for item in measured)
        if quantity == 0:
            return None
        return sum(value * qty for value, qty in measured) / Decimal(quantity)

    def _future_midpoint_x2(
        self,
        target_time_us: int,
        ending_time_us: int,
    ) -> int | None:
        if target_time_us > ending_time_us:
            return None
        for observation in self._midpoints:
            if (
                observation.simulation_time_us >= target_time_us
                and observation.midpoint_x2 is not None
            ):
                return observation.midpoint_x2
        return next(
            (
                observation.midpoint_x2
                for observation in reversed(self._midpoints)
                if observation.midpoint_x2 is not None
            ),
            None,
        )

    def _implementation_shortfall(self) -> Decimal:
        if self.arrival_midpoint_x2 is None:
            return Decimal(0)
        total_x2 = 0
        for fill in self._fills:
            signed_x2 = (
                fill.price_ticks * 2 - self.arrival_midpoint_x2
                if fill.side is Side.BUY
                else self.arrival_midpoint_x2 - fill.price_ticks * 2
            )
            total_x2 += signed_x2 * fill.quantity
        return Decimal(total_x2) / Decimal(2)


def build_session_report(session: LiveMarketSession) -> SessionReport:
    tracker = session.execution_tracker
    objective = session.objective
    if tracker is None or objective is None:
        raise ValueError("session has no training objective to score")
    session.engine.book.assert_invariants()
    metrics = tracker.metrics(session.simulation_time_us)
    return SessionReport(
        objective=objective,
        metrics=metrics,
        reading=_reading_score(session.timeline),
        discipline=_discipline_score(session.input_records, session.market_states),
        execution=_execution_score(objective, metrics),
        state_sha256=session.state_sha256(),
        timeline_sha256=session.timeline_sha256(),
        invariant_status="PASS",
    )


def _reading_score(records: tuple[TimelineRecord, ...]) -> ScoreFamily:
    transitions = [record for record in records if record.kind is TimelineKind.TRAFFIC]
    if not transitions:
        return ScoreFamily(
            "READING",
            None,
            "UNAVAILABLE",
            True,
            "No traffic-light strategy was attached; no reading score is inferred.",
            {"transition_count": 0},
        )
    supports: list[Decimal] = []
    state_counts = {"GREEN": 0, "WAIT": 0, "RED": 0}
    for transition in transitions:
        evaluation = transition.data["evaluation"]
        if not isinstance(evaluation, dict):
            raise RuntimeError("traffic transition lacks evaluation details")
        state = str(evaluation["state"])
        state_counts[state] += 1
        green = evaluation["green_conditions"]
        wait = evaluation["wait_conditions"]
        if not isinstance(green, list) or not isinstance(wait, list):
            raise RuntimeError("traffic transition conditions must be lists")
        green_match = _condition_match_ratio(green)
        wait_match = _condition_match_ratio(wait)
        if state == "GREEN":
            support = green_match
        elif state == "WAIT":
            support = (wait_match + (Decimal(1) - green_match)) / Decimal(2)
        else:
            support = (
                Decimal(2) - green_match - wait_match
            ) / Decimal(2)
        supports.append(support)
    score = sum(supports) * Decimal(100) / Decimal(len(supports))
    return ScoreFamily(
        "READING",
        score,
        "SCORED",
        True,
        (
            "Heuristic condition-support score: measures how completely observable "
            "conditions supported each recorded classification, not predictive truth."
        ),
        {"state_counts": state_counts, "transition_count": len(transitions)},
    )


def _discipline_score(
    inputs: tuple[InputRecord, ...],
    market_states: tuple[MarketStateRecord, ...],
) -> ScoreFamily:
    state_by_id: dict[str, str] = {}
    for record in market_states:
        traffic = record.snapshot.get("traffic_light")
        if isinstance(traffic, dict) and traffic.get("state") is not None:
            state_by_id[record.state_id] = str(traffic["state"])
    if not state_by_id:
        return ScoreFamily(
            "DISCIPLINE",
            None,
            "UNAVAILABLE",
            True,
            "No traffic-light strategy was attached; discipline is not inferred.",
            {"evaluated_actions": 0},
        )
    risk_commands = {
        "buy_bid",
        "buy_ask",
        "market_buy",
        "sell_ask",
        "sell_bid",
        "market_sell",
        "replace_nearest",
    }
    risk_reducing_commands = {"cancel_nearest", "cancel_all", "flatten"}
    evaluated = 0
    agreed = 0
    violations: list[dict[str, object]] = []
    for record in inputs:
        command = record.resolved_command
        is_accidental = command is None and not record.accepted
        if command not in risk_commands | risk_reducing_commands and not is_accidental:
            continue
        state = state_by_id.get(record.market_state_id)
        if state is None:
            continue
        evaluated += 1
        agreement = command in risk_reducing_commands or (
            record.accepted and command in risk_commands and state == "GREEN"
        )
        if agreement:
            agreed += 1
        else:
            violations.append(
                {
                    "command": command,
                    "input_key": record.input_key,
                    "state": state,
                    "simulation_timestamp": record.simulation_time_us,
                }
            )
    score = (
        Decimal(100)
        if evaluated == 0
        else Decimal(agreed) * Decimal(100) / Decimal(evaluated)
    )
    return ScoreFamily(
        "DISCIPLINE",
        score,
        "SCORED",
        True,
        (
            "Heuristic rule-adherence score: new risk agrees only during GREEN; "
            "cancels and flattening are treated as risk-reducing."
        ),
        {
            "agreed_actions": agreed,
            "evaluated_actions": evaluated,
            "violations": violations,
        },
    )


def _execution_score(
    objective: SessionObjective,
    metrics: ExecutionMetrics,
) -> ScoreFamily:
    if objective.objective_type is ObjectiveType.OBSERVE_ONLY:
        traded = metrics.market_order_quantity + metrics.limit_order_quantity
        score = Decimal(100 if traded == 0 else 0)
        return ScoreFamily(
            "EXECUTION",
            score,
            "SCORED",
            True,
            "Heuristic OBSERVE_ONLY score: any executed player quantity is a violation.",
            {"executed_quantity": traded},
        )
    completion = min(Decimal(100), metrics.completion_percentage)
    if metrics.slippage_ticks is None:
        slippage_component = Decimal(0)
    else:
        excess_slippage = max(
            Decimal(0),
            metrics.slippage_ticks - Decimal(objective.preferred_slippage_ticks),
        )
        slippage_component = max(
            Decimal(0),
            Decimal(100) - excess_slippage * Decimal(25),
        )
    time_component = Decimal(100 if metrics.completed_within_limit else 0)
    missed_component = (
        Decimal(0)
        if metrics.completed_quantity == 0
        else max(
            Decimal(0),
            Decimal(100)
            - Decimal(metrics.missed_available_liquidity)
            * Decimal(100)
            / Decimal(objective.target_quantity),
        )
    )
    score = (
        completion * Decimal("0.55")
        + slippage_component * Decimal("0.20")
        + time_component * Decimal("0.15")
        + missed_component * Decimal("0.10")
    )
    return ScoreFamily(
        "EXECUTION",
        score,
        "SCORED",
        True,
        (
            "Heuristic weighted execution score: 55% completion, 20% preferred "
            "slippage, 15% time limit, and 10% non-duplicative liquidity opportunity."
        ),
        {
            "completion_component": str(completion),
            "missed_liquidity_component": str(missed_component),
            "slippage_component": str(slippage_component),
            "time_component": str(time_component),
        },
    )


def _condition_match_ratio(conditions: list[object]) -> Decimal:
    if not conditions:
        return Decimal(0)
    matched = sum(
        bool(condition.get("matched"))
        for condition in conditions
        if isinstance(condition, dict)
    )
    return Decimal(matched) / Decimal(len(conditions))


def _display_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    rendered = format(value.quantize(Decimal("0.01")), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _score_line(family: ScoreFamily) -> str:
    value = _display_decimal(family.score)
    return f"{family.name} score={value} status={family.status} heuristic=true"


def report_json(report: SessionReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))
