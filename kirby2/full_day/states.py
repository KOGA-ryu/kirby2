"""Immutable state-model contracts for deterministic full-day plans.

This module is deliberately a leaf: it depends only on the standard library so
the plan, transition runtime, and checkpoint codecs can all consume the same
wire definitions without an import cycle.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


STATE_MODEL_SCHEMA_VERSION = 1
DAY_STATE_RNG_SUBSTREAM_PATH_V1 = "full_day/runtime/state/day/transition"
LOCAL_STATE_RNG_SUBSTREAM_PATH_V1 = "full_day/runtime/state/local/transition"


class DayStateV1(str, Enum):
    QUIET = "QUIET"
    NORMAL = "NORMAL"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    DISORDERLY = "DISORDERLY"


class LocalStateV1(str, Enum):
    BALANCED = "BALANCED"
    BUY_PRESSURE = "BUY_PRESSURE"
    SELL_PRESSURE = "SELL_PRESSURE"
    ABSORPTION_BID = "ABSORPTION_BID"
    ABSORPTION_ASK = "ABSORPTION_ASK"
    MOMENTUM_UP = "MOMENTUM_UP"
    MOMENTUM_DOWN = "MOMENTUM_DOWN"
    MEAN_REVERSION = "MEAN_REVERSION"
    LIQUIDITY_WITHDRAWAL = "LIQUIDITY_WITHDRAWAL"
    PANIC = "PANIC"
    RECOVERY = "RECOVERY"


class TriggerInformationClassV1(str, Enum):
    OBSERVABLE_AT_TIME = "OBSERVABLE_AT_TIME"
    SYNTHETIC_GROUND_TRUTH = "SYNTHETIC_GROUND_TRUTH"


class DurationExhaustionBehaviorV1(str, Enum):
    TRANSITION_ON_EXHAUSTION = "TRANSITION_ON_EXHAUSTION"
    WAIT_FOR_TRIGGER = "WAIT_FOR_TRIGGER"


class ParameterTargetV1(str, Enum):
    """Closed, non-imperative controls that a state transition may retune."""

    LIMIT_BUY_INTENSITY = "LIMIT_BUY_INTENSITY"
    LIMIT_SELL_INTENSITY = "LIMIT_SELL_INTENSITY"
    MARKET_BUY_INTENSITY = "MARKET_BUY_INTENSITY"
    MARKET_SELL_INTENSITY = "MARKET_SELL_INTENSITY"
    CANCEL_BID_INTENSITY = "CANCEL_BID_INTENSITY"
    CANCEL_ASK_INTENSITY = "CANCEL_ASK_INTENSITY"
    ORDER_SIZE_SCALE = "ORDER_SIZE_SCALE"
    DEPTH_PLACEMENT_SCALE = "DEPTH_PLACEMENT_SCALE"
    PARTICIPANT_ACTIVITY_SCALE = "PARTICIPANT_ACTIVITY_SCALE"
    QUOTING_ACTIVITY_SCALE = "QUOTING_ACTIVITY_SCALE"
    LIQUIDITY_PROVISION_SCALE = "LIQUIDITY_PROVISION_SCALE"


class TriggerParameterUnitV1(str, Enum):
    TICKS = "TICKS"
    SHARES = "SHARES"
    MICROSECONDS = "MICROSECONDS"
    PPM = "PPM"
    COUNT = "COUNT"


DayState = DayStateV1
LocalState = LocalStateV1
TriggerInformationClass = TriggerInformationClassV1
DurationExhaustionBehavior = DurationExhaustionBehaviorV1
ParameterTarget = ParameterTargetV1
TriggerParameterUnit = TriggerParameterUnitV1


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str] | frozenset[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    actual = set(payload)
    missing = sorted(set(expected).difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {context} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )


def _wire_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _wire_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _wire_object(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = payload[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {name} must be an object")
    return value


def _wire_array(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload[name]
    if type(value) is not list:
        raise TypeError(f"serialized {name} must be an array")
    return value


def _canonical_label(value: object, context: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{context} must be a string")
    if not value or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{context} must be nonempty NFC text")
    if any(character.isspace() for character in value):
        raise ValueError(f"{context} must not contain whitespace")
    return value


@dataclass(frozen=True, slots=True)
class DurationMassV1:
    duration_us: int
    weight: int

    def __post_init__(self) -> None:
        if type(self.duration_us) is not int or self.duration_us < 0:
            raise ValueError("duration mass must use nonnegative integer microseconds")
        if type(self.weight) is not int or self.weight <= 0:
            raise ValueError("duration mass weight must be a positive integer")

    def as_dict(self) -> dict[str, object]:
        return {"duration_us": self.duration_us, "weight": self.weight}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DurationMassV1:
        _require_exact_fields(payload, {"duration_us", "weight"}, "duration mass")
        return cls(
            duration_us=_wire_int(payload, "duration_us"),
            weight=_wire_int(payload, "weight"),
        )


@dataclass(frozen=True, slots=True)
class DurationLawV1:
    minimum_us: int
    maximum_us: int
    masses: tuple[DurationMassV1, ...]

    def __post_init__(self) -> None:
        if type(self.minimum_us) is not int or self.minimum_us < 0:
            raise ValueError("duration minimum must be nonnegative integer microseconds")
        if type(self.maximum_us) is not int or self.maximum_us < self.minimum_us:
            raise ValueError("duration maximum must not precede its minimum")
        if type(self.masses) is not tuple or not self.masses:
            raise ValueError("duration law requires a nonempty tuple of masses")
        if any(type(item) is not DurationMassV1 for item in self.masses):
            raise TypeError("duration law masses must use DurationMassV1")
        durations = tuple(item.duration_us for item in self.masses)
        if durations != tuple(sorted(set(durations))):
            raise ValueError("duration masses must be unique and strictly increasing")
        if durations[0] != self.minimum_us or durations[-1] != self.maximum_us:
            raise ValueError("declared duration bounds must equal the mass-table bounds")

    @property
    def expected_duration_numerator(self) -> int:
        numerator = sum(item.duration_us * item.weight for item in self.masses)
        denominator = sum(item.weight for item in self.masses)
        divisor = math.gcd(numerator, denominator)
        return numerator // divisor

    @property
    def expected_duration_denominator(self) -> int:
        numerator = sum(item.duration_us * item.weight for item in self.masses)
        denominator = sum(item.weight for item in self.masses)
        divisor = math.gcd(numerator, denominator)
        return denominator // divisor

    def as_dict(self) -> dict[str, object]:
        return {
            "masses": [item.as_dict() for item in self.masses],
            "maximum_us": self.maximum_us,
            "minimum_us": self.minimum_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DurationLawV1:
        _require_exact_fields(
            payload,
            {"masses", "maximum_us", "minimum_us"},
            "duration law",
        )
        masses = _wire_array(payload, "masses")
        return cls(
            minimum_us=_wire_int(payload, "minimum_us"),
            maximum_us=_wire_int(payload, "maximum_us"),
            masses=tuple(
                DurationMassV1.from_dict(_array_object(item, "duration mass"))
                for item in masses
            ),
        )


@dataclass(frozen=True, slots=True)
class ParameterEffectV1:
    target: ParameterTargetV1
    modifier_numerator: int
    modifier_denominator: int
    minimum_numerator: int
    minimum_denominator: int
    maximum_numerator: int
    maximum_denominator: int

    def __post_init__(self) -> None:
        if type(self.target) is not ParameterTargetV1:
            raise TypeError("parameter effect target must use ParameterTargetV1")
        values = (
            self.modifier_numerator,
            self.modifier_denominator,
            self.minimum_numerator,
            self.minimum_denominator,
            self.maximum_numerator,
            self.maximum_denominator,
        )
        if any(type(value) is not int for value in values):
            raise TypeError("parameter effect ratios must use integers")
        if (
            self.modifier_numerator < 0
            or self.minimum_numerator < 0
            or self.maximum_numerator < 0
            or self.modifier_denominator <= 0
            or self.minimum_denominator <= 0
            or self.maximum_denominator <= 0
        ):
            raise ValueError("parameter effect ratios must be nonnegative with positive denominators")
        for numerator, denominator in (
            (self.modifier_numerator, self.modifier_denominator),
            (self.minimum_numerator, self.minimum_denominator),
            (self.maximum_numerator, self.maximum_denominator),
        ):
            if math.gcd(numerator, denominator) != 1:
                raise ValueError("parameter effect ratios must be reduced")
        if (
            self.minimum_numerator * self.modifier_denominator
            > self.modifier_numerator * self.minimum_denominator
            or self.modifier_numerator * self.maximum_denominator
            > self.maximum_numerator * self.modifier_denominator
        ):
            raise ValueError("parameter modifier must lie within its declared bounds")

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_denominator": self.maximum_denominator,
            "maximum_numerator": self.maximum_numerator,
            "minimum_denominator": self.minimum_denominator,
            "minimum_numerator": self.minimum_numerator,
            "modifier_denominator": self.modifier_denominator,
            "modifier_numerator": self.modifier_numerator,
            "target": self.target.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ParameterEffectV1:
        fields = {
            "maximum_denominator",
            "maximum_numerator",
            "minimum_denominator",
            "minimum_numerator",
            "modifier_denominator",
            "modifier_numerator",
            "target",
        }
        _require_exact_fields(payload, fields, "parameter effect")
        return cls(
            target=ParameterTargetV1(_wire_str(payload, "target")),
            modifier_numerator=_wire_int(payload, "modifier_numerator"),
            modifier_denominator=_wire_int(payload, "modifier_denominator"),
            minimum_numerator=_wire_int(payload, "minimum_numerator"),
            minimum_denominator=_wire_int(payload, "minimum_denominator"),
            maximum_numerator=_wire_int(payload, "maximum_numerator"),
            maximum_denominator=_wire_int(payload, "maximum_denominator"),
        )


@dataclass(frozen=True, slots=True)
class TriggerParameterV1:
    name: str
    unit: TriggerParameterUnitV1
    value: int

    def __post_init__(self) -> None:
        _canonical_label(self.name, "trigger parameter name")
        if type(self.unit) is not TriggerParameterUnitV1:
            raise TypeError("trigger parameter unit must use TriggerParameterUnitV1")
        if type(self.value) is not int:
            raise TypeError("trigger parameter value must be an integer")
        if self.unit in {
            TriggerParameterUnitV1.SHARES,
            TriggerParameterUnitV1.MICROSECONDS,
            TriggerParameterUnitV1.COUNT,
        } and self.value < 0:
            raise ValueError("unsigned trigger parameter cannot be negative")
        if self.unit is TriggerParameterUnitV1.PPM and not 0 <= self.value <= 1_000_000:
            raise ValueError("PPM trigger parameter must lie in [0, 1000000]")

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "unit": self.unit.value, "value": self.value}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TriggerParameterV1:
        _require_exact_fields(
            payload,
            {"name", "unit", "value"},
            "trigger parameter",
        )
        return cls(
            name=_wire_str(payload, "name"),
            unit=TriggerParameterUnitV1(_wire_str(payload, "unit")),
            value=_wire_int(payload, "value"),
        )


@dataclass(frozen=True, slots=True)
class StateTransitionV1:
    transition_id: str
    source_state: str
    successor_state: str
    minimum_age_us: int
    duration_exhaustion_behavior: DurationExhaustionBehaviorV1
    weight: int
    trigger_id: str
    trigger_version: int
    trigger_parameters: tuple[TriggerParameterV1, ...]
    trigger_information_class: TriggerInformationClassV1
    parameter_effects: tuple[ParameterEffectV1, ...]

    def __post_init__(self) -> None:
        _canonical_label(self.transition_id, "transition ID")
        _canonical_label(self.source_state, "transition source state")
        _canonical_label(self.successor_state, "transition successor state")
        _canonical_label(self.trigger_id, "trigger ID")
        if self.source_state == self.successor_state:
            raise ValueError("state transitions must change state")
        if type(self.minimum_age_us) is not int or self.minimum_age_us < 0:
            raise ValueError("transition minimum age must be nonnegative microseconds")
        if type(self.duration_exhaustion_behavior) is not DurationExhaustionBehaviorV1:
            raise TypeError("transition exhaustion behavior uses the wrong enum")
        if type(self.weight) is not int or self.weight <= 0:
            raise ValueError("transition weight must be a positive integer")
        if type(self.trigger_version) is not int or self.trigger_version <= 0:
            raise ValueError("trigger version must be a positive integer")
        if type(self.trigger_parameters) is not tuple or any(
            type(item) is not TriggerParameterV1 for item in self.trigger_parameters
        ):
            raise TypeError("transition trigger parameters must use TriggerParameterV1")
        parameter_names = tuple(item.name for item in self.trigger_parameters)
        if parameter_names != tuple(sorted(set(parameter_names))):
            raise ValueError("trigger parameters must be unique and canonically ordered")
        if type(self.trigger_information_class) is not TriggerInformationClassV1:
            raise TypeError("transition trigger information class uses the wrong enum")
        if type(self.parameter_effects) is not tuple:
            raise TypeError("transition parameter effects must be a tuple")
        if any(type(item) is not ParameterEffectV1 for item in self.parameter_effects):
            raise TypeError("transition effects must use ParameterEffectV1")
        targets = tuple(item.target for item in self.parameter_effects)
        if len(targets) != len(set(targets)):
            raise ValueError("a transition cannot retune one parameter target twice")
        if targets != tuple(sorted(targets, key=lambda item: item.value)):
            raise ValueError("transition parameter effects must be canonically ordered")

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_exhaustion_behavior": self.duration_exhaustion_behavior.value,
            "minimum_age_us": self.minimum_age_us,
            "parameter_effects": [item.as_dict() for item in self.parameter_effects],
            "source_state": self.source_state,
            "successor_state": self.successor_state,
            "transition_id": self.transition_id,
            "trigger_id": self.trigger_id,
            "trigger_information_class": self.trigger_information_class.value,
            "trigger_parameters": [item.as_dict() for item in self.trigger_parameters],
            "trigger_version": self.trigger_version,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> StateTransitionV1:
        fields = {
            "duration_exhaustion_behavior",
            "minimum_age_us",
            "parameter_effects",
            "source_state",
            "successor_state",
            "transition_id",
            "trigger_id",
            "trigger_information_class",
            "trigger_parameters",
            "trigger_version",
            "weight",
        }
        _require_exact_fields(payload, fields, "state transition")
        effects = _wire_array(payload, "parameter_effects")
        trigger_parameters = _wire_array(payload, "trigger_parameters")
        return cls(
            transition_id=_wire_str(payload, "transition_id"),
            source_state=_wire_str(payload, "source_state"),
            successor_state=_wire_str(payload, "successor_state"),
            minimum_age_us=_wire_int(payload, "minimum_age_us"),
            duration_exhaustion_behavior=DurationExhaustionBehaviorV1(
                _wire_str(payload, "duration_exhaustion_behavior")
            ),
            weight=_wire_int(payload, "weight"),
            trigger_id=_wire_str(payload, "trigger_id"),
            trigger_version=_wire_int(payload, "trigger_version"),
            trigger_parameters=tuple(
                TriggerParameterV1.from_dict(
                    _array_object(item, "trigger parameter")
                )
                for item in trigger_parameters
            ),
            trigger_information_class=TriggerInformationClassV1(
                _wire_str(payload, "trigger_information_class")
            ),
            parameter_effects=tuple(
                ParameterEffectV1.from_dict(_array_object(item, "parameter effect"))
                for item in effects
            ),
        )


@dataclass(frozen=True, slots=True)
class DayStateDefinitionV1:
    state: DayStateV1
    duration_law: DurationLawV1
    parameter_effects: tuple[ParameterEffectV1, ...]
    transitions: tuple[StateTransitionV1, ...]

    def __post_init__(self) -> None:
        _validate_definition(self, DayStateV1, "day")

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_law": self.duration_law.as_dict(),
            "parameter_effects": [item.as_dict() for item in self.parameter_effects],
            "state": self.state.value,
            "transitions": [item.as_dict() for item in self.transitions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DayStateDefinitionV1:
        _require_exact_fields(
            payload,
            {"duration_law", "parameter_effects", "state", "transitions"},
            "day-state definition",
        )
        transitions = _wire_array(payload, "transitions")
        effects = _wire_array(payload, "parameter_effects")
        return cls(
            state=DayStateV1(_wire_str(payload, "state")),
            duration_law=DurationLawV1.from_dict(
                _wire_object(payload, "duration_law")
            ),
            parameter_effects=tuple(
                ParameterEffectV1.from_dict(_array_object(item, "parameter effect"))
                for item in effects
            ),
            transitions=tuple(
                StateTransitionV1.from_dict(_array_object(item, "state transition"))
                for item in transitions
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalStateDefinitionV1:
    state: LocalStateV1
    duration_law: DurationLawV1
    parameter_effects: tuple[ParameterEffectV1, ...]
    transitions: tuple[StateTransitionV1, ...]

    def __post_init__(self) -> None:
        _validate_definition(self, LocalStateV1, "local")

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_law": self.duration_law.as_dict(),
            "parameter_effects": [item.as_dict() for item in self.parameter_effects],
            "state": self.state.value,
            "transitions": [item.as_dict() for item in self.transitions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LocalStateDefinitionV1:
        _require_exact_fields(
            payload,
            {"duration_law", "parameter_effects", "state", "transitions"},
            "local-state definition",
        )
        transitions = _wire_array(payload, "transitions")
        effects = _wire_array(payload, "parameter_effects")
        return cls(
            state=LocalStateV1(_wire_str(payload, "state")),
            duration_law=DurationLawV1.from_dict(
                _wire_object(payload, "duration_law")
            ),
            parameter_effects=tuple(
                ParameterEffectV1.from_dict(_array_object(item, "parameter effect"))
                for item in effects
            ),
            transitions=tuple(
                StateTransitionV1.from_dict(_array_object(item, "state transition"))
                for item in transitions
            ),
        )


@dataclass(frozen=True, slots=True)
class StateModelV1:
    schema_version: int
    initial_day_state: DayStateV1
    initial_local_state: LocalStateV1
    day_rng_substream_label: str
    local_rng_substream_label: str
    day_definitions: tuple[DayStateDefinitionV1, ...]
    local_definitions: tuple[LocalStateDefinitionV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != STATE_MODEL_SCHEMA_VERSION:
            raise ValueError("state model schema version must be 1")
        if type(self.initial_day_state) is not DayStateV1:
            raise TypeError("initial day state must use DayStateV1")
        if type(self.initial_local_state) is not LocalStateV1:
            raise TypeError("initial local state must use LocalStateV1")
        if self.day_rng_substream_label != DAY_STATE_RNG_SUBSTREAM_PATH_V1:
            raise ValueError("day-state RNG must use the frozen runtime-owned path")
        if self.local_rng_substream_label != LOCAL_STATE_RNG_SUBSTREAM_PATH_V1:
            raise ValueError("local-state RNG must use the frozen runtime-owned path")
        if type(self.day_definitions) is not tuple or any(
            type(item) is not DayStateDefinitionV1 for item in self.day_definitions
        ):
            raise TypeError("day definitions must be a tuple of DayStateDefinitionV1")
        if type(self.local_definitions) is not tuple or any(
            type(item) is not LocalStateDefinitionV1 for item in self.local_definitions
        ):
            raise TypeError("local definitions must be a tuple of LocalStateDefinitionV1")
        if tuple(item.state for item in self.day_definitions) != tuple(DayStateV1):
            raise ValueError("day definitions must cover every state in canonical order")
        if tuple(item.state for item in self.local_definitions) != tuple(LocalStateV1):
            raise ValueError("local definitions must cover every state in canonical order")
        all_transitions = tuple(
            transition
            for definition in (*self.day_definitions, *self.local_definitions)
            for transition in definition.transitions
        )
        transition_ids = tuple(item.transition_id for item in all_transitions)
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("state transition IDs must be globally unique")
        _validate_transition_graph(
            self.day_definitions,
            self.initial_day_state.value,
            "day",
        )
        _validate_transition_graph(
            self.local_definitions,
            self.initial_local_state.value,
            "local",
        )
        _reject_zero_time_cycles(self.day_definitions)
        _reject_zero_time_cycles(self.local_definitions)

    def as_dict(self) -> dict[str, object]:
        return {
            "day_definitions": [item.as_dict() for item in self.day_definitions],
            "day_rng_substream_label": self.day_rng_substream_label,
            "initial_day_state": self.initial_day_state.value,
            "initial_local_state": self.initial_local_state.value,
            "local_definitions": [item.as_dict() for item in self.local_definitions],
            "local_rng_substream_label": self.local_rng_substream_label,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> StateModelV1:
        _require_exact_fields(
            payload,
            {
                "day_definitions",
                "day_rng_substream_label",
                "initial_day_state",
                "initial_local_state",
                "local_definitions",
                "local_rng_substream_label",
                "schema_version",
            },
            "state model",
        )
        days = _wire_array(payload, "day_definitions")
        locals_ = _wire_array(payload, "local_definitions")
        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            initial_day_state=DayStateV1(_wire_str(payload, "initial_day_state")),
            initial_local_state=LocalStateV1(
                _wire_str(payload, "initial_local_state")
            ),
            day_rng_substream_label=_wire_str(
                payload, "day_rng_substream_label"
            ),
            local_rng_substream_label=_wire_str(
                payload, "local_rng_substream_label"
            ),
            day_definitions=tuple(
                DayStateDefinitionV1.from_dict(
                    _array_object(item, "day-state definition")
                )
                for item in days
            ),
            local_definitions=tuple(
                LocalStateDefinitionV1.from_dict(
                    _array_object(item, "local-state definition")
                )
                for item in locals_
            ),
        )


def _array_object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    return value


def _validate_definition(
    definition: DayStateDefinitionV1 | LocalStateDefinitionV1,
    state_type: type[DayStateV1] | type[LocalStateV1],
    context: str,
) -> None:
    if type(definition.state) is not state_type:
        raise TypeError(f"{context}-state definition uses the wrong state enum")
    if type(definition.duration_law) is not DurationLawV1:
        raise TypeError(f"{context}-state definition requires DurationLawV1")
    if type(definition.parameter_effects) is not tuple or any(
        type(item) is not ParameterEffectV1 for item in definition.parameter_effects
    ):
        raise TypeError(f"{context}-state parameter effects use the wrong contract")
    effect_targets = tuple(item.target for item in definition.parameter_effects)
    if effect_targets != tuple(sorted(set(effect_targets), key=lambda item: item.value)):
        raise ValueError(f"{context}-state parameter effects must be unique and sorted")
    if type(definition.transitions) is not tuple or not definition.transitions:
        raise ValueError(f"{context}-state definition requires outgoing transitions")
    if any(type(item) is not StateTransitionV1 for item in definition.transitions):
        raise TypeError(f"{context}-state transitions use the wrong contract")
    if tuple(item.transition_id for item in definition.transitions) != tuple(
        sorted(item.transition_id for item in definition.transitions)
    ):
        raise ValueError(f"{context}-state transitions must be canonically ordered")
    allowed = {item.value for item in state_type}
    for transition in definition.transitions:
        if transition.source_state != definition.state.value:
            raise ValueError(f"{context}-state transition source does not match its owner")
        if transition.successor_state not in allowed:
            raise ValueError(f"{context}-state transition successor uses the wrong level")


def _reject_zero_time_cycles(
    definitions: tuple[DayStateDefinitionV1, ...]
    | tuple[LocalStateDefinitionV1, ...],
) -> None:
    forced_edges: dict[str, tuple[str, ...]] = {}
    for definition in definitions:
        if definition.duration_law.minimum_us != 0:
            forced_edges[definition.state.value] = ()
            continue
        forced_edges[definition.state.value] = tuple(
            transition.successor_state
            for transition in definition.transitions
            if transition.minimum_age_us == 0
            and transition.duration_exhaustion_behavior
            is DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(state: str) -> None:
        if state in visiting:
            raise ValueError("state model contains a forced zero-time cycle")
        if state in visited:
            return
        visiting.add(state)
        for successor in forced_edges[state]:
            visit(successor)
        visiting.remove(state)
        visited.add(state)

    for state in forced_edges:
        visit(state)


def _validate_transition_graph(
    definitions: tuple[DayStateDefinitionV1, ...]
    | tuple[LocalStateDefinitionV1, ...],
    initial_state: str,
    context: str,
) -> None:
    edges = {
        definition.state.value: tuple(
            transition.successor_state for transition in definition.transitions
        )
        for definition in definitions
    }
    incoming = {successor for successors in edges.values() for successor in successors}
    if incoming != set(edges):
        missing = sorted(set(edges).difference(incoming))
        raise ValueError(f"{context}-state graph has states with no predecessor: {missing}")
    reachable: set[str] = set()
    pending = [initial_state]
    while pending:
        state = pending.pop()
        if state in reachable:
            continue
        reachable.add(state)
        pending.extend(edges[state])
    if reachable != set(edges):
        missing = sorted(set(edges).difference(reachable))
        raise ValueError(f"{context}-state graph is unreachable from its initial state: {missing}")


__all__ = [
    "DAY_STATE_RNG_SUBSTREAM_PATH_V1",
    "DayState",
    "DayStateDefinitionV1",
    "DayStateV1",
    "DurationExhaustionBehavior",
    "DurationExhaustionBehaviorV1",
    "DurationLawV1",
    "DurationMassV1",
    "LocalState",
    "LocalStateDefinitionV1",
    "LocalStateV1",
    "LOCAL_STATE_RNG_SUBSTREAM_PATH_V1",
    "ParameterEffectV1",
    "ParameterTarget",
    "ParameterTargetV1",
    "STATE_MODEL_SCHEMA_VERSION",
    "StateModelV1",
    "StateTransitionV1",
    "TriggerInformationClass",
    "TriggerInformationClassV1",
    "TriggerParameterUnit",
    "TriggerParameterUnitV1",
    "TriggerParameterV1",
]
