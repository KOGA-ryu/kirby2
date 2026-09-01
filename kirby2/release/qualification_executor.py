"""Closed Tart host executor for the macOS release qualification.

This module is deliberately not a generic VM or remote-command adapter.  It owns
one exact provider, one exact target, two exact artifact forms, and a fixed guest
worker grammar.  In particular, it never falls back from Tart's host-only network
mode to the default NAT device.

The guest worker is shipped in the installed project wheel.  Checkout Python never
enters either clone.  The host deeply verifies the immutable release artifacts,
copies only the selected transports into owner-only read-only shares, installs them
with ``pip --no-index``, and captures the installed worker's canonical result.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Final, Mapping, Sequence

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
)

from .artifacts import (
    RELEASE_ARTIFACT_INDEX_FILENAME_V1,
    RELEASE_ARTIFACT_MAX_BYTES_V1,
    RELEASE_BUILD_RECORD_FILENAME_V1,
    RELEASE_RECORD_MAX_BYTES_V1,
    ReleaseArtifactBuildRecordV1,
)
from .build import (
    ReleaseCleanProviderInventoryV1,
    ReleaseCleanProviderV1,
    ReleaseCommandOutcomeV1,
    ReleaseCommandStatusV1,
    ReleaseProtocolBundleV1,
    verify_release_artifacts,
)
from .manifest import RELEASE_VERSION_V1, ReleaseArtifactIndexV1
from .qualification import (
    ReleaseBuildEvidenceBindingV1,
    _require_canonical_tracked_build_evidence,
    verify_release_qualification,
)
from .qualification_records import (
    RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1,
    RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
    RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
    ReleaseCleanProviderAttestationV1,
    ReleaseQualificationArtifactBindingV1,
    ReleaseQualificationAttemptV1,
    ReleaseQualificationCommandObservationV1,
    ReleaseQualificationFactsV1,
    ReleaseQualificationNetworkScopeV1,
    ReleaseQualificationRootObservationV1,
    ReleaseQualificationSessionV1,
    ReleaseQualificationStepObservationV1,
    build_release_qualification_attempt_record,
    release_qualification_record_paths,
    verify_release_qualification_record,
)


TART_EXECUTABLE_V1: Final[Path] = Path(
    "/opt/homebrew/Cellar/tart/2.32.1/bin/tart"
)
TART_VERSION_V1: Final[str] = "2.32.1"
TART_EXECUTABLE_SHA256_V1: Final[str] = (
    "44137d8dba251d4a4f9a113ecc8619d821ea7ea0f28217a88f57dde894d83d76"
)
TART_EXECUTABLE_MODE_V1: Final[int] = 0o555
TART_BASE_VM_V1: Final[str] = "kirby2-dev0014-macos-offline-base-hardened"
TART_BASE_CONFIG_SHA256_V1: Final[str] = (
    "1f1175a3731ab4fbb1beff6990775fdbfb00931a10c7d43be87915a766699d8c"
)
TART_BASE_NVRAM_SHA256_V1: Final[str] = (
    "69078eae7ddf3193411d98f27931674a6abfd75fca229f082591525eddea8387"
)
TART_BASE_DISK_BYTES_V1: Final[int] = 80_000_000_000
TART_BASE_DISK_MODE_V1: Final[int] = 0o644
TART_BASE_DISK_SHA256_V1: Final[str] = (
    "fa88c6aae58badcf38943ee2c85cf9070d0a79ceff801c2114a9767f82f572a3"
)
TART_PROVIDER_POLICY_ID_V1: Final[str] = (
    "KIRBY2_TART_HOST_ONLY_HARDENED_PROVIDER_V1"
)
TART_TARGET_ID_V1: Final[str] = "macos-arm64"

_FORMS: Final[tuple[str, ...]] = ("desktop", "headless")
_ARTIFACT_SELECTORS: Final[Mapping[str, str]] = {
    "desktop": "macos-arm64/desktop",
    "headless": "macos-arm64/headless",
}
_BUNDLE_ARTIFACTS: Final[Mapping[str, str]] = {
    "desktop": "macos-arm64-desktop-bundle",
    "headless": "macos-arm64-wheelhouse",
}
_BUNDLE_ROOTS: Final[Mapping[str, str]] = {
    "desktop": f"kirby2-{RELEASE_VERSION_V1}-macos-arm64",
    "headless": f"kirby2-{RELEASE_VERSION_V1}-macos-arm64-wheelhouse",
}
_INSTALLED_LAUNCHERS: Final[Mapping[str, str]] = {
    "desktop": "kirby2-desktop",
    "headless": "kirby2-headless",
}
_GUEST_SHARE_ROOT: Final[PurePosixPath] = PurePosixPath(
    "/Volumes/My Shared Files/release"
)
_GUEST_SHARE_MOUNT_ROOT: Final[PurePosixPath] = _GUEST_SHARE_ROOT.parent
_GUEST_PYTHON_CANDIDATES: Final[tuple[str, ...]] = (
    "/opt/homebrew/bin/python3.14",
    "/usr/local/bin/python3.14",
    "/usr/bin/python3",
)
_CLONE_NAME = re.compile(
    r"kirby2-wo40g-[0-9a-f]{12}-(?:desktop|headless)-[0-9a-f]{16}\Z"
)
_GUEST_HOME = re.compile(r"/Users/[A-Za-z0-9._-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORKER_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_WORKER_FAILURE_DETAIL_MAX_BYTES_V1: Final = 3072
_WORKER_SCHEMA_ID_V1: Final[str] = (
    "KIRBY2_RELEASE_QUALIFICATION_WORKER_RESULT_V1"
)
_WORKER_EXECUTION_POLICY_ID_V1: Final[str] = (
    "KIRBY2_WO40_GH_INSTALLED_EXECUTION_POLICY_V1"
)
_PROJECT_WHEEL_V1: Final[str] = f"kirby2-{RELEASE_VERSION_V1}-py3-none-any.whl"

_HOST_COMMAND_TIMEOUT_SECONDS: Final[int] = 600
_GUEST_BOOT_TIMEOUT_SECONDS: Final[int] = 300
_GUEST_COMMAND_TIMEOUT_SECONDS: Final[int] = 120
_WORKER_TIMEOUT_SECONDS: Final[int] = 3_600
_COMMAND_OUTPUT_MAX_BYTES: Final[int] = 16 * 1024 * 1024
_WORKER_OUTPUT_MAX_BYTES: Final[int] = 32 * 1024 * 1024
_BUILD_EVIDENCE_MAX_BYTES: Final[int] = 16 * 1024 * 1024
_PROVIDER_DIGEST_CHUNK_BYTES: Final[int] = 8 * 1024 * 1024
_MINIMUM_GUEST_FREE_BYTES: Final[int] = 20 * 1024 * 1024 * 1024
_MINIMUM_HOST_FREE_BYTES: Final[int] = 40 * 1024 * 1024 * 1024


class QualificationExecutorRefusalCodeV1(str, Enum):
    """Closed refusal vocabulary for the host provider boundary."""

    TARGET_UNSUPPORTED = "QUALIFICATION_TARGET_UNSUPPORTED"
    INPUT_INVALID = "QUALIFICATION_INPUT_INVALID"
    ARTIFACT_VERIFICATION_FAILED = "QUALIFICATION_ARTIFACT_VERIFICATION_FAILED"
    PRIOR_ATTEMPT_EXISTS = "QUALIFICATION_PRIOR_ATTEMPT_EXISTS"
    PROVIDER_UNAVAILABLE = "QUALIFICATION_PROVIDER_UNAVAILABLE"
    PROVIDER_IDENTITY_MISMATCH = "QUALIFICATION_PROVIDER_IDENTITY_MISMATCH"
    PROVIDER_ISOLATION_UNAVAILABLE = "QUALIFICATION_PROVIDER_ISOLATION_UNAVAILABLE"
    PROVIDER_NOT_CLEAN = "QUALIFICATION_PROVIDER_NOT_CLEAN"
    PROVIDER_EXECUTION_FAILED = "QUALIFICATION_PROVIDER_EXECUTION_FAILED"
    PROVIDER_CLEANUP_FAILED = "QUALIFICATION_PROVIDER_CLEANUP_FAILED"
    RESULT_INVALID = "QUALIFICATION_RESULT_INVALID"
    PUBLICATION_CONFLICT = "QUALIFICATION_PUBLICATION_CONFLICT"


class _QualificationRefused(RuntimeError):
    def __init__(
        self,
        code: QualificationExecutorRefusalCodeV1,
        detail: str,
        *,
        terminal: bool = False,
    ) -> None:
        self.code = code
        self.detail = detail
        self.terminal = terminal
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    raw: bytes
    identity: tuple[int, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True, slots=True)
class _FileDigest:
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ns: int
    timed_out: bool
    output_exceeded: bool

    def observation(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "duration_ns": self.duration_ns,
            "returncode": self.returncode,
            "stderr_bytes": len(self.stderr),
            "stderr_sha256": hashlib.sha256(self.stderr).hexdigest(),
            "stdout_bytes": len(self.stdout),
            "stdout_sha256": hashlib.sha256(self.stdout).hexdigest(),
        }


class _PipePump:
    def __init__(self, stream, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self.exceeded = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(64 * 1024)
                if not chunk:
                    break
                with self._lock:
                    remaining = self._maximum_bytes - len(self._buffer)
                    if remaining > 0:
                        self._buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self.exceeded.set()
        finally:
            self._stream.close()

    def value(self) -> bytes:
        with self._lock:
            return bytes(self._buffer)


@dataclass(slots=True)
class _ManagedProcess:
    argv: tuple[str, ...]
    process: subprocess.Popen[bytes]
    stdout_pump: _PipePump
    stderr_pump: _PipePump
    started_ns: int

    @property
    def output_exceeded(self) -> bool:
        return self.stdout_pump.exceeded.is_set() or self.stderr_pump.exceeded.is_set()

    def poll(self) -> int | None:
        return self.process.poll()

    def finish(self, *, timed_out: bool = False) -> _CommandResult:
        returncode = self.process.wait()
        self.stdout_pump.thread.join(timeout=5)
        self.stderr_pump.thread.join(timeout=5)
        if self.stdout_pump.thread.is_alive() or self.stderr_pump.thread.is_alive():
            raise RuntimeError("provider command output pumps did not terminate")
        return _CommandResult(
            argv=self.argv,
            returncode=returncode,
            stdout=self.stdout_pump.value(),
            stderr=self.stderr_pump.value(),
            duration_ns=time.monotonic_ns() - self.started_ns,
            timed_out=timed_out,
            output_exceeded=self.output_exceeded,
        )


@dataclass(slots=True)
class _CloneState:
    form: str
    name: str
    projection_root: Path
    input_rows: tuple[dict[str, object], ...]
    created: bool = False
    deleted: bool = False
    run_process: _ManagedProcess | None = None
    provider_proofs: list[dict[str, object]] = field(default_factory=list)
    worker_result: dict[str, object] | None = None


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_read(path: Path, *, maximum_bytes: int, require_read_only: bool) -> _FileSnapshot:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("qualification input path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow input support")
    descriptor = os.open(path, flags | nofollow)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
        ):
            raise ValueError(f"qualification input is not one regular single-link file: {path.name}")
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise ValueError(f"qualification input size is outside its bound: {path.name}")
        if before.st_mode & 0o022:
            raise ValueError(f"qualification input is group/world writable: {path.name}")
        if require_read_only and stat.S_IMODE(before.st_mode) != 0o444:
            raise ValueError(f"immutable qualification input is writable: {path.name}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or _identity(before) != _identity(after):
            raise ValueError(f"qualification input changed while read: {path.name}")
        return _FileSnapshot(raw=raw, identity=_identity(before))
    finally:
        os.close(descriptor)


def _stable_file_digest(path: Path, *, expected_bytes: int) -> _FileDigest:
    """Stream one exact regular file while proving its identity stayed stable."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("qualification digest path must be absolute")
    if type(expected_bytes) is not int or expected_bytes <= 0:
        raise ValueError("qualification digest size must be one positive integer")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow input support")
    descriptor = os.open(path, flags | nofollow)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or before.st_size != expected_bytes
            or before.st_mode & 0o022
        ):
            raise ValueError(
                f"qualification digest input differs from its fixed identity: {path.name}"
            )
        digest = hashlib.sha256()
        observed_bytes = 0
        while observed_bytes < expected_bytes:
            chunk = os.read(
                descriptor,
                min(_PROVIDER_DIGEST_CHUNK_BYTES, expected_bytes - observed_bytes),
            )
            if not chunk:
                break
            digest.update(chunk)
            observed_bytes += len(chunk)
        after = os.fstat(descriptor)
        if observed_bytes != expected_bytes or _identity(before) != _identity(after):
            raise ValueError(f"qualification digest input changed while read: {path.name}")
        return _FileDigest(sha256=digest.hexdigest(), identity=_identity(before))
    finally:
        os.close(descriptor)


