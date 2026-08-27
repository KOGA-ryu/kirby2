"""Live deterministic session controller consumed by the execution UI."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

from kirby2.exchange import Order, OrderOwner, OrderStatus, Side
from kirby2.scenarios import ScenarioDefinition, create_market_engine
from kirby2.session.events import EventType, SimulationEvent
from kirby2.simulation import (
    LiquidityPreset,
    RegimeOrderFlow,
    ScenarioDimensions,
    VolumePreset,
)
from kirby2.simulation.clock import MICROSECONDS_PER_SECOND
from kirby2.simulation.flow import FlowEvent
from kirby2.strategy import (
    StateMachineDefinition,
    StateMachineBoundaryStep,
    StateMachineEvaluation,
    StateMachineRuntime,
    StateMachineTransition,
    StrategyDefinition,
    TrafficLightRuntime,
    TrafficTransition,
)

from .bindings import BindingMap, SessionCommand
from .objectives import SessionObjective
from .records import InputRecord, MarketStateRecord, TimelineKind, TimelineRecord
from .scoring import ExecutionTracker

if TYPE_CHECKING:
    from kirby2.curriculum import CurriculumDrill


DEFAULT_QUANTITIES = (25, 50, 100, 200, 500, 1_000, 2_000)


@dataclass(frozen=True, slots=True)
class LevelView:
    price_ticks: int
    price: str
    aggregate_quantity: int
    player_quantity: int
    queue_ahead_quantity: int | None


@dataclass(frozen=True, slots=True)
class TapePrint:
    simulation_time_us: int
    trade_id: str
    price_ticks: int
    price: str
    quantity: int
    aggressor_side: Side


@dataclass(frozen=True, slots=True)
class WorkingOrderView:
    order_id: str
    side: Side
    price_ticks: int
    price: str
    remaining_quantity: int
    filled_quantity: int
    queue_ahead_quantity: int


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    scenario_name: str
    regime: str
    seed: int | None
    relative_volume: str
    liquidity: str
    simulation_time_us: int
    duration_us: int
    running: bool
    complete: bool
    selected_quantity: int
    position: int
    bought_quantity: int
    sold_quantity: int
    bids: tuple[LevelView, ...]
    asks: tuple[LevelView, ...]
    tape: tuple[TapePrint, ...]
    working_orders: tuple[WorkingOrderView, ...]
    traffic_light: str
    traffic_setup: str | None
    strategy_state: str | None
    strategy_entry_permission: str
    strategy_exit_permission: str
    traffic_reason: str
    objective_type: str | None
    objective_target_quantity: int
    objective_completed_quantity: int
    objective_completion_percentage: str
    objective_time_limit_us: int | None
    status_message: str
    exchange_event_sequence: int
    market_state_id: str
    market_state_time_us: int


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    command: SessionCommand | None
    accepted: bool
    message: str
    order_ids: tuple[str, ...] = ()
    parameters: dict[str, object] = field(default_factory=dict)


class LiveMarketSession:
    """Deterministic training session with one causal boundary order.

    At a shared simulation timestamp, scheduled market or intraday activity is
    applied first. Emitted exchange events update rolling features next. Strategy
    evaluation and bounded zero-time transitions follow. A clock-only strategy
    deadline is evaluated only if no exchange batch already evaluated that same
    timestamp. Arbitrary caller chunk boundaries update the current snapshot
    silently so advance slicing cannot change the canonical timeline.
    """

    def __init__(
        self,
        definition: ScenarioDefinition,
        seed: int | None = None,
        duration_seconds: int = 300,
        relative_volume: VolumePreset | None = None,
        liquidity: LiquidityPreset | None = None,
        initial_quantity: int = 100,
        quantity_options: tuple[int, ...] = DEFAULT_QUANTITIES,
        strategy_definition: StrategyDefinition | StateMachineDefinition | None = None,
        objective: SessionObjective | None = None,
        curriculum_drill: CurriculumDrill | None = None,
    ) -> None:
        if type(duration_seconds) is not int or duration_seconds <= 0:
            raise ValueError("session duration must be a positive integer")
        if not quantity_options or any(
            type(quantity) is not int or quantity <= 0 for quantity in quantity_options
        ):
            raise ValueError("quantity options must contain positive integers")
        if tuple(sorted(set(quantity_options))) != quantity_options:
            raise ValueError("quantity options must be unique and ascending")
        if initial_quantity not in quantity_options:
            raise ValueError("initial quantity must be one of the quantity options")

        self.definition = definition
        self.seed = definition.seed if seed is None else seed
        self.duration_us = duration_seconds * MICROSECONDS_PER_SECOND
        self.relative_volume = relative_volume
        self.liquidity = liquidity
        self.quantity_options = quantity_options
        self.strategy_definition = strategy_definition
        self.objective = objective
        self.curriculum_drill = curriculum_drill
        self._initial_quantity = initial_quantity
        self._quantity_index = quantity_options.index(initial_quantity)
        self._order_sequence = 0
        self._cancel_sequence = 0
        self._tape: list[TapePrint] = []
        self._seen_trade_ids: set[str] = set()
        self.running = False
        self.complete = False
        self.status_message = "READY - SPACE starts simulated flow"
        self.engine: RegimeOrderFlow
        self.dimensions: ScenarioDimensions
        self._traffic_runtime: TrafficLightRuntime | StateMachineRuntime | None
        self._execution_tracker: ExecutionTracker | None
        if objective is not None and objective.time_limit_us > self.duration_us:
            raise ValueError("objective time limit cannot exceed session duration")
        if curriculum_drill is not None:
            if definition.name != curriculum_drill.scenario_name:
                raise ValueError("curriculum drill scenario does not match session")
            if self.seed != curriculum_drill.scenario_seed:
                raise ValueError("curriculum drill seed does not match session")
            if duration_seconds != curriculum_drill.duration_seconds:
                raise ValueError("curriculum drill duration does not match session")
            if relative_volume is not curriculum_drill.volume:
                raise ValueError("curriculum drill volume does not match session")
            if liquidity is not curriculum_drill.liquidity:
                raise ValueError("curriculum drill liquidity does not match session")
            if objective != curriculum_drill.player_objective:
                raise ValueError("curriculum drill objective does not match session")
        self.reset()

    @property
    def selected_quantity(self) -> int:
        return self.quantity_options[self._quantity_index]

    @property
    def initial_quantity(self) -> int:
        return self._initial_quantity

    @property
    def simulation_time_us(self) -> int:
        return self.engine.clock.current_time_us

    @property
    def input_records(self) -> tuple[InputRecord, ...]:
        return tuple(self._input_records)

    @property
    def market_states(self) -> tuple[MarketStateRecord, ...]:
        return tuple(self._market_states.values())

    @property
    def timeline(self) -> tuple[TimelineRecord, ...]:
        return tuple(self._timeline)

    @property
    def execution_tracker(self) -> ExecutionTracker | None:
        return self._execution_tracker

    def start(self) -> None:
        if self.complete:
            self.status_message = "SESSION COMPLETE - reset to run again"
            return
        self.running = True
        self.status_message = "RUNNING"

    def pause(self) -> None:
        self.running = False
        self.status_message = "PAUSED"

    def reset(self, start: bool = False) -> None:
        self.engine, self.dimensions = create_market_engine(
            self.definition,
            seed=self.seed,
            relative_volume=self.relative_volume,
            liquidity=self.liquidity,
        )
        self.engine.start()
        self._quantity_index = self.quantity_options.index(self._initial_quantity)
        self._order_sequence = 0
        self._cancel_sequence = 0
        self._order_submitted_at: dict[str, int] = {}
        self._tape = []
        self._seen_trade_ids = set()
        self._input_records: list[InputRecord] = []
        self._market_states: dict[str, MarketStateRecord] = {}
        self._timeline: list[TimelineRecord] = []
        self._latest_market_state_time_us = 0
        self._timeline_best_bid = self.engine.book.best_bid
        self._timeline_best_ask = self.engine.book.best_ask
        if self.curriculum_drill is not None:
            self._append_timeline(
                TimelineKind.CURRICULUM,
                (
                    f"CURRICULUM START mode={self.curriculum_drill.mode.value} "
                    f"variation={self.curriculum_drill.variation_id}"
                ),
                {
                    "mode": self.curriculum_drill.mode.value,
                    "variation_id": self.curriculum_drill.variation_id,
                },
                self.simulation_time_us,
            )
        self._execution_tracker = None
        if self.objective is not None:
            self._execution_tracker = ExecutionTracker(
                self.objective,
                self.engine.book,
            )
            self._append_timeline(
                TimelineKind.OBJECTIVE,
                f"OBJECTIVE START: {self.objective.describe()}",
                {
                    "objective": self.objective.as_dict(),
                    "progress": self._execution_tracker.progress().as_dict(),
                },
                self.simulation_time_us,
            )
        self._traffic_runtime = None
        if self.strategy_definition is not None:
            if isinstance(self.strategy_definition, StateMachineDefinition):
                self._traffic_runtime = StateMachineRuntime(
                    self.strategy_definition,
                    Decimal(str(self.dimensions.volume_scale.relative_volume)),
                )
            else:
                self._traffic_runtime = TrafficLightRuntime(
                    self.strategy_definition,
                    Decimal(str(self.dimensions.volume_scale.relative_volume)),
                )
            initial_transition = self._traffic_runtime.reset(
                self.simulation_time_us,
                self.engine.book,
            )
            if isinstance(initial_transition, StateMachineTransition):
                self._record_strategy_evaluation(initial_transition.evaluation)
                self._record_traffic_transition(initial_transition)
                initial_steps = self._traffic_runtime.settle(
                    self.simulation_time_us,
                    (),
                    self.engine.book,
                )
                if any(step.transition is not None for step in initial_steps):
                    self._record_state_machine_steps(initial_steps, True)
                else:
                    self._traffic_runtime.current = initial_transition.evaluation
            else:
                self._record_traffic_transition(initial_transition)
        self.complete = False
        self.running = start
        self.status_message = "RUNNING" if start else "RESET - SPACE starts simulated flow"

    def advance_by(self, delta_us: int) -> tuple[FlowEvent, ...]:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("simulation delta must be a nonnegative integer")
        if not self.running or self.complete or delta_us == 0:
            return ()
        target = min(self.duration_us, self.simulation_time_us + delta_us)
        flow_events: list[FlowEvent] = []
        if self._traffic_runtime is None:
            flow_events.extend(
                self.engine.advance_to(target, on_event=self._capture_flow_trades)
            )
        while self.simulation_time_us < target:
            current_time_us = self.simulation_time_us
            scheduled_time_us = self.engine.next_scheduled_time_us
            strategy_deadline_us = self._strategy_deadline_us()
            candidates = [target]
            if scheduled_time_us is not None:
                if scheduled_time_us <= current_time_us:
                    raise RuntimeError("simulation scheduler failed to advance its arrival")
                candidates.append(scheduled_time_us)
            if strategy_deadline_us is not None:
                if strategy_deadline_us <= current_time_us:
                    raise RuntimeError("strategy scheduler failed to advance its deadline")
                candidates.append(strategy_deadline_us)
            boundary_us = min(candidates)
            is_strategy_deadline = strategy_deadline_us == boundary_us
            emitted = self.engine.advance_to(
                boundary_us,
                on_event=self._capture_flow_trades,
            )
            flow_events.extend(emitted)
            if self._traffic_evaluation_time_us() < boundary_us:
                self._update_traffic(
                    (),
                    boundary_us,
                    record_evaluation=(
                        is_strategy_deadline or boundary_us == self.duration_us
                    ),
                )
        if target == self.duration_us:
            self.running = False
            self.complete = True
            self.status_message = "SESSION COMPLETE"
            if self._execution_tracker is not None:
                self._append_timeline(
                    TimelineKind.OBJECTIVE,
                    "OBJECTIVE SESSION END",
                    {
                        "objective": self.objective.as_dict(),  # type: ignore[union-attr]
                        "progress": self._execution_tracker.progress().as_dict(),
                    },
                    target,
                )
            if self.curriculum_drill is not None:
                self._append_timeline(
                    TimelineKind.CURRICULUM,
                    (
                        f"CURRICULUM COMPLETE lesson={self.curriculum_drill.lesson_id} "
                        f"{self.curriculum_drill.title}"
                    ),
                    {"drill": self.curriculum_drill.as_dict()},
                    target,
                )
        return tuple(flow_events)

    def handle_input(self, key: str, bindings: BindingMap) -> InputRecord:
        if not isinstance(key, str) or not key:
            raise ValueError("input key must be a nonempty string")
        command = bindings.resolve(key)
        if command is SessionCommand.RESET:
            self.reset()
        state = self._capture_market_state()
        input_time_us = self.simulation_time_us
        latency_reference_time_us = self._latest_market_state_time_us
        latency_us = input_time_us - latency_reference_time_us
        if latency_us < 0:
            raise RuntimeError("input latency cannot be negative")
        self._append_timeline(
            TimelineKind.INPUT,
            f"KEY={self._display_key(key)}",
            {
                "input_key": key,
                "market_state_id": state.state_id,
                "resolved_command": command.value if command is not None else None,
            },
            input_time_us,
        )

        if command is None:
            outcome = CommandOutcome(
                command=None,
                accepted=False,
                message=f"UNBOUND KEY {key!r}",
            )
            self.status_message = outcome.message
        elif command is SessionCommand.RESET:
            outcome = self._outcome(command, True, self.status_message)
        else:
            outcome = self.execute(command)

        timeline_kind = TimelineKind.COMMAND if outcome.accepted else TimelineKind.REJECTED
        self._append_timeline(
            timeline_kind,
            outcome.message,
            {
                "accepted": outcome.accepted,
                "command": command.value if command is not None else None,
                "order_ids": list(outcome.order_ids),
                "parameters": outcome.parameters,
            },
            input_time_us,
        )
        record = InputRecord(
            sequence=len(self._input_records) + 1,
            simulation_time_us=input_time_us,
            input_key=key,
            resolved_command=command.value if command is not None else None,
            order_parameters=dict(outcome.parameters),
            market_state_id=state.state_id,
            latency_reference_time_us=latency_reference_time_us,
            action_latency_us=latency_us,
            accepted=outcome.accepted,
            rejection_reason=None if outcome.accepted else outcome.message,
            resulting_order_id=outcome.order_ids[0] if outcome.order_ids else None,
            resulting_order_ids=outcome.order_ids,
        )
        self._input_records.append(record)
        return record

    def execute(self, command: SessionCommand) -> CommandOutcome:
        if self.complete and command not in {SessionCommand.RESET, SessionCommand.QUIT}:
            return self._outcome(
                command,
                False,
                f"{command.value} rejected: session complete",
            )
        if command is SessionCommand.TOGGLE_RUN:
            if self.running:
                self.pause()
            else:
                self.start()
            return self._outcome(command, True, self.status_message)
        if command is SessionCommand.RESET:
            self.reset()
            return self._outcome(command, True, self.status_message)
        if command is SessionCommand.INCREASE_QUANTITY:
            return self._change_quantity(command, 1)
        if command is SessionCommand.DECREASE_QUANTITY:
            return self._change_quantity(command, -1)
        if command is SessionCommand.CANCEL_NEAREST:
            return self._cancel_nearest(command)
        if command is SessionCommand.CANCEL_ALL:
            return self._cancel_all(command)
        if command is SessionCommand.REPLACE_NEAREST:
            return self._replace_nearest(command)
        if command is SessionCommand.FLATTEN:
            return self._flatten(command)
        if command is SessionCommand.BUY_BID:
            return self._submit_limit(command, Side.BUY, self._bid_price())
        if command is SessionCommand.BUY_ASK:
            return self._submit_limit(command, Side.BUY, self.engine.book.best_ask)
        if command is SessionCommand.MARKET_BUY:
            return self._submit_market(command, Side.BUY, self.selected_quantity)
        if command is SessionCommand.SELL_ASK:
            return self._submit_limit(command, Side.SELL, self._ask_price())
        if command is SessionCommand.SELL_BID:
            return self._submit_limit(command, Side.SELL, self.engine.book.best_bid)
        if command is SessionCommand.MARKET_SELL:
            return self._submit_market(command, Side.SELL, self.selected_quantity)
        if command is SessionCommand.QUIT:
            return self._outcome(command, True, "QUIT")
        raise ValueError(f"unsupported session command: {command}")

    def snapshot(self) -> SessionSnapshot:
        book_snapshot = self.engine.book.snapshot()
        player = book_snapshot["player"]
        if not isinstance(player, dict):
            raise RuntimeError("exchange player snapshot must be an object")
        market_state_id = self._market_state_id(book_snapshot)
        traffic = (
            None
            if self._traffic_runtime is None
            else self._traffic_runtime.current
        )
        machine_runtime = (
            self._traffic_runtime
            if isinstance(self._traffic_runtime, StateMachineRuntime)
            else None
        )
        objective_progress = (
            None
            if self._execution_tracker is None
            else self._execution_tracker.progress()
        )
        return SessionSnapshot(
            scenario_name=(
                self.definition.name
                if self.curriculum_drill is None
                else self.curriculum_drill.live_scenario_label
            ),
            regime=(
                self.definition.regime.value
                if self.curriculum_drill is None
                else self.curriculum_drill.live_regime_label
            ),
            seed=self.seed if self.curriculum_drill is None else None,
            relative_volume=(
                self.dimensions.volume.value
                if self.curriculum_drill is None
                else self.curriculum_drill.live_dimension_label
            ),
            liquidity=(
                self.dimensions.liquidity.value
                if self.curriculum_drill is None
                else self.curriculum_drill.live_dimension_label
            ),
            simulation_time_us=self.simulation_time_us,
            duration_us=self.duration_us,
            running=self.running,
            complete=self.complete,
            selected_quantity=self.selected_quantity,
            position=int(player["position"]),
            bought_quantity=int(player["bought_quantity"]),
            sold_quantity=int(player["sold_quantity"]),
            bids=self._level_views(book_snapshot, "bids"),
            asks=self._level_views(book_snapshot, "asks"),
            tape=tuple(self._tape),
            working_orders=self._working_order_views(),
            traffic_light="UNCONFIGURED" if traffic is None else traffic.state.value,
            traffic_setup=None if traffic is None else traffic.setup_name,
            strategy_state=(
                None
                if machine_runtime is None or machine_runtime.current is None
                else machine_runtime.current.machine_state
            ),
            strategy_entry_permission=(
                "UNRESTRICTED"
                if machine_runtime is None or machine_runtime.current is None
                else machine_runtime.current.entry_permission.value
            ),
            strategy_exit_permission=(
                "UNRESTRICTED"
                if machine_runtime is None or machine_runtime.current is None
                else machine_runtime.current.exit_permission.value
            ),
            traffic_reason=(
                "No strategy rule file loaded" if traffic is None else traffic.reason
            ),
            objective_type=(
                None
                if self.objective is None
                else self.objective.objective_type.value
            ),
            objective_target_quantity=(
                0 if objective_progress is None else objective_progress.target_quantity
            ),
            objective_completed_quantity=(
                0 if objective_progress is None else objective_progress.completed_quantity
            ),
            objective_completion_percentage=(
                "0"
                if objective_progress is None
                else str(objective_progress.completion_percentage)
            ),
            objective_time_limit_us=(
                None if self.objective is None else self.objective.time_limit_us
            ),
            status_message=self.status_message,
            exchange_event_sequence=len(self.engine.book.journal.events),
            market_state_id=market_state_id,
            market_state_time_us=self._latest_market_state_time_us,
        )

    def state_sha256(self) -> str:
        payload = {
            "book": self.engine.book.snapshot(),
            "clock_us": self.simulation_time_us,
            "dimensions": self.dimensions.as_dict(),
            "exchange_events": [
                event.as_dict() for event in self.engine.book.journal.events
            ],
            "flow_events": [event.as_dict() for event in self.engine.flow_events],
            "quantity": self.selected_quantity,
            "seed": self.seed,
            "tape": [
                {**asdict(item), "aggressor_side": item.aggressor_side.value}
                for item in self._tape
            ],
        }
        if self._traffic_runtime is not None:
            current = self._traffic_runtime.current
            if current is None:
                raise RuntimeError("configured traffic runtime lacks an evaluation")
            payload["traffic_light"] = {
                "evaluation": current.as_dict(),
                "strategy": self.strategy_definition.as_dict(),  # type: ignore[union-attr]
            }
        if self._execution_tracker is not None:
            payload["training_objective"] = {
                "metrics": self._execution_tracker.metrics(
                    self.simulation_time_us
                ).as_dict(),
                "objective": self.objective.as_dict(),  # type: ignore[union-attr]
            }
        if self.curriculum_drill is not None:
            payload["curriculum_drill"] = self.curriculum_drill.as_dict()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def timeline_sha256(self) -> str:
        canonical = json.dumps(
            [record.as_dict() for record in self._timeline],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _change_quantity(
        self,
        command: SessionCommand,
        direction: int,
    ) -> CommandOutcome:
        previous = self.selected_quantity
        self._quantity_index = max(
            0,
            min(len(self.quantity_options) - 1, self._quantity_index + direction),
        )
        changed = self.selected_quantity != previous
        message = f"QTY {self.selected_quantity}"
        if not changed:
            message += " (limit)"
        return self._outcome(
            command,
            changed,
            message,
            parameters={"new_quantity": self.selected_quantity, "previous_quantity": previous},
        )

    def _submit_limit(
        self,
        command: SessionCommand,
        side: Side,
        price_ticks: int | None,
    ) -> CommandOutcome:
        if price_ticks is None:
            return self._outcome(command, False, f"{command.value} rejected: no touch")
        denied = self._strategy_permission_rejection(side, self.selected_quantity)
        if denied is not None:
            return self._outcome(command, False, f"{command.value} rejected: {denied}")
        order = Order.limit(
            self._next_order_id(),
            side,
            self.selected_quantity,
            price_ticks,
            OrderOwner.PLAYER,
        )
        self._order_submitted_at[order.order_id] = self.simulation_time_us
        if self._execution_tracker is not None:
            self._execution_tracker.register_order(order, self.simulation_time_us)
        exchange_events = self.engine.book.process(order)
        self._consume_exchange_activity(exchange_events, self.simulation_time_us)
        return self._order_outcome(command, order)

    def _submit_market(
        self,
        command: SessionCommand,
        side: Side,
        quantity: int,
    ) -> CommandOutcome:
        denied = self._strategy_permission_rejection(side, quantity)
        if denied is not None:
            return self._outcome(command, False, f"{command.value} rejected: {denied}")
        order = Order.market(
            self._next_order_id(),
            side,
            quantity,
            OrderOwner.PLAYER,
        )
        self._order_submitted_at[order.order_id] = self.simulation_time_us
        if self._execution_tracker is not None:
            self._execution_tracker.register_order(order, self.simulation_time_us)
        exchange_events = self.engine.book.process(order)
        self._consume_exchange_activity(exchange_events, self.simulation_time_us)
        return self._order_outcome(command, order)

    def _order_outcome(
        self,
        command: SessionCommand,
        order: Order,
    ) -> CommandOutcome:
        price = "MKT" if order.price_ticks is None else self._format_price(order.price_ticks)
        details = (
            f"{order.side.value.upper()} {order.original_quantity} @ {price}; "
            f"filled={order.filled_quantity} resting={order.remaining_quantity}"
        )
        if order.status is OrderStatus.EXPIRED and order.filled_quantity < order.original_quantity:
            details += f" expired={order.cancelled_quantity}"
        return self._outcome(
            command,
            True,
            details,
            (order.order_id,),
            {
                "filled_quantity": order.filled_quantity,
                "order_id": order.order_id,
                "order_type": order.order_type.value,
                "price_ticks": order.price_ticks,
                "quantity": order.original_quantity,
                "remaining_quantity": order.remaining_quantity,
                "side": order.side.value if order.side is not None else None,
            },
        )

    def _cancel_nearest(self, command: SessionCommand) -> CommandOutcome:
        active = self._player_orders()
        if not active:
            return self._outcome(command, False, "CXL NEAR rejected: no working orders")
        reference_x2 = self._reference_price_x2()
        target = min(
            active,
            key=lambda order: (
                self._distance_from_reference_x2(order, reference_x2),
                order.resting_sequence or 0,
                order.order_id,
            ),
        )
        cancelled_quantity = target.remaining_quantity
        submitted_at = self._order_submitted_at.get(target.order_id, self.simulation_time_us)
        cancel_latency_us = self.simulation_time_us - submitted_at
        exchange_events = self.engine.book.cancel(target.order_id, self._next_cancel_id())
        if self._execution_tracker is not None:
            self._execution_tracker.record_cancel()
        self._consume_exchange_activity(exchange_events, self.simulation_time_us)
        return self._outcome(
            command,
            True,
            f"CANCELLED {target.order_id} rem={cancelled_quantity}",
            parameters={
                "cancel_latency_us": cancel_latency_us,
                "cancelled_quantity": cancelled_quantity,
                "target_order_id": target.order_id,
            },
        )

    def _cancel_all(self, command: SessionCommand) -> CommandOutcome:
        active = sorted(
            self._player_orders(),
            key=lambda order: (order.resting_sequence or 0, order.order_id),
        )
        if not active:
            return self._outcome(command, False, "CXL ALL rejected: no working orders")
        order_ids: list[str] = []
        cancel_timings: list[dict[str, object]] = []
        for order in active:
            cancelled_quantity = order.remaining_quantity
            submitted_at = self._order_submitted_at.get(
                order.order_id,
                self.simulation_time_us,
            )
            exchange_events = self.engine.book.cancel(
                order.order_id,
                self._next_cancel_id(),
            )
            if self._execution_tracker is not None:
                self._execution_tracker.record_cancel()
            self._consume_exchange_activity(exchange_events, self.simulation_time_us)
            order_ids.append(order.order_id)
            cancel_timings.append(
                {
                    "cancel_latency_us": self.simulation_time_us - submitted_at,
                    "cancelled_quantity": cancelled_quantity,
                    "target_order_id": order.order_id,
                }
            )
        return self._outcome(
            command,
            True,
            f"CANCELLED ALL count={len(order_ids)}",
            parameters={"cancelled_orders": cancel_timings},
        )

    def _replace_nearest(self, command: SessionCommand) -> CommandOutcome:
        active = self._player_orders()
        if not active:
            return self._outcome(command, False, "REPLACE rejected: no working orders")
        reference_x2 = self._reference_price_x2()
        target = min(
            active,
            key=lambda order: (
                self._distance_from_reference_x2(order, reference_x2),
                order.resting_sequence or 0,
                order.order_id,
            ),
        )
        if target.side is None:
            raise RuntimeError("working player order must have a side")
        denied = self._strategy_permission_rejection(
            target.side,
            self.selected_quantity,
        )
        if denied is not None:
            return self._outcome(command, False, f"REPLACE rejected: {denied}")
        price_ticks = self._bid_price() if target.side is Side.BUY else self._ask_price()
        replacement = Order.limit(
            self._next_order_id(),
            target.side,
            self.selected_quantity,
            price_ticks,
            OrderOwner.PLAYER,
        )
        submitted_at = self._order_submitted_at.get(target.order_id, self.simulation_time_us)
        replace_latency_us = self.simulation_time_us - submitted_at
        if self._execution_tracker is not None:
            self._execution_tracker.register_order(
                replacement,
                self.simulation_time_us,
            )
        exchange_events = self.engine.book.replace(
            target.order_id,
            replacement,
            self._next_cancel_id(),
        )
        self._order_submitted_at[replacement.order_id] = self.simulation_time_us
        if self._execution_tracker is not None:
            self._execution_tracker.record_replace()
        self._consume_exchange_activity(exchange_events, self.simulation_time_us)
        return self._outcome(
            command,
            True,
            (
                f"REPLACED {target.order_id} -> {replacement.order_id} "
                f"{replacement.original_quantity} @ {self._format_price(price_ticks)}"
            ),
            (replacement.order_id,),
            {
                "new_order_id": replacement.order_id,
                "old_order_id": target.order_id,
                "price_ticks": price_ticks,
                "quantity": replacement.original_quantity,
                "replace_latency_us": replace_latency_us,
                "side": replacement.side.value,
            },
        )

    def _flatten(self, command: SessionCommand) -> CommandOutcome:
        position = self.engine.book.player_position.position
        if position != 0:
            side = Side.SELL if position > 0 else Side.BUY
            denied = self._strategy_permission_rejection(side, abs(position))
            if denied is not None:
                return self._outcome(command, False, f"FLATTEN rejected: {denied}")
        cancelled: list[dict[str, object]] = []
        if self._player_orders():
            cancel_outcome = self._cancel_all(command)
            raw_cancelled = cancel_outcome.parameters.get("cancelled_orders", [])
            if isinstance(raw_cancelled, list):
                cancelled = [
                    dict(item) for item in raw_cancelled if isinstance(item, dict)
                ]
        position = self.engine.book.player_position.position
        if position == 0:
            if cancelled:
                return self._outcome(
                    command,
                    True,
                    f"FLATTEN cancelled_working={len(cancelled)}; position=0",
                    parameters={
                        "cancelled_working_orders": cancelled,
                        "flatten_from_position": 0,
                        "flatten_to_position": 0,
                    },
                )
            return self._outcome(command, False, "FLATTEN rejected: already flat")
        side = Side.SELL if position > 0 else Side.BUY
        outcome = self._submit_market(command, side, abs(position))
        remaining = self.engine.book.player_position.position
        message = f"{outcome.message}; position={remaining}"
        parameters = dict(outcome.parameters)
        parameters.update(
            {
                "flatten_from_position": position,
                "flatten_to_position": remaining,
                "cancelled_working_orders": cancelled,
            }
        )
        return self._outcome(
            command,
            outcome.accepted,
            message,
            outcome.order_ids,
            parameters,
        )

    def _bid_price(self) -> int:
        if self.engine.book.best_bid is not None:
            return self.engine.book.best_bid
        if self.engine.book.best_ask is not None:
            return self.engine.book.best_ask - 1
        return (
            self.engine.config.initial_mid_ticks
            - self.engine.config.initial_half_spread_ticks
        )

    def _ask_price(self) -> int:
        if self.engine.book.best_ask is not None:
            return self.engine.book.best_ask
        if self.engine.book.best_bid is not None:
            return self.engine.book.best_bid + 1
        return (
            self.engine.config.initial_mid_ticks
            + self.engine.config.initial_half_spread_ticks
        )

    def _reference_price_x2(self) -> int:
        best_bid = self.engine.book.best_bid
        best_ask = self.engine.book.best_ask
        if best_bid is not None and best_ask is not None:
            return best_bid + best_ask
        if best_bid is not None:
            return best_bid * 2
        if best_ask is not None:
            return best_ask * 2
        return self.engine.config.initial_mid_ticks * 2

    @staticmethod
    def _distance_from_reference_x2(order: Order, reference_x2: int) -> int:
        if order.price_ticks is None:
            raise RuntimeError("working player order must have a limit price")
        return abs(order.price_ticks * 2 - reference_x2)

    def _player_orders(self) -> list[Order]:
        return [
            order
            for order in self.engine.book.active_orders.values()
            if order.owner is OrderOwner.PLAYER
        ]

    def _level_views(
        self,
        snapshot: dict[str, object],
        side_key: str,
    ) -> tuple[LevelView, ...]:
        levels = snapshot[side_key]
        if not isinstance(levels, list):
            raise RuntimeError("exchange side snapshot must be a list")
        views: list[LevelView] = []
        for level in levels:
            if not isinstance(level, dict):
                raise RuntimeError("exchange level snapshot must be an object")
            orders = level["orders"]
            if not isinstance(orders, list):
                raise RuntimeError("exchange queue snapshot must be a list")
            player_quantity = 0
            queue_ahead = 0
            first_player_seen = False
            for order in orders:
                if not isinstance(order, dict):
                    raise RuntimeError("exchange order snapshot must be an object")
                remaining = int(order["remaining_quantity"])
                if order["owner"] == OrderOwner.PLAYER.value:
                    player_quantity += remaining
                    first_player_seen = True
                elif not first_player_seen:
                    queue_ahead += remaining
            price_ticks = int(level["price_ticks"])
            views.append(
                LevelView(
                    price_ticks=price_ticks,
                    price=self._format_price(price_ticks),
                    aggregate_quantity=int(level["total_quantity"]),
                    player_quantity=player_quantity,
                    queue_ahead_quantity=queue_ahead if player_quantity else None,
                )
            )
        return tuple(views)

    def _working_order_views(self) -> tuple[WorkingOrderView, ...]:
        views: list[tuple[int, WorkingOrderView]] = []
        for order in self._player_orders():
            if order.side is None or order.price_ticks is None:
                raise RuntimeError("working player order must have side and price")
            level = (
                self.engine.book.bids[order.price_ticks]
                if order.side is Side.BUY
                else self.engine.book.asks[order.price_ticks]
            )
            queue_ahead = 0
            for queued in level.orders:
                if queued is order:
                    break
                queue_ahead += queued.remaining_quantity
            views.append(
                (
                    order.resting_sequence or 0,
                    WorkingOrderView(
                        order_id=order.order_id,
                        side=order.side,
                        price_ticks=order.price_ticks,
                        price=self._format_price(order.price_ticks),
                        remaining_quantity=order.remaining_quantity,
                        filled_quantity=order.filled_quantity,
                        queue_ahead_quantity=queue_ahead,
                    ),
                )
            )
        views.sort(key=lambda item: (item[0], item[1].order_id))
        return tuple(item[1] for item in views)

    def _capture_flow_trades(self, flow_event: FlowEvent) -> None:
        if (
            flow_event.exchange_event_start is None
            or flow_event.exchange_event_end is None
        ):
            return
        journal = self.engine.book.journal.events
        exchange_events = journal[
            flow_event.exchange_event_start - 1 : flow_event.exchange_event_end
        ]
        self._consume_exchange_activity(exchange_events, flow_event.simulation_time_us)

    def _consume_exchange_activity(
        self,
        events: Iterable[SimulationEvent],
        simulation_time_us: int,
    ) -> None:
        captured = tuple(events)
        if not captured:
            return
        self._latest_market_state_time_us = simulation_time_us
        self._capture_exchange_trades(captured, simulation_time_us)
        self._capture_player_fill_status(captured)
        self._capture_timeline_activity(captured, simulation_time_us)
        self._update_traffic(captured, simulation_time_us)
        if self._execution_tracker is not None:
            completed_now = self._execution_tracker.observe_exchange_activity(
                simulation_time_us,
                captured,
                self.engine.book,
            )
            if completed_now:
                progress = self._execution_tracker.progress()
                self._append_timeline(
                    TimelineKind.OBJECTIVE,
                    (
                        f"OBJECTIVE COMPLETE qty={progress.completed_quantity} "
                        f"time_us={progress.completion_time_us}"
                    ),
                    {"progress": progress.as_dict()},
                    simulation_time_us,
                )

    def _capture_exchange_trades(
        self,
        events: Iterable[SimulationEvent],
        simulation_time_us: int,
    ) -> None:
        for event in events:
            if event.event_type is not EventType.TRADE:
                continue
            data = event.data
            trade_id = str(data["trade_id"])
            if trade_id in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(trade_id)
            price_ticks = int(data["price_ticks"])
            self._tape.append(
                TapePrint(
                    simulation_time_us=simulation_time_us,
                    trade_id=trade_id,
                    price_ticks=price_ticks,
                    price=self._format_price(price_ticks),
                    quantity=int(data["quantity"]),
                    aggressor_side=Side(str(data["taker_side"])),
                )
            )

    def _capture_player_fill_status(
        self,
        events: Iterable[SimulationEvent],
    ) -> None:
        for event in events:
            if event.event_type not in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                continue
            order_id = str(event.data["order_id"])
            order = self.engine.book.all_orders.get(order_id)
            if order is None or order.owner is not OrderOwner.PLAYER:
                continue
            label = "FULL FILL" if event.event_type is EventType.FULL_FILL else "PARTIAL FILL"
            self.status_message = (
                f"{label} {order_id} qty={event.data['fill_quantity']} "
                f"@ {self._format_price(int(event.data['price_ticks']))} "
                f"remaining={event.data['remaining_quantity']}"
            )

    def _capture_timeline_activity(
        self,
        events: Iterable[SimulationEvent],
        simulation_time_us: int,
    ) -> None:
        for event in events:
            data = event.data
            if event.event_type in {EventType.PARTIAL_FILL, EventType.FULL_FILL}:
                order_id = str(data["order_id"])
                order = self.engine.book.all_orders.get(order_id)
                if order is None or order.owner is not OrderOwner.PLAYER:
                    continue
                is_full = event.event_type is EventType.FULL_FILL
                kind = TimelineKind.FILL if is_full else TimelineKind.PARTIAL_FILL
                label = "FILL" if is_full else "PARTIAL FILL"
                self._append_timeline(
                    kind,
                    f"{label} {data['fill_quantity']} @ {self._format_price(int(data['price_ticks']))}",
                    dict(data),
                    simulation_time_us,
                )
            elif event.event_type is EventType.PLAYER_POSITION_CHANGED:
                self._append_timeline(
                    TimelineKind.POSITION,
                    f"POSITION {int(data['position']):+d}",
                    dict(data),
                    simulation_time_us,
                )
            elif event.event_type is EventType.ORDER_CANCELLED:
                order_id = str(data["order_id"])
                order = self.engine.book.all_orders.get(order_id)
                if order is not None and order.owner is OrderOwner.PLAYER:
                    self._append_timeline(
                        TimelineKind.CANCEL,
                        f"CANCEL {order_id} qty={data['cancelled_quantity']}",
                        dict(data),
                        simulation_time_us,
                    )
            elif event.event_type is EventType.ORDER_REPLACED:
                new_order_id = str(data["new_order_id"])
                order = self.engine.book.all_orders.get(new_order_id)
                if order is not None and order.owner is OrderOwner.PLAYER:
                    self._append_timeline(
                        TimelineKind.REPLACE,
                        f"REPLACE {data['old_order_id']} -> {new_order_id}",
                        dict(data),
                        simulation_time_us,
                    )
            elif event.event_type in {
                EventType.BEST_BID_CHANGED,
                EventType.BEST_ASK_CHANGED,
            }:
                previous_mid_x2 = self._timeline_midpoint_x2()
                if event.event_type is EventType.BEST_BID_CHANGED:
                    self._timeline_best_bid = self._optional_int(data["new_price_ticks"])
                    side = "BID"
                else:
                    self._timeline_best_ask = self._optional_int(data["new_price_ticks"])
                    side = "ASK"
                new_mid_x2 = self._timeline_midpoint_x2()
                if (
                    previous_mid_x2 is not None
                    and new_mid_x2 is not None
                    and new_mid_x2 != previous_mid_x2
                ):
                    delta_x2 = new_mid_x2 - previous_mid_x2
                    self._append_timeline(
                        TimelineKind.MID,
                        f"MID {self._format_half_ticks(delta_x2)} TICK",
                        {
                            "midpoint_half_ticks": new_mid_x2,
                            "previous_midpoint_half_ticks": previous_mid_x2,
                        },
                        simulation_time_us,
                    )
                elif new_mid_x2 is None:
                    self._append_timeline(
                        TimelineKind.BOOK,
                        f"BEST {side} EMPTY",
                        dict(data),
                        simulation_time_us,
                    )

    def _format_price(self, price_ticks: int) -> str:
        tick_size: Decimal = self.engine.config.tick_size
        return format(tick_size * price_ticks, "f")

    def _next_order_id(self) -> str:
        self._order_sequence += 1
        return f"PLAYER-O-{self._order_sequence:06d}"

    def _next_cancel_id(self) -> str:
        self._cancel_sequence += 1
        return f"PLAYER-C-{self._cancel_sequence:06d}"

    def _capture_market_state(self) -> MarketStateRecord:
        book_snapshot = self.engine.book.snapshot()
        state_id = self._market_state_id(book_snapshot)
        existing = self._market_states.get(state_id)
        if existing is not None:
            return existing
        snapshot = {
            "book": book_snapshot,
            "selected_quantity": self.selected_quantity,
            "working_order_ids": [order.order_id for order in self._player_orders()],
        }
        if self._traffic_runtime is not None:
            current = self._traffic_runtime.current
            if current is None:
                raise RuntimeError("configured traffic runtime lacks an evaluation")
            snapshot["traffic_light"] = current.as_dict()
        if self._execution_tracker is not None:
            snapshot["objective_progress"] = (
                self._execution_tracker.progress().as_dict()
            )
        record = MarketStateRecord(
            state_id=state_id,
            simulation_time_us=self.simulation_time_us,
            observed_state_time_us=self._latest_market_state_time_us,
            exchange_event_sequence=len(self.engine.book.journal.events),
            snapshot=snapshot,
        )
        self._market_states[state_id] = record
        return record

    def _market_state_id(self, book_snapshot: dict[str, object]) -> str:
        payload = {
            "book": book_snapshot,
            "exchange_event_sequence": len(self.engine.book.journal.events),
            "selected_quantity": self.selected_quantity,
            "simulation_time_us": self.simulation_time_us,
        }
        if self._traffic_runtime is not None:
            current = self._traffic_runtime.current
            if current is None:
                raise RuntimeError("configured traffic runtime lacks an evaluation")
            payload["traffic_light"] = current.as_dict()
        if self._execution_tracker is not None:
            payload["objective_progress"] = (
                self._execution_tracker.progress().as_dict()
            )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"MS-{digest[:20]}"

    def _append_timeline(
        self,
        kind: TimelineKind,
        message: str,
        data: dict[str, object],
        simulation_time_us: int,
    ) -> None:
        self._timeline.append(
            TimelineRecord(
                sequence=len(self._timeline) + 1,
                simulation_time_us=simulation_time_us,
                kind=kind,
                message=message,
                data=data,
            )
        )

    def _update_traffic(
        self,
        events: Iterable[SimulationEvent],
        simulation_time_us: int,
        *,
        record_evaluation: bool = True,
    ) -> None:
        if self._traffic_runtime is None:
            return
        if isinstance(self._traffic_runtime, StateMachineRuntime):
            steps = self._traffic_runtime.settle(
                simulation_time_us,
                events,
                self.engine.book,
            )
            self._record_state_machine_steps(steps, record_evaluation)
            return
        transition = self._traffic_runtime.observe(
            simulation_time_us,
            events,
            self.engine.book,
        )
        if transition is not None:
            self._record_traffic_transition(transition)

    def _record_state_machine_steps(
        self,
        steps: tuple[StateMachineBoundaryStep, ...],
        record_evaluation: bool,
    ) -> None:
        for step in steps:
            if record_evaluation or step.transition is not None:
                self._record_strategy_evaluation(step.evaluation)
            if step.transition is not None:
                self._record_traffic_transition(step.transition)

    def _record_strategy_evaluation(
        self,
        evaluation: StateMachineEvaluation,
    ) -> None:
        self._append_timeline(
            TimelineKind.STRATEGY_EVALUATION,
            (
                f"STRATEGY EVALUATION state={evaluation.machine_state} "
                f"signal={evaluation.state.value} [{evaluation.setup_name}]: "
                f"{evaluation.reason}"
            ),
            {
                "evaluation": evaluation.as_dict(),
                "record_type": "STRATEGY_EVALUATION",
            },
            evaluation.simulation_time_us,
        )

    def _strategy_deadline_us(self) -> int | None:
        if self._traffic_runtime is None:
            return None
        return self._traffic_runtime.next_deadline_us

    def _traffic_evaluation_time_us(self) -> int:
        if self._traffic_runtime is None:
            return self.simulation_time_us
        if self._traffic_runtime.current is None:
            raise RuntimeError("configured strategy runtime lacks an evaluation")
        return self._traffic_runtime.current.simulation_time_us

    def _record_traffic_transition(
        self,
        transition: TrafficTransition | StateMachineTransition,
    ) -> None:
        if isinstance(transition, StateMachineTransition):
            evaluation = transition.evaluation
            previous = transition.previous_state or "INITIAL"
            self._append_timeline(
                TimelineKind.TRAFFIC,
                (
                    f"STRATEGY {previous} -> {evaluation.machine_state} "
                    f"signal={evaluation.state.value} "
                    f"entry={evaluation.entry_permission.value} "
                    f"exit={evaluation.exit_permission.value} "
                    f"[{evaluation.setup_name}]: {evaluation.reason}"
                ),
                transition.as_dict(),
                evaluation.simulation_time_us,
            )
            return
        previous = (
            "INITIAL"
            if transition.previous_state is None
            else transition.previous_state.value
        )
        evaluation = transition.evaluation
        self._append_timeline(
            TimelineKind.TRAFFIC,
            (
                f"TRAFFIC {previous} -> {evaluation.state.value} "
                f"[{evaluation.setup_name}]: {evaluation.reason}"
            ),
            transition.as_dict(),
            evaluation.simulation_time_us,
        )

    def _strategy_permission_rejection(
        self,
        side: Side,
        quantity: int,
    ) -> str | None:
        runtime = self._traffic_runtime
        if not isinstance(runtime, StateMachineRuntime):
            return None
        current = runtime.current
        if current is None:
            raise RuntimeError("state-machine strategy lacks a current state")
        position = self.engine.book.player_position.position
        requires_exit = position != 0 and (
            (position > 0 and side is Side.SELL)
            or (position < 0 and side is Side.BUY)
        )
        requires_entry = (
            position == 0
            or not requires_exit
            or quantity > abs(position)
        )
        denied: list[str] = []
        if requires_exit and not runtime.exit_allowed():
            denied.append("exit")
        if requires_entry and not runtime.entry_allowed():
            denied.append("entry")
        if not denied:
            return None
        return (
            f"strategy state {current.machine_state} denies "
            f"{' and '.join(denied)} permission"
        )

    def _timeline_midpoint_x2(self) -> int | None:
        if self._timeline_best_bid is None or self._timeline_best_ask is None:
            return None
        return self._timeline_best_bid + self._timeline_best_ask

    @staticmethod
    def _format_half_ticks(value_x2: int) -> str:
        sign = "+" if value_x2 >= 0 else "-"
        whole, half = divmod(abs(value_x2), 2)
        suffix = ".5" if half else ""
        return f"{sign}{whole}{suffix}"

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return None if value is None else int(value)

    @staticmethod
    def _display_key(key: str) -> str:
        return "SPACE" if key == " " else key

    def _outcome(
        self,
        command: SessionCommand,
        accepted: bool,
        message: str,
        order_ids: tuple[str, ...] = (),
        parameters: dict[str, object] | None = None,
    ) -> CommandOutcome:
        self.status_message = message
        return CommandOutcome(
            command,
            accepted,
            message,
            order_ids,
            {} if parameters is None else parameters,
        )
