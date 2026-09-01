"""Closed installed-artifact executor for the frozen WO40-I qualification.

The coordinator is deliberately narrow.  It accepts the already verified WO40-F
artifact store, installs the exact macOS headless/desktop pair without an index,
executes the five auxiliary workloads, and then admits exactly 10,000 preregistered
rows through four FIFO workers.  Workload code runs only from the installed wheel;
this checkout owns verification, resource enforcement, CAS publication, and the
single final activation.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
import platform
import queue
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from multiprocessing.connection import wait as wait_connections
from pathlib import Path, PurePosixPath
from typing import Final, Mapping

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes

from .artifacts import ReleaseArtifactBuildRecordV1
from .build import (
    ReleaseCommandOutcomeV1,
    ReleaseCommandStatusV1,
    ReleaseProtocolBundleV1,
    _release_performance_source_identities,
    verify_release_artifacts,
)
from .manifest import (
    RELEASE_VERSION_V1,
    ReleaseArtifactIndexV1,
    ReleaseManifestV1,
)
from .performance import (
    RELEASE_ARTIFACT_PASS_BYTES_V1,
    RELEASE_ARTIFACT_WARNING_BYTES_V1,
    RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
    RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
    RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
    RELEASE_PERFORMANCE_WORKER_COUNT_V1,
    RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1,
    RELEASE_TOTAL_WALL_LIMIT_NS_V1,
    ReleaseAuxiliaryPerformanceResultV1,
    ReleaseAuxiliaryPerformanceTemplateV1,
    ReleasePerformanceCellResultV1,
    ReleasePerformanceCapabilityRecordV1,
    ReleasePerformanceCheckRecordV1,
    ReleasePerformanceOperationalV1,
    RunnerSourceTreeV1,
    auxiliary_performance_templates,
    bind_performance_row_template,
    iter_performance_row_templates,
    round_div_even,
    validate_performance_attempt_sequence,
)
from .performance_worker import (
    ReleasePerformanceRowAttemptV1,
    replace_performance_row_operational,
    verify_performance_row_attempt,
)
from .performance_auxiliary import (
    ReleaseAuxiliaryExecutionV1,
    ReleaseAuxiliarySourceRunV1,
    verify_auxiliary_performance_execution,
)
from .qualification import (
    _require_canonical_tracked_build_evidence,
    load_release_build_evidence_binding,
)
from .performance_records import (
    RELEASE_PERFORMANCE_ACTIVATION_PATH_V1,
    RELEASE_PERFORMANCE_AGGREGATE_PATH_V1,
    RELEASE_PERFORMANCE_ATTEMPT_PATH_V1,
    ReleasePerformanceActivationRecordV1,
    ReleasePerformanceAggregateV1,
    ReleasePerformanceArtifactInventoryV1,
    ReleasePerformanceAttemptPublicationV1,
    ReleasePerformanceAttemptRecordV1,
    ReleasePerformanceAuxiliaryReferenceV1,
    ReleasePerformanceRecordReferenceV1,
    ReleasePerformanceWorkUnitPublicationV1,
    ReleasePerformanceVerificationInputsV1,
    ReleasePerformanceVerificationV1,
    release_performance_auxiliary_path,
    release_performance_cas_path,
    release_performance_reference,
    release_performance_work_unit_paths,
    verify_release_performance_records,
)


PERFORMANCE_EXECUTION_POLICY_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_EXECUTION_V1"
)
PERFORMANCE_INSTALLED_INPUT_POLICY_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_INSTALLED_INPUT_V1"
)
PERFORMANCE_OFFLINE_ENVIRONMENT_POLICY_V1: Final[str] = (
    "KIRBY2_RELEASE_PERFORMANCE_OFFLINE_ENVIRONMENT_V1"
)

_FORMS: Final[tuple[str, ...]] = ("desktop", "headless")
_BUNDLE_ARTIFACTS: Final[Mapping[str, str]] = {
    "desktop": "macos-arm64-desktop-bundle",
    "headless": "macos-arm64-wheelhouse",
}
_BUNDLE_ROOTS: Final[Mapping[str, str]] = {
    "desktop": f"kirby2-{RELEASE_VERSION_V1}-macos-arm64",
    "headless": f"kirby2-{RELEASE_VERSION_V1}-macos-arm64-wheelhouse",
}
_BUNDLE_FORMS: Final[Mapping[str, str]] = {
    "desktop": "DESKTOP_TAR_GZ",
    "headless": "HEADLESS_WHEELHOUSE_TAR_GZ",
}
_AUXILIARY_FORMS: Final[Mapping[str, str]] = {
    "RELEASE_INTERACTIVE_ACK_V1": "desktop",
    "RELEASE_TERMINAL_UPDATE_V1": "desktop",
    "RELEASE_FULL_DAY_GENERATION_V1": "headless",
    "RELEASE_FULL_DAY_REPLAY_V1": "headless",
    "RELEASE_MICROSCOPE_LOAD_V1": "desktop",
}
_INPUT_PROTOCOL_NAME: Final[str] = "performance_thresholds.toml"
_INPUT_SOURCE_LOCK_NAME: Final[str] = "performance_runner_sources.lock"
_MAX_COMMAND_OUTPUT_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_AUXILIARY_OUTPUT_BYTES: Final[int] = 64 * 1024 * 1024
_MINIMUM_DISK_BYTES: Final[int] = 20 * 1024**3
_MINIMUM_MEMORY_BYTES: Final[int] = 8 * 1024**3
_FROZEN_THRESHOLD_MANIFEST_SHA256_V1: Final[str] = (
    "9dc132220e48813c842d9f8a76381abe72db60f0efbfac2e0f83d7702c12aff4"
)


class PerformanceExecutorRefusalCodeV1(str, Enum):
    INPUT_INVALID = "PERFORMANCE_INPUT_INVALID"
    HOST_UNSUPPORTED = "PERFORMANCE_HOST_UNSUPPORTED"
    RESOURCES_UNAVAILABLE = "PERFORMANCE_RESOURCES_UNAVAILABLE"
    ARTIFACT_VERIFICATION_FAILED = "PERFORMANCE_ARTIFACT_VERIFICATION_FAILED"
    INSTALLATION_FAILED = "PERFORMANCE_INSTALLATION_FAILED"
    PRIOR_ACTIVATION_EXISTS = "PERFORMANCE_PRIOR_ACTIVATION_EXISTS"
    AUXILIARY_FAILED = "PERFORMANCE_AUXILIARY_FAILED"
    WORK_UNIT_FAILED = "PERFORMANCE_WORK_UNIT_FAILED"
    TOTAL_LIMIT = "PERFORMANCE_TOTAL_LIMIT"
    PUBLICATION_CONFLICT = "PERFORMANCE_PUBLICATION_CONFLICT"
    VERIFICATION_FAILED = "PERFORMANCE_VERIFICATION_FAILED"


class _PerformanceRefused(RuntimeError):
    def __init__(self, code: PerformanceExecutorRefusalCodeV1, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class _InstalledFormV1:
    form: str
    python: Path
    kirby2: Path
    bundle_root: Path
    wheelhouse: Path
    artifact_id: str
    artifact_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ProcessObservationV1:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    started_ns: int
    ended_ns: int
    peak_rss_bytes: int
    peak_temporary_bytes: int
    timed_out: bool
    resource_exceeded: bool
    output_exceeded: bool

    @property
    def duration_ns(self) -> int:
        return self.ended_ns - self.started_ns


@dataclass(frozen=True, slots=True)
class _CapturedAttemptV1:
    result: ReleasePerformanceCellResultV1
    semantic_members: tuple[tuple[str, bytes], ...]
    compatibility_sidecars: tuple[tuple[str, bytes], ...]
    operational_sidecars: tuple[tuple[str, bytes], ...]

    def files(self) -> tuple[tuple[str, bytes], ...]:
        return (
            *self.semantic_members,
            *self.compatibility_sidecars,
            *self.operational_sidecars,
            ("cell-result.json", self.result.canonical_bytes()),
        )


@dataclass(frozen=True, slots=True)
class _CompletedRowV1:
    ordinal: int
    work_unit_id: str
    attempts: tuple[ReleasePerformanceCellResultV1, ...]
    inventories: tuple[ReleasePerformanceArtifactInventoryV1, ...]
    publication: ReleasePerformanceWorkUnitPublicationV1


@dataclass(frozen=True, slots=True)
class _PreparedPerformanceV1:
    artifact_root: Path
    build_evidence: Path
    build_evidence_sha256: str
    candidate_commit: str
    artifact_index: ReleaseArtifactIndexV1
    artifact_index_sha256: str
    build_record: ReleaseArtifactBuildRecordV1
    source_tree: RunnerSourceTreeV1
    runner_source_lock_sha256: str
    threshold_manifest_sha256: str
    auxiliary_templates: tuple[ReleaseAuxiliaryPerformanceTemplateV1, ...]
    environment: dict[str, object]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_performance_inputs(
    bundle: ReleaseProtocolBundleV1,
    *,
    manifest: Path,
    complete_run_work_units: int,
    build_evidence: Path,
    artifact_root: Path,
) -> _PreparedPerformanceV1:
    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("performance execution requires the exact protocol bundle")
    if complete_run_work_units != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "performance qualification requires exactly 10,000 complete work units",
        )
    repository = bundle.repository_root.resolve(strict=True)
    expected_manifest = (repository / "release/performance_thresholds.toml").resolve(
        strict=True
    )
    supplied_manifest = manifest.resolve(strict=True)
    if supplied_manifest != expected_manifest:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "performance qualification requires the canonical frozen threshold file",
        )
    threshold_manifest_sha256 = _sha256_file(supplied_manifest)
    if threshold_manifest_sha256 != _FROZEN_THRESHOLD_MANIFEST_SHA256_V1:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "frozen performance threshold bytes changed",
        )
    if (
        bundle.performance_protocol.row_count
        != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
        or bundle.performance_protocol.worker_count
        != RELEASE_PERFORMANCE_WORKER_COUNT_V1
        or bundle.performance_protocol.queue_size
        != RELEASE_PERFORMANCE_QUEUE_SIZE_V1
        or bundle.performance_protocol.designated_target != "macos-arm64"
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "loaded performance protocol differs from the frozen coordinator contract",
        )
    selected_build_evidence = build_evidence.resolve(strict=True)
    selected_artifact_root = artifact_root.resolve(strict=True)
    _require_canonical_tracked_build_evidence(repository, selected_build_evidence)
    binding = load_release_build_evidence_binding(selected_build_evidence)
    verification = verify_release_artifacts(
        bundle,
        selected_artifact_root,
        candidate_commit=binding.candidate_commit,
    )
    if verification.status is not ReleaseCommandStatusV1.PASS:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
            "the immutable WO40-F artifact set did not deeply verify",
        )
    index_path = selected_artifact_root / "release-artifact-index.json"
    record_path = selected_artifact_root / "release-build-record.json"
    artifact_index = ReleaseArtifactIndexV1.from_bytes(index_path.read_bytes())
    build_record = ReleaseArtifactBuildRecordV1.from_bytes(record_path.read_bytes())
    artifact_index_sha256 = _sha256_file(index_path)
    build_evidence_sha256 = _sha256_file(selected_build_evidence)
    if (
        artifact_index.candidate_commit != binding.candidate_commit
        or build_record.candidate_commit != binding.candidate_commit
        or build_record.protocol_set_sha256 != bundle.protocol_set_sha256
        or artifact_index_sha256 != binding.artifact_index_sha256
        or build_record.sha256 != binding.build_record_sha256
        or build_evidence_sha256 != binding.build_evidence_sha256
        or build_record.source_manifest_sha256 != binding.source_manifest_sha256
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
            "WO40-F evidence, index, build record, protocol, or candidate differs",
        )
    source_lock_path = repository / "release/performance_runner_sources.lock"
    source_lock_raw = source_lock_path.read_bytes()
    source_tree = RunnerSourceTreeV1.from_bytes(source_lock_raw)
    if source_tree.source_manifest_sha256 != binding.source_manifest_sha256:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "runner-source lock differs from immutable WO40-F evidence",
        )
    (
        qualification_evidence_sha256,
        source_artifact_manifest_sha256,
        profile_manifest_sha256,
        selected_plan_sha256,
    ) = _release_performance_source_identities(repository)
    templates = auxiliary_performance_templates(
        starter_layout=bundle.artifact_layout.starter_set,
        qualification_evidence_sha256=qualification_evidence_sha256,
        source_artifact_manifest_sha256=source_artifact_manifest_sha256,
        profile_manifest_sha256=profile_manifest_sha256,
        selected_plan_sha256=selected_plan_sha256,
    )
    active_root = selected_artifact_root / "gate-evidence/wo40-i"
    if active_root.exists() or active_root.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.PRIOR_ACTIVATION_EXISTS,
            "a WO40-I publication already exists; performance qualification is one-time",
        )
    return _PreparedPerformanceV1(
        artifact_root=selected_artifact_root,
        build_evidence=selected_build_evidence,
        build_evidence_sha256=build_evidence_sha256,
        candidate_commit=binding.candidate_commit,
        artifact_index=artifact_index,
        artifact_index_sha256=artifact_index_sha256,
        build_record=build_record,
        source_tree=source_tree,
        runner_source_lock_sha256=hashlib.sha256(source_lock_raw).hexdigest(),
        threshold_manifest_sha256=threshold_manifest_sha256,
        auxiliary_templates=templates,
        environment=_host_preflight(selected_artifact_root),
    )


def _memory_bytes() -> int:
    # ``hw.memsize`` is the most direct Darwin source, but managed execution
    # environments may deny the sysctl subprocess even when the corresponding
    # POSIX sysconf values are available.  Accept either independently measured
    # value and keep the existing fail-closed zero when neither surface works.
    values: list[int] = []
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            values.append(pages * page_size)
    except (OSError, TypeError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10,
        )
        value = int(result.stdout.decode("ascii").strip())
        if value > 0:
            values.append(value)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        pass
    return max(values, default=0)


def _host_preflight(artifact_root: Path) -> dict[str, object]:
    system = platform.system()
    machine = platform.machine()
    implementation = platform.python_implementation()
    version = platform.python_version()
    cpu_count = os.cpu_count() or 0
    memory_bytes = _memory_bytes()
    disk_bytes = shutil.disk_usage(artifact_root).free
    failures: list[str] = []
    if (system, machine) != ("Darwin", "arm64"):
        failures.append(f"target is {system}/{machine}, expected Darwin/arm64")
    if implementation != "CPython" or sys.version_info[:2] != (3, 14):
        failures.append(f"runtime is {implementation} {version}, expected CPython 3.14")
    if cpu_count < RELEASE_PERFORMANCE_WORKER_COUNT_V1:
        failures.append("fewer than four logical CPUs are available")
    if memory_bytes < _MINIMUM_MEMORY_BYTES:
        failures.append("less than eight GiB of memory is available")
    if disk_bytes < _MINIMUM_DISK_BYTES:
        failures.append("less than twenty GiB of free storage is available")
    if failures:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.HOST_UNSUPPORTED,
            "; ".join(failures),
        )
    return {
        "available_disk_bytes": disk_bytes,
        "available_memory_bytes": memory_bytes,
        "logical_cpu_count": cpu_count,
        "machine": machine,
        "python_implementation": implementation,
        "python_version": version,
        "system": system,
    }


def _safe_extract_bundle(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(source, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
                f"release bundle is empty: {source.name}",
            )
        for member in members:
            selected = PurePosixPath(member.name)
            if (
                selected.is_absolute()
                or not selected.parts
                or any(part in {"", ".", ".."} for part in selected.parts)
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise _PerformanceRefused(
                    PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
                    f"unsafe release bundle member: {member.name}",
                )
        archive.extractall(destination, filter="data")


def _closed_install_environment(root: Path) -> dict[str, str]:
    environment = {
        "HOME": os.fspath(root / "home"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": os.fspath(root / "tmp"),
    }
    Path(environment["HOME"]).mkdir(parents=True, mode=0o700)
    Path(environment["TMPDIR"]).mkdir(parents=True, mode=0o700)
    return environment


def _run_checked(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if (
        result.returncode != 0
        or len(result.stdout) > _MAX_COMMAND_OUTPUT_BYTES
        or len(result.stderr) > _MAX_COMMAND_OUTPUT_BYTES
    ):
        detail = result.stderr[:2048].decode("utf-8", errors="replace")
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"installed command failed ({result.returncode}): {detail}",
        )
    return result


def _install_form(
    *,
    form: str,
    artifact_root: Path,
    scratch: Path,
    bundle: ReleaseProtocolBundleV1,
    build_record: ReleaseArtifactBuildRecordV1,
) -> _InstalledFormV1:
    if form not in _FORMS:
        raise ValueError("performance installed form is invalid")
    artifact_id = _BUNDLE_ARTIFACTS[form]
    transport = artifact_root / artifact_id
    artifact_rows = tuple(
        item
        for item in build_record.attempts[0].artifacts
        if item.artifact_id == artifact_id
    )
    if len(artifact_rows) != 1:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            f"{form} artifact is absent from the immutable build record",
        )
    artifact_row = artifact_rows[0]
    if (
        not transport.is_file()
        or transport.is_symlink()
        or transport.stat(follow_symlinks=False).st_size != artifact_row.size
        or _sha256_file(transport) != artifact_row.transport_sha256
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
            f"{form} transport changed after WO40-F verification",
        )
    unpacked = scratch / form / "unpacked"
    _safe_extract_bundle(transport, unpacked)
    if (
        transport.stat(follow_symlinks=False).st_size != artifact_row.size
        or _sha256_file(transport) != artifact_row.transport_sha256
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
            f"{form} transport changed during extraction",
        )
    bundle_root = unpacked / _BUNDLE_ROOTS[form]
    wheelhouse = bundle_root / "wheelhouse"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"{form} wheelhouse is missing after extraction",
        )
    manifest_path = bundle_root / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"{form} embedded release manifest is missing or unsafe",
        )
    manifest_raw = manifest_path.read_bytes()
    manifest = ReleaseManifestV1.from_bytes(manifest_raw)
    if (
        artifact_row.embedded_manifest_sha256 is None
        or hashlib.sha256(manifest_raw).hexdigest()
        != artifact_row.embedded_manifest_sha256
        or manifest.candidate_commit != build_record.candidate_commit
        or manifest.archive_root != _BUNDLE_ROOTS[form]
        or (
            manifest.target.system,
            manifest.target.machine,
            manifest.target.artifact_form,
        )
        != ("Darwin", "arm64", _BUNDLE_FORMS[form])
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"{form} embedded release manifest differs from WO40-F",
        )
    venv = scratch / form / "venv"
    environment = _closed_install_environment(scratch / form / "environment")
    _run_checked(
        (sys.executable, "-I", "-m", "venv", os.fspath(venv)),
        cwd=scratch,
        environment=environment,
        timeout_seconds=600,
    )
    python = venv / "bin/python"
    dependency = bundle.requirements_lock.for_target("macos-arm64")
    if len(dependency) != 1:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "macOS dependency inventory differs from one locked wheel",
        )
    locked = dependency[0]
    install = _run_checked(
        (
            os.fspath(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--find-links",
            os.fspath(wheelhouse),
            f"kirby2=={RELEASE_VERSION_V1}",
            f"{locked.name}=={locked.version}",
        ),
        cwd=scratch,
        environment=environment,
        timeout_seconds=600,
    )
    if b"Downloading" in install.stdout or b"https://" in install.stdout:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            "offline installation emitted network-fetch output",
        )
    kirby2 = venv / "bin/kirby2"
    if not python.is_file() or not kirby2.is_file() or not os.access(kirby2, os.X_OK):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"{form} installed launchers are incomplete",
        )
    origin = _run_checked(
        (
            os.fspath(python),
            "-I",
            "-c",
            "import importlib.util,json,os,sys;"
            "s=importlib.util.find_spec('kirby2');"
            "print(json.dumps({'origin':None if s is None else os.path.realpath(s.origin),"
            "'prefix':os.path.realpath(sys.prefix)},sort_keys=True,separators=(',',':')))",
        ),
        cwd=scratch,
        environment=environment,
        timeout_seconds=60,
    )
    try:
        origin_payload = json.loads(origin.stdout)
    except json.JSONDecodeError as error:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            "installed origin probe is not JSON",
        ) from error
    prefix = os.fspath(venv.resolve(strict=True))
    package_origin = origin_payload.get("origin") if type(origin_payload) is dict else None
    if (
        origin_payload.get("prefix") != prefix
        or type(package_origin) is not str
        or not package_origin.startswith(prefix + os.sep)
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            f"{form} package did not import from its isolated installation",
        )
    assert artifact_row.embedded_manifest_sha256 is not None
    return _InstalledFormV1(
        form=form,
        # Preserve the venv launcher path. Resolving its interpreter symlink to
        # the base executable bypasses the adjacent pyvenv.cfg and therefore
        # loses the isolated site-packages that were just verified above.
        python=python,
        kirby2=kirby2.resolve(strict=True),
        bundle_root=bundle_root.resolve(strict=True),
        wheelhouse=wheelhouse.resolve(strict=True),
        artifact_id=artifact_id,
        artifact_manifest_sha256=artifact_row.embedded_manifest_sha256,
    )


def _prepare_installed_inputs(
    repository: Path,
    scratch: Path,
    *,
    threshold_manifest_sha256: str,
    runner_source_lock_sha256: str,
) -> Path:
    input_root = scratch / "installed-inputs"
    release_root = input_root / "release"
    release_root.mkdir(parents=True, mode=0o700)
    for source_name, target_name, expected_sha256 in (
        (
            "release/performance_thresholds.toml",
            _INPUT_PROTOCOL_NAME,
            threshold_manifest_sha256,
        ),
        (
            "release/performance_runner_sources.lock",
            _INPUT_SOURCE_LOCK_NAME,
            runner_source_lock_sha256,
        ),
    ):
        source = repository / source_name
        if not source.is_file() or source.is_symlink():
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
                f"installed performance input is missing or unsafe: {source_name}",
            )
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
                f"installed performance input changed during staging: {source_name}",
            )
        target = release_root / target_name
        target.write_bytes(raw)
        os.chmod(target, 0o444)
        if target.read_bytes() != raw:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
                f"installed performance input changed during copy: {source_name}",
            )
    return input_root.resolve(strict=True)


def _verify_installed_inputs(
    input_root: Path,
    *,
    threshold_manifest_sha256: str,
    runner_source_lock_sha256: str,
) -> None:
    release_root = input_root / "release"
    if (
        not input_root.is_dir()
        or input_root.is_symlink()
        or not release_root.is_dir()
        or release_root.is_symlink()
        or {item.name for item in input_root.iterdir()} != {"release"}
        or {item.name for item in release_root.iterdir()}
        != {_INPUT_PROTOCOL_NAME, _INPUT_SOURCE_LOCK_NAME}
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
            "installed performance input inventory changed during execution",
        )
    for name, expected_sha256 in (
        (_INPUT_PROTOCOL_NAME, threshold_manifest_sha256),
        (_INPUT_SOURCE_LOCK_NAME, runner_source_lock_sha256),
    ):
        path = release_root / name
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or _sha256_file(path) != expected_sha256
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INPUT_INVALID,
                f"installed performance input changed during execution: {name}",
            )


def _installed_environment(attempt_root: Path) -> dict[str, str]:
    data_root = attempt_root / "data"
    home_root = attempt_root / "home"
    output_root = attempt_root / "output"
    temporary_root = attempt_root / "tmp"
    for path in (data_root, home_root, output_root, temporary_root):
        path.mkdir(parents=True, mode=0o700)
    return {
        "HOME": os.fspath(home_root),
        "KIRBY2_DATA_ROOT": os.fspath(data_root),
        "KIRBY2_PERFORMANCE_ATTEMPT_ROOT": os.fspath(output_root),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": os.fspath(temporary_root),
    }


def _darwin_process_rss_bytes(pid: int) -> int:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2,
        )
        if result.returncode != 0:
            return 0
        return max(0, int(result.stdout.decode("ascii").strip())) * 1024
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        return 0


def _child_peak_rss_bytes(usage: object) -> int:
    """Normalize the per-child high-water RSS returned by ``wait4``."""

    value = getattr(usage, "ru_maxrss", None)
    if type(value) not in {int, float} or value < 0:
        raise RuntimeError("performance child peak RSS measurement is invalid")
    peak = int(value)
    return peak if sys.platform == "darwin" else peak * 1024


def _bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    wall_limit_ns: int,
    rss_limit_bytes: int,
    output_limit_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
    temporary_root: Path | None = None,
    temporary_limit_bytes: int | None = None,
) -> _ProcessObservationV1:
    if (temporary_root is None) != (temporary_limit_bytes is None):
        raise ValueError("temporary resource supervision requires a root and limit")
    started = time.monotonic_ns()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    peak_rss = 0
    peak_temporary = 0
    timed_out = False
    resource_exceeded = False
    output_exceeded = False
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    reaped = False
    child_usage: object | None = None
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    streams = {
        process.stdout.fileno(): (process.stdout, stdout_buffer),
        process.stderr.fileno(): (process.stderr, stderr_buffer),
    }
    for descriptor, (stream, _buffer) in streams.items():
        os.set_blocking(descriptor, False)
        selector.register(stream, selectors.EVENT_READ, descriptor)

    def terminate() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Some managed Darwin sandboxes deny group signalling even for a
            # session created by this process.  Still terminate the exact child;
            # unrestricted qualification hosts retain whole-group enforcement.
            try:
                os.kill(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def reap(*, block: bool) -> None:
        nonlocal reaped, child_usage
        if reaped:
            return
        options = 0 if block else os.WNOHANG
        while True:
            try:
                waited_pid, status, usage = os.wait4(process.pid, options)
                break
            except InterruptedError:
                continue
        if waited_pid == 0:
            return
        if waited_pid != process.pid:
            raise RuntimeError("performance supervisor reaped an unexpected process")
        process.returncode = os.waitstatus_to_exitcode(status)
        child_usage = usage
        reaped = True

    def drain_ready(timeout: float) -> None:
        nonlocal output_exceeded
        for key, _mask in selector.select(timeout):
            descriptor = key.data
            stream, buffer = streams[descriptor]
            while True:
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    break
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    del streams[descriptor]
                    break
                remaining = output_limit_bytes + 1 - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(buffer) > output_limit_bytes:
                    output_exceeded = True

    try:
        while not reaped or streams:
            drain_ready(0.1)
            if not reaped:
                peak_rss = max(peak_rss, _darwin_process_rss_bytes(process.pid))
            if temporary_root is not None:
                peak_temporary = max(
                    peak_temporary,
                    _temporary_tree_bytes(temporary_root),
                )
            elapsed = time.monotonic_ns() - started
            timed_out = elapsed > wall_limit_ns and (not reaped or bool(streams))
            resource_exceeded = (
                peak_rss > rss_limit_bytes
                or (
                    temporary_limit_bytes is not None
                    and peak_temporary > temporary_limit_bytes
                )
            )
            if timed_out or resource_exceeded or output_exceeded:
                terminate()
            reap(block=False)
    except BaseException:
        terminate()
        try:
            reap(block=True)
        except ChildProcessError:
            pass
        raise
    finally:
        selector.close()
        for stream, _buffer in tuple(streams.values()):
            stream.close()
        streams.clear()
    if not reaped:
        # The loop can only finish after all streams close; retain an explicit
        # invariant rather than delegating reaping to Popen's implicit waitpid.
        reap(block=True)
    ended = time.monotonic_ns()
    assert child_usage is not None and process.returncode is not None
    peak_rss = max(peak_rss, _child_peak_rss_bytes(child_usage))
    if temporary_root is not None:
        peak_temporary = max(
            peak_temporary,
            _temporary_tree_bytes(temporary_root),
        )
    timed_out = timed_out or ended - started > wall_limit_ns
    resource_exceeded = (
        resource_exceeded
        or peak_rss > rss_limit_bytes
        or (
            temporary_limit_bytes is not None
            and peak_temporary > temporary_limit_bytes
        )
    )
    stdout = bytes(stdout_buffer)
    stderr = bytes(stderr_buffer)
    if len(stdout) > output_limit_bytes or len(stderr) > output_limit_bytes:
        output_exceeded = True
        stdout = stdout[:output_limit_bytes]
        stderr = stderr[:output_limit_bytes]
    return _ProcessObservationV1(
        argv=argv,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        started_ns=started,
        ended_ns=ended,
        peak_rss_bytes=peak_rss,
        peak_temporary_bytes=peak_temporary,
        timed_out=timed_out,
        resource_exceeded=resource_exceeded,
        output_exceeded=output_exceeded,
    )


def _temporary_tree_bytes(root: Path) -> int:
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in root.rglob("*"):
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            # A supervised child may atomically replace or remove a temporary
            # entry between enumeration and the no-follow stat.  The next 100-ms
            # sample observes the replacement; never follow a transient name.
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED,
                "work-unit temporary tree contains a symlink",
            )
        if stat.S_ISREG(metadata.st_mode):
            identity = (metadata.st_dev, metadata.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += metadata.st_size
    return total


def _utc_second_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_SEMANTIC_MEMBER_NAMES_V1: Final[tuple[str, ...]] = (
    "run_manifest.json",
    "native_recording.json",
    "semantic_result.json",
    "capabilities.json",
    "checks.json",
    "audit_result.json",
)


def _bound_text(bound_row: Mapping[str, object], field: str) -> str:
    value = bound_row.get(field)
    if type(value) is not str or not value:
        raise ValueError(f"bound performance row {field} is invalid")
    return value


def _failed_attempt(
    bound_row: dict[str, object],
    *,
    attempt: int,
    failure_code: str,
    observation: _ProcessObservationV1,
    temporary_bytes: int,
    retry_reason: str | None,
) -> _CapturedAttemptV1:
    expected_capabilities = bound_row.get("expected_capabilities")
    required_checks = bound_row.get("required_checks")
    if type(expected_capabilities) is not list or any(
        type(item) is not str for item in expected_capabilities
    ):
        raise ValueError("bound performance capabilities are invalid")
    if type(required_checks) is not list or any(
        type(item) is not str for item in required_checks
    ):
        raise ValueError("bound performance checks are invalid")
    unavailable_digest = hashlib.sha256(canonical_json_bytes(None)).hexdigest()
    operational = ReleasePerformanceOperationalV1(
        start_monotonic_ns=observation.started_ns,
        end_monotonic_ns=observation.ended_ns,
        peak_rss_bytes=observation.peak_rss_bytes,
        max_temporary_bytes=temporary_bytes,
        retry_reason=retry_reason,
    )
    result = ReleasePerformanceCellResultV1(
        work_unit_id=_bound_text(bound_row, "work_unit_id"),
        attempt=attempt,
        status="FAILED",
        generated_configuration_sha256=_bound_text(
            bound_row, "generated_configuration_sha256"
        ),
        native_fixture_sha256=_bound_text(bound_row, "native_fixture_sha256"),
        runner_source_sha256=_bound_text(bound_row, "runner_source_sha256"),
        capability_records=tuple(
            ReleasePerformanceCapabilityRecordV1(
                capability=item,
                configured_value=None,
                status="NOT_EXERCISED",
                evidence_sha256=unavailable_digest,
            )
            for item in expected_capabilities
        ),
        check_records=tuple(
            ReleasePerformanceCheckRecordV1(
                check_id=item,
                status="NOT_EXERCISED",
                evidence_sha256=unavailable_digest,
            )
            for item in required_checks
        ),
        run_manifest_sha256=None,
        native_recording_sha256=None,
        semantic_result_sha256=None,
        artifact_set_sha256=None,
        audit_result_sha256=None,
        operational=operational,
        failure_code=failure_code,
    )
    result.validate_bound_row(bound_row)
    return _CapturedAttemptV1(
        result=result,
        semantic_members=(),
        compatibility_sidecars=(),
        operational_sidecars=(
            (
                f"operational_attempt_{attempt}.json",
                canonical_json_bytes(operational.as_dict()),
            ),
        ),
    )


def _read_attempt_file(output_root: Path, name: str) -> bytes:
    path = output_root / name
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"installed row output is missing or unsafe: {name}")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_AUXILIARY_OUTPUT_BYTES:
        raise ValueError(f"installed row output size is invalid: {name}")
    load_canonical_json_bytes(raw, f"installed row output {name}")
    return raw


def _typed_worker_failure(
    raw: bytes,
    *,
    bound_row: Mapping[str, object],
    attempt: int,
) -> str | None:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        return None
    try:
        value = load_canonical_json_bytes(raw[:-1], "installed row failure")
    except (TypeError, ValueError):
        return None
    if type(value) is not dict or set(value) != {
        "attempt",
        "command_id",
        "failure_code",
        "schema_version",
        "status",
        "work_unit_id",
    }:
        return None
    failure_code = value["failure_code"]
    if (
        value["attempt"] != attempt
        or value["command_id"] != "QUALIFY_PERFORMANCE_ROW"
        or value["schema_version"] != 1
        or value["status"] != "FAILED"
        or value["work_unit_id"] != bound_row.get("work_unit_id")
        or failure_code
        not in {
            "PROCESS_FAILURE",
            "RESOURCE_LIMIT",
            "SEMANTIC_FAILURE",
            "INVARIANT_FAILURE",
            "REPLAY_FAILURE",
            "SCHEMA_FAILURE",
            "DIGEST_FAILURE",
        }
    ):
        return None
    return failure_code


def _typed_worker_success(
    raw: bytes,
    *,
    bound_row: Mapping[str, object],
    attempt: int,
    result_raw: bytes,
) -> bool:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        return False
    try:
        value = load_canonical_json_bytes(raw[:-1], "installed row completion")
    except (TypeError, ValueError):
        return False
    return type(value) is dict and set(value) == {
        "attempt",
        "command_id",
        "result_sha256",
        "schema_version",
        "status",
        "work_unit_id",
    } and value == {
        "attempt": attempt,
        "command_id": "QUALIFY_PERFORMANCE_ROW",
        "result_sha256": hashlib.sha256(result_raw).hexdigest(),
        "schema_version": 1,
        "status": "COMPLETE",
        "work_unit_id": bound_row.get("work_unit_id"),
    }


def _capture_installed_attempt(
    *,
    installed: _InstalledFormV1,
    input_root: Path,
    row_root: Path,
    bound_row: dict[str, object],
    attempt: int,
    prior: ReleasePerformanceCellResultV1 | None,
) -> _CapturedAttemptV1:
    attempt_root = row_root / f"attempt-{attempt}"
    attempt_root.mkdir(parents=True, mode=0o700)
    environment = _installed_environment(attempt_root)
    retry_reason = None if prior is None else prior.failure_code
    if prior is not None:
        if retry_reason not in {"PROCESS_FAILURE", "RESOURCE_LIMIT"}:
            raise ValueError("performance retry lacks a retryable failure")
        authorization = row_root / "attempt-1-authorization.json"
        with authorization.open("xb") as stream:
            stream.write(prior.canonical_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        authorization.chmod(0o444)
        environment["KIRBY2_PERFORMANCE_PRIOR_RESULT"] = os.fspath(authorization)
        environment["KIRBY2_PERFORMANCE_RETRY_REASON"] = retry_reason
    audit_argv = bound_row.get("audit_argv")
    if type(audit_argv) is not dict:
        raise ValueError("bound performance row lacks its audit argv")
    logical = audit_argv.get(str(attempt))
    if (
        type(logical) is not list
        or not logical
        or logical[0] != "kirby2"
        or any(type(item) is not str for item in logical)
    ):
        raise ValueError("bound performance row audit argv is invalid")
    argv = (os.fspath(installed.kirby2), *logical[1:])
    observation = _bounded_process(
        argv,
        cwd=input_root,
        environment=environment,
        wall_limit_ns=RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1,
        rss_limit_bytes=RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1,
        temporary_root=attempt_root,
        temporary_limit_bytes=RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1,
    )
    temporary_bytes = max(
        observation.peak_temporary_bytes,
        _temporary_tree_bytes(attempt_root),
    )
    exceeded = (
        observation.timed_out
        or observation.resource_exceeded
        or temporary_bytes > RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1
    )
    output_root = Path(environment["KIRBY2_PERFORMANCE_ATTEMPT_ROOT"])
    if exceeded:
        return _failed_attempt(
            bound_row,
            attempt=attempt,
            failure_code="RESOURCE_LIMIT",
            observation=observation,
            temporary_bytes=temporary_bytes,
            retry_reason=retry_reason,
        )
    if observation.output_exceeded:
        return _failed_attempt(
            bound_row,
            attempt=attempt,
            failure_code="PROCESS_FAILURE",
            observation=observation,
            temporary_bytes=temporary_bytes,
            retry_reason=retry_reason,
        )
    expected_names = {
        *_SEMANTIC_MEMBER_NAMES_V1,
        "legacy_digest_bindings.json",
        f"operational_attempt_{attempt}.json",
        "cell-result.json",
    }
    output_entries = tuple(output_root.iterdir())
    observed_names = {item.name for item in output_entries}
    output_inventory_safe = all(
        item.is_file() and not item.is_symlink() for item in output_entries
    )
    declared_failure = (
        _typed_worker_failure(
            observation.stdout,
            bound_row=bound_row,
            attempt=attempt,
        )
        if (
            observation.returncode == 1
            and not observed_names
            and observation.stderr == b""
        )
        else None
    )
    if declared_failure is not None:
        return _failed_attempt(
            bound_row,
            attempt=attempt,
            failure_code=declared_failure,
            observation=observation,
            temporary_bytes=temporary_bytes,
            retry_reason=retry_reason,
        )
    if (
        observation.returncode not in {0, 1}
        or observation.stderr != b""
        or not output_inventory_safe
        or observed_names != expected_names
    ):
        return _failed_attempt(
            bound_row,
            attempt=attempt,
            failure_code="PROCESS_FAILURE",
            observation=observation,
            temporary_bytes=temporary_bytes,
            retry_reason=retry_reason,
        )
    try:
        result_raw = _read_attempt_file(output_root, "cell-result.json")
        result = ReleasePerformanceCellResultV1.from_bytes(result_raw)
        semantic_members = tuple(
            (name, _read_attempt_file(output_root, name))
            for name in _SEMANTIC_MEMBER_NAMES_V1
        )
        compatibility = (
            (
                "legacy_digest_bindings.json",
                _read_attempt_file(output_root, "legacy_digest_bindings.json"),
            ),
        )
        result.validate_bound_row(bound_row)
        if result.attempt != attempt:
            raise ValueError("installed row result attempt differs")
        if (result.status == "COMPLETE") != (observation.returncode == 0):
            raise ValueError("installed row exit status differs from its result")
        if observation.returncode == 0 and not _typed_worker_success(
            observation.stdout,
            bound_row=bound_row,
            attempt=attempt,
            result_raw=result_raw,
        ):
            raise ValueError("installed row completion receipt differs")
        authoritative_peak_rss = max(
            result.operational.peak_rss_bytes,
            observation.peak_rss_bytes,
        )
        authoritative_temporary = max(
            result.operational.max_temporary_bytes,
            temporary_bytes,
        )
        if (
            result.operational.end_monotonic_ns
            - result.operational.start_monotonic_ns
            > RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1
            or authoritative_peak_rss
            > RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1
            or authoritative_temporary
            > RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1
        ):
            limited_observation = replace(
                observation,
                peak_rss_bytes=authoritative_peak_rss,
                peak_temporary_bytes=authoritative_temporary,
                resource_exceeded=True,
            )
            return _failed_attempt(
                bound_row,
                attempt=attempt,
                failure_code="RESOURCE_LIMIT",
                observation=limited_observation,
                temporary_bytes=authoritative_temporary,
                retry_reason=retry_reason,
            )
        operational = replace(
            result.operational,
            start_monotonic_ns=observation.started_ns,
            end_monotonic_ns=observation.ended_ns,
            peak_rss_bytes=authoritative_peak_rss,
            max_temporary_bytes=authoritative_temporary,
            retry_reason=retry_reason,
        )
        installed_output = ReleasePerformanceRowAttemptV1(
            result=result,
            semantic_members=semantic_members,
            compatibility_sidecars=compatibility,
            operational_sidecars=(
                (
                    f"operational_attempt_{attempt}.json",
                    _read_attempt_file(
                        output_root, f"operational_attempt_{attempt}.json"
                    ),
                ),
            ),
            result_bytes=result.canonical_bytes(),
        )
        frozen = replace_performance_row_operational(installed_output, operational)
        verify_performance_row_attempt(frozen, bound_row)
        if prior is not None:
            validate_performance_attempt_sequence((prior, result))
        return _CapturedAttemptV1(
            result=frozen.result,
            semantic_members=frozen.semantic_members,
            compatibility_sidecars=frozen.compatibility_sidecars,
            operational_sidecars=frozen.operational_sidecars,
        )
    except (OSError, TypeError, ValueError):
        return _failed_attempt(
            bound_row,
            attempt=attempt,
            failure_code="PROCESS_FAILURE",
            observation=observation,
            temporary_bytes=temporary_bytes,
            retry_reason=retry_reason,
        )


_PUBLICATION_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_DARWIN_RENAME_EXCL: Final[int] = 0x00000004
_RENAME_EXCLUSIVE_FUNCTION: object | None = None


def _rename_exclusive_at(
    source_directory: int,
    source_name: bytes,
    destination_directory: int,
    destination_name: bytes,
) -> bool:
    """Move one entry without replacement using Darwin's atomic primitive."""

    global _RENAME_EXCLUSIVE_FUNCTION
    if _RENAME_EXCLUSIVE_FUNCTION is None:
        try:
            selected = ctypes.CDLL(None, use_errno=True).renameatx_np
        except AttributeError as error:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "Darwin exclusive publication is unavailable",
            ) from error
        selected.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        selected.restype = ctypes.c_int
        _RENAME_EXCLUSIVE_FUNCTION = selected
    rename_exclusive = _RENAME_EXCLUSIVE_FUNCTION
    if not callable(rename_exclusive):  # pragma: no cover - guarded assignment
        raise RuntimeError("Darwin exclusive publication function is invalid")
    ctypes.set_errno(0)
    if (
        rename_exclusive(
            source_directory,
            source_name,
            destination_directory,
            destination_name,
            _DARWIN_RENAME_EXCL,
        )
        == 0
    ):
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise OSError(error_number, os.strerror(error_number))