def _write_private(path: Path, raw: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow publication support")
    descriptor = os.open(path, flags | nofollow, mode)
    try:
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short qualification projection write")
            view = view[count:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tart_environment() -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir).resolve()
    if home != Path.home().resolve():
        raise ValueError("Tart account home differs from the active account")
    return {
        "HOME": os.fspath(home),
        "LANG": "C",
        "LC_ALL": "C",
        "LOGNAME": account.pw_name,
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TART_NO_AUTO_PRUNE": "1",
        "TMPDIR": tempfile.gettempdir(),
        "USER": account.pw_name,
    }


def _spawn_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
) -> _ManagedProcess:
    if (
        not argv
        or any(type(item) is not str or not item or "\x00" in item for item in argv)
        or any(type(key) is not str or type(value) is not str for key, value in environment.items())
    ):
        raise TypeError("provider command requires nonempty NUL-free argv and environment")
    started = time.monotonic_ns()
    process = subprocess.Popen(
        tuple(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        raise RuntimeError("provider command omitted captured pipes")
    stdout_pump = _PipePump(process.stdout, maximum_output_bytes)
    stderr_pump = _PipePump(process.stderr, maximum_output_bytes)
    stdout_pump.start()
    stderr_pump.start()
    return _ManagedProcess(
        argv=tuple(argv),
        process=process,
        stdout_pump=stdout_pump,
        stderr_pump=stderr_pump,
        started_ns=started,
    )


def _terminate_process(process: _ManagedProcess) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_command(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
    timeout_is_result: bool = False,
) -> _CommandResult:
    if type(timeout_is_result) is not bool:
        raise TypeError("provider timeout policy must be Boolean")
    process = _spawn_command(
        argv,
        environment=environment,
        maximum_output_bytes=maximum_output_bytes,
    )
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if process.output_exceeded:
            _terminate_process(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process(process)
            break
        time.sleep(0.05)
    result = process.finish(timed_out=timed_out)
    if result.output_exceeded:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "provider command exceeded its bounded output allowance",
            terminal=True,
        )
    if result.timed_out and not timeout_is_result:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "provider command exceeded its wall-time allowance",
            terminal=True,
        )
    return result


def _tart_command(*arguments: str) -> tuple[str, ...]:
    return (os.fspath(TART_EXECUTABLE_V1), *arguments)


def _run_tart(
    *arguments: str,
    timeout_seconds: int = _HOST_COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
    timeout_is_result: bool = False,
) -> _CommandResult:
    return _run_command(
        _tart_command(*arguments),
        environment=_tart_environment(),
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
        timeout_is_result=timeout_is_result,
    )


def _decode_text(raw: bytes, label: str, *, maximum_bytes: int = 1024 * 1024) -> str:
    if len(raw) > maximum_bytes:
        raise ValueError(f"{label} exceeds its text bound")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error
    if "\x00" in value:
        raise ValueError(f"{label} contains NUL")
    return value


def _diagnostic_excerpt(raw: bytes, label: str) -> str:
    value = _decode_text(raw, label, maximum_bytes=_COMMAND_OUTPUT_MAX_BYTES)
    printable = "".join(
        character if 0x20 <= ord(character) <= 0x7E else " "
        for character in value
    )
    return " ".join(printable.split())[:512]


def _local_vms() -> dict[str, dict[str, object]]:
    result = _run_tart("list", "--source", "local", "--format", "json", timeout_seconds=30)
    if result.returncode != 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "Tart local VM inventory could not be read",
        )
    try:
        payload = json.loads(_decode_text(result.stdout, "Tart VM inventory"))
    except json.JSONDecodeError as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "Tart local VM inventory is not JSON",
        ) from error
    if type(payload) is not list or any(type(item) is not dict for item in payload):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "Tart local VM inventory has an unexpected shape",
        )
    by_name: dict[str, dict[str, object]] = {}
    for item in payload:
        name = item.get("Name")
        if type(name) is not str or name in by_name:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
                "Tart local VM inventory has ambiguous names",
            )
        by_name[name] = dict(item)
    return by_name


def _require_stopped_vm(name: str) -> dict[str, object]:
    item = _local_vms().get(name)
    if (
        item is None
        or item.get("Source") != "local"
        or item.get("Running") is not False
        or item.get("State") != "stopped"
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            f"required stopped local Tart VM is unavailable: {name}",
        )
    return item


def _vm_directory(name: str) -> Path:
    if name != TART_BASE_VM_V1 and _CLONE_NAME.fullmatch(name) is None:
        raise ValueError("VM name is outside the qualification ownership grammar")
    return Path.home().resolve() / ".tart" / "vms" / name


def _vm_config(name: str) -> tuple[dict[str, object], str]:
    snapshot = _stable_read(
        _vm_directory(name) / "config.json",
        maximum_bytes=1024 * 1024,
        require_read_only=False,
    )
    try:
        value = json.loads(snapshot.raw)
    except json.JSONDecodeError as error:
        raise ValueError("Tart VM configuration is not JSON") from error
    if type(value) is not dict:
        raise ValueError("Tart VM configuration is not an object")
    return dict(value), snapshot.sha256


def _redacted_vm_projection(config: Mapping[str, object]) -> dict[str, object]:
    fields = {
        "arch",
        "cpuCount",
        "cpuCountMin",
        "diskFormat",
        "display",
        "ecid",
        "hardwareModel",
        "memorySize",
        "memorySizeMin",
        "os",
        "version",
    }
    if not fields.issubset(config):
        raise ValueError("Tart VM configuration omits fixed provider fields")
    return {field: config[field] for field in sorted(fields)}


def _require_tart_provider() -> dict[str, object]:
    try:
        executable = _stable_read(
            TART_EXECUTABLE_V1,
            maximum_bytes=256 * 1024 * 1024,
            require_read_only=False,
        )
    except (OSError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Tart executable could not be verified as one pinned regular file",
        ) from error
    if (
        executable.sha256 != TART_EXECUTABLE_SHA256_V1
        or stat.S_IMODE(executable.identity[2]) != TART_EXECUTABLE_MODE_V1
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Tart executable digest or non-writable executable mode differs from the fixed provider",
        )
    version = _run_tart("--version", timeout_seconds=30)
    if version.returncode != 0 or _decode_text(version.stdout, "Tart version").strip() != TART_VERSION_V1:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "Tart runtime version differs from 2.32.1",
        )
    _require_stopped_vm(TART_BASE_VM_V1)
    base = _vm_directory(TART_BASE_VM_V1)
    try:
        config, config_sha256 = _vm_config(TART_BASE_VM_V1)
        nvram = _stable_read(
            base / "nvram.bin",
            maximum_bytes=128 * 1024 * 1024,
            require_read_only=False,
        )
        disk = _stable_file_digest(
            base / "disk.img",
            expected_bytes=TART_BASE_DISK_BYTES_V1,
        )
        disk_metadata = os.lstat(base / "disk.img")
    except (OSError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "offline Tart base files differ from the fixed provider",
        ) from error
    if (
        config_sha256 != TART_BASE_CONFIG_SHA256_V1
        or nvram.sha256 != TART_BASE_NVRAM_SHA256_V1
        or disk.sha256 != TART_BASE_DISK_SHA256_V1
        or _identity(disk_metadata) != disk.identity
        or not stat.S_ISREG(disk_metadata.st_mode)
        or disk_metadata.st_nlink != 1
        or disk_metadata.st_size != TART_BASE_DISK_BYTES_V1
        or stat.S_IMODE(disk_metadata.st_mode) != TART_BASE_DISK_MODE_V1
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "offline Tart base identity differs from the fixed provider",
        )
    projection = _redacted_vm_projection(config)
    if (
        projection.get("arch") != "arm64"
        or projection.get("os") != "darwin"
        or projection.get("cpuCount") != 4
        or projection.get("memorySize") != 12 * 1024 * 1024 * 1024
        or projection.get("diskFormat") != "raw"
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "offline Tart base capability projection differs",
        )
    free = os.statvfs(base)
    free_bytes = free.f_bavail * free.f_frsize
    if free_bytes < _MINIMUM_HOST_FREE_BYTES:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "host storage is insufficient for two disposable qualification clones",
        )
    return {
        "base_config_projection": projection,
        "base_config_sha256": config_sha256,
        "base_disk_bytes": disk_metadata.st_size,
        "base_disk_mode": stat.S_IMODE(disk_metadata.st_mode),
        "base_disk_sha256": disk.sha256,
        "base_nvram_sha256": nvram.sha256,
        "base_vm": TART_BASE_VM_V1,
        "host_free_bytes": free_bytes,
        "tart_executable_sha256": executable.sha256,
        "tart_version": TART_VERSION_V1,
    }


