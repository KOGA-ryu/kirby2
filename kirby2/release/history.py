"""Fail-closed release-history rollover for the DEV-0017 and DEV-0018 restarts.

The active release store can contain millions of governed relationships and several
GiB of immutable evidence.  A restart must therefore preserve the directory as one
filesystem object.  This module inventories every regular file, stages only the
small public evidence documents and the next provider configuration, and uses
same-filesystem directory renames for the bulk store.  It deliberately has no copy
or deletion recovery path.

Planning and state inspection are read-only.  The executor is intentionally not
registered as a top-level command: each deviation must be audited and committed
before an operator explicitly invokes its one-time rollover from the repaired
candidate.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Final, Iterable, Mapping


RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V3 = "KIRBY2_RELEASE_HISTORY_SNAPSHOT_V3"
RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V4 = "KIRBY2_RELEASE_HISTORY_SNAPSHOT_V4"
RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1 = (
    "KIRBY2_RELEASE_HISTORY_ATOMIC_RENAME_V1"
)
DEV0017_RELEASE_EVIDENCE_COMMIT_V1 = (
    "8ee892575372c3e296454ae6c3b2b991e481699e"
)
DEV0017_SOURCE_CANDIDATE_COMMIT_V1 = (
    "10b0d205ce0efdeff5e4e833c7cbfa808ccaf1cc"
)
DEV0017_RESOURCE_PREFLIGHT_PATH_V1 = "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
DEV0017_EVIDENCE_STATUS_BY_GATE_V1: tuple[tuple[str, str], ...] = (
    ("WO40-D1", "PASS"),
    ("WO40-F", "PASS"),
    ("WO40-G", "PASS"),
    ("WO40-H", "PASS"),
    ("WO40-I", "FAIL"),
)
DEV0017_EVIDENCE_PATH_BY_GATE_V1: Mapping[str, str] = {
    "WO40-D1": DEV0017_RESOURCE_PREFLIGHT_PATH_V1,
    "WO40-F": "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
    "WO40-G": "KIRBY2_RELEASE_MACOS_EVIDENCE.md",
    "WO40-H": "KIRBY2_RELEASE_LINUX_EVIDENCE.md",
    "WO40-I": "KIRBY2_RELEASE_PERFORMANCE_EVIDENCE.md",
}
DEV0017_RESOURCE_PREFLIGHT_SHA256_V1 = (
    "a4e791a0e401f657448f8bfe6364789d4e2cfcf5032c1344b3a401911d6bb9cc"
)
DEV0018_RELEASE_EVIDENCE_COMMIT_V1 = (
    "901e31c3e7d7a2ce5a423011e36d363440f20cc2"
)
DEV0018_SOURCE_CANDIDATE_COMMIT_V1 = (
    "a198c69426551b8d2f44269cbdc82980a8978b03"
)
DEV0018_PROTOCOL_COMMIT_V1 = "020da2c90c0f0000f822aad7c66538fe68c6c6e6"
DEV0018_EVIDENCE_STATUS_BY_GATE_V1: tuple[tuple[str, str], ...] = (
    ("WO40-D1", "PASS"),
    ("WO40-F", "PASS"),
    ("WO40-G", "PASS"),
    ("WO40-H", "PASS"),
    ("WO40-I", "NOT_RUN"),
)
DEV0018_EVIDENCE_PATH_BY_GATE_V1: Mapping[str, str] = {
    gate_id: DEV0017_EVIDENCE_PATH_BY_GATE_V1[gate_id]
    for gate_id in ("WO40-D1", "WO40-F", "WO40-G", "WO40-H")
}

_ACTIVE_STORE_RELATIVE = ".kirby2/release"
_HISTORY_PARENT_RELATIVE = ".kirby2/release-history"
_CONFIG_RELATIVE = "clean-providers.toml"
_MANIFEST_NAME = "HISTORY_MANIFEST.json"
_HISTORY_STAGE_NAME = (
    f".{DEV0017_RELEASE_EVIDENCE_COMMIT_V1}.dev-0017-history-staging"
)
_HISTORY_BUILD_NAME = f"{_HISTORY_STAGE_NAME}.building"
_NEXT_ACTIVE_NAME = ".dev-0017-release-next"
_NEXT_ACTIVE_BUILD_NAME = f"{_NEXT_ACTIVE_NAME}.building"
_DEV0018_HISTORY_STAGE_NAME = (
    f".{DEV0018_RELEASE_EVIDENCE_COMMIT_V1}.dev-0018-history-staging"
)
_DEV0018_HISTORY_BUILD_NAME = f"{_DEV0018_HISTORY_STAGE_NAME}.building"
_DEV0018_NEXT_ACTIVE_NAME = ".dev-0018-release-next"
_DEV0018_NEXT_ACTIVE_BUILD_NAME = f"{_DEV0018_NEXT_ACTIVE_NAME}.building"
_LOCK_NAME = "release-history-rollover.lock"
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_PROVIDER_CONFIG_BYTES = 1024 * 1024
_MAX_ROLLOVER_SOURCE_BYTES = 4 * 1024 * 1024
_DIGEST_CHUNK_BYTES = 1024 * 1024
_DARWIN_RENAME_EXCL: Final[int] = 0x00000004
_RENAME_EXCLUSIVE_FUNCTION: object | None = None
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_EVIDENCE_START = b"<!-- KIRBY2_RELEASE_GATE_EVIDENCE_V1\n"
_EVIDENCE_END = b"\nKIRBY2_RELEASE_GATE_EVIDENCE_V1 -->"


class ReleaseHistoryRolloverStateV1(str, Enum):
    READY = "READY"
    PREPARED = "PREPARED"
    OLD_STORE_QUARANTINED = "OLD_STORE_QUARANTINED"
    ACTIVE_REPLACED_HISTORY_PENDING = "ACTIVE_REPLACED_HISTORY_PENDING"
    COMPLETE = "COMPLETE"
    PREPARATION_INCOMPLETE = "PREPARATION_INCOMPLETE"
    AMBIGUOUS = "AMBIGUOUS"


class ReleaseHistoryRefusalCodeV1(str, Enum):
    PATH_IDENTITY_MISMATCH = "PATH_IDENTITY_MISMATCH"
    ACTIVE_RELEASE_MISSING = "ACTIVE_RELEASE_MISSING"
    HISTORY_ALREADY_EXISTS = "HISTORY_ALREADY_EXISTS"
    STAGING_CONFLICT = "STAGING_CONFLICT"
    CROSS_DEVICE_RENAME = "CROSS_DEVICE_RENAME"
    NONREGULAR_MEMBER = "NONREGULAR_MEMBER"
    ACTIVE_STORE_CHANGED = "ACTIVE_STORE_CHANGED"
    HISTORICAL_EVIDENCE_MISMATCH = "HISTORICAL_EVIDENCE_MISMATCH"
    PREPARATION_INCOMPLETE = "PREPARATION_INCOMPLETE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"


class ReleaseHistoryRefused(RuntimeError):
    def __init__(self, code: ReleaseHistoryRefusalCodeV1, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _require_exact_type(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must be exact {expected.__name__}")


def _require_commit(value: object, label: str) -> str:
    _require_exact_type(value, str, label)
    if _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase commit ID")
    return value


def _require_sha256(value: object, label: str) -> str:
    _require_exact_type(value, str, label)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _require_relative_path(value: object, label: str) -> str:
    _require_exact_type(value, str, label)
    if (
        not value
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} is not a canonical relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"{label} escapes its history root")
    if pure.as_posix() != value:
        raise ValueError(f"{label} is not a canonical POSIX path")
    return value


@dataclass(frozen=True, slots=True)
class ReleaseHistoryFileV1:
    path: str
    size: int
    sha256: str
    mode: str = "0444"

    def __post_init__(self) -> None:
        _require_relative_path(self.path, "history file path")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("history file size must be a nonnegative integer")
        _require_sha256(self.sha256, "history file SHA-256")
        if self.mode != "0444":
            raise ValueError("historical files must have exact mode 0444")

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ReleaseHistoryGateResultV1:
    gate_id: str
    status: str

    def __post_init__(self) -> None:
        if self.gate_id not in DEV0017_EVIDENCE_PATH_BY_GATE_V1:
            raise ValueError("history gate ID is outside the DEV-0017 set")
        if self.status not in {"PASS", "PASS_WITH_WARNINGS", "FAIL"}:
            raise ValueError("history gate status is invalid")

    def as_dict(self) -> dict[str, str]:
        return {"gate_id": self.gate_id, "status": self.status}


@dataclass(frozen=True, slots=True)
class ReleaseHistoryManifestV3:
    release_evidence_commit: str
    source_candidate_commit: str
    gate_results: tuple[ReleaseHistoryGateResultV1, ...]
    files: tuple[ReleaseHistoryFileV1, ...]
    history_schema_id: str = RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V3
    schema_version: int = 3
    rollover_policy_id: str = RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1

    def __post_init__(self) -> None:
        _require_commit(self.release_evidence_commit, "release evidence commit")
        _require_commit(self.source_candidate_commit, "source candidate commit")
        if self.history_schema_id != RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V3:
            raise ValueError("history manifest schema differs")
        if self.schema_version != 3:
            raise ValueError("history manifest version differs")
        if self.rollover_policy_id != RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1:
            raise ValueError("history rollover policy differs")
        expected_results = tuple(
            ReleaseHistoryGateResultV1(gate_id, status)
            for gate_id, status in DEV0017_EVIDENCE_STATUS_BY_GATE_V1
        )
        if self.gate_results != expected_results:
            raise ValueError("history gate-result projection differs")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("history file inventory must be path sorted")
        if len(paths) != len(set(paths)) or not paths:
            raise ValueError("history file inventory is empty or duplicated")
        public_paths = {
            item.path
            for item in self.files
            if PurePosixPath(item.path).parts[0] != "artifacts"
        }
        if public_paths != set(DEV0017_EVIDENCE_PATH_BY_GATE_V1.values()):
            raise ValueError("history manifest must contain exactly five public documents")
        artifact_paths = {
            item.path
            for item in self.files
            if PurePosixPath(item.path).parts[0] == "artifacts"
        }
        if not artifact_paths or f"artifacts/{_CONFIG_RELATIVE}" not in artifact_paths:
            raise ValueError("history manifest lacks the complete active-store class")

    def as_dict(self) -> dict[str, object]:
        return {
            "files": [item.as_dict() for item in self.files],
            "gate_results": [item.as_dict() for item in self.gate_results],
            "history_schema_id": self.history_schema_id,
            "release_evidence_commit": self.release_evidence_commit,
            "rollover_policy_id": self.rollover_policy_id,
            "schema_version": self.schema_version,
            "source_candidate_commit": self.source_candidate_commit,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseHistoryManifestV3":
        _require_exact_type(raw, bytes, "history manifest bytes")
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("history manifest exceeds its byte bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("history manifest is not canonical JSON") from error
        if type(payload) is not dict or set(payload) != {
            "files",
            "gate_results",
            "history_schema_id",
            "release_evidence_commit",
            "rollover_policy_id",
            "schema_version",
            "source_candidate_commit",
        }:
            raise ValueError("history manifest fields differ")
        raw_files = payload["files"]
        raw_results = payload["gate_results"]
        if type(raw_files) is not list or type(raw_results) is not list:
            raise TypeError("history manifest inventories must be arrays")
        files: list[ReleaseHistoryFileV1] = []
        for row in raw_files:
            if type(row) is not dict or set(row) != {"mode", "path", "sha256", "size"}:
                raise ValueError("history file record fields differ")
            files.append(ReleaseHistoryFileV1(**row))
        results: list[ReleaseHistoryGateResultV1] = []
        for row in raw_results:
            if type(row) is not dict or set(row) != {"gate_id", "status"}:
                raise ValueError("history gate-result fields differ")
            results.append(ReleaseHistoryGateResultV1(**row))
        restored = cls(
            release_evidence_commit=payload["release_evidence_commit"],
            source_candidate_commit=payload["source_candidate_commit"],
            gate_results=tuple(results),
            files=tuple(files),
            history_schema_id=payload["history_schema_id"],
            schema_version=payload["schema_version"],
            rollover_policy_id=payload["rollover_policy_id"],
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("history manifest bytes are noncanonical")
        return restored


@dataclass(frozen=True, slots=True)
class ReleaseHistoryGateResultV2:
    gate_id: str
    status: str

    def __post_init__(self) -> None:
        expected = dict(DEV0018_EVIDENCE_STATUS_BY_GATE_V1)
        if self.gate_id not in expected:
            raise ValueError("partial-history gate ID is outside the DEV-0018 set")
        if self.status != expected[self.gate_id]:
            raise ValueError("partial-history gate status differs")

    def as_dict(self) -> dict[str, str]:
        return {"gate_id": self.gate_id, "status": self.status}


@dataclass(frozen=True, slots=True)
class ReleaseHistoryManifestV4:
    """Exact snapshot of the incomplete DEV-0018 predecessor candidate."""

    release_evidence_commit: str
    source_candidate_commit: str
    gate_results: tuple[ReleaseHistoryGateResultV2, ...]
    files: tuple[ReleaseHistoryFileV1, ...]
    history_schema_id: str = RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V4
    schema_version: int = 4
    rollover_policy_id: str = RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1

    def __post_init__(self) -> None:
        _require_commit(self.release_evidence_commit, "partial release evidence commit")
        _require_commit(self.source_candidate_commit, "partial source candidate commit")
        if self.history_schema_id != RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V4:
            raise ValueError("partial-history manifest schema differs")
        if self.schema_version != 4:
            raise ValueError("partial-history manifest version differs")
        if self.rollover_policy_id != RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1:
            raise ValueError("partial-history rollover policy differs")
        expected_results = tuple(
            ReleaseHistoryGateResultV2(gate_id, status)
            for gate_id, status in DEV0018_EVIDENCE_STATUS_BY_GATE_V1
        )
        if self.gate_results != expected_results:
            raise ValueError("partial-history gate-result projection differs")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("partial-history file inventory must be path sorted")
        if len(paths) != len(set(paths)) or not paths:
            raise ValueError("partial-history file inventory is empty or duplicated")
        public_paths = {
            item.path
            for item in self.files
            if PurePosixPath(item.path).parts[0] != "artifacts"
        }
        if public_paths != set(DEV0018_EVIDENCE_PATH_BY_GATE_V1.values()):
            raise ValueError(
                "partial-history manifest must contain exactly four public documents"
            )
        artifact_paths = {
            item.path
            for item in self.files
            if PurePosixPath(item.path).parts[0] == "artifacts"
        }
        if not artifact_paths or f"artifacts/{_CONFIG_RELATIVE}" not in artifact_paths:
            raise ValueError("partial-history manifest lacks the active-store class")
        if any(
            path == "artifacts/gate-evidence/wo40-i"
            or path.startswith("artifacts/gate-evidence/wo40-i/")
            for path in artifact_paths
        ):
            raise ValueError("partial-history manifest contains a WO40-I publication")

    def as_dict(self) -> dict[str, object]:
        return {
            "files": [item.as_dict() for item in self.files],
            "gate_results": [item.as_dict() for item in self.gate_results],
            "history_schema_id": self.history_schema_id,
            "release_evidence_commit": self.release_evidence_commit,
            "rollover_policy_id": self.rollover_policy_id,
            "schema_version": self.schema_version,
            "source_candidate_commit": self.source_candidate_commit,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseHistoryManifestV4":
        _require_exact_type(raw, bytes, "partial-history manifest bytes")
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise ValueError("partial-history manifest exceeds its byte bound")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("partial-history manifest is not canonical JSON") from error
        if type(payload) is not dict or set(payload) != {
            "files",
            "gate_results",
            "history_schema_id",
            "release_evidence_commit",
            "rollover_policy_id",
            "schema_version",
            "source_candidate_commit",
        }:
            raise ValueError("partial-history manifest fields differ")
        raw_files = payload["files"]
        raw_results = payload["gate_results"]
        if type(raw_files) is not list or type(raw_results) is not list:
            raise TypeError("partial-history manifest inventories must be arrays")
        files: list[ReleaseHistoryFileV1] = []
        for row in raw_files:
            if type(row) is not dict or set(row) != {"mode", "path", "sha256", "size"}:
                raise ValueError("partial-history file record fields differ")
            files.append(ReleaseHistoryFileV1(**row))
        results: list[ReleaseHistoryGateResultV2] = []
        for row in raw_results:
            if type(row) is not dict or set(row) != {"gate_id", "status"}:
                raise ValueError("partial-history gate-result fields differ")
            results.append(ReleaseHistoryGateResultV2(**row))
        restored = cls(
            release_evidence_commit=payload["release_evidence_commit"],
            source_candidate_commit=payload["source_candidate_commit"],
            gate_results=tuple(results),
            files=tuple(files),
            history_schema_id=payload["history_schema_id"],
            schema_version=payload["schema_version"],
            rollover_policy_id=payload["rollover_policy_id"],
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("partial-history manifest bytes are noncanonical")
        return restored


_ReleaseHistoryManifest = ReleaseHistoryManifestV3 | ReleaseHistoryManifestV4


@dataclass(frozen=True, slots=True)
class ReleaseHistoryRolloverPlanV1:
    repository_root: Path
    active_root: Path
    history_parent: Path
    history_build_root: Path
    history_staging_root: Path
    history_final_root: Path
    next_active_build_root: Path
    next_active_root: Path
    lock_path: Path
    state: ReleaseHistoryRolloverStateV1


@dataclass(frozen=True, slots=True)
class ReleaseHistoryRolloverReceiptV1:
    release_evidence_commit: str
    source_candidate_commit: str
    history_root: str
    active_root: str
    file_count: int
    manifest_sha256: str
    disposition: str
    policy_id: str = RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1

    def __post_init__(self) -> None:
        _require_commit(self.release_evidence_commit, "receipt evidence commit")
        _require_commit(self.source_candidate_commit, "receipt candidate commit")
        _require_sha256(self.manifest_sha256, "receipt manifest SHA-256")
        if type(self.file_count) is not int or self.file_count <= 0:
            raise ValueError("receipt file count must be positive")
        if self.disposition not in {"ROLLED_OVER", "ALREADY_COMPLETE"}:
            raise ValueError("receipt disposition differs")
        if self.policy_id != RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1:
            raise ValueError("receipt policy differs")


@dataclass(frozen=True, slots=True)
class _InventoryEntry:
    record: ReleaseHistoryFileV1
    device: int
    inode: int
    mode: int
    nlink: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _RegularFileSnapshot:
    raw: bytes
    size: int
    sha256: str
    device: int
    inode: int
    mode: int
    nlink: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    relative: str
    size: int
    sha256: str | None
    device: int
    inode: int
    mode: int
    nlink: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _ExecutionAuthority:
    candidate_commit: str
    source_commit: str


def _plain_directory(
    path: Path,
    label: str,
    *,
    expected_device: int | None = None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} is unavailable",
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} is not a plain directory",
        )
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} ownership or permissions are unsafe",
        )
    if expected_device is not None and metadata.st_dev != expected_device:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.CROSS_DEVICE_RENAME,
            f"{label} is not on the rollover filesystem",
        )
    return metadata


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"rollover path cannot be inspected: {path.name}",
        ) from error
    return True


def _path_flags(plan: ReleaseHistoryRolloverPlanV1) -> dict[str, bool]:
    return {
        "active": _path_exists_no_follow(plan.active_root),
        "final": _path_exists_no_follow(plan.history_final_root),
        "history_build": _path_exists_no_follow(plan.history_build_root),
        "history_stage": _path_exists_no_follow(plan.history_staging_root),
        "next_build": _path_exists_no_follow(plan.next_active_build_root),
        "next_stage": _path_exists_no_follow(plan.next_active_root),
    }


def _validate_lifecycle_roots(plan: ReleaseHistoryRolloverPlanV1) -> None:
    _plain_directory(plan.repository_root, "repository root")
    state_root = plan.repository_root / ".kirby2"
    state_metadata = _plain_directory(state_root, "release state root")
    _plain_directory(
        plan.history_parent,
        "release history root",
        expected_device=state_metadata.st_dev,
    )
    for path, label in (
        (plan.active_root, "active release root"),
        (plan.history_build_root, "history build root"),
        (plan.history_staging_root, "history staging root"),
        (plan.history_final_root, "history final root"),
        (plan.next_active_build_root, "next-active build root"),
        (plan.next_active_root, "next-active staging root"),
    ):
        if _path_exists_no_follow(path):
            _plain_directory(path, label, expected_device=state_metadata.st_dev)
    artifacts = plan.history_staging_root / "artifacts"
    if _path_exists_no_follow(artifacts):
        _plain_directory(
            artifacts,
            "quarantined active release root",
            expected_device=state_metadata.st_dev,
        )
    if _path_exists_no_follow(plan.lock_path):
        metadata = plan.lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_dev != state_metadata.st_dev
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                "release-history lock identity is unsafe",
            )


def _classify_state(plan: ReleaseHistoryRolloverPlanV1) -> ReleaseHistoryRolloverStateV1:
    flags = _path_flags(plan)
    artifacts_present = _path_exists_no_follow(
        plan.history_staging_root / "artifacts"
    )
    if (
        flags["active"]
        and not flags["final"]
        and not artifacts_present
        and (
            flags["history_build"]
            or flags["next_build"]
            or flags["history_stage"] != flags["next_stage"]
        )
    ):
        return ReleaseHistoryRolloverStateV1.PREPARATION_INCOMPLETE
    if flags["history_build"] or flags["next_build"]:
        return ReleaseHistoryRolloverStateV1.AMBIGUOUS
    if flags["final"]:
        if flags["active"] and not flags["history_stage"] and not flags["next_stage"]:
            return ReleaseHistoryRolloverStateV1.COMPLETE
        return ReleaseHistoryRolloverStateV1.AMBIGUOUS
    if not flags["history_stage"] and not flags["next_stage"]:
        return (
            ReleaseHistoryRolloverStateV1.READY
            if flags["active"]
            else ReleaseHistoryRolloverStateV1.AMBIGUOUS
        )
    if flags["history_stage"] and flags["next_stage"] and flags["active"]:
        artifacts = plan.history_staging_root / "artifacts"
        if not _path_exists_no_follow(artifacts):
            return ReleaseHistoryRolloverStateV1.PREPARED
    if (
        flags["history_stage"]
        and flags["next_stage"]
        and not flags["active"]
        and artifacts_present
    ):
        return ReleaseHistoryRolloverStateV1.OLD_STORE_QUARANTINED
    if flags["history_stage"] and not flags["next_stage"] and flags["active"]:
        return ReleaseHistoryRolloverStateV1.ACTIVE_REPLACED_HISTORY_PENDING
    return ReleaseHistoryRolloverStateV1.AMBIGUOUS


def _base_plan(repository_root: Path | str) -> ReleaseHistoryRolloverPlanV1:
    repository = Path(repository_root).absolute()
    dot_root = repository / ".kirby2"
    history_parent = repository / _HISTORY_PARENT_RELATIVE
    provisional = ReleaseHistoryRolloverPlanV1(
        repository_root=repository,
        active_root=repository / _ACTIVE_STORE_RELATIVE,
        history_parent=history_parent,
        history_build_root=history_parent / _HISTORY_BUILD_NAME,
        history_staging_root=history_parent / _HISTORY_STAGE_NAME,
        history_final_root=history_parent / DEV0017_RELEASE_EVIDENCE_COMMIT_V1,
        next_active_build_root=dot_root / _NEXT_ACTIVE_BUILD_NAME,
        next_active_root=dot_root / _NEXT_ACTIVE_NAME,
        lock_path=dot_root / _LOCK_NAME,
        state=ReleaseHistoryRolloverStateV1.AMBIGUOUS,
    )
    return provisional


def _plan_with_current_state(
    plan: ReleaseHistoryRolloverPlanV1,
) -> ReleaseHistoryRolloverPlanV1:
    return ReleaseHistoryRolloverPlanV1(
        repository_root=plan.repository_root,
        active_root=plan.active_root,
        history_parent=plan.history_parent,
        history_build_root=plan.history_build_root,
        history_staging_root=plan.history_staging_root,
        history_final_root=plan.history_final_root,
        next_active_build_root=plan.next_active_build_root,
        next_active_root=plan.next_active_root,
        lock_path=plan.lock_path,
        state=_classify_state(plan),
    )


def inspect_dev0017_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverPlanV1:
    """Return the current lifecycle state without reading payload bodies."""

    plan = _base_plan(repository_root)
    _validate_lifecycle_roots(plan)
    return _plan_with_current_state(plan)


def plan_dev0017_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverPlanV1:
    """Preflight a pristine DEV-0017 rollover without enumerating the store."""

    plan = inspect_dev0017_release_history_rollover(repository_root)
    if plan.state is not ReleaseHistoryRolloverStateV1.READY:
        code = (
            ReleaseHistoryRefusalCodeV1.HISTORY_ALREADY_EXISTS
            if plan.state is ReleaseHistoryRolloverStateV1.COMPLETE
            else ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED
        )
        raise ReleaseHistoryRefused(code, f"rollover state is {plan.state.value}")
    active_metadata = _plain_directory(plan.active_root, "active release root")
    history_metadata = _plain_directory(plan.history_parent, "release history root")
    state_metadata = _plain_directory(
        plan.repository_root / ".kirby2", "release state root"
    )
    if len({active_metadata.st_dev, history_metadata.st_dev, state_metadata.st_dev}) != 1:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.CROSS_DEVICE_RENAME,
            "active, history, and next-active roots are not on one filesystem",
        )
    config = plan.active_root / _CONFIG_RELATIVE
    _digest_regular_file(config)
    return plan


def _base_plan_dev0018(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverPlanV1:
    repository = Path(repository_root).absolute()
    dot_root = repository / ".kirby2"
    history_parent = repository / _HISTORY_PARENT_RELATIVE
    return ReleaseHistoryRolloverPlanV1(
        repository_root=repository,
        active_root=repository / _ACTIVE_STORE_RELATIVE,
        history_parent=history_parent,
        history_build_root=history_parent / _DEV0018_HISTORY_BUILD_NAME,
        history_staging_root=history_parent / _DEV0018_HISTORY_STAGE_NAME,
        history_final_root=history_parent / DEV0018_RELEASE_EVIDENCE_COMMIT_V1,
        next_active_build_root=dot_root / _DEV0018_NEXT_ACTIVE_BUILD_NAME,
        next_active_root=dot_root / _DEV0018_NEXT_ACTIVE_NAME,
        lock_path=dot_root / _LOCK_NAME,
        state=ReleaseHistoryRolloverStateV1.AMBIGUOUS,
    )


def inspect_dev0018_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverPlanV1:
    """Return the incomplete-candidate rollover state without reading payloads."""

    plan = _base_plan_dev0018(repository_root)
    _validate_lifecycle_roots(plan)
    return _plan_with_current_state(plan)


def plan_dev0018_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverPlanV1:
    """Preflight a pristine DEV-0018 rollover without enumerating the store."""

    plan = inspect_dev0018_release_history_rollover(repository_root)
    if plan.state is not ReleaseHistoryRolloverStateV1.READY:
        code = (
            ReleaseHistoryRefusalCodeV1.HISTORY_ALREADY_EXISTS
            if plan.state is ReleaseHistoryRolloverStateV1.COMPLETE
            else ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED
        )
        raise ReleaseHistoryRefused(code, f"rollover state is {plan.state.value}")
    active_metadata = _plain_directory(plan.active_root, "active release root")
    history_metadata = _plain_directory(plan.history_parent, "release history root")
    state_metadata = _plain_directory(
        plan.repository_root / ".kirby2", "release state root"
    )
    if len({active_metadata.st_dev, history_metadata.st_dev, state_metadata.st_dev}) != 1:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.CROSS_DEVICE_RENAME,
            "active, history, and next-active roots are not on one filesystem",
        )
    _digest_regular_file(plan.active_root / _CONFIG_RELATIVE)
    return plan


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _digest_regular_file_descriptor(
    descriptor: int,
    *,
    label: str,
    expected_device: int | None = None,
) -> tuple[int, str, os.stat_result]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or (expected_device is not None and before.st_dev != expected_device)
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
            f"{label} identity is unsafe",
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _DIGEST_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    after = os.fstat(descriptor)
    if _file_identity(before) != _file_identity(after) or size != before.st_size:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            f"{label} changed while hashing",
        )
    return size, digest.hexdigest(), after


def _digest_regular_file(path: Path) -> tuple[int, str, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow file support",
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
            f"cannot open regular file without following links: {path.name}",
        ) from error
    try:
        return _digest_regular_file_descriptor(descriptor, label=path.name)
    finally:
        os.close(descriptor)


def _read_regular_file_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int,
    label: str,
) -> _RegularFileSnapshot:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > maximum_bytes
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
            f"{label} identity or size is unsafe",
        )
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(_DIGEST_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    after = os.fstat(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        identity_before != identity_after
        or len(raw) != before.st_size
        or len(raw) > maximum_bytes
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            f"{label} changed during its stable read",
        )
    return _RegularFileSnapshot(
        raw=raw,
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        device=after.st_dev,
        inode=after.st_ino,
        mode=after.st_mode,
        nlink=after.st_nlink,
        uid=after.st_uid,
        gid=after.st_gid,
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
    )


def _read_regular_file_snapshot(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> _RegularFileSnapshot:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow file support",
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
            f"{label} cannot be opened safely",
        ) from error
    try:
        return _read_regular_file_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=label,
        )
    finally:
        os.close(descriptor)


def _read_regular_file_snapshot_at(
    directory_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    label: str,
) -> _RegularFileSnapshot:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow file support",
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
            f"{label} cannot be opened safely",
        ) from error
    try:
        return _read_regular_file_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=label,
        )
    finally:
        os.close(descriptor)


def _scan_regular_files_at(
    root_descriptor: int,
    *,
    hash_files: bool,
    required_file_mode: int | None = None,
    required_directory_mode: int | None = None,
) -> tuple[_ScannedFile, ...]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow inventory support",
        )
    root_metadata = os.fstat(root_descriptor)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
        or (
            required_directory_mode is not None
            and stat.S_IMODE(root_metadata.st_mode) != required_directory_mode
        )
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "history inventory root identity is unsafe",
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    scanned: list[_ScannedFile] = []

    def scan_directory(
        directory_descriptor: int,
        relative_parts: tuple[str, ...],
    ) -> None:
        directory_before = os.fstat(directory_descriptor)
        try:
            with os.scandir(directory_descriptor) as iterator:
                entries = tuple(
                    sorted(iterator, key=lambda item: os.fsencode(item.name))
                )
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                "history directory cannot be scanned safely",
            ) from error
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                    "history member identity cannot be read",
                ) from error
            child_parts = (*relative_parts, entry.name)
            if stat.S_ISDIR(metadata.st_mode) and not entry.is_symlink():
                try:
                    child_descriptor = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                        "history directory changed while it was pinned",
                    ) from error
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        opened.st_uid != os.getuid()
                        or opened.st_dev != root_metadata.st_dev
                        or stat.S_IMODE(opened.st_mode) & 0o022
                        or (
                            required_directory_mode is not None
                            and stat.S_IMODE(opened.st_mode)
                            != required_directory_mode
                        )
                        or _directory_identity(opened)
                        != _directory_identity(metadata)
                    ):
                        raise ReleaseHistoryRefused(
                            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                            "history directory identity is unsafe",
                        )
                    scan_directory(child_descriptor, child_parts)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(metadata.st_mode) and not entry.is_symlink():
                try:
                    file_descriptor = os.open(
                        entry.name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                        "history file changed while it was pinned",
                    ) from error
                try:
                    opened = os.fstat(file_descriptor)
                    if (
                        opened.st_uid != os.getuid()
                        or opened.st_nlink != 1
                        or opened.st_dev != root_metadata.st_dev
                        or (
                            required_file_mode is not None
                            and stat.S_IMODE(opened.st_mode) != required_file_mode
                        )
                        or _file_identity(opened) != _file_identity(metadata)
                    ):
                        raise ReleaseHistoryRefused(
                            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                            "history file identity is unsafe",
                        )
                    if hash_files:
                        size, digest, opened = _digest_regular_file_descriptor(
                            file_descriptor,
                            label="/".join(child_parts),
                            expected_device=root_metadata.st_dev,
                        )
                    else:
                        size = opened.st_size
                        digest = None
                    scanned.append(
                        _ScannedFile(
                            relative="/".join(child_parts),
                            size=size,
                            sha256=digest,
                            device=opened.st_dev,
                            inode=opened.st_ino,
                            mode=opened.st_mode,
                            nlink=opened.st_nlink,
                            uid=opened.st_uid,
                            gid=opened.st_gid,
                            mtime_ns=opened.st_mtime_ns,
                            ctime_ns=opened.st_ctime_ns,
                        )
                    )
                finally:
                    os.close(file_descriptor)
            else:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                    "history tree contains a link or special member",
                )
        directory_after = os.fstat(directory_descriptor)
        if _directory_identity(directory_before) != _directory_identity(
            directory_after
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
                "history directory changed during its inventory",
            )

    scan_directory(root_descriptor, ())
    return tuple(
        sorted(scanned, key=lambda item: item.relative.encode("utf-8"))
    )


def _inventory_active_store(root_descriptor: int) -> tuple[_InventoryEntry, ...]:
    inventory: list[_InventoryEntry] = []
    for scanned in _scan_regular_files_at(root_descriptor, hash_files=True):
        if scanned.sha256 is None:  # pragma: no cover - hash policy is fixed above
            raise RuntimeError("active inventory lacks a file digest")
        record = ReleaseHistoryFileV1(
            path=f"artifacts/{scanned.relative}",
            size=scanned.size,
            sha256=scanned.sha256,
        )
        inventory.append(
            _InventoryEntry(
                record=record,
                device=scanned.device,
                inode=scanned.inode,
                mode=scanned.mode,
                nlink=scanned.nlink,
                uid=scanned.uid,
                gid=scanned.gid,
                mtime_ns=scanned.mtime_ns,
                ctime_ns=scanned.ctime_ns,
            )
        )
    if not inventory:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_RELEASE_MISSING,
            "active release store has no regular files",
        )
    return tuple(inventory)


def _verify_inventory_identity(
    root_descriptor: int,
    inventory: Iterable[_InventoryEntry],
) -> None:
    expected = tuple(inventory)
    observed = _scan_regular_files_at(root_descriptor, hash_files=False)
    expected_relatives = tuple(
        PurePosixPath(item.record.path).relative_to("artifacts").as_posix()
        for item in expected
    )
    observed_relatives = tuple(item.relative for item in observed)
    if observed_relatives != expected_relatives:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            "active release inventory changed before activation",
        )
    for scanned, item in zip(observed, expected, strict=True):
        if (
            scanned.device,
            scanned.inode,
            scanned.mode,
            scanned.nlink,
            scanned.uid,
            scanned.gid,
            scanned.size,
            scanned.mtime_ns,
            scanned.ctime_ns,
        ) != (
            item.device,
            item.inode,
            item.mode,
            item.nlink,
            item.uid,
            item.gid,
            item.record.size,
            item.mtime_ns,
            item.ctime_ns,
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
                f"active release identity changed: {item.record.path}",
            )


def _history_git_environment() -> dict[str, str]:
    overridden = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_WORK_TREE",
    }
    environment = {
        key: value for key, value in os.environ.items() if key not in overridden
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _git_blob(repository: Path, commit: str, relative: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{relative}"],
        cwd=repository,
        env=_history_git_environment(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            f"historical evidence is unavailable at {commit}:{relative}",
        )
    return completed.stdout


def _git_commit_metadata(
    repository: Path,
    commit: str,
) -> tuple[str, tuple[str, ...], str]:
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%H%x00%P%x00%s", commit],
        cwd=repository,
        env=_history_git_environment(),
        capture_output=True,
        check=False,
    )
    fields = completed.stdout.rstrip(b"\n").split(b"\0")
    try:
        resolved = fields[0].decode("ascii")
        parents = tuple(fields[1].decode("ascii").split())
        subject = fields[2].decode("utf-8")
    except (IndexError, UnicodeDecodeError) as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "release restart commit metadata cannot be decoded",
        ) from error
    if (
        completed.returncode != 0
        or len(fields) != 3
        or _COMMIT.fullmatch(resolved) is None
        or any(_COMMIT.fullmatch(parent) is None for parent in parents)
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "release restart commit metadata differs",
        )
    return resolved, parents, subject


def _git_changed_paths(
    repository: Path,
    commit: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(allow_empty) is not bool:
        raise TypeError("empty changed-path policy must be Boolean")
    completed = subprocess.run(
        [
            "git",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        ],
        cwd=repository,
        env=_history_git_environment(),
        capture_output=True,
        check=False,
    )
    try:
        paths = tuple(
            raw.decode("utf-8")
            for raw in completed.stdout.split(b"\0")
            if raw
        )
    except UnicodeDecodeError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "release restart changed-path inventory is not UTF-8",
        ) from error
    if completed.returncode != 0 or (not paths and not allow_empty):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "release restart changed-path inventory is unavailable",
        )
    return paths


def _verify_execution_authority(repository: Path) -> _ExecutionAuthority:
    try:
        from kirby2.release.build import (
            load_release_protocol_bundle,
            verify_release_candidate_inputs,
        )

        head_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repository,
            env=_history_git_environment(),
            capture_output=True,
            check=False,
        )
        head = head_result.stdout.decode("ascii", errors="replace").strip()
        if head_result.returncode != 0 or _COMMIT.fullmatch(head) is None:
            raise ValueError("HEAD does not resolve to one exact commit")
        resolved, head_parents, head_subject = _git_commit_metadata(repository, head)
        if (
            resolved != head
            or len(head_parents) != 1
            or head_subject != "Reverify release resources for DEV-0017"
            or _git_changed_paths(repository, head)
            != (DEV0017_RESOURCE_PREFLIGHT_PATH_V1,)
        ):
            raise ValueError("HEAD is not the exact DEV-0017 D1 evidence commit")
        source = head_parents[0]
        source_resolved, source_parents, source_subject = _git_commit_metadata(
            repository, source
        )
        if (
            source_resolved != source
            or source_parents != (DEV0017_RELEASE_EVIDENCE_COMMIT_V1,)
            or source_subject != "Repair measured release performance failures"
        ):
            raise ValueError("D1 does not directly follow the DEV-0017 source repair")
        bundle = load_release_protocol_bundle(repository)
        candidate = verify_release_candidate_inputs(
            bundle,
            head,
            require_checkout=True,
        )
        if candidate.candidate_commit != head or candidate.protocol_commit != source:
            raise ValueError("candidate protocol ownership differs from the two-commit order")
        runtime_source = _read_regular_file_snapshot(
            repository / "kirby2/release/history.py",
            maximum_bytes=_MAX_ROLLOVER_SOURCE_BYTES,
            label="runtime rollover source",
        )
        if _git_blob(repository, head, "kirby2/release/history.py") != runtime_source.raw:
            raise ValueError("runtime rollover source differs from committed authority")
    except (OSError, RuntimeError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "rollover requires the exact clean DEV-0017 source and D1 commits",
        ) from error
    return _ExecutionAuthority(candidate_commit=head, source_commit=source)


def _verify_dev0018_execution_authority(repository: Path) -> _ExecutionAuthority:
    try:
        from kirby2.release.build import (
            load_release_protocol_bundle,
            verify_release_candidate_inputs,
        )

        head_result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repository,
            env=_history_git_environment(),
            capture_output=True,
            check=False,
        )
        head = head_result.stdout.decode("ascii", errors="replace").strip()
        if head_result.returncode != 0 or _COMMIT.fullmatch(head) is None:
            raise ValueError("HEAD does not resolve to one exact commit")
        resolved, head_parents, head_subject = _git_commit_metadata(repository, head)
        if (
            resolved != head
            or len(head_parents) != 1
            or head_subject != "Reverify release resources for DEV-0018"
            or _git_changed_paths(repository, head, allow_empty=True) != ()
        ):
            raise ValueError("HEAD is not the exact empty DEV-0018 D1 boundary commit")
        source = head_parents[0]
        source_resolved, source_parents, source_subject = _git_commit_metadata(
            repository, source
        )
        if (
            source_resolved != source
            or source_parents != (DEV0018_RELEASE_EVIDENCE_COMMIT_V1,)
            or source_subject != "Repair WO40-I V2 publication verification"
        ):
            raise ValueError("D1 does not directly follow the DEV-0018 source repair")
        bundle = load_release_protocol_bundle(repository)
        candidate = verify_release_candidate_inputs(
            bundle,
            head,
            require_checkout=True,
        )
        if (
            candidate.candidate_commit != head
            or candidate.protocol_commit != DEV0018_PROTOCOL_COMMIT_V1
        ):
            raise ValueError("candidate protocol ownership differs from DEV-0018 order")
        runtime_source = _read_regular_file_snapshot(
            repository / "kirby2/release/history.py",
            maximum_bytes=_MAX_ROLLOVER_SOURCE_BYTES,
            label="runtime DEV-0018 rollover source",
        )
        if _git_blob(repository, head, "kirby2/release/history.py") != runtime_source.raw:
            raise ValueError("runtime DEV-0018 rollover source differs from authority")
    except (OSError, RuntimeError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "rollover requires the exact clean DEV-0018 source and D1 commits",
        ) from error
    return _ExecutionAuthority(candidate_commit=head, source_commit=source)


def _parse_evidence_document(raw: bytes) -> dict[str, object]:
    if raw.count(_EVIDENCE_START) != 1 or raw.count(_EVIDENCE_END) != 1:
        raise ValueError("historical evidence marker differs")
    payload_raw = raw.split(_EVIDENCE_START, 1)[1].split(_EVIDENCE_END, 1)[0]
    payload = json.loads(payload_raw.decode("utf-8"))
    if type(payload) is not dict or _canonical_json_bytes(payload) != payload_raw:
        raise ValueError("historical evidence payload is noncanonical")
    return payload


def _load_historical_evidence(repository: Path) -> tuple[tuple[str, bytes], ...]:
    documents: list[tuple[str, bytes]] = []
    for gate_id, expected_status in DEV0017_EVIDENCE_STATUS_BY_GATE_V1:
        relative = DEV0017_EVIDENCE_PATH_BY_GATE_V1[gate_id]
        raw = _git_blob(repository, DEV0017_RELEASE_EVIDENCE_COMMIT_V1, relative)
        if gate_id == "WO40-D1":
            if (
                hashlib.sha256(raw).hexdigest()
                != DEV0017_RESOURCE_PREFLIGHT_SHA256_V1
                or b"Status: `PASS`\n" not in raw
                or b"Protocol set SHA-256: `94f0050a592e3279a4b38b3d2e55b0ccfdc784202e67dca13e21a91fb631f9e8`\n"
                not in raw
                or b"WO40-D protocol commit: `8730ba83b4f54beb2308d7ef710b29e06e99a9fb`\n"
                not in raw
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                    "historical WO40-D1 resource preflight differs",
                )
            documents.append((relative, raw))
            continue
        try:
            payload = _parse_evidence_document(raw)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"historical {gate_id} evidence cannot be parsed",
            ) from error
        if (
            payload.get("schema_id") != "KIRBY2_RELEASE_GATE_EVIDENCE_V1"
            or payload.get("schema_version") != 1
            or payload.get("gate_id") != gate_id
            or payload.get("status") != expected_status
            or payload.get("candidate_commit") != DEV0017_SOURCE_CANDIDATE_COMMIT_V1
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"historical {gate_id} evidence identity differs",
            )
        documents.append((relative, raw))
    if len(documents) != len(DEV0017_EVIDENCE_STATUS_BY_GATE_V1):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "historical public-document inventory differs",
        )
    return tuple(documents)


def _load_dev0018_historical_evidence(
    repository: Path,
) -> tuple[tuple[str, bytes], ...]:
    documents: list[tuple[str, bytes]] = []
    for gate_id, expected_status in DEV0018_EVIDENCE_STATUS_BY_GATE_V1:
        if gate_id == "WO40-I":
            if expected_status != "NOT_RUN":
                raise RuntimeError("DEV-0018 WO40-I history status differs")
            continue
        relative = DEV0018_EVIDENCE_PATH_BY_GATE_V1[gate_id]
        raw = _git_blob(repository, DEV0018_RELEASE_EVIDENCE_COMMIT_V1, relative)
        if gate_id == "WO40-D1":
            if (
                b"Status: `PASS`\n" not in raw
                or b"Protocol set SHA-256: `6ea17041787f54887f6b01304c7ebe250267ff971492bf2970ae893796cd95a1`\n"
                not in raw
                or b"WO40-D protocol commit: `020da2c90c0f0000f822aad7c66538fe68c6c6e6`\n"
                not in raw
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                    "DEV-0018 predecessor WO40-D1 preflight differs",
                )
            documents.append((relative, raw))
            continue
        try:
            payload = _parse_evidence_document(raw)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"DEV-0018 predecessor {gate_id} evidence cannot be parsed",
            ) from error
        if (
            payload.get("schema_id") != "KIRBY2_RELEASE_GATE_EVIDENCE_V1"
            or payload.get("schema_version") != 1
            or payload.get("gate_id") != gate_id
            or payload.get("status") != expected_status
            or payload.get("candidate_commit")
            != DEV0018_SOURCE_CANDIDATE_COMMIT_V1
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"DEV-0018 predecessor {gate_id} evidence identity differs",
            )
        documents.append((relative, raw))
    if len(documents) != len(DEV0018_EVIDENCE_PATH_BY_GATE_V1):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "DEV-0018 predecessor public-document inventory differs",
        )
    try:
        prior_performance = _parse_evidence_document(
            _git_blob(
                repository,
                DEV0018_RELEASE_EVIDENCE_COMMIT_V1,
                DEV0017_EVIDENCE_PATH_BY_GATE_V1["WO40-I"],
            )
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "tracked WO40-I document cannot be parsed as historical evidence",
        ) from error
    if (
        prior_performance.get("gate_id") != "WO40-I"
        or prior_performance.get("status") != "FAIL"
        or prior_performance.get("candidate_commit")
        != DEV0017_SOURCE_CANDIDATE_COMMIT_V1
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "tracked WO40-I document is not the earlier historical failure",
        )
    return tuple(documents)


def _verify_dev0018_absent_performance_publication(
    inventory: Iterable[_InventoryEntry],
) -> None:
    forbidden = "artifacts/gate-evidence/wo40-i"
    if any(
        item.record.path == forbidden
        or item.record.path.startswith(f"{forbidden}/")
        for item in inventory
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "DEV-0018 predecessor unexpectedly contains WO40-I publication bytes",
        )


def _verify_evidence_anchors(
    inventory: Iterable[_InventoryEntry],
    documents: Iterable[tuple[str, bytes]],
) -> None:
    records_by_relative = {
        PurePosixPath(item.record.path).relative_to("artifacts").as_posix(): item.record
        for item in inventory
    }
    for relative, raw in documents:
        if relative == DEV0017_RESOURCE_PREFLIGHT_PATH_V1:
            continue
        payload = _parse_evidence_document(raw)
        records = payload.get("evidence_records")
        if type(records) is not list:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"{relative} evidence records differ",
            )
        for row in records:
            if type(row) is not dict or set(row) != {
                "evidence_id",
                "path",
                "sha256",
                "size",
            }:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                    f"{relative} evidence record fields differ",
                )
            path_value = row["path"]
            if type(path_value) is not str or not path_value.startswith(
                f"{_ACTIVE_STORE_RELATIVE}/"
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                    f"{relative} evidence record escapes the active store",
                )
            active_relative = path_value[len(_ACTIVE_STORE_RELATIVE) + 1 :]
            _require_relative_path(active_relative, "evidence record path")
            anchored = records_by_relative.get(active_relative)
            if (
                anchored is None
                or type(row["size"]) is not int
                or anchored.size != row["size"]
                or anchored.sha256
                != _require_sha256(row["sha256"], "evidence record SHA-256")
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                    f"{relative} evidence record bytes differ",
                )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _revalidate_pinned_directory(path: Path, descriptor: int, label: str) -> None:
    try:
        named = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} identity cannot be revalidated",
        ) from error
    if (
        not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or named.st_uid != os.getuid()
        or stat.S_IMODE(named.st_mode) & 0o022
        or _directory_identity(named) != _directory_identity(opened)
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} changed after it was pinned",
        )


def _open_plain_directory(
    path: Path,
    label: str,
    *,
    expected_device: int | None = None,
) -> tuple[int, os.stat_result]:
    expected = _plain_directory(path, label, expected_device=expected_device)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow directory support",
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
        )
        opened = os.fstat(descriptor)
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} cannot be pinned",
        ) from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) & 0o022
        or _directory_identity(opened) != _directory_identity(expected)
        or (expected_device is not None and opened.st_dev != expected_device)
    ):
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} changed while it was pinned",
        )
    return descriptor, opened


def _open_history_lock(state_descriptor: int, expected_device: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow lock support",
        )
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | nofollow
    created = False
    try:
        descriptor = os.open(
            _LOCK_NAME,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=state_descriptor,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(_LOCK_NAME, flags, dir_fd=state_descriptor)
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                "release-history lock cannot be opened safely",
            ) from error
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "release-history lock cannot be opened safely",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_dev != expected_device
            or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                "release-history lock identity is unsafe",
            )
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
        named = os.stat(_LOCK_NAME, dir_fd=state_descriptor, follow_symlinks=False)
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
            )
            != (
                named.st_dev,
                named.st_ino,
                named.st_mode,
                named.st_nlink,
                named.st_uid,
            )
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                "release-history lock identity is unsafe",
            )
        if created:
            os.fsync(state_descriptor)
    except ReleaseHistoryRefused:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "release-history lock identity cannot be verified",
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
            "another release-history rollover owns the lock",
        ) from error
    try:
        _revalidate_history_lock(state_descriptor, descriptor)
    except ReleaseHistoryRefused:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        raise
    return descriptor


def _revalidate_history_lock(state_descriptor: int, descriptor: int) -> None:
    try:
        locked = os.fstat(descriptor)
        named = os.stat(_LOCK_NAME, dir_fd=state_descriptor, follow_symlinks=False)
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "release-history lock name changed while held",
        ) from error
    if (
        not stat.S_ISREG(locked.st_mode)
        or locked.st_uid != os.getuid()
        or locked.st_nlink != 1
        or stat.S_IMODE(locked.st_mode) != 0o600
        or (
            locked.st_dev,
            locked.st_ino,
            locked.st_mode,
            locked.st_nlink,
            locked.st_uid,
        )
        != (
            named.st_dev,
            named.st_ino,
            named.st_mode,
            named.st_nlink,
            named.st_uid,
        )
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "release-history lock name changed while held",
        )


def _lock_release_directory(
    path: Path,
    label: str,
    *,
    expected_device: int,
) -> int:
    descriptor, _ = _open_plain_directory(
        path,
        label,
        expected_device=expected_device,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
            f"{label} is locked by another release writer",
        ) from error
    try:
        named = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as error:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} identity cannot be revalidated",
        ) from error
    if _directory_identity(named) != _directory_identity(opened):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            f"{label} changed during lock acquisition",
        )
    return descriptor


def _rename_exclusive_at(
    source_directory: int,
    source_name: bytes,
    destination_directory: int,
    destination_name: bytes,
) -> bool:
    """Move one directory without replacement using Darwin's atomic primitive."""

    if sys.platform != "darwin":
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
            "DEV-0017 rollover requires Darwin exclusive rename support",
        )
    global _RENAME_EXCLUSIVE_FUNCTION
    if _RENAME_EXCLUSIVE_FUNCTION is None:
        try:
            selected = ctypes.CDLL(None, use_errno=True).renameatx_np
        except AttributeError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                "Darwin exclusive rename is unavailable",
            ) from error
        selected.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        selected.restype = ctypes.c_int
        _RENAME_EXCLUSIVE_FUNCTION = selected
    rename_exclusive = _RENAME_EXCLUSIVE_FUNCTION
    if not callable(rename_exclusive):  # pragma: no cover - guarded assignment
        raise RuntimeError("Darwin exclusive rename binding is invalid")
    ctypes.set_errno(0)
    if (
        rename_exclusive(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
            _DARWIN_RENAME_EXCL,
        )
        == 0
    ):
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    if error_number == errno.EXDEV:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.CROSS_DEVICE_RENAME,
            "rollover rename crossed a filesystem boundary",
        )
    raise OSError(error_number, os.strerror(error_number))


