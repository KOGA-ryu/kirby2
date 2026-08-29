"""Immutable V1 contracts for the declarative scenario source language.

The source model is intentionally not a runtime plan.  It records typed authoring
intent; target adapters later compile that intent into one of Kirby2's existing
native plan/configuration/recording contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from math import gcd
from types import MappingProxyType


SCENARIO_SOURCE_SCHEMA_VERSION = 1
SCENARIO_PLAN_ENVELOPE_SCHEMA_VERSION = 1

SCENARIO_SOURCE_SECTION_NAMES = (
    "metadata",
    "market_profile",
    "instrument",
    "venues",
    "session_schedule",
    "flow_model",
    "regimes",
    "day_local_states",
    "volume",
    "liquidity",
    "latency",
    "agent_populations",
    "scheduled_events",
    "unscheduled_events",
    "transition_rules",
    "historical_constraints",
    "player_objective",
    "strategy",
    "curriculum_metadata",
    "reveal_policy",
    "checkpoint_policy",
    "seed_policy",
    "accepted_behavioral_envelopes",
    "required_source_capabilities",
)
SCENARIO_BEHAVIOR_SECTION_NAMES = SCENARIO_SOURCE_SECTION_NAMES[1:]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")


def _validate_text(value: object, context: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{context} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{context} must not be empty")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{context} must be NFC-normalized")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{context} must not contain surrogate code points")
    return value


def _validate_identifier(value: object, context: str) -> str:
    text = _validate_text(value, context)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ValueError(f"{context} is not a canonical identifier")
    return text


def _validate_sha256(value: object, context: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return value


class ScenarioTargetKindV1(str, Enum):
    """The closed V1 set of native Kirby2 scenario targets."""

    FULL_DAY_PLAN_V1 = "FULL_DAY_PLAN_V1"
    MARKET_SCENARIO_V1 = "MARKET_SCENARIO_V1"
    HIDDEN_LIQUIDITY_RECORDING_V1 = "HIDDEN_LIQUIDITY_RECORDING_V1"
    MULTIVENUE_RECORDING_V1 = "MULTIVENUE_RECORDING_V1"
    HISTORICAL_LESSON_V1 = "HISTORICAL_LESSON_V1"


class ScenarioDefinitionTypeV1(str, Enum):
    """The closed set of reusable definition namespaces in source V1."""

    MARKET = "market"
    VENUE = "venue"
    LATENCY = "latency"
    AGENT_POPULATION = "agent_population"
    REGIME = "regime"
    HISTORICAL_SOURCE = "historical_source"
    OBJECTIVE_TEMPLATE = "objective_template"


DEFINITION_SECTION_BY_TYPE_V1: Mapping[ScenarioDefinitionTypeV1, str] = (
    MappingProxyType(
        {
            ScenarioDefinitionTypeV1.MARKET: "market_profile",
            ScenarioDefinitionTypeV1.VENUE: "venues",
            ScenarioDefinitionTypeV1.LATENCY: "latency",
            ScenarioDefinitionTypeV1.AGENT_POPULATION: "agent_populations",
            ScenarioDefinitionTypeV1.REGIME: "regimes",
            ScenarioDefinitionTypeV1.HISTORICAL_SOURCE: "historical_constraints",
            ScenarioDefinitionTypeV1.OBJECTIVE_TEMPLATE: "player_objective",
        }
    )
)
DEFINITION_TYPE_BY_SECTION_V1: Mapping[str, ScenarioDefinitionTypeV1] = (
    MappingProxyType(
        {
            section: definition_type
            for definition_type, section in DEFINITION_SECTION_BY_TYPE_V1.items()
        }
    )
)


class ScenarioListMergeModeV1(str, Enum):
    REPLACE = "REPLACE"
    KEYED_MERGE = "KEYED_MERGE"


@dataclass(frozen=True, slots=True)
class ScenarioDefinitionMergePolicyV1:
    definition_type: ScenarioDefinitionTypeV1
    scalar_mode: str
    identifier_list_mode: ScenarioListMergeModeV1

    def __post_init__(self) -> None:
        if type(self.definition_type) is not ScenarioDefinitionTypeV1:
            raise TypeError("definition merge policy requires a V1 definition type")
        if self.scalar_mode != "KEYED_OVERRIDE":
            raise ValueError("V1 definition scalar merge must be KEYED_OVERRIDE")
        if type(self.identifier_list_mode) is not ScenarioListMergeModeV1:
            raise TypeError("definition list merge mode uses the wrong contract")


DEFINITION_MERGE_POLICIES_V1: Mapping[
    ScenarioDefinitionTypeV1, ScenarioDefinitionMergePolicyV1
] = MappingProxyType(
    {
        definition_type: ScenarioDefinitionMergePolicyV1(
            definition_type,
            "KEYED_OVERRIDE",
            list_mode,
        )
        for definition_type, list_mode in (
            (
                ScenarioDefinitionTypeV1.MARKET,
                ScenarioListMergeModeV1.REPLACE,
            ),
            (
                ScenarioDefinitionTypeV1.VENUE,
                ScenarioListMergeModeV1.KEYED_MERGE,
            ),
            (
                ScenarioDefinitionTypeV1.LATENCY,
                ScenarioListMergeModeV1.REPLACE,
            ),
            (
                ScenarioDefinitionTypeV1.AGENT_POPULATION,
                ScenarioListMergeModeV1.KEYED_MERGE,
            ),
            (
                ScenarioDefinitionTypeV1.REGIME,
                ScenarioListMergeModeV1.REPLACE,
            ),
            (
                ScenarioDefinitionTypeV1.HISTORICAL_SOURCE,
                ScenarioListMergeModeV1.REPLACE,
            ),
            (
                ScenarioDefinitionTypeV1.OBJECTIVE_TEMPLATE,
                ScenarioListMergeModeV1.KEYED_MERGE,
            ),
        )
    }
)


class ScenarioSourceOriginV1(str, Enum):
    SOURCE_ROOT = "SOURCE_ROOT"
    PACK_NAMESPACE = "PACK_NAMESPACE"


@dataclass(frozen=True, slots=True)
class ScenarioTargetContractV1:
    target_kind: ScenarioTargetKindV1
    target_version: int
    adapter_id: str
    adapter_version: int

    def __post_init__(self) -> None:
        if type(self.target_kind) is not ScenarioTargetKindV1:
            raise TypeError("scenario target contract requires a V1 target kind")
        if type(self.target_version) is not int or self.target_version != 1:
            raise ValueError("scenario target version must be exactly 1")
        _validate_identifier(self.adapter_id, "scenario adapter ID")
        if type(self.adapter_version) is not int or self.adapter_version != 1:
            raise ValueError("scenario adapter version must be exactly 1")


SCENARIO_TARGET_CONTRACTS_V1: Mapping[
    ScenarioTargetKindV1, ScenarioTargetContractV1
] = MappingProxyType(
    {
        kind: ScenarioTargetContractV1(kind, 1, adapter_id, 1)
        for kind, adapter_id in (
            (
                ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
                "KIRBY2_FULL_DAY_PLAN_ADAPTER_V1",
            ),
            (
                ScenarioTargetKindV1.MARKET_SCENARIO_V1,
                "KIRBY2_MARKET_SCENARIO_ADAPTER_V1",
            ),
            (
                ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
                "KIRBY2_HIDDEN_LIQUIDITY_RECORDING_ADAPTER_V1",
            ),
            (
                ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
                "KIRBY2_MULTIVENUE_RECORDING_ADAPTER_V1",
            ),
            (
                ScenarioTargetKindV1.HISTORICAL_LESSON_V1,
                "KIRBY2_HISTORICAL_LESSON_ADAPTER_V1",
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class VolumeMultiplierV1:
    """One positive reduced rational volume multiplier."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if type(self.numerator) is not int or self.numerator <= 0:
            raise ValueError("volume multiplier numerator must be a positive integer")
        if type(self.denominator) is not int or self.denominator <= 0:
            raise ValueError("volume multiplier denominator must be a positive integer")
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError("volume multiplier must be a reduced rational")

    def as_dict(self) -> dict[str, int]:
        return {
            "denominator": self.denominator,
            "numerator": self.numerator,
        }