def _absolute_input(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            f"{label} must be an absolute Path",
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            f"{label} cannot be resolved",
        ) from error
    if resolved != path:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            f"{label} must be supplied in resolved form",
        )
    return resolved


def _open_artifact_store(root: Path) -> tuple[int, tuple[int, ...]]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow directory support")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | nofollow,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        os.close(descriptor)
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            "release artifact store ownership or permissions are unsafe",
        )
    return descriptor, _identity(metadata)


def _require_store_identity(root: Path, expected: tuple[int, ...]) -> None:
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode) or _identity(metadata) != expected:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            "release artifact store identity changed during qualification",
            terminal=True,
        )


def _record_target_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RuntimeError("qualification record path escaped the governed store")
    return root.joinpath(*path.parts)


def _require_no_prior_records(root: Path, target_id: str) -> tuple[Path, Path]:
    provider_relative, attempt_relative = release_qualification_record_paths(
        target_id
    )
    provider = _record_target_path(root, provider_relative)
    attempt = _record_target_path(root, attempt_relative)
    for path in (provider, attempt):
        if path.exists() or path.is_symlink():
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PRIOR_ATTEMPT_EXISTS,
                "immutable provider or qualification-attempt evidence already exists "
                f"for {target_id}",
            )
    return provider, attempt


def _copy_projection_file(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    require_read_only: bool,
) -> dict[str, object]:
    snapshot = _stable_read(
        source,
        maximum_bytes=maximum_bytes,
        require_read_only=require_read_only,
    )
    _write_private(destination, snapshot.raw, mode=0o444)
    copied = _stable_read(
        destination,
        maximum_bytes=maximum_bytes,
        require_read_only=True,
    )
    if copied.raw != snapshot.raw:
        raise ValueError(f"qualification projection copy differs: {destination.name}")
    return {
        "name": destination.name,
        "sha256": snapshot.sha256,
        "size": len(snapshot.raw),
        "source_identity": list(snapshot.identity),
    }


def _build_projection(
    *,
    form: str,
    artifact_root: Path,
    build_evidence: Path,
    index: ReleaseArtifactIndexV1,
    build_record: ReleaseArtifactBuildRecordV1,
) -> tuple[Path, tuple[dict[str, object], ...]]:
    if form not in _FORMS:
        raise ValueError("qualification projection form is invalid")
    selected = index.select(_ARTIFACT_SELECTORS[form])
    projection = Path(
        tempfile.mkdtemp(prefix=f"kirby2-wo40g-{form}-input-")
    ).resolve()
    os.chmod(projection, 0o700)
    if ":" in os.fspath(projection):
        shutil.rmtree(projection)
        raise ValueError("Tart share path contains an unsupported colon")
    rows: list[dict[str, object]] = []
    try:
        for artifact in selected:
            row = _copy_projection_file(
                artifact_root / artifact.artifact_id,
                projection / artifact.artifact_id,
                maximum_bytes=RELEASE_ARTIFACT_MAX_BYTES_V1,
                require_read_only=True,
            )
            if row["size"] != artifact.size or row["sha256"] != artifact.transport_sha256:
                raise ValueError(
                    f"selected qualification artifact differs from index: {artifact.artifact_id}"
                )
            rows.append({"artifact_id": artifact.artifact_id, **row})
        index_row = _copy_projection_file(
            artifact_root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            projection / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        if index_row["sha256"] != index.sha256:
            raise ValueError("qualification projection artifact index differs")
        rows.append({"artifact_id": "release-artifact-index", **index_row})
        build_row = _copy_projection_file(
            artifact_root / RELEASE_BUILD_RECORD_FILENAME_V1,
            projection / RELEASE_BUILD_RECORD_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        if build_row["sha256"] != build_record.sha256:
            raise ValueError("qualification projection build record differs")
        rows.append({"artifact_id": "release-build-record", **build_row})
        evidence_row = _copy_projection_file(
            build_evidence,
            projection / "KIRBY2_RELEASE_BUILD_EVIDENCE.md",
            maximum_bytes=_BUILD_EVIDENCE_MAX_BYTES,
            require_read_only=False,
        )
        rows.append({"artifact_id": "release-build-evidence", **evidence_row})
        request = canonical_json_bytes(
            {
                "artifact_index_sha256": index.sha256,
                "candidate_commit": index.candidate_commit,
                "form": form,
                "inputs": [
                    {
                        "artifact_id": row["artifact_id"],
                        "name": row["name"],
                        "sha256": row["sha256"],
                        "size": row["size"],
                    }
                    for row in rows
                ],
                "logical_build_id": index.logical_build_id,
                "network_scope": "HOST_ONLY",
                "policy_id": TART_PROVIDER_POLICY_ID_V1,
                "protocol_set_sha256": build_record.protocol_set_sha256,
                "schema_id": "KIRBY2_TART_QUALIFICATION_INPUT_V1",
                "schema_version": 1,
                "target_id": TART_TARGET_ID_V1,
            }
        )
        _write_private(projection / "qualification-request.json", request, mode=0o444)
        rows.append(
            {
                "artifact_id": "qualification-request",
                "name": "qualification-request.json",
                "sha256": _sha256(request),
                "size": len(request),
                "source_identity": [],
            }
        )
        _fsync_directory(projection)
        return projection, tuple(rows)
    except Exception:
        shutil.rmtree(projection, ignore_errors=True)
        raise


def _clone_name(candidate_commit: str, form: str) -> str:
    value = f"kirby2-wo40g-{candidate_commit[:12]}-{form}-{secrets.token_hex(8)}"
    if _CLONE_NAME.fullmatch(value) is None:
        raise RuntimeError("generated qualification clone name is invalid")
    return value


def _create_clone(state: _CloneState, base_projection: Mapping[str, object]) -> None:
    if state.name in _local_vms():
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "generated qualification clone name already exists",
        )
    result = _run_tart(
        "clone",
        TART_BASE_VM_V1,
        state.name,
        timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
            "Tart could not create a disposable qualification clone",
        )
    state.created = True
    _require_stopped_vm(state.name)
    clone = _vm_directory(state.name)
    try:
        config, config_sha256 = _vm_config(state.name)
        projection = _redacted_vm_projection(config)
        nvram = _stable_read(
            clone / "nvram.bin",
            maximum_bytes=128 * 1024 * 1024,
            require_read_only=False,
        )
        disk = _stable_file_digest(
            clone / "disk.img",
            expected_bytes=TART_BASE_DISK_BYTES_V1,
        )
        disk_metadata = os.lstat(clone / "disk.img")
    except (OSError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "qualification clone files differ from the fixed provider",
        ) from error
    if (
        projection != base_projection
        or nvram.sha256 != TART_BASE_NVRAM_SHA256_V1
        or disk.sha256 != TART_BASE_DISK_SHA256_V1
        or _identity(disk_metadata) != disk.identity
        or stat.S_IMODE(disk_metadata.st_mode) != TART_BASE_DISK_MODE_V1
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "qualification clone identity differs from the fixed offline base",
        )
    state.provider_proofs.append(
        {
            "clone_config_projection_sha256": _sha256(canonical_json_bytes(projection)),
            "clone_config_sha256": config_sha256,
            "clone_disk_sha256": disk.sha256,
            "clone_name": state.name,
            "clone_nvram_sha256": nvram.sha256,
            "clone_operation": result.observation(),
            "form": state.form,
        }
    )


def _spawn_host_only_vm(state: _CloneState) -> None:
    if not state.created or state.run_process is not None:
        raise RuntimeError("qualification clone lifecycle is invalid")
    share = f"release:{state.projection_root}:ro"
    state.run_process = _spawn_command(
        _tart_command(
            "run",
            "--no-graphics",
            "--no-audio",
            "--no-clipboard",
            "--net-host",
            "--root-disk-opts=sync=full",
            f"--dir={share}",
            state.name,
        ),
        environment=_tart_environment(),
        maximum_output_bytes=_COMMAND_OUTPUT_MAX_BYTES,
    )


def _wait_for_guest_agent(state: _CloneState) -> None:
    process = state.run_process
    if process is None:
        raise RuntimeError("qualification clone was not started")
    deadline = time.monotonic() + _GUEST_BOOT_TIMEOUT_SECONDS
    last_result: _CommandResult | None = None
    while time.monotonic() < deadline:
        if process.output_exceeded:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_ISOLATION_UNAVAILABLE,
                "host-only Tart launch exceeded bounded diagnostics",
            )
        if process.poll() is not None:
            result = process.finish()
            state.run_process = None
            detail = _diagnostic_excerpt(
                result.stderr,
                "host-only Tart launch diagnostics",
            )
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_ISOLATION_UNAVAILABLE,
                "true Tart --net-host launch is unavailable"
                + (f": {detail[:512]}" if detail else ""),
            )
        last_result = _run_tart(
            "exec",
            state.name,
            "/usr/bin/true",
            timeout_seconds=10,
            maximum_output_bytes=1024 * 1024,
            timeout_is_result=True,
        )
        if last_result.returncode == 0:
            return
        time.sleep(0.5)
    detail = "guest agent did not become ready under true host-only isolation"
    if last_result is not None and last_result.stderr:
        excerpt = _diagnostic_excerpt(last_result.stderr, "guest-agent diagnostics")
        if excerpt:
            detail += ": " + excerpt
    raise _QualificationRefused(
        QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
        detail,
    )


def _utc_second() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _guest_exec(
    state: _CloneState,
    guest_home: str,
    *command: str,
    timeout_seconds: int = _GUEST_COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
) -> _CommandResult:
    if _GUEST_HOME.fullmatch(guest_home) is None:
        raise ValueError("guest home is outside the closed macOS provider grammar")
    environment = (
        "HOME=" + guest_home,
        "LANG=C",
        "LC_ALL=C",
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "PIP_NO_INDEX=1",
        "PIP_NO_INPUT=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONNOUSERSITE=1",
        "TMPDIR=/private/var/tmp",
        "TZ=UTC",
    )
    return _run_tart(
        "exec",
        state.name,
        "/usr/bin/env",
        "-i",
        *environment,
        *command,
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
    )


def _guest_plain_exec(
    state: _CloneState,
    *command: str,
    timeout_seconds: int = _GUEST_COMMAND_TIMEOUT_SECONDS,
    maximum_output_bytes: int = _COMMAND_OUTPUT_MAX_BYTES,
) -> _CommandResult:
    return _run_tart(
        "exec",
        state.name,
        *command,
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
    )


def _require_guest_success(
    result: _CommandResult,
    label: str,
    *,
    terminal: bool = False,
) -> _CommandResult:
    if result.returncode != 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"guest {label} failed with exit status {result.returncode}",
            terminal=terminal,
        )
    return result


def _guest_text(result: _CommandResult, label: str) -> str:
    return _decode_text(result.stdout, f"guest {label}").strip()


