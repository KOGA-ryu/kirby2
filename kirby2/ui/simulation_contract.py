"""Strict V1 setup records shared with the standalone Kirby2 UI.

The public facade returns detached dictionaries containing only canonical JSON
values.  These classes keep validation, identity, and refusal semantics in the
backend; Qt and simulator runtime objects never cross this boundary.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from kirby2.packs.formats import canonical_json_bytes


PROFILE_CATALOG_SCHEMA_ID: Final = "KIRBY2_SIMULATION_PROFILE_CATALOG_V1"
PROFILE_SELECTION_SCHEMA_ID: Final = "KIRBY2_SIMULATION_PROFILE_SELECTION_V1"
TRAINING_RESOURCE_CATALOG_SCHEMA_ID: Final = (
    "KIRBY2_SIMULATION_TRAINING_RESOURCE_CATALOG_V1"
)
RESOLVED_CONFIGURATION_SCHEMA_ID: Final = (
    "KIRBY2_RESOLVED_SIMULATION_CONFIGURATION_V1"
)
PROFILE_RESOLUTION_SCHEMA_ID: Final = "KIRBY2_SIMULATION_PROFILE_RESOLUTION_V1"
ENGINE_CONTRACT_ID: Final = "KIRBY2_SIMULATION_ENGINE_V1"
SCHEMA_VERSION: Final = 1

COMPONENT_KINDS: Final = frozenset(
    {
        "SCENARIO_DEFINITION",
        "REGIME_PROFILE",
        "DISTRIBUTION_BUNDLE",
        "QUEUE_REACTIVE",
        "HAWKES",
        "INTRADAY",
        "HOTKEY_LAYOUT",
        "STRATEGY_DEFINITION",
        "CURRICULUM_DRILL",
        "OBSERVATION_POLICY",
    }
)
ARRIVAL_MODEL_FAMILIES: Final = frozenset({"simple", "hawkes"})
REGIMES: Final = frozenset(
    {
        "BALANCED",
        "BUY_PRESSURE",
        "SELL_PRESSURE",
        "MOMENTUM_UP",
        "MOMENTUM_DOWN",
        "ABSORPTION_BID",
        "ABSORPTION_ASK",
        "THIN_LIQUIDITY",
        "LIQUIDITY_VACUUM",
        "MEAN_REVERSION",
        "HIGH_CANCELLATION",
        "PANIC",
    }
)
INTRADAY_PHASES: Final = frozenset(
    {
        "PREOPEN",
        "OPENING",
        "MORNING",
        "MIDDAY",
        "AFTERNOON",
        "CLOSE",
        "NOT_APPLICABLE",
    }
)
RELATIVE_VOLUMES: Final = (
    "0.25x",
    "0.50x",
    "1.00x",
    "2.00x",
    "5.00x",
    "10.00x",
)
LIQUIDITIES: Final = ("VERY_THIN", "THIN", "NORMAL", "DEEP", "VERY_DEEP")
CONTROL_VALUE_KINDS: Final = frozenset(
    {"INTEGER", "FIXED_POINT", "ENUM", "BOOLEAN"}
)
TRAINING_ACTION_KINDS: Final = frozenset({"PLAYER_ACTION", "LIFECYCLE"})
PLAYER_QUEUE_DISCLOSURES: Final = frozenset({"AVAILABLE", "UNAVAILABLE"})
PROFILE_RESOLUTION_STATUSES: Final = frozenset({"AVAILABLE", "REFUSED"})
RESOLUTION_REFUSAL_REASONS: Final = frozenset(
    {
        "UNKNOWN_PROFILE",
        "PROFILE_DIGEST_MISMATCH",
        "UNSUPPORTED_COMPONENT_COMBINATION",
        "COMPONENT_NOT_FOUND",
        "COMPONENT_DIGEST_MISMATCH",
        "UNKNOWN_CONTROL",
        "CONTROL_VALUE_OUT_OF_RANGE",
        "INVALID_INTRADAY_WINDOW",
        "INVALID_DURATION",
    }
)

_COMPONENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_COMPONENT_FIELDS = frozenset(
    {"component_kind", "component_id", "component_version", "content_sha256"}
)
_PROFILE_REF_FIELDS = frozenset(
    {"profile_id", "profile_version", "profile_sha256"}
)
_CATALOG_FIELDS = frozenset(
    {"schema_id", "schema_version", "catalog_sha256", "profiles"}
)
_PROFILE_FIELDS = frozenset(
    {
        "profile_ref",
        "presentation",
        "engine_contract_id",
        "arrival_model_family",
        "regime",
        "defaults",
        "controls",
        "provenance",
    }
)
_PRESENTATION_FIELDS = frozenset({"display_name", "summary"})
_DEFAULT_FIELDS = frozenset(
    {
        "seed",
        "duration_us",
        "intraday_phase",
        "relative_volume",
        "liquidity",
        "intensity_scale_ppm",
        "scenario_definition_ref",
        "regime_profile_ref",
        "distribution_bundle_ref",
        "queue_reactive_ref",
        "hawkes_ref",
        "intraday_ref",
    }
)
_CONTROL_FIELDS = frozenset(
    {
        "control_id",
        "label",
        "value_kind",
        "scale",
        "default_value",
        "minimum_value",
        "maximum_value",
        "step",
        "unit",
        "options",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {
        "classification",
        "real_market_data",
        "matching_engine_derived",
        "generation_method",
        "level2_origin",
        "display_label",
    }
)
_SELECTION_FIELDS = frozenset(
    {"schema_id", "schema_version", "profile_ref", "seed", "duration_us", "control_values"}
)
_TRAINING_CATALOG_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "catalog_sha256",
        "layouts",
        "observation_policies",
        "strategies",
        "curriculum_drills",
        "defaults",
    }
)
_LAYOUT_FIELDS = frozenset({"layout_ref", "presentation", "actions"})
_LAYOUT_ACTION_FIELDS = frozenset(
    {"semantic_action_id", "action_kind", "display_label", "bound_key"}
)
_OBSERVATION_POLICY_FIELDS = frozenset(
    {"policy_ref", "presentation", "player_queue_disclosure"}
)
_STRATEGY_FIELDS = frozenset({"strategy_ref", "presentation"})
_CURRICULUM_DRILL_FIELDS = frozenset({"curriculum_drill_ref", "presentation"})
_TRAINING_DEFAULT_FIELDS = frozenset(
    {"layout_ref", "observation_policy_ref", "quantity_options", "initial_quantity"}
)
_RESOLVED_CONFIGURATION_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "profile_ref",
        "selection_sha256",
        "engine_contract_id",
        "seed",
        "duration_us",
        "arrival_model_family",
        "regime",
        "intraday_phase",
        "relative_volume",
        "liquidity",
        "intensity_scale_ppm",
        "scenario_definition_ref",
        "regime_profile_ref",
        "distribution_bundle_ref",
        "queue_reactive_ref",
        "hawkes_ref",
        "intraday_ref",
        "effective_control_values",
    }
)
_PROFILE_RESOLUTION_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "status",
        "selection",
        "selection_sha256",
        "resolved_configuration_sha256",
        "resolved_configuration",
        "refusal",
    }
)
_REFUSAL_FIELDS = frozenset({"reason_code", "explanation"})


class SimulationContractDecodeError(ValueError):
    """An input cannot be decoded as the exact V1 wire schema."""


class SimulationContractIntegrityError(ValueError):
    """A well-shaped record has inconsistent content-derived identity."""


class SimulationResolutionRefusal(Exception):
    """Private control-flow exception converted into a typed refusal record."""

    def __init__(self, reason_code: str, explanation: str) -> None:
        super().__init__(explanation)
        self.reason_code = reason_code
        self.explanation = explanation


def canonical_digest(value: object) -> str:
    """Return the V1 lowercase SHA-256 over canonical semantic JSON."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot(
    value: object,
    path: str = "$",
    active_container_ids: set[int] | None = None,
) -> object:
    active = set() if active_container_ids is None else active_container_ids
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"simulation contract contains a cycle at {path}")
        active.add(identity)
        try:
            detached: dict[str, object] = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise TypeError(f"simulation contract key at {path} is not text")
                _normalized_text(key, f"{path}.<key>")
                if key in detached:
                    raise ValueError(
                        f"simulation contract contains a duplicate key at {path}.{key}"
                    )
                detached[key] = _snapshot(child, f"{path}.{key}", active)
        finally:
            active.remove(identity)
        return detached
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError(f"simulation contract contains a cycle at {path}")
        active.add(identity)
        try:
            return [
                _snapshot(child, f"{path}[{index}]", active)
                for index, child in enumerate(value)
            ]
        finally:
            active.remove(identity)
    if type(value) is str:
        _normalized_text(value, path)
        return value
    if value is None or type(value) in {bool, int}:
        return value
    raise TypeError(f"simulation contract contains non-canonical JSON at {path}")


