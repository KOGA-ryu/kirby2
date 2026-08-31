"""Backup-first, digest-bound, resumable release metadata migrations."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import ClassVar

from kirby2.packs.formats import (
    canonical_json_bytes,
    compare_semver_precedence,
    load_canonical_json_bytes,
    require_data_identifier,
    require_relative_pack_path,
    require_sha256,
)
from kirby2.packs.registry import PACK_REGISTRY_FILENAME
from kirby2.research.paths import DataAreaId, DataPaths

from .models import (
    ReleaseSchemaInventoryV1,
    ReleaseSchemaKindV1,
    ReleaseSchemaUseV1,
)


RELEASE_MIGRATION_PLAN_SCHEMA_ID_V1 = "KIRBY2_RELEASE_MIGRATION_PLAN_V1"
RELEASE_MIGRATION_BACKUP_SCHEMA_ID_V1 = "KIRBY2_RELEASE_MIGRATION_BACKUP_V1"
RELEASE_MIGRATION_RECEIPT_SCHEMA_ID_V1 = "KIRBY2_RELEASE_MIGRATION_RECEIPT_V1"
RELEASE_MIGRATION_SCHEMA_VERSION_V1 = 1
MAX_MIGRATION_TARGET_BYTES_V1 = 64 * 1024 * 1024
MAX_MIGRATION_TOTAL_BYTES_V1 = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

_PROTECTED_TARGET_AREAS = frozenset(
    {
        DataAreaId.RUNS,
        DataAreaId.EVIDENCE,
        DataAreaId.CHECKPOINTS,
        DataAreaId.DATASETS,
        DataAreaId.BACKUPS,
        DataAreaId.STAGING,
    }
)


class ReleaseMigrationStatusV1(str, Enum):
    COMPLETED = "COMPLETED"


class ReleaseMigrationRefusalCodeV1(str, Enum):
    PLAN_INVALID = "PLAN_INVALID"
    FUTURE_SCHEMA = "FUTURE_SCHEMA"
    UNSAFE_DOWNGRADE = "UNSAFE_DOWNGRADE"
    IMMUTABLE_TARGET = "IMMUTABLE_TARGET"
    TARGET_UNSAFE = "TARGET_UNSAFE"
    SOURCE_DIGEST_MISMATCH = "SOURCE_DIGEST_MISMATCH"
    DESTINATION_DIGEST_MISMATCH = "DESTINATION_DIGEST_MISMATCH"
    BACKUP_FAILED = "BACKUP_FAILED"
    BACKUP_VERIFICATION_FAILED = "BACKUP_VERIFICATION_FAILED"
    STAGE_FAILED = "STAGE_FAILED"
    TARGET_CHANGED = "TARGET_CHANGED"
    APPLY_FAILED = "APPLY_FAILED"
    RECEIPT_CONFLICT = "RECEIPT_CONFLICT"


class ReleaseMigrationRefused(RuntimeError):
    def __init__(self, code: ReleaseMigrationRefusalCodeV1, detail: str) -> None:
        if type(code) is not ReleaseMigrationRefusalCodeV1:
            raise TypeError("release migration refusal code is invalid")
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 2048:
            raise ValueError("release migration refusal detail is invalid")
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class ReleaseMigrationTargetV1:
    """One exact mutable file; directory-wide ambient migration is forbidden."""

    target_id: str
    area_id: DataAreaId
    relative_path: str
    schema_kind: ReleaseSchemaKindV1
    source_schema_id: str
    source_schema_version: int
    source_sha256: str
    destination_schema_id: str
    destination_schema_version: int
    destination_sha256: str
    maximum_bytes: int = MAX_MIGRATION_TARGET_BYTES_V1

    def __post_init__(self) -> None:
        require_data_identifier(self.target_id, "release migration target ID")
        if type(self.area_id) is not DataAreaId:
            raise TypeError("release migration target area is invalid")
        require_relative_pack_path(
            self.relative_path,
            "release migration relative target path",
        )
        if type(self.schema_kind) is not ReleaseSchemaKindV1:
            raise TypeError("release migration target schema kind is invalid")
        require_data_identifier(self.source_schema_id, "migration source schema ID")
        require_data_identifier(
            self.destination_schema_id,
            "migration destination schema ID",
        )
        if (
            type(self.source_schema_version) is not int
            or type(self.destination_schema_version) is not int
            or self.source_schema_version <= 0
            or self.destination_schema_version <= 0
        ):
            raise ValueError("migration schema versions must be positive")
        require_sha256(self.source_sha256, "migration source digest")
        require_sha256(self.destination_sha256, "migration destination digest")
        if (
            type(self.maximum_bytes) is not int
            or self.maximum_bytes <= 0
            or self.maximum_bytes > MAX_MIGRATION_TARGET_BYTES_V1
        ):
            raise ValueError("migration target byte limit is invalid")
        _require_mutable_target(self.area_id, self.relative_path)

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.area_id.value, self.relative_path, self.target_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "area_id": self.area_id.value,
            "destination_schema_id": self.destination_schema_id,
            "destination_schema_version": self.destination_schema_version,
            "destination_sha256": self.destination_sha256,
            "maximum_bytes": self.maximum_bytes,
            "relative_path": self.relative_path,
            "schema_kind": self.schema_kind.value,
            "source_schema_id": self.source_schema_id,
            "source_schema_version": self.source_schema_version,
            "source_sha256": self.source_sha256,
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class ReleaseMigrationPlanV1:
    source_inventory: ReleaseSchemaInventoryV1
    destination_inventory: ReleaseSchemaInventoryV1
    targets: tuple[ReleaseMigrationTargetV1, ...]
    migration_id: str = field(init=False)

    schema_id: ClassVar[str] = RELEASE_MIGRATION_PLAN_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_MIGRATION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.source_inventory) is not ReleaseSchemaInventoryV1:
            raise TypeError("migration source inventory is invalid")
        if type(self.destination_inventory) is not ReleaseSchemaInventoryV1:
            raise TypeError("migration destination inventory is invalid")
        if type(self.targets) is not tuple or not self.targets or any(
            type(item) is not ReleaseMigrationTargetV1 for item in self.targets
        ):
            raise TypeError("migration targets must be a nonempty typed tuple")
        if self.targets != tuple(sorted(self.targets, key=lambda item: item.sort_key)):
            raise ValueError("migration targets must use canonical order")
        if len({item.target_id for item in self.targets}) != len(self.targets):
            raise ValueError("migration target IDs must be unique")
        addresses = {(item.area_id, item.relative_path) for item in self.targets}
        if len(addresses) != len(self.targets):
            raise ValueError("migration target paths must be unique")
        if sum(item.maximum_bytes for item in self.targets) > MAX_MIGRATION_TOTAL_BYTES_V1:
            raise ValueError("migration declared byte limits exceed the total bound")
        _validate_inventory_transition(
            self.source_inventory,
            self.destination_inventory,
        )
        for target in self.targets:
            source_schema = self.source_inventory.schema(target.schema_kind)
            destination_schema = self.destination_inventory.schema(target.schema_kind)
            if (
                target.source_schema_id != source_schema.schema_id
                or target.destination_schema_id != destination_schema.schema_id
                or target.source_schema_version != source_schema.current_version
                or target.destination_schema_version
                != destination_schema.current_version
            ):
                raise ValueError(
                    "migration target schema identity/version differs from its inventory"
                )
            self.destination_inventory.require_supported(
                target.schema_kind,
                ReleaseSchemaUseV1.READ,
                target.source_schema_version,
            )
            self.destination_inventory.require_supported(
                target.schema_kind,
                ReleaseSchemaUseV1.WRITE,
                target.destination_schema_version,
            )
            if target.destination_schema_version < target.source_schema_version:
                raise ValueError("migration target requests an unsafe schema downgrade")
        identity = self.identity_dict()
        object.__setattr__(
            self,
            "migration_id",
            "migration-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "destination_inventory_sha256": self.destination_inventory.sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_inventory_sha256": self.source_inventory.sha256,
            "targets": [item.as_dict() for item in self.targets],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "migration_id": self.migration_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseMigrationBackupEntryV1:
    target_id: str
    area_id: DataAreaId
    relative_path: str
    backup_path: str
    byte_count: int
    source_schema_id: str
    source_schema_version: int
    source_sha256: str
    destination_schema_id: str
    destination_schema_version: int
    destination_sha256: str

    def __post_init__(self) -> None:
        require_data_identifier(self.target_id, "migration backup target ID")
        if type(self.area_id) is not DataAreaId:
            raise TypeError("migration backup area is invalid")
        require_relative_pack_path(self.relative_path, "migration backup source path")
        require_relative_pack_path(self.backup_path, "migration backup payload path")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ValueError("migration backup byte count must be positive")
        require_data_identifier(self.source_schema_id, "backup source schema ID")
        require_data_identifier(
            self.destination_schema_id,
            "backup destination schema ID",
        )
        if (
            type(self.source_schema_version) is not int
            or type(self.destination_schema_version) is not int
            or self.source_schema_version <= 0
            or self.destination_schema_version <= 0
        ):
            raise ValueError("migration backup schema versions must be positive")
        require_sha256(self.source_sha256, "migration backup source digest")
        require_sha256(self.destination_sha256, "migration backup destination digest")

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.area_id.value, self.relative_path, self.target_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "area_id": self.area_id.value,
            "backup_path": self.backup_path,
            "byte_count": self.byte_count,
            "destination_schema_id": self.destination_schema_id,
            "destination_schema_version": self.destination_schema_version,
            "destination_sha256": self.destination_sha256,
            "relative_path": self.relative_path,
            "source_schema_id": self.source_schema_id,
            "source_schema_version": self.source_schema_version,
            "source_sha256": self.source_sha256,
            "target_id": self.target_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> ReleaseMigrationBackupEntryV1:
        expected = {
            "area_id",
            "backup_path",
            "byte_count",
            "destination_schema_id",
            "destination_schema_version",
            "destination_sha256",
            "relative_path",
            "source_schema_id",
            "source_schema_version",
            "source_sha256",
            "target_id",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("migration backup entry fields differ")
        try:
            return cls(
                target_id=_text(value, "target_id"),
                area_id=DataAreaId(_text(value, "area_id")),
                relative_path=_text(value, "relative_path"),
                backup_path=_text(value, "backup_path"),
                byte_count=_int(value, "byte_count"),
                source_schema_id=_text(value, "source_schema_id"),
                source_schema_version=_int(value, "source_schema_version"),
                source_sha256=_text(value, "source_sha256"),
                destination_schema_id=_text(value, "destination_schema_id"),
                destination_schema_version=_int(value, "destination_schema_version"),
                destination_sha256=_text(value, "destination_sha256"),
            )
        except ValueError as error:
            raise ValueError("migration backup entry enum differs") from error


@dataclass(frozen=True, slots=True)
class ReleaseMigrationBackupManifestV1:
    migration_id: str
    plan_sha256: str
    source_inventory_sha256: str
    destination_inventory_sha256: str
    entries: tuple[ReleaseMigrationBackupEntryV1, ...]

    schema_id: ClassVar[str] = RELEASE_MIGRATION_BACKUP_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_MIGRATION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _migration_id(self.migration_id)
        require_sha256(self.plan_sha256, "migration backup plan digest")
        require_sha256(
            self.source_inventory_sha256,
            "migration backup source-inventory digest",
        )
        require_sha256(
            self.destination_inventory_sha256,
            "migration backup destination-inventory digest",
        )
        if type(self.entries) is not tuple or not self.entries or any(
            type(item) is not ReleaseMigrationBackupEntryV1 for item in self.entries
        ):
            raise TypeError("migration backup entries must be a nonempty typed tuple")
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.sort_key)):
            raise ValueError("migration backup entries must use canonical order")
        if len({item.target_id for item in self.entries}) != len(self.entries):
            raise ValueError("migration backup target IDs must be unique")
        if len({item.backup_path for item in self.entries}) != len(self.entries):
            raise ValueError("migration backup payload paths must be unique")

    def as_dict(self) -> dict[str, object]:
        return {
            "destination_inventory_sha256": self.destination_inventory_sha256,
            "entries": [item.as_dict() for item in self.entries],
            "migration_id": self.migration_id,
            "plan_sha256": self.plan_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_inventory_sha256": self.source_inventory_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ReleaseMigrationBackupManifestV1:
        value = load_canonical_json_bytes(raw, "release migration backup manifest")
        expected = {
            "destination_inventory_sha256",
            "entries",
            "migration_id",
            "plan_sha256",
            "schema_id",
            "schema_version",
            "source_inventory_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("migration backup manifest fields differ")
        if value["schema_id"] != cls.schema_id or value["schema_version"] != 1:
            raise ValueError("migration backup manifest contract differs")
        entries = value["entries"]
        if type(entries) is not list:
            raise TypeError("migration backup entries must be an array")
        restored = cls(
            migration_id=_text(value, "migration_id"),
            plan_sha256=_text(value, "plan_sha256"),
            source_inventory_sha256=_text(value, "source_inventory_sha256"),
            destination_inventory_sha256=_text(
                value,
                "destination_inventory_sha256",
            ),
            entries=tuple(
                sorted(
                    (ReleaseMigrationBackupEntryV1.from_dict(item) for item in entries),
                    key=lambda item: item.sort_key,
                )
            ),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("migration backup manifest changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class ReleaseMigrationReceiptV1:
    migration_id: str
    plan_sha256: str
    backup_manifest_sha256: str
    target_sha256s: tuple[tuple[str, str], ...]
    status: ReleaseMigrationStatusV1 = ReleaseMigrationStatusV1.COMPLETED

    schema_id: ClassVar[str] = RELEASE_MIGRATION_RECEIPT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_MIGRATION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _migration_id(self.migration_id)
        require_sha256(self.plan_sha256, "migration receipt plan digest")
        require_sha256(
            self.backup_manifest_sha256,
            "migration receipt backup digest",
        )
        if type(self.target_sha256s) is not tuple or not self.target_sha256s:
            raise ValueError("migration receipt targets must be nonempty")
        for target_id, digest in self.target_sha256s:
            require_data_identifier(target_id, "migration receipt target ID")
            require_sha256(digest, "migration receipt target digest")
        if self.target_sha256s != tuple(sorted(set(self.target_sha256s))):
            raise ValueError("migration receipt targets must be canonical and unique")
        if self.status is not ReleaseMigrationStatusV1.COMPLETED:
            raise ValueError("persisted migration receipt must be completed")

    def as_dict(self) -> dict[str, object]:
        return {
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "migration_id": self.migration_id,
            "plan_sha256": self.plan_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "targets": [
                {"sha256": digest, "target_id": target_id}
                for target_id, digest in self.target_sha256s
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def apply_release_migration(
    plan: ReleaseMigrationPlanV1,
    destination_bytes: Mapping[str, bytes],
    *,
    paths: DataPaths,
) -> ReleaseMigrationReceiptV1:
    """Back up, verify, stage, and idempotently replace declared mutable files."""

    if type(plan) is not ReleaseMigrationPlanV1:
        raise TypeError("release migration requires ReleaseMigrationPlanV1")
    if type(paths) is not DataPaths:
        raise TypeError("release migration requires the exact DataPaths provider")
    if not isinstance(destination_bytes, Mapping) or any(
        type(key) is not str or type(value) is not bytes
        for key, value in destination_bytes.items()
    ):
        raise TypeError("migration destination payloads must map text IDs to bytes")
    expected_ids = tuple(item.target_id for item in plan.targets)
    if tuple(sorted(destination_bytes)) != tuple(sorted(expected_ids)):
        _refuse(
            ReleaseMigrationRefusalCodeV1.PLAN_INVALID,
            "migration destination payload IDs differ from the exact plan",
        )
    paths.ensure((DataAreaId.BACKUPS, DataAreaId.STAGING, DataAreaId.RELEASE))
    selected_areas = tuple(
        sorted({item.area_id for item in plan.targets}, key=lambda item: item.value)
    )
    paths.validate(
        (*selected_areas, DataAreaId.BACKUPS, DataAreaId.STAGING, DataAreaId.RELEASE)
    )

    destinations: dict[str, bytes] = {}
    destination_total = 0
    for target in plan.targets:
        destination = destination_bytes[target.target_id]
        if (
            not destination
            or len(destination) > target.maximum_bytes
            or not hmac.compare_digest(
                hashlib.sha256(destination).hexdigest(),
                target.destination_sha256,
            )
        ):
            _refuse(
                ReleaseMigrationRefusalCodeV1.DESTINATION_DIGEST_MISMATCH,
                f"migration destination differs from plan: {target.target_id}",
            )
        destination_total += len(destination)
        if destination_total > MAX_MIGRATION_TOTAL_BYTES_V1:
            _refuse(
                ReleaseMigrationRefusalCodeV1.PLAN_INVALID,
                "migration destination bytes exceed the total bound",
            )
        destinations[target.target_id] = destination

    backup_root = paths.backups / "migrations" / plan.migration_id
    if backup_root.is_symlink() or backup_root.exists():
        backup, sources = _load_verified_backup(plan, backup_root)
    else:
        sources = _capture_source_targets(plan, paths=paths)
        backup = _ensure_verified_backup(plan, sources, paths=paths)
    if (
        destination_total + sum(len(raw) for raw in sources.values())
        > MAX_MIGRATION_TOTAL_BYTES_V1
    ):
        _refuse(
            ReleaseMigrationRefusalCodeV1.PLAN_INVALID,
            "migration source and destination bytes exceed the total bound",
        )

    stage = _ensure_verified_stage(plan, destinations, paths=paths)
    receipt_path = _receipt_path(paths, plan.migration_id)
    if receipt_path.exists():
        receipt = _read_receipt(receipt_path)
        _require_receipt_matches(receipt, plan, backup)
        _verify_destination_targets(plan, destinations, paths=paths)
        return receipt

    # The complete backup is read back again immediately before the first mutation.
    _verify_backup_directory(backup[0], backup[1], sources)
    for target in plan.targets:
        target_path = _target_path(paths, target)
        current = _read_stable_file(target_path, maximum_bytes=target.maximum_bytes)
        current_sha256 = hashlib.sha256(current).hexdigest()
        if hmac.compare_digest(current_sha256, target.destination_sha256):
            continue
        if not hmac.compare_digest(current_sha256, target.source_sha256):
            _refuse(
                ReleaseMigrationRefusalCodeV1.TARGET_CHANGED,
                f"migration target changed after backup: {target.target_id}",
            )
        staged = _read_stable_file(
            stage / _payload_name(target.target_id),
            maximum_bytes=target.maximum_bytes,
        )
        _replace_mutable_file(target_path, staged)

    _verify_destination_targets(plan, destinations, paths=paths)
    receipt = ReleaseMigrationReceiptV1(
        migration_id=plan.migration_id,
        plan_sha256=plan.sha256,
        backup_manifest_sha256=backup[1].sha256,
        target_sha256s=tuple(
            sorted((item.target_id, item.destination_sha256) for item in plan.targets)
        ),
    )
    _write_atomic_new_file(receipt_path, receipt.canonical_bytes())
    restored = _read_receipt(receipt_path)
    _require_receipt_matches(restored, plan, backup)
    return restored


def _ensure_verified_backup(
    plan: ReleaseMigrationPlanV1,
    sources: Mapping[str, bytes],
    *,
    paths: DataPaths,
) -> tuple[Path, ReleaseMigrationBackupManifestV1]:
    parent = paths.backups / "migrations"
    _ensure_real_directory(parent)
    target = parent / plan.migration_id
    entries = tuple(
        ReleaseMigrationBackupEntryV1(
            target_id=item.target_id,
            area_id=item.area_id,
            relative_path=item.relative_path,
            backup_path=_payload_name(item.target_id),
            byte_count=len(sources[item.target_id]),
            source_schema_id=item.source_schema_id,
            source_schema_version=item.source_schema_version,
            source_sha256=item.source_sha256,
            destination_schema_id=item.destination_schema_id,
            destination_schema_version=item.destination_schema_version,
            destination_sha256=item.destination_sha256,
        )
        for item in plan.targets
    )
    manifest = ReleaseMigrationBackupManifestV1(
        migration_id=plan.migration_id,
        plan_sha256=plan.sha256,
        source_inventory_sha256=plan.source_inventory.sha256,
        destination_inventory_sha256=plan.destination_inventory.sha256,
        entries=entries,
    )
    if not target.exists():
        try:
            with tempfile.TemporaryDirectory(
                dir=paths.backups,
                prefix=f".{plan.migration_id}-",
            ) as temporary_name:
                temporary = Path(temporary_name) / "backup"
                temporary.mkdir()
                for entry in entries:
                    _write_new_file(
                        temporary / entry.backup_path,
                        sources[entry.target_id],
                    )
                _write_new_file(temporary / "manifest.json", manifest.canonical_bytes())
                os.rename(temporary, target)
                _fsync_directory(parent)
        except (OSError, RuntimeError, ValueError) as error:
            _refuse(
                ReleaseMigrationRefusalCodeV1.BACKUP_FAILED,
                "pre-migration backup could not be published",
                cause=error,
            )
    restored, restored_sources = _load_verified_backup(plan, target)
    if restored[1] != manifest or restored_sources != dict(sources):
        _refuse(
            ReleaseMigrationRefusalCodeV1.BACKUP_VERIFICATION_FAILED,
            "pre-migration backup conflicts with the exact plan",
        )
    return restored


def _capture_source_targets(
    plan: ReleaseMigrationPlanV1,
    *,
    paths: DataPaths,
) -> dict[str, bytes]:
    sources: dict[str, bytes] = {}
    total = 0
    for target in plan.targets:
        source_path = _target_path(paths, target)
        raw = _read_stable_file(source_path, maximum_bytes=target.maximum_bytes)
        if not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            target.source_sha256,
        ):
            _refuse(
                ReleaseMigrationRefusalCodeV1.SOURCE_DIGEST_MISMATCH,
                f"migration source differs before backup: {target.target_id}",
            )
        total += len(raw)
        if total > MAX_MIGRATION_TOTAL_BYTES_V1:
            _refuse(
                ReleaseMigrationRefusalCodeV1.PLAN_INVALID,
                "migration source bytes exceed the total bound",
            )
        sources[target.target_id] = raw
    return sources


def _load_verified_backup(
    plan: ReleaseMigrationPlanV1,
    root: Path,
) -> tuple[
    tuple[Path, ReleaseMigrationBackupManifestV1],
    dict[str, bytes],
]:
    try:
        restored_raw = _read_stable_file(
            root / "manifest.json",
            maximum_bytes=MAX_MIGRATION_TARGET_BYTES_V1,
        )
        manifest = ReleaseMigrationBackupManifestV1.from_canonical_bytes(restored_raw)
        if (
            manifest.migration_id != plan.migration_id
            or manifest.plan_sha256 != plan.sha256
            or manifest.source_inventory_sha256 != plan.source_inventory.sha256
            or manifest.destination_inventory_sha256
            != plan.destination_inventory.sha256
            or len(manifest.entries) != len(plan.targets)
        ):
            raise ValueError("backup manifest identity differs from the exact plan")

        entries = {entry.target_id: entry for entry in manifest.entries}
        sources: dict[str, bytes] = {}
        total = 0
        for target in plan.targets:
            entry = entries.get(target.target_id)
            if entry is None or (
                entry.area_id is not target.area_id
                or entry.relative_path != target.relative_path
                or entry.backup_path != _payload_name(target.target_id)
                or entry.source_schema_id != target.source_schema_id
                or entry.source_schema_version != target.source_schema_version
                or entry.source_sha256 != target.source_sha256
                or entry.destination_schema_id != target.destination_schema_id
                or entry.destination_schema_version
                != target.destination_schema_version
                or entry.destination_sha256 != target.destination_sha256
                or entry.byte_count > target.maximum_bytes
            ):
                raise ValueError("backup entry differs from the exact plan")
            raw = _read_stable_file(
                root / entry.backup_path,
                maximum_bytes=target.maximum_bytes,
            )
            if len(raw) != entry.byte_count or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(),
                target.source_sha256,
            ):
                raise ValueError("backup payload differs from its source digest")
            total += len(raw)
            if total > MAX_MIGRATION_TOTAL_BYTES_V1:
                raise ValueError("backup payloads exceed the total byte bound")
            sources[target.target_id] = raw
    except (ReleaseMigrationRefused, OSError, RuntimeError, TypeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.BACKUP_VERIFICATION_FAILED,
            "pre-migration backup is invalid or incomplete",
            cause=error,
        )
    _verify_backup_directory(root, manifest, sources)
    return (root, manifest), sources


def _verify_backup_directory(
    root: Path,
    manifest: ReleaseMigrationBackupManifestV1,
    sources: Mapping[str, bytes],
) -> None:
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("backup root is not one real directory")
        expected_names = {"manifest.json"} | {
            item.backup_path for item in manifest.entries
        }
        actual_names = {item.name for item in root.iterdir()}
        if actual_names != expected_names:
            raise ValueError("backup directory contains missing or ambient files")
        if _read_stable_file(
            root / "manifest.json",
            maximum_bytes=MAX_MIGRATION_TARGET_BYTES_V1,
        ) != manifest.canonical_bytes():
            raise ValueError("backup manifest bytes changed after verification")
        for entry in manifest.entries:
            raw = _read_stable_file(
                root / entry.backup_path,
                maximum_bytes=MAX_MIGRATION_TARGET_BYTES_V1,
            )
            if (
                raw != sources[entry.target_id]
                or len(raw) != entry.byte_count
                or not hmac.compare_digest(
                    hashlib.sha256(raw).hexdigest(),
                    entry.source_sha256,
                )
            ):
                raise ValueError("backup payload differs from exact source bytes")
    except (ReleaseMigrationRefused, OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.BACKUP_VERIFICATION_FAILED,
            "pre-migration backup failed complete read-back verification",
            cause=error,
        )


def _ensure_verified_stage(
    plan: ReleaseMigrationPlanV1,
    destinations: Mapping[str, bytes],
    *,
    paths: DataPaths,
) -> Path:
    parent = paths.staging / "migrations"
    _ensure_real_directory(parent)
    target = parent / plan.migration_id
    try:
        if not target.exists():
            with tempfile.TemporaryDirectory(
                dir=paths.staging,
                prefix=f".{plan.migration_id}-",
            ) as temporary_name:
                temporary = Path(temporary_name) / "stage"
                temporary.mkdir()
                _write_new_file(temporary / "plan.json", plan.canonical_bytes())
                for item in plan.targets:
                    _write_new_file(
                        temporary / _payload_name(item.target_id),
                        destinations[item.target_id],
                    )
                os.rename(temporary, target)
                _fsync_directory(parent)
        expected_names = {"plan.json"} | {
            _payload_name(item.target_id) for item in plan.targets
        }
        if target.is_symlink() or not target.is_dir():
            raise ValueError("migration stage is not one real directory")
        if {item.name for item in target.iterdir()} != expected_names:
            raise ValueError("migration stage contains missing or ambient files")
        if _read_stable_file(
            target / "plan.json",
            maximum_bytes=MAX_MIGRATION_TARGET_BYTES_V1,
        ) != plan.canonical_bytes():
            raise ValueError("migration staged plan differs")
        for item in plan.targets:
            raw = _read_stable_file(
                target / _payload_name(item.target_id),
                maximum_bytes=item.maximum_bytes,
            )
            if raw != destinations[item.target_id]:
                raise ValueError("migration staged payload differs")
        return target
    except (ReleaseMigrationRefused, OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.STAGE_FAILED,
            "migration destination stage could not be verified",
            cause=error,
        )


def _verify_destination_targets(
    plan: ReleaseMigrationPlanV1,
    destinations: Mapping[str, bytes],
    *,
    paths: DataPaths,
) -> None:
    for item in plan.targets:
        raw = _read_stable_file(
            _target_path(paths, item),
            maximum_bytes=item.maximum_bytes,
        )
        if raw != destinations[item.target_id] or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(),
            item.destination_sha256,
        ):
            _refuse(
                ReleaseMigrationRefusalCodeV1.APPLY_FAILED,
                f"migrated target failed final verification: {item.target_id}",
            )


def _target_path(paths: DataPaths, target: ReleaseMigrationTargetV1) -> Path:
    paths.validate((target.area_id,))
    root = paths.area(target.area_id)
    candidate = root.joinpath(*PurePosixPath(target.relative_path).parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.TARGET_UNSAFE,
            f"migration target escapes its governed area: {target.target_id}",
            cause=error,
        )
    _require_real_parent_chain(root, candidate.parent)
    return candidate


def _read_stable_file(path: Path, *, maximum_bytes: int) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        _refuse(
            ReleaseMigrationRefusalCodeV1.TARGET_UNSAFE,
            "migration capture requires no-follow file opens",
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ValueError("migration file is empty, linked, special, or oversized")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError("migration file exceeds its byte bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != after.st_size
        ):
            raise ValueError("migration file changed during capture")
        return b"".join(chunks)
    except ReleaseMigrationRefused:
        raise
    except (OSError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.TARGET_UNSAFE,
            f"migration file cannot be captured safely: {path.name}",
            cause=error,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_mutable_file(target: Path, raw: bytes) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("migration replacement target is not one regular file")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.migration-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = target.lstat()
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("migration target changed before atomic replacement")
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    except (OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.APPLY_FAILED,
            f"migration target could not be replaced atomically: {target.name}",
            cause=error,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _write_new_file(path: Path, raw: bytes, *, create_parents: bool = False) -> None:
    if type(raw) is not bytes or not raw:
        raise ValueError("migration file payload must be nonempty exact bytes")
    if create_parents:
        _ensure_real_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("migration file write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _write_atomic_new_file(path: Path, raw: bytes) -> None:
    """Durably publish one new receipt without exposing a partial final file."""

    _ensure_real_directory(path.parent)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("migration receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
        _fsync_directory(path.parent)
    except FileExistsError as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt appeared concurrently",
            cause=error,
        )
    except (OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.APPLY_FAILED,
            "migration completion receipt could not be published atomically",
            cause=error,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_receipt(path: Path) -> ReleaseMigrationReceiptV1:
    raw = _read_stable_file(path, maximum_bytes=MAX_MIGRATION_TARGET_BYTES_V1)
    try:
        value = load_canonical_json_bytes(raw, "release migration receipt")
    except (TypeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt is not canonical JSON",
            cause=error,
        )
    expected = {
        "backup_manifest_sha256",
        "migration_id",
        "plan_sha256",
        "schema_id",
        "schema_version",
        "status",
        "targets",
    }
    if type(value) is not dict or set(value) != expected:
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt fields differ",
        )
    if (
        value["schema_id"] != RELEASE_MIGRATION_RECEIPT_SCHEMA_ID_V1
        or value["schema_version"] != 1
        or value["status"] != ReleaseMigrationStatusV1.COMPLETED.value
    ):
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt contract differs",
        )
    rows = value["targets"]
    if type(rows) is not list:
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt targets are invalid",
        )
    try:
        receipt = ReleaseMigrationReceiptV1(
            migration_id=_text(value, "migration_id"),
            plan_sha256=_text(value, "plan_sha256"),
            backup_manifest_sha256=_text(value, "backup_manifest_sha256"),
            target_sha256s=tuple(
                sorted(
                    (_text(row, "target_id"), _text(row, "sha256"))
                    for row in rows
                    if type(row) is dict and set(row) == {"sha256", "target_id"}
                )
            ),
        )
    except (TypeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt failed exact reconstruction",
            cause=error,
        )
    if receipt.canonical_bytes() != raw or len(receipt.target_sha256s) != len(rows):
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "migration receipt changed during reconstruction",
        )
    return receipt


def _require_receipt_matches(
    receipt: ReleaseMigrationReceiptV1,
    plan: ReleaseMigrationPlanV1,
    backup: tuple[Path, ReleaseMigrationBackupManifestV1],
) -> None:
    expected = tuple(
        sorted((item.target_id, item.destination_sha256) for item in plan.targets)
    )
    if (
        receipt.migration_id != plan.migration_id
        or receipt.plan_sha256 != plan.sha256
        or receipt.backup_manifest_sha256 != backup[1].sha256
        or receipt.target_sha256s != expected
    ):
        _refuse(
            ReleaseMigrationRefusalCodeV1.RECEIPT_CONFLICT,
            "existing migration receipt conflicts with the requested plan",
        )


def _validate_inventory_transition(
    source: ReleaseSchemaInventoryV1,
    destination: ReleaseSchemaInventoryV1,
) -> None:
    if compare_semver_precedence(
        destination.engine_version,
        source.engine_version,
    ) < 0 or destination.data_paths_schema_version < source.data_paths_schema_version:
        _refuse(
            ReleaseMigrationRefusalCodeV1.UNSAFE_DOWNGRADE,
            "destination release is older than the source; restore the backup or use a newer release",
        )
    for source_schema in source.schemas:
        destination_schema = destination.schema(source_schema.kind)
        if destination_schema.schema_id != source_schema.schema_id:
            _refuse(
                ReleaseMigrationRefusalCodeV1.FUTURE_SCHEMA,
                f"schema identity changed without a declared compatibility path: {source_schema.kind.value}",
            )
        if not destination_schema.supports(
            ReleaseSchemaUseV1.READ,
            source_schema.current_version,
        ):
            _refuse(
                ReleaseMigrationRefusalCodeV1.FUTURE_SCHEMA,
                f"source schema is not readable by this release: {source_schema.schema_id}",
            )


def _require_mutable_target(area_id: DataAreaId, relative_path: str) -> None:
    if area_id in _PROTECTED_TARGET_AREAS:
        _refuse(
            ReleaseMigrationRefusalCodeV1.IMMUTABLE_TARGET,
            f"migration cannot rewrite governed immutable area: {area_id.value}",
        )
    if area_id is DataAreaId.PACKS and relative_path != PACK_REGISTRY_FILENAME:
        _refuse(
            ReleaseMigrationRefusalCodeV1.IMMUTABLE_TARGET,
            "migration may update pack registry metadata but never installed pack objects",
        )
    if area_id is DataAreaId.RELEASE and PurePosixPath(relative_path).parts[0] == "migrations":
        _refuse(
            ReleaseMigrationRefusalCodeV1.IMMUTABLE_TARGET,
            "migration target cannot overlap migration receipts",
        )


def _require_real_parent_chain(root: Path, parent: Path) -> None:
    try:
        root_resolved = root.resolve(strict=True)
        relative = parent.resolve(strict=True).relative_to(root_resolved)
        current = root_resolved
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("migration parent chain contains a link or file")
    except (OSError, RuntimeError, ValueError) as error:
        _refuse(
            ReleaseMigrationRefusalCodeV1.TARGET_UNSAFE,
            "migration target parent chain is missing, linked, or escaped",
            cause=error,
        )


def _ensure_real_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"migration directory is linked or invalid: {path.name}")


def _receipt_path(paths: DataPaths, migration_id: str) -> Path:
    return paths.release / "migrations" / f"{_migration_id(migration_id)}.json"


def _payload_name(target_id: str) -> str:
    require_data_identifier(target_id, "migration payload target ID")
    digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()
    return f"target-{digest}.bin"


def _migration_id(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 34
        or not value.startswith("migration-")
        or any(character not in "0123456789abcdef" for character in value[10:])
    ):
        raise ValueError("release migration ID is invalid")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _text(value: dict[str, object], key: str) -> str:
    item = value[key]
    if type(item) is not str:
        raise TypeError(f"{key} must be text")
    return item


def _int(value: dict[str, object], key: str) -> int:
    item = value[key]
    if type(item) is not int:
        raise TypeError(f"{key} must be an integer")
    return item


def _refuse(
    code: ReleaseMigrationRefusalCodeV1,
    detail: str,
    *,
    cause: BaseException | None = None,
) -> None:
    refusal = ReleaseMigrationRefused(code, detail)
    if cause is None:
        raise refusal
    raise refusal from cause


__all__ = [
    "MAX_MIGRATION_TARGET_BYTES_V1",
    "MAX_MIGRATION_TOTAL_BYTES_V1",
    "RELEASE_MIGRATION_BACKUP_SCHEMA_ID_V1",
    "RELEASE_MIGRATION_PLAN_SCHEMA_ID_V1",
    "RELEASE_MIGRATION_RECEIPT_SCHEMA_ID_V1",
    "RELEASE_MIGRATION_SCHEMA_VERSION_V1",
    "ReleaseMigrationBackupEntryV1",
    "ReleaseMigrationBackupManifestV1",
    "ReleaseMigrationPlanV1",
    "ReleaseMigrationReceiptV1",
    "ReleaseMigrationRefusalCodeV1",
    "ReleaseMigrationRefused",
    "ReleaseMigrationStatusV1",
    "ReleaseMigrationTargetV1",
    "apply_release_migration",
]
