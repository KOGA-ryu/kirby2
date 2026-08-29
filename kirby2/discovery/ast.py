"""Typed canonical syntax trees for the existing Kirby2 strategy language."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TypeAlias

from kirby2.strategy.language import (
    ComparisonOperator,
    FeatureName,
    StrategyDefinition,
    TrafficState,
    UnavailableValuePolicy,
    parse_strategy,
)
from kirby2.strategy.state_machine import (
    PositionFeature,
    StateMachineDefinition,
    StrategyPermission,
    TimeQualifier,
)


STRATEGY_AST_SCHEMA_VERSION_V1 = 1
STRATEGY_AST_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_AST_V1"
MAX_EXACT_DECIMAL_DIGITS_V1 = 128
MAX_EXACT_DECIMAL_SCALE_V1 = 128
MAX_STRATEGY_DURATION_US_V1 = (1 << 63) - 1
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MARKET_FEATURES = frozenset(feature.value for feature in FeatureName)
_STATEFUL_FEATURES = _MARKET_FEATURES | frozenset(
    feature.value for feature in PositionFeature
)


class StrategyAstKindV1(str, Enum):
    TRAFFIC_LIGHT = "TRAFFIC_LIGHT"
    STATE_MACHINE = "STATE_MACHINE"


@dataclass(frozen=True, slots=True, order=True)
class ExactDecimalV1:
    """A finite decimal represented as a reduced fixed-point integer pair."""

    coefficient: int
    scale: int

    def __post_init__(self) -> None:
        if type(self.coefficient) is not int or type(self.scale) is not int:
            raise TypeError("exact decimal coefficient and scale must be integers")
        if self.scale < 0:
            raise ValueError("exact decimal scale must be nonnegative")
        if self.scale > MAX_EXACT_DECIMAL_SCALE_V1:
            raise ValueError("exact decimal exceeds the canonical scale bound")
        if abs(self.coefficient) >= 10**MAX_EXACT_DECIMAL_DIGITS_V1:
            raise ValueError("exact decimal exceeds the canonical digit bound")
        if self.coefficient == 0 and self.scale != 0:
            raise ValueError("canonical zero must have scale zero")
        if self.scale and self.coefficient % 10 == 0:
            raise ValueError("exact decimal must not contain trailing fractional zeroes")

    @classmethod
    def from_decimal(cls, value: Decimal) -> ExactDecimalV1:
        if not isinstance(value, Decimal):
            raise TypeError("exact decimal input must be Decimal")
        if not value.is_finite():
            raise ValueError("exact decimal input must be finite")
        parts = value.as_tuple()
        if len(parts.digits) > MAX_EXACT_DECIMAL_DIGITS_V1:
            raise ValueError("exact decimal exceeds the canonical digit bound")
        coefficient = 0
        for digit in parts.digits:
            coefficient = coefficient * 10 + digit
        if coefficient == 0:
            return cls(0, 0)
        exponent = parts.exponent
        if exponent >= 0:
            if len(parts.digits) + exponent > MAX_EXACT_DECIMAL_DIGITS_V1:
                raise ValueError("exact decimal exceeds the canonical digit bound")
            coefficient *= 10**exponent
            scale = 0
        else:
            scale = -exponent
            while scale and coefficient % 10 == 0:
                coefficient //= 10
                scale -= 1
            if scale > MAX_EXACT_DECIMAL_SCALE_V1:
                raise ValueError("exact decimal exceeds the canonical scale bound")
        if parts.sign:
            coefficient = -coefficient
        return cls(coefficient, scale)

    def as_decimal(self) -> Decimal:
        return Decimal(self.coefficient).scaleb(-self.scale)

    def render(self) -> str:
        if self.scale == 0:
            return str(self.coefficient)
        digits = str(abs(self.coefficient)).rjust(self.scale + 1, "0")
        rendered = f"{digits[:-self.scale]}.{digits[-self.scale:]}"
        return f"-{rendered}" if self.coefficient < 0 else rendered

    def as_dict(self) -> dict[str, int]:
        return {"coefficient": self.coefficient, "scale": self.scale}


@dataclass(frozen=True, slots=True)
class ComparisonNodeV1:
    feature: str
    operator: ComparisonOperator
    threshold: ExactDecimalV1

    def __post_init__(self) -> None:
        if type(self.feature) is not str or self.feature not in _STATEFUL_FEATURES:
            raise ValueError("comparison feature is outside the strategy grammar")
        if not isinstance(self.operator, ComparisonOperator):
            raise TypeError("comparison operator is invalid")
        if not isinstance(self.threshold, ExactDecimalV1):
            raise TypeError("comparison threshold must be ExactDecimalV1")

    @property
    def sort_key(self) -> tuple[str, str, int, int]:
        return (
            self.feature,
            self.operator.value,
            self.threshold.coefficient,
            self.threshold.scale,
        )

    def render(self) -> str:
        return f"{self.feature} {self.operator.value} {self.threshold.render()}"

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "operator": self.operator.value,
            "threshold": self.threshold.as_dict(),
        }


def _canonical_conditions(
    values: tuple[ComparisonNodeV1, ...],
) -> tuple[ComparisonNodeV1, ...]:
    if type(values) is not tuple or any(
        not isinstance(value, ComparisonNodeV1) for value in values
    ):
        raise TypeError("strategy conditions must be a tuple of ComparisonNodeV1")
    return tuple(sorted(set(values), key=lambda value: value.sort_key))


@dataclass(frozen=True, slots=True)
class TrafficLightStrategyAstV1:
    name: str
    window_us: int
    green_conditions: tuple[ComparisonNodeV1, ...]
    wait_conditions: tuple[ComparisonNodeV1, ...]
    unavailable_policy: UnavailableValuePolicy = UnavailableValuePolicy.REFUSE

    def __post_init__(self) -> None:
        if type(self.name) is not str or _NAME.fullmatch(self.name) is None:
            raise ValueError("traffic-light AST name is invalid")
        _require_renderable_duration(self.window_us, "traffic-light window")
        if not isinstance(self.unavailable_policy, UnavailableValuePolicy):
            raise TypeError("traffic-light unavailable policy is invalid")
        green = _canonical_conditions(self.green_conditions)
        wait = _canonical_conditions(self.wait_conditions)
        if not green or not wait:
            raise ValueError("traffic-light AST requires GREEN and WAIT conditions")
        if any(condition.feature not in _MARKET_FEATURES for condition in green + wait):
            raise ValueError("traffic-light AST cannot use position-only features")
        object.__setattr__(self, "green_conditions", green)
        object.__setattr__(self, "wait_conditions", wait)

    @property
    def kind(self) -> StrategyAstKindV1:
        return StrategyAstKindV1.TRAFFIC_LIGHT

    def semantic_projection(self) -> dict[str, object]:
        return {
            "green_conditions": [value.as_dict() for value in self.green_conditions],
            "kind": self.kind.value,
            "name": self.name,
            "schema_id": STRATEGY_AST_SCHEMA_ID_V1,
            "schema_version": STRATEGY_AST_SCHEMA_VERSION_V1,
            "unavailable_policy": self.unavailable_policy.value,
            "wait_conditions": [value.as_dict() for value in self.wait_conditions],
            "window_us": self.window_us,
        }


@dataclass(frozen=True, slots=True)
class StateNodeV1:
    name: str
    signal: TrafficState
    entry_permission: StrategyPermission
    exit_permission: StrategyPermission
    cooldown_us: int = 0

    def __post_init__(self) -> None:
        if type(self.name) is not str or _NAME.fullmatch(self.name) is None:
            raise ValueError("strategy state AST name is invalid")
        if not isinstance(self.signal, TrafficState):
            raise TypeError("strategy state signal is invalid")
        if not isinstance(self.entry_permission, StrategyPermission) or not isinstance(
            self.exit_permission, StrategyPermission
        ):
            raise TypeError("strategy state permission is invalid")
        _require_renderable_duration(
            self.cooldown_us,
            "strategy state cooldown",
            allow_zero=True,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "cooldown_us": self.cooldown_us,
            "entry_permission": self.entry_permission.value,
            "exit_permission": self.exit_permission.value,
            "name": self.name,
            "signal": self.signal.value,
        }


@dataclass(frozen=True, slots=True)
class TransitionNodeV1:
    source_state: str
    target_state: str
    qualifier: TimeQualifier
    conditions: tuple[ComparisonNodeV1, ...] = ()
    duration_us: int = 0
    event_count: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.source_state) is not str
            or _NAME.fullmatch(self.source_state) is None
            or type(self.target_state) is not str
            or _NAME.fullmatch(self.target_state) is None
        ):
            raise ValueError("strategy transition endpoints are invalid")
        if not isinstance(self.qualifier, TimeQualifier):
            raise TypeError("strategy transition qualifier is invalid")
        if type(self.duration_us) is not int or self.duration_us < 0:
            raise ValueError("strategy transition duration is invalid")
        if type(self.event_count) is not int or self.event_count < 0:
            raise ValueError("strategy transition event count is invalid")
        conditions = _canonical_conditions(self.conditions)
        if self.qualifier is TimeQualifier.AFTER_ENTRY:
            if conditions or self.duration_us or self.event_count:
                raise ValueError("after-entry transition cannot carry conditions or timing")
        else:
            if not conditions:
                raise ValueError("condition transition requires at least one condition")
            if self.qualifier is TimeQualifier.INSTANT:
                if self.duration_us or self.event_count:
                    raise ValueError("instant transition cannot carry timing")
            else:
                _require_renderable_duration(
                    self.duration_us,
                    "qualified transition duration",
                )
                if (
                    self.qualifier is TimeQualifier.EVENTS_WITHIN
                    and self.event_count <= 0
                ):
                    raise ValueError("event-window transition requires a positive count")
                if (
                    self.qualifier is not TimeQualifier.EVENTS_WITHIN
                    and self.event_count
                ):
                    raise ValueError("non-event transition cannot carry an event count")
        object.__setattr__(self, "conditions", conditions)

    def as_dict(self) -> dict[str, object]:
        return {
            "conditions": [value.as_dict() for value in self.conditions],
            "duration_us": self.duration_us,
            "event_count": self.event_count,
            "qualifier": self.qualifier.value,
            "source_state": self.source_state,
            "target_state": self.target_state,
        }


@dataclass(frozen=True, slots=True)
class StateMachineStrategyAstV1:
    name: str
    window_us: int
    initial_state: str
    states: tuple[StateNodeV1, ...]
    transitions: tuple[TransitionNodeV1, ...]
    unavailable_policy: UnavailableValuePolicy = UnavailableValuePolicy.REFUSE

    def __post_init__(self) -> None:
        if type(self.name) is not str or _NAME.fullmatch(self.name) is None:
            raise ValueError("state-machine AST name is invalid")
        _require_renderable_duration(self.window_us, "state-machine window")
        if not isinstance(self.unavailable_policy, UnavailableValuePolicy):
            raise TypeError("state-machine unavailable policy is invalid")
        if (
            type(self.initial_state) is not str
            or _NAME.fullmatch(self.initial_state) is None
        ):
            raise ValueError("state-machine AST initial state is invalid")
        if type(self.states) is not tuple or any(
            not isinstance(state, StateNodeV1) for state in self.states
        ):
            raise TypeError("state-machine states must be a tuple of StateNodeV1")
        if type(self.transitions) is not tuple or any(
            not isinstance(transition, TransitionNodeV1)
            for transition in self.transitions
        ):
            raise TypeError(
                "state-machine transitions must be a tuple of TransitionNodeV1"
            )
        states = tuple(sorted(self.states, key=lambda state: state.name))
        names = tuple(state.name for state in states)
        if not states or len(names) != len(set(names)):
            raise ValueError("state-machine AST states must be nonempty and unique")
        if self.initial_state not in names:
            raise ValueError("state-machine AST initial state is unknown")
        if not self.transitions:
            raise ValueError("state-machine AST requires at least one transition")
        if any(
            transition.source_state not in names or transition.target_state not in names
            for transition in self.transitions
        ):
            raise ValueError("state-machine AST transition references an unknown state")
        object.__setattr__(self, "states", states)

    @property
    def kind(self) -> StrategyAstKindV1:
        return StrategyAstKindV1.STATE_MACHINE

    def semantic_projection(self) -> dict[str, object]:
        return {
            "initial_state": self.initial_state,
            "kind": self.kind.value,
            "name": self.name,
            "schema_id": STRATEGY_AST_SCHEMA_ID_V1,
            "schema_version": STRATEGY_AST_SCHEMA_VERSION_V1,
            "states": [state.as_dict() for state in self.states],
            # Transition position is priority and therefore remains semantic.
            "transitions": [value.as_dict() for value in self.transitions],
            "unavailable_policy": self.unavailable_policy.value,
            "window_us": self.window_us,
        }


StrategyAstV1: TypeAlias = TrafficLightStrategyAstV1 | StateMachineStrategyAstV1


def parse_strategy_ast(source: str) -> StrategyAstV1:
    """Parse the existing strict grammar and discard presentation-only coordinates."""

    return strategy_ast_from_definition(parse_strategy(source))


def strategy_ast_from_definition(
    definition: StrategyDefinition | StateMachineDefinition,
) -> StrategyAstV1:
    if isinstance(definition, StrategyDefinition):
        return TrafficLightStrategyAstV1(
            name=definition.name,
            window_us=definition.window_us,
            green_conditions=tuple(
                _comparison(condition.feature.value, condition.operator, condition.threshold)
                for condition in definition.green_conditions
            ),
            wait_conditions=tuple(
                _comparison(condition.feature.value, condition.operator, condition.threshold)
                for condition in definition.wait_conditions
            ),
            unavailable_policy=definition.unavailable_policy,
        )
    if isinstance(definition, StateMachineDefinition):
        return StateMachineStrategyAstV1(
            name=definition.name,
            window_us=definition.window_us,
            initial_state=definition.initial_state,
            states=tuple(
                StateNodeV1(
                    name=state.name,
                    signal=state.signal,
                    entry_permission=state.entry_permission,
                    exit_permission=state.exit_permission,
                    cooldown_us=state.cooldown_us,
                )
                for state in definition.states
            ),
            transitions=tuple(
                TransitionNodeV1(
                    source_state=transition.source_state,
                    target_state=transition.target_state,
                    qualifier=transition.qualifier,
                    conditions=tuple(
                        _comparison(
                            condition.feature,
                            condition.operator,
                            condition.threshold,
                        )
                        for condition in transition.conditions
                    ),
                    duration_us=transition.duration_us,
                    event_count=transition.event_count,
                )
                for transition in definition.transitions
            ),
            unavailable_policy=definition.unavailable_policy,
        )
    raise TypeError("strategy definition kind is unsupported")


def render_canonical_strategy_ast(ast: StrategyAstV1) -> str:
    if isinstance(ast, TrafficLightStrategyAstV1):
        lines = [
            f"setup {ast.name}",
            f"window {_render_duration(ast.window_us)}",
            f"unavailable {ast.unavailable_policy.value}",
            "GREEN when",
            *(f"    {condition.render()}" for condition in ast.green_conditions),
            "WAIT when",
            *(f"    {condition.render()}" for condition in ast.wait_conditions),
            "RED otherwise",
        ]
        return "\n".join(lines) + "\n"
    if isinstance(ast, StateMachineStrategyAstV1):
        lines = [
            f"machine {ast.name}",
            f"window {_render_duration(ast.window_us)}",
            f"unavailable {ast.unavailable_policy.value}",
            f"initial {ast.initial_state}",
        ]
        for state in ast.states:
            line = (
                f"state {state.name} signal {state.signal.value} "
                f"entry {state.entry_permission.value} "
                f"exit {state.exit_permission.value}"
            )
            if state.cooldown_us:
                line += f" cooldown {_render_duration(state.cooldown_us)}"
            lines.append(line)
        for transition in ast.transitions:
            lines.append(_render_transition_header(transition))
            lines.extend(f"    {condition.render()}" for condition in transition.conditions)
        return "\n".join(lines) + "\n"
    raise TypeError("canonical strategy rendering requires a StrategyAstV1")


def canonicalize_strategy_source(source: str) -> str:
    return render_canonical_strategy_ast(parse_strategy_ast(source))


def strategy_ast_round_trip(ast: StrategyAstV1) -> StrategyAstV1:
    return parse_strategy_ast(render_canonical_strategy_ast(ast))


def _comparison(
    feature: str,
    operator: ComparisonOperator,
    threshold: Decimal,
) -> ComparisonNodeV1:
    return ComparisonNodeV1(
        feature=feature,
        operator=operator,
        threshold=ExactDecimalV1.from_decimal(threshold),
    )


def _require_renderable_duration(
    value: int,
    context: str,
    *,
    allow_zero: bool = False,
) -> None:
    if type(value) is not int or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{context} must be a valid integer microsecond duration")
    if value > MAX_STRATEGY_DURATION_US_V1:
        raise ValueError(f"{context} exceeds the canonical duration bound")
    if value % 1_000:
        raise ValueError(f"{context} is outside the existing ms/s grammar")


def _render_duration(value_us: int) -> str:
    if value_us % 1_000_000 == 0:
        return f"{value_us // 1_000_000}s"
    return f"{value_us // 1_000}ms"


def _render_transition_header(transition: TransitionNodeV1) -> str:
    prefix = f"transition {transition.source_state} -> {transition.target_state}"
    if transition.qualifier is TimeQualifier.AFTER_ENTRY:
        return f"{prefix} after entry"
    if transition.qualifier is TimeQualifier.INSTANT:
        return f"{prefix} when"
    if transition.qualifier is TimeQualifier.TRUE_FOR:
        return f"{prefix} when for {_render_duration(transition.duration_us)}"
    if transition.qualifier is TimeQualifier.OCCURRED_WITHIN:
        return f"{prefix} when occurred within {_render_duration(transition.duration_us)}"
    if transition.qualifier is TimeQualifier.EVENTS_WITHIN:
        return (
            f"{prefix} when events {transition.event_count} within "
            f"{_render_duration(transition.duration_us)}"
        )
    raise TypeError("strategy transition qualifier is unsupported")


__all__ = [
    "ComparisonNodeV1",
    "ExactDecimalV1",
    "MAX_EXACT_DECIMAL_DIGITS_V1",
    "MAX_EXACT_DECIMAL_SCALE_V1",
    "MAX_STRATEGY_DURATION_US_V1",
    "STRATEGY_AST_SCHEMA_ID_V1",
    "STRATEGY_AST_SCHEMA_VERSION_V1",
    "StateMachineStrategyAstV1",
    "StateNodeV1",
    "StrategyAstKindV1",
    "StrategyAstV1",
    "TrafficLightStrategyAstV1",
    "TransitionNodeV1",
    "canonicalize_strategy_source",
    "parse_strategy_ast",
    "render_canonical_strategy_ast",
    "strategy_ast_from_definition",
    "strategy_ast_round_trip",
]