def _discover_guest_home(state: _CloneState) -> str:
    result = _require_guest_success(
        _guest_plain_exec(state, "/usr/bin/printenv", "HOME"),
        "home discovery",
    )
    home = _guest_text(result, "home discovery")
    if _GUEST_HOME.fullmatch(home) is None:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "guest-agent account home is outside /Users",
        )
    return home


def _guest_python_probe(
    state: _CloneState,
    guest_home: str,
) -> tuple[str, dict[str, object], _CommandResult]:
    script = (
        "import json,os,platform,sys;"
        "p=sys.executable;"
        "r={'candidate_realpath':os.path.realpath(sys.argv[1]),"
        "'executable':p,'executable_realpath':os.path.realpath(p),"
        "'implementation':platform.python_implementation(),"
        "'machine':platform.machine(),'system':platform.system(),"
        "'version':platform.python_version()};"
        "print(json.dumps(r,sort_keys=True,separators=(',',':')))"
    )
    failures: list[str] = []
    for candidate in _GUEST_PYTHON_CANDIDATES:
        result = _guest_exec(
            state,
            guest_home,
            candidate,
            "-I",
            "-c",
            script,
            candidate,
        )
        if result.returncode != 0:
            failures.append(candidate)
            continue
        try:
            value = json.loads(_guest_text(result, "Python probe"))
        except json.JSONDecodeError:
            failures.append(candidate)
            continue
        if (
            type(value) is dict
            and value.get("implementation") == "CPython"
            and type(value.get("version")) is str
            and str(value["version"]).startswith("3.14.")
            and value.get("system") == "Darwin"
            and value.get("machine") == "arm64"
            and type(value.get("executable")) is str
            and value.get("candidate_realpath")
            == value.get("executable_realpath")
            and type(value.get("executable_realpath")) is str
            and str(value["executable_realpath"]).startswith(
                "/opt/homebrew/Cellar/python@3.14/"
            )
            and str(value["executable_realpath"]).endswith("/bin/python3.14")
        ):
            return candidate, dict(value), result
        failures.append(candidate)
    raise _QualificationRefused(
        QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
        "clean provider lacks the fixed CPython 3.14 runtime",
    )


def _parse_positive_integer(value: str, label: str) -> int:
    try:
        selected = int(value, 10)
    except ValueError as error:
        raise ValueError(f"guest {label} is not an integer") from error
    if selected <= 0:
        raise ValueError(f"guest {label} must be positive")
    return selected


def _guest_input_hashes(
    state: _CloneState,
    guest_home: str,
) -> tuple[dict[str, object], ...]:
    observed: list[dict[str, object]] = []
    for row in state.input_rows:
        name = row["name"]
        expected = row["sha256"]
        size = row["size"]
        if type(name) is not str or type(expected) is not str or type(size) is not int:
            raise RuntimeError("host input projection row is malformed")
        guest_path = _GUEST_SHARE_ROOT / name
        result = _require_guest_success(
            _guest_exec(
                state,
                guest_home,
                "/usr/bin/shasum",
                "-a",
                "256",
                os.fspath(guest_path),
            ),
            f"input hash for {name}",
        )
        line = _guest_text(result, f"input hash for {name}")
        digest = line.split(" ", 1)[0]
        if _SHA256.fullmatch(digest) is None or digest != expected:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
                f"guest input digest differs for {name}",
                terminal=True,
            )
        observed.append(
            {
                "name": name,
                "sha256": digest,
                "size": size,
                "verification": result.observation(),
            }
        )
    return tuple(observed)


def _prove_guest_provider(
    state: _CloneState,
    *,
    phase: str,
) -> tuple[dict[str, object], str]:
    if phase not in {"BEFORE_INSTALL", "AFTER_INSTALL", "AFTER_WORKER"}:
        raise ValueError("guest provider proof phase is invalid")
    guest_home = _discover_guest_home(state)

    commands: dict[str, _CommandResult] = {}

    def capture(label: str, *command: str) -> str:
        result = _require_guest_success(
            _guest_exec(state, guest_home, *command),
            label,
            terminal=phase != "BEFORE_INSTALL",
        )
        commands[label] = result
        return _guest_text(result, label)

    system = capture("system", "/usr/bin/uname", "-s")
    machine = capture("machine", "/usr/bin/uname", "-m")
    kernel = capture("kernel release", "/usr/bin/uname", "-r")
    os_version = capture(
        "OS version", "/usr/bin/sw_vers", "-productVersion"
    )
    os_build = capture("OS build", "/usr/bin/sw_vers", "-buildVersion")
    user_id = capture("guest user ID", "/usr/bin/id", "-u")
    cpu_count = _parse_positive_integer(
        capture("CPU count", "/usr/sbin/sysctl", "-n", "hw.ncpu"),
        "CPU count",
    )
    memory_bytes = _parse_positive_integer(
        capture("memory", "/usr/sbin/sysctl", "-n", "hw.memsize"),
        "memory",
    )
    machine_model = capture(
        "machine model", "/usr/sbin/sysctl", "-n", "hw.model"
    )
    df_result = _require_guest_success(
        _guest_exec(state, guest_home, "/bin/df", "-k", "/private/var/tmp"),
        "free-store observation",
        terminal=phase != "BEFORE_INSTALL",
    )
    commands["free store"] = df_result
    df_lines = _guest_text(df_result, "free-store observation").splitlines()
    if len(df_lines) < 2 or len(df_lines[-1].split()) < 4:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "guest free-store observation is malformed",
        )
    available_disk_bytes = _parse_positive_integer(
        df_lines[-1].split()[3], "free-store blocks"
    ) * 1024

    if (
        system != "Darwin"
        or machine != "arm64"
        or user_id == "0"
        or cpu_count < 2
        or memory_bytes < 8 * 1024 * 1024 * 1024
        or available_disk_bytes < _MINIMUM_GUEST_FREE_BYTES
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "guest platform, privilege, memory, or storage differs from the target",
            terminal=phase != "BEFORE_INSTALL",
        )

    sudo = _guest_exec(
        state,
        guest_home,
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/true",
    )
    commands["passwordless sudo refusal"] = sudo
    if sudo.returncode == 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "qualification guest unexpectedly grants passwordless root authority",
            terminal=phase != "BEFORE_INSTALL",
        )

    mount = capture("mount inventory", "/sbin/mount")
    mount_marker = (
        f" on {os.fspath(_GUEST_SHARE_MOUNT_ROOT)} (AppleVirtIOFS,"
    )
    if len([line for line in mount.splitlines() if mount_marker in line]) != 1:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "read-only qualification input share is not mounted",
            terminal=phase != "BEFORE_INSTALL",
        )
    share_directory = _require_guest_success(
        _guest_exec(
            state,
            guest_home,
            "/bin/test",
            "-d",
            os.fspath(_GUEST_SHARE_ROOT),
        ),
        "named qualification share directory",
        terminal=phase != "BEFORE_INSTALL",
    )
    commands["named qualification share directory"] = share_directory
    sentinel = _GUEST_SHARE_ROOT / ".kirby2-write-probe"
    write_probe = _guest_exec(
        state,
        guest_home,
        "/usr/bin/touch",
        os.fspath(sentinel),
    )
    commands["read-only share write refusal"] = write_probe
    absent_probe = _require_guest_success(
        _guest_exec(
            state,
            guest_home,
            "/bin/test",
            "!",
            "-e",
            os.fspath(sentinel),
        ),
        "read-only share sentinel absence",
        terminal=phase != "BEFORE_INSTALL",
    )
    commands["read-only share sentinel absence"] = absent_probe
    if write_probe.returncode == 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "Tart artifact share is writable despite the required :ro option",
            terminal=True,
        )

    # This fixed TEST-NET probe cannot name a production endpoint.  True Tart
    # --net-host must prevent a successful connection; no DNS query is attempted.
    network_probe = _guest_exec(
        state,
        guest_home,
        "/usr/bin/nc",
        "-G",
        "2",
        "-z",
        "192.0.2.1",
        "9",
        timeout_seconds=10,
    )
    commands["host-only TEST-NET refusal"] = network_probe
    if network_probe.returncode == 0:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_ISOLATION_UNAVAILABLE,
            "host-only guest unexpectedly connected to a TEST-NET endpoint",
            terminal=True,
        )
    route = _guest_exec(
        state,
        guest_home,
        "/usr/sbin/netstat",
        "-rn",
        "-f",
        "inet",
    )
    route6 = _guest_exec(
        state,
        guest_home,
        "/usr/sbin/netstat",
        "-rn",
        "-f",
        "inet6",
    )
    dns = _guest_exec(state, guest_home, "/usr/sbin/scutil", "--dns")
    interfaces = _guest_exec(state, guest_home, "/sbin/ifconfig", "-a")
    for label, result in (
        ("IPv4 route inventory", route),
        ("IPv6 route inventory", route6),
        ("DNS inventory", dns),
        ("interface inventory", interfaces),
    ):
        _require_guest_success(result, label, terminal=phase != "BEFORE_INSTALL")
        commands[label] = result

    python_path, python_identity, python_result = _guest_python_probe(
        state, guest_home
    )
    commands["Python identity"] = python_result
    if phase == "BEFORE_INSTALL":
        absent_script = (
            "import importlib.util;"
            "raise SystemExit(0 if importlib.util.find_spec('kirby2') is None else 42)"
        )
        absent = _guest_exec(
            state,
            guest_home,
            python_path,
            "-I",
            "-c",
            absent_script,
        )
        commands["preinstalled Kirby2 refusal"] = absent
        if absent.returncode != 0:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
                "Kirby2 is already importable before artifact installation",
            )

    inputs = _guest_input_hashes(state, guest_home)
    proof = {
        "available_disk_bytes": available_disk_bytes,
        "command_observations": {
            label: result.observation()
            for label, result in sorted(commands.items())
        },
        "cpu_count": cpu_count,
        "form": state.form,
        "guest_home_sha256": _sha256(guest_home.encode("utf-8")),
        "input_rows": list(inputs),
        "kernel_release": kernel,
        "machine": machine,
        "machine_model": machine_model,
        "memory_bytes": memory_bytes,
        "network_scope": "HOST_ONLY",
        "os_build": os_build,
        "os_version": os_version,
        "phase": phase,
        "python": python_identity,
        "python_path": python_path,
        "system": system,
    }
    state.provider_proofs.append(proof)
    return proof, guest_home


def _guest_attempt_paths(form: str) -> dict[str, PurePosixPath]:
    if form not in _FORMS:
        raise ValueError("qualification form is invalid")
    # Both forms use the same logical paths in separate disposable clones.  This
    # lets the combined qualification record describe three clean roots without
    # pretending that operational clone names are part of scientific identity.
    scratch = PurePosixPath("/private/var/tmp/kirby2-wo40g")
    unpacked = scratch / "unpacked"
    bundle_root = unpacked / _BUNDLE_ROOTS[form]
    return {
        "scratch": scratch,
        "unpacked": unpacked,
        "bundle_root": bundle_root,
        "wheelhouse": bundle_root / "wheelhouse",
        "venv": scratch / "venv",
        "worker_attempt": scratch / "worker-attempt",
    }


