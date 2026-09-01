"""Read-only release identity and installation health checks."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import stat
import tomllib
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, ClassVar

from kirby2 import __version__
from kirby2.packs.builders import (
    builtin_pack_runtime_environment_v1,
    verify_domain_pack_archive_bytes,
)
from kirby2.packs.dependencies import (
    PackRuntimeEnvironmentV1,
    resolve_pack_dependencies,
)
from kirby2.packs.formats import (
    K2PACK_MANIFEST_PATH,
    canonical_json_bytes,
    canonical_manifest_bytes,
    normalized_archive_paths,
    normalized_zip_info,
)
from kirby2.packs.identity import verify_pack_payload_identity
from kirby2.packs.install import read_pack_registry
from kirby2.packs.registry import PackRegistryEntryV1, PackRegistryV1
from kirby2.research.models import RunManifest
from kirby2.research.paths import DataAreaId, DataPaths
from kirby2.session.journal import LiveSessionJournalV1
from kirby2.session.records import RecoveryBoundaryKindV1

from .models import (
    ReleaseSchemaKindV1,
    ReleaseSchemaUseV1,
    builtin_release_schema_inventory,
)


RELEASE_IDENTITY_SCHEMA_ID_V1 = "KIRBY2_RELEASE_IDENTITY_V1"
RELEASE_DOCTOR_SCHEMA_ID_V1 = "KIRBY2_RELEASE_DOCTOR_V1"
RELEASE_DOCTOR_SCHEMA_VERSION_V1 = 1
RELEASE_SOURCE_REVISION_V1 = "UNFROZEN_SOURCE_V1"

_MAX_RUN_MANIFEST_BYTES_V1 = 4 * 1024 * 1024
_MAX_RUN_MANIFEST_COUNT_V1 = 100_000
_MAX_PACK_OBJECT_FILE_COUNT_V1 = 16_384
_MAX_ACTIVE_RECOVERY_POINTERS_V1 = 4_096


class HealthStatusV1(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ReleaseIdentityV1:
    engine_version: str
    source_revision: str
    source_sha256: str
    source_bound: bool
    python_implementation: str
    python_version: str
    runtime_platform: str
    runtime_architecture: str
    schema_inventory: dict[str, object]
    schema_inventory_sha256: str

    schema_id: ClassVar[str] = RELEASE_IDENTITY_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "engine_version": self.engine_version,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "runtime_architecture": self.runtime_architecture,
            "runtime_platform": self.runtime_platform,
            "schema_id": self.schema_id,
            "schema_inventory": self.schema_inventory,
            "schema_inventory_sha256": self.schema_inventory_sha256,
            "schema_version": self.schema_version,
            "source_bound": self.source_bound,
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class HealthCheckV1:
    check_id: str
    status: HealthStatusV1
    summary: str
    facts: dict[str, object]
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.check_id or type(self.status) is not HealthStatusV1:
            raise ValueError("release health check identity is invalid")
        if not self.summary:
            raise ValueError("release health check summary is required")

    def as_dict(self) -> dict[str, object]:
        return {
            "actions": list(self.actions),
            "check_id": self.check_id,
            "facts": self.facts,
            "status": self.status.value,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class DoctorReportV1:
    status: HealthStatusV1
    strict: bool
    identity: ReleaseIdentityV1
    checks: tuple[HealthCheckV1, ...]

    schema_id: ClassVar[str] = RELEASE_DOCTOR_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_DOCTOR_SCHEMA_VERSION_V1

    def as_dict(self) -> dict[str, object]:
        return {
            "checks": [item.as_dict() for item in self.checks],
            "identity": self.identity.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "strict": self.strict,
        }


def release_identity() -> ReleaseIdentityV1:
    """Return installed runtime identity without consulting Git or the network."""

    projection = {
        "engine_version": __version__,
        "identity_policy": "DEVELOPMENT_UNFROZEN_SOURCE_V1",
        "schema_id": RELEASE_IDENTITY_SCHEMA_ID_V1,
        "schema_version": 1,
        "source_revision": RELEASE_SOURCE_REVISION_V1,
    }
    source_sha256 = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
    inventory = builtin_release_schema_inventory(
        source_revision=RELEASE_SOURCE_REVISION_V1,
        source_sha256=source_sha256,
    )
    return ReleaseIdentityV1(
        engine_version=__version__,
        source_revision=RELEASE_SOURCE_REVISION_V1,
        source_sha256=source_sha256,
        source_bound=False,
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        runtime_platform=platform.system().casefold() or "unknown",
        runtime_architecture=platform.machine().casefold() or "unknown",
        schema_inventory=inventory.as_dict(),
        schema_inventory_sha256=inventory.sha256,
    )


def run_doctor(
    paths: DataPaths,
    *,
    strict: bool = False,
    require_starter_set: bool = False,
) -> DoctorReportV1:
    """Inspect paths, schemas, runs, packs, dependencies, and recovery state."""

    if type(paths) is not DataPaths:
        raise TypeError("release doctor requires exact DataPaths")
    if type(strict) is not bool or type(require_starter_set) is not bool:
        raise TypeError("release doctor modes must be exact booleans")
    identity = release_identity()
    checks = (
        _captured_check(
            "RELEASE_IDENTITY",
            lambda: _check_identity(identity, strict=strict),
        ),
        _captured_check("DATA_PATHS", lambda: _check_paths(paths, strict=strict)),
        _captured_check("SCHEMAS", lambda: _check_schemas(identity)),
        _captured_check("RUN_MANIFESTS", lambda: _check_run_manifests(paths)),
        _captured_check(
            "PACKS",
            lambda: _check_packs(
                paths,
                require_starter_set=require_starter_set,
            ),
        ),
        _captured_check("RECOVERY", lambda: _check_recovery(paths)),
        HealthCheckV1(
            check_id="OFFLINE_BOUNDARY",
            status=HealthStatusV1.PASS,
            summary="Release diagnostics perform no network or account operation.",
            facts={
                "account_required": False,
                "brokerage_connection": False,
                "network_access": False,
                "telemetry": False,
                "update_check": False,
            },
        ),
        HealthCheckV1(
            check_id="UNINSTALL_GUIDANCE",
            status=HealthStatusV1.PASS,
            summary="Removing the application preserves user data by default.",
            facts={
                "application_removal_deletes_user_data": False,
                "data_root_removal_requires_separate_explicit_request": True,
                "governed_area_ids": [item.value for item in DataAreaId],
            },
            actions=(
                "Back up or separately remove the displayed data root only when explicitly intended.",
            ),
        ),
    )
    status = _aggregate_status(checks)
    return DoctorReportV1(
        status=status,
        strict=strict,
        identity=identity,
        checks=checks,
    )


def verify_installation(paths: DataPaths) -> DoctorReportV1:
    """Apply the strict release-installation interpretation of doctor checks."""

    return run_doctor(paths, strict=True, require_starter_set=True)


def _captured_check(
    check_id: str,
    operation: Callable[[], HealthCheckV1],
) -> HealthCheckV1:
    try:
        result = operation()
        if result.check_id != check_id:
            raise ValueError("health check returned another check ID")
        return result
    except Exception as error:
        return HealthCheckV1(
            check_id=check_id,
            status=HealthStatusV1.FAIL,
            summary=f"{check_id.casefold().replace('_', ' ')} verification failed.",
            facts={"error": f"{type(error).__name__}: {error}"},
            actions=(
                "Preserve the data root and run export-diagnostics to a new file.",
                "Repair or restore only after reviewing the exact failing check.",
            ),
        )


def _check_identity(
    identity: ReleaseIdentityV1,
    *,
    strict: bool,
) -> HealthCheckV1:
    if identity.source_bound:
        status = HealthStatusV1.PASS
        summary = "Release identity is bound to a frozen source manifest."
        actions: tuple[str, ...] = ()
    else:
        status = HealthStatusV1.FAIL if strict else HealthStatusV1.WARN
        summary = (
            "Engine/runtime/schema identity is available, but source is not yet "
            "bound to a frozen release candidate."
        )
        actions = (
            "Use the later candidate-freeze workflow before qualifying a distributable release.",
        )
    return HealthCheckV1(
        check_id="RELEASE_IDENTITY",
        status=status,
        summary=summary,
        facts={
            "engine_version": identity.engine_version,
            "schema_inventory_sha256": identity.schema_inventory_sha256,
            "source_bound": identity.source_bound,
            "source_revision": identity.source_revision,
            "source_sha256": identity.source_sha256,
        },
        actions=actions,
    )


def _check_paths(paths: DataPaths, *, strict: bool) -> HealthCheckV1:
    paths.validate()
    rows: list[dict[str, object]] = []
    missing: list[str] = []
    unwritable: list[str] = []
    for area_id in DataAreaId:
        path = paths.area(area_id)
        exists = path.exists()
        if path.is_symlink():
            raise ValueError(f"data area is a symlink: {area_id.value}")
        writable = False
        mode = None
        if exists:
            metadata = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"data area is not a directory: {area_id.value}")
            mode = stat.S_IMODE(metadata.st_mode)
            writable = os.access(path, os.W_OK | os.X_OK)
            if not writable:
                unwritable.append(area_id.value)
        else:
            missing.append(area_id.value)
        rows.append(
            {
                "area_id": area_id.value,
                "exists": exists,
                "mode": mode,
                "path": str(path),
                "writable": writable,
            }
        )
    if unwritable or (strict and missing):
        status = HealthStatusV1.FAIL
    elif missing:
        status = HealthStatusV1.WARN
    else:
        status = HealthStatusV1.PASS
    return HealthCheckV1(
        check_id="DATA_PATHS",
        status=status,
        summary=(
            "Every governed data area exists and is writable."
            if status is HealthStatusV1.PASS
            else "One or more governed data areas require first-run creation or repair."
        ),
        facts={
            "areas": rows,
            "missing_area_ids": missing,
            "root": str(paths.root),
            "unwritable_area_ids": unwritable,
        },
        actions=(
            (
                "Run the first-run flow to create missing areas."
                if missing
                else "Correct ownership or permissions without replacing user data."
            ),
        )
        if missing or unwritable
        else (),
    )


def _check_schemas(identity: ReleaseIdentityV1) -> HealthCheckV1:
    inventory = builtin_release_schema_inventory(
        source_revision=identity.source_revision,
        source_sha256=identity.source_sha256,
    )
    restored = type(inventory).from_canonical_bytes(inventory.canonical_bytes())
    if restored != inventory or restored.sha256 != identity.schema_inventory_sha256:
        raise ValueError("release schema inventory did not round-trip exactly")
    return HealthCheckV1(
        check_id="SCHEMAS",
        status=HealthStatusV1.PASS,
        summary="The closed release schema inventory round-trips exactly.",
        facts={
            "data_paths_schema_version": inventory.data_paths_schema_version,
            "inventory_sha256": inventory.sha256,
            "schema_count": len(inventory.schemas),
            "schemas": [
                {
                    "current_version": item.current_version,
                    "kind": item.kind.value,
                    "schema_id": item.schema_id,
                }
                for item in inventory.schemas
            ],
        },
    )


def _check_run_manifests(paths: DataPaths) -> HealthCheckV1:
    if not paths.runs.exists():
        return HealthCheckV1(
            check_id="RUN_MANIFESTS",
            status=HealthStatusV1.PASS,
            summary="No immutable run manifests exist yet.",
            facts={"manifest_count": 0, "run_type_counts": {}},
        )
    paths.validate(DataAreaId.RUNS)
    candidates = tuple(sorted(paths.runs.glob("run-*/manifest.toml")))
    if len(candidates) > _MAX_RUN_MANIFEST_COUNT_V1:
        raise ValueError("run manifest count exceeds the release diagnostic bound")
    identity = release_identity()
    inventory = builtin_release_schema_inventory(
        source_revision=identity.source_revision,
        source_sha256=identity.source_sha256,
    )
    counts: dict[str, int] = {}
    schema_counts: dict[str, int] = {}
    digests: list[str] = []
    for path in candidates:
        _require_real_chain(path.parent, stop=paths.runs)
        raw = _read_regular_file(path, maximum_bytes=_MAX_RUN_MANIFEST_BYTES_V1)
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"run manifest is not valid TOML: {path.name}") from error
        manifest = RunManifest.from_dict(value)
        if manifest.to_toml().encode("utf-8") != raw:
            raise ValueError(f"run manifest is not canonical: {manifest.run_id}")
        inventory.require_supported(
            ReleaseSchemaKindV1.RUN,
            ReleaseSchemaUseV1.READ,
            manifest.schema_version,
        )
        counts[manifest.run_type.value] = counts.get(manifest.run_type.value, 0) + 1
        schema_key = str(manifest.schema_version)
        schema_counts[schema_key] = schema_counts.get(schema_key, 0) + 1
        digests.append(hashlib.sha256(raw).hexdigest())
    return HealthCheckV1(
        check_id="RUN_MANIFESTS",
        status=HealthStatusV1.PASS,
        summary=f"Verified {len(candidates)} canonical immutable run manifest(s).",
        facts={
            "manifest_count": len(candidates),
            "manifest_set_sha256": hashlib.sha256(
                canonical_json_bytes(sorted(digests))
            ).hexdigest(),
            "run_type_counts": dict(sorted(counts.items())),
            "schema_version_counts": dict(sorted(schema_counts.items())),
        },
    )


def _check_packs(
    paths: DataPaths,
    *,
    require_starter_set: bool,
) -> HealthCheckV1:
    registry = read_pack_registry(paths=paths)
    verified = tuple(_verify_pack_object(paths, entry) for entry in registry.entries)
    environment = _registry_runtime_environment(registry)
    for entry in registry.active_entries:
        resolution = resolve_pack_dependencies(entry.manifest, registry, environment)
        if resolution.registry_edges != entry.resolved_dependencies:
            raise ValueError(
                f"installed pack dependency edges differ: {entry.pack_id}"
            )

    missing_starter: list[str] = []
    starter_set_id: str | None = None
    starter_entries_sha256: str | None = None
    if require_starter_set:
        from .first_run import build_release_starter_set

        starter = build_release_starter_set()
        starter_set_id = starter.set_id
        starter_entries_sha256 = starter.entries_sha256
        active_ids = {entry.pack_id for entry in registry.active_entries}
        missing_starter = [
            item.pack_id for item in starter.entries if item.pack_id not in active_ids
        ]
    status = HealthStatusV1.FAIL if missing_starter else HealthStatusV1.PASS
    return HealthCheckV1(
        check_id="PACKS",
        status=status,
        summary=(
            f"Verified {len(verified)} installed pack object(s) and dependency closure."
            if not missing_starter
            else "The exact bundled starter set is not fully active."
        ),
        facts={
            "active_count": len(registry.active_entries),
            "entries": list(verified),
            "entry_count": len(registry.entries),
            "missing_starter_pack_ids": missing_starter,
            "registry_sha256": registry.sha256,
            "starter_entries_sha256": starter_entries_sha256,
            "starter_set_id": starter_set_id,
        },
        actions=("Run the first-run flow without overwriting conflicting packs.",)
        if missing_starter
        else (),
    )


def _verify_pack_object(
    paths: DataPaths,
    entry: PackRegistryEntryV1,
) -> dict[str, object]:
    paths.validate(DataAreaId.PACKS)
    object_root = paths.packs.joinpath(*entry.object_path.split("/"))
    try:
        object_root.relative_to(paths.packs)
    except ValueError as error:
        raise ValueError("installed pack object escapes the packs area") from error
    _require_real_chain(object_root, stop=paths.packs)
    expected_files = {
        K2PACK_MANIFEST_PATH,
        *(item.path for item in entry.manifest.inventory),
    }
    actual_files = _tree_files(object_root)
    if actual_files != expected_files:
        raise ValueError(
            f"installed pack object inventory differs: {entry.pack_id}"
        )
    manifest_raw = _read_regular_file(
        object_root / K2PACK_MANIFEST_PATH,
        maximum_bytes=4 * 1024 * 1024,
    )
    if manifest_raw != canonical_manifest_bytes(entry.manifest):
        raise ValueError(f"installed pack manifest differs: {entry.pack_id}")
    payloads: dict[str, bytes] = {}
    for item in entry.manifest.inventory:
        raw = _read_regular_file(
            object_root.joinpath(*item.path.split("/")),
            maximum_bytes=item.byte_count,
        )
        if len(raw) != item.byte_count or hashlib.sha256(raw).hexdigest() != item.sha256:
            raise ValueError(f"installed pack payload differs: {item.path}")
        payloads[item.path] = raw
    verify_pack_payload_identity(entry.manifest, payloads)
    archive_bytes = _normalized_archive(manifest_raw, payloads)
    domain = verify_domain_pack_archive_bytes(
        archive_bytes,
        expected_pack_id=entry.pack_id,
    )
    return {
        "active": entry.active,
        "domain_identity_sha256": domain.index.domain_identity_sha256,
        "pack_id": entry.pack_id,
        "pack_type": entry.manifest.pack_type.value,
        "payload_count": len(payloads),
        "resolved_dependency_count": len(entry.resolved_dependencies),
    }


def _registry_runtime_environment(
    registry: PackRegistryV1,
) -> PackRuntimeEnvironmentV1:
    base = builtin_pack_runtime_environment_v1()
    schemas: dict[str, int] = dict(base.schema_versions)
    for entry in registry.entries:
        for item in entry.manifest.inventory:
            previous = schemas.get(item.schema_id)
            if previous is not None and previous != item.schema_version:
                raise ValueError(
                    f"installed packs require conflicting schema versions: {item.schema_id}"
                )
            schemas[item.schema_id] = item.schema_version
    return PackRuntimeEnvironmentV1(
        engine_component_id=base.engine_component_id,
        engine_version=base.engine_version,
        compiler_versions=base.compiler_versions,
        schema_versions=tuple(sorted(schemas.items())),
    )


def _check_recovery(paths: DataPaths) -> HealthCheckV1:
    if not paths.checkpoints.exists():
        return HealthCheckV1(
            check_id="RECOVERY",
            status=HealthStatusV1.PASS,
            summary="No interactive recovery state exists yet.",
            facts={"active_pointer_count": 0, "checkpoint_count": 0},
        )
    paths.validate(DataAreaId.CHECKPOINTS)
    active = paths.checkpoints / "interactive" / "active"
    if not active.exists():
        return HealthCheckV1(
            check_id="RECOVERY",
            status=HealthStatusV1.PASS,
            summary="No unfinished interactive session is active.",
            facts={"active_pointer_count": 0, "checkpoint_count": 0},
        )
    _require_real_chain(active, stop=paths.checkpoints)
    pointers = tuple(sorted(active.iterdir()))
    if len(pointers) > _MAX_ACTIVE_RECOVERY_POINTERS_V1:
        raise ValueError("active recovery pointer count exceeds the V1 bound")
    checkpoint_count = 0
    pending_count = 0
    session_ids: list[str] = []
    for pointer in pointers:
        if pointer.suffix != ".json" or len(pointer.stem) != 64:
            raise ValueError("active recovery pointer name is invalid")
        journal = LiveSessionJournalV1.discover(
            paths=paths,
            configuration_sha256=pointer.stem,
        )
        if journal is None:
            raise ValueError("active recovery pointer did not resolve to a journal")
        session_ids.append(journal.session_id)
        pending_count += len(journal.pending_transactions)
        for record in journal.records:
            if record.boundary is RecoveryBoundaryKindV1.CHECKPOINT_COMMITTED:
                journal.load_checkpoint(record)
                checkpoint_count += 1
    return HealthCheckV1(
        check_id="RECOVERY",
        status=HealthStatusV1.PASS,
        summary=f"Verified {len(pointers)} active recovery journal(s).",
        facts={
            "active_pointer_count": len(pointers),
            "checkpoint_count": checkpoint_count,
            "pending_transaction_count": pending_count,
            "session_ids": session_ids,
        },
    )


def _normalized_archive(
    manifest_raw: bytes,
    payloads: dict[str, bytes],
) -> bytes:
    output = io.BytesIO()
    values = {K2PACK_MANIFEST_PATH: manifest_raw, **payloads}
    with zipfile.ZipFile(output, mode="w") as archive:
        for path in normalized_archive_paths(tuple(values)):
            archive.writestr(normalized_zip_info(path), values[path])
    return output.getvalue()


def _tree_files(root: Path) -> set[str]:
    pending = [root]
    files: set[str] = set()
    while pending:
        directory = pending.pop()
        directory_metadata = directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) & 0o222
        ):
            raise ValueError("installed pack directory is not immutable")
        with os.scandir(directory) as scan:
            entries = tuple(scan)
        for entry in entries:
            if entry.is_symlink():
                raise ValueError("installed pack tree contains a symlink")
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix()
            if entry.is_dir(follow_symlinks=False):
                pending.append(candidate)
            elif entry.is_file(follow_symlinks=False):
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_IMODE(metadata.st_mode) & 0o222:
                    raise ValueError("installed pack payload is not immutable")
                files.add(relative)
                if len(files) > _MAX_PACK_OBJECT_FILE_COUNT_V1:
                    raise ValueError("installed pack object has too many files")
            else:
                raise ValueError("installed pack tree contains a special file")
    return files


def _require_real_chain(path: Path, *, stop: Path) -> None:
    candidate = path
    while True:
        metadata = candidate.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"filesystem chain contains a symlink: {candidate.name}")
        if candidate == stop:
            return
        if stop not in candidate.parents:
            raise ValueError("filesystem chain escaped its governed root")
        candidate = candidate.parent


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("read limit must be positive")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ValueError("health-check file is empty, linked, special, or oversized")
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
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("health-check file changed during read")
        return raw
    finally:
        os.close(descriptor)


def _aggregate_status(checks: tuple[HealthCheckV1, ...]) -> HealthStatusV1:
    if any(item.status is HealthStatusV1.FAIL for item in checks):
        return HealthStatusV1.FAIL
    if any(item.status is HealthStatusV1.WARN for item in checks):
        return HealthStatusV1.WARN
    return HealthStatusV1.PASS


__all__ = [
    "RELEASE_DOCTOR_SCHEMA_ID_V1",
    "RELEASE_DOCTOR_SCHEMA_VERSION_V1",
    "RELEASE_IDENTITY_SCHEMA_ID_V1",
    "DoctorReportV1",
    "HealthCheckV1",
    "HealthStatusV1",
    "ReleaseIdentityV1",
    "release_identity",
    "run_doctor",
    "verify_installation",
]