@dataclass(frozen=True, slots=True)
class ExactFixedPointV1:
    """A tagged, reduced base-10 fixed-point value for dimensionless metrics."""

    coefficient: int
    scale: int
    unit: str

    def __post_init__(self) -> None:
        if type(self.coefficient) is not int:
            raise TypeError("fixed-point coefficient must be an integer")
        if type(self.scale) is not int or self.scale <= 0:
            raise ValueError("fixed-point scale must be a positive integer")
        value = self.scale
        while value > 1 and value % 10 == 0:
            value //= 10
        if value != 1:
            raise ValueError("fixed-point scale must be a power of ten")
        if self.scale > 1 and self.coefficient % 10 == 0:
            raise ValueError("fixed-point values must use their reduced scale")
        _validate_identifier(self.unit, "fixed-point unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "coefficient": self.coefficient,
            "scale": self.scale,
            "unit": self.unit,
        }


class ScenarioValueKindV1(str, Enum):
    """Closed tags for source values; there is no implicit numeric value tag."""

    TEXT = "text"
    FLAG = "flag"
    IDENTIFIER = "identifier"
    IDENTIFIERS = "identifiers"
    DURATION_MS = "duration_ms"
    PRICE_TICKS = "price_ticks"
    QUANTITY_SHARES = "quantity_shares"
    RATE_PER_SECOND = "rate_per_second"
    LATENCY_US = "latency_us"
    VOLUME_MULTIPLIER = "volume_multiplier"
    PROBABILITY_WEIGHT = "probability_weight"
    COUNT = "count"
    INDEX = "index"
    SEED = "seed"
    VERSION = "version"
    FIXED_POINT = "fixed_point"


