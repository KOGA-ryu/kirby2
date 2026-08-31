"""Immutable two-attempt construction and verification of Kirby2 release artifacts.

This module is the WO40-F execution layer.  It consumes only a verified Git
candidate, the frozen release protocols, preflighted offline wheels, and the
fingerprinted local build runtime.  Disposable attempts are isolated in owner-only
system temporary roots, while public output is confined to the explicit artifact
store.  The external artifact index is published last and is the activation marker
for a complete release set.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Mapping

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)
from kirby2.research.paths import DataAreaId, DataPaths

from .build import (
    RELEASE_ARTIFACT_LAYOUT_SCHEMA_ID_V1,
    ReleaseBuildRefusalCodeV1,
    ReleaseBuildRefused,
    ReleaseBuildRuntimeSnapshotV1,
    ReleaseCandidateInputsV1,
    ReleaseCommandOutcomeV1,
    ReleaseCommandStatusV1,
    ReleaseProtocolBundleV1,
    _candidate_blob_bytes,
    _candidate_tree_objects,
    plan_release_build,
    release_resource_preflight,
    verify_release_candidate_inputs,
)
from .first_run import (
    RELEASE_STARTER_SET_ID_V1,
    ReleaseStarterSetV1,
    build_release_starter_set,
)
from .licenses import (
    RELEASE_LICENSE_INVENTORY_SCHEMA_ID_V1,
    RELEASE_NOTICES_ENCODER_ID_V1,
    release_license_inventory_bytes,
    release_notices_bytes,
)
from .manifest import (
    RELEASE_ARTIFACT_ROWS_V1,
    RELEASE_ARTIFACT_SELECTORS_V1,
    RELEASE_MANIFEST_SCHEMA_ID_V1,
    RELEASE_REQUIRED_KNOWN_LIMITATIONS_V1,
    RELEASE_VERSION_V1,
    ReleaseArtifactIndexV1,
    ReleaseArtifactRecordV1,
    ReleaseAssetV1,
    ReleaseDependencyV1,
    ReleaseKnownLimitationV1,
    ReleaseManifestV1,
    ReleasePayloadMemberV1,
    ReleaseSchemaVersionV1,
    ReleaseStarterEntryManifestV1,
    ReleaseSubordinateArtifactV1,
    ReleaseTargetV1,
)
from .models import builtin_release_schema_inventory
from .packaging import (
    ArchiveMemberPlanV1,
    ReleaseSourceClassV1,
    build_canonical_release_archive,
    normalize_release_path,
    verify_canonical_release_archive,
)


RELEASE_ARTIFACT_BUILD_POLICY_ID_V1 = "KIRBY2_RELEASE_ARTIFACT_BUILD_POLICY_V1"
RELEASE_BUILD_RECORD_SCHEMA_ID_V1 = "KIRBY2_RELEASE_BUILD_RECORD_V1"
RELEASE_BUILD_ATTEMPT_COUNT_V1 = 2

RELEASE_BUILD_RECORD_FILENAME_V1 = "release-build-record.json"
RELEASE_ARTIFACT_INDEX_FILENAME_V1 = "release-artifact-index.json"
RELEASE_PROJECT_WHEEL_FILENAME_V1 = "kirby2-0.1.0-py3-none-any.whl"
RELEASE_NETWORK_POLICY_ID_V1 = "CODEX_SEATBELT_NETWORK_DISABLED_V1"
RELEASE_BUILD_TIMEOUT_SECONDS_V1 = 600
RELEASE_ARTIFACT_MAX_BYTES_V1 = 4 * 1024 * 1024 * 1024
RELEASE_RECORD_MAX_BYTES_V1 = 16 * 1024 * 1024

_COMMIT_LENGTH = 40
_PUBLIC_ARTIFACT_FILENAMES_V1 = tuple(row[0] for row in RELEASE_ARTIFACT_ROWS_V1)
_RELEASE_BUILD_CHECK_IDS_V1 = (
    "CANDIDATE_SOURCE_LOCK",
    "HEADLESS_ARTIFACTS",
    "DESKTOP_ARTIFACTS",
    "REPEAT_BUILD_REPRODUCIBILITY",
    "MANIFEST_LICENSE_PACK_ASSET_INVENTORY",
    "OFFLINE_INSTALLABILITY",
    "NO_DEVELOPER_DATA",
)
_RELEASE_STORE_CONFIGURATION_FILENAME_V1 = "clean-providers.toml"
_RELEASE_INDEX_STAGE_FILENAME_V1 = ".release-artifact-index.stage"
_RELEASE_POST_BUILD_FILE_NAMES_V1 = frozenset(
    {
        "clean-provider-linux-x86_64.json",
        "clean-provider-macos-arm64.json",
        "closeout-prerequisites.json",
    }
)
_RELEASE_POST_BUILD_DIRECTORY_NAMES_V1 = frozenset({"gate-evidence"})
_SUBORDINATE_SOURCE_CLASSES_V1 = frozenset(
    {
        ReleaseSourceClassV1.CANDIDATE_PROJECT_WHEEL,
        ReleaseSourceClassV1.LOCKED_DEPENDENCY_WHEEL,
        ReleaseSourceClassV1.CANDIDATE_STARTER_PACK,
        ReleaseSourceClassV1.CANDIDATE_ASSET,
    }
)
_LAUNCHER_PATHS_V1 = frozenset(
    {
        "release/launchers/headless/kirby2",
        "release/launchers/linux/kirby2",
        "release/launchers/macos/kirby2",
    }
)
_DOCUMENTATION_PATHS_V1 = (
    "docs/INSTRUCTOR_RESEARCH.md",
    "docs/LIMITATIONS.md",
    "docs/SCENARIO_AUTHORING.md",
    "docs/SECURITY_PRIVACY.md",
    "docs/TROUBLESHOOTING.md",
    "docs/USER_GUIDE.md",
)
_MICROSCOPE_ASSET_PATHS_V1 = (
    "kirby2/microscope/assets/report.css",
    "kirby2/microscope/assets/report.html",
    "kirby2/microscope/assets/report.js",
)


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 contract")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _commit(value: object, label: str) -> str:
    text = _text(value, label, 40)
    if len(text) != _COMMIT_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{label} must be forty lowercase hexadecimal characters")
    return text


@dataclass(frozen=True, slots=True)
class _ReleaseBuildCheckV1:
    check_id: str
    evidence_sha256: str
    status: str = "PASS"

    def __post_init__(self) -> None:
        if self.check_id not in _RELEASE_BUILD_CHECK_IDS_V1:
            raise ValueError("release build check ID differs from WO40-F")
        require_sha256(self.evidence_sha256, "release build check evidence digest")
        if self.status != "PASS":
            raise ValueError("published release build checks must pass")

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "evidence_sha256": self.evidence_sha256,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object) -> "_ReleaseBuildCheckV1":
        row = _exact(
            value,
            {"check_id", "evidence_sha256", "status"},
            "release build check",
        )
        return cls(
            check_id=_text(row["check_id"], "release build check ID", 128),
            evidence_sha256=_text(
                row["evidence_sha256"], "release build check evidence digest", 64
            ),
            status=_text(row["status"], "release build check status", 32),
        )


@dataclass(frozen=True, slots=True)
class _ReleaseArtifactObservationV1:
    artifact_id: str
    size: int
    transport_sha256: str
    embedded_manifest_sha256: str | None
    member_plan_sha256: str | None

    def __post_init__(self) -> None:
        _text(self.artifact_id, "build observation artifact ID", 128)
        _nonnegative(self.size, "build observation size")
        require_sha256(self.transport_sha256, "build observation transport digest")
        if self.embedded_manifest_sha256 is not None:
            require_sha256(
                self.embedded_manifest_sha256,
                "build observation embedded-manifest digest",
            )
        if self.member_plan_sha256 is not None:
            require_sha256(self.member_plan_sha256, "build observation plan digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "embedded_manifest_sha256": self.embedded_manifest_sha256,
            "member_plan_sha256": self.member_plan_sha256,
            "size": self.size,
            "transport_sha256": self.transport_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "_ReleaseArtifactObservationV1":
        row = _exact(
            value,
            {
                "artifact_id",
                "embedded_manifest_sha256",
                "member_plan_sha256",
                "size",
                "transport_sha256",
            },
            "build observation",
        )
        return cls(
            artifact_id=_text(row["artifact_id"], "build observation artifact ID", 128),
            size=_nonnegative(row["size"], "build observation size"),
            transport_sha256=_text(
                row["transport_sha256"], "build observation transport digest", 64
            ),
            embedded_manifest_sha256=(
                None
                if row["embedded_manifest_sha256"] is None
                else _text(
                    row["embedded_manifest_sha256"],
                    "build observation manifest digest",
                    64,
                )
            ),
            member_plan_sha256=(
                None
                if row["member_plan_sha256"] is None
                else _text(
                    row["member_plan_sha256"], "build observation plan digest", 64
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class _ReleaseBuildAttemptV1:
    attempt: int
    candidate_tree_projection_sha256: str
    artifact_index_sha256: str
    artifacts: tuple[_ReleaseArtifactObservationV1, ...]

    def __post_init__(self) -> None:
        if self.attempt not in range(1, RELEASE_BUILD_ATTEMPT_COUNT_V1 + 1):
            raise ValueError("release build attempt number is invalid")
        require_sha256(
            self.candidate_tree_projection_sha256,
            "attempt candidate-tree projection digest",
        )
        require_sha256(self.artifact_index_sha256, "attempt artifact-index digest")
        if tuple(item.artifact_id for item in self.artifacts) != _PUBLIC_ARTIFACT_FILENAMES_V1:
            raise ValueError("attempt artifact observations differ from the six-row protocol")
        expected_manifest_presence = tuple(row[3] for row in RELEASE_ARTIFACT_ROWS_V1)
        if (
            tuple(item.embedded_manifest_sha256 is not None for item in self.artifacts)
            != expected_manifest_presence
        ):
            raise ValueError("attempt embedded-manifest observations differ")
        if self.artifacts[4].member_plan_sha256 is not None:
            raise ValueError("standalone project wheel cannot claim an archive member plan")
        if any(
            item.member_plan_sha256 is None
            for item in (*self.artifacts[:4], self.artifacts[5])
        ):
            raise ValueError("canonical archives require member-plan observations")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_index_sha256": self.artifact_index_sha256,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "attempt": self.attempt,
            "candidate_tree_projection_sha256": self.candidate_tree_projection_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "_ReleaseBuildAttemptV1":
        row = _exact(
            value,
            {
                "artifact_index_sha256",
                "artifacts",
                "attempt",
                "candidate_tree_projection_sha256",
            },
            "release build attempt",
        )
        return cls(
            attempt=_nonnegative(row["attempt"], "release build attempt"),
            candidate_tree_projection_sha256=_text(
                row["candidate_tree_projection_sha256"],
                "attempt candidate-tree projection digest",
                64,
            ),
            artifact_index_sha256=_text(
                row["artifact_index_sha256"], "attempt artifact-index digest", 64
            ),
            artifacts=tuple(
                _ReleaseArtifactObservationV1.from_dict(item)
                for item in _array(row["artifacts"], "attempt artifacts")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseArtifactBuildRecordV1:
    candidate_commit: str
    candidate_tree: str
    source_date_epoch: int
    source_manifest_sha256: str
    candidate_tree_projection_sha256: str
    logical_build_id: str
    protocol_set_sha256: str
    resource_snapshot_sha256: str
    build_runtime_sha256: str
    network_policy_id: str
    attempts: tuple[_ReleaseBuildAttemptV1, ...]
    checks: tuple[_ReleaseBuildCheckV1, ...]
    artifact_index_sha256: str
    reproducible: bool

    schema_id: ClassVar[str] = RELEASE_BUILD_RECORD_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1
    policy_id: ClassVar[str] = RELEASE_ARTIFACT_BUILD_POLICY_ID_V1
    attempt_count: ClassVar[int] = RELEASE_BUILD_ATTEMPT_COUNT_V1

    def __post_init__(self) -> None:
        _commit(self.candidate_commit, "build-record candidate commit")
        _commit(self.candidate_tree, "build-record candidate tree")
        _nonnegative(self.source_date_epoch, "build-record source-date epoch")
        for value, label in (
            (self.source_manifest_sha256, "build-record source-manifest digest"),
            (
                self.candidate_tree_projection_sha256,
                "build-record candidate-tree projection digest",
            ),
            (self.protocol_set_sha256, "build-record protocol-set digest"),
            (self.resource_snapshot_sha256, "build-record resource-snapshot digest"),
            (self.build_runtime_sha256, "build-record runtime digest"),
            (self.artifact_index_sha256, "build-record artifact-index digest"),
        ):
            require_sha256(value, label)
        if not self.logical_build_id.startswith("kirby2-release-"):
            raise ValueError("build-record logical build ID has the wrong namespace")
        require_sha256(
            self.logical_build_id.removeprefix("kirby2-release-"),
            "build-record logical build digest",
        )
        if self.network_policy_id != RELEASE_NETWORK_POLICY_ID_V1:
            raise ValueError("build-record network policy differs")
        if tuple(item.attempt for item in self.attempts) != (1, 2):
            raise ValueError("build record must contain exactly two ordered attempts")
        if tuple(item.check_id for item in self.checks) != _RELEASE_BUILD_CHECK_IDS_V1:
            raise ValueError("build record WO40-F check inventory or order differs")
        if self.reproducible is not True:
            raise ValueError("a published build record must be reproducible")
        if any(
            item.candidate_tree_projection_sha256
            != self.candidate_tree_projection_sha256
            for item in self.attempts
        ):
            raise ValueError("attempt candidate-tree projections differ")
        if any(
            item.artifact_index_sha256 != self.artifact_index_sha256
            for item in self.attempts
        ):
            raise ValueError("attempt artifact indexes differ")
        left = tuple(item.as_dict() for item in self.attempts[0].artifacts)
        right = tuple(item.as_dict() for item in self.attempts[1].artifacts)
        if left != right:
            raise ValueError("attempt artifact observations are not reproducible")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_index_sha256": self.artifact_index_sha256,
            "attempt_count": self.attempt_count,
            "attempts": [item.as_dict() for item in self.attempts],
            "build_runtime_sha256": self.build_runtime_sha256,
            "candidate_commit": self.candidate_commit,
            "candidate_tree": self.candidate_tree,
            "candidate_tree_projection_sha256": self.candidate_tree_projection_sha256,
            "checks": [item.as_dict() for item in self.checks],
            "logical_build_id": self.logical_build_id,
            "network_policy_id": self.network_policy_id,
            "policy_id": self.policy_id,
            "protocol_set_sha256": self.protocol_set_sha256,
            "reproducible": self.reproducible,
            "resource_snapshot_sha256": self.resource_snapshot_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_date_epoch": self.source_date_epoch,
            "source_manifest_sha256": self.source_manifest_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseArtifactBuildRecordV1":
        value = load_canonical_json_bytes(raw, "ReleaseArtifactBuildRecordV1")
        row = _exact(
            value,
            {
                "artifact_index_sha256",
                "attempt_count",
                "attempts",
                "build_runtime_sha256",
                "candidate_commit",
                "candidate_tree",
                "candidate_tree_projection_sha256",
                "checks",
                "logical_build_id",
                "network_policy_id",
                "policy_id",
                "protocol_set_sha256",
                "reproducible",
                "resource_snapshot_sha256",
                "schema_id",
                "schema_version",
                "source_date_epoch",
                "source_manifest_sha256",
            },
            "release build record",
        )
        if (
            row["schema_id"] != cls.schema_id
            or row["schema_version"] != cls.schema_version
            or row["policy_id"] != cls.policy_id
            or row["attempt_count"] != cls.attempt_count
        ):
            raise ValueError("release build record identity differs")
        instance = cls(
            candidate_commit=_commit(row["candidate_commit"], "build-record candidate commit"),
            candidate_tree=_commit(row["candidate_tree"], "build-record candidate tree"),
            source_date_epoch=_nonnegative(
                row["source_date_epoch"], "build-record source-date epoch"
            ),
            source_manifest_sha256=_text(
                row["source_manifest_sha256"], "build-record source digest", 64
            ),
            candidate_tree_projection_sha256=_text(
                row["candidate_tree_projection_sha256"],
                "build-record candidate-tree digest",
                64,
            ),
            logical_build_id=_text(row["logical_build_id"], "logical build ID", 128),
            protocol_set_sha256=_text(
                row["protocol_set_sha256"], "build-record protocol digest", 64
            ),
            resource_snapshot_sha256=_text(
                row["resource_snapshot_sha256"], "build-record resource digest", 64
            ),
            build_runtime_sha256=_text(
                row["build_runtime_sha256"], "build-record runtime digest", 64
            ),
            network_policy_id=_text(
                row["network_policy_id"], "build-record network policy", 128
            ),
            attempts=tuple(
                _ReleaseBuildAttemptV1.from_dict(item)
                for item in _array(row["attempts"], "build-record attempts")
            ),
            checks=tuple(
                _ReleaseBuildCheckV1.from_dict(item)
                for item in _array(row["checks"], "build-record checks")
            ),
            artifact_index_sha256=_text(
                row["artifact_index_sha256"], "build-record index digest", 64
            ),
            reproducible=row["reproducible"],  # type: ignore[arg-type]
        )
        if instance.canonical_bytes() != raw:
            raise ValueError("release build record bytes are not canonical")
        return instance


@dataclass(frozen=True, slots=True)
class _BuiltArtifactV1:
    artifact_id: str
    artifact_form: str
    target: str
    raw: bytes
    embedded_manifest_sha256: str | None
    member_plan_sha256: str | None

    @property
    def transport_sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def observation(self) -> _ReleaseArtifactObservationV1:
        return _ReleaseArtifactObservationV1(
            artifact_id=self.artifact_id,
            size=len(self.raw),
            transport_sha256=self.transport_sha256,
            embedded_manifest_sha256=self.embedded_manifest_sha256,
            member_plan_sha256=self.member_plan_sha256,
        )

    def record(self) -> ReleaseArtifactRecordV1:
        return ReleaseArtifactRecordV1(
            artifact_id=self.artifact_id,
            artifact_form=self.artifact_form,
            target=self.target,
            size=len(self.raw),
            transport_sha256=self.transport_sha256,
            embedded_manifest_sha256=self.embedded_manifest_sha256,
        )


@dataclass(frozen=True, slots=True)
class _AttemptResultV1:
    artifacts: tuple[_BuiltArtifactV1, ...]
    index: ReleaseArtifactIndexV1
    candidate_tree_projection_sha256: str


@dataclass(frozen=True, slots=True)
class _PublicationReceiptV1:
    identity: tuple[int, ...]
    sha256: str

    def __post_init__(self) -> None:
        if type(self.identity) is not tuple or any(
            type(item) is not int for item in self.identity
        ):
            raise TypeError("publication receipt requires one integer identity")
        require_sha256(self.sha256, "publication receipt digest")


def _absolute_root(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute Path")
    if path != path.resolve(strict=False):
        raise ValueError(f"{label} must be supplied already resolved")
    return path


def _require_safe_store(root: Path, *, create: bool) -> tuple[int, int]:
    _absolute_root(root, "release artifact root")
    if create and not root.exists():
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    metadata = None
    for selected, label in (
        (root.parent, "release artifact parent"),
        (root, "release artifact root"),
    ):
        try:
            observed = selected.stat(follow_symlinks=False)
        except OSError as error:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                f"{label} cannot be inspected",
            ) from error
        if (
            not stat.S_ISDIR(observed.st_mode)
            or selected.is_symlink()
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                f"{label} must be one owned, non-writable real directory",
            )
        if selected == root:
            metadata = observed
    if metadata is None:  # pragma: no cover - loop invariant
        raise RuntimeError("release artifact root inspection omitted its metadata")
    return metadata.st_dev, metadata.st_ino


def _require_governed_store(
    bundle: ReleaseProtocolBundleV1,
    root: Path,
) -> DataPaths:
    literal_data_root = bundle.repository_root / ".kirby2"
    try:
        paths = DataPaths(literal_data_root)
        paths.validate(DataAreaId.RELEASE)
    except (OSError, TypeError, ValueError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "WO40-F data-path governance refused the release store",
        ) from error
    expected = paths.release
    if root != expected:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "WO40-F output is confined to the repository .kirby2/release store",
        )
    return paths


def _require_store_identity(root: Path, identity: tuple[int, int]) -> None:
    metadata = root.stat(follow_symlinks=False)
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or (
        metadata.st_dev,
        metadata.st_ino,
    ) != identity:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "release artifact root changed during execution",
        )


def _open_store_directory(root: Path, identity: tuple[int, int]) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "platform lacks O_NOFOLLOW directory support",
        )
    flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(root, flags)
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "release artifact root cannot be pinned",
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "pinned release artifact root identity differs",
        )
    return descriptor


def _store_inventory(root: Path, identity: tuple[int, int]) -> tuple[str, ...]:
    _require_store_identity(root, identity)
    try:
        with os.scandir(root) as entries:
            names = tuple(
                sorted((entry.name for entry in entries), key=lambda item: item.encode("utf-8"))
            )
    except (OSError, UnicodeEncodeError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "release artifact store inventory cannot be read",
        ) from error
    if any(
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or unicodedata.normalize("NFC", name) != name
        for name in names
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "release artifact store contains an invalid entry name",
        )
    folded = tuple(unicodedata.normalize("NFC", name.casefold()) for name in names)
    if len(folded) != len(set(folded)):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "release artifact store contains portable-name aliases",
        )
    _require_store_identity(root, identity)
    return names


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return content-relevant identity without metadata-only change time.

    macOS File Provider attaches provenance xattrs asynchronously.  That operation
    changes ``ctime`` without changing the file, so ctime cannot be a portable
    content-identity field.  The stable readers also compare the complete bytes;
    publication rollback additionally authenticates SHA-256 before unlinking.
    """

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _publication_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return _stable_file_identity(metadata)


