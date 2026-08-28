"""Neutral, strict-JSON component-state records for portable checkpoints.

This module owns only the serialized vocabulary shared by checkpoint producers,
consumers, and counterfactual compatibility adapters.  It deliberately does not
know how a composition is selected or how checkpoint artifacts are stored.
Callers must provide the exact frozen inventory, active set, dependency graph,
and composition-proven absence reasons to the inventory validator.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.full_day.models import (
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from kirby2.immutable import freeze_json, thaw_json


RUNTIME_COMPONENT_STATE_SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_ABSENT_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuntimeComponentStatusV1(str, Enum):
    """The only two truthful states of one checkpoint inventory record."""

    PRESERVED = "PRESERVED"
    ABSENT = "ABSENT"


def _exact_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} must be an integer >= 1")
    return value


def _exact_identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable identifier")
    validate_strict_json(value)
    return value


def _exact_absent_reason(value: object, field: str = "absent_reason") -> str:
    if type(value) is not str or _ABSENT_REASON_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an uppercase stable reason identifier")
    validate_strict_json(value)
    return value


def _exact_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _dependencies_from_wire(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError("dependencies must be a JSON array")
    return tuple(
        _exact_identifier(item, f"dependencies[{index}]")
        for index, item in enumerate(value)
    )


def _validate_dependencies(
    dependencies: object,
    *,
    component_id: str,
    field: str = "dependencies",
) -> tuple[str, ...]:
    if type(dependencies) is not tuple:
        raise TypeError(f"{field} must be an immutable tuple")
    checked = tuple(
        _exact_identifier(item, f"{field}[{index}]")
        for index, item in enumerate(dependencies)
    )
    if checked != tuple(sorted(checked)) or len(checked) != len(set(checked)):
        raise ValueError(f"{field} must be sorted and unique")
    if component_id in checked:
        raise ValueError("a runtime component cannot depend on itself")
    return checked


def _freeze_state(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("preserved component state must be a strict JSON object")
    validate_strict_json(value)
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise TypeError("preserved component state must be a strict JSON object")
    return frozen


@dataclass(frozen=True, slots=True)
class RuntimeComponentStateV1:
    """One exact, neutral component record in a portable runtime checkpoint.

    The serialized union is deliberately branch-specific.  A ``PRESERVED``
    record has state/version/dependency fields and no absence field.  An
    ``ABSENT`` record has only its composition-proven reason and cannot smuggle
    state, dependency, or compatibility claims into the wire representation.
    """

    schema_version: int
    component_id: str
    status: RuntimeComponentStatusV1
    component_schema_version: int | None = None
    implementation_version: int | None = None
    state: Mapping[str, object] | None = None
    state_sha256: str | None = None
    dependencies: tuple[str, ...] | None = None
    absent_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            _exact_positive_int(self.schema_version, "schema_version")
            != RUNTIME_COMPONENT_STATE_SCHEMA_VERSION
        ):
            raise ValueError("RuntimeComponentStateV1 schema_version must be 1")
        component_id = _exact_identifier(self.component_id, "component_id")
        if type(self.status) is not RuntimeComponentStatusV1:
            raise TypeError("status must use RuntimeComponentStatusV1")

        if self.status is RuntimeComponentStatusV1.PRESERVED:
            _exact_positive_int(
                self.component_schema_version, "component_schema_version"
            )
            _exact_positive_int(self.implementation_version, "implementation_version")
            frozen_state = _freeze_state(self.state)
            state_sha256 = _exact_sha256(self.state_sha256, "state_sha256")
            dependencies = _validate_dependencies(
                self.dependencies,
                component_id=component_id,
            )
            if self.absent_reason is not None:
                raise ValueError("a preserved component cannot carry absent_reason")
            if canonical_sha256(frozen_state) != state_sha256:
                raise ValueError("preserved component state digest mismatch")
            object.__setattr__(self, "state", frozen_state)
            object.__setattr__(self, "dependencies", dependencies)
            return

        if self.status is RuntimeComponentStatusV1.ABSENT:
            if any(
                value is not None
                for value in (
                    self.component_schema_version,
                    self.implementation_version,
                    self.state,
                    self.state_sha256,
                    self.dependencies,
                )
            ):
                raise ValueError(
                    "an absent component cannot carry state, version, digest, or dependencies"
                )
            _exact_absent_reason(self.absent_reason)
            return

        raise ValueError("runtime component status is unsupported")

    @classmethod
    def preserved(
        cls,
        *,
        component_id: str,
        component_schema_version: int,
        implementation_version: int,
        state: Mapping[str, object],
        dependencies: tuple[str, ...] = (),
    ) -> RuntimeComponentStateV1:
        """Build a preserved record and bind its digest to canonical state bytes."""

        frozen_state = _freeze_state(state)
        return cls(
            schema_version=RUNTIME_COMPONENT_STATE_SCHEMA_VERSION,
            component_id=component_id,
            status=RuntimeComponentStatusV1.PRESERVED,
            component_schema_version=component_schema_version,
            implementation_version=implementation_version,
            state=frozen_state,
            state_sha256=canonical_sha256(frozen_state),
            dependencies=dependencies,
        )

    @classmethod
    def absent(
        cls,
        *,
        component_id: str,
        absent_reason: str,
    ) -> RuntimeComponentStateV1:
        """Build an absent record from a caller-supplied composition proof code."""

        return cls(
            schema_version=RUNTIME_COMPONENT_STATE_SCHEMA_VERSION,
            component_id=component_id,
            status=RuntimeComponentStatusV1.ABSENT,
            absent_reason=absent_reason,
        )

    def as_dict(self) -> dict[str, object]:
        common: dict[str, object] = {
            "component_id": self.component_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }
        if self.status is RuntimeComponentStatusV1.PRESERVED:
            assert self.component_schema_version is not None
            assert self.implementation_version is not None
            assert self.state is not None
            assert self.state_sha256 is not None
            assert self.dependencies is not None
            common.update(
                {
                    "component_schema_version": self.component_schema_version,
                    "dependencies": list(self.dependencies),
                    "implementation_version": self.implementation_version,
                    "state": thaw_json(self.state),
                    "state_sha256": self.state_sha256,
                }
            )
        else:
            assert self.absent_reason is not None
            common["absent_reason"] = self.absent_reason
        return common

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> RuntimeComponentStateV1:
        validate_strict_json(payload)
        if not isinstance(payload, Mapping):
            raise TypeError("serialized RuntimeComponentStateV1 must be an object")
        if "status" not in payload:
            raise ValueError("serialized RuntimeComponentStateV1 is missing status")
        raw_status = payload["status"]
        if type(raw_status) is not str:
            raise TypeError("serialized runtime component status must be a string")
        try:
            status = RuntimeComponentStatusV1(raw_status)
        except ValueError as error:
            raise ValueError("runtime component status is unsupported") from error

        if status is RuntimeComponentStatusV1.PRESERVED:
            _require_exact_fields(
                payload,
                {
                    "component_id",
                    "component_schema_version",
                    "dependencies",
                    "implementation_version",
                    "schema_version",
                    "state",
                    "state_sha256",
                    "status",
                },
                "RuntimeComponentStateV1.PRESERVED",
            )
            state = payload["state"]
            if not isinstance(state, Mapping):
                raise TypeError("preserved component state must be a strict JSON object")
            return cls(
                schema_version=_exact_positive_int(
                    payload["schema_version"], "schema_version"
                ),
                component_id=_exact_identifier(payload["component_id"], "component_id"),
                status=status,
                component_schema_version=_exact_positive_int(
                    payload["component_schema_version"],
                    "component_schema_version",
                ),
                implementation_version=_exact_positive_int(
                    payload["implementation_version"], "implementation_version"
                ),
                state=state,
                state_sha256=_exact_sha256(payload["state_sha256"], "state_sha256"),
                dependencies=_dependencies_from_wire(payload["dependencies"]),
            )

        _require_exact_fields(
            payload,
            {"absent_reason", "component_id", "schema_version", "status"},
            "RuntimeComponentStateV1.ABSENT",
        )
        return cls(
            schema_version=_exact_positive_int(
                payload["schema_version"], "schema_version"
            ),
            component_id=_exact_identifier(payload["component_id"], "component_id"),
            status=status,
            absent_reason=_exact_absent_reason(payload["absent_reason"]),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> RuntimeComponentStateV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def _canonical_id_tuple(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{field} must be an immutable tuple")
    checked = tuple(
        _exact_identifier(item, f"{field}[{index}]")
        for index, item in enumerate(values)
    )
    if checked != tuple(sorted(checked)) or len(checked) != len(set(checked)):
        raise ValueError(f"{field} must be sorted and unique")
    return checked


def validate_runtime_component_inventory(
    records: tuple[RuntimeComponentStateV1, ...],
    *,
    expected_component_ids: tuple[str, ...],
    active_component_ids: tuple[str, ...],
    dependencies_by_component: Mapping[str, tuple[str, ...]],
    absent_reasons_by_component: Mapping[str, str],
) -> None:
    """Validate ordered component records against exact composition-derived truth.

    ``absent_reasons_by_component`` is the dependency-injected proof from the
    selected composition.  It must cover exactly the inactive inventory IDs;
    this neutral layer never guesses why a component is absent.
    """

    if type(records) is not tuple or any(
        type(record) is not RuntimeComponentStateV1 for record in records
    ):
        raise TypeError("runtime component records must be an immutable V1 tuple")
    expected_ids = _canonical_id_tuple(
        expected_component_ids, "expected_component_ids"
    )
    if not expected_ids:
        raise ValueError("expected_component_ids must not be empty")
    active_ids = _canonical_id_tuple(active_component_ids, "active_component_ids")
    expected_set = set(expected_ids)
    active_set = set(active_ids)
    if not active_set <= expected_set:
        raise ValueError("active component IDs are outside the checkpoint inventory")

    if not isinstance(dependencies_by_component, Mapping) or any(
        type(key) is not str for key in dependencies_by_component
    ):
        raise TypeError("dependencies_by_component must be a string-keyed mapping")
    if set(dependencies_by_component) != expected_set:
        raise ValueError("dependency declarations must cover the exact inventory")
    expected_dependencies: dict[str, tuple[str, ...]] = {}
    for component_id in expected_ids:
        dependencies = _validate_dependencies(
            dependencies_by_component[component_id],
            component_id=component_id,
            field=f"dependencies_by_component[{component_id!r}]",
        )
        missing = set(dependencies) - expected_set
        if missing:
            raise ValueError(
                f"component {component_id} has dependencies outside the inventory"
            )
        expected_dependencies[component_id] = dependencies

    if not isinstance(absent_reasons_by_component, Mapping) or any(
        type(key) is not str for key in absent_reasons_by_component
    ):
        raise TypeError("absent_reasons_by_component must be a string-keyed mapping")
    inactive_ids = expected_set - active_set
    if set(absent_reasons_by_component) != inactive_ids:
        raise ValueError("absence reasons must cover exactly the inactive inventory")
    expected_absent_reasons = {
        _exact_identifier(component_id, "absent reason component ID"):
        _exact_absent_reason(reason, f"absence reason for {component_id}")
        for component_id, reason in absent_reasons_by_component.items()
    }

    record_ids = tuple(record.component_id for record in records)
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("runtime component records contain duplicate component IDs")
    if record_ids != expected_ids:
        raise ValueError("runtime component records differ from the ordered inventory")

    by_id = {record.component_id: record for record in records}
    for component_id in expected_ids:
        record = by_id[component_id]
        if component_id in active_set:
            if record.status is not RuntimeComponentStatusV1.PRESERVED:
                raise ValueError(f"active component {component_id} is not preserved")
            if record.dependencies != expected_dependencies[component_id]:
                raise ValueError(
                    f"preserved component {component_id} dependencies differ from inventory"
                )
            absent_dependencies = tuple(
                dependency
                for dependency in record.dependencies
                if by_id[dependency].status is not RuntimeComponentStatusV1.PRESERVED
            )
            if absent_dependencies:
                raise ValueError(
                    f"preserved component {component_id} has an absent dependency"
                )
        else:
            if record.status is not RuntimeComponentStatusV1.ABSENT:
                raise ValueError(f"inactive component {component_id} is preserved")
            if record.absent_reason != expected_absent_reasons[component_id]:
                raise ValueError(
                    f"absent component {component_id} reason is not composition-proven"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(component_id: str) -> None:
        if component_id in visiting:
            raise ValueError("runtime component dependencies contain a cycle")
        if component_id in visited:
            return
        visiting.add(component_id)
        for dependency in expected_dependencies[component_id]:
            visit(dependency)
        visiting.remove(component_id)
        visited.add(component_id)

    for component_id in expected_ids:
        visit(component_id)


__all__ = [
    "RUNTIME_COMPONENT_STATE_SCHEMA_VERSION",
    "RuntimeComponentStateV1",
    "RuntimeComponentStatusV1",
    "validate_runtime_component_inventory",
]