def _normalized_text(value: str, path: str) -> None:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"simulation contract contains non-NFC text at {path}")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"simulation contract contains a surrogate at {path}")


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a list")
    return value


def _exact(mapping: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(mapping) != expected:
        raise ValueError(f"{label} fields differ from the V1 contract")


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"{label} must be text")
    return value


def _identifier(value: object, label: str) -> str:
    result = _text(value, label)
    if result.strip() != result:
        raise ValueError(f"{label} must not have outer whitespace")
    return result


def _component_identifier(value: object, label: str) -> str:
    result = _text(value, label)
    if _COMPONENT_ID.fullmatch(result) is None:
        raise ValueError(f"{label} must be a canonical component identifier")
    return result


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _positive_integer(value: object, label: str) -> int:
    return _integer(value, label, minimum=1)


def _power_of_ten(value: object, label: str) -> int:
    result = _positive_integer(value, label)
    candidate = result
    while candidate > 1 and candidate % 10 == 0:
        candidate //= 10
    if candidate != 1:
        raise ValueError(f"{label} must be a positive power of ten")
    return result


def _enum(value: object, allowed: frozenset[str] | tuple[str, ...], label: str) -> str:
    result = _text(value, label)
    if result not in allowed:
        raise ValueError(f"{label} is not a supported V1 value")
    return result


