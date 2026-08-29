"""Strict TOML parsing and canonical rendering for ``ScenarioSourceV1``."""

from __future__ import annotations

import tomllib
import unicodedata
from collections.abc import Mapping
from math import gcd

from kirby2.research.toml_codec import canonical_toml

from .models import (
    SCENARIO_BEHAVIOR_SECTION_NAMES,
    SCENARIO_SOURCE_SECTION_NAMES,
    ExactFixedPointV1,
    ScenarioFieldV1,
    ScenarioMetadataV1,
    ScenarioRecordV1,
    ScenarioSectionV1,
    ScenarioSourceV1,
    ScenarioTargetKindV1,
    ScenarioValueKindV1,
    VolumeMultiplierV1,
)


_ROOT_FIELDS = frozenset({"schema_version", *SCENARIO_SOURCE_SECTION_NAMES})
_METADATA_FIELDS = frozenset(
    {
        "adapter_id",
        "adapter_version",
        "capability_digest",
        "description",
        "scenario_id",
        "scenario_version",
        "target_kind",
        "target_version",
        "title",
    }
)
_SECTION_FIELDS = frozenset({"records"})
_RECORD_REQUIRED_FIELDS = frozenset(
    {"fields", "logical_name", "record_type", "version"}
)
_RECORD_OPTIONAL_FIELDS = frozenset({"extends", "reference"})
_FIELD_VALUE_TAGS = frozenset(kind.value for kind in ScenarioValueKindV1)


