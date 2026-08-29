"""Domain-separated identities for source, behavior, and compiled artifacts."""

from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import ScenarioPlanEnvelopeV1, ScenarioSourceV1


SOURCE_BUNDLE_DIGEST_DOMAIN_V1 = b"KIRBY2_SOURCE_BUNDLE_DIGEST_V1\x00"
SEMANTIC_PLAN_DIGEST_DOMAIN_V1 = b"KIRBY2_SEMANTIC_PLAN_DIGEST_V1\x00"
COMPILED_ARTIFACT_DIGEST_DOMAIN_V1 = b"KIRBY2_COMPILED_ARTIFACT_DIGEST_V1\x00"


@dataclass(frozen=True, slots=True)
class SourceBundleEntryV1:
    """One ordered source/import byte member; no filesystem lookup is implied."""

    logical_path: str
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.logical_path) is not str or not self.logical_path:
            raise ValueError("source bundle logical path must be a nonempty string")
        if "\x00" in self.logical_path:
            raise ValueError("source bundle logical path must not contain NUL")
        if unicodedata.normalize("NFC", self.logical_path) != self.logical_path:
            raise ValueError("source bundle logical path must be NFC-normalized")
        if type(self.raw_bytes) is not bytes:
            raise TypeError("source bundle member must contain exact bytes")


def source_bundle_digest(
    entries: Iterable[SourceBundleEntryV1 | tuple[str, bytes] | bytes] | bytes,
) -> str:
    """Hash ordered raw source/import bytes with explicit member framing.

    Passing bare bytes is shorthand for a one-document bundle.  Path labels are
    identity-bearing because two ordered import graphs with identical concatenated
    bytes are not the same source provenance.
    """

    if type(entries) is bytes:
        normalized = (SourceBundleEntryV1("source.toml", entries),)
    else:
        normalized = tuple(
            _normalize_source_entry(item, index)
            for index, item in enumerate(entries)
        )
    if not normalized:
        raise ValueError("source bundle must contain at least one byte member")
    logical_paths = tuple(item.logical_path for item in normalized)
    if len(logical_paths) != len(set(logical_paths)):
        raise ValueError("source bundle logical paths must be unique")
    digest = hashlib.sha256()
    digest.update(SOURCE_BUNDLE_DIGEST_DOMAIN_V1)
    digest.update(struct.pack(">Q", len(normalized)))
    for entry in normalized:
        _update_framed(digest, entry.logical_path.encode("utf-8"))
        _update_framed(digest, entry.raw_bytes)
    return digest.hexdigest()


def semantic_plan_digest(
    value: ScenarioSourceV1 | ScenarioPlanEnvelopeV1 | Mapping[str, object],
) -> str:
    """Hash canonical resolved behavior, never raw authoring bytes."""

    canonical = canonical_semantic_plan_bytes(value)
    digest = hashlib.sha256()
    digest.update(SEMANTIC_PLAN_DIGEST_DOMAIN_V1)
    _update_framed(digest, canonical)
    return digest.hexdigest()


def canonical_semantic_plan_bytes(
    value: ScenarioSourceV1 | ScenarioPlanEnvelopeV1 | Mapping[str, object],
) -> bytes:
    if type(value) is ScenarioSourceV1:
        return _canonical_strict_json_bytes(value.semantic_projection())
    if type(value) is ScenarioPlanEnvelopeV1:
        # Existing native target contracts may legitimately contain finite legacy
        # floats.  Their own validated canonical bytes remain authoritative.
        return value.canonical_bytes()
    if isinstance(value, Mapping):
        return _canonical_strict_json_bytes(value)
    raise TypeError(
        "semantic plan identity requires ScenarioSourceV1, "
        "ScenarioPlanEnvelopeV1, or a resolved strict mapping"
    )


def compiled_artifact_digest(
    exact_compiled_artifact: bytes,
    provenance: bytes | Mapping[str, object],
) -> str:
    """Hash exact compiled bytes together with their canonical provenance."""

    if type(exact_compiled_artifact) is not bytes:
        raise TypeError("compiled artifact identity requires exact bytes")
    if type(provenance) is bytes:
        provenance_bytes = provenance
    elif isinstance(provenance, Mapping):
        provenance_bytes = _canonical_strict_json_bytes(provenance)
    else:
        raise TypeError("compiled artifact provenance must be bytes or a strict mapping")
    digest = hashlib.sha256()
    digest.update(COMPILED_ARTIFACT_DIGEST_DOMAIN_V1)
    _update_framed(digest, exact_compiled_artifact)
    _update_framed(digest, provenance_bytes)
    return digest.hexdigest()


def _normalize_source_entry(
    value: SourceBundleEntryV1 | tuple[str, bytes] | bytes,
    index: int,
) -> SourceBundleEntryV1:
    if type(value) is SourceBundleEntryV1:
        return value
    if type(value) is bytes:
        return SourceBundleEntryV1(f"ordered-member-{index:08d}", value)
    if (
        type(value) is tuple
        and len(value) == 2
        and type(value[0]) is str
        and type(value[1]) is bytes
    ):
        return SourceBundleEntryV1(value[0], value[1])
    raise TypeError(
        "source bundle entries must be SourceBundleEntryV1, (path, bytes), or bytes"
    )


def _update_framed(digest: object, value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _canonical_strict_json_bytes(value: object) -> bytes:
    _validate_strict_identity_value(value, set())
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_strict_identity_value(value: object, active: set[int]) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("semantic identity text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("semantic identity text contains a surrogate code point")
        return
    if type(value) is float:
        raise TypeError("semantic identity forbids binary/decimal floats")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("semantic identity object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("semantic identity values must not contain cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_strict_identity_value(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("semantic identity values must not contain cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_strict_identity_value(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported semantic identity value: {type(value).__name__}")


__all__ = [
    "COMPILED_ARTIFACT_DIGEST_DOMAIN_V1",
    "SEMANTIC_PLAN_DIGEST_DOMAIN_V1",
    "SOURCE_BUNDLE_DIGEST_DOMAIN_V1",
    "SourceBundleEntryV1",
    "canonical_semantic_plan_bytes",
    "compiled_artifact_digest",
    "semantic_plan_digest",
    "source_bundle_digest",
]