def _write_exclusive_at(
    directory_descriptor: int,
    name: str,
    raw: bytes,
    mode: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow publication support",
        )
    descriptor = os.open(name, flags | nofollow, mode, dir_fd=directory_descriptor)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("exclusive history write made no progress")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(raw)
        ):
            raise OSError("exclusive history write identity differs")
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor, _ = _open_plain_directory(path, "fsync directory")
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_preparation_root(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
    allowed_files: frozenset[str],
) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow cleanup support",
        )
    _revalidate_pinned_directory(
        parent_path,
        parent_descriptor,
        "preparation parent root",
    )
    try:
        named_root = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
            "incomplete preparation root cannot be inspected",
        ) from error
    if not stat.S_ISDIR(named_root.st_mode):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
            "incomplete preparation root is not a directory",
        )
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
            "incomplete preparation root cannot be pinned",
        ) from error
    try:
        root_metadata = os.fstat(descriptor)
        if (
            root_metadata.st_uid != os.getuid()
            or root_metadata.st_dev != os.fstat(parent_descriptor).st_dev
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
            or _directory_identity(root_metadata)
            != _directory_identity(named_root)
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "incomplete preparation root identity is unsafe",
            )
        try:
            descriptor_path = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                bytes(1024),
            ).split(b"\0", 1)[0]
        except (AttributeError, OSError, ValueError) as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "incomplete preparation root path cannot be pinned",
            ) from error
        with os.scandir(descriptor) as entries:
            observed = tuple(entries)
        names = {entry.name for entry in observed}
        if not names.issubset(allowed_files):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "incomplete preparation root contains an unknown member",
            )
        for entry in observed:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                    "incomplete preparation member identity cannot be read",
                ) from error
            try:
                file_descriptor = os.open(
                    entry.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                    "incomplete preparation member cannot be pinned",
                ) from error
            try:
                opened = os.fstat(file_descriptor)
                try:
                    rebound = os.stat(
                        entry.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                        "incomplete preparation member changed before cleanup",
                    ) from error
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or opened.st_nlink != 1
                    or opened.st_dev != root_metadata.st_dev
                    or stat.S_IMODE(opened.st_mode) & 0o022
                    or _file_identity(opened) != _file_identity(metadata)
                    or _file_identity(opened) != _file_identity(rebound)
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                        "incomplete preparation member identity is unsafe",
                    )
                os.unlink(entry.name, dir_fd=descriptor)
                if os.fstat(file_descriptor).st_nlink != 0:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                        "incomplete preparation member was swapped during cleanup",
                    )
                try:
                    os.stat(
                        entry.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                        "incomplete preparation member was replaced during cleanup",
                    )
            finally:
                os.close(file_descriptor)
        os.fsync(descriptor)
        with os.scandir(descriptor) as entries:
            if any(True for _ in entries):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                    "incomplete preparation root changed during cleanup",
                )
        try:
            rebound_root = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "incomplete preparation root changed before removal",
            ) from error
        if _directory_identity(os.fstat(descriptor)) != _directory_identity(
            rebound_root
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "incomplete preparation root was replaced during cleanup",
            )
        os.rmdir(name, dir_fd=parent_descriptor)
        try:
            after_path = fcntl.fcntl(
                descriptor,
                fcntl.F_GETPATH,
                bytes(1024),
            ).split(b"\0", 1)[0]
        except (AttributeError, OSError, ValueError) as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "removed preparation root identity cannot be revalidated",
            ) from error
        if after_path != descriptor_path:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "a replacement preparation root was removed",
            )
        os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)