@dataclass(frozen=True, slots=True)
class ScenarioFieldV1:
    """One named, explicitly tagged source value."""

    name: str
    value_kind: ScenarioValueKindV1
    value: object

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "scenario field name")
        if type(self.value_kind) is not ScenarioValueKindV1:
            raise TypeError("scenario field value kind must use ScenarioValueKindV1")
        kind = self.value_kind
        value = self.value
        if kind is ScenarioValueKindV1.TEXT:
            _validate_text(value, "scenario text value", allow_empty=True)
        elif kind is ScenarioValueKindV1.FLAG:
            if type(value) is not bool:
                raise TypeError("scenario flag value must be a bool")
        elif kind is ScenarioValueKindV1.IDENTIFIER:
            _validate_identifier(value, "scenario identifier value")
        elif kind is ScenarioValueKindV1.IDENTIFIERS:
            if type(value) not in {tuple, list}:
                raise TypeError("scenario identifiers value must be an array")
            normalized = tuple(value)
            for item in normalized:
                _validate_identifier(item, "scenario identifiers item")
            if len(normalized) != len(set(normalized)):
                raise ValueError("scenario identifier arrays must be unique")
            object.__setattr__(self, "value", tuple(sorted(normalized)))
        elif kind is ScenarioValueKindV1.VOLUME_MULTIPLIER:
            if type(value) is not VolumeMultiplierV1:
                raise TypeError("volume_multiplier requires VolumeMultiplierV1")
        elif kind is ScenarioValueKindV1.FIXED_POINT:
            if type(value) is not ExactFixedPointV1:
                raise TypeError("fixed_point requires ExactFixedPointV1")
        else:
            if type(value) is not int:
                raise TypeError(f"{kind.value} must be an integer")
            if kind in {
                ScenarioValueKindV1.DURATION_MS,
                ScenarioValueKindV1.QUANTITY_SHARES,
                ScenarioValueKindV1.RATE_PER_SECOND,
                ScenarioValueKindV1.LATENCY_US,
                ScenarioValueKindV1.COUNT,
                ScenarioValueKindV1.INDEX,
                ScenarioValueKindV1.SEED,
            } and value < 0:
                raise ValueError(f"{kind.value} must be nonnegative")
            if kind in {
                ScenarioValueKindV1.PROBABILITY_WEIGHT,
                ScenarioValueKindV1.VERSION,
            } and value <= 0:
                raise ValueError(f"{kind.value} must be positive")

    def as_dict(self) -> dict[str, object]:
        value = self.value
        if type(value) in {VolumeMultiplierV1, ExactFixedPointV1}:
            value = value.as_dict()
        elif type(value) is tuple:
            value = list(value)
        return {"name": self.name, self.value_kind.value: value}

    def semantic_dict(self) -> dict[str, object]:
        """Return the normalized behavior projection for this source value."""

        if self.value_kind is ScenarioValueKindV1.DURATION_MS:
            return {"duration_us": int(self.value) * 1_000, "name": self.name}
        return self.as_dict()


@dataclass(frozen=True, slots=True)
class ScenarioRecordV1:
    """A target-neutral named declaration inside one source section."""

    logical_name: str
    record_type: str
    version: int
    fields: tuple[ScenarioFieldV1, ...]
    reference: str | None = None
    extends: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.logical_name, "scenario record logical name")
        _validate_identifier(self.record_type, "scenario record type")
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("scenario record version must be a positive integer")
        if type(self.fields) is not tuple or any(
            type(item) is not ScenarioFieldV1 for item in self.fields
        ):
            raise TypeError("scenario record fields must be a typed immutable tuple")
        names = tuple(item.name for item in self.fields)
        if len(names) != len(set(names)):
            raise ValueError("scenario record field names must be unique")
        object.__setattr__(
            self,
            "fields",
            tuple(sorted(self.fields, key=lambda item: item.name)),
        )
        if self.reference is not None:
            _validate_identifier(self.reference, "scenario record reference")
        if self.extends is not None:
            _validate_identifier(self.extends, "scenario record inheritance reference")

    def as_dict(self, *, semantic: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "fields": [
                field.semantic_dict() if semantic else field.as_dict()
                for field in self.fields
            ],
            "logical_name": self.logical_name,
            "record_type": self.record_type,
            "version": self.version,
        }
        if self.extends is not None:
            payload["extends"] = self.extends
        if self.reference is not None:
            payload["reference"] = self.reference
        return payload


