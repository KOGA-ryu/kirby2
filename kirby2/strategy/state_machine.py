"""Restricted deterministic state-machine strategy language and runtime."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable

from kirby2.exchange import OrderBook, OrderOwner, OrderType, Side
from kirby2.session.events import EventType, SimulationEvent

from .features import FeatureSnapshot, ObservableFeatureTracker
from .language import (
    ComparisonOperator,
    FeatureName,
    RuleSyntaxError,
    TrafficState,
    UnavailableValuePolicy,
    _meaningful_lines,
    _parse_unavailable_policy,
    _parse_window,
)


class StrategyPermission(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class PositionFeature(str, Enum):
    POSITION = "position"
    BOUGHT_QUANTITY = "bought_quantity"
    SOLD_QUANTITY = "sold_quantity"
    WORKING_ORDER_COUNT = "working_order_count"


class TimeQualifier(str, Enum):
    INSTANT = "INSTANT"
    TRUE_FOR = "TRUE_FOR"
    EVENTS_WITHIN = "EVENTS_WITHIN"
    OCCURRED_WITHIN = "OCCURRED_WITHIN"
    AFTER_ENTRY = "AFTER_ENTRY"


@dataclass(frozen=True, slots=True)
class StatefulCondition:
    line_number: int
    feature: str
    operator: ComparisonOperator
    threshold: Decimal

    def __post_init__(self) -> None:
        allowed = {feature.value for feature in FeatureName} | {
            feature.value for feature in PositionFeature
        }
        if (
            self.line_number <= 0
            or self.feature not in allowed
            or not self.threshold.is_finite()
        ):
            raise ValueError("stateful condition is invalid")

    def render(self) -> str:
        return f"{self.feature} {self.operator.value} {self.threshold}"


@dataclass(frozen=True, slots=True)
class StrategyStateDefinition:
    name: str
    signal: TrafficState
    entry_permission: StrategyPermission
    exit_permission: StrategyPermission
    cooldown_us: int = 0

    def __post_init__(self) -> None:
        if (
            not _valid_name(self.name)
            or type(self.cooldown_us) is not int
            or self.cooldown_us < 0
        ):
            raise ValueError("strategy state definition is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "cooldown_us": self.cooldown_us,
            "entry_permission": self.entry_permission.value,
            "exit_permission": self.exit_permission.value,
            "name": self.name,
            "signal": self.signal.value,
        }


@dataclass(frozen=True, slots=True)
class StateTransitionDefinition:
    line_number: int
    source_state: str
    target_state: str
    qualifier: TimeQualifier
    conditions: tuple[StatefulCondition, ...] = ()
    duration_us: int = 0
    event_count: int = 0

    def __post_init__(self) -> None:
        if (
            self.line_number <= 0
            or not _valid_name(self.source_state)
            or not _valid_name(self.target_state)
        ):
            raise ValueError("state transition identity is invalid")
        if self.qualifier is TimeQualifier.AFTER_ENTRY:
            if self.conditions or self.duration_us or self.event_count:
                raise ValueError("after-entry transition cannot carry conditions or timing")
        else:
            if not self.conditions:
                raise ValueError("condition-based transition requires conditions")
            if self.qualifier in {
                TimeQualifier.TRUE_FOR,
                TimeQualifier.EVENTS_WITHIN,
                TimeQualifier.OCCURRED_WITHIN,
            } and self.duration_us <= 0:
                raise ValueError("qualified transition requires a positive duration")
            if self.qualifier is TimeQualifier.EVENTS_WITHIN and self.event_count <= 0:
                raise ValueError("event-qualified transition requires a positive count")

    def as_dict(self) -> dict[str, object]:
        return {
            "conditions": [condition.render() for condition in self.conditions],
            "duration_us": self.duration_us,
            "event_count": self.event_count,
            "line": self.line_number,
            "qualifier": self.qualifier.value,
            "source_state": self.source_state,
            "target_state": self.target_state,
        }


@dataclass(frozen=True, slots=True)
class StateMachineDefinition:
    name: str
    window_us: int
    initial_state: str
    states: tuple[StrategyStateDefinition, ...]
    transitions: tuple[StateTransitionDefinition, ...]
    source: str
    unavailable_policy: UnavailableValuePolicy = UnavailableValuePolicy.REFUSE

    def __post_init__(self) -> None:
        if not _valid_name(self.name) or self.window_us <= 0:
            raise ValueError("state-machine identity and window are invalid")
        names = tuple(state.name for state in self.states)
        if not names or len(names) != len(set(names)) or self.initial_state not in names:
            raise ValueError("state-machine states must be unique and include initial state")
        if not self.transitions:
            raise ValueError("state machine requires at least one transition")
        if any(
            transition.source_state not in names or transition.target_state not in names
            for transition in self.transitions
        ):
            raise ValueError("state transition references an unknown state")
        transition_ids = tuple(
            (transition.source_state, transition.target_state, transition.line_number)
            for transition in self.transitions
        )
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("state transitions must be unique")
        if not isinstance(self.unavailable_policy, UnavailableValuePolicy):
            raise TypeError("state-machine unavailable-value policy is invalid")

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def state(self, name: str) -> StrategyStateDefinition:
        for state in self.states:
            if state.name == name:
                return state
        raise KeyError(name)

    def as_dict(self) -> dict[str, object]:
        result = {
            "format": "STATE_MACHINE",
            "initial_state": self.initial_state,
            "name": self.name,
            "source_sha256": self.source_sha256,
            "states": [state.as_dict() for state in self.states],
            "transitions": [transition.as_dict() for transition in self.transitions],
            "window_us": self.window_us,
        }
        if self.unavailable_policy is not UnavailableValuePolicy.REFUSE:
            result["unavailable_policy"] = self.unavailable_policy.value
        return result


@dataclass(frozen=True, slots=True)
class StatefulConditionResult:
    line_number: int
    expression: str
    actual: Decimal | None
    matched: bool
    evidence_simulation_time_us: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "actual": None if self.actual is None else str(self.actual),
            "expression": self.expression,
            "evidence_simulation_timestamp": self.evidence_simulation_time_us,
            "line": self.line_number,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class StateMachineEvaluation:
    setup_name: str
    simulation_time_us: int
    state: TrafficState
    reason: str
    features: FeatureSnapshot
    machine_state: str
    state_entered_us: int
    entry_permission: StrategyPermission
    exit_permission: StrategyPermission
    transition_source: str | None
    transition_qualifier: TimeQualifier | None
    condition_results: tuple[StatefulConditionResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "condition_results": [result.as_dict() for result in self.condition_results],
            "entry_permission": self.entry_permission.value,
            "exit_permission": self.exit_permission.value,
            "features": self.features.as_dict(),
            "kind": "state_machine",
            "machine_state": self.machine_state,
            "reason": self.reason,
            "setup_name": self.setup_name,
            "simulation_timestamp": self.simulation_time_us,
            "state": self.state.value,
            "state_entered_us": self.state_entered_us,
            "transition_qualifier": (
                None
                if self.transition_qualifier is None
                else self.transition_qualifier.value
            ),
            "transition_source": self.transition_source,
            "window_us": self.features.window_us,
        }


@dataclass(frozen=True, slots=True)
class StateMachineTransition:
    previous_state: str | None
    evaluation: StateMachineEvaluation

    def as_dict(self) -> dict[str, object]:
        return {
            "current_machine_state": self.evaluation.machine_state,
            "evaluation": self.evaluation.as_dict(),
            "previous_machine_state": self.previous_state,
        }


@dataclass(frozen=True, slots=True)
class StateMachineBoundaryStep:
    evaluation: StateMachineEvaluation
    transition: StateMachineTransition | None


class StateMachineInvariantViolation(RuntimeError):
    pass


MAX_TRANSITIONS_PER_BOUNDARY = 64


class StateMachineRuntime:
    def __init__(
        self,
        definition: StateMachineDefinition,
        relative_volume: Decimal,
    ) -> None:
        self.definition = definition
        self.tracker = ObservableFeatureTracker(definition.window_us, relative_volume)
        self.current: StateMachineEvaluation | None = None
        self._state_entered_us = 0
        self._true_since: dict[int, int] = {}
        self._matched_events: dict[int, deque[int]] = {}
        self._last_occurred: dict[tuple[str, str, str], int] = {}

    def reset(self, simulation_time_us: int, book: OrderBook) -> StateMachineTransition:
        features = self.tracker.reset(simulation_time_us, book)
        self._state_entered_us = simulation_time_us
        self._last_occurred.clear()
        self._clear_qualifiers()
        evaluation = self._evaluation(
            self.definition.initial_state,
            features,
            "INITIAL state",
            None,
            (),
        )
        self.current = evaluation
        self._prime_true_for(simulation_time_us, features, book)
        return StateMachineTransition(None, evaluation)

    @property
    def next_deadline_us(self) -> int | None:
        if self.current is None:
            return None
        now = self.current.simulation_time_us
        current_name = self.current.machine_state
        current_state = self.definition.state(current_name)
        candidates: list[int] = []
        feature_expiry = self.tracker.next_expiry_time_us
        if feature_expiry is not None and feature_expiry > now:
            candidates.append(feature_expiry)
        cooldown_end = self._state_entered_us + current_state.cooldown_us
        if cooldown_end > now:
            candidates.append(cooldown_end)
        for index, transition in enumerate(self.definition.transitions):
            if transition.source_state != current_name:
                continue
            if transition.qualifier is TimeQualifier.TRUE_FOR:
                started = self._true_since.get(index)
                if started is not None:
                    deadline = started + transition.duration_us
                    if deadline > now:
                        candidates.append(deadline)
            elif transition.qualifier is TimeQualifier.EVENTS_WITHIN:
                matches = self._matched_events.get(index)
                if matches:
                    expiry = matches[0] + transition.duration_us + 1
                    if expiry > now:
                        candidates.append(expiry)
            elif transition.qualifier is TimeQualifier.OCCURRED_WITHIN:
                for condition in transition.conditions:
                    occurred = self._last_occurred.get(_condition_key(condition))
                    if occurred is not None:
                        expiry = occurred + transition.duration_us + 1
                        if expiry > now:
                            candidates.append(expiry)
        return min(candidates) if candidates else None

    def settle(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> tuple[StateMachineBoundaryStep, ...]:
        """Consume one event batch once, then settle zero-time transitions."""

        pending_events = tuple(events)
        steps: list[StateMachineBoundaryStep] = []
        state_path = [
            "UNINITIALIZED" if self.current is None else self.current.machine_state
        ]
        transitions = 0
        while True:
            transition = self.observe(
                simulation_time_us,
                pending_events,
                book,
            )
            pending_events = ()
            if self.current is None:
                raise RuntimeError("state-machine observation omitted its evaluation")
            if transition is None:
                if steps:
                    self.current = steps[-1].evaluation
                    return tuple(steps)
                steps.append(StateMachineBoundaryStep(self.current, None))
                return tuple(steps)
            steps.append(StateMachineBoundaryStep(self.current, transition))
            transitions += 1
            state_path.append(transition.evaluation.machine_state)
            if transitions >= MAX_TRANSITIONS_PER_BOUNDARY:
                raise StateMachineInvariantViolation(
                    "state-machine zero-time transition bound exceeded at "
                    f"{simulation_time_us}: " + " -> ".join(state_path)
                )

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> StateMachineTransition | None:
        captured = tuple(events)
        features = self.tracker.observe(simulation_time_us, captured, book)
        if self.current is None:
            raise RuntimeError("state-machine runtime must be reset before observation")
        self._record_occurrences(simulation_time_us, captured, features, book)
        current_name = self.current.machine_state
        current_state = self.definition.state(current_name)
        evaluated: list[
            tuple[
                StateTransitionDefinition,
                bool,
                tuple[StatefulConditionResult, ...],
            ]
        ] = []
        for index, transition in enumerate(self.definition.transitions):
            if transition.source_state != current_name:
                continue
            matched, results = self._transition_matches(
                index,
                transition,
                simulation_time_us,
                captured,
                features,
                book,
            )
            evaluated.append((transition, matched, results))
        if simulation_time_us < self._state_entered_us + current_state.cooldown_us:
            self.current = self._evaluation(
                current_name,
                features,
                (
                    f"{current_name} cooldown until "
                    f"{self._state_entered_us + current_state.cooldown_us}"
                ),
                None,
                (),
            )
            return None
        for transition, matched, results in evaluated:
            if not matched:
                continue
            previous = current_name
            self._state_entered_us = simulation_time_us
            self._clear_qualifiers()
            reason = (
                f"{transition.source_state} -> {transition.target_state} "
                f"via {transition.qualifier.value} at line {transition.line_number}"
            )
            evaluation = self._evaluation(
                transition.target_state,
                features,
                reason,
                transition,
                results,
            )
            self.current = evaluation
            return StateMachineTransition(previous, evaluation)
        self.current = self._evaluation(
            current_name,
            features,
            f"{current_name} remains active; no outgoing transition matched",
            None,
            (),
        )
        return None

    def entry_allowed(self) -> bool:
        return (
            self.current is not None
            and self.current.entry_permission is StrategyPermission.ALLOW
        )

    def exit_allowed(self) -> bool:
        return (
            self.current is not None
            and self.current.exit_permission is StrategyPermission.ALLOW
        )

    def _transition_matches(
        self,
        index: int,
        transition: StateTransitionDefinition,
        simulation_time_us: int,
        events: tuple[SimulationEvent, ...],
        features: FeatureSnapshot,
        book: OrderBook,
    ) -> tuple[bool, tuple[StatefulConditionResult, ...]]:
        if transition.qualifier is TimeQualifier.AFTER_ENTRY:
            return _contains_player_entry(events, book), ()
        results = tuple(
            _condition_result(condition, features, book)
            for condition in transition.conditions
        )
        conditions_match = all(result.matched for result in results)
        if transition.qualifier is TimeQualifier.INSTANT:
            return conditions_match, results
        if transition.qualifier is TimeQualifier.TRUE_FOR:
            if not conditions_match:
                self._true_since.pop(index, None)
                return False, results
            started = self._true_since.setdefault(index, simulation_time_us)
            return simulation_time_us - started >= transition.duration_us, results
        if transition.qualifier is TimeQualifier.EVENTS_WITHIN:
            matches = self._matched_events.setdefault(index, deque())
            cutoff = simulation_time_us - transition.duration_us
            while matches and matches[0] < cutoff:
                matches.popleft()
            if events and conditions_match:
                matches.append(simulation_time_us)
            return len(matches) >= transition.event_count, results
        occurred_results: list[StatefulConditionResult] = []
        for condition, result in zip(transition.conditions, results, strict=True):
            occurred = self._last_occurred.get(_condition_key(condition))
            matched = (
                occurred is not None
                and simulation_time_us - occurred <= transition.duration_us
            )
            occurred_results.append(
                StatefulConditionResult(
                    result.line_number,
                    result.expression,
                    result.actual,
                    matched,
                    occurred,
                )
            )
        return (
            all(result.matched for result in occurred_results),
            tuple(occurred_results),
        )

    def _record_occurrences(
        self,
        simulation_time_us: int,
        events: tuple[SimulationEvent, ...],
        features: FeatureSnapshot,
        book: OrderBook,
    ) -> None:
        if not events:
            return
        for transition in self.definition.transitions:
            if transition.qualifier is not TimeQualifier.OCCURRED_WITHIN:
                continue
            for condition in transition.conditions:
                if _condition_result(condition, features, book).matched:
                    self._last_occurred[_condition_key(condition)] = simulation_time_us

    def _prime_true_for(
        self,
        simulation_time_us: int,
        features: FeatureSnapshot,
        book: OrderBook,
    ) -> None:
        if self.current is None:
            return
        current_name = self.current.machine_state
        for index, transition in enumerate(self.definition.transitions):
            if (
                transition.source_state == current_name
                and transition.qualifier is TimeQualifier.TRUE_FOR
                and all(
                    _condition_result(condition, features, book).matched
                    for condition in transition.conditions
                )
            ):
                self._true_since[index] = simulation_time_us

    def _evaluation(
        self,
        state_name: str,
        features: FeatureSnapshot,
        reason: str,
        transition: StateTransitionDefinition | None,
        results: tuple[StatefulConditionResult, ...],
    ) -> StateMachineEvaluation:
        state = self.definition.state(state_name)
        return StateMachineEvaluation(
            setup_name=self.definition.name,
            simulation_time_us=features.simulation_time_us,
            state=state.signal,
            reason=reason,
            features=features,
            machine_state=state.name,
            state_entered_us=self._state_entered_us,
            entry_permission=state.entry_permission,
            exit_permission=state.exit_permission,
            transition_source=None if transition is None else transition.source_state,
            transition_qualifier=None if transition is None else transition.qualifier,
            condition_results=results,
        )

    def _clear_qualifiers(self) -> None:
        self._true_since.clear()
        self._matched_events.clear()


def parse_state_machine(source: str) -> StateMachineDefinition:
    if not isinstance(source, str):
        raise TypeError("state-machine source must be text")
    lines = _meaningful_lines(source)
    if not lines:
        raise RuleSyntaxError(1, "strategy file is empty")
    line_number, text = lines[0]
    parts = text.split()
    if (
        len(parts) != 2
        or parts[0].lower() != "machine"
        or not _valid_name(parts[1])
    ):
        raise RuleSyntaxError(line_number, "expected 'machine NAME'", text)
    name = parts[1]
    index = 1
    window_us = 5_000_000
    if index < len(lines) and lines[index][1].lower().startswith("window"):
        window_line, window_text = lines[index]
        window_parts = window_text.split()
        if len(window_parts) != 2 or window_parts[0].lower() != "window":
            raise RuleSyntaxError(window_line, "expected 'window NUMBERs'", window_text)
        window_us = _parse_window(window_parts[1], window_line, window_text)
        index += 1
    unavailable_policy = UnavailableValuePolicy.REFUSE
    if index < len(lines) and lines[index][1].lower().startswith("unavailable"):
        policy_line, policy_text = lines[index]
        unavailable_policy = _parse_unavailable_policy(policy_text, policy_line)
        index += 1
    if index >= len(lines):
        raise RuleSyntaxError(line_number + 1, "expected 'initial STATE'")
    initial_line, initial_text = lines[index]
    initial_parts = initial_text.split()
    if (
        len(initial_parts) != 2
        or initial_parts[0].lower() != "initial"
        or not _valid_name(initial_parts[1])
    ):
        raise RuleSyntaxError(initial_line, "expected 'initial STATE'", initial_text)
    initial_state = initial_parts[1]
    index += 1
    states: list[StrategyStateDefinition] = []
    transitions: list[StateTransitionDefinition] = []
    while index < len(lines):
        current_line, current_text = lines[index]
        if current_text.lower().startswith("state "):
            if transitions:
                raise RuleSyntaxError(
                    current_line,
                    "states must be declared before transitions",
                    current_text,
                )
            states.append(_parse_state(current_line, current_text))
            index += 1
            continue
        if current_text.lower().startswith("transition "):
            transition, index = _parse_transition(lines, index)
            transitions.append(transition)
            continue
        raise RuleSyntaxError(
            current_line,
            "expected state or transition declaration",
            current_text,
        )
    try:
        return StateMachineDefinition(
            name,
            window_us,
            initial_state,
            tuple(states),
            tuple(transitions),
            source,
            unavailable_policy,
        )
    except ValueError as error:
        raise RuleSyntaxError(line_number, str(error), text) from error


def _parse_state(line_number: int, text: str) -> StrategyStateDefinition:
    parts = text.split()
    if len(parts) not in {8, 10} or [value.lower() for value in parts[2:7:2]] != [
        "signal",
        "entry",
        "exit",
    ]:
        raise RuleSyntaxError(
            line_number,
            "state must be 'state NAME signal GREEN|WAIT|RED entry "
            "ALLOW|DENY exit ALLOW|DENY [cooldown TIME]'",
            text,
        )
    if not _valid_name(parts[1]):
        raise RuleSyntaxError(line_number, "invalid state name", text)
    try:
        signal = TrafficState(parts[3].upper())
        entry = StrategyPermission(parts[5].upper())
        exit_permission = StrategyPermission(parts[7].upper())
    except ValueError as error:
        raise RuleSyntaxError(line_number, "invalid state signal or permission", text) from error
    cooldown_us = 0
    if len(parts) == 10:
        if parts[8].lower() != "cooldown":
            raise RuleSyntaxError(line_number, "expected cooldown TIME", text)
        cooldown_us = _parse_window(parts[9], line_number, text)
    return StrategyStateDefinition(parts[1], signal, entry, exit_permission, cooldown_us)


def _parse_transition(
    lines: list[tuple[int, str]],
    index: int,
) -> tuple[StateTransitionDefinition, int]:
    line_number, text = lines[index]
    parts = text.split()
    if len(parts) < 5 or parts[0].lower() != "transition" or parts[2] != "->":
        raise RuleSyntaxError(line_number, "invalid transition header", text)
    source, target = parts[1], parts[3]
    qualifier: TimeQualifier
    duration_us = 0
    event_count = 0
    if [part.lower() for part in parts[4:]] == ["after", "entry"]:
        qualifier = TimeQualifier.AFTER_ENTRY
    elif parts[4].lower() == "when":
        suffix = [part.lower() for part in parts[5:]]
        if not suffix:
            qualifier = TimeQualifier.INSTANT
        elif len(suffix) == 2 and suffix[0] == "for":
            qualifier = TimeQualifier.TRUE_FOR
            duration_us = _parse_window(parts[6], line_number, text)
        elif len(suffix) == 3 and suffix[:2] == ["occurred", "within"]:
            qualifier = TimeQualifier.OCCURRED_WITHIN
            duration_us = _parse_window(parts[7], line_number, text)
        elif len(suffix) == 4 and suffix[0] == "events" and suffix[2] == "within":
            qualifier = TimeQualifier.EVENTS_WITHIN
            try:
                event_count = int(parts[6])
            except ValueError as error:
                raise RuleSyntaxError(
                    line_number,
                    "event count must be an integer",
                    text,
                ) from error
            if event_count <= 0:
                raise RuleSyntaxError(line_number, "event count must be positive", text)
            duration_us = _parse_window(parts[8], line_number, text)
        else:
            raise RuleSyntaxError(line_number, "invalid transition timing qualifier", text)
    else:
        raise RuleSyntaxError(line_number, "transition must use 'when' or 'after entry'", text)
    index += 1
    conditions: list[StatefulCondition] = []
    while index < len(lines) and not lines[index][1].lower().startswith(("state ", "transition ")):
        condition_line, condition_text = lines[index]
        conditions.append(_parse_stateful_condition(condition_line, condition_text))
        index += 1
    try:
        transition = StateTransitionDefinition(
            line_number,
            source,
            target,
            qualifier,
            tuple(conditions),
            duration_us,
            event_count,
        )
    except ValueError as error:
        raise RuleSyntaxError(line_number, str(error), text) from error
    return transition, index


def _parse_stateful_condition(line_number: int, text: str) -> StatefulCondition:
    parts = text.split()
    if len(parts) != 3:
        raise RuleSyntaxError(line_number, "condition must be 'FEATURE OPERATOR NUMBER'", text)
    feature, operator_text, threshold_text = parts
    allowed = {item.value for item in FeatureName} | {item.value for item in PositionFeature}
    if feature not in allowed:
        raise RuleSyntaxError(
            line_number,
            f"unknown observable or position feature '{feature}'",
            text,
        )
    try:
        operator = ComparisonOperator(operator_text)
        threshold = Decimal(threshold_text)
    except (ValueError, InvalidOperation) as error:
        raise RuleSyntaxError(
            line_number,
            "invalid comparison operator or decimal threshold",
            text,
        ) from error
    if not threshold.is_finite():
        raise RuleSyntaxError(line_number, "threshold must be finite", text)
    return StatefulCondition(line_number, feature, operator, threshold)


def _condition_result(
    condition: StatefulCondition,
    features: FeatureSnapshot,
    book: OrderBook,
) -> StatefulConditionResult:
    try:
        market_feature = FeatureName(condition.feature)
    except ValueError:
        position_feature = PositionFeature(condition.feature)
        player = book.player_position
        actual = {
            PositionFeature.POSITION: Decimal(player.position),
            PositionFeature.BOUGHT_QUANTITY: Decimal(player.bought_quantity),
            PositionFeature.SOLD_QUANTITY: Decimal(player.sold_quantity),
            PositionFeature.WORKING_ORDER_COUNT: Decimal(
                sum(
                    order.owner is OrderOwner.PLAYER
                    and order.order_type is OrderType.LIMIT
                    for order in book.active_orders.values()
                )
            ),
        }[position_feature]
    else:
        actual = features.values[market_feature]
    return StatefulConditionResult(
        condition.line_number,
        condition.render(),
        actual,
        actual is not None and condition.operator.compare(actual, condition.threshold),
    )


def _condition_key(condition: StatefulCondition) -> tuple[str, str, str]:
    return (
        condition.feature,
        condition.operator.value,
        str(condition.threshold),
    )


def _contains_player_entry(
    events: tuple[SimulationEvent, ...],
    book: OrderBook,
) -> bool:
    position_before = book.player_position.position
    for event in reversed(events):
        if event.event_type is not EventType.PLAYER_POSITION_CHANGED:
            continue
        quantity = int(event.data["fill_quantity"])
        if event.data["fill_side"] == Side.BUY.value:
            position_before -= quantity
        else:
            position_before += quantity
    for event in events:
        if (
            event.event_type is not EventType.ORDER_SUBMITTED
            or event.data.get("owner") != OrderOwner.PLAYER.value
            or event.data.get("order_type")
            not in {OrderType.LIMIT.value, OrderType.MARKET.value}
        ):
            continue
        side = Side(str(event.data["side"]))
        quantity = int(event.data["original_quantity"])
        if _requires_entry(position_before, side, quantity):
            return True
    return False


def _requires_entry(position: int, side: Side, quantity: int) -> bool:
    if position == 0:
        return True
    same_direction = (position > 0 and side is Side.BUY) or (
        position < 0 and side is Side.SELL
    )
    return same_direction or quantity > abs(position)


def _valid_name(value: str) -> bool:
    return (
        bool(value)
        and value[0].isalpha()
        and all(
            character.isalnum() or character in {"_", "-"}
            for character in value
        )
        and len(value) <= 64
    )
