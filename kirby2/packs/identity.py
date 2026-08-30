"""Content-derived logical and transport identities for Kirby2 packs."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from .formats import (
    canonical_json_bytes,
    canonical_manifest_bytes,
    inspect_payload_format_claim,
    require_sha256,
)


PACK_IDENTITY_PROJECTION_SCHEMA_ID = "KIRBY2_PACK_IDENTITY_PROJECTION_V1"
PACK_IDENTITY_PROJECTION_SCHEMA_VERSION = 1
PACK_IDENTITY_ALGORITHM = "SHA256_CANONICAL_PACK_IDENTITY_V1"
CREATOR_ID_ALGORITHM = "SHA256_CANONICAL_CREATOR_METADATA_V1"
TRANSPORT_IDENTITY_ALGORITHM = "SHA256_EXACT_ARCHIVE_BYTES_V1"


@dataclass(frozen=True, slots=True)
class PackPayloadIdentityVerificationV1:
    """Verified bytes/inventory identity, not a WO39-B parser-safety result."""

    pack_id: str
    inventory_sha256: str
    file_count: int
    total_byte_count: int

    def __post_init__(self) -> None:
        require_sha256(self.pack_id, "verified pack ID")
        require_sha256(self.inventory_sha256, "verified inventory digest")
        if type(self.file_count) is not int or self.file_count <= 0:
            raise ValueError("verified pack file count must be positive")
        if type(self.total_byte_count) is not int or self.total_byte_count <= 0:
            raise ValueError("verified pack byte count must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "file_count": self.file_count,
            "inventory_sha256": self.inventory_sha256,
            "pack_id": self.pack_id,
            "schema_id": "KIRBY2_PACK_PAYLOAD_IDENTITY_VERIFICATION_V1",
            "schema_version": 1,
            "total_byte_count": self.total_byte_count,
        }


@dataclass(frozen=True, slots=True)
class UnverifiedPackTransportIdentityV1:
    """Exact transport descriptor that makes no archive-safety claim."""

    pack_id: str
    manifest_sha256: str
    transport_sha256: str
    archive_byte_count: int

    def __post_init__(self) -> None:
        require_sha256(self.pack_id, "archive logical pack ID")
        require_sha256(
            self.manifest_sha256,
            "archive exact canonical manifest digest",
        )
        require_sha256(self.transport_sha256, "archive transport digest")
        if type(self.archive_byte_count) is not int or self.archive_byte_count <= 0:
            raise ValueError("archive byte count must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_byte_count": self.archive_byte_count,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "schema_id": "KIRBY2_UNVERIFIED_PACK_TRANSPORT_IDENTITY_V1",
            "schema_version": 1,
            "transport_sha256": self.transport_sha256,
        }


def canonical_creator_metadata_bytes(creator: object) -> bytes:
    """Encode only creator metadata; ``creator_id`` cannot hash itself."""

    from .models import PackCreatorV1

    if type(creator) is not PackCreatorV1:
        raise TypeError("creator identity requires PackCreatorV1")
    return canonical_json_bytes(creator.metadata_dict())


def derive_creator_id(creator: object) -> str:
    """Derive an identity key, never a proof of authorship or authenticity."""

    return hashlib.sha256(canonical_creator_metadata_bytes(creator)).hexdigest()


def pack_identity_projection(manifest: object) -> dict[str, object]:
    """Return the sole self-reference-free WO39-A logical identity projection."""

    from .models import PackManifestV1

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack identity requires PackManifestV1")
    return {
        "algorithm": PACK_IDENTITY_ALGORITHM,
        "inventory": [item.as_dict() for item in manifest.inventory],
        "manifest_identity": manifest.identity_dict(),
        "schema_id": PACK_IDENTITY_PROJECTION_SCHEMA_ID,
        "schema_version": PACK_IDENTITY_PROJECTION_SCHEMA_VERSION,
    }


def canonical_pack_identity_bytes(manifest: object) -> bytes:
    return canonical_json_bytes(pack_identity_projection(manifest))


def derive_pack_id(manifest: object) -> str:
    return hashlib.sha256(canonical_pack_identity_bytes(manifest)).hexdigest()


def verify_pack_id(manifest: object, declared_pack_id: object | None = None) -> str:
    """Recompute and optionally compare one full logical pack digest."""

    expected = derive_pack_id(manifest)
    if declared_pack_id is None:
        declared_pack_id = getattr(manifest, "pack_id", None)
    actual = require_sha256(declared_pack_id, "declared pack ID")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("declared pack ID differs from canonical pack identity")
    return expected


def inventory_sha256(manifest: object) -> str:
    from .models import PackManifestV1

    if type(manifest) is not PackManifestV1:
        raise TypeError("inventory identity requires PackManifestV1")
    return hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in manifest.inventory])
    ).hexdigest()


def verify_pack_payload_identity(
    manifest: object,
    payloads: Mapping[str, bytes],
) -> PackPayloadIdentityVerificationV1:
    """Verify a complete exact-byte inventory and screen its format declarations.

    This function is a logical-identity check only.  It is not extraction approval,
    complete Parquet/media validation, or activation eligibility; WO39-B supplies
    those bounded hostile-input decisions.
    """

    from .models import PackManifestV1

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack payload verification requires PackManifestV1")
    if not isinstance(payloads, Mapping):
        raise TypeError("pack payloads must be a path-to-bytes mapping")
    snapshot = dict(payloads)
    if any(type(path) is not str or type(raw) is not bytes for path, raw in snapshot.items()):
        raise TypeError("pack payload mapping requires exact string paths and bytes")

    expected_paths = tuple(item.path for item in manifest.inventory)
    actual_paths = tuple(sorted(snapshot, key=lambda item: item.encode("utf-8")))
    if actual_paths != expected_paths:
        missing = sorted(set(expected_paths) - set(actual_paths))
        extra = sorted(set(actual_paths) - set(expected_paths))
        raise ValueError(
            f"pack payload inventory differs: missing={missing}, extra={extra}"
        )

    total = 0
    for item in manifest.inventory:
        raw = snapshot[item.path]
        if len(raw) != item.byte_count:
            raise ValueError(f"pack payload {item.path!r} byte count differs")
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual_sha256, item.sha256):
            raise ValueError(f"pack payload {item.path!r} digest differs")
        inspect_payload_format_claim(
            raw,
            path=item.path,
            content_format=item.content_format.value,
            media_type=item.media_type,
            schema_id=item.schema_id,
        )
        total += len(raw)

    return PackPayloadIdentityVerificationV1(
        pack_id=verify_pack_id(manifest),
        inventory_sha256=inventory_sha256(manifest),
        file_count=len(manifest.inventory),
        total_byte_count=total,
    )


def transport_sha256(archive_bytes: bytes) -> str:
    if type(archive_bytes) is not bytes or not archive_bytes:
        raise ValueError("pack transport identity requires nonempty exact archive bytes")
    return hashlib.sha256(archive_bytes).hexdigest()


def describe_archive_transport(
    manifest: object,
    archive_bytes: bytes,
) -> UnverifiedPackTransportIdentityV1:
    """Describe candidate transport bytes without claiming container verification.

    WO39-A keeps the exact-byte digest separate from logical identity.  Parsing the
    archive, matching its embedded manifest, and verifying every member belong to
    WO39-B; consumers must not treat this descriptor as that later safety result.
    """

    pack_id = verify_pack_id(manifest)
    manifest_bytes = canonical_manifest_bytes(manifest)
    return UnverifiedPackTransportIdentityV1(
        pack_id=pack_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        transport_sha256=transport_sha256(archive_bytes),
        archive_byte_count=len(archive_bytes),
    )


__all__ = [
    "CREATOR_ID_ALGORITHM",
    "PACK_IDENTITY_ALGORITHM",
    "PACK_IDENTITY_PROJECTION_SCHEMA_ID",
    "PACK_IDENTITY_PROJECTION_SCHEMA_VERSION",
    "TRANSPORT_IDENTITY_ALGORITHM",
    "PackPayloadIdentityVerificationV1",
    "UnverifiedPackTransportIdentityV1",
    "canonical_creator_metadata_bytes",
    "canonical_pack_identity_bytes",
    "describe_archive_transport",
    "derive_creator_id",
    "derive_pack_id",
    "inventory_sha256",
    "pack_identity_projection",
    "transport_sha256",
    "verify_pack_id",
    "verify_pack_payload_identity",
]