@dataclass(frozen=True, slots=True)
class ScenarioSectionV1:
    """One required source section with a canonical named-record namespace."""

    records: tuple[ScenarioRecordV1, ...]

    def __post_init__(self) -> None:
        if type(self.records) is not tuple or any(
            type(item) is not ScenarioRecordV1 for item in self.records
        ):
            raise TypeError("scenario section records must be a typed immutable tuple")
        names = tuple(item.logical_name for item in self.records)
        if len(names) != len(set(names)):
            raise ValueError("scenario section logical names must be unique")
        object.__setattr__(
            self,
            "records",
            tuple(sorted(self.records, key=lambda item: item.logical_name)),
        )

    def as_dict(self, *, semantic: bool = False) -> dict[str, object]:
        return {
            "records": [record.as_dict(semantic=semantic) for record in self.records]
        }


@dataclass(frozen=True, slots=True)
class ScenarioMetadataV1:
    scenario_id: str
    scenario_version: int
    title: str
    description: str
    target_kind: ScenarioTargetKindV1
    target_version: int
    adapter_id: str
    adapter_version: int
    capability_digest: str

    def __post_init__(self) -> None:
        _validate_identifier(self.scenario_id, "scenario ID")
        if type(self.scenario_version) is not int or self.scenario_version <= 0:
            raise ValueError("scenario version must be a positive integer")
        _validate_text(self.title, "scenario title")
        _validate_text(self.description, "scenario description", allow_empty=True)
        if type(self.target_kind) is not ScenarioTargetKindV1:
            raise TypeError("scenario target kind must use ScenarioTargetKindV1")
        expected = SCENARIO_TARGET_CONTRACTS_V1[self.target_kind]
        if (
            type(self.target_version) is not int
            or self.target_version != expected.target_version
            or self.adapter_id != expected.adapter_id
            or type(self.adapter_version) is not int
            or self.adapter_version != expected.adapter_version
        ):
            raise ValueError("scenario metadata does not match the closed target contract")
        _validate_sha256(self.capability_digest, "scenario capability digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capability_digest": self.capability_digest,
            "description": self.description,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "target_kind": self.target_kind.value,
            "target_version": self.target_version,
            "title": self.title,
        }

    def semantic_dict(self) -> dict[str, object]:
        """Exclude presentation-only title/description from behavioral identity."""

        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capability_digest": self.capability_digest,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "target_kind": self.target_kind.value,
            "target_version": self.target_version,
        }


@dataclass(frozen=True, slots=True)
class ScenarioSourceV1:
    """Complete, strict V1 authoring document before import resolution/compilation."""

    schema_version: int
    metadata: ScenarioMetadataV1
    market_profile: ScenarioSectionV1
    instrument: ScenarioSectionV1
    venues: ScenarioSectionV1
    session_schedule: ScenarioSectionV1
    flow_model: ScenarioSectionV1
    regimes: ScenarioSectionV1
    day_local_states: ScenarioSectionV1
    volume: ScenarioSectionV1
    liquidity: ScenarioSectionV1
    latency: ScenarioSectionV1
    agent_populations: ScenarioSectionV1
    scheduled_events: ScenarioSectionV1
    unscheduled_events: ScenarioSectionV1
    transition_rules: ScenarioSectionV1
    historical_constraints: ScenarioSectionV1
    player_objective: ScenarioSectionV1
    strategy: ScenarioSectionV1
    curriculum_metadata: ScenarioSectionV1
    reveal_policy: ScenarioSectionV1
    checkpoint_policy: ScenarioSectionV1
    seed_policy: ScenarioSectionV1
    accepted_behavioral_envelopes: ScenarioSectionV1
    required_source_capabilities: ScenarioSectionV1

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SCENARIO_SOURCE_SCHEMA_VERSION
        ):
            raise ValueError("scenario source schema version must be exactly 1")
        if type(self.metadata) is not ScenarioMetadataV1:
            raise TypeError("scenario source metadata must use ScenarioMetadataV1")
        for name in SCENARIO_BEHAVIOR_SECTION_NAMES:
            if type(getattr(self, name)) is not ScenarioSectionV1:
                raise TypeError(f"scenario source section {name!r} has the wrong contract")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "metadata": self.metadata.as_dict(),
            "schema_version": self.schema_version,
        }
        payload.update(
            {
                name: getattr(self, name).as_dict()
                for name in SCENARIO_BEHAVIOR_SECTION_NAMES
            }
        )
        return payload

    def semantic_projection(self) -> dict[str, object]:
        """Return canonical normalized behavior, excluding source presentation."""

        payload: dict[str, object] = {
            "metadata": self.metadata.semantic_dict(),
            "schema_version": self.schema_version,
        }
        payload.update(
            {
                name: getattr(self, name).as_dict(semantic=True)
                for name in SCENARIO_BEHAVIOR_SECTION_NAMES
            }
        )
        return payload


