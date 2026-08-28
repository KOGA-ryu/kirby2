"""Portable, strict-JSON runtime checkpoint envelopes.

This module binds the neutral component-state vocabulary to the frozen full-day
composition, checkpoint inventory, quiescent-cut, and outer-event contracts.  It
does not restore a runtime or write an artifact; those are later responsibilities.
"""

from __future__ import annotations

import hashlib
import platform
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType

from kirby2 import __version__
from kirby2.research.models import ArtifactReference
from kirby2.runtime_state import (
    RuntimeComponentStateV1,
    RuntimeComponentStatusV1,
    validate_runtime_component_inventory,
)

from .checkpoint_contract import (
    CheckpointInventoryV1,
    QuiescentCutV1,
    validate_checkpoint_capture,
    validate_checkpoint_component_state_keys,
)
from .composition import CompositionMatrixV1
from .events import (
    FullDayEventPayloadV1,
    FullDayEventTypeV1,
    FullDayEventV1,
    ScheduledWorkKeyV1,
    WorkStageV1,
    canonical_event_prefix_sha256,
)
from .models import (
    FullDayPlanV1,
    RNG_LABEL_PREFIXES_BY_COMPONENT_V1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)


RUNTIME_CHECKPOINT_SCHEMA_VERSION = 1
RUNTIME_CHECKPOINT_FORMAT_ID = "KIRBY2_RUNTIME_CHECKPOINT_V1"
ENGINE_RUNTIME_COMPATIBILITY_SCHEMA_VERSION = 1
ENGINE_CHECKPOINT_ABI_VERSION = 1
ENGINE_ID = "KIRBY2"
ABSENT_NATIVE_PLAN = "ABSENT_NATIVE_PLAN"
OWNED_PRNG_STATE_SCHEMA_VERSION = 1
OWNED_PRNG_CODEC_VERSION = 1
OWNED_PRNG_CODEC_REGISTRY_ID = "KIRBY2_OWNED_PRNG_CODECS_V1"
SPLITMIX64_ALGORITHM_ID = "SPLITMIX64_V1"
SPLITMIX64_CODEC_ID = "KIRBY2_SPLITMIX64_STATE_V1"
CPYTHON_MT19937_ALGORITHM_ID = "CPYTHON_MT19937_V1"
CPYTHON_MT19937_CODEC_ID = "CPYTHON_RANDOM_STATE_V3_INTEGER_V1"
CHECKPOINT_COMPOSITION_EXPECTATION_SCHEMA_VERSION = 1
CHECKPOINT_MEDIA_TYPE = "application/vnd.kirby2.runtime-checkpoint+json"
ABSENT_REASON_PROFILE_OMITS_STATE_OWNER = (
    "COMPOSITION_PROFILE_OMITS_STATE_OWNER"
)
SUPPORTED_CHECKPOINT_IMPLEMENTATION_VERSIONS_V1 = MappingProxyType(
    {
        "AGENT_SCHEDULER_V1": frozenset({1}),
        "ENGINE_MARKET_MECHANICS_V1": frozenset({1}),
        "FULL_DAY_RUNTIME_V1": frozenset({1}),
    }
)
SUPPORTED_CHECKPOINT_COMPOSITION_PROFILES_V1 = MappingProxyType(
    {
        ("SINGLE_VENUE_AGENT_MECHANICS_V1", 1): (
            "f2b7df6f4299c94ad59969472d0291365d56cce81559f20bc2f4a78c9ec03c31"
        )
    }
)

_MASK_32 = (1 << 32) - 1
_MASK_64 = (1 << 64) - 1
_SPLITMIX64_INCREMENT = 0x9E3779B97F4A7C15
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,191}$")
_CHECKPOINT_ID_RE = re.compile(r"^checkpoint-[0-9a-f]{24}$")
_WORK_PARENT_RE = re.compile(r"^work:[0-9a-f]{64}$")
_EVENT_PARENT_RE = re.compile(r"^event:([1-9][0-9]*)$")
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10))}
)

_RNG_STATE_FIELDS = frozenset(
    {
        "agent.rng_states",
        "delivery.latency.rng_state",
        "flow.hawkes.rng_state",
        "flow.queue_reactive.rng_state",
        "flow.simple.rng_state",
        "multivenue.venue_latency_rng_states",
        "runtime.rng_states",
    }
)

# These V1 emissions are scheduled directly by the deterministic plan/runtime
# controllers.  Their outer component-local sequence is the reserved sequence in
# the five-field ScheduledWorkKeyV1, so a checkpoint validator can reconstruct
# the causal work identity without trusting a caller-supplied executed-work map.
_RECONSTRUCTIBLE_DIRECT_WORK_EVENT_TYPES_V1 = frozenset(
    {
        FullDayEventTypeV1.CALENDAR_BOUNDARY,
        FullDayEventTypeV1.SCHEDULED_INFORMATION,
        FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
        FullDayEventTypeV1.DAY_STATE_TRANSITION,
        FullDayEventTypeV1.LOCAL_STATE_TRANSITION,
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
        FullDayEventTypeV1.PARTICIPANT_RETUNED,
        FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
    }
)


def _exact_int(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be an integer <= {maximum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _identifier(value: object, field: str) -> str:
    selected = _exact_string(value, field)
    if _IDENTIFIER_RE.fullmatch(selected) is None:
        raise ValueError(f"{field} must be a stable identifier")
    return selected


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be an immutable tuple")
    result = tuple(_identifier(item, f"{field}[{index}]") for index, item in enumerate(value))
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError(f"{field} must be sorted and unique")
    return result


def _wire_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"serialized {field} must be an array")
    return _string_tuple(tuple(value), field)


@dataclass(frozen=True, slots=True)
class EngineRuntimeCompatibilityV1:
    """Exact engine and interpreter ABI required to interpret checkpoint state."""

    schema_version: int
    engine_id: str
    engine_version: str
    checkpoint_abi_version: int
    python_implementation: str
    python_major: int
    python_minor: int

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version", minimum=1)
            != ENGINE_RUNTIME_COMPATIBILITY_SCHEMA_VERSION
        ):
            raise ValueError("EngineRuntimeCompatibilityV1 schema_version must be 1")
        if self.engine_id != ENGINE_ID:
            raise ValueError("checkpoint engine ID is unsupported")
        if _exact_string(self.engine_version, "engine_version") != __version__:
            raise ValueError("checkpoint engine version is incompatible")
        if (
            _exact_int(
                self.checkpoint_abi_version,
                "checkpoint_abi_version",
                minimum=1,
            )
            != ENGINE_CHECKPOINT_ABI_VERSION
        ):
            raise ValueError("checkpoint ABI version is unsupported")
        if self.python_implementation != "CPython":
            raise ValueError("only CPython checkpoint state is supported")
        _exact_int(self.python_major, "python_major", minimum=0)
        _exact_int(self.python_minor, "python_minor", minimum=0)
        current = self.current()
        if (
            self.python_implementation,
            self.python_major,
            self.python_minor,
        ) != (
            current.python_implementation,
            current.python_major,
            current.python_minor,
        ):
            raise ValueError("checkpoint Python runtime is incompatible")

    @classmethod
    def current(cls) -> EngineRuntimeCompatibilityV1:
        implementation = platform.python_implementation()
        if implementation != "CPython":
            raise RuntimeError("portable runtime checkpoints currently require CPython")
        return cls.__new_current(
            implementation=implementation,
            major=sys.version_info.major,
            minor=sys.version_info.minor,
        )

    @classmethod
    def __new_current(
        cls,
        *,
        implementation: str,
        major: int,
        minor: int,
    ) -> EngineRuntimeCompatibilityV1:
        # Bypass the recursive call from __post_init__ while retaining one public
        # constructor that validates all externally supplied records.
        result = object.__new__(cls)
        object.__setattr__(result, "schema_version", 1)
        object.__setattr__(result, "engine_id", ENGINE_ID)
        object.__setattr__(result, "engine_version", __version__)
        object.__setattr__(result, "checkpoint_abi_version", ENGINE_CHECKPOINT_ABI_VERSION)
        object.__setattr__(result, "python_implementation", implementation)
        object.__setattr__(result, "python_major", major)
        object.__setattr__(result, "python_minor", minor)
        return result

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_abi_version": self.checkpoint_abi_version,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "python_implementation": self.python_implementation,
            "python_major": self.python_major,
            "python_minor": self.python_minor,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> EngineRuntimeCompatibilityV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "checkpoint_abi_version",
                "engine_id",
                "engine_version",
                "python_implementation",
                "python_major",
                "python_minor",
                "schema_version",
            },
            "EngineRuntimeCompatibilityV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version", minimum=1),
            engine_id=_exact_string(payload["engine_id"], "engine_id"),
            engine_version=_exact_string(payload["engine_version"], "engine_version"),
            checkpoint_abi_version=_exact_int(
                payload["checkpoint_abi_version"],
                "checkpoint_abi_version",
                minimum=1,
            ),
            python_implementation=_exact_string(
                payload["python_implementation"], "python_implementation"
            ),
            python_major=_exact_int(payload["python_major"], "python_major"),
            python_minor=_exact_int(payload["python_minor"], "python_minor"),
        )