def _stage_rollover(
    plan: ReleaseHistoryRolloverPlanV1,
    evidence: tuple[tuple[str, bytes], ...],
    inventory: tuple[_InventoryEntry, ...],
    config: _RegularFileSnapshot,
    *,
    state_descriptor: int,
    history_parent_descriptor: int,
) -> ReleaseHistoryManifestV3:
    document_records: list[ReleaseHistoryFileV1] = []
    for relative, raw in evidence:
        document_records.append(
            ReleaseHistoryFileV1(
                path=relative,
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    files = tuple(
        sorted(
            (*document_records, *(item.record for item in inventory)),
            key=lambda item: item.path.encode("utf-8"),
        )
    )
    manifest = ReleaseHistoryManifestV3(
        release_evidence_commit=DEV0017_RELEASE_EVIDENCE_COMMIT_V1,
        source_candidate_commit=DEV0017_SOURCE_CANDIDATE_COMMIT_V1,
        gate_results=tuple(
            ReleaseHistoryGateResultV1(gate_id, status)
            for gate_id, status in DEV0017_EVIDENCE_STATUS_BY_GATE_V1
        ),
        files=files,
    )
    manifest_raw = manifest.canonical_bytes()
    if len(manifest_raw) > _MAX_MANIFEST_BYTES:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
            "history inventory exceeds the V3 manifest byte bound",
        )
    config_entry = next(
        item
        for item in inventory
        if item.record.path == f"artifacts/{_CONFIG_RELATIVE}"
    )
    config_record = config_entry.record
    if (
        config_record.size != config.size
        or config_record.sha256 != config.sha256
        or (
            config_entry.device,
            config_entry.inode,
            config_entry.mode,
            config_entry.nlink,
            config_entry.uid,
            config_entry.gid,
            config_entry.mtime_ns,
            config_entry.ctime_ns,
        )
        != (
            config.device,
            config.inode,
            config.mode,
            config.nlink,
            config.uid,
            config.gid,
            config.mtime_ns,
            config.ctime_ns,
        )
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            "provider configuration differs from the active inventory",
        )

    if _path_exists_no_follow(plan.next_active_build_root):
        _remove_preparation_root(
            state_descriptor,
            plan.repository_root / ".kirby2",
            _NEXT_ACTIVE_BUILD_NAME,
            frozenset({_CONFIG_RELATIVE}),
        )
    if not _path_exists_no_follow(plan.next_active_root):
        try:
            os.mkdir(_NEXT_ACTIVE_BUILD_NAME, 0o700, dir_fd=state_descriptor)
            next_build_descriptor, _ = _open_plain_directory(
                plan.next_active_build_root,
                "next-active build root",
                expected_device=os.fstat(state_descriptor).st_dev,
            )
            try:
                _write_exclusive_at(
                    next_build_descriptor,
                    _CONFIG_RELATIVE,
                    config.raw,
                    0o444,
                )
                os.fsync(next_build_descriptor)
                _revalidate_pinned_directory(
                    plan.next_active_build_root,
                    next_build_descriptor,
                    "next-active build root",
                )
                _revalidate_pinned_directory(
                    plan.repository_root / ".kirby2",
                    state_descriptor,
                    "release state root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(_NEXT_ACTIVE_BUILD_NAME),
                    state_descriptor,
                    os.fsencode(_NEXT_ACTIVE_NAME),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                        "next-active staging destination already exists",
                    )
                os.fsync(state_descriptor)
            finally:
                os.close(next_build_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                "next-active staging could not be completed",
            ) from error
    _verify_next_active_root(
        plan.next_active_root,
        expected_config=config_record,
        require_config_only=True,
    )

    if _path_exists_no_follow(plan.history_build_root):
        _remove_preparation_root(
            history_parent_descriptor,
            plan.history_parent,
            _HISTORY_BUILD_NAME,
            frozenset(
                {*DEV0017_EVIDENCE_PATH_BY_GATE_V1.values(), _MANIFEST_NAME}
            ),
        )
    if not _path_exists_no_follow(plan.history_staging_root):
        try:
            os.mkdir(_HISTORY_BUILD_NAME, 0o700, dir_fd=history_parent_descriptor)
            history_build_descriptor, _ = _open_plain_directory(
                plan.history_build_root,
                "history build root",
                expected_device=os.fstat(history_parent_descriptor).st_dev,
            )
            try:
                for relative, raw in evidence:
                    _write_exclusive_at(
                        history_build_descriptor,
                        relative,
                        raw,
                        0o444,
                    )
                _write_exclusive_at(
                    history_build_descriptor,
                    _MANIFEST_NAME,
                    manifest_raw,
                    0o444,
                )
                os.fsync(history_build_descriptor)
                _revalidate_pinned_directory(
                    plan.history_build_root,
                    history_build_descriptor,
                    "history build root",
                )
                _revalidate_pinned_directory(
                    plan.history_parent,
                    history_parent_descriptor,
                    "release history root",
                )
                if not _rename_exclusive_at(
                    history_parent_descriptor,
                    os.fsencode(_HISTORY_BUILD_NAME),
                    history_parent_descriptor,
                    os.fsencode(_HISTORY_STAGE_NAME),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                        "history staging destination already exists",
                    )
                os.fsync(history_parent_descriptor)
            finally:
                os.close(history_build_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                "history document staging could not be completed",
            ) from error
    staged = _load_staged_manifest(plan.history_staging_root)
    if staged != manifest:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
            "staged history manifest differs from the active inventory",
        )
    _verify_prepared_history_documents(plan.history_staging_root, staged)
    return manifest


