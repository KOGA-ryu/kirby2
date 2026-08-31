"""Offline release protocol loading, build planning, and resource preflight.

The public commands in this module are fail-closed.  Before WO40-E freezes a source
tree they can parse and digest every preregistered input, explain exact missing
resources, and produce a deterministic dispatch plan, but they cannot accidentally
build from the working tree or fetch a dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from enum import Enum
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
    ReleaseProtocolFileV1,
)
from .packaging import (
    RELEASE_SOURCE_CLASS_ORDER_V1,
    ReleaseSourceClassV1,
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
    CANDIDATE_COMMIT_INVALID = "CANDIDATE_COMMIT_INVALID"
    CANDIDATE_SOURCE_DIRTY = "CANDIDATE_SOURCE_DIRTY"
    RESOURCE_PREFLIGHT_INCOMPLETE = "RESOURCE_PREFLIGHT_INCOMPLETE"
    WHEEL_MISSING = "WHEEL_MISSING"
    WHEEL_DIGEST_MISMATCH = "WHEEL_DIGEST_MISMATCH"
    CLEAN_PROVIDER_MISSING = "CLEAN_PROVIDER_MISSING"
    ARTIFACT_INDEX_MISSING = "ARTIFACT_INDEX_MISSING"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    NETWORK_POLICY_MISMATCH = "NETWORK_POLICY_MISMATCH"
    FUTURE_SOURCE_INPUT_MISSING = "FUTURE_SOURCE_INPUT_MISSING"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"


class ReleaseBuildRefused(ValueError):
    def __init__(self, code: ReleaseBuildRefusalCodeV1, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


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
class ReleaseResourcePreflightV1:
    protocol_set_sha256: str
    protocol_commit: str | None
    items: tuple[ReleaseResourceItemV1, ...]
    no_network: bool

    schema_id: ClassVar[str] = RELEASE_RESOURCE_PREFLIGHT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        require_sha256(self.protocol_set_sha256, "preflight protocol-set digest")
        if self.protocol_commit is not None and _COMMIT.fullmatch(self.protocol_commit) is None:
            raise ValueError("preflight protocol commit is invalid")
        if self.no_network is not True:
            raise ValueError("release resource preflight must be no-network")
        if type(self.items) is not tuple or any(
            type(item) is not ReleaseResourceItemV1 for item in self.items
        ):
            raise TypeError("release preflight items must use exact V1 records")

    @property
    def status(self) -> str:
        return "PASS" if self.items and all(item.status == "PASS" for item in self.items) else "NOT_READY"

    @property
    def missing_items(self) -> tuple[ReleaseResourceItemV1, ...]:
        return tuple(item for item in self.items if item.status != "PASS")

    def as_dict(self) -> dict[str, object]:
        return {
            "items": [item.as_dict() for item in self.items],
            "missing_item_count": len(self.missing_items),
            "no_network": self.no_network,
            "protocol_commit": self.protocol_commit,
            "protocol_set_sha256": self.protocol_set_sha256,
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
    tools = (
        ("git", shutil.which("git")),
        ("project-wheel-frontend", bundle.repository_root / ".venv/bin/pip"),
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
        no_network=True,
    )


def plan_release_build(
    bundle: ReleaseProtocolBundleV1,
    *,
    candidate_commit: str,
    output_root: Path,
) -> ReleaseCommandOutcomeV1:
    _absolute(output_root, "release output root")
    if _COMMIT.fullmatch(candidate_commit) is None:
        raise ReleaseBuildRefused(
            ReleaseBuildRefusalCodeV1.CANDIDATE_COMMIT_INVALID,
            "candidate commit must be exactly forty lowercase hexadecimal characters",
        )
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
    future_inputs: list[str] = []
    for item in bundle.artifact_layout.members:
        if item.availability != "WO40_E" or item.source_path == "{candidate_commit}":
            continue
        if item.source_path.endswith("/{filename}"):
            directory = bundle.repository_root / item.source_path.removesuffix(
                "/{filename}"
            )
            ready = directory.is_dir() and any(
                path.is_file() for path in directory.iterdir()
            )
        else:
            ready = (bundle.repository_root / item.source_path).is_file()
        if not ready:
            future_inputs.append(item.source_path)
    if future_inputs:
        return ReleaseCommandOutcomeV1(
            command_id="BUILD_RELEASE",
            status=ReleaseCommandStatusV1.NOT_EXERCISED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail="Frozen candidate source inputs are not present.",
            refusal_code=ReleaseBuildRefusalCodeV1.FUTURE_SOURCE_INPUT_MISSING.value,
            payload={"candidate_commit": candidate_commit, "missing_paths": future_inputs},
        )
    return ReleaseCommandOutcomeV1(
        command_id="BUILD_RELEASE",
        status=ReleaseCommandStatusV1.READY,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail="All preregistered build inputs are addressable; artifact execution belongs to WO40-F.",
        payload={
            "artifact_ids": [row[0] for row in RELEASE_ARTIFACT_ROWS_V1],
            "candidate_commit": candidate_commit,
            "frontends": RELEASE_BUILD_FRONTENDS_V1,
            "network": "FORBIDDEN",
            "output_root": str(output_root),
        },
    )


def verify_release_artifacts(
    bundle: ReleaseProtocolBundleV1,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    root = _absolute(artifact_root, "artifact root")
    index_path = root / "release-artifact-index.json"
    if not index_path.is_file():
        return ReleaseCommandOutcomeV1(
            command_id="VERIFY_RELEASE_ARTIFACTS",
            status=ReleaseCommandStatusV1.NOT_EXERCISED,
            protocol_set_sha256=bundle.protocol_set_sha256,
            detail="No immutable release artifact index exists yet.",
            refusal_code=ReleaseBuildRefusalCodeV1.ARTIFACT_INDEX_MISSING.value,
            payload={"expected_index": str(index_path)},
        )
    index = ReleaseArtifactIndexV1.from_bytes(index_path.read_bytes())
    failures: list[dict[str, object]] = []
    for row in index.artifacts:
        path = root / row.artifact_id
        if not path.is_file():
            failures.append({"artifact_id": row.artifact_id, "code": "ARTIFACT_MISSING"})
            continue
        raw = path.read_bytes()
        observed = hashlib.sha256(raw).hexdigest()
        if len(raw) != row.size or observed != row.transport_sha256:
            failures.append(
                {
                    "artifact_id": row.artifact_id,
                    "code": "ARTIFACT_IDENTITY_MISMATCH",
                    "observed_sha256": observed,
                    "observed_size": len(raw),
                }
            )
    return ReleaseCommandOutcomeV1(
        command_id="VERIFY_RELEASE_ARTIFACTS",
        status=ReleaseCommandStatusV1.PASS if not failures else ReleaseCommandStatusV1.FAIL,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail="All six indexed transports matched." if not failures else "One or more indexed transports differed.",
        payload={
            "artifact_index_sha256": index.sha256,
            "candidate_commit": index.candidate_commit,
            "failures": failures,
            "logical_build_id": index.logical_build_id,
        },
    )


__all__ = [
    "RELEASE_ARTIFACT_LAYOUT_SCHEMA_ID_V1",
    "RELEASE_BUILD_FRONTENDS_V1",
    "RELEASE_BUILD_PROTOCOL_SCHEMA_ID_V1",
    "RELEASE_COMMAND_OUTCOME_SCHEMA_ID_V1",
    "RELEASE_RESOURCE_PREFLIGHT_SCHEMA_ID_V1",
    "ReleaseArtifactLayoutV1",
    "ReleaseBuildRefusalCodeV1",
    "ReleaseBuildRefused",
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
    "load_release_protocol_bundle",
    "plan_release_build",
    "release_resource_preflight",
    "verify_release_artifacts",
]