@dataclass(frozen=True, slots=True)
class OwnedPrngStateV1:
    """Integer-only state for one explicitly owned supported PRNG substream."""

    schema_version: int
    substream_id: str
    algorithm_id: str
    codec_id: str
    codec_version: int
    initial_seed: int
    state_u64: int | None = None
    draw_count: int | None = None
    sample_count: int | None = None
    python_implementation: str | None = None
    python_major: int | None = None
    python_minor: int | None = None
    random_state_version: int | None = None
    state_words: tuple[int, ...] | None = None
    state_index: int | None = None
    gaussian_cache_u64: int | None = None

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version", minimum=1)
            != OWNED_PRNG_STATE_SCHEMA_VERSION
        ):
            raise ValueError("OwnedPrngStateV1 schema_version must be 1")
        _identifier(self.substream_id, "substream_id")
        _exact_int(self.codec_version, "codec_version", minimum=1)
        _exact_int(self.initial_seed, "initial_seed", maximum=(1 << 63) - 1)
        if self.algorithm_id == SPLITMIX64_ALGORITHM_ID:
            self._validate_splitmix64()
            return
        if self.algorithm_id == CPYTHON_MT19937_ALGORITHM_ID:
            self._validate_cpython_mt19937()
            return
        raise ValueError("owned PRNG algorithm is unsupported")

    def _validate_splitmix64(self) -> None:
        if (
            self.codec_id != SPLITMIX64_CODEC_ID
            or self.codec_version != OWNED_PRNG_CODEC_VERSION
        ):
            raise ValueError("SplitMix64 state codec is unsupported")
        state_u64 = _exact_int(self.state_u64, "state_u64", maximum=_MASK_64)
        draw_count = _exact_int(self.draw_count, "draw_count")
        sample_count = _exact_int(self.sample_count, "sample_count")
        if sample_count > draw_count:
            raise ValueError("SplitMix64 sample count exceeds draw count")
        expected_state = (
            self.initial_seed + draw_count * _SPLITMIX64_INCREMENT
        ) & _MASK_64
        if state_u64 != expected_state:
            raise ValueError("SplitMix64 state does not reconcile with draw count")
        if any(
            value is not None
            for value in (
                self.python_implementation,
                self.python_major,
                self.python_minor,
                self.random_state_version,
                self.state_words,
                self.state_index,
                self.gaussian_cache_u64,
            )
        ):
            raise ValueError("SplitMix64 state carries CPython-only fields")

    def _validate_cpython_mt19937(self) -> None:
        if (
            self.codec_id != CPYTHON_MT19937_CODEC_ID
            or self.codec_version != OWNED_PRNG_CODEC_VERSION
        ):
            raise ValueError("CPython MT19937 state codec is unsupported")
        current = EngineRuntimeCompatibilityV1.current()
        if (
            self.python_implementation,
            self.python_major,
            self.python_minor,
        ) != (
            current.python_implementation,
            current.python_major,
            current.python_minor,
        ):
            raise ValueError("CPython MT19937 state belongs to another runtime")
        if self.random_state_version != 3:
            raise ValueError("CPython random state version is unsupported")
        if type(self.state_words) is not tuple or len(self.state_words) != 624:
            raise ValueError("CPython MT19937 state requires exactly 624 words")
        for index, word in enumerate(self.state_words):
            _exact_int(word, f"state_words[{index}]", maximum=_MASK_32)
        if not any(self.state_words):
            raise ValueError("CPython MT19937 all-zero state is invalid")
        _exact_int(self.state_index, "state_index", maximum=624)
        if self.gaussian_cache_u64 is not None:
            bits = _exact_int(
                self.gaussian_cache_u64,
                "gaussian_cache_u64",
                maximum=_MASK_64,
            )
            if bits == 0x8000000000000000:
                raise ValueError("Gaussian cache bits must not encode negative zero")
            if ((bits >> 52) & 0x7FF) == 0x7FF:
                raise ValueError("Gaussian cache bits must encode a finite binary64")
        if any(
            value is not None
            for value in (self.state_u64, self.draw_count, self.sample_count)
        ):
            raise ValueError("CPython MT19937 state carries SplitMix64-only fields")

    @classmethod
    def splitmix64(
        cls,
        *,
        substream_id: str,
        initial_seed: int,
        state_u64: int,
        draw_count: int,
        sample_count: int,
    ) -> OwnedPrngStateV1:
        return cls(
            schema_version=1,
            substream_id=substream_id,
            algorithm_id=SPLITMIX64_ALGORITHM_ID,
            codec_id=SPLITMIX64_CODEC_ID,
            codec_version=1,
            initial_seed=initial_seed,
            state_u64=state_u64,
            draw_count=draw_count,
            sample_count=sample_count,
        )

    @classmethod
    def cpython_mt19937(
        cls,
        *,
        substream_id: str,
        initial_seed: int,
        state_words: tuple[int, ...],
        state_index: int,
        gaussian_cache_u64: int | None = None,
    ) -> OwnedPrngStateV1:
        current = EngineRuntimeCompatibilityV1.current()
        return cls(
            schema_version=1,
            substream_id=substream_id,
            algorithm_id=CPYTHON_MT19937_ALGORITHM_ID,
            codec_id=CPYTHON_MT19937_CODEC_ID,
            codec_version=1,
            initial_seed=initial_seed,
            python_implementation=current.python_implementation,
            python_major=current.python_major,
            python_minor=current.python_minor,
            random_state_version=3,
            state_words=state_words,
            state_index=state_index,
            gaussian_cache_u64=gaussian_cache_u64,
        )

    def as_dict(self) -> dict[str, object]:
        common: dict[str, object] = {
            "algorithm_id": self.algorithm_id,
            "codec_id": self.codec_id,
            "codec_version": self.codec_version,
            "initial_seed": self.initial_seed,
            "schema_version": self.schema_version,
            "substream_id": self.substream_id,
        }
        if self.algorithm_id == SPLITMIX64_ALGORITHM_ID:
            common.update(
                {
                    "draw_count": self.draw_count,
                    "sample_count": self.sample_count,
                    "state_u64": self.state_u64,
                }
            )
        else:
            common.update(
                {
                    "gaussian_cache_u64": self.gaussian_cache_u64,
                    "python_implementation": self.python_implementation,
                    "python_major": self.python_major,
                    "python_minor": self.python_minor,
                    "random_state_version": self.random_state_version,
                    "state_index": self.state_index,
                    "state_words": list(self.state_words or ()),
                }
            )
        return common

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> OwnedPrngStateV1:
        validate_strict_json(payload)
        if not isinstance(payload, Mapping):
            raise TypeError("serialized OwnedPrngStateV1 must be an object")
        algorithm = payload.get("algorithm_id")
        if algorithm == SPLITMIX64_ALGORITHM_ID:
            _require_exact_fields(
                payload,
                {
                    "algorithm_id",
                    "codec_id",
                    "codec_version",
                    "draw_count",
                    "initial_seed",
                    "sample_count",
                    "schema_version",
                    "state_u64",
                    "substream_id",
                },
                "OwnedPrngStateV1.SPLITMIX64",
            )
            return cls(
                schema_version=_exact_int(payload["schema_version"], "schema_version", minimum=1),
                substream_id=_identifier(payload["substream_id"], "substream_id"),
                algorithm_id=_exact_string(payload["algorithm_id"], "algorithm_id"),
                codec_id=_exact_string(payload["codec_id"], "codec_id"),
                codec_version=_exact_int(payload["codec_version"], "codec_version", minimum=1),
                initial_seed=_exact_int(payload["initial_seed"], "initial_seed"),
                state_u64=_exact_int(payload["state_u64"], "state_u64"),
                draw_count=_exact_int(payload["draw_count"], "draw_count"),
                sample_count=_exact_int(payload["sample_count"], "sample_count"),
            )
        if algorithm == CPYTHON_MT19937_ALGORITHM_ID:
            _require_exact_fields(
                payload,
                {
                    "algorithm_id",
                    "codec_id",
                    "codec_version",
                    "gaussian_cache_u64",
                    "initial_seed",
                    "python_implementation",
                    "python_major",
                    "python_minor",
                    "random_state_version",
                    "schema_version",
                    "state_index",
                    "state_words",
                    "substream_id",
                },
                "OwnedPrngStateV1.CPYTHON_MT19937",
            )
            words = payload["state_words"]
            if type(words) is not list:
                raise TypeError("serialized state_words must be an array")
            gaussian = payload["gaussian_cache_u64"]
            if gaussian is not None:
                gaussian = _exact_int(gaussian, "gaussian_cache_u64")
            return cls(
                schema_version=_exact_int(payload["schema_version"], "schema_version", minimum=1),
                substream_id=_identifier(payload["substream_id"], "substream_id"),
                algorithm_id=_exact_string(payload["algorithm_id"], "algorithm_id"),
                codec_id=_exact_string(payload["codec_id"], "codec_id"),
                codec_version=_exact_int(payload["codec_version"], "codec_version", minimum=1),
                initial_seed=_exact_int(payload["initial_seed"], "initial_seed"),
                python_implementation=_exact_string(
                    payload["python_implementation"], "python_implementation"
                ),
                python_major=_exact_int(payload["python_major"], "python_major"),
                python_minor=_exact_int(payload["python_minor"], "python_minor"),
                random_state_version=_exact_int(
                    payload["random_state_version"], "random_state_version"
                ),
                state_words=tuple(
                    _exact_int(word, f"state_words[{index}]")
                    for index, word in enumerate(words)
                ),
                state_index=_exact_int(payload["state_index"], "state_index"),
                gaussian_cache_u64=gaussian,
            )
        raise ValueError("owned PRNG algorithm is unsupported")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> OwnedPrngStateV1:
        return cls.from_dict(parse_canonical_json_object(payload))


@dataclass(frozen=True, slots=True)
class CheckpointCompositionExpectationV1:
    """Exact composition-derived truth used to validate component records."""

    schema_version: int
    semantic_plan_sha256: str
    composition_matrix_sha256: str
    composition_profile_id: str
    composition_profile_version: int
    composition_profile_sha256: str
    checkpoint_inventory_id: str
    checkpoint_inventory_sha256: str
    component_inventory: tuple[str, ...]
    active_component_ids: tuple[str, ...]
    state_owner_ids: Mapping[str, str]
    component_schema_versions: Mapping[str, int]
    implementation_versions: Mapping[str, int]
    dependencies_by_component: Mapping[str, tuple[str, ...]]
    absent_reasons_by_component: Mapping[str, str]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version", minimum=1)
            != CHECKPOINT_COMPOSITION_EXPECTATION_SCHEMA_VERSION
        ):
            raise ValueError(
                "CheckpointCompositionExpectationV1 schema_version must be 1"
            )
        for field in (
            "semantic_plan_sha256",
            "composition_matrix_sha256",
            "composition_profile_sha256",
            "checkpoint_inventory_sha256",
        ):
            _sha256(getattr(self, field), field)
        _identifier(self.composition_profile_id, "composition_profile_id")
        _exact_int(
            self.composition_profile_version,
            "composition_profile_version",
            minimum=1,
        )
        _identifier(self.checkpoint_inventory_id, "checkpoint_inventory_id")
        inventory = _string_tuple(self.component_inventory, "component_inventory")
        active = _string_tuple(self.active_component_ids, "active_component_ids")
        if not inventory or not set(active) <= set(inventory):
            raise ValueError("active checkpoint IDs must lie in a nonempty inventory")

        owners = _exact_string_mapping(
            self.state_owner_ids,
            "state_owner_ids",
            expected_keys=inventory,
        )
        schemas = _exact_int_mapping(
            self.component_schema_versions,
            "component_schema_versions",
            expected_keys=inventory,
            minimum=1,
        )
        implementations = _exact_int_mapping(
            self.implementation_versions,
            "implementation_versions",
            expected_keys=active,
            minimum=1,
        )
        dependencies = _exact_dependency_mapping(
            self.dependencies_by_component,
            expected_keys=inventory,
        )
        inactive = tuple(sorted(set(inventory) - set(active)))
        absent_reasons = _exact_string_mapping(
            self.absent_reasons_by_component,
            "absent_reasons_by_component",
            expected_keys=inactive,
        )
        for reason in absent_reasons.values():
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,191}", reason) is None:
                raise ValueError("checkpoint absence reason is not stable uppercase text")

        object.__setattr__(self, "component_inventory", inventory)
        object.__setattr__(self, "active_component_ids", active)
        object.__setattr__(self, "state_owner_ids", owners)
        object.__setattr__(self, "component_schema_versions", schemas)
        object.__setattr__(self, "implementation_versions", implementations)
        object.__setattr__(self, "dependencies_by_component", dependencies)
        object.__setattr__(self, "absent_reasons_by_component", absent_reasons)

    def as_dict(self) -> dict[str, object]:
        return {
            "absent_reasons_by_component": dict(self.absent_reasons_by_component),
            "active_component_ids": list(self.active_component_ids),
            "checkpoint_inventory_id": self.checkpoint_inventory_id,
            "checkpoint_inventory_sha256": self.checkpoint_inventory_sha256,
            "component_inventory": list(self.component_inventory),
            "component_schema_versions": dict(self.component_schema_versions),
            "composition_matrix_sha256": self.composition_matrix_sha256,
            "composition_profile_id": self.composition_profile_id,
            "composition_profile_sha256": self.composition_profile_sha256,
            "composition_profile_version": self.composition_profile_version,
            "dependencies_by_component": {
                component_id: list(dependencies)
                for component_id, dependencies in self.dependencies_by_component.items()
            },
            "implementation_versions": dict(self.implementation_versions),
            "schema_version": self.schema_version,
            "semantic_plan_sha256": self.semantic_plan_sha256,
            "state_owner_ids": dict(self.state_owner_ids),
        }


def _exact_string_mapping(
    value: Mapping[str, object],
    field: str,
    *,
    expected_keys: tuple[str, ...],
) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{field} must be a string-keyed mapping")
    if set(value) != set(expected_keys):
        raise ValueError(f"{field} must cover its exact expected keys")
    result = {
        key: _identifier(value[key], f"{field}[{key!r}]")
        for key in sorted(value)
    }
    return MappingProxyType(result)


def _exact_int_mapping(
    value: Mapping[str, object],
    field: str,
    *,
    expected_keys: tuple[str, ...],
    minimum: int,
) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError(f"{field} must be a string-keyed mapping")
    if set(value) != set(expected_keys):
        raise ValueError(f"{field} must cover its exact expected keys")
    return MappingProxyType(
        {
            key: _exact_int(value[key], f"{field}[{key!r}]", minimum=minimum)
            for key in sorted(value)
        }
    )


