"""Atomic local installation for verified data-only Kirby2 packs.

This module is the sole WO39-C mutation boundary.  It consumes a freshly
revalidated staging capability, resolves compatibility and dependencies only from
the caller-supplied runtime description and the local canonical registry, publishes
one read-only content-addressed tree, and only then atomically replaces registry
truth.  It never performs network access and never opens ``runs`` or ``evidence``.

All filesystem mutation is descriptor-relative beneath ``DataPaths.packs`` or
``DataPaths.staging``.  Names are rebound to pinned device/inode pairs before each
rename.  As with the staging boundary, a malicious process already running as the
same operating-system user is outside the portable POSIX isolation model: advisory
locks and final name/inode checks coordinate Kirby2 writers but cannot make
name-based rename or unlink hostile-same-UID-proof on every supported filesystem.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import ClassVar

from kirby2.research.paths import DataAreaId, DataPaths

from .dependencies import (
    PackDependencyResolutionV1,
    PackRuntimeEnvironmentV1,
    resolve_pack_dependencies,
)
from .formats import (
    K2PACK_MANIFEST_PATH,
    canonical_json_bytes,
    canonical_manifest_bytes,
    require_sha256,
)
from .models import PackManifestV1, PackRegistryKeyV1
from .registry import (
    PACK_OBJECT_DIGEST_ALGORITHM,
    PACK_REGISTRY_FILENAME,
    PACK_REGISTRY_MAX_BYTE_COUNT,
    PackRegistryDependencyEdgeV1,
    PackRegistryEntryV1,
    PackRegistryV1,
    canonical_pack_registry_bytes,
    load_pack_registry_bytes,
    pack_object_relative_path,
    pack_registry_sha256,
)
from .staging import (
    ActivationEligiblePackStageV1,
    revalidate_pack_stage,
)
from .validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackValidationLimitsV1,
    PackValidationRefused,
)


PACK_REGISTRY_LOCK_FILENAME = ".registry.lock"
PACK_RECOVERY_DIRECTORY = "recovery"
PACK_INSTALL_RECEIPT_SCHEMA_ID = "KIRBY2_PACK_INSTALL_RECEIPT_V1"
PACK_DEACTIVATION_RECEIPT_SCHEMA_ID = "KIRBY2_PACK_DEACTIVATION_RECEIPT_V1"
PACK_REMOVAL_RECEIPT_SCHEMA_ID = "KIRBY2_PACK_REMOVAL_RECEIPT_V1"
PACK_MUTATION_RECEIPT_SCHEMA_VERSION = 1
_REGISTRY_TEMP_PREFIX = ".registry-write-"
_REGISTRY_TEMP_SUFFIX = ".tmp"
_IO_CHUNK_BYTES = 1024 * 1024


class PackInstallOperationV1(str, Enum):
    """Closed operation labels used by stable install refusals."""

    INSTALL = "INSTALL"
    DEACTIVATE = "DEACTIVATE"
    REMOVE = "REMOVE"
    READ_REGISTRY = "READ_REGISTRY"


class PackInstallRefusalCodeV1(str, Enum):
    """Closed V1 refusal vocabulary for the mutating pack boundary."""

    STAGE_INVALID = "STAGE_INVALID"
    STAGING_ROOT_MISMATCH = "STAGING_ROOT_MISMATCH"
    STAGING_ENTRY_REBOUND = "STAGING_ENTRY_REBOUND"
    DATA_PATHS_UNSAFE = "DATA_PATHS_UNSAFE"
    PACK_AREA_UNSAFE = "PACK_AREA_UNSAFE"
    REGISTRY_LOCK_UNSAFE = "REGISTRY_LOCK_UNSAFE"
    REGISTRY_MISSING = "REGISTRY_MISSING"
    REGISTRY_UNSAFE = "REGISTRY_UNSAFE"
    REGISTRY_CHANGED = "REGISTRY_CHANGED"
    REGISTRY_KEY_CONFLICT = "REGISTRY_KEY_CONFLICT"
    DEPENDENCY_RESOLUTION_FAILED = "DEPENDENCY_RESOLUTION_FAILED"
    DEPENDENCY_OBJECT_INVALID = "DEPENDENCY_OBJECT_INVALID"
    OBJECT_PATH_UNSAFE = "OBJECT_PATH_UNSAFE"
    OBJECT_CONFLICT = "OBJECT_CONFLICT"
    OBJECT_TREE_INVALID = "OBJECT_TREE_INVALID"
    OBJECT_PUBLISH_FAILED = "OBJECT_PUBLISH_FAILED"
    REGISTRY_WRITE_FAILED = "REGISTRY_WRITE_FAILED"
    REGISTRY_DURABILITY_UNCERTAIN = "REGISTRY_DURABILITY_UNCERTAIN"
    PACK_NOT_INSTALLED = "PACK_NOT_INSTALLED"
    PACK_STILL_ACTIVE = "PACK_STILL_ACTIVE"
    ACTIVE_DEPENDENTS = "ACTIVE_DEPENDENTS"
    RECOVERY_CONFLICT = "RECOVERY_CONFLICT"
    RECOVERY_MOVE_FAILED = "RECOVERY_MOVE_FAILED"


@dataclass(frozen=True, slots=True)
class PackInstallRefusalV1:
    """One immutable, data-only refusal emitted before a transition completes."""

    code: PackInstallRefusalCodeV1
    operation: PackInstallOperationV1
    detail: str

    def __post_init__(self) -> None:
        if type(self.code) is not PackInstallRefusalCodeV1:
            raise TypeError("pack install refusal code is invalid")
        if type(self.operation) is not PackInstallOperationV1:
            raise TypeError("pack install refusal operation is invalid")
        if type(self.detail) is not str or not self.detail or len(self.detail) > 1024:
            raise ValueError("pack install refusal detail is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "detail": self.detail,
            "operation": self.operation.value,
        }


class PackInstallRefused(RuntimeError):
    """Raised when a closed WO39-C install policy refuses an operation."""

    def __init__(self, refusal: PackInstallRefusalV1) -> None:
        if type(refusal) is not PackInstallRefusalV1:
            raise TypeError("pack install refusal exception requires a typed refusal")
        self.refusal = refusal
        super().__init__(
            f"{refusal.code.value}: {refusal.operation.value}: {refusal.detail}"
        )

    @property
    def code(self) -> PackInstallRefusalCodeV1:
        return self.refusal.code

    @property
    def operation(self) -> PackInstallOperationV1:
        return self.refusal.operation


@dataclass(frozen=True, slots=True)
class PackInstallReceiptV1:
    """Immutable receipt for one successful active-registry outcome."""

    key: PackRegistryKeyV1
    pack_id: str
    object_path: str
    stage_verification_sha256: str
    registry_before_sha256: str
    registry_after_sha256: str
    resolved_dependencies: tuple[PackRegistryDependencyEdgeV1, ...]
    installed_new_object: bool
    registry_changed: bool

    schema_id: ClassVar[str] = PACK_INSTALL_RECEIPT_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_MUTATION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.key) is not PackRegistryKeyV1:
            raise TypeError("pack install receipt key is invalid")
        require_sha256(self.pack_id, "installed pack ID")
        require_sha256(
            self.stage_verification_sha256,
            "installed stage verification digest",
        )
        require_sha256(self.registry_before_sha256, "prior pack registry digest")
        require_sha256(self.registry_after_sha256, "updated pack registry digest")
        if self.object_path != pack_object_relative_path(self.pack_id):
            raise ValueError("install receipt object path is not content-addressed")
        if type(self.resolved_dependencies) is not tuple or any(
            type(item) is not PackRegistryDependencyEdgeV1
            for item in self.resolved_dependencies
        ):
            raise TypeError("install receipt dependency edges are invalid")
        if tuple(sorted(self.resolved_dependencies, key=lambda item: item.sort_key)) != (
            self.resolved_dependencies
        ):
            raise ValueError("install receipt dependencies are not canonical")
        if type(self.installed_new_object) is not bool:
            raise TypeError("install receipt object-publication state is invalid")
        if type(self.registry_changed) is not bool:
            raise TypeError("install receipt registry-change state is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "installed_new_object": self.installed_new_object,
            "key": self.key.as_dict(),
            "object_path": self.object_path,
            "pack_id": self.pack_id,
            "registry_after_sha256": self.registry_after_sha256,
            "registry_before_sha256": self.registry_before_sha256,
            "registry_changed": self.registry_changed,
            "resolved_dependencies": [
                edge.as_dict() for edge in self.resolved_dependencies
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "stage_verification_sha256": self.stage_verification_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PackDeactivationReceiptV1:
    """Immutable receipt for one successful inactive-registry outcome."""

    key: PackRegistryKeyV1
    pack_id: str
    object_path: str
    registry_before_sha256: str
    registry_after_sha256: str
    already_inactive: bool

    schema_id: ClassVar[str] = PACK_DEACTIVATION_RECEIPT_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_MUTATION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.key) is not PackRegistryKeyV1:
            raise TypeError("pack deactivation receipt key is invalid")
        require_sha256(self.pack_id, "deactivated pack ID")
        require_sha256(self.registry_before_sha256, "prior pack registry digest")
        require_sha256(self.registry_after_sha256, "updated pack registry digest")
        if self.object_path != pack_object_relative_path(self.pack_id):
            raise ValueError("deactivation receipt object path is not canonical")
        if type(self.already_inactive) is not bool:
            raise TypeError("deactivation receipt idempotence state is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "already_inactive": self.already_inactive,
            "key": self.key.as_dict(),
            "object_path": self.object_path,
            "pack_id": self.pack_id,
            "registry_after_sha256": self.registry_after_sha256,
            "registry_before_sha256": self.registry_before_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PackRemovalReceiptV1:
    """Immutable receipt for registry-first recoverable object removal."""

    key: PackRegistryKeyV1
    pack_id: str
    object_path: str
    recovery_path: str
    registry_before_sha256: str
    registry_after_sha256: str

    schema_id: ClassVar[str] = PACK_REMOVAL_RECEIPT_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_MUTATION_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.key) is not PackRegistryKeyV1:
            raise TypeError("pack removal receipt key is invalid")
        require_sha256(self.pack_id, "removed pack ID")
        require_sha256(self.registry_before_sha256, "prior pack registry digest")
        require_sha256(self.registry_after_sha256, "updated pack registry digest")
        if self.object_path != pack_object_relative_path(self.pack_id):
            raise ValueError("removal receipt object path is not canonical")
        token = self.recovery_path.rsplit("-", 1)[-1]
        if self.recovery_path != pack_recovery_relative_path(self.pack_id, token):
            raise ValueError("removal receipt recovery path is not canonical")

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key.as_dict(),
            "object_path": self.object_path,
            "pack_id": self.pack_id,
            "recovery_path": self.recovery_path,
            "registry_after_sha256": self.registry_after_sha256,
            "registry_before_sha256": self.registry_before_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class _RegistrySnapshotV1:
    registry: PackRegistryV1
    named_identity: tuple[int, int, int, int, int] | None


@dataclass(frozen=True, slots=True)
class _TreeBindingV1:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int

    @classmethod
    def from_metadata(cls, metadata: os.stat_result) -> _TreeBindingV1:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
            mode=metadata.st_mode,
        )

    @property
    def identity(self) -> tuple[int, int]:
        return (self.device, self.inode)


class _exclusive_registry_lock:
    """Owner-only advisory lock for all cooperating registry mutations."""

    __slots__ = ("_packs_descriptor", "_lock_descriptor", "_operation")

    def __init__(
        self,
        packs_descriptor: int,
        operation: PackInstallOperationV1,
    ) -> None:
        self._packs_descriptor = packs_descriptor
        self._lock_descriptor: int | None = None
        self._operation = operation

    def __enter__(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            _refuse(
                PackInstallRefusalCodeV1.REGISTRY_LOCK_UNSAFE,
                self._operation,
                "platform cannot refuse registry-lock symlinks",
            )
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor: int | None = None
        try:
            descriptor = os.open(
                PACK_REGISTRY_LOCK_FILENAME,
                flags,
                0o600,
                dir_fd=self._packs_descriptor,
            )
            before = os.fstat(descriptor)
            named = os.stat(
                PACK_REGISTRY_LOCK_FILENAME,
                dir_fd=self._packs_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != 0
                or _wrong_owner(before)
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ValueError("registry lock is not one canonical regular file")
            os.fchmod(descriptor, 0o600)
            secured = os.fstat(descriptor)
            if (
                stat.S_IMODE(secured.st_mode) != 0o600
                or (secured.st_dev, secured.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ValueError("registry lock could not be secured owner-only")
            os.fsync(descriptor)
            os.fsync(self._packs_descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            final_named = os.stat(
                PACK_REGISTRY_LOCK_FILENAME,
                dir_fd=self._packs_descriptor,
                follow_symlinks=False,
            )
            if (final_named.st_dev, final_named.st_ino) != (
                secured.st_dev,
                secured.st_ino,
            ):
                raise ValueError("registry lock name changed during acquisition")
            self._lock_descriptor = descriptor
        except PackInstallRefused:
            raise
        except (OSError, ValueError, PermissionError) as error:
            _close_suppress(descriptor)
            _refuse(
                PackInstallRefusalCodeV1.REGISTRY_LOCK_UNSAFE,
                self._operation,
                "registry mutation lock is missing, aliased, or not owner-only",
                cause=error,
            )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._lock_descriptor
        self._lock_descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def pack_recovery_relative_path(pack_id: object, recovery_token: object) -> str:
    """Return one collision-safe recoverable address for a logical pack."""

    digest = require_sha256(pack_id, "pack recovery digest")
    if (
        type(recovery_token) is not str
        or len(recovery_token) != 32
        or any(character not in "0123456789abcdef" for character in recovery_token)
    ):
        raise ValueError("pack recovery token must be 128-bit lowercase hexadecimal")
    return (
        f"{PACK_RECOVERY_DIRECTORY}/{PACK_OBJECT_DIGEST_ALGORITHM}/"
        f"{digest[:2]}/{digest}-{recovery_token}"
    )


def install_pack(
    stage: ActivationEligiblePackStageV1,
    *,
    paths: DataPaths,
    environment: PackRuntimeEnvironmentV1,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> PackInstallReceiptV1:
    """Atomically activate one freshly revalidated local staged pack.

    The content-addressed object rename is durable before registry replacement.
    Therefore interruption can expose only a read-only inactive orphan, never a
    registry entry that points at a partially published tree.
    """

    if type(stage) is not ActivationEligiblePackStageV1:
        raise TypeError("pack installation requires ActivationEligiblePackStageV1")
    if type(paths) is not DataPaths:
        raise TypeError("pack installation requires the exact DataPaths provider")
    if type(environment) is not PackRuntimeEnvironmentV1:
        raise TypeError("pack installation requires PackRuntimeEnvironmentV1")
    if type(limits) is not PackValidationLimitsV1:
        raise TypeError("pack installation limits must be PackValidationLimitsV1")

    # This is deliberately the first operation after exact argument validation.
    # Untrusted stage state cannot cause even a governed directory to be created.
    try:
        initial_verification = revalidate_pack_stage(stage, limits=limits)
    except PackValidationRefused as error:
        _refuse(
            PackInstallRefusalCodeV1.STAGE_INVALID,
            PackInstallOperationV1.INSTALL,
            "pack stage failed immediate full revalidation",
            cause=error,
        )

    if stage.staging_root != str(paths.staging):
        _refuse(
            PackInstallRefusalCodeV1.STAGING_ROOT_MISMATCH,
            PackInstallOperationV1.INSTALL,
            "pack stage is outside the selected DataPaths staging area",
        )
    try:
        paths.validate((DataAreaId.PACKS, DataAreaId.STAGING))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.DATA_PATHS_UNSAFE,
            PackInstallOperationV1.INSTALL,
            "selected pack or staging data area is unsafe",
            cause=error,
        )

    root_descriptor: int | None = None
    staging_descriptor: int | None = None
    stage_descriptor: int | None = None
    packs_descriptor: int | None = None
    try:
        root_descriptor = _open_absolute_directory(paths.root, "governed data root")
        staging_descriptor = _open_area_from_root(
            paths,
            root_descriptor,
            DataAreaId.STAGING,
            create=False,
            operation=PackInstallOperationV1.INSTALL,
        )
        _require_identity(
            os.fstat(staging_descriptor),
            (
                stage.verification.staging_root_device,
                stage.verification.staging_root_inode,
            ),
            PackInstallRefusalCodeV1.STAGING_ROOT_MISMATCH,
            PackInstallOperationV1.INSTALL,
            "DataPaths staging root differs from the stage proof",
        )
        packs_descriptor = _open_area_from_root(
            paths,
            root_descriptor,
            DataAreaId.PACKS,
            create=True,
            operation=PackInstallOperationV1.INSTALL,
        )
        _require_safe_mutable_directory(
            os.fstat(packs_descriptor),
            PackInstallOperationV1.INSTALL,
            "pack area",
        )
        if os.fstat(staging_descriptor).st_dev != os.fstat(packs_descriptor).st_dev:
            _refuse(
                PackInstallRefusalCodeV1.OBJECT_PUBLISH_FAILED,
                PackInstallOperationV1.INSTALL,
                "pack and staging areas are on different filesystems",
            )

        with _exclusive_registry_lock(
            packs_descriptor,
            PackInstallOperationV1.INSTALL,
        ):
            try:
                locked_verification = revalidate_pack_stage(stage, limits=limits)
            except PackValidationRefused as error:
                _refuse(
                    PackInstallRefusalCodeV1.STAGE_INVALID,
                    PackInstallOperationV1.INSTALL,
                    "pack stage changed before the locked activation boundary",
                    cause=error,
                )
            if locked_verification != initial_verification:
                _refuse(
                    PackInstallRefusalCodeV1.STAGING_ENTRY_REBOUND,
                    PackInstallOperationV1.INSTALL,
                    "pack-stage verification changed before activation",
                )
            stage_descriptor = _open_directory_at(
                staging_descriptor,
                stage.stage_name,
                "pack stage",
                PackInstallOperationV1.INSTALL,
            )
            _require_identity(
                os.fstat(stage_descriptor),
                (stage.verification.stage_device, stage.verification.stage_inode),
                PackInstallRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackInstallOperationV1.INSTALL,
                "named pack stage differs from its verified inode",
            )
            _require_named_identity(
                staging_descriptor,
                stage.stage_name,
                (stage.verification.stage_device, stage.verification.stage_inode),
                PackInstallRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackInstallOperationV1.INSTALL,
                "pack-stage name changed before activation",
            )

            snapshot = _read_registry_snapshot(
                packs_descriptor,
                operation=PackInstallOperationV1.INSTALL,
            )
            registry_before = snapshot.registry
            before_sha256 = pack_registry_sha256(registry_before)
            try:
                resolution = resolve_pack_dependencies(
                    stage.manifest,
                    registry_before,
                    environment,
                )
            except (LookupError, OverflowError, RecursionError, TypeError, ValueError) as error:
                _refuse(
                    PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED,
                    PackInstallOperationV1.INSTALL,
                    "pack compatibility or local exact dependency resolution failed",
                    cause=error,
                )
            if type(resolution) is not PackDependencyResolutionV1:
                _refuse(
                    PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED,
                    PackInstallOperationV1.INSTALL,
                    "dependency resolver returned an unsupported result",
                )
            proposed_entry = PackRegistryEntryV1.from_manifest(
                stage.manifest,
                resolution.registry_edges,
                active=True,
            )
            prior_entry = registry_before.get(proposed_entry.key)
            if prior_entry is not None:
                _require_immutable_key_binding(prior_entry, proposed_entry)

            for dependency in resolution.dependency_first_order:
                try:
                    _verify_registry_object(packs_descriptor, dependency.entry)
                except PackInstallRefused as error:
                    _refuse(
                        PackInstallRefusalCodeV1.DEPENDENCY_OBJECT_INVALID,
                        PackInstallOperationV1.INSTALL,
                        "a resolved local dependency object is missing or invalid",
                        cause=error,
                    )

            object_parent, object_leaf = _open_object_parent(
                packs_descriptor,
                proposed_entry.pack_id,
                proposed_entry.object_path,
                create=True,
                operation=PackInstallOperationV1.INSTALL,
            )
            installed_new_object = False
            try:
                object_exists = _entry_exists_at(object_parent, object_leaf)
                if prior_entry is not None and not object_exists:
                    _refuse(
                        PackInstallRefusalCodeV1.OBJECT_TREE_INVALID,
                        PackInstallOperationV1.INSTALL,
                        "an existing registry binding has no exact installed object",
                    )
                if object_exists:
                    object_descriptor = _open_directory_at(
                        object_parent,
                        object_leaf,
                        "installed pack object",
                        PackInstallOperationV1.INSTALL,
                    )
                    try:
                        _verify_read_only_pack_tree(
                            object_descriptor,
                            proposed_entry.manifest,
                            operation=PackInstallOperationV1.INSTALL,
                        )
                    finally:
                        os.close(object_descriptor)
                else:
                    _make_stage_read_only(
                        stage_descriptor,
                        stage,
                    )
                    _require_named_identity(
                        staging_descriptor,
                        stage.stage_name,
                        (stage.verification.stage_device, stage.verification.stage_inode),
                        PackInstallRefusalCodeV1.STAGING_ENTRY_REBOUND,
                        PackInstallOperationV1.INSTALL,
                        "read-only pack stage was rebound before publication",
                    )
                    try:
                        os.rename(
                            stage.stage_name,
                            object_leaf,
                            src_dir_fd=staging_descriptor,
                            dst_dir_fd=object_parent,
                        )
                    except OSError as error:
                        _restore_stage_modes(stage_descriptor, stage.manifest)
                        _refuse(
                            PackInstallRefusalCodeV1.OBJECT_PUBLISH_FAILED,
                            PackInstallOperationV1.INSTALL,
                            "verified stage could not be atomically published",
                            cause=error,
                        )
                    installed_new_object = True
                    os.fsync(staging_descriptor)
                    os.fsync(object_parent)
                    object_descriptor = _open_directory_at(
                        object_parent,
                        object_leaf,
                        "published pack object",
                        PackInstallOperationV1.INSTALL,
                    )
                    try:
                        _require_identity(
                            os.fstat(object_descriptor),
                            (
                                stage.verification.stage_device,
                                stage.verification.stage_inode,
                            ),
                            PackInstallRefusalCodeV1.OBJECT_PUBLISH_FAILED,
                            PackInstallOperationV1.INSTALL,
                            "published object is not the verified staging inode",
                        )
                        _verify_read_only_pack_tree(
                            object_descriptor,
                            proposed_entry.manifest,
                            operation=PackInstallOperationV1.INSTALL,
                        )
                    finally:
                        os.close(object_descriptor)
            finally:
                os.close(object_parent)

            registry_after = _registry_with_active_entry(
                registry_before,
                proposed_entry,
            )
            registry_changed = registry_after != registry_before
            if registry_changed:
                _replace_registry_snapshot(
                    packs_descriptor,
                    snapshot,
                    registry_after,
                    operation=PackInstallOperationV1.INSTALL,
                )
            after_sha256 = pack_registry_sha256(registry_after)
            return PackInstallReceiptV1(
                key=proposed_entry.key,
                pack_id=proposed_entry.pack_id,
                object_path=proposed_entry.object_path,
                stage_verification_sha256=stage.verification.sha256,
                registry_before_sha256=before_sha256,
                registry_after_sha256=after_sha256,
                resolved_dependencies=proposed_entry.resolved_dependencies,
                installed_new_object=installed_new_object,
                registry_changed=registry_changed,
            )
    except PackInstallRefused:
        raise
    except (OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.PACK_AREA_UNSAFE,
            PackInstallOperationV1.INSTALL,
            "pack installation could not remain confined to governed areas",
            cause=error,
        )
    finally:
        _close_suppress(stage_descriptor)
        _close_suppress(packs_descriptor)
        _close_suppress(staging_descriptor)
        _close_suppress(root_descriptor)


def deactivate_pack(
    key: PackRegistryKeyV1,
    *,
    paths: DataPaths,
) -> PackDeactivationReceiptV1:
    """Atomically mark one installed pack inactive after dependent refusal."""

    _require_key_and_paths(key, paths, PackInstallOperationV1.DEACTIVATE)
    root_descriptor, packs_descriptor = _open_existing_packs_area(
        paths,
        PackInstallOperationV1.DEACTIVATE,
    )
    try:
        with _exclusive_registry_lock(
            packs_descriptor,
            PackInstallOperationV1.DEACTIVATE,
        ):
            snapshot = _read_registry_snapshot(
                packs_descriptor,
                operation=PackInstallOperationV1.DEACTIVATE,
            )
            before = snapshot.registry
            entry = before.get(key)
            if entry is None:
                _refuse(
                    PackInstallRefusalCodeV1.PACK_NOT_INSTALLED,
                    PackInstallOperationV1.DEACTIVATE,
                    "registry key is not installed",
                )
            before_sha256 = pack_registry_sha256(before)
            if not entry.active:
                return PackDeactivationReceiptV1(
                    key=entry.key,
                    pack_id=entry.pack_id,
                    object_path=entry.object_path,
                    registry_before_sha256=before_sha256,
                    registry_after_sha256=before_sha256,
                    already_inactive=True,
                )
            if before.dependents_of(key, active_only=True):
                _refuse(
                    PackInstallRefusalCodeV1.ACTIVE_DEPENDENTS,
                    PackInstallOperationV1.DEACTIVATE,
                    "pack has active dependents and cannot be deactivated",
                )
            inactive = PackRegistryEntryV1(
                key=entry.key,
                pack_id=entry.pack_id,
                manifest_sha256=entry.manifest_sha256,
                object_path=entry.object_path,
                manifest=entry.manifest,
                resolved_dependencies=entry.resolved_dependencies,
                active=False,
            )
            after = _registry_replacing_entry(before, inactive)
            _replace_registry_snapshot(
                packs_descriptor,
                snapshot,
                after,
                operation=PackInstallOperationV1.DEACTIVATE,
            )
            return PackDeactivationReceiptV1(
                key=entry.key,
                pack_id=entry.pack_id,
                object_path=entry.object_path,
                registry_before_sha256=before_sha256,
                registry_after_sha256=pack_registry_sha256(after),
                already_inactive=False,
            )
    finally:
        os.close(packs_descriptor)
        os.close(root_descriptor)


def remove_deactivated_pack(
    key: PackRegistryKeyV1,
    *,
    paths: DataPaths,
) -> PackRemovalReceiptV1:
    """Remove registry truth first, then recoverably rename the exact CAS inode."""

    _require_key_and_paths(key, paths, PackInstallOperationV1.REMOVE)
    root_descriptor, packs_descriptor = _open_existing_packs_area(
        paths,
        PackInstallOperationV1.REMOVE,
    )
    try:
        with _exclusive_registry_lock(
            packs_descriptor,
            PackInstallOperationV1.REMOVE,
        ):
            snapshot = _read_registry_snapshot(
                packs_descriptor,
                operation=PackInstallOperationV1.REMOVE,
            )
            before = snapshot.registry
            entry = before.get(key)
            if entry is None:
                _refuse(
                    PackInstallRefusalCodeV1.PACK_NOT_INSTALLED,
                    PackInstallOperationV1.REMOVE,
                    "registry key is not installed",
                )
            if entry.active:
                _refuse(
                    PackInstallRefusalCodeV1.PACK_STILL_ACTIVE,
                    PackInstallOperationV1.REMOVE,
                    "pack must be deactivated before recoverable removal",
                )
            if before.dependents_of(key, active_only=True):
                _refuse(
                    PackInstallRefusalCodeV1.ACTIVE_DEPENDENTS,
                    PackInstallOperationV1.REMOVE,
                    "pack has active dependents and cannot be removed",
                )

            object_parent, object_leaf = _open_object_parent(
                packs_descriptor,
                entry.pack_id,
                entry.object_path,
                create=False,
                operation=PackInstallOperationV1.REMOVE,
            )
            object_descriptor: int | None = None
            recovery_parent: int | None = None
            try:
                object_descriptor = _open_directory_at(
                    object_parent,
                    object_leaf,
                    "installed pack object",
                    PackInstallOperationV1.REMOVE,
                )
                object_metadata = os.fstat(object_descriptor)
                _verify_read_only_pack_tree(
                    object_descriptor,
                    entry.manifest,
                    operation=PackInstallOperationV1.REMOVE,
                )
                recovery_path: str | None = None
                recovery_leaf: str | None = None
                for _ in range(32):
                    candidate = pack_recovery_relative_path(
                        entry.pack_id,
                        secrets.token_hex(16),
                    )
                    if recovery_parent is None:
                        recovery_parent, candidate_leaf = _open_recovery_parent(
                            packs_descriptor,
                            entry.pack_id,
                            candidate,
                        )
                    else:
                        candidate_leaf = PurePosixPath(candidate).name
                    if not _entry_exists_at(recovery_parent, candidate_leaf):
                        recovery_path = candidate
                        recovery_leaf = candidate_leaf
                        break
                if (
                    recovery_path is None
                    or recovery_leaf is None
                    or recovery_parent is None
                ):
                    _refuse(
                        PackInstallRefusalCodeV1.RECOVERY_CONFLICT,
                        PackInstallOperationV1.REMOVE,
                        "could not allocate a fresh recoverable pack address",
                    )
                if os.fstat(object_parent).st_dev != os.fstat(recovery_parent).st_dev:
                    _refuse(
                        PackInstallRefusalCodeV1.RECOVERY_MOVE_FAILED,
                        PackInstallOperationV1.REMOVE,
                        "object and recovery areas are on different filesystems",
                    )
                _require_named_identity(
                    object_parent,
                    object_leaf,
                    (object_metadata.st_dev, object_metadata.st_ino),
                    PackInstallRefusalCodeV1.OBJECT_CONFLICT,
                    PackInstallOperationV1.REMOVE,
                    "installed object name changed before removal",
                )

                after = PackRegistryV1(
                    entries=tuple(item for item in before.entries if item.key != key)
                )
                before_sha256 = pack_registry_sha256(before)
                # Registry truth is made durable before the object name can move.
                _replace_registry_snapshot(
                    packs_descriptor,
                    snapshot,
                    after,
                    operation=PackInstallOperationV1.REMOVE,
                )
                try:
                    _require_named_identity(
                        object_parent,
                        object_leaf,
                        (object_metadata.st_dev, object_metadata.st_ino),
                        PackInstallRefusalCodeV1.OBJECT_CONFLICT,
                        PackInstallOperationV1.REMOVE,
                        "installed object changed after registry removal",
                    )
                    os.rename(
                        object_leaf,
                        recovery_leaf,
                        src_dir_fd=object_parent,
                        dst_dir_fd=recovery_parent,
                    )
                    moved = os.stat(
                        recovery_leaf,
                        dir_fd=recovery_parent,
                        follow_symlinks=False,
                    )
                    if (moved.st_dev, moved.st_ino) != (
                        object_metadata.st_dev,
                        object_metadata.st_ino,
                    ):
                        raise ValueError("recovery target is not the exact CAS inode")
                    os.fsync(object_parent)
                    os.fsync(recovery_parent)
                except (OSError, PackInstallRefused, ValueError) as error:
                    _refuse(
                        PackInstallRefusalCodeV1.RECOVERY_MOVE_FAILED,
                        PackInstallOperationV1.REMOVE,
                        "registry was removed but the exact CAS object remains orphaned",
                        cause=error,
                    )
                return PackRemovalReceiptV1(
                    key=entry.key,
                    pack_id=entry.pack_id,
                    object_path=entry.object_path,
                    recovery_path=recovery_path,
                    registry_before_sha256=before_sha256,
                    registry_after_sha256=pack_registry_sha256(after),
                )
            finally:
                _close_suppress(recovery_parent)
                _close_suppress(object_descriptor)
                os.close(object_parent)
    finally:
        os.close(packs_descriptor)
        os.close(root_descriptor)


def read_pack_registry(*, paths: DataPaths) -> PackRegistryV1:
    """Read one bounded atomic registry snapshot without creating any path."""

    if type(paths) is not DataPaths:
        raise TypeError("pack registry read requires the exact DataPaths provider")
    try:
        paths.validate(DataAreaId.PACKS)
        root_descriptor = _open_absolute_directory_optional(paths.root)
        if root_descriptor is None:
            return PackRegistryV1.empty()
        try:
            packs_descriptor = _open_area_from_root_optional(
                paths,
                root_descriptor,
                DataAreaId.PACKS,
            )
            if packs_descriptor is None:
                return PackRegistryV1.empty()
            try:
                return _read_registry_snapshot(
                    packs_descriptor,
                    operation=PackInstallOperationV1.READ_REGISTRY,
                ).registry
            finally:
                os.close(packs_descriptor)
        finally:
            os.close(root_descriptor)
    except PackInstallRefused:
        raise
    except (OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_UNSAFE,
            PackInstallOperationV1.READ_REGISTRY,
            "pack registry cannot be read safely",
            cause=error,
        )


def lookup_installed_pack(
    key: PackRegistryKeyV1,
    *,
    paths: DataPaths,
) -> PackRegistryEntryV1 | None:
    """Return one exact installed registry entry from a bounded read snapshot."""

    if type(key) is not PackRegistryKeyV1:
        raise TypeError("installed pack lookup requires PackRegistryKeyV1")
    return read_pack_registry(paths=paths).get(key)


def _require_key_and_paths(
    key: object,
    paths: object,
    operation: PackInstallOperationV1,
) -> None:
    if type(key) is not PackRegistryKeyV1:
        raise TypeError(f"pack {operation.value.lower()} requires PackRegistryKeyV1")
    if type(paths) is not DataPaths:
        raise TypeError(
            f"pack {operation.value.lower()} requires the exact DataPaths provider"
        )


def _open_existing_packs_area(
    paths: DataPaths,
    operation: PackInstallOperationV1,
) -> tuple[int, int]:
    try:
        paths.validate(DataAreaId.PACKS)
        root_descriptor = _open_absolute_directory(paths.root, "governed data root")
        try:
            packs_descriptor = _open_area_from_root(
                paths,
                root_descriptor,
                DataAreaId.PACKS,
                create=False,
                operation=operation,
            )
            _require_safe_mutable_directory(
                os.fstat(packs_descriptor),
                operation,
                "pack area",
            )
            return root_descriptor, packs_descriptor
        except BaseException:
            os.close(root_descriptor)
            raise
    except PackInstallRefused:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.PACK_AREA_UNSAFE,
            operation,
            "governed pack area is missing or unsafe",
            cause=error,
        )


def _registry_with_active_entry(
    registry: PackRegistryV1,
    entry: PackRegistryEntryV1,
) -> PackRegistryV1:
    existing = registry.get(entry.key)
    if existing is not None:
        _require_immutable_key_binding(existing, entry)
    replacement = tuple(item for item in registry.entries if item.key != entry.key) + (
        entry,
    )
    try:
        return PackRegistryV1(entries=tuple(sorted(replacement, key=lambda item: item.sort_key)))
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.DEPENDENCY_RESOLUTION_FAILED,
            PackInstallOperationV1.INSTALL,
            "proposed registry violates dependency or identity invariants",
            cause=error,
        )


def _registry_replacing_entry(
    registry: PackRegistryV1,
    replacement: PackRegistryEntryV1,
) -> PackRegistryV1:
    entries = tuple(
        replacement if item.key == replacement.key else item
        for item in registry.entries
    )
    try:
        return PackRegistryV1(entries=entries)
    except (OverflowError, RecursionError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.ACTIVE_DEPENDENTS,
            PackInstallOperationV1.DEACTIVATE,
            "registry dependency invariants refuse deactivation",
            cause=error,
        )


def _require_immutable_key_binding(
    prior: PackRegistryEntryV1,
    proposed: PackRegistryEntryV1,
) -> None:
    if (
        prior.key != proposed.key
        or not hmac.compare_digest(prior.pack_id, proposed.pack_id)
        or not hmac.compare_digest(prior.manifest_sha256, proposed.manifest_sha256)
        or prior.object_path != proposed.object_path
        or prior.manifest != proposed.manifest
        or prior.resolved_dependencies != proposed.resolved_dependencies
    ):
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_KEY_CONFLICT,
            PackInstallOperationV1.INSTALL,
            "an existing registry key cannot be rebound to different content",
        )


def _read_registry_snapshot(
    packs_descriptor: int,
    *,
    operation: PackInstallOperationV1,
) -> _RegistrySnapshotV1:
    flags = os.O_RDONLY | _nofollow_flag()
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(
            PACK_REGISTRY_FILENAME,
            flags,
            dir_fd=packs_descriptor,
        )
    except FileNotFoundError:
        return _RegistrySnapshotV1(PackRegistryV1.empty(), None)
    except OSError as error:
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_UNSAFE,
            operation,
            "registry path is missing or unsafe",
            cause=error,
        )
    try:
        before = os.fstat(descriptor)
        named = os.stat(
            PACK_REGISTRY_FILENAME,
            dir_fd=packs_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _wrong_owner(before)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > PACK_REGISTRY_MAX_BYTE_COUNT
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ValueError("registry file metadata is unsafe")
        raw = _read_exact_descriptor(
            descriptor,
            before,
            PACK_REGISTRY_MAX_BYTE_COUNT,
            "pack registry",
        )
        final = os.fstat(descriptor)
        final_named = os.stat(
            PACK_REGISTRY_FILENAME,
            dir_fd=packs_descriptor,
            follow_symlinks=False,
        )
        identity = _registry_identity(final)
        if (
            identity != _registry_identity(before)
            or (final_named.st_dev, final_named.st_ino) != (final.st_dev, final.st_ino)
        ):
            raise ValueError("registry changed during its bounded read")
        registry = load_pack_registry_bytes(raw)
        return _RegistrySnapshotV1(registry=registry, named_identity=identity)
    except PackInstallRefused:
        raise
    except (OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_UNSAFE,
            operation,
            "registry is not one exact canonical snapshot",
            cause=error,
        )
    finally:
        os.close(descriptor)


def _replace_registry_snapshot(
    packs_descriptor: int,
    before: _RegistrySnapshotV1,
    after: PackRegistryV1,
    *,
    operation: PackInstallOperationV1,
) -> None:
    raw = canonical_pack_registry_bytes(after)
    _require_registry_name_binding(packs_descriptor, before, operation)
    temp_name = (
        _REGISTRY_TEMP_PREFIX + secrets.token_hex(16) + _REGISTRY_TEMP_SUFFIX
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor: int | None = None
    temp_identity: tuple[int, int] | None = None
    renamed = False
    try:
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=packs_descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _wrong_owner(metadata)
        ):
            raise ValueError("temporary registry is not one owner-bound regular file")
        temp_identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        secured = os.fstat(descriptor)
        if (
            (secured.st_dev, secured.st_ino) != temp_identity
            or secured.st_size != len(raw)
            or stat.S_IMODE(secured.st_mode) != 0o600
            or secured.st_nlink != 1
        ):
            raise ValueError("temporary registry changed before atomic replacement")
        named_temp = os.stat(temp_name, dir_fd=packs_descriptor, follow_symlinks=False)
        if (named_temp.st_dev, named_temp.st_ino) != temp_identity:
            raise ValueError("temporary registry name was rebound")
        _require_registry_name_binding(packs_descriptor, before, operation)
        os.rename(
            temp_name,
            PACK_REGISTRY_FILENAME,
            src_dir_fd=packs_descriptor,
            dst_dir_fd=packs_descriptor,
        )
        renamed = True
        os.fsync(packs_descriptor)
        installed = os.stat(
            PACK_REGISTRY_FILENAME,
            dir_fd=packs_descriptor,
            follow_symlinks=False,
        )
        if (installed.st_dev, installed.st_ino) != temp_identity:
            raise ValueError("atomic registry replacement did not publish temp inode")
    except PackInstallRefused:
        raise
    except (OSError, ValueError) as error:
        _refuse(
            (
                PackInstallRefusalCodeV1.REGISTRY_DURABILITY_UNCERTAIN
                if renamed
                else PackInstallRefusalCodeV1.REGISTRY_WRITE_FAILED
            ),
            operation,
            (
                "new registry is visible but durable sync could not be confirmed; "
                "reread registry truth before retrying"
                if renamed
                else "canonical registry could not be atomically replaced"
            ),
            cause=error,
        )
    finally:
        _close_suppress(descriptor)
        if not renamed and temp_identity is not None:
            _unlink_exact_name_suppress(packs_descriptor, temp_name, temp_identity)


def _require_registry_name_binding(
    packs_descriptor: int,
    snapshot: _RegistrySnapshotV1,
    operation: PackInstallOperationV1,
) -> None:
    if snapshot.named_identity is None:
        if _entry_exists_at(packs_descriptor, PACK_REGISTRY_FILENAME):
            _refuse(
                PackInstallRefusalCodeV1.REGISTRY_CHANGED,
                operation,
                "registry appeared after the locked snapshot",
            )
        return
    try:
        current = os.stat(
            PACK_REGISTRY_FILENAME,
            dir_fd=packs_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_CHANGED,
            operation,
            "registry disappeared after the locked snapshot",
            cause=error,
        )
    if _registry_identity(current) != snapshot.named_identity:
        _refuse(
            PackInstallRefusalCodeV1.REGISTRY_CHANGED,
            operation,
            "registry inode changed after the locked snapshot",
        )


def _registry_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_registry_object(
    packs_descriptor: int,
    entry: PackRegistryEntryV1,
) -> None:
    parent, leaf = _open_object_parent(
        packs_descriptor,
        entry.pack_id,
        entry.object_path,
        create=False,
        operation=PackInstallOperationV1.INSTALL,
    )
    descriptor: int | None = None
    try:
        descriptor = _open_directory_at(
            parent,
            leaf,
            "dependency pack object",
            PackInstallOperationV1.INSTALL,
        )
        _verify_read_only_pack_tree(
            descriptor,
            entry.manifest,
            operation=PackInstallOperationV1.INSTALL,
        )
    finally:
        _close_suppress(descriptor)
        os.close(parent)


def _open_object_parent(
    packs_descriptor: int,
    pack_id: str,
    declared_path: str,
    *,
    create: bool,
    operation: PackInstallOperationV1,
) -> tuple[int, str]:
    canonical = pack_object_relative_path(pack_id)
    if type(declared_path) is not str or declared_path != canonical:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            operation,
            "registry object path is not the canonical digest address",
        )
    return _open_relative_parent(
        packs_descriptor,
        canonical,
        create=create,
        operation=operation,
        label="pack object address",
    )


def _open_recovery_parent(
    packs_descriptor: int,
    pack_id: str,
    declared_path: str,
) -> tuple[int, str]:
    token = declared_path.rsplit("-", 1)[-1]
    try:
        canonical = pack_recovery_relative_path(pack_id, token)
    except (TypeError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            PackInstallOperationV1.REMOVE,
            "recovery path is not a canonical digest/token address",
            cause=error,
        )
    if declared_path != canonical:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            PackInstallOperationV1.REMOVE,
            "recovery path is not the canonical digest address",
        )
    return _open_relative_parent(
        packs_descriptor,
        canonical,
        create=True,
        operation=PackInstallOperationV1.REMOVE,
        label="pack recovery address",
    )


def _open_relative_parent(
    root_descriptor: int,
    relative_path: str,
    *,
    create: bool,
    operation: PackInstallOperationV1,
    label: str,
) -> tuple[int, str]:
    parts = PurePosixPath(relative_path).parts
    if (
        type(relative_path) is not str
        or not parts
        or str(PurePosixPath(*parts)) != relative_path
        or any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in parts)
    ):
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            operation,
            f"{label} is not a confined canonical relative path",
        )
    current = os.dup(root_descriptor)
    try:
        for component in parts[:-1]:
            following = _open_or_create_directory_at(
                current,
                component,
                create=create,
                operation=operation,
                label=label,
            )
            os.close(current)
            current = following
        result = current
        current = -1
        return result, parts[-1]
    except PackInstallRefused:
        raise
    except OSError as error:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_TREE_INVALID,
            operation,
            f"{label} is missing, linked, or unsafe",
            cause=error,
        )
    finally:
        _close_suppress(current if current >= 0 else None)


def _make_stage_read_only(
    stage_descriptor: int,
    stage: ActivationEligiblePackStageV1,
) -> None:
    manifest = stage.manifest
    files, directories = _expected_tree(manifest)
    root_binding, file_bindings, directory_bindings = _scan_pack_tree(
        stage_descriptor,
        files,
        directories,
        root_mode=0o700,
        file_mode=0o600,
        directory_mode=0o700,
        operation=PackInstallOperationV1.INSTALL,
    )
    if root_binding.identity != (
        stage.verification.stage_device,
        stage.verification.stage_inode,
    ):
        _refuse(
            PackInstallRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackInstallOperationV1.INSTALL,
            "stage tree root differs from its activation proof",
        )
    _verify_tree_payloads(
        stage_descriptor,
        manifest,
        file_bindings,
        expected_file_mode=0o600,
        operation=PackInstallOperationV1.INSTALL,
    )
    converted = False
    try:
        for path in sorted(files, key=lambda item: item.encode("utf-8")):
            _chmod_bound_path(
                stage_descriptor,
                path,
                file_bindings[path],
                directory=False,
                mode=0o400,
                operation=PackInstallOperationV1.INSTALL,
            )
        for path in sorted(
            directories,
            key=lambda item: (-item.count("/"), item.encode("utf-8")),
        ):
            _chmod_bound_path(
                stage_descriptor,
                path,
                directory_bindings[path],
                directory=True,
                mode=0o500,
                operation=PackInstallOperationV1.INSTALL,
            )
        os.fchmod(stage_descriptor, 0o500)
        os.fsync(stage_descriptor)
        _verify_read_only_pack_tree(
            stage_descriptor,
            manifest,
            operation=PackInstallOperationV1.INSTALL,
        )
        converted = True
    finally:
        if not converted:
            _restore_stage_modes(stage_descriptor, manifest)


def _restore_stage_modes(stage_descriptor: int, manifest: PackManifestV1) -> None:
    """Best-effort exact-tree mode rollback for a not-yet-published stage."""

    try:
        os.fchmod(stage_descriptor, 0o700)
        _, directories = _expected_tree(manifest)
        for path in sorted(
            directories,
            key=lambda item: (item.count("/"), item.encode("utf-8")),
        ):
            _chmod_path_best_effort(stage_descriptor, path, directory=True, mode=0o700)
        files, _ = _expected_tree(manifest)
        for path in sorted(files, key=lambda item: item.encode("utf-8")):
            _chmod_path_best_effort(stage_descriptor, path, directory=False, mode=0o600)
        os.fsync(stage_descriptor)
    except OSError:
        pass


def _verify_read_only_pack_tree(
    root_descriptor: int,
    manifest: PackManifestV1,
    *,
    operation: PackInstallOperationV1,
) -> None:
    files, directories = _expected_tree(manifest)
    _, file_bindings, _ = _scan_pack_tree(
        root_descriptor,
        files,
        directories,
        root_mode=0o500,
        file_mode=0o400,
        directory_mode=0o500,
        operation=operation,
    )
    _verify_tree_payloads(
        root_descriptor,
        manifest,
        file_bindings,
        expected_file_mode=0o400,
        operation=operation,
    )


def _expected_tree(
    manifest: PackManifestV1,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(manifest) is not PackManifestV1:
        raise TypeError("pack tree verification requires PackManifestV1")
    files = (K2PACK_MANIFEST_PATH,) + tuple(item.path for item in manifest.inventory)
    directories: set[str] = set()
    for path in files:
        parts = PurePosixPath(path).parts[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add("/".join(parts[:depth]))
    return (
        tuple(sorted(files, key=lambda item: item.encode("utf-8"))),
        tuple(sorted(directories, key=lambda item: item.encode("utf-8"))),
    )


def _scan_pack_tree(
    root_descriptor: int,
    expected_files: tuple[str, ...],
    expected_directories: tuple[str, ...],
    *,
    root_mode: int,
    file_mode: int,
    directory_mode: int,
    operation: PackInstallOperationV1,
) -> tuple[
    _TreeBindingV1,
    dict[str, _TreeBindingV1],
    dict[str, _TreeBindingV1],
]:
    root_before = os.fstat(root_descriptor)
    _require_directory_metadata(root_before, root_mode, operation, "pack tree root")
    file_set = set(expected_files)
    directory_set = set(expected_directories)
    maximum_entries = len(file_set) + len(directory_set)
    found_files: dict[str, _TreeBindingV1] = {}
    found_directories: dict[str, _TreeBindingV1] = {}
    visited = 0

    def visit(directory_descriptor: int, prefix: str) -> None:
        nonlocal visited
        try:
            with os.scandir(directory_descriptor) as entries:
                for item in entries:
                    visited += 1
                    if visited > maximum_entries:
                        raise ValueError("pack tree exceeds its exact entry ceiling")
                    if type(item.name) is not str or not item.name:
                        raise ValueError("pack tree contains an invalid entry name")
                    path = f"{prefix}/{item.name}" if prefix else item.name
                    if path not in file_set and path not in directory_set:
                        raise ValueError("pack tree contains an undeclared entry")
                    before = item.stat(follow_symlinks=False)
                    if stat.S_ISDIR(before.st_mode):
                        if path not in directory_set:
                            raise ValueError("pack tree has a file/directory collision")
                        child = _open_directory_at(
                            directory_descriptor,
                            item.name,
                            "pack tree directory",
                            operation,
                        )
                        try:
                            pinned = os.fstat(child)
                            after = os.stat(
                                item.name,
                                dir_fd=directory_descriptor,
                                follow_symlinks=False,
                            )
                            if (
                                (before.st_dev, before.st_ino)
                                != (pinned.st_dev, pinned.st_ino)
                                or (after.st_dev, after.st_ino)
                                != (pinned.st_dev, pinned.st_ino)
                            ):
                                raise ValueError("pack directory changed during traversal")
                            _require_directory_metadata(
                                pinned,
                                directory_mode,
                                operation,
                                "pack tree directory",
                            )
                            found_directories[path] = _TreeBindingV1.from_metadata(pinned)
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(before.st_mode):
                        if path not in file_set:
                            raise ValueError("pack tree has a directory/file collision")
                        if (
                            before.st_nlink != 1
                            or stat.S_IMODE(before.st_mode) != file_mode
                            or _wrong_owner(before)
                        ):
                            raise ValueError("pack file is not one exact owner-bound file")
                        found_files[path] = _TreeBindingV1.from_metadata(before)
                    else:
                        raise ValueError("pack tree contains a link or special entry")
        except OSError as error:
            raise ValueError("pack tree cannot be scanned safely") from error

    try:
        visit(root_descriptor, "")
        if set(found_files) != file_set or set(found_directories) != directory_set:
            raise ValueError("pack tree differs from its exact manifest inventory")
        root_after = os.fstat(root_descriptor)
        if (
            (root_after.st_dev, root_after.st_ino)
            != (root_before.st_dev, root_before.st_ino)
            or root_after.st_mtime_ns != root_before.st_mtime_ns
            or root_after.st_ctime_ns != root_before.st_ctime_ns
            or stat.S_IMODE(root_after.st_mode) != root_mode
        ):
            raise ValueError("pack tree root changed during traversal")
        return (
            _TreeBindingV1.from_metadata(root_after),
            found_files,
            found_directories,
        )
    except PackInstallRefused:
        raise
    except (OSError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_TREE_INVALID,
            operation,
            "pack tree is not one closed, exact, regular-file inventory",
            cause=error,
        )


def _verify_tree_payloads(
    root_descriptor: int,
    manifest: PackManifestV1,
    bindings: dict[str, _TreeBindingV1],
    *,
    expected_file_mode: int,
    operation: PackInstallOperationV1,
) -> None:
    canonical_manifest = canonical_manifest_bytes(manifest)
    _verify_bound_file(
        root_descriptor,
        K2PACK_MANIFEST_PATH,
        bindings[K2PACK_MANIFEST_PATH],
        expected_size=len(canonical_manifest),
        expected_sha256=hashlib.sha256(canonical_manifest).hexdigest(),
        expected_file_mode=expected_file_mode,
        operation=operation,
        exact_bytes=canonical_manifest,
    )
    for item in manifest.inventory:
        _verify_bound_file(
            root_descriptor,
            item.path,
            bindings[item.path],
            expected_size=item.byte_count,
            expected_sha256=item.sha256,
            expected_file_mode=expected_file_mode,
            operation=operation,
            exact_bytes=None,
        )


def _verify_bound_file(
    root_descriptor: int,
    path: str,
    binding: _TreeBindingV1,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_file_mode: int,
    operation: PackInstallOperationV1,
    exact_bytes: bytes | None,
) -> None:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_relative_parent(
            root_descriptor,
            path,
            create=False,
            operation=operation,
            label="pack file path",
        )
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        flags = os.O_RDONLY | _nofollow_flag()
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(leaf, flags, dir_fd=parent)
        pinned = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != binding.identity
            or (pinned.st_dev, pinned.st_ino) != binding.identity
            or not stat.S_ISREG(pinned.st_mode)
            or pinned.st_nlink != 1
            or pinned.st_size != expected_size
            or stat.S_IMODE(pinned.st_mode) != expected_file_mode
            or _wrong_owner(pinned)
        ):
            raise ValueError("pack file metadata differs from its bound inventory")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if exact_bytes is not None else None
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(_IO_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("pack file ended before its declared byte count")
            remaining -= len(chunk)
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        if os.read(descriptor, 1):
            raise ValueError("pack file grew beyond its declared byte count")
        final = os.fstat(descriptor)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            (final.st_dev, final.st_ino) != binding.identity
            or (named.st_dev, named.st_ino) != binding.identity
            or final.st_size != pinned.st_size
            or final.st_mtime_ns != pinned.st_mtime_ns
            or final.st_ctime_ns != pinned.st_ctime_ns
            or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
            or (chunks is not None and b"".join(chunks) != exact_bytes)
        ):
            raise ValueError("pack file changed or differs from its declared digest")
    except PackInstallRefused:
        raise
    except (OSError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_TREE_INVALID,
            operation,
            "pack file bytes differ from the exact verified inventory",
            cause=error,
        )
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _chmod_bound_path(
    root_descriptor: int,
    path: str,
    binding: _TreeBindingV1,
    *,
    directory: bool,
    mode: int,
    operation: PackInstallOperationV1,
) -> None:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_relative_parent(
            root_descriptor,
            path,
            create=False,
            operation=operation,
            label="pack mode-transition path",
        )
        descriptor = (
            _open_directory_at(parent, leaf, "pack directory", operation)
            if directory
            else os.open(leaf, os.O_RDONLY | _nofollow_flag(), dir_fd=parent)
        )
        metadata = os.fstat(descriptor)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            (metadata.st_dev, metadata.st_ino) != binding.identity
            or (named.st_dev, named.st_ino) != binding.identity
        ):
            raise ValueError("pack entry changed before mode transition")
        os.fchmod(descriptor, mode)
        secured = os.fstat(descriptor)
        if (
            (secured.st_dev, secured.st_ino) != binding.identity
            or stat.S_IMODE(secured.st_mode) != mode
        ):
            raise ValueError("pack entry mode transition did not bind exact inode")
        os.fsync(descriptor)
    except PackInstallRefused:
        raise
    except (OSError, ValueError) as error:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PUBLISH_FAILED,
            operation,
            "pack tree could not be made durably read-only",
            cause=error,
        )
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _chmod_path_best_effort(
    root_descriptor: int,
    path: str,
    *,
    directory: bool,
    mode: int,
) -> None:
    parent: int | None = None
    descriptor: int | None = None
    try:
        parent, leaf = _open_relative_parent(
            root_descriptor,
            path,
            create=False,
            operation=PackInstallOperationV1.INSTALL,
            label="pack mode rollback path",
        )
        descriptor = (
            _open_directory_at(
                parent,
                leaf,
                "pack rollback directory",
                PackInstallOperationV1.INSTALL,
            )
            if directory
            else os.open(leaf, os.O_RDONLY | _nofollow_flag(), dir_fd=parent)
        )
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except (OSError, PackInstallRefused):
        pass
    finally:
        _close_suppress(descriptor)
        _close_suppress(parent)


def _require_directory_metadata(
    metadata: os.stat_result,
    mode: int,
    operation: PackInstallOperationV1,
    label: str,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or _wrong_owner(metadata)
    ):
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_TREE_INVALID,
            operation,
            f"{label} is not one exact owner-bound directory",
        )


def _open_area_from_root(
    paths: DataPaths,
    root_descriptor: int,
    area_id: DataAreaId,
    *,
    create: bool,
    operation: PackInstallOperationV1,
) -> int:
    current = os.dup(root_descriptor)
    try:
        for component in paths.area_children[area_id].split("/"):
            following = _open_or_create_directory_at(
                current,
                component,
                create=create,
                operation=operation,
                label=f"{area_id.value} data area",
            )
            os.close(current)
            current = following
        result = current
        current = -1
        return result
    finally:
        _close_suppress(current if current >= 0 else None)


def _open_area_from_root_optional(
    paths: DataPaths,
    root_descriptor: int,
    area_id: DataAreaId,
) -> int | None:
    current = os.dup(root_descriptor)
    try:
        for component in paths.area_children[area_id].split("/"):
            try:
                following = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                return None
            os.close(current)
            current = following
        result = current
        current = -1
        return result
    finally:
        _close_suppress(current if current >= 0 else None)


def _open_or_create_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    operation: PackInstallOperationV1,
    label: str,
) -> int:
    if type(name) is not str or not name or name in {".", ".."} or "/" in name or "\\" in name:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            operation,
            f"{label} contains an invalid path component",
        )
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _wrong_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        _refuse(
            PackInstallRefusalCodeV1.PACK_AREA_UNSAFE,
            operation,
            f"{label} is not one owner-controlled directory",
        )
    return descriptor


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    label: str,
    operation: PackInstallOperationV1,
) -> int:
    if type(name) is not str or not name or name in {".", ".."} or "/" in name or "\\" in name:
        _refuse(
            PackInstallRefusalCodeV1.OBJECT_PATH_UNSAFE,
            operation,
            f"{label} component is invalid",
        )
    try:
        descriptor: int | None = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} is not a directory")
        return descriptor
    except PackInstallRefused:
        raise
    except (OSError, ValueError) as error:
        _close_suppress(descriptor if "descriptor" in locals() else None)
        _refuse(
            PackInstallRefusalCodeV1.PACK_AREA_UNSAFE,
            operation,
            f"{label} is missing, linked, or unsafe",
            cause=error,
        )


def _open_absolute_directory(path: Path, label: str) -> int:
    current = os.open(path.anchor, _directory_flags())
    try:
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


def _open_absolute_directory_optional(path: Path) -> int | None:
    try:
        return _open_absolute_directory(path, "governed data root")
    except FileNotFoundError:
        return None


def _require_safe_mutable_directory(
    metadata: os.stat_result,
    operation: PackInstallOperationV1,
    label: str,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _wrong_owner(metadata)
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _refuse(
            PackInstallRefusalCodeV1.PACK_AREA_UNSAFE,
            operation,
            f"{label} must be an owner-controlled real directory",
        )


def _require_identity(
    metadata: os.stat_result,
    expected: tuple[int, int],
    code: PackInstallRefusalCodeV1,
    operation: PackInstallOperationV1,
    detail: str,
) -> None:
    if (metadata.st_dev, metadata.st_ino) != expected:
        _refuse(code, operation, detail)


def _require_named_identity(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int],
    code: PackInstallRefusalCodeV1,
    operation: PackInstallOperationV1,
    detail: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        _refuse(code, operation, detail, cause=error)
    if (named.st_dev, named.st_ino) != expected:
        _refuse(code, operation, detail)


def _read_exact_descriptor(
    descriptor: int,
    metadata: os.stat_result,
    maximum_bytes: int,
    label: str,
) -> bytes:
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise ValueError(f"{label} byte count is outside its closed bound")
    chunks: list[bytes] = []
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(_IO_CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError(f"{label} ended during its exact read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError(f"{label} grew during its exact read")
    return b"".join(chunks)


def _write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise OSError("short write while persisting canonical registry")
        offset += written


def _entry_exists_at(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _unlink_exact_name_suppress(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int],
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino) == expected and stat.S_ISREG(named.st_mode):
            os.unlink(name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError:
        pass


def _wrong_owner(metadata: os.stat_result) -> bool:
    return hasattr(os, "geteuid") and metadata.st_uid != os.geteuid()


def _nofollow_flag() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("pack installation requires no-follow file opens")
    return os.O_NOFOLLOW


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("pack installation requires no-follow directory handles")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _close_suppress(descriptor: int | None) -> None:
    if descriptor is not None and descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _refuse(
    code: PackInstallRefusalCodeV1,
    operation: PackInstallOperationV1,
    detail: str,
    *,
    cause: BaseException | None = None,
) -> None:
    refusal = PackInstallRefusalV1(code=code, operation=operation, detail=detail)
    if cause is None:
        raise PackInstallRefused(refusal)
    raise PackInstallRefused(refusal) from cause


__all__ = [
    "PACK_DEACTIVATION_RECEIPT_SCHEMA_ID",
    "PACK_INSTALL_RECEIPT_SCHEMA_ID",
    "PACK_MUTATION_RECEIPT_SCHEMA_VERSION",
    "PACK_RECOVERY_DIRECTORY",
    "PACK_REGISTRY_LOCK_FILENAME",
    "PACK_REMOVAL_RECEIPT_SCHEMA_ID",
    "PackDeactivationReceiptV1",
    "PackInstallOperationV1",
    "PackInstallReceiptV1",
    "PackInstallRefusalCodeV1",
    "PackInstallRefusalV1",
    "PackInstallRefused",
    "PackRemovalReceiptV1",
    "deactivate_pack",
    "install_pack",
    "lookup_installed_pack",
    "pack_recovery_relative_path",
    "read_pack_registry",
    "remove_deactivated_pack",
]