def _open_publication_directory(root_descriptor: int, parts: tuple[str, ...]) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow
    )
    current = os.dup(root_descriptor)
    root_metadata = os.fstat(root_descriptor)
    try:
        for part in parts:
            if _PUBLICATION_COMPONENT.fullmatch(part) is None:
                raise ValueError("performance publication component is invalid")
            try:
                os.mkdir(part, 0o755, dir_fd=current)
                os.fsync(current)
            except FileExistsError:
                pass
            following = os.open(part, flags, dir_fd=current)
            metadata = os.fstat(following)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != root_metadata.st_uid
                or metadata.st_dev != root_metadata.st_dev
                or metadata.st_mode & 0o022
            ):
                os.close(following)
                raise _PerformanceRefused(
                    PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                    "performance publication directory ownership is unsafe",
                )
            os.close(current)
            current = following
        return current
    except Exception:
        os.close(current)
        raise


def _existing_publication_bytes(directory: int, name: str) -> bytes | None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.fstat(directory).st_uid
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or not 0 < metadata.st_size <= _MAX_AUXILIARY_OUTPUT_BYTES
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "existing performance record is not immutable and owned",
            )
        raw = b""
        remaining = metadata.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("performance publication was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if os.read(descriptor, 1):
            raise OSError("performance publication grew during read")
        return raw
    finally:
        os.close(descriptor)


def _publish_relative(
    root_descriptor: int,
    relative: str,
    raw: bytes,
    *,
    admit_identical: bool,
) -> None:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_AUXILIARY_OUTPUT_BYTES:
        raise ValueError("performance publication bytes are invalid")
    selected = PurePosixPath(relative)
    if (
        selected.is_absolute()
        or selected.parts[:2] != ("gate-evidence", "wo40-i")
        or any(_PUBLICATION_COMPONENT.fullmatch(part) is None for part in selected.parts)
    ):
        raise ValueError("performance publication path is outside WO40-I")
    directory = _open_publication_directory(root_descriptor, selected.parts[:-1])
    temporary = f".kirby2-performance-{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        existing = _existing_publication_bytes(directory, selected.name)
        if existing is not None:
            if admit_identical and existing == raw:
                return
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                f"immutable performance record already exists: {relative}",
            )
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short performance publication write")
            view = view[count:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if not _rename_exclusive_at(
            directory,
            os.fsencode(temporary),
            directory,
            os.fsencode(selected.name),
        ):
            existing = _existing_publication_bytes(directory, selected.name)
            if not admit_identical or existing != raw:
                raise _PerformanceRefused(
                    PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                    f"performance publication raced with conflicting bytes: {relative}",
                )
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        if _existing_publication_bytes(directory, selected.name) != raw:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                f"performance publication read-back differs: {relative}",
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _cas_reference(
    root_descriptor: int,
    *,
    record_id: str,
    kind: str,
    raw: bytes,
) -> ReleasePerformanceRecordReferenceV1:
    digest = hashlib.sha256(raw).hexdigest()
    path = release_performance_cas_path(digest)
    _publish_relative(root_descriptor, path, raw, admit_identical=True)
    return release_performance_reference(record_id, kind, path, raw)


def _finalize_captured_attempt(
    captured: _CapturedAttemptV1,
    *,
    ended_ns: int,
) -> _CapturedAttemptV1:
    operational = replace(captured.result.operational, end_monotonic_ns=ended_ns)
    exceeded = (
        operational.end_monotonic_ns - operational.start_monotonic_ns
        > RELEASE_PER_ATTEMPT_WALL_LIMIT_NS_V1
        or operational.peak_rss_bytes > RELEASE_PER_ATTEMPT_RSS_LIMIT_BYTES_V1
        or operational.max_temporary_bytes
        > RELEASE_PER_ATTEMPT_TEMP_LIMIT_BYTES_V1
    )
    if captured.semantic_members:
        output = ReleasePerformanceRowAttemptV1(
            result=captured.result,
            semantic_members=captured.semantic_members,
            compatibility_sidecars=captured.compatibility_sidecars,
            operational_sidecars=captured.operational_sidecars,
            result_bytes=captured.result.canonical_bytes(),
        )
        if exceeded:
            limited_result = replace(
                output.result,
                status="FAILED",
                operational=operational,
                failure_code="RESOURCE_LIMIT",
            )
            updated = ReleasePerformanceRowAttemptV1(
                result=limited_result,
                semantic_members=output.semantic_members,
                compatibility_sidecars=output.compatibility_sidecars,
                operational_sidecars=(
                    (
                        f"operational_attempt_{limited_result.attempt}.json",
                        canonical_json_bytes(operational.as_dict()),
                    ),
                ),
                result_bytes=limited_result.canonical_bytes(),
            )
        else:
            updated = replace_performance_row_operational(output, operational)
        return _CapturedAttemptV1(
            result=updated.result,
            semantic_members=updated.semantic_members,
            compatibility_sidecars=updated.compatibility_sidecars,
            operational_sidecars=updated.operational_sidecars,
        )
    failed_result = replace(
        captured.result,
        operational=operational,
        failure_code=("RESOURCE_LIMIT" if exceeded else captured.result.failure_code),
    )
    return _CapturedAttemptV1(
        result=failed_result,
        semantic_members=(),
        compatibility_sidecars=(),
        operational_sidecars=(
            (
                f"operational_attempt_{failed_result.attempt}.json",
                canonical_json_bytes(operational.as_dict()),
            ),
        ),
    )


def _publish_row_attempt(
    root_descriptor: int,
    captured: _CapturedAttemptV1,
) -> tuple[
    _CapturedAttemptV1,
    ReleasePerformanceAttemptPublicationV1,
    ReleasePerformanceArtifactInventoryV1,
]:
    semantic_refs = tuple(
        _cas_reference(
            root_descriptor,
            record_id=name,
            kind="SEMANTIC_MEMBER",
            raw=raw,
        )
        for name, raw in captured.semantic_members
    )
    compatibility_refs = tuple(
        _cas_reference(
            root_descriptor,
            record_id=name,
            kind="COMPATIBILITY_SIDECAR",
            raw=raw,
        )
        for name, raw in captured.compatibility_sidecars
    )
    captured = _finalize_captured_attempt(captured, ended_ns=time.monotonic_ns())
    operational_refs = tuple(
        _cas_reference(
            root_descriptor,
            record_id=name,
            kind="OPERATIONAL_SIDECAR",
            raw=raw,
        )
        for name, raw in captured.operational_sidecars
    )
    result_path, inventory_path = release_performance_work_unit_paths(
        captured.result.work_unit_id,
        captured.result.attempt,
    )
    inventory = ReleasePerformanceArtifactInventoryV1(
        work_unit_id=captured.result.work_unit_id,
        attempt=captured.result.attempt,
        semantic_members=semantic_refs,
        compatibility_sidecars=compatibility_refs,
        operational_sidecars=operational_refs,
        artifact_set_sha256=captured.result.artifact_set_sha256,
    )
    result_raw = captured.result.canonical_bytes()
    inventory_raw = inventory.canonical_bytes()
    _publish_relative(root_descriptor, result_path, result_raw, admit_identical=False)
    _publish_relative(
        root_descriptor, inventory_path, inventory_raw, admit_identical=False
    )
    result_reference = release_performance_reference(
        "cell-result", "RESULT_RECORD", result_path, result_raw
    )
    inventory_reference = release_performance_reference(
        "artifact-inventory", "ARTIFACT_INVENTORY", inventory_path, inventory_raw
    )
    audit_reference = next(
        (item for item in semantic_refs if item.record_id == "audit_result.json"),
        None,
    )
    return (
        captured,
        ReleasePerformanceAttemptPublicationV1(
            attempt=captured.result.attempt,
            result_record=result_reference,
            artifact_inventory_record=inventory_reference,
            audit_record=(
                None
                if audit_reference is None
                else ReleasePerformanceRecordReferenceV1(
                    record_id=audit_reference.record_id,
                    kind="AUDIT_RECORD",
                    path=audit_reference.path,
                    sha256=audit_reference.sha256,
                    size=audit_reference.size,
                )
            ),
        ),
        inventory,
    )


def _execute_and_publish_row(
    *,
    ordinal: int,
    template: object,
    source_tree: RunnerSourceTreeV1,
    installed: _InstalledFormV1,
    input_root: Path,
    row_scratch_root: Path,
    publication_root_descriptor: int,
) -> _CompletedRowV1:
    bound_row = bind_performance_row_template(template, source_tree)  # type: ignore[arg-type]
    work_unit_id = _bound_text(bound_row, "work_unit_id")
    row_root = row_scratch_root / f"row-{ordinal:05d}"
    row_root.mkdir(parents=True, mode=0o700)
    captured_attempts: list[_CapturedAttemptV1] = []
    published_attempts: list[ReleasePerformanceAttemptPublicationV1] = []
    inventories: list[ReleasePerformanceArtifactInventoryV1] = []
    first = _capture_installed_attempt(
        installed=installed,
        input_root=input_root,
        row_root=row_root,
        bound_row=bound_row,
        attempt=1,
        prior=None,
    )
    first, first_publication, first_inventory = _publish_row_attempt(
        publication_root_descriptor, first
    )
    captured_attempts.append(first)
    published_attempts.append(first_publication)
    inventories.append(first_inventory)
    if first.result.status == "FAILED" and first.result.failure_code in {
        "PROCESS_FAILURE",
        "RESOURCE_LIMIT",
    }:
        second = _capture_installed_attempt(
            installed=installed,
            input_root=input_root,
            row_root=row_root,
            bound_row=bound_row,
            attempt=2,
            prior=first.result,
        )
        second, second_publication, second_inventory = _publish_row_attempt(
            publication_root_descriptor, second
        )
        captured_attempts.append(second)
        published_attempts.append(second_publication)
        inventories.append(second_inventory)
    results = tuple(item.result for item in captured_attempts)
    validate_performance_attempt_sequence(results)
    final_status = results[-1].status
    publication = ReleasePerformanceWorkUnitPublicationV1(
        work_unit_id=work_unit_id,
        status=final_status,
        attempts=tuple(published_attempts),
    )
    return _CompletedRowV1(
        ordinal=ordinal,
        work_unit_id=work_unit_id,
        attempts=results,
        inventories=tuple(inventories),
        publication=publication,
    )


def _execute_row_corpus(
    *,
    source_tree: RunnerSourceTreeV1,
    installed: _InstalledFormV1,
    input_root: Path,
    scratch: Path,
    publication_root_descriptor: int,
) -> tuple[tuple[_CompletedRowV1, ...], int, str, str]:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError as error:  # pragma: no cover - designated Darwin always has fork
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.RESOURCES_UNAVAILABLE,
            "the designated host lacks persistent fork workers",
        ) from error
    work_queue = context.JoinableQueue(
        maxsize=RELEASE_PERFORMANCE_QUEUE_SIZE_V1
    )
    result_pipes = tuple(
        context.Pipe(duplex=False)
        for _ in range(RELEASE_PERFORMANCE_WORKER_COUNT_V1)
    )
    result_receivers = tuple(item[0] for item in result_pipes)
    result_senders = tuple(item[1] for item in result_pipes)
    start_event = context.Event()
    abort_event = context.Event()
    ready_events = tuple(
        context.Event() for _ in range(RELEASE_PERFORMANCE_WORKER_COUNT_V1)
    )
    started_value = context.Value("Q", 0, lock=False)
    completed: list[_CompletedRowV1] = []
    row_scratch = scratch / "row-attempts"
    row_scratch.mkdir(parents=True, mode=0o700)

    def worker(worker_id: int) -> None:
        for receiver in result_receivers:
            receiver.close()
        sender = result_senders[worker_id]
        for ordinal, unused_sender in enumerate(result_senders):
            if ordinal != worker_id:
                unused_sender.close()
        ready_events[worker_id].set()
        start_event.wait()
        try:
            while True:
                item = work_queue.get()
                try:
                    if item is None:
                        return
                    if abort_event.is_set():
                        continue
                    started_ns = int(started_value.value)
                    if started_ns <= 0:
                        raise RuntimeError("performance total timer was not armed")
                    if time.monotonic_ns() - started_ns > RELEASE_TOTAL_WALL_LIMIT_NS_V1:
                        raise _PerformanceRefused(
                            PerformanceExecutorRefusalCodeV1.TOTAL_LIMIT,
                            "the frozen 36-hour performance limit was exceeded",
                        )
                    if (
                        type(item) is not tuple
                        or len(item) != 2
                        or type(item[0]) is not int
                    ):
                        raise RuntimeError("performance FIFO item is invalid")
                    row = _execute_and_publish_row(
                        ordinal=item[0],
                        template=item[1],
                        source_tree=source_tree,
                        installed=installed,
                        input_root=input_root,
                        row_scratch_root=row_scratch,
                        publication_root_descriptor=publication_root_descriptor,
                    )
                    sender.send(("ROW", worker_id, row))
                except BaseException as error:
                    abort_event.set()
                    if isinstance(error, _PerformanceRefused):
                        sender.send(
                            (
                                "ERROR",
                                worker_id,
                                error.code.value,
                                error.detail,
                                type(error).__name__,
                            )
                        )
                    else:
                        sender.send(
                            (
                                "ERROR",
                                worker_id,
                                None,
                                str(error)[:4096],
                                type(error).__name__,
                            )
                        )
                finally:
                    work_queue.task_done()
        finally:
            sender.close()

    workers = tuple(
        context.Process(
            target=worker,
            args=(worker_id,),
            name=f"kirby2-wo40i-{worker_id}",
            daemon=False,
        )
        for worker_id in range(RELEASE_PERFORMANCE_WORKER_COUNT_V1)
    )
    started_processes: list[multiprocessing.Process] = []
    try:
        for process in workers:
            process.start()
            started_processes.append(process)
    except (OSError, RuntimeError) as error:
        abort_event.set()
        start_event.set()
        for process in started_processes:
            if process.is_alive():
                process.terminate()
        for process in started_processes:
            process.join(timeout=30)
        work_queue.close()
        for receiver in result_receivers:
            receiver.close()
        for sender in result_senders:
            sender.close()
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.RESOURCES_UNAVAILABLE,
            "persistent performance workers could not start",
        ) from error
    for sender in result_senders:
        sender.close()
    for ready in ready_events:
        if not ready.wait(timeout=30):
            abort_event.set()
            start_event.set()
            for process in workers:
                if process.is_alive():
                    process.terminate()
            for process in workers:
                process.join(timeout=30)
            work_queue.close()
            for receiver in result_receivers:
                receiver.close()
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.RESOURCES_UNAVAILABLE,
                "one or more persistent performance worker processes did not become ready",
            )
    started_utc = _utc_second_now()
    started_ns = time.monotonic_ns()
    started_value.value = started_ns
    start_event.set()
    expected_count = RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
    templates = iter(enumerate(iter_performance_row_templates()))
    pending: tuple[int, object] | None = None
    admitted = 0
    received = 0
    exhausted = False
    failure: tuple[object, ...] | None = None
    forced_worker_termination = False
    try:
        while received < expected_count and failure is None:
            while admitted < expected_count and not abort_event.is_set():
                if pending is None:
                    try:
                        pending = next(templates)
                    except StopIteration:
                        exhausted = True
                        break
                try:
                    work_queue.put(pending, block=False)
                except queue.Full:
                    break
                pending = None
                admitted += 1
            if exhausted and pending is None and received == admitted:
                failure = (
                    "ERROR",
                    -1,
                    None,
                    "performance template generator ended before 10,000 rows",
                    "RuntimeError",
                )
                break
            if time.monotonic_ns() - started_ns > RELEASE_TOTAL_WALL_LIMIT_NS_V1:
                abort_event.set()
                failure = (
                    "ERROR",
                    -1,
                    PerformanceExecutorRefusalCodeV1.TOTAL_LIMIT.value,
                    "the frozen 36-hour performance limit was exceeded",
                    "_PerformanceRefused",
                )
                break
            readable = wait_connections(result_receivers, timeout=0.25)
            if not readable:
                failed_processes = tuple(
                    process
                    for process in workers
                    if process.exitcode is not None and process.exitcode != 0
                )
                if failed_processes:
                    failure = (
                        "ERROR",
                        -1,
                        None,
                        "a persistent performance worker exited unexpectedly",
                        "ChildProcessError",
                    )
                    abort_event.set()
                continue
            try:
                message = readable[0].recv()
            except EOFError:
                abort_event.set()
                failure = (
                    "ERROR",
                    -1,
                    None,
                    "a persistent performance worker closed its result pipe",
                    "ChildProcessError",
                )
                continue
            if (
                type(message) is not tuple
                or not message
                or message[0] not in {"ROW", "ERROR"}
            ):
                abort_event.set()
                failure = (
                    "ERROR",
                    -1,
                    None,
                    "performance worker returned an invalid coordinator message",
                    "RuntimeError",
                )
            elif message[0] == "ERROR":
                abort_event.set()
                failure = message
            elif (
                len(message) != 3
                or type(message[2]) is not _CompletedRowV1
            ):
                abort_event.set()
                failure = (
                    "ERROR",
                    -1,
                    None,
                    "performance worker returned an invalid row result",
                    "RuntimeError",
                )
            else:
                completed.append(message[2])
                received += 1

        if failure is None and admitted == expected_count:
            try:
                next(templates)
            except StopIteration:
                exhausted = True
            else:
                failure = (
                    "ERROR",
                    -1,
                    None,
                    "performance template generator exceeded 10,000 rows",
                    "RuntimeError",
                )
                abort_event.set()
        if failure is None and not exhausted:
            failure = (
                "ERROR",
                -1,
                None,
                "performance template generator did not close exactly",
                "RuntimeError",
            )
            abort_event.set()

        unexpected_worker_exit = any(
            process.exitcode is not None for process in workers
        )
        if unexpected_worker_exit:
            abort_event.set()
            for process in workers:
                if process.is_alive():
                    process.terminate()
        else:
            for _process in workers:
                work_queue.put(None)
            # Exact row receipt accounting above proves that all admitted work
            # finished.  Do not block on JoinableQueue's unauditable semaphore:
            # an abrupt worker exit between this point and task_done could strand
            # the qualification forever.  The bounded process joins below are the
            # authoritative persistent-worker shutdown check.
    finally:
        start_event.set()
        for process in workers:
            process.join(timeout=30)
        for process in workers:
            if process.is_alive():
                forced_worker_termination = True
                process.terminate()
        for process in workers:
            process.join(timeout=30)
        work_queue.close()
        for receiver in result_receivers:
            receiver.close()
    if any(process.is_alive() for process in workers):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED,
            "persistent performance worker processes did not terminate",
        )
    if forced_worker_termination:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED,
            "persistent performance workers required forced termination",
        )
    if failure is not None:
        code = failure[2] if len(failure) > 2 else None
        detail = failure[3] if len(failure) > 3 else "performance worker failed"
        error_type = failure[4] if len(failure) > 4 else "RuntimeError"
        if type(code) is str:
            try:
                refusal_code = PerformanceExecutorRefusalCodeV1(code)
            except ValueError:
                refusal_code = PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED
            raise _PerformanceRefused(refusal_code, str(detail)[:4096])
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED,
            f"performance worker failed: {error_type}: {str(detail)[:2048]}",
        )
    ordered = tuple(sorted(completed, key=lambda item: item.ordinal))
    if (
        len(ordered) != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1
        or tuple(item.ordinal for item in ordered)
        != tuple(range(RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1))
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.WORK_UNIT_FAILED,
            "performance corpus did not produce exactly 10,000 ordered work units",
        )
    return ordered, started_ns, started_utc, _utc_second_now()


