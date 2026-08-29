"""Canonical sealed data partitions for strategy-discovery experiments."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .identity import canonical_identity_bytes


PARTITION_MANIFEST_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_PARTITION_MANIFEST_V1"
PARTITION_MANIFEST_SCHEMA_VERSION_V1 = 1
PARTITION_MANIFEST_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_PARTITION_MANIFEST_V1\x00"
NOT_APPLICABLE_V1 = "NOT_APPLICABLE"
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrategyPartitionV1(str, Enum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"
    ADVERSARIAL_HOLDOUT = "ADVERSARIAL_HOLDOUT"
    ROBUSTNESS = "ROBUSTNESS"

    @property
    def canonical_rank(self) -> int:
        return _PARTITION_ORDER.index(self)


_PARTITION_ORDER = (
    StrategyPartitionV1.TRAIN,
    StrategyPartitionV1.VALIDATION,
    StrategyPartitionV1.HOLDOUT,
    StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
    StrategyPartitionV1.ROBUSTNESS,
)
_REQUIRED_PARTITIONS = frozenset(
    {
        StrategyPartitionV1.TRAIN,
        StrategyPartitionV1.VALIDATION,
        StrategyPartitionV1.HOLDOUT,
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
    }
)
_SEALED_PARTITIONS = (
    StrategyPartitionV1.HOLDOUT,
    StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
    StrategyPartitionV1.ROBUSTNESS,
)


class HistoricalPeriodStatusV1(str, Enum):
    INTERVAL = "INTERVAL"
    NOT_APPLICABLE = NOT_APPLICABLE_V1


@dataclass(frozen=True, slots=True)
class HistoricalPeriodV1:
    status: HistoricalPeriodStatusV1
    start_ns: int | None
    end_ns: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HistoricalPeriodStatusV1):
            raise TypeError("historical-period status is invalid")
        if self.status is HistoricalPeriodStatusV1.NOT_APPLICABLE:
            if self.start_ns is not None or self.end_ns is not None:
                raise ValueError("not-applicable historical period cannot carry bounds")
            return
        if (
            type(self.start_ns) is not int
            or type(self.end_ns) is not int
            or self.start_ns < 0
            or self.end_ns <= self.start_ns
        ):
            raise ValueError("historical period must be a positive half-open interval")

    @classmethod
    def not_applicable(cls) -> HistoricalPeriodV1:
        return cls(HistoricalPeriodStatusV1.NOT_APPLICABLE, None, None)

    @classmethod
    def interval(cls, start_ns: int, end_ns: int) -> HistoricalPeriodV1:
        return cls(HistoricalPeriodStatusV1.INTERVAL, start_ns, end_ns)

    def as_dict(self) -> dict[str, object]:
        return {
            "end_ns": self.end_ns,
            "start_ns": self.start_ns,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> HistoricalPeriodV1:
        row = _exact_object(
            payload,
            {"end_ns", "start_ns", "status"},
            "historical period",
        )
        status = HistoricalPeriodStatusV1(_exact_str(row, "status"))
        start = row["start_ns"]
        end = row["end_ns"]
        if start is not None and type(start) is not int:
            raise TypeError("historical period start must be an integer or null")
        if end is not None and type(end) is not int:
            raise TypeError("historical period end must be an integer or null")
        return cls(status, start, end)


@dataclass(frozen=True, slots=True)
class PartitionMemberV1:
    member_id: str
    partition: StrategyPartitionV1
    source_day: str
    scenario_family: str
    historical_period: HistoricalPeriodV1
    seed: int
    dataset_sha256: str
    independence_group_sha256: str
    extracted_window_ancestry_sha256: tuple[str, ...]
    branch_ancestry_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.member_id, "partition member ID")
        if not isinstance(self.partition, StrategyPartitionV1):
            raise TypeError("strategy partition kind is invalid")
        _require_source_day(self.source_day)
        _require_identifier(self.scenario_family, "scenario family")
        if not isinstance(self.historical_period, HistoricalPeriodV1):
            raise TypeError("partition historical period is invalid")
        if (self.source_day == NOT_APPLICABLE_V1) != (
            self.historical_period.status
            is HistoricalPeriodStatusV1.NOT_APPLICABLE
        ):
            raise ValueError(
                "source day and historical period must agree on NOT_APPLICABLE"
            )
        if type(self.seed) is not int or not 0 <= self.seed <= (1 << 64) - 1:
            raise ValueError("partition seed must be an unsigned 64-bit integer")
        _require_sha256(self.dataset_sha256, "partition dataset digest")
        _require_sha256(
            self.independence_group_sha256,
            "partition independence-group digest",
        )
        _require_digest_chain(
            self.extracted_window_ancestry_sha256,
            "extracted-window ancestry",
        )
        _require_digest_chain(self.branch_ancestry_sha256, "branch ancestry")

    @property
    def independence_tokens(self) -> frozenset[str]:
        return frozenset(
            {
                self.independence_group_sha256,
                *self.extracted_window_ancestry_sha256,
                *self.branch_ancestry_sha256,
            }
        )

    @property
    def member_sha256(self) -> str:
        return hashlib.sha256(canonical_identity_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "branch_ancestry_sha256": list(self.branch_ancestry_sha256),
            "dataset_sha256": self.dataset_sha256,
            "extracted_window_ancestry_sha256": list(
                self.extracted_window_ancestry_sha256
            ),
            "historical_period": self.historical_period.as_dict(),
            "independence_group_sha256": self.independence_group_sha256,
            "member_id": self.member_id,
            "partition": self.partition.value,
            "scenario_family": self.scenario_family,
            "seed": self.seed,
            "source_day": self.source_day,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PartitionMemberV1:
        row = _exact_object(
            payload,
            {
                "branch_ancestry_sha256",
                "dataset_sha256",
                "extracted_window_ancestry_sha256",
                "historical_period",
                "independence_group_sha256",
                "member_id",
                "partition",
                "scenario_family",
                "seed",
                "source_day",
            },
            "partition member",
        )
        return cls(
            member_id=_exact_str(row, "member_id"),
            partition=StrategyPartitionV1(_exact_str(row, "partition")),
            source_day=_exact_str(row, "source_day"),
            scenario_family=_exact_str(row, "scenario_family"),
            historical_period=HistoricalPeriodV1.from_dict(row["historical_period"]),
            seed=_exact_int(row, "seed"),
            dataset_sha256=_exact_str(row, "dataset_sha256"),
            independence_group_sha256=_exact_str(
                row,
                "independence_group_sha256",
            ),
            extracted_window_ancestry_sha256=_string_tuple(
                row["extracted_window_ancestry_sha256"],
                "extracted-window ancestry",
            ),
            branch_ancestry_sha256=_string_tuple(
                row["branch_ancestry_sha256"],
                "branch ancestry",
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationScheduleV1:
    schedule_id: str
    member_ids: tuple[str, ...]
    release_after_train_access_count: int
    max_access_count: int

    def __post_init__(self) -> None:
        _require_identifier(self.schedule_id, "validation schedule ID")
        if type(self.member_ids) is not tuple or not self.member_ids:
            raise ValueError("validation schedule member IDs must be a nonempty tuple")
        for member_id in self.member_ids:
            _require_identifier(member_id, "validation schedule member ID")
        canonical_members = tuple(sorted(set(self.member_ids)))
        if len(canonical_members) != len(self.member_ids):
            raise ValueError("validation schedule member IDs must be unique")
        if (
            type(self.release_after_train_access_count) is not int
            or self.release_after_train_access_count < 0
            or type(self.max_access_count) is not int
            or self.max_access_count <= 0
        ):
            raise ValueError("validation schedule counts are invalid")
        object.__setattr__(self, "member_ids", canonical_members)

    def as_dict(self) -> dict[str, object]:
        return {
            "max_access_count": self.max_access_count,
            "member_ids": list(self.member_ids),
            "release_after_train_access_count": self.release_after_train_access_count,
            "schedule_id": self.schedule_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ValidationScheduleV1:
        row = _exact_object(
            payload,
            {
                "max_access_count",
                "member_ids",
                "release_after_train_access_count",
                "schedule_id",
            },
            "validation schedule",
        )
        return cls(
            schedule_id=_exact_str(row, "schedule_id"),
            member_ids=_string_tuple(row["member_ids"], "validation member IDs"),
            release_after_train_access_count=_exact_int(
                row,
                "release_after_train_access_count",
            ),
            max_access_count=_exact_int(row, "max_access_count"),
        )


@dataclass(frozen=True, slots=True)
class PartitionManifestV1:
    experiment_id: str
    experiment_version: int
    members: tuple[PartitionMemberV1, ...]
    validation_schedule: tuple[ValidationScheduleV1, ...]
    schema_version: int = PARTITION_MANIFEST_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_identifier(self.experiment_id, "partition experiment ID")
        if type(self.experiment_version) is not int or self.experiment_version <= 0:
            raise ValueError("partition experiment version must be positive")
        if (
            type(self.schema_version) is not int
            or self.schema_version != PARTITION_MANIFEST_SCHEMA_VERSION_V1
        ):
            raise ValueError("unsupported strategy partition manifest schema")
        if type(self.members) is not tuple or any(
            not isinstance(member, PartitionMemberV1) for member in self.members
        ):
            raise TypeError("partition manifest members must be typed tuples")
        members = tuple(
            sorted(
                self.members,
                key=lambda item: (item.partition.canonical_rank, item.member_id),
            )
        )
        member_ids = tuple(member.member_id for member in members)
        if not members or len(member_ids) != len(set(member_ids)):
            raise ValueError("partition member IDs must be nonempty and unique")
        seeds = tuple(member.seed for member in members)
        if len(seeds) != len(set(seeds)):
            raise ValueError("partition seeds must be globally unique")
        present = {member.partition for member in members}
        missing = sorted(item.value for item in _REQUIRED_PARTITIONS - present)
        if missing:
            raise ValueError(f"partition manifest is missing required partitions: {missing}")
        _validate_independence(members)
        if type(self.validation_schedule) is not tuple or any(
            not isinstance(row, ValidationScheduleV1)
            for row in self.validation_schedule
        ):
            raise TypeError("validation schedule must be a typed tuple")
        schedules = tuple(sorted(self.validation_schedule, key=lambda row: row.schedule_id))
        schedule_ids = tuple(row.schedule_id for row in schedules)
        if not schedules or len(schedule_ids) != len(set(schedule_ids)):
            raise ValueError("validation schedules must be nonempty and uniquely named")
        member_by_id = {member.member_id: member for member in members}
        for schedule in schedules:
            for member_id in schedule.member_ids:
                member = member_by_id.get(member_id)
                if member is None or member.partition is not StrategyPartitionV1.VALIDATION:
                    raise ValueError(
                        "validation schedule references a non-validation member"
                    )
        validation_ids = {
            member.member_id
            for member in members
            if member.partition is StrategyPartitionV1.VALIDATION
        }
        scheduled_ids = {
            member_id for schedule in schedules for member_id in schedule.member_ids
        }
        if scheduled_ids != validation_ids:
            raise ValueError("validation schedule must cover every validation member")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "validation_schedule", schedules)

    @property
    def manifest_sha256(self) -> str:
        raw = self.canonical_bytes()
        digest = hashlib.sha256()
        digest.update(PARTITION_MANIFEST_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    @property
    def manifest_id(self) -> str:
        return "strategy-partitions-" + self.manifest_sha256

    def partition_members(
        self,
        partition: StrategyPartitionV1,
    ) -> tuple[PartitionMemberV1, ...]:
        if not isinstance(partition, StrategyPartitionV1):
            raise TypeError("partition selection is invalid")
        return tuple(member for member in self.members if member.partition is partition)

    def member(self, member_id: str) -> PartitionMemberV1:
        for member in self.members:
            if member.member_id == member_id:
                return member
        raise KeyError(member_id)

    def schedule(self, schedule_id: str) -> ValidationScheduleV1:
        for schedule in self.validation_schedule:
            if schedule.schedule_id == schedule_id:
                return schedule
        raise KeyError(schedule_id)

    def search_view(self) -> dict[str, object]:
        sealed: dict[str, object] = {}
        for partition in _SEALED_PARTITIONS:
            members = self.partition_members(partition)
            sealed[partition.value] = {
                "member_count": len(members),
                "members_sha256": hashlib.sha256(
                    canonical_identity_bytes([member.as_dict() for member in members])
                ).hexdigest(),
            }
        return {
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "partition_manifest_sha256": self.manifest_sha256,
            "sealed_partitions": sealed,
            "train_members": [
                member.as_dict()
                for member in self.partition_members(StrategyPartitionV1.TRAIN)
            ],
            "validation_schedule": [
                schedule.as_dict() for schedule in self.validation_schedule
            ],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "members": [member.as_dict() for member in self.members],
            "schema_id": PARTITION_MANIFEST_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "validation_schedule": [
                schedule.as_dict() for schedule in self.validation_schedule
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> PartitionManifestV1:
        row = _exact_object(
            payload,
            {
                "experiment_id",
                "experiment_version",
                "members",
                "schema_id",
                "schema_version",
                "validation_schedule",
            },
            "partition manifest",
        )
        if _exact_str(row, "schema_id") != PARTITION_MANIFEST_SCHEMA_ID_V1:
            raise ValueError("unsupported strategy partition manifest schema ID")
        raw_members = _object_array(row["members"], "partition members")
        raw_schedule = _object_array(
            row["validation_schedule"],
            "validation schedule",
        )
        return cls(
            experiment_id=_exact_str(row, "experiment_id"),
            experiment_version=_exact_int(row, "experiment_version"),
            members=tuple(PartitionMemberV1.from_dict(item) for item in raw_members),
            validation_schedule=tuple(
                ValidationScheduleV1.from_dict(item) for item in raw_schedule
            ),
            schema_version=_exact_int(row, "schema_version"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> PartitionManifestV1:
        import json

        if type(raw) is not bytes:
            raise TypeError("partition manifest payload must be exact bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("partition manifest must be valid UTF-8 JSON") from error
        manifest = cls.from_dict(payload)
        if manifest.canonical_bytes() != raw:
            raise ValueError("partition manifest bytes are not canonical")
        return manifest


def partition_manifest_round_trip(manifest: PartitionManifestV1) -> PartitionManifestV1:
    if not isinstance(manifest, PartitionManifestV1):
        raise TypeError("partition manifest round trip requires PartitionManifestV1")
    return PartitionManifestV1.from_json_bytes(manifest.canonical_bytes())


def _validate_independence(members: tuple[PartitionMemberV1, ...]) -> None:
    token_owner: dict[str, StrategyPartitionV1] = {}
    for member in members:
        for token in sorted(member.independence_tokens):
            owner = token_owner.setdefault(token, member.partition)
            if owner is not member.partition:
                raise ValueError(
                    "related window or branch ancestry crosses a partition boundary"
                )


def _require_source_day(value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError("partition source day must be explicit text")
    if value == NOT_APPLICABLE_V1:
        return
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("partition source day must be ISO date or NOT_APPLICABLE") from error


def _require_identifier(value: object, context: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{context} is invalid")


def _require_sha256(value: object, context: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be lowercase SHA-256")


def _require_digest_chain(values: object, context: str) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{context} must be a nonempty tuple")
    for value in values:
        _require_sha256(value, f"{context} member")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} members must be unique")


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


def _exact_str(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _exact_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise TypeError(f"{context} must be an array of strings")
    return tuple(value)


def _object_array(value: object, context: str) -> list[dict[str, object]]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise TypeError(f"{context} must be an array of objects")
    return value


__all__ = [
    "HistoricalPeriodStatusV1",
    "HistoricalPeriodV1",
    "NOT_APPLICABLE_V1",
    "PARTITION_MANIFEST_DIGEST_DOMAIN_V1",
    "PARTITION_MANIFEST_SCHEMA_ID_V1",
    "PARTITION_MANIFEST_SCHEMA_VERSION_V1",
    "PartitionManifestV1",
    "PartitionMemberV1",
    "StrategyPartitionV1",
    "ValidationScheduleV1",
    "partition_manifest_round_trip",
]