def _install_form(
    state: _CloneState,
    guest_home: str,
    system_python: str,
    bundle: ReleaseProtocolBundleV1,
) -> tuple[str, str, tuple[dict[str, object], ...]]:
    paths = _guest_attempt_paths(state.form)
    selector = _ARTIFACT_SELECTORS[state.form]
    commands: list[dict[str, object]] = []

    def execute(label: str, *argv: str, timeout: int = _GUEST_COMMAND_TIMEOUT_SECONDS) -> _CommandResult:
        result = _require_guest_success(
            _guest_exec(
                state,
                guest_home,
                *argv,
                timeout_seconds=timeout,
            ),
            label,
            terminal=True,
        )
        commands.append({"label": label, **result.observation()})
        return result

    execute(
        "create qualification scratch",
        "/bin/mkdir",
        "-m",
        "700",
        os.fspath(paths["scratch"]),
    )
    execute(
        "create extraction root",
        "/bin/mkdir",
        "-m",
        "700",
        os.fspath(paths["unpacked"]),
    )
    bundle_artifact = _BUNDLE_ARTIFACTS[state.form]
    execute(
        "extract verified release bundle",
        "/usr/bin/tar",
        "-xzf",
        os.fspath(_GUEST_SHARE_ROOT / bundle_artifact),
        "-C",
        os.fspath(paths["unpacked"]),
        timeout=_HOST_COMMAND_TIMEOUT_SECONDS,
    )
    execute(
        "verify extracted bundle root",
        "/bin/test",
        "-d",
        os.fspath(paths["bundle_root"]),
    )
    execute(
        "create isolated installed runtime",
        system_python,
        "-I",
        "-m",
        "venv",
        os.fspath(paths["venv"]),
        timeout=_HOST_COMMAND_TIMEOUT_SECONDS,
    )
    installed_python = os.fspath(paths["venv"] / "bin" / "python")
    dependency = bundle.requirements_lock.for_target(TART_TARGET_ID_V1)
    if len(dependency) != 1:
        raise RuntimeError("macOS qualification dependency inventory differs")
    locked = dependency[0]
    install_result = execute(
        "install release artifacts without an index",
        installed_python,
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
        os.fspath(paths["wheelhouse"]),
        f"kirby2=={RELEASE_VERSION_V1}",
        f"{locked.name}=={locked.version}",
        timeout=_HOST_COMMAND_TIMEOUT_SECONDS,
    )
    if b"Downloading" in install_result.stdout or b"https://" in install_result.stdout:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "offline pip installation emitted network-fetch output",
            terminal=True,
        )
    launcher = os.fspath(paths["venv"] / "bin" / _INSTALLED_LAUNCHERS[state.form])
    execute(
        "verify installed launcher",
        "/bin/test",
        "-x",
        launcher,
    )
    execute(
        "verify worker attempt root is absent",
        "/bin/test",
        "!",
        "-e",
        os.fspath(paths["worker_attempt"]),
    )
    origin_script = (
        "import importlib.util,json,os,sys;"
        "s=importlib.util.find_spec('kirby2');"
        "r=os.path.realpath;"
        "o=None if s is None else r(s.origin);"
        "print(json.dumps({'base_exec_prefix':r(sys.base_exec_prefix),"
        "'base_prefix':r(sys.base_prefix),'executable':sys.executable,"
        "'executable_realpath':r(sys.executable),'origin':o,"
        "'path':[r(p) if p else p for p in sys.path],'prefix':r(sys.prefix)},"
        "sort_keys=True,separators=(',',':')))"
    )
    origin = execute(
        "verify installed package origin",
        installed_python,
        "-I",
        "-c",
        origin_script,
    )
    try:
        origin_payload = json.loads(_guest_text(origin, "installed package origin"))
    except json.JSONDecodeError as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            "installed package origin record is not JSON",
            terminal=True,
        ) from error
    expected_prefix = os.fspath(paths["venv"])
    package_origin = origin_payload.get("origin") if type(origin_payload) is dict else None
    effective_path = origin_payload.get("path") if type(origin_payload) is dict else None
    observed_prefix = origin_payload.get("prefix") if type(origin_payload) is dict else None
    base_prefix = origin_payload.get("base_prefix") if type(origin_payload) is dict else None
    base_exec_prefix = (
        origin_payload.get("base_exec_prefix")
        if type(origin_payload) is dict
        else None
    )
    approved_base_prefix = re.compile(
        r"/opt/homebrew/Cellar/python@3\.14/[^/]+/Frameworks/"
        r"Python\.framework/Versions/3\.14\Z"
    )
    base_roots = (base_prefix, base_exec_prefix)
    roots_are_approved = all(
        type(item) is str and approved_base_prefix.fullmatch(item) is not None
        for item in base_roots
    )

    def beneath(path: str, root: str) -> bool:
        return path == root or path.startswith(root + "/")

    if (
        type(package_origin) is not str
        or not beneath(package_origin, expected_prefix)
        or observed_prefix != expected_prefix
        or not roots_are_approved
        or type(effective_path) is not list
        or not effective_path
        or any(
            type(item) is not str
            or not item
            or not any(
                beneath(item, root)
                for root in (expected_prefix, *base_roots)
                if type(root) is str
            )
            for item in effective_path
        )
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "installed runtime exposes a source checkout or foreign import root",
            terminal=True,
        )
    return installed_python, launcher, tuple(commands)


def _validated_worker_failure_fields(
    payload: Mapping[str, object],
    *,
    label: str,
) -> tuple[str, str]:
    code = payload.get("failure_code")
    detail = payload.get("detail")
    try:
        canonical_code = require_nfc_text(
            code,
            f"{label} failure code",
            maximum_bytes=128,
        )
        canonical_detail = require_nfc_text(
            detail,
            f"{label} detail",
            maximum_bytes=_WORKER_FAILURE_DETAIL_MAX_BYTES_V1,
        )
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            f"{label} has noncanonical failure fields",
            terminal=True,
        ) from error
    if _WORKER_FAILURE_CODE.fullmatch(canonical_code) is None:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            f"{label} has an invalid failure code",
            terminal=True,
        )
    return canonical_code, canonical_detail


def _parse_worker_result(
    *,
    form: str,
    launcher: str,
    command: _CommandResult,
) -> dict[str, object]:
    if command.returncode not in {0, 2}:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"installed qualification worker exited {command.returncode}",
            terminal=True,
        )
    if (
        not command.stdout.endswith(b"\n")
        or command.stdout.count(b"\n") != 1
        or not command.stdout[:-1]
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker stdout is not one canonical JSON line",
            terminal=True,
        )
    try:
        payload = load_canonical_json_bytes(
            command.stdout[:-1], "installed qualification worker result"
        )
    except (TypeError, ValueError) as error:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker result is not canonical JSON",
            terminal=True,
        ) from error
    if type(payload) is not dict:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker result is not an object",
            terminal=True,
        )
    schema_id = payload.get("schema_id")
    schema_version = payload.get("schema_version")
    status = payload.get("status")
    observed_form = payload.get("form")
    result_sha256 = payload.get("result_sha256")
    if (
        schema_id != _WORKER_SCHEMA_ID_V1
        or schema_version != 1
        or observed_form != form
        or status not in {"PASS", "FAIL", "REFUSED"}
        or type(result_sha256) is not str
        or _SHA256.fullmatch(result_sha256) is None
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker result identity fields differ",
            terminal=True,
        )
    body = dict(payload)
    del body["result_sha256"]
    if _sha256(canonical_json_bytes(body)) != result_sha256:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker self-digest differs",
            terminal=True,
        )
    expected_returncode = 0 if status == "PASS" else 2
    if command.returncode != expected_returncode:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker status and exit code disagree",
            terminal=True,
        )
    if status != "PASS":
        code, detail = _validated_worker_failure_fields(
            payload,
            label="failed qualification worker result",
        )
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            f"installed qualification worker {status.lower()}: {code}: {detail}",
            terminal=True,
        )
    expected_fields = {
        "attempt_root",
        "command_observations",
        "execution_policy_id",
        "facts",
        "form",
        "launcher",
        "offline",
        "platform",
        "result_sha256",
        "roots",
        "schema_id",
        "schema_version",
        "status",
        "step_results",
    }
    platform_row = payload.get("platform")
    if (
        set(payload) != expected_fields
        or payload.get("execution_policy_id") != _WORKER_EXECUTION_POLICY_ID_V1
        or payload.get("offline") is not True
        or payload.get("attempt_root")
        != os.fspath(_guest_attempt_paths(form)["worker_attempt"])
        or payload.get("launcher") != launcher
        or type(platform_row) is not dict
        or set(platform_row)
        != {"machine", "python_implementation", "python_version", "system"}
        or platform_row.get("system") != "Darwin"
        or platform_row.get("machine") != "arm64"
        or platform_row.get("python_implementation") != "CPython"
        or type(platform_row.get("python_version")) is not str
        or not str(platform_row["python_version"]).startswith("3.14.")
        or any(type(payload.get(name)) is not list for name in (
            "command_observations", "roots", "step_results"
        ))
        or type(payload.get("facts")) is not dict
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "passing qualification worker envelope differs from the closed contract",
            terminal=True,
        )
    return dict(payload)


def _run_installed_worker(
    state: _CloneState,
    guest_home: str,
    installed_python: str,
    launcher: str,
) -> tuple[dict[str, object], _CommandResult]:
    paths = _guest_attempt_paths(state.form)
    result = _guest_exec(
        state,
        guest_home,
        installed_python,
        "-I",
        "-m",
        "kirby2.release.qualification_worker",
        "--form",
        state.form,
        "--launcher",
        launcher,
        "--attempt-root",
        os.fspath(paths["worker_attempt"]),
        timeout_seconds=_WORKER_TIMEOUT_SECONDS,
        maximum_output_bytes=_WORKER_OUTPUT_MAX_BYTES,
    )
    parsed = _parse_worker_result(
        form=state.form,
        launcher=launcher,
        command=result,
    )
    state.worker_result = parsed
    return parsed, result


def _run_form(
    state: _CloneState,
    bundle: ReleaseProtocolBundleV1,
) -> None:
    _spawn_host_only_vm(state)
    _wait_for_guest_agent(state)
    before, guest_home = _prove_guest_provider(state, phase="BEFORE_INSTALL")
    system_python = before["python_path"]
    if type(system_python) is not str:
        raise RuntimeError("provider proof omitted its selected Python path")
    installed_python, launcher, installation = _install_form(
        state,
        guest_home,
        system_python,
        bundle,
    )
    state.provider_proofs.append(
        {
            "form": state.form,
            "installation_commands": list(installation),
            "phase": "INSTALLATION",
        }
    )
    _prove_guest_provider(state, phase="AFTER_INSTALL")
    worker, command = _run_installed_worker(
        state,
        guest_home,
        installed_python,
        launcher,
    )
    state.provider_proofs.append(
        {
            "form": state.form,
            "phase": "WORKER",
            "worker_command": command.observation(),
            "worker_result_sha256": worker["result_sha256"],
            "worker_status": worker["status"],
        }
    )
    _prove_guest_provider(state, phase="AFTER_WORKER")