ScenarioSource = ScenarioSourceV1


@dataclass(frozen=True, slots=True)
class ScenarioImportV1:
    """One lexical source import before confinement resolution."""

    path: str
    pack_namespace: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.path, "scenario import path")
        if "\x00" in self.path:
            raise ValueError("scenario import path must not contain NUL")
        if self.pack_namespace is not None:
            _validate_identifier(self.pack_namespace, "scenario pack namespace")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"path": self.path}
        if self.pack_namespace is not None:
            payload["pack_namespace"] = self.pack_namespace
        return payload


@dataclass(frozen=True, slots=True)
class ScenarioSourceDocumentV1:
    """One parsed document with stable, relocation-independent provenance."""

    logical_path: str
    origin: ScenarioSourceOriginV1
    pack_namespace: str | None
    source: ScenarioSourceV1
    imports: tuple[ScenarioImportV1, ...]
    raw_sha256: str
    raw_byte_count: int

    def __post_init__(self) -> None:
        _validate_text(self.logical_path, "scenario document logical path")
        if "\x00" in self.logical_path:
            raise ValueError("scenario document logical path must not contain NUL")
        if type(self.origin) is not ScenarioSourceOriginV1:
            raise TypeError("scenario document origin uses the wrong contract")
        if self.origin is ScenarioSourceOriginV1.SOURCE_ROOT:
            if self.pack_namespace is not None:
                raise ValueError("source-root document cannot claim a pack namespace")
        elif self.pack_namespace is None:
            raise ValueError("pack document requires its activated namespace")
        else:
            _validate_identifier(self.pack_namespace, "scenario pack namespace")
        if type(self.source) is not ScenarioSourceV1:
            raise TypeError("scenario document source uses the wrong contract")
        if type(self.imports) is not tuple or any(
            type(item) is not ScenarioImportV1 for item in self.imports
        ):
            raise TypeError("scenario document imports must be an immutable tuple")
        _validate_sha256(self.raw_sha256, "scenario document raw digest")
        if type(self.raw_byte_count) is not int or self.raw_byte_count <= 0:
            raise ValueError("scenario document byte count must be positive")

    def provenance_dict(self) -> dict[str, object]:
        return {
            "imports": [item.as_dict() for item in self.imports],
            "logical_path": self.logical_path,
            "origin": self.origin.value,
            "pack_namespace": self.pack_namespace,
            "raw_byte_count": self.raw_byte_count,
            "raw_sha256": self.raw_sha256,
        }


@dataclass(frozen=True, slots=True)
class ScenarioImportEdgeV1:
    importer_logical_path: str
    imported_logical_path: str
    import_ordinal: int

    def __post_init__(self) -> None:
        _validate_text(self.importer_logical_path, "scenario importer logical path")
        _validate_text(self.imported_logical_path, "scenario imported logical path")
        if type(self.import_ordinal) is not int or self.import_ordinal < 0:
            raise ValueError("scenario import ordinal must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "import_ordinal": self.import_ordinal,
            "imported_logical_path": self.imported_logical_path,
            "importer_logical_path": self.importer_logical_path,
        }


