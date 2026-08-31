"""Frozen release platform, functional, evidence, and closeout protocols.

WO40-D owns dispatch/refusal semantics only.  It can prove that a future command
addresses the exact preregistered target and step matrix, but it cannot manufacture
qualification evidence or silently rerun a completed attempt.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from kirby2.packs.formats import canonical_json_bytes, require_nfc_text, require_sha256

from .manifest import RELEASE_ARTIFACT_SELECTORS_V1, RELEASE_VERSION_V1


RELEASE_PLATFORMS_SCHEMA_ID_V1 = "KIRBY2_RELEASE_PLATFORMS_V1"
RELEASE_QUALIFICATION_PROTOCOL_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_QUALIFICATION_PROTOCOL_V1"
)
RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_QUALIFICATION_ATTEMPT_V1"
)
RELEASE_EVIDENCE_REFERENCE_SCHEMA_ID_V1 = "KIRBY2_RELEASE_EVIDENCE_REFERENCE_V1"
WO40_J_PREREQUISITES_ID_V1 = "WO40_J_PREREQUISITES_V1"

RELEASE_FUNCTIONAL_STEP_ORDER_V1 = (
    "CLEAN_INSTALL",
    "LAUNCH",
    "FULL_FIRST_RUN",
    "STARTER_LESSON",
    "PLACE_CANCEL",
    "COMPLETE_SAVE",
    "OPEN_REPLAY_MICROSCOPE",
    "EXPORT_PACK",
    "CLOSE",
    "REOPEN_VERIFY_SAVED",
    "IMPORT_SECOND_CLEAN_ROOT",
    "REPLAY_IMPORTED_LESSON",
    "COMPARE_DECLARED_REPLAY_DIGEST",
    "RESTORE_BACKUP",
    "CRASH_RECOVERY",
    "EXPORT_DIAGNOSTICS",
    "UNINSTALL_PRESERVE_USER_DATA",
)

RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1 = (
    "HEADLESS_SIMULATION",
    "HEADLESS_AUDIT",
    "HEADLESS_CALIBRATION",
    "HEADLESS_DISTRIBUTED_WORKER",
)

RELEASE_EVIDENCE_GATE_ORDER_V1 = (
    "WO40-D1",
    "WO40-E",
    "WO40-F",
    "WO40-G",
    "WO40-H",
    "WO40-I",
    "WO40-J",
)

WO40_J_REQUIRED_PRIOR_GATES_V1 = (
    "K2X-02",
    "WO31-A",
    "WO31-B",
    "WO31-C",
    "WO31-D",
    "WO31-E1",
    "WO31-E2",
    "WO31-E3",
    "WO31-E4",
    "WO31-E5",
    "WO31-E6",
    "WO31-F",
    "WO31-G",
    "WO31-H",
    "WO31-I",
    "WO31-I1",
    "WO32-A",
    "WO32-B",
    "WO32-C",
    "WO32-D",
    "WO32-E",
    "WO33-A",
    "WO33-A1",
    "WO33-B1",
    "WO33-B2",
    "WO33-C",
    "WO33-D",
    "WO33-E",
    "WO34-A",
    "WO34-B",
    "WO34-C",
    "WO34-D",
    "WO35-A",
    "WO35-B",
    "WO35-C",
    "WO35-D",
    "WO35-E",
    "WO35-F",
    "WO35-F1",
    "WO36-A",
    "WO36-B",
    "WO36-C",
    "WO36-D",
    "WO36-E",
    "WO37-A",
    "WO37-B",
    "WO37-C",
    "WO37-D",
    "WO37-E",
    "WO39-A",
    "WO39-B",
    "WO39-C",
    "WO38-A",
    "WO38-B",
    "WO38-C",
    "WO38-D",
    "WO38-E",
    "WO39-D1",
    "WO39-D2",
    "WO39-E",
    "WO40-A",
    "WO40-B",
    "WO40-B1",
    "WO40-C",
    "WO40-D",
    "WO40-D1",
    "WO40-E",
    "WO40-F",
    "WO40-G",
    "WO40-H",
    "WO40-I",
)

_TARGET_IDS = ("macos-arm64", "linux-x86_64")


class ReleaseQualificationStatusV1(str, Enum):
    READY = "READY"
    NOT_EXERCISED = "NOT_EXERCISED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    REFUSED = "REFUSED"


class ReleaseQualificationRefusalCodeV1(str, Enum):
    PROTOCOL_INVALID = "PROTOCOL_INVALID"
    CANDIDATE_NOT_FROZEN = "CANDIDATE_NOT_FROZEN"
    ARTIFACT_INDEX_MISSING = "ARTIFACT_INDEX_MISSING"
    ARTIFACT_SELECTOR_MISMATCH = "ARTIFACT_SELECTOR_MISMATCH"
    CLEAN_PROVIDER_MISSING = "CLEAN_PROVIDER_MISSING"
    DATA_ROOT_NOT_CLEAN = "DATA_ROOT_NOT_CLEAN"
    PRIOR_ATTEMPT_EXISTS = "PRIOR_ATTEMPT_EXISTS"
    UPSTREAM_EVIDENCE_MISSING = "UPSTREAM_EVIDENCE_MISSING"
    RESULT_NOT_FROZEN = "RESULT_NOT_FROZEN"
    CLOSEOUT_PREREQUISITES_INCOMPLETE = "CLOSEOUT_PREREQUISITES_INCOMPLETE"


class ReleaseQualificationRefused(ValueError):
    def __init__(self, code: ReleaseQualificationRefusalCodeV1, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 protocol")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


@dataclass(frozen=True, slots=True)
class ReleasePlatformTargetV1:
    target_id: str
    system: str
    machine: str
    python_implementation: str
    python_version: str
    clean_provider_required: bool
    functional_selectors: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.target_id not in _TARGET_IDS:
            raise ValueError("release target ID is invalid")
        for label, value in (
            ("target system", self.system),
            ("target machine", self.machine),
            ("Python implementation", self.python_implementation),
            ("Python version", self.python_version),
        ):
            _text(value, label, 128)
        if self.clean_provider_required is not True:
            raise ValueError("minimum targets require a real clean provider")
        expected = (
            f"{self.target_id}/desktop",
            f"{self.target_id}/headless",
        )
        if self.functional_selectors != expected:
            raise ValueError("target functional selectors differ")
        if any(selector not in RELEASE_ARTIFACT_SELECTORS_V1 for selector in expected):
            raise ValueError("target selector is absent from the release artifact protocol")

    def as_dict(self) -> dict[str, object]:
        return {
            "clean_provider_required": self.clean_provider_required,
            "functional_selectors": list(self.functional_selectors),
            "machine": self.machine,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "system": self.system,
            "target_id": self.target_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePlatformTargetV1":
        fields = {
            "target_id",
            "system",
            "machine",
            "python_implementation",
            "python_version",
            "clean_provider_required",
            "functional_selectors",
        }
        row = _exact(value, fields, "release target")
        return cls(
            target_id=_text(row["target_id"], "target ID", 128),
            system=_text(row["system"], "target system", 128),
            machine=_text(row["machine"], "target machine", 128),
            python_implementation=_text(
                row["python_implementation"], "Python implementation", 128
            ),
            python_version=_text(row["python_version"], "Python version", 128),
            clean_provider_required=row["clean_provider_required"],  # type: ignore[arg-type]
            functional_selectors=tuple(
                _text(item, "functional selector", 256)
                for item in _array(row["functional_selectors"], "functional selectors")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleasePlatformsV1:
    release_version: str
    targets: tuple[ReleasePlatformTargetV1, ...]
    designated_performance_target: str
    windows_supported: bool

    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if self.release_version != RELEASE_VERSION_V1:
            raise ValueError("release platforms version differs")
        if tuple(item.target_id for item in self.targets) != _TARGET_IDS:
            raise ValueError("minimum target order differs")
        if self.designated_performance_target != "macos-arm64":
            raise ValueError("designated performance target differs")
        if self.windows_supported is not False:
            raise ValueError("Windows is outside the V1 release")

    def as_dict(self) -> dict[str, object]:
        return {
            "designated_performance_target": self.designated_performance_target,
            "release_version": self.release_version,
            "schema_version": self.schema_version,
            "targets": [item.as_dict() for item in self.targets],
            "windows_supported": self.windows_supported,
        }

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleasePlatformsV1":
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("platform protocol is not valid UTF-8 TOML") from error
        fields = {
            "schema_version",
            "release_version",
            "designated_performance_target",
            "windows_supported",
            "targets",
        }
        row = _exact(value, fields, "release platforms")
        if row["schema_version"] != cls.schema_version:
            raise ValueError("platform protocol schema version differs")
        return cls(
            release_version=_text(row["release_version"], "release version", 128),
            designated_performance_target=_text(
                row["designated_performance_target"],
                "designated performance target",
                128,
            ),
            windows_supported=row["windows_supported"],  # type: ignore[arg-type]
            targets=tuple(
                ReleasePlatformTargetV1.from_dict(item)
                for item in _array(row["targets"], "release targets")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseFunctionalStepV1:
    step_id: str
    root_role: str
    mutation: str
    expected: str

    def __post_init__(self) -> None:
        for label, value in self.as_dict().items():
            _text(value, f"functional step {label}", 1024)

    def as_dict(self) -> dict[str, str]:
        return {
            "expected": self.expected,
            "mutation": self.mutation,
            "root_role": self.root_role,
            "step_id": self.step_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseFunctionalStepV1":
        fields = {"step_id", "root_role", "mutation", "expected"}
        row = _exact(value, fields, "functional step")
        return cls(
            step_id=_text(row["step_id"], "functional step ID", 128),
            root_role=_text(row["root_role"], "functional root role", 128),
            mutation=_text(row["mutation"], "functional mutation", 1024),
            expected=_text(row["expected"], "functional expectation", 1024),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationProtocolV1:
    release_version: str
    protocol_id: str
    functional_steps: tuple[ReleaseFunctionalStepV1, ...]
    headless_extra_steps: tuple[ReleaseFunctionalStepV1, ...]
    attempts_per_clean_root: int
    result_retry_count: int
    environmental_interruption_policy: str
    offline_required: bool
    user_data_preservation_required: bool
    cross_platform_workload_id: str
    cross_platform_root_start: int
    cross_platform_root_end_inclusive: int
    evidence_gate_order: tuple[str, ...]
    closeout_prerequisite_id: str
    closeout_required_gates: tuple[str, ...]

    schema_version: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if self.release_version != RELEASE_VERSION_V1:
            raise ValueError("qualification release version differs")
        if self.protocol_id != "RELEASE_QUALIFICATION_V1":
            raise ValueError("qualification protocol ID differs")
        if tuple(item.step_id for item in self.functional_steps) != RELEASE_FUNCTIONAL_STEP_ORDER_V1:
            raise ValueError("functional qualification step order differs")
        if tuple(item.step_id for item in self.headless_extra_steps) != RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1:
            raise ValueError("headless qualification step order differs")
        if (
            type(self.attempts_per_clean_root) is not int
            or type(self.result_retry_count) is not int
            or self.attempts_per_clean_root != 1
            or self.result_retry_count != 0
        ):
            raise ValueError("functional qualification cannot retry results")
        if self.environmental_interruption_policy != "NEW_ATTEMPT_ONLY_BEFORE_USER_DATA_MUTATION":
            raise ValueError("environmental interruption policy differs")
        if self.offline_required is not True or self.user_data_preservation_required is not True:
            raise ValueError("qualification must be offline and preserve user data")
        if self.cross_platform_workload_id != "CROSS_PLATFORM_INTEGER_CORE_V1":
            raise ValueError("cross-platform workload identity differs")
        if (
            type(self.cross_platform_root_start) is not int
            or type(self.cross_platform_root_end_inclusive) is not int
            or (
                self.cross_platform_root_start,
                self.cross_platform_root_end_inclusive,
            )
            != (
                4_000_000,
                4_000_015,
            )
        ):
            raise ValueError("cross-platform integer-core roots differ")
        if self.evidence_gate_order != RELEASE_EVIDENCE_GATE_ORDER_V1:
            raise ValueError("release evidence gate order differs")
        if self.closeout_prerequisite_id != WO40_J_PREREQUISITES_ID_V1:
            raise ValueError("closeout prerequisite validator differs")
        if self.closeout_required_gates != WO40_J_REQUIRED_PRIOR_GATES_V1:
            raise ValueError("WO40-J prerequisite gate inventory differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "closeout": {
                "prerequisite_id": self.closeout_prerequisite_id,
                "required_gates": list(self.closeout_required_gates),
            },
            "cross_platform": {
                "root_end_inclusive": self.cross_platform_root_end_inclusive,
                "root_start": self.cross_platform_root_start,
                "workload_id": self.cross_platform_workload_id,
            },
            "evidence_gate_order": list(self.evidence_gate_order),
            "functional_steps": [item.as_dict() for item in self.functional_steps],
            "headless_extra_steps": [item.as_dict() for item in self.headless_extra_steps],
            "offline_required": self.offline_required,
            "protocol_id": self.protocol_id,
            "release_version": self.release_version,
            "retry_policy": {
                "attempts_per_clean_root": self.attempts_per_clean_root,
                "environmental_interruption": self.environmental_interruption_policy,
                "result_retry_count": self.result_retry_count,
            },
            "schema_version": self.schema_version,
            "user_data_preservation_required": self.user_data_preservation_required,
        }

    @property
    def logical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.as_dict())).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseQualificationProtocolV1":
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("qualification protocol is not valid UTF-8 TOML") from error
        fields = {
            "schema_version",
            "release_version",
            "protocol_id",
            "offline_required",
            "user_data_preservation_required",
            "retry_policy",
            "functional_steps",
            "headless_extra_steps",
            "cross_platform",
            "evidence_gate_order",
            "closeout",
        }
        row = _exact(value, fields, "qualification protocol")
        if row["schema_version"] != cls.schema_version:
            raise ValueError("qualification protocol schema version differs")
        retry = _exact(
            row["retry_policy"],
            {"attempts_per_clean_root", "result_retry_count", "environmental_interruption"},
            "qualification retry policy",
        )
        cross = _exact(
            row["cross_platform"],
            {"workload_id", "root_start", "root_end_inclusive"},
            "cross-platform workload",
        )
        closeout = _exact(
            row["closeout"],
            {"prerequisite_id", "required_gates"},
            "closeout prerequisites",
        )
        return cls(
            release_version=_text(row["release_version"], "release version", 128),
            protocol_id=_text(row["protocol_id"], "qualification protocol ID", 128),
            functional_steps=tuple(
                ReleaseFunctionalStepV1.from_dict(item)
                for item in _array(row["functional_steps"], "functional steps")
            ),
            headless_extra_steps=tuple(
                ReleaseFunctionalStepV1.from_dict(item)
                for item in _array(row["headless_extra_steps"], "headless steps")
            ),
            attempts_per_clean_root=retry["attempts_per_clean_root"],  # type: ignore[arg-type]
            result_retry_count=retry["result_retry_count"],  # type: ignore[arg-type]
            environmental_interruption_policy=_text(
                retry["environmental_interruption"],
                "environmental interruption policy",
                256,
            ),
            offline_required=row["offline_required"],  # type: ignore[arg-type]
            user_data_preservation_required=row["user_data_preservation_required"],  # type: ignore[arg-type]
            cross_platform_workload_id=_text(
                cross["workload_id"], "cross-platform workload ID", 128
            ),
            cross_platform_root_start=cross["root_start"],  # type: ignore[arg-type]
            cross_platform_root_end_inclusive=cross["root_end_inclusive"],  # type: ignore[arg-type]
            evidence_gate_order=tuple(
                _text(item, "evidence gate ID", 128)
                for item in _array(row["evidence_gate_order"], "evidence gates")
            ),
            closeout_prerequisite_id=_text(
                closeout["prerequisite_id"], "closeout prerequisite ID", 128
            ),
            closeout_required_gates=tuple(
                _text(item, "required closeout gate ID", 128)
                for item in _array(closeout["required_gates"], "required gates")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationDispatchV1:
    command_id: str
    status: ReleaseQualificationStatusV1
    protocol_sha256: str
    target_id: str
    artifact_selector: str
    clean_root_role: str
    step_ids: tuple[str, ...]
    refusal_code: str | None
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_selector": self.artifact_selector,
            "clean_root_role": self.clean_root_role,
            "command_id": self.command_id,
            "detail": self.detail,
            "protocol_sha256": self.protocol_sha256,
            "refusal_code": self.refusal_code,
            "schema_id": "KIRBY2_RELEASE_QUALIFICATION_DISPATCH_V1",
            "schema_version": 1,
            "status": self.status.value,
            "step_ids": list(self.step_ids),
            "target_id": self.target_id,
        }


def qualification_dispatch(
    protocol: ReleaseQualificationProtocolV1,
    *,
    target_id: str,
    artifact_selector: str,
    clean_provider_id: str | None,
    clean_root_role: str,
    prior_attempt_exists: bool,
) -> ReleaseQualificationDispatchV1:
    """Validate one future dispatch without executing or inspecting its outcome."""

    if type(protocol) is not ReleaseQualificationProtocolV1:
        raise TypeError("qualification dispatch requires the exact V1 protocol")
    if target_id not in _TARGET_IDS:
        raise ReleaseQualificationRefused(
            ReleaseQualificationRefusalCodeV1.ARTIFACT_SELECTOR_MISMATCH,
            "qualification target is outside the minimum platform matrix",
        )
    if type(prior_attempt_exists) is not bool:
        raise TypeError("prior-attempt state must be Boolean")
    expected_selectors = {
        f"{target_id}/desktop",
        f"{target_id}/headless",
    }
    if artifact_selector not in expected_selectors:
        raise ReleaseQualificationRefused(
            ReleaseQualificationRefusalCodeV1.ARTIFACT_SELECTOR_MISMATCH,
            "artifact selector does not belong to the requested target",
        )
    if clean_provider_id is None:
        raise ReleaseQualificationRefused(
            ReleaseQualificationRefusalCodeV1.CLEAN_PROVIDER_MISSING,
            "qualification requires a recorded real clean-environment provider",
        )
    _text(clean_provider_id, "clean provider ID", 256)
    if prior_attempt_exists:
        raise ReleaseQualificationRefused(
            ReleaseQualificationRefusalCodeV1.PRIOR_ATTEMPT_EXISTS,
            "functional qualification does not rerun an existing result",
        )
    if _text(clean_root_role, "clean root role", 128) != "PRIMARY_CLEAN_ROOT":
        raise ReleaseQualificationRefused(
            ReleaseQualificationRefusalCodeV1.DATA_ROOT_NOT_CLEAN,
            "qualification dispatch requires the primary clean root coordinator",
        )
    steps = tuple(item.step_id for item in protocol.functional_steps)
    if artifact_selector.endswith("/headless"):
        steps += tuple(item.step_id for item in protocol.headless_extra_steps)
    return ReleaseQualificationDispatchV1(
        command_id="QUALIFY_RELEASE",
        status=ReleaseQualificationStatusV1.READY,
        protocol_sha256=protocol.logical_sha256,
        target_id=target_id,
        artifact_selector=artifact_selector,
        clean_root_role=clean_root_role,
        step_ids=steps,
        refusal_code=None,
        detail="Dispatch is fully specified; no workload was executed by preregistration.",
    )


@dataclass(frozen=True, slots=True)
class ReleaseEvidenceReferenceV1:
    gate_id: str
    evidence_id: str
    size: int
    sha256: str
    status: str

    def __post_init__(self) -> None:
        _text(self.gate_id, "release evidence gate ID", 128)
        _text(self.evidence_id, "release evidence ID", 256)
        if type(self.size) is not int or self.size <= 0:
            raise ValueError("release evidence size must be positive")
        require_sha256(self.sha256, "release evidence digest")
        if self.status not in {"PASS", "PASS_WITH_WARNINGS"}:
            raise ValueError("closeout accepts only passing immutable evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "gate_id": self.gate_id,
            "sha256": self.sha256,
            "size": self.size,
            "status": self.status,
        }


def verify_closeout_prerequisites(
    references: tuple[ReleaseEvidenceReferenceV1, ...],
) -> dict[str, object]:
    """Verify every prior gate while deliberately excluding WO40-J self-evidence."""

    if type(references) is not tuple or any(
        type(item) is not ReleaseEvidenceReferenceV1 for item in references
    ):
        raise TypeError("closeout prerequisites require typed immutable references")
    by_gate = {item.gate_id: item for item in references}
    if len(by_gate) != len(references):
        raise ValueError("closeout prerequisite gate references must be unique")
    if "WO40-J" in by_gate:
        raise ValueError("WO40-J prerequisite validation cannot reference its own packet")
    missing = tuple(gate for gate in WO40_J_REQUIRED_PRIOR_GATES_V1 if gate not in by_gate)
    extra = tuple(sorted(set(by_gate) - set(WO40_J_REQUIRED_PRIOR_GATES_V1)))
    status = "PASS" if not missing and not extra else "NOT_EXERCISED"
    projection = [by_gate[gate].as_dict() for gate in WO40_J_REQUIRED_PRIOR_GATES_V1 if gate in by_gate]
    return {
        "evidence_projection_sha256": hashlib.sha256(canonical_json_bytes(projection)).hexdigest(),
        "extra_gates": list(extra),
        "missing_gates": list(missing),
        "prerequisite_id": WO40_J_PREREQUISITES_ID_V1,
        "schema_version": 1,
        "status": status,
    }


def load_release_qualification_protocol(path: Path) -> ReleaseQualificationProtocolV1:
    if not isinstance(path, Path):
        raise TypeError("qualification protocol path must use a Path")
    return ReleaseQualificationProtocolV1.from_bytes(path.read_bytes())


def load_release_platforms(path: Path) -> ReleasePlatformsV1:
    if not isinstance(path, Path):
        raise TypeError("platform protocol path must use a Path")
    return ReleasePlatformsV1.from_bytes(path.read_bytes())


__all__ = [
    "RELEASE_EVIDENCE_GATE_ORDER_V1",
    "RELEASE_FUNCTIONAL_STEP_ORDER_V1",
    "RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1",
    "RELEASE_PLATFORMS_SCHEMA_ID_V1",
    "RELEASE_QUALIFICATION_PROTOCOL_SCHEMA_ID_V1",
    "WO40_J_PREREQUISITES_ID_V1",
    "WO40_J_REQUIRED_PRIOR_GATES_V1",
    "ReleaseEvidenceReferenceV1",
    "ReleaseFunctionalStepV1",
    "ReleasePlatformTargetV1",
    "ReleasePlatformsV1",
    "ReleaseQualificationDispatchV1",
    "ReleaseQualificationProtocolV1",
    "ReleaseQualificationRefusalCodeV1",
    "ReleaseQualificationRefused",
    "ReleaseQualificationStatusV1",
    "load_release_platforms",
    "load_release_qualification_protocol",
    "qualification_dispatch",
    "verify_closeout_prerequisites",
]