def _stage_dev0018_rollover(
    plan: ReleaseHistoryRolloverPlanV1,
    evidence: tuple[tuple[str, bytes], ...],
    inventory: tuple[_InventoryEntry, ...],
    config: _RegularFileSnapshot,
    *,
    state_descriptor: int,
    history_parent_descriptor: int,
) -> ReleaseHistoryManifestV4:
    document_records = [
        ReleaseHistoryFileV1(
            path=relative,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        for relative, raw in evidence
    ]
    files = tuple(
        sorted(
            (*document_records, *(item.record for item in inventory)),
            key=lambda item: item.path.encode("utf-8"),
        )
    )
    manifest = ReleaseHistoryManifestV4(
        release_evidence_commit=DEV0018_RELEASE_EVIDENCE_COMMIT_V1,
        source_candidate_commit=DEV0018_SOURCE_CANDIDATE_COMMIT_V1,
        gate_results=tuple(
            ReleaseHistoryGateResultV2(gate_id, status)
            for gate_id, status in DEV0018_EVIDENCE_STATUS_BY_GATE_V1
        ),
        files=files,
    )
    manifest_raw = manifest.canonical_bytes()
    if len(manifest_raw) > _MAX_MANIFEST_BYTES:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
            "history inventory exceeds the V4 manifest byte bound",
        )
    config_entry = next(
        item
        for item in inventory
        if item.record.path == f"artifacts/{_CONFIG_RELATIVE}"
    )
    config_record = config_entry.record
    if (
        config_record.size != config.size
        or config_record.sha256 != config.sha256
        or (
            config_entry.device,
            config_entry.inode,
            config_entry.mode,
            config_entry.nlink,
            config_entry.uid,
            config_entry.gid,
            config_entry.mtime_ns,
            config_entry.ctime_ns,
        )
        != (
            config.device,
            config.inode,
            config.mode,
            config.nlink,
            config.uid,
            config.gid,
            config.mtime_ns,
            config.ctime_ns,
        )
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            "provider configuration differs from the active inventory",
        )

    if _path_exists_no_follow(plan.next_active_build_root):
        _remove_preparation_root(
            state_descriptor,
            plan.repository_root / ".kirby2",
            _DEV0018_NEXT_ACTIVE_BUILD_NAME,
            frozenset({_CONFIG_RELATIVE}),
        )
    if not _path_exists_no_follow(plan.next_active_root):
        try:
            os.mkdir(_DEV0018_NEXT_ACTIVE_BUILD_NAME, 0o700, dir_fd=state_descriptor)
            next_build_descriptor, _ = _open_plain_directory(
                plan.next_active_build_root,
                "DEV-0018 next-active build root",
                expected_device=os.fstat(state_descriptor).st_dev,
            )
            try:
                _write_exclusive_at(
                    next_build_descriptor,
                    _CONFIG_RELATIVE,
                    config.raw,
                    0o444,
                )
                os.fsync(next_build_descriptor)
                _revalidate_pinned_directory(
                    plan.next_active_build_root,
                    next_build_descriptor,
                    "DEV-0018 next-active build root",
                )
                _revalidate_pinned_directory(
                    plan.repository_root / ".kirby2",
                    state_descriptor,
                    "release state root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(_DEV0018_NEXT_ACTIVE_BUILD_NAME),
                    state_descriptor,
                    os.fsencode(_DEV0018_NEXT_ACTIVE_NAME),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                        "DEV-0018 next-active staging destination already exists",
                    )
                os.fsync(state_descriptor)
            finally:
                os.close(next_build_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                "DEV-0018 next-active staging could not be completed",
            ) from error
    _verify_next_active_root(
        plan.next_active_root,
        expected_config=config_record,
        require_config_only=True,
    )

    if _path_exists_no_follow(plan.history_build_root):
        _remove_preparation_root(
            history_parent_descriptor,
            plan.history_parent,
            _DEV0018_HISTORY_BUILD_NAME,
            frozenset(
                {*DEV0018_EVIDENCE_PATH_BY_GATE_V1.values(), _MANIFEST_NAME}
            ),
        )
    if not _path_exists_no_follow(plan.history_staging_root):
        try:
            os.mkdir(_DEV0018_HISTORY_BUILD_NAME, 0o700, dir_fd=history_parent_descriptor)
            history_build_descriptor, _ = _open_plain_directory(
                plan.history_build_root,
                "DEV-0018 history build root",
                expected_device=os.fstat(history_parent_descriptor).st_dev,
            )
            try:
                for relative, raw in evidence:
                    _write_exclusive_at(
                        history_build_descriptor,
                        relative,
                        raw,
                        0o444,
                    )
                _write_exclusive_at(
                    history_build_descriptor,
                    _MANIFEST_NAME,
                    manifest_raw,
                    0o444,
                )
                os.fsync(history_build_descriptor)
                _revalidate_pinned_directory(
                    plan.history_build_root,
                    history_build_descriptor,
                    "DEV-0018 history build root",
                )
                _revalidate_pinned_directory(
                    plan.history_parent,
                    history_parent_descriptor,
                    "release history root",
                )
                if not _rename_exclusive_at(
                    history_parent_descriptor,
                    os.fsencode(_DEV0018_HISTORY_BUILD_NAME),
                    history_parent_descriptor,
                    os.fsencode(_DEV0018_HISTORY_STAGE_NAME),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                        "DEV-0018 history staging destination already exists",
                    )
                os.fsync(history_parent_descriptor)
            finally:
                os.close(history_build_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.STAGING_CONFLICT,
                "DEV-0018 history document staging could not be completed",
            ) from error
    staged = _load_staged_manifest_v4(plan.history_staging_root)
    if staged != manifest:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
            "staged V4 history manifest differs from the active inventory",
        )
    _verify_prepared_history_documents(plan.history_staging_root, staged)
    return manifest