@dataclass(frozen=True, slots=True)
class ScenarioImportBundleV1:
    """Complete ordered import graph and raw-byte identity."""

    root_logical_path: str
    documents: tuple[ScenarioSourceDocumentV1, ...]
    edges: tuple[ScenarioImportEdgeV1, ...]
    source_bundle_digest: str
    expanded_byte_count: int

    def __post_init__(self) -> None:
        _validate_text(self.root_logical_path, "scenario root logical path")
        if not self.documents or type(self.documents) is not tuple or any(
            type(item) is not ScenarioSourceDocumentV1 for item in self.documents
        ):
            raise TypeError("scenario import bundle requires typed documents")
        logical_paths = tuple(item.logical_path for item in self.documents)
        if logical_paths[0] != self.root_logical_path:
            raise ValueError("scenario root document must be first in import order")
        if len(logical_paths) != len(set(logical_paths)):
            raise ValueError("scenario import bundle document paths must be unique")
        if type(self.edges) is not tuple or any(
            type(item) is not ScenarioImportEdgeV1 for item in self.edges
        ):
            raise TypeError("scenario import graph edges must be an immutable tuple")
        known = set(logical_paths)
        if any(
            edge.importer_logical_path not in known
            or edge.imported_logical_path not in known
            for edge in self.edges
        ):
            raise ValueError("scenario import edge references an unknown document")
        _validate_sha256(self.source_bundle_digest, "scenario source bundle digest")
        if (
            type(self.expanded_byte_count) is not int
            or self.expanded_byte_count
            != sum(item.raw_byte_count for item in self.documents)
        ):
            raise ValueError("scenario expanded byte count differs from its documents")

    @property
    def root_source(self) -> ScenarioSourceV1:
        return self.documents[0].source

    def provenance_dict(self) -> dict[str, object]:
        return {
            "documents": [item.provenance_dict() for item in self.documents],
            "edges": [item.as_dict() for item in self.edges],
            "expanded_byte_count": self.expanded_byte_count,
            "root_logical_path": self.root_logical_path,
            "source_bundle_digest": self.source_bundle_digest,
        }


@dataclass(frozen=True, slots=True)
class ResolvedScenarioDefinitionV1:
    definition_type: ScenarioDefinitionTypeV1
    qualified_name: str
    source_logical_path: str
    inheritance_chain: tuple[str, ...]
    record: ScenarioRecordV1

    def __post_init__(self) -> None:
        if type(self.definition_type) is not ScenarioDefinitionTypeV1:
            raise TypeError("resolved definition uses the wrong type contract")
        expected = f"{self.definition_type.value}:{self.record.logical_name}"
        if self.qualified_name != expected:
            raise ValueError("resolved definition qualified name is inconsistent")
        _validate_text(self.source_logical_path, "definition source logical path")
        if type(self.inheritance_chain) is not tuple or any(
            type(item) is not str or not item for item in self.inheritance_chain
        ):
            raise TypeError("definition inheritance chain must be a string tuple")
        if len(self.inheritance_chain) != len(set(self.inheritance_chain)):
            raise ValueError("definition inheritance chain must not contain a cycle")
        if type(self.record) is not ScenarioRecordV1 or self.record.extends is not None:
            raise ValueError("resolved definition must contain a flattened record")

    def semantic_dict(self) -> dict[str, object]:
        return {
            "definition_type": self.definition_type.value,
            "qualified_name": self.qualified_name,
            "record": self.record.as_dict(semantic=True),
        }

    def provenance_dict(self) -> dict[str, object]:
        return {
            "inheritance_chain": list(self.inheritance_chain),
            "qualified_name": self.qualified_name,
            "source_logical_path": self.source_logical_path,
        }


@dataclass(frozen=True, slots=True)
class ResolvedScenarioBundleV1:
    """Relocation-stable resolved definitions plus separate source provenance."""

    import_bundle: ScenarioImportBundleV1
    definitions: tuple[ResolvedScenarioDefinitionV1, ...]

    def __post_init__(self) -> None:
        if type(self.import_bundle) is not ScenarioImportBundleV1:
            raise TypeError("resolved scenario bundle requires an import bundle")
        if type(self.definitions) is not tuple or any(
            type(item) is not ResolvedScenarioDefinitionV1
            for item in self.definitions
        ):
            raise TypeError("resolved scenario definitions must be an immutable tuple")
        names = tuple(item.qualified_name for item in self.definitions)
        if names != tuple(sorted(set(names))):
            raise ValueError("resolved scenario definitions must be unique and sorted")

    @property
    def root_source(self) -> ScenarioSourceV1:
        return self.import_bundle.root_source

    def definition(self, qualified_name: str) -> ResolvedScenarioDefinitionV1:
        for definition in self.definitions:
            if definition.qualified_name == qualified_name:
                return definition
        raise KeyError(f"unknown scenario definition: {qualified_name}")

    def semantic_projection(self) -> dict[str, object]:
        root = self.root_source.semantic_projection()
        for section_name in DEFINITION_TYPE_BY_SECTION_V1:
            root[section_name] = {"records": []}
        return {
            "resolved_definitions": [
                definition.semantic_dict() for definition in self.definitions
            ],
            "root_source": root,
        }

    def provenance_projection(self) -> dict[str, object]:
        return {
            "definition_resolution": [
                definition.provenance_dict() for definition in self.definitions
            ],
            "import_bundle": self.import_bundle.provenance_dict(),
        }