def _clone_is_present(name: str) -> bool:
    return name in _local_vms()


def _stop_delete_clone(state: _CloneState) -> tuple[str, ...]:
    failures: list[str] = []
    if not state.created or state.deleted:
        return ()
    if state.name == TART_BASE_VM_V1 or _CLONE_NAME.fullmatch(state.name) is None:
        return ("cleanup target escaped the owned clone grammar",)
    try:
        inventory = _local_vms()
    except Exception as error:  # cleanup must report rather than hide the first failure
        inventory = {}
        failures.append(f"VM inventory failed during cleanup: {type(error).__name__}")
    item = inventory.get(state.name)
    if item is not None and item.get("Running") is True:
        try:
            stopped = _run_tart(
                "stop",
                state.name,
                "--timeout",
                "30",
                timeout_seconds=60,
            )
            if stopped.returncode != 0:
                failures.append("Tart stop returned nonzero")
        except Exception as error:
            failures.append(f"Tart stop failed: {type(error).__name__}")
    process = state.run_process
    if process is not None:
        deadline = time.monotonic() + 60
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            _terminate_process(process)
            failures.append("Tart run process required host termination")
        try:
            finished = process.finish()
            state.provider_proofs.append(
                {
                    "form": state.form,
                    "phase": "TART_RUN",
                    "run_command": finished.observation(),
                }
            )
        except Exception as error:
            failures.append(f"Tart run capture failed: {type(error).__name__}")
        state.run_process = None
    try:
        if _clone_is_present(state.name):
            deleted = _run_tart("delete", state.name, timeout_seconds=120)
            if deleted.returncode != 0:
                failures.append("Tart delete returned nonzero")
            elif _clone_is_present(state.name):
                failures.append("Tart clone remains after delete")
            else:
                state.deleted = True
        else:
            state.deleted = True
    except Exception as error:
        failures.append(f"Tart delete verification failed: {type(error).__name__}")
    return tuple(failures)


def _remove_projection(path: Path) -> str | None:
    try:
        selected = path.resolve(strict=True)
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        selected.relative_to(temporary_root)
        if not selected.name.startswith("kirby2-wo40g-"):
            raise ValueError("projection name escaped the qualification prefix")
        shutil.rmtree(selected)
    except FileNotFoundError:
        return None
    except Exception as error:
        return f"projection cleanup failed: {type(error).__name__}"
    return None


def _worker_collection(
    payload: Mapping[str, object],
    name: str,
) -> list[object]:
    value = payload.get(name)
    if type(value) is not list:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            f"qualification worker omitted {name}",
            terminal=True,
        )
    return value


def _qualification_commands(
    states: Sequence[_CloneState],
) -> tuple[ReleaseQualificationCommandObservationV1, ...]:
    commands: list[ReleaseQualificationCommandObservationV1] = []
    sequence = 1
    seen: set[str] = set()
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("qualification worker result is unavailable")
        for value in _worker_collection(state.worker_result, "command_observations"):
            if type(value) is not dict:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker command observation is not an object",
                    terminal=True,
                )
            row = dict(value)
            command_id = row.get("command_id")
            if type(command_id) is not str or command_id in seen:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker command IDs are invalid or duplicated",
                    terminal=True,
                )
            row["sequence"] = sequence
            try:
                command = ReleaseQualificationCommandObservationV1.from_dict(row)
            except (TypeError, ValueError) as error:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker command observation failed typed parsing",
                    terminal=True,
                ) from error
            expected_selector = _ARTIFACT_SELECTORS[state.form]
            if command.artifact_selector != expected_selector:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker command selector differs from its form",
                    terminal=True,
                )
            commands.append(command)
            seen.add(command_id)
            sequence += 1
    return tuple(commands)


def _qualification_steps(
    states: Sequence[_CloneState],
) -> tuple[ReleaseQualificationStepObservationV1, ...]:
    steps: list[ReleaseQualificationStepObservationV1] = []
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("qualification worker result is unavailable")
        for value in _worker_collection(state.worker_result, "step_results"):
            try:
                step = ReleaseQualificationStepObservationV1.from_dict(value)
            except (TypeError, ValueError) as error:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker step observation failed typed parsing",
                    terminal=True,
                ) from error
            if step.artifact_selector != _ARTIFACT_SELECTORS[state.form]:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                    "qualification worker step selector differs from its form",
                    terminal=True,
                )
            steps.append(step)
    return tuple(steps)


def _qualification_roots(
    states: Sequence[_CloneState],
) -> tuple[ReleaseQualificationRootObservationV1, ...]:
    observed: list[tuple[ReleaseQualificationRootObservationV1, ...]] = []
    for state in states:
        if state.worker_result is None:
            raise RuntimeError("qualification worker result is unavailable")
        roots = tuple(
            ReleaseQualificationRootObservationV1.from_dict(value)
            for value in _worker_collection(state.worker_result, "roots")
        )
        observed.append(roots)
    if not observed or any(
        tuple(item.as_dict() for item in roots)
        != tuple(item.as_dict() for item in observed[0])
        for roots in observed[1:]
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "desktop and headless worker clean-root observations differ",
            terminal=True,
        )
    return observed[0]


def _worker_facts(state: _CloneState) -> dict[str, object]:
    if state.worker_result is None or type(state.worker_result.get("facts")) is not dict:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker omitted its fact projection",
            terminal=True,
        )
    facts = dict(state.worker_result["facts"])
    expected = {
        "clean_environment",
        "cross_platform_integer_core_sha256",
        "replay_sha256",
        "run_sha256",
    }
    if set(facts) != expected:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "qualification worker fact fields differ",
            terminal=True,
        )
    for field in (
        "cross_platform_integer_core_sha256",
        "replay_sha256",
        "run_sha256",
    ):
        if type(facts[field]) is not str or _SHA256.fullmatch(facts[field]) is None:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "qualification worker fact digest is invalid",
                terminal=True,
            )
    if facts["clean_environment"] is not True:
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_NOT_CLEAN,
            "qualification worker did not observe a clean environment",
            terminal=True,
        )
    return facts


def _artifact_bindings(
    index: ReleaseArtifactIndexV1,
    states: Sequence[_CloneState],
) -> tuple[ReleaseQualificationArtifactBindingV1, ...]:
    copied: dict[str, tuple[int, str]] = {}
    for state in states:
        for row in state.input_rows:
            artifact_id = row.get("artifact_id")
            if artifact_id in RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[
                TART_TARGET_ID_V1
            ]:
                size = row.get("size")
                digest = row.get("sha256")
                if type(artifact_id) is not str or type(size) is not int or type(digest) is not str:
                    raise RuntimeError("artifact projection binding is malformed")
                prior = copied.get(artifact_id)
                if prior is not None and prior != (size, digest):
                    raise RuntimeError("artifact projection bindings conflict")
                copied[artifact_id] = (size, digest)
    indexed = {item.artifact_id: item for item in index.artifacts}
    bindings: list[ReleaseQualificationArtifactBindingV1] = []
    for artifact_id in RELEASE_QUALIFICATION_ARTIFACT_IDS_BY_TARGET_V1[
        TART_TARGET_ID_V1
    ]:
        size, copy_digest = copied[artifact_id]
        record = indexed[artifact_id]
        bindings.append(
            ReleaseQualificationArtifactBindingV1(
                artifact_id=artifact_id,
                size=size,
                release_store_sha256=record.transport_sha256,
                provider_copy_sha256=copy_digest,
            )
        )
    return tuple(bindings)


def _before_install_proofs(states: Sequence[_CloneState]) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for state in states:
        selected = next(
            (
                item
                for item in state.provider_proofs
                if item.get("phase") == "BEFORE_INSTALL"
            ),
            None,
        )
        if selected is None:
            raise RuntimeError("provider before-install proof is unavailable")
        result.append(selected)
    return tuple(result)