def _harden_tree(root: Path) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "platform lacks no-follow hardening support",
        )
    root_descriptor, root_metadata = _open_plain_directory(
        root,
        "history hardening root",
    )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow

    def harden_directory(directory_descriptor: int) -> None:
        try:
            with os.scandir(directory_descriptor) as iterator:
                entries = tuple(
                    sorted(iterator, key=lambda item: os.fsencode(item.name))
                )
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                "moved history directory cannot be scanned safely",
            ) from error
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                    "moved history member identity cannot be read",
                ) from error
            if stat.S_ISDIR(metadata.st_mode) and not entry.is_symlink():
                try:
                    child_descriptor = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                        "moved history directory cannot be pinned",
                    ) from error
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        opened.st_uid != os.getuid()
                        or opened.st_dev != root_metadata.st_dev
                        or stat.S_IMODE(opened.st_mode) & 0o022
                        or _directory_identity(opened) != _directory_identity(metadata)
                    ):
                        raise ReleaseHistoryRefused(
                            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
                            "moved history directory changed before hardening",
                        )
                    harden_directory(child_descriptor)
                    os.fchmod(child_descriptor, 0o555)
                    os.fsync(child_descriptor)
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(metadata.st_mode) and not entry.is_symlink():
                try:
                    file_descriptor = os.open(
                        entry.name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                        "moved history file cannot be pinned",
                    ) from error
                try:
                    opened = os.fstat(file_descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_uid != os.getuid()
                        or opened.st_nlink != 1
                        or opened.st_dev != root_metadata.st_dev
                        or (
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_mode,
                            opened.st_nlink,
                            opened.st_uid,
                            opened.st_gid,
                            opened.st_size,
                            opened.st_mtime_ns,
                            opened.st_ctime_ns,
                        )
                        != (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_mode,
                            metadata.st_nlink,
                            metadata.st_uid,
                            metadata.st_gid,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                        )
                    ):
                        raise ReleaseHistoryRefused(
                            ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                            "moved history file changed before hardening",
                        )
                    os.fchmod(file_descriptor, 0o444)
                    os.fsync(file_descriptor)
                finally:
                    os.close(file_descriptor)
            else:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.NONREGULAR_MEMBER,
                    "moved history contains a link or special member",
                )

    try:
        harden_directory(root_descriptor)
        os.fchmod(root_descriptor, 0o555)
        os.fsync(root_descriptor)
        _revalidate_pinned_directory(root, root_descriptor, "history hardening root")
    finally:
        os.close(root_descriptor)