@dataclass(frozen=True, slots=True, init=False)
class ScenarioPlanEnvelopeV1:
    """Immutable tagged envelope around one existing native Kirby2 target.

    Only canonical native payload bytes are retained.  Accessing ``payload`` creates
    a fresh instance of the native class, so mutable dictionaries inside older
    native contracts cannot mutate the envelope.
    """

    schema_version: int
    target_kind: ScenarioTargetKindV1
    target_version: int
    adapter_id: str
    adapter_version: int
    capability_digest: str
    _payload_bytes: bytes

    def __init__(
        self,
        *,
        target_kind: ScenarioTargetKindV1 | str,
        payload: object,
        capability_digest: str,
        target_version: int = 1,
        adapter_id: str | None = None,
        adapter_version: int = 1,
        schema_version: int = SCENARIO_PLAN_ENVELOPE_SCHEMA_VERSION,
    ) -> None:
        if (
            type(schema_version) is not int
            or schema_version != SCENARIO_PLAN_ENVELOPE_SCHEMA_VERSION
        ):
            raise ValueError("scenario plan envelope schema version must be exactly 1")
        try:
            kind = ScenarioTargetKindV1(target_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported scenario target kind") from error
        contract = SCENARIO_TARGET_CONTRACTS_V1[kind]
        selected_adapter = contract.adapter_id if adapter_id is None else adapter_id
        if (
            type(target_version) is not int
            or target_version != contract.target_version
            or selected_adapter != contract.adapter_id
            or type(adapter_version) is not int
            or adapter_version != contract.adapter_version
        ):
            raise ValueError("scenario plan envelope target contract does not match its tag")
        _validate_sha256(capability_digest, "scenario envelope capability digest")
        payload_bytes = canonical_native_payload_bytes(kind, payload)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "target_kind", kind)
        object.__setattr__(self, "target_version", target_version)
        object.__setattr__(self, "adapter_id", selected_adapter)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "capability_digest", capability_digest)
        object.__setattr__(self, "_payload_bytes", payload_bytes)

    @property
    def payload(self) -> object:
        return _native_payload_from_bytes(self.target_kind, self._payload_bytes)

    @property
    def native_plan_digest(self) -> str:
        return hashlib.sha256(self._payload_bytes).hexdigest()

    def as_dict(self) -> dict[str, object]:
        payload = json.loads(self._payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict):  # defensive; native validation guarantees it
            raise AssertionError("native scenario payload ceased to be an object")
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capability_digest": self.capability_digest,
            "payload": payload,
            "schema_version": self.schema_version,
            "target_kind": self.target_kind.value,
            "target_version": self.target_version,
        }

    def semantic_projection(self) -> dict[str, object]:
        return self.as_dict()

    def canonical_bytes(self) -> bytes:
        return _canonical_native_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScenarioPlanEnvelopeV1:
        _require_exact_fields(
            payload,
            {
                "adapter_id",
                "adapter_version",
                "capability_digest",
                "payload",
                "schema_version",
                "target_kind",
                "target_version",
            },
            "scenario plan envelope",
        )
        raw_payload = payload["payload"]
        if not isinstance(raw_payload, Mapping):
            raise TypeError("scenario plan envelope payload must be an object")
        if type(payload["target_kind"]) is not str:
            raise TypeError("scenario plan envelope target kind must be a string")
        try:
            kind = ScenarioTargetKindV1(payload["target_kind"])
        except ValueError as error:
            raise ValueError("unsupported scenario target kind") from error
        native_payload = _native_payload_from_mapping(kind, raw_payload)
        return cls(
            schema_version=_exact_int(payload, "schema_version"),
            target_kind=kind,
            target_version=_exact_int(payload, "target_version"),
            adapter_id=_exact_str(payload, "adapter_id"),
            adapter_version=_exact_int(payload, "adapter_version"),
            capability_digest=_exact_str(payload, "capability_digest"),
            payload=native_payload,
        )


