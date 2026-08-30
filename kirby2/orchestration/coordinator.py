"""Independent local coordinator verification and canonical result registration."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import ClassVar

from kirby2.packs.formats import canonical_json_bytes

from .local import ExecutionBackendV1
from .models import DigestReferenceV1, ExperimentWorkPlanV1
from .protocol import (
    InlineArtifactV1,
    RuntimeAuditResultV1,
    RuntimeAuditStatusV1,
    WorkerCompatibilityV1,
    WorkerResultManifestV1,
    WorkerResultStatusV1,
    WorkerResultV1,
    WorkRequestV1,
)
from .worker import (
    complete_run_expected_output_identities,
    complete_run_runtime_audit_identities,
    execute_work_request,
)


ORCHESTRATION_COORDINATOR_SCHEMA_VERSION = 1
VERIFIED_WORK_RESULT_SCHEMA_ID = "KIRBY2_VERIFIED_WORK_RESULT_V1"
COORDINATOR_RUN_RESULT_SCHEMA_ID = "KIRBY2_COORDINATOR_RUN_RESULT_V1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BACKEND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class CoordinatorVerificationError(RuntimeError):
    """A worker response failed independent coordinator verification."""


@dataclass(frozen=True, slots=True)
class VerifiedWorkResultV1:
    """One independently replayed, registration-eligible scientific result."""

    work_request_id: str
    logical_work_unit_id: str
    worker_compatibility_sha256: str
    worker_result_manifest_sha256: str
    artifacts: tuple[InlineArtifactV1, ...]
    runtime_audit_results: tuple[RuntimeAuditResultV1, ...]

    schema_id: ClassVar[str] = VERIFIED_WORK_RESULT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_COORDINATOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.work_request_id, "verified work request ID")
        _require_sha256(self.logical_work_unit_id, "verified logical work ID")
        _require_sha256(
            self.worker_compatibility_sha256,
            "verified worker compatibility digest",
        )
        _require_sha256(
            self.worker_result_manifest_sha256,
            "verified worker result-manifest digest",
        )
        _canonical_artifacts(self.artifacts)
        _canonical_runtime_audits(self.runtime_audit_results)
        if any(
            item.status is not RuntimeAuditStatusV1.PASSED
            for item in self.runtime_audit_results
        ):
            raise ValueError("verified work cannot contain a failed runtime audit")
        expected_manifest = WorkerResultManifestV1(
            work_request_id=self.work_request_id,
            logical_work_unit_id=self.logical_work_unit_id,
            worker_compatibility_sha256=self.worker_compatibility_sha256,
            artifacts=tuple(item.digest_reference for item in self.artifacts),
            runtime_audit_results=tuple(
                item.result_reference for item in self.runtime_audit_results
            ),
        )
        if not hmac.compare_digest(
            self.worker_result_manifest_sha256,
            expected_manifest.manifest_sha256,
        ):
            raise ValueError(
                "verified manifest digest differs from artifacts and runtime audits"
            )

    def scientific_dict(self) -> dict[str, object]:
        """Complete deterministic projection; operational diagnostics are excluded."""

        return {
            "artifacts": [item.descriptor_dict() for item in self.artifacts],
            "logical_work_unit_id": self.logical_work_unit_id,
            "runtime_audit_results": [
                item.identity_dict() for item in self.runtime_audit_results
            ],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "work_request_id": self.work_request_id,
            "worker_compatibility_sha256": self.worker_compatibility_sha256,
            "worker_result_manifest_sha256": self.worker_result_manifest_sha256,
        }

    @property
    def scientific_result_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.scientific_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            **self.scientific_dict(),
            "artifacts": [item.as_dict() for item in self.artifacts],
            "runtime_audit_results": [
                item.as_dict() for item in self.runtime_audit_results
            ],
            "scientific_result_sha256": self.scientific_result_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> VerifiedWorkResultV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "artifacts",
                    "logical_work_unit_id",
                    "runtime_audit_results",
                    "schema_id",
                    "schema_version",
                    "scientific_result_sha256",
                    "work_request_id",
                    "worker_compatibility_sha256",
                    "worker_result_manifest_sha256",
                }
            ),
            "verified work result",
        )
        _require_schema(payload, cls.schema_id, cls.schema_version, "verified work result")
        declared = _require_sha256(
            payload["scientific_result_sha256"],
            "declared verified scientific result digest",
        )
        restored = cls(
            work_request_id=_exact_text(payload, "work_request_id"),
            logical_work_unit_id=_exact_text(payload, "logical_work_unit_id"),
            worker_compatibility_sha256=_exact_text(
                payload,
                "worker_compatibility_sha256",
            ),
            worker_result_manifest_sha256=_exact_text(
                payload,
                "worker_result_manifest_sha256",
            ),
            artifacts=tuple(
                InlineArtifactV1.from_dict(item)
                for item in _exact_array(payload["artifacts"], "verified artifacts")
            ),
            runtime_audit_results=tuple(
                RuntimeAuditResultV1.from_dict(item)
                for item in _exact_array(
                    payload["runtime_audit_results"],
                    "verified runtime audit results",
                )
            ),
        )
        if not hmac.compare_digest(declared, restored.scientific_result_sha256):
            raise ValueError("verified scientific result digest differs from content")
        if restored.as_dict() != payload:
            raise ValueError("serialized verified work result did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class CoordinatorRunResultV1:
    """One complete experiment result with backend-neutral aggregate identity."""

    plan_id: str
    backend_id: str
    verified_results: tuple[VerifiedWorkResultV1, ...]

    schema_id: ClassVar[str] = COORDINATOR_RUN_RESULT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_COORDINATOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.plan_id, "coordinator plan ID")
        if type(self.backend_id) is not str or _BACKEND_ID.fullmatch(self.backend_id) is None:
            raise ValueError("coordinator backend ID must be canonical text")
        if type(self.verified_results) is not tuple or not self.verified_results:
            raise ValueError("coordinator run requires verified results")
        if any(type(item) is not VerifiedWorkResultV1 for item in self.verified_results):
            raise TypeError("coordinator run results must be VerifiedWorkResultV1 values")
        logical_ids = tuple(
            item.logical_work_unit_id for item in self.verified_results
        )
        if logical_ids != tuple(sorted(logical_ids)):
            raise ValueError("coordinator results must use canonical logical-ID order")
        if len(logical_ids) != len(set(logical_ids)):
            raise ValueError("coordinator results cannot duplicate logical work")

    def scientific_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "verified_results": [
                item.scientific_dict() for item in self.verified_results
            ],
        }

    @property
    def aggregate_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.scientific_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_sha256": self.aggregate_sha256,
            "backend_id": self.backend_id,
            "plan_id": self.plan_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "verified_results": [item.as_dict() for item in self.verified_results],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> CoordinatorRunResultV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "aggregate_sha256",
                    "backend_id",
                    "plan_id",
                    "schema_id",
                    "schema_version",
                    "verified_results",
                }
            ),
            "coordinator run result",
        )
        _require_schema(
            payload,
            cls.schema_id,
            cls.schema_version,
            "coordinator run result",
        )
        declared = _require_sha256(
            payload["aggregate_sha256"],
            "declared coordinator aggregate digest",
        )
        restored = cls(
            plan_id=_exact_text(payload, "plan_id"),
            backend_id=_exact_text(payload, "backend_id"),
            verified_results=tuple(
                VerifiedWorkResultV1.from_dict(item)
                for item in _exact_array(
                    payload["verified_results"],
                    "coordinator verified results",
                )
            ),
        )
        if not hmac.compare_digest(declared, restored.aggregate_sha256):
            raise ValueError("coordinator aggregate digest differs from scientific results")
        if restored.as_dict() != payload:
            raise ValueError("serialized coordinator run did not round-trip exactly")
        return restored


class OrchestrationCoordinatorV1:
    """Plan, dispatch, independently replay, and register local experiment work."""

    def execute(
        self,
        plan: ExperimentWorkPlanV1,
        backend: ExecutionBackendV1,
    ) -> CoordinatorRunResultV1:
        if type(plan) is not ExperimentWorkPlanV1:
            raise TypeError("coordinator requires ExperimentWorkPlanV1")
        compatibility = getattr(backend, "compatibility", None)
        if type(compatibility) is not WorkerCompatibilityV1:
            raise TypeError("execution backend must expose WorkerCompatibilityV1")
        backend_id = getattr(backend, "backend_id", None)
        if type(backend_id) is not str or _BACKEND_ID.fullmatch(backend_id) is None:
            raise TypeError("execution backend must expose one canonical backend ID")
        execute_many = getattr(backend, "execute_many", None)
        if not callable(execute_many):
            raise TypeError("execution backend must implement execute_many")

        required_audits = complete_run_runtime_audit_identities()
        expected_outputs = complete_run_expected_output_identities()
        requests: list[WorkRequestV1] = []
        for logical_unit in plan.logical_units:
            if not compatibility.matches_logical_work_unit(logical_unit):
                raise CoordinatorVerificationError(
                    "backend compatibility differs from logical work "
                    f"{logical_unit.logical_work_unit_id}"
                )
            if logical_unit.expected_outputs != expected_outputs:
                raise CoordinatorVerificationError(
                    "logical work expected-output contracts differ from COMPLETE_RUN"
                )
            requests.append(
                WorkRequestV1(
                    logical_work_unit=logical_unit,
                    required_runtime_audits=required_audits,
                )
            )
        request_tuple = tuple(requests)
        returned = execute_many(request_tuple)
        if type(returned) is not tuple or any(
            type(item) is not WorkerResultV1 for item in returned
        ):
            raise CoordinatorVerificationError(
                "execution backend returned values outside WorkerResultV1"
            )

        by_request: dict[str, WorkerResultV1] = {}
        for result in returned:
            request_id = result.request.work_request_id
            if request_id in by_request:
                raise CoordinatorVerificationError(
                    "execution backend returned a duplicate request"
                )
            by_request[request_id] = result
        expected_ids = tuple(item.work_request_id for item in request_tuple)
        if frozenset(by_request) != frozenset(expected_ids):
            raise CoordinatorVerificationError(
                "execution backend omitted or invented work results"
            )

        verified = tuple(
            self._verify_result(
                request,
                by_request[request.work_request_id],
                compatibility,
                expected_outputs,
            )
            for request in request_tuple
        )
        return CoordinatorRunResultV1(
            plan_id=plan.plan_id,
            backend_id=backend_id,
            verified_results=verified,
        )

    def _verify_result(
        self,
        request: WorkRequestV1,
        result: WorkerResultV1,
        compatibility: WorkerCompatibilityV1,
        expected_outputs: tuple[DigestReferenceV1, ...],
    ) -> VerifiedWorkResultV1:
        if result.request != request:
            raise CoordinatorVerificationError("worker result binds a foreign request")
        if result.worker_compatibility != compatibility:
            raise CoordinatorVerificationError(
                "worker result compatibility differs from measured backend identity"
            )
        if result.status is not WorkerResultStatusV1.SUCCEEDED:
            codes = ",".join(item.code for item in result.diagnostics)
            raise CoordinatorVerificationError(
                f"worker did not return a successful result: {result.status.value}:{codes}"
            )
        if type(result.manifest) is not WorkerResultManifestV1:
            raise CoordinatorVerificationError("successful worker result lacks manifest")
        parsed_manifest = WorkerResultManifestV1.from_bytes(result.manifest_bytes)
        if parsed_manifest != result.manifest:
            raise CoordinatorVerificationError("worker manifest bytes changed identity")
        expected_names = tuple(item.name for item in expected_outputs)
        artifact_names = tuple(item.artifact_id for item in result.artifacts)
        if artifact_names != expected_names:
            raise CoordinatorVerificationError(
                "worker artifact inventory differs from expected outputs"
            )
        if tuple(item.audit_identity for item in result.runtime_audit_results) != (
            request.required_runtime_audits
        ):
            raise CoordinatorVerificationError(
                "worker runtime-audit inventory differs from the request"
            )

        replayed = execute_work_request(request)
        if replayed.status is not WorkerResultStatusV1.SUCCEEDED:
            raise CoordinatorVerificationError(
                "coordinator-side independent replay did not succeed"
            )
        if (
            replayed.manifest != result.manifest
            or replayed.artifacts != result.artifacts
            or tuple(item.identity_dict() for item in replayed.runtime_audit_results)
            != tuple(item.identity_dict() for item in result.runtime_audit_results)
        ):
            raise CoordinatorVerificationError(
                "worker bytes differ from independent coordinator replay"
            )

        return VerifiedWorkResultV1(
            work_request_id=request.work_request_id,
            logical_work_unit_id=request.logical_work_unit.logical_work_unit_id,
            worker_compatibility_sha256=compatibility.compatibility_sha256,
            worker_result_manifest_sha256=result.manifest.manifest_sha256,
            artifacts=result.artifacts,
            runtime_audit_results=result.runtime_audit_results,
        )


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _canonical_artifacts(values: tuple[InlineArtifactV1, ...]) -> None:
    if type(values) is not tuple or not values or any(
        type(item) is not InlineArtifactV1 for item in values
    ):
        raise TypeError("verified artifacts must be a nonempty InlineArtifactV1 tuple")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError("verified artifacts must use canonical order")
    if len({item.artifact_id for item in values}) != len(values):
        raise ValueError("verified artifacts cannot contain duplicate names")


def _canonical_runtime_audits(values: tuple[RuntimeAuditResultV1, ...]) -> None:
    if type(values) is not tuple or not values or any(
        type(item) is not RuntimeAuditResultV1 for item in values
    ):
        raise TypeError(
            "verified runtime audits must be a nonempty RuntimeAuditResultV1 tuple"
        )
    names = tuple(item.audit_identity.name for item in values)
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        raise ValueError("verified runtime audits must use unique canonical name order")


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
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


def _exact_text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _require_schema(
    payload: dict[str, object],
    schema_id: str,
    schema_version: int,
    label: str,
) -> None:
    if (
        payload.get("schema_id") != schema_id
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != schema_version
    ):
        raise ValueError(f"serialized {label} schema differs from the V1 contract")


__all__ = [
    "COORDINATOR_RUN_RESULT_SCHEMA_ID",
    "ORCHESTRATION_COORDINATOR_SCHEMA_VERSION",
    "VERIFIED_WORK_RESULT_SCHEMA_ID",
    "CoordinatorRunResultV1",
    "CoordinatorVerificationError",
    "OrchestrationCoordinatorV1",
    "VerifiedWorkResultV1",
]