def _performance_status_codes(
    *,
    complete_count: int,
    failed_rows: tuple[tuple[str, str], ...],
    auxiliary_results: tuple[ReleaseAuxiliaryPerformanceResultV1, ...],
    total_wall_ns: int,
    throughput_status: str,
    artifact_status: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    failures = {
        f"WORK_UNIT_FAILED:{work_unit_id}:{code}"
        for work_unit_id, code in failed_rows
    }
    warnings: set[str] = set()
    if complete_count != RELEASE_PERFORMANCE_WORK_UNIT_COUNT_V1:
        failures.add("INCOMPLETE_WORK_UNITS")
    if total_wall_ns > RELEASE_TOTAL_WALL_LIMIT_NS_V1:
        failures.add("TOTAL_WALL_LIMIT")
    if throughput_status == "FAIL":
        failures.add("THROUGHPUT_THRESHOLD_MISS")
    elif throughput_status == "WARNING":
        warnings.add("THROUGHPUT_WARNING")
    if artifact_status == "FAIL":
        failures.add("ARTIFACT_BYTES_THRESHOLD_MISS")
    elif artifact_status == "WARNING":
        warnings.add("ARTIFACT_BYTES_WARNING")
    for result in auxiliary_results:
        if result.status == "FAIL":
            codes = result.hard_failure_codes or ("THRESHOLD_MISS",)
            failures.update(
                f"AUXILIARY_FAILED:{result.workload_id}:{code}" for code in codes
            )
        elif result.status == "WARNING":
            warnings.add(f"AUXILIARY_WARNING:{result.workload_id}")
    ordered_failures = tuple(
        sorted(failures, key=lambda item: item.encode("utf-8"))
    )
    ordered_warnings = tuple(
        sorted(warnings, key=lambda item: item.encode("utf-8"))
    )
    status = (
        "FAIL"
        if ordered_failures
        else ("PASS_WITH_WARNINGS" if ordered_warnings else "PASS")
    )
    return ordered_failures, ordered_warnings, status


def _build_performance_aggregate(
    *,
    rows: tuple[_CompletedRowV1, ...],
    auxiliaries: tuple[ReleasePerformanceAuxiliaryReferenceV1, ...],
    auxiliary_results: tuple[ReleaseAuxiliaryPerformanceResultV1, ...],
    inputs: ReleasePerformanceVerificationInputsV1,
    start_monotonic_ns: int,
    end_monotonic_ns: int,
) -> tuple[ReleasePerformanceAggregateV1, tuple[str, ...], tuple[str, ...]]:
    path_claims: dict[str, tuple[str, int]] = {}
    digest_sizes: dict[str, int] = {}
    cas_digest_sizes: dict[str, int] = {}
    logical_record_count = 0
    logical_referenced_bytes = 0
    cas_kinds = {
        "AUDIT_RECORD",
        "AUXILIARY_EVIDENCE",
        "COMPATIBILITY_SIDECAR",
        "OPERATIONAL_SIDECAR",
        "SEMANTIC_MEMBER",
    }

    def account(reference: ReleasePerformanceRecordReferenceV1) -> None:
        nonlocal logical_record_count, logical_referenced_bytes
        claim = (reference.sha256, reference.size)
        if path_claims.setdefault(reference.path, claim) != claim:
            raise ValueError("performance publication path claim conflicts")
        if digest_sizes.setdefault(reference.sha256, reference.size) != reference.size:
            raise ValueError("performance publication digest size conflicts")
        if reference.kind in cas_kinds and (
            cas_digest_sizes.setdefault(reference.sha256, reference.size)
            != reference.size
        ):
            raise ValueError("performance CAS digest size conflicts")
        logical_record_count += 1
        logical_referenced_bytes += reference.size

    complete_ids: set[str] = set()
    failed_rows: list[tuple[str, str]] = []
    retry_count = 0
    for row in rows:
        if len(row.publication.attempts) == 2:
            retry_count += 1
        for attempt in row.publication.attempts:
            account(attempt.result_record)
            account(attempt.artifact_inventory_record)
            inventory = row.inventories[attempt.attempt - 1]
            for reference in (
                *inventory.semantic_members,
                *inventory.compatibility_sidecars,
                *inventory.operational_sidecars,
            ):
                account(reference)
        final = row.attempts[-1]
        if final.status == "COMPLETE":
            complete_ids.add(row.work_unit_id)
        else:
            failed_rows.append((row.work_unit_id, str(final.failure_code)))
    for auxiliary in auxiliaries:
        account(auxiliary.result_record)
        for reference in auxiliary.evidence_records:
            account(reference)
    work_units = tuple(item.publication for item in rows)
    total_wall_ns = end_monotonic_ns - start_monotonic_ns
    if total_wall_ns <= 0:
        raise ValueError("performance aggregate total wall interval is invalid")
    complete_count = len(complete_ids)
    throughput = round_div_even(complete_count * 10**15, total_wall_ns)
    if complete_count * 10**9 >= total_wall_ns:
        throughput_status = "PASS"
    elif 10 * complete_count * 10**9 >= total_wall_ns:
        throughput_status = "WARNING"
    else:
        throughput_status = "FAIL"
    aggregate_artifact_bytes = sum(digest_sizes.values())
    if aggregate_artifact_bytes <= RELEASE_ARTIFACT_PASS_BYTES_V1:
        artifact_status = "PASS"
    elif aggregate_artifact_bytes <= RELEASE_ARTIFACT_WARNING_BYTES_V1:
        artifact_status = "WARNING"
    else:
        artifact_status = "FAIL"
    failures, warnings, status = _performance_status_codes(
        complete_count=complete_count,
        failed_rows=tuple(failed_rows),
        auxiliary_results=auxiliary_results,
        total_wall_ns=total_wall_ns,
        throughput_status=throughput_status,
        artifact_status=artifact_status,
    )
    work_units_sha256 = hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in work_units])
    ).hexdigest()
    auxiliary_results_sha256 = hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in auxiliaries])
    ).hexdigest()
    cas_rows = [
        {"sha256": digest, "size": cas_digest_sizes[digest]}
        for digest in sorted(cas_digest_sizes, key=lambda item: item.encode("utf-8"))
    ]
    aggregate = ReleasePerformanceAggregateV1(
        status=status,
        candidate_commit=inputs.candidate_commit,
        source_manifest_sha256=inputs.source_manifest_sha256,
        protocol_set_sha256=inputs.protocol_set_sha256,
        artifact_index_sha256=inputs.artifact_index_sha256,
        build_evidence_sha256=inputs.build_evidence_sha256,
        threshold_manifest_sha256=inputs.threshold_manifest_sha256,
        runner_source_lock_sha256=inputs.runner_source_lock_sha256,
        row_corpus_sha256=inputs.row_corpus_sha256,
        work_units_sha256=work_units_sha256,
        auxiliary_results_sha256=auxiliary_results_sha256,
        cas_inventory_sha256=hashlib.sha256(
            canonical_json_bytes(cas_rows)
        ).hexdigest(),
        work_unit_count=len(work_units),
        unique_complete_run_ids=len(complete_ids),
        complete_work_unit_count=complete_count,
        complete_result_records=complete_count,
        complete_artifact_records=complete_count,
        complete_audit_records=complete_count,
        auxiliary_result_count=len(auxiliary_results),
        retry_count=retry_count,
        failed_work_unit_count=len(failed_rows),
        total_wall_ns=total_wall_ns,
        throughput_microruns_per_second=throughput,
        throughput_status=throughput_status,
        aggregate_artifact_bytes=aggregate_artifact_bytes,
        logical_referenced_bytes=logical_referenced_bytes,
        artifact_bytes_status=artifact_status,
        cas_object_count=len(cas_digest_sizes),
        logical_record_count=logical_record_count,
        warning_count=len(warnings),
        failure_codes=failures,
    )
    return aggregate, warnings, failures


