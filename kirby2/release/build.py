"""Offline release protocol loading, build planning, and resource preflight.

The public commands in this module are fail-closed.  Before WO40-E freezes a source
tree they can parse and digest every preregistered input, explain exact missing
resources, and produce a deterministic dispatch plan, but they cannot accidentally
build from the working tree or fetch a dependency.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as importlib_metadata
import importlib.util as importlib_util
import json
import os
import platform
import re
import shutil
import site
import stat
import subprocess
import sys
import sysconfig
import tomllib
import zlib
from dataclasses import dataclass
from enum import Enum
from importlib.machinery import PathFinder
from pathlib import Path
from typing import ClassVar

from kirby2.packs.formats import (
    canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)

from .first_run import RELEASE_STARTER_SET_ID_V1, build_release_starter_set
from .licenses import ReleaseRequirementsLockV1
from .manifest import (
    RELEASE_ARTIFACT_ROWS_V1,
    RELEASE_PROTOCOL_PATHS_V1,
    RELEASE_VERSION_V1,
    ReleaseArtifactIndexV1,
    ReleaseLogicalBuildProjectionV1,
    ReleaseProtocolFileV1,
    ReleaseRuntimeV1,
)
from .packaging import (
    RELEASE_SOURCE_CLASS_ORDER_V1,
    ReleaseSourceClassV1,
    canonical_gzip_bytes,
    normalize_release_path,
)
from .performance import (
    RELEASE_ARTIFACT_PASS_BYTES_V1,
    RELEASE_ARTIFACT_WARNING_BYTES_V1,
    RELEASE_AUXILIARY_THRESHOLDS_V1,
    RELEASE_BENCHMARK_FIXTURES_ID_V1,
    RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
    RELEASE_PERFORMANCE_CELL_ORDER_V1,
    RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
    RELEASE_PERFORMANCE_ROOTS_V1,
    RELEASE_PERFORMANCE_WORKER_COUNT_V1,
    RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1,
    RELEASE_RUNNER_SOURCE_POLICY_ID_V1,
    RELEASE_TOTAL_WALL_LIMIT_NS_V1,
    RunnerSourceEntryV1,
    RunnerSourceTreeV1,
    auxiliary_performance_templates,
)
from .qualification import (
    ReleasePlatformTargetV1,
    ReleasePlatformsV1,
    ReleaseQualificationProtocolV1,
)


RELEASE_BUILD_PROTOCOL_SCHEMA_ID_V1 = "KIRBY2_RELEASE_BUILD_PROTOCOL_V1"
RELEASE_RESOURCE_PREFLIGHT_SCHEMA_ID_V1 = "KIRBY2_RELEASE_RESOURCE_PREFLIGHT_V1"
RELEASE_COMMAND_OUTCOME_SCHEMA_ID_V1 = "KIRBY2_RELEASE_COMMAND_OUTCOME_V1"
RELEASE_ARTIFACT_LAYOUT_SCHEMA_ID_V1 = "KIRBY2_RELEASE_ARTIFACT_LAYOUT_V1"
RELEASE_BUILD_FRONTENDS_V1 = {
    "project_wheel": [
        "./.venv/bin/pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
    ],
    "source_inventory": ["git", "archive"],
    "archive_encoder": ["KIRBY2_STDLIB_USTAR_GZIP_V1"],
}

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseCommandStatusV1(str, Enum):
    READY = "READY"
    COMPLETE = "COMPLETE"
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    NOT_EXERCISED = "NOT_EXERCISED"
    REFUSED = "REFUSED"
    FAIL = "FAIL"


class ReleaseBuildRefusalCodeV1(str, Enum):
    PROTOCOL_MISSING = "PROTOCOL_MISSING"
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    SOURCE_LOCK_MISSING = "SOURCE_LOCK_MISSING"
    SOURCE_LOCK_MISMATCH = "SOURCE_LOCK_MISMATCH"
    CANDIDATE_COMMIT_INVALID = "CANDIDATE_COMMIT_INVALID"
    CANDIDATE_SOURCE_DIRTY = "CANDIDATE_SOURCE_DIRTY"
    CANDIDATE_PROTOCOL_MISMATCH = "CANDIDATE_PROTOCOL_MISMATCH"
    RESOURCE_PREFLIGHT_INCOMPLETE = "RESOURCE_PREFLIGHT_INCOMPLETE"
    WHEEL_MISSING = "WHEEL_MISSING"
    WHEEL_DIGEST_MISMATCH = "WHEEL_DIGEST_MISMATCH"
    CLEAN_PROVIDER_MISSING = "CLEAN_PROVIDER_MISSING"
    ARTIFACT_INDEX_MISSING = "ARTIFACT_INDEX_MISSING"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    NETWORK_POLICY_MISMATCH = "NETWORK_POLICY_MISMATCH"
    BUILD_ATTEMPT_FAILED = "BUILD_ATTEMPT_FAILED"
    BUILD_ATTEMPT_TIMEOUT = "BUILD_ATTEMPT_TIMEOUT"
    BUILD_NONDETERMINISTIC = "BUILD_NONDETERMINISTIC"
    CANDIDATE_INPUT_DRIFT = "CANDIDATE_INPUT_DRIFT"
    RESOURCE_INPUT_DRIFT = "RESOURCE_INPUT_DRIFT"
    ARTIFACT_SEMANTIC_MISMATCH = "ARTIFACT_SEMANTIC_MISMATCH"
    ARTIFACT_STORE_UNSAFE = "ARTIFACT_STORE_UNSAFE"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    FUTURE_SOURCE_INPUT_MISSING = "FUTURE_SOURCE_INPUT_MISSING"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"


class ReleaseBuildRefused(ValueError):
    def __init__(self, code: ReleaseBuildRefusalCodeV1, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class ReleaseCandidateInputsV1:
    candidate_commit: str
    candidate_tree: str
    source_date_epoch: int
    tree_entry_count: int
    source_entry_count: int
    source_lock_object_id: str
    source_lock_sha256: str
    source_manifest_sha256: str
    protocol_files: tuple[ReleaseProtocolFileV1, ...]
    protocol_commit: str
    protocol_set_sha256: str
    resource_preflight_sha256: str
    logical_build_id: str
    tracked_tree_clean: bool

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("verified candidate commit is invalid")
        if _COMMIT.fullmatch(self.candidate_tree) is None:
            raise ValueError("verified candidate tree is invalid")
        if type(self.source_date_epoch) is not int or self.source_date_epoch < 0:
            raise ValueError("candidate source-date epoch must be nonnegative")
        for label, value in (
            ("candidate tree entry count", self.tree_entry_count),
            ("candidate source entry count", self.source_entry_count),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be positive")
        require_sha256(self.source_lock_sha256, "candidate source-lock digest")
        if _COMMIT.fullmatch(self.source_lock_object_id) is None:
            raise ValueError("candidate source-lock object ID is invalid")
        require_sha256(
            self.source_manifest_sha256,
            "candidate source-manifest digest",
        )
        require_sha256(self.protocol_set_sha256, "candidate protocol-set digest")
        if type(self.protocol_files) is not tuple or any(
            type(item) is not ReleaseProtocolFileV1 for item in self.protocol_files
        ):
            raise TypeError("candidate protocol files must use exact V1 records")
        if tuple(item.path for item in self.protocol_files) != RELEASE_PROTOCOL_PATHS_V1:
            raise ValueError("candidate protocol file projection differs")
        if _COMMIT.fullmatch(self.protocol_commit) is None:
            raise ValueError("candidate protocol commit is invalid")
        require_sha256(
            self.resource_preflight_sha256,
            "candidate resource-preflight digest",
        )
        if not self.logical_build_id.startswith("kirby2-release-"):
            raise ValueError("candidate logical build ID has the wrong namespace")
        require_sha256(
            self.logical_build_id.removeprefix("kirby2-release-"),
            "candidate logical build digest",
        )
        if self.tracked_tree_clean is not True:
            raise ValueError("verified candidate requires a clean tracked tree")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_commit": self.candidate_commit,
            "candidate_tree": self.candidate_tree,
            "logical_build_id": self.logical_build_id,
            "protocol_commit": self.protocol_commit,
            "protocol_files": [item.as_dict() for item in self.protocol_files],
            "protocol_set_sha256": self.protocol_set_sha256,
            "resource_preflight_sha256": self.resource_preflight_sha256,
            "source_date_epoch": self.source_date_epoch,
            "source_entry_count": self.source_entry_count,
            "source_lock_object_id": self.source_lock_object_id,
            "source_lock_sha256": self.source_lock_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "tracked_tree_clean": self.tracked_tree_clean,
            "tree_entry_count": self.tree_entry_count,
        }


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 protocol")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute Path")
    resolved = path.resolve(strict=False)
    if path != resolved:
        raise ValueError(f"{label} must be supplied already resolved")
    return path


@dataclass(frozen=True, slots=True)
class ReleaseLayoutArtifactV1:
    artifact_id: str
    artifact_form: str
    target: str
    embedded_manifest: bool
    archive_root: str | None

    def __post_init__(self) -> None:
        _text(self.artifact_id, "layout artifact ID", 128)
        _text(self.artifact_form, "layout artifact form", 128)
        _text(self.target, "layout artifact target", 128)
        if type(self.embedded_manifest) is not bool:
            raise TypeError("layout embedded-manifest flag must be Boolean")
        if self.archive_root is not None:
            normalize_release_path(self.archive_root, label="layout archive root")

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_root": self.archive_root,
            "artifact_form": self.artifact_form,
            "artifact_id": self.artifact_id,
            "embedded_manifest": self.embedded_manifest,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseLayoutArtifactV1":
        row = _exact(
            value,
            {"artifact_id", "artifact_form", "target", "embedded_manifest", "archive_root"},
            "layout artifact",
        )
        return cls(
            artifact_id=_text(row["artifact_id"], "layout artifact ID", 128),
            artifact_form=_text(row["artifact_form"], "layout artifact form", 128),
            target=_text(row["target"], "layout artifact target", 128),
            embedded_manifest=row["embedded_manifest"],  # type: ignore[arg-type]
            archive_root=(
                None
                if row["archive_root"] is None
                else _text(row["archive_root"], "layout archive root", 255)
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseLayoutMemberV1:
    member_id: str
    artifact_ids: tuple[str, ...]
    archive_path: str
    source_path: str
    source_class: ReleaseSourceClassV1
    availability: str

    def __post_init__(self) -> None:
        _text(self.member_id, "layout member ID", 128)
        if not self.artifact_ids:
            raise ValueError("layout members require an ID and artifact selectors")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("layout member artifact selectors must be unique")
        for artifact_id in self.artifact_ids:
            _text(artifact_id, "layout member artifact ID", 128)
        normalize_release_path(self.archive_path, label="layout member archive path")
        normalize_release_path(self.source_path, label="layout member source path")
        if self.availability not in {"WO40_D", "WO40_D1", "WO40_E", "WO40_F"}:
            raise ValueError("layout member availability stage is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_path": self.archive_path,
            "artifact_ids": list(self.artifact_ids),
            "availability": self.availability,
            "member_id": self.member_id,
            "source_class": self.source_class.value,
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseLayoutMemberV1":
        row = _exact(
            value,
            {"member_id", "artifact_ids", "archive_path", "source_path", "source_class", "availability"},
            "layout member",
        )
        return cls(
            member_id=_text(row["member_id"], "layout member ID", 128),
            artifact_ids=tuple(
                _text(item, "layout member artifact ID", 128)
                for item in _array(row["artifact_ids"], "member artifacts")
            ),
            archive_path=_text(row["archive_path"], "layout member archive path", 512),
            source_path=_text(row["source_path"], "layout member source path", 512),
            source_class=ReleaseSourceClassV1(
                _text(row["source_class"], "layout member source class", 128)
            ),
            availability=_text(row["availability"], "layout availability", 128),
        )


@dataclass(frozen=True, slots=True)
class ReleaseArtifactLayoutV1:
    release_version: str
    canonical_archive_id: str
    source_classes: tuple[str, ...]
    starter_set: dict[str, object]
    artifacts: tuple[ReleaseLayoutArtifactV1, ...]
    members: tuple[ReleaseLayoutMemberV1, ...]
    build_frontends: dict[str, object]

    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if self.release_version != RELEASE_VERSION_V1:
            raise ValueError("artifact layout release version differs")
        if self.canonical_archive_id != "CANONICAL_RELEASE_ARCHIVE_V1":
            raise ValueError("artifact layout archive policy differs")
        if self.source_classes != RELEASE_SOURCE_CLASS_ORDER_V1:
            raise ValueError("artifact layout source class order differs")
        actual = tuple(
            (
                item.artifact_id,
                item.artifact_form,
                item.target,
                item.embedded_manifest,
            )
            for item in self.artifacts
        )
        if actual != RELEASE_ARTIFACT_ROWS_V1:
            raise ValueError("artifact layout six-row inventory differs")
        expected_roots = (
            "kirby2-0.1.0-linux-x86_64",
            "kirby2-0.1.0-linux-x86_64-wheelhouse",
            "kirby2-0.1.0-macos-arm64",
            "kirby2-0.1.0-macos-arm64-wheelhouse",
            "NOT_APPLICABLE",
            "kirby2-0.1.0",
        )
        if tuple(item.archive_root for item in self.artifacts) != expected_roots:
            raise ValueError("artifact layout archive roots differ")
        if self.build_frontends != RELEASE_BUILD_FRONTENDS_V1:
            raise ValueError("release build frontend declaration differs")
        if set(self.starter_set) != {"schema_version", "set_id", "entries", "entries_sha256"}:
            raise ValueError("artifact layout starter-set fields differ")
        if (
            self.starter_set["schema_version"] != 1
            or self.starter_set["set_id"] != RELEASE_STARTER_SET_ID_V1
        ):
            raise ValueError("artifact layout starter set ID differs")
        entries = self.starter_set["entries"]
        if type(entries) is not list or len(entries) != 2:
            raise ValueError("artifact layout requires exactly two starter entries")
        expected_entries_digest = hashlib.sha256(canonical_json_bytes(entries)).hexdigest()
        if expected_entries_digest != self.starter_set["entries_sha256"]:
            raise ValueError("artifact layout starter entries digest differs")
        artifact_ids = {item.artifact_id for item in self.artifacts}
        if any(set(item.artifact_ids) - artifact_ids for item in self.members):
            raise ValueError("layout member references an unknown artifact")
        member_ids = tuple(item.member_id for item in self.members)
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("layout member IDs must be unique")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "build_frontends": self.build_frontends,
            "canonical_archive_id": self.canonical_archive_id,
            "members": [item.as_dict() for item in self.members],
            "release_version": self.release_version,
            "schema_version": self.schema_version,
            "source_classes": list(self.source_classes),
            "starter_set": self.starter_set,
        }

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    def compare_installed_starter_set(self) -> None:
        exact = build_release_starter_set().layout_dict()
        if exact != self.starter_set:
            raise ValueError("artifact layout starter literals differ from committed resources")

    @classmethod
    def from_bytes(cls, raw: bytes, *, verify_starter_set: bool = True) -> "ReleaseArtifactLayoutV1":
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("artifact layout is not valid UTF-8 TOML") from error
        fields = {
            "schema_version", "release_version", "canonical_archive_id",
            "source_classes", "starter_set", "artifacts", "members", "build_frontends",
        }
        row = _exact(value, fields, "artifact layout")
        if row["schema_version"] != cls.schema_version:
            raise ValueError("artifact layout schema version differs")
        if type(row["starter_set"]) is not dict:
            raise TypeError("artifact layout starter set must be a table")
        if type(row["build_frontends"]) is not dict:
            raise TypeError("artifact layout build frontends must be a table")
        instance = cls(
            release_version=_text(row["release_version"], "layout release version", 128),
            canonical_archive_id=_text(
                row["canonical_archive_id"], "canonical archive ID", 128
            ),
            source_classes=tuple(
                _text(item, "release source class", 128)
                for item in _array(row["source_classes"], "source classes")
            ),
            starter_set=dict(row["starter_set"]),  # type: ignore[arg-type]
            artifacts=tuple(
                ReleaseLayoutArtifactV1.from_dict(item)
                for item in _array(row["artifacts"], "layout artifacts")
            ),
            members=tuple(
                ReleaseLayoutMemberV1.from_dict(item)
                for item in _array(row["members"], "layout members")
            ),
            build_frontends=dict(row["build_frontends"]),  # type: ignore[arg-type]
        )
        if verify_starter_set:
            instance.compare_installed_starter_set()
        return instance


@dataclass(frozen=True, slots=True)
class ReleasePerformanceProtocolHeaderV1:
    benchmark_id: str
    row_count: int
    row_corpus_sha256: str
    row_storage: str
    worker_count: int
    queue_size: int
    designated_target: str

    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if self.benchmark_id != RELEASE_BENCHMARK_FIXTURES_ID_V1:
            raise ValueError("performance benchmark ID differs")
        if (
            type(self.row_count) is not int
            or self.row_count != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
        ):
            raise ValueError("performance row count differs")
        require_sha256(self.row_corpus_sha256, "performance row corpus digest")
        if self.row_storage != "CANONICAL_GENERATOR_AND_DIGEST_V1":
            raise ValueError("performance row storage policy differs")
        if (
            type(self.worker_count) is not int
            or type(self.queue_size) is not int
            or (self.worker_count, self.queue_size) != (
                RELEASE_PERFORMANCE_WORKER_COUNT_V1,
                RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
            )
        ):
            raise ValueError("performance worker resources differ")
        if self.designated_target != "macos-arm64":
            raise ValueError("performance designated target differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "designated_target": self.designated_target,
            "queue_size": self.queue_size,
            "row_corpus_sha256": self.row_corpus_sha256,
            "row_count": self.row_count,
            "row_storage": self.row_storage,
            "schema_version": self.schema_version,
            "worker_count": self.worker_count,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePerformanceProtocolHeaderV1":
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("performance protocol is not valid UTF-8 TOML") from error
        header = value.get("protocol") if type(value) is dict else None
        row = _exact(
            header,
            {
                "schema_version", "benchmark_id", "row_count", "row_corpus_sha256",
                "row_storage", "worker_count", "queue_size", "designated_target",
            },
            "performance protocol header",
        )
        if row["schema_version"] != cls.schema_version:
            raise ValueError("performance protocol schema version differs")
        return cls(
            benchmark_id=_text(row["benchmark_id"], "performance benchmark ID", 128),
            row_count=row["row_count"],  # type: ignore[arg-type]
            row_corpus_sha256=_text(
                row["row_corpus_sha256"], "performance row corpus digest", 64
            ),
            row_storage=_text(row["row_storage"], "performance row storage", 128),
            worker_count=row["worker_count"],  # type: ignore[arg-type]
            queue_size=row["queue_size"],  # type: ignore[arg-type]
            designated_target=_text(
                row["designated_target"], "performance designated target", 128
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseProtocolBundleV1:
    repository_root: Path
    platform_protocol: ReleasePlatformsV1
    qualification_protocol: ReleaseQualificationProtocolV1
    requirements_lock: ReleaseRequirementsLockV1
    artifact_layout: ReleaseArtifactLayoutV1
    performance_protocol: ReleasePerformanceProtocolHeaderV1
    protocol_files: tuple[ReleaseProtocolFileV1, ...]

    def __post_init__(self) -> None:
        _absolute(self.repository_root, "repository root")
        if tuple(item.path for item in self.protocol_files) != RELEASE_PROTOCOL_PATHS_V1:
            raise ValueError("release protocol file projection differs")

    @property
    def protocol_set_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes([item.as_dict() for item in self.protocol_files])
        ).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_layout_sha256": self.artifact_layout.logical_sha256,
            "performance_protocol": self.performance_protocol.as_dict(),
            "platforms_sha256": self.platform_protocol.logical_sha256,
            "protocol_files": [item.as_dict() for item in self.protocol_files],
            "protocol_set_sha256": self.protocol_set_sha256,
            "qualification_sha256": self.qualification_protocol.logical_sha256,
            "requirements_sha256": self.requirements_lock.logical_sha256,
            "schema_id": RELEASE_BUILD_PROTOCOL_SCHEMA_ID_V1,
            "schema_version": 1,
        }


def _release_performance_source_identities(
    repository_root: Path,
) -> tuple[str, str, str, str]:
    """Recompute every content-derived auxiliary workload identity."""

    evidence_path = repository_root / "KIRBY2_FULL_DAY_QUALIFICATION_EVIDENCE.md"
    profile_path = repository_root / "kirby2/full_day/profile_candidates.toml"
    source_path = repository_root / "kirby2/mining/fixtures/qualification_sources.toml"
    for path in (evidence_path, profile_path, source_path):
        if not path.is_file():
            raise ValueError(f"release performance identity source is missing: {path.name}")

    source_document = tomllib.loads(source_path.read_text("utf-8"))
    try:
        proof_bytes = source_document["rows"]["quiet"]["configuration"]["bytes_json"]
    except (KeyError, TypeError) as error:
        raise ValueError("quiet-range qualification source projection is missing") from error
    if type(proof_bytes) is not str:
        raise TypeError("quiet-range qualification source projection must be text")
    try:
        proof = json.loads(proof_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("quiet-range qualification source projection is invalid JSON") from error
    expected_fields = {
        "abort_code",
        "candidate_id",
        "event_prefix_sha256",
        "final_checkpoint_sha256",
        "full_day_evidence_digest",
        "full_day_run_id",
        "outer_event_count",
        "partition",
        "plan_sha256",
        "replay_verification_status",
        "root_seed",
        "run_digest",
        "workload_sha256",
    }
    if type(proof) is not dict or set(proof) != expected_fields:
        raise ValueError("quiet-range qualification source fields differ")
    if (
        proof["candidate_id"] != "QUIET_RANGE_PRESSURE"
        or proof["partition"] != "QUALIFICATION"
        or proof["root_seed"] != 3_102_000
        or proof["replay_verification_status"] != "PASS"
        or proof["abort_code"] is not None
    ):
        raise ValueError("quiet-range qualification source selector differs")
    source_artifact_manifest_sha256 = _text(
        proof["full_day_evidence_digest"],
        "terminal-update source artifact-manifest digest",
        64,
    )
    require_sha256(
        source_artifact_manifest_sha256,
        "terminal-update source artifact-manifest digest",
    )

    from kirby2.full_day.qualification import (
        materialize_release_performance_full_day_plan_v1,
    )

    plan, _workload = materialize_release_performance_full_day_plan_v1()
    return (
        hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        source_artifact_manifest_sha256,
        hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        plan.semantic_sha256,
    )


def _validate_release_performance_protocol(
    raw: bytes,
    *,
    repository_root: Path,
    artifact_layout: ReleaseArtifactLayoutV1,
) -> None:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("performance protocol is not valid UTF-8 TOML") from error
    expected_top = {
        "protocol",
        "row_generation",
        "resources",
        "designated_target",
        "cross_platform_integer_core",
        "auxiliary_templates",
        "auxiliary_thresholds",
    }
    if set(value) != expected_top:
        raise ValueError("performance protocol top-level fields differ")

    expected_generation = {
        "cell_order": [item.value for item in RELEASE_PERFORMANCE_CELL_ORDER_V1],
        "initial_attempt": 1,
        "result_schema_id": "ReleasePerformanceCellResultV1",
        "root_end_inclusive": RELEASE_PERFORMANCE_ROOTS_V1.stop - 1,
        "root_start": RELEASE_PERFORMANCE_ROOTS_V1.start,
        "runner_source_policy_id": RELEASE_RUNNER_SOURCE_POLICY_ID_V1,
    }
    if value["row_generation"] != expected_generation:
        raise ValueError("performance row-generation protocol differs")
    expected_resources = {
        "aggregate_artifact_pass_bytes": RELEASE_ARTIFACT_PASS_BYTES_V1,
        "aggregate_artifact_warning_bytes": RELEASE_ARTIFACT_WARNING_BYTES_V1,
        "logical_cpu_minimum": 4,
        "memory_bytes_minimum": 8 * 1024**3,
        "per_attempt_rss_bytes": RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
        "per_attempt_temporary_bytes": RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
        "per_attempt_wall_ns": RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
        "retries": 1,
        "retryable_failure_codes": ["PROCESS_FAILURE", "RESOURCE_LIMIT"],
        "store_bytes_minimum": 20 * 1024**3,
        "total_wall_ns": RELEASE_TOTAL_WALL_LIMIT_NS_V1,
    }
    if value["resources"] != expected_resources:
        raise ValueError("performance resource and abort protocol differs")
    if value["designated_target"] != {
        "artifact_pair": ["macos-arm64/headless", "macos-arm64/desktop"],
        "python_implementation": "CPython",
        "python_version": "3.14",
        "target": "macos-arm64",
    }:
        raise ValueError("performance designated target differs")
    integer_roots = list(range(4_000_000, 4_000_016))
    if value["cross_platform_integer_core"] != {
        "core_flow_roots": integer_roots,
        "mechanics_roots": integer_roots,
        "policy_id": "CROSS_PLATFORM_INTEGER_CORE_V1",
    }:
        raise ValueError("cross-platform integer-core protocol differs")

    (
        qualification_evidence_sha256,
        source_artifact_manifest_sha256,
        profile_manifest_sha256,
        selected_plan_sha256,
    ) = _release_performance_source_identities(repository_root)
    auxiliary = auxiliary_performance_templates(
        starter_layout=artifact_layout.starter_set,
        qualification_evidence_sha256=qualification_evidence_sha256,
        source_artifact_manifest_sha256=source_artifact_manifest_sha256,
        profile_manifest_sha256=profile_manifest_sha256,
        selected_plan_sha256=selected_plan_sha256,
    )
    if value["auxiliary_templates"] != [item.as_dict() for item in auxiliary]:
        raise ValueError("auxiliary performance workload templates differ")
    expected_thresholds = [
        {
            "hard_failure": row[5],
            "metric_id": row[1],
            "pass_upper_inclusive": row[3],
            "reduction_id": row[0],
            "statistic": row[2],
            "warning_upper_inclusive": row[4],
        }
        for row in RELEASE_AUXILIARY_THRESHOLDS_V1
    ]
    if value["auxiliary_thresholds"] != expected_thresholds:
        raise ValueError("auxiliary performance thresholds differ")


_CLEAN_PROVIDER_ACCESS_METHODS_V1 = {
    "LOCAL_VM",
    "REMOTE_SSH",
    "REMOTE_CONTROL",
    "CI_RUNNER",
}
_CLEAN_PROVIDER_ROOT_MECHANISMS_V1 = {
    "DISPOSABLE_VM_SNAPSHOT",
    "EPHEMERAL_HOST",
    "EPHEMERAL_CI_RUNNER",
}
_CLEAN_PROVIDER_EVIDENCE_RETURN_V1 = {
    "LOCAL_ARTIFACT_EXPORT",
    "SSH_ARTIFACT_RETURN",
    "REMOTE_CONTROL_ARTIFACT_RETURN",
    "CI_ARTIFACT_EXPORT",
}


@dataclass(frozen=True, slots=True)
class ReleaseCleanProviderV1:
    target_id: str
    system: str
    machine: str
    python_implementation: str
    python_version: str
    available: bool
    credential_available: bool
    available_disk_bytes: int
    available_memory_bytes: int
    offline_install: bool
    access_method: str
    clean_root_mechanism: str
    evidence_return: str

    def __post_init__(self) -> None:
        if self.target_id not in {"macos-arm64", "linux-x86_64"}:
            raise ValueError("clean-provider target is outside the release matrix")
        for label, value in (
            ("provider system", self.system),
            ("provider machine", self.machine),
            ("provider Python implementation", self.python_implementation),
            ("provider Python version", self.python_version),
            ("provider access method", self.access_method),
            ("provider clean-root mechanism", self.clean_root_mechanism),
            ("provider evidence return", self.evidence_return),
        ):
            _text(value, label, 512)
        if self.access_method not in _CLEAN_PROVIDER_ACCESS_METHODS_V1:
            raise ValueError("clean-provider access method is outside the closed enum")
        if self.clean_root_mechanism not in _CLEAN_PROVIDER_ROOT_MECHANISMS_V1:
            raise ValueError("clean-provider root mechanism is outside the closed enum")
        if self.evidence_return not in _CLEAN_PROVIDER_EVIDENCE_RETURN_V1:
            raise ValueError("clean-provider evidence return is outside the closed enum")
        expected_platform = {
            "macos-arm64": ("Darwin", "arm64", "CPython", "3.14"),
            "linux-x86_64": ("Linux", "x86_64", "CPython", "3.14"),
        }[self.target_id]
        if (
            self.system,
            self.machine,
            self.python_implementation,
            self.python_version,
        ) != expected_platform:
            raise ValueError("clean-provider platform/runtime differs from its target ID")
        for label, value in (
            ("provider available", self.available),
            ("provider credential available", self.credential_available),
            ("provider offline install", self.offline_install),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be Boolean")
        for label, value in (
            ("provider available disk", self.available_disk_bytes),
            ("provider available memory", self.available_memory_bytes),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{label} must be a nonnegative integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "access_method": self.access_method,
            "available": self.available,
            "available_disk_bytes": self.available_disk_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "clean_root_mechanism": self.clean_root_mechanism,
            "credential_available": self.credential_available,
            "evidence_return": self.evidence_return,
            "machine": self.machine,
            "offline_install": self.offline_install,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "system": self.system,
            "target_id": self.target_id,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    @property
    def capability_summary(self) -> str:
        return (
            f"access={self.access_method}; system={self.system}; machine={self.machine}; "
            f"runtime={self.python_implementation}-{self.python_version}; "
            f"available={'true' if self.available else 'false'}; "
            f"credential_available={'true' if self.credential_available else 'false'}; "
            f"disk_bytes={self.available_disk_bytes}; "
            f"memory_bytes={self.available_memory_bytes}; "
            f"offline_install={'true' if self.offline_install else 'false'}; "
            f"clean_root={self.clean_root_mechanism}; "
            f"evidence_return={self.evidence_return}"
        )

    def readiness(
        self, target: ReleasePlatformTargetV1
    ) -> tuple[str, str]:
        if type(target) is not ReleasePlatformTargetV1:
            raise TypeError("provider readiness requires a typed platform target")
        if self.target_id != target.target_id:
            return "INSUFFICIENT", "Provider target ID differs from the frozen target."
        if not self.available or not self.credential_available:
            return "MISSING", "Provider or its existing credential is unavailable."
        expected = (
            target.system,
            target.machine,
            target.python_implementation,
            target.python_version,
        )
        observed = (
            self.system,
            self.machine,
            self.python_implementation,
            self.python_version,
        )
        if observed != expected:
            return "INSUFFICIENT", "Provider platform/runtime differs from the frozen target."
        if not self.offline_install:
            return "INSUFFICIENT", "Provider does not enforce the offline install boundary."
        if self.available_memory_bytes < 8 * 1024**3:
            return "INSUFFICIENT", "Provider has less than the required 8 GiB memory."
        if self.available_disk_bytes < 20 * 1024**3:
            return "INSUFFICIENT", "Provider has less than the required 20 GiB free store."
        return "PASS", "Provider satisfies the frozen clean-target capability contract."

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseCleanProviderV1":
        fields = {
            "target_id",
            "system",
            "machine",
            "python_implementation",
            "python_version",
            "available",
            "credential_available",
            "available_disk_bytes",
            "available_memory_bytes",
            "offline_install",
            "access_method",
            "clean_root_mechanism",
            "evidence_return",
        }
        row = _exact(value, fields, "clean-provider record")
        return cls(
            target_id=_text(row["target_id"], "provider target ID", 128),
            system=_text(row["system"], "provider system", 128),
            machine=_text(row["machine"], "provider machine", 128),
            python_implementation=_text(
                row["python_implementation"], "provider Python implementation", 128
            ),
            python_version=_text(
                row["python_version"], "provider Python version", 128
            ),
            available=row["available"],  # type: ignore[arg-type]
            credential_available=row["credential_available"],  # type: ignore[arg-type]
            available_disk_bytes=row["available_disk_bytes"],  # type: ignore[arg-type]
            available_memory_bytes=row["available_memory_bytes"],  # type: ignore[arg-type]
            offline_install=row["offline_install"],  # type: ignore[arg-type]
            access_method=_text(row["access_method"], "provider access method", 512),
            clean_root_mechanism=_text(
                row["clean_root_mechanism"], "provider clean-root mechanism", 512
            ),
            evidence_return=_text(
                row["evidence_return"], "provider evidence return", 512
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseCleanProviderInventoryV1:
    providers: tuple[ReleaseCleanProviderV1, ...]
    inventory_id: str = "KIRBY2_RELEASE_CLEAN_PROVIDER_INVENTORY_V1"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.inventory_id != "KIRBY2_RELEASE_CLEAN_PROVIDER_INVENTORY_V1"
        ):
            raise ValueError("clean-provider inventory identity differs")
        if type(self.providers) is not tuple or any(
            type(item) is not ReleaseCleanProviderV1 for item in self.providers
        ):
            raise TypeError("clean-provider inventory entries must be typed")
        order = {"macos-arm64": 0, "linux-x86_64": 1}
        targets = tuple(item.target_id for item in self.providers)
        if targets != tuple(sorted(targets, key=order.__getitem__)) or len(targets) != len(
            set(targets)
        ):
            raise ValueError("clean-provider inventory must be unique and target-ordered")

    def by_target(self) -> dict[str, ReleaseCleanProviderV1]:
        return {item.target_id: item for item in self.providers}

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseCleanProviderInventoryV1":
        if type(raw) is not bytes:
            raise TypeError("clean-provider inventory must use exact bytes")
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("clean-provider inventory is not valid UTF-8 TOML") from error
        row = _exact(
            value,
            {"schema_version", "inventory_id", "providers"},
            "clean-provider inventory",
        )
        return cls(
            schema_version=row["schema_version"],  # type: ignore[arg-type]
            inventory_id=_text(row["inventory_id"], "provider inventory ID", 128),
            providers=tuple(
                ReleaseCleanProviderV1.from_dict(item)
                for item in _array(row["providers"], "clean-provider entries")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseResourceItemV1:
    resource_id: str
    target: str
    kind: str
    status: str
    expected_sha256: str | None
    observed_sha256: str | None
    detail: str

    def __post_init__(self) -> None:
        _text(self.resource_id, "release resource ID", 512)
        _text(self.target, "release resource target", 128)
        _text(self.kind, "release resource kind", 128)
        if self.status not in {
            "PASS",
            "MISSING",
            "DIGEST_MISMATCH",
            "INVALID",
            "INSUFFICIENT",
        }:
            raise ValueError("release resource status is invalid")
        if self.expected_sha256 is not None:
            require_sha256(self.expected_sha256, "expected resource digest")
        if self.observed_sha256 is not None:
            require_sha256(self.observed_sha256, "observed resource digest")
        _text(self.detail, "release resource detail", 4096)

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "expected_sha256": self.expected_sha256,
            "kind": self.kind,
            "observed_sha256": self.observed_sha256,
            "resource_id": self.resource_id,
            "status": self.status,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBuildDistributionV1:
    name: str
    version: str
    file_count: int
    file_projection_sha256: str

    def __post_init__(self) -> None:
        _text(self.name, "build distribution name", 128)
        _text(self.version, "build distribution version", 128)
        if type(self.file_count) is not int or self.file_count <= 0:
            raise ValueError("build distribution file count must be positive")
        require_sha256(
            self.file_projection_sha256,
            "build distribution file-projection digest",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "file_count": self.file_count,
            "file_projection_sha256": self.file_projection_sha256,
            "name": self.name,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBuildImportV1:
    module: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.module, "build import module", 128)
        normalize_release_path(self.path, label="build import path")
        require_sha256(self.sha256, "build import digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBuildRuntimeSnapshotV1:
    runtime: ReleaseRuntimeV1
    python_executable_sha256: str
    virtual_environment_configuration_sha256: str
    effective_import_paths: tuple[str, ...]
    virtual_environment_entry_count: int
    virtual_environment_projection_sha256: str
    zlib_extension_sha256: str
    zlib_runtime_version: str
    archive_encoder_probe_sha256: str
    distributions: tuple[ReleaseBuildDistributionV1, ...]
    imports: tuple[ReleaseBuildImportV1, ...]
    python_installation_entry_count: int
    python_installation_projection_sha256: str
    site_packages_file_count: int
    site_packages_projection_sha256: str

    schema_id: ClassVar[str] = "KIRBY2_RELEASE_BUILD_RUNTIME_SNAPSHOT_V1"
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if type(self.runtime) is not ReleaseRuntimeV1:
            raise TypeError("build runtime snapshot requires an exact runtime record")
        require_sha256(self.python_executable_sha256, "Python executable digest")
        require_sha256(
            self.virtual_environment_configuration_sha256,
            "virtual-environment configuration digest",
        )
        if (
            type(self.effective_import_paths) is not tuple
            or not self.effective_import_paths
            or any(type(item) is not str for item in self.effective_import_paths)
        ):
            raise TypeError("effective import paths must be a nonempty text tuple")
        folded_import_paths: set[str] = set()
        for import_path in self.effective_import_paths:
            require_nfc_text(
                import_path,
                "effective import path",
                maximum_bytes=4096,
            )
            key = import_path.casefold()
            if key in folded_import_paths:
                raise ValueError("effective import paths collide under case folding")
            folded_import_paths.add(key)
        if (
            type(self.virtual_environment_entry_count) is not int
            or self.virtual_environment_entry_count <= 0
        ):
            raise ValueError("virtual-environment entry count must be positive")
        require_sha256(
            self.virtual_environment_projection_sha256,
            "virtual-environment projection digest",
        )
        require_sha256(self.zlib_extension_sha256, "zlib extension digest")
        require_sha256(
            self.archive_encoder_probe_sha256,
            "archive encoder probe digest",
        )
        _text(self.zlib_runtime_version, "zlib runtime version", 128)
        if type(self.distributions) is not tuple or any(
            type(item) is not ReleaseBuildDistributionV1
            for item in self.distributions
        ):
            raise TypeError("build distributions must use exact V1 records")
        names = tuple(item.name for item in self.distributions)
        if names != ("pip", "setuptools"):
            raise ValueError("build distribution inventory must be pip then setuptools")
        if type(self.imports) is not tuple or any(
            type(item) is not ReleaseBuildImportV1 for item in self.imports
        ):
            raise TypeError("build imports must use exact V1 records")
        if tuple(item.module for item in self.imports) != (
            "pip",
            "setuptools",
            "setuptools.build_meta",
        ):
            raise ValueError("build import inventory differs")
        if (
            type(self.python_installation_entry_count) is not int
            or self.python_installation_entry_count <= 0
        ):
            raise ValueError("Python installation entry count must be positive")
        require_sha256(
            self.python_installation_projection_sha256,
            "Python installation projection digest",
        )
        if (
            type(self.site_packages_file_count) is not int
            or self.site_packages_file_count <= 0
        ):
            raise ValueError("site-packages file count must be positive")
        require_sha256(
            self.site_packages_projection_sha256,
            "site-packages projection digest",
        )

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_encoder_probe_sha256": self.archive_encoder_probe_sha256,
            "distributions": [item.as_dict() for item in self.distributions],
            "effective_import_paths": list(self.effective_import_paths),
            "imports": [item.as_dict() for item in self.imports],
            "python_executable_sha256": self.python_executable_sha256,
            "python_installation_entry_count": (
                self.python_installation_entry_count
            ),
            "python_installation_projection_sha256": (
                self.python_installation_projection_sha256
            ),
            "runtime": self.runtime.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "site_packages_file_count": self.site_packages_file_count,
            "site_packages_projection_sha256": (
                self.site_packages_projection_sha256
            ),
            "virtual_environment_configuration_sha256": (
                self.virtual_environment_configuration_sha256
            ),
            "virtual_environment_entry_count": (
                self.virtual_environment_entry_count
            ),
            "virtual_environment_projection_sha256": (
                self.virtual_environment_projection_sha256
            ),
            "zlib_extension_sha256": self.zlib_extension_sha256,
            "zlib_runtime_version": self.zlib_runtime_version,
        }


@dataclass(frozen=True, slots=True)
class ReleaseResourcePreflightV1:
    protocol_set_sha256: str
    protocol_commit: str | None
    items: tuple[ReleaseResourceItemV1, ...]
    build_runtime: ReleaseBuildRuntimeSnapshotV1 | None
    no_network: bool

    schema_id: ClassVar[str] = RELEASE_RESOURCE_PREFLIGHT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_sha256(self.protocol_set_sha256, "preflight protocol-set digest")
        if (
            self.protocol_commit is not None
            and _COMMIT.fullmatch(self.protocol_commit) is None
        ):
            raise ValueError("preflight protocol commit is invalid")
        if self.no_network is not True:
            raise ValueError("release resource preflight must be no-network")
        if type(self.items) is not tuple or any(
            type(item) is not ReleaseResourceItemV1 for item in self.items
        ):
            raise TypeError("release preflight items must use exact V1 records")
        if (
            self.build_runtime is not None
            and type(self.build_runtime) is not ReleaseBuildRuntimeSnapshotV1
        ):
            raise TypeError("release preflight build runtime must use the exact V1 record")
        runtime_items = tuple(
            item
            for item in self.items
            if item.resource_id == "build-runtime-and-backend"
        )
        if len(runtime_items) != 1:
            raise ValueError("release preflight requires one build runtime resource")
        runtime_item = runtime_items[0]
        if self.build_runtime is None:
            if runtime_item.status == "PASS" or runtime_item.observed_sha256 is not None:
                raise ValueError("unavailable build runtime resource is inconsistent")
        elif (
            runtime_item.status != "PASS"
            or runtime_item.observed_sha256 != self.build_runtime.logical_sha256
        ):
            raise ValueError("passing build runtime resource identity differs")

    @property
    def status(self) -> str:
        return (
            "PASS"
            if self.items and all(item.status == "PASS" for item in self.items)
            else "NOT_READY"
        )

    @property
    def missing_items(self) -> tuple[ReleaseResourceItemV1, ...]:
        return tuple(item for item in self.items if item.status != "PASS")

    @property
    def resource_snapshot_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes([item.as_dict() for item in self.items])
        ).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "build_runtime": (
                None if self.build_runtime is None else self.build_runtime.as_dict()
            ),
            "items": [item.as_dict() for item in self.items],
            "missing_item_count": len(self.missing_items),
            "no_network": self.no_network,
            "protocol_commit": self.protocol_commit,
            "protocol_set_sha256": self.protocol_set_sha256,
            "resource_snapshot_sha256": self.resource_snapshot_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status,
        }

    def markdown(self) -> str:
        lines = [
            "# Kirby2 Release Resource Preflight",
            "",
            f"Status: `{self.status}`",
            "",
            f"Protocol set SHA-256: `{self.protocol_set_sha256}`",
            f"WO40-D protocol commit: `{self.protocol_commit or 'UNAVAILABLE'}`",
            f"Resource snapshot SHA-256: `{self.resource_snapshot_sha256}`",
            "",
            "This inspection was read-only and no-network. It did not download, install, build, connect to a provider, or alter credentials.",
            "",
            "| Resource | Target | Kind | Status | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| `{item.resource_id}` | `{item.target}` | `{item.kind}` | `{item.status}` | {item.detail.replace('|', '/')} |"
            for item in self.items
        )
        lines.extend(("", "## Machine-readable missing items", "", "```json"))
        lines.append(canonical_json_bytes([item.as_dict() for item in self.missing_items]).decode("ascii"))
        lines.extend(("```", ""))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ReleaseCommandOutcomeV1:
    command_id: str
    status: ReleaseCommandStatusV1
    protocol_set_sha256: str
    detail: str
    payload: dict[str, object]
    refusal_code: str | None = None

    def __post_init__(self) -> None:
        _text(self.command_id, "release command ID", 128)
        if type(self.status) is not ReleaseCommandStatusV1:
            raise TypeError("release command status must use the V1 enum")
        require_sha256(self.protocol_set_sha256, "release protocol-set digest")
        _text(self.detail, "release command detail", 4096)
        if type(self.payload) is not dict:
            raise TypeError("release command payload must be an object")
        if self.refusal_code is not None:
            _text(self.refusal_code, "release refusal code", 128)

    def as_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "detail": self.detail,
            "payload": self.payload,
            "protocol_set_sha256": self.protocol_set_sha256,
            "refusal_code": self.refusal_code,
            "schema_id": RELEASE_COMMAND_OUTCOME_SCHEMA_ID_V1,
            "schema_version": 1,
            "status": self.status.value,
        }


def load_release_protocol_bundle(
    repository_root: Path,
    *,
    verify_starter_set: bool = True,
) -> ReleaseProtocolBundleV1:
    root = _absolute(repository_root, "repository root")
    raw_by_path: dict[str, bytes] = {}
    for relative in RELEASE_PROTOCOL_PATHS_V1:
        path = root / relative
        if not path.is_file():
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.PROTOCOL_MISSING,
                f"required release protocol is missing: {relative}",
            )
        raw_by_path[relative] = path.read_bytes()
    try:
        artifact_layout = ReleaseArtifactLayoutV1.from_bytes(
            raw_by_path["release/artifact_layout.toml"],
            verify_starter_set=verify_starter_set,
        )
        performance_protocol = ReleasePerformanceProtocolHeaderV1.from_bytes(
            raw_by_path["release/performance_thresholds.toml"]
        )
        _validate_release_performance_protocol(
            raw_by_path["release/performance_thresholds.toml"],
            repository_root=root,
            artifact_layout=artifact_layout,
        )
        return ReleaseProtocolBundleV1(
            repository_root=root,
            artifact_layout=artifact_layout,
            performance_protocol=performance_protocol,
            platform_protocol=ReleasePlatformsV1.from_bytes(
                raw_by_path["release/platforms.toml"]
            ),
            qualification_protocol=ReleaseQualificationProtocolV1.from_bytes(
                raw_by_path["release/qualification.toml"]
            ),
            requirements_lock=ReleaseRequirementsLockV1.from_bytes(
                raw_by_path["release/requirements.lock"]
            ),
            protocol_files=tuple(
                ReleaseProtocolFileV1(
                    path=relative,
                    sha256=hashlib.sha256(raw_by_path[relative]).hexdigest(),
                )
                for relative in RELEASE_PROTOCOL_PATHS_V1
            ),
        )
    except ReleaseBuildRefused:
        raise
    except (TypeError, ValueError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.PROTOCOL_INVALID,
            str(error),
        ) from error


def _resolve_release_protocol_commit(
    bundle: ReleaseProtocolBundleV1,
) -> tuple[str | None, bool]:
    """Resolve the newest commit that changed a frozen protocol input.

    Resource evidence is committed after the protocol, so binding it to repository
    HEAD would make the evidence invalidate itself.  The owning revision is instead
    the newest first-parent-visible commit touching any exact protocol path, with
    every current protocol byte independently compared to that tree.
    """

    try:
        commit_result = subprocess.run(
            [
                "git",
                "log",
                "-1",
                "--first-parent",
                "--format=%H",
                "--",
                *RELEASE_PROTOCOL_PATHS_V1,
            ],
            cwd=bundle.repository_root,
            capture_output=True,
            check=False,
        )
        protocol_commit = commit_result.stdout.decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None, False
    if (
        commit_result.returncode != 0
        or _COMMIT.fullmatch(protocol_commit) is None
    ):
        return None, False

    for relative in RELEASE_PROTOCOL_PATHS_V1:
        try:
            committed = subprocess.run(
                ["git", "show", f"{protocol_commit}:{relative}"],
                cwd=bundle.repository_root,
                capture_output=True,
                check=False,
            )
            current = (bundle.repository_root / relative).read_bytes()
        except OSError:
            return protocol_commit, False
        if committed.returncode != 0 or committed.stdout != current:
            return protocol_commit, False
    return protocol_commit, True


def _build_distribution_snapshot(
    name: str,
) -> tuple[ReleaseBuildDistributionV1, Path]:
    try:
        distribution = importlib_metadata.distribution(name)
    except importlib_metadata.PackageNotFoundError as error:
        raise FileNotFoundError(
            f"Required build distribution {name} is absent."
        ) from error
    files = distribution.files
    if not files:
        raise ValueError(f"Build distribution {name} has no file inventory.")
    base = Path(distribution.locate_file("")).resolve(strict=True)
    rows: list[dict[str, object]] = []
    folded: set[str] = set()
    recorded_paths: set[str] = set()
    owned_roots: set[str] = set()
    for entry in files:
        candidate = Path(distribution.locate_file(entry))
        if candidate.is_symlink():
            raise ValueError(f"Build distribution {name} contains a symlink.")
        prospective = candidate.resolve(strict=False)
        try:
            prospective.relative_to(base)
        except ValueError:
            continue
        if not candidate.is_file():
            raise ValueError(f"Build distribution {name} has a missing file.")
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(base).as_posix()
        parts = relative.split("/")
        if "__pycache__" in parts or relative.endswith(".pyc"):
            continue
        if not resolved.is_file():
            raise ValueError(f"Build distribution {name} contains a non-file entry.")
        normalize_release_path(relative, label=f"{name} distribution path")
        key = relative.casefold()
        if key in folded:
            raise ValueError(f"Build distribution {name} paths collide under case folding.")
        folded.add(key)
        recorded_paths.add(relative)
        owned_roots.add(parts[0])
        raw = resolved.read_bytes()
        rows.append(
            {
                "mode": stat.S_IMODE(resolved.stat().st_mode),
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    for root_name in sorted(owned_roots, key=lambda value: value.encode("utf-8")):
        owned_root = base / root_name
        candidates = (
            tuple(owned_root.rglob("*")) if owned_root.is_dir() else (owned_root,)
        )
        for candidate in candidates:
            if candidate.is_symlink():
                raise ValueError(f"Build distribution {name} contains a symlink.")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise ValueError(
                    f"Build distribution {name} contains a non-file entry."
                )
            relative = candidate.resolve(strict=True).relative_to(base).as_posix()
            parts = relative.split("/")
            if "__pycache__" in parts or relative.endswith(".pyc"):
                continue
            normalize_release_path(relative, label=f"{name} distribution path")
            if relative not in recorded_paths:
                raise ValueError(
                    f"Build distribution {name} contains an unrecorded file."
                )
    rows.sort(key=lambda row: str(row["path"]).encode("utf-8"))
    if not rows:
        raise ValueError(f"Build distribution {name} has no in-environment files.")
    normalized_name = distribution.metadata.get("Name", "").casefold()
    if normalized_name != name:
        raise ValueError(f"Build distribution {name} metadata identity differs.")
    return (
        ReleaseBuildDistributionV1(
            name=name,
            version=_text(
                distribution.version,
                f"{name} build distribution version",
                128,
            ),
            file_count=len(rows),
            file_projection_sha256=hashlib.sha256(
                canonical_json_bytes(rows)
            ).hexdigest(),
        ),
        base,
    )


def _tree_file_projection(
    root: Path,
    *,
    label: str,
    allow_relative_symlinks: bool = False,
    allowed_absolute_symlink_root: Path | None = None,
) -> tuple[int, str]:
    root = root.resolve(strict=True)
    if allowed_absolute_symlink_root is not None:
        allowed_absolute_symlink_root = allowed_absolute_symlink_root.resolve(
            strict=True
        )
    if not root.is_dir():
        raise ValueError(f"{label} projection root is not a directory.")
    rows: list[dict[str, object]] = []
    folded: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            if not allow_relative_symlinks:
                raise ValueError(f"{label} contains a symlink.")
            relative = candidate.relative_to(root).as_posix()
            normalize_release_path(relative, label=f"{label} path")
            key = relative.casefold()
            if key in folded:
                raise ValueError(f"{label} paths collide under case folding.")
            folded.add(key)
            target = require_nfc_text(
                os.readlink(candidate),
                f"{label} symlink target",
                maximum_bytes=4096,
            )
            if Path(target).is_absolute():
                if allowed_absolute_symlink_root is None:
                    raise ValueError(
                        f"{label} contains an absolute symlink target."
                    )
                try:
                    candidate.resolve(strict=True).relative_to(
                        allowed_absolute_symlink_root
                    )
                except (OSError, ValueError) as error:
                    raise ValueError(
                        f"{label} contains an external absolute symlink target."
                    ) from error
            rows.append(
                {
                    "kind": "SYMLINK",
                    "mode": stat.S_IMODE(candidate.lstat().st_mode),
                    "path": relative,
                    "target": target,
                }
            )
            continue
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError(f"{label} contains a non-file entry.")
        relative = candidate.resolve(strict=True).relative_to(root).as_posix()
        normalize_release_path(relative, label=f"{label} path")
        key = relative.casefold()
        if key in folded:
            raise ValueError(f"{label} paths collide under case folding.")
        folded.add(key)
        raw = candidate.read_bytes()
        rows.append(
            {
                "kind": "REGULAR",
                "mode": stat.S_IMODE(candidate.stat().st_mode),
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    rows.sort(key=lambda row: str(row["path"]).encode("utf-8"))
    if not rows:
        raise ValueError(f"{label} file projection is empty.")
    return len(rows), hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def _build_import_snapshot(
    module: str,
    spec: object,
    *,
    site_packages: Path,
    expected_path: str,
) -> ReleaseBuildImportV1:
    origin_value = getattr(spec, "origin", None)
    if not isinstance(origin_value, str):
        raise ValueError(f"Build import {module} has no file origin.")
    origin = Path(origin_value)
    if origin.is_symlink() or not origin.is_file():
        raise ValueError(f"Build import {module} origin is not a regular file.")
    try:
        relative = origin.resolve(strict=True).relative_to(site_packages).as_posix()
    except ValueError as error:
        raise ValueError(f"Build import {module} resolves outside site-packages.") from error
    normalize_release_path(relative, label=f"{module} import path")
    if relative != expected_path:
        raise ValueError(f"Build import {module} resolves to an unexpected origin.")
    raw = origin.read_bytes()
    return ReleaseBuildImportV1(
        module=module,
        path=relative,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def inspect_release_build_runtime(
    bundle: ReleaseProtocolBundleV1,
    *,
    pip_frontend: Path,
) -> ReleaseBuildRuntimeSnapshotV1:
    """Fingerprint the exact interpreter, zlib encoder, and imported build backend."""

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("build runtime inspection requires a release protocol bundle")
    if (
        sys.flags.isolated != 1
        or sys.flags.safe_path is not True
        or sys.flags.no_user_site != 1
    ):
        raise ValueError(
            "Build interpreter must use isolated startup with a safe import path."
        )
    pip_path = _absolute(pip_frontend, "project wheel frontend")
    if not pip_path.is_file() or not os.access(pip_path, os.X_OK):
        raise FileNotFoundError("Required project wheel frontend is absent.")
    first_line = pip_path.read_bytes().splitlines()[:1]
    if not first_line or not first_line[0].startswith(b"#!"):
        raise ValueError("Project wheel frontend does not declare its interpreter.")
    try:
        frontend_python = Path(first_line[0][2:].decode("utf-8")).resolve(strict=True)
        active_python = Path(sys.executable).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("Project wheel frontend interpreter cannot be resolved.") from error
    if frontend_python != active_python:
        raise ValueError("Project wheel frontend and active build interpreter differ.")
    if any(os.environ.get(name) for name in ("PYTHONHOME", "PYTHONPATH")):
        raise ValueError("Build interpreter environment contains a Python path override.")

    cache_tag = sys.implementation.cache_tag
    runtime = ReleaseRuntimeV1(
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        cache_tag=(cache_tag if isinstance(cache_tag, str) else ""),
        compiler=platform.python_compiler(),
        zlib_version=zlib.ZLIB_VERSION,
    )
    if runtime.python_implementation != "CPython" or tuple(
        runtime.python_version.split(".")[:2]
    ) != ("3", "14"):
        raise ValueError("Build interpreter differs from the frozen CPython 3.14 line.")

    try:
        virtual_environment = Path(sys.prefix).resolve(strict=True)
        python_installation = Path(sys.base_prefix).resolve(strict=True)
        frontend_environment = pip_path.parent.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("Build virtual-environment roots cannot be resolved.") from error
    if virtual_environment == python_installation:
        raise ValueError("Build interpreter is not running inside a virtual environment.")
    if virtual_environment != frontend_environment:
        raise ValueError("Project wheel frontend is outside the active virtual environment.")
    virtual_environment_configuration = virtual_environment / "pyvenv.cfg"
    if (
        virtual_environment_configuration.is_symlink()
        or not virtual_environment_configuration.is_file()
    ):
        raise ValueError("Build virtual-environment configuration is not a regular file.")
    try:
        virtual_environment_configuration_bytes = (
            virtual_environment_configuration.read_bytes()
        )
        virtual_environment_configuration_text = (
            virtual_environment_configuration_bytes.decode("utf-8")
        )
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("Build virtual-environment configuration is unreadable.") from error
    virtual_environment_policy: dict[str, str] = {}
    for line in virtual_environment_configuration_text.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("Build virtual-environment configuration is malformed.")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip().casefold()
        value = raw_value.strip()
        if (
            re.fullmatch(r"[a-z][a-z0-9-]*", key) is None
            or not value
            or key in virtual_environment_policy
        ):
            raise ValueError("Build virtual-environment configuration is malformed.")
        virtual_environment_policy[key] = value
    include_system_site_packages = virtual_environment_policy.get(
        "include-system-site-packages",
        "",
    )
    if include_system_site_packages.casefold() != "false":
        raise ValueError("Build virtual environment permits system site-packages.")
    if site.ENABLE_USER_SITE is not False:
        raise ValueError("Build virtual environment permits user site-packages.")

    zlib_runtime_version = _text(
        zlib.ZLIB_RUNTIME_VERSION,
        "zlib runtime version",
        128,
    )
    zlib_path_value = getattr(zlib, "__file__", None)
    if not isinstance(zlib_path_value, str):
        raise FileNotFoundError("The zlib extension module has no file identity.")
    zlib_path = Path(zlib_path_value)
    if zlib_path.is_symlink():
        raise ValueError("The zlib extension module may not be a symlink.")
    zlib_path = zlib_path.resolve(strict=True)
    if not zlib_path.is_file():
        raise FileNotFoundError("The zlib extension module is absent.")

    distribution_rows = tuple(
        _build_distribution_snapshot(name) for name in ("pip", "setuptools")
    )
    distributions = tuple(row[0] for row in distribution_rows)
    site_packages = distribution_rows[0][1]
    if distribution_rows[1][1] != site_packages:
        raise ValueError("Pip and setuptools are installed in different environments.")
    site_packages = site_packages.resolve(strict=True)
    configured_site_packages: list[Path] = []
    try:
        for value in site.getsitepackages():
            configured_site_packages.append(Path(value).resolve(strict=True))
        purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
        platlib = Path(sysconfig.get_path("platlib")).resolve(strict=True)
    except (OSError, TypeError) as error:
        raise ValueError("Build site-package policy cannot be resolved.") from error
    if tuple(configured_site_packages) != (site_packages,):
        raise ValueError("Build interpreter exposes an external site-packages root.")
    if purelib != site_packages or platlib != site_packages:
        raise ValueError("Build installation schemes expose another package root.")
    effective_import_paths = tuple(sys.path)
    if not effective_import_paths:
        raise ValueError("Build import path is empty.")
    seen_import_paths: set[str] = set()
    observed_site_packages = 0
    for value in effective_import_paths:
        if type(value) is not str:
            raise TypeError("Build import path contains a non-text entry.")
        normalized = require_nfc_text(
            value,
            "effective build import path",
            maximum_bytes=4096,
        )
        candidate = Path(normalized)
        if not candidate.is_absolute():
            raise ValueError("Build import path contains a relative external root.")
        resolved = candidate.resolve(strict=False)
        if resolved == site_packages:
            observed_site_packages += 1
        else:
            try:
                resolved.relative_to(python_installation)
            except ValueError as error:
                raise ValueError(
                    "Build import path contains an unprojected external root."
                ) from error
        key = normalized.casefold()
        if key in seen_import_paths:
            raise ValueError("Build import paths collide under case folding.")
        seen_import_paths.add(key)
    if observed_site_packages != 1:
        raise ValueError("Build import path must contain one projected site-packages root.")
    if distributions[1].version != "80.9.0":
        raise ValueError("Setuptools differs from the authorized exact version 80.9.0.")
    pip_spec = importlib_util.find_spec("pip")
    setuptools_spec = importlib_util.find_spec("setuptools")
    backend_spec = PathFinder.find_spec(
        "setuptools.build_meta",
        [os.fspath(site_packages / "setuptools")],
    )
    if pip_spec is None or setuptools_spec is None or backend_spec is None:
        raise ValueError("Required build imports cannot be resolved.")
    imports = (
        _build_import_snapshot(
            "pip",
            pip_spec,
            site_packages=site_packages,
            expected_path="pip/__init__.py",
        ),
        _build_import_snapshot(
            "setuptools",
            setuptools_spec,
            site_packages=site_packages,
            expected_path="setuptools/__init__.py",
        ),
        _build_import_snapshot(
            "setuptools.build_meta",
            backend_spec,
            site_packages=site_packages,
            expected_path="setuptools/build_meta.py",
        ),
    )
    site_packages_file_count, site_packages_projection_sha256 = (
        _tree_file_projection(site_packages, label="build site-packages")
    )
    python_installation_entry_count, python_installation_projection_sha256 = (
        _tree_file_projection(
            python_installation,
            label="CPython installation",
            allow_relative_symlinks=True,
        )
    )
    virtual_environment_entry_count, virtual_environment_projection_sha256 = (
        _tree_file_projection(
            virtual_environment,
            label="build virtual environment",
            allow_relative_symlinks=True,
            allowed_absolute_symlink_root=python_installation,
        )
    )
    probe_input = b"KIRBY2_RELEASE_ARCHIVE_ENCODER_PROBE_V1\n" + bytes(range(256))
    return ReleaseBuildRuntimeSnapshotV1(
        runtime=runtime,
        python_executable_sha256=hashlib.sha256(active_python.read_bytes()).hexdigest(),
        virtual_environment_configuration_sha256=hashlib.sha256(
            virtual_environment_configuration_bytes
        ).hexdigest(),
        effective_import_paths=effective_import_paths,
        virtual_environment_entry_count=virtual_environment_entry_count,
        virtual_environment_projection_sha256=(
            virtual_environment_projection_sha256
        ),
        zlib_extension_sha256=hashlib.sha256(zlib_path.read_bytes()).hexdigest(),
        zlib_runtime_version=zlib_runtime_version,
        archive_encoder_probe_sha256=hashlib.sha256(
            canonical_gzip_bytes(probe_input)
        ).hexdigest(),
        distributions=distributions,
        imports=imports,
        python_installation_entry_count=python_installation_entry_count,
        python_installation_projection_sha256=(
            python_installation_projection_sha256
        ),
        site_packages_file_count=site_packages_file_count,
        site_packages_projection_sha256=site_packages_projection_sha256,
    )


def release_resource_preflight(
    bundle: ReleaseProtocolBundleV1,
    *,
    wheelhouse_root: Path,
    provider_inventory: Path | None = None,
) -> ReleaseResourcePreflightV1:
    """Read exact wheel/provider metadata without downloading or connecting."""

    root = _absolute(wheelhouse_root, "wheelhouse root")
    items: list[ReleaseResourceItemV1] = []
    protocol_commit_value, protocol_matches_commit = (
        _resolve_release_protocol_commit(bundle)
    )
    items.append(
        ReleaseResourceItemV1(
            resource_id="wo40-d-protocol-commit",
            target="all",
            kind="PROTOCOL_COMMIT",
            status=(
                "PASS"
                if protocol_matches_commit
                else (
                    "MISSING"
                    if protocol_commit_value is None
                    else "DIGEST_MISMATCH"
                )
            ),
            expected_sha256=None,
            observed_sha256=None,
            detail=(
                "Resolved protocol revision contains every exact byte bound by the report."
                if protocol_matches_commit
                else (
                    "Protocol-owning commit could not be resolved."
                    if protocol_commit_value is None
                    else "Current protocol bytes differ from the resolved protocol revision."
                )
            ),
        )
    )
    for wheel in bundle.requirements_lock.wheels:
        path = root / wheel.target / wheel.filename
        if not path.is_file():
            items.append(
                ReleaseResourceItemV1(
                    resource_id=wheel.filename,
                    target=wheel.target,
                    kind="LOCKED_DEPENDENCY_WHEEL",
                    status="MISSING",
                    expected_sha256=wheel.sha256,
                    observed_sha256=None,
                    detail=f"Expected preexisting file under {wheel.target}/; no download was attempted.",
                )
            )
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            items.append(
                ReleaseResourceItemV1(
                    resource_id=wheel.filename,
                    target=wheel.target,
                    kind="LOCKED_DEPENDENCY_WHEEL",
                    status="MISSING",
                    expected_sha256=wheel.sha256,
                    observed_sha256=None,
                    detail="Preexisting wheel is unreadable; no download was attempted.",
                )
            )
            continue
        observed = hashlib.sha256(raw).hexdigest()
        items.append(
            ReleaseResourceItemV1(
                resource_id=wheel.filename,
                target=wheel.target,
                kind="LOCKED_DEPENDENCY_WHEEL",
                status="PASS" if observed == wheel.sha256 else "DIGEST_MISMATCH",
                expected_sha256=wheel.sha256,
                observed_sha256=observed,
                detail="Exact local wheel digest matched." if observed == wheel.sha256 else "Local wheel bytes differ from the frozen lock.",
            )
        )
    pip_frontend = bundle.repository_root / ".venv/bin/pip"
    tools = (
        ("git", shutil.which("git")),
        ("project-wheel-frontend", pip_frontend),
    )
    for resource_id, candidate in tools:
        path = None if candidate is None else Path(candidate)
        ready = path is not None and path.is_file() and os.access(path, os.X_OK)
        observed = None
        if ready:
            try:
                observed = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                ready = False
        items.append(
            ReleaseResourceItemV1(
                resource_id=resource_id,
                target="build-host",
                kind="EXTERNAL_PACKAGING_TOOL",
                status="PASS" if ready else "MISSING",
                expected_sha256=None,
                observed_sha256=observed,
                detail=(
                    "Executable is available and its local bytes were fingerprinted."
                    if ready
                    else "Required local executable is absent or unreadable."
                ),
            )
        )

    build_runtime: ReleaseBuildRuntimeSnapshotV1 | None = None
    runtime_status = "PASS"
    runtime_detail = (
        "Isolated CPython startup, the virtual environment, import roots, zlib, "
        "archive encoder, and exact pip/setuptools bytes were fingerprinted."
    )
    try:
        build_runtime = inspect_release_build_runtime(
            bundle,
            pip_frontend=pip_frontend,
        )
    except FileNotFoundError as error:
        runtime_status = "MISSING"
        runtime_detail = str(error)
    except (OSError, TypeError, ValueError) as error:
        runtime_status = "INVALID"
        runtime_detail = str(error)
    items.append(
        ReleaseResourceItemV1(
            resource_id="build-runtime-and-backend",
            target="build-host",
            kind="BUILD_RUNTIME_SNAPSHOT",
            status=runtime_status,
            expected_sha256=None,
            observed_sha256=(
                None if build_runtime is None else build_runtime.logical_sha256
            ),
            detail=runtime_detail,
        )
    )

    providers: dict[str, ReleaseCleanProviderV1] = {}
    inventory_status = "MISSING"
    inventory_digest = None
    inventory_detail = "No secret-free provider inventory was supplied."
    if provider_inventory is not None:
        inventory = _absolute(provider_inventory, "provider inventory")
        if inventory.is_file():
            try:
                raw_inventory = inventory.read_bytes()
                inventory_digest = hashlib.sha256(raw_inventory).hexdigest()
                parsed_inventory = ReleaseCleanProviderInventoryV1.from_bytes(
                    raw_inventory
                )
                providers = parsed_inventory.by_target()
                inventory_status = "PASS"
                inventory_detail = (
                    "Secret-free provider inventory parsed and was fingerprinted."
                )
            except (OSError, TypeError, ValueError):
                inventory_status = "INVALID"
                inventory_detail = (
                    "Provider inventory is unreadable or differs from the frozen schema."
                )
        else:
            inventory_detail = "The supplied provider inventory does not exist."
    items.append(
        ReleaseResourceItemV1(
            resource_id="clean-provider-inventory",
            target="all",
            kind="CLEAN_PROVIDER_INVENTORY",
            status=inventory_status,
            expected_sha256=None,
            observed_sha256=inventory_digest,
            detail=inventory_detail,
        )
    )
    for target in bundle.platform_protocol.targets:
        provider = providers.get(target.target_id)
        if provider is None:
            status = "MISSING"
            detail = "No matching clean-provider capability record is available."
            observed = None
        else:
            status, detail = provider.readiness(target)
            observed = provider.fingerprint
            detail = f"{detail} {provider.capability_summary}"
        items.append(
            ReleaseResourceItemV1(
                resource_id=f"clean-provider-{target.target_id}",
                target=target.target_id,
                kind="REAL_CLEAN_ENVIRONMENT_PROVIDER",
                status=status,
                expected_sha256=None,
                observed_sha256=observed,
                detail=detail,
            )
        )
    starter = build_release_starter_set()
    for entry, build in zip(starter.entries, starter.builds, strict=True):
        role = entry.role.value.casefold()
        items.append(
            ReleaseResourceItemV1(
                resource_id=f"starter-{role}-inventory",
                target="all",
                kind="CANDIDATE_STARTER_PACK_INVENTORY",
                status="PASS",
                expected_sha256=entry.pack_id,
                observed_sha256=build.manifest.pack_id,
                detail=f"Verified committed inventory with {len(build.manifest.inventory)} members.",
            )
        )
        items.append(
            ReleaseResourceItemV1(
                resource_id=f"starter-{role}-manifest",
                target="all",
                kind="CANDIDATE_STARTER_MANIFEST",
                status="PASS",
                expected_sha256=entry.manifest_sha256,
                observed_sha256=entry.manifest_sha256,
                detail="Committed starter source-manifest bytes matched the frozen layout.",
            )
        )
        items.append(
            ReleaseResourceItemV1(
                resource_id=f"starter-{role}-archive",
                target="all",
                kind="CANDIDATE_STARTER_PACK_ARCHIVE",
                status="PASS",
                expected_sha256=entry.archive_sha256,
                observed_sha256=build.transport_sha256,
                detail="In-memory deterministic starter archive verified with its owning adapter.",
            )
        )
    return ReleaseResourcePreflightV1(
        protocol_set_sha256=bundle.protocol_set_sha256,
        protocol_commit=protocol_commit_value,
        items=tuple(items),
        build_runtime=build_runtime,
        no_network=True,
    )


def _candidate_tree_objects(
    repository: Path,
    candidate_commit: str,
) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", candidate_commit],
        cwd=repository,
        env=_candidate_git_environment(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit tree cannot be read from the repository",
        )
    objects: dict[str, str] = {}
    casefolded: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate tree contains a malformed Git inventory record",
            )
        mode, object_type, raw_object_id = fields
        if mode not in {b"100644", b"100755"} or object_type != b"blob":
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate tree contains a non-regular archive member",
            )
        try:
            path = raw_path.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
            normalize_release_path(path, label="candidate tree path")
            normalize_release_path(
                f"kirby2-{RELEASE_VERSION_V1}/{path}",
                label="candidate source-archive path",
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate tree contains a noncanonical release path",
            ) from error
        if _COMMIT.fullmatch(object_id) is None:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate tree contains an invalid Git object identity",
            )
        folded = path.casefold()
        if path in objects or folded in casefolded:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate tree paths are not unique under case folding",
            )
        objects[path] = object_id
        casefolded.add(folded)
    if not objects:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "candidate tree inventory is empty",
        )
    return objects


def _candidate_blob_bytes(
    repository: Path,
    object_ids: tuple[str, ...],
) -> dict[str, bytes]:
    ordered_ids = tuple(dict.fromkeys(object_ids))
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repository,
        env=_candidate_git_environment(),
        input=b"".join(f"{object_id}\n".encode("ascii") for object_id in ordered_ids),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "candidate blob inventory cannot be read from Git",
        )
    blobs: dict[str, bytes] = {}
    offset = 0
    for object_id in ordered_ids:
        line_end = result.stdout.find(b"\n", offset)
        if line_end < 0:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate blob batch response is incomplete",
            )
        header = result.stdout[offset:line_end].split()
        if len(header) != 3 or header[0] != object_id.encode("ascii") or header[1] != b"blob":
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate object is not an exact Git blob",
            )
        try:
            size = int(header[2].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate blob size is invalid",
            ) from error
        body_start = line_end + 1
        body_end = body_start + size
        if body_end >= len(result.stdout) or result.stdout[body_end : body_end + 1] != b"\n":
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
                "candidate blob batch framing is invalid",
            )
        blobs[object_id] = result.stdout[body_start:body_end]
        offset = body_end + 1
    if offset != len(result.stdout):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "candidate blob batch contains trailing data",
        )
    return blobs


_CANDIDATE_GIT_ENVIRONMENT_OVERRIDES_V1 = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_WORK_TREE",
    }
)
_CANDIDATE_UNTRACKED_INPUT_PATHS_V1 = (
    "build",
    "dist",
    "kirby2",
    "kirby2.egg-info",
    "MANIFEST.in",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "release/launchers",
    "docs/INSTRUCTOR_RESEARCH.md",
    "docs/LIMITATIONS.md",
    "docs/SCENARIO_AUTHORING.md",
    "docs/SECURITY_PRIVACY.md",
    "docs/TROUBLESHOOTING.md",
    "docs/USER_GUIDE.md",
)


def _candidate_git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _CANDIDATE_GIT_ENVIRONMENT_OVERRIDES_V1
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["LC_ALL"] = "C"
    return environment


def _require_candidate_checkout_clean(
    repository: Path,
    candidate_commit: str,
) -> None:
    environment = _candidate_git_environment()
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
    )
    head_commit = head.stdout.decode("ascii", errors="replace").strip()
    if head.returncode != 0 or head_commit != candidate_commit:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY,
            "checked-out HEAD is not the requested release candidate",
        )
    index_flags = subprocess.run(
        ["git", "ls-files", "-v", "-z", "--"],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
    )
    if index_flags.returncode != 0 or any(
        record and not record.startswith(b"H ")
        for record in index_flags.stdout.split(b"\0")
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY,
            "release candidate index contains a non-normal tracked-file flag",
        )
    for arguments in (
        [
            "git",
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--ignore-submodules=none",
            "--",
        ],
        [
            "git",
            "diff",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--ignore-submodules=none",
            "--",
        ],
    ):
        clean = subprocess.run(
            arguments,
            cwd=repository,
            env=environment,
            capture_output=True,
            check=False,
        )
        if clean.returncode != 0:
            raise ReleaseBuildRefused(
                ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY,
                "release candidate has staged or unstaged tracked changes",
            )
    untracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--others",
            "-z",
            "--",
            *_CANDIDATE_UNTRACKED_INPUT_PATHS_V1,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
    )
    unexpected_untracked: list[bytes] = []
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            unexpected_untracked.append(raw_path)
            continue
        parts = path.split("/")
        interpreter_cache = "__pycache__" in parts and path.endswith(".pyc")
        if not interpreter_cache:
            unexpected_untracked.append(raw_path)
    if untracked.returncode != 0 or unexpected_untracked:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_SOURCE_DIRTY,
            "release candidate has an untracked file in a build-input namespace",
        )


def verify_release_candidate_inputs(
    bundle: ReleaseProtocolBundleV1,
    candidate_commit: str,
    *,
    require_checkout: bool = True,
) -> ReleaseCandidateInputsV1:
    """Bind frozen protocols and the source lock to one immutable Git tree.

    Build execution requires the candidate to be the clean checked-out ``HEAD``.
    Read-only verification after the evidence-only WO40-F commit instead resolves
    the original candidate's Git objects while allowing ``HEAD`` to advance.  The
    latter never grants build authority and never reads candidate bytes from the
    working tree.
    """

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("candidate verification requires a release protocol bundle")
    if _COMMIT.fullmatch(candidate_commit) is None:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit must be exactly forty lowercase hexadecimal characters",
        )
    repository = bundle.repository_root
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", f"{candidate_commit}^{{commit}}"],
        cwd=repository,
        env=_candidate_git_environment(),
        capture_output=True,
        check=False,
    )
    resolved_commit = resolved.stdout.decode("ascii", errors="replace").strip()
    if resolved.returncode != 0 or resolved_commit != candidate_commit:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit does not resolve to that exact repository commit",
        )
    metadata = subprocess.run(
        ["git", "show", "-s", "--format=%T%x00%ct", candidate_commit],
        cwd=repository,
        env=_candidate_git_environment(),
        capture_output=True,
        check=False,
    )
    metadata_fields = metadata.stdout.rstrip(b"\n").split(b"\0")
    try:
        candidate_tree = metadata_fields[0].decode("ascii")
        source_date_epoch = int(metadata_fields[1].decode("ascii"))
    except (IndexError, UnicodeDecodeError, ValueError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit metadata cannot be resolved exactly",
        ) from error
    if (
        metadata.returncode != 0
        or len(metadata_fields) != 2
        or _COMMIT.fullmatch(candidate_tree) is None
        or source_date_epoch < 0
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit metadata cannot be resolved exactly",
        )
    if type(require_checkout) is not bool:
        raise TypeError("candidate checkout policy must be Boolean")
    if require_checkout:
        _require_candidate_checkout_clean(repository, candidate_commit)

    tree_objects = _candidate_tree_objects(repository, candidate_commit)
    lock_path = "release/performance_runner_sources.lock"
    if lock_path not in tree_objects:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISSING,
            "candidate commit does not contain the frozen runner-source lock",
        )
    missing_protocols = tuple(
        path for path in RELEASE_PROTOCOL_PATHS_V1 if path not in tree_objects
    )
    if missing_protocols:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_PROTOCOL_MISMATCH,
            "candidate commit does not contain every frozen release protocol",
        )
    preflight_report_path = "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
    if preflight_report_path not in tree_objects:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE,
            "candidate commit does not contain the passing resource-preflight report",
        )
    source_paths = tuple(
        sorted(
            (
                path
                for path in tree_objects
                if path == "pyproject.toml" or path.startswith("kirby2/")
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )
    missing_candidate_inputs: list[str] = []
    for item in bundle.artifact_layout.members:
        if item.availability != "WO40_E" or item.source_path == "{candidate_commit}":
            continue
        if item.source_path.endswith("/{filename}"):
            prefix = item.source_path.removesuffix("{filename}")
            ready = any(path.startswith(prefix) for path in tree_objects)
        else:
            ready = item.source_path in tree_objects
        if not ready:
            missing_candidate_inputs.append(item.source_path)
    if missing_candidate_inputs:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.FUTURE_SOURCE_INPUT_MISSING,
            "candidate commit omits one or more frozen WO40-E inputs",
        )
    requested_paths = (
        *source_paths,
        lock_path,
        *RELEASE_PROTOCOL_PATHS_V1,
        preflight_report_path,
    )
    blobs = _candidate_blob_bytes(
        repository,
        tuple(tree_objects[path] for path in requested_paths),
    )
    candidate_lock_bytes = blobs[tree_objects[lock_path]]
    current_lock_path = repository / lock_path
    if not current_lock_path.is_file():
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISSING,
            "working checkout does not contain the frozen runner-source lock",
        )
    try:
        current_lock_matches = current_lock_path.read_bytes() == candidate_lock_bytes
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "working source lock cannot be read exactly",
        ) from error
    if not current_lock_matches:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "working source lock differs from the candidate lock blob",
        )
    try:
        locked_tree = RunnerSourceTreeV1.from_bytes(candidate_lock_bytes)
        source_entries = tuple(
            RunnerSourceEntryV1(
                path=path,
                sha256=hashlib.sha256(blobs[tree_objects[path]]).hexdigest(),
            )
            for path in source_paths
        )
        reproduced_tree = RunnerSourceTreeV1(
            source_manifest=source_entries,
            source_manifest_sha256=hashlib.sha256(
                canonical_json_bytes([item.as_dict() for item in source_entries])
            ).hexdigest(),
        )
    except (TypeError, ValueError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "candidate runner-source lock is invalid",
        ) from error
    if locked_tree != reproduced_tree:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISMATCH,
            "candidate source projection does not reproduce its runner-source lock",
        )

    candidate_protocols = tuple(
        ReleaseProtocolFileV1(
            path=path,
            sha256=hashlib.sha256(blobs[tree_objects[path]]).hexdigest(),
        )
        for path in RELEASE_PROTOCOL_PATHS_V1
    )
    try:
        current_protocols_match = all(
            (repository / path).read_bytes() == blobs[tree_objects[path]]
            for path in RELEASE_PROTOCOL_PATHS_V1
        )
    except OSError as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_PROTOCOL_MISMATCH,
            "working release protocol bytes cannot be read",
        ) from error
    candidate_protocol_set = hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in candidate_protocols])
    ).hexdigest()
    if (
        not current_protocols_match
        or candidate_protocols != bundle.protocol_files
        or candidate_protocol_set != bundle.protocol_set_sha256
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_PROTOCOL_MISMATCH,
            "candidate protocol, dependency, or layout bytes differ from the loaded bundle",
        )
    history = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--first-parent",
            "--format=%H",
            candidate_commit,
            "--",
            *RELEASE_PROTOCOL_PATHS_V1,
        ],
        cwd=repository,
        env=_candidate_git_environment(),
        capture_output=True,
        check=False,
    )
    protocol_commit = history.stdout.decode("ascii", errors="replace").strip()
    report_bytes = blobs[tree_objects[preflight_report_path]]
    try:
        report_lines = report_bytes.decode("utf-8").splitlines()
        current_report_matches = (
            repository / preflight_report_path
        ).read_bytes() == report_bytes
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE,
            "candidate resource-preflight report cannot be read exactly",
        ) from error
    if (
        history.returncode != 0
        or _COMMIT.fullmatch(protocol_commit) is None
        or not current_report_matches
        or "Status: `PASS`" not in report_lines
        or f"Protocol set SHA-256: `{candidate_protocol_set}`" not in report_lines
        or f"WO40-D protocol commit: `{protocol_commit}`" not in report_lines
    ):
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_PROTOCOL_MISMATCH,
            "candidate protocol identities differ from the passing resource preflight",
        )
    if require_checkout:
        _require_candidate_checkout_clean(repository, candidate_commit)
    logical_projection = ReleaseLogicalBuildProjectionV1(
        release_version=RELEASE_VERSION_V1,
        candidate_commit=candidate_commit,
        source_manifest_sha256=reproduced_tree.source_manifest_sha256,
        protocol_files=candidate_protocols,
        starter_set_entries_sha256=str(
            bundle.artifact_layout.starter_set["entries_sha256"]
        ),
    )
    return ReleaseCandidateInputsV1(
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        source_date_epoch=source_date_epoch,
        tree_entry_count=len(tree_objects),
        source_entry_count=len(source_entries),
        source_lock_object_id=tree_objects[lock_path],
        source_lock_sha256=hashlib.sha256(candidate_lock_bytes).hexdigest(),
        source_manifest_sha256=reproduced_tree.source_manifest_sha256,
        protocol_files=candidate_protocols,
        protocol_commit=protocol_commit,
        protocol_set_sha256=candidate_protocol_set,
        resource_preflight_sha256=hashlib.sha256(report_bytes).hexdigest(),
        logical_build_id=logical_projection.logical_build_id,
        tracked_tree_clean=True,
    )


def plan_release_build(
    bundle: ReleaseProtocolBundleV1,
    *,
    candidate_commit: str,
    output_root: Path,
) -> ReleaseCommandOutcomeV1:
    _absolute(output_root, "release output root")
    source_lock = bundle.repository_root / "release/performance_runner_sources.lock"
    if not source_lock.is_file():
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.NOT_EXERCISED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail="WO40-E has not frozen the mechanical runner-source lock.",
            refusal_code=ReleaseBuildRefusalCodeV1.SOURCE_LOCK_MISSING.value,
            payload={
                "candidate_commit": candidate_commit,
                "frontends": RELEASE_BUILD_FRONTENDS_V1,
                "output_root": str(output_root),
                "required_source_lock": "release/performance_runner_sources.lock",
            },
        )
    try:
        candidate_inputs = verify_release_candidate_inputs(bundle, candidate_commit)
    except ReleaseBuildRefused as error:
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.REFUSED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail=error.detail,
            refusal_code=error.code.value,
            payload={
                "candidate_commit": candidate_commit,
                "frontends": RELEASE_BUILD_FRONTENDS_V1,
                "output_root": str(output_root),
            },
        )
    provider_inventory = bundle.repository_root / ".kirby2/release/clean-providers.toml"
    try:
        preflight = release_resource_preflight(
            bundle,
            wheelhouse_root=(bundle.repository_root / "release/wheelhouse").resolve(),
            provider_inventory=(
                provider_inventory.resolve()
                if provider_inventory.is_file()
                else None
            ),
        )
        expected_report = preflight.markdown().encode("utf-8")
        observed_report = (
            bundle.repository_root / "KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md"
        ).read_bytes()
    except (OSError, ReleaseBuildRefused, TypeError, ValueError) as error:
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.REFUSED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail=f"Live offline resource preflight failed: {type(error).__name__}.",
            refusal_code=ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE.value,
            payload={
                "candidate_commit": candidate_commit,
                "candidate_inputs": candidate_inputs.as_dict(),
                "output_root": str(output_root),
            },
        )
    if preflight.status != "PASS" or observed_report != expected_report:
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.REFUSED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail="Live offline resources or their exact passing report changed after preflight.",
            refusal_code=ReleaseBuildRefusalCodeV1.RESOURCE_PREFLIGHT_INCOMPLETE.value,
            payload={
                "candidate_commit": candidate_commit,
                "candidate_inputs": candidate_inputs.as_dict(),
                "missing_resource_ids": [
                    item.resource_id for item in preflight.missing_items
                ],
                "output_root": str(output_root),
            },
        )
    return ReleaseCommandOutcomeV1(
        command_id="BUILD_RELEASE",
        status=ReleaseCommandStatusV1.READY,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail="All preregistered build inputs are addressable; artifact execution belongs to WO40-F.",
        payload={
            "artifact_ids": [row[0] for row in RELEASE_ARTIFACT_ROWS_V1],
            "candidate_commit": candidate_commit,
            "candidate_inputs": candidate_inputs.as_dict(),
            "frontends": RELEASE_BUILD_FRONTENDS_V1,
            "network": "FORBIDDEN",
            "output_root": str(output_root),
            "resource_preflight": {
                "build_runtime": (
                    None
                    if preflight.build_runtime is None
                    else preflight.build_runtime.as_dict()
                ),
                "protocol_commit": preflight.protocol_commit,
                "report_sha256": hashlib.sha256(expected_report).hexdigest(),
                "resource_snapshot_sha256": preflight.resource_snapshot_sha256,
                "status": preflight.status,
            },
        },
    )


def build_release_artifacts(
    bundle: ReleaseProtocolBundleV1,
    *,
    candidate_commit: str,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Execute the frozen WO40-F builder through its isolated implementation.

    The lazy import keeps protocol loading and resource preflight independent from
    the executor while preserving this module's established public command seam.
    """

    from .artifacts import build_release_artifacts as execute_release_artifacts

    return execute_release_artifacts(
        bundle,
        candidate_commit=candidate_commit,
        artifact_root=artifact_root,
    )


