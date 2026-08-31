"""Verified, non-destructive restoration into a separate Kirby2 data root."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import ClassVar

from kirby2.packs.archive import preflight_pack_archive_bytes, read_pack_archive_bytes
from kirby2.packs.formats import (
    K2PACK_MANIFEST_PATH,
    canonical_json_bytes,
    canonical_manifest_bytes,
    load_canonical_json_bytes,
)
from kirby2.packs.install import read_pack_registry
from kirby2.packs.registry import PackRegistryV1
from kirby2.research.models import RunManifest
from kirby2.research.paths import DataAreaId, DataPaths

from .backup import (
    BackupDispositionV1,
    BackupEncryptionStatusV1,
    BackupEntryV1,
    BackupFamilyV1,
    BackupManifestV1,
    BackupRefused,
    data_root_identity,
    verify_backup,
)


RESTORE_RECEIPT_SCHEMA_ID_V1 = "KIRBY2_USER_DATA_RESTORE_RECEIPT_V1"
RESTORE_RECEIPT_SCHEMA_VERSION_V1 = 1
MAX_RESTORE_RECEIPT_BYTES_V1 = 64 * 1024 * 1024
MAX_RESTORE_STRUCTURED_DOCUMENT_BYTES_V1 = 64 * 1024 * 1024
MAX_RESTORE_JSONL_ROW_BYTES_V1 = 16 * 1024 * 1024
MAX_RESTORE_ITEM_BYTES_V1 = (1 << 63) - 1


class RestoreConflictPolicyV1(str, Enum):
    FAIL = "FAIL"
    ACCEPT_IDENTICAL_ONLY = "ACCEPT_IDENTICAL_ONLY"


class RestoreStatusV1(str, Enum):
    RESTORED = "RESTORED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


class RestoreRefusalCodeV1(str, Enum):
    BACKUP_INVALID = "BACKUP_INVALID"
    CONSENT_MISMATCH = "CONSENT_MISMATCH"
    ENCRYPTION_UNSUPPORTED = "ENCRYPTION_UNSUPPORTED"
    REFERENCE_SOURCE_REQUIRED = "REFERENCE_SOURCE_REQUIRED"
    REFERENCE_SOURCE_MISMATCH = "REFERENCE_SOURCE_MISMATCH"
    DESTINATION_CONFLICT = "DESTINATION_CONFLICT"
    DESTINATION_INVALID = "DESTINATION_INVALID"
    SCHEMA_OR_PARSER_INVALID = "SCHEMA_OR_PARSER_INVALID"
    PACK_OR_DEPENDENCY_INVALID = "PACK_OR_DEPENDENCY_INVALID"
    STAGE_INVALID = "STAGE_INVALID"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"


class RestoreRefused(RuntimeError):
    def __init__(
        self,
        code: RestoreRefusalCodeV1,
        detail: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.cause = cause
        suffix = "" if cause is None else f" ({type(cause).__name__}: {cause})"
        super().__init__(f"{code.value}: {detail}{suffix}")


@dataclass(frozen=True, slots=True)
class RestoreTargetV1:
    area_id: DataAreaId
    relative_path: str
    byte_count: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.area_id) is not DataAreaId:
            raise TypeError("restore target area is invalid")
        _relative_path(self.relative_path)
        if (
            type(self.byte_count) is not int
            or not 0 <= self.byte_count <= MAX_RESTORE_ITEM_BYTES_V1
        ):
            raise ValueError("restore target byte count is invalid")
        _sha256(self.sha256, "restore target")

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.area_id.value, self.relative_path

    def as_dict(self) -> dict[str, object]:
        return {
            "area_id": self.area_id.value,
            "byte_count": self.byte_count,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> RestoreTargetV1:
        expected = {"area_id", "byte_count", "relative_path", "sha256"}
        if type(value) is not dict or set(value) != expected:
            raise ValueError("restore target fields differ")
        return cls(
            area_id=DataAreaId(_text(value, "area_id")),
            relative_path=_text(value, "relative_path"),
            byte_count=_integer(value, "byte_count"),
            sha256=_text(value, "sha256"),
        )


@dataclass(frozen=True, slots=True)
class RestoreReceiptV1:
    restore_id: str
    status: RestoreStatusV1
    backup_id: str
    backup_manifest_sha256: str
    source_data_root_id: str
    destination_data_root_id: str
    conflict_policy: RestoreConflictPolicyV1
    accepted_consent_id: str
    targets: tuple[RestoreTargetV1, ...]

    schema_id: ClassVar[str] = RESTORE_RECEIPT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RESTORE_RECEIPT_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.status) is not RestoreStatusV1:
            raise TypeError("restore status is invalid")
        if (
            not self.backup_id.startswith("backup-")
            or len(self.backup_id) != len("backup-") + 24
            or any(
                character not in "0123456789abcdef"
                for character in self.backup_id[len("backup-") :]
            )
        ):
            raise ValueError("restore receipt backup ID is invalid")
        _sha256(self.backup_manifest_sha256, "restore backup manifest")
        _sha256(self.source_data_root_id, "restore source data-root")
        _sha256(self.destination_data_root_id, "restore destination data-root")
        if type(self.conflict_policy) is not RestoreConflictPolicyV1:
            raise TypeError("restore conflict policy is invalid")
        if (
            self.status is RestoreStatusV1.ALREADY_PRESENT
            and self.conflict_policy
            is not RestoreConflictPolicyV1.ACCEPT_IDENTICAL_ONLY
        ):
            raise ValueError(
                "already-present restore status requires the identical-only policy"
            )
        _token(self.accepted_consent_id, "restore accepted consent ID")
        if type(self.targets) is not tuple or any(
            type(item) is not RestoreTargetV1 for item in self.targets
        ):
            raise TypeError("restore targets must be a typed tuple")
        if self.targets != tuple(sorted(self.targets, key=lambda item: item.sort_key)):
            raise ValueError("restore targets must use canonical path order")
        addresses = {(item.area_id, item.relative_path) for item in self.targets}
        if len(addresses) != len(self.targets):
            raise ValueError("restore targets contain duplicate paths")
        expected = "restore-" + hashlib.sha256(
            canonical_json_bytes(self.identity_dict())
        ).hexdigest()[:24]
        if not self.restore_id:
            object.__setattr__(self, "restore_id", expected)
        elif self.restore_id != expected:
            raise ValueError("restore receipt identity differs")

    def identity_dict(self) -> dict[str, object]:
        return {
            "accepted_consent_id": self.accepted_consent_id,
            "backup_id": self.backup_id,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "conflict_policy": self.conflict_policy.value,
            "destination_data_root_id": self.destination_data_root_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_data_root_id": self.source_data_root_id,
            "status": self.status.value,
            "targets": [item.as_dict() for item in self.targets],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "restore_id": self.restore_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RestoreReceiptV1:
        value = load_canonical_json_bytes(raw, "Kirby2 restore receipt")
        expected = {
            "accepted_consent_id",
            "backup_id",
            "backup_manifest_sha256",
            "conflict_policy",
            "destination_data_root_id",
            "restore_id",
            "schema_id",
            "schema_version",
            "source_data_root_id",
            "status",
            "targets",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("restore receipt fields differ")
        if (
            value["schema_id"] != cls.schema_id
            or value["schema_version"] != cls.schema_version
            or type(value["targets"]) is not list
        ):
            raise ValueError("restore receipt contract differs")
        restored = cls(
            restore_id=_text(value, "restore_id"),
            status=RestoreStatusV1(_text(value, "status")),
            backup_id=_text(value, "backup_id"),
            backup_manifest_sha256=_text(value, "backup_manifest_sha256"),
            source_data_root_id=_text(value, "source_data_root_id"),
            destination_data_root_id=_text(value, "destination_data_root_id"),
            conflict_policy=RestoreConflictPolicyV1(
                _text(value, "conflict_policy")
            ),
            accepted_consent_id=_text(value, "accepted_consent_id"),
            targets=tuple(
                RestoreTargetV1.from_dict(item) for item in value["targets"]
            ),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("restore receipt changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class RestoreResultV1:
    destination_root: Path
    receipt: RestoreReceiptV1

    def as_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.receipt.backup_id,
            "destination_root": str(self.destination_root),
            "restore_id": self.receipt.restore_id,
            "status": self.receipt.status.value,
            "target_count": len(self.receipt.targets),
        }


def restore_backup(
    *,
    backup_root: Path,
    destination_paths: DataPaths,
    reference_paths: DataPaths | None = None,
    conflict_policy: RestoreConflictPolicyV1 = RestoreConflictPolicyV1.FAIL,
    accepted_consent_id: str = "LOCAL_USER_REQUEST_V1",
) -> RestoreResultV1:
    if type(destination_paths) is not DataPaths:
        raise TypeError("restore requires the exact destination DataPaths provider")
    if reference_paths is not None and type(reference_paths) is not DataPaths:
        raise TypeError("restore reference source must be DataPaths or null")
    if type(conflict_policy) is not RestoreConflictPolicyV1:
        raise TypeError("restore conflict policy is invalid")
    _token(accepted_consent_id, "restore accepted consent ID")
    try:
        backup = verify_backup(backup_root)
    except BackupRefused as error:
        raise RestoreRefused(
            RestoreRefusalCodeV1.BACKUP_INVALID,
            "restore source is not a complete verified backup",
            cause=error,
        ) from error
    manifest = backup.manifest
    if _is_within(destination_paths.root, backup.root) or (
        reference_paths is not None
        and _is_within(destination_paths.root, reference_paths.root)
    ):
        raise RestoreRefused(
            RestoreRefusalCodeV1.DESTINATION_INVALID,
            "restore destination must be separate from backup and reference roots",
        )
    if manifest.selection.consent_id != accepted_consent_id:
        raise RestoreRefused(
            RestoreRefusalCodeV1.CONSENT_MISMATCH,
            "restore consent does not match the backup capture decision",
        )
    if (
        manifest.selection.encryption_status
        is not BackupEncryptionStatusV1.NOT_ENCRYPTED
    ):
        raise RestoreRefused(
            RestoreRefusalCodeV1.ENCRYPTION_UNSUPPORTED,
            "this release cannot decrypt the declared backup",
        )
    selected_entries = tuple(
        item
        for item in manifest.entries
        if item.disposition is not BackupDispositionV1.OMITTED
    )
    references = tuple(
        item
        for item in selected_entries
        if item.disposition is BackupDispositionV1.REFERENCED
    )
    if references:
        if reference_paths is None:
            raise RestoreRefused(
                RestoreRefusalCodeV1.REFERENCE_SOURCE_REQUIRED,
                "digest-referenced datasets require the explicit source data root",
            )
        if data_root_identity(reference_paths) != manifest.source_data_root_id:
            raise RestoreRefused(
                RestoreRefusalCodeV1.REFERENCE_SOURCE_MISMATCH,
                "reference data root identity differs from the backup manifest",
            )
        try:
            reference_paths.validate(_area_ids(references))
            for entry in references:
                _require_entry_digest(_entry_source(reference_paths, entry), entry)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RestoreRefused(
                RestoreRefusalCodeV1.REFERENCE_SOURCE_MISMATCH,
                "one or more digest-referenced datasets are missing or changed",
                cause=error,
            ) from error

    targets = tuple(
        RestoreTargetV1(
            area_id=item.area_id,
            relative_path=item.relative_path,
            byte_count=item.byte_count,
            sha256=item.sha256,
        )
        for item in selected_entries
    )
    destination_root = destination_paths.root
    if destination_root.exists() or destination_root.is_symlink():
        if conflict_policy is not RestoreConflictPolicyV1.ACCEPT_IDENTICAL_ONLY:
            raise RestoreRefused(
                RestoreRefusalCodeV1.DESTINATION_CONFLICT,
                "destination data root already exists; no content was overwritten",
            )
        try:
            destination_paths.validate(_area_ids(selected_entries))
            for entry in selected_entries:
                _require_entry_digest(_entry_source(destination_paths, entry), entry)
            _validate_candidate(destination_paths, selected_entries)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RestoreRefused(
                RestoreRefusalCodeV1.DESTINATION_CONFLICT,
                "existing destination is not byte-identical to the restore selection",
                cause=error,
            ) from error
        receipt = _receipt(
            manifest,
            destination_paths,
            conflict_policy,
            accepted_consent_id,
            targets,
            status=RestoreStatusV1.ALREADY_PRESENT,
        )
        return RestoreResultV1(destination_root=destination_root, receipt=receipt)

    try:
        destination_root.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RestoreRefused(
            RestoreRefusalCodeV1.DESTINATION_INVALID,
            "destination parent could not be created safely",
            cause=error,
        ) from error
    if destination_root.parent.is_symlink() or not destination_root.parent.is_dir():
        raise RestoreRefused(
            RestoreRefusalCodeV1.DESTINATION_INVALID,
            "destination parent is not one real directory",
        )
    try:
        with tempfile.TemporaryDirectory(
            dir=destination_root.parent,
            prefix=f".{destination_root.name}-restore-",
        ) as temporary_name:
            candidate_root = Path(temporary_name) / "data-root"
            candidate_paths = DataPaths(candidate_root.resolve(strict=False))
            pack_registry: PackRegistryV1 | None = None
            pack_objects_touched = False
            try:
                area_ids = _area_ids(selected_entries)
                if area_ids:
                    candidate_paths.ensure(area_ids)
                for entry in selected_entries:
                    if entry.disposition is BackupDispositionV1.INCLUDED:
                        if entry.object_path is None:
                            raise ValueError(
                                "included restore entry lacks its object path"
                            )
                        source = backup.root.joinpath(
                            *PurePosixPath(entry.object_path).parts
                        )
                    else:
                        if reference_paths is None:
                            raise ValueError(
                                "referenced restore entry lacks its source root"
                            )
                        source = _entry_source(reference_paths, entry)
                    destination = _entry_source(candidate_paths, entry)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _copy_verified(source, destination, entry)
                _validate_candidate(
                    candidate_paths,
                    selected_entries,
                    require_pack_read_only=False,
                )
                candidate_paths.ensure(DataAreaId.RELEASE)
                receipt = _receipt(
                    manifest,
                    destination_paths,
                    conflict_policy,
                    accepted_consent_id,
                    targets,
                    status=RestoreStatusV1.RESTORED,
                )
                receipt_path = candidate_paths.release / "restore-receipt.json"
                _write_new_file(receipt_path, receipt.canonical_bytes())
                restored_receipt = RestoreReceiptV1.from_canonical_bytes(
                    _read_stable_file(
                        receipt_path,
                        maximum_bytes=MAX_RESTORE_RECEIPT_BYTES_V1,
                    )
                )
                if restored_receipt != receipt:
                    raise ValueError("restore receipt failed complete read-back")
                for entry in selected_entries:
                    _require_entry_digest(_entry_source(candidate_paths, entry), entry)
                if DataAreaId.PACKS in area_ids:
                    pack_registry = read_pack_registry(paths=candidate_paths)
                    pack_objects_touched = True
                    _seal_installed_pack_objects(candidate_paths, pack_registry)
                    _validate_installed_pack_objects(
                        candidate_paths,
                        pack_registry,
                        require_read_only=True,
                    )
                if destination_root.exists() or destination_root.is_symlink():
                    raise FileExistsError(
                        "destination appeared before restore activation"
                    )
                os.rename(candidate_root, destination_root)
                try:
                    _fsync_directory(destination_root.parent)
                except OSError:
                    os.rename(destination_root, candidate_root)
                    _fsync_directory(destination_root.parent)
                    raise
            finally:
                if (
                    pack_objects_touched
                    and pack_registry is not None
                    and candidate_root.exists()
                ):
                    _make_pack_objects_writable(candidate_paths, pack_registry)
    except RestoreRefused:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RestoreRefused(
            RestoreRefusalCodeV1.ACTIVATION_FAILED,
            "restore failed before durable atomic activation; source was untouched",
            cause=error,
        ) from error
    return RestoreResultV1(destination_root=destination_root, receipt=receipt)


def _receipt(
    manifest: BackupManifestV1,
    destination_paths: DataPaths,
    conflict_policy: RestoreConflictPolicyV1,
    accepted_consent_id: str,
    targets: tuple[RestoreTargetV1, ...],
    *,
    status: RestoreStatusV1,
) -> RestoreReceiptV1:
    return RestoreReceiptV1(
        restore_id="",
        status=status,
        backup_id=manifest.backup_id,
        backup_manifest_sha256=manifest.sha256,
        source_data_root_id=manifest.source_data_root_id,
        destination_data_root_id=data_root_identity(destination_paths),
        conflict_policy=conflict_policy,
        accepted_consent_id=accepted_consent_id,
        targets=targets,
    )


def _validate_candidate(
    paths: DataPaths,
    entries: tuple[BackupEntryV1, ...],
    *,
    require_pack_read_only: bool = True,
) -> None:
    for entry in entries:
        path = _entry_source(paths, entry)
        _validate_parser(path, entry)
    if any(item.area_id is DataAreaId.PACKS for item in entries):
        try:
            registry = read_pack_registry(paths=paths)
            _validate_installed_pack_objects(
                paths,
                registry,
                require_read_only=require_pack_read_only,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RestoreRefused(
                RestoreRefusalCodeV1.PACK_OR_DEPENDENCY_INVALID,
                "restored pack registry, objects, or dependency closure is invalid",
                cause=error,
            ) from error


def _validate_installed_pack_objects(
    paths: DataPaths,
    registry: PackRegistryV1,
    *,
    require_read_only: bool,
) -> None:
    for registry_entry in registry.entries:
        object_root = paths.packs.joinpath(
            *PurePosixPath(registry_entry.object_path).parts
        )
        expected_files = {K2PACK_MANIFEST_PATH} | {
            item.path for item in registry_entry.manifest.inventory
        }
        expected_directories = _expected_directories(expected_files)
        actual_files, actual_directories = _tree_inventory(object_root)
        if actual_files != expected_files or actual_directories != expected_directories:
            raise ValueError("installed pack object differs from its manifest tree")
        manifest_raw = canonical_manifest_bytes(registry_entry.manifest)
        manifest_path = object_root.joinpath(
            *PurePosixPath(K2PACK_MANIFEST_PATH).parts
        )
        if _read_stable_file(
            manifest_path,
            maximum_bytes=len(manifest_raw),
        ) != manifest_raw:
            raise ValueError("installed pack manifest bytes are not canonical")
        for item in registry_entry.manifest.inventory:
            _require_file_identity(
                object_root.joinpath(*PurePosixPath(item.path).parts),
                byte_count=item.byte_count,
                sha256=item.sha256,
            )
        if require_read_only:
            _require_mode(object_root, mode=0o500, directory=True)
            for relative_path in expected_directories:
                _require_mode(
                    object_root.joinpath(*PurePosixPath(relative_path).parts),
                    mode=0o500,
                    directory=True,
                )
            for relative_path in expected_files:
                _require_mode(
                    object_root.joinpath(*PurePosixPath(relative_path).parts),
                    mode=0o400,
                    directory=False,
                )


def _seal_installed_pack_objects(
    paths: DataPaths,
    registry: PackRegistryV1,
) -> None:
    for registry_entry in registry.entries:
        object_root = paths.packs.joinpath(
            *PurePosixPath(registry_entry.object_path).parts
        )
        expected_files = {K2PACK_MANIFEST_PATH} | {
            item.path for item in registry_entry.manifest.inventory
        }
        expected_directories = _expected_directories(expected_files)
        for relative_path in sorted(expected_files, key=lambda item: item.encode("utf-8")):
            _set_mode(
                object_root.joinpath(*PurePosixPath(relative_path).parts),
                mode=0o400,
                directory=False,
            )
        for relative_path in sorted(
            expected_directories,
            key=lambda item: (-item.count("/"), item.encode("utf-8")),
        ):
            _set_mode(
                object_root.joinpath(*PurePosixPath(relative_path).parts),
                mode=0o500,
                directory=True,
            )
        _set_mode(object_root, mode=0o500, directory=True)


def _make_pack_objects_writable(
    paths: DataPaths,
    registry: PackRegistryV1,
) -> None:
    for registry_entry in registry.entries:
        try:
            object_root = paths.packs.joinpath(
                *PurePosixPath(registry_entry.object_path).parts
            )
            expected_files = {K2PACK_MANIFEST_PATH} | {
                item.path for item in registry_entry.manifest.inventory
            }
            expected_directories = _expected_directories(expected_files)
            _set_mode(object_root, mode=0o700, directory=True)
            for relative_path in sorted(
                expected_directories,
                key=lambda item: (item.count("/"), item.encode("utf-8")),
            ):
                _set_mode(
                    object_root.joinpath(*PurePosixPath(relative_path).parts),
                    mode=0o700,
                    directory=True,
                )
            for relative_path in sorted(
                expected_files,
                key=lambda item: item.encode("utf-8"),
            ):
                _set_mode(
                    object_root.joinpath(*PurePosixPath(relative_path).parts),
                    mode=0o600,
                    directory=False,
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            continue


def _expected_directories(files: set[str]) -> set[str]:
    result: set[str] = set()
    for relative_path in files:
        parts = PurePosixPath(relative_path).parts[:-1]
        for depth in range(1, len(parts) + 1):
            result.add("/".join(parts[:depth]))
    return result


def _tree_inventory(root: Path) -> tuple[set[str], set[str]]:
    _require_mode(root, mode=None, directory=True)
    files: set[str] = set()
    directories: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for item in reversed(ordered):
            metadata = item.stat(follow_symlinks=False)
            path = Path(item.path)
            relative_path = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative_path)
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative_path)
            else:
                raise ValueError("installed pack object contains a link or special node")
    return files, directories


def _validate_parser(path: Path, entry: BackupEntryV1) -> None:
    try:
        name = entry.relative_path.casefold()
        if entry.schema_version != 1:
            raise ValueError("unsupported restore artifact schema version")
        if entry.schema_id == "KIRBY2_JSON_ARTIFACT":
            if name.endswith(".jsonl"):
                _validate_jsonl(path, byte_count=entry.byte_count)
            else:
                raw = _read_stable_file(
                    path,
                    maximum_bytes=min(
                        entry.byte_count,
                        MAX_RESTORE_STRUCTURED_DOCUMENT_BYTES_V1,
                    ),
                )
                json.loads(raw.decode("utf-8"))
        elif entry.schema_id == "KIRBY2_TOML_ARTIFACT":
            raw = _read_stable_file(
                path,
                maximum_bytes=min(
                    entry.byte_count,
                    MAX_RESTORE_STRUCTURED_DOCUMENT_BYTES_V1,
                ),
            )
            payload = tomllib.loads(raw.decode("utf-8"))
            if entry.family is BackupFamilyV1.RUN_MANIFESTS:
                RunManifest.from_dict(payload)
        elif entry.schema_id == "KIRBY2_PARQUET_ARTIFACT":
            _validate_parquet(path)
        elif entry.schema_id == "KIRBY2_PORTABLE_PACK_TRANSPORT":
            preflight_pack_archive_bytes(read_pack_archive_bytes(path))
        elif entry.schema_id != "KIRBY2_OPAQUE_FILE":
            raise ValueError("unknown restore artifact schema ID")
    except RestoreRefused:
        raise
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as error:
        raise RestoreRefused(
            RestoreRefusalCodeV1.SCHEMA_OR_PARSER_INVALID,
            f"restored artifact failed parser/schema verification: {entry.relative_path}",
            cause=error,
        ) from error


def _entry_source(paths: DataPaths, entry: BackupEntryV1) -> Path:
    return paths.area(entry.area_id).joinpath(
        *PurePosixPath(entry.relative_path).parts
    )


def _area_ids(entries: tuple[BackupEntryV1, ...]) -> tuple[DataAreaId, ...]:
    selected = {item.area_id for item in entries}
    return tuple(item for item in DataAreaId if item in selected)


def _copy_verified(source: Path, destination: Path, entry: BackupEntryV1) -> None:
    source_descriptor = _open_regular_file(source)
    destination_descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        before = os.fstat(source_descriptor)
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > entry.byte_count:
                raise ValueError("restore source exceeds declared size")
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if (
            _stat_identity(before) != _stat_identity(after)
            or byte_count != entry.byte_count
            or not hmac.compare_digest(digest.hexdigest(), entry.sha256)
        ):
            raise ValueError("restore source differs from its manifest")
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _require_entry_digest(path: Path, entry: BackupEntryV1) -> None:
    _require_file_identity(
        path,
        byte_count=entry.byte_count,
        sha256=entry.sha256,
    )


def _require_file_identity(
    path: Path,
    *,
    byte_count: int,
    sha256: str,
) -> None:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        observed_byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed_byte_count += len(chunk)
            if observed_byte_count > byte_count:
                raise ValueError("restore target exceeds declared size")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            _stat_identity(before) != _stat_identity(after)
            or observed_byte_count != byte_count
            or not hmac.compare_digest(digest.hexdigest(), sha256)
        ):
            raise ValueError("restore target differs from its declared digest")
    finally:
        os.close(descriptor)


def _require_mode(path: Path, *, mode: int | None, directory: bool) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode) or _wrong_owner(metadata):
        raise ValueError("restore pack entry has unsafe type or ownership")
    if not directory and metadata.st_nlink != 1:
        raise ValueError("restore pack file has multiple filesystem links")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise ValueError("restore pack entry has an unexpected access mode")


def _set_mode(path: Path, *, mode: int, directory: bool) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_kind(before.st_mode)
            or _wrong_owner(before)
            or (not directory and before.st_nlink != 1)
        ):
            raise ValueError("restore pack entry cannot be sealed safely")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            _stat_identity(before)[:2] != _stat_identity(after)[:2]
            or stat.S_IMODE(after.st_mode) != mode
        ):
            raise ValueError("restore pack entry mode transition was not durable")
    finally:
        os.close(descriptor)


def _read_stable_file(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum_bytes:
            raise ValueError("restore artifact exceeds its parser byte limit")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        raw = b"".join(chunks)
        if len(raw) != before.st_size or _stat_identity(before) != _stat_identity(after):
            raise ValueError("restore artifact changed during read")
        return raw
    finally:
        os.close(descriptor)


def _validate_jsonl(path: Path, *, byte_count: int) -> None:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if before.st_size != byte_count:
            raise ValueError("JSONL artifact differs from its declared byte count")
        buffer = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            buffer.extend(chunk)
            consumed = 0
            while True:
                newline = buffer.find(b"\n", consumed)
                if newline < 0:
                    break
                line = bytes(buffer[consumed:newline])
                if len(line) > MAX_RESTORE_JSONL_ROW_BYTES_V1:
                    raise ValueError("JSONL artifact row exceeds its parser limit")
                json.loads(line.decode("utf-8"))
                consumed = newline + 1
            if consumed:
                del buffer[:consumed]
            if len(buffer) > MAX_RESTORE_JSONL_ROW_BYTES_V1:
                raise ValueError("JSONL artifact row exceeds its parser limit")
        after = os.fstat(descriptor)
        if remaining or _stat_identity(before) != _stat_identity(after):
            raise ValueError("JSONL artifact changed during parser verification")
        if buffer:
            raise ValueError("JSONL artifact has a partial final row")
    finally:
        os.close(descriptor)


def _validate_parquet(path: Path) -> None:
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            "DuckDB is required to verify restored Parquet artifacts"
        ) from error
    connection = duckdb.connect(":memory:")
    try:
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?)",
            [str(path.resolve(strict=True))],
        )
        while cursor.fetchmany(256):
            pass
    finally:
        connection.close()


def _open_regular_file(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("restore source is not a regular file")
    return descriptor


def _write_new_file(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("restore write made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _wrong_owner(metadata: os.stat_result) -> bool:
    return hasattr(os, "geteuid") and metadata.st_uid != os.geteuid()


def _relative_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError("restore relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("restore relative path is not normalized")
    if path.as_posix() != value:
        raise ValueError("restore relative path is not canonical")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} digest is invalid")
    return value


def _token(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 256
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _text(value: dict[str, object], key: str) -> str:
    selected = value[key]
    if type(selected) is not str:
        raise TypeError(f"{key} must be text")
    return selected


def _integer(value: dict[str, object], key: str) -> int:
    selected = value[key]
    if type(selected) is not int:
        raise TypeError(f"{key} must be an integer")
    return selected


__all__ = [
    "RESTORE_RECEIPT_SCHEMA_ID_V1",
    "RESTORE_RECEIPT_SCHEMA_VERSION_V1",
    "RestoreConflictPolicyV1",
    "RestoreReceiptV1",
    "RestoreRefusalCodeV1",
    "RestoreRefused",
    "RestoreResultV1",
    "RestoreStatusV1",
    "RestoreTargetV1",
    "restore_backup",
]