def _microscope_asset_manifest_sha256(
    installed: _InstalledFormV1,
    *,
    candidate_commit: str,
) -> str:
    """Bind the microscope workload to the exact installed desktop asset array."""

    if installed.form != "desktop":
        raise ValueError("microscope assets require the installed desktop form")
    manifest_path = installed.bundle_root / "RELEASE_MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            "the installed desktop release manifest is missing or unsafe",
        )
    raw = manifest_path.read_bytes()
    manifest = ReleaseManifestV1.from_bytes(raw)
    if (
        hashlib.sha256(raw).hexdigest()
        != installed.artifact_manifest_sha256
        or manifest.candidate_commit != candidate_commit
        or (
            manifest.target.system,
            manifest.target.machine,
            manifest.target.artifact_form,
        )
        != ("Darwin", "arm64", "DESKTOP_TAR_GZ")
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
            "the installed desktop manifest differs from its WO40-F binding",
        )
    for asset in manifest.assets:
        path = installed.bundle_root.joinpath(*PurePosixPath(asset.path).parts)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != asset.size
            or _sha256_file(path) != asset.sha256
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.INSTALLATION_FAILED,
                f"installed microscope asset differs: {asset.path}",
            )
    return hashlib.sha256(
        canonical_json_bytes([item.as_dict() for item in manifest.assets])
    ).hexdigest()


