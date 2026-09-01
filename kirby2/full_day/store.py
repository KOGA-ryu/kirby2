"""Crash-safe immutable persistence, reopen, seek, and extraction for full days."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from kirby2 import __version__
from kirby2.research.models import (
    RUN_MANIFEST_SCHEMA_VERSION,
    ArtifactReference,
    ArtifactType,
    RunManifest,
    RunType,
)
from kirby2.research.paths import DataAreaId, DataPaths
from kirby2.research.runtime import git_commit, software_version
from kirby2.research.toml_codec import load_toml

from .checkpoint_contract import QuiescentCutV1
from .events import (
    FullDayEventV1,
    NativeLedgerEntryV1,
    WorkStageV1,
    canonical_event_prefix_sha256,
)
from .models import (
    FULL_DAY_PLAN_SCHEMA_VERSION,
    FullDayPlanV1,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from .runtime import FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION, FullDayRuntime
from .summary import DAY_SUMMARY_SCHEMA_VERSION, DaySummaryV1, summarize_full_day


FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION = 1
FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION = 1
FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION = 1
FULL_DAY_QUALIFICATION_SCHEMA_VERSION = 1
FULL_DAY_DIAGNOSTICS_SCHEMA_VERSION = 1
FULL_DAY_WINDOW_SCHEMA_VERSION = 1
FULL_DAY_RUNTIME_STATE_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-runtime-state+json"
)
FULL_DAY_CHECKPOINT_INDEX_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-checkpoint-index+json"
)
FULL_DAY_OUTER_LEDGER_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-outer-event-ledger+json"
)
FULL_DAY_SUBSYSTEM_LEDGER_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-subsystem-ledger+json"
)
FULL_DAY_SUMMARY_MEDIA_TYPE = "application/vnd.kirby2.day-summary+json"
FULL_DAY_WINDOW_MEDIA_TYPE = "application/vnd.kirby2.full-day-window+json"
FULL_DAY_PLAN_MEDIA_TYPE = "application/vnd.kirby2.full-day-plan+json"
FULL_DAY_QUALIFICATION_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-qualification+json"
)
FULL_DAY_DIAGNOSTICS_MEDIA_TYPE = (
    "application/vnd.kirby2.full-day-diagnostics+json"
)
_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOW_POLICY_IDS = frozenset({"OBSERVABLE_CONTEXT_V1"})
_VERIFIED_FULL_DAY_SESSION_TOKEN = object()
_SEALED_ARTIFACT_TYPES = frozenset(
    {ArtifactType.FULL_DAY_PLAN, ArtifactType.FULL_DAY_CHECKPOINT}
)
_ARTIFACT_CONTRACTS = {
    ArtifactType.FULL_DAY_PLAN: (
        FULL_DAY_PLAN_SCHEMA_VERSION,
        FULL_DAY_PLAN_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER: (
        FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
        FULL_DAY_OUTER_LEDGER_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER: (
        FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
        FULL_DAY_SUBSYSTEM_LEDGER_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_CHECKPOINT_INDEX: (
        FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION,
        FULL_DAY_CHECKPOINT_INDEX_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_CHECKPOINT: (
        FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION,
        FULL_DAY_RUNTIME_STATE_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_SUMMARY: (
        DAY_SUMMARY_SCHEMA_VERSION,
        FULL_DAY_SUMMARY_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_QUALIFICATION: (
        FULL_DAY_QUALIFICATION_SCHEMA_VERSION,
        FULL_DAY_QUALIFICATION_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_DIAGNOSTICS: (
        FULL_DAY_DIAGNOSTICS_SCHEMA_VERSION,
        FULL_DAY_DIAGNOSTICS_MEDIA_TYPE,
    ),
    ArtifactType.FULL_DAY_WINDOW: (
        FULL_DAY_WINDOW_SCHEMA_VERSION,
        FULL_DAY_WINDOW_MEDIA_TYPE,
    ),
}
_ROW_COUNT_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER,
        ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER,
        ArtifactType.FULL_DAY_CHECKPOINT_INDEX,
    }
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be nonempty text")
    return value


def _portable_relative_path(value: object) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError("artifact path must be nonempty canonical POSIX text")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or value != posix.as_posix()
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError("artifact path must be portable, relative, and traversal-free")
    return value


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    payload = parse_canonical_json_object(raw)
    if canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} is not canonical JSON")
    return payload


@dataclass(frozen=True, slots=True)
class FullDayCheckpointIndexEntryV1:
    schema_version: int
    cut: QuiescentCutV1
    last_global_key: tuple[object, ...] | None
    checkpoint_semantic_sha256: str
    artifact: ArtifactReference
    engine_id: str
    engine_version: str
    runtime_implementation_version: int
    python_implementation: str
    python_major: int
    python_minor: int

    def __post_init__(self) -> None:
        if self.schema_version != FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION:
            raise ValueError("checkpoint-index entry schema version must be 1")
        if type(self.cut) is not QuiescentCutV1:
            raise TypeError("checkpoint-index cut must use QuiescentCutV1")
        self.cut.validate_quiescent()
        if self.last_global_key is not None:
            if type(self.last_global_key) is not tuple or len(self.last_global_key) != 5:
                raise ValueError("checkpoint last global key must have five fields")
            validate_strict_json(list(self.last_global_key))
        if (self.cut.last_global_event_sequence == 0) != (
            self.last_global_key is None
        ):
            raise ValueError("checkpoint last key availability differs from sequence")
        _sha256(self.checkpoint_semantic_sha256, "checkpoint semantic digest")
        if (
            type(self.artifact) is not ArtifactReference
            or self.artifact.artifact_type is not ArtifactType.FULL_DAY_CHECKPOINT
            or self.artifact.media_type != FULL_DAY_RUNTIME_STATE_MEDIA_TYPE
        ):
            raise ValueError("checkpoint index requires a typed runtime-state artifact")
        if type(self.engine_id) is not str or not self.engine_id:
            raise ValueError("checkpoint engine ID is required")
        if type(self.engine_version) is not str or not self.engine_version:
            raise ValueError("checkpoint engine version is required")
        _exact_int(
            self.runtime_implementation_version,
            "runtime implementation version",
            minimum=1,
        )
        if type(self.python_implementation) is not str or not self.python_implementation:
            raise ValueError("checkpoint Python implementation is required")
        _exact_int(self.python_major, "python major")
        _exact_int(self.python_minor, "python minor")

    @property
    def cut_time_us(self) -> int:
        return self.cut.simulation_time_us

    @property
    def is_compatible(self) -> bool:
        return (
            self.engine_id == "KIRBY2"
            and self.engine_version == __version__
            and self.runtime_implementation_version
            == FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION
            and self.python_implementation == platform.python_implementation()
            and self.python_major == os.sys.version_info.major
            and self.python_minor == os.sys.version_info.minor
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.as_dict(),
            "checkpoint_semantic_sha256": self.checkpoint_semantic_sha256,
            "cut": self.cut.as_dict(),
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "last_global_key": (
                None if self.last_global_key is None else list(self.last_global_key)
            ),
            "python_implementation": self.python_implementation,
            "python_major": self.python_major,
            "python_minor": self.python_minor,
            "runtime_implementation_version": self.runtime_implementation_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> FullDayCheckpointIndexEntryV1:
        expected = {
            "artifact",
            "checkpoint_semantic_sha256",
            "cut",
            "engine_id",
            "engine_version",
            "last_global_key",
            "python_implementation",
            "python_major",
            "python_minor",
            "runtime_implementation_version",
            "schema_version",
        }
        if set(payload) != expected:
            raise ValueError("checkpoint-index entry fields differ from schema v1")
        artifact = payload["artifact"]
        cut = payload["cut"]
        last_key = payload["last_global_key"]
        if not isinstance(artifact, Mapping) or not isinstance(cut, Mapping):
            raise TypeError("checkpoint-index nested records are invalid")
        if last_key is not None and type(last_key) is not list:
            raise TypeError("checkpoint last global key must be an array or null")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            cut=QuiescentCutV1.from_dict(cut),
            last_global_key=None if last_key is None else tuple(last_key),
            checkpoint_semantic_sha256=_sha256(
                payload["checkpoint_semantic_sha256"],
                "checkpoint semantic digest",
            ),
            artifact=ArtifactReference.from_dict(dict(artifact)),
            engine_id=_exact_text(payload["engine_id"], "checkpoint engine ID"),
            engine_version=_exact_text(
                payload["engine_version"], "checkpoint engine version"
            ),
            runtime_implementation_version=_exact_int(
                payload["runtime_implementation_version"],
                "runtime implementation version",
                minimum=1,
            ),
            python_implementation=_exact_text(
                payload["python_implementation"],
                "checkpoint Python implementation",
            ),
            python_major=_exact_int(payload["python_major"], "python major"),
            python_minor=_exact_int(payload["python_minor"], "python minor"),
        )


@dataclass(frozen=True, slots=True)
class FullDayCheckpointIndexV1:
    schema_version: int
    semantic_plan_sha256: str
    entries: tuple[FullDayCheckpointIndexEntryV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION:
            raise ValueError("full-day checkpoint-index schema version must be 1")
        _sha256(self.semantic_plan_sha256, "checkpoint-index plan digest")
        if type(self.entries) is not tuple or not self.entries or any(
            type(item) is not FullDayCheckpointIndexEntryV1 for item in self.entries
        ):
            raise ValueError("checkpoint index requires typed entries")
        keys = tuple(
            (item.cut_time_us, item.cut.last_global_event_sequence)
            for item in self.entries
        )
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("checkpoint index entries must be unique and ordered")
        paths = tuple(item.artifact.relative_path for item in self.entries)
        if len(paths) != len(set(paths)):
            raise ValueError("checkpoint index artifact paths must be unique")

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [item.as_dict() for item in self.entries],
            "schema_version": self.schema_version,
            "semantic_plan_sha256": self.semantic_plan_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> FullDayCheckpointIndexV1:
        payload = _canonical_object(raw, "checkpoint index")
        if set(payload) != {"entries", "schema_version", "semantic_plan_sha256"}:
            raise ValueError("checkpoint index fields differ from schema v1")
        entries = payload["entries"]
        if type(entries) is not list or any(
            not isinstance(item, Mapping) for item in entries
        ):
            raise TypeError("checkpoint index entries must be an array of objects")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            semantic_plan_sha256=_sha256(
                payload["semantic_plan_sha256"], "checkpoint-index plan digest"
            ),
            entries=tuple(
                FullDayCheckpointIndexEntryV1.from_dict(item) for item in entries
            ),
        )


@dataclass(frozen=True, slots=True)
class FullDayVerificationReportV1:
    run_id: str
    manifest_valid: bool
    artifact_inventory_valid: bool
    artifact_digests_valid: bool
    canonical_payloads_valid: bool
    checkpoints_valid: bool
    replay_valid: bool
    summary_valid: bool
    privacy_contract_valid: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.manifest_valid,
                self.artifact_inventory_valid,
                self.artifact_digests_valid,
                self.canonical_payloads_valid,
                self.checkpoints_valid,
                self.replay_valid,
                self.summary_valid,
                self.privacy_contract_valid,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_valid": self.artifact_digests_valid,
            "artifact_inventory_valid": self.artifact_inventory_valid,
            "canonical_payloads_valid": self.canonical_payloads_valid,
            "checkpoints_valid": self.checkpoints_valid,
            "failures": list(self.failures),
            "manifest_valid": self.manifest_valid,
            "privacy_contract_valid": self.privacy_contract_valid,
            "replay_valid": self.replay_valid,
            "run_id": self.run_id,
            "status": "PASS" if self.passed else "FAIL",
            "summary_valid": self.summary_valid,
        }


@dataclass(frozen=True, slots=True)
class VerifiedFullDayRunV1:
    manifest: RunManifest
    plan: FullDayPlanV1
    events: tuple[FullDayEventV1, ...]
    native_entries: tuple[NativeLedgerEntryV1, ...]
    checkpoint_index: FullDayCheckpointIndexV1
    summary: DaySummaryV1


@dataclass(frozen=True, slots=True)
class FullDaySeekResultV1:
    run_id: str
    target_time_us: int
    source_checkpoint: FullDayCheckpointIndexEntryV1
    quiescent_cut: QuiescentCutV1
    runtime: FullDayRuntime
    uninterrupted_event_count: int
    uninterrupted_event_prefix_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "event_count": len(self.runtime.events),
            "event_prefix_sha256": canonical_event_prefix_sha256(self.runtime.events),
            "quiescent_cut": self.quiescent_cut.as_dict(),
            "run_id": self.run_id,
            "source_checkpoint_cut_time_us": self.source_checkpoint.cut_time_us,
            "source_checkpoint_sha256": self.source_checkpoint.artifact.sha256,
            "result_projection_sha256": canonical_sha256(
                self.runtime.result_projection()
            ),
            "target_time_us": self.target_time_us,
            "uninterrupted_event_count": self.uninterrupted_event_count,
            "uninterrupted_event_prefix_sha256": (
                self.uninterrupted_event_prefix_sha256
            ),
        }


@dataclass(frozen=True, slots=True)
class VerifiedFullDaySessionV1:
    """One operation-scoped, deeply verified view of an immutable full day."""

    _store: FullDayStore = field(repr=False, compare=False)
    _loaded: VerifiedFullDayRunV1 = field(repr=False, compare=False)
    verification: FullDayVerificationReportV1
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _VERIFIED_FULL_DAY_SESSION_TOKEN:
            raise TypeError("verified full-day sessions require the store verifier")
        if type(self._store) is not FullDayStore:
            raise TypeError("verified full-day session requires its verifying store")
        if type(self._loaded) is not VerifiedFullDayRunV1:
            raise TypeError("verified full-day session requires a typed loaded run")
        if (
            type(self.verification) is not FullDayVerificationReportV1
            or not self.verification.passed
            or self.verification.run_id != self._loaded.manifest.run_id
        ):
            raise ValueError("verified full-day session requires its passing report")

    @property
    def run_id(self) -> str:
        return self._loaded.manifest.run_id

    @property
    def plan(self) -> FullDayPlanV1:
        return self._loaded.plan

    def inspection(self) -> dict[str, object]:
        payload = FullDayStore._inspect_complete(self._loaded)
        payload["verification"] = self.verification.as_dict()
        return payload

    def seek(self, target_time_us: int) -> FullDaySeekResultV1:
        return self._store._seek_loaded(
            self._loaded,
            target_time_us,
        )


@dataclass(frozen=True, slots=True)
class FullDayWindowV1:
    schema_version: int
    parent_run_id: str
    source_checkpoint: ArtifactReference
    source_checkpoint_cut_time_us: int
    source_event_prefix_sha256: str
    context_event_count: int
    context_event_prefix_sha256: str
    start_time_us: int
    end_time_us: int
    reveal_policy: str
    observable_context: Mapping[str, object]
    outer_events: tuple[Mapping[str, object], ...]
    observable_native_entries: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if self.schema_version != FULL_DAY_WINDOW_SCHEMA_VERSION:
            raise ValueError("full-day window schema version must be 1")
        if _RUN_ID.fullmatch(self.parent_run_id) is None:
            raise ValueError("full-day window parent run ID is invalid")
        if (
            type(self.source_checkpoint) is not ArtifactReference
            or self.source_checkpoint.artifact_type
            is not ArtifactType.FULL_DAY_CHECKPOINT
        ):
            raise ValueError("full-day window requires a typed source checkpoint")
        _exact_int(self.source_checkpoint_cut_time_us, "source checkpoint cut")
        _sha256(self.source_event_prefix_sha256, "source event-prefix digest")
        _exact_int(self.context_event_count, "context event count")
        _sha256(self.context_event_prefix_sha256, "context event-prefix digest")
        start = _exact_int(self.start_time_us, "window start")
        end = _exact_int(self.end_time_us, "window end")
        if end <= start:
            raise ValueError("full-day window must be nonempty and half-open")
        if self.reveal_policy not in _WINDOW_POLICY_IDS:
            raise ValueError("full-day window reveal policy is unsupported")
        validate_strict_json(self.observable_context)
        validate_strict_json(self.outer_events)
        validate_strict_json(self.observable_native_entries)

    def as_dict(self) -> dict[str, object]:
        return {
            "end_time_us": self.end_time_us,
            "context_event_count": self.context_event_count,
            "context_event_prefix_sha256": self.context_event_prefix_sha256,
            "observable_context": dict(self.observable_context),
            "observable_native_entries": [dict(item) for item in self.observable_native_entries],
            "outer_events": [dict(item) for item in self.outer_events],
            "parent_run_id": self.parent_run_id,
            "reveal_policy": self.reveal_policy,
            "schema_version": self.schema_version,
            "source_checkpoint": self.source_checkpoint.as_dict(),
            "source_checkpoint_cut_time_us": self.source_checkpoint_cut_time_us,
            "source_event_prefix_sha256": self.source_event_prefix_sha256,
            "start_time_us": self.start_time_us,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def semantic_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> FullDayWindowV1:
        payload = _canonical_object(raw, "full-day window")
        expected = {
            "context_event_count",
            "context_event_prefix_sha256",
            "end_time_us",
            "observable_context",
            "observable_native_entries",
            "outer_events",
            "parent_run_id",
            "reveal_policy",
            "schema_version",
            "source_checkpoint",
            "source_checkpoint_cut_time_us",
            "source_event_prefix_sha256",
            "start_time_us",
        }
        if set(payload) != expected:
            raise ValueError("full-day window fields differ from schema v1")
        reference = payload["source_checkpoint"]
        context = payload["observable_context"]
        outer = payload["outer_events"]
        native = payload["observable_native_entries"]
        if (
            not isinstance(reference, Mapping)
            or not isinstance(context, Mapping)
            or type(outer) is not list
            or type(native) is not list
            or any(not isinstance(item, Mapping) for item in (*outer, *native))
        ):
            raise TypeError("full-day window nested payloads are invalid")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            parent_run_id=_exact_text(payload["parent_run_id"], "parent run ID"),
            source_checkpoint=ArtifactReference.from_dict(dict(reference)),
            source_checkpoint_cut_time_us=_exact_int(
                payload["source_checkpoint_cut_time_us"], "source checkpoint cut"
            ),
            source_event_prefix_sha256=_sha256(
                payload["source_event_prefix_sha256"], "source event-prefix digest"
            ),
            context_event_count=_exact_int(
                payload["context_event_count"], "context event count"
            ),
            context_event_prefix_sha256=_sha256(
                payload["context_event_prefix_sha256"],
                "context event-prefix digest",
            ),
            start_time_us=_exact_int(payload["start_time_us"], "window start"),
            end_time_us=_exact_int(payload["end_time_us"], "window end"),
            reveal_policy=_exact_text(payload["reveal_policy"], "reveal policy"),
            observable_context=dict(context),
            outer_events=tuple(dict(item) for item in outer),
            observable_native_entries=tuple(dict(item) for item in native),
        )


def _reference(
    *,
    name: str,
    relative_path: str,
    payload: bytes,
    schema_version: int,
    media_type: str,
    artifact_type: ArtifactType,
    row_count: int | None = None,
) -> ArtifactReference:
    return ArtifactReference(
        name=name,
        relative_path=_portable_relative_path(relative_path),
        sha256=_sha256_bytes(payload),
        schema_version=schema_version,
        row_count=row_count,
        media_type=media_type,
        artifact_type=artifact_type,
    )


def _synthetic_quiescent_cut(runtime: FullDayRuntime) -> QuiescentCutV1:
    pending = runtime.pending_work
    events = runtime.events
    time_us = runtime.clock.current_time_us
    due = tuple(item for item in pending if item.key.simulation_time_us <= time_us)
    if due:
        raise RuntimeError("runtime still has due work at a synthetic seek/storage cut")
    completed = runtime._last_completed_key
    boundary_complete = all(
        operation.boundary.simulation_time_us > time_us
        or index < runtime._calendar_boundary_index
        for index, operation in enumerate(runtime.plan.calendar.boundary_operations)
    )
    return QuiescentCutV1(
        schema_version=1,
        simulation_time_us=time_us,
        microstep=(
            0
            if completed is None or completed.simulation_time_us != time_us
            else completed.microstep
        ),
        checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
        last_global_event_sequence=len(events),
        event_prefix_last_global_sequence=len(events),
        event_prefix_sha256=canonical_event_prefix_sha256(events),
        pending_work_count=len(pending),
        next_pending_time_us=(
            None if not pending else pending[0].key.simulation_time_us
        ),
        next_pending_microstep=(None if not pending else pending[0].key.microstep),
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=boundary_complete,
    )


class FullDayStore:
    """One explicit-root immutable store for complete and extracted full-day runs."""

    def __init__(self, root: Path) -> None:
        self.paths = DataPaths(root)
        self.root = self.paths.root

    @property
    def runs_directory(self) -> Path:
        return self.paths.runs

    def run_directory(self, run_id: str) -> Path:
        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("invalid content-derived run ID")
        return self.paths.runs / run_id

    def _safe_existing_run_directory(self, run_id: str) -> Path:
        self.paths.validate((DataAreaId.RUNS,))
        directory = self.run_directory(run_id)
        try:
            metadata = directory.lstat()
        except FileNotFoundError as error:
            raise ValueError(f"unknown run ID: {run_id}") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("run artifact root is not a real directory")
        resolved_runs = self.paths.runs.resolve(strict=True)
        resolved = directory.resolve(strict=True)
        try:
            resolved.relative_to(resolved_runs)
        except ValueError as error:
            raise ValueError("run artifact root escapes the governed runs area") from error
        if resolved != directory:
            raise ValueError("run artifact root was rebound through a symlink")
        return directory

    @staticmethod
    def _safe_file(directory: Path, relative_path: str) -> Path:
        selected = _portable_relative_path(relative_path)
        candidate = directory.joinpath(*PurePosixPath(selected).parts)
        cursor = directory
        for part in PurePosixPath(selected).parts:
            cursor = cursor / part
            metadata = cursor.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("artifact path contains a symlink")
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("artifact reference does not name a regular file")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(directory.resolve(strict=True))
        except ValueError as error:
            raise ValueError("artifact reference escapes its immutable run") from error
        return candidate

    def load_manifest(self, run_id: str) -> RunManifest:
        directory = self._safe_existing_run_directory(run_id)
        manifest_path = self._safe_file(directory, "manifest.toml")
        manifest = RunManifest.from_dict(load_toml(manifest_path))
        if manifest.run_id != run_id or manifest.run_type is not RunType.FULL_DAY:
            raise ValueError("run manifest is not the requested full-day identity")
        if manifest_path.read_text(encoding="utf-8") != manifest.to_toml():
            raise ValueError("full-day manifest is not canonical TOML")
        return manifest

    def _artifact_bytes(
        self, directory: Path, reference: ArtifactReference
    ) -> bytes:
        path = self._safe_file(directory, reference.relative_path)
        raw = path.read_bytes()
        if _sha256_bytes(raw) != reference.sha256:
            raise ValueError(f"artifact digest mismatch: {reference.name}")
        return raw

    @staticmethod
    def _write_file(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directories(directory: Path) -> None:
        directories = [directory, *(path for path in directory.rglob("*") if path.is_dir())]
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _validate_directory_bundle(
        directory: Path,
        manifest: RunManifest,
        artifact_payloads: Mapping[str, bytes] | None = None,
    ) -> None:
        expected_files = {"manifest.toml", *(item.relative_path for item in manifest.artifacts)}
        actual_files: set[str] = set()
        for path in directory.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("immutable run bundle contains a symlink")
            if stat.S_ISREG(metadata.st_mode):
                actual_files.add(path.relative_to(directory).as_posix())
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("immutable run bundle contains a special filesystem node")
        if actual_files != expected_files:
            raise ValueError(
                "immutable run file inventory mismatch "
                f"missing={sorted(expected_files - actual_files)} "
                f"extra={sorted(actual_files - expected_files)}"
            )
        if (directory / "manifest.toml").read_text(encoding="utf-8") != manifest.to_toml():
            raise ValueError("staged manifest bytes are not canonical")
        for reference in manifest.artifacts:
            raw = (directory / reference.relative_path).read_bytes()
            if _sha256_bytes(raw) != reference.sha256:
                raise ValueError(f"staged artifact digest mismatch: {reference.name}")
            if artifact_payloads is not None and raw != artifact_payloads[reference.relative_path]:
                raise ValueError(f"staged artifact bytes changed: {reference.name}")

    def _activate_bundle(
        self,
        manifest: RunManifest,
        artifact_payloads: Mapping[str, bytes],
    ) -> RunManifest:
        expected_paths = {item.relative_path for item in manifest.artifacts}
        if set(artifact_payloads) != expected_paths:
            raise ValueError("artifact byte inventory differs from the manifest")
        if any(
            _sha256_bytes(artifact_payloads[item.relative_path]) != item.sha256
            for item in manifest.artifacts
        ):
            raise ValueError("artifact bytes differ from their declared digests")
        self.paths.ensure((DataAreaId.RUNS, DataAreaId.STAGING))
        self.paths.validate((DataAreaId.RUNS, DataAreaId.STAGING))
        target = self.run_directory(manifest.run_id)
        if target.exists():
            existing = self.load_manifest(manifest.run_id)
            if (
                existing.identity_dict() != manifest.identity_dict()
                or existing.artifacts != manifest.artifacts
            ):
                raise RuntimeError("content-derived run ID collides with different immutable bytes")
            report = self.verify_day(manifest.run_id)
            if not report.passed:
                raise RuntimeError("existing immutable full-day run failed verification")
            return existing

        with tempfile.TemporaryDirectory(
            dir=self.paths.staging,
            prefix="full-day-",
        ) as temporary:
            temporary_root = Path(temporary)
            stage = temporary_root / manifest.run_id
            stage.mkdir(mode=0o700)
            for relative_path in sorted(artifact_payloads):
                self._write_file(stage / relative_path, artifact_payloads[relative_path])
            self._write_file(
                stage / "manifest.toml",
                manifest.to_toml().encode("utf-8"),
            )
            self._validate_directory_bundle(stage, manifest, artifact_payloads)
            self._fsync_directories(stage)

            self.paths.validate((DataAreaId.RUNS, DataAreaId.STAGING))
            if target.exists():
                raise RuntimeError("run target appeared during atomic activation")
            source_descriptor = os.open(
                temporary_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            runs_descriptor = os.open(
                self.paths.runs,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.rename(
                    stage.name,
                    manifest.run_id,
                    src_dir_fd=source_descriptor,
                    dst_dir_fd=runs_descriptor,
                )
                os.fsync(runs_descriptor)
            finally:
                os.close(runs_descriptor)
                os.close(source_descriptor)

        report = self.verify_day(manifest.run_id)
        if not report.passed:
            raise RuntimeError(
                "atomically activated full-day run failed verification: "
                + "; ".join(report.failures)
            )
        return self.load_manifest(manifest.run_id)

    @staticmethod
    def _current_cut(runtime: FullDayRuntime) -> QuiescentCutV1:
        latest = runtime.latest_quiescent_cut
        if latest is not None and latest.simulation_time_us == runtime.clock.current_time_us:
            latest.validate_quiescent()
            return latest
        return _synthetic_quiescent_cut(runtime)

    @staticmethod
    def _checkpoint_entry(
        runtime: FullDayRuntime,
        cut: QuiescentCutV1,
        reference: ArtifactReference,
    ) -> FullDayCheckpointIndexEntryV1:
        events = runtime.events
        last_event = None if not events else events[-1]
        last_key = (
            None
            if last_event is None
            else (
                last_event.simulation_time_us,
                last_event.microstep,
                int(last_event.stage),
                last_event.source_component_id,
                last_event.component_local_sequence,
            )
        )
        state = runtime.checkpoint_state()
        return FullDayCheckpointIndexEntryV1(
            schema_version=1,
            cut=cut,
            last_global_key=last_key,
            checkpoint_semantic_sha256=runtime.state_sha256(),
            artifact=reference,
            engine_id="KIRBY2",
            engine_version=__version__,
            runtime_implementation_version=_exact_int(
                state["implementation_version"], "runtime implementation version", minimum=1
            ),
            python_implementation=platform.python_implementation(),
            python_major=os.sys.version_info.major,
            python_minor=os.sys.version_info.minor,
        )

    def generate_day(
        self,
        plan: FullDayPlanV1,
        runtime: FullDayRuntime,
        *,
        repository: Path | None = None,
    ) -> RunManifest:
        """Run, checkpoint, summarize, and atomically persist one complete day."""

        if type(plan) is not FullDayPlanV1 or type(runtime) is not FullDayRuntime:
            raise TypeError("generate_day requires exact plan and runtime contracts")
        if (
            runtime.plan != plan
            or runtime.clock.current_time_us != 0
            or runtime.events
            or runtime.native_event_ledger
            or runtime.engine.events
            or runtime.engine.orders
        ):
            raise ValueError("generate_day requires a fresh runtime for the exact plan")
        runtime.assert_invariants()
        end_time_us = plan.calendar.end_time_us
        capture_times = tuple(sorted({0, *plan.resolved_checkpoint_times_us, end_time_us}))
        payloads: dict[str, bytes] = {}
        references: list[ArtifactReference] = []
        checkpoint_entries: list[FullDayCheckpointIndexEntryV1] = []

        plan_bytes = plan.to_json_bytes()
        plan_reference = _reference(
            name="full-day-plan",
            relative_path="plan.json",
            payload=plan_bytes,
            schema_version=FULL_DAY_PLAN_SCHEMA_VERSION,
            media_type=FULL_DAY_PLAN_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_PLAN,
        )
        payloads[plan_reference.relative_path] = plan_bytes
        references.append(plan_reference)

        for ordinal, cut_time_us in enumerate(capture_times):
            runtime.advance_to(cut_time_us)
            cut = self._current_cut(runtime)
            if cut.simulation_time_us != cut_time_us:
                raise RuntimeError("runtime checkpoint cut differs from requested capture time")
            checkpoint_bytes = runtime.canonical_state_bytes()
            restored = FullDayRuntime.from_canonical_state_bytes(checkpoint_bytes)
            if restored.canonical_state_bytes() != checkpoint_bytes:
                raise RuntimeError("runtime checkpoint is not a canonical restore fixed point")
            relative_path = f"checkpoints/{ordinal:06d}-{cut_time_us:020d}.json"
            checkpoint_reference = _reference(
                name=f"full-day-checkpoint-{ordinal:06d}",
                relative_path=relative_path,
                payload=checkpoint_bytes,
                schema_version=FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION,
                media_type=FULL_DAY_RUNTIME_STATE_MEDIA_TYPE,
                artifact_type=ArtifactType.FULL_DAY_CHECKPOINT,
            )
            payloads[relative_path] = checkpoint_bytes
            references.append(checkpoint_reference)
            checkpoint_entries.append(
                self._checkpoint_entry(runtime, cut, checkpoint_reference)
            )

        runtime.assert_invariants()
        if runtime.clock.current_time_us != end_time_us or runtime.pending_work:
            raise RuntimeError("complete full-day generation did not close its work queue")
        events = runtime.events
        native_entries = tuple(runtime.native_event_ledger.values())
        outer_bytes = canonical_json_bytes(
            {
                "events": [event.as_dict() for event in events],
                "schema_version": FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
            }
        )
        outer_reference = _reference(
            name="full-day-outer-event-ledger",
            relative_path="ledgers/outer-events.json",
            payload=outer_bytes,
            schema_version=FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
            media_type=FULL_DAY_OUTER_LEDGER_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER,
            row_count=len(events),
        )
        payloads[outer_reference.relative_path] = outer_bytes
        references.append(outer_reference)

        groups: dict[tuple[str, str], list[NativeLedgerEntryV1]] = {}
        for entry in native_entries:
            groups.setdefault(
                (
                    entry.reference.owner_component_id,
                    entry.reference.native_ledger_id,
                ),
                [],
            ).append(entry)
        for ordinal, ((owner, ledger_id), rows) in enumerate(sorted(groups.items())):
            ordered_rows = tuple(sorted(rows, key=lambda item: item.reference.local_sequence))
            subsystem_bytes = canonical_json_bytes(
                {
                    "entries": [item.as_dict() for item in ordered_rows],
                    "native_ledger_id": ledger_id,
                    "owner_component_id": owner,
                    "schema_version": FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
                }
            )
            relative_path = f"ledgers/subsystems/{ordinal:04d}-{owner}.json"
            reference = _reference(
                name=f"full-day-subsystem-ledger-{owner}",
                relative_path=relative_path,
                payload=subsystem_bytes,
                schema_version=FULL_DAY_LEDGER_ARTIFACT_SCHEMA_VERSION,
                media_type=FULL_DAY_SUBSYSTEM_LEDGER_MEDIA_TYPE,
                artifact_type=ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER,
                row_count=len(ordered_rows),
            )
            payloads[relative_path] = subsystem_bytes
            references.append(reference)

        checkpoint_index = FullDayCheckpointIndexV1(
            schema_version=1,
            semantic_plan_sha256=plan.semantic_sha256,
            entries=tuple(checkpoint_entries),
        )
        checkpoint_index_bytes = checkpoint_index.canonical_bytes()
        checkpoint_index_reference = _reference(
            name="full-day-checkpoint-index",
            relative_path="checkpoints/index.json",
            payload=checkpoint_index_bytes,
            schema_version=FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION,
            media_type=FULL_DAY_CHECKPOINT_INDEX_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_CHECKPOINT_INDEX,
            row_count=len(checkpoint_entries),
        )
        payloads[checkpoint_index_reference.relative_path] = checkpoint_index_bytes
        references.append(checkpoint_index_reference)

        summary = summarize_full_day(plan, events, native_entries)
        summary_bytes = summary.canonical_bytes()
        summary_reference = _reference(
            name="full-day-summary",
            relative_path="summary.json",
            payload=summary_bytes,
            schema_version=DAY_SUMMARY_SCHEMA_VERSION,
            media_type=FULL_DAY_SUMMARY_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_SUMMARY,
        )
        payloads[summary_reference.relative_path] = summary_bytes
        references.append(summary_reference)

        qualification_bytes = canonical_json_bytes(
            {
                "automated_disposition": "NOT_EXERCISED",
                "behavioral_envelope": "NOT_EXERCISED",
                "engineering_status": "PASS",
                "human_review": "PENDING",
                "schema_version": FULL_DAY_QUALIFICATION_SCHEMA_VERSION,
            }
        )
        qualification_reference = _reference(
            name="full-day-qualification",
            relative_path="qualification.json",
            payload=qualification_bytes,
            schema_version=FULL_DAY_QUALIFICATION_SCHEMA_VERSION,
            media_type=FULL_DAY_QUALIFICATION_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_QUALIFICATION,
        )
        payloads[qualification_reference.relative_path] = qualification_bytes
        references.append(qualification_reference)

        diagnostics_bytes = canonical_json_bytes(
            {
                "checkpoint_count": len(checkpoint_entries),
                "event_count": len(events),
                "native_event_count": len(native_entries),
                "operational_measurements_excluded_from_replay_identity": True,
                "schema_version": FULL_DAY_DIAGNOSTICS_SCHEMA_VERSION,
                "subsystem_ledger_count": len(groups),
            }
        )
        diagnostics_reference = _reference(
            name="full-day-diagnostics",
            relative_path="diagnostics.json",
            payload=diagnostics_bytes,
            schema_version=FULL_DAY_DIAGNOSTICS_SCHEMA_VERSION,
            media_type=FULL_DAY_DIAGNOSTICS_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_DIAGNOSTICS,
        )
        payloads[diagnostics_reference.relative_path] = diagnostics_bytes
        references.append(diagnostics_reference)

        identity_references = tuple(
            sorted(
                (
                    item
                    for item in references
                    if item.artifact_type is not ArtifactType.FULL_DAY_DIAGNOSTICS
                ),
                key=lambda item: (item.artifact_type.value, item.name),
            )
        )
        evidence_digest = canonical_sha256(
            {
                "artifacts": [
                    {
                        "artifact_type": item.artifact_type.value,
                        "sha256": item.sha256,
                    }
                    for item in identity_references
                ]
            }
        )
        result_digest = canonical_sha256(runtime.result_projection())
        repository_path = repository or Path(__file__).resolve().parents[2]
        manifest = RunManifest.create(
            parent_run_id=None,
            run_type=RunType.FULL_DAY,
            scenario_id=plan.plan_id,
            lesson_id=None,
            seed=plan.seed_policy.root_seed,
            flow_model=plan.composition_profile.reference_id,
            market_profile=plan.market_profile.reference_id,
            strategy_id=(
                "FEATURE_STRATEGY_PLAYER_V1"
                if "FEATURE_STRATEGY_PLAYER_V1" in plan.selected_component_ids
                else "NONE"
            ),
            hotkey_layout_id="NONE",
            session_objective="FULL_DAY_GENERATION",
            simulation_start_us=0,
            simulation_end_us=end_time_us,
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions={
                "day_summary": DAY_SUMMARY_SCHEMA_VERSION,
                "full_day_checkpoint_index": FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION,
                "full_day_plan": FULL_DAY_PLAN_SCHEMA_VERSION,
                "full_day_runtime_state": FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION,
                "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
            },
            input_dataset_references=(f"full-day-plan:{plan.semantic_sha256}",),
            configuration_digest=plan.semantic_sha256,
            evidence_digest=evidence_digest,
            result_digest=result_digest,
            creation_timestamp_utc=_utc_now(),
            artifacts=tuple(
                sorted(references, key=lambda item: (item.artifact_type.value, item.name))
            ),
        )
        return self._activate_bundle(manifest, payloads)

    @staticmethod
    def _references_by_type(
        manifest: RunManifest,
    ) -> dict[ArtifactType, tuple[ArtifactReference, ...]]:
        return {
            artifact_type: tuple(
                item
                for item in manifest.artifacts
                if item.artifact_type is artifact_type
            )
            for artifact_type in ArtifactType
        }

    def _load_bundle_bytes(
        self, manifest: RunManifest
    ) -> tuple[Path, dict[str, bytes]]:
        directory = self._safe_existing_run_directory(manifest.run_id)
        self._validate_directory_bundle(directory, manifest)
        for reference in manifest.artifacts:
            contract = _ARTIFACT_CONTRACTS.get(reference.artifact_type)
            if contract != (reference.schema_version, reference.media_type):
                raise ValueError(
                    f"typed artifact contract mismatch: {reference.name}"
                )
            if (reference.artifact_type in _ROW_COUNT_ARTIFACT_TYPES) != (
                reference.row_count is not None
            ):
                raise ValueError(
                    f"typed artifact row-count contract mismatch: {reference.name}"
                )
        payloads = {
            reference.relative_path: self._artifact_bytes(directory, reference)
            for reference in manifest.artifacts
        }
        for reference in manifest.artifacts:
            if reference.row_count is None:
                continue
            payload = _canonical_object(payloads[reference.relative_path], reference.name)
            row_field = {
                ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER: "events",
                ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER: "entries",
                ArtifactType.FULL_DAY_CHECKPOINT_INDEX: "entries",
            }.get(reference.artifact_type)
            if row_field is None:
                raise ValueError("row-count-bearing full-day artifact has no row contract")
            rows = payload.get(row_field)
            if type(rows) is not list or len(rows) != reference.row_count:
                raise ValueError(f"artifact row count mismatch: {reference.name}")
        return directory, payloads

    @staticmethod
    def _one_reference(
        by_type: Mapping[ArtifactType, tuple[ArtifactReference, ...]],
        artifact_type: ArtifactType,
    ) -> ArtifactReference:
        rows = by_type[artifact_type]
        if len(rows) != 1:
            raise ValueError(
                f"full-day run requires exactly one {artifact_type.value} artifact"
            )
        return rows[0]

    @staticmethod
    def _load_outer_events(raw: bytes) -> tuple[FullDayEventV1, ...]:
        payload = _canonical_object(raw, "outer event ledger")
        if set(payload) != {"events", "schema_version"} or payload["schema_version"] != 1:
            raise ValueError("outer event ledger fields differ from schema v1")
        rows = payload["events"]
        if type(rows) is not list or any(not isinstance(item, Mapping) for item in rows):
            raise TypeError("outer event ledger rows must be an array of objects")
        events = tuple(FullDayEventV1.from_dict(item) for item in rows)
        if canonical_json_bytes(
            {"events": [event.as_dict() for event in events], "schema_version": 1}
        ) != raw:
            raise ValueError("outer event ledger is not a canonical typed round trip")
        return events

    @staticmethod
    def _load_native_entries(
        references: Sequence[ArtifactReference],
        payloads: Mapping[str, bytes],
    ) -> tuple[NativeLedgerEntryV1, ...]:
        entries: list[NativeLedgerEntryV1] = []
        keys: set[tuple[str, str, str]] = set()
        for reference in references:
            payload = _canonical_object(
                payloads[reference.relative_path], reference.name
            )
            if set(payload) != {
                "entries",
                "native_ledger_id",
                "owner_component_id",
                "schema_version",
            } or payload["schema_version"] != 1:
                raise ValueError("subsystem ledger fields differ from schema v1")
            rows = payload["entries"]
            if type(rows) is not list or any(
                not isinstance(item, Mapping) for item in rows
            ):
                raise TypeError("subsystem ledger entries must be an array of objects")
            loaded = tuple(NativeLedgerEntryV1.from_dict(item) for item in rows)
            if tuple(item.reference.local_sequence for item in loaded) != tuple(
                sorted(item.reference.local_sequence for item in loaded)
            ):
                raise ValueError("subsystem ledger local sequences are not ordered")
            for item in loaded:
                if (
                    item.reference.owner_component_id != payload["owner_component_id"]
                    or item.reference.native_ledger_id != payload["native_ledger_id"]
                    or item.ledger_key in keys
                ):
                    raise ValueError("subsystem ledger identity is inconsistent or duplicated")
                keys.add(item.ledger_key)
            entries.extend(loaded)
        return tuple(entries)

    @staticmethod
    def _checkpoint_last_key(runtime: FullDayRuntime) -> tuple[object, ...] | None:
        if not runtime.events:
            return None
        event = runtime.events[-1]
        return (
            event.simulation_time_us,
            event.microstep,
            int(event.stage),
            event.source_component_id,
            event.component_local_sequence,
        )

    def _load_complete_run(
        self,
        manifest: RunManifest,
        *,
        bundle: tuple[Path, Mapping[str, bytes]] | None = None,
    ) -> VerifiedFullDayRunV1:
        if manifest.session_objective != "FULL_DAY_GENERATION" or manifest.parent_run_id is not None:
            raise ValueError("run is not a complete generated full day")
        if manifest.schema_version != RUN_MANIFEST_SCHEMA_VERSION or manifest.schema_versions != {
            "day_summary": DAY_SUMMARY_SCHEMA_VERSION,
            "full_day_checkpoint_index": FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION,
            "full_day_plan": FULL_DAY_PLAN_SCHEMA_VERSION,
            "full_day_runtime_state": FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION,
            "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
        }:
            raise ValueError("complete full-day manifest schema inventory is unsupported")
        _directory, payloads = (
            self._load_bundle_bytes(manifest) if bundle is None else bundle
        )
        by_type = self._references_by_type(manifest)
        required_singletons = (
            ArtifactType.FULL_DAY_PLAN,
            ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER,
            ArtifactType.FULL_DAY_CHECKPOINT_INDEX,
            ArtifactType.FULL_DAY_SUMMARY,
            ArtifactType.FULL_DAY_QUALIFICATION,
            ArtifactType.FULL_DAY_DIAGNOSTICS,
        )
        singleton_references = {
            artifact_type: self._one_reference(by_type, artifact_type)
            for artifact_type in required_singletons
        }
        if not by_type[ArtifactType.FULL_DAY_CHECKPOINT]:
            raise ValueError("full-day run has no checkpoint artifacts")
        if not by_type[ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER]:
            raise ValueError("full-day run has no subsystem ledgers")
        allowed_types = set(required_singletons) | {
            ArtifactType.FULL_DAY_CHECKPOINT,
            ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER,
        }
        if any(
            item.artifact_type not in allowed_types for item in manifest.artifacts
        ):
            raise ValueError("complete full-day run contains an unsupported artifact role")

        plan_raw = payloads[
            singleton_references[ArtifactType.FULL_DAY_PLAN].relative_path
        ]
        plan = FullDayPlanV1.from_json_bytes(plan_raw)
        if plan.to_json_bytes() != plan_raw or plan.semantic_sha256 != manifest.configuration_digest:
            raise ValueError("stored full-day plan differs from manifest configuration identity")
        expected_strategy_id = (
            "FEATURE_STRATEGY_PLAYER_V1"
            if "FEATURE_STRATEGY_PLAYER_V1" in plan.selected_component_ids
            else "NONE"
        )
        if (
            manifest.scenario_id != plan.plan_id
            or manifest.lesson_id is not None
            or manifest.seed != plan.seed_policy.root_seed
            or manifest.flow_model != plan.composition_profile.reference_id
            or manifest.market_profile != plan.market_profile.reference_id
            or manifest.strategy_id != expected_strategy_id
            or manifest.hotkey_layout_id != "NONE"
            or manifest.simulation_start_us != 0
            or manifest.simulation_end_us != plan.calendar.end_time_us
            or manifest.input_dataset_references
            != (f"full-day-plan:{plan.semantic_sha256}",)
        ):
            raise ValueError("full-day manifest metadata differs from its exact plan")
        events = self._load_outer_events(
            payloads[
                singleton_references[
                    ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER
                ].relative_path
            ]
        )
        native_entries = self._load_native_entries(
            by_type[ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER], payloads
        )
        index = FullDayCheckpointIndexV1.from_json_bytes(
            payloads[
                singleton_references[
                    ArtifactType.FULL_DAY_CHECKPOINT_INDEX
                ].relative_path
            ]
        )
        if index.semantic_plan_sha256 != plan.semantic_sha256:
            raise ValueError("checkpoint index is bound to another semantic plan")
        if index.entries[0].cut_time_us != 0:
            raise ValueError("checkpoint index omits the explicit initialization cut")
        checkpoint_manifest_by_path = {
            item.relative_path: item
            for item in by_type[ArtifactType.FULL_DAY_CHECKPOINT]
        }
        if set(checkpoint_manifest_by_path) != {
            item.artifact.relative_path for item in index.entries
        }:
            raise ValueError("checkpoint index and manifest inventories differ")

        stored_native = {entry.ledger_key: entry for entry in native_entries}
        restored_by_entry: list[tuple[FullDayCheckpointIndexEntryV1, FullDayRuntime]] = []
        for entry in index.entries:
            manifest_reference = checkpoint_manifest_by_path.get(
                entry.artifact.relative_path
            )
            if manifest_reference != entry.artifact:
                raise ValueError("checkpoint index typed reference differs from manifest")
            raw = payloads[entry.artifact.relative_path]
            runtime = FullDayRuntime.from_canonical_state_bytes(raw)
            if runtime.plan != plan or runtime.clock.current_time_us != entry.cut_time_us:
                raise ValueError("checkpoint runtime plan/time differs from its index entry")
            if _sha256_bytes(raw) != entry.checkpoint_semantic_sha256:
                raise ValueError("checkpoint semantic digest differs after restore")
            if len(runtime.events) != entry.cut.last_global_event_sequence:
                raise ValueError("checkpoint event sequence differs from its quiescent cut")
            if canonical_event_prefix_sha256(runtime.events) != entry.cut.event_prefix_sha256:
                raise ValueError("checkpoint event prefix differs from its quiescent cut")
            if tuple(event.as_dict() for event in runtime.events) != tuple(
                event.as_dict() for event in events[: len(runtime.events)]
            ):
                raise ValueError("checkpoint outer prefix differs from stored canonical ledger")
            if self._checkpoint_last_key(runtime) != entry.last_global_key:
                raise ValueError("checkpoint last global key differs from restored ledger")
            expected_native_keys = {
                event.payload.native_event.ledger_key
                for event in events[: len(runtime.events)]
                if event.payload.native_event is not None
            }
            restored_native = runtime.native_event_ledger
            if set(restored_native) != expected_native_keys or any(
                restored_native[key].as_dict() != stored_native[key].as_dict()
                for key in restored_native
            ):
                raise ValueError(
                    "checkpoint subsystem prefix differs from stored native ledgers"
                )
            if (
                entry.runtime_implementation_version
                != FULL_DAY_RUNTIME_IMPLEMENTATION_VERSION
            ):
                raise ValueError("checkpoint runtime implementation metadata differs")
            restored_by_entry.append((entry, runtime))

        final_entry, final_runtime = restored_by_entry[-1]
        if (
            final_entry.cut_time_us != plan.calendar.end_time_us
            or final_runtime.clock.current_time_us != plan.calendar.end_time_us
            or final_runtime.pending_work
        ):
            raise ValueError("last checkpoint is not a complete closed day")
        if tuple(event.as_dict() for event in final_runtime.events) != tuple(
            event.as_dict() for event in events
        ):
            raise ValueError("final checkpoint does not reproduce the complete outer ledger")
        final_native = final_runtime.native_event_ledger
        if set(final_native) != set(stored_native) or any(
            final_native[key].as_dict() != stored_native[key].as_dict()
            for key in final_native
        ):
            raise ValueError("final checkpoint does not reproduce subsystem ledgers")
        if canonical_sha256(final_runtime.result_projection()) != manifest.result_digest:
            raise ValueError("final runtime result digest differs from the manifest")

        summary_raw = payloads[
            singleton_references[ArtifactType.FULL_DAY_SUMMARY].relative_path
        ]
        summary = DaySummaryV1.from_json_bytes(summary_raw)
        expected_summary = summarize_full_day(plan, events, native_entries)
        if summary.canonical_bytes() != expected_summary.canonical_bytes():
            raise ValueError("stored day summary differs from exact ledger derivation")

        qualification = _canonical_object(
            payloads[
                singleton_references[
                    ArtifactType.FULL_DAY_QUALIFICATION
                ].relative_path
            ],
            "qualification",
        )
        diagnostics = _canonical_object(
            payloads[
                singleton_references[
                    ArtifactType.FULL_DAY_DIAGNOSTICS
                ].relative_path
            ],
            "diagnostics",
        )
        expected_qualification = {
            "automated_disposition": "NOT_EXERCISED",
            "behavioral_envelope": "NOT_EXERCISED",
            "engineering_status": "PASS",
            "human_review": "PENDING",
            "schema_version": FULL_DAY_QUALIFICATION_SCHEMA_VERSION,
        }
        if qualification != expected_qualification:
            raise ValueError("full-day qualification payload differs from schema v1")
        expected_diagnostics = {
            "checkpoint_count": len(index.entries),
            "event_count": len(events),
            "native_event_count": len(native_entries),
            "operational_measurements_excluded_from_replay_identity": True,
            "schema_version": FULL_DAY_DIAGNOSTICS_SCHEMA_VERSION,
            "subsystem_ledger_count": len(
                by_type[ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER]
            ),
        }
        if diagnostics != expected_diagnostics:
            raise ValueError("full-day diagnostics payload differs from exact counts")

        identity_references = tuple(
            sorted(
                (
                    item
                    for item in manifest.artifacts
                    if item.artifact_type is not ArtifactType.FULL_DAY_DIAGNOSTICS
                ),
                key=lambda item: (item.artifact_type.value, item.name),
            )
        )
        evidence_digest = canonical_sha256(
            {
                "artifacts": [
                    {
                        "artifact_type": item.artifact_type.value,
                        "sha256": item.sha256,
                    }
                    for item in identity_references
                ]
            }
        )
        if evidence_digest != manifest.evidence_digest:
            raise ValueError("full-day evidence digest differs from typed artifact inventory")
        return VerifiedFullDayRunV1(
            manifest=manifest,
            plan=plan,
            events=events,
            native_entries=native_entries,
            checkpoint_index=index,
            summary=summary,
        )

    def _load_window_run(
        self,
        manifest: RunManifest,
        *,
        bundle: tuple[Path, Mapping[str, bytes]] | None = None,
    ) -> FullDayWindowV1:
        if manifest.session_objective != "FULL_DAY_WINDOW_EXTRACTION" or manifest.parent_run_id is None:
            raise ValueError("run is not an extracted full-day window")
        if manifest.schema_version != RUN_MANIFEST_SCHEMA_VERSION or manifest.schema_versions != {
            "full_day_window": FULL_DAY_WINDOW_SCHEMA_VERSION,
            "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
        }:
            raise ValueError("full-day window manifest schema inventory is unsupported")
        _directory, payloads = (
            self._load_bundle_bytes(manifest) if bundle is None else bundle
        )
        by_type = self._references_by_type(manifest)
        reference = self._one_reference(by_type, ArtifactType.FULL_DAY_WINDOW)
        if len(manifest.artifacts) != 1:
            raise ValueError("window child run contains artifacts outside its typed payload")
        window = FullDayWindowV1.from_json_bytes(payloads[reference.relative_path])
        if window.parent_run_id != manifest.parent_run_id:
            raise ValueError("window child manifest and payload name different parents")
        if window.semantic_sha256 != manifest.result_digest:
            raise ValueError("window semantic digest differs from child result identity")
        if manifest.evidence_digest != reference.sha256:
            raise ValueError("window child evidence digest differs from exact artifact bytes")
        parent = self._load_complete_run(self.load_manifest(window.parent_run_id))
        expected_configuration_digest = canonical_sha256(
            {
                "end_time_us": window.end_time_us,
                "parent_run_id": window.parent_run_id,
                "reveal_policy": window.reveal_policy,
                "start_time_us": window.start_time_us,
            }
        )
        if (
            manifest.seed is not None
            or manifest.lesson_id is not None
            or manifest.scenario_id != parent.manifest.scenario_id
            or manifest.flow_model != parent.manifest.flow_model
            or manifest.market_profile != parent.manifest.market_profile
            or manifest.strategy_id != parent.manifest.strategy_id
            or manifest.hotkey_layout_id != "NONE"
            or manifest.simulation_start_us != window.start_time_us
            or manifest.simulation_end_us != window.end_time_us
            or manifest.configuration_digest != expected_configuration_digest
            or manifest.input_dataset_references
            != (f"parent-run:{window.parent_run_id}",)
        ):
            raise ValueError("window child manifest metadata differs from its lineage")
        source = next(
            (
                item
                for item in parent.checkpoint_index.entries
                if item.artifact == window.source_checkpoint
            ),
            None,
        )
        if source is None or source.cut_time_us != window.source_checkpoint_cut_time_us:
            raise ValueError("window source checkpoint is absent from its parent")
        if source.cut.event_prefix_sha256 != window.source_event_prefix_sha256:
            raise ValueError("window source event-prefix digest differs from its parent")
        expected_outer = tuple(
            event.as_dict()
            for event in parent.events
            if window.start_time_us <= event.simulation_time_us < window.end_time_us
        )
        if window.outer_events != expected_outer:
            raise ValueError("window outer events differ from the immutable parent slice")
        native_by_key = {entry.ledger_key: entry for entry in parent.native_entries}
        observable_owner_ids = {
            "DELIVERY_ASYNC_V1",
            "ENGINE_MARKET_MECHANICS_V1",
            "FEATURE_STRATEGY_PLAYER_V1",
        }
        expected_native = tuple(
            native_by_key[event.payload.native_event.ledger_key].as_dict()
            for event in parent.events
            if window.start_time_us <= event.simulation_time_us < window.end_time_us
            and event.payload.native_event is not None
            and event.payload.native_event.owner_component_id in observable_owner_ids
        )
        if window.observable_native_entries != expected_native:
            raise ValueError("window observable native rows differ from its parent slice")
        expected_seek = self._seek_loaded(parent, window.start_time_us)
        if expected_seek.source_checkpoint.artifact != window.source_checkpoint:
            raise ValueError("window did not bind the greatest compatible source checkpoint")
        if (
            window.context_event_count
            != expected_seek.uninterrupted_event_count
            or window.context_event_prefix_sha256
            != expected_seek.uninterrupted_event_prefix_sha256
        ):
            raise ValueError("window context prefix differs from replay through its start cut")
        expected_context = self._observable_context(
            parent, expected_seek, window.start_time_us
        )
        if canonical_json_bytes(window.observable_context) != canonical_json_bytes(
            expected_context
        ):
            raise ValueError("window observable context differs from its parent prefix")
        public_bytes = window.canonical_bytes()
        if any(
            token in public_bytes
            for token in (
                b'"checkpoint_state"',
                b'"rng_state"',
                b'"scheduled_events"',
                b'"seed_policy"',
            )
        ):
            raise ValueError("window artifact leaks sealed schedule or RNG state")
        return window

    def verify_day(self, run_id: str) -> FullDayVerificationReportV1:
        flags = {
            "manifest_valid": False,
            "artifact_inventory_valid": False,
            "artifact_digests_valid": False,
            "canonical_payloads_valid": False,
            "checkpoints_valid": False,
            "replay_valid": False,
            "summary_valid": False,
            "privacy_contract_valid": False,
        }
        failures: list[str] = []
        try:
            manifest = self.load_manifest(run_id)
            flags["manifest_valid"] = True
            bundle = self._load_bundle_bytes(manifest)
            flags["artifact_inventory_valid"] = True
            flags["artifact_digests_valid"] = True
            if manifest.session_objective == "FULL_DAY_GENERATION":
                loaded = self._load_complete_run(manifest, bundle=bundle)
                flags["canonical_payloads_valid"] = True
                flags["checkpoints_valid"] = True
                flags["replay_valid"] = True
                flags["summary_valid"] = True
                self._validate_inspection_privacy(loaded)
                flags["privacy_contract_valid"] = True
            elif manifest.session_objective == "FULL_DAY_WINDOW_EXTRACTION":
                self._load_window_run(manifest, bundle=bundle)
                flags["canonical_payloads_valid"] = True
                flags["checkpoints_valid"] = True
                flags["replay_valid"] = True
                flags["summary_valid"] = True
                flags["privacy_contract_valid"] = True
            else:
                raise ValueError("unsupported FULL_DAY session objective")
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            failures.append(str(error))
        return FullDayVerificationReportV1(
            run_id=run_id,
            failures=tuple(failures),
            **flags,
        )

    def _verified_complete(self, run_id: str) -> VerifiedFullDayRunV1:
        try:
            manifest = self.load_manifest(run_id)
            bundle = self._load_bundle_bytes(manifest)
            return self._load_complete_run(manifest, bundle=bundle)
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            raise ValueError(f"full-day run verification failed: {error}") from error

    @staticmethod
    def _inspect_complete(loaded: VerifiedFullDayRunV1) -> dict[str, object]:
        artifacts = []
        for reference in loaded.manifest.artifacts:
            row: dict[str, object] = {
                "artifact_type": reference.artifact_type.value,
                "media_type": reference.media_type,
                "name": reference.name,
                "row_count": reference.row_count,
                "schema_version": reference.schema_version,
                "sealed": reference.artifact_type in _SEALED_ARTIFACT_TYPES,
                "sha256": reference.sha256,
            }
            if reference.artifact_type not in _SEALED_ARTIFACT_TYPES:
                row["relative_path"] = reference.relative_path
            artifacts.append(row)
        return {
            "artifacts": artifacts,
            "checkpoint_cuts": [
                {
                    "cut_time_us": item.cut_time_us,
                    "event_prefix_sha256": item.cut.event_prefix_sha256,
                    "last_global_event_sequence": item.cut.last_global_event_sequence,
                }
                for item in loaded.checkpoint_index.entries
            ],
            "event_count": len(loaded.events),
            "plan": {
                "calendar_end_time_us": loaded.plan.calendar.end_time_us,
                "phase_ids": [phase.phase_id for phase in loaded.plan.calendar.phases],
                "plan_id": loaded.plan.plan_id,
                "plan_version": loaded.plan.plan_version,
                "semantic_sha256": loaded.plan.semantic_sha256,
            },
            "run_id": loaded.manifest.run_id,
            "run_type": loaded.manifest.run_type.value,
            "summary": loaded.summary.as_dict(),
        }

    @classmethod
    def _validate_inspection_privacy(cls, loaded: VerifiedFullDayRunV1) -> None:
        safe = cls._inspect_complete(loaded)
        forbidden = {
            "seed_policy",
            "checkpoint_state",
            "rng_state",
            "scheduled_events",
        }
        if any(key in json.dumps(safe, sort_keys=True) for key in forbidden):
            raise ValueError("ordinary inspection leaks sealed full-day state")

    @staticmethod
    def _passing_verification(run_id: str) -> FullDayVerificationReportV1:
        return FullDayVerificationReportV1(
            run_id=run_id,
            manifest_valid=True,
            artifact_inventory_valid=True,
            artifact_digests_valid=True,
            canonical_payloads_valid=True,
            checkpoints_valid=True,
            replay_valid=True,
            summary_valid=True,
            privacy_contract_valid=True,
            failures=(),
        )

    def _open_verified_complete(
        self,
        manifest: RunManifest,
    ) -> VerifiedFullDaySessionV1:
        bundle = self._load_bundle_bytes(manifest)
        loaded = self._load_complete_run(manifest, bundle=bundle)
        self._validate_inspection_privacy(loaded)
        return VerifiedFullDaySessionV1(
            _store=self,
            _loaded=loaded,
            verification=self._passing_verification(manifest.run_id),
            _construction_token=_VERIFIED_FULL_DAY_SESSION_TOKEN,
        )

    def open_verified_day(self, run_id: str) -> VerifiedFullDaySessionV1:
        """Load and deeply verify one complete day for related projections."""

        try:
            return self._open_verified_complete(self.load_manifest(run_id))
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            raise ValueError(f"full-day run verification failed: {error}") from error

    def inspect_day(self, run_id: str) -> dict[str, object]:
        manifest = self.load_manifest(run_id)
        try:
            if manifest.session_objective == "FULL_DAY_GENERATION":
                return self._open_verified_complete(manifest).inspection()
            if manifest.session_objective == "FULL_DAY_WINDOW_EXTRACTION":
                bundle = self._load_bundle_bytes(manifest)
                window = self._load_window_run(manifest, bundle=bundle)
                payload: dict[str, object] = {
                    "end_time_us": window.end_time_us,
                    "event_count": len(window.outer_events),
                    "parent_run_id": window.parent_run_id,
                    "reveal_policy": window.reveal_policy,
                    "run_id": manifest.run_id,
                    "run_type": manifest.run_type.value,
                    "start_time_us": window.start_time_us,
                }
                payload["verification"] = self._passing_verification(
                    manifest.run_id
                ).as_dict()
                return payload
            raise ValueError("unsupported FULL_DAY session objective")
        except (OSError, TypeError, ValueError, RuntimeError, KeyError) as error:
            raise ValueError("full-day run failed inspection verification") from error

    def _seek_loaded(
        self,
        loaded: VerifiedFullDayRunV1,
        target_time_us: int,
    ) -> FullDaySeekResultV1:
        """Apply the public seek algorithm to an already verified immutable day."""

        run_id = loaded.manifest.run_id
        target = _exact_int(target_time_us, "seek target")
        if target > loaded.plan.calendar.end_time_us:
            raise ValueError("seek target lies beyond the full-day calendar")
        compatible = tuple(
            item
            for item in loaded.checkpoint_index.entries
            if item.is_compatible and item.cut_time_us <= target
        )
        if not compatible:
            raise ValueError("no compatible initialization checkpoint precedes seek target")
        source = compatible[-1]
        directory = self._safe_existing_run_directory(run_id)
        raw = self._artifact_bytes(directory, source.artifact)
        runtime = FullDayRuntime.from_canonical_state_bytes(raw)
        runtime.advance_to(target)
        cut = _synthetic_quiescent_cut(runtime)
        expected_events = tuple(
            event for event in loaded.events if event.simulation_time_us <= target
        )
        if tuple(event.as_dict() for event in runtime.events) != tuple(
            event.as_dict() for event in expected_events
        ):
            raise ValueError("sought runtime ledger differs from uninterrupted parent prefix")
        expected_native_keys = {
            event.payload.native_event.ledger_key
            for event in expected_events
            if event.payload.native_event is not None
        }
        runtime_native = runtime.native_event_ledger
        stored_native = {entry.ledger_key: entry for entry in loaded.native_entries}
        if set(runtime_native) != expected_native_keys or any(
            runtime_native[key].as_dict() != stored_native[key].as_dict()
            for key in runtime_native
        ):
            raise ValueError("sought runtime subsystem prefix differs from its parent")
        return FullDaySeekResultV1(
            run_id=run_id,
            target_time_us=target,
            source_checkpoint=source,
            quiescent_cut=cut,
            runtime=runtime,
            uninterrupted_event_count=len(expected_events),
            uninterrupted_event_prefix_sha256=canonical_event_prefix_sha256(
                expected_events
            ),
        )

    def seek(self, run_id: str, target_time_us: int) -> FullDaySeekResultV1:
        return self._seek_loaded(self._verified_complete(run_id), target_time_us)

    @staticmethod
    def _observable_context(
        loaded: VerifiedFullDayRunV1,
        seek_result: FullDaySeekResultV1,
        start_time_us: int,
    ) -> dict[str, object]:
        native_by_key = {entry.ledger_key: entry for entry in loaded.native_entries}
        prior_trades: list[Mapping[str, object]] = []
        last_market_state: Mapping[str, object] | None = None
        for event in loaded.events:
            if event.simulation_time_us > start_time_us:
                break
            native_reference = event.payload.native_event
            if native_reference is None:
                continue
            entry = native_by_key[native_reference.ledger_key]
            if (
                entry.reference.owner_component_id == "ENGINE_MARKET_MECHANICS_V1"
                and entry.reference.event_type in {"TRADE", "AUCTION_FILL"}
            ):
                data = entry.payload.get("data")
                if isinstance(data, Mapping):
                    prior_trades.append(data)
            if entry.reference.event_type == "CLIENT_MESSAGE_DELIVERED":
                payload = entry.payload
                client = payload.get("client_payload")
                market = None if not isinstance(client, Mapping) else client.get("market_state")
                if payload.get("kind") == "MARKET_STATE" and isinstance(market, Mapping):
                    last_market_state = market

        runtime = seek_result.runtime
        active_participants = tuple(
            sorted(
                participant_id
                for participant_id, active in runtime.participant_runtime.active.items()
                if active
            )
        )
        inventories: dict[str, int] | str = "UNAVAILABLE"
        if runtime.agent_scheduler is not None:
            scheduler_state = runtime.agent_scheduler.checkpoint_state().get("state")
            agents = None if not isinstance(scheduler_state, Mapping) else scheduler_state.get("agents")
            if isinstance(agents, Mapping):
                projected: dict[str, int] = {}
                for participant_id, agent in agents.items():
                    if not isinstance(agent, Mapping) or type(agent.get("inventory")) is not int:
                        raise ValueError("scheduler inventory projection is malformed")
                    projected[str(participant_id)] = int(agent["inventory"])
                inventories = dict(sorted(projected.items()))
        prices = tuple(
            _exact_int(row.get("price_ticks"), "prior trade price", minimum=1)
            for row in prior_trades
        )
        differences = tuple(
            current - previous for previous, current in zip(prices, prices[1:])
        )
        if differences:
            from math import isqrt

            scale = 1_000_000
            mean_square = (
                sum(value * value for value in differences) * scale**2
                + len(differences) // 2
            ) // len(differences)
            prior_volatility: int | str = isqrt(mean_square)
        else:
            prior_volatility = "UNAVAILABLE"
        market = (
            runtime._delivery_public_market_cut()
            if last_market_state is None
            else dict(last_market_state)
        )
        return {
            "active_participant_ids": list(active_participants),
            "context_cut_policy": "AFTER_ALL_WORK_AT_WINDOW_START",
            "context_includes_window_start": True,
            "market_state": market,
            "participant_inventory_shares": inventories,
            "prior_trade_count": len(prior_trades),
            "prior_traded_volume_shares": sum(
                _exact_int(row.get("quantity"), "prior trade quantity", minimum=1)
                for row in prior_trades
            ),
            "prior_volatility_ticks_fixed": prior_volatility,
            "volatility_formula": (
                "RMS_CONSECUTIVE_EXECUTED_TRADE_TICK_DIFFERENCE_SCALE_1000000"
            ),
        }

    def extract_window(
        self,
        run_id: str,
        start_time_us: int,
        end_time_us: int,
        *,
        reveal_policy: str = "OBSERVABLE_CONTEXT_V1",
        repository: Path | None = None,
    ) -> RunManifest:
        loaded = self._verified_complete(run_id)
        start = _exact_int(start_time_us, "window start")
        end = _exact_int(end_time_us, "window end")
        if end <= start or end > loaded.plan.calendar.end_time_us:
            raise ValueError("window bounds must be a nonempty half-open day interval")
        if reveal_policy not in _WINDOW_POLICY_IDS:
            raise ValueError("window reveal policy is unsupported")
        seek_result = self.seek(run_id, start)
        native_by_key = {entry.ledger_key: entry for entry in loaded.native_entries}
        outer_events = tuple(
            event
            for event in loaded.events
            if start <= event.simulation_time_us < end
        )
        observable_owner_ids = {
            "DELIVERY_ASYNC_V1",
            "ENGINE_MARKET_MECHANICS_V1",
            "FEATURE_STRATEGY_PLAYER_V1",
        }
        observable_native: list[Mapping[str, object]] = []
        for event in outer_events:
            reference = event.payload.native_event
            if reference is None or reference.owner_component_id not in observable_owner_ids:
                continue
            observable_native.append(native_by_key[reference.ledger_key].as_dict())
        source = seek_result.source_checkpoint
        window = FullDayWindowV1(
            schema_version=1,
            parent_run_id=run_id,
            source_checkpoint=source.artifact,
            source_checkpoint_cut_time_us=source.cut_time_us,
            source_event_prefix_sha256=source.cut.event_prefix_sha256,
            context_event_count=seek_result.uninterrupted_event_count,
            context_event_prefix_sha256=(
                seek_result.uninterrupted_event_prefix_sha256
            ),
            start_time_us=start,
            end_time_us=end,
            reveal_policy=reveal_policy,
            observable_context=self._observable_context(loaded, seek_result, start),
            outer_events=tuple(event.as_dict() for event in outer_events),
            observable_native_entries=tuple(observable_native),
        )
        window_bytes = window.canonical_bytes()
        reference = _reference(
            name="full-day-window",
            relative_path="window.json",
            payload=window_bytes,
            schema_version=FULL_DAY_WINDOW_SCHEMA_VERSION,
            media_type=FULL_DAY_WINDOW_MEDIA_TYPE,
            artifact_type=ArtifactType.FULL_DAY_WINDOW,
            row_count=None,
        )
        configuration_digest = canonical_sha256(
            {
                "end_time_us": end,
                "parent_run_id": run_id,
                "reveal_policy": reveal_policy,
                "start_time_us": start,
            }
        )
        repository_path = repository or Path(__file__).resolve().parents[2]
        manifest = RunManifest.create(
            parent_run_id=run_id,
            run_type=RunType.FULL_DAY,
            scenario_id=loaded.manifest.scenario_id,
            lesson_id=None,
            seed=None,
            flow_model=loaded.manifest.flow_model,
            market_profile=loaded.manifest.market_profile,
            strategy_id=loaded.manifest.strategy_id,
            hotkey_layout_id="NONE",
            session_objective="FULL_DAY_WINDOW_EXTRACTION",
            simulation_start_us=start,
            simulation_end_us=end,
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions={
                "full_day_window": FULL_DAY_WINDOW_SCHEMA_VERSION,
                "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
            },
            input_dataset_references=(f"parent-run:{run_id}",),
            configuration_digest=configuration_digest,
            evidence_digest=reference.sha256,
            result_digest=window.semantic_sha256,
            creation_timestamp_utc=_utc_now(),
            artifacts=(reference,),
        )
        return self._activate_bundle(manifest, {reference.relative_path: window_bytes})


__all__ = [
    "FULL_DAY_CHECKPOINT_INDEX_SCHEMA_VERSION",
    "FULL_DAY_RUNTIME_STATE_ARTIFACT_SCHEMA_VERSION",
    "FULL_DAY_WINDOW_SCHEMA_VERSION",
    "FullDayCheckpointIndexEntryV1",
    "FullDayCheckpointIndexV1",
    "FullDaySeekResultV1",
    "FullDayStore",
    "FullDayVerificationReportV1",
    "FullDayWindowV1",
    "VerifiedFullDayRunV1",
    "VerifiedFullDaySessionV1",
]
