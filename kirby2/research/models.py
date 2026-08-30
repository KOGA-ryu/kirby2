"""Versioned immutable run-manifest contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath

from .toml_codec import canonical_digest, canonical_toml


RUN_MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_RUN_MANIFEST_SCHEMA_VERSIONS = frozenset({1, 2})
RUN_CONFIGURATION_SCHEMA_VERSION = 1
TABLE_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")


class RunType(str, Enum):
    SIMULATION = "SIMULATION"
    REPLAY = "REPLAY"
    LESSON = "LESSON"
    CALIBRATION = "CALIBRATION"
    EXPERIMENT = "EXPERIMENT"
    FULL_DAY = "FULL_DAY"
    FULL_DAY_QUALIFICATION = "FULL_DAY_QUALIFICATION"
    LESSON_MINING = "LESSON_MINING"
    LESSON_REVIEW = "LESSON_REVIEW"
    LESSON_BUILD = "LESSON_BUILD"
    LEARNER_UPDATE = "LEARNER_UPDATE"
    INSTRUCTOR_ASSIGNMENT = "INSTRUCTOR_ASSIGNMENT"
    INSTRUCTOR_ATTEMPT = "INSTRUCTOR_ATTEMPT"
    INSTRUCTOR_RUBRIC = "INSTRUCTOR_RUBRIC"
    INSTRUCTOR_REVIEW = "INSTRUCTOR_REVIEW"


class ArtifactType(str, Enum):
    """Stable semantic role for an immutable run artifact.

    ``GENERIC`` preserves the schema-v1 research-run surface.  Full-day roles are
    deliberately explicit so inspection, export, and privacy policy never infer
    meaning from a filename.
    """

    GENERIC = "GENERIC"
    FULL_DAY_PLAN = "FULL_DAY_PLAN"
    FULL_DAY_OUTER_EVENT_LEDGER = "FULL_DAY_OUTER_EVENT_LEDGER"
    FULL_DAY_SUBSYSTEM_LEDGER = "FULL_DAY_SUBSYSTEM_LEDGER"
    FULL_DAY_CHECKPOINT_INDEX = "FULL_DAY_CHECKPOINT_INDEX"
    FULL_DAY_CHECKPOINT = "FULL_DAY_CHECKPOINT"
    FULL_DAY_SUMMARY = "FULL_DAY_SUMMARY"
    FULL_DAY_QUALIFICATION = "FULL_DAY_QUALIFICATION"
    FULL_DAY_DIAGNOSTICS = "FULL_DAY_DIAGNOSTICS"
    FULL_DAY_WINDOW = "FULL_DAY_WINDOW"
    FULL_DAY_PROFILE_QUALIFICATION = "FULL_DAY_PROFILE_QUALIFICATION"
    FULL_DAY_QUALIFICATION_RUN_PROOFS = "FULL_DAY_QUALIFICATION_RUN_PROOFS"
    FULL_DAY_REVIEW_SOURCE = "FULL_DAY_REVIEW_SOURCE"
    FULL_DAY_REVIEW_SELECTION = "FULL_DAY_REVIEW_SELECTION"
    FULL_DAY_REVIEW_PACKET = "FULL_DAY_REVIEW_PACKET"
    FULL_DAY_PERFORMANCE_EVIDENCE = "FULL_DAY_PERFORMANCE_EVIDENCE"
    FULL_DAY_QUALIFICATION_LEDGER = "FULL_DAY_QUALIFICATION_LEDGER"
    FULL_DAY_REVEAL_TOKEN = "FULL_DAY_REVEAL_TOKEN"
    FULL_DAY_REVIEWER_SIDECAR = "FULL_DAY_REVIEWER_SIDECAR"
    LESSON_MINING_SOURCE_MATRIX = "LESSON_MINING_SOURCE_MATRIX"
    LESSON_MINING_SOURCE_VALIDATION = "LESSON_MINING_SOURCE_VALIDATION"
    LESSON_MINING_CANDIDATES = "LESSON_MINING_CANDIDATES"
    LESSON_MINING_SELECTION = "LESSON_MINING_SELECTION"
    LESSON_TECHNICAL_REVIEW_PACKET = "LESSON_TECHNICAL_REVIEW_PACKET"
    LESSON_REVIEW_SIDECAR = "LESSON_REVIEW_SIDECAR"
    LESSON_BUILD_PROPOSAL = "LESSON_BUILD_PROPOSAL"
    LEARNER_EVIDENCE_UPDATE = "LEARNER_EVIDENCE_UPDATE"
    LEARNER_STATE_PROJECTION = "LEARNER_STATE_PROJECTION"
    STRATEGY_PARTITION_MANIFEST = "STRATEGY_PARTITION_MANIFEST"
    STRATEGY_EXPERIMENT_STATE = "STRATEGY_EXPERIMENT_STATE"
    STRATEGY_ACCESS_RECORD = "STRATEGY_ACCESS_RECORD"
    STRATEGY_DISCOVERY_BINDING = "STRATEGY_DISCOVERY_BINDING"
    STRATEGY_DISCOVERY_RECORD = "STRATEGY_DISCOVERY_RECORD"
    STRATEGY_LINEAGE_REPORT = "STRATEGY_LINEAGE_REPORT"
    STRATEGY_REVEAL_TOKEN = "STRATEGY_REVEAL_TOKEN"
    STRATEGY_SCIENTIFIC_OUTCOME = "STRATEGY_SCIENTIFIC_OUTCOME"
    INSTRUCTOR_ASSIGNMENT = "INSTRUCTOR_ASSIGNMENT"
    INSTRUCTOR_ATTEMPT_MANIFEST = "INSTRUCTOR_ATTEMPT_MANIFEST"
    INSTRUCTOR_RUBRIC = "INSTRUCTOR_RUBRIC"
    INSTRUCTOR_RUBRIC_SCORE = "INSTRUCTOR_RUBRIC_SCORE"
    INSTRUCTOR_REVIEW_SIDECAR = "INSTRUCTOR_REVIEW_SIDECAR"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    name: str
    relative_path: str
    sha256: str
    schema_version: int
    row_count: int | None
    media_type: str
    artifact_type: ArtifactType = ArtifactType.GENERIC

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        windows_path = PureWindowsPath(self.relative_path)
        if (
            not self.name
            or type(self.relative_path) is not str
            or not self.relative_path
            or "\\" in self.relative_path
            or path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or self.relative_path != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
            or not _SHA256.fullmatch(self.sha256)
        ):
            raise ValueError("artifact reference identity or digest is invalid")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("artifact schema version must be positive")
        if self.row_count is not None and (
            type(self.row_count) is not int or self.row_count < 0
        ):
            raise ValueError("artifact row count must be nonnegative or absent")
        if not self.media_type:
            raise ValueError("artifact media type is required")
        if not isinstance(self.artifact_type, ArtifactType):
            raise TypeError("artifact type must use ArtifactType")

    def as_dict(self, *, include_type: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "media_type": self.media_type,
            "name": self.name,
            "relative_path": self.relative_path,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }
        if include_type:
            payload["artifact_type"] = self.artifact_type.value
        if self.row_count is not None:
            payload["row_count"] = self.row_count
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ArtifactReference:
        row_count = payload.get("row_count")
        return cls(
            name=str(payload["name"]),
            relative_path=str(payload["relative_path"]),
            sha256=str(payload["sha256"]),
            schema_version=int(payload["schema_version"]),
            row_count=None if row_count is None else int(row_count),
            media_type=str(payload["media_type"]),
            artifact_type=ArtifactType(
                str(payload.get("artifact_type", ArtifactType.GENERIC.value))
            ),
        )


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    parent_run_id: str | None
    run_type: RunType
    scenario_id: str | None
    lesson_id: str | None
    seed: int | None
    flow_model: str
    market_profile: str
    strategy_id: str
    hotkey_layout_id: str
    session_objective: str
    simulation_start_us: int
    simulation_end_us: int
    software_version: str
    git_commit: str
    schema_versions: dict[str, int]
    input_dataset_references: tuple[str, ...]
    configuration_digest: str
    evidence_digest: str
    result_digest: str
    creation_timestamp_utc: str
    artifacts: tuple[ArtifactReference, ...]
    schema_version: int = RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run ID must be a content-derived Kirby2 identifier")
        if self.parent_run_id is not None and not _RUN_ID.fullmatch(self.parent_run_id):
            raise ValueError("parent run ID is invalid")
        if not isinstance(self.run_type, RunType):
            raise TypeError("run type is invalid")
        if self.seed is not None and type(self.seed) is not int:
            raise TypeError("run seed must be an integer or absent")
        if (
            type(self.simulation_start_us) is not int
            or type(self.simulation_end_us) is not int
            or self.simulation_start_us < 0
            or self.simulation_end_us < self.simulation_start_us
        ):
            raise ValueError("manifest simulation bounds are invalid")
        required_text = (
            self.flow_model,
            self.market_profile,
            self.strategy_id,
            self.hotkey_layout_id,
            self.session_objective,
            self.software_version,
            self.git_commit,
            self.creation_timestamp_utc,
        )
        if any(not value for value in required_text):
            raise ValueError("manifest required text fields must not be empty")
        try:
            datetime.fromisoformat(self.creation_timestamp_utc.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("manifest creation timestamp must be ISO-8601") from error
        if self.schema_version not in SUPPORTED_RUN_MANIFEST_SCHEMA_VERSIONS:
            raise ValueError("unsupported run manifest schema version")
        if self.schema_version == 1 and any(
            item.artifact_type is not ArtifactType.GENERIC for item in self.artifacts
        ):
            raise ValueError("schema-v1 manifests cannot carry typed artifacts")
        if not self.schema_versions or any(
            not key or type(value) is not int or value <= 0
            for key, value in self.schema_versions.items()
        ):
            raise ValueError("manifest schema inventory is invalid")
        if any(not reference for reference in self.input_dataset_references):
            raise ValueError("input dataset references must not be empty")
        if len(self.input_dataset_references) != len(
            set(self.input_dataset_references)
        ):
            raise ValueError("input dataset references must be unique")
        for digest in (
            self.configuration_digest,
            self.evidence_digest,
            self.result_digest,
        ):
            if not _SHA256.fullmatch(digest):
                raise ValueError("manifest content digest is invalid")
        names = tuple(item.name for item in self.artifacts)
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("manifest artifact names and paths must be unique")
        if self.run_id != self.derive_run_id(self.identity_dict()):
            raise ValueError("run ID does not match canonical run identity")

    @staticmethod
    def derive_run_id(identity: dict[str, object]) -> str:
        return "run-" + canonical_digest(identity)[:24]

    @classmethod
    def create(
        cls,
        *,
        parent_run_id: str | None,
        run_type: RunType,
        scenario_id: str | None,
        lesson_id: str | None,
        seed: int | None,
        flow_model: str,
        market_profile: str,
        strategy_id: str,
        hotkey_layout_id: str,
        session_objective: str,
        simulation_start_us: int,
        simulation_end_us: int,
        software_version: str,
        git_commit: str,
        schema_versions: dict[str, int],
        input_dataset_references: tuple[str, ...],
        configuration_digest: str,
        evidence_digest: str,
        result_digest: str,
        creation_timestamp_utc: str,
        artifacts: tuple[ArtifactReference, ...],
    ) -> RunManifest:
        identity: dict[str, object] = {
            "configuration_digest": configuration_digest,
            "evidence_digest": evidence_digest,
            "flow_model": flow_model,
            "git_commit": git_commit,
            "hotkey_layout_id": hotkey_layout_id,
            "input_dataset_references": list(input_dataset_references),
            "market_profile": market_profile,
            "result_digest": result_digest,
            "run_type": run_type.value,
            "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
            "schema_versions": schema_versions,
            "session_objective": session_objective,
            "simulation_end_us": simulation_end_us,
            "simulation_start_us": simulation_start_us,
            "software_version": software_version,
            "strategy_id": strategy_id,
        }
        if parent_run_id is not None:
            identity["parent_run_id"] = parent_run_id
        if scenario_id is not None:
            identity["scenario_id"] = scenario_id
        if lesson_id is not None:
            identity["lesson_id"] = lesson_id
        if seed is not None:
            identity["seed"] = seed
        return cls(
            run_id=cls.derive_run_id(identity),
            parent_run_id=parent_run_id,
            run_type=run_type,
            scenario_id=scenario_id,
            lesson_id=lesson_id,
            seed=seed,
            flow_model=flow_model,
            market_profile=market_profile,
            strategy_id=strategy_id,
            hotkey_layout_id=hotkey_layout_id,
            session_objective=session_objective,
            simulation_start_us=simulation_start_us,
            simulation_end_us=simulation_end_us,
            software_version=software_version,
            git_commit=git_commit,
            schema_versions=schema_versions,
            input_dataset_references=input_dataset_references,
            configuration_digest=configuration_digest,
            evidence_digest=evidence_digest,
            result_digest=result_digest,
            creation_timestamp_utc=creation_timestamp_utc,
            artifacts=artifacts,
        )

    def identity_dict(self) -> dict[str, object]:
        payload = self._base_dict()
        payload.pop("run_id")
        payload.pop("creation_timestamp_utc")
        payload.pop("artifacts")
        return payload

    def _base_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "artifacts": [
                item.as_dict(include_type=self.schema_version >= 2)
                for item in self.artifacts
            ],
            "configuration_digest": self.configuration_digest,
            "creation_timestamp_utc": self.creation_timestamp_utc,
            "evidence_digest": self.evidence_digest,
            "flow_model": self.flow_model,
            "git_commit": self.git_commit,
            "hotkey_layout_id": self.hotkey_layout_id,
            "input_dataset_references": list(self.input_dataset_references),
            "market_profile": self.market_profile,
            "result_digest": self.result_digest,
            "run_id": self.run_id,
            "run_type": self.run_type.value,
            "schema_version": self.schema_version,
            "schema_versions": self.schema_versions,
            "session_objective": self.session_objective,
            "simulation_end_us": self.simulation_end_us,
            "simulation_start_us": self.simulation_start_us,
            "software_version": self.software_version,
            "strategy_id": self.strategy_id,
        }
        if self.parent_run_id is not None:
            payload["parent_run_id"] = self.parent_run_id
        if self.scenario_id is not None:
            payload["scenario_id"] = self.scenario_id
        if self.lesson_id is not None:
            payload["lesson_id"] = self.lesson_id
        if self.seed is not None:
            payload["seed"] = self.seed
        return payload

    def as_dict(self) -> dict[str, object]:
        return self._base_dict()

    def to_toml(self) -> str:
        return canonical_toml(self.as_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> RunManifest:
        raw_artifacts = payload.get("artifacts")
        raw_schemas = payload.get("schema_versions")
        raw_inputs = payload.get("input_dataset_references")
        if not isinstance(raw_artifacts, list) or not all(
            isinstance(item, dict) for item in raw_artifacts
        ):
            raise ValueError("run manifest artifacts must be an array of tables")
        if not isinstance(raw_schemas, dict):
            raise ValueError("run manifest schema versions must be a table")
        if not isinstance(raw_inputs, list):
            raise ValueError("run manifest input datasets must be an array")
        return cls(
            run_id=str(payload["run_id"]),
            parent_run_id=(
                None
                if payload.get("parent_run_id") is None
                else str(payload["parent_run_id"])
            ),
            run_type=RunType(str(payload["run_type"])),
            scenario_id=(
                None if payload.get("scenario_id") is None else str(payload["scenario_id"])
            ),
            lesson_id=(
                None if payload.get("lesson_id") is None else str(payload["lesson_id"])
            ),
            seed=None if payload.get("seed") is None else int(payload["seed"]),
            flow_model=str(payload["flow_model"]),
            market_profile=str(payload["market_profile"]),
            strategy_id=str(payload["strategy_id"]),
            hotkey_layout_id=str(payload["hotkey_layout_id"]),
            session_objective=str(payload["session_objective"]),
            simulation_start_us=int(payload["simulation_start_us"]),
            simulation_end_us=int(payload["simulation_end_us"]),
            software_version=str(payload["software_version"]),
            git_commit=str(payload["git_commit"]),
            schema_versions={str(key): int(value) for key, value in raw_schemas.items()},
            input_dataset_references=tuple(str(item) for item in raw_inputs),
            configuration_digest=str(payload["configuration_digest"]),
            evidence_digest=str(payload["evidence_digest"]),
            result_digest=str(payload["result_digest"]),
            creation_timestamp_utc=str(payload["creation_timestamp_utc"]),
            artifacts=tuple(
                ArtifactReference.from_dict(item) for item in raw_artifacts
            ),
            schema_version=int(payload["schema_version"]),
        )