def _read_auxiliary_result(output_root: Path) -> ReleaseAuxiliaryPerformanceResultV1:
    result_path = output_root / "result.json"
    if not result_path.is_file() or result_path.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "installed auxiliary execution omitted its canonical result",
        )
    raw = result_path.read_bytes()
    if not 0 < len(raw) <= _MAX_AUXILIARY_OUTPUT_BYTES:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "installed auxiliary result is outside its byte bound",
        )
    return ReleaseAuxiliaryPerformanceResultV1.from_bytes(raw)


def _execute_and_publish_auxiliary(
    *,
    request: ReleaseAuxiliaryExecutionV1,
    installed: _InstalledFormV1,
    scratch: Path,
    publication_root_descriptor: int,
) -> tuple[
    ReleaseAuxiliaryPerformanceResultV1,
    ReleasePerformanceAuxiliaryReferenceV1,
    Path,
]:
    workload_id = request.template.workload_id
    if request.template.artifact_selector != f"macos-arm64/{installed.form}":
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            f"auxiliary artifact selector differs for {workload_id}",
        )
    slug = workload_id.removeprefix("RELEASE_").removesuffix("_V1").lower()
    execution_root = scratch / slug
    request_root = execution_root / "request"
    attempt_root = execution_root / "process"
    output_root = execution_root / "publication"
    request_root.mkdir(parents=True, mode=0o700)
    request_path = request_root / "request.json"
    request_path.write_bytes(request.canonical_bytes())
    request_path.chmod(0o444)
    environment = _installed_environment(attempt_root)
    observation = _bounded_process(
        (
            os.fspath(installed.python),
            "-I",
            "-c",
            (
                "from kirby2.release.performance_auxiliary import main;"
                "raise SystemExit(main())"
            ),
            "--request",
            os.fspath(request_path.resolve(strict=True)),
            "--output-root",
            os.fspath(output_root.absolute()),
        ),
        cwd=execution_root,
        environment=environment,
        wall_limit_ns=RELEASE_TOTAL_WALL_LIMIT_NS_V1,
        rss_limit_bytes=_MINIMUM_MEMORY_BYTES,
        output_limit_bytes=_MAX_AUXILIARY_OUTPUT_BYTES,
    )
    if (
        observation.timed_out
        or observation.resource_exceeded
        or observation.output_exceeded
        or observation.returncode not in {0, 1}
    ):
        detail = observation.stderr[:2048].decode("utf-8", errors="replace")
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            f"installed auxiliary process failed for {workload_id}: {detail}",
        )
    result = _read_auxiliary_result(output_root)
    top_level = {item.name: item for item in output_root.iterdir()}
    if set(top_level) != {"evidence", "result.json", "workspace"} or any(
        item.is_symlink() for item in top_level.values()
    ) or not (
        top_level["evidence"].is_dir()
        and top_level["result.json"].is_file()
        and top_level["workspace"].is_dir()
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            f"installed auxiliary output inventory differs for {workload_id}",
        )
    expected_returncode = 0 if result.status in {"PASS", "WARNING"} else 1
    if (
        observation.returncode != expected_returncode
        or observation.stderr != b""
        or observation.stdout != result.canonical_bytes() + b"\n"
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            f"installed auxiliary process/result status differs for {workload_id}",
        )
    verified = verify_auxiliary_performance_execution(request, output_root, result)
    result_raw = verified.canonical_bytes()
    result_path = release_performance_auxiliary_path(workload_id)
    _publish_relative(
        publication_root_descriptor,
        result_path,
        result_raw,
        admit_identical=False,
    )
    result_reference = release_performance_reference(
        workload_id,
        "AUXILIARY_RESULT",
        result_path,
        result_raw,
    )
    evidence_root = output_root / "evidence"
    evidence_references: list[ReleasePerformanceRecordReferenceV1] = []
    for record in verified.evidence_records:
        evidence_id = record["evidence_id"]
        if type(evidence_id) is not str:
            raise ValueError("auxiliary evidence ID is not text")
        evidence_path = evidence_root.joinpath(*PurePosixPath(evidence_id).parts)
        raw = evidence_path.read_bytes()
        reference = _cas_reference(
            publication_root_descriptor,
            record_id=evidence_id,
            kind="AUXILIARY_EVIDENCE",
            raw=raw,
        )
        if (
            reference.sha256 != record["sha256"]
            or reference.size != record["size"]
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
                f"auxiliary evidence changed before publication: {evidence_id}",
            )
        evidence_references.append(reference)
    wrapper = ReleasePerformanceAuxiliaryReferenceV1(
        workload_id=workload_id,
        result_record=result_reference,
        evidence_records=tuple(evidence_references),
    )
    return verified, wrapper, output_root.resolve(strict=True)