def _compose_records(
    *,
    bundle: ReleaseProtocolBundleV1,
    build_evidence: _FileSnapshot,
    provider_inventory: _FileSnapshot,
    provider_capability: ReleaseCleanProviderV1,
    index: ReleaseArtifactIndexV1,
    build_record: ReleaseArtifactBuildRecordV1,
    fixed_provider: Mapping[str, object],
    states: Sequence[_CloneState],
    started_at_utc: str,
    finished_at_utc: str,
    duration_ns: int,
) -> tuple[ReleaseCleanProviderAttestationV1, ReleaseQualificationAttemptV1]:
    proofs = _before_install_proofs(states)
    comparable = (
        "available_disk_bytes",
        "cpu_count",
        "kernel_release",
        "machine",
        "machine_model",
        "memory_bytes",
        "os_version",
        "system",
    )
    for field_name in comparable[1:]:
        if any(item[field_name] != proofs[0][field_name] for item in proofs[1:]):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
                f"desktop and headless provider {field_name} observations differ",
                terminal=True,
            )
    python_rows = tuple(item["python"] for item in proofs)
    if any(item != python_rows[0] for item in python_rows[1:]):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PROVIDER_IDENTITY_MISMATCH,
            "desktop and headless provider Python observations differ",
            terminal=True,
        )
    capability_sha256 = provider_capability.fingerprint
    provider = ReleaseCleanProviderAttestationV1(
        provider_id=(
            f"kirby2-clean-provider-{TART_TARGET_ID_V1}-{capability_sha256[:16]}"
        ),
        target_id=TART_TARGET_ID_V1,
        provider_inventory_sha256=provider_inventory.sha256,
        provider_capability_sha256=capability_sha256,
        provider_adapter_id="TART_LOCAL_VM_V1",
        attestation_method=RELEASE_QUALIFICATION_ATTESTATION_METHOD_V1,
        system=str(proofs[0]["system"]),
        os_version=str(proofs[0]["os_version"]),
        kernel_release=str(proofs[0]["kernel_release"]),
        machine=str(proofs[0]["machine"]),
        machine_model=str(proofs[0]["machine_model"]),
        python_implementation=str(python_rows[0]["implementation"]),  # type: ignore[index]
        python_version=str(python_rows[0]["version"]),  # type: ignore[index]
        cpu_count=int(proofs[0]["cpu_count"]),
        memory_bytes=int(proofs[0]["memory_bytes"]),
        available_disk_bytes=min(int(item["available_disk_bytes"]) for item in proofs),
        offline_install=True,
        network_scope=ReleaseQualificationNetworkScopeV1.HOST_ONLY,
        observed_at_utc=started_at_utc,
    )
    commands = _qualification_commands(states)
    steps = _qualification_steps(states)
    roots = _qualification_roots(states)
    desktop_facts = _worker_facts(states[0])
    headless_facts = _worker_facts(states[1])
    if (
        desktop_facts["cross_platform_integer_core_sha256"]
        != headless_facts["cross_platform_integer_core_sha256"]
        or desktop_facts["replay_sha256"] != headless_facts["replay_sha256"]
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.RESULT_INVALID,
            "desktop and headless baseline or replay identities differ",
            terminal=True,
        )
    facts = ReleaseQualificationFactsV1(
        clean_environment=True,
        cross_platform_integer_core_sha256=str(
            desktop_facts["cross_platform_integer_core_sha256"]
        ),
        desktop_run_sha256=str(desktop_facts["run_sha256"]),
        headless_run_sha256=str(headless_facts["run_sha256"]),
        platform_id=TART_TARGET_ID_V1,
        replay_sha256=str(desktop_facts["replay_sha256"]),
    )
    instance_projection = {
        "fixed_provider": dict(fixed_provider),
        "owned_clone_lifecycle": [
            item
            for state in states
            for item in state.provider_proofs
            if item.get("phase") == "TART_RUN" or "clone_name" in item
        ],
    }
    instance_sha256 = _sha256(canonical_json_bytes(instance_projection))
    session_projection = {
        "candidate_commit": index.candidate_commit,
        "instance_sha256": instance_sha256,
        "provider_sha256": provider.sha256,
        "worker_result_sha256s": [
            state.worker_result["result_sha256"]  # type: ignore[index]
            for state in states
        ],
    }
    session_sha256 = _sha256(canonical_json_bytes(session_projection))
    session = ReleaseQualificationSessionV1(
        session_id=f"wo40g-{session_sha256[:24]}",
        provider_id=provider.provider_id,
        provider_attestation_sha256=provider.sha256,
        provider_instance_id=f"tart-instance-{instance_sha256[:24]}",
        target_id=TART_TARGET_ID_V1,
        attempt_number=1,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        duration_ns=duration_ns,
        network_scope=ReleaseQualificationNetworkScopeV1.HOST_ONLY,
        installation_source=RELEASE_QUALIFICATION_INSTALLATION_SOURCE_V1,
        source_checkout_present=False,
        artifact_bindings=_artifact_bindings(index, states),
        roots=roots,
    )
    attempt = build_release_qualification_attempt_record(
        gate_id="WO40-G",
        target_id=TART_TARGET_ID_V1,
        candidate_commit=index.candidate_commit,
        protocol_set_sha256=bundle.protocol_set_sha256,
        source_manifest_sha256=build_record.source_manifest_sha256,
        artifact_index_sha256=index.sha256,
        build_evidence_sha256=build_evidence.sha256,
        session=session,
        commands=commands,
        steps=steps,
        facts=facts,
    )
    verify_release_qualification_record(
        provider,
        attempt,
        bundle.qualification_protocol,
    )
    return provider, attempt


def _require_store_anchor(
    root: Path,
    descriptor: int,
    opened_identity: tuple[int, ...],
) -> None:
    """Rebind a path to its already-locked directory without comparing size."""

    by_path = os.lstat(root)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(by_path.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (by_path.st_dev, by_path.st_ino) != (opened.st_dev, opened.st_ino)
        or (opened.st_dev, opened.st_ino) != opened_identity[:2]
        or opened.st_uid != os.getuid()
        or opened.st_mode & 0o022
    ):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.INPUT_INVALID,
            "release artifact store anchor changed or became unsafe",
            terminal=True,
        )