def _require_unactivated_file(
    metadata: os.stat_result,
    name: str,
    *,
    link_counts: frozenset[int] = frozenset({1}),
    allow_empty: bool = False,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink not in link_counts
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size < (0 if allow_empty else 1)
        or metadata.st_size > RELEASE_ARTIFACT_MAX_BYTES_V1
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            f"unactivated release file metadata is unsafe: {name}",
        )


def _recover_unactivated_store(
    root: Path,
    identity: tuple[int, int],
    public_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Remove only owned pre-activation residue while the build lock is held."""

    inventory = _store_inventory(root, identity)
    directory = _open_store_directory(root, identity)
    changed = False
    try:
        for name in inventory:
            if not name.startswith((".wo40f-attempt-1-", ".wo40f-attempt-2-")):
                continue
            suffix = name.rsplit("-", 1)[-1]
            path = root / name
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError as error:
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                    "private build-attempt residue cannot be inspected",
                ) from error
            if (
                len(suffix) < 6
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in suffix)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or path.is_symlink()
                or path.resolve(strict=True) != path
            ):
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                    "private build-attempt residue is unsafe",
                )
            shutil.rmtree(path)
            changed = True

        inventory = _store_inventory(root, identity)
        if _RELEASE_INDEX_STAGE_FILENAME_V1 in inventory:
            stage = os.stat(
                _RELEASE_INDEX_STAGE_FILENAME_V1,
                dir_fd=directory,
                follow_symlinks=False,
            )
            if RELEASE_ARTIFACT_INDEX_FILENAME_V1 in inventory:
                final = os.stat(
                    RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                _require_unactivated_file(
                    stage,
                    _RELEASE_INDEX_STAGE_FILENAME_V1,
                    link_counts=frozenset({2}),
                )
                _require_unactivated_file(
                    final,
                    RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                    link_counts=frozenset({2}),
                )
                if (stage.st_dev, stage.st_ino) != (final.st_dev, final.st_ino):
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                        "staged and activated release indexes are not aliases",
                    )
            else:
                _require_unactivated_file(
                    stage,
                    _RELEASE_INDEX_STAGE_FILENAME_V1,
                    allow_empty=True,
                )
            os.unlink(_RELEASE_INDEX_STAGE_FILENAME_V1, dir_fd=directory)
            changed = True

        inventory = _store_inventory(root, identity)
        if RELEASE_ARTIFACT_INDEX_FILENAME_V1 not in inventory:
            partial = tuple(name for name in public_names if name in inventory)
            identities: dict[str, tuple[int, ...]] = {}
            for name in partial:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _require_unactivated_file(metadata, name, allow_empty=True)
                identities[name] = _publication_identity(metadata)
            for name in partial:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _publication_identity(current) != identities[name]:
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                        f"unactivated release file changed before cleanup: {name}",
                    )
            for name in reversed(partial):
                os.unlink(name, dir_fd=directory)
                changed = True
        if changed:
            os.fsync(directory)
    except ReleaseBuildRefused:
        raise
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "unactivated release transaction recovery failed",
        ) from error
    finally:
        os.close(directory)
    return _store_inventory(root, identity)


def _validate_post_build_entries(
    root: Path,
    identity: tuple[int, int],
    inventory: tuple[str, ...],
) -> None:
    directory = _open_store_directory(root, identity)
    try:
        for name in _RELEASE_POST_BUILD_FILE_NAMES_V1.intersection(inventory):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or metadata.st_size <= 0
                or metadata.st_size > RELEASE_RECORD_MAX_BYTES_V1
            ):
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                    f"post-build release file is unsafe: {name}",
                )
        for name in _RELEASE_POST_BUILD_DIRECTORY_NAMES_V1.intersection(inventory):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                    f"post-build release directory is unsafe: {name}",
                )
    finally:
        os.close(directory)


def _stable_regular_read(
    path: Path | str,
    *,
    maximum_bytes: int,
    require_read_only: bool,
    directory_fd: int | None = None,
    identity_out: list[tuple[int, ...]] | None = None,
) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "platform lacks O_NOFOLLOW read support",
        )
    flags = (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags, dir_fd=directory_fd)
    except OSError as error:
        name = Path(os.fspath(path)).name
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            f"release store file cannot be opened safely: {name}",
        ) from error
    try:
        before = os.fstat(descriptor)
        permissions = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or permissions & 0o022
            or (require_read_only and permissions != 0o444)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                f"release store file metadata is unsafe: {Path(os.fspath(path)).name}",
            )
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("release store file ended during stable read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError("release store file grew during stable read")
        after = os.fstat(descriptor)
        if _stable_file_identity(before) != _stable_file_identity(after):
            raise OSError("release store file changed during stable read")
        if identity_out is not None:
            identity_out.append(_stable_file_identity(before))
        return b"".join(chunks)
    except ReleaseBuildRefused:
        raise
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            f"release store file changed during read: {Path(os.fspath(path)).name}",
        ) from error
    finally:
        os.close(descriptor)


def _candidate_files(
    bundle: ReleaseProtocolBundleV1,
    candidate_commit: str,
) -> tuple[dict[str, str], dict[str, bytes], str]:
    objects = _candidate_tree_objects(bundle.repository_root, candidate_commit)
    by_object = _candidate_blob_bytes(
        bundle.repository_root,
        tuple(objects[path] for path in sorted(objects, key=lambda item: item.encode("utf-8"))),
    )
    files = {path: by_object[object_id] for path, object_id in objects.items()}
    rows = [
        {"path": path, "sha256": hashlib.sha256(files[path]).hexdigest()}
        for path in sorted(files, key=lambda item: item.encode("utf-8"))
    ]
    projection_sha256 = hashlib.sha256(canonical_json_bytes(rows)).hexdigest()
    return objects, files, projection_sha256


def _require_candidate_version(files: Mapping[str, bytes]) -> None:
    try:
        pyproject = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
        project_version = pyproject["project"]["version"]
        module = ast.parse(files["kirby2/__init__.py"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError, SyntaxError) as error:
        raise ValueError("candidate release version sources are invalid") from error
    module_versions = tuple(
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is str
    )
    if project_version != RELEASE_VERSION_V1 or module_versions != (RELEASE_VERSION_V1,):
        raise ValueError("candidate project and package versions differ from release 0.1.0")


def _wheel_api():
    try:
        from .wheels import (  # type: ignore[attr-defined]
            resolve_offline_wheelhouse,
            verify_project_wheel,
        )
    except ImportError as error:
        raise RuntimeError("release wheel verification API is unavailable") from error
    return verify_project_wheel, resolve_offline_wheelhouse


def _setuptools_version(runtime: ReleaseBuildRuntimeSnapshotV1) -> str:
    selected = tuple(item.version for item in runtime.distributions if item.name == "setuptools")
    if len(selected) != 1:
        raise ValueError("build runtime does not bind one exact setuptools distribution")
    return selected[0]


def _preflight(
    bundle: ReleaseProtocolBundleV1,
    candidate_commit: str,
    *,
    require_checkout: bool,
) -> tuple[ReleaseCandidateInputsV1, object]:
    candidate = verify_release_candidate_inputs(
        bundle,
        candidate_commit,
        require_checkout=require_checkout,
    )
    provider = bundle.repository_root / ".kirby2/release/clean-providers.toml"
    preflight = release_resource_preflight(
        bundle,
        wheelhouse_root=(bundle.repository_root / "release/wheelhouse").resolve(),
        provider_inventory=provider.resolve() if provider.is_file() else None,
    )
    report = preflight.markdown().encode("utf-8")
    tracked_report = bundle.repository_root / "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
    if (
        preflight.status != "PASS"
        or preflight.build_runtime is None
        or not tracked_report.is_file()
        or tracked_report.read_bytes() != report
        or hashlib.sha256(report).hexdigest() != candidate.resource_preflight_sha256
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE,
            "live offline resources differ from the candidate passing preflight",
        )
    return candidate, preflight


def _require_codex_network_seatbelt() -> None:
    if (
        os.environ.get("CODEX_SANDBOX") != "seatbelt"
        or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") != "1"
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.NETWORK_POLICY_MISMATCH,
            "WO40-F execution requires the active Codex seatbelt network denial",
        )


def _new_private_attempt_root(
    repository_root: Path,
    artifact_root: Path,
    number: int,
) -> Path:
    """Create one owner-only attempt outside source and governed output trees."""

    created = Path(
        tempfile.mkdtemp(prefix=f"kirby2-wo40f-attempt-{number}-")
    )
    try:
        root = created.resolve(strict=True)
        metadata = root.stat(follow_symlinks=False)
        forbidden = (repository_root, artifact_root)
        if (
            root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or any(root == selected or selected in root.parents for selected in forbidden)
        ):
            raise OSError("private attempt root is not isolated")
        return root
    except Exception as error:
        try:
            os.rmdir(created)
        except OSError:
            pass
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "a private build-attempt root could not be isolated from source and output",
        ) from error


def _materialize_candidate(root: Path, files: Mapping[str, bytes]) -> None:
    for relative in sorted(files, key=lambda item: item.encode("utf-8")):
        normalize_release_path(relative, label="materialized candidate path")
        destination = root / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(files[relative])
        destination.chmod(0o755 if relative in _LAUNCHER_PATHS_V1 else 0o644)


def _require_materialized_candidate_intact(
    root: Path,
    files: Mapping[str, bytes],
) -> None:
    for relative in sorted(files, key=lambda item: item.encode("utf-8")):
        destination = root / relative
        try:
            if destination.resolve(strict=True) != destination:
                raise OSError("candidate path was rebound through a symlink")
            metadata = destination.stat(follow_symlinks=False)
        except OSError as error:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.CANDIDATE_INPUT_DRIFT,
                f"materialized candidate path changed: {relative}",
            ) from error
        expected_mode = 0o755 if relative in _LAUNCHER_PATHS_V1 else 0o644
        if (
            destination.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_size != len(files[relative])
            or destination.read_bytes() != files[relative]
        ):
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.CANDIDATE_INPUT_DRIFT,
                f"materialized candidate bytes changed: {relative}",
            )


def _build_project_wheel(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    runtime: ReleaseBuildRuntimeSnapshotV1,
    files: Mapping[str, bytes],
    attempt_root: Path,
):
    verify_project_wheel, _resolve_offline_wheelhouse = _wheel_api()
    source_root = attempt_root / "candidate"
    dist_root = attempt_root / "dist"
    temporary_root = attempt_root / "tmp"
    home_root = attempt_root / "home"
    for path in (source_root, dist_root, temporary_root, home_root):
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    _materialize_candidate(source_root, files)
    frontend = (bundle.repository_root / ".venv/bin/pip").resolve(strict=True)
    declared = bundle.artifact_layout.build_frontends["project_wheel"]
    if declared != [
        "./.venv/bin/pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
    ]:
        raise ValueError("project-wheel frontend differs from the frozen protocol")
    environment = {
        "CODEX_SANDBOX": "seatbelt",
        "CODEX_SANDBOX_NETWORK_DISABLED": "1",
        "HOME": os.fspath(home_root),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "PATH": os.fspath(frontend.parent),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INPUT": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "SOURCE_DATE_EPOCH": str(candidate.source_date_epoch),
        "TMPDIR": os.fspath(temporary_root),
        "TZ": "UTC",
        "XDG_CACHE_HOME": os.fspath(attempt_root / "cache"),
    }
    arguments = [
        os.fspath(frontend),
        *declared[1:],
        "--wheel-dir",
        os.fspath(dist_root),
        ".",
    ]
    try:
        process = subprocess.Popen(
            arguments,
            cwd=source_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        returncode = process.wait(timeout=RELEASE_BUILD_TIMEOUT_SECONDS_V1)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.BUILD_ATTEMPT_TIMEOUT,
            "project-wheel frontend exceeded its fixed execution timeout",
        ) from error
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.BUILD_ATTEMPT_FAILED,
            "project-wheel frontend could not be started",
        ) from error
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.killpg(process.pid, signal.SIGKILL)
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.BUILD_ATTEMPT_FAILED,
            "project-wheel frontend left a background process",
        )
    _require_materialized_candidate_intact(source_root, files)
    if returncode != 0:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.BUILD_ATTEMPT_FAILED,
            f"project-wheel frontend failed with exit status {returncode}",
        )
    outputs = tuple(dist_root.iterdir())
    if (
        len(outputs) != 1
        or outputs[0].name != RELEASE_PROJECT_WHEEL_FILENAME_V1
        or outputs[0].is_symlink()
        or not outputs[0].is_file()
    ):
        raise ValueError("project-wheel frontend emitted an unexpected output set")
    raw = _stable_regular_read(
        outputs[0],
        maximum_bytes=RELEASE_ARTIFACT_MAX_BYTES_V1,
        require_read_only=False,
    )
    return verify_project_wheel(
        raw,
        filename=RELEASE_PROJECT_WHEEL_FILENAME_V1,
        candidate_files=files,
        pyproject_bytes=files["pyproject.toml"],
        source_date_epoch=candidate.source_date_epoch,
        backend_version=_setuptools_version(runtime),
        requirements_lock=bundle.requirements_lock,
    )


def _expand_candidate_member(
    source_path: str,
    archive_path: str,
    files: Mapping[str, bytes],
) -> tuple[tuple[str, str, bytes], ...]:
    marker = "{filename}"
    if marker not in source_path and marker not in archive_path:
        if source_path not in files:
            raise ValueError(f"candidate member is absent: {source_path}")
        return ((source_path, archive_path, files[source_path]),)
    if not source_path.endswith(marker) or not archive_path.endswith(marker):
        raise ValueError("layout filename placeholders must be terminal and paired")
    source_prefix = source_path.removesuffix(marker)
    archive_prefix = archive_path.removesuffix(marker)
    selected: list[tuple[str, str, bytes]] = []
    for path in sorted(files, key=lambda item: item.encode("utf-8")):
        if not path.startswith(source_prefix):
            continue
        filename = path.removeprefix(source_prefix)
        if not filename or "/" in filename:
            continue
        selected.append((path, archive_prefix + filename, files[path]))
    if not selected:
        raise ValueError(f"layout filename expansion is empty: {source_path}")
    selected_paths = tuple(item[0] for item in selected)
    if source_path == "docs/{filename}" and selected_paths != _DOCUMENTATION_PATHS_V1:
        raise ValueError("candidate user-documentation inventory differs")
    if (
        source_path == "kirby2/microscope/assets/{filename}"
        and selected_paths != _MICROSCOPE_ASSET_PATHS_V1
    ):
        raise ValueError("candidate microscope-asset inventory differs")
    return tuple(selected)


def _schema_versions(candidate: ReleaseCandidateInputsV1) -> tuple[ReleaseSchemaVersionV1, ...]:
    inventory = builtin_release_schema_inventory(
        source_revision=candidate.candidate_commit,
        source_sha256=candidate.source_manifest_sha256,
    )
    return tuple(
        sorted(
            (
                ReleaseSchemaVersionV1(
                    schema_id=item.schema_id,
                    version=item.current_version,
                )
                for item in inventory.schemas
            ),
            key=lambda item: item.schema_id.encode("utf-8"),
        )
    )


def _starter_entries(starter: ReleaseStarterSetV1) -> tuple[ReleaseStarterEntryManifestV1, ...]:
    return tuple(
        ReleaseStarterEntryManifestV1(
            role=item.role.value,
            manifest_path=item.manifest_path,
            manifest_sha256=item.manifest_sha256,
            pack_id=item.pack_id,
        )
        for item in starter.entries
    )


def _known_limitations() -> tuple[ReleaseKnownLimitationV1, ...]:
    return tuple(
        ReleaseKnownLimitationV1(code=code, detail=detail)
        for code, detail in RELEASE_REQUIRED_KNOWN_LIMITATIONS_V1
    )


def _build_source_archive(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    files: Mapping[str, bytes],
) -> _BuiltArtifactV1:
    layout = next(
        item
        for item in bundle.artifact_layout.artifacts
        if item.artifact_id == "source-archive"
    )
    if layout.archive_root is None:
        raise ValueError("source archive root is absent")
    members = tuple(
        ArchiveMemberPlanV1(
            path=path,
            payload=files[path],
            source_class=ReleaseSourceClassV1.CANDIDATE_SOURCE,
        )
        for path in sorted(files, key=lambda item: item.encode("utf-8"))
    )
    archive = build_canonical_release_archive(
        layout.archive_root,
        members,
        source_date_epoch=candidate.source_date_epoch,
    )
    return _BuiltArtifactV1(
        artifact_id=layout.artifact_id,
        artifact_form=layout.artifact_form,
        target=layout.target,
        raw=archive.gzip_bytes,
        embedded_manifest_sha256=None,
        member_plan_sha256=archive.member_plan_sha256,
    )


def _generated_plan(
    *,
    path: str,
    payload: bytes,
    source_class: ReleaseSourceClassV1,
    encoder_id: str,
    input_digests: tuple[str, ...],
) -> ArchiveMemberPlanV1:
    return ArchiveMemberPlanV1(
        path=path,
        payload=payload,
        source_class=source_class,
        encoder_id=encoder_id,
        input_digests=input_digests,
    )


def _build_bundle(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    runtime: ReleaseBuildRuntimeSnapshotV1,
    files: Mapping[str, bytes],
    project_wheel,
    locked_wheels: tuple[object, ...],
    starter: ReleaseStarterSetV1,
    *,
    artifact_id: str,
) -> _BuiltArtifactV1:
    artifact = next(
        item
        for item in bundle.artifact_layout.artifacts
        if item.artifact_id == artifact_id
    )
    if not artifact.embedded_manifest or artifact.archive_root is None:
        raise ValueError("bundle artifact must declare one embedded manifest and root")
    target_by_id = {item.target_id: item for item in bundle.platform_protocol.targets}
    platform_target = target_by_id[artifact.target]
    locked_by_target = {item.target: item for item in locked_wheels}
    locked = locked_by_target[artifact.target]
    starter_by_pack = {
        entry.pack_id: build
        for entry, build in zip(starter.entries, starter.builds, strict=True)
    }
    layout_bytes = canonical_json_bytes(bundle.artifact_layout.as_dict())
    license_bytes = release_license_inventory_bytes(bundle.requirements_lock)
    license_payloads = {
        item.locked.license_sha256: item.license_bytes for item in locked_wheels
    }
    notices_bytes = release_notices_bytes(bundle.requirements_lock, license_payloads)
    protocol_sha = {item.path: item.sha256 for item in candidate.protocol_files}

    planned: list[tuple[str, ArchiveMemberPlanV1, str | None, bool]] = []
    manifest_layout_rows = []
    for member in bundle.artifact_layout.members:
        if artifact_id not in member.artifact_ids:
            continue
        source_class = member.source_class
        if source_class is ReleaseSourceClassV1.GENERATED_MANIFEST:
            manifest_layout_rows.append(member)
            continue
        if source_class is ReleaseSourceClassV1.CANDIDATE_PROJECT_WHEEL:
            rows = ((member.source_path, member.archive_path, project_wheel.wheel_bytes),)
        elif source_class is ReleaseSourceClassV1.LOCKED_DEPENDENCY_WHEEL:
            if Path(member.source_path).name != locked.filename:
                raise ValueError("bundle dependency member differs from its target lock")
            rows = ((member.source_path, member.archive_path, locked.wheel_bytes),)
        elif source_class in {
            ReleaseSourceClassV1.CANDIDATE_LAUNCHER,
            ReleaseSourceClassV1.CANDIDATE_DOCUMENTATION,
            ReleaseSourceClassV1.CANDIDATE_ASSET,
        }:
            rows = _expand_candidate_member(member.source_path, member.archive_path, files)
        elif source_class is ReleaseSourceClassV1.CANDIDATE_STARTER_PACK:
            pack_id = Path(member.archive_path).name.removesuffix(".k2pack")
            build = starter_by_pack.get(pack_id)
            if build is None:
                raise ValueError("bundle starter member differs from the starter set")
            rows = ((member.source_path, member.archive_path, build.archive_bytes),)
        elif source_class is ReleaseSourceClassV1.GENERATED_LAYOUT:
            rows = ((member.source_path, member.archive_path, layout_bytes),)
        elif source_class is ReleaseSourceClassV1.GENERATED_LICENSE:
            rows = ((member.source_path, member.archive_path, license_bytes),)
        elif source_class is ReleaseSourceClassV1.GENERATED_NOTICE:
            rows = ((member.source_path, member.archive_path, notices_bytes),)
        else:
            raise ValueError(f"unsupported bundle source class: {source_class.value}")
        for _source_path, archive_path, payload in rows:
            if source_class is ReleaseSourceClassV1.GENERATED_LAYOUT:
                plan = _generated_plan(
                    path=archive_path,
                    payload=payload,
                    source_class=source_class,
                    encoder_id=RELEASE_ARTIFACT_LAYOUT_SCHEMA_ID_V1,
                    input_digests=(protocol_sha["release/artifact_layout.toml"],),
                )
            elif source_class is ReleaseSourceClassV1.GENERATED_LICENSE:
                plan = _generated_plan(
                    path=archive_path,
                    payload=payload,
                    source_class=source_class,
                    encoder_id=RELEASE_LICENSE_INVENTORY_SCHEMA_ID_V1,
                    input_digests=(protocol_sha["release/requirements.lock"],),
                )
            elif source_class is ReleaseSourceClassV1.GENERATED_NOTICE:
                plan = _generated_plan(
                    path=archive_path,
                    payload=payload,
                    source_class=source_class,
                    encoder_id=RELEASE_NOTICES_ENCODER_ID_V1,
                    input_digests=(
                        protocol_sha["release/requirements.lock"],
                        *tuple(sorted(license_payloads)),
                    ),
                )
            else:
                plan = ArchiveMemberPlanV1(
                    path=archive_path,
                    payload=payload,
                    source_class=source_class,
                )
            subordinate_id = None
            if source_class in _SUBORDINATE_SOURCE_CLASSES_V1:
                subordinate_id = (
                    archive_path
                    if source_class is ReleaseSourceClassV1.CANDIDATE_ASSET
                    else member.member_id
                )
            planned.append(
                (
                    member.member_id,
                    plan,
                    subordinate_id,
                    source_class is ReleaseSourceClassV1.CANDIDATE_ASSET,
                )
            )
    if (
        len(manifest_layout_rows) != 1
        or manifest_layout_rows[0].archive_path != "RELEASE_MANIFEST.json"
    ):
        raise ValueError("bundle must reserve one exact manifest member")
    planned.sort(key=lambda item: item[1].path.encode("utf-8"))
    paths = tuple(item[1].path for item in planned)
    if len(paths) != len(set(paths)):
        raise ValueError("bundle member paths are not unique")
    payload_members = tuple(
        ReleasePayloadMemberV1(
            path=plan.path,
            size=plan.size,
            sha256=plan.sha256,
            source_class=plan.source_class.value,
        )
        for _member_id, plan, _subordinate, _asset in planned
    )
    assets = tuple(
        ReleaseAssetV1(path=plan.path, size=plan.size, sha256=plan.sha256)
        for _member_id, plan, _subordinate, is_asset in planned
        if is_asset
    )
    subordinate = tuple(
        sorted(
            (
                ReleaseSubordinateArtifactV1(
                    artifact_id=subordinate_id,
                    size=plan.size,
                    sha256=plan.sha256,
                )
                for _member_id, plan, subordinate_id, _asset in planned
                if subordinate_id is not None
            ),
            key=lambda item: item.artifact_id.encode("utf-8"),
        )
    )
    dependencies = (
        ReleaseDependencyV1(
            name=locked.locked.name,
            version=locked.locked.version,
            wheel_filename=locked.locked.filename,
            wheel_sha256=locked.locked.sha256,
            license_id=locked.locked.license_id,
        ),
    )
    target = ReleaseTargetV1(
        system=platform_target.system,
        machine=platform_target.machine,
        artifact_form=artifact.artifact_form,
    )
    build_timestamp = datetime.fromtimestamp(
        candidate.source_date_epoch, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = ReleaseManifestV1(
        release_version=RELEASE_VERSION_V1,
        candidate_commit=candidate.candidate_commit,
        build_timestamp=build_timestamp,
        target=target,
        runtime=runtime.runtime,
        dependencies=dependencies,
        schema_versions=_schema_versions(candidate),
        starter_set_id=RELEASE_STARTER_SET_ID_V1,
        starter_entries_sha256=starter.entries_sha256,
        starter_entries=_starter_entries(starter),
        assets=assets,
        known_limitations=_known_limitations(),
        license_inventory_sha256=hashlib.sha256(license_bytes).hexdigest(),
        notices_sha256=hashlib.sha256(notices_bytes).hexdigest(),
        artifact_layout_sha256=hashlib.sha256(layout_bytes).hexdigest(),
        archive_root=artifact.archive_root,
        logical_build_id=candidate.logical_build_id,
        payload_members=payload_members,
        subordinate_artifacts=subordinate,
    )
    manifest_input = hashlib.sha256(
        canonical_json_bytes(
            {
                "assets": [item.as_dict() for item in assets],
                "build_timestamp": build_timestamp,
                "candidate_commit": candidate.candidate_commit,
                "dependencies": [item.as_dict() for item in dependencies],
                "known_limitations": [item.as_dict() for item in _known_limitations()],
                "layout_sha256": hashlib.sha256(layout_bytes).hexdigest(),
                "license_sha256": hashlib.sha256(license_bytes).hexdigest(),
                "logical_build_id": candidate.logical_build_id,
                "notices_sha256": hashlib.sha256(notices_bytes).hexdigest(),
                "payload_members": [item.as_dict() for item in payload_members],
                "release_version": RELEASE_VERSION_V1,
                "runtime_snapshot_sha256": runtime.logical_sha256,
                "schema_versions": [item.as_dict() for item in _schema_versions(candidate)],
                "source_date_epoch": candidate.source_date_epoch,
                "starter_entries": [item.as_dict() for item in _starter_entries(starter)],
                "subordinate_artifacts": [item.as_dict() for item in subordinate],
                "target": target.as_dict(),
            }
        )
    ).hexdigest()
    manifest_plan = _generated_plan(
        path="RELEASE_MANIFEST.json",
        payload=manifest.canonical_bytes(),
        source_class=ReleaseSourceClassV1.GENERATED_MANIFEST,
        encoder_id=RELEASE_MANIFEST_SCHEMA_ID_V1,
        input_digests=(manifest_input,),
    )
    relative_members = tuple(item[1] for item in planned) + (manifest_plan,)
    archive = build_canonical_release_archive(
        artifact.archive_root,
        relative_members,
        source_date_epoch=candidate.source_date_epoch,
    )
    return _BuiltArtifactV1(
        artifact_id=artifact.artifact_id,
        artifact_form=artifact.artifact_form,
        target=artifact.target,
        raw=archive.gzip_bytes,
        embedded_manifest_sha256=manifest.sha256,
        member_plan_sha256=archive.member_plan_sha256,
    )


def _assemble_release(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    runtime: ReleaseBuildRuntimeSnapshotV1,
    files: Mapping[str, bytes],
    project_wheel,
    locked_wheels: tuple[object, ...],
    starter: ReleaseStarterSetV1,
) -> tuple[tuple[_BuiltArtifactV1, ...], ReleaseArtifactIndexV1]:
    by_id: dict[str, _BuiltArtifactV1] = {}
    by_id["project-wheel"] = _BuiltArtifactV1(
        artifact_id="project-wheel",
        artifact_form="PY3_NONE_ANY_WHEEL",
        target="any",
        raw=project_wheel.wheel_bytes,
        embedded_manifest_sha256=None,
        member_plan_sha256=None,
    )
    by_id["source-archive"] = _build_source_archive(bundle, candidate, files)
    for artifact_id in _PUBLIC_ARTIFACT_FILENAMES_V1[:4]:
        by_id[artifact_id] = _build_bundle(
            bundle,
            candidate,
            runtime,
            files,
            project_wheel,
            locked_wheels,
            starter,
            artifact_id=artifact_id,
        )
    artifacts = tuple(by_id[artifact_id] for artifact_id in _PUBLIC_ARTIFACT_FILENAMES_V1)
    index = ReleaseArtifactIndexV1(
        candidate_commit=candidate.candidate_commit,
        logical_build_id=candidate.logical_build_id,
        artifacts=tuple(item.record() for item in artifacts),
    )
    return artifacts, index


def _build_attempt(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    runtime: ReleaseBuildRuntimeSnapshotV1,
    files: Mapping[str, bytes],
    attempt_root: Path,
) -> _AttemptResultV1:
    project_wheel = _build_project_wheel(bundle, candidate, runtime, files, attempt_root)
    _verify_project_wheel, resolve_offline_wheelhouse = _wheel_api()
    locked_wheels = resolve_offline_wheelhouse(
        (bundle.repository_root / "release/wheelhouse").resolve(),
        bundle.requirements_lock,
    )
    starter = build_release_starter_set()
    if starter.layout_dict() != bundle.artifact_layout.starter_set:
        raise ValueError("release starter set differs from the frozen artifact layout")
    artifacts, index = _assemble_release(
        bundle,
        candidate,
        runtime,
        files,
        project_wheel,
        locked_wheels,
        starter,
    )
    _objects, _files, projection = _candidate_files(bundle, candidate.candidate_commit)
    return _AttemptResultV1(
        artifacts=artifacts,
        index=index,
        candidate_tree_projection_sha256=projection,
    )


def _attempt_record(number: int, result: _AttemptResultV1) -> _ReleaseBuildAttemptV1:
    return _ReleaseBuildAttemptV1(
        attempt=number,
        candidate_tree_projection_sha256=result.candidate_tree_projection_sha256,
        artifact_index_sha256=result.index.sha256,
        artifacts=tuple(item.observation() for item in result.artifacts),
    )


def _release_build_checks(
    bundle: ReleaseProtocolBundleV1,
    candidate: ReleaseCandidateInputsV1,
    runtime: ReleaseBuildRuntimeSnapshotV1,
    resource_snapshot_sha256: str,
    attempts: tuple[_ReleaseBuildAttemptV1, ...],
    selected: _AttemptResultV1,
) -> tuple[_ReleaseBuildCheckV1, ...]:
    observations = {
        item.artifact_id: item.observation().as_dict() for item in selected.artifacts
    }

    def evidence(check_id: str, proof: object) -> _ReleaseBuildCheckV1:
        payload = {
            "candidate_commit": candidate.candidate_commit,
            "check_id": check_id,
            "proof": proof,
            "protocol_set_sha256": candidate.protocol_set_sha256,
        }
        return _ReleaseBuildCheckV1(
            check_id=check_id,
            evidence_sha256=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    headless_selectors = {
        selector: [observations[item] for item in RELEASE_ARTIFACT_SELECTORS_V1[selector]]
        for selector in ("linux-x86_64/headless", "macos-arm64/headless")
    }
    desktop_selectors = {
        selector: [observations[item] for item in RELEASE_ARTIFACT_SELECTORS_V1[selector]]
        for selector in ("linux-x86_64/desktop", "macos-arm64/desktop")
    }
    manifest_inventory = [
        {
            "artifact_id": artifact_id,
            "embedded_manifest_sha256": observations[artifact_id][
                "embedded_manifest_sha256"
            ],
            "member_plan_sha256": observations[artifact_id]["member_plan_sha256"],
        }
        for artifact_id in _PUBLIC_ARTIFACT_FILENAMES_V1
    ]
    proof_by_id: dict[str, object] = {
        "CANDIDATE_SOURCE_LOCK": {
            "candidate_tree": candidate.candidate_tree,
            "candidate_tree_projection_sha256": selected.candidate_tree_projection_sha256,
            "source_entry_count": candidate.source_entry_count,
            "source_lock_object_id": candidate.source_lock_object_id,
            "source_lock_sha256": candidate.source_lock_sha256,
            "source_manifest_sha256": candidate.source_manifest_sha256,
        },
        "HEADLESS_ARTIFACTS": headless_selectors,
        "DESKTOP_ARTIFACTS": desktop_selectors,
        "REPEAT_BUILD_REPRODUCIBILITY": {
            "attempt_count": RELEASE_BUILD_ATTEMPT_COUNT_V1,
            "attempts": [item.as_dict() for item in attempts],
        },
        "MANIFEST_LICENSE_PACK_ASSET_INVENTORY": manifest_inventory,
        "OFFLINE_INSTALLABILITY": {
            "build_runtime_sha256": runtime.logical_sha256,
            "network_policy_id": RELEASE_NETWORK_POLICY_ID_V1,
            "requirements_lock_sha256": bundle.requirements_lock.logical_sha256,
            "resource_snapshot_sha256": resource_snapshot_sha256,
            "wheel_artifacts": [
                observations[item]
                for item in (
                    "project-wheel",
                    "linux-x86_64-wheelhouse",
                    "macos-arm64-wheelhouse",
                )
            ],
        },
        "NO_DEVELOPER_DATA": {
            "allowlist_policy": "EXACT_CANONICAL_MEMBER_PLANS_V1",
            "artifact_member_plans": manifest_inventory,
            "candidate_tree_projection_sha256": selected.candidate_tree_projection_sha256,
        },
    }
    return tuple(evidence(check_id, proof_by_id[check_id]) for check_id in _RELEASE_BUILD_CHECK_IDS_V1)


def _same_attempt_bytes(left: _AttemptResultV1, right: _AttemptResultV1) -> bool:
    return (
        left.candidate_tree_projection_sha256 == right.candidate_tree_projection_sha256
        and left.index.canonical_bytes() == right.index.canonical_bytes()
        and tuple((item.artifact_id, item.raw) for item in left.artifacts)
        == tuple((item.artifact_id, item.raw) for item in right.artifacts)
        and tuple(item.observation().as_dict() for item in left.artifacts)
        == tuple(item.observation().as_dict() for item in right.artifacts)
    )


def _write_exclusive(directory_fd: int, name: str, raw: bytes) -> tuple[int, ...]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "platform lacks O_NOFOLLOW publication support",
        )
    descriptor = os.open(
        name,
        flags | nofollow | getattr(os, "O_CLOEXEC", 0),
        0o444,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("exclusive publication write made no progress")
            view = view[written:]
        os.fchmod(descriptor, 0o444)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or metadata.st_size != len(raw)
        ):
            raise OSError("exclusive publication metadata differs")
        os.fsync(descriptor)
        return _publication_identity(os.fstat(descriptor))
    except Exception:
        try:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _rollback_publications_fd(
    directory: int,
    receipts: Mapping[str, _PublicationReceiptV1],
) -> None:
    for name, receipt in receipts.items():
        identities: list[tuple[int, ...]] = []
        raw = _stable_regular_read(
            name,
            maximum_bytes=RELEASE_ARTIFACT_MAX_BYTES_V1,
            require_read_only=True,
            directory_fd=directory,
            identity_out=identities,
        )
        if (
            identities != [receipt.identity]
            or hashlib.sha256(raw).hexdigest() != receipt.sha256
        ):
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                f"published release file changed before rollback: {name}",
            )
    for name in reversed(tuple(receipts)):
        os.unlink(name, dir_fd=directory)
    os.fsync(directory)


def _activate_index(directory: int, raw: bytes) -> tuple[int, ...]:
    stage_identity = _write_exclusive(
        directory,
        _RELEASE_INDEX_STAGE_FILENAME_V1,
        raw,
    )
    linked = False
    try:
        os.link(
            _RELEASE_INDEX_STAGE_FILENAME_V1,
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        linked = True
        stage = os.stat(
            _RELEASE_INDEX_STAGE_FILENAME_V1,
            dir_fd=directory,
            follow_symlinks=False,
        )
        final = os.stat(
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            dir_fd=directory,
            follow_symlinks=False,
        )
        _require_unactivated_file(
            stage,
            _RELEASE_INDEX_STAGE_FILENAME_V1,
            link_counts=frozenset({2}),
        )
        _require_unactivated_file(
            final,
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            link_counts=frozenset({2}),
        )
        if (
            (stage.st_dev, stage.st_ino) != (final.st_dev, final.st_ino)
            or stage_identity[:2] != (stage.st_dev, stage.st_ino)
        ):
            raise OSError("release index activation identity differs")
        os.unlink(_RELEASE_INDEX_STAGE_FILENAME_V1, dir_fd=directory)
        final = os.stat(
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            dir_fd=directory,
            follow_symlinks=False,
        )
        _require_unactivated_file(final, RELEASE_ARTIFACT_INDEX_FILENAME_V1)
        os.fsync(directory)
        return _publication_identity(final)
    except Exception:
        for name in (
            RELEASE_ARTIFACT_INDEX_FILENAME_V1 if linked else None,
            _RELEASE_INDEX_STAGE_FILENAME_V1,
        ):
            if name is None:
                continue
            try:
                observed = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (observed.st_dev, observed.st_ino) == stage_identity[:2]:
                    os.unlink(name, dir_fd=directory)
            except OSError:
                pass
        try:
            os.fsync(directory)
        except OSError:
            pass
        raise


def _publish(
    root: Path,
    identity: tuple[int, int],
    artifacts: tuple[_BuiltArtifactV1, ...],
    record: ReleaseArtifactBuildRecordV1,
    index: ReleaseArtifactIndexV1,
) -> dict[str, _PublicationReceiptV1]:
    created: dict[str, _PublicationReceiptV1] = {}
    payloads = [
        *((item.artifact_id, item.raw) for item in artifacts),
        (RELEASE_BUILD_RECORD_FILENAME_V1, record.canonical_bytes()),
    ]
    directory = _open_store_directory(root, identity)
    try:
        for name, raw in payloads:
            _require_store_identity(root, identity)
            created[name] = _PublicationReceiptV1(
                identity=_write_exclusive(directory, name, raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        os.fsync(directory)
        _require_store_identity(root, identity)
        index_raw = index.canonical_bytes()
        created[RELEASE_ARTIFACT_INDEX_FILENAME_V1] = _PublicationReceiptV1(
            identity=_activate_index(directory, index_raw),
            sha256=hashlib.sha256(index_raw).hexdigest(),
        )
        os.fsync(directory)
        return created
    except Exception:
        try:
            _rollback_publications_fd(directory, created)
        except (OSError, ReleaseBuildRefused):
            if created:
                raise
        raise
    finally:
        os.close(directory)


def _rollback_publications(
    root: Path,
    identity: tuple[int, int],
    receipts: Mapping[str, _PublicationReceiptV1],
) -> None:
    directory = _open_store_directory(root, identity)
    try:
        _rollback_publications_fd(directory, receipts)
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
            "failed release publication could not be rolled back safely",
        ) from error
    finally:
        os.close(directory)


def _refused_outcome(
    bundle: ReleaseProtocolBundleV1,
    command_id: str,
    error: Exception,
    *,
    candidate_commit: str | None,
) -> ReleaseCommandOutcomeV1:
    if isinstance(error, ReleaseBuildRefused):
        code = error.code
        detail = error.detail
    else:
        code = ReleaseBuildRefusalCodeV1.ARTIFACT_SEMANTIC_MISMATCH
        detail = f"{type(error).__name__}: {error}"
    return ReleaseCommandOutcomeV1(
        command_id=command_id,
        status=ReleaseCommandStatusV1.REFUSED,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail=detail,
        refusal_code=code.value,
        payload={"candidate_commit": candidate_commit},
    )


def build_release_artifacts(
    bundle: ReleaseProtocolBundleV1,
    *,
    candidate_commit: str,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Build all six transports twice and publish one immutable release set."""

    lock_descriptor: int | None = None
    try:
        root = _absolute_root(artifact_root, "release artifact root")
        governed_paths = _require_governed_store(bundle, root)
        if not root.exists():
            governed_paths.ensure(DataAreaId.RELEASE)
        governed_paths.validate(DataAreaId.RELEASE)
        identity = _require_safe_store(root, create=False)
        lock_descriptor = _open_store_directory(root, identity)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(lock_descriptor)
            lock_descriptor = None
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                "another release artifact build owns the governed store",
            ) from error
        public_names = (
            *_PUBLIC_ARTIFACT_FILENAMES_V1,
            RELEASE_BUILD_RECORD_FILENAME_V1,
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
        )
        inventory = _recover_unactivated_store(root, identity, public_names)
        present_public = tuple(name for name in public_names if name in inventory)
        complete_public = set(present_public) == set(public_names)
        allowed_names = {
            *public_names,
            _RELEASE_STORE_CONFIGURATION_FILENAME_V1,
            *(_RELEASE_POST_BUILD_FILE_NAMES_V1 if complete_public else ()),
            *(_RELEASE_POST_BUILD_DIRECTORY_NAMES_V1 if complete_public else ()),
        }
        unknown_names = tuple(name for name in inventory if name not in allowed_names)
        if unknown_names:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                "release artifact store contains an undeclared entry",
            )
        if complete_public:
            _validate_post_build_entries(root, identity, inventory)
        directory = _open_store_directory(root, identity)
        try:
            if _RELEASE_STORE_CONFIGURATION_FILENAME_V1 in inventory:
                _stable_regular_read(
                    _RELEASE_STORE_CONFIGURATION_FILENAME_V1,
                    maximum_bytes=1024 * 1024,
                    require_read_only=False,
                    directory_fd=directory,
                )
        finally:
            os.close(directory)
        _require_codex_network_seatbelt()
        if complete_public:
            existing = verify_release_artifacts(
                bundle,
                artifact_root=root,
                candidate_commit=candidate_commit,
            )
            if existing.status is ReleaseCommandStatusV1.PASS:
                return ReleaseCommandOutcomeV1(
                    command_id="BUILD_RELEASE",
                    status=ReleaseCommandStatusV1.COMPLETE,
                    protocol_set_sha256=bundle.protocol_set_sha256,
                    detail="The exact immutable release set already exists and verifies.",
                    payload=dict(existing.payload),
                )
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.OUTPUT_EXISTS,
                "the existing immutable release set did not verify",
            )
        if present_public:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.OUTPUT_EXISTS,
                "a partial immutable release output set already exists",
            )
        plan = plan_release_build(
            bundle,
            candidate_commit=candidate_commit,
            output_root=root,
        )
        if plan.status is not ReleaseCommandStatusV1.READY:
            return plan
        candidate, preflight = _preflight(
            bundle,
            candidate_commit,
            require_checkout=True,
        )
        runtime = preflight.build_runtime
        if runtime is None:
            raise ValueError("passing release preflight omitted its build runtime")
        _objects, files, initial_projection = _candidate_files(bundle, candidate_commit)
        _require_candidate_version(files)
        attempts: list[_AttemptResultV1] = []
        temporary_roots: list[Path] = []
        try:
            for number in range(1, RELEASE_BUILD_ATTEMPT_COUNT_V1 + 1):
                temporary = _new_private_attempt_root(
                    bundle.repository_root,
                    root,
                    number,
                )
                temporary_roots.append(temporary)
                result = _build_attempt(
                    bundle,
                    candidate,
                    runtime,
                    files,
                    temporary,
                )
                if result.candidate_tree_projection_sha256 != initial_projection:
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.CANDIDATE_INPUT_DRIFT,
                        "candidate Git-object bytes changed during an attempt",
                    )
                refreshed_candidate, refreshed_preflight = _preflight(
                    bundle,
                    candidate_commit,
                    require_checkout=True,
                )
                if refreshed_candidate != candidate:
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.CANDIDATE_INPUT_DRIFT,
                        "candidate identity changed during artifact construction",
                    )
                if (
                    refreshed_preflight.resource_snapshot_sha256
                    != preflight.resource_snapshot_sha256
                    or refreshed_preflight.build_runtime != runtime
                ):
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.RESOURCE_INPUT_DRIFT,
                        "offline resources changed during artifact construction",
                    )
                attempts.append(result)
            if not _same_attempt_bytes(attempts[0], attempts[1]):
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.BUILD_NONDETERMINISTIC,
                    "the two independent builds did not emit exact matching bytes",
                )
            selected = attempts[0]
            attempt_records = tuple(
                _attempt_record(number, result)
                for number, result in enumerate(attempts, start=1)
            )
            record = ReleaseArtifactBuildRecordV1(
                candidate_commit=candidate.candidate_commit,
                candidate_tree=candidate.candidate_tree,
                source_date_epoch=candidate.source_date_epoch,
                source_manifest_sha256=candidate.source_manifest_sha256,
                candidate_tree_projection_sha256=initial_projection,
                logical_build_id=candidate.logical_build_id,
                protocol_set_sha256=candidate.protocol_set_sha256,
                resource_snapshot_sha256=preflight.resource_snapshot_sha256,
                build_runtime_sha256=runtime.logical_sha256,
                network_policy_id=RELEASE_NETWORK_POLICY_ID_V1,
                attempts=attempt_records,
                checks=_release_build_checks(
                    bundle,
                    candidate,
                    runtime,
                    preflight.resource_snapshot_sha256,
                    attempt_records,
                    selected,
                ),
                artifact_index_sha256=selected.index.sha256,
                reproducible=True,
            )
        finally:
            for temporary in reversed(temporary_roots):
                try:
                    shutil.rmtree(temporary)
                    if temporary.exists():
                        raise OSError("private attempt root survived cleanup")
                except OSError as error:
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                        "a private build-attempt directory could not be removed",
                    ) from error
        governed_paths.validate(DataAreaId.RELEASE)
        if _store_inventory(root, identity) != inventory:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                "release artifact store changed during private build attempts",
            )
        published = _publish(root, identity, selected.artifacts, record, selected.index)
        verification = verify_release_artifacts(
            bundle,
            artifact_root=root,
            candidate_commit=candidate.candidate_commit,
        )
        if verification.status is not ReleaseCommandStatusV1.PASS:
            try:
                _rollback_publications(root, identity, published)
            except ReleaseBuildRefused as rollback_error:
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.PUBLICATION_FAILED,
                    (
                        "the published release set failed immediate deep verification "
                        f"({verification.refusal_code}: {verification.detail}); "
                        f"rollback also refused: {rollback_error.detail}"
                    ),
                ) from rollback_error
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.PUBLICATION_FAILED,
                (
                    "the published release set failed immediate deep verification "
                    f"({verification.refusal_code}: {verification.detail})"
                ),
            )
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.COMPLETE,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail=(
                "All six release artifacts matched across two builds and were "
                "published immutably."
            ),
            payload={
                "artifact_index_sha256": selected.index.sha256,
                "artifact_root": str(root),
                "build_record_sha256": record.sha256,
                "candidate_commit": candidate.candidate_commit,
                "logical_build_id": candidate.logical_build_id,
            },
        )
    except Exception as error:
        return _refused_outcome(
            bundle,
            "BUILD_RELEASE",
            error,
            candidate_commit=candidate_commit,
        )
    finally:
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)