def canonical_native_payload_bytes(
    target_kind: ScenarioTargetKindV1 | str,
    payload: object,
) -> bytes:
    """Validate one exact native target type and return canonical payload bytes."""

    try:
        kind = ScenarioTargetKindV1(target_kind)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported scenario target kind") from error
    expected_type = _native_payload_type(kind)
    if type(payload) is not expected_type:
        raise TypeError(
            f"{kind.value} requires exact native payload type {expected_type.__name__}"
        )
    as_dict = getattr(payload, "as_dict", None)
    if not callable(as_dict):
        raise TypeError("native scenario payload lacks its canonical serializer")
    mapping = as_dict()
    if not isinstance(mapping, Mapping):
        raise TypeError("native scenario payload serializer must return an object")
    canonical = _canonical_native_json_bytes(mapping)
    restored = _native_payload_from_bytes(kind, canonical)
    restored_mapping = getattr(restored, "as_dict")()
    if _canonical_native_json_bytes(restored_mapping) != canonical:
        raise ValueError("native scenario payload does not round trip canonically")
    return canonical


def _native_payload_type(target_kind: ScenarioTargetKindV1) -> type[object]:
    if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
        from kirby2.full_day.models import FullDayPlanV1

        return FullDayPlanV1
    if target_kind is ScenarioTargetKindV1.MARKET_SCENARIO_V1:
        from kirby2.scenarios.market import ScenarioDefinition

        return ScenarioDefinition
    if target_kind is ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1:
        from kirby2.observability.replay import ObservabilityRecording

        return ObservabilityRecording
    if target_kind is ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1:
        from kirby2.multivenue.replay import MultiVenueRecording

        return MultiVenueRecording
    if target_kind is ScenarioTargetKindV1.HISTORICAL_LESSON_V1:
        from kirby2.historical.lesson_models import HistoricalLesson

        return HistoricalLesson
    raise AssertionError(f"unhandled scenario target kind: {target_kind.value}")


def _native_payload_from_mapping(
    target_kind: ScenarioTargetKindV1,
    payload: Mapping[str, object],
) -> object:
    incoming = _canonical_native_json_bytes(payload)
    plain = json.loads(incoming.decode("utf-8"))
    if not isinstance(plain, dict):
        raise TypeError("native scenario payload must be an object")
    payload_type = _native_payload_type(target_kind)
    from_dict = getattr(payload_type, "from_dict", None)
    if not callable(from_dict):
        raise TypeError("native scenario payload lacks its canonical parser")
    native_payload = from_dict(plain)
    as_dict = getattr(native_payload, "as_dict", None)
    if not callable(as_dict) or _canonical_native_json_bytes(as_dict()) != incoming:
        raise ValueError("native scenario payload fields are not exact and canonical")
    return native_payload


def _native_payload_from_bytes(
    target_kind: ScenarioTargetKindV1,
    raw: bytes,
) -> object:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("native scenario payload must be an object")
    return _native_payload_from_mapping(target_kind, payload)


def _canonical_native_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise TypeError("native scenario payload is not canonical JSON") from error
    return text.encode("utf-8")


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: set[str] | frozenset[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{context} must be an object")
    actual = set(payload)
    missing = sorted(set(expected).difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing or unknown:
        raise ValueError(
            f"{context} fields are not exact: missing={missing} unknown={unknown}"
        )


def _exact_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _exact_str(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be a string")
    return value


__all__ = [
    "DEFINITION_MERGE_POLICIES_V1",
    "DEFINITION_SECTION_BY_TYPE_V1",
    "DEFINITION_TYPE_BY_SECTION_V1",
    "ExactFixedPointV1",
    "SCENARIO_BEHAVIOR_SECTION_NAMES",
    "SCENARIO_PLAN_ENVELOPE_SCHEMA_VERSION",
    "SCENARIO_SOURCE_SCHEMA_VERSION",
    "SCENARIO_SOURCE_SECTION_NAMES",
    "SCENARIO_TARGET_CONTRACTS_V1",
    "ScenarioFieldV1",
    "ScenarioDefinitionMergePolicyV1",
    "ScenarioDefinitionTypeV1",
    "ScenarioImportBundleV1",
    "ScenarioImportEdgeV1",
    "ScenarioImportV1",
    "ScenarioListMergeModeV1",
    "ScenarioMetadataV1",
    "ScenarioPlanEnvelopeV1",
    "ScenarioRecordV1",
    "ScenarioSectionV1",
    "ScenarioSource",
    "ScenarioSourceDocumentV1",
    "ScenarioSourceOriginV1",
    "ScenarioSourceV1",
    "ScenarioTargetContractV1",
    "ScenarioTargetKindV1",
    "ScenarioValueKindV1",
    "ResolvedScenarioBundleV1",
    "ResolvedScenarioDefinitionV1",
    "VolumeMultiplierV1",
    "canonical_native_payload_bytes",
]
