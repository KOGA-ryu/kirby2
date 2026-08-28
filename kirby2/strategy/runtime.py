"""Deterministic traffic-light evaluation and transition tracking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from kirby2.features import MarketDepthView
from kirby2.session.events import SimulationEvent

from .features import FeatureSnapshot, ObservableFeatureTracker
from .language import FeatureName, RuleCondition, StrategyDefinition, TrafficState


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

    def reset(self, simulation_time_us: int, book: MarketDepthView) -> TrafficTransition:
        features = self.tracker.reset(simulation_time_us, book)
        evaluation = self._evaluate(features)
        self.current = evaluation
        return TrafficTransition(None, evaluation)

    @property
    def next_deadline_us(self) -> int | None:
        return self.tracker.next_expiry_time_us

    def observe(
        self,
        simulation_time_us: int,
        events: Iterable[SimulationEvent],
        book: MarketDepthView,
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

    def runtime_state(self) -> dict[str, object]:
        return {
            "current": None if self.current is None else self.current.as_dict(),
            "definition": self.definition.as_dict(),
            "feature_windows": self.tracker.runtime_state(),
            "runtime": "TRAFFIC_LIGHT",
        }

    @classmethod
    def from_runtime_state(
        cls,
        definition: StrategyDefinition,
        relative_volume: Decimal,
        payload: Mapping[str, object],
    ) -> TrafficLightRuntime:
        """Restore the recorded decision cut instead of evaluating future state."""

        if not isinstance(payload, Mapping) or set(payload) != {
            "current",
            "definition",
            "feature_windows",
            "runtime",
        }:
            raise ValueError("traffic-light runtime state fields are not exact")
        if payload["runtime"] != "TRAFFIC_LIGHT":
            raise ValueError("traffic-light runtime kind is unsupported")
        if payload["definition"] != definition.as_dict():
            raise ValueError("traffic-light definition differs from configuration")
        runtime = cls(definition, relative_volume)
        from kirby2.features import MicrostructureFeatureEngine

        feature_state = payload["feature_windows"]
        if not isinstance(feature_state, Mapping):
            raise TypeError("traffic-light feature state must be an object")
        runtime.tracker.engine = MicrostructureFeatureEngine.from_runtime_state(
            feature_state
        )
        if runtime.tracker.engine.windows_us != (definition.window_us,):
            raise ValueError("traffic-light feature window differs from its definition")
        raw_current = payload["current"]
        runtime.current = (
            None
            if raw_current is None
            else _evaluation_from_dict(raw_current, definition)
        )
        if runtime.current is not None and (
            not runtime.tracker.engine.runtime_state()["initialized"]
            or runtime.current.simulation_time_us
            != runtime.tracker.engine.runtime_state()["last_time_us"]
        ):
            raise ValueError("traffic-light evaluation differs from feature cutoff")
        if runtime.runtime_state() != dict(payload):
            raise ValueError("traffic-light runtime state is not a canonical fixed point")
        return runtime

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


def _feature_snapshot_from_dict(
    payload: object,
    *,
    expected_window_us: int,
    simulation_time_us: int,
) -> FeatureSnapshot:
    if not isinstance(payload, Mapping) or set(payload) != {
        feature.value for feature in FeatureName
    }:
        raise ValueError("traffic-light feature snapshot fields are not exact")
    values: dict[FeatureName, Decimal | None] = {}
    for feature in FeatureName:
        value = payload[feature.value]
        if value is not None and type(value) is not str:
            raise TypeError("traffic-light feature values must be decimal text or null")
        try:
            decoded = None if value is None else Decimal(value)
        except InvalidOperation as error:
            raise ValueError("traffic-light feature value is invalid") from error
        if decoded is not None and not decoded.is_finite():
            raise ValueError("traffic-light feature value must be finite")
        values[feature] = decoded
    return FeatureSnapshot(simulation_time_us, expected_window_us, values)


def _condition_result_from_dict(payload: object) -> ConditionResult:
    if not isinstance(payload, Mapping) or set(payload) != {
        "actual",
        "expression",
        "line",
        "matched",
    }:
        raise ValueError("traffic-light condition result fields are not exact")
    line = payload["line"]
    expression = payload["expression"]
    actual = payload["actual"]
    matched = payload["matched"]
    if type(line) is not int or line <= 0:
        raise ValueError("traffic-light condition line must be positive")
    if type(expression) is not str or not expression:
        raise ValueError("traffic-light condition expression must be nonempty")
    if actual is not None and type(actual) is not str:
        raise TypeError("traffic-light condition actual must be decimal text or null")
    if type(matched) is not bool:
        raise TypeError("traffic-light condition match flag must be boolean")
    try:
        decoded = None if actual is None else Decimal(actual)
    except InvalidOperation as error:
        raise ValueError("traffic-light condition actual is invalid") from error
    if decoded is not None and not decoded.is_finite():
        raise ValueError("traffic-light condition actual must be finite")
    return ConditionResult(line, expression, decoded, matched)


def _evaluation_from_dict(
    payload: object,
    definition: StrategyDefinition,
) -> EvaluationResult:
    if not isinstance(payload, Mapping) or set(payload) != {
        "features",
        "green_conditions",
        "reason",
        "setup_name",
        "simulation_timestamp",
        "state",
        "wait_conditions",
        "window_us",
    }:
        raise ValueError("traffic-light evaluation fields are not exact")
    simulation_time_us = payload["simulation_timestamp"]
    window_us = payload["window_us"]
    if type(simulation_time_us) is not int or simulation_time_us < 0:
        raise ValueError("traffic-light evaluation time must be nonnegative")
    if window_us != definition.window_us:
        raise ValueError("traffic-light evaluation window differs from definition")
    if payload["setup_name"] != definition.name:
        raise ValueError("traffic-light evaluation setup differs from definition")
    if type(payload["reason"]) is not str or not payload["reason"]:
        raise ValueError("traffic-light evaluation reason must be nonempty")
    raw_green = payload["green_conditions"]
    raw_wait = payload["wait_conditions"]
    if type(raw_green) is not list or type(raw_wait) is not list:
        raise TypeError("traffic-light condition results must be arrays")
    features = _feature_snapshot_from_dict(
        payload["features"],
        expected_window_us=definition.window_us,
        simulation_time_us=simulation_time_us,
    )
    evaluation = EvaluationResult(
        setup_name=definition.name,
        simulation_time_us=simulation_time_us,
        state=TrafficState(str(payload["state"])),
        reason=str(payload["reason"]),
        features=features,
        green_conditions=tuple(
            _condition_result_from_dict(row) for row in raw_green
        ),
        wait_conditions=tuple(
            _condition_result_from_dict(row) for row in raw_wait
        ),
    )
    expected = TrafficLightRuntime(definition, Decimal(1))._evaluate(features)
    if evaluation.as_dict() != expected.as_dict():
        raise ValueError("traffic-light evaluation differs from observable features")
    if evaluation.as_dict() != dict(payload):
        raise ValueError("traffic-light evaluation is not canonical")
    return evaluation
