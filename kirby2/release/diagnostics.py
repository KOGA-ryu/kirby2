"""Explicitly allowlisted, locally exported release diagnostics."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from kirby2.packs.formats import canonical_json_bytes
from kirby2.research.paths import DataAreaId, DataPaths

from .doctor import DoctorReportV1, release_identity, run_doctor


RELEASE_DIAGNOSTICS_SCHEMA_ID_V1 = "KIRBY2_RELEASE_DIAGNOSTICS_V1"
RELEASE_DIAGNOSTICS_SCHEMA_VERSION_V1 = 1
RELEASE_DIAGNOSTICS_PREVIEW_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_DIAGNOSTICS_PREVIEW_V1"
)
RELEASE_DIAGNOSTICS_EXPORT_SCHEMA_ID_V1 = "KIRBY2_RELEASE_DIAGNOSTICS_EXPORT_V1"


@dataclass(frozen=True, slots=True)
class DiagnosticRedactionV1:
    field_class: str
    disposition: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "field_class": self.field_class,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReleaseDiagnosticsV1:
    identity: dict[str, object]
    health: dict[str, object]
    data_paths: dict[str, object]
    packs: dict[str, object]
    runs: dict[str, object]
    recovery: dict[str, object]
    environment: dict[str, object]
    redactions: tuple[DiagnosticRedactionV1, ...]

    schema_id: ClassVar[str] = RELEASE_DIAGNOSTICS_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_DIAGNOSTICS_SCHEMA_VERSION_V1

    def as_dict(self) -> dict[str, object]:
        return {
            "data_paths": self.data_paths,
            "environment": self.environment,
            "health": self.health,
            "identity": self.identity,
            "packs": self.packs,
            "recovery": self.recovery,
            "redactions": [item.as_dict() for item in self.redactions],
            "runs": self.runs,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class DiagnosticsPreviewV1:
    diagnostics_sha256: str
    byte_count: int
    included_sections: tuple[str, ...]
    redactions: tuple[DiagnosticRedactionV1, ...]

    schema_id: ClassVar[str] = RELEASE_DIAGNOSTICS_PREVIEW_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "diagnostics_sha256": self.diagnostics_sha256,
            "included_sections": list(self.included_sections),
            "redactions": [item.as_dict() for item in self.redactions],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticsExportReceiptV1:
    destination: str
    diagnostics_sha256: str
    byte_count: int
    preview: DiagnosticsPreviewV1

    schema_id: ClassVar[str] = RELEASE_DIAGNOSTICS_EXPORT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "destination": self.destination,
            "diagnostics_sha256": self.diagnostics_sha256,
            "preview": self.preview.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


def build_release_diagnostics(
    paths: DataPaths,
    *,
    authorize_hidden_lesson_truth: bool = False,
) -> ReleaseDiagnosticsV1:
    """Build a bounded summary without copying user content or direct identity."""

    if type(paths) is not DataPaths:
        raise TypeError("release diagnostics require exact DataPaths")
    if type(authorize_hidden_lesson_truth) is not bool:
        raise TypeError("hidden-truth authorization must be an exact boolean")
    report = run_doctor(paths, strict=False, require_starter_set=False)
    checks = {item.check_id: item for item in report.checks}
    identity = release_identity()
    return ReleaseDiagnosticsV1(
        identity=identity.as_dict(),
        health=_health_allowlist(report),
        data_paths=_path_allowlist(paths, checks["DATA_PATHS"].facts),
        packs=_pack_allowlist(checks["PACKS"].facts),
        runs=_run_allowlist(checks["RUN_MANIFESTS"].facts),
        recovery=_recovery_allowlist(checks["RECOVERY"].facts),
        environment={
            "engine_version": identity.engine_version,
            "python_implementation": identity.python_implementation,
            "python_version": identity.python_version,
            "runtime_architecture": identity.runtime_architecture,
            "runtime_platform": identity.runtime_platform,
        },
        redactions=_redaction_rows(authorize_hidden_lesson_truth),
    )


def preview_release_diagnostics(
    diagnostics: ReleaseDiagnosticsV1,
) -> DiagnosticsPreviewV1:
    if type(diagnostics) is not ReleaseDiagnosticsV1:
        raise TypeError("diagnostics preview requires ReleaseDiagnosticsV1")
    raw = diagnostics.canonical_bytes()
    return DiagnosticsPreviewV1(
        diagnostics_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        included_sections=(
            "data_paths",
            "environment",
            "health",
            "identity",
            "packs",
            "recovery",
            "redactions",
            "runs",
        ),
        redactions=diagnostics.redactions,
    )


def export_release_diagnostics(
    paths: DataPaths,
    destination: Path,
    *,
    authorize_hidden_lesson_truth: bool = False,
) -> DiagnosticsExportReceiptV1:
    """Write one new user-selected JSON file without overwriting existing data."""

    diagnostics = build_release_diagnostics(
        paths,
        authorize_hidden_lesson_truth=authorize_hidden_lesson_truth,
    )
    preview = preview_release_diagnostics(diagnostics)
    target = _resolved_new_destination(destination)
    raw = diagnostics.canonical_bytes()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        published = True
        temporary.unlink()
        directory_descriptor = os.open(
            target.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        if published:
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise RuntimeError(
                    "diagnostics publication failed and its new destination could "
                    f"not be rolled back: {cleanup_error}"
                ) from error
        raise
    return DiagnosticsExportReceiptV1(
        destination=str(target),
        diagnostics_sha256=preview.diagnostics_sha256,
        byte_count=preview.byte_count,
        preview=preview,
    )


def _health_allowlist(report: DoctorReportV1) -> dict[str, object]:
    return {
        "checks": [
            {"check_id": item.check_id, "status": item.status.value}
            for item in report.checks
        ],
        "status": report.status.value,
        "strict": report.strict,
    }


def _path_allowlist(
    paths: DataPaths,
    facts: dict[str, object],
) -> dict[str, object]:
    raw_areas = facts.get("areas", [])
    by_id = {
        str(row.get("area_id")): row
        for row in raw_areas
        if type(row) is dict
    }
    return {
        "areas": [
            {
                "area_id": area_id.value,
                "child": paths.area_children[area_id],
                "exists": bool(by_id.get(area_id.value, {}).get("exists", False)),
                "writable": bool(
                    by_id.get(area_id.value, {}).get("writable", False)
                ),
            }
            for area_id in DataAreaId
        ],
        "schema_version": paths.as_dict()["schema_version"],
    }


def _pack_allowlist(facts: dict[str, object]) -> dict[str, object]:
    rows = facts.get("entries", [])
    entries = []
    if type(rows) is list:
        for row in rows:
            if type(row) is not dict:
                continue
            entries.append(
                {
                    "active": bool(row.get("active", False)),
                    "domain_identity_sha256": row.get("domain_identity_sha256"),
                    "pack_id": row.get("pack_id"),
                    "pack_type": row.get("pack_type"),
                    "payload_count": row.get("payload_count"),
                    "resolved_dependency_count": row.get(
                        "resolved_dependency_count"
                    ),
                }
            )
    return {
        "active_count": facts.get("active_count", 0),
        "entries": entries,
        "entry_count": facts.get("entry_count", 0),
        "registry_sha256": facts.get("registry_sha256"),
    }


def _run_allowlist(facts: dict[str, object]) -> dict[str, object]:
    return {
        "manifest_count": facts.get("manifest_count", 0),
        "manifest_set_sha256": facts.get("manifest_set_sha256"),
        "run_type_counts": facts.get("run_type_counts", {}),
        "schema_version_counts": facts.get("schema_version_counts", {}),
    }


def _recovery_allowlist(facts: dict[str, object]) -> dict[str, object]:
    return {
        "active_pointer_count": facts.get("active_pointer_count", 0),
        "checkpoint_count": facts.get("checkpoint_count", 0),
        "pending_transaction_count": facts.get("pending_transaction_count", 0),
    }


def _redaction_rows(
    authorize_hidden_lesson_truth: bool,
) -> tuple[DiagnosticRedactionV1, ...]:
    return (
        DiagnosticRedactionV1(
            field_class="DIRECT_IDENTITY",
            disposition="EXCLUDED",
            reason=(
                "Names, contact details, identity mappings, usernames, and absolute "
                "data-root paths are outside the diagnostic allowlist."
            ),
        ),
        DiagnosticRedactionV1(
            field_class="SECRETS_AND_CREDENTIALS",
            disposition="EXCLUDED",
            reason=(
                "Tokens, passwords, keys, cookies, credential files, and arbitrary "
                "environment variables are never collected."
            ),
        ),
        DiagnosticRedactionV1(
            field_class="PROPRIETARY_DATASET_CONTENT",
            disposition="EXCLUDED",
            reason=(
                "Dataset payloads, source rows, annotations, and full research "
                "artifacts are never copied into release diagnostics."
            ),
        ),
        DiagnosticRedactionV1(
            field_class="HIDDEN_LESSON_TRUTH",
            disposition=(
                "AUTHORIZED_NOT_COLLECTED"
                if authorize_hidden_lesson_truth
                else "EXCLUDED"
            ),
            reason=(
                "Authorization was recorded, but the V1 support allowlist still "
                "does not need hidden truth."
                if authorize_hidden_lesson_truth
                else "Hidden reveal/scoring truth requires separate explicit authorization."
            ),
        ),
        DiagnosticRedactionV1(
            field_class="USER_CONTENT",
            disposition="EXCLUDED",
            reason=(
                "Run payloads, strategies, curricula, reports, logs, crash bodies, "
                "and backup contents are represented only by bounded counts/statuses."
            ),
        ),
    )


def _resolved_new_destination(destination: Path) -> Path:
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError("diagnostics destination must be an explicit absolute Path")
    try:
        resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("diagnostics destination cannot be resolved safely") from error
    if destination != resolved or resolved == Path(resolved.anchor):
        raise ValueError("diagnostics destination must be resolved and non-root")
    if resolved.suffix != ".json":
        raise ValueError("diagnostics destination must use lowercase .json")
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError("diagnostics destination already exists")
    parent = resolved.parent
    metadata = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
        raise ValueError("diagnostics destination parent must be a real directory")
    return resolved


__all__ = [
    "RELEASE_DIAGNOSTICS_EXPORT_SCHEMA_ID_V1",
    "RELEASE_DIAGNOSTICS_PREVIEW_SCHEMA_ID_V1",
    "RELEASE_DIAGNOSTICS_SCHEMA_ID_V1",
    "RELEASE_DIAGNOSTICS_SCHEMA_VERSION_V1",
    "DiagnosticRedactionV1",
    "DiagnosticsExportReceiptV1",
    "DiagnosticsPreviewV1",
    "ReleaseDiagnosticsV1",
    "build_release_diagnostics",
    "export_release_diagnostics",
    "preview_release_diagnostics",
]