def parse_scenario_source(raw: bytes) -> ScenarioSourceV1:
    """Parse arbitrary formatting/comments into the strict immutable V1 model."""

    if type(raw) is not bytes:
        raise TypeError("scenario source input must be exact bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("scenario source must be valid UTF-8") from error
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError("scenario source must be valid TOML") from error
    if not isinstance(payload, dict):
        raise ValueError("scenario source root must be a table")
    _reject_non_identity_values(payload, "scenario_source")
    _require_exact_fields(payload, _ROOT_FIELDS, "scenario source")
    schema_version = _exact_int(payload, "schema_version")
    metadata = _parse_metadata(_object(payload, "metadata"))
    sections = {
        name: _parse_section(_object(payload, name), name)
        for name in SCENARIO_BEHAVIOR_SECTION_NAMES
    }
    return ScenarioSourceV1(
        schema_version=schema_version,
        metadata=metadata,
        **sections,
    )


def parse_scenario_source_toml(raw: bytes) -> ScenarioSourceV1:
    """Explicitly named alias for callers that distinguish future source codecs."""

    return parse_scenario_source(raw)


def render_canonical_scenario_source(source: ScenarioSourceV1) -> str:
    if type(source) is not ScenarioSourceV1:
        raise TypeError("canonical scenario rendering requires ScenarioSourceV1")
    return canonical_toml(source.as_dict())


def canonical_scenario_source_bytes(source: ScenarioSourceV1) -> bytes:
    return render_canonical_scenario_source(source).encode("utf-8")


def parse_canonical_scenario_source(raw: bytes) -> ScenarioSourceV1:
    """Parse a stored canonical source artifact and reject noncanonical bytes."""

    source = parse_scenario_source(raw)
    if canonical_scenario_source_bytes(source) != raw:
        raise ValueError("scenario source bytes are not canonical TOML")
    return source


def scenario_source_round_trip(source: ScenarioSourceV1) -> ScenarioSourceV1:
    return parse_canonical_scenario_source(canonical_scenario_source_bytes(source))


def _parse_metadata(payload: Mapping[str, object]) -> ScenarioMetadataV1:
    _require_exact_fields(payload, _METADATA_FIELDS, "scenario metadata")
    raw_kind = _exact_str(payload, "target_kind")
    try:
        target_kind = ScenarioTargetKindV1(raw_kind)
    except ValueError as error:
        raise ValueError("unsupported scenario target kind") from error
    return ScenarioMetadataV1(
        scenario_id=_exact_str(payload, "scenario_id"),
        scenario_version=_exact_int(payload, "scenario_version"),
        title=_exact_str(payload, "title"),
        description=_exact_str(payload, "description"),
        target_kind=target_kind,
        target_version=_exact_int(payload, "target_version"),
        adapter_id=_exact_str(payload, "adapter_id"),
        adapter_version=_exact_int(payload, "adapter_version"),
        capability_digest=_exact_str(payload, "capability_digest"),
    )


def _parse_section(
    payload: Mapping[str, object],
    section_name: str,
) -> ScenarioSectionV1:
    _require_exact_fields(payload, _SECTION_FIELDS, f"{section_name} section")
    records = payload["records"]
    if type(records) is not list:
        raise TypeError(f"{section_name}.records must be an array")
    return ScenarioSectionV1(
        tuple(
            _parse_record(_array_object(item, f"{section_name} record"))
            for item in records
        )
    )


def _parse_record(payload: Mapping[str, object]) -> ScenarioRecordV1:
    actual = set(payload)
    missing = sorted(_RECORD_REQUIRED_FIELDS.difference(actual))
    unknown = sorted(
        actual.difference(_RECORD_REQUIRED_FIELDS | _RECORD_OPTIONAL_FIELDS)
    )
    if missing or unknown:
        raise ValueError(
            "scenario record fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )
    raw_fields = payload["fields"]
    if type(raw_fields) is not list:
        raise TypeError("scenario record fields must be an array")
    reference = payload.get("reference")
    extends = payload.get("extends")
    if reference is not None and type(reference) is not str:
        raise TypeError("scenario record reference must be a string")
    if extends is not None and type(extends) is not str:
        raise TypeError("scenario record extends must be a string")
    return ScenarioRecordV1(
        logical_name=_exact_str(payload, "logical_name"),
        record_type=_exact_str(payload, "record_type"),
        version=_exact_int(payload, "version"),
        fields=tuple(
            _parse_field(_array_object(item, "scenario field"))
            for item in raw_fields
        ),
        reference=reference,
        extends=extends,
    )


def _parse_field(payload: Mapping[str, object]) -> ScenarioFieldV1:
    if "name" not in payload:
        raise ValueError("scenario field is missing name")
    unknown = sorted(set(payload).difference({"name", *_FIELD_VALUE_TAGS}))
    if unknown:
        raise ValueError(f"scenario field has unknown fields: {unknown}")
    selected_tags = tuple(tag for tag in _FIELD_VALUE_TAGS if tag in payload)
    if len(selected_tags) != 1 or len(payload) != 2:
        raise ValueError("scenario field requires exactly one explicit value tag")
    tag = ScenarioValueKindV1(selected_tags[0])
    value = payload[tag.value]
    if tag is ScenarioValueKindV1.VOLUME_MULTIPLIER:
        value = _parse_volume_multiplier(_mapping(value, "volume_multiplier"))
    elif tag is ScenarioValueKindV1.FIXED_POINT:
        raw = _mapping(value, "fixed_point")
        _require_exact_fields(
            raw,
            frozenset({"coefficient", "scale", "unit"}),
            "fixed_point",
        )
        value = ExactFixedPointV1(
            coefficient=_exact_int(raw, "coefficient"),
            scale=_exact_int(raw, "scale"),
            unit=_exact_str(raw, "unit"),
        )
    elif tag is ScenarioValueKindV1.IDENTIFIERS:
        if type(value) is not list or any(type(item) is not str for item in value):
            raise TypeError("identifiers must be an array of strings")
        value = tuple(value)
    return ScenarioFieldV1(
        name=_exact_str(payload, "name"),
        value_kind=tag,
        value=value,
    )


def _parse_volume_multiplier(
    payload: Mapping[str, object],
) -> VolumeMultiplierV1:
    if set(payload) == {"denominator", "numerator"}:
        return VolumeMultiplierV1(
            numerator=_exact_int(payload, "numerator"),
            denominator=_exact_int(payload, "denominator"),
        )
    if set(payload) == {"coefficient", "scale"}:
        coefficient = _exact_int(payload, "coefficient")
        scale = _exact_int(payload, "scale")
        ExactFixedPointV1(coefficient, scale, "VOLUME_MULTIPLIER")
        if coefficient <= 0:
            raise ValueError("volume multiplier fixed point must be positive")
        divisor = gcd(coefficient, scale)
        return VolumeMultiplierV1(coefficient // divisor, scale // divisor)
    raise ValueError(
        "volume_multiplier requires exactly numerator/denominator or "
        "coefficient/scale"
    )


def _reject_non_identity_values(value: object, path: str) -> None:
    if type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"{path} text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError(f"{path} text contains a surrogate code point")
        return
    if type(value) is float:
        raise TypeError(f"{path} uses a forbidden binary/decimal float")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError(f"{path} object keys must be strings")
        for key, item in value.items():
            _reject_non_identity_values(item, f"{path}.{key}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _reject_non_identity_values(item, f"{path}[{index}]")
        return
    raise TypeError(
        f"{path} contains unsupported TOML identity value {type(value).__name__}"
    )


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be an inline table")
    return value


def _object(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping(payload[key], key)


def _array_object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be an inline table")
    return value


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(payload)
    missing = sorted(expected.difference(actual))
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
    "canonical_scenario_source_bytes",
    "parse_canonical_scenario_source",
    "parse_scenario_source",
    "parse_scenario_source_toml",
    "render_canonical_scenario_source",
    "scenario_source_round_trip",
]