def _generation_source_runs(output_root: Path) -> tuple[ReleaseAuxiliarySourceRunV1, ...]:
    source_path = output_root / "evidence/full-day-generation/sources.json"
    if not source_path.is_file() or source_path.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "full-day generation omitted its source inventory",
        )
    value = load_canonical_json_bytes(
        source_path.read_bytes(), "full-day generation source inventory"
    )
    if type(value) is not dict or set(value) != {
        "producer_workload_id",
        "sources",
        "status",
    } or value["producer_workload_id"] != "RELEASE_FULL_DAY_GENERATION_V1" or (
        value["status"] != "PASS"
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "full-day generation source inventory identity differs",
        )
    rows = value["sources"]
    if type(rows) is not list or len(rows) != 4:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "full-day generation did not expose exactly four private sources",
        )
    sources: list[ReleaseAuxiliarySourceRunV1] = []
    for ordinal, item in enumerate(rows):
        if type(item) is not dict or set(item) != {
            "artifact_id",
            "manifest_sha256",
            "ordinal",
            "run_id",
            "store_relative_root",
        } or item["ordinal"] != ordinal:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
                "full-day generation source row differs",
            )
        relative = item["store_relative_root"]
        if type(relative) is not str:
            raise ValueError("full-day source store path is not text")
        artifact_id = item["artifact_id"]
        run_id = item["run_id"]
        manifest_sha256 = item["manifest_sha256"]
        if (
            type(artifact_id) is not str
            or artifact_id != f"release-full-day-generation-{ordinal:04d}"
            or type(run_id) is not str
            or type(manifest_sha256) is not str
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
                "full-day generation source identity differs",
            )
        selected = PurePosixPath(relative)
        expected = PurePosixPath(
            f"workspace/generation/ordinal-{ordinal:04d}"
        )
        if selected != expected:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
                "full-day generation source store path differs",
            )
        store_root = output_root.joinpath(*selected.parts).resolve(strict=True)
        if output_root not in store_root.parents or not store_root.is_dir():
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
                "full-day generation source escaped its private workspace",
            )
        sources.append(
            ReleaseAuxiliarySourceRunV1(
                artifact_id=artifact_id,
                ordinal=ordinal,
                store_root=os.fspath(store_root),
                run_id=run_id,
                manifest_sha256=manifest_sha256,
            )
        )
    return tuple(sources)


