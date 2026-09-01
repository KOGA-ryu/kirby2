"""Immutable WO40-I publication records and provider-free deep verification.

The performance workload is intentionally absent from this module.  It defines the
only canonical records a coordinator may publish and reconstructs every release
claim from immutable bytes.  Verification never imports a runner, launches a
process, installs an artifact, or mutates the release store.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)

from .performance import (
    RELEASE_ARTIFACT_PASS_BYTES_V1,
    RELEASE_ARTIFACT_WARNING_BYTES_V1,
    RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
    RELEASE_PERFORMANCE_CELL_ORDER_V1,
    RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
    RELEASE_PERFORMANCE_ROOTS_V1,
    RELEASE_PERFORMANCE_WORKER_COUNT_V1,
    RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1,
    RELEASE_TOTAL_WALL_LIMIT_NS_V1,
    ReleaseAuxiliaryPerformanceResultV1,
    ReleaseAuxiliaryPerformanceTemplateV1,
    ReleasePerformanceCellResultV1,
    RunnerSourceTreeV1,
    bind_performance_row_template,
    build_performance_row_template,
    round_div_even,
    validate_performance_attempt_sequence,
    verify_performance_cell_artifacts,
)


RELEASE_PERFORMANCE_GATE_ID_V1: Final[str] = "WO40-I"
RELEASE_PERFORMANCE_TARGET_ID_V1: Final[str] = "macos-arm64"
RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1: Final[str] = "gate-evidence/wo40-i"
RELEASE_PERFORMANCE_AGGREGATE_PATH_V1: Final[str] = (
    "gate-evidence/wo40-i/performance-aggregate.json"
)
RELEASE_PERFORMANCE_ATTEMPT_PATH_V1: Final[str] = (
    "gate-evidence/wo40-i/performance-attempt.json"
)
RELEASE_PERFORMANCE_ACTIVATION_PATH_V1: Final[str] = (
    "gate-evidence/wo40-i/performance-activation.json"
)

RELEASE_PERFORMANCE_RECORD_REFERENCE_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_RECORD_REFERENCE_V1"
)
RELEASE_PERFORMANCE_ARTIFACT_INVENTORY_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_ARTIFACT_INVENTORY_V1"
)
RELEASE_PERFORMANCE_WORK_UNIT_PUBLICATION_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_WORK_UNIT_PUBLICATION_V1"
)
RELEASE_PERFORMANCE_AGGREGATE_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_AGGREGATE_V1"
)
RELEASE_PERFORMANCE_ATTEMPT_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_ATTEMPT_V1"
)
RELEASE_PERFORMANCE_ACTIVATION_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_ACTIVATION_V1"
)

_SEMANTIC_MEMBER_NAMES: Final[tuple[str, ...]] = (
    "run_manifest.json",
    "native_recording.json",
    "semantic_result.json",
    "capabilities.json",
    "checks.json",
    "audit_result.json",
)
_AUXILIARY_ORDER: Final[tuple[str, ...]] = (
    "RELEASE_INTERACTIVE_ACK_V1",
    "RELEASE_TERMINAL_UPDATE_V1",
    "RELEASE_FULL_DAY_GENERATION_V1",
    "RELEASE_FULL_DAY_REPLAY_V1",
    "RELEASE_MICROSCOPE_LOAD_V1",
)
_AUXILIARY_SLUGS: Final[Mapping[str, str]] = {
    "RELEASE_INTERACTIVE_ACK_V1": "interactive-ack",
    "RELEASE_TERMINAL_UPDATE_V1": "terminal-update",
    "RELEASE_FULL_DAY_GENERATION_V1": "full-day-generation",
    "RELEASE_FULL_DAY_REPLAY_V1": "full-day-replay",
    "RELEASE_MICROSCOPE_LOAD_V1": "microscope-load",
}
_REFERENCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "ACTIVATION",
        "AGGREGATE",
        "ARTIFACT_INVENTORY",
        "ATTEMPT",
        "AUDIT_RECORD",
        "AUXILIARY_EVIDENCE",
        "AUXILIARY_RESULT",
        "COMPATIBILITY_SIDECAR",
        "OPERATIONAL_SIDECAR",
        "RESULT_RECORD",
        "SEMANTIC_MEMBER",
    }
)
_CAS_KINDS: Final[frozenset[str]] = frozenset(
    {
        "AUDIT_RECORD",
        "AUXILIARY_EVIDENCE",
        "COMPATIBILITY_SIDECAR",
        "OPERATIONAL_SIDECAR",
        "SEMANTIC_MEMBER",
    }
)
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"PASS", "PASS_WITH_WARNINGS", "FAIL"}
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ATTEMPT_ID = re.compile(r"wo40i-[0-9a-f]{24}\Z")
_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_MAX_RECORD_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_PUBLICATION_FILES: Final[int] = 200_000


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 schema")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _canonical_model_bytes(model: object) -> bytes:
    as_dict = getattr(model, "as_dict", None)
    if not callable(as_dict):
        raise TypeError("performance publication model lacks an encoder")
    return canonical_json_bytes(as_dict())


def _canonical_payload(raw: bytes, label: str) -> dict[str, object]:
    value = load_canonical_json_bytes(raw, label)
    if type(value) is not dict:
        raise TypeError(f"{label} must contain one canonical object")
    return value


def _publication_path(value: object, label: str) -> str:
    path = _text(value, label, 1024)
    if "\\" in path:
        raise ValueError(f"{label} contains a noncanonical separator")
    selected = PurePosixPath(path)
    if (
        selected.is_absolute()
        or len(selected.parts) < 3
        or any(part in {"", ".", ".."} for part in selected.parts)
        or selected.parts[:2] != ("gate-evidence", "wo40-i")
    ):
        raise ValueError(f"{label} is outside the WO40-I publication root")
    return selected.as_posix()


def _work_unit_parts(work_unit_id: object) -> tuple[str, int]:
    selected = _text(work_unit_id, "performance work-unit ID", 256)
    parts = selected.split("/")
    if len(parts) != 3 or parts[0] != "release-perf":
        raise ValueError("performance work-unit ID is invalid")
    try:
        root_seed = int(parts[2])
    except ValueError as error:
        raise ValueError("performance work-unit root is invalid") from error
    cells = {item.value for item in RELEASE_PERFORMANCE_CELL_ORDER_V1}
    if (
        parts[1] not in cells
        or parts[2] != str(root_seed)
        or root_seed not in RELEASE_PERFORMANCE_ROOTS_V1
    ):
        raise ValueError("performance work-unit ID is noncanonical")
    return parts[1], root_seed


def _cas_path(sha256: str) -> str:
    require_sha256(sha256, "CAS digest")
    return (
        f"{RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1}/cas/sha256/"
        f"{sha256[:2]}/{sha256}"
    )


def release_performance_record_paths() -> tuple[str, str, str]:
    """Return aggregate, attempt, then final activation paths."""

    return (
        RELEASE_PERFORMANCE_AGGREGATE_PATH_V1,
        RELEASE_PERFORMANCE_ATTEMPT_PATH_V1,
        RELEASE_PERFORMANCE_ACTIVATION_PATH_V1,
    )


def performance_publication_paths() -> tuple[str, str, str]:
    """Compatibility wrapper for the preregistered DEV-0016 surface."""

    return release_performance_record_paths()


def release_performance_work_unit_paths(
    work_unit_id: str,
    attempt: int,
) -> tuple[str, str]:
    """Return the exact cell-result and artifact-inventory paths for one attempt."""

    cell, root_seed = _work_unit_parts(work_unit_id)
    if type(attempt) is not int or attempt not in {1, 2}:
        raise ValueError("performance publication attempt must be one or two")
    root = (
        f"{RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1}/work-units/"
        f"{cell}/{root_seed}/attempt-{attempt}"
    )
    return f"{root}/cell-result.json", f"{root}/artifact-inventory.json"


def _auxiliary_path(workload_id: str) -> str:
    try:
        slug = _AUXILIARY_SLUGS[workload_id]
    except KeyError as error:
        raise ValueError("performance auxiliary workload is invalid") from error
    return f"{RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1}/auxiliary/{slug}/result.json"


def release_performance_cas_path(sha256: str) -> str:
    """Return the one digest-addressed governed-object path."""

    return _cas_path(sha256)


def release_performance_auxiliary_path(workload_id: str) -> str:
    """Return the fixed result path for one preregistered auxiliary workload."""

    return _auxiliary_path(workload_id)


def release_performance_reference(
    record_id: str,
    kind: str,
    path: str,
    raw: bytes,
) -> "ReleasePerformanceRecordReferenceV1":
    """Build an exact byte-bound reference without publishing the bytes."""

    if type(raw) is not bytes:
        raise TypeError("performance referenced record must use exact bytes")
    return ReleasePerformanceRecordReferenceV1(
        record_id=record_id,
        kind=kind,
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )


@dataclass(frozen=True, slots=True)
class ReleasePerformanceRecordReferenceV1:
    record_id: str
    kind: str
    path: str
    sha256: str
    size: int
    schema_id: str = RELEASE_PERFORMANCE_RECORD_REFERENCE_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.record_id, "performance record ID", 256)
        if self.kind not in _REFERENCE_KINDS:
            raise ValueError("performance record-reference kind is invalid")
        normalized = _publication_path(self.path, "performance record path")
        if normalized != self.path:
            raise ValueError("performance record path is not normalized")
        require_sha256(self.sha256, "performance record digest")
        if type(self.size) is not int or not 0 < self.size <= _MAX_RECORD_BYTES:
            raise ValueError("performance record size is outside its bound")
        if (
            self.schema_id != RELEASE_PERFORMANCE_RECORD_REFERENCE_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("performance record-reference schema differs")
        if self.kind in _CAS_KINDS and self.path != _cas_path(self.sha256):
            raise ValueError("performance CAS reference path differs from its digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "record_id": self.record_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceRecordReferenceV1":
        row = _exact_object(
            value,
            {"record_id", "kind", "path", "sha256", "size", "schema_id", "schema_version"},
            "performance record reference",
        )
        return cls(
            record_id=_text(row["record_id"], "performance record ID", 256),
            kind=_text(row["kind"], "performance record kind", 64),
            path=_text(row["path"], "performance record path", 1024),
            sha256=_text(row["sha256"], "performance record digest", 64),
            size=row["size"],  # type: ignore[arg-type]
            schema_id=_text(row["schema_id"], "performance reference schema", 128),
            schema_version=row["schema_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReleasePerformanceArtifactInventoryV1:
    work_unit_id: str
    attempt: int
    semantic_members: tuple[ReleasePerformanceRecordReferenceV1, ...]
    compatibility_sidecars: tuple[ReleasePerformanceRecordReferenceV1, ...]
    operational_sidecars: tuple[ReleasePerformanceRecordReferenceV1, ...]
    artifact_set_sha256: str | None
    schema_id: str = RELEASE_PERFORMANCE_ARTIFACT_INVENTORY_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        _work_unit_parts(self.work_unit_id)
        if type(self.attempt) is not int or self.attempt not in {1, 2}:
            raise ValueError("performance artifact inventory attempt is invalid")
        for collection, label in (
            (self.semantic_members, "semantic members"),
            (self.compatibility_sidecars, "compatibility sidecars"),
            (self.operational_sidecars, "operational sidecars"),
        ):
            if type(collection) is not tuple or any(
                type(item) is not ReleasePerformanceRecordReferenceV1 for item in collection
            ):
                raise TypeError(f"performance {label} must contain typed references")
        names = tuple(item.record_id for item in self.semantic_members)
        if names != _SEMANTIC_MEMBER_NAMES[: len(names)]:
            raise ValueError("performance semantic members are not one canonical prefix")
        if any(item.kind != "SEMANTIC_MEMBER" for item in self.semantic_members):
            raise ValueError("performance semantic member kind differs")
        compatibility_names = tuple(item.record_id for item in self.compatibility_sidecars)
        if compatibility_names not in {(), ("legacy_digest_bindings.json",)} or any(
            item.kind != "COMPATIBILITY_SIDECAR" for item in self.compatibility_sidecars
        ):
            raise ValueError("performance compatibility-sidecar inventory differs")
        expected_operational = (f"operational_attempt_{self.attempt}.json",)
        if tuple(item.record_id for item in self.operational_sidecars) != expected_operational or any(
            item.kind != "OPERATIONAL_SIDECAR" for item in self.operational_sidecars
        ):
            raise ValueError("performance operational-sidecar inventory differs")
        if self.artifact_set_sha256 is not None:
            require_sha256(self.artifact_set_sha256, "performance artifact-set digest")
        if (
            self.schema_id != RELEASE_PERFORMANCE_ARTIFACT_INVENTORY_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("performance artifact-inventory schema differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_set_sha256": self.artifact_set_sha256,
            "attempt": self.attempt,
            "compatibility_sidecars": [item.as_dict() for item in self.compatibility_sidecars],
            "operational_sidecars": [item.as_dict() for item in self.operational_sidecars],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "semantic_members": [item.as_dict() for item in self.semantic_members],
            "work_unit_id": self.work_unit_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceArtifactInventoryV1":
        row = _exact_object(
            value,
            {
                "artifact_set_sha256", "attempt", "compatibility_sidecars",
                "operational_sidecars", "schema_id", "schema_version",
                "semantic_members", "work_unit_id",
            },
            "performance artifact inventory",
        )
        digest = row["artifact_set_sha256"]
        return cls(
            work_unit_id=_text(row["work_unit_id"], "performance work-unit ID", 256),
            attempt=row["attempt"],  # type: ignore[arg-type]
            semantic_members=tuple(
                ReleasePerformanceRecordReferenceV1.from_dict(item)
                for item in _array(row["semantic_members"], "semantic members")
            ),
            compatibility_sidecars=tuple(
                ReleasePerformanceRecordReferenceV1.from_dict(item)
                for item in _array(row["compatibility_sidecars"], "compatibility sidecars")
            ),
            operational_sidecars=tuple(
                ReleasePerformanceRecordReferenceV1.from_dict(item)
                for item in _array(row["operational_sidecars"], "operational sidecars")
            ),
            artifact_set_sha256=(
                None if digest is None else _text(digest, "performance artifact-set digest", 64)
            ),
            schema_id=_text(row["schema_id"], "artifact-inventory schema", 128),
            schema_version=row["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePerformanceArtifactInventoryV1":
        return cls.from_dict(_canonical_payload(raw, "performance artifact inventory"))


@dataclass(frozen=True, slots=True)
class ReleasePerformanceAttemptPublicationV1:
    attempt: int
    result_record: ReleasePerformanceRecordReferenceV1
    artifact_inventory_record: ReleasePerformanceRecordReferenceV1
    audit_record: ReleasePerformanceRecordReferenceV1 | None

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or self.attempt not in {1, 2}:
            raise ValueError("performance attempt-publication ordinal is invalid")
        if type(self.result_record) is not ReleasePerformanceRecordReferenceV1 or (
            type(self.artifact_inventory_record) is not ReleasePerformanceRecordReferenceV1
        ):
            raise TypeError("performance attempt publication requires typed records")
        if self.result_record.kind != "RESULT_RECORD" or self.result_record.record_id != "cell-result":
            raise ValueError("performance result publication reference differs")
        if (
            self.artifact_inventory_record.kind != "ARTIFACT_INVENTORY"
            or self.artifact_inventory_record.record_id != "artifact-inventory"
        ):
            raise ValueError("performance artifact-inventory publication reference differs")
        if self.audit_record is not None and (
            type(self.audit_record) is not ReleasePerformanceRecordReferenceV1
            or self.audit_record.kind != "AUDIT_RECORD"
            or self.audit_record.record_id != "audit_result.json"
        ):
            raise ValueError("performance audit publication reference differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_inventory_record": self.artifact_inventory_record.as_dict(),
            "attempt": self.attempt,
            "audit_record": None if self.audit_record is None else self.audit_record.as_dict(),
            "result_record": self.result_record.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceAttemptPublicationV1":
        row = _exact_object(
            value,
            {"attempt", "result_record", "artifact_inventory_record", "audit_record"},
            "performance attempt publication",
        )
        audit = row["audit_record"]
        return cls(
            attempt=row["attempt"],  # type: ignore[arg-type]
            result_record=ReleasePerformanceRecordReferenceV1.from_dict(row["result_record"]),
            artifact_inventory_record=ReleasePerformanceRecordReferenceV1.from_dict(
                row["artifact_inventory_record"]
            ),
            audit_record=(
                None if audit is None else ReleasePerformanceRecordReferenceV1.from_dict(audit)
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleasePerformanceWorkUnitPublicationV1:
    work_unit_id: str
    status: str
    attempts: tuple[ReleasePerformanceAttemptPublicationV1, ...]
    schema_id: str = RELEASE_PERFORMANCE_WORK_UNIT_PUBLICATION_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        _work_unit_parts(self.work_unit_id)
        if self.status not in {"COMPLETE", "FAILED"}:
            raise ValueError("performance work-unit publication status is invalid")
        if (
            type(self.attempts) is not tuple
            or not 1 <= len(self.attempts) <= 2
            or any(type(item) is not ReleasePerformanceAttemptPublicationV1 for item in self.attempts)
            or tuple(item.attempt for item in self.attempts) != tuple(range(1, len(self.attempts) + 1))
        ):
            raise ValueError("performance work-unit attempt publication order differs")
        for item in self.attempts:
            result_path, inventory_path = release_performance_work_unit_paths(
                self.work_unit_id, item.attempt
            )
            if (
                item.result_record.path != result_path
                or item.artifact_inventory_record.path != inventory_path
            ):
                raise ValueError("performance work-unit publication path differs")
        if (
            self.schema_id != RELEASE_PERFORMANCE_WORK_UNIT_PUBLICATION_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("performance work-unit publication schema differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "attempts": [item.as_dict() for item in self.attempts],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status,
            "work_unit_id": self.work_unit_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceWorkUnitPublicationV1":
        row = _exact_object(
            value,
            {"work_unit_id", "status", "attempts", "schema_id", "schema_version"},
            "performance work-unit publication",
        )
        return cls(
            work_unit_id=_text(row["work_unit_id"], "performance work-unit ID", 256),
            status=_text(row["status"], "performance work-unit status", 64),
            attempts=tuple(
                ReleasePerformanceAttemptPublicationV1.from_dict(item)
                for item in _array(row["attempts"], "performance attempt publications")
            ),
            schema_id=_text(row["schema_id"], "work-unit publication schema", 128),
            schema_version=row["schema_version"],  # type: ignore[arg-type]
        )


ReleasePerformanceUnitPublicationV1 = ReleasePerformanceWorkUnitPublicationV1


@dataclass(frozen=True, slots=True)
class ReleasePerformanceAuxiliaryReferenceV1:
    workload_id: str
    result_record: ReleasePerformanceRecordReferenceV1
    evidence_records: tuple[ReleasePerformanceRecordReferenceV1, ...]

    def __post_init__(self) -> None:
        if self.workload_id not in _AUXILIARY_ORDER:
            raise ValueError("performance auxiliary reference workload is invalid")
        if type(self.result_record) is not ReleasePerformanceRecordReferenceV1:
            raise TypeError("performance auxiliary result reference must be typed")
        if (
            self.result_record.kind != "AUXILIARY_RESULT"
            or self.result_record.record_id != self.workload_id
            or self.result_record.path != _auxiliary_path(self.workload_id)
        ):
            raise ValueError("performance auxiliary result reference differs")
        if type(self.evidence_records) is not tuple or any(
            type(item) is not ReleasePerformanceRecordReferenceV1
            for item in self.evidence_records
        ):
            raise TypeError("performance auxiliary evidence references must be typed")
        ids = tuple(item.record_id for item in self.evidence_records)
        if (
            ids != tuple(sorted(ids, key=lambda item: item.encode("utf-8")))
            or len(ids) != len(set(ids))
            or any(item.kind != "AUXILIARY_EVIDENCE" for item in self.evidence_records)
        ):
            raise ValueError("performance auxiliary evidence inventory differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_records": [item.as_dict() for item in self.evidence_records],
            "result_record": self.result_record.as_dict(),
            "workload_id": self.workload_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceAuxiliaryReferenceV1":
        row = _exact_object(
            value,
            {"workload_id", "result_record", "evidence_records"},
            "performance auxiliary reference",
        )
        return cls(
            workload_id=_text(row["workload_id"], "auxiliary workload ID", 128),
            result_record=ReleasePerformanceRecordReferenceV1.from_dict(row["result_record"]),
            evidence_records=tuple(
                ReleasePerformanceRecordReferenceV1.from_dict(item)
                for item in _array(row["evidence_records"], "auxiliary evidence records")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleasePerformanceVerificationInputsV1:
    candidate_commit: str
    source_manifest_sha256: str
    protocol_set_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    threshold_manifest_sha256: str
    runner_source_lock_sha256: str
    row_corpus_sha256: str
    source_tree: RunnerSourceTreeV1
    auxiliary_templates: tuple[ReleaseAuxiliaryPerformanceTemplateV1, ...]
    microscope_asset_manifest_sha256: str

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("performance verification candidate commit is invalid")
        for value, label in (
            (self.source_manifest_sha256, "source manifest"),
            (self.protocol_set_sha256, "protocol set"),
            (self.artifact_index_sha256, "artifact index"),
            (self.build_evidence_sha256, "build evidence"),
            (self.threshold_manifest_sha256, "threshold manifest"),
            (self.runner_source_lock_sha256, "runner source lock"),
            (self.row_corpus_sha256, "row corpus"),
            (self.microscope_asset_manifest_sha256, "microscope asset manifest"),
        ):
            require_sha256(value, f"performance verification {label} digest")
        if type(self.source_tree) is not RunnerSourceTreeV1:
            raise TypeError("performance verification requires the exact runner source tree")
        if self.source_tree.source_manifest_sha256 != self.source_manifest_sha256:
            raise ValueError("performance source tree differs from the expected manifest")
        if hashlib.sha256(canonical_json_bytes(self.source_tree.as_dict())).hexdigest() != (
            self.runner_source_lock_sha256
        ):
            raise ValueError("performance runner-source lock bytes differ from their digest")
        if (
            type(self.auxiliary_templates) is not tuple
            or any(
                type(item) is not ReleaseAuxiliaryPerformanceTemplateV1
                for item in self.auxiliary_templates
            )
            or tuple(item.workload_id for item in self.auxiliary_templates)
            != _AUXILIARY_ORDER
        ):
            raise ValueError("performance auxiliary template inventory differs")


@dataclass(frozen=True, slots=True)
class ReleasePerformanceAggregateV1:
    status: str
    candidate_commit: str
    source_manifest_sha256: str
    protocol_set_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    threshold_manifest_sha256: str
    runner_source_lock_sha256: str
    row_corpus_sha256: str
    work_units_sha256: str
    auxiliary_results_sha256: str
    cas_inventory_sha256: str
    work_unit_count: int
    unique_complete_run_ids: int
    complete_work_unit_count: int
    complete_result_records: int
    complete_artifact_records: int
    complete_audit_records: int
    auxiliary_result_count: int
    retry_count: int
    failed_work_unit_count: int
    total_wall_ns: int
    throughput_microruns_per_second: int
    throughput_status: str
    aggregate_artifact_bytes: int
    logical_referenced_bytes: int
    artifact_bytes_status: str
    cas_object_count: int
    logical_record_count: int
    warning_count: int
    failure_codes: tuple[str, ...]
    gate_id: str = RELEASE_PERFORMANCE_GATE_ID_V1
    schema_id: str = RELEASE_PERFORMANCE_AGGREGATE_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.gate_id != RELEASE_PERFORMANCE_GATE_ID_V1
            or self.status not in _TERMINAL_STATUSES
            or self.schema_id != RELEASE_PERFORMANCE_AGGREGATE_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("performance aggregate identity or status differs")
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("performance aggregate candidate commit is invalid")
        for value, label in (
            (self.source_manifest_sha256, "source manifest"),
            (self.protocol_set_sha256, "protocol set"),
            (self.artifact_index_sha256, "artifact index"),
            (self.build_evidence_sha256, "build evidence"),
            (self.threshold_manifest_sha256, "threshold manifest"),
            (self.runner_source_lock_sha256, "runner source lock"),
            (self.row_corpus_sha256, "row corpus"),
            (self.work_units_sha256, "work-unit publication"),
            (self.auxiliary_results_sha256, "auxiliary publication"),
            (self.cas_inventory_sha256, "CAS inventory"),
        ):
            require_sha256(value, f"performance aggregate {label} digest")
        for value, label in (
            (self.work_unit_count, "work-unit count"),
            (self.unique_complete_run_ids, "unique complete-run count"),
            (self.complete_work_unit_count, "complete work-unit count"),
            (self.complete_result_records, "complete result count"),
            (self.complete_artifact_records, "complete artifact count"),
            (self.complete_audit_records, "complete audit count"),
            (self.auxiliary_result_count, "auxiliary count"),
            (self.retry_count, "retry count"),
            (self.failed_work_unit_count, "failed work-unit count"),
            (self.total_wall_ns, "total wall time"),
            (self.throughput_microruns_per_second, "throughput"),
            (self.aggregate_artifact_bytes, "aggregate artifact bytes"),
            (self.logical_referenced_bytes, "logical referenced bytes"),
            (self.cas_object_count, "CAS object count"),
            (self.logical_record_count, "logical record count"),
            (self.warning_count, "warning count"),
        ):
            _nonnegative(value, f"performance aggregate {label}")
        if self.throughput_status not in {"PASS", "WARNING", "FAIL"}:
            raise ValueError("performance aggregate throughput status is invalid")
        if self.artifact_bytes_status not in {"PASS", "WARNING", "FAIL"}:
            raise ValueError("performance aggregate artifact status is invalid")
        if type(self.failure_codes) is not tuple:
            raise TypeError("performance aggregate failure codes must be a tuple")
        for code in self.failure_codes:
            _text(code, "performance aggregate failure code", 256)
        if self.failure_codes != tuple(
            sorted(set(self.failure_codes), key=lambda item: item.encode("utf-8"))
        ):
            raise ValueError("performance aggregate failure codes are not unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_artifact_bytes": self.aggregate_artifact_bytes,
            "artifact_bytes_status": self.artifact_bytes_status,
            "artifact_index_sha256": self.artifact_index_sha256,
            "auxiliary_result_count": self.auxiliary_result_count,
            "auxiliary_results_sha256": self.auxiliary_results_sha256,
            "build_evidence_sha256": self.build_evidence_sha256,
            "candidate_commit": self.candidate_commit,
            "cas_inventory_sha256": self.cas_inventory_sha256,
            "cas_object_count": self.cas_object_count,
            "complete_artifact_records": self.complete_artifact_records,
            "complete_audit_records": self.complete_audit_records,
            "complete_result_records": self.complete_result_records,
            "complete_work_unit_count": self.complete_work_unit_count,
            "failed_work_unit_count": self.failed_work_unit_count,
            "failure_codes": list(self.failure_codes),
            "gate_id": self.gate_id,
            "logical_record_count": self.logical_record_count,
            "logical_referenced_bytes": self.logical_referenced_bytes,
            "protocol_set_sha256": self.protocol_set_sha256,
            "retry_count": self.retry_count,
            "row_corpus_sha256": self.row_corpus_sha256,
            "runner_source_lock_sha256": self.runner_source_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "status": self.status,
            "threshold_manifest_sha256": self.threshold_manifest_sha256,
            "throughput_microruns_per_second": self.throughput_microruns_per_second,
            "throughput_status": self.throughput_status,
            "total_wall_ns": self.total_wall_ns,
            "unique_complete_run_ids": self.unique_complete_run_ids,
            "warning_count": self.warning_count,
            "work_unit_count": self.work_unit_count,
            "work_units_sha256": self.work_units_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceAggregateV1":
        fields = {
            "aggregate_artifact_bytes", "artifact_bytes_status", "artifact_index_sha256",
            "auxiliary_result_count", "auxiliary_results_sha256", "build_evidence_sha256",
            "candidate_commit", "cas_inventory_sha256", "cas_object_count",
            "complete_artifact_records", "complete_audit_records", "complete_result_records",
            "complete_work_unit_count", "failed_work_unit_count", "failure_codes", "gate_id",
            "logical_record_count", "logical_referenced_bytes", "protocol_set_sha256",
            "retry_count", "row_corpus_sha256", "runner_source_lock_sha256", "schema_id",
            "schema_version", "source_manifest_sha256", "status", "threshold_manifest_sha256",
            "throughput_microruns_per_second", "throughput_status", "total_wall_ns",
            "unique_complete_run_ids", "warning_count", "work_unit_count", "work_units_sha256",
        }
        row = _exact_object(value, fields, "performance aggregate")
        return cls(
            **{
                key: row[key]
                for key in fields
                if key != "failure_codes"
            },  # type: ignore[arg-type]
            failure_codes=tuple(
                _text(item, "performance aggregate failure code", 256)
                for item in _array(row["failure_codes"], "performance aggregate failure codes")
            ),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePerformanceAggregateV1":
        return cls.from_dict(_canonical_payload(raw, "performance aggregate"))


@dataclass(frozen=True, slots=True)
class ReleasePerformanceAttemptRecordV1:
    attempt_id: str
    status: str
    candidate_commit: str
    source_manifest_sha256: str
    protocol_set_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    threshold_manifest_sha256: str
    runner_source_lock_sha256: str
    row_corpus_sha256: str
    target_id: str
    environment: dict[str, object]
    started_at_utc: str
    finished_at_utc: str
    start_monotonic_ns: int
    end_monotonic_ns: int
    worker_count: int
    queue_size: int
    work_units: tuple[ReleasePerformanceWorkUnitPublicationV1, ...]
    auxiliaries: tuple[ReleasePerformanceAuxiliaryReferenceV1, ...]
    aggregate_record: ReleasePerformanceRecordReferenceV1
    warning_codes: tuple[str, ...]
    failure_codes: tuple[str, ...]
    gate_id: str = RELEASE_PERFORMANCE_GATE_ID_V1
    schema_id: str = RELEASE_PERFORMANCE_ATTEMPT_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.gate_id != RELEASE_PERFORMANCE_GATE_ID_V1
            or self.status not in _TERMINAL_STATUSES
            or self.schema_id != RELEASE_PERFORMANCE_ATTEMPT_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
            or _ATTEMPT_ID.fullmatch(self.attempt_id) is None
        ):
            raise ValueError("performance attempt identity or status differs")
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("performance attempt candidate commit is invalid")
        for value, label in (
            (self.source_manifest_sha256, "source manifest"),
            (self.protocol_set_sha256, "protocol set"),
            (self.artifact_index_sha256, "artifact index"),
            (self.build_evidence_sha256, "build evidence"),
            (self.threshold_manifest_sha256, "threshold manifest"),
            (self.runner_source_lock_sha256, "runner source lock"),
            (self.row_corpus_sha256, "row corpus"),
        ):
            require_sha256(value, f"performance attempt {label} digest")
        if self.target_id != RELEASE_PERFORMANCE_TARGET_ID_V1:
            raise ValueError("performance attempt designated target differs")
        _validate_environment(self.environment)
        if (
            _UTC_SECOND.fullmatch(self.started_at_utc) is None
            or _UTC_SECOND.fullmatch(self.finished_at_utc) is None
            or self.finished_at_utc < self.started_at_utc
        ):
            raise ValueError("performance attempt UTC interval is invalid")
        _nonnegative(self.start_monotonic_ns, "performance attempt start time")
        _nonnegative(self.end_monotonic_ns, "performance attempt end time")
        if self.end_monotonic_ns <= self.start_monotonic_ns:
            raise ValueError("performance attempt monotonic interval is not positive")
        if (self.worker_count, self.queue_size) != (
            RELEASE_PERFORMANCE_WORKER_COUNT_V1,
            RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
        ):
            raise ValueError("performance attempt worker resources differ")
        if type(self.work_units) is not tuple or any(
            type(item) is not ReleasePerformanceWorkUnitPublicationV1 for item in self.work_units
        ):
            raise TypeError("performance attempt work units must be typed")
        if type(self.auxiliaries) is not tuple or any(
            type(item) is not ReleasePerformanceAuxiliaryReferenceV1 for item in self.auxiliaries
        ):
            raise TypeError("performance attempt auxiliaries must be typed")
        if tuple(item.workload_id for item in self.auxiliaries) != _AUXILIARY_ORDER:
            raise ValueError("performance attempt auxiliary order differs")
        if (
            type(self.aggregate_record) is not ReleasePerformanceRecordReferenceV1
            or self.aggregate_record.kind != "AGGREGATE"
            or self.aggregate_record.record_id != "performance-aggregate"
            or self.aggregate_record.path != RELEASE_PERFORMANCE_AGGREGATE_PATH_V1
        ):
            raise ValueError("performance attempt aggregate reference differs")
        for values, label in (
            (self.warning_codes, "warning codes"),
            (self.failure_codes, "failure codes"),
        ):
            if type(values) is not tuple:
                raise TypeError(f"performance attempt {label} must be a tuple")
            for value in values:
                _text(value, f"performance attempt {label}", 256)
            if values != tuple(sorted(set(values), key=lambda item: item.encode("utf-8"))):
                raise ValueError(f"performance attempt {label} are not unique and sorted")

    @property
    def total_wall_ns(self) -> int:
        return self.end_monotonic_ns - self.start_monotonic_ns

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_record": self.aggregate_record.as_dict(),
            "artifact_index_sha256": self.artifact_index_sha256,
            "attempt_id": self.attempt_id,
            "auxiliaries": [item.as_dict() for item in self.auxiliaries],
            "build_evidence_sha256": self.build_evidence_sha256,
            "candidate_commit": self.candidate_commit,
            "end_monotonic_ns": self.end_monotonic_ns,
            "environment": self.environment,
            "failure_codes": list(self.failure_codes),
            "finished_at_utc": self.finished_at_utc,
            "gate_id": self.gate_id,
            "protocol_set_sha256": self.protocol_set_sha256,
            "queue_size": self.queue_size,
            "row_corpus_sha256": self.row_corpus_sha256,
            "runner_source_lock_sha256": self.runner_source_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "start_monotonic_ns": self.start_monotonic_ns,
            "started_at_utc": self.started_at_utc,
            "status": self.status,
            "target_id": self.target_id,
            "threshold_manifest_sha256": self.threshold_manifest_sha256,
            "warning_codes": list(self.warning_codes),
            "work_units": [item.as_dict() for item in self.work_units],
            "worker_count": self.worker_count,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceAttemptRecordV1":
        fields = {
            "aggregate_record", "artifact_index_sha256", "attempt_id", "auxiliaries",
            "build_evidence_sha256", "candidate_commit", "end_monotonic_ns", "environment",
            "failure_codes", "finished_at_utc", "gate_id", "protocol_set_sha256",
            "queue_size", "row_corpus_sha256", "runner_source_lock_sha256", "schema_id",
            "schema_version", "source_manifest_sha256", "start_monotonic_ns",
            "started_at_utc", "status", "target_id", "threshold_manifest_sha256",
            "warning_codes", "work_units", "worker_count",
        }
        row = _exact_object(value, fields, "performance attempt")
        environment = row["environment"]
        if type(environment) is not dict:
            raise TypeError("performance attempt environment must be an object")
        return cls(
            attempt_id=_text(row["attempt_id"], "performance attempt ID", 64),
            status=_text(row["status"], "performance attempt status", 64),
            candidate_commit=_text(row["candidate_commit"], "candidate commit", 40),
            source_manifest_sha256=_text(row["source_manifest_sha256"], "source manifest", 64),
            protocol_set_sha256=_text(row["protocol_set_sha256"], "protocol set", 64),
            artifact_index_sha256=_text(row["artifact_index_sha256"], "artifact index", 64),
            build_evidence_sha256=_text(row["build_evidence_sha256"], "build evidence", 64),
            threshold_manifest_sha256=_text(row["threshold_manifest_sha256"], "threshold manifest", 64),
            runner_source_lock_sha256=_text(row["runner_source_lock_sha256"], "runner source lock", 64),
            row_corpus_sha256=_text(row["row_corpus_sha256"], "row corpus", 64),
            target_id=_text(row["target_id"], "performance target", 64),
            environment=dict(environment),
            started_at_utc=_text(row["started_at_utc"], "performance start UTC", 32),
            finished_at_utc=_text(row["finished_at_utc"], "performance finish UTC", 32),
            start_monotonic_ns=row["start_monotonic_ns"],  # type: ignore[arg-type]
            end_monotonic_ns=row["end_monotonic_ns"],  # type: ignore[arg-type]
            worker_count=row["worker_count"],  # type: ignore[arg-type]
            queue_size=row["queue_size"],  # type: ignore[arg-type]
            work_units=tuple(
                ReleasePerformanceWorkUnitPublicationV1.from_dict(item)
                for item in _array(row["work_units"], "performance work units")
            ),
            auxiliaries=tuple(
                ReleasePerformanceAuxiliaryReferenceV1.from_dict(item)
                for item in _array(row["auxiliaries"], "performance auxiliaries")
            ),
            aggregate_record=ReleasePerformanceRecordReferenceV1.from_dict(row["aggregate_record"]),
            warning_codes=tuple(
                _text(item, "performance warning code", 256)
                for item in _array(row["warning_codes"], "performance warning codes")
            ),
            failure_codes=tuple(
                _text(item, "performance failure code", 256)
                for item in _array(row["failure_codes"], "performance failure codes")
            ),
            gate_id=_text(row["gate_id"], "performance gate ID", 64),
            schema_id=_text(row["schema_id"], "performance attempt schema", 128),
            schema_version=row["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePerformanceAttemptRecordV1":
        return cls.from_dict(_canonical_payload(raw, "performance attempt"))


@dataclass(frozen=True, slots=True)
class ReleasePerformanceActivationV1:
    status: str
    candidate_commit: str
    source_manifest_sha256: str
    protocol_set_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    threshold_manifest_sha256: str
    runner_source_lock_sha256: str
    attempt_id: str
    attempt_record: ReleasePerformanceRecordReferenceV1
    aggregate_record: ReleasePerformanceRecordReferenceV1
    work_unit_count: int
    complete_work_unit_count: int
    auxiliary_result_count: int
    activated_at_utc: str
    gate_id: str = RELEASE_PERFORMANCE_GATE_ID_V1
    schema_id: str = RELEASE_PERFORMANCE_ACTIVATION_SCHEMA_ID_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.gate_id != RELEASE_PERFORMANCE_GATE_ID_V1
            or self.status not in _TERMINAL_STATUSES
            or self.schema_id != RELEASE_PERFORMANCE_ACTIVATION_SCHEMA_ID_V1
            or type(self.schema_version) is not int
            or self.schema_version != 1
            or _ATTEMPT_ID.fullmatch(self.attempt_id) is None
            or _UTC_SECOND.fullmatch(self.activated_at_utc) is None
        ):
            raise ValueError("performance activation identity or status differs")
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("performance activation candidate commit is invalid")
        for value, label in (
            (self.source_manifest_sha256, "source manifest"),
            (self.protocol_set_sha256, "protocol set"),
            (self.artifact_index_sha256, "artifact index"),
            (self.build_evidence_sha256, "build evidence"),
            (self.threshold_manifest_sha256, "threshold manifest"),
            (self.runner_source_lock_sha256, "runner source lock"),
        ):
            require_sha256(value, f"performance activation {label} digest")
        for reference, kind, record_id, path in (
            (self.attempt_record, "ATTEMPT", "performance-attempt", RELEASE_PERFORMANCE_ATTEMPT_PATH_V1),
            (self.aggregate_record, "AGGREGATE", "performance-aggregate", RELEASE_PERFORMANCE_AGGREGATE_PATH_V1),
        ):
            if (
                type(reference) is not ReleasePerformanceRecordReferenceV1
                or reference.kind != kind
                or reference.record_id != record_id
                or reference.path != path
            ):
                raise ValueError("performance activation record reference differs")
        for value, label in (
            (self.work_unit_count, "work-unit count"),
            (self.complete_work_unit_count, "complete work-unit count"),
            (self.auxiliary_result_count, "auxiliary count"),
        ):
            _nonnegative(value, f"performance activation {label}")

    def as_dict(self) -> dict[str, object]:
        return {
            "activated_at_utc": self.activated_at_utc,
            "aggregate_record": self.aggregate_record.as_dict(),
            "artifact_index_sha256": self.artifact_index_sha256,
            "attempt_id": self.attempt_id,
            "attempt_record": self.attempt_record.as_dict(),
            "auxiliary_result_count": self.auxiliary_result_count,
            "build_evidence_sha256": self.build_evidence_sha256,
            "candidate_commit": self.candidate_commit,
            "complete_work_unit_count": self.complete_work_unit_count,
            "gate_id": self.gate_id,
            "protocol_set_sha256": self.protocol_set_sha256,
            "runner_source_lock_sha256": self.runner_source_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "status": self.status,
            "threshold_manifest_sha256": self.threshold_manifest_sha256,
            "work_unit_count": self.work_unit_count,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePerformanceActivationV1":
        fields = {
            "activated_at_utc", "aggregate_record", "artifact_index_sha256", "attempt_id",
            "attempt_record", "auxiliary_result_count", "build_evidence_sha256",
            "candidate_commit", "complete_work_unit_count", "gate_id",
            "protocol_set_sha256", "runner_source_lock_sha256", "schema_id",
            "schema_version", "source_manifest_sha256", "status",
            "threshold_manifest_sha256", "work_unit_count",
        }
        row = _exact_object(value, fields, "performance activation")
        return cls(
            status=_text(row["status"], "performance activation status", 64),
            candidate_commit=_text(row["candidate_commit"], "candidate commit", 40),
            source_manifest_sha256=_text(row["source_manifest_sha256"], "source manifest", 64),
            protocol_set_sha256=_text(row["protocol_set_sha256"], "protocol set", 64),
            artifact_index_sha256=_text(row["artifact_index_sha256"], "artifact index", 64),
            build_evidence_sha256=_text(row["build_evidence_sha256"], "build evidence", 64),
            threshold_manifest_sha256=_text(row["threshold_manifest_sha256"], "threshold manifest", 64),
            runner_source_lock_sha256=_text(row["runner_source_lock_sha256"], "runner source lock", 64),
            attempt_id=_text(row["attempt_id"], "performance attempt ID", 64),
            attempt_record=ReleasePerformanceRecordReferenceV1.from_dict(row["attempt_record"]),
            aggregate_record=ReleasePerformanceRecordReferenceV1.from_dict(row["aggregate_record"]),
            work_unit_count=row["work_unit_count"],  # type: ignore[arg-type]
            complete_work_unit_count=row["complete_work_unit_count"],  # type: ignore[arg-type]
            auxiliary_result_count=row["auxiliary_result_count"],  # type: ignore[arg-type]
            activated_at_utc=_text(row["activated_at_utc"], "activation UTC", 32),
            gate_id=_text(row["gate_id"], "performance gate ID", 64),
            schema_id=_text(row["schema_id"], "performance activation schema", 128),
            schema_version=row["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePerformanceActivationV1":
        return cls.from_dict(_canonical_payload(raw, "performance activation"))


ReleasePerformanceActivationRecordV1 = ReleasePerformanceActivationV1


@dataclass(frozen=True, slots=True)
class ReleasePerformanceVerificationV1:
    status: str
    candidate_commit: str
    source_manifest_sha256: str
    protocol_set_sha256: str
    artifact_index_sha256: str
    attempt_id: str
    activation_sha256: str
    attempt_sha256: str
    aggregate_sha256: str
    work_unit_count: int
    complete_work_unit_count: int
    auxiliary_result_count: int
    retry_count: int
    cas_object_count: int
    aggregate_artifact_bytes: int

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_STATUSES:
            raise ValueError("performance verification status is invalid")
        if _COMMIT.fullmatch(self.candidate_commit) is None or _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            raise ValueError("performance verification identity is invalid")
        for value in (
            self.source_manifest_sha256,
            self.protocol_set_sha256,
            self.artifact_index_sha256,
            self.activation_sha256,
            self.attempt_sha256,
            self.aggregate_sha256,
        ):
            require_sha256(value, "performance verification digest")
        for value in (
            self.work_unit_count,
            self.complete_work_unit_count,
            self.auxiliary_result_count,
            self.retry_count,
            self.cas_object_count,
            self.aggregate_artifact_bytes,
        ):
            _nonnegative(value, "performance verification count")


def _validate_environment(value: object) -> None:
    fields = {
        "available_disk_bytes",
        "available_memory_bytes",
        "logical_cpu_count",
        "machine",
        "python_implementation",
        "python_version",
        "system",
    }
    row = _exact_object(value, fields, "performance environment")
    if (
        row["system"] != "Darwin"
        or row["machine"] != "arm64"
        or row["python_implementation"] != "CPython"
        or type(row["python_version"]) is not str
        or re.fullmatch(r"3\.14\.[0-9]+", row["python_version"]) is None
    ):
        raise ValueError("performance environment is not the designated runtime")
    if (
        _nonnegative(row["logical_cpu_count"], "performance logical CPU count")
        < RELEASE_PERFORMANCE_WORKER_COUNT_V1
        or _nonnegative(row["available_memory_bytes"], "performance available memory")
        < 8 * 1024**3
        or _nonnegative(row["available_disk_bytes"], "performance available storage")
        < 20 * 1024**3
    ):
        raise ValueError("performance environment lacks preregistered resources")


class _PerformanceRecordSource(Protocol):
    def read(self, path: str) -> bytes: ...

    def finish(self) -> None: ...

    def close(self) -> None: ...


class _CandidateRecordSource:
    def __init__(self, records: Mapping[str, bytes]) -> None:
        if type(records) is not dict:
            raise TypeError("candidate performance records must be an exact dictionary")
        normalized: dict[str, bytes] = {}
        for path, raw in records.items():
            selected = _publication_path(path, "candidate performance-record path")
            if selected != path or type(raw) is not bytes or not 0 < len(raw) <= _MAX_RECORD_BYTES:
                raise ValueError("candidate performance record is not exact bounded bytes")
            normalized[selected] = raw
        if len(normalized) > _MAX_PUBLICATION_FILES:
            raise ValueError("candidate performance publication contains too many records")
        self._records = normalized
        self._seen: set[str] = set()

    def read(self, path: str) -> bytes:
        selected = _publication_path(path, "candidate performance-record path")
        try:
            raw = self._records[selected]
        except KeyError as error:
            raise ValueError(f"performance publication lacks {selected}") from error
        self._seen.add(selected)
        return raw

    def finish(self) -> None:
        if set(self._records) != self._seen:
            extras = sorted(set(self._records) - self._seen, key=lambda item: item.encode("utf-8"))
            raise ValueError(f"performance publication contains unreferenced records: {extras[:3]}")

    def close(self) -> None:
        return


class _DiskRecordSource:
    def __init__(self, artifact_root: Path) -> None:
        if not isinstance(artifact_root, Path):
            raise TypeError("performance artifact root must be a Path")
        self._root = artifact_root.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        self._root_fd = os.open(self._root, flags)
        metadata = os.fstat(self._root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            os.close(self._root_fd)
            raise ValueError("performance artifact root ownership or mode is unsafe")
        self._owner = metadata.st_uid
        self._device = metadata.st_dev
        self._root_identity = self._directory_identity(metadata)
        self._seen: set[str] = set()
        self._file_identities: dict[str, tuple[int, ...]] = {}
        self._directory_identities: dict[str, tuple[int, ...]] = {}

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, ...]:
        # macOS File Provider may attach provenance xattrs after publication.
        # That changes ctime only; complete bytes, inventory, and every other
        # content-relevant identity field are verified independently here.
        return (
            metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_uid, metadata.st_gid, metadata.st_size,
            metadata.st_mtime_ns,
        )

    @staticmethod
    def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
        """Return replacement-relevant identity for a verified directory.

        Directory size and timestamps are filesystem metadata rather than a
        portable inventory identity.  macOS File Provider can update them while
        attaching provenance metadata even though the directory inode, ownership,
        permissions, links, descendants, and every descendant byte remain exact.
        The verifier separately walks the complete bounded inventory and reopens
        every referenced immutable file, so retaining the stable replacement and
        access-control fields is both stricter and portable.
        """

        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
        )

    def read(self, path: str) -> bytes:
        selected = _publication_path(path, "performance record path")
        parts = PurePosixPath(selected).parts
        directory = os.dup(self._root_fd)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            walked: list[str] = []
            for part in parts[:-1]:
                walked.append(part)
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
                metadata = os.fstat(directory)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self._owner
                    or metadata.st_dev != self._device
                    or metadata.st_mode & 0o022
                ):
                    raise ValueError("performance publication directory is unsafe")
                identity = self._directory_identity(metadata)
                selected_directory = PurePosixPath(*walked).as_posix()
                prior = self._directory_identities.setdefault(
                    selected_directory, identity
                )
                if prior != identity:
                    raise ValueError(
                        "performance publication directory changed between reads"
                    )
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != self._owner
                    or before.st_dev != self._device
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o444
                    or not 0 < before.st_size <= _MAX_RECORD_BYTES
                ):
                    raise ValueError("immutable performance record ownership, mode, or size differs")
                chunks: list[bytes] = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("immutable performance record was truncated")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise ValueError("immutable performance record grew during read")
                after = os.fstat(descriptor)
                if self._identity(before) != self._identity(after):
                    raise ValueError("immutable performance record changed during read")
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
        identity = self._identity(after)
        prior = self._file_identities.setdefault(selected, identity)
        if prior != identity:
            raise ValueError("immutable performance record changed between reads")
        self._seen.add(selected)
        return b"".join(chunks)

    def _open_directory(self, parts: tuple[str, ...]) -> int:
        directory = os.dup(self._root_fd)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            walked: list[str] = []
            for part in parts:
                walked.append(part)
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
                metadata = os.fstat(directory)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self._owner
                    or metadata.st_dev != self._device
                    or metadata.st_mode & 0o022
                ):
                    raise ValueError("performance publication directory is unsafe")
                identity = self._directory_identity(metadata)
                selected_directory = PurePosixPath(*walked).as_posix()
                prior = self._directory_identities.setdefault(
                    selected_directory, identity
                )
                if prior != identity:
                    raise ValueError(
                        "performance publication directory changed during verification"
                    )
            return directory
        except BaseException:
            os.close(directory)
            raise

    def _final_file_identity(self, selected: str) -> tuple[int, ...]:
        parts = PurePosixPath(selected).parts
        directory = self._open_directory(parts[:-1])
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=directory)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != self._owner
                    or metadata.st_dev != self._device
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o444
                    or not 0 < metadata.st_size <= _MAX_RECORD_BYTES
                ):
                    raise ValueError("immutable performance record ownership, mode, or size differs")
                return self._identity(metadata)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)

    def _scan_publication(
        self,
        directory: int,
        relative: PurePosixPath,
        observed_files: set[str],
        observed_directories: dict[str, tuple[int, ...]],
    ) -> None:
        metadata = os.fstat(directory)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._owner
            or metadata.st_dev != self._device
            or metadata.st_mode & 0o022
        ):
            raise ValueError("performance publication directory is unsafe")
        selected_directory = relative.as_posix()
        prior = self._directory_identities.setdefault(
            selected_directory, self._directory_identity(metadata)
        )
        if prior != self._directory_identity(metadata):
            raise ValueError("performance publication directory changed before inventory scan")
        observed_directories[selected_directory] = self._directory_identity(metadata)
        if len(observed_directories) > _MAX_PUBLICATION_FILES:
            raise ValueError("performance publication contains too many directories")

        entries = sorted(
            os.scandir(directory),
            key=lambda item: item.name.encode("utf-8"),
        )
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for entry in entries:
            child_relative = relative / entry.name
            child_metadata = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(child_metadata.st_mode):
                child = os.open(entry.name, directory_flags, dir_fd=directory)
                try:
                    if self._directory_identity(
                        os.fstat(child)
                    ) != self._directory_identity(child_metadata):
                        raise ValueError("performance publication directory changed during scan")
                    self._scan_publication(
                        child,
                        child_relative,
                        observed_files,
                        observed_directories,
                    )
                finally:
                    os.close(child)
            elif stat.S_ISREG(child_metadata.st_mode):
                if (
                    child_metadata.st_uid != self._owner
                    or child_metadata.st_dev != self._device
                    or child_metadata.st_nlink != 1
                    or stat.S_IMODE(child_metadata.st_mode) != 0o444
                    or not 0 < child_metadata.st_size <= _MAX_RECORD_BYTES
                ):
                    raise ValueError(
                        "immutable performance record ownership, mode, or size differs"
                    )
                observed_files.add(child_relative.as_posix())
                if len(observed_files) > _MAX_PUBLICATION_FILES:
                    raise ValueError("performance publication contains too many records")
            else:
                raise ValueError("performance publication contains a non-regular descendant")

    def finish(self) -> None:
        if self._directory_identity(os.fstat(self._root_fd)) != self._root_identity:
            raise ValueError("performance artifact root changed during verification")
        publication_parts = PurePosixPath(RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1).parts
        publication = self._open_directory(publication_parts)
        observed_files: set[str] = set()
        observed_directories: dict[str, tuple[int, ...]] = {}
        try:
            self._scan_publication(
                publication,
                PurePosixPath(RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1),
                observed_files,
                observed_directories,
            )
        finally:
            os.close(publication)
        if observed_files != self._seen:
            extras = sorted(observed_files - self._seen, key=lambda item: item.encode("utf-8"))
            missing = sorted(self._seen - observed_files, key=lambda item: item.encode("utf-8"))
            raise ValueError(f"performance publication inventory differs: extras={extras[:3]} missing={missing[:3]}")
        expected_directories = {RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1}
        for selected in self._seen:
            parent = PurePosixPath(selected).parent
            while parent.as_posix() != "gate-evidence":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if set(observed_directories) != expected_directories:
            extras = sorted(
                set(observed_directories) - expected_directories,
                key=lambda item: item.encode("utf-8"),
            )
            missing = sorted(
                expected_directories - set(observed_directories),
                key=lambda item: item.encode("utf-8"),
            )
            raise ValueError(
                f"performance publication directory inventory differs: "
                f"extras={extras[:3]} missing={missing[:3]}"
            )
        for selected in sorted(self._seen, key=lambda item: item.encode("utf-8")):
            if self._final_file_identity(selected) != self._file_identities[selected]:
                raise ValueError("immutable performance record changed after verification")
        for selected, identity in observed_directories.items():
            descriptor = self._open_directory(PurePosixPath(selected).parts)
            try:
                if self._directory_identity(os.fstat(descriptor)) != identity:
                    raise ValueError("performance publication directory changed during verification")
            finally:
                os.close(descriptor)
        if self._directory_identity(os.fstat(self._root_fd)) != self._root_identity:
            raise ValueError("performance artifact root changed during verification")

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1


def _read_reference(
    source: _PerformanceRecordSource,
    reference: ReleasePerformanceRecordReferenceV1,
) -> bytes:
    raw = source.read(reference.path)
    if len(raw) != reference.size or hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError(f"performance record reference differs for {reference.record_id}")
    return raw


def _expected_work_unit_ids() -> tuple[str, ...]:
    return tuple(
        f"release-perf/{cell.value}/{root_seed}"
        for cell in RELEASE_PERFORMANCE_CELL_ORDER_V1
        for root_seed in RELEASE_PERFORMANCE_ROOTS_V1
    )


def _identity_tuple(value: object) -> tuple[str, ...]:
    return tuple(
        getattr(value, field)
        for field in (
            "candidate_commit", "source_manifest_sha256", "protocol_set_sha256",
            "artifact_index_sha256", "build_evidence_sha256",
            "threshold_manifest_sha256", "runner_source_lock_sha256",
        )
    )


def _expected_identity(inputs: ReleasePerformanceVerificationInputsV1) -> tuple[str, ...]:
    return (
        inputs.candidate_commit,
        inputs.source_manifest_sha256,
        inputs.protocol_set_sha256,
        inputs.artifact_index_sha256,
        inputs.build_evidence_sha256,
        inputs.threshold_manifest_sha256,
        inputs.runner_source_lock_sha256,
    )


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    value = load_canonical_json_bytes(raw, label)
    if type(value) is not dict:
        raise TypeError(f"{label} must contain an object")
    _validate_projected_semantic(value, label)
    return value


def _json_pointer(value: object, label: str) -> str:
    pointer = _text(value, label, 1024)
    if pointer == "":
        return pointer
    if not pointer.startswith("/"):
        raise ValueError(f"{label} is not an RFC 6901 pointer")
    for token in pointer.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    raise ValueError(f"{label} contains an invalid RFC 6901 escape")
                index += 2
            else:
                index += 1
        decoded = token.replace("~1", "/").replace("~0", "~")
        if decoded.isdecimal() and len(decoded) > 1 and decoded.startswith("0"):
            raise ValueError(f"{label} contains a noncanonical array index")
    return pointer


def _validate_projected_semantic(value: object, label: str) -> None:
    """Validate an already-projected value without applying the projector twice."""

    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            _text(value, label)
        return
    if type(value) is float:
        raise ValueError(f"{label} contains a raw JSON float")
    if type(value) is list:
        for item in value:
            _validate_projected_semantic(item, label)
        return
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{label} contains an unsupported semantic value")
    for key in value:
        _text(key, f"{label} key")
    if "__kirby2_legacy_digest_ref_v1__" in value:
        if set(value) != {"__kirby2_legacy_digest_ref_v1__"}:
            raise ValueError(f"{label} legacy-reference fields differ")
        _json_pointer(value["__kirby2_legacy_digest_ref_v1__"], f"{label} legacy pointer")
        return
    if "__kirby2_release_scalar_v1__" in value:
        scalar = value["__kirby2_release_scalar_v1__"]
        if scalar == "EXACT_RATIONAL":
            if set(value) != {"__kirby2_release_scalar_v1__", "numerator", "denominator"}:
                raise ValueError(f"{label} exact-rational fields differ")
            numerator = value["numerator"]
            denominator = value["denominator"]
            if (
                type(numerator) is not int
                or type(denominator) is not int
                or denominator <= 0
                or math.gcd(numerator, denominator) != 1
            ):
                raise ValueError(f"{label} exact rational is not reduced")
            return
        if scalar == "EXACT_DECIMAL":
            if set(value) != {"__kirby2_release_scalar_v1__", "coefficient", "exponent"}:
                raise ValueError(f"{label} exact-decimal fields differ")
            coefficient = value["coefficient"]
            exponent = value["exponent"]
            if (
                type(coefficient) is not int
                or type(exponent) is not int
                or coefficient != 0 and coefficient % 10 == 0
            ):
                raise ValueError(f"{label} exact decimal is not normalized")
            return
        raise ValueError(f"{label} contains an unrecognized projected scalar")
    for item in value.values():
        _validate_projected_semantic(item, label)


def _legacy_pointers(value: object) -> tuple[str, ...]:
    pointers: list[str] = []

    def visit(selected: object) -> None:
        if type(selected) is dict:
            if "__kirby2_legacy_digest_ref_v1__" in selected:
                if set(selected) != {"__kirby2_legacy_digest_ref_v1__"}:
                    raise ValueError("projected legacy reference contains extra fields")
                pointers.append(_json_pointer(selected["__kirby2_legacy_digest_ref_v1__"], "legacy pointer"))
                return
            for item in selected.values():
                visit(item)
        elif type(selected) is list:
            for item in selected:
                visit(item)
        elif type(selected) is str and _SHA256.fullmatch(selected) is not None:
            raise ValueError("projected semantic payload retains a legacy digest")

    visit(value)
    ordered = tuple(sorted(pointers, key=lambda item: item.encode("utf-8")))
    if len(ordered) != len(set(ordered)):
        raise ValueError("projected semantic payload repeats a legacy pointer")
    return ordered


def _named_rows(value: object, key: str, label: str) -> dict[str, dict[str, object]]:
    rows = _array(value, label)
    selected: dict[str, dict[str, object]] = {}
    for raw in rows:
        if type(raw) is not dict:
            raise TypeError(f"{label} must contain objects")
        name = _text(raw.get(key), f"{label} name", 128)
        if name in selected:
            raise ValueError(f"{label} contains a duplicate mapping")
        selected[name] = raw
    return selected


def _legacy_reference_digest(
    value: object,
    bindings: Mapping[str, str],
    label: str,
) -> str:
    row = _exact_object(value, {"__kirby2_legacy_digest_ref_v1__"}, label)
    pointer = _json_pointer(row["__kirby2_legacy_digest_ref_v1__"], f"{label} pointer")
    try:
        return bindings[pointer]
    except KeyError as error:
        raise ValueError(f"{label} is absent from the legacy bindings") from error


def _validate_projection_records(
    result: ReleasePerformanceCellResultV1,
    payload: object,
    bindings: Mapping[str, str],
) -> None:
    if type(payload) is not dict:
        raise TypeError("performance semantic-result payload must be an object")
    capability_names = tuple(item.capability for item in result.capability_records)
    check_names = tuple(item.check_id for item in result.check_records)
    declared = payload.get("declared_outputs")
    if type(declared) is dict:
        capabilities = _named_rows(declared.get("exercises"), "capability", "projected exercises")
        checks = _named_rows(declared.get("check_results"), "name", "projected checks")
        if tuple(capabilities) != capability_names or tuple(checks) != check_names:
            raise ValueError("projected exercise/check order differs from the result records")
        for wrapper in result.capability_records:
            row = capabilities[wrapper.capability]
            evidence = row.get("evidence")
            if type(evidence) is not dict or (
                row.get("configured_value") != wrapper.configured_value
                or row.get("status") != wrapper.status
                or hashlib.sha256(canonical_json_bytes(evidence)).hexdigest()
                != wrapper.evidence_sha256
            ):
                raise ValueError("performance capability wrapper differs from projected evidence")
        for wrapper in result.check_records:
            row = checks[wrapper.check_id]
            evidence = row.get("evidence")
            if type(evidence) is not dict or (
                row.get("status") != wrapper.status
                or hashlib.sha256(canonical_json_bytes(evidence)).hexdigest()
                != wrapper.evidence_sha256
            ):
                raise ValueError("performance check wrapper differs from projected evidence")
        return

    capabilities = _named_rows(payload.get("capability_records"), "capability", "queue capabilities")
    checks = _named_rows(payload.get("check_records"), "check_id", "queue checks")
    if tuple(capabilities) != capability_names or tuple(checks) != check_names:
        raise ValueError("queue projection order differs from the result records")
    for wrapper in result.capability_records:
        row = capabilities[wrapper.capability]
        if (
            row.get("configured_value") != wrapper.configured_value
            or row.get("status") != wrapper.status
            or _legacy_reference_digest(
                row.get("evidence_sha256"), bindings, "queue capability evidence"
            )
            != wrapper.evidence_sha256
        ):
            raise ValueError("queue capability wrapper differs from projected evidence")
    for wrapper in result.check_records:
        row = checks[wrapper.check_id]
        if (
            row.get("status") != wrapper.status
            or _legacy_reference_digest(
                row.get("evidence_sha256"), bindings, "queue check evidence"
            )
            != wrapper.evidence_sha256
        ):
            raise ValueError("queue check wrapper differs from projected evidence")


def _validate_semantic_members(
    result: ReleasePerformanceCellResultV1,
    bound_row: dict[str, object],
    members: Mapping[str, bytes],
    compatibility: bytes | None,
) -> None:
    if "run_manifest.json" in members:
        manifest = _canonical_object(members["run_manifest.json"], "performance run manifest")
        expected = {
            "artifact_form": bound_row["artifact_form"],
            "cell": bound_row["cell"],
            "expected_capabilities": bound_row["expected_capabilities"],
            "generated_configuration_sha256": bound_row["generated_configuration_sha256"],
            "native_fixture_sha256": bound_row["native_fixture_sha256"],
            "required_checks": bound_row["required_checks"],
            "root_seed": bound_row["root_seed"],
            "runner_id": bound_row["runner_id"],
            "runner_source_sha256": bound_row["runner_source_sha256"],
            "schema_version": 1,
            "work_unit_id": bound_row["work_unit_id"],
        }
        if manifest != expected:
            raise ValueError("performance run manifest differs from its bound row")
    projected_pointers: dict[str, tuple[str, ...]] = {}
    projected_payloads: dict[str, object] = {}
    for name, schema in (
        ("native_recording.json", bound_row["artifact_form"]),
        ("semantic_result.json", "GENERATED_CASE_RESULT_AS_DICT_V2"),
    ):
        if name not in members:
            continue
        envelope = _canonical_object(members[name], f"performance {name}")
        _exact_object(
            envelope,
            {"schema_version", "projection_policy", "legacy_digest_policy", "native_schema_id", "payload"},
            f"performance {name}",
        )
        if (
            envelope["schema_version"] != 1
            or envelope["projection_policy"] != "RELEASE_FLOAT_FREE_SEMANTIC_V1"
            or envelope["legacy_digest_policy"] != "RELEASE_LEGACY_DIGEST_EXTRACTION_V1"
            or envelope["native_schema_id"] != schema
        ):
            raise ValueError(f"performance {name} envelope differs")
        projected_pointers[name] = _legacy_pointers(envelope["payload"])
        projected_payloads[name] = envelope["payload"]
    if "capabilities.json" in members:
        expected = canonical_json_bytes([item.as_dict() for item in result.capability_records])
        if members["capabilities.json"] != expected:
            raise ValueError("performance capability projection differs")
    if "checks.json" in members:
        expected = canonical_json_bytes([item.as_dict() for item in result.check_records])
        if members["checks.json"] != expected:
            raise ValueError("performance check projection differs")
    if "audit_result.json" in members:
        audit = _canonical_object(members["audit_result.json"], "performance audit result")
        _exact_object(
            audit,
            {"schema_version", "work_unit_id", "status", "capability_projection_sha256",
             "check_projection_sha256", "native_recording_sha256", "semantic_result_sha256", "failures"},
            "performance audit result",
        )
        failures = _array(audit["failures"], "performance audit failures")
        if failures != sorted(set(failures), key=lambda item: str(item).encode("utf-8")) or any(
            type(item) is not str for item in failures
        ):
            raise ValueError("performance audit failures are not unique and sorted")
        semantic_complete = (
            tuple(members) == _SEMANTIC_MEMBER_NAMES
            and result.artifact_set_sha256 is not None
        )
        expected_status = "PASS" if semantic_complete else "FAIL"
        if (expected_status == "PASS" and failures) or (expected_status == "FAIL" and not failures):
            raise ValueError("performance audit failure inventory differs from its status")
        if (
            audit["schema_version"] != 1
            or audit["work_unit_id"] != result.work_unit_id
            or audit["status"] != expected_status
            or audit["capability_projection_sha256"]
            != hashlib.sha256(canonical_json_bytes([item.as_dict() for item in result.capability_records])).hexdigest()
            or audit["check_projection_sha256"]
            != hashlib.sha256(canonical_json_bytes([item.as_dict() for item in result.check_records])).hexdigest()
            or audit["native_recording_sha256"] != result.native_recording_sha256
            or audit["semantic_result_sha256"] != result.semantic_result_sha256
        ):
            raise ValueError("performance audit result does not reconcile")
    if compatibility is not None:
        sidecar = _canonical_object(compatibility, "performance legacy bindings")
        _exact_object(
            sidecar,
            {"schema_version", "work_unit_id", "native_recording_bindings", "semantic_result_bindings"},
            "performance legacy bindings",
        )
        if sidecar["schema_version"] != 1 or sidecar["work_unit_id"] != result.work_unit_id:
            raise ValueError("performance legacy-binding identity differs")
        legacy_values: dict[str, dict[str, str]] = {}
        for key, member_name in (
            ("native_recording_bindings", "native_recording.json"),
            ("semantic_result_bindings", "semantic_result.json"),
        ):
            bindings = _array(sidecar[key], f"performance {key}")
            pointers: list[str] = []
            values: dict[str, str] = {}
            for raw in bindings:
                row = _exact_object(raw, {"json_pointer", "legacy_sha256"}, "legacy digest binding")
                pointer = _json_pointer(row["json_pointer"], "legacy JSON pointer")
                require_sha256(row["legacy_sha256"], "legacy digest")
                pointers.append(pointer)
                values[pointer] = row["legacy_sha256"]  # type: ignore[assignment]
            if tuple(pointers) != tuple(sorted(set(pointers), key=lambda item: item.encode("utf-8"))):
                raise ValueError("legacy digest bindings are not unique and pointer-sorted")
            if tuple(pointers) != projected_pointers.get(member_name, ()):
                raise ValueError("legacy bindings differ from projected semantic references")
            legacy_values[member_name] = values
        if "semantic_result.json" in projected_payloads:
            _validate_projection_records(
                result,
                projected_payloads["semantic_result.json"],
                legacy_values["semantic_result.json"],
            )


def _resource_exceeded(result: ReleasePerformanceCellResultV1) -> bool:
    operational = result.operational
    return (
        operational.end_monotonic_ns - operational.start_monotonic_ns
        > RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1
        or operational.peak_rss_bytes > RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1
        or operational.max_temporary_bytes > RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1
    )


def _validate_worker_intervals(intervals: tuple[tuple[int, int], ...]) -> None:
    events = [
        event
        for started_ns, ended_ns in intervals
        for event in ((started_ns, 1), (ended_ns, -1))
    ]
    # A worker ending at one timestamp is available to a task beginning at that
    # same timestamp, so release events sort before acquisition events.
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    for _timestamp, delta in events:
        active += delta
        if active < 0:
            raise ValueError("performance worker interval ordering is invalid")
        if active > RELEASE_PERFORMANCE_WORKER_COUNT_V1:
            raise ValueError("performance publication exceeds its four-worker limit")
    if active != 0:
        raise ValueError("performance worker interval inventory is incomplete")


def _status_codes(
    complete: int,
    failed_rows: tuple[tuple[str, str], ...],
    auxiliaries: tuple[ReleaseAuxiliaryPerformanceResultV1, ...],
    total_wall_ns: int,
    throughput_status: str,
    artifact_status: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    failures = {f"WORK_UNIT_FAILED:{work_unit_id}:{code}" for work_unit_id, code in failed_rows}
    warnings: set[str] = set()
    if complete != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1:
        failures.add("INCOMPLETE_WORK_UNITS")
    if total_wall_ns > RELEASE_TOTAL_WALL_LIMIT_NS_V1:
        failures.add("TOTAL_WALL_LIMIT")
    if throughput_status == "FAIL":
        failures.add("THROUGHPUT_THRESHOLD_MISS")
    elif throughput_status == "WARNING":
        warnings.add("THROUGHPUT_WARNING")
    if artifact_status == "FAIL":
        failures.add("ARTIFACT_BYTES_THRESHOLD_MISS")
    elif artifact_status == "WARNING":
        warnings.add("ARTIFACT_BYTES_WARNING")
    for result in auxiliaries:
        if result.status == "FAIL":
            codes = result.hard_failure_codes or ("THRESHOLD_MISS",)
            failures.update(f"AUXILIARY_FAILED:{result.workload_id}:{code}" for code in codes)
        elif result.status == "WARNING":
            warnings.add(f"AUXILIARY_WARNING:{result.workload_id}")
    ordered_failures = tuple(sorted(failures, key=lambda item: item.encode("utf-8")))
    ordered_warnings = tuple(sorted(warnings, key=lambda item: item.encode("utf-8")))
    status = "FAIL" if ordered_failures else ("PASS_WITH_WARNINGS" if ordered_warnings else "PASS")
    return ordered_failures, ordered_warnings, status


_AuxiliarySourceIdentity = tuple[str, int, str, str]


def _validate_auxiliary_evidence_inventory(
    workload_id: str,
    evidence_payloads: Mapping[str, bytes],
) -> None:
    common = {"execution-envelope.json", "series.json"}
    if "failure.json" in evidence_payloads:
        expected = common | {"failure.json"}
    else:
        expected = common | {
            "RELEASE_INTERACTIVE_ACK_V1": {
                "interactive-ack/acknowledgements.json",
                "interactive-ack/receipt.json",
            },
            "RELEASE_TERMINAL_UPDATE_V1": {
                "terminal-update/receipt.json",
                "terminal-update/update-inventory.json",
            },
            "RELEASE_FULL_DAY_GENERATION_V1": {
                "full-day-generation/attempts.json",
                "full-day-generation/sources.json",
            },
            "RELEASE_FULL_DAY_REPLAY_V1": {
                "full-day-replay/attempts.json",
            },
            "RELEASE_MICROSCOPE_LOAD_V1": {
                "microscope-load/attempts.json",
                "microscope-load/installed-assets/report.css",
                "microscope-load/installed-assets/report.html",
                "microscope-load/installed-assets/report.js",
                "microscope-load/report/assets/report.css",
                "microscope-load/report/assets/report.js",
                "microscope-load/report/index.html",
                "microscope-load/report/manifest.json",
            },
        }[workload_id]
    if set(evidence_payloads) != expected:
        extras = sorted(
            set(evidence_payloads) - expected,
            key=lambda item: item.encode("utf-8"),
        )
        missing = sorted(
            expected - set(evidence_payloads),
            key=lambda item: item.encode("utf-8"),
        )
        raise ValueError(
            f"auxiliary evidence inventory differs: extras={extras[:3]} "
            f"missing={missing[:3]}"
        )


def _generation_source_identities(
    evidence_payloads: Mapping[str, bytes],
) -> tuple[_AuxiliarySourceIdentity, ...]:
    from .performance_auxiliary import ReleaseAuxiliarySourceRunV1

    try:
        raw = evidence_payloads["full-day-generation/sources.json"]
    except KeyError as error:
        raise ValueError("generation publication lacks its source inventory") from error
    inventory = _exact_object(
        _canonical_payload(raw, "generation source inventory"),
        {"producer_workload_id", "sources", "status"},
        "generation source inventory",
    )
    if (
        inventory["producer_workload_id"] != "RELEASE_FULL_DAY_GENERATION_V1"
        or inventory["status"] != "PASS"
    ):
        raise ValueError("generation source inventory identity differs")
    rows = _array(inventory["sources"], "generation source inventory rows")
    if len(rows) != 4:
        raise ValueError("generation source inventory must contain exactly four rows")
    identities: list[_AuxiliarySourceIdentity] = []
    for ordinal, raw_row in enumerate(rows):
        row = _exact_object(
            raw_row,
            {
                "artifact_id", "manifest_sha256", "ordinal", "run_id",
                "store_relative_root",
            },
            "generation source inventory row",
        )
        expected_artifact_id = f"release-full-day-generation-{ordinal:04d}"
        expected_store_root = f"workspace/generation/ordinal-{ordinal:04d}"
        if (
            row["artifact_id"] != expected_artifact_id
            or row["ordinal"] != ordinal
            or row["store_relative_root"] != expected_store_root
        ):
            raise ValueError("generation source inventory row identity differs")
        source = ReleaseAuxiliarySourceRunV1(
            artifact_id=_text(row["artifact_id"], "generation source artifact ID", 128),
            ordinal=ordinal,
            store_root=f"/KIRBY2_NONEXECUTING_GENERATION_SOURCES/{ordinal}",
            run_id=_text(row["run_id"], "generation source run ID", 64),
            manifest_sha256=_text(
                row["manifest_sha256"], "generation source manifest digest", 64
            ),
        )
        identities.append(
            (source.artifact_id, source.ordinal, source.run_id, source.manifest_sha256)
        )
    return tuple(identities)


def _full_day_verification_matches(value: object, run_id: str) -> bool:
    if type(value) is not dict or set(value) != {
        "artifact_digests_valid", "artifact_inventory_valid",
        "canonical_payloads_valid", "checkpoints_valid", "failures",
        "manifest_valid", "privacy_contract_valid", "replay_valid", "run_id",
        "status", "summary_valid",
    }:
        return False
    return (
        value["run_id"] == run_id
        and value["status"] == "PASS"
        and value["failures"] == []
        and all(
            value[field] is True
            for field in (
                "artifact_digests_valid", "artifact_inventory_valid",
                "canonical_payloads_valid", "checkpoints_valid", "manifest_valid",
                "privacy_contract_valid", "replay_valid", "summary_valid",
            )
        )
    )


def _validate_terminal_sink_receipt(value: object) -> None:
    row = _exact_object(
        value,
        {
            "bytes_drained", "bytes_written", "color", "columns", "drain_policy",
            "encoding", "rows", "sha256", "term",
        },
        "auxiliary terminal sink receipt",
    )
    bytes_drained = _nonnegative(row["bytes_drained"], "terminal drained bytes")
    bytes_written = _nonnegative(row["bytes_written"], "terminal written bytes")
    require_sha256(row["sha256"], "terminal stream digest")
    if (
        bytes_drained == 0
        or bytes_drained != bytes_written
        or {
            "color": row["color"],
            "columns": row["columns"],
            "drain_policy": row["drain_policy"],
            "encoding": row["encoding"],
            "rows": row["rows"],
            "term": row["term"],
        }
        != {
            "color": False,
            "columns": 120,
            "drain_policy": "CONTINUOUS",
            "encoding": "UTF-8",
            "rows": 40,
            "term": "dumb",
        }
    ):
        raise ValueError("auxiliary terminal sink receipt differs")


def _validate_auxiliary_attempt_receipts(
    template: ReleaseAuxiliaryPerformanceTemplateV1,
    source_identities: tuple[_AuxiliarySourceIdentity, ...],
    evidence_payloads: Mapping[str, bytes],
) -> None:
    workload_id = template.workload_id
    if "failure.json" in evidence_payloads:
        return
    parameters = template.input_identity.get("parameters")
    if type(parameters) is not dict:
        raise ValueError("auxiliary template parameters are unavailable")
    if workload_id == "RELEASE_INTERACTIVE_ACK_V1":
        receipt = _exact_object(
            _canonical_payload(
                evidence_payloads["interactive-ack/receipt.json"],
                "interactive acknowledgement receipt",
            ),
            {
                "acknowledgement_count", "acknowledgement_inventory_sha256",
                "best_bid_ticks", "lesson_id", "pair_count", "peak_rss_bytes",
                "starter_install", "starter_set", "status", "terminal",
            },
            "interactive acknowledgement receipt",
        )
        best_bid = _nonnegative(receipt["best_bid_ticks"], "interactive best bid")
        peak_rss = _nonnegative(receipt["peak_rss_bytes"], "interactive peak RSS")
        if (
            best_bid == 0
            or peak_rss == 0
            or receipt["acknowledgement_count"] != 1100
            or receipt["lesson_id"] != parameters["lesson_id"]
            or receipt["pair_count"] != 550
            or receipt["status"] != "PASS"
        ):
            raise ValueError("interactive acknowledgement receipt identity differs")
        _validate_terminal_sink_receipt(receipt["terminal"])
        expected_starter_entries = [
            {
                "manifest_path": parameters[f"{role}_manifest_path"],
                "manifest_sha256": parameters[f"{role}_manifest_sha256"],
                "pack_id": parameters[f"{role}_pack_id"],
                "role": role.upper(),
            }
            for role in ("scenario", "curriculum")
        ]
        starter_set = _exact_object(
            receipt["starter_set"],
            {"entries", "entries_sha256", "schema_version", "set_id"},
            "interactive starter set",
        )
        if starter_set != {
            "entries": expected_starter_entries,
            "entries_sha256": parameters["starter_entries_sha256"],
            "schema_version": 1,
            "set_id": "RELEASE_STARTER_SET_V1",
        }:
            raise ValueError("interactive starter-set evidence differs")
        starter_install = _exact_object(
            receipt["starter_install"],
            {
                "complete", "conflict_entries", "detail", "disposition", "entries",
                "schema_id", "schema_version", "set_id",
            },
            "interactive starter installation",
        )
        installed_entries = _array(
            starter_install["entries"], "interactive installed starter entries"
        )
        if len(installed_entries) != 2:
            raise ValueError("interactive starter installation entry count differs")
        from kirby2.packs.models import PackRegistryKeyV1

        for raw_entry, expected_entry, pack_type in zip(
            installed_entries,
            expected_starter_entries,
            ("SCENARIO", "CURRICULUM"),
            strict=True,
        ):
            entry = _exact_object(
                raw_entry,
                {"active", "key", "pack_id", "pack_type"},
                "interactive installed starter entry",
            )
            key = _exact_object(
                entry["key"],
                {"creator_id", "name", "namespace", "version"},
                "interactive installed starter key",
            )
            restored_key = PackRegistryKeyV1.from_dict(key)
            if (
                entry["active"] is not True
                or entry["pack_id"] != expected_entry["pack_id"]
                or entry["pack_type"] != pack_type
                or restored_key.namespace != "kirby2.examples"
            ):
                raise ValueError("interactive installed starter entry differs")
        if (
            starter_install["complete"] is not True
            or starter_install["conflict_entries"] != []
            or starter_install["detail"]
            != "Installed 2 missing starter pack(s) in dependency order."
            or starter_install["disposition"] != "INSTALLED"
            or starter_install["schema_id"] != "KIRBY2_RELEASE_STARTER_INSTALL_V1"
            or starter_install["schema_version"] != 1
            or starter_install["set_id"] != "RELEASE_STARTER_SET_V1"
        ):
            raise ValueError("interactive starter installation evidence differs")
        return
    if workload_id == "RELEASE_TERMINAL_UPDATE_V1":
        receipt = _exact_object(
            _canonical_payload(
                evidence_payloads["terminal-update/receipt.json"],
                "terminal update receipt",
            ),
            {
                "first_message_sequence", "last_message_sequence", "peak_rss_bytes",
                "rendered_update_count", "run_id", "source_evidence_sha256",
                "source_materialization", "status", "terminal",
                "update_inventory_sha256",
            },
            "terminal update receipt",
        )
        _nonnegative(receipt["first_message_sequence"], "terminal first sequence")
        _nonnegative(receipt["last_message_sequence"], "terminal last sequence")
        _nonnegative(receipt["peak_rss_bytes"], "terminal peak RSS")
        _validate_terminal_sink_receipt(receipt["terminal"])
        materialization = _exact_object(
            receipt["source_materialization"],
            {
                "candidate_id", "evidence_sha256", "maximum_initial_pending",
                "plan_sha256", "root_seed", "run_id", "verification",
                "workload_sha256",
            },
            "terminal source materialization",
        )
        _nonnegative(
            materialization["maximum_initial_pending"],
            "terminal maximum initial pending",
        )
        for field in ("evidence_sha256", "plan_sha256", "workload_sha256"):
            require_sha256(materialization[field], f"terminal {field}")
        terminal_run_id = _text(
            materialization["run_id"], "terminal source run ID", 64
        )
        if (
            _RUN_ID.fullmatch(terminal_run_id) is None
            or not _full_day_verification_matches(
                materialization["verification"], terminal_run_id
            )
        ):
            raise ValueError("terminal source verification differs")
        return
    if workload_id == "RELEASE_FULL_DAY_GENERATION_V1":
        rows = _array(
            load_canonical_json_bytes(
                evidence_payloads["full-day-generation/attempts.json"],
                "generation attempt receipts",
            ),
            "generation attempt receipts",
        )
        generation_sources = _generation_source_identities(evidence_payloads)
        if len(rows) != 4:
            raise ValueError("generation attempt receipt count differs")
        expected_fields = {
            "checkpoint_count", "checkpoint_growth_bytes_per_simulation_hour",
            "day_duration_us", "fresh_process_policy_id", "full_day_bytes",
            "largest_checkpoint_bytes", "ledger_growth_bytes_per_1000_events",
            "manifest_sha256", "maximum_initial_pending", "ordinal",
            "outer_event_count", "peak_rss_bytes", "plan_sha256", "run_id", "status",
            "wall_time_ns",
        }
        for ordinal, (raw_row, source) in enumerate(
            zip(rows, generation_sources, strict=True)
        ):
            row = _exact_object(raw_row, expected_fields, "generation attempt receipt")
            positive_fields = (
                "checkpoint_count", "checkpoint_growth_bytes_per_simulation_hour",
                "day_duration_us", "full_day_bytes", "largest_checkpoint_bytes",
                "ledger_growth_bytes_per_1000_events", "maximum_initial_pending",
                "outer_event_count", "peak_rss_bytes", "wall_time_ns",
            )
            if any(
                _nonnegative(row[field], f"generation {field}") == 0
                for field in positive_fields
            ):
                raise ValueError("generation attempt receipt contains a zero measurement")
            if (
                row["fresh_process_policy_id"] != "SPAWNED_CPYTHON_PROCESS_V1"
                or row["manifest_sha256"] != source[3]
                or row["ordinal"] != ordinal
                or row["plan_sha256"] != parameters["selected_plan_sha256"]
                or row["run_id"] != source[2]
                or row["status"] != "PASS"
            ):
                raise ValueError("generation attempt receipt differs from its source")
        return
    if workload_id not in {
        "RELEASE_FULL_DAY_REPLAY_V1", "RELEASE_MICROSCOPE_LOAD_V1",
    }:
        raise ValueError("auxiliary receipt workload is outside the closed registry")
    evidence_id = {
        "RELEASE_FULL_DAY_REPLAY_V1": "full-day-replay/attempts.json",
        "RELEASE_MICROSCOPE_LOAD_V1": "microscope-load/attempts.json",
    }[workload_id]
    try:
        value = load_canonical_json_bytes(evidence_payloads[evidence_id], evidence_id)
    except KeyError as error:
        raise ValueError("auxiliary publication lacks its attempt receipts") from error
    rows = _array(value, "auxiliary attempt receipts")
    if workload_id == "RELEASE_FULL_DAY_REPLAY_V1":
        if len(rows) != len(source_identities):
            raise ValueError("replay receipt count differs from its source inventory")
        expected_fields = {
            "fresh_process_policy_id", "manifest_result_sha256", "ordinal",
            "peak_rss_bytes", "run_id", "status", "verification", "wall_time_ns",
        }
        for raw_row, source in zip(rows, source_identities, strict=True):
            row = _exact_object(raw_row, expected_fields, "replay attempt receipt")
            if (
                _nonnegative(row["peak_rss_bytes"], "replay peak RSS") == 0
                or _nonnegative(row["wall_time_ns"], "replay wall time") == 0
            ):
                raise ValueError("replay attempt receipt contains a zero measurement")
            require_sha256(row["manifest_result_sha256"], "replay result digest")
            if (
                row["fresh_process_policy_id"] != "SPAWNED_CPYTHON_PROCESS_V1"
                or row["ordinal"] != source[1]
                or row["run_id"] != source[2]
                or row["status"] != "PASS"
                or not _full_day_verification_matches(row["verification"], source[2])
            ):
                raise ValueError("replay attempt receipt differs from its generation source")
        return

    if len(source_identities) != 1 or len(rows) != 21:
        raise ValueError("microscope receipt or source inventory differs")
    report_prefix = "microscope-load/report/"
    expected_report_names = (
        "assets/report.css", "assets/report.js", "index.html", "manifest.json",
    )
    actual_report_names = tuple(
        sorted(
            (
                evidence_id.removeprefix(report_prefix)
                for evidence_id in evidence_payloads
                if evidence_id.startswith(report_prefix)
            ),
            key=lambda item: item.encode("utf-8"),
        )
    )
    if actual_report_names != tuple(
        sorted(expected_report_names, key=lambda item: item.encode("utf-8"))
    ):
        raise ValueError("microscope report evidence inventory differs")
    from kirby2.microscope.report import _embedded_report, _validate_bundle_members

    report_members = {
        name: evidence_payloads[f"{report_prefix}{name}"]
        for name in expected_report_names
    }
    verification = _validate_bundle_members(
        report_members
    )
    expected_fields = {
        "asset_inventory", "bundle_id", "cursor_time_us", "fresh_process_policy_id",
        "inspection_run_id", "pane_ids", "peak_rss_bytes", "report_id", "run_id",
        "seek_event_count", "status", "verification", "wall_time_ns",
    }
    source = source_identities[0]
    report, _report_bytes = _embedded_report(report_members["index.html"])
    frames = report.get("frames")
    if (
        type(frames) is not list
        or len(frames) != 1
        or type(frames[0]) is not dict
        or type(frames[0].get("identity")) is not dict
        or frames[0]["identity"].get("source_run_id") != source[2]
    ):
        raise ValueError("microscope report differs from its generation source")
    if type(parameters.get("pane_ids")) is not list:
        raise ValueError("microscope template pane inventory is unavailable")
    expected_pane_ids = parameters["pane_ids"]
    receipt_identity: tuple[object, ...] | None = None
    for raw_row in rows:
        row = _exact_object(raw_row, expected_fields, "microscope attempt receipt")
        if any(
            _nonnegative(row[field], f"microscope {field}") == 0
            for field in (
                "cursor_time_us", "peak_rss_bytes", "seek_event_count", "wall_time_ns",
            )
        ):
            raise ValueError("microscope attempt receipt contains a zero measurement")
        if (
            row["fresh_process_policy_id"] != "SPAWNED_CPYTHON_PROCESS_V1"
            or row["inspection_run_id"] != source[2]
            or row["pane_ids"] != expected_pane_ids
            or row["run_id"] != source[2]
            or row["status"] != "PASS"
            or row["bundle_id"] != verification["bundle_id"]
            or row["report_id"] != verification["report_id"]
            or row["verification"] != verification
        ):
            raise ValueError("microscope attempt receipt differs from its verified report")
        identity = (
            row["asset_inventory"], row["bundle_id"], row["cursor_time_us"],
            row["inspection_run_id"], row["pane_ids"], row["report_id"], row["run_id"],
            row["seek_event_count"], row["verification"],
        )
        if receipt_identity is None:
            receipt_identity = identity
        elif identity != receipt_identity:
            raise ValueError("microscope attempt receipts changed semantic identity")


def _verify_auxiliary_evidence(
    result: ReleaseAuxiliaryPerformanceResultV1,
    template: ReleaseAuxiliaryPerformanceTemplateV1,
    inputs: ReleasePerformanceVerificationInputsV1,
    evidence_payloads: Mapping[str, bytes],
) -> tuple[_AuxiliarySourceIdentity, ...]:
    from .performance_auxiliary import (
        ReleaseAuxiliaryExecutionV1,
        ReleaseAuxiliarySourceRunV1,
        verify_auxiliary_performance_evidence,
    )

    _validate_auxiliary_evidence_inventory(template.workload_id, evidence_payloads)
    try:
        envelope_raw = evidence_payloads["execution-envelope.json"]
    except KeyError as error:
        raise ValueError("auxiliary publication lacks its execution envelope") from error
    envelope = _exact_object(
        _canonical_payload(envelope_raw, "auxiliary execution envelope"),
        {
            "artifact_manifest_sha256", "asset_manifest_sha256", "candidate_commit",
            "execution_policy_id", "input_identity_sha256", "source_manifest_sha256",
            "source_runs", "workload_id",
        },
        "auxiliary execution envelope",
    )
    source_rows = _array(envelope["source_runs"], "auxiliary execution source runs")
    source_runs = []
    for ordinal, raw in enumerate(source_rows):
        row = _exact_object(
            raw,
            {"artifact_id", "manifest_sha256", "ordinal", "run_id"},
            "auxiliary execution source run",
        )
        source_runs.append(
            ReleaseAuxiliarySourceRunV1(
                artifact_id=_text(row["artifact_id"], "auxiliary source artifact ID", 128),
                ordinal=row["ordinal"],  # type: ignore[arg-type]
                store_root=(
                    "/KIRBY2_NONEXECUTING_AUXILIARY_SOURCES/"
                    f"{template.workload_id.casefold()}/{ordinal}"
                ),
                run_id=_text(row["run_id"], "auxiliary source run ID", 64),
                manifest_sha256=_text(row["manifest_sha256"], "auxiliary source manifest", 64),
            )
        )
    request = ReleaseAuxiliaryExecutionV1(
        template=template,
        candidate_commit=inputs.candidate_commit,
        source_tree=inputs.source_tree,
        artifact_manifest_sha256=inputs.artifact_index_sha256,
        asset_manifest_sha256=(
            inputs.microscope_asset_manifest_sha256
            if template.workload_id == "RELEASE_MICROSCOPE_LOAD_V1"
            else None
        ),
        source_runs=tuple(source_runs),
    )
    verify_auxiliary_performance_evidence(request, result, evidence_payloads)
    identities = tuple(
        (item.artifact_id, item.ordinal, item.run_id, item.manifest_sha256)
        for item in source_runs
    )
    _validate_auxiliary_attempt_receipts(
        template,
        identities,
        evidence_payloads,
    )
    return identities


def _verify_release_performance_records(
    source: _PerformanceRecordSource,
    inputs: ReleasePerformanceVerificationInputsV1,
) -> ReleasePerformanceVerificationV1:
    if type(inputs) is not ReleasePerformanceVerificationInputsV1:
        raise TypeError("performance verification requires exact typed inputs")

    activation_raw = source.read(RELEASE_PERFORMANCE_ACTIVATION_PATH_V1)
    activation = ReleasePerformanceActivationRecordV1.from_bytes(activation_raw)
    if _identity_tuple(activation) != _expected_identity(inputs):
        raise ValueError("performance activation differs from its immutable upstream inputs")

    attempt_raw = _read_reference(source, activation.attempt_record)
    attempt = ReleasePerformanceAttemptRecordV1.from_bytes(attempt_raw)
    aggregate_raw = _read_reference(source, activation.aggregate_record)
    aggregate = ReleasePerformanceAggregateV1.from_bytes(aggregate_raw)
    if (
        _identity_tuple(attempt) != _expected_identity(inputs)
        or _identity_tuple(aggregate) != _expected_identity(inputs)
        or attempt.row_corpus_sha256 != inputs.row_corpus_sha256
        or aggregate.row_corpus_sha256 != inputs.row_corpus_sha256
        or attempt.attempt_id != activation.attempt_id
        or attempt.aggregate_record.as_dict() != activation.aggregate_record.as_dict()
    ):
        raise ValueError("performance attempt or aggregate upstream identity differs")

    expected_ids = _expected_work_unit_ids()
    actual_ids = tuple(item.work_unit_id for item in attempt.work_units)
    if (
        len(actual_ids) != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
        or actual_ids != expected_ids
        or len(set(actual_ids)) != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
    ):
        raise ValueError("performance publication lacks the exact 10,000 ordered logical IDs")

    path_claims: dict[str, tuple[str, int]] = {}
    digest_sizes: dict[str, int] = {}
    cas_digest_sizes: dict[str, int] = {}
    logical_record_count = 0
    logical_referenced_bytes = 0

    def account(reference: ReleasePerformanceRecordReferenceV1) -> None:
        nonlocal logical_record_count, logical_referenced_bytes
        claim = (reference.sha256, reference.size)
        prior = path_claims.setdefault(reference.path, claim)
        if prior != claim:
            raise ValueError("performance publication assigns conflicting bytes to one path")
        prior_size = digest_sizes.setdefault(reference.sha256, reference.size)
        if prior_size != reference.size:
            raise ValueError("performance publication assigns conflicting sizes to one digest")
        if reference.kind in _CAS_KINDS:
            prior_cas_size = cas_digest_sizes.setdefault(reference.sha256, reference.size)
            if prior_cas_size != reference.size:
                raise ValueError("performance CAS digest size differs across references")
        logical_record_count += 1
        logical_referenced_bytes += reference.size

    retry_count = 0
    complete_work_units = 0
    complete_result_records = 0
    complete_artifact_records = 0
    complete_audit_records = 0
    complete_ids: set[str] = set()
    failed_rows: list[tuple[str, str]] = []
    attempt_intervals: list[tuple[int, int]] = []

    for publication in attempt.work_units:
        cell, root_seed = _work_unit_parts(publication.work_unit_id)
        template = build_performance_row_template(cell, root_seed)
        bound_row = bind_performance_row_template(template, inputs.source_tree)
        results: list[ReleasePerformanceCellResultV1] = []
        compatibility_digests: list[str | None] = []

        for published_attempt in publication.attempts:
            result_raw = _read_reference(source, published_attempt.result_record)
            account(published_attempt.result_record)
            result = ReleasePerformanceCellResultV1.from_bytes(result_raw)
            inventory_raw = _read_reference(source, published_attempt.artifact_inventory_record)
            account(published_attempt.artifact_inventory_record)
            inventory = ReleasePerformanceArtifactInventoryV1.from_bytes(inventory_raw)
            if (
                result.work_unit_id != publication.work_unit_id
                or result.attempt != published_attempt.attempt
                or inventory.work_unit_id != publication.work_unit_id
                or inventory.attempt != published_attempt.attempt
                or inventory.artifact_set_sha256 != result.artifact_set_sha256
            ):
                raise ValueError("performance result, inventory, or publication identity differs")
            result.validate_bound_row(bound_row)
            if result.runner_source_sha256 != inputs.source_manifest_sha256:
                raise ValueError("performance result differs from the staged runner-source tree")
            if not (
                attempt.start_monotonic_ns
                <= result.operational.start_monotonic_ns
                < result.operational.end_monotonic_ns
                <= attempt.end_monotonic_ns
            ):
                raise ValueError("performance attempt interval does not enclose its cell evidence")
            attempt_intervals.append(
                (
                    result.operational.start_monotonic_ns,
                    result.operational.end_monotonic_ns,
                )
            )

            semantic: dict[str, bytes] = {}
            for reference in inventory.semantic_members:
                raw = _read_reference(source, reference)
                account(reference)
                semantic[reference.record_id] = raw
            compatibility_raw: bytes | None = None
            for reference in inventory.compatibility_sidecars:
                compatibility_raw = _read_reference(source, reference)
                account(reference)
            operational_reference = inventory.operational_sidecars[0]
            operational_raw = _read_reference(source, operational_reference)
            account(operational_reference)
            operational = _canonical_payload(operational_raw, "performance operational sidecar")
            if operational != result.operational.as_dict():
                raise ValueError("performance operational sidecar differs from its result")

            audit_member = next(
                (item for item in inventory.semantic_members if item.record_id == "audit_result.json"),
                None,
            )
            if (published_attempt.audit_record is None) != (audit_member is None):
                raise ValueError("performance audit publication completeness differs")
            if published_attempt.audit_record is not None and audit_member is not None and (
                published_attempt.audit_record.path != audit_member.path
                or published_attempt.audit_record.sha256 != audit_member.sha256
                or published_attempt.audit_record.size != audit_member.size
            ):
                raise ValueError("performance audit publication differs from its semantic member")

            present_names = tuple(semantic)
            digest_by_name = {
                "run_manifest.json": result.run_manifest_sha256,
                "native_recording.json": result.native_recording_sha256,
                "semantic_result.json": result.semantic_result_sha256,
                "capabilities.json": hashlib.sha256(
                    canonical_json_bytes([item.as_dict() for item in result.capability_records])
                ).hexdigest(),
                "checks.json": hashlib.sha256(
                    canonical_json_bytes([item.as_dict() for item in result.check_records])
                ).hexdigest(),
                "audit_result.json": result.audit_result_sha256,
            }
            for name in present_names:
                if hashlib.sha256(semantic[name]).hexdigest() != digest_by_name[name]:
                    raise ValueError("performance semantic member differs from the result digest")
            for name in ("run_manifest.json", "native_recording.json", "semantic_result.json", "audit_result.json"):
                if (name in semantic) != (digest_by_name[name] is not None):
                    raise ValueError("performance partial semantic inventory differs from result availability")
            if result.status == "COMPLETE":
                if present_names != _SEMANTIC_MEMBER_NAMES or len(inventory.compatibility_sidecars) != 1:
                    raise ValueError("complete performance result lacks its full artifact tuple")
                verify_performance_cell_artifacts(result, semantic)
            elif present_names == _SEMANTIC_MEMBER_NAMES:
                if (
                    result.failure_code != "RESOURCE_LIMIT"
                    or result.artifact_set_sha256 is None
                    or len(inventory.compatibility_sidecars) != 1
                ):
                    raise ValueError("only a resource-limit failure may retain a full artifact tuple")
                verify_performance_cell_artifacts(result, semantic)
            elif result.artifact_set_sha256 is not None:
                raise ValueError("partial failed performance result claims an artifact set")
            needs_compatibility = any(
                name in semantic for name in ("native_recording.json", "semantic_result.json")
            )
            if bool(inventory.compatibility_sidecars) != needs_compatibility:
                raise ValueError("performance legacy-binding sidecar availability differs")
            _validate_semantic_members(result, bound_row, semantic, compatibility_raw)

            exceeded = _resource_exceeded(result)
            if result.status == "COMPLETE" and exceeded:
                raise ValueError("complete performance result exceeds an attempt resource limit")
            if result.failure_code == "RESOURCE_LIMIT" and not exceeded:
                raise ValueError("resource-limit failure lacks an exceeded resource")
            if result.status == "FAILED" and result.failure_code != "RESOURCE_LIMIT" and exceeded:
                raise ValueError("non-resource failure conceals an exceeded resource limit")
            expected_retry_reason = None if result.attempt == 1 else results[0].failure_code
            if result.operational.retry_reason != expected_retry_reason:
                raise ValueError("performance operational retry reason differs")
            results.append(result)
            compatibility_digests.append(
                None if compatibility_raw is None else hashlib.sha256(compatibility_raw).hexdigest()
            )

        typed_results = tuple(results)
        validate_performance_attempt_sequence(typed_results)
        if len(typed_results) == 2:
            retry_count += 1
            if (
                typed_results[1].operational.start_monotonic_ns
                < typed_results[0].operational.end_monotonic_ns
            ):
                raise ValueError("performance retry began before attempt one ended")
            for field in (
                "run_manifest_sha256", "native_recording_sha256",
                "semantic_result_sha256", "artifact_set_sha256", "audit_result_sha256",
            ):
                first_digest = getattr(typed_results[0], field)
                if first_digest is not None and getattr(typed_results[1], field) != first_digest:
                    raise ValueError("performance retry failed to reproduce a committed semantic artifact")
            if compatibility_digests[0] is not None and compatibility_digests[1] != compatibility_digests[0]:
                raise ValueError("performance retry changed its legacy binding sidecar")
        elif (
            typed_results[0].status == "FAILED"
            and typed_results[0].failure_code in {"PROCESS_FAILURE", "RESOURCE_LIMIT"}
        ):
            raise ValueError("performance publication omitted its required retry")
        final_result = typed_results[-1]
        if publication.status != final_result.status:
            raise ValueError("performance work-unit publication status differs from its final result")
        if final_result.status == "COMPLETE":
            complete_work_units += 1
            complete_result_records += 1
            complete_artifact_records += 1
            if publication.work_unit_id in complete_ids:
                raise ValueError("performance complete-run logical ID is duplicated")
            complete_ids.add(publication.work_unit_id)
            if publication.attempts[-1].audit_record is None:
                raise ValueError("complete performance work unit lacks an audit publication")
            complete_audit_records += 1
        else:
            if final_result.failure_code is None:
                raise ValueError("failed performance work unit lacks a stable final code")
            failed_rows.append((publication.work_unit_id, final_result.failure_code))

    _validate_worker_intervals(tuple(attempt_intervals))

    auxiliary_results: list[ReleaseAuxiliaryPerformanceResultV1] = []
    generation_sources: tuple[_AuxiliarySourceIdentity, ...] | None = None
    for wrapper, template in zip(attempt.auxiliaries, inputs.auxiliary_templates, strict=True):
        result_raw = _read_reference(source, wrapper.result_record)
        account(wrapper.result_record)
        result = ReleaseAuxiliaryPerformanceResultV1.from_bytes(result_raw)
        if (
            result.workload_id != wrapper.workload_id
            or result.provenance["candidate_commit"] != inputs.candidate_commit
        ):
            raise ValueError("auxiliary result publication identity differs")
        result.validate_protocol_binding(
            template,
            inputs.source_tree,
            artifact_manifest_sha256=inputs.artifact_index_sha256,
            asset_manifest_sha256=(
                inputs.microscope_asset_manifest_sha256
                if result.workload_id == "RELEASE_MICROSCOPE_LOAD_V1"
                else None
            ),
        )
        expected_evidence = tuple(
            {
                "evidence_id": reference.record_id,
                "sha256": reference.sha256,
                "size": reference.size,
            }
            for reference in wrapper.evidence_records
        )
        if result.evidence_records != expected_evidence:
            raise ValueError("auxiliary evidence inventory differs from its result")
        evidence_payloads: dict[str, bytes] = {}
        for reference in wrapper.evidence_records:
            evidence_payloads[reference.record_id] = _read_reference(source, reference)
            account(reference)
        source_identities = _verify_auxiliary_evidence(
            result,
            template,
            inputs,
            evidence_payloads,
        )
        if result.workload_id == "RELEASE_FULL_DAY_GENERATION_V1":
            generation_sources = _generation_source_identities(evidence_payloads)
        elif result.workload_id == "RELEASE_FULL_DAY_REPLAY_V1":
            if generation_sources is None or source_identities != generation_sources:
                raise ValueError("replay sources differ from the generation publication")
        elif result.workload_id == "RELEASE_MICROSCOPE_LOAD_V1":
            if generation_sources is None or source_identities != generation_sources[1:2]:
                raise ValueError("microscope source differs from generation ordinal one")
        auxiliary_results.append(result)

    work_units_sha256 = hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in attempt.work_units])
    ).hexdigest()
    auxiliary_results_sha256 = hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in attempt.auxiliaries])
    ).hexdigest()
    cas_rows = [
        {"sha256": digest, "size": cas_digest_sizes[digest]}
        for digest in sorted(cas_digest_sizes, key=lambda item: item.encode("utf-8"))
    ]
    cas_inventory_sha256 = hashlib.sha256(canonical_json_bytes(cas_rows)).hexdigest()
    aggregate_artifact_bytes = sum(digest_sizes.values())
    total_wall_ns = attempt.total_wall_ns
    throughput = round_div_even(complete_work_units * 10**15, total_wall_ns)
    if complete_work_units * 10**9 >= total_wall_ns:
        throughput_status = "PASS"
    elif 10 * complete_work_units * 10**9 >= total_wall_ns:
        throughput_status = "WARNING"
    else:
        throughput_status = "FAIL"
    if aggregate_artifact_bytes <= RELEASE_ARTIFACT_PASS_BYTES_V1:
        artifact_status = "PASS"
    elif aggregate_artifact_bytes <= RELEASE_ARTIFACT_WARNING_BYTES_V1:
        artifact_status = "WARNING"
    else:
        artifact_status = "FAIL"
    failures, warnings, status_value = _status_codes(
        complete_work_units,
        tuple(failed_rows),
        tuple(auxiliary_results),
        total_wall_ns,
        throughput_status,
        artifact_status,
    )
    expected_aggregate = ReleasePerformanceAggregateV1(
        status=status_value,
        candidate_commit=inputs.candidate_commit,
        source_manifest_sha256=inputs.source_manifest_sha256,
        protocol_set_sha256=inputs.protocol_set_sha256,
        artifact_index_sha256=inputs.artifact_index_sha256,
        build_evidence_sha256=inputs.build_evidence_sha256,
        threshold_manifest_sha256=inputs.threshold_manifest_sha256,
        runner_source_lock_sha256=inputs.runner_source_lock_sha256,
        row_corpus_sha256=inputs.row_corpus_sha256,
        work_units_sha256=work_units_sha256,
        auxiliary_results_sha256=auxiliary_results_sha256,
        cas_inventory_sha256=cas_inventory_sha256,
        work_unit_count=len(attempt.work_units),
        unique_complete_run_ids=len(complete_ids),
        complete_work_unit_count=complete_work_units,
        complete_result_records=complete_result_records,
        complete_artifact_records=complete_artifact_records,
        complete_audit_records=complete_audit_records,
        auxiliary_result_count=len(auxiliary_results),
        retry_count=retry_count,
        failed_work_unit_count=len(failed_rows),
        total_wall_ns=total_wall_ns,
        throughput_microruns_per_second=throughput,
        throughput_status=throughput_status,
        aggregate_artifact_bytes=aggregate_artifact_bytes,
        logical_referenced_bytes=logical_referenced_bytes,
        artifact_bytes_status=artifact_status,
        cas_object_count=len(cas_digest_sizes),
        logical_record_count=logical_record_count,
        warning_count=len(warnings),
        failure_codes=failures,
    )
    if aggregate != expected_aggregate:
        raise ValueError("performance aggregate does not reconstruct from immutable records")
    if (
        attempt.status != status_value
        or attempt.warning_codes != warnings
        or attempt.failure_codes != failures
        or activation.status != status_value
        or activation.work_unit_count != len(attempt.work_units)
        or activation.complete_work_unit_count != complete_work_units
        or activation.auxiliary_result_count != len(auxiliary_results)
        or activation.activated_at_utc < attempt.finished_at_utc
    ):
        raise ValueError("performance attempt or activation summary does not reconcile")

    if (
        source.read(RELEASE_PERFORMANCE_ACTIVATION_PATH_V1) != activation_raw
        or source.read(RELEASE_PERFORMANCE_ATTEMPT_PATH_V1) != attempt_raw
        or source.read(RELEASE_PERFORMANCE_AGGREGATE_PATH_V1) != aggregate_raw
    ):
        raise ValueError("performance publication changed during final reread")
    source.finish()
    return ReleasePerformanceVerificationV1(
        status=status_value,
        candidate_commit=inputs.candidate_commit,
        source_manifest_sha256=inputs.source_manifest_sha256,
        protocol_set_sha256=inputs.protocol_set_sha256,
        artifact_index_sha256=inputs.artifact_index_sha256,
        attempt_id=attempt.attempt_id,
        activation_sha256=hashlib.sha256(activation_raw).hexdigest(),
        attempt_sha256=hashlib.sha256(attempt_raw).hexdigest(),
        aggregate_sha256=hashlib.sha256(aggregate_raw).hexdigest(),
        work_unit_count=len(attempt.work_units),
        complete_work_unit_count=complete_work_units,
        auxiliary_result_count=len(auxiliary_results),
        retry_count=retry_count,
        cas_object_count=len(cas_digest_sizes),
        aggregate_artifact_bytes=aggregate_artifact_bytes,
    )


def verify_release_performance_candidate_records(
    records: Mapping[str, bytes],
    *,
    inputs: ReleasePerformanceVerificationInputsV1,
) -> ReleasePerformanceVerificationV1:
    """Deeply verify an unpublished candidate record set without executing work."""

    source = _CandidateRecordSource(records)
    try:
        return _verify_release_performance_records(source, inputs)
    finally:
        source.close()


def verify_release_performance_records(
    artifact_root: Path,
    *,
    inputs: ReleasePerformanceVerificationInputsV1,
) -> ReleasePerformanceVerificationV1:
    """Deeply verify the one immutable active WO40-I publication from exact bytes."""

    source = _DiskRecordSource(artifact_root)
    try:
        return _verify_release_performance_records(source, inputs)
    finally:
        source.close()


__all__ = [
    "RELEASE_PERFORMANCE_ACTIVATION_PATH_V1",
    "RELEASE_PERFORMANCE_AGGREGATE_PATH_V1",
    "RELEASE_PERFORMANCE_ATTEMPT_PATH_V1",
    "RELEASE_PERFORMANCE_GATE_ID_V1",
    "RELEASE_PERFORMANCE_PUBLICATION_ROOT_V1",
    "RELEASE_PERFORMANCE_TARGET_ID_V1",
    "ReleasePerformanceActivationV1",
    "ReleasePerformanceActivationRecordV1",
    "ReleasePerformanceAggregateV1",
    "ReleasePerformanceArtifactInventoryV1",
    "ReleasePerformanceAttemptPublicationV1",
    "ReleasePerformanceAttemptRecordV1",
    "ReleasePerformanceAuxiliaryReferenceV1",
    "ReleasePerformanceRecordReferenceV1",
    "ReleasePerformanceUnitPublicationV1",
    "ReleasePerformanceVerificationInputsV1",
    "ReleasePerformanceVerificationV1",
    "ReleasePerformanceWorkUnitPublicationV1",
    "performance_publication_paths",
    "release_performance_auxiliary_path",
    "release_performance_cas_path",
    "release_performance_record_paths",
    "release_performance_reference",
    "release_performance_work_unit_paths",
    "verify_release_performance_candidate_records",
    "verify_release_performance_records",
]
