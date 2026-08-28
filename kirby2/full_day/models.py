"""Strict immutable wire contracts for a Kirby2 full trading day."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from .states import (
    DAY_STATE_RNG_SUBSTREAM_PATH_V1,
    LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
    DayStateV1,
    ParameterEffectV1,
    StateModelV1,
)

if TYPE_CHECKING:
    from kirby2.exchange.mechanics_models import InstrumentRules

    from .calendar import TradingDayCalendarV1


FULL_DAY_PLAN_SCHEMA_VERSION = 1
MECHANICS_RULES_SCHEMA_VERSION = 1
SEED_POLICY_SCHEMA_VERSION = 1
FULL_DAY_SUBSTREAM_POLICY_VERSION = "FULL_DAY_SUBSTREAM_V1"
CHECKPOINT_POLICY_SCHEMA_VERSION = 1
DETERMINISTIC_LIMITS_SCHEMA_VERSION = 1
MACRO_REGIME_SCHEDULE_SEMANTICS_V1 = (
    "EXOGENOUS_HARD_RESET_AT_SEGMENT_START_THEN_STATE_MODEL_TRANSITIONS"
)
RNG_LABEL_PREFIXES_BY_COMPONENT_V1: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "AGENT_SCHEDULER_V1": ("full_day/participant",),
        "DELIVERY_ASYNC_V1": ("full_day/delivery",),
        "FLOW_HAWKES_V1": ("full_day/flow/hawkes",),
        "FLOW_QUEUE_REACTIVE_V1": ("full_day/flow/queue_reactive",),
        "FLOW_SIMPLE_V1": ("full_day/flow/simple",),
        "FULL_DAY_RUNTIME_V1": ("full_day/runtime",),
        "REGIME_ORDER_FLOW_V1": ("full_day/flow/regime",),
        "VENUE_MULTIVENUE_HIDDEN_V1": ("full_day/multivenue",),
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_SEED_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_SUPPORTED_ORDER_INSTRUCTIONS = frozenset(
    {
        "DAY",
        "FOK",
        "GOOD_UNTIL_TIME",
        "GTC",
        "IOC",
        "LIMIT",
        "MARKET",
        "MARKETABLE_LIMIT",
        "POST_ONLY",
        "SESSION",
    }
)
_SUPPORTED_STP_MODES = frozenset(
    {"NONE", "CANCEL_AGGRESSOR", "CANCEL_RESTING", "CANCEL_BOTH"}
)


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str] | frozenset[str],
    context: str,
) -> None:
    """Reject both omitted and unknown fields at every V1 schema boundary."""

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


def validate_strict_json(value: object) -> object:
    """Validate the exact semantic JSON subset and return *value* unchanged.

    Binary floats, non-string keys, non-NFC text, unsupported containers, and
    reference cycles are outside the full-day identity language.
    """

    _validate_strict_json(value, set())
    return value


def _validate_strict_json(value: object, active: set[int]) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("semantic JSON strings must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("semantic JSON strings must contain Unicode scalar values")
        return
    if type(value) is float:
        raise TypeError("binary floats are forbidden in semantic JSON")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("semantic JSON object keys must be strings")
        if any(unicodedata.normalize("NFC", key) != key for key in value):
            raise ValueError("semantic JSON object keys must be NFC-normalized")
        if any(
            0xD800 <= ord(character) <= 0xDFFF
            for key in value
            for character in key
        ):
            raise ValueError("semantic JSON object keys must contain Unicode scalar values")
        identity = id(value)
        if identity in active:
            raise ValueError("semantic JSON must not contain reference cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_strict_json(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("semantic JSON must not contain reference cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_strict_json(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported semantic JSON value: {type(value).__name__}")


def _plain_strict_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, Mapping):
        return {key: _plain_strict_json(value[key]) for key in sorted(value)}
    if type(value) in {list, tuple}:
        return [_plain_strict_json(item) for item in value]
    raise TypeError(f"unsupported semantic JSON value: {type(value).__name__}")


def _identity_projection(value: object) -> object:
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
    validate_strict_json(value)
    return _plain_strict_json(value)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize semantic JSON as compact, sorted, ASCII-escaped UTF-8 bytes."""

    return json.dumps(
        _identity_projection(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def parse_canonical_json_object(raw: bytes) -> dict[str, object]:
    """Parse one canonical JSON object, rejecting duplicate or noncanonical bytes."""

    if type(raw) is not bytes:
        raise TypeError("canonical JSON input must be bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("canonical JSON must be valid UTF-8") from error

    def reject_float(_value: str) -> object:
        raise TypeError("binary/decimal JSON numbers are forbidden in semantic JSON")

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON numbers are forbidden")

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=exact_object,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("canonical JSON is malformed") from error
    if type(value) is not dict:
        raise TypeError("canonical contract root must be a JSON object")
    validate_strict_json(value)
    if canonical_json_bytes(value) != raw:
        raise ValueError("JSON bytes are not in canonical form")
    return value


def _freeze_strict_object(value: object, context: str) -> Mapping[str, object]:
    validate_strict_json(value)
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a semantic JSON object")
    frozen = _freeze_strict_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


def _freeze_strict_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_strict_json(value[key]) for key in sorted(value)}
        )
    if type(value) in {list, tuple}:
        return tuple(_freeze_strict_json(item) for item in value)
    raise TypeError(f"unsupported semantic JSON value: {type(value).__name__}")


def _wire_int(payload: Mapping[str, object], name: str) -> int:
    value = payload[name]
    if type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer")
    return value


def _wire_optional_int(payload: Mapping[str, object], name: str) -> int | None:
    value = payload[name]
    if value is not None and type(value) is not int:
        raise TypeError(f"serialized {name} must be an integer or null")
    return value


def _wire_str(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if type(value) is not str:
        raise TypeError(f"serialized {name} must be a string")
    return value


def _wire_bool(payload: Mapping[str, object], name: str) -> bool:
    value = payload[name]
    if type(value) is not bool:
        raise TypeError(f"serialized {name} must be a boolean")
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


def _array_object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"serialized {context} must be an object")
    return value


def _validate_identifier(value: object, context: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{context} must be a string")
    if unicodedata.normalize("NFC", value) != value or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{context} is not a canonical identifier")
    return value


def _validate_sha256(value: object, context: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class VersionedReferenceV1:
    reference_id: str
    version: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_identifier(self.reference_id, "reference ID")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("reference version must be a positive integer")
        _validate_sha256(self.sha256, "reference digest")

    @property
    def profile_id(self) -> str:
        return self.reference_id

    def as_dict(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "sha256": self.sha256,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> VersionedReferenceV1:
        _require_exact_fields(
            payload,
            {"reference_id", "sha256", "version"},
            "versioned reference",
        )
        return cls(
            reference_id=_wire_str(payload, "reference_id"),
            version=_wire_int(payload, "version"),
            sha256=_wire_str(payload, "sha256"),
        )


@dataclass(frozen=True, slots=True)
class ComponentConfigurationBindingV1:
    """Bind one composition component ID to one immutable configuration."""

    component_id: str
    configuration: VersionedReferenceV1

    def __post_init__(self) -> None:
        _validate_identifier(
            self.component_id, "component configuration component ID"
        )
        if type(self.configuration) is not VersionedReferenceV1:
            raise TypeError(
                "component configuration binding requires VersionedReferenceV1"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "configuration": self.configuration.as_dict(),
        }

    @property
    def sort_key(self) -> tuple[str, str, int, str]:
        return (
            self.component_id,
            self.configuration.reference_id,
            self.configuration.version,
            self.configuration.sha256,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ComponentConfigurationBindingV1:
        _require_exact_fields(
            payload,
            {"component_id", "configuration"},
            "component configuration binding",
        )
        return cls(
            component_id=_wire_str(payload, "component_id"),
            configuration=VersionedReferenceV1.from_dict(
                _wire_object(payload, "configuration")
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountStpModeV1:
    account_id: str
    mode: str

    def __post_init__(self) -> None:
        _validate_identifier(self.account_id, "STP account ID")
        if type(self.mode) is not str or self.mode not in _SUPPORTED_STP_MODES:
            raise ValueError("unsupported self-trade prevention mode")

    def as_dict(self) -> dict[str, object]:
        return {"account_id": self.account_id, "mode": self.mode}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AccountStpModeV1:
        _require_exact_fields(payload, {"account_id", "mode"}, "STP mode")
        return cls(
            account_id=_wire_str(payload, "account_id"),
            mode=_wire_str(payload, "mode"),
        )


@dataclass(frozen=True, slots=True)
class MechanicsRulesV1:
    schema_version: int
    tick_size_numerator: int
    tick_size_denominator: int
    lot_size: int
    minimum_quantity: int
    maximum_quantity: int
    lower_price_band_ticks: int
    upper_price_band_ticks: int
    supported_order_instructions: tuple[str, ...]
    session_schedule: tuple[object, ...]
    preserve_priority_on_quantity_reduction: bool
    reference_price_ticks: int
    price_collar_ticks: int | None
    volatility_interruption_ticks: int | None
    fat_finger_ticks: int | None
    account_stp_modes: tuple[AccountStpModeV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != MECHANICS_RULES_SCHEMA_VERSION:
            raise ValueError("mechanics-rules schema version must be 1")
        if (
            type(self.tick_size_numerator) is not int
            or type(self.tick_size_denominator) is not int
            or self.tick_size_numerator <= 0
            or self.tick_size_denominator <= 0
            or math.gcd(self.tick_size_numerator, self.tick_size_denominator) != 1
        ):
            raise ValueError("tick size must be a positive reduced integer ratio")
        finite_denominator = self.tick_size_denominator
        while finite_denominator % 2 == 0:
            finite_denominator //= 2
        while finite_denominator % 5 == 0:
            finite_denominator //= 5
        if finite_denominator != 1:
            raise ValueError("tick-size denominator must have a finite decimal expansion")
        for name, value in (
            ("lot size", self.lot_size),
            ("minimum quantity", self.minimum_quantity),
            ("maximum quantity", self.maximum_quantity),
            ("lower price band", self.lower_price_band_ticks),
            ("upper price band", self.upper_price_band_ticks),
            ("reference price", self.reference_price_ticks),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"mechanics {name} must be a positive integer")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("minimum quantity exceeds maximum quantity")
        if self.minimum_quantity % self.lot_size or self.maximum_quantity % self.lot_size:
            raise ValueError("mechanics quantity limits must align to lot size")
        if not self.lower_price_band_ticks <= self.reference_price_ticks <= self.upper_price_band_ticks:
            raise ValueError("reference price is outside the mechanics price band")
        if type(self.supported_order_instructions) is not tuple or not self.supported_order_instructions:
            raise ValueError("supported order instructions require a nonempty tuple")
        if any(type(item) is not str for item in self.supported_order_instructions):
            raise TypeError("supported order instructions must be strings")
        if tuple(sorted(set(self.supported_order_instructions))) != self.supported_order_instructions:
            raise ValueError("supported order instructions must be unique and sorted")
        if set(self.supported_order_instructions) - _SUPPORTED_ORDER_INSTRUCTIONS:
            raise ValueError("mechanics rules contain an unsupported order instruction")
        if not set(self.supported_order_instructions).intersection(
            {"LIMIT", "MARKET", "MARKETABLE_LIMIT"}
        ):
            raise ValueError("mechanics rules require a primary order instruction")
        if type(self.session_schedule) is not tuple or self.session_schedule:
            raise ValueError(
                "full-day mechanics session_schedule must be explicitly empty"
            )
        if type(self.preserve_priority_on_quantity_reduction) is not bool:
            raise TypeError("priority-preservation rule must be a boolean")
        for name, value in (
            ("price collar", self.price_collar_ticks),
            ("volatility interruption", self.volatility_interruption_ticks),
            ("fat-finger distance", self.fat_finger_ticks),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"mechanics {name} must be positive ticks or null")
        if type(self.account_stp_modes) is not tuple or any(
            type(item) is not AccountStpModeV1 for item in self.account_stp_modes
        ):
            raise TypeError("account STP modes must use AccountStpModeV1")
        account_ids = tuple(item.account_id for item in self.account_stp_modes)
        if account_ids != tuple(sorted(set(account_ids))):
            raise ValueError("account STP modes must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "account_stp_modes": [item.as_dict() for item in self.account_stp_modes],
            "fat_finger_ticks": self.fat_finger_ticks,
            "lot_size": self.lot_size,
            "lower_price_band_ticks": self.lower_price_band_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_quantity": self.minimum_quantity,
            "preserve_priority_on_quantity_reduction": self.preserve_priority_on_quantity_reduction,
            "price_collar_ticks": self.price_collar_ticks,
            "reference_price_ticks": self.reference_price_ticks,
            "schema_version": self.schema_version,
            "session_schedule": [],
            "supported_order_instructions": list(self.supported_order_instructions),
            "tick_size_denominator": self.tick_size_denominator,
            "tick_size_numerator": self.tick_size_numerator,
            "upper_price_band_ticks": self.upper_price_band_ticks,
            "volatility_interruption_ticks": self.volatility_interruption_ticks,
        }

    def to_instrument_rules(self) -> InstrumentRules:
        """Build the native mechanics rules with no independent session calendar."""

        from decimal import Decimal

        from kirby2.exchange.mechanics_models import (
            InstrumentRules,
            OrderInstruction,
            SelfTradePreventionMode,
            SessionSchedule,
        )

        denominator = self.tick_size_denominator
        twos = 0
        fives = 0
        while denominator % 2 == 0:
            denominator //= 2
            twos += 1
        while denominator % 5 == 0:
            denominator //= 5
            fives += 1
        if denominator != 1:
            raise ValueError("tick-size denominator has no finite decimal expansion")
        scale = max(twos, fives)
        scaled_numerator = (
            self.tick_size_numerator
            * 2 ** (scale - twos)
            * 5 ** (scale - fives)
        )
        tick_size = Decimal(
            (0, tuple(int(character) for character in str(scaled_numerator)), -scale)
        )
        if not tick_size.is_finite():
            raise ValueError("tick size did not convert to a finite Decimal")
        return InstrumentRules(
            tick_size=tick_size,
            lot_size=self.lot_size,
            minimum_quantity=self.minimum_quantity,
            maximum_quantity=self.maximum_quantity,
            lower_price_band_ticks=self.lower_price_band_ticks,
            upper_price_band_ticks=self.upper_price_band_ticks,
            supported_order_instructions=frozenset(
                OrderInstruction(item) for item in self.supported_order_instructions
            ),
            session_schedule=SessionSchedule(()),
            preserve_priority_on_quantity_reduction=(
                self.preserve_priority_on_quantity_reduction
            ),
            reference_price_ticks=self.reference_price_ticks,
            price_collar_ticks=self.price_collar_ticks,
            volatility_interruption_ticks=self.volatility_interruption_ticks,
            fat_finger_ticks=self.fat_finger_ticks,
            account_stp_modes=tuple(
                (item.account_id, SelfTradePreventionMode(item.mode))
                for item in self.account_stp_modes
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> MechanicsRulesV1:
        fields = {
            "account_stp_modes",
            "fat_finger_ticks",
            "lot_size",
            "lower_price_band_ticks",
            "maximum_quantity",
            "minimum_quantity",
            "preserve_priority_on_quantity_reduction",
            "price_collar_ticks",
            "reference_price_ticks",
            "schema_version",
            "session_schedule",
            "supported_order_instructions",
            "tick_size_denominator",
            "tick_size_numerator",
            "upper_price_band_ticks",
            "volatility_interruption_ticks",
        }
        _require_exact_fields(payload, fields, "mechanics rules")
        schedule = _wire_array(payload, "session_schedule")
        instructions = _wire_array(payload, "supported_order_instructions")
        stp_modes = _wire_array(payload, "account_stp_modes")
        if schedule:
            raise ValueError("full-day mechanics session_schedule must be empty")
        if any(type(item) is not str for item in instructions):
            raise TypeError("serialized order instructions must be strings")
        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            tick_size_numerator=_wire_int(payload, "tick_size_numerator"),
            tick_size_denominator=_wire_int(payload, "tick_size_denominator"),
            lot_size=_wire_int(payload, "lot_size"),
            minimum_quantity=_wire_int(payload, "minimum_quantity"),
            maximum_quantity=_wire_int(payload, "maximum_quantity"),
            lower_price_band_ticks=_wire_int(payload, "lower_price_band_ticks"),
            upper_price_band_ticks=_wire_int(payload, "upper_price_band_ticks"),
            supported_order_instructions=tuple(instructions),
            session_schedule=(),
            preserve_priority_on_quantity_reduction=_wire_bool(
                payload, "preserve_priority_on_quantity_reduction"
            ),
            reference_price_ticks=_wire_int(payload, "reference_price_ticks"),
            price_collar_ticks=_wire_optional_int(payload, "price_collar_ticks"),
            volatility_interruption_ticks=_wire_optional_int(
                payload, "volatility_interruption_ticks"
            ),
            fat_finger_ticks=_wire_optional_int(payload, "fat_finger_ticks"),
            account_stp_modes=tuple(
                AccountStpModeV1.from_dict(_array_object(item, "STP mode"))
                for item in stp_modes
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedInstrumentProfileV1:
    profile: VersionedReferenceV1
    mechanics_rules: MechanicsRulesV1

    def __post_init__(self) -> None:
        if type(self.profile) is not VersionedReferenceV1:
            raise TypeError("instrument profile requires VersionedReferenceV1")
        if type(self.mechanics_rules) is not MechanicsRulesV1:
            raise TypeError("instrument profile requires MechanicsRulesV1")
        if self.mechanics_rules.session_schedule:
            raise ValueError("instrument mechanics session schedule must be empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "mechanics_rules": self.mechanics_rules.as_dict(),
            "profile": self.profile.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ResolvedInstrumentProfileV1:
        _require_exact_fields(
            payload,
            {"mechanics_rules", "profile"},
            "resolved instrument profile",
        )
        return cls(
            profile=VersionedReferenceV1.from_dict(_wire_object(payload, "profile")),
            mechanics_rules=MechanicsRulesV1.from_dict(
                _wire_object(payload, "mechanics_rules")
            ),
        )


def _validate_seed_path(value: object) -> str:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        raise ValueError("seed semantic path must be NFC text")
    segments = value.split("/")
    if len(segments) < 2 or segments[0] != "full_day":
        raise ValueError("seed semantic path must begin with 'full_day/'")
    if any(not _SEED_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ValueError("seed semantic path contains a noncanonical segment")
    return value


def derive_substream_seed(
    root_seed: int,
    policy_version: str,
    label: str,
) -> int:
    """Derive a stable unsigned 64-bit seed from root bytes and semantic path."""

    if type(root_seed) is not int or not 0 <= root_seed <= 2**63 - 1:
        raise ValueError("root seed must be an integer in [0, 2**63-1]")
    _validate_identifier(policy_version, "seed policy version")
    if unicodedata.normalize("NFC", policy_version) != policy_version:
        raise ValueError("seed policy version must be NFC text")
    label_bytes = _validate_seed_path(label).encode("utf-8")
    digest = hashlib.sha256(
        root_seed.to_bytes(8, "big")
        + b"\x00"
        + policy_version.encode("utf-8")
        + b"\x00"
        + label_bytes
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


@dataclass(frozen=True, slots=True)
class SubstreamDeclarationV1:
    semantic_path: str
    derived_seed: int

    def __post_init__(self) -> None:
        _validate_seed_path(self.semantic_path)
        if type(self.derived_seed) is not int or not 0 <= self.derived_seed <= 2**63 - 1:
            raise ValueError("derived substream seed must lie in [0, 2**63-1]")

    def as_dict(self) -> dict[str, object]:
        return {"derived_seed": self.derived_seed, "semantic_path": self.semantic_path}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SubstreamDeclarationV1:
        _require_exact_fields(
            payload,
            {"derived_seed", "semantic_path"},
            "substream declaration",
        )
        return cls(
            semantic_path=_wire_str(payload, "semantic_path"),
            derived_seed=_wire_int(payload, "derived_seed"),
        )


@dataclass(frozen=True, slots=True)
class SeedPolicyV1:
    schema_version: int
    policy_version: str
    root_seed: int
    substreams: tuple[SubstreamDeclarationV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SEED_POLICY_SCHEMA_VERSION:
            raise ValueError("seed-policy schema version must be 1")
        _validate_identifier(self.policy_version, "seed policy version")
        if unicodedata.normalize("NFC", self.policy_version) != self.policy_version:
            raise ValueError("seed policy version must be NFC text")
        if type(self.root_seed) is not int or not 0 <= self.root_seed <= 2**63 - 1:
            raise ValueError("root seed must lie in [0, 2**63-1]")
        if type(self.substreams) is not tuple or not self.substreams:
            raise ValueError("seed policy requires declared substreams")
        if any(type(item) is not SubstreamDeclarationV1 for item in self.substreams):
            raise TypeError("seed substreams must use SubstreamDeclarationV1")
        paths = tuple(item.semantic_path for item in self.substreams)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("seed semantic paths must be unique and sorted")
        for declaration in self.substreams:
            expected = derive_substream_seed(
                self.root_seed,
                self.policy_version,
                declaration.semantic_path,
            )
            if declaration.derived_seed != expected:
                raise ValueError("declared substream seed does not match deterministic derivation")

    def derive(self, semantic_path: str) -> int:
        declared = {item.semantic_path: item.derived_seed for item in self.substreams}
        path = _validate_seed_path(semantic_path)
        if path not in declared:
            raise KeyError(f"undeclared full-day RNG substream: {path}")
        return declared[path]

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "root_seed": self.root_seed,
            "schema_version": self.schema_version,
            "substreams": [item.as_dict() for item in self.substreams],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SeedPolicyV1:
        _require_exact_fields(
            payload,
            {"policy_version", "root_seed", "schema_version", "substreams"},
            "seed policy",
        )
        substreams = _wire_array(payload, "substreams")
        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            policy_version=_wire_str(payload, "policy_version"),
            root_seed=_wire_int(payload, "root_seed"),
            substreams=tuple(
                SubstreamDeclarationV1.from_dict(
                    _array_object(item, "substream declaration")
                )
                for item in substreams
            ),
        )


class PressureKindV1(str, Enum):
    VOLUME = "VOLUME"
    LIQUIDITY = "LIQUIDITY"
    VOLATILITY = "VOLATILITY"


@dataclass(frozen=True, slots=True)
class PressureSegmentV1:
    start_us: int
    end_us: int
    modifier_ppm: int

    def __post_init__(self) -> None:
        if (
            type(self.start_us) is not int
            or type(self.end_us) is not int
            or self.start_us < 0
            or self.end_us <= self.start_us
        ):
            raise ValueError("pressure segment must be a forward microsecond interval")
        if type(self.modifier_ppm) is not int or not 0 <= self.modifier_ppm <= 10_000_000:
            raise ValueError("pressure modifier must lie in [0, 10000000] ppm")

    def as_dict(self) -> dict[str, object]:
        return {
            "end_us": self.end_us,
            "modifier_ppm": self.modifier_ppm,
            "start_us": self.start_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PressureSegmentV1:
        _require_exact_fields(
            payload,
            {"end_us", "modifier_ppm", "start_us"},
            "pressure segment",
        )
        return cls(
            start_us=_wire_int(payload, "start_us"),
            end_us=_wire_int(payload, "end_us"),
            modifier_ppm=_wire_int(payload, "modifier_ppm"),
        )


@dataclass(frozen=True, slots=True)
class PressureProfileV1:
    profile_id: str
    profile_version: int
    pressure_kind: PressureKindV1
    minimum_ppm: int
    maximum_ppm: int
    segments: tuple[PressureSegmentV1, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.profile_id, "pressure profile ID")
        if type(self.profile_version) is not int or self.profile_version <= 0:
            raise ValueError("pressure profile version must be positive")
        if type(self.pressure_kind) is not PressureKindV1:
            raise TypeError("pressure kind must use PressureKindV1")
        if (
            type(self.minimum_ppm) is not int
            or type(self.maximum_ppm) is not int
            or not 0 <= self.minimum_ppm <= self.maximum_ppm <= 10_000_000
        ):
            raise ValueError("pressure bounds must lie in [0, 10000000] ppm")
        if type(self.segments) is not tuple or not self.segments or any(
            type(item) is not PressureSegmentV1 for item in self.segments
        ):
            raise ValueError("pressure profile requires typed segments")
        if self.segments[0].start_us != 0:
            raise ValueError("pressure profile coverage must begin at t=0")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.end_us != current.start_us:
                raise ValueError("pressure profile segments must be contiguous and ordered")
        if any(
            not self.minimum_ppm <= item.modifier_ppm <= self.maximum_ppm
            for item in self.segments
        ):
            raise ValueError("pressure modifier exceeds its declared profile bounds")

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_ppm": self.maximum_ppm,
            "minimum_ppm": self.minimum_ppm,
            "pressure_kind": self.pressure_kind.value,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "segments": [item.as_dict() for item in self.segments],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PressureProfileV1:
        fields = {
            "maximum_ppm",
            "minimum_ppm",
            "pressure_kind",
            "profile_id",
            "profile_version",
            "segments",
        }
        _require_exact_fields(payload, fields, "pressure profile")
        return cls(
            profile_id=_wire_str(payload, "profile_id"),
            profile_version=_wire_int(payload, "profile_version"),
            pressure_kind=PressureKindV1(_wire_str(payload, "pressure_kind")),
            minimum_ppm=_wire_int(payload, "minimum_ppm"),
            maximum_ppm=_wire_int(payload, "maximum_ppm"),
            segments=tuple(
                PressureSegmentV1.from_dict(_array_object(item, "pressure segment"))
                for item in _wire_array(payload, "segments")
            ),
        )


@dataclass(frozen=True, slots=True)
class MacroRegimeSegmentV1:
    start_us: int
    end_us: int
    day_state: DayStateV1

    def __post_init__(self) -> None:
        if (
            type(self.start_us) is not int
            or type(self.end_us) is not int
            or self.start_us < 0
            or self.end_us <= self.start_us
        ):
            raise ValueError("macro regime must be a forward microsecond interval")
        if type(self.day_state) is not DayStateV1:
            raise TypeError("macro regime day state must use DayStateV1")

    def as_dict(self) -> dict[str, object]:
        return {
            "day_state": self.day_state.value,
            "end_us": self.end_us,
            "start_us": self.start_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> MacroRegimeSegmentV1:
        _require_exact_fields(
            payload,
            {"day_state", "end_us", "start_us"},
            "macro-regime segment",
        )
        return cls(
            start_us=_wire_int(payload, "start_us"),
            end_us=_wire_int(payload, "end_us"),
            day_state=DayStateV1(_wire_str(payload, "day_state")),
        )


class ParticipantKindV1(str, Enum):
    MARKET_MAKER = "MARKET_MAKER"
    NOISE_FLOW = "NOISE_FLOW"
    METAORDER = "METAORDER"
    DISTRESSED_FLOW = "DISTRESSED_FLOW"
    LIQUIDITY_PROVIDER = "LIQUIDITY_PROVIDER"
    AUCTION_PARTICIPANT = "AUCTION_PARTICIPANT"


class ParticipantScheduleActionV1(str, Enum):
    ACTIVATE = "ACTIVATE"
    RETUNE = "RETUNE"
    DEACTIVATE = "DEACTIVATE"


@dataclass(frozen=True, slots=True)
class ParticipantDefinitionV1:
    participant_id: str
    participant_kind: ParticipantKindV1
    specification: VersionedReferenceV1
    rng_substream_label: str
    initially_active: bool

    def __post_init__(self) -> None:
        _validate_identifier(self.participant_id, "participant ID")
        if type(self.participant_kind) is not ParticipantKindV1:
            raise TypeError("participant kind must use ParticipantKindV1")
        if type(self.specification) is not VersionedReferenceV1:
            raise TypeError("participant specification must be a versioned reference")
        _validate_seed_path(self.rng_substream_label)
        if type(self.initially_active) is not bool:
            raise TypeError("initial participant activation must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "initially_active": self.initially_active,
            "participant_id": self.participant_id,
            "participant_kind": self.participant_kind.value,
            "rng_substream_label": self.rng_substream_label,
            "specification": self.specification.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ParticipantDefinitionV1:
        _require_exact_fields(
            payload,
            {
                "initially_active",
                "participant_id",
                "participant_kind",
                "rng_substream_label",
                "specification",
            },
            "participant definition",
        )
        return cls(
            participant_id=_wire_str(payload, "participant_id"),
            participant_kind=ParticipantKindV1(_wire_str(payload, "participant_kind")),
            specification=VersionedReferenceV1.from_dict(
                _wire_object(payload, "specification")
            ),
            rng_substream_label=_wire_str(payload, "rng_substream_label"),
            initially_active=_wire_bool(payload, "initially_active"),
        )


@dataclass(frozen=True, slots=True)
class ParticipantScheduleEntryV1:
    schedule_id: str
    simulation_time_us: int
    participant_id: str
    action: ParticipantScheduleActionV1
    replacement_specification: VersionedReferenceV1 | None

    def __post_init__(self) -> None:
        _validate_identifier(self.schedule_id, "participant schedule ID")
        _validate_identifier(self.participant_id, "scheduled participant ID")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("participant schedule time must be nonnegative microseconds")
        if type(self.action) is not ParticipantScheduleActionV1:
            raise TypeError("participant schedule action uses the wrong enum")
        if self.action is ParticipantScheduleActionV1.RETUNE:
            if type(self.replacement_specification) is not VersionedReferenceV1:
                raise ValueError("RETUNE requires a replacement specification")
        elif self.replacement_specification is not None:
            raise ValueError("only RETUNE may carry a replacement specification")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "participant_id": self.participant_id,
            "replacement_specification": (
                None
                if self.replacement_specification is None
                else self.replacement_specification.as_dict()
            ),
            "schedule_id": self.schedule_id,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ParticipantScheduleEntryV1:
        _require_exact_fields(
            payload,
            {
                "action",
                "participant_id",
                "replacement_specification",
                "schedule_id",
                "simulation_time_us",
            },
            "participant schedule entry",
        )
        raw_spec = payload["replacement_specification"]
        if raw_spec is not None and not isinstance(raw_spec, Mapping):
            raise TypeError("replacement specification must be an object or null")
        return cls(
            schedule_id=_wire_str(payload, "schedule_id"),
            simulation_time_us=_wire_int(payload, "simulation_time_us"),
            participant_id=_wire_str(payload, "participant_id"),
            action=ParticipantScheduleActionV1(_wire_str(payload, "action")),
            replacement_specification=(
                None if raw_spec is None else VersionedReferenceV1.from_dict(raw_spec)
            ),
        )


class ScheduledEventTypeV1(str, Enum):
    ECONOMIC_ANNOUNCEMENT = "ECONOMIC_ANNOUNCEMENT"
    EARNINGS_LIKE_RELEASE = "EARNINGS_LIKE_RELEASE"
    NEWS_SHOCK = "NEWS_SHOCK"
    LARGE_SCHEDULED_METAORDER = "LARGE_SCHEDULED_METAORDER"
    AUCTION_IMBALANCE_PUBLICATION = "AUCTION_IMBALANCE_PUBLICATION"
    VOLATILITY_INTERRUPTION = "VOLATILITY_INTERRUPTION"
    HALT = "HALT"
    REOPENING = "REOPENING"


class IntegerParameterUnitV1(str, Enum):
    TICKS = "TICKS"
    SHARES = "SHARES"
    MICROSECONDS = "MICROSECONDS"
    PPM = "PPM"
    COUNT = "COUNT"


class FlowSideV1(str, Enum):
    NONE = "NONE"
    BUY = "BUY"
    SELL = "SELL"


_SCHEDULED_EVENT_PHASE_ORDER_V1 = (
    "PREOPEN",
    "OPENING_AUCTION",
    "CONTINUOUS",
    "CLOSING_AUCTION",
    "POSTCLOSE",
)
SCHEDULED_EVENT_MAX_DURATION_US_V1 = 86_400_000_000
SCHEDULED_EVENT_MAX_SHARES_V1 = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class ScheduledEventParameterRuleV1:
    """Frozen unit and range contract for one scheduled-event parameter."""

    name: str
    unit: IntegerParameterUnitV1
    minimum_value: int
    maximum_value: int

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "scheduled-event parameter-spec name")
        if type(self.unit) is not IntegerParameterUnitV1:
            raise TypeError("scheduled-event parameter spec uses the wrong unit enum")
        if type(self.minimum_value) is not int:
            raise TypeError("scheduled-event parameter minimum must be an integer")
        if (
            type(self.maximum_value) is not int
            or self.maximum_value <= 0
            or self.maximum_value < self.minimum_value
        ):
            raise ValueError(
                "scheduled-event parameter maximum must be a positive finite integer "
                "at least as large as its minimum"
            )


@dataclass(frozen=True, slots=True)
class ScheduledEventSemanticSpecV1:
    """Frozen semantic contract for one ScheduledEventTypeV1 member."""

    event_type: ScheduledEventTypeV1
    parameters: tuple[ScheduledEventParameterRuleV1, ...]
    allowed_sides: tuple[FlowSideV1, ...]
    allowed_phase_ids: tuple[str, ...]
    population_reference_required: bool
    mechanics_reference_required: bool

    def __post_init__(self) -> None:
        if type(self.event_type) is not ScheduledEventTypeV1:
            raise TypeError("scheduled-event spec uses the wrong event-type enum")
        if type(self.parameters) is not tuple or not self.parameters or any(
            type(item) is not ScheduledEventParameterRuleV1
            for item in self.parameters
        ):
            raise TypeError(
                "scheduled-event spec parameters require immutable V1 records"
            )
        parameter_names = tuple(item.name for item in self.parameters)
        if parameter_names != tuple(sorted(set(parameter_names))):
            raise ValueError(
                "scheduled-event spec parameter names must be unique and sorted"
            )
        if type(self.allowed_sides) is not tuple or not self.allowed_sides or any(
            type(side) is not FlowSideV1 for side in self.allowed_sides
        ):
            raise TypeError("scheduled-event spec sides require a nonempty enum tuple")
        side_positions = tuple(
            tuple(FlowSideV1).index(side) for side in self.allowed_sides
        )
        if side_positions != tuple(sorted(set(side_positions))):
            raise ValueError(
                "scheduled-event spec sides must be unique and in enum order"
            )
        if (
            type(self.allowed_phase_ids) is not tuple
            or not self.allowed_phase_ids
            or any(
                type(phase_id) is not str
                or phase_id not in _SCHEDULED_EVENT_PHASE_ORDER_V1
                for phase_id in self.allowed_phase_ids
            )
        ):
            raise ValueError(
                "scheduled-event spec phases require canonical calendar phase IDs"
            )
        phase_positions = tuple(
            _SCHEDULED_EVENT_PHASE_ORDER_V1.index(phase_id)
            for phase_id in self.allowed_phase_ids
        )
        if phase_positions != tuple(sorted(set(phase_positions))):
            raise ValueError(
                "scheduled-event spec phases must be unique and in calendar order"
            )
        if (
            type(self.population_reference_required) is not bool
            or type(self.mechanics_reference_required) is not bool
        ):
            raise TypeError("scheduled-event reference requirements must be booleans")
        if self.population_reference_required and self.mechanics_reference_required:
            raise ValueError(
                "one scheduled-event type cannot require both reference families"
            )

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.parameters)


SCHEDULED_EVENT_SEMANTICS_V1: Mapping[
    ScheduledEventTypeV1, ScheduledEventSemanticSpecV1
] = MappingProxyType(
    {
        ScheduledEventTypeV1.ECONOMIC_ANNOUNCEMENT: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.ECONOMIC_ANNOUNCEMENT,
            (
                ScheduledEventParameterRuleV1(
                    "impact_ppm", IntegerParameterUnitV1.PPM, 1, 10_000_000
                ),
            ),
            (FlowSideV1.NONE,),
            ("PREOPEN", "CONTINUOUS", "POSTCLOSE"),
            False,
            False,
        ),
        ScheduledEventTypeV1.EARNINGS_LIKE_RELEASE: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.EARNINGS_LIKE_RELEASE,
            (
                ScheduledEventParameterRuleV1(
                    "impact_ppm", IntegerParameterUnitV1.PPM, 1, 10_000_000
                ),
            ),
            (FlowSideV1.BUY, FlowSideV1.SELL),
            ("PREOPEN", "POSTCLOSE"),
            False,
            False,
        ),
        ScheduledEventTypeV1.NEWS_SHOCK: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.NEWS_SHOCK,
            (
                ScheduledEventParameterRuleV1(
                    "duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    1,
                    SCHEDULED_EVENT_MAX_DURATION_US_V1,
                ),
                ScheduledEventParameterRuleV1(
                    "impact_ppm", IntegerParameterUnitV1.PPM, 1, 10_000_000
                ),
            ),
            (FlowSideV1.BUY, FlowSideV1.SELL),
            ("PREOPEN", "CONTINUOUS", "POSTCLOSE"),
            False,
            False,
        ),
        (
            ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER
        ): ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER,
            (
                ScheduledEventParameterRuleV1(
                    "duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    1,
                    SCHEDULED_EVENT_MAX_DURATION_US_V1,
                ),
                ScheduledEventParameterRuleV1(
                    "participation_ppm", IntegerParameterUnitV1.PPM, 1, 1_000_000
                ),
                ScheduledEventParameterRuleV1(
                    "quantity_shares",
                    IntegerParameterUnitV1.SHARES,
                    1,
                    SCHEDULED_EVENT_MAX_SHARES_V1,
                ),
            ),
            (FlowSideV1.BUY, FlowSideV1.SELL),
            ("CONTINUOUS",),
            True,
            False,
        ),
        (
            ScheduledEventTypeV1.AUCTION_IMBALANCE_PUBLICATION
        ): ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.AUCTION_IMBALANCE_PUBLICATION,
            (
                ScheduledEventParameterRuleV1(
                    "imbalance_shares",
                    IntegerParameterUnitV1.SHARES,
                    1,
                    SCHEDULED_EVENT_MAX_SHARES_V1,
                ),
            ),
            (FlowSideV1.BUY, FlowSideV1.SELL),
            ("OPENING_AUCTION", "CLOSING_AUCTION"),
            False,
            True,
        ),
        ScheduledEventTypeV1.VOLATILITY_INTERRUPTION: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
            (
                ScheduledEventParameterRuleV1(
                    "halt_duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    1,
                    SCHEDULED_EVENT_MAX_DURATION_US_V1,
                ),
            ),
            (FlowSideV1.NONE,),
            ("CONTINUOUS",),
            False,
            True,
        ),
        ScheduledEventTypeV1.HALT: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.HALT,
            (
                ScheduledEventParameterRuleV1(
                    "halt_duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    1,
                    SCHEDULED_EVENT_MAX_DURATION_US_V1,
                ),
            ),
            (FlowSideV1.NONE,),
            ("CONTINUOUS",),
            False,
            True,
        ),
        ScheduledEventTypeV1.REOPENING: ScheduledEventSemanticSpecV1(
            ScheduledEventTypeV1.REOPENING,
            (
                ScheduledEventParameterRuleV1(
                    "reopening_auction_duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    1,
                    SCHEDULED_EVENT_MAX_DURATION_US_V1,
                ),
            ),
            (FlowSideV1.NONE,),
            ("CONTINUOUS",),
            False,
            True,
        ),
    }
)

if tuple(SCHEDULED_EVENT_SEMANTICS_V1) != tuple(ScheduledEventTypeV1):
    raise RuntimeError("scheduled-event V1 registry must cover all event types in order")


@dataclass(frozen=True, slots=True)
class NamedIntegerParameterV1:
    name: str
    unit: IntegerParameterUnitV1
    value: int

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "event parameter name")
        lowered = self.name.lower()
        forbidden = ("target_price", "desired_return", "force_trade", "book_write", "forced_close")
        if any(token in lowered for token in forbidden):
            raise ValueError("imperative price/book/return event parameters are forbidden")
        if type(self.unit) is not IntegerParameterUnitV1:
            raise TypeError("event parameter unit uses the wrong enum")
        if type(self.value) is not int:
            raise TypeError("event parameter value must be an integer")
        if self.unit in {
            IntegerParameterUnitV1.SHARES,
            IntegerParameterUnitV1.MICROSECONDS,
            IntegerParameterUnitV1.COUNT,
        } and self.value < 0:
            raise ValueError("unsigned event parameter cannot be negative")
        if self.unit is IntegerParameterUnitV1.PPM and not 0 <= self.value <= 10_000_000:
            raise ValueError("event PPM parameter is outside its bounded range")

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "unit": self.unit.value, "value": self.value}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NamedIntegerParameterV1:
        _require_exact_fields(
            payload,
            {"name", "unit", "value"},
            "named integer parameter",
        )
        return cls(
            name=_wire_str(payload, "name"),
            unit=IntegerParameterUnitV1(_wire_str(payload, "unit")),
            value=_wire_int(payload, "value"),
        )


@dataclass(frozen=True, slots=True)
class ScheduledEventV1:
    event_id: str
    simulation_time_us: int
    event_type: ScheduledEventTypeV1
    stage_ordinal: int
    side: FlowSideV1
    parameters: tuple[NamedIntegerParameterV1, ...]
    population_reference: VersionedReferenceV1 | None
    mechanics_reference: VersionedReferenceV1 | None

    def __post_init__(self) -> None:
        _validate_identifier(self.event_id, "scheduled event ID")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("scheduled event time must be nonnegative microseconds")
        if type(self.event_type) is not ScheduledEventTypeV1:
            raise TypeError("scheduled event type uses the wrong enum")
        if type(self.stage_ordinal) is not int or self.stage_ordinal != 1:
            raise ValueError("scheduled information must use microstep-zero stage 1")
        if type(self.side) is not FlowSideV1:
            raise TypeError("scheduled event side uses the wrong enum")
        if type(self.parameters) is not tuple or any(
            type(item) is not NamedIntegerParameterV1 for item in self.parameters
        ):
            raise TypeError("scheduled event parameters use the wrong contract")
        names = tuple(item.name for item in self.parameters)
        if names != tuple(sorted(set(names))):
            raise ValueError("scheduled event parameters must be unique and sorted")
        for value, context in (
            (self.population_reference, "population reference"),
            (self.mechanics_reference, "mechanics reference"),
        ):
            if value is not None and type(value) is not VersionedReferenceV1:
                raise TypeError(f"scheduled event {context} uses the wrong contract")
        specification = SCHEDULED_EVENT_SEMANTICS_V1[self.event_type]
        if names != specification.parameter_names:
            raise ValueError(
                f"{self.event_type.value} requires exact sorted parameters "
                f"{specification.parameter_names}"
            )
        for parameter, parameter_spec in zip(
            self.parameters, specification.parameters, strict=True
        ):
            if parameter.unit is not parameter_spec.unit:
                raise ValueError(
                    f"{self.event_type.value} parameter {parameter.name} requires "
                    f"unit {parameter_spec.unit.value}"
                )
            if not (
                parameter_spec.minimum_value
                <= parameter.value
                <= parameter_spec.maximum_value
            ):
                raise ValueError(
                    f"{self.event_type.value} parameter {parameter.name} must lie "
                    f"in [{parameter_spec.minimum_value}, "
                    f"{parameter_spec.maximum_value}]"
                )
        if self.side not in specification.allowed_sides:
            raise ValueError(
                f"{self.event_type.value} side must be one of "
                f"{tuple(side.value for side in specification.allowed_sides)}"
            )
        if specification.population_reference_required:
            if self.population_reference is None:
                raise ValueError(
                    f"{self.event_type.value} requires a population reference"
                )
        elif self.population_reference is not None:
            raise ValueError(
                f"{self.event_type.value} forbids a population reference"
            )
        if specification.mechanics_reference_required:
            if self.mechanics_reference is None:
                raise ValueError(
                    f"{self.event_type.value} requires a mechanics reference"
                )
        elif self.mechanics_reference is not None:
            raise ValueError(
                f"{self.event_type.value} forbids a mechanics reference"
            )

    @property
    def parameter_set_sha256(self) -> str:
        """Digest the canonical ordered parameter array without event metadata."""

        return canonical_sha256(
            [parameter.as_dict() for parameter in self.parameters]
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "mechanics_reference": None if self.mechanics_reference is None else self.mechanics_reference.as_dict(),
            "parameters": [item.as_dict() for item in self.parameters],
            "population_reference": None if self.population_reference is None else self.population_reference.as_dict(),
            "side": self.side.value,
            "simulation_time_us": self.simulation_time_us,
            "stage_ordinal": self.stage_ordinal,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScheduledEventV1:
        fields = {
            "event_id",
            "event_type",
            "mechanics_reference",
            "parameters",
            "population_reference",
            "side",
            "simulation_time_us",
            "stage_ordinal",
        }
        _require_exact_fields(payload, fields, "scheduled event")
        population = payload["population_reference"]
        mechanics = payload["mechanics_reference"]
        if population is not None and not isinstance(population, Mapping):
            raise TypeError("population reference must be an object or null")
        if mechanics is not None and not isinstance(mechanics, Mapping):
            raise TypeError("mechanics reference must be an object or null")
        return cls(
            event_id=_wire_str(payload, "event_id"),
            simulation_time_us=_wire_int(payload, "simulation_time_us"),
            event_type=ScheduledEventTypeV1(_wire_str(payload, "event_type")),
            stage_ordinal=_wire_int(payload, "stage_ordinal"),
            side=FlowSideV1(_wire_str(payload, "side")),
            parameters=tuple(
                NamedIntegerParameterV1.from_dict(
                    _array_object(item, "named integer parameter")
                )
                for item in _wire_array(payload, "parameters")
            ),
            population_reference=(
                None if population is None else VersionedReferenceV1.from_dict(population)
            ),
            mechanics_reference=(
                None if mechanics is None else VersionedReferenceV1.from_dict(mechanics)
            ),
        )


@dataclass(frozen=True, slots=True)
class UnscheduledShockPolicyV1:
    policy_id: str
    policy_version: int
    enabled: bool
    candidate_window_start_us: int
    candidate_window_end_us: int
    maximum_candidate_draws: int
    maximum_accepted_shocks: int
    minimum_spacing_us: int
    acceptance_numerator: int
    acceptance_denominator: int
    substream_label: str
    allowed_sides: tuple[FlowSideV1, ...]
    quantity_distribution_reference: VersionedReferenceV1
    parameter_effects: tuple[ParameterEffectV1, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.policy_id, "shock policy ID")
        if type(self.policy_version) is not int or self.policy_version <= 0:
            raise ValueError("shock policy version must be positive")
        if type(self.enabled) is not bool:
            raise TypeError("shock policy enabled flag must be boolean")
        if (
            type(self.candidate_window_start_us) is not int
            or type(self.candidate_window_end_us) is not int
            or self.candidate_window_start_us < 0
            or self.candidate_window_end_us <= self.candidate_window_start_us
        ):
            raise ValueError("shock candidate window must be a forward interval")
        integers = (
            self.maximum_candidate_draws,
            self.maximum_accepted_shocks,
            self.minimum_spacing_us,
        )
        if any(type(value) is not int or value < 0 for value in integers):
            raise ValueError("shock draw/acceptance/spacing bounds must be nonnegative")
        if self.maximum_accepted_shocks > self.maximum_candidate_draws:
            raise ValueError("accepted shocks cannot exceed candidate draws")
        if self.enabled and self.maximum_candidate_draws == 0:
            raise ValueError("enabled shock policy requires a positive draw bound")
        if not self.enabled and self.maximum_accepted_shocks != 0:
            raise ValueError("disabled shock policy cannot accept shocks")
        if (
            type(self.acceptance_numerator) is not int
            or type(self.acceptance_denominator) is not int
            or self.acceptance_denominator <= 0
            or not 0 <= self.acceptance_numerator <= self.acceptance_denominator
            or math.gcd(self.acceptance_numerator, self.acceptance_denominator) != 1
        ):
            raise ValueError("shock acceptance probability must be a reduced ratio in [0,1]")
        _validate_seed_path(self.substream_label)
        if type(self.allowed_sides) is not tuple or not self.allowed_sides:
            raise ValueError("shock policy requires allowed sides")
        if any(side not in {FlowSideV1.BUY, FlowSideV1.SELL} for side in self.allowed_sides):
            raise ValueError("shock allowed sides must be BUY and/or SELL")
        if self.allowed_sides != tuple(sorted(set(self.allowed_sides), key=lambda item: item.value)):
            raise ValueError("shock allowed sides must be unique and sorted")
        if type(self.quantity_distribution_reference) is not VersionedReferenceV1:
            raise TypeError("shock quantity distribution must be versioned")
        if type(self.parameter_effects) is not tuple or any(
            type(item) is not ParameterEffectV1 for item in self.parameter_effects
        ):
            raise TypeError("shock parameter effects use the wrong contract")
        targets = tuple(item.target for item in self.parameter_effects)
        if targets != tuple(sorted(set(targets), key=lambda item: item.value)):
            raise ValueError("shock parameter effects must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "acceptance_denominator": self.acceptance_denominator,
            "acceptance_numerator": self.acceptance_numerator,
            "allowed_sides": [item.value for item in self.allowed_sides],
            "candidate_window_end_us": self.candidate_window_end_us,
            "candidate_window_start_us": self.candidate_window_start_us,
            "enabled": self.enabled,
            "maximum_accepted_shocks": self.maximum_accepted_shocks,
            "maximum_candidate_draws": self.maximum_candidate_draws,
            "minimum_spacing_us": self.minimum_spacing_us,
            "parameter_effects": [item.as_dict() for item in self.parameter_effects],
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "quantity_distribution_reference": self.quantity_distribution_reference.as_dict(),
            "substream_label": self.substream_label,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> UnscheduledShockPolicyV1:
        fields = {
            "acceptance_denominator",
            "acceptance_numerator",
            "allowed_sides",
            "candidate_window_end_us",
            "candidate_window_start_us",
            "enabled",
            "maximum_accepted_shocks",
            "maximum_candidate_draws",
            "minimum_spacing_us",
            "parameter_effects",
            "policy_id",
            "policy_version",
            "quantity_distribution_reference",
            "substream_label",
        }
        _require_exact_fields(payload, fields, "unscheduled shock policy")
        sides = _wire_array(payload, "allowed_sides")
        if any(type(side) is not str for side in sides):
            raise TypeError("shock allowed sides must be strings")
        return cls(
            policy_id=_wire_str(payload, "policy_id"),
            policy_version=_wire_int(payload, "policy_version"),
            enabled=_wire_bool(payload, "enabled"),
            candidate_window_start_us=_wire_int(payload, "candidate_window_start_us"),
            candidate_window_end_us=_wire_int(payload, "candidate_window_end_us"),
            maximum_candidate_draws=_wire_int(payload, "maximum_candidate_draws"),
            maximum_accepted_shocks=_wire_int(payload, "maximum_accepted_shocks"),
            minimum_spacing_us=_wire_int(payload, "minimum_spacing_us"),
            acceptance_numerator=_wire_int(payload, "acceptance_numerator"),
            acceptance_denominator=_wire_int(payload, "acceptance_denominator"),
            substream_label=_wire_str(payload, "substream_label"),
            allowed_sides=tuple(FlowSideV1(side) for side in sides),
            quantity_distribution_reference=VersionedReferenceV1.from_dict(
                _wire_object(payload, "quantity_distribution_reference")
            ),
            parameter_effects=tuple(
                ParameterEffectV1.from_dict(_array_object(item, "parameter effect"))
                for item in _wire_array(payload, "parameter_effects")
            ),
        )


@dataclass(frozen=True, slots=True)
class HaltReopenRulesV1:
    rules_id: str
    rules_version: int
    halt_trigger_reference: VersionedReferenceV1
    resume_trigger_reference: VersionedReferenceV1
    minimum_halt_duration_us: int
    maximum_halt_duration_us: int
    reopening_auction_duration_us: int
    maximum_halts: int
    uncross_before_resume: bool
    expire_day_orders_on_halt: bool
    expire_session_orders_on_close: bool

    def __post_init__(self) -> None:
        _validate_identifier(self.rules_id, "halt/reopen rules ID")
        if type(self.rules_version) is not int or self.rules_version <= 0:
            raise ValueError("halt/reopen rules version must be positive")
        if type(self.halt_trigger_reference) is not VersionedReferenceV1 or type(
            self.resume_trigger_reference
        ) is not VersionedReferenceV1:
            raise TypeError("halt and resume triggers must be versioned references")
        durations = (
            self.minimum_halt_duration_us,
            self.maximum_halt_duration_us,
            self.reopening_auction_duration_us,
        )
        if any(type(value) is not int or value <= 0 for value in durations):
            raise ValueError("halt/reopen durations must be positive microseconds")
        if self.maximum_halt_duration_us < self.minimum_halt_duration_us:
            raise ValueError("maximum halt duration precedes minimum")
        if type(self.maximum_halts) is not int or self.maximum_halts < 0:
            raise ValueError("maximum halt count must be nonnegative")
        for value, context in (
            (self.uncross_before_resume, "uncross-before-resume"),
            (self.expire_day_orders_on_halt, "day-order expiry"),
            (self.expire_session_orders_on_close, "session-order expiry"),
        ):
            if type(value) is not bool:
                raise TypeError(f"halt/reopen {context} must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "expire_day_orders_on_halt": self.expire_day_orders_on_halt,
            "expire_session_orders_on_close": self.expire_session_orders_on_close,
            "halt_trigger_reference": self.halt_trigger_reference.as_dict(),
            "maximum_halt_duration_us": self.maximum_halt_duration_us,
            "maximum_halts": self.maximum_halts,
            "minimum_halt_duration_us": self.minimum_halt_duration_us,
            "reopening_auction_duration_us": self.reopening_auction_duration_us,
            "resume_trigger_reference": self.resume_trigger_reference.as_dict(),
            "rules_id": self.rules_id,
            "rules_version": self.rules_version,
            "uncross_before_resume": self.uncross_before_resume,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> HaltReopenRulesV1:
        fields = {
            "expire_day_orders_on_halt",
            "expire_session_orders_on_close",
            "halt_trigger_reference",
            "maximum_halt_duration_us",
            "maximum_halts",
            "minimum_halt_duration_us",
            "reopening_auction_duration_us",
            "resume_trigger_reference",
            "rules_id",
            "rules_version",
            "uncross_before_resume",
        }
        _require_exact_fields(payload, fields, "halt/reopen rules")
        return cls(
            rules_id=_wire_str(payload, "rules_id"),
            rules_version=_wire_int(payload, "rules_version"),
            halt_trigger_reference=VersionedReferenceV1.from_dict(
                _wire_object(payload, "halt_trigger_reference")
            ),
            resume_trigger_reference=VersionedReferenceV1.from_dict(
                _wire_object(payload, "resume_trigger_reference")
            ),
            minimum_halt_duration_us=_wire_int(payload, "minimum_halt_duration_us"),
            maximum_halt_duration_us=_wire_int(payload, "maximum_halt_duration_us"),
            reopening_auction_duration_us=_wire_int(
                payload, "reopening_auction_duration_us"
            ),
            maximum_halts=_wire_int(payload, "maximum_halts"),
            uncross_before_resume=_wire_bool(payload, "uncross_before_resume"),
            expire_day_orders_on_halt=_wire_bool(payload, "expire_day_orders_on_halt"),
            expire_session_orders_on_close=_wire_bool(
                payload, "expire_session_orders_on_close"
            ),
        )


@dataclass(frozen=True, slots=True)
class CheckpointPolicyV1:
    schema_version: int
    interval_us: int | None
    explicit_times_us: tuple[int, ...]
    require_quiescence: bool
    after_t0_microsteps: bool
    at_phase_boundaries: bool
    include_event_prefix_digest: bool
    maximum_checkpoint_count: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CHECKPOINT_POLICY_SCHEMA_VERSION:
            raise ValueError("checkpoint-policy schema version must be 1")
        if self.interval_us is not None and (
            type(self.interval_us) is not int or self.interval_us <= 0
        ):
            raise ValueError("checkpoint interval must be positive microseconds or null")
        if type(self.explicit_times_us) is not tuple or any(
            type(value) is not int or value < 0 for value in self.explicit_times_us
        ):
            raise ValueError("explicit checkpoint times must be nonnegative integer microseconds")
        if self.explicit_times_us != tuple(sorted(set(self.explicit_times_us))):
            raise ValueError("explicit checkpoint times must be unique and sorted")
        if type(self.require_quiescence) is not bool or not self.require_quiescence:
            raise ValueError("full-day checkpoints must require quiescence")
        if type(self.after_t0_microsteps) is not bool or not self.after_t0_microsteps:
            raise ValueError("the t=0 checkpoint must follow completed t=0 microsteps")
        if type(self.at_phase_boundaries) is not bool:
            raise TypeError("phase-boundary checkpoint policy must be boolean")
        if (
            type(self.include_event_prefix_digest) is not bool
            or not self.include_event_prefix_digest
        ):
            raise ValueError("full-day checkpoints must bind the event-prefix digest")
        if type(self.maximum_checkpoint_count) is not int or self.maximum_checkpoint_count <= 0:
            raise ValueError("maximum checkpoint count must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "after_t0_microsteps": self.after_t0_microsteps,
            "at_phase_boundaries": self.at_phase_boundaries,
            "explicit_times_us": list(self.explicit_times_us),
            "include_event_prefix_digest": self.include_event_prefix_digest,
            "interval_us": self.interval_us,
            "maximum_checkpoint_count": self.maximum_checkpoint_count,
            "require_quiescence": self.require_quiescence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CheckpointPolicyV1:
        _require_exact_fields(
            payload,
            {
                "after_t0_microsteps",
                "at_phase_boundaries",
                "explicit_times_us",
                "include_event_prefix_digest",
                "interval_us",
                "maximum_checkpoint_count",
                "require_quiescence",
                "schema_version",
            },
            "checkpoint policy",
        )
        times = _wire_array(payload, "explicit_times_us")
        if any(type(item) is not int for item in times):
            raise TypeError("serialized explicit checkpoint times must be integers")
        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            interval_us=_wire_optional_int(payload, "interval_us"),
            explicit_times_us=tuple(times),
            require_quiescence=_wire_bool(payload, "require_quiescence"),
            after_t0_microsteps=_wire_bool(payload, "after_t0_microsteps"),
            at_phase_boundaries=_wire_bool(payload, "at_phase_boundaries"),
            include_event_prefix_digest=_wire_bool(
                payload, "include_event_prefix_digest"
            ),
            maximum_checkpoint_count=_wire_int(payload, "maximum_checkpoint_count"),
        )


@dataclass(frozen=True, slots=True)
class DeterministicLimitsV1:
    schema_version: int
    maximum_duration_us: int
    maximum_outer_events: int
    maximum_pending_work_items: int
    maximum_microsteps_per_timestamp: int
    maximum_events_per_timestamp: int
    maximum_checkpoint_bytes: int
    maximum_synchronous_consequences_per_work_item: int
    maximum_zero_delay_children_per_work_item: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != DETERMINISTIC_LIMITS_SCHEMA_VERSION:
            raise ValueError("deterministic-limits schema version must be 1")
        values = (
            self.maximum_duration_us,
            self.maximum_outer_events,
            self.maximum_pending_work_items,
            self.maximum_microsteps_per_timestamp,
            self.maximum_events_per_timestamp,
            self.maximum_checkpoint_bytes,
            self.maximum_synchronous_consequences_per_work_item,
            self.maximum_zero_delay_children_per_work_item,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("deterministic limits must be positive integers")

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_checkpoint_bytes": self.maximum_checkpoint_bytes,
            "maximum_duration_us": self.maximum_duration_us,
            "maximum_events_per_timestamp": self.maximum_events_per_timestamp,
            "maximum_microsteps_per_timestamp": self.maximum_microsteps_per_timestamp,
            "maximum_outer_events": self.maximum_outer_events,
            "maximum_pending_work_items": self.maximum_pending_work_items,
            "maximum_synchronous_consequences_per_work_item": self.maximum_synchronous_consequences_per_work_item,
            "maximum_zero_delay_children_per_work_item": self.maximum_zero_delay_children_per_work_item,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DeterministicLimitsV1:
        fields = {
            "maximum_checkpoint_bytes",
            "maximum_duration_us",
            "maximum_events_per_timestamp",
            "maximum_microsteps_per_timestamp",
            "maximum_outer_events",
            "maximum_pending_work_items",
            "maximum_synchronous_consequences_per_work_item",
            "maximum_zero_delay_children_per_work_item",
            "schema_version",
        }
        _require_exact_fields(payload, fields, "deterministic limits")
        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            maximum_duration_us=_wire_int(payload, "maximum_duration_us"),
            maximum_outer_events=_wire_int(payload, "maximum_outer_events"),
            maximum_pending_work_items=_wire_int(payload, "maximum_pending_work_items"),
            maximum_microsteps_per_timestamp=_wire_int(
                payload, "maximum_microsteps_per_timestamp"
            ),
            maximum_events_per_timestamp=_wire_int(
                payload, "maximum_events_per_timestamp"
            ),
            maximum_checkpoint_bytes=_wire_int(payload, "maximum_checkpoint_bytes"),
            maximum_synchronous_consequences_per_work_item=_wire_int(
                payload, "maximum_synchronous_consequences_per_work_item"
            ),
            maximum_zero_delay_children_per_work_item=_wire_int(
                payload, "maximum_zero_delay_children_per_work_item"
            ),
        )


@dataclass(frozen=True, slots=True)
class FullDayPlanV1:
    """The sole V1 runtime IR; every field is identity-bearing."""

    schema_version: int
    plan_id: str
    plan_version: int
    market_profile: VersionedReferenceV1
    instrument_profile: ResolvedInstrumentProfileV1
    calendar: TradingDayCalendarV1
    pressure_profiles: tuple[PressureProfileV1, ...]
    state_model: StateModelV1
    macro_regime_schedule: tuple[MacroRegimeSegmentV1, ...]
    participant_definitions: tuple[ParticipantDefinitionV1, ...]
    participant_schedule: tuple[ParticipantScheduleEntryV1, ...]
    scheduled_events: tuple[ScheduledEventV1, ...]
    unscheduled_shock_policy: UnscheduledShockPolicyV1
    halt_reopen_rules: HaltReopenRulesV1
    seed_policy: SeedPolicyV1
    checkpoint_policy: CheckpointPolicyV1
    deterministic_limits: DeterministicLimitsV1
    pilot_limits_reference: VersionedReferenceV1
    composition_profile: VersionedReferenceV1
    component_configurations: tuple[ComponentConfigurationBindingV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != FULL_DAY_PLAN_SCHEMA_VERSION:
            raise ValueError("full-day plan schema version must be 1")
        _validate_identifier(self.plan_id, "full-day plan ID")
        if type(self.plan_version) is not int or self.plan_version <= 0:
            raise ValueError("full-day plan version must be a positive integer")
        if type(self.market_profile) is not VersionedReferenceV1:
            raise TypeError("market profile must use VersionedReferenceV1")
        if type(self.instrument_profile) is not ResolvedInstrumentProfileV1:
            raise TypeError("instrument profile must use ResolvedInstrumentProfileV1")
        from .calendar import TradingDayCalendarV1

        if type(self.calendar) is not TradingDayCalendarV1:
            raise TypeError("calendar must use TradingDayCalendarV1")
        if type(self.state_model) is not StateModelV1:
            raise TypeError("state model must use StateModelV1")
        for value, expected, context in (
            (self.seed_policy, SeedPolicyV1, "seed policy"),
            (self.checkpoint_policy, CheckpointPolicyV1, "checkpoint policy"),
            (self.deterministic_limits, DeterministicLimitsV1, "deterministic limits"),
            (self.pilot_limits_reference, VersionedReferenceV1, "pilot-limits reference"),
            (self.composition_profile, VersionedReferenceV1, "composition profile"),
            (self.unscheduled_shock_policy, UnscheduledShockPolicyV1, "shock policy"),
            (self.halt_reopen_rules, HaltReopenRulesV1, "halt/reopen rules"),
        ):
            if type(value) is not expected:
                raise TypeError(f"{context} uses the wrong V1 contract")
        end_us = self.calendar.end_time_us
        if end_us > self.deterministic_limits.maximum_duration_us:
            raise ValueError("calendar exceeds the identity-bearing duration limit")
        if type(self.pressure_profiles) is not tuple or any(
            type(item) is not PressureProfileV1 for item in self.pressure_profiles
        ):
            raise TypeError("pressure profiles must use PressureProfileV1")
        if tuple(item.pressure_kind for item in self.pressure_profiles) != tuple(
            PressureKindV1
        ):
            raise ValueError("plan requires exactly VOLUME, LIQUIDITY, VOLATILITY pressure profiles")
        pressure_ids = tuple(item.profile_id for item in self.pressure_profiles)
        if len(pressure_ids) != len(set(pressure_ids)):
            raise ValueError("pressure profile IDs must be unique")
        if any(item.segments[-1].end_us != end_us for item in self.pressure_profiles):
            raise ValueError("each pressure profile must cover the complete calendar")
        if type(self.macro_regime_schedule) is not tuple or not self.macro_regime_schedule or any(
            type(item) is not MacroRegimeSegmentV1 for item in self.macro_regime_schedule
        ):
            raise ValueError("macro-regime schedule requires typed segments")
        if self.macro_regime_schedule[0].start_us != 0 or self.macro_regime_schedule[-1].end_us != end_us:
            raise ValueError("macro-regime schedule must cover the complete calendar")
        for previous, current in zip(
            self.macro_regime_schedule, self.macro_regime_schedule[1:]
        ):
            if previous.end_us != current.start_us:
                raise ValueError("macro-regime schedule must be contiguous and ordered")
        if self.macro_regime_schedule[0].day_state is not self.state_model.initial_day_state:
            raise ValueError("macro schedule must begin in the declared initial day state")
        if type(self.participant_definitions) is not tuple or any(
            type(item) is not ParticipantDefinitionV1 for item in self.participant_definitions
        ):
            raise TypeError("participant definitions use the wrong contract")
        participant_ids = tuple(item.participant_id for item in self.participant_definitions)
        if participant_ids != tuple(sorted(set(participant_ids))):
            raise ValueError("participant definitions must be unique and sorted")
        if type(self.participant_schedule) is not tuple or any(
            type(item) is not ParticipantScheduleEntryV1 for item in self.participant_schedule
        ):
            raise TypeError("participant schedule uses the wrong contract")
        schedule_keys = tuple(
            (item.simulation_time_us, item.participant_id, item.schedule_id)
            for item in self.participant_schedule
        )
        if schedule_keys != tuple(sorted(schedule_keys)):
            raise ValueError("participant schedule must be canonically ordered")
        schedule_ids = tuple(item.schedule_id for item in self.participant_schedule)
        if len(schedule_ids) != len(set(schedule_ids)):
            raise ValueError("participant schedule IDs must be unique")
        if any(item.participant_id not in set(participant_ids) for item in self.participant_schedule):
            raise ValueError("participant schedule references an unknown participant")
        if any(item.simulation_time_us >= end_us for item in self.participant_schedule):
            raise ValueError("participant schedule time lies outside the calendar")
        if type(self.scheduled_events) is not tuple or any(
            type(item) is not ScheduledEventV1 for item in self.scheduled_events
        ):
            raise TypeError("scheduled events use the wrong contract")
        event_keys = tuple(
            (item.simulation_time_us, item.event_id) for item in self.scheduled_events
        )
        if event_keys != tuple(sorted(event_keys)) or len({item.event_id for item in self.scheduled_events}) != len(self.scheduled_events):
            raise ValueError("scheduled events must have unique IDs in canonical time order")
        if any(item.simulation_time_us >= end_us for item in self.scheduled_events):
            raise ValueError("scheduled event time lies outside the calendar")
        for scheduled_event in self.scheduled_events:
            matching_phases = tuple(
                phase
                for phase in self.calendar.phases
                if phase.start.simulation_time_us
                <= scheduled_event.simulation_time_us
                < phase.end.simulation_time_us
            )
            if len(matching_phases) != 1:
                raise ValueError(
                    f"scheduled event {scheduled_event.event_id} does not resolve "
                    "to exactly one half-open calendar phase"
                )
            phase = matching_phases[0]
            phase_id = phase.phase_id
            allowed_phases = SCHEDULED_EVENT_SEMANTICS_V1[
                scheduled_event.event_type
            ].allowed_phase_ids
            if phase_id not in allowed_phases:
                raise ValueError(
                    f"scheduled event {scheduled_event.event_id} of type "
                    f"{scheduled_event.event_type.value} is forbidden in phase "
                    f"{phase_id}"
                )
            if scheduled_event.event_type in {
                ScheduledEventTypeV1.NEWS_SHOCK,
                ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER,
            }:
                duration_us = next(
                    parameter.value
                    for parameter in scheduled_event.parameters
                    if parameter.name == "duration_us"
                )
                if (
                    scheduled_event.simulation_time_us + duration_us
                    > phase.end.simulation_time_us
                ):
                    raise ValueError(
                        f"scheduled event {scheduled_event.event_id} duration spills "
                        f"past the half-open {phase_id} phase"
                    )
            if (
                scheduled_event.event_type
                is ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER
            ):
                matching_participants = tuple(
                    participant
                    for participant in self.participant_definitions
                    if participant.specification
                    == scheduled_event.population_reference
                )
                if len(matching_participants) != 1:
                    raise ValueError(
                        f"scheduled metaorder {scheduled_event.event_id} population "
                        "reference must resolve to exactly one participant"
                    )
                if (
                    matching_participants[0].participant_kind
                    is not ParticipantKindV1.METAORDER
                ):
                    raise ValueError(
                        f"scheduled metaorder {scheduled_event.event_id} population "
                        "reference resolves to a non-METAORDER participant"
                    )
        halt_start_types = {
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
            ScheduledEventTypeV1.HALT,
        }
        halt_starts = tuple(
            item
            for item in self.scheduled_events
            if item.event_type in halt_start_types
        )
        reopenings = tuple(
            item
            for item in self.scheduled_events
            if item.event_type is ScheduledEventTypeV1.REOPENING
        )
        halt_rules = self.halt_reopen_rules
        if len(halt_starts) > halt_rules.maximum_halts:
            raise ValueError("scheduled halt starts exceed the maximum halt count")
        reopenings_by_time: dict[int, list[ScheduledEventV1]] = {}
        for reopening in reopenings:
            reopenings_by_time.setdefault(reopening.simulation_time_us, []).append(
                reopening
            )
        continuous_phase = next(
            phase for phase in self.calendar.phases if phase.phase_id == "CONTINUOUS"
        )
        previous_reopening_end_time: int | None = None
        matched_reopening_ids: set[str] = set()
        for halt_start in halt_starts:
            halt_parameters = {
                item.name: item.value for item in halt_start.parameters
            }
            halt_duration_us = halt_parameters["halt_duration_us"]
            if not (
                halt_rules.minimum_halt_duration_us
                <= halt_duration_us
                <= halt_rules.maximum_halt_duration_us
            ):
                raise ValueError(
                    f"scheduled halt {halt_start.event_id} duration is outside "
                    "the halt/reopen rule bounds"
                )
            if halt_start.mechanics_reference != halt_rules.halt_trigger_reference:
                raise ValueError(
                    f"scheduled halt {halt_start.event_id} must use the declared "
                    "halt-trigger reference"
                )
            if (
                previous_reopening_end_time is not None
                and halt_start.simulation_time_us < previous_reopening_end_time
            ):
                raise ValueError("scheduled halts must not nest or overlap")
            reopening_time = halt_start.simulation_time_us + halt_duration_us
            matches = reopenings_by_time.get(reopening_time, [])
            if len(matches) != 1:
                raise ValueError(
                    f"scheduled halt {halt_start.event_id} requires exactly one "
                    f"reopening at {reopening_time}"
                )
            reopening = matches[0]
            if reopening.event_id in matched_reopening_ids:
                raise ValueError("one scheduled reopening cannot close multiple halts")
            if reopening.mechanics_reference != halt_rules.resume_trigger_reference:
                raise ValueError(
                    f"scheduled reopening {reopening.event_id} must use the declared "
                    "resume-trigger reference"
                )
            reopening_parameters = {
                item.name: item.value for item in reopening.parameters
            }
            reopening_duration_us = reopening_parameters[
                "reopening_auction_duration_us"
            ]
            if reopening_duration_us != halt_rules.reopening_auction_duration_us:
                raise ValueError(
                    f"scheduled reopening {reopening.event_id} duration does not "
                    "match the halt/reopen rules"
                )
            if (
                reopening_time + reopening_duration_us
                > continuous_phase.end.simulation_time_us
            ):
                raise ValueError(
                    f"scheduled reopening {reopening.event_id} does not remain "
                    "within the continuous phase"
                )
            matched_reopening_ids.add(reopening.event_id)
            previous_reopening_end_time = reopening_time + reopening_duration_us
        orphan_reopenings = {
            item.event_id for item in reopenings
        }.difference(matched_reopening_ids)
        if orphan_reopenings:
            raise ValueError(
                "orphan scheduled reopenings are forbidden: "
                + ",".join(sorted(orphan_reopenings))
            )
        shock = self.unscheduled_shock_policy
        if shock.candidate_window_end_us > end_us:
            raise ValueError("unscheduled-shock window exceeds the calendar")
        declared_labels = {item.semantic_path for item in self.seed_policy.substreams}
        owned_labels = (
            *(item.rng_substream_label for item in self.participant_definitions),
            shock.substream_label,
            self.state_model.day_rng_substream_label,
            self.state_model.local_rng_substream_label,
        )
        if len(owned_labels) != len(set(owned_labels)):
            raise ValueError("full-day RNG substream ownership labels must be unique")
        if not set(owned_labels).issubset(declared_labels):
            raise ValueError("plan uses an undeclared RNG substream label")
        if any(
            not item.rng_substream_label.startswith("full_day/participant/")
            for item in self.participant_definitions
        ):
            raise ValueError(
                "participant RNG labels must lie under the AgentScheduler prefix"
            )
        if not shock.substream_label.startswith("full_day/runtime/shock/"):
            raise ValueError("shock RNG label must lie under the runtime shock prefix")
        if (
            self.state_model.day_rng_substream_label
            != DAY_STATE_RNG_SUBSTREAM_PATH_V1
            or self.state_model.local_rng_substream_label
            != LOCAL_STATE_RNG_SUBSTREAM_PATH_V1
        ):
            raise ValueError("state RNG labels do not use the frozen runtime paths")
        if type(self.component_configurations) is not tuple or any(
            type(item) is not ComponentConfigurationBindingV1
            for item in self.component_configurations
        ):
            raise TypeError(
                "component configurations must use ComponentConfigurationBindingV1"
            )
        binding_keys = tuple(item.sort_key for item in self.component_configurations)
        if binding_keys != tuple(sorted(set(binding_keys))):
            raise ValueError(
                "component configuration bindings must be unique and sorted"
            )
        component_reference_versions = tuple(
            (
                item.component_id,
                item.configuration.reference_id,
                item.configuration.version,
            )
            for item in self.component_configurations
        )
        if len(component_reference_versions) != len(
            set(component_reference_versions)
        ):
            raise ValueError(
                "one component/reference ID/version mapping must resolve to one digest"
            )
        reference_versions = tuple(
            (item.configuration.reference_id, item.configuration.version)
            for item in self.component_configurations
        )
        if len(reference_versions) != len(set(reference_versions)):
            raise ValueError(
                "one configuration reference ID/version may bind only one component"
            )
        required_configuration_owners: dict[
            VersionedReferenceV1, set[str]
        ] = {}

        def require_configuration_owner(
            reference: VersionedReferenceV1, component_id: str
        ) -> None:
            required_configuration_owners.setdefault(reference, set()).add(component_id)

        for participant in self.participant_definitions:
            require_configuration_owner(
                participant.specification, "AGENT_SCHEDULER_V1"
            )
        for schedule_entry in self.participant_schedule:
            if schedule_entry.replacement_specification is not None:
                require_configuration_owner(
                    schedule_entry.replacement_specification, "AGENT_SCHEDULER_V1"
                )
        for scheduled_event in self.scheduled_events:
            if scheduled_event.population_reference is not None:
                require_configuration_owner(
                    scheduled_event.population_reference, "AGENT_SCHEDULER_V1"
                )
            if scheduled_event.mechanics_reference is not None:
                require_configuration_owner(
                    scheduled_event.mechanics_reference,
                    "ENGINE_MARKET_MECHANICS_V1",
                )
        require_configuration_owner(
            shock.quantity_distribution_reference, "FULL_DAY_RUNTIME_V1"
        )
        require_configuration_owner(
            self.halt_reopen_rules.halt_trigger_reference,
            "ENGINE_MARKET_MECHANICS_V1",
        )
        require_configuration_owner(
            self.halt_reopen_rules.resume_trigger_reference,
            "ENGINE_MARKET_MECHANICS_V1",
        )
        conflicting_owners = {
            reference.reference_id: tuple(sorted(owners))
            for reference, owners in required_configuration_owners.items()
            if len(owners) != 1
        }
        if conflicting_owners:
            raise ValueError(
                "one configuration reference cannot serve roles owned by different "
                f"components: {conflicting_owners}"
            )
        bound_configuration_owners = {
            item.configuration: item.component_id
            for item in self.component_configurations
        }
        for reference, owners in required_configuration_owners.items():
            expected_owner = next(iter(owners))
            actual_owner = bound_configuration_owners.get(reference)
            if actual_owner is None:
                raise ValueError(
                    "a referenced component configuration is absent from the digest "
                    "registry"
                )
            if actual_owner != expected_owner:
                raise ValueError(
                    f"configuration {reference.reference_id} is bound to "
                    f"{actual_owner}, not its role owner {expected_owner}"
                )
        selected_components = {
            binding.component_id for binding in self.component_configurations
        }
        for declaration in self.seed_policy.substreams:
            matching_owners = {
                component_id
                for component_id in selected_components
                for prefix in RNG_LABEL_PREFIXES_BY_COMPONENT_V1.get(
                    component_id, ()
                )
                if declaration.semantic_path == prefix
                or declaration.semantic_path.startswith(prefix + "/")
            }
            if len(matching_owners) != 1:
                raise ValueError(
                    f"RNG label {declaration.semantic_path} must have exactly one "
                    "selected component owner"
                )
        if any(time_us > end_us for time_us in self.checkpoint_policy.explicit_times_us):
            raise ValueError("checkpoint time exceeds the calendar")
        checkpoint_times = set(self.checkpoint_policy.explicit_times_us)
        if self.checkpoint_policy.at_phase_boundaries:
            checkpoint_times.update(
                operation.boundary.simulation_time_us
                for operation in self.calendar.boundary_operations
            )
        interval = self.checkpoint_policy.interval_us
        interval_count = 0 if interval is None else end_us // interval
        interval_overlaps = (
            0
            if interval is None
            else sum(
                time_us > 0 and time_us % interval == 0
                for time_us in checkpoint_times
            )
        )
        resolved_checkpoint_count = (
            len(checkpoint_times) + interval_count - interval_overlaps
        )
        if (
            resolved_checkpoint_count
            > self.checkpoint_policy.maximum_checkpoint_count
        ):
            raise ValueError(
                "resolved checkpoint schedule exceeds the declared checkpoint count "
                "bound"
            )

    @property
    def semantic_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def sha256(self) -> str:
        return self.semantic_sha256

    @property
    def resolved_checkpoint_times_us(self) -> tuple[int, ...]:
        """Return the exact de-duplicated checkpoint schedule for this calendar.

        Interval checkpoints originate at ``t=0 + interval`` and include the
        calendar end when it is an exact multiple. Phase-boundary checkpoints include
        the preopen ``t=0`` boundary and the final closed boundary.
        """

        times = set(self.checkpoint_policy.explicit_times_us)
        if self.checkpoint_policy.at_phase_boundaries:
            times.update(
                operation.boundary.simulation_time_us
                for operation in self.calendar.boundary_operations
            )
        interval = self.checkpoint_policy.interval_us
        if interval is not None:
            times.update(range(interval, self.calendar.end_time_us + 1, interval))
        return tuple(sorted(times))

    @property
    def macro_regime_schedule_semantics(self) -> str:
        """Return the schema-v1 precedence rule for macro anchors and state dynamics.

        Every segment start is an exogenous stage-2 hard reset to its declared day
        state, including an age/deadline resample. Between segment starts, the
        duration-aware state model may transition normally; a later segment start
        takes precedence and resets it again. Any pre-reset day transition due at
        that exact timestamp is replaced by the anchor rather than emitted first.
        The local state and its age are not reset by a macro anchor.
        """

        return MACRO_REGIME_SCHEDULE_SEMANTICS_V1

    @property
    def selected_component_ids(self) -> tuple[str, ...]:
        """Return composition component IDs in their canonical selection order."""

        return tuple(
            sorted({item.component_id for item in self.component_configurations})
        )

    @property
    def component_configurations_by_id(
        self,
    ) -> Mapping[str, tuple[VersionedReferenceV1, ...]]:
        """Expose an immutable, deterministically ordered composition lookup."""

        grouped: dict[str, list[VersionedReferenceV1]] = {
            component_id: [] for component_id in self.selected_component_ids
        }
        for binding in self.component_configurations:
            grouped[binding.component_id].append(binding.configuration)
        return MappingProxyType(
            {
                component_id: tuple(grouped[component_id])
                for component_id in self.selected_component_ids
            }
        )

    def configurations_for_component(
        self, component_id: str
    ) -> tuple[VersionedReferenceV1, ...]:
        """Resolve one component's ordered configurations without coercion."""

        selected = _validate_identifier(component_id, "component ID lookup")
        try:
            return self.component_configurations_by_id[selected]
        except KeyError as error:
            raise KeyError(f"unselected full-day component: {selected}") from error

    def as_dict(self) -> dict[str, object]:
        return {
            "calendar": self.calendar.as_dict(),
            "checkpoint_policy": self.checkpoint_policy.as_dict(),
            "component_configurations": [item.as_dict() for item in self.component_configurations],
            "composition_profile": self.composition_profile.as_dict(),
            "deterministic_limits": self.deterministic_limits.as_dict(),
            "halt_reopen_rules": self.halt_reopen_rules.as_dict(),
            "instrument_profile": self.instrument_profile.as_dict(),
            "macro_regime_schedule": [item.as_dict() for item in self.macro_regime_schedule],
            "market_profile": self.market_profile.as_dict(),
            "participant_definitions": [item.as_dict() for item in self.participant_definitions],
            "participant_schedule": [item.as_dict() for item in self.participant_schedule],
            "pilot_limits_reference": self.pilot_limits_reference.as_dict(),
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "pressure_profiles": [item.as_dict() for item in self.pressure_profiles],
            "scheduled_events": [item.as_dict() for item in self.scheduled_events],
            "schema_version": self.schema_version,
            "seed_policy": self.seed_policy.as_dict(),
            "state_model": self.state_model.as_dict(),
            "unscheduled_shock_policy": self.unscheduled_shock_policy.as_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FullDayPlanV1:
        fields = {
            "calendar",
            "checkpoint_policy",
            "component_configurations",
            "composition_profile",
            "deterministic_limits",
            "halt_reopen_rules",
            "instrument_profile",
            "macro_regime_schedule",
            "market_profile",
            "participant_definitions",
            "participant_schedule",
            "pilot_limits_reference",
            "plan_id",
            "plan_version",
            "pressure_profiles",
            "scheduled_events",
            "schema_version",
            "seed_policy",
            "state_model",
            "unscheduled_shock_policy",
        }
        _require_exact_fields(payload, fields, "full-day plan")
        from .calendar import TradingDayCalendarV1

        return cls(
            schema_version=_wire_int(payload, "schema_version"),
            plan_id=_wire_str(payload, "plan_id"),
            plan_version=_wire_int(payload, "plan_version"),
            market_profile=VersionedReferenceV1.from_dict(
                _wire_object(payload, "market_profile")
            ),
            instrument_profile=ResolvedInstrumentProfileV1.from_dict(
                _wire_object(payload, "instrument_profile")
            ),
            calendar=TradingDayCalendarV1.from_dict(_wire_object(payload, "calendar")),
            pressure_profiles=tuple(
                PressureProfileV1.from_dict(_array_object(item, "pressure profile"))
                for item in _wire_array(payload, "pressure_profiles")
            ),
            state_model=StateModelV1.from_dict(_wire_object(payload, "state_model")),
            macro_regime_schedule=tuple(
                MacroRegimeSegmentV1.from_dict(
                    _array_object(item, "macro-regime segment")
                )
                for item in _wire_array(payload, "macro_regime_schedule")
            ),
            participant_definitions=tuple(
                ParticipantDefinitionV1.from_dict(
                    _array_object(item, "participant definition")
                )
                for item in _wire_array(payload, "participant_definitions")
            ),
            participant_schedule=tuple(
                ParticipantScheduleEntryV1.from_dict(
                    _array_object(item, "participant schedule entry")
                )
                for item in _wire_array(payload, "participant_schedule")
            ),
            scheduled_events=tuple(
                ScheduledEventV1.from_dict(_array_object(item, "scheduled event"))
                for item in _wire_array(payload, "scheduled_events")
            ),
            unscheduled_shock_policy=UnscheduledShockPolicyV1.from_dict(
                _wire_object(payload, "unscheduled_shock_policy")
            ),
            halt_reopen_rules=HaltReopenRulesV1.from_dict(
                _wire_object(payload, "halt_reopen_rules")
            ),
            seed_policy=SeedPolicyV1.from_dict(_wire_object(payload, "seed_policy")),
            checkpoint_policy=CheckpointPolicyV1.from_dict(
                _wire_object(payload, "checkpoint_policy")
            ),
            deterministic_limits=DeterministicLimitsV1.from_dict(
                _wire_object(payload, "deterministic_limits")
            ),
            pilot_limits_reference=VersionedReferenceV1.from_dict(
                _wire_object(payload, "pilot_limits_reference")
            ),
            composition_profile=VersionedReferenceV1.from_dict(
                _wire_object(payload, "composition_profile")
            ),
            component_configurations=tuple(
                ComponentConfigurationBindingV1.from_dict(
                    _array_object(item, "component configuration binding")
                )
                for item in _wire_array(payload, "component_configurations")
            ),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> FullDayPlanV1:
        return cls.from_dict(parse_canonical_json_object(raw))


FullDayPlan = FullDayPlanV1


__all__ = [
    "AccountStpModeV1",
    "CHECKPOINT_POLICY_SCHEMA_VERSION",
    "CheckpointPolicyV1",
    "ComponentConfigurationBindingV1",
    "DETERMINISTIC_LIMITS_SCHEMA_VERSION",
    "DeterministicLimitsV1",
    "FULL_DAY_PLAN_SCHEMA_VERSION",
    "FULL_DAY_SUBSTREAM_POLICY_VERSION",
    "FlowSideV1",
    "FullDayPlan",
    "FullDayPlanV1",
    "HaltReopenRulesV1",
    "IntegerParameterUnitV1",
    "MacroRegimeSegmentV1",
    "MECHANICS_RULES_SCHEMA_VERSION",
    "MechanicsRulesV1",
    "NamedIntegerParameterV1",
    "ParticipantDefinitionV1",
    "ParticipantKindV1",
    "ParticipantScheduleActionV1",
    "ParticipantScheduleEntryV1",
    "PressureKindV1",
    "PressureProfileV1",
    "PressureSegmentV1",
    "ResolvedInstrumentProfileV1",
    "SEED_POLICY_SCHEMA_VERSION",
    "SCHEDULED_EVENT_SEMANTICS_V1",
    "SCHEDULED_EVENT_MAX_DURATION_US_V1",
    "SCHEDULED_EVENT_MAX_SHARES_V1",
    "ScheduledEventParameterRuleV1",
    "ScheduledEventSemanticSpecV1",
    "ScheduledEventTypeV1",
    "ScheduledEventV1",
    "SeedPolicyV1",
    "SubstreamDeclarationV1",
    "UnscheduledShockPolicyV1",
    "VersionedReferenceV1",
    "_require_exact_fields",
    "canonical_json_bytes",
    "canonical_sha256",
    "derive_substream_seed",
    "parse_canonical_json_object",
    "validate_strict_json",
]
