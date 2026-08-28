"""Deterministic duration-aware day/local state runtime.

The runtime in this module owns only hierarchical state-transition state.  It
does not own the full-day global event allocator, the work heap, an exchange,
or any price-forming operation.  Its emissions are therefore typed proposals
that the full-day orchestrator can place in the frozen stage-2/stage-3 outer
event envelope.

All random choices use two plan-declared, explicitly owned SplitMix64
substreams.  Runtime snapshots contain the complete integer PRNG state and use
the strict canonical JSON language frozen by WO31-A.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from .events import (
    FULL_DAY_PAYLOAD_SCHEMA_VERSION,
    FullDayEventPayloadV1,
    FullDayEventTypeV1,
    ScheduledWorkKeyV1,
    WorkStageV1,
)
from .models import (
    FullDayPlanV1,
    PressureKindV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from .states import (
    DayStateDefinitionV1,
    DayStateV1,
    DurationExhaustionBehaviorV1,
    DurationLawV1,
    LocalStateDefinitionV1,
    LocalStateV1,
    ParameterEffectV1,
    ParameterTargetV1,
    StateTransitionV1,
    TriggerInformationClassV1,
)


STATE_TRANSITION_RUNTIME_SCHEMA_VERSION = 1
TRIGGER_OBSERVATION_SCHEMA_VERSION = 1
STATE_TRANSITION_EMISSION_SCHEMA_VERSION = 1
DAY_STATE_ANCHOR_EMISSION_SCHEMA_VERSION = 1
TRANSITION_RNG_ALGORITHM_V1 = "SPLITMIX64_V1"
FULL_DAY_RUNTIME_COMPONENT_ID_V1 = "FULL_DAY_RUNTIME_V1"

_MASK_64 = (1 << 64) - 1
_SPLITMIX64_INCREMENT = 0x9E3779B97F4A7C15
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StateLevelV1(str, Enum):
    """The two state levels, ordered in their frozen outer-event stage order."""

    DAY = "DAY"
    LOCAL = "LOCAL"


class TriggerObservationPhaseV1(str, Enum):
    """Provenance of a trigger observation.

    Only ``PRE_TRANSITION`` observations are admissible runtime input.  The two
    forbidden values are representable so callers and audits can prove that
    reveal-only and post-transition reads are refused at the causal boundary.
    """

    PRE_TRANSITION = "PRE_TRANSITION"
    POST_TRANSITION = "POST_TRANSITION"
    REVEAL_ONLY = "REVEAL_ONLY"


class TransitionCauseV1(str, Enum):
    TRIGGER = "TRIGGER"
    DURATION_EXHAUSTION = "DURATION_EXHAUSTION"


def _identifier(value: object, context: str) -> str:
    if type(value) is not str or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{context} must be a canonical identifier")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{context} must be NFC-normalized")
    return value


def _exact_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _exact_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _exact_bool(payload: Mapping[str, object], name: str) -> bool:
    value = payload[name]
    if type(value) is not bool:
        raise TypeError(f"serialized {name} must be a Boolean")
    return value


def _exact_object(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = payload[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {name} must be an object")
    return value


def _exact_array(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload[name]
    if type(value) is not list:
        raise TypeError(f"serialized {name} must be an array")
    return value


def _array_object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    return value


def _optional_nonnegative_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{context} must be null or a nonnegative integer")
    return value


def _freeze_json(value: object) -> object:
    validate_strict_json(value)
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(value[key]) for key in sorted(value)}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(value[key]) for key in sorted(value)}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class TriggerObservationV1:
    """One causally classified trigger result made available to the runtime."""

    schema_version: int
    observation_id: str
    transition_id: str
    trigger_id: str
    trigger_version: int
    trigger_parameter_set_sha256: str
    information_class: TriggerInformationClassV1
    observation_time_us: int
    information_cutoff_us: int
    available_time_us: int
    phase: TriggerObservationPhaseV1
    triggered: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRIGGER_OBSERVATION_SCHEMA_VERSION
        ):
            raise ValueError("trigger observation schema version must be 1")
        _identifier(self.observation_id, "trigger observation ID")
        _identifier(self.transition_id, "observed transition ID")
        _identifier(self.trigger_id, "trigger ID")
        if type(self.trigger_version) is not int or self.trigger_version <= 0:
            raise ValueError("trigger observation version must be positive")
        if type(self.information_class) is not TriggerInformationClassV1:
            raise TypeError("trigger information class uses the wrong enum")
        if (
            type(self.trigger_parameter_set_sha256) is not str
            or not _SHA256_RE.fullmatch(self.trigger_parameter_set_sha256)
            or type(self.evidence_sha256) is not str
            or not _SHA256_RE.fullmatch(self.evidence_sha256)
        ):
            raise ValueError("trigger parameter/evidence digests must be lowercase SHA-256")
        if (
            type(self.observation_time_us) is not int
            or self.observation_time_us < 0
            or type(self.information_cutoff_us) is not int
            or self.information_cutoff_us < 0
            or self.information_cutoff_us > self.observation_time_us
            or type(self.available_time_us) is not int
            or self.available_time_us < self.observation_time_us
        ):
            raise ValueError(
                "trigger observation times must be forward nonnegative microseconds"
            )
        if type(self.phase) is not TriggerObservationPhaseV1:
            raise TypeError("trigger observation phase uses the wrong enum")
        if type(self.triggered) is not bool:
            raise TypeError("triggered must be Boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "available_time_us": self.available_time_us,
            "evidence_sha256": self.evidence_sha256,
            "information_class": self.information_class.value,
            "information_cutoff_us": self.information_cutoff_us,
            "observation_id": self.observation_id,
            "observation_time_us": self.observation_time_us,
            "phase": self.phase.value,
            "schema_version": self.schema_version,
            "transition_id": self.transition_id,
            "trigger_id": self.trigger_id,
            "trigger_parameter_set_sha256": self.trigger_parameter_set_sha256,
            "trigger_version": self.trigger_version,
            "triggered": self.triggered,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TriggerObservationV1:
        _require_exact_fields(
            payload,
            {
                "available_time_us",
                "evidence_sha256",
                "information_class",
                "information_cutoff_us",
                "observation_id",
                "observation_time_us",
                "phase",
                "schema_version",
                "transition_id",
                "trigger_id",
                "trigger_parameter_set_sha256",
                "trigger_version",
                "triggered",
            },
            "trigger observation",
        )
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            observation_id=_exact_str(payload, "observation_id"),
            transition_id=_exact_str(payload, "transition_id"),
            trigger_id=_exact_str(payload, "trigger_id"),
            trigger_version=_exact_int(payload, "trigger_version"),
            trigger_parameter_set_sha256=_exact_str(
                payload, "trigger_parameter_set_sha256"
            ),
            information_class=TriggerInformationClassV1(
                _exact_str(payload, "information_class")
            ),
            observation_time_us=_exact_int(payload, "observation_time_us"),
            information_cutoff_us=_exact_int(payload, "information_cutoff_us"),
            available_time_us=_exact_int(payload, "available_time_us"),
            phase=TriggerObservationPhaseV1(_exact_str(payload, "phase")),
            triggered=_exact_bool(payload, "triggered"),
            evidence_sha256=_exact_str(payload, "evidence_sha256"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> TriggerObservationV1:
        return cls.from_dict(parse_canonical_json_object(raw))


@dataclass(frozen=True, slots=True)
class TransitionRngStateV1:
    """Portable complete state of one runtime-owned SplitMix64 substream."""

    algorithm: str
    substream_label: str
    initial_seed: int
    state_u64: int
    draw_count: int
    sample_count: int

    def __post_init__(self) -> None:
        if self.algorithm != TRANSITION_RNG_ALGORITHM_V1:
            raise ValueError("transition RNG algorithm must be SPLITMIX64_V1")
        _identifier(self.substream_label, "transition RNG substream label")
        if type(self.initial_seed) is not int or not 0 <= self.initial_seed <= 2**63 - 1:
            raise ValueError("transition RNG initial seed is outside [0, 2**63-1]")
        if type(self.state_u64) is not int or not 0 <= self.state_u64 <= _MASK_64:
            raise ValueError("transition RNG state must be an unsigned 64-bit integer")
        if type(self.draw_count) is not int or self.draw_count < 0:
            raise ValueError("transition RNG draw count must be nonnegative")
        if (
            type(self.sample_count) is not int
            or self.sample_count < 0
            or self.sample_count > self.draw_count
        ):
            raise ValueError(
                "transition RNG sample count must lie within its raw draw count"
            )
        expected = (
            self.initial_seed + self.draw_count * _SPLITMIX64_INCREMENT
        ) & _MASK_64
        if self.state_u64 != expected:
            raise ValueError("transition RNG state does not reconcile with its draw count")

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "draw_count": self.draw_count,
            "initial_seed": self.initial_seed,
            "sample_count": self.sample_count,
            "state_u64": self.state_u64,
            "substream_label": self.substream_label,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TransitionRngStateV1:
        _require_exact_fields(
            payload,
            {
                "algorithm",
                "draw_count",
                "initial_seed",
                "sample_count",
                "state_u64",
                "substream_label",
            },
            "transition RNG state",
        )
        return cls(
            algorithm=_exact_str(payload, "algorithm"),
            substream_label=_exact_str(payload, "substream_label"),
            initial_seed=_exact_int(payload, "initial_seed"),
            state_u64=_exact_int(payload, "state_u64"),
            draw_count=_exact_int(payload, "draw_count"),
            sample_count=_exact_int(payload, "sample_count"),
        )


class _SplitMix64V1:
    def __init__(self, state: TransitionRngStateV1) -> None:
        self.algorithm = state.algorithm
        self.label = state.substream_label
        self.initial_seed = state.initial_seed
        self.state_u64 = state.state_u64
        self.draw_count = state.draw_count
        self.sample_count = state.sample_count

    @classmethod
    def seeded(cls, seed: int, label: str) -> _SplitMix64V1:
        return cls(
            TransitionRngStateV1(
                algorithm=TRANSITION_RNG_ALGORITHM_V1,
                substream_label=label,
                initial_seed=seed,
                state_u64=seed,
                draw_count=0,
                sample_count=0,
            )
        )

    def snapshot(self) -> TransitionRngStateV1:
        return TransitionRngStateV1(
            algorithm=self.algorithm,
            substream_label=self.label,
            initial_seed=self.initial_seed,
            state_u64=self.state_u64,
            draw_count=self.draw_count,
            sample_count=self.sample_count,
        )

    def _next_u64(self) -> int:
        self.state_u64 = (self.state_u64 + _SPLITMIX64_INCREMENT) & _MASK_64
        self.draw_count += 1
        value = self.state_u64
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_64
        return (value ^ (value >> 31)) & _MASK_64

    def randbelow(self, bound: int) -> int:
        if type(bound) is not int or bound <= 0:
            raise ValueError("random bound must be a positive integer")
        self.sample_count += 1
        if bound == 1:
            self._next_u64()
            return 0
        if bound <= 1 << 64:
            threshold = ((1 << 64) - bound) % bound
            while True:
                candidate = self._next_u64()
                if candidate >= threshold:
                    return candidate % bound
        bits = bound.bit_length()
        words = (bits + 63) // 64
        mask = (1 << bits) - 1
        while True:
            candidate = 0
            for _ in range(words):
                candidate = (candidate << 64) | self._next_u64()
            candidate &= mask
            if candidate < bound:
                return candidate


@dataclass(frozen=True, slots=True)
class StateTriggerMemoryV1:
    """An accepted observation bound to one precise state incarnation."""

    state: str
    state_entered_time_us: int
    observation: TriggerObservationV1

    def __post_init__(self) -> None:
        _identifier(self.state, "trigger-memory state")
        if type(self.state_entered_time_us) is not int or self.state_entered_time_us < 0:
            raise ValueError("trigger-memory entered time must be nonnegative")
        if type(self.observation) is not TriggerObservationV1:
            raise TypeError("trigger memory requires TriggerObservationV1")
        if self.observation.observation_time_us < self.state_entered_time_us:
            raise ValueError("trigger observation predates its bound state incarnation")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation": self.observation.as_dict(),
            "state": self.state,
            "state_entered_time_us": self.state_entered_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> StateTriggerMemoryV1:
        _require_exact_fields(
            payload,
            {"observation", "state", "state_entered_time_us"},
            "state trigger memory",
        )
        return cls(
            state=_exact_str(payload, "state"),
            state_entered_time_us=_exact_int(payload, "state_entered_time_us"),
            observation=TriggerObservationV1.from_dict(
                _exact_object(payload, "observation")
            ),
        )


@dataclass(frozen=True, slots=True)
class StateLevelRuntimeStateV1:
    """Canonical snapshot of one day/local state level."""

    level: StateLevelV1
    as_of_time_us: int
    current_state: str
    entered_time_us: int
    elapsed_age_us: int
    sampled_duration_us: int
    deadline_time_us: int
    next_eligible_transition_id: str
    next_eligible_transition_time_us: int | None
    trigger_memory: tuple[StateTriggerMemoryV1, ...]

    def __post_init__(self) -> None:
        if type(self.level) is not StateLevelV1:
            raise TypeError("state level uses the wrong enum")
        _identifier(self.current_state, "runtime current state")
        integers = (
            self.as_of_time_us,
            self.entered_time_us,
            self.elapsed_age_us,
            self.sampled_duration_us,
            self.deadline_time_us,
        )
        if any(type(value) is not int or value < 0 for value in integers):
            raise ValueError("state runtime times must be nonnegative integers")
        if self.entered_time_us > self.as_of_time_us:
            raise ValueError("state entered time cannot be in the future")
        if self.elapsed_age_us != self.as_of_time_us - self.entered_time_us:
            raise ValueError("state elapsed age does not reconcile with simulation time")
        if self.deadline_time_us != self.entered_time_us + self.sampled_duration_us:
            raise ValueError("state deadline does not reconcile with sampled duration")
        _identifier(
            self.next_eligible_transition_id,
            "next eligible transition ID",
        )
        _optional_nonnegative_int(
            self.next_eligible_transition_time_us,
            "next eligible transition time",
        )
        if (
            self.next_eligible_transition_time_us is not None
            and self.next_eligible_transition_time_us < self.entered_time_us
        ):
            raise ValueError("next eligible transition predates state entry")
        if type(self.trigger_memory) is not tuple or any(
            type(item) is not StateTriggerMemoryV1 for item in self.trigger_memory
        ):
            raise TypeError("trigger memory must be a tuple of StateTriggerMemoryV1")
        keys = tuple(
            (
                item.observation.available_time_us,
                item.observation.observation_time_us,
                item.observation.observation_id,
            )
            for item in self.trigger_memory
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("trigger memory must be unique and canonically ordered")
        if any(
            item.state != self.current_state
            or item.state_entered_time_us != self.entered_time_us
            or item.observation.available_time_us > self.as_of_time_us
            for item in self.trigger_memory
        ):
            raise ValueError("trigger memory is not bound to the current state/time")

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_time_us": self.as_of_time_us,
            "current_state": self.current_state,
            "deadline_time_us": self.deadline_time_us,
            "elapsed_age_us": self.elapsed_age_us,
            "entered_time_us": self.entered_time_us,
            "level": self.level.value,
            "next_eligible_transition_id": self.next_eligible_transition_id,
            "next_eligible_transition_time_us": self.next_eligible_transition_time_us,
            "sampled_duration_us": self.sampled_duration_us,
            "trigger_memory": [item.as_dict() for item in self.trigger_memory],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> StateLevelRuntimeStateV1:
        _require_exact_fields(
            payload,
            {
                "as_of_time_us",
                "current_state",
                "deadline_time_us",
                "elapsed_age_us",
                "entered_time_us",
                "level",
                "next_eligible_transition_id",
                "next_eligible_transition_time_us",
                "sampled_duration_us",
                "trigger_memory",
            },
            "state-level runtime state",
        )
        return cls(
            level=StateLevelV1(_exact_str(payload, "level")),
            as_of_time_us=_exact_int(payload, "as_of_time_us"),
            current_state=_exact_str(payload, "current_state"),
            entered_time_us=_exact_int(payload, "entered_time_us"),
            elapsed_age_us=_exact_int(payload, "elapsed_age_us"),
            sampled_duration_us=_exact_int(payload, "sampled_duration_us"),
            deadline_time_us=_exact_int(payload, "deadline_time_us"),
            next_eligible_transition_id=_exact_str(
                payload, "next_eligible_transition_id"
            ),
            next_eligible_transition_time_us=_optional_nonnegative_int(
                payload["next_eligible_transition_time_us"],
                "serialized next eligible transition time",
            ),
            trigger_memory=tuple(
                StateTriggerMemoryV1.from_dict(
                    _array_object(item, "state trigger memory")
                )
                for item in _exact_array(payload, "trigger_memory")
            ),
        )


@dataclass(frozen=True, slots=True)
class HierarchicalStateRuntimeStateV1:
    """Complete canonical state needed to continue the state runtime exactly."""

    schema_version: int
    plan_sha256: str
    state_model_sha256: str
    current_time_us: int
    input_closed_through_time_us: int | None
    component_local_sequence: int
    component_sequence_offset: int
    runtime_emission_count: int
    day_transition_count: int
    local_transition_count: int
    day_transitions_since_macro_anchor: int
    next_macro_segment_index: int
    observation_ids_seen: tuple[str, ...]
    day: StateLevelRuntimeStateV1
    local: StateLevelRuntimeStateV1
    day_rng: TransitionRngStateV1
    local_rng: TransitionRngStateV1

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != STATE_TRANSITION_RUNTIME_SCHEMA_VERSION
        ):
            raise ValueError("state runtime schema version must be 1")
        if (
            type(self.plan_sha256) is not str
            or not _SHA256_RE.fullmatch(self.plan_sha256)
            or type(self.state_model_sha256) is not str
            or not _SHA256_RE.fullmatch(self.state_model_sha256)
        ):
            raise ValueError("plan/model digests must be lowercase SHA-256")
        if type(self.current_time_us) is not int or self.current_time_us < 0:
            raise ValueError("runtime time must be nonnegative")
        _optional_nonnegative_int(
            self.input_closed_through_time_us,
            "input-closed-through time",
        )
        if (
            self.input_closed_through_time_us is not None
            and self.input_closed_through_time_us != self.current_time_us
        ):
            raise ValueError("closed input frontier must equal current runtime time")
        allocator_values = (
            self.component_local_sequence,
            self.component_sequence_offset,
            self.runtime_emission_count,
            self.day_transition_count,
            self.local_transition_count,
            self.day_transitions_since_macro_anchor,
        )
        if any(type(value) is not int or value < 0 for value in allocator_values):
            raise ValueError("component allocator counters must be nonnegative integers")
        if type(self.next_macro_segment_index) is not int or self.next_macro_segment_index < 0:
            raise ValueError("next macro-segment index must be nonnegative")
        if (
            self.component_local_sequence
            != self.component_sequence_offset + self.runtime_emission_count
        ):
            raise ValueError(
                "component-local sequence does not reconcile with its shared offset "
                "and runtime emissions"
            )
        if self.next_macro_segment_index > self.runtime_emission_count:
            raise ValueError("macro anchors exceed recorded runtime emissions")
        if self.runtime_emission_count != (
            self.next_macro_segment_index
            + self.day_transition_count
            + self.local_transition_count
        ):
            raise ValueError(
                "runtime emissions do not reconcile with anchor/day/local counts"
            )
        if self.day_transitions_since_macro_anchor > self.day_transition_count:
            raise ValueError("recent day transitions exceed the cumulative count")
        if type(self.observation_ids_seen) is not tuple:
            raise TypeError("seen observation IDs must be a tuple")
        for identifier in self.observation_ids_seen:
            _identifier(identifier, "seen observation ID")
        if self.observation_ids_seen != tuple(sorted(set(self.observation_ids_seen))):
            raise ValueError("seen observation IDs must be unique and sorted")
        if type(self.day) is not StateLevelRuntimeStateV1 or self.day.level is not StateLevelV1.DAY:
            raise TypeError("day runtime state is missing or mislabeled")
        if (
            type(self.local) is not StateLevelRuntimeStateV1
            or self.local.level is not StateLevelV1.LOCAL
        ):
            raise TypeError("local runtime state is missing or mislabeled")
        if (
            self.day.as_of_time_us != self.current_time_us
            or self.local.as_of_time_us != self.current_time_us
        ):
            raise ValueError("level snapshots do not share the runtime time")
        if (
            type(self.day_rng) is not TransitionRngStateV1
            or type(self.local_rng) is not TransitionRngStateV1
        ):
            raise TypeError("runtime RNG states use the wrong contract")
        expected_day_samples = (
            2
            + 2 * self.day_transition_count
            + 2 * max(self.next_macro_segment_index - 1, 0)
        )
        expected_local_samples = 2 + 2 * self.local_transition_count
        if self.day_rng.sample_count != expected_day_samples:
            raise ValueError(
                "day RNG samples do not reconcile with transitions and anchors"
            )
        if self.local_rng.sample_count != expected_local_samples:
            raise ValueError(
                "local RNG samples do not reconcile with transition history"
            )
        active_ids = {
            item.observation.observation_id
            for item in (*self.day.trigger_memory, *self.local.trigger_memory)
        }
        if not active_ids.issubset(set(self.observation_ids_seen)):
            raise ValueError("active trigger memory is absent from seen observation IDs")

    def as_dict(self) -> dict[str, object]:
        return {
            "component_local_sequence": self.component_local_sequence,
            "component_sequence_offset": self.component_sequence_offset,
            "current_time_us": self.current_time_us,
            "day_transition_count": self.day_transition_count,
            "day_transitions_since_macro_anchor": (
                self.day_transitions_since_macro_anchor
            ),
            "day": self.day.as_dict(),
            "day_rng": self.day_rng.as_dict(),
            "local": self.local.as_dict(),
            "local_transition_count": self.local_transition_count,
            "local_rng": self.local_rng.as_dict(),
            "next_macro_segment_index": self.next_macro_segment_index,
            "observation_ids_seen": list(self.observation_ids_seen),
            "input_closed_through_time_us": self.input_closed_through_time_us,
            "plan_sha256": self.plan_sha256,
            "runtime_emission_count": self.runtime_emission_count,
            "schema_version": self.schema_version,
            "state_model_sha256": self.state_model_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> HierarchicalStateRuntimeStateV1:
        _require_exact_fields(
            payload,
            {
                "component_local_sequence",
                "component_sequence_offset",
                "current_time_us",
                "day",
                "day_transition_count",
                "day_transitions_since_macro_anchor",
                "day_rng",
                "local",
                "local_transition_count",
                "local_rng",
                "next_macro_segment_index",
                "observation_ids_seen",
                "input_closed_through_time_us",
                "plan_sha256",
                "runtime_emission_count",
                "schema_version",
                "state_model_sha256",
            },
            "hierarchical state runtime state",
        )
        seen = _exact_array(payload, "observation_ids_seen")
        if any(type(item) is not str for item in seen):
            raise TypeError("serialized seen observation IDs must be strings")
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            plan_sha256=_exact_str(payload, "plan_sha256"),
            state_model_sha256=_exact_str(payload, "state_model_sha256"),
            current_time_us=_exact_int(payload, "current_time_us"),
            input_closed_through_time_us=_optional_nonnegative_int(
                payload["input_closed_through_time_us"],
                "serialized input-closed-through time",
            ),
            component_local_sequence=_exact_int(payload, "component_local_sequence"),
            component_sequence_offset=_exact_int(
                payload, "component_sequence_offset"
            ),
            runtime_emission_count=_exact_int(payload, "runtime_emission_count"),
            day_transition_count=_exact_int(payload, "day_transition_count"),
            local_transition_count=_exact_int(payload, "local_transition_count"),
            day_transitions_since_macro_anchor=_exact_int(
                payload, "day_transitions_since_macro_anchor"
            ),
            next_macro_segment_index=_exact_int(payload, "next_macro_segment_index"),
            observation_ids_seen=tuple(seen),
            day=StateLevelRuntimeStateV1.from_dict(_exact_object(payload, "day")),
            local=StateLevelRuntimeStateV1.from_dict(_exact_object(payload, "local")),
            day_rng=TransitionRngStateV1.from_dict(_exact_object(payload, "day_rng")),
            local_rng=TransitionRngStateV1.from_dict(_exact_object(payload, "local_rng")),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> HierarchicalStateRuntimeStateV1:
        return cls.from_dict(parse_canonical_json_object(raw))


@dataclass(frozen=True, slots=True)
class FixedPointValueV1:
    """A nonnegative reduced rational used by modifier consumers."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if (
            type(self.numerator) is not int
            or self.numerator < 0
            or type(self.denominator) is not int
            or self.denominator <= 0
        ):
            raise ValueError("fixed-point values require nonnegative/positive integers")
        if math.gcd(self.numerator, self.denominator) != 1:
            raise ValueError("fixed-point values must be reduced")

    @classmethod
    def reduced(cls, numerator: int, denominator: int) -> FixedPointValueV1:
        if type(numerator) is not int or type(denominator) is not int:
            raise TypeError("fixed-point values require integer terms")
        if numerator < 0 or denominator <= 0:
            raise ValueError("fixed-point values require nonnegative/positive terms")
        divisor = math.gcd(numerator, denominator)
        return cls(numerator // divisor, denominator // divisor)

    def as_dict(self) -> dict[str, object]:
        return {"denominator": self.denominator, "numerator": self.numerator}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FixedPointValueV1:
        _require_exact_fields(payload, {"denominator", "numerator"}, "fixed-point value")
        return cls(
            numerator=_exact_int(payload, "numerator"),
            denominator=_exact_int(payload, "denominator"),
        )


@dataclass(frozen=True, slots=True)
class BoundedParameterModifierV1:
    """Closed exact projection of a frozen non-imperative parameter effect."""

    target: ParameterTargetV1
    modifier: FixedPointValueV1
    minimum: FixedPointValueV1
    maximum: FixedPointValueV1

    def __post_init__(self) -> None:
        if type(self.target) is not ParameterTargetV1:
            raise TypeError("modifier target uses the wrong enum")
        if any(
            type(item) is not FixedPointValueV1
            for item in (self.modifier, self.minimum, self.maximum)
        ):
            raise TypeError("modifier ratios must use FixedPointValueV1")
        if (
            self.minimum.numerator * self.modifier.denominator
            > self.modifier.numerator * self.minimum.denominator
        ):
            raise ValueError("modifier lies below its exact lower bound")
        if (
            self.modifier.numerator * self.maximum.denominator
            > self.maximum.numerator * self.modifier.denominator
        ):
            raise ValueError("modifier lies above its exact upper bound")

    @classmethod
    def from_effect(cls, effect: ParameterEffectV1) -> BoundedParameterModifierV1:
        if type(effect) is not ParameterEffectV1:
            raise TypeError("modifier projection requires ParameterEffectV1")
        return cls(
            target=effect.target,
            modifier=FixedPointValueV1(effect.modifier_numerator, effect.modifier_denominator),
            minimum=FixedPointValueV1(effect.minimum_numerator, effect.minimum_denominator),
            maximum=FixedPointValueV1(effect.maximum_numerator, effect.maximum_denominator),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum": self.maximum.as_dict(),
            "minimum": self.minimum.as_dict(),
            "modifier": self.modifier.as_dict(),
            "target": self.target.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> BoundedParameterModifierV1:
        _require_exact_fields(
            payload,
            {"maximum", "minimum", "modifier", "target"},
            "bounded modifier",
        )
        return cls(
            target=ParameterTargetV1(_exact_str(payload, "target")),
            modifier=FixedPointValueV1.from_dict(_exact_object(payload, "modifier")),
            minimum=FixedPointValueV1.from_dict(_exact_object(payload, "minimum")),
            maximum=FixedPointValueV1.from_dict(_exact_object(payload, "maximum")),
        )


def apply_bounded_modifiers_v1(
    base_values: Mapping[ParameterTargetV1, FixedPointValueV1],
    modifiers: Sequence[BoundedParameterModifierV1],
) -> Mapping[ParameterTargetV1, FixedPointValueV1]:
    """Multiply exact mock-consumer values without binary floating point.

    The closed ``ParameterTargetV1`` key type prevents this helper from naming a
    price, trade, close, return target, or imperative order-book mutation.
    """

    if not isinstance(base_values, Mapping):
        raise TypeError("modifier base values must be a mapping")
    if any(
        type(key) is not ParameterTargetV1 or type(value) is not FixedPointValueV1
        for key, value in base_values.items()
    ):
        raise TypeError("modifier bases require ParameterTargetV1/FixedPointValueV1 pairs")
    rows = tuple(modifiers)
    if any(type(item) is not BoundedParameterModifierV1 for item in rows):
        raise TypeError("modifiers must use BoundedParameterModifierV1")
    targets = tuple(item.target for item in rows)
    if len(targets) != len(set(targets)):
        raise ValueError("one modifier batch cannot target a parameter twice")
    if any(target not in base_values for target in targets):
        raise KeyError("modifier target has no base value")
    result = dict(base_values)
    for item in rows:
        base = result[item.target]
        result[item.target] = FixedPointValueV1.reduced(
            base.numerator * item.modifier.numerator,
            base.denominator * item.modifier.denominator,
        )
    return MappingProxyType(dict(sorted(result.items(), key=lambda row: row[0].value)))


@dataclass(frozen=True, slots=True)
class FullDayParameterSnapshotV1:
    """Exact non-imperative controls visible to composed runtime consumers.

    Pressure, hierarchical state, and each accepted-shock batch remain
    provenance-separated.  Consumers may inspect the resulting exact ratios,
    but the closed ``ParameterTargetV1`` enum makes a price, desired return,
    forced trade, inventory liquidation, or direct book write unrepresentable.
    """

    simulation_time_us: int
    pressure_modifiers: tuple[BoundedParameterModifierV1, ...]
    day_state_modifiers: tuple[BoundedParameterModifierV1, ...]
    local_state_modifiers: tuple[BoundedParameterModifierV1, ...]
    shock_modifier_batches: tuple[tuple[BoundedParameterModifierV1, ...], ...]
    effective_values: Mapping[ParameterTargetV1, FixedPointValueV1]

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("full-day parameter snapshot time must be nonnegative")
        for rows, context in (
            (self.pressure_modifiers, "pressure"),
            (self.day_state_modifiers, "day-state"),
            (self.local_state_modifiers, "local-state"),
        ):
            if type(rows) is not tuple or any(
                type(row) is not BoundedParameterModifierV1 for row in rows
            ):
                raise TypeError(f"{context} modifiers use the wrong contract")
            targets = tuple(row.target for row in rows)
            if len(targets) != len(set(targets)):
                raise ValueError(f"{context} modifier targets are duplicated")
        if type(self.shock_modifier_batches) is not tuple or any(
            type(batch) is not tuple
            or any(type(row) is not BoundedParameterModifierV1 for row in batch)
            or len(tuple(row.target for row in batch))
            != len(set(row.target for row in batch))
            for batch in self.shock_modifier_batches
        ):
            raise TypeError("shock modifier batches use the wrong contract")
        if not isinstance(self.effective_values, Mapping) or any(
            type(key) is not ParameterTargetV1
            or type(value) is not FixedPointValueV1
            for key, value in self.effective_values.items()
        ):
            raise TypeError("effective full-day values use the wrong exact contract")
        if set(self.effective_values) != set(ParameterTargetV1):
            raise ValueError("effective full-day values do not cover every target")

    def as_dict(self) -> dict[str, object]:
        return {
            "day_state_modifiers": [
                row.as_dict() for row in self.day_state_modifiers
            ],
            "effective_values": {
                target.value: self.effective_values[target].as_dict()
                for target in ParameterTargetV1
            },
            "local_state_modifiers": [
                row.as_dict() for row in self.local_state_modifiers
            ],
            "pressure_modifiers": [
                row.as_dict() for row in self.pressure_modifiers
            ],
            "shock_modifier_batches": [
                [row.as_dict() for row in batch]
                for batch in self.shock_modifier_batches
            ],
            "simulation_time_us": self.simulation_time_us,
        }


_PRESSURE_TARGET_V1 = MappingProxyType(
    {
        PressureKindV1.VOLUME: ParameterTargetV1.PARTICIPANT_ACTIVITY_SCALE,
        PressureKindV1.LIQUIDITY: ParameterTargetV1.LIQUIDITY_PROVISION_SCALE,
        PressureKindV1.VOLATILITY: ParameterTargetV1.DEPTH_PLACEMENT_SCALE,
    }
)


def _fixed_ppm(value: int) -> FixedPointValueV1:
    return FixedPointValueV1.reduced(value, 1_000_000)


def full_day_parameter_snapshot_v1(
    plan: FullDayPlanV1,
    state_runtime: HierarchicalStateRuntimeV1,
    *,
    simulation_time_us: int,
    accepted_shock_effect_batches: Sequence[Sequence[ParameterEffectV1]] = (),
) -> FullDayParameterSnapshotV1:
    """Compose all bounded full-day parameter sources without floats.

    This function deliberately returns values instead of mutating an adapter.
    Each executable consumer remains responsible for applying only the targets
    it owns through its normal configuration interface.
    """

    if type(plan) is not FullDayPlanV1:
        raise TypeError("full-day parameter snapshot requires FullDayPlanV1")
    if type(state_runtime) is not HierarchicalStateRuntimeV1:
        raise TypeError(
            "full-day parameter snapshot requires HierarchicalStateRuntimeV1"
        )
    if (
        type(simulation_time_us) is not int
        or simulation_time_us < 0
        or simulation_time_us > plan.calendar.end_time_us
        or state_runtime.current_time_us != simulation_time_us
    ):
        raise ValueError("parameter snapshot time differs from the state runtime")

    pressure_rows: list[BoundedParameterModifierV1] = []
    for profile in plan.pressure_profiles:
        segment = next(
            (
                row
                for row in profile.segments
                if row.start_us <= simulation_time_us < row.end_us
            ),
            profile.segments[-1]
            if simulation_time_us == plan.calendar.end_time_us
            else None,
        )
        if segment is None:  # pragma: no cover - FullDayPlan coverage prevents it
            raise RuntimeError("pressure profile omits the requested time")
        pressure_rows.append(
            BoundedParameterModifierV1(
                target=_PRESSURE_TARGET_V1[profile.pressure_kind],
                modifier=_fixed_ppm(segment.modifier_ppm),
                minimum=_fixed_ppm(profile.minimum_ppm),
                maximum=_fixed_ppm(profile.maximum_ppm),
            )
        )
    pressure_modifiers = tuple(
        sorted(pressure_rows, key=lambda row: row.target.value)
    )
    day_modifiers = state_runtime.active_modifiers(StateLevelV1.DAY)
    local_modifiers = state_runtime.active_modifiers(StateLevelV1.LOCAL)
    shock_batches = tuple(
        tuple(BoundedParameterModifierV1.from_effect(effect) for effect in batch)
        for batch in accepted_shock_effect_batches
    )
    values: Mapping[ParameterTargetV1, FixedPointValueV1] = MappingProxyType(
        {target: FixedPointValueV1(1, 1) for target in ParameterTargetV1}
    )
    for batch in (
        pressure_modifiers,
        day_modifiers,
        local_modifiers,
        *shock_batches,
    ):
        values = apply_bounded_modifiers_v1(values, batch)
    return FullDayParameterSnapshotV1(
        simulation_time_us=simulation_time_us,
        pressure_modifiers=pressure_modifiers,
        day_state_modifiers=day_modifiers,
        local_state_modifiers=local_modifiers,
        shock_modifier_batches=shock_batches,
        effective_values=values,
    )


@dataclass(frozen=True, slots=True)
class DayStateAnchorEmissionV1:
    """One plan-authoritative macro-segment hard-reset proposal."""

    schema_version: int
    plan_sha256: str
    state_model_sha256: str
    simulation_time_us: int
    microstep: int
    component_local_sequence: int
    macro_segment_index: int
    macro_segment_sha256: str
    previous_state: DayStateV1
    anchored_state: DayStateV1
    sampled_duration_us: int
    next_transition_id: str
    state_modifiers: tuple[BoundedParameterModifierV1, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != DAY_STATE_ANCHOR_EMISSION_SCHEMA_VERSION
        ):
            raise ValueError("day-state anchor emission schema version must be 1")
        for digest, context in (
            (self.plan_sha256, "anchor plan digest"),
            (self.state_model_sha256, "anchor state-model digest"),
            (self.macro_segment_sha256, "anchor macro-segment digest"),
        ):
            if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"{context} must be lowercase SHA-256")
        if (
            type(self.simulation_time_us) is not int
            or self.simulation_time_us < 0
            or type(self.microstep) is not int
            or self.microstep != 0
            or type(self.component_local_sequence) is not int
            or self.component_local_sequence <= 0
            or type(self.macro_segment_index) is not int
            or self.macro_segment_index < 0
            or type(self.sampled_duration_us) is not int
            or self.sampled_duration_us < 0
        ):
            raise ValueError("anchor time/index/sequence/duration fields are invalid")
        if (
            type(self.previous_state) is not DayStateV1
            or type(self.anchored_state) is not DayStateV1
        ):
            raise TypeError("anchor states must use DayStateV1")
        _identifier(self.next_transition_id, "anchor next transition ID")
        if type(self.state_modifiers) is not tuple or any(
            type(item) is not BoundedParameterModifierV1
            for item in self.state_modifiers
        ):
            raise TypeError("anchor state modifiers use the wrong contract")
        targets = tuple(item.target.value for item in self.state_modifiers)
        if targets != tuple(sorted(set(targets))):
            raise ValueError("anchor state modifiers must be unique and sorted")

    @property
    def source_component_id(self) -> str:
        return FULL_DAY_RUNTIME_COMPONENT_ID_V1

    @property
    def stage(self) -> WorkStageV1:
        return WorkStageV1.DAY_STATE_TRANSITION

    @property
    def event_type(self) -> FullDayEventTypeV1:
        return FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET

    @property
    def scheduled_work_key(self) -> ScheduledWorkKeyV1:
        return ScheduledWorkKeyV1(
            simulation_time_us=self.simulation_time_us,
            microstep=self.microstep,
            stage_ordinal=self.stage,
            source_component_id=self.source_component_id,
            component_local_sequence=self.component_local_sequence,
        )

    @property
    def event_key(self) -> tuple[int, int, int, str, int]:
        return self.scheduled_work_key.ordering_key

    def as_dict(self) -> dict[str, object]:
        return {
            "anchored_state": self.anchored_state.value,
            "component_local_sequence": self.component_local_sequence,
            "event_type": self.event_type.value,
            "macro_segment_index": self.macro_segment_index,
            "macro_segment_sha256": self.macro_segment_sha256,
            "microstep": self.microstep,
            "next_transition_id": self.next_transition_id,
            "plan_sha256": self.plan_sha256,
            "previous_state": self.previous_state.value,
            "sampled_duration_us": self.sampled_duration_us,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
            "source_component_id": self.source_component_id,
            "stage": int(self.stage),
            "state_model_sha256": self.state_model_sha256,
            "state_modifiers": [item.as_dict() for item in self.state_modifiers],
        }


@dataclass(frozen=True, slots=True)
class StateTransitionEmissionV1:
    """Plan-derived transition proposal awaiting the global event allocator."""

    schema_version: int
    plan_sha256: str
    state_model_sha256: str
    level: StateLevelV1
    simulation_time_us: int
    microstep: int
    component_local_sequence: int
    transition_id: str
    previous_state: str
    new_state: str
    sampled_duration_us: int
    trigger_id: str
    trigger_version: int
    cause: TransitionCauseV1
    state_modifiers: tuple[BoundedParameterModifierV1, ...]
    transition_modifiers: tuple[BoundedParameterModifierV1, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != STATE_TRANSITION_EMISSION_SCHEMA_VERSION
        ):
            raise ValueError("state transition emission schema version must be 1")
        if (
            type(self.plan_sha256) is not str
            or not _SHA256_RE.fullmatch(self.plan_sha256)
            or type(self.state_model_sha256) is not str
            or not _SHA256_RE.fullmatch(self.state_model_sha256)
        ):
            raise ValueError("emission plan/model digests must be lowercase SHA-256")
        if type(self.level) is not StateLevelV1:
            raise TypeError("emission level uses the wrong enum")
        if (
            type(self.simulation_time_us) is not int
            or self.simulation_time_us < 0
            or type(self.microstep) is not int
            or self.microstep < 0
            or type(self.component_local_sequence) is not int
            or self.component_local_sequence <= 0
            or type(self.sampled_duration_us) is not int
            or self.sampled_duration_us < 0
        ):
            raise ValueError("emission time/sequence/duration fields are invalid")
        for value, context in (
            (self.transition_id, "transition ID"),
            (self.previous_state, "previous state"),
            (self.new_state, "new state"),
            (self.trigger_id, "trigger ID"),
        ):
            _identifier(value, context)
        if self.previous_state == self.new_state:
            raise ValueError("a transition emission must change state")
        if type(self.trigger_version) is not int or self.trigger_version <= 0:
            raise ValueError("emission trigger version must be positive")
        if type(self.cause) is not TransitionCauseV1:
            raise TypeError("emission cause uses the wrong enum")
        for rows, context in (
            (self.state_modifiers, "state modifiers"),
            (self.transition_modifiers, "transition modifiers"),
        ):
            if type(rows) is not tuple or any(
                type(item) is not BoundedParameterModifierV1 for item in rows
            ):
                raise TypeError(f"{context} use the wrong contract")
            targets = tuple(item.target.value for item in rows)
            if targets != tuple(sorted(set(targets))):
                raise ValueError(f"{context} must be unique and canonically ordered")

    @property
    def source_component_id(self) -> str:
        return FULL_DAY_RUNTIME_COMPONENT_ID_V1

    @property
    def stage(self) -> WorkStageV1:
        return (
            WorkStageV1.DAY_STATE_TRANSITION
            if self.level is StateLevelV1.DAY
            else WorkStageV1.LOCAL_STATE_TRANSITION
        )

    @property
    def event_type(self) -> FullDayEventTypeV1:
        return (
            FullDayEventTypeV1.DAY_STATE_TRANSITION
            if self.level is StateLevelV1.DAY
            else FullDayEventTypeV1.LOCAL_STATE_TRANSITION
        )

    @property
    def event_key(self) -> tuple[int, int, int, str, int]:
        return self.scheduled_work_key.ordering_key

    @property
    def scheduled_work_key(self) -> ScheduledWorkKeyV1:
        """Return the exact frozen five-field scheduler identity."""

        return ScheduledWorkKeyV1(
            simulation_time_us=self.simulation_time_us,
            microstep=self.microstep,
            stage_ordinal=self.stage,
            source_component_id=self.source_component_id,
            component_local_sequence=self.component_local_sequence,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "cause": self.cause.value,
            "component_local_sequence": self.component_local_sequence,
            "event_type": self.event_type.value,
            "level": self.level.value,
            "microstep": self.microstep,
            "new_state": self.new_state,
            "plan_sha256": self.plan_sha256,
            "previous_state": self.previous_state,
            "sampled_duration_us": self.sampled_duration_us,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
            "source_component_id": self.source_component_id,
            "stage": int(self.stage),
            "state_model_sha256": self.state_model_sha256,
            "state_modifiers": [item.as_dict() for item in self.state_modifiers],
            "transition_id": self.transition_id,
            "transition_modifiers": [item.as_dict() for item in self.transition_modifiers],
            "trigger_id": self.trigger_id,
            "trigger_version": self.trigger_version,
        }


@dataclass(slots=True)
class _MutableLevelState:
    state: str
    entered_time_us: int
    sampled_duration_us: int
    deadline_time_us: int
    next_transition_id: str
    trigger_memory: list[StateTriggerMemoryV1]


DefinitionV1 = DayStateDefinitionV1 | LocalStateDefinitionV1


class HierarchicalStateRuntimeV1:
    """Owned deterministic state machine for one immutable full-day plan."""

    def __init__(
        self,
        *,
        plan: FullDayPlanV1,
        current_time_us: int,
        input_closed_through_time_us: int | None,
        component_local_sequence: int,
        component_sequence_offset: int,
        runtime_emission_count: int,
        day_transition_count: int,
        local_transition_count: int,
        day_transitions_since_macro_anchor: int,
        next_macro_segment_index: int,
        observation_ids_seen: set[str],
        day: _MutableLevelState,
        local: _MutableLevelState,
        day_rng: _SplitMix64V1,
        local_rng: _SplitMix64V1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("state runtime requires FullDayPlanV1")
        self._plan = plan
        self._plan_sha256 = canonical_sha256(plan.as_dict())
        self._model_sha256 = canonical_sha256(plan.state_model.as_dict())
        self._current_time_us = current_time_us
        self._input_closed_through_time_us = input_closed_through_time_us
        self._component_local_sequence = component_local_sequence
        self._component_sequence_offset = component_sequence_offset
        self._runtime_emission_count = runtime_emission_count
        self._day_transition_count = day_transition_count
        self._local_transition_count = local_transition_count
        self._day_transitions_since_macro_anchor = (
            day_transitions_since_macro_anchor
        )
        self._next_macro_segment_index = next_macro_segment_index
        self._observation_ids_seen = set(observation_ids_seen)
        self._levels = {StateLevelV1.DAY: day, StateLevelV1.LOCAL: local}
        self._rngs = {StateLevelV1.DAY: day_rng, StateLevelV1.LOCAL: local_rng}
        self._definitions: dict[StateLevelV1, dict[str, DefinitionV1]] = {
            StateLevelV1.DAY: {
                item.state.value: item for item in plan.state_model.day_definitions
            },
            StateLevelV1.LOCAL: {
                item.state.value: item for item in plan.state_model.local_definitions
            },
        }

    @classmethod
    def create(
        cls,
        plan: FullDayPlanV1,
        *,
        component_local_sequence: int = 0,
    ) -> HierarchicalStateRuntimeV1:
        """Create the t=0 runtime and sample both initial finite duration laws.

        ``component_local_sequence`` is the already-allocated high-water mark
        for the shared ``FULL_DAY_RUNTIME_V1`` owner.  Subsequent full-day work
        must reserve identities through :meth:`reserve_component_local_sequence`
        so state emissions and calendar/information work cannot collide.
        """

        if type(plan) is not FullDayPlanV1:
            raise TypeError("state runtime requires FullDayPlanV1")
        if (
            type(component_local_sequence) is not int
            or component_local_sequence < 0
        ):
            raise ValueError(
                "initial component-local sequence must be a nonnegative integer"
            )
        model = plan.state_model
        day_rng = _SplitMix64V1.seeded(
            plan.seed_policy.derive(model.day_rng_substream_label),
            model.day_rng_substream_label,
        )
        local_rng = _SplitMix64V1.seeded(
            plan.seed_policy.derive(model.local_rng_substream_label),
            model.local_rng_substream_label,
        )
        day_definition = next(
            item for item in model.day_definitions if item.state is model.initial_day_state
        )
        local_definition = next(
            item
            for item in model.local_definitions
            if item.state is model.initial_local_state
        )
        day_duration = _sample_duration(day_definition.duration_law, day_rng)
        local_duration = _sample_duration(local_definition.duration_law, local_rng)
        day_transition = _sample_transition(day_definition, day_rng)
        local_transition = _sample_transition(local_definition, local_rng)
        return cls(
            plan=plan,
            current_time_us=0,
            input_closed_through_time_us=None,
            component_local_sequence=component_local_sequence,
            component_sequence_offset=component_local_sequence,
            runtime_emission_count=0,
            day_transition_count=0,
            local_transition_count=0,
            day_transitions_since_macro_anchor=0,
            next_macro_segment_index=0,
            observation_ids_seen=set(),
            day=_MutableLevelState(
                model.initial_day_state.value,
                0,
                day_duration,
                day_duration,
                day_transition.transition_id,
                [],
            ),
            local=_MutableLevelState(
                model.initial_local_state.value,
                0,
                local_duration,
                local_duration,
                local_transition.transition_id,
                [],
            ),
            day_rng=day_rng,
            local_rng=local_rng,
        )

    @classmethod
    def from_state(
        cls,
        plan: FullDayPlanV1,
        state: HierarchicalStateRuntimeStateV1,
        *,
        verified_component_local_sequence_floor: int,
    ) -> HierarchicalStateRuntimeV1:
        """Restore only against an authoritative shared-owner sequence floor.

        The floor is derived from the already-verified outer event/work prefix,
        not from the component snapshot being restored.  Requiring it here
        prevents coordinated rollback of the runtime's allocator counters.
        """

        if type(plan) is not FullDayPlanV1:
            raise TypeError("state runtime requires FullDayPlanV1")
        if type(state) is not HierarchicalStateRuntimeStateV1:
            raise TypeError("restore requires HierarchicalStateRuntimeStateV1")
        if (
            type(verified_component_local_sequence_floor) is not int
            or verified_component_local_sequence_floor < 0
        ):
            raise ValueError(
                "verified component-local sequence floor must be nonnegative"
            )
        if (
            state.component_local_sequence
            != verified_component_local_sequence_floor
        ):
            raise ValueError(
                "runtime component-local sequence differs from the verified prefix"
            )
        model_sha256 = canonical_sha256(plan.state_model.as_dict())
        plan_sha256 = canonical_sha256(plan.as_dict())
        if state.plan_sha256 != plan_sha256 or state.state_model_sha256 != model_sha256:
            raise ValueError("runtime state is bound to a different full-day plan")
        expected_seeds = {
            StateLevelV1.DAY: plan.seed_policy.derive(
                plan.state_model.day_rng_substream_label
            ),
            StateLevelV1.LOCAL: plan.seed_policy.derive(
                plan.state_model.local_rng_substream_label
            ),
        }
        expected_labels = {
            StateLevelV1.DAY: plan.state_model.day_rng_substream_label,
            StateLevelV1.LOCAL: plan.state_model.local_rng_substream_label,
        }
        for level, rng_state in (
            (StateLevelV1.DAY, state.day_rng),
            (StateLevelV1.LOCAL, state.local_rng),
        ):
            if (
                rng_state.initial_seed != expected_seeds[level]
                or rng_state.substream_label != expected_labels[level]
            ):
                raise ValueError("runtime RNG state does not belong to the plan substream")
        runtime = cls(
            plan=plan,
            current_time_us=state.current_time_us,
            input_closed_through_time_us=state.input_closed_through_time_us,
            component_local_sequence=state.component_local_sequence,
            component_sequence_offset=state.component_sequence_offset,
            runtime_emission_count=state.runtime_emission_count,
            day_transition_count=state.day_transition_count,
            local_transition_count=state.local_transition_count,
            day_transitions_since_macro_anchor=(
                state.day_transitions_since_macro_anchor
            ),
            next_macro_segment_index=state.next_macro_segment_index,
            observation_ids_seen=set(state.observation_ids_seen),
            day=_MutableLevelState(
                state.day.current_state,
                state.day.entered_time_us,
                state.day.sampled_duration_us,
                state.day.deadline_time_us,
                state.day.next_eligible_transition_id,
                list(state.day.trigger_memory),
            ),
            local=_MutableLevelState(
                state.local.current_state,
                state.local.entered_time_us,
                state.local.sampled_duration_us,
                state.local.deadline_time_us,
                state.local.next_eligible_transition_id,
                list(state.local.trigger_memory),
            ),
            day_rng=_SplitMix64V1(state.day_rng),
            local_rng=_SplitMix64V1(state.local_rng),
        )
        runtime._validate_restored_state(state)
        return runtime

    @property
    def current_time_us(self) -> int:
        return self._current_time_us

    @property
    def component_local_sequence(self) -> int:
        return self._component_local_sequence

    def reserve_component_local_sequence(self) -> int:
        """Reserve the next identity for non-state work of the shared owner.

        Calendar, scheduled-information, checkpoint, and other work sourced by
        ``FULL_DAY_RUNTIME_V1`` must use this gateway between runtime advances.
        The reservation is canonical checkpoint state, not ambient allocator
        state.
        """

        self._component_local_sequence += 1
        self._component_sequence_offset += 1
        return self._component_local_sequence

    def _allocate_runtime_emission_sequence(self) -> int:
        self._component_local_sequence += 1
        self._runtime_emission_count += 1
        return self._component_local_sequence

    def state(self) -> HierarchicalStateRuntimeStateV1:
        """Return a complete strict canonical checkpoint payload."""

        return HierarchicalStateRuntimeStateV1(
            schema_version=STATE_TRANSITION_RUNTIME_SCHEMA_VERSION,
            plan_sha256=self._plan_sha256,
            state_model_sha256=self._model_sha256,
            current_time_us=self._current_time_us,
            input_closed_through_time_us=self._input_closed_through_time_us,
            component_local_sequence=self._component_local_sequence,
            component_sequence_offset=self._component_sequence_offset,
            runtime_emission_count=self._runtime_emission_count,
            day_transition_count=self._day_transition_count,
            local_transition_count=self._local_transition_count,
            day_transitions_since_macro_anchor=(
                self._day_transitions_since_macro_anchor
            ),
            next_macro_segment_index=self._next_macro_segment_index,
            observation_ids_seen=tuple(sorted(self._observation_ids_seen)),
            day=self._level_snapshot(StateLevelV1.DAY),
            local=self._level_snapshot(StateLevelV1.LOCAL),
            day_rng=self._rngs[StateLevelV1.DAY].snapshot(),
            local_rng=self._rngs[StateLevelV1.LOCAL].snapshot(),
        )

    def active_modifiers(
        self, level: StateLevelV1
    ) -> tuple[BoundedParameterModifierV1, ...]:
        """Return the current state's closed, non-imperative modifier set."""

        definition = self._definition(level)
        return tuple(
            BoundedParameterModifierV1.from_effect(item)
            for item in definition.parameter_effects
        )

    def advance_to(
        self,
        target_time_us: int,
        observations: Sequence[TriggerObservationV1] = (),
    ) -> tuple[DayStateAnchorEmissionV1 | StateTransitionEmissionV1, ...]:
        """Advance by simulation time, processing every internal boundary.

        Observations may be supplied for future instants inside this one advance;
        they are queued as input but cannot be read until ``available_time_us``.
        Supplying the same observations over subdivided calls produces the same
        transition/event keys and exact terminal state as one large call.
        """

        before = self.state()
        working = type(self).from_state(
            self._plan,
            before,
            verified_component_local_sequence_floor=(
                before.component_local_sequence
            ),
        )
        emissions = working._advance_to_in_place(target_time_us, observations)
        self._adopt(working)
        return emissions

    def _advance_to_in_place(
        self,
        target_time_us: int,
        observations: Sequence[TriggerObservationV1],
    ) -> tuple[DayStateAnchorEmissionV1 | StateTransitionEmissionV1, ...]:
        if type(target_time_us) is not int or target_time_us < self._current_time_us:
            raise ValueError("state runtime cannot advance backward")
        if target_time_us > self._plan.calendar.end_time_us:
            raise ValueError("state runtime cannot advance beyond the plan calendar")
        pending = tuple(observations)
        if any(type(item) is not TriggerObservationV1 for item in pending):
            raise TypeError("advance observations must use TriggerObservationV1")
        ordered = tuple(
            sorted(
                pending,
                key=lambda item: (
                    item.available_time_us,
                    item.observation_time_us,
                    item.observation_id,
                ),
            )
        )
        ids = tuple(item.observation_id for item in ordered)
        if len(ids) != len(set(ids)) or set(ids).intersection(self._observation_ids_seen):
            raise ValueError("trigger observation IDs must be globally unique")
        for item in ordered:
            if item.phase is not TriggerObservationPhaseV1.PRE_TRANSITION:
                raise ValueError("post-transition and reveal-only trigger reads are forbidden")
            if (
                self._input_closed_through_time_us is not None
                and item.available_time_us <= self._input_closed_through_time_us
            ):
                raise ValueError("a trigger observation cannot enter a closed time frontier")
            if item.available_time_us > target_time_us:
                raise ValueError("advance cannot read an observation beyond its target time")

        emissions: list[DayStateAnchorEmissionV1 | StateTransitionEmissionV1] = []
        cursor = 0
        synchronous_limit = (
            self._plan.deterministic_limits.maximum_synchronous_consequences_per_work_item
        )

        while True:
            next_observation_time = (
                ordered[cursor].available_time_us if cursor < len(ordered) else None
            )
            next_due_time = self._next_due_time()
            next_anchor_time = self._next_macro_anchor_time()
            candidates = [target_time_us]
            if next_observation_time is not None:
                candidates.append(next_observation_time)
            if next_due_time is not None and next_due_time <= target_time_us:
                candidates.append(next_due_time)
            if next_anchor_time is not None and next_anchor_time <= target_time_us:
                candidates.append(next_anchor_time)
            next_time = min(candidates)
            if next_time < self._current_time_us:
                raise RuntimeError("state runtime selected a past internal boundary")
            self._current_time_us = next_time
            synchronous_steps = 0

            # Macro authority replaces the obsolete pre-anchor day state before
            # matching same-time trigger observations.  Local state is untouched,
            # so its stage-3 observation eligibility is preserved.
            obsolete_day_transition = (
                self._selected_transition(StateLevelV1.DAY)
                if next_anchor_time == next_time
                else None
            )
            anchor = self._apply_macro_anchor_if_due()
            if anchor is not None:
                emissions.append(anchor)

            while cursor < len(ordered) and ordered[cursor].available_time_us == next_time:
                observation = ordered[cursor]
                matched = self._accept_observation(observation)
                suppressed_obsolete_day_input = (
                    not matched
                    and anchor is not None
                    and obsolete_day_transition is not None
                    and _observation_matches_transition(
                        observation, obsolete_day_transition
                    )
                )
                if not matched and not suppressed_obsolete_day_input:
                    raise ValueError(
                        "trigger observation is not valid for either current state"
                    )
                self._observation_ids_seen.add(observation.observation_id)
                cursor += 1

            microstep = 0
            while True:
                round_emitted = False
                for level in (StateLevelV1.DAY, StateLevelV1.LOCAL):
                    if (
                        anchor is not None
                        and microstep == 0
                        and level is StateLevelV1.DAY
                    ):
                        continue
                    emission = self._transition_if_due(level, microstep)
                    if emission is not None:
                        if (
                            emission.microstep
                            >= self._plan.deterministic_limits.maximum_microsteps_per_timestamp
                        ):
                            raise RuntimeError(
                                "state transition exceeds the microstep-count bound"
                            )
                        emissions.append(emission)
                        round_emitted = True
                        synchronous_steps += 1
                        if synchronous_steps > synchronous_limit:
                            raise RuntimeError(
                                "state transition synchronous consequence limit exceeded"
                            )
                if not round_emitted:
                    if anchor is not None and microstep == 0:
                        # The stage-2 anchor occupies day microstep zero.  A
                        # zero-duration anchored successor is tested only at
                        # the next microstep, after local stage 3 at step zero.
                        microstep = 1
                        continue
                    break
                microstep += 1

            if self._current_time_us == target_time_us and cursor == len(ordered):
                break
            # A future internal boundary must make progress; due-now work was
            # exhausted by the microstep loop above.
            future_due = self._next_due_time()
            if future_due is not None and future_due <= self._current_time_us:
                raise RuntimeError("state runtime made no progress at an internal boundary")

        self._input_closed_through_time_us = target_time_us
        return tuple(emissions)

    def _adopt(self, other: HierarchicalStateRuntimeV1) -> None:
        """Atomically publish a successfully advanced working copy."""

        self._current_time_us = other._current_time_us
        self._input_closed_through_time_us = other._input_closed_through_time_us
        self._component_local_sequence = other._component_local_sequence
        self._component_sequence_offset = other._component_sequence_offset
        self._runtime_emission_count = other._runtime_emission_count
        self._day_transition_count = other._day_transition_count
        self._local_transition_count = other._local_transition_count
        self._day_transitions_since_macro_anchor = (
            other._day_transitions_since_macro_anchor
        )
        self._next_macro_segment_index = other._next_macro_segment_index
        self._observation_ids_seen = set(other._observation_ids_seen)
        self._levels = other._levels
        self._rngs = other._rngs

    def _definition(self, level: StateLevelV1) -> DefinitionV1:
        if type(level) is not StateLevelV1:
            raise TypeError("state level uses the wrong enum")
        return self._definitions[level][self._levels[level].state]

    def _level_snapshot(self, level: StateLevelV1) -> StateLevelRuntimeStateV1:
        row = self._levels[level]
        return StateLevelRuntimeStateV1(
            level=level,
            as_of_time_us=self._current_time_us,
            current_state=row.state,
            entered_time_us=row.entered_time_us,
            elapsed_age_us=self._current_time_us - row.entered_time_us,
            sampled_duration_us=row.sampled_duration_us,
            deadline_time_us=row.deadline_time_us,
            next_eligible_transition_id=row.next_transition_id,
            next_eligible_transition_time_us=_next_eligible_time(
                self._selected_transition(level), row.entered_time_us
            ),
            trigger_memory=tuple(
                sorted(
                    row.trigger_memory,
                    key=lambda item: (
                        item.observation.available_time_us,
                        item.observation.observation_time_us,
                        item.observation.observation_id,
                    ),
                )
            ),
        )

    def _validate_restored_state(self, state: HierarchicalStateRuntimeStateV1) -> None:
        if state.current_time_us > self._plan.calendar.end_time_us:
            raise ValueError("restored runtime time exceeds the plan calendar")
        segment_count = len(self._plan.macro_regime_schedule)
        if state.next_macro_segment_index > segment_count:
            raise ValueError("restored macro-segment cursor exceeds the plan")
        if state.input_closed_through_time_us is None:
            normalized = replace(
                state,
                component_local_sequence=0,
                component_sequence_offset=0,
            )
            if normalized != type(self).create(self._plan).state():
                raise ValueError(
                    "an unclosed runtime snapshot must be the deterministic pristine state"
                )
            return
        expected_cursor = sum(
            item.start_us <= state.current_time_us
            for item in self._plan.macro_regime_schedule
        )
        if state.next_macro_segment_index != expected_cursor:
            raise ValueError("restored macro-segment cursor does not match runtime time")
        if state.next_macro_segment_index:
            latest_segment = self._plan.macro_regime_schedule[
                state.next_macro_segment_index - 1
            ]
            if state.day_transitions_since_macro_anchor == 0:
                if (
                    state.day.current_state != latest_segment.day_state.value
                    or state.day.entered_time_us != latest_segment.start_us
                ):
                    raise ValueError(
                        "restored day state does not preserve the latest macro anchor"
                    )
            else:
                anchored_definition = self._definitions[StateLevelV1.DAY][
                    latest_segment.day_state.value
                ]
                earliest_first_transition = latest_segment.start_us + min(
                    item.minimum_age_us
                    for item in anchored_definition.transitions
                )
                if state.day.entered_time_us < earliest_first_transition:
                    raise ValueError(
                        "restored day transition history predates macro eligibility"
                    )
        for level, snapshot in (
            (StateLevelV1.DAY, state.day),
            (StateLevelV1.LOCAL, state.local),
        ):
            if snapshot.current_state not in self._definitions[level]:
                raise ValueError("restored current state is absent from the plan model")
            definition = self._definition(level)
            allowed_durations = {item.duration_us for item in definition.duration_law.masses}
            if snapshot.sampled_duration_us not in allowed_durations:
                raise ValueError("restored sampled duration is outside the finite duration law")
            transition_ids = {item.transition_id for item in definition.transitions}
            if snapshot.next_eligible_transition_id not in transition_ids:
                raise ValueError("restored next transition is not owned by the current state")
            selected = next(
                item
                for item in definition.transitions
                if item.transition_id == snapshot.next_eligible_transition_id
            )
            expected_next = _next_eligible_time(selected, snapshot.entered_time_us)
            if snapshot.next_eligible_transition_time_us != expected_next:
                raise ValueError("restored next-eligible time does not reconcile with the plan")
            for memory in snapshot.trigger_memory:
                if not _observation_matches_transition(memory.observation, selected):
                    raise ValueError(
                        "restored trigger memory does not match the selected transition"
                    )
                if memory.observation.phase is not TriggerObservationPhaseV1.PRE_TRANSITION:
                    raise ValueError("restored trigger memory contains forbidden information")
            eligible_time = snapshot.entered_time_us + selected.minimum_age_us
            exhaustion_due = (
                selected.duration_exhaustion_behavior
                is DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
                and snapshot.deadline_time_us <= state.current_time_us
                and eligible_time <= state.current_time_us
            )
            trigger_due = (
                eligible_time <= state.current_time_us
                and any(memory.observation.triggered for memory in snapshot.trigger_memory)
            )
            if exhaustion_due or trigger_due:
                raise ValueError(
                    "restored closed frontier contains an unconsumed due transition"
                )

    def _next_macro_anchor_time(self) -> int | None:
        if self._next_macro_segment_index >= len(self._plan.macro_regime_schedule):
            return None
        return self._plan.macro_regime_schedule[
            self._next_macro_segment_index
        ].start_us

    def _apply_macro_anchor_if_due(self) -> DayStateAnchorEmissionV1 | None:
        anchor_time = self._next_macro_anchor_time()
        if anchor_time != self._current_time_us:
            return None
        index = self._next_macro_segment_index
        segment = self._plan.macro_regime_schedule[index]
        row = self._levels[StateLevelV1.DAY]
        previous_state = DayStateV1(row.state)
        anchored_definition = self._definitions[StateLevelV1.DAY][
            segment.day_state.value
        ]
        if index == 0:
            # ``create`` prepared the t=0 hard reset once; publishing the anchor
            # must not consume a second pair of duration/transition draws.
            if (
                self._current_time_us != 0
                or row.state != segment.day_state.value
                or row.entered_time_us != 0
            ):
                raise RuntimeError("prepared initial macro anchor is inconsistent")
        else:
            rng = self._rngs[StateLevelV1.DAY]
            duration = _sample_duration(anchored_definition.duration_law, rng)
            next_transition = _sample_transition(anchored_definition, rng)
            row.state = segment.day_state.value
            row.entered_time_us = self._current_time_us
            row.sampled_duration_us = duration
            row.deadline_time_us = self._current_time_us + duration
            row.next_transition_id = next_transition.transition_id
            row.trigger_memory.clear()
        selected = self._selected_transition(StateLevelV1.DAY)
        self._day_transitions_since_macro_anchor = 0
        emission_sequence = self._allocate_runtime_emission_sequence()
        self._next_macro_segment_index += 1
        return DayStateAnchorEmissionV1(
            schema_version=DAY_STATE_ANCHOR_EMISSION_SCHEMA_VERSION,
            plan_sha256=self._plan_sha256,
            state_model_sha256=self._model_sha256,
            simulation_time_us=self._current_time_us,
            microstep=0,
            component_local_sequence=emission_sequence,
            macro_segment_index=index,
            macro_segment_sha256=canonical_sha256(segment.as_dict()),
            previous_state=previous_state,
            anchored_state=segment.day_state,
            sampled_duration_us=row.sampled_duration_us,
            next_transition_id=selected.transition_id,
            state_modifiers=tuple(
                BoundedParameterModifierV1.from_effect(item)
                for item in anchored_definition.parameter_effects
            ),
        )

    def _accept_observation(self, observation: TriggerObservationV1) -> bool:
        if (
            observation.observation_time_us > self._current_time_us
            or observation.information_cutoff_us > self._current_time_us
        ):
            raise ValueError("trigger cannot read a future observation")
        matched = False
        for level in (StateLevelV1.DAY, StateLevelV1.LOCAL):
            transition = self._selected_transition(level)
            if _observation_matches_transition(observation, transition):
                row = self._levels[level]
                row.trigger_memory.append(
                    StateTriggerMemoryV1(
                        row.state,
                        row.entered_time_us,
                        observation,
                    )
                )
                matched = True
        return matched

    def _next_due_time(self) -> int | None:
        due: list[int] = []
        for level in (StateLevelV1.DAY, StateLevelV1.LOCAL):
            row = self._levels[level]
            transition = self._selected_transition(level)
            eligible_time = row.entered_time_us + transition.minimum_age_us
            if (
                transition.duration_exhaustion_behavior
                is DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
            ):
                due.append(max(row.deadline_time_us, eligible_time, self._current_time_us))
            if any(
                memory.observation.triggered
                and _observation_matches_transition(memory.observation, transition)
                for memory in row.trigger_memory
            ):
                due.append(max(eligible_time, self._current_time_us))
        return min(due) if due else None

    def _transition_if_due(
        self, level: StateLevelV1, microstep: int
    ) -> StateTransitionEmissionV1 | None:
        row = self._levels[level]
        transition = self._selected_transition(level)
        if self._current_time_us < row.entered_time_us + transition.minimum_age_us:
            return None
        triggered = any(
            memory.observation.triggered
            and _observation_matches_transition(memory.observation, transition)
            for memory in row.trigger_memory
        )
        exhausted = (
            transition.duration_exhaustion_behavior
            is DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
            and self._current_time_us >= row.deadline_time_us
        )
        if not triggered and not exhausted:
            return None
        cause = (
            TransitionCauseV1.TRIGGER
            if triggered
            else TransitionCauseV1.DURATION_EXHAUSTION
        )
        rng = self._rngs[level]
        previous_state = row.state
        successor = self._definitions[level][transition.successor_state]
        sampled_duration = _sample_duration(successor.duration_law, rng)
        next_transition = _sample_transition(successor, rng)
        row.state = transition.successor_state
        row.entered_time_us = self._current_time_us
        row.sampled_duration_us = sampled_duration
        row.deadline_time_us = self._current_time_us + sampled_duration
        row.next_transition_id = next_transition.transition_id
        row.trigger_memory.clear()
        if level is StateLevelV1.DAY:
            self._day_transition_count += 1
            self._day_transitions_since_macro_anchor += 1
        else:
            self._local_transition_count += 1
        emission_sequence = self._allocate_runtime_emission_sequence()
        return StateTransitionEmissionV1(
            schema_version=STATE_TRANSITION_EMISSION_SCHEMA_VERSION,
            plan_sha256=self._plan_sha256,
            state_model_sha256=self._model_sha256,
            level=level,
            simulation_time_us=self._current_time_us,
            microstep=microstep,
            component_local_sequence=emission_sequence,
            transition_id=transition.transition_id,
            previous_state=previous_state,
            new_state=transition.successor_state,
            sampled_duration_us=sampled_duration,
            trigger_id=transition.trigger_id,
            trigger_version=transition.trigger_version,
            cause=cause,
            state_modifiers=tuple(
                BoundedParameterModifierV1.from_effect(item)
                for item in successor.parameter_effects
            ),
            transition_modifiers=tuple(
                BoundedParameterModifierV1.from_effect(item)
                for item in transition.parameter_effects
            ),
        )

    def _selected_transition(self, level: StateLevelV1) -> StateTransitionV1:
        definition = self._definition(level)
        matches = tuple(
            item
            for item in definition.transitions
            if item.transition_id == self._levels[level].next_transition_id
        )
        if len(matches) != 1:
            raise RuntimeError("selected transition does not resolve in current state")
        return matches[0]


def _next_eligible_time(
    transition: StateTransitionV1, entered_time_us: int
) -> int:
    return entered_time_us + transition.minimum_age_us


def _matching_transitions(
    definition: DefinitionV1,
    observation: TriggerObservationV1,
) -> tuple[StateTransitionV1, ...]:
    return tuple(
        item
        for item in definition.transitions
        if _observation_matches_transition(observation, item)
    )


def _observation_matches_transition(
    observation: TriggerObservationV1,
    transition: StateTransitionV1,
) -> bool:
    return (
        observation.transition_id == transition.transition_id
        and observation.trigger_id == transition.trigger_id
        and observation.trigger_version == transition.trigger_version
        and observation.trigger_parameter_set_sha256
        == trigger_parameter_set_sha256_v1(transition)
        and observation.information_class is transition.trigger_information_class
    )


def trigger_parameter_set_sha256_v1(transition: StateTransitionV1) -> str:
    """Return the canonical digest that binds an evaluated trigger input."""

    if type(transition) is not StateTransitionV1:
        raise TypeError("trigger parameter digest requires StateTransitionV1")
    return canonical_sha256(
        [item.as_dict() for item in transition.trigger_parameters]
    )


def _sample_weighted_index(weights: tuple[int, ...], rng: _SplitMix64V1) -> int:
    if not weights or any(type(weight) is not int or weight <= 0 for weight in weights):
        raise ValueError("weighted sampling requires positive integer weights")
    draw = rng.randbelow(sum(weights))
    cumulative = 0
    for index, weight in enumerate(weights):
        cumulative += weight
        if draw < cumulative:
            return index
    raise RuntimeError("weighted state draw exceeded its integer mass")


def _sample_duration(law: DurationLawV1, rng: _SplitMix64V1) -> int:
    if type(law) is not DurationLawV1:
        raise TypeError("duration sampling requires DurationLawV1")
    index = _sample_weighted_index(tuple(item.weight for item in law.masses), rng)
    sampled = law.masses[index].duration_us
    if not law.minimum_us <= sampled <= law.maximum_us:
        raise RuntimeError("sampled duration escaped its declared finite bounds")
    return sampled


def _sample_transition(
    definition: DefinitionV1, rng: _SplitMix64V1
) -> StateTransitionV1:
    index = _sample_weighted_index(
        tuple(item.weight for item in definition.transitions),
        rng,
    )
    return definition.transitions[index]


def project_transition_payload_v1(
    emission: StateTransitionEmissionV1,
    *,
    plan: FullDayPlanV1,
) -> FullDayEventPayloadV1:
    """Project a runtime emission into the frozen plan-bound outer payload.

    This helper intentionally does not allocate a global sequence, work key,
    causal parent, or outer envelope.
    """

    if type(emission) is not StateTransitionEmissionV1:
        raise TypeError("payload projection requires StateTransitionEmissionV1")
    if type(plan) is not FullDayPlanV1:
        raise TypeError("payload projection requires FullDayPlanV1")
    if emission.simulation_time_us > plan.calendar.end_time_us:
        raise ValueError("transition emission exceeds the plan calendar")
    if emission.plan_sha256 != canonical_sha256(plan.as_dict()):
        raise ValueError("transition emission belongs to a different full-day plan")
    model_sha256 = canonical_sha256(plan.state_model.as_dict())
    if emission.state_model_sha256 != model_sha256:
        raise ValueError("transition emission belongs to a different state model")
    definitions: tuple[DefinitionV1, ...] = (
        plan.state_model.day_definitions
        if emission.level is StateLevelV1.DAY
        else plan.state_model.local_definitions
    )
    matches = tuple(
        transition
        for definition in definitions
        for transition in definition.transitions
        if transition.transition_id == emission.transition_id
    )
    if len(matches) != 1:
        raise ValueError("transition emission ID does not resolve uniquely in the plan")
    transition = matches[0]
    if (
        transition.source_state != emission.previous_state
        or transition.successor_state != emission.new_state
        or transition.trigger_id != emission.trigger_id
        or transition.trigger_version != emission.trigger_version
    ):
        raise ValueError("transition emission fields do not match the plan transition")
    successor = next(
        item for item in definitions if item.state.value == emission.new_state
    )
    if emission.sampled_duration_us not in {
        item.duration_us for item in successor.duration_law.masses
    }:
        raise ValueError("transition emission duration is outside the successor law")
    expected_state_modifiers = tuple(
        BoundedParameterModifierV1.from_effect(item)
        for item in successor.parameter_effects
    )
    expected_transition_modifiers = tuple(
        BoundedParameterModifierV1.from_effect(item)
        for item in transition.parameter_effects
    )
    if emission.state_modifiers != expected_state_modifiers:
        raise ValueError("transition emission state modifiers differ from the plan")
    if emission.transition_modifiers != expected_transition_modifiers:
        raise ValueError(
            "transition emission edge modifiers differ from the plan"
        )
    return FullDayEventPayloadV1(
        schema_version=FULL_DAY_PAYLOAD_SCHEMA_VERSION,
        payload_type=emission.event_type.value,
        payload_version=1,
        native_event=None,
        data={
            "entered_time_us": emission.simulation_time_us,
            "new_state": emission.new_state,
            "previous_state": emission.previous_state,
            "sampled_duration_us": emission.sampled_duration_us,
            "transition_id": emission.transition_id,
            "trigger_id": emission.trigger_id,
            "trigger_version": emission.trigger_version,
        },
    )


def project_anchor_payload_v1(
    emission: DayStateAnchorEmissionV1,
    *,
    plan: FullDayPlanV1,
) -> FullDayEventPayloadV1:
    """Project a plan-bound macro reset without allocating an outer event."""

    if type(emission) is not DayStateAnchorEmissionV1:
        raise TypeError("anchor payload projection requires DayStateAnchorEmissionV1")
    if type(plan) is not FullDayPlanV1:
        raise TypeError("anchor payload projection requires FullDayPlanV1")
    if emission.simulation_time_us > plan.calendar.end_time_us:
        raise ValueError("day-state anchor exceeds the plan calendar")
    if emission.plan_sha256 != canonical_sha256(plan.as_dict()):
        raise ValueError("day-state anchor belongs to a different full-day plan")
    if emission.state_model_sha256 != canonical_sha256(plan.state_model.as_dict()):
        raise ValueError("day-state anchor belongs to a different state model")
    if emission.macro_segment_index >= len(plan.macro_regime_schedule):
        raise ValueError("day-state anchor segment index exceeds the plan")
    segment = plan.macro_regime_schedule[emission.macro_segment_index]
    if (
        emission.macro_segment_sha256 != canonical_sha256(segment.as_dict())
        or emission.simulation_time_us != segment.start_us
        or emission.anchored_state is not segment.day_state
    ):
        raise ValueError("day-state anchor fields do not reconcile with the plan")
    definition = next(
        item
        for item in plan.state_model.day_definitions
        if item.state is emission.anchored_state
    )
    if emission.sampled_duration_us not in {
        item.duration_us for item in definition.duration_law.masses
    }:
        raise ValueError("anchor duration is outside the anchored-state law")
    if emission.next_transition_id not in {
        item.transition_id for item in definition.transitions
    }:
        raise ValueError("anchor next transition is absent from the anchored state")
    expected_state_modifiers = tuple(
        BoundedParameterModifierV1.from_effect(item)
        for item in definition.parameter_effects
    )
    if emission.state_modifiers != expected_state_modifiers:
        raise ValueError("anchor state modifiers differ from the plan")
    return FullDayEventPayloadV1(
        schema_version=FULL_DAY_PAYLOAD_SCHEMA_VERSION,
        payload_type=FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET.value,
        payload_version=1,
        native_event=None,
        data={
            "anchored_state": emission.anchored_state.value,
            "entered_time_us": emission.simulation_time_us,
            "macro_segment_index": emission.macro_segment_index,
            "macro_segment_sha256": emission.macro_segment_sha256,
            "previous_state": emission.previous_state.value,
            "sampled_duration_us": emission.sampled_duration_us,
        },
    )


__all__ = [
    "BoundedParameterModifierV1",
    "DAY_STATE_ANCHOR_EMISSION_SCHEMA_VERSION",
    "DayStateAnchorEmissionV1",
    "FixedPointValueV1",
    "FullDayParameterSnapshotV1",
    "FULL_DAY_RUNTIME_COMPONENT_ID_V1",
    "HierarchicalStateRuntimeStateV1",
    "HierarchicalStateRuntimeV1",
    "STATE_TRANSITION_EMISSION_SCHEMA_VERSION",
    "STATE_TRANSITION_RUNTIME_SCHEMA_VERSION",
    "StateLevelRuntimeStateV1",
    "StateLevelV1",
    "StateTransitionEmissionV1",
    "StateTriggerMemoryV1",
    "TRANSITION_RNG_ALGORITHM_V1",
    "TRIGGER_OBSERVATION_SCHEMA_VERSION",
    "TransitionCauseV1",
    "TransitionRngStateV1",
    "TriggerObservationPhaseV1",
    "TriggerObservationV1",
    "apply_bounded_modifiers_v1",
    "full_day_parameter_snapshot_v1",
    "project_transition_payload_v1",
    "project_anchor_payload_v1",
    "trigger_parameter_set_sha256_v1",
]