def _open_publication_directory(root_descriptor: int, parts: Sequence[str]) -> int:
    """Open/create one fixed evidence directory chain without following links."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow publication support")
    current = os.dup(root_descriptor)
    try:
        for part in parts:
            if (
                type(part) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", part) is None
            ):
                raise RuntimeError("qualification publication directory is invalid")
            try:
                os.mkdir(part, 0o755, dir_fd=current)
                os.fsync(current)
            except FileExistsError:
                pass
            following = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | nofollow,
                dir_fd=current,
            )
            metadata = os.fstat(following)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
            ):
                os.close(following)
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                    "qualification publication directory ownership is unsafe",
                    terminal=True,
                )
            os.close(current)
            current = following
        return current
    except Exception:
        os.close(current)
        raise


def _relative_file_exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _publish_immutable_file(directory: int, name: str, raw: bytes) -> None:
    """Publish complete bytes with an atomic, no-overwrite hard-link activation."""

    if (
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}\.json", name) is None
        or type(raw) is not bytes
        or not raw
        or len(raw) > 64 * 1024 * 1024
    ):
        raise ValueError("qualification publication name or payload is invalid")
    if _relative_file_exists(directory, name):
        raise _QualificationRefused(
            QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
            "immutable qualification record already exists",
            terminal=True,
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform lacks no-follow publication support")
    staging = f".kirby2-qualification-{secrets.token_hex(16)}.tmp"
    descriptor = -1
    verification_descriptor = -1
    linked = False
    staging_retired = False
    activated = False
    try:
        descriptor = os.open(
            staging,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            0o600,
            dir_fd=directory,
        )
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short qualification publication write")
            view = view[count:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        verification_descriptor = os.open(
            staging,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
            dir_fd=directory,
        )
        metadata = os.fstat(verification_descriptor)
        chunks: list[bytes] = []
        remaining = len(raw) + 1
        while remaining:
            chunk = os.read(
                verification_descriptor,
                min(1024 * 1024, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o444
            or b"".join(chunks) != raw
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "qualification staging verification failed",
                terminal=True,
            )
        try:
            os.link(
                staging,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "immutable qualification publication lost its exclusive activation",
                terminal=True,
            ) from error
        linked = True
        os.unlink(staging, dir_fd=directory)
        staging_retired = True
        os.fsync(directory)
        activated = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not linked and not staging_retired:
            try:
                os.unlink(staging, dir_fd=directory)
                os.fsync(directory)
            except FileNotFoundError:
                pass
        try:
            if verification_descriptor >= 0:
                os.close(verification_descriptor)
        except OSError:
            if not activated:
                raise


def _publish_records(
    *,
    root_descriptor: int,
    target_id: str,
    provider: ReleaseCleanProviderAttestationV1,
    attempt: ReleaseQualificationAttemptV1,
) -> tuple[str, str]:
    provider_relative, attempt_relative = release_qualification_record_paths(
        target_id
    )
    provider_path = PurePosixPath(provider_relative)
    attempt_path = PurePosixPath(attempt_relative)
    expected_gate = "wo40-g" if target_id == "macos-arm64" else "wo40-h"
    if (
        provider.target_id != target_id
        or attempt.target_id != target_id
        or provider_path.parent != PurePosixPath(".")
        or attempt_path.parent.parts != ("gate-evidence", expected_gate)
    ):
        raise RuntimeError("qualification record paths differ from the closed target policy")
    provider_raw = provider.canonical_bytes()
    attempt_raw = attempt.canonical_bytes()
    ReleaseCleanProviderAttestationV1.from_bytes(provider_raw)
    ReleaseQualificationAttemptV1.from_bytes(attempt_raw)
    provider_directory = _open_publication_directory(root_descriptor, ())
    attempt_directory = _open_publication_directory(
        root_descriptor, attempt_path.parent.parts
    )
    try:
        if _relative_file_exists(provider_directory, provider_path.name) or _relative_file_exists(
            attempt_directory, attempt_path.name
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "immutable qualification evidence appeared before activation",
                terminal=True,
            )
        # Provider attestation must exist before the attempt that binds it.  The
        # attempt activation is deliberately the final publication operation.
        _publish_immutable_file(provider_directory, provider_path.name, provider_raw)
        _publish_immutable_file(attempt_directory, attempt_path.name, attempt_raw)
    finally:
        for descriptor in (provider_directory, attempt_directory):
            try:
                os.close(descriptor)
            except OSError:
                # Directory retirement cannot invalidate an already activated,
                # fsynced, byte-verified attempt. The process owns no later work.
                pass
    return provider_relative, attempt_relative


def _same_snapshot(path: Path, expected: _FileSnapshot, maximum_bytes: int) -> bool:
    observed = _stable_read(
        path,
        maximum_bytes=maximum_bytes,
        require_read_only=False,
    )
    return observed.raw == expected.raw and observed.identity == expected.identity


def _executor_outcome(
    bundle: ReleaseProtocolBundleV1,
    *,
    status: ReleaseCommandStatusV1,
    detail: str,
    refusal_code: QualificationExecutorRefusalCodeV1 | None = None,
    payload: Mapping[str, object] | None = None,
) -> ReleaseCommandOutcomeV1:
    return ReleaseCommandOutcomeV1(
        command_id="QUALIFY_RELEASE",
        status=status,
        protocol_set_sha256=bundle.protocol_set_sha256,
        detail=detail,
        refusal_code=None if refusal_code is None else refusal_code.value,
        payload=dict(payload or {}),
    )


def _execute_macos_release_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Run the one closed macOS qualification attempt and publish it once.

    The API intentionally exposes no VM name, executable, argv, network, timeout,
    environment, share, or cleanup configuration.  Unsupported targets and a host
    that cannot provide real Tart ``--net-host`` isolation return typed refusals.
    """

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("qualification execution requires the exact protocol bundle")
    states: list[_CloneState] = []
    projections: list[Path] = []
    store_descriptor: int | None = None
    opened_identity: tuple[int, ...] | None = None
    candidate_commit: str | None = None
    result: ReleaseCommandOutcomeV1 | None = None
    failure: Exception | None = None
    cleanup_failures: list[str] = []
    try:
        root = _absolute_input(artifact_root, "release artifact root")
        evidence_path = _absolute_input(build_evidence, "WO40-F build evidence")
        try:
            _require_canonical_tracked_build_evidence(
                bundle.repository_root.resolve(strict=True),
                evidence_path,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                "qualification requires committed canonical WO40-F evidence",
            ) from error
        store_descriptor, opened_identity = _open_artifact_store(root)
        try:
            fcntl.flock(store_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PUBLICATION_CONFLICT,
                "release artifact store is locked by another operation",
            ) from error
        _require_no_prior_records(root, TART_TARGET_ID_V1)

        evidence_snapshot = _stable_read(
            evidence_path,
            maximum_bytes=_BUILD_EVIDENCE_MAX_BYTES,
            require_read_only=False,
        )
        evidence_binding = ReleaseBuildEvidenceBindingV1.from_markdown_bytes(
            evidence_snapshot.raw
        )
        index_snapshot = _stable_read(
            root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        build_record_snapshot = _stable_read(
            root / RELEASE_BUILD_RECORD_FILENAME_V1,
            maximum_bytes=RELEASE_RECORD_MAX_BYTES_V1,
            require_read_only=True,
        )
        index = ReleaseArtifactIndexV1.from_bytes(index_snapshot.raw)
        build_record = ReleaseArtifactBuildRecordV1.from_bytes(build_record_snapshot.raw)
        candidate_commit = index.candidate_commit
        build_check_rows = tuple(
            (item.check_id, item.evidence_sha256, item.status)
            for item in build_record.checks
        )
        if (
            evidence_binding.build_evidence_sha256 != evidence_snapshot.sha256
            or evidence_binding.candidate_commit != candidate_commit
            or evidence_binding.protocol_set_sha256 != bundle.protocol_set_sha256
            or evidence_binding.source_manifest_sha256
            != build_record.source_manifest_sha256
            or evidence_binding.artifact_index_sha256 != index.sha256
            or evidence_binding.artifact_index_record_sha256 != index_snapshot.sha256
            or evidence_binding.artifact_index_record_size != len(index_snapshot.raw)
            or evidence_binding.build_record_sha256 != build_record_snapshot.sha256
            or evidence_binding.build_record_size != len(build_record_snapshot.raw)
            or build_record.candidate_commit != candidate_commit
            or build_record.protocol_set_sha256 != bundle.protocol_set_sha256
            or build_record.artifact_index_sha256 != index.sha256
            or evidence_binding.check_rows != build_check_rows
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "WO40-F evidence, artifact index, build record, or protocol identity differs",
            )
        artifact_verification = verify_release_artifacts(
            bundle,
            root,
            candidate_commit=candidate_commit,
        )
        if artifact_verification.status is not ReleaseCommandStatusV1.PASS:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "deep immutable release-artifact verification did not pass",
            )

        inventory_snapshot = _stable_read(
            root / "clean-providers.toml",
            maximum_bytes=4 * 1024 * 1024,
            require_read_only=False,
        )
        inventory = ReleaseCleanProviderInventoryV1.from_bytes(inventory_snapshot.raw)
        capability = inventory.by_target().get(TART_TARGET_ID_V1)
        platform_target = next(
            item
            for item in bundle.platform_protocol.targets
            if item.target_id == TART_TARGET_ID_V1
        )
        if capability is None or capability.readiness(platform_target)[0] != "PASS":
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.PROVIDER_UNAVAILABLE,
                "preregistered macOS clean-provider capability is not ready",
            )

        started_at_utc = _utc_second()
        started_ns = time.monotonic_ns()
        fixed_provider = _require_tart_provider()
        for form in _FORMS:
            projection, input_rows = _build_projection(
                form=form,
                artifact_root=root,
                build_evidence=evidence_path,
                index=index,
                build_record=build_record,
            )
            projections.append(projection)
            states.append(
                _CloneState(
                    form=form,
                    name=_clone_name(candidate_commit, form),
                    projection_root=projection,
                    input_rows=input_rows,
                )
            )
        for state in states:
            form_failure: Exception | None = None
            try:
                _create_clone(
                    state,
                    fixed_provider["base_config_projection"],  # type: ignore[arg-type]
                )
                _run_form(state, bundle)
            except Exception as error:
                form_failure = error
            form_cleanup = _stop_delete_clone(state)
            if form_cleanup:
                raise _QualificationRefused(
                    QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
                    "; ".join(form_cleanup),
                    terminal=True,
                ) from form_failure
            if form_failure is not None:
                raise form_failure

        _require_tart_provider()
        final_verification = verify_release_artifacts(
            bundle,
            root,
            candidate_commit=candidate_commit,
        )
        if final_verification.status is not ReleaseCommandStatusV1.PASS:
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.ARTIFACT_VERIFICATION_FAILED,
                "release artifacts changed during qualification",
                terminal=True,
            )
        if (
            not _same_snapshot(
                evidence_path, evidence_snapshot, _BUILD_EVIDENCE_MAX_BYTES
            )
            or not _same_snapshot(
                root / RELEASE_ARTIFACT_INDEX_FILENAME_V1,
                index_snapshot,
                RELEASE_RECORD_MAX_BYTES_V1,
            )
            or not _same_snapshot(
                root / RELEASE_BUILD_RECORD_FILENAME_V1,
                build_record_snapshot,
                RELEASE_RECORD_MAX_BYTES_V1,
            )
            or not _same_snapshot(
                root / "clean-providers.toml",
                inventory_snapshot,
                4 * 1024 * 1024,
            )
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.INPUT_INVALID,
                "immutable qualification inputs changed during provider execution",
                terminal=True,
            )
        finished_at_utc = _utc_second()
        duration_ns = time.monotonic_ns() - started_ns
        provider, attempt = _compose_records(
            bundle=bundle,
            build_evidence=evidence_snapshot,
            provider_inventory=inventory_snapshot,
            provider_capability=capability,
            index=index,
            build_record=build_record,
            fixed_provider=fixed_provider,
            states=states,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            duration_ns=duration_ns,
        )
        _require_store_anchor(root, store_descriptor, opened_identity)
        _require_no_prior_records(root, TART_TARGET_ID_V1)
        provider_relative, attempt_relative = _publish_records(
            root_descriptor=store_descriptor,
            target_id=TART_TARGET_ID_V1,
            provider=provider,
            attempt=attempt,
        )
        deep = verify_release_qualification(
            bundle,
            target_id=TART_TARGET_ID_V1,
            build_evidence=evidence_path,
            artifact_root=root,
        )
        if (
            deep.status != attempt.status
            or deep.provider_attestation_sha256 != provider.sha256
            or deep.qualification_attempt_sha256 != attempt.sha256
            or deep.check_count != len(attempt.checks)
        ):
            raise _QualificationRefused(
                QualificationExecutorRefusalCodeV1.RESULT_INVALID,
                "published qualification evidence failed deep verification",
                terminal=True,
            )
        status_by_attempt = {
            "PASS": ReleaseCommandStatusV1.PASS,
            "PASS_WITH_WARNINGS": ReleaseCommandStatusV1.PASS_WITH_WARNINGS,
            "FAIL": ReleaseCommandStatusV1.FAIL,
            "NOT_EXERCISED": ReleaseCommandStatusV1.NOT_EXERCISED,
        }
        result = _executor_outcome(
            bundle,
            status=status_by_attempt[attempt.status],
            detail=(
                "macOS desktop and headless qualification completed in two "
                "disposable host-only Tart clones; immutable evidence was published."
            ),
            payload={
                "artifact_index_sha256": index.sha256,
                "attempt_path": attempt_relative,
                "candidate_commit": candidate_commit,
                "check_count": len(attempt.checks),
                "provider_attestation_path": provider_relative,
                "provider_attestation_sha256": provider.sha256,
                "qualification_attempt_sha256": attempt.sha256,
                "session_id": attempt.session.session_id,
                "target_id": TART_TARGET_ID_V1,
                "warning_count": len(attempt.warnings),
            },
        )
    except Exception as error:
        failure = error
    finally:
        for state in states:
            cleanup_failures.extend(_stop_delete_clone(state))
        for projection in projections:
            projection_failure = _remove_projection(projection)
            if projection_failure is not None:
                cleanup_failures.append(projection_failure)
        if store_descriptor is not None:
            try:
                try:
                    fcntl.flock(store_descriptor, fcntl.LOCK_UN)
                except OSError as error:
                    cleanup_failures.append(
                        f"release-store unlock failed: {type(error).__name__}"
                    )
            finally:
                try:
                    os.close(store_descriptor)
                except OSError as error:
                    cleanup_failures.append(
                        f"release-store close failed: {type(error).__name__}"
                    )

    payload: dict[str, object] = {
        "candidate_commit": candidate_commit,
        "target_id": TART_TARGET_ID_V1,
    }
    if cleanup_failures:
        payload["cleanup_failures"] = cleanup_failures
        if isinstance(failure, _QualificationRefused):
            payload["primary_refusal_code"] = failure.code.value
        return _executor_outcome(
            bundle,
            status=ReleaseCommandStatusV1.FAIL,
            detail="qualification provider cleanup did not complete exactly",
            refusal_code=QualificationExecutorRefusalCodeV1.PROVIDER_CLEANUP_FAILED,
            payload=payload,
        )
    if failure is not None:
        if isinstance(failure, _QualificationRefused):
            return _executor_outcome(
                bundle,
                status=(
                    ReleaseCommandStatusV1.FAIL
                    if failure.terminal
                    else ReleaseCommandStatusV1.REFUSED
                ),
                detail=failure.detail,
                refusal_code=failure.code,
                payload=payload,
            )
        return _executor_outcome(
            bundle,
            status=(
                ReleaseCommandStatusV1.FAIL
                if any(state.created for state in states)
                else ReleaseCommandStatusV1.REFUSED
            ),
            detail=f"closed qualification executor failed: {type(failure).__name__}",
            refusal_code=QualificationExecutorRefusalCodeV1.PROVIDER_EXECUTION_FAILED,
            payload=payload,
        )
    if result is None:  # pragma: no cover - total control-flow guard
        raise RuntimeError("qualification executor omitted its terminal outcome")
    return result


def _execute_linux_release_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    # Keep the SSH provider out of the macOS/Tart import and execution path.  The
    # Linux module imports shared hardened record/publication helpers from here,
    # so this target-local lazy import also avoids a module-initialization cycle.
    from .qualification_linux_executor import execute_linux_release_qualification

    return execute_linux_release_qualification(
        bundle,
        build_evidence=build_evidence,
        artifact_root=artifact_root,
    )


_QUALIFICATION_EXECUTORS_BY_TARGET_V1 = {
    TART_TARGET_ID_V1: _execute_macos_release_qualification,
    "linux-x86_64": _execute_linux_release_qualification,
}


def execute_release_qualification(
    bundle: ReleaseProtocolBundleV1,
    *,
    target_id: str,
    build_evidence: Path,
    artifact_root: Path,
) -> ReleaseCommandOutcomeV1:
    """Dispatch one closed qualification controller for its exact target."""

    if type(bundle) is not ReleaseProtocolBundleV1:
        raise TypeError("qualification execution requires the exact protocol bundle")
    executor = (
        _QUALIFICATION_EXECUTORS_BY_TARGET_V1.get(target_id)
        if type(target_id) is str
        else None
    )
    if executor is None:
        return _executor_outcome(
            bundle,
            status=ReleaseCommandStatusV1.REFUSED,
            detail="closed qualification supports only macos-arm64 and linux-x86_64",
            refusal_code=QualificationExecutorRefusalCodeV1.TARGET_UNSUPPORTED,
            payload={
                "candidate_commit": None,
                "target_id": target_id if type(target_id) is str else None,
            },
        )
    return executor(
        bundle,
        build_evidence=build_evidence,
        artifact_root=artifact_root,
    )


__all__ = [
    "QualificationExecutorRefusalCodeV1",
    "execute_release_qualification",
]