def _execute_auxiliary_corpus(
    *,
    prepared: _PreparedPerformanceV1,
    installed_forms: Mapping[str, _InstalledFormV1],
    microscope_asset_manifest_sha256: str,
    scratch: Path,
    publication_root_descriptor: int,
) -> tuple[
    tuple[ReleaseAuxiliaryPerformanceResultV1, ...],
    tuple[ReleasePerformanceAuxiliaryReferenceV1, ...],
]:
    results: list[ReleaseAuxiliaryPerformanceResultV1] = []
    publications: list[ReleasePerformanceAuxiliaryReferenceV1] = []
    generation_sources: tuple[ReleaseAuxiliarySourceRunV1, ...] = ()
    for template in prepared.auxiliary_templates:
        workload_id = template.workload_id
        if workload_id == "RELEASE_FULL_DAY_REPLAY_V1":
            source_runs = generation_sources
        elif workload_id == "RELEASE_MICROSCOPE_LOAD_V1":
            source_runs = generation_sources[1:2]
        else:
            source_runs = ()
        request = ReleaseAuxiliaryExecutionV1(
            template=template,
            candidate_commit=prepared.candidate_commit,
            source_tree=prepared.source_tree,
            artifact_manifest_sha256=prepared.artifact_index_sha256,
            asset_manifest_sha256=(
                microscope_asset_manifest_sha256
                if workload_id == "RELEASE_MICROSCOPE_LOAD_V1"
                else None
            ),
            source_runs=source_runs,
        )
        installed = installed_forms[_AUXILIARY_FORMS[workload_id]]
        result, publication, output_root = _execute_and_publish_auxiliary(
            request=request,
            installed=installed,
            scratch=scratch,
            publication_root_descriptor=publication_root_descriptor,
        )
        results.append(result)
        publications.append(publication)
        if workload_id == "RELEASE_FULL_DAY_GENERATION_V1":
            generation_sources = _generation_source_runs(output_root)
    if len(generation_sources) != 4:
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.AUXILIARY_FAILED,
            "full-day auxiliary dependency inventory is incomplete",
        )
    return tuple(results), tuple(publications)


def _verification_inputs(
    bundle: ReleaseProtocolBundleV1,
    prepared: _PreparedPerformanceV1,
    *,
    microscope_asset_manifest_sha256: str,
) -> ReleasePerformanceVerificationInputsV1:
    return ReleasePerformanceVerificationInputsV1(
        candidate_commit=prepared.candidate_commit,
        source_manifest_sha256=prepared.build_record.source_manifest_sha256,
        protocol_set_sha256=bundle.protocol_set_sha256,
        artifact_index_sha256=prepared.artifact_index_sha256,
        build_evidence_sha256=prepared.build_evidence_sha256,
        threshold_manifest_sha256=prepared.threshold_manifest_sha256,
        runner_source_lock_sha256=prepared.runner_source_lock_sha256,
        row_corpus_sha256=bundle.performance_protocol.row_corpus_sha256,
        source_tree=prepared.source_tree,
        auxiliary_templates=prepared.auxiliary_templates,
        microscope_asset_manifest_sha256=microscope_asset_manifest_sha256,
    )


def _performance_outcome(
    bundle: ReleaseProtocolBundleV1,
    *,
    status: ReleaseCommandStatusV1,
    detail: str,
    payload: Mapping[str, object] | None = None,
    refusal_code: PerformanceExecutorRefusalCodeV1 | None = None,
) -> ReleaseCommandOutcomeV1:
    return ReleaseCommandOutcomeV1(
        command_id="QUALIFY_PERFORMANCE",
        status=status,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail=detail,
        payload=dict(payload or {}),
        refusal_code=None if refusal_code is None else refusal_code.value,
    )


