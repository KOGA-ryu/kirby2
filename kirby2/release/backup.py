"""Deterministic, content-addressed backups of governed Kirby2 user data."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import ClassVar

from kirby2.packs.archive import preflight_pack_archive_bytes, read_pack_archive_bytes
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.research.paths import DATA_PATHS_SCHEMA_VERSION, DataAreaId, DataPaths


BACKUP_MANIFEST_SCHEMA_ID_V1 = "KIRBY2_USER_DATA_BACKUP_MANIFEST_V1"
BACKUP_MANIFEST_SCHEMA_VERSION_V1 = 1
MAX_BACKUP_MANIFEST_BYTES_V1 = 64 * 1024 * 1024
MAX_BACKUP_FILE_COUNT_V1 = 200_000
MAX_BACKUP_ITEM_BYTES_V1 = (1 << 63) - 1
DEFAULT_MAX_EMBEDDED_DATASET_BYTES_V1 = 512 * 1024 * 1024


class BackupFamilyV1(str, Enum):
    CONFIGURATION = "CONFIGURATION"
    PSEUDONYMOUS_PROFILES = "PSEUDONYMOUS_PROFILES"
    STRATEGIES = "STRATEGIES"
    CURRICULA = "CURRICULA"
    ANNOTATIONS = "ANNOTATIONS"
    LEARNER_EVIDENCE = "LEARNER_EVIDENCE"
    LEARNER_PROJECTIONS = "LEARNER_PROJECTIONS"
    RUN_MANIFESTS = "RUN_MANIFESTS"
    PORTABLE_ARTIFACTS = "PORTABLE_ARTIFACTS"
    DATASETS = "DATASETS"


class BackupDispositionV1(str, Enum):
    INCLUDED = "INCLUDED"
    REFERENCED = "REFERENCED"
    OMITTED = "OMITTED"


class DatasetBackupPolicyV1(str, Enum):
    EMBED = "EMBED"
    REFERENCE = "REFERENCE"
    OMIT = "OMIT"


class BackupEncryptionStatusV1(str, Enum):
    NOT_ENCRYPTED = "NOT_ENCRYPTED"


class BackupRefusalCodeV1(str, Enum):
    SOURCE_INVALID = "SOURCE_INVALID"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    SELECTION_INVALID = "SELECTION_INVALID"
    DESTINATION_INVALID = "DESTINATION_INVALID"
    DESTINATION_EXISTS = "DESTINATION_EXISTS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    PORTABLE_ARTIFACT_INVALID = "PORTABLE_ARTIFACT_INVALID"
    BACKUP_INCOMPLETE = "BACKUP_INCOMPLETE"
    BACKUP_CORRUPT = "BACKUP_CORRUPT"


class BackupRefused(RuntimeError):
    def __init__(
        self,
        code: BackupRefusalCodeV1,
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
class BackupSelectionV1:
    families: tuple[BackupFamilyV1, ...]
    dataset_policy: DatasetBackupPolicyV1
    consent_id: str
    redaction_policy_id: str
    encryption_status: BackupEncryptionStatusV1 = (
        BackupEncryptionStatusV1.NOT_ENCRYPTED
    )
    max_embedded_dataset_bytes: int = DEFAULT_MAX_EMBEDDED_DATASET_BYTES_V1

    def __post_init__(self) -> None:
        if type(self.families) is not tuple or any(
            type(item) is not BackupFamilyV1 for item in self.families
        ):
            raise TypeError("backup families must be a typed tuple")
        canonical = tuple(item for item in BackupFamilyV1 if item in self.families)
        if self.families != canonical:
            raise ValueError("backup families must be unique and canonical")
        if type(self.dataset_policy) is not DatasetBackupPolicyV1:
            raise TypeError("dataset backup policy is invalid")
        _token(self.consent_id, "backup consent ID")
        _token(self.redaction_policy_id, "backup redaction policy ID")
        if self.encryption_status is not BackupEncryptionStatusV1.NOT_ENCRYPTED:
            raise ValueError("unsupported backup encryption status")
        if (
            type(self.max_embedded_dataset_bytes) is not int
            or self.max_embedded_dataset_bytes < 0
        ):
            raise ValueError("embedded dataset byte limit must be nonnegative")

    @classmethod
    def all_portable(
        cls,
        *,
        dataset_policy: DatasetBackupPolicyV1 = DatasetBackupPolicyV1.REFERENCE,
        consent_id: str = "LOCAL_USER_REQUEST_V1",
        redaction_policy_id: str = "EXCLUDE_DIRECT_IDENTITY_V1",
        max_embedded_dataset_bytes: int = DEFAULT_MAX_EMBEDDED_DATASET_BYTES_V1,
    ) -> BackupSelectionV1:
        return cls(
            families=tuple(BackupFamilyV1),
            dataset_policy=dataset_policy,
            consent_id=consent_id,
            redaction_policy_id=redaction_policy_id,
            max_embedded_dataset_bytes=max_embedded_dataset_bytes,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "consent_id": self.consent_id,
            "dataset_policy": self.dataset_policy.value,
            "encryption_status": self.encryption_status.value,
            "families": [item.value for item in self.families],
            "max_embedded_dataset_bytes": self.max_embedded_dataset_bytes,
            "redaction_policy_id": self.redaction_policy_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> BackupSelectionV1:
        expected = {
            "consent_id",
            "dataset_policy",
            "encryption_status",
            "families",
            "max_embedded_dataset_bytes",
            "redaction_policy_id",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("backup selection fields differ")
        families = value["families"]
        if type(families) is not list or any(type(item) is not str for item in families):
            raise TypeError("backup selection families must be an array of text")
        return cls(
            families=tuple(BackupFamilyV1(item) for item in families),
            dataset_policy=DatasetBackupPolicyV1(_text(value, "dataset_policy")),
            consent_id=_text(value, "consent_id"),
            redaction_policy_id=_text(value, "redaction_policy_id"),
            encryption_status=BackupEncryptionStatusV1(
                _text(value, "encryption_status")
            ),
            max_embedded_dataset_bytes=_integer(
                value,
                "max_embedded_dataset_bytes",
            ),
        )


@dataclass(frozen=True, slots=True)
class BackupEntryV1:
    sequence: int
    family: BackupFamilyV1
    disposition: BackupDispositionV1
    area_id: DataAreaId
    relative_path: str
    object_path: str | None
    byte_count: int
    sha256: str
    schema_id: str
    schema_version: int
    provenance_sha256: str
    consent_id: str
    redaction_policy_id: str
    encryption_status: BackupEncryptionStatusV1

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("backup entry sequence must be positive")
        if type(self.family) is not BackupFamilyV1:
            raise TypeError("backup entry family is invalid")
        if type(self.disposition) is not BackupDispositionV1:
            raise TypeError("backup entry disposition is invalid")
        if type(self.area_id) is not DataAreaId:
            raise TypeError("backup entry area is invalid")
        _relative_path(self.relative_path, "backup source path")
        if self.area_id not in _SOURCE_AREAS:
            raise ValueError("backup entry uses an excluded data area")
        if self.family is not _classify(self.area_id, self.relative_path):
            raise ValueError("backup entry family differs from its governed path")
        if type(self.byte_count) is not int or not 0 <= self.byte_count <= MAX_BACKUP_ITEM_BYTES_V1:
            raise ValueError("backup entry byte count is invalid")
        _sha256(self.sha256, "backup entry")
        _token(self.schema_id, "backup entry schema ID")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("backup entry schema version must be positive")
        if (self.schema_id, self.schema_version) != _schema_identity(
            self.relative_path
        ):
            raise ValueError("backup entry schema differs from its path contract")
        _sha256(self.provenance_sha256, "backup entry provenance")
        _token(self.consent_id, "backup entry consent ID")
        _token(self.redaction_policy_id, "backup entry redaction policy ID")
        if self.encryption_status is not BackupEncryptionStatusV1.NOT_ENCRYPTED:
            raise ValueError("backup entry encryption status is unsupported")
        expected_object = _object_path(self.sha256)
        if self.disposition is BackupDispositionV1.INCLUDED:
            if self.object_path != expected_object:
                raise ValueError("included backup entry has another object path")
        elif self.object_path is not None:
            raise ValueError("non-included backup entry cannot carry an object path")
        if self.disposition is BackupDispositionV1.REFERENCED and (
            self.family is not BackupFamilyV1.DATASETS
        ):
            raise ValueError("only datasets may be digest-referenced")

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.area_id.value, self.relative_path

    def as_dict(self) -> dict[str, object]:
        return {
            "area_id": self.area_id.value,
            "byte_count": self.byte_count,
            "consent_id": self.consent_id,
            "disposition": self.disposition.value,
            "encryption_status": self.encryption_status.value,
            "family": self.family.value,
            "object_path": self.object_path,
            "provenance_sha256": self.provenance_sha256,
            "redaction_policy_id": self.redaction_policy_id,
            "relative_path": self.relative_path,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> BackupEntryV1:
        expected = {
            "area_id",
            "byte_count",
            "consent_id",
            "disposition",
            "encryption_status",
            "family",
            "object_path",
            "provenance_sha256",
            "redaction_policy_id",
            "relative_path",
            "schema_id",
            "schema_version",
            "sequence",
            "sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("backup entry fields differ")
        object_path = value["object_path"]
        if object_path is not None and type(object_path) is not str:
            raise TypeError("backup object path must be text or null")
        return cls(
            sequence=_integer(value, "sequence"),
            family=BackupFamilyV1(_text(value, "family")),
            disposition=BackupDispositionV1(_text(value, "disposition")),
            area_id=DataAreaId(_text(value, "area_id")),
            relative_path=_text(value, "relative_path"),
            object_path=object_path,
            byte_count=_integer(value, "byte_count"),
            sha256=_text(value, "sha256"),
            schema_id=_text(value, "schema_id"),
            schema_version=_integer(value, "schema_version"),
            provenance_sha256=_text(value, "provenance_sha256"),
            consent_id=_text(value, "consent_id"),
            redaction_policy_id=_text(value, "redaction_policy_id"),
            encryption_status=BackupEncryptionStatusV1(
                _text(value, "encryption_status")
            ),
        )


@dataclass(frozen=True, slots=True)
class BackupManifestV1:
    backup_id: str
    source_data_root_id: str
    data_paths_schema_version: int
    selection: BackupSelectionV1
    excluded_area_ids: tuple[DataAreaId, ...]
    entries: tuple[BackupEntryV1, ...]
    embedded_byte_count: int

    schema_id: ClassVar[str] = BACKUP_MANIFEST_SCHEMA_ID_V1
    schema_version: ClassVar[int] = BACKUP_MANIFEST_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _sha256(self.source_data_root_id, "backup source data-root")
        if self.data_paths_schema_version != DATA_PATHS_SCHEMA_VERSION:
            raise ValueError("backup data-path schema version differs")
        if type(self.selection) is not BackupSelectionV1:
            raise TypeError("backup selection is invalid")
        if type(self.excluded_area_ids) is not tuple or any(
            type(item) is not DataAreaId for item in self.excluded_area_ids
        ):
            raise TypeError("backup excluded areas must be a typed tuple")
        canonical_excluded = tuple(
            item for item in DataAreaId if item in self.excluded_area_ids
        )
        if (
            self.excluded_area_ids != canonical_excluded
            or self.excluded_area_ids != _EXCLUDED_AREAS
        ):
            raise ValueError("backup excluded areas must be unique and canonical")
        if type(self.entries) is not tuple or any(
            type(item) is not BackupEntryV1 for item in self.entries
        ):
            raise TypeError("backup entries must be a typed tuple")
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.sort_key)):
            raise ValueError("backup entries must use canonical path order")
        if [item.sequence for item in self.entries] != list(
            range(1, len(self.entries) + 1)
        ):
            raise ValueError("backup entry sequences must be contiguous")
        addresses = {(item.area_id, item.relative_path) for item in self.entries}
        if len(addresses) != len(self.entries):
            raise ValueError("backup entries contain duplicate source paths")
        for item in self.entries:
            expected_provenance = _provenance_sha256(
                area_id=item.area_id,
                relative_path=item.relative_path,
                sha256=item.sha256,
                source_data_root_id=self.source_data_root_id,
            )
            if not hmac.compare_digest(
                item.provenance_sha256,
                expected_provenance,
            ):
                raise ValueError("backup entry provenance differs from its source")
            expected_disposition = BackupDispositionV1.OMITTED
            if item.family in self.selection.families:
                if item.family is BackupFamilyV1.DATASETS:
                    if self.selection.dataset_policy is DatasetBackupPolicyV1.EMBED:
                        expected_disposition = BackupDispositionV1.INCLUDED
                    elif self.selection.dataset_policy is DatasetBackupPolicyV1.REFERENCE:
                        expected_disposition = BackupDispositionV1.REFERENCED
                else:
                    expected_disposition = BackupDispositionV1.INCLUDED
            if item.disposition is not expected_disposition:
                raise ValueError("backup entry disposition differs from selection policy")
            if (
                item.consent_id != self.selection.consent_id
                or item.redaction_policy_id != self.selection.redaction_policy_id
                or item.encryption_status is not self.selection.encryption_status
            ):
                raise ValueError("backup entry policy metadata differs from selection")
        expected_embedded = sum(
            item.byte_count
            for item in self.entries
            if item.disposition is BackupDispositionV1.INCLUDED
        )
        if self.embedded_byte_count != expected_embedded:
            raise ValueError("backup embedded byte count differs from entries")
        embedded_datasets = sum(
            item.byte_count
            for item in self.entries
            if item.family is BackupFamilyV1.DATASETS
            and item.disposition is BackupDispositionV1.INCLUDED
        )
        if embedded_datasets > self.selection.max_embedded_dataset_bytes:
            raise ValueError("backup embedded datasets exceed the selection limit")
        expected_id = "backup-" + hashlib.sha256(
            canonical_json_bytes(self.identity_dict())
        ).hexdigest()[:24]
        if not self.backup_id:
            object.__setattr__(self, "backup_id", expected_id)
        elif self.backup_id != expected_id:
            raise ValueError("backup identity differs from its manifest")

    def identity_dict(self) -> dict[str, object]:
        return {
            "data_paths_schema_version": self.data_paths_schema_version,
            "embedded_byte_count": self.embedded_byte_count,
            "entries": [item.as_dict() for item in self.entries],
            "excluded_area_ids": [item.value for item in self.excluded_area_ids],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "selection": self.selection.as_dict(),
            "source_data_root_id": self.source_data_root_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "backup_id": self.backup_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> BackupManifestV1:
        value = load_canonical_json_bytes(raw, "Kirby2 user-data backup manifest")
        expected = {
            "backup_id",
            "data_paths_schema_version",
            "embedded_byte_count",
            "entries",
            "excluded_area_ids",
            "schema_id",
            "schema_version",
            "selection",
            "source_data_root_id",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("backup manifest fields differ")
        if (
            value["schema_id"] != cls.schema_id
            or value["schema_version"] != cls.schema_version
        ):
            raise ValueError("backup manifest contract differs")
        entries = value["entries"]
        excluded = value["excluded_area_ids"]
        if type(entries) is not list or type(excluded) is not list:
            raise TypeError("backup manifest inventories must be arrays")
        restored = cls(
            backup_id=_text(value, "backup_id"),
            source_data_root_id=_text(value, "source_data_root_id"),
            data_paths_schema_version=_integer(value, "data_paths_schema_version"),
            selection=BackupSelectionV1.from_dict(value["selection"]),
            excluded_area_ids=tuple(DataAreaId(_exact_text(item)) for item in excluded),
            entries=tuple(BackupEntryV1.from_dict(item) for item in entries),
            embedded_byte_count=_integer(value, "embedded_byte_count"),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("backup manifest changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class BackupResultV1:
    root: Path
    manifest: BackupManifestV1

    @property
    def included_count(self) -> int:
        return sum(
            item.disposition is BackupDispositionV1.INCLUDED
            for item in self.manifest.entries
        )

    @property
    def referenced_count(self) -> int:
        return sum(
            item.disposition is BackupDispositionV1.REFERENCED
            for item in self.manifest.entries
        )

    @property
    def omitted_count(self) -> int:
        return sum(
            item.disposition is BackupDispositionV1.OMITTED
            for item in self.manifest.entries
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.manifest.backup_id,
            "backup_root": str(self.root),
            "embedded_byte_count": self.manifest.embedded_byte_count,
            "included_count": self.included_count,
            "manifest_sha256": self.manifest.sha256,
            "omitted_count": self.omitted_count,
            "referenced_count": self.referenced_count,
        }


_SOURCE_AREAS = (
    DataAreaId.CONFIG,
    DataAreaId.EVIDENCE,
    DataAreaId.CHECKPOINTS,
    DataAreaId.RUNS,
    DataAreaId.PACKS,
    DataAreaId.EXPORTS,
    DataAreaId.DATASETS,
)

_EXCLUDED_AREAS = tuple(
    item
    for item in DataAreaId
    if item not in _SOURCE_AREAS
)


def data_root_identity(paths: DataPaths) -> str:
    if type(paths) is not DataPaths:
        raise TypeError("data-root identity requires DataPaths")
    return hashlib.sha256(canonical_json_bytes(paths.as_dict())).hexdigest()


def create_backup(
    *,
    paths: DataPaths,
    selection: BackupSelectionV1,
    destination: Path,
) -> BackupResultV1:
    if type(paths) is not DataPaths:
        raise TypeError("backup requires the exact DataPaths provider")
    if type(selection) is not BackupSelectionV1:
        raise TypeError("backup selection is invalid")
    target = _resolved_destination(destination, source_root=paths.root)
    if target.exists() or target.is_symlink():
        raise BackupRefused(
            BackupRefusalCodeV1.DESTINATION_EXISTS,
            "backup destination already exists and will not be overwritten",
        )
    try:
        paths.validate(_SOURCE_AREAS)
        source_data_root_id = data_root_identity(paths)
        captured = _capture_inventory(
            paths,
            selection,
            source_data_root_id=source_data_root_id,
        )
        entries = tuple(
            BackupEntryV1(sequence=index, **item)
            for index, item in enumerate(captured, start=1)
        )
        manifest = BackupManifestV1(
            backup_id="",
            source_data_root_id=source_data_root_id,
            data_paths_schema_version=DATA_PATHS_SCHEMA_VERSION,
            selection=selection,
            excluded_area_ids=_EXCLUDED_AREAS,
            entries=entries,
            embedded_byte_count=sum(
                item.byte_count
                for item in entries
                if item.disposition is BackupDispositionV1.INCLUDED
            ),
        )
        manifest_bytes = manifest.canonical_bytes()
        if len(manifest_bytes) > MAX_BACKUP_MANIFEST_BYTES_V1:
            raise BackupRefused(
                BackupRefusalCodeV1.LIMIT_EXCEEDED,
                "backup manifest exceeds its publication byte limit",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink() or not target.parent.is_dir():
            raise ValueError("backup destination parent is not a real directory")
        with tempfile.TemporaryDirectory(
            dir=target.parent,
            prefix=f".{target.name}-",
        ) as temporary_name:
            stage = Path(temporary_name) / "backup"
            stage.mkdir(mode=0o700)
            for entry in manifest.entries:
                if entry.disposition is not BackupDispositionV1.INCLUDED:
                    continue
                source = paths.area(entry.area_id).joinpath(
                    *PurePosixPath(entry.relative_path).parts
                )
                if entry.object_path is None:
                    raise ValueError("included backup entry lacks its object path")
                object_path = stage.joinpath(*PurePosixPath(entry.object_path).parts)
                object_path.parent.mkdir(parents=True, exist_ok=True)
                if object_path.exists():
                    _require_file_digest(object_path, entry)
                else:
                    _copy_stable_file(source, object_path, entry)
            _write_new_file(stage / "manifest.json", manifest_bytes)
            verified = verify_backup(stage)
            if verified.manifest != manifest:
                raise ValueError("published backup differs from captured inventory")
            paths.validate(_SOURCE_AREAS)
            for entry in manifest.entries:
                if entry.disposition is BackupDispositionV1.REFERENCED:
                    source = paths.area(entry.area_id).joinpath(
                        *PurePosixPath(entry.relative_path).parts
                    )
                    _require_file_digest(source, entry)
            if target.exists() or target.is_symlink():
                raise FileExistsError("backup destination appeared before activation")
            os.rename(stage, target)
            try:
                _fsync_directory(target.parent)
            except OSError:
                os.rename(target, stage)
                _fsync_directory(target.parent)
                raise
    except BackupRefused:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise BackupRefused(
            BackupRefusalCodeV1.BACKUP_INCOMPLETE,
            "backup failed before complete atomic publication",
            cause=error,
        ) from error
    return BackupResultV1(root=target, manifest=manifest)


def verify_backup(root: Path) -> BackupResultV1:
    try:
        selected = _resolved_existing_directory(root, "backup root")
        manifest_raw = _read_stable_file(
            selected / "manifest.json",
            maximum_bytes=MAX_BACKUP_MANIFEST_BYTES_V1,
        )
        manifest = BackupManifestV1.from_canonical_bytes(manifest_raw)
        expected_files = {"manifest.json"}
        object_entries: dict[str, BackupEntryV1] = {}
        for entry in manifest.entries:
            if entry.disposition is BackupDispositionV1.INCLUDED:
                if entry.object_path is None:
                    raise ValueError("included backup entry lacks its object")
                expected_files.add(entry.object_path)
                object_entries.setdefault(entry.object_path, entry)
        actual_files = {
            path.relative_to(selected).as_posix()
            for path in _walk_regular_files(selected)
        }
        if actual_files != expected_files:
            raise ValueError("backup contains missing, unknown, or ambient files")
        for object_path, entry in object_entries.items():
            path = selected.joinpath(*PurePosixPath(object_path).parts)
            _require_file_digest(path, entry)
        for entry in manifest.entries:
            if (
                entry.disposition is BackupDispositionV1.INCLUDED
                and entry.relative_path.casefold().endswith(".k2pack")
            ):
                raw = read_pack_archive_bytes(
                    selected.joinpath(*PurePosixPath(entry.object_path or "").parts)
                )
                preflight_pack_archive_bytes(raw)
        return BackupResultV1(root=selected, manifest=manifest)
    except BackupRefused:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise BackupRefused(
            BackupRefusalCodeV1.BACKUP_CORRUPT,
            "backup manifest or payload inventory failed verification",
            cause=error,
        ) from error


def _capture_inventory(
    paths: DataPaths,
    selection: BackupSelectionV1,
    *,
    source_data_root_id: str,
) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []
    embedded_dataset_bytes = 0
    for area_id in _SOURCE_AREAS:
        root = paths.area(area_id)
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"backup source area is not a real directory: {area_id.value}")
        for path in _walk_regular_files(root):
            if len(captured) >= MAX_BACKUP_FILE_COUNT_V1:
                raise BackupRefused(
                    BackupRefusalCodeV1.LIMIT_EXCEEDED,
                    "backup source exceeds the file-count limit",
                )
            relative_path = path.relative_to(root).as_posix()
            family = _classify(area_id, relative_path)
            byte_count, digest = _stable_file_identity(path)
            disposition = BackupDispositionV1.OMITTED
            if family in selection.families:
                if family is BackupFamilyV1.DATASETS:
                    if selection.dataset_policy is DatasetBackupPolicyV1.EMBED:
                        embedded_dataset_bytes += byte_count
                        if embedded_dataset_bytes > selection.max_embedded_dataset_bytes:
                            raise BackupRefused(
                                BackupRefusalCodeV1.LIMIT_EXCEEDED,
                                "embedded datasets exceed the explicit selection limit",
                            )
                        disposition = BackupDispositionV1.INCLUDED
                    elif selection.dataset_policy is DatasetBackupPolicyV1.REFERENCE:
                        disposition = BackupDispositionV1.REFERENCED
                else:
                    disposition = BackupDispositionV1.INCLUDED
            schema_id, schema_version = _schema_identity(relative_path)
            provenance = _provenance_sha256(
                area_id=area_id,
                relative_path=relative_path,
                sha256=digest,
                source_data_root_id=source_data_root_id,
            )
            captured.append(
                {
                    "family": family,
                    "disposition": disposition,
                    "area_id": area_id,
                    "relative_path": relative_path,
                    "object_path": (
                        _object_path(digest)
                        if disposition is BackupDispositionV1.INCLUDED
                        else None
                    ),
                    "byte_count": byte_count,
                    "sha256": digest,
                    "schema_id": schema_id,
                    "schema_version": schema_version,
                    "provenance_sha256": provenance,
                    "consent_id": selection.consent_id,
                    "redaction_policy_id": selection.redaction_policy_id,
                    "encryption_status": selection.encryption_status,
                }
            )
    captured.sort(key=lambda item: (str(item["area_id"].value), str(item["relative_path"])))
    return captured


def _classify(area_id: DataAreaId, relative_path: str) -> BackupFamilyV1:
    parts = tuple(part.casefold() for part in PurePosixPath(relative_path).parts)
    if area_id is DataAreaId.DATASETS:
        return BackupFamilyV1.DATASETS
    if area_id is DataAreaId.RUNS and parts[-1:] == ("manifest.toml",):
        return BackupFamilyV1.RUN_MANIFESTS
    if area_id in {DataAreaId.PACKS, DataAreaId.EXPORTS, DataAreaId.RUNS}:
        return BackupFamilyV1.PORTABLE_ARTIFACTS
    if area_id is DataAreaId.CHECKPOINTS or "projections" in parts:
        return BackupFamilyV1.LEARNER_PROJECTIONS
    if any(part.startswith("profile") for part in parts):
        return BackupFamilyV1.PSEUDONYMOUS_PROFILES
    if any(part.startswith("strateg") for part in parts):
        return BackupFamilyV1.STRATEGIES
    if any(part.startswith("curricul") for part in parts):
        return BackupFamilyV1.CURRICULA
    if any(part.startswith("annotat") for part in parts):
        return BackupFamilyV1.ANNOTATIONS
    if area_id is DataAreaId.EVIDENCE:
        return BackupFamilyV1.LEARNER_EVIDENCE
    return BackupFamilyV1.CONFIGURATION


def _schema_identity(relative_path: str) -> tuple[str, int]:
    name = relative_path.casefold()
    if name.endswith(".k2pack"):
        return "KIRBY2_PORTABLE_PACK_TRANSPORT", 1
    if name.endswith(".parquet"):
        return "KIRBY2_PARQUET_ARTIFACT", 1
    if name.endswith(".toml"):
        return "KIRBY2_TOML_ARTIFACT", 1
    if name.endswith(".json") or name.endswith(".jsonl"):
        return "KIRBY2_JSON_ARTIFACT", 1
    return "KIRBY2_OPAQUE_FILE", 1


def _walk_regular_files(root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("backup inventory encountered a linked or invalid directory")
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in reversed(ordered):
            if entry.is_symlink():
                raise ValueError("backup inventory refuses symbolic links")
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                result.append(Path(entry.path))
            else:
                raise ValueError("backup inventory refuses special filesystem nodes")
    return tuple(sorted(result, key=lambda item: item.relative_to(root).as_posix()))


def _stable_file_identity(path: Path) -> tuple[int, str]:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > MAX_BACKUP_ITEM_BYTES_V1:
                raise ValueError("backup item exceeds its byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after) or byte_count != before.st_size:
            raise ValueError("backup source changed while hashing")
        return byte_count, digest.hexdigest()
    finally:
        os.close(descriptor)


def _copy_stable_file(source: Path, destination: Path, entry: BackupEntryV1) -> None:
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
                raise ValueError("backup source grew during capture")
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        if (
            _stat_identity(before) != _stat_identity(after)
            or byte_count != entry.byte_count
            or not hmac.compare_digest(digest.hexdigest(), entry.sha256)
        ):
            raise ValueError("backup source differs from captured inventory")
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _require_file_digest(path: Path, entry: BackupEntryV1) -> None:
    byte_count, digest = _stable_file_identity(path)
    if byte_count != entry.byte_count or not hmac.compare_digest(digest, entry.sha256):
        raise ValueError("backup object differs from its manifest entry")


def _read_stable_file(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum_bytes:
            raise ValueError("backup file exceeds its byte limit")
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
            raise ValueError("backup file changed during read")
        return raw
    finally:
        os.close(descriptor)


def _open_regular_file(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 0:
        os.close(descriptor)
        raise ValueError("backup source is not a regular file")
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
            raise OSError("backup write made no progress")
        view = view[written:]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _object_path(digest: str) -> str:
    _sha256(digest, "backup object")
    return f"objects/{digest[:2]}/{digest}"


def _provenance_sha256(
    *,
    area_id: DataAreaId,
    relative_path: str,
    sha256: str,
    source_data_root_id: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "area_id": area_id.value,
                "relative_path": relative_path,
                "sha256": sha256,
                "source_data_root_id": source_data_root_id,
            }
        )
    ).hexdigest()


def _relative_path(value: object, label: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} is not a normalized relative path")
    if path.as_posix() != value:
        raise ValueError(f"{label} is not canonical")
    return value


def _resolved_destination(destination: Path, *, source_root: Path) -> Path:
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise BackupRefused(
            BackupRefusalCodeV1.DESTINATION_INVALID,
            "backup destination must be an explicit absolute Path",
        )
    try:
        resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise BackupRefused(
            BackupRefusalCodeV1.DESTINATION_INVALID,
            "backup destination cannot be resolved safely",
            cause=error,
        ) from error
    if resolved != destination:
        raise BackupRefused(
            BackupRefusalCodeV1.DESTINATION_INVALID,
            "backup destination must be supplied already resolved",
        )
    try:
        resolved.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise BackupRefused(
            BackupRefusalCodeV1.DESTINATION_INVALID,
            "backup destination cannot be inside the source data root",
        )
    return resolved


def _resolved_existing_directory(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an explicit absolute Path")
    resolved = path.resolve(strict=True)
    if path != resolved or path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be one already-resolved real directory")
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _exact_text(value: object) -> str:
    if type(value) is not str:
        raise TypeError("backup inventory values must be text")
    return value


__all__ = [
    "BACKUP_MANIFEST_SCHEMA_ID_V1",
    "BACKUP_MANIFEST_SCHEMA_VERSION_V1",
    "BackupDispositionV1",
    "BackupEncryptionStatusV1",
    "BackupEntryV1",
    "BackupFamilyV1",
    "BackupManifestV1",
    "BackupRefusalCodeV1",
    "BackupRefused",
    "BackupResultV1",
    "BackupSelectionV1",
    "DatasetBackupPolicyV1",
    "create_backup",
    "data_root_identity",
    "verify_backup",
]