def _load_staged_manifest(root: Path) -> ReleaseHistoryManifestV3:
    snapshot = _read_regular_file_snapshot(
        root / _MANIFEST_NAME,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        label="history manifest",
    )
    return ReleaseHistoryManifestV3.from_bytes(snapshot.raw)


def _load_staged_manifest_v4(root: Path) -> ReleaseHistoryManifestV4:
    snapshot = _read_regular_file_snapshot(
        root / _MANIFEST_NAME,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        label="partial-history manifest",
    )
    return ReleaseHistoryManifestV4.from_bytes(snapshot.raw)


def _verify_prepared_history_documents(
    root: Path,
    manifest: _ReleaseHistoryManifest,
) -> None:
    descriptor, _ = _open_plain_directory(root, "prepared history root")
    try:
        scanned = _scan_regular_files_at(
            descriptor,
            hash_files=True,
            required_file_mode=0o444,
        )
        manifest_raw = manifest.canonical_bytes()
        expected = {
            record.path: record
            for record in manifest.files
            if PurePosixPath(record.path).parts[0] != "artifacts"
        }
        expected[_MANIFEST_NAME] = ReleaseHistoryFileV1(
            path=_MANIFEST_NAME,
            size=len(manifest_raw),
            sha256=hashlib.sha256(manifest_raw).hexdigest(),
        )
        if {item.relative for item in scanned} != set(expected):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
                "prepared history document inventory differs",
            )
        for item in scanned:
            record = expected[item.relative]
            if item.sha256 is None or (
                item.size != record.size or item.sha256 != record.sha256
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.PREPARATION_INCOMPLETE,
                    f"prepared public document differs: {item.relative}",
                )
        _revalidate_pinned_directory(root, descriptor, "prepared history root")
    finally:
        os.close(descriptor)


def _verify_complete_snapshot_tree_at(
    root_descriptor: int,
    manifest: _ReleaseHistoryManifest,
    *,
    require_historical_modes: bool,
) -> None:
    manifest_raw = manifest.canonical_bytes()
    expected = {record.path: record for record in manifest.files}
    expected[_MANIFEST_NAME] = ReleaseHistoryFileV1(
        path=_MANIFEST_NAME,
        size=len(manifest_raw),
        sha256=hashlib.sha256(manifest_raw).hexdigest(),
    )
    scanned = _scan_regular_files_at(
        root_descriptor,
        hash_files=True,
        required_file_mode=0o444 if require_historical_modes else None,
        required_directory_mode=0o555 if require_historical_modes else None,
    )
    if {item.relative for item in scanned} != set(expected):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
            "history snapshot has an extra or missing file",
        )
    for item in scanned:
        record = expected[item.relative]
        if item.sha256 is None or (
            item.size != record.size or item.sha256 != record.sha256
        ):
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVE_STORE_CHANGED,
                f"history manifest bytes differ: {item.relative}",
            )


def _verify_complete_snapshot_tree(
    root: Path,
    manifest: _ReleaseHistoryManifest,
    *,
    require_historical_modes: bool,
) -> None:
    descriptor, _ = _open_plain_directory(root, "history snapshot root")
    try:
        _verify_complete_snapshot_tree_at(
            descriptor,
            manifest,
            require_historical_modes=require_historical_modes,
        )
        _revalidate_pinned_directory(root, descriptor, "history snapshot root")
    finally:
        os.close(descriptor)


def _provider_config_record(
    manifest: _ReleaseHistoryManifest,
) -> ReleaseHistoryFileV1:
    selected = tuple(
        item
        for item in manifest.files
        if item.path == f"artifacts/{_CONFIG_RELATIVE}"
    )
    if len(selected) != 1:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "history manifest provider configuration inventory differs",
        )
    return selected[0]


def _verify_next_active_root_at(
    root_descriptor: int,
    *,
    expected_config: ReleaseHistoryFileV1,
    require_config_only: bool,
) -> None:
    files = _scan_regular_files_at(root_descriptor, hash_files=False)
    relatives = tuple(item.relative for item in files)
    if _CONFIG_RELATIVE not in relatives or (
        require_config_only and relatives != (_CONFIG_RELATIVE,)
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "next active store provider inventory differs",
        )
    config_metadata = next(
        item for item in files if item.relative == _CONFIG_RELATIVE
    )
    config = _read_regular_file_snapshot_at(
        root_descriptor,
        _CONFIG_RELATIVE,
        maximum_bytes=_MAX_PROVIDER_CONFIG_BYTES,
        label="next provider configuration",
    )
    if (
        config.size != expected_config.size
        or config.sha256 != expected_config.sha256
        or stat.S_IMODE(config.mode) != 0o444
        or (
            config.device,
            config.inode,
            config.mode,
            config.nlink,
            config.uid,
            config.gid,
            config.size,
            config.mtime_ns,
            config.ctime_ns,
        )
        != (
            config_metadata.device,
            config_metadata.inode,
            config_metadata.mode,
            config_metadata.nlink,
            config_metadata.uid,
            config_metadata.gid,
            config_metadata.size,
            config_metadata.mtime_ns,
            config_metadata.ctime_ns,
        )
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "next provider configuration bytes or mode differ",
        )


def _verify_next_active_root(
    root: Path,
    *,
    expected_config: ReleaseHistoryFileV1,
    require_config_only: bool,
) -> None:
    descriptor, _ = _open_plain_directory(root, "next active store root")
    try:
        _verify_next_active_root_at(
            descriptor,
            expected_config=expected_config,
            require_config_only=require_config_only,
        )
        _revalidate_pinned_directory(root, descriptor, "next active store root")
    finally:
        os.close(descriptor)


def _receipt(
    plan: ReleaseHistoryRolloverPlanV1,
    manifest: _ReleaseHistoryManifest,
    disposition: str,
) -> ReleaseHistoryRolloverReceiptV1:
    raw = manifest.canonical_bytes()
    return ReleaseHistoryRolloverReceiptV1(
        release_evidence_commit=manifest.release_evidence_commit,
        source_candidate_commit=manifest.source_candidate_commit,
        history_root=os.fspath(plan.history_final_root),
        active_root=os.fspath(plan.active_root),
        file_count=len(manifest.files),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        disposition=disposition,
    )


def verify_release_history_snapshot_v3(
    history_root: Path | str,
) -> ReleaseHistoryManifestV3:
    """Deeply verify one completed V3 snapshot without executing a workload."""

    root = Path(history_root).absolute()
    repository = root.parent.parent.parent
    expected_root = (
        repository
        / _HISTORY_PARENT_RELATIVE
        / DEV0017_RELEASE_EVIDENCE_COMMIT_V1
    )
    if root != expected_root:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "history snapshot is outside its canonical repository location",
        )
    _plain_directory(repository, "repository root")
    state_metadata = _plain_directory(
        repository / ".kirby2",
        "release state root",
    )
    _plain_directory(
        repository / _HISTORY_PARENT_RELATIVE,
        "release history root",
        expected_device=state_metadata.st_dev,
    )
    _plain_directory(
        root,
        "release history snapshot",
        expected_device=state_metadata.st_dev,
    )
    manifest = _load_staged_manifest(root)
    if (
        manifest.release_evidence_commit != DEV0017_RELEASE_EVIDENCE_COMMIT_V1
        or manifest.source_candidate_commit != DEV0017_SOURCE_CANDIDATE_COMMIT_V1
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "history snapshot is not the DEV-0017 predecessor",
        )
    historical_documents = _load_historical_evidence(repository)
    if {relative for relative, _ in historical_documents} != set(
        DEV0017_EVIDENCE_PATH_BY_GATE_V1.values()
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "history snapshot public-document set differs",
        )
    for relative, expected_raw in historical_documents:
        observed = _read_regular_file_snapshot(
            root / relative,
            maximum_bytes=max(len(expected_raw), 1),
            label=f"historical {relative}",
        )
        if observed.raw != expected_raw:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"historical public document differs: {relative}",
            )
    _verify_complete_snapshot_tree(
        root,
        manifest,
        require_historical_modes=True,
    )
    return manifest