def _digest(value: object, label: str) -> str:
    result = _text(value, label)
    if _SHA256.fullmatch(result) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _freeze(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if type(value) is list:
        return tuple(_freeze(child) for child in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if type(value) is tuple:
        return [_plain(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class SimulationComponentRefV1:
    component_kind: str
    component_id: str
    component_version: int
    content_sha256: str

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        expected_kind: str | None = None,
        label: str = "simulation component reference",
    ) -> SimulationComponentRefV1:
        value = _object(_snapshot(payload), label)
        _exact(value, _COMPONENT_FIELDS, label)
        kind = _enum(value["component_kind"], COMPONENT_KINDS, f"{label}.component_kind")
        if expected_kind is not None and kind != expected_kind:
            raise ValueError(f"{label} component kind must be {expected_kind}")
        return cls(
            component_kind=kind,
            component_id=_component_identifier(value["component_id"], f"{label}.component_id"),
            component_version=_positive_integer(
                value["component_version"], f"{label}.component_version"
            ),
            content_sha256=_digest(value["content_sha256"], f"{label}.content_sha256"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "component_kind": self.component_kind,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class SimulationProfileRefV1:
    profile_id: str
    profile_version: int
    profile_sha256: str

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        label: str = "simulation profile reference",
    ) -> SimulationProfileRefV1:
        value = _object(_snapshot(payload), label)
        _exact(value, _PROFILE_REF_FIELDS, label)
        return cls(
            profile_id=_identifier(value["profile_id"], f"{label}.profile_id"),
            profile_version=_positive_integer(
                value["profile_version"], f"{label}.profile_version"
            ),
            profile_sha256=_digest(value["profile_sha256"], f"{label}.profile_sha256"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_sha256": self.profile_sha256,
        }


def _component_ref(
    value: object, label: str, expected_kind: str
) -> SimulationComponentRefV1:
    return SimulationComponentRefV1.from_dict(
        _object(value, label), expected_kind=expected_kind, label=label
    )


def _optional_component_ref(
    value: object, label: str, expected_kind: str
) -> SimulationComponentRefV1 | None:
    if value is None:
        return None
    return _component_ref(value, label, expected_kind)


def _validate_control(value: object, label: str) -> dict[str, object]:
    control = _object(value, label)
    _exact(control, _CONTROL_FIELDS, label)
    control_id = _identifier(control["control_id"], f"{label}.control_id")
    value_kind = _enum(
        control["value_kind"], CONTROL_VALUE_KINDS, f"{label}.value_kind"
    )
    scale = _power_of_ten(control["scale"], f"{label}.scale")
    options = [
        _text(item, f"{label}.options[{index}]", allow_empty=True)
        for index, item in enumerate(_array(control["options"], f"{label}.options"))
    ]
    if value_kind in {"INTEGER", "FIXED_POINT"}:
        default = _integer(control["default_value"], f"{label}.default_value")
        minimum = _integer(control["minimum_value"], f"{label}.minimum_value")
        maximum = _integer(control["maximum_value"], f"{label}.maximum_value")
        step = _positive_integer(control["step"], f"{label}.step")
        if options:
            raise ValueError(f"{label}.options must be empty for numeric controls")
        if minimum > maximum or not minimum <= default <= maximum:
            raise ValueError(f"{label} numeric bounds or default are invalid")
        if (default - minimum) % step:
            raise ValueError(f"{label} default does not align to its minimum-based step")
    elif value_kind == "ENUM":
        if scale != 1:
            raise ValueError(f"{label}.scale must be 1 for ENUM")
        default = _text(control["default_value"], f"{label}.default_value", allow_empty=True)
        if not options or default not in options:
            raise ValueError(f"{label} ENUM default must be one of its options")
        if any(control[field] is not None for field in ("minimum_value", "maximum_value", "step")):
            raise ValueError(f"{label} ENUM numeric fields must be null")
        minimum = maximum = step = None
    else:
        if scale != 1 or type(control["default_value"]) is not bool:
            raise ValueError(f"{label} BOOLEAN scale or default is invalid")
        default = control["default_value"]
        if options or any(
            control[field] is not None for field in ("minimum_value", "maximum_value", "step")
        ):
            raise ValueError(f"{label} BOOLEAN options and numeric fields must be empty")
        minimum = maximum = step = None
    return {
        "control_id": control_id,
        "label": _text(control["label"], f"{label}.label", allow_empty=True),
        "value_kind": value_kind,
        "scale": scale,
        "default_value": default,
        "minimum_value": minimum,
        "maximum_value": maximum,
        "step": step,
        "unit": _text(control["unit"], f"{label}.unit", allow_empty=True),
        "options": options,
    }


def _validate_control_value(
    control: Mapping[str, object], value: object, label: str
) -> int | str | bool:
    value_kind = str(control["value_kind"])
    if value_kind in {"INTEGER", "FIXED_POINT"}:
        result = _integer(value, label)
        minimum = int(control["minimum_value"])
        maximum = int(control["maximum_value"])
        step = int(control["step"])
        if not minimum <= result <= maximum or (result - minimum) % step:
            raise ValueError(f"{label} is outside its minimum-based numeric grid")
        return result
    if value_kind == "ENUM":
        result = _text(value, label)
        if result not in control["options"]:
            raise ValueError(f"{label} is not a catalog-authored option")
        return result
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _validate_defaults(value: object, label: str) -> dict[str, object]:
    defaults = _object(value, label)
    _exact(defaults, _DEFAULT_FIELDS, label)
    refs = {
        "scenario_definition_ref": _component_ref(
            defaults["scenario_definition_ref"],
            f"{label}.scenario_definition_ref",
            "SCENARIO_DEFINITION",
        ),
        "regime_profile_ref": _component_ref(
            defaults["regime_profile_ref"],
            f"{label}.regime_profile_ref",
            "REGIME_PROFILE",
        ),
        "distribution_bundle_ref": _component_ref(
            defaults["distribution_bundle_ref"],
            f"{label}.distribution_bundle_ref",
            "DISTRIBUTION_BUNDLE",
        ),
        "queue_reactive_ref": _optional_component_ref(
            defaults["queue_reactive_ref"],
            f"{label}.queue_reactive_ref",
            "QUEUE_REACTIVE",
        ),
        "hawkes_ref": _optional_component_ref(
            defaults["hawkes_ref"], f"{label}.hawkes_ref", "HAWKES"
        ),
        "intraday_ref": _optional_component_ref(
            defaults["intraday_ref"], f"{label}.intraday_ref", "INTRADAY"
        ),
    }
    return {
        "seed": _integer(defaults["seed"], f"{label}.seed", minimum=0),
        "duration_us": _positive_integer(defaults["duration_us"], f"{label}.duration_us"),
        "intraday_phase": _enum(defaults["intraday_phase"], INTRADAY_PHASES, f"{label}.intraday_phase"),
        "relative_volume": _enum(defaults["relative_volume"], RELATIVE_VOLUMES, f"{label}.relative_volume"),
        "liquidity": _enum(defaults["liquidity"], LIQUIDITIES, f"{label}.liquidity"),
        "intensity_scale_ppm": _integer(
            defaults["intensity_scale_ppm"], f"{label}.intensity_scale_ppm", minimum=0
        ),
        **{
            field: None if ref is None else ref.as_dict()
            for field, ref in refs.items()
        },
    }


def _profile_semantics(profile: Mapping[str, object]) -> dict[str, object]:
    profile_ref = _object(profile["profile_ref"], "profile.profile_ref")
    controls = _array(profile["controls"], "profile.controls")
    return {
        "schema_id": "KIRBY2_SIMULATION_PROFILE_SEMANTICS_V1",
        "schema_version": 1,
        "profile_id": profile_ref["profile_id"],
        "profile_version": profile_ref["profile_version"],
        "engine_contract_id": profile["engine_contract_id"],
        "arrival_model_family": profile["arrival_model_family"],
        "regime": profile["regime"],
        "defaults": profile["defaults"],
        "controls": [
            {
                field: _object(control, f"profile.controls[{index}]")[field]
                for field in (
                    "control_id",
                    "value_kind",
                    "scale",
                    "default_value",
                    "minimum_value",
                    "maximum_value",
                    "step",
                    "unit",
                    "options",
                )
            }
            for index, control in enumerate(controls)
        ],
    }


def _validate_profile(value: object, label: str) -> dict[str, object]:
    profile = _object(value, label)
    _exact(profile, _PROFILE_FIELDS, label)
    profile_ref = SimulationProfileRefV1.from_dict(
        _object(profile["profile_ref"], f"{label}.profile_ref"),
        label=f"{label}.profile_ref",
    )
    presentation = _object(profile["presentation"], f"{label}.presentation")
    _exact(presentation, _PRESENTATION_FIELDS, f"{label}.presentation")
    engine_contract_id = _text(profile["engine_contract_id"], f"{label}.engine_contract_id")
    if engine_contract_id != ENGINE_CONTRACT_ID:
        raise ValueError(f"{label}.engine_contract_id is unsupported")
    arrival_model_family = _enum(
        profile["arrival_model_family"], ARRIVAL_MODEL_FAMILIES, f"{label}.arrival_model_family"
    )
    defaults = _validate_defaults(profile["defaults"], f"{label}.defaults")
    if arrival_model_family == "simple" and defaults["hawkes_ref"] is not None:
        raise ValueError(f"{label} simple profile must not carry a Hawkes reference")
    if arrival_model_family == "hawkes" and defaults["hawkes_ref"] is None:
        raise ValueError(f"{label} Hawkes profile requires a Hawkes reference")
    if (defaults["intraday_phase"] == "NOT_APPLICABLE") != (defaults["intraday_ref"] is None):
        raise ValueError(f"{label} intraday phase and component reference disagree")
    controls = [
        _validate_control(item, f"{label}.controls[{index}]")
        for index, item in enumerate(_array(profile["controls"], f"{label}.controls"))
    ]
    control_ids = [str(control["control_id"]) for control in controls]
    if len(control_ids) != len(set(control_ids)):
        raise ValueError(f"{label} control IDs must be unique")
    provenance = _object(profile["provenance"], f"{label}.provenance")
    _exact(provenance, _PROVENANCE_FIELDS, f"{label}.provenance")
    expected_provenance: dict[str, object] = {
        "classification": "SYNTHETIC_SIMULATION_ONLY",
        "real_market_data": False,
        "matching_engine_derived": True,
        "generation_method": "ORDER_FLOW_THROUGH_MATCHING_ENGINE",
        "level2_origin": "MATCHING_ENGINE_BOOK_STATE",
    }
    if any(
        provenance[field] != expected or type(provenance[field]) is not type(expected)
        for field, expected in expected_provenance.items()
    ):
        raise ValueError(f"{label}.provenance violates the synthetic-only contract")
    normalized = {
        "profile_ref": profile_ref.as_dict(),
        "presentation": {
            "display_name": _text(presentation["display_name"], f"{label}.presentation.display_name", allow_empty=True),
            "summary": _text(presentation["summary"], f"{label}.presentation.summary", allow_empty=True),
        },
        "engine_contract_id": engine_contract_id,
        "arrival_model_family": arrival_model_family,
        "regime": _enum(profile["regime"], REGIMES, f"{label}.regime"),
        "defaults": defaults,
        "controls": controls,
        "provenance": {
            **expected_provenance,
            "display_label": _text(
                provenance["display_label"], f"{label}.provenance.display_label", allow_empty=True
            ),
        },
    }
    if profile_ref.profile_sha256 != canonical_digest(_profile_semantics(normalized)):
        raise SimulationContractIntegrityError(
            f"{label} profile digest does not match its semantics"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class SimulationProfileCatalogV1:
    schema_id: str
    schema_version: int
    catalog_sha256: str
    profiles: tuple[Mapping[str, object], ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationProfileCatalogV1:
        root = _object(_snapshot(payload), "simulation profile catalog")
        _exact(root, _CATALOG_FIELDS, "simulation profile catalog")
        if (
            root["schema_id"] != PROFILE_CATALOG_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("simulation profile catalog schema is unsupported")
        profiles = [
            _validate_profile(item, f"simulation profile catalog.profiles[{index}]")
            for index, item in enumerate(_array(root["profiles"], "simulation profile catalog.profiles"))
        ]
        if not profiles:
            raise ValueError("simulation profile catalog must not be empty")
        refs = [SimulationProfileRefV1.from_dict(profile["profile_ref"]) for profile in profiles]
        labels = [(ref.profile_id, ref.profile_version) for ref in refs]
        if len(labels) != len(set(labels)):
            raise ValueError("simulation profile catalog IDs and versions must be unique")
        expected_digest = canonical_digest(
            {
                "schema_id": PROFILE_CATALOG_SCHEMA_ID,
                "schema_version": 1,
                "profiles": profiles,
            }
        )
        catalog_sha256 = _digest(root["catalog_sha256"], "simulation profile catalog.catalog_sha256")
        if catalog_sha256 != expected_digest:
            raise SimulationContractIntegrityError(
                "simulation profile catalog digest does not match its record"
            )
        return cls(
            PROFILE_CATALOG_SCHEMA_ID,
            1,
            catalog_sha256,
            tuple(_freeze(profile) for profile in profiles),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "catalog_sha256": self.catalog_sha256,
            "profiles": [_plain(profile) for profile in self.profiles],
        }

    def profile_for_ref(self, ref: SimulationProfileRefV1) -> Mapping[str, object]:
        same_label = [
            profile
            for profile in self.profiles
            if _object(_plain(profile["profile_ref"]), "profile reference")["profile_id"] == ref.profile_id
            and _object(_plain(profile["profile_ref"]), "profile reference")["profile_version"] == ref.profile_version
        ]
        if not same_label:
            raise SimulationResolutionRefusal(
                "UNKNOWN_PROFILE", "The selected simulation profile is not in this catalog."
            )
        for profile in same_label:
            if SimulationProfileRefV1.from_dict(profile["profile_ref"]) == ref:
                return profile
        raise SimulationResolutionRefusal(
            "PROFILE_DIGEST_MISMATCH",
            "The selected simulation profile digest differs from this catalog.",
        )

    def validate_selection(
        self, selection: SimulationProfileSelectionV1
    ) -> Mapping[str, object]:
        profile = self.profile_for_ref(selection.profile_ref)
        controls = tuple(profile["controls"])
        expected_ids = {str(control["control_id"]) for control in controls}
        values = dict(selection.control_values)
        if set(values) != expected_ids:
            raise SimulationResolutionRefusal(
                "UNKNOWN_CONTROL",
                "Selection controls differ from the selected catalog profile.",
            )
        try:
            for control in controls:
                control_id = str(control["control_id"])
                _validate_control_value(
                    control,
                    values[control_id],
                    f"profile selection.control_values.{control_id}",
                )
        except (TypeError, ValueError) as error:
            raise SimulationResolutionRefusal(
                "CONTROL_VALUE_OUT_OF_RANGE", str(error)
            ) from error
        return profile


def _scalar_control_values(value: object, label: str) -> tuple[tuple[str, int | str | bool], ...]:
    mapping = _object(value, label)
    result: dict[str, int | str | bool] = {}
    for raw_id, raw_value in mapping.items():
        control_id = _identifier(raw_id, f"{label}.<control_id>")
        if type(raw_value) not in {bool, int, str}:
            raise ValueError(f"{label}.{control_id} must be an integer, string, or boolean")
        result[control_id] = raw_value
    return tuple((control_id, result[control_id]) for control_id in sorted(result))


@dataclass(frozen=True, slots=True)
class SimulationProfileSelectionV1:
    schema_id: str
    schema_version: int
    profile_ref: SimulationProfileRefV1
    seed: int
    duration_us: int
    control_values: tuple[tuple[str, int | str | bool], ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationProfileSelectionV1:
        root = _object(_snapshot(payload), "simulation profile selection")
        _exact(root, _SELECTION_FIELDS, "simulation profile selection")
        if (
            root["schema_id"] != PROFILE_SELECTION_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("simulation profile selection schema is unsupported")
        return cls(
            PROFILE_SELECTION_SCHEMA_ID,
            1,
            SimulationProfileRefV1.from_dict(
                _object(root["profile_ref"], "simulation profile selection.profile_ref"),
                label="simulation profile selection.profile_ref",
            ),
            _integer(root["seed"], "simulation profile selection.seed", minimum=0),
            _positive_integer(root["duration_us"], "simulation profile selection.duration_us"),
            _scalar_control_values(root["control_values"], "simulation profile selection.control_values"),
        )

    @property
    def selection_sha256(self) -> str:
        return canonical_digest(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "profile_ref": self.profile_ref.as_dict(),
            "seed": self.seed,
            "duration_us": self.duration_us,
            "control_values": dict(self.control_values),
        }


def _validate_presentation(value: object, label: str) -> dict[str, object]:
    presentation = _object(value, label)
    _exact(presentation, _PRESENTATION_FIELDS, label)
    return {
        "display_name": _text(presentation["display_name"], f"{label}.display_name", allow_empty=True),
        "summary": _text(presentation["summary"], f"{label}.summary", allow_empty=True),
    }


def _validate_layout(value: object, label: str) -> dict[str, object]:
    layout = _object(value, label)
    _exact(layout, _LAYOUT_FIELDS, label)
    ref = _component_ref(layout["layout_ref"], f"{label}.layout_ref", "HOTKEY_LAYOUT")
    actions: list[dict[str, object]] = []
    for index, item in enumerate(_array(layout["actions"], f"{label}.actions")):
        action_label = f"{label}.actions[{index}]"
        action = _object(item, action_label)
        _exact(action, _LAYOUT_ACTION_FIELDS, action_label)
        raw_key = action["bound_key"]
        bound_key = None if raw_key is None else _identifier(raw_key, f"{action_label}.bound_key")
        actions.append(
            {
                "semantic_action_id": _identifier(action["semantic_action_id"], f"{action_label}.semantic_action_id"),
                "action_kind": _enum(action["action_kind"], TRAINING_ACTION_KINDS, f"{action_label}.action_kind"),
                "display_label": _text(action["display_label"], f"{action_label}.display_label", allow_empty=True),
                "bound_key": bound_key,
            }
        )
    if not actions:
        raise ValueError(f"{label}.actions must not be empty")
    action_ids = [str(action["semantic_action_id"]) for action in actions]
    if len(action_ids) != len(set(action_ids)):
        raise ValueError(f"{label} action IDs must be unique")
    for required in ("SIMULATION_PLAY", "SIMULATION_PAUSE"):
        matching = [action for action in actions if action["semantic_action_id"] == required]
        if len(matching) != 1 or matching[0]["action_kind"] != "LIFECYCLE":
            raise ValueError(f"{label} must expose {required} as a lifecycle action")
    by_key: dict[str, list[dict[str, object]]] = {}
    for action in actions:
        key = action["bound_key"]
        if type(key) is str:
            by_key.setdefault(key, []).append(action)
    for key, bound_actions in by_key.items():
        if len(bound_actions) == 1:
            continue
        is_toggle = (
            len(bound_actions) == 2
            and {str(action["semantic_action_id"]) for action in bound_actions}
            == {"SIMULATION_PLAY", "SIMULATION_PAUSE"}
            and all(action["action_kind"] == "LIFECYCLE" for action in bound_actions)
        )
        if not is_toggle:
            raise ValueError(f"{label}.actions contains invalid duplicate bound_key {key!r}")
    return {
        "layout_ref": ref.as_dict(),
        "presentation": _validate_presentation(layout["presentation"], f"{label}.presentation"),
        "actions": actions,
    }


def _validate_resource_row(
    value: object,
    label: str,
    *,
    ref_field: str,
    expected_kind: str,
) -> dict[str, object]:
    row = _object(value, label)
    expected_fields = _STRATEGY_FIELDS if ref_field == "strategy_ref" else (
        _CURRICULUM_DRILL_FIELDS if ref_field == "curriculum_drill_ref" else _OBSERVATION_POLICY_FIELDS
    )
    _exact(row, expected_fields, label)
    result = {
        ref_field: _component_ref(row[ref_field], f"{label}.{ref_field}", expected_kind).as_dict(),
        "presentation": _validate_presentation(row["presentation"], f"{label}.presentation"),
    }
    if ref_field == "policy_ref":
        result["player_queue_disclosure"] = _enum(
            row["player_queue_disclosure"],
            PLAYER_QUEUE_DISCLOSURES,
            f"{label}.player_queue_disclosure",
        )
    return result


@dataclass(frozen=True, slots=True)
class SimulationTrainingResourceCatalogV1:
    schema_id: str
    schema_version: int
    catalog_sha256: str
    layouts: tuple[Mapping[str, object], ...]
    observation_policies: tuple[Mapping[str, object], ...]
    strategies: tuple[Mapping[str, object], ...]
    curriculum_drills: tuple[Mapping[str, object], ...]
    defaults: Mapping[str, object]

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> SimulationTrainingResourceCatalogV1:
        root = _object(_snapshot(payload), "simulation training resource catalog")
        _exact(root, _TRAINING_CATALOG_FIELDS, "simulation training resource catalog")
        if (
            root["schema_id"] != TRAINING_RESOURCE_CATALOG_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("simulation training resource catalog schema is unsupported")
        layouts = [
            _validate_layout(item, f"simulation training resource catalog.layouts[{index}]")
            for index, item in enumerate(_array(root["layouts"], "simulation training resource catalog.layouts"))
        ]
        policies = [
            _validate_resource_row(
                item,
                f"simulation training resource catalog.observation_policies[{index}]",
                ref_field="policy_ref",
                expected_kind="OBSERVATION_POLICY",
            )
            for index, item in enumerate(
                _array(root["observation_policies"], "simulation training resource catalog.observation_policies")
            )
        ]
        strategies = [
            _validate_resource_row(
                item,
                f"simulation training resource catalog.strategies[{index}]",
                ref_field="strategy_ref",
                expected_kind="STRATEGY_DEFINITION",
            )
            for index, item in enumerate(_array(root["strategies"], "simulation training resource catalog.strategies"))
        ]
        drills = [
            _validate_resource_row(
                item,
                f"simulation training resource catalog.curriculum_drills[{index}]",
                ref_field="curriculum_drill_ref",
                expected_kind="CURRICULUM_DRILL",
            )
            for index, item in enumerate(
                _array(root["curriculum_drills"], "simulation training resource catalog.curriculum_drills")
            )
        ]
        if not layouts or not policies:
            raise ValueError("training catalog requires a layout and observation policy")
        for rows, ref_field, label in (
            (layouts, "layout_ref", "layouts"),
            (policies, "policy_ref", "observation policies"),
            (strategies, "strategy_ref", "strategies"),
            (drills, "curriculum_drill_ref", "curriculum drills"),
        ):
            refs = [SimulationComponentRefV1.from_dict(row[ref_field]) for row in rows]
            ids = [ref.component_id for ref in refs]
            if len(ids) != len(set(ids)) or len(refs) != len(set(refs)):
                raise ValueError(f"training catalog {label} must have unique IDs and references")
        defaults = _object(root["defaults"], "simulation training resource catalog.defaults")
        _exact(defaults, _TRAINING_DEFAULT_FIELDS, "simulation training resource catalog.defaults")
        layout_ref = _component_ref(defaults["layout_ref"], "training defaults.layout_ref", "HOTKEY_LAYOUT")
        policy_ref = _component_ref(
            defaults["observation_policy_ref"],
            "training defaults.observation_policy_ref",
            "OBSERVATION_POLICY",
        )
        if all(SimulationComponentRefV1.from_dict(row["layout_ref"]) != layout_ref for row in layouts):
            raise ValueError("default layout reference is not an exact catalog row")
        if all(SimulationComponentRefV1.from_dict(row["policy_ref"]) != policy_ref for row in policies):
            raise ValueError("default observation policy reference is not an exact catalog row")
        quantities = [
            _positive_integer(item, f"training defaults.quantity_options[{index}]")
            for index, item in enumerate(_array(defaults["quantity_options"], "training defaults.quantity_options"))
        ]
        if not quantities or any(left >= right for left, right in zip(quantities, quantities[1:])):
            raise ValueError("training quantity options must be strictly ascending and unique")
        initial_quantity = _positive_integer(defaults["initial_quantity"], "training defaults.initial_quantity")
        if initial_quantity not in quantities:
            raise ValueError("initial quantity must be an advertised option")
        normalized_defaults = {
            "layout_ref": layout_ref.as_dict(),
            "observation_policy_ref": policy_ref.as_dict(),
            "quantity_options": quantities,
            "initial_quantity": initial_quantity,
        }
        basis = {
            "schema_id": TRAINING_RESOURCE_CATALOG_SCHEMA_ID,
            "schema_version": 1,
            "layouts": layouts,
            "observation_policies": policies,
            "strategies": strategies,
            "curriculum_drills": drills,
            "defaults": normalized_defaults,
        }
        catalog_sha256 = _digest(root["catalog_sha256"], "training catalog.catalog_sha256")
        if catalog_sha256 != canonical_digest(basis):
            raise SimulationContractIntegrityError(
                "training resource catalog digest does not match its record"
            )
        return cls(
            TRAINING_RESOURCE_CATALOG_SCHEMA_ID,
            1,
            catalog_sha256,
            tuple(_freeze(row) for row in layouts),
            tuple(_freeze(row) for row in policies),
            tuple(_freeze(row) for row in strategies),
            tuple(_freeze(row) for row in drills),
            _freeze(normalized_defaults),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "catalog_sha256": self.catalog_sha256,
            "layouts": [_plain(row) for row in self.layouts],
            "observation_policies": [_plain(row) for row in self.observation_policies],
            "strategies": [_plain(row) for row in self.strategies],
            "curriculum_drills": [_plain(row) for row in self.curriculum_drills],
            "defaults": _plain(self.defaults),
        }


@dataclass(frozen=True, slots=True)
class ResolvedSimulationConfigurationV1:
    profile_ref: SimulationProfileRefV1
    selection_sha256: str
    engine_contract_id: str
    seed: int
    duration_us: int
    arrival_model_family: str
    regime: str
    intraday_phase: str
    relative_volume: str
    liquidity: str
    intensity_scale_ppm: int
    scenario_definition_ref: SimulationComponentRefV1
    regime_profile_ref: SimulationComponentRefV1
    distribution_bundle_ref: SimulationComponentRefV1
    queue_reactive_ref: SimulationComponentRefV1 | None
    hawkes_ref: SimulationComponentRefV1 | None
    intraday_ref: SimulationComponentRefV1 | None
    effective_control_values: tuple[tuple[str, int | str | bool], ...]

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ResolvedSimulationConfigurationV1:
        root = _object(_snapshot(payload), "resolved simulation configuration")
        _exact(root, _RESOLVED_CONFIGURATION_FIELDS, "resolved simulation configuration")
        if (
            root["schema_id"] != RESOLVED_CONFIGURATION_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("resolved simulation configuration schema is unsupported")
        return cls(
            profile_ref=SimulationProfileRefV1.from_dict(_object(root["profile_ref"], "resolved profile ref")),
            selection_sha256=_digest(root["selection_sha256"], "resolved selection_sha256"),
            engine_contract_id=_text(root["engine_contract_id"], "resolved engine_contract_id"),
            seed=_integer(root["seed"], "resolved seed", minimum=0),
            duration_us=_positive_integer(root["duration_us"], "resolved duration_us"),
            arrival_model_family=_enum(root["arrival_model_family"], ARRIVAL_MODEL_FAMILIES, "resolved arrival model"),
            regime=_enum(root["regime"], REGIMES, "resolved regime"),
            intraday_phase=_enum(root["intraday_phase"], INTRADAY_PHASES, "resolved intraday phase"),
            relative_volume=_enum(root["relative_volume"], RELATIVE_VOLUMES, "resolved relative volume"),
            liquidity=_enum(root["liquidity"], LIQUIDITIES, "resolved liquidity"),
            intensity_scale_ppm=_integer(root["intensity_scale_ppm"], "resolved intensity scale", minimum=0),
            scenario_definition_ref=_component_ref(root["scenario_definition_ref"], "resolved scenario ref", "SCENARIO_DEFINITION"),
            regime_profile_ref=_component_ref(root["regime_profile_ref"], "resolved regime ref", "REGIME_PROFILE"),
            distribution_bundle_ref=_component_ref(root["distribution_bundle_ref"], "resolved distribution ref", "DISTRIBUTION_BUNDLE"),
            queue_reactive_ref=_optional_component_ref(root["queue_reactive_ref"], "resolved queue ref", "QUEUE_REACTIVE"),
            hawkes_ref=_optional_component_ref(root["hawkes_ref"], "resolved Hawkes ref", "HAWKES"),
            intraday_ref=_optional_component_ref(root["intraday_ref"], "resolved intraday ref", "INTRADAY"),
            effective_control_values=_scalar_control_values(
                root["effective_control_values"], "resolved effective_control_values"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": RESOLVED_CONFIGURATION_SCHEMA_ID,
            "schema_version": 1,
            "profile_ref": self.profile_ref.as_dict(),
            "selection_sha256": self.selection_sha256,
            "engine_contract_id": self.engine_contract_id,
            "seed": self.seed,
            "duration_us": self.duration_us,
            "arrival_model_family": self.arrival_model_family,
            "regime": self.regime,
            "intraday_phase": self.intraday_phase,
            "relative_volume": self.relative_volume,
            "liquidity": self.liquidity,
            "intensity_scale_ppm": self.intensity_scale_ppm,
            "scenario_definition_ref": self.scenario_definition_ref.as_dict(),
            "regime_profile_ref": self.regime_profile_ref.as_dict(),
            "distribution_bundle_ref": self.distribution_bundle_ref.as_dict(),
            "queue_reactive_ref": None if self.queue_reactive_ref is None else self.queue_reactive_ref.as_dict(),
            "hawkes_ref": None if self.hawkes_ref is None else self.hawkes_ref.as_dict(),
            "intraday_ref": None if self.intraday_ref is None else self.intraday_ref.as_dict(),
            "effective_control_values": dict(self.effective_control_values),
        }

    def validate_against(
        self,
        selection: SimulationProfileSelectionV1,
        profile: Mapping[str, object],
    ) -> None:
        profile_ref = SimulationProfileRefV1.from_dict(profile["profile_ref"])
        if self.profile_ref != selection.profile_ref or self.profile_ref != profile_ref:
            raise SimulationContractIntegrityError("resolved profile differs from the selection")
        if self.selection_sha256 != selection.selection_sha256:
            raise SimulationContractIntegrityError("resolved selection digest differs")
        if self.seed != selection.seed or self.duration_us != selection.duration_us:
            raise SimulationContractIntegrityError("resolved seed or duration differs from selection")
        if self.duration_us % 1_000_000:
            raise SimulationContractIntegrityError("available duration is not a whole second")
        if self.engine_contract_id != profile["engine_contract_id"]:
            raise SimulationContractIntegrityError("resolved engine contract differs")
        if self.arrival_model_family != profile["arrival_model_family"] or self.regime != profile["regime"]:
            raise SimulationContractIntegrityError("resolved arrival family or regime differs")
        defaults = profile["defaults"]
        exact_refs = (
            (self.scenario_definition_ref, defaults["scenario_definition_ref"]),
            (self.regime_profile_ref, defaults["regime_profile_ref"]),
            (self.distribution_bundle_ref, defaults["distribution_bundle_ref"]),
            (self.queue_reactive_ref, defaults["queue_reactive_ref"]),
            (self.hawkes_ref, defaults["hawkes_ref"]),
            (self.intraday_ref, defaults["intraday_ref"]),
        )
        for actual, expected in exact_refs:
            expected_ref = None if expected is None else SimulationComponentRefV1.from_dict(expected)
            if actual != expected_ref:
                raise SimulationContractIntegrityError("resolved component reference differs from profile")
        if self.effective_control_values != selection.control_values:
            raise SimulationContractIntegrityError("resolved controls differ from selection")
        values = dict(selection.control_values)
        expected_volume = values.get("relative_volume", defaults["relative_volume"])
        expected_liquidity = values.get("liquidity", defaults["liquidity"])
        expected_intensity = values.get("intensity_scale_ppm", defaults["intensity_scale_ppm"])
        if (
            self.relative_volume != expected_volume
            or self.liquidity != expected_liquidity
            or self.intensity_scale_ppm != expected_intensity
        ):
            raise SimulationContractIntegrityError("resolved scalar effects differ from controls")
        if self.intraday_phase != defaults["intraday_phase"]:
            raise SimulationContractIntegrityError("resolved intraday phase differs from profile")


@dataclass(frozen=True, slots=True)
class SimulationResolutionRefusalV1:
    reason_code: str
    explanation: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationResolutionRefusalV1:
        root = _object(_snapshot(payload), "simulation resolution refusal")
        _exact(root, _REFUSAL_FIELDS, "simulation resolution refusal")
        return cls(
            _enum(root["reason_code"], RESOLUTION_REFUSAL_REASONS, "refusal reason_code"),
            _text(root["explanation"], "refusal explanation"),
        )

    def as_dict(self) -> dict[str, object]:
        return {"reason_code": self.reason_code, "explanation": self.explanation}


@dataclass(frozen=True, slots=True)
class SimulationProfileResolutionV1:
    status: str
    selection: SimulationProfileSelectionV1
    selection_sha256: str
    resolved_configuration_sha256: str | None
    resolved_configuration: ResolvedSimulationConfigurationV1 | None
    refusal: SimulationResolutionRefusalV1 | None

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        catalog: SimulationProfileCatalogV1,
    ) -> SimulationProfileResolutionV1:
        root = _object(_snapshot(payload), "simulation profile resolution")
        _exact(root, _PROFILE_RESOLUTION_FIELDS, "simulation profile resolution")
        if (
            root["schema_id"] != PROFILE_RESOLUTION_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("simulation profile resolution schema is unsupported")
        status = _enum(root["status"], PROFILE_RESOLUTION_STATUSES, "resolution status")
        selection = SimulationProfileSelectionV1.from_dict(
            _object(root["selection"], "resolution selection")
        )
        selection_sha256 = _digest(root["selection_sha256"], "resolution selection_sha256")
        if selection_sha256 != selection.selection_sha256:
            raise SimulationContractIntegrityError("resolution selection digest does not match")
        if status == "AVAILABLE":
            if root["resolved_configuration_sha256"] is None or root["resolved_configuration"] is None or root["refusal"] is not None:
                raise ValueError("AVAILABLE resolution has invalid nullability")
            profile = catalog.validate_selection(selection)
            configuration_record = _object(root["resolved_configuration"], "resolved configuration")
            configuration = ResolvedSimulationConfigurationV1.from_dict(configuration_record)
            configuration.validate_against(selection, profile)
            configuration_sha256 = _digest(
                root["resolved_configuration_sha256"], "resolved configuration sha256"
            )
            if configuration_sha256 != canonical_digest(configuration_record):
                raise SimulationContractIntegrityError("resolved configuration digest does not match")
            refusal = None
        else:
            if root["resolved_configuration_sha256"] is not None or root["resolved_configuration"] is not None or root["refusal"] is None:
                raise ValueError("REFUSED resolution has invalid nullability")
            configuration = None
            configuration_sha256 = None
            refusal = SimulationResolutionRefusalV1.from_dict(
                _object(root["refusal"], "resolution refusal")
            )
        return cls(
            status,
            selection,
            selection_sha256,
            configuration_sha256,
            configuration,
            refusal,
        )

    @property
    def available(self) -> bool:
        return self.status == "AVAILABLE"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": PROFILE_RESOLUTION_SCHEMA_ID,
            "schema_version": 1,
            "status": self.status,
            "selection": self.selection.as_dict(),
            "selection_sha256": self.selection_sha256,
            "resolved_configuration_sha256": self.resolved_configuration_sha256,
            "resolved_configuration": None if self.resolved_configuration is None else self.resolved_configuration.as_dict(),
            "refusal": None if self.refusal is None else self.refusal.as_dict(),
        }


__all__ = [
    "ARRIVAL_MODEL_FAMILIES",
    "COMPONENT_KINDS",
    "ENGINE_CONTRACT_ID",
    "LIQUIDITIES",
    "PROFILE_CATALOG_SCHEMA_ID",
    "PROFILE_RESOLUTION_SCHEMA_ID",
    "PROFILE_SELECTION_SCHEMA_ID",
    "RESOLUTION_REFUSAL_REASONS",
    "RESOLVED_CONFIGURATION_SCHEMA_ID",
    "RELATIVE_VOLUMES",
    "SCHEMA_VERSION",
    "TRAINING_RESOURCE_CATALOG_SCHEMA_ID",
    "ResolvedSimulationConfigurationV1",
    "SimulationComponentRefV1",
    "SimulationContractDecodeError",
    "SimulationContractIntegrityError",
    "SimulationProfileCatalogV1",
    "SimulationProfileRefV1",
    "SimulationProfileResolutionV1",
    "SimulationProfileSelectionV1",
    "SimulationResolutionRefusalV1",
    "SimulationTrainingResourceCatalogV1",
    "canonical_digest",
]
