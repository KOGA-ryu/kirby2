"""Bounded, no-follow staging for preflighted ``.k2pack`` transports.

This module stops at an activation-eligible *staging capability*.  It does not
install, activate, register, quarantine, or otherwise publish pack content.
Every capability is bound to one exact transport, manifest, validation policy,
trusted staging root, and live stage inode.  Consumers must revalidate it at the
point of use.  The boundary treats archive-controlled bytes as hostile; a separate
process already running as the same operating-system user is outside its isolation
model because portable POSIX deletion remains name-based after the final inode check.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar

from .archive import (
    PackArchiveMemberV1,
    PackArchivePreflightV1,
    _open_verified_archive_context,
    _read_verified_context_member_bytes,
    preflight_pack_archive_bytes,
)
from .formats import (
    K2PACK_MANIFEST_PATH,
    canonical_json_bytes,
    canonical_manifest_bytes,
    load_canonical_json_bytes,
    load_manifest_bytes,
    require_sha256,
)
from .identity import inventory_sha256, transport_sha256
from .models import PackFileV1, PackManifestV1
from .validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackRefusalCodeV1,
    PackValidationLimitsV1,
    PackValidationPhaseV1,
    PackValidationRefused,
    refuse,
    validate_structural_payload,
    validation_policy_id,
)


PACK_STAGE_VERIFICATION_SCHEMA_ID = "KIRBY2_PACK_STAGE_VERIFICATION_V1"
PACK_STAGE_VERIFICATION_SCHEMA_VERSION = 1
PACK_STAGE_CAPABILITY_SCHEMA_ID = "KIRBY2_ACTIVATION_ELIGIBLE_PACK_STAGE_V1"
PACK_STAGE_CAPABILITY_SCHEMA_VERSION = 1
_STAGE_PREFIX = ".k2pack-stage-"
_IO_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PackStageVerificationV1:
    """Canonical verification of exact bytes in one pinned local stage."""

    pack_id: str
    transport_sha256: str
    archive_byte_count: int
    manifest_sha256: str
    manifest_byte_count: int
    inventory_sha256: str
    validation_policy_id: str
    staged_tree_sha256: str
    payload_file_count: int
    payload_byte_count: int
    staging_root_device: int
    staging_root_inode: int
    stage_device: int
    stage_inode: int

    schema_id: ClassVar[str] = PACK_STAGE_VERIFICATION_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_STAGE_VERIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_sha256(self.pack_id, "staged pack ID")
        require_sha256(self.transport_sha256, "staged transport digest")
        require_sha256(self.manifest_sha256, "staged manifest digest")
        require_sha256(self.inventory_sha256, "staged inventory digest")
        require_sha256(self.validation_policy_id, "staged validation-policy digest")
        require_sha256(self.staged_tree_sha256, "staged-tree digest")
        _require_positive_integer(self.archive_byte_count, "staged archive byte count")
        _require_positive_integer(self.manifest_byte_count, "staged manifest byte count")
        _require_positive_integer(self.payload_file_count, "staged payload file count")
        _require_positive_integer(self.payload_byte_count, "staged payload byte count")
        _require_nonnegative_integer(
            self.staging_root_device, "staging-root device number"
        )
        _require_positive_integer(self.staging_root_inode, "staging-root inode")
        _require_nonnegative_integer(self.stage_device, "pack-stage device number")
        _require_positive_integer(self.stage_inode, "pack-stage inode")

    @property
    def staged_file_count(self) -> int:
        return self.payload_file_count + 1

    @property
    def total_staged_byte_count(self) -> int:
        return self.manifest_byte_count + self.payload_byte_count

    @property
    def file_count(self) -> int:
        return self.payload_file_count

    @property
    def total_byte_count(self) -> int:
        return self.payload_byte_count

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_byte_count": self.archive_byte_count,
            "inventory_sha256": self.inventory_sha256,
            "manifest_byte_count": self.manifest_byte_count,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "payload_byte_count": self.payload_byte_count,
            "payload_file_count": self.payload_file_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "stage_device": self.stage_device,
            "stage_inode": self.stage_inode,
            "staged_tree_sha256": self.staged_tree_sha256,
            "staging_root_device": self.staging_root_device,
            "staging_root_inode": self.staging_root_inode,
            "transport_sha256": self.transport_sha256,
            "validation_policy_id": self.validation_policy_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def verification_sha256(self) -> str:
        return self.sha256

    @classmethod
    def from_dict(cls, value: object) -> PackStageVerificationV1:
        if type(value) is not dict:
            raise TypeError("pack-stage verification must be one exact object")
        expected = {
            "archive_byte_count",
            "inventory_sha256",
            "manifest_byte_count",
            "manifest_sha256",
            "pack_id",
            "payload_byte_count",
            "payload_file_count",
            "schema_id",
            "schema_version",
            "stage_device",
            "stage_inode",
            "staged_tree_sha256",
            "staging_root_device",
            "staging_root_inode",
            "transport_sha256",
            "validation_policy_id",
        }
        if set(value) != expected or any(type(key) is not str for key in value):
            raise ValueError("pack-stage verification fields differ from schema")
        if type(value["schema_id"]) is not str or value["schema_id"] != cls.schema_id:
            raise ValueError("pack-stage verification schema ID is unsupported")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != cls.schema_version
        ):
            raise ValueError("pack-stage verification schema version is unsupported")
        text_fields = (
            "pack_id",
            "transport_sha256",
            "manifest_sha256",
            "inventory_sha256",
            "validation_policy_id",
            "staged_tree_sha256",
        )
        integer_fields = (
            "archive_byte_count",
            "manifest_byte_count",
            "payload_file_count",
            "payload_byte_count",
            "staging_root_device",
            "staging_root_inode",
            "stage_device",
            "stage_inode",
        )
        if any(type(value[field]) is not str for field in text_fields):
            raise TypeError("pack-stage verification digest fields must be strings")
        if any(type(value[field]) is not int for field in integer_fields):
            raise TypeError("pack-stage verification count fields must be integers")
        restored = cls(
            pack_id=value["pack_id"],
            transport_sha256=value["transport_sha256"],
            archive_byte_count=value["archive_byte_count"],
            manifest_sha256=value["manifest_sha256"],
            manifest_byte_count=value["manifest_byte_count"],
            inventory_sha256=value["inventory_sha256"],
            validation_policy_id=value["validation_policy_id"],
            staged_tree_sha256=value["staged_tree_sha256"],
            payload_file_count=value["payload_file_count"],
            payload_byte_count=value["payload_byte_count"],
            staging_root_device=value["staging_root_device"],
            staging_root_inode=value["staging_root_inode"],
            stage_device=value["stage_device"],
            stage_inode=value["stage_inode"],
        )
        if restored.as_dict() != value:
            raise ValueError("pack-stage verification did not round-trip exactly")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> PackStageVerificationV1:
        restored = cls.from_dict(
            load_canonical_json_bytes(raw, "pack-stage verification")
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("pack-stage verification bytes are not canonical")
        return restored


@dataclass(frozen=True, slots=True)
class ActivationEligiblePackStageV1:
    """A stale-able local stage proof, never standalone activation authority.

    The record is intentionally inspectable and therefore constructible.  WO39-C
    must call :func:`revalidate_pack_stage` at its descriptor-relative activation
    boundary; the type name alone is not a live-filesystem verdict.
    """

    staging_root: str
    stage_name: str
    manifest: PackManifestV1
    preflight: PackArchivePreflightV1
    verification: PackStageVerificationV1

    schema_id: ClassVar[str] = PACK_STAGE_CAPABILITY_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_STAGE_CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        root = _require_canonical_absolute_path(
            self.staging_root, "activation-eligible staging root"
        )
        if str(root) != self.staging_root:
            raise ValueError("activation-eligible staging root is not canonical")
        _require_stage_name(self.stage_name)
        if type(self.manifest) is not PackManifestV1:
            raise TypeError("activation-eligible stage manifest is invalid")
        if type(self.preflight) is not PackArchivePreflightV1:
            raise TypeError("activation-eligible archive preflight is invalid")
        if type(self.verification) is not PackStageVerificationV1:
            raise TypeError("activation-eligible stage verification is invalid")
        if self.preflight.manifest != self.manifest:
            raise ValueError("stage preflight and manifest differ")
        if self.preflight.pack_id != self.manifest.pack_id:
            raise ValueError("stage preflight does not bind the manifest pack ID")
        if self.verification.pack_id != self.manifest.pack_id:
            raise ValueError("stage verification does not bind the manifest pack ID")
        if self.verification.transport_sha256 != self.preflight.transport_sha256:
            raise ValueError("stage verification does not bind the preflight transport")
        if self.verification.archive_byte_count != self.preflight.archive_byte_count:
            raise ValueError("stage verification does not bind the archive byte count")
        if self.verification.manifest_sha256 != self.preflight.manifest_sha256:
            raise ValueError("stage verification does not bind the preflight manifest")
        if self.verification.inventory_sha256 != self.preflight.inventory_sha256:
            raise ValueError("stage verification does not bind the preflight inventory")
        if self.verification.validation_policy_id != self.preflight.validation_policy_id:
            raise ValueError("stage verification does not bind the preflight policy")

    @property
    def stage_path(self) -> Path:
        return Path(self.staging_root) / self.stage_name

    @property
    def staging_path(self) -> Path:
        return self.stage_path

    @property
    def path(self) -> Path:
        return self.stage_path

    @property
    def stage_directory(self) -> Path:
        return self.stage_path

    @property
    def directory(self) -> Path:
        """Compatibility name for the exact pinned stage directory."""

        return self.stage_path

    @property
    def staging_root_path(self) -> Path:
        return Path(self.staging_root)

    @property
    def pack_id(self) -> str:
        return self.verification.pack_id

    @property
    def transport_sha256(self) -> str:
        return self.verification.transport_sha256

    @property
    def stage_device(self) -> int:
        return self.verification.stage_device

    @property
    def stage_inode(self) -> int:
        return self.verification.stage_inode

    @property
    def verification_sha256(self) -> str:
        return self.verification.sha256

    @property
    def manifest_sha256(self) -> str:
        return self.verification.manifest_sha256

    @property
    def inventory_sha256(self) -> str:
        return self.verification.inventory_sha256

    @property
    def staged_tree_sha256(self) -> str:
        return self.verification.staged_tree_sha256

    @property
    def validation_policy_id(self) -> str:
        return self.verification.validation_policy_id

    @property
    def file_count(self) -> int:
        return self.verification.payload_file_count

    @property
    def total_byte_count(self) -> int:
        return self.verification.payload_byte_count

    @property
    def directory_device(self) -> int:
        return self.verification.stage_device

    @property
    def directory_inode(self) -> int:
        return self.verification.stage_inode

    def as_dict(self) -> dict[str, object]:
        """Describe the live capability without offering a forgeable reloader."""

        return {
            "manifest": self.manifest.as_dict(),
            "preflight": self.preflight.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "stage_name": self.stage_name,
            "staging_root": self.staging_root,
            "verification": self.verification.as_dict(),
        }


def stage_preflighted_pack(
    archive_bytes: bytes,
    preflight: PackArchivePreflightV1,
    staging_root: str | os.PathLike[str],
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> ActivationEligiblePackStageV1:
    """Rebind and stage one exact preflighted transport beneath a trusted root."""

    _require_limits(limits)
    if type(archive_bytes) is not bytes:
        raise TypeError("pack archive transport must be exact immutable bytes")
    if type(preflight) is not PackArchivePreflightV1:
        raise TypeError("pack staging requires PackArchivePreflightV1")
    _require_preflight_rebinding(archive_bytes, preflight, limits)
    confirmed_preflight = preflight_pack_archive_bytes(
        archive_bytes,
        limits=limits,
        expected_pack_id=preflight.pack_id,
        expected_transport_sha256=preflight.transport_sha256,
    )
    if confirmed_preflight != preflight:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "supplied preflight differs from a fresh full-archive validation",
        )
    preflight = confirmed_preflight
    archive_context = _open_verified_archive_context(
        archive_bytes,
        preflight,
        limits=limits,
    )

    root_path = _require_existing_trusted_root(staging_root)
    root_descriptor: int | None = None
    stage_descriptor: int | None = None
    stage_name: str | None = None
    created_files: dict[str, tuple[int, int]] = {}
    created_directories: dict[str, tuple[int, int]] = {}
    try:
        root_descriptor = _open_trusted_root(root_path)
        root_metadata = os.fstat(root_descriptor)
        stage_name, stage_descriptor, stage_metadata = _create_private_stage(
            root_descriptor, preflight.pack_id
        )

        directory_paths = _expected_directory_paths(preflight.manifest)
        for directory_path in directory_paths:
            _create_relative_directory(
                stage_descriptor,
                directory_path,
                created_directories,
            )

        manifest_bytes = canonical_manifest_bytes(preflight.manifest)
        if len(manifest_bytes) > limits.maximum_manifest_bytes:
            refuse(
                PackRefusalCodeV1.STAGING_WRITE_FAILED,
                PackValidationPhaseV1.STAGE_WRITE,
                "canonical manifest exceeds the staging manifest ceiling",
                member_path=K2PACK_MANIFEST_PATH,
                observed=len(manifest_bytes),
                limit=limits.maximum_manifest_bytes,
            )
        _write_exclusive_relative_file(
            stage_descriptor,
            K2PACK_MANIFEST_PATH,
            manifest_bytes,
            created_files,
        )

        members = _payload_member_map(preflight)
        payload_total = 0
        for declared_file in preflight.manifest.inventory:
            member = members[declared_file.path]
            raw = _read_verified_context_member_bytes(
                archive_context,
                member,
                declared_file,
                limits=limits,
            )
            payload_total += len(raw)
            if payload_total + len(manifest_bytes) > limits.maximum_total_expanded_bytes:
                refuse(
                    PackRefusalCodeV1.TOTAL_EXPANDED_SIZE_LIMIT,
                    PackValidationPhaseV1.CONTENT_STREAM,
                    "actual expanded archive bytes exceed the total ceiling",
                    member_path=declared_file.path,
                    observed=payload_total + len(manifest_bytes),
                    limit=limits.maximum_total_expanded_bytes,
                )
            _write_exclusive_relative_file(
                stage_descriptor,
                declared_file.path,
                raw,
                created_files,
            )

        _fsync_directory_tree(stage_descriptor, directory_paths)
        os.fsync(stage_descriptor)
        os.fsync(root_descriptor)

        verification = _verify_open_stage(
            root_descriptor=root_descriptor,
            stage_descriptor=stage_descriptor,
            stage_name=stage_name,
            root_metadata=root_metadata,
            stage_metadata=stage_metadata,
            preflight=preflight,
            limits=limits,
        )
        _require_root_path_binding(root_path, root_metadata)
        result = ActivationEligiblePackStageV1(
            staging_root=str(root_path),
            stage_name=stage_name,
            manifest=preflight.manifest,
            preflight=preflight,
            verification=verification,
        )
        return result
    except Exception:
        if (
            root_descriptor is not None
            and stage_descriptor is not None
            and stage_name is not None
        ):
            cleaned = _cleanup_partial_stage(
                root_descriptor,
                stage_name,
                stage_descriptor,
                created_files,
                created_directories,
                limits,
            )
            if not cleaned:
                refuse(
                    PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
                    PackValidationPhaseV1.STAGE_WRITE,
                    "failed stage could not be removed without risking rebound data",
                )
        raise
    finally:
        _close_suppress(stage_descriptor)
        _close_suppress(root_descriptor)


def stage_pack_archive_bytes(
    archive_bytes: bytes,
    staging_root: str | os.PathLike[str],
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    expected_pack_id: str | None = None,
    expected_transport_sha256: str | None = None,
) -> ActivationEligiblePackStageV1:
    """Preflight and stage exact immutable archive bytes without activating them."""

    _require_limits(limits)
    preflight = preflight_pack_archive_bytes(
        archive_bytes,
        limits=limits,
        expected_pack_id=expected_pack_id,
        expected_transport_sha256=expected_transport_sha256,
    )
    return stage_preflighted_pack(
        archive_bytes,
        preflight,
        staging_root,
        limits=limits,
    )


def revalidate_pack_stage(
    stage: ActivationEligiblePackStageV1,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> PackStageVerificationV1:
    """Reopen and exactly revalidate a live stage through no-follow handles."""

    _require_stage(stage)
    _require_limits(limits)
    _require_stage_policy(stage, limits)
    root_descriptor: int | None = None
    stage_descriptor: int | None = None
    try:
        root_path = _require_existing_trusted_root(stage.staging_root)
        root_descriptor = _open_trusted_root(root_path)
        root_metadata = os.fstat(root_descriptor)
        _require_bound_identity(
            root_metadata,
            stage.verification.staging_root_device,
            stage.verification.staging_root_inode,
            "staging root",
        )
        stage_descriptor = _open_relative_directory(
            root_descriptor,
            stage.stage_name,
            "pack stage",
        )
        stage_metadata = os.fstat(stage_descriptor)
        _require_bound_identity(
            stage_metadata,
            stage.verification.stage_device,
            stage.verification.stage_inode,
            "pack stage",
        )
        verification = _verify_open_stage(
            root_descriptor=root_descriptor,
            stage_descriptor=stage_descriptor,
            stage_name=stage.stage_name,
            root_metadata=root_metadata,
            stage_metadata=stage_metadata,
            preflight=stage.preflight,
            limits=limits,
        )
        _require_root_path_binding(root_path, root_metadata)
        if verification != stage.verification:
            refuse(
                PackRefusalCodeV1.STAGING_REVALIDATION_FAILED,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "revalidated stage differs from its activation-eligibility proof",
            )
        return verification
    except PackValidationRefused:
        raise
    except (OSError, ValueError, TypeError) as error:
        refuse(
            PackRefusalCodeV1.STAGING_REVALIDATION_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "pack stage could not be reopened and revalidated safely",
        )
    finally:
        _close_suppress(stage_descriptor)
        _close_suppress(root_descriptor)


def discard_pack_stage(
    stage: ActivationEligiblePackStageV1,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> None:
    """Remove only the exact still-bound and fully revalidated private stage."""

    _require_stage(stage)
    _require_limits(limits)
    _require_stage_policy(stage, limits)
    root_descriptor: int | None = None
    stage_descriptor: int | None = None
    try:
        root_path = _require_existing_trusted_root(stage.staging_root)
        root_descriptor = _open_trusted_root(root_path)
        root_metadata = os.fstat(root_descriptor)
        _require_bound_identity(
            root_metadata,
            stage.verification.staging_root_device,
            stage.verification.staging_root_inode,
            "staging root",
        )
        stage_descriptor = _open_relative_directory(
            root_descriptor,
            stage.stage_name,
            "pack stage",
        )
        stage_metadata = os.fstat(stage_descriptor)
        _require_bound_identity(
            stage_metadata,
            stage.verification.stage_device,
            stage.verification.stage_inode,
            "pack stage",
        )
        current = _verify_open_stage(
            root_descriptor=root_descriptor,
            stage_descriptor=stage_descriptor,
            stage_name=stage.stage_name,
            root_metadata=root_metadata,
            stage_metadata=stage_metadata,
            preflight=stage.preflight,
            limits=limits,
        )
        if current != stage.verification:
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "pack stage changed before discard",
            )
        _discard_verified_open_stage(
            root_descriptor,
            stage.stage_name,
            stage_descriptor,
            stage,
        )
    except PackValidationRefused:
        raise
    except (OSError, ValueError, TypeError):
        refuse(
            PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "verified pack stage could not be discarded safely",
        )
    finally:
        _close_suppress(stage_descriptor)
        _close_suppress(root_descriptor)


def _require_positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _require_limits(limits: PackValidationLimitsV1) -> None:
    if type(limits) is not PackValidationLimitsV1:
        raise TypeError("pack staging limits must be PackValidationLimitsV1")


def _require_stage(stage: ActivationEligiblePackStageV1) -> None:
    if type(stage) is not ActivationEligiblePackStageV1:
        raise TypeError("pack-stage operation requires ActivationEligiblePackStageV1")


def _require_stage_policy(
    stage: ActivationEligiblePackStageV1,
    limits: PackValidationLimitsV1,
) -> None:
    policy_id = validation_policy_id(limits)
    if not hmac.compare_digest(policy_id, stage.preflight.validation_policy_id):
        refuse(
            PackRefusalCodeV1.STAGING_REVALIDATION_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "revalidation limits differ from the preflight validation policy",
        )


def _require_preflight_rebinding(
    archive_bytes: bytes,
    preflight: PackArchivePreflightV1,
    limits: PackValidationLimitsV1,
) -> None:
    if not archive_bytes or len(archive_bytes) > limits.maximum_archive_bytes:
        refuse(
            PackRefusalCodeV1.EXPECTED_TRANSPORT_DIGEST_MISMATCH,
            PackValidationPhaseV1.TRANSPORT,
            "archive bytes no longer satisfy the preflight transport binding",
            observed=len(archive_bytes),
            limit=limits.maximum_archive_bytes,
        )
    actual_transport = transport_sha256(archive_bytes)
    if (
        len(archive_bytes) != preflight.archive_byte_count
        or not hmac.compare_digest(actual_transport, preflight.transport_sha256)
    ):
        refuse(
            PackRefusalCodeV1.EXPECTED_TRANSPORT_DIGEST_MISMATCH,
            PackValidationPhaseV1.TRANSPORT,
            "archive bytes differ from the immutable preflight transport",
        )
    policy_id = validation_policy_id(limits)
    if not hmac.compare_digest(policy_id, preflight.validation_policy_id):
        refuse(
            PackRefusalCodeV1.EXPECTED_TRANSPORT_DIGEST_MISMATCH,
            PackValidationPhaseV1.TRANSPORT,
            "preflight was produced under different validation limits",
        )
    if type(preflight.manifest) is not PackManifestV1:
        refuse(
            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
            PackValidationPhaseV1.MANIFEST,
            "preflight manifest type is invalid",
        )
    manifest_bytes = canonical_manifest_bytes(preflight.manifest)
    declared_total = len(manifest_bytes) + sum(
        item.byte_count for item in preflight.manifest.inventory
    )
    if (
        preflight.pack_id != preflight.manifest.pack_id
        or not hmac.compare_digest(
            hashlib.sha256(manifest_bytes).hexdigest(), preflight.manifest_sha256
        )
        or not hmac.compare_digest(
            inventory_sha256(preflight.manifest), preflight.inventory_sha256
        )
        or preflight.total_expanded_byte_count != declared_total
    ):
        refuse(
            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
            PackValidationPhaseV1.MANIFEST,
            "preflight manifest identity is internally inconsistent",
        )
    _payload_member_map(preflight)


def _payload_member_map(
    preflight: PackArchivePreflightV1,
) -> dict[str, PackArchiveMemberV1]:
    members = preflight.payload_members
    if type(members) is not tuple or any(
        type(member) is not PackArchiveMemberV1 for member in members
    ):
        refuse(
            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "preflight payload member inventory is invalid",
        )
    result = {member.path: member for member in members}
    expected = tuple(item.path for item in preflight.manifest.inventory)
    if len(result) != len(members) or tuple(sorted(result, key=_utf8_key)) != expected:
        refuse(
            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "preflight payload members differ from the manifest inventory",
        )
    for declared in preflight.manifest.inventory:
        member = result[declared.path]
        if member.expanded_byte_count != declared.byte_count:
            refuse(
                PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
                PackValidationPhaseV1.CENTRAL_DIRECTORY,
                "preflight payload member size differs from its declaration",
                member_path=declared.path,
                observed=member.expanded_byte_count,
                limit=declared.byte_count,
            )
    return result


def _require_canonical_absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{label} must be a string or path-like value")
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if path == Path(path.anchor):
        raise ValueError(f"{label} cannot be the filesystem anchor")
    return path


def _require_existing_trusted_root(value: object) -> Path:
    try:
        path = _require_canonical_absolute_path(value, "pack staging root")
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise ValueError("pack staging root must be supplied already resolved")
        descriptor = _open_absolute_directory(path, "pack staging root")
        try:
            _require_trusted_root_metadata(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        return path
    except PackValidationRefused:
        raise
    except (OSError, ValueError, TypeError):
        refuse(
            PackRefusalCodeV1.STAGING_ROOT_UNSAFE,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack staging root must be an existing trusted absolute directory",
        )


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        refuse(
            PackRefusalCodeV1.STAGING_ROOT_UNSAFE,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack staging requires no-follow directory descriptors",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_absolute_directory(path: Path, label: str) -> int:
    current = -1
    try:
        current = os.open(path.anchor, _directory_flags())
        for component in path.parts[1:]:
            following = os.open(component, _directory_flags(), dir_fd=current)
            os.close(current)
            current = following
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a directory")
        result = current
        current = -1
        return result
    finally:
        _close_suppress(current if current >= 0 else None)


def _open_trusted_root(path: Path) -> int:
    descriptor: int | None = None
    try:
        descriptor = _open_absolute_directory(path, "pack staging root")
        _require_trusted_root_metadata(os.fstat(descriptor))
        result = descriptor
        descriptor = None
        return result
    except PackValidationRefused:
        raise
    except (OSError, ValueError, PermissionError):
        refuse(
            PackRefusalCodeV1.STAGING_ROOT_UNSAFE,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack staging root changed or cannot be pinned safely",
        )
    finally:
        _close_suppress(descriptor)


def _require_trusted_root_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("pack staging root is not a real directory")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError("pack staging root must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PermissionError("pack staging root cannot be group/world writable")


def _require_stage_name(name: object) -> str:
    suffix = name[len(_STAGE_PREFIX) :] if type(name) is str else ""
    pieces = suffix.split("-")
    if (
        type(name) is not str
        or not name.startswith(_STAGE_PREFIX)
        or len(name.encode("ascii", "ignore")) != len(name)
        or len(name) > 160
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or len(pieces) != 2
        or len(pieces[0]) != 16
        or len(pieces[1]) != 32
        or any(character not in "0123456789abcdef" for piece in pieces for character in piece)
    ):
        raise ValueError("pack stage leaf name is invalid")
    return name


def _create_private_stage(
    root_descriptor: int,
    pack_id: str,
) -> tuple[str, int, os.stat_result]:
    for _ in range(32):
        name = f"{_STAGE_PREFIX}{pack_id[:16]}-{secrets.token_hex(16)}"
        created_metadata: os.stat_result | None = None
        try:
            os.mkdir(name, 0o700, dir_fd=root_descriptor)
        except FileExistsError:
            continue
        except OSError:
            refuse(
                PackRefusalCodeV1.STAGING_WRITE_FAILED,
                PackValidationPhaseV1.STAGE_WRITE,
                "fresh private pack stage could not be created",
            )
        descriptor: int | None = None
        try:
            named = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            created_metadata = named
            descriptor = _open_relative_directory(root_descriptor, name, "pack stage")
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                refuse(
                    PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                    PackValidationPhaseV1.STAGE_WRITE,
                    "fresh pack stage was rebound during creation",
                )
            os.fchmod(descriptor, 0o700)
            secured = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(secured.st_mode)
                or stat.S_IMODE(secured.st_mode) != 0o700
                or (hasattr(os, "geteuid") and secured.st_uid != os.geteuid())
                or (secured.st_dev, secured.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                refuse(
                    PackRefusalCodeV1.STAGING_WRITE_FAILED,
                    PackValidationPhaseV1.STAGE_WRITE,
                    "fresh pack stage is not one private owner-only directory",
                )
            os.fsync(descriptor)
            os.fsync(root_descriptor)
            result = descriptor
            descriptor = None
            return name, result, secured
        except Exception:
            if not _remove_fresh_empty_stage(
                root_descriptor,
                name,
                descriptor,
                created_metadata,
            ):
                refuse(
                    PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
                    PackValidationPhaseV1.STAGE_WRITE,
                    "failed fresh stage could not be removed without a binding risk",
                )
            raise
        finally:
            _close_suppress(descriptor)
    refuse(
        PackRefusalCodeV1.STAGING_TARGET_EXISTS,
        PackValidationPhaseV1.STAGE_WRITE,
        "could not allocate a fresh pack stage name",
    )


def _remove_fresh_empty_stage(
    root_descriptor: int,
    name: str,
    descriptor: int | None,
    expected: os.stat_result | None,
) -> bool:
    opened_here = False
    try:
        if expected is None:
            return False
        if descriptor is None:
            descriptor = _open_relative_directory(root_descriptor, name, "fresh pack stage")
            opened_here = True
        pinned = os.fstat(descriptor)
        named = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        identity = (expected.st_dev, expected.st_ino)
        if (
            (pinned.st_dev, pinned.st_ino) != identity
            or (named.st_dev, named.st_ino) != identity
            or not _directory_is_empty(descriptor)
        ):
            return False
        os.rmdir(name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
        return True
    except Exception:
        return False
    finally:
        if opened_here:
            _close_suppress(descriptor)


def _open_relative_directory(parent: int, name: str, label: str) -> int:
    if type(name) is not str or not name or "/" in name or "\\" in name:
        raise ValueError(f"{label} component is invalid")
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a real directory")
        result = descriptor
        descriptor = None
        return result
    finally:
        _close_suppress(descriptor)


def _expected_directory_paths(manifest: PackManifestV1) -> tuple[str, ...]:
    paths: set[str] = set()
    for item in manifest.inventory:
        parts = PurePosixPath(item.path).parts[:-1]
        for depth in range(1, len(parts) + 1):
            paths.add("/".join(parts[:depth]))
    return tuple(sorted(paths, key=lambda item: (item.count("/"), item.encode("utf-8"))))


def _create_relative_directory(
    stage_descriptor: int,
    relative_path: str,
    created_directories: dict[str, tuple[int, int]],
) -> None:
    current = os.dup(stage_descriptor)
    prefix: list[str] = []
    try:
        for component in PurePosixPath(relative_path).parts:
            prefix.append(component)
            canonical = "/".join(prefix)
            created_now = canonical not in created_directories
            created_identity: tuple[int, int] | None = None
            if created_now:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    refuse(
                        PackRefusalCodeV1.STAGING_TARGET_EXISTS,
                        PackValidationPhaseV1.STAGE_WRITE,
                        "pack-stage directory unexpectedly already exists",
                        member_path=canonical,
                    )
                created_metadata = os.stat(
                    component,
                    dir_fd=current,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(created_metadata.st_mode):
                    refuse(
                        PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                        PackValidationPhaseV1.STAGE_WRITE,
                        "new pack-stage directory is not a real directory",
                        member_path=canonical,
                    )
                created_identity = (
                    created_metadata.st_dev,
                    created_metadata.st_ino,
                )
                os.fsync(current)
            following = _open_relative_directory(current, component, "pack-stage directory")
            metadata = os.fstat(following)
            identity = (metadata.st_dev, metadata.st_ino)
            if (
                stat.S_IMODE(metadata.st_mode) != 0o700
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
                or (
                    created_identity is not None
                    and identity != created_identity
                )
                or (
                    not created_now
                    and created_directories[canonical] != identity
                )
            ):
                os.close(following)
                refuse(
                    PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                    PackValidationPhaseV1.STAGE_WRITE,
                    "pack-stage directory is not private and owner-bound",
                    member_path=canonical,
                )
            if created_now:
                created_directories[canonical] = identity
            os.close(current)
            current = following
        os.fsync(current)
    except PackValidationRefused:
        raise
    except OSError:
        refuse(
            PackRefusalCodeV1.STAGING_WRITE_FAILED,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack-stage directory could not be created safely",
            member_path=relative_path,
        )
    finally:
        _close_suppress(current)


def _open_parent_for_path(stage_descriptor: int, path: str) -> tuple[int, str]:
    parts = PurePosixPath(path).parts
    current = os.dup(stage_descriptor)
    try:
        for component in parts[:-1]:
            following = _open_relative_directory(current, component, "pack-stage parent")
            os.close(current)
            current = following
        result = current
        current = -1
        return result, parts[-1]
    finally:
        _close_suppress(current if current >= 0 else None)


def _write_exclusive_relative_file(
    stage_descriptor: int,
    path: str,
    raw: bytes,
    created_files: dict[str, tuple[int, int]],
) -> None:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, 0o600, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_WRITE,
                "new pack-stage entry is not one regular file",
                member_path=path,
            )
        pinned = (metadata.st_dev, metadata.st_ino)
        created_files[path] = pinned
        os.fchmod(descriptor, 0o600)
        secured = os.fstat(descriptor)
        if (
            not stat.S_ISREG(secured.st_mode)
            or secured.st_nlink != 1
            or stat.S_IMODE(secured.st_mode) != 0o600
            or (secured.st_dev, secured.st_ino) != pinned
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_WRITE,
                "new pack-stage entry changed while being secured",
                member_path=path,
            )
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset : offset + _IO_CHUNK_BYTES])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        final = os.fstat(descriptor)
        if (
            (named.st_dev, named.st_ino) != pinned
            or (final.st_dev, final.st_ino) != pinned
            or final.st_size != len(raw)
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_WRITE,
                "pack-stage entry changed during its exact-byte write",
                member_path=path,
            )
        os.fsync(parent)
    except PackValidationRefused:
        raise
    except FileExistsError:
        refuse(
            PackRefusalCodeV1.STAGING_TARGET_EXISTS,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack-stage file unexpectedly already exists",
            member_path=path,
        )
    except OSError:
        refuse(
            PackRefusalCodeV1.STAGING_WRITE_FAILED,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack-stage file could not be written and synced safely",
            member_path=path,
        )
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _verify_open_stage(
    *,
    root_descriptor: int,
    stage_descriptor: int,
    stage_name: str,
    root_metadata: os.stat_result,
    stage_metadata: os.stat_result,
    preflight: PackArchivePreflightV1,
    limits: PackValidationLimitsV1,
) -> PackStageVerificationV1:
    try:
        _require_private_stage_metadata(stage_metadata)
        _require_named_stage_binding(
            root_descriptor,
            stage_name,
            stage_metadata.st_dev,
            stage_metadata.st_ino,
        )
        expected_files = (K2PACK_MANIFEST_PATH,) + tuple(
            item.path for item in preflight.manifest.inventory
        )
        expected_directories = _expected_directory_paths(preflight.manifest)
        maximum_tree_entries = len(expected_files) + len(expected_directories)
        actual_files, actual_directories, initial_bindings = _scan_closed_tree(
            stage_descriptor,
            limits,
            maximum_entries=maximum_tree_entries,
        )
        if actual_files != tuple(sorted(expected_files, key=_utf8_key)):
            refuse(
                PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged file inventory is not exact",
            )
        if actual_directories != tuple(sorted(expected_directories, key=_utf8_key)):
            refuse(
                PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged directory inventory is not exact",
            )

        manifest_bytes = _read_bound_regular_file(
            stage_descriptor,
            K2PACK_MANIFEST_PATH,
            maximum_bytes=limits.maximum_manifest_bytes,
            expected_size=None,
        )
        try:
            manifest = load_manifest_bytes(manifest_bytes)
        except (OverflowError, RecursionError, TypeError, ValueError):
            refuse(
                PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged manifest is not exact canonical manifest bytes",
                member_path=K2PACK_MANIFEST_PATH,
            )
        if manifest != preflight.manifest:
            refuse(
                PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged manifest differs from the preflight manifest",
                member_path=K2PACK_MANIFEST_PATH,
            )
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if not hmac.compare_digest(manifest_digest, preflight.manifest_sha256):
            refuse(
                PackRefusalCodeV1.PAYLOAD_DIGEST_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged manifest digest differs from preflight",
                member_path=K2PACK_MANIFEST_PATH,
            )

        payload_rows: list[dict[str, object]] = []
        payload_total = 0
        for item in preflight.manifest.inventory:
            raw = _read_bound_regular_file(
                stage_descriptor,
                item.path,
                maximum_bytes=limits.maximum_file_expanded_bytes,
                expected_size=item.byte_count,
            )
            payload_total += len(raw)
            if payload_total + len(manifest_bytes) > limits.maximum_total_expanded_bytes:
                refuse(
                    PackRefusalCodeV1.TOTAL_EXPANDED_SIZE_LIMIT,
                    PackValidationPhaseV1.STAGE_REVALIDATION,
                    "reopened staged bytes exceed the total expanded ceiling",
                    member_path=item.path,
                    observed=payload_total + len(manifest_bytes),
                    limit=limits.maximum_total_expanded_bytes,
                )
            validate_structural_payload(
                item,
                raw,
                limits=limits,
                phase=PackValidationPhaseV1.STAGE_REVALIDATION,
            )
            payload_rows.append(
                {
                    "byte_count": len(raw),
                    "path": item.path,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )

        tree_projection = {
            "files": [
                {
                    "byte_count": len(manifest_bytes),
                    "path": K2PACK_MANIFEST_PATH,
                    "sha256": manifest_digest,
                },
                *payload_rows,
            ],
            "schema_id": "KIRBY2_PACK_STAGED_TREE_PROJECTION_V1",
            "schema_version": 1,
        }
        _require_named_stage_binding(
            root_descriptor,
            stage_name,
            stage_metadata.st_dev,
            stage_metadata.st_ino,
        )
        final_files, final_directories, final_bindings = _scan_closed_tree(
            stage_descriptor,
            limits,
            maximum_entries=maximum_tree_entries,
        )
        if (
            final_files != actual_files
            or final_directories != actual_directories
            or final_bindings != initial_bindings
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged tree changed during exact-byte revalidation",
            )
        return PackStageVerificationV1(
            pack_id=preflight.pack_id,
            transport_sha256=preflight.transport_sha256,
            archive_byte_count=preflight.archive_byte_count,
            manifest_sha256=manifest_digest,
            manifest_byte_count=len(manifest_bytes),
            inventory_sha256=inventory_sha256(preflight.manifest),
            validation_policy_id=preflight.validation_policy_id,
            staged_tree_sha256=hashlib.sha256(
                canonical_json_bytes(tree_projection)
            ).hexdigest(),
            payload_file_count=len(payload_rows),
            payload_byte_count=payload_total,
            staging_root_device=root_metadata.st_dev,
            staging_root_inode=root_metadata.st_ino,
            stage_device=stage_metadata.st_dev,
            stage_inode=stage_metadata.st_ino,
        )
    except PackValidationRefused:
        raise
    except (OSError, OverflowError, RecursionError, TypeError, ValueError):
        refuse(
            PackRefusalCodeV1.STAGING_REVALIDATION_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "staged tree could not be revalidated safely",
        )


def _scan_closed_tree(
    stage_descriptor: int,
    limits: PackValidationLimitsV1,
    *,
    maximum_entries: int,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, int, int, int, int, int, int, int], ...],
]:
    if type(maximum_entries) is not int or maximum_entries < 0:
        raise TypeError("staged-tree entry ceiling must be a nonnegative integer")
    files: list[str] = []
    directories: list[str] = []
    bindings: list[tuple[str, int, int, int, int, int, int, int]] = []
    entry_count = 0

    def visit(directory: int, prefix: str, parent_depth: int) -> None:
        nonlocal entry_count
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > maximum_entries:
                    refuse(
                        PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged tree exceeds its exact expected entry ceiling",
                        observed=entry_count,
                        limit=maximum_entries,
                    )
                name = entry.name
                if type(name) is not str or name in {"", ".", ".."}:
                    refuse(
                        PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged tree contains an invalid directory entry",
                    )
                depth = parent_depth + 1
                if depth > limits.maximum_path_depth:
                    refuse(
                        PackRefusalCodeV1.PATH_DEPTH_LIMIT,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged path exceeds the bounded depth ceiling",
                        member_path=name,
                        observed=depth,
                        limit=limits.maximum_path_depth,
                    )
                path = f"{prefix}/{name}" if prefix else name
                try:
                    path_byte_count = len(path.encode("utf-8"))
                except UnicodeEncodeError:
                    refuse(
                        PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged path is not valid UTF-8 text",
                    )
                if path_byte_count > limits.maximum_path_bytes:
                    refuse(
                        PackRefusalCodeV1.PATH_LENGTH_LIMIT,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged path exceeds the bounded byte-length ceiling",
                        member_path=name,
                        observed=path_byte_count,
                        limit=limits.maximum_path_bytes,
                    )
                before = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = _open_relative_directory(
                        directory,
                        name,
                        "staged directory",
                    )
                    try:
                        pinned = os.fstat(child)
                        after = os.stat(
                            name,
                            dir_fd=directory,
                            follow_symlinks=False,
                        )
                        if (
                            (before.st_dev, before.st_ino)
                            != (pinned.st_dev, pinned.st_ino)
                            or (after.st_dev, after.st_ino)
                            != (pinned.st_dev, pinned.st_ino)
                        ):
                            refuse(
                                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                                PackValidationPhaseV1.STAGE_REVALIDATION,
                                "staged directory was rebound during traversal",
                                member_path=path,
                            )
                        _require_private_stage_metadata(pinned)
                        directories.append(path)
                        bindings.append(
                            (
                                path,
                                1,
                                pinned.st_dev,
                                pinned.st_ino,
                                pinned.st_size,
                                pinned.st_mtime_ns,
                                pinned.st_ctime_ns,
                                pinned.st_mode,
                            )
                        )
                        visit(child, path, depth)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600:
                        refuse(
                            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                            PackValidationPhaseV1.STAGE_REVALIDATION,
                            "staged file is not one owner-only regular file",
                            member_path=path,
                        )
                    files.append(path)
                    bindings.append(
                        (
                            path,
                            0,
                            before.st_dev,
                            before.st_ino,
                            before.st_size,
                            before.st_mtime_ns,
                            before.st_ctime_ns,
                            before.st_mode,
                        )
                    )
                else:
                    refuse(
                        PackRefusalCodeV1.STAGING_TREE_MISMATCH,
                        PackValidationPhaseV1.STAGE_REVALIDATION,
                        "staged tree contains a link or special entry",
                        member_path=path,
                    )

    visit(stage_descriptor, "", 0)
    return (
        tuple(sorted(files, key=_utf8_key)),
        tuple(sorted(directories, key=_utf8_key)),
        tuple(sorted(bindings, key=lambda item: _utf8_key(item[0]))),
    )


def _read_bound_regular_file(
    stage_descriptor: int,
    path: str,
    *,
    maximum_bytes: int,
    expected_size: int | None,
) -> bytes:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, dir_fd=parent)
        pinned = os.fstat(descriptor)
        identity = (pinned.st_dev, pinned.st_ino)
        if (
            not stat.S_ISREG(pinned.st_mode)
            or pinned.st_nlink != 1
            or stat.S_IMODE(pinned.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != identity
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged file is unsafe or was rebound before reading",
                member_path=path,
            )
        if pinned.st_size <= 0 or pinned.st_size > maximum_bytes:
            refuse(
                PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged file size exceeds its bounded read ceiling",
                member_path=path,
                observed=pinned.st_size,
                limit=maximum_bytes,
            )
        if expected_size is not None and pinned.st_size != expected_size:
            refuse(
                PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged payload byte count differs from its declaration",
                member_path=path,
                observed=pinned.st_size,
                limit=expected_size,
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_IO_CHUNK_BYTES, maximum_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                refuse(
                    PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
                    PackValidationPhaseV1.STAGE_REVALIDATION,
                    "staged file grew beyond its bounded read ceiling",
                    member_path=path,
                    observed=total,
                    limit=maximum_bytes,
                )
            chunks.append(chunk)
        after_descriptor = os.fstat(descriptor)
        after_name = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            (after_descriptor.st_dev, after_descriptor.st_ino) != identity
            or (after_name.st_dev, after_name.st_ino) != identity
            or after_descriptor.st_size != total
            or after_descriptor.st_mtime_ns != pinned.st_mtime_ns
            or after_descriptor.st_ctime_ns != pinned.st_ctime_ns
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged file changed during exact-byte reading",
                member_path=path,
            )
        raw = b"".join(chunks)
        if expected_size is not None and len(raw) != expected_size:
            refuse(
                PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "staged payload read length differs from its declaration",
                member_path=path,
                observed=len(raw),
                limit=expected_size,
            )
        return raw
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _fsync_directory_tree(
    stage_descriptor: int,
    directory_paths: tuple[str, ...],
) -> None:
    try:
        for path in sorted(
            directory_paths,
            key=lambda item: (-item.count("/"), item.encode("utf-8")),
        ):
            parent: int | None = None
            child: int | None = None
            try:
                parent, leaf = _open_parent_for_path(stage_descriptor, path)
                child = _open_relative_directory(
                    parent,
                    leaf,
                    "pack-stage directory",
                )
                os.fsync(child)
            finally:
                _close_suppress(child)
                _close_suppress(parent)
        os.fsync(stage_descriptor)
    except PackValidationRefused:
        raise
    except (OSError, ValueError):
        refuse(
            PackRefusalCodeV1.STAGING_WRITE_FAILED,
            PackValidationPhaseV1.STAGE_WRITE,
            "pack-stage directory tree could not be synced",
        )


def _require_private_stage_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        refuse(
            PackRefusalCodeV1.STAGING_TREE_MISMATCH,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "pack stage is not one private owner-only directory",
        )


def _require_root_path_binding(path: Path, expected: os.stat_result) -> None:
    descriptor: int | None = None
    try:
        descriptor = _open_absolute_directory(path, "pack staging root")
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "pack staging root path was rebound during staging",
            )
    except PackValidationRefused:
        raise
    except (OSError, ValueError):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "pack staging root path cannot be rebound to its pinned directory",
        )
    finally:
        _close_suppress(descriptor)


def _require_named_stage_binding(
    root_descriptor: int,
    stage_name: str,
    expected_device: int,
    expected_inode: int,
) -> None:
    try:
        named = os.stat(
            stage_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "named pack stage is missing or unsafe",
        )
    if (
        not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (expected_device, expected_inode)
    ):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "named pack stage no longer has its pinned filesystem identity",
        )


def _require_bound_identity(
    metadata: os.stat_result,
    expected_device: int,
    expected_inode: int,
    label: str,
) -> None:
    if (metadata.st_dev, metadata.st_ino) != (expected_device, expected_inode):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            f"{label} no longer has its pinned filesystem identity",
        )


def _discard_verified_open_stage(
    root_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
    stage: ActivationEligiblePackStageV1,
) -> None:
    expected_bytes: dict[str, tuple[int, str]] = {
        K2PACK_MANIFEST_PATH: (
            stage.verification.manifest_byte_count,
            stage.verification.manifest_sha256,
        ),
        **{
            item.path: (item.byte_count, item.sha256)
            for item in stage.manifest.inventory
        },
    }
    for path in sorted(expected_bytes, key=lambda item: (-item.count("/"), _utf8_key(item))):
        size, digest = expected_bytes[path]
        _unlink_exact_regular(stage_descriptor, path, size, digest)
    for path in sorted(
        _expected_directory_paths(stage.manifest),
        key=lambda item: (-item.count("/"), item.encode("utf-8")),
    ):
        _rmdir_exact_empty(stage_descriptor, path)
    try:
        if not _directory_is_empty(stage_descriptor):
            raise ValueError("pack stage is not empty after exact discard")
        pinned = os.fstat(stage_descriptor)
        named = os.stat(stage_name, dir_fd=root_descriptor, follow_symlinks=False)
        if (
            (pinned.st_dev, pinned.st_ino)
            != (stage.verification.stage_device, stage.verification.stage_inode)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.STAGE_REVALIDATION,
                "pack stage was rebound before final discard",
            )
        os.rmdir(stage_name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
    except PackValidationRefused:
        raise
    except OSError:
        refuse(
            PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "pack stage could not be removed after exact content discard",
        )


def _unlink_exact_regular(
    stage_descriptor: int,
    path: str,
    expected_size: int,
    expected_sha256: str,
) -> None:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, dir_fd=parent)
        pinned = os.fstat(descriptor)
        identity = (pinned.st_dev, pinned.st_ino)
        if (
            not stat.S_ISREG(pinned.st_mode)
            or pinned.st_nlink != 1
            or pinned.st_size != expected_size
            or (before.st_dev, before.st_ino) != identity
        ):
            raise ValueError("pack-stage file binding differs during discard")
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(_IO_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("pack-stage file ended during discard")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("pack-stage file grew during discard")
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            (named.st_dev, named.st_ino) != identity
            or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
        ):
            raise ValueError("pack-stage file bytes or binding differ during discard")
        os.unlink(leaf, dir_fd=parent)
        os.fsync(parent)
    except (OSError, ValueError):
        refuse(
            PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "exact staged file could not be discarded safely",
            member_path=path,
        )
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _rmdir_exact_empty(stage_descriptor: int, path: str) -> None:
    parent: int | None = None
    child: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        child = _open_relative_directory(parent, leaf, "pack-stage directory")
        pinned = os.fstat(child)
        if (
            (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino)
            or not _directory_is_empty(child)
        ):
            raise ValueError("pack-stage directory differs or is not empty")
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("pack-stage directory was rebound during discard")
        os.close(child)
        child = None
        os.rmdir(leaf, dir_fd=parent)
        os.fsync(parent)
    except (OSError, ValueError):
        refuse(
            PackRefusalCodeV1.STAGING_CLEANUP_FAILED,
            PackValidationPhaseV1.STAGE_REVALIDATION,
            "exact staged directory could not be discarded safely",
            member_path=path,
        )
    finally:
        _close_suppress(child)
        _close_suppress(parent)


def _cleanup_partial_stage(
    root_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
    created_files: dict[str, tuple[int, int]],
    created_directories: dict[str, tuple[int, int]],
    limits: PackValidationLimitsV1,
) -> bool:
    """Best-effort cleanup, only while every created name remains exact and bound."""

    try:
        pinned_stage = os.fstat(stage_descriptor)
        named_stage = os.stat(
            stage_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (named_stage.st_dev, named_stage.st_ino) != (
            pinned_stage.st_dev,
            pinned_stage.st_ino,
        ):
            return False
        actual_files, actual_directories, bindings = _scan_closed_tree(
            stage_descriptor,
            limits,
            maximum_entries=len(created_files) + len(created_directories),
        )
        if set(actual_files) != set(created_files) or set(actual_directories) != set(
            created_directories
        ):
            return False
        current_identities = {
            (path, kind): (device, inode)
            for path, kind, device, inode, *_ in bindings
        }
        if any(
            current_identities.get((path, 0)) != identity
            for path, identity in created_files.items()
        ) or any(
            current_identities.get((path, 1)) != identity
            for path, identity in created_directories.items()
        ):
            return False
        for path in sorted(
            created_files, key=lambda item: (-item.count("/"), item.encode("utf-8"))
        ):
            if not _unlink_owned_partial(
                stage_descriptor,
                path,
                created_files[path],
            ):
                return False
        for path in sorted(
            created_directories,
            key=lambda item: (-item.count("/"), item.encode("utf-8")),
        ):
            if not _rmdir_owned_partial(
                stage_descriptor,
                path,
                created_directories[path],
            ):
                return False
        if not _directory_is_empty(stage_descriptor):
            return False
        named_stage = os.stat(
            stage_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (named_stage.st_dev, named_stage.st_ino) != (
            pinned_stage.st_dev,
            pinned_stage.st_ino,
        ):
            return False
        os.rmdir(stage_name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
        return True
    except Exception:
        return False


def _unlink_owned_partial(
    stage_descriptor: int,
    path: str,
    expected_identity: tuple[int, int],
) -> bool:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, dir_fd=parent)
        pinned = os.fstat(descriptor)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(pinned.st_mode)
            or pinned.st_nlink != 1
            or (pinned.st_dev, pinned.st_ino) != expected_identity
            or (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            return False
        os.unlink(leaf, dir_fd=parent)
        os.fsync(parent)
        return True
    except Exception:
        return False
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _rmdir_owned_partial(
    stage_descriptor: int,
    path: str,
    expected_identity: tuple[int, int],
) -> bool:
    parent: int | None = None
    child: int | None = None
    try:
        parent, leaf = _open_parent_for_path(stage_descriptor, path)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        child = _open_relative_directory(parent, leaf, "partial pack-stage directory")
        pinned = os.fstat(child)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            (pinned.st_dev, pinned.st_ino) != expected_identity
            or (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino)
            or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
            or not _directory_is_empty(child)
        ):
            return False
        os.close(child)
        child = None
        os.rmdir(leaf, dir_fd=parent)
        os.fsync(parent)
        return True
    except Exception:
        return False
    finally:
        _close_suppress(child)
        _close_suppress(parent)


def _directory_is_empty(directory_descriptor: int) -> bool:
    with os.scandir(directory_descriptor) as entries:
        return next(entries, None) is None


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _close_suppress(descriptor: int | None) -> None:
    if descriptor is None or descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


__all__ = [
    "PACK_STAGE_CAPABILITY_SCHEMA_ID",
    "PACK_STAGE_CAPABILITY_SCHEMA_VERSION",
    "PACK_STAGE_VERIFICATION_SCHEMA_ID",
    "PACK_STAGE_VERIFICATION_SCHEMA_VERSION",
    "ActivationEligiblePackStageV1",
    "PackStageVerificationV1",
    "discard_pack_stage",
    "revalidate_pack_stage",
    "stage_pack_archive_bytes",
    "stage_preflighted_pack",
]