def verify_release_artifacts(
    bundle: ReleaseProtocolBundleV1,
    artifact_root: Path,
    *,
    candidate_commit: str | None = None,
) -> ReleaseCommandOutcomeV1:
    """Deeply reconstruct and verify one activated immutable artifact set."""

    from .artifacts import verify_release_artifacts as verify_artifact_set

    return verify_artifact_set(
        bundle,
        artifact_root=artifact_root,
        candidate_commit=candidate_commit,
    )


__all__ = [
    "RELEASE_ARTIFACT_LAYOUT_SCHEMA_ID_V1",
    "RELEASE_BUILD_FRONTENDS_V1",
    "RELEASE_BUILD_PROTOCOL_SCHEMA_ID_V1",
    "RELEASE_COMMAND_OUTCOME_SCHEMA_ID_V1",
    "RELEASE_RESOURCE_PREFLIGHT_SCHEMA_ID_V1",
    "ReleaseArtifactLayoutV1",
    "ReleaseBuildDistributionV1",
    "ReleaseBuildImportV1",
    "ReleaseBuildRefusalCodeV1",
    "ReleaseBuildRefused",
    "ReleaseBuildRuntimeSnapshotV1",
    "ReleaseCandidateInputsV1",
    "ReleaseCleanProviderInventoryV1",
    "ReleaseCleanProviderV1",
    "ReleaseCommandOutcomeV1",
    "ReleaseCommandStatusV1",
    "ReleaseLayoutArtifactV1",
    "ReleaseLayoutMemberV1",
    "ReleasePerformanceProtocolHeaderV1",
    "ReleaseProtocolBundleV1",
    "ReleaseResourceItemV1",
    "ReleaseResourcePreflightV1",
    "inspect_release_build_runtime",
    "load_release_protocol_bundle",
    "plan_release_build",
    "release_resource_preflight",
    "verify_release_candidate_inputs",
    "build_release_artifacts",
    "verify_release_artifacts",
]
