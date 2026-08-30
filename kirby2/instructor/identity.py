"""Local pseudonymous-profile identity mappings for WO37-A.

The two storage planes in this module intentionally have different lifetimes:

* direct identity is kept only in ``DataPaths.identity_mappings`` and can be
  erased; and
* a deletion receipt is an immutable, pseudonymous-only artifact kept below
  ``DataPaths.evidence``.

Nothing here treats pseudonymization as anonymity.  A mapping is sensitive local
material.  Its filename is derived solely from an already-opaque profile ID, and
the deletion receipt does not retain the mapping's content digest because such a
digest would itself be derived from the deleted direct-identity payload.  Canonical
mapping IDs detect stale or malformed bytes but are not authentication against an
owner-level rewrite; owner-only filesystem permissions are the local trust boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from kirby2.research.paths import (
    ERASABLE_IDENTITY_AREA_IDS,
    IMMUTABLE_EVIDENCE_AREA_IDS,
    DataAreaId,
    DataPaths,
)

from .models import (
    InstructorProfile,
    LearnerProfile,
    ProfileKind,
    create_instructor_profile,
    create_learner_profile,
    profile_kind_for_id,
    require_profile_id,
)

if TYPE_CHECKING:
    from .consent import (
        ConsentRecordV1,
        ProfileDeletionDecisionV1,
        validate_profile_deletion_decision,
    )


DIRECT_IDENTIFIER_SCHEMA_ID = "kirby2.instructor.direct-identifier"
DIRECT_IDENTIFIER_SCHEMA_VERSION = 1
DIRECT_IDENTITY_SCHEMA_ID = "kirby2.instructor.direct-identity"
DIRECT_IDENTITY_SCHEMA_VERSION = 1
IDENTITY_MAPPING_SCHEMA_ID = "kirby2.instructor.identity-mapping"
IDENTITY_MAPPING_SCHEMA_VERSION = 1
IDENTITY_DELETION_RECEIPT_SCHEMA_ID = (
    "kirby2.instructor.identity-deletion-receipt"
)
IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION = 1
IDENTITY_MAPPING_AUTHORITY_POLICY = (
    "LOCAL_OWNER_PERMISSION_BOUNDARY_NOT_AUTHENTICATED"
)

IDENTITY_DELETION_RECEIPT_DIRECTORY = "identity-deletion-receipts"
OPAQUE_ENTROPY_BYTES = 32

# These are the only profile-related data planes eligible for a default package
# or export inventory.  The erasable mapping area requires an explicit, separately
# authorized operation and therefore cannot enter either default inventory.
DEFAULT_EXPORT_AREA_IDS: tuple[DataAreaId, ...] = (
    *IMMUTABLE_EVIDENCE_AREA_IDS,
)
DEFAULT_PACKAGE_AREA_IDS: tuple[DataAreaId, ...] = DEFAULT_EXPORT_AREA_IDS

if ERASABLE_IDENTITY_AREA_IDS != (DataAreaId.IDENTITY_MAPPINGS,):
    raise RuntimeError("identity mapping lifecycle declaration changed")

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_DIRECT_IDENTIFIER_KIND = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_RECEIPT_ID = re.compile(r"identity-deletion-receipt-[0-9a-f]{64}")
_FINAL_RECEIPT = re.compile(
    r"(identity-deletion-receipt-[0-9a-f]{64})\.json"
)
_PENDING_RECEIPT = re.compile(
    r"\.pending-(identity-deletion-receipt-[0-9a-f]{64})-[0-9a-f]{16}\.json"
)
_CONSENT_ID = re.compile(r"consent-[0-9a-f]{24}")
_CONSENT_DECISION_ID = re.compile(r"consent-decision-[0-9a-f]{24}")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"Z"
)
_MAX_DIRECT_VALUE_BYTES = 16 * 1024
_MAX_DIRECT_IDENTIFIERS = 64
_MAX_MAPPING_BYTES = 1 * 1024 * 1024


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
        raise ValueError("identity record is not strict canonical JSON") from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} field inventory differs from its V1 schema")


def _require_text(
    value: object,
    label: str,
    *,
    allow_none: bool = False,
    maximum_utf8_bytes: int = _MAX_DIRECT_VALUE_BYTES,
) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use canonical NFC text")
    if len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{label} exceeds its bounded local-storage size")
    if any(character == "\x00" or character in "\r\n" for character in value):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_utc_timestamp(value: object, label: str) -> str:
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{label} must be a strict UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a real UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must identify UTC")
    return value


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("identity JSON contains a duplicate object key")
        result[key] = value
    return result


def _load_canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} bytes must be exact bytes")
    if not raw or len(raw) > _MAX_MAPPING_BYTES:
        raise ValueError(f"{label} byte length is invalid")
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} bytes are not canonical JSON")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class DirectIdentifierV1:
    """One sensitive local identifier; never an evidence-plane value."""

    identifier_kind: str
    identifier_value: str

    schema_id: ClassVar[str] = DIRECT_IDENTIFIER_SCHEMA_ID
    schema_version: ClassVar[int] = DIRECT_IDENTIFIER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.identifier_kind) is not str
            or _DIRECT_IDENTIFIER_KIND.fullmatch(self.identifier_kind) is None
        ):
            raise ValueError(
                "direct identifier kind must be a lowercase semantic token"
            )
        _require_text(self.identifier_value, "direct identifier value")

    def __repr__(self) -> str:
        return (
            "DirectIdentifierV1(identifier_kind="
            f"{self.identifier_kind!r}, identifier_value=<redacted>)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "identifier_kind": self.identifier_kind,
            "identifier_value": self.identifier_value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DirectIdentifierV1:
        if not isinstance(value, Mapping):
            raise ValueError("direct identifier must be an object")
        _require_exact_keys(
            value,
            {"identifier_kind", "identifier_value", "schema_id", "schema_version"},
            "direct identifier",
        )
        if (
            value["schema_id"] != DIRECT_IDENTIFIER_SCHEMA_ID
            or type(value["schema_version"]) is not int
            or value["schema_version"] != DIRECT_IDENTIFIER_SCHEMA_VERSION
        ):
            raise ValueError("unsupported direct identifier schema")
        identifier_kind = value["identifier_kind"]
        identifier_value = value["identifier_value"]
        if type(identifier_kind) is not str or type(identifier_value) is not str:
            raise ValueError("direct identifier fields must be exact text")
        return cls(
            identifier_kind=identifier_kind,
            identifier_value=identifier_value,
        )


@dataclass(frozen=True, slots=True, repr=False)
class DirectIdentityV1:
    """Sensitive display and identifier data stored only in the mapping area."""

    display_name: str | None
    direct_identifiers: tuple[DirectIdentifierV1, ...] = ()

    schema_id: ClassVar[str] = DIRECT_IDENTITY_SCHEMA_ID
    schema_version: ClassVar[int] = DIRECT_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.display_name, "display name", allow_none=True)
        if type(self.direct_identifiers) is not tuple or any(
            type(item) is not DirectIdentifierV1
            for item in self.direct_identifiers
        ):
            raise TypeError(
                "direct identifiers must be an immutable DirectIdentifierV1 tuple"
            )
        if len(self.direct_identifiers) > _MAX_DIRECT_IDENTIFIERS:
            raise ValueError("direct identity has too many local identifiers")
        ordered = tuple(
            sorted(
                self.direct_identifiers,
                key=lambda item: (item.identifier_kind, item.identifier_value),
            )
        )
        if len(
            {
                (item.identifier_kind, item.identifier_value)
                for item in ordered
            }
        ) != len(ordered):
            raise ValueError("direct identifiers must be unique")
        object.__setattr__(self, "direct_identifiers", ordered)
        if self.display_name is None and not ordered:
            raise ValueError("direct identity must contain at least one local value")

    def __repr__(self) -> str:
        return (
            "DirectIdentityV1(display_name=<redacted>, "
            f"direct_identifiers={len(self.direct_identifiers)} redacted)"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "direct_identifiers": [item.as_dict() for item in self.direct_identifiers],
            "display_name": self.display_name,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DirectIdentityV1:
        if not isinstance(value, Mapping):
            raise ValueError("direct identity must be an object")
        _require_exact_keys(
            value,
            {"direct_identifiers", "display_name", "schema_id", "schema_version"},
            "direct identity",
        )
        if (
            value["schema_id"] != DIRECT_IDENTITY_SCHEMA_ID
            or type(value["schema_version"]) is not int
            or value["schema_version"] != DIRECT_IDENTITY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported direct identity schema")
        raw_identifiers = value["direct_identifiers"]
        if type(raw_identifiers) is not list or any(
            not isinstance(item, Mapping) for item in raw_identifiers
        ):
            raise ValueError("direct identifiers must be a JSON array")
        display_name = value["display_name"]
        if display_name is not None and type(display_name) is not str:
            raise ValueError("display name must be exact text or null")
        return cls(
            display_name=display_name,
            direct_identifiers=tuple(
                DirectIdentifierV1.from_dict(item)
                for item in raw_identifiers
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class IdentityMappingV1:
    """One separately erasable pseudonym-to-direct-identity mapping."""

    pseudonymous_profile_id: str
    profile_kind: ProfileKind
    profile_sha256: str
    direct_identity: DirectIdentityV1 = field(repr=False)

    schema_id: ClassVar[str] = IDENTITY_MAPPING_SCHEMA_ID
    schema_version: ClassVar[int] = IDENTITY_MAPPING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_profile_id(self.pseudonymous_profile_id)
        if type(self.profile_kind) is not ProfileKind:
            raise TypeError("identity mapping profile kind must be ProfileKind")
        if profile_kind_for_id(self.pseudonymous_profile_id) is not self.profile_kind:
            raise ValueError("identity mapping profile kind differs from its opaque ID")
        _require_sha256(self.profile_sha256, "profile digest")
        if _profile_for_id(self.pseudonymous_profile_id).profile_sha256 != (
            self.profile_sha256
        ):
            raise ValueError("profile digest differs from its canonical opaque profile")
        if type(self.direct_identity) is not DirectIdentityV1:
            raise TypeError("identity mapping requires DirectIdentityV1")

    def __repr__(self) -> str:
        return (
            "IdentityMappingV1(pseudonymous_profile_id="
            f"{self.pseudonymous_profile_id!r}, profile_kind={self.profile_kind!r}, "
            f"profile_sha256={self.profile_sha256!r}, direct_identity=<redacted>)"
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "direct_identity": self.direct_identity.as_dict(),
            "profile_kind": self.profile_kind.value,
            "profile_sha256": self.profile_sha256,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def mapping_id(self) -> str:
        # This content-derived identity is intentionally local to the sensitive
        # mapping object and is never copied to a deletion receipt or filename.
        return "identity-mapping-" + _canonical_sha256(self.identity_dict())

    @property
    def mapping_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {"mapping_id": self.mapping_id, **self.identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> IdentityMappingV1:
        value = _load_canonical_object(raw, "identity mapping")
        _require_exact_keys(
            value,
            {
                "direct_identity",
                "mapping_id",
                "profile_kind",
                "profile_sha256",
                "pseudonymous_profile_id",
                "schema_id",
                "schema_version",
            },
            "identity mapping",
        )
        if (
            value["schema_id"] != IDENTITY_MAPPING_SCHEMA_ID
            or type(value["schema_version"]) is not int
            or value["schema_version"] != IDENTITY_MAPPING_SCHEMA_VERSION
        ):
            raise ValueError("unsupported identity mapping schema")
        direct_identity = value["direct_identity"]
        if not isinstance(direct_identity, Mapping):
            raise ValueError("identity mapping direct identity must be an object")
        try:
            profile_kind = ProfileKind(value["profile_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("identity mapping profile kind is invalid") from error
        pseudonymous_profile_id = value["pseudonymous_profile_id"]
        profile_sha256 = value["profile_sha256"]
        if (
            type(pseudonymous_profile_id) is not str
            or type(profile_sha256) is not str
        ):
            raise ValueError("identity mapping profile fields must be exact text")
        mapping = cls(
            pseudonymous_profile_id=pseudonymous_profile_id,
            profile_kind=profile_kind,
            profile_sha256=profile_sha256,
            direct_identity=DirectIdentityV1.from_dict(direct_identity),
        )
        if (
            value["mapping_id"] != mapping.mapping_id
            or mapping.canonical_bytes() != raw
        ):
            raise ValueError("identity mapping identity or canonical bytes differ")
        return mapping


@dataclass(frozen=True, slots=True)
class IdentityDeletionReceiptV1:
    """Immutable proof of mapping erasure containing no deleted payload commitment."""

    pseudonymous_profile_id: str
    profile_kind: ProfileKind
    profile_sha256: str
    deletion_decision_id: str
    deletion_decision_sha256: str
    consent_id: str
    consent_sha256: str
    pseudonymous_evidence_retained: bool
    deletion_time_utc: str

    schema_id: ClassVar[str] = IDENTITY_DELETION_RECEIPT_SCHEMA_ID
    schema_version: ClassVar[int] = IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_profile_id(self.pseudonymous_profile_id)
        if type(self.profile_kind) is not ProfileKind:
            raise TypeError("deletion receipt profile kind must be ProfileKind")
        if profile_kind_for_id(self.pseudonymous_profile_id) is not self.profile_kind:
            raise ValueError("deletion receipt profile kind differs from its opaque ID")
        _require_sha256(self.profile_sha256, "deletion receipt profile digest")
        if _profile_for_id(self.pseudonymous_profile_id).profile_sha256 != (
            self.profile_sha256
        ):
            raise ValueError("receipt profile digest differs from its opaque profile")
        _require_text(
            self.deletion_decision_id,
            "deletion decision ID",
            maximum_utf8_bytes=256,
        )
        _require_sha256(
            self.deletion_decision_sha256,
            "deletion decision digest",
        )
        if _CONSENT_DECISION_ID.fullmatch(self.deletion_decision_id) is None:
            raise ValueError("deletion decision ID is invalid")
        _require_text(self.consent_id, "consent ID", maximum_utf8_bytes=256)
        if _CONSENT_ID.fullmatch(self.consent_id) is None:
            raise ValueError("consent ID is invalid")
        _require_sha256(self.consent_sha256, "consent digest")
        if type(self.pseudonymous_evidence_retained) is not bool:
            raise TypeError("pseudonymous evidence retention must be boolean")
        _require_utc_timestamp(self.deletion_time_utc, "deletion time")

    def identity_dict(self) -> dict[str, object]:
        return {
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "deletion_decision_id": self.deletion_decision_id,
            "deletion_decision_sha256": self.deletion_decision_sha256,
            "deletion_time_utc": self.deletion_time_utc,
            "mapping_disposition": "DELETED",
            "profile_kind": self.profile_kind.value,
            "profile_sha256": self.profile_sha256,
            "pseudonymous_evidence_action": "RETAINED_UNCHANGED"
            if self.pseudonymous_evidence_retained
            else "NOT_RETAINED_BY_THIS_DECISION",
            "pseudonymous_evidence_retained": self.pseudonymous_evidence_retained,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def receipt_id(self) -> str:
        return "identity-deletion-receipt-" + _canonical_sha256(
            self.identity_dict()
        )

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {"receipt_id": self.receipt_id, **self.identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> IdentityDeletionReceiptV1:
        value = _load_canonical_object(raw, "identity deletion receipt")
        _require_exact_keys(
            value,
            {
                "consent_id",
                "consent_sha256",
                "deletion_decision_id",
                "deletion_decision_sha256",
                "deletion_time_utc",
                "mapping_disposition",
                "profile_kind",
                "profile_sha256",
                "pseudonymous_evidence_action",
                "pseudonymous_evidence_retained",
                "pseudonymous_profile_id",
                "receipt_id",
                "schema_id",
                "schema_version",
            },
            "identity deletion receipt",
        )
        if (
            value["schema_id"] != IDENTITY_DELETION_RECEIPT_SCHEMA_ID
            or type(value["schema_version"]) is not int
            or value["schema_version"]
            != IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION
            or value["mapping_disposition"] != "DELETED"
        ):
            raise ValueError("unsupported identity deletion receipt schema")
        try:
            profile_kind = ProfileKind(value["profile_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("deletion receipt profile kind is invalid") from error
        retained = value["pseudonymous_evidence_retained"]
        if type(retained) is not bool:
            raise ValueError("deletion receipt retention value must be boolean")
        expected_action = (
            "RETAINED_UNCHANGED"
            if retained
            else "NOT_RETAINED_BY_THIS_DECISION"
        )
        if value["pseudonymous_evidence_action"] != expected_action:
            raise ValueError("deletion receipt evidence action is inconsistent")
        text_fields = (
            "pseudonymous_profile_id",
            "profile_sha256",
            "deletion_decision_id",
            "deletion_decision_sha256",
            "consent_id",
            "consent_sha256",
            "deletion_time_utc",
        )
        if any(type(value[field_name]) is not str for field_name in text_fields):
            raise ValueError("deletion receipt committed fields must be exact text")
        receipt = cls(
            pseudonymous_profile_id=value["pseudonymous_profile_id"],
            profile_kind=profile_kind,
            profile_sha256=value["profile_sha256"],
            deletion_decision_id=value["deletion_decision_id"],
            deletion_decision_sha256=value["deletion_decision_sha256"],
            consent_id=value["consent_id"],
            consent_sha256=value["consent_sha256"],
            pseudonymous_evidence_retained=retained,
            deletion_time_utc=value["deletion_time_utc"],
        )
        if (
            value["receipt_id"] != receipt.receipt_id
            or receipt.canonical_bytes() != raw
        ):
            raise ValueError("deletion receipt identity or canonical bytes differ")
        return receipt


@dataclass(frozen=True, slots=True)
class IdentityCreationV1:
    """In-memory result for a new system-entropy or audit-entropy profile."""

    profile: InstructorProfile | LearnerProfile
    mapping: IdentityMappingV1

    def __post_init__(self) -> None:
        if type(self.profile) not in {InstructorProfile, LearnerProfile}:
            raise TypeError("identity creation requires one typed profile")
        if self.profile.profile_id != self.mapping.pseudonymous_profile_id:
            raise ValueError("created profile and mapping IDs differ")
        if self.profile.profile_sha256 != self.mapping.profile_sha256:
            raise ValueError("created profile and mapping digests differ")


def generate_opaque_entropy() -> bytes:
    """Return fresh local entropy suitable for one pseudonymous profile ID."""

    return secrets.token_bytes(OPAQUE_ENTROPY_BYTES)


def _require_opaque_entropy(value: object) -> bytes:
    if type(value) is not bytes or len(value) < OPAQUE_ENTROPY_BYTES:
        raise ValueError(
            "opaque profile entropy must be at least "
            f"{OPAQUE_ENTROPY_BYTES} exact bytes"
        )
    return value


def create_local_learner_identity(
    paths: DataPaths,
    direct_identity: DirectIdentityV1,
    *,
    opaque_entropy: bytes | None = None,
) -> IdentityCreationV1:
    """Create a learner profile and its local mapping with no identity-derived ID."""

    entropy = (
        generate_opaque_entropy()
        if opaque_entropy is None
        else _require_opaque_entropy(opaque_entropy)
    )
    profile = create_learner_profile(entropy)
    mapping = create_identity_mapping(
        paths,
        profile,
        direct_identity,
        opaque_entropy=entropy,
    )
    return IdentityCreationV1(profile=profile, mapping=mapping)


def create_local_instructor_identity(
    paths: DataPaths,
    direct_identity: DirectIdentityV1,
    *,
    opaque_entropy: bytes | None = None,
) -> IdentityCreationV1:
    """Create an instructor profile and mapping with system entropy by default."""

    entropy = (
        generate_opaque_entropy()
        if opaque_entropy is None
        else _require_opaque_entropy(opaque_entropy)
    )
    profile = create_instructor_profile(entropy)
    mapping = create_identity_mapping(
        paths,
        profile,
        direct_identity,
        opaque_entropy=entropy,
    )
    return IdentityCreationV1(profile=profile, mapping=mapping)


def create_identity_mapping(
    paths: DataPaths,
    profile: InstructorProfile | LearnerProfile,
    direct_identity: DirectIdentityV1,
    *,
    opaque_entropy: bytes | None = None,
) -> IdentityMappingV1:
    """Persist one exclusive local mapping for an existing pseudonymous profile.

    Supplying ``opaque_entropy`` is optional proof that the caller created the
    profile from those opaque bytes.  Deterministic audits should supply it.  Normal
    callers can use ``create_local_*_identity`` to obtain system entropy here.
    """

    _require_paths(paths)
    if type(profile) not in {InstructorProfile, LearnerProfile}:
        raise TypeError("identity mapping requires a typed pseudonymous profile")
    if type(direct_identity) is not DirectIdentityV1:
        raise TypeError("identity mapping requires DirectIdentityV1")
    if opaque_entropy is not None:
        entropy = _require_opaque_entropy(opaque_entropy)
        derived = (
            create_instructor_profile(entropy)
            if type(profile) is InstructorProfile
            else create_learner_profile(entropy)
        )
        if derived.canonical_bytes() != profile.canonical_bytes():
            raise ValueError("opaque entropy does not derive the supplied profile")

    mapping = IdentityMappingV1(
        pseudonymous_profile_id=profile.profile_id,
        profile_kind=profile.profile_kind,
        profile_sha256=profile.profile_sha256,
        direct_identity=direct_identity,
    )
    mapping_bytes = mapping.canonical_bytes()
    if len(mapping_bytes) > _MAX_MAPPING_BYTES:
        raise ValueError("identity mapping exceeds its bounded local-storage size")
    paths.ensure((DataAreaId.IDENTITY_MAPPINGS,))
    paths.validate((DataAreaId.IDENTITY_MAPPINGS,))
    root_descriptor = _open_governed_root(paths)
    if root_descriptor is None:  # pragma: no cover - required open
        raise ValueError("governed data root is missing or unsafe")
    try:
        descriptor = _open_identity_area(paths, root_descriptor)
        try:
            with _exclusive_store_lock(descriptor):
                _recover_pending_identity_deletions_locked(
                    paths,
                    root_descriptor,
                    descriptor,
                )
                _refuse_tombstoned_profile_locked(
                    paths,
                    root_descriptor,
                    profile.profile_id,
                )
                _write_exclusive_at(
                    descriptor,
                    _mapping_filename(profile.profile_id),
                    mapping_bytes,
                    "identity mapping",
                )
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(root_descriptor)
    return mapping


def resolve_identity_mapping(
    paths: DataPaths,
    pseudonymous_profile_id: str,
) -> IdentityMappingV1:
    """Resolve one exact local mapping, serialized with cooperative deletion."""

    _require_paths(paths)
    require_profile_id(pseudonymous_profile_id)
    paths.validate((DataAreaId.IDENTITY_MAPPINGS,))
    root_descriptor = _open_governed_root(paths)
    if root_descriptor is None:  # pragma: no cover - required open
        raise ValueError("governed data root is missing or unsafe")
    try:
        descriptor = _open_identity_area(paths, root_descriptor)
        try:
            with _exclusive_store_lock(descriptor):
                raw = _read_regular_at(
                    descriptor,
                    _mapping_filename(pseudonymous_profile_id),
                    "identity mapping",
                )
        finally:
            os.close(descriptor)
    finally:
        os.close(root_descriptor)
    mapping = IdentityMappingV1.from_json_bytes(raw)
    if mapping.pseudonymous_profile_id != pseudonymous_profile_id:
        raise ValueError("stored mapping identity differs from its opaque filename")
    return mapping


def delete_identity_mapping(
    paths: DataPaths,
    pseudonymous_profile_id: str,
    deletion_decision: ProfileDeletionDecisionV1,
    current_consent: ConsentRecordV1,
    *,
    deletion_time_utc: str,
) -> IdentityDeletionReceiptV1:
    """Delete exactly one authorized mapping and persist a payload-free receipt."""

    from .consent import (
        ConsentRecordV1,
        ProfileDeletionDecisionV1,
        decide_profile_deletion,
        decide_retention_after_profile_deletion,
        validate_profile_deletion_decision,
    )

    _require_paths(paths)
    require_profile_id(pseudonymous_profile_id)
    deletion_time = _require_utc_timestamp(deletion_time_utc, "deletion time")
    if type(deletion_decision) is not ProfileDeletionDecisionV1:
        raise TypeError("mapping deletion requires ProfileDeletionDecisionV1")
    deletion_decision = validate_profile_deletion_decision(deletion_decision)
    if type(current_consent) is not ConsentRecordV1:
        raise TypeError("mapping deletion requires the current ConsentRecordV1")
    current_consent = ConsentRecordV1.from_json_bytes(
        current_consent.canonical_bytes()
    )
    expected_decision = decide_profile_deletion(
        current_consent,
        required_scope=deletion_decision.required_scope,
        requested_pseudonymous_evidence_retention=(
            deletion_decision.requested_pseudonymous_evidence_retention
        ),
        decision_time_utc=deletion_decision.decision_time_utc,
    )
    if expected_decision != deletion_decision:
        raise PermissionError(
            "deletion decision does not match the current consent revision"
        )
    decision_bytes = deletion_decision.canonical_bytes()
    if (
        hashlib.sha256(decision_bytes).hexdigest()
        != deletion_decision.decision_sha256
    ):
        raise ValueError("deletion decision canonical commitment differs")
    if deletion_decision.pseudonymous_profile_id != pseudonymous_profile_id:
        raise PermissionError("deletion decision targets a different profile")
    if deletion_decision.allowed is not True:
        raise PermissionError("profile deletion decision is not allowed")
    decision_time = _require_utc_timestamp(
        deletion_decision.decision_time_utc,
        "deletion decision time",
    )
    if _timestamp(deletion_time) < _timestamp(decision_time):
        raise ValueError("deletion time cannot precede its authorization decision")
    if deletion_decision.requested_pseudonymous_evidence_retention:
        retention_at_deletion = decide_retention_after_profile_deletion(
            current_consent,
            required_scope=deletion_decision.required_scope,
            decision_time_utc=deletion_time,
        )
        if not retention_at_deletion.allowed:
            raise PermissionError(
                "pseudonymous evidence retention is not authorized at deletion time"
            )

    # Authorization is complete before creating the receipt plane.  Pin both areas
    # from the same root generation before reading or deleting the mapping.
    paths.ensure((DataAreaId.EVIDENCE,))
    paths.validate((DataAreaId.IDENTITY_MAPPINGS, DataAreaId.EVIDENCE))
    root_descriptor = _open_governed_root(paths)
    if root_descriptor is None:  # pragma: no cover - required open
        raise ValueError("governed data root is missing or unsafe")
    try:
        identity_descriptor = _open_identity_area(paths, root_descriptor)
        try:
            with _exclusive_store_lock(identity_descriptor):
                _recover_pending_identity_deletions_locked(
                    paths,
                    root_descriptor,
                    identity_descriptor,
                )
                _refuse_tombstoned_profile_locked(
                    paths,
                    root_descriptor,
                    pseudonymous_profile_id,
                )
                filename = _mapping_filename(pseudonymous_profile_id)
                mapping_raw, mapping_metadata = _read_regular_at_with_metadata(
                    identity_descriptor,
                    filename,
                    "identity mapping",
                )
                mapping = IdentityMappingV1.from_json_bytes(mapping_raw)
                if mapping.pseudonymous_profile_id != pseudonymous_profile_id:
                    raise ValueError(
                        "stored mapping identity differs from its opaque filename"
                    )

                receipt = IdentityDeletionReceiptV1(
                    pseudonymous_profile_id=pseudonymous_profile_id,
                    profile_kind=mapping.profile_kind,
                    profile_sha256=mapping.profile_sha256,
                    deletion_decision_id=deletion_decision.decision_id,
                    deletion_decision_sha256=deletion_decision.decision_sha256,
                    consent_id=deletion_decision.consent_id,
                    consent_sha256=deletion_decision.consent_sha256,
                    pseudonymous_evidence_retained=(
                        deletion_decision.requested_pseudonymous_evidence_retention
                    ),
                    deletion_time_utc=deletion_time,
                )

                evidence_descriptor = _open_governed_area_from_root(
                    paths,
                    root_descriptor,
                    DataAreaId.EVIDENCE,
                    "evidence",
                )
                if evidence_descriptor is None:  # pragma: no cover - required open
                    raise ValueError("evidence area is missing or unsafe")
                try:
                    receipt_descriptor = _open_or_create_directory_at(
                        evidence_descriptor,
                        IDENTITY_DELETION_RECEIPT_DIRECTORY,
                    )
                finally:
                    os.close(evidence_descriptor)
                try:
                    receipt_filename = f"{receipt.receipt_id}.json"
                    if _entry_exists_at(receipt_descriptor, receipt_filename):
                        raise RuntimeError(
                            "deletion receipt already exists before mapping deletion"
                        )

                    # Stage immutable receipt bytes before the destructive step.
                    pending = (
                        f".pending-{receipt.receipt_id}-{secrets.token_hex(8)}.json"
                    )
                    _write_exclusive_at(
                        receipt_descriptor,
                        pending,
                        receipt.canonical_bytes(),
                        "pending deletion receipt",
                    )
                    mapping_descriptor: int | None = None
                    mapping_unlinked = False
                    mapping_alias_detected = False
                    try:
                        os.fsync(receipt_descriptor)
                        mapping_descriptor, current = _open_regular_descriptor_at(
                            identity_descriptor,
                            filename,
                            "identity mapping",
                        )
                        if (
                            current.st_dev != mapping_metadata.st_dev
                            or current.st_ino != mapping_metadata.st_ino
                            or not stat.S_ISREG(current.st_mode)
                            or current.st_nlink != 1
                        ):
                            raise RuntimeError(
                                "identity mapping changed before exact deletion"
                            )
                        os.unlink(filename, dir_fd=identity_descriptor)
                        mapping_unlinked = True
                        unlinked = os.fstat(mapping_descriptor)
                        if unlinked.st_nlink != 0:
                            mapping_alias_detected = True
                            raise RuntimeError(
                                "identity mapping still has a filesystem alias after unlink"
                            )
                        os.fsync(identity_descriptor)
                    except BaseException:
                        if not mapping_unlinked or mapping_alias_detected:
                            os.unlink(pending, dir_fd=receipt_descriptor)
                            os.fsync(receipt_descriptor)
                        raise
                    finally:
                        if mapping_descriptor is not None:
                            os.close(mapping_descriptor)

                    os.rename(
                        pending,
                        receipt_filename,
                        src_dir_fd=receipt_descriptor,
                        dst_dir_fd=receipt_descriptor,
                    )
                    os.fsync(receipt_descriptor)
                finally:
                    os.close(receipt_descriptor)
        finally:
            os.close(identity_descriptor)
    finally:
        os.close(root_descriptor)
    return receipt


def resolve_identity_deletion_receipt(
    paths: DataPaths,
    receipt_id: str,
) -> IdentityDeletionReceiptV1:
    """Load and verify one immutable deletion receipt by content-derived ID."""

    _require_paths(paths)
    _require_receipt_id(receipt_id)
    paths.validate((DataAreaId.EVIDENCE,))
    root_descriptor = _open_governed_root(paths)
    if root_descriptor is None:  # pragma: no cover - required open
        raise ValueError("governed data root is missing or unsafe")
    try:
        evidence_descriptor = _open_governed_area_from_root(
            paths,
            root_descriptor,
            DataAreaId.EVIDENCE,
            "evidence",
        )
        if evidence_descriptor is None:  # pragma: no cover - required open
            raise ValueError("evidence area is missing or unsafe")
        try:
            receipt_descriptor = _open_existing_directory_at(
                evidence_descriptor,
                IDENTITY_DELETION_RECEIPT_DIRECTORY,
                "identity deletion receipts",
            )
        finally:
            os.close(evidence_descriptor)
        try:
            filename = f"{receipt_id}.json"
            if not _entry_exists_at(receipt_descriptor, filename):
                os.close(receipt_descriptor)
                receipt_descriptor = -1
                recovered = _recover_pending_identity_deletions_from_root(
                    paths,
                    root_descriptor,
                )
                for receipt in recovered:
                    if receipt.receipt_id == receipt_id:
                        return receipt
                raise ValueError("unknown identity deletion receipt")
            raw = _read_regular_at(
                receipt_descriptor,
                filename,
                "identity deletion receipt",
            )
        finally:
            if receipt_descriptor >= 0:
                os.close(receipt_descriptor)
    finally:
        os.close(root_descriptor)
    receipt = IdentityDeletionReceiptV1.from_json_bytes(raw)
    if receipt.receipt_id != receipt_id:
        raise ValueError("deletion receipt identity differs from its filename")
    return receipt


def recover_pending_identity_deletions(
    paths: DataPaths,
) -> tuple[IdentityDeletionReceiptV1, ...]:
    """Finalize durable deletion intents after an interrupted receipt publish.

    A pending receipt is promoted only when its exact mapping path is absent.  A
    pending receipt that conflicts with a live mapping fails closed: it may represent
    either a pre-unlink interruption or an illicit post-unlink re-creation, and those
    states cannot be distinguished from the safe receipt alone.  Pending records
    contain no direct identity or mapping-derived commitment.
    """

    _require_paths(paths)
    root_descriptor = _open_governed_root(paths, optional=True)
    if root_descriptor is None:
        return ()
    try:
        return _recover_pending_identity_deletions_from_root(
            paths,
            root_descriptor,
        )
    finally:
        os.close(root_descriptor)


def identity_mapping_path(
    paths: DataPaths,
    pseudonymous_profile_id: str,
) -> Path:
    """Return a display path derived only from an opaque profile ID."""

    _require_paths(paths)
    require_profile_id(pseudonymous_profile_id)
    return paths.identity_mappings / _mapping_filename(pseudonymous_profile_id)


def identity_deletion_receipt_path(
    paths: DataPaths,
    receipt_id: str,
) -> Path:
    """Return a display path derived only from a receipt commitment."""

    _require_paths(paths)
    _require_receipt_id(receipt_id)
    return (
        paths.evidence
        / IDENTITY_DELETION_RECEIPT_DIRECTORY
        / f"{receipt_id}.json"
    )


def is_default_export_area(area_id: DataAreaId | str) -> bool:
    """Return the closed default profile-export area decision."""

    try:
        selected = area_id if isinstance(area_id, DataAreaId) else DataAreaId(area_id)
    except (TypeError, ValueError) as error:
        raise ValueError("unknown data area for default export policy") from error
    return selected in DEFAULT_EXPORT_AREA_IDS


def _require_paths(paths: object) -> DataPaths:
    if type(paths) is not DataPaths:
        raise TypeError("identity operations require the exact DataPaths provider")
    return paths


def _profile_for_id(
    pseudonymous_profile_id: str,
) -> InstructorProfile | LearnerProfile:
    kind = profile_kind_for_id(pseudonymous_profile_id)
    if kind is ProfileKind.INSTRUCTOR:
        return InstructorProfile(profile_id=pseudonymous_profile_id)
    return LearnerProfile(profile_id=pseudonymous_profile_id)


def _mapping_filename(profile_id: str) -> str:
    require_profile_id(profile_id)
    return f"{profile_id}.json"


def _require_receipt_id(receipt_id: object) -> str:
    if type(receipt_id) is not str or _RECEIPT_ID.fullmatch(receipt_id) is None:
        raise ValueError("invalid identity deletion receipt ID")
    return receipt_id


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "identity storage requires symlink-refusing directory descriptors"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_governed_root(
    paths: DataPaths,
    *,
    optional: bool = False,
) -> int | None:
    """Pin one root generation through no-follow descriptor traversal."""

    paths.validate(())
    flags = _directory_flags()
    anchor = Path(paths.root.anchor)
    try:
        current = os.open(anchor, flags)
    except OSError as error:  # pragma: no cover - platform/filesystem failure
        raise ValueError("filesystem anchor is missing or unsafe") from error
    components = paths.root.parts[1:]
    try:
        for component in components:
            try:
                following = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as error:
                if optional:
                    return None
                raise ValueError("governed data root is missing or unsafe") from error
            except OSError as error:
                raise ValueError("governed data root is missing or unsafe") from error
            previous = current
            current = following
            os.close(previous)
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):  # pragma: no cover - O_DIRECTORY
            raise ValueError("governed data root is not a real directory")
        descriptor = current
        current = -1
        return descriptor
    finally:
        if current >= 0:
            os.close(current)


def _open_governed_area_from_root(
    paths: DataPaths,
    root_descriptor: int,
    area_id: DataAreaId,
    label: str,
    *,
    optional: bool = False,
) -> int | None:
    """Open one area from an already-pinned root generation."""

    try:
        current = os.dup(root_descriptor)
    except OSError as error:  # pragma: no cover - descriptor exhaustion
        raise ValueError("governed data root descriptor cannot be duplicated") from error
    flags = _directory_flags()
    try:
        for component in paths.area_children[area_id].split("/"):
            try:
                following = os.open(component, flags, dir_fd=current)
            except FileNotFoundError as error:
                if optional:
                    return None
                raise ValueError(f"{label} area is missing or unsafe") from error
            except OSError as error:
                raise ValueError(f"{label} area is missing or unsafe") from error
            previous = current
            current = following
            os.close(previous)
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):  # pragma: no cover - O_DIRECTORY
            raise ValueError(f"{label} area is not a real directory")
        descriptor = current
        current = -1
        return descriptor
    finally:
        if current >= 0:
            os.close(current)


def _open_identity_area(paths: DataPaths, root_descriptor: int) -> int:
    """Open and tighten the sensitive mapping directory to owner-only access."""

    descriptor = _open_governed_area_from_root(
        paths,
        root_descriptor,
        DataAreaId.IDENTITY_MAPPINGS,
        "identity mappings",
    )
    if descriptor is None:  # pragma: no cover - required open
        raise ValueError("identity mappings area is missing or unsafe")
    return _secure_private_directory_descriptor(
        descriptor,
        "identity mapping area",
    )


def _open_optional_identity_area(
    paths: DataPaths,
    root_descriptor: int,
) -> int | None:
    descriptor = _open_governed_area_from_root(
        paths,
        root_descriptor,
        DataAreaId.IDENTITY_MAPPINGS,
        "identity mappings",
        optional=True,
    )
    if descriptor is None:
        return None
    return _secure_private_directory_descriptor(
        descriptor,
        "identity mapping area",
    )


def _secure_private_directory_descriptor(descriptor: int, label: str) -> int:
    """Force one already-open sensitive directory to exact owner-only mode."""

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a real directory")
        try:
            os.fchmod(descriptor, 0o700)
        except OSError as error:
            raise PermissionError(f"{label} permissions cannot be secured") from error
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise PermissionError(f"{label} must use mode 0700")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _exclusive_store_lock:
    """Descriptor-relative advisory lock for cooperating identity operations."""

    __slots__ = ("_area_descriptor", "_lock_descriptor")

    def __init__(self, area_descriptor: int) -> None:
        self._area_descriptor = area_descriptor
        self._lock_descriptor: int | None = None

    def __enter__(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(
                ".identity-store.lock",
                flags,
                0o600,
                dir_fd=self._area_descriptor,
            )
        except OSError as error:
            raise ValueError("identity store lock is unsafe") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError(
                    "identity store lock must be one private regular file"
                )
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise PermissionError("identity store lock must use mode 0600")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            os.close(descriptor)
            raise PermissionError(
                "identity store lock cannot be secured or acquired"
            ) from error
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_descriptor = descriptor

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._lock_descriptor
        self._lock_descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _open_or_create_directory_at(parent_descriptor: int, name: str) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        return _secure_private_directory_descriptor(
            descriptor,
            "identity deletion receipt directory",
        )
    except FileNotFoundError:
        created = False
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ValueError("receipt directory cannot be created safely") from error
        if created:
            try:
                os.fsync(parent_descriptor)
            except OSError as error:
                raise ValueError(
                    "receipt directory creation cannot be made durable"
                ) from error
        return _open_existing_directory_at(
            parent_descriptor,
            name,
            "identity deletion receipts",
        )
    except OSError as error:
        raise ValueError("receipt directory is unsafe") from error


def _open_existing_directory_at(
    parent_descriptor: int,
    name: str,
    label: str,
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError(f"{label} directory is missing or unsafe") from error
    return _secure_private_directory_descriptor(descriptor, label)


def _write_exclusive_at(
    directory_descriptor: int,
    filename: str,
    payload: bytes,
    label: str,
) -> None:
    if type(payload) is not bytes:
        raise TypeError(f"{label} payload must be exact bytes")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(
            filename,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except FileExistsError as error:
        raise FileExistsError(f"{label} already exists") from error
    except OSError as error:
        raise ValueError(f"{label} cannot be created safely") from error
    succeeded = False
    try:
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise PermissionError(f"{label} must use mode 0600")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(f"short write while persisting {label}")
            offset += written
        os.fsync(descriptor)
        succeeded = True
    finally:
        os.close(descriptor)
        if not succeeded:
            try:
                os.unlink(filename, dir_fd=directory_descriptor)
            except OSError:
                pass


def _read_regular_at(
    directory_descriptor: int,
    filename: str,
    label: str,
) -> bytes:
    raw, _ = _read_regular_at_with_metadata(
        directory_descriptor,
        filename,
        label,
    )
    return raw


def _read_regular_at_with_metadata(
    directory_descriptor: int,
    filename: str,
    label: str,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except FileNotFoundError as error:
        raise ValueError(f"unknown {label}") from error
    except OSError as error:
        raise ValueError(f"{label} is missing or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must not have filesystem aliases")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError(f"{label} permits group or other access")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_MAPPING_BYTES:
            raise ValueError(f"{label} byte length is invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                raise ValueError(f"{label} changed during its exact read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} grew during its exact read")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or not stat.S_ISREG(final_metadata.st_mode)
            or final_metadata.st_nlink != 1
            or stat.S_IMODE(final_metadata.st_mode) & 0o077
        ):
            raise ValueError(f"{label} changed during its exact read")
        return b"".join(chunks), final_metadata
    finally:
        os.close(descriptor)


def _open_regular_descriptor_at(
    directory_descriptor: int,
    filename: str,
    label: str,
) -> tuple[int, os.stat_result]:
    """Open one unaliased private regular file and keep its inode pinned."""

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except FileNotFoundError as error:
        raise ValueError(f"unknown {label}") from error
    except OSError as error:
        raise ValueError(f"{label} is missing or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must not have filesystem aliases")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError(f"{label} permits group or other access")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_MAPPING_BYTES:
            raise ValueError(f"{label} byte length is invalid")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, metadata


def _open_optional_receipt_directory(
    paths: DataPaths,
    root_descriptor: int,
) -> int | None:
    evidence_descriptor = _open_governed_area_from_root(
        paths,
        root_descriptor,
        DataAreaId.EVIDENCE,
        "evidence",
        optional=True,
    )
    if evidence_descriptor is None:
        return None
    try:
        if not _entry_exists_at(
            evidence_descriptor,
            IDENTITY_DELETION_RECEIPT_DIRECTORY,
        ):
            return None
        return _open_existing_directory_at(
            evidence_descriptor,
            IDENTITY_DELETION_RECEIPT_DIRECTORY,
            "identity deletion receipts",
        )
    finally:
        os.close(evidence_descriptor)


def _receipt_names(receipt_descriptor: int) -> tuple[str, ...]:
    with os.scandir(receipt_descriptor) as entries:
        names = tuple(sorted(entry.name for entry in entries))
    if any(
        _FINAL_RECEIPT.fullmatch(name) is None
        and _PENDING_RECEIPT.fullmatch(name) is None
        for name in names
    ):
        raise ValueError("identity deletion receipt inventory is unsafe")
    return names


def _read_named_deletion_receipt(
    receipt_descriptor: int,
    filename: str,
    *,
    pending: bool,
) -> IdentityDeletionReceiptV1:
    pattern = _PENDING_RECEIPT if pending else _FINAL_RECEIPT
    match = pattern.fullmatch(filename)
    if match is None:
        raise ValueError("identity deletion receipt filename is invalid")
    label = (
        "pending identity deletion receipt"
        if pending
        else "identity deletion receipt"
    )
    raw = _read_regular_at(receipt_descriptor, filename, label)
    receipt = IdentityDeletionReceiptV1.from_json_bytes(raw)
    if match.group(1) != receipt.receipt_id:
        raise ValueError("deletion receipt identity differs from its filename")
    return receipt


def _recover_pending_identity_deletions_from_root(
    paths: DataPaths,
    root_descriptor: int,
) -> tuple[IdentityDeletionReceiptV1, ...]:
    """Recover within one already-pinned governed root generation."""

    identity_descriptor = _open_optional_identity_area(paths, root_descriptor)
    if identity_descriptor is None:
        return ()
    try:
        with _exclusive_store_lock(identity_descriptor):
            return _recover_pending_identity_deletions_locked(
                paths,
                root_descriptor,
                identity_descriptor,
            )
    finally:
        os.close(identity_descriptor)


def _recover_pending_identity_deletions_locked(
    paths: DataPaths,
    root_descriptor: int,
    identity_descriptor: int,
) -> tuple[IdentityDeletionReceiptV1, ...]:
    """Recover pending receipts while the caller holds the identity-store lock."""

    receipt_descriptor = _open_optional_receipt_directory(
        paths,
        root_descriptor,
    )
    if receipt_descriptor is None:
        return ()
    recovered: dict[str, IdentityDeletionReceiptV1] = {}
    try:
        pending_names = tuple(
            name
            for name in _receipt_names(receipt_descriptor)
            if _PENDING_RECEIPT.fullmatch(name) is not None
        )
        for pending_name in pending_names:
            receipt = _read_named_deletion_receipt(
                receipt_descriptor,
                pending_name,
                pending=True,
            )
            mapping_filename = _mapping_filename(
                receipt.pseudonymous_profile_id
            )
            if _entry_exists_at(identity_descriptor, mapping_filename):
                raise RuntimeError(
                    "pending deletion receipt conflicts with a live identity mapping"
                )
            raw = receipt.canonical_bytes()
            receipt_filename = f"{receipt.receipt_id}.json"
            if _entry_exists_at(receipt_descriptor, receipt_filename):
                existing = _read_regular_at(
                    receipt_descriptor,
                    receipt_filename,
                    "identity deletion receipt",
                )
                if existing != raw:
                    raise RuntimeError(
                        "pending deletion receipt collides with other bytes"
                    )
                os.unlink(pending_name, dir_fd=receipt_descriptor)
            else:
                os.rename(
                    pending_name,
                    receipt_filename,
                    src_dir_fd=receipt_descriptor,
                    dst_dir_fd=receipt_descriptor,
                )
            os.fsync(receipt_descriptor)
            recovered[receipt.receipt_id] = receipt
    finally:
        os.close(receipt_descriptor)
    return tuple(recovered[key] for key in sorted(recovered))


def _refuse_tombstoned_profile_locked(
    paths: DataPaths,
    root_descriptor: int,
    pseudonymous_profile_id: str,
) -> None:
    """Permanently refuse re-use of a profile with a deletion receipt."""

    receipt_descriptor = _open_optional_receipt_directory(
        paths,
        root_descriptor,
    )
    if receipt_descriptor is None:
        return
    try:
        for filename in _receipt_names(receipt_descriptor):
            pending = _PENDING_RECEIPT.fullmatch(filename) is not None
            receipt = _read_named_deletion_receipt(
                receipt_descriptor,
                filename,
                pending=pending,
            )
            if receipt.pseudonymous_profile_id == pseudonymous_profile_id:
                raise PermissionError(
                    "deleted pseudonymous profile IDs are permanent tombstones"
                )
    finally:
        os.close(receipt_descriptor)


def _entry_exists_at(directory_descriptor: int, filename: str) -> bool:
    try:
        os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError("artifact existence cannot be checked safely") from error
    return True


__all__ = [
    "DEFAULT_EXPORT_AREA_IDS",
    "DEFAULT_PACKAGE_AREA_IDS",
    "DIRECT_IDENTIFIER_SCHEMA_ID",
    "DIRECT_IDENTIFIER_SCHEMA_VERSION",
    "DIRECT_IDENTITY_SCHEMA_ID",
    "DIRECT_IDENTITY_SCHEMA_VERSION",
    "IDENTITY_DELETION_RECEIPT_DIRECTORY",
    "IDENTITY_DELETION_RECEIPT_SCHEMA_ID",
    "IDENTITY_DELETION_RECEIPT_SCHEMA_VERSION",
    "IDENTITY_MAPPING_SCHEMA_ID",
    "IDENTITY_MAPPING_SCHEMA_VERSION",
    "IDENTITY_MAPPING_AUTHORITY_POLICY",
    "OPAQUE_ENTROPY_BYTES",
    "DirectIdentifierV1",
    "DirectIdentityV1",
    "IdentityCreationV1",
    "IdentityDeletionReceiptV1",
    "IdentityMappingV1",
    "create_identity_mapping",
    "create_local_instructor_identity",
    "create_local_learner_identity",
    "delete_identity_mapping",
    "generate_opaque_entropy",
    "identity_deletion_receipt_path",
    "identity_mapping_path",
    "is_default_export_area",
    "recover_pending_identity_deletions",
    "resolve_identity_deletion_receipt",
    "resolve_identity_mapping",
]