def _published_project_wheel(raw: bytes, verified):
    if verified.wheel_bytes != raw:
        raise ValueError("verified project-wheel bytes changed")
    return verified


def verify_release_artifacts(
    bundle: ReleaseProtocolBundleV1,
    *,
    artifact_root: Path,
    candidate_commit: str | None = None,
) -> ReleaseCommandOutcomeV1:
    """Deeply reconstruct and verify a published six-artifact release set."""

    try:
        root = _absolute_root(artifact_root, "release artifact root")
        _require_governed_store(bundle, root)
        identity = _require_safe_store(root, create=False)
        _require_codex_network_seatbelt()
        inventory = _store_inventory(root, identity)
        public_names = {
            *_PUBLIC_ARTIFACT_FILENAMES_V1,
            RELEASE_BUILD_RECORD_FILENAME_V1,
            RELEASE_ARTIFACT_INDEX_FILENAME_V1,
        }
        allowed_names = {
            *public_names,
            _RELEASE_STORE_CONFIGURATION_FILENAME_V1,
            *_RELEASE_POST_BUILD_FILE_NAMES_V1,
            *_RELEASE_POST_BUILD_DIRECTORY_NAMES_V1,
        }
        if any(name not in allowed_names for name in inventory):
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                "release artifact store contains an undeclared entry",
            )
        _validate_post_build_entries(root, identity, inventory)
        present_public = public_names.intersection(inventory)
        if RELEASE_ARTIFACT_INDEX_FILENAME_V1 not in inventory:
            if present_public:
                raise ReleaseBuildRefused(
                    ReleaseBuildRefusalCodeV1.OUTPUT_EXISTS,
                    "release artifact store contains a partial inactive output set",
                )
            return ReleaseCommandOutcomeV1(
                command_id="VERIFY_RELEASE_ARTIFACTS",
                status=ReleaseCommandStatusV1.NOT_EXERCISED,
                protocol_set_sha256=bundle.protocol_set_sha256,
                detail="No immutable release artifact index exists yet.",
                refusal_code=ReleaseBuildRefusalCodeV1.ARTIFACT_INDEX_MISSING.value,
                payload={
                    "expected_index": str(
                        root / RELEASE_ARTIFACT_INDEX_FILENAME_V1
                    )
                },
            )
        if present_public != public_names:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_MISSING,
                "the activated release set omits one or more governed outputs",
            )
        snapshots: dict[str, tuple[bytes, tuple[int, ...], int, bool]] = {}

        def capture(name: str, maximum_bytes: int, read_only: bool) -> bytes:
            identities: list[tuple[int, ...]] = []
            raw = _stable_regular_read(
                name,
                maximum_bytes=maximum_bytes,
                require_read_only=read_only,
                directory_fd=directory,
                identity_out=identities,
            )
            if len(identities) != 1:  # pragma: no cover - helper invariant
                raise RuntimeError("stable release read omitted its file identity")
            snapshots[name] = (raw, identities[0], maximum_bytes, read_only)
            return raw

        directory = _open_store_directory(root, identity)
        try:
            if _RELEASE_STORE_CONFIGURATION_FILENAME_V1 in inventory:
                capture(
                    _RELEASE_STORE_CONFIGURATION_FILENAME_V1,
                    1024 * 1024,
                    False,
                )
            for post_build_name in sorted(
                _RELEASE_POST_BUILD_FILE_NAMES_V1.intersection(inventory),
                key=lambda item: item.encode("utf-8"),
            ):
                capture(
                    post_build_name,
                    RELEASE_RECORD_MAX_BYTES_V1,
                    False,
                )
            index_raw = capture(
                RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                RELEASE_RECORD_MAX_BYTES_V1,
                True,
            )
            record_raw = capture(
                RELEASE_BUILD_RECORD_FILENAME_V1,
                RELEASE_RECORD_MAX_BYTES_V1,
                True,
            )
            raw_by_id = {
                artifact_id: capture(
                    artifact_id,
                    RELEASE_ARTIFACT_MAX_BYTES_V1,
                    True,
                )
                for artifact_id in _PUBLIC_ARTIFACT_FILENAMES_V1
            }
        finally:
            os.close(directory)
        index = ReleaseArtifactIndexV1.from_bytes(index_raw)
        record = ReleaseArtifactBuildRecordV1.from_bytes(record_raw)
        selected_candidate = (
            index.candidate_commit if candidate_commit is None else candidate_commit
        )
        if (
            index.candidate_commit != selected_candidate
            or record.candidate_commit != selected_candidate
        ):
            raise ValueError("requested, indexed, and recorded candidate identities differ")
        candidate, preflight = _preflight(
            bundle,
            selected_candidate,
            require_checkout=False,
        )
        runtime = preflight.build_runtime
        if runtime is None:
            raise ValueError("passing release preflight omitted its build runtime")
        _objects, files, projection = _candidate_files(bundle, selected_candidate)
        _require_candidate_version(files)
        if (
            record.candidate_tree != candidate.candidate_tree
            or record.source_date_epoch != candidate.source_date_epoch
            or record.source_manifest_sha256 != candidate.source_manifest_sha256
            or record.candidate_tree_projection_sha256 != projection
            or record.logical_build_id != candidate.logical_build_id
            or record.protocol_set_sha256 != candidate.protocol_set_sha256
            or record.resource_snapshot_sha256 != preflight.resource_snapshot_sha256
            or record.build_runtime_sha256 != runtime.logical_sha256
        ):
            raise ValueError("build record differs from immutable candidate or resources")
        verify_project_wheel, resolve_offline_wheelhouse = _wheel_api()
        project_verified = verify_project_wheel(
            raw_by_id["project-wheel"],
            filename=RELEASE_PROJECT_WHEEL_FILENAME_V1,
            candidate_files=files,
            pyproject_bytes=files["pyproject.toml"],
            source_date_epoch=candidate.source_date_epoch,
            backend_version=_setuptools_version(runtime),
            requirements_lock=bundle.requirements_lock,
        )
        project_verified = _published_project_wheel(
            raw_by_id["project-wheel"], project_verified
        )
        locked_wheels = resolve_offline_wheelhouse(
            (bundle.repository_root / "release/wheelhouse").resolve(),
            bundle.requirements_lock,
        )
        starter = build_release_starter_set()
        expected_artifacts, expected_index = _assemble_release(
            bundle,
            candidate,
            runtime,
            files,
            project_verified,
            locked_wheels,
            starter,
        )
        if expected_index.canonical_bytes() != index.canonical_bytes():
            raise ValueError("artifact index differs from reconstructed release inputs")
        failures: list[dict[str, object]] = []
        for expected in expected_artifacts:
            observed = raw_by_id[expected.artifact_id]
            if observed != expected.raw:
                failures.append(
                    {
                        "artifact_id": expected.artifact_id,
                        "code": "ARTIFACT_SEMANTIC_MISMATCH",
                        "observed_sha256": hashlib.sha256(observed).hexdigest(),
                    }
                )
                continue
            if expected.member_plan_sha256 is not None:
                # Re-encoding above is the authoritative plan verification.  Call the
                # public verifier as an additional exact transport assertion.
                if expected.artifact_id == "source-archive":
                    layout = next(
                        item
                        for item in bundle.artifact_layout.artifacts
                        if item.artifact_id == expected.artifact_id
                    )
                    members = tuple(
                        ArchiveMemberPlanV1(
                            path=path,
                            payload=files[path],
                            source_class=ReleaseSourceClassV1.CANDIDATE_SOURCE,
                        )
                        for path in sorted(files, key=lambda item: item.encode("utf-8"))
                    )
                    verify_canonical_release_archive(
                        observed,
                        layout.archive_root or "",
                        members,
                        source_date_epoch=candidate.source_date_epoch,
                    )
        expected_observations = tuple(item.observation().as_dict() for item in expected_artifacts)
        if any(
            tuple(observation.as_dict() for observation in attempt.artifacts)
            != expected_observations
            for attempt in record.attempts
        ):
            raise ValueError("build-record attempts differ from reconstructed artifacts")
        if (
            record.artifact_index_sha256 != index.sha256
            or record.artifact_index_sha256 != expected_index.sha256
        ):
            raise ValueError("build-record artifact-index identity differs")
        expected_checks = _release_build_checks(
            bundle,
            candidate,
            runtime,
            preflight.resource_snapshot_sha256,
            record.attempts,
            _AttemptResultV1(
                artifacts=expected_artifacts,
                index=expected_index,
                candidate_tree_projection_sha256=projection,
            ),
        )
        if tuple(item.as_dict() for item in record.checks) != tuple(
            item.as_dict() for item in expected_checks
        ):
            raise ValueError("build-record WO40-F checks differ from reconstructed proof")
        if _store_inventory(root, identity) != inventory:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                "release artifact store inventory changed during verification",
            )
        final_directory = _open_store_directory(root, identity)
        try:
            for name in sorted(snapshots, key=lambda item: item.encode("utf-8")):
                raw, captured_identity, maximum_bytes, read_only = snapshots[name]
                final_identities: list[tuple[int, ...]] = []
                observed = _stable_regular_read(
                    name,
                    maximum_bytes=maximum_bytes,
                    require_read_only=read_only,
                    directory_fd=final_directory,
                    identity_out=final_identities,
                )
                if (
                    observed != raw
                    or final_identities != [captured_identity]
                ):
                    raise ReleaseBuildRefused(
                        ReleaseBuildRefusalCodeV1.ARTIFACT_STORE_UNSAFE,
                        f"release store file changed during verification: {name}",
                    )
        finally:
            os.close(final_directory)
        _require_governed_store(bundle, root).validate(DataAreaId.RELEASE)
        status = ReleaseCommandStatusV1.PASS if not failures else ReleaseCommandStatusV1.FAIL
        return ReleaseCommandOutcomeV1(
            command_id="VERIFY_RELEASE_ARTIFACTS",
            status=status,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail=(
                "All six artifacts, manifests, wheels, plans, receipt, and index verified."
                if not failures
                else "One or more release artifacts differ from their reconstructed plans."
            ),
            payload={
                "artifact_index_sha256": index.sha256,
                "build_record_sha256": record.sha256,
                "candidate_commit": candidate.candidate_commit,
                "failures": failures,
                "logical_build_id": candidate.logical_build_id,
            },
        )
    except Exception as error:
        return _refused_outcome(
            bundle,
            "VERIFY_RELEASE_ARTIFACTS",
            error,
            candidate_commit=candidate_commit,
        )


__all__ = [
    "RELEASE_ARTIFACT_BUILD_POLICY_ID_V1",
    "RELEASE_BUILD_ATTEMPT_COUNT_V1",
    "RELEASE_BUILD_RECORD_SCHEMA_ID_V1",
    "ReleaseArtifactBuildRecordV1",
    "build_release_artifacts",
    "verify_release_artifacts",
]
