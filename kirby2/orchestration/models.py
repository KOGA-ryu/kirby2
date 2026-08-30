"""Immutable scientific-work and operational-attempt contracts for WO38-A.

This module is deliberately data-only.  A logical work unit contains every input
that can change the scientific result, while a work attempt contains only the
mutable operational history used to execute that logical unit.  Filesystem access,
network transport, worker execution, lease mutation, and dynamic code loading belong
to later orchestration cards.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar, TypeVar

from kirby2.immutable import freeze_json, thaw_json
from kirby2.orchestration.seeds import (
    MasterSeedIdentityV1,
    StableCellIdentityV1,
    derive_logical_cell_seed,
)


ORCHESTRATION_MODEL_SCHEMA_VERSION = 1
LOGICAL_WORK_CELL_SCHEMA_ID = "KIRBY2_LOGICAL_WORK_CELL_V1"
LOGICAL_WORK_UNIT_SCHEMA_ID = "KIRBY2_LOGICAL_WORK_UNIT_V1"
WORK_ATTEMPT_IDENTITY_SCHEMA_ID = "KIRBY2_WORK_ATTEMPT_IDENTITY_V1"
WORK_ATTEMPT_SCHEMA_ID = "KIRBY2_WORK_ATTEMPT_V1"
EXPERIMENT_WORK_PLAN_SCHEMA_ID = "KIRBY2_EXPERIMENT_WORK_PLAN_V1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_UTC_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z"
)
_MAX_SEED = (1 << 63) - 1
_MAX_ATTEMPT_NUMBER = (1 << 31) - 1
_EnumT = TypeVar("_EnumT", bound=Enum)

# These keys have operational meaning and therefore cannot hide inside the
# scientific configuration whose bytes define ``logical_work_unit_id``.  Market
# timestamps, event time, duration, and other domain parameters remain valid; only
# the coordinator/worker state named here is rejected.
_OPERATIONAL_CONFIGURATION_KEYS = frozenset(
    {
        "assigned_at_utc",
        "assigned_host_id",
        "attempt",
        "attempt_id",
        "attempt_number",
        "attempt_state",
        "attempt_status",
        "completed_at_utc",
        "completion_order",
        "coordinator_id",
        "diagnostics",
        "dispatch_order",
        "dispatch_sequence",
        "dispatched_at_utc",
        "executor_id",
        "finished_at_utc",
        "heartbeat",
        "heartbeat_sequence",
        "last_heartbeat_at_utc",
        "lease",
        "lease_deadline_utc",
        "lease_expires_at_utc",
        "lease_id",
        "lease_issued_at_utc",
        "lease_owner",
        "max_retries",
        "outcome",
        "pid",
        "process_count",
        "process_id",
        "process_index",
        "recorded_at_utc",
        "retry",
        "retry_backoff",
        "retry_count",
        "retry_delay",
        "retry_limit",
        "returned_artifact_sha256",
        "scheduler_id",
        "scheduler_state",
        "started_at_utc",
        "wall_clock",
        "wall_clock_time",
        "wall_clock_utc",
        "worker",
        "worker_count",
        "worker_id",
        "worker_ids",
        "worker_pool_size",
        "workers",
    }
)
_OPERATIONAL_CONFIGURATION_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _OPERATIONAL_CONFIGURATION_KEYS
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENTIFIER_TEXT = re.compile(r"[^a-z0-9]+")


class WorkKindV1(str, Enum):
    """Closed V1 inventory of independently distributable scientific work."""

    COMPLETE_RUN = "COMPLETE_RUN"
    COUNTERFACTUAL_BRANCH = "COUNTERFACTUAL_BRANCH"
    CALIBRATION = "CALIBRATION"
    STRATEGY_EVALUATION = "STRATEGY_EVALUATION"


class WorkAttemptOutcomeV1(str, Enum):
    """Closed operational state recorded by an immutable attempt snapshot."""

    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    QUARANTINED = "QUARANTINED"


_ATTEMPT_ARTIFACT_RULE_V1 = {
    WorkAttemptOutcomeV1.LEASED: False,
    WorkAttemptOutcomeV1.RUNNING: False,
    WorkAttemptOutcomeV1.SUCCEEDED: True,
    WorkAttemptOutcomeV1.FAILED: False,
    WorkAttemptOutcomeV1.EXPIRED: False,
    WorkAttemptOutcomeV1.CANCELLED: False,
    WorkAttemptOutcomeV1.QUARANTINED: True,
}


class _CanonicalRecordV1:
    """Small shared byte projection; subclasses retain exact typed loaders."""

    __slots__ = ()

    def as_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class DigestReferenceV1(_CanonicalRecordV1):
    """One unambiguous logical name bound to exact canonical content bytes."""

    name: str
    sha256: str

    def __post_init__(self) -> None:
        _identifier(self.name, "digest reference name")
        _sha256(self.sha256, "digest reference SHA-256")

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.name, self.sha256)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: object) -> DigestReferenceV1:
        payload = _exact_object(
            value,
            frozenset({"name", "sha256"}),
            "digest reference",
        )
        restored = cls(
            name=_exact_text(payload, "name"),
            sha256=_exact_text(payload, "sha256"),
        )
        _require_exact_round_trip(restored, payload, "digest reference")
        return restored


@dataclass(frozen=True, slots=True)
class LogicalWorkCellV1(_CanonicalRecordV1):
    """One planner input before deterministic seed derivation.

    ``partition_id`` and ``cell_id`` are the stable seed-derivation identity.  The
    full ``cell_sha256`` additionally binds work kind and scientific configuration,
    so changing either produces a different logical work-unit identity without
    making list order part of the seed policy.
    """

    partition_id: str
    cell_id: str
    work_kind: WorkKindV1
    configuration: Mapping[str, object]

    schema_id: ClassVar[str] = LOGICAL_WORK_CELL_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_MODEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.partition_id, "logical work partition ID")
        _identifier(self.cell_id, "logical work cell ID")
        if type(self.work_kind) is not WorkKindV1:
            raise TypeError("logical work kind must be WorkKindV1")
        frozen = _float_free_frozen_object(
            self.configuration,
            "logical work configuration",
            reject_operational_keys=True,
        )
        object.__setattr__(self, "configuration", frozen)

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.partition_id, self.cell_id)

    def stable_identity_dict(self) -> dict[str, object]:
        """Return the list-order-independent identity consumed by seed derivation."""

        return {
            "cell_id": self.cell_id,
            "partition_id": self.partition_id,
        }

    def identity_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "configuration": _detached_object(self.configuration),
            "partition_id": self.partition_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "work_kind": self.work_kind.value,
        }

    @property
    def cell_sha256(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "cell_sha256": self.cell_sha256}

    @classmethod
    def from_dict(cls, value: object) -> LogicalWorkCellV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "cell_id",
                    "cell_sha256",
                    "configuration",
                    "partition_id",
                    "schema_id",
                    "schema_version",
                    "work_kind",
                }
            ),
            "logical work cell",
        )
        _require_schema(payload, cls.schema_id, cls.schema_version, "logical work cell")
        declared = _sha256(payload["cell_sha256"], "declared logical cell digest")
        restored = cls(
            partition_id=_exact_text(payload, "partition_id"),
            cell_id=_exact_text(payload, "cell_id"),
            work_kind=_enum_value(
                WorkKindV1,
                payload["work_kind"],
                "logical work kind",
            ),
            configuration=_exact_mapping(payload["configuration"], "work configuration"),
        )
        if not hmac.compare_digest(declared, restored.cell_sha256):
            raise ValueError("declared logical cell digest differs from canonical content")
        _require_exact_round_trip(restored, payload, "logical work cell")
        return restored


@dataclass(frozen=True, slots=True)
class LogicalWorkUnit(_CanonicalRecordV1):
    """Complete scientific identity for one independently executable work unit.

    No field in this record describes an attempt, worker, lease, heartbeat, retry,
    or wall-clock observation.  Reissuing work therefore preserves
    ``logical_work_unit_id`` exactly.
    """

    experiment_identity: DigestReferenceV1
    cell: LogicalWorkCellV1
    scenario: DigestReferenceV1
    market_profile: DigestReferenceV1
    datasets: tuple[DigestReferenceV1, ...]
    strategies: tuple[DigestReferenceV1, ...]
    packs: tuple[DigestReferenceV1, ...]
    seed: int
    software_version: str
    source_version: str
    engine_identity: DigestReferenceV1
    runtime_identity: DigestReferenceV1
    dependency_identity: DigestReferenceV1
    compiler_identity: DigestReferenceV1
    schemas: tuple[DigestReferenceV1, ...]
    capabilities: tuple[DigestReferenceV1, ...]
    expected_outputs: tuple[DigestReferenceV1, ...]
    resource_class: str

    schema_id: ClassVar[str] = LOGICAL_WORK_UNIT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_MODEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.experiment_identity, "experiment identity"),
            (self.scenario, "scenario identity"),
            (self.market_profile, "market-profile identity"),
            (self.engine_identity, "engine identity"),
            (self.runtime_identity, "runtime identity"),
            (self.dependency_identity, "dependency identity"),
            (self.compiler_identity, "compiler identity"),
        ):
            if type(value) is not DigestReferenceV1:
                raise TypeError(f"logical work {label} must be DigestReferenceV1")
        if type(self.cell) is not LogicalWorkCellV1:
            raise TypeError("logical work cell must be LogicalWorkCellV1")
        _canonical_references(self.datasets, "logical work datasets")
        _canonical_references(self.strategies, "logical work strategies")
        _canonical_references(self.packs, "logical work packs")
        _canonical_references(
            self.schemas,
            "logical work schemas",
            require_nonempty=True,
        )
        _canonical_references(
            self.capabilities,
            "logical work capabilities",
            require_nonempty=True,
        )
        _canonical_references(
            self.expected_outputs,
            "logical work expected outputs",
            require_nonempty=True,
        )
        if type(self.seed) is not int or not 0 <= self.seed <= _MAX_SEED:
            raise ValueError("logical work seed must be an unsigned 63-bit integer")
        _identifier(self.software_version, "logical work software version")
        _identifier(self.source_version, "logical work source version")
        _identifier(self.resource_class, "logical work resource class")

    @property
    def partition_id(self) -> str:
        return self.cell.partition_id

    @property
    def cell_id(self) -> str:
        return self.cell.cell_id

    @property
    def work_kind(self) -> WorkKindV1:
        return self.cell.work_kind

    @property
    def configuration(self) -> Mapping[str, object]:
        return self.cell.configuration

    def identity_dict(self) -> dict[str, object]:
        return {
            "capabilities": [item.as_dict() for item in self.capabilities],
            "cell": self.cell.as_dict(),
            "compiler_identity": self.compiler_identity.as_dict(),
            "datasets": [item.as_dict() for item in self.datasets],
            "dependency_identity": self.dependency_identity.as_dict(),
            "engine_identity": self.engine_identity.as_dict(),
            "expected_outputs": [item.as_dict() for item in self.expected_outputs],
            "experiment_identity": self.experiment_identity.as_dict(),
            "market_profile": self.market_profile.as_dict(),
            "packs": [item.as_dict() for item in self.packs],
            "resource_class": self.resource_class,
            "runtime_identity": self.runtime_identity.as_dict(),
            "scenario": self.scenario.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "schemas": [item.as_dict() for item in self.schemas],
            "seed": self.seed,
            "software_version": self.software_version,
            "source_version": self.source_version,
            "strategies": [item.as_dict() for item in self.strategies],
        }

    @property
    def logical_work_unit_id(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "logical_work_unit_id": self.logical_work_unit_id,
        }

    @classmethod
    def from_cell(
        cls,
        *,
        experiment_identity: DigestReferenceV1,
        cell: LogicalWorkCellV1,
        scenario: DigestReferenceV1,
        market_profile: DigestReferenceV1,
        datasets: tuple[DigestReferenceV1, ...],
        strategies: tuple[DigestReferenceV1, ...],
        packs: tuple[DigestReferenceV1, ...],
        seed: int,
        software_version: str,
        source_version: str,
        engine_identity: DigestReferenceV1,
        runtime_identity: DigestReferenceV1,
        dependency_identity: DigestReferenceV1,
        compiler_identity: DigestReferenceV1,
        schemas: tuple[DigestReferenceV1, ...],
        capabilities: tuple[DigestReferenceV1, ...],
        expected_outputs: tuple[DigestReferenceV1, ...],
        resource_class: str,
    ) -> LogicalWorkUnit:
        """Construct the exact unit shape consumed by the deterministic planner."""

        return cls(
            experiment_identity=experiment_identity,
            cell=cell,
            scenario=scenario,
            market_profile=market_profile,
            datasets=datasets,
            strategies=strategies,
            packs=packs,
            seed=seed,
            software_version=software_version,
            source_version=source_version,
            engine_identity=engine_identity,
            runtime_identity=runtime_identity,
            dependency_identity=dependency_identity,
            compiler_identity=compiler_identity,
            schemas=schemas,
            capabilities=capabilities,
            expected_outputs=expected_outputs,
            resource_class=resource_class,
        )

    @classmethod
    def from_dict(cls, value: object) -> LogicalWorkUnit:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "capabilities",
                    "cell",
                    "compiler_identity",
                    "datasets",
                    "dependency_identity",
                    "engine_identity",
                    "expected_outputs",
                    "experiment_identity",
                    "logical_work_unit_id",
                    "market_profile",
                    "packs",
                    "resource_class",
                    "runtime_identity",
                    "scenario",
                    "schema_id",
                    "schema_version",
                    "schemas",
                    "seed",
                    "software_version",
                    "source_version",
                    "strategies",
                }
            ),
            "logical work unit",
        )
        _require_schema(payload, cls.schema_id, cls.schema_version, "logical work unit")
        declared = _sha256(
            payload["logical_work_unit_id"],
            "declared logical work-unit ID",
        )
        restored = cls(
            experiment_identity=DigestReferenceV1.from_dict(
                payload["experiment_identity"]
            ),
            cell=LogicalWorkCellV1.from_dict(payload["cell"]),
            scenario=DigestReferenceV1.from_dict(payload["scenario"]),
            market_profile=DigestReferenceV1.from_dict(payload["market_profile"]),
            datasets=_references_from_dict(payload["datasets"], "logical work datasets"),
            strategies=_references_from_dict(
                payload["strategies"],
                "logical work strategies",
            ),
            packs=_references_from_dict(payload["packs"], "logical work packs"),
            seed=_exact_integer(payload, "seed"),
            software_version=_exact_text(payload, "software_version"),
            source_version=_exact_text(payload, "source_version"),
            engine_identity=DigestReferenceV1.from_dict(payload["engine_identity"]),
            runtime_identity=DigestReferenceV1.from_dict(payload["runtime_identity"]),
            dependency_identity=DigestReferenceV1.from_dict(
                payload["dependency_identity"]
            ),
            compiler_identity=DigestReferenceV1.from_dict(
                payload["compiler_identity"]
            ),
            schemas=_references_from_dict(payload["schemas"], "logical work schemas"),
            capabilities=_references_from_dict(
                payload["capabilities"],
                "logical work capabilities",
            ),
            expected_outputs=_references_from_dict(
                payload["expected_outputs"],
                "logical work expected outputs",
            ),
            resource_class=_exact_text(payload, "resource_class"),
        )
        if not hmac.compare_digest(declared, restored.logical_work_unit_id):
            raise ValueError("declared logical work-unit ID differs from scientific identity")
        _require_exact_round_trip(restored, payload, "logical work unit")
        return restored


@dataclass(frozen=True, slots=True)
class WorkAttempt(_CanonicalRecordV1):
    """One immutable operational snapshot for an attempt to execute logical work.

    ``attempt_id`` hashes only logical ID plus attempt number.  ``record_sha256``
    hashes the complete operational snapshot.  Heartbeats can therefore produce new
    records without silently changing attempt identity.
    """

    logical_work_unit_id: str
    attempt_number: int
    worker_id: str
    lease_id: str
    lease_issued_at_utc: str
    lease_expires_at_utc: str
    heartbeat_sequence: int
    last_heartbeat_at_utc: str | None
    outcome: WorkAttemptOutcomeV1
    diagnostics: Mapping[str, object]
    returned_artifact_sha256: str | None
    recorded_at_utc: str

    schema_id: ClassVar[str] = WORK_ATTEMPT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_MODEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.logical_work_unit_id, "attempt logical work-unit ID")
        if (
            type(self.attempt_number) is not int
            or not 1 <= self.attempt_number <= _MAX_ATTEMPT_NUMBER
        ):
            raise ValueError("attempt number must be an integer from 1 through 2^31-1")
        _identifier(self.worker_id, "attempt worker ID")
        _identifier(self.lease_id, "attempt lease ID")
        issued = _utc_instant(self.lease_issued_at_utc, "lease issue time")
        expires = _utc_instant(self.lease_expires_at_utc, "lease expiry time")
        recorded = _utc_instant(self.recorded_at_utc, "attempt record time")
        if issued >= expires:
            raise ValueError("attempt lease must expire after it is issued")
        if recorded < issued:
            raise ValueError("attempt record time cannot precede lease issue")
        if type(self.heartbeat_sequence) is not int or self.heartbeat_sequence < 0:
            raise ValueError("attempt heartbeat sequence must be a nonnegative integer")
        heartbeat: datetime | None = None
        if self.last_heartbeat_at_utc is not None:
            heartbeat = _utc_instant(
                self.last_heartbeat_at_utc,
                "last heartbeat time",
            )
            if heartbeat < issued or heartbeat > expires or heartbeat > recorded:
                raise ValueError(
                    "attempt heartbeat must fall within its lease and precede record time"
                )
        if self.heartbeat_sequence == 0 and heartbeat is not None:
            raise ValueError("zero heartbeat sequence cannot carry a heartbeat time")
        if self.heartbeat_sequence > 0 and heartbeat is None:
            raise ValueError("positive heartbeat sequence requires a heartbeat time")
        if type(self.outcome) is not WorkAttemptOutcomeV1:
            raise TypeError("attempt outcome must be WorkAttemptOutcomeV1")
        if frozenset(_ATTEMPT_ARTIFACT_RULE_V1) != frozenset(WorkAttemptOutcomeV1):
            raise RuntimeError("attempt artifact policy does not cover every outcome")
        needs_artifact = _ATTEMPT_ARTIFACT_RULE_V1[self.outcome]
        if self.returned_artifact_sha256 is not None:
            _sha256(
                self.returned_artifact_sha256,
                "returned attempt artifact digest",
            )
        if needs_artifact and self.returned_artifact_sha256 is None:
            raise ValueError(f"{self.outcome.value} attempt requires a returned artifact")
        if not needs_artifact and self.returned_artifact_sha256 is not None:
            raise ValueError(f"{self.outcome.value} attempt cannot carry a returned artifact")
        frozen = _float_free_frozen_object(
            self.diagnostics,
            "attempt diagnostics",
            reject_operational_keys=False,
        )
        object.__setattr__(self, "diagnostics", frozen)

    def attempt_identity_dict(self) -> dict[str, object]:
        return {
            "attempt_number": self.attempt_number,
            "logical_work_unit_id": self.logical_work_unit_id,
            "schema_id": WORK_ATTEMPT_IDENTITY_SCHEMA_ID,
            "schema_version": self.schema_version,
        }

    @property
    def attempt_id(self) -> str:
        return _canonical_sha256(self.attempt_identity_dict())

    def record_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "diagnostics": _detached_object(self.diagnostics),
            "heartbeat_sequence": self.heartbeat_sequence,
            "last_heartbeat_at_utc": self.last_heartbeat_at_utc,
            "lease_expires_at_utc": self.lease_expires_at_utc,
            "lease_id": self.lease_id,
            "lease_issued_at_utc": self.lease_issued_at_utc,
            "logical_work_unit_id": self.logical_work_unit_id,
            "outcome": self.outcome.value,
            "recorded_at_utc": self.recorded_at_utc,
            "returned_artifact_sha256": self.returned_artifact_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "worker_id": self.worker_id,
        }

    @property
    def record_sha256(self) -> str:
        return _canonical_sha256(self.record_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.record_dict(), "record_sha256": self.record_sha256}

    @classmethod
    def from_dict(cls, value: object) -> WorkAttempt:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "attempt_id",
                    "attempt_number",
                    "diagnostics",
                    "heartbeat_sequence",
                    "last_heartbeat_at_utc",
                    "lease_expires_at_utc",
                    "lease_id",
                    "lease_issued_at_utc",
                    "logical_work_unit_id",
                    "outcome",
                    "record_sha256",
                    "recorded_at_utc",
                    "returned_artifact_sha256",
                    "schema_id",
                    "schema_version",
                    "worker_id",
                }
            ),
            "work attempt",
        )
        _require_schema(payload, cls.schema_id, cls.schema_version, "work attempt")
        declared_attempt_id = _sha256(payload["attempt_id"], "declared attempt ID")
        declared_record_sha256 = _sha256(
            payload["record_sha256"],
            "declared attempt record digest",
        )
        restored = cls(
            logical_work_unit_id=_exact_text(payload, "logical_work_unit_id"),
            attempt_number=_exact_integer(payload, "attempt_number"),
            worker_id=_exact_text(payload, "worker_id"),
            lease_id=_exact_text(payload, "lease_id"),
            lease_issued_at_utc=_exact_text(payload, "lease_issued_at_utc"),
            lease_expires_at_utc=_exact_text(payload, "lease_expires_at_utc"),
            heartbeat_sequence=_exact_integer(payload, "heartbeat_sequence"),
            last_heartbeat_at_utc=_optional_exact_text(
                payload,
                "last_heartbeat_at_utc",
            ),
            outcome=_enum_value(
                WorkAttemptOutcomeV1,
                payload["outcome"],
                "work attempt outcome",
            ),
            diagnostics=_exact_mapping(payload["diagnostics"], "attempt diagnostics"),
            returned_artifact_sha256=_optional_exact_text(
                payload,
                "returned_artifact_sha256",
            ),
            recorded_at_utc=_exact_text(payload, "recorded_at_utc"),
        )
        if not hmac.compare_digest(declared_attempt_id, restored.attempt_id):
            raise ValueError("declared attempt ID differs from logical ID and number")
        if not hmac.compare_digest(declared_record_sha256, restored.record_sha256):
            raise ValueError("declared attempt record digest differs from operational record")
        _require_exact_round_trip(restored, payload, "work attempt")
        return restored


@dataclass(frozen=True, slots=True)
class ExperimentWorkPlanV1(_CanonicalRecordV1):
    """One canonical experiment plan ordered only by logical work-unit ID."""

    master_seed_identity: MasterSeedIdentityV1
    experiment_identity: DigestReferenceV1
    logical_units: tuple[LogicalWorkUnit, ...]

    schema_id: ClassVar[str] = EXPERIMENT_WORK_PLAN_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_MODEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.master_seed_identity) is not MasterSeedIdentityV1:
            raise TypeError("work plan master-seed identity must be MasterSeedIdentityV1")
        if type(self.experiment_identity) is not DigestReferenceV1:
            raise TypeError("work plan experiment identity must be DigestReferenceV1")
        if type(self.logical_units) is not tuple or not self.logical_units:
            raise ValueError("work plan logical units must be a nonempty immutable tuple")
        if any(type(item) is not LogicalWorkUnit for item in self.logical_units):
            raise TypeError("work plan logical units must contain LogicalWorkUnit values")
        logical_ids = tuple(item.logical_work_unit_id for item in self.logical_units)
        if logical_ids != tuple(sorted(logical_ids)):
            raise ValueError("work plan logical units must use canonical logical-ID order")
        if len(logical_ids) != len(set(logical_ids)):
            raise ValueError("work plan logical work-unit IDs must be unique")
        stable_cells = tuple(
            (item.partition_id, item.cell_id) for item in self.logical_units
        )
        if len(stable_cells) != len(set(stable_cells)):
            raise ValueError("work plan stable partition/cell identities must be unique")
        seeds = tuple(item.seed for item in self.logical_units)
        if len(seeds) != len(set(seeds)):
            raise ValueError("work plan derived seeds must be unique")
        if any(
            item.experiment_identity != self.experiment_identity
            for item in self.logical_units
        ):
            raise ValueError("work plan units must bind the plan experiment identity")
        for item in self.logical_units:
            derivation = derive_logical_cell_seed(
                self.master_seed_identity,
                self.experiment_identity.sha256,
                StableCellIdentityV1(
                    partition_id=item.partition_id,
                    cell_id=item.cell_id,
                ),
            )
            if item.seed != derivation.derived_seed:
                raise ValueError(
                    "work plan unit seed differs from the canonical master-seed, "
                    "experiment, and stable-cell identities"
                )

    def identity_dict(self) -> dict[str, object]:
        return {
            "experiment_identity": self.experiment_identity.as_dict(),
            "logical_units": [item.as_dict() for item in self.logical_units],
            "master_seed_identity": self.master_seed_identity.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def plan_id(self) -> str:
        return _canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "plan_id": self.plan_id}

    @classmethod
    def from_dict(cls, value: object) -> ExperimentWorkPlanV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "experiment_identity",
                    "logical_units",
                    "master_seed_identity",
                    "plan_id",
                    "schema_id",
                    "schema_version",
                }
            ),
            "experiment work plan",
        )
        _require_schema(
            payload,
            cls.schema_id,
            cls.schema_version,
            "experiment work plan",
        )
        declared = _sha256(payload["plan_id"], "declared experiment work-plan ID")
        restored = cls(
            master_seed_identity=MasterSeedIdentityV1.from_dict(
                payload["master_seed_identity"]
            ),
            experiment_identity=DigestReferenceV1.from_dict(
                payload["experiment_identity"]
            ),
            logical_units=tuple(
                LogicalWorkUnit.from_dict(item)
                for item in _exact_array(payload["logical_units"], "work plan units")
            ),
        )
        if not hmac.compare_digest(declared, restored.plan_id):
            raise ValueError("declared work-plan ID differs from canonical plan content")
        _require_exact_round_trip(restored, payload, "experiment work plan")
        return restored


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("orchestration record is not strict canonical JSON") from error


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical data identifier")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must be NFC-normalized")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _utc_instant(value: object, label: str) -> datetime:
    if type(value) is not str or _UTC_INSTANT.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be UTC text with whole seconds or six microsecond digits"
        )
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a real UTC instant") from error


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} field names must be exact text")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(expected - actual)} unknown={sorted(actual - expected)}"
        )
    return value


def _exact_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"serialized {label} must be an exact array")
    return value


def _exact_mapping(value: object, label: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    return value


def _exact_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _optional_exact_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text or null")
    return value


def _exact_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"serialized {key} must be an exact integer")
    return value


def _enum_value(
    enum_type: type[_EnumT],
    value: object,
    label: str,
) -> _EnumT:
    if type(value) is not str:
        raise TypeError(f"serialized {label} must be exact text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"serialized {label} is unsupported") from error


def _require_schema(
    payload: Mapping[str, object],
    schema_id: str,
    schema_version: int,
    label: str,
) -> None:
    if (
        type(payload["schema_id"]) is not str
        or payload["schema_id"] != schema_id
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != schema_version
    ):
        raise ValueError(f"serialized {label} schema differs from the V1 contract")


def _canonical_references(
    values: tuple[DigestReferenceV1, ...],
    label: str,
    *,
    require_nonempty: bool = False,
) -> None:
    if type(values) is not tuple or any(type(item) is not DigestReferenceV1 for item in values):
        raise TypeError(f"{label} must be an immutable DigestReferenceV1 tuple")
    if require_nonempty and not values:
        raise ValueError(f"{label} cannot be empty")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError(f"{label} must use canonical name/digest order")
    names = tuple(item.name for item in values)
    if len(names) != len(set(names)):
        raise ValueError(f"{label} cannot contain ambiguous duplicate names")


def _references_from_dict(
    value: object,
    label: str,
) -> tuple[DigestReferenceV1, ...]:
    return tuple(
        DigestReferenceV1.from_dict(item) for item in _exact_array(value, label)
    )


def _normalized_configuration_key(value: str) -> str:
    expanded = _CAMEL_CASE_BOUNDARY.sub("_", value)
    return _NON_IDENTIFIER_TEXT.sub("_", expanded.casefold()).strip("_")


def _is_operational_configuration_key(value: str) -> bool:
    normalized = _normalized_configuration_key(value)
    if normalized in _OPERATIONAL_CONFIGURATION_KEYS:
        return True
    compact = normalized.replace("_", "")
    return compact in _OPERATIONAL_CONFIGURATION_COMPACT_KEYS


def _validate_float_free_json(
    value: object,
    label: str,
    *,
    reject_operational_keys: bool,
    active: set[int],
) -> None:
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str and value != unicodedata.normalize("NFC", value):
            raise ValueError(f"{label} text must be NFC-normalized")
        return
    if type(value) is float:
        raise TypeError(f"{label} must use integer or textual exact quantities, not floats")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} must not contain reference cycles")
        active.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError(f"{label} object keys must be exact text")
                if (
                    not key
                    or key != unicodedata.normalize("NFC", key)
                    or any(ord(character) < 0x20 or ord(character) == 0x7F for character in key)
                ):
                    raise ValueError(f"{label} object keys must be nonempty canonical text")
                if reject_operational_keys and _is_operational_configuration_key(key):
                    raise ValueError(
                        f"{label} contains operational key outside scientific identity: {key!r}"
                    )
                _validate_float_free_json(
                    item,
                    f"{label}.{key}",
                    reject_operational_keys=reject_operational_keys,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} must not contain reference cycles")
        active.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_float_free_json(
                    item,
                    f"{label}[{index}]",
                    reject_operational_keys=reject_operational_keys,
                    active=active,
                )
        finally:
            active.remove(identity)
        return
    raise TypeError(f"{label} contains unsupported value type {type(value).__name__}")


def _float_free_frozen_object(
    value: object,
    label: str,
    *,
    reject_operational_keys: bool,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    _validate_float_free_json(
        value,
        label,
        reject_operational_keys=reject_operational_keys,
        active=set(),
    )
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise RuntimeError(f"{label} lost its object shape during immutable detachment")
    return frozen


def _detached_object(value: Mapping[str, object]) -> dict[str, object]:
    detached = thaw_json(value)
    if type(detached) is not dict:
        raise RuntimeError("immutable orchestration object lost its mapping shape")
    return detached


def _require_exact_round_trip(
    record: _CanonicalRecordV1,
    payload: Mapping[str, object],
    label: str,
) -> None:
    if record.as_dict() != payload:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "DigestReferenceV1",
    "EXPERIMENT_WORK_PLAN_SCHEMA_ID",
    "ExperimentWorkPlanV1",
    "LOGICAL_WORK_CELL_SCHEMA_ID",
    "LOGICAL_WORK_UNIT_SCHEMA_ID",
    "LogicalWorkCellV1",
    "LogicalWorkUnit",
    "ORCHESTRATION_MODEL_SCHEMA_VERSION",
    "WORK_ATTEMPT_IDENTITY_SCHEMA_ID",
    "WORK_ATTEMPT_SCHEMA_ID",
    "WorkAttempt",
    "WorkAttemptOutcomeV1",
    "WorkKindV1",
]
