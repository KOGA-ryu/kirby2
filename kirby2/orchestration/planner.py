"""Pure deterministic planning policy for WO38-A.

This module decomposes scientific work and reduces successful-attempt history.  It
does not assign workers, create queues, mutate leases, execute work, persist state,
or communicate over a network.  Operational backends consume these immutable
decisions in later work orders.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from kirby2.orchestration.models import (
    DigestReferenceV1,
    ExperimentWorkPlanV1,
    LogicalWorkCellV1,
    LogicalWorkUnit,
    WorkAttempt,
    WorkAttemptOutcomeV1,
)
from kirby2.orchestration.seeds import (
    MasterSeedIdentityV1,
    SeedDerivationCollisionError,
    StableCellIdentityV1,
    derive_logical_cell_seed,
)


ORCHESTRATION_PLANNER_SCHEMA_VERSION = 1
ATTEMPT_RESULT_RESOLUTION_SCHEMA_ID = "KIRBY2_ATTEMPT_RESULT_RESOLUTION_V1"
COORDINATOR_RESPONSIBILITY_CONTRACT_SCHEMA_ID = (
    "KIRBY2_COORDINATOR_RESPONSIBILITY_CONTRACT_V1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CoordinatorResponsibilityV1(str, Enum):
    """Closed V1 sequence of responsibilities owned by a coordinator backend."""

    PARTITION_EXPERIMENT = "PARTITION_EXPERIMENT"
    DERIVE_LOGICAL_CELL_SEEDS = "DERIVE_LOGICAL_CELL_SEEDS"
    ENQUEUE_LOGICAL_WORK_UNITS = "ENQUEUE_LOGICAL_WORK_UNITS"
    ASSIGN_COMPATIBLE_WORKERS = "ASSIGN_COMPATIBLE_WORKERS"
    TRACK_OPERATIONAL_LEASES = "TRACK_OPERATIONAL_LEASES"
    VALIDATE_RETURNED_RESULTS_AND_ARTIFACTS = (
        "VALIDATE_RETURNED_RESULTS_AND_ARTIFACTS"
    )
    RETRY_OPERATIONAL_ATTEMPTS = "RETRY_OPERATIONAL_ATTEMPTS"
    AGGREGATE_IN_CANONICAL_ORDER = "AGGREGATE_IN_CANONICAL_ORDER"
    PERSIST_IMMUTABLE_RESULTS_AND_OPERATIONAL_HISTORY = (
        "PERSIST_IMMUTABLE_RESULTS_AND_OPERATIONAL_HISTORY"
    )


COORDINATOR_RESPONSIBILITY_SEQUENCE_V1 = (
    CoordinatorResponsibilityV1.PARTITION_EXPERIMENT,
    CoordinatorResponsibilityV1.DERIVE_LOGICAL_CELL_SEEDS,
    CoordinatorResponsibilityV1.ENQUEUE_LOGICAL_WORK_UNITS,
    CoordinatorResponsibilityV1.ASSIGN_COMPATIBLE_WORKERS,
    CoordinatorResponsibilityV1.TRACK_OPERATIONAL_LEASES,
    CoordinatorResponsibilityV1.VALIDATE_RETURNED_RESULTS_AND_ARTIFACTS,
    CoordinatorResponsibilityV1.RETRY_OPERATIONAL_ATTEMPTS,
    CoordinatorResponsibilityV1.AGGREGATE_IN_CANONICAL_ORDER,
    CoordinatorResponsibilityV1.PERSIST_IMMUTABLE_RESULTS_AND_OPERATIONAL_HISTORY,
)


@dataclass(frozen=True, slots=True)
class CoordinatorResponsibilityContractV1:
    """Data-only declaration of coordinator ownership; not an implementation."""

    responsibilities: tuple[CoordinatorResponsibilityV1, ...] = (
        COORDINATOR_RESPONSIBILITY_SEQUENCE_V1
    )

    schema_id: ClassVar[str] = COORDINATOR_RESPONSIBILITY_CONTRACT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PLANNER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.responsibilities) is not tuple or any(
            type(item) is not CoordinatorResponsibilityV1
            for item in self.responsibilities
        ):
            raise TypeError(
                "coordinator responsibilities must be an immutable V1 enum tuple"
            )
        if self.responsibilities != COORDINATOR_RESPONSIBILITY_SEQUENCE_V1:
            raise ValueError("the closed V1 coordinator sequence cannot be changed")

    def as_dict(self) -> dict[str, object]:
        return {
            "responsibilities": [item.value for item in self.responsibilities],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def contract_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> CoordinatorResponsibilityContractV1:
        payload = _exact_object(
            value,
            frozenset({"responsibilities", "schema_id", "schema_version"}),
            "coordinator responsibility contract",
        )
        _require_schema(
            payload,
            cls.schema_id,
            cls.schema_version,
            "coordinator responsibility contract",
        )
        restored = cls(
            responsibilities=tuple(
                _enum_value(
                    CoordinatorResponsibilityV1,
                    item,
                    "coordinator responsibility",
                )
                for item in _exact_array(
                    payload["responsibilities"],
                    "coordinator responsibilities",
                )
            )
        )
        _require_exact_round_trip(
            restored,
            payload,
            "coordinator responsibility contract",
        )
        return restored


COORDINATOR_RESPONSIBILITY_CONTRACT_V1 = CoordinatorResponsibilityContractV1()


class AttemptResultResolutionStatusV1(str, Enum):
    """Closed result of reducing successful operational attempts."""

    NO_SUCCESSFUL_RESULT = "NO_SUCCESSFUL_RESULT"
    RESULT_SELECTED = "RESULT_SELECTED"
    DETERMINISM_FAILURE = "DETERMINISM_FAILURE"


@dataclass(frozen=True, slots=True)
class AttemptResultResolutionV1:
    """Canonical, side-effect-free decision over one logical unit's attempts.

    Repeated records and repeated successful artifact identities are idempotent.
    When successful attempts disagree, every successful record is nominated for
    quarantine and no artifact is selected.
    """

    logical_work_unit_id: str
    status: AttemptResultResolutionStatusV1
    successful_attempt_record_sha256s: tuple[str, ...]
    successful_artifact_sha256s: tuple[str, ...]
    selected_artifact_sha256: str | None

    schema_id: ClassVar[str] = ATTEMPT_RESULT_RESOLUTION_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_PLANNER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.logical_work_unit_id, "resolution logical work-unit ID")
        if type(self.status) is not AttemptResultResolutionStatusV1:
            raise TypeError("attempt resolution status must be V1")
        _canonical_sha256_tuple(
            self.successful_attempt_record_sha256s,
            "successful attempt record identities",
        )
        _canonical_sha256_tuple(
            self.successful_artifact_sha256s,
            "successful artifact identities",
        )
        if self.selected_artifact_sha256 is not None:
            _require_sha256(
                self.selected_artifact_sha256,
                "selected artifact identity",
            )

        if self.status is AttemptResultResolutionStatusV1.NO_SUCCESSFUL_RESULT:
            if (
                self.successful_attempt_record_sha256s
                or self.successful_artifact_sha256s
                or self.selected_artifact_sha256 is not None
            ):
                raise ValueError("no-success resolution cannot contain a result")
            return
        if not self.successful_attempt_record_sha256s:
            raise ValueError("a successful resolution requires attempt evidence")
        if self.status is AttemptResultResolutionStatusV1.RESULT_SELECTED:
            if (
                len(self.successful_artifact_sha256s) != 1
                or self.selected_artifact_sha256
                != self.successful_artifact_sha256s[0]
            ):
                raise ValueError(
                    "selected resolution requires exactly one successful artifact identity"
                )
            return
        if self.status is AttemptResultResolutionStatusV1.DETERMINISM_FAILURE:
            if (
                len(self.successful_artifact_sha256s) < 2
                or len(self.successful_attempt_record_sha256s)
                < len(self.successful_artifact_sha256s)
                or self.selected_artifact_sha256 is not None
            ):
                raise ValueError(
                    "determinism failure requires attempt evidence for every "
                    "conflicting artifact and no selection"
                )
            return
        raise RuntimeError("attempt result resolution status is not exhaustively handled")

    @property
    def quarantined_attempt_record_sha256s(self) -> tuple[str, ...]:
        if self.status is AttemptResultResolutionStatusV1.DETERMINISM_FAILURE:
            return self.successful_attempt_record_sha256s
        return ()

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_work_unit_id": self.logical_work_unit_id,
            "quarantined_attempt_record_sha256s": list(
                self.quarantined_attempt_record_sha256s
            ),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "selected_artifact_sha256": self.selected_artifact_sha256,
            "status": self.status.value,
            "successful_artifact_sha256s": list(
                self.successful_artifact_sha256s
            ),
            "successful_attempt_record_sha256s": list(
                self.successful_attempt_record_sha256s
            ),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def resolution_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> AttemptResultResolutionV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "logical_work_unit_id",
                    "quarantined_attempt_record_sha256s",
                    "schema_id",
                    "schema_version",
                    "selected_artifact_sha256",
                    "status",
                    "successful_artifact_sha256s",
                    "successful_attempt_record_sha256s",
                }
            ),
            "attempt result resolution",
        )
        _require_schema(
            payload,
            cls.schema_id,
            cls.schema_version,
            "attempt result resolution",
        )
        restored = cls(
            logical_work_unit_id=_exact_text(payload, "logical_work_unit_id"),
            status=_enum_value(
                AttemptResultResolutionStatusV1,
                payload["status"],
                "attempt result resolution status",
            ),
            successful_attempt_record_sha256s=_exact_text_tuple(
                payload["successful_attempt_record_sha256s"],
                "successful attempt record identities",
            ),
            successful_artifact_sha256s=_exact_text_tuple(
                payload["successful_artifact_sha256s"],
                "successful artifact identities",
            ),
            selected_artifact_sha256=_optional_exact_text(
                payload,
                "selected_artifact_sha256",
            ),
        )
        _exact_text_tuple(
            payload["quarantined_attempt_record_sha256s"],
            "quarantined attempt record identities",
        )
        _require_exact_round_trip(restored, payload, "attempt result resolution")
        return restored


def snapshot_logical_cells(
    cells: Iterable[LogicalWorkCellV1],
) -> tuple[LogicalWorkCellV1, ...]:
    """Snapshot and validate planner cells without making input order semantic."""

    supplied = _snapshot_exact_values(
        cells,
        LogicalWorkCellV1,
        "logical work cells",
    )
    if not supplied:
        raise ValueError("an experiment work plan requires at least one cell")

    definitions: dict[tuple[str, str], str] = {}
    for cell in supplied:
        stable_identity = (cell.partition_id, cell.cell_id)
        incumbent = definitions.get(stable_identity)
        if incumbent is None:
            definitions[stable_identity] = cell.cell_sha256
            continue
        label = f"{cell.partition_id!r}/{cell.cell_id!r}"
        if incumbent == cell.cell_sha256:
            raise ValueError(f"duplicate logical cell identity {label}")
        raise ValueError(f"conflicting definitions for logical cell identity {label}")
    return supplied


def build_experiment_work_plan(
    *,
    master_seed_identity: MasterSeedIdentityV1,
    experiment_identity: DigestReferenceV1,
    cells: Iterable[LogicalWorkCellV1],
    scenario: DigestReferenceV1,
    market_profile: DigestReferenceV1,
    datasets: tuple[DigestReferenceV1, ...],
    strategies: tuple[DigestReferenceV1, ...],
    packs: tuple[DigestReferenceV1, ...],
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
) -> ExperimentWorkPlanV1:
    """Build a permutation-invariant plan from scientific identities only.

    There is deliberately no worker-count, worker identity, attempt, lease,
    completion-order, clock, queue, storage, or transport input.
    """

    if type(master_seed_identity) is not MasterSeedIdentityV1:
        raise TypeError("planning requires MasterSeedIdentityV1")
    if type(experiment_identity) is not DigestReferenceV1:
        raise TypeError("planning requires a DigestReferenceV1 experiment identity")
    cell_snapshot = snapshot_logical_cells(cells)

    units: list[LogicalWorkUnit] = []
    seed_owners: dict[int, StableCellIdentityV1] = {}
    for cell in cell_snapshot:
        stable_identity = StableCellIdentityV1(
            partition_id=cell.partition_id,
            cell_id=cell.cell_id,
        )
        derivation = derive_logical_cell_seed(
            master_seed_identity,
            experiment_identity.sha256,
            stable_identity,
        )
        incumbent = seed_owners.setdefault(
            derivation.derived_seed,
            stable_identity,
        )
        if incumbent != stable_identity:
            raise SeedDerivationCollisionError(
                "distinct stable cells collide after V1 63-bit seed truncation: "
                f"{incumbent.partition_id!r}/{incumbent.cell_id!r} and "
                f"{stable_identity.partition_id!r}/{stable_identity.cell_id!r}"
            )
        units.append(
            LogicalWorkUnit.from_cell(
                experiment_identity=experiment_identity,
                cell=cell,
                scenario=scenario,
                market_profile=market_profile,
                datasets=datasets,
                strategies=strategies,
                packs=packs,
                seed=derivation.derived_seed,
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
        )

    ordered_units = order_logical_work_units_for_aggregation(units)
    return ExperimentWorkPlanV1(
        master_seed_identity=master_seed_identity,
        experiment_identity=experiment_identity,
        logical_units=ordered_units,
    )


def order_logical_work_units_for_aggregation(
    logical_units: Iterable[LogicalWorkUnit],
) -> tuple[LogicalWorkUnit, ...]:
    """Return immutable work units sorted only by logical work-unit ID."""

    supplied = _snapshot_exact_values(
        logical_units,
        LogicalWorkUnit,
        "logical work units",
    )
    ordered = tuple(
        sorted(supplied, key=lambda unit: unit.logical_work_unit_id)
    )
    logical_ids = tuple(unit.logical_work_unit_id for unit in ordered)
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError("canonical aggregation cannot contain duplicate logical IDs")
    return ordered


def canonical_aggregation_order(
    logical_units: Iterable[LogicalWorkUnit],
) -> tuple[str, ...]:
    """Expose the canonical logical-ID sequence consumed by aggregation."""

    return tuple(
        unit.logical_work_unit_id
        for unit in order_logical_work_units_for_aggregation(logical_units)
    )


def resolve_successful_attempts(
    logical_work_unit_id: str,
    attempts: Iterable[WorkAttempt],
) -> AttemptResultResolutionV1:
    """Reduce attempt snapshots without using their arrival/completion order."""

    logical_id = _require_sha256(
        logical_work_unit_id,
        "resolved logical work-unit ID",
    )
    supplied = _snapshot_exact_values(attempts, WorkAttempt, "work attempts")
    for attempt in supplied:
        if attempt.logical_work_unit_id != logical_id:
            raise ValueError("attempt history spans more than one logical work unit")

    successful = tuple(
        attempt
        for attempt in supplied
        if attempt.outcome is WorkAttemptOutcomeV1.SUCCEEDED
    )
    if not successful:
        return AttemptResultResolutionV1(
            logical_work_unit_id=logical_id,
            status=AttemptResultResolutionStatusV1.NO_SUCCESSFUL_RESULT,
            successful_attempt_record_sha256s=(),
            successful_artifact_sha256s=(),
            selected_artifact_sha256=None,
        )

    artifact_identities: set[str] = set()
    for attempt in successful:
        artifact_identity = attempt.returned_artifact_sha256
        if artifact_identity is None:
            raise RuntimeError("SUCCEEDED attempt lost its required artifact identity")
        artifact_identities.add(artifact_identity)
    artifacts = tuple(sorted(artifact_identities))
    records = tuple(sorted({attempt.record_sha256 for attempt in successful}))
    if len(artifacts) == 1:
        return AttemptResultResolutionV1(
            logical_work_unit_id=logical_id,
            status=AttemptResultResolutionStatusV1.RESULT_SELECTED,
            successful_attempt_record_sha256s=records,
            successful_artifact_sha256s=artifacts,
            selected_artifact_sha256=artifacts[0],
        )
    return AttemptResultResolutionV1(
        logical_work_unit_id=logical_id,
        status=AttemptResultResolutionStatusV1.DETERMINISM_FAILURE,
        successful_attempt_record_sha256s=records,
        successful_artifact_sha256s=artifacts,
        selected_artifact_sha256=None,
    )


def _snapshot_exact_values(
    values: Iterable[object],
    expected_type: type[object],
    label: str,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"{label} must be an iterable of {expected_type.__name__}")
    try:
        supplied = tuple(values)
    except TypeError as error:
        raise TypeError(
            f"{label} must be an iterable of {expected_type.__name__}"
        ) from error
    if any(type(item) is not expected_type for item in supplied):
        raise TypeError(f"{label} must contain only {expected_type.__name__}")
    return supplied


def _canonical_sha256_tuple(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    for value in values:
        _require_sha256(value, label)
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be sorted and unique")


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


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


def _exact_text_tuple(value: object, label: str) -> tuple[str, ...]:
    supplied = _exact_array(value, label)
    if any(type(item) is not str for item in supplied):
        raise TypeError(f"serialized {label} must contain exact text")
    return tuple(supplied)


def _enum_value(
    enum_type: type[Enum],
    value: object,
    label: str,
) -> Enum:
    if type(value) is not str:
        raise TypeError(f"serialized {label} must be exact text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"serialized {label} is not supported") from error


def _require_schema(
    payload: Mapping[str, object],
    expected_id: str,
    expected_version: int,
    label: str,
) -> None:
    if _exact_text(payload, "schema_id") != expected_id:
        raise ValueError(f"serialized {label} schema ID is not supported")
    version = payload["schema_version"]
    if type(version) is not int or version != expected_version:
        raise ValueError(f"serialized {label} schema version is not supported")


def _require_exact_round_trip(
    record: CoordinatorResponsibilityContractV1 | AttemptResultResolutionV1,
    payload: Mapping[str, object],
    label: str,
) -> None:
    if record.as_dict() != payload:
        raise ValueError(f"serialized {label} did not round-trip exactly")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


__all__ = [
    "ATTEMPT_RESULT_RESOLUTION_SCHEMA_ID",
    "COORDINATOR_RESPONSIBILITY_CONTRACT_SCHEMA_ID",
    "COORDINATOR_RESPONSIBILITY_CONTRACT_V1",
    "COORDINATOR_RESPONSIBILITY_SEQUENCE_V1",
    "ORCHESTRATION_PLANNER_SCHEMA_VERSION",
    "AttemptResultResolutionStatusV1",
    "AttemptResultResolutionV1",
    "CoordinatorResponsibilityContractV1",
    "CoordinatorResponsibilityV1",
    "build_experiment_work_plan",
    "canonical_aggregation_order",
    "order_logical_work_units_for_aggregation",
    "resolve_successful_attempts",
    "snapshot_logical_cells",
]