def verify_release_history_snapshot_v4(
    history_root: Path | str,
) -> ReleaseHistoryManifestV4:
    """Deeply verify one completed DEV-0018 partial-candidate snapshot."""

    root = Path(history_root).absolute()
    repository = root.parent.parent.parent
    expected_root = (
        repository
        / _HISTORY_PARENT_RELATIVE
        / DEV0018_RELEASE_EVIDENCE_COMMIT_V1
    )
    if root != expected_root:
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.PATH_IDENTITY_MISMATCH,
            "partial-history snapshot is outside its canonical repository location",
        )
    _plain_directory(repository, "repository root")
    state_metadata = _plain_directory(
        repository / ".kirby2",
        "release state root",
    )
    _plain_directory(
        repository / _HISTORY_PARENT_RELATIVE,
        "release history root",
        expected_device=state_metadata.st_dev,
    )
    _plain_directory(
        root,
        "partial release history snapshot",
        expected_device=state_metadata.st_dev,
    )
    manifest = _load_staged_manifest_v4(root)
    if (
        manifest.release_evidence_commit != DEV0018_RELEASE_EVIDENCE_COMMIT_V1
        or manifest.source_candidate_commit != DEV0018_SOURCE_CANDIDATE_COMMIT_V1
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "partial-history snapshot is not the DEV-0018 predecessor",
        )
    historical_documents = _load_dev0018_historical_evidence(repository)
    if {relative for relative, _ in historical_documents} != set(
        DEV0018_EVIDENCE_PATH_BY_GATE_V1.values()
    ):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
            "partial-history public-document set differs",
        )
    for relative, expected_raw in historical_documents:
        observed = _read_regular_file_snapshot(
            root / relative,
            maximum_bytes=max(len(expected_raw), 1),
            label=f"partial-history {relative}",
        )
        if observed.raw != expected_raw:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.HISTORICAL_EVIDENCE_MISMATCH,
                f"partial-history public document differs: {relative}",
            )
    _verify_complete_snapshot_tree(
        root,
        manifest,
        require_historical_modes=True,
    )
    return manifest


