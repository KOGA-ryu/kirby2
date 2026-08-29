"""Separate legacy source-byte identity from canonical strategy semantics."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .ast import (
    MAX_EXACT_DECIMAL_DIGITS_V1,
    MAX_EXACT_DECIMAL_SCALE_V1,
    MAX_STRATEGY_DURATION_US_V1,
    StateMachineStrategyAstV1,
    StrategyAstV1,
    TrafficLightStrategyAstV1,
    parse_strategy_ast,
    render_canonical_strategy_ast,
)


STRATEGY_SEMANTIC_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_AST_SEMANTIC_V1\x00"
STRATEGY_LINEAGE_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_LINEAGE_V1\x00"
STRATEGY_IDENTITY_SCHEMA_VERSION_V1 = 1
STRATEGY_IDENTITY_MIGRATION_ID_V1 = "LEGACY_SOURCE_SHA256_TO_STRATEGY_AST_V1"
STRATEGY_AST_IMPORTER_ID_V1 = "KIRBY2_STRATEGY_AST_IMPORTER_V1"
LEGACY_SOURCE_IDENTITY_ALGORITHM_V1 = "SHA256_UTF8_SOURCE_BYTES_V1"
SEMANTIC_AST_IDENTITY_ALGORITHM_V1 = "DOMAIN_SEPARATED_CANONICAL_AST_SHA256_V1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrategyImportOriginV1(str, Enum):
    DIRECT_LEGACY_SOURCE = "DIRECT_LEGACY_SOURCE"
    EXPERIMENT_INLINE_SOURCE = "EXPERIMENT_INLINE_SOURCE"
    EXPERIMENT_RULE_FILE = "EXPERIMENT_RULE_FILE"
    CANONICAL_AST_RENDER = "CANONICAL_AST_RENDER"


@dataclass(frozen=True, slots=True)
class StrategyIdentityProvenanceV1:
    import_origin: StrategyImportOriginV1
    logical_source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.import_origin, StrategyImportOriginV1):
            raise TypeError("strategy import origin is invalid")
        if self.logical_source is not None:
            _validate_text(self.logical_source, "strategy logical source")
            if not self.logical_source:
                raise ValueError("strategy logical source must be nonempty when present")

    def as_dict(self) -> dict[str, object]:
        return {
            "import_origin": self.import_origin.value,
            "importer_id": STRATEGY_AST_IMPORTER_ID_V1,
            "importer_version": 1,
            "legacy_identity_algorithm": LEGACY_SOURCE_IDENTITY_ALGORITHM_V1,
            "logical_source": self.logical_source,
            "migration_id": STRATEGY_IDENTITY_MIGRATION_ID_V1,
            "semantic_identity_algorithm": SEMANTIC_AST_IDENTITY_ALGORITHM_V1,
        }


@dataclass(frozen=True, slots=True)
class StrategyIdentityV1:
    """Inspectable dual identity; neither digest aliases or replaces the other."""

    legacy_source_sha256: str
    semantic_ast_sha256: str
    provenance: StrategyIdentityProvenanceV1

    def __post_init__(self) -> None:
        _require_sha256(self.legacy_source_sha256, "legacy strategy source digest")
        _require_sha256(self.semantic_ast_sha256, "strategy semantic AST digest")
        if not isinstance(self.provenance, StrategyIdentityProvenanceV1):
            raise TypeError("strategy identity provenance is invalid")

    @property
    def source_sha256(self) -> str:
        """Compatibility spelling for the pre-discovery source identity."""

        return self.legacy_source_sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "identity_schema_version": STRATEGY_IDENTITY_SCHEMA_VERSION_V1,
            "legacy_source_sha256": self.legacy_source_sha256,
            "provenance": self.provenance.as_dict(),
            "semantic_ast_sha256": self.semantic_ast_sha256,
        }


def legacy_strategy_source_sha256(source: str) -> str:
    if type(source) is not str:
        raise TypeError("legacy strategy identity requires source text")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def canonical_strategy_ast_bytes(ast: StrategyAstV1) -> bytes:
    if not isinstance(
        ast,
        (TrafficLightStrategyAstV1, StateMachineStrategyAstV1),
    ):
        raise TypeError("semantic identity requires a canonical strategy AST")
    return canonical_identity_bytes(ast.semantic_projection())


def strategy_semantic_sha256(ast: StrategyAstV1) -> str:
    canonical = canonical_strategy_ast_bytes(ast)
    digest = hashlib.sha256()
    digest.update(STRATEGY_SEMANTIC_DIGEST_DOMAIN_V1)
    digest.update(struct.pack(">Q", len(canonical)))
    digest.update(canonical)
    return digest.hexdigest()


def strategy_identity_from_source(
    source: str,
    *,
    import_origin: StrategyImportOriginV1 = (
        StrategyImportOriginV1.DIRECT_LEGACY_SOURCE
    ),
    logical_source: str | None = None,
) -> StrategyIdentityV1:
    ast = parse_strategy_ast(source)
    return strategy_identity_from_ast(
        ast,
        legacy_source=source,
        import_origin=import_origin,
        logical_source=logical_source,
    )


def strategy_identity_from_ast(
    ast: StrategyAstV1,
    *,
    legacy_source: str,
    import_origin: StrategyImportOriginV1,
    logical_source: str | None = None,
) -> StrategyIdentityV1:
    if not isinstance(import_origin, StrategyImportOriginV1):
        raise TypeError("strategy import origin is invalid")
    return StrategyIdentityV1(
        legacy_source_sha256=legacy_strategy_source_sha256(legacy_source),
        semantic_ast_sha256=strategy_semantic_sha256(ast),
        provenance=StrategyIdentityProvenanceV1(import_origin, logical_source),
    )


def canonical_render_identity(ast: StrategyAstV1) -> StrategyIdentityV1:
    rendered = render_canonical_strategy_ast(ast)
    return strategy_identity_from_ast(
        ast,
        legacy_source=rendered,
        import_origin=StrategyImportOriginV1.CANONICAL_AST_RENDER,
        logical_source="canonical-strategy.k2strategy",
    )


def canonical_identity_bytes(value: object) -> bytes:
    _validate_identity_value(value, set())
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def lineage_payload_sha256(value: object) -> str:
    canonical = canonical_identity_bytes(value)
    digest = hashlib.sha256()
    digest.update(STRATEGY_LINEAGE_DIGEST_DOMAIN_V1)
    digest.update(struct.pack(">Q", len(canonical)))
    digest.update(canonical)
    return digest.hexdigest()


def _require_sha256(value: str, context: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be lowercase SHA-256")


def _validate_text(value: object, context: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{context} must be text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{context} must be NFC-normalized")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{context} contains a surrogate code point")
    if "\x00" in value:
        raise ValueError(f"{context} must not contain NUL")


def _validate_identity_value(value: object, active: set[int]) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        _validate_text(value, "semantic identity text")
        return
    if type(value) is float:
        raise TypeError("semantic identity forbids binary floats")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("semantic identity object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("semantic identity values must not contain cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_text(key, "semantic identity object key")
                _validate_identity_value(value[key], active)
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
                _validate_identity_value(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported semantic identity value: {type(value).__name__}")


STRATEGY_CANONICALIZATION_POLICY_V1 = {
    "canonical_schema": "KIRBY2_STRATEGY_AST_V1",
    "exact_decimal_max_digits": MAX_EXACT_DECIMAL_DIGITS_V1,
    "exact_decimal_max_scale": MAX_EXACT_DECIMAL_SCALE_V1,
    "max_duration_us": MAX_STRATEGY_DURATION_US_V1,
    "collapsed_equivalences": [
        "COMMENTS_AND_WHITESPACE",
        "CASE_INSENSITIVE_GRAMMAR_KEYWORDS",
        "DEFAULT_UNAVAILABLE_POLICY_ELISION",
        "DEFAULT_WINDOW_ELISION",
        "DECIMAL_SPELLING",
        "MILLISECOND_SECOND_UNIT_SPELLING",
        "CONJUNCTION_CHILD_ORDER",
        "DUPLICATE_CONJUNCTION_CHILD",
        "STATE_DECLARATION_ORDER",
    ],
    "preserved_semantics": [
        "IDENTIFIERS",
        "COMPARISON_OPERATOR",
        "TRANSITION_PRIORITY_ORDER",
        "UNAVAILABLE_VALUE_POLICY",
    ],
    "version": 1,
}
STRATEGY_CANONICALIZATION_POLICY_SHA256_V1 = hashlib.sha256(
    canonical_identity_bytes(STRATEGY_CANONICALIZATION_POLICY_V1)
).hexdigest()


__all__ = [
    "LEGACY_SOURCE_IDENTITY_ALGORITHM_V1",
    "SEMANTIC_AST_IDENTITY_ALGORITHM_V1",
    "STRATEGY_AST_IMPORTER_ID_V1",
    "STRATEGY_CANONICALIZATION_POLICY_SHA256_V1",
    "STRATEGY_CANONICALIZATION_POLICY_V1",
    "STRATEGY_IDENTITY_MIGRATION_ID_V1",
    "STRATEGY_IDENTITY_SCHEMA_VERSION_V1",
    "STRATEGY_LINEAGE_DIGEST_DOMAIN_V1",
    "STRATEGY_SEMANTIC_DIGEST_DOMAIN_V1",
    "StrategyIdentityProvenanceV1",
    "StrategyIdentityV1",
    "StrategyImportOriginV1",
    "canonical_identity_bytes",
    "canonical_render_identity",
    "canonical_strategy_ast_bytes",
    "legacy_strategy_source_sha256",
    "lineage_payload_sha256",
    "strategy_identity_from_ast",
    "strategy_identity_from_source",
    "strategy_semantic_sha256",
]
