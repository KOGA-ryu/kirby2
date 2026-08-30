"""Deterministic field-level redaction for portable instructor evidence.

Redaction is a pure transformation of one exact canonical JSON object.  A policy
selects subtrees from a closed portable-evidence allowlist; categorical rules still
exclude identity mappings, direct identifiers, secrets, and local filesystem paths.
Hidden or reveal material is excluded unless a policy separately authorizes its
exact path below ``/selected_causal_traces``.

The redacted bytes never silently lose a source field.  The accompanying manifest
contains one entry for the root and every object field or array item in the source,
including descendants of an omitted container.  Retained entries bind both source
and output value digests; omitted entries bind the source value and an explicit
reason.  All records are immutable, canonical, and standalone reloadable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import ClassVar


FIELD_REDACTION_ENTRY_SCHEMA_ID = "KIRBY2_FIELD_REDACTION_ENTRY_V1"
FIELD_REDACTION_ENTRY_SCHEMA_VERSION = 1
REDACTION_MANIFEST_SCHEMA_ID = "KIRBY2_REDACTION_MANIFEST_V1"
REDACTION_MANIFEST_SCHEMA_VERSION = 1
REDACTION_POLICY_SCHEMA_ID = "KIRBY2_REDACTION_POLICY_V1"
REDACTION_POLICY_SCHEMA_VERSION = 1
REDACTED_DOCUMENT_SCHEMA_ID = "KIRBY2_REDACTED_DOCUMENT_V1"
REDACTED_DOCUMENT_SCHEMA_VERSION = 1
PORTABLE_EVIDENCE_ALLOWLIST_VERSION = "KIRBY2_PORTABLE_EVIDENCE_ALLOWLIST_V1"

# Closed maximum export surface.  A policy may narrow these subtree roots but cannot
# add another top-level category.  Order is canonical ASCII byte order.
PORTABLE_EVIDENCE_ALLOWLIST_V1: tuple[str, ...] = (
    "/annotations",
    "/assignment",
    "/attempt_manifest",
    "/limitations",
    "/provenance",
    "/rubric_scores",
    "/selected_causal_traces",
    "/software_version",
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_INVALID_POINTER_ESCAPE = re.compile(r"~(?![01])")
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


class PortableEvidenceKindV1(str, Enum):
    """Closed document vocabulary for the unpacked portable evidence directory."""

    PORTABLE_EVIDENCE = "PORTABLE_EVIDENCE"
    ASSIGNMENT = "ASSIGNMENT"
    ATTEMPT_MANIFEST = "ATTEMPT_MANIFEST"
    RUBRIC_SCORES = "RUBRIC_SCORES"
    ANNOTATIONS = "ANNOTATIONS"
    SELECTED_CAUSAL_TRACES = "SELECTED_CAUSAL_TRACES"
    PROVENANCE = "PROVENANCE"
    SOFTWARE_VERSION = "SOFTWARE_VERSION"
    LIMITATIONS = "LIMITATIONS"


PORTABLE_EVIDENCE_ROOT_BY_KIND = MappingProxyType(
    {
        PortableEvidenceKindV1.ASSIGNMENT: "/assignment",
        PortableEvidenceKindV1.ATTEMPT_MANIFEST: "/attempt_manifest",
        PortableEvidenceKindV1.RUBRIC_SCORES: "/rubric_scores",
        PortableEvidenceKindV1.ANNOTATIONS: "/annotations",
        PortableEvidenceKindV1.SELECTED_CAUSAL_TRACES: "/selected_causal_traces",
        PortableEvidenceKindV1.PROVENANCE: "/provenance",
        PortableEvidenceKindV1.SOFTWARE_VERSION: "/software_version",
        PortableEvidenceKindV1.LIMITATIONS: "/limitations",
    }
)


class RedactionActionV1(str, Enum):
    RETAIN = "RETAIN"
    OMIT = "OMIT"


class RedactionReasonV1(str, Enum):
    PORTABLE_EVIDENCE_ALLOWLIST = "PORTABLE_EVIDENCE_ALLOWLIST"
    ALLOWLIST_CONTAINER = "ALLOWLIST_CONTAINER"
    HIDDEN_REVEAL_EXPLICITLY_AUTHORIZED = (
        "HIDDEN_REVEAL_EXPLICITLY_AUTHORIZED"
    )
    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    IDENTITY_MAPPING_PROHIBITED = "IDENTITY_MAPPING_PROHIBITED"
    DIRECT_IDENTIFIER_PROHIBITED = "DIRECT_IDENTIFIER_PROHIBITED"
    SECRET_PROHIBITED = "SECRET_PROHIBITED"
    LOCAL_FILESYSTEM_PATH_PROHIBITED = "LOCAL_FILESYSTEM_PATH_PROHIBITED"
    HIDDEN_REVEAL_NOT_AUTHORIZED = "HIDDEN_REVEAL_NOT_AUTHORIZED"
    ANCESTOR_EXCLUDED = "ANCESTOR_EXCLUDED"


class JsonValueKindV1(str, Enum):
    OBJECT = "OBJECT"
    ARRAY = "ARRAY"
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    NULL = "NULL"


_REASONS_BY_ACTION = MappingProxyType(
    {
        RedactionActionV1.RETAIN: frozenset(
            {
                RedactionReasonV1.PORTABLE_EVIDENCE_ALLOWLIST,
                RedactionReasonV1.ALLOWLIST_CONTAINER,
                RedactionReasonV1.HIDDEN_REVEAL_EXPLICITLY_AUTHORIZED,
            }
        ),
        RedactionActionV1.OMIT: frozenset(
            {
                RedactionReasonV1.NOT_ALLOWLISTED,
                RedactionReasonV1.IDENTITY_MAPPING_PROHIBITED,
                RedactionReasonV1.DIRECT_IDENTIFIER_PROHIBITED,
                RedactionReasonV1.SECRET_PROHIBITED,
                RedactionReasonV1.LOCAL_FILESYSTEM_PATH_PROHIBITED,
                RedactionReasonV1.HIDDEN_REVEAL_NOT_AUTHORIZED,
                RedactionReasonV1.ANCESTOR_EXCLUDED,
            }
        ),
    }
)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("redaction value is not strict canonical JSON") from error


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("redaction JSON contains a duplicate object key")
        result[key] = value
    return result


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} decoder requires exact bytes")
    if not raw or len(raw) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} byte length is invalid")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return value


def _fields(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _version(value: object, label: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{label} must be one explicit canonical version")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_pointer(value: object, label: str, *, allow_root: bool = True) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not value:
        if allow_root:
            return value
        raise ValueError(f"{label} cannot select the whole document root")
    if not value.startswith("/") or _INVALID_POINTER_ESCAPE.search(value) is not None:
        raise ValueError(f"{label} must be a canonical RFC 6901 JSON pointer")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"{label} contains a forbidden control character")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} exceeds the bounded JSON-pointer size")
    return value


def _pointer_escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _child_pointer(parent: str, segment: str) -> str:
    return parent + "/" + _pointer_escape(segment)


def _at_or_below(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _ancestor_of(path: str, descendant: str) -> bool:
    if path == "":
        return bool(descendant)
    return descendant.startswith(path + "/")


def _canonical_pointers(
    value: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    if not allow_empty and not value:
        raise ValueError(f"{label} cannot be empty")
    if any(type(item) is not str for item in value):
        raise TypeError(f"{label} must contain exact text pointers")
    checked = tuple(_json_pointer(item, label, allow_root=False) for item in value)
    canonical = tuple(sorted(set(checked), key=lambda item: item.encode("utf-8")))
    if canonical != value:
        raise ValueError(f"{label} must be unique and canonically ordered")
    return value


def _value_kind(value: object) -> JsonValueKindV1:
    if type(value) is dict:
        return JsonValueKindV1.OBJECT
    if type(value) is list:
        return JsonValueKindV1.ARRAY
    if type(value) is str:
        return JsonValueKindV1.STRING
    if type(value) is bool:
        return JsonValueKindV1.BOOLEAN
    if type(value) is int:
        return JsonValueKindV1.INTEGER
    if type(value) is float:
        return JsonValueKindV1.NUMBER
    if value is None:
        return JsonValueKindV1.NULL
    raise TypeError("source contains a value outside canonical JSON")


@dataclass(frozen=True, slots=True)
class FieldRedactionEntryV1:
    """One exact source JSON path and its deterministic redaction disposition."""

    json_path: str
    action: RedactionActionV1
    reason: RedactionReasonV1
    value_kind: JsonValueKindV1
    source_sha256: str
    output_sha256: str | None

    schema_id: ClassVar[str] = FIELD_REDACTION_ENTRY_SCHEMA_ID
    schema_version: ClassVar[int] = FIELD_REDACTION_ENTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _json_pointer(self.json_path, "field redaction JSON path")
        if type(self.action) is not RedactionActionV1:
            raise TypeError("field redaction action is invalid")
        if type(self.reason) is not RedactionReasonV1:
            raise TypeError("field redaction reason is invalid")
        if self.reason not in _REASONS_BY_ACTION[self.action]:
            raise ValueError("field redaction reason does not match its action")
        if type(self.value_kind) is not JsonValueKindV1:
            raise TypeError("field redaction value kind is invalid")
        _sha256(self.source_sha256, "field redaction source digest")
        if self.action is RedactionActionV1.RETAIN:
            _sha256(self.output_sha256, "field redaction output digest")
        elif self.output_sha256 is not None:
            raise ValueError("omitted fields cannot claim output bytes")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "json_path": self.json_path,
            "output_sha256": self.output_sha256,
            "reason": self.reason.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "value_kind": self.value_kind.value,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> FieldRedactionEntryV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "action",
                    "json_path",
                    "output_sha256",
                    "reason",
                    "schema_id",
                    "schema_version",
                    "source_sha256",
                    "value_kind",
                }
            ),
            "field redaction entry",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("field redaction entry schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("field redaction entry schema version differs")
        try:
            action = RedactionActionV1(payload["action"])
            reason = RedactionReasonV1(payload["reason"])
            value_kind = JsonValueKindV1(payload["value_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("field redaction enum value is invalid") from error
        raw_output = payload["output_sha256"]
        return cls(
            json_path=_json_pointer(payload["json_path"], "field redaction JSON path"),
            action=action,
            reason=reason,
            value_kind=value_kind,
            source_sha256=_sha256(
                payload["source_sha256"], "field redaction source digest"
            ),
            output_sha256=(
                None
                if raw_output is None
                else _sha256(raw_output, "field redaction output digest")
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> FieldRedactionEntryV1:
        return cls.from_dict(_canonical_object(raw, "field redaction entry"))


@dataclass(frozen=True, slots=True)
class RedactionPolicyV1:
    """Narrow selection inside the closed portable-evidence export surface."""

    policy_id: str
    policy_version: str
    allowlisted_paths: tuple[str, ...]
    authorized_hidden_paths: tuple[str, ...] = ()

    schema_id: ClassVar[str] = REDACTION_POLICY_SCHEMA_ID
    schema_version: ClassVar[int] = REDACTION_POLICY_SCHEMA_VERSION
    portable_evidence_allowlist_version: ClassVar[str] = (
        PORTABLE_EVIDENCE_ALLOWLIST_VERSION
    )
    portable_evidence_roots: ClassVar[tuple[str, ...]] = (
        PORTABLE_EVIDENCE_ALLOWLIST_V1
    )

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "redaction policy ID")
        _version(self.policy_version, "redaction policy version")
        _canonical_pointers(
            self.allowlisted_paths,
            "redaction allowlisted paths",
            allow_empty=False,
        )
        _canonical_pointers(
            self.authorized_hidden_paths,
            "authorized hidden paths",
            allow_empty=True,
        )
        for path in self.allowlisted_paths:
            if not any(
                _at_or_below(path, root)
                for root in PORTABLE_EVIDENCE_ALLOWLIST_V1
            ):
                raise ValueError(
                    "redaction allowlist path widens the portable evidence surface"
                )
        for path in self.authorized_hidden_paths:
            if not _at_or_below(path, "/selected_causal_traces"):
                raise ValueError(
                    "hidden/reveal authorization is restricted to selected causal traces"
                )
            if not any(
                _at_or_below(path, allowed)
                for allowed in self.allowlisted_paths
            ):
                raise ValueError(
                    "hidden/reveal authorization must be inside an allowlisted subtree"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "allowlisted_paths": list(self.allowlisted_paths),
            "authorized_hidden_paths": list(self.authorized_hidden_paths),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "portable_evidence_allowlist_version": (
                self.portable_evidence_allowlist_version
            ),
            "portable_evidence_roots": list(self.portable_evidence_roots),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def policy_sha256(self) -> str:
        return self.sha256

    @classmethod
    def from_dict(cls, value: object) -> RedactionPolicyV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "allowlisted_paths",
                    "authorized_hidden_paths",
                    "policy_id",
                    "policy_version",
                    "portable_evidence_allowlist_version",
                    "portable_evidence_roots",
                    "schema_id",
                    "schema_version",
                }
            ),
            "redaction policy",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("redaction policy schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("redaction policy schema version differs")
        if (
            payload["portable_evidence_allowlist_version"]
            != PORTABLE_EVIDENCE_ALLOWLIST_VERSION
        ):
            raise ValueError("portable evidence allowlist version differs")
        raw_roots = payload["portable_evidence_roots"]
        if (
            type(raw_roots) is not list
            or tuple(raw_roots) != PORTABLE_EVIDENCE_ALLOWLIST_V1
        ):
            raise ValueError("portable evidence root inventory differs")
        raw_allowed = payload["allowlisted_paths"]
        raw_hidden = payload["authorized_hidden_paths"]
        if type(raw_allowed) is not list or type(raw_hidden) is not list:
            raise TypeError("redaction policy paths must be JSON arrays")
        return cls(
            policy_id=_identifier(payload["policy_id"], "redaction policy ID"),
            policy_version=_version(
                payload["policy_version"], "redaction policy version"
            ),
            allowlisted_paths=tuple(raw_allowed),  # type: ignore[arg-type]
            authorized_hidden_paths=tuple(raw_hidden),  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RedactionPolicyV1:
        policy = cls.from_dict(_canonical_object(raw, "redaction policy"))
        if policy.canonical_bytes() != raw:
            raise ValueError("redaction policy bytes changed after reload")
        return policy


def create_portable_evidence_redaction_policy(
    *,
    policy_id: str,
    policy_version: str = "1",
    allowlisted_paths: tuple[str, ...] = PORTABLE_EVIDENCE_ALLOWLIST_V1,
    authorized_hidden_paths: tuple[str, ...] = (),
) -> RedactionPolicyV1:
    return RedactionPolicyV1(
        policy_id=policy_id,
        policy_version=policy_version,
        allowlisted_paths=allowlisted_paths,
        authorized_hidden_paths=authorized_hidden_paths,
    )


@dataclass(frozen=True, slots=True)
class RedactionManifestV1:
    """Complete field inventory for one source-to-output transformation."""

    document_id: str
    document_kind: PortableEvidenceKindV1
    source_sha256: str
    output_sha256: str
    policy_id: str
    policy_sha256: str
    entries: tuple[FieldRedactionEntryV1, ...]

    schema_id: ClassVar[str] = REDACTION_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = REDACTION_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.document_id, "redaction document ID")
        if type(self.document_kind) is not PortableEvidenceKindV1:
            raise TypeError("redaction document kind is invalid")
        _sha256(self.source_sha256, "redaction manifest source digest")
        _sha256(self.output_sha256, "redaction manifest output digest")
        _identifier(self.policy_id, "redaction manifest policy ID")
        _sha256(self.policy_sha256, "redaction manifest policy digest")
        if type(self.entries) is not tuple or not self.entries:
            raise ValueError("redaction manifest entries must be a nonempty tuple")
        if any(type(item) is not FieldRedactionEntryV1 for item in self.entries):
            raise TypeError("redaction manifest contains an invalid field entry")
        canonical = tuple(
            sorted(self.entries, key=lambda item: item.json_path.encode("utf-8"))
        )
        if canonical != self.entries:
            raise ValueError("redaction manifest entries must be canonically ordered")
        if len({item.json_path for item in self.entries}) != len(self.entries):
            raise ValueError("redaction manifest cannot repeat a JSON path")
        root = self.entries[0]
        if (
            root.json_path != ""
            or root.action is not RedactionActionV1.RETAIN
            or root.source_sha256 != self.source_sha256
            or root.output_sha256 != self.output_sha256
        ):
            raise ValueError("redaction manifest root does not bind source and output")

    @property
    def retained_count(self) -> int:
        return sum(item.action is RedactionActionV1.RETAIN for item in self.entries)

    @property
    def omitted_count(self) -> int:
        return sum(item.action is RedactionActionV1.OMIT for item in self.entries)

    @property
    def retained_paths(self) -> tuple[str, ...]:
        return tuple(
            item.json_path
            for item in self.entries
            if item.action is RedactionActionV1.RETAIN
        )

    @property
    def omitted_paths(self) -> tuple[str, ...]:
        return tuple(
            item.json_path
            for item in self.entries
            if item.action is RedactionActionV1.OMIT
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "document_kind": self.document_kind.value,
            "entries": [item.as_dict() for item in self.entries],
            "omitted_count": self.omitted_count,
            "output_sha256": self.output_sha256,
            "policy_id": self.policy_id,
            "policy_sha256": self.policy_sha256,
            "retained_count": self.retained_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        return self.sha256

    @classmethod
    def from_dict(cls, value: object) -> RedactionManifestV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "document_id",
                    "document_kind",
                    "entries",
                    "omitted_count",
                    "output_sha256",
                    "policy_id",
                    "policy_sha256",
                    "retained_count",
                    "schema_id",
                    "schema_version",
                    "source_sha256",
                }
            ),
            "redaction manifest",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("redaction manifest schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("redaction manifest schema version differs")
        raw_entries = payload["entries"]
        if type(raw_entries) is not list:
            raise TypeError("redaction manifest entries must be a JSON array")
        try:
            document_kind = PortableEvidenceKindV1(payload["document_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("redaction manifest document kind is invalid") from error
        manifest = cls(
            document_id=_identifier(payload["document_id"], "redaction document ID"),
            document_kind=document_kind,
            source_sha256=_sha256(
                payload["source_sha256"], "redaction manifest source digest"
            ),
            output_sha256=_sha256(
                payload["output_sha256"], "redaction manifest output digest"
            ),
            policy_id=_identifier(
                payload["policy_id"], "redaction manifest policy ID"
            ),
            policy_sha256=_sha256(
                payload["policy_sha256"], "redaction manifest policy digest"
            ),
            entries=tuple(FieldRedactionEntryV1.from_dict(item) for item in raw_entries),
        )
        if (
            _nonnegative_int(payload["retained_count"], "redaction retained count")
            != manifest.retained_count
        ):
            raise ValueError("redaction retained count differs from field entries")
        if (
            _nonnegative_int(payload["omitted_count"], "redaction omitted count")
            != manifest.omitted_count
        ):
            raise ValueError("redaction omitted count differs from field entries")
        return manifest

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RedactionManifestV1:
        manifest = cls.from_dict(_canonical_object(raw, "redaction manifest"))
        if manifest.canonical_bytes() != raw:
            raise ValueError("redaction manifest bytes changed after reload")
        return manifest


@dataclass(frozen=True, slots=True)
class RedactedDocumentV1:
    """Canonical output text paired with its complete field-level manifest."""

    document_id: str
    document_kind: PortableEvidenceKindV1
    redacted_json: str
    manifest: RedactionManifestV1

    schema_id: ClassVar[str] = REDACTED_DOCUMENT_SCHEMA_ID
    schema_version: ClassVar[int] = REDACTED_DOCUMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.document_id, "redacted document ID")
        if type(self.document_kind) is not PortableEvidenceKindV1:
            raise TypeError("redacted document kind is invalid")
        if type(self.redacted_json) is not str or not self.redacted_json:
            raise ValueError("redacted document JSON must be nonempty exact text")
        try:
            output_bytes = self.redacted_json.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("redacted document JSON must be canonical ASCII") from error
        _canonical_object(output_bytes, "redacted document output")
        if type(self.manifest) is not RedactionManifestV1:
            raise TypeError("redacted document manifest is invalid")
        if (
            self.manifest.document_id != self.document_id
            or self.manifest.document_kind is not self.document_kind
            or self.manifest.output_sha256
            != hashlib.sha256(output_bytes).hexdigest()
        ):
            raise ValueError("redacted document differs from its manifest")

    @property
    def output_bytes(self) -> bytes:
        return self.redacted_json.encode("ascii")

    @property
    def source_sha256(self) -> str:
        return self.manifest.source_sha256

    @property
    def output_sha256(self) -> str:
        return self.manifest.output_sha256

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    @property
    def redaction_manifest(self) -> RedactionManifestV1:
        return self.manifest

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "document_kind": self.document_kind.value,
            "manifest": self.manifest.as_dict(),
            "redacted_json": self.redacted_json,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> RedactedDocumentV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "document_id",
                    "document_kind",
                    "manifest",
                    "redacted_json",
                    "schema_id",
                    "schema_version",
                }
            ),
            "redacted document",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("redacted document schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("redacted document schema version differs")
        try:
            document_kind = PortableEvidenceKindV1(payload["document_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("redacted document kind is invalid") from error
        redacted_json = payload["redacted_json"]
        if type(redacted_json) is not str:
            raise TypeError("redacted document JSON must be exact text")
        return cls(
            document_id=_identifier(payload["document_id"], "redacted document ID"),
            document_kind=document_kind,
            redacted_json=redacted_json,
            manifest=RedactionManifestV1.from_dict(payload["manifest"]),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RedactedDocumentV1:
        document = cls.from_dict(_canonical_object(raw, "redacted document"))
        if document.canonical_bytes() != raw:
            raise ValueError("redacted document bytes changed after reload")
        return document


@dataclass(frozen=True, slots=True)
class _CategoricalRuleV1:
    reason: RedactionReasonV1
    field_pattern: re.Pattern[str] | None = None
    value_pattern: re.Pattern[str] | None = None
    authorizable: bool = False

    def matches(self, field_name: str | None, value: object) -> bool:
        field_matches = (
            self.field_pattern is not None
            and field_name is not None
            and self.field_pattern.fullmatch(field_name) is not None
        )
        value_matches = (
            self.value_pattern is not None
            and type(value) is str
            and self.value_pattern.search(value) is not None
        )
        return field_matches or value_matches


_CATEGORICAL_RULES: tuple[_CategoricalRuleV1, ...] = (
    _CategoricalRuleV1(
        reason=RedactionReasonV1.IDENTITY_MAPPING_PROHIBITED,
        field_pattern=re.compile(
            r"(?:identity_?map|identity_?mapping|identity_?mappings|"
            r"identity_?mapping_?area|direct_?identity|direct_?identities|"
            r"direct_?identity_?mapping)"
        ),
    ),
    _CategoricalRuleV1(
        reason=RedactionReasonV1.DIRECT_IDENTIFIER_PROHIBITED,
        field_pattern=re.compile(
            r"(?:direct_?identifier|direct_?identifiers|full_?name|legal_?name|"
            r"display_?name|name|first_?name|last_?name|given_?name|surname|"
            r"username|user_?name|"
            r"email|email_?address|phone|phone_?number|address|postal_?address|"
            r"street_?address|institutional_?id|student_?id|employee_?id|"
            r"government_?id)"
        ),
        value_pattern=re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"),
    ),
    _CategoricalRuleV1(
        reason=RedactionReasonV1.SECRET_PROHIBITED,
        field_pattern=re.compile(
            r"(?:secret|secrets|password|password_?hash|api_?key|token|access_?token|"
            r"refresh_?token|auth_?token|private_?key|credential|credentials|"
            r"session_?cookie|.*_secret|.*_password|.*_token|.*_credential|"
            r".*_private_?key)"
        ),
        value_pattern=re.compile(
            r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
            r"\bBearer\s+[A-Za-z0-9._~+/=-]+|\bAKIA[0-9A-Z]{16}\b|"
            r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bsk-[A-Za-z0-9_-]{20,}\b)"
        ),
    ),
    _CategoricalRuleV1(
        reason=RedactionReasonV1.LOCAL_FILESYSTEM_PATH_PROHIBITED,
        field_pattern=re.compile(
            r"(?:path|local_?path|file_?path|filesystem_?path|data_?root|"
            r"workspace_?root|repo_?root|repository_?root|project_?root|"
            r"home_?directory|working_?directory|directory|cwd|"
            r".*_path|.*_directory)"
        ),
        value_pattern=re.compile(
            r"(?:\A(?:/Users/|/home/|/private/|/var/|/tmp/|/opt/|/etc/|~/)|"
            r"\A[A-Za-z]:[\\/]|\Afile://|\A\\\\)"
        ),
    ),
    _CategoricalRuleV1(
        reason=RedactionReasonV1.HIDDEN_REVEAL_NOT_AUTHORIZED,
        field_pattern=re.compile(
            r"(?:hidden|hidden_.*|.*_hidden|reveal|reveal_.*|.*_reveal|"
            r"ground_?truth|ground_?truth_.*|latent_.*|private_?state|"
            r"nonpublic_.*|undisplayed_.*|queue_?position|reserve|"
            r"reserve_?quantity|iceberg_?reserve)"
        ),
        authorizable=True,
    ),
)


@dataclass(frozen=True, slots=True)
class _FieldClassificationV1:
    exclusion_reason: RedactionReasonV1 | None
    hidden_authorized: bool


def _normalized_field_name(path: str) -> str | None:
    if not path:
        return None
    raw = path.rsplit("/", 1)[-1]
    decoded = raw.replace("~1", "/").replace("~0", "~")
    # Canonical field classifiers deliberately ignore punctuation and case.
    return re.sub(r"[^a-z0-9]+", "_", decoded.lower()).strip("_")


def _hidden_path_authorized(path: str, policy: RedactionPolicyV1) -> bool:
    return any(_at_or_below(path, root) for root in policy.authorized_hidden_paths)


def _classify_field(
    path: str,
    value: object,
    policy: RedactionPolicyV1,
) -> _FieldClassificationV1:
    field_name = _normalized_field_name(path)
    for rule in _CATEGORICAL_RULES:
        if not rule.matches(field_name, value):
            continue
        if rule.authorizable and _hidden_path_authorized(path, policy):
            return _FieldClassificationV1(
                exclusion_reason=None,
                hidden_authorized=True,
            )
        return _FieldClassificationV1(
            exclusion_reason=rule.reason,
            hidden_authorized=False,
        )
    return _FieldClassificationV1(
        exclusion_reason=None,
        hidden_authorized=False,
    )


def _is_allowlisted(path: str, policy: RedactionPolicyV1) -> bool:
    return any(_at_or_below(path, root) for root in policy.allowlisted_paths)


def _is_allowlist_container(path: str, policy: RedactionPolicyV1) -> bool:
    return any(_ancestor_of(path, root) for root in policy.allowlisted_paths)


_OMITTED = object()


def _field_entry(
    *,
    path: str,
    value: object,
    action: RedactionActionV1,
    reason: RedactionReasonV1,
    output: object = _OMITTED,
) -> FieldRedactionEntryV1:
    return FieldRedactionEntryV1(
        json_path=path,
        action=action,
        reason=reason,
        value_kind=_value_kind(value),
        source_sha256=hashlib.sha256(_canonical_json_bytes(value)).hexdigest(),
        output_sha256=(
            None
            if output is _OMITTED
            else hashlib.sha256(_canonical_json_bytes(output)).hexdigest()
        ),
    )


def _record_omitted_subtree(
    value: object,
    *,
    path: str,
    reason: RedactionReasonV1,
    policy: RedactionPolicyV1,
    entries: list[FieldRedactionEntryV1],
) -> None:
    classification = _classify_field(path, value, policy)
    current_reason = classification.exclusion_reason or reason
    entries.append(
        _field_entry(
            path=path,
            value=value,
            action=RedactionActionV1.OMIT,
            reason=current_reason,
        )
    )
    descendant_reason = RedactionReasonV1.ANCESTOR_EXCLUDED
    if type(value) is dict:
        for key in sorted(value, key=lambda item: item.encode("utf-8")):
            _record_omitted_subtree(
                value[key],
                path=_child_pointer(path, key),
                reason=descendant_reason,
                policy=policy,
                entries=entries,
            )
    elif type(value) is list:
        for index, item in enumerate(value):
            _record_omitted_subtree(
                item,
                path=_child_pointer(path, str(index)),
                reason=descendant_reason,
                policy=policy,
                entries=entries,
            )


def _redact_value(
    value: object,
    *,
    path: str,
    policy: RedactionPolicyV1,
    entries: list[FieldRedactionEntryV1],
) -> object:
    classification = _classify_field(path, value, policy)
    if classification.exclusion_reason is not None:
        _record_omitted_subtree(
            value,
            path=path,
            reason=classification.exclusion_reason,
            policy=policy,
            entries=entries,
        )
        return _OMITTED

    selected = _is_allowlisted(path, policy)
    container = _is_allowlist_container(path, policy)
    if not selected and not container:
        _record_omitted_subtree(
            value,
            path=path,
            reason=RedactionReasonV1.NOT_ALLOWLISTED,
            policy=policy,
            entries=entries,
        )
        return _OMITTED

    if type(value) is dict:
        output: object = {
            key: retained
            for key in sorted(value, key=lambda item: item.encode("utf-8"))
            if (
                retained := _redact_value(
                    value[key],
                    path=_child_pointer(path, key),
                    policy=policy,
                    entries=entries,
                )
            )
            is not _OMITTED
        }
    elif type(value) is list:
        output = [
            retained
            for index, item in enumerate(value)
            if (
                retained := _redact_value(
                    item,
                    path=_child_pointer(path, str(index)),
                    policy=policy,
                    entries=entries,
                )
            )
            is not _OMITTED
        ]
    else:
        output = value

    reason = (
        RedactionReasonV1.HIDDEN_REVEAL_EXPLICITLY_AUTHORIZED
        if classification.hidden_authorized
        else (
            RedactionReasonV1.PORTABLE_EVIDENCE_ALLOWLIST
            if selected
            else RedactionReasonV1.ALLOWLIST_CONTAINER
        )
    )
    entries.append(
        _field_entry(
            path=path,
            value=value,
            action=RedactionActionV1.RETAIN,
            reason=reason,
            output=output,
        )
    )
    return output


def redact_document(
    source_bytes: bytes,
    *,
    document_id: str,
    document_kind: PortableEvidenceKindV1,
    policy: RedactionPolicyV1,
) -> RedactedDocumentV1:
    """Redact one exact canonical portable-evidence envelope.

    The source envelope may contain any subset of the eight portable top-level
    roots.  Paths absent from the source do not produce synthetic manifest entries.
    """

    _identifier(document_id, "redaction document ID")
    if type(document_kind) is not PortableEvidenceKindV1:
        raise TypeError("redaction document kind must be PortableEvidenceKindV1")
    if type(policy) is not RedactionPolicyV1:
        raise TypeError("redaction policy must be RedactionPolicyV1")
    source = _canonical_object(source_bytes, "redaction source document")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    entries: list[FieldRedactionEntryV1] = []
    output = _redact_value(
        source,
        path="",
        policy=policy,
        entries=entries,
    )
    if type(output) is not dict:
        raise RuntimeError("redaction source root did not produce an object")
    output_bytes = _canonical_json_bytes(output)
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    canonical_entries = tuple(
        sorted(entries, key=lambda item: item.json_path.encode("utf-8"))
    )
    manifest = RedactionManifestV1(
        document_id=document_id,
        document_kind=document_kind,
        source_sha256=source_sha256,
        output_sha256=output_sha256,
        policy_id=policy.policy_id,
        policy_sha256=policy.sha256,
        entries=canonical_entries,
    )
    return RedactedDocumentV1(
        document_id=document_id,
        document_kind=document_kind,
        redacted_json=output_bytes.decode("ascii"),
        manifest=manifest,
    )


def load_field_redaction_entry(raw: bytes) -> FieldRedactionEntryV1:
    return FieldRedactionEntryV1.from_canonical_bytes(raw)


def load_redaction_manifest(raw: bytes) -> RedactionManifestV1:
    return RedactionManifestV1.from_canonical_bytes(raw)


def load_redaction_policy(raw: bytes) -> RedactionPolicyV1:
    return RedactionPolicyV1.from_canonical_bytes(raw)


def load_redacted_document(raw: bytes) -> RedactedDocumentV1:
    return RedactedDocumentV1.from_canonical_bytes(raw)