def execute_dev0017_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverReceiptV1:
    """Prepare and atomically roll the DEV-0017 predecessor into history.

    The active payload directory is never copied or deleted.  An interruption may
    leave a typed hidden staging state; callers must inspect that state and resume
    only through this function.  Ambiguous or partially prepared states refuse.
    """

    if sys.platform != "darwin" or not hasattr(fcntl, "F_GETPATH"):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
            "DEV-0017 rollover requires Darwin descriptor and rename primitives",
        )
    initial = inspect_dev0017_release_history_rollover(repository_root)
    state_descriptor, state_metadata = _open_plain_directory(
        initial.repository_root / ".kirby2",
        "release state root",
    )
    history_parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    release_descriptors: list[int] = []
    try:
        history_parent_descriptor, _ = _open_plain_directory(
            initial.history_parent,
            "release history root",
            expected_device=state_metadata.st_dev,
        )
        lock_descriptor = _open_history_lock(state_descriptor, state_metadata.st_dev)

        def revalidate_control_roots() -> None:
            _revalidate_pinned_directory(
                initial.repository_root / ".kirby2",
                state_descriptor,
                "release state root",
            )
            _revalidate_pinned_directory(
                initial.history_parent,
                history_parent_descriptor,
                "release history root",
            )
            _revalidate_history_lock(state_descriptor, lock_descriptor)

        def current_plan() -> ReleaseHistoryRolloverPlanV1:
            revalidate_control_roots()
            observed = inspect_dev0017_release_history_rollover(repository_root)
            revalidate_control_roots()
            return observed

        plan = current_plan()
        if plan.state is ReleaseHistoryRolloverStateV1.COMPLETE:
            manifest = verify_release_history_snapshot_v3(plan.history_final_root)
            _verify_next_active_root(
                plan.active_root,
                expected_config=_provider_config_record(manifest),
                require_config_only=False,
            )
            return _receipt(plan, manifest, "ALREADY_COMPLETE")
        if plan.state is ReleaseHistoryRolloverStateV1.AMBIGUOUS:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "rollover paths do not describe one closed lifecycle state",
            )

        _verify_execution_authority(plan.repository_root)

        inventory: tuple[_InventoryEntry, ...] | None = None
        manifest: ReleaseHistoryManifestV3
        old_release_descriptor: int | None = None
        next_release_descriptor: int | None = None
        if plan.state in {
            ReleaseHistoryRolloverStateV1.READY,
            ReleaseHistoryRolloverStateV1.PREPARATION_INCOMPLETE,
            ReleaseHistoryRolloverStateV1.PREPARED,
        }:
            if plan.state is ReleaseHistoryRolloverStateV1.READY:
                plan_dev0017_release_history_rollover(repository_root)
            old_release_descriptor = _lock_release_directory(
                plan.active_root,
                "active release root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(old_release_descriptor)
            plan = current_plan()
            if plan.state not in {
                ReleaseHistoryRolloverStateV1.READY,
                ReleaseHistoryRolloverStateV1.PREPARATION_INCOMPLETE,
                ReleaseHistoryRolloverStateV1.PREPARED,
            }:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                    "rollover state changed while the active store was locked",
                )
            evidence = _load_historical_evidence(plan.repository_root)
            config = _read_regular_file_snapshot_at(
                old_release_descriptor,
                _CONFIG_RELATIVE,
                maximum_bytes=_MAX_PROVIDER_CONFIG_BYTES,
                label="active provider configuration",
            )
            inventory = _inventory_active_store(old_release_descriptor)
            _verify_evidence_anchors(inventory, evidence)
            manifest = _stage_rollover(
                plan,
                evidence,
                inventory,
                config,
                state_descriptor=state_descriptor,
                history_parent_descriptor=history_parent_descriptor,
            )
            plan = current_plan()
        else:
            manifest = _load_staged_manifest(plan.history_staging_root)

        if plan.state is ReleaseHistoryRolloverStateV1.PREPARED:
            if inventory is None:  # pragma: no cover - pre-move states share one branch
                raise RuntimeError("prepared rollover lacks its active inventory")
            _verify_inventory_identity(old_release_descriptor, inventory)
            config_record = _provider_config_record(manifest)
            next_release_descriptor = _lock_release_directory(
                plan.next_active_root,
                "next-active staging root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(next_release_descriptor)
            _verify_next_active_root_at(
                next_release_descriptor,
                expected_config=config_record,
                require_config_only=True,
            )
            _verify_execution_authority(plan.repository_root)
            history_stage_descriptor, _ = _open_plain_directory(
                plan.history_staging_root,
                "history staging root",
                expected_device=state_metadata.st_dev,
            )
            try:
                revalidate_control_roots()
                _revalidate_pinned_directory(
                    plan.active_root,
                    old_release_descriptor,
                    "active release root",
                )
                _revalidate_pinned_directory(
                    plan.next_active_root,
                    next_release_descriptor,
                    "next-active staging root",
                )
                _revalidate_pinned_directory(
                    plan.history_staging_root,
                    history_stage_descriptor,
                    "history staging root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(Path(_ACTIVE_STORE_RELATIVE).name),
                    history_stage_descriptor,
                    b"artifacts",
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                        "history artifact destination already exists",
                    )
                os.fsync(history_stage_descriptor)
                os.fsync(state_descriptor)
            except ReleaseHistoryRefused:
                raise
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "active release could not be atomically quarantined",
                ) from error
            finally:
                os.close(history_stage_descriptor)
            plan = current_plan()

        quarantine_verified = False
        if plan.state is ReleaseHistoryRolloverStateV1.OLD_STORE_QUARANTINED:
            if old_release_descriptor is None:
                old_release_descriptor = _lock_release_directory(
                    plan.history_staging_root / "artifacts",
                    "quarantined active release root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(old_release_descriptor)
            if next_release_descriptor is None:
                next_release_descriptor = _lock_release_directory(
                    plan.next_active_root,
                    "next-active staging root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(next_release_descriptor)
            _verify_complete_snapshot_tree(
                plan.history_staging_root,
                manifest,
                require_historical_modes=False,
            )
            quarantine_verified = True
            _verify_next_active_root_at(
                next_release_descriptor,
                expected_config=_provider_config_record(manifest),
                require_config_only=True,
            )
            _verify_execution_authority(plan.repository_root)
            try:
                revalidate_control_roots()
                _revalidate_pinned_directory(
                    plan.history_staging_root / "artifacts",
                    old_release_descriptor,
                    "quarantined active release root",
                )
                _revalidate_pinned_directory(
                    plan.next_active_root,
                    next_release_descriptor,
                    "next-active staging root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(_NEXT_ACTIVE_NAME),
                    state_descriptor,
                    os.fsencode(Path(_ACTIVE_STORE_RELATIVE).name),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                        "active release destination already exists",
                    )
                os.fsync(state_descriptor)
            except ReleaseHistoryRefused:
                raise
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "the config-only active store could not be activated",
                ) from error
            plan = current_plan()

        if plan.state is not ReleaseHistoryRolloverStateV1.ACTIVE_REPLACED_HISTORY_PENDING:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                f"rollover stopped in state {plan.state.value}",
            )
        if next_release_descriptor is None:
            next_release_descriptor = _lock_release_directory(
                plan.active_root,
                "replacement active release root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(next_release_descriptor)
        config_record = _provider_config_record(manifest)
        _verify_next_active_root_at(
            next_release_descriptor,
            expected_config=config_record,
            require_config_only=True,
        )
        if not quarantine_verified:
            if old_release_descriptor is None:
                old_release_descriptor = _lock_release_directory(
                    plan.history_staging_root / "artifacts",
                    "quarantined active release root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(old_release_descriptor)
            _verify_complete_snapshot_tree(
                plan.history_staging_root,
                manifest,
                require_historical_modes=False,
            )
        if old_release_descriptor is None or (
            os.fstat(old_release_descriptor).st_dev,
            os.fstat(old_release_descriptor).st_ino,
        ) == (
            os.fstat(next_release_descriptor).st_dev,
            os.fstat(next_release_descriptor).st_ino,
        ):
            raise RuntimeError("historical and replacement store locks are not distinct")
        _harden_tree(plan.history_staging_root)
        _verify_complete_snapshot_tree(
            plan.history_staging_root,
            manifest,
            require_historical_modes=True,
        )
        _verify_execution_authority(plan.repository_root)
        history_stage_descriptor, _ = _open_plain_directory(
            plan.history_staging_root,
            "hardened history staging root",
            expected_device=state_metadata.st_dev,
        )
        try:
            revalidate_control_roots()
            _revalidate_pinned_directory(
                plan.active_root,
                next_release_descriptor,
                "replacement active release root",
            )
            _revalidate_pinned_directory(
                plan.history_staging_root / "artifacts",
                old_release_descriptor,
                "quarantined active release root",
            )
            _revalidate_pinned_directory(
                plan.history_staging_root,
                history_stage_descriptor,
                "hardened history staging root",
            )
            if not _rename_exclusive_at(
                history_parent_descriptor,
                os.fsencode(_HISTORY_STAGE_NAME),
                history_parent_descriptor,
                os.fsencode(DEV0017_RELEASE_EVIDENCE_COMMIT_V1),
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "final history destination already exists",
                )
            os.fsync(history_parent_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                "verified history could not be atomically published",
            ) from error
        finally:
            os.close(history_stage_descriptor)
        final_plan = current_plan()
        if final_plan.state is not ReleaseHistoryRolloverStateV1.COMPLETE:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "rollover did not reach its complete state",
            )
        final_manifest = verify_release_history_snapshot_v3(
            final_plan.history_final_root
        )
        _verify_next_active_root_at(
            next_release_descriptor,
            expected_config=_provider_config_record(final_manifest),
            require_config_only=True,
        )
        return _receipt(final_plan, final_manifest, "ROLLED_OVER")
    finally:
        cleanup_errors: list[OSError] = []
        for descriptor in reversed(tuple(dict.fromkeys(release_descriptors))):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                cleanup_errors.append(error)
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError as error:
                cleanup_errors.append(error)
            try:
                os.close(lock_descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        if history_parent_descriptor is not None:
            try:
                os.close(history_parent_descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        try:
            os.close(state_descriptor)
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors and sys.exception() is None:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                "rollover locks or directory descriptors could not be closed",
            ) from cleanup_errors[0]


def execute_dev0018_release_history_rollover(
    repository_root: Path | str,
) -> ReleaseHistoryRolloverReceiptV1:
    """Prepare and atomically roll the DEV-0018 predecessor into history.

    The active payload directory is never copied or deleted.  An interruption may
    leave a typed hidden staging state; callers must inspect that state and resume
    only through this function.  Ambiguous or partially prepared states refuse.
    """

    if sys.platform != "darwin" or not hasattr(fcntl, "F_GETPATH"):
        raise ReleaseHistoryRefused(
            ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
            "DEV-0018 rollover requires Darwin descriptor and rename primitives",
        )
    initial = inspect_dev0018_release_history_rollover(repository_root)
    state_descriptor, state_metadata = _open_plain_directory(
        initial.repository_root / ".kirby2",
        "release state root",
    )
    history_parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    release_descriptors: list[int] = []
    try:
        history_parent_descriptor, _ = _open_plain_directory(
            initial.history_parent,
            "release history root",
            expected_device=state_metadata.st_dev,
        )
        lock_descriptor = _open_history_lock(state_descriptor, state_metadata.st_dev)

        def revalidate_control_roots() -> None:
            _revalidate_pinned_directory(
                initial.repository_root / ".kirby2",
                state_descriptor,
                "release state root",
            )
            _revalidate_pinned_directory(
                initial.history_parent,
                history_parent_descriptor,
                "release history root",
            )
            _revalidate_history_lock(state_descriptor, lock_descriptor)

        def current_plan() -> ReleaseHistoryRolloverPlanV1:
            revalidate_control_roots()
            observed = inspect_dev0018_release_history_rollover(repository_root)
            revalidate_control_roots()
            return observed

        plan = current_plan()
        if plan.state is ReleaseHistoryRolloverStateV1.COMPLETE:
            manifest = verify_release_history_snapshot_v4(plan.history_final_root)
            _verify_next_active_root(
                plan.active_root,
                expected_config=_provider_config_record(manifest),
                require_config_only=False,
            )
            return _receipt(plan, manifest, "ALREADY_COMPLETE")
        if plan.state is ReleaseHistoryRolloverStateV1.AMBIGUOUS:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "rollover paths do not describe one closed lifecycle state",
            )

        _verify_dev0018_execution_authority(plan.repository_root)

        inventory: tuple[_InventoryEntry, ...] | None = None
        manifest: ReleaseHistoryManifestV4
        old_release_descriptor: int | None = None
        next_release_descriptor: int | None = None
        if plan.state in {
            ReleaseHistoryRolloverStateV1.READY,
            ReleaseHistoryRolloverStateV1.PREPARATION_INCOMPLETE,
            ReleaseHistoryRolloverStateV1.PREPARED,
        }:
            if plan.state is ReleaseHistoryRolloverStateV1.READY:
                plan_dev0018_release_history_rollover(repository_root)
            old_release_descriptor = _lock_release_directory(
                plan.active_root,
                "active release root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(old_release_descriptor)
            plan = current_plan()
            if plan.state not in {
                ReleaseHistoryRolloverStateV1.READY,
                ReleaseHistoryRolloverStateV1.PREPARATION_INCOMPLETE,
                ReleaseHistoryRolloverStateV1.PREPARED,
            }:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                    "rollover state changed while the active store was locked",
                )
            evidence = _load_dev0018_historical_evidence(plan.repository_root)
            config = _read_regular_file_snapshot_at(
                old_release_descriptor,
                _CONFIG_RELATIVE,
                maximum_bytes=_MAX_PROVIDER_CONFIG_BYTES,
                label="active provider configuration",
            )
            inventory = _inventory_active_store(old_release_descriptor)
            _verify_dev0018_absent_performance_publication(inventory)
            _verify_evidence_anchors(inventory, evidence)
            manifest = _stage_dev0018_rollover(
                plan,
                evidence,
                inventory,
                config,
                state_descriptor=state_descriptor,
                history_parent_descriptor=history_parent_descriptor,
            )
            plan = current_plan()
        else:
            manifest = _load_staged_manifest_v4(plan.history_staging_root)

        if plan.state is ReleaseHistoryRolloverStateV1.PREPARED:
            if inventory is None:  # pragma: no cover - pre-move states share one branch
                raise RuntimeError("prepared rollover lacks its active inventory")
            _verify_inventory_identity(old_release_descriptor, inventory)
            config_record = _provider_config_record(manifest)
            next_release_descriptor = _lock_release_directory(
                plan.next_active_root,
                "next-active staging root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(next_release_descriptor)
            _verify_next_active_root_at(
                next_release_descriptor,
                expected_config=config_record,
                require_config_only=True,
            )
            _verify_dev0018_execution_authority(plan.repository_root)
            history_stage_descriptor, _ = _open_plain_directory(
                plan.history_staging_root,
                "history staging root",
                expected_device=state_metadata.st_dev,
            )
            try:
                revalidate_control_roots()
                _revalidate_pinned_directory(
                    plan.active_root,
                    old_release_descriptor,
                    "active release root",
                )
                _revalidate_pinned_directory(
                    plan.next_active_root,
                    next_release_descriptor,
                    "next-active staging root",
                )
                _revalidate_pinned_directory(
                    plan.history_staging_root,
                    history_stage_descriptor,
                    "history staging root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(Path(_ACTIVE_STORE_RELATIVE).name),
                    history_stage_descriptor,
                    b"artifacts",
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                        "history artifact destination already exists",
                    )
                os.fsync(history_stage_descriptor)
                os.fsync(state_descriptor)
            except ReleaseHistoryRefused:
                raise
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "active release could not be atomically quarantined",
                ) from error
            finally:
                os.close(history_stage_descriptor)
            plan = current_plan()

        quarantine_verified = False
        if plan.state is ReleaseHistoryRolloverStateV1.OLD_STORE_QUARANTINED:
            if old_release_descriptor is None:
                old_release_descriptor = _lock_release_directory(
                    plan.history_staging_root / "artifacts",
                    "quarantined active release root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(old_release_descriptor)
            if next_release_descriptor is None:
                next_release_descriptor = _lock_release_directory(
                    plan.next_active_root,
                    "next-active staging root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(next_release_descriptor)
            _verify_complete_snapshot_tree(
                plan.history_staging_root,
                manifest,
                require_historical_modes=False,
            )
            quarantine_verified = True
            _verify_next_active_root_at(
                next_release_descriptor,
                expected_config=_provider_config_record(manifest),
                require_config_only=True,
            )
            _verify_dev0018_execution_authority(plan.repository_root)
            try:
                revalidate_control_roots()
                _revalidate_pinned_directory(
                    plan.history_staging_root / "artifacts",
                    old_release_descriptor,
                    "quarantined active release root",
                )
                _revalidate_pinned_directory(
                    plan.next_active_root,
                    next_release_descriptor,
                    "next-active staging root",
                )
                if not _rename_exclusive_at(
                    state_descriptor,
                    os.fsencode(_DEV0018_NEXT_ACTIVE_NAME),
                    state_descriptor,
                    os.fsencode(Path(_ACTIVE_STORE_RELATIVE).name),
                ):
                    raise ReleaseHistoryRefused(
                        ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                        "active release destination already exists",
                    )
                os.fsync(state_descriptor)
            except ReleaseHistoryRefused:
                raise
            except OSError as error:
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "the config-only active store could not be activated",
                ) from error
            plan = current_plan()

        if plan.state is not ReleaseHistoryRolloverStateV1.ACTIVE_REPLACED_HISTORY_PENDING:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                f"rollover stopped in state {plan.state.value}",
            )
        if next_release_descriptor is None:
            next_release_descriptor = _lock_release_directory(
                plan.active_root,
                "replacement active release root",
                expected_device=state_metadata.st_dev,
            )
            release_descriptors.append(next_release_descriptor)
        config_record = _provider_config_record(manifest)
        _verify_next_active_root_at(
            next_release_descriptor,
            expected_config=config_record,
            require_config_only=True,
        )
        if not quarantine_verified:
            if old_release_descriptor is None:
                old_release_descriptor = _lock_release_directory(
                    plan.history_staging_root / "artifacts",
                    "quarantined active release root",
                    expected_device=state_metadata.st_dev,
                )
                release_descriptors.append(old_release_descriptor)
            _verify_complete_snapshot_tree(
                plan.history_staging_root,
                manifest,
                require_historical_modes=False,
            )
        if old_release_descriptor is None or (
            os.fstat(old_release_descriptor).st_dev,
            os.fstat(old_release_descriptor).st_ino,
        ) == (
            os.fstat(next_release_descriptor).st_dev,
            os.fstat(next_release_descriptor).st_ino,
        ):
            raise RuntimeError("historical and replacement store locks are not distinct")
        _harden_tree(plan.history_staging_root)
        _verify_complete_snapshot_tree(
            plan.history_staging_root,
            manifest,
            require_historical_modes=True,
        )
        _verify_dev0018_execution_authority(plan.repository_root)
        history_stage_descriptor, _ = _open_plain_directory(
            plan.history_staging_root,
            "hardened history staging root",
            expected_device=state_metadata.st_dev,
        )
        try:
            revalidate_control_roots()
            _revalidate_pinned_directory(
                plan.active_root,
                next_release_descriptor,
                "replacement active release root",
            )
            _revalidate_pinned_directory(
                plan.history_staging_root / "artifacts",
                old_release_descriptor,
                "quarantined active release root",
            )
            _revalidate_pinned_directory(
                plan.history_staging_root,
                history_stage_descriptor,
                "hardened history staging root",
            )
            if not _rename_exclusive_at(
                history_parent_descriptor,
                os.fsencode(_DEV0018_HISTORY_STAGE_NAME),
                history_parent_descriptor,
                os.fsencode(DEV0018_RELEASE_EVIDENCE_COMMIT_V1),
            ):
                raise ReleaseHistoryRefused(
                    ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                    "final history destination already exists",
                )
            os.fsync(history_parent_descriptor)
        except ReleaseHistoryRefused:
            raise
        except OSError as error:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                "verified history could not be atomically published",
            ) from error
        finally:
            os.close(history_stage_descriptor)
        final_plan = current_plan()
        if final_plan.state is not ReleaseHistoryRolloverStateV1.COMPLETE:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.RECOVERY_REQUIRED,
                "rollover did not reach its complete state",
            )
        final_manifest = verify_release_history_snapshot_v4(
            final_plan.history_final_root
        )
        _verify_next_active_root_at(
            next_release_descriptor,
            expected_config=_provider_config_record(final_manifest),
            require_config_only=True,
        )
        return _receipt(final_plan, final_manifest, "ROLLED_OVER")
    finally:
        cleanup_errors: list[OSError] = []
        for descriptor in reversed(tuple(dict.fromkeys(release_descriptors))):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                cleanup_errors.append(error)
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError as error:
                cleanup_errors.append(error)
            try:
                os.close(lock_descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        if history_parent_descriptor is not None:
            try:
                os.close(history_parent_descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        try:
            os.close(state_descriptor)
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors and sys.exception() is None:
            raise ReleaseHistoryRefused(
                ReleaseHistoryRefusalCodeV1.ACTIVATION_FAILED,
                "rollover locks or directory descriptors could not be closed",
            ) from cleanup_errors[0]


__all__ = [
    "DEV0017_EVIDENCE_PATH_BY_GATE_V1",
    "DEV0017_EVIDENCE_STATUS_BY_GATE_V1",
    "DEV0017_RELEASE_EVIDENCE_COMMIT_V1",
    "DEV0017_RESOURCE_PREFLIGHT_PATH_V1",
    "DEV0017_RESOURCE_PREFLIGHT_SHA256_V1",
    "DEV0017_SOURCE_CANDIDATE_COMMIT_V1",
    "DEV0018_EVIDENCE_PATH_BY_GATE_V1",
    "DEV0018_EVIDENCE_STATUS_BY_GATE_V1",
    "DEV0018_PROTOCOL_COMMIT_V1",
    "DEV0018_RELEASE_EVIDENCE_COMMIT_V1",
    "DEV0018_SOURCE_CANDIDATE_COMMIT_V1",
    "RELEASE_HISTORY_ROLLOVER_POLICY_ID_V1",
    "RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V3",
    "RELEASE_HISTORY_SNAPSHOT_SCHEMA_ID_V4",
    "ReleaseHistoryFileV1",
    "ReleaseHistoryGateResultV1",
    "ReleaseHistoryGateResultV2",
    "ReleaseHistoryManifestV3",
    "ReleaseHistoryManifestV4",
    "ReleaseHistoryRefusalCodeV1",
    "ReleaseHistoryRefused",
    "ReleaseHistoryRolloverPlanV1",
    "ReleaseHistoryRolloverReceiptV1",
    "ReleaseHistoryRolloverStateV1",
    "execute_dev0017_release_history_rollover",
    "execute_dev0018_release_history_rollover",
    "inspect_dev0017_release_history_rollover",
    "inspect_dev0018_release_history_rollover",
    "plan_dev0017_release_history_rollover",
    "plan_dev0018_release_history_rollover",
    "verify_release_history_snapshot_v3",
    "verify_release_history_snapshot_v4",
]
