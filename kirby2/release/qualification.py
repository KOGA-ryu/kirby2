"""Frozen release platform, functional, evidence, and closeout protocols.

WO40-D owns dispatch/refusal semantics only.  It can prove that a future command
addresses the exact preregistered target and step matrix, but it cannot manufacture
qualification evidence or silently rerun a completed attempt.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
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

_RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1 = "KIRBY2_RELEASE_GATE_EVIDENCE_V1"
_RELEASE_EVIDENCE_MARKER_START_V1 = "<!-- KIRBY2_RELEASE_GATE_EVIDENCE_V1\n"
_RELEASE_EVIDENCE_MARKER_END_V1 = "\nKIRBY2_RELEASE_GATE_EVIDENCE_V1 -->"
_WO40F_CHECK_IDS_V1 = (
    "CANDIDATE_SOURCE_LOCK",
    "HEADLESS_ARTIFACTS",
    "DESKTOP_ARTIFACTS",
    "REPEAT_BUILD_REPRODUCIBILITY",
    "MANIFEST_LICENSE_PACK_ASSET_INVENTORY",
    "OFFLINE_INSTALLABILITY",
    "NO_DEVELOPER_DATA",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BUILD_EVIDENCE_BYTES_V1 = 4 * 1024 * 1024

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


@dataclass(frozen=True, slots=True)
class ReleaseBuildEvidenceBindingV1:
    """Strict WO40-F identity projection consumed before qualification begins."""

    candidate_commit: str
    protocol_set_sha256: str
    source_manifest_sha256: str
    artifact_index_sha256: str
    build_evidence_sha256: str
    artifact_index_record_sha256: str
    artifact_index_record_size: int
    build_record_sha256: str
    build_record_size: int
    check_rows: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("WO40-F evidence candidate commit is invalid")
        for value, label in (
            (self.protocol_set_sha256, "WO40-F protocol-set digest"),
            (self.source_manifest_sha256, "WO40-F source-manifest digest"),
            (self.artifact_index_sha256, "WO40-F artifact-index digest"),
            (self.build_evidence_sha256, "WO40-F document digest"),
            (self.artifact_index_record_sha256, "WO40-F index-record digest"),
            (self.build_record_sha256, "WO40-F build-record digest"),
        ):
            require_sha256(value, label)
        if self.artifact_index_sha256 != self.artifact_index_record_sha256:
            raise ValueError("WO40-F index evidence identities differ")
        for value, label in (
            (self.artifact_index_record_size, "WO40-F index-record size"),
            (self.build_record_size, "WO40-F build-record size"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be positive")
        if type(self.check_rows) is not tuple or any(
                type(item) is not tuple
                or len(item) != 3
                or type(item[0]) is not str
                or type(item[1]) is not str
                or _SHA256.fullmatch(item[1]) is None
                or item[2] != "PASS"
                for item in self.check_rows
            ):
            raise ValueError("WO40-F check proof rows differ")
        if tuple(item[0] for item in self.check_rows) != _WO40F_CHECK_IDS_V1:
            raise ValueError("WO40-F check proof rows differ")

    @classmethod
    def from_markdown_bytes(cls, raw: bytes) -> "ReleaseBuildEvidenceBindingV1":
        if type(raw) is not bytes or not raw or len(raw) > _MAX_BUILD_EVIDENCE_BYTES_V1:
            raise ValueError("WO40-F build evidence size is outside the V1 bound")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("WO40-F build evidence is not UTF-8") from error
        start = text.find(_RELEASE_EVIDENCE_MARKER_START_V1)
        end = text.find(
            _RELEASE_EVIDENCE_MARKER_END_V1,
            start + len(_RELEASE_EVIDENCE_MARKER_START_V1),
        )
        if (
            start < 0
            or end < 0
            or text.find(_RELEASE_EVIDENCE_MARKER_START_V1, start + 1) >= 0
            or text.find(_RELEASE_EVIDENCE_MARKER_END_V1, end + 1) >= 0
        ):
            raise ValueError("WO40-F build evidence canonical marker differs")
        try:
            payload_raw = text[
                start + len(_RELEASE_EVIDENCE_MARKER_START_V1) : end
            ].encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("WO40-F canonical payload is not ASCII JSON") from error
        from kirby2.packs.formats import load_canonical_json_bytes

        payload = load_canonical_json_bytes(payload_raw, "WO40-F build evidence")
        fields = {
            "artifact_index_sha256",
            "candidate_commit",
            "checks",
            "evidence_records",
            "facts",
            "gate_id",
            "protocol_set_sha256",
            "schema_id",
            "schema_version",
            "source_manifest_sha256",
            "status",
        }
        row = _exact(payload, fields, "WO40-F build evidence")
        if (
            row["schema_id"] != _RELEASE_GATE_EVIDENCE_SCHEMA_ID_V1
            or type(row["schema_version"]) is not int
            or row["schema_version"] != 1
            or row["gate_id"] != "WO40-F"
            or row["status"] != "PASS"
            or row["facts"] != {"artifact_count": 6, "build_repetitions": 2}
        ):
            raise ValueError("WO40-F build evidence identity or terminal facts differ")
        checks = _array(row["checks"], "WO40-F checks")
        observed_check_ids: list[str] = []
        observed_check_rows: list[tuple[str, str, str]] = []
        for item in checks:
            check = _exact(
                item,
                {"check_id", "evidence_sha256", "status"},
                "WO40-F check",
            )
            if (
                type(check["check_id"]) is not str
                or type(check["evidence_sha256"]) is not str
                or _SHA256.fullmatch(check["evidence_sha256"]) is None
                or check["status"] != "PASS"
            ):
                raise ValueError("WO40-F check identity or status differs")
            observed_check_ids.append(check["check_id"])
            observed_check_rows.append(
                (check["check_id"], check["evidence_sha256"], check["status"])
            )
        if tuple(observed_check_ids) != _WO40F_CHECK_IDS_V1:
            raise ValueError("WO40-F check inventory or order differs")
        records = _array(row["evidence_records"], "WO40-F evidence records")
        by_id: dict[str, dict[str, object]] = {}
        for item in records:
            record = _exact(
                item,
                {"evidence_id", "path", "sha256", "size"},
                "WO40-F evidence record",
            )
            evidence_id = record["evidence_id"]
            if type(evidence_id) is not str or evidence_id in by_id:
                raise ValueError("WO40-F evidence record IDs differ")
            by_id[evidence_id] = record
        expected_paths = {
            "artifact-index": ".kirby2/release/release-artifact-index.json",
            "release-build-record": ".kirby2/release/release-build-record.json",
        }
        if tuple(by_id) != tuple(sorted(expected_paths)) or any(
            by_id[key]["path"] != path for key, path in expected_paths.items()
        ):
            raise ValueError("WO40-F evidence record inventory differs")
        for record in by_id.values():
            if (
                type(record["sha256"]) is not str
                or _SHA256.fullmatch(record["sha256"]) is None
                or type(record["size"]) is not int
                or record["size"] <= 0
            ):
                raise ValueError("WO40-F evidence record identity is invalid")
        scalar_names = (
            "candidate_commit",
            "protocol_set_sha256",
            "source_manifest_sha256",
            "artifact_index_sha256",
        )
        if any(type(row[name]) is not str for name in scalar_names):
            raise TypeError("WO40-F evidence identity fields must be text")
        return cls(
            candidate_commit=row["candidate_commit"],
            protocol_set_sha256=row["protocol_set_sha256"],
            source_manifest_sha256=row["source_manifest_sha256"],
            artifact_index_sha256=row["artifact_index_sha256"],
            build_evidence_sha256=hashlib.sha256(raw).hexdigest(),
            artifact_index_record_sha256=by_id["artifact-index"]["sha256"],
            artifact_index_record_size=by_id["artifact-index"]["size"],
            build_record_sha256=by_id["release-build-record"]["sha256"],
            build_record_size=by_id["release-build-record"]["size"],
            check_rows=tuple(observed_check_rows),
        )


def load_release_build_evidence_binding(
    path: Path,
) -> ReleaseBuildEvidenceBindingV1:
    """Read one tracked WO40-F document without symlink following or drift."""

    if not isinstance(path, Path) or not path.is_absolute() or path.resolve(False) != path:
        raise ValueError("WO40-F evidence path must be an absolute resolved Path")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks O_NOFOLLOW evidence-read support")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size <= 0
            or before.st_size > _MAX_BUILD_EVIDENCE_BYTES_V1
        ):
            raise ValueError("WO40-F evidence file is linked, special, empty, or oversized")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            item.st_uid,
            item.st_gid,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if len(raw) != before.st_size or identity(before) != identity(after):
            raise ValueError("WO40-F evidence changed during read")
    finally:
        os.close(descriptor)
    return ReleaseBuildEvidenceBindingV1.from_markdown_bytes(raw)


@dataclass(frozen=True, slots=True)
class ReleaseQualificationVerificationV1:
    target_id: str
    gate_id: str
    status: str
    candidate_commit: str
    provider_attestation_sha256: str
    qualification_attempt_sha256: str
    artifact_index_sha256: str
    build_record_sha256: str
    build_evidence_sha256: str
    session_id: str
    check_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_index_sha256": self.artifact_index_sha256,
            "build_evidence_sha256": self.build_evidence_sha256,
            "build_record_sha256": self.build_record_sha256,
            "candidate_commit": self.candidate_commit,
            "check_count": self.check_count,
            "gate_id": self.gate_id,
            "provider_attestation_sha256": self.provider_attestation_sha256,
            "qualification_attempt_sha256": self.qualification_attempt_sha256,
            "session_id": self.session_id,
            "status": self.status,
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class _MacosIntegerCoreBaselineSnapshotV1:
    provider_name: str
    provider_raw: bytes
    provider_identity: tuple[int, ...]
    attempt_name: str
    attempt_raw: bytes
    attempt_identity: tuple[int, ...]
    attempt_directory_fd: int
    attempt_directory_identity: tuple[int, ...]
    artifact_ids: tuple[str, ...]


def _stable_release_record_bytes(
    path: Path | str,
    *,
    maximum_bytes: int,
    require_read_only: bool,
    directory_fd: int | None = None,
    identity_out: list[tuple[int, ...]] | None = None,
) -> bytes:
    if (
        not isinstance(path, (Path, str))
        or type(maximum_bytes) is not int
        or maximum_bytes <= 0
    ):
        raise ValueError("release record path or bound is invalid")
    if directory_fd is None:
        selected = Path(path)
        if not selected.is_absolute() or selected.resolve(strict=False) != selected:
            raise ValueError("release record path must be absolute and resolved")
    else:
        selected_text = os.fspath(path)
        selected_path = Path(selected_text)
        if (
            selected_path.is_absolute()
            or not selected_path.parts
            or ".." in selected_path.parts
        ):
            raise ValueError("release record relative path is not confined")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks O_NOFOLLOW release-record support")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or (require_read_only and stat.S_IMODE(before.st_mode) != 0o444)
        ):
            raise ValueError("release record is linked, special, writable, empty, or oversized")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            item.st_uid,
            item.st_gid,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        extra = os.read(descriptor, 1)
        final = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or extra
            or identity(before) != identity(after)
            or identity(before) != identity(final)
        ):
            raise ValueError("release record grew during stable read")
        if identity_out is not None:
            identity_out.append(identity(before))
        return raw
    finally:
        os.close(descriptor)


def _open_owned_release_directory(
    path: Path | str,
    *,
    parent_fd: int | None = None,
) -> tuple[int, tuple[int, ...]]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks O_NOFOLLOW release-directory support")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=parent_fd,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise ValueError("release directory ownership or permissions are unsafe")
    identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    return descriptor, identity


def _require_release_directory_identity(
    descriptor: int,
    expected: tuple[int, ...],
) -> None:
    metadata = os.fstat(descriptor)
    observed = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    if observed != expected:
        raise ValueError("release directory changed during qualification verification")


def _require_canonical_tracked_build_evidence(
    repository: Path,
    supplied: Path,
) -> None:
    canonical = (repository / "KIRBY2_RELEASE_BUILD_EVIDENCE.md").resolve(
        strict=True
    )
    if supplied != canonical:
        raise ValueError("qualification requires the canonical WO40-F evidence path")
    for arguments, detail in (
        (
            (
                "git",
                "ls-files",
                "--error-unmatch",
                "--",
                "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
            ),
            "WO40-F evidence is not tracked",
        ),
        (
            (
                "git",
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--",
                "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
            ),
            "WO40-F evidence has unstaged drift",
        ),
        (
            (
                "git",
                "diff",
                "--cached",
                "--quiet",
                "--no-ext-diff",
                "--",
                "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
            ),
            "WO40-F evidence has uncommitted index drift",
        ),
    ):
        result = subprocess.run(
            arguments,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(detail)


def _require_macos_integer_core_baseline(
    *,
    bundle: object,
    root_fd: int,
    gate_fd: int,
    inventory: object,
    inventory_raw: bytes,
    index: object,
    binding: ReleaseBuildEvidenceBindingV1,
    linux_attempt: object,
) -> _MacosIntegerCoreBaselineSnapshotV1:
    """Load and bind the immutable WO40-G baseline without provider access."""

    from .build import ReleaseCleanProviderInventoryV1, ReleaseProtocolBundleV1
    from .manifest import ReleaseArtifactIndexV1
    from .qualification_records import (
        RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1,
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationAttemptV1,
        release_qualification_record_paths,
        verify_release_qualification_record,
    )

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("macOS baseline verification requires the exact protocol bundle")
    if type(inventory) is not ReleaseCleanProviderInventoryV1:
        raise TypeError("macOS baseline verification requires a typed provider inventory")
    if type(inventory_raw) is not bytes:
        raise TypeError("macOS baseline verification requires exact inventory bytes")
    if type(index) is not ReleaseArtifactIndexV1:
        raise TypeError("macOS baseline verification requires a typed artifact index")
    if type(binding) is not ReleaseBuildEvidenceBindingV1:
        raise TypeError("macOS baseline verification requires a typed build binding")
    if type(linux_attempt) is not ReleaseQualificationAttemptV1:
        raise TypeError("macOS baseline verification requires a typed Linux attempt")
    if linux_attempt.target_id != "linux-x86_64":
        raise ValueError("macOS baseline comparison requires a Linux attempt")

    provider_relative, attempt_relative = release_qualification_record_paths(
        "macos-arm64"
    )
    provider_parts = Path(provider_relative).parts
    attempt_parts = Path(attempt_relative).parts
    if (
        len(provider_parts) != 1
        or len(attempt_parts) != 3
        or attempt_parts[0] != "gate-evidence"
    ):
        raise RuntimeError("macOS qualification record path layout differs")
    attempt_directory_fd, attempt_directory_identity = _open_owned_release_directory(
        attempt_parts[1], parent_fd=gate_fd
    )
    try:
        provider_identities: list[tuple[int, ...]] = []
        provider_raw = _stable_release_record_bytes(
            provider_parts[0],
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=True,
            directory_fd=root_fd,
            identity_out=provider_identities,
        )
        attempt_identities: list[tuple[int, ...]] = []
        attempt_raw = _stable_release_record_bytes(
            attempt_parts[2],
            maximum_bytes=64 * 1024 * 1024,
            require_read_only=True,
            directory_fd=attempt_directory_fd,
            identity_out=attempt_identities,
        )
        if len(provider_identities) != 1 or len(attempt_identities) != 1:
            raise RuntimeError("WO40-G baseline record identity capture failed")
        provider = ReleaseCleanProviderAttestationV1.from_bytes(provider_raw)
        baseline_attempt = ReleaseQualificationAttemptV1.from_bytes(attempt_raw)
        pure = verify_release_qualification_record(
            provider,
            baseline_attempt,
            bundle.qualification_protocol,
        )
        if pure.status != "PASS" or baseline_attempt.status != "PASS":
            raise ValueError("WO40-G macOS baseline is not a passing qualification")

        capability = inventory.by_target().get("macos-arm64")
        platform = next(
            (
                item
                for item in bundle.platform_protocol.targets
                if item.target_id == "macos-arm64"
            ),
            None,
        )
        if (
            capability is None
            or platform is None
            or capability.readiness(platform)[0] != "PASS"
        ):
            raise ValueError("WO40-G macOS provider capability is not ready")
        inventory_sha256 = hashlib.sha256(inventory_raw).hexdigest()
        if (
            provider.provider_inventory_sha256 != inventory_sha256
            or provider.provider_capability_sha256 != capability.fingerprint
        ):
            raise ValueError("WO40-G macOS provider differs from its inventory")

        expected_ids = RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1["macos-arm64"]
        if (
            tuple(
                item.artifact_id
                for item in baseline_attempt.session.artifact_bindings
            )
            != expected_ids
        ):
            raise ValueError("WO40-G macOS artifact-binding order differs")
        indexed = {item.artifact_id: item for item in index.artifacts}
        for observed in baseline_attempt.session.artifact_bindings:
            expected = indexed.get(observed.artifact_id)
            if expected is None or (
                observed.size != expected.size
                or observed.release_store_sha256 != expected.transport_sha256
                or observed.provider_copy_sha256 != expected.transport_sha256
            ):
                raise ValueError("WO40-G macOS artifact transfer binding differs")

        baseline_build_identity = (
            baseline_attempt.candidate_commit,
            baseline_attempt.protocol_set_sha256,
            baseline_attempt.source_manifest_sha256,
            baseline_attempt.artifact_index_sha256,
            baseline_attempt.build_evidence_sha256,
        )
        linux_build_identity = (
            linux_attempt.candidate_commit,
            linux_attempt.protocol_set_sha256,
            linux_attempt.source_manifest_sha256,
            linux_attempt.artifact_index_sha256,
            linux_attempt.build_evidence_sha256,
        )
        frozen_build_identity = (
            binding.candidate_commit,
            binding.protocol_set_sha256,
            binding.source_manifest_sha256,
            binding.artifact_index_sha256,
            binding.build_evidence_sha256,
        )
        if (
            baseline_build_identity != linux_build_identity
            or baseline_build_identity != frozen_build_identity
            or baseline_attempt.protocol_set_sha256 != bundle.protocol_set_sha256
            or baseline_attempt.artifact_index_sha256 != index.sha256
        ):
            raise ValueError(
                "Linux qualification and WO40-G baseline build identities differ"
            )
        if (
            linux_attempt.facts.cross_platform_integer_core_sha256
            != baseline_attempt.facts.cross_platform_integer_core_sha256
        ):
            raise ValueError(
                "Linux qualification integer core differs from the WO40-G baseline"
            )
        return _MacosIntegerCoreBaselineSnapshotV1(
            provider_name=provider_parts[0],
            provider_raw=provider_raw,
            provider_identity=provider_identities[0],
            attempt_name=attempt_parts[2],
            attempt_raw=attempt_raw,
            attempt_identity=attempt_identities[0],
            attempt_directory_fd=attempt_directory_fd,
            attempt_directory_identity=attempt_directory_identity,
            artifact_ids=expected_ids,
        )
    except Exception:
        os.close(attempt_directory_fd)
        raise


def _verify_release_qualification_records(
    bundle: object,
    *,
    target_id: str,
    build_evidence: Path,
    artifact_root: Path,
    candidate_provider_raw: bytes | None = None,
    candidate_attempt_raw: bytes | None = None,
) -> ReleaseQualificationVerificationV1:
    """Deeply verify the two qualification records and all immutable inputs.

    This verifier is deliberately provider-free: it never boots, connects to, or
    mutates a machine.  It reparses every canonical record, reconstructs the exact
    42 checks, verifies the selected WO40-F artifacts, and binds the provider
    capability to the original clean-provider inventory.
    """

    from .artifacts import (
        RELEASE_ARTIFACT_MAX_BYTES_V1,
        ReleaseArtifactBuildRecordV1,
    )
    from .build import (
        ReleaseCleanProviderInventoryV1,
        ReleaseCommandStatusV1,
        ReleaseProtocolBundleV1,
        verify_release_artifacts,
    )
    from .manifest import ReleaseArtifactIndexV1
    from .qualification_records import (
        RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1,
        RELEASE_QUALIFICATION_CHECK_COUNT_V1,
        ReleaseCleanProviderAttestationV1,
        ReleaseQualificationAttemptV1,
        release_qualification_record_paths,
        verify_release_qualification_record,
    )

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("qualification verification requires the exact protocol bundle")
    if target_id not in _TARGET_IDS:
        raise ValueError("qualification verification target is invalid")
    candidate_records = (
        candidate_provider_raw is not None or candidate_attempt_raw is not None
    )
    if candidate_records and (
        type(candidate_provider_raw) is not bytes
        or type(candidate_attempt_raw) is not bytes
        or not candidate_provider_raw
        or not candidate_attempt_raw
    ):
        raise TypeError("qualification candidate records require two exact byte strings")
    if (
        not isinstance(artifact_root, Path)
        or not artifact_root.is_absolute()
        or artifact_root.resolve(strict=True) != artifact_root
    ):
        raise ValueError("qualification artifact root must be absolute and resolved")
    if (
        not isinstance(build_evidence, Path)
        or not build_evidence.is_absolute()
        or build_evidence.resolve(strict=True) != build_evidence
    ):
        raise ValueError("WO40-F evidence path must be absolute and resolved")
    repository = bundle.repository_root.resolve(strict=True)
    _require_canonical_tracked_build_evidence(repository, build_evidence)
    binding = load_release_build_evidence_binding(build_evidence)
    if binding.protocol_set_sha256 != bundle.protocol_set_sha256:
        raise ValueError("WO40-F evidence protocol set differs from the live frozen bundle")

    root_fd, root_identity = _open_owned_release_directory(artifact_root)
    gate_fd: int | None = None
    attempt_directory_fd: int | None = None
    macos_baseline: _MacosIntegerCoreBaselineSnapshotV1 | None = None
    try:
        index_raw = _stable_release_record_bytes(
            "release-artifact-index.json",
            maximum_bytes=16 * 1024 * 1024,
            require_read_only=True,
            directory_fd=root_fd,
        )
        build_record_raw = _stable_release_record_bytes(
            "release-build-record.json",
            maximum_bytes=64 * 1024 * 1024,
            require_read_only=True,
            directory_fd=root_fd,
        )
        if (
            len(index_raw) != binding.artifact_index_record_size
            or hashlib.sha256(index_raw).hexdigest()
            != binding.artifact_index_record_sha256
            or len(build_record_raw) != binding.build_record_size
            or hashlib.sha256(build_record_raw).hexdigest()
            != binding.build_record_sha256
        ):
            raise ValueError("WO40-F referenced record bytes differ")
        index = ReleaseArtifactIndexV1.from_bytes(index_raw)
        build_record = ReleaseArtifactBuildRecordV1.from_bytes(build_record_raw)
        build_check_rows = tuple(
            (item.check_id, item.evidence_sha256, item.status)
            for item in build_record.checks
        )
        if (
            index.candidate_commit != binding.candidate_commit
            or index.sha256 != binding.artifact_index_sha256
            or build_record.candidate_commit != binding.candidate_commit
            or build_record.protocol_set_sha256 != binding.protocol_set_sha256
            or build_record.source_manifest_sha256 != binding.source_manifest_sha256
            or build_record.artifact_index_sha256 != binding.artifact_index_sha256
            or build_check_rows != binding.check_rows
        ):
            raise ValueError("WO40-F evidence, artifact index, and build record differ")

        provider_relative, attempt_relative = release_qualification_record_paths(
            target_id
        )
        provider_parts = Path(provider_relative).parts
        attempt_parts = Path(attempt_relative).parts
        if len(provider_parts) != 1 or len(attempt_parts) != 3:
            raise RuntimeError("qualification record path layout differs")
        gate_fd, gate_identity = _open_owned_release_directory(
            attempt_parts[0], parent_fd=root_fd
        )
        attempt_directory_identity: tuple[int, ...] | None = None
        if candidate_records:
            provider_raw = candidate_provider_raw
            attempt_raw = candidate_attempt_raw
        else:
            attempt_directory_fd, attempt_directory_identity = (
                _open_owned_release_directory(attempt_parts[1], parent_fd=gate_fd)
            )
            provider_raw = _stable_release_record_bytes(
                provider_parts[0],
                maximum_bytes=4 * 1024 * 1024,
                require_read_only=True,
                directory_fd=root_fd,
            )
            attempt_raw = _stable_release_record_bytes(
                attempt_parts[2],
                maximum_bytes=64 * 1024 * 1024,
                require_read_only=True,
                directory_fd=attempt_directory_fd,
            )
        inventory_raw = _stable_release_record_bytes(
            "clean-providers.toml",
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=False,
            directory_fd=root_fd,
        )
        provider = ReleaseCleanProviderAttestationV1.from_bytes(provider_raw)
        attempt = ReleaseQualificationAttemptV1.from_bytes(attempt_raw)
        pure = verify_release_qualification_record(
            provider,
            attempt,
            bundle.qualification_protocol,
        )

        inventory = ReleaseCleanProviderInventoryV1.from_bytes(inventory_raw)
        capability = inventory.by_target().get(target_id)
        platform = next(
            item
            for item in bundle.platform_protocol.targets
            if item.target_id == target_id
        )
        if capability is None or capability.readiness(platform)[0] != "PASS":
            raise ValueError("qualification provider inventory capability is not ready")
        if (
            provider.provider_inventory_sha256
            != hashlib.sha256(inventory_raw).hexdigest()
            or provider.provider_capability_sha256 != capability.fingerprint
        ):
            raise ValueError(
                "qualification provider attestation differs from its inventory"
            )

        indexed = {item.artifact_id: item for item in index.artifacts}
        expected_ids = RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[target_id]
        if (
            tuple(
                item.artifact_id for item in attempt.session.artifact_bindings
            )
            != expected_ids
        ):
            raise ValueError("qualification artifact-binding order differs")
        for observed in attempt.session.artifact_bindings:
            expected = indexed[observed.artifact_id]
            if (
                observed.size != expected.size
                or observed.release_store_sha256 != expected.transport_sha256
                or observed.provider_copy_sha256 != expected.transport_sha256
            ):
                raise ValueError("qualification artifact transfer binding differs")
        if (
            attempt.candidate_commit != binding.candidate_commit
            or attempt.protocol_set_sha256 != binding.protocol_set_sha256
            or attempt.source_manifest_sha256 != binding.source_manifest_sha256
            or attempt.artifact_index_sha256 != binding.artifact_index_sha256
            or attempt.build_evidence_sha256 != binding.build_evidence_sha256
            or pure.check_count != RELEASE_QUALIFICATION_CHECK_COUNT_V1
        ):
            raise ValueError(
                "qualification attempt differs from immutable WO40-F inputs"
            )

        if target_id == "linux-x86_64":
            macos_baseline = _require_macos_integer_core_baseline(
                bundle=bundle,
                root_fd=root_fd,
                gate_fd=gate_fd,
                inventory=inventory,
                inventory_raw=inventory_raw,
                index=index,
                binding=binding,
                linux_attempt=attempt,
            )

        artifact_verification = verify_release_artifacts(
            bundle,
            artifact_root,
            candidate_commit=binding.candidate_commit,
        )
        if artifact_verification.status is not ReleaseCommandStatusV1.PASS:
            raise ValueError(
                "deep immutable release-artifact verification did not pass"
            )
        verified_artifact_ids = expected_ids
        if macos_baseline is not None:
            verified_artifact_ids = tuple(
                dict.fromkeys((*expected_ids, *macos_baseline.artifact_ids))
            )
        for artifact_id in verified_artifact_ids:
            artifact_raw = _stable_release_record_bytes(
                artifact_id,
                maximum_bytes=RELEASE_ARTIFACT_MAX_BYTES_V1,
                require_read_only=True,
                directory_fd=root_fd,
            )
            expected = indexed[artifact_id]
            if (
                len(artifact_raw) != expected.size
                or hashlib.sha256(artifact_raw).hexdigest()
                != expected.transport_sha256
            ):
                raise ValueError("qualification selected artifact bytes changed")

        final_small_records: list[tuple[str, int, bytes, int, bool]] = [
            (
                "release-artifact-index.json",
                root_fd,
                index_raw,
                16 * 1024 * 1024,
                True,
            ),
            (
                "release-build-record.json",
                root_fd,
                build_record_raw,
                64 * 1024 * 1024,
                True,
            ),
            (
                "clean-providers.toml",
                root_fd,
                inventory_raw,
                4 * 1024 * 1024,
                False,
            ),
        ]
        if not candidate_records:
            if attempt_directory_fd is None:
                raise RuntimeError("qualification attempt directory was not opened")
            final_small_records.extend(
                (
                    (
                        provider_parts[0],
                        root_fd,
                        provider_raw,
                        4 * 1024 * 1024,
                        True,
                    ),
                    (
                        attempt_parts[2],
                        attempt_directory_fd,
                        attempt_raw,
                        64 * 1024 * 1024,
                        True,
                    ),
                )
            )
        for name, directory, original, bound, read_only in final_small_records:
            if _stable_release_record_bytes(
                name,
                maximum_bytes=bound,
                require_read_only=read_only,
                directory_fd=directory,
            ) != original:
                raise ValueError("qualification input changed before verification ended")
        if load_release_build_evidence_binding(build_evidence) != binding:
            raise ValueError("WO40-F evidence changed before verification ended")
        if macos_baseline is not None:
            provider_identities = []
            provider_raw = _stable_release_record_bytes(
                macos_baseline.provider_name,
                maximum_bytes=4 * 1024 * 1024,
                require_read_only=True,
                directory_fd=root_fd,
                identity_out=provider_identities,
            )
            attempt_identities = []
            attempt_raw = _stable_release_record_bytes(
                macos_baseline.attempt_name,
                maximum_bytes=64 * 1024 * 1024,
                require_read_only=True,
                directory_fd=macos_baseline.attempt_directory_fd,
                identity_out=attempt_identities,
            )
            if (
                provider_raw != macos_baseline.provider_raw
                or provider_identities != [macos_baseline.provider_identity]
                or attempt_raw != macos_baseline.attempt_raw
                or attempt_identities != [macos_baseline.attempt_identity]
            ):
                raise ValueError(
                    "WO40-G baseline file identity changed during Linux verification"
                )
            _require_release_directory_identity(
                macos_baseline.attempt_directory_fd,
                macos_baseline.attempt_directory_identity,
            )
        if attempt_directory_fd is not None:
            if attempt_directory_identity is None:
                raise RuntimeError("qualification attempt directory identity is absent")
            _require_release_directory_identity(
                attempt_directory_fd, attempt_directory_identity
            )
        _require_release_directory_identity(gate_fd, gate_identity)
        _require_release_directory_identity(root_fd, root_identity)
        return ReleaseQualificationVerificationV1(
            target_id=target_id,
            gate_id=pure.gate_id,
            status=pure.status,
            candidate_commit=binding.candidate_commit,
            provider_attestation_sha256=provider.sha256,
            qualification_attempt_sha256=attempt.sha256,
            artifact_index_sha256=index.sha256,
            build_record_sha256=build_record.sha256,
            build_evidence_sha256=binding.build_evidence_sha256,
            session_id=pure.session_id,
            check_count=pure.check_count,
        )
    finally:
        if macos_baseline is not None:
            os.close(macos_baseline.attempt_directory_fd)
        if attempt_directory_fd is not None:
            os.close(attempt_directory_fd)
        if gate_fd is not None:
            os.close(gate_fd)
        os.close(root_fd)


def verify_release_qualification(
    bundle: object,
    *,
    target_id: str,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseQualificationVerificationV1:
    """Deeply verify one published, immutable qualification record pair."""

    return _verify_release_qualification_records(
        bundle,
        target_id=target_id,
        build_evidence=build_evidence,
        artifact_root=artifact_root,
    )


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
    "RELEASE_QUALIFICATION_ATTEMPT_SCHEMA_ID_V1",
    "RELEASE_QUALIFICATION_PROTOCOL_SCHEMA_ID_V1",
    "WO40_J_PREREQUISITES_ID_V1",
    "WO40_J_REQUIRED_PRIOR_GATES_V1",
    "ReleaseEvidenceReferenceV1",
    "ReleaseBuildEvidenceBindingV1",
    "ReleaseFunctionalStepV1",
    "ReleasePlatformTargetV1",
    "ReleasePlatformsV1",
    "ReleaseQualificationDispatchV1",
    "ReleaseQualificationProtocolV1",
    "ReleaseQualificationRefusalCodeV1",
    "ReleaseQualificationRefused",
    "ReleaseQualificationStatusV1",
    "ReleaseQualificationVerificationV1",
    "load_release_build_evidence_binding",
    "load_release_platforms",
    "load_release_qualification_protocol",
    "qualification_dispatch",
    "verify_closeout_prerequisites",
    "verify_release_qualification",
]
