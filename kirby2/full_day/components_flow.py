"""Typed background-flow proposal owners for the authoritative full-day runtime.

The flow owner may observe immutable public book cuts and propose actions.  It
does not own a clock, exchange, book, order allocator, or gateway; the full-day
runtime interprets every proposal and applies it through MarketMechanicsEngine.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.simulation.flow import FlowEventFamily
from kirby2.simulation.flow_models import (
    FLOW_CHANNELS,
    HawkesConfig,
    HawkesFlowModel,
    SimpleFlowModel,
    load_accepted_hawkes_configs,
)
from kirby2.simulation.rng import SeededRng
from kirby2.simulation.queue_reactive import (
    IntensityInspection,
    QueueReactiveConfig,
    QueueReactiveFlowModifier,
    QueueReactiveState,
    default_queue_reactive_config,
)

from .components import ComponentSnapshotV1, FullDayComponentAdapterV1
from .composition import (
    FLOW_HAWKES_COMPONENT,
    FLOW_QUEUE_REACTIVE_COMPONENT,
    FLOW_SIMPLE_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
    MECHANICS_COMPONENT,
    component_configured_predicate,
)
from .models import (
    FullDayPlanV1,
    VersionedReferenceV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)


SIMPLE_FLOW_SCHEMA_VERSION = 1
SIMPLE_FLOW_MODEL_ID = "SIMPLE_POISSON_FLOW_V1"
SIMPLE_FLOW_MODEL_VERSION = 1
SIMPLE_FLOW_RNG_LABEL = "full_day/flow/simple/proposal"
HAWKES_FLOW_SCHEMA_VERSION = 1
HAWKES_FLOW_MODEL_ID = "EXPONENTIAL_HAWKES_FLOW_V1"
HAWKES_FLOW_MODEL_VERSION = 1
HAWKES_FLOW_RNG_LABEL = "full_day/flow/hawkes/proposal"
QUEUE_REACTIVE_FLOW_SCHEMA_VERSION = 1
QUEUE_REACTIVE_FLOW_MODEL_ID = "OBSERVABLE_QUEUE_REACTIVE_FLOW_V1"
QUEUE_REACTIVE_FLOW_MODEL_VERSION = 1
QUEUE_REACTIVE_FLOW_RNG_LABEL = "full_day/flow/queue_reactive/proposal"
QUEUE_REACTIVE_MAX_RETAINED_RECORDS = 10_000


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _exact_string(value, field)


def _hawkes_profile_sha256(config: HawkesConfig) -> str:
    if type(config) is not HawkesConfig:
        raise TypeError("Hawkes profile digest requires HawkesConfig")
    payload = json.dumps(
        config.as_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _queue_reactive_profile_sha256(config: QueueReactiveConfig) -> str:
    if type(config) is not QueueReactiveConfig:
        raise TypeError("queue-reactive profile digest requires QueueReactiveConfig")
    payload = json.dumps(
        config.as_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_queue_inspection(
    inspection: IntensityInspection | None,
) -> dict[str, object] | None:
    if inspection is None:
        return None
    state = inspection.state
    encoded: dict[str, object] = {
        "queue_state": {
            "best_ask_size": state.best_ask_size,
            "best_bid_size": state.best_bid_size,
            "depth_near_touch_ask": state.depth_near_touch_ask,
            "depth_near_touch_bid": state.depth_near_touch_bid,
            "imbalance_hex": _float_hex(state.imbalance),
            "recent_aggressive_buy_volume": state.recent_aggressive_buy_volume,
            "recent_aggressive_flow_imbalance_hex": _float_hex(
                state.recent_aggressive_flow_imbalance
            ),
            "recent_aggressive_sell_volume": state.recent_aggressive_sell_volume,
            "recent_depletion_ask": state.recent_depletion_ask,
            "recent_depletion_bid": state.recent_depletion_bid,
            "recent_replenishment_ask": state.recent_replenishment_ask,
            "recent_replenishment_bid": state.recent_replenishment_bid,
            "short_term_price_movement_ticks_hex": _float_hex(
                state.short_term_price_movement_ticks
            ),
            "simulation_time_us": state.simulation_time_us,
            "spread_ticks": state.spread_ticks,
            "window_us": state.window_us,
        },
        "channels": [
            {
                "base_intensity_hex": _float_hex(channel.base_intensity),
                "family": channel.family.value,
                "final_intensity_hex": _float_hex(channel.final_intensity),
                "state_multiplier_hex": _float_hex(channel.state_multiplier),
                "terms": [
                    {
                        "input_hex": _float_hex(float(term["input"])),
                        "multiplier_hex": _float_hex(float(term["multiplier"])),
                        "variable": term["variable"],
                    }
                    for term in channel.term_results
                ],
            }
            for channel in inspection.channels
        ],
    }
    validate_strict_json(encoded)
    return encoded


def _float_hex(value: float) -> str:
    if type(value) is not float:
        raise TypeError("Hawkes binary state must contain exact floats")
    return value.hex()


def _float_from_canonical_hex(value: object, field: str) -> float:
    text = _exact_string(value, field)
    try:
        decoded = float.fromhex(text)
    except ValueError as error:
        raise ValueError(f"{field} is not an exact binary64 hexadecimal value") from error
    if decoded.hex() != text:
        raise ValueError(f"{field} is not canonical binary64 hexadecimal")
    return decoded


def _encode_hawkes_runtime_state(state: Mapping[str, object]) -> dict[str, object]:
    _require_exact_fields(
        state,
        {
            "excitation",
            "intensity_cap_hits",
            "model",
            "observed_events",
            "profile_id",
            "state_time_us",
            "thinning_rejections",
            "use_runtime_baseline",
        },
        "Hawkes runtime state",
    )
    excitation = state["excitation"]
    if type(excitation) is not list:
        raise TypeError("Hawkes excitation state must be an array")
    encoded_excitation: list[list[str]] = []
    for row in excitation:
        if type(row) is not list:
            raise TypeError("Hawkes excitation rows must be arrays")
        encoded_excitation.append([_float_hex(value) for value in row])
    encoded = {
        **{key: value for key, value in state.items() if key != "excitation"},
        "excitation": encoded_excitation,
    }
    validate_strict_json(encoded)
    return encoded


def _decode_hawkes_runtime_state(state: Mapping[str, object]) -> dict[str, object]:
    validate_strict_json(state)
    _require_exact_fields(
        state,
        {
            "excitation",
            "intensity_cap_hits",
            "model",
            "observed_events",
            "profile_id",
            "state_time_us",
            "thinning_rejections",
            "use_runtime_baseline",
        },
        "encoded Hawkes runtime state",
    )
    excitation = state["excitation"]
    if type(excitation) is not list:
        raise TypeError("encoded Hawkes excitation state must be an array")
    decoded_excitation: list[list[float]] = []
    for row_index, row in enumerate(excitation):
        if type(row) is not list:
            raise TypeError("encoded Hawkes excitation rows must be arrays")
        decoded_excitation.append(
            [
                _float_from_canonical_hex(
                    value, f"excitation[{row_index}][{column_index}]"
                )
                for column_index, value in enumerate(row)
            ]
        )
    return {
        **{key: value for key, value in state.items() if key != "excitation"},
        "excitation": decoded_excitation,
    }


@dataclass(frozen=True, slots=True)
class SimpleFlowConfigurationV1:
    """Integer-only identity for the six-channel simple arrival model.

    Intensities are micro-events per second, so 1_000_000 represents one event
    per second.  This keeps the plan/checkpoint identity free of binary floats.
    """

    schema_version: int
    configuration_id: str
    configuration_version: int
    limit_buy_microevents_per_second: int
    limit_sell_microevents_per_second: int
    market_buy_microevents_per_second: int
    market_sell_microevents_per_second: int
    cancel_bid_microevents_per_second: int
    cancel_ask_microevents_per_second: int
    minimum_quantity: int
    maximum_quantity: int
    minimum_placement_depth_ticks: int
    maximum_placement_depth_ticks: int
    account_id: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SIMPLE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("simple-flow configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        rates = self.intensity_microevents_per_second
        if not any(rates.values()):
            raise ValueError("simple-flow configuration requires a positive intensity")
        _exact_int(self.minimum_quantity, "minimum_quantity", minimum=1)
        _exact_int(
            self.maximum_quantity,
            "maximum_quantity",
            minimum=self.minimum_quantity,
        )
        _exact_int(
            self.minimum_placement_depth_ticks,
            "minimum_placement_depth_ticks",
        )
        _exact_int(
            self.maximum_placement_depth_ticks,
            "maximum_placement_depth_ticks",
            minimum=self.minimum_placement_depth_ticks,
        )
        _exact_string(self.account_id, "account_id")

    @property
    def intensity_microevents_per_second(self) -> dict[FlowEventFamily, int]:
        return {
            FlowEventFamily.LIMIT_BUY: _exact_int(
                self.limit_buy_microevents_per_second,
                "limit_buy_microevents_per_second",
            ),
            FlowEventFamily.LIMIT_SELL: _exact_int(
                self.limit_sell_microevents_per_second,
                "limit_sell_microevents_per_second",
            ),
            FlowEventFamily.MARKET_BUY: _exact_int(
                self.market_buy_microevents_per_second,
                "market_buy_microevents_per_second",
            ),
            FlowEventFamily.MARKET_SELL: _exact_int(
                self.market_sell_microevents_per_second,
                "market_sell_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_BID: _exact_int(
                self.cancel_bid_microevents_per_second,
                "cancel_bid_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_ASK: _exact_int(
                self.cancel_ask_microevents_per_second,
                "cancel_ask_microevents_per_second",
            ),
        }

    @property
    def reference(self) -> VersionedReferenceV1:
        return VersionedReferenceV1(
            self.configuration_id,
            self.configuration_version,
            self.sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "cancel_ask_microevents_per_second": self.cancel_ask_microevents_per_second,
            "cancel_bid_microevents_per_second": self.cancel_bid_microevents_per_second,
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "limit_buy_microevents_per_second": self.limit_buy_microevents_per_second,
            "limit_sell_microevents_per_second": self.limit_sell_microevents_per_second,
            "market_buy_microevents_per_second": self.market_buy_microevents_per_second,
            "market_sell_microevents_per_second": self.market_sell_microevents_per_second,
            "maximum_placement_depth_ticks": self.maximum_placement_depth_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_placement_depth_ticks": self.minimum_placement_depth_ticks,
            "minimum_quantity": self.minimum_quantity,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimpleFlowConfigurationV1:
        validate_strict_json(payload)
        expected = {
            "account_id",
            "cancel_ask_microevents_per_second",
            "cancel_bid_microevents_per_second",
            "configuration_id",
            "configuration_version",
            "limit_buy_microevents_per_second",
            "limit_sell_microevents_per_second",
            "market_buy_microevents_per_second",
            "market_sell_microevents_per_second",
            "maximum_placement_depth_ticks",
            "maximum_quantity",
            "minimum_placement_depth_ticks",
            "minimum_quantity",
            "schema_version",
        }
        _require_exact_fields(payload, expected, "SimpleFlowConfigurationV1")
        return cls(**{field: payload[field] for field in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class HawkesFlowConfigurationV1:
    """Strict full-day binding to one accepted Hawkes excitation profile.

    The accepted model digest binds the floating-point alpha/beta profile while
    integer micro-event baselines keep the semantic plan free of binary floats.
    Binary model state is checkpointed separately as canonical ``float.hex``
    strings, preserving exact fresh-process replay.
    """

    schema_version: int
    configuration_id: str
    configuration_version: int
    accepted_profile_id: str
    accepted_profile_sha256: str
    limit_buy_microevents_per_second: int
    limit_sell_microevents_per_second: int
    market_buy_microevents_per_second: int
    market_sell_microevents_per_second: int
    cancel_bid_microevents_per_second: int
    cancel_ask_microevents_per_second: int
    minimum_quantity: int
    maximum_quantity: int
    minimum_placement_depth_ticks: int
    maximum_placement_depth_ticks: int
    account_id: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != HAWKES_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("Hawkes-flow configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        _exact_string(self.accepted_profile_id, "accepted_profile_id")
        digest = _exact_string(
            self.accepted_profile_sha256, "accepted_profile_sha256"
        )
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("accepted Hawkes profile digest must be lowercase SHA-256")
        rates = self.intensity_microevents_per_second
        if not any(rates.values()):
            raise ValueError("Hawkes-flow configuration requires a positive baseline")
        _exact_int(self.minimum_quantity, "minimum_quantity", minimum=1)
        _exact_int(
            self.maximum_quantity,
            "maximum_quantity",
            minimum=self.minimum_quantity,
        )
        _exact_int(
            self.minimum_placement_depth_ticks,
            "minimum_placement_depth_ticks",
        )
        _exact_int(
            self.maximum_placement_depth_ticks,
            "maximum_placement_depth_ticks",
            minimum=self.minimum_placement_depth_ticks,
        )
        _exact_string(self.account_id, "account_id")
        self.accepted_profile

    @property
    def accepted_profile(self) -> HawkesConfig:
        profiles = load_accepted_hawkes_configs()
        try:
            profile = profiles[self.accepted_profile_id]
        except KeyError as error:
            raise ValueError("Hawkes-flow profile is absent from the accepted registry") from error
        if _hawkes_profile_sha256(profile) != self.accepted_profile_sha256:
            raise ValueError("Hawkes-flow profile digest differs from the accepted registry")
        return profile

    @property
    def intensity_microevents_per_second(self) -> dict[FlowEventFamily, int]:
        return {
            FlowEventFamily.LIMIT_BUY: _exact_int(
                self.limit_buy_microevents_per_second,
                "limit_buy_microevents_per_second",
            ),
            FlowEventFamily.LIMIT_SELL: _exact_int(
                self.limit_sell_microevents_per_second,
                "limit_sell_microevents_per_second",
            ),
            FlowEventFamily.MARKET_BUY: _exact_int(
                self.market_buy_microevents_per_second,
                "market_buy_microevents_per_second",
            ),
            FlowEventFamily.MARKET_SELL: _exact_int(
                self.market_sell_microevents_per_second,
                "market_sell_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_BID: _exact_int(
                self.cancel_bid_microevents_per_second,
                "cancel_bid_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_ASK: _exact_int(
                self.cancel_ask_microevents_per_second,
                "cancel_ask_microevents_per_second",
            ),
        }

    @property
    def reference(self) -> VersionedReferenceV1:
        return VersionedReferenceV1(
            self.configuration_id,
            self.configuration_version,
            self.sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_profile_id": self.accepted_profile_id,
            "accepted_profile_sha256": self.accepted_profile_sha256,
            "account_id": self.account_id,
            "cancel_ask_microevents_per_second": self.cancel_ask_microevents_per_second,
            "cancel_bid_microevents_per_second": self.cancel_bid_microevents_per_second,
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "limit_buy_microevents_per_second": self.limit_buy_microevents_per_second,
            "limit_sell_microevents_per_second": self.limit_sell_microevents_per_second,
            "market_buy_microevents_per_second": self.market_buy_microevents_per_second,
            "market_sell_microevents_per_second": self.market_sell_microevents_per_second,
            "maximum_placement_depth_ticks": self.maximum_placement_depth_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_placement_depth_ticks": self.minimum_placement_depth_ticks,
            "minimum_quantity": self.minimum_quantity,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_accepted_profile(
        cls,
        *,
        configuration_id: str,
        configuration_version: int,
        accepted_profile_id: str,
        limit_buy_microevents_per_second: int,
        limit_sell_microevents_per_second: int,
        market_buy_microevents_per_second: int,
        market_sell_microevents_per_second: int,
        cancel_bid_microevents_per_second: int,
        cancel_ask_microevents_per_second: int,
        minimum_quantity: int,
        maximum_quantity: int,
        minimum_placement_depth_ticks: int,
        maximum_placement_depth_ticks: int,
        account_id: str,
    ) -> HawkesFlowConfigurationV1:
        try:
            profile = load_accepted_hawkes_configs()[accepted_profile_id]
        except KeyError as error:
            raise ValueError("unknown accepted Hawkes profile") from error
        return cls(
            schema_version=HAWKES_FLOW_SCHEMA_VERSION,
            configuration_id=configuration_id,
            configuration_version=configuration_version,
            accepted_profile_id=accepted_profile_id,
            accepted_profile_sha256=_hawkes_profile_sha256(profile),
            limit_buy_microevents_per_second=limit_buy_microevents_per_second,
            limit_sell_microevents_per_second=limit_sell_microevents_per_second,
            market_buy_microevents_per_second=market_buy_microevents_per_second,
            market_sell_microevents_per_second=market_sell_microevents_per_second,
            cancel_bid_microevents_per_second=cancel_bid_microevents_per_second,
            cancel_ask_microevents_per_second=cancel_ask_microevents_per_second,
            minimum_quantity=minimum_quantity,
            maximum_quantity=maximum_quantity,
            minimum_placement_depth_ticks=minimum_placement_depth_ticks,
            maximum_placement_depth_ticks=maximum_placement_depth_ticks,
            account_id=account_id,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> HawkesFlowConfigurationV1:
        validate_strict_json(payload)
        expected = {
            "accepted_profile_id",
            "accepted_profile_sha256",
            "account_id",
            "cancel_ask_microevents_per_second",
            "cancel_bid_microevents_per_second",
            "configuration_id",
            "configuration_version",
            "limit_buy_microevents_per_second",
            "limit_sell_microevents_per_second",
            "market_buy_microevents_per_second",
            "market_sell_microevents_per_second",
            "maximum_placement_depth_ticks",
            "maximum_quantity",
            "minimum_placement_depth_ticks",
            "minimum_quantity",
            "schema_version",
        }
        _require_exact_fields(payload, expected, "HawkesFlowConfigurationV1")
        return cls(**{field: payload[field] for field in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class QueueReactiveFlowConfigurationV1:
    """Strict binding to the observable queue-feedback response profile."""

    schema_version: int
    configuration_id: str
    configuration_version: int
    modifier_profile_id: str
    modifier_profile_sha256: str
    window_us: int
    near_touch_levels: int
    limit_buy_microevents_per_second: int
    limit_sell_microevents_per_second: int
    market_buy_microevents_per_second: int
    market_sell_microevents_per_second: int
    cancel_bid_microevents_per_second: int
    cancel_ask_microevents_per_second: int
    minimum_quantity: int
    maximum_quantity: int
    minimum_placement_depth_ticks: int
    maximum_placement_depth_ticks: int
    account_id: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != QUEUE_REACTIVE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("queue-reactive configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        _exact_string(self.modifier_profile_id, "modifier_profile_id")
        digest = _exact_string(
            self.modifier_profile_sha256, "modifier_profile_sha256"
        )
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("queue-reactive profile digest must be lowercase SHA-256")
        _exact_int(self.window_us, "window_us", minimum=1)
        _exact_int(self.near_touch_levels, "near_touch_levels", minimum=1)
        if not any(self.intensity_microevents_per_second.values()):
            raise ValueError("queue-reactive configuration requires a positive baseline")
        _exact_int(self.minimum_quantity, "minimum_quantity", minimum=1)
        _exact_int(
            self.maximum_quantity,
            "maximum_quantity",
            minimum=self.minimum_quantity,
        )
        _exact_int(self.minimum_placement_depth_ticks, "minimum_placement_depth_ticks")
        _exact_int(
            self.maximum_placement_depth_ticks,
            "maximum_placement_depth_ticks",
            minimum=self.minimum_placement_depth_ticks,
        )
        _exact_string(self.account_id, "account_id")
        self.modifier_profile

    @property
    def modifier_profile(self) -> QueueReactiveConfig:
        profile = default_queue_reactive_config()
        if (
            profile.profile_id != self.modifier_profile_id
            or profile.window_us != self.window_us
            or profile.near_touch_levels != self.near_touch_levels
            or _queue_reactive_profile_sha256(profile)
            != self.modifier_profile_sha256
        ):
            raise ValueError("queue-reactive profile differs from its bound default")
        return profile

    @property
    def intensity_microevents_per_second(self) -> dict[FlowEventFamily, int]:
        fields = (
            (FlowEventFamily.LIMIT_BUY, self.limit_buy_microevents_per_second),
            (FlowEventFamily.LIMIT_SELL, self.limit_sell_microevents_per_second),
            (FlowEventFamily.MARKET_BUY, self.market_buy_microevents_per_second),
            (FlowEventFamily.MARKET_SELL, self.market_sell_microevents_per_second),
            (FlowEventFamily.CANCEL_BID, self.cancel_bid_microevents_per_second),
            (FlowEventFamily.CANCEL_ASK, self.cancel_ask_microevents_per_second),
        )
        return {
            family: _exact_int(value, f"{family.value}_microevents_per_second")
            for family, value in fields
        }

    @property
    def reference(self) -> VersionedReferenceV1:
        return VersionedReferenceV1(
            self.configuration_id,
            self.configuration_version,
            self.sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "cancel_ask_microevents_per_second": self.cancel_ask_microevents_per_second,
            "cancel_bid_microevents_per_second": self.cancel_bid_microevents_per_second,
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "limit_buy_microevents_per_second": self.limit_buy_microevents_per_second,
            "limit_sell_microevents_per_second": self.limit_sell_microevents_per_second,
            "market_buy_microevents_per_second": self.market_buy_microevents_per_second,
            "market_sell_microevents_per_second": self.market_sell_microevents_per_second,
            "maximum_placement_depth_ticks": self.maximum_placement_depth_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_placement_depth_ticks": self.minimum_placement_depth_ticks,
            "minimum_quantity": self.minimum_quantity,
            "modifier_profile_id": self.modifier_profile_id,
            "modifier_profile_sha256": self.modifier_profile_sha256,
            "near_touch_levels": self.near_touch_levels,
            "schema_version": self.schema_version,
            "window_us": self.window_us,
        }

    @classmethod
    def from_default_profile(
        cls,
        *,
        configuration_id: str,
        configuration_version: int,
        limit_buy_microevents_per_second: int,
        limit_sell_microevents_per_second: int,
        market_buy_microevents_per_second: int,
        market_sell_microevents_per_second: int,
        cancel_bid_microevents_per_second: int,
        cancel_ask_microevents_per_second: int,
        minimum_quantity: int,
        maximum_quantity: int,
        minimum_placement_depth_ticks: int,
        maximum_placement_depth_ticks: int,
        account_id: str,
    ) -> QueueReactiveFlowConfigurationV1:
        profile = default_queue_reactive_config()
        return cls(
            schema_version=QUEUE_REACTIVE_FLOW_SCHEMA_VERSION,
            configuration_id=configuration_id,
            configuration_version=configuration_version,
            modifier_profile_id=profile.profile_id,
            modifier_profile_sha256=_queue_reactive_profile_sha256(profile),
            window_us=profile.window_us,
            near_touch_levels=profile.near_touch_levels,
            limit_buy_microevents_per_second=limit_buy_microevents_per_second,
            limit_sell_microevents_per_second=limit_sell_microevents_per_second,
            market_buy_microevents_per_second=market_buy_microevents_per_second,
            market_sell_microevents_per_second=market_sell_microevents_per_second,
            cancel_bid_microevents_per_second=cancel_bid_microevents_per_second,
            cancel_ask_microevents_per_second=cancel_ask_microevents_per_second,
            minimum_quantity=minimum_quantity,
            maximum_quantity=maximum_quantity,
            minimum_placement_depth_ticks=minimum_placement_depth_ticks,
            maximum_placement_depth_ticks=maximum_placement_depth_ticks,
            account_id=account_id,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> QueueReactiveFlowConfigurationV1:
        validate_strict_json(payload)
        expected = {
            "account_id",
            "cancel_ask_microevents_per_second",
            "cancel_bid_microevents_per_second",
            "configuration_id",
            "configuration_version",
            "limit_buy_microevents_per_second",
            "limit_sell_microevents_per_second",
            "market_buy_microevents_per_second",
            "market_sell_microevents_per_second",
            "maximum_placement_depth_ticks",
            "maximum_quantity",
            "minimum_placement_depth_ticks",
            "minimum_quantity",
            "modifier_profile_id",
            "modifier_profile_sha256",
            "near_touch_levels",
            "schema_version",
            "window_us",
        }
        _require_exact_fields(payload, expected, "QueueReactiveFlowConfigurationV1")
        return cls(**{field: payload[field] for field in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FlowObservationCutV1:
    schema_version: int
    simulation_time_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    reference_price_ticks: int
    cancellable_bid_order_ids: tuple[str, ...]
    cancellable_ask_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("flow observation schema version must be 1")
        _exact_int(self.simulation_time_us, "simulation_time_us")
        for value, field in (
            (self.best_bid_ticks, "best_bid_ticks"),
            (self.best_ask_ticks, "best_ask_ticks"),
        ):
            if value is not None:
                _exact_int(value, field, minimum=1)
        _exact_int(self.reference_price_ticks, "reference_price_ticks", minimum=1)
        for values, field in (
            (self.cancellable_bid_order_ids, "cancellable_bid_order_ids"),
            (self.cancellable_ask_order_ids, "cancellable_ask_order_ids"),
        ):
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ValueError(f"{field} must be a sorted unique tuple")
            for value in values:
                _exact_string(value, field)

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_ticks": self.best_bid_ticks,
            "cancellable_ask_order_ids": list(self.cancellable_ask_order_ids),
            "cancellable_bid_order_ids": list(self.cancellable_bid_order_ids),
            "reference_price_ticks": self.reference_price_ticks,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FlowObservationCutV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "best_ask_ticks",
                "best_bid_ticks",
                "cancellable_ask_order_ids",
                "cancellable_bid_order_ids",
                "reference_price_ticks",
                "schema_version",
                "simulation_time_us",
            },
            "FlowObservationCutV1",
        )
        bids = payload["cancellable_bid_order_ids"]
        asks = payload["cancellable_ask_order_ids"]
        if type(bids) is not list or type(asks) is not list:
            raise TypeError("flow observation cancellable IDs must be arrays")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
            best_bid_ticks=(
                None
                if payload["best_bid_ticks"] is None
                else _exact_int(payload["best_bid_ticks"], "best_bid_ticks", minimum=1)
            ),
            best_ask_ticks=(
                None
                if payload["best_ask_ticks"] is None
                else _exact_int(payload["best_ask_ticks"], "best_ask_ticks", minimum=1)
            ),
            reference_price_ticks=_exact_int(
                payload["reference_price_ticks"],
                "reference_price_ticks",
                minimum=1,
            ),
            cancellable_bid_order_ids=tuple(
                _exact_string(value, "cancellable_bid_order_id") for value in bids
            ),
            cancellable_ask_order_ids=tuple(
                _exact_string(value, "cancellable_ask_order_id") for value in asks
            ),
        )


@dataclass(frozen=True, slots=True)
class QueueReactiveObservationCutV1:
    """Immutable public venue projection for one queue-feedback decision."""

    schema_version: int
    simulation_time_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    best_bid_size: int
    best_ask_size: int
    depth_near_touch_bid: int
    depth_near_touch_ask: int
    cumulative_trade_count: int
    cumulative_aggressive_buy_volume: int
    cumulative_aggressive_sell_volume: int
    reference_price_ticks: int
    cancellable_bid_order_ids: tuple[str, ...]
    cancellable_ask_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("queue observation schema version must be 1")
        _exact_int(self.simulation_time_us, "simulation_time_us")
        for value, field in (
            (self.best_bid_ticks, "best_bid_ticks"),
            (self.best_ask_ticks, "best_ask_ticks"),
        ):
            if value is not None:
                _exact_int(value, field, minimum=1)
        for value, field in (
            (self.best_bid_size, "best_bid_size"),
            (self.best_ask_size, "best_ask_size"),
            (self.depth_near_touch_bid, "depth_near_touch_bid"),
            (self.depth_near_touch_ask, "depth_near_touch_ask"),
            (self.cumulative_trade_count, "cumulative_trade_count"),
            (
                self.cumulative_aggressive_buy_volume,
                "cumulative_aggressive_buy_volume",
            ),
            (
                self.cumulative_aggressive_sell_volume,
                "cumulative_aggressive_sell_volume",
            ),
        ):
            _exact_int(value, field)
        if (self.best_bid_ticks is None) != (self.best_bid_size == 0):
            raise ValueError("queue observation bid price/size presence differs")
        if (self.best_ask_ticks is None) != (self.best_ask_size == 0):
            raise ValueError("queue observation ask price/size presence differs")
        if (
            self.depth_near_touch_bid < self.best_bid_size
            or self.depth_near_touch_ask < self.best_ask_size
        ):
            raise ValueError("near-touch depth cannot be smaller than best size")
        _exact_int(self.reference_price_ticks, "reference_price_ticks", minimum=1)
        for values, field in (
            (self.cancellable_bid_order_ids, "cancellable_bid_order_ids"),
            (self.cancellable_ask_order_ids, "cancellable_ask_order_ids"),
        ):
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ValueError(f"{field} must be a sorted unique tuple")
            for value in values:
                _exact_string(value, field)

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_size": self.best_ask_size,
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_size": self.best_bid_size,
            "best_bid_ticks": self.best_bid_ticks,
            "cancellable_ask_order_ids": list(self.cancellable_ask_order_ids),
            "cancellable_bid_order_ids": list(self.cancellable_bid_order_ids),
            "cumulative_aggressive_buy_volume": self.cumulative_aggressive_buy_volume,
            "cumulative_aggressive_sell_volume": self.cumulative_aggressive_sell_volume,
            "cumulative_trade_count": self.cumulative_trade_count,
            "depth_near_touch_ask": self.depth_near_touch_ask,
            "depth_near_touch_bid": self.depth_near_touch_bid,
            "reference_price_ticks": self.reference_price_ticks,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> QueueReactiveObservationCutV1:
        validate_strict_json(payload)
        expected = {
            "best_ask_size",
            "best_ask_ticks",
            "best_bid_size",
            "best_bid_ticks",
            "cancellable_ask_order_ids",
            "cancellable_bid_order_ids",
            "cumulative_aggressive_buy_volume",
            "cumulative_aggressive_sell_volume",
            "cumulative_trade_count",
            "depth_near_touch_ask",
            "depth_near_touch_bid",
            "reference_price_ticks",
            "schema_version",
            "simulation_time_us",
        }
        _require_exact_fields(payload, expected, "QueueReactiveObservationCutV1")
        bids = payload["cancellable_bid_order_ids"]
        asks = payload["cancellable_ask_order_ids"]
        if type(bids) is not list or type(asks) is not list:
            raise TypeError("queue observation cancellable IDs must be arrays")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
            best_bid_ticks=(
                None
                if payload["best_bid_ticks"] is None
                else _exact_int(payload["best_bid_ticks"], "best_bid_ticks", minimum=1)
            ),
            best_ask_ticks=(
                None
                if payload["best_ask_ticks"] is None
                else _exact_int(payload["best_ask_ticks"], "best_ask_ticks", minimum=1)
            ),
            best_bid_size=_exact_int(payload["best_bid_size"], "best_bid_size"),
            best_ask_size=_exact_int(payload["best_ask_size"], "best_ask_size"),
            depth_near_touch_bid=_exact_int(
                payload["depth_near_touch_bid"], "depth_near_touch_bid"
            ),
            depth_near_touch_ask=_exact_int(
                payload["depth_near_touch_ask"], "depth_near_touch_ask"
            ),
            cumulative_trade_count=_exact_int(
                payload["cumulative_trade_count"], "cumulative_trade_count"
            ),
            cumulative_aggressive_buy_volume=_exact_int(
                payload["cumulative_aggressive_buy_volume"],
                "cumulative_aggressive_buy_volume",
            ),
            cumulative_aggressive_sell_volume=_exact_int(
                payload["cumulative_aggressive_sell_volume"],
                "cumulative_aggressive_sell_volume",
            ),
            reference_price_ticks=_exact_int(
                payload["reference_price_ticks"], "reference_price_ticks", minimum=1
            ),
            cancellable_bid_order_ids=tuple(
                _exact_string(value, "cancellable_bid_order_id") for value in bids
            ),
            cancellable_ask_order_ids=tuple(
                _exact_string(value, "cancellable_ask_order_id") for value in asks
            ),
        )


@dataclass(frozen=True, slots=True)
class SimpleFlowProposalV1:
    schema_version: int
    proposal_id: str
    proposal_sequence: int
    scheduled_time_us: int
    observation_cutoff_us: int
    family: FlowEventFamily
    quantity: int | None
    placement_depth_ticks: int | None
    cancel_target_order_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("simple-flow proposal schema version must be 1")
        _exact_string(self.proposal_id, "proposal_id")
        _exact_int(self.proposal_sequence, "proposal_sequence", minimum=1)
        _exact_int(self.scheduled_time_us, "scheduled_time_us")
        _exact_int(self.observation_cutoff_us, "observation_cutoff_us")
        if self.scheduled_time_us < self.observation_cutoff_us:
            raise ValueError("flow proposal cannot precede its observation cut")
        if type(self.family) is not FlowEventFamily:
            raise TypeError("flow proposal family must use FlowEventFamily")
        submit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }
        limit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
        }
        if submit != (self.quantity is not None):
            raise ValueError("only submit proposals carry quantity")
        if self.quantity is not None:
            _exact_int(self.quantity, "quantity", minimum=1)
        if limit != (self.placement_depth_ticks is not None):
            raise ValueError("only limit proposals carry placement depth")
        if self.placement_depth_ticks is not None:
            _exact_int(self.placement_depth_ticks, "placement_depth_ticks")
        if submit and self.cancel_target_order_id is not None:
            raise ValueError("submit proposal cannot carry a cancellation target")
        _optional_string(self.cancel_target_order_id, "cancel_target_order_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_target_order_id": self.cancel_target_order_id,
            "family": self.family.value,
            "observation_cutoff_us": self.observation_cutoff_us,
            "placement_depth_ticks": self.placement_depth_ticks,
            "proposal_id": self.proposal_id,
            "proposal_sequence": self.proposal_sequence,
            "quantity": self.quantity,
            "scheduled_time_us": self.scheduled_time_us,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimpleFlowProposalV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "cancel_target_order_id",
                "family",
                "observation_cutoff_us",
                "placement_depth_ticks",
                "proposal_id",
                "proposal_sequence",
                "quantity",
                "scheduled_time_us",
                "schema_version",
            },
            "SimpleFlowProposalV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            proposal_id=_exact_string(payload["proposal_id"], "proposal_id"),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence", minimum=1
            ),
            scheduled_time_us=_exact_int(
                payload["scheduled_time_us"], "scheduled_time_us"
            ),
            observation_cutoff_us=_exact_int(
                payload["observation_cutoff_us"], "observation_cutoff_us"
            ),
            family=FlowEventFamily(_exact_string(payload["family"], "family")),
            quantity=(
                None
                if payload["quantity"] is None
                else _exact_int(payload["quantity"], "quantity", minimum=1)
            ),
            placement_depth_ticks=(
                None
                if payload["placement_depth_ticks"] is None
                else _exact_int(
                    payload["placement_depth_ticks"], "placement_depth_ticks"
                )
            ),
            cancel_target_order_id=_optional_string(
                payload["cancel_target_order_id"], "cancel_target_order_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class HawkesFlowProposalV1:
    schema_version: int
    proposal_id: str
    proposal_sequence: int
    scheduled_time_us: int
    observation_cutoff_us: int
    family: FlowEventFamily
    quantity: int | None
    placement_depth_ticks: int | None
    cancel_target_order_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Hawkes-flow proposal schema version must be 1")
        _exact_string(self.proposal_id, "proposal_id")
        _exact_int(self.proposal_sequence, "proposal_sequence", minimum=1)
        _exact_int(self.scheduled_time_us, "scheduled_time_us")
        _exact_int(self.observation_cutoff_us, "observation_cutoff_us")
        if self.scheduled_time_us < self.observation_cutoff_us:
            raise ValueError("Hawkes proposal cannot precede its observation cut")
        if type(self.family) is not FlowEventFamily:
            raise TypeError("Hawkes proposal family must use FlowEventFamily")
        submit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }
        limit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
        }
        if submit != (self.quantity is not None):
            raise ValueError("only Hawkes submit proposals carry quantity")
        if self.quantity is not None:
            _exact_int(self.quantity, "quantity", minimum=1)
        if limit != (self.placement_depth_ticks is not None):
            raise ValueError("only Hawkes limit proposals carry placement depth")
        if self.placement_depth_ticks is not None:
            _exact_int(self.placement_depth_ticks, "placement_depth_ticks")
        if submit and self.cancel_target_order_id is not None:
            raise ValueError("Hawkes submit proposal cannot carry a cancel target")
        _optional_string(self.cancel_target_order_id, "cancel_target_order_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_target_order_id": self.cancel_target_order_id,
            "family": self.family.value,
            "observation_cutoff_us": self.observation_cutoff_us,
            "placement_depth_ticks": self.placement_depth_ticks,
            "proposal_id": self.proposal_id,
            "proposal_sequence": self.proposal_sequence,
            "quantity": self.quantity,
            "scheduled_time_us": self.scheduled_time_us,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> HawkesFlowProposalV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "cancel_target_order_id",
                "family",
                "observation_cutoff_us",
                "placement_depth_ticks",
                "proposal_id",
                "proposal_sequence",
                "quantity",
                "scheduled_time_us",
                "schema_version",
            },
            "HawkesFlowProposalV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            proposal_id=_exact_string(payload["proposal_id"], "proposal_id"),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence", minimum=1
            ),
            scheduled_time_us=_exact_int(
                payload["scheduled_time_us"], "scheduled_time_us"
            ),
            observation_cutoff_us=_exact_int(
                payload["observation_cutoff_us"], "observation_cutoff_us"
            ),
            family=FlowEventFamily(_exact_string(payload["family"], "family")),
            quantity=(
                None
                if payload["quantity"] is None
                else _exact_int(payload["quantity"], "quantity", minimum=1)
            ),
            placement_depth_ticks=(
                None
                if payload["placement_depth_ticks"] is None
                else _exact_int(
                    payload["placement_depth_ticks"], "placement_depth_ticks"
                )
            ),
            cancel_target_order_id=_optional_string(
                payload["cancel_target_order_id"], "cancel_target_order_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class QueueReactiveFlowProposalV1:
    schema_version: int
    proposal_id: str
    proposal_sequence: int
    scheduled_time_us: int
    observation_cutoff_us: int
    family: FlowEventFamily
    quantity: int | None
    placement_depth_ticks: int | None
    cancel_target_order_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("queue-reactive proposal schema version must be 1")
        _exact_string(self.proposal_id, "proposal_id")
        _exact_int(self.proposal_sequence, "proposal_sequence", minimum=1)
        _exact_int(self.scheduled_time_us, "scheduled_time_us")
        _exact_int(self.observation_cutoff_us, "observation_cutoff_us")
        if self.scheduled_time_us < self.observation_cutoff_us:
            raise ValueError("queue-reactive proposal precedes its observation cut")
        if type(self.family) is not FlowEventFamily:
            raise TypeError("queue-reactive proposal family must use FlowEventFamily")
        submit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }
        limit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
        }
        if submit != (self.quantity is not None):
            raise ValueError("only queue-reactive submit proposals carry quantity")
        if self.quantity is not None:
            _exact_int(self.quantity, "quantity", minimum=1)
        if limit != (self.placement_depth_ticks is not None):
            raise ValueError("only queue-reactive limits carry placement depth")
        if self.placement_depth_ticks is not None:
            _exact_int(self.placement_depth_ticks, "placement_depth_ticks")
        if submit and self.cancel_target_order_id is not None:
            raise ValueError("queue-reactive submit cannot carry a cancel target")
        _optional_string(self.cancel_target_order_id, "cancel_target_order_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_target_order_id": self.cancel_target_order_id,
            "family": self.family.value,
            "observation_cutoff_us": self.observation_cutoff_us,
            "placement_depth_ticks": self.placement_depth_ticks,
            "proposal_id": self.proposal_id,
            "proposal_sequence": self.proposal_sequence,
            "quantity": self.quantity,
            "scheduled_time_us": self.scheduled_time_us,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> QueueReactiveFlowProposalV1:
        validate_strict_json(payload)
        expected = {
            "cancel_target_order_id",
            "family",
            "observation_cutoff_us",
            "placement_depth_ticks",
            "proposal_id",
            "proposal_sequence",
            "quantity",
            "scheduled_time_us",
            "schema_version",
        }
        _require_exact_fields(payload, expected, "QueueReactiveFlowProposalV1")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            proposal_id=_exact_string(payload["proposal_id"], "proposal_id"),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence", minimum=1
            ),
            scheduled_time_us=_exact_int(
                payload["scheduled_time_us"], "scheduled_time_us"
            ),
            observation_cutoff_us=_exact_int(
                payload["observation_cutoff_us"], "observation_cutoff_us"
            ),
            family=FlowEventFamily(_exact_string(payload["family"], "family")),
            quantity=(
                None
                if payload["quantity"] is None
                else _exact_int(payload["quantity"], "quantity", minimum=1)
            ),
            placement_depth_ticks=(
                None
                if payload["placement_depth_ticks"] is None
                else _exact_int(
                    payload["placement_depth_ticks"], "placement_depth_ticks"
                )
            ),
            cancel_target_order_id=_optional_string(
                payload["cancel_target_order_id"], "cancel_target_order_id"
            ),
        )


class SimpleFlowOwnerV1:
    """Restorable proposal state with one labeled, component-owned RNG."""

    COMPONENT_ID = FLOW_SIMPLE_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: SimpleFlowConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("simple-flow owner requires FullDayPlanV1")
        if type(configuration) is not SimpleFlowConfigurationV1:
            raise TypeError("simple-flow owner requires SimpleFlowConfigurationV1")
        self.configuration = configuration
        self.model = SimpleFlowModel()
        self.rng_label = SIMPLE_FLOW_RNG_LABEL
        self.rng = SeededRng(plan.seed_policy.derive(self.rng_label))
        self.last_observation: FlowObservationCutV1 | None = None
        self.pending_proposal: SimpleFlowProposalV1 | None = None
        self.proposal_sequence = 0
        self.diagnostic_draw_sequence: list[dict[str, object]] = []
        self.applied_count = 0
        self.rejected_count = 0
        self.last_rejection_reason: str | None = None
        self._validate_plan_binding(plan)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError("simple-flow configuration is absent from the plan") from error
        if references != (self.configuration.reference,):
            raise ValueError("simple-flow configuration differs from the plan binding")
        if self.rng_label not in {
            row.semantic_path for row in plan.seed_policy.substreams
        }:
            raise ValueError("simple-flow RNG label is undeclared")

    def _baseline_intensities(self) -> dict[FlowEventFamily, float]:
        return {
            family: value / 1_000_000.0
            for family, value in self.configuration.intensity_microevents_per_second.items()
        }

    def plan_next(
        self,
        observation: FlowObservationCutV1,
        *,
        horizon_us: int,
    ) -> SimpleFlowProposalV1 | None:
        if type(observation) is not FlowObservationCutV1:
            raise TypeError("simple flow requires a typed observation cut")
        _exact_int(horizon_us, "horizon_us")
        if self.pending_proposal is not None:
            raise RuntimeError("simple flow already owns a pending proposal")
        if observation.simulation_time_us > horizon_us:
            raise ValueError("flow observation lies beyond the horizon")
        before = self.rng.state_sha256()
        arrival = self.model.schedule_next(
            observation.simulation_time_us,
            self._baseline_intensities(),
            self.rng,
        )
        self.last_observation = observation
        draw: dict[str, object] = {
            "draw_sequence": len(self.diagnostic_draw_sequence) + 1,
            "observation_cutoff_us": observation.simulation_time_us,
            "rng_state_before_sha256": before,
        }
        if arrival is None or arrival.simulation_time_us > horizon_us:
            draw.update(
                {
                    "family": None if arrival is None else arrival.family.value,
                    "outcome": "NO_POSITIVE_INTENSITY" if arrival is None else "OUT_OF_HORIZON",
                    "scheduled_time_us": (
                        None if arrival is None else arrival.simulation_time_us
                    ),
                }
            )
            draw["rng_state_after_sha256"] = self.rng.state_sha256()
            self.diagnostic_draw_sequence.append(draw)
            return None

        family = arrival.family
        quantity: int | None = None
        placement_depth: int | None = None
        cancel_target: str | None = None
        if family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }:
            quantity = self.rng.integer(
                self.configuration.minimum_quantity,
                self.configuration.maximum_quantity,
            )
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            placement_depth = self.rng.integer(
                self.configuration.minimum_placement_depth_ticks,
                self.configuration.maximum_placement_depth_ticks,
            )
        if family is FlowEventFamily.CANCEL_BID:
            candidates = observation.cancellable_bid_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[min(int(selection_draw * len(candidates)), len(candidates) - 1)]
            )
        elif family is FlowEventFamily.CANCEL_ASK:
            candidates = observation.cancellable_ask_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[min(int(selection_draw * len(candidates)), len(candidates) - 1)]
            )

        sequence = self.proposal_sequence + 1
        proposal = SimpleFlowProposalV1(
            schema_version=1,
            proposal_id=f"FLOW-SIMPLE-P-{sequence:010d}",
            proposal_sequence=sequence,
            scheduled_time_us=arrival.simulation_time_us,
            observation_cutoff_us=observation.simulation_time_us,
            family=family,
            quantity=quantity,
            placement_depth_ticks=placement_depth,
            cancel_target_order_id=cancel_target,
        )
        self.proposal_sequence = sequence
        self.pending_proposal = proposal
        draw.update(
            {
                "cancel_target_order_id": cancel_target,
                "family": family.value,
                "outcome": "PROPOSAL_CREATED",
                "placement_depth_ticks": placement_depth,
                "proposal_id": proposal.proposal_id,
                "quantity": quantity,
                "scheduled_time_us": proposal.scheduled_time_us,
            }
        )
        draw["rng_state_after_sha256"] = self.rng.state_sha256()
        self.diagnostic_draw_sequence.append(draw)
        return proposal

    def resolve_pending(self, *, applied: bool, rejection_reason: str | None) -> None:
        if type(applied) is not bool:
            raise TypeError("flow proposal applied state must be boolean")
        proposal = self.pending_proposal
        if proposal is None:
            raise RuntimeError("simple flow has no pending proposal to resolve")
        if applied:
            if rejection_reason is not None:
                raise ValueError("applied flow proposal cannot carry a rejection")
            self.applied_count += 1
        else:
            self.rejected_count += 1
            self.last_rejection_reason = _exact_string(
                rejection_reason, "rejection_reason"
            )
        self.model.observe(proposal.family, proposal.scheduled_time_us)
        self.pending_proposal = None

    def checkpoint_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "configuration": self.configuration.as_dict(),
            "diagnostic_draw_sequence": list(self.diagnostic_draw_sequence),
            "intensity_state": {
                family.value: value
                for family, value in self.configuration.intensity_microevents_per_second.items()
            },
            "model_id_version": {
                "model_id": SIMPLE_FLOW_MODEL_ID,
                "model_version": SIMPLE_FLOW_MODEL_VERSION,
                "runtime_state": self.model.runtime_state(),
            },
            "observation_cutoff": (
                None if self.last_observation is None else self.last_observation.as_dict()
            ),
            "pending_proposal": (
                None if self.pending_proposal is None else self.pending_proposal.as_dict()
            ),
            "proposal_sequence": self.proposal_sequence,
            "rejection_state": {
                "applied_count": self.applied_count,
                "last_rejection_reason": self.last_rejection_reason,
                "rejected_count": self.rejected_count,
            },
            "rng_label": self.rng_label,
            "rng_state": self.rng.runtime_state(),
            "schema_version": SIMPLE_FLOW_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
    ) -> SimpleFlowOwnerV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "configuration",
                "diagnostic_draw_sequence",
                "intensity_state",
                "model_id_version",
                "observation_cutoff",
                "pending_proposal",
                "proposal_sequence",
                "rejection_state",
                "rng_label",
                "rng_state",
                "schema_version",
            },
            "SimpleFlowOwnerV1",
        )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != SIMPLE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("simple-flow owner schema version is unsupported")
        raw_configuration = payload["configuration"]
        raw_model = payload["model_id_version"]
        raw_rejection = payload["rejection_state"]
        raw_rng = payload["rng_state"]
        if not all(
            isinstance(value, Mapping)
            for value in (raw_configuration, raw_model, raw_rejection, raw_rng)
        ):
            raise TypeError("simple-flow owner nested states must be objects")
        configuration = SimpleFlowConfigurationV1.from_dict(raw_configuration)
        owner = cls(plan, configuration)
        _require_exact_fields(
            raw_model,
            {"model_id", "model_version", "runtime_state"},
            "simple-flow model identity",
        )
        if (
            raw_model["model_id"] != SIMPLE_FLOW_MODEL_ID
            or raw_model["model_version"] != SIMPLE_FLOW_MODEL_VERSION
            or not isinstance(raw_model["runtime_state"], Mapping)
        ):
            raise ValueError("simple-flow model identity is unsupported")
        owner.model = SimpleFlowModel.from_runtime_state(raw_model["runtime_state"])
        if payload["rng_label"] != SIMPLE_FLOW_RNG_LABEL:
            raise ValueError("simple-flow RNG label is unsupported")
        owner.rng = SeededRng.from_runtime_state(raw_rng)
        raw_observation = payload["observation_cutoff"]
        raw_pending = payload["pending_proposal"]
        owner.last_observation = (
            None
            if raw_observation is None
            else FlowObservationCutV1.from_dict(raw_observation)  # type: ignore[arg-type]
        )
        owner.pending_proposal = (
            None
            if raw_pending is None
            else SimpleFlowProposalV1.from_dict(raw_pending)  # type: ignore[arg-type]
        )
        owner.proposal_sequence = _exact_int(
            payload["proposal_sequence"], "proposal_sequence"
        )
        diagnostics = payload["diagnostic_draw_sequence"]
        if type(diagnostics) is not list or any(
            not isinstance(row, Mapping) for row in diagnostics
        ):
            raise TypeError("simple-flow diagnostics must be an object array")
        owner.diagnostic_draw_sequence = [dict(row) for row in diagnostics]
        _require_exact_fields(
            raw_rejection,
            {"applied_count", "last_rejection_reason", "rejected_count"},
            "simple-flow rejection state",
        )
        owner.applied_count = _exact_int(raw_rejection["applied_count"], "applied_count")
        owner.rejected_count = _exact_int(
            raw_rejection["rejected_count"], "rejected_count"
        )
        owner.last_rejection_reason = _optional_string(
            raw_rejection["last_rejection_reason"], "last_rejection_reason"
        )
        expected_intensity = {
            family.value: value
            for family, value in configuration.intensity_microevents_per_second.items()
        }
        if payload["intensity_state"] != expected_intensity:
            raise ValueError("simple-flow intensity state differs from configuration")
        owner.assert_invariants(plan)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("simple-flow checkpoint is not a canonical fixed point")
        return owner

    def assert_invariants(self, plan: FullDayPlanV1) -> None:
        self._validate_plan_binding(plan)
        expected_seed = plan.seed_policy.derive(self.rng_label)
        if self.rng.seed != expected_seed:
            raise RuntimeError("simple-flow RNG differs from the plan substream")
        if self.proposal_sequence < 0:
            raise RuntimeError("simple-flow proposal sequence is invalid")
        resolved = self.applied_count + self.rejected_count
        pending_count = 0 if self.pending_proposal is None else 1
        if resolved + pending_count != self.proposal_sequence:
            raise RuntimeError("simple-flow proposal lifecycle is not conserved")
        if self.pending_proposal is not None:
            if (
                self.pending_proposal.proposal_sequence != self.proposal_sequence
                or self.last_observation is None
                or self.pending_proposal.observation_cutoff_us
                != self.last_observation.simulation_time_us
            ):
                raise RuntimeError("simple-flow pending proposal differs from its cut")
        for sequence, row in enumerate(self.diagnostic_draw_sequence, start=1):
            if row.get("draw_sequence") != sequence:
                raise RuntimeError("simple-flow diagnostic draw sequence has a gap")
        if self.diagnostic_draw_sequence and (
            self.diagnostic_draw_sequence[-1].get("rng_state_after_sha256")
            != self.rng.state_sha256()
        ):
            raise RuntimeError("simple-flow diagnostic tail differs from RNG state")
        validate_strict_json(self.checkpoint_state())


class HawkesFlowOwnerV1:
    """Restorable Hawkes excitation owner with one isolated RNG substream."""

    COMPONENT_ID = FLOW_HAWKES_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: HawkesFlowConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("Hawkes-flow owner requires FullDayPlanV1")
        if type(configuration) is not HawkesFlowConfigurationV1:
            raise TypeError(
                "Hawkes-flow owner requires HawkesFlowConfigurationV1"
            )
        self.configuration = configuration
        self.model = HawkesFlowModel(
            configuration.accepted_profile,
            use_runtime_baseline=True,
        )
        self.rng_label = HAWKES_FLOW_RNG_LABEL
        self.rng = SeededRng(plan.seed_policy.derive(self.rng_label))
        self.last_observation: FlowObservationCutV1 | None = None
        self.pending_proposal: HawkesFlowProposalV1 | None = None
        self.proposal_sequence = 0
        self.diagnostic_draw_sequence: list[dict[str, object]] = []
        self.applied_count = 0
        self.rejected_count = 0
        self.last_rejection_reason: str | None = None
        self._validate_plan_binding(plan)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError("Hawkes-flow configuration is absent from the plan") from error
        if references != (self.configuration.reference,):
            raise ValueError("Hawkes-flow configuration differs from plan binding")
        if self.rng_label not in {
            row.semantic_path for row in plan.seed_policy.substreams
        }:
            raise ValueError("Hawkes-flow RNG label is undeclared")

    def _baseline_intensities(self) -> dict[FlowEventFamily, float]:
        return {
            family: value / 1_000_000.0
            for family, value in self.configuration.intensity_microevents_per_second.items()
        }

    def _intensity_state(self) -> dict[str, object]:
        time_us = (
            self.model.runtime_state()["state_time_us"]
            if self.last_observation is None
            else self.last_observation.simulation_time_us
        )
        if type(time_us) is not int:
            raise RuntimeError("Hawkes model state time is not an integer")
        current = self.model.current_intensities(
            time_us,
            self._baseline_intensities(),
        )
        return {
            "baseline_microevents_per_second": {
                family.value: value
                for family, value in self.configuration.intensity_microevents_per_second.items()
            },
            "current_per_second_hex": {
                family.value: _float_hex(current[family])
                for family in FLOW_CHANNELS
            },
            "observation_time_us": time_us,
        }

    def plan_next(
        self,
        observation: FlowObservationCutV1,
        *,
        horizon_us: int,
    ) -> HawkesFlowProposalV1 | None:
        if type(observation) is not FlowObservationCutV1:
            raise TypeError("Hawkes flow requires a typed observation cut")
        _exact_int(horizon_us, "horizon_us")
        if self.pending_proposal is not None:
            raise RuntimeError("Hawkes flow already owns a pending proposal")
        if observation.simulation_time_us > horizon_us:
            raise ValueError("Hawkes observation lies beyond the horizon")
        before = self.rng.state_sha256()
        current_intensities = self.model.current_intensities(
            observation.simulation_time_us,
            self._baseline_intensities(),
        )
        arrival = self.model.schedule_next(
            observation.simulation_time_us,
            self._baseline_intensities(),
            self.rng,
        )
        self.last_observation = observation
        draw: dict[str, object] = {
            "draw_sequence": len(self.diagnostic_draw_sequence) + 1,
            "intensity_state_before_hex": {
                family.value: _float_hex(current_intensities[family])
                for family in FLOW_CHANNELS
            },
            "observation_cutoff_us": observation.simulation_time_us,
            "rng_state_before_sha256": before,
        }
        if arrival is None or arrival.simulation_time_us > horizon_us:
            draw.update(
                {
                    "family": None if arrival is None else arrival.family.value,
                    "outcome": (
                        "NO_POSITIVE_INTENSITY"
                        if arrival is None
                        else "OUT_OF_HORIZON"
                    ),
                    "scheduled_time_us": (
                        None if arrival is None else arrival.simulation_time_us
                    ),
                }
            )
            draw["rng_state_after_sha256"] = self.rng.state_sha256()
            self.diagnostic_draw_sequence.append(draw)
            return None

        family = arrival.family
        quantity: int | None = None
        placement_depth: int | None = None
        cancel_target: str | None = None
        if family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }:
            quantity = self.rng.integer(
                self.configuration.minimum_quantity,
                self.configuration.maximum_quantity,
            )
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            placement_depth = self.rng.integer(
                self.configuration.minimum_placement_depth_ticks,
                self.configuration.maximum_placement_depth_ticks,
            )
        if family is FlowEventFamily.CANCEL_BID:
            candidates = observation.cancellable_bid_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[
                    min(int(selection_draw * len(candidates)), len(candidates) - 1)
                ]
            )
        elif family is FlowEventFamily.CANCEL_ASK:
            candidates = observation.cancellable_ask_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[
                    min(int(selection_draw * len(candidates)), len(candidates) - 1)
                ]
            )

        sequence = self.proposal_sequence + 1
        proposal = HawkesFlowProposalV1(
            schema_version=1,
            proposal_id=f"FLOW-HAWKES-P-{sequence:010d}",
            proposal_sequence=sequence,
            scheduled_time_us=arrival.simulation_time_us,
            observation_cutoff_us=observation.simulation_time_us,
            family=family,
            quantity=quantity,
            placement_depth_ticks=placement_depth,
            cancel_target_order_id=cancel_target,
        )
        self.proposal_sequence = sequence
        self.pending_proposal = proposal
        draw.update(
            {
                "cancel_target_order_id": cancel_target,
                "family": family.value,
                "outcome": "PROPOSAL_CREATED",
                "placement_depth_ticks": placement_depth,
                "proposal_id": proposal.proposal_id,
                "quantity": quantity,
                "scheduled_time_us": proposal.scheduled_time_us,
            }
        )
        draw["rng_state_after_sha256"] = self.rng.state_sha256()
        self.diagnostic_draw_sequence.append(draw)
        return proposal

    def resolve_pending(self, *, applied: bool, rejection_reason: str | None) -> None:
        if type(applied) is not bool:
            raise TypeError("Hawkes proposal applied state must be boolean")
        proposal = self.pending_proposal
        if proposal is None:
            raise RuntimeError("Hawkes flow has no pending proposal to resolve")
        if applied:
            if rejection_reason is not None:
                raise ValueError("applied Hawkes proposal cannot carry rejection")
            self.applied_count += 1
        else:
            self.rejected_count += 1
            self.last_rejection_reason = _exact_string(
                rejection_reason, "rejection_reason"
            )
        self.model.observe(proposal.family, proposal.scheduled_time_us)
        self.pending_proposal = None

    def checkpoint_state(self) -> dict[str, object]:
        raw_runtime_state = self.model.runtime_state()
        encoded_runtime_state = _encode_hawkes_runtime_state(raw_runtime_state)
        state: dict[str, object] = {
            "configuration": self.configuration.as_dict(),
            "diagnostic_draw_sequence": list(self.diagnostic_draw_sequence),
            "excitation_state": encoded_runtime_state["excitation"],
            "intensity_state": self._intensity_state(),
            "last_decay_time_us": encoded_runtime_state["state_time_us"],
            "model_id_version": {
                "model_id": HAWKES_FLOW_MODEL_ID,
                "model_version": HAWKES_FLOW_MODEL_VERSION,
                "runtime_state": encoded_runtime_state,
            },
            "observation_cutoff": (
                None if self.last_observation is None else self.last_observation.as_dict()
            ),
            "pending_proposal": (
                None if self.pending_proposal is None else self.pending_proposal.as_dict()
            ),
            "proposal_sequence": self.proposal_sequence,
            "rejection_state": {
                "applied_count": self.applied_count,
                "last_rejection_reason": self.last_rejection_reason,
                "rejected_count": self.rejected_count,
            },
            "rng_label": self.rng_label,
            "rng_state": self.rng.runtime_state(),
            "schema_version": HAWKES_FLOW_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
    ) -> HawkesFlowOwnerV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "configuration",
                "diagnostic_draw_sequence",
                "excitation_state",
                "intensity_state",
                "last_decay_time_us",
                "model_id_version",
                "observation_cutoff",
                "pending_proposal",
                "proposal_sequence",
                "rejection_state",
                "rng_label",
                "rng_state",
                "schema_version",
            },
            "HawkesFlowOwnerV1",
        )
        if payload["schema_version"] != HAWKES_FLOW_SCHEMA_VERSION:
            raise ValueError("Hawkes-flow owner schema version is unsupported")
        raw_configuration = payload["configuration"]
        raw_model = payload["model_id_version"]
        raw_rejection = payload["rejection_state"]
        raw_rng = payload["rng_state"]
        if not all(
            isinstance(value, Mapping)
            for value in (raw_configuration, raw_model, raw_rejection, raw_rng)
        ):
            raise TypeError("Hawkes-flow nested states must be objects")
        configuration = HawkesFlowConfigurationV1.from_dict(raw_configuration)
        owner = cls(plan, configuration)
        _require_exact_fields(
            raw_model,
            {"model_id", "model_version", "runtime_state"},
            "Hawkes-flow model identity",
        )
        raw_runtime_state = raw_model["runtime_state"]
        if (
            raw_model["model_id"] != HAWKES_FLOW_MODEL_ID
            or raw_model["model_version"] != HAWKES_FLOW_MODEL_VERSION
            or not isinstance(raw_runtime_state, Mapping)
        ):
            raise ValueError("Hawkes-flow model identity is unsupported")
        owner.model = HawkesFlowModel.from_runtime_state(
            _decode_hawkes_runtime_state(raw_runtime_state),
            config=configuration.accepted_profile,
        )
        if payload["excitation_state"] != raw_runtime_state["excitation"]:
            raise ValueError("Hawkes excitation state differs from model state")
        if payload["last_decay_time_us"] != raw_runtime_state["state_time_us"]:
            raise ValueError("Hawkes decay time differs from model state")
        if payload["rng_label"] != HAWKES_FLOW_RNG_LABEL:
            raise ValueError("Hawkes-flow RNG label is unsupported")
        owner.rng = SeededRng.from_runtime_state(raw_rng)
        raw_observation = payload["observation_cutoff"]
        raw_pending = payload["pending_proposal"]
        owner.last_observation = (
            None
            if raw_observation is None
            else FlowObservationCutV1.from_dict(raw_observation)  # type: ignore[arg-type]
        )
        owner.pending_proposal = (
            None
            if raw_pending is None
            else HawkesFlowProposalV1.from_dict(raw_pending)  # type: ignore[arg-type]
        )
        owner.proposal_sequence = _exact_int(
            payload["proposal_sequence"], "proposal_sequence"
        )
        diagnostics = payload["diagnostic_draw_sequence"]
        if type(diagnostics) is not list or any(
            not isinstance(row, Mapping) for row in diagnostics
        ):
            raise TypeError("Hawkes-flow diagnostics must be an object array")
        owner.diagnostic_draw_sequence = [dict(row) for row in diagnostics]
        _require_exact_fields(
            raw_rejection,
            {"applied_count", "last_rejection_reason", "rejected_count"},
            "Hawkes-flow rejection state",
        )
        owner.applied_count = _exact_int(raw_rejection["applied_count"], "applied_count")
        owner.rejected_count = _exact_int(
            raw_rejection["rejected_count"], "rejected_count"
        )
        owner.last_rejection_reason = _optional_string(
            raw_rejection["last_rejection_reason"], "last_rejection_reason"
        )
        if payload["intensity_state"] != owner._intensity_state():
            raise ValueError("Hawkes intensity state differs from restored model")
        owner.assert_invariants(plan)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("Hawkes-flow checkpoint is not a canonical fixed point")
        return owner

    def assert_invariants(self, plan: FullDayPlanV1) -> None:
        self._validate_plan_binding(plan)
        if (
            self.model.config.profile_id != self.configuration.accepted_profile_id
            or _hawkes_profile_sha256(self.model.config)
            != self.configuration.accepted_profile_sha256
        ):
            raise RuntimeError("Hawkes model differs from its accepted profile binding")
        expected_seed = plan.seed_policy.derive(self.rng_label)
        if self.rng.seed != expected_seed:
            raise RuntimeError("Hawkes-flow RNG differs from the plan substream")
        if self.proposal_sequence < 0:
            raise RuntimeError("Hawkes proposal sequence is invalid")
        resolved = self.applied_count + self.rejected_count
        pending_count = 0 if self.pending_proposal is None else 1
        if resolved + pending_count != self.proposal_sequence:
            raise RuntimeError("Hawkes proposal lifecycle is not conserved")
        model_state = self.model.runtime_state()
        if model_state["observed_events"] != resolved:
            raise RuntimeError("Hawkes excitation count differs from resolved proposals")
        if self.pending_proposal is not None:
            if (
                self.pending_proposal.proposal_sequence != self.proposal_sequence
                or self.last_observation is None
                or self.pending_proposal.observation_cutoff_us
                != self.last_observation.simulation_time_us
                or model_state["state_time_us"] > self.pending_proposal.scheduled_time_us
            ):
                raise RuntimeError("Hawkes pending proposal differs from its cut/state")
        for sequence, row in enumerate(self.diagnostic_draw_sequence, start=1):
            if row.get("draw_sequence") != sequence:
                raise RuntimeError("Hawkes diagnostic draw sequence has a gap")
        if self.diagnostic_draw_sequence and (
            self.diagnostic_draw_sequence[-1].get("rng_state_after_sha256")
            != self.rng.state_sha256()
        ):
            raise RuntimeError("Hawkes diagnostic tail differs from RNG state")
        validate_strict_json(self.checkpoint_state())


class QueueReactiveFlowOwnerV1:
    """Restorable observable queue-feedback owner with bounded integer windows."""

    COMPONENT_ID = FLOW_QUEUE_REACTIVE_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: QueueReactiveFlowConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("queue-reactive owner requires FullDayPlanV1")
        if type(configuration) is not QueueReactiveFlowConfigurationV1:
            raise TypeError(
                "queue-reactive owner requires QueueReactiveFlowConfigurationV1"
            )
        self.configuration = configuration
        self.arrival_model = SimpleFlowModel()
        self.modifier = QueueReactiveFlowModifier(configuration.modifier_profile)
        self.rng_label = QUEUE_REACTIVE_FLOW_RNG_LABEL
        self.rng = SeededRng(plan.seed_policy.derive(self.rng_label))
        self.last_observation: QueueReactiveObservationCutV1 | None = None
        self.pending_proposal: QueueReactiveFlowProposalV1 | None = None
        self.proposal_sequence = 0
        self.diagnostic_draw_sequence: list[dict[str, object]] = []
        self.applied_count = 0
        self.rejected_count = 0
        self.last_rejection_reason: str | None = None
        self.queue_changes: list[tuple[int, int, int, int, int]] = []
        self.aggressive_flow: list[tuple[int, int, int]] = []
        self.midpoints_x2: list[tuple[int, int]] = []
        self._validate_plan_binding(plan)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError(
                "queue-reactive configuration is absent from the plan"
            ) from error
        if references != (self.configuration.reference,):
            raise ValueError(
                "queue-reactive configuration differs from the plan binding"
            )
        if self.rng_label not in {
            row.semantic_path for row in plan.seed_policy.substreams
        }:
            raise ValueError("queue-reactive RNG label is undeclared")

    def _baseline_intensities(self) -> dict[FlowEventFamily, float]:
        return {
            family: value / 1_000_000.0
            for family, value in self.configuration.intensity_microevents_per_second.items()
        }

    def _prune(self, simulation_time_us: int) -> None:
        cutoff = simulation_time_us - self.configuration.window_us
        for records in (
            self.queue_changes,
            self.aggressive_flow,
            self.midpoints_x2,
        ):
            while records and records[0][0] < cutoff:
                records.pop(0)
            if len(records) > QUEUE_REACTIVE_MAX_RETAINED_RECORDS:
                del records[: len(records) - QUEUE_REACTIVE_MAX_RETAINED_RECORDS]

    def _ingest_observation(self, observation: QueueReactiveObservationCutV1) -> None:
        previous = self.last_observation
        if previous is not None:
            if observation.simulation_time_us <= previous.simulation_time_us:
                raise ValueError("queue-reactive observation cutoff is stale")
            if observation.cumulative_trade_count < previous.cumulative_trade_count:
                raise ValueError("queue-reactive cumulative trade count rolled back")
            if (
                observation.cumulative_aggressive_buy_volume
                < previous.cumulative_aggressive_buy_volume
                or observation.cumulative_aggressive_sell_volume
                < previous.cumulative_aggressive_sell_volume
            ):
                raise ValueError("queue-reactive cumulative aggressive flow rolled back")
            bid_delta = (
                observation.depth_near_touch_bid
                - previous.depth_near_touch_bid
            )
            ask_delta = (
                observation.depth_near_touch_ask
                - previous.depth_near_touch_ask
            )
            self.queue_changes.append(
                (
                    observation.simulation_time_us,
                    max(0, -bid_delta),
                    max(0, -ask_delta),
                    max(0, bid_delta),
                    max(0, ask_delta),
                )
            )
            aggressive_buy = (
                observation.cumulative_aggressive_buy_volume
                - previous.cumulative_aggressive_buy_volume
            )
            aggressive_sell = (
                observation.cumulative_aggressive_sell_volume
                - previous.cumulative_aggressive_sell_volume
            )
            if aggressive_buy or aggressive_sell:
                self.aggressive_flow.append(
                    (
                        observation.simulation_time_us,
                        aggressive_buy,
                        aggressive_sell,
                    )
                )
        if (
            observation.best_bid_ticks is not None
            and observation.best_ask_ticks is not None
        ):
            self.midpoints_x2.append(
                (
                    observation.simulation_time_us,
                    observation.best_bid_ticks + observation.best_ask_ticks,
                )
            )
        self.last_observation = observation
        self._prune(observation.simulation_time_us)

    def _queue_state(self) -> QueueReactiveState | None:
        observation = self.last_observation
        if observation is None:
            return None
        denominator = observation.best_bid_size + observation.best_ask_size
        imbalance = (
            (observation.best_bid_size - observation.best_ask_size) / denominator
            if denominator
            else 0.0
        )
        aggressive_buy = sum(row[1] for row in self.aggressive_flow)
        aggressive_sell = sum(row[2] for row in self.aggressive_flow)
        aggressive_total = aggressive_buy + aggressive_sell
        aggressive_imbalance = (
            (aggressive_buy - aggressive_sell) / aggressive_total
            if aggressive_total
            else 0.0
        )
        current_midpoint_x2 = (
            observation.best_bid_ticks + observation.best_ask_ticks
            if observation.best_bid_ticks is not None
            and observation.best_ask_ticks is not None
            else None
        )
        price_movement = (
            (current_midpoint_x2 - self.midpoints_x2[0][1]) / 2.0
            if current_midpoint_x2 is not None and self.midpoints_x2
            else 0.0
        )
        return QueueReactiveState(
            simulation_time_us=observation.simulation_time_us,
            window_us=self.configuration.window_us,
            best_bid_size=observation.best_bid_size,
            best_ask_size=observation.best_ask_size,
            imbalance=imbalance,
            spread_ticks=(
                observation.best_ask_ticks - observation.best_bid_ticks
                if observation.best_bid_ticks is not None
                and observation.best_ask_ticks is not None
                else None
            ),
            depth_near_touch_bid=observation.depth_near_touch_bid,
            depth_near_touch_ask=observation.depth_near_touch_ask,
            recent_depletion_bid=sum(row[1] for row in self.queue_changes),
            recent_depletion_ask=sum(row[2] for row in self.queue_changes),
            recent_replenishment_bid=sum(row[3] for row in self.queue_changes),
            recent_replenishment_ask=sum(row[4] for row in self.queue_changes),
            recent_aggressive_buy_volume=aggressive_buy,
            recent_aggressive_sell_volume=aggressive_sell,
            recent_aggressive_flow_imbalance=aggressive_imbalance,
            short_term_price_movement_ticks=price_movement,
        )

    def plan_next(
        self,
        observation: QueueReactiveObservationCutV1,
        *,
        horizon_us: int,
    ) -> QueueReactiveFlowProposalV1 | None:
        if type(observation) is not QueueReactiveObservationCutV1:
            raise TypeError("queue-reactive flow requires a typed observation cut")
        _exact_int(horizon_us, "horizon_us")
        if self.pending_proposal is not None:
            raise RuntimeError("queue-reactive flow already owns a pending proposal")
        if observation.simulation_time_us > horizon_us:
            raise ValueError("queue-reactive observation lies beyond the horizon")
        self._ingest_observation(observation)
        state = self._queue_state()
        if state is None:  # pragma: no cover - established by ingestion
            raise RuntimeError("queue-reactive state is unavailable")
        before = self.rng.state_sha256()
        inspection = self.modifier.inspect_state(self._baseline_intensities(), state)
        arrival = self.arrival_model.schedule_next(
            observation.simulation_time_us,
            inspection.final_intensities,
            self.rng,
        )
        draw: dict[str, object] = {
            "draw_sequence": len(self.diagnostic_draw_sequence) + 1,
            "intensity_inspection": _encode_queue_inspection(inspection),
            "observation_cutoff_us": observation.simulation_time_us,
            "rng_state_before_sha256": before,
        }
        if arrival is None or arrival.simulation_time_us > horizon_us:
            draw.update(
                {
                    "family": None if arrival is None else arrival.family.value,
                    "outcome": (
                        "NO_POSITIVE_INTENSITY"
                        if arrival is None
                        else "OUT_OF_HORIZON"
                    ),
                    "scheduled_time_us": (
                        None if arrival is None else arrival.simulation_time_us
                    ),
                }
            )
            draw["rng_state_after_sha256"] = self.rng.state_sha256()
            self.diagnostic_draw_sequence.append(draw)
            return None

        family = arrival.family
        quantity: int | None = None
        placement_depth: int | None = None
        cancel_target: str | None = None
        if family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }:
            quantity = self.rng.integer(
                self.configuration.minimum_quantity,
                self.configuration.maximum_quantity,
            )
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            placement_depth = self.rng.integer(
                self.configuration.minimum_placement_depth_ticks,
                self.configuration.maximum_placement_depth_ticks,
            )
        if family is FlowEventFamily.CANCEL_BID:
            candidates = observation.cancellable_bid_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[
                    min(int(selection_draw * len(candidates)), len(candidates) - 1)
                ]
            )
        elif family is FlowEventFamily.CANCEL_ASK:
            candidates = observation.cancellable_ask_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[
                    min(int(selection_draw * len(candidates)), len(candidates) - 1)
                ]
            )

        sequence = self.proposal_sequence + 1
        proposal = QueueReactiveFlowProposalV1(
            schema_version=1,
            proposal_id=f"FLOW-QUEUE-P-{sequence:010d}",
            proposal_sequence=sequence,
            scheduled_time_us=arrival.simulation_time_us,
            observation_cutoff_us=observation.simulation_time_us,
            family=family,
            quantity=quantity,
            placement_depth_ticks=placement_depth,
            cancel_target_order_id=cancel_target,
        )
        self.proposal_sequence = sequence
        self.pending_proposal = proposal
        draw.update(
            {
                "cancel_target_order_id": cancel_target,
                "family": family.value,
                "outcome": "PROPOSAL_CREATED",
                "placement_depth_ticks": placement_depth,
                "proposal_id": proposal.proposal_id,
                "quantity": quantity,
                "scheduled_time_us": proposal.scheduled_time_us,
            }
        )
        draw["rng_state_after_sha256"] = self.rng.state_sha256()
        self.diagnostic_draw_sequence.append(draw)
        return proposal

    def resolve_pending(self, *, applied: bool, rejection_reason: str | None) -> None:
        if type(applied) is not bool:
            raise TypeError("queue-reactive proposal applied state must be boolean")
        proposal = self.pending_proposal
        if proposal is None:
            raise RuntimeError("queue-reactive flow has no pending proposal to resolve")
        if applied:
            if rejection_reason is not None:
                raise ValueError(
                    "applied queue-reactive proposal cannot carry a rejection"
                )
            self.applied_count += 1
        else:
            self.rejected_count += 1
            self.last_rejection_reason = _exact_string(
                rejection_reason, "rejection_reason"
            )
        self.arrival_model.observe(proposal.family, proposal.scheduled_time_us)
        self.pending_proposal = None

    def checkpoint_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "configuration": self.configuration.as_dict(),
            "diagnostic_draw_sequence": list(self.diagnostic_draw_sequence),
            "model_id_version": {
                "arrival_model_runtime_state": self.arrival_model.runtime_state(),
                "last_intensity_inspection": _encode_queue_inspection(
                    self.modifier.last_inspection
                ),
                "model_id": QUEUE_REACTIVE_FLOW_MODEL_ID,
                "model_version": QUEUE_REACTIVE_FLOW_MODEL_VERSION,
                "modifier_profile_id": self.configuration.modifier_profile_id,
                "modifier_profile_sha256": self.configuration.modifier_profile_sha256,
            },
            "observation_cutoff": (
                None if self.last_observation is None else self.last_observation.as_dict()
            ),
            "pending_proposal": (
                None if self.pending_proposal is None else self.pending_proposal.as_dict()
            ),
            "proposal_sequence": self.proposal_sequence,
            "rejection_state": {
                "applied_count": self.applied_count,
                "last_rejection_reason": self.last_rejection_reason,
                "rejected_count": self.rejected_count,
            },
            "retained_windows": {
                "aggressive_flow": [list(row) for row in self.aggressive_flow],
                "midpoints_x2": [list(row) for row in self.midpoints_x2],
                "queue_changes": [list(row) for row in self.queue_changes],
            },
            "rng_label": self.rng_label,
            "rng_state": self.rng.runtime_state(),
            "schema_version": QUEUE_REACTIVE_FLOW_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    @staticmethod
    def _decode_window(
        payload: object,
        *,
        field: str,
        width: int,
    ) -> list[tuple[int, ...]]:
        if type(payload) is not list:
            raise TypeError(f"queue-reactive {field} window must be an array")
        if len(payload) > QUEUE_REACTIVE_MAX_RETAINED_RECORDS:
            raise ValueError(f"queue-reactive {field} window exceeds its bound")
        decoded: list[tuple[int, ...]] = []
        previous_time = -1
        for index, raw_row in enumerate(payload):
            if type(raw_row) is not list or len(raw_row) != width:
                raise TypeError(
                    f"queue-reactive {field}[{index}] must be a width-{width} array"
                )
            row = tuple(
                _exact_int(value, f"{field}[{index}][{column}]")
                for column, value in enumerate(raw_row)
            )
            if row[0] <= previous_time:
                raise ValueError(
                    f"queue-reactive {field} timestamps must be strictly increasing"
                )
            previous_time = row[0]
            decoded.append(row)
        return decoded

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
    ) -> QueueReactiveFlowOwnerV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "configuration",
                "diagnostic_draw_sequence",
                "model_id_version",
                "observation_cutoff",
                "pending_proposal",
                "proposal_sequence",
                "rejection_state",
                "retained_windows",
                "rng_label",
                "rng_state",
                "schema_version",
            },
            "QueueReactiveFlowOwnerV1",
        )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != QUEUE_REACTIVE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("queue-reactive owner schema version is unsupported")
        raw_configuration = payload["configuration"]
        raw_model = payload["model_id_version"]
        raw_rejection = payload["rejection_state"]
        raw_windows = payload["retained_windows"]
        raw_rng = payload["rng_state"]
        if not all(
            isinstance(value, Mapping)
            for value in (
                raw_configuration,
                raw_model,
                raw_rejection,
                raw_windows,
                raw_rng,
            )
        ):
            raise TypeError("queue-reactive nested states must be objects")
        configuration = QueueReactiveFlowConfigurationV1.from_dict(
            raw_configuration
        )
        owner = cls(plan, configuration)
        _require_exact_fields(
            raw_model,
            {
                "arrival_model_runtime_state",
                "last_intensity_inspection",
                "model_id",
                "model_version",
                "modifier_profile_id",
                "modifier_profile_sha256",
            },
            "queue-reactive model identity",
        )
        raw_arrival_model = raw_model["arrival_model_runtime_state"]
        if (
            raw_model["model_id"] != QUEUE_REACTIVE_FLOW_MODEL_ID
            or raw_model["model_version"] != QUEUE_REACTIVE_FLOW_MODEL_VERSION
            or raw_model["modifier_profile_id"]
            != configuration.modifier_profile_id
            or raw_model["modifier_profile_sha256"]
            != configuration.modifier_profile_sha256
            or not isinstance(raw_arrival_model, Mapping)
        ):
            raise ValueError("queue-reactive model identity is unsupported")
        owner.arrival_model = SimpleFlowModel.from_runtime_state(raw_arrival_model)
        if payload["rng_label"] != QUEUE_REACTIVE_FLOW_RNG_LABEL:
            raise ValueError("queue-reactive RNG label is unsupported")
        owner.rng = SeededRng.from_runtime_state(raw_rng)
        raw_observation = payload["observation_cutoff"]
        raw_pending = payload["pending_proposal"]
        if raw_observation is not None and not isinstance(raw_observation, Mapping):
            raise TypeError("queue-reactive observation cutoff must be an object")
        if raw_pending is not None and not isinstance(raw_pending, Mapping):
            raise TypeError("queue-reactive pending proposal must be an object")
        owner.last_observation = (
            None
            if raw_observation is None
            else QueueReactiveObservationCutV1.from_dict(raw_observation)
        )
        owner.pending_proposal = (
            None
            if raw_pending is None
            else QueueReactiveFlowProposalV1.from_dict(raw_pending)
        )
        owner.proposal_sequence = _exact_int(
            payload["proposal_sequence"], "proposal_sequence"
        )
        diagnostics = payload["diagnostic_draw_sequence"]
        if type(diagnostics) is not list or any(
            not isinstance(row, Mapping) for row in diagnostics
        ):
            raise TypeError("queue-reactive diagnostics must be an object array")
        owner.diagnostic_draw_sequence = [dict(row) for row in diagnostics]
        _require_exact_fields(
            raw_rejection,
            {"applied_count", "last_rejection_reason", "rejected_count"},
            "queue-reactive rejection state",
        )
        owner.applied_count = _exact_int(
            raw_rejection["applied_count"], "applied_count"
        )
        owner.rejected_count = _exact_int(
            raw_rejection["rejected_count"], "rejected_count"
        )
        owner.last_rejection_reason = _optional_string(
            raw_rejection["last_rejection_reason"], "last_rejection_reason"
        )
        _require_exact_fields(
            raw_windows,
            {"aggressive_flow", "midpoints_x2", "queue_changes"},
            "queue-reactive retained windows",
        )
        owner.queue_changes = [
            tuple(row)  # type: ignore[misc]
            for row in cls._decode_window(
                raw_windows["queue_changes"],
                field="queue_changes",
                width=5,
            )
        ]
        owner.aggressive_flow = [
            tuple(row)  # type: ignore[misc]
            for row in cls._decode_window(
                raw_windows["aggressive_flow"],
                field="aggressive_flow",
                width=3,
            )
        ]
        owner.midpoints_x2 = [
            tuple(row)  # type: ignore[misc]
            for row in cls._decode_window(
                raw_windows["midpoints_x2"],
                field="midpoints_x2",
                width=2,
            )
        ]
        queue_state = owner._queue_state()
        if queue_state is None:
            if raw_model["last_intensity_inspection"] is not None:
                raise ValueError(
                    "queue-reactive inspection exists without an observation"
                )
        else:
            owner.modifier.inspect_state(owner._baseline_intensities(), queue_state)
            if (
                _encode_queue_inspection(owner.modifier.last_inspection)
                != raw_model["last_intensity_inspection"]
            ):
                raise ValueError(
                    "queue-reactive inspection differs from retained windows"
                )
        owner.assert_invariants(plan)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError(
                "queue-reactive checkpoint is not a canonical fixed point"
            )
        return owner

    def assert_invariants(self, plan: FullDayPlanV1) -> None:
        self._validate_plan_binding(plan)
        if (
            self.modifier.config.profile_id
            != self.configuration.modifier_profile_id
            or self.modifier.config.window_us != self.configuration.window_us
            or self.modifier.config.near_touch_levels
            != self.configuration.near_touch_levels
            or _queue_reactive_profile_sha256(self.modifier.config)
            != self.configuration.modifier_profile_sha256
        ):
            raise RuntimeError(
                "queue-reactive modifier differs from its bound profile"
            )
        if self.rng.seed != plan.seed_policy.derive(self.rng_label):
            raise RuntimeError("queue-reactive RNG differs from the plan substream")
        if self.proposal_sequence < 0:
            raise RuntimeError("queue-reactive proposal sequence is invalid")
        resolved = self.applied_count + self.rejected_count
        pending_count = 0 if self.pending_proposal is None else 1
        if resolved + pending_count != self.proposal_sequence:
            raise RuntimeError("queue-reactive proposal lifecycle is not conserved")
        if self.pending_proposal is not None:
            if (
                self.pending_proposal.proposal_sequence != self.proposal_sequence
                or self.last_observation is None
                or self.pending_proposal.observation_cutoff_us
                != self.last_observation.simulation_time_us
            ):
                raise RuntimeError(
                    "queue-reactive pending proposal differs from its cut"
                )
        cutoff = (
            None
            if self.last_observation is None
            else self.last_observation.simulation_time_us
            - self.configuration.window_us
        )
        for name, records in (
            ("queue changes", self.queue_changes),
            ("aggressive flow", self.aggressive_flow),
            ("midpoints", self.midpoints_x2),
        ):
            if len(records) > QUEUE_REACTIVE_MAX_RETAINED_RECORDS:
                raise RuntimeError(f"queue-reactive {name} exceeds its bound")
            times = [row[0] for row in records]
            if times != sorted(set(times)):
                raise RuntimeError(
                    f"queue-reactive {name} timestamps are not strictly increasing"
                )
            if self.last_observation is None and records:
                raise RuntimeError(
                    f"queue-reactive {name} exists without an observation"
                )
            if cutoff is not None and any(
                time < cutoff
                or time > self.last_observation.simulation_time_us  # type: ignore[union-attr]
                for time in times
            ):
                raise RuntimeError(
                    f"queue-reactive {name} lies outside the retained window"
                )
        expected_inspection: dict[str, object] | None = None
        queue_state = self._queue_state()
        if queue_state is not None:
            verifier = QueueReactiveFlowModifier(self.configuration.modifier_profile)
            expected_inspection = _encode_queue_inspection(
                verifier.inspect_state(self._baseline_intensities(), queue_state)
            )
        if _encode_queue_inspection(self.modifier.last_inspection) != expected_inspection:
            raise RuntimeError(
                "queue-reactive intensity inspection differs from retained state"
            )
        for sequence, row in enumerate(self.diagnostic_draw_sequence, start=1):
            if row.get("draw_sequence") != sequence:
                raise RuntimeError(
                    "queue-reactive diagnostic draw sequence has a gap"
                )
        if self.diagnostic_draw_sequence and (
            self.diagnostic_draw_sequence[-1].get("rng_state_after_sha256")
            != self.rng.state_sha256()
        ):
            raise RuntimeError(
                "queue-reactive diagnostic tail differs from RNG state"
            )
        validate_strict_json(self.checkpoint_state())


class SimpleFlowComponentAdapterV1(FullDayComponentAdapterV1):
    component_id = FLOW_SIMPLE_COMPONENT
    active_predicate = component_configured_predicate(FLOW_SIMPLE_COMPONENT)
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_SIMPLE_COMPONENT}_MODEL_STATE",
                f"{FLOW_SIMPLE_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_SIMPLE_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))
    owned_state_ids = (FLOW_SIMPLE_COMPONENT,)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("simple-flow adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not SimpleFlowOwnerV1:
            raise TypeError("simple-flow adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if type(plan) is not FullDayPlanV1:
            raise ValueError("simple-flow restore requires the exact plan")
        detached = snapshot.as_dict()["state"]
        if not isinstance(detached, Mapping):  # pragma: no cover - snapshot contract
            raise TypeError("simple-flow snapshot state is not an object")
        return SimpleFlowOwnerV1.from_checkpoint_state(detached, plan=plan)


class _ContractOnlyFlowAdapterV1(FullDayComponentAdapterV1):
    """Declaration row for an E2 adapter that is not executable yet."""

    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("flow adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        raise RuntimeError(f"{self.component_id} remains contract-only")

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        raise RuntimeError(f"{self.component_id} remains contract-only")

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        raise RuntimeError(f"{self.component_id} remains contract-only")


class HawkesFlowComponentAdapterV1(_ContractOnlyFlowAdapterV1):
    component_id = FLOW_HAWKES_COMPONENT
    active_predicate = component_configured_predicate(FLOW_HAWKES_COMPONENT)
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_HAWKES_COMPONENT}_MODEL_STATE",
                f"{FLOW_HAWKES_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_HAWKES_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    owned_state_ids = (FLOW_HAWKES_COMPONENT,)


class HawkesFlowComponentAdapterV2(FullDayComponentAdapterV1):
    component_id = FLOW_HAWKES_COMPONENT
    implementation_version = 2
    active_predicate = component_configured_predicate(FLOW_HAWKES_COMPONENT)
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_HAWKES_COMPONENT}_MODEL_STATE",
                f"{FLOW_HAWKES_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_HAWKES_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))
    owned_state_ids = (FLOW_HAWKES_COMPONENT,)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("Hawkes-flow adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not HawkesFlowOwnerV1:
            raise TypeError("Hawkes-flow adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if type(plan) is not FullDayPlanV1:
            raise ValueError("Hawkes-flow restore requires the exact plan")
        detached = snapshot.as_dict()["state"]
        if not isinstance(detached, Mapping):  # pragma: no cover - snapshot contract
            raise TypeError("Hawkes-flow snapshot state is not an object")
        return HawkesFlowOwnerV1.from_checkpoint_state(detached, plan=plan)


class QueueReactiveFlowComponentAdapterV1(_ContractOnlyFlowAdapterV1):
    component_id = FLOW_QUEUE_REACTIVE_COMPONENT
    active_predicate = component_configured_predicate(FLOW_QUEUE_REACTIVE_COMPONENT)
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_MODEL_STATE",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    owned_state_ids = (FLOW_QUEUE_REACTIVE_COMPONENT,)


class QueueReactiveFlowComponentAdapterV2(FullDayComponentAdapterV1):
    component_id = FLOW_QUEUE_REACTIVE_COMPONENT
    implementation_version = 2
    active_predicate = component_configured_predicate(FLOW_QUEUE_REACTIVE_COMPONENT)
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_MODEL_STATE",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))
    owned_state_ids = (FLOW_QUEUE_REACTIVE_COMPONENT,)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("queue-reactive adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not QueueReactiveFlowOwnerV1:
            raise TypeError("queue-reactive adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if type(plan) is not FullDayPlanV1:
            raise ValueError("queue-reactive restore requires the exact plan")
        detached = snapshot.as_dict()["state"]
        if not isinstance(detached, Mapping):  # pragma: no cover - snapshot contract
            raise TypeError("queue-reactive snapshot state is not an object")
        return QueueReactiveFlowOwnerV1.from_checkpoint_state(detached, plan=plan)


__all__ = [
    "FlowObservationCutV1",
    "HAWKES_FLOW_MODEL_ID",
    "HAWKES_FLOW_MODEL_VERSION",
    "HAWKES_FLOW_RNG_LABEL",
    "HawkesFlowComponentAdapterV1",
    "HawkesFlowComponentAdapterV2",
    "HawkesFlowConfigurationV1",
    "HawkesFlowOwnerV1",
    "HawkesFlowProposalV1",
    "SIMPLE_FLOW_MODEL_ID",
    "SIMPLE_FLOW_MODEL_VERSION",
    "SIMPLE_FLOW_RNG_LABEL",
    "SimpleFlowComponentAdapterV1",
    "SimpleFlowConfigurationV1",
    "SimpleFlowOwnerV1",
    "SimpleFlowProposalV1",
    "QUEUE_REACTIVE_FLOW_MODEL_ID",
    "QUEUE_REACTIVE_FLOW_MODEL_VERSION",
    "QUEUE_REACTIVE_FLOW_RNG_LABEL",
    "QueueReactiveFlowComponentAdapterV1",
    "QueueReactiveFlowComponentAdapterV2",
    "QueueReactiveFlowConfigurationV1",
    "QueueReactiveFlowOwnerV1",
    "QueueReactiveFlowProposalV1",
    "QueueReactiveObservationCutV1",
]