def _rehearse_aggregate_publication(
    aggregate: ReleasePerformanceAggregateV1,
    rehearsal_root: Path,
) -> None:
    """Exercise aggregate encoding, commit, fsync, reread, and digest verification.

    The aggregate records ``total_wall_ns``.  A literal timer endpoint taken after
    committing the final bytes containing that endpoint is therefore circular.  The
    private rehearsal performs the same bounded publication work before the endpoint
    is captured; the measured aggregate is then encoded once from that endpoint and
    published immutably.  A separate post-verification deadline check below prevents
    the final publication work from silently crossing the 36-hour hard limit.
    """

    rehearsal_root.mkdir(parents=True, mode=0o700)
    path = rehearsal_root / "performance-aggregate.json"
    raw = aggregate.canonical_bytes()
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o444)
    reread = path.read_bytes()
    if (
        reread != raw
        or hashlib.sha256(reread).hexdigest()
        != hashlib.sha256(raw).hexdigest()
        or ReleasePerformanceAggregateV1.from_bytes(reread) != aggregate
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.VERIFICATION_FAILED,
            "private aggregate publication rehearsal changed bytes",
        )
    descriptor = os.open(
        rehearsal_root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _activate_staged_publication(staging_root: Path, artifact_root: Path) -> None:
    source = staging_root / "gate-evidence/wo40-i"
    source_parent = source.parent
    destination_parent = artifact_root / "gate-evidence"
    destination = destination_parent / "wo40-i"
    if not source.is_dir() or source.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
            "verified WO40-I staging publication is unavailable",
        )
    if not destination_parent.is_dir() or destination_parent.is_symlink():
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
            "release gate-evidence parent is unavailable or unsafe",
        )
    parent_metadata = destination_parent.stat(follow_symlinks=False)
    if (
        parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o022
        or destination.exists()
        or destination.is_symlink()
    ):
        raise _PerformanceRefused(
            PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
            "WO40-I activation destination is unsafe or already occupied",
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = os.open(source_parent, directory_flags)
    destination_descriptor = os.open(destination_parent, directory_flags)
    try:
        source_metadata = os.fstat(source_descriptor)
        destination_metadata = os.fstat(destination_descriptor)
        if (
            source_metadata.st_uid != os.getuid()
            or destination_metadata.st_uid != os.getuid()
            or source_metadata.st_dev != destination_metadata.st_dev
            or source_metadata.st_mode & 0o022
            or destination_metadata.st_mode & 0o022
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "WO40-I activation directories are not owned and immutable",
            )
        if not _rename_exclusive_at(
            source_descriptor,
            b"wo40-i",
            destination_descriptor,
            b"wo40-i",
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "WO40-I activation raced with an existing destination",
            )
        os.fsync(source_descriptor)
        os.fsync(destination_descriptor)
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _execute_release_performance_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    manifest: Path,
    complete_run_work_units: int,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    prepared = _prepare_performance_inputs(
        bundle,
        manifest=manifest,
        complete_run_work_units=complete_run_work_units,
        build_evidence=build_evidence,
        artifact_root=artifact_root,
    )
    repository = bundle.repository_root.resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="kirby2-wo40i-", dir=prepared.artifact_root.parent
    ) as scratch_text:
        scratch = Path(scratch_text).resolve(strict=True)
        install_root = scratch / "installed"
        runtime_root = scratch / "runtime"
        staging_root = scratch / "staging"
        for path in (install_root, runtime_root, staging_root):
            path.mkdir(mode=0o700)
        installed_forms = {
            form: _install_form(
                form=form,
                artifact_root=prepared.artifact_root,
                scratch=install_root,
                bundle=bundle,
                build_record=prepared.build_record,
            )
            for form in _FORMS
        }
        desktop = installed_forms["desktop"]
        microscope_asset_sha256 = _microscope_asset_manifest_sha256(
            desktop,
            candidate_commit=prepared.candidate_commit,
        )
        inputs = _verification_inputs(
            bundle,
            prepared,
            microscope_asset_manifest_sha256=microscope_asset_sha256,
        )
        input_root = _prepare_installed_inputs(
            repository,
            runtime_root,
            threshold_manifest_sha256=prepared.threshold_manifest_sha256,
            runner_source_lock_sha256=prepared.runner_source_lock_sha256,
        )
        root_descriptor = os.open(
            staging_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            auxiliary_results, auxiliary_publications = _execute_auxiliary_corpus(
                prepared=prepared,
                installed_forms=installed_forms,
                microscope_asset_manifest_sha256=microscope_asset_sha256,
                scratch=runtime_root / "auxiliary",
                publication_root_descriptor=root_descriptor,
            )
            rows, started_ns, started_utc, _rows_finished_utc = _execute_row_corpus(
                source_tree=prepared.source_tree,
                installed=installed_forms["headless"],
                input_root=input_root,
                scratch=runtime_root,
                publication_root_descriptor=root_descriptor,
            )
            _verify_installed_inputs(
                input_root,
                threshold_manifest_sha256=prepared.threshold_manifest_sha256,
                runner_source_lock_sha256=prepared.runner_source_lock_sha256,
            )
            rehearsal_end_ns = max(time.monotonic_ns(), started_ns + 1)
            rehearsal_aggregate, _rehearsal_warnings, _rehearsal_failures = (
                _build_performance_aggregate(
                    rows=rows,
                    auxiliaries=auxiliary_publications,
                    auxiliary_results=auxiliary_results,
                    inputs=inputs,
                    start_monotonic_ns=started_ns,
                    end_monotonic_ns=rehearsal_end_ns,
                )
            )
            _rehearse_aggregate_publication(
                rehearsal_aggregate,
                runtime_root / "aggregate-publication-rehearsal",
            )
            ended_ns = max(time.monotonic_ns(), started_ns + 1)
            finished_utc = _utc_second_now()
            aggregate, warnings, failures = _build_performance_aggregate(
                rows=rows,
                auxiliaries=auxiliary_publications,
                auxiliary_results=auxiliary_results,
                inputs=inputs,
                start_monotonic_ns=started_ns,
                end_monotonic_ns=ended_ns,
            )
            aggregate_raw = aggregate.canonical_bytes()
            _publish_relative(
                root_descriptor,
                RELEASE_PERFORMANCE_AGGREGATE_PATH_V1,
                aggregate_raw,
                admit_identical=False,
            )
            aggregate_reference = release_performance_reference(
                "performance-aggregate",
                "AGGREGATE",
                RELEASE_PERFORMANCE_AGGREGATE_PATH_V1,
                aggregate_raw,
            )
            attempt_id = f"wo40i-{secrets.token_hex(12)}"
            attempt = ReleasePerformanceAttemptRecordV1(
                attempt_id=attempt_id,
                status=aggregate.status,
                candidate_commit=inputs.candidate_commit,
                source_manifest_sha256=inputs.source_manifest_sha256,
                protocol_set_sha256=inputs.protocol_set_sha256,
                artifact_index_sha256=inputs.artifact_index_sha256,
                build_evidence_sha256=inputs.build_evidence_sha256,
                threshold_manifest_sha256=inputs.threshold_manifest_sha256,
                runner_source_lock_sha256=inputs.runner_source_lock_sha256,
                row_corpus_sha256=inputs.row_corpus_sha256,
                target_id="macos-arm64",
                environment=prepared.environment,
                started_at_utc=started_utc,
                finished_at_utc=finished_utc,
                start_monotonic_ns=started_ns,
                end_monotonic_ns=ended_ns,
                worker_count=RELEASE_PERFORMANCE_WORKER_COUNT_V1,
                queue_size=RELEASE_PERFORMANCE_QUEUE_SIZE_V1,
                work_units=tuple(item.publication for item in rows),
                auxiliaries=auxiliary_publications,
                aggregate_record=aggregate_reference,
                warning_codes=warnings,
                failure_codes=failures,
            )
            attempt_raw = attempt.canonical_bytes()
            _publish_relative(
                root_descriptor,
                RELEASE_PERFORMANCE_ATTEMPT_PATH_V1,
                attempt_raw,
                admit_identical=False,
            )
            attempt_reference = release_performance_reference(
                "performance-attempt",
                "ATTEMPT",
                RELEASE_PERFORMANCE_ATTEMPT_PATH_V1,
                attempt_raw,
            )
            activation = ReleasePerformanceActivationRecordV1(
                status=aggregate.status,
                candidate_commit=inputs.candidate_commit,
                source_manifest_sha256=inputs.source_manifest_sha256,
                protocol_set_sha256=inputs.protocol_set_sha256,
                artifact_index_sha256=inputs.artifact_index_sha256,
                build_evidence_sha256=inputs.build_evidence_sha256,
                threshold_manifest_sha256=inputs.threshold_manifest_sha256,
                runner_source_lock_sha256=inputs.runner_source_lock_sha256,
                attempt_id=attempt_id,
                attempt_record=attempt_reference,
                aggregate_record=aggregate_reference,
                work_unit_count=aggregate.work_unit_count,
                complete_work_unit_count=aggregate.complete_work_unit_count,
                auxiliary_result_count=aggregate.auxiliary_result_count,
                activated_at_utc=_utc_second_now(),
            )
            _publish_relative(
                root_descriptor,
                RELEASE_PERFORMANCE_ACTIVATION_PATH_V1,
                activation.canonical_bytes(),
                admit_identical=False,
            )
        finally:
            os.close(root_descriptor)
        staged_verification = verify_release_performance_records(
            staging_root,
            inputs=inputs,
        )
        staged_checked_ns = time.monotonic_ns()
        if (
            staged_checked_ns - started_ns > RELEASE_TOTAL_WALL_LIMIT_NS_V1
            and aggregate.total_wall_ns <= RELEASE_TOTAL_WALL_LIMIT_NS_V1
        ):
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.TOTAL_LIMIT,
                "aggregate publication or deep verification crossed the 36-hour limit",
            )
        # The exclusive rename is activation of bytes whose terminal status has
        # already been decided and deeply verified.  It is deliberately after the
        # last total-limit decision: returning a new timing refusal after this
        # one-time move would strand an active PASS publication that cannot be
        # retried.  The frozen measured lane ends at aggregate commit/reread, not
        # at this store-location change.
        _activate_staged_publication(staging_root, prepared.artifact_root)
        active_verification = verify_release_performance_records(
            prepared.artifact_root,
            inputs=inputs,
        )
        if staged_verification != active_verification:
            raise _PerformanceRefused(
                PerformanceExecutorRefusalCodeV1.VERIFICATION_FAILED,
                "WO40-I evidence changed during atomic activation",
            )
    status = {
        "PASS": ReleaseCommandStatusV1.PASS,
        "PASS_WITH_WARNINGS": ReleaseCommandStatusV1.PASS_WITH_WARNINGS,
        "FAIL": ReleaseCommandStatusV1.FAIL,
    }[active_verification.status]
    return _performance_outcome(
        bundle,
        status=status,
        detail=(
            "executed and deeply verified the closed WO40-I performance qualification"
        ),
        payload={
            "activation_sha256": active_verification.activation_sha256,
            "aggregate_artifact_bytes": active_verification.aggregate_artifact_bytes,
            "aggregate_sha256": active_verification.aggregate_sha256,
            "artifact_index_sha256": active_verification.artifact_index_sha256,
            "attempt_id": active_verification.attempt_id,
            "attempt_sha256": active_verification.attempt_sha256,
            "auxiliary_result_count": active_verification.auxiliary_result_count,
            "candidate_commit": active_verification.candidate_commit,
            "cas_object_count": active_verification.cas_object_count,
            "complete_run_work_units": active_verification.complete_work_unit_count,
            "publication_root": "gate-evidence/wo40-i",
            "retry_count": active_verification.retry_count,
            "source_manifest_sha256": active_verification.source_manifest_sha256,
            "work_unit_count": active_verification.work_unit_count,
        },
    )


def execute_release_performance_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    manifest: Path,
    complete_run_work_units: int,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Execute the one closed WO40-I attempt and activate verified bytes once."""

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("performance execution requires the exact protocol bundle")
    try:
        return _execute_release_performance_qualification(
            bundle,
            manifest=manifest,
            complete_run_work_units=complete_run_work_units,
            build_evidence=build_evidence,
            artifact_root=artifact_root,
        )
    except _PerformanceRefused as error:
        return _performance_outcome(
            bundle,
            status=ReleaseCommandStatusV1.REFUSED,
            detail=error.detail,
            refusal_code=error.code,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        return _performance_outcome(
            bundle,
            status=ReleaseCommandStatusV1.REFUSED,
            detail=f"performance qualification verification failed: {error}",
            refusal_code=PerformanceExecutorRefusalCodeV1.VERIFICATION_FAILED,
        )


__all__ = [
    "PERFORMANCE_EXECUTION_POLICY_V1",
    "PerformanceExecutorRefusalCodeV1",
    "execute_release_performance_qualification",
]
