"""Enforced, immutable access decisions for sealed strategy partitions."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, replace
from enum import Enum

from .experiment import (
    ExperimentPhaseV1,
    StrategyDiscoveryExperimentV1,
    TerminalEvaluationOutcomeV1,
    ValidationAccessCountV1,
)
from .identity import canonical_identity_bytes
from .partitions import PartitionManifestV1, PartitionMemberV1, StrategyPartitionV1


PARTITION_ACCESS_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_PARTITION_ACCESS_V1"
PARTITION_ACCESS_SCHEMA_VERSION_V1 = 1
PARTITION_ACCESS_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_PARTITION_ACCESS_V1\x00"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PartitionAccessPurposeV1(str, Enum):
    SEARCH_TRAIN = "SEARCH_TRAIN"
    SEARCH_VALIDATION = "SEARCH_VALIDATION"
    HOLDOUT_REVEAL = "HOLDOUT_REVEAL"
    TERMINAL_EVALUATION = "TERMINAL_EVALUATION"


class PartitionAccessDecisionV1(str, Enum):
    GRANTED = "GRANTED"
    REFUSED = "REFUSED"


class PartitionAccessReasonV1(str, Enum):
    GRANTED = "GRANTED"
    CANDIDATES_NOT_FROZEN = "CANDIDATES_NOT_FROZEN"
    SEARCH_CLOSED = "SEARCH_CLOSED"
    SEARCH_TERMINATED = "SEARCH_TERMINATED"
    REVEAL_ALREADY_CONSUMED = "REVEAL_ALREADY_CONSUMED"
    TERMINAL_EVALUATION_NOT_STARTED = "TERMINAL_EVALUATION_NOT_STARTED"
    PARTITION_PURPOSE_MISMATCH = "PARTITION_PURPOSE_MISMATCH"
    MEMBER_SCOPE_MISMATCH = "MEMBER_SCOPE_MISMATCH"
    VALIDATION_SCHEDULE_REQUIRED = "VALIDATION_SCHEDULE_REQUIRED"
    VALIDATION_SCHEDULE_UNKNOWN = "VALIDATION_SCHEDULE_UNKNOWN"
    VALIDATION_NOT_RELEASED = "VALIDATION_NOT_RELEASED"
    VALIDATION_BUDGET_EXHAUSTED = "VALIDATION_BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class PartitionAccessRecordV1:
    experiment_id: str
    experiment_version: int
    partition_manifest_sha256: str
    access_ordinal: int
    previous_access_sha256: str | None
    state_before_sha256: str
    phase_before: ExperimentPhaseV1
    phase_after: ExperimentPhaseV1
    partition: StrategyPartitionV1
    purpose: PartitionAccessPurposeV1
    requested_member_ids: tuple[str, ...]
    validation_schedule_id: str | None
    decision: PartitionAccessDecisionV1
    reason: PartitionAccessReasonV1
    metrics_visible: bool
    granted_member_ids: tuple[str, ...]
    candidate_freeze_sha256: str | None
    schema_version: int = PARTITION_ACCESS_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.experiment_id) is not str or not self.experiment_id:
            raise ValueError("partition access experiment ID must be nonempty")
        if type(self.experiment_version) is not int or self.experiment_version <= 0:
            raise ValueError("partition access experiment version must be positive")
        if (
            type(self.schema_version) is not int
            or self.schema_version != PARTITION_ACCESS_SCHEMA_VERSION_V1
        ):
            raise ValueError("unsupported partition access record schema")
        _require_sha256(self.partition_manifest_sha256, "partition manifest digest")
        if type(self.access_ordinal) is not int or self.access_ordinal <= 0:
            raise ValueError("partition access ordinal must be positive")
        if self.previous_access_sha256 is not None:
            _require_sha256(self.previous_access_sha256, "previous access digest")
        if (self.access_ordinal == 1) != (self.previous_access_sha256 is None):
            raise ValueError("partition access predecessor and ordinal disagree")
        _require_sha256(self.state_before_sha256, "pre-access state digest")
        if not isinstance(self.phase_before, ExperimentPhaseV1) or not isinstance(
            self.phase_after,
            ExperimentPhaseV1,
        ):
            raise TypeError("partition access phase is invalid")
        if not isinstance(self.partition, StrategyPartitionV1):
            raise TypeError("partition access target is invalid")
        if not isinstance(self.purpose, PartitionAccessPurposeV1):
            raise TypeError("partition access purpose is invalid")
        _require_member_ids(self.requested_member_ids, "requested partition members")
        if self.validation_schedule_id is not None and (
            type(self.validation_schedule_id) is not str
            or not self.validation_schedule_id
        ):
            raise ValueError("validation schedule ID must be nonempty text or null")
        if not isinstance(self.decision, PartitionAccessDecisionV1):
            raise TypeError("partition access decision is invalid")
        if not isinstance(self.reason, PartitionAccessReasonV1):
            raise TypeError("partition access reason is invalid")
        if type(self.metrics_visible) is not bool:
            raise TypeError("partition metrics visibility must be boolean")
        _require_member_ids(self.granted_member_ids, "granted partition members")
        if self.candidate_freeze_sha256 is not None:
            _require_sha256(self.candidate_freeze_sha256, "candidate-freeze digest")
        self._validate_decision()

    def _validate_decision(self) -> None:
        if self.decision is PartitionAccessDecisionV1.GRANTED:
            if (
                self.reason is not PartitionAccessReasonV1.GRANTED
                or not self.metrics_visible
                or not self.granted_member_ids
            ):
                raise ValueError("granted partition access is internally inconsistent")
            if not set(self.granted_member_ids) <= set(self.requested_member_ids):
                raise ValueError("granted members exceed the requested scope")
        elif (
            self.reason is PartitionAccessReasonV1.GRANTED
            or self.metrics_visible
            or self.granted_member_ids
        ):
            raise ValueError("refused partition access exposed sealed data")
        reveal = self.purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL
        if (
            self.decision is PartitionAccessDecisionV1.GRANTED
            and reveal
            and (
                self.partition is not StrategyPartitionV1.HOLDOUT
                or self.phase_before is not ExperimentPhaseV1.CANDIDATES_FROZEN
                or self.phase_after is not ExperimentPhaseV1.TERMINAL_EVALUATION
                or self.candidate_freeze_sha256 is None
            )
        ):
            raise ValueError("granted holdout reveal has an invalid state transition")
        if not (self.decision is PartitionAccessDecisionV1.GRANTED and reveal):
            if self.phase_after is not self.phase_before:
                raise ValueError("only a granted holdout reveal may change phase")

    @property
    def access_sha256(self) -> str:
        raw = self.canonical_bytes()
        digest = hashlib.sha256()
        digest.update(PARTITION_ACCESS_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "access_ordinal": self.access_ordinal,
            "candidate_freeze_sha256": self.candidate_freeze_sha256,
            "decision": self.decision.value,
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "granted_member_ids": list(self.granted_member_ids),
            "metrics_visible": self.metrics_visible,
            "partition": self.partition.value,
            "partition_manifest_sha256": self.partition_manifest_sha256,
            "phase_after": self.phase_after.value,
            "phase_before": self.phase_before.value,
            "previous_access_sha256": self.previous_access_sha256,
            "purpose": self.purpose.value,
            "reason": self.reason.value,
            "requested_member_ids": list(self.requested_member_ids),
            "schema_id": PARTITION_ACCESS_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "state_before_sha256": self.state_before_sha256,
            "validation_schedule_id": self.validation_schedule_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> PartitionAccessRecordV1:
        row = _exact_object(
            payload,
            {
                "access_ordinal",
                "candidate_freeze_sha256",
                "decision",
                "experiment_id",
                "experiment_version",
                "granted_member_ids",
                "metrics_visible",
                "partition",
                "partition_manifest_sha256",
                "phase_after",
                "phase_before",
                "previous_access_sha256",
                "purpose",
                "reason",
                "requested_member_ids",
                "schema_id",
                "schema_version",
                "state_before_sha256",
                "validation_schedule_id",
            },
            "partition access record",
        )
        if _text(row, "schema_id") != PARTITION_ACCESS_SCHEMA_ID_V1:
            raise ValueError("unsupported partition access schema ID")
        previous = _nullable_text(row, "previous_access_sha256")
        schedule_id = _nullable_text(row, "validation_schedule_id")
        candidate_freeze = _nullable_text(row, "candidate_freeze_sha256")
        metrics_visible = row["metrics_visible"]
        if type(metrics_visible) is not bool:
            raise TypeError("metrics_visible must be boolean")
        return cls(
            experiment_id=_text(row, "experiment_id"),
            experiment_version=_integer(row, "experiment_version"),
            partition_manifest_sha256=_text(row, "partition_manifest_sha256"),
            access_ordinal=_integer(row, "access_ordinal"),
            previous_access_sha256=previous,
            state_before_sha256=_text(row, "state_before_sha256"),
            phase_before=ExperimentPhaseV1(_text(row, "phase_before")),
            phase_after=ExperimentPhaseV1(_text(row, "phase_after")),
            partition=StrategyPartitionV1(_text(row, "partition")),
            purpose=PartitionAccessPurposeV1(_text(row, "purpose")),
            requested_member_ids=_text_tuple(
                row["requested_member_ids"],
                "requested member IDs",
            ),
            validation_schedule_id=schedule_id,
            decision=PartitionAccessDecisionV1(_text(row, "decision")),
            reason=PartitionAccessReasonV1(_text(row, "reason")),
            metrics_visible=metrics_visible,
            granted_member_ids=_text_tuple(
                row["granted_member_ids"],
                "granted member IDs",
            ),
            candidate_freeze_sha256=candidate_freeze,
            schema_version=_integer(row, "schema_version"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> PartitionAccessRecordV1:
        if type(raw) is not bytes:
            raise TypeError("partition access record must be exact bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("partition access record must be UTF-8 JSON") from error
        record = cls.from_dict(payload)
        if record.canonical_bytes() != raw:
            raise ValueError("partition access record bytes are not canonical")
        return record


@dataclass(frozen=True, slots=True)
class PartitionAccessResultV1:
    experiment: StrategyDiscoveryExperimentV1
    record: PartitionAccessRecordV1
    members: tuple[PartitionMemberV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.experiment, StrategyDiscoveryExperimentV1):
            raise TypeError("partition access result experiment is invalid")
        if not isinstance(self.record, PartitionAccessRecordV1):
            raise TypeError("partition access result record is invalid")
        if type(self.members) is not tuple or any(
            not isinstance(item, PartitionMemberV1) for item in self.members
        ):
            raise TypeError("partition access result members are invalid")
        if self.experiment.access_record_sha256[-1:] != (
            self.record.access_sha256,
        ):
            raise ValueError("partition access result omitted its immutable record")
        if (
            self.experiment.experiment_id != self.record.experiment_id
            or self.experiment.experiment_version != self.record.experiment_version
            or self.experiment.partition_manifest_sha256
            != self.record.partition_manifest_sha256
            or tuple(item.member_id for item in self.members)
            != self.record.granted_member_ids
        ):
            raise ValueError("partition access result is not bound to its record")
        if self.record.decision is PartitionAccessDecisionV1.REFUSED and self.members:
            raise ValueError("refused partition access returned sealed members")


def request_partition_access(
    manifest: PartitionManifestV1,
    experiment: StrategyDiscoveryExperimentV1,
    *,
    partition: StrategyPartitionV1,
    purpose: PartitionAccessPurposeV1,
    member_ids: tuple[str, ...] | None = None,
    validation_schedule_id: str | None = None,
) -> PartitionAccessResultV1:
    """Evaluate one access and append its grant or refusal to immutable state."""

    _require_binding(manifest, experiment)
    if not isinstance(partition, StrategyPartitionV1):
        raise TypeError("partition access target is invalid")
    if not isinstance(purpose, PartitionAccessPurposeV1):
        raise TypeError("partition access purpose is invalid")
    all_members = manifest.partition_members(partition)
    all_ids = tuple(item.member_id for item in all_members)
    requested_ids = all_ids if member_ids is None else member_ids
    _require_member_ids(requested_ids, "requested partition members")
    requested_members = tuple(
        item for item in all_members if item.member_id in set(requested_ids)
    )
    scope_valid = (
        bool(requested_ids)
        and len(requested_members) == len(requested_ids)
        and tuple(item.member_id for item in requested_members) == requested_ids
    )
    decision, reason, phase_after, granted = _decide_access(
        manifest,
        experiment,
        partition=partition,
        purpose=purpose,
        requested_ids=requested_ids,
        requested_members=requested_members,
        all_ids=all_ids,
        scope_valid=scope_valid,
        validation_schedule_id=validation_schedule_id,
    )
    record = PartitionAccessRecordV1(
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.experiment_version,
        partition_manifest_sha256=experiment.partition_manifest_sha256,
        access_ordinal=len(experiment.access_record_sha256) + 1,
        previous_access_sha256=(
            None
            if not experiment.access_record_sha256
            else experiment.access_record_sha256[-1]
        ),
        state_before_sha256=experiment.state_sha256,
        phase_before=experiment.phase,
        phase_after=phase_after,
        partition=partition,
        purpose=purpose,
        requested_member_ids=requested_ids,
        validation_schedule_id=validation_schedule_id,
        decision=decision,
        reason=reason,
        metrics_visible=decision is PartitionAccessDecisionV1.GRANTED,
        granted_member_ids=tuple(item.member_id for item in granted),
        candidate_freeze_sha256=experiment.candidate_freeze_sha256,
    )
    updated = _append_access_state(
        experiment,
        record,
        validation_schedule_id=validation_schedule_id,
    )
    return PartitionAccessResultV1(updated, record, granted)


def _decide_access(
    manifest: PartitionManifestV1,
    experiment: StrategyDiscoveryExperimentV1,
    *,
    partition: StrategyPartitionV1,
    purpose: PartitionAccessPurposeV1,
    requested_ids: tuple[str, ...],
    requested_members: tuple[PartitionMemberV1, ...],
    all_ids: tuple[str, ...],
    scope_valid: bool,
    validation_schedule_id: str | None,
) -> tuple[
    PartitionAccessDecisionV1,
    PartitionAccessReasonV1,
    ExperimentPhaseV1,
    tuple[PartitionMemberV1, ...],
]:
    refused = PartitionAccessDecisionV1.REFUSED
    same_phase = experiment.phase
    empty: tuple[PartitionMemberV1, ...] = ()
    if not scope_valid:
        return refused, PartitionAccessReasonV1.MEMBER_SCOPE_MISMATCH, same_phase, empty
    if (
        validation_schedule_id is not None
        and purpose is not PartitionAccessPurposeV1.SEARCH_VALIDATION
    ):
        return (
            refused,
            PartitionAccessReasonV1.PARTITION_PURPOSE_MISMATCH,
            same_phase,
            empty,
        )
    if experiment.phase is ExperimentPhaseV1.SEARCH_OPEN:
        if purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL:
            return (
                refused,
                PartitionAccessReasonV1.CANDIDATES_NOT_FROZEN,
                same_phase,
                empty,
            )
        if purpose is PartitionAccessPurposeV1.TERMINAL_EVALUATION:
            return (
                refused,
                PartitionAccessReasonV1.TERMINAL_EVALUATION_NOT_STARTED,
                same_phase,
                empty,
            )
        if (
            purpose is PartitionAccessPurposeV1.SEARCH_TRAIN
            and partition is StrategyPartitionV1.TRAIN
        ):
            return (
                PartitionAccessDecisionV1.GRANTED,
                PartitionAccessReasonV1.GRANTED,
                same_phase,
                requested_members,
            )
        if (
            purpose is PartitionAccessPurposeV1.SEARCH_VALIDATION
            and partition is StrategyPartitionV1.VALIDATION
        ):
            return _decide_validation(
                manifest,
                experiment,
                requested_ids,
                requested_members,
                validation_schedule_id,
            )
        return (
            refused,
            PartitionAccessReasonV1.PARTITION_PURPOSE_MISMATCH,
            same_phase,
            empty,
        )
    if experiment.phase is ExperimentPhaseV1.CANDIDATES_FROZEN:
        if purpose in {
            PartitionAccessPurposeV1.SEARCH_TRAIN,
            PartitionAccessPurposeV1.SEARCH_VALIDATION,
        }:
            return refused, PartitionAccessReasonV1.SEARCH_CLOSED, same_phase, empty
        if (
            purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL
            and partition is StrategyPartitionV1.HOLDOUT
            and requested_ids == all_ids
        ):
            return (
                PartitionAccessDecisionV1.GRANTED,
                PartitionAccessReasonV1.GRANTED,
                ExperimentPhaseV1.TERMINAL_EVALUATION,
                requested_members,
            )
        if purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL:
            return (
                refused,
                PartitionAccessReasonV1.MEMBER_SCOPE_MISMATCH,
                same_phase,
                empty,
            )
        return (
            refused,
            PartitionAccessReasonV1.TERMINAL_EVALUATION_NOT_STARTED,
            same_phase,
            empty,
        )
    if purpose in {
        PartitionAccessPurposeV1.SEARCH_TRAIN,
        PartitionAccessPurposeV1.SEARCH_VALIDATION,
    }:
        return refused, PartitionAccessReasonV1.SEARCH_TERMINATED, same_phase, empty
    if purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL:
        return (
            refused,
            PartitionAccessReasonV1.REVEAL_ALREADY_CONSUMED,
            same_phase,
            empty,
        )
    terminal_partitions = {
        StrategyPartitionV1.HOLDOUT,
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        StrategyPartitionV1.ROBUSTNESS,
    }
    if partition in terminal_partitions and requested_ids == all_ids:
        return (
            PartitionAccessDecisionV1.GRANTED,
            PartitionAccessReasonV1.GRANTED,
            same_phase,
            requested_members,
        )
    return (
        refused,
        PartitionAccessReasonV1.PARTITION_PURPOSE_MISMATCH,
        same_phase,
        empty,
    )


def _decide_validation(
    manifest: PartitionManifestV1,
    experiment: StrategyDiscoveryExperimentV1,
    requested_ids: tuple[str, ...],
    requested_members: tuple[PartitionMemberV1, ...],
    schedule_id: str | None,
) -> tuple[
    PartitionAccessDecisionV1,
    PartitionAccessReasonV1,
    ExperimentPhaseV1,
    tuple[PartitionMemberV1, ...],
]:
    refused = PartitionAccessDecisionV1.REFUSED
    empty: tuple[PartitionMemberV1, ...] = ()
    if schedule_id is None:
        return (
            refused,
            PartitionAccessReasonV1.VALIDATION_SCHEDULE_REQUIRED,
            experiment.phase,
            empty,
        )
    try:
        schedule = manifest.schedule(schedule_id)
    except KeyError:
        return (
            refused,
            PartitionAccessReasonV1.VALIDATION_SCHEDULE_UNKNOWN,
            experiment.phase,
            empty,
        )
    if requested_ids != schedule.member_ids:
        return (
            refused,
            PartitionAccessReasonV1.MEMBER_SCOPE_MISMATCH,
            experiment.phase,
            empty,
        )
    if experiment.train_access_count < schedule.release_after_train_access_count:
        return (
            refused,
            PartitionAccessReasonV1.VALIDATION_NOT_RELEASED,
            experiment.phase,
            empty,
        )
    if experiment.validation_access_count(schedule_id) >= schedule.max_access_count:
        return (
            refused,
            PartitionAccessReasonV1.VALIDATION_BUDGET_EXHAUSTED,
            experiment.phase,
            empty,
        )
    return (
        PartitionAccessDecisionV1.GRANTED,
        PartitionAccessReasonV1.GRANTED,
        experiment.phase,
        requested_members,
    )


def _append_access_state(
    experiment: StrategyDiscoveryExperimentV1,
    record: PartitionAccessRecordV1,
    *,
    validation_schedule_id: str | None,
) -> StrategyDiscoveryExperimentV1:
    granted = record.decision is PartitionAccessDecisionV1.GRANTED
    train_count = experiment.train_access_count
    validation_counts = {
        row.schedule_id: row.count for row in experiment.validation_access_counts
    }
    if granted and record.purpose is PartitionAccessPurposeV1.SEARCH_TRAIN:
        train_count += 1
    if granted and record.purpose is PartitionAccessPurposeV1.SEARCH_VALIDATION:
        if validation_schedule_id is None:
            raise AssertionError("granted validation access lacks its schedule")
        validation_counts[validation_schedule_id] = (
            validation_counts.get(validation_schedule_id, 0) + 1
        )
    reveal = (
        granted and record.purpose is PartitionAccessPurposeV1.HOLDOUT_REVEAL
    )
    return replace(
        experiment,
        phase=record.phase_after,
        train_access_count=train_count,
        validation_access_counts=tuple(
            ValidationAccessCountV1(schedule_id, count)
            for schedule_id, count in sorted(validation_counts.items())
        ),
        access_record_sha256=(
            *experiment.access_record_sha256,
            record.access_sha256,
        ),
        reveal_access_sha256=(
            record.access_sha256 if reveal else experiment.reveal_access_sha256
        ),
        terminal_outcome=(
            TerminalEvaluationOutcomeV1.PENDING
            if reveal
            else experiment.terminal_outcome
        ),
    )


def _require_binding(
    manifest: PartitionManifestV1,
    experiment: StrategyDiscoveryExperimentV1,
) -> None:
    if not isinstance(manifest, PartitionManifestV1):
        raise TypeError("partition access requires a typed manifest")
    if not isinstance(experiment, StrategyDiscoveryExperimentV1):
        raise TypeError("partition access requires a typed experiment")
    if (
        manifest.experiment_id != experiment.experiment_id
        or manifest.experiment_version != experiment.experiment_version
        or manifest.manifest_sha256 != experiment.partition_manifest_sha256
    ):
        raise ValueError("partition access manifest and experiment do not match")


def _require_sha256(value: object, context: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be lowercase SHA-256")


def _require_member_ids(value: object, context: str) -> None:
    if type(value) is not tuple or any(
        type(item) is not str or not item for item in value
    ):
        raise TypeError(f"{context} must be a tuple of nonempty strings")
    if tuple(sorted(set(value))) != value:
        raise ValueError(f"{context} must be unique and canonically sorted")


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


def _nullable_text(payload: dict[str, object], key: str) -> str | None:
    value = payload[key]
    if value is not None and type(value) is not str:
        raise TypeError(f"{key} must be text or null")
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


__all__ = [
    "PARTITION_ACCESS_SCHEMA_ID_V1",
    "PARTITION_ACCESS_SCHEMA_VERSION_V1",
    "PartitionAccessDecisionV1",
    "PartitionAccessPurposeV1",
    "PartitionAccessReasonV1",
    "PartitionAccessRecordV1",
    "PartitionAccessResultV1",
    "request_partition_access",
]
