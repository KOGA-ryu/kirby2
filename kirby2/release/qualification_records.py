"""Canonical clean-provider and qualification-attempt evidence records.

This module is deliberately pure.  It models the two immutable records produced by
clean-environment release qualification, reconstructs every WO40-G/WO40-H check,
and verifies the relationships between the records and the frozen qualification
protocol.  Provider control, command execution, filesystem publication, and audit
document rendering belong to their respective integration layers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)

from .qualification import (
    RELEASE_FUNCTIONAL_STEP_ORDER_V1,
    RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
    RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1,
    ReleaseQualificationProtocolV1,
)


RELEASE_CLEAN_PROVIDER_ATTESTATION_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_CLEAN_PROVIDER_ATTESTATION_V1"
)
RELEASE_QUALIFICATION_EXECUTION_POLICY_ID_V1 = (
    "KIRBY2_RELEASE_QUALIFICATION_EXECUTION_POLICY_V1"
)
RELEASE_QUALIFICATION_CHECK_PROOF_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_QUALIFICATION_CHECK_PROOF_V1"
)
RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1 = 1
RELEASE_QUALIFICATION_ATTEMPT_NUMBER_V1 = 1
RELEASE_QUALIFICATION_STEP_COUNT_V1 = 38
RELEASE_QUALIFICATION_CHECK_COUNT_V1 = 42
RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1 = (
    "CONTROLLER_CAPTURED_GUEST_OBSERVATION_V1"
)
RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1 = (
    "IMMUTABLE_RELEASE_ARTIFACTS_ONLY"
)

RELEASE_QUALIFICATION_GATE_BY_TARGET_V1 = {
    "macos-arm64": "WO40-G",
    "linux-x86_64": "WO40-H",
}
RELEASE_QUALIFICATION_PROVIDER_RECORD_PATH_BY_TARGET_V1 = {
    "macos-arm64": "clean-provider-macos-arm64.json",
    "linux-x86_64": "clean-provider-linux-x86_64.json",
}
RELEASE_QUALIFICATION_ATTEMPT_RECORD_PATH_BY_TARGET_V1 = {
    "macos-arm64": "gate-evidence/wo40-g/qualification-attempt.json",
    "linux-x86_64": "gate-evidence/wo40-h/qualification-attempt.json",
}
RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1 = {
    "macos-arm64": (
        "macos-arm64-desktop-bundle",
        "project-wheel",
        "source-archive",
        "macos-arm64-wheelhouse",
    ),
    "linux-x86_64": (
        "linux-x86_64-desktop-bundle",
        "project-wheel",
        "source-archive",
        "linux-x86_64-wheelhouse",
    ),
}
RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1 = (
    "PRIMARY_CLEAN_ROOT",
    "SECONDARY_CLEAN_ROOT",
    "RESTORE_CLEAN_ROOT",
)
RELEASE_QUALIFICATION_FUNCTIONAL_ROOT_ROLES_V1 = (
    "PRIMARY_CLEAN_ROOT",  # CLEAN_INSTALL
    "PRIMARY_CLEAN_ROOT",  # LAUNCH
    "PRIMARY_CLEAN_ROOT",  # FULL_FIRST_RUN
    "PRIMARY_CLEAN_ROOT",  # STARTER_LESSON
    "PRIMARY_CLEAN_ROOT",  # PLACE_CANCEL
    "PRIMARY_CLEAN_ROOT",  # COMPLETE_SAVE
    "PRIMARY_CLEAN_ROOT",  # OPEN_REPLAY_MICROSCOPE
    "PRIMARY_CLEAN_ROOT",  # EXPORT_PACK
    "PRIMARY_CLEAN_ROOT",  # CLOSE
    "PRIMARY_CLEAN_ROOT",  # REOPEN_VERIFY_SAVED
    "SECONDARY_CLEAN_ROOT",  # IMPORT_SECOND_CLEAN_ROOT
    "SECONDARY_CLEAN_ROOT",  # REPLAY_IMPORTED_LESSON
    "BOTH_CLEAN_ROOTS",  # COMPARE_DECLARED_REPLAY_DIGEST
    "RESTORE_CLEAN_ROOT",  # RESTORE_BACKUP
    "PRIMARY_CLEAN_ROOT",  # CRASH_RECOVERY
    "PRIMARY_CLEAN_ROOT",  # EXPORT_DIAGNOSTICS
    "PRIMARY_CLEAN_ROOT",  # UNINSTALL_PRESERVE_USER_DATA
)
RELEASE_QUALIFICATION_HEADLESS_ROOT_ROLES_V1 = (
    "PRIMARY_CLEAN_ROOT",
) * len(RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1)

_PROVIDER_ADAPTER_BY_TARGET_V1 = {
    "macos-arm64": "TART_LOCAL_VM_V1",
    "linux-x86_64": "SSH_EPHEMERAL_HOST_V1",
}
_PLATFORM_BY_TARGET_V1 = {
    "macos-arm64": ("Darwin", "arm64"),
    "linux-x86_64": ("Linux", "x86_64"),
}
_QUALIFICATION_STATUSES_V1 = {
    "PASS",
    "PASS_WITH_WARNINGS",
    "FAIL",
    "NOT_EXERCISED",
}
_STEP_STATUSES_V1 = {"PASS", "WARNING", "FAIL", "NOT_EXERCISED"}
_COMMAND_ROOT_ROLES_V1 = {
    "PROVIDER",
    "PRIMARY_CLEAN_ROOT",
    "SECONDARY_CLEAN_ROOT",
    "RESTORE_CLEAN_ROOT",
    "BOTH_CLEAN_ROOTS",
}
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,255}\Z")
_UTC_SECOND = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_PYTHON_314 = re.compile(r"3\.14(?:\.[0-9]+)?(?:[A-Za-z0-9.+-]*)?\Z")
_EMPTY_INVENTORY_SHA256 = hashlib.sha256(canonical_json_bytes([])).hexdigest()


class ReleaseQualificationNetworkScopeV1(str, Enum):
    HOST_ONLY = "HOST_ONLY"
    GUEST_NETWORK_DISABLED_VERIFIED = "GUEST_NETWORK_DISABLED_VERIFIED"


def _require_passing_network_scope(
    *,
    target_id: str,
    status: str,
    network_scope: ReleaseQualificationNetworkScopeV1,
) -> None:
    if status not in {"PASS", "PASS_WITH_WARNINGS"}:
        return
    expected = {
        "macos-arm64": ReleaseQualificationNetworkScopeV1.HOST_ONLY,
        "linux-x86_64": (
            ReleaseQualificationNetworkScopeV1.GUEST_NETWORK_DISABLED_VERIFIED
        ),
    }[target_id]
    if network_scope is expected:
        return
    if target_id == "macos-arm64":
        raise ValueError("passing macOS qualification currently requires HOST_ONLY")
    raise ValueError(
        "passing Linux qualification requires GUEST_NETWORK_DISABLED_VERIFIED"
    )


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _token(value: object, label: str) -> str:
    selected = _text(value, label, 256)
    if _TOKEN.fullmatch(selected) is None:
        raise ValueError(f"{label} is not a canonical token")
    return selected


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 record")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive(value: object, label: str) -> int:
    selected = _nonnegative(value, label)
    if selected == 0:
        raise ValueError(f"{label} must be positive")
    return selected


def _commit(value: object, label: str) -> str:
    selected = _text(value, label, 40)
    if _COMMIT.fullmatch(selected) is None:
        raise ValueError(f"{label} must be a lowercase Git commit identity")
    return selected


def _utc_second(value: object, label: str) -> str:
    selected = _text(value, label, 20)
    if _UTC_SECOND.fullmatch(selected) is None:
        raise ValueError(f"{label} must be a UTC-second timestamp")
    return selected


def _sha256(value: object, label: str) -> str:
    selected = _text(value, label, 64)
    require_sha256(selected, label)
    return selected


def _network_scope(value: object, label: str) -> ReleaseQualificationNetworkScopeV1:
    try:
        return ReleaseQualificationNetworkScopeV1(_text(value, label, 64))
    except ValueError as error:
        raise ValueError(f"{label} is outside the closed V1 enum") from error


def _target_selectors(target_id: str) -> tuple[str, str]:
    if target_id not in RELEASE_QUALIFICATION_GATE_BY_TARGET_V1:
        raise ValueError("qualification target is outside the frozen platform matrix")
    return (f"{target_id}/desktop", f"{target_id}/headless")


def _expected_step_rows(target_id: str) -> tuple[tuple[str, str, str], ...]:
    desktop, headless = _target_selectors(target_id)
    functional = tuple(
        zip(
            RELEASE_FUNCTIONAL_STEP_ORDER_V1,
            RELEASE_QUALIFICATION_FUNCTIONAL_ROOT_ROLES_V1,
            strict=True,
        )
    )
    extra = tuple(
        zip(
            RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
            RELEASE_QUALIFICATION_HEADLESS_ROOT_ROLES_V1,
            strict=True,
        )
    )
    return (
        *((desktop, step_id, root_role) for step_id, root_role in functional),
        *((headless, step_id, root_role) for step_id, root_role in functional),
        *((headless, step_id, root_role) for step_id, root_role in extra),
    )


def required_release_qualification_check_ids(
    target_id: str,
) -> tuple[str, ...]:
    desktop, headless = _target_selectors(target_id)
    del desktop, headless
    final = (
        "CROSS_PLATFORM_INTEGER_CORE_BASELINE"
        if target_id == "macos-arm64"
        else "CROSS_PLATFORM_INTEGER_CORE_MATCH"
    )
    result = (
        "CLEAN_PROVIDER",
        "INSTALLED_ARTIFACT_ONLY",
        *(f"DESKTOP:{item}" for item in RELEASE_FUNCTIONAL_STEP_ORDER_V1),
        *(f"HEADLESS:{item}" for item in RELEASE_FUNCTIONAL_STEP_ORDER_V1),
        *(f"HEADLESS:{item}" for item in RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1),
        "SAME_PLATFORM_DESKTOP_HEADLESS",
        final,
    )
    if len(result) != RELEASE_QUALIFICATION_CHECK_COUNT_V1:
        raise RuntimeError("qualification check inventory no longer contains 42 rows")
    return result


def release_qualification_record_paths(target_id: str) -> tuple[str, str]:
    """Return provider and attempt paths relative to the governed release store."""

    _target_selectors(target_id)
    return (
        RELEASE_QUALIFICATION_PROVIDER_RECORD_PATH_BY_TARGET_V1[target_id],
        RELEASE_QUALIFICATION_ATTEMPT_RECORD_PATH_BY_TARGET_V1[target_id],
    )


@dataclass(frozen=True, slots=True)
class ReleaseCleanProviderAttestationV1:
    provider_id: str
    target_id: str
    provider_inventory_sha256: str
    provider_capability_sha256: str
    provider_adapter_id: str
    attestation_method: str
    system: str
    os_version: str
    kernel_release: str
    machine: str
    machine_model: str
    python_implementation: str
    python_version: str
    cpu_count: int
    memory_bytes: int
    available_disk_bytes: int
    offline_install: bool
    network_scope: ReleaseQualificationNetworkScopeV1
    observed_at_utc: str

    schema_id: ClassVar[str] = RELEASE_CLEAN_PROVIDER_ATTESTATION_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _token(self.provider_id, "provider ID")
        if self.target_id not in RELEASE_QUALIFICATION_GATE_BY_TARGET_V1:
            raise ValueError("provider target is outside the frozen platform matrix")
        require_sha256(self.provider_inventory_sha256, "provider inventory digest")
        require_sha256(self.provider_capability_sha256, "provider capability digest")
        expected_provider_id = (
            f"kirby2-clean-provider-{self.target_id}-"
            f"{self.provider_capability_sha256[:16]}"
        )
        if self.provider_id != expected_provider_id:
            raise ValueError("provider ID differs from its capability binding")
        if self.provider_adapter_id != _PROVIDER_ADAPTER_BY_TARGET_V1[self.target_id]:
            raise ValueError("provider adapter differs from the target contract")
        if self.attestation_method != RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1:
            raise ValueError("provider attestation method differs")
        expected_system, expected_machine = _PLATFORM_BY_TARGET_V1[self.target_id]
        if (self.system, self.machine) != (expected_system, expected_machine):
            raise ValueError("provider platform differs from the frozen target")
        for label, value, maximum in (
            ("provider OS version", self.os_version, 512),
            ("provider kernel release", self.kernel_release, 512),
            ("provider machine model", self.machine_model, 512),
        ):
            _text(value, label, maximum)
        if self.python_implementation != "CPython":
            raise ValueError("qualification provider must use CPython")
        if _PYTHON_314.fullmatch(self.python_version) is None:
            raise ValueError("qualification provider must use CPython 3.14")
        _positive(self.cpu_count, "provider CPU count")
        if self.memory_bytes < 8 * 1024**3:
            raise ValueError("qualification provider has less than 8 GiB memory")
        if self.available_disk_bytes < 20 * 1024**3:
            raise ValueError("qualification provider has less than 20 GiB free store")
        if self.offline_install is not True:
            raise ValueError("qualification provider must support offline installation")
        if type(self.network_scope) is not ReleaseQualificationNetworkScopeV1:
            raise TypeError("provider network scope must use the closed V1 enum")
        _utc_second(self.observed_at_utc, "provider observation time")

    def as_dict(self) -> dict[str, object]:
        return {
            "attestation_method": self.attestation_method,
            "available_disk_bytes": self.available_disk_bytes,
            "cpu_count": self.cpu_count,
            "kernel_release": self.kernel_release,
            "machine": self.machine,
            "machine_model": self.machine_model,
            "memory_bytes": self.memory_bytes,
            "network_scope": self.network_scope.value,
            "observed_at_utc": self.observed_at_utc,
            "offline_install": self.offline_install,
            "os_version": self.os_version,
            "provider_adapter_id": self.provider_adapter_id,
            "provider_capability_sha256": self.provider_capability_sha256,
            "provider_id": self.provider_id,
            "provider_inventory_sha256": self.provider_inventory_sha256,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "system": self.system,
            "target_id": self.target_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseCleanProviderAttestationV1":
        value = load_canonical_json_bytes(raw, "clean-provider attestation")
        row = _exact(
            value,
            {
                "attestation_method",
                "available_disk_bytes",
                "cpu_count",
                "kernel_release",
                "machine",
                "machine_model",
                "memory_bytes",
                "network_scope",
                "observed_at_utc",
                "offline_install",
                "os_version",
                "provider_adapter_id",
                "provider_capability_sha256",
                "provider_id",
                "provider_inventory_sha256",
                "python_implementation",
                "python_version",
                "schema_id",
                "schema_version",
                "system",
                "target_id",
            },
            "clean-provider attestation",
        )
        if (
            row["schema_id"] != cls.schema_id
            or row["schema_version"] != cls.schema_version
        ):
            raise ValueError("clean-provider attestation identity differs")
        instance = cls(
            provider_id=_token(row["provider_id"], "provider ID"),
            target_id=_text(row["target_id"], "provider target", 128),
            provider_inventory_sha256=_sha256(
                row["provider_inventory_sha256"], "provider inventory digest"
            ),
            provider_capability_sha256=_sha256(
                row["provider_capability_sha256"], "provider capability digest"
            ),
            provider_adapter_id=_token(
                row["provider_adapter_id"], "provider adapter ID"
            ),
            attestation_method=_token(
                row["attestation_method"], "provider attestation method"
            ),
            system=_text(row["system"], "provider system", 128),
            os_version=_text(row["os_version"], "provider OS version", 512),
            kernel_release=_text(
                row["kernel_release"], "provider kernel release", 512
            ),
            machine=_text(row["machine"], "provider machine", 128),
            machine_model=_text(
                row["machine_model"], "provider machine model", 512
            ),
            python_implementation=_text(
                row["python_implementation"], "provider Python implementation", 128
            ),
            python_version=_text(
                row["python_version"], "provider Python version", 128
            ),
            cpu_count=_positive(row["cpu_count"], "provider CPU count"),
            memory_bytes=_positive(row["memory_bytes"], "provider memory"),
            available_disk_bytes=_positive(
                row["available_disk_bytes"], "provider available disk"
            ),
            offline_install=row["offline_install"],  # type: ignore[arg-type]
            network_scope=_network_scope(
                row["network_scope"], "provider network scope"
            ),
            observed_at_utc=_utc_second(
                row["observed_at_utc"], "provider observation time"
            ),
        )
        if instance.canonical_bytes() != raw:
            raise ValueError("clean-provider attestation bytes are not canonical")
        return instance


@dataclass(frozen=True, slots=True)
class ReleaseQualificationArtifactBindingV1:
    artifact_id: str
    size: int
    release_store_sha256: str
    provider_copy_sha256: str

    def __post_init__(self) -> None:
        _token(self.artifact_id, "qualification artifact ID")
        _positive(self.size, "qualification artifact size")
        require_sha256(self.release_store_sha256, "release-store artifact digest")
        require_sha256(self.provider_copy_sha256, "provider-copy artifact digest")
        if self.release_store_sha256 != self.provider_copy_sha256:
            raise ValueError("provider artifact copy differs from the release store")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "provider_copy_sha256": self.provider_copy_sha256,
            "release_store_sha256": self.release_store_sha256,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationArtifactBindingV1":
        row = _exact(
            value,
            {
                "artifact_id",
                "provider_copy_sha256",
                "release_store_sha256",
                "size",
            },
            "qualification artifact binding",
        )
        return cls(
            artifact_id=_token(row["artifact_id"], "qualification artifact ID"),
            size=_positive(row["size"], "qualification artifact size"),
            release_store_sha256=_sha256(
                row["release_store_sha256"], "release-store artifact digest"
            ),
            provider_copy_sha256=_sha256(
                row["provider_copy_sha256"], "provider-copy artifact digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationRootObservationV1:
    root_role: str
    root_id: str
    data_root: str
    initial_state: str
    initial_inventory_sha256: str

    def __post_init__(self) -> None:
        if self.root_role not in RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1:
            raise ValueError("clean-root role is outside the V1 record")
        _token(self.root_id, "clean-root ID")
        root = _text(self.data_root, "clean data root", 1024)
        if not root.startswith("/") or root == "/" or ".." in root.split("/"):
            raise ValueError("clean data root must be one non-root absolute guest path")
        if self.initial_state != "ABSENT":
            raise ValueError("qualification data roots must initially be absent")
        require_sha256(self.initial_inventory_sha256, "initial root inventory digest")
        if self.initial_inventory_sha256 != _EMPTY_INVENTORY_SHA256:
            raise ValueError("absent root must bind the canonical empty inventory")

    def as_dict(self) -> dict[str, object]:
        return {
            "data_root": self.data_root,
            "initial_inventory_sha256": self.initial_inventory_sha256,
            "initial_state": self.initial_state,
            "root_id": self.root_id,
            "root_role": self.root_role,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationRootObservationV1":
        row = _exact(
            value,
            {
                "data_root",
                "initial_inventory_sha256",
                "initial_state",
                "root_id",
                "root_role",
            },
            "qualification root observation",
        )
        return cls(
            root_role=_text(row["root_role"], "clean-root role", 128),
            root_id=_token(row["root_id"], "clean-root ID"),
            data_root=_text(row["data_root"], "clean data root", 1024),
            initial_state=_text(row["initial_state"], "initial root state", 64),
            initial_inventory_sha256=_sha256(
                row["initial_inventory_sha256"], "initial root inventory digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationSessionV1:
    session_id: str
    provider_id: str
    provider_attestation_sha256: str
    provider_instance_id: str
    target_id: str
    attempt_number: int
    started_at_utc: str
    finished_at_utc: str
    duration_ns: int
    network_scope: ReleaseQualificationNetworkScopeV1
    installation_source: str
    source_checkout_present: bool
    artifact_bindings: tuple[ReleaseQualificationArtifactBindingV1, ...]
    roots: tuple[ReleaseQualificationRootObservationV1, ...]

    def __post_init__(self) -> None:
        _token(self.session_id, "qualification session ID")
        _token(self.provider_id, "qualification provider ID")
        require_sha256(
            self.provider_attestation_sha256,
            "qualification provider-attestation digest",
        )
        _token(self.provider_instance_id, "qualification provider-instance ID")
        if self.target_id not in RELEASE_QUALIFICATION_GATE_BY_TARGET_V1:
            raise ValueError("qualification session target is invalid")
        if self.attempt_number != RELEASE_QUALIFICATION_ATTEMPT_NUMBER_V1:
            raise ValueError("qualification session must be attempt one")
        _utc_second(self.started_at_utc, "qualification session start")
        _utc_second(self.finished_at_utc, "qualification session finish")
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError("qualification session finishes before it starts")
        _nonnegative(self.duration_ns, "qualification session duration")
        if type(self.network_scope) is not ReleaseQualificationNetworkScopeV1:
            raise TypeError("session network scope must use the closed V1 enum")
        if self.installation_source != RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1:
            raise ValueError("qualification installation source differs")
        if self.source_checkout_present is not False:
            raise ValueError("qualification cannot use a source checkout in the provider")
        if type(self.artifact_bindings) is not tuple or any(
            type(item) is not ReleaseQualificationArtifactBindingV1
            for item in self.artifact_bindings
        ):
            raise TypeError("qualification artifact bindings must be typed")
        artifact_ids = tuple(item.artifact_id for item in self.artifact_bindings)
        if artifact_ids != RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[self.target_id]:
            raise ValueError("qualification artifact binding order or inventory differs")
        if type(self.roots) is not tuple or any(
            type(item) is not ReleaseQualificationRootObservationV1
            for item in self.roots
        ):
            raise TypeError("qualification roots must be typed")
        if tuple(item.root_role for item in self.roots) != (
            RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1
        ):
            raise ValueError("qualification clean-root order differs")
        root_ids = tuple(item.root_id for item in self.roots)
        root_paths = tuple(item.data_root for item in self.roots)
        if len(set(root_ids)) != len(root_ids) or len(set(root_paths)) != len(root_paths):
            raise ValueError("qualification clean roots must be distinct")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_bindings": [item.as_dict() for item in self.artifact_bindings],
            "attempt_number": self.attempt_number,
            "duration_ns": self.duration_ns,
            "finished_at_utc": self.finished_at_utc,
            "installation_source": self.installation_source,
            "network_scope": self.network_scope.value,
            "provider_attestation_sha256": self.provider_attestation_sha256,
            "provider_id": self.provider_id,
            "provider_instance_id": self.provider_instance_id,
            "roots": [item.as_dict() for item in self.roots],
            "session_id": self.session_id,
            "source_checkout_present": self.source_checkout_present,
            "started_at_utc": self.started_at_utc,
            "target_id": self.target_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationSessionV1":
        row = _exact(
            value,
            {
                "artifact_bindings",
                "attempt_number",
                "duration_ns",
                "finished_at_utc",
                "installation_source",
                "network_scope",
                "provider_attestation_sha256",
                "provider_id",
                "provider_instance_id",
                "roots",
                "session_id",
                "source_checkout_present",
                "started_at_utc",
                "target_id",
            },
            "qualification session",
        )
        return cls(
            session_id=_token(row["session_id"], "qualification session ID"),
            provider_id=_token(row["provider_id"], "qualification provider ID"),
            provider_attestation_sha256=_sha256(
                row["provider_attestation_sha256"],
                "qualification provider-attestation digest",
            ),
            provider_instance_id=_token(
                row["provider_instance_id"], "qualification provider-instance ID"
            ),
            target_id=_text(row["target_id"], "qualification session target", 128),
            attempt_number=_nonnegative(
                row["attempt_number"], "qualification attempt number"
            ),
            started_at_utc=_utc_second(
                row["started_at_utc"], "qualification session start"
            ),
            finished_at_utc=_utc_second(
                row["finished_at_utc"], "qualification session finish"
            ),
            duration_ns=_nonnegative(
                row["duration_ns"], "qualification session duration"
            ),
            network_scope=_network_scope(
                row["network_scope"], "qualification session network scope"
            ),
            installation_source=_text(
                row["installation_source"], "qualification installation source", 128
            ),
            source_checkout_present=row["source_checkout_present"],  # type: ignore[arg-type]
            artifact_bindings=tuple(
                ReleaseQualificationArtifactBindingV1.from_dict(item)
                for item in _array(
                    row["artifact_bindings"], "qualification artifact bindings"
                )
            ),
            roots=tuple(
                ReleaseQualificationRootObservationV1.from_dict(item)
                for item in _array(row["roots"], "qualification roots")
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationCommandObservationV1:
    command_id: str
    sequence: int
    artifact_selector: str | None
    root_role: str
    argv: tuple[str, ...]
    environment_sha256: str
    started_at_utc: str
    duration_ns: int
    returncode: int
    timed_out: bool
    stdout_size: int
    stdout_sha256: str
    stderr_size: int
    stderr_sha256: str

    def __post_init__(self) -> None:
        _token(self.command_id, "qualification command ID")
        _positive(self.sequence, "qualification command sequence")
        if self.artifact_selector is not None:
            _text(self.artifact_selector, "qualification artifact selector", 256)
        if self.root_role not in _COMMAND_ROOT_ROLES_V1:
            raise ValueError("qualification command root role is invalid")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or len(self.argv) > 256
            or any(type(item) is not str for item in self.argv)
        ):
            raise TypeError("qualification command argv must be a bounded text tuple")
        for item in self.argv:
            _text(item, "qualification argv item", 4096)
            if "\x00" in item:
                raise ValueError("qualification argv cannot contain NUL")
        require_sha256(self.environment_sha256, "qualification environment digest")
        _utc_second(self.started_at_utc, "qualification command start")
        _nonnegative(self.duration_ns, "qualification command duration")
        if type(self.returncode) is not int or not -255 <= self.returncode <= 255:
            raise ValueError("qualification command return code is invalid")
        if type(self.timed_out) is not bool:
            raise TypeError("qualification command timeout state must be Boolean")
        _nonnegative(self.stdout_size, "qualification stdout size")
        require_sha256(self.stdout_sha256, "qualification stdout digest")
        _nonnegative(self.stderr_size, "qualification stderr size")
        require_sha256(self.stderr_sha256, "qualification stderr digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "artifact_selector": self.artifact_selector,
            "command_id": self.command_id,
            "duration_ns": self.duration_ns,
            "environment_sha256": self.environment_sha256,
            "returncode": self.returncode,
            "root_role": self.root_role,
            "sequence": self.sequence,
            "started_at_utc": self.started_at_utc,
            "stderr_sha256": self.stderr_sha256,
            "stderr_size": self.stderr_size,
            "stdout_sha256": self.stdout_sha256,
            "stdout_size": self.stdout_size,
            "timed_out": self.timed_out,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationCommandObservationV1":
        row = _exact(
            value,
            {
                "argv",
                "artifact_selector",
                "command_id",
                "duration_ns",
                "environment_sha256",
                "returncode",
                "root_role",
                "sequence",
                "started_at_utc",
                "stderr_sha256",
                "stderr_size",
                "stdout_sha256",
                "stdout_size",
                "timed_out",
            },
            "qualification command observation",
        )
        selector = row["artifact_selector"]
        if selector is not None and type(selector) is not str:
            raise TypeError("qualification command selector must be text or null")
        return cls(
            command_id=_token(row["command_id"], "qualification command ID"),
            sequence=_positive(row["sequence"], "qualification command sequence"),
            artifact_selector=selector,
            root_role=_text(row["root_role"], "qualification command root role", 128),
            argv=tuple(
                _text(item, "qualification argv item", 4096)
                for item in _array(row["argv"], "qualification command argv")
            ),
            environment_sha256=_sha256(
                row["environment_sha256"], "qualification environment digest"
            ),
            started_at_utc=_utc_second(
                row["started_at_utc"], "qualification command start"
            ),
            duration_ns=_nonnegative(
                row["duration_ns"], "qualification command duration"
            ),
            returncode=row["returncode"],  # type: ignore[arg-type]
            timed_out=row["timed_out"],  # type: ignore[arg-type]
            stdout_size=_nonnegative(row["stdout_size"], "qualification stdout size"),
            stdout_sha256=_sha256(
                row["stdout_sha256"], "qualification stdout digest"
            ),
            stderr_size=_nonnegative(row["stderr_size"], "qualification stderr size"),
            stderr_sha256=_sha256(
                row["stderr_sha256"], "qualification stderr digest"
            ),
        )


def _step_payload(
    payload: object,
    fields: set[str],
    step_id: str,
) -> dict[str, object]:
    row = _exact(payload, {"execution_index", *fields}, f"{step_id} result payload")
    _positive(row["execution_index"], f"{step_id} execution index")
    return row


def _payload_bool(
    row: dict[str, object],
    field: str,
    expected: bool,
    step_id: str,
) -> None:
    if type(row[field]) is not bool or row[field] is not expected:
        raise ValueError(f"{step_id} {field} semantic predicate is false")


def _payload_integer(
    row: dict[str, object],
    field: str,
    expected: int,
    step_id: str,
) -> None:
    if type(row[field]) is not int or row[field] != expected:
        raise ValueError(f"{step_id} {field} integer differs")


def _payload_digest(row: dict[str, object], field: str, step_id: str) -> str:
    return _sha256(row[field], f"{step_id} {field}")


def _payload_token(row: dict[str, object], field: str, step_id: str) -> str:
    return _token(row[field], f"{step_id} {field}")


def _validate_clean_install_payload(payload: object) -> None:
    step_id = "CLEAN_INSTALL"
    row = _step_payload(
        payload,
        {"distributions", "offline_install", "source_checkout_present"},
        step_id,
    )
    distributions = _array(row["distributions"], f"{step_id} distributions")
    expected = [
        {
            "name": "duckdb",
            "origin": "LOCKED_DEPENDENCY_WHEEL",
            "version": "1.5.5",
        },
        {
            "name": "kirby2",
            "origin": "CANDIDATE_PROJECT_WHEEL",
            "version": "0.1.0",
        },
    ]
    if distributions != expected:
        raise ValueError("CLEAN_INSTALL distribution inventory or origins differ")
    _payload_bool(row, "offline_install", True, step_id)
    _payload_bool(row, "source_checkout_present", False, step_id)


def _validate_launch_payload(payload: object) -> None:
    step_id = "LAUNCH"
    fields = {
        "application_ready",
        "background_daemon_present",
        "brokerage_connector_present",
        "live_market_connector_present",
        "synthetic_training_environment",
        "telemetry_present",
        "updater_present",
    }
    row = _step_payload(payload, fields, step_id)
    _payload_bool(row, "application_ready", True, step_id)
    _payload_bool(row, "synthetic_training_environment", True, step_id)
    for field in (
        "background_daemon_present",
        "brokerage_connector_present",
        "live_market_connector_present",
        "telemetry_present",
        "updater_present",
    ):
        _payload_bool(row, field, False, step_id)


def _validate_first_run_payload(payload: object) -> None:
    step_id = "FULL_FIRST_RUN"
    row = _step_payload(
        payload,
        {
            "first_run_complete",
            "governed_path_count",
            "healthy_path_count",
            "starter_entry_count",
            "starter_set_id",
        },
        step_id,
    )
    _payload_bool(row, "first_run_complete", True, step_id)
    _payload_integer(row, "governed_path_count", 16, step_id)
    _payload_integer(row, "healthy_path_count", 16, step_id)
    _payload_integer(row, "starter_entry_count", 2, step_id)
    if row["starter_set_id"] != "RELEASE_STARTER_SET_V1":
        raise ValueError("FULL_FIRST_RUN starter-set identity differs")


def _validate_starter_lesson_payload(payload: object) -> None:
    step_id = "STARTER_LESSON"
    row = _step_payload(
        payload,
        {"lesson_id", "lesson_ready", "synthetic_only"},
        step_id,
    )
    if row["lesson_id"] != "KIRBY2_STARTER_PLACE_CANCEL_V1":
        raise ValueError("STARTER_LESSON lesson identity differs")
    _payload_bool(row, "lesson_ready", True, step_id)
    _payload_bool(row, "synthetic_only", True, step_id)


def _validate_place_cancel_payload(payload: object) -> None:
    step_id = "PLACE_CANCEL"
    row = _step_payload(
        payload,
        {
            "cancelled_order_count",
            "conservation_passed",
            "event_stream_sha256",
            "fill_count",
        },
        step_id,
    )
    _payload_integer(row, "fill_count", 0, step_id)
    _payload_integer(row, "cancelled_order_count", 1, step_id)
    _payload_bool(row, "conservation_passed", True, step_id)
    _payload_digest(row, "event_stream_sha256", step_id)


def _validate_complete_save_payload(payload: object) -> None:
    step_id = "COMPLETE_SAVE"
    row = _step_payload(
        payload,
        {
            "immutable_manifests_committed",
            "run_id",
            "run_manifest_sha256",
            "run_sha256",
            "session_manifest_sha256",
        },
        step_id,
    )
    _payload_token(row, "run_id", step_id)
    _payload_digest(row, "run_sha256", step_id)
    _payload_digest(row, "run_manifest_sha256", step_id)
    _payload_digest(row, "session_manifest_sha256", step_id)
    _payload_bool(row, "immutable_manifests_committed", True, step_id)


def _validate_microscope_payload(payload: object) -> None:
    step_id = "OPEN_REPLAY_MICROSCOPE"
    row = _step_payload(
        payload,
        {
            "offline",
            "pane_count",
            "report_sha256",
            "saved_run_id",
            "supported_panes_rendered",
        },
        step_id,
    )
    _payload_token(row, "saved_run_id", step_id)
    _payload_integer(row, "pane_count", 18, step_id)
    _payload_bool(row, "supported_panes_rendered", True, step_id)
    _payload_bool(row, "offline", True, step_id)
    _payload_digest(row, "report_sha256", step_id)


def _validate_export_pack_payload(payload: object) -> None:
    step_id = "EXPORT_PACK"
    row = _step_payload(
        payload,
        {"adapter_id", "adapter_verified", "pack_id", "pack_sha256"},
        step_id,
    )
    _payload_token(row, "adapter_id", step_id)
    _payload_digest(row, "pack_id", step_id)
    _payload_digest(row, "pack_sha256", step_id)
    _payload_bool(row, "adapter_verified", True, step_id)


def _validate_close_payload(payload: object) -> None:
    step_id = "CLOSE"
    row = _step_payload(
        payload,
        {"active_mutation_count", "closed_cleanly"},
        step_id,
    )
    _payload_integer(row, "active_mutation_count", 0, step_id)
    _payload_bool(row, "closed_cleanly", True, step_id)


def _validate_reopen_payload(payload: object) -> None:
    step_id = "REOPEN_VERIFY_SAVED"
    row = _step_payload(
        payload,
        {"replay_sha256", "saved_run_id", "verification_status"},
        step_id,
    )
    _payload_token(row, "saved_run_id", step_id)
    _payload_digest(row, "replay_sha256", step_id)
    if row["verification_status"] != "PASS":
        raise ValueError("REOPEN_VERIFY_SAVED verification did not pass")


def _validate_import_payload(payload: object) -> None:
    step_id = "IMPORT_SECOND_CLEAN_ROOT"
    row = _step_payload(
        payload,
        {"dependencies_satisfied", "pack_active", "pack_id", "secondary_root_id"},
        step_id,
    )
    _payload_digest(row, "pack_id", step_id)
    _payload_token(row, "secondary_root_id", step_id)
    _payload_bool(row, "pack_active", True, step_id)
    _payload_bool(row, "dependencies_satisfied", True, step_id)


def _validate_imported_replay_payload(payload: object) -> None:
    step_id = "REPLAY_IMPORTED_LESSON"
    row = _step_payload(
        payload,
        {"replay_executed", "replay_sha256", "verification_status"},
        step_id,
    )
    _payload_bool(row, "replay_executed", True, step_id)
    _payload_digest(row, "replay_sha256", step_id)
    if row["verification_status"] != "PASS":
        raise ValueError("REPLAY_IMPORTED_LESSON verification did not pass")


def _validate_digest_compare_payload(payload: object) -> None:
    step_id = "COMPARE_DECLARED_REPLAY_DIGEST"
    row = _step_payload(
        payload,
        {"digests_equal", "primary_replay_sha256", "secondary_replay_sha256"},
        step_id,
    )
    primary = _payload_digest(row, "primary_replay_sha256", step_id)
    secondary = _payload_digest(row, "secondary_replay_sha256", step_id)
    _payload_bool(row, "digests_equal", True, step_id)
    if primary != secondary:
        raise ValueError("COMPARE_DECLARED_REPLAY_DIGEST values differ")


def _validate_restore_payload(payload: object) -> None:
    step_id = "RESTORE_BACKUP"
    row = _step_payload(
        payload,
        {
            "backup_sha256",
            "overwrite_performed",
            "restore_receipt_sha256",
            "restore_verified",
            "restored_inventory_sha256",
        },
        step_id,
    )
    _payload_digest(row, "backup_sha256", step_id)
    _payload_digest(row, "restore_receipt_sha256", step_id)
    _payload_digest(row, "restored_inventory_sha256", step_id)
    _payload_bool(row, "restore_verified", True, step_id)
    _payload_bool(row, "overwrite_performed", False, step_id)


def _validate_crash_recovery_payload(payload: object) -> None:
    step_id = "CRASH_RECOVERY"
    row = _step_payload(
        payload,
        {
            "committed_checkpoint_sha256",
            "last_committed_checkpoint_recovered",
            "recovered_checkpoint_sha256",
        },
        step_id,
    )
    committed = _payload_digest(row, "committed_checkpoint_sha256", step_id)
    recovered = _payload_digest(row, "recovered_checkpoint_sha256", step_id)
    _payload_bool(row, "last_committed_checkpoint_recovered", True, step_id)
    if committed != recovered:
        raise ValueError("CRASH_RECOVERY did not restore the committed checkpoint")


def _validate_diagnostics_payload(payload: object) -> None:
    step_id = "EXPORT_DIAGNOSTICS"
    row = _step_payload(
        payload,
        {
            "diagnostics_sha256",
            "direct_identity_disposition",
            "new_file_created",
            "receipt_sha256",
            "secrets_disposition",
        },
        step_id,
    )
    _payload_digest(row, "diagnostics_sha256", step_id)
    _payload_digest(row, "receipt_sha256", step_id)
    _payload_bool(row, "new_file_created", True, step_id)
    if (
        row["direct_identity_disposition"] != "EXCLUDED"
        or row["secrets_disposition"] != "EXCLUDED"
    ):
        raise ValueError("EXPORT_DIAGNOSTICS redaction dispositions differ")


def _validate_uninstall_payload(payload: object) -> None:
    step_id = "UNINSTALL_PRESERVE_USER_DATA"
    row = _step_payload(
        payload,
        {
            "application_artifacts_removed",
            "application_importable_after",
            "application_importable_before",
            "inventories_equal",
            "user_data_inventory_after_sha256",
            "user_data_inventory_before_sha256",
        },
        step_id,
    )
    before = _payload_digest(row, "user_data_inventory_before_sha256", step_id)
    after = _payload_digest(row, "user_data_inventory_after_sha256", step_id)
    _payload_bool(row, "application_importable_before", True, step_id)
    _payload_bool(row, "application_importable_after", False, step_id)
    _payload_bool(row, "application_artifacts_removed", True, step_id)
    _payload_bool(row, "inventories_equal", True, step_id)
    if before != after:
        raise ValueError("UNINSTALL_PRESERVE_USER_DATA changed user data")


def _validate_headless_simulation_payload(payload: object) -> None:
    step_id = "HEADLESS_SIMULATION"
    row = _step_payload(
        payload,
        {
            "cross_platform_integer_core_sha256",
            "replay_sha256",
            "replay_verified",
            "simulation_executed",
            "simulation_sha256",
        },
        step_id,
    )
    _payload_bool(row, "simulation_executed", True, step_id)
    _payload_bool(row, "replay_verified", True, step_id)
    _payload_digest(row, "simulation_sha256", step_id)
    _payload_digest(row, "replay_sha256", step_id)
    _payload_digest(row, "cross_platform_integer_core_sha256", step_id)


def _validate_headless_audit_payload(payload: object) -> None:
    step_id = "HEADLESS_AUDIT"
    row = _step_payload(
        payload,
        {"audit_gate_id", "audit_sha256", "audit_status"},
        step_id,
    )
    _payload_token(row, "audit_gate_id", step_id)
    _payload_digest(row, "audit_sha256", step_id)
    if row["audit_status"] != "PASS":
        raise ValueError("HEADLESS_AUDIT result did not pass")


def _validate_headless_calibration_payload(payload: object) -> None:
    step_id = "HEADLESS_CALIBRATION"
    row = _step_payload(
        payload,
        {"calibration_artifact_sha256", "verification_status", "verified"},
        step_id,
    )
    _payload_digest(row, "calibration_artifact_sha256", step_id)
    _payload_bool(row, "verified", True, step_id)
    if row["verification_status"] != "PASS":
        raise ValueError("HEADLESS_CALIBRATION verification did not pass")


def _validate_headless_distributed_payload(payload: object) -> None:
    step_id = "HEADLESS_DISTRIBUTED_WORKER"
    row = _step_payload(
        payload,
        {
            "artifact_set_sha256",
            "artifact_set_verified",
            "result_sha256",
            "signature_verified",
            "work_unit_id",
        },
        step_id,
    )
    _payload_token(row, "work_unit_id", step_id)
    _payload_digest(row, "result_sha256", step_id)
    _payload_digest(row, "artifact_set_sha256", step_id)
    _payload_bool(row, "signature_verified", True, step_id)
    _payload_bool(row, "artifact_set_verified", True, step_id)


_STEP_RESULT_VALIDATORS_V1 = {
    (None, "CLEAN_INSTALL"): _validate_clean_install_payload,
    (None, "LAUNCH"): _validate_launch_payload,
    (None, "FULL_FIRST_RUN"): _validate_first_run_payload,
    (None, "STARTER_LESSON"): _validate_starter_lesson_payload,
    (None, "PLACE_CANCEL"): _validate_place_cancel_payload,
    (None, "COMPLETE_SAVE"): _validate_complete_save_payload,
    (None, "OPEN_REPLAY_MICROSCOPE"): _validate_microscope_payload,
    (None, "EXPORT_PACK"): _validate_export_pack_payload,
    (None, "CLOSE"): _validate_close_payload,
    (None, "REOPEN_VERIFY_SAVED"): _validate_reopen_payload,
    (None, "IMPORT_SECOND_CLEAN_ROOT"): _validate_import_payload,
    (None, "REPLAY_IMPORTED_LESSON"): _validate_imported_replay_payload,
    (None, "COMPARE_DECLARED_REPLAY_DIGEST"): _validate_digest_compare_payload,
    (None, "RESTORE_BACKUP"): _validate_restore_payload,
    (None, "CRASH_RECOVERY"): _validate_crash_recovery_payload,
    (None, "EXPORT_DIAGNOSTICS"): _validate_diagnostics_payload,
    (None, "UNINSTALL_PRESERVE_USER_DATA"): _validate_uninstall_payload,
    ("headless", "HEADLESS_SIMULATION"): _validate_headless_simulation_payload,
    ("headless", "HEADLESS_AUDIT"): _validate_headless_audit_payload,
    ("headless", "HEADLESS_CALIBRATION"): _validate_headless_calibration_payload,
    ("headless", "HEADLESS_DISTRIBUTED_WORKER"): _validate_headless_distributed_payload,
}

if len(_STEP_RESULT_VALIDATORS_V1) != 21:
    raise RuntimeError("qualification step-result validator inventory differs")


def _validate_step_result_payload(
    artifact_selector: str,
    step_id: str,
    payload: object,
) -> None:
    form = "headless" if artifact_selector.endswith("/headless") else "desktop"
    validator = _STEP_RESULT_VALIDATORS_V1.get((form, step_id))
    if validator is None:
        validator = _STEP_RESULT_VALIDATORS_V1.get((None, step_id))
    if validator is None:
        raise ValueError("qualification step lacks a closed result validator")
    validator(payload)


@dataclass(frozen=True, slots=True)
class ReleaseQualificationStepResultV1:
    result_id: str
    size: int
    sha256: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        _token(self.result_id, "qualification step result ID")
        if type(self.payload) is not dict:
            raise TypeError("qualification step result payload must be an object")
        raw = canonical_json_bytes(self.payload)
        if not raw:
            raise ValueError("qualification step result payload cannot be empty")
        if self.size != len(raw):
            raise ValueError("qualification step result size differs from its payload")
        require_sha256(self.sha256, "qualification step result digest")
        if self.sha256 != hashlib.sha256(raw).hexdigest():
            raise ValueError("qualification step result digest differs from its payload")

    def as_dict(self) -> dict[str, object]:
        return {
            "payload": self.payload,
            "result_id": self.result_id,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_payload(
        cls,
        result_id: str,
        payload: dict[str, object],
    ) -> "ReleaseQualificationStepResultV1":
        if type(payload) is not dict:
            raise TypeError("qualification step result payload must be an object")
        selected = dict(payload)
        raw = canonical_json_bytes(selected)
        return cls(
            result_id=result_id,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            payload=selected,
        )

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationStepResultV1":
        row = _exact(
            value,
            {"payload", "result_id", "sha256", "size"},
            "qualification step result",
        )
        payload = row["payload"]
        if type(payload) is not dict:
            raise TypeError("qualification step result payload must be an object")
        return cls(
            result_id=_token(row["result_id"], "qualification step result ID"),
            size=_positive(row["size"], "qualification step result size"),
            sha256=_sha256(row["sha256"], "qualification step result digest"),
            payload=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationStepObservationV1:
    artifact_selector: str
    step_id: str
    root_role: str
    command_ids: tuple[str, ...]
    duration_ns: int
    status: str
    warning_codes: tuple[str, ...]
    result: ReleaseQualificationStepResultV1 | None

    def __post_init__(self) -> None:
        _text(self.artifact_selector, "qualification step selector", 256)
        _token(self.step_id, "qualification step ID")
        if self.root_role not in _COMMAND_ROOT_ROLES_V1 - {"PROVIDER"}:
            raise ValueError("qualification step root role is invalid")
        if type(self.command_ids) is not tuple or any(
            type(item) is not str for item in self.command_ids
        ):
            raise TypeError("qualification step command IDs must be a text tuple")
        for item in self.command_ids:
            _token(item, "qualification step command ID")
        if len(self.command_ids) != len(set(self.command_ids)):
            raise ValueError("qualification step command IDs must be unique")
        _nonnegative(self.duration_ns, "qualification step duration")
        if self.status not in _STEP_STATUSES_V1:
            raise ValueError("qualification step status is invalid")
        if type(self.warning_codes) is not tuple or any(
            type(item) is not str for item in self.warning_codes
        ):
            raise TypeError("qualification step warnings must be a text tuple")
        for item in self.warning_codes:
            _token(item, "qualification step warning code")
        if self.warning_codes != tuple(
            sorted(self.warning_codes, key=lambda item: item.encode("utf-8"))
        ) or len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("qualification step warnings must be unique and sorted")
        if (self.status == "WARNING") != bool(self.warning_codes):
            raise ValueError("qualification warning status and warning codes differ")
        if self.status == "NOT_EXERCISED":
            if self.command_ids or self.result is not None:
                raise ValueError("unexercised qualification step cannot claim execution")
        else:
            if not self.command_ids or type(self.result) is not ReleaseQualificationStepResultV1:
                raise ValueError("exercised qualification step requires commands and a result")
            expected_result_id = f"{self.artifact_selector}:{self.step_id}"
            if self.result.result_id != expected_result_id:
                raise ValueError("qualification step result identity differs")
            _validate_step_result_payload(
                self.artifact_selector,
                self.step_id,
                self.result.payload,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_selector": self.artifact_selector,
            "command_ids": list(self.command_ids),
            "duration_ns": self.duration_ns,
            "result": None if self.result is None else self.result.as_dict(),
            "root_role": self.root_role,
            "status": self.status,
            "step_id": self.step_id,
            "warning_codes": list(self.warning_codes),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationStepObservationV1":
        row = _exact(
            value,
            {
                "artifact_selector",
                "command_ids",
                "duration_ns",
                "result",
                "root_role",
                "status",
                "step_id",
                "warning_codes",
            },
            "qualification step observation",
        )
        result = row["result"]
        return cls(
            artifact_selector=_text(
                row["artifact_selector"], "qualification step selector", 256
            ),
            step_id=_token(row["step_id"], "qualification step ID"),
            root_role=_text(row["root_role"], "qualification step root role", 128),
            command_ids=tuple(
                _token(item, "qualification step command ID")
                for item in _array(row["command_ids"], "qualification step command IDs")
            ),
            duration_ns=_nonnegative(
                row["duration_ns"], "qualification step duration"
            ),
            status=_text(row["status"], "qualification step status", 64),
            warning_codes=tuple(
                _token(item, "qualification step warning code")
                for item in _array(row["warning_codes"], "qualification step warnings")
            ),
            result=(
                None
                if result is None
                else ReleaseQualificationStepResultV1.from_dict(result)
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationFactsV1:
    clean_environment: bool
    cross_platform_integer_core_sha256: str
    desktop_run_sha256: str
    headless_run_sha256: str
    platform_id: str
    replay_sha256: str

    def __post_init__(self) -> None:
        if type(self.clean_environment) is not bool:
            raise TypeError("clean-environment fact must be Boolean")
        if self.platform_id not in RELEASE_QUALIFICATION_GATE_BY_TARGET_V1:
            raise ValueError("qualification fact platform is invalid")
        for value, label in (
            (
                self.cross_platform_integer_core_sha256,
                "cross-platform integer-core digest",
            ),
            (self.desktop_run_sha256, "desktop run digest"),
            (self.headless_run_sha256, "headless run digest"),
            (self.replay_sha256, "qualification replay digest"),
        ):
            require_sha256(value, label)

    def as_dict(self) -> dict[str, object]:
        return {
            "clean_environment": self.clean_environment,
            "cross_platform_integer_core_sha256": self.cross_platform_integer_core_sha256,
            "desktop_run_sha256": self.desktop_run_sha256,
            "headless_run_sha256": self.headless_run_sha256,
            "platform_id": self.platform_id,
            "replay_sha256": self.replay_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationFactsV1":
        row = _exact(
            value,
            {
                "clean_environment",
                "cross_platform_integer_core_sha256",
                "desktop_run_sha256",
                "headless_run_sha256",
                "platform_id",
                "replay_sha256",
            },
            "qualification facts",
        )
        return cls(
            clean_environment=row["clean_environment"],  # type: ignore[arg-type]
            cross_platform_integer_core_sha256=_sha256(
                row["cross_platform_integer_core_sha256"],
                "cross-platform integer-core digest",
            ),
            desktop_run_sha256=_sha256(
                row["desktop_run_sha256"], "desktop run digest"
            ),
            headless_run_sha256=_sha256(
                row["headless_run_sha256"], "headless run digest"
            ),
            platform_id=_text(row["platform_id"], "qualification fact platform", 128),
            replay_sha256=_sha256(
                row["replay_sha256"], "qualification replay digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationCheckV1:
    check_id: str
    evidence_sha256: str
    status: str

    def __post_init__(self) -> None:
        _token(self.check_id, "qualification check ID")
        require_sha256(self.evidence_sha256, "qualification check evidence digest")
        if self.status not in _STEP_STATUSES_V1:
            raise ValueError("qualification check status is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "evidence_sha256": self.evidence_sha256,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseQualificationCheckV1":
        row = _exact(
            value,
            {"check_id", "evidence_sha256", "status"},
            "qualification check",
        )
        return cls(
            check_id=_token(row["check_id"], "qualification check ID"),
            evidence_sha256=_sha256(
                row["evidence_sha256"], "qualification check evidence digest"
            ),
            status=_text(row["status"], "qualification check status", 64),
        )


def _check_digest(check_id: str, proof: object) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "check_id": check_id,
                "proof": proof,
                "schema_id": RELEASE_QUALIFICATION_CHECK_PROOF_SCHEMA_ID_V1,
                "schema_version": RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1,
            }
        )
    ).hexdigest()


def reconstruct_release_qualification_checks(
    *,
    target_id: str,
    session: ReleaseQualificationSessionV1,
    commands: tuple[ReleaseQualificationCommandObservationV1, ...],
    steps: tuple[ReleaseQualificationStepObservationV1, ...],
    facts: ReleaseQualificationFactsV1,
) -> tuple[ReleaseQualificationCheckV1, ...]:
    """Reconstruct the exact ordered 42-row qualification proof."""

    if session.target_id != target_id or facts.platform_id != target_id:
        raise ValueError("qualification reconstruction target identities differ")
    expected_rows = _expected_step_rows(target_id)
    observed_rows = tuple(
        (item.artifact_selector, item.step_id, item.root_role) for item in steps
    )
    if observed_rows != expected_rows:
        raise ValueError("qualification reconstruction step inventory differs")
    command_by_id = {item.command_id: item for item in commands}
    if len(command_by_id) != len(commands):
        raise ValueError("qualification reconstruction command IDs are not unique")
    result: list[ReleaseQualificationCheckV1] = []
    clean_proof = {
        "network_scope": session.network_scope.value,
        "provider_attestation_sha256": session.provider_attestation_sha256,
        "provider_id": session.provider_id,
        "provider_instance_id": session.provider_instance_id,
        "roots": [item.as_dict() for item in session.roots],
        "target_id": target_id,
    }
    result.append(
        ReleaseQualificationCheckV1(
            "CLEAN_PROVIDER",
            _check_digest("CLEAN_PROVIDER", clean_proof),
            "PASS",
        )
    )
    installed_proof = {
        "artifact_bindings": [
            item.as_dict() for item in session.artifact_bindings
        ],
        "installation_source": session.installation_source,
        "source_checkout_present": session.source_checkout_present,
        "target_id": target_id,
    }
    result.append(
        ReleaseQualificationCheckV1(
            "INSTALLED_ARTIFACT_ONLY",
            _check_digest("INSTALLED_ARTIFACT_ONLY", installed_proof),
            "PASS",
        )
    )
    for step in steps:
        prefix = "DESKTOP" if step.artifact_selector.endswith("/desktop") else "HEADLESS"
        check_id = f"{prefix}:{step.step_id}"
        try:
            referenced = [command_by_id[item] for item in step.command_ids]
        except KeyError as error:
            raise ValueError("qualification step references an unknown command") from error
        proof = {
            "commands": [item.as_dict() for item in referenced],
            "step": step.as_dict(),
        }
        result.append(
            ReleaseQualificationCheckV1(
                check_id,
                _check_digest(check_id, proof),
                step.status,
            )
        )
    same_platform_id = "SAME_PLATFORM_DESKTOP_HEADLESS"
    same_platform = facts.desktop_run_sha256 == facts.headless_run_sha256
    result.append(
        ReleaseQualificationCheckV1(
            same_platform_id,
            _check_digest(
                same_platform_id,
                {
                    "desktop_run_sha256": facts.desktop_run_sha256,
                    "equal": same_platform,
                    "headless_run_sha256": facts.headless_run_sha256,
                    "target_id": target_id,
                },
            ),
            "PASS" if same_platform else "FAIL",
        )
    )
    final_id = required_release_qualification_check_ids(target_id)[-1]
    result.append(
        ReleaseQualificationCheckV1(
            final_id,
            _check_digest(
                final_id,
                {
                    "result_sha256": facts.cross_platform_integer_core_sha256,
                    "root_end_inclusive": 4_000_015,
                    "root_start": 4_000_000,
                    "workload_id": "CROSS_PLATFORM_INTEGER_CORE_V1",
                },
            ),
            "PASS",
        )
    )
    checks = tuple(result)
    if tuple(item.check_id for item in checks) != required_release_qualification_check_ids(
        target_id
    ):
        raise RuntimeError("reconstructed qualification check order differs")
    return checks


def _aggregate_status(checks: tuple[ReleaseQualificationCheckV1, ...]) -> str:
    statuses = tuple(item.status for item in checks)
    if "FAIL" in statuses:
        return "FAIL"
    if "NOT_EXERCISED" in statuses:
        return "NOT_EXERCISED"
    if "WARNING" in statuses:
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _validate_attempt_step_results(
    *,
    target_id: str,
    session: ReleaseQualificationSessionV1,
    steps: tuple[ReleaseQualificationStepObservationV1, ...],
    facts: ReleaseQualificationFactsV1,
) -> None:
    """Recompute embedded results and enforce cross-step semantic bindings."""

    desktop, headless = _target_selectors(target_id)
    expected_indices: dict[tuple[str, str], int] = {}
    for index, step_id in enumerate(RELEASE_FUNCTIONAL_STEP_ORDER_V1, start=1):
        expected_indices[(desktop, step_id)] = index
        expected_indices[(headless, step_id)] = (
            21 if step_id == "UNINSTALL_PRESERVE_USER_DATA" else index
        )
    for index, step_id in enumerate(
        RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
        start=17,
    ):
        expected_indices[(headless, step_id)] = index

    by_key = {(item.artifact_selector, item.step_id): item for item in steps}
    if len(by_key) != len(steps):
        raise ValueError("qualification step selector/ID rows must be unique")
    for step in steps:
        if step.result is None:
            continue
        raw = canonical_json_bytes(step.result.payload)
        if (
            step.result.size != len(raw)
            or step.result.sha256 != hashlib.sha256(raw).hexdigest()
        ):
            raise ValueError("qualification step result payload identity changed")
        _validate_step_result_payload(
            step.artifact_selector,
            step.step_id,
            step.result.payload,
        )
        if step.result.payload["execution_index"] != expected_indices[
            (step.artifact_selector, step.step_id)
        ]:
            raise ValueError("qualification physical execution order differs")

    def payload(selector: str, step_id: str) -> dict[str, object] | None:
        result = by_key[(selector, step_id)].result
        return None if result is None else result.payload

    secondary_root_id = session.roots[1].root_id
    for selector, run_fact in (
        (desktop, facts.desktop_run_sha256),
        (headless, facts.headless_run_sha256),
    ):
        saved = payload(selector, "COMPLETE_SAVE")
        microscope = payload(selector, "OPEN_REPLAY_MICROSCOPE")
        reopened = payload(selector, "REOPEN_VERIFY_SAVED")
        exported = payload(selector, "EXPORT_PACK")
        imported = payload(selector, "IMPORT_SECOND_CLEAN_ROOT")
        replayed = payload(selector, "REPLAY_IMPORTED_LESSON")
        compared = payload(selector, "COMPARE_DECLARED_REPLAY_DIGEST")
        if saved is not None:
            if saved["run_sha256"] != run_fact:
                raise ValueError("qualification saved-run fact binding differs")
            for related in (microscope, reopened):
                if related is not None and related["saved_run_id"] != saved["run_id"]:
                    raise ValueError("qualification saved-run identities differ")
        if exported is not None and imported is not None:
            if imported["pack_id"] != exported["pack_id"]:
                raise ValueError("qualification exported/imported pack identities differ")
            if imported["secondary_root_id"] != secondary_root_id:
                raise ValueError("qualification import used another secondary root")
        for related in (reopened, replayed):
            if related is not None and related["replay_sha256"] != facts.replay_sha256:
                raise ValueError("qualification replay fact binding differs")
        if compared is not None and (
            compared["primary_replay_sha256"] != facts.replay_sha256
            or compared["secondary_replay_sha256"] != facts.replay_sha256
        ):
            raise ValueError("qualification replay comparison fact binding differs")

    headless_simulation = payload(headless, "HEADLESS_SIMULATION")
    if (
        headless_simulation is not None
        and headless_simulation["cross_platform_integer_core_sha256"]
        != facts.cross_platform_integer_core_sha256
    ):
        raise ValueError("qualification integer-core fact binding differs")


@dataclass(frozen=True, slots=True)
class ReleaseQualificationAttemptV1:
    gate_id: str
    target_id: str
    candidate_commit: str
    protocol_set_sha256: str
    source_manifest_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    session: ReleaseQualificationSessionV1
    commands: tuple[ReleaseQualificationCommandObservationV1, ...]
    steps: tuple[ReleaseQualificationStepObservationV1, ...]
    facts: ReleaseQualificationFactsV1
    checks: tuple[ReleaseQualificationCheckV1, ...]
    warnings: tuple[str, ...]
    status: str

    schema_id: ClassVar[str] = RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1
    policy_id: ClassVar[str] = RELEASE_QUALIFICATION_EXECUTION_POLICY_ID_V1

    def __post_init__(self) -> None:
        if self.target_id not in RELEASE_QUALIFICATION_GATE_BY_TARGET_V1:
            raise ValueError("qualification attempt target is invalid")
        if self.gate_id != RELEASE_QUALIFICATION_GATE_BY_TARGET_V1[self.target_id]:
            raise ValueError("qualification attempt gate and target differ")
        _commit(self.candidate_commit, "qualification candidate commit")
        for value, label in (
            (self.protocol_set_sha256, "qualification protocol-set digest"),
            (self.source_manifest_sha256, "qualification source-manifest digest"),
            (self.artifact_index_sha256, "qualification artifact-index digest"),
            (self.build_evidence_sha256, "qualification build-evidence digest"),
        ):
            require_sha256(value, label)
        if type(self.session) is not ReleaseQualificationSessionV1:
            raise TypeError("qualification attempt session must be typed")
        if self.session.target_id != self.target_id:
            raise ValueError("qualification attempt and session targets differ")
        if type(self.commands) is not tuple or any(
            type(item) is not ReleaseQualificationCommandObservationV1
            for item in self.commands
        ):
            raise TypeError("qualification command observations must be typed")
        if not self.commands:
            raise ValueError("qualification attempt requires command observations")
        if tuple(item.sequence for item in self.commands) != tuple(
            range(1, len(self.commands) + 1)
        ):
            raise ValueError("qualification command sequences must be contiguous")
        command_ids = tuple(item.command_id for item in self.commands)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("qualification command IDs must be unique")
        selectors = set(_target_selectors(self.target_id))
        if any(
            item.artifact_selector is not None
            and item.artifact_selector not in selectors
            for item in self.commands
        ):
            raise ValueError("qualification command selector belongs to another target")
        if type(self.steps) is not tuple or any(
            type(item) is not ReleaseQualificationStepObservationV1
            for item in self.steps
        ):
            raise TypeError("qualification step observations must be typed")
        if len(self.steps) != RELEASE_QUALIFICATION_STEP_COUNT_V1:
            raise ValueError("qualification attempt must contain exactly 38 steps")
        observed_rows = tuple(
            (item.artifact_selector, item.step_id, item.root_role)
            for item in self.steps
        )
        if observed_rows != _expected_step_rows(self.target_id):
            raise ValueError("qualification step order differs from the frozen matrix")
        referenced_ids = {
            command_id for step in self.steps for command_id in step.command_ids
        }
        if referenced_ids != set(command_ids):
            raise ValueError("qualification commands and step references differ")
        by_id = {item.command_id: item for item in self.commands}
        for step in self.steps:
            if step.status in {"PASS", "WARNING"} and any(
                by_id[command_id].timed_out
                or by_id[command_id].returncode != 0
                for command_id in step.command_ids
            ):
                raise ValueError("passing qualification step references a failed command")
        if type(self.facts) is not ReleaseQualificationFactsV1:
            raise TypeError("qualification facts must be typed")
        if self.facts.platform_id != self.target_id:
            raise ValueError("qualification facts name another platform")
        _validate_attempt_step_results(
            target_id=self.target_id,
            session=self.session,
            steps=self.steps,
            facts=self.facts,
        )
        if type(self.checks) is not tuple or any(
            type(item) is not ReleaseQualificationCheckV1 for item in self.checks
        ):
            raise TypeError("qualification checks must be typed")
        if len(self.checks) != RELEASE_QUALIFICATION_CHECK_COUNT_V1:
            raise ValueError("qualification attempt must contain exactly 42 checks")
        expected_checks = reconstruct_release_qualification_checks(
            target_id=self.target_id,
            session=self.session,
            commands=self.commands,
            steps=self.steps,
            facts=self.facts,
        )
        if tuple(item.as_dict() for item in self.checks) != tuple(
            item.as_dict() for item in expected_checks
        ):
            raise ValueError("qualification checks differ from reconstructed proof")
        if self.status not in _QUALIFICATION_STATUSES_V1:
            raise ValueError("qualification attempt status is invalid")
        if self.status != _aggregate_status(self.checks):
            raise ValueError("qualification attempt aggregate status differs")
        if type(self.warnings) is not tuple or any(
            type(item) is not str for item in self.warnings
        ):
            raise TypeError("qualification warnings must be a text tuple")
        for item in self.warnings:
            _token(item, "qualification warning code")
        expected_warnings = tuple(
            sorted(
                {
                    warning
                    for step in self.steps
                    for warning in step.warning_codes
                },
                key=lambda item: item.encode("utf-8"),
            )
        )
        if self.warnings != expected_warnings:
            raise ValueError("qualification aggregate warning inventory differs")
        if self.status in {"PASS", "PASS_WITH_WARNINGS"}:
            if self.facts.clean_environment is not True:
                raise ValueError("passing qualification must use a clean environment")
            if self.facts.desktop_run_sha256 != self.facts.headless_run_sha256:
                raise ValueError("passing desktop/headless run identities differ")
            _require_passing_network_scope(
                target_id=self.target_id,
                status=self.status,
                network_scope=self.session.network_scope,
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_index_sha256": self.artifact_index_sha256,
            "build_evidence_sha256": self.build_evidence_sha256,
            "candidate_commit": self.candidate_commit,
            "checks": [item.as_dict() for item in self.checks],
            "commands": [item.as_dict() for item in self.commands],
            "facts": self.facts.as_dict(),
            "gate_id": self.gate_id,
            "policy_id": self.policy_id,
            "protocol_set_sha256": self.protocol_set_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session": self.session.as_dict(),
            "source_manifest_sha256": self.source_manifest_sha256,
            "status": self.status,
            "steps": [item.as_dict() for item in self.steps],
            "target_id": self.target_id,
            "warnings": list(self.warnings),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseQualificationAttemptV1":
        value = load_canonical_json_bytes(raw, "release qualification attempt")
        row = _exact(
            value,
            {
                "artifact_index_sha256",
                "build_evidence_sha256",
                "candidate_commit",
                "checks",
                "commands",
                "facts",
                "gate_id",
                "policy_id",
                "protocol_set_sha256",
                "schema_id",
                "schema_version",
                "session",
                "source_manifest_sha256",
                "status",
                "steps",
                "target_id",
                "warnings",
            },
            "release qualification attempt",
        )
        if (
            row["schema_id"] != cls.schema_id
            or row["schema_version"] != cls.schema_version
            or row["policy_id"] != cls.policy_id
        ):
            raise ValueError("release qualification attempt identity differs")
        instance = cls(
            gate_id=_text(row["gate_id"], "qualification gate ID", 128),
            target_id=_text(row["target_id"], "qualification target ID", 128),
            candidate_commit=_commit(
                row["candidate_commit"], "qualification candidate commit"
            ),
            protocol_set_sha256=_sha256(
                row["protocol_set_sha256"], "qualification protocol-set digest"
            ),
            source_manifest_sha256=_sha256(
                row["source_manifest_sha256"], "qualification source-manifest digest"
            ),
            artifact_index_sha256=_sha256(
                row["artifact_index_sha256"], "qualification artifact-index digest"
            ),
            build_evidence_sha256=_sha256(
                row["build_evidence_sha256"], "qualification build-evidence digest"
            ),
            session=ReleaseQualificationSessionV1.from_dict(row["session"]),
            commands=tuple(
                ReleaseQualificationCommandObservationV1.from_dict(item)
                for item in _array(row["commands"], "qualification commands")
            ),
            steps=tuple(
                ReleaseQualificationStepObservationV1.from_dict(item)
                for item in _array(row["steps"], "qualification steps")
            ),
            facts=ReleaseQualificationFactsV1.from_dict(row["facts"]),
            checks=tuple(
                ReleaseQualificationCheckV1.from_dict(item)
                for item in _array(row["checks"], "qualification checks")
            ),
            warnings=tuple(
                _token(item, "qualification warning code")
                for item in _array(row["warnings"], "qualification warnings")
            ),
            status=_text(row["status"], "qualification attempt status", 64),
        )
        if instance.canonical_bytes() != raw:
            raise ValueError("release qualification attempt bytes are not canonical")
        return instance


def build_release_qualification_attempt_record(
    *,
    gate_id: str,
    target_id: str,
    candidate_commit: str,
    protocol_set_sha256: str,
    source_manifest_sha256: str,
    artifact_index_sha256: str,
    build_evidence_sha256: str,
    session: ReleaseQualificationSessionV1,
    commands: tuple[ReleaseQualificationCommandObservationV1, ...],
    steps: tuple[ReleaseQualificationStepObservationV1, ...],
    facts: ReleaseQualificationFactsV1,
) -> ReleaseQualificationAttemptV1:
    """Assemble one attempt with reconstructed checks, warnings, and status."""

    checks = reconstruct_release_qualification_checks(
        target_id=target_id,
        session=session,
        commands=commands,
        steps=steps,
        facts=facts,
    )
    warnings = tuple(
        sorted(
            {warning for step in steps for warning in step.warning_codes},
            key=lambda item: item.encode("utf-8"),
        )
    )
    return ReleaseQualificationAttemptV1(
        gate_id=gate_id,
        target_id=target_id,
        candidate_commit=candidate_commit,
        protocol_set_sha256=protocol_set_sha256,
        source_manifest_sha256=source_manifest_sha256,
        artifact_index_sha256=artifact_index_sha256,
        build_evidence_sha256=build_evidence_sha256,
        session=session,
        commands=commands,
        steps=steps,
        facts=facts,
        checks=checks,
        warnings=warnings,
        status=_aggregate_status(checks),
    )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationRecordVerificationV1:
    target_id: str
    gate_id: str
    status: str
    provider_attestation_sha256: str
    qualification_attempt_sha256: str
    session_id: str
    check_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "check_count": self.check_count,
            "gate_id": self.gate_id,
            "provider_attestation_sha256": self.provider_attestation_sha256,
            "qualification_attempt_sha256": self.qualification_attempt_sha256,
            "session_id": self.session_id,
            "status": self.status,
            "target_id": self.target_id,
        }


def verify_release_qualification_record(
    provider: ReleaseCleanProviderAttestationV1,
    attempt: ReleaseQualificationAttemptV1,
    protocol: ReleaseQualificationProtocolV1,
) -> ReleaseQualificationRecordVerificationV1:
    """Purely verify two typed records against the frozen protocol."""

    if type(provider) is not ReleaseCleanProviderAttestationV1:
        raise TypeError("qualification verification requires a typed provider record")
    if type(attempt) is not ReleaseQualificationAttemptV1:
        raise TypeError("qualification verification requires a typed attempt record")
    if type(protocol) is not ReleaseQualificationProtocolV1:
        raise TypeError("qualification verification requires the exact V1 protocol")
    if provider.target_id != attempt.target_id:
        raise ValueError("provider and qualification attempt targets differ")
    if attempt.session.provider_id != provider.provider_id:
        raise ValueError("qualification session names another provider")
    if attempt.session.provider_attestation_sha256 != provider.sha256:
        raise ValueError("qualification session provider-attestation digest differs")
    if attempt.session.network_scope is not provider.network_scope:
        raise ValueError("provider and session network scopes differ")
    _require_passing_network_scope(
        target_id=attempt.target_id,
        status=attempt.status,
        network_scope=attempt.session.network_scope,
    )
    protocol_rows = tuple(
        (
            step.step_id,
            step.root_role,
        )
        for step in protocol.functional_steps
    )
    expected_functional = tuple(
        zip(
            RELEASE_FUNCTIONAL_STEP_ORDER_V1,
            RELEASE_QUALIFICATION_FUNCTIONAL_ROOT_ROLES_V1,
            strict=True,
        )
    )
    if protocol_rows != expected_functional:
        raise ValueError("qualification record differs from protocol functional rows")
    protocol_extra = tuple(
        (step.step_id, step.root_role) for step in protocol.headless_extra_steps
    )
    expected_extra = tuple(
        zip(
            RELEASE_HEADLESS_EXTRA_STEP_ORDER_V1,
            RELEASE_QUALIFICATION_HEADLESS_ROOT_ROLES_V1,
            strict=True,
        )
    )
    if protocol_extra != expected_extra:
        raise ValueError("qualification record differs from protocol headless rows")
    _validate_attempt_step_results(
        target_id=attempt.target_id,
        session=attempt.session,
        steps=attempt.steps,
        facts=attempt.facts,
    )
    expected_checks = reconstruct_release_qualification_checks(
        target_id=attempt.target_id,
        session=attempt.session,
        commands=attempt.commands,
        steps=attempt.steps,
        facts=attempt.facts,
    )
    if tuple(item.as_dict() for item in attempt.checks) != tuple(
        item.as_dict() for item in expected_checks
    ):
        raise ValueError("qualification attempt checks failed reconstruction")
    return ReleaseQualificationRecordVerificationV1(
        target_id=attempt.target_id,
        gate_id=attempt.gate_id,
        status=attempt.status,
        provider_attestation_sha256=provider.sha256,
        qualification_attempt_sha256=attempt.sha256,
        session_id=attempt.session.session_id,
        check_count=len(attempt.checks),
    )


def verify_release_qualification_record_bytes(
    provider_raw: bytes,
    attempt_raw: bytes,
    protocol: ReleaseQualificationProtocolV1,
) -> ReleaseQualificationRecordVerificationV1:
    """Parse canonical bytes and perform the pure two-record verification."""

    return verify_release_qualification_record(
        ReleaseCleanProviderAttestationV1.from_bytes(provider_raw),
        ReleaseQualificationAttemptV1.from_bytes(attempt_raw),
        protocol,
    )


__all__ = [
    "RELEASE_CLEAN_PROVIDER_ATTESTATION_SCHEMA_ID_V1",
    "RELEASE_QUALIFICATION_ATTEMPT_NUMBER_V1",
    "RELEASE_QUALIFICATION_ATTEMPT_RECORD_PATH_BY_TARGET_V1",
    "RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1",
    "RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1",
    "RELEASE_QUALIFICATION_CHECK_COUNT_V1",
    "RELEASE_QUALIFICATION_CHECK_PROOF_SCHEMA_ID_V1",
    "RELEASE_QUALIFICATION_EXECUTION_POLICY_ID_V1",
    "RELEASE_QUALIFICATION_GATE_BY_TARGET_V1",
    "RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1",
    "RELEASE_QUALIFICATION_PROVIDER_RECORD_PATH_BY_TARGET_V1",
    "RELEASE_QUALIFICATION_RECORD_SCHEMA_VERSION_V1",
    "RELEASE_QUALIFICATION_ROOT_ROLE_ORDER_V1",
    "RELEASE_QUALIFICATION_STEP_COUNT_V1",
    "ReleaseCleanProviderAttestationV1",
    "ReleaseQualificationArtifactBindingV1",
    "ReleaseQualificationAttemptV1",
    "ReleaseQualificationCheckV1",
    "ReleaseQualificationCommandObservationV1",
    "ReleaseQualificationFactsV1",
    "ReleaseQualificationNetworkScopeV1",
    "ReleaseQualificationRecordVerificationV1",
    "ReleaseQualificationRootObservationV1",
    "ReleaseQualificationSessionV1",
    "ReleaseQualificationStepObservationV1",
    "ReleaseQualificationStepResultV1",
    "build_release_qualification_attempt_record",
    "reconstruct_release_qualification_checks",
    "release_qualification_record_paths",
    "required_release_qualification_check_ids",
    "verify_release_qualification_record",
    "verify_release_qualification_record_bytes",
]
