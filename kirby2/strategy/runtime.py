"""Deterministic traffic-light evaluation and transition tracking."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from kirby2.exchange import OrderBook
from kirby2.session.events import SimulationEvent

from .features import FeatureSnapshot, ObservableFeatureTracker
from .language import RuleCondition, StrategyDefinition, TrafficState


@dataclass(frozen=True, slots=True)
class ConditionResult:
    line_number: int
    expression: str
    actual: Decimal | None
    matched: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "actual": None if self.actual is None else str(self.actual),
            "expression": self.expression,
            "line": self.line_number,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    setup_name: str
    simulation_time_us: int
    state: TrafficState
    reason: str
    features: FeatureSnapshot
    green_conditions: tuple[ConditionResult, ...]
    wait_conditions: tuple[ConditionResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "features": self.features.as_dict(),
            "green_conditions": [result.as_dict() for result in self.green_conditions],
            "reason": self.reason,
            "setup_name": self.setup_name,
            "simulation_timestamp": self.simulation_time_us,
            "state": self.state.value,
            "wait_conditions": [result.as_dict() for result in self.wait_conditions],
            "window_us": self.features.window_us,
        }


@dataclass(frozen=True, slots=True)
class TrafficTransition:
    previous_state: TrafficState | None
    evaluation: EvaluationResult

    def as_dict(self) -> dict[str, object]:
        return {
            "current_state": self.evaluation.state.value,
            "evaluation": self.evaluation.as_dict(),
            "previous_state": (
                None if self.previous_state is None else self.previous_state.value
            ),
        }


class TrafficLightRuntime:
    def __init__(
        self,
        definition: StrategyDefinition,
        relative_volume: Decimal,
    ) -> None:
        self.definition = definition
        self.tracker = ObservableFeatureTracker(
            definition.window_us,
            relative_volume,
        )
        self.current: EvaluationResult | None = None

    def reset(self, simulation_time_us: int, book: OrderBook) -> TrafficTransition:
        features = self.tracker.reset(simulation_time_us, book)
        evaluation = self._evaluate(features)
        self.current = evaluation
        return TrafficTransition(None, evaluation)

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: OrderBook,
    ) -> TrafficTransition | None:
        features = self.tracker.observe(simulation_time_us, events, book)
        evaluation = self._evaluate(features)
        previous = self.current
        self.current = evaluation
        if previous is None or previous.state is not evaluation.state:
            return TrafficTransition(
                None if previous is None else previous.state,
                evaluation,
            )
        return None

    def _evaluate(self, features: FeatureSnapshot) -> EvaluationResult:
        green = self._condition_results(self.definition.green_conditions, features)
        wait = self._condition_results(self.definition.wait_conditions, features)
        if all(result.matched for result in green):
            state = TrafficState.GREEN
            reason = f"GREEN: {len(green)}/{len(green)} GREEN conditions matched"
        elif all(result.matched for result in wait):
            state = TrafficState.WAIT
            failed = self._first_failure(green)
            reason = (
                f"WAIT: {len(wait)}/{len(wait)} WAIT conditions matched; "
                f"GREEN failed {failed}"
            )
        else:
            state = TrafficState.RED
            reason = (
                f"RED: GREEN failed {self._first_failure(green)}; "
                f"WAIT failed {self._first_failure(wait)}"
            )
        return EvaluationResult(
            setup_name=self.definition.name,
            simulation_time_us=features.simulation_time_us,
            state=state,
            reason=reason,
            features=features,
            green_conditions=green,
            wait_conditions=wait,
        )

    @staticmethod
    def _condition_results(
        conditions: tuple[RuleCondition, ...],
        features: FeatureSnapshot,
    ) -> tuple[ConditionResult, ...]:
        results: list[ConditionResult] = []
        for condition in conditions:
            actual = features.values[condition.feature]
            matched = (
                actual is not None
                and condition.operator.compare(actual, condition.threshold)
            )
            results.append(
                ConditionResult(
                    line_number=condition.line_number,
                    expression=condition.render(),
                    actual=actual,
                    matched=matched,
                )
            )
        return tuple(results)

    @staticmethod
    def _first_failure(results: tuple[ConditionResult, ...]) -> str:
        failed = next((result for result in results if not result.matched), None)
        if failed is None:
            return "none"
        actual = "unavailable" if failed.actual is None else str(failed.actual)
        return f"at line {failed.line_number} ({failed.expression}; actual={actual})"