def _exact_dependency_mapping(
    value: Mapping[str, tuple[str, ...]],
    *,
    expected_keys: tuple[str, ...],
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise TypeError("dependencies_by_component must be a string-keyed mapping")
    if set(value) != set(expected_keys):
        raise ValueError("dependencies_by_component must cover the exact inventory")
    inventory = set(expected_keys)
    result: dict[str, tuple[str, ...]] = {}
    for component_id in sorted(value):
        dependencies = _string_tuple(
            value[component_id],
            f"dependencies_by_component[{component_id!r}]",
        )
        if component_id in dependencies or not set(dependencies) <= inventory:
            raise ValueError("checkpoint dependencies are self-referential or missing")
        result[component_id] = dependencies
    return MappingProxyType(result)


def derive_checkpoint_composition_expectation(
    plan: FullDayPlanV1,
    composition_matrix: CompositionMatrixV1,
    inventory: CheckpointInventoryV1,
) -> CheckpointCompositionExpectationV1:
    """Derive PRESERVED/ABSENT truth from one exact plan, matrix, and inventory."""

    if type(plan) is not FullDayPlanV1:
        raise TypeError("checkpoint composition derivation requires FullDayPlanV1")
    if type(composition_matrix) is not CompositionMatrixV1:
        raise TypeError("composition_matrix must be CompositionMatrixV1")
    if type(inventory) is not CheckpointInventoryV1:
        raise TypeError("inventory must be CheckpointInventoryV1")
    if plan.composition_profile.sha256 != composition_matrix.sha256:
        raise ValueError("plan composition reference does not bind the matrix")
    profile = composition_matrix.profile(
        plan.composition_profile.reference_id,
        plan.composition_profile.version,
    )
    supported_profile_sha256 = SUPPORTED_CHECKPOINT_COMPOSITION_PROFILES_V1.get(
        (profile.profile_id, profile.profile_version)
    )
    if supported_profile_sha256 != profile.sha256:
        raise ValueError("selected checkpoint composition profile is unsupported")
    predicate_values = profile.predicate_values_for_plan_bindings(
        plan.selected_component_ids,
        participant_schedule_nonempty=bool(plan.participant_schedule),
        any_participant_initially_active=any(
            participant.initially_active
            for participant in plan.participant_definitions
        ),
    )
    active_owner_ids = set(profile.resolve_active_components(predicate_values))
    by_owner = {component.component_id: component for component in profile.components}
    rows_by_id = {item.component_id: item for item in inventory.items}
    row_ids_by_owner: dict[str, set[str]] = {}
    for item in inventory.items:
        row_ids_by_owner.setdefault(item.state_owner_id, set()).add(item.component_id)

    for component in profile.components:
        declared = set(component.checkpoint_state_ids)
        unknown = declared - set(rows_by_id)
        if unknown:
            raise ValueError(
                f"composition component {component.component_id} names unknown checkpoint state"
            )
        misowned = {
            state_id
            for state_id in declared
            if rows_by_id[state_id].state_owner_id != component.component_id
        }
        if misowned:
            raise ValueError("composition checkpoint state owner differs from inventory")
        if declared != row_ids_by_owner.get(component.component_id, set()):
            raise ValueError(
                f"composition component {component.component_id} omits or adds owned state"
            )
        if component.component_id in active_owner_ids and component.implementation_status == "REFUSED":
            raise ValueError("a refused implementation cannot own active checkpoint state")
        if component.component_id in active_owner_ids and component.component_version not in (
            SUPPORTED_CHECKPOINT_IMPLEMENTATION_VERSIONS_V1.get(
                component.component_id, frozenset()
            )
        ):
            raise ValueError(
                "active checkpoint component implementation version is unsupported"
            )

    active_state_ids = tuple(
        sorted(
            state_id
            for owner_id in active_owner_ids
            for state_id in by_owner[owner_id].checkpoint_state_ids
        )
    )
    always_ids = {
        item.component_id for item in inventory.items if item.presence == "ALWAYS"
    }
    if not always_ids <= set(active_state_ids):
        raise ValueError("composition omits always-present checkpoint state")

    absent_reasons: dict[str, str] = {}
    for item in inventory.items:
        if item.component_id in set(active_state_ids):
            continue
        if item.state_owner_id in by_owner:
            absent_reasons[item.component_id] = profile.absence_reason_code(
                item.state_owner_id,
                predicate_values,
            )
        elif item.state_owner_id in profile.refused_component_ids:
            absent_reasons[item.component_id] = profile.absence_reason_code(
                item.state_owner_id,
                predicate_values,
            )
        else:
            absent_reasons[item.component_id] = (
                ABSENT_REASON_PROFILE_OMITS_STATE_OWNER
            )

    implementation_versions = {
        item.component_id: by_owner[item.state_owner_id].component_version
        for item in inventory.items
        if item.component_id in set(active_state_ids)
    }
    expectation = CheckpointCompositionExpectationV1(
        schema_version=1,
        semantic_plan_sha256=plan.semantic_sha256,
        composition_matrix_sha256=composition_matrix.sha256,
        composition_profile_id=profile.profile_id,
        composition_profile_version=profile.profile_version,
        composition_profile_sha256=profile.sha256,
        checkpoint_inventory_id=inventory.inventory_id,
        checkpoint_inventory_sha256=inventory.sha256,
        component_inventory=tuple(item.component_id for item in inventory.items),
        active_component_ids=active_state_ids,
        state_owner_ids={
            item.component_id: item.state_owner_id for item in inventory.items
        },
        component_schema_versions={
            item.component_id: item.state_schema_version for item in inventory.items
        },
        implementation_versions=implementation_versions,
        dependencies_by_component={
            item.component_id: item.dependencies for item in inventory.items
        },
        absent_reasons_by_component=absent_reasons,
    )
    # Reuse the frozen inventory's dependency/presence proof at derivation time.
    validate_checkpoint_capture(
        inventory,
        cut=_expectation_probe_cut(),
        active_component_ids=expectation.active_component_ids,
        preserved_component_ids=expectation.active_component_ids,
        absent_component_ids=tuple(expectation.absent_reasons_by_component),
    )
    return expectation


def _expectation_probe_cut() -> QuiescentCutV1:
    """A nonsemantic quiescent witness used only for inventory set validation."""

    return QuiescentCutV1(
        schema_version=1,
        simulation_time_us=0,
        microstep=0,
        checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
        last_global_event_sequence=0,
        event_prefix_last_global_sequence=0,
        event_prefix_sha256=canonical_sha256([]),
        pending_work_count=0,
        next_pending_time_us=None,
        next_pending_microstep=None,
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=True,
    )


@dataclass(frozen=True, slots=True)
class RuntimeCheckpointV1:
    """One immutable, portable checkpoint of the complete full-day runtime."""

    schema_version: int
    format_id: str
    engine_runtime: EngineRuntimeCompatibilityV1
    native_plan_compiler_identity: str
    semantic_plan_sha256: str
    composition_matrix_sha256: str
    composition_profile_id: str
    composition_profile_version: int
    composition_profile_sha256: str
    checkpoint_inventory_id: str
    checkpoint_inventory_sha256: str
    component_inventory: tuple[str, ...]
    quiescent_cut: QuiescentCutV1
    components: tuple[RuntimeComponentStateV1, ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version", minimum=1)
            != RUNTIME_CHECKPOINT_SCHEMA_VERSION
        ):
            raise ValueError("RuntimeCheckpointV1 schema_version must be 1")
        if self.format_id != RUNTIME_CHECKPOINT_FORMAT_ID:
            raise ValueError("runtime checkpoint format is unsupported")
        if type(self.engine_runtime) is not EngineRuntimeCompatibilityV1:
            raise TypeError("engine_runtime must use EngineRuntimeCompatibilityV1")
        # V1 has no native plan compiler.  Accepting an unregistered label would
        # falsely claim that this interpreter knows how to reconstruct its IR.
        if self.native_plan_compiler_identity != ABSENT_NATIVE_PLAN:
            raise ValueError("V1 runtime checkpoints require ABSENT_NATIVE_PLAN")
        for field in (
            "semantic_plan_sha256",
            "composition_matrix_sha256",
            "composition_profile_sha256",
            "checkpoint_inventory_sha256",
        ):
            _sha256(getattr(self, field), field)
        _identifier(self.composition_profile_id, "composition_profile_id")
        _exact_int(
            self.composition_profile_version,
            "composition_profile_version",
            minimum=1,
        )
        _identifier(self.checkpoint_inventory_id, "checkpoint_inventory_id")
        inventory = _string_tuple(self.component_inventory, "component_inventory")
        if not inventory:
            raise ValueError("runtime checkpoint component inventory must not be empty")
        if type(self.quiescent_cut) is not QuiescentCutV1:
            raise TypeError("quiescent_cut must use QuiescentCutV1")
        self.quiescent_cut.validate_quiescent()
        if type(self.components) is not tuple or any(
            type(record) is not RuntimeComponentStateV1
            for record in self.components
        ):
            raise TypeError("components must be an immutable RuntimeComponentStateV1 tuple")
        component_ids = tuple(record.component_id for record in self.components)
        if component_ids != inventory:
            raise ValueError(
                "runtime checkpoint component records differ from its ordered inventory"
            )
        object.__setattr__(self, "component_inventory", inventory)

    def semantic_identity_dict(self) -> dict[str, object]:
        """Return the complete semantic projection (which has no path/self digest)."""

        return {
            "checkpoint_inventory_id": self.checkpoint_inventory_id,
            "checkpoint_inventory_sha256": self.checkpoint_inventory_sha256,
            "component_inventory": list(self.component_inventory),
            "components": [record.as_dict() for record in self.components],
            "composition_matrix_sha256": self.composition_matrix_sha256,
            "composition_profile_id": self.composition_profile_id,
            "composition_profile_sha256": self.composition_profile_sha256,
            "composition_profile_version": self.composition_profile_version,
            "engine_runtime": self.engine_runtime.as_dict(),
            "format_id": self.format_id,
            "native_plan_compiler_identity": self.native_plan_compiler_identity,
            "quiescent_cut": self.quiescent_cut.as_dict(),
            "schema_version": self.schema_version,
            "semantic_plan_sha256": self.semantic_plan_sha256,
        }

    def identity_dict(self) -> dict[str, object]:
        return self.semantic_identity_dict()

    def as_dict(self) -> dict[str, object]:
        return self.semantic_identity_dict()

    @property
    def semantic_sha256(self) -> str:
        return canonical_sha256(self.semantic_identity_dict())

    @property
    def checkpoint_id(self) -> str:
        return "checkpoint-" + self.semantic_sha256[:24]

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    def to_json_bytes(self) -> bytes:
        return self.canonical_bytes()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RuntimeCheckpointV1:
        validate_strict_json(payload)
        if not isinstance(payload, Mapping):
            raise TypeError("serialized RuntimeCheckpointV1 must be an object")
        _require_exact_fields(
            payload,
            {
                "checkpoint_inventory_id",
                "checkpoint_inventory_sha256",
                "component_inventory",
                "components",
                "composition_matrix_sha256",
                "composition_profile_id",
                "composition_profile_sha256",
                "composition_profile_version",
                "engine_runtime",
                "format_id",
                "native_plan_compiler_identity",
                "quiescent_cut",
                "schema_version",
                "semantic_plan_sha256",
            },
            "RuntimeCheckpointV1",
        )
        engine_runtime = payload["engine_runtime"]
        cut = payload["quiescent_cut"]
        components = payload["components"]
        if not isinstance(engine_runtime, Mapping):
            raise TypeError("serialized engine_runtime must be an object")
        if not isinstance(cut, Mapping):
            raise TypeError("serialized quiescent_cut must be an object")
        if type(components) is not list or any(
            not isinstance(record, Mapping) for record in components
        ):
            raise TypeError("serialized components must be an array of objects")
        return cls(
            schema_version=_exact_int(
                payload["schema_version"], "schema_version", minimum=1
            ),
            format_id=_exact_string(payload["format_id"], "format_id"),
            engine_runtime=EngineRuntimeCompatibilityV1.from_dict(engine_runtime),
            native_plan_compiler_identity=_exact_string(
                payload["native_plan_compiler_identity"],
                "native_plan_compiler_identity",
            ),
            semantic_plan_sha256=_sha256(
                payload["semantic_plan_sha256"], "semantic_plan_sha256"
            ),
            composition_matrix_sha256=_sha256(
                payload["composition_matrix_sha256"],
                "composition_matrix_sha256",
            ),
            composition_profile_id=_identifier(
                payload["composition_profile_id"], "composition_profile_id"
            ),
            composition_profile_version=_exact_int(
                payload["composition_profile_version"],
                "composition_profile_version",
                minimum=1,
            ),
            composition_profile_sha256=_sha256(
                payload["composition_profile_sha256"],
                "composition_profile_sha256",
            ),
            checkpoint_inventory_id=_identifier(
                payload["checkpoint_inventory_id"], "checkpoint_inventory_id"
            ),
            checkpoint_inventory_sha256=_sha256(
                payload["checkpoint_inventory_sha256"],
                "checkpoint_inventory_sha256",
            ),
            component_inventory=_wire_string_tuple(
                payload["component_inventory"], "component_inventory"
            ),
            quiescent_cut=QuiescentCutV1.from_dict(cut),
            components=tuple(
                RuntimeComponentStateV1.from_dict(record) for record in components
            ),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> RuntimeCheckpointV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def _validated_event_prefix_sequences(
    checkpoint: RuntimeCheckpointV1,
    event_prefix: Sequence[FullDayEventV1],
) -> Mapping[str, int]:
    if type(checkpoint) is not RuntimeCheckpointV1:
        raise TypeError("checkpoint must be RuntimeCheckpointV1")
    if not isinstance(event_prefix, Sequence) or isinstance(
        event_prefix, (str, bytes, bytearray)
    ):
        raise TypeError("event_prefix must be a sequence of FullDayEventV1")
    checkpoint.quiescent_cut.validate_quiescent()
    if not event_prefix:
        raise ValueError("runtime checkpoint requires a nonempty outer-event prefix")
    cut = checkpoint.quiescent_cut
    prefix_digest = canonical_event_prefix_sha256(event_prefix)
    if len(event_prefix) != cut.last_global_event_sequence:
        raise ValueError("checkpoint global sequence differs from event-prefix length")
    if len(event_prefix) != cut.event_prefix_last_global_sequence:
        raise ValueError("checkpoint ledger sequence differs from event-prefix length")
    if prefix_digest != cut.event_prefix_sha256:
        raise ValueError("checkpoint event-prefix digest differs from verified events")

    prior_key: tuple[int, int, int] | None = None
    component_sequences: dict[str, int] = {}
    prior_event_keys: dict[str, tuple[int, int, int]] = {}
    prior_event_root_work_ids: dict[str, str] = {}
    prior_event_root_work_keys: dict[str, ScheduledWorkKeyV1 | None] = {}
    reconstructible_root_work_keys: dict[str, ScheduledWorkKeyV1] = {}
    prior_reconstructible_root_ordering_key: tuple[int, int, int, str, int] | None = None
    completed_root_work_ids: set[str] = set()
    current_root_work_id: str | None = None
    native_ids: set[tuple[str, str, str]] = set()
    native_sequences: dict[tuple[str, str], int] = {}
    for expected_global_sequence, event in enumerate(event_prefix, start=1):
        if type(event) is not FullDayEventV1:
            raise TypeError("event_prefix must contain only FullDayEventV1")
        if event.global_event_sequence != expected_global_sequence:
            raise ValueError(
                "checkpoint global event sequence must be contiguous from one"
            )
        if prior_key is not None and event.chronological_key < prior_key:
            raise ValueError("checkpoint event prefix moves backward chronologically")
        previous_local = component_sequences.get(event.source_component_id)
        if previous_local is not None and event.component_local_sequence <= previous_local:
            raise ValueError(
                "checkpoint event prefix component-local sequence does not increase"
            )
        component_sequences[event.source_component_id] = event.component_local_sequence
        parent_id = event.causal_parent_ids[0]
        parent_match = _EVENT_PARENT_RE.fullmatch(parent_id)
        if parent_match is not None:
            if (
                parent_id not in prior_event_keys
                or prior_event_keys[parent_id] != event.chronological_key
            ):
                raise ValueError(
                    "checkpoint event parent is missing or not immediate-time causal"
                )
            root_work_id = prior_event_root_work_ids[parent_id]
            root_work_key = prior_event_root_work_keys[parent_id]
        else:
            if _WORK_PARENT_RE.fullmatch(parent_id) is None:
                raise ValueError("checkpoint event has an invalid causal parent")
            root_work_id = parent_id
            root_work_key = None
            if event.event_type in _RECONSTRUCTIBLE_DIRECT_WORK_EVENT_TYPES_V1:
                root_work_key = ScheduledWorkKeyV1(
                    simulation_time_us=event.simulation_time_us,
                    microstep=event.microstep,
                    stage_ordinal=event.stage,
                    source_component_id=event.source_component_id,
                    component_local_sequence=event.component_local_sequence,
                )
                if root_work_id != root_work_key.work_id:
                    raise ValueError(
                        "checkpoint plan-derived event cites the wrong causal work"
                    )
        known_root_work_key = reconstructible_root_work_keys.get(root_work_id)
        if root_work_key is None and known_root_work_key is not None:
            root_work_key = known_root_work_key
        elif root_work_key is not None:
            if (
                known_root_work_key is not None
                and root_work_key != known_root_work_key
            ):
                raise ValueError(
                    "checkpoint causal work root has conflicting five-field keys"
                )
            if known_root_work_key is None:
                if (
                    prior_reconstructible_root_ordering_key is not None
                    and root_work_key.ordering_key
                    <= prior_reconstructible_root_ordering_key
                ):
                    raise ValueError(
                        "checkpoint reconstructible work roots are out of queue order"
                    )
                reconstructible_root_work_keys[root_work_id] = root_work_key
                prior_reconstructible_root_ordering_key = root_work_key.ordering_key
        if event.event_type in _RECONSTRUCTIBLE_DIRECT_WORK_EVENT_TYPES_V1:
            if root_work_key is None:
                raise ValueError(
                    "checkpoint plan-derived event lacks a reconstructible work root"
                )
            if (
                root_work_key.simulation_time_us != event.simulation_time_us
                or root_work_key.microstep != event.microstep
                or root_work_key.stage_ordinal is not event.stage
                or root_work_key.source_component_id != event.source_component_id
            ):
                raise ValueError(
                    "checkpoint plan-derived event differs from its causal work root"
                )
        if root_work_id != current_root_work_id:
            if root_work_id in completed_root_work_ids:
                raise ValueError(
                    "checkpoint events for one causal work item are not contiguous"
                )
            if current_root_work_id is not None:
                completed_root_work_ids.add(current_root_work_id)
            current_root_work_id = root_work_id
        native = event.payload.native_event
        if native is not None:
            if native.ledger_key in native_ids:
                raise ValueError("checkpoint event prefix repeats a native event identity")
            owner_key = (native.owner_component_id, native.native_ledger_id)
            prior_native = native_sequences.get(owner_key)
            if prior_native is not None and native.local_sequence <= prior_native:
                raise ValueError("native event local sequence does not increase per ledger")
            native_ids.add(native.ledger_key)
            native_sequences[owner_key] = native.local_sequence
        prior_event_keys[event.event_id] = event.chronological_key
        prior_event_root_work_ids[event.event_id] = root_work_id
        prior_event_root_work_keys[event.event_id] = root_work_key
        prior_key = event.chronological_key

    marker = event_prefix[-1]
    expected_cut_key = (
        cut.simulation_time_us,
        cut.microstep,
        cut.checkpoint_stage_ordinal,
    )
    if marker.chronological_key != expected_cut_key:
        raise ValueError("checkpoint marker is not aligned to the quiescent cut")
    if (
        marker.event_type is not FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
        or marker.stage is not WorkStageV1.CHECKPOINT_CAPTURE
        or marker.source_component_id != "FULL_DAY_RUNTIME_V1"
    ):
        raise ValueError(
            "checkpoint prefix must end with the full-day runtime capture marker"
        )
    if cut.simulation_time_us == 0:
        t0_boundary = tuple(
            event
            for event in event_prefix
            if event.simulation_time_us == 0
            and event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY
        )
        t0_anchor = tuple(
            event
            for event in event_prefix
            if event.simulation_time_us == 0
            and event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET
        )
        if (
            len(t0_boundary) != 1
            or len(t0_anchor) != 1
            or t0_boundary[0].source_component_id != "FULL_DAY_RUNTIME_V1"
            or t0_anchor[0].source_component_id != "FULL_DAY_RUNTIME_V1"
            or t0_boundary[0].payload.data.get("boundary_operation_index") != 0
            or t0_anchor[0].payload.data.get("macro_segment_index") != 0
        ):
            raise ValueError(
                "t=0 checkpoint must follow the boundary and macro-state anchor"
            )
    return MappingProxyType(dict(sorted(component_sequences.items())))


def validate_checkpoint_event_prefix(
    checkpoint: RuntimeCheckpointV1,
    event_prefix: Sequence[FullDayEventV1],
) -> None:
    """Bind a checkpoint to the real, marker-inclusive outer-event prefix."""

    _validated_event_prefix_sequences(checkpoint, event_prefix)


def _preserved_states(
    checkpoint: RuntimeCheckpointV1,
) -> Mapping[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for record in checkpoint.components:
        if record.status is RuntimeComponentStatusV1.PRESERVED:
            assert record.state is not None
            result[record.component_id] = record.state
    return MappingProxyType(result)


def _required_state(
    states: Mapping[str, Mapping[str, object]],
    component_id: str,
) -> Mapping[str, object]:
    try:
        return states[component_id]
    except KeyError as error:
        raise ValueError(f"required checkpoint state {component_id} is not preserved") from error


def _state_int(
    state: Mapping[str, object],
    field: str,
    *,
    minimum: int = 0,
) -> int:
    try:
        value = state[field]
    except KeyError as error:  # pragma: no cover - frozen inventory guards this first
        raise ValueError(f"checkpoint state is missing {field}") from error
    return _exact_int(value, field, minimum=minimum)


def _plain_owned_prng(value: object, field: str) -> OwnedPrngStateV1:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must contain owned PRNG state objects")
    # Component state is deeply frozen.  Pass it through the canonical JSON
    # codec so the branch-strict wire parser sees ordinary JSON containers.
    payload = parse_canonical_json_object(canonical_json_bytes(value))
    return OwnedPrngStateV1.from_dict(payload)


def _active_rng_substream_ids(
    plan: FullDayPlanV1,
    composition_matrix: CompositionMatrixV1,
) -> tuple[str, ...]:
    profile = composition_matrix.profile(
        plan.composition_profile.reference_id,
        plan.composition_profile.version,
    )
    predicate_values = profile.predicate_values_for_plan_bindings(
        plan.selected_component_ids,
        participant_schedule_nonempty=bool(plan.participant_schedule),
        any_participant_initially_active=any(
            participant.initially_active
            for participant in plan.participant_definitions
        ),
    )
    active_owner_ids = set(profile.resolve_active_components(predicate_values))
    labels: list[str] = []
    for declaration in plan.seed_policy.substreams:
        owners = {
            owner_id
            for owner_id in active_owner_ids
            for prefix in RNG_LABEL_PREFIXES_BY_COMPONENT_V1.get(owner_id, ())
            if declaration.semantic_path == prefix
            or declaration.semantic_path.startswith(prefix + "/")
        }
        if len(owners) > 1:
            raise ValueError("active RNG substream has ambiguous composition ownership")
        if owners:
            labels.append(declaration.semantic_path)
    return tuple(sorted(labels))


def _validate_root_rng_state(
    state: Mapping[str, object],
    *,
    plan: FullDayPlanV1,
    composition_matrix: CompositionMatrixV1,
) -> Mapping[str, OwnedPrngStateV1]:
    if state["runtime.rng_algorithm_codec_version"] != OWNED_PRNG_CODEC_REGISTRY_ID:
        raise ValueError("runtime RNG codec registry is unsupported")
    if _state_int(state, "runtime.root_seed") != plan.seed_policy.root_seed:
        raise ValueError("checkpoint root seed differs from the semantic plan")
    if state["runtime.substream_policy_version"] != plan.seed_policy.policy_version:
        raise ValueError("checkpoint substream policy differs from the semantic plan")

    registry = state["runtime.derived_seed_registry"]
    if type(registry) is not tuple or any(not isinstance(row, Mapping) for row in registry):
        raise TypeError("runtime.derived_seed_registry must be an ordered object array")
    expected_registry = tuple(
        declaration.as_dict() for declaration in plan.seed_policy.substreams
    )
    if canonical_json_bytes(registry) != canonical_json_bytes(expected_registry):
        raise ValueError("checkpoint derived-seed registry differs from the semantic plan")

    raw_states = state["runtime.rng_states"]
    if type(raw_states) is not tuple:
        raise TypeError("runtime.rng_states must be an ordered PRNG-state array")
    records = tuple(
        _plain_owned_prng(row, f"runtime.rng_states[{index}]")
        for index, row in enumerate(raw_states)
    )
    labels = tuple(record.substream_id for record in records)
    if labels != tuple(sorted(set(labels))):
        raise ValueError("runtime RNG state labels must be sorted and unique")
    active_labels = _active_rng_substream_ids(plan, composition_matrix)
    if labels != active_labels:
        raise ValueError("runtime RNG states differ from active composition ownership")
    seeds = {
        declaration.semantic_path: declaration.derived_seed
        for declaration in plan.seed_policy.substreams
    }
    for record in records:
        if record.initial_seed != seeds[record.substream_id]:
            raise ValueError("runtime RNG initial seed differs from its plan declaration")
    splitmix_labels = {
        plan.state_model.day_rng_substream_label,
        plan.state_model.local_rng_substream_label,
    }
    for record in records:
        expected_algorithm = (
            SPLITMIX64_ALGORITHM_ID
            if record.substream_id in splitmix_labels
            else CPYTHON_MT19937_ALGORITHM_ID
        )
        if record.algorithm_id != expected_algorithm:
            raise ValueError("runtime RNG algorithm differs from its frozen owner codec")
    return MappingProxyType({record.substream_id: record for record in records})


def _validate_component_rng_copies(
    states: Mapping[str, Mapping[str, object]],
    root_rng: Mapping[str, OwnedPrngStateV1],
    *,
    expectation: CheckpointCompositionExpectationV1,
) -> None:
    root_wire = {
        label: canonical_json_bytes(record.as_dict())
        for label, record in root_rng.items()
    }
    for component_id, state in states.items():
        owner_id = expectation.state_owner_ids[component_id]
        owner_prefixes = RNG_LABEL_PREFIXES_BY_COMPONENT_V1.get(owner_id, ())
        owner_labels = tuple(
            sorted(
                label
                for label in root_rng
                if any(
                    label == prefix or label.startswith(prefix + "/")
                    for prefix in owner_prefixes
                )
            )
        )
        for field, value in state.items():
            if field not in _RNG_STATE_FIELDS or field == "runtime.rng_states":
                continue
            if isinstance(value, Mapping) and "algorithm_id" in value:
                raw_records = (value,)
            elif type(value) is tuple:
                raw_records = value
            else:
                raise TypeError(f"{component_id}.{field} must contain owned PRNG records")
            records = tuple(
                _plain_owned_prng(row, f"{component_id}.{field}[{index}]")
                for index, row in enumerate(raw_records)
            )
            labels = tuple(record.substream_id for record in records)
            if labels != tuple(sorted(set(labels))):
                raise ValueError(f"{component_id}.{field} RNG labels are not sorted/unique")
            if labels != owner_labels:
                raise ValueError(
                    f"{component_id}.{field} RNG labels differ from exact owner set"
                )
            for record in records:
                expected = root_wire.get(record.substream_id)
                if expected is None or record.canonical_bytes() != expected:
                    raise ValueError(
                        f"{component_id}.{field} RNG state does not reconcile with root ownership"
                    )


def _validate_pending_work(
    state: Mapping[str, object],
    cut: QuiescentCutV1,
) -> tuple[ScheduledWorkKeyV1, ...]:
    _state_int(state, "scheduled_work.dequeued_count")
    heap = state["scheduled_work.pending_heap"]
    if type(heap) is not tuple:
        raise TypeError("scheduled_work.pending_heap must be an ordered work-key array")
    work = tuple(
        ScheduledWorkKeyV1.from_dict(row)
        if isinstance(row, Mapping)
        else (_ for _ in ()).throw(
            TypeError("scheduled_work.pending_heap must contain work-key objects")
        )
        for row in heap
    )
    ordering = tuple(item.ordering_key for item in work)
    if ordering != tuple(sorted(ordering)) or len(ordering) != len(set(ordering)):
        raise ValueError("scheduled work heap keys must be sorted and unique")
    if len(work) != cut.pending_work_count:
        raise ValueError("scheduled work count differs from the quiescent cut")
    if work:
        first = work[0]
        if (
            first.simulation_time_us != cut.next_pending_time_us
            or first.microstep != cut.next_pending_microstep
        ):
            raise ValueError("scheduled work head differs from the quiescent cut")
        if first.simulation_time_us <= cut.simulation_time_us:
            raise ValueError("scheduled work remains due at the quiescent cut")
    elif cut.next_pending_time_us is not None or cut.next_pending_microstep is not None:
        raise ValueError("empty scheduled work conflicts with the quiescent cut")
    return work


def _validate_plan_bound_prefix(
    plan: FullDayPlanV1,
    event_prefix: Sequence[FullDayEventV1],
    cut: QuiescentCutV1,
) -> Mapping[str, object]:
    boundary_events = tuple(
        event
        for event in event_prefix
        if event.event_type is FullDayEventTypeV1.CALENDAR_BOUNDARY
    )
    expected_operations = tuple(
        (index, operation)
        for index, operation in enumerate(plan.calendar.boundary_operations)
        if operation.boundary.simulation_time_us <= cut.simulation_time_us
    )
    if len(boundary_events) != len(expected_operations):
        raise ValueError("event prefix omits or adds a due calendar boundary")
    for event, (index, operation) in zip(
        boundary_events, expected_operations, strict=True
    ):
        if (
            event.source_component_id != "FULL_DAY_RUNTIME_V1"
            or event.simulation_time_us
            != operation.boundary.simulation_time_us
            or event.payload.data["boundary_operation_index"] != index
            or event.payload.data["destination_session_state"]
            != operation.destination_session_state.value
            or event.payload.data["uncross_before"] != operation.uncross_before
        ):
            raise ValueError("calendar boundary event differs from the semantic plan")

    anchor_events = tuple(
        event
        for event in event_prefix
        if event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET
    )
    expected_segments = tuple(
        (index, segment)
        for index, segment in enumerate(plan.macro_regime_schedule)
        if segment.start_us <= cut.simulation_time_us
    )
    if len(anchor_events) != len(expected_segments):
        raise ValueError("event prefix omits or adds a due macro-state anchor")
    for event, (index, segment) in zip(anchor_events, expected_segments, strict=True):
        if (
            event.source_component_id != "FULL_DAY_RUNTIME_V1"
            or event.simulation_time_us != segment.start_us
            or event.payload.data["macro_segment_index"] != index
            or event.payload.data["macro_segment_sha256"]
            != canonical_sha256(segment.as_dict())
            or event.payload.data["anchored_state"] != segment.day_state.value
            or event.payload.data["entered_time_us"] != segment.start_us
        ):
            raise ValueError("macro-state anchor event differs from the semantic plan")

    current_day = plan.state_model.initial_day_state.value
    current_local = plan.state_model.initial_local_state.value
    day_entered_time_us = 0
    local_entered_time_us = 0
    day_sampled_duration_us: int | None = None
    local_sampled_duration_us: int | None = None
    day_transition_count = 0
    local_transition_count = 0
    day_transitions_since_anchor = 0
    runtime_emission_count = 0
    for event in event_prefix:
        if event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET:
            if event.payload.data["previous_state"] != current_day:
                raise ValueError("macro-state anchor breaks day-state continuity")
            current_day = event.payload.data["anchored_state"]
            day_entered_time_us = event.simulation_time_us
            day_sampled_duration_us = event.payload.data["sampled_duration_us"]
            day_transitions_since_anchor = 0
            runtime_emission_count += 1
        elif event.event_type is FullDayEventTypeV1.DAY_STATE_TRANSITION:
            if (
                event.source_component_id != "FULL_DAY_RUNTIME_V1"
                or event.payload.data["previous_state"] != current_day
            ):
                raise ValueError("day-state transition breaks verified prefix continuity")
            current_day = event.payload.data["new_state"]
            day_entered_time_us = event.simulation_time_us
            day_sampled_duration_us = event.payload.data["sampled_duration_us"]
            day_transition_count += 1
            day_transitions_since_anchor += 1
            runtime_emission_count += 1
        elif event.event_type is FullDayEventTypeV1.LOCAL_STATE_TRANSITION:
            if (
                event.source_component_id != "FULL_DAY_RUNTIME_V1"
                or event.payload.data["previous_state"] != current_local
            ):
                raise ValueError("local-state transition breaks verified prefix continuity")
            current_local = event.payload.data["new_state"]
            local_entered_time_us = event.simulation_time_us
            local_sampled_duration_us = event.payload.data["sampled_duration_us"]
            local_transition_count += 1
            runtime_emission_count += 1

    next_boundary_time = (
        None
        if len(expected_operations) == len(plan.calendar.boundary_operations)
        else plan.calendar.boundary_operations[len(expected_operations)]
        .boundary.simulation_time_us
    )
    current_phase_id = (
        None
        if not expected_operations
        else expected_operations[-1][1].destination_session_state.value
    )
    return MappingProxyType(
        {
            "boundary_operation_index": len(expected_operations),
            "current_day": current_day,
            "current_local": current_local,
            "current_phase_id": current_phase_id,
            "day_transition_count": day_transition_count,
            "day_transitions_since_macro_anchor": day_transitions_since_anchor,
            "day_entered_time_us": day_entered_time_us,
            "day_sampled_duration_us": day_sampled_duration_us,
            "local_transition_count": local_transition_count,
            "local_entered_time_us": local_entered_time_us,
            "local_sampled_duration_us": local_sampled_duration_us,
            "next_boundary_time_us": next_boundary_time,
            "next_macro_segment_index": len(expected_segments),
            "runtime_emission_count": runtime_emission_count,
        }
    )


def _validate_flat_state_levels(
    state: Mapping[str, object],
    *,
    plan: FullDayPlanV1,
    cut: QuiescentCutV1,
    prefix_state: Mapping[str, object],
    pending_work: tuple[ScheduledWorkKeyV1, ...],
) -> None:
    deterministic_due_times: list[int] = []
    active_observation_ids: set[str] = set()
    for level, definitions in (
        ("day", plan.state_model.day_definitions),
        ("local", plan.state_model.local_definitions),
    ):
        current_state = state[f"state.current_{level}"]
        matches = tuple(
            definition
            for definition in definitions
            if definition.state.value == current_state
        )
        if len(matches) != 1:
            raise ValueError(f"current {level} state is absent from the plan model")
        definition = matches[0]
        entered = _state_int(state, f"state.{level}_entered_time_us")
        if entered != prefix_state[f"{level}_entered_time_us"]:
            raise ValueError(f"{level} entry time differs from verified state events")
        sampled = _state_int(state, f"state.{level}_sampled_duration_us")
        allowed_durations = {
            mass.duration_us for mass in definition.duration_law.masses
        }
        if sampled not in allowed_durations:
            raise ValueError(f"{level} sampled duration is outside the plan law")
        prefix_sampled = prefix_state[f"{level}_sampled_duration_us"]
        if prefix_sampled is not None and sampled != prefix_sampled:
            raise ValueError(f"{level} sampled duration differs from its verified event")
        deadline = _state_int(state, f"state.{level}_sampled_deadline_us")
        if deadline != entered + sampled:
            raise ValueError(f"{level} deadline does not reconcile with sampled duration")

        transition_id = _identifier(
            state[f"state.{level}_next_eligible_transition_id"],
            f"state.{level}_next_eligible_transition_id",
        )
        transition_matches = tuple(
            transition
            for transition in definition.transitions
            if transition.transition_id == transition_id
        )
        if len(transition_matches) != 1:
            raise ValueError(f"{level} next transition is not owned by current state")
        selected = transition_matches[0]
        next_eligible = _state_int(
            state, f"state.{level}_next_eligible_transition_time_us"
        )
        if next_eligible != entered + selected.minimum_age_us:
            raise ValueError(f"{level} next-eligible time differs from the plan edge")

        raw_memory = state[f"state.{level}_trigger_memory"]
        if type(raw_memory) is not tuple:
            raise TypeError(f"state.{level}_trigger_memory must be an ordered array")
        if raw_memory:
            from .transitions import (
                StateTriggerMemoryV1,
                TriggerObservationPhaseV1,
                trigger_parameter_set_sha256_v1,
            )

            memories = tuple(
                StateTriggerMemoryV1.from_dict(
                    parse_canonical_json_object(canonical_json_bytes(row))
                )
                if isinstance(row, Mapping)
                else (_ for _ in ()).throw(
                    TypeError(f"state.{level}_trigger_memory rows must be objects")
                )
                for row in raw_memory
            )
            for memory in memories:
                observation = memory.observation
                if (
                    memory.state != current_state
                    or memory.state_entered_time_us != entered
                    or observation.transition_id != selected.transition_id
                    or observation.trigger_id != selected.trigger_id
                    or observation.trigger_version != selected.trigger_version
                    or observation.trigger_parameter_set_sha256
                    != trigger_parameter_set_sha256_v1(selected)
                    or observation.information_class
                    is not selected.trigger_information_class
                    or observation.phase is not TriggerObservationPhaseV1.PRE_TRANSITION
                    or observation.available_time_us > cut.simulation_time_us
                ):
                    raise ValueError(f"{level} trigger memory differs from selected edge")
                active_observation_ids.add(observation.observation_id)
                if observation.triggered and next_eligible <= cut.simulation_time_us:
                    raise ValueError("checkpoint state retains a due triggered transition")

        if selected.duration_exhaustion_behavior.value == "TRANSITION_ON_EXHAUSTION":
            due_time = max(deadline, next_eligible)
            if due_time <= cut.simulation_time_us:
                raise ValueError("checkpoint state retains a due duration transition")
            deterministic_due_times.append(due_time)

    seen = state["state.observation_ids_seen"]
    if type(seen) is not tuple or any(type(item) is not str for item in seen):
        raise TypeError("state.observation_ids_seen must be an ordered string array")
    if seen != tuple(sorted(set(seen))) or not active_observation_ids <= set(seen):
        raise ValueError("state observation IDs do not reconcile with trigger memory")
    if cut.simulation_time_us == 0 and seen:
        raise ValueError("completed genesis state cannot have consumed observations")

    if deterministic_due_times:
        expected_frontier = min(deterministic_due_times)
        state_work_times = tuple(
            item.simulation_time_us
            for item in pending_work
            if item.source_component_id == "FULL_DAY_RUNTIME_V1"
            and item.stage_ordinal is WorkStageV1.DAY_STATE_TRANSITION
        )
        if not state_work_times or min(state_work_times) != expected_frontier:
            raise ValueError("pending state-work frontier differs from runtime deadlines")


def _validate_state_rng_replay(
    *,
    plan: FullDayPlanV1,
    event_prefix: Sequence[FullDayEventV1],
    runtime_state: Mapping[str, object],
    root_rng: Mapping[str, OwnedPrngStateV1],
) -> None:
    """Replay every V1 state draw and bind exact SplitMix internal snapshots."""

    from .transitions import _SplitMix64V1, _sample_duration, _sample_transition

    day_definitions = {
        definition.state.value: definition
        for definition in plan.state_model.day_definitions
    }
    local_definitions = {
        definition.state.value: definition
        for definition in plan.state_model.local_definitions
    }
    day_label = plan.state_model.day_rng_substream_label
    local_label = plan.state_model.local_rng_substream_label
    day_rng = _SplitMix64V1.seeded(plan.seed_policy.derive(day_label), day_label)
    local_rng = _SplitMix64V1.seeded(plan.seed_policy.derive(local_label), local_label)

    current_day = plan.state_model.initial_day_state.value
    current_local = plan.state_model.initial_local_state.value
    day_definition = day_definitions[current_day]
    local_definition = local_definitions[current_local]
    day_duration = _sample_duration(day_definition.duration_law, day_rng)
    local_duration = _sample_duration(local_definition.duration_law, local_rng)
    day_transition = _sample_transition(day_definition, day_rng)
    local_transition = _sample_transition(local_definition, local_rng)

    for event in event_prefix:
        data = event.payload.data
        if event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET:
            macro_index = data["macro_segment_index"]
            anchored_state = data["anchored_state"]
            if macro_index == 0:
                if (
                    anchored_state != current_day
                    or data["sampled_duration_us"] != day_duration
                ):
                    raise ValueError("initial macro anchor differs from replayed state draws")
                continue
            current_day = anchored_state
            day_definition = day_definitions[current_day]
            day_duration = _sample_duration(day_definition.duration_law, day_rng)
            day_transition = _sample_transition(day_definition, day_rng)
            if data["sampled_duration_us"] != day_duration:
                raise ValueError("macro anchor duration differs from replayed state RNG")
            continue

        if event.event_type is FullDayEventTypeV1.DAY_STATE_TRANSITION:
            if (
                data["transition_id"] != day_transition.transition_id
                or data["previous_state"] != current_day
                or data["new_state"] != day_transition.successor_state
                or data["trigger_id"] != day_transition.trigger_id
                or data["trigger_version"] != day_transition.trigger_version
            ):
                raise ValueError("day transition differs from replayed selected edge")
            current_day = day_transition.successor_state
            day_definition = day_definitions[current_day]
            day_duration = _sample_duration(day_definition.duration_law, day_rng)
            day_transition = _sample_transition(day_definition, day_rng)
            if data["sampled_duration_us"] != day_duration:
                raise ValueError("day transition duration differs from replayed RNG")
            continue

        if event.event_type is FullDayEventTypeV1.LOCAL_STATE_TRANSITION:
            if (
                data["transition_id"] != local_transition.transition_id
                or data["previous_state"] != current_local
                or data["new_state"] != local_transition.successor_state
                or data["trigger_id"] != local_transition.trigger_id
                or data["trigger_version"] != local_transition.trigger_version
            ):
                raise ValueError("local transition differs from replayed selected edge")
            current_local = local_transition.successor_state
            local_definition = local_definitions[current_local]
            local_duration = _sample_duration(local_definition.duration_law, local_rng)
            local_transition = _sample_transition(local_definition, local_rng)
            if data["sampled_duration_us"] != local_duration:
                raise ValueError("local transition duration differs from replayed RNG")

    replayed_flat = {
        "state.current_day": current_day,
        "state.current_local": current_local,
        "state.day_next_eligible_transition_id": day_transition.transition_id,
        "state.day_sampled_duration_us": day_duration,
        "state.local_next_eligible_transition_id": local_transition.transition_id,
        "state.local_sampled_duration_us": local_duration,
    }
    if any(runtime_state[field] != value for field, value in replayed_flat.items()):
        raise ValueError("flat state runtime differs from deterministic RNG replay")

    for label, replayed in (
        (day_label, day_rng.snapshot()),
        (local_label, local_rng.snapshot()),
    ):
        preserved = root_rng[label]
        if (
            preserved.algorithm_id != replayed.algorithm
            or preserved.substream_id != replayed.substream_label
            or preserved.initial_seed != replayed.initial_seed
            or preserved.state_u64 != replayed.state_u64
            or preserved.draw_count != replayed.draw_count
            or preserved.sample_count != replayed.sample_count
        ):
            raise ValueError("state-runtime SplitMix snapshot differs from replayed draws")


def _validate_zero_activity_cpython_rng(
    *,
    plan: FullDayPlanV1,
    event_prefix: Sequence[FullDayEventV1],
    cut: QuiescentCutV1,
    root_rng: Mapping[str, OwnedPrngStateV1],
) -> None:
    """Refuse silently advanced MT streams that have no activity provenance."""

    from random import Random

    participant_by_label = {
        participant.rng_substream_label: participant
        for participant in plan.participant_definitions
    }
    shock_activity_types = {
        FullDayEventTypeV1.SHOCK_CANDIDATE,
        FullDayEventTypeV1.SHOCK_ACCEPTED,
        FullDayEventTypeV1.SHOCK_REJECTED,
    }
    for label, record in root_rng.items():
        if record.algorithm_id != CPYTHON_MT19937_ALGORITHM_ID:
            continue
        inactive = False
        participant = participant_by_label.get(label)
        if cut.simulation_time_us == 0:
            # V1 has no typed owner-state ledger from which to prove a decision,
            # proposal, or shock draw at the genesis capture.  Do not let a
            # self-consistently forged outer event turn into RNG provenance.
            inactive = True
        elif participant is not None:
            participant_id = participant.participant_id
            decision_activity = any(
                event.event_type is FullDayEventTypeV1.PARTICIPANT_DECISION
                and event.payload.data.get("participant_id") == participant_id
                for event in event_prefix
            )
            # Construction, initial activation, and scheduled
            # activate/deactivate/retune operations do not consume the decision
            # stream.  Only BaseMarketAgent.decide() does, and the V1 full-day
            # contract records that call as PARTICIPANT_DECISION.  No typed
            # pre-sampled/pending-decision state codec exists in this ABI, so an
            # opaque nonempty agent.pending_decisions value is not provenance.
            inactive = not decision_activity
        elif label == plan.unscheduled_shock_policy.substream_label:
            inactive = not any(
                event.event_type in shock_activity_types for event in event_prefix
            )
        if not inactive:
            continue

        random_state = Random(record.initial_seed).getstate()[1]
        expected = OwnedPrngStateV1.cpython_mt19937(
            substream_id=label,
            initial_seed=record.initial_seed,
            state_words=tuple(random_state[:-1]),
            state_index=random_state[-1],
        )
        if record.canonical_bytes() != expected.canonical_bytes():
            raise ValueError("zero-activity CPython RNG differs from its seed-initial state")


def _validate_plan_schedule_cursors(
    states: Mapping[str, Mapping[str, object]],
    *,
    plan: FullDayPlanV1,
    cut: QuiescentCutV1,
    event_prefix: Sequence[FullDayEventV1],
) -> None:
    participant_state = _required_state(states, "PARTICIPANT_SCHEDULE_RUNTIME_V1")
    due_participant_entries = tuple(
        entry
        for entry in plan.participant_schedule
        if entry.simulation_time_us <= cut.simulation_time_us
    )
    if (
        _state_int(participant_state, "participant_schedule.next_index")
        != len(due_participant_entries)
    ):
        raise ValueError("participant-schedule cursor differs from the verified cut")
    participant_event_types = {
        "ACTIVATE": FullDayEventTypeV1.PARTICIPANT_ACTIVATED,
        "DEACTIVATE": FullDayEventTypeV1.PARTICIPANT_DEACTIVATED,
        "RETUNE": FullDayEventTypeV1.PARTICIPANT_RETUNED,
    }
    participant_events = tuple(
        event
        for event in event_prefix
        if event.event_type in set(participant_event_types.values())
    )
    if len(participant_events) != len(due_participant_entries):
        raise ValueError("participant-schedule prefix omits or adds due actions")
    bindings = {
        participant.participant_id: participant.specification.as_dict()
        for participant in plan.participant_definitions
    }
    generations = {
        participant.participant_id: 0
        for participant in plan.participant_definitions
    }
    for event, entry in zip(
        participant_events, due_participant_entries, strict=True
    ):
        if (
            event.source_component_id != "AGENT_SCHEDULER_V1"
            or event.event_type is not participant_event_types[entry.action.value]
            or event.simulation_time_us != entry.simulation_time_us
            or event.payload.data["schedule_id"] != entry.schedule_id
            or event.payload.data["participant_id"] != entry.participant_id
        ):
            raise ValueError("participant-schedule event differs from the semantic plan")
        if entry.action.value == "RETUNE":
            replacement = entry.replacement_specification
            assert replacement is not None
            if (
                event.payload.data["replacement_specification_sha256"]
                != replacement.sha256
            ):
                raise ValueError("participant retune digest differs from the plan")
            bindings[entry.participant_id] = replacement.as_dict()
            generations[entry.participant_id] += 1
    if canonical_json_bytes(
        participant_state["participant_schedule.spec_version_bindings"]
    ) != canonical_json_bytes(bindings):
        raise ValueError("participant specification bindings differ from verified schedule")
    if canonical_json_bytes(
        participant_state["participant_schedule.replacement_generation"]
    ) != canonical_json_bytes(generations):
        raise ValueError("participant replacement generations differ from verified schedule")

    scheduled_state = _required_state(
        states, "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1"
    )
    due_scheduled_events = tuple(
        entry
        for entry in plan.scheduled_events
        if entry.simulation_time_us <= cut.simulation_time_us
    )
    if _state_int(scheduled_state, "scheduled_event.next_index") != len(
        due_scheduled_events
    ):
        raise ValueError("scheduled-event cursor differs from the verified cut")
    scheduled_prefix = tuple(
        event
        for event in event_prefix
        if event.event_type is FullDayEventTypeV1.SCHEDULED_INFORMATION
    )
    if len(scheduled_prefix) != len(due_scheduled_events):
        raise ValueError("scheduled-information prefix omits or adds due plan rows")
    for event, entry in zip(scheduled_prefix, due_scheduled_events, strict=True):
        if (
            event.source_component_id != "FULL_DAY_RUNTIME_V1"
            or event.simulation_time_us != entry.simulation_time_us
            or event.payload.data["scheduled_event_id"] != entry.event_id
            or event.payload.data["scheduled_event_type"] != entry.event_type.value
            or event.payload.data["side"] != entry.side.value
            or event.payload.data["parameter_set_sha256"]
            != entry.parameter_set_sha256
        ):
            raise ValueError("scheduled-information event differs from the semantic plan")
    event_state = scheduled_state["scheduled_event.state"]
    if not isinstance(event_state, Mapping) or any(
        type(event_id) is not str for event_id in event_state
    ):
        raise TypeError("scheduled_event.state must be a plan-keyed object")
    expected_event_state = {
        entry.event_id: {
            "applied_time_us": entry.simulation_time_us,
            "parameter_set_sha256": entry.parameter_set_sha256,
            "scheduled_event_type": entry.event_type.value,
            "side": entry.side.value,
        }
        for entry in due_scheduled_events
    }
    if canonical_json_bytes(event_state) != canonical_json_bytes(
        expected_event_state
    ):
        raise ValueError("scheduled_event.state differs from the applied plan prefix")


def _validate_checkpoint_controller(
    state: Mapping[str, object],
    *,
    plan: FullDayPlanV1,
    cut: QuiescentCutV1,
    event_prefix: Sequence[FullDayEventV1],
    pending_work: tuple[ScheduledWorkKeyV1, ...],
) -> None:
    markers = tuple(
        event
        for event in event_prefix
        if event.event_type is FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER
    )
    marker_ids = [event.payload.data["checkpoint_request_id"] for event in markers]
    if len(marker_ids) != len(set(marker_ids)):
        raise ValueError("checkpoint request IDs are duplicated in the verified prefix")
    due_checkpoint_times = tuple(
        time_us
        for time_us in plan.resolved_checkpoint_times_us
        if time_us <= cut.simulation_time_us
    )
    if tuple(event.simulation_time_us for event in markers) != due_checkpoint_times:
        raise ValueError("verified checkpoint markers differ from the resolved policy")
    if _state_int(state, "checkpoint.completed_count") != len(markers):
        raise ValueError("checkpoint completed count differs from verified markers")
    future_times = tuple(
        time_us
        for time_us in plan.resolved_checkpoint_times_us
        if time_us > cut.simulation_time_us
    )
    expected_next_time = None if not future_times else future_times[0]
    if state["checkpoint.next_time_us"] != expected_next_time:
        raise ValueError("checkpoint next time differs from the semantic plan")
    expected_policy = {
        "checkpoint_policy": plan.checkpoint_policy.as_dict(),
        "resolved_checkpoint_times_us": list(plan.resolved_checkpoint_times_us),
    }
    if canonical_json_bytes(state["checkpoint.capture_policy_state"]) != canonical_json_bytes(
        expected_policy
    ):
        raise ValueError("checkpoint capture policy state differs from the semantic plan")
    expected_allocator = {
        "allocated_request_ids": marker_ids,
        "next_sequence": len(marker_ids) + 1,
    }
    if canonical_json_bytes(
        state["checkpoint.sequence_allocator_state"]
    ) != canonical_json_bytes(expected_allocator):
        raise ValueError("checkpoint request allocator differs from verified markers")
    cut_key = (
        cut.simulation_time_us,
        cut.microstep,
        cut.checkpoint_stage_ordinal,
    )
    coincident_ids = [
        event.payload.data["checkpoint_request_id"]
        for event in markers
        if event.chronological_key == cut_key
    ]
    expected_coincident = {
        "capture_time_us": cut.simulation_time_us,
        "request_ids": coincident_ids,
    }
    if canonical_json_bytes(
        state["checkpoint.coincident_request_state"]
    ) != canonical_json_bytes(expected_coincident):
        raise ValueError("checkpoint coincident requests differ from verified markers")
    expected_pending = {
        "pending_work_ids": sorted(
            item.work_id
            for item in pending_work
            if item.stage_ordinal is WorkStageV1.CHECKPOINT_CAPTURE
        )
    }
    if canonical_json_bytes(
        state["checkpoint.pending_request_state"]
    ) != canonical_json_bytes(expected_pending):
        raise ValueError("checkpoint pending requests differ from queued capture work")


def _validate_cross_record_state(
    checkpoint: RuntimeCheckpointV1,
    *,
    plan: FullDayPlanV1,
    composition_matrix: CompositionMatrixV1,
    expectation: CheckpointCompositionExpectationV1,
    event_prefix: Sequence[FullDayEventV1],
) -> None:
    states = _preserved_states(checkpoint)
    cut = checkpoint.quiescent_cut
    component_highwater = _validated_event_prefix_sequences(checkpoint, event_prefix)
    prefix_state = _validate_plan_bound_prefix(plan, event_prefix, cut)

    clock = _required_state(states, "SIMULATION_CLOCK_V1")
    if _state_int(clock, "full_day.current_time_us") != cut.simulation_time_us:
        raise ValueError("checkpoint clock differs from the quiescent cut")

    allocator = _required_state(states, "GLOBAL_EVENT_ALLOCATOR_V1")
    if (
        _state_int(allocator, "global_event_allocator.next_sequence", minimum=1)
        != cut.last_global_event_sequence + 1
    ):
        raise ValueError("global event allocator does not follow the checkpoint prefix")

    ledger = _required_state(states, "LEDGER_PREFIX_V1")
    if (
        _state_int(ledger, "ledger.event_prefix_last_global_sequence")
        != cut.event_prefix_last_global_sequence
        or ledger["ledger.event_prefix_sha256"] != cut.event_prefix_sha256
    ):
        raise ValueError("ledger prefix state differs from the checkpoint cut")

    identity = _required_state(states, "PLAN_COMPOSITION_IDENTITY_V1")
    expected_identity: dict[str, object] = {
        "plan.composition_matrix_sha256": expectation.composition_matrix_sha256,
        "plan.composition_profile_id": expectation.composition_profile_id,
        "plan.composition_profile_version": expectation.composition_profile_version,
        "plan.semantic_sha256": expectation.semantic_plan_sha256,
    }
    if any(identity[field] != value for field, value in expected_identity.items()):
        raise ValueError("preserved plan/composition identity differs from the envelope")

    calendar = _required_state(states, "CALENDAR_CURSOR_V1")
    boundary_index = _state_int(calendar, "calendar.boundary_operation_index")
    next_boundary = calendar["calendar.next_boundary_time_us"]
    if (
        boundary_index != prefix_state["boundary_operation_index"]
        or calendar["calendar.current_phase_id"] != prefix_state["current_phase_id"]
        or next_boundary != prefix_state["next_boundary_time_us"]
    ):
        raise ValueError("calendar cursor does not reconcile with verified boundaries")

    queue = _required_state(states, "SCHEDULED_WORK_QUEUE_V1")
    pending_work = _validate_pending_work(queue, cut)
    pending_events = _required_state(states, "PENDING_EVENT_QUEUES_V1")
    causal_by_work = pending_events["pending_event.causal_parent_by_work_id"]
    payloads_by_work = pending_events["pending_event.payloads_by_work_id"]
    if not isinstance(causal_by_work, Mapping) or not isinstance(
        payloads_by_work, Mapping
    ):
        raise TypeError("pending-event indexes must be work-ID keyed objects")
    pending_work_ids = {item.work_id for item in pending_work}
    plan_derived_stages = {
        WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
        WorkStageV1.SCHEDULED_INFORMATION,
        WorkStageV1.DAY_STATE_TRANSITION,
        WorkStageV1.LOCAL_STATE_TRANSITION,
        WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
        WorkStageV1.CHECKPOINT_CAPTURE,
    }
    required_pending_event_work_ids = {
        item.work_id
        for item in pending_work
        if item.stage_ordinal not in plan_derived_stages
    }
    if (
        set(causal_by_work) != set(payloads_by_work)
        or set(causal_by_work) != required_pending_event_work_ids
        or not set(causal_by_work) <= pending_work_ids
        or any(type(work_id) is not str for work_id in causal_by_work)
        or any(type(parent_id) is not str for parent_id in causal_by_work.values())
        or any(not isinstance(payload, Mapping) for payload in payloads_by_work.values())
    ):
        raise ValueError("pending-event indexes do not reconcile with pending work")
    pending_by_id = {item.work_id: item for item in pending_work}
    for work_id in causal_by_work:
        work = pending_by_id[work_id]
        if work.stage_ordinal in plan_derived_stages:
            raise ValueError("pending-event override collides with plan-derived work")
        parent_id = causal_by_work[work_id]
        if parent_id != work_id:
            raise ValueError(
                "pending event must cite its exact future causal work item"
            )
        pending_payload = FullDayEventPayloadV1.from_dict(
            parse_canonical_json_object(
                canonical_json_bytes(payloads_by_work[work_id])
            )
        )
        try:
            pending_event_type = FullDayEventTypeV1(
                pending_payload.payload_type
            )
        except ValueError as error:  # pragma: no cover - payload registry guards this
            raise ValueError("pending event payload type is unsupported") from error
        # Constructing the future outer record is a zero-side-effect reuse of the
        # frozen event contract.  It proves stage compatibility, payload/type
        # agreement, source/native ownership, and payload time bounds before the
        # checkpoint can claim the queued emission is restorable.
        FullDayEventV1(
            schema_version=1,
            global_event_sequence=cut.last_global_event_sequence + 1,
            simulation_time_us=work.simulation_time_us,
            microstep=work.microstep,
            stage=work.stage_ordinal,
            source_component_id=work.source_component_id,
            component_local_sequence=work.component_local_sequence,
            event_type=pending_event_type,
            causal_parent_ids=(work.work_id,),
            payload=pending_payload,
        )
    _validate_plan_schedule_cursors(
        states,
        plan=plan,
        cut=cut,
        event_prefix=event_prefix,
    )
    _validate_checkpoint_controller(
        _required_state(states, "CHECKPOINT_CONTROLLER_V1"),
        plan=plan,
        cut=cut,
        event_prefix=event_prefix,
        pending_work=pending_work,
    )

    runtime_state = _required_state(
        states, "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"
    )
    if runtime_state["state.plan_sha256"] != plan.semantic_sha256:
        raise ValueError("state runtime plan digest differs from checkpoint identity")
    if runtime_state["state.state_model_sha256"] != canonical_sha256(
        plan.state_model.as_dict()
    ):
        raise ValueError("state runtime model digest differs from the semantic plan")
    if (
        _state_int(runtime_state, "state.input_closed_through_time_us")
        != cut.simulation_time_us
    ):
        raise ValueError("state input closure does not reach the checkpoint cut")
    for prefix in ("state.day", "state.local"):
        entered = _state_int(runtime_state, prefix + "_entered_time_us")
        elapsed = _state_int(runtime_state, prefix + "_elapsed_age_us")
        if entered > cut.simulation_time_us or elapsed != cut.simulation_time_us - entered:
            raise ValueError(f"{prefix} age does not reconcile with checkpoint time")
    local_sequence = _state_int(runtime_state, "state.component_local_sequence")
    offset = _state_int(runtime_state, "state.component_sequence_offset")
    emissions = _state_int(runtime_state, "state.runtime_emission_count")
    if local_sequence != offset + emissions:
        raise ValueError("state runtime component-local allocator does not reconcile")
    counter_bindings = {
        "state.current_day": prefix_state["current_day"],
        "state.current_local": prefix_state["current_local"],
        "state.day_transition_count": prefix_state["day_transition_count"],
        "state.day_transitions_since_macro_anchor": prefix_state[
            "day_transitions_since_macro_anchor"
        ],
        "state.local_transition_count": prefix_state["local_transition_count"],
        "state.next_macro_segment_index": prefix_state["next_macro_segment_index"],
        "state.runtime_emission_count": prefix_state["runtime_emission_count"],
    }
    if any(runtime_state[field] != value for field, value in counter_bindings.items()):
        raise ValueError("state runtime counters differ from the verified event prefix")
    _validate_flat_state_levels(
        runtime_state,
        plan=plan,
        cut=cut,
        prefix_state=prefix_state,
        pending_work=pending_work,
    )
    runtime_reservations = tuple(
        item.component_local_sequence
        for item in pending_work
        if item.source_component_id == "FULL_DAY_RUNTIME_V1"
    )
    runtime_floor = max(
        (component_highwater.get("FULL_DAY_RUNTIME_V1", 0), *runtime_reservations)
    )
    if local_sequence < runtime_floor:
        raise ValueError(
            "full-day runtime allocator lies behind an event or pending-work identity"
        )

    non_state_allocators = _required_state(states, "COMPONENT_LOCAL_ALLOCATORS_V1")[
        "runtime.non_state_component_local_event_sequence_allocators"
    ]
    if not isinstance(non_state_allocators, Mapping) or any(
        type(owner) is not str for owner in non_state_allocators
    ):
        raise TypeError("non-state component allocators must be a string-keyed object")
    active_owner_ids = {
        expectation.state_owner_ids[component_id]
        for component_id in expectation.active_component_ids
    }
    if "FULL_DAY_RUNTIME_V1" in non_state_allocators or not set(
        non_state_allocators
    ) <= active_owner_ids:
        raise ValueError("non-state allocator map names an inactive or state-runtime owner")
    required_non_state_allocator_owners = (
        set(component_highwater)
        | {
            item.source_component_id
            for item in pending_work
        }
    ) - {"FULL_DAY_RUNTIME_V1"}
    if not required_non_state_allocator_owners <= set(non_state_allocators):
        raise ValueError(
            "non-state allocator map omits an event or pending-work owner"
        )
    for owner, value in non_state_allocators.items():
        highwater = _exact_int(value, f"component allocator {owner!r}")
        if highwater < component_highwater.get(owner, 0):
            raise ValueError("component allocator lies behind its event-prefix high-water")
    for item in pending_work:
        if item.source_component_id == "FULL_DAY_RUNTIME_V1":
            if item.component_local_sequence > local_sequence:
                raise ValueError("pending full-day work exceeds its reserved allocator")
        else:
            allocated = non_state_allocators.get(item.source_component_id)
            if type(allocated) is not int or allocated < item.component_local_sequence:
                raise ValueError("pending work is absent from its component allocator")

    observable = _required_state(states, "OBSERVABLE_PUBLICATION_CURSOR_V1")
    published = _state_int(observable, "observable.last_published_global_sequence")
    publication_time = _state_int(observable, "observable.publication_time_us")
    client_cursor = _state_int(observable, "observable.client_publication_cursor")
    delivery_events = tuple(
        event
        for event in event_prefix
        if event.event_type is FullDayEventTypeV1.OBSERVABLE_DELIVERY
    )
    expected_published = (
        0 if not delivery_events else delivery_events[-1].global_event_sequence
    )
    expected_publication_time = (
        0 if not delivery_events else delivery_events[-1].simulation_time_us
    )
    if (
        client_cursor != len(delivery_events)
        or published != expected_published
        or publication_time != expected_publication_time
    ):
        raise ValueError("observable publication cursor differs from verified deliveries")

    rng_root = _required_state(
        states, "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
    )
    root_rng = _validate_root_rng_state(
        rng_root,
        plan=plan,
        composition_matrix=composition_matrix,
    )
    expected_rng_samples = {
        plan.state_model.day_rng_substream_label: (
            2
            + 2 * _state_int(runtime_state, "state.day_transition_count")
            + 2
            * max(
                _state_int(runtime_state, "state.next_macro_segment_index") - 1,
                0,
            )
        ),
        plan.state_model.local_rng_substream_label: (
            2 + 2 * _state_int(runtime_state, "state.local_transition_count")
        ),
    }
    for label, expected_samples in expected_rng_samples.items():
        record = root_rng[label]
        if record.sample_count != expected_samples:
            raise ValueError("state-runtime RNG samples do not reconcile with transitions")
    _validate_state_rng_replay(
        plan=plan,
        event_prefix=event_prefix,
        runtime_state=runtime_state,
        root_rng=root_rng,
    )
    _validate_zero_activity_cpython_rng(
        plan=plan,
        event_prefix=event_prefix,
        cut=cut,
        root_rng=root_rng,
    )
    _validate_component_rng_copies(
        states,
        root_rng,
        expectation=expectation,
    )


def validate_runtime_checkpoint(
    checkpoint: RuntimeCheckpointV1,
    *,
    plan: FullDayPlanV1,
    composition_matrix: CompositionMatrixV1,
    inventory: CheckpointInventoryV1,
    event_prefix: Sequence[FullDayEventV1],
) -> CheckpointCompositionExpectationV1:
    """Validate the envelope and every cross-record binding required for restore."""

    if type(checkpoint) is not RuntimeCheckpointV1:
        raise TypeError("checkpoint must be RuntimeCheckpointV1")
    expectation = derive_checkpoint_composition_expectation(
        plan, composition_matrix, inventory
    )
    envelope_values = {
        "semantic_plan_sha256": expectation.semantic_plan_sha256,
        "composition_matrix_sha256": expectation.composition_matrix_sha256,
        "composition_profile_id": expectation.composition_profile_id,
        "composition_profile_version": expectation.composition_profile_version,
        "composition_profile_sha256": expectation.composition_profile_sha256,
        "checkpoint_inventory_id": expectation.checkpoint_inventory_id,
        "checkpoint_inventory_sha256": expectation.checkpoint_inventory_sha256,
        "component_inventory": expectation.component_inventory,
    }
    for field, expected in envelope_values.items():
        if getattr(checkpoint, field) != expected:
            raise ValueError(f"checkpoint {field} differs from derived composition truth")

    validate_runtime_component_inventory(
        checkpoint.components,
        expected_component_ids=expectation.component_inventory,
        active_component_ids=expectation.active_component_ids,
        dependencies_by_component=expectation.dependencies_by_component,
        absent_reasons_by_component=expectation.absent_reasons_by_component,
    )
    preserved_ids: list[str] = []
    absent_ids: list[str] = []
    for record in checkpoint.components:
        if record.status is RuntimeComponentStatusV1.PRESERVED:
            preserved_ids.append(record.component_id)
            assert record.state is not None
            if (
                record.component_schema_version
                != expectation.component_schema_versions[record.component_id]
            ):
                raise ValueError("checkpoint component state schema is incompatible")
            if (
                record.implementation_version
                != expectation.implementation_versions[record.component_id]
            ):
                raise ValueError("checkpoint component implementation is incompatible")
            validate_checkpoint_component_state_keys(
                inventory,
                component_id=record.component_id,
                state=record.state,
            )
        else:
            absent_ids.append(record.component_id)
    validate_checkpoint_capture(
        inventory,
        cut=checkpoint.quiescent_cut,
        active_component_ids=expectation.active_component_ids,
        preserved_component_ids=tuple(preserved_ids),
        absent_component_ids=tuple(absent_ids),
    )
    _validate_cross_record_state(
        checkpoint,
        plan=plan,
        composition_matrix=composition_matrix,
        expectation=expectation,
        event_prefix=event_prefix,
    )
    if len(checkpoint.canonical_bytes()) > plan.deterministic_limits.maximum_checkpoint_bytes:
        raise ValueError("checkpoint exceeds the plan's deterministic byte limit")
    return expectation


def _portable_relative_path(value: object) -> str:
    selected = _exact_string(value, "relative_path")
    posix = PurePosixPath(selected)
    windows = PureWindowsPath(selected)
    if (
        "\\" in selected
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or selected != posix.as_posix()
        or not posix.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in selected)
        or any(character in '<>:"|?*' for character in selected)
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(part.endswith((".", " ")) for part in posix.parts)
        or any(
            part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_BASENAMES
            for part in posix.parts
        )
    ):
        raise ValueError("checkpoint artifact path must be canonical portable relative POSIX")
    return selected


def checkpoint_artifact_reference(
    checkpoint: RuntimeCheckpointV1,
    relative_path: str,
    *,
    name: str | None = None,
) -> ArtifactReference:
    """Bind exact checkpoint bytes to a relocatable containing artifact record."""

    if type(checkpoint) is not RuntimeCheckpointV1:
        raise TypeError("checkpoint must be RuntimeCheckpointV1")
    selected_path = _portable_relative_path(relative_path)
    selected_name = checkpoint.checkpoint_id if name is None else _identifier(name, "name")
    if name is None and _CHECKPOINT_ID_RE.fullmatch(selected_name) is None:
        raise RuntimeError("derived checkpoint identity is malformed")
    return ArtifactReference(
        name=selected_name,
        relative_path=selected_path,
        sha256=hashlib.sha256(checkpoint.canonical_bytes()).hexdigest(),
        schema_version=RUNTIME_CHECKPOINT_SCHEMA_VERSION,
        row_count=None,
        media_type=CHECKPOINT_MEDIA_TYPE,
    )


__all__ = [
    "ABSENT_NATIVE_PLAN",
    "CHECKPOINT_MEDIA_TYPE",
    "CPYTHON_MT19937_ALGORITHM_ID",
    "CPYTHON_MT19937_CODEC_ID",
    "ENGINE_CHECKPOINT_ABI_VERSION",
    "ENGINE_RUNTIME_COMPATIBILITY_SCHEMA_VERSION",
    "EngineRuntimeCompatibilityV1",
    "OWNED_PRNG_CODEC_REGISTRY_ID",
    "OWNED_PRNG_CODEC_VERSION",
    "OWNED_PRNG_STATE_SCHEMA_VERSION",
    "OwnedPrngStateV1",
    "RUNTIME_CHECKPOINT_FORMAT_ID",
    "RUNTIME_CHECKPOINT_SCHEMA_VERSION",
    "RuntimeCheckpointV1",
    "SPLITMIX64_ALGORITHM_ID",
    "SPLITMIX64_CODEC_ID",
    "SUPPORTED_CHECKPOINT_COMPOSITION_PROFILES_V1",
    "SUPPORTED_CHECKPOINT_IMPLEMENTATION_VERSIONS_V1",
    "CheckpointCompositionExpectationV1",
    "checkpoint_artifact_reference",
    "derive_checkpoint_composition_expectation",
    "validate_checkpoint_event_prefix",
    "validate_runtime_checkpoint",
]
