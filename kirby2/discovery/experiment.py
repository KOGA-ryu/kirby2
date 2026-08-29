"""Immutable lifecycle for sealed strategy-discovery experiments."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, replace
from enum import Enum

from .identity import canonical_identity_bytes
from .partitions import PartitionManifestV1, StrategyPartitionV1


EXPERIMENT_BINDING_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_EXPERIMENT_BINDING_V1"
EXPERIMENT_STATE_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_EXPERIMENT_STATE_V1"
EXPERIMENT_STATE_SCHEMA_VERSION_V1 = 1
EXPERIMENT_STATE_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_EXPERIMENT_STATE_V1\x00"
CANDIDATE_FREEZE_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_CANDIDATE_FREEZE_V1"
CANDIDATE_FREEZE_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_CANDIDATE_FREEZE_V1\x00"
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExperimentPhaseV1(str, Enum):
    SEARCH_OPEN = "SEARCH_OPEN"
    CANDIDATES_FROZEN = "CANDIDATES_FROZEN"
    TERMINAL_EVALUATION = "TERMINAL_EVALUATION"


class TerminalEvaluationOutcomeV1(str, Enum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ExperimentPartitionBindingV1:
    """Bind the legacy experiment bytes to sealed partitions and AST identities."""

    experiment_id: str
    experiment_manifest_sha256: str
    partition_manifest_sha256: str
    strategy_semantic_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.experiment_id, "experiment binding ID")
        _require_sha256(
            self.experiment_manifest_sha256,
            "legacy experiment manifest digest",
        )
        _require_sha256(
            self.partition_manifest_sha256,
            "partition manifest digest",
        )
        _require_digest_tuple(
            self.strategy_semantic_sha256,
            "bound strategy semantic digests",
            nonempty=True,
        )
        object.__setattr__(
            self,
            "strategy_semantic_sha256",
            tuple(sorted(set(self.strategy_semantic_sha256))),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "experiment_manifest_sha256": self.experiment_manifest_sha256,
            "partition_manifest_sha256": self.partition_manifest_sha256,
            "schema_id": EXPERIMENT_BINDING_SCHEMA_ID_V1,
            "schema_version": 1,
            "strategy_semantic_sha256": list(self.strategy_semantic_sha256),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class CandidateSetFreezeV1:
    experiment_id: str
    experiment_version: int
    partition_manifest_sha256: str
    candidate_semantic_sha256: tuple[str, ...]
    selected_candidate_semantic_sha256: str
    selection_record_sha256: str

    def __post_init__(self) -> None:
        _require_identifier(self.experiment_id, "candidate-freeze experiment ID")
        if type(self.experiment_version) is not int or self.experiment_version <= 0:
            raise ValueError("candidate-freeze experiment version must be positive")
        _require_sha256(
            self.partition_manifest_sha256,
            "candidate-freeze partition digest",
        )
        _require_digest_tuple(
            self.candidate_semantic_sha256,
            "candidate semantic digests",
            nonempty=True,
        )
        canonical_candidates = tuple(sorted(set(self.candidate_semantic_sha256)))
        if len(canonical_candidates) != len(self.candidate_semantic_sha256):
            raise ValueError("candidate semantic digests must be unique")
        if self.selected_candidate_semantic_sha256 not in canonical_candidates:
            raise ValueError("selected candidate is outside the frozen candidate set")
        _require_sha256(
            self.selection_record_sha256,
            "candidate selection-record digest",
        )
        object.__setattr__(
            self,
            "candidate_semantic_sha256",
            canonical_candidates,
        )

    @property
    def freeze_sha256(self) -> str:
        return _domain_digest(
            CANDIDATE_FREEZE_DIGEST_DOMAIN_V1,
            self.canonical_bytes(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_semantic_sha256": list(self.candidate_semantic_sha256),
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "partition_manifest_sha256": self.partition_manifest_sha256,
            "schema_id": CANDIDATE_FREEZE_SCHEMA_ID_V1,
            "schema_version": 1,
            "selected_candidate_semantic_sha256": (
                self.selected_candidate_semantic_sha256
            ),
            "selection_record_sha256": self.selection_record_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> CandidateSetFreezeV1:
        row = _exact_object(
            payload,
            {
                "candidate_semantic_sha256",
                "experiment_id",
                "experiment_version",
                "partition_manifest_sha256",
                "schema_id",
                "schema_version",
                "selected_candidate_semantic_sha256",
                "selection_record_sha256",
            },
            "candidate freeze",
        )
        if _text(row, "schema_id") != CANDIDATE_FREEZE_SCHEMA_ID_V1:
            raise ValueError("unsupported candidate-freeze schema ID")
        if _integer(row, "schema_version") != 1:
            raise ValueError("unsupported candidate-freeze schema version")
        return cls(
            experiment_id=_text(row, "experiment_id"),
            experiment_version=_integer(row, "experiment_version"),
            partition_manifest_sha256=_text(row, "partition_manifest_sha256"),
            candidate_semantic_sha256=_text_tuple(
                row["candidate_semantic_sha256"],
                "candidate semantic digests",
            ),
            selected_candidate_semantic_sha256=_text(
                row,
                "selected_candidate_semantic_sha256",
            ),
            selection_record_sha256=_text(row, "selection_record_sha256"),
        )


@dataclass(frozen=True, slots=True)
class ValidationAccessCountV1:
    schedule_id: str
    count: int

    def __post_init__(self) -> None:
        _require_identifier(self.schedule_id, "validation access schedule ID")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("validation access count must be positive")

    def as_dict(self) -> dict[str, object]:
        return {"count": self.count, "schedule_id": self.schedule_id}

    @classmethod
    def from_dict(cls, payload: object) -> ValidationAccessCountV1:
        row = _exact_object(payload, {"count", "schedule_id"}, "validation count")
        return cls(_text(row, "schedule_id"), _integer(row, "count"))


@dataclass(frozen=True, slots=True)
class StrategyDiscoveryExperimentV1:
    experiment_id: str
    experiment_version: int
    partition_manifest_sha256: str
    phase: ExperimentPhaseV1
    candidate_freeze: CandidateSetFreezeV1 | None
    train_access_count: int
    validation_access_counts: tuple[ValidationAccessCountV1, ...]
    access_record_sha256: tuple[str, ...]
    reveal_access_sha256: str | None
    terminal_outcome: TerminalEvaluationOutcomeV1 | None
    schema_version: int = EXPERIMENT_STATE_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_identifier(self.experiment_id, "strategy experiment ID")
        if type(self.experiment_version) is not int or self.experiment_version <= 0:
            raise ValueError("strategy experiment version must be positive")
        _require_sha256(self.partition_manifest_sha256, "partition manifest digest")
        if not isinstance(self.phase, ExperimentPhaseV1):
            raise TypeError("strategy experiment phase is invalid")
        if (
            type(self.schema_version) is not int
            or self.schema_version != EXPERIMENT_STATE_SCHEMA_VERSION_V1
        ):
            raise ValueError("unsupported strategy experiment state schema")
        if type(self.train_access_count) is not int or self.train_access_count < 0:
            raise ValueError("train access count must be nonnegative")
        if type(self.validation_access_counts) is not tuple or any(
            not isinstance(item, ValidationAccessCountV1)
            for item in self.validation_access_counts
        ):
            raise TypeError("validation access counters must be typed tuples")
        counters = tuple(
            sorted(self.validation_access_counts, key=lambda item: item.schedule_id)
        )
        counter_ids = tuple(item.schedule_id for item in counters)
        if len(counter_ids) != len(set(counter_ids)):
            raise ValueError("validation access counters must be unique")
        object.__setattr__(self, "validation_access_counts", counters)
        _require_digest_tuple(
            self.access_record_sha256,
            "experiment access-record digests",
            nonempty=False,
        )
        if len(self.access_record_sha256) != len(set(self.access_record_sha256)):
            raise ValueError("experiment access-record digests must be unique")
        if self.reveal_access_sha256 is not None:
            _require_sha256(self.reveal_access_sha256, "holdout reveal access digest")
            if self.reveal_access_sha256 not in self.access_record_sha256:
                raise ValueError("holdout reveal digest is absent from access history")
        if self.candidate_freeze is not None:
            if not isinstance(self.candidate_freeze, CandidateSetFreezeV1):
                raise TypeError("candidate freeze is invalid")
            if (
                self.candidate_freeze.experiment_id != self.experiment_id
                or self.candidate_freeze.experiment_version != self.experiment_version
                or self.candidate_freeze.partition_manifest_sha256
                != self.partition_manifest_sha256
            ):
                raise ValueError("candidate freeze is bound to another experiment")
        if self.terminal_outcome is not None and not isinstance(
            self.terminal_outcome,
            TerminalEvaluationOutcomeV1,
        ):
            raise TypeError("terminal evaluation outcome is invalid")
        self._validate_phase()

    def _validate_phase(self) -> None:
        if self.phase is ExperimentPhaseV1.SEARCH_OPEN:
            if (
                self.candidate_freeze is not None
                or self.reveal_access_sha256 is not None
                or self.terminal_outcome is not None
            ):
                raise ValueError("open search cannot carry frozen or terminal state")
            return
        if self.candidate_freeze is None:
            raise ValueError("closed search requires a frozen candidate set")
        if self.phase is ExperimentPhaseV1.CANDIDATES_FROZEN:
            if self.reveal_access_sha256 is not None or self.terminal_outcome is not None:
                raise ValueError("candidate-frozen phase cannot carry terminal state")
            return
        if self.reveal_access_sha256 is None:
            raise ValueError("terminal evaluation requires a reveal access record")
        if self.terminal_outcome is None:
            raise ValueError("terminal evaluation outcome must be explicit")

    @property
    def state_sha256(self) -> str:
        return _domain_digest(EXPERIMENT_STATE_DIGEST_DOMAIN_V1, self.canonical_bytes())

    @property
    def candidate_freeze_sha256(self) -> str | None:
        if self.candidate_freeze is None:
            return None
        return self.candidate_freeze.freeze_sha256

    def validation_access_count(self, schedule_id: str) -> int:
        for row in self.validation_access_counts:
            if row.schedule_id == schedule_id:
                return row.count
        return 0

    def as_dict(self) -> dict[str, object]:
        return {
            "access_record_sha256": list(self.access_record_sha256),
            "candidate_freeze": (
                None if self.candidate_freeze is None else self.candidate_freeze.as_dict()
            ),
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "partition_manifest_sha256": self.partition_manifest_sha256,
            "phase": self.phase.value,
            "reveal_access_sha256": self.reveal_access_sha256,
            "schema_id": EXPERIMENT_STATE_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "terminal_outcome": (
                None if self.terminal_outcome is None else self.terminal_outcome.value
            ),
            "train_access_count": self.train_access_count,
            "validation_access_counts": [
                item.as_dict() for item in self.validation_access_counts
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> StrategyDiscoveryExperimentV1:
        row = _exact_object(
            payload,
            {
                "access_record_sha256",
                "candidate_freeze",
                "experiment_id",
                "experiment_version",
                "partition_manifest_sha256",
                "phase",
                "reveal_access_sha256",
                "schema_id",
                "schema_version",
                "terminal_outcome",
                "train_access_count",
                "validation_access_counts",
            },
            "strategy experiment state",
        )
        if _text(row, "schema_id") != EXPERIMENT_STATE_SCHEMA_ID_V1:
            raise ValueError("unsupported strategy experiment state schema ID")
        raw_freeze = row["candidate_freeze"]
        raw_reveal = row["reveal_access_sha256"]
        raw_outcome = row["terminal_outcome"]
        if raw_reveal is not None and type(raw_reveal) is not str:
            raise TypeError("reveal access digest must be text or null")
        if raw_outcome is not None and type(raw_outcome) is not str:
            raise TypeError("terminal outcome must be text or null")
        raw_counts = _object_array(
            row["validation_access_counts"],
            "validation access counts",
        )
        return cls(
            experiment_id=_text(row, "experiment_id"),
            experiment_version=_integer(row, "experiment_version"),
            partition_manifest_sha256=_text(row, "partition_manifest_sha256"),
            phase=ExperimentPhaseV1(_text(row, "phase")),
            candidate_freeze=(
                None
                if raw_freeze is None
                else CandidateSetFreezeV1.from_dict(raw_freeze)
            ),
            train_access_count=_integer(row, "train_access_count"),
            validation_access_counts=tuple(
                ValidationAccessCountV1.from_dict(item) for item in raw_counts
            ),
            access_record_sha256=_text_tuple(
                row["access_record_sha256"],
                "experiment access-record digests",
            ),
            reveal_access_sha256=raw_reveal,
            terminal_outcome=(
                None
                if raw_outcome is None
                else TerminalEvaluationOutcomeV1(raw_outcome)
            ),
            schema_version=_integer(row, "schema_version"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> StrategyDiscoveryExperimentV1:
        if type(raw) is not bytes:
            raise TypeError("strategy experiment state must be exact bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("strategy experiment state must be UTF-8 JSON") from error
        state = cls.from_dict(payload)
        if state.canonical_bytes() != raw:
            raise ValueError("strategy experiment state bytes are not canonical")
        return state


def start_strategy_experiment(
    manifest: PartitionManifestV1,
) -> StrategyDiscoveryExperimentV1:
    if not isinstance(manifest, PartitionManifestV1):
        raise TypeError("strategy experiment requires a partition manifest")
    return StrategyDiscoveryExperimentV1(
        experiment_id=manifest.experiment_id,
        experiment_version=manifest.experiment_version,
        partition_manifest_sha256=manifest.manifest_sha256,
        phase=ExperimentPhaseV1.SEARCH_OPEN,
        candidate_freeze=None,
        train_access_count=0,
        validation_access_counts=(),
        access_record_sha256=(),
        reveal_access_sha256=None,
        terminal_outcome=None,
    )


def freeze_candidate_set(
    experiment: StrategyDiscoveryExperimentV1,
    *,
    candidate_semantic_sha256: tuple[str, ...],
    selected_candidate_semantic_sha256: str,
    selection_record_sha256: str,
) -> StrategyDiscoveryExperimentV1:
    if not isinstance(experiment, StrategyDiscoveryExperimentV1):
        raise TypeError("candidate freeze requires a strategy experiment")
    if experiment.phase is not ExperimentPhaseV1.SEARCH_OPEN:
        raise ValueError("candidate set can only freeze while search is open")
    freeze = CandidateSetFreezeV1(
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.experiment_version,
        partition_manifest_sha256=experiment.partition_manifest_sha256,
        candidate_semantic_sha256=candidate_semantic_sha256,
        selected_candidate_semantic_sha256=selected_candidate_semantic_sha256,
        selection_record_sha256=selection_record_sha256,
    )
    return replace(
        experiment,
        phase=ExperimentPhaseV1.CANDIDATES_FROZEN,
        candidate_freeze=freeze,
    )


def close_terminal_evaluation(
    experiment: StrategyDiscoveryExperimentV1,
    outcome: TerminalEvaluationOutcomeV1,
) -> StrategyDiscoveryExperimentV1:
    if not isinstance(experiment, StrategyDiscoveryExperimentV1):
        raise TypeError("terminal outcome requires a strategy experiment")
    if experiment.phase is not ExperimentPhaseV1.TERMINAL_EVALUATION:
        raise ValueError("terminal outcome requires a revealed experiment")
    if outcome not in {
        TerminalEvaluationOutcomeV1.PASSED,
        TerminalEvaluationOutcomeV1.FAILED,
    }:
        raise ValueError("terminal evaluation must close as PASSED or FAILED")
    if experiment.terminal_outcome is not TerminalEvaluationOutcomeV1.PENDING:
        if experiment.terminal_outcome is outcome:
            return experiment
        raise ValueError("terminal evaluation outcome is immutable")
    return replace(experiment, terminal_outcome=outcome)


def start_successor_experiment(
    previous: StrategyDiscoveryExperimentV1,
    previous_manifest: PartitionManifestV1,
    successor_manifest: PartitionManifestV1,
) -> StrategyDiscoveryExperimentV1:
    if not isinstance(previous, StrategyDiscoveryExperimentV1):
        raise TypeError("successor creation requires a prior experiment")
    _require_manifest_binding(previous, previous_manifest)
    if not isinstance(successor_manifest, PartitionManifestV1):
        raise TypeError("successor creation requires a partition manifest")
    if previous.phase is not ExperimentPhaseV1.TERMINAL_EVALUATION:
        raise ValueError("successor search requires a revealed prior experiment")
    if previous.terminal_outcome is TerminalEvaluationOutcomeV1.PENDING:
        raise ValueError("successor search requires terminal evaluation closure")
    if (
        successor_manifest.experiment_id != previous.experiment_id
        or successor_manifest.experiment_version != previous.experiment_version + 1
    ):
        raise ValueError("successor must increment the same experiment by one version")
    previous_tokens, previous_datasets = _terminal_identity(previous_manifest)
    successor_tokens, successor_datasets = _terminal_identity(successor_manifest)
    if previous_tokens & successor_tokens:
        raise ValueError("successor reuses terminal-partition ancestry")
    if previous_datasets & successor_datasets:
        raise ValueError("successor reuses a terminal-partition dataset")
    return start_strategy_experiment(successor_manifest)


def _terminal_identity(
    manifest: PartitionManifestV1,
) -> tuple[frozenset[str], frozenset[str]]:
    terminal = {
        StrategyPartitionV1.HOLDOUT,
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        StrategyPartitionV1.ROBUSTNESS,
    }
    members = tuple(item for item in manifest.members if item.partition in terminal)
    return (
        frozenset(token for item in members for token in item.independence_tokens),
        frozenset(item.dataset_sha256 for item in members),
    )


def _require_manifest_binding(
    experiment: StrategyDiscoveryExperimentV1,
    manifest: PartitionManifestV1,
) -> None:
    if not isinstance(manifest, PartitionManifestV1):
        raise TypeError("strategy experiment manifest is invalid")
    if (
        experiment.experiment_id != manifest.experiment_id
        or experiment.experiment_version != manifest.experiment_version
        or experiment.partition_manifest_sha256 != manifest.manifest_sha256
    ):
        raise ValueError("strategy experiment and partition manifest do not match")


def _domain_digest(domain: bytes, raw: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(struct.pack(">Q", len(raw)))
    digest.update(raw)
    return digest.hexdigest()


def _require_identifier(value: object, context: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{context} is invalid")


def _require_sha256(value: object, context: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be lowercase SHA-256")


def _require_digest_tuple(values: object, context: str, *, nonempty: bool) -> None:
    if type(values) is not tuple or (nonempty and not values):
        raise ValueError(f"{context} must be a{' nonempty' if nonempty else ''} tuple")
    for value in values:
        _require_sha256(value, f"{context} member")


def _exact_object(
    payload: object,
    expected: set[str],
    context: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        raise TypeError(f"serialized {context} must be an object")
    missing = sorted(expected - set(payload))
    unknown = sorted(set(payload) - expected)
    if missing or unknown:
        raise ValueError(
            f"serialized {context} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )
    return payload


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _text_tuple(value: object, context: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TypeError(f"{context} must be an array of strings")
    return tuple(value)


def _object_array(value: object, context: str) -> list[dict[str, object]]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise TypeError(f"{context} must be an array of objects")
    return value


__all__ = [
    "CANDIDATE_FREEZE_SCHEMA_ID_V1",
    "EXPERIMENT_BINDING_SCHEMA_ID_V1",
    "EXPERIMENT_STATE_SCHEMA_ID_V1",
    "EXPERIMENT_STATE_SCHEMA_VERSION_V1",
    "CandidateSetFreezeV1",
    "ExperimentPartitionBindingV1",
    "ExperimentPhaseV1",
    "StrategyDiscoveryExperimentV1",
    "TerminalEvaluationOutcomeV1",
    "ValidationAccessCountV1",
    "close_terminal_evaluation",
    "freeze_candidate_set",
    "start_strategy_experiment",
    "start_successor_experiment",
]
